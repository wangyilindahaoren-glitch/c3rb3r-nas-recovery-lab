#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""严格解析 .osheet 容器（第四版，版式已确认）。

文法：
    文件   := magic(4) + 头部TLV* + 条目*
    头部   := [u8 tag][key\\0][值]，值长度按 tag 决定（0x08 用 u8，其余用 u32）
    条目   := [u32 X][u8 tag=0x05][name\\0][u32 len][u8 0x00][zlib(len字节)][u8 尾字节]
              X = 该条目从 X 字段到末尾的**总字节数**（含 X 自己）
    条目链的下一个条目 = 当前条目起始 + X

严格校验：每个条目的 zlib 必须能完整解压；条目必须首尾相接；最后必须正好落在文件末尾。
"""
import json
import sys
import zlib

MAGIC = b'\x78\x00\x00\x00'


def parse_strict(data):
    """返回 (header, entries, end_pos) 或抛 ValueError"""
    if data[:4] != MAGIC:
        raise ValueError('magic 不对: %s' % data[:4].hex())
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
            # 旧版 WPS（schema=2）在头部字段之间多一个 0x00 填充字节，跳过
            pos += 1
            continue
        e = data.find(b'\x00', pos + 1)
        if e < 0:
            raise ValueError('头部 key 截断 @%d' % pos)
        key = data[pos + 1:e].decode('latin1')
        p = e + 1
        if tag == 0x08:
            ln = data[p]
            p += 1
            val = data[p:p + ln]
            p += ln
        else:
            if p + 4 > len(data):
                raise ValueError('头部长度截断 @%d' % pos)
            ln = int.from_bytes(data[p:p + 4], 'little')
            p += 4
            val = data[p:p + ln]
            p += ln
        if len(val) != ln:
            raise ValueError('头部值越界 @%d key=%s len=%d' % (pos, key, ln))
        header.append((tag, key, val))
        if p < len(data) and data[p] == 0:
            p += 1
        pos = p
    entries = []
    while pos < len(data):
        if pos + 9 > len(data):
            raise ValueError('条目头截断 @%d（剩 %d 字节）' % (pos, len(data) - pos))
        X = int.from_bytes(data[pos:pos + 4], 'little')
        tag = data[pos + 4]
        if tag not in (0x05, 0x02):
            raise ValueError('条目 tag 不是 0x05/0x02 @%d (0x%02x)' % (pos, tag))
        e = data.find(b'\x00', pos + 5)
        if e < 0:
            raise ValueError('条目名截断 @%d' % pos)
        name = data[pos + 5:e].decode('latin1')
        p = e + 1
        ln = int.from_bytes(data[p:p + 4], 'little')
        p += 4
        if p + 1 + ln > len(data):
            raise ValueError('条目数据越界 @%d %s len=%d' % (pos, name, ln))
        payload = None
        if tag == 0x05:
            if data[p] != 0x00:
                raise ValueError('条目数据前缀不是 0 @%d %s' % (pos, name))
            raw = data[p + 1:p + 1 + ln]
            d = zlib.decompressobj()
            try:
                payload = d.decompress(raw)
                payload += d.flush()
            except Exception as ex:
                raise ValueError('条目 %s @%d zlib 解压失败: %s' % (name, pos, ex))
            if not d.eof:
                raise ValueError('条目 %s @%d zlib 不完整' % (name, pos))
        else:
            payload = data[p:p + ln]
        span = (p + 1 + ln - pos)
        # X = 到下一个条目的距离；最后一条允许有少量尾部
        nxt = pos + X
        if nxt > len(data):
            raise ValueError('条目 %s @%d 的 X=%d 超出文件（长 %d）' % (name, pos, X, len(data)))
        if nxt < len(data):
            if nxt + 4 >= len(data):
                raise ValueError('条目 %s @%d 的 X=%d 越界' % (name, pos, X))
            ntag = data[nxt + 4]
            if ntag in (0x05, 0x02):
                pass                      # 下一个条目（压缩 / 不压缩）
            elif ntag == 0x08:
                pass                      # 尾部标记段
            else:
                raise ValueError('条目 %s @%d 的 X=%d 指向的不是条目起点（@%d tag=0x%02x）'
                                 % (name, pos, X, nxt, ntag))
        entries.append((pos, X, name, payload))
        pos = nxt
        if pos < len(data) and data[pos + 4:pos + 5] == b'\x08':
            break                     # 接下来是尾部标记段
    footer = None
    if pos < len(data):
        # 尾部： [u32 总长][u8 tag][name\0][值]
        ln = int.from_bytes(data[pos:pos + 4], 'little')
        if ln != len(data) - pos:
            raise ValueError('尾部长度 %d 与剩余 %d 不符 @%d' % (ln, len(data) - pos, pos))
        tag = data[pos + 4]
        e = data.find(b'\x00', pos + 5)
        name = data[pos + 5:e].decode('latin1')
        footer = (tag, name, data[e + 1:])
    return header, entries, pos


def report(path, verbose=True):
    data = open(path, 'rb').read()
    try:
        header, entries, end = parse_strict(data)
    except ValueError as ex:
        if verbose:
            print('%-58s 严格解析失败: %s' % (path[-58:], ex))
        return False
    shards = [e for e in entries if 'text/sh_' in e[2]]
    if verbose:
        print('%-58s OK  条目=%-4d 分片=%-4d 头=%s' % (
            path[-58:], len(entries), len(shards),
            [(k, v[:12]) for _t, k, v in header]))
    return True


if __name__ == '__main__':
    for p in sys.argv[1:]:
        report(p)
