<!-- 拆分自 AGENTS.md（原 65-149 行），内容原样保留；原第 100 行「5.同步更新 README」编号错位已修正为独立小节 -->

何时读我：commit/push/开分支/版本号/解冲突时

## 提交 / 推送 / 版本号

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

## 分支策略（开发 / 改 UI / 修 bug 一律开新分支）

**不要直接在 `main` 上改代码**。每次开发功能、优化 UI、修复 bug 都先从最新 `main` 切一个新分支，
改完在分支上验证，再合回 `main`，避免多人 / 多任务改动互相冲突。

分支命名前缀（小写 + 短横线，紧跟简短英文/拼音描述）：

- **`feat/`** —— 新增功能。例：`feat/add-stop-loss-panel`、`feat/backtest-config-module`
- **`bugfix/`** —— 修复 bug。例：`bugfix/fix-t1-sell-qty`、`bugfix/fix-fee-double-count`
- **`ui/`** —— UI 调整 / 样式优化。例：`ui/polish-config-panel`、`ui/fix-table-overflow`

流程：`git checkout main && git pull` → `git checkout -b <前缀>/<描述>` → 改 + 本地验证 →
commit 到该分支 → 合并回 `main`（或走 PR）→ push。**版本号 bump、部署、README 更新仍按上面各节执行。**

### 并行开发：用 git worktree 隔离多任务

同时推进多个开发任务时，**用 `git worktree` 为每个任务建独立工作目录**，每个 worktree
从最新 `main` 切自己的分支，互不干扰、可并行改、可同时跑本地验证。避免多个任务挤在同一个
工作区来回切分支、改乱彼此的未提交改动。

```powershell
git fetch origin
git worktree add ../layer-lab-featA -b feat/featA origin/main   # 任务 A 独立目录 + 新分支
git worktree add ../layer-lab-bugB  -b bugfix/bugB  origin/main   # 任务 B 独立目录 + 新分支
# 各 worktree 内独立改代码 / 验证；用完 git worktree remove <目录> 清理
git worktree list   # 查看所有 worktree
```

- 每个 worktree = 一个任务 = 一个从 `origin/main` 切出的分支，前缀仍按 `feat/` `bugfix/` `ui/`。
- worktree 目录之间共享同一个 `.git`，但工作区文件完全独立，可并行开发。

**并行任务合并上线（关键）**：所有并行任务各自验证通过后，必须**把每一个都合回 `main`**，
确保这一批并行研发的功能**全部**进入 `main`，再统一走部署流程。**不能只合一个就发版**，
否则线上会漏掉其他并行任务的功能。合完确认 `main` 已包含全部并行改动（逐个 `git log` / diff 复核）
后再 `pack_deploy.py` 部署。

**合并冲突处理**：合回 `main` 时若产生冲突，逐个文件判断双方改动的意图后再合。
**凡是拿不准该保留哪一边、或冲突涉及业务逻辑 / 别人的改动时，先停下来问用户，别自己瞎选**；
能明确判断的（如本分支独有新增、纯格式差异）可直接解。解完务必本地跑一遍验证再 push。

### 同步更新 README（按变动量级）

**大、中功能变动**（新增/重构模块、新增端点、改数据流、改部署口径）如有必要就更新 README，保持它是**最新可用**的描述。**修 bug、调 UI 样式不算**，不用动 README。
- 判定口径：一个外部使用者照 README 能否正确理解和跑起来——若变动让它过时了就改。
- 项目根目前**没有 README.md**（只有 `AGENTS.md`）；若要落地 README，先跟我确认放哪、写什么范围，别擅自生成大而全的文档。

## git push 实操（沙箱内可直接跑，VPN 开关无关）

> ⚠️ **动手前先核对分支落点 + 先 fetch**（2026-11-14 踩坑，浪费多轮）：
> 本地 `main` 可能**落后于 `origin/main`**（本地陈旧），且 `git checkout -b` 建分支后
> 若中途被打断/切走，commit 可能落到**意料之外的已有分支**上。教训：
> 1. 提交前 `git branch --show-current` 确认真在目标分支，别想当然；
> 2. 开分支前先 `git fetch origin`，从 `origin/main`（不是本地 main）切，否则
>    cherry-pick / 合并会撞上远端已有、本地没有的改动（同文件同版本号 → 冲突）；
> 3. 若 commit 落错分支：`git branch tmp-save <sha>` 存住 → 旧分支 `git reset --hard <原基点>`
>    → 新分支 `git reset --hard origin/main` → `git cherry-pick tmp-save` → 删 tmp-save。
>    （`git cherry-pick` 若 HEAD 已是目标 commit 会报 empty，先 `--abort`/`--quit` 再重来。）
> 4. 版本号冲突：若远端已 bump 到同一版本（如都到 1.5.1），cherry-pick 时版本号 hunk 会
>    自动去重丢弃——这是对的，别再重复 bump，确认三处一致即可。

系统 git 配置（`D:/Program Files/Git/etc/gitconfig`）写死了 `http.sslbackend=schannel`，
而 schannel 取不到 Windows 凭据管理器里的 GitHub 凭据 → `git push` 报
`SEC_E_NO_CREDENTIALS`。**这与 VPN 开关无关**（诊断确认过 TCP 443 到 github.com 通、
无任何代理配置），开不开 VPN 都用下面同一套方法。

沙箱还禁止 git 的凭据交互提示（`couldn't create signal pipe`），所以不能走任何
需要弹提示的 helper。可靠做法是 **openssl 后端 + 从凭据管理器直读 token 内嵌 URL**：

