# 数据区签名雕刻：把被整文件加密的 `.osheet` 原件从盘上捞回来

> 2026-10-31 实测成功：群晖 NAS（14.5 TiB，DATA 区 2.63 TiB，空闲 12 T），
> 救回 `客服上班表-最新.osheet`（48250 B，**与勒索标记原长完全一致**）和
> `保税区退款订单.osheet`（19559 B，**同样一致**）。

---

## 0. 先看这张决策树（30 秒决定走哪条路）

```
目标文件被勒索整文件加密（尾部标记 mode=0x24）、且体积 < 1MB？
│
├─ 卷空闲 < 5%？ ── 是 ──> 旧数据块大概率已被复用，雕刻白干
│                    └─> 回头走路线 C（找备份 zip / 其它副本），别浪费时间
│
├─ 卷空闲充足？ ── 是 ──> ★ 走本文：数据区强签名雕刻
│
└─ 目标在 NAS 上还有同名未加密副本吗？ ── 有 ──> 先走路线 C（最省事）
```

**什么时候用本文**：文件被**整文件加密重写**、旧 extent 已从文件树摘掉
（`c3r_btrfs.py plan` 判 `no_candidate`），但卷还空着、旧数据块还在盘上。

**什么时候不用**：文件只是被加密了开头一段（`0x25`/`0x26`，>1MB）→
先走路线 A（`plan` 会给出仍被引用的旧 extent，直接 `extract` 就行，别雕刻）。

**`.osheet` 以外的格式**：思路完全一样，把 39 字节签名换成该格式的魔数/文件头即可
（xlsx 用 `PK\x03\x04`、PDF 用 `%PDF-`），但**没有 uid 这种强区分度**，误报会多，
认领时要靠"结构自洽 + 长度吻合 + 内容像"三重过滤。

---

## 1. 本轮走过的弯路（**不要重复**）

| # | 弯路 | 浪费 | 教训 |
|---|---|---|---|
| 1 | **去元数据区反查"旧叶子块"**，想从旧叶子里的 EXTENT_DATA 项拿到原 extent 地址 | ~40 分钟 | 方向对但**叶内 item 头阵列的真实布局试了三版都没钉死**。**直接扫数据区**，又快又稳。元数据区的坑见文末 |
| 2 | **把逻辑地址当物理偏移读**（`btrfs` 给的 extent 地址是逻辑地址） | 一整轮白扫 | 扫出 300 个命中"一个都不匹配"就是这么来的。**先校准再全扫**（第 3 节） |
| 3 | **把尾部标记里的 u32 当成文件长度** | 认领全灭 | 那个 u32 是**尾部段自身长度（=11）**，不是文件长度 |
| 4 | 只细看邻近命中的**前几个**就下结论 | 差点误判"最终版不在盘上" | 第 6 个邻近命中恰好就是原版。**必须批量认领**，不能抽查 |
| 5 | 拿 `schema` 长度 = 2 的容器当目标 | 白认领 | 长度 2 = **旧版 WPS**，3 = 现代版 |
| 6 | 中文目录名 unicode 转义写错 | 两次返工 | `勒索` = `\u52d2\u7d22`（写成了 `\u52a0` 就成了"加"）。**用目录结构识别，不要硬编码** |
| 7 | 认领脚本"只认了一半" | `--len 48250 19559` 只吃掉第一个值，第二个数字被当成日志名 | 已改 argparse：`--len N [N...] --label A [A...] --log <日志...>`，**日志一律用 `--log`** |

---

## 2. 签名从哪来

现代版 WPS OSheet（`schema` 长度 = 3）的头部 **39 字节完全固定**：

```
78 00 00 00                     magic
12 "schema\0" 03 00 00 00      tag=0x12, key="schema", 值长 u32=3
00 00 00                        值
08 "enc\0" 00                   tag=0x08, key="enc", 值长 u8=0
02 "id\0" 23 00 00 00           tag=0x02, key="id", 值长 u32=35
"1042_..."                      id 前 5 字节 = 作者 uid（每个用户一个专属值）
```

最后 5 字节是**用户 uid**，所以每个用户一份专属签名，别人的文件不会误命中。

三种取签名的方式（`carve_osheet_sig.py` 都支持，按优先级）：

