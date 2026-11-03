# Synology Drive 重建路线：勒索前明文还在 Git 仓库里

**2026-10-31 实战实测。** 这条路线的价值是：**分钟级**就能拿到大部分表格数据，
不用等雕刻（雕刻要几个小时）。雕刻只用来补它拿不到的两样东西——**字节级精确原件**和**图片附件**。

---

## 1. 适用前提

目标文件在 **Synology Drive 同步目录**下（群晖上通常在 `@appstore/SynologyDrive` 或
用户 home 里的 Drive 目录），且是**容器型文档**（`.osheet` / `.docx` / `.xlsx` / `.ods`）。

先确认 Drive 确实在同步这个目录，且**勒索发生时 Drive 服务是开着的**。

## 2. 核心发现

Synology Drive 的 office 集成把**每个 WPS 文档存成一个独立的 Git 仓库**：

```
<office目录>/
  <uid>_<docid>.sh/              ← 目录名 == .osheet 头部 id 字段（去掉结尾 NUL）
    version/
      .git/
        refs/heads/master        ← commit id == .osheet 头部 ver 字段
        objects/                 ← Git 对象，**无扩展名**
      text/<base64条目名>        ← 条目内容，**未压缩形式**
```

**勒索程序按扩展名加密，Git object 和 `version/text/` 都没有扩展名，因此没被加密。**
这些就是勒索前的完整明文。

由此得到三条对应关系（实测全部吻合）：

| Drive 侧 | `.osheet` 头部/结构侧 |
|---|---|
| office 目录名 `<uid>_<docid>.sh` | `id` 字段值（去掉结尾 NUL） |
| `version/.git/refs/heads/master` 的 commit id | `ver` 字段值 |
| `version/text/<base64条目名>` | 条目名 base64 解码后的名字（`text/sh_1` 等） |

**条目顺序 = 条目名字母序**（`text/*` 在前），裸名条目（`metatext`）在最后。

## 3. 操作流程

### 3.1 找到 office 目录并定位文档

Drive 的 office 目录位置因版本而异，常见在：

```
/volume1/@appstore/SynologyDrive/office/           （较新版本）
/volume1/homes/<用户>/@synoDrive/...                （按需同步版）
```

先用 `gitgrep.py` 在所有 Git 对象里搜文件名（勒索前的 `metatext.json` 里带 `title=文件名`）：

```bash
python3 gitgrep.py <office根目录> -- 竞店产品分析 加库存开预售明细表
```

输出是 `<目录名>\t<对象sha>\t<命中片段>`，据此确定要处理哪个 office 目录。

### 3.2 列出并取出条目内容

```bash
python3 gitls.py <office目录>/version/.git                       # 列出 HEAD 里所有文件
python3 gitls.py <office目录>/version/.git --cat <路径>          # 打印某个 blob（zlib 解压后）
python3 gitls.py <office目录>/version/.git --grep <子串>         # 在所有 blob 里找内容
```

`gitls.py` 是纯标准库实现，NAS 上没有 `git` 命令也能用。

### 3.3 用「模板法」重建容器

**不要手工推导头部字节布局**——schema=2 和 schema=3 两种头部差 1 字节，手写必错。
正确做法是**照抄一个已知正常 `.osheet` 的头部字节，只替换 `id` / `ver` 两个字段的值**：

```bash
python3 rebuild_from_drive.py <office目录> 输出.osheet \
        --template <一个已知正常的.osheet> --expect-len 162725
```

`--expect-len` 传勒索标记里的 `orig_len`，重建后长度对不上就说明重建错了，别往下走。

## 4. 重建件的性质（必须如实告知用户）

| 方面 | 雕刻出的原件 | Drive 重建件 |
|---|---|---|
| 字节级精确 | ✓ 是 | ✗ 是重新拼的 |
| 长度 == 勒索标记 orig_len | ✓ | ✗ 通常略大（多尾段、吞了附件区） |
| 条目名 | `@C_text/xxx`（压缩形式） | `text/xxx`（未压缩形式） |
| `text/attachment` 条目 | **没有**（落在加密区） | **有**（元数据完整） |
| 表格数据 | **完整** | ⚠️ **可能缺失工作表和格子**（见下） |
| 图片二进制 | 视是否命中而定 | **没有** |
| WPS 能否打开 | ✓ 一定能 | ⚠️ **不确定**（实测能打开的原生件都是压缩形式） |

