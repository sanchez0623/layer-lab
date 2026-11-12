# layer-lab · 阿里云部署（ECS + Docker + HTTPS）

参考同机项目 `ashare-limit-lens` 的成熟链路，把 layer-lab（FastAPI 后端 + Vite/React 前端）部署到
阿里云 ECS `i-wz95p1n73tl4oraq1xmi`（公网 IP **47.119.50.139**，cn-shenzhen）。

## 端口与 HTTPS（重点）

同机已有多个项目，端口必须避开已占用者。部署前实测监听端口：
`22, 80, 443, 1053, 3010, 3443, 3480, 7890, 8080, 8083, 8787, 9090, 20241, 36117`。

| 用途 | 宿主机端口 | 容器端口 | 说明 |
|---|---|---|---|
| layer-lab 应用 | **3011** | 3000 | 容器内嵌 nginx 同源服务前端 + 反代 uvicorn(8010) |
| layer-lab HTTPS | **3444** | 443 | `layer-lab-https` nginx 容器终结 TLS，反代到 127.0.0.1:3011 |
| （参考）limit-lens | 3010 | 3000 | 同机已占用，勿冲突 |
| （参考）limit-lens HTTPS | 3443 | 443 | 同机已占用，故本项用 3444 |

**HTTPS 证书复用**：`dividend-alpha` 已为 IP `47.119.50.139` 签发 Let's Encrypt **IP 证书**（shortlived），
其 certbot cron 每 12 小时自动续期。本项目的 `layer-lab-https` 容器**只读挂载**该证书目录，不参与签发：
- 证书目录（宿主机）：`/root/dividend-alpha/infra/letsencrypt` → 容器 `/etc/letsencrypt:ro`
- 续期 webroot：`/root/dividend-alpha/infra/certs` → 容器 `/var/www/certbot:ro`
- 证书文件：`/etc/letsencrypt/live/47.119.50.139/fullchain.pem`

**访问保护**：`layer-lab-https` 层加 HTTP Basic 认证。首次部署若未传 `AUTH_PASSWORD` 会生成随机强密码，存
`/root/layer-lab-auth.txt`（htpasswd 文件 `/root/layer-lab.htpasswd`）。**密码长度不限、支持 `@` 等符号**；
部署时用 `AUTH_PASSWORD='你的密码' bash deploy/deploy_aliyun.sh` 指定，或随时用
`AUTH_PASSWORD='新密码' bash deploy/set_auth_password.sh` 在线改密（reload nginx 立即生效，无需重启容器）。

**为什么用「容器内嵌 nginx 同源」**：前端固定用相对路径 `/api`（构建时 `VITE_API_BASE=/api`），
由应用容器内的 nginx 把 `/api` 反代到同容器 uvicorn。这样浏览器全程同源，从根上避免
跨域(CORS)与 HTTPS 下混合内容(mixed content)问题——否则前端写死 `http://127.0.0.1:8010` 会被浏览器拦截。

## 目录结构（新增）

```
layer-lab/
├── Dockerfile                  # 多阶段：Node 构建前端 → python:3.11-slim + nginx 同源网关
├── .dockerignore
└── deploy/
    ├── compose.server.yaml     # 应用容器编排（3011→3000，数据卷 layerlab-data）
    ├── nginx.app.conf          # 应用容器内 nginx：静态资源 + /api 反代 uvicorn
    ├── nginx.https.conf        # HTTPS 反代（3444→443，复用 IP 证书 + Basic 认证）
    ├── start.sh                # 容器启动：起 uvicorn → 等就绪 → 前台 nginx
    ├── deploy_aliyun.sh        # 一键部署（幂等）
    ├── ll_https_reload.sh      # 证书续期后 reload nginx（挂 cron）
    ├── pack_deploy.py          # 打最小部署包 layer-lab-deploy.tar.gz
    ├── oss_upload.py           # OSS 中转上传（生成 30 分钟预签名 URL）
    ├── ecs_run.py              # 阿里云云助手 OpenAPI（远程执行/上传）
    └── sg_open.py              # 安全组放行 3011/3444
```

