#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pdf_xref_repair.py —— PDF 就地修复：保留全部原始字节，只在末尾追加一张新的 xref 表。

为什么要有这个工具
------------------
雕刻出来的 PDF 原件常常**只坏在 xref**：文件头尾完整、对象/页树/Catalog 都在，
但 `startxref` 指向的位置对不上（因为原文件被分片存放，中间那段后来被别的数据覆盖，
或者雕刻区间没有从文件真正的开头开始）。阅读器会报"文件已损坏"或弹修复提示。

已有的 `pdf_rebuild.py` 走的是「扫描出全部对象 -> 重排 -> 重新写一个 PDF」。
这条路能出能打开的文件，但**会丢内容**：
  - `scan_objects()` 用 `data.find('endobj')` 找对象结尾，二进制流里混进 'endobj'
    字样时会提前截断，丢掉流数据；
  - 增量更新（incremental update）的 PDF 会只保留对象的**第一个**版本，
    追加的新版被丢掉。
实测一个 211747 字节的身份证扫描件，重建后只剩 103454 字节、1 个页面 —— 丢了一半。

本工具的思路相反：**一个原始字节都不动**，只在文件末尾追加
  xref / trailer / startxref / %%EOF
这在 PDF 规范里是合法的「增量更新」，阅读器以最后一个 startxref 为准。
于是原文件的全部内容都保住了，同时 xref 恢复正确。

什么时候用不了
--------------
- 文件里用了**对象流（ObjStm）**：压缩对象没法用经典 xref 表表达 -> 本工具会明确拒绝，
  改用 `pdf_rebuild.py` 全量重建。
- 原 trailer 里没有 /Root，或文件里找不到任何 /Type /Page 对象 -> 拒绝。

用法
----
  python3 pdf_xref_repair.py <文件.pdf> [输出.pdf]
  python3 pdf_xref_repair.py <目录>          # 目录里所有 .pdf 逐个修复，输出 <名>_修复.pdf
  python3 pdf_xref_repair.py <文件.pdf> --inplace

