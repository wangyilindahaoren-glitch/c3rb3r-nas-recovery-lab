#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ole2_lib.py —— OLE2(.xls/.doc/.ppt) 容器解析库（只读块设备）

为什么需要它
------------
OLE2 文件**没有结尾签名**，所以雕刻时不能像 zip/pdf 那样"按长度配对"，
只能先按 OLE2 魔数找到候选，再**解析容器结构**算出它的真实分配长度，
最后**读出正文**（WordDocument / Workbook 流）按内容认领。

2026-11-02 实测：修正长度算法后，
12 个 .xls/.doc 目标里 **4 个 .doc 靠正文内容 100% 认准**（长度精确相等 + 正文标题吻合），
另外 8 个 .xls 长度也全对但正文期间/品牌与文件名不符 → 按纪律**不装回**，只作候选交付。

⚠️ 关键坑：`scripts/ole2_size.py` 的长度公式比真实分配长度**少 512 字节**，
   据此认领会把真候选全部排除（见 references/pitfalls.md 2026-11-02 条）。
   本库用「FAT 里出现过的最大扇区号 + 1」算长度，是对的。

用法
----
    import sys; sys.path.insert(0, '<skill>/scripts')
    from ole2_lib import Ole, rd, biff_sheets, biff_sst, doc_cjk
    o = Ole(device_offset)      # 设备上的物理偏移
    o.alloc_len()               # 真实分配长度
    o.names()                   # 目录项名字
    o.stream('WordDocument')    # 取流（自动处理迷你流）
