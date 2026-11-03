#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carve_len_dump.py —— 把雕刻索引里所有容器的**真实长度**算出来，用于长度匹配。

输入：ids.tsv（id \\t 逻辑地址 \\t 物理地址 \\t 头部长）
输出：id \\t 长度 \\t 严格解析 \\t 工作表名 \\t 首行摘要

自适应读块（64KB 起，翻倍），避免大容器浪费带宽。
"""
import importlib.util
import json
import os
import struct
import sys

DEV = os.environ.get("C3R_DEV", "/dev/mapper/cachedev_0")
STRICT = os.environ.get("C3R_STRICT", "/tmp/osheet_strict.py")
IDS = sys.argv[1] if len(sys.argv) > 1 else "/tmp/c3r_carve/ids.tsv"


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
    st = load_strict()
    rows = []
    for line in open(IDS, encoding="utf-8", errors="replace"):
        p = line.rstrip("\n").split("\t")
        if len(p) >= 3:
            rows.append((p[0], int(p[1]), int(p[2])))
    print("容器 %d 个" % len(rows), file=sys.stderr)
    fd = os.open(DEV, os.O_RDONLY)
    for i, (docid, la, phys) in enumerate(rows):
        data_c, total = read_container(fd, phys)
        if data_c is None:
            print("%s\t%d\t-\t读取失败\t%d" % (docid, total, la))
            continue
        ok = "自洽"
        titles = ""
        try:
            header, entries, end = st.parse_strict(data_c)
            tl = []
            for _pos, _X, name, payload in entries:
                n = name[3:] if name.startswith("@C_") else name
                if n == "text/index":
                    try:
                        idx, _ = json.JSONDecoder().raw_decode(payload.decode("utf-8", "replace"))
                        tl = ["%s:%s" % (k, v.get("title")) for k, v in idx.get("sheets", {}).items()]
                    except Exception:
                        pass
            titles = ",".join(tl)
        except Exception as e:
            ok = "解析失败"
            titles = str(e)[:60]
        print("%s\t%d\t%s\t%s\t%d" % (docid, total, ok, titles, la))
        if (i + 1) % 500 == 0:
            print("... %d/%d" % (i + 1, len(rows)), file=sys.stderr)
            sys.stderr.flush()
    os.close(fd)


if __name__ == '__main__':
    main()
