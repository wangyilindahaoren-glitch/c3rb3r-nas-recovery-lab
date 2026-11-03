#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carve_gen_hits.py —— 路线 D 扫描器 v2：把**所有**签名命中偏移都记下来，事后离线配对。

为什么要有 v2
-------------
`carve_gen.py` 的配对是**在线**做的：每遇到一个结尾签名，就拿最近若干个（≤16 个）
开头签名试长度。但：

  - `MAX_PENDING = 16`：盘上 PDF 一多，pending 队列会溢出，**较早的开头偏移被丢掉**，
    于是"某个 %PDF- 开头"再也没机会和它自己的 %%EOF 配对 → 该文件认领不到；
  - 结尾签名 `%%EOF` 的命中远多于 PDF 真实个数，很多是二进制数据里的巧合。

v2 的做法：**只管扫、只管记**，把每个开头/结尾签名的偏移都写进日志
（pdf 一个 PDF 大约 1 个开头 + 1 个结尾，全盘也就十万级，几 MB 日志）。
事后离线做集合运算：

    对每个开头偏移 X、每个目标长度 N：
        若 (X + N - 5) 在「结尾偏移集合」里 → 命中（%%EOF 正好是文件最后 5 字节）

这样就没有 pending 队列、没有"最近的配对"、也没有先来后到的问题，
而且能一次看到"每个目标长度各命中几处"。

用法
----
  # 扫描（只扫 pdf/png/jpeg 里的 pdf 段最常用）
  python3 carve_gen_hits.py scan <输出目录> --targets <targets.tsv> \
      --sigs pdf,pdf_end --workers 2

  # 离线配对
  python3 carve_gen_hits.py match <输出目录> --targets <targets.tsv>

输出：
  <输出目录>/starts_<sig>.tsv   每行 <偏移>
  <输出目录>/ends_<sig>.tsv     每行 <偏移>
  <输出目录>/matched.tsv        每行 <type> \t <start> \t <size> \t <end_off>
  <输出目录>/match_summary.txt  每个目标长度的命中统计

