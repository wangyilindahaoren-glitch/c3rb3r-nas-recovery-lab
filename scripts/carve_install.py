#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carve_install.py —— 路线 C+D 统一收尾：认领候选 -> 择优 -> 证实 -> 生成装回计划 -> 装回。

把「明文副本」和「雕刻件」合成一张装回计划，避免两套流程各装各的、互相冲突。

分组键 = **(原始长度, 文件名)**。同名同长 = 同一份文档的副本，这解决了
「雕刻候选按长度认领，长度撞到别的文件」的歧义问题：
  - 一个候选 -> 装到该组所有目标路径；
  - 候选份数 >= 目标路径数 -> 一一分配。

候选优先级：
  1. 能与密文**未加密区逐字节一致**的（`proven`）；
  2. 其次按结构质量（zip 成员数 / PDF 页数与 xref 吻合度 / 图片像素数）；
  3. 同类里质量高的优先。

PDF 严格校验不过时，就地追加新 xref 修复（原始字节全保留），而不是全量重建
（重建会丢内容，见 scripts/pdf_xref_repair.py 的说明）。

用法
----
  # 1) 认领（先跑 scripts/carve_claim.py，它会写 <输出目录>/_认领结果.json）
  # 2) 出计划
  python3 carve_install.py plan --home <家目录> --targets targets.tsv \
        --index plain_index.tsv --claim <carve目录>/_认领结果.json \
        --out-plan install_plan.tsv
  # 3) 装回（只新增、不覆盖、不动密文）+ 结构校验
  python3 carve_install.py install --home <家目录> --plan install_plan.tsv \
        --log install.log

