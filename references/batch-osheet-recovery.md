# 批量恢复 `.osheet`：雕刻索引 + Drive 重建，两条路互为证明

> 2026-10-31 晚在群晖 NAS 上定型。**一次跑完 74 个 `.osheet`，救回 58 个（78%）**。
> 本文是"从单个文件试水"到"成批交付"的操作手册。

---

## 0. 为什么值得先做批量盘点

单文件试水（第一批 14 个）摸清了容器格式后，**才有资格做批量**。批量前先花几分钟
把全盘密文清点一遍，就知道自己面对的是多大工程：

```bash
# NAS 上（root）：扫所有 .L0CK3D，读尾部 588 字节的 orig_len / mode
python3 - <<'PYEOF'
import os, struct
FOOT = 588
for root in ['/volume1/homes', '/volume1/千牛商品', '/volume1/编队备份', ...]:
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in ('@eaDir', '#recycle')]
        for fn in fns:
            if not fn.endswith('.L0CK3D'):
                continue
            p = os.path.join(dp, fn)
            sz = os.path.getsize(p)
            if sz < FOOT:
                continue
            with open(p, 'rb') as f:
                f.seek(-FOOT, 2); ft = f.read(FOOT)
            orig = struct.unpack_from('<Q', ft, 580)[0]
            print('%d\t0x%02x\t0x%02x\t%d\t%s' % (orig, ft[572], ft[574], sz, p))
PYEOF
```

本机实测结果（**结论先行**）：

| 维度 | 数量 |
|---|---|
| 密文总数 | 8716 |
| 其中 jpg/png/JPG 等图片 | 5000+ |
| **`.osheet`** | **74** |
| `.xlsx` / `.docx` / `.pdf` / `.zip` | 306 / 87 / 459 / 180 |

**结论：容器型文档只占 5%，别被 8716 这个数字吓退。** 先把 `.osheet` 做完（74 个），
再按同样的思路扩到其它格式。

---

## 1. 两条路线

| 路线 | 做法 | 产出 |
|---|---|---|
| **雕刻认领** | 复用已扫完的全盘索引（`logs/w*.log`），按 orig_len 认领 | **字节级精确原件** |
| **Drive 重建** | 用 `@SynoDrive/office/<uid>_<docid>.sh/version/text/` 的勒索前明文重建 | 内容等价的重建件（可做到逐字节） |

**批量认领一次搞定**（不用一个一个来）：

```bash
python3 claim_fast.py --len <N 个长度> --label <N 个标签> \
        --log /tmp/c3r_carve/logs/w*.log --out /tmp/c3r_carve/claim_all
```

74 个长度 → 199780×74/74… 实测 **100 万次候选检查约 10 分钟**，
从 14270 个去重地址里捞出 22 个字节级精确原件。

---

## 2. Drive 重建怎么匹配到对应的密文

**难点**：密文的 `id` 字段被加密了，读不出来；而 Drive 仓库目录名就是 `id`。
**只能靠长度反推**：重建一个 Drive 文档，看它的长度 == 某个密文的 orig_len 就算对上。

### 2.1 长度公式（实测确认）

```
容器长度 = 头部 + Σ条目 + 尾段(11)

头部 = 120 + (len(id值) - 35)        ← 实测 idlen=35 时头长 120
条目 = 12 + len(条目名) + len(Drive 数据)
       （= 4(X) + 1(tag) + len(name)+1 + 4(len) + (len(data)+1) + 1(tail)）
尾段 = 11
再加 metatext 条目：短版 +32 / 长版 +129
```

### 2.2 ⚠️ 长度匹配**会撞车**（本轮实测踩到）

不同文档可能恰好同长。实测有 1 个密文（orig_len 27331）按长度匹配到了
`<账号甲>_<docid1>.sh`，但雕刻出的原件头部 `id` 是
`<账号乙>_<docid2>.sh` —— **完全是另一个文档**。

**两条防线**：

