#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deep_validate.py —— 对已装回的原件做"深度"可打开性校验。

比 `validate_installed.py` 严格一档：
  - xlsx/docx/zip：每个成员都试着用 XML 解析器解析（ElementTree），
    并统计工作表数、非空单元格数 —— 能解析说明 Word/Excel 真能打开；
  - pdf：Catalog + xref 逐条核对 + 每页 /Contents 在位 + 统计可提取文本的页数；
  - png/jpeg：头尾完整 + 尺寸。

输出：统计 + 明细 JSON + 每个文件的"能不能开"结论。

用法：python3 zmm_deep_validate.py <pairs.tsv> <out.json>
      pairs.tsv 每行: cipher \t 已装回的文件路径 \t 相对路径
"""
import json
import os
import re
import struct
import sys
import zipfile
import xml.etree.ElementTree as ET

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
PAGE_RE = re.compile(rb'/Type\s*/Page(?![sA-Za-z])')
OBJ_RE = re.compile(rb'(?<![0-9])(\d+)\s+(\d+)\s+obj\b')


def check_ooxml(p):
    """zip 族：全成员可读 + XML 可解析 + 统计工作表/非空格数。"""
    try:
        z = zipfile.ZipFile(p)
    except Exception as e:
        return False, 'zip 打不开: %s' % str(e)[:80], {}
    names = z.namelist()
    if not names:
        return False, '空容器', {}
    bad = z.testzip()
    if bad:
        return False, '成员 CRC 错: %s' % bad, {}
    xml_bad = []
    sheets = 0
    cells = 0
    for n in names:
        if not n.lower().endswith('.xml'):
            continue
        try:
            data = z.read(n)
        except Exception:
            xml_bad.append(n)
            continue
        try:
            root = ET.fromstring(data)
        except Exception:
            xml_bad.append(n)
            continue
        if '/worksheets/' in n or n.startswith('xl/worksheets/'):
            sheets += 1
            for c in root.iter(NS + 'c'):
                v = c.find(NS + 'v')
                isel = c.find(NS + 'is')
                if (v is not None and (v.text or '').strip()) or isel is not None:
                    cells += 1
    if xml_bad:
        return False, 'XML 解析失败 %d 个: %s' % (
            len(xml_bad), ','.join(xml_bad[:3])), {'sheets': sheets, 'cells': cells}
    return True, '%d 个成员全过，%d 个工作表 XML 可解析，%d 个非空单元格' % (
        len(names), sheets, cells), {'sheets': sheets, 'cells': cells}


def check_pdf(p):
    data = open(p, 'rb').read()
    if not data.startswith(b'%PDF-'):
        return False, '没有 %PDF- 头', {}
    if b'%%EOF' not in data[-4096:]:
        return False, '结尾没有 %%EOF', {}
    ms = list(re.finditer(rb'startxref\s+(\d+)', data[-2048:]))
    if not ms:
        return False, '没有 startxref', {}
    off = int(ms[-1].group(1))
    if off >= len(data):
        return False, 'startxref 越界', {}
    if not data[off:off + 4].startswith(b'xref'):
        return False, 'startxref 不指向 xref 表', {}
    if b'/Root' not in data:
        return False, '没有 /Root', {}
    if b'/Type/Catalog' not in data and b'/Type /Catalog' not in data:
        return False, '没有 Catalog（阅读器必然拒绝打开）', {}
    # xref 偏移逐条核对
    em = re.search(rb'xref\s+0\s+(\d+)\s+', data[off:])
    bad = 0
    if em:
        n = int(em.group(1))
        ent = data[off + em.end(): off + em.end() + 20 * n]
        for i in range(1, n):
            l = ent[i * 20:(i + 1) * 20]
            if len(l) < 18 or l[17:18] != b'n':
                continue
            o = int(l[0:10])
            if not re.match(rb'%d\s+0\s+obj' % i, data[o:o + 24]):
                bad += 1
    # 页面与内容流
    nums = set(int(m.group(1)) for m in OBJ_RE.finditer(data))
    pages = 0
    with_content = 0
    for m in OBJ_RE.finditer(data):
        end = data.find(b'endobj', m.end())
        if end < 0:
            continue
        body = data[m.end():end]
        if not PAGE_RE.search(body):
            continue
        pages += 1
        refs = [int(x) for x in re.findall(rb'/Contents\s+(\d+)\s+0\s+R', body)]
        if not refs:
            for arr in re.findall(rb'/Contents\s*\[([^\]]*)\]', body):
                refs += [int(x) for x in re.findall(rb'(\d+)\s+0\s+R', arr)]
        if refs and all(r in nums for r in refs):
            with_content += 1
    if pages == 0:
        return False, '没有页面对象', {}
    if bad:
        return False, 'xref 错位 %d 条' % bad, {'pages': pages}
    return True, '%d 页（%d 页有内容流），xref 全对' % (pages, with_content), {
        'pages': pages, 'pages_with_content': with_content}


def check_img(p, kind):
    sz = os.path.getsize(p)
    with open(p, 'rb') as f:
        head = f.read(32)
        f.seek(max(0, sz - 4096))
        tail = f.read()
    if kind == 'png':
        if not head.startswith(b'\x89PNG\r\n\x1a\n'):
            return False, '不是 PNG', {}
        if b'IEND' not in tail:
            return False, '缺 IEND', {}
        w, h = struct.unpack('>II', head[16:24])
        return True, '%dx%d' % (w, h), {'w': w, 'h': h}
    if not head.startswith(b'\xff\xd8'):
        return False, '不是 JPEG', {}
    if b'\xff\xd9' not in tail:
        return False, '缺 FFD9', {}
    return True, 'JPEG 头尾完整', {}


def main():
    pairs_tsv, out_json = sys.argv[1], sys.argv[2]
    items = []
    stats = {'ok': 0, 'bad': 0, 'skip': 0}
    ext_seen = {}
    with open(pairs_tsv, encoding='utf-8') as f:
        for ln in f:
            ln = ln.rstrip('\n')
            if not ln:
                continue
            parts = ln.split('\t')
            p = parts[1] if len(parts) > 1 else parts[0]
            rel = parts[2] if len(parts) > 2 else p
            ext = os.path.splitext(p)[1].lower()
            rec = {'path': p, 'rel': rel, 'ext': ext}
            try:
                if not os.path.exists(p):
                    raise ValueError('装回件不存在')
                if ext in ('.xlsx', '.docx', '.zip', '.xlsm', '.pptx'):
                    ok, why, info = check_ooxml(p)
                elif ext == '.pdf':
                    ok, why, info = check_pdf(p)
                elif ext == '.png':
                    ok, why, info = check_img(p, 'png')
                elif ext in ('.jpg', '.jpeg'):
                    ok, why, info = check_img(p, 'jpg')
                else:
                    rec['verdict'] = 'skip'
                    rec['why'] = ext
                    stats['skip'] += 1
                    items.append(rec)
                    continue
                rec['verdict'] = 'ok' if ok else 'bad'
                rec['why'] = why
                rec['info'] = info
                stats['ok' if ok else 'bad'] += 1
                if ok:
                    ext_seen.setdefault(ext, []).append((rel, why))
            except Exception as ex:
                rec['verdict'] = 'bad'
                rec['why'] = str(ex)[:120]
                stats['bad'] += 1
            items.append(rec)
    json.dump({'stats': stats, 'items': items},
              open(out_json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('深度校验: %s' % json.dumps(stats, ensure_ascii=False))
    for it in items:
        if it['verdict'] == 'bad':
            print('  BAD  %-8s %-60s %s' % (it['ext'], it['rel'][-58:], it['why']))
    print()
    print('== 按扩展名的抽样明细 ==')
    for ext, lst in sorted(ext_seen.items()):
        print('  %-8s %d 个' % (ext, len(lst)))
        for rel, why in lst[:3]:
            print('       %-56s %s' % (rel[-54:], why))


if __name__ == '__main__':
    main()
