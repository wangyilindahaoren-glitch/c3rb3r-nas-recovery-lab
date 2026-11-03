#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群晖收尾：把恢复出来的文件/目录改成用户属主，并补上 Synology ACL。

为什么必须有这一步：
  root 写出来的文件在 DSM 上是 "It's Linux mode"（`synoacltool -get` 可见），
  没有 ACL → administrators 组读不到 → 以 admin 身份在 File Station 里
  看不到、下载不了，用户会以为文件坏了。原始文件（如备份 zip）是有 ACL 的。

做法：
  - 属主：改成该用户 home 的 uid/gid；
  - ACL：文件用同一个人 home 下的**原始文件**当模板，目录用**目录**当模板
    （文件模板复制到目录上不生效）；
  - **顺序**：先 chown/chmod，最后复制 ACL —— chmod 会把 ACL 打回 Linux mode。

用法：python3 fix_nas_acl.py <用户目录> [<用户目录> ...]
      例：python3 fix_nas_acl.py /volume1/homes/<用户A> /volume1/homes/<用户B>
"""
import os
import subprocess
import sys
import time

TOOL = '/usr/syno/bin/synoacltool'
STAMP = '勒索恢复_20261030'
HOURS = 24          # 只处理最近这些小时内新建/改动的文件


def acl_state(path):
    r = subprocess.run([TOOL, '-get', path], capture_output=True)
    out = (r.stdout + r.stderr).decode('utf-8', 'replace')
    if 'ACL version' in out and "It's Linux mode" not in out:
        return 'acl'
    return 'linux'


def copy_acl(src, dst):
    r = subprocess.run([TOOL, '-copy', src, dst], capture_output=True)
    return r.returncode


def template_file(home):
    """带 ACL 的文件模板：优先 home 根目录下的原始密文"""
    for fn in sorted(os.listdir(home)):
        if fn.endswith('.L0CK3D'):
            p = os.path.join(home, fn)
            if acl_state(p) == 'acl':
                return p
    for fn in sorted(os.listdir(home)):
        p = os.path.join(home, fn)
        if os.path.isfile(p) and acl_state(p) == 'acl':
            return p
    for dp, dns, fns in os.walk(home):
        if '@eaDir' in dp or '#recycle' in dp:
            dns[:] = []
            continue
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            if os.path.isfile(p) and acl_state(p) == 'acl':
                return p
    return None


def main():
    for home in sys.argv[1:]:
        if not os.path.isdir(home):
            print('不存在:', home)
            continue
        user = os.path.basename(home)
        tpl_file = template_file(home)
        rec = os.path.join(home, STAMP)
        uid = os.stat(home).st_uid
        gid = os.stat(home).st_gid
        nd = nf = 0
        # 1) 目录（含恢复目录树）
        dirs = []
        if os.path.isdir(rec):
            for dp, dns, fns in os.walk(rec):
                dirs.append(dp)
        dirs.append(home)
        for dp in dirs:
            if acl_state(dp) == 'acl':
                continue          # 已有 ACL 就别动：chown/chmod 会破坏它
            try:
                os.chown(dp, uid, gid)
                os.chmod(dp, 0o711)
            except OSError:
                pass
            copy_acl(home, dp)        # 目录用目录当模板
            nd += 1
        # 2) 文件
        files = []
        if os.path.isdir(rec):
            for dp, dns, fns in os.walk(rec):
                for fn in fns:
                    files.append(os.path.join(dp, fn))
        for dp, dns, fns in os.walk(home):
            if '@eaDir' in dp or '#recycle' in dp or STAMP in dp:
                dns[:] = []
                continue
            for fn in fns:
                p = os.path.join(dp, fn)
                try:
                    if time.time() - os.stat(p).st_mtime <= HOURS * 3600:
                        files.append(p)
                except OSError:
                    pass
        for p in files:
            if acl_state(p) == 'acl':
                continue          # 已有 ACL 就别动
            try:
                os.chown(p, uid, gid)
                os.chmod(p, 0o711)
            except OSError:
                pass
            if tpl_file:
                copy_acl(tpl_file, p)
            nf += 1
        print('%s: 补 ACL 目录 %d 个、文件 %d 个（模板 %s）' % (
            user, nd, nf, os.path.basename(tpl_file) if tpl_file else '无'))


if __name__ == '__main__':
    main()