只读块设备，不写卷。
"""
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time

DEV = os.environ.get('C3R_DEV', '/dev/mapper/cachedev_0')
CHUNK_IO = 64 << 20
OVERLAP = 64

SIGS = [
    ('pdf',      b'%PDF-',                       0),
    ('pdf_end',  b'%%EOF',                       5),
    ('pdf_xref', b'startxref',                   9),
    ('png',      b'\x89PNG\r\n\x1a\n',            0),
    ('png_end',  b'\x00\x00\x00\x00IEND\xaeB`\x82', 12),
    ('jpeg',     b'\xff\xd8\xff',                0),
    ('jpeg_end', b'\xff\xd9',                    2),
    ('eocd',     b'PK\x05\x06',                  0),
]
END_TAIL = {'pdf_end': 5, 'png_end': 12, 'jpeg_end': 2}
SIG_TYPE = {'pdf': 'pdf', 'png': 'png', 'jpeg': 'jpeg'}
END_TYPE = {'pdf_end': 'pdf', 'png_end': 'png', 'jpeg_end': 'jpeg'}


def run(cmd):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT).stdout.decode('utf-8', 'replace')
    except OSError:
        return ''


def dump_tree(dev, tree):
    for cmd in (['btrfs', 'inspect-internal', 'dump-tree', '-t', str(tree), dev],
                ['btrfs-debug-tree', '-t', str(tree), dev]):
        out = run(cmd)
        if 'item ' in out:
            return out
    return ''


def parse_chunks(text):
    chunks = []
    for blk in re.split(r'\n\s*item \d+ key ', text):
        m = re.search(r'CHUNK_ITEM (\d+)\)', blk)
        if not m:
            continue
        ln = re.search(r'chunk length (\d+)', blk)
        ty = re.search(r'\n\s*type (\S+)', blk)
        if not (ln and ty):
            continue
        stripes = [(int(a), int(b)) for a, b in
                   re.findall(r'stripe \d+ devid (\d+) offset (\d+)', blk)]
        if stripes:
            chunks.append((int(m.group(1)), int(ln.group(1)), ty.group(1), stripes))
    return chunks


def data_chunks(dev):
    out = []
    seen = set()
    for cid, ln, ty, stripes in parse_chunks(dump_tree(dev, 3)):
        if ty != 'DATA' or cid in seen:
            continue
        seen.add(cid)
        for _devid, off in stripes:
            out.append((off, ln))
    out.sort()
    return out


def load_sizes(path):
    d = {}
    with open(path, encoding='utf-8') as f:
        for ln in f:
            ln = ln.rstrip('\n')
            if not ln:
                continue
            sz, ext, rel = ln.split('\t')
            d.setdefault(int(sz), []).append((ext, rel))
    return d


def do_scan(outdir, worker, workers, targets_tsv, sig_names, skip_file=None):
    os.makedirs(outdir, exist_ok=True)
    targets = load_sizes(targets_tsv) if targets_tsv else {}
    sigs = [(n, p) for n, p, _t in SIGS if (sig_names is None or n in sig_names)]

    chunks = data_chunks(DEV)
    pairs = list(enumerate(chunks))
    mine = pairs[worker::workers]
    if skip_file:
        # 断点续扫：跳过已经扫过的全局 chunk 序号（每行一个整数）
        try:
            skip = set(int(x) for x in open(skip_file) if x.strip())
        except OSError:
            skip = set()
        before = len(mine)
        mine = [(g, c) for (g, c) in mine if g not in skip]
        print('[skip] 全局 chunk %d 个，跳过已扫 %d 个，本路剩 %d 个'
              % (len(chunks), before - len(mine), len(mine)))

    outs = {}
    for name, _p in sigs:
        fn = os.path.join(outdir, '%s_%s_%d.tsv' % (
            'starts' if name in SIG_TYPE else 'ends', name, worker))
        outs[name] = open(fn, 'a', buffering=1)
    logf = open(os.path.join(outdir, 'scan_%d.log' % worker), 'a', buffering=1)
    logf.write('# worker %d/%d DATA chunks=%d mine=%d sigs=%s\n'
               % (worker, workers, len(chunks), len(mine),
                  ','.join(n for n, _ in sigs)))
    logf.flush()

    fd = os.open(DEV, os.O_RDONLY)
    t0 = time.time()
    done = 0
    hits = 0

    def producer():
        try:
            for (g, (start, length)) in mine:
                pos = 0
                while pos < length:
                    n = min(CHUNK_IO, length - pos)
                    want = n + OVERLAP
                    buf = os.pread(fd, want, start + pos)
                    if not buf:
                        break
                    q.put((start + pos, buf))
                    if len(buf) < want:
                        break
                    pos += n
        except Exception:
            pass
        q.put(None)

    q = queue.Queue(maxsize=3)
    th = threading.Thread(target=producer, daemon=True)
    th.start()
    try:
        ci = -1
        chunk_starts = [c[1][0] for c in mine]
        import bisect
        while True:
            item = q.get()
            if item is None:
                break
            base, buf = item
            ci += 1
            n = len(buf)
            for name, pat in sigs:
                i = buf.find(pat)
                while i >= 0:
                    abs_off = base + i
                    hits += 1
                    outs[name].write('%d\n' % abs_off)
                    if name == 'eocd':
                        h = os.pread(fd, 22, abs_off)
                        if len(h) == 22 and h[:4] == b'PK\x05\x06':
                            cd_size, cd_off, comment = struct.unpack('<IIH', h[12:22])
                            size = cd_off + cd_size + 22 + comment
                            if size in targets:
                                outs['eocd'].write('%d %d\n' % (abs_off + 22 + comment - size,
                                                                size))
                    i = buf.find(pat, i + 1)
            done += n
            if ci % 64 == 0:
                el = time.time() - t0
                ck = bisect.bisect_right(chunk_starts, base)
                logf.write('# progress chunk %d/%d %.0f MB/s hits=%d\n'
                           % (ck, len(mine), done / 1048576.0 / max(el, 1e-9), hits))
                logf.flush()
    finally:
        os.close(fd)
        for f in outs.values():
            f.close()
        logf.close()
    print('[w%d] %.2f GB, %d hits, %.0f MB/s' %
          (worker, done / 1073741824.0, hits,
           done / 1048576.0 / max(time.time() - t0, 1e-9)))


def do_match(outdir, targets_tsv, window=96):
    """离线配对。

    规则（按强度从高到低）：
      A. `%%EOF` 正好落在 X+N-5（文件最后 5 字节）—— 精确；
      B. 文件末尾 window 字节内出现 `%%EOF` 或 `startxref` —— 近似
         （尾部被覆盖了几十字节时也能命中）。

    用 bisect 在有序的结尾偏移数组上做区间查询，17M 次查询也就几十秒。
    """
    import bisect
    targets = load_sizes(targets_tsv)
    ends = {}
    starts = {}
    for fn in sorted(os.listdir(outdir)):
        if not fn.endswith('.tsv'):
            continue
        if fn.startswith('ends_'):
            kind = fn[5:].rsplit('_', 1)[0]
        elif fn.startswith('starts_'):
            kind = fn[7:].rsplit('_', 1)[0]
        else:
            continue
        s = set()
        with open(os.path.join(outdir, fn), encoding='utf-8') as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    s.add(int(ln.split()[0]))
                except ValueError:
                    continue
        if kind.endswith('_end') or kind.endswith('_xref'):
            ends.setdefault(kind, set()).update(s)
        else:
            # ⚠️ 必须 union，不能赋值：同一 kind 会有多个 worker 的文件
            #    （starts_pdf_0.tsv / starts_pdf_1.tsv），赋值会把前一个覆盖掉。
            #    2026-11-02 实测：worker1 的文件是空的（命中随 SIGTERM 全丢），
            #    赋值后 starts['pdf'] 变成空集 → 配对命中 0 条，看着像"没扫到"。
            starts.setdefault(kind, set()).update(s)
        print('%s %s: %d 个唯一偏移' % (kind, fn, len(s)))

    # pdf 的"结尾标志"集合：%%EOF 和 startxref 合并
    tail_sets = {}
    for kind in ('pdf', 'png', 'jpeg'):
        s = set()
        for k, v in ends.items():
            if k.startswith(kind):
                s |= v
        if s:
            tail_sets[kind] = sorted(s)

    sizes = sorted(targets)
    rows = []
    for kind, sset in starts.items():
        arr = tail_sets.get(kind)
        if not arr:
            continue
        for x in sset:
            for n in sizes:
                # A: %%EOF 精确落在末尾
                if (x + n - 5) in ends.get(kind + '_end', set()):
                    rows.append((kind, x, n, x + n - 5, 'exact'))
                    continue
                # B: 末尾窗口内有结尾标志
                lo = x + n - window
                i = bisect.bisect_left(arr, lo)
                if i < len(arr) and arr[i] <= x + n:
                    rows.append((kind, x, n, arr[i], 'window'))
    with open(os.path.join(outdir, 'matched.tsv'), 'w', encoding='utf-8') as f:
        for kind, x, n, e, how in sorted(rows, key=lambda r: (r[2], r[1])):
            f.write('%s\t%d\t%d\t%d\t%s\n' % (kind, x, n, e, how))

    hit_sizes = {}
    for kind, x, n, e, how in rows:
        hit_sizes.setdefault(n, []).append((kind, x, e, how))
    with open(os.path.join(outdir, 'match_summary.txt'), 'w', encoding='utf-8') as f:
        f.write('命中条目 %d，涉及 %d 个目标长度\n' % (len(rows), len(hit_sizes)))
        for n in sorted(hit_sizes):
            lst = hit_sizes[n]
            ex = sum(1 for x in lst if x[3] == 'exact')
            f.write('长度 %-10d 命中 %-5d 处(精确 %d)  目标 %s\n'
                    % (n, len(lst), ex, targets.get(n)))
    print('命中条目 %d，涉及 %d 个目标长度' % (len(rows), len(hit_sizes)))

    # ---- 新旧规则对比：老规则（carve_gen.py）每个结尾只试最近 16 个开头 ----
    old_hits = 0
    old_sizes = set()
    for kind, sset in starts.items():
        earr = sorted(ends.get(kind + '_end', set()))
        if not earr or not sset:
            continue
        sarr = sorted(sset)
        pend = []          # 模拟 MAX_PENDING=16
        si = 0
        for e in earr:
            while si < len(sarr) and sarr[si] < e:
                pend.append(sarr[si])
                if len(pend) > 16:
                    del pend[0]
                si += 1
            for x in pend:
                n = e + 5 - x
                if n in targets:
                    old_hits += 1
                    old_sizes.add(n)
                    break
    print('新规则(离线集合配对)命中 %d 条 / %d 个目标长度' % (len(rows), len(hit_sizes)))
    print('老规则(pending<=16)  命中 %d 条 / %d 个目标长度' % (old_hits, len(old_sizes)))
    new_sizes = set(hit_sizes)
    print('多出来的目标长度: %d 个 %s'
          % (len(new_sizes - old_sizes), sorted(new_sizes - old_sizes)[:20]))
    print('-> %s' % os.path.join(outdir, 'matched.tsv'))


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    mode = sys.argv[1]
    if mode == 'scan':
        outdir = sys.argv[2]
        worker, workers = 0, 1
        targets_tsv = None
        sig_names = None
        skip_file = None
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == '--worker':
                worker = int(sys.argv[i + 1]); i += 2
            elif sys.argv[i] == '--workers':
                workers = int(sys.argv[i + 1]); i += 2
            elif sys.argv[i] == '--targets':
                targets_tsv = sys.argv[i + 1]; i += 2
            elif sys.argv[i] == '--sigs':
                sig_names = sys.argv[i + 1].split(','); i += 2
            elif sys.argv[i] == '--skip-file':
                skip_file = sys.argv[i + 1]; i += 2
            else:
                i += 1
        do_scan(outdir, worker, workers, targets_tsv, sig_names, skip_file)
    elif mode == 'match':
        do_match(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
