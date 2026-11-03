#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cleanup_home.py —— 递归整理一个 home 目录，把恢复产物收成"每个文档一份"。

和 tidy_delivery.py 的区别：
  tidy_delivery.py  整理**一个**目录（单层）
  cleanup_home.py   递归整理**整个 home**（多层），按同一套规则逐目录执行

规则（与 tidy_delivery.py 一致）：
  - *.L0CK3D 密文**永远不删**（除非显式 --drop-cipher）
  - 同一文档同一扩展名有多个 → 留 mtime 最新的那份
  - 文件名带中间产物标记（_已修复 / _部分修复 / _Drive重建 / _重建 …）、
    含空格、或带 _1 / (1) 后缀 → 视为重名副本，删
  - read-me3.txt 勒索信默认保留（事件凭证）

安全设计：
  - **默认 --dry**，必须显式 --apply 才真删
  - 删除前可选复制到 --backup-dir（群晖上删除默认进回收站，等于双保险）
  - 跳过 @eaDir、#recycle、@sharesnap 等系统/快照目录

用法：
    python3 cleanup_home.py --home /volume1/homes/<user>            # 预览
    python3 cleanup_home.py --home /volume1/homes/<user> --apply \
            --backup-dir /volume1/@tmp/c3r_backup
"""
import argparse
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tidy_delivery import (  # noqa: E402  复用同一套判定规则，避免两处逻辑分叉
    DUP_MARKERS, SPACED, DUP_SUFFIX, split_name, is_dup_marker, listdir,
)

SKIP_DIR_NAMES = ('@eaDir', '#recycle', '@sharesnap', '.git', 'node_modules')


def parse_args():
    ap = argparse.ArgumentParser(description='递归整理 home 目录的恢复产物')
    ap.add_argument('--home', required=True, help='要整理的 home 根目录')
    ap.add_argument('--dry', action='store_true', help='只预览，不改动（默认行为）')
    ap.add_argument('--apply', action='store_true', help='真正执行删除')
    ap.add_argument('--drop-cipher', action='store_true',
                    help='连同 *.L0CK3D 密文一起删（默认保留密文）')
    ap.add_argument('--backup-dir', default='', help='删除前把要删的文件复制到这里')
    ap.add_argument('--report', default='', help='把报告写到这个文件')
    ap.add_argument('--keep-ext', default='L0CK3D', help='永远保留的扩展名（逗号分隔）')
    return ap.parse_args()


def plan_dir(target, keep_ext, drop_cipher):
    """算一个目录的删除/保留清单，返回 (plan_del, plan_keep)。"""
    groups = {}
    ciphers = []
    loose = []
    for name, path in listdir(target):
        doc, ext, cipher = split_name(name)
        if cipher:
            ciphers.append((name, path, os.path.getmtime(path)))
            continue
        if ext == '':
            loose.append((name, path, os.path.getmtime(path)))
            continue
        groups.setdefault(doc, {}).setdefault(ext, []).append(
            (name, path, os.path.getmtime(path)))

    plan_del, plan_keep = [], []

    for doc, exts in groups.items():
        if is_dup_marker(doc):
            for ext, items in exts.items():
                if ext in keep_ext and not drop_cipher:
                    plan_keep += items
                else:
                    plan_del += items
            continue
        for ext, items in exts.items():
            if ext in keep_ext and not drop_cipher:
                plan_keep += items
                continue
            items = sorted(items, key=lambda t: t[2])
            if len(items) == 1:
                plan_keep.append(items[0])
                continue
            plan_keep.append(items[-1])
            plan_del += items[:-1]

    # 密文单独一组，绝不与同名恢复件混在一起判重
    for item in ciphers:
        if drop_cipher:
            plan_del.append(item)
        else:
            plan_keep.append(item)

    # 勒索信默认保留
    for item in loose:
        plan_keep.append(item)

    return plan_del, plan_keep


def main():
    a = parse_args()
    home = a.home
    if not os.path.isdir(home):
        print('不是目录: %s' % home)
        return 1
    apply_ = a.apply and not a.dry
    keep_ext = set(x.strip() for x in a.keep_ext.split(',') if x.strip())
    backup = a.backup_dir or ''
    if backup:
        os.makedirs(backup, exist_ok=True)

    lines = []
    n_del = n_keep = 0

    for dirpath, dirnames, filenames in os.walk(home):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        if not filenames:
            continue
        plan_del, plan_keep = plan_dir(dirpath, keep_ext, a.drop_cipher)
        if not plan_del:
            n_keep += len(plan_keep)
            continue
        for name, path, _mtime in plan_del:
            rel = path[len(home):].lstrip('/') or name
            if apply_:
                if backup:
                    dst = os.path.join(backup, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    try:
                        shutil.copy2(path, dst)
                    except OSError:
                        pass
                try:
                    os.remove(path)
                except OSError as e:
                    lines.append('  [失败] %s: %s' % (rel, e))
                    continue
            lines.append('  删除 %s' % rel)
            n_del += 1
        n_keep += len(plan_keep)

    header = '=== cleanup_home %s ===' % ('执行' if apply_ else '预览（未改动）')
    out = [header, '待删 %d 个，保留 %d 个' % (n_del, n_keep)]
    out += lines
    text = '\n'.join(out)
    print(text)
    if a.report:
        with open(a.report, 'w', encoding='utf-8') as f:
            f.write(text + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