**⚠️ 重建件不等于完整恢复。** 2026-10-31 实测：`价格调整记录表` 的重建件只有
**1 个工作表 / 4150 个非空格**，而雕刻出的精确原件有 **5 个工作表 / 17643 个非空格**——
**丢了 4 个工作表（竞店、大牌补货、询价、工作表8）和 1.3 万个格子**。
原因：Drive 里的明文分片本身就不全（被勒索切掉的部分拿不到），重建只是把拿到的拼起来。

**所以重建件只能当"临时交付"，最终交付必须换成雕刻出的精确原件。**
交付时**两个都要给**：

- `.xlsx` —— 用 `osheet_to_xlsx.py` 转换，用户日常直接双击用
- `.osheet` —— 换成雕刻出的精确原件（重建件改名 `*_Drive重建.osheet` 备份）

**换的时候必须对比工作表数量和格子数**，不能只看"两个文件都能打开"——
重建件缺的正是"打开看起来正常、但少了几张表"的那种，最容易被当成完整恢复交出去。

## 5. 图片附件：两边都没有

`text/attachment` 条目里是**元数据 JSON**（每个附件的 name/ext/size/thumb/thumb_size），
真正的图片二进制在**条目链之外的数据区**。实测：

- Drive 侧只同步 `version/text/` 的分片，**数据区不在 Drive 里** → 重建件没有图片
- 雕刻出的原件若命中的是加密前版本，也未必带数据区
- 两边全文扫 `PNG`/`JPG` 头都是 0 次 → 图片随密文一起被加密了

**唯一的希望是雕刻继续挖到带数据区的旧 extent。** 这是雕刻不可替代的价值，
汇报时要说清楚，别含糊成"附件已恢复"。

## 6. 与雕刻路线的分工与顺序

```
第 1 步（分钟级）  Drive 重建  →  立刻拿到大部分表格数据，先交付 .xlsx 给用户（**可能缺工作表**）
第 2 步（小时级）  雕刻        →  只为拿精确原件（换掉重建件）+ 图片附件
```

**不要一上来就雕刻。** 先做重建让用户有能用的文件，再挂雕刻后台跑。
雕刻要独占磁盘带宽（见 `references/carving.md` 的并发建议），先把用户需求满足掉。

## 7. 相关脚本

| 脚本 | 位置 | 作用 |
|---|---|---|
| `gitls.py` | 本地 / NAS | 纯标准库读 Synology Drive 的 Git 仓库（列出 / 取 blob / 搜索） |
| `gitgrep.py` | 本地 / NAS | 遍历所有 loose object 找含关键字的 blob（定位文档用） |
| `rebuild_from_drive.py` | 本地 | 用 Drive 的 Git 数据 + 模板法重建 `.osheet` 容器 |
| `office_index.py` / `office_report.py` | 本地 | 批量索引 office 目录、生成恢复报告 |
| `osheet_to_xlsx.py` | 本地（需 openpyxl） | 容器 → 多工作表 xlsx |
| `verify_osheet_xlsx.py` | 本地（需 openpyxl） | xlsx 与容器逐格回读比对 |

## 8. 本轮踩过的坑

- **重建件比原件长**是正常的（多追加一段尾段、把附件数据区吞进末条目）。
  **不要**因为长度对不上就判定重建失败——用 `orig_len` 只校验雕刻出的原件。
- **`osheet_check.py` 对重建件报 OK 不代表 WPS 能打开**，它只验条目链自洽。
- **抽样回读校验会漏**。`竞店产品分析` 抽样 400 格只命中 2 格（因为 3000 格全是
  空但带样式的格子），全量比对才发现 40 个真值格。**交付前对关键文件做全量逐格比对。**
- **画布尺寸 ≠ 数据量**。`rowCount=100, colCount=30` 是画布大小，实际有值的可能只有 4 行 40 格。
  汇报时要说"40 格有值"，不要说"3000 格"。
- **原文件没有 `text/attachment` 条目 ≠ 原文件没有图片**。看 `drawing` 字段和附件数据区。

---

## 9. Drive 重建升级：能做到**字节级精确**（2026-10-31 晚间实测）

上一节说的「重建件只能当临时交付」是**旧版 `rebuild_from_drive.py` 的水平**。
摸清容器格式后，可以做到**逐字节重建**——2026-10-31 在「2019-2024年采购明细」14 个文件上验证：

| 用 `rebuild_exact.py` 重建已知的雕刻原件 | 结果 |
|---|---|
| 7 个已知原件 | **6 个 SHA-256 与原件完全相同**；第 7 个只有头部 `ver` 字段不同，内容全同 |

