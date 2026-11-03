# 在 NAS 上执行与传回数据

## 基本模式：脚本走 stdin，数据走 stdout

NAS 上不落任何脚本文件，作业中的临时文件只放在 `/tmp`，退出时删除。输出是 gzip、tar 等二进制流，直接重定向到本地文件或通过管道交给本地程序。

```bash
# 1) 生成作业（本地）
python scripts/mkjob.py -- env /volume1 > job.sh
python scripts/mkjob.py --list paths.txt -- extract @LIST --g 4842040 > job.sh

# 2a) 普通用户 + sudo 需要密码：第一行喂给 sudo -S，其余交给 sh -s
{ printf '%s\n' "$SUDO_PW"; cat job.sh; } | ssh -o ServerAliveInterval=30 USER@NAS "sudo -S -p '' sh -s" > out.bin

# 2b) root 能直接登录，或 sudo 免密
cat job.sh | ssh root@NAS "sh -s" > out.bin

# 3) 导出时边传边解包，不落中间 tar 文件
{ printf '%s\n' "$SUDO_PW"; cat job.sh; } | ssh USER@NAS "sudo -S -p '' sh -s" | python scripts/c3r_local.py extract - OUT_DIR
```

DSM 的 SSH 需要先在"控制面板 → 终端机和 SNMP"里开启，而且只有管理员组成员能登录。群晖默认禁止 root 用密码登录，常见做法是用管理员账号登录后 `sudo`。

### 密码怎么传

- 交互运行时直接输入即可。
- 需要自动化（例如由 AI agent 执行）时，用 `SSH_ASKPASS` 配合 `SSH_ASKPASS_REQUIRE=force`（OpenSSH 8.4 以上）。askpass 程序从环境变量读取密码，不要把密码写进任何文件或日志。
- 也可以在 NAS 上临时放一个公钥，事后删除。

## 各平台注意事项

- **Windows PowerShell 的管道会改写二进制数据（编码转换），绝对不要用它传 tar 或 gzip。** 改用 Git Bash、WSL，或者在 cmd 里用 `>` 直接重定向到文件。
- 在 Windows 上生成作业脚本必须保证 LF 换行。`mkjob.py` 已按字节输出，自己写脚本时要注意。
- Windows Git Bash 会把以 `/` 开头的参数（如 `/volume1/...`）改写成 `C:/Program Files/Git/...`：`mkjob.py` 会自动还原；自己写命令时设 `MSYS_NO_PATHCONV=1`。
- Windows 的 Git Bash 中，`tar` 遇到 `C:` 开头的路径会误认为远程主机，需要加 `--force-local`；也可以直接用 `c3r_local.py extract` 解包。
- Python 的 `-I` 隔离模式会忽略 `PYTHONIOENCODING`；在 Windows 中文控制台下打印特殊字符时，要用 `python -X utf8`。

### Windows 作为**目标机**（导出结果要落到 Windows 上）

导出方向是 NAS -> Windows，反向推流时有两个坑：

1. **Windows 自带的 ssh.exe 客户端可能卡死。** 实测某台目标机（装了安全软件）的 `ssh.exe` 连 `127.0.0.1` 都停在 KEX 阶段，但它的 sshd 完全正常。**不要因为目标机 ssh 客户端坏了就放弃这条路**——改成让 NAS 用自带的 Linux 版 ssh 主动连目标机：

   ```bash
   # 在 NAS 端（root）
   ssh -i /root/.c3r/push_key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
       -o BatchMode=yes user@WINDOWS_HOST "D:\\AI-Canvas\\python\\python.exe D:\\c3r\\recv.py D:\\c3r\\out"
   ```

   推流密钥放在 NAS 的 `/root/.c3r/push_key`，公钥装到目标机 Windows 的
   `C:\ProgramData\ssh\administrators_authorized_keys`（administrators 组成员读这个文件，
   不是 `~/.ssh/authorized_keys`），并 `icacls` 设为 Administrators + SYSTEM 完全控制。

