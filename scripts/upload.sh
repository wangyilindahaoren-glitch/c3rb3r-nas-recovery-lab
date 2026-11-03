#!/bin/bash
# upload.sh <本地文件> <NAS 目标路径> [NAS地址]
#
# 把本地文件推到 NAS 上（DSM 的 SSH 默认没开 SFTP 子系统，
# 所以走 `sudo cat > 目标路径` 的管道方式）。
#
# 密码从环境变量 NAS_PASS 读，**不要把密码写进脚本或任何文件**：
#   export NAS_PASS='你的密码'
#   export NAS_HOST='192.168.1.x'         # 必填：NAS 地址
#   export NAS_PORT='4080'                 # 可选，默认 4080
#   export NAS_USER='admin'                # 可选，默认 admin
#
# 用法：bash upload.sh ./local.py /tmp/c3r/local.py
set -u

LOCAL_FILE="${1:?用法: upload.sh <本地文件> <NAS目标路径>}"
NAS_TARGET="${2:?用法: upload.sh <本地文件> <NAS目标路径>}"
NAS_HOST="${NAS_HOST:-}"
NAS_PORT="${NAS_PORT:-4080}"
NAS_USER="${NAS_USER:-admin}"

if [ -z "${NAS_PASS:-}" ]; then
  echo "[upload] 缺少环境变量 NAS_PASS，请先 export NAS_PASS=..." >&2
  exit 2
fi
if [ -z "$NAS_HOST" ]; then
  echo "[upload] 缺少环境变量 NAS_HOST，请先 export NAS_HOST=<NAS地址>" >&2
  exit 2
fi

# DSM 上 `sudo -S` 会从 stdin 读密码，所以把密码和数据拼在一起送进去。
{ printf '%s\n' "$NAS_PASS"; cat "$LOCAL_FILE"; } | \
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
      -p "$NAS_PORT" "$NAS_USER@$NAS_HOST" \
      "sudo -S -p '' sh -c 'cat > \"$NAS_TARGET\"'"
