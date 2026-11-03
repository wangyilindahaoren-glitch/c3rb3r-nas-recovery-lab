#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""74 个 .osheet 密文的最终恢复流水线（v2）。

修正：
  * 所有有 Drive 仓库的都重建 → 交叉验证才有意义
  * 长版 metatext 从密文明文区（0x26）或雕刻原件里抠真实的载荷
  * uid 一致性过滤防长度撞车
"""
import io
import json
import os
import shutil
import struct
import sys
import hashlib

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rebuild_exact as RE   # noqa: E402
import osheet_to_xlsx as M   # noqa: E402

FOOT = 588
DRIVE_ROOTS = ['drive/x56/drive56', 'drive/x56/drive_extra']
TPL = 'claim3/target_2019年产品采购记录.osheet'
OUT = 'final74'
CIPHER_DIRS = ['cipher/oc/osheet_cipher', 'cipher/dup/dup_cipher']
# 上一批已交付（运营相关），本批不重复处理
SKIP = {'低于7天发货时效产品明细'}


def sha(b):
    return hashlib.sha256(b).hexdigest()


def header_and_meta(raw):
    h = {}
    pos = 4
    while True:
        if pos + 9 <= len(raw) and raw[pos + 4] in (0x05, 0x02) and \
                (raw[pos + 5:pos + 8] == b'@C_' or raw[pos + 5:pos + 9] == b'text'):
            break
        if pos >= len(raw):
            break
        tag = raw[pos]
        if tag == 0x00:
            pos += 1
            continue
        e = raw.find(b'\x00', pos + 1)
        key = raw[pos + 1:e].decode('latin1')
        p = e + 1
        if tag == 0x08:
            ln = raw[p]; p += 1
        else:
            ln = int.from_bytes(raw[p:p + 4], 'little'); p += 4
        h[key] = raw[p:p + ln]
        p += ln
        if p < len(raw) and raw[p] == 0:
            p += 1
        pos = p
    meta = None
    for _p, _X, name, payload in M.parse_strict(raw)[1]:
        if name == 'metatext':
            meta = payload
            break
    return h, meta


def meta_from_cipher(C0):
    """从密文的明文区里抠出 metatext 条目的载荷（0x26 文件的 metatext 在尾部）。"""
    i = C0.find(b'\x02metatext\x00')
    if i < 0:
        return None
    ln = struct.unpack_from('<I', C0, i + 1 + 9)[0]
    payload = C0[i + 1 + 9 + 4: i + 1 + 9 + 4 + ln]
    return payload[:-1] if payload.endswith(b'\x00') else payload


def drive_dir(repo):
    for root in DRIVE_ROOTS:
        p = os.path.join(root, repo)
        if os.path.isdir(os.path.join(p, 'text')):
            return p
    return None


def main():
    plan = json.load(io.open('_batch_plan.json', encoding='utf-8'))
    tmpl = open(TPL, 'rb').read()

    cipher_files = []
    for d in CIPHER_DIRS:
        if os.path.isdir(d):
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                cipher_files.append((p, os.path.getsize(p)))

    def find_cipher(orig_len):
        want = orig_len + FOOT
        for p, sz in cipher_files:
            if sz == want:
                return p
        return None

    info = {}
    for r in plan:
        rec = {'tag': r['tag'], 'orig_len': r['orig_len'], 'cipher': r['cipher'],
               'carved': r['carved'], 'repo': None, 'meta': None, 'uid': None}
        if r['carved']:
            raw = open(r['carved'], 'rb').read()
            h, meta = header_and_meta(raw)
            idstr = h['id'].rstrip(b'\x00').decode('latin1')
            rec['repo'] = idstr
            rec['uid'] = idstr.split('_')[0]
            rec['meta'] = meta[:-1] if meta and meta.endswith(b'\x00') else meta
        info[r['tag']] = rec

    # Drive-only：长度匹配 + uid 过滤
    idx = {}
    for line in io.open('drive/drive_index.tsv', encoding='utf-8', errors='replace'):
        p = line.rstrip('\n').split('\t')
        if len(p) >= 4:
            idx.setdefault(p[0], []).append((p[1], p[2], int(p[3])))
    _tpl = open(TPL, 'rb').read()
    TPL_HDR = RE.split_header(_tpl)[1]
    _i = _tpl.find(b'\x02id\x00')
    TPL_IDLEN = int.from_bytes(_tpl[_i + 4:_i + 8], 'little')

    def calc_lens(base):
        import base64 as _b
        total = (TPL_HDR - TPL_IDLEN + len(base) + 1) + 11
        for _ver, fn, size in idx.get(base, []):
            out = []
            k = 0
            while k < len(fn):
                if fn[k] == "{":
                    j = fn.index("}", k)
                    out.append(chr(int(fn[k + 1:j])))
                    k = j + 1
                else:
                    out.append(fn[k])
                    k += 1
            try:
                nm = _b.b64decode("".join(out)).decode('utf-8', 'replace')
            except Exception:
                nm = "".join(out)
            if nm == 'read-me3.txt':
                continue
            full = nm if nm in ('metatext', 'metabinairy_files') else ('text/' + nm)
            total += 12 + len(full) + size
        return (total + 32, total + 129)

    uid_by_home = {}
    for r in plan:
        rec = info[r['tag']]
        if rec['uid']:
            home = r['cipher'].split('/homes/')[1].split('/')[0]
            uid_by_home.setdefault(home, set()).add(rec['uid'])

    for r in plan:
        rec = info[r['tag']]
        if rec['repo']:
            continue
        cands = [b for b in idx if r['orig_len'] in calc_lens(b)]
        if not cands:
            continue
        home = r['cipher'].split('/homes/')[1].split('/')[0]
        allow = uid_by_home.get(home, set())
        filt = [b for b in cands if b.split('_')[0] in allow]
        pick = filt if len(filt) == 1 else (cands if len(cands) == 1 else [])
        if pick:
            rec['repo'] = pick[0]
            rec['uid'] = pick[0].split('_')[0]
            rec['ambiguous'] = len(cands) > 1

    # 重建（所有有 Drive 仓库的）
    rebuilt = {}
    for r in plan:
        rec = info[r['tag']]
        if not rec['repo']:
            continue
        d = drive_dir(rec['repo'])
        if not d:
            continue
        ver = io.open(os.path.join(d, 'master'), encoding='utf-8').read().strip()
        meta = rec['meta']
        if meta is None:
            cp = find_cipher(r['orig_len'])
            if cp:
                C = open(cp, 'rb').read()
                C0 = C[:len(C) - FOOT]
                if C[-FOOT + 572] == 0x26:
                    meta = meta_from_cipher(C0)
                    if meta:
                        rec['meta_from'] = '密文明文区'
        rebuilt[r['tag']] = RE.rebuild(os.path.join(d, 'text'), ver, tmpl, rec['repo'], meta)
        rec['rebuilt_len'] = len(rebuilt[r['tag']])

    # 交叉验证
    print('== 交叉验证（雕刻原件 vs Drive 重建，ground truth）==')
    xs_ok = xs_bad = 0
    for r in plan:
        rec = info[r['tag']]
        if not (rec['carved'] and r['tag'] in rebuilt):
            continue
        orig = open(rec['carved'], 'rb').read()
        got = rebuilt[r['tag']]
        if orig == got:
            xs_ok += 1
        else:
            xs_bad += 1
            n = min(len(orig), len(got))
            at = next((i for i in range(n) if orig[i] != got[i]), n)
            print('  ✗ %-9d %-42s 长度 %d/%d 差异@%d' %
                  (r['orig_len'], rec['repo'], len(orig), len(got), at))
    print('  %d 个两路都有：逐字节相同 %d，不同 %d' % (xs_ok + xs_bad, xs_ok, xs_bad))

    # 与密文明文区对证
    print('\n== 与密文明文区逐字节对证 ==')
    vok = vbad = 0
    for r in plan:
        rec = info[r['tag']]
        data = open(rec['carved'], 'rb').read() if rec['carved'] else rebuilt.get(r['tag'])
        if not data:
            continue
        cp = find_cipher(r['orig_len'])
        if not cp:
            continue
        C = open(cp, 'rb').read()
        C0 = C[:len(C) - FOOT]
        if C[-FOOT + 572] != 0x26:
            continue
        n = min(len(C0), len(data))
        i = 0
        while i < n and C0[len(C0) - 1 - i] == data[len(data) - 1 - i]:
            i += 1
        E = len(data) - i
        if C0[E:] == data[E:]:
            vok += 1
            rec['verified_bytes'] = len(data) - E
        else:
            vbad += 1
            print('  ✗ %-9d %-42s' % (r['orig_len'], rec['repo'] or '-'))
    print('  0x26 文件：一致 %d，不一致 %d' % (vok, vbad))

    # 组装
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    rows = []
    for r in plan:
        rec = info[r['tag']]
        base = os.path.basename(r['cipher'])[:-len('.L0CK3D')]
        if base.endswith('.osheet'):
            base = base[:-len('.osheet')]
        if base in SKIP:
            rows.append({'name': base, 'cipher': r['cipher'], 'status': '上一批已交付'})
            continue
        rel = r['cipher'].split('/homes/')[1]
        rel = rel[:-len('.L0CK3D')]
        if rel.endswith('.osheet'):
            rel = rel[:-len('.osheet')]
        dst = os.path.join(OUT, rel + '.osheet')
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if rec['carved']:
            shutil.copy(rec['carved'], dst)
            st = '雕刻（字节级精确原件）'
        elif r['tag'] in rebuilt:
            with open(dst, 'wb') as f:
                f.write(rebuilt[r['tag']])
            st = 'Drive 字节级重建'
        else:
            rows.append({'name': base, 'cipher': r['cipher'], 'status': '未恢复',
                         'orig_len': r['orig_len']})
            continue
        rows.append({'name': base, 'cipher': r['cipher'], 'status': st,
                     'orig_len': r['orig_len'], 'len': os.path.getsize(dst),
                     'len_ok': os.path.getsize(dst) == r['orig_len'],
                     'repo': rec['repo'], 'verified': rec.get('verified_bytes', 0),
                     'sha': sha(open(dst, 'rb').read())})
    json.dump(rows, io.open('_final74.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    ok = sum(1 for x in rows if x['status'] not in ('未恢复', '上一批已交付'))
    lok = sum(1 for x in rows if x.get('len_ok'))
    print('\n== %d/%d 恢复（另 1 个上一批已交付）；长度与 orig_len 分毫不差 %d 个 =='
          % (ok, len(rows), lok))
    for x in rows:
        if x['status'] == '未恢复':
            print('  未恢复: %-9d %s' % (x['orig_len'], x['cipher']))
        elif 'len' in x and not x.get('len_ok'):
            print('  长度差 %+d: %-9d %s' % (x['len'] - x['orig_len'], x['orig_len'], x['cipher']))


if __name__ == '__main__':
    main()