2. **二进制流要绕过 cmd 的文本处理。** `more` 会分页并改写数据；PowerShell 管道会做编码转换。
   让目标机上的 Python 直接读 `sys.stdin.buffer` 写文件：

   ```python
   # recv.py
   import hashlib, sys
   h = hashlib.sha256(); n = 0
   with open(sys.argv[1], 'wb') as f:
       while True:
           b = sys.stdin.buffer.read(1 << 20)
           if not b: break
           f.write(b); h.update(b); n += len(b)
   print('received %d sha256=%s' % (n, h.hexdigest()))
   ```

   **上线前先用随机数据验一次通道**：`head -c 20000000 /dev/urandom | ssh ... recv.py`，比对两端 SHA。
   本例正是靠这个发现 `more` 会破坏数据，换成 Python 后 20MB SHA 完全一致。

3. **文件名含中文或空格时 scp 会失败**（`protocol error: filename does not match`）。
   用 tar 流传输：源端 `tarfile` 打成 `w|` 流，远端 `tar -x` 解开。

### Windows 作为**控制端**（从 Windows 驱动 NAS）

- Windows Git Bash 里 `ssh` 是可用的；但 `cmd.exe` 的转义地狱很常见。
  **稳妥做法是把逻辑写成 Python 脚本再上传执行**，用 `subprocess` 传参数数组，避免 shell 引号问题。
- 从 Windows 往 NAS 传脚本：base64 后一行行喂给远端 `base64 -d`，
  写成 `echo '<b64>' | base64 -d > /tmp/x.py`，别用嵌套 heredoc（会被多层转发截断）。
- `mkjob.py` 的作业走 stdin；密码行是 stdin 第一行，其余是脚本内容。

### Windows 本机环境（实测，2026-10-30）

这几条是这台机器的实际状况，换台机器不一定适用，但踩坑代价高：

- **`python` 命令是 Microsoft Store 占位符**，执行任何脚本都直接退出码 49（什么都不干）。真正的解释器在
  `C:\Users\Administrator\AppData\Local\Programs\Python\Python38\python.exe`（3.8.0），
  用 `py` 也能调到。本地脚本需要 3.8+。
- **`github.com` 网页不可达**，但 `api.github.com` 和 `codeload.github.com` 直连正常。
  拉 GitHub 仓库走这两条路：`api.github.com/repos/<owner>/<repo>/contents/<path>` 取文件，
  或 `codeload.github.com/<owner>/<repo>/tar.gz/refs/heads/main` 打包下载。
- **git 全局代理指向 `127.0.0.1:10808`，但该端口常常没有进程在监听**，
  于是 `git clone` 报 "Failed to connect to github.com port 443 via 127.0.0.1"。
  单次绕过：`git -c http.proxy= -c https.proxy= clone ...`；用 curl 时加 `--noproxy '*'`。
- **GitHub Code Search API 需要 `GITHUB_TOKEN`**（匿名只能搜仓库，不能搜代码内容）。
  没有 token 时退化为搜仓库 + 直接读 `contents` 接口。

## 不要这样做

- **不要把大段脚本或长路径列表放进 ssh 的命令行参数**：超过约 100KB 会被 sshd 或 sudo 直接断开（Connection reset）。
- **不要用 paramiko 等纯 Python SSH 库传几十 GB 的数据**：第一个案例中传到约 100MB 就停住了。原生 OpenSSH 能稳定达到 150MB/s 以上。
- **不要把输出写到 NAS 数据卷**：那会占用可能还存着旧数据的空闲空间。
- **不要用 `cpio` 复制 Windows 路径**：`cpio` 把反斜杠当普通字符，会产生 `xxx\D:\...` 这样的垃圾条目。用 `os.walk` + `shutil.copy2`，或 tar 流。

## 吞吐与耗时

- 实际速度通常受 NAS 机械盘随机读和本地目标盘写入限制，在 50–80MB/s 左右。300GB 大约要 1–2 小时。
- 写入前先测目标盘：`dd if=/dev/zero of=目标/test bs=1M count=200`。近满的 QLC 或无缓存 SATA SSD 可能低到不足 1MB/s。
- 本地验证（完整解码）是 CPU 密集型任务，要多进程并行，并且和下一批导出同时进行，避免串行等待。
- 估算空间时注意：exact 文件导出为原大小；推测文件可能有 prev、next 两个候选，空间翻倍。
