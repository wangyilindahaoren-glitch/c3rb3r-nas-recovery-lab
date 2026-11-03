# -*- coding: utf-8 -*-
"""把抢救回来的 xlsx 内部成员（sheet XML / sharedStrings）还原成可读的 xlsx + csv。

输入：2_抢救成员 目录（每个 <原名>.xlsx/ 下是 xl/worksheets/sheetN.xml 等成员）
输出：同级的 <原名>_抢救还原.xlsx 和 <原名>_抢救还原_<sheet>.csv
"""
import io
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
RNS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def col_of(ref):
    m = re.match(r'([A-Z]+)', ref or '')
    return m.group(1) if m else ''


def col_idx(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def parse_shared_strings(path):
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return []
    out = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    for si in root.findall(NS + 'si'):
        parts = []
        for t in si.iter(NS + 't'):
            parts.append(t.text or '')
        out.append(''.join(parts))
    return out


def parse_sheet(path, sst):
    """返回 [[cell,...], ...] 二维表"""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    rows = []
    for row in root.iter(NS + 'row'):
        cells = {}
        for c in row.findall(NS + 'c'):
            ref = c.get('r') or ''
            t = c.get('t') or 'n'
            v = c.find(NS + 'v')
            isel = c.find(NS + 'is')
            if t == 's' and v is not None and v.text is not None:
                try:
                    val = sst[int(v.text)]
                except (ValueError, IndexError):
                    val = v.text
            elif t == 'inlineStr' and isel is not None:
                val = ''.join(x.text or '' for x in isel.iter(NS + 't'))
            elif v is not None:
                val = v.text
            else:
                val = ''
            cells[col_idx(col_of(ref))] = val
        if cells:
            width = max(cells) + 1
            arr = [''] * width
            for k, val in cells.items():
                arr[k] = val
            rows.append(arr)
        else:
            rows.append([])
    return rows


def sheet_names(wb_path):
    try:
        with open(wb_path, 'rb') as f:
            root = ET.fromstring(f.read())
    except Exception:
        return []
    out = []
    for sh in root.iter(NS + 'sheet'):
        out.append(sh.get('name') or '')
    return out


def build_xlsx(path, sheets):
    """sheets: [(name, rows)]"""
    try:
        from openpyxl import Workbook
    except ImportError:
        return False
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets:
        ws = wb.create_sheet(title=(name or 'Sheet')[:31])
        for r in rows:
            ws.append([('' if v is None else v) for v in r])
    wb.save(path)
    return True


def main():
    root = sys.argv[1]
    outroot = sys.argv[2] if len(sys.argv) > 2 else root
    os.makedirs(outroot, exist_ok=True)
    groups = []
    for dp, dns, fns in os.walk(root):
        if os.path.isdir(os.path.join(dp, 'xl', 'worksheets')):
            groups.append(dp)
    print('找到容器目录:', len(groups))
    total_sheets = 0
    for g in groups:
        base = os.path.basename(g)
        if base.endswith('.L0CK3D'):
            base = base[:-len('.L0CK3D')]
        sst = parse_shared_strings(os.path.join(g, 'xl', 'sharedStrings.xml'))
        names = sheet_names(os.path.join(g, 'xl', 'workbook.xml'))
        wsdir = os.path.join(g, 'xl', 'worksheets')
        sheets = []
        if os.path.isdir(wsdir):
            for fn in sorted(os.listdir(wsdir)):
                if not fn.endswith('.xml'):
                    continue
                rows = parse_sheet(os.path.join(wsdir, fn), sst)
                if not rows:
                    continue
                idx = re.match(r'sheet(\d+)\.xml', fn)
                nm = ''
                if idx and names:
                    k = int(idx.group(1)) - 1
                    if 0 <= k < len(names):
                        nm = names[k]
                if not nm:
                    nm = fn[:-4]
                sheets.append((nm, rows))
        if not sheets:
            continue
        total_sheets += len(sheets)
        outxlsx = os.path.join(outroot, base + '_抢救还原.xlsx')
        build_xlsx(outxlsx, sheets)
        for nm, rows in sheets:
            csvp = os.path.join(outroot, '%s_抢救还原_%s.csv' % (base, nm.replace('/', '_')))
            with io.open(csvp, 'w', encoding='utf-8-sig', newline='') as f:
                import csv
                w = csv.writer(f)
                for r in rows:
                    w.writerow(r)
        print('  %-50s 工作表 %d  行数 %s'
              % (base, len(sheets), [len(r) for _, r in sheets]))
    print('共还原工作表:', total_sheets)


if __name__ == '__main__':
    main()
