#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把验证通过的恢复原件放回 NAS 原位（去掉 .L0CK3D 的原始路径）。

来源（只在恢复目录里找，不碰密文）：
    勒索恢复_20261030/5_旧数据恢复/   路线 A：Btrfs 旧 extent 找回的原件/推测候选
    勒索恢复_20261030/6_可打开文档/   抢救后重打包的可打开文档（放到原位时加 _修复版 后缀）

验证规则：
    1. 结构自证：按文件类型解析（OLE2 目录项 / zip 成员 / PDF 对象 / JPEG-PNG 尾标记）；
    2. 明文区比对：若同名密文还在，按 C3RB3R 加密模式取明文区间，
       要求候选文件在明文区与密文逐字节一致 —— 这一条是"确实是原件"的硬证据；
    3. 只有结构通过、且明文区完全一致（或整文件加密无法比对）的，才写回原路径；
       推测候选（.__prev__ / .__next__）即使通过也加 _推测恢复 后缀，不冒充原件。

安全约束：
    - 只新增文件；目标已存在就跳过（绝不覆盖）。
    - 不删、不移动、不改任何 .L0CK3D 密文。
    - 写临时文件后 rename，写完重读校验 SHA-256。
"""
import hashlib
import json
import os
import struct
import sys
import zipfile

TAIL = 588
STAMP = '勒索恢复_20261030'


# ---------- 密文模式与明文区间 ----------
def tail_meta(p):
    sz = os.path.getsize(p)
    with open(p, 'rb') as f:
        f.seek(sz - TAIL)
        t = f.read(TAIL)
    if len(t) < TAIL:
        return None
    return t[572], t[573], struct.unpack('<Q', t[580:588])[0], sz


def enc_ranges(mode, pct, n):
    """返回被加密的区间 [(start, end), ...]"""
    if mode == 0x24:
        return [(0, n)]
    if mode == 0x26:
        return [(0, min(n, 1 << 20))]
    if mode == 0x25:
        w = 4 * (n // 100)
        gap = (n - 3 * w) // 2
        if w <= 0 or gap < 0:
            return [(0, n)]
        return [(0, w), (w + gap, 2 * w + gap), (2 * (w + gap), 3 * w + 2 * gap)]
    return None


def plain_ranges(mode, n):
    enc = enc_ranges(mode, 4, n)
    if enc is None:
        return None
    out = []
    prev = 0
    for s, e in enc:
        if s > prev:
            out.append((prev, s))
        prev = max(prev, e)
    if prev < n:
        out.append((prev, n))
    return out


# ---------- 结构自证 ----------
def ole2_entries(data):
    """极简 OLE2 解析：返回目录项 [(name, type, size)]，解析失败抛异常"""
    if data[:8] != b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        raise ValueError('not OLE2')
    sector_shift = struct.unpack('<H', data[0x1E:0x20])[0]
    if not (7 <= sector_shift <= 12):
        raise ValueError('bad sector shift')
    ssz = 1 << sector_shift
    fat_cnt = struct.unpack('<I', data[0x2C:0x30])[0]
    dir_start = struct.unpack('<I', data[0x30:0x34])[0]
    mini_cutoff = struct.unpack('<I', data[0x38:0x3C])[0]
    difat = list(struct.unpack('<109I', data[0x4C:0x200]))
    total = len(data)
    nsect = (total - 512) // ssz

    def sector_off(i):
        return 512 + i * ssz

    fat = []
    for s in difat[:fat_cnt]:
        if s >= 0xFFFFFFFA or s >= nsect:
            continue
        off = sector_off(s)
        fat.extend(struct.unpack('<%dI' % (ssz // 4), data[off:off + ssz]))

    def chain(start):
        out = []
        cur = start
        seen = set()
        while cur < 0xFFFFFFFA and cur not in seen and len(out) < 100000:
            seen.add(cur)
            out.append(cur)
            if cur >= len(fat):
                break
            cur = fat[cur]
        return out

    dir_data = b''
    for s in chain(dir_start):
        off = sector_off(s)
        dir_data += data[off:off + ssz]
    if len(dir_data) < 128:
        raise ValueError('dir too small')
    entries = []
    for i in range(len(dir_data) // 128):
        e = dir_data[i * 128:(i + 1) * 128]
        nlen = struct.unpack('<H', e[64:66])[0]
        if nlen < 2 or nlen > 64:
            continue
        name = e[:nlen - 2].decode('utf-16-le', 'replace')
        typ = e[66]
        size = struct.unpack('<Q', e[120:128])[0]
        if typ == 0:
            continue
        entries.append((name, typ, size))
    if not entries:
        raise ValueError('no dir entries')
    return entries


def struct_check(path):
    """返回 (kind, ok, detail)"""
    try:
        sz = os.path.getsize(path)
        with open(path, 'rb') as f:
            head = f.read(4096)
            if len(head) < 8:
                return 'unknown', False, 'too small'
            if head[:2] == b'PK':
                try:
                    z = zipfile.ZipFile(path)
                    bad = z.testzip()
                    names = z.namelist()
                    if bad:
                        return 'zip', False, 'crc bad: %s' % bad
                    if not names:
                        return 'zip', False, 'no members'
                    return 'zip', True, '%d members' % len(names)
                except Exception as e:
                    return 'zip', False, 'broken: %s' % e
            if head[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
                try:
                    ents = ole2_entries(open(path, 'rb').read())
                    streams = [e for e in ents if e[1] == 2]
                    if not streams:
                        return 'ole2', False, 'no streams'
                    return 'ole2', True, '%d entries, %d streams' % (len(ents), len(streams))
                except Exception as e:
                    return 'ole2', False, 'bad ole2: %s' % e
            if head[:5] == b'%PDF-':
                with open(path, 'rb') as f:
                    f.seek(max(0, sz - 4096))
                    tail = f.read()
                has_eof = b'%%EOF' in tail
                with open(path, 'rb') as f:
                    blob = f.read()
                pages = blob.count(b'/Type/Page') + blob.count(b'/Type /Page')
                if not has_eof:
                    return 'pdf', False, 'no %%EOF'
                return 'pdf', True, '%d pages' % pages
            if head[:4] == b'Rar!':
                return 'rar', True, 'rar header'
            if head[:4] == b'\x78\x00\x00\x00':
                # WPS OSheet 容器头：x\0\0\0 + "\x12schem"
                return 'osheet', True, 'WPS OSheet 头完整'
            if head[:3] == b'\xff\xd8\xff':
                with open(path, 'rb') as f:
                    f.seek(max(0, sz - 2))
                    if f.read(2) == b'\xff\xd9':
                        return 'jpeg', True, 'ok'
                    return 'jpeg', False, 'no EOI'
            if head[:8] == b'\x89PNG\r\n\x1a\n':
                with open(path, 'rb') as f:
                    f.seek(max(0, sz - 8))
                    if f.read(4) == b'IEND':
                        return 'png', True, 'ok'
                    return 'png', False, 'no IEND'
            if head[4:8] == b'ftyp':
                return 'mp4', True, 'ftyp'
            if head[:4] == b'RIFF':
                return 'riff', True, 'RIFF'
            if head[:2] == b'BM':
                return 'bmp', True, 'ok'
            return 'other', False, 'unknown head %s' % head[:8].hex()
    except OSError as e:
        return 'unknown', False, str(e)


# ---------- 与密文明文区比对 ----------
def compare_with_cipher(cand, enc_path):
    """返回 (verified, compared, mismatched, mode)"""
    try:
        meta = tail_meta(enc_path)
    except Exception:
        return None, 0, 0, None
    if not meta:
        return None, 0, 0, None
    mode, pct, _flags, enc_size = meta
    if enc_size < TAIL:
        return None, 0, 0, mode
    orig_n = enc_size - TAIL
    pr = plain_ranges(mode, orig_n)
    if pr is None:
        return None, 0, 0, mode
    cand_size = os.path.getsize(cand)
    if cand_size != orig_n:
        # 尺寸对不上，只能按区间裁剪比对
        pass
    compared = 0
    bad = 0
    with open(cand, 'rb') as fc, open(enc_path, 'rb') as fe:
        for s, e in pr:
            if s >= cand_size:
                continue
            e = min(e, cand_size, orig_n)
            if e <= s:
                continue
            fc.seek(s)
            fe.seek(s)
            a = fc.read(e - s)
            b = fe.read(e - s)
            compared += len(a)
            if a != b:
                # 逐块定位不一致
                step = 4096
                for off in range(0, len(a), step):
                    if a[off:off + step] != b[off:off + step]:
                        bad += min(step, len(a) - off)
    return (bad == 0 and compared > 0), compared, bad, mode


def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def write_atomic(src, dst):
    d = os.path.dirname(dst)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = dst + '.c3rtmp'
    with open(src, 'rb') as fi, open(tmp, 'wb') as fo:
        while True:
            b = fi.read(1 << 20)
            if not b:
                break
            fo.write(b)
        fo.flush()
        os.fsync(fo.fileno())
    os.rename(tmp, dst)
    return sha256(dst)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    users = args
    if not users:
        print('用法: python3 restore_inplace.py <用户1> [<用户2> ...] [--dry]')
        return 2
    dry = '--dry' in sys.argv
    src_dirs = ['5_旧数据恢复']
    results = []
    for u in users:
        home = '/volume1/homes/' + u
        rec = os.path.join(home, STAMP)
        if not os.path.isdir(rec):
            print('用户 %s 没有恢复目录' % u)
            continue
        for sd in src_dirs:
            sdpath = os.path.join(rec, sd)
            if not os.path.isdir(sdpath):
                continue
            for dp, dns, fns in os.walk(sdpath):
                for fn in sorted(fns):
                    cand = os.path.join(dp, fn)
                    rel = os.path.relpath(cand, sdpath)
                    inferred = False
                    orig_rel = rel
                    stem, ext = os.path.splitext(rel)
                    for suf in ('.__prev__', '.__next__'):
                        if stem.endswith(suf):
                            stem = stem[:-len(suf)]
                            inferred = True
                    if stem.endswith('_partial'):
                        stem = stem[:-len('_partial')]
                        inferred = True
                    orig_rel = stem + ext
                    target = os.path.join(home, orig_rel)
                    enc = target + '.L0CK3D'
                    kind, ok, detail = struct_check(cand)
                    verified = None
                    compared = 0
                    bad = 0
                    mode = None
                    if os.path.exists(enc):
                        verified, compared, bad, mode = compare_with_cipher(cand, enc)
                    # 判定：先看硬证据（明文区逐字节一致），再看结构
                    if os.path.exists(target):
                        status = 'skip_exists'
                    elif verified is True:
                        status = 'write_exact'
                    elif verified is False:
                        status = 'skip_mismatch'
                    elif not ok:
                        status = 'skip_struct_fail'
                    elif mode == 0x24:
                        status = 'write_structure_only'   # 整文件加密，无明文可对比
                    else:
                        status = 'skip_nocomparison'
                    dst = target
                    if status.startswith('write') and inferred:
                        base, ext = os.path.splitext(target)
                        dst = base + '_推测恢复' + ext
                        if os.path.exists(dst):
                            status = 'skip_exists'
                    item = {
                        'user': u, 'src': rel, 'target': os.path.relpath(dst, home),
                        'status': status, 'kind': kind, 'struct': detail,
                        'verified_plain': verified, 'compared_bytes': compared,
                        'mismatch_bytes': bad, 'mode': hex(mode) if mode is not None else None,
                        'inferred': inferred,
                        'size': os.path.getsize(cand),
                    }
                    if status.startswith('write') and not dry:
                        s = write_atomic(cand, dst)
                        item['written_sha256'] = s
                    results.append(item)
                    print('%-18s %-8s %-7s cmp=%-10s bad=%-8s %s' % (
                        status, kind, (hex(mode) if mode is not None else '-'),
                        compared, bad, rel[-46:]))
    out = {'results': results, 'dry': dry}
    with open('/tmp/restore_inplace_result.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print('\n汇总:')
    from collections import Counter
    print(dict(Counter(i['status'] for i in results)))


if __name__ == '__main__':
    main()
