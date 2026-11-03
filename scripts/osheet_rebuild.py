#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重建 .osheet 容器：修正被推断搞错的长度字段，其余字节原样保留。

背景：路线 A 推测出来的字节，头部与条目长度字段可能错一两个字节，
      WPS 解析头部就失败 → 报"无效文件格式"，文件根本打不开。

文法（已用正常文件反推确认）：
    文件 := magic(4) + 头部TLV + 条目* + 尾部段
    头部 := [u8 tag][key\\0][值]      tag=0x08 用 u8 长度，其余用 u32 长度
    条目 := [u32 X][u8 0x05][name\\0][u32 len][u8 0x00][zlib(len字节)][尾随原始字节]
            X = 到下一个条目（或尾部段）的距离
    尾部 := [u32 总长][u8 0x08]["end\\0"][值]

做法：
  1. 以一个结构正常的 .osheet 作模板，取它的头部与尾部字节；
  2. 从待修文件里取 id / ver 两个字段（长度一致可原样搬）；
  3. 用条目名当锚点定位每个条目，用 zlib 自终止确定压缩数据的真实范围；
  4. 只重写 X 与 len 两个字段，其余字节逐字节照抄；
  5. 用严格解析器复核（grammar4.py）。

用法：python3 osheet_rebuild.py <模板正常件> <待修文件> <输出文件>
"""
import os
import re
import sys
import zlib

MAGIC = b'\x78\x00\x00\x00'
NAME_RE = re.compile(rb'@C_[A-Za-z0-9_/]+|text/[A-Za-z0-9_]+')
ZLIB_HEADS = (b'\x78\x9c', b'\x78\xda', b'\x78\x01')
FOOT_RE = re.compile(rb'\x08end\x00')


def zlib_span(data, start):
    d = zlib.decompressobj()
    try:
        out = d.decompress(data[start:])
        out += d.flush()
    except Exception:
        return None, None
    if not d.eof:
        return None, None
    consumed = len(data) - start - len(d.unused_data)
    return out, start + consumed


def find_entries(data):
    """按顺序找出条目锚点：[(entry_start, name, data_start, data_end)]

    两种条目：
      tag 0x05 —— [u32 len][u8 0x00][zlib(len字节)]      （压缩）
      tag 0x02 —— [u32 len][raw(len字节)]                 （不压缩）
    压缩数据用 zlib 自终止确定真实结束位置；raw 直接用 len 字段。
    """
    anchors = []
    for m in NAME_RE.finditer(data):
        s = m.start()
        if s < 5:
            continue
        tag = data[s - 1]
        if tag not in (0x05, 0x02):
            continue                      # 名字前面必须是条目 tag
        p = m.end() + 1                  # 跳过名字后的 NUL
        if p + 4 > len(data):
            continue
        ln = int.from_bytes(data[p:p + 4], 'little')
        base = p + 4
        hit = None
        if tag == 0x05:
            for off in (0, 1):
                q = base + off
                if q + 2 > len(data):
                    break
                if data[q:q + 2] in ZLIB_HEADS:
                    payload, end = zlib_span(data, q)
                    if payload is not None:
                        hit = (q, end)
                        break
        else:
            q = base
            if q + ln <= len(data):
                hit = (q, q + ln)
        if hit is None:
            continue
        anchors.append((s - 5, m.group(0).decode('latin1'), hit[0], hit[1]))
    # 去重 + 按位置排序
    anchors.sort()
    out = []
    seen = set()
    for a in anchors:
        k = (a[0], a[2])
        if k in seen:
            continue
        seen.add(k)
        out.append(a)
    return out


def find_footer(data, after):
    """找尾部段起点： [u32 总长][0x08]["end\\0"]..."""
    for m in FOOT_RE.finditer(data):
        if m.start() < after:
            continue
        st = m.start() - 4
        if st < 0:
            continue
        ln = int.from_bytes(data[st:st + 4], 'little')
        if ln == len(data) - st:
            return st
    return None


def split_template(data):
    ents = find_entries(data)
    if not ents:
        raise ValueError('模板里找不到条目')
    head = data[:ents[0][0]]
    last = ents[-1]
    foot_start = find_footer(data, last[3])
    foot = data[foot_start:] if foot_start is not None else b''
    return head, foot


def extract_field(data, key):
    m = re.compile(re.escape(key) + rb'\x00').search(data[:512])
    if not m:
        return None
    p = m.end()
    ln = int.from_bytes(data[p:p + 4], 'little')
    return data[m.start() - 1:m.end()] + data[p:p + 4] + data[p + 4:p + 4 + ln]


def rebuild(tpl_path, src_path, dst_path, verbose=True):
    tpl = open(tpl_path, 'rb').read()
    src = open(src_path, 'rb').read()
    head_tpl, foot_tpl = split_template(tpl)
    m = re.search(rb'\x02id\x00', head_tpl)
    n = re.search(rb'\x02ver\x00', head_tpl)
    if not m or not n:
        raise ValueError('模板头部结构不认识')
    head_fixed = head_tpl[:m.start()]
    ln_ver = int.from_bytes(head_tpl[n.end():n.end() + 4], 'little')
    tail_sep = head_tpl[n.end() + 4 + ln_ver:]
    id_src = extract_field(src, b'id')
    ver_src = extract_field(src, b'ver')
    if id_src is None or ver_src is None:
        raise ValueError('待修文件里找不到 id/ver')
    head = head_fixed + id_src + ver_src + tail_sep

    ents = find_entries(src)
    if not ents:
        raise ValueError('待修文件里扫不到条目')
    # 每个条目的结束位置 = 下一个条目的起点；最后一个 = 尾部段起点
    bounds = []
    for i, e in enumerate(ents):
        if i + 1 < len(ents):
            end = ents[i + 1][0]
        else:
            fs = find_footer(src, e[3])
            end = fs if fs is not None else len(src)
        bounds.append(end)
    body = b''
    for i, (es, name, zs, ze) in enumerate(ents):
        nb = name.encode('latin1')
        raw = src[zs:ze]
        trail = src[ze:bounds[i]]              # 数据之后、下一条目之前的原始字节
        tag = src[es + 4]
        pre = b'\x00' if tag == 0x05 else b''   # 压缩条目在 len 之后有一个 0x00
        rec_len = 4 + 1 + len(nb) + 1 + 4 + len(pre) + len(raw) + len(trail)
        body += rec_len.to_bytes(4, 'little') + bytes([tag]) + nb + b'\x00' + \
            len(raw).to_bytes(4, 'little') + pre + raw + trail
    out = head + body + foot_tpl
    with open(dst_path, 'wb') as f:
        f.write(out)
    if verbose:
        print('重建 %-42s -> %-30s %d 字节，条目 %d' % (
            os.path.basename(src), os.path.basename(dst), len(out), len(ents)))
    return out, ents


if __name__ == '__main__':
    tpl, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
    rebuild(tpl, src, dst)
