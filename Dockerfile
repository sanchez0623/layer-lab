# layer-lab 生产镜像（阿里云 ECS 用）
# 目标：单容器同时提供前端静态资源与 FastAPI，且**同源**（前端 /api 走容器内 nginx 反代到 uvicorn）。
# 这样 HTTPS 反代层只需转发到一个上游端口，避免跨域(CORS)和混合内容(mixed content)问题。
#
# 阶段 1：构建前端静态资源（Vite 产物 dist/）
FROM node:20-alpine AS frontend-build
WORKDIR /build
# 阿里云 npm 镜像，境内拉包更快更稳
RUN npm config set registry https://registry.npmmirror.com
COPY frontend/package.json frontend/package-lock.json ./
# 用 npm ci 严格按 lockfile 安装（可复现）。lock 已与 package.json 同步并 pin 住 typescript，
# 避免 npm install 漂移到不同 TS 版本导致 tsc 行为不一致、构建失败。
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# 部署固定用同源相对路径 /api（关键：不能写死 http://127.0.0.1:8010，否则 HTTPS 下被拦）
ENV VITE_API_BASE=/api
RUN npm run build

# 阶段 2：Python 后端 + 内嵌 nginx（同源网关）
FROM python:3.11-slim
WORKDIR /app

# 系统依赖：nginx 做同源反代；tini 做信号转发；curl 供健康检查
# 用阿里云 Debian 镜像源加速（ECS 内网访问极快；默认 deb.debian.org 在境内很慢）
RUN sed -i 's#deb.debian.org#mirrors.aliyun.com#g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's#deb.debian.org#mirrors.aliyun.com#g' /etc/apt/sources.list 2>/dev/null || true
RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx tini curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 后端依赖（baostock 拉取 A 股日线，requests 访问腾讯行情）
# 用阿里云 PyPI 镜像加速（默认 pypi.org 在 ECS 上仅 ~25KB/s，pandas/numpy/baostock 会拖几十分钟）
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
        -r backend/requirements.txt

# 后端源码
COPY backend/app ./backend/app
COPY backend/scripts ./backend/scripts

# 前端产物 + 容器内 nginx 站点配置
COPY --from=frontend-build /build/dist ./frontend/dist
COPY deploy/nginx.app.conf /etc/nginx/conf.d/default.conf
RUN rm -f /etc/nginx/sites-enabled/default

# 运行期环境变量默认值（可被 compose/.env 覆盖）
#   WB_DATA_DIR / WB_DB_PATH：数据集 CSV、SQLite、上传目录统一落 /data 卷
ENV WB_DATA_DIR=/data \
    WB_DB_PATH=/data/app.db \
    UVICORN_HOST=127.0.0.1 \
    UVICORN_PORT=8010
VOLUME ["/data"]

EXPOSE 3000

# 启动脚本：先起 uvicorn，再前台起 nginx（容器主进程），并做就绪探针
COPY deploy/start.sh /app/deploy/start.sh
RUN chmod +x /app/deploy/start.sh
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["/app/deploy/start.sh"]