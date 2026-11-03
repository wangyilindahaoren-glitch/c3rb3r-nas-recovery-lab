#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carve_gen.py —— 通用文件签名扫描 + 边扫边认领（路线 D）。

在 Btrfs DATA chunk 上按文件头/尾魔数扫描，把**已被释放的旧 extent**
（勒索前原件）捞回来。用于 mode=0x24 整文件加密、文件树里已无旧 extent 引用的情况。

做法（边扫边匹配，不产生巨型日志）：
  - zip 族：命中 EOCD(`PK\\x05\\x06`) 时，22 字节记录里就有 cd_offset/cd_size，
            直接算出文件长度；长度命中目标清单就记一条候选。
  - pdf/png/jpeg：记住最近几个「开头签名」偏移，遇到「结尾签名」时用
            (结尾 + 尾长 - 开头) 算长度；命中目标清单就记一条候选。
  - OLE2(xls/doc)：只记偏移，不做长度判定（结构复杂，价值低）。

用法：
  python3 carve_gen.py <日志目录> --worker N --workers M --targets <tsv>

只读：只 open(O_RDONLY) 读块设备，不写卷。
候选日志每行：<type>\\t<start>\\t<size>\\t<end_sig_offset>
"""
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time

DEV = os.environ.get("C3R_DEV", "/dev/mapper/cachedev_0")
CHUNK_IO = 64 << 20
OVERLAP = 64
MAX_PENDING = 16         # 每种类型最多同时记几个开头偏移

SIGS = [
    ('eocd',     b'PK\x05\x06',                  0),    # zip 结尾记录（自带长度）
    ('png_end',  b'\x00\x00\x00\x00IEND\xaeB`\x82', 12),  # png 结尾
    ('jpeg_end', b'\xff\xd9',                    2),   # jpeg 结尾
    ('pdf_end',  b'%%EOF',                       5),   # pdf 结尾
    ('png',      b'\x89PNG\r\n\x1a\n',            0),   # png 开头
    ('jpeg',     b'\xff\xd8\xff',                0),   # jpeg 开头
    ('pdf',      b'%PDF-',                       0),   # pdf 开头
    ('ole',      b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', 0),
]
END_TAIL = {'png_end': 12, 'jpeg_end': 2, 'pdf_end': 5}
# 开头签名 -> 类型（⚠️ 必须是**开头**签名的名字，不是结尾签名的名字；
# 2026-10-31 踩过这个坑：两个字典用了同样的 key，导致分支判断写反，
# pdf/png/jpeg 一直没能配对，扫了 0.95TB 只捞到 zip 候选）
SIG_TYPE = {'png': 'png', 'jpeg': 'jpeg', 'pdf': 'pdf'}
# 结尾签名 -> 类型
END_TYPE = {'png_end': 'png', 'jpeg_end': 'jpeg', 'pdf_end': 'pdf'}


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
        stripes = [(int(a), int(b)) for a, b in re.findall(r'stripe \d+ devid (\d+) offset (\d+)', blk)]
        if stripes:
            chunks.append((int(m.group(1)), int(ln.group(1)), ty.group(1), stripes))
    return chunks


def data_chunks(dev):
    """返回 [(phys_start, phys_len)]，只取 DATA 类型 chunk

    只 dump tree 3（chunk 树）。tree 1/2 在 14TB 卷上极慢，千万别全 dump。
    dump-tree 会把 chunk 列表输出两遍，按 chunk id 去重。
    """
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
    """返回 {size: [(ext, rel), ...]}"""
    d = {}
    with open(path, encoding='utf-8') as f:
        for ln in f:
            ln = ln.rstrip('\n')
            if not ln:
                continue
            sz, ext, rel = ln.split('\t')
            d.setdefault(int(sz), []).append((ext, rel))
    return d


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    logdir = args[0]
    worker, workers = 0, 1
    targets_tsv = None
    resume_offset = 0
    sig_names = None
    i = 1
    while i < len(args):
        if args[i] == '--worker':
            worker = int(args[i + 1]); i += 2
        elif args[i] == '--workers':
            workers = int(args[i + 1]); i += 2
        elif args[i] == '--targets':
            targets_tsv = args[i + 1]; i += 2
        elif args[i] == '--resume-offset':
            resume_offset = int(args[i + 1]); i += 2
        elif args[i] == '--sigs':
            sig_names = args[i + 1].split(','); i += 2
        else:
            i += 1

    os.makedirs(logdir, exist_ok=True)
    logf = open(os.path.join(logdir, 'c%d.log' % worker), 'a', buffering=1 << 20)
    targets = load_sizes(targets_tsv) if targets_tsv else {}

    chunks = data_chunks(DEV)
    mine = chunks[worker::workers]
    sigs = [(n, p, t) for n, p, t in SIGS if (sig_names is None or n in sig_names)]
    if resume_offset:
        # 断点续扫：跳过物理起点在 resume_offset 之前的 chunk（之前已由别的 worker 扫过）
        skipped = 0
        keep = []
        for c in mine:
            if c[0] < resume_offset:
                skipped += 1
            else:
                keep.append(c)
        logf.write('# resume from phys %d: 跳过 %d 个已扫 chunk\n'
                   % (resume_offset, skipped))
        mine = keep
    logf.write('# worker %d/%d  DATA chunks=%d  mine=%d  targets=%d sizes  sigs=%s\n'
               % (worker, workers, len(chunks), len(mine), len(targets),
                  ','.join(n for n, _, _ in sigs)))
    logf.flush()

    # 每种类型的待配对开头偏移（按新到旧）
    pending = {k: [] for k in SIG_TYPE.values()}
    noted = set()

    fd = os.open(DEV, os.O_RDONLY)
    t0 = time.time()
    done = 0
    hits = 0
    cands = 0

    def note(t, start, size, endsig_off):
        nonlocal cands
        if size not in targets:
            return
        key = (t, start, size)
        if key in noted:
            return
        noted.add(key)
        cands += 1
        logf.write('%s\t%d\t%d\t%d\n' % (t, start, size, endsig_off))

    # 预取线程：读取和扫描并行，把磁盘顺序读跑满
    q = queue.Queue(maxsize=3)

    def producer():
        try:
            for (start, length) in mine:
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

    th = threading.Thread(target=producer, daemon=True)
    th.start()

    try:
        ci = -1
        chunk_starts = [c[0] for c in mine]
        while True:
            item = q.get()
            if item is None:
                break
            base, buf = item
            ci += 1
            n = len(buf)
            # 一次遍历找出所有签名的位置
            events = []
            for name, pat, _t in sigs:
                i = buf.find(pat)
                while i >= 0:
                    events.append((i, name))
                    i = buf.find(pat, i + 1)
            events.sort()
            for off, name in events:
                abs_off = base + off
                hits += 1
                if name == 'eocd':
                    h = os.pread(fd, 22, abs_off)
                    if len(h) < 22 or h[:4] != b'PK\x05\x06':
                        continue
                    cd_size, cd_off, comment = struct.unpack('<IIH', h[12:22])
                    size = cd_off + cd_size + 22 + comment
                    note('zip', abs_off + 22 + comment - size, size, abs_off)
                elif name in SIG_TYPE:
                    # 开头签名：记下偏移，等结尾签名来配对
                    st = SIG_TYPE[name]
                    pl = pending[st]
                    pl.append(abs_off)
                    if len(pl) > MAX_PENDING:
                        del pl[0]
                elif name in END_TAIL:
                    # 结尾签名：把待配对的开头**全部**试一遍
                    # （PDF 里可能有增量更新的中间 %%EOF，或嵌套 PDF，
                    #   只试最近一个会漏；全试一遍靠目标长度集合兜底）
                    st = END_TYPE[name]
                    pl = pending[st]
                    if not pl:
                        continue
                    tail = END_TAIL[name]
                    hit = False
                    for k in range(len(pl) - 1, -1, -1):
                        size = abs_off + tail - pl[k]
                        if size in targets:
                            note(st, pl[k], size, abs_off)
                            hit = True
                    if hit:
                        pending[st] = []
                elif name == 'ole':
                    logf.write('ole\t%d\t0\t0\n' % abs_off)
            done += n
            if ci % 64 == 0:
                el = time.time() - t0
                # 用物理偏移二分出当前 chunk 序号
                import bisect
                ck = bisect.bisect_right(chunk_starts, base)
                logf.write('# progress chunk %d/%d  %.0f MB/s  hits=%d cands=%d\n'
                           % (ck, len(mine), done / 1048576.0 / max(el, 1e-9), hits, cands))
                logf.flush()
    finally:
        os.close(fd)
        logf.close()
    print('[w%d] done %.2f GB, %d hits, %d cands, %.0f MB/s'
          % (worker, done / 1073741824.0, hits, cands,
             done / 1048576.0 / max(time.time() - t0, 1e-9)))


if __name__ == '__main__':
    main()
