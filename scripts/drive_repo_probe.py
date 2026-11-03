# -*- coding: utf-8 -*-
"""probe_drive.py —— 分析 .osheet 容器 与 Synology Drive Git 仓库 的对应关系。
在 NAS 上以 root 运行（纯标准库）。
"""
import os, sys, zlib, base64, json, hashlib

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OFFICE = '/volume1/@SynoDrive/office'
MAGIC = b'\x78\x00\x00\x00'


# ---------- 容器解析（与 osheet_strict.py 同文法） ----------
def parse_header(data):
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
            pos += 1
            continue
        e = data.find(b'\x00', pos + 1)
        key = data[pos + 1:e].decode('latin1')
        p = e + 1
        if tag == 0x08:
            ln = data[p]; p += 1
        else:
            ln = int.from_bytes(data[p:p + 4], 'little'); p += 4
        header.append((tag, key, data[p:p + ln]))
        p += ln
        if p < len(data) and data[p] == 0:
            p += 1
        pos = p
    return header, pos


def parse_entries(data, start):
    pos = start
    entries = []
    while pos < len(data):
        if pos + 9 > len(data):
            break
        X = int.from_bytes(data[pos:pos + 4], 'little')
        tag = data[pos + 4]
        if tag not in (0x05, 0x02):
            break
        e = data.find(b'\x00', pos + 5)
        name = data[pos + 5:e].decode('latin1')
        p = e + 1
        ln = int.from_bytes(data[p:p + 4], 'little'); p += 4
        if tag == 0x05:
            raw = data[p + 1:p + 1 + ln]
            d = zlib.decompressobj()
            payload = d.decompress(raw) + d.flush()
        else:
            payload = data[p:p + ln]
        entries.append((pos, X, name, tag, payload))
        pos += X
        if pos < len(data) and data[pos + 4:pos + 5] == b'\x08':
            break
    return entries, pos


# ---------- Drive Git ----------
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
        return base64.b64decode(s).decode('utf-8', 'replace')
    except Exception:
        return s


def read_obj(root, sha):
    p = os.path.join(root, "objects", sha[:2], sha[2:])
    raw = open(p, 'rb').read()
    d = zlib.decompress(raw)
    i = d.index(b"\0")
    return d[:i].decode('utf-8', 'replace').split()[0], d[i + 1:]


def ls_tree(root, sha, prefix=""):
    typ, data = read_obj(root, sha)
    if typ != "tree":
        return []
    out = []
    i = 0
    while i < len(data):
        j = data.index(b" ", i)
        mode = data[i:j].decode()
        k = data.index(b"\0", j)
        name = data[j + 1:k].decode('utf-8', 'replace')
        sha2 = data[k + 1:k + 21].hex()
        i = k + 21
        if mode.startswith("40"):
            out.extend(ls_tree(root, sha2, prefix + name + "/"))
        else:
            out.append((prefix + name, sha2, mode))
    return out


def commits_of(root):
    try:
        c = open(os.path.join(root, "refs", "heads", "master")).read().strip()
    except Exception:
        return []
    out, seen = [], set()
    while c and c not in seen:
        seen.add(c)
        try:
            typ, data = read_obj(root, c)
        except Exception:
            break
        tree = parent = None
        for line in data.split(b"\n"):
            if line.startswith(b"tree "):
                tree = line.split()[1].decode()
            elif line.startswith(b"parent "):
                parent = line.split()[1].decode()
            elif not line.strip():
                break
        if not tree:
            break
        out.append((c, tree))
        c = parent
    return out


def tree_entries(git, tree):
    """-> {条目名: bytes}，name 与容器内一致（text/xxx、metatext）"""
    out = {}
    for fn, sha, mode in ls_tree(git, tree):
        base = fn.rsplit('/', 1)[-1]
        if base == 'read-me3.txt':
            continue
        nm = b64name(base)
        full = nm if nm in ('metatext', 'metabinairy_files') else ('text/' + nm)
        try:
            typ, data = read_obj(git, sha)
        except Exception:
            continue
        if typ != 'blob':
            continue
        out[full] = data
    return out