1. **有雕刻原件时，直接用它的 `id` 字段**（== Drive 仓库目录名）做精确键。
   21 个两路都有的文件里，靠 id 校对推翻了 1 个长度误配、修正了 2 个长度公式对不上的。
2. **没有雕刻原件时（Drive-only）**，用长度匹配 **+ 同目录已知 uid 一致性**过滤：
   先从同 home 目录下已确认的雕刻原件收集 uid 集合，只接受 uid 在集合里的候选。

> 顺带一个**有利**的性质：长度匹配本身会**自动过滤掉 Drive 里的更新版本** ——
> Drive 若存的是更新版（更大），算出的长度就 != orig_len，不会被误配。
> 反过来也说明：**长度对上了，基本就等于"Drive 版本 == 勒索时的版本"。**

### 2.3 `metatext` 有长短两版

| 形态 | 载荷 | 条目总长 | 取法 |
|---|---|---|---|
| 短版 | `{"ntype":37}`（12 字节） | 32 | 默认 |
| 长版 | `{"import_format":"drive","import_path":"link:...","ntype":37,"user":"..."}`（109 字节） | 129 | 从**密文明文区**抠（0x26）或从雕刻原件里抠 |

用错形态 → 长度差 **97 字节**。

---

## 3. 交叉验证：两条路互为证明（**本轮最有价值的一步**）

有 21 个文件两路都拿得到。把**雕刻出的字节级精确原件**当 ground truth 去检验 Drive 重建：

```
19/21 逐字节完全相同（SHA-256 一致）
 2/21 不同 → 查清原因：Drive 仓库里存的是该文档的**更新版本**（更大），
              不是重建法出错。这 2 个最终采用雕刻原件。
```

**没有这一步，重建法只能算"看起来对"**。做完这一步，重建件就能按"精确恢复"的口径交付。

---

## 4. 用密文明文区独立对证

`mode 0x26` = 只加密开头 **1 MiB**，其余是原件原样字节。于是：

```
C0 = 密文去掉尾部 588 字节标记
重建件 R 与 C0 从偏移 E=1048576 起应当逐字节相同
```

本轮 7 个 0x26 文件对证了 374 万字节，**全部一致**。

**同时解决 metatext 问题**：长版 metatext 就在密文明文区尾部，直接抠出来。

`mode 0x24`（整文件加密）没有明文区可对证，只能靠「长度 == orig_len」+ 方法自检。

---

## 5. 完整流水线（照做）

```bash
# 0) 盘点：全盘密文清单 → all_cipher.tsv
# 1) 认领：批量 claim_fast → claim_all/（22 个字节级原件）
# 2) 建 Drive 索引：每个仓库的条目名 + 大小 + master → drive_index.tsv
# 3) 匹配：长度 + uid → 映射表
python3 scripts/match_drive_all.py      # 含自检（用已知对验证公式）
python3 scripts/final_pipeline2.py      # 建映射→重建→交叉验证→对证→组装
# 4) 校验：严格解析 + 转 xlsx + 全量逐格回读
python3 scripts/verify_batch.py <目录> --out <目录>
# 5) 装回：只新增、属主/ACL 对齐同目录密文、写完回读 SHA-256
```

### 交付前必须过的四关

| # | 检查 | 判据 | 本轮结果 |
|---|---|---|---|
| 1 | 容器结构 | `osheet_strict.py` | 58/58 |
| 2 | 长度 | == 勒索标记 orig_len | 44/44 新交付件全中（1 个差 97，见下） |
| 3 | 独立对证 | 与密文明文区逐字节 | 7/7 |
| 4 | 回读 | `--sample 999999999` 全量逐格 | 58/58，0 不符 |

---

## 6. 已知例外与坑

### 6.1 长度差 97 字节（数据完整，只差元数据）

`用户A/2025年异常出库及过期出库表`：长版 `metatext` + 整文件加密 → 载荷取不到 →
重建件用短版 → 少 97 字节。**表格数据完整**（工作表数、有值格数都正常），
差的只是那条元数据。交付时要说清。

### 6.2 Synology Drive 同步客户端会覆盖你装回的文件

