#!/usr/bin/env bash
# layer-lab · 阿里云 ECS 一键部署（Docker）
#   - 应用容器：宿主机 ${HOST_PORT:-3011} → 容器 3000（内嵌 nginx 同源服务前端 + 反代 uvicorn）
#   - HTTPS 反代：layer-lab-https 容器，宿主机 3444 → 容器 443，复用 47.119.50.139 的
#     Let's Encrypt IP 证书（只读挂载 dividend-alpha 的 letsencrypt 目录，不参与签发）
#   - HTTP Basic 认证：nginx 层 htpasswd，随机强密码存 /root/layer-lab-auth.txt
# 幂等：重复执行安全。数据落在命名卷 layerlab-data。
# 前置：本脚本假定部署在 /root/layer-lab（HTTPS conf / 上传路径按此写）；其他目录请自行调整。
set -euo pipefail
cd "$(dirname "$0")/.."   # 项目根
HOST_PORT="${HOST_PORT:-3011}"
HTTPS_PORT="${HTTPS_PORT:-3444}"
PUBLIC_IP="${PUBLIC_IP:-47.119.50.139}"
COMPOSE="deploy/compose.server.yaml"
HTTPS_CONF_DST="/root/layer-lab-nginx.conf"
HTPASSWD_DST="/root/layer-lab.htpasswd"
AUTH_TXT="/root/layer-lab-auth.txt"
LETS_DIR="/root/dividend-alpha/infra/letsencrypt"     # 已有 IP 证书目录（只读复用）
CERTBOT_DIR="/root/dividend-alpha/infra/certs"        # 续期 webroot

echo "==> [1/7] 检查 Docker"
if ! command -v docker >/dev/null 2>&1; then
  echo "    未安装，正在安装（阿里云镜像源）..."
  curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
  systemctl enable --now docker
fi
docker --version
docker compose version >/dev/null 2>&1 || {
  echo "    安装 docker compose 插件..."
  (apt-get update && apt-get install -y docker-compose-plugin) || \
  (yum install -y docker-compose-plugin) || true
}

echo "==> [2/7] 检查端口占用（${HOST_PORT} 应用 / ${HTTPS_PORT} HTTPS，应空闲）"
for p in "$HOST_PORT" "$HTTPS_PORT"; do
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ":${p}\$"; then
    echo "    ⚠️  端口 ${p} 已被占用，请确认是否预期（同机其他项目可能在用）"
  else
    echo "    端口 ${p} 空闲"
  fi
done

echo "==> [3/7] 配置 2G swap（防构建 OOM，幂等）"
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "    swap 已启用"
else
  swapon /swapfile 2>/dev/null || true
  echo "    swap 已存在"
fi

echo "==> [4/7] 配置 HTTP Basic 凭据（${AUTH_TXT}；显式传 AUTH_PASSWORD 则更新，否则复用已有）"
# 规则：
#   - 显式提供 AUTH_PASSWORD（环境变量）→ 始终用它（重）生成 htpasswd，长度不限、支持 @ 等符号。
#   - 未提供且 htpasswd 已存在 → 沿用现有凭据。
#   - 未提供且无 htpasswd → 生成随机强密码（仅作兜底），并存入 ${AUTH_TXT}。
if [ -n "${AUTH_PASSWORD:-}" ]; then
  AUTH_USER="${AUTH_USER:-$(grep -E '^AUTH_USER=' "$AUTH_TXT" 2>/dev/null | cut -d= -f2 || true)}"
  AUTH_USER="${AUTH_USER:-owner}"
  # nginx 镜像无 htpasswd，用 openssl 生成 apr1（MD5）哈希；openssl 会安全处理任意可打印字符。
  HASH=$(openssl passwd -apr1 "$AUTH_PASSWORD")
  printf '%s:%s\n' "$AUTH_USER" "$HASH" > "$HTPASSWD_DST"
  printf 'AUTH_USER=%s\nAUTH_PASSWORD=%s\n' "$AUTH_USER" "$AUTH_PASSWORD" > "$AUTH_TXT"
  echo "    已用显式 AUTH_PASSWORD 更新 Basic 凭据（用户 ${AUTH_USER}），存于 ${AUTH_TXT}"
elif [ -f "$HTPASSWD_DST" ]; then
  echo "    ${HTPASSWD_DST} 已存在且未传 AUTH_PASSWORD，沿用现有凭据（见 ${AUTH_TXT}）"
