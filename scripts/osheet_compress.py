#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 .osheet 里"不压缩"的条目（tag=0x02）改写成"压缩"形式（tag=0x05）。

背景：精确恢复出来的 .osheet（客服上班表-最新.osheet 等）条目全是压缩形式
      条目 := [u32 X][u8 0x05][@C_名字\\0][u32 len][0x00][zlib(len)][0x00]
      （metatext 条目后面还会跟一段未压缩副本 [0x02][名字\\0][u32 rawlen][raw][0x00]）
      而路由 A 候选件里的条目是不压缩形式（tag=0x02、无 @C_ 前缀、载荷是原始 JSON）。
      两种形式 WPS 都可能只认其中一种，所以这里给出压缩版。

用法：python3 osheet_compress.py <输入> <输出>
"""
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from osheet_repair import (prescan, parse_header, find_entries, find_footer,
                           build_header, MAGIC, ZERO)


def compress_file(src, dst, verbose=True):
    raw = open(src, 'rb').read()
    data, notes = prescan(raw)
    foot = find_footer(data)
    hdr, first, err = parse_header(data)
    if err:
        raise ValueError(err)
    head = build_header(hdr, notes)
    ents = find_entries(data, first, foot)
    if not ents:
        raise ValueError('找不到条目')

    out = bytearray(head)
    changed = 0
    last = ents[-1]
    last_bound = last[0] + int.from_bytes(data[last[0]:last[0] + 4], 'little')
    for i, e in enumerate(ents):
        es, tag, name, ln, ps, pl = e
        payload = data[ps:ps + pl]
        if i + 1 < len(ents):
            bound = ents[i + 1][0]
        else:
            bound = last_bound      # 末条目按它自己的 X 收尾，后面的 blob/尾段另算
        trail = data[ps + pl:bound]
        if tag == 0x05:
            # 已是压缩形式：原样搬（名字后补 NUL，再 len / 0x00 / zlib / 尾随）
            body = bytes([0x05]) + name + ZERO + len(payload).to_bytes(4, 'little') + \
                ZERO + payload + trail
        else:
            z = zlib.compress(payload, 9)
            nb = b'@C_' + name + ZERO
            body = bytes([0x05]) + nb + len(z).to_bytes(4, 'little') + \
                ZERO + z + trail
            if name.endswith(b'metatext'):
                # 压缩形式下 metatext 还要带一段未压缩副本
                body += bytes([0x02]) + name + ZERO + len(payload).to_bytes(4, 'little') + \
                    payload + ZERO
            changed += 1
        span = 4 + len(body)
        out += span.to_bytes(4, 'little') + body
    out += data[last_bound:]      # 附件 blob + 尾部段
    with open(dst, 'wb') as f:
        f.write(out)
    if verbose:
        print('压缩化 %s -> %s' % (os.path.basename(src), os.path.basename(dst)))
        print('  条目 %d 个，其中 %d 个由不压缩改为压缩；%d -> %d 字节'
              % (len(ents), changed, len(raw), len(out)))
    return bytes(out), changed


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    compress_file(sys.argv[1], sys.argv[2])
