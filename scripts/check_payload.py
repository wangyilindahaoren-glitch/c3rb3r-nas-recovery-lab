#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 .osheet 各条目的载荷内容：是不是合法 JSON、有没有真业务数据。"""
import json
import sys
import zlib

sys.path.insert(0, '/tmp')
from osheet_check import find_entries, parse_header, MAGIC


def payload_of(data, ent):
    es, tag, name, ln, ps, pl = ent
    if ps is None:
        return None
    raw = data[ps:ps + pl]
    if tag == 0x05:
        try:
            return zlib.decompress(raw)
        except Exception as ex:
            return b'<<zlib fail: %s>>' % str(ex).encode()
    return raw


def check(path):
    data = open(path, 'rb').read()
    print('#' * 72)
    print('# %s  (%d 字节)' % (path.split('/')[-1], len(data)))
    hdr, pos, err = parse_header(data)
    if err:
        print('  头部错误: %s' % err)
        return
    print('  头部: %s' % [(k, v[:24]) for _t, k, v in hdr])
    ents = find_entries(data, pos)
    for ent in ents:
        es, tag, name, ln, ps, pl = ent
        nm = name.decode('latin1')
        pay = payload_of(data, ent)
        if pay is None:
            print('  %-22s payload 定位失败' % nm)
            continue
        s = pay.decode('utf-8', 'replace')
        head = s[:150].replace('\n', ' ')
        info = ''
        if nm.endswith('index') or '/sh_' in nm or nm == 'text/sh_1':
            try:
                j = json.loads(s.rstrip('\x00'))
                if isinstance(j, dict):
                    bits = []
                    if 'cells' in j:
                        cells = j['cells']
                        nr = len(cells)
                        nc = max((len(v) for v in cells.values()), default=0)
                        bits.append('cells %d行 x %d列' % (nr, nc))
                    for k in ('rowCount', 'colCount'):
                        if k in j:
                            bits.append('%s=%s' % (k, j[k]))
                    if 'order' in j:
                        bits.append('order=%s' % (j['order'][:6],))
                    if 'sheets' in j:
                        bits.append('sheets=%s' % list(j['sheets'].items())[:4])
                    info = ' | ' + ' '.join(bits)
            except Exception as ex:
                info = ' | JSON 解析失败: %s' % str(ex)[:60]
        print('  %-22s len=%-8d 载荷=%-8d %s' % (nm, ln, len(pay), head[:110]))
        if info:
            print('        %s' % info)


if __name__ == '__main__':
    for p in sys.argv[1:]:
        check(p)
