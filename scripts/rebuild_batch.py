#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量 Drive 重建 + 与雕刻原件交叉验证。

交叉验证是这轮的关键：20 个文件两条路都有，
用雕刻原件（字节级精确）当 ground truth，检验 Drive 重建是否逐字节正确。
"""
import io
import json
import os
import shutil
import sys
import hashlib

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rebuild_exact as RE   # noqa: E402

DRIVE_ROOT = 'drive/x56/drive56'
OUT = 'rebuild56'
TPL = 'claim3/target_2019年产品采购记录.osheet'
META_LONG = None      # 长版 metatext 从密文明文区抠（有则用）


def sha(b):
    return hashlib.sha256(b).hexdigest()


def main():
    plan = json.load(io.open('_batch_plan.json', encoding='utf-8'))
    os.makedirs(OUT, exist_ok=True)
    tmpl = open(TPL, 'rb').read()

    rebuilt = {}
    for r in plan:
        if not r['repo']:
            continue
        d = os.path.join(DRIVE_ROOT, r['repo'])
        textd = os.path.join(d, 'text')
        verp = os.path.join(d, 'master')
        if not os.path.isdir(textd):
            print('  !! 缺 Drive 数据', r['repo'])
            continue
        ver = io.open(verp, encoding='utf-8').read().strip()
        meta = None
        if r['metatext'] == 'long':
            meta = META_LONG
        data = RE.rebuild(textd, ver, tmpl, r['repo'], meta)
        rebuilt[r['tag']] = data

    print('== 重建 %d 个 Drive 文档 ==' % len(rebuilt))

    # 与雕刻原件交叉验证
    print('\n== 交叉验证：Drive 重建 vs 雕刻原件（ground truth）==')
    both = same = diff = 0
    for r in plan:
        if not (r['carved'] and r['tag'] in rebuilt):
            continue
        both += 1
        orig = open(r['carved'], 'rb').read()
        got = rebuilt[r['tag']]
        if orig == got:
            same += 1
        else:
            diff += 1
            n = min(len(orig), len(got))
            at = next((i for i in range(n) if orig[i] != got[i]), n)
            print('  ✗ %-9d %-42s 长度 %d vs %d  首个差异 @%d'
                  % (r['orig_len'], r['repo'], len(orig), len(got), at))
    print('  两路都有 %d 个 → 逐字节相同 %d， 不同 %d' % (both, same, diff))

    # 长度核对
    print('\n== 长度核对（重建长度 vs orig_len）==')
    okc = badc = 0
    for r in plan:
        if r['tag'] not in rebuilt:
            continue
        d = len(rebuilt[r['tag']])
        if d == r['orig_len']:
            okc += 1
        else:
            badc += 1
            print('  差 %+d : %-9d %-42s %s'
                  % (d - r['orig_len'], r['orig_len'], r['repo'], r['cipher']))
    print('  分毫不差 %d 个， 有差异 %d 个' % (okc, badc))

    with io.open('_rebuild56.json', 'w', encoding='utf-8') as f:
        json.dump({t: sha(v) for t, v in rebuilt.items()}, f, ensure_ascii=False, indent=1)
    # 落盘
    for r in plan:
        if r['tag'] in rebuilt:
            with io.open(os.path.join(OUT, 'target_%s.osheet' % r['tag'][:3]), 'wb') as f:
                f.write(rebuilt[r['tag']])


if __name__ == '__main__':
    main()
