#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验 Drive 明文分片 与 雕刻出的精确原件 分片 是否一致（逐格）。

目的：在用 Drive 重建那些"雕刻没捞到"的文件之前，先用已知的 7 个原件
证明 Drive 侧的数据到底全不全 —— 不然重建出来的可能又是个缺表缺格的残件。

用法:
    python3 compare_drive.py <雕刻原件目录> <Drive解压目录> <映射文件.json>
映射文件: {"原件文件名": "drive目录前缀"}
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import osheet_to_xlsx as M   # noqa: E402

sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def b64name(fn):
    out = []
    i = 0
    while i < len(fn):
        if fn[i] == "{":
            j = fn.index("}", i)
            out.append(chr(int(fn[i + 1:j])))
            i = j + 1
        else:
            out.append(fn[i])
            i += 1
    s = "".join(out)
    try:
        return base64.b64decode(s).decode("utf-8", "replace")
    except Exception:
        return s


def load_drive(textdir):
    """返回 {条目名: 载荷bytes}，与容器里的条目名一致（text/sh_1 ...）。"""
    ents = {}
    for fn in sorted(os.listdir(textdir)):
        p = os.path.join(textdir, fn)
        if not os.path.isfile(p):
            continue
        name = b64name(fn)
        if name == 'read-me3.txt':
            continue
        full = name if name in ('metatext', 'metabinairy_files') else ('text/' + name)
        ents[full] = open(p, 'rb').read()
    return ents


def cellmap(payload):
    """分片 JSON -> {(row,col): 值}"""
    obj, _ = M.parse_shard(payload)
    cells = obj.get('cells') or {}
    out = {}
    for r, row in cells.items():
        if not isinstance(row, dict):
            continue
        for c, cv in row.items():
            v = M.unwrap(cv.get('v')) if isinstance(cv, dict) else cv
            if v is None or v == '':
                continue
            out[(int(r), int(c))] = v
    return out


def main():
    carved_dir, drive_root, mapfile = sys.argv[1], sys.argv[2], sys.argv[3]
    mapping = json.load(open(mapfile, encoding='utf-8'))

    for orig_name, drive_prefix in mapping.items():
        op = os.path.join(carved_dir, orig_name)
        dp = os.path.join(drive_root, drive_prefix + '.text')
        if not os.path.exists(op) or not os.path.isdir(dp):
            print('跳过 %s（文件缺失）' % orig_name)
            continue
        h, entries = M.parse_strict(open(op, 'rb').read())
        orig_shards = {}
        for _p, _X, name, payload in entries:
            if 'text/sh_' in name:
                orig_shards[name.rsplit('/', 1)[-1]] = payload
        drive_ents = load_drive(dp)
        drive_shards = {k.rsplit('/', 1)[-1]: v for k, v in drive_ents.items()
                        if 'text/sh_' in k}

        only_o = sorted(set(orig_shards) - set(drive_shards))
        only_d = sorted(set(drive_shards) - set(orig_shards))
        print('\n=== %s  (drive: %s) ===' % (orig_name, drive_prefix))
        print('  原件分片 %d 个, Drive 分片 %d 个 | 仅原件有: %s | 仅Drive有: %s'
              % (len(orig_shards), len(drive_shards), only_o or '无', only_d or '无'))
        for sid in sorted(set(orig_shards) & set(drive_shards)):
            try:
                oc = cellmap(orig_shards[sid])
            except Exception as e:
                print('  %s: 原件分片解析失败 %s' % (sid, e))
                continue
            try:
                dc = cellmap(drive_shards[sid])
            except Exception as e:
                print('  %s: Drive 分片解析失败 %s' % (sid, e))
                continue
            miss = set(oc) - set(dc)
            extra = set(dc) - set(oc)
            diff = [k for k in set(oc) & set(dc) if oc[k] != dc[k]]
            tag = '一致' if not (miss or extra or diff) else '有差异'
            print('  %-8s 原件 %5d 格 / Drive %5d 格  缺 %d / 多 %d / 值不同 %d  -> %s'
                  % (sid, len(oc), len(dc), len(miss), len(extra), len(diff), tag))
            if diff[:3]:
                for k in diff[:3]:
                    print('       差异 @%s 原件=%r Drive=%r' % (k, oc[k], dc[k]))
            if miss:
                print('       缺失样例: %s' % (sorted(miss)[:5],))
        sys.stdout.flush()


if __name__ == '__main__':
    main()
