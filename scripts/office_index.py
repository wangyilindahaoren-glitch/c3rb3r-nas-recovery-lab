#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""office_index.py —— 汇总 Synology Drive office 目录下每个 WPS 文档的可恢复信息。

对 /volume1/@SynoDrive/office/<uid>_<docid>.sh/ :
  * 目录名即 osheet 容器头部的 id 字段
  * version/.git 的 HEAD commit 即 osheet 头部的 ver 字段
  * version/text/<base64条目名> 是该文档的条目内容（勒索程序没加密）
  * 工作区 basic.json / metatext.json 已被勒索加密，其中的 title 即文件名

输出 TSV：id  ver  工作表名  条目总字节  sh_N:大小列表
"""
import base64
import json
import os
import sys

OFFICE = "/volume1/@SynoDrive/office"


def b64name(fn):
    """把 'ZGV{109}{97}W5{108}' 这种文件名还原成条目名。"""
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


def main():
    rows = []
    for name in sorted(os.listdir(OFFICE)):
        d = os.path.join(OFFICE, name)
        if not os.path.isdir(d):
            continue
        docid = name.split(".")[0]
        uid = docid.split("_")[0]
        if not uid.isdigit():
            continue
        ver = ""
        ref = os.path.join(d, "version", ".git", "refs", "heads", "master")
        if os.path.exists(ref):
            ver = open(ref).read().strip()
        textdir = os.path.join(d, "version", "text")
        entries = {}
        if os.path.isdir(textdir):
            for fn in os.listdir(textdir):
                p = os.path.join(textdir, fn)
                if not os.path.isfile(p):
                    continue
                entries[b64name(fn)] = os.path.getsize(p)
        total = sum(entries.values())
        sheets = ""
        if "index" in entries:
            try:
                data = open(os.path.join(textdir, _raw("index")), "rb").read()
                idx, _ = json.JSONDecoder().raw_decode(data.decode("utf-8", "replace"))
                sheets = "|".join("%s:%s" % (k, v.get("title"))
                                  for k, v in sorted(idx.get("sheets", {}).items()))
            except Exception:
                pass
        shs = "|".join("%s:%d" % (k, v) for k, v in sorted(entries.items())
                       if k.startswith("sh_"))
        rows.append((docid, uid, ver, total, sheets, shs))
    print("id\tuid\tver\t总字节\t工作表\tsh条目")
    for r in rows:
        print("\t".join(str(x) for x in r))


def _raw(name):
    """反向：条目名 -> version/text 下的实际文件名。"""
    enc = base64.b64encode(name.encode()).decode()
    out = []
    for ch in enc:
        if ch == "=":
            out.append("{61}")
        else:
            out.append(ch)
    return "".join(out)


if __name__ == "__main__":
    sys.exit(main())
