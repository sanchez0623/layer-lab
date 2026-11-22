# 领域要点：交易撮合 / 做T / 止盈 / 加仓

> **何时读我：改交易撮合/做T/止盈/加仓/strategy_state/baostock时**
>
> 本文是从 `AGENTS.md` 抽出的领域要点留档，收录每条的结论、关键约束、参数默认值、涉及文件/函数名与关联单测名。长篇事故复盘压缩为一行结论 + 指向对应单测，不复述故事。改这些逻辑前先对照本页，避免重复踩坑。

## 撮合与申报

- **T+1 可卖**：`broker.new_day` 只在真正跨交易日时清零 `today_*_buy_qty`；撮合每根 K 线都会调它，别无条件 reset。
- **分板块申报口径**：股数计算必须走 `trading_rules.rules_for(symbol)` 的 `min_qty`/`step`（科创板 200/1，创业板·主板 100/100），不要写死 100 股整手。
- **16 笔序列**：高价股按「方案 2」——金额不足最小申报也按最小申报建仓（`board_buy_quantity`），宁可略微透支该笔预算也不跳过。

## 止盈

- **止盈**：价格轨 / 账户轨各 **15%** 立即清；**10%~15%** 进 Armed 区间，内含**量价背离**、**VWAP 回撤 4%** 两个辅助清仓点。

## 做T

- **做 T 退出（净利止盈 + T+1 硬截止）**
  - 平 T 目标价 = T 仓均价 ×(1 + `t_target_net_spread` + `_t_roundtrip_cost()`)，即**扣完成本后净赚 `t_target_net_spread`（默认 3%）**才平，不是毛价差。
  - 成本率由 `_t_roundtrip_cost()` 统一估算（双边佣金/经手费/证管费/过户费 + 卖出印花税 + 双边滑点 ≈0.1%）；准入门槛 `_t_eligible` 复用同一口径。
  - 资金硬截止：**T+1 日 `t_exit_cutoff`（默认 14:50）仍未止盈则无条件强平**（reason「T+1尾盘截止平T」）。
  - 用 `position.t_qty > today_t_buy_qty` 区分隔夜仓（T1）与当日仓（T0）——**T0 当日不强平**可留到 T1，保证做 T 资金最迟 T+1 收盘前了结。
  - 两个参数都在模拟盘做 T 面板可视化编辑。
  - ⚠️ 旧的 `t_target_spread`（毛价差）**已废弃**，不再驱动平 T，仅保留字段兼容老账户快照，别再读它。
- **做T准入的四阈值已可视化（2.5.0 起）**
  - `t_min_amplitude`(4%) / `t_min_turnover`(3%) / `t_max_turnover`(12%) / `t_min_expected_net_spread`(3%)。
  - 已进 `AccountConfigRequest`（可局部更新）+ 回测页 FormGrid + 模拟盘「做 T 准入门槛」块。
  - `StrategyConfig.__post_init__` 校验 `0 <= t_min_turnover <= t_max_turnover <= 1`、振幅/期望净利在 (0,1]——自相矛盾的值必须在源头 400，而不是让做T准入悄悄恒 False。
  - 单测：`backend/tests/test_paper_t_live_wiring.py`。
- ⚠️ **做T准入的换手率/盘口只有回测与模拟盘两条喂法，别再加第三条**
  - 回测：`quan_data.load_minute5` 的代理值（bid/ask = close±0.1%、换手取当日日线）+ `backtest.py` 显式传 `daily_amplitude`/`daily_turnover_rate`。
  - 模拟盘 live-tick：`main.apply_bar` 把**实时快照** bar 的 `turnover_rate`/`bid`/`ask` 经 `live_turnover`/`live_bid`/`live_ask` 传下去（腾讯只把快照挂在最后一根未完成 5 分钟 bar 上，那根会被 `recent_bars` 的 timestamp 过滤丢掉，所以**已收盘 bar 上恒无**这两个值）。
  - 事故一行结论：`40f24d6` 加了这三个入参 + 单测却没接 `apply_bar` 的调用点，导致模拟盘做T准入恒 False（架构原则 1 的隐性违背）——改做T准入前务必确认「这条路径的值从哪来」。
