# layer-lab 项目记忆（跨会话约定）

本文件是给 AI agent 的持久项目记忆，跨会话读取。改动代码前先看这里的约定。

## 部署铁律：本地改完必须同步部署到服务器

**每一次代码改动（后端 / 前端 / 配置）都要同时做两件事：**

1. **改本地**：在工作区 `D:\Sanchez\AI\GithubProjects\layer-lab` 改代码、跑测试。
2. **部署到服务器**：本地验证通过后，部署到阿里云 ECS，让线上生效。

不要只改本地就收工——用户的实际使用环境是服务器，本地和服务器必须保持一致。
用户明确说「只改本地先验证」时才可暂缓部署，否则默认每次都部署。

### 服务器信息

- 阿里云 ECS：实例 `i-wz95p1n73tl4oraq1xmi`，公网 IP `47.119.50.139`（cn-shenzhen）
- 远程执行：`python deploy/ecs_run.py run "<命令>"`（读取本机 `C:\Users\zsq23\.workbench\config.json` 的 AK，不在日志回显 secret）
- 上传文件：`python deploy/ecs_run.py upload <本地> <远端>`
- 应用跑在 Docker 容器 `layer-lab` 内，SQLite 数据库在容器 `/data/app.db`（命名卷 `layerlab-data`）
- 一键部署脚本：`bash deploy/deploy_aliyun.sh`（详见 `deploy/README.md`）

### 增量部署速查（每次改完代码）

前提：本机 `C:\Users\zsq23\.workbench\config.json` 已配好阿里云 AK（`ecs_run.py` / `oss_upload.py` 依赖）。

1. 本地验证通过后打包：`python deploy/pack_deploy.py`（产出 `layer-lab-deploy.tar.gz`）
2. OSS 中转上传：`python deploy/oss_upload.py layer-lab-deploy.tar.gz layer-lab-deploy.tar.gz`
3. ECS 下载解包：`python deploy/ecs_run.py run "mkdir -p /root/layer-lab && tar -xzf /root/layer-lab.tar.gz -C /root/layer-lab"`
4. 一键部署（幂等，重建镜像 + 重启容器）：`python deploy/ecs_run.py run "cd /root/layer-lab && bash deploy/deploy_aliyun.sh"`

- 改后端/前端代码走上面全量重打包；只改配置/脚本可直接 `python deploy/ecs_run.py upload <本地> <远端>` 覆盖单个文件再重启容器。
- 部署后自检：`docker compose -f deploy/compose.server.yaml ps` + `docker logs layer-lab --tail 50`；访问 `https://47.119.50.139:3444`。
- 完整手册（端口/证书/Basic 认证/排错）：`deploy/README.md`。

### 提交 / 推送 / 版本号

每次改完功能（本地验证通过后）：

1. **及时 commit 并 push 到 GitHub**：`origin` = `https://github.com/sanchez0623/layer-lab.git`。不要攒一堆改动不推。
2. **推送会触发 GitHub CI**：`.github/workflows/ci.yml` 已配置，push 即触发（后端 pytest + 前端 build）。CI 红了先修再继续。
3. **同步 bump 版本号（semver）**，三处一起改，保持一致：
   - `backend/app/__init__.py` 的 `__version__`
   - `frontend/package.json` 的 `version`
   - `backend/app/main.py` 里 FastAPI(app, version=...) 的版本
   - 改完 `grep -rn "1\.0\.0\|version="` 复核，别漏。
4. **版本号升降规则**：
   - 小改动（调 UI、修 bug）：patch +1，如 `1.0.0 → 1.0.1`
   - 新增大功能：minor +1，如 `1.0.0 → 1.1.0`
   - 重构级/不兼容改动：major +1，如 `1.2.3 → 2.0.0`
   - **拿不准属于哪级就先问我**，别自己拍。

5. **同步更新 README（按变动量级）**：**大、中功能变动**（新增/重构模块、新增端点、改数据流、改部署口径）如有必要就更新 README，保持它是**最新可用**的描述。**修 bug、调 UI 样式不算**，不用动 README。
   - 判定口径：一个外部使用者照 README 能否正确理解和跑起来——若变动让它过时了就改。
   - 项目根目前**没有 README.md**（只有 `AGENTS.md`）；若要落地 README，先跟我确认放哪、写什么范围，别擅自生成大而全的文档。

