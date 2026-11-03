#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gitgrep.py —— 遍历一个（或多个）Git 仓库的所有 loose object，找含指定关键字的 blob。

Synology Drive 的 office 集成把每个 WPS 文档存成一个 Git 仓库。
勒索程序按扩展名加密，Git object（无扩展名）与 version/text/ 都没被加密，
所以勒索前的 metatext.json（含 title=文件名）往往还在历史对象里。

用法:
  python3 gitgrep.py <git根目录> [更多目录...] -- <关键字1> [关键字2] ...
输出: <目录名>\t<对象sha>\t<命中片段>
"""
import os
import sys
import zlib


def iter_objects(root):
    objdir = os.path.join(root, "objects")
    for d1 in sorted(os.listdir(objdir)):
        p1 = os.path.join(objdir, d1)
        if not os.path.isdir(p1) or len(d1) != 2:
            continue
        for fn in sorted(os.listdir(p1)):
            if len(fn) != 38:
                continue
            sha = d1 + fn
            try:
                with open(os.path.join(p1, fn), "rb") as f:
                    raw = f.read()
                d = zlib.decompress(raw)
            except Exception:
                continue
            i = d.find(b"\0")
            if i < 0:
                continue
            hdr = d[:i].decode("utf-8", "replace").split()
            if not hdr:
                continue
            yield sha, hdr[0], d[i + 1:]


def main():
    argv = sys.argv[1:]
    if "--" not in argv:
        print(__doc__)
        return 1
    i = argv.index("--")
    dirs = argv[:i]
    needles = [n.encode("utf-8") for n in argv[i + 1:]]
    for root in dirs:
        label = root.rstrip("/").split("/")[-3] if len(root.rstrip("/").split("/")) >= 3 else root
        for sha, typ, data in iter_objects(root):
            for n in needles:
                if n in data:
                    # 提取命中上下文
                    k = data.find(n)
                    snippet = data[max(0, k - 120):k + 200]
                    try:
                        sn = snippet.decode("utf-8", "replace")
                    except Exception:
                        sn = repr(snippet)
                    print("%s\t%s\t%s\t%s" % (label, sha, typ, sn.replace("\n", " ")))
                    break
    return 0


if __name__ == "__main__":
    sys.exit(main())
