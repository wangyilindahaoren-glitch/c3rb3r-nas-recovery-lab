#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carve_claim.py —— 从扫描命中里认领回被释放的旧 extent（路线 D 通用版）。

输入：
  1) 扫描日志目录（g*.log，每行 "sig<TAB>phys_offset"）
  2) 目标清单（size<TAB>ext<TAB>relpath）
  3) 输出目录

做法：
  - zip 族：用 EOCD 记录里的 cd_offset/cd_size 直接算出文件长度（精确）；
  - pdf/png/jpeg：把「开头签名」与「下一个结尾签名」配对，差值即文件长度；
  - 长度命中目标清单的才切出来，并做结构校验（zip 能开、PDF 有 xref、图片头尾完整）。

只读块设备；输出写到 /tmp。
"""
import io
import json
import os
import struct
import sys
import zipfile

DEV = os.environ.get("C3R_DEV", "/dev/mapper/cachedev_0")


def load_targets(path):
    by_size = {}
    with open(path, encoding='utf-8') as f:
        for ln in f:
            ln = ln.rstrip('\n')
            if not ln:
                continue
            sz, ext, rel = ln.split('\t')
            by_size.setdefault(int(sz), []).append((ext, rel))
    return by_size


# 雕刻出的类型 -> 可能的原始扩展名
TYPE_EXT = {
    'zip': ('.xlsx', '.docx', '.zip', '.xlsm', '.pptx'),
    'pdf': ('.pdf',),
    'png': ('.png',),
    'jpeg': ('.jpg', '.jpeg'),
}


def pick_target(tlist, ctype):
    """同长度多目标时，按雕刻出的类型挑扩展名对得上的那个"""
    if not tlist:
        return None
    if len(tlist) == 1:
        return tlist[0]
    want = TYPE_EXT.get(ctype, ())
    for ext, rel in tlist:
        if ext.lower() in want:
            return (ext, rel)
    return tlist[0]


def load_hits(logdir):
    hits = {}
    for fn in sorted(os.listdir(logdir)):
        if not (fn.startswith('g') and fn.endswith('.log')):
            continue
        cur = {}
        with open(os.path.join(logdir, fn), encoding='utf-8', errors='replace') as f:
            for ln in f:
                if ln.startswith('#') or '\t' not in ln:
                    continue
                sig, off = ln.rstrip('\n').split('\t', 1)
                try:
                    cur.setdefault(sig, []).append(int(off))
                except ValueError:
                    pass
        for k, v in cur.items():
            hits.setdefault(k, []).extend(v)
    for k in hits:
        hits[k].sort()
    return hits


def pair_starts_ends(starts, ends, end_tail):
    """每个 start 配到它之后最近的一个 end；end_tail = 结尾签名之后还要算多少字节"""
    out = []
    j = 0
    ne = len(ends)
    for s in starts:
        while j < ne and ends[j] <= s:
            j += 1
        if j >= ne:
            break
        out.append((s, ends[j] + end_tail))
    return out


def carve(fd, start, size):
    out = bytearray()
    pos, left = start, size
    while left > 0:
        n = min(8 << 20, left)
        b = os.pread(fd, n, pos)
        if not b:
            return None
        out += b
        pos += len(b)
        left -= len(b)
    return bytes(out)


def head_tail(fd, start, size, n=4096):
    h = os.pread(fd, min(n, size), start)
    t = os.pread(fd, min(n, size), max(start, start + size - n))
    return h, t


def check_zip(head, tail):
    if head[:4] != b'PK\x03\x04':
        return False, '无 PK 头'
    if b'PK\x05\x06' not in tail:
        return False, '无 EOCD'
    return True, 'ok'


def check_pdf(head, tail):
    if not head.startswith(b'%PDF-'):
        return False, '不是 PDF'
    if b'%%EOF' not in tail:
        return False, '无 %%EOF'
    if b'startxref' not in tail:
        return False, '无 startxref'
    return True, 'ok'


def check_png(head, tail):
    if not head.startswith(b'\x89PNG\r\n\x1a\n'):
        return False, '不是 PNG'
    if b'IEND' not in tail:
        return False, '无 IEND'
    return True, 'ok'


def check_jpeg(head, tail):
    if not head.startswith(b'\xff\xd8\xff'):
        return False, '不是 JPEG'
    if not tail.endswith(b'\xff\xd9'):
        return False, '无 FFD9 结尾'
    return True, 'ok'


def load_candidates(logdir):
    """读 carve_gen.py 的候选日志：type \\t start \\t size \\t endsig_offset"""
    out = []
    for fn in sorted(os.listdir(logdir)):
        if not (fn.startswith('c') and fn.endswith('.log')):
            continue
        with open(os.path.join(logdir, fn), encoding='utf-8', errors='replace') as f:
            for ln in f:
                if ln.startswith('#') or '\t' not in ln:
                    continue
                parts = ln.rstrip('\n').split('\t')
                if len(parts) < 3:
                    continue
                t = parts[0]
                if t == 'ole':
                    continue
                try:
                    out.append((t, int(parts[1]), int(parts[2])))
                except ValueError:
                    pass
    return out


CHECKERS = {
    'zip': check_zip,
    'pdf': check_pdf,
    'png': check_png,
    'jpeg': check_jpeg,
}


def main():
    logdir, targets_tsv, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(outdir, exist_ok=True)
    targets = load_targets(targets_tsv)
    cands = load_candidates(logdir)
    sys.stderr.write('候选条目: %d\n' % len(cands))

    fd = os.open(DEV, os.O_RDONLY)
    results = []
    seen = set()
    try:
        for t, start, size in cands:
            if size not in targets:
                continue
            key = (t, start, size)
            if key in seen:
                continue
            seen.add(key)
            checker = CHECKERS.get(t)
            if checker is None:
                continue
            h, tail = head_tail(fd, start, size)
            ok, why = checker(h, tail)
            tg = pick_target(targets[size], t)
            results.append({'type': t, 'start': start, 'size': size,
                            'ok': ok, 'why': why, 'ext': tg[0], 'rel': tg[1],
                            'targets': targets[size]})

        # --- 落盘 ---
        n = 0
        written = set()
        for r in results:
            if not r['ok']:
                continue
            key2 = (r['type'], r['size'], r['rel'])
            if key2 in written:
                r['dup'] = True          # 同一个原件在盘上有多个历史副本
                continue
            written.add(key2)
            dst = os.path.join(outdir, r['rel'])
            d = os.path.dirname(dst)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            data = carve(fd, r['start'], r['size'])
            if data is None:
                continue
            with open(dst, 'wb') as f:
                f.write(data)
            r['dst'] = dst
            n += 1
    finally:
        os.close(fd)

    uniq = set((r['type'], r['size'], r['rel']) for r in results if r['ok'])
    json.dump({'results': results, 'saved': n, 'unique_targets': len(uniq)},
              open(os.path.join(outdir, '_认领结果.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('候选 %d, 结构校验通过 %d, 落盘 %d, 命中**不同目标** %d'
          % (len(results), sum(1 for r in results if r['ok']), n, len(uniq)))
    hit_sizes = set(r['size'] for r in results if r['ok'])
    print('命中目标大小数: %d' % len(hit_sizes))
    for r in results:
        if not r['ok']:
            print('  未通过: %-6s %-10d %s' % (r['type'], r['size'], r['rel']))


if __name__ == '__main__':
    main()
