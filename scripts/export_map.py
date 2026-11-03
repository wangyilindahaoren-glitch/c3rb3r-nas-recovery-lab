#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出「.osheet 密文 → Drive 仓库」映射表，供批量重建用。"""
import io
import os
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rebuild_exact as RE   # noqa: E402
import base64
import json


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
        return base64.b64decode(s).decode('utf-8', 'replace')
    except Exception:
        return s


_tpl = open('claim3/target_2019年产品采购记录.osheet', 'rb').read()
TPL_HDR = RE.split_header(_tpl)[1]
_i = _tpl.find(b'\x02id\x00')
TPL_IDLEN = int.from_bytes(_tpl[_i + 4:_i + 8], 'little')

repos = defaultdict(list)
for line in io.open('drive/drive_index.tsv', encoding='utf-8', errors='replace'):
    p = line.rstrip('\n').split('\t')
    if len(p) < 4:
        continue
    repos[p[0]].append((b64name(p[2]), int(p[3])))

exp = {}
for base, ents in repos.items():
    total = (TPL_HDR - TPL_IDLEN + len(base) + 1) + 11
    for name, size in ents:
        if name == 'read-me3.txt':
            continue
        full = name if name in ('metatext', 'metabinairy_files') else ('text/' + name)
        total += 12 + len(full) + size
    exp[base] = (total + 32, total + 129)     # metatext 短版 / 长版条目

cands = defaultdict(list)
for line in io.open('_osheet_len_map.tsv', encoding='utf-8', errors='replace'):
    orig, tag, path = line.rstrip('\n').split('\t')
    cands[int(orig)].append((tag, path))

out = []
amb = 0
for orig in sorted(cands):
    m = [b for b, e in exp.items() if orig in e]
    if len(m) != 1:
        amb += 1
        continue
    b = m[0]
    which = 'short' if exp[b][0] == orig else 'long'
    for tag, path in cands[orig]:
        out.append({'orig_len': orig, 'repo': b, 'metatext': which,
                    'cipher': path, 'tag': tag})

with io.open('_drive_map.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print('导出 %d 条映射（歧义跳过 %d 个长度）' % (len(out), amb))
print('需要拉取的 Drive 仓库数: %d' % len(set(o['repo'] for o in out)))
