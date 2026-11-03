# -*- coding: utf-8 -*-
"""读取每个 .L0CK3D 密文的尾部 588 字节标记，提取 orig_len / mode / session marker。
只读，不修改任何文件。"""
import os, sys, struct, json, collections

D = sys.argv[1] if len(sys.argv) > 1 else '/volume1/homes/<用户名>'
out = []
nerr = 0
for root, dirs, files in os.walk(D):
    for f in files:
        if not f.endswith('.L0CK3D'):
            continue
        p = os.path.join(root, f)
        try:
            sz = os.path.getsize(p)
            with open(p, 'rb') as fh:
                fh.seek(sz - 588)
                ft = fh.read(588)
        except Exception as e:
            out.append({'path': p, 'err': str(e)}); nerr += 1; continue
        if len(ft) < 588:
            out.append({'path': p, 'size': sz, 'err': 'short_footer'}); nerr += 1; continue
        rec = {
            'path': p,
            'size': sz,
            'orig_len': struct.unpack('<Q', ft[580:588])[0],
            'mode': ft[572],
            'pct': ft[573],
            'enc': ft[574],
            'marker': ft[568:572].hex(),
            'tail_ok': (sz >= 588),
        }
        out.append(rec)

with open('/tmp/c3r_zmm/footers.json', 'w') as fh:
    json.dump(out, fh, ensure_ascii=False)

print('files=%d err=%d' % (len(out), nerr))
print('mode 分布:', collections.Counter(r.get('mode') for r in out))
print('enc  分布:', collections.Counter(r.get('enc') for r in out))
print('marker 分布:', collections.Counter(r.get('marker') for r in out))
bad = [r for r in out if r.get('orig_len') is not None and r['size'] != r['orig_len'] + 588]
print('size != orig_len+588 的条数:', len(bad))
for r in bad[:10]:
    print('   ', r['size'], r.get('orig_len'), r['path'])
m = collections.Counter((r.get('mode'), r.get('pct')) for r in out)
print('mode/pct:', dict(m))
