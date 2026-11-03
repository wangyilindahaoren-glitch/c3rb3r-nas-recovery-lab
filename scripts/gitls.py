#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gitls.py —— 纯标准库读取 Synology Drive office 的 Git 仓库（无 git 命令时用）。

用法:
  python3 gitls.py <git根目录>              列出 HEAD 里的所有文件
  python3 gitls.py <git根目录> --cat <路径>  打印该路径的 blob 内容（zlib 解压后）
  python3 gitls.py <git根目录> --grep <子串> 在所有 blob 里找含该子串的内容

Synology Drive 的 office 集成把每个 WPS 文档存成一个 Git 仓库：
  refs/heads/master 的 commit id == osheet 头部里的 ver 字段
仓库里的 blob 是勒索前的明文（Git 对象无扩展名，勒索程序没加密）。
"""
import os
import sys
import zlib


def read_obj(root, sha):
    p = os.path.join(root, "objects", sha[:2], sha[2:])
    with open(p, "rb") as f:
        raw = f.read()
    d = zlib.decompress(raw)
    i = d.index(b"\0")
    hdr = d[:i].decode("utf-8", "replace")
    typ = hdr.split()[0]
    return typ, d[i + 1:]


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


def head_tree(root):
    ref = os.path.join(root, "refs", "heads", "master")
    with open(ref) as f:
        commit = f.read().strip()
    typ, data = read_obj(root, commit)
    tree = None
    for line in data.split(b"\n"):
        if line.startswith(b"tree "):
            tree = line.split()[1].decode()
            break
    return commit, tree


def main():
    root = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "ls"
    commit, tree = head_tree(root)
    files = ls_tree(root, tree)
    if mode == "ls":
        print("commit %s" % commit)
        for name, sha, m in files:
            print("%s\t%s\t%s" % (m, sha, name))
    elif mode == "--cat":
        want = sys.argv[3]
        for name, sha, m in files:
            if name == want or name.endswith("/" + want):
                typ, data = read_obj(root, sha)
                sys.stdout.buffer.write(data)
                break
    elif mode == "--grep":
        needle = sys.argv[3].encode("utf-8")
        for name, sha, m in files:
            typ, data = read_obj(root, sha)
            if needle in data:
                print("### %s (%s)" % (name, sha))
                try:
                    print(data.decode("utf-8"))
                except Exception:
                    print(repr(data[:400]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
