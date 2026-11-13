#!/usr/bin/env bash
# layer-lab 容器启动脚本（tini -g 作为 init，转发信号给整个进程组）
# 流程：后台起 uvicorn 监督循环 → 轮询 /api/health 就绪 → 前台起 nginx（作为常驻主进程）
# 监督循环：uvicorn 被 OOM/异常杀死后自动重启，避免 nginx 仍在前台但 /api 永久 502。
set -euo pipefail

cd /app/backend

UVICORN_HOST="${UVICORN_HOST:-127.0.0.1}"
UVICORN_PORT="${UVICORN_PORT:-8010}"

supervise_uvicorn() {
  while true; do
    echo "[start] 启动 uvicorn (app.main:app) on ${UVICORN_HOST}:${UVICORN_PORT} ..."
    uvicorn app.main:app --host "${UVICORN_HOST}" --port "${UVICORN_PORT}" --workers 1 &
    local pid=$!
    local code=0
    wait "$pid" || code=$?
    echo "[start] ⚠️ uvicorn 退出 (code=${code})，2 秒后重启..." >&2
    sleep 2
  done
}

cleanup() {
  echo "[start] 收到退出信号，停止服务..."
  kill -TERM "$SUPERVISOR_PID" 2>/dev/null || true
  wait "$SUPERVISOR_PID" 2>/dev/null || true
}
trap cleanup TERM INT

supervise_uvicorn &
SUPERVISOR_PID=$!

# 就绪探针：最多等 60s；uvicorn 由监督循环负责重启，这里不再因单次崩溃直接退出容器
echo "[start] 等待后端就绪..."
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${UVICORN_PORT}/api/health" >/dev/null 2>&1; then
    echo "[start] 后端就绪 (${i}s)"
    break
  fi
  if ! kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
    echo "[start] ⚠️ uvicorn 监督进程异常退出；nginx 仍会启动并返回 502" >&2
    break
  fi
  sleep 1
done

echo "[start] 前台启动 nginx (容器内 :3000) ..."
# 用 exec 让 nginx 成为脚本进程的替身，tini 直接管理它；uvicorn 监督循环在同进程组内由 tini -g 一并回收
exec nginx -g 'daemon off;'
