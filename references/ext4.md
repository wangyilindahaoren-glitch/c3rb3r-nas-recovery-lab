# ext4 上的 C3RB3R 恢复路线（没有写时复制也能救）

> 适用对象：卷是 **ext4** 的机器 —— 威联通 QNAP 老卷（多数是 ext4 + LVM）、
> 群晖老卷、手工装的 Linux 桌面/服务器、U 盘与移动硬盘做成的 ext4。
> 先确认文件系统：`findmnt -no FSTYPE,OPTIONS <挂载点>` 或 `df -T`。
>
> **本文是 SKILL.md 的旁支**：主线（路线 A）是 Btrfs 专用，
> ext4 用户请从本文的第 2 节开始按顺序做，不要去跑 `preflight`/`gens`/`survey`。

---

## 0. 结论先说

ext4 **没有写时复制**，所以本 skill 的主线（路线 A：从 Btrfs 旧 extent 逐字节还原）
在 ext4 上**用不了**。这不是工具的问题，是文件系统原理决定的：
C3RB3R 是**原地改写**文件，ext4 直接把同一批物理块覆盖掉，旧内容当场消失。

但 ext4 有自己的一套路，合计能救回相当一部分：

| 路线 | 手段 | ext4 上可用? | 拿回什么 |
|---|---|---|---|
| A | Btrfs 旧 extent | ❌ 原理上不成立 | — |
| **B** | 未被加密的明文区 | ✅ **脚本原样可用** | 0x25 约 88%、0x26 的尾部全部 |
| **C** | 找别的副本 | ✅ **脚本原样可用** | 备份 / 回收站 / 快照 / 别人 home 里的原件 |
| **E1** | **jbd2 日志里的旧数据块** | ✅ **ext4 独有** | 被覆盖前的原始内容，可拼回完整文件 |
| **E2** | **已删除 inode（debugfs）** | ✅ **ext4 独有** | 被删掉但位图还没被复用的文件 |
| D | 块设备雕刻 | ⚠️ 要改扫描范围 | 盘上残留的、还没被复用的旧数据块 |

**ext4 上的执行优先级**（和 Btrfs 的思路一致，只是换了几条路）：

```
第 0 步  取密钥的窗口（与文件系统无关，几小时内的唯一"全救"机会）
   ↓
路线 B   明文提取        分钟级、零风险、纯用户态
   ↓
路线 C   找副本          分钟级、零风险
   ↓
路线 E1  jbd2 日志       分钟级到小时级，**越早越灵**（日志会被后续写入覆盖）
   ↓
路线 E2  已删 inode      分钟级
   ↓
路线 D   雕刻            小时级，可能白干
```

---

## 1. 为什么路线 A 在 ext4 上必然失败

Btrfs 的写时复制（CoW）意味着：文件被改写时，旧数据块**不会原地覆盖**，
而是留在盘上等引用归零后才释放。所以只要旧块还被文件的另一部分引用着，
就能把它读回来 —— 这就是路线 A。

ext4 没有这层保护。勒索程序打开文件、截断、写入新内容（密文），
**同一批物理块被直接覆盖**，旧字节被新字节顶掉。盘上从此没有"旧版本"。
除非旧块因为文件变小、块重排等原因被释放且尚未复用 —— 那属于路线 D 的范畴，
而且极不可靠。

**结论**：ext4 上不要再花时间研究"能不能从文件系统内部把旧 extent 找出来"。
直接走 B / C / E1 / E2 / D。

---

## 2. 步骤 0：确认设备与分区，并**立刻**导出 jbd2 日志

> ⚠️ **时间就是命中率**：jbd2 日志只保留最近的事务（默认 4MB，最大 64MB）。
> 你在卷上每写一次东西，勒索前的旧内容就可能被挤出日志。
> **磁盘探测、导出日志这一步要放在所有其他事情前面做。**

```bash
# ① 找到 ext4 分区在哪（读分区表 + 用 ext4 魔数 0xEF53 自验证，不靠猜）
python3 scripts/carve_scan.py --fs ext4 --dev /dev/sda
#    ext4 魔数在「分区起点 + 0x438」处（超块在 +1024，magic 在超块内 +0x38）

# ② 探测卷信息 + 导出日志镜像（**只读**，不挂载、不 fsck）
python3 scripts/ext4_probe.py --dev /dev/sda1 --dump-journal /tmp/jrn.bin \
        --json-out /tmp/ext4.json --lsdel-out /tmp/lsdel.tsv
```

