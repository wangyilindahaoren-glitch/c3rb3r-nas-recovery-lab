#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OSheet 修复 + 数据提取 + 原位放回。

对每个路线 A 候选（.__prev__.osheet）：
  1) 与同名密文合并 —— 明文区间逐字节取自密文（本来就是原文），
     被加密的区间取自候选（推断值）；
  2) 解析合并后的容器（头部 + text/* 条目 + text/sh_N 分片 JSON）；
  3) 从分片 cells 抽出单元格，导出 CSV；
  4) 结构完好（index 在、分片都能解）才把合并件放回原路径。

用法：python3 osheet_fix.py [--apply]
"""
import csv
import hashlib
import json
import os
import shutil
import struct
import sys

TAIL = 588
STAMP = '勒索恢复_20261030'
# 要处理的 home 目录名。命令行给的用户名优先；这里只放占位示例，
# 实际使用请传参：python3 osheet_fix.py <用户1> <用户2> ...
USERS = []
DEC = json.JSONDecoder()

import os as _os
_p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'osheet_parse.py')
if not _os.path.exists(_p):
    _p = '/tmp/osheet_parse.py'
_G = {'__name__': 'osheet_parse_mod'}
exec(open(_p).read(), _G)
globals().update({k: v for k, v in _G.items() if not k.startswith('__')})


def tail_meta(p):
    sz = os.path.getsize(p)
    with open(p, 'rb') as f:
        f.seek(sz - TAIL)
        t = f.read(TAIL)
    if len(t) < TAIL:
        return None
    return t[572], t[573], struct.unpack('<Q', t[580:588])[0], sz


def enc_ranges(mode, n):
    if mode == 0x24:
        return [(0, n)]
    if mode == 0x26:
        return [(0, min(n, 1 << 20))]
    if mode == 0x25:
        w = 4 * (n // 100)
        gap = (n - 3 * w) // 2
        if w <= 0 or gap < 0:
            return [(0, n)]
        return [(0, w), (w + gap, 2 * w + gap), (2 * (w + gap), 3 * w + 2 * gap)]
    return None


def plain_ranges(mode, n):
    enc = enc_ranges(mode, n)
    if enc is None:
        return None
    out = []
    prev = 0
    for s, e in enc:
        if s > prev:
            out.append((prev, s))
        prev = max(prev, e)
    if prev < n:
        out.append((prev, n))
    return out


def copy_range(fin, fout, size):
    left = size
    while left > 0:
        b = fin.read(min(1 << 20, left))
        if not b:
            break
        fout.write(b)
        left -= len(b)


def build_merged(cand, enc, out):
    meta = tail_meta(enc)
    if not meta:
        return None
    mode, pct, _f, enc_size = meta
    n = enc_size - TAIL
    encr = enc_ranges(mode, n) or []
    csize = os.path.getsize(cand)
    with open(cand, 'rb') as fc, open(enc, 'rb') as fe, open(out, 'wb') as fo:
        pos = 0
        for s, e in encr:
            if s > pos:
                fc.seek(pos)
                copy_range(fc, fo, s - pos)
            fc.seek(s)
            copy_range(fc, fo, min(e, csize) - s)
            pos = min(e, csize)
        if pos < csize:
            fc.seek(pos)
            copy_range(fc, fo, csize - pos)
    return mode, n, csize


def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def cell_value(c):
    if isinstance(c, (str, int, float)):
        return str(c)
    if isinstance(c, dict):
        for k in ('v', 'value', 'text', 'd', 'f'):
            if k in c and isinstance(c[k], (str, int, float)):
                return str(c[k])
        return ''
    return ''


def extract_rows(payload):
    """从 shard JSON 抽 cells -> (rows_dict, colmax)"""
    s = payload.decode('utf-8', 'replace')
    try:
        obj, end = DEC.raw_decode(s)
    except Exception:
        return None
    cells = obj.get('cells')
    if not isinstance(cells, dict):
        return None
    rowmap = {}
    colmax = 0
    for rk, row in cells.items():
        try:
            ri = int(rk)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        vals = {}
        for ck, c in row.items():
            try:
                ci = int(ck)
            except Exception:
                continue
            vals[ci] = cell_value(c)
            colmax = max(colmax, ci)
        rowmap[ri] = vals
    if not rowmap:
        return None
    return rowmap, colmax


def write_csv(rowmap, colmax, path):
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        for ri in sorted(rowmap):
            vals = rowmap[ri]
            w.writerow([vals.get(ci, '') for ci in range(colmax + 1)])


def main():
    apply = '--apply' in sys.argv
    tmp = '/tmp/_osheet_fix'
    os.makedirs(tmp, exist_ok=True)
    report = []
    for u in USERS:
        home = '/volume1/homes/%s' % u
        rec = os.path.join(home, STAMP)
        srcdir = os.path.join(rec, '5_旧数据恢复')
        if not os.path.isdir(srcdir):
            continue
        for dp, dns, fns in os.walk(srcdir):
            for fn in sorted(fns):
                if not fn.endswith('.osheet'):
                    continue
                cand = os.path.join(dp, fn)
                stem, ext = os.path.splitext(fn)
                base = stem[:-len('.__prev__')] if stem.endswith('.__prev__') else stem
                orig_rel = os.path.join(os.path.dirname(os.path.relpath(cand, srcdir)), base + ext)
                enc = os.path.join(home, orig_rel + '.L0CK3D')
                merged = os.path.join(tmp, 'merged.osheet')
                info = {'user': u, 'rel': orig_rel}
                if not os.path.exists(enc):
                    info['status'] = 'no_cipher'
                    report.append(info)
                    print('%-14s %s' % (info['status'], orig_rel))
                    continue
                res = build_merged(cand, enc, merged)
                if not res:
                    info['status'] = 'no_tail'
                    report.append(info)
                    print('%-14s %s' % (info['status'], orig_rel))
                    continue
                mode, n, csize = res
                with open(merged, 'rb') as f:
                    data = f.read()
                r = parse(data)
                ents = r['entries']
                idx = None
                shards = {}
                for name, kind, payload in ents:
                    if name.endswith('text/index') and idx is None:
                        idx = payload
                    elif 'text/sh_' in name:
                        shards[name] = payload
                ok_shards = []
                rows_total = 0
                cdir = None
                for name, payload in shards.items():
                    got = extract_rows(payload)
                    if got:
                        ok_shards.append(name)
                        rows_total += len(got[0])
                info['mode'] = hex(mode)
                info['shards'] = len(shards)
                info['ok_shards'] = len(ok_shards)
                info['rows'] = rows_total
                info['index'] = bool(idx)
                if idx is None or not shards:
                    info['status'] = 'structure_bad'
                elif len(ok_shards) == len(shards):
                    info['status'] = 'structure_ok'
                elif ok_shards:
                    info['status'] = 'structure_partial'
                else:
                    info['status'] = 'structure_bad'
                # CSV 导出
                if ok_shards:
                    cdir = os.path.join(rec, '8_修复OSheet', base + '_CSV')
                    os.makedirs(cdir, exist_ok=True)
                    for name, payload in shards.items():
                        got = extract_rows(payload)
                        if not got:
                            continue
                        rowmap, colmax = got
                        fn_csv = name.replace('/', '_').replace('@C_', '') + '.csv'
                        write_csv(rowmap, colmax, os.path.join(cdir, fn_csv))
                    info['csv_dir'] = os.path.relpath(cdir, home)
                # 原位放回
                dst = os.path.join(home, orig_rel)
                if info['status'] == 'structure_ok' and apply:
                    if not os.path.exists(dst):
                        shutil.copyfile(merged, dst)
                        info['placed'] = os.path.relpath(dst, home)
                    else:
                        same = False
                        try:
                            same = os.path.getsize(dst) == os.path.getsize(merged) and \
                                   sha256(dst) == sha256(merged)
                        except OSError:
                            pass
                        if same:
                            info['placed'] = 'already_same'
                        else:
                            alt = os.path.join(home, os.path.dirname(orig_rel),
                                               base + '_修复' + ext)
                            if not os.path.exists(alt):
                                shutil.copyfile(merged, alt)
                                info['placed'] = os.path.relpath(alt, home)
                            else:
                                info['placed'] = None
                elif info['status'] == 'structure_partial' and apply:
                    alt = os.path.join(home, os.path.dirname(orig_rel),
                                       base + '_部分修复' + ext)
                    if not os.path.exists(alt):
                        shutil.copyfile(merged, alt)
                        info['placed'] = os.path.relpath(alt, home)
                    else:
                        info['placed'] = 'already_exists'
                report.append(info)
                print('%-16s %-46s mode=%s shard=%d/%d 行=%d %s' % (
                    info['status'], orig_rel, hex(mode), len(ok_shards), len(shards),
                    rows_total, ('放原位:' + str(info.get('placed'))) if info.get('placed') else ''))
    with open('/tmp/osheet_fix_report.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
