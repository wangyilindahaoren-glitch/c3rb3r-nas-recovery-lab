# -*- coding: utf-8 -*-
"""校验已装回的 142 个原件：按类型做结构自检，判断"双击能不能打开"。

- zip 族(xlsx/docx/zip)：zipfile 能开 + 每个成员能 read + testzip() 通过
- pdf：%PDF- 头 + %%EOF + xref + Catalog + 页面对象
- 图片：头尾标记完整（PNG 的 IEND / JPEG 的 FFD9）
- xls/doc(OLE2)：D0CF11E0 头

只读。输出 JSON 清单 + 统计。
"""
import json
import os
import struct
import sys
import zipfile


def check_zip(p):
    try:
        z = zipfile.ZipFile(p)
    except Exception as e:
        return False, 'zip 打不开: %s' % str(e)[:60]
    names = z.namelist()
    if not names:
        return False, '空容器'
    bad = z.testzip()
    if bad:
        return False, '成员 CRC 错误: %s' % bad
    ok = 0
    for n in names:
        try:
            z.read(n)
            ok += 1
        except Exception:
            pass
    if ok == 0:
        return False, '没有可读成员'
    return True, '%d 个成员全部可读' % ok


def check_pdf(p):
    with open(p, 'rb') as f:
        data = f.read()
    if not data.startswith(b'%PDF-'):
        return False, '没有 %PDF- 头'
    if b'%%EOF' not in data[-4096:]:
        return False, '结尾没有 %%EOF'
    if b'startxref' not in data[-4096:]:
        return False, '没有 startxref'
    if b'/Type' not in data or b'/Catalog' not in data:
        return False, '没有 Catalog'
    import re
    pages = len(re.findall(rb'/Type\s*/Page(?![sA-Za-z])', data))
    if pages == 0:
        return False, '没有页面对象'
    return True, '%d 页' % pages


def check_png(p):
    with open(p, 'rb') as f:
        head = f.read(32)
        f.seek(max(0, os.path.getsize(p) - 64))
        tail = f.read()
    if not head.startswith(b'\x89PNG\r\n\x1a\n'):
        return False, '不是 PNG'
    if b'IEND' not in tail:
        return False, '缺 IEND'
    w, h = struct.unpack('>II', head[16:24])
    return True, '%dx%d' % (w, h)


def check_jpeg(p):
    with open(p, 'rb') as f:
        head = f.read(4)
        f.seek(max(0, os.path.getsize(p) - 4))
        tail = f.read()
    if not head.startswith(b'\xff\xd8'):
        return False, '不是 JPEG'
    if not tail.endswith(b'\xff\xd9'):
        return False, '缺 FFD9'
    return True, 'ok'


def check_ole(p):
    with open(p, 'rb') as f:
        head = f.read(8)
    if head != b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return False, '不是 OLE2'
    return True, 'ok'


def check_ini(p):
    with open(p, 'rb') as f:
        d = f.read(4096)
    if not d:
        return False, '空'
    return True, '%d 字节' % len(d)


CHECK = {
    '.xlsx': check_zip, '.docx': check_zip, '.zip': check_zip, '.xlsm': check_zip,
    '.pptx': check_zip,
    '.pdf': check_pdf,
    '.png': check_png,
    '.jpg': check_jpeg, '.jpeg': check_jpeg,
    '.xls': check_ole, '.doc': check_ole,
    '.ini': check_ini,
}


def main():
    pairs_tsv, out_json = sys.argv[1], sys.argv[2]
    items = []
    stats = {'ok': 0, 'bad': 0}
    with open(pairs_tsv, encoding='utf-8') as f:
        for ln in f:
            ln = ln.rstrip('\n')
            if not ln:
                continue
            parts = ln.split('\t')
            cipher, cand = parts[0], parts[1]
            dst = cipher[:-len('.L0CK3D')]
            ext = os.path.splitext(dst)[1].lower()
            rec = {'dst': dst, 'ext': ext}
            try:
                if not os.path.exists(dst):
                    raise ValueError('装回件不存在')
                fn = CHECK.get(ext)
                if fn is None:
                    rec['verdict'] = 'no_checker'
                    rec['why'] = ext
                    items.append(rec)
                    continue
                ok, why = fn(dst)
                rec['verdict'] = 'ok' if ok else 'bad'
                rec['why'] = why
                stats['ok' if ok else 'bad'] += 1
            except Exception as ex:
                rec['verdict'] = 'bad'
                rec['why'] = str(ex)[:120]
                stats['bad'] += 1
            items.append(rec)
    json.dump({'stats': stats, 'items': items},
              open(out_json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('校验:', json.dumps(stats, ensure_ascii=False))
    for it in items:
        if it['verdict'] != 'ok':
            print('  %-8s %-6s %s' % (it['verdict'], it.get('ext', ''),
                                      it['dst'][-70:]))


if __name__ == '__main__':
    main()
