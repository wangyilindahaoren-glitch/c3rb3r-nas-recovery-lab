# -*- coding: utf-8 -*-
"""_fix_crlf.py —— 修正 xlsx 里的换行符失真。

openpyxl 写单元格时把 LF 转成 CRLF，而 XML 解析器又把字面 CRLF 归一化成 LF，
于是「源里用裸 CR 分隔」的格子会变成 LF。修法：把失真单元格的 <t> 内容里
的 CR/LF 全部转成数字实体（&#13; / &#10;），XML 解析器就原样还原。
"""
import os, re, sys, zipfile, shutil

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import osheet_to_xlsx as M                      # noqa: E402
from openpyxl import load_workbook              # noqa: E402
from openpyxl.utils import get_column_letter    # noqa: E402


def source_cells(osheet):
    """{sheet_title: {(r,c): 字符串值}}"""
    data = open(osheet, 'rb').read()
    _h, entries = M.parse_strict(data)
    index = None
    shards = {}
    for pos, X, name, payload in entries:
        if name.endswith('text/index') and index is None:
            index = M.DEC.raw_decode(payload.decode('utf-8', 'replace'))[0]
        elif 'text/sh_' in name:
            shards[name.rsplit('/', 1)[-1]] = payload
    ids = [s for s in (index.get('order') or []) if s in shards]
    for sid in sorted(shards, key=lambda s: (len(s), s)):
        if sid not in ids:
            ids.append(sid)
    used = set()
    out = {}
    for sid in ids:
        title = M.sheet_title(index, sid, used)
        obj, _c = M.parse_shard(shards[sid])
        cells = obj.get('cells') or {}
        d = {}
        for r, row in cells.items():
            if not isinstance(row, dict):
                continue
            for c, v in row.items():
                if not isinstance(v, dict):
                    continue
                val = M.unwrap(v.get('v'))
                if isinstance(val, str):
                    d[(int(r), int(c))] = val
        out[title] = d
    return out


def main():
    osheet, xlsx = sys.argv[1], sys.argv[2]
    src = source_cells(osheet)
    wb = load_workbook(xlsx)
    sheet_of = {}
    for title, d in src.items():
        for (r, c), val in d.items():
            if '\r' not in val and '\n' not in val:
                continue
            ws = wb[title]
            got = ws.cell(row=r + 1, column=c + 1).value
            if got != val:
                sheet_of.setdefault(title, {})[get_column_letter(c + 1) + str(r + 1)] = val
    wb.close()
    total = sum(len(v) for v in sheet_of.values())
    print('需要修正的单元格: %d （涉及 %d 个工作表）' % (total, len(sheet_of)))
    if not total:
        return 0
    # 工作表标题 -> sheetN.xml（从 workbook.xml + rels 解析，别假设编号顺序）
    z = zipfile.ZipFile(xlsx)
    wbxml = z.read('xl/workbook.xml').decode('utf-8')
    relxml = z.read('xl/_rels/workbook.xml.rels').decode('utf-8')
    z.close()
    rid2target = {}
    for rm in re.finditer(r'<Relationship\b[^>]*/>', relxml):
        tag = rm.group(0)
        idm = re.search(r'\bId="([^"]+)"', tag)
        tgm = re.search(r'\bTarget="([^"]+)"', tag)
        if idm and tgm:
            rid2target[idm.group(1)] = tgm.group(1)
    file2title = {}
    for sm in re.finditer(r'<sheet\b[^>]*/>', wbxml):
        tag = sm.group(0)
        nm = re.search(r'\bname="([^"]+)"', tag)
        idm = re.search(r'\br:id="([^"]+)"', tag)
        if not (nm and idm):
            continue
        tgt = rid2target.get(idm.group(1), '')
        tgt = tgt.split('/')[-1]
        if tgt:
            file2title['xl/worksheets/' + tgt] = nm.group(1)
    print('sheet 文件 -> 标题:', file2title)
    # 逐个 sheet xml 处理
    tmp = xlsx + '.tmp'
    zin = zipfile.ZipFile(xlsx)
    zout = zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED)
    patched = 0
    for it in zin.infolist():
        data = zin.read(it.filename)
        if it.filename.startswith('xl/worksheets/sheet'):
            title = file2title.get(it.filename)
            fixes = sheet_of.get(title)
            if fixes:
                t = data.decode('utf-8')
                for ref, val in fixes.items():
                    pat = re.compile(r'(<c r="%s"[^>]*>.*?<t[^>]*>)(.*?)(</t>)' % re.escape(ref), re.S)
                    mm = pat.search(t)
                    if not mm:
                        print('  !! 找不到单元格 %s in %s' % (ref, title))
                        continue
                    new = mm.group(2).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    new = new.replace('\r', '&#13;').replace('\n', '&#10;')
                    t = t[:mm.start()] + mm.group(1) + new + mm.group(3) + t[mm.end():]
                    patched += 1
                data = t.encode('utf-8')
        zout.writestr(it, data)
    zout.close()
    zin.close()
    os.replace(tmp, xlsx)
    print('已修正 %d 个单元格' % patched)
    return 0


if __name__ == '__main__':
    sys.exit(main())
