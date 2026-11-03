# -*- coding: utf-8 -*-
"""路线 C 证实：把候选原件与密文的**未加密区**逐字节比对。

- mode 0x26：明文区 = [1MiB, n)  -> 可逐字节证实
- mode 0x25：明文区 = 三段之间的间隙 -> 可逐字节证实
- mode 0x24：整文件加密，无明文可比 -> 只能靠「同名 + 同 orig_len」判定（标记为 name_size_only）

只读。输出 JSON 清单。
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
    """返回明文区间列表"""
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
    pairs_tsv = sys.argv[1]
    out_json = sys.argv[2]
    items = []
    stats = {'proven': 0, 'name_size_only': 0, 'mismatch': 0, 'error': 0}
    with open(pairs_tsv, encoding='utf-8') as f:
        for ln in f:
            ln = ln.rstrip('\n')
            if not ln:
                continue
            parts = ln.split('\t')
            cipher, cand = parts[0], parts[1]
            rec = {'cipher': cipher, 'cand': cand, 'src': parts[2] if len(parts) > 2 else ''}
            try:
                tm = tail_meta(cipher)
                if tm is None:
                    raise ValueError('footer 不足')
                mode, pct, orig, csz = tm
                rec['mode'] = '0x%02x' % mode
                rec['orig_len'] = orig
                if not os.path.exists(cand):
                    raise ValueError('候选不存在')
                csz2 = os.path.getsize(cand)
                rec['cand_size'] = csz2
                if csz2 != orig:
                    raise ValueError('候选大小 %d != orig_len %d' % (csz2, orig))
                enc = enc_ranges(mode, pct, orig)
                pr = plain_ranges(orig, enc)
                total = sum(e - s for s, e in pr)
                rec['plain_bytes'] = total
                if total == 0:
                    rec['verdict'] = 'name_size_only'
                    stats['name_size_only'] += 1
                    items.append(rec)
                    continue
                same = 0
                diff = 0
                first_diff = None
                with open(cand, 'rb') as fc, open(cipher, 'rb') as fk:
                    for s, e in pr:
                        fc.seek(s)
                        fk.seek(s)
                        n = e - s
                        left = n
                        while left > 0:
                            k = min(1 << 20, left)
                            a = fc.read(k)
                            b = fk.read(k)
                            if a == b:
                                same += k
                            else:
                                if first_diff is None:
                                    for j in range(min(len(a), len(b))):
                                        if a[j] != b[j]:
                                            first_diff = s + j
                                            break
                                d = 0
                                for j in range(min(len(a), len(b))):
                                    if a[j] != b[j]:
                                        d += 1
                                diff += d
                            left -= k
                rec['same_bytes'] = same
                rec['diff_bytes'] = diff
                if first_diff is not None:
                    rec['first_diff_offset'] = first_diff
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

    json.dump({'stats': stats, 'items': items}, open(out_json, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('证实结果:', json.dumps(stats, ensure_ascii=False))
    for it in items:
        if it['verdict'] != 'proven':
            print('  %-12s %s  %s' % (it['verdict'], it.get('err', ''),
                                      it['cipher'][-70:]))


if __name__ == '__main__':
    main()
