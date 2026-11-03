# 路线 D 通用版：按文件魔数在块设备上雕刻已被释放的旧 extent

> 适用：目标是被**整文件加密（mode 0x24）**、文件树里已无旧 extent 引用的文件；
> 或任何"文件树里找不到旧数据、但数据可能还在盘上"的情况。
> 本文档记录 2026-10-31 在群晖 NAS（14.5TiB 卷）上实测的一整套做法。

## 为什么需要这一版

`references/carve-osheet.md` 那套是 **.osheet 专用**：靠 39 字节的强签名（作者 uid）定位，
误报几乎为零。但它只认 `.osheet` 一种容器。

当目标是一批 **pdf / xlsx / docx / zip / png / jpg** 时，需要一套通用的、
按「文件长度」而不是「文件路径」认领的雕刻方法：

- 盘上被释放的旧 extent **没有路径**，没法按文件名找；
- 但**文件长度是知道的** —— 密文尾部标记里存着原始长度 N；
- 于是只要能在盘上扫出「长度 == N 且文件头合法」的文件，就能把它认领回给对应的密文。

## 三个关键前提

### 1. 拿到原始长度（orig_len）

密文尾部 588 字节标记的 `footer[580:588]` 是 **原始长度 N（u64 LE）**。
`scripts/read_footers.py` 一次把目录里所有密文的 `mode / pct / enc / orig_len / session marker`
读出来存成 JSON。实测：用户A home 的 624 个密文，`size == orig_len + 588` **全部成立**，
说明标记自洽，orig_len 可直接当长度基准。

### 2. 扫得够快（否则 2.65TB 要跑几天）

**踩过的坑：用 `re.compile(b'PK\x03\x04|...')` 一把扫，实测只有 5 MB/s** ——
Python 正则的 alternation 在长字节串上退化成逐字符尝试，比磁盘还慢。

改成 **每个签名单独 `bytes.find()`**（底层 memchr）后，扫描速度 **406 MB/s**（单核纯扫描）。

| 写法 | 速度（64MB 缓冲） |
|---|---|
| `re` alternation | **5 MB/s**（别用） |
| `bytes.find()` 逐签名 | **406 MB/s**（用这个） |

### 3. 边扫边匹配，不要落命中日志

第一次跑通用扫描时，把每个命中都写进日志：JPEG 的结尾签名 `\xff\xd9` 在 2.65TB 上会命中
**几千万次**，日志膨胀到 GB 级，后续认领阶段光读日志就要十几分钟、内存也吃不下。

改成 **边扫边匹配**：扫描器自己带着「目标长度集合」，命中结尾签名时立刻算出长度、
查表、命中就记一条候选（通常只有几十条）。日志从 GB 级降到 KB 级。

## 签名表

| 类型 | 开头签名 | 结尾签名 | 尾长 | 长度怎么算 |
|---|---|---|---|---|
| zip 族(xlsx/docx/zip/pptx) | `PK\x03\x04` | `PK\x05\x06`（EOCD） | 22+comment | **EOCD 的 22 字节里直接有**：`size = cd_off + cd_size + 22 + comment_len`，**一次 22 字节读就能精确算长** |
| png | `\x89PNG\r\n\x1a\n` | `\x00\x00\x00\x00IEND\xaeB\x60\x82` | 12 | `size = 结尾 + 12 - 开头` |
| jpeg | `\xff\xd8\xff` | `\xff\xd9` | 2 | `size = 结尾 + 2 - 开头` |
| pdf | `%PDF-` | `%%EOF` | 5 | `size = 结尾 + 5 - 开头` |
| xls/doc (OLE2) | `\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1` | — | — | 结构复杂，本版只记偏移不做长度判定（价值低） |

**配对规则**：顺序扫描时记住最近几个「开头」偏移，遇到「结尾」时**从最近的往回试**，
第一个能算出目标长度的就配对。结尾签名在文件内部不会误命中
（JPEG 熵编码数据里 `0xFF` 必被填 `0x00`，`FFD9` 只可能是文件尾）。

## 扫描器只 dump tree 3

`btrfs inspect-internal dump-tree -t 3`（chunk 树）拿 DATA chunk 列表，**2.3 秒**。
本机 14.5TiB 卷上 **tree 1/2 的 dump-tree 要跑几十分钟**（extent 树有几千万条），
别全 dump。