装回后 8 分钟，Drive 把 `2024年1-7月耗材采购明细.osheet` 重写成 7837 字节
（新 WPS 压缩格式，doc id 也不同）。逐格比对：**977 格、0 缺失、0 多余、0 值差异** ——
同一份数据的另一种序列化，**无损失**。

**处置**：保留 Drive 的版本（避免反复同步冲突），字节级重建件留本地备份。
**但要知道**：NAS 上"刚装回去的文件"可能已经不是你写的那份了，交付前要按 mtime 复核。

### 6.3 Drive 仓库里可能存的是**更新版本**

21 个两路都有的文件里有 2 个，Drive 的 HEAD 比雕刻原件大（40641 vs 6667）。
**id 相同、内容不同**。所以 `ver`（git commit id）也会不同 ——
重建件的 `ver` 用 Drive 的 master，雕刻原件用盘上的那个。

### 6.4 Windows → NAS 传文件的两个坑

- **CRLF**：Python 在 Windows 上写文件带 `\r\n`，传到 NAS 后路径多一个 `\r`，
  `cp` **静默失败**（报"文件不存在"）。NAS 端先 `tr -d '\r'`。
- **二进制别用 `$(...)`**：bash 的命令替换会吞掉 NUL 字节，tar/文件会损坏。
  用 `push_bin.sh` / `pull_bin.sh`（远端 `sh -c 'cat > 文件'`，stdin 直通）。

---

## 7. 下一步能接着做的

1. **剩下的 16 个**：Drive 的 **git 历史**里存着多个历史版本 ——
   逐个 commit 算 `text/` 的容器长度，对 orig_len 就能找到对应版本。
2. **其它格式**：`.xlsx` 306 / `.docx` 87 / `.pdf` 459 / `.zip` 180 ——
   Drive 重建对容器型文档通用，但**不是 `.osheet` 的话容器格式不同**，
   要先拿一个已知原件摸清结构，不能直接套 `rebuild_exact.py`。
3. **图片**（5000+）：没有 Drive 明文，走的是另一条路。

---

## 7.1 追剩下的文件：四条新路线（2026-10-31 第三批定型）

前两批做完还剩 16 个 `.osheet`。第三批指定恢复其中 2 个时，验证出下面四条**通用**打法，
顺序照着走，命中率最高。

### 路线 ①：`.osheet` 有**两种形态**，长度匹配必须两种都算

| 形态 | 特征 | 谁产出 |
|---|---|---|
| **不压缩形态** | 条目 tag=0x02，载荷就是 Drive 明文原始字节 | Drive 重建件、雕刻出的老件（schema 值 2 字节，头部 120 字节） |
| **WPS 压缩形态** | 条目 tag=0x05、名字带 `@C_` 前缀、载荷是 zlib；`schema` 值 3 字节，头部 121 字节 | **用户自己在 WPS 里保存的原件**（多数） |

勒索时被加密的原件**两种都有可能**。只按不压缩形态算长度，会漏掉全部 WPS 原生件。
`drv_survey2.py` 一次把两种形态、zlib level 6/7/8/9 的长度全算出来。

### 路线 ②：雕刻索引**按 `id` 反查**（本轮最有效的一招）

容器头里有 `id` 字段，值 = **Drive 仓库目录名**（`<uid>_<docid>.sh`）。

```bash
# 1) 从雕刻索引提取每个命中容器的 id（读 512 字节解析头部，极快）
python3 carve_id_dump.py logs/w*.log > ids.tsv
#    → 实测 14270 个地址 → 6861 个可解析容器 → 68 个不同 id

# 2) 关键词搜 Drive 全部 git 对象，定位"这个文档是哪个仓库"
python3 drv_grep.py 授权 未付款

# 3) 按 id 把盘上该文档的**所有历史版本**读出来，按长度筛
python3 carve_probe.py --id <uid>_<docid>.sh \
        --logs logs/w*.log --expect 4340 --save /tmp/hit
```

