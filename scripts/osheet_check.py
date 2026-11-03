#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 .osheet 真实文法做"能不能打开"级校验。

文法（已用两个精确恢复的良品文件反推确认）：
    文件 := magic(4) + 头部TLV* + 条目* + [尾部段]
    头部 := [u8 tag][key\\0][值]，tag=0x08 用 u8 长度，其余用 u32 长度
    条目 := [u32 X][u8 tag][name\\0][u32 len][tag==0x05 ? u8 0x00 : ∅][payload(len)][u8 0x00][可选附加块]
            X = 从 X 字段自身到本条目结束的总字节数
    附加块 := [u8 0x02][name\\0][u32 len][raw]      （未压缩副本，@C_metatext 有）
    尾部 := [u32 总长][u8 0x08]["end\\0"][值]

校验项：
    1 magic / 头部 TLV 自洽
    2 条目链首尾相接：entry[i+1].start == entry[i].start + X[i]
    3 每个条目的 len 字段 == 实际 payload 长度（0x05 用 zlib 自终止判定）
    4 0x05 条目 len 之后紧跟 0x00；payload 之后紧跟 0x00
    5 最后一个条目正好落在尾部段起点（或文件末尾）
    6 尾部段长度字段 == 剩余字节数
退出码：0 = 全部通过，1 = 有问题（逐条打印）
"""
import re
import sys
import zlib

MAGIC = b'\x78\x00\x00\x00'
NAME_RE = re.compile(rb'@C_[A-Za-z0-9_/]+|text/[A-Za-z0-9_]+')
METATEXT_RE = re.compile(rb'metatext')
ZLIB_HEADS = (b'\x78\x9c', b'\x78\xda', b'\x78\x01')


def zlib_scan(data, start):
    """从 start 开始找 zlib 自终止；返回 (压缩长度, 解压长度) 或 None"""
    d = zlib.decompressobj()
    try:
        out = d.decompress(data[start:])
        out += d.flush()
    except Exception:
        return None
    if not d.eof:
        return None
    return (len(data) - start - len(d.unused_data), len(out))


def parse_header(data):
    pos = 4
    hdr = []
    while pos < len(data):
        if pos + 5 <= len(data) and data[pos + 4] in (0x05, 0x02) and \
                (data[pos + 5:pos + 8] == b'@C_' or data[pos + 5:pos + 9] == b'text'):
            break
        tag = data[pos]
        if tag == 0x00:
            # 旧版 WPS（schema=2）在头部字段之间多一个 0x00 填充字节，跳过
            pos += 1
            continue
        e = data.find(b'\x00', pos + 1)
        if e < 0:
            return hdr, pos, '头部 key 截断 @%d' % pos
        key = data[pos + 1:e].decode('latin1')
        p = e + 1
        if tag == 0x08:
            if p >= len(data):
                return hdr, pos, '头部长度截断'
            ln = data[p]; p += 1
        else:
            if p + 4 > len(data):
                return hdr, pos, '头部长度截断 @%d' % pos
            ln = int.from_bytes(data[p:p + 4], 'little'); p += 4
        if p + ln > len(data):
            return hdr, pos, '头部值越界 @%d %s' % (pos, key)
        hdr.append((tag, key, data[p:p + ln]))
        p += ln
        if p < len(data) and data[p] == 0:
            p += 1
        pos = p
    return hdr, pos, None


def find_entries(data, start, footer=None):
    """按名字锚点 + zlib 自终止定位条目，返回 [(start, tag, name, len, payload_start, payload_len)]

    可信名字（@C_xxx / text/xxx）直接采信；
    裸名 "metatext" 只有在它的 X 正好落在某个可信条目起点或尾部段起点上时
    才算真条目（否则会撞上 0x05 条目内嵌的未压缩副本）。
    """
    out = []
    for m in NAME_RE.finditer(data, start):
        s = m.start()
        if s < 5:
            continue
        tag = data[s - 1]
        if tag not in (0x05, 0x02):
            continue
        p = m.end() + 1
        if p + 4 > len(data):
            continue
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
                out.append((s - 5, tag, m.group(0), ln, None, None))
                continue
            out.append((s - 5, tag, m.group(0), ln, hit[0], hit[1]))
        else:
            out.append((s - 5, tag, m.group(0), ln, base, ln))
    trusted = set(e[0] for e in out)
    for m in METATEXT_RE.finditer(data, start):
        s = m.start()
        if s < 5 or data[s - 1] != 0x02:
            continue
        es = s - 5
        X = int.from_bytes(data[es:es + 4], 'little')
        if X <= 0:
            continue
        nxt = es + X
        if nxt not in trusted and not (footer is not None and nxt == footer):
            continue
        p = s + len(m.group(0)) + 1
        if p + 4 > len(data):
            continue
        ln = int.from_bytes(data[p:p + 4], 'little')
        base = p + 4
        if base + ln > len(data):
            continue
        out.append((es, 0x02, m.group(0), ln, base, ln))
    out.sort()
    # 去重（同名锚点可能重复命中）
    ded = []
    seen = set()
    for e in out:
        k = (e[0], e[4])
        if k in seen:
            continue
        seen.add(k)
        ded.append(e)
    return ded


def check(path, verbose=True):
    data = open(path, 'rb').read()
    problems = []
    if data[:4] != MAGIC:
        problems.append('magic 不对: %s' % data[:4].hex())
        return problems, None
    foot0 = None
    for off in range(0, 64):
        st = len(data) - 11 - off
        if st < 0:
            break
        if data[st + 4] == 0x08 and data[st + 5:st + 9] == b'end\x00':
            ln = int.from_bytes(data[st:st + 4], 'little')
            if ln == len(data) - st:
                foot0 = st
                break
    hdr, pos, err = parse_header(data)
    if err:
        problems.append(err)
        return problems, None
    ents = find_entries(data, pos, foot0)
    if not ents:
        problems.append('找不到条目')
        return problems, None
    info = []
    for i, (es, tag, name, ln, ps, pl) in enumerate(ents):
        X = int.from_bytes(data[es:es + 4], 'little')
        nm = name.decode('latin1')
        info.append(dict(start=es, tag=tag, name=nm, len=ln, X=X,
                         ps=ps, pl=pl))
        if ps is None:
            problems.append('条目 %s @%d payload 定位失败' % (nm, es))
            continue
        # 3. len 字段是否等于实际 payload 长度
        if ln != pl:
            problems.append('条目 %s @%d len 字段=%d 实际 payload=%d' % (nm, es, ln, pl))
        # 4. 结构标记
        if tag == 0x05:
            if data[ps - 1] != 0x00:
                problems.append('条目 %s @%d len 之后不是 0x00 (0x%02x)' % (nm, es, data[ps - 1]))
        end = ps + pl
        if end < len(data) and data[end] not in (0x00, 0x02):
            problems.append('条目 %s @%d payload 之后不是 0x00/0x02 (0x%02x)' % (nm, es, data[end]))
        # 2. 链：下一个条目的起点 == 本条目起点 + X
        if i + 1 < len(ents):
            nxt = ents[i + 1][0]
            if es + X != nxt:
                problems.append('条目 %s @%d X=%d 指向 %d，但下一个条目在 %d（差 %+d）'
                                % (nm, es, X, es + X, nxt, nxt - (es + X)))
        # 本条目声明结束位置是否越过下一个条目
        if i + 1 < len(ents) and es + X > ents[i + 1][0]:
            problems.append('条目 %s @%d X=%d 越过下一个条目' % (nm, es, X))
    last = info[-1]
    last_end = last['start'] + last['X']
    # 5/6. 尾部段：若有则在文件末尾；末条目与尾部段之间允许有尾部 blob
    footer = None
    for off in range(0, 64):
        st = len(data) - 11 - off
        if st < 0:
            break
        if data[st + 4] == 0x08 and data[st + 5:st + 9] == b'end\x00':
            ln = int.from_bytes(data[st:st + 4], 'little')
            if ln == len(data) - st:
                footer = st
                break
    if footer is not None:
        if last_end > footer:
            problems.append('末条目 X=%d 结束于 %d，越过尾部段起点 %d'
                            % (last['X'], last_end, footer))
    else:
        # 没有尾部段时，末条目之后允许有尾部 blob
        if last_end > len(data):
            problems.append('末条目 X=%d 结束于 %d，超过文件末尾 %d'
                            % (last['X'], last_end, len(data)))
    if verbose:
        print('%-46s 条目=%-3d %s' % (path.split('/')[-1][:46], len(ents),
                                     'OK' if not problems else 'FAIL(%d)' % len(problems)))
        for p in problems:
            print('    ! %s' % p)
    return problems, info


if __name__ == '__main__':
    bad = 0
    for p in sys.argv[1:]:
        pr, _ = check(p)
        if pr:
            bad += 1
    sys.exit(1 if bad else 0)
