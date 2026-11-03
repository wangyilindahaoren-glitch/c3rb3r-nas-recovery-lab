#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ole2_install.py —— 把**按正文确认过**的 OLE2(.xls/.doc) 原件装回原目录

判据（三道闸，缺一不可）：
  1. 雕出的长度 == 勒索标记里的 orig_len（精确相等）；
  2. 正文里含目标关键词（标题/品牌/公司名）；
  3. 写回后 SHA-256 与雕出件一致。

⚠️ 只新增、不覆盖、不动 `.L0CK3D`。属主/ACL 用**同目录的密文**当模板，
   再补一条 administrators 条目（群晖上 root 建的文件 admin 在 File Station 里读不到）。

用法
----
  # 候选表 cand.tsv：<设备偏移>\t<目标相对路径>\t<必须出现的关键词>
  python3 ole2_install.py <cand.tsv> --home /volume1/homes/<用户> \
          --targets <targets.tsv> --outdir /tmp/c3r_ole_rec --log /tmp/ole_install.log
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ole2_lib import Ole, rd, doc_cjk

DEV = os.environ.get('C3R_DEV', '/dev/mapper/cachedev_0')
ACL = '/usr/syno/bin/synoacltool'


def load_targets(path):
    out = {}
    for ln in open(path, encoding='utf-8'):
        ln = ln.rstrip('\n')
        if not ln:
            continue
        p = ln.split('\t')
        if len(p) < 3:
            continue
        out[p[2]] = int(p[0])
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cand')
    ap.add_argument('--home', required=True)
    ap.add_argument('--targets', required=True)
    ap.add_argument('--outdir', default='/tmp/c3r_ole_rec')
    ap.add_argument('--log', default='/tmp/ole_install.log')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()

    targets = load_targets(a.targets)
    os.makedirs(a.outdir, exist_ok=True)
    log = open(a.log, 'w', encoding='utf-8')
    add = skip = fail = 0

    for ln in open(a.cand, encoding='utf-8'):
        ln = ln.rstrip('\n')
        if not ln:
            continue
        parts = ln.split('\t')
        if len(parts) < 3:
            continue
        off, rel, kw = int(parts[0]), parts[1], parts[2]
        dst = os.path.join(a.home, rel)
        want = targets.get(rel)
        try:
            o = Ole(off, DEV)
            alloc = o.alloc_len()
            wd = o.stream('WordDocument')
            blob = ' '.join(doc_cjk(wd))
            if not blob:                       # .xls：取 Workbook 流的原始文本
                wb = o.stream('Workbook') or o.stream('Book')
                blob = wb.decode('utf-16-le', 'ignore') + wb.decode('gbk', 'ignore')
        except Exception as e:
            log.write('失败(解析) %s %s\n' % (rel, e))
            fail += 1
            continue
        if want is None:
            log.write('跳过(不在目标清单) %s\n' % rel)
            skip += 1
            continue
        if alloc != want:
            log.write('失败(长度 %d != orig_len %d) %s\n' % (alloc, want, rel))
            fail += 1
            continue
        if kw and kw not in blob:
            log.write('失败(正文无 "%s") %s\n' % (kw, rel))
            fail += 1
            continue
        src = os.path.join(a.outdir, os.path.basename(rel))
        if not a.dry:
            with open(src, 'wb') as f:
                f.write(rd(off, alloc, DEV))
        if os.path.exists(dst):
            log.write('跳过(已存在) %s\n' % rel)
            skip += 1
            continue
        d = os.path.dirname(dst)
        if not os.path.isdir(d):
            log.write('跳过(目录不存在) %s\n' % rel)
            skip += 1
            continue
        if a.dry:
            log.write('预演(可装) %s  %d 字节  关键词=%s\n' % (rel, alloc, kw))
            add += 1
            continue
        tpl = ''
        for fn in sorted(os.listdir(d)):
            if fn.endswith('.L0CK3D'):
                tpl = os.path.join(d, fn)
                break
        shutil.copyfile(src, dst)
        if tpl:
            st = os.stat(tpl)
            os.chown(dst, st.st_uid, st.st_gid)
        os.chmod(dst, 0o666)
        if tpl and os.path.exists(ACL):
            subprocess.call([ACL, '-copy', tpl, dst],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.call([ACL, '-add', dst,
                             'group:administrators:allow:rwxpdDaARWc--:----'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if sha256_file(src) == sha256_file(dst):
            log.write('OK\t%s\t%d\t%s\n' % (rel, alloc, kw))
            add += 1
        else:
            log.write('失败(校验) %s\n' % rel)
            os.remove(dst)
            fail += 1
    log.write('新增 %d，跳过 %d，失败 %d\n' % (add, skip, fail))
    log.close()
    print('新增 %d，跳过 %d，失败 %d（日志 %s）' % (add, skip, fail, a.log))


if __name__ == '__main__':
    main()
