#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plain_index.py —— 全盘明文索引：给路线 C 找"别的目录里的原件"用。

背景
----
路线 C 传统做法是在几个已知目录（Backup/、#recycle/、@sharesnap/）里按
(文件名, orig_len) 找原件。实测**漏得很多**：群晖上同一个共享往往同时存在
「快照」「其它团队空间」「别人的 home」几份拷贝，只要有一个没被加密，就是现成的原件。

本工具一次性把整个卷的文件清单（大小 + 路径）抓下来建索引，然后可以：
  1) 按 (basename, size) 精确找同名同大小的明文副本 —— 最可靠；
  2) 按 basename 找同名但大小不同的副本 —— 往往是**同一份文档的另一个版本**，
     对原件已被覆盖的 0x24 文件是重要的兜底；
  3) 按 (目录, size) 粗查。

⚠️ 建索引时**不要加 `-xdev`**：Btrfs 的每个子卷 st_dev 都不同，
`find /volume1 -xdev` 只会遍历根子卷，homes / photo / 团队空间 等子卷会被整片跳过
（实测 14TB 卷上：加 -xdev 只找到 45824 个文件，去掉后 829514 个）。

用法
----
  # 建索引
  python3 plain_index.py --build /volume1 --out /tmp/plain_index.tsv
  # 找副本
  python3 plain_index.py --index /tmp/plain_index.tsv --targets /tmp/targets.tsv
  # 只看某几个名字
  python3 plain_index.py --index /tmp/plain_index.tsv --name 采购明细.xlsx

索引格式：每行 `size<TAB>path`
"""
import argparse
import collections
import os
import sys


def build(root, out):
    """在 NAS 上调用 find 建索引（find 支持 -printf 时用 -printf，否则退回 stat）。"""
    import subprocess
    if subprocess.call(['find', root, '-type', 'f', '-printf', '%s\\t%p\\n'],
                       stdout=open(out, 'wb')) != 0:
        raise SystemExit('find 失败')
    n = 0
    with open(out, 'rb') as f:
        for _ in f:
            n += 1
    print('索引 %d 行 -> %s' % (n, out))


def load_index(path):
    byname = collections.defaultdict(list)
    bysize = collections.defaultdict(list)
    n = 0
    with open(path, encoding='utf-8', errors='replace') as f:
        for ln in f:
            ln = ln.rstrip('\n')
            if '\t' not in ln:
                continue
            szs, p = ln.split('\t', 1)
            try:
                szs = int(szs)
            except ValueError:
                continue
            n += 1
            byname[os.path.basename(p)].append((szs, p))
            bysize[szs].append(p)
    print('索引 %d 行, %d 个文件名, %d 个尺寸' % (n, len(byname), len(bysize)))
    return byname, bysize


def load_targets(path):
    out = []
    with open(path, encoding='utf-8') as f:
        for ln in f:
            ln = ln.rstrip('\n')
            if not ln:
                continue
            parts = ln.split('\t')
            if len(parts) >= 3:
                out.append((int(parts[0]), parts[1], '\t'.join(parts[2:])))
            elif len(parts) == 2:
                out.append((int(parts[0]), '', parts[1]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--build', metavar='ROOT')
    ap.add_argument('--out')
    ap.add_argument('--index')
    ap.add_argument('--targets')
    ap.add_argument('--name')
    ap.add_argument('--exclude', default='', help='排除的路径前缀（逗号分隔）')
    ap.add_argument('--loose', action='store_true',
                    help='同时列出同名但大小不同的副本')
    args = ap.parse_args()

    if args.build:
        build(args.build, args.out or '/tmp/plain_index.tsv')
        return

    if not args.index:
        sys.exit(__doc__)
    byname, bysize = load_index(args.index)
    excl = [x for x in args.exclude.split(',') if x]

    def skip(p):
        return any(p.startswith(x) for x in excl) or p.endswith('.L0CK3D')

    if args.name:
        for sz, p in byname.get(args.name, []):
            if skip(p):
                continue
            print('%d\t%s' % (sz, p))
        return

    if not args.targets:
        return
    targets = load_targets(args.targets)
    exact = 0
    loose = 0
    for sz, ext, rel in targets:
        b = os.path.basename(rel)
        hit = None
        for csz, p in byname.get(b, []):
            if csz == sz and not skip(p):
                hit = (csz, p)
                break
        if hit:
            exact += 1
            print('命中\t%d\t%s\t%s' % (sz, rel, hit[1]))
        elif args.loose:
            for csz, p in byname.get(b, []):
                if skip(p):
                    continue
                loose += 1
                ratio = abs(csz - sz) / float(sz or 1)
                print('近似\t%d->%d(%.1f%%)\t%s\t%s'
                      % (sz, csz, ratio * 100, rel, p))
                break
    print('精确命中 %d / %d' % (exact, len(targets)), file=sys.stderr)
    if args.loose:
        print('仅近似命中 %d' % loose, file=sys.stderr)


if __name__ == '__main__':
    main()