> 实测：授权信息那个文档盘上有 **346 个历史版本**（4316 ~ 54196325 字节），
> 其中长度正好等于 orig_len 的那一个就是被加密的原件。
> **比按长度盲猜准得多**——同一文档在盘上留了几百个版本，长度匹配是唯一可靠的筛子。

### 路线 ③：群晖**回收站**和 `@sharesnap` 快照是路线 C 的金矿

```bash
find /volume1 -path '*#recycle*' -type f -name '*.osheet' -printf '%s %p\n'
find /volume1/@sharesnap/ -type f -name '*.osheet' -printf '%s %p\n'
```

2026-10-31 实测：某团队共享的回收站里躺着 3 份勒索前的**明文原件**
（一份欠款明细 48104 字节、一份年度采购明细 839108 字节、一份上新日志 192496 字节），
`@sharesnap` 快照里还有一份同样内容的副本。**找原件前先扫这两个地方。**

注意：DSM 的回收站是 **per-share / per-home** 的
（`/volume1/<共享>/#recycle/`、`/volume1/homes/<用户>/#recycle/`），不是 `/volume1/#recycle/`。

### 路线 ④：Drive 仓库全量盘点（`drv_survey.py` / `drv_survey2.py`）

166 个仓库里 **113 个有多个 commit**（2~4 个），历史 commit 里往往藏着勒索前的旧版本。
按"每个 commit × 两种形态"算长度，对 orig_len 即可命中（本轮 `26.4月清仓明细` 就是这么找到的）。

### ⚠️ 交付时必须说清的口径

**勒索时的 orig_len 与「最后一次 Drive 同步」的版本可能差几十字节** —— 说明用户本地改过但没同步。
这种情况下能拿到的最好结果是 Drive/回收站版本：

| 情况 | 能拿到的 | 怎么说 |
|---|---|---|
| 盘上有长度 == orig_len 的原件 | 字节级精确原件 | 可以说"精确恢复" |
| 只有 Drive/回收站版本，差几十~几千字节 | 完整内容，**较早版本** | 必须说"比被加密的那份少一点，差 N 字节"，并指出差在哪（通常少几行） |

同一个文档经常同时存在三种形态（Drive 版、回收站版、雕刻版），**先比 SHA-256**：
回收站版与雕刻版同 SHA 时，说明盘上那份就是原封不动的历史版本。

---

## 8. 脚本的输入路径（下一轮照着准备）

工作目录（任意路径）下按下面布局放好，脚本就能直接跑：

| 路径 | 内容 | 谁来准备 |
|---|---|---|
| `claim3/target_*.osheet` | 至少一个**已知正常的** `.osheet`（当重建模板） | `claim_fast.py` 认领，或手工放 |
| `claim/claim_all/target_*.osheet` | 批量认领出的字节级精确原件 | `claim_fast.py --len ... --label ...` |
| `drive/x56/drive56/<repo>/text/` | 每个 Drive 仓库的明文条目 | 从 NAS `@SynoDrive/office/<repo>/version/text` 复制 |
| `drive/x56/drive56/<repo>/master` | 对应的 git commit id（= 容器的 ver） | 同上，从 `version/.git/refs/heads/master` |
| `drive/drive_index.tsv` | `仓库名\tver\t条目文件名\t字节数` | NAS 上遍历 `version/text/` 生成 |
| `_osheet_len_map.tsv` | `orig_len\ttag\t密文全路径` | 本地扫 `all_cipher.tsv` 筛出 `.osheet` |
| `_batch_plan.json` | 74 个目标的映射计划 | `plan_batch.py` |
| `_drive_map.json` | 长度+uid 匹配出的仓库映射 | `export_map.py` |

顺序：`match_drive_all.py`（验证长度公式 + 出匹配）→ `plan_batch.py` →
`export_map.py` → `final_pipeline2.py`（重建+交叉验证+对证+组装）→
`verify_batch.py`（校验+转 xlsx+全量回读）。

> 脚本里少数路径是写死的（如 `claim3/`、`drive/x56/drive56`），
> 换工作目录时改脚本顶部的常量，或按本表建同名的软链接/目录。
