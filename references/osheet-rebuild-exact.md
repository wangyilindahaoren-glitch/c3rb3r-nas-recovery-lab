# .osheet 字节级精确重建（2026-11-03 实测定型）

**这套方法能把 Synology Drive 的 Git 明文重建成与 WPS 原件**逐字节相同**的 `.osheet` 容器。
2026-11-03 在 `2025年产品采购明细.osheet` 上验证通过：
用 Drive 明文重建的容器与回收站里那份**真实存在的**文件 SHA-256 完全相同。

---

## 1. 什么时候用

- 目标是 `.osheet`（WPS OSheet），mode=`0x24` 整文件加密，`plan` 判 `no_candidate`（路线 A 死路）；
- 但盘上**存在该文档的 Drive 仓库**（`/volume1/@SynoDrive/office/<uid>_<docid>.sh/`），
  里面有勒索前的明文 blob；
- 需要**确认到底是哪个 Drive 版本**才是被加密的那一份（`orig_len` 长度匹配法，见第 5 节）。

---

## 2. 容器格式（字节级，实测确认）

```
文件 = 头部 + 条目链 + 尾段

头部 = magic(78 00 00 00)
     + TLV(schema)  tag=0x12 key="schema\0" len=u32 值=3 字节
     + 0x00 0x00 0x00            ← 对齐填充（3 个 0，别漏）
     + TLV(enc)     tag=0x08 key="enc\0"    len=u8  值=0 字节
     + 0x00
     + TLV(id)      tag=0x02 key="id\0"     len=u32 值="<uid>_<docid>.sh"+NUL
     + 0x00
     + TLV(ver)     tag=0x02 key="ver\0"    len=u32 值=git commit id+NUL（41 字节）
     + 0x00

条目 = [u32 X][u8 0x05]["@C_<name>\0"][u32 clen][0x00][zlib 数据][0x00]
       X    = 4 + 1 + len("@C_"+name+"\0") + 4 + 1 + clen + 1
       clen = **压缩后**的字节数
       压缩 = zlib level 9 / 默认策略 / wbits 15
              （Python `zlib.compress(data, 9)` 与 WPS 输出**逐字节相同**）
       条目顺序 = 条目名字母序（text/* 在前），metatext 最后

尾段 = [u32 11][0x08]["end\0"][0x01 0x00]
```

**头部解析的坑**：schema 后面是 **3 个** 0x00 填充，不是 1 个。
按 `osheet_strict.py` 的写法循环跳 0x00 会自然吃掉，但**手工拼头部时必须补齐**。

### metatext 条目（唯一特例）

```
[u32 X][0x05]["@C_metatext\0"][u32 20][0x00][zlib(b'{"ntype":37}') 20 字节]
      [0x02]["metatext\0"][u32 5]["null\0"][0x00]
X = 62（固定，与内容无关）
```

即：压缩条目后面**还跟一段未压缩副本**（载荷是 `null\0`），末尾**多一个** 0x00 条目结束符。
少写这个 0x00 → 长度差 1；多写 0x11 → 字节差 1 但长度对——**两者都试过**，
只有按上面的字节序列才既长度对又逐字节相同。

---

## 3. 校准：先证明配方对，再重建目标

**不要直接重建交付件**。先找一个**真实存在的** `.osheet`（回收站 / 快照 / 备份里），
用它的 Drive 仓库重建，**必须逐字节相同**（SHA-256 一致）才能继续：

```bash
python3 scripts/osheet_rebuild_from_drive.py --selftest <真实件> <office目录>
# 期望：commit xxxxx  重建 839108 字节  逐字节一致 ✓
```

2026-11-03 实测校准结果：
`rebuild(1026 仓库 @751070a8)` 与回收站里的 `2025年产品采购明细.osheet`
**SHA-256 完全相同**（`eb1501a3…`）。这才证明 zlib 参数、条目封装、metatext 尾段、
头部模板全对。

---

## 4. 重建

```bash
# 先列出每个 commit 能重建出多长的容器
python3 scripts/osheet_rebuild_from_drive.py --survey <office目录> \
        --template <一个已知正常的 .osheet>

# 重建指定 commit
python3 scripts/osheet_rebuild_from_drive.py \
        --repo <office目录> --ver <commit> --template <模板> \
        --out <输出.osheet> --expect-len 839115 [--exclude text/attachment]
```

`--exclude` 用来去掉 **Drive 侧有、本地容器没有**的条目（实测最常见的是 `text/attachment`：
Drive 仓库里存了附件元数据，本地 WPS 文件的容器里不一定有这一条，
带上它重建会**多出 1200 多字节**）。

---

## 5. 认领：哪个 Drive 版本才是被加密的那一份？

**长度匹配法**（2026-11-03 首次使用，命中）：

勒索标记里的 `orig_len`（`footer[580:588]` u64 LE）是**被加密文件的原始大小**。
把 Drive 每个 commit 重建出来，看哪个的**长度恰好等于 `orig_len`** —— 那就是原件。