`ext4_probe.py` 会打印块大小、inode 数、特性位（extent / 64bit / huge_file /
metadata_csum）、**日志 inode 号与日志物理块数**，以及已删除 inode 清单。
这些数字后面都要用。

> 没有 `debugfs` 时（e2fsprogs 没装）：`ext4_probe.py` 仍能读超块，
> 但拿不到日志块和已删 inode 清单。装 e2fsprogs：
> `apt install e2fsprogs` / `apk add e2fsprogs` / entware 的 `e2fsprogs`。
>
> 已经有日志镜像也可以直接用 debugfs 导：
> `debugfs -R "dump_journal /tmp/jrn.bin" /dev/sda1`

---

## 3. 路线 B：明文提取（ext4 上最该先做的一步）

**这一条与文件系统完全无关**，脚本原样可用。原理：勒索程序把整个文件写成了新内容，
但**未被加密的那部分字节仍然是原始明文**，只是搬了位置。

```bash
# 读标记：拿到每个密文的 mode / orig_len（orig_len = footer[580:588] u64 LE）
python3 scripts/read_footers.py /volume1/homes/<用户> > /tmp/footers.json

# 明文提取：密文区填 0、明文区填原字节，**保持原文件偏移**
python3 scripts/plain_extract.py --stamp 勒索恢复_ext4 /volume1/homes/<用户>
```

加密区间模型（与 `plain_extract.py` 完全一致）：

| mode | 加密区间 | 明文占比（按 `orig_len`） |
|---|---|---|
| `0x24` | 整个文件 | 0% |
| `0x25` | 三等分，每段前 4%（`w = 4*(n//100)`） | **约 88%** |
| `0x26` | 前 1 MiB | 尾部全部（文件 < 1MiB 时退化为整文件加密） |

拿回明文之后继续抢救容器结构（脚本原样可用）：

```bash
python3 scripts/salvage_zip.py      <密文目录>     # 从坏掉的 xlsx/docx/zip 里抢救成员
python3 scripts/xlsx_from_members.py <抢救目录>   # 把抢回来的成员还原成 xlsx + csv
python3 scripts/pdf_rebuild.py     <密文文件>     # 重建 PDF
python3 scripts/pdf_xref_repair.py <PDF 文件>     # PDF 就地追加 xref（零损失，优先）
```

> **0x25 的坑**（与 Btrfs 一样）：容器中央目录落在加密区，
> `zipfile` 打不开，只能用**密文原件**抢救成员。
> **0x26 的坑**：尾部明文区可以直接抢救 zip 成员（实测能救回 `sharedStrings.xml` 等）。

---

## 4. 路线 C：找别的副本（ext4 原样可用）

ext4 上没有子卷概念，所以 `plain_index.py` 里那条"**不要加 `-xdev`**"的警告
在 ext4 上不存在（但也别加，`-xdev` 会把别的挂载点整片跳过）。

```bash
# 全盘明文索引：一次 find 抓全卷 (size, path)
find /volume1 -type f -printf '%s\t%p\n' > /tmp/plain_index.tsv

# 按 (basename, orig_len) 找别的目录 / 快照 / 别人 home 里的明文原件
python3 scripts/plain_index.py --index /tmp/plain_index.tsv --footers /tmp/footers.json

# 逐字节证实：候选与密文的未加密区比对（0x25/0x26 可证实；0x24 只能标"同名同大小"）
python3 scripts/verify_origins.py <候选目录> <密文目录>
```

ext4 上常见的副本来源：`#recycle`（回收站）、备份目录、快照挂载点、
同步盘目录、别人的 home。**回收站是 per-home 的**（`/volume1/homes/<用户>/#recycle/`），
不是 `/volume1/#recycle/`。

---

## 5. 路线 E1：jbd2 日志（ext4 独有的金矿）

### 5.1 原理

ext4 默认开日志，挂载选项默认 `data=ordered`：

> **文件的数据块在元数据事务提交之前，会先写进 jbd2 日志；日志里保存的是这些块的旧内容。**
> 这是为了保证"数据先落盘、元数据后提交"的顺序性 —— 万一中途断电，
> 日志里还有旧数据可以回滚。

勒索程序改写文件必然伴随一次元数据事务（文件大小变了、mtime 变了），
于是**被覆盖前的旧内容被写进了日志**。这是 ext4 独有的东西，Btrfs 没有。

