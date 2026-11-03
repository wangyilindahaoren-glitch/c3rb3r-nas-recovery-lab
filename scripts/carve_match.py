#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地端：把雕刻出来的文件对应回被加密的 .L0CK3D 原件。

雕刻出来的文件没有文件名和路径，只能靠内容特征反查它原本是哪个密文文件。
判断依据是 C3RB3R 的加密模式：
  - 0x25 分段 / 0x26 前缀：密文文件里仍保留着未加密的明文区间，
    雕刻出的原件在**同样的偏移**上必须与密文文件逐字节相同 —— 这是硬证据。
  - 0x24 整文件加密：密文里没有任何明文可比对，只能按原始长度匹配，
    属于**弱候选**，必须人工确认，不能算证实。

用法：
  python carve_match.py <雕刻输出目录> --enc <NAS 上的加密文件根目录>
  python carve_match.py <雕刻输出目录> --enc-list <路径清单.txt>
  python carve_match.py <雕刻输出目录> --enc <根目录> --index enc_index.json
  python carve_match.py <雕刻输出目录> --enc <根目录> --tol 4096 --workers 8

输出：
  <雕刻输出目录>/_carve_match.jsonl   每个雕刻文件一条记录
  <雕刻输出目录>/_carve_summary.json   统计

状态（必须分开登记，候选不等于恢复成功）：
  confirmed        密文文件的明文区与雕刻结果逐字节一致，长度也一致 —— 证实
  confirmed_prefix 雕刻结果是原件的前缀（被截断），明文区一致 —— 证实但不全
  confirmed_extra  雕刻结果比原件长（尾部是雕刻带出的垃圾），前 N 字节已证实
  size_only        只有原始长度对得上（整文件加密，无明文可比）—— 弱候选
  no_match         找不到对应密文文件

参数：
  --tol N           长度允许的偏差（foremost 常在尾部多切出垃圾，可用 --tol 1048576）
  --prefix-max N    反查截断前缀时，原始长度允许比雕刻结果大多少（默认 64MB）
  --prefix-cands N  反查截断前缀时最多检查多少个候选（默认 200）
