#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jrn_match.py —— 在 ext4 的 jbd2 日志镜像里找回被覆盖掉的旧文件内容。

为什么 ext4 上值得专门做这件事
------------------------------
ext4 没有写时复制，勒索程序原地改写文件时，旧内容会被**物理覆盖**。
但 ext4 默认开日志（has_journal），挂载选项默认 `data=ordered`：
**文件的数据块在元数据事务提交之前会先写进日志，日志里保存的是这些块的旧内容。**
勒索程序改写文件必然伴随一次元数据事务（改大小、改 mtime），
于是"旧的文件内容"很可能就躺在 jbd2 日志里 —— 这是 ext4 独有的宝藏。

怎么用日志找回旧文件
--------------------
密文文件里有两段内容：
  - **明文区**：勒索程序没加密的那部分字节，**原样保留**，是原件的真内容；
  - **加密区**：被 ChaCha8 加密的部分，当前是密文。

用明文区当**指纹**在日志镜像里搜索：
  1. 在明文区里取几个 96 字节的探针；
  2. 在日志里找到它们，算出「文件偏移 → 日志偏移」的映射 delta
     （多个探针的 delta 一致才算可靠）；
  3. 加密区在日志里对应的位置，放的就是**旧的原始字节**；
  4. 加密区取日志、明文区取密文，拼起来 = 候选原件。

**诚实说明**：日志里存的是整个文件系统的块，混着别的文件的内容，
所以 delta 只在某些连续区间成立。脚本会如实报告"日志能覆盖多少字节"，
并做魔数与结构校验；**校验不过的只是候选，不能算恢复成功**。

用法：
  python3 jrn_match.py <日志镜像> --cipher <密文文件> [--out 目录]
  python3 jrn_match.py <日志镜像> --cipher-dir <密文目录> [--out 目录] [--max-files 200]

日志镜像怎么来（ext4_probe.py 一条命令）：
  python3 ext4_probe.py --dev /dev/sda1 --dump-journal /tmp/jrn.bin
  # 或直接用 debugfs（e2fsprogs 较新时支持）：
  debugfs -R "dump_journal /tmp/jrn.bin" /dev/sda1

