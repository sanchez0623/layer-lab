#!/usr/bin/env bash
# layer-lab 容器启动脚本（tini -g 作为 init，转发信号给整个进程组）
# 流程：后台起 uvicorn → 轮询 /api/health 就绪 → 前台起 nginx（作为常驻主进程）
set -euo pipefail

cd /app/backend

UVICORN_HOST="${UVICORN_HOST:-127.0.0.1}"
UVICORN_PORT="${UVICORN_PORT:-8010}"

echo "[start] 启动 uvicorn (app.main:app) on ${UVICORN_HOST}:${UVICORN_PORT} ..."
uvicorn app.main:app --host "${UVICORN_HOST}" --port "${UVICORN_PORT}" --workers 1 &
UVICORN_PID=$!

cleanup() {
  echo "[start] 收到退出信号，停止服务..."
  kill -TERM "$UVICORN_PID" 2>/dev/null || true
  wait "$UVICORN_PID" 2>/dev/null || true
}
trap cleanup TERM INT

# 就绪探针：最多等 60s，避免 nginx 先起导致首页能开但 /api 未就绪
echo "[start] 等待后端就绪..."
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${UVICORN_PORT}/api/health" >/dev/null 2>&1; then
    echo "[start] 后端就绪 (${i}s)"
    break
  fi
  if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
    echo "[start] ❌ uvicorn 启动失败，退出" >&2
    exit 1
  fi
  sleep 1
done

echo "[start] 前台启动 nginx (容器内 :3000) ..."
# 用 exec 让 nginx 成为脚本进程的替身，tini 直接管理它；uvicorn 在同进程组内由 tini -g 一并回收
exec nginx -g 'daemon off;'