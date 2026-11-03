#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAS 端：定位 Btrfs / ext4 卷在块设备上的字节范围，并生成可直接使用的雕刻配置与命令。

为什么需要它：
  Synology 的 /dev/mapper/cachedev_0 上有多个分区（系统、交换、数据卷），
  数据卷（volume1）不在 0 偏移。foremost / PhotoRec 都只认"文件从头开始"的
  裸设备流，直接对着整个 md 阵列扫会把系统分区也扫一遍，还容易把
  "分区表里的分区起点" 当成文件头。要做减法，先把数据卷的起始偏移算准。

本脚本做三件事：
  1. 读块设备的分区表（MBR / GPT 都支持）；
  2. 逐个分区读文件系统超块，用魔数确认哪个分区才是数据卷 —— 自验证，不靠猜；
     - Btrfs：魔数 `_BHRfS_M`（0x4D5F536E），在分区起点 + 0x40000 处的超块里；
     - ext4 ：魔数 `0xEF53`，在分区起点 + 0x438（超块在 +1024，magic 在 +0x38）；
  3. 生成 foremost.conf（按目标类型过滤），并打印可直接粘贴执行的命令。

只读：只用 O_RDONLY 打开设备，只做 pread，不写设备、不挂载、不改任何东西。

用法：
  python carve_scan.py                              # 自动从 /proc/mounts 找 btrfs 卷
  python carve_scan.py --dev /dev/mapper/cachedev_0 --mount /volume1
  python carve_scan.py --dev /dev/mapper/cachedev_0 --types jpg,png,docx,xlsx,pdf
  python carve_scan.py --dev /dev/mapper/cachedev_0 --conf-out /tmp/carve.conf

  # ext4 卷（QNAP 老卷、群晖老卷、手工装的 Linux 常见）：
  python carve_scan.py --fs ext4 --dev /dev/sda1
  python carve_scan.py --fs ext4 --dev /dev/sda            # 整盘 + 分区表，自动挑 ext4 分区
  python carve_scan.py --fs ext4 --mount /mnt/ext4         # 按挂载点反查设备