--index 可以不给（不给就只有路线 D 的雕刻件）。
"""
import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile

TAIL = 588
DEV = os.environ.get('C3R_DEV', '/dev/mapper/cachedev_0')
ACL = '/usr/syno/bin/synoacltool'
CIPHER_SUF = '.L0CK3D'
ZIP_EXT = ('.xlsx', '.docx', '.zip', '.xlsm', '.pptx')
IMG_EXT = ('.png', '.jpg', '.jpeg')
OBJ_RE = re.compile(rb'(?<![0-9])(\d+)\s+(\d+)\s+obj\b')
XREF_RE = re.compile(rb'(?m)^xref\r?\n(\d+)\s+(\d+)\r?\n')


# ------------------------------------------------------------------ 基础工具
def sha256_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def tail_meta(p):
    sz = os.path.getsize(p)
    if sz < TAIL:
        return None
    with open(p, 'rb') as f:
        f.seek(sz - TAIL)
        t = f.read(TAIL)
    return t[572], t[573], struct.unpack('<Q', t[580:588])[0]


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


def verify_against_cipher(cand, cipher):
    """候选 vs 密文未加密区逐字节比对。返回 (verdict, 说明)。"""
    tm = tail_meta(cipher)
    if tm is None:
        return 'error', 'footer 不足'
    mode, pct, orig = tm
    if os.path.getsize(cand) != orig:
        return 'error', '候选大小 != orig_len'
    pr = plain_ranges(orig, enc_ranges(mode, pct, orig) or [])
    total = sum(e - s for s, e in pr)
    if total == 0:
        return 'name_size_only', '整文件加密(0x24)，无可比对区'
    diff = 0
    with open(cand, 'rb') as fc, open(cipher, 'rb') as fk:
        for s, e in pr:
            fc.seek(s)
            fk.seek(s)
            left = e - s
            while left > 0:
                k = min(1 << 20, left)
                a = fc.read(k)
                b = fk.read(k)
                if a != b:
                    for j in range(min(len(a), len(b))):
                        if a[j] != b[j]:
                            diff += 1
                left -= k
    if diff == 0:
        return 'proven', '与密文明文区逐字节一致（%d 字节）' % total
    return 'mismatch', '与密文明文区差 %d 字节' % diff


def carve_range(start, size):
    fd = os.open(DEV, os.O_RDONLY)
    try:
        out = bytearray()
        pos, left = start, size
        while left > 0:
            n = min(8 << 20, left)
            b = os.pread(fd, n, pos)
            if not b:
                break
            out += b
            pos += len(b)
            left -= len(b)
        return bytes(out)
    finally:
        os.close(fd)


# ------------------------------------------------------------------ 结构质量
def zip_quality(p):
    try:
        z = zipfile.ZipFile(p)
    except Exception as e:
        return -1, 'zip 打不开: %s' % str(e)[:60]
    names = z.namelist()
    if not names:
        return -1, '空容器'
    bad = z.testzip()
    if bad:
        return 0, '成员 CRC 错: %s' % bad
    return len(names), '%d 个成员 CRC 全过' % len(names)


def img_quality(p, kind):
    """JPEG 允许 FFD9 之后有残留字节（实测有原件尾部多 24 字节）。"""
    sz = os.path.getsize(p)
    with open(p, 'rb') as f:
        head = f.read(32)
        f.seek(max(0, sz - 4096))
        tail = f.read()
    if kind == 'png':
        if not head.startswith(b'\x89PNG\r\n\x1a\n'):
            return -1, '不是 PNG'
        if b'IEND' not in tail:
            return 0, '缺 IEND'
        w, h = struct.unpack('>II', head[16:24])
        return 1000 + w * h, '%dx%d' % (w, h)
    if not head.startswith(b'\xff\xd8'):
        return -1, '不是 JPEG'
    if b'\xff\xd9' not in tail:
        return 0, '缺 FFD9'
    return 1000, 'JPEG 头尾完整'


def pdf_quality(p):
    data = open(p, 'rb').read()
    ok = data.startswith(b'%PDF-') and b'%%EOF' in data[-2048:]
    if ok:
        ms = list(re.finditer(rb'startxref\s+(\d+)', data[-2048:]))
        ok = bool(ms)
    if ok:
        off = int(ms[-1].group(1))
        ok = off < len(data) and data[off:off + 4].startswith(b'xref')
    if ok and b'/Catalog' not in data:
        ok = False
    pages = len(re.findall(rb'/Type\s*/Page(?![sA-Za-z])', data))
    if ok:
        return 1000 + pages, '严格校验通过 | 页面 %d 个' % pages
    real = {}
    for m in OBJ_RE.finditer(data):
        real.setdefault(int(m.group(1)), m.start())
    ms = list(XREF_RE.finditer(data))
    good = tot = 0
    if ms:
        m = ms[-1]
        start = int(m.group(1))
        cnt = int(m.group(2))
        for i in range(1, cnt):
            e = data[m.end() + i * 20: m.end() + i * 20 + 20]
            if len(e) < 18 or e[17:18] != b'n':
                continue
            num = start + i
            if num not in real:
                continue
            tot += 1
            if real[num] == int(e[0:10]):
                good += 1
    ratio = (good / tot) if tot else 0
    return int(ratio * 100), '严格校验不过 | xref 偏移吻合 %d/%d | 页面 %d 个' % (good, tot, pages)


def quality_of(p, ext):
    try:
        if ext in ZIP_EXT:
            return zip_quality(p)
        if ext == '.pdf':
            return pdf_quality(p)
        if ext == '.png':
            return img_quality(p, 'png')
        if ext in ('.jpg', '.jpeg'):
            return img_quality(p, 'jpg')
        if ext in ('.xls', '.doc'):
            with open(p, 'rb') as f:
                head = f.read(8)
            if head != b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
                return -1, '不是 OLE2'
            return 1, 'OLE2 头正常'
        return 1, '无专用校验器'
    except Exception as e:
        return -1, '校验异常 %s' % str(e)[:60]


# ------------------------------------------------------------------ PDF 就地修复
def obj_body(data, start):
    m = OBJ_RE.match(data, start)
    if not m:
        return None, None
    p = m.end()
    sm = re.compile(rb'stream\r?\n').search(data, p, p + 8192)
    if sm:
        em = data.find(b'endstream', sm.end())
        if em >= 0:
            e = data.find(b'endobj', em)
            if e >= 0:
                return data[p:e], data[p:sm.start()]
    e = data.find(b'endobj', p)
    if e < 0:
        return None, None
    return data[p:e], data[p:e]


def pdf_repair(data):
    """就地追加新 xref；返回 (新字节, 说明) 或 (None, 原因)。"""
    if not data.startswith(b'%PDF-'):
        return None, '不是 PDF'
    roots = re.findall(rb'/Root\s+(\d+)\s+0\s+R', data)
    if not roots:
        return None, '原 trailer 里没有 /Root'
    root_num = int(roots[-1])
    real = {}
    bodies = {}
    for m in OBJ_RE.finditer(data):
        num = int(m.group(1))
        if num in real:
            continue
        b, d = obj_body(data, m.start())
        if b is None:
            continue
        real[num] = m.start()
        bodies[num] = d or b
    if any(b'/ObjStm' in (bodies.get(n) or b'') for n in real):
        return None, '含对象流(ObjStm)，需全量重建'
    cat_ok = (root_num in real and
              re.search(rb'/Type\s*/Catalog', bodies.get(root_num) or b'') is not None)
    pages = [n for n, d in bodies.items()
             if re.search(rb'/Type\s*/Page(?![sA-Za-z])', d or b'')]
    out = bytearray(data)
    if not out.endswith(b'\n'):
        out += b'\n'
    synth = ''
    if not cat_ok:
        if not pages:
            return None, '既没有可用的 Catalog，也找不到页面对象'
        pn = max(real) + 1
        cn = pn + 1
        off_pn = len(out)
        out += b'%d 0 obj\n<< /Type /Pages /Kids [%s] /Count %d >>\nendobj\n' % (
            pn, b' '.join(b'%d 0 R' % p for p in pages), len(pages))
        off_cn = len(out)
        out += b'%d 0 obj\n<< /Type /Catalog /Pages %d 0 R >>\nendobj\n' % (cn, pn)
        real[pn] = off_pn
        real[cn] = off_cn
        root_num = cn
        synth = '（补造 Catalog+页树）'
    maxnum = max(max(real), root_num)
    xref_off = len(out)
    out += b'xref\n0 %d\n' % (maxnum + 1)
    out += b'0000000000 65535 f \n'
    for i in range(1, maxnum + 1):
        if i in real:
            out += b'%010d 00000 n \n' % real[i]
        else:
            out += b'0000000000 65535 f \n'
    out += b'trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (
        maxnum + 1, root_num, xref_off)
    return bytes(out), ('追加修复 xref%s（原字节全保留，+%d 字节，%d 个页对象）'
                        % (synth, len(out) - len(data), len(pages)))


# ------------------------------------------------------------------ plan
def cmd_plan(a):
    home = a.home
    targets = []
    with open(a.targets, encoding='utf-8') as f:
        for ln in f:
            ln = ln.rstrip('\n')
            if not ln:
                continue
            sz, ext, rel = ln.split('\t')
            targets.append((int(sz), ext, rel))

    idx = collections.defaultdict(list)
    if a.index and os.path.exists(a.index):
        with open(a.index, encoding='utf-8', errors='replace') as f:
            for ln in f:
                ln = ln.rstrip('\n')
                if '\t' not in ln:
                    continue
                szs, p = ln.split('\t', 1)
                if p.endswith(CIPHER_SUF):
                    continue
                try:
                    szs = int(szs)
                except ValueError:
                    continue
                idx[(os.path.basename(p), szs)].append(p)

    copies = collections.defaultdict(list)
    if a.claim and os.path.exists(a.claim):
        claim = json.load(open(a.claim, encoding='utf-8'))
        for r in claim['results']:
            if r.get('ok'):
                copies[r['size']].append((r['start'], r['type']))

    groups = collections.defaultdict(list)
    for sz, ext, rel in targets:
        groups[(sz, os.path.basename(rel))].append((ext, rel))

    workdir = a.workdir or (os.path.dirname(os.path.abspath(a.out_plan)) + '/copies')
    os.makedirs(workdir, exist_ok=True)

    plan = []
    stats = collections.Counter()
    for (sz, b), tgts_all in groups.items():
        ext = tgts_all[0][0]
        ciphers = {}
        for (e, p) in tgts_all:
            tc = os.path.join(home, p) + CIPHER_SUF
            if os.path.exists(tc):
                ciphers[p] = tc
        if not ciphers:
            stats['no_cipher'] += len(tgts_all)
            continue
        mode = '0x%02x' % tail_meta(list(ciphers.values())[0])[0]

        cands = []
        for p in idx.get((b, sz), []):
            q, why = quality_of(p, ext)
            if q < 0 or (ext in IMG_EXT and q < 1000):
                continue
            cands.append({'src': 'C', 'path': p, 'q': q, 'why': why})
        for start, ctype in copies.get(sz, []):
            dst = os.path.join(workdir, '%d_%d' % (sz, start))
            if not os.path.exists(dst):
                data = carve_range(start, sz)
                if len(data) != sz:
                    continue
                with open(dst, 'wb') as f:
                    f.write(data)
            q, why = quality_of(dst, ext)
            if q < 0 or (ext in IMG_EXT and q < 1000):
                continue
            cands.append({'src': 'D', 'path': dst, 'q': q, 'why': why, 'start': start})

        if not cands:
            stats['no_candidate'] += len(tgts_all)
            for (e, p) in tgts_all:
                plan.append({'rel': p, 'ext': ext, 'size': sz, 'mode': mode,
                             'cipher': os.path.join(home, p) + CIPHER_SUF,
                             'action': 'skip', 'reason': '没有任何候选'})
            continue

        seen = set()
        uniq = []
        for c in cands:
            h = sha256_file(c['path'])
            if h in seen:
                continue
            seen.add(h)
            c['sha'] = h
            uniq.append(c)

        def rank(c, cipher):
            v, _ = verify_against_cipher(c['path'], cipher)
            vo = {'proven': 0, 'name_size_only': 1, 'mismatch': 2, 'error': 3}
            return (vo.get(v, 4), -c['q'])

        used = set()
        for (e, t) in tgts_all:
            tc = ciphers.get(t)
            if tc is None:
                continue
            best = bestv = bestvwhy = None
            for i, c in enumerate(uniq):
                if i in used and len(uniq) > len(tgts_all):
                    continue
                v, vwhy = verify_against_cipher(c['path'], tc)
                if v == 'mismatch':
                    continue
                if best is None or rank(c, tc) < rank(best, tc):
                    best, bestv, bestvwhy = c, v, vwhy
                if v == 'proven':
                    break
            if best is None:
                stats['mismatch'] += 1
                plan.append({'rel': t, 'ext': ext, 'size': sz, 'mode': mode,
                             'cipher': tc, 'action': 'skip',
                             'reason': '所有候选都与密文明文区冲突'})
                continue
            used.add(uniq.index(best))
            if len(uniq) < len(tgts_all):
                stats['shared_candidate'] += 1

            install_path = best['path']
            why = best['why']
            if ext == '.pdf' and not why.startswith('严格校验通过'):
                rp = best['path'] + '.repair.pdf'
                out, rwhy = pdf_repair(open(best['path'], 'rb').read())
                if out:
                    with open(rp, 'wb') as f:
                        f.write(out)
                    q2, w2 = quality_of(rp, ext)
                    if q2 >= 1000:
                        install_path = rp
                        why = '修复后严格校验通过 | %s' % rwhy
                    else:
                        why += ' | 就地修复后仍不过（%s）' % w2
                else:
                    why += ' | 就地修复失败：%s' % rwhy

            multi = ''
            if len(uniq) < len(tgts_all):
                multi = (' | 同名同大小有 %d 个目标路径，候选只有 %d 份，'
                         '同一份内容装到全部路径' % (len(tgts_all), len(uniq)))
            plan.append({
                'rel': t, 'ext': ext, 'size': sz, 'cipher': tc, 'mode': mode,
                'action': 'install', 'cand': install_path, 'src': best['src'],
                'verdict': bestv, 'why': why, 'vwhy': bestvwhy, 'q': best['q'],
                'sha': best['sha'],
                'reason': '%s | %s | %s%s' % (
                    '路线C明文副本' if best['src'] == 'C' else '路线D雕刻件',
                    why, bestvwhy, multi),
            })
            stats['install'] += 1

    with open(a.out_plan, 'w', encoding='utf-8') as f:
        for r in plan:
            f.write('%s\t%s\t%s\t%s\t%s\n' % (
                r.get('cipher', ''), r.get('cand', ''), r.get('rel', ''),
                r.get('action', ''), r.get('reason', '').replace('\t', ' ')))
    json.dump(plan, open(a.out_plan + '.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('目标 %d, 分组 %d' % (len(targets), len(groups)))
    print('统计: %s' % dict(stats))
    print('装回条目按来源: %s' % dict(collections.Counter(
        r['src'] for r in plan if r['action'] == 'install')))
    print('装回条目按证实: %s' % dict(collections.Counter(
        r['verdict'] for r in plan if r['action'] == 'install')))


# ------------------------------------------------------------------ install
def trim_jpeg_tail(p):
    """JPEG 末尾若有多余字节（雕刻区间常见的残留），截断到最后一个 FFD9。

    返回 (是否截断, 说明)。判定用"尾部 4KB 内有 FFD9"而不是 endswith。
    """
    try:
        data = open(p, 'rb').read()
    except OSError:
        return False, '读不了'
    if not data.startswith(b'\xff\xd8'):
        return False, '不是 JPEG'
    i = data.rfind(b'\xff\xd9')
    if i < 0:
        return False, '没有 FFD9'
    if i + 2 == len(data):
        return False, '尾部本来就干净'
    with open(p, 'wb') as f:
        f.write(data[:i + 2])
    return True, '截掉尾部 %d 字节' % (len(data) - (i + 2))


def cmd_install(a):
    plan = json.load(open(a.plan, encoding='utf-8'))
    log = open(a.log, 'w', encoding='utf-8')
    add = skip = fail = 0
    for r in plan:
        if r['action'] != 'install':
            continue
        cipher = r['cipher']
        cand = r['cand']
        dst = cipher[:-len(CIPHER_SUF)]
        if not os.path.exists(cand):
            log.write('失败(候选不存在) %s\n' % dst)
            fail += 1
            continue
        if os.path.exists(dst):
            log.write('跳过(已存在) %s\n' % dst)
            skip += 1
            continue
        d = os.path.dirname(dst)
        if not os.path.isdir(d):
            log.write('跳过(目录不存在) %s\n' % dst)
            skip += 1
            continue
        tpl = ''
        for fn in sorted(os.listdir(d)):
            if fn.endswith(CIPHER_SUF):
                tpl = os.path.join(d, fn)
                break
        # JPEG 先裁掉尾部残留字节（不影响其它类型）
        trimnote = ''
        if dst.lower().endswith(('.jpg', '.jpeg')):
            t, why = trim_jpeg_tail(cand)
            trimnote = why if t else ''
        shutil.copyfile(cand, dst)
        if tpl:
            try:
                st = os.stat(tpl)
                os.chown(dst, st.st_uid, st.st_gid)
            except Exception as e:
                log.write('警告(chown) %s %s\n' % (dst, str(e)[:60]))
        try:
            os.chmod(dst, 0o666)
        except Exception:
            pass
        if tpl and os.path.exists(ACL):
            subprocess.call([ACL, '-copy', tpl, dst],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.call([ACL, '-add', dst,
                             'group:administrators:allow:rwxpdDaARWc--:----'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if sha256_file(cand) == sha256_file(dst):
            log.write('OK\t%s\t%s\t%s\t%s\n' % (dst, r.get('src', ''),
                                               r.get('verdict', ''), trimnote))
            add += 1
        else:
            log.write('失败(校验) %s\n' % dst)
            os.remove(dst)
            fail += 1
    log.close()
    print('装回: 新增 %d, 跳过 %d, 失败 %d' % (add, skip, fail))
    print('台账: %s' % a.log)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--home', required=True)
    p.add_argument('--targets', required=True)
    p.add_argument('--index')
    p.add_argument('--claim')
    p.add_argument('--out-plan', required=True)
    p.add_argument('--workdir')
    p.set_defaults(func=cmd_plan)
    q = sub.add_parser('install')
    q.add_argument('--plan', required=True)
    q.add_argument('--log', required=True)
    q.set_defaults(func=cmd_install)
    a = ap.parse_args()
    a.func(a)


if __name__ == '__main__':
    main()
