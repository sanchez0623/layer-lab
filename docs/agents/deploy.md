<!-- 拆分自 AGENTS.md（原 5-63、151-156、158-175 行），内容原样保留 -->

何时读我：部署/改配置/碰服务器/HTTPS证书/数据库时

## 部署铁律：本地改完必须同步部署到服务器

**每一次代码改动（后端 / 前端 / 配置）都要同时做两件事：**

1. **改本地**：在工作区 `D:\Sanchez\AI\GithubProjects\layer-lab` 改代码、跑测试。
2. **部署到服务器**：本地验证通过后，部署到阿里云 ECS，让线上生效。

不要只改本地就收工——用户的实际使用环境是服务器，本地和服务器必须保持一致。
用户明确说「只改本地先验证」时才可暂缓部署，否则默认每次都部署。

## 部署分支铁律：上线内容必须来自 `main`

**部署到服务器的代码必须来自 `main` 分支**，这是硬约束：

1. 改动先在 `feat/` / `bugfix/` / `ui/` 分支开发、本地验证。
2. 验证通过后合回 `main`（merge / PR 均可），确保 `main` 是最新状态。
3. **部署前确认当前要打包/上传的内容就是 `main`**（`git checkout main` + `git pull` 后再 `pack_deploy.py`）。
4. 禁止把未合回 `main` 的开发分支直接打包部署上线——线上跑的必须是 `main` 的代码。

一句话：**线上 = `main`**。任何绕过 `main` 直接部署其他分支的做法都算违规。

**并行任务上线完整性**：若同时有多个并行开发任务（各自在独立 worktree / 分支上），
部署前必须确认**所有并行任务的功能都已合回 `main`**，再统一部署。
线上跑的 `main` 应当是这一批并行研发功能的**全集**，不能只合了一个分支就发版、
把其他并行任务的功能漏在线下。部署前逐个核对：每个并行分支是否已 merge 进 `main`，
`main` 的 log/diff 是否包含全部改动。

## 服务器信息

- 阿里云 ECS：实例 `i-wz95p1n73tl4oraq1xmi`，公网 IP `47.119.50.139`（cn-shenzhen）
- 远程执行：`python deploy/ecs_run.py run "<命令>"`（读取本机 `C:\Users\zsq23\.workbench\config.json` 的 AK，不在日志回显 secret）
- 上传文件：`python deploy/ecs_run.py upload <本地> <远端>`
- 应用跑在 Docker 容器 `layer-lab` 内，SQLite 数据库在容器 `/data/app.db`（命名卷 `layerlab-data`）
- 一键部署脚本：`bash deploy/deploy_aliyun.sh`（详见 `deploy/README.md`）

## 增量部署速查（每次改完代码）

前提：本机 `C:\Users\zsq23\.workbench\config.json` 已配好阿里云 AK（`ecs_run.py` 依赖）。

> **⚠️ 不要再用 OSS 中转（`deploy/oss_upload.py`）**：阿里云 OSS 账号已被禁用，
> 上传一律返回 `403 UserDisable`（2026-11-14 实测确认）。**改走 `ecs_run.py upload` 直传服务器**，
> 跳过下面的第 2 步，把 tar 直接传到 `/root/layer-lab.tar.gz`（脚本自带 sha256 校验）。

1. 本地验证通过后打包：`python deploy/pack_deploy.py`（产出 `layer-lab-deploy.tar.gz`）
2. **（弃用）~~OSS 中转上传~~** → 改用直传：`python deploy/ecs_run.py upload layer-lab-deploy.tar.gz /root/layer-lab.tar.gz`
3. ECS 下载解包：`python deploy/ecs_run.py run "mkdir -p /root/layer-lab && tar -xzf /root/layer-lab.tar.gz -C /root/layer-lab"`
4. 一键部署（幂等，重建镜像 + 重启容器）：`python deploy/ecs_run.py run "cd /root/layer-lab && bash deploy/deploy_aliyun.sh"`

> 注意：第 4 步会**重建容器**，之前 `docker cp` 进容器的临时文件会丢，别依赖容器内残留文件。

- 改后端/前端代码走上面全量重打包；只改配置/脚本可直接 `python deploy/ecs_run.py upload <本地> <远端>` 覆盖单个文件再重启容器。
- 部署后自检：`docker compose -f deploy/compose.server.yaml ps` + `docker logs layer-lab --tail 50`；访问 `https://47.119.50.139:3444`。
- 完整手册（端口/证书/Basic 认证/排错）：`deploy/README.md`。

