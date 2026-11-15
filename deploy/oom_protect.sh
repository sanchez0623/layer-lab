#!/bin/bash
# OOM 保护公共函数（被 ll_quan_nightly.sh / ll_quan_recovery.sh source）。
# 背景：宿主机仅 1.8G RAM，quan 更新器的 _flush_daily 每次 flush 都全表读
# existing daily（~630 万行），内存尖峰会触发 global OOM，可能挤死 layer-lab
# 容器后端（uvicorn）。双保险：
#   1) 容器进程 oom_score_adj = -800（几乎不被 OOM 选中）
#   2) 本更新器进程 oom_score_adj = 1000（优先被杀；可断点续跑，损失小）
# 容器侧的 -800 已在 compose.server.yaml 里原生声明（oom_score_adj），重启持久；
# 这里再对运行中进程兜底设一次（覆盖旧容器 / 未重建的场景）。

protect_oom() {
  # 容器：降低被 OOM 选中概率
  if command -v docker >/dev/null 2>&1; then
    local mainpid
    mainpid=$(docker inspect layer-lab --format '{{.State.Pid}}' 2>/dev/null || echo "")
    if [ -n "$mainpid" ] && [ "$mainpid" != "0" ]; then
      for p in $(docker top layer-lab -eo pid 2>/dev/null); do
        echo -800 > /proc/$p/oom_score_adj 2>/dev/null || true
      done
    fi
  fi
  # 自身 + 子进程（更新器）：优先被 OOM 杀
  echo 1000 > /proc/self/oom_score_adj 2>/dev/null || true
}