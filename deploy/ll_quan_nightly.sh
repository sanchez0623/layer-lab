#!/bin/bash
# layer-lab · quan_parquet 每日增量更新（工作日 19:00 由 cron 调用）
# 用法：bash /root/ll-quan/ll_quan_nightly.sh
# - flock 防重入：上一轮没跑完时本轮直接退出，避免 baostock 并发被拉黑
# - 走宿主机 python3.11（已装 baostock + polars）
# - 日志追加到 /root/ll-quan/nightly.log

set -u
LOCK=/tmp/ll_quan_nightly.lock
LOG=/root/ll-quan/nightly.log
PY=/usr/bin/python3.11
SCRIPT=/root/ll-quan/ll_quan_nightly.py
POST=/root/post_quan_update.py

mkdir -p /root/ll-quan

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') 上一轮更新仍在进行，本轮跳过（防 baostock 并发）" >> "$LOG"
  exit 0
fi

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