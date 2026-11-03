#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_wps.py —— 用 Drive 明文重建 **WPS 原生压缩形式** 的 .osheet 容器。

按 recovered2/未付款明细.osheet（真·WPS 原生件）的字节布局 1:1 复刻：
  头 = magic + TLV(schema,值3字节) + 0x00 + TLV(enc,len0) + TLV(id) + TLV(ver) + 0x00
  条目 = [u32 X][0x05][@C_名字\\0][u32 zlib长][0x00][zlib][0x00]
  metatext 额外跟一段未压缩副本： [0x02][metatext\\0][u32 len][载荷][0x00]
  尾段 = [u32 11][0x08]["end\\0"][0x01 0x00]
"""
import argparse
import base64
import hashlib
import os
import sys
import zlib

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ZERO = b'\x00'
FOOTER = (11).to_bytes(4, 'little') + b'\x08' + b'end' + ZERO + b'\x01' + ZERO


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


def tlv(tag, key, val):
    return bytes([tag]) + key.encode() + ZERO + len(val).to_bytes(4, 'little') + val + ZERO


def build(textdir, docid, ver, meta=b'{"ntype":37}', meta_raw=b'null'):
    hdr = b'\x78\x00\x00\x00'
    hdr += tlv(0x12, 'schema', b'\x00\x00\x00')
    hdr += bytes([0x08]) + b'enc' + ZERO + b'\x00' + ZERO
    hdr += tlv(0x02, 'id', docid.encode() + ZERO)
    hdr += tlv(0x02, 'ver', ver.encode() + ZERO)

    ents = []
    for fn in sorted(os.listdir(textdir)):
        p = os.path.join(textdir, fn)
        if not os.path.isfile(p):
            continue
        nm = b64name(fn)
        if nm == 'read-me3.txt':
            continue
        ents.append((nm, open(p, 'rb').read()))
    ents = sorted(ents, key=lambda e: e[0])

    out = bytearray(hdr)
    for name, data in ents:
        z = zlib.compress(data, 9)
        nb = b'@C_' + name.encode() + ZERO
        body = bytes([0x05]) + nb + len(z).to_bytes(4, 'little') + ZERO + z + ZERO
        span = 4 + len(body)
        out += span.to_bytes(4, 'little') + body
    # metatext：压缩一份 + 未压缩副本
    z = zlib.compress(meta, 9)
    nb = b'@C_metatext' + ZERO
    body = bytes([0x05]) + nb + len(z).to_bytes(4, 'little') + ZERO + z + ZERO
    body += bytes([0x02]) + b'metatext' + ZERO + len(meta_raw + ZERO).to_bytes(4, 'little') \
        + meta_raw + ZERO + ZERO
    span = 4 + len(body) - 1        # 与真件一致：X 不计最后那个 0x00
    out += span.to_bytes(4, 'little') + body
    out += FOOTER
    return bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--drive', required=True)
    ap.add_argument('--ver', required=True)
    ap.add_argument('--docid', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--expect-len', type=int, default=0)
    a = ap.parse_args()
    ver = open(a.ver).read().strip()
    data = build(a.drive, a.docid, ver)
    with open(a.out, 'wb') as f:
        f.write(data)
    print('重建 %s -> %d 字节  sha256=%s' % (a.out, len(data), hashlib.sha256(data).hexdigest()[:16]))
    if a.expect_len:
        d = len(data) - a.expect_len
        print(('长度一致 ✓' if d == 0 else '!! 长度差 %+d' % d))


if __name__ == '__main__':
    main()
