#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAS 端诊断：用文件结构签名反推每种加密模式的真实加密区间。

标记里的模式字节若不在 {0x24, 0x25, 0x26}，或怀疑公式不准时，用本脚本验证。

原理：文件里有些位置"必须是明文"才能保持结构自洽，这些位置就是探针：
  - zip 类(xlsx/docx/zip)：中央目录在尾部；每个成员的本地头 PK\\x03\\x04
    偏移可从中央目录读出，明文区里的成员头一定能对上
  - PDF：尾部 %%EOF
  - JPEG：尾部 FFD9；PNG：尾部 IEND

对每个候选模型计算明文区，检查明文区内的探针是否全部命中；
全部命中即为正确模型。

用法：
  python probe_modes.py <根目录> [<根目录> ...]
  python probe_modes.py --sizes <根目录>     只统计 mode/pct/扩展名分布

只读。
"""
import collections
import os
import struct
import sys

TAIL = 588


def tail_meta(p):
    sz = os.path.getsize(p)
    with open(p, 'rb') as f:
        f.seek(sz - TAIL)
        t = f.read(TAIL)
    if len(t) < TAIL:
        return None
    return t[572], t[573], struct.unpack('<Q', t[580:588])[0], sz


def enc_ranges(mode, pct, n):
    if mode == 0x24:
        return [(0, n)]
    if mode == 0x26:
        return [(0, min(n, 1 << 20))]
    if mode == 0x25:
        w = 4 * (n // 100)
        if w <= 0:
            return [(0, n)]
        gap = (n - 3 * w) // 2
        if gap < 0:
            return [(0, n)]
        return [(0, w), (w + gap, 2 * w + gap), (2 * (w + gap), 3 * w + 2 * gap)]
    return None


def plain_ranges(n, enc):
    out = []
    pos = 0
    for s, e in sorted(enc):
        if s > pos:
            out.append((pos, min(s, n)))
        pos = max(pos, e)
    if pos < n:
        out.append((pos, n))
    return [(s, e) for s, e in out if e > s]


def read_central(data):
    idx = data.rfind(b'PK\x05\x06')
    if idx < 0 or idx + 22 > len(data):
        return None
    cd_size, cd_off = struct.unpack('<II', data[idx + 12:idx + 20])
    n_ent = struct.unpack('<H', data[idx + 10:idx + 12])[0]
    if cd_off == 0xFFFFFFFF or n_ent == 0 or cd_off >= len(data):
        return None
    cd = data[cd_off:cd_off + cd_size]
    offs = []
    i = 0
    for _ in range(n_ent):
        if i + 46 > len(cd) or cd[i:i + 4] != b'PK\x01\x02':
            break
        offs.append(struct.unpack('<I', cd[i + 42:i + 46])[0])
        i += 46 + struct.unpack('<H', cd[i + 28:i + 30])[0] + struct.unpack('<H', cd[i + 30:i + 32])[0]
    return offs


def candidate_models(n):
    """候选加密区间模型"""
    c = {}
    c['whole'] = [(0, n)]
    c['prefix1M'] = [(0, min(n, 1 << 20))]
    w = 4 * (n // 100)
    if w > 0:
        gap = (n - 3 * w) // 2
        if gap >= 0:
            c['seg4pct_x3'] = [(0, w), (w + gap, 2 * w + gap), (2 * (w + gap), 3 * w + 2 * gap)]
    for pv in (5, 8, 10, 12, 16, 20, 25, 33):
        ww = (n * pv) // 100
        gp = (n - 3 * ww) // 2
        if ww > 0 and 3 * ww + 2 * gp <= n:
            c['seg_w%d%%' % pv] = [(0, ww), (ww + gp, 2 * ww + gp), (2 * (ww + gp), 3 * ww + 2 * gp)]
    return c


def evaluate_zip(p, orig, enc):
    """明文区内的成员头是否全部命中"""
    with open(p, 'rb') as f:
        data = f.read(orig)
    offs = read_central(data)
    if not offs:
        return None
    pl = plain_ranges(orig, enc)
    ip = hit = 0
    for o in offs:
        if o >= orig:
            continue
        if any(s <= o < e for s, e in pl):
            ip += 1
            if data[o:o + 4] == b'PK\x03\x04':
                hit += 1
    return hit, ip


def main():
    args = sys.argv[1:]
    sizes_only = False
    if args and args[0] == '--sizes':
        sizes_only = True
        args = args[1:]
    if not args:
        sys.exit(__doc__)

    census = collections.Counter()
    total = collections.Counter()
    tail_plain = collections.Counter()

    for root in args:
        for dp, dns, fns in os.walk(root):
            for fn in sorted(fns):
                if not fn.endswith('.L0CK3D'):
                    continue
                p = os.path.join(dp, fn)
                try:
                    mode, pct, orig, sz = tail_meta(p)
                except Exception:
                    continue
                key = (mode, pct)
                total[key] += 1
                ext = os.path.splitext(fn[:-7])[1].lower()
                census[(key, ext)] += 1

                # 尾部明文判定
                try:
                    with open(p, 'rb') as f:
                        f.seek(max(0, orig - 8192))
                        t = f.read(min(8192, orig))
                    if ext == '.pdf' and b'%%EOF' in t:
                        tail_plain[key] += 1
                    elif ext in ('.jpg', '.jpeg') and b'\xff\xd9' in t[-4:]:
                        tail_plain[key] += 1
                    elif ext == '.png' and b'IEND' in t[-16:]:
                        tail_plain[key] += 1
                    elif ext in ('.xlsx', '.docx', '.zip') and b'PK\x05\x06' in t:
                        tail_plain[key] += 1
                except Exception:
                    pass

    print('=== 各 mode 总量与尾部明文率 ===')
    for k in sorted(total):
        n = total[k]
        print('  mode=0x%02x pct=0x%02x  总数 %5d  尾部明文 %5d (%.0f%%)'
              % (k[0], k[1], n, tail_plain[k], 100.0 * tail_plain[k] / max(1, n)))
    print('=== mode x 扩展名 ===')
    for (k, ext), c in sorted(census.items()):
        print('  mode=0x%02x pct=0x%02x  %-10s %5d' % (k[0], k[1], ext or '(无)', c))
    if sizes_only:
        return

    print('=== zip 结构探针反推 ===')
    shown = 0
    for root in args:
        for dp, dns, fns in os.walk(root):
            for fn in sorted(fns):
                low = fn.lower()
                if not low.endswith(('.xlsx.l0ck3d', '.docx.l0ck3d', '.zip.l0ck3d')):
                    continue
                p = os.path.join(dp, fn)
                try:
                    mode, pct, orig, sz = tail_meta(p)
                except Exception:
                    continue
                print('--- mode=0x%02x pct=0x%02x  %s' % (mode, pct, fn[:44]))
                scored = []
                for name, enc in candidate_models(orig).items():
                    r = evaluate_zip(p, orig, enc)
                    if r is None:
                        continue
                    hit, ip = r
                    scored.append((hit, ip, name))
                for hit, ip, name in sorted(scored, key=lambda x: -x[0]):
                    tag = '  <<< 全命中' if ip > 0 and hit == ip else ''
                    print('    %-14s 命中 %4d / 明文区内成员 %4d%s' % (name, hit, ip, tag))
                shown += 1
                if shown >= 8:
                    return


if __name__ == '__main__':
    main()