实测：目标 `orig_len = 839115`，Drive 两个仓库的现成版本都是 839108（差 7）。
逐个 blob 算「压缩长度增量」后发现：

| 条目 | 变体 | zlib9 长度 | 相对基准 |
|---|---|---:|---:|
| `text/index` | 三个变体之一 | 270 | **−1** |
| `text/style` | 12196 字节那个 | 2124 | **+8** |
| 其余 13 条 | — | — | 0 |

**−1 + 8 = +7 → 长度正好等于 orig_len**，而且这个组合**唯一**
（其它条目变体的增量都是 −40 ~ −77，凑不出 +7）。

> ⚠️ **前提**：`orig_len` 必须是**原始文件大小**。2026-11-03 用同共享的另一个文件
> 做了对照实验（`2026年耗材采购明细截止到2026-8-20.osheet`：
> `orig_len = 5495`，盘上明文副本 `5495` 字节，**差 0**）——证明 `orig_len` 精确，
> 那个 +7 是真的内容差异，不是记账误差。
> **做长度匹配前，先在同目录找一个「密文 + 同名明文副本」的对照样本验一下这个不变量。**

### 判定 id / ver 字段

`id` = `<uid>_<docid>.sh`（office 目录名），`ver` = git commit id。
两者长度固定（id 35 字节含 NUL、ver 41 字节含 NUL），**不影响总长度**，
但影响"是否字节级精确"。判断依据：
内容来自哪个 commit 的 blob 序列，就配哪个 commit 的 ver；
id 看内容属于哪条血缘（同一文档在多人 Drive 里有各自的 uid）。

---

## 6. 坑：xlsx 的换行符会被 openpyxl 弄丢

`osheet_to_xlsx.py` 转出来的 xlsx，**源里用裸 `\r` 分隔的单元格**会变成 `\n`：

- openpyxl 写单元格时把 `\n` 转成 `\r\n`；
- XML 解析器又把**字面** CRLF 归一化成 LF；
- 于是源里的裸 `\r` 被吃掉。

2026-11-03 实测：80,718 格全量比对，**只有 1 格**不符（私账支付 J2712，10 行订单号用 `\r` 分隔）。

**修法**：把失真单元格的 `<t>` 内容里的 CR/LF 全换成数字实体
（`&#13;` / `&#10;`），XML 解析器就原样还原：

```bash
python3 scripts/osheet_fix_crlf.py <源.osheet> <生成的.xlsx>
```

⚠️ **不要无脑把所有 CR 都转义**——那样 LF-only 的格子反而会多出 `\r`
（实测把 1 处不符变成 544 处不符）。**只修真正失真的那几格**。

---

## 7. 本次实战数字（2026-11-03）

| 项 | 值 |
|---|---|
| 目标 | `/volume1/<团队共享>/采购相关/2025年采购明细/2025年产品采购明细.osheet.L0CK3D` |
| 密文 | 839,703 字节，mode `0x24`，enc `0x20`，marker `<marker>` |
| `orig_len` | **839,115** |
| 路线 A | `plan` 判 `no_candidate`（整文件被重写成一个新 extent），**死路** |
| 明文来源 | Synology Drive 仓库 `<uid>_<docid>.sh` 的 HEAD commit |
| 重建长度 | **839,115 —— 与 orig_len 分毫不差** |
| 重建件 SHA-256 | `926de851696c579968c6cfef51829ec4e32206625ab4e516b24f3408767b2e32` |
| 工作表 | 6 个（私账支付 / 对公支付 / 某分公司零星采购 / 到货相关退货明细 / 客服反馈售后待补发 / 垫付过账记录） |
| 有值格 | 80,715 |
| 全量逐格校验 | 80,718 / 80,718 一致，0 不符 |

**同时存在的两个"次优"副本**（回收站里，839,108 字节，各差 7 字节）：
它们是同一文档的**旧版本**（`text/index` 的 `ver` 7402、`text/style` 12147 字节）。
说明 **7 字节差异 = 一次版本更新**，不是数据缺失。

---

## 8. 相关脚本

| 脚本 | 位置 | 作用 |
|---|---|---|
| `osheet_rebuild_from_drive.py` | 本地 / NAS | Drive 明文 → 逐字节精确 .osheet；含 `--selftest` / `--survey` |
| `osheet_fix_crlf.py` | 本地 | 修 xlsx 里被 openpyxl 弄丢的裸 CR |
| `drive_repo_probe.py` | NAS | 解析一个 .osheet 的头部与条目，并和 Drive 仓库逐条比对 |
| `drive_grep_all.py` | NAS | 在**全部** Drive 仓库的 git 对象里搜关键字（找文档用） |
| `osheet_to_xlsx.py` / `verify_osheet_xlsx.py` | 本地 | 转 xlsx / 全量逐格回读校验 |
| `osheet_strict.py` / `osheet_check.py` | 本地 / NAS | 容器结构严格校验 |
