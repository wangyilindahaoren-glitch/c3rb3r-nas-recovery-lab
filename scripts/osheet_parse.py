#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OSheet(.osheet) 容器解析（递归链式）。

实测版式：
    magic '78 00 00 00'
    头部若干 key/value（schema / enc / id / ver）
    条目链：[u32 X][u8 type][name\\0][payload]
        type=2 → payload 前带 u32 长度（裸文本/JSON）
        type=5 → payload 是 zlib 流（自带结束符）
    压缩条目的解压结果里往往还嵌着后续条目，所以要递归。

对外提供：
    parse(data)  -> {'magic':bool, 'header':{...}, 'entries':[(name, kind, payload)]}
"""
import json
import re
import zlib

MAGIC = b'\x78\x00\x00\x00'
NAME_RE = re.compile(rb'(?:@C_)?text/[A-Za-z0-9_]+')
ZLIB_HEADS = (b'\x78\x9c', b'\x78\xda', b'\x78\x01', b'\x78\x5e')


def _zlib_at(data, p):
    if data[p:p + 2] not in ZLIB_HEADS:
        return None
    try:
        d = zlib.decompressobj()
        out = d.decompress(data[p:])
        out += d.flush()
        if d.eof:
            return out
    except Exception:
        return None
    return None


def _raw_at(data, p):
    if p + 4 > len(data):
        return None
    L = int.from_bytes(data[p:p + 4], 'little')
    if L <= 0 or L > len(data) or p + 4 + L > len(data):
        return None
    return data[p + 4:p + 4 + L]


def _collect(data, out, seen, depth=0):
    if depth > 8:
        return
    for m in NAME_RE.finditer(data):
        name = m.group(0).decode('latin1')
        base = m.end()
        payload = None
        kind = None
        for off in range(0, 32):
            p = base + off
            if p + 2 > len(data):
                break
            z = _zlib_at(data, p)
            if z is not None and len(z) >= 8:
                payload, kind = z, 'zlib'
                break
        if payload is None:
            for off in range(0, 16):
                p = base + off
                r = _raw_at(data, p)
                if r is not None:
                    payload, kind = r, 'raw'
                    break
        if payload is None:
            continue
        h = hash(payload)
        key = (name, h)
        if key in seen:
            continue
        seen.add(key)
        out.append((name, kind, payload))
        _collect(payload, out, seen, depth + 1)


def parse(data):
    res = {'magic': data[:4] == MAGIC, 'header': {}, 'entries': []}
    if not res['magic']:
        return res
    for key in (b'schema', b'enc', b'id', b'ver'):
        i = data.find(key + b'\x00')
        if i < 0:
            continue
        p = i + len(key) + 1
        for off in range(0, 16):
            q = p + off
            if q + 4 > len(data):
                break
            L = int.from_bytes(data[q:q + 4], 'little')
            if 0 < L < 4096 and q + 4 + L <= len(data):
                v = data[q + 4:q + 4 + L].rstrip(b'\x00')
                try:
                    txt = v.decode('utf-8')
                except Exception:
                    continue
                if txt and all(32 <= ord(c) < 127 for c in txt):
                    res['header'][key.decode()] = txt
                    break
    out = []
    _collect(data, out, set())
    res['entries'] = out
    return res


def summarize(path, dumpdir=None):
    import os
    with open(path, 'rb') as f:
        data = f.read()
    r = parse(data)
    print('=' * 78)
    print('%s  (%d bytes)' % (path, len(data)))
    if not r['magic']:
        print('  不是 OSheet 容器')
        return r
    print('  头:', r['header'])
    if not r['entries']:
        print('  !! 没找到任何可解析条目')
        return r
    for name, kind, payload in r['entries'][:25]:
        try:
            txt = payload.decode('utf-8')
        except Exception:
            txt = repr(payload[:40])
        print('  %-22s %-5s len=%-9d %s' % (name, kind, len(payload),
                                             txt[:80].replace('\n', ' ')))
    if dumpdir:
        base = os.path.basename(path)
        for name, kind, payload in r['entries']:
            safe = name.replace('/', '_')
            with open(os.path.join(dumpdir, '%s.%s.bin' % (base, safe)), 'wb') as f:
                f.write(payload)
    return r


if __name__ == '__main__':
    import sys
    _dumpdir = None
    _args = sys.argv[1:]
    if _args and _args[0] == '--dump':
        _dumpdir = _args[1]
        _args = _args[2:]
    for _p in _args:
        summarize(_p, _dumpdir)