两个细节：
- dump-tree 会把 chunk 列表**输出两遍**，必须按 chunk id 去重，否则每个 chunk 白扫一次；
- 物理起点取 `stripe devid N offset M` 里的 **M**（物理偏移），不是逻辑地址。

## 实测参数（群晖 NAS / RAID1 机械盘阵列 / cachedev_0）

| 项 | 值 |
|---|---|
| DATA 区总量 | 2.65 TB（271 个 chunk，每个 10GB） |
| 单流读速 | 213 MB/s |
| 3 路并发 | 各 ~110 MB/s，聚合 **~330 MB/s** |
| 全盘扫完 | **约 2.2 小时** |
| 峰值命中数 | JPEG 结尾签名 ~4000 万次 → 必须边扫边匹配 |

并发路数按盘的类型定：机械盘阵列 4 路聚合 906 MB/s、20 路反而掉到 341 MB/s（磁头互踩）。
**2~4 路是安全区**，并给 NAS 日常访问留带宽。DSM 无 `ionice`，只能 `nice -n 19`。

## 操作步骤

```bash
# 1) 读出所有密文的 orig_len / mode
python3 read_footers.py /volume1/homes/<用户>            # -> footers.json

# 2) 造目标长度清单：size \t ext \t 相对路径
#    （本地生成后 tr -d '\r' 再传 NAS）

# 3) 启动扫描（3 路，后台）
for i in 0 1 2; do
  setsid nohup nice -n 19 python3 carve_gen.py <日志目录> --worker $i --workers 3 \
      --targets <目标清单.tsv> > <日志目录>/w$i.out 2>&1 &
done

# 4) 扫描结束后认领 + 校验 + 落盘
python3 carve_claim.py <日志目录> <目标清单.tsv> <输出目录>
#    carve_claim.py 会：按长度认领 → 结构校验（zip 能开 / PDF 有 xref / 图片头尾完整）→ 落盘
```

`carve_gen.py` 输出的候选日志每行：`type \t start \t size \t endsig_offset`。

## 认领后的确认（重要）

雕刻出来的是**内容正确但路径未知**的文件。怎么确认它就是某个密文的原件？

1. **长度必须精确等于 orig_len**（这是第一道闸，carve_claim 已经卡死）；
2. **文件头/结构校验通过**（第二道闸）；
3. **同类文件的大小撞车**：实测 483 个目标里有 19 个大小重复（如 3 个文件都是 26112 字节），
   这种要靠内容特征（xlsx 的 sheet 名、pdf 的标题）再人工分一下。

**2026-11-01 补充的三条硬规则：**

- **分组键用 (长度, 文件名)，不是只用长度。** 同名同长 = 同一份文档的副本；
  同长不同名 = 完全不同的文档，长度撞车时不要凭长度乱认。
- **同一份文档在盘上往往有多份物理副本**（实测 240 份 / 63 个目标），
  **要逐份雕出来评质**（PDF 看 xref 偏移吻合数、zip 看 `testzip()`、图片看像素数），
  别只取第一份 —— 第一份可能是"碎片拼接"的坏副本。
- **OLE2（xls/doc）不要按长度认领**。用 `ole2_size.py` 能算出分配长度，
  但 2026-11-01 实测 **13 个目标命中 35 处、正文核对后 0/6 命中**，
  装错一份合同比空着更糟。

## 已知局限

- 雕刻出来的是**原件**，但**没有原路径**；要装回原处得靠「长度 + 名字/内容特征」人工配对。
- 旧 extent 可能已被后来的写入覆盖（尤其勒索程序自己写密文时可能就地复用了刚释放的块）。
  卷空闲越多、事发后写入越少，命中率越高。
- OLE2（xls/doc）本版不做长度判定（见上）。
- 扫描本身是全盘顺序读，**会吃满磁盘带宽**，别和导出/其它扫描同时跑。
- **`carve_gen.py` 的 pending 队列只有 16 个槽**：盘上 PDF 一多，较早的开头偏移会被挤掉，
  于是那个文件再也配不上自己的 `%%EOF` → 认领不到。
  2026-11-01 为此写了 `carve_gen_hits.py`（记录**全部**命中偏移 + 离线集合配对）。

## 配套脚本