else
  # 先完整生成再截断，避免 pipefail 下 head 提前关管道触发 SIGPIPE
  AUTH_PASSWORD=$(tr -dc 'A-Za-z0-9' </dev/urandom | fold -w 24 | head -1) || true
  [ -n "$AUTH_PASSWORD" ] || AUTH_PASSWORD=$(date +%s | sha256sum | cut -c1-24)
  AUTH_USER="${AUTH_USER:-owner}"
  HASH=$(openssl passwd -apr1 "$AUTH_PASSWORD")
  printf '%s:%s\n' "$AUTH_USER" "$HASH" > "$HTPASSWD_DST"
  printf 'AUTH_USER=%s\nAUTH_PASSWORD=%s\n' "$AUTH_USER" "$AUTH_PASSWORD" > "$AUTH_TXT"
  echo "    已生成随机 Basic 凭据，保存在 ${AUTH_TXT}"
fi
# htpasswd 必须 0644：nginx worker 以非特权 nginx 用户运行，0600 会 Permission denied → 502/500。
# （文件只含 apr1 哈希，不含明文，0644 可接受。）密码明文文件保持 0600。
chmod 644 "$HTPASSWD_DST" 2>/dev/null || true
chmod 600 "$AUTH_TXT" 2>/dev/null || true

echo "==> [5/7] 构建并启动应用容器（宿主机 ${HOST_PORT} → 容器 3000）"
HOST_PORT="$HOST_PORT" docker compose -f "$COMPOSE" up -d --build

echo "==> [6/7] 启动 HTTPS 反代容器（宿主机 ${HTTPS_PORT} → 443，Basic 认证）"
# 先把 HTTPS conf 拷到宿主机约定位置（容器以 ro 挂载它，必须先存在）
cp "$(dirname "$0")/nginx.https.conf" "$HTTPS_CONF_DST"
chmod 644 "$HTTPS_CONF_DST"
docker rm -f layer-lab-https >/dev/null 2>&1 || true
# 前置：确认证书目录存在（复用 dividend-alpha 已签的 IP 证书）
if [ ! -f "${LETS_DIR}/live/${PUBLIC_IP}/fullchain.pem" ]; then
  echo "    ❌ 未找到 IP 证书 ${LETS_DIR}/live/${PUBLIC_IP}/fullchain.pem" >&2
  echo "       请先在 dividend-alpha 侧签发/续期 47.119.50.139 的 Let's Encrypt IP 证书。" >&2
  echo "       应用容器已启动，可先用 http://${PUBLIC_IP}:${HOST_PORT} 访问（无 TLS）。" >&2
else
  # 只绑定 3444→443，不占宿主机 80：80 由 infra-web 持有，且证书续期的 acme-challenge
  # webroot 由 dividend-alpha/infra-web 统一处理（共用 certs 目录），layer-lab 无需 80。
  # http→https 跳转因此仅在 3444 自身生效；直接访问 https://IP:3444 即可。
  docker rm -f layer-lab-https >/dev/null 2>&1 || true
  docker run -d --name layer-lab-https --restart unless-stopped \
    -p "${HTTPS_PORT}:443" \
    --add-host host.docker.internal:host-gateway \
    -v "${HTTPS_CONF_DST}:/etc/nginx/conf.d/default.conf:ro" \
    -v "${HTPASSWD_DST}:/etc/nginx/layer-lab.htpasswd:ro" \
    -v "${LETS_DIR}:/etc/letsencrypt:ro" \
    -v "${CERTBOT_DIR}:/var/www/certbot:ro" \
    nginx:1.27-alpine
fi

echo "==> [7/7] 健康检查"
sleep 2
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  "http://127.0.0.1:${HOST_PORT}/api/health" 2>/dev/null || echo 000)
if [ "$code" = "200" ]; then
  echo "✅ 应用 /api/health 健康（HTTP ${code}）"
else
  echo "⚠️  应用 /api/health 返回 ${code}（可能仍在启动）"
  echo "    查看日志: docker compose -f ${COMPOSE} logs -f layer-lab"
fi
if docker ps --format '{{.Names}}' | grep -q '^layer-lab-https$'; then
  hcode=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    "https://127.0.0.1:${HTTPS_PORT}/api/health" -k 2>/dev/null || echo 000)
  # 401 = Basic 认证已生效（服务响应正常）；200 亦正常
  echo "HTTPS /api/health → HTTP ${hcode}（401/200 均表示 TLS+服务正常）"
else
  echo "HTTPS 反代未运行；临时用 http://${PUBLIC_IP}:${HOST_PORT} 访问"
fi

echo ""
echo "完成。"
echo "  应用容器 : 宿主机 ${HOST_PORT} → 容器 3000"
echo "  HTTPS    : https://${PUBLIC_IP}:${HTTPS_PORT}  （Basic: $(grep AUTH_USER "$AUTH_TXT" | cut -d= -f2)）"
echo "  凭据     : ${AUTH_TXT}"
echo "  数据卷   : layerlab-data（SQLite/CSV/行情缓存，持久化）"
echo "  查看日志 : docker compose -f ${COMPOSE} logs -f layer-lab"