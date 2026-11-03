#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回读校验：把 `osheet_to_xlsx.py` 生成的 xlsx 与源 .osheet 逐格比对。

用法：
    python3 verify_osheet_xlsx.py <源.osheet> <生成的.xlsx> [--sample 400]

判据：
  * 工作表数量、名字、顺序与容器 text/index 一致；
  * 随机抽样若干格：文字/公式/数字逐格相等；日期按序列号比；
    超长整数（16 位以上）按文本比；原表里就是日期/时间格式的格，
    按 Excel 的口径把读出来的日期换算回序列号再比。

退出码 0 = 全部一致，1 = 有不一致（会把不一致的格子打出来）。
"""
import datetime
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import osheet_to_xlsx as M          # noqa: E402
from openpyxl import load_workbook   # noqa: E402


def to_serial(dt):
    """把 Excel 读出来的日期/时间还原成序列号。"""
    if isinstance(dt, datetime.time):
        return (dt.hour * 3600 + dt.minute * 60 + dt.second +
                dt.microsecond / 1e6) / 86400.0
    if isinstance(dt, datetime.datetime):
        base = datetime.datetime(1899, 12, 30)
        if dt < datetime.datetime(1900, 3, 1):
            base = datetime.datetime(1899, 12, 31)
        return (dt - base).total_seconds() / 86400.0
    return None


def main():
    args = sys.argv[1:]
    sample = 400
    if '--sample' in args:
        i = args.index('--sample')
        sample = int(args[i + 1])
        del args[i:i + 2]
    if len(args) < 2:
        print(__doc__)
        return 1
    src, dst = args[0], args[1]
    random.seed(7)

    data = open(src, 'rb').read()
    h, entries = M.parse_strict(data)
    index = None
    shards = {}
    for pos, X, name, payload in entries:
        if name.endswith('text/index') and index is None:
            try:
                index = M.DEC.raw_decode(payload.decode('utf-8', 'replace'))[0]
            except Exception:
                index = None
        elif 'text/sh_' in name:
            shards[name.rsplit('/', 1)[-1]] = payload
    if index is None:
        index = {}
    ids = [s for s in (index.get('order') or []) if s in shards]
    for sid in sorted(shards, key=lambda s: (len(s), s)):
        if sid not in ids:
            ids.append(sid)
    used = set()
    exp_names = [M.sheet_title(index, sid, used) for sid in ids]

    wb = load_workbook(dst)
    got_names = [s for s in wb.sheetnames if s != '恢复说明']
    if exp_names != got_names:
        print('!! 工作表不一致')
        print('   源  :', exp_names)
        print('   xlsx:', got_names)
    ok = bad = 0
    for sid, name in zip(ids, got_names):
        obj, _complete = M.parse_shard(shards[sid])
        cells = obj.get('cells') or {}
        ws = wb[name]
        keys = [(int(r), int(c)) for r, row in cells.items()
                if isinstance(row, dict) for c in row.keys()]
        pick = random.sample(keys, sample) if len(keys) > sample else keys
        for (ri, ci) in pick:
            c = cells.get(str(ri), {}).get(str(ci))
            if not isinstance(c, dict):
                continue
            v = M.unwrap(c.get('v'))
            if v is None:
                continue
            got = ws.cell(row=ri + 1, column=ci + 1).value
            if isinstance(v, str) and v.startswith('='):
                good = (got == v) or (isinstance(got, str) and got.lstrip("'") == v)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                if isinstance(got, (datetime.datetime, datetime.date, datetime.time)):
                    ser = to_serial(got)
                    good = ser is not None and abs(ser - v) < 1e-6
                elif isinstance(got, str) and isinstance(v, int) and abs(v) >= 10 ** 15:
                    good = got == str(v)          # 超长整数按文本存
                else:
                    good = (got == v)
            else:
                good = (got == v)
            if good:
                ok += 1
            else:
                bad += 1
                print('   不符 [%s] r%d c%d 源=%r xlsx=%r' % (name, ri, ci, v, got))
    print('抽检 %d 格：一致 %d，不一致 %d' % (ok + bad, ok, bad))
    return 0 if bad == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
