#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carve_osheet_sig.py —— 在 Btrfs 数据区按 `.osheet` 容器强签名顺序扫描，把被整文件加密后
丢失的原件捞回来（路线 D 的精确版）。

签名怎么来（三种，任选其一，按优先级）：
  1) --sig-file <一个完好的 .osheet>   取它的头部若干字节作为签名（最稳，任何用户/版本都行）
  2) --uid 1042                        按标准布局拼 39 字节签名（uid 必须是 4 位数字）
  3) --sig-hex <hex>                   直接给十六进制签名

标准布局（现代版 WPS OSheet，头部 39 字节固定）：
  78 00 00 00 | 12 "schema\0" 03 00 00 00 | 00 00 00 | 08 "enc\0" 00 | 02 "id\0" 23 00 00 00 | "<uid>_"

用法：
  python3 carve_osheet_sig.py <输出目录> [--sig-file F | --uid N | --sig-hex HEX] <窗口起> <窗口止> [更多窗口...]
  例：python3 carve_osheet_sig.py /tmp/carve_out --uid 1042 2180000000000 2250000000000

只读：只 open(O_RDONLY) 读块设备 + 跑 dump-tree，不写卷。
"""
import os, re, struct, subprocess, sys, time

DEV = os.environ.get("C3R_DEV", "/dev/mapper/cachedev_0")
CHUNK = 64 << 20
OVERLAP = 64


def run(cmd):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT).stdout.decode("utf-8", "replace")
    except OSError:
        return ""


def dump_tree(dev, tree):
    for cmd in (["btrfs", "inspect-internal", "dump-tree", "-t", str(tree), dev],
                ["btrfs-debug-tree", "-t", str(tree), dev]):
        out = run(cmd)
        if "item " in out:
            return out
    raise SystemExit("无法运行 dump-tree")


def parse_chunks(text):
    chunks = []
    for blk in re.split(r'\n\s*item \d+ key ', text):
        m = re.search(r'CHUNK_ITEM (\d+)\)', blk)
        if not m:
            continue
        ln = re.search(r'chunk length (\d+)', blk)
        ty = re.search(r'\n\s*type (\S+)', blk)
        if not (ln and ty):
            continue
        stripes = [(int(a), int(b)) for a, b in re.findall(r'stripe \d+ devid (\d+) offset (\d+)', blk)]
        if stripes:
            chunks.append((int(m.group(1)), int(ln.group(1)), ty.group(1), stripes))
    return chunks


def log(*a):
    print("[%s]" % time.strftime("%H:%M:%S"), *a)
    sys.stdout.flush()


def sig_from_file(path):
    """取一个完好 .osheet 的头部作为签名：magic 之后到 id 字段结束（含 uid_ 前缀）。"""
    data = open(path, "rb").read(256)
    if data[:4] != b"\x78\x00\x00\x00":
        raise SystemExit("模板文件不是 OSheet 容器：%s" % path)
    i = data.find(b"\x02id\x00")
    if i < 0:
        raise SystemExit("模板文件里找不到 id 字段：%s" % path)
    n = struct.unpack_from('<I', data, i + 5)[0]          # id 值长
    end = i + 5 + 4 + n
    return data[:end], end


def sig_from_uid(uid):
    s = "%d_" % uid
    if len(s) != 5:
        raise SystemExit("uid 必须拼成 5 字节（4 位数字 + 下划线），当前是 %r" % s)
    return (bytes.fromhex("7800000012736368656d6100030000000000000008656e6300000269640023000000")
            + s.encode()), 39


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    outdir = argv[0]
    argv = argv[1:]
    sig = None
    windows = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--sig-file":
            sig, _ = sig_from_file(argv[i + 1]); i += 2
        elif a == "--uid":
            sig, _ = sig_from_uid(int(argv[i + 1])); i += 2
        elif a == "--sig-hex":
            sig = bytes.fromhex(argv[i + 1]); i += 2
        else:
            windows.append((int(a), int(argv[i + 1]))); i += 2
    if sig is None:
        raise SystemExit("必须给签名来源：--sig-file / --uid / --sig-hex")
    if not windows:
        raise SystemExit("至少给一个扫描窗口（起始 结束）")
    os.makedirs(outdir, exist_ok=True)
    log("签名 %d 字节: %s" % (len(sig), sig.hex()))

    chunks = parse_chunks(dump_tree(DEV, 3))
    data = sorted([(c[0], c[1], c[3][0][1]) for c in chunks if "DATA" in c[2]])
    log("DATA chunk %d 个" % len(data))

    def log2phys(a):
        for ls, ln, off in data:
            if ls <= a < ls + ln:
                return off + (a - ls)
        return None

    fd = os.open(DEV, os.O_RDONLY)
    t0 = time.time()
    scanned = 0
    hits = 0
    for wstart, wend in windows:
        log("=== 窗口 %.4f ~ %.4f TB（%.1f GB）===" %
            (wstart / 1e12, wend / 1e12, (wend - wstart) / 1e9))
        pos = wstart
        while pos < wend:
            want = min(CHUNK, wend - pos)
            phys = log2phys(pos)
            if phys is None:
                nxt = None
                for ls, ln, off in data:
                    if ls > pos:
                        nxt = ls
                        break
                if nxt is None:
                    break
                pos = nxt
                continue
            buf = os.pread(fd, want, phys)
            if not buf:
                break
            scanned += len(buf)
            p = 0
            while True:
                p = buf.find(sig, p)
                if p < 0:
                    break
                hits += 1
                log("  *** 签名命中 逻辑地址 %d（物理 %d）" % (pos + p, phys + p))
                p += 1
            step = want - OVERLAP
            pos += step if step > 0 else want
            if scanned % (1 << 30) < want:
                el = time.time() - t0
                log("  已扫 %.2f GiB 用时 %.0fs 速率 %.0f MB/s 命中 %d"
                    % (scanned / (1 << 30), el, scanned / 1e6 / max(el, 1), hits))
    log("扫描结束 %.2f GiB / %.0fs，命中 %d" % (scanned / (1 << 30), time.time() - t0, hits))
    os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