修完用 `pdf_validate.py <输出>` 复核（要 Catalog 齐全 + xref 偏移全对才算过）。
"""
import os
import re
import sys
import zlib

OBJ_RE = re.compile(rb'(?<![0-9])(\d+)\s+(\d+)\s+obj\b')
PAGE_RE = re.compile(rb'/Type\s*/Page(?![sA-Za-z])')
CAT_RE = re.compile(rb'/Type\s*/Catalog')


def obj_body(data, start):
    """稳健地取出一个间接对象的 body（body 含 stream 数据）。

    二进制流里可能混进 'endobj' 字样，所以先定位 stream/endstream 再找 endobj。
    """
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


def repair(data):
    """返回 (新字节, 说明)；失败返回 (None, 原因)。"""
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
        b, d = obj_body(data, m.start())
        if b is None:
            continue
        # ⚠️ PDF 常带**增量更新**：同一个对象号在文件里出现多次，**最后一次才是当前版本**。
        # 取第一次会读到旧版本（老图、老数据），必须取最后一次。
        real[num] = m.start()
        bodies[num] = d or b

    if any(b'/ObjStm' in (bodies.get(n) or b'') for n in real):
        return None, '含对象流(ObjStm)，需全量重建'

    cat_ok = (root_num in real and
              CAT_RE.search(bodies.get(root_num) or b'') is not None)
    pages = [n for n, d in bodies.items() if PAGE_RE.search(d or b'')]

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


def expand_objstm(dictpart):
    """把一个 ObjStm 对象流里的成员对象解出来，返回 [(对象号, body), ...]。"""
    sm = re.compile(rb'stream\r?\n').search(dictpart)
    if not sm:
        return []
    em = dictpart.find(b'endstream', sm.end())
    if em < 0:
        return []
    try:
        raw = zlib.decompress(dictpart[sm.end():em])
    except Exception:
        return []
    n_m = re.search(rb'/N\s+(\d+)', dictpart)
    f_m = re.search(rb'/First\s+(\d+)', dictpart)
    if not (n_m and f_m):
        return []
    n = int(n_m.group(1))
    first = int(f_m.group(1))
    head = raw[:first].split()
    out = []
    for i in range(n):
        try:
            onum = int(head[2 * i])
            ooff = int(head[2 * i + 1])
        except Exception:
            break
        nxt = int(head[2 * i + 3]) if 2 * i + 3 < len(head) else len(raw) - first
        out.append((onum, raw[first + ooff: first + nxt].strip()))
    return out


def rebuild_objstm(data):
    """含对象流(ObjStm)的 PDF：把对象流里的对象全解出来，重排成**经典 xref** 的新文件。

    经典 xref 表达不了 ObjStm 里的压缩对象，所以就地追加 xref 这条路走不通，
    只能整体重写。但要用**稳健**的对象切分（`obj_body`），否则二进制流里混进的
    'endobj' 字样会把对象提前截断、丢掉流数据（`pdf_rebuild.py` 的老问题）。

    对象编号保持不变，所以文件内部所有互相引用都不用改。
    """
    real = {}
    full = {}
    dicts = {}
    entries = []          # (位置, 对象号, body) —— 按文件位置排序后取每个号"最后一次"
    for m in OBJ_RE.finditer(data):
        num = int(m.group(1))
        b, d = obj_body(data, m.start())
        if b is None:
            continue
        if b'/ObjStm' in d:
            for onum, obody in expand_objstm(d):
                entries.append((m.start(), onum, obody))
            continue                      # 对象流本身不输出，成员摊平后重写
        entries.append((m.start(), num, b))
        real[num] = m.start()
        full[num] = b
        dicts[num] = d or b

    # ⚠️ 增量更新：同一个对象号出现多次时，**最后一次才是当前版本**。
    # 取第一次会读到旧版本（老图、老数据），PDF 会"能打开但内容是旧的"。
    objs = {}
    for pos, num, body in sorted(entries, key=lambda e: e[0]):
        objs[num] = body

    if not objs:
        return None, '没扫到对象'
    pages = [n for n, b in objs.items() if PAGE_RE.search(b)]
    cats = [n for n, b in objs.items() if CAT_RE.search(b)]
    maxnum = max(objs)

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
    # 页树
    pn = maxnum + 1
    cat = None
    if cats:
        cand = cats[0]
        m = re.search(rb'/Pages\s+(\d+)\s+0\s+R', objs[cand])
        if m and int(m.group(1)) in objs:
            cat = cand
    if cat is None:
        if not pages:
            return None, '没有页面对象'
        off_pn = len(out)
        out += b'%d 0 obj\n<< /Type /Pages /Kids [%s] /Count %d >>\nendobj\n' % (
            pn, b' '.join(b'%d 0 R' % p for p in pages), len(pages))
        offsets[pn] = off_pn
        off_cn = len(out)
        out += b'%d 0 obj\n<< /Type /Catalog /Pages %d 0 R >>\nendobj\n' % (pn + 1, pn)
        offsets[pn + 1] = off_cn
        cat = pn + 1
        maxnum = pn + 1
        # 给页面对象补 /Parent
        for p in pages:
            s = objs[p].rstrip()
            i = s.rfind(b'>>')
            if i < 0:
                continue
            objs[p] = s[:i] + b' /Parent %d 0 R ' % pn + s[i:] + b'\n'
    xref = len(out)
    out += b'xref\n0 %d\n' % (maxnum + 1)
    out += b'0000000000 65535 f \n'
    for i in range(1, maxnum + 1):
        if i in offsets:
            out += b'%010d 00000 n \n' % offsets[i]
        else:
            out += b'0000000000 65535 f \n'
    out += b'trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (
        maxnum + 1, cat, xref)
    return bytes(out), ('含对象流，全量重排为经典 xref（%d 个对象，%d 页，%d 字节）'
                        % (len(objs), len(pages), len(out)))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    inplace = '--inplace' in sys.argv
    if not args:
        sys.exit(__doc__)
    targets = []
    for a in args:
        if os.path.isdir(a):
            for dp, dns, fns in os.walk(a):
                for fn in sorted(fns):
                    if fn.lower().endswith('.pdf') and not fn.endswith('_修复.pdf'):
                        targets.append(os.path.join(dp, fn))
        else:
            targets.append(a)
    ok = bad = 0
    for p in targets:
        data = open(p, 'rb').read()
        out, why = repair(data)
        if out is None:
            out2, why2 = rebuild_objstm(data)
            if out2 is None:
                print('放弃 %-56s %s / %s' % (os.path.basename(p)[-54:], why, why2))
                bad += 1
                continue
            out, why = out2, why2
        if inplace:
            dst = p
        else:
            base = os.path.basename(p)
            if base.lower().endswith('.pdf'):
                base = base[:-4]
            dst = os.path.join(os.path.dirname(p), base + '_修复.pdf')
        with open(dst, 'wb') as f:
            f.write(out)
        print('修复 %-56s %s' % (os.path.basename(p)[-54:], why))
        ok += 1
    print('修复 %d, 放弃 %d' % (ok, bad))


if __name__ == '__main__':
    main()