### git push 实操（沙箱内可直接跑，VPN 开关无关）

系统 git 配置（`D:/Program Files/Git/etc/gitconfig`）写死了 `http.sslbackend=schannel`，
而 schannel 取不到 Windows 凭据管理器里的 GitHub 凭据 → `git push` 报
`SEC_E_NO_CREDENTIALS`。**这与 VPN 开关无关**（诊断确认过 TCP 443 到 github.com 通、
无任何代理配置），开不开 VPN 都用下面同一套方法。

沙箱还禁止 git 的凭据交互提示（`couldn't create signal pipe`），所以不能走任何
需要弹提示的 helper。可靠做法是 **openssl 后端 + 从凭据管理器直读 token 内嵌 URL**：

```powershell
# 1) 用 CredRead 从 Windows 凭据管理器读出 GitHub token（gho_... OAuth token）
#    写一段 Add-Type P/Invoke 调 CredRead，target 用 "gh:github.com:sanchez0623"
# 2) token 放进内存变量（勿回显到输出/日志），内嵌 URL 推送：
git -c http.sslbackend=openssl push "https://x-access-token:$tok@github.com/sanchez0623/layer-lab.git" main
# 3) 验证：git -c http.sslbackend=openssl ls-remote "https://x-access-token:$tok@github.com/sanchez0623/layer-lab.git" main
```

要点：
- 必须 `-c http.sslbackend=openssl`（覆盖系统默认的 schannel，否则必报 SEC_E_NO_CREDENTIALS）。
- 已验证：远端 `refs/heads/main` 与本地 HEAD 一致即成功；`signal pipe` 那两行是 git 无害警告，不影响结果。
- token 全程只在内存、不回显、不落盘；临时读取脚本用完即删。

### 数据库改动注意

- 服务器真实数据在容器内 `/data/app.db`，查询/修正要走 `docker exec layer-lab python ...`。
- 费率等全局设置存在 `app_settings` 表（key=`fee_config_v1`，万分之口径）。
- 已存在模拟账户会把创建时的费率/配置**快照**进自己的 `config`，改全局不会回填老账户。
- 持仓「买入费用」列来自 `_positions_internal` 的 `permanent_fee`（已含在 `permanent_cost` 内），重算手续费时别漏改，也别重复扣成本。

## 领域要点（避免重复踩坑）

- **T+1 可卖**：`broker.new_day` 只在真正跨交易日时清零 `today_*_buy_qty`，撮合每根 K 线都调它，别无条件 reset。
- **分板块申报口径**：股数计算必须走 `trading_rules.rules_for(symbol)` 的 `min_qty`/`step`（科创板 200/1，创业板·主板 100/100），不要写死 100 股整手。
- **16 笔序列**：高价股按「方案 2」——金额不足最小申报也按最小申报建仓（`board_buy_quantity`），宁可略微透支该笔预算也不跳过。
- **止盈**：价格轨/账户轨各 15% 立即清；10%~15% 进 Armed 区间，内含量价背离、VWAP 回撤 4% 两个辅助清仓点。做 T 目标价差 `t_target_spread` 已可配置（模拟盘 config 面板）。
- **baostock 禁止并发**：baostock 数据源**不能并发访问**，并发会被服务端拉黑（封 IP/账号）。任何调用 baostock 的路径必须串行化：
  - `universe.py` 已做对：单 login 复用全部查询 + `threading.RLock` 串行 + 后台单线程回补（`_relogin` 定时轮换）。
  - **`market.py` 是隐患**：`fetch_baostock_daily` / 两处 `fetch_*_index` 各自独立 `bs.login()`（约 317/378/431 行），**没有全局锁**；若调度线程（paper-scheduler）与用户触发请求并发打 baostock，会撞车。新增任何 baostock 调用前，先确认已被全局锁串行化，或复用 `universe.py` 的会话，别再各开 login。
  - 默认数据源已是 quan_parquet（只读本地 parquet，无并发限制），只有回落旧链路时才真正打 baostock——但别因此掉以轻心，回落路径同样要串行。
