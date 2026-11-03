#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Drive 索引算每个文档重建后的容器长度，与全盘 .osheet 密文的 orig_len 对账。

容器长度 = 头部 + Σ条目 + 尾段(11)
  条目 = 12 + len(name) + len(data)          （payload = data + 1 个 NUL）
  头部 = 实测值（随 id 长度线性变化）
metatext 两种形态：短版 12 字节 / 长版 109 字节
"""
import base64
import io
import os
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rebuild_exact as RE   # noqa: E402


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


# --- 实测表头长度：以一个已知原件为模板，量出 idlen=35 时的头部长度 ---
TPL = 'claim3/target_2019年产品采购记录.osheet'
_tpl_raw = open(TPL, 'rb').read()
TPL_HDR = RE.split_header(_tpl_raw)[1]
_i = _tpl_raw.find(b'\x02id\x00')
TPL_IDLEN = int.from_bytes(_tpl_raw[_i + 4:_i + 8], 'little')
print('模板表头长度 = %d (idlen=%d)  →  头部 = %d + idlen - %d'
      % (TPL_HDR, TPL_IDLEN, TPL_HDR, TPL_IDLEN))


def hdr_len(idlen):
    return TPL_HDR - TPL_IDLEN + idlen


repos = defaultdict(list)
for line in io.open('drive/drive_index.tsv', encoding='utf-8', errors='replace'):
    p = line.rstrip('\n').split('\t')
    if len(p) < 4:
        continue
    base, ver, fn, size = p[0], p[1], p[2], int(p[3])
    repos[base].append((b64name(fn), size))

exp = {}
for base, ents in repos.items():
    idlen = len(base) + 1          # id 值 = "<base>.sh" + NUL
    total = hdr_len(idlen) + 11
    for name, size in ents:
        if name == 'read-me3.txt':
            continue
        full = name if name in ('metatext', 'metabinairy_files') else ('text/' + name)
        total += 12 + len(full) + size
    # metatext 条目本身也要算封装: 12 + len('metatext'=8) + len(载荷)
    #   短版载荷 12 字节 → 条目 32；长版载荷 109 字节 → 条目 129
    exp[base] = (total + 12 + 8 + 12, total + 12 + 8 + 109)

# --- 自检：用「已知对」验证公式 ---
# 已知对 = {Drive 仓库目录名: 该文档被加密前的 orig_len}
# 这份对照表与具体 NAS 强相关（仓库名带作者 uid），所以**不内置**：
# 用 --known <json> 传入，或放同目录的 known_pairs.json。
# 没有对照表时跳过自检（公式是否正确要靠你自己的样本验证）。
KNOWN = {}
_known_file = (sys.argv[sys.argv.index('--known') + 1]
               if '--known' in sys.argv else 'known_pairs.json')
try:
    if os.path.isfile(_known_file):
        import json as _json
        with open(_known_file, 'r', encoding='utf-8') as _f:
            KNOWN = _json.load(_f)
except Exception as e:                                    # noqa: BLE001
    print('  [警告] 读取已知对文件失败: %s' % e)

if not KNOWN:
    print('\n== 公式自检：跳过（没有已知对对照表）==')
    print('   用 --known <json> 传入 {"<仓库名>.sh": orig_len, ...} 后可自动校验长度公式')
bad = 0
for b, o in KNOWN.items():
    if b not in exp:
        print('  %-42s 不在索引里' % b)
        bad += 1
        continue
    s, l = exp[b]
    ok = (o == s or o == l)
    if not ok:
        bad += 1
    print('  %-42s orig=%-9d 短=%-9d 长=%-9d %s'
          % (b, o, s, l, '✓' if ok else '✗ 差%+d' % (s - o)))
print('自检失败 %d 个' % bad)

cands = defaultdict(list)
for line in io.open('_osheet_len_map.tsv', encoding='utf-8', errors='replace'):
    orig, tag, path = line.rstrip('\n').split('\t')
    cands[int(orig)].append((tag, path))

print('\n== .osheet 密文 → Drive 仓库 匹配 ==')
hit = uniq = miss = 0
for orig in sorted(cands):
    m = [b for b, e in exp.items() if orig in e]
    if not m:
        miss += 1
        continue
    hit += 1
    if len(m) == 1:
        uniq += 1
        which = '短' if exp[m[0]][0] == orig else '长'
        print('  ✓ orig=%-9d → %-42s metatext=%s版' % (orig, m[0], which))
    else:
        print('  ? orig=%-9d → %d 个候选: %s' % (orig, len(m), m))
print('\n对上 %d / %d（唯一 %d），对不上 %d' % (hit, len(cands), uniq, miss))
