#!/usr/bin/env bash
# layer-lab HTTPS 证书续期跟进（放 /root/layer-lab_https_reload.sh，挂 cron）
# 背景：IP 证书 47.119.50.139 由 dividend-alpha 的 certbot cron 每 12 小时自动续期（shortlived）。
# 本任务只负责在续期后 reload layer-lab-https 的 nginx，加载新的证书文件。幂等、无并发。
set -uo pipefail
docker exec layer-lab-https nginx -s reload 2>/dev/null && \
  echo "[$(date '+%F %T')] layer-lab-https reloaded" >> /root/layer-lab_https_reload.log