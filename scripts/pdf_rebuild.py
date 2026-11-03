#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 重建（改进版）：从 .L0CK3D 密文里重建能打开的 PDF。

上一版的问题：把 /Root 写死成 "1 0 R"，而原文的 Catalog 往往不是对象 1、
或者根本不在明文区 —— 于是 PDF 没有 Catalog，阅读器直接拒绝打开。

这一版：
  1. 按加密模式取出明文区，**保持原文件偏移**（对象表/xref 的偏移量才对得上）；
  2. 扫描出全部间接对象，顺带解开 /Type /ObjStm 对象流里的对象；
  3. 找到 /Type /Page 的页面对象；
  4. Catalog 缺失就**造一个**：新的 Catalog + 新的 Pages 树，
     并把找到的页面对象重新写一遍、补上 /Parent；
  5. 重排全部对象、生成正确的 xref 与 trailer。

用法：python3 pdf_rebuild2.py <用户目录...>
输出：<用户目录>/勒索恢复_20261030/3_重建PDF/<原路径>
"""
import json
import os
import re
import struct
import sys
import zlib

TAIL = 588
STAMP = '勒索恢复_20261030'
OBJ_RE = re.compile(rb'(?<![0-9])(\d+)\s+(\d+)\s+obj\b')
PAGE_RE = re.compile(rb'/Type\s*/Page(?![sA-Za-z])')
CAT_RE = re.compile(rb'/Type\s*/Catalog')


def tail_meta(p):
    sz = os.path.getsize(p)
    with open(p, 'rb') as f:
        f.seek(sz - TAIL)
        t = f.read(TAIL)
    if len(t) < TAIL:
        return None
    return t[572], sz - TAIL


def enc_ranges(mode, n):
    if mode == 0x24:
        return [(0, n)]
    if mode == 0x26:
        return [(0, min(n, 1 << 20))]
    if mode == 0x25:
        w = 4 * (n // 100)
        gap = (n - 3 * w) // 2
        if w <= 0 or gap < 0:
            return [(0, n)]
        return [(0, w), (w + gap, 2 * w + gap), (2 * (w + gap), 3 * w + 2 * gap)]
    return None


def plain_bytes(path, mode, n):
    """取明文区，保持原偏移（加密区填 0）"""
    data = bytearray(n)
    enc = enc_ranges(mode, n) or []
    with open(path, 'rb') as f:
        pos = 0
        for s, e in enc:
            if s > pos:
                f.seek(pos)
                data[pos:min(e, n)] = f.read(min(e, n) - pos)
            pos = max(pos, e)
        if pos < n:
            f.seek(pos)
            data[pos:n] = f.read(n - pos)
    return bytes(data)


def scan_objects(data):
    """返回 {对象号: (body, 是否有流)}；顺带解对象流"""
    objs = {}
    for m in OBJ_RE.finditer(data):
        num = int(m.group(1))
        start = m.start()
        end = data.find(b'endobj', m.end())
        if end < 0:
            continue
        body = data[m.end():end]
        if num not in objs:
            objs[num] = body
    # 对象流里的对象
    extra = {}
    for num, body in list(objs.items()):
        if b'/ObjStm' not in body:
            continue
        sm = re.search(rb'stream\r?\n', body)
        if not sm:
            continue
        st = sm.end()
        en = body.find(b'endstream', st)
        if en < 0:
            continue
        try:
            raw = zlib.decompress(body[st:en])
        except Exception:
            continue
        n_m = re.search(rb'/N\s+(\d+)', body)
        first_m = re.search(rb'/First\s+(\d+)', body)
        if not n_m or not first_m:
            continue
        n = int(n_m.group(1))
        first = int(first_m.group(1))
        head = raw[:first].split()
        for i in range(n):
            try:
                onum = int(head[2 * i])
                ooff = int(head[2 * i + 1])
            except Exception:
                break
            nxt = int(head[2 * i + 3]) if 2 * i + 3 < len(head) else len(raw) - first
            if onum not in objs and onum not in extra:
                extra[onum] = raw[first + ooff: first + nxt].strip()
    for k, v in extra.items():
        objs.setdefault(k, v)
    return objs


def build_pdf(objs, pages, catalog_num):
    """重排对象，生成正确的 xref/trailer"""
    out = bytearray(b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n')
    offsets = {}
    for num in sorted(objs):
        body = objs[num]
        offsets[num] = len(out)
        out += b'%d 0 obj\n' % num
        out += body
        if not body.rstrip().endswith(b'endobj'):
            out += b'\nendobj\n'
        else:
            out += b'\n'
    xref = len(out)
    maxnum = max(objs) if objs else 0
    out += b'xref\n0 %d\n' % (maxnum + 1)
    out += b'0000000000 65535 f \n'
    for i in range(1, maxnum + 1):
        if i in offsets:
            out += b'%010d 00000 n \n' % offsets[i]
        else:
            out += b'0000000000 65535 f \n'
    out += b'trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (
        maxnum + 1, catalog_num, xref)
    return bytes(out)


def add_parent(body, parent_num):
    """给页面对象补 /Parent"""
    s = body.rstrip()
    i = s.rfind(b'>>')
    if i < 0:
        return body
    return s[:i] + b' /Parent %d 0 R ' % parent_num + s[i:] + b'\n'


def rebuild_one(enc_path, dst_path):
    meta = tail_meta(enc_path)
    if not meta:
        return None, '尾标异常'
    mode, n = meta
    if mode == 0x24:
        return None, '整文件加密，无明文'
    data = plain_bytes(enc_path, mode, n)
    objs = scan_objects(data)
    if not objs:
        return None, '没扫到对象'
    pages = [n_ for n_, b in objs.items() if PAGE_RE.search(b)]
    cats = [n_ for n_, b in objs.items() if CAT_RE.search(b)]
    maxnum = max(objs)
    if cats and pages:
        cat = cats[0]
        # 原 Catalog 在，但它的 /Pages 可能缺失 → 也要补页树
        catbody = objs[cat]
        m = re.search(rb'/Pages\s+(\d+)\s+0\s+R', catbody)
        pagesnode = int(m.group(1)) if m else None
        if pagesnode is None or pagesnode not in objs:
            pn = maxnum + 1
            objs[pn] = b'<< /Type /Pages /Kids [' + b' '.join(
                b'%d 0 R' % p for p in pages) + b'] /Count %d >>' % len(pages)
            catbody = re.sub(rb'/Type\s*/Catalog', b'/Type /Catalog /Pages %d 0 R' % pn,
                             catbody, count=1)
            objs[cat] = catbody
            for p in pages:
                objs[p] = add_parent(objs[p], pn)
            cat = cat
        out = build_pdf(objs, pages, cat)
    else:
        # 没有可用的 Catalog → 造
        cat = maxnum + 1
        pn = maxnum + 2
        if not pages:
            return None, '没有页面对象'
        objs[pn] = b'<< /Type /Pages /Kids [' + b' '.join(
            b'%d 0 R' % p for p in pages) + b'] /Count %d >>' % len(pages)
        objs[cat] = b'<< /Type /Catalog /Pages %d 0 R >>' % pn
        for p in pages:
            objs[p] = add_parent(objs[p], pn)
        out = build_pdf(objs, pages, cat)
    with open(dst_path, 'wb') as f:
        f.write(out)
    return len(pages), 'ok'


def main():
    for root in sys.argv[1:]:
        home = root
        outroot = os.path.join(home, STAMP, '3_重建PDF')
        for dp, dns, fns in os.walk(home):
            if STAMP in dp or '#recycle' in dp or '@eaDir' in dp:
                dns[:] = []
                continue
            for fn in sorted(fns):
                if not fn.endswith('.L0CK3D') or not fn.lower().endswith('.pdf.l0ck3d'):
                    continue
                enc = os.path.join(dp, fn)
                rel = os.path.relpath(enc, home)
                dst = os.path.join(outroot, rel[:-7])
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    npages, why = rebuild_one(enc, dst)
                except Exception as ex:
                    print('失败 %-50s %s' % (rel[-50:], ex))
                    continue
                if npages:
                    print('重建 %-52s %d 页  %d 字节' % (rel[:-7][-52:], npages,
                                                        os.path.getsize(dst)))
                else:
                    print('放弃 %-52s %s' % (rel[:-7][-52:], why))
                    if os.path.exists(dst):
                        os.remove(dst)


if __name__ == '__main__':
    main()