限制（必须如实告诉用户）：
- 日志只保留最近的事务（默认 4MB，最大 64MB）→ **越早导出越可能捞到，越晚越没戏**；
- 日志里是整个文件系统的块，混着别的文件 → 拼出来的原件可能缺段；
- `data=writeback` 挂载不保证旧数据进日志；`data=journal` 是整个文件系统写日志，块设备更大。

### 5.2 用明文区当指纹，在日志里定位旧内容

密文文件里有两段：明文区（**没被加密，字节原样**）和加密区（现在是密文）。
用明文区当指纹：

1. 在明文区里取几个 96 字节探针；
2. 在日志镜像里搜索，算出「文件偏移 → 日志偏移」的映射 `delta`；
3. **加密区**在日志里对应的位置，放的就是**旧的原始字节**；
4. 加密区取日志 + 明文区取密文 → 拼成候选原件。

```bash
python3 scripts/jrn_match.py /tmp/jrn.bin --cipher-dir /volume1/homes/<用户> \
        --out /tmp/jrn_out
```

输出会逐个文件报告：模式、`delta`（`multi` = 多个探针一致，可信；`single` = 只中一个，存疑）、
日志能覆盖加密区的百分比、魔数、以及**结构校验结果**。
只有结构校验通过（zip 能开 / PDF 有 `%%EOF` / PNG 有 IEND / JPEG 有 FFD9）的才会落盘。

> ⚠️ 落盘的是**候选件**，不是"已恢复"。交付前仍要过
> `scripts/validate_installed.py` / `scripts/deep_validate.py`。
>
> **最强的证据**：如果文件头（魔数）落在**加密区**里，而拼出来的候选件头部
> 是正确的 `%PDF-` / `PK\x03\x04` / PNG 头 —— 说明日志里确实躺着这个文件的旧版本。
> 脚本会标 `魔数来自日志`（`magic_from_journal`）。

### 5.3 日志镜像本身也可以直接雕刻

日志镜像就是个普通文件，扫描器对普通文件同样有效（`os.pread` 能读文件）：

```bash
python3 scripts/carve_gen_hits.py scan /tmp/carve_jrn --targets /tmp/targets.tsv \
        --dev /tmp/jrn.bin --range 0:$(stat -c%s /tmp/jrn.bin) --workers 1 \
        --sigs pdf,pdf_end,png,jpeg,eocd
```

---

## 6. 路线 E2：已删除 inode（debugfs）

被删除的文件，inode 号还在位图里、没被复用的话，**内容还在盘上**。
`debugfs` 能列出来并直接 dump：

```bash
# 列出已删 inode（inode / 大小 / 块数 / 删除时间）
python3 scripts/ext4_probe.py --dev /dev/sda1 --lsdel-out /tmp/lsdel.tsv

# 取回其中一个（只新增、不覆盖）
debugfs -R "dump <inode号> /tmp/out.bin" /dev/sda1
```

注意：
- `debugfs` **不加 `-w` 就是只读**；加了 `-w` 会写盘，**绝对不要加**；
- 挂载着的文件系统用 debugfs 读可能不一致，**以只读方式卸载后再做最稳**；
- C3RB3R 是"改名 + 改写"而不是"删除"，所以这条路线只对**用户自己删过的文件**有效，
  勒索前被删的旧版本、临时文件、`.swp` 之类才可能在这里。

---

## 7. 路线 D：在 ext4 上雕刻（要改扫描范围）

Btrfs 把元数据和 DATA chunk 分开，所以扫描器只扫 DATA chunk。
**ext4 的元数据和数据混在同一分区**，没有 chunk 划分，只能按字节范围整段扫。

```bash
# ① 先算出 ext4 分区在设备上的字节范围
python3 scripts/carve_scan.py --fs ext4 --dev /dev/sda

# ② 按范围扫（--range 的偏移是相对 --dev 的字节偏移）
python3 scripts/carve_gen_hits.py scan /tmp/carve_out --targets /tmp/targets.tsv \
        --dev /dev/sda1 --range 0:8589934592 --workers 2 \
        --sigs pdf,pdf_end,png,jpeg,eocd

# ③ 离线配对（把开头/结尾签名配成文件长度）
python3 scripts/carve_gen_hits.py match /tmp/carve_out /tmp/targets.tsv

# ④ 认领 + 装回（与 Btrfs 完全同一套流水线）
python3 scripts/carve_claim.py /tmp/carve_out /tmp/targets.tsv /tmp/carve_out2
python3 scripts/carve_install.py plan --home /volume1/homes/<用户> \
        --targets /tmp/targets.tsv --index /tmp/empty_index.tsv \
        --claim /tmp/carve_out2/_认领结果.json --out-plan /tmp/plan.tsv
python3 scripts/carve_install.py install --plan /tmp/plan.tsv.json --log /tmp/install.log
```

