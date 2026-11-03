#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 WPS OSheet(.osheet) 容器**合并还原**成一个多工作表的 .xlsx。

背景
----
C3RB3R 之后，OSheet 容器里每个工作表是一个独立的 `text/sh_N` 条目。
上次抢救把每个分片单独导成 CSV，结果一个表格炸成十几个文件，
工作表名字、顺序、样式全丢了。

本脚本把整个容器还原成**一个 xlsx**：
  * 每个工作表 = 一个标签页，名字和顺序取自容器的 text/index；
  * 样式按容器的样式表还原：字体 / 填充 / 边框 / 数字格式 / 对齐 /
    合并单元格 / 行高 / 列宽；
  * 表头行加粗冻结 + 自动筛选，列宽按内容自适应（"美化"部分）；
  * 日期按数字格式还原成真正的日期，公式保留为公式并让 Excel 重算；
  * 截断的分片（勒索加密区切断了 JSON）按行抢救，能救多少救多少。

用法
----
    python3 osheet_to_xlsx.py <输入.osheet> <输出.xlsx> [--csv <目录>]

依赖 openpyxl —— 在本地机器上跑（NAS 的 Python 没有第三方库）。
"""
import datetime
import json
import os
import re
import sys

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

MAGIC = b'\x78\x00\x00\x00'
BS = chr(92)
DEC = json.JSONDecoder()


# --------------------------------------------------------------------------
# 容器解析
# --------------------------------------------------------------------------
def parse_strict(data):
    """严格解析 OSheet 容器，返回条目列表 [(pos, X, name, payload)]。"""
    if data[:4] != MAGIC:
        raise ValueError('不是 OSheet 容器（magic 不对）')
    pos = 4
    header = []
    while True:
        if pos + 9 <= len(data) and data[pos + 4] in (0x05, 0x02) and \
                (data[pos + 5:pos + 8] == b'@C_' or data[pos + 5:pos + 9] == b'text'):
            break
        if pos >= len(data):
            break
        tag = data[pos]
        if tag == 0x00:
            # 旧版 WPS（schema=2）在头部字段之间多一个 0x00 填充字节，跳过
            pos += 1
            continue
        e = data.find(b'\x00', pos + 1)
        if e < 0:
            raise ValueError('头部 key 截断')
        key = data[pos + 1:e].decode('latin1')
        p = e + 1
        if tag == 0x08:
            ln = data[p]
            p += 1
        else:
            ln = int.from_bytes(data[p:p + 4], 'little')
            p += 4
        header.append((tag, key, data[p:p + ln]))
        p += ln
        if p < len(data) and data[p] == 0:
            p += 1
        pos = p
    entries = []
    while pos < len(data):
        if pos + 9 > len(data):
            raise ValueError('条目头截断')
        X = int.from_bytes(data[pos:pos + 4], 'little')
        tag = data[pos + 4]
        if tag not in (0x05, 0x02):
            raise ValueError('条目 tag 非法')
        e = data.find(b'\x00', pos + 5)
        name = data[pos + 5:e].decode('latin1')
        p = e + 1
        ln = int.from_bytes(data[p:p + 4], 'little')
        p += 4
        if p + 1 + ln > len(data):
            raise ValueError('条目数据越界')
        if tag == 0x05:
            import zlib
            d = zlib.decompressobj()
            payload = d.decompress(data[p + 1:p + 1 + ln]) + d.flush()
        else:
            payload = data[p:p + ln]
        entries.append((pos, X, name, payload))
        pos += X
        if pos < len(data) and data[pos + 4:pos + 5] == b'\x08':
            break
    return header, entries


def grab_array(text, key):
    """从一段 JSON 文本里抠出某个 key 的数组（容忍尾部截断）。"""
    i = text.find('"%s"' % key)
    if i < 0:
        return None
    j = text.find('[', i)
    if j < 0:
        return None
    depth = 0
    instr = False
    esc = False
    for k in range(j, len(text)):
        ch = text[k]
        if esc:
            esc = False
            continue
        if ch == BS:
            esc = True
            continue
        if ch == '"':
            instr = not instr
            continue
        if instr:
            continue
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[j:k + 1], strict=False)
                except Exception:
                    return None
    return None


def salvage_cells(text):
    """分片 JSON 被截断时，按行抢救 cells 里的完整行。"""
    i = text.find('"cells"')
    if i < 0:
        return {}
    j = text.find('{', i)
    if j < 0:
        return {}
    cells = {}
    k = j + 1
    n = len(text)
    while k < n:
        ch = text[k]
        if ch in ' \t\r\n,':
            k += 1
            continue
        if ch == '}':
            break
        if ch != '"':
            break
        try:
            key, end = DEC.raw_decode(text[k:])
        except Exception:
            break
        k += end
        if k >= n or text[k] != ':':
            break
        k += 1
        try:
            row, end = DEC.raw_decode(text[k:])
        except Exception:
            # 这一行被截断了 —— 逐格再救一次
            row = salvage_row(text[k:])
            if not row:
                break
            cells[key] = row
            break
        cells[key] = row
        k += end
    return cells


def salvage_row(text):
    """截断行里逐个单元格救（只救能完整解析的）。"""
    row = {}
    k = text.find('{')
    if k < 0:
        return row
    k += 1
    n = len(text)
    while k < n:
        ch = text[k]
        if ch in ' \t\r\n,':
            k += 1
            continue
        if ch == '}':
            break
        if ch != '"':
            break
        try:
            key, end = DEC.raw_decode(text[k:])
        except Exception:
            break
        k += end
        if k >= n or text[k] != ':':
            break
        k += 1
        try:
            val, end = DEC.raw_decode(text[k:])
        except Exception:
            break
        row[key] = val
        k += end
    return row


def parse_shard(payload):
    """返回 (shard 对象, 是否完整)。"""
    text = payload.decode('utf-8', 'replace')
    try:
        obj = DEC.raw_decode(text)[0]
        if isinstance(obj, dict):
            return obj, True
    except Exception:
        pass
    # 退化路径：整个 JSON 被加密区截断
    obj = {}
    for key in ('rowCount', 'colCount'):
        m = re.search(r'"%s"\s*:\s*(\d+)' % key, text[:4000])
        if m:
            obj[key] = int(m.group(1))
    obj['cells'] = salvage_cells(text)
    return obj, False


# --------------------------------------------------------------------------
# 样式
# --------------------------------------------------------------------------
def rgb(color):
    if not color:
        return None
    c = str(color).strip().lstrip('#').upper()
    if len(c) == 6:
        return 'FF' + c
    if len(c) == 8:
        return c
    return None


class StyleBook(object):
    """容器里的样式表。styles 每行 = [边框, 字体, 填充, 数字格式, 水平对齐, 垂直对齐, ?]"""

    def __init__(self, style_payload):
        text = style_payload.decode('utf-8', 'replace') if style_payload else ''
        self.styles = grab_array(text, 'styles') or []
        self.fonts = grab_array(text, 'fonts') or []
        self.fills = grab_array(text, 'fills') or []
        self.borders = grab_array(text, 'borders') or []
        self.aligns = grab_array(text, 'alignments') or []
        self.nfs = grab_array(text, 'numberFormats') or []
        self._cache = {}

    def xf(self, si):
        if si is None:
            return None
        if si in self._cache:
            return self._cache[si]
        out = {}
        try:
            row = self.styles[si]
        except Exception:
            row = None
        if not row or len(row) < 6:
            self._cache[si] = out
            return out
        b, f, fi, nf, ah, av = row[0], row[1], row[2], row[3], row[4], row[5]
        # 字体
        if f < len(self.fonts) and isinstance(self.fonts[f], dict):
            fd = self.fonts[f]
            kw = {}
            if fd.get('n'):
                kw['name'] = fd['n']
            if fd.get('sz'):
                kw['size'] = float(fd['sz'])
            if fd.get('b'):
                kw['bold'] = True
            if fd.get('i'):
                kw['italic'] = True
            if fd.get('u'):
                kw['underline'] = 'single'
            c = rgb(fd.get('c'))
            if c:
                kw['color'] = c
            if kw:
                out['font'] = Font(**kw)
        # 填充
        if fi < len(self.fills) and isinstance(self.fills[fi], dict):
            bg = rgb(self.fills[fi].get('bg'))
            if bg and self.fills[fi].get('bg', '').upper() not in ('FFFFFF',):
                out['fill'] = PatternFill(patternType='solid', fgColor=bg, bgColor=bg)
        # 数字格式
        if nf < len(self.nfs) and isinstance(self.nfs[nf], dict):
            fc = self.nfs[nf].get('fc')
            if fc:
                out['numfmt'] = fc
        # 对齐
        h = v = None
        wrap = False
        if ah < len(self.aligns) and isinstance(self.aligns[ah], dict):
            h = self.aligns[ah].get('h')
            wrap = bool(self.aligns[ah].get('w'))
        if av < len(self.aligns) and isinstance(self.aligns[av], dict):
            v = self.aligns[av].get('v')
            wrap = wrap or bool(self.aligns[av].get('w'))
        hmap = {'left': 'left', 'center': 'center', 'right': 'right', 'justify': 'justify'}
        vmap = {'top': 'top', 'middle': 'center', 'center': 'center', 'bottom': 'bottom'}
        h = hmap.get(h, h)
        v = vmap.get(v, v)
        if h or v or wrap:
            out['align'] = Alignment(horizontal=h, vertical=v, wrap_text=wrap or None)
        # 边框
        if b < len(self.borders) and isinstance(self.borders[b], dict):
            sides = self.borders[b].get('b') or []
            if len(sides) == 4 and any(sides):
                def side(c):
                    cc = rgb(c)
                    return Side(style='thin', color=cc) if cc else Side(style=None)
                out['border'] = Border(left=side(sides[0]), right=side(sides[1]),
                                       top=side(sides[2]), bottom=side(sides[3]))
        self._cache[si] = out
        return out


DATE_HINT = ('y', 'd')
HAS_TIME = ('h',)


def is_date_fmt(code):
    if not code:
        return False
    c = code.lower()
    if 'yy' in c and ('d' in c or 'm' in c):
        return True
    if 'd' in c and 'm' in c:
        return True
    return False


def is_datetime_fmt(code):
    if not code:
        return False
    c = code.lower()
    return is_date_fmt(code) and 'h' in c


EPOCH = datetime.datetime(1899, 12, 30)
MIN_SERIAL = 20000          # 1954-10-03 以下不当作日期（1、1.94 这类是小数）
MAX_SERIAL = 2958465


def serial_to_datetime(serial):
    """Excel 序列号 → 日期。只有像样的日期序列号才转换，避免把小数当日期。"""
    try:
        v = float(serial)
    except Exception:
        return None
    if MIN_SERIAL <= v <= MAX_SERIAL:
        if v < 60:
            return datetime.datetime(1899, 12, 31) + datetime.timedelta(days=v - 1)
        return EPOCH + datetime.timedelta(days=v)
    if 0 <= v < 1:                      # 纯时间（不足一天）
        # 保留毫秒/微秒精度：整秒四舍五入会丢掉小数位（如 0.40909 → 09:49:05，
        # 少 0.38 秒），回读比对时就会报"不一致"
        total = v * 86400.0
        secs = int(total)
        micro = int(round((total - secs) * 1e6))
        if micro >= 1000000:            # 进位保护
            secs += 1
            micro = 0
        return datetime.time(secs // 3600, (secs % 3600) // 60, secs % 60, micro)
    return None


# --------------------------------------------------------------------------
# 值
# --------------------------------------------------------------------------
def unwrap(v):
    """富文本 / 包装对象 → 纯值。"""
    if isinstance(v, dict):
        if v.get('t') == 'r' and isinstance(v.get('v'), list):
            return ''.join(str(p.get('tx', '')) for p in v['v'] if isinstance(p, dict))
        if 'v' in v:
            return unwrap(v['v'])
        if 'tx' in v:
            return str(v['tx'])
        return None
    return v


REF_RE = re.compile(r'(?<![A-Za-z0-9_$!])\$?([A-Z]{1,3})\$?([0-9]{1,7})(?![0-9(])')


def formula_ok(text, cellmap):
    """公式里引用的单元格是否都还在（有值或有样式）。cellmap 用 0 基坐标。"""
    for m in REF_RE.finditer(text):
        col = 0
        for ch in m.group(1):
            col = col * 26 + (ord(ch) - 64)
        row = int(m.group(2))
        if (row - 1, col - 1) not in cellmap:
            return False
    return True


DATE_HEADER_WORDS = ('时间', '日期', '年月日', '下单日', '登记日', '发货日', '到货日',
                     '开票日', '生日', '月份', '签收日', '付款日', '创建日', '更新日')


def date_columns(valmap, cellmap, sb, hdr_row):
    """判断哪些列是日期列。

    1) 单元格自带日期数字格式 → 该列按日期处理；
    2) 表头里有「时间/日期…」字样 → 该列按日期处理；
    3) 兜底：一列里有 >=3 个 20000~60000 的整数且占多数 → 当日期列。
    """
    cols = set()
    nums = {}
    for (ri, ci), v in valmap.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums.setdefault(ci, []).append(v)
    for (ri, ci), c in cellmap.items():
        if not isinstance(c, dict):
            continue
        xf = sb.xf(c.get('s'))
        if xf and xf.get('numfmt') and is_date_fmt(xf['numfmt']):
            cols.add(ci)
    if hdr_row is not None:
        for (ri, ci), v in valmap.items():
            if ri == hdr_row and isinstance(v, str):
                if any(w in v for w in DATE_HEADER_WORDS):
                    cols.add(ci)
    for ci, lst in nums.items():
        if ci in cols:
            continue
        if len(lst) < 3:
            continue
        inrange = [x for x in lst if 20000 <= x <= 60000]
        if len(inrange) < 3 or len(inrange) < 0.7 * len(lst):
            continue
        if max(inrange) - min(inrange) > 30000:
            continue
        cols.add(ci)
    return cols


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def sheet_title(index, shard_id, used):
    title = None
    if index:
        sheets = index.get('sheets') or {}
        info = sheets.get(shard_id)
        if isinstance(info, dict):
            title = info.get('title')
    if not title:
        title = shard_id
    title = re.sub(r'[\[\]:*?/\\]', '-', str(title)).strip()[:31] or 'Sheet'
    base = title
    i = 2
    while title in used:
        suf = '(%d)' % i
        title = base[:31 - len(suf)] + suf
        i += 1
    used.add(title)
    return title


def convert(src, dst, csv_dir=None, note_lines=None):
    data = open(src, 'rb').read()
    header, entries = parse_strict(data)

    index = None
    style_payload = None
    shards = {}
    for pos, X, name, payload in entries:
        if name.endswith('text/index'):
            if index is None:
                try:
                    index = DEC.raw_decode(payload.decode('utf-8', 'replace'))[0]
                except Exception:
                    index = None
        elif name.endswith('text/style'):
            if style_payload is None:
                style_payload = payload
        elif 'text/sh_' in name:
            sid = name.rsplit('/', 1)[-1]
            shards[sid] = payload
    if index is None:
        index = {}

    order = [s for s in (index.get('order') or []) if s in shards]
    for sid in sorted(shards, key=lambda s: (len(s), s)):
        if sid not in order:
            order.append(sid)
    deleted = set()
    for sid, info in (index.get('sheets') or {}).items():
        if isinstance(info, dict) and info.get('deleted'):
            deleted.add(sid)

    sb = StyleBook(style_payload)
    wb = Workbook()
    wb.remove(wb.active)
    try:
        wb.calculation.fullCalcOnLoad = True
    except Exception:
        pass

    report = {'src': src, 'dst': dst, 'sheets': [], 'missing_shards': [],
              'index_ok': bool(index), 'style_ok': bool(sb.styles)}

    used_names = set()
    for sid in order:
        if sid in deleted:
            continue
        obj, complete = parse_shard(shards[sid])
        title = sheet_title(index, sid, used_names)
        ws = wb.create_sheet(title=title)
        cells = obj.get('cells') or {}
        if not isinstance(cells, dict):
            cells = {}
        cellmap = {}
        for rk, row in cells.items():
            try:
                ri = int(rk)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            for ck, c in row.items():
                try:
                    ci = int(ck)
                except Exception:
                    continue
                cellmap[(ri, ci)] = c

        maxrow = -1
        maxcol = -1
        widths = {}
        has_val = {}
        valmap = {}
        for (ri, ci), c in sorted(cellmap.items()):
            val = unwrap(c.get('v')) if isinstance(c, dict) else None
            if val is not None and val != '':
                valmap[(ri, ci)] = val
                has_val[(ri, ci)] = True
            if ri > maxrow:
                maxrow = ri
            if ci > maxcol:
                maxcol = ci
            if isinstance(val, str):
                widths[ci] = max(widths.get(ci, 0), _display_len(val))

        hdr_row = detect_header(cellmap, has_val, obj)
        dcols = date_columns(valmap, cellmap, sb, hdr_row)

        for (ri, ci), c in sorted(cellmap.items()):
            xf = sb.xf(c.get('s')) if isinstance(c, dict) else None
            val = unwrap(c.get('v')) if isinstance(c, dict) else None
            if val is None and (not isinstance(c, dict) or c.get('v') is None):
                # 只有样式的空格子：只在有填充/边框时写出来（保持表格观感）
                if not xf or ('fill' not in xf and 'border' not in xf):
                    continue
            cell = ws.cell(row=ri + 1, column=ci + 1)
            nf = xf.get('numfmt') if xf else None
            want_fmt = None
            if isinstance(val, str) and val.startswith('='):
                if formula_ok(val, cellmap):
                    cell.value = val
                else:
                    cell.value = "'" + val
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                if isinstance(val, int) and abs(val) >= 10 ** 15:
                    # 16 位以上的整数（订单号之类）Excel 存不下，会丢精度 → 存成文本
                    cell.value = str(val)
                    want_fmt = '@'
                elif nf and is_date_fmt(nf):
                    dt = serial_to_datetime(val)
                    if dt is not None:
                        cell.value = dt
                        want_fmt = nf
                    else:
                        # 原格式是日期、但值根本不是日期序列号（多半是订单号）
                        cell.value = val
                        want_fmt = 'General'
                elif ci in dcols and MIN_SERIAL <= val <= MAX_SERIAL:
                    dt = serial_to_datetime(val)
                    if dt is not None:
                        cell.value = dt
                        want_fmt = 'yyyy-mm-dd hh:mm:ss' if is_datetime_fmt(nf) else 'yyyy-mm-dd'
                    else:
                        cell.value = val
                else:
                    cell.value = val
            elif val is None:
                cell.value = None
            else:
                cell.value = str(val)
            if xf:
                if 'font' in xf:
                    cell.font = xf['font']
                if 'fill' in xf:
                    cell.fill = xf['fill']
                if 'align' in xf:
                    cell.alignment = xf['align']
                if 'border' in xf:
                    cell.border = xf['border']
                if 'numfmt' in xf:
                    cell.number_format = xf['numfmt']
            if want_fmt:
                cell.number_format = want_fmt

        # 行高
        rowmeta = obj.get('rows') or {}
        if isinstance(rowmeta, dict):
            for rk, rd in rowmeta.items():
                try:
                    ri = int(rk)
                except Exception:
                    continue
                h = rd.get('h') if isinstance(rd, dict) else None
                if h:
                    try:
                        ws.row_dimensions[ri + 1].height = max(9.0, float(h) * 0.75)
                    except Exception:
                        pass

        # 列宽：先按容器里的 colSpans，再按内容自适应
        spans = obj.get('colSpans') or []
        spanned = set()
        if isinstance(spans, list):
            for sp in spans:
                if not isinstance(sp, dict):
                    continue
                st = sp.get('start')
                en = sp.get('end', st)
                w = sp.get('w')
                if st is None or not w:
                    continue
                try:
                    st = int(st)
                    en = int(en)
                    w = float(w)
                except Exception:
                    continue
                for ci in range(st, en + 1):
                    ws.column_dimensions[get_column_letter(ci + 1)].width = \
                        max(4.0, min(80.0, w / 7.0))
                    spanned.add(ci)
        for ci, w in widths.items():
            if ci in spanned:
                continue
            ws.column_dimensions[get_column_letter(ci + 1)].width = \
                max(6.0, min(36.0, w * 1.1 + 2))

        # 合并单元格（被覆盖的格子有值就不合并，免得丢数据）
        merges = obj.get('mergeCells') or []
        merged = 0
        skipped = 0
        if isinstance(merges, list):
            for mc in merges:
                if not (isinstance(mc, list) and len(mc) == 4):
                    continue
                r1, c1, r2, c2 = [int(x) for x in mc]
                if r2 < r1 or c2 < c1:
                    continue
                conflict = False
                for rr in range(r1, r2 + 1):
                    for cc in range(c1, c2 + 1):
                        if (rr, cc) == (r1, c1):
                            continue
                        if has_val.get((rr, cc)):
                            conflict = True
                            break
                    if conflict:
                        break
                if conflict:
                    skipped += 1
                    continue
                try:
                    ws.merge_cells(start_row=r1 + 1, start_column=c1 + 1,
                                   end_row=r2 + 1, end_column=c2 + 1)
                    merged += 1
                except Exception:
                    skipped += 1

        # 批注
        notes = obj.get('notes') or []
        if isinstance(notes, list):
            for nt in notes:
                if not isinstance(nt, dict):
                    continue
                try:
                    r = int(nt.get('row'))
                    c = int(nt.get('col'))
                except Exception:
                    continue
                try:
                    from openpyxl.comments import Comment
                    ws.cell(row=r + 1, column=c + 1).comment = \
                        Comment(str(nt.get('text', '')).rstrip('\n'), '恢复')
                except Exception:
                    pass

        # 表头识别 + 冻结 + 筛选 + 美化
        if hdr_row is not None:
            style_header(ws, hdr_row, maxcol, sb, cellmap)
            ws.freeze_panes = "A%d" % (hdr_row + 2)
            if maxrow >= 0 and maxcol >= 0 and maxrow - hdr_row < 200000:
                try:
                    ws.auto_filter.ref = "%s%d:%s%d" % (
                        get_column_letter(1), hdr_row + 1,
                        get_column_letter(maxcol + 1), maxrow + 1)
                except Exception:
                    pass

        report['sheets'].append({
            'id': sid, 'title': title, 'complete': complete,
            'rows': maxrow + 1, 'cols': maxcol + 1,
            'cells': len(cellmap), 'merged': merged, 'merge_skipped': skipped,
            'header_row': hdr_row,
        })

    idx_missing = []
    if index.get('order'):
        for sid in index['order']:
            if sid not in shards:
                idx_missing.append(sid)
    report['missing_shards'] = idx_missing

    if note_lines:
        ws = wb.create_sheet(title='恢复说明')
        for i, line in enumerate(note_lines, start=1):
            ws.cell(row=i, column=1, value=line)

    wb.save(dst)
    if csv_dir:
        write_csvs(src, dst, csv_dir, index, shards, used_names)
    return report


def _display_len(text):
    n = 0
    for ch in text:
        n += 2 if ord(ch) > 0x2000 else 1
    return n


def detect_header(cellmap, has_val, obj):
    """找表头行：第一行里有 >=2 个文字、且下一行也有内容的行。"""
    rows = sorted(set(r for (r, c) in cellmap))
    if not rows:
        return None
    cand = rows[:4]
    for r in cand:
        texts = 0
        for (rr, cc), c in cellmap.items():
            if rr != r:
                continue
            v = unwrap(c.get('v')) if isinstance(c, dict) else None
            if isinstance(v, str) and v.strip() and not v.startswith('='):
                texts += 1
        if texts < 2:
            continue
        below = 0
        for (rr, cc) in has_val:
            if rr == r + 1:
                below += 1
        if below >= 1 or r == rows[0]:
            return r
    return None


def style_header(ws, hdr_row, maxcol, sb, cellmap):
    """表头行没有底色时，补一个淡底色 + 加粗 + 居中。"""
    from openpyxl.styles import Font as _Font
    for ci in range(maxcol + 1):
        cell = ws.cell(row=hdr_row + 1, column=ci + 1)
        f = cell.font
        if not f or not f.bold:
            kw = {}
            if f is not None:
                if f.name:
                    kw['name'] = f.name
                if f.size:
                    kw['size'] = f.size
                if f.italic:
                    kw['italic'] = True
                if f.underline:
                    kw['underline'] = f.underline
                try:
                    c = f.color
                    if c is not None and c.type == 'rgb' and c.rgb is not None:
                        kw['color'] = str(c.rgb)
                except Exception:
                    pass
            cell.font = _Font(bold=True, **kw)
        if not (cell.fill and cell.fill.patternType):
            cell.fill = PatternFill(patternType='solid', fgColor='FFDCE6F1',
                                    bgColor='FFDCE6F1')
        al = cell.alignment
        cell.alignment = Alignment(horizontal=al.horizontal if al else 'center',
                                   vertical='center',
                                   wrap_text=True)


def write_csvs(src, dst, csv_dir, index, shards, used_names):
    """每张工作表导一份带表头行的 CSV（UTF-8 BOM，Excel 直接能开）。"""
    import csv
    if not os.path.isdir(csv_dir):
        os.makedirs(csv_dir)
    base = os.path.splitext(os.path.basename(dst))[0]
    order = [s for s in (index.get('order') or []) if s in shards]
    for sid in sorted(shards, key=lambda s: (len(s), s)):
        if sid not in order:
            order.append(sid)
    used = set()
    for sid in order:
        obj, _c = parse_shard(shards[sid])
        title = sheet_title(index, sid, used)
        cells = obj.get('cells') or {}
        if not isinstance(cells, dict) or not cells:
            continue
        rows = {}
        maxcol = -1
        for rk, row in cells.items():
            try:
                ri = int(rk)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            for ck, c in row.items():
                try:
                    ci = int(ck)
                except Exception:
                    continue
                v = unwrap(c.get('v')) if isinstance(c, dict) else None
                if v is None:
                    continue
                rows.setdefault(ri, {})[ci] = v
                maxcol = max(maxcol, ci)
        if not rows:
            continue
        path = os.path.join(csv_dir, '%s__%s.csv' % (base, title))
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            for ri in sorted(rows):
                r = rows[ri]
                w.writerow([r.get(ci, '') for ci in range(maxcol + 1)])


def main():
    args = sys.argv[1:]
    csv_dir = None
    if '--csv' in args:
        i = args.index('--csv')
        csv_dir = args[i + 1]
        del args[i:i + 2]
    if len(args) < 2:
        print(__doc__)
        return 1
    src, dst = args[0], args[1]
    rep = convert(src, dst, csv_dir)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
