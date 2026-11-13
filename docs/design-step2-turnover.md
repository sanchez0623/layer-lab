# 漏斗回测历史换手率 — 详细设计（第 2 步，待审）

> 状态：**待用户审批**。本文档是第 2 步（选项 A）的交付物，审通过后按此实现。
> 不改动任何核心代码。

## 1. 背景与目标

漏斗轮动回测的做 T 准入（`_t_eligible`）需要「当日换手率」，但：
- quan 分钟/日线 parquet **无换手率字段**（`turnover_rate` 恒 None）
- 东财系（AkShare/efinance/直连）在服务器和本地**都被封**（实测 RemoteDisconnected）
- 模拟盘走实时快照（腾讯字段[38]）有换手率，但回测是历史、无实时快照

**目标**：给漏斗回测做 T 准入补上「当日日线换手率」，使其能真正触发。

## 2. 已验证的事实

- BaoStock 日线 `turn` 字段可用（POC 实测 300251 整年 250 天，反推股本三天一致）
- 用日线换手率口径，300251 整年 **38 天**满足做 T 准入（5 分钟口径 0 天）
- BaoStock **禁止并发**、需串行登录（universe.py 已有单 login + RLock 模式可复用）
- 服务器可连通 BaoStock（POC 在服务器实测通过）

## 3. 方案：本地预生成 turn parquet，随包部署

**核心思路**：本地（网络可通 BaoStock）预生成 `turnover_daily.parquet`，随部署包带上服务器。
服务器运行时只读本地 parquet，**不联网、不碰 BaoStock 并发风险**。

### 3.1 数据准备脚本（本地跑，不入部署包）

新增 `backend/scripts/gen_turnover_parquet.py`：
- 遍历 `turnover_daily.parquet` 需覆盖的标的（双创全部，~2000 只）
- 对每只串行调 BaoStock `query_history_k_data_plus(frequency="d")` 拉 `date,turn`
- 落 parquet：列 `date, code, turnover`（turn 为小数，如 0.0421）
- 幂等：已存在的 (code,date) 跳过，支持增量

输出：`data/turnover_daily.parquet`（随部署包）

### 3.2 读取改动（`quan_data.py`）

`QuanStore.load_minute5` 已支持 `daily_turnover: dict[date, float]` 注入（第 1 步已加）。
新增：从 `turnover_daily.parquet` 读当日换手率填充该 dict。

- 加载 parquet 为 `{date: turnover}`（全标的共用，按 date 查）
- 传给现有 `daily_turnover` 参数

### 3.3 漏斗回测数据流（`backtest.py`，已部分就绪）

- `_prepare_data`：加载 `turnover_daily.parquet` 到 `self._turnover_by_date`
- `on_intraday` 调用处：`live_turnover=bar.turnover_rate`（日线 bar 已注入 turn）
- 已验证：注入后 `_t_eligible` 返回 True（POC）

### 3.4 与模拟盘的边界（架构原则 1）

- **共用**：`_t_eligible` 的换手率兜底逻辑（`live_turnover` 参数）——模拟盘传实时快照值，回测传日线 turn 值，同一套准入逻辑
- **不共用**：数据来源（模拟盘=实时快照，回测=parquet），因两者时间口径本质不同

## 4. 改动清单

| 文件 | 改动 | 风险 |
|---|---|---|
| `backend/scripts/gen_turnover_parquet.py` | 新增，本地生成 parquet | 低（独立脚本）|
| `backend/app/quan_data.py` | `load_minute5` 读 turnover parquet | 低（已支持 daily_turnover 注入）|
| `backend/app/backtest.py` | `_prepare_data` 加载 turn parquet；`on_intraday` 传参（已就绪）| 低 |
| `backend/tests/` | 新增 turn 注入 + 做 T 触发单测 | 低 |
| `data/turnover_daily.parquet` | 随包部署 | 低（只读数据）|

## 5. 验收

1. 本地 `gen_turnover_parquet.py` 生成 parquet，覆盖双创 ~2000 只
2. 漏斗回测 1 年，做 T 触发天数 > 0（POC 预估 300251 约 38 天）
3. 后端全量 pytest 通过 + 新增单测
4. 部署服务器，容器内跑漏斗回测确认不 OOM 且做 T 触发

## 6. 风险

- BaoStock 串行拉 ~2000 只可能慢（估 30-60 分钟），本地跑一次即可
- parquet 体积（~2000 只 × 250 天 ≈ 50 万行）约 10-20MB，可接受
- 不改动做 T 准入的阈值语义（振幅/换手门槛不变），只补数据源

## 7. 待确认

- 是否接受「做 T 准入换手率改用当日日线口径」（与模拟盘实时快照同口径）？
- parquet 覆盖范围：双创全部，还是仅回测池内标的？
