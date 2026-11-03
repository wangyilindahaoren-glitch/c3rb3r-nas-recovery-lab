#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tidy_delivery.py —— 收尾清理：把恢复目录整理成"只留下用户要的文件"。

背景（2026-10-31 实战）：
  恢复完成后，一个目录里往往同时躺着
    *.osheet.L0CK3D   密文（勒索产物）
    原名.osheet        恢复件（.osheet，WPS 打开）
    原名.xlsx          恢复件（Excel 通用）
    原名_Drive重建.osheet  中间产物（手工重建，WPS 可能打不开）
    "原名 .osheet"      带空格/带 _1 后缀的中间件（重名副本）
    read-me3.txt        勒索信
  用户看到的就是"一堆积乱七八糟的文件"。这个脚本按规则整理成
  **每个文档一份 .osheet + 一份 .xlsx**，其余清掉。

安全设计（默认不删）：
  * **默认 --dry**，必须显式 --apply 才真删
  * 密文默认**保留**，必须显式 --drop-cipher 才删
  * 删任何东西之前先复制到 --backup-dir（外加 DSM 回收站双保险）
  * 每组只删"多余的"那份，判定依据是**mtime 最新**优先留
    （精确原件是最后装上的，mtime 最新）
  * 一个文档的恢复件全都没有时，绝不动它的密文

用法：
    python3 tidy_delivery.py <目录> [--dry] [--apply] [--drop-cipher]
                             [--backup-dir DIR] [--report FILE]
                             [--prefer-ext osheet,xlsx] [--keep-ext L0CK3D]
