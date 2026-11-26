# layer-lab 项目记忆（跨会话约定）

本文件是给 AI agent 的持久项目记忆，跨会话读取。改动代码前先看这里的约定。

> **本文件已瘦身**：只保留「每会话必记的铁律」+「按需加载索引」。详细内容下沉到
> `docs/agents/` 下的主题文档，**按任务类型加载，不要全量读**。

## 铁律（每会话必记）

- **部署**：任何改动（后端/前端/配置）本地验证通过后，必须同步部署到阿里云 ECS 让线上生效。
  不要只改本地就收工。用户明确说「只改本地先验证」时才可暂缓。**部署到服务器的内容必须来自 `main` 分支**——
  只能部署已合入 `main` 的代码，禁止把 `feat/` `bugfix/` `ui/` 等未合回的分支直接部署上线。详见 `docs/agents/deploy.md`。
- **分支**：不直接在 `main` 改代码。开发/改UI/修bug 一律从最新 `main` 切新分支
  （前缀 `feat/` / `bugfix/` / `ui/`），验证后合回 `main` 再部署。详见 `docs/agents/git-workflow.md`。
- **并行开发隔离**：有多个并行任务时，用 `git worktree` 为每个任务建独立工作目录（各自从最新 `main` 切分支），
  互不干扰、可同时改。合并上线时必须把**所有并行任务的功能全部合回 `main` 后再统一部署**，
  不能只上一个就发版。详见 `docs/agents/git-workflow.md`。
- **版本号**：功能改动同步 bump，三处一起改保持一致：
  `backend/app/__init__.py` 的 `__version__`、`frontend/package.json` 的 `version`、
  `backend/app/main.py` 里 `FastAPI(app, version=...)`。
  小改动 patch +1，大功能 minor +1，重构/不兼容 major +1，拿不准先问。
- **测试**：涉及资金/撮合/规则判定的改动必须补 pytest 单测，本地 `pytest` 过了再合、再部署。
- **及时推送**：改完功能及时 commit 并 push 到 `origin`（GitHub），push 触发 CI，红了先修再继续。

## 架构原则（硬约束，全文见 `docs/agents/conventions.md`）

1. **实盘与回测共用同一套逻辑（单一真相源）**——不许维护两份平行实现。
2. **配置项尽量可视化可编辑，收敛到统一模块**。
3. **交易进行中的逻辑要「定格」**——开仓时快照配置，全局改动不回填在途交易。
4. **重要功能必须有单元测试**。

## 按需加载索引

| 当你要… | 读这个 |
|---|---|
| 部署 / 改配置 / 碰服务器 / HTTPS 证书 / 数据库 | `docs/agents/deploy.md` |
| commit / push / 开分支 / 版本号 / 解冲突 / git 凭据 / 行尾 CRLF | `docs/agents/git-workflow.md` |
| 改交易撮合 / 做T / 止盈 / 加仓 / strategy_state / baostock | `docs/agents/domain-trading.md` |
| 新增列表接口 / 分页，或对齐架构原则全文 | `docs/agents/conventions.md` |
| 通用工程设计原则（SOLID/KISS/DRY 等，非项目特定，可选读） | `docs/agents/design-principles.md` |

## README 更新口径

大、中功能变动（新增/重构模块、新增端点、改数据流、改部署口径）如有必要就更新 README，
保持它是**最新可用**的描述；修 bug、调 UI 样式不用动。判定口径：一个外部使用者照 README
能否正确理解和跑起来——若变动让它过时了就改。项目根目前**没有 README.md**（只有 `AGENTS.md`）；
若要落地 README，先跟用户确认放哪、写什么范围，别擅自生成大而全的文档。
