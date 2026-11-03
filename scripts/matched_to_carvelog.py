#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""matched_to_carvelog.py —— 把 `carve_gen_hits.py match` 的离线配对结果，
转成 `carve_claim.py` 认识的候选日志格式，好让后面那条成熟流水线直接用。

为什么需要它
------------
`carve_claim.py` 的认领逻辑（按长度找目标、结构校验、落盘、逐份择优）已经很成熟，
但它只认**在线配对**的候选日志（`type \\t start \\t size \\t end_off`）。
`carve_gen_hits.py` 的离线集合配对结果格式是
`type \\t start \\t size \\t end_off \\t how`（多一列匹配方式），
本工具把它去掉最后一列、改写成候选日志即可复用整条流水线。

用法：
  python3 matched_to_carvelog.py <matched.tsv> <输出日志目录>
  python3 carve_claim.py <输出日志目录> <targets.tsv> <输出目录>
"""
import os
import sys


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    src, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    n = 0
    kinds = {}
    with open(src, encoding='utf-8') as f, \
            open(os.path.join(outdir, 'c0.log'), 'w', encoding='utf-8') as out:
        for ln in f:
            ln = ln.rstrip('\n')
            if not ln or ln.startswith('#'):
                continue
            parts = ln.split('\t')
            if len(parts) < 4:
                continue
            t, start, size, end = parts[0], parts[1], parts[2], parts[3]
            try:
                int(start)
                int(size)
                int(end)
            except ValueError:
                continue
            out.write('%s\t%s\t%s\t%s\n' % (t, start, size, end))
            kinds[t] = kinds.get(t, 0) + 1
            n += 1
    print('转换 %d 条候选 -> %s/c0.log' % (n, outdir))
    print('  类型分布: %s' % kinds)


if __name__ == '__main__':
    main()