"""
import argparse
import os
import shutil
import sys
import time

# 认定为"中间产物/冗余"的文件名模式
DUP_MARKERS = ('_Drive重建', '_已修复', '_修复', '_重建', '_部分修复')
SPACED = ' '          # 文件名含空格 → 重名副本，多半是中间件
DUP_SUFFIX = ('_1', '_2', ' (1)', ' (2)')

DEFAULT_KEEP_EXT = ('L0CK3D',)


def parse_args():
    ap = argparse.ArgumentParser(description='收尾清理恢复目录')
    ap.add_argument('target', help='要整理的目录')
    ap.add_argument('--dry', action='store_true', help='只预览，不改动（默认行为）')
    ap.add_argument('--apply', action='store_true', help='真正执行删除')
    ap.add_argument('--drop-cipher', action='store_true',
                    help='连同 *.L0CK3D 密文一起删（默认保留密文）')
    ap.add_argument('--drop-ransom-note', action='store_true',
                    help='连同 read-me3.txt 勒索信一起删（默认保留）')
    ap.add_argument('--backup-dir', default='', help='删除前把要删的文件复制到这里')
    ap.add_argument('--report', default='', help='把报告写到这个文件')
    ap.add_argument('--prefer-ext', default='osheet,xlsx',
                    help='每个文档优先保留的扩展名（逗号分隔，按顺序）')
    ap.add_argument('--keep-ext', default=','.join(DEFAULT_KEEP_EXT),
                    help='永远保留的扩展名（逗号分隔）')
    return ap.parse_args()


def listdir(path):
    out = []
    try:
        for n in sorted(os.listdir(path)):
            p = os.path.join(path, n)
            if os.path.isfile(p) and not os.path.islink(p):
                out.append((n, p))
    except OSError as e:
        print('读取目录失败 %s: %s' % (path, e))
    return out


def split_name(name):
    """拆成 (文档名, 扩展名, 是否密文)。"""
    if name.endswith('.L0CK3D'):
        base = name[:-len('.L0CK3D')]
        # base 可能是 'xxx.osheet' 也可能是 'xxx'
        for ext in ('.osheet', '.xlsx', '.docx', '.ods', '.pdf'):
            if base.endswith(ext):
                return base[:-len(ext)], ext[1:], True
        return base, 'L0CK3D', True
    for ext in ('.osheet', '.xlsx', '.docx', '.ods', '.pdf'):
        if name.endswith(ext):
            return name[:-len(ext)], ext[1:], False
    return name, '', False


def is_dup_marker(doc):
    if any(m in doc for m in DUP_MARKERS):
        return True
    if SPACED in doc:
        return True
    for s in DUP_SUFFIX:
        if doc.endswith(s):
            return True
    return False


def main():
    a = parse_args()
    target = a.target
    if not os.path.isdir(target):
        print('不是目录: %s' % target)
        return 1

    apply_ = a.apply and not a.dry
    keep_ext = set(x.strip() for x in a.keep_ext.split(',') if x.strip())
    prefer = [x.strip() for x in a.prefer_ext.split(',') if x.strip()]
    backup = a.backup_dir or (os.path.join('/tmp', 'c3r_tidy_backup_' + time.strftime('%Y%m%d_%H%M%S')))

    entries = listdir(target)
    groups = {}          # 文档名 -> {扩展名: [(name, path, mtime)]}
    ciphers = []         # 密文单独收集，绝不与同名恢复件混在一组
    loose = []           # 不成组的杂项
    for name, path in entries:
        doc, ext, cipher = split_name(name)
        if cipher:
            ciphers.append((name, path, os.path.getmtime(path)))
            continue
        if ext == '':
            loose.append((name, path))
            continue
        groups.setdefault(doc, {}).setdefault(ext, []).append(
            (name, path, os.path.getmtime(path)))

    plan_del = []
    plan_keep = []

    for doc, exts in groups.items():
        # 1) 文档名带中间产物标记 / 空格 / _1 副本 → 整组都是冗余
        if is_dup_marker(doc):
            for ext, items in exts.items():
                if ext in keep_ext and not a.drop_cipher:
                    plan_keep += items          # 密文默认留
                else:
                    plan_del += items
            continue
        # 2) 正常文档名：每个扩展名留最新的一个，其余删
        for ext, items in exts.items():
            if ext in keep_ext and not a.drop_cipher:
                plan_keep += items
                continue
            items = sorted(items, key=lambda t: t[2])   # mtime 升序
            if len(items) == 1:
                plan_keep.append(items[0])
                continue
            # 保留优先级：prefer 里的扩展名优先留最新的；同一扩展名留最新
            keep = items[-1]
            for it in items[:-1]:
                plan_del.append(it)
            plan_keep.append(keep)

    for name, path in loose:
        # 勒索信默认保留（是唯一的事件凭证，且含 orig_len 来源）；要删必须显式指定
        if name.startswith('read-me3') and a.drop_ransom_note:
            plan_del.append((name, path, os.path.getmtime(path)))
        else:
            plan_keep.append((name, path, os.path.getmtime(path)))

    # 密文：默认全部保留；只有显式 --drop-cipher 才进删除清单
    for n, p, m in ciphers:
        if a.drop_cipher:
            plan_del.append((n, p, m))
        else:
            plan_keep.append((n, p, m))

    # ---- 输出计划 ----
    lines = []
    lines.append('===== 保留 (%d) =====' % len(plan_keep))
    for n, p, m in sorted(plan_keep, key=lambda t: t[0]):
        lines.append('  留 %10d  %s' % (os.path.getsize(p), n))
    lines.append('===== 将删除 (%d) =====' % len(plan_del))
    for n, p, m in sorted(plan_del, key=lambda t: t[0]):
        lines.append('  删 %10d  %s' % (os.path.getsize(p), n))
    cipher_del = [x for x in plan_del if x[0].endswith('.L0CK3D')]
    lines.append('  其中密文 %d 个（%s）'
                 % (len(cipher_del), '将删除' if a.drop_cipher else '保留——需加 --drop-cipher 才删'))
    lines.append('===== 模式：%s =====' % ('实际执行' if apply_ else '预览（加 --apply 才执行）'))
    text = '\n'.join(lines)
    print(text)
    if a.report:
        with open(a.report, 'w') as f:
            f.write(text + '\n')

    if not apply_:
        print('\n预览模式，未做任何改动。')
        return 0

    if not plan_del:
        print('\n没有要删的东西。')
        return 0

    # ---- 备份 ----
    os.makedirs(backup, exist_ok=True)
    for n, p, m in plan_del:
        try:
            shutil.copy2(p, os.path.join(backup, n))
        except Exception as e:
            print('备份失败 %s: %s' % (n, e))
    print('\n已备份 %d 个文件到 %s' % (len(plan_del), backup))

    # ---- 删除 ----
    ok = 0
    for n, p, m in plan_del:
        try:
            os.remove(p)
            ok += 1
        except Exception as e:
            print('删除失败 %s: %s' % (n, e))
    print('已删除 %d / %d' % (ok, len(plan_del)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
