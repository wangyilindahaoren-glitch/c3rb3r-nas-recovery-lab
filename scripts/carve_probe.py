#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carve_probe.py —— 把雕刻索引里某个 id 的所有容器读出来，打印长度/工作表/首行。

用法：python3 carve_probe.py --id <uid>_<docid>.sh --logs logs/w*.log
      python3 carve_probe.py --id X --expect 4340 --save /tmp/out   # 命中就存盘
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
STRICT = os.environ.get("C3R_STRICT", "/tmp/osheet_strict.py")


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


def load_strict():
    spec = importlib.util.spec_from_file_location("st", STRICT)
    st = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(st)
    return st


def read_container(fd, phys, cap=64 << 20):
    buf = b""
    off = 0
    while off < cap:
        part = os.pread(fd, 2 << 20, phys + off)
        if not part:
            break
        buf += part
        i = buf.find(b"\x08end\x00")
        if i >= 4:
            tlen = struct.unpack_from('<I', buf, i - 4)[0]
            total = (i - 4) + tlen
            if 0 < total <= len(buf):
                return buf[:total], total
        off += 2 << 20
    return None, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--logs", nargs="+", required=True)
    ap.add_argument("--expect", type=int, default=0)
    ap.add_argument("--save", default="")
    ap.add_argument("--max", type=int, default=0)
    a = ap.parse_args()

    chunks = parse_chunks(dump_tree(DEV, 3))
    data = sorted([(c[0], c[1], c[3][0][1]) for c in chunks if "DATA" in c[2]])

    def log2phys(x):
        for ls, ln, off in data:
            if ls <= x < ls + ln:
                return off + (x - ls)
        return None

    addrs = []
    for lg in a.logs:
        if not os.path.exists(lg):
            continue
        for line in open(lg, "rb"):
            m = RE.search(line.decode("utf-8", "replace"))
            if m:
                addrs.append(int(m.group(1)))
    addrs = sorted(set(addrs))
    st = load_strict()
    fd = os.open(DEV, os.O_RDONLY)
    n = 0
    seen = set()
    for x in addrs:
        phys = log2phys(x)
        if phys is None or phys in seen:
            continue
        seen.add(phys)
        buf = os.pread(fd, 200, phys)
        if b'\x02id\x00' not in buf[:160]:
            continue
        i = buf.find(b'\x02id\x00')
        ln = int.from_bytes(buf[i + 4:i + 8], 'little')
        did = buf[i + 8:i + 8 + ln].rstrip(b'\x00').decode('utf-8', 'replace')
        if did != a.id:
            continue
        data_c, total = read_container(fd, phys)
        if data_c is None:
            continue
        n += 1
        try:
            header, entries, end = st.parse_strict(data_c)
            ok = "自洽"
        except Exception as e:
            entries = None
            ok = "解析失败:" + str(e)[:40]
        mark = ''
        if a.expect and total == a.expect:
            mark = '  ★★★ 命中目标长度'
            if a.save:
                with open(os.path.join(a.save, "target_%s.osheet" % a.id), "wb") as f:
                    f.write(data_c)
                mark += ' 已存'
        print("逻辑 %-14d 物理 %-14d 长度 %-9d %s%s" % (x, phys, total, ok, mark))
        if entries:
            for _pos, _X, name, payload in entries:
                n2 = name[3:] if name.startswith("@C_") else name
                if n2 == "text/index":
                    try:
                        idx, _ = json.JSONDecoder().raw_decode(payload.decode("utf-8", "replace"))
                        print("    工作表: %s" % ([(k, v.get("title")) for k, v in idx.get("sheets", {}).items()],))
                    except Exception:
                        pass
                if n2.startswith("text/sh_"):
                    try:
                        sh, _ = json.JSONDecoder().raw_decode(payload.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    cells = sh.get("cells", {})
                    print("    %s rowCount=%s 有数据行=%d" % (n2, sh.get("rowCount"), len(cells)))
                    for r in sorted(cells, key=lambda x: int(x))[:2]:
                        row = cells[r]
                        print("       行%s: %s" % (r, [(c, row[c].get("v") if isinstance(row[c], dict) else row[c])
                                                    for c in sorted(row, key=lambda x: int(x))[:8]]))
        if a.max and n >= a.max:
            break
    os.close(fd)
    print("共 %d 个该 id 的容器" % n)


if __name__ == '__main__':
    main()
