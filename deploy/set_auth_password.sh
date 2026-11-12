#!/usr/bin/env bash
# 设置/更新 layer-lab HTTPS Basic 认证密码（幂等）。
# 用法： AUTH_USER=owner AUTH_PASSWORD='你的密码' bash deploy/set_auth_password.sh
#   或： bash deploy/set_auth_password.sh            # 用已有 /root/layer-lab-auth.txt 的用户名，交互不会发生（需预置 AUTH_PASSWORD）
# 说明：不再强制 16 位；密码原样使用（openssl apr1 会安全处理任意可打印字符，含 @ 等符号）。
# 生效：htpasswd 文件被 layer-lab-https 以 ro 挂载，更新后 reload nginx 即生效，无需重启容器。
set -euo pipefail

HTPASSWD_DST="/root/layer-lab.htpasswd"
AUTH_TXT="/root/layer-lab-auth.txt"

AUTH_USER="${AUTH_USER:-$(grep -E '^AUTH_USER=' "$AUTH_TXT" 2>/dev/null | cut -d= -f2 || true)}"
AUTH_USER="${AUTH_USER:-owner}"

if [ -z "${AUTH_PASSWORD:-}" ]; then
  echo "❌ 未提供 AUTH_PASSWORD。用法： AUTH_PASSWORD='密码' bash $0" >&2
  exit 1
fi

HASH=$(openssl passwd -apr1 "$AUTH_PASSWORD")
printf '%s:%s\n' "$AUTH_USER" "$HASH" > "$HTPASSWD_DST"
printf 'AUTH_USER=%s\nAUTH_PASSWORD=%s\n' "$AUTH_USER" "$AUTH_PASSWORD" > "$AUTH_TXT"
# htpasswd 0644 供 nginx worker(非特权 nginx 用户)读取；密码明文文件 0600。
chmod 644 "$HTPASSWD_DST"
chmod 600 "$AUTH_TXT"

# 若 HTTPS 容器在跑，reload 使新密码立即生效
if docker ps --format '{{.Names}}' | grep -qx 'layer-lab-https'; then
  docker exec layer-lab-https nginx -s reload && echo "已 reload layer-lab-https"
fi

echo "✅ Basic 认证已更新：用户 $(grep -E '^AUTH_USER=' "$AUTH_TXT" | cut -d= -f2)，密码见 ${AUTH_TXT}"