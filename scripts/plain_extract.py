#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAS 端：从 C3RB3R 加密的 .L0CK3D 文件中提取未被加密的明文部分。

背景：C3RB3R 只加密文件的一部分，但勒索程序改写文件时把整个文件写成了
新的 extent。因此新 extent 里未被加密的那部分字节仍是原始明文。

加密区间模型（已用备份 zip 里的未加密原件逐 4K 块比对实证）：
  0x24  整文件加密      -> 明文区为空
  0x25  分段加密       -> 三等分，每段前 4% 加密，明文约 88%
  0x26  前缀加密 1MiB  -> 明文区 = [1MiB, n)

输出：密文区填 0、明文区填原字节，**保持原文件偏移**，
这样 zip 的中央目录偏移、PDF 的 xref 偏移依然正确，可以继续抢救内部结构。

用法：
  python plain_extract.py <根目录1> [根目录2] ...
  python plain_extract.py --stamp 勒索恢复_20260101 <根目录> ...

只读源文件；写入 <根目录>/<stamp>/1_可读内容/ 下；已存在的目标改名加 .rN，绝不覆盖。
"""
import io
import json
import os
import struct
import sys
import tarfile

TAIL = 588
DEFAULT_STAMP = '勒索恢复'


def tail_meta(p):
    """返回 (mode, pct, orig_len, file_size)"""
    sz = os.path.getsize(p)
    with open(p, 'rb') as f:
        f.seek(sz - TAIL)
        t = f.read(TAIL)
    if len(t) < TAIL:
        return None
    return t[572], t[573], struct.unpack('<Q', t[580:588])[0], sz


def enc_ranges(mode, pct, n):
    """被加密的字节区间 [(s,e), ...]；未知模式返回 None"""
    if mode == 0x24:
        return [(0, n)]
    if mode == 0x26:
        return [(0, min(n, 1 << 20))]
    if mode == 0x25:
        # 三等分，每段前 4%
        w = 4 * (n // 100)
        if w <= 0:
            return [(0, n)]
        gap = (n - 3 * w) // 2
        if gap < 0:
            # 文件太小放不下三段，退化为整文件
            return [(0, n)]
        return [(0, w), (w + gap, 2 * w + gap), (2 * (w + gap), 3 * w + 2 * gap)]
    return None


def plain_ranges(n, enc):
    """加密区间之外的明文区间"""
    out = []
    pos = 0
    for s, e in sorted(enc):
        if s > pos:
            out.append((pos, min(s, n)))
        pos = max(pos, e)
    if pos < n:
        out.append((pos, n))
    return [(s, e) for s, e in out if e > s]


def write_safe(path, data):
    """写新文件；已存在则改名加 .rN 后缀，绝不覆盖。返回实际写入路径"""
    final = path
    if os.path.exists(path):
        i = 1
        while os.path.exists('%s.r%d' % (path, i)):
            i += 1
        final = '%s.r%d' % (path, i)
    d = os.path.dirname(final)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = final + '.wtmp'
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, final)
    return final


def main():
    args = sys.argv[1:]
    stamp = DEFAULT_STAMP
    if args and args[0] == '--stamp':
        stamp = args[1]
        args = args[2:]
    if not args:
        sys.exit(__doc__)

    stats = {'total': 0, 'whole_encrypted': 0, 'recovered': 0,
             'unknown_mode': 0, 'read_error': 0, 'plain_bytes': 0, 'orig_bytes': 0}
    manifest = []

    for root in args:
        if not os.path.isdir(root):
            continue
        for dp, dns, fns in os.walk(root):
            # 不重复处理已生成的恢复目录
            if stamp in dp:
                continue
            for fn in sorted(fns):
                if not fn.endswith('.L0CK3D'):
                    continue
                p = os.path.join(dp, fn)
                stats['total'] += 1
                try:
                    t = tail_meta(p)
                except Exception:
                    stats['read_error'] += 1
                    continue
                if t is None:
                    stats['read_error'] += 1
                    continue
                mode, pct, orig, sz = t
                rec = {'path': p, 'mode': '0x%02x' % mode, 'pct': '0x%02x' % pct, 'orig': orig}

                enc = enc_ranges(mode, pct, orig)
                if enc is None:
                    rec['status'] = 'unknown_mode'
                    stats['unknown_mode'] += 1
                    manifest.append(rec)
                    continue
                pr = plain_ranges(orig, enc)
                if not pr:
                    rec['status'] = 'whole_encrypted'
                    stats['whole_encrypted'] += 1
                    manifest.append(rec)
                    continue

                buf = bytearray(orig)
                try:
                    with open(p, 'rb') as f:
                        for s, e in pr:
                            f.seek(s)
                            buf[s:e] = f.read(e - s)
                except Exception:
                    stats['read_error'] += 1
                    rec['status'] = 'read_error'
                    manifest.append(rec)
                    continue

                rel = os.path.relpath(p, root)
                outrel = rel[:-7] if rel.endswith('.L0CK3D') else rel
                dst = os.path.join(root, stamp, '1_可读内容', outrel)
                try:
                    final = write_safe(dst, bytes(buf))
                except Exception as e:
                    rec['status'] = 'write_error'
                    rec['error'] = repr(e)[:200]
                    manifest.append(rec)
                    continue

                nbytes = sum(e - s for s, e in pr)
                rec.update({'status': 'partial_plain',
                            'dst': os.path.relpath(final, root),
                            'plain_bytes': nbytes,
                            'plain_ratio': round(nbytes / float(orig or 1), 4),
                            'fragments': len(pr)})
                stats['recovered'] += 1
                stats['plain_bytes'] += nbytes
                stats['orig_bytes'] += orig
                manifest.append(rec)

    # 清单写到第一个根目录下
    rdir = os.path.join(args[0], stamp)
    try:
        os.makedirs(rdir, exist_ok=True)
        with open(os.path.join(rdir, '可读内容清单.json'), 'w', encoding='utf-8') as f:
            json.dump({'stats': stats, 'items': manifest}, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

    print('扫描 %d 个加密文件: 整文件加密 %d, 提取明文 %d (%.1f MB / %.1f MB), '
          '未知模式 %d, 读取错误 %d'
          % (stats['total'], stats['whole_encrypted'], stats['recovered'],
             stats['plain_bytes'] / 1048576.0, stats['orig_bytes'] / 1048576.0,
             stats['unknown_mode'], stats['read_error']))


if __name__ == '__main__':
    main()
