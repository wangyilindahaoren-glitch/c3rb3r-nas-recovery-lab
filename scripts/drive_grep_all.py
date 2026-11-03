# -*- coding: utf-8 -*-
"""_grep_all.py —— 在全部 Drive 仓库的 git 对象里搜关键字（NAS, root）。
用法: python3 _grep_all.py <关键字1> [关键字2 ...]
"""
import os, sys, zlib

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
OFFICE = '/volume1/@SynoDrive/office'


def iter_objects(root):
    objdir = os.path.join(root, "objects")
    for d1 in sorted(os.listdir(objdir)):
        p1 = os.path.join(objdir, d1)
        if not os.path.isdir(p1) or len(d1) != 2:
            continue
        for fn in sorted(os.listdir(p1)):
            if len(fn) != 38:
                continue
            try:
                with open(os.path.join(p1, fn), 'rb') as f:
                    raw = f.read()
                d = zlib.decompress(raw)
            except Exception:
                continue
            i = d.find(b"\0")
            if i < 0:
                continue
            hdr = d[:i].decode('utf-8', 'replace').split()
            if not hdr:
                continue
            yield d1 + fn, hdr[0], d[i + 1:]


def main():
    kws = [k.encode('utf-8') for k in sys.argv[1:]]
    for name in sorted(os.listdir(OFFICE)):
        if not name.endswith('.sh'):
            continue
        git = os.path.join(OFFICE, name, 'version', '.git')
        if not os.path.isdir(os.path.join(git, 'objects')):
            continue
        for sha, typ, data in iter_objects(git):
            for kw in kws:
                if kw in data:
                    i = data.find(kw)
                    lo = max(0, i - 60)
                    print('%s\t%s\t%s\t%s' % (
                        name, sha[:12], typ, data[lo:i + 120].decode('utf-8', 'replace')))
                    break


if __name__ == '__main__':
    main()
