#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drv_grep.py —— 在 NAS 上扫描全部 Drive 仓库的所有 git 对象，找含关键字的 blob。
只读；输出 repo / commit / 条目名 / 命中片段。
"""
import base64
import json
import os
import sys
import zlib

OFFICE = '/volume1/@SynoDrive/office'
KEYS = [k.encode('utf-8') for k in sys.argv[1:]] or ['授权'.encode('utf-8')]


def b64name(fn):
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


def read_obj(root, sha):
    p = os.path.join(root, "objects", sha[:2], sha[2:])
    with open(p, "rb") as f:
        raw = f.read()
    d = zlib.decompress(raw)
    i = d.index(b"\0")
    return d[:i].decode("utf-8", "replace").split()[0], d[i + 1:]


def ls_tree(root, sha, prefix=""):
    typ, data = read_obj(root, sha)
    if typ != "tree":
        return []
    out = []
    i = 0
    while i < len(data):
        j = data.index(b" ", i)
        mode = data[i:j].decode()
        k = data.index(b"\0", j)
        name = data[j + 1:k].decode("utf-8", "replace")
        sha2 = data[k + 1:k + 21].hex()
        i = k + 21
        if mode.startswith("40"):
            out.extend(ls_tree(root, sha2, prefix + name + "/"))
        else:
            out.append((prefix + name, sha2, mode))
    return out


def commits_of(root):
    try:
        with open(os.path.join(root, "refs", "heads", "master")) as f:
            c = f.read().strip()
    except Exception:
        return []
    out = []
    seen = set()
    while c and c not in seen:
        seen.add(c)
        try:
            typ, data = read_obj(root, c)
        except Exception:
            break
        tree = parent = None
        for line in data.split(b"\n"):
            if line.startswith(b"tree "):
                tree = line.split()[1].decode()
            elif line.startswith(b"parent "):
                parent = line.split()[1].decode()
            elif not line.strip():
                break
        if not tree:
            break
        out.append((c, tree))
        c = parent
    return out


def main():
    for name in sorted(os.listdir(OFFICE)):
        if not name.endswith('.sh'):
            continue
        git = os.path.join(OFFICE, name, 'version', '.git')
        for cid, tree in commits_of(git):
            for fn, sha, mode in ls_tree(git, tree):
                if not fn.startswith('text/'):
                    continue
                try:
                    typ, data = read_obj(git, sha)
                except Exception:
                    continue
                if typ != 'blob':
                    continue
                for k in KEYS:
                    if k in data:
                        i = data.find(k)
                        lo = max(0, i - 60)
                        snip = data[lo:i + 120]
                        try:
                            txt = snip.decode('utf-8', 'replace')
                        except Exception:
                            txt = repr(snip)
                        print("HIT\t%s\t%s\t%s\t%s\t%s"
                              % (name, cid[:12], b64name(fn.rsplit('/', 1)[-1]),
                                 k.decode('utf-8'), txt.replace('\n', ' ')))
                        break
    sys.stderr.write('done\n')


if __name__ == '__main__':
    main()
