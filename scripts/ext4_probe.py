#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ext4_probe.py —— ext4 卷只读探测（C3RB3R 勒索恢复专用）。

ext4 没有写时复制，路线 A（Btrfs 旧 extent）用不了；但 ext4 上有三样
Btrfs 没有的东西，本脚本负责把它们一次查清楚：

  1. **jbd2 日志**：ext4 默认开日志。data=ordered 模式下，文件的数据块在
     元数据事务提交前会**先写进日志**，日志里留着这些块的**旧内容** ——
     勒索程序原地改写文件时，旧内容很可能就躺在日志里。
     这是 ext4 上最值钱的一条路（详见 references/ext4.md）。
  2. **已删除 inode**：被删除但位图还没被复用的 inode，内容还在盘上，
     用 debugfs 能直接 dump 出来（目录项没了，文件还在）。
  3. **物理块范围**：ext4 的元数据和数据混在同一分区、没有 chunk 划分，
     雕刻必须扫整段。这里把分区起点/大小、块大小一次读全，供扫描器
     用 `--range` 指定。

只读：只用 O_RDONLY 打开设备、只做 pread；debugfs 不带 `-w` 也是只读。
不挂载、不 fsck、不写设备、不动任何文件。

用法：
  # 探测（只看元数据，不写任何东西）
  python3 ext4_probe.py --dev /dev/sda1

  # 整盘上的分区：--dev 给整盘，--offset 给文件系统在盘内的起点
  python3 ext4_probe.py --dev /dev/sda --offset 1048576

  # 把 jbd2 日志导出成镜像（后续可以当普通文件去雕刻）
  python3 ext4_probe.py --dev /dev/sda1 --dump-journal /tmp/jrn.bin

  # 机器可读结果 + 已删 inode 清单
  python3 ext4_probe.py --dev /dev/sda1 --json-out /tmp/ext4.json \
          --lsdel-out /tmp/lsdel.tsv
