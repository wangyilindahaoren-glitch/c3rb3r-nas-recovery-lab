#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claim_by_len.py —— 按 len.tsv 里的长度，把命中的容器批量读出来存盘。

用法：python3 claim_by_len.py <len.tsv> <目标长度表(txt,一行一个长度)> <输出目录>
"""
import importlib.util
import os
import re
import struct
import subprocess
import sys

DEV = os.environ.get("C3R_DEV", "/dev/mapper/cachedev_0")
STRICT = os.environ.get("C3R_STRICT", "/tmp/osheet_strict.py")


def _run(cmd):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT).stdout.decode("utf-8", "replace")
    except OSError:
        return ""


def _dump_tree(dev, tree):
    for cmd in (["btrfs", "inspect-internal", "dump-tree", "-t", str(tree), dev],
                ["btrfs-debug-tree", "-t", str(tree), dev]):
        out = _run(cmd)
        if "item " in out:
            return out
    raise SystemExit("无法运行 dump-tree")


def _chunks(dev):
    text = _dump_tree(dev, 3)
    out = []
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
            out.append((int(m.group(1)), int(ln.group(1)), ty.group(1), stripes))
    return sorted([(c[0], c[1], c[3][0][1]) for c in out if "DATA" in c[2]])


def log2phys_factory(data):
    def log2phys(a):
        for ls, ln, off in data:
            if ls <= a < ls + ln:
                return off + (a - ls)
        return None
    return log2phys


def load_strict():
    spec = importlib.util.spec_from_file_location("st", STRICT)
    st = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(st)
    return st


def read_container(fd, phys, cap=64 << 20):
    buf = b""
    off = 0
    chunk = 64 << 10
    while off < cap:
        part = os.pread(fd, chunk, phys + off)
        if not part:
            break
        buf += part
        i = buf.find(b"\x08end\x00")
        if i >= 4:
            tlen = struct.unpack_from('<I', buf, i - 4)[0]
            total = (i - 4) + tlen
            if 0 < total <= len(buf):
                return buf[:total], total
        off += len(part)
        chunk = min(chunk * 2, 4 << 20)
    return None, 0


def main():
    lentab, targetfile, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    targets = set()
    for line in open(targetfile, encoding='utf-8', errors='replace'):
        line = line.strip()
        if line:
            targets.add(int(line.split('\t')[0]))
    os.makedirs(outdir, exist_ok=True)
    st = load_strict()
    log2phys = log2phys_factory(_chunks(DEV))
    fd = os.open(DEV, os.O_RDONLY)
    n = 0
    for line in open(lentab, encoding='utf-8', errors='replace'):
        p = line.rstrip('\n').split('\t')
        if len(p) < 5 or not p[1].isdigit():
            continue
        total = int(p[1])
        if total not in targets:
            continue
        docid = p[0]
        la = int(p[4])
        phys = log2phys(la)
        if phys is None:
            continue
        data_c, t2 = read_container(fd, phys)
        if data_c is None or t2 != total:
            continue
        ok = "自洽"
        try:
            st.parse_strict(data_c)
        except Exception as e:
            ok = "解析失败:" + str(e)[:40]
        out = os.path.join(outdir, "len%d_%s.osheet" % (total, docid))
        with open(out, "wb") as f:
            f.write(data_c)
        n += 1
        print("认领 len=%-9d %-42s %s  -> %s" % (total, docid, ok, os.path.basename(out)))
    os.close(fd)
    print("共认领 %d 个" % n)


if __name__ == '__main__':
    main()
