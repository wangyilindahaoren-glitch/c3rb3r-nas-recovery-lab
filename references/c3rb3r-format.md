# C3RB3R 加密格式与密钥路线

## 勒索信里那串 hex 是什么？（别再在这上面浪费时间）

`read-me3.txt` 是一封英文勒索信，里面给出 .onion 付款链接，链接 path 里带一长串 hex。
**那串 hex 不是密钥。** 2026-10-31 通过对公开样本的静态逆向已确认其构成：

```
note 里的 URL path = "%u"(设备/战役 ID)  +  hex( RSA-1024_OAEP(会话描述记录) )
                    =  "2202"            +  256 个 hex 字符
```

- 样本里 `.data` 段的常量 `0x89a = 2202`，与 URL 前 4 位**逐位吻合**。
- 那 256 个 hex 字符是**攻击者 RSA-1024 公钥的密文**。没有攻击者私钥，它就是随机数。
- **它本来就不含 AES 主密钥**——公开分析明确指出 1024 位 RSA 包裹的会话描述记录里没有直接复制主密钥。

所以：**不要试图用勒索信里的 hex 解密**。想拿主密钥只有三条路（见下）。

两个容易踩的细节：

- `footer[574]` 是**加密方式**字节：`0x20` = ChaCha8（可解）；**`0x21` = 诱饵模式**，写的是随机材料，**有密钥也解不开**。
- 样本里有**两把不同的 RSA 公钥**：RSA-1024 包裹"会话描述记录"→ 进勒索信 URL；
  RSA-2048 包裹会话主密钥 → 进**每个文件**的 `footer[48:304]`。
  **攻击者只要有 RSA-2048 私钥，就能从任意一个被加密文件的 `footer[48:304]` 恢复会话主密钥，一把解开全部文件。**

## 尾部（588 字节，追加在原文件之后）

| 偏移 | 长度 | 内容 |
|---|---|---|
| 0 | 48 | AES-256-CBC(会话主密钥, IV)( file_key32 ‖ nonce8 ‖ nonce8 )。两个 nonce 相同，可作为候选密钥的校验 |
| 48 | 520 | 其余密钥材料（公开分析未用到） |
| 568 | 4 | 会话标记（每次运行随机，例如 19a06d7a、883c0309、ce35a593） |
| 572 | 1 | 模式：0x24 整文件、0x25 分段、0x26 前缀 1MiB |
| 573 | 1 | 百分比参数（常见 0x0a） |
| 574 | 1 | 加密方式，0x20 = ChaCha8 |
| 575 | 1 | 保留 |
| 576 | 4 | 活动常量（例如 0x3A982） |
| 580 | 8 | 原始长度 N（u64 LE） |

数据加密方式：ChaCha8，32 字节密钥、8 字节 nonce、64 位小端计数器，每个文件从计数器 0 开始；各加密区段连续使用同一条密钥流。

模式大致按文件大小分档：约 1.05MB 以下用 0x24，约 1.05–5.24MB 用 0x26，更大的用 0x25。但 `.db`、`.db-wal` 等文件不论大小都用 0x24。

## 拿到主密钥后的解密链（供将来复用）

```
master = IV16 ‖ AES_key32   (48 字节，只在加密进程内存里)

每个文件 footer[0:48] = AES-256-CBC( key=master[16:48], iv=master[0:16] )
                        ( file_key32 ‖ nonce8 ‖ nonce8 )
   → 解出 file_key32 和 nonce8（两个 nonce 相等是筛选判据，不是认证）
   → ChaCha8(key=file_key32, nonce=nonce8, ctr=0) 解密数据区
```

ChaCha8 参数（实测核对）：轮数 **8**；state 布局 `key[0:12] | ctr[12:14] (u64 LE) | nonce[14:16]`，
**不是 IETF 的 12 字节 nonce 布局**；每文件计数器从 0 开始；单次 cipher 调用 ≤ 5 MiB；
末尾不足 64 字节仍消耗整块，剩余密钥流丢弃；未加密的间隙不推进计数器。