- **深跌幅当日做T（`t_deep_drop_pct`，默认 0.10）**
  - 当日盘中跌幅（相对前收）**≥ 阈值**时绕过底仓笔数门槛（sequence_index≥4）允许做T（20cm 板极端恐慌日抢反弹，退出仍走净利止盈 + T+1 尾盘强平）。
  - 阈值**大于 0 即可开启**（2026-11-22 起取消原「必须 ≥10%」下限，可设 10% 以下低门槛回测）；None/0 = 关闭；`__post_init__` 校验开启时须 (0,1]。模拟盘做T面板与回测页均可视化编辑。

## 配置校验

- **「前端 tips 先提示，后端 400 兜底」**：前端 `frontend/src/lib/configValidation.ts` 与 `domain.py::StrategyConfig.__post_init__` 同口径预校验——字段下方红字 tips + 有错时**暂停自动保存**并阻止运行/应用到模拟盘，真出 400 时把后端 detail 原样显示。改约束时两处同步（后端仍是唯一真相源）。
- ⚠️ **幽灵配置键**：服务器历史回测报告（如 P5-base 00e01918）的 config 里可能含 `add_tier_strict`/`t_deep_drop_pct` 键——2026-11-16 起它们已是合法字段；更早的报告若还带其他未知键，`StrategyConfig.from_dict` 会拒绝（400），导入前需剥离。

## 加仓

- **加仓按档位「先松后紧」（`add_tier_strict`，默认开）**
  - A3 浮亏加仓信号按 `sequence_index // 4` 分 4 档逐级叠加条件：档1 最松（浮亏+缩量<5日均量）→ 档2 +收盘站回前3日低点 → 档3 +此前破过新低 → 档4 最严（缩量须 < 5日均量×0.5，= 原始 A3）。
  - 浮盈右侧回撤 5% 加仓不受档位影响。关闭（`add_tier_strict=false`）= 全档统一原始 A3（向后兼容）。
  - 哨兵单测：`test_tiered_a3_spec_progression`（规范是「档4 才收紧 ×0.5」，不是 reflog 恢复的 3d91e4a 旧实现）。
- **加仓间隔按成交日期动态换算**
  - 「间隔 ≥ `min_add_gap_days` 交易日」判定用 `_add_gap_blocked`——以最近一笔永久仓成交日为基准，按 `history ∪ {bar}` 中 (成交日, bar.date] 的交易日数计算；旧行序号口径（`session_index − last_add_session`）仅在账户无成交记录时兜底。
  - 事故一行结论：2026-11-16 第二次联特误拦——数据集刷新去重/补数让行序号整体移位（当日 session_index=240 vs 存储值 241 → gap=−1 误拦），日期口径天然免疫。

## strategy_state

- **单一来源 + 不变式自愈**
  - 策略状态字段清单收敛在 `strategy.py` 的 `STRATEGY_STATE_FIELDS`（`state_dict`/`load_state` 由它生成，新增字段漏登记会被 `test_strategy_state.py` 拦下）；落库结构带 `state_version`。
  - `ensure_round_state` 在每个 live-tick 载入状态后校验不变式「sequence_index>0 ⇒ last_add_price / last_add_session / round_entry_date 非空」，违反时从最近 sequence_index 笔永久仓买入自愈并打 `[strategy-state]` 日志；无法自愈（无成交记录）时告警。
  - 事故一行结论：2026-11-16 的 64f692bb 卡死（有持仓但 last_add_price=null，加仓永远不发）就是缺这道防线的教训。
  - 另：关闭自动策略只暂停调度、不清状态（`/reset` 才是重开一轮）。

## baostock

- **禁止并发**：baostock 数据源不能并发访问，并发会被服务端拉黑（封 IP/账号）。任何调用 baostock 的路径必须串行化。
  - `universe.py` 已做对：单 login 复用全部查询 + `threading.RLock` 串行 + 后台单线程回补（`_relogin` 定时轮换）。
  - ⚠️ `market.py` 是隐患：`fetch_baostock_daily` / 两处 `fetch_*_index` 各自独立 `bs.login()`（约 317/378/431 行），**没有全局锁**；调度线程（paper-scheduler）与用户触发请求并发打 baostock 会撞车。新增任何 baostock 调用前，先确认已被全局锁串行化，或复用 `universe.py` 的会话，别再各开 login。
  - 默认数据源已是 quan_parquet（只读本地 parquet，无并发限制），只有回落旧链路时才真正打 baostock——回落路径同样要串行。
