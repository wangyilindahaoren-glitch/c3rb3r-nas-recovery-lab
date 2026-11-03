#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""osheet_rebuild_from_drive.py —— 用 Synology Drive 的 Git 明文**逐字节重建** .osheet 容器。

2026-11-03 实测：配方与 WPS 写出的文件**逐字节一致**（SHA-256 相同）。

容器格式（字节级，实测确认）
----------------------------
    文件   = 头部 + 条目链 + 尾段
    头部   = magic(78 00 00 00)
           + TLV(schema): tag=0x12, key="schema\\0", len=u32, 值3字节
           + 0x00 0x00 0x00（对齐填充）
           + TLV(enc):    tag=0x08, key="enc\\0",    len=u8,  值0字节
           + 0x00
           + TLV(id):     tag=0x02, key="id\\0",     len=u32, 值="<uid>_<docid>.sh"+NUL
           + 0x00
           + TLV(ver):    tag=0x02, key="ver\\0",    len=u32, 值=git commit id+NUL
           + 0x00
    条目   = [u32 X][u8 0x05][@C_<name>\\0][u32 clen][0x00][zlib 压缩数据][0x00]
             X = 4 + 1 + len("@C_"+name+"\\0") + 4 + 1 + clen + 1
             clen = **压缩后**字节数；zlib 参数 = level 9 / 默认策略 / wbits 15
                   （Python 的 zlib.compress(data, 9) 与 WPS 输出**逐字节相同**）
    末尾   = metatext 条目（见下）之后接尾段 [u32 11][0x08]["end\\0"][0x01 0x00]
    条目顺序 = 条目名字母序（text/* 在前），metatext 最后

metatext 条目的特殊结构（实测）
-------------------------------
    [u32 X][0x05]["@C_metatext\\0"][u32 20][0x00][zlib(b'{"ntype":37}') 20字节]
          [0x02]["metatext\\0"][u32 5]["null\\0"][0x00]
    即：压缩条目后面**还跟一段未压缩副本**，且末尾多一个 0x00 条目结束符。
    X = 62（固定），与内容无关。

用法
----
    # 1) 看某个 Drive 仓库每个 commit 能重建出多长的容器（用 orig_len 认领）
    python3 osheet_rebuild_from_drive.py --survey <office目录> --expect-len 839115

    # 2) 重建指定 commit（可排除某些条目）
    python3 osheet_rebuild_from_drive.py --repo <office目录> --ver <commit> \\
            --template <一个已知正常的 .osheet> --out <输出.osheet> \\
            [--expect-len 839115] [--exclude text/attachment]

    # 3) 校准：重建结果必须与一个**真实存在的** .osheet 逐字节相同
    python3 osheet_rebuild_from_drive.py --selftest <真实件> <office目录> <commit> [--exclude ...]
"""
import argparse
import base64
import hashlib
import os
import sys
import zlib

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

LEVEL = 9
META_ZLIB = zlib.compress(b'{"ntype":37}', LEVEL)
META_RAW = b'null\x00'


# ---------------- Drive Git 读取 ----------------
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
        return base64.b64decode(s).decode('utf-8', 'replace')
    except Exception:
        return s


def read_obj(root, sha):
    p = os.path.join(root, "objects", sha[:2], sha[2:])
    raw = open(p, 'rb').read()
    d = zlib.decompress(raw)
    i = d.index(b"\0")
    return d[:i].decode('utf-8', 'replace').split()[0], d[i + 1:]


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
        name = data[j + 1:k].decode('utf-8', 'replace')
        sha2 = data[k + 1:k + 21].hex()
        i = k + 21
        if mode.startswith("40"):
            out.extend(ls_tree(root, sha2, prefix + name + "/"))
        else:
            out.append((prefix + name, sha2, mode))
    return out


def commits_of(root):
    try:
        c = open(os.path.join(root, "refs", "heads", "master")).read().strip()
    except Exception:
        return []
    out, seen = [], set()
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


def tree_entries(git, tree):
    """{条目名: blob 原始字节}，条目名与容器内一致（text/xxx、metatext）"""
    out = {}
    for fn, sha, mode in ls_tree(git, tree):
        base = fn.rsplit('/', 1)[-1]
        if base == 'read-me3.txt':
            continue
        nm = b64name(base)
        full = nm if nm in ('metatext', 'metabinairy_files') else ('text/' + nm)
        try:
            typ, data = read_obj(git, sha)
        except Exception:
            continue
        if typ == 'blob':
            out[full] = data
    return out


# ---------------- 容器解析 / 构建 ----------------
def parse_header(data):
    """返回 (header 列表, 条目链起点)"""
    pos = 4
    header = []
    while True:
        if pos + 9 <= len(data) and data[pos + 4] in (0x05, 0x02) and \
                (data[pos + 5:pos + 8] == b'@C_' or data[pos + 5:pos + 9] == b'text'):
            break
        if pos >= len(data):
            break
        tag = data[pos]
        if tag == 0x00:
            pos += 1
            continue
        e = data.find(b'\x00', pos + 1)
        key = data[pos + 1:e].decode('latin1')
        p = e + 1
        if tag == 0x08:
            ln = data[p]; p += 1
        else:
            ln = int.from_bytes(data[p:p + 4], 'little'); p += 4
        header.append((tag, key, data[p:p + ln]))
        p += ln
        if p < len(data) and data[p] == 0:
            p += 1
        pos = p
    return header, pos


def parse_entries(data, start):
    pos = start
    entries = []
    while pos < len(data):
        if pos + 9 > len(data):
            break
        X = int.from_bytes(data[pos:pos + 4], 'little')
        tag = data[pos + 4]
        if tag not in (0x05, 0x02):
            break
        e = data.find(b'\x00', pos + 5)
        name = data[pos + 5:e].decode('latin1')
        p = e + 1
        ln = int.from_bytes(data[p:p + 4], 'little'); p += 4
        if tag == 0x05:
            zbytes = data[p + 1:p + 1 + ln]
            payload = zlib.decompress(zbytes)
        else:
            payload = data[p:p + ln]
        entries.append((pos, X, name, tag, ln, zbytes, payload))
        pos += X
        if pos < len(data) and data[pos + 4:pos + 5] == b'\x08':
            break
    return entries, pos


def build_header(tmpl, docid, ver):
    """照抄模板头部结构，只替换 id / ver 的值（长度字段同步改写）。"""
    i = tmpl.find(b'\x02id\x00')
    j = tmpl.find(b'\x02ver\x00')
    if i < 0 or j < 0:
        raise ValueError('模板里没有 id/ver 字段')
    idlen = int.from_bytes(tmpl[i + 4:i + 8], 'little')
    idend = i + 8 + idlen
    verlen = int.from_bytes(tmpl[j + 5:j + 9], 'little')
    verstart = j + 9
    verend = verstart + verlen
    entry_start = parse_header(tmpl)[1]
    idval = docid.encode() + b'\x00'
    verval = ver.encode() + b'\x00'
    return (tmpl[:i + 4] + len(idval).to_bytes(4, 'little') + idval
            + tmpl[idend:j + 5] + len(verval).to_bytes(4, 'little') + verval
            + tmpl[verend:entry_start])


def build(ents, tmpl, docid, ver):
    """ents: {条目名: 原始字节}；tmpl: 一个已知正常的 .osheet（只借它的头部结构）"""
    out = bytearray(build_header(tmpl, docid, ver))
    items = sorted(ents.items(), key=lambda e: (not e[0].startswith('text/'), e[0]))
    items = [(n, d) for n, d in items if n != 'metatext']
    for n, d in items:
        nb = b'@C_' + n.encode() + b'\x00'
        z = zlib.compress(d, LEVEL)
        body = bytes([0x05]) + nb + len(z).to_bytes(4, 'little') + b'\x00' + z + b'\x00'
        out += (4 + len(body)).to_bytes(4, 'little') + body
    nb = b'@C_metatext\x00'
    body = bytes([0x05]) + nb + len(META_ZLIB).to_bytes(4, 'little') + b'\x00' + META_ZLIB
    body += bytes([0x02]) + b'metatext\x00' + (5).to_bytes(4, 'little') + META_RAW + b'\x00'
    out += (4 + len(body)).to_bytes(4, 'little') + body
    out += (11).to_bytes(4, 'little') + b'\x08end\x00\x01\x00'
    return bytes(out)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def gitdir_of(repo):
    for cand in (os.path.join(repo, 'version', '.git'), repo):
        if os.path.isdir(os.path.join(cand, 'objects')):
            return cand
    raise SystemExit('找不到 git 目录: %s' % repo)


def docid_of(repo):
    """office 目录名 == 容器里的 id 字段（<uid>_<docid>.sh）。"""
    base = os.path.basename(repo.rstrip('/'))
    if base in ('git', '.git'):
        base = os.path.basename(os.path.dirname(os.path.join(repo, 'version', '.git')).rstrip('/'))
    if not base.endswith('.sh'):
        base += '.sh'
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', help='Drive office 目录（含 version/.git）')
    ap.add_argument('--ver', help='commit id')
    ap.add_argument('--out')
    ap.add_argument('--template', help='已知正常的 .osheet（提供头部结构）')
    ap.add_argument('--expect-len', type=int, default=0)
    ap.add_argument('--exclude', action='append', default=[], help='排除的条目名，可重复')
    ap.add_argument('--docid', help='覆盖容器 id 字段（默认取 office 目录名）')
    ap.add_argument('--survey', action='store_true', help='列出每个 commit 的重建长度')
    ap.add_argument('--selftest', nargs=2, metavar=('真实件', 'office目录'))
    args = ap.parse_args()

    if args.selftest:
        real, repo = args.selftest
        git = gitdir_of(repo)
        tmpl = open(real, 'rb').read()
        hdr, _ = parse_header(tmpl)
        docid = next((v.rstrip(b'\x00').decode() for _t, k, v in hdr if k == 'id'), '')
        ok = 0
        for cid, tree in commits_of(git):
            ents = tree_entries(git, tree)
            got = build(ents, tmpl, docid, cid)
            same = (got == tmpl)
            ok += same
            print('commit %s  重建 %d 字节  %s' % (cid[:12], len(got), '逐字节一致 ✓' if same else '不一致 ✗'))
        print('自检：%d 个 commit 逐字节一致' % ok)
        return 0

    if not args.repo:
        ap.error('需要 --repo')
    git = gitdir_of(args.repo)
    name = args.docid or docid_of(args.repo)
    tmpl = open(args.template, 'rb').read() if args.template else None

    if args.survey:
        for cid, tree in commits_of(git):
            ents = tree_entries(git, tree)
            keep = {k: v for k, v in ents.items() if k not in args.exclude}
            if tmpl is None:
                print('commit %s  条目 %d  %s' % (cid[:12], len(ents), sorted(ents)))
                continue
            got = build(keep, tmpl, name, cid)
            hit = (args.expect_len and len(got) == args.expect_len)
            print('commit %s  条目 %d  重建长度 %-10d %s'
                  % (cid[:12], len(keep), len(got), '✓ == orig_len' if hit else ''))
        return 0

    if not args.ver or not args.out or not args.template:
        ap.error('需要 --ver --out --template')
    ents = {}
    for cid, tree in commits_of(git):
        if cid == args.ver:
            ents = tree_entries(git, tree)
    if not ents:
        raise SystemExit('找不到 commit %s' % args.ver)
    keep = {k: v for k, v in ents.items() if k not in args.exclude}
    out = build(keep, tmpl, name, args.ver)
    with open(args.out, 'wb') as f:
        f.write(out)
    print('重建 %s  长度 %d  sha256=%s  条目 %d'
          % (args.out, len(out), sha(out), len(keep)))
    if args.expect_len and len(out) != args.expect_len:
        print('!! 长度不等于期望 %d（差 %+d）' % (args.expect_len, len(out) - args.expect_len))
        return 1
    if args.expect_len and len(out) == args.expect_len:
        print('✓ 长度与勒索标记 orig_len 分毫不差')
    return 0


if __name__ == '__main__':
    sys.exit(main())