| 脚本 | 在哪运行 | 作用 |
|---|---|---|
| `scripts/read_footers.py` | NAS | 读所有 `.L0CK3D` 的尾部标记 → orig_len / mode / session marker |
| `scripts/carve_gen.py` | NAS（root） | 通用魔数扫描 + **边扫边匹配**目标长度，输出候选 |
| `scripts/carve_gen_hits.py` | NAS（root） | 扫描器 v2：**记录全部**签名命中偏移 + 离线配对（解决 pending 溢出） |
| `scripts/carve_claim.py` | NAS（root） | 认领候选 → 结构校验 → 落盘 |
| `scripts/carve_install.py` | NAS（root） | **路线 C+D 统一收尾**：分组/择优/证实/出计划/装回一条龙 |
| `scripts/plain_index.py` | NAS | **全盘明文索引**，找别的目录/快照/别人 home 里的原件（⚠️ 勿加 `-xdev`） |
| `scripts/verify_origins.py` | NAS（root） | 路线 C 候选原件的**逐字节证实**（用密文未加密区比对） |
| `scripts/xlsx_from_members.py` | 本地 | 把抢救回来的 xlsx 内部成员还原成 xlsx + csv |
| `scripts/pdf_rebuild_plain.py` | NAS 或本地 | 雕刻出的 PDF **全量重建**（⚠️ 会丢内容，优先用下面的就地修复） |
| `scripts/pdf_xref_repair.py` | NAS 或本地 | 雕刻出的 PDF **就地修复**：原字节全保留，只追加新 xref |
| `scripts/ole2_size.py` | NAS（root） | OLE2 分配长度反推（⚠️ 只当线索，假阳性率高） |

## 2026-10-31 补充：速度与正确性的四个要点

1. **签名要按需收窄**。8 个签名（png/jpeg/pdf/zip/ole + 结尾签名）时单流只有 **110 MB/s**，
   收窄到 3 个（zip EOCD + pdf 头 + %%EOF）后 **165 MB/s**。
2. **加预取线程**（读取与扫描并行，队列深度 3）→ **227 MB/s**，跑满 RAID1 机械盘的顺序读上限
   （实测裸读 211 MB/s）。代码见 `carve_gen.py` 的 `producer()`。
3. **开头/结尾签名的字典 key 必须分开**（`SIG_TYPE` vs `END_TYPE`），
   写反了 pdf/png/jpeg 会**一个都配不出来**，而日志里只会有 zip 候选 ——
   **扫完先看候选的类型分布**，全是 zip 就是配对没生效。
4. **雕刻出的 PDF 常常只坏在 xref**：`startxref` 指向的偏移对不上（中间几百字节被覆盖），
   但对象、页树、Catalog 都完好。`pdf_rebuild_plain.py` 重建 xref 后就能打开，
   **别因为严格校验不过就丢掉整个文件**。
   另外 PDF 常带增量更新，"`/Type /Page` 出现 6 次"可能只对应 **1 个**页对象，
   比页数要数**不同的页对象号**。

## 2026-11-01 补充：把雕刻结果变成可靠交付的五件事

1. **别自己写认领/装回脚本** —— 用 `scripts/carve_install.py`（分组 → 择优 → 证实 → 计划 → 装回）。
2. **全盘明文索引是路线 C 的放大器**：829514 个文件的索引里，按 (basename, orig_len)
   找到 23 个目标的明文副本（9 个能逐字节证实），另有 16 个能用「同名不同版本」顶替。
   ⚠️ 建索引**不要加 `-xdev`**（Btrfs 子卷 st_dev 不同，会被整片跳过：45824 vs 829514）。
3. **PDF 优先「就地修复」而不是「全量重建」**：重建会把 211747 字节的原件压成 103454 字节、只剩 1 页；
   就地追加 xref 零损失，17 个全部救活。副作用是修完的文件比 orig_len 多约 1KB，
   **证实要在修复前做**。
4. **JPEG 允许 FFD9 之后有残留字节**，完整性检查别写 `endswith`；装回时截断到最后一个 FFD9。
5. **同一明文 → 密文不同**（每文件 nonce 随机，实测 99% 字节不同），
   所以 0x24 的同长度密文对**不能**用密文判断"是不是同一份"，
   只能靠**多个物理副本的内容比对**。


