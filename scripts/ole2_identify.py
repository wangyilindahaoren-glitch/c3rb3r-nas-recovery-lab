#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ole2_identify.py —— 按**正文内容**认领 OLE2(.xls/.doc) 原件（路线 D 专用）

为什么
----
OLE2 没有结尾签名 → 雕刻时只能按魔数找到候选。**长度只用来筛候选，正文才是判据。**
2026-11-02 实测：修正长度算法后，12 个 .xls/.doc 目标里 4 个 .doc 正文 100% 认准。

用法
----
  # 1) 造候选表：从雕刻日志里取 OLE2 魔数命中点
  python3 ole2_identify.py offsets <雕刻日志目录>            # 打印所有命中偏移

  # 2) 算长度 + 筛出接近目标长度的候选（阶段1，只读头 + FAT，1909 个点约 14 秒）
  python3 ole2_identify.py screen <候选表> --targets <targets.tsv> --out near.json

  # 3) 读正文并按关键词判定（阶段2）
  python3 ole2_identify.py identify <near.json> --targets <targets.tsv> \
          --words 结算单 <公司甲> <公司乙> <公司丙> --out content.json

targets.tsv 每行： <原始长度>\t<扩展名>\t<相对路径>
候选表每行：     <设备物理偏移>
"""
import json
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ole2_lib import (Ole, biff_sheets, biff_sst, doc_cjk, rd, text_hits,
                       context)

DEV = os.environ.get('C3R_DEV', '/dev/mapper/cachedev_0')
OLE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'


def load_targets(path):
    """-> {长度: [(ext, rel), ...]}"""
    out = {}
    for ln in open(path, encoding='utf-8'):
        ln = ln.rstrip('\n')
        if not ln:
            continue
        parts = ln.split('\t')
        if len(parts) < 3:
            continue
        sz, ext, rel = parts[0], parts[1], parts[2]
        out.setdefault(int(sz), []).append((ext, rel))
    return out


def cmd_offsets(logdir):
    """从雕刻日志里收集所有 OLE2 魔数命中的物理偏移"""
    offs = set()
    for fn in sorted(os.listdir(logdir)):
        if not fn.endswith('.log'):
            continue
        for ln in open(os.path.join(logdir, fn), encoding='utf-8', errors='replace'):
            p = ln.rstrip('\n').split('\t')
            if len(p) >= 2 and p[0] == 'ole':
                try:
                    offs.add(int(p[1]))
                except ValueError:
                    pass
    for o in sorted(offs):
        print(o)


def cmd_screen(offlist, targets_tsv, out, window=8192):
    """阶段1：只读头 + FAT，算真实分配长度，筛出长度接近目标的候选"""
    targets = load_targets(targets_tsv)
    tl = sorted(targets)
    print('目标长度 %d 种: %s' % (len(tl), tl))
    offs = [int(x) for x in open(offlist, encoding='utf-8') if x.strip()]
    print('候选点 %d 个' % len(offs))
    near = []
    for k, off in enumerate(offs):
        try:
            o = Ole(off, DEV)
        except Exception:
            continue
        al = o.alloc_len()
        hit = [t for t in tl if abs(t - al) <= window]
        if hit:
            near.append({'off': off, 'len': al, 'near': hit})
        if k % 200 == 0:
            print('  %d/%d' % (k, len(offs)))
            sys.stdout.flush()
    json.dump(near, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('长度接近的候选 %d 个 -> %s' % (len(near), out))


def cmd_identify(near_json, targets_tsv, words, out):
    """阶段2：读正文，按关键词判定"""
    targets = load_targets(targets_tsv)
    near = json.load(open(near_json, encoding='utf-8'))
    res = []
    for c in near:
        off = c['off']
        try:
            o = Ole(off, DEV)
            buf = rd(off, o.alloc_len(), DEV)
        except Exception:
            continue
        names = o.names()
        isdoc = 'WordDocument' in names
        sheets, sst, texts = [], [], []
        try:
            if isdoc:
                wd = o.stream('WordDocument')
                texts = doc_cjk(wd)
                blob = ' '.join(texts)
            else:
                wb = o.stream('Workbook') or o.stream('Book')
                sheets = biff_sheets(wb)
                sst = biff_sst(wb)
                blob = ' '.join(sst) + ' ' + wb.decode('gbk', 'ignore')
        except Exception:
            blob = ''
        kw = [w for w in words if w in blob]
        raw_kw = text_hits(buf, words)
        rec = {'off': off, 'len': c['len'], 'near': c['near'], 'doc': isdoc,
               'names': names[:8], 'sheets': sheets[:8], 'kw': kw,
               'raw_kw': [(w, e) for w, e, _ in raw_kw],
               'text': texts[:20], 'sst': sst[:20]}
        res.append(rec)
        if kw or raw_kw:
            print('== off=%-14d len=%-8d near=%s %s' % (off, c['len'], c['near'],
                                                       'DOC' if isdoc else 'XLS'))
            if sheets:
                print('   表名: %s' % (sheets[:6],))
            if kw:
                print('   关键词: %s' % kw)
            if texts:
                print('   正文: %s' % (' | '.join(texts[:10])[:300],))
            if sst:
                print('   SST: %s' % (' | '.join(sst[:10])[:260],))
            sys.stdout.flush()
    json.dump(res, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('共 %d 个候选 -> %s' % (len(res), out))


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == 'offsets':
        cmd_offsets(sys.argv[2])
    elif cmd == 'screen':
        args = sys.argv[2:]
        offlist = args[0]
        targets = args[args.index('--targets') + 1]
        out = args[args.index('--out') + 1]
        win = int(args[args.index('--window') + 1]) if '--window' in args else 8192
        cmd_screen(offlist, targets, out, win)
    elif cmd == 'identify':
        args = sys.argv[2:]
        near = args[0]
        targets = args[args.index('--targets') + 1]
        out = args[args.index('--out') + 1]
        words = args[args.index('--words') + 1].split(',') if '--words' in args else []
        cmd_identify(near, targets, words, out)
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
