#!/bin/sh
# scan.sh —— 自愈式全量扫描监督器（NAS 端，setsid 启动）
#
# 每个 shard 一个子循环：worker 退出后自动重启，靠 state/shardN.done 断点续扫。
#
# 并发路数别照抄直觉，按盘的类型定（2026-10-31 RAID1 机械盘实测）：
#   单路 258 MB/s | 4 路聚合 906 MB/s | 20 路反而掉到 341 MB/s（磁头互踩）
#   用 2~4 路，既快又给 NAS 日常访问留出带宽；20 路会把 NAS 拖到卡死。
#   可用环境变量覆盖：C3R_SHARDS=4 C3R_NICE=19 sh scan.sh
#
# DSM 没有 ionice，有的话用 -c 3 把 IO 优先级压到最低，让业务访问优先。
cd /tmp/c3r_carve || exit 1
SIGS="${C3R_SIGS:-7800000012736368656d610003000000,7800000012736368656d610002000000}"
N="${C3R_SHARDS:-4}"
NICE="${C3R_NICE:-19}"
mkdir -p state logs

if command -v ionice >/dev/null 2>&1; then
  ION=ionice
elif [ -x /usr/bin/ionice ]; then
  ION=/usr/bin/ionice
else
  ION=""
fi

run_shard() {
  i=$1
  while true; do
    if [ -n "$ION" ]; then
      $ION -c 3 nice -n "$NICE" python3 carve_worker.py /tmp/c3r_carve/logs \
        --sig-hex "$SIGS" --shard "$i" --shards "$N" \
        --state "/tmp/c3r_carve/state/shard$i.done" >> "/tmp/c3r_carve/logs/w$i.log" 2>&1
    else
      nice -n "$NICE" python3 carve_worker.py /tmp/c3r_carve/logs \
        --sig-hex "$SIGS" --shard "$i" --shards "$N" \
        --state "/tmp/c3r_carve/state/shard$i.done" >> "/tmp/c3r_carve/logs/w$i.log" 2>&1
    fi
    if grep -q "^COMPLETE$" "/tmp/c3r_carve/state/shard$i.done" 2>/dev/null; then
      echo "[$(date +%H:%M:%S)] shard $i 完成" >> /tmp/c3r_carve/logs/supervisor.log
      break
    fi
    echo "[$(date +%H:%M:%S)] shard $i 异常退出，自动续扫" >> /tmp/c3r_carve/logs/supervisor.log
    sleep 5
  done
}

for i in $(seq 0 $((N - 1))); do
  run_shard "$i" &
  sleep 2
done
wait
touch /tmp/c3r_carve/ALLDONE
echo "[$(date +%H:%M:%S)] 全部 shard 完成" >> /tmp/c3r_carve/logs/supervisor.log