"""
import os
import re
import struct

DEV = os.environ.get('C3R_DEV', '/dev/mapper/cachedev_0')
OLE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
ENDOFCHAIN = 0xFFFFFFFE
_fds = {}


def _fd(dev=None):
    dev = dev or DEV
    if dev not in _fds:
        _fds[dev] = os.open(dev, os.O_RDONLY)
    return _fds[dev]


def rd(off, n, dev=None):
    """从设备读 n 字节（off 是物理偏移）"""
    fd = _fd(dev)
    out = bytearray()
    while len(out) < n:
        b = os.pread(fd, min(1 << 20, n - len(out)), off + len(out))
        if not b:
            break
        out += b
    return bytes(out)


class Ole:
    """一个 OLE2 容器（从设备上的物理偏移构造）"""

    def __init__(self, off, dev=None):
        self.dev = dev or DEV
        self.base = off
        h = rd(off, 512, self.dev)
        if len(h) < 512 or h[:8] != OLE:
            raise ValueError('not an OLE2 file at offset %d' % off)
        self.S = 1 << struct.unpack_from('<H', h, 0x1E)[0]      # 扇区大小
        self.miniS = 1 << struct.unpack_from('<H', h, 0x20)[0]  # 迷你扇区大小
        self.mini_cut = struct.unpack_from('<I', h, 0x38)[0]    # 迷你流阈值(4096)
        self.nfats = struct.unpack_from('<I', h, 0x2C)[0]
        self.dirstart = struct.unpack_from('<I', h, 0x30)[0]
        self.minifat_start = struct.unpack_from('<I', h, 0x3C)[0]
        self.nminifats = struct.unpack_from('<I', h, 0x40)[0]
        self.difat_start = struct.unpack_from('<I', h, 0x44)[0]
        self.ndifats = struct.unpack_from('<I', h, 0x48)[0]
        difat = list(struct.unpack_from('<109I', h, 0x4C))
        ds = self.difat_start
        for _ in range(self.ndifats + 2):        # DIFAT 链
            if ds >= 0xFFFFFFFA:
                break
            blk = rd(off + 512 + ds * self.S, self.S, self.dev)
            vals = struct.unpack_from('<%dI' % (self.S // 4), blk, 0)
            difat.extend(vals[:-1])
            ds = vals[-1]
        self.fat = []
        for fs in difat:
            if fs >= 0xFFFFFFFA:
                continue
            blk = rd(off + 512 + fs * self.S, self.S, self.dev)
            if len(blk) < self.S:
                break
            self.fat.extend(struct.unpack_from('<%dI' % (self.S // 4), blk, 0))
        self.dirs = []
        for sec in self.chain(self.dirstart):
            blk = rd(off + 512 + sec * self.S, self.S, self.dev)
            for i in range(self.S // 128):
                e = blk[i * 128:(i + 1) * 128]
                if len(e) < 128:
                    break
                nlen = struct.unpack_from('<H', e, 64)[0]
                if nlen < 2 or nlen > 64:
                    continue
                nm = e[:nlen - 2].decode('utf-16-le', 'replace')
                typ = e[66]
                start = struct.unpack_from('<I', e, 116)[0]
                size = struct.unpack_from('<Q', e, 120)[0]
                if typ == 0:
                    continue
                self.dirs.append({'name': nm, 'type': typ, 'start': start, 'size': size})
        root = self.dirs[0] if self.dirs else None
        self.ministream = b''
        if root and root['type'] == 5:
            self.ministream = self.read_chain(root['start'], root['size'], mini=False)
        self.minifat = []
        for sec in self.chain(self.minifat_start)[:self.nminifats or 9999]:
            blk = rd(off + 512 + sec * self.S, self.S, self.dev)
            self.minifat.extend(struct.unpack_from('<%dI' % (self.S // 4), blk, 0))

    def chain(self, start, limit=4096):
        out = []
        s = start
        seen = set()
        while s < 0xFFFFFFFA and len(out) < limit and s not in seen:
            seen.add(s)
            out.append(s)
            s = self.fat[s] if s < len(self.fat) else ENDOFCHAIN
        return out

    def read_chain(self, start, size, mini):
        out = bytearray()
        if mini:                      # 迷你流：在 Root Entry 的流里按迷你 FAT 链走
            for s in self.chain(start):
                out += self.ministream[s * self.miniS:(s + 1) * self.miniS]
        else:
            for s in self.chain(start):
                out += rd(self.base + 512 + s * self.S, self.S, self.dev)
        return bytes(out[:size]) if size else bytes(out)

    def stream(self, name):
        for d in self.dirs:
            if d['name'] == name:
                return self.read_chain(d['start'], d['size'], d['size'] < self.mini_cut)
        return b''

    def names(self):
        return [d['name'] for d in self.dirs]

    def alloc_len(self):
        """真实分配长度 = 512 + (FAT 里用到的最大扇区号 + 1) * 扇区大小"""
        used = set()
        for v in self.fat:
            if v < 0xFFFFFFFC:
                used.add(v)
        used.add(self.dirstart)
        return 512 + (max(used) + 1) * self.S

    def max_stream_end(self):
        """目录里声明的流最远扇区 -> 文件至少要这么长（交叉校验用）"""
        best = 0
        for d in self.dirs:
            if d['type'] == 5 or d['size'] < self.mini_cut:
                continue
            best = max(best, d['start'] + (d['size'] + self.S - 1) // self.S)
        return 512 + best * self.S


def biff_sheets(wb):
    """BIFF8 BOUNDSHEET -> 工作表名

    ⚠️ 记录布局：rec(2) sz(2) lbPlyPos(4) hsState(1) dt(1) cch(1) rgch
       → cch 在记录起点 +10，名称从 +11 开始；dt&1 == 1 是 unicode（cch*2 字节）。
    """
    out = []
    i = 0
    while i + 4 <= len(wb):
        rec, sz = struct.unpack_from('<HH', wb, i)
        if rec == 0x0085 and i + 11 + sz <= len(wb):
            bo = i + 11
            cch = wb[bo - 1]
            grbit = wb[bo - 2]
            try:
                if grbit & 1:
                    nm = wb[bo:bo + cch * 2].decode('utf-16-le', 'ignore')
                else:
                    nm = wb[bo:bo + cch].decode('gbk', 'ignore')
            except Exception:
                nm = ''
            out.append(nm)
        i += 4 + sz + (sz & 1)
        if i > 4 * 1024 * 1024:
            break
    return out


def biff_sst(wb):
    """BIFF8 SST(+CONTINUE) -> 共享字符串表"""
    out = []
    i = 0
    chunks = []
    while i + 4 <= len(wb):
        rec, sz = struct.unpack_from('<HH', wb, i)
        if rec == 0x00FC:
            chunks = [wb[i + 4:i + 4 + sz]]
            j = i + 4 + sz + (sz & 1)
            while j + 4 <= len(wb):
                r2, s2 = struct.unpack_from('<HH', wb, j)
                if r2 == 0x003C:
                    chunks.append(wb[j + 4:j + 4 + s2])
                    j += 4 + s2 + (s2 & 1)
                else:
                    break
            break
        i += 4 + sz + (sz & 1)
        if i > 4 * 1024 * 1024:
            break
    if not chunks:
        return out
    data = b''.join(chunks)
    if len(data) < 8:
        return out
    nstr = struct.unpack_from('<I', data, 4)[0]
    p = 8
    for _ in range(min(nstr, 20000)):
        if p + 3 > len(data):
            break
        cch = struct.unpack_from('<H', data, p)[0]
        p += 2
        grbit = data[p]
        p += 1
        if grbit & 0x08:
            p += 2
        if grbit & 0x04:
            p += 4
        if grbit & 0x01:
            s = data[p:p + cch * 2].decode('utf-16-le', 'ignore')
            p += cch * 2
        else:
            s = data[p:p + cch].decode('gbk', 'ignore')
            p += cch
        out.append(s)
        if p >= len(data):
            break
    return out


def doc_cjk(wd, limit=4000):
    """WordDocument 流里的中文串（UTF-16LE，≥4 字连排）"""
    s = wd.decode('utf-16-le', 'ignore')
    res = []
    for m in re.finditer(r'[\u4e00-\u9fff]{4,}', s):
        res.append(m.group())
        if len(res) >= limit:
            break
    return res


def text_hits(buf, words):
    """在原始字节里按 GBK / UTF-16LE 两种编码搜关键词 -> [(词, 编码, 偏移)]"""
    hits = []
    for w in words:
        for enc in ('utf-16-le', 'gbk'):
            b = w.encode(enc)
            k = buf.find(b)
            if k >= 0:
                hits.append((w, enc, k))
                break
    return hits


def context(buf, off, back=300, fwd=700, enc='utf-16-le'):
    """取关键词附近的文字上下文（可读）"""
    a = max(0, off - back)
    b = min(len(buf), off + fwd)
    s = buf[a:b].decode(enc, 'ignore')
    return ''.join(c if (c.isprintable() or c in '\n\t') else '·' for c in s)
