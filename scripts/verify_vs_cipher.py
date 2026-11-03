#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用密文的**明文区**独立验证重建的 .osheet 是否与原件逐字节一致。

密文结构 = [文件内容（被加密）][588 字节勒索标记]
所以文件内容部分 C0 = C[:size-588]，长度 == orig_len。
mode 0x26 = 只加密开头一段，C0 的尾部是**原件的原样字节**。
重建件 R 与 C0 从某个偏移 E 起应当逐字节相同 —— 这就是对重建的独立验证。
"""
import json
import os
import struct
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

FOOT = 588
CIPHER_DIR = 'cipher/cipher14'
REBUILD_DIR = 'rebuilt7'

PAIRS = [
    ('2024年8-12月产品采购明细.osheet.L0CK3D', '2024年8-12月产品采购明细.osheet'),
    ('2024年1-7月产品采购明细.osheet.L0CK3D', '2024年1-7月产品采购明细.osheet'),
    ('2023年产品采购记录.osheet.L0CK3D', '2023年产品采购记录.osheet'),
    ('2022年产品采购记录.osheet.L0CK3D', '2022年产品采购记录.osheet'),
    ('产品采购记录.osheet.L0CK3D', '2021年产品采购记录.osheet'),
    ('2020年产品采购记录 .osheet.L0CK3D', '2020年产品采购记录.osheet'),
    ('2024年1-7月耗材采购明细.osheet.L0CK3D', '2024年1-7月耗材采购明细.osheet'),
]


def footer_of(C):
    ft = C[-FOOT:]
    return {'orig': struct.unpack_from('<Q', ft, 580)[0],
            'mode': ft[572], 'crypto': ft[574]}


def main():
    print('%-24s %-5s %-9s %-9s %-10s %-10s %s' %
          ('文件', 'mode', 'orig_len', '重建长度', '加密区E', '明文区', '结论'))
    print('-' * 104)
    out = {}
    for cname, rname in PAIRS:
        cp = os.path.join(CIPHER_DIR, cname)
        rp = os.path.join(REBUILD_DIR, rname)
        if not os.path.exists(cp):
            continue
        C = open(cp, 'rb').read()
        ft = footer_of(C)
        C0 = C[:len(C) - FOOT]
        assert len(C0) == ft['orig'], (len(C0), ft['orig'])
        R = open(rp, 'rb').read() if os.path.exists(rp) else None
        name = rname.replace('.osheet', '')

        if R is None:
            print('%-24s 0x%02X  %-9d %-9s' % (name, ft['mode'], ft['orig'], '无'))
            continue
        if len(R) != ft['orig']:
            print('%-24s 0x%02X  %-9d %-9d %-10s %-10s 重建件长度差 %+d 字节'
                  % (name, ft['mode'], ft['orig'], len(R), '-', '-', len(R) - ft['orig']))
            out[name] = {'mode': ft['mode'], 'orig': ft['orig'], 'len': len(R),
                         'same': False}
            continue
        # 求最长公共后缀
        n = min(len(C0), len(R))
        i = 0
        while i < n and C0[len(C0) - 1 - i] == R[len(R) - 1 - i]:
            i += 1
        E = len(R) - i
        plain = C0[E:] == R[E:]
        # 明文区里是否**整段**一致（含前面部分）
        concl = '明文区 %d 字节逐字节一致 ✓' % (len(R) - E) if plain else '明文区有差异 ✗'
        print('%-24s 0x%02X  %-9d %-9d %-10d %-10d %s'
              % (name, ft['mode'], ft['orig'], len(R), E, len(R) - E, concl))
        if not plain and E < len(R):
            for j in range(E, len(R)):
                if C0[j] != R[j]:
                    print('        首个差异 @%d: 密文=%r 重建=%r' % (j, C0[j:j + 24], R[j:j + 24]))
                    break
        out[name] = {'mode': ft['mode'], 'orig': ft['orig'], 'len': len(R),
                      'E': E, 'plain_len': len(R) - E, 'same': plain}
    print()
    print('== 加密区长度（mode 0x26）==')
    for k, v in out.items():
        if v.get('mode') == 0x26 and 'E' in v:
            print('  %-24s 加密 %d 字节 (%.3f MiB)  明文 %d 字节 (%.1f%%)'
                  % (k, v['E'], v['E'] / 1048576.0, v['plain_len'],
                     100.0 * v['plain_len'] / v['orig']))
    with open('_cipher_verify.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
