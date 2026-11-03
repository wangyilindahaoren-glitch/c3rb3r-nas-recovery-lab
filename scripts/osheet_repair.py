#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OSheet 容器修复器：修正头部字段与条目链，其余字节逐字节保留。

文法（已用两个精确恢复的良品文件反推确认，并由本脚本做逐字节往返验证）：
    文件 := magic(4) + 头部TLV* + 条目* + [附件数据区] + [尾部段]
    头部 := [u8 tag][key\\0][值]；tag=0x08 用 u8 长度，其余用 u32 长度
           值之后紧跟的 0x00 视为字段分隔字节（吃掉后继续解析下一个字段）
    条目 := [u32 X][u8 tag][name\\0][u32 len][tag==0x05 ? u8 0x00 : 无][payload(len)][尾随字节]
            X = 从 X 字段自身到本条目结束的总字节数
    尾随字节 = payload 结束到下一个条目（或附件数据区）之间的原始字节
    尾部段 := [u32 总长][u8 0x08]["end\\0"][值]

条目名的两种形态：
    压缩型  @C_text/comment / @C_metatext   （前面带 @C_ 前缀）
    不压缩型 text/comment   / metatext      （没有前缀）
  注意裸名 "metatext" 会和 0x05 条目内嵌的未压缩副本撞名，
  所以它只有在"X 正好落在某个可信条目起点或尾部段起点"时才算真条目。

修的四件事：
    1. schema 长度字段强制为 3（实测被推断成 2 时 WPS 报"无效文件格式"）
    2. id / ver 两个字段的值写反时换回来（id 形如 b"<4位uid>_<docid>"）
    3. 末尾多追加的 11 字节尾段（用勒索标记里的原始长度裁掉）
    4. 条目链的 X 与 len 按真实 payload 长度重算；附件数据区移回条目链之外

用法：
    python3 osheet_repair.py <输入> <输出> [--orig-len=N] [--keep-dup-chain]
    python3 osheet_repair.py --selftest <良品1> <良品2> ...
