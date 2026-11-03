# -*- coding: utf-8 -*-
"""雕刻件证实：把雕刻出来的原件与对应密文的**未加密区**逐字节比对。

- mode 0x26 / 0x25：密文有明文区，比对一致 → `proven`
- mode 0x24：整文件加密，没有可比对区 → 只能标 `name_size_only`
  （此时另有结构校验：zip 能开 / PDF 有 xref / 图片头尾完整）

输入：pairs.tsv（cipher \t candidate \t 目标相对路径）
输出：JSON 清单
"""
import json
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
    for s, e in enc:
        if s > pos:
            out.append((pos, s))
        pos = max(pos, e)
    if pos < n:
        out.append((pos, n))
    return out


def main():
    pairs_tsv, out_json = sys.argv[1], sys.argv[2]
    items = []
    stats = {'proven': 0, 'name_size_only': 0, 'mismatch': 0, 'error': 0}
    with open(pairs_tsv, encoding='utf-8') as f:
        for ln in f:
            ln = ln.rstrip('\n')
            if not ln:
                continue
            parts = ln.split('\t')
            cipher, cand = parts[0], parts[1]
            rec = {'cipher': cipher, 'cand': cand}
            try:
                tm = tail_meta(cipher)
                if tm is None:
                    raise ValueError('footer 不足')
                mode, pct, orig, csz = tm
                rec['mode'] = '0x%02x' % mode
                rec['orig_len'] = orig
                csz2 = os.path.getsize(cand)
                rec['cand_size'] = csz2
                if csz2 != orig:
                    raise ValueError('雕刻件大小 %d != orig_len %d' % (csz2, orig))
                enc = enc_ranges(mode, pct, orig)
                pr = plain_ranges(orig, enc)
                total = sum(e - s for s, e in pr)
                rec['plain_bytes'] = total
                if total == 0:
                    rec['verdict'] = 'name_size_only'
                    stats['name_size_only'] += 1
                    items.append(rec)
                    continue
                diff = 0
                first = None
                with open(cand, 'rb') as fc, open(cipher, 'rb') as fk:
                    for s, e in pr:
                        fc.seek(s); fk.seek(s)
                        left = e - s
                        while left > 0:
                            k = min(1 << 20, left)
                            a = fc.read(k); b = fk.read(k)
                            if a != b:
                                for j in range(min(len(a), len(b))):
                                    if a[j] != b[j]:
                                        diff += 1
                                        if first is None:
                                            first = s + j
                            left -= k
                rec['diff_bytes'] = diff
                if first is not None:
                    rec['first_diff_offset'] = first
                if diff == 0:
                    rec['verdict'] = 'proven'
                    stats['proven'] += 1
                else:
                    rec['verdict'] = 'mismatch'
                    stats['mismatch'] += 1
            except Exception as ex:
                rec['verdict'] = 'error'
                rec['err'] = str(ex)[:200]
                stats['error'] += 1
            items.append(rec)

    json.dump({'stats': stats, 'items': items},
              open(out_json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('证实结果:', json.dumps(stats, ensure_ascii=False))
    for it in items:
        if it['verdict'] != 'proven':
            print('  %-14s %-10s %s' % (it['verdict'], it.get('mode', ''),
                                        it['cand'][-70:]))


if __name__ == '__main__':
    main()