只读：不写日志镜像、不写密文；候选件写到 --out 指定的目录。
"""
import os
import struct
import sys
import zipfile

TAIL = 588
PROBE_LEN = 96

MAGICS = [
    (b'%PDF-', 'pdf'),
    (b'PK\x03\x04', 'zip'),
    (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'\xff\xd8\xff', 'jpeg'),
    (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', 'ole2'),
    (b'\x1f\x8b\x08', 'gz'),
    (b'SQLite format 3\x00', 'sqlite'),
    (b'Rar!\x1a\x07\x00', 'rar'),
    (b'7z\xbc\xaf\x27\x1c', '7z'),
]


# ---------- 勒索标记与加密区间（与 plain_extract.py 同一套语义） ----------

def footer_meta(path):
    """返回 (mode, pct, orig_len, file_size)"""
    sz = os.path.getsize(path)
    if sz < TAIL:
        return None
    with open(path, 'rb') as f:
        f.seek(sz - TAIL)
        t = f.read(TAIL)
    if len(t) < TAIL:
        return None
    return t[572], t[573], struct.unpack('<Q', t[580:588])[0], sz


def enc_ranges(mode, pct, n):
    """被加密的字节区间 [(s,e), ...]；未知模式返回 None"""
    if mode == 0x24:
        return [(0, n)]
    if mode == 0x26:
        return [(0, min(n, 1 << 20))]
    if mode == 0x25:
        w = 4 * (n // 100)
        if w <= 0:
            return [(0, n)]
        gap = (n - 3 * w) // 2
        if gap < 0:
            return [(0, n)]
        return [(0, w), (w + gap, 2 * w + gap), (2 * (w + gap), 3 * w + 2 * gap)]
    return None


def plain_ranges(n, enc):
    """加密区间之外的明文区间"""
    out = []
    pos = 0
    for s, e in sorted(enc):
        if s > pos:
            out.append((pos, min(s, n)))
        pos = max(pos, e)
    if pos < n:
        out.append((pos, n))
    return [(s, e) for s, e in out if e > s]


# ---------- 工具 ----------

def find_all(hay, needle, limit=64):
    out = []
    i = hay.find(needle)
    while i >= 0 and len(out) < limit:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def magic_of(buf):
    for m, kind in MAGICS:
        if buf[:len(m)] == m:
            return kind
    return None


def validate(buf):
    """结构校验：判断候选件"双击能不能打开"。返回 (ok, 说明)"""
    if not buf:
        return False, '空文件'
    kind = magic_of(buf)
    if kind == 'zip':
        try:
            z = zipfile.ZipFile(_BytesIO(buf))
            bad = z.testzip()
            n = len(z.namelist())
            if bad:
                return False, 'zip 有坏成员 %s' % bad
            return True, 'zip %d 个成员全部可读' % n
        except Exception as e:
            return False, 'zip 打不开: %r' % (e,)
    if kind == 'pdf':
        if b'%%EOF' in buf[-4096:] or b'%%EOF' in buf:
            return True, 'pdf 有 %%EOF'
        return False, 'pdf 没有 %%EOF'
    if kind == 'png':
        return (True, 'png 有 IEND') if b'IEND\xaeB`\x82' in buf else (False, 'png 没有 IEND')
    if kind == 'jpeg':
        return (True, 'jpeg 有 FFD9') if b'\xff\xd9' in buf else (False, 'jpeg 没有 FFD9')
    if kind in ('ole2', 'sqlite', 'gz', 'rar', '7z'):
        return True, '%s 头正常' % kind
    return False, '认不出格式'


def _BytesIO(data):
    import io
    return io.BytesIO(data)


def write_safe(path, data):
    final = path
    if os.path.exists(path):
        i = 1
        while os.path.exists('%s.r%d' % (path, i)):
            i += 1
        final = '%s.r%d' % (path, i)
    d = os.path.dirname(final)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = final + '.wtmp'
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, final)
    return final


# ---------- 主分析 ----------

def analyze(jrn, cipher, outdir=None):
    """在日志镜像里找这个密文对应的旧内容。返回一条结果字典。"""
    rec = {'cipher': cipher}
    meta = footer_meta(cipher)
    if not meta:
        rec['status'] = 'no_footer'
        return rec
    mode, pct, orig, sz = meta
    rec['mode'] = '0x%02x' % mode
    rec['orig_len'] = orig
    if orig <= 0:
        rec['status'] = 'bad_footer'
        return rec
    enc = enc_ranges(mode, pct, orig)
    if enc is None:
        rec['status'] = 'unknown_mode'
        return rec
    pr = plain_ranges(orig, enc)
    if not pr:
        rec['status'] = 'whole_encrypted'      # 0x24：没有明文指纹可用
        return rec

    with open(cipher, 'rb') as f:
        plains = {}
        for s, e in pr:
            f.seek(s)
            plains[(s, e)] = f.read(e - s)

    # 探针：从最大的明文区里取 3 个
    s0, e0 = max(pr, key=lambda x: x[1] - x[0])
    span = e0 - s0
    probes = []
    for frac in (0.25, 0.5, 0.75):
        off = s0 + int(span * frac)
        chunk = plains[(s0, e0)][off - s0:off - s0 + PROBE_LEN]
        if len(chunk) == PROBE_LEN:
            probes.append((off, chunk))

    deltas = {}
    probe_hits = 0
    for off, chunk in probes:
        occ = find_all(jrn, chunk)
        if occ:
            probe_hits += 1
        for j in occ:
            deltas.setdefault(j - off, set()).add(off)

    if not deltas:
        rec['status'] = 'not_in_journal'
        rec['probe_hits'] = probe_hits
        return rec

    # 多个探针指向同一个 delta 才算可靠
    best, conf = None, 'single'
    for d, offs in sorted(deltas.items(), key=lambda kv: -len(kv[1])):
        if len(offs) >= 2:
            best, conf = d, 'multi'
            break
    if best is None:
        best, conf = sorted(deltas.items(), key=lambda kv: -len(kv[1]))[0][0], 'single'

    rec['delta'] = best
    rec['confidence'] = conf
    rec['probe_hits'] = probe_hits

    # 拼装候选：加密区取日志（旧内容），明文区取密文（未变）
    buf = bytearray(orig)
    enc_cov = 0
    for s, e in enc:
        js = best + s
        if js < 0 or js >= len(jrn):
            continue
        n = min(e - s, len(jrn) - js)
        buf[s:s + n] = jrn[js:js + n]
        enc_cov += n
    for s, e in pr:
        buf[s:e] = plains[(s, e)]

    rec['enc_bytes'] = sum(e - s for s, e in enc)
    rec['enc_recovered'] = enc_cov
    rec['coverage'] = round(enc_cov / float(rec['enc_bytes'] or 1), 4)

    # 文件头在加密区里时，日志里的魔数就是独立证据
    head_in_enc = any(s <= 0 < e for s, e in enc)
    kind = magic_of(bytes(buf))
    rec['magic'] = kind
    rec['magic_from_journal'] = bool(kind) and head_in_enc

    ok, detail = validate(bytes(buf))
    rec['structural_ok'] = ok
    rec['detail'] = detail

    if outdir and ok:
        rel = os.path.basename(cipher)
        if rel.endswith('.L0CK3D'):
            rel = rel[:-7]
        dst = os.path.join(outdir, rel)
        final = write_safe(dst, bytes(buf))
        rec['out'] = final
        rec['status'] = 'candidate_written'
    elif outdir:
        rec['status'] = 'candidate_rejected'
    else:
        rec['status'] = 'candidate'
    return rec


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    jrn_path = args[0]
    ciphers = []
    outdir = None
    max_files = 200
    i = 1
    while i < len(args):
        if args[i] == '--cipher':
            ciphers.append(args[i + 1]); i += 2
        elif args[i] == '--cipher-dir':
            root = args[i + 1]; i += 2
            for dp, dns, fns in os.walk(root):
                for fn in sorted(fns):
                    if fn.endswith('.L0CK3D'):
                        ciphers.append(os.path.join(dp, fn))
        elif args[i] == '--out':
            outdir = args[i + 1]; i += 2
        elif args[i] == '--max-files':
            max_files = int(args[i + 1]); i += 2
        else:
            i += 1

    if not ciphers:
        sys.exit('要给 --cipher 或 --cipher-dir')
    ciphers = ciphers[:max_files]
    if not os.path.isfile(jrn_path):
        sys.exit('日志镜像不存在: %s' % jrn_path)

    with open(jrn_path, 'rb') as f:
        jrn = f.read()
    print('日志镜像 %s（%d 字节）' % (jrn_path, len(jrn)))

    counts = {}
    for c in ciphers:
        try:
            rec = analyze(jrn, c, outdir)
        except Exception as e:
            rec = {'cipher': c, 'status': 'error', 'detail': repr(e)[:200]}
        st = rec.get('status', '?')
        counts[st] = counts.get(st, 0) + 1
        line = '%-58s %-12s' % (os.path.basename(rec['cipher'])[:58], st)
        if 'delta' in rec:
            line += ' delta=%d(%s) 覆盖=%s' % (rec['delta'], rec['confidence'],
                                              rec.get('coverage', '-'))
        if 'magic' in rec:
            line += ' 魔数=%s' % (rec['magic'] or '无')
        line += ' %s' % (rec.get('detail', ''))
        print(line)
        if rec.get('out'):
            print('    -> %s' % rec['out'])

    print('\n统计: %s' % ', '.join('%s=%d' % (k, v) for k, v in sorted(counts.items())))
    print('注意: 写出来的都是**候选件**，还要过 validate_installed.py / deep_validate.py 才算恢复成功。')


if __name__ == '__main__':
    main()
