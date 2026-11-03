#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""office_report.py —— 汇总 Synology Drive office 目录里每个文档的可恢复信息。

输出 TSV: uid  id  重建长度  sh_1大小  工作表名(来自 text/index)  各分片大小
"""
import base64
import json
import os
import sys

OFF = "/volume1/@SynoDrive/office"


def b64n(fn):
    out = []
    i = 0
    while i < len(fn):
        if fn[i] == "{":
            j = fn.index("}", i)
            out.append(chr(int(fn[i + 1:j])))
            i = j + 1
        else:
            out.append(fn[i])
            i += 1
    s = "".join(out)
    try:
        return base64.b64decode(s).decode("utf-8", "replace")
    except Exception:
        return s


def rebuild_len(office_dir):
    """按 .osheet 文法重建容器并返回长度（尾随字节缺失，故略小于原件）。"""
    import struct
    docid = os.path.basename(office_dir.rstrip("/"))
    ref = os.path.join(office_dir, "version", ".git", "refs", "heads", "master")
    ver = open(ref).read().strip()
    td = os.path.join(office_dir, "version", "text")
    ents = []
    for fn in sorted(os.listdir(td)):
        p = os.path.join(td, fn)
        if not os.path.isfile(p):
            continue
        nm = b64n(fn)
        if nm == "read-me3.txt":
            continue
        ents.append((nm, open(p, "rb").read()))
    if not any(n == "metatext" for n, _ in ents):
        ents.append(("metatext", b'{"ntype":37}'))
    total = 4
    total += 1 + 8 + 4 + 2 + 2          # schema(2字节值)+2个分隔
    total += 1 + 5 + 1 + 1              # enc
    total += 1 + 4 + 4 + len(docid) + 1  # id
    total += 1 + 5 + 4 + len(ver) + 1   # ver
    for name, data in ents:
        total += 4 + 1 + len(name) + 1 + 4 + len(data) + 1
    total += 4 + 4 + 3 + 2              # 尾部段
    return total, docid, ver


def main():
    print("uid\tid\t重建长度\tsh_1\t工作表\t分片")
    for name in sorted(os.listdir(OFF)):
        d = os.path.join(OFF, name)
        if not os.path.isdir(d):
            continue
        uid = name.split("_")[0]
        if not uid.isdigit():
            continue
        td = os.path.join(d, "version", "text")
        if not os.path.isdir(td):
            continue
        idx = None
        sh = {}
        for fn in os.listdir(td):
            nm = b64n(fn)
            p = os.path.join(td, fn)
            if not os.path.isfile(p):
                continue
            if nm == "index":
                try:
                    idx = json.JSONDecoder().raw_decode(
                        open(p, "rb").read().decode("utf-8", "replace"))[0]
                except Exception:
                    idx = None
            elif nm.startswith("sh_"):
                sh[nm] = os.path.getsize(p)
        titles = ""
        if isinstance(idx, dict):
            parts = []
            sheets = idx.get("sheets")
            if isinstance(sheets, dict):
                for k, v in sorted(sheets.items()):
                    t = v.get("title") if isinstance(v, dict) else v
                    parts.append("%s:%s" % (k, t))
            titles = "|".join(parts)
        try:
            n, docid, ver = rebuild_len(d)
        except Exception:
            n, docid = -1, name
        print("%s\t%s\t%d\t%s\t%s\t%s" % (
            uid, docid, n, sh.get("sh_1", ""), titles,
            ",".join("%s:%d" % (k, v) for k, v in sorted(sh.items()))))


if __name__ == "__main__":
    sys.exit(main())