"""
import hashlib
import json
import os
import struct
import sys
import threading

TAIL = 588
DEFAULT_TOL = 0
LOCK = threading.Lock()


# ---------- 加密区间（与 plain_extract.py / c3r_btrfs.py 的公式保持一致） ----------

def enc_ranges(mode, pct, n):
    """被加密的字节区间 [(s,e), ...]；未知模式返回 None"""
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


def tail_meta(path):
    """返回 (mode, pct, orig_len, file_size)；读不出返回 None"""
    try:
        sz = os.path.getsize(path)
        if sz < TAIL:
            return None
        with open(path, 'rb') as f:
            f.seek(sz - TAIL)
            t = f.read(TAIL)
    except OSError:
        return None
    if len(t) < TAIL:
        return None
    return t[572], t[573], struct.unpack('<Q', t[580:588])[0], sz


# ---------- 密文索引 ----------

def build_index(roots):
    """扫描 .L0CK3D 文件，按原始长度建索引 {orig_len: [(path, mode, pct, enc_size, orig_len)]}
    roots 可以是目录，也可以直接是 .L0CK3D 文件。"""
    idx = {}
    n_files = 0

    def add(p):
        nonlocal n_files
        m = tail_meta(p)
        if not m:
            return
        mode, pct, orig_len, sz = m
        idx.setdefault(orig_len, []).append((p, mode, pct, sz, orig_len))
        n_files += 1

    for root in roots:
        if os.path.isfile(root):
            add(root)
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.endswith('.L0CK3D'):
                    add(os.path.join(dirpath, fn))
    return idx, n_files


def load_index(path):
    with open(path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    idx = {}
    for k, v in raw.items():
        if k.startswith('_'):
            continue
        idx[int(k)] = [tuple(x) for x in v]
    n_files = 0
    meta = path + '.meta'
    if os.path.exists(meta):
        try:
            with open(meta, 'r', encoding='utf-8') as f:
                n_files = json.load(f).get('_n_files', 0)
        except (OSError, ValueError):
            pass
    return idx, n_files


def save_index(path, idx, n_files):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({str(k): [list(x) for x in v] for k, v in idx.items()}, f)
    with open(path + '.meta', 'w', encoding='utf-8') as f:
        json.dump({'_n_files': n_files}, f)


# ---------- 比对 ----------

def compare_plain(enc_path, carved_path, mode, pct, n, carved_size=None, min_cmp=512):
    """在明文区间上逐字节比对密文文件与雕刻结果。
    返回 (是否一致, 比对字节数)；明文区不可用时返回 (None, 0)。"""
    enc = enc_ranges(mode, pct, n)
    if enc is None:
        return None, 0
    plains = plain_ranges(n, enc)
    if not plains:
        return None, 0
    csize = carved_size if carved_size is not None else os.path.getsize(carved_path)
    compared = 0
    with open(enc_path, 'rb') as fe, open(carved_path, 'rb') as fc:
        for s, e in plains:
            if s >= csize:
                break
            end = min(e, csize)
            ln = end - s
            if ln <= 0:
                continue
            fe.seek(s)
            fc.seek(s)
            remaining = ln
            while remaining > 0:
                chunk = min(1 << 20, remaining)
                a = fe.read(chunk)
                b = fc.read(chunk)
                if len(a) != chunk or len(b) != chunk or a != b:
                    return False, compared
                compared += chunk
                remaining -= chunk
    if compared < min_cmp:
        return None, compared
    return True, compared


def sha256(path, limit=None):
    h = hashlib.sha256()
    n = 0
    with open(path, 'rb') as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
            if limit and n >= limit:
                break
    return h.hexdigest(), n


# ---------- 主流程 ----------

def walk_files(root):
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.startswith('_carve'):
                continue
            p = os.path.join(dirpath, fn)
            if os.path.isfile(p):
                yield p


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    carved_root = args[0]
    enc_roots = []
    enc_list = None
    index_path = None
    out_dir = None
    tol = DEFAULT_TOL
    prefix_max = 64 << 20
    prefix_cands = 200
    workers = 8
    i = 1
    while i < len(args):
        a = args[i]
        if a == '--enc':
            enc_roots.append(args[i + 1]); i += 2
        elif a == '--enc-list':
            enc_list = args[i + 1]; i += 2
        elif a == '--index':
            index_path = args[i + 1]; i += 2
        elif a == '--out':
            out_dir = args[i + 1]; i += 2
        elif a == '--tol':
            tol = int(args[i + 1]); i += 2
        elif a == '--prefix-max':
            prefix_max = int(args[i + 1]); i += 2
        elif a == '--prefix-cands':
            prefix_cands = int(args[i + 1]); i += 2
        elif a == '--workers':
            workers = int(args[i + 1]); i += 2
        else:
            i += 1

    if not os.path.isdir(carved_root):
        sys.exit('雕刻目录不存在: %s' % carved_root)
    out_dir = out_dir or carved_root

    # 建索引
    if index_path and os.path.exists(index_path):
        try:
            idx, n_files = load_index(index_path)
            print('载入索引: %s（%d 个密文文件）' % (index_path, n_files))
        except Exception as e:
            print('索引载入失败，重新扫描: %s' % e)
            idx, n_files = build_index(enc_roots)
    else:
        if enc_list:
            enc_roots = []
            with open(enc_list, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        enc_roots.append(line)
        if not enc_roots:
            sys.exit('需要 --enc <根目录> 或 --enc-list <清单>')
        print('扫描密文文件建立索引 ...')
        idx, n_files = build_index(enc_roots)
        print('索引完成: %d 个密文文件，%d 种原始长度' % (n_files, len(idx)))
        if index_path:
            save_index(index_path, idx, n_files)
            print('索引已保存: %s' % index_path)

    carved = list(walk_files(carved_root))
    print('待匹配雕刻文件: %d' % len(carved))

    results = []
    stats = {'confirmed': 0, 'confirmed_prefix': 0, 'confirmed_extra': 0,
             'size_only': 0, 'no_match': 0, 'error': 0, 'multi_candidate': 0}
    lock = threading.Lock()
    work = [0]
    total = len(carved)

    def handle(path):
        try:
            size = os.path.getsize(path)
        except OSError:
            with lock:
                stats['error'] += 1
            return
        rec = {'carved': path, 'carved_size': size, 'status': 'no_match',
               'enc_path': None, 'orig_len': None, 'mode': None,
               'compared_bytes': 0, 'note': ''}

        # --- 路径 1：按原始长度匹配 ---
        cands = []
        if size in idx:
            cands.extend(idx[size])
        if tol:
            for n in range(max(0, size - tol), size + tol + 1):
                if n != size and n in idx:
                    cands.extend(idx[n])

        best = None          # (比对字节数, 密文路径, mode, pct, 原始长度, 状态)
        weak = []            # 整个文件都被加密 -> 没有明文可比，只能按长度给弱候选
        for (enc_path, mode, pct, enc_sz, orig_len) in cands:
            enc = enc_ranges(mode, pct, orig_len)
            plains = plain_ranges(orig_len, enc) if enc else []
            if not plains:
                # 0x24 整文件加密，或 0x26 作用在小于 1MiB 的文件上 —— 全文都是密文
                weak.append((enc_path, mode, pct, enc_sz, orig_len))
                continue
            ok, cmp_bytes = compare_plain(enc_path, path, mode, pct, orig_len,
                                          carved_size=size)
            if ok:
                if size == orig_len:
                    st = 'confirmed'
                elif size < orig_len:
                    st = 'confirmed_prefix'
                else:
                    st = 'confirmed_extra'
                if best is None or cmp_bytes > best[0]:
                    best = (cmp_bytes, enc_path, mode, pct, orig_len, st)

        # --- 路径 2：雕刻结果可能是被截断的前缀，反查原始长度更大的密文文件 ---
        if best is None and prefix_max > 0:
            cand2 = []
            for n, lst in idx.items():
                if size <= n <= size + prefix_max:
                    for item in lst:
                        cand2.append((n - size, item))
            cand2.sort(key=lambda x: x[0])
            for _d, (enc_path, mode, pct, enc_sz, orig_len) in cand2[:prefix_cands]:
                ok, cmp_bytes = compare_plain(enc_path, path, mode, pct, orig_len,
                                              carved_size=size)
                if ok:
                    best = (cmp_bytes, enc_path, mode, pct, orig_len, 'confirmed_prefix')
                    break

        if best is not None:
            cmp_bytes, enc_path, mode, pct, orig_len, st = best
            rec.update({'status': st, 'enc_path': enc_path, 'orig_len': orig_len,
                        'mode': '0x%02x' % mode, 'pct': '0x%02x' % pct,
                        'compared_bytes': cmp_bytes})
            with lock:
                stats[st] += 1
                if len(cands) > 1:
                    stats['multi_candidate'] += 1
        elif weak:
            # 弱候选只在长度**精确**相等时成立；tol 再松也不放宽这里，
            # 否则会把毫不相干的文件挂成候选。
            exact = [w for w in weak if w[4] == size]
            if not exact:
                with lock:
                    stats['no_match'] += 1
                return
            enc_path, mode, pct, enc_sz, orig_len = exact[0]
            rec.update({'status': 'size_only', 'enc_path': enc_path, 'orig_len': orig_len,
                        'mode': '0x%02x' % mode, 'pct': '0x%02x' % pct,
                        'note': '整个文件都被加密，没有明文可比；仅原始长度相同，需人工确认'})
            with lock:
                stats['size_only'] += 1
        else:
            with lock:
                stats['no_match'] += 1

        try:
            rec['carved_sha256'] = sha256(path)[0]
        except OSError:
            rec['carved_sha256'] = None
        with lock:
            results.append(rec)
            work[0] += 1
            if work[0] % 200 == 0 or work[0] == total:
                print('  %d/%d  已确认=%d 前缀=%d 弱候选=%d 无匹配=%d'
                      % (work[0], total, stats['confirmed'], stats['confirmed_prefix'],
                         stats['size_only'], stats['no_match']))

    threads = []
    for i2 in range(0, total, max(1, workers)):
        chunk = carved[i2:i2 + max(1, workers)]
        for p in chunk:
            t = threading.Thread(target=handle, args=(p,))
            t.start()
            threads.append(t)
    for t in threads:
        t.join()

    results.sort(key=lambda r: (r['status'], r['carved']))
    jsonl = os.path.join(out_dir, '_carve_match.jsonl')
    with open(jsonl, 'w', encoding='utf-8') as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    summary = {'carved_files': total, 'enc_files_indexed': n_files, 'stats': stats,
               'note': 'size_only 为弱候选，不是证实；必须人工确认。'}
    with open(os.path.join(out_dir, '_carve_summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print('\n结果: %s' % jsonl)
    for k in ('confirmed', 'confirmed_prefix', 'confirmed_extra', 'size_only',
              'no_match', 'error'):
        print('  %-18s %d' % (k, stats[k]))


if __name__ == '__main__':
    main()
