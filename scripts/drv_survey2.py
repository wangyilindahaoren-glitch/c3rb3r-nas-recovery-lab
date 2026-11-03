#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drv_survey2.py —— 对全部 Drive 仓库的每个 commit，算出两种形态的容器长度。

  * 不压缩形态（rebuild_exact 那套）：tag=0x02，条目载荷 = Drive 原始字节
  * WPS 压缩形态（build_wps 那套）：tag=0x05，@C_ 前缀，zlib 载荷

两种形态各按 zlib level 6/7/8/9 算一遍，供长度匹配用。
输出 JSON：{repo: {commit: {shape: {level: len}}}}
"""
import base64
import json
import os
import sys
import zlib

OFFICE = '/volume1/@SynoDrive/office'
LEVELS = (6, 7, 8, 9)
ZERO = b'\x00'
FOOTER_LEN = 11


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


def header_len(repo_id, schema_len=3):
    """magic(4) + schema TLV + enc TLV + id TLV + ver TLV"""
    # schema: tag+key\0+len4+value(3)+sep = 1+7+4+3+1 = 16
    # enc   : 1+4+1+0+1 = 7
    # id    : 1+3+4+ (len(docid)+1) +1
    # ver   : 1+4+4+ 41 +1
    return 4 + 16 + 7 + (1 + 3 + 4 + len(repo_id) + 1 + 1) + (1 + 4 + 4 + 41 + 1)


def uncompressed_len(repo_id, ents, meta_len):
    tot = header_len(repo_id) - 1 + 1     # 头部同构（含 schema=3）
    for name, data in ents:
        tot += 12 + len(name) + len(data)
    tot += 12 + len('metatext') + meta_len
    tot += FOOTER_LEN
    return tot


def compressed_len(repo_id, ents, meta, meta_raw, lvl):
    tot = header_len(repo_id)
    for name, data in ents:
        z = zlib.compress(data, lvl)
        tot += 4 + 1 + (4 + len(name) + 1) + 4 + 1 + len(z) + 1
    z = zlib.compress(meta, lvl)
    tot += 4 + (1 + 12 + 4 + 1 + len(z) + 1) + (1 + 9 + 4 + 5 + 1) - 1
    tot += FOOTER_LEN
    return tot


def main():
    res = {}
    for name in sorted(os.listdir(OFFICE)):
        if not name.endswith('.sh'):
            continue
        git = os.path.join(OFFICE, name, 'version', '.git')
        rec = {}
        for cid, tree in commits_of(git):
            ents = []
            for fn, sha, mode in ls_tree(git, tree):
                if not fn.startswith('text/'):
                    continue
                base = fn.rsplit('/', 1)[-1]
                if base == 'read-me3.txt':
                    continue
                nm = b64name(base)
                try:
                    typ, data = read_obj(git, sha)
                except Exception:
                    continue
                if typ != 'blob':
                    continue
                ents.append((nm, data))
            ents = sorted(ents, key=lambda e: e[0])
            e = {
                'uncompressed': uncompressed_len(name, ents, 12),
                'uncompressed_long': uncompressed_len(name, ents, 109),
            }
            for lvl in LEVELS:
                e['c%d' % lvl] = compressed_len(name, ents, b'{"ntype":37}', b'null', lvl)
            rec[cid] = e
        res[name] = rec
    json.dump(res, sys.stdout, ensure_ascii=False)


if __name__ == '__main__':
    main()
