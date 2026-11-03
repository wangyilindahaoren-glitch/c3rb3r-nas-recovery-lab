#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAS 端：从部分损坏的 zip（xlsx/docx/zip）中抢救能读的成员。

对每个成员：
  - 用中央目录里的本地头偏移定位数据；
  - 本地头完整（PK\\x03\\x04）时按常规路径取；
  - 本地头被加密（落在密文区）时，用中央目录记录的文件名长度 + 若干种
    extra 长度组合去试数据起点；
  - 解压后用中央目录里的 CRC32 校验，对不上就丢弃这一成员。

用法：
  python salvage_zip.py <待抢救的根目录> <输出根目录>

源文件既可以是正常的 .xlsx/.zip，也可以是被加密改名的 .xlsx.L0CK3D
（后者推荐：中央目录与成员头都还在原始密文文件里，定位更准）。

输出结构：
  <输出根目录>/<原路径>/<成员名>      救回的成员
  <输出根目录>/_抢救清单.json          每个文件的成员数与救回数

只读源文件；已存在的目标改名加 .rN，绝不覆盖。
"""
import binascii
import json
import os
import struct
import sys
import zlib


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


def read_central(data):
    """从 zip 数据读中央目录，返回 {name: (lho, csize, usize, method, crc, nlen)}"""
    idx = data.rfind(b'PK\x05\x06')
    if idx < 0 or idx + 22 > len(data):
        return None
    cd_size, cd_off = struct.unpack('<II', data[idx + 12:idx + 20])
    n_ent = struct.unpack('<H', data[idx + 10:idx + 12])[0]
    if cd_off == 0xFFFFFFFF or n_ent == 0 or cd_off >= len(data):
        return None
    cd = data[cd_off:cd_off + cd_size]
    out = {}
    i = 0
    for _ in range(n_ent):
        if i + 46 > len(cd) or cd[i:i + 4] != b'PK\x01\x02':
            break
        method = struct.unpack('<H', cd[i + 10:i + 12])[0]
        crc = struct.unpack('<I', cd[i + 16:i + 20])[0]
        csize = struct.unpack('<I', cd[i + 20:i + 24])[0]
        usize = struct.unpack('<I', cd[i + 24:i + 28])[0]
        nlen = struct.unpack('<H', cd[i + 28:i + 30])[0]
        elen = struct.unpack('<H', cd[i + 30:i + 32])[0]
        clen = struct.unpack('<H', cd[i + 32:i + 34])[0]
        lho = struct.unpack('<I', cd[i + 42:i + 46])[0]
        name = cd[i + 46:i + 46 + nlen].decode('utf-8', 'replace')
        out[name] = (lho, csize, usize, method, crc, nlen)
        i += 46 + nlen + elen + clen
    return out


def try_member(data, name, lho, csize, usize, method, crc, nlen):
    """尝试取出并解压一个成员；成功返回 bytes，失败返回 None"""
    if lho + 30 > len(data):
        return None
    cands = []
    if data[lho:lho + 4] == b'PK\x03\x04':
        _, elen = struct.unpack('<HH', data[lho + 26:lho + 30])
        cands.append(lho + 30 + nlen + elen)
    else:
        # 本地头落在密文区，用中央目录的文件名长度 + 常见 extra 长度去定位
        for extra in (0, 8, 16, 24, 32, 40, 48, 56):
            cands.append(lho + 30 + nlen + extra)
    for st in cands:
        if st < 0 or st + csize > len(data):
            continue
        raw = data[st:st + csize]
        try:
            if method == 0:
                out = raw
            elif method == 8:
                out = zlib.decompressobj(-15).decompress(raw)
            else:
                continue
        except Exception:
            continue
        if crc and (binascii.crc32(out) & 0xFFFFFFFF) != crc:
            continue
        return out
    return None


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    src_root = sys.argv[1]
    out_root = sys.argv[2]

    # 源文件既可能是正常的 .xlsx/.zip，也可能是被加密改名的 .xlsx.L0CK3D
    ZIPS = ('.xlsx', '.docx', '.zip', '.xlsm', '.pptx')

    def is_zip_like(fn):
        low = fn.lower()
        if low.endswith(ZIPS):
            return True
        # 注意 low 已全小写，后缀比较必须用小写
        if low.endswith('.l0ck3d'):
            return low[:-len('.l0ck3d')].endswith(ZIPS)
        return False

    total_files = total_members = total_ok = 0
    report = []
    for dp, dns, fns in os.walk(src_root):
        for fn in sorted(fns):
            if not is_zip_like(fn):
                continue
            p = os.path.join(dp, fn)
            try:
                with open(p, 'rb') as f:
                    data = f.read()
            except Exception:
                continue
            cd = read_central(data)
            if not cd:
                report.append({'file': p, 'status': 'no_central_directory'})
                continue
            rel = os.path.relpath(p, src_root)
            got = []
            for name, (lho, csize, usize, method, crc, nlen) in cd.items():
                if name.endswith('/'):
                    continue
                out = try_member(data, name, lho, csize, usize, method, crc, nlen)
                if out is None:
                    continue
                dst = os.path.join(out_root, rel, name.replace('/', os.sep))
                try:
                    final = write_safe(dst, out)
                except Exception:
                    continue
                got.append({'name': name, 'size': len(out)})
                total_ok += 1
            total_files += 1
            total_members += len(cd)
            report.append({'file': p, 'members': len(cd), 'recovered': len(got),
                           'recovered_bytes': sum(g['size'] for g in got), 'items': got})
            sys.stderr.write('%s: 成员 %d 救回 %d\n' % (fn[:50], len(cd), len(got)))

    os.makedirs(out_root, exist_ok=True)
    with open(os.path.join(out_root, '_抢救清单.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print('抢救完成: 文件 %d, 成员 %d, 救回 %d' % (total_files, total_members, total_ok))


if __name__ == '__main__':
    main()