```bash
# 1) 最好：拿一个该用户完好的 .osheet 当模板（任何版本都行，脚本自己截到 id 字段结束）
python3 carve_osheet_sig.py /tmp/carve_out --sig-file /volume1/homes/用户B/26年售后记录表.osheet <窗口起> <窗口止>

# 2) 按 uid 拼（uid 必须是 4 位数字 + 下划线）
python3 carve_osheet_sig.py /tmp/carve_out --uid 1042 <窗口起> <窗口止>

# 3) 直接给 hex
python3 carve_osheet_sig.py /tmp/carve_out --sig-hex 7800000012736368656d6100... <窗口起> <窗口止>
```

---

## 3. 流程（照做，六步）

### Step 1　先确认值得做
```bash
RUN plan <目标文件> --g G      # 确认 status=no_candidate 且 mode=0x24
df -h /volume1                 # 空闲 < 5% 就别做
```

### Step 2　准备目标长度
勒索标记尾部 offset 580 处是**原始长度**（u64 LE）——这是认领的关键锚点。
`probe_marker.py` 那套可以直接读出来。

### Step 3　**校准（必做！）**
拿一个**已知仍被引用的旧 extent 地址**（用 `c3r_btrfs.py plan` 从某个大文件拿），
先用扫描器确认：那个地址能命中签名、且读出来的容器能严格解析。
地址换算：逻辑 → 物理要过 chunk 表（`carve_osheet_sig.py` 内部已按 chunk 表换算，
单卷 DATA 区偏移恒为 `+1082130432`，但别硬编码）。

### Step 4　全量扫描
把 DATA 区按窗口切给多个进程并行（单流 ~225 MB/s，6 路聚合 ~534 MB/s，2.63 TiB 约 4 小时）。
```bash
# 每路一个窗口，日志分开写
python3 carve_osheet_sig.py /tmp/carve_s1 --uid 1042 1900000000000 2180000000000 > /tmp/carve_s1.log &
python3 carve_osheet_sig.py /tmp/carve_s2 --uid 1042 1500000000000 1900000000000 > /tmp/carve_s2.log &
...   # 窗口要覆盖整个 DATA 区，别留空隙
```

### Step 5　**批量**认领
```bash
python3 carve_osheet_identify.py --len 48250 19559 \
        --label 客服上班表-最新 保税区退款订单 \
        --log /tmp/carve_s*.log
```
判据三关（缺一不可）：
1. **尾部标记定真实长度**：尾部 = `[u32 尾部长度][0x08]["end\0"][值]`，
   **文件长度 = (尾部标记起点 − 4) + 该 u32**（u32 是尾部段自身长度，通常 11）。
2. **`osheet_strict.py` 严格解析完全自洽**（宽松解析不算数）。
3. **长度 == 勒索标记里的 orig_len** → 就是原件，**精确恢复**。

### Step 6　交付前的比对与落位
- 长度不等于 orig_len 的命中 = **同一文件的其它自动保存版本**。
  用 `carve_osheet_near.py <逻辑地址...>` 逐个看，逐格比对后选最全的那版，
  并在说明里写清"哪一版、差在哪几格"——**近似版不能说成精确恢复**。
- 同时给 `.osheet` 原件 + 合并成一个工作簿的 `.xlsx`（`osheet_to_xlsx.py`）。
- 落位：`scripts/restore_inplace.py` / 手写流程 —— **只新增不覆盖**，
  无条件 chown → chmod → `synoacltool -copy` → `synoacltool -add administrators`。

---

## 4. 顺带确认的 Btrfs 事实（省得再踩）

| 事实 | 值 |
|---|---|
| EXTENT_DATA 的 key type | **108**（168 是 EXTENT_ITEM） |
| EXTENT_DATA 项数据体 | 53 字节：`gen@0 ram@8 comp@16 enc@17 oenc@18 type@20 disk_bytenr@21 disk_num_bytes@29 offset@37 num_bytes@45` |
| 树块头布局 | sha256 式（csum 占 32 字节）：`fsid@0x20 bytenr@0x30 flags@0x38 chunk_tree_uuid@0x40 generation@0x50 owner@0x58 nritems@0x60 level@0x64 csum_type@0x65` |
| 超级块 csum_type 偏移 | **0xC8**（不是 0xC4） |
| 叶子块大小 | 16 KB（nodesize），不是 4 KB |
| 文件的 `num_bytes` | 按扇区向上取整（19559 字节的文件记成 20480） |

⚠️ 元数据区反查旧叶子块那条路本轮**没走通**（叶内 item 头阵列的真实布局没钉死）。
**别在元数据区硬啃——直接扫数据区。**
