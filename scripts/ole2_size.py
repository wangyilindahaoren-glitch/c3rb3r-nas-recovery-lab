#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ole2_size.py —— 从 OLE2（.xls/.doc）文件头算出它的**分配长度**，用来认领雕刻候选。

为什么需要
----------
`carve_gen.py` 对 OLE2 只记偏移不记长度（结构复杂，价值低），所以 0x24 整文件加密的
`.xls` / `.doc` 一直认领不回来。但 OLE2 容器有个好性质：

    **文件长度 = 512（文件头） + 扇区数 × 扇区大小**

且文件总是按扇区补齐，所以 `orig_len` 一定是 `512 + k × S`（S = 2^扇区移位，通常 512）。
于是只要算出 FAT 里**最大被占用扇区号**，就知道这个 OLE2 文件有多长，
可以直接和勒索标记里的 `orig_len` 对上。

做法
----
1. 读 512 字节文件头，取扇区大小 S、DIFAT 数组；
2. 按 DIFAT 读出 FAT 扇区，拼成扇区分配表；
3. 找 FAT 里最大的"已分配"扇区号 M → 分配长度 = 512 + (M+1)×S。

用法
----
  # 从雕刻日志里认领（扫描日志每行: ole \t 物理偏移 \t 0 \t 0）
  python3 ole2_size.py --log <扫描日志目录> --targets <targets.tsv> --out <认领结果.json>

  # 直接看某个偏移上的 OLE2 有多大
  python3 ole2_size.py --probe <物理偏移>

⚠️ **算出来的长度只能当"候选线索"，绝对不能直接装回去**
--------------------------------------------------------------------
2026-11-01 实测：13 个 `.xls`/`.doc` 目标（全部 mode 0x24，没有明文区可比对），
按 OLE2 分配长度命中 35 处。逐个抽出正文核对后：
  - 4 个 12288 字节的 `.doc` 候选，正文分别是「成美国际售后快递证明」
    「遂宁市…股东会决议」「遂宁市…董事会决议」「…」——**没有一个**是目标
    「食品安全管理制度目录清单.doc」；
  - 2 个 26112 字节的 `.doc` 候选，正文都是「公众信息网查询系统密码重置申请表」，
    而目标是三份「代运营服务协议」；
  - `.xls` 候选抽 SST 后是「采购申请单/供应商货品编码/<某品牌A>/<某品牌B>」
    「<公司乙>科技有限公司」等，目标是「<公司甲>结算单」。
**命中率 0/6+**。原因：卷上同长度的 OLE2 文档（合同、决议、申请表、表单）非常多。

所以 OLE2 只能「用长度找线索」，**装回前必须抽正文人工确认是哪个文档**，
拿不准就别装 —— 装错一份合同比空着更糟。
"""
import argparse
import json
import os
import struct
import sys

DEV = os.environ.get('C3R_DEV', '/dev/mapper/cachedev_0')
OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE


def ole2_alloc_size(fd, off):
    """返回 (分配长度, 扇区大小) 或 (None, 原因)。"""
    h = os.pread(fd, 512, off)
    if len(h) < 512 or h[:8] != OLE_MAGIC:
        return None, '不是 OLE2 头'
    shift = struct.unpack_from('<H', h, 0x1E)[0]
    if not (7 <= shift <= 12):
        return None, '扇区移位异常 %d' % shift
    S = 1 << shift
    nfat = struct.unpack_from('<I', h, 0x2C)[0]
    if nfat == 0 or nfat > 1024:
        return None, 'FAT 扇区数异常 %d' % nfat
    difat = list(struct.unpack_from('<109I', h, 0x4C))
    fat = []
    for fs in difat[:nfat]:
        if fs >= 0xFFFFFFFA:
            break
        b = os.pread(fd, S, off + 512 + fs * S)
        if len(b) < S:
            break
        fat.extend(struct.unpack_from('<%dI' % (S // 4), b))
    if not fat:
        return None, 'FAT 读不到'
    maxs = -1
    for i, v in enumerate(fat):
        if v < 0xFFFFFFFA:          # 已分配
            maxs = i
    if maxs < 0:
        return None, 'FAT 里没有已分配扇区'
    return 512 + (maxs + 1) * S, S


def load_targets(path):
    out = {}
    with open(path, encoding='utf-8') as f:
        for ln in f:
            ln = ln.rstrip('\n')
            if not ln:
                continue
            sz, ext, rel = ln.split('\t')
            out.setdefault(int(sz), []).append((ext, rel))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', help='扫描日志目录（找 ole 行）')
    ap.add_argument('--targets')
    ap.add_argument('--out')
    ap.add_argument('--probe', type=int)
    args = ap.parse_args()

    fd = os.open(DEV, os.O_RDONLY)
    try:
        if args.probe is not None:
            n, s = ole2_alloc_size(fd, args.probe)
            print('偏移 %d: 分配长度 %s, 扇区 %s' % (args.probe, n, s))
            return

        targets = load_targets(args.targets) if args.targets else {}
        hits = []
        if args.log:
            for fn in sorted(os.listdir(args.log)):
                if not fn.endswith('.log'):
                    continue
                with open(os.path.join(args.log, fn), encoding='utf-8', errors='replace') as f:
                    for ln in f:
                        if ln.startswith('#') or '\t' not in ln:
                            continue
                        p = ln.rstrip('\n').split('\t')
                        if p[0] == 'ole':
                            try:
                                hits.append(int(p[1]))
                            except ValueError:
                                pass
        print('ole 命中 %d 个' % len(hits))
        found = []
        seen = set()
        for off in hits:
            n, info = ole2_alloc_size(fd, off)
            if not n:
                continue
            key = (off, n)
            if key in seen:
                continue
            seen.add(key)
            tg = targets.get(n)
            print('  off=%-14d 长度=%-10d %s %s' % (
                off, n, info, ('命中 ' + str(tg)) if tg else ''))
            if tg:
                found.append({'start': off, 'size': n, 'targets': tg})
        if args.out:
            json.dump({'found': found}, open(args.out, 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=1)
            print('命中目标 %d 个 -> %s' % (len(found), args.out))
    finally:
        os.close(fd)


if __name__ == '__main__':
    main()