**前提条件（实测）**：目标文档的条目是**未压缩**的（`tag=0x02`）。
现代 WPS 有些条目用 `tag=0x05`（zlib）——那种重建不出逐字节一致，只能重建内容等价的容器。

### 9.1 容器格式（实测确认）

```
文件 = 头部 + 条目链 + 尾段

头部 = magic(78 00 00 00)
     + TLV(schema) + 0x00        ← schema=2 版多一个填充字节；schema=3 版没有
     + TLV(enc)                  ← tag=0x08，长度是 u8
     + TLV(id)                   ← tag=0x02，值 = "<uid>_<docid>.sh" + NUL
     + TLV(ver)                  ← tag=0x02，值 = git commit id + NUL
     + 0x00

条目 = [u32 X][u8 0x02][name\0][u32 len][载荷][u8 0x00]
       X = 4 + 1 + len(name)+1 + 4 + len(载荷) + 1
       载荷 = Drive 的 version/text/<条目> 字节 + 一个 0x00
       条目顺序 = 名字母序（text/* 在前），裸名条目 metatext 在最后

尾段 = [u32 11][0x08]["end\0"][0x01 0x00]
```

⚠️ **两个必踩的坑**：
1. **`id` 的长度字段要跟着改**——不同文档的 `id` 长度不同（`<uid>_<docid>` 的 docid 长度不固定），
   照抄模板的长度字段会导致整体错位。`ver` 长度固定 41（含 NUL）。
2. **`metatext` 条目有两种形态**：短版 `{"ntype":37}`（13 字节）和长版
   `{"import_format":"drive","import_path":"link:...","ntype":37,"user":"..."}`（110 字节）。
   用错会导致重建件长度对不上 `orig_len`（实测差 97 字节）。
   长度对不上时，**从密文的明文区把真实的 metatext 载荷抠出来**（见 9.2）。

### 9.2 用密文的明文区独立对证（关键验证手段）

`mode 0x26` = 只加密开头 1 MiB，其余是**原件的原样字节**。所以：

```
密文内容部分 C0 = 密文去掉尾部 588 字节标记
重建件 R 与 C0 从某个偏移 E 起应当逐字节相同
E = 1048576（实测 5 个 0x26 文件全部是 1 MiB）
```

实测 5 个文件对证了 170KB~2MB 的字节，**全部一致**。
`mode 0x24`（整文件加密）没有明文区可对证，只能靠「长度 == orig_len」+ 方法自检。

**这条对证同时解决了 metatext 长度问题**：真实载荷就在密文明文区里。

### 9.3 标准流程（下一轮照做）

```bash
# 1) 先证明方法可靠：用已认领的精确原件做对照
python3 rebuild_exact.py --selftest <雕刻原件目录> <Drive根目录> <映射.json>
#    期望：绝大多数 "SHA 一致 ✓"。不一致的先查 metatext / id 长度字段

# 2) 重建目标文件
python3 rebuild_exact.py --drive <office目录>/version/text \
        --ver <office目录>/version/.git/refs/heads/master \
        --out <输出.osheet> --template <一个已知正常的 .osheet> \
        --expect-len <勒索标记里的 orig_len>

# 3) 对证（0x26 文件）
python3 verify_vs_cipher.py     # 比对重建件与密文明文区

# 4) 严格校验 + 转 xlsx + 全量逐格回读
python3 osheet_strict.py <件>
python3 osheet_to_xlsx.py <件> <件.xlsx>
python3 verify_osheet_xlsx.py <件> <件.xlsx> --sample 999999999
```

### 9.4 这条路线的价值

- **不限文件类型**：Drive 重建对任何**容器型文档**都适用，不只是 `.osheet`。
- **分钟级**，不用扫盘。
- 上一轮「运营相关」丢 4 张表的问题，根因是旧脚本 + 没做自检；
  本轮用「先自检再重建 + 逐个核对工作表数和有值格数」后，14 个文件**零缺表缺格**。

### 9.5 升级：可以做到**逐字节精确**（2026-11-03 实测）

§9 的 `rebuild_exact.py` 重建出的是**不压缩形态**（tag=0x02）的容器。
2026-11-03 把**压缩形态**（tag=0x05、`@C_` 前缀、zlib level 9）的配方也摸透了，
**重建结果与盘上真实存在的 `.osheet` SHA-256 完全相同**，
并能用勒索标记里的 `orig_len` **认领出"到底哪一版才是被加密的那一份"**。

完整格式、metatext 特例、`--exclude text/attachment`、xlsx 换行符坑 →
**见 `references/osheet-rebuild-exact.md`**，脚本 `scripts/osheet_rebuild_from_drive.py`。
