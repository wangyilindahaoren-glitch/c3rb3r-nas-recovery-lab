#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recover_one.py —— 扫描一次之后，**按需恢复任意一个文件**，不用重新扫盘。

原理（2026-10-31 实测验证）：
  * 扫描阶段（carve_worker.py）只做一件事：全盘找 `.osheet` 容器的强签名，
    把命中地址写进日志。**日志就是一份"全盘 .osheet 容器位置索引"**。
  * 认领阶段（claim_fast.py）只做一件事：从日志里取地址 → 逻辑地址映射物理地址
    → 读块设备那几百字节 → 按尾部标记定容器长度 → 严格解析 → 落盘。
    **只读日志 + 随机读，几乎不耗 IO。**

所以：扫描跑完一次之后，日志就长期有效，以后想恢复哪个就恢复哪个。

目标文件的原始长度不用去翻勒索信——**直接读它密文尾部 588 字节的
`footer[580:588]`（u64 LE）** 就是 `orig_len`。

用法（NAS 上，root）：
    python3 recover_one.py <密文路径> [--out 目录] [--logs 日志目录] [--xlsx]

    # 例：恢复 2019 年采购记录
    python3 recover_one.py "/volume1/homes/<用户名>/某个目录/2019年产品采购记录.osheet.L0CK3D" --xlsx

注意：
  * 只能恢复**扫描签名覆盖到的类型**（当前是 `.osheet` / WPS 表格容器）。
  * **时效性**：旧 extent 可能被后来写入的数据覆盖。扫完越早认领越安全；
    扫完很久之后再回头认，命中率会下降。
"""
import argparse
import glob
import os
import struct
import subprocess
import sys

FOOT = 588
DEV = os.environ.get("C3R_DEV", "/dev/mapper/cachedev_0")


def footer_of(path):
    size = os.path.getsize(path)
    if size < FOOT:
        raise SystemExit('文件太小，读不到尾部标记')
    with open(path, 'rb') as f:
        f.seek(-FOOT, 2)
        ft = f.read(FOOT)
    orig = struct.unpack_from('<Q', ft, 580)[0]
    return {
        'orig_len': orig,
        'mode': ft[572],
        'pct': ft[573],
        'crypto': ft[574],
        'marker': ft[568:572].hex(),
        'const': ft[576:580].hex(),
        'size': size,
    }


def main():
    ap = argparse.ArgumentParser(description='按需恢复单个文件（复用已有扫描日志）')
    ap.add_argument('cipher', help='密文路径（*.L0CK3D）')
    ap.add_argument('--out', default='/tmp/c3r_carve/claim2', help='输出目录')
    ap.add_argument('--logs', default='/tmp/c3r_carve/logs', help='扫描日志目录')
    ap.add_argument('--claim', default='claim_fast.py', help='claim_fast.py 路径')
    ap.add_argument('--xlsx', action='store_true', help='顺带转成 xlsx（需本地有 openpyxl）')
    args = ap.parse_args()

    if not os.path.exists(args.cipher):
        raise SystemExit('密文不存在: %s' % args.cipher)
    ft = footer_of(args.cipher)
    label = os.path.basename(args.cipher)
    for suf in ('.osheet.L0CK3D', '.L0CK3D'):
        if label.endswith(suf):
            label = label[:-len(suf)]
            break
    label = label.replace('.osheet', '')

    print('目标       : %s' % args.cipher)
    print('原始长度   : %d（密文 %d = 原长 + %d 尾部标记）'
          % (ft['orig_len'], ft['size'], FOOT))
    print('加密模式   : 0x%02x | 加密方式 0x%02x | 会话标记 %s'
          % (ft['mode'], ft['crypto'], ft['marker']))
    if ft['crypto'] != 0x20:
        print('⚠️ 加密方式不是 0x20（0x21 是诱饵模式，有密钥也解不开）')

    logs = sorted(glob.glob(os.path.join(args.logs, 'w*.log')))
    if not logs:
        raise SystemExit('找不到扫描日志: %s' % args.logs)
    n = sum(1 for lg in logs for line in open(lg, 'rb')
            if '签名命中'.encode('utf-8') in line)
    print('扫描日志   : %d 个文件，命中记录约 %d 条（复用，不再扫盘）' % (len(logs), n))

    cmd = [sys.executable, args.claim,
           '--len', str(ft['orig_len']),
           '--label', label,
           '--out', args.out]
    # ⚠️ claim_fast.py 的 --log 是 nargs="+"，重复写 --log 会被 argparse 覆盖成最后一个！
    # 必须一次性把所有日志路径跟在单个 --log 后面。
    cmd += ['--log'] + logs
    print('\n===== 认领 =====')
    sys.stdout.flush()
    rc = subprocess.call(cmd)
    sys.stdout.flush()
    if rc != 0:
        print('认领返回 %d' % rc)

    out = os.path.join(args.out, 'target_%s.osheet' % label)
    if not os.path.exists(out):
        print('\n未命中。可能原因：该文件不在已扫范围内，或旧 extent 已被覆盖。')
        return 1
    # 认领是"覆盖写"，若命中数为 0 则可能是上次留下的旧文件——按 mtime 判断
    import time
    age = time.time() - os.path.getmtime(out)
    if age > 600:
        print('\n⚠️ 产出文件是 %d 分钟前的旧物，本次未命中。' % (age / 60))
        return 1
    print('\n产出: %s (%d 字节)' % (out, os.path.getsize(out)))

    if args.xlsx:
        here = os.path.dirname(os.path.abspath(args.claim))
        conv = os.path.join(here, 'osheet_to_xlsx.py')
        if os.path.exists(conv):
            x = os.path.join(args.out, label + '.xlsx')
            print('\n===== 转 xlsx =====')
            subprocess.call([sys.executable, conv, out, x])
            if os.path.exists(x):
                print('产出: %s (%d 字节)' % (x, os.path.getsize(x)))
        else:
            print('（未找到 osheet_to_xlsx.py，跳过）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