`targets.tsv` 的格式是 `<原始长度>\t<扩展名>\t<相对路径>`，
用 `scripts/read_footers.py` 输出的 `orig_len` 生成。

**并发**：ext4 卷通常在机械盘阵列上，**2~4 路**即可，20 路反而更慢（磁头互踩）。
DSM 上没有 `ionice`，用 `nice -n 19`。

---

## 8. 现实预期（按加密模式）

| mode | 明文区占比 | ext4 上现实能拿回什么 |
|---|---|---|
| `0x25` 分段 | 约 88% | **路线 B 直接拿到 88% 的原文字节**；E1 若命中可再补上那 12%，凑成完整原件 |
| `0x26` 前缀 1MiB | 1MiB 之后全部 | **路线 B 拿到尾部全部**；E1 命中时可补回头部（PDF/图片这类最有价值） |
| `0x24` 整文件加密 | 0% | 只能靠 E1（日志里若留有旧块）、E2、C（副本）、D（雕刻） |

**0x24 的文件不是"没救"，而是"只可能从盘上的残留里救"**：
- E1 命中 → 概率最高、代价最小，务必先做；
- C 命中 → 只要有一份没被加密的副本就直接可用；
- D 命中 → 看运气，取决于旧块有没有被复用。

---

## 9. ext4 红线（比 Btrfs 还多两条）

1. **不要跑 `e2fsck`**（不带 `-n` 的）—— 它会重写位图、**清掉释放的块**，
   把还没捞出来的旧数据彻底销毁。`e2fsck -n` 是只读的，可以跑（但很慢）。
2. **不要 `resize2fs`、不要 `tune2fs` 改特性、不要 `mkfs`、不要以读写方式重新挂载。**
3. **不要用 `debugfs -w`**，不要 `debugfs -R "rm ..."` 之类写操作。
4. **不要做碎片整理 / defrag**（会重排数据块，等于毁掉残留）。
5. **不要删任何 `.L0CK3D`**：密文里还有没被加密的明文区，删了就永久没了。
6. 恢复结果写到**卷以外**的盘。

---

## 10. 给用户的话术（别把话说满）

> 您的这台机器用的是 ext4，不是 Btrfs。ext4 没有"写时复制"，
> 勒索程序在原文件上直接覆盖写入，勒索前的旧版本在文件系统层面**已经不存在了**，
> 这是 ext4 的机制，我们没法绕过。
>
> 但有三件事还能做，我们按顺序来：
> ① 勒索程序**没有加密的那部分内容**（按加密方式，占 0% ~ 88% 不等）仍然是原样，
>    我们先把它提取出来，这一步零风险、几分钟；
> ② ext4 默认开文件系统日志，日志里常常留着被覆盖前的旧内容，
>    我们现在就去日志里找（**越早越有戏**，所以我们先做这个）；
> ③ 到备份目录、回收站、快照和同事的目录里找还没被加密的原件。
>
> 最后才是全盘雕刻 —— 这一步要读整块盘，要几个小时，而且不一定有收获。
> 另外，勒索程序如果还在跑，**从它的内存里拿密钥**是唯一能把文件全部解开的办法，
> 这个窗口只有几个小时。

---

## 11. 相关文件

- `scripts/ext4_probe.py` —— ext4 卷只读探测 + 日志导出 + 已删 inode 清单
- `scripts/jrn_match.py` —— 在日志镜像里用明文区指纹找回旧内容
- `scripts/carve_scan.py` —— `--fs ext4` 按 ext4 魔数定位分区
- `scripts/carve_gen_hits.py` / `scripts/carve_gen.py` —— `--range` 按字节范围扫（非 Btrfs 卷）
- `references/carve-generic.md` —— 通用雕刻细节
- `references/pitfalls.md` —— 踩过的坑（含 ext4 相关）
