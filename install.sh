#!/usr/bin/env bash
# install.sh —— 把本 skill 装进 AI 助手的 skill 目录
#
# 用法：
#   bash install.sh                    # 装到所有探测到的 skill 目录
#   bash install.sh ~/.claude/skills   # 只装到指定目录
#   bash install.sh --dry              # 只看会装到哪，不动手
#
# 探测的目标（存在的才装）：
#   ~/.claude/skills      Claude Code / Claude Desktop
#   ~/.kimi-code/skills   Kimi Code（含 MiniMax / DeepSeek 等 provider）
#   ~/.openclaw/skills    OpenClaw
#   ~/.agents/skills      通用 AGENTS 生态目录
#
# 没有 skill 目录机制的工具（Cursor / Codex / Cline …）：
#   见 README.md「没有 skill 目录机制的工具」一节。
set -eu

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="c3rb3r-nas-recovery-lab"
HOME_DIR="${HOME:-/root}"
DRY=0

if [ "${1:-}" = "--dry" ]; then
  DRY=1
  shift
fi

if [ "$#" -ge 1 ]; then
  TARGETS="$1"
else
  TARGETS=""
  for c in "$HOME_DIR/.claude/skills" \
           "$HOME_DIR/.kimi-code/skills" \
           "$HOME_DIR/.openclaw/skills" \
           "$HOME_DIR/.agents/skills"; do
    [ -d "$c" ] && TARGETS="$TARGETS $c"
  done
fi

if [ -z "$(echo "$TARGETS" | tr -d ' ')" ]; then
  cat >&2 <<'EOF'
没有找到任何 skill 目录。

你可以手动指定一个：
    bash install.sh ~/.your-tool/skills

或者参考 README.md「没有 skill 目录机制的工具」一节，
把 SKILL.md 转成 rules / AGENTS.md / system prompt。
EOF
  exit 1
fi

echo "源目录: $SRC"
echo

for t in $TARGETS; do
  dst="$t/$NAME"
  echo "==> $dst"
  if [ "$DRY" = "1" ]; then
    echo "    (dry-run，不实际写入)"
    continue
  fi
  mkdir -p "$dst"
  # 复制，但排除 .git / __pycache__ / 本地现场数据
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
      --exclude 'PROGRESS.md' --exclude '*.L0CK3D' \
      "$SRC/" "$dst/"
  else
    # 没有 rsync 的兜底（macOS 自带、Linux 常常没有）
    rm -rf "$dst"
    mkdir -p "$dst"
    ( cd "$SRC" && tar cf - \
        --exclude=.git --exclude=__pycache__ --exclude='*.pyc' \
        --exclude=PROGRESS.md --exclude='*.L0CK3D' . ) | \
      ( cd "$dst" && tar xf - )
  fi
  n=$(find "$dst" -type f | wc -l | tr -d ' ')
  echo "    已安装 $n 个文件"
done

echo
echo "装完了。现在跟你的 AI 助手这么说："
echo
echo '    "我的群晖被 C3RB3R 勒索了，文件变成 .L0CK3D 了，帮我恢复"'
echo
echo "它会自动加载 SKILL.md，先跟你对齐参数，再按流程开工。"
