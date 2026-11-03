# c3rb3r-nas-recovery-lab

> **作者**：wangyilin ｜ **版本**：v1.1.0 ｜ **首发日期**：2026-11-03 ｜ **License**：MIT
>
> 群晖 / Linux NAS（**Btrfs 与 ext4**）遭勒索后的**数据分诊与交付**工具箱。
> 密钥、备份、快照都没有的时候，判断哪些文件还能救、怎么救、怎么证明救回来了。

---

## ⚠️ 先看这里：这是扩展项目，不是从零写的

本项目基于 **[crt106/c3rb3r-btrfs-recovery](https://github.com/crt106/c3rb3r-btrfs-recovery)**（MIT）**大幅扩展**，
原作者是 [crt106](https://github.com/crt106)。**MIT 许可，版权声明保留在 `LICENSE` 里**。

| | 上游提供 | 本项目新增 |
|---|---|---|
| 路线 A · 旧 extent 还原 | ✅ 核心实现（`c3r_btrfs.py` / `c3r_local.py`） | 补充边界案例处理 |
| 路线 B · 明文提取 | ❌ | ✅ `plain_extract.py`（按 588 字节标记切明文区） |
| 路线 C · 找副本 | ❌ | ✅ `plain_index.py`（全卷明文索引）+ `carve_install.py` |
| 路线 D · 磁盘雕刻 | ❌ | ✅ 3 套扫描器（`.osheet` 强签名 / 通用魔数 / OLE2 正文认领） |
| **路线 E · ext4 专用** | ❌ | ✅ `ext4_probe.py`（**jbd2 日志导出** + 已删 inode）+ `jrn_match.py`（**在日志里找回被覆盖的原始字节**）+ `--fs ext4` / `--range` 扫描 |
| Synology Drive 重建 | ❌ | ✅ **字节级精确**重建 `.osheet`（SHA-256 与原件一致） |
| WPS OSheet 容器工具链 | ❌ | ✅ 解析 / 修复 / 严格校验 / 转 xlsx / 附件抠出（10 个脚本） |
| 交付验证 | 基础校验 | ✅ **两档闸门**（`validate_installed.py` / `deep_validate.py`） |
| 进度记录 | ❌ | ✅ `progress.py` —— 跨会话、跨天接着干 |
| 文档 | 7 篇 | ✅ **19 篇**（含两份脱敏实战实录 + ext4 路线与实录） |
| 脚本 | 3 个 | ✅ **81 个** |

**如果你只想要"按 Btrfs 旧 extent 逐字节还原"这一个能力**，直接用上游那个就够了，代码更少、依赖更干净。

**本项目的价值在于：把"能救回多少"这个问题拆成多条路线逐个试**
（Btrfs 主线 A/B/C/D + Drive 重建；**ext4 走 E 路线**：明文区 / 副本 / **jbd2 日志** / 已删 inode / 雕刻），
**并且对每一份交付出去的文件都给出可验证的证据。**
受害者在磁盘被写满、旧块被复用之前，每多试一条路线就多救回一批文件。

---

C3RB3R 用 ChaCha8 加密文件的一部分，在尾部追加 **588 字节标记**，再把文件改名成
`*.L0CK3D`。没有密钥就解不开密文——但**原始数据往往还在盘上**，
只是不再被文件系统引用了。这个工具箱就是用来把它们找回来的。

> **Btrfs 与 ext4 的前提不一样**：Btrfs 有写时复制，旧数据块只要还被引用就还在，能直接还原；
> **ext4 没有** —— 原地改写就把旧内容物理覆盖了，但它默认开的 **jbd2 日志**里常常留着
> 被覆盖前的旧数据块，配合未被加密的明文区能把文件拼回来（见 `references/ext4.md`）。

---

## 用 AI 助手的话（Claude Code / Kimi Code / OpenClaw / Cursor / Codex / DeepSeek / MiniMax / Cline / Windsurf …）

本目录是一个标准的 **Agent Skill**，目录结构是通用的：

```
c3rb3r-nas-recovery-lab/
├── SKILL.md        ← 主流程（frontmatter 里的 description 决定何时触发）
├── references/     ← 19 篇参考文档
└── scripts/        ← 81 个脚本
```

任何支持这个结构的 AI 助手都能用——**把整个目录放进它的 skill 目录就行**。

### 已验证的安装路径

| AI 助手 | skill 目录 | 怎么用 |
|---|---|---|
| **Claude Code / Claude Desktop** | `~/.claude/skills/c3rb3r-nas-recovery-lab/` | 装完直接说人话即可 |
| **Kimi Code** | `~/.kimi-code/skills/c3rb3r-nas-recovery-lab/` | 同上；Kimi 可挂 MiniMax / DeepSeek 等模型 provider，skill 机制一致 |
| **OpenClaw** | `~/.openclaw/skills/c3rb3r-nas-recovery-lab/` | 同上 |
| **通用 AGENTS 生态目录** | `~/.agents/skills/c3rb3r-nas-recovery-lab/` | 任何读 `~/.agents/skills/` 的 harness 都能用 |

一键安装（自动探测上面几个目录）：

```bash
bash install.sh                    # 装到所有探测到的 skill 目录
bash install.sh ~/.claude/skills   # 只装到指定目录
```

### 没有 skill 目录机制的工具

有些工具不走 skill 目录，得换个装法：

| 工具 | 装法 |
|---|---|
| **Cursor** | 放进 `.cursor/rules/c3rb3r.mdc`，frontmatter 写成 `description` + `alwaysApply: false`，正文用 `SKILL.md` 的内容；脚本路径改成绝对路径 |
| **Codex CLI** | 放进 `~/.codex/` 或仓库根的 `AGENTS.md`，让 agent 在需要时读取 `SKILL.md` |
| **DeepSeek / MiniMax 自建 harness** | 把 `SKILL.md` 的 frontmatter + 正文作为 system prompt 或首条指令；`scripts/`、`references/` 放在 harness 能读到的本地路径，正文里的相对路径相应改成绝对路径 |
| **Cline / Roo Code / Windsurf** | 同 Cursor，导出成 rules 文件 |
| **纯对话模型（网页版）** | 把 `SKILL.md` 整份贴进去，连同 `references/` 里要用的那几篇。脚本得自己执行、把输出贴回去 |

> **通用兜底**：只要 agent 能读文件、能执行 shell 命令，这套东西就能跑。
> skill 目录机制只是为了让它**自动触发**；不装也行——直接说
> "读一下 `SKILL.md`，按里面的流程恢复我的 NAS"，效果一样。

### 装完直接说人话

```
"我的群晖被 C3RB3R 勒索了，文件变成 .L0CK3D 了，帮我恢复"
```

`SKILL.md` 的 `description` 里写了完整触发词（NAS / 群晖 / 被勒索 / `.L0CK3D` /
`read-me3.txt` / C3RB3R / Cerber / 文件被加密想恢复 / 文件打不开但想看里面内容 /
文件被删了想找回 / 磁盘里还有残留文件 …），助手会**自动加载** `SKILL.md` 并按流程走：

分流 → 自检 → 定 G → 普查 → 选路线 → 导出 → 验证 → 装回 → 收尾

**你只需要准备**：SSH 能连上 NAS、sudo 权限、知道要恢复的目录路径。

助手会自己建 `PROGRESS.md` 记录进度，你随时问"现在到哪了"它都能答。

### 助手第一次会问你什么

加载完 skill，助手**不会直接开跑**，而是先跟你对齐三件事：

**① 它能做什么 / 不能做什么**

> 能做：从磁盘的旧数据块里把被改写过的文件**逐字节还原**（不需要密钥）；
> 把只被加密一部分的文件切出明文（表格能抢救工作表、PDF 能抢救页面）；
> 在备份目录、回收站、快照、别人的目录里找同一份文件的原件；
> 直接在磁盘原始数据区扫描，把还能认出来的文件切出来；
> 还原 WPS `.osheet` 表格并转成通用 `.xlsx`。
>
> 不能做：**不能解密**——被完整加密的文件没有密钥就是解不开；
> 不能造出已经彻底消失的数据；**Btrfs** 卷走主线，**ext4** 卷走另一条路线
> （明文区 / 找副本 / **jbd2 日志** / 已删 inode / 雕刻，见 `references/ext4.md`），**ZFS 不适用**。

**② 请你提供 4 项信息**

| # | 要什么 | 例子 |
|---|---|---|
| 1 | NAS 内网地址 + SSH 端口 | `<NAS_IP>`，端口 `22` 或 `4080` |
| 2 | SSH 账号 + 密码 | 账号一般是 `admin` |
| 3 | 要恢复的目录（**绝对路径**） | `/volume1/homes/xxx/某文件夹` |
| 4 | 恢复结果放在哪（**本地路径**） | `D:\c3r_recover` |

> ⚠️ **密码尽量别直接贴在对话里**（会进对话历史）。
> 助手会优先建议你自己在终端 SSH 上去跑、它给命令；
> 其次是设一个环境变量 `NAS_PASS`，由它用 `SSH_ASKPASS` 取，不落盘。

**③ 它拿到信息后会做什么**——建进度表 → 只读自检 → 盘点 → 分流选路线 →
导出 → 校验 → 验证通过的才放回原路径 → 出报告。

<details>
<summary>手动使用（不用 AI 助手）</summary>

按 [30 秒自检](#30-秒自检先确认这事能不能干) → [前置条件](#前置条件) →
[安装与连接](#安装与连接) → [快速开始](#快速开始) 一步步来，
完整决策树看 [`SKILL.md`](SKILL.md)。

</details>

---

## 30 秒自检：先确认这事能不能干

在动手之前，用这三条判断：

| 检查 | 命令（**NAS 端**，root） | 不通过怎么办 |
|---|---|---|
| 卷是什么文件系统 | `df -T /volume1` / `mount \| grep -E 'btrfs\|ext4'` | **ext4** → 走 ext4 路线（`references/ext4.md`，路线 A 不适用）；**ZFS** → 本工具不适用，见 `references/compatibility.md` |
| 加密程序还在跑吗 | `ls -l /proc/*/exe 2>/dev/null \| grep deleted` | **还在跑就 `kill -STOP` 冻结它**（别 kill、别重启），可能能从内存里取到密钥 |
| 卷空闲还剩多少 | `df -h /volume1` | **空闲 < 5%** → 旧数据块大概率已被复用，雕刻这条路基本白干，改走"找副本" |

> ⚠️ **恢复过程中不要删任何 `.L0CK3D` 文件**，哪怕已经找到原件了。
> 旧数据块靠密文文件"拽着"才没被回收，删掉密文等于销毁原件。

---

## 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| NAS | 群晖 DSM 6/7、OMV、Unraid 等 **Btrfs** 系统（主线）；**ext4** 系统走 `references/ext4.md` 里的 ext4 路线 | ZFS **不适用**（机制不同） |
| SSH | 能 SSH 上去，并能 `sudo` | DSM：控制面板 → 终端机和 SNMP → 启用 SSH |
| Python | NAS 端 3.6+（DSM 7 自带 3.8） | **只用标准库**，不用装任何东西 |
| 权限 | 恢复操作需要 **root** | 要读块设备、发 Btrfs ioctl |
| 本机 | 一个能 SSH 的机器 | 用来解包、校验、转格式；**结果写在这边，不要写回 NAS** |
| 磁盘 | 本机留够空间 | 第一个案例导出了 229.9 GB |

> 若某台设备没有 Python 3，或不方便联网装包，
> 可以把盘拆下来挂到 Linux 电脑上离线处理 —— 见 `references/compatibility.md`。

---

## 安装与连接

```bash
# 1) 把整个 scripts/ 目录传到 NAS（DSM 的 SSH 默认没开 SFTP 子系统，
#    所以走 ssh + sudo cat 的管道方式；细节见 references/transport.md）
export NAS_HOST='192.168.1.x' NAS_USER='admin' NAS_PASS='你的密码'
bash scripts/upload.sh ./scripts/c3r_btrfs.py /tmp/c3r/c3r_btrfs.py
#    （文件多的话，整个目录打包传更快）

# 2) SSH 上去，用 root 跑
ssh -p 22 admin@$NAS_HOST
sudo -i
cd /tmp/c3r && python3 c3r_btrfs.py preflight /volume1/homes/<用户>/某文件.osheet.L0CK3D
```

> DSM 会**自动封禁短时间内开太多 SSH 连接的 IP**。
> 一次只开一个连接；要拉多个文件就打包成 tar 一次拉。

---

## 快速开始

> ⚠️ **下面 ①~④ 全部在 NAS 端（root）跑**。
> 本地跑会直接报"这个脚本要在 NAS 上以 root 运行"——那是设计如此，不是坏了。

```bash
# ① 开工先落进度表（在本地跑，见 references/progress-log.md）
python3 scripts/progress.py --dir /work init \
        --target "某用户 home 的 624 个密文" --nas "群晖/DSM 7.2.1" --g <稍后填>

# ② 环境自检（NAS 端，root，只读，几分钟）
python3 scripts/c3r_btrfs.py preflight /volume1/homes/<用户>/某文件.osheet.L0CK3D
#    逐项 OK / WARN / FAIL；有 FAIL 就按 references/compatibility.md 处理，别硬着头皮往下走

# ③ 看卷信息（NAS 端）：CoW / NoCOW、快照代数
python3 scripts/c3r_btrfs.py env /volume1

# ④ 确定 G 值（NAS 端）：勒索时新写入的 Btrfs 代数
python3 scripts/c3r_btrfs.py gens /volume1/homes/<用户> --sample 3000
#    ⚠️ 不同目录要分别取 G，否则部分目录会 100% no_candidate

# ⑤ 全量普查（NAS 端，只读，输出 JSONL）
python3 scripts/c3r_btrfs.py survey /volume1/homes/<用户> --g <G> > survey.jsonl.gz

# ⑥ 看普查结果 → 决定走哪条路线
python3 scripts/c3r_local.py report survey.jsonl.gz --depth 4
```

完整流程（分流、导出、验证、装回、清理）见 [`SKILL.md`](SKILL.md)。

### 哪些脚本在哪跑

| 在哪跑 | 脚本 |
|---|---|
| **NAS 端（root）** | `c3r_btrfs.py` `carve_scan.py` `carve_gen.py` `carve_gen_hits.py` `carve_worker.py` `carve_claim.py` `carve_install.py` `plain_extract.py` `salvage_zip.py` `read_footers.py` `plain_index.py` `ole2_*.py` `restore_inplace.py` `fix_nas_acl.py` `validate_installed.py` `deep_validate.py` |
| **本地** | `c3r_local.py` `progress.py` `osheet_to_xlsx.py` `verify_osheet_xlsx.py` `osheet_fix_crlf.py` `rebuild_exact.py` `final_pipeline2.py` `xlsx_from_members.py` `mkjob.py` |
| **两边都行** | `osheet_repair.py` `osheet_check.py` `osheet_strict.py` `probe_modes.py` `pdf_rebuild.py` `pdf_validate.py` `pdf_xref_repair.py` |

---

## 恢复路线

| 路线 | 手段 | 适用 | 成本 |
|---|---|---|---|
| **A** | Btrfs 写时复制留下的**旧 extent** | 大文件（>1MB）、CoW 共享 | 分钟~小时 |
| **B** | 密文里**未被加密的那部分字节**仍是明文 | mode `0x25` / `0x26` | 分钟级 |
| **C** | 备份目录 / 快照 / 回收站 / 别人 home 里的**副本** | 全类型 | 分钟级，命中率常最高 |
| **D** | 块设备上**雕刻**已释放的旧 extent | mode `0x24` 整文件加密的小文件 | 小时级 |
| **E** | **ext4 专用**：**jbd2 日志**里的旧数据块 + 已删除 inode | ext4 卷 | 分钟~小时 |

外加一条 **Synology Drive 重建**路线：Drive 把每个 WPS 文档存成独立 Git 仓库，
`.git/objects` 里的 blob 没有扩展名、**不会被勒索程序加密**，
可以据此**逐字节精确**重建 `.osheet` 容器（实测 SHA-256 与原件完全相同）。

**路线 A 优先，A 失败后走 B，C/D 兜底。** 但**顺序要按目标文件的状态分流**——
被整文件加密的小文件不要再指望旧 extent，直接扫数据区更快。

> **ext4 用户看这里** ⚠️
> ext4 **没有写时复制**，勒索程序原地改写就把旧内容物理覆盖了 ——
> **路线 A 原理上不成立**（`preflight` 会直接 FAIL，这不是工具的问题）。
> 但路线 **B / C / D 照常可用**，还多两条 ext4 独有的路：
> **jbd2 日志**（ext4 默认开日志，日志里常常留着被覆盖前的旧内容，**越早导越有戏**）
> 和**已删除 inode**（`debugfs lsdel`）。
> 完整执行顺序、命令与预期见 **[`references/ext4.md`](references/ext4.md)**。

---

## 安全红线 ⚠️

1. **不删、不移动、不覆盖任何 `.L0CK3D` 文件**，直到用户验收确认。
   旧 extent 靠密文文件"拽着"才没被释放，删掉密文等于销毁原件。
2. **不做 defrag、balance、`btrfs check --repair`、rescue**，不跑来源不明的"解密器"。
   defrag 会重写 extent。
3. **恢复期间尽量少写盘**：暂停 NVR 录像、下载、索引、同步任务。
4. **结果写到 NAS 以外的盘**，绝不回写原目录（装回除外，且只新增、不覆盖）。
5. **NAS 端全程只读**。本工具箱的 NAS 端脚本只做查询、读文件、读块设备。
6. **汇报要诚实**：精确恢复 + 校验和证实 / 推测后解码通过 / 部分明文 / 失败，
   必须分开写。候选 ≠ 恢复成功。

---

## 目录结构

```
SKILL.md              主流程：分流、路线选择、验证、收尾清单
install.sh            一键装进 AI 助手的 skill 目录（Claude / Kimi / OpenClaw / …）
README.md             给人看的入门说明
CONTRIBUTING.md       贡献指南：提 issue / 提 PR / 脱敏要求 / 脚本红线
references/            19 篇详细文档（原理、踩坑、案例、操作手册）
scripts/               78 个 Python 脚本 + 3 个 shell 脚本
  c3r_btrfs.py         NAS 端核心：preflight / env / gens / survey / extract / csum / plan
  c3r_local.py         本地端：解包、校验（SHA / 解码 / 已知原件对照）、挑选候选
  mkjob.py             把脚本 + 路径列表打包成一个 stdin 作业
  progress.py          进度记录：开工建 PROGRESS.md、切阶段、记数字、收尾结项
  plain_extract.py     路线 B：按加密模式切出明文部分
  salvage_zip.py       从损坏的 xlsx/docx/zip 里抢救成员
  carve_*.py           路线 D：块设备雕刻扫描器 + 认领 + 装回
  ext4_probe.py        ext4 专用：卷探测 + jbd2 日志导出 + 已删 inode 清单
  jrn_match.py         ext4 专用：在 jbd2 日志里用明文指纹找回旧内容
  osheet_*.py          WPS OSheet 容器：解析 / 修复 / 校验 / 转 xlsx / 重建
  ole2_*.py            OLE2（.xls/.doc）容器解析与按正文认领
  validate_installed.py / deep_validate.py   交付前的"双击能不能打开"闸门
```

每个脚本都带独立 docstring 说明**在哪运行**（NAS 端 / 本地端）、需要什么依赖。

**依赖**：Python 3.6+ 标准库即可（NAS 端 DSM 自带 3.8）。
个别脚本可选 `Pillow` / `pillow-heif` / `ffmpeg`（图片音视频解码校验）、
`openpyxl`（OSheet → xlsx 转换与逐格回读校验）。

---

## 维护约定

**每轮开工必须先落 `PROGRESS.md`，每轮收尾必须回写 skill。**

```bash
P="python3 scripts/progress.py"
$P --dir /work init --target "..." --nas "..." --g 9324851   # 拿到参数后的第一条命令
$P --dir /work stage --name "8 路线D 雕刻"                    # 切阶段
$P --dir /work metric --name "已装回原件" --value 142         # 记数字
$P --dir /work status                                         # 换会话时读回
$P --dir /work close --result "救回 459/483"                   # 结项
```

进度表让"跨会话、跨天、跨人"的长任务接得上；
收尾回写 skill（第 14 步）让**这一轮的学费不白交**。
两者互补，都不能省。详见 [`references/progress-log.md`](references/progress-log.md)。

---

## 文档索引

| 文件 | 内容 |
|---|---|
| `references/principles.md` | **原理**：为什么旧数据还在盘上，Btrfs 快照与代数的规则 |
| `references/ext4.md` | **ext4 路线**：没有写时复制也能救 —— 明文区 / 副本 / **jbd2 日志** / 已删 inode / 雕刻 |
| `references/c3rb3r-format.md` | 加密格式：588 字节标记、mode 语义、密钥派生 |
| `references/plain-extract.md` | 路线 B 详解（动手前必读） |
| `references/osheet.md` | WPS OSheet 容器格式、修复、交付形态 |
| `references/osheet-rebuild-exact.md` | `.osheet` 字节级精确重建配方 |
| `references/carving.md` / `carve-generic.md` / `carve-osheet.md` | 路线 D 的三个变体 |
| `references/synology-drive.md` | Drive 重建路线 |
| `references/batch-osheet-recovery.md` | 成批恢复操作手册 |
| `references/compatibility.md` | 各系统（DSM 6/7 / OMV / Unraid）与不适用场景 |
| `references/transport.md` | Windows / macOS / Linux 的传输与执行姿势 |
| `references/pitfalls.md` | **踩过的坑**（现象 / 原因 / 处理），实战前先扫一遍 |
| `references/progress-log.md` | **进度记录规范**：每轮开工落 `PROGRESS.md`，收尾回写 skill |
| `references/case-*.md` | 三份实战/设计实录：两份脱敏实战 + **ext4 路线设计与自检** |
| `CONTRIBUTING.md` | **贡献指南**：怎么提 issue、怎么提 PR、脱敏要求、脚本红线 |

---

## 参与贡献

欢迎。这个项目里最值钱的贡献往往不是写代码，而是**"我在 XX 环境上跑，第 N 步报了这个错，我是怎么绕过去的"**。

- 提 issue / PR 前先看 **[CONTRIBUTING.md](CONTRIBUTING.md)**（含脱敏要求、脚本红线、提交前自查清单）
- GitHub 上已配好 **issue 模板**（bug 报告会自动收集 `preflight` 输出、NAS 类型、Python 版本）
- 提 issue 时**务必附上 `preflight` 输出**，能省掉一半来回

```bash
sudo python3 scripts/c3r_btrfs.py preflight /volume1/homes/<用户>/<某个>.L0CK3D
```

---

## 已知适用范围

- **适用**：Btrfs 卷（群晖 DSM 6/7、OMV、Unraid 等），Data 剖面为 single 或 RAID1 类。
- **ext4**：**路线 A 不适用**（没有写时复制），但**有专门的 ext4 路线**——
  明文区（路线 B）/ 找副本（路线 C）/ **jbd2 日志里的旧数据块** / 已删除 inode / 雕刻，
  完整步骤见 [`references/ext4.md`](references/ext4.md)。
- **不适用**：ZFS —— 机制不同，见 `references/compatibility.md`。
  QNAP 老卷多为 ext4，走上面的 ext4 路线。
- **无法适用**：文件已被整体覆盖且旧 extent 已被复用（卷空闲 < 5% 时大概率如此）。
  这时唯一出路是找**别的副本**（路线 C）。

---

## 免责声明

本工具箱用于**数据恢复**，不是解密器，无法解密已被勒索程序加密的内容。
所有操作默认只读、默认预览。执行删除/装回前请自行确认目标路径与备份。

## License

MIT，见 [LICENSE](LICENSE)。