## 2026-11-02 补充：OLE2(.xls/.doc) 必须按**正文**认领（长度只用来筛候选）

OLE2 文件**没有结尾签名**，`carve_gen.py` 的签名表里 OLE2 只记偏移、不做长度判定。
要认领 0x24 加密的 `.xls`/`.doc`，必须自己解容器：

```bash
# ① 从雕刻日志里收集所有 OLE2 魔数命中点（每行一个设备物理偏移）
python3 scripts/ole2_identify.py offsets /tmp/c3r_zmm/carve_g_v1

# ② 阶段1：只读 512 字节头 + FAT，算真实分配长度，筛出「长度 ±8KB」内的候选
#    （1909 个命中点约 14 秒）
python3 scripts/ole2_identify.py screen /tmp/c3r_zmm/ole_offsets.txt \
        --targets /tmp/c3r_zmm/targets.tsv --out /tmp/c3r_zmm/ole_near.json

# ③ 阶段2：取流抽正文，按关键词判定
python3 scripts/ole2_identify.py identify /tmp/c3r_zmm/ole_near.json \
        --targets /tmp/c3r_zmm/targets.tsv \
        --words 结算单 <公司甲> <公司乙> <公司丙> 代运营服务协议 食品安全管理制度 \
        --out /tmp/c3r_zmm/ole_content.json

# ④ 内容确认过的才装回（三道闸：长度==orig_len / 正文含关键词 / 写回 SHA 一致）
python3 scripts/ole2_install.py /tmp/c3r_zmm/ole_cand.tsv \
        --home /volume1/homes/<用户> --targets /tmp/c3r_zmm/targets.tsv \
        --outdir /tmp/c3r_ole_rec --log /tmp/ole_install.log
```

**2026-11-02 实测（群晖 NAS / 用户A home）**：

| 目标 | 盘上同长度候选 | 结果 |
|---|---|---|
| 12 个 `.xls`/`.doc` | 长度 ±8KB 内 666 个；精确同长度 20 个 | **4 个 `.doc` 正文 100% 认准**并装回 |
| `合同/公司甲买断-代运营服务协议.doc`（26112） | 2 个物理副本 | 正文「代运营服务协议 … 与公司甲商贸有限责任公司」✓ |
| `合同/公司乙-代运营服务协议(1).doc`（26112） | 同上 | 正文「… 与公司乙科技有限公司」✓ |
| `合同/公司丙买断-代运营服务协议 - 副本.doc`（26112） | 同上 | 正文「… 与公司丙贸易有限公司」✓ |
| `公司甲资料/622/食品安全管理制度目录清单.doc`（12288） | 10 个候选 | 正文标题逐字吻合「食品安全管理制度目录清单 / 食品安全自查制度 / …」✓ |
| 8 个 `结算单.xls` | 长度全对 | 正文期间/品牌与文件名不符 → **不装回**，作候选交付 |

**三条硬规则**：

1. **长度算法别自己写**：用 `ole2_lib.py` 的 `alloc_len()`
   （`512 + (FAT 里用到的最大扇区号+1) × 扇区大小`）。
   旧的 `ole2_size.py` 少算 512 字节，会把真候选全排除（见 pitfalls）。
2. **正文抽取要走流**：`.doc` 取 `WordDocument` 流、`.xls` 取 `Workbook` 流；
   对整块设备数据做 UTF-16LE/GBK 解码只会得到「螌坥鱛犘」这种 ASCII 误解码噪声。
   判关键词时**按字节搜**（`w.encode('gbk')` / `w.encode('utf-16-le')` + `bytes.find`）。
3. **内容对不上就别装**：0x24 没有明文区可比对，正文期间/品牌与文件名不符就是不对上。
   候选改名成真实内容 + 写说明，放进「候选待确认」目录让人工核对。

**顺带**：`0x26`（前缀 1MiB 加密）的 xlsx，明文区的**中央目录和尾部成员**可以直接抢救。
2026-11-02 实测一个 1744874 字节的 xlsx，从明文区救出 **17 个成员**（含
`xl/sharedStrings.xml` 879337 字节、`xl/workbook.xml` 的 5 个工作表名、
10 张内嵌图片），全部 CRC 相符；只有落在前 1MiB 的 `xl/worksheets/sheetN.xml` 丢了。
