#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drv_survey.py —— 盘点 NAS 上全部 Synology Drive office 仓库（只读）。

对每个 <id>.sh 仓库输出：
  * HEAD commit
  * 全部历史 commit（沿 parent 链）
  * 每个 commit 的 tree 条目（名字 + blob 字节数）
  * metabinary/ 下的文件（名字 + 字节数）—— 那是**加密后的原件**，size-588 = orig_len
  * metabinary_info/ 下的文件（名字 + 字节数）

输出 JSON 到 stdout。
"""
import base64
import json
import os
import sys
import zlib

OFFICE = '/volume1/@SynoDrive/office'


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
    hdr = d[:i].decode("utf-8", "replace")
    return hdr.split()[0], d[i + 1:]


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
    """沿 master 沿 parent 链返回 [(commit_id, tree_id)]，从新到旧。"""
    ref = os.path.join(root, "refs", "heads", "master")
    try:
        with open(ref) as f:
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
        tree = None
        parent = None
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


def listdir_size(path):
    out = []
    try:
        names = sorted(os.listdir(path))
    except Exception:
        return out
    for n in names:
        if n == 'read-me3.txt':
            continue
        p = os.path.join(path, n)
        try:
            if os.path.isfile(p):
                out.append([n, os.path.getsize(p)])
        except Exception:
            pass
    return out


def main():
    res = {}
    try:
        repos = sorted(os.listdir(OFFICE))
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        return
    for name in repos:
        if not name.endswith('.sh'):
            continue
        rp = os.path.join(OFFICE, name)
        git = os.path.join(rp, 'version', '.git')
        rec = {'id': name, 'commits': [], 'metabinary': [], 'metabinary_info': [],
               'textdir': []}
        rec['metabinary'] = listdir_size(os.path.join(rp, 'metabinary'))
        rec['metabinary_info'] = listdir_size(os.path.join(rp, 'metabinary_info'))
        # 工作目录里的 text/
        try:
            for n in sorted(os.listdir(os.path.join(rp, 'version', 'text'))):
                p = os.path.join(rp, 'version', 'text', n)
                if os.path.isfile(p) and n != 'read-me3.txt':
                    rec['textdir'].append([n, os.path.getsize(p)])
        except Exception:
            pass
        for cid, tree in commits_of(git):
            ents = []
            for fn, sha, mode in ls_tree(git, tree):
                try:
                    typ, data = read_obj(git, sha)
                    sz = len(data) if typ == 'blob' else -1
                except Exception:
                    sz = -1
                ents.append([fn, sz])
            rec['commits'].append({'commit': cid, 'tree': tree, 'entries': ents})
        res[name] = rec
    json.dump(res, sys.stdout, ensure_ascii=False)


if __name__ == '__main__':
    main()
