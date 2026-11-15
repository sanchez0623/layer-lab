#!/bin/bash
# layer-lab · quan_parquet 每日增量更新（工作日 19:00 由 cron 调用）
# 用法：bash /root/ll-quan/ll_quan_nightly.sh
# - flock 防重入：上一轮没跑完时本轮直接退出，避免 baostock 并发被拉黑
# - 走宿主机 python3.11（已装 baostock + polars）
# - OOM 保护：容器设 oom_score_adj=-800、本更新器设 1000，避免全表 flush
#   的内存尖峰挤死 layer-lab 容器（详见 oom_protect.sh）
# - 日志追加到 /root/ll-quan/nightly.log

set -u
LOCK=/tmp/ll_quan_nightly.lock
LOG=/root/ll-quan/nightly.log
PY=/usr/bin/python3.11
SCRIPT=/root/ll-quan/ll_quan_nightly.py
POST=/root/post_quan_update.py
# OOM 保护公共函数（容器 -800 / 更新器 1000）
. /root/ll-quan/oom_protect.sh

mkdir -p /root/ll-quan

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') 上一轮更新仍在进行，本轮跳过（防 baostock 并发）" >> "$LOG"
  exit 0
fi

# 防重入拿到锁后再设 OOM 保护（容器豁免 + 更新器优先被杀）
protect_oom

echo "===== $(date '+%Y-%m-%d %H:%M:%S') nightly start =====" >> "$LOG"
"$PY" "$SCRIPT" >> "$LOG" 2>&1
UPD_RC=$?

# 更新器跑完后重建顶层 30 天滚动窗口（容器只读挂的就是它）
if [ "$UPD_RC" -eq 0 ]; then
  echo "----- post: rebuild top-level rolling window -----" >> "$LOG"
  "$PY" "$POST" >> "$LOG" 2>&1
  POST_RC=$?
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') nightly done updater=$UPD_RC post=$POST_RC =====" >> "$LOG"
  exit $POST_RC
else
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') nightly FAILED updater=$UPD_RC（跳过 post） =====" >> "$LOG"
  exit $UPD_RC
fi