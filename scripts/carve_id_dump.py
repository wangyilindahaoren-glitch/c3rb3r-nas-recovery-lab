#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carve_id_dump.py —— 从雕刻索引里把每个命中地址的容器 **id 字段** 提出来。

容器头是定长 TLV：
  magic(4) | 12 "schema\\0" u32len(3) val(3) | 00 | 08 "enc\\0" 00 | 02 "id\\0" u32len val | ...
id 值就在固定位置附近，读 512 字节足够解析。

输出 TSV： id \\t 逻辑地址 \\t 物理地址 \\t 头部长 \\t 条目起始
"""
import os
import re
import struct
import subprocess
import sys

DEV = os.environ.get("C3R_DEV", "/dev/mapper/cachedev_0")
RE = re.compile(r"签名命中 逻辑地址 (\d+)")


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


def parse_header(buf):
    """返回 (id值字符串, 条目起始偏移)；解析不出来返回 (None, -1)"""
    if len(buf) < 16 or buf[:4] != b'\x78\x00\x00\x00':
        return None, -1
    pos = 4
    docid = None
    while pos < len(buf):
        tag = buf[pos]
        e = buf.find(b'\x00', pos + 1)
        if e < 0:
            break
        key = buf[pos + 1:e].decode('latin1', 'replace')
        p = e + 1
        if tag == 0x08:
            ln = buf[p]
            p += 1
        else:
            if p + 4 > len(buf):
                break
            ln = int.from_bytes(buf[p:p + 4], 'little')
            p += 4
        val = buf[p:p + ln]
        if key == 'id':
            docid = val.rstrip(b'\x00').decode('utf-8', 'replace')
        q = p + ln
        if q < len(buf) and buf[q] == 0:
            q += 1
        if key in ('schema', 'enc', 'id', 'ver') and docid is not None:
            pass
        pos = q
        if pos + 5 <= len(buf) and buf[pos + 4] in (0x05, 0x02) and (
                buf[pos + 5:pos + 8] == b'@C_' or buf[pos + 5:pos + 9] == b'text'):
            break
        if key == 'ver':
            break
    return docid, pos


def main():
    logs = sys.argv[1:]
    chunks = parse_chunks(dump_tree(DEV, 3))
    data = sorted([(c[0], c[1], c[3][0][1]) for c in chunks if "DATA" in c[2]])

    def log2phys(a):
        for ls, ln, off in data:
            if ls <= a < ls + ln:
                return off + (a - ls)
        return None

    addrs = []
    for lg in logs:
        if not os.path.exists(lg):
            continue
        for line in open(lg, "rb"):
            m = RE.search(line.decode("utf-8", "replace"))
            if m:
                addrs.append(int(m.group(1)))
    addrs = sorted(set(addrs))
    print("命中地址 %d 个（去重）" % len(addrs), file=sys.stderr)
    fd = os.open(DEV, os.O_RDONLY)
    for a in addrs:
        phys = log2phys(a)
        if phys is None:
            continue
        buf = os.pread(fd, 512, phys)
        docid, pos = parse_header(buf)
        if not docid:
            continue
        print("%s\t%d\t%d\t%d" % (docid, a, phys, pos))
    os.close(fd)


if __name__ == '__main__':
    main()