"""
import errno
import json
import os
import struct
import sys

# --- 平台守卫：要在 NAS（Linux）上跑 ---
# 它要读块设备（/dev/mapper/cachedev_0 之类）、读分区表、用 Btrfs 超块魔数自验证数据卷起点。
# Windows 上没有块设备节点，做不到。
if os.name != 'posix':
    sys.stderr.write(
        '\n[carve_scan] 这个脚本要在 **NAS（Linux）上以 root 运行**，不能在 Windows/macOS 上跑。\n'
        '[carve_scan] 它要直接读块设备，Windows 上没有块设备节点。\n'
        '[carve_scan] 正确用法：SSH 到 NAS，sudo python3 scripts/carve_scan.py <块设备>\n'
        '[carve_scan] （详见 references/carving.md）\n\n')
    sys.exit(3)

SECTOR = 512
BTRFS_MAGIC = b'\x6e\x53\x5f\x4d'      # BTRFS_FS_MAGIC 0x4D5F536E，小端
BTRFS_SB_OFFSETS = (0x40000, 0x10000, 0x0)   # 主超块 / 可能的镜像 / 文件头
SB_MAGIC_OFF = 0x40
# ext4：超级块在分区起点后 1024 字节，magic（0xEF53）在超块内 +0x38
#        → 绝对位置 = 分区起点 + 0x438
EXT4_MAGIC = b'\x53\xef'               # EXT4_SUPER_MAGIC 0xEF53，小端
EXT4_SB_OFFSET = 1024                  # 超块相对分区起点的偏移
EXT4_MAGIC_OFF = 0x38                  # s_magic 在超块内的偏移
BLKGETSIZE64 = 0x80081272
LIBC = 'libc.so.6'


# ---------- 设备与分区表 ----------

def device_size(path):
    """块设备字节数；拿不到就用分区表推算"""
    st = os.stat(path)
    if st.st_size:
        return st.st_size
    try:
        import fcntl
        import array
        buf = array.array('q', [0])
        fcntl.ioctl(st.st_fd, BLKGETSIZE64, buf, True)
        return buf[0]
    except Exception:
        pass
    # 退而求其次：读 /sys/block
    base = os.path.basename(path)
    for root, dirs, files in os.walk('/sys/block'):
        if base in dirs or base in files:
            p = os.path.join(root, base, 'size')
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        return int(f.read().strip()) * 512
                except (OSError, ValueError):
                    pass
    return None


def read_at(f, off, n):
    f.seek(off)
    return f.read(n)


def parse_mbr(buf):
    parts = []
    if len(buf) < 512 or buf[510:512] != b'\x55\xaa':
        return parts
    for i in range(4):
        e = buf[446 + i * 16: 462 + i * 16]
        if len(e) < 16:
            break
        ptype = e[4]
        if ptype == 0:
            continue
        start_lba = struct.unpack('<I', e[8:12])[0]
        nsec = struct.unpack('<I', e[12:16])[0]
        parts.append({'index': i + 1, 'type': '0x%02x' % ptype,
                      'start': start_lba * SECTOR, 'size': nsec * SECTOR})
    return parts


def parse_gpt(f, devsz):
    hdr = read_at(f, SECTOR, 92)
    if len(hdr) < 92 or hdr[0:8] != b'EFI PART':
        return []
    entry_lba = struct.unpack('<Q', hdr[72:80])[0]
    num = struct.unpack('<I', hdr[80:84])[0]
    esz = struct.unpack('<I', hdr[84:88])[0]
    if esz < 128 or num > 4096:
        return []
    raw = read_at(f, entry_lba * SECTOR, num * esz)
    parts = []
    for i in range(num):
        e = raw[i * esz:(i + 1) * esz]
        if len(e) < 40:
            break
        first = struct.unpack('<Q', e[32:40])[0]
        last = struct.unpack('<Q', e[40:48])[0]
        if first == 0:
            continue
        parts.append({'index': i + 1, 'type': 'gpt',
                      'start': first * SECTOR, 'size': (last - first + 1) * SECTOR})
    return parts


def find_mount_source(mountpoint=None, fsname='btrfs'):
    """从 /proc/mounts 找指定文件系统的挂载点及其源设备"""
    out = []
    try:
        with open('/proc/mounts', 'r', errors='replace') as f:
            for line in f:
                p = line.split()
                if len(p) < 3:
                    continue
                if p[2] == fsname:
                    out.append((p[0], p[1]))
    except OSError:
        pass
    if mountpoint:
        for src, mp in out:
            if mp == mountpoint:
                return src, mp
        return None, None
    return out[0] if out else (None, None)


# ---------- 识别文件系统分区 ----------

def btrfs_at(f, off):
    """在 off 处读一段，判断是否是 Btrfs 超块"""
    for sb_off in BTRFS_SB_OFFSETS:
        try:
            blob = read_at(f, off + sb_off + SB_MAGIC_OFF, 4)
        except OSError:
            return None
        if blob == BTRFS_MAGIC:
            return sb_off
    return None


def ext4_at(f, off):
    """在 off（分区起点）判断是不是 ext4 超级块；是则返回超块偏移 1024

    ext4 超级块在分区起点后 1024 字节处，s_magic(0xEF53) 在超块内 +0x38，
    所以绝对位置 = 分区起点 + 0x438。
    """
    try:
        blob = read_at(f, off + EXT4_SB_OFFSET + EXT4_MAGIC_OFF, 2)
    except OSError:
        return None
    if blob == EXT4_MAGIC:
        return EXT4_SB_OFFSET
    return None


def inspect(dev, mountpoint=None):
    out = {'device': dev, 'parts': [], 'btrfs_parts': [], 'ext4_parts': [], 'mounts': []}
    try:
        sz = device_size(dev)
        out['device_size'] = sz
    except OSError as e:
        out['device_size'] = None
        out['error'] = str(e)
    with open(dev, 'rb') as f:
        mbr = read_at(f, 0, SECTOR)
        gpt = parse_gpt(f, out.get('device_size'))
        parts = parse_mbr(mbr) or []
        if gpt:
            parts = gpt
        out['table'] = 'gpt' if gpt else ('mbr' if parts else 'unknown')
        out['parts'] = parts
        for p in parts:
            sb = btrfs_at(f, p['start'])
            if sb is not None:
                p['btrfs_sb_offset'] = '0x%x' % sb
                out['btrfs_parts'].append(p)
                continue
            sb = ext4_at(f, p['start'])
            if sb is not None:
                p['ext4_sb_offset'] = '0x%x' % sb
                out['ext4_parts'].append(p)
        # 整盘就是一个文件系统（无分区表：LVM LV、直接 mkfs 的设备）
        if not out['btrfs_parts'] and not out['ext4_parts']:
            if btrfs_at(f, 0) is not None:
                out['btrfs_parts'].append({'index': 0, 'start': 0, 'size': sz or 0,
                                           'btrfs_sb_offset': '0x0'})
            elif ext4_at(f, 0) is not None:
                out['ext4_parts'].append({'index': 0, 'start': 0, 'size': sz or 0,
                                          'ext4_sb_offset': '0x400'})
    if mountpoint:
        src, mp = find_mount_source(mountpoint)
        out['mounts'] = [{'source': src, 'mountpoint': mp}] if src else []
    return out


# ---------- foremost 配置 ----------

SIGS = [
    # ext,      cs, max_size,     header,                              footer
    ('jpg',      'y', '20M', r'\xff\xd8\xff',            r'\xff\xd9'),
    ('jpeg',     'y', '20M', r'\xff\xd8\xff',            r'\xff\xd9'),
    ('png',      'y', '20M', r'\x89PNG\x0d\x0a\x1a\x0a', r'\x49\x45\x4e\x44\xae\x42\x60\x82'),
    ('gif',      'y', '10M', r'GIF8[79]a',               r'\x00\x3b'),
    ('bmp',      'y', '30M', r'BM',                      ''),
    ('heic',     'y', '100M', r'\x00\x00\x00\x18ftypheic', r''),
    ('pdf',      'y', '500M', r'%PDF-',                  r'%%EOF'),
    ('zip',      'y', '500M', r'PK\x03\x04',             r'PK\x05\x06'),
    ('docx',     'y', '200M', r'PK\x03\x04',             r'PK\x05\x06'),
    ('xlsx',     'y', '200M', r'PK\x03\x04',             r'PK\x05\x06'),
    ('pptx',     'y', '200M', r'PK\x03\x04',             r'PK\x05\x06'),
    ('7z',       'y', '500M', r'7z\xbc\xaf\x27\x1c',     r''),
    ('rar',      'y', '500M', r'Rar!\x1a\x07\x00',       r''),
    ('gz',       'y', '500M', r'\x1f\x8b\x08',           r''),
    ('sqlite',   'y', '200M', r'SQLite format 3\x00',    r''),
    ('mdb',      'y', '200M', r'\x00\x01\x00\x00Standard Jet DB', r''),
    ('eml',      'n', '50M', r'From:',                  r''),
    ('mp4',      'y', '2G',  r'\x00\x00\x00\x18ftypmp4', r''),
    ('mov',      'y', '2G',  r'\x00\x00\x00\x14ftypqt', r''),
]


def write_conf(path, types=None):
    want = None
    if types:
        want = set(t.strip().lower() for t in types.split(',') if t.strip())
    lines = [
        '# C3RB3R 恢复用 foremost 配置（由 carve_scan.py 生成）',
        '# 格式: extension case_sensitive max_size header footer',
        '# 空 footer 表示不校验文件尾（这类文件更容易被完整切出）',
        '',
    ]
    used = []
    for ext, cs, mx, hdr, ftr in SIGS:
        if want and ext not in want:
            continue
        lines.append('%-10s %-4s %-10s %-28s %s' % (ext, cs, mx, hdr, ftr))
        used.append(ext)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return used


# ---------- 主流程 ----------

def human(n):
    if n is None:
        return '未知'
    for u in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or u == 'TiB':
            return '%.1f %s' % (n, u)
        n /= 1024.0


def main():
    args = sys.argv[1:]
    dev = None
    mountpoint = None
    types = None
    conf_out = None
    json_out = None
    fsname = 'auto'
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--dev':
            dev = args[i + 1]; i += 2
        elif a == '--mount':
            mountpoint = args[i + 1]; i += 2
        elif a == '--types':
            types = args[i + 1]; i += 2
        elif a == '--conf-out':
            conf_out = args[i + 1]; i += 2
        elif a == '--json-out':
            json_out = args[i + 1]; i += 2
        elif a == '--fs':
            fsname = args[i + 1].lower(); i += 2
        else:
            i += 1

    if fsname not in ('auto', 'btrfs', 'ext4'):
        sys.exit('--fs 只能是 auto / btrfs / ext4')

    if not dev:
        want = ['btrfs', 'ext4'] if fsname == 'auto' else [fsname]
        for cand in want:
            src, mp = find_mount_source(mountpoint, cand)
            if src:
                dev, mountpoint, fsname = src, mp, cand
                print('自动识别: 设备 %s 挂载于 %s（%s）' % (dev, mp, cand))
                break
        if not dev:
            sys.exit('未能自动找到 %s 挂载的设备，请用 --dev 指定' % '/'.join(want))

    info = inspect(dev, mountpoint)
    print('\n=== 设备 ===')
    print('  %s  大小 %s' % (info['device'], human(info.get('device_size'))))
    print('  分区表: %s' % info.get('table'))

    print('\n=== 分区 ===')
    for p in info['parts']:
        if p in info['btrfs_parts']:
            mark = '  <== Btrfs 卷'
        elif p in info['ext4_parts']:
            mark = '  <== ext4 卷'
        else:
            mark = ''
        print('  #%-2d 起点 %13d (%10s)  大小 %s%s'
              % (p['index'], p['start'], human(p['start']), human(p['size']), mark))
    if not info['parts']:
        print('  (没有解析出分区表；整盘扫描请注意系统分区会被一起扫)')

    if fsname == 'auto':
        fsname = 'btrfs' if info['btrfs_parts'] else ('ext4' if info['ext4_parts'] else 'btrfs')
    target = info['btrfs_parts'] if fsname == 'btrfs' else info['ext4_parts']

    print('\n=== 结论 ===')
    if not target:
        print('  没有在分区表里找到 %s 超块。' % fsname.upper())
        print('  可能原因: 整个设备就是这个文件系统（无分区表），或分区表解析失败。')
        print('  此时只能整盘扫描，范围 0 .. %s' % human(info.get('device_size')))
    elif len(target) == 1:
        p = target[0]
        print('  %s 卷 = 分区 #%d，起点偏移 %d 字节 (%s)，可用长度 %s'
              % (fsname.upper(), p['index'], p['start'], human(p['start']), human(p['size'])))
        if fsname == 'ext4':
            print('  ext4 的真实文件系统长度用 ext4_probe.py 读超块确认；分区大小只是上限。')
    else:
        print('  发现 %d 个 %s 分区，请人工确认目标：' % (len(target), fsname.upper()))
        for p in target:
            print('    #%d 起点 %d 大小 %s' % (p['index'], p['start'], human(p['size'])))

    if types:
        conf = conf_out or '/tmp/carve.conf'
        used = write_conf(conf, types)
        print('\n=== foremost 配置 ===')
        print('  已写入 %s  （类型: %s）' % (conf, ','.join(used)))

    print('\n=== 可直接执行的命令（OFFSET / SIZE 取上面算出的值） ===')
    off = target[0]['start'] if len(target) == 1 else 0
    size = target[0]['size'] if len(target) == 1 else (info.get('device_size') or 0)
    # DSM 的 dd 是 busybox，不支持 iflag=skip_bytes，只能按块取整（多扫一点无妨）
    skip_mb = off // (1 << 20)
    count_mb = (size // (1 << 20)) if size else 0
    cnt = (' count=%d' % count_mb) if count_mb else ''
    dev_for_photorec = dev[5:] if dev.startswith('/dev/') else dev
    print("""# 只扫数据卷：按 1M 块从卷起点读，管道给 foremost（间接模式）
