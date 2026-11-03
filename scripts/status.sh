#!/bin/sh
# status.sh —— NAS 端扫描状态采集：每 60 秒把进度写到 STATUS 文件
#
# ⚠️ 不要写成 `cat state/shard*.done | grep -c '^[0-9]'`：
# state 文件膨胀到几个 GB 时，那等于每分钟把几个 GB 读一遍，CPU 直接打满，
# ssh 进程会占掉整机 99% 的 CPU（2026-10-31 实测）。
# 正确做法是用 awk 一次遍历求和：每个 chunk 只取最后一次记录
# （offset 在 chunk 内单调递增，末条即最大值；chunk 扫完时 offset == 长度），
# 求和即真实已扫字节数。
#
# 用法：setsid sh status.sh > /dev/null 2>&1 &
TOTAL_BYTES="${C3R_TOTAL_BYTES:-0}"   # DATA chunk 逻辑总量，从 worker 日志首行取（必填，否则百分比无意义）
while true; do
  N=$(ps ax | grep -c "[c]arve_worker.py")
  R=$(for p in $(ps ax | grep "[c]arve_worker.py" | awk '{print $1}'); do
        awk '/^rchar/{print $2}' /proc/$p/io 2>/dev/null
      done | awk '{s+=$1} END{print s+0}')
  CB=$(cat /tmp/c3r_carve/state/shard*.done 2>/dev/null | \
       awk '{ last[$1] = $2 } END { s = 0; for (k in last) s += last[k]; print length(last) " " s }')
  C=$(echo "$CB" | cut -d' ' -f1)
  B=$(echo "$CB" | cut -d' ' -f2)
  H=$(cat /tmp/c3r_carve/logs/w*.log 2>/dev/null | grep -c '签名命中')
  if [ -f /tmp/c3r_carve/ALLDONE ]; then D=DONE; else D=RUNNING; fi
  PCT=$(awk -v b="${B:-0}" -v t="$TOTAL_BYTES" 'BEGIN{printf "%.2f", b*100/t}')
  printf "N=%s RCHAR=%s CHUNKS=%s BYTES=%s PCT=%s%% HITS=%s STATE=%s\n" \
    "$N" "$R" "${C:-0}" "${B:-0}" "$PCT" "$H" "$D" > /tmp/c3r_carve/STATUS
  [ -f /tmp/c3r_carve/ALLDONE ] && break
  sleep 60
done