## 部署流程

### 方式 A：本机有源码，直接推到 ECS（推荐）

```bash
# 1) 本地打包（排除 node_modules/.env/缓存）
python deploy/pack_deploy.py

# 2) 传到 ECS（OSS 中转 → 云助手下载；或用 ecs_run.py upload 分块直传）
python deploy/oss_upload.py layer-lab-deploy.tar.gz layer-lab-deploy.tar.gz
# 记下输出的 PRESIGNED_URL，然后在 ECS 上下载到 /root/layer-lab.tar.gz

# 3) 在 ECS 上解包到 /root/layer-lab 并部署
python deploy/ecs_run.py run "mkdir -p /root/layer-lab && tar -xzf /root/layer-lab.tar.gz -C /root/layer-lab"

# 4) 放行安全组端口（幂等；3011 应用 + 3444 HTTPS）
python deploy/sg_open.py open-all

# 5) 一键部署（装 Docker→swap→Basic 凭据→构建应用→起 HTTPS 反代→健康检查）
python deploy/ecs_run.py run "cd /root/layer-lab && bash deploy/deploy_aliyun.sh"
```

> `ecs_run.py` 读取本机 `C:\Users\zsq23\.workbench\config.json` 的 AK，不在日志回显 secret。

### 方式 B：登 ECS 手动跑

```bash
cd /root/layer-lab
bash deploy/deploy_aliyun.sh        # 可用环境变量覆盖：HOST_PORT=3011 HTTPS_PORT=3444
```

## 部署后

- 访问：`https://47.119.50.139:3444`（直接访问；不占宿主机 80，无 http:80 跳转；Basic 认证见 `/root/layer-lab-auth.txt`）
- 应用直连（无 TLS，仅内网/调试）：`http://47.119.50.139:3011`
- 日志：`docker compose -f deploy/compose.server.yaml logs -f layer-lab`
- 数据：命名卷 `deploy_layerlab-data`（SQLite `app.db`、数据集 CSV、行情缓存、上传目录；前缀 `deploy_` 来自 compose 项目名，因 compose 文件在 `deploy/` 下）
- 证书续期：把 `deploy/ll_https_reload.sh` 拷到 `/root/` 并加 cron（每 6 小时 reload 一次即可）：
  ```
  cp /root/layer-lab/deploy/ll_https_reload.sh /root/
  chmod +x /root/ll_https_reload.sh
  (crontab -l 2>/dev/null; echo '17 */6 * * * /root/ll_https_reload.sh') | crontab -
  ```

## 常见问题

- **HTTPS 容器没起来 / 80 冲突**：`layer-lab-https` 只绑定 3444→443，**不占宿主机 80**（80 由 infra-web 持有，
  且证书续期的 acme-challenge webroot 由 dividend-alpha/infra-web 统一处理）。因此没有 http:80→https 自动跳转，
  直接访问 `https://47.119.50.139:3444` 即可，属预期。
- **证书找不到**：确认 `/root/dividend-alpha/infra/letsencrypt/live/47.119.50.139/fullchain.pem` 存在
  （由 dividend-alpha 维护）。缺失时应用容器仍可用 `http://...:3011` 访问。
- **前端报「无法连接后端」**：说明 `/api` 反代不通，查应用容器日志 `docker logs layer-lab`。
- **数据要清空重跑**：`docker compose -f deploy/compose.server.yaml down -v`（删除 `layerlab-data` 卷）。

## 与 limit-lens 的差异

| | ashare-limit-lens | layer-lab |
|---|---|---|
| 技术栈 | Node 单进程（前端+API 同源） | FastAPI(8010) + Vite 前端，容器内 nginx 同源化 |
| 应用端口 | 3010 | **3011** |
| HTTPS 端口 | 3443 | **3444** |
| 认证 | 应用内 Basic（Node） | nginx 层 Basic（htpasswd） |
| 数据 | 命名卷 paper-data | 命名卷 layerlab-data |
| 证书 | 复用 47.119.50.139 IP 证书 | 同（只读挂载） |