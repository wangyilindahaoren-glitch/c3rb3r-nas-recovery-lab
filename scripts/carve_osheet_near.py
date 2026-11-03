#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""near_check.py —— 对指定逻辑地址逐个读出容器：长度、工作表名、表头行"""
import os, re, sys, json, struct, subprocess, importlib.util

DEV = os.environ.get("C3R_DEV", "/dev/mapper/cachedev_0")
spec = importlib.util.spec_from_file_location("st", "/tmp/osheet_strict.py")
st = importlib.util.module_from_spec(spec)
spec.loader.exec_module(st)


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


chunks = parse_chunks(dump_tree(DEV, 3))
data = sorted([(c[0], c[1], c[3][0][1]) for c in chunks if "DATA" in c[2]])


def log2phys(a):
    for ls, ln, off in data:
        if ls <= a < ls + ln:
            return off + (a - ls)
    return None


addrs = [int(x) for x in sys.argv[1:]]
fd = os.open(DEV, os.O_RDONLY)
for a in addrs:
    phys = log2phys(a)
    buf = b""
    total = 0
    off = 0
    while off < (16 << 20):
        part = os.pread(fd, 2 << 20, phys + off)
        if not part:
            break
        buf += part
        i = buf.find(b"\x08end\x00")
        if i >= 4:
            tlen = struct.unpack_from('<I', buf, i - 4)[0]
            total = (i - 4) + tlen
            if 0 < total <= len(buf):
                break
        off += 2 << 20
    print("=" * 72)
    print("逻辑 %d 物理 %d  容器长度 %d" % (a, phys, total))
    if not total:
        print("  未找到尾部")
        continue
    d = buf[:total]
    try:
        header, entries, end = st.parse_strict(d)
        print("  严格解析 OK  条目 %d" % len(entries))
    except Exception as e:
        print("  严格解析失败:", e)
        continue
    for _pos, _X, name, payload in entries:
        n = name[3:] if name.startswith("@C_") else name
        if n == "text/index":
            try:
                idx, _ = json.JSONDecoder().raw_decode(payload.decode("utf-8", "replace"))
                print("  工作表: %s" % [(k, v.get("title")) for k, v in idx.get("sheets", {}).items()])
            except Exception as e:
                print("  index 失败:", e)
        if n.startswith("text/sh_"):
            try:
                sh, _ = json.JSONDecoder().raw_decode(payload.decode("utf-8", "replace"))
            except Exception:
                continue
            cells = sh.get("cells", {})
            print("  分片 %s rowCount=%s 有数据行=%d" % (n, sh.get("rowCount"), len(cells)))
            for r in sorted(cells)[:3]:
                row = cells[r]
                vals = [(c, row[c].get("v") if isinstance(row[c], dict) else row[c]) for c in sorted(row)[:10]]
                print("     行%s: %s" % (r, vals))
os.close(fd)
