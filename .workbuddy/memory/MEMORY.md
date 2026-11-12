# LayerLab 项目长期记忆

- 项目：A股分层加仓做T模拟交易与回测系统；仅本地仿真，不接券商。
- 端口：后端 FastAPI `127.0.0.1:8010`；前端 Vite `127.0.0.1:5180`。不要擅自改端口。
- 数据链路（用户 2026-11-11/12 指示）：baostock 前复权日线 + 腾讯实时快照/1分钟K线；不要把 CSV 日线导入作为主链路。
- 盘中扫描口径：1 分钟，不用 10 秒 tick；信号去抖为连续 3 根 1 分钟 K 线。
- baostock 不覆盖 `sh.000688`；科创50指数日线使用腾讯 `fqkline/get` 兜底，来源标记 `tencent_index`。
- 腾讯 1 分钟接口实测约 320 根；长周期回测的盘中精度有限，必须在 warnings/data_info 中明示。
- SQLite 中模拟账户订单/成交持久化键使用 `account_id:order_id`；本地单号跨账户复用，撤单必须带 `account_id`（`POST /api/paper/accounts/{id}/orders/{order_id}/cancel`）。
- 后端 API 枚举为大写（`BUY/SELL`、`MARKET/LIMIT`、`FILLED`、`ACTIVE`），前端 UI 状态为小写；上行在 `api.placeOrder` 归一化，下行比较统一用 `format.ts` 的 `lower()`。
- 腾讯实时快照 Bar 的 `open/high/low/volume` 是**全天口径**（今开/当日高低/当日累计量）；实时撮合必须经 `snapshot_execution_bar()` 转成按现价成交的 1 分钟 bar，策略信号仍用原始 bar，回测链路不变。
- 持仓市值/浮盈必须用后端 `total_cost`/`market_value`/`float_pnl`；`average_cost` 已按分四舍五入，只用于展示，禁止拿它反算金额。
- 模拟盘自动策略复用回测 LayeredTStrategy，状态持久化在 `strategy_state`；大盘门方向必须是指数20日跌幅<0时开门。
- 涨红跌绿；金额 CNY；演示/真实来源必须在 UI 和报告中明确标注。
- 交易费用口径（2026-11-12 用户截图指定）：五项分项相加 = 佣金 + 印花税(仅卖出) + 经手费 + 证管费 + 过户费，佣金/经手/证管/过户双边；费率 UI/API 单位统一「万分之」，结算内部用小数比例；最低佣金（元/笔）**只兜底佣金项**；默认值 0.5 / 5 / 0.341 / 0.2 / 0.1 万分 + 最低 5 元；滑点体现在成交价，不计入 total_fees。
- 费用配置是**全局可自定义**的：SQLite `app_settings` 表 key `fee_config_v1`，`GET/PUT /api/fees` + `/api/fees/reset`，已有模拟账户用 `POST /api/paper/accounts/{id}/fees` 同步（仅对此后成交生效）；回测/建账户请求可用 `fees` 局部覆盖，优先级 全局 < 本次 fees < 显式 config。
- 后端服务改动后必须重启 uvicorn 才生效（8010 常驻进程 reload=False）；不要擅自重启盘中服务，需提示用户。
