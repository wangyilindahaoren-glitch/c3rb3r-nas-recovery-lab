#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rebuild_from_drive.py —— 用 Synology Drive office 目录里的 Git 数据重建 .osheet 容器。

依据（2026-10-31 实测确认）：
  * office 目录名 `<uid>_<docid>.sh` == .osheet 头部 id 字段的值（去掉结尾 NUL）；
  * `version/.git/refs/heads/master` 的 commit id == .osheet 头部 ver 字段的值；
  * `version/text/<base64条目名>` 是该文档的条目内容（未压缩形式，勒索没加密）；
  * .osheet 的条目顺序 = 条目名字母序（text/* 在前），裸名条目（metatext）在最后。

**头部用「模板法」**：直接照抄一个已知正常 .osheet 的头部字节结构，只替换 id / ver 两个字段值，
避免手工推导 schema=2 / schema=3 两种头部布局的差异（实测手写会差 1 字节）。

用法:
  python3 rebuild_from_drive.py <office目录> <输出.osheet> [--template 正常.osheet] [--expect-len N]
"""
import base64
import os
import struct
import sys

MAGIC = b'\x78\x00\x00\x00'


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


def header_end(template):
    """逐字段解析模板头部，返回条目链的起始偏移（头部结束位置）。"""
    pos = 4
    while pos < len(template):
        tag = template[pos]
        if tag == 0x00:
            pos += 1
            continue
        e = template.find(b'\x00', pos + 1)
        if e < 0:
            raise ValueError("模板头部 key 截断")
        p = e + 1
        if tag == 0x08:
            ln = template[p]
            p += 1
        else:
            ln = struct.unpack_from('<I', template, p)[0]
            p += 4
        p += ln
        if p < len(template) and template[p] == 0:
            p += 1
        if p + 5 <= len(template) and template[p + 4] in (0x05, 0x02) and \
                (template[p + 5:p + 8] == b'@C_' or template[p + 5:p + 9] == b'text'):
            return p
        pos = p
    raise ValueError("模板里找不到条目链起点")


def build_header_like(template, docid, ver):
    """照抄模板头部结构，只替换 id / ver 的值（长度可变，故按切片拼接）。"""
    i = template.find(b'\x02id\x00')
    if i < 0:
        raise ValueError("模板里没有 id 字段")
    # tag(1) + key("id\0"=3) 之后才是长度字段
    idlen = struct.unpack_from('<I', template, i + 4)[0]
    idstart = i + 4 + 4
    idend = idstart + idlen
    j = template.find(b'\x02ver\x00')
    if j < 0:
        raise ValueError("模板里没有 ver 字段")
    # tag(1) + key("ver\0"=4) 之后才是长度字段
    verlen = struct.unpack_from('<I', template, j + 5)[0]
    verstart = j + 5 + 4
    verend = verstart + verlen
    # 头部结束 = 条目链的起点（逐字段解析头部，不能用 find 猜）
    entry_start = header_end(template)
    return (template[:idstart] + docid.encode() + b'\x00'
            + template[idend:verstart] + ver.encode() + b'\x00'
            + template[verend:entry_start])


def entries_of(office_dir):
    textdir = os.path.join(office_dir, "version", "text")
    ents = []
    for fn in sorted(os.listdir(textdir)):
        p = os.path.join(textdir, fn)
        if not os.path.isfile(p):
            continue
        name = b64name(fn)
        if name == "read-me3.txt":
            continue
        with open(p, "rb") as f:
            data = f.read()
        # Git 里条目名去掉了 "text/" 前缀；metatext 等是裸名
        full = name if name in ('metatext', 'metabinairy_files') else ('text/' + name)
        ents.append((full, data))
    # 条目顺序 = 名称字母序，裸名条目放最后
    ents.sort(key=lambda e: (e[0].startswith('text/'), e[0]))
    return ents


def build_footer():
    val = b'\x01\x00'
    body = b'\x08' + b'end\x00' + val
    return struct.pack('<I', len(body) + 4) + body


def rebuild(office_dir, out_path, template, with_footer=True):
    docid = os.path.basename(office_dir.rstrip("/"))
    ref = os.path.join(office_dir, "version", ".git", "refs", "heads", "master")
    ver = open(ref).read().strip()
    ents = entries_of(office_dir)
    out = bytearray(build_header_like(template, docid, ver))
    # Git 里没有 metatext 条目，但 .osheet 里有（裸名，放在条目链最后）
    if not any(n == 'metatext' for n, _ in ents):
        ents = ents + [('metatext', b'{"ntype":37}')]
    for name, data in ents:
        nb = name.encode() + b'\x00'
        body = bytes([0x02]) + nb + struct.pack('<I', len(data)) + data + b'\x00'
        out += struct.pack('<I', 4 + len(body)) + body
    if with_footer:
        out += build_footer()
    with open(out_path, "wb") as f:
        f.write(bytes(out))
    return len(out), docid, ver, [n for n, _ in ents]


if __name__ == "__main__":
    office = sys.argv[1]
    out = sys.argv[2]
    tpl = None
    if "--template" in sys.argv:
        tpl = sys.argv[sys.argv.index("--template") + 1]
    expect = None
    if "--expect-len" in sys.argv:
        expect = int(sys.argv[sys.argv.index("--expect-len") + 1])
    if tpl is None:
        raise SystemExit("必须给 --template <一个正常的 .osheet>")
    n, docid, ver, names = rebuild(office, out, open(tpl, "rb").read())
    print("重建 %s -> %s  长度 %d  id=%s  ver=%s" % (office, out, n, docid, ver))
    print("条目: %s" % names)
    if expect and n != expect:
        print("!! 长度不等于期望 %d（差 %+d）" % (expect, n - expect))
