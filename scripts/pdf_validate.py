#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 严格校验：判断一个 PDF "能不能打开"，而不是"看着像不像 PDF"。

三种坏法，只有第一种会被常见脚本漏掉：
  1) 没有 Catalog /Root      → 阅读器直接拒绝打开（"无效的 PDF"）
  2) xref 偏移指错位置        → 阅读器报"文件已损坏"或弹修复提示
  3) 页面的 /Contents 缺失    → 能打开，但页面是空白

用法：
  python3 pdf_validate.py <文件或目录> ...
  python3 pdf_validate.py --quiet <目录>    # 只在有问题时输出
退出码：0 = 全部通过，1 = 有问题
"""
import os
import re
import sys

PAGE_RE = re.compile(rb'/Type\s*/Page(?![sA-Za-z])')
OBJ_RE = re.compile(rb'(?<![0-9])(\d+)\s+(\d+)\s+obj\b')
CONT_RE = re.compile(rb'/Contents\s+(\d+)\s+0\s+R')
CONT_ARR_RE = re.compile(rb'/Contents\s*\[([^\]]*)\]')


def check(data):
    """返回 (是否合格, 说明, 页面数)"""
    if not data.startswith(b'%PDF-'):
        return False, '没有 %PDF 头', 0
    if b'%%EOF' not in data[-2048:]:
        return False, '结尾没有 %%EOF', 0
    ms = list(re.finditer(rb'startxref\s+(\d+)', data[-2048:]))
    if not ms:
        return False, '没有 startxref', 0
    off = int(ms[-1].group(1))
    if off >= len(data):
        return False, 'startxref 越界', 0
    if not data[off:off + 4].startswith(b'xref'):
        return False, 'startxref 不指向 xref 表', 0
    if b'/Root' not in data:
        return False, '没有 /Root', 0
    if b'/Type/Catalog' not in data and b'/Type /Catalog' not in data:
        return False, '没有 Catalog（阅读器必然拒绝打开）', 0
    # xref 偏移核对
    tail = data[off:]
    em = re.search(rb'xref\s+0\s+(\d+)\s+', tail)
    if not em:
        return False, 'xref 表格式不对', 0
    n = int(em.group(1))
    ent = tail[em.end():em.end() + 20 * n]
    bad = 0
    for i in range(1, n):
        l = ent[i * 20:(i + 1) * 20]
        if len(l) < 18:
            continue
        if l[17:18] == b'n':
            o = int(l[0:10])
            if not re.match(rb'%d\s+0\s+obj' % i, data[o:o + 24]):
                bad += 1
    # 页面与内容流
    nums = set(int(m.group(1)) for m in OBJ_RE.finditer(data))
    pages = 0
    pages_with_content = 0
    for m in OBJ_RE.finditer(data):
        end = data.find(b'endobj', m.end())
        if end < 0:
            continue
        body = data[m.end():end]
        if not PAGE_RE.search(body):
            continue
        pages += 1
        refs = [int(x) for x in CONT_RE.findall(body)]
        if not refs:
            for arr in CONT_ARR_RE.findall(body):
                refs += [int(x) for x in re.findall(rb'(\d+)\s+0\s+R', arr)]
        if refs and all(r in nums for r in refs):
            pages_with_content += 1
    ok = (bad == 0)
    desc = '页面 %d 个（内容齐全 %d），xref 错位 %d' % (pages, pages_with_content, bad)
    return ok, desc, pages


def main():
    args = sys.argv[1:]
    quiet = '--quiet' in args
    args = [a for a in args if not a.startswith('--')]
    files = []
    for a in args:
        if os.path.isdir(a):
            for dp, dns, fns in os.walk(a):
                for fn in sorted(fns):
                    if fn.lower().endswith('.pdf'):
                        files.append(os.path.join(dp, fn))
        elif a.lower().endswith('.pdf'):
            files.append(a)
    bad = 0
    for p in files:
        with open(p, 'rb') as f:
            data = f.read()
        ok, desc, pages = check(data)
        if not ok or not quiet:
            print('%-6s %-60s %s' % ('OK' if ok else 'BAD', os.path.basename(p)[-60:], desc))
        if not ok:
            bad += 1
    print('共 %d 个 PDF，有问题 %d 个' % (len(files), bad))
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
