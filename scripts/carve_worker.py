#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carve_worker.py —— 按 DATA chunk 精确划分的多路并行 `.osheet` 签名扫描器。

与 carve_osheet_sig.py 的区别：不用「逻辑地址窗口」而是**直接把 DATA chunk 列表**
按物理量切成 N 组，每组内每个 chunk 单独完整扫描。这样既不会在窗口边界漏扫，
也能让各路负载均衡。

只读：只 open(O_RDONLY) 读块设备 + 跑 dump-tree，不写卷。

用法：
  python3 carve_worker.py <日志目录> --sig-hex <HEX> --worker N --workers M
"""
import os
import re
import struct
import subprocess
import sys
import time

DEV = os.environ.get("C3R_DEV", "/dev/mapper/cachedev_0")
CHUNK_IO = 64 << 20
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


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    logdir = argv[0]
    argv = argv[1:]
    sigs = []
    worker = 0
    workers = 1
    statefile = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--sig-hex":
            for h in argv[i + 1].split(","):
                if h.strip():
                    sigs.append(bytes.fromhex(h.strip()))
            i += 2
        elif a == "--uid":
            u = int(argv[i + 1]); i += 2
            s = "%d_" % u
            if len(s) != 5:
                raise SystemExit("uid 必须 4 位数字 + 下划线")
            sigs.append(bytes.fromhex(
                "7800000012736368656d6100030000000000000008656e6300000269640023000000")
                + s.encode())
        elif a == "--sig-file":
            data = open(argv[i + 1], "rb").read(256)
            j = data.find(b"\x02id\x00")
            if data[:4] != b"\x78\x00\x00\x00" or j < 0:
                raise SystemExit("模板不是 OSheet 容器")
            n = struct.unpack_from('<I', data, j + 5)[0]
            sigs.append(data[:j + 5 + 4 + n])
            i += 2
        elif a == "--worker":
            worker = int(argv[i + 1]); i += 2
        elif a == "--workers":
            workers = int(argv[i + 1]); i += 2
        elif a == "--shard":
            worker = int(argv[i + 1]); i += 2
        elif a == "--shards":
            workers = int(argv[i + 1]); i += 2
        elif a == "--state":
            statefile = argv[i + 1]; i += 2
        else:
            raise SystemExit("未知参数 %r" % a)
    if not sigs:
        raise SystemExit("必须给签名来源")
    # 长签名优先，短签名命中已被长签名覆盖的位置时跳过
    sigs.sort(key=len, reverse=True)
    os.makedirs(logdir, exist_ok=True)

    chunks = parse_chunks(dump_tree(DEV, 3))
    # dump-tree 会把 chunk 列表输出两遍，按逻辑起始去重（否则每个 chunk 白扫一次）
    _seen = set()
    _uniq = []
    for c in chunks:
        if "DATA" not in c[2]:
            continue
        if c[0] in _seen:
            continue
        _seen.add(c[0])
        _uniq.append((c[0], c[1], c[3][0][1]))
    data = sorted(_uniq, key=lambda c: c[2])
    total = sum(c[1] for c in data)

    # 按物理量切成 workers 组，保证每组覆盖连续物理区间
    per = total // workers
    groups = [[] for _ in range(workers)]
    g = 0
    acc = 0
    for idx, c in enumerate(data):
        groups[g].append(idx)
        acc += c[1]
        if g < workers - 1 and acc >= per * (g + 1):
            g += 1
    mine_idx = groups[worker]
    mine = [data[j] for j in mine_idx]

    done = {}
    if statefile and os.path.exists(statefile):
        for line in open(statefile):
            line = line.strip()
            if not line or line == "COMPLETE":
                continue
            parts = line.split()
            try:
                done[int(parts[0])] = int(parts[1])
            except Exception:
                pass
    # todo: [(chunk 索引, 已扫字节偏移)]
    todo = []
    for j in mine_idx:
        off = done.get(j, 0)
        if off < data[j][1]:
            todo.append((j, off))
    log("签名 %d 个: %s" % (len(sigs), " | ".join(s.hex() for s in sigs)))
    log("DATA chunk %d 个 / 总 %.1f GB，本路 %d 个 chunk / %.1f GB（待扫 %d 段）"
        % (len(data), total / 1e9, len(mine), sum(c[1] for c in mine) / 1e9, len(todo)))

    fd = os.open(DEV, os.O_RDONLY)
    t0 = time.time()
    scanned = 0
    hits = 0
    seen = set()
    sf = open(statefile, "a") if statefile else None
    try:
        for j, start_off in todo:
            (ls, ln, poff) = data[j]
            pos = start_off
            while pos < ln:
                want = min(CHUNK_IO, ln - pos)
                buf = os.pread(fd, want, poff + pos)
                if not buf:
                    break
                scanned += len(buf)
                for si, sig in enumerate(sigs):
                    p = 0
                    while True:
                        p = buf.find(sig, p)
                        if p < 0:
                            break
                        # 8 字节短签名的命中做二次确认：后面必须是 "ema\0" + 长度(2|3)
                        if len(sig) <= 8:
                            if (buf[p + 8:p + 12] != b'ema\x00' or
                                    buf[p + 12] not in (2, 3) or
                                    buf[p + 13:p + 16] != b'\x00\x00\x00'):
                                p += 1
                                continue
                        abs_p = poff + pos + p
                        if abs_p not in seen:
                            seen.add(abs_p)
                            hits += 1
                            ctx = buf[p:p + 56].hex()
                            log("*** 签名命中 逻辑地址 %d（物理 %d） sig#%d HEAD=%s"
                                % (ls + pos + p, abs_p, si, ctx))
                        p += 1
                # 步进必须严格大于 0：want <= OVERLAP 时若仍按 want-OVERLAP 推进，
                # pos 将永远停在 chunk 末尾，worker 陷入死循环（每轮只读 64 字节、
                # 反复往 state 文件追加同一行）。曾因此把 5.68 亿行 / 8.6GB 的
                # state 文件写满 /tmp，扫描实际进度为 0。
                step = (want - OVERLAP) if want > OVERLAP else want
                if step <= 0:
                    # 兜底：步进必须为正，否则 pos 不动 → 死循环
                    log("!! 步进为 0，放弃该 chunk（chunk %d pos=%d want=%d）" % (j, pos, want))
                    break
                pos += step
                if sf:
                    sf.write("%d %d\n" % (j, pos))
                    sf.flush()
            el = time.time() - t0
            log("进度 %.2f GiB 用时 %.0fs 速率 %.0f MB/s 命中 %d (chunk %d/%d)"
                % (scanned / (1 << 30), el, scanned / 1e6 / max(el, 1), hits, j, len(data) - 1))
    finally:
        if sf:
            sf.close()
        os.close(fd)
    log("结束 %.2f GiB / %.0fs，命中 %d" % (scanned / (1 << 30), time.time() - t0, hits))
    if statefile and not todo:
        with open(statefile, "a") as f:
            f.write("COMPLETE\n")
        log("SHARD_COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
