#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pdf_rebuild_plain.py —— 对**已经是明文**的 PDF 重建 xref / 页树。

场景：雕刻出来的原件，文件头尾都完整，但中间有一段被后来的写入覆盖，
导致 `startxref` 指向的偏移对不上、阅读器判"文件已损坏"。
这类文件本身内容是好的（对象、页面、Catalog 都在），
重新扫一遍间接对象、重排编号、重生成 xref 就能打开。

用法：
  python3 pdf_rebuild_plain.py <目录或文件...> [--out 输出目录]
输出：<原名>_重建.pdf（放在输出目录，默认与源同目录）
"""
import importlib.util
import os
import re
import sys

# 复用 skill 的 pdf_rebuild 里的对象扫描/构造函数
_spec = None
for cand in ('/tmp/c3r_zmm/pdf_rebuild.py',
             os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pdf_rebuild.py')):
    if os.path.exists(cand):
        _spec = cand
        break
if _spec is None:
    sys.exit('找不到 pdf_rebuild.py')
_sp = importlib.util.spec_from_file_location('prb', _spec)
prb = importlib.util.module_from_spec(_sp)
_sp.loader.exec_module(prb)

PAGE_RE = prb.PAGE_RE
CAT_RE = prb.CAT_RE


def rebuild(data, dst):
    objs = prb.scan_objects(data)
    if not objs:
        return None, '没扫到对象'
    pages = [n for n, b in objs.items() if PAGE_RE.search(b)]
    cats = [n for n, b in objs.items() if CAT_RE.search(b)]
    maxnum = max(objs)
    if cats and pages:
        cat = cats[0]
        catbody = objs[cat]
        m = re.search(rb'/Pages\s+(\d+)\s+0\s+R', catbody)
        pagesnode = int(m.group(1)) if m else None
        if pagesnode is None or pagesnode not in objs:
            pn = maxnum + 1
            objs[pn] = (b'<< /Type /Pages /Kids ['
                        + b' '.join(b'%d 0 R' % p for p in pages)
                        + b'] /Count %d >>' % len(pages))
            catbody = re.sub(rb'/Type\s*/Catalog', rb'/Type /Catalog /Pages %d 0 R' % pn,
                             catbody, count=1)
            objs[cat] = catbody
            for p in pages:
                objs[p] = prb.add_parent(objs[p], pn)
        out = prb.build_pdf(objs, pages, cat)
    else:
        if not pages:
            return None, '没有页面对象'
        cat = maxnum + 1
        pn = maxnum + 2
        objs[pn] = (b'<< /Type /Pages /Kids ['
                    + b' '.join(b'%d 0 R' % p for p in pages)
                    + b'] /Count %d >>' % len(pages))
        objs[cat] = b'<< /Type /Catalog /Pages %d 0 R >>' % pn
        for p in pages:
            objs[p] = prb.add_parent(objs[p], pn)
        out = prb.build_pdf(objs, pages, cat)
    with open(dst, 'wb') as f:
        f.write(out)
    return len(pages), 'ok'


def main():
    args = sys.argv[1:]
    outdir = None
    if '--out' in args:
        i = args.index('--out')
        outdir = args[i + 1]
        del args[i:i + 2]
    files = []
    for a in args:
        if os.path.isdir(a):
            for dp, dns, fns in os.walk(a):
                for fn in sorted(fns):
                    if fn.lower().endswith('.pdf') and not fn.endswith('_重建.pdf'):
                        files.append(os.path.join(dp, fn))
        elif a.lower().endswith('.pdf'):
            files.append(a)
    ok = bad = 0
    for p in files:
        d = outdir or os.path.dirname(p)
        base = os.path.basename(p)
        if base.endswith('.pdf'):
            base = base[:-4]
        dst = os.path.join(d, base + '_重建.pdf')
        try:
            data = open(p, 'rb').read()
            npages, why = rebuild(data, dst)
            if npages:
                print('重建 %-56s %d 页  %d 字节' % (os.path.basename(p)[:54], npages,
                                                      os.path.getsize(dst)))
                ok += 1
            else:
                print('放弃 %-56s %s' % (os.path.basename(p)[:54], why))
                bad += 1
                if os.path.exists(dst):
                    os.remove(dst)
        except Exception as ex:
            print('失败 %-56s %s' % (os.path.basename(p)[:54], str(ex)[:60]))
            bad += 1
    print('重建成功 %d, 放弃 %d' % (ok, bad))


if __name__ == '__main__':
    main()
