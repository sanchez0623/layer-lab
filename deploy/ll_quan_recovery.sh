#!/bin/bash
# 手动收尾/补跑：跑完剩余 daily + minute5 + 重建顶层窗口。
# 与 ll_quan_nightly.sh 的区别：不重建 history 之外的滚动窗口前先补齐全部 scope。
# 用途：夜间 cron 中途被杀（OOM/断电）后，手动续跑到完成。
# 用法：bash /root/ll-quan/ll_quan_recovery.sh
set -u
PY=/usr/bin/python3.11
LOG=/root/ll-quan/recovery.log
SCRIPT=/root/ll-quan/ll_quan_nightly.py
POST=/root/post_quan_update.py
LOCK=/tmp/ll_quan_recovery.lock
# OOM 保护公共函数（容器 -800 / 更新器 1000）
. /root/ll-quan/oom_protect.sh

mkdir -p /root/ll-quan

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') 另一个 recovery 在跑，退出" | tee -a "$LOG"
  exit 0
fi
protect_oom

echo "===== $(date '+%F %T') recovery start =====" | tee -a "$LOG"
"$PY" "$SCRIPT" >> "$LOG" 2>&1
RC=$?
if [ "$RC" -eq 0 ]; then
  echo "----- post rebuild -----" | tee -a "$LOG"
  "$PY" "$POST" >> "$LOG" 2>&1
  POST_RC=$?
  echo "===== $(date '+%F %T') recovery done updater=$RC post=$POST_RC =====" | tee -a "$LOG"
  exit $POST_RC
fi
echo "===== $(date '+%F %T') recovery FAILED updater=$RC =====" | tee -a "$LOG"
exit $RC