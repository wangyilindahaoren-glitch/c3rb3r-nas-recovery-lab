#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规划 74 个 .osheet 的恢复来源，并做交叉验证（雕刻原件 vs Drive 重建）。

优先级：雕刻（字节级精确）> Drive 重建
交叉验证：两个来源都有 → 比 SHA-256，验证 Drive 重建法在真实数据上是否逐字节正确。
"""
import io
import json
import os
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 1) 密文清单
cipher = {}
for line in io.open('_osheet_len_map.tsv', encoding='utf-8', errors='replace'):
    orig, tag, path = line.rstrip('\n').split('\t')
    cipher[tag] = {'orig_len': int(orig), 'path': path, 'tag': tag}

# 2) 雕刻认领到的（文件名里带 tag 序号前缀）
carved = {}
CDIR = 'claim/claim_all'
if os.path.isdir(CDIR):
    for fn in os.listdir(CDIR):
        if not fn.endswith('.osheet'):
            continue
        num = fn.split('_')[1]        # 001
        for tag in cipher:
            if tag.startswith(num + '_'):
                carved[tag] = os.path.join(CDIR, fn)

# 3) Drive 映射
dmap = {d['tag']: d for d in json.load(io.open('_drive_map.json', encoding='utf-8'))}

rows = []
for tag, c in sorted(cipher.items()):
    d = dmap.get(tag)
    rows.append({
        'tag': tag, 'orig_len': c['orig_len'], 'cipher': c['path'],
        'carved': carved.get(tag),
        'repo': d['repo'] if d else None,
        'metatext': d['metatext'] if d else None,
    })

has_c = sum(1 for r in rows if r['carved'])
has_d = sum(1 for r in rows if r['repo'])
both = sum(1 for r in rows if r['carved'] and r['repo'])
only_c = sum(1 for r in rows if r['carved'] and not r['repo'])
only_d = sum(1 for r in rows if not r['carved'] and r['repo'])
neither = sum(1 for r in rows if not r['carved'] and not r['repo'])
print('共 %d 个 .osheet 密文' % len(rows))
print('  雕刻命中      : %d' % has_c)
print('  Drive 可重建  : %d' % has_d)
print('  两者都有(可交叉验证): %d' % both)
print('  只有雕刻      : %d' % only_c)
print('  只有 Drive    : %d' % only_d)
print('  都拿不到      : %d' % neither)
print()
print('== 两者都有的（交叉验证用）==')
for r in rows:
    if r['carved'] and r['repo']:
        print('  %-9d %-42s metatext=%s' % (r['orig_len'], r['repo'], r['metatext']))
print()
print('== 都拿不到的 ==')
for r in rows:
    if not r['carved'] and not r['repo']:
        print('  %-9d %s' % (r['orig_len'], r['cipher']))
json.dump(rows, io.open('_batch_plan.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1)