模式分段规则：`0x24` 整文件加密；`0x26` 只加密前 1 MiB；`0x25` 按百分比分段
（`pct` 决定每段长度 = `(size//100) * PF[pct]`，50% 时 5 段、其余 3 段）。

**判据**：nonce 相等、magic 正常都**不能**算恢复成功——必须拿整个文件比对
**原始文件的完整 SHA-256**（或 gzip CRC / tar 结构 / ZIP 完整性）。
另：公开案例里有 22 个大备份出现"两组 key 交错"，**重复 footer 不等于顺序解密两次**。

**开源实现**（拿来即用，不用自己写）：

- <https://github.com/soimy/c3rb3r-rescue> —— `scripts/decrypt_copy.py`（解密核心）、
  `recover_ram_keys.py`（内存扫描）、`recover_process_key.py`（进程内存）、`inspect_footer.py`（只读元数据）
- <https://github.com/Benjamin-zlt/c3rb3r-baremetal-rescue> —— `tools/c3dec.py`（格式+解密核心）、
  `extract_master.py`（按 profile 从内存镜像提 master）、`bulk_decrypt_local.py`（NAS 本地批量、断点续跑）
- <https://github.com/Creeper-OF/synology-c3rb3r-nas-case-study> —— **公开发布了真实恶意样本**
  （ZIP 密码 `infected`）与静态逆向报告，可用来核对字节级结论
- <https://github.com/hongjiahao371-pixel/L0CK3D-Recovery> —— 概率性抢救工具（非解密器）

## 什么情况下能拿到密钥

- 会话主密钥（IV16 ‖ AES-256 key32，共 48 字节）只在加密进程的内存里，没有落盘。
- 公开的成功案例都是**加密进程还活着时**取到的：先 `kill -STOP`，再按 `/proc/PID/maps` 导出可写映射，从全局指针取出主密钥。参见 soimy/c3rb3r-rescue、Benjamin-zlt/c3rb3r-baremetal-rescue。
- 进程退出后，它占用的内存页会被内核回收复用。第一个案例在加密结束约 13 小时后采集，做了以下扫描，**全部 0 命中**：
  - 物理内存（LiME，16.7GB）、4 个 zram（逻辑读取，共 9.8GB）、磁盘 swap md1（2GB）：每个字节偏移都当作 AES-256 密钥，去试解 3 个文件的密钥记录（第 3 块解密后两个 nonce 是否相等），有阳性对照；
  - 内存里全部 `expand 32-byte k` ChaCha 状态（194 个）× 4 种布局，对 77.8 万个加密文件头试解，看能否出现已知文件魔数；
  - ELF、UPX、libgld、勒索信、会话标记的上下文。
- 结论：进程已退出、事发已十几小时的情况下，内存路线可以放弃，直接走 Btrfs 旧 extent 路线。

**拿到主密钥后**，`per-file key` 虽然每个文件都不同（现场随机），但主密钥在整个 run 内固定，
**一把 master 能解开全部文件**（公开案例用一把 key 解了 84,500 个文件、约 940 GB）。
用上面的开源解密器批量跑即可，不必自己实现 ChaCha8。

## 快速判断加密进程是否还活着（NAS 上执行）

```
ls -l /proc/*/exe 2>/dev/null | grep -i deleted
ps -eo pid,etime,args | grep -v '\[' | sort -k2 | tail -50
```

公开案例中，进程以 `/usr/bin/libgld -n 308` 运行，二进制文件已从磁盘删除。发现后先 `kill -STOP <pid>`，不要 kill，然后立即导出该进程的内存。

## 入侵线索（供取证参考）

公开案例的入口包括：公网 IPv6 暴露 telnet、SSH、DSM 端口，路由器 IPv6 防火墙关闭；有的潜伏期长达 37 天，期间有反弹 shell。恢复完成后再系统性排查，不要因为排查耽误数据保全。