```powershell
# 1) 用 CredRead 从 Windows 凭据管理器读出 GitHub token（gho_... OAuth token）
#    写一段 Add-Type P/Invoke 调 CredRead，target 用 "gh:github.com:sanchez0623"
# 2) blob 必须用 UTF-8 解码（见下方要点，用 Unicode 解码会认证失败）
$tok = [System.Text.Encoding]::UTF8.GetString($bytes).Trim([char]0)
# 3) token 放进内存变量（勿回显到输出/日志），内嵌 URL 推送：
git -c http.sslbackend=openssl push "https://x-access-token:$tok@github.com/sanchez0623/layer-lab.git" main
# 4) 验证：git -c http.sslbackend=openssl ls-remote "https://x-access-token:$tok@github.com/sanchez0623/layer-lab.git" main
```

要点：
- 必须 `-c http.sslbackend=openssl`（覆盖系统默认的 schannel，否则必报 SEC_E_NO_CREDENTIALS）。
- **token blob 是 UTF-8 编码**（解出来是 40 字符、`gho_` 开头的 OAuth token）：解码必须用
  `[System.Text.Encoding]::UTF8.GetString($bytes).Trim([char]0)`。**别用 `Unicode`/UTF-16**——
  会得到乱码，GitHub 报 `Invalid username or token. Password authentication is not supported`
  （2026-11-14 实测踩坑，浪费一轮排查）。诊断法：只打印 token 的 `Length`（应为 40）和前 4 位
  （应为 `gho_`）确认解码对不对，**别回显全文**。
- **间歇性网络抖动要重试**：GitHub 偶发 `Connection was reset` / `Failed to connect ... port 443`。
  push 外面包一层 3 次重试（每次间隔几秒、每轮后用 `ls-remote` 比对远端 vs 本地 HEAD），认证没问题、
  纯网络抖动时第 2~3 次即成功，别一次失败就判定凭据有问题。
- 已验证：远端 `refs/heads/main` 与本地 HEAD 一致即成功；`signal pipe` 那两行是 git 无害警告，不影响结果。
  另外 `git-credential-wincred.exe ... No such file or directory` 的 erase/store 报错同样是**无害噪音**
  （沙箱里 helper 缺失），只要内嵌 token 的 push 最终成功就忽略。
- token 全程只在内存、不回显、不落盘；临时读取脚本用完即删（`$tok = $null`）。

### HTTPS push 被 DNS 污染卡死时：切 SSH over 443（2026-11-23 实测解法）

上面那套 HTTPS + token 方法依赖能连上 `github.com:443`。但凌晨时段可能遇到
**DNS 污染**：`Resolve-DnsName github.com` 把域名解析到被墙的 IP（如实测解析到
`20.247.166.64`，连接直接 `Connection was reset` / `port 443 Could not connect`），
而 `curl --resolve github.com:443:140.82.121.4 https://github.com/` 用可用 IP 直连
却返回 200——证明**纯 DNS 解析问题，不是凭据、不是代码**。此时重试 HTTPS push 无效，
改 hosts 又需管理员权限（沙箱内 `Access denied`）。

**可靠绕法：走 SSH over 443**（`ssh.github.com:443` 是另一个域名+端口，通常不在污染范围）。
项目已预配好 SSH（`~/.ssh/config` 里 `Host github.com` → `HostName ssh.github.com` /
`Port 443` / `IdentityFile ~/.ssh/id_ed25519`），无需额外配置：

```powershell
# 1) 探测 SSH 端点连通 + 认证（首次会写 known_hosts，输出含 "Hi sanchez0623!" 即成功；
#    ssh -T 正常退出码是 1，因为 GitHub 不提供 shell，别当成失败）
ssh -T git@github.com -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20
# 2) 直接用 SSH URL 推送（不必先改 remote，一次性）：
git push git@github.com:sanchez0623/layer-lab.git main
# 3) 验证远端确实收到（比对远端 HEAD 是否等于本地 commit）：
git ls-remote git@github.com:sanchez0623/layer-lab.git main
```

诊断顺序（push 卡住时按此排查，别一上来就怀疑凭据）：
1. `Test-NetConnection github.com -Port 443` 通不通；不通则 `Resolve-DnsName github.com`
   看解析到的 IP，再用 `curl --resolve github.com:443:<可用IP> https://github.com/` 测
   该 IP 能否完成 TLS（HTTP 200 = IP 可用，纯 DNS 问题）。
2. DNS 被污染 → 走上面 SSH over 443（首选，已验证稳定）。
3. 临时把 remote 切成 SSH（可选，一劳永逸避开 HTTPS 的 DNS 干扰）：
   `git remote set-url origin git@github.com:sanchez0623/layer-lab.git`
   **完全可逆**，随时切回 HTTPS：`git remote set-url origin https://github.com/sanchez0623/layer-lab.git`
   （只动本仓库 `.git/config` 一行 URL，不影响线上/其他仓库/SSH key；原 HTTPS token 凭据保留，
   切回即用。切完 `git fetch origin` + `git push --dry-run origin main` 双向验证。）
4. 本地代理（Clash 等）没开时别指望 `http.proxy`——先 `Get-NetTCPConnection -State Listen`
   确认代理端口真在监听，否则 `ProxyServer` 里配的端口（如 7897）是死的。

> 经验：HTTPS 推送「fetch 偶尔通、push 稳定失败」且重试 4+ 次全挂时，基本可判定是
> DNS 污染而非抖动，直接跳到 SSH 方案，别在 HTTPS 重试上耗时间。