"""
import json
import os
import re
import struct
import subprocess
import sys

# --- 平台守卫：读块设备要在 Linux 上跑 ---
if os.name != 'posix':
    sys.stderr.write(
        '\n[ext4_probe] 这个脚本要在 **NAS / Linux 上以 root 运行**。\n'
        '[ext4_probe] 它要直接读块设备（/dev/sda1、/dev/mapper/vg1-volume_1 之类）。\n'
        '[ext4_probe] 详见 references/ext4.md\n\n')
    sys.exit(3)

EXT4_MAGIC = 0xEF53          # s_magic
SB_OFFSET = 1024             # 超块在分区起点之后的位置
SB_SIZE = 1024               # 超块长度

# 特性位（只列用得上的）
COMPAT_HAS_JOURNAL = 0x0004
INCOMPAT_FILETYPE = 0x0002
INCOMPAT_RECOVER = 0x0004
INCOMPAT_META_BG = 0x0010
INCOMPAT_EXTENTS = 0x0040
INCOMPAT_64BIT = 0x0080
INCOMPAT_FLEX_BG = 0x0200
RO_COMPAT_SPARSE_SUPER = 0x0001
RO_COMPAT_HUGE_FILE = 0x0008
RO_COMPAT_METADATA_CSUM = 0x0400
RO_COMPAT_BIGALLOC = 0x0200

BLKGETSIZE64 = 0x80081272


def die(msg, code=2):
    sys.stderr.write('[ext4_probe] %s\n' % msg)
    sys.exit(code)


def run(cmd):
    """跑一条命令，返回 (输出文本, 返回码)。命令不存在时返回码 127。"""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.stdout.decode('utf-8', 'replace'), p.returncode
    except OSError:
        return '', 127


def device_size(fd):
    st = os.fstat(fd)
    if st.st_size:
        return st.st_size
    try:
        import fcntl
        import array
        buf = array.array('q', [0])
        fcntl.ioctl(fd, BLKGETSIZE64, buf, True)
        return buf[0]
    except Exception:
        return None


def parse_super(sb, base):
    """解析 ext4 超块。魔数不对返回 None。"""
    if len(sb) < SB_SIZE:
        return None
    magic = struct.unpack('<H', sb[0x38:0x3A])[0]
    if magic != EXT4_MAGIC:
        return None
    d = {}
    d['partition_offset'] = base
    d['inodes_count'] = struct.unpack('<I', sb[0x00:0x04])[0]
    d['first_data_block'] = struct.unpack('<I', sb[0x14:0x18])[0]
    d['log_block_size'] = struct.unpack('<I', sb[0x18:0x1C])[0]
    d['block_size'] = 1024 << d['log_block_size']
    d['blocks_per_group'] = struct.unpack('<I', sb[0x20:0x24])[0]
    d['inodes_per_group'] = struct.unpack('<I', sb[0x28:0x2C])[0]
    d['state'] = struct.unpack('<H', sb[0x3A:0x3C])[0]
    d['volume_name'] = sb[0x78:0x88].split(b'\x00')[0].decode('utf-8', 'replace')
    d['last_mounted'] = sb[0x88:0xC8].split(b'\x00')[0].decode('utf-8', 'replace')
    d['uuid'] = sb[0x68:0x78].hex()

    compat = struct.unpack('<I', sb[0x5C:0x60])[0]
    incompat = struct.unpack('<I', sb[0x60:0x64])[0]
    ro_compat = struct.unpack('<I', sb[0x64:0x68])[0]
    d['feature_compat'] = hex(compat)
    d['feature_incompat'] = hex(incompat)
    d['feature_ro_compat'] = hex(ro_compat)
    d['has_journal'] = bool(compat & COMPAT_HAS_JOURNAL)
    d['has_extents'] = bool(incompat & INCOMPAT_EXTENTS)
    d['is_64bit'] = bool(incompat & INCOMPAT_64BIT)
    d['has_huge_file'] = bool(ro_compat & RO_COMPAT_HUGE_FILE)
    d['has_meta_csum'] = bool(ro_compat & RO_COMPAT_METADATA_CSUM)
    d['has_bigalloc'] = bool(ro_compat & RO_COMPAT_BIGALLOC)

    blocks_lo = struct.unpack('<I', sb[0x04:0x08])[0]
    if d['is_64bit']:
        blocks_hi = struct.unpack('<I', sb[0x150:0x154])[0]
        d['blocks_count'] = blocks_lo | (blocks_hi << 32)
        d['inode_size'] = struct.unpack('<H', sb[0x58:0x5A])[0] or 128
    else:
        d['blocks_count'] = blocks_lo
        d['inode_size'] = 128
    d['fs_size'] = d['blocks_count'] * d['block_size']

    # s_journal_inum 在 0xE2；外部日志时 s_journal_dev != 0
    d['journal_inode'] = struct.unpack('<I', sb[0xE2:0xE6])[0]
    d['journal_dev'] = struct.unpack('<I', sb[0xE6:0xEA])[0]
    d['external_journal'] = d['journal_dev'] != 0
    d['mount_opts'] = struct.unpack('<I', sb[0x102:0x106])[0]
    return d


def debugfs_stat_extents(dev, inum):
    """用 debugfs 拿某个 inode 的物理块列表（EXTENTS/BLOCKS）。

    只读；debugfs 不存在或命令失败时返回 ([], 原始输出)。
    """
    out, rc = run(['debugfs', '-R', 'stat %d' % inum, dev])
    if rc != 0 and not out:
        return [], out
    blocks = []
    seen = set()
    for m in re.finditer(r'blk (\d+)', out):
        b = int(m.group(1))
        if b not in seen:
            seen.add(b)
            blocks.append(b)
    return blocks, out


def debugfs_lsdel(dev):
    """列出已删除的 inode（位图还没被复用的话，内容还在盘上）。

    debugfs lsdel 输出形如：
        Inode  Unix Mode  Deleted  Size  Blocks  Links  dtime
           12  00006000      1      1024      1      1  2026-01-02 03:04:05
    """
    out, rc = run(['debugfs', '-R', 'lsdel', dev])
    rows = []
    for ln in out.splitlines():
        m = re.match(r'\s*(\d+)\s+([0-9a-fA-F]{8})\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(.+?)\s*$', ln)
        if not m:
            continue
        rows.append({
            'inode': int(m.group(1)),
            'mode': '0x' + m.group(2),
            'size': int(m.group(4)),
            'blocks': int(m.group(5)),
            'dtime': m.group(7),
        })
    return rows, out


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
    offset = 0
    dump_jrn = None
    json_out = None
    lsdel_out = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--dev':
            dev = args[i + 1]; i += 2
        elif a == '--offset':
            offset = int(args[i + 1], 0); i += 2
        elif a == '--dump-journal':
            dump_jrn = args[i + 1]; i += 2
        elif a == '--json-out':
            json_out = args[i + 1]; i += 2
        elif a == '--lsdel-out':
            lsdel_out = args[i + 1]; i += 2
        else:
            i += 1

    if not dev:
        die('缺少 --dev（要指向**文件系统所在**的设备，如 /dev/sda1、/dev/mapper/vg1-volume_1）')

    try:
        fd = os.open(dev, os.O_RDONLY)
    except OSError as e:
        die('打不开设备 %s：%s（要 root）' % (dev, e))

    try:
        devsz = device_size(fd)
        sb = os.pread(fd, SB_SIZE, offset + SB_OFFSET)
        sup = parse_super(sb, offset)
        if sup is None:
            die('%s 偏移 %d 处不是 ext4 超块（魔数不对）。'
                'ext4 魔数 0xEF53 在分区起点 + 0x438。'
                '整盘 + 分区表请先用 carve_scan.py --fs ext4 定位。'
                % (dev, offset))

        print('\n=== ext4 卷 ===')
        print('  设备            %s（文件系统在盘内偏移 %d）' % (dev, offset))
        print('  卷名            %s' % (sup['volume_name'] or '(无名)'))
        print('  上次挂载        %s' % (sup['last_mounted'] or '(无)'))
        print('  块大小          %d 字节' % sup['block_size'])
        print('  总块数          %d  → 文件系统 %s' % (sup['blocks_count'], human(sup['fs_size'])))
        print('  inode 数        %d（每组 %d，大小 %d）'
              % (sup['inodes_count'], sup['inodes_per_group'], sup['inode_size']))
        print('  特性位          compat=%s incompat=%s ro_compat=%s'
              % (sup['feature_compat'], sup['feature_incompat'], sup['feature_ro_compat']))
        print('  extent 特性     %s   64bit %s   huge_file %s   metadata_csum %s'
              % ('有' if sup['has_extents'] else '无',
                 '有' if sup['is_64bit'] else '无',
                 '有' if sup['has_huge_file'] else '无',
                 '有' if sup['has_meta_csum'] else '无'))
        print('  jbd2 日志       %s' % (
            '有（inode %d）' % sup['journal_inode'] if sup['has_journal'] else '无'))

        result = {'device': dev, 'superblock': sup}
        fs_end = offset + sup['fs_size']
        if devsz:
            fs_end = min(fs_end, devsz)

        # ---- jbd2 日志 ----
        jrn_blocks = []
        if sup['has_journal'] and not sup['external_journal']:
            jrn_blocks, raw = debugfs_stat_extents(dev, sup['journal_inode'])
            if jrn_blocks:
                print('  日志物理块      %d 个（%.1f MB）'
                      % (len(jrn_blocks), len(jrn_blocks) * sup['block_size'] / 1048576.0))
            else:
                print('  日志物理块      拿不到（debugfs 不可用或 inode 读不出来）')
                print('                  手工办法: debugfs -R "stat %d" %s' % (sup['journal_inode'], dev))
            result['journal_blocks'] = jrn_blocks

            if dump_jrn:
                if not jrn_blocks:
                    die('--dump-journal 需要 debugfs 读出日志块；先装 e2fsprogs 或手工 stat')
                total = 0
                with open(dump_jrn, 'wb') as f:
                    for b in jrn_blocks:
                        chunk = os.pread(fd, sup['block_size'],
                                         offset + b * sup['block_size'])
                        if not chunk:
                            break
                        f.write(chunk)
                        total += len(chunk)
                print('\n  日志镜像已导出  %s（%s）' % (dump_jrn, human(total)))
                result['journal_image'] = dump_jrn
                result['journal_image_size'] = total

        # ---- 已删除 inode ----
        rows, raw = debugfs_lsdel(dev)
        result['deleted_inodes'] = rows
        print('\n=== 已删除 inode（debugfs lsdel）===')
        if not rows:
            print('  没有列出来（debugfs 不可用，或没有可回收的已删 inode）')
        else:
            print('  %-10s %-12s %-12s %s' % ('inode', 'size', 'blocks', '删除时间'))
            for r in rows[:40]:
                print('  %-10d %-12d %-12d %s' % (r['inode'], r['size'], r['blocks'], r['dtime']))
            if len(rows) > 40:
                print('  ...（共 %d 条，只列前 40 条）' % len(rows))
            print('\n  取回其中一个（只新增、不覆盖）：')
            print('    debugfs -R "dump %d /tmp/out.bin" %s' % (rows[0]['inode'], dev))

        if lsdel_out:
            with open(lsdel_out, 'w', encoding='utf-8') as f:
                f.write('inode\tsize\tblocks\tdtime\tmode\n')
                for r in rows:
                    f.write('%d\t%d\t%d\t%s\t%s\n'
                            % (r['inode'], r['size'], r['blocks'], r['dtime'], r['mode']))
            print('\n  已删 inode 清单  %s' % lsdel_out)

        # ---- 给扫描器的范围 ----
        print('\n=== 雕刻扫描范围（ext4 没有 DATA chunk，整段扫）===')
        print('  范围: %d .. %d（%s）' % (offset, fs_end, human(fs_end - offset)))
        print('\n  # 扫描整个 ext4 卷（2~4 路并发，别把 NAS 拖死）')
        print('  python3 carve_gen_hits.py scan /tmp/carve_out --targets /tmp/targets.tsv \\')
        print('      --dev %s --range %d:%d --workers 2 --sigs pdf,pdf_end,png,jpeg,eocd'
              % (dev, offset, fs_end))
        print('\n  # 如果日志镜像导出来了，它就是个普通文件，直接当设备扫：')
        if dump_jrn:
            print('  python3 carve_gen_hits.py scan /tmp/carve_jrn --targets /tmp/targets.tsv \\')
            print('      --dev %s --range 0:%d --workers 1 --sigs pdf,pdf_end,png,jpeg,eocd'
                  % (dump_jrn, result.get('journal_image_size', 0)))
        else:
            print('  #   先加 --dump-journal /tmp/jrn.bin 导出日志镜像')
        print('\n  # 认领 + 装回（与 Btrfs 完全同一套流水线）：')
        print('  python3 carve_claim.py <扫描日志目录> /tmp/targets.tsv /tmp/carve_out')
        print('  python3 carve_install.py plan --home <家目录> --targets /tmp/targets.tsv \\')
        print('      --index /tmp/empty_index.tsv --claim /tmp/carve_out/_认领结果.json \\')
        print('      --out-plan /tmp/plan.tsv')
        print('  python3 carve_install.py install --plan /tmp/plan.tsv.json --log /tmp/install.log')
        print('  python3 validate_installed.py <pairs.tsv> /tmp/val.json')

        print('\n=== 红线 ===')
        print('  - 不要跑 e2fsck（不带 -n 的会写盘，会把旧块清掉）')
        print('  - 不要 resize2fs、不要 defrag、不要 tune2fs 改特性')
        print('  - 不要以读写方式重新挂载')
        print('  - 不要删任何 .L0CK3D（删密文 = 删掉还没救回的明文区）')

        if json_out:
            with open(json_out, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print('\n机器可读结果: %s' % json_out)
    finally:
        os.close(fd)


if __name__ == '__main__':
    main()
