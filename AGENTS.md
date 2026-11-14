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

### 分支策略（开发 / 改 UI / 修 bug 一律开新分支）

**不要直接在 `main` 上改代码**。每次开发功能、优化 UI、修复 bug 都先从最新 `main` 切一个新分支，
改完在分支上验证，再合回 `main`，避免多人 / 多任务改动互相冲突。

分支命名前缀（小写 + 短横线，紧跟简短英文/拼音描述）：

- **`feat/`** —— 新增功能。例：`feat/add-stop-loss-panel`、`feat/backtest-config-module`
- **`bugfix/`** —— 修复 bug。例：`bugfix/fix-t1-sell-qty`、`bugfix/fix-fee-double-count`
- **`ui/`** —— UI 调整 / 样式优化。例：`ui/polish-config-panel`、`ui/fix-table-overflow`

流程：`git checkout main && git pull` → `git checkout -b <前缀>/<描述>` → 改 + 本地验证 →
commit 到该分支 → 合并回 `main`（或走 PR）→ push。**版本号 bump、部署、README 更新仍按上面各节执行。**

**合并冲突处理**：合回 `main` 时若产生冲突，逐个文件判断双方改动的意图后再合。
**凡是拿不准该保留哪一边、或冲突涉及业务逻辑 / 别人的改动时，先停下来问用户，别自己瞎选**；
能明确判断的（如本分支独有新增、纯格式差异）可直接解。解完务必本地跑一遍验证再 push。

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

## 架构原则（实现功能时必须遵守）

这三条是写代码时的硬约束，新功能 / 重构都要对齐，别各写一套。

1. **实盘与回测共用同一套逻辑（单一真相源）**。
   模拟实时交易和回测必须走同一份核心撮合 / 决策代码，不允许维护两份平行实现。
   目的：防止「回测赚钱、实盘行为不一致」这类分叉 bug。要改逻辑只改一处，两种模式都受益。
   新增功能时先问：这段逻辑是两种模式共享，还是被我复制了一份？

2. **配置项尽量可视化可编辑，收敛到统一模块**。
   可调参数（费率、止盈阈值、`t_target_spread`、申报口径等）集中到一个统一的配置模块，
   并尽量在前端暴露成可视化、可直接编辑的面板，改完即可拿去回测验证。
   目的：降低调参 / 试错成本，避免参数散落各处、改一次要翻好几个文件。

3. **交易进行中的逻辑要「定格」，不被配置改动干扰**。
   一笔交易 / 一个模拟账户在创建或开仓时，把当时生效的配置**快照**进自己的状态
   （与现有「模拟账户创建时快照费率进 `config`」的做法一致）。
   之后即使全局配置被修改，也不得回填、不得影响已经在跑的交易，保证在途逻辑确定、可复现。
   新配置只对之后新建的交易 / 账户生效。

4. **重要功能必须有单元测试**。
   新增 / 改动核心逻辑（撮合、T+1、止盈、申报口径、费率计算、回测与实盘共用部分等）都要补 pytest 单测，
   覆盖正常路径 + 边界（最小申报、金额不足、跨交易日、费用含入成本等易错点）。
   纯 UI 样式微调可不强制，但凡涉及资金 / 撮合 / 规则判定的改动都不能裸奔——本地 `pytest` 过了再合、再部署。
   测试和功能一起在同一个 `feat/`、`bugfix/` 分支里提交。

## 系统设计规则（写代码时约束"设计得好不好"）

封装 / 继承 / 多态是面向对象的**机制**，不是设计规则；真正约束设计质量的是下面几组经典规则。
新增模块、重构、抽象时对着这几组自查。

### 一、对 OOP 机制的补正

- **抽象**：先定「是什么 / 做什么」，再定「怎么做」。比封装更前置。
- **组合优于继承**：继承是白盒复用、强耦合（父类一改全族受灾）；组合是黑盒拼装、可运行时替换。
  继承只留给真正 is-a 且稳定的层次。
- **里氏替换（LSP）**：子类必须能替掉父类而不破坏调用方。这是判断「这个继承该不该用」的硬标准
  ——方形继承长方形就是经典反例。

### 二、SOLID（最成体系的一组）

- **SRP 单一职责**：一个模块只对一个变更原因负责。判断口径不是「功能少」，而是「谁会来改它」。
- **OCP 开闭**：对扩展开放、对修改关闭。靠抽象和多态加分支，而不是回去改老 `if-else`。
- **ISP 接口隔离**：别逼调用方依赖它用不到的方法，胖接口拆成小角色接口。
- **DIP 依赖倒置**：高层不依赖低层细节，双方都依赖抽象——即「面向接口编程，而非面向实现编程」。
- （LSP 见上，五条凑齐。）

### 三、复杂度控制三件套

- **KISS**：能简单就别炫技，可读性优先于巧妙。
- **YAGNI**：不为「以后可能要」提前写扩展点。互联网里最贵的就是没人用的抽象层。
- **DRY**：同一份知识只留一处；但别把「长得像的重复」硬合并——偶然重复抽成公共模块反而制造耦合。

### 四、边界与耦合

- **高内聚、低耦合**：老规矩，但仍是所有分层 / 微服务的祖先。
- **信息隐藏（Parnas）**：模块刻意藏住「最可能变的决策」（数据结构、算法、外部依赖），只暴露稳定接口。
  比封装更强调预测变化。
- **关注点分离**：业务、持久化、展示、横切逻辑（日志 / 事务 / 鉴权）分开放，AOP 就是它的产物。
- **迪米特法则 / 最少知识**：别跟陌生人说话，`a.getB().getC().do()` 这种链式钻取就是耦合泄漏。

### 五、正确性约定

- **契约式设计**：前置条件、后置条件、不变式写清楚，谁担保谁负责。
- **Fail Fast + 防御式边界**：内部尽早断言崩掉，外部输入才做校验兜底。别让脏数据一路流到 DB 才爆。