## ⚠️ 远程命令引号转义（2026-11-17 第 N 次踩坑，写成硬规则）

`python deploy/ecs_run.py run "..."` 的命令串里**不能再嵌单引号**（比如
`grep 'x'`、`python -c '...'`）——PowerShell 会把内层单引号吃掉/错配，云端生成的
`.sh` 直接报 `unexpected EOF while looking for matching` / `syntax error`，
status Failed exit 2，白跑一轮。**可靠写法：用 PowerShell here-string 整段传参**，
远程命令内部随便用单/双引号都不会被本机解析：

```powershell
$cmd = @'
cd /root/layer-lab
grep -n "__version__" backend/app/__init__.py
bash deploy/deploy_aliyun.sh
'@
python deploy/ecs_run.py run $cmd
```

判断规律：外层 `run "..."` 是**双引号**定界，里面只要出现单引号就崩；改成
`$cmd = @'...'@` 后内层随便写。凡涉及引号/管道/重定向/heredoc 的远程命令一律
here-string，别拼字符串。同理，PowerShell 里 `git -c http.sslbackend=openssl ...`
的 `-c` 必须放在 `git` 之后、子命令之前（`git fetch -c ...` 是错的，`-c` 不是
fetch 的选项）。

## 数据库改动注意

- 服务器真实数据在容器内 `/data/app.db`，查询/修正要走 `docker exec layer-lab python ...`。
- 费率等全局设置存在 `app_settings` 表（key=`fee_config_v1`，万分之口径）。
- 已存在模拟账户会把创建时的费率/配置**快照**进自己的 `config`，改全局不会回填老账户。
- 持仓「买入费用」列来自 `_positions_internal` 的 `permanent_fee`（已含在 `permanent_cost` 内），重算手续费时别漏改，也别重复扣成本。

## HTTPS 证书依赖（dividend-alpha 共享，避免重复踩坑）

- **本项目的 HTTPS 证书不归自己管**：`layer-lab-https`(3444) 只**只读挂载** dividend-alpha 的 IP 证书
  （Let's Encrypt `shortlived` profile，约 6 天有效），**不签发、不续期**。续期入口在 dividend-alpha：
  crontab `17 */12 * * *` 跑 `certbot renew`（docker run），日志在 `/root/dividend-alpha/logs/certbot_renew.log`。
  证书加载靠两个 cron：certbot 续成后 reload `infra-web-1`；`23 */6 * * * /root/ll_https_reload.sh` 每 6 小时
  reload `layer-lab-https`（兜底，手动续期后要立即 reload 两个容器，否则 6 小时内浏览器仍看旧证书）。
- **续期静默失败的典型形态**：浏览器报证书失效、`archive/` 里始终只有 `cert1.pem`、
  `certbot_renew.log` 反复报 `unauthorized / reader size limit exceeded`。
- **2026-11-17 踩坑与修复**：infra-web 的 `infra/nginx.conf` 把 HTTP→HTTPS 的 `return 307` 写在
  **server 级**（`if (-f https.conf)` 守卫）。nginx 的 server 级 `return` 在 **location 匹配之前**执行，
  导致 `/.well-known/acme-challenge/` 也被 307 跳到 https，Boulder 跟随跳转发到 SPA `index.html`（大响应体）→
  `reader size limit exceeded` → HTTP-01 挑战必然失败 → 续期从 11/13 起全挂，11/16 22:26 UTC 证书过期无新接上。
  **修复**：把 `return 307`（含 `if (-f ...)` 守卫）从 server 级挪进 `location /`（catch-all），
  让更具体的 `location ^~ /.well-known/acme-challenge/` 优先 serve token 文件；改完 `nginx -t` 再 reload。
  已验证：改后 challenge 路径返回 200 + token 内容，`/` 仍 307 跳 https、`/da/v1/` 仍走 api 代理。
- **改这一类共享配置要小心**：`infra/nginx.conf` 属 dividend-alpha，是同一 IP 上所有 IP 证书项目的续期入口，
  改它会连带影响 dividend-alpha 自身的 HTTPS。动手前先备份（`nginx.conf.bak-<时间戳>`）+ `nginx -t`。