dd if=%s bs=1M skip=%d%s | foremost -c %s -o /tmp/carve_out

# 若你的 dd 支持 GNU 扩展，可以按字节精确起点（DSM busybox 不支持，别用）：
#   dd if=%s bs=4M iflag=skip_bytes skip=%d | foremost -c %s -o /tmp/carve_out

# PhotoRec（交互式，选择目标分区）：
photorec /d/%s

# 注意：
#  - 输出目录必须在 NAS 之外（本地盘或另一台机器），别写回数据卷；
#  - 雕刻的是"盘上剩余的字节"，被复用过的旧 extent 会扫出垃圾；
#  - 扫出来的文件没有路径，用 carve_match.py 对应回 .L0CK3D 原件。""" %
          (dev, skip_mb, cnt, conf_out or '/tmp/carve.conf',
           dev, off, conf_out or '/tmp/carve.conf', dev_for_photorec))

    if fsname == 'ext4':
        print("""
=== ext4 专用命令（优先于上面的 foremost） ===

# ① ext4 没有 DATA chunk 划分，元数据和数据混在同一分区：
#    用自带扫描器按字节范围扫，命中偏移离线配对，比 foremost 准得多。
#    --range 的偏移是相对 --dev 的字节偏移。
python3 scripts/carve_gen_hits.py scan /tmp/carve_out --targets /tmp/targets.tsv \\
    --dev %s --range %d:%d --workers 2 --sigs pdf,pdf_end,png,jpeg,eocd

# ② ext4 默认开 jbd2 日志，日志里常留着被覆盖前的旧内容（**越早越灵**）：
python3 scripts/ext4_probe.py --dev %s --dump-journal /tmp/jrn.bin --json-out /tmp/ext4.json
python3 scripts/jrn_match.py /tmp/jrn.bin --cipher-dir <密文目录> --out /tmp/jrn_out

# ③ 认领 + 装回（与 Btrfs 同一套流水线）：
python3 scripts/carve_claim.py <扫描日志目录> /tmp/targets.tsv /tmp/carve_out
python3 scripts/carve_install.py plan --home <家目录> --targets /tmp/targets.tsv \\
    --index /tmp/empty_index.tsv --claim /tmp/carve_out/_认领结果.json --out-plan /tmp/plan.tsv

# 红线：不要跑 e2fsck（不带 -n 的会清掉释放的块，把还没捞的数据彻底销毁）。
""" % (dev, off, off + size, dev))

    if json_out:
        with open(json_out, 'w', encoding='utf-8') as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        print('\n机器可读结果: %s' % json_out)


if __name__ == '__main__':
    main()
