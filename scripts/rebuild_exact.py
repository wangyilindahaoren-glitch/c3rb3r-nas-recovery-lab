#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rebuild_exact.py —— 用 Drive 明文**逐字节重建** .osheet 原件。

依据（2026-10-31 实战实测，7/7 与雕刻原件 SHA-256 完全一致）：
  * 原件条目是**未压缩**的（tag=0x02），载荷 = Drive 的 version/text/<条目> 字节 + 一个 0x00
  * 条目封装 = [u32 X][u8 0x02][name\\0][u32 len][载荷][u8 0x00]
      X = 4 + 1 + len(name) + 1 + 4 + len(载荷) + 1
  * 条目顺序 = 名字母序（text/* 在前），裸名条目（metatext）在最后
  * 头部 = magic + TLV(schema) + 0x00 + TLV(enc) + TLV(id) + TLV(ver) + 0x00
      schema 值/长度按模板（schema=2 版多一个 0x00 填充字节）
  * 尾段 = [u32 11][0x08]["end\\0"][0x01 0x00]

用法:
    python3 rebuild_exact.py --drive <Drive text目录> --ver <git commit id> \\
            --out <输出.osheet> [--template <已知正常的 .osheet>] [--expect-len N]
    python3 rebuild_exact.py --selftest <雕刻原件目录> <Drive根目录> <映射.json>
"""
import argparse
import base64
import hashlib
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

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


def drive_entries(textdir):
    """{条目名: Drive 里的原始字节}，条目名与容器内一致（text/sh_1 等）。"""
    ents = {}
    for fn in sorted(os.listdir(textdir)):
        p = os.path.join(textdir, fn)
        if not os.path.isfile(p):
            continue
        name = b64name(fn)
        if name == 'read-me3.txt':
            continue
        full = name if name in ('metatext', 'metabinairy_files') else ('text/' + name)
        ents[full] = open(p, 'rb').read()
    return ents


def split_header(raw):
    """解析头部，返回 (头部字节, 条目链起点)。"""
    pos = 4
    hdr = []
    while True:
        if pos + 9 <= len(raw) and raw[pos + 4] in (0x05, 0x02) and \
                (raw[pos + 5:pos + 8] == b'@C_' or raw[pos + 5:pos + 9] == b'text'):
            break
        if pos >= len(raw):
            break
        tag = raw[pos]
        if tag == 0x00:
            pos += 1
            continue
        e = raw.find(b'\x00', pos + 1)
        key = raw[pos + 1:e].decode('latin1')
        p = e + 1
        if tag == 0x08:
            ln = raw[p]
            p += 1
        else:
            ln = int.from_bytes(raw[p:p + 4], 'little')
            p += 4
        hdr.append((tag, key, raw[p:p + ln]))
        p += ln
        if p < len(raw) and raw[p] == 0:
            p += 1
        pos = p
    return raw[:pos], pos, hdr


def build_header(template, docid, ver):
    """照抄模板头部结构，只替换 id / ver 的值（并同步改写长度字段）。"""
    i = template.find(b'\x02id\x00')
    j = template.find(b'\x02ver\x00')
    if i < 0 or j < 0:
        raise ValueError('模板里没有 id/ver 字段')
    idlen = int.from_bytes(template[i + 4:i + 8], 'little')
    idstart = i + 8
    idend = idstart + idlen
    verlen = int.from_bytes(template[j + 5:j + 9], 'little')
    verstart = j + 9
    verend = verstart + verlen
    entry_start = split_header(template)[1]
    idval = docid.encode() + b'\x00'      # 值含结尾 NUL，长度字段也含它
    verval = ver.encode() + b'\x00'
    return (template[:i + 4] + len(idval).to_bytes(4, 'little') + idval
            + template[idend:j + 5] + len(verval).to_bytes(4, 'little') + verval
            + template[verend:entry_start])


def build_footer():
    body = b'\x08' + b'end\x00' + b'\x01\x00'
    # u32 是**尾段总长**（含这 4 字节长度字段本身），实测原值为 11
    return (len(body) + 4).to_bytes(4, 'little') + body


def rebuild(textdir, ver, template_raw, docid, metatext=None):
    hdr = build_header(template_raw, docid, ver)
    ents = drive_entries(textdir)
    # 裸名条目（metatext）排最后；text/* 按名字母序
    ents = sorted(ents.items(), key=lambda e: (not e[0].startswith('text/'), e[0]))
    ents = [(n, d) for n, d in ents if n != 'metatext']
    if metatext is None:
        metatext = b'{"ntype":37}'
    ents.append(('metatext', metatext))
    out = bytearray(hdr)
    for name, data in ents:
        payload = data + b'\x00'
        nb = name.encode() + b'\x00'
        X = 4 + 1 + len(nb) + 4 + len(payload) + 1
        out += X.to_bytes(4, 'little') + bytes([0x02]) + nb \
            + len(payload).to_bytes(4, 'little') + payload + b'\x00'
    out += build_footer()
    return bytes(out)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def selftest(carved_dir, drive_root, mapfile):
    mapping = json.load(open(mapfile, encoding='utf-8'))
    ok = 0
    for orig_name, prefix in mapping.items():
        op = os.path.join(carved_dir, orig_name)
        dp = os.path.join(drive_root, prefix + '.text')
        vp = os.path.join(drive_root, prefix + '.ver')
        if not os.path.exists(op):
            continue
        orig = open(op, 'rb').read()
        ver = open(vp).read().strip()
        # 模板用**另一个**已知正常件，避免"自己抄自己"掩盖差异
        tmpl_name = sorted(f for f in os.listdir(carved_dir) if f.endswith('.osheet')
                           and f != orig_name)[0]
        tmpl = open(os.path.join(carved_dir, tmpl_name), 'rb').read()
        got = rebuild(dp, ver, tmpl, prefix + '.sh')
        same = (got == orig)
        ok += same
        print('%-46s 原件 %d 字节 / 重建 %d 字节  差 %+d  SHA %s'
              % (orig_name, len(orig), len(got), len(got) - len(orig),
                 '一致 ✓' if same else '不一致 ✗'))
        if not same:
            n = min(len(got), len(orig))
            for i in range(n):
                if got[i] != orig[i]:
                    print('    首个差异 @%d: 原件=%r 重建=%r' % (i, orig[i:i + 24], got[i:i + 24]))
                    break
    print('\n自检：%d/%d 逐字节一致' % (ok, len(mapping)))
    return 0 if ok == len(mapping) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--drive')
    ap.add_argument('--ver')
    ap.add_argument('--out')
    ap.add_argument('--template')
    ap.add_argument('--expect-len', type=int, default=0)
    ap.add_argument('--metatext-hex', default=None,
                    help='metatext 条目的载荷（含结尾 NUL）十六进制；'
                         '不传则用默认 {"ntype":37}（长度必须与 orig_len 吻合）')
    ap.add_argument('--selftest', nargs=3, metavar=('雕刻目录', 'Drive根', '映射json'))
    args = ap.parse_args()

    if args.selftest:
        return selftest(*args.selftest)

    tmpl = open(args.template, 'rb').read()
    ver = open(args.ver).read().strip()
    # Drive 侧目录名是 <uid>_<docid>.text，容器里的 id 字段是 <uid>_<docid>.sh
    docid = os.path.basename(args.drive.rstrip('/'))
    if docid.endswith('.text'):
        docid = docid[:-len('.text')] + '.sh'
    meta = bytes.fromhex(args.metatext_hex) if args.metatext_hex else None
    out = rebuild(args.drive, ver, tmpl, docid, meta)
    with open(args.out, 'wb') as f:
        f.write(out)
    print('重建 %s -> %s  长度 %d  id=%s  ver=%s  sha256=%s'
          % (args.drive, args.out, len(out), docid, ver, sha(out)[:16]))
    if args.expect_len and len(out) != args.expect_len:
        print('!! 长度不等于期望 %d（差 %+d）' % (args.expect_len, len(out) - args.expect_len))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