def sha(b):
    return hashlib.sha256(b).hexdigest()


def analyze(path):
    print('=' * 78)
    print('文件:', path)
    data = open(path, 'rb').read()
    print('大小: %d 字节   sha256: %s' % (len(data), sha(data)))
    if data[:4] != MAGIC:
        print('  !! magic 不对')
        return
    header, estart = parse_header(data)
    entries, epos = parse_entries(data, estart)
    hid = hver = None
    for tag, key, val in header:
        print('  头字段: tag=0x%02x key=%-8s len=%d val=%r' % (tag, key, len(val), val[:60]))
        if key == 'id':
            hid = val.rstrip(b'\x00').decode('utf-8', 'replace')
        if key == 'ver':
            hver = val.rstrip(b'\x00').decode('utf-8', 'replace')
    print('  id = %s' % hid)
    print('  ver= %s' % hver)
    print('  条目数: %d   条目链结束 @%d / 文件长 %d  (差 %d)'
          % (len(entries), epos, len(data), len(data) - epos))
    total = 0
    for pos, X, name, tag, payload in entries:
        total += len(payload)
        print('    %-28s tag=0x%02X  X=%-8d 载荷=%-8d' % (name, tag, X, len(payload)))
    print('  载荷合计 %d' % total)
    for pos, X, name, tag, payload in entries:
        if name in ('@C_metatext', '@C_text/index', '@C_text/file', '@C_text/locale'):
            print('    >>> %s 载荷原文: %r' % (name, payload[:600]))

    if not hid:
        return
    repo = os.path.join(OFFICE, hid if hid.endswith('.sh') else hid + '.sh')
    print('  Drive 仓库:', repo, '存在' if os.path.isdir(repo) else '**不存在**')
    if not os.path.isdir(repo):
        return
    git = os.path.join(repo, 'version', '.git')
    cms = commits_of(git)
    print('  Drive commit 数: %d' % len(cms))
    hdr = open(os.path.join(git, 'refs', 'heads', 'master')).read().strip()
    print('  HEAD = %s   (容器 ver %s %s)'
          % (hdr, hver, '一致 ✓' if hver == hdr else '不同'))
    # 找与容器内容匹配的 commit
    cont = {}
    for pos, X, name, tag, payload in entries:
        cont[name] = payload
    for cid, tree in cms:
        ents = tree_entries(git, tree)
        same = 0
        diff = []
        for name, payload in cont.items():
            if name not in ents:
                diff.append(name + ':Drive无')
                continue
            dv = ents[name]
            if dv == payload or dv + b'\x00' == payload:
                same += 1
            else:
                diff.append('%s:Drive %dB vs 容器 %dB' % (name, len(dv), len(payload)))
        extra = [n for n in ents if n not in cont]
        mark = '★ 全同' if (same == len(cont) and not extra) else ''
        print('    commit %s  条目 %d  同内容 %d/%d  额外 %d %s'
              % (cid[:12], len(ents), same, len(cont), len(extra), mark))
        for d in diff[:6]:
            print('        - ' + d)
        for x in extra[:6]:
            print('        + Drive多出 %s (%dB)' % (x, len(ents[x])))
        # 重建（不压缩形态）长度
        hdrl = 4 + 16 + 7 + (1 + 3 + 4 + len(hid) + 1 + 1) + (1 + 4 + 4 + 41 + 1)
        for meta_len in (13, 110):
            tot = hdrl
            for name, data2 in sorted(ents.items(), key=lambda e: (not e[0].startswith('text/'), e[0])):
                if name == 'metatext':
                    continue
                tot += 4 + 1 + len(name) + 1 + 4 + len(data2) + 1 + 1
            tot += 4 + 1 + len('metatext') + 1 + 4 + meta_len + 1
            tot += 11
            print('        不压缩重建长度(meta=%d) = %d' % (meta_len, tot))


for p in sys.argv[1:]:
    analyze(p)
