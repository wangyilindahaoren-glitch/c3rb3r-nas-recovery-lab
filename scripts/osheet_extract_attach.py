#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 .osheet 的附件数据区里把图片抠出来。

附件条目（text/attachment / @C_text/attachment）的载荷是一段 JSON 元数据
（每个附件的 name/ext/fullname/size/thumb/thumb_size...），
真正的图片二进制在条目链之外的附件数据区里，按图片自身的头尾标记切分，
再用元数据里的 size / thumb_size 对号入座。

用法：python3 osheet_extract_attach.py <osheet> <输出目录>
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from osheet_repair import prescan, parse_header, find_entries, find_footer

PNG_HEAD = b'\x89PNG\r\n\x1a\n'
PNG_TAIL = b'IEND\xaeB`\x82'
JPG_HEAD = b'\xff\xd8\xff'
JPG_TAIL = b'\xff\xd9'


def carve(data, head, tail):
    """切出所有 head 开头、tail 结尾的片段"""
    out = []
    idx = 0
    while True:
        i = data.find(head, idx)
        if i < 0:
            break
        j = data.find(tail, i)
        if j < 0:
            break
        out.append((i, j + len(tail), data[i:j + len(tail)]))
        idx = j + len(tail)
    return out


def main(src, outdir):
    raw = open(src, 'rb').read()
    data, _ = prescan(raw)
    foot = find_footer(data)
    hdr, first, err = parse_header(data)
    if err:
        raise SystemExit('头部解析失败: %s' % err)
    ents = find_entries(data, first, foot)
    att = [e for e in ents if b'attachment' in e[2]]
    if not att:
        raise SystemExit('没有附件条目')
    e = att[-1]
    meta = json.loads(data[e[4]:e[4] + e[5]].rstrip(b'\x00').decode('utf-8'))
    # 附件数据区：附件条目之后
    end = e[0] + int.from_bytes(data[e[0]:e[0] + 4], 'little')
    blob = data[end:]
    print('元数据 %d 个附件；附件数据区 %d 字节' % (len(meta), len(blob)))

    pieces = {}
    for i, (a, b, buf) in enumerate(carve(blob, PNG_HEAD, PNG_TAIL)):
        pieces.setdefault(len(buf), []).append(('png', buf))
    for i, (a, b, buf) in enumerate(carve(blob, JPG_HEAD, JPG_TAIL)):
        pieces.setdefault(len(buf), []).append(('jpg', buf))

    os.makedirs(outdir, exist_ok=True)
    used = {}
    n = 0
    missing = 0
    for key, info in meta.items():
        for kind, size_field, name_field in (('main', 'size', 'fullname'),
                                             ('thumb', 'thumb_size', 'thumb')):
            sz = info.get(size_field)
            if not sz:
                continue
            cand = pieces.get(sz)
            if not cand:
                missing += 1
                continue
            ext, buf = cand.pop(0)
            base = info.get(name_field) or info.get('fullname') or key
            ext2 = info.get('ext') or ext
            if not base.lower().endswith('.' + ext2.lower()):
                base = base + '.' + ext2
            fname = '%02d_%s' % (n, base)
            if kind == 'thumb':
                fname = fname.replace('.', '_缩略.', 1)
            with open(os.path.join(outdir, fname), 'wb') as f:
                f.write(buf)
            n += 1
    print('抠出 %d 个文件；元数据里对不上大小的 %d 个' % (n, missing))
    print('输出目录: %s' % outdir)


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2])