"""
import os
import re
import sys
import zlib

MAGIC = b'\x78\x00\x00\x00'
TRUSTED_RE = re.compile(rb'@C_[A-Za-z0-9_/]+|text/[A-Za-z0-9_]+')
METATEXT_RE = re.compile(rb'metatext')
ZLIB_HEADS = (b'\x78\x9c', b'\x78\xda', b'\x78\x01')
ZERO = b'\x00'
# id 字段取值形如 "<4位uid>_<docid>"（uid 是作者账号号，每人不同）
UID_PREFIX_RE = re.compile(rb'^\d{4}_')


def _is_uid_prefix(val):
    """判断一个头部字段值是不是 uid 前缀形态。"""
    return bool(val) and bool(UID_PREFIX_RE.match(val))


def prescan(data):
    """解析前修两处已知毛病。

    1) schema 长度字段被推断成 2（正常是 3）——头部第一个字段位置固定，直接改。
    2) 上一轮"容器重建"会在文件末尾多追加一段 11 字节的尾段，
       结果末尾出现两段尾段。去掉最后那段，文件长度就回到原始长度。
    """
    notes = []
    if len(data) >= 16 and data[4] == 0x12 and data[5:12] == b'schema' + ZERO:
        ln = int.from_bytes(data[12:16], 'little')
        if ln != 3:
            data = data[:12] + (3).to_bytes(4, 'little') + data[16:]
            notes.append('schema 长度字段 %d -> 3（偏移 12）' % ln)
    if len(data) >= 22:
        a = data[-11:]
        b = data[-22:-11]
        if (a[4] == 0x08 and a[5:9] == b'end' + ZERO and
                int.from_bytes(a[:4], 'little') == 11 and
                b[4] == 0x08 and b[5:9] == b'end' + ZERO and
                int.from_bytes(b[:4], 'little') == 11):
            data = data[:-11]
            notes.append('去掉末尾多追加的 11 字节尾段（原来有两段）')
    return data, notes


def parse_header(data):
    """返回 (字段块列表, 第一条目偏移, 错误)

    字段块 = dict(tag, key, value(值字节), sep(分隔字节), raw(整块字节))
    """
    pos = 4
    hdr = []
    while pos < len(data):
        if pos + 5 <= len(data) and data[pos + 4] in (0x05, 0x02) and (
                data[pos + 5:pos + 8] == b'@C_' or data[pos + 5:pos + 9] == b'text'):
            break
        tag = data[pos]
        e = data.find(ZERO, pos + 1)
        if e < 0:
            return hdr, pos, 'header key truncated @%d' % pos
        key = data[pos + 1:e].decode('latin1')
        p = e + 1
        if tag == 0x08:
            if p >= len(data):
                return hdr, pos, 'header length truncated'
            ln = data[p]
            p += 1
        else:
            if p + 4 > len(data):
                return hdr, pos, 'header length truncated @%d' % pos
            ln = int.from_bytes(data[p:p + 4], 'little')
            p += 4
        if p + ln > len(data):
            return hdr, pos, 'header value out of range @%d %s' % (pos, key)
        val = data[p:p + ln]
        q = p + ln
        sep = b''
        if q < len(data) and data[q] == 0:
            sep = ZERO
            q += 1
        hdr.append(dict(tag=tag, key=key, value=val, sep=sep,
                        raw=data[pos:q], start=pos, end=q))
        pos = q
    return hdr, pos, None


def zlib_scan(data, start):
    d = zlib.decompressobj()
    try:
        out = d.decompress(data[start:])
        out += d.flush()
    except Exception:
        return None
    if not d.eof:
        return None
    return (len(data) - start - len(d.unused_data), len(out))


def _entry_at(data, es, tag, name):
    """已知条目起点与 tag、名字，定位 payload。返回 (start, tag, name, len, ps, pl)"""
    p = es + 5 + len(name) + 1
    if p + 4 > len(data):
        return None
    ln = int.from_bytes(data[p:p + 4], 'little')
    base = p + 4
    if tag == 0x05:
        hit = None
        for off in (0, 1):
            q = base + off
            if q + 2 <= len(data) and data[q:q + 2] in ZLIB_HEADS:
                r = zlib_scan(data, q)
                if r:
                    hit = (q, r[0])
                    break
        if hit is None:
            return None
        return (es, tag, name, ln, hit[0], hit[1])
    if base + ln > len(data):
        return None
    return (es, tag, name, ln, base, ln)


def find_entries(data, start, footer=None):
    """找出全部条目。返回 [(start, tag, name, len, payload_start, payload_len)]

    可信名字（@C_xxx / text/xxx）直接采信；
    裸名 "metatext" 只有在它的 X 正好落在某个可信条目起点或尾部段起点上时
    才算真条目（否则会撞上 0x05 条目内嵌的未压缩副本）。
    """
    out = []
    for m in TRUSTED_RE.finditer(data, start):
        s = m.start()
        if s < 5:
            continue
        tag = data[s - 1]
        if tag not in (0x05, 0x02):
            continue
        e = _entry_at(data, s - 5, tag, m.group(0))
        if e:
            out.append(e)
    trusted_starts = set(e[0] for e in out)
    # 裸 metatext：校验 X 是否正好落在可信条目起点 / 尾部段起点
    for m in METATEXT_RE.finditer(data, start):
        s = m.start()
        if s < 5 or data[s - 1] != 0x02:
            continue
        es = s - 5
        X = int.from_bytes(data[es:es + 4], 'little')
        if X <= 0:
            continue
        nxt = es + X
        if nxt not in trusted_starts and not (footer is not None and nxt == footer):
            continue
        e = _entry_at(data, es, 0x02, m.group(0))
        if e:
            out.append(e)
    out.sort()
    ded, seen = [], set()
    for e in out:
        k = (e[0], e[4])
        if k in seen:
            continue
        seen.add(k)
        ded.append(e)
    return ded


def build_header(src_hdr, notes):
    """按源文件的头部块重建头部（长度不变）。

    - id / ver 的值写反时互换两个值（长度字段跟着换，总长度不变）
    """
    idb = next((i for i, b in enumerate(src_hdr) if b['key'] == 'id'), None)
    vrb = next((i for i, b in enumerate(src_hdr) if b['key'] == 'ver'), None)
    if idb is not None and vrb is not None:
        iv = src_hdr[idb]['value']
        vv = src_hdr[vrb]['value']
        # id 的取值形如 "<4位uid>_<docid>"；ver 是另一种格式。两者写反时，
        # 只要**有且只有一个**符合 uid 前缀特征，就把它们换回来。
        if iv and vv and _is_uid_prefix(iv) != _is_uid_prefix(vv):
            src_hdr[idb]['value'] = vv
            src_hdr[vrb]['value'] = iv
            notes.append('id / ver 两个字段的值写反，已互换（id 恢复为 <uid>_ 开头）')
    out = bytearray(MAGIC)
    for b in src_hdr:
        v = b['value']
        if b['tag'] == 0x08:
            out += bytes([b['tag']]) + b['key'].encode('latin1') + ZERO + \
                bytes([len(v)]) + v + b['sep']
        else:
            out += bytes([b['tag']]) + b['key'].encode('latin1') + ZERO + \
                len(v).to_bytes(4, 'little') + v + b['sep']
    return bytes(out)


def find_footer(data):
    """返回尾部段起点，找不到返回 None。尾部段 = [u32 总长][u8 0x08]["end\\0"][值]"""
    for off in range(0, 64):
        st = len(data) - 11 - off
        if st < 0:
            break
        if data[st + 4] == 0x08 and data[st + 5:st + 9] == b'end' + ZERO:
            ln = int.from_bytes(data[st:st + 4], 'little')
            if ln == len(data) - st:
                return st
    return None


def build_chain(data, ents, drop_dup_chain=True, footer=None):
    """沿 X 从第一条目往后走，得到连续可信的条目链。返回 (chain, stop, fixes)

    - X 指不到下一个条目就停（不猜、不并进前一条目的尾巴）；
    - X 越过了中间某条目、而那个条目自身自洽（它的 X 正好落在某个可信条目
      或尾部段起点上）时，说明前一条目的 X 推断错了，缩短它并把中间条目接进来。
    """
    by_start = {e[0]: e for e in ents}
    starts = sorted(by_start)
    chain = []
    seen_names = set()
    fixes = []
    pos = ents[0][0]
    stop = 'chain-end'
    while True:
        e = by_start.get(pos)
        if e is None:
            stop = 'no-entry-at-%d' % pos
            break
        if e[2] in seen_names:
            stop = 'dup-chain@%d' % pos
            break
        seen_names.add(e[2])
        chain.append(e)
        X = int.from_bytes(data[pos:pos + 4], 'little')
        if X <= 0:
            stop = 'bad-x'
            break
        nxt = pos + X
        if nxt > len(data):
            stop = 'x-overrun'
            break
        # X 越过的中间条目里有没有自身自洽的？
        cand = None
        for s in starts:
            if s <= pos or s >= nxt:
                continue
            sx = int.from_bytes(data[s:s + 4], 'little')
            if sx <= 0:
                continue
            if (s + sx) in by_start or (footer is not None and (s + sx) == footer):
                cand = s
                break
        if cand is not None:
            fixes.append((pos, X, cand))
            pos = cand
            continue
        if nxt in by_start:
            pos = nxt
            continue
        stop = 'blob-after-%d' % pos
        break
    good = []
    for e in chain:
        if e[4] is None:
            stop = 'payload-broken@%d' % e[0]
            break
        good.append(e)
    return good, stop, fixes


def repair(src_path, dst_path, verbose=True, drop_dup_chain=True, orig_len=None):
    raw = open(src_path, 'rb').read()
    data, notes = prescan(raw)
    # 上一轮"容器重建"会在文件末尾多追加一段 11 字节的尾段：
    # 原始长度已知时，按长度裁掉（末尾那 11 字节确实是尾段才裁）
    if orig_len and len(data) == orig_len + 11:
        tail11 = data[-11:]
        if (tail11[4] == 0x08 and tail11[5:9] == b'end' + ZERO and
                int.from_bytes(tail11[:4], 'little') == 11):
            data = data[:-11]
            notes.append('去掉末尾多追加的 11 字节尾段（原始长度 %d）' % orig_len)
    foot = find_footer(data)
    hdr, first, err = parse_header(data)
    if err:
        raise ValueError(err)
    head = build_header(hdr, notes)
    ents = find_entries(data, first, foot)
    if not ents:
        raise ValueError('找不到条目')
    chain, stop, fixes = build_chain(data, ents, drop_dup_chain, foot)
    if not chain:
        raise ValueError('条目链为空')

    last = chain[-1]
    X_src = int.from_bytes(data[last[0]:last[0] + 4], 'little')
    payload_end = last[4] + last[5]
    dup_start = None
    if stop.startswith('dup-chain'):
        dup_start = int(stop.split('@')[1])
    # 附件条目的 X 不该吞掉后面的附件数据区：条目本体 = 载荷 + 1 字节结束符
    own_end = payload_end
    if payload_end < len(data) and data[payload_end] == 0:
        own_end = payload_end + 1
    if dup_start is not None:
        tail_start = own_end
    elif last[2].endswith(b'attachment') and own_end - last[0] < X_src:
        tail_start = own_end
        notes.append('附件条目的 X 原本吞掉了附件数据区，已改回条目本体长度')
    else:
        tail_start = last[0] + X_src
        if X_src <= 0 or tail_start > len(data) or tail_start < payload_end:
            tail_start = payload_end

    out = bytearray(head)
    for i, e in enumerate(chain):
        es, tag, name, ln, ps, pl = e
        payload = data[ps:ps + pl]
        bound = chain[i + 1][0] if i + 1 < len(chain) else tail_start
        trail = data[ps + pl:bound]
        nb = name
        pre = ZERO if tag == 0x05 else b''
        span = 4 + 1 + len(nb) + 1 + 4 + len(pre) + len(payload) + len(trail)
        out += span.to_bytes(4, 'little') + bytes([tag]) + nb + ZERO + \
            len(payload).to_bytes(4, 'little') + pre + payload + trail
    if dup_start is not None:
        tail = data[tail_start:dup_start]
        if foot is not None:
            tail += data[foot:]
        notes.append('丢弃重复条目链（%s 起）；附件 blob 移出条目链' % stop)
    else:
        tail = data[tail_start:]
    out += tail
    with open(dst_path, 'wb') as f:
        f.write(out)
    if verbose:
        print('修复 %s -> %s' % (os.path.basename(src_path), os.path.basename(dst_path)))
        print('  条目 %d/%d；%d -> %d 字节；链止于 %s；尾部段 %s'
              % (len(chain), len(ents), len(raw), len(out), stop,
                 ('在 %d' % foot) if foot is not None else '无'))
        for n in notes:
            print('  * %s' % n)
    return bytes(out), notes


def selftest(paths):
    """拿正常件往返：输出必须与输入逐字节一致"""
    ok = True
    for p in paths:
        data = open(p, 'rb').read()
        out, notes = repair(p, '/tmp/_selftest.osheet', verbose=False)
        same = (out == data)
        print('%-44s 往返%s %s' % (os.path.basename(p)[:44],
                                  '逐字节一致' if same else '不一致',
                                  ('；'.join(notes) if notes else '')))
        if not same:
            ok = False
            n = min(len(out), len(data))
            for i in range(n):
                if out[i] != data[i]:
                    print('    首个不同位置 %d (原 %02x 新 %02x)'
                          % (i, data[i], out[i]))
                    break
            else:
                print('    长度不同: 原 %d 新 %d' % (len(data), len(out)))
    return ok


if __name__ == '__main__':
    a = sys.argv[1:]
    if a and a[0] == '--selftest':
        sys.exit(0 if selftest(a[1:]) else 1)
    if len(a) < 2:
        print(__doc__)
        sys.exit(2)
    ol = None
    for x in a[2:]:
        if x.startswith('--orig-len='):
            ol = int(x.split('=')[1])
    repair(a[0], a[1], drop_dup_chain=('--keep-dup-chain' not in a), orig_len=ol)
