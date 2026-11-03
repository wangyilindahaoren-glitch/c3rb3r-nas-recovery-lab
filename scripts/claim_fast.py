#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claim_fast.py —— 快速认领：直接在「命中地址 + 目标长度」处查容器尾段标记。

原理：雕刻命中的是容器文件头的位置（magic）。osheet 是单 extent 文件，
文件内容在盘上连续存放，所以文件末尾就在 head + orig_len 附近。
只要在 head+L-64 .. head+L+64 区间里找到尾段标记
  尾部段 = [u32 总长][0x08]["end\\0"][值]
且 u32 == 11（尾段自身长度），则该容器长度精确等于 L —— 也就是勒索标记里的原始长度。

每个候选只需读 128 字节，比逐个读整个容器快几个数量级。
命中后再用 osheet_strict 严格解析复核。

用法:
  python3 claim_fast.py --len 120669 29001 ... --label A B ... --log f1.log [f2.log ...]
                         [--out 目录] [--read 字节数=64]
"""
import argparse
import importlib.util
import json
import os
import re
import struct
import subprocess
import sys

DEV = os.environ.get("C3R_DEV", "/dev/mapper/cachedev_0")
RE = re.compile(r"签名命中 逻辑地址 (\d+)")
STRICT = os.environ.get("C3R_STRICT", "/tmp/c3r_carve/osheet_strict.py")


def run(cmd):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT).stdout.decode("utf-8", "replace")
    except OSError:
        return ""


def dump_tree(dev, tree):
    for cmd in (["btrfs", "inspect-internal", "dump-tree", "-t", str(tree), dev],
                ["btrfs-debug-tree", "-t", str(tree), dev]):
        out = run(cmd)
        if "item " in out:
            return out
    raise SystemExit("无法运行 dump-tree")


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


def log2phys_map():
    chunks = parse_chunks(dump_tree(DEV, 3))
    _seen = set()
    _uniq = []
    for c in chunks:
        if "DATA" not in c[2] or c[0] in _seen:
            continue
        _seen.add(c[0])
        _uniq.append((c[0], c[1], c[3][0][1]))
    data = sorted(_uniq, key=lambda c: c[2])
    return data


def log2phys(data, a):
    for ls, ln, off in data:
        if ls <= a < ls + ln:
            return off + (a - ls)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--len", dest="lengths", type=int, nargs="+", required=True)
    ap.add_argument("--label", dest="labels", nargs="+", default=None)
    ap.add_argument("--log", dest="logs", nargs="+", required=True)
    ap.add_argument("--out", default="/tmp/c3r_carve/claim")
    ap.add_argument("--read", type=int, default=64, help="尾段标记前后各读多少字节")
    args = ap.parse_args()

    lengths = list(args.lengths)
    labels = list(args.labels) if args.labels else [str(x) for x in lengths]
    while len(labels) < len(lengths):
        labels.append(str(lengths[len(labels)]))

    data = log2phys_map()
    fd = os.open(DEV, os.O_RDONLY)

    addrs = set()
    for lg in args.logs:
        if not os.path.exists(lg):
            continue
        with open(lg, "rb") as f:
            for line in f:
                m = RE.search(line.decode("utf-8", "replace"))
                if m:
                    addrs.add(int(m.group(1)))
    addrs = sorted(addrs)
    print("命中地址 %d 个（去重）" % len(addrs))

    os.makedirs(args.out, exist_ok=True)
    found = 0
    tried = 0
    for a in addrs:
        phys = log2phys(data, a)
        if phys is None:
            continue
        for L in lengths:
            # 尾段标记位于 head + L - 11 - 4 + ... ；直接扫 head+L-128 .. head+L+64
            start = a + L - 128
            if start < a:
                start = a
            span = 128 + args.read
            buf = os.pread(fd, span, phys + (start - a))
            if not buf:
                continue
            tried += 1
            i = buf.find(b"\x08end\x00")
            while i >= 4:
                tlen = struct.unpack_from('<I', buf, i - 4)[0]
                total = (start - a) + (i - 4) + tlen
                if total == L:
                    # 候选：读整个容器复核
                    whole = os.pread(fd, L, phys)
                    found += 1
                    label = labels[lengths.index(L)]
                    print("=" * 72)
                    print("★★ 命中 %s  长度 %d  逻辑 %d  物理 %d" % (label, L, a, phys))
                    path = os.path.join(args.out, "target_%s.osheet" % label)
                    with open(path, "wb") as f:
                        f.write(whole)
                    print("   已存 %s (%d 字节)" % (path, len(whole)))
                    try:
                        spec = importlib.util.spec_from_file_location("st", STRICT)
                        st = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(st)
                        header, entries, end = st.parse_strict(whole)
                        print("   严格解析 OK：条目 %d" % len(entries))
                        for _p, _X, name, payload in entries:
                            n = name[3:] if name.startswith("@C_") else name
                            if n == "text/index":
                                try:
                                    idx, _ = json.JSONDecoder().raw_decode(payload.decode("utf-8", "replace"))
                                    print("   工作表: %s" % [(k, v.get("title")) for k, v in idx.get("sheets", {}).items()])
                                    print("   顺序: %s" % idx.get("order"))
                                except Exception as e:
                                    print("   index 解析失败:", e)
                            if n.startswith("text/sh_"):
                                try:
                                    sh, _ = json.JSONDecoder().raw_decode(payload.decode("utf-8", "replace"))
                                except Exception:
                                    continue
                                cells = sh.get("cells", {})
                                print("   分片 %s rowCount=%s 有数据行=%d" % (n, sh.get("rowCount"), len(cells)))
                                for r in sorted(cells)[:2]:
                                    row = cells[r]
                                    print("      行%s: %s" % (r, [(c, row[c].get("v") if isinstance(row[c], dict) else row[c])
                                                                 for c in sorted(row)[:10]]))
                    except Exception as e:
                        print("   严格解析失败:", str(e)[:120])
                    break
                i = buf.find(b"\x08end\x00", i + 1)
    os.close(fd)
    print("候选检查 %d 次，精确命中 %d" % (tried, found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
