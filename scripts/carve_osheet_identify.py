#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carve_osheet_identify.py —— 从雕刻日志的命中地址里认领目标原件。

认领判据（缺一不可）：
  1. 容器尾部标记定位真实长度：尾部 = [u32 尾部长度][0x08]["end\\0"][值]
     文件长度 = (尾部标记起点 − 4) + 该 u32   ← u32 是尾部段自身长度，不是文件长度
  2. `osheet_strict.py` 严格解析必须完全自洽
  3. 长度命中目标清单（--len 给出，通常取勒索标记里的 orig_len）→ 打印工作表名与首行

用法：
  python3 carve_osheet_identify.py --len 48250 19559 --label 客服上班表-最新 保税区退款订单 --log /tmp/carve_s*.log
  （--len 收一个或多个长度；--label 与 --len 一一对应；日志一律用 --log 指定）
"""
import os, re, sys, json, struct, subprocess, importlib.util

DEV = os.environ.get("C3R_DEV", "/dev/mapper/codedev_0")
DEV = os.environ.get("C3R_DEV", "/dev/mapper/cachedev_0")
RE = re.compile(r"签名命中 逻辑地址 (\d+)")
STRICT = os.environ.get("C3R_STRICT", "/tmp/osheet_strict.py")


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


chunks = parse_chunks(dump_tree(DEV, 3))
data = sorted([(c[0], c[1], c[3][0][1]) for c in chunks if "DATA" in c[2]])


def log2phys(a):
    for ls, ln, off in data:
        if ls <= a < ls + ln:
            return off + (a - ls)
    return None


def read_container(fd, phys, cap=64 << 20):
    buf = b""
    off = 0
    while off < cap:
        part = os.pread(fd, 2 << 20, phys + off)
        if not part:
            break
        buf += part
        i = buf.find(b"\x08end\x00")
        if i >= 4:
            tlen = struct.unpack_from('<I', buf, i - 4)[0]
            total = (i - 4) + tlen
            if 0 < total <= len(buf):
                return buf[:total], total
        off += 2 << 20
    return None, 0


def load_strict():
    spec = importlib.util.spec_from_file_location("st", STRICT)
    st = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(st)
    return st


def main():
    import argparse
    ap = argparse.ArgumentParser(description="从雕刻日志认领目标 .osheet 原件")
    ap.add_argument("--len", dest="lengths", type=int, nargs="+", required=True,
                    help="目标文件长度（勒索标记里的 orig_len），可给多个")
    ap.add_argument("--label", dest="labels", nargs="+", default=None,
                    help="与 --len 一一对应的名字")
    ap.add_argument("--log", dest="logs", nargs="+", required=True,
                    help="扫描日志文件")
    ap.add_argument("--out", default="/tmp", help="认领出来的容器存放目录")
    args = ap.parse_args()

    lengths = list(args.lengths)
    labels = list(args.labels) if args.labels else [str(x) for x in lengths]
    if len(labels) < len(lengths):
        labels += [str(x) for x in lengths[len(labels):]]
    logs = list(args.logs)
    st = load_strict()

    addrs = []
    for lg in logs:
        if not os.path.exists(lg):
            print("跳过不存在的日志:", lg)
            continue
        for line in open(lg, "rb"):
            m = RE.search(line.decode("utf-8", "replace"))
            if m:
                addrs.append(int(m.group(1)))
    addrs = sorted(set(addrs))
    print("日志命中地址 %d 个（去重）" % len(addrs))

    fd = os.open(DEV, os.O_RDONLY)
    found = 0
    for a in addrs:
        phys = log2phys(a)
        if phys is None:
            continue
        data_c, total = read_container(fd, phys)
        if data_c is None:
            continue
        try:
            header, entries, end = st.parse_strict(data_c)
            ok = "自洽"
        except Exception as e:
            entries = None
            ok = "解析失败:" + str(e)[:36]
        if total not in lengths:
            continue
        found += 1
        label = labels[lengths.index(total)]
        print("=" * 70)
        print("★★ 目标长度命中：%s（%d 字节）逻辑 %d 物理 %d  %s" % (label, total, a, phys, ok))
        out = os.path.join(args.out, "target_%s.osheet" % label)
        with open(out, "wb") as f:
            f.write(data_c)
        print("   已存 %s" % out)
        if entries:
            for _pos, _X, name, payload in entries:
                n = name[3:] if name.startswith("@C_") else name
                if n == "text/index":
                    try:
                        idx, _ = json.JSONDecoder().raw_decode(payload.decode("utf-8", "replace"))
                        print("   工作表: %s  顺序 %s" %
                              ([(x, v.get("title")) for x, v in idx.get("sheets", {}).items()],
                               idx.get("order")))
                    except Exception as e:
                        print("   index 解析失败:", e)
                if n.startswith("text/sh_"):
                    try:
                        sh, _ = json.JSONDecoder().raw_decode(payload.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    cells = sh.get("cells", {})
                    print("   分片 %s rowCount=%s colCount=%s 有数据行=%d"
                          % (n, sh.get("rowCount"), sh.get("colCount"), len(cells)))
                    for r in sorted(cells)[:2]:
                        row = cells[r]
                        print("      行%s: %s" % (r, [(c, row[c].get("v") if isinstance(row[c], dict) else row[c])
                                                    for c in sorted(row)[:10]]))
    os.close(fd)
    print("认领结果: %d" % found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
