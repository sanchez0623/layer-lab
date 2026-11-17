"""修复 2（Armed 量价背离口径）的回归哨兵。

核心口径（已批准，勿重设计）：
- 盘中不再把「当前 5 分钟 bar」与「日线序列 + m5 复制 bar」混喂 _risk_requests，
  而是复用 _today_daily_bar 合成一根「今日日线」（量按 risk_volume_caliber 折算成
  日量当量、high/low 用当日 running 极值、close=当前价），拼成纯日线序列再喂。
- _risk_requests 本体与 on_daily 不改动，日线路径行为零变化。
- recent_long_upper 从「当日 latched」改成「按最近 5 根窗口重算」（P2）。
"""

from datetime import datetime, timedelta

import pytest

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig
from app.strategy import LayeredTStrategy


DAY0, TODAY = datetime(2025, 1, 1), datetime(2025, 1, 21)


def daily(day: int, close: float = 10.00, low: float | None = None,
          volume: float = 1_000_000) -> Bar:
    """日线 fixture：open=close，high=close×1.02，low=close×0.95（或显式 low）。

    默认（close=10.0）→ high=10.20 / low=9.50 / volume=1e6，与方案报告测试 6 一致。
    """
    low = low if low is not None else close * 0.95
    return Bar(
        timestamp=(DAY0 + timedelta(days=day)).replace(hour=15), symbol="TEST",
        open=close, high=close * 1.02, low=low, close=close,
        volume=volume, amount=close * volume, prev_close=close,
    )


def m5(hour: int, minute: int, *, high=10.12, low=10.08, close=10.10,
       volume=20_000) -> Bar:
    """5 分钟 fixture：open=high（默认无上影），close 默认 10.10。"""
    return Bar(
        timestamp=TODAY.replace(hour=hour, minute=minute), symbol="TEST",
        open=high, high=high, low=low, close=close, volume=volume,
        amount=close * volume, timeframe="5m", prev_close=close,
    )


def armed_strategy(**overrides) -> LayeredTStrategy:
    config = StrategyConfig(slippage_bps=0, volume_participation=1.0, **overrides)
    strategy = LayeredTStrategy(config)
    strategy.armed = True
    return strategy


def armed_account() -> BrokerAccount:
    """已建仓账户：1000 股 @9.0（成本 9000）。

    价格 10.10 时 price_return = (10100-9000)/9000 ≈ 12.2% ≥ 10% → Armed 保持；
    10.00 时 = 11.1% ≥ 10% → 仍 Armed；均未到 15% 止盈。
    """
    config = StrategyConfig(slippage_bps=0, volume_participation=1.0)
    account = BrokerAccount(4_000_000, config)
    account.new_day("2024-12-31")
    account.submit(OrderRequest(symbol="TEST", side="BUY", quantity=1000,
                                execution_mode="NEXT_BAR"))
    account.process_bar(daily(-1, 9.0))
    account.new_day("2025-01-06")
    account.sequence_index = 1
    return account


def today_recent(bar: Bar, count: int = 33, volume: float = 20_000) -> list[Bar]:
    """当日 5 分钟序列：k=1..count-1 生成 09:35 起逐根 + 决策 bar（末尾）。"""
    bars = [m5(9 + (30 + 5 * k) // 60, (30 + 5 * k) % 60, volume=volume)
            for k in range(1, count)]
    bars.append(bar)
    return bars


def spy_risk_requests(strategy: LayeredTStrategy) -> dict:
    """用 spy 包一层 _risk_requests，记录传入的 risk view。"""
    seen: dict = {}
    orig = strategy._risk_requests

    def wrapped(risk_history, risk_bar, account, recent, mode):
        seen["history_len"] = len(risk_history)
        seen["history_volumes"] = [b.volume for b in risk_history]
        seen["history_timeframes"] = [b.timeframe for b in risk_history]
        seen["history_last_volume"] = risk_history[-1].volume
        seen["bar_volume"] = risk_bar.volume
        seen["bar_high"] = risk_bar.high
        seen["bar_close"] = risk_bar.close
        return orig(risk_history, risk_bar, account, recent, mode)

    strategy._risk_requests = wrapped  # type: ignore[method-assign]
    return seen


def test_intraday_volume_item_not_true_on_single_m5_volume():
    """回归哨兵（最重要）：缩量项必须用「当日累计量折算成日量当量」，不是单根 5 分钟量。

    修复前 on_intraday 把决策 bar（单根 5m，volume=20000）直接当 bar 喂 _risk_requests，
    缩量项 = 20000 < 前5日均量×0.8 → 恒 True（白送一票）；修复后折算成 960000 ≥ 800000
    → 缩量项 False。同时验证喂给 _risk_requests 的是**纯日线**序列（无 5m bar）。
    """
    history = [daily(i) for i in range(20)]
    bar = m5(14, 15)
    recent = today_recent(bar, 33, 20_000)  # 累计 660,000
    strategy = armed_strategy()
    account = armed_account()
    seen = spy_risk_requests(strategy)
    strategy.on_intraday(history, recent, bar, account)

    # 核心：折算成日量当量 = 660000 × 48/33 = 960000，而非单根量 20000。
    assert seen["bar_volume"] == pytest.approx(960_000)
    assert seen["history_last_volume"] == pytest.approx(960_000)
    # 纯日线序列：20 根历史日线 + 1 根合成今日日线，无任何 5m bar。
    assert len(seen["history_timeframes"]) == 21
    assert seen["history_timeframes"].count("5m") == 0
    assert seen["history_timeframes"][-1] == "daily"
    # 纯函数折算验证。
    assert strategy._daily_volume_equivalent(660_000, 33, "annualized") == pytest.approx(960_000)
    assert strategy._daily_volume_equivalent(660_000, 33, "annualized") >= 800_000
    # 缩量项 False（且 is_20d_high 因 daily 高点 10.20 > 合成 high 10.12 为 False）→ 不累计背离。
    assert strategy.divergence_count == 0


def test_intraday_volume_item_true_when_day_is_genuinely_light():
    """真缩量日（累计 297,000 → 折算 432,000 < 800,000）→ 缩量项 True。

    同时 is_20d_high（running high 10.60 ≥ 前 20 日最高 10.20）成立，缩量 + 高回撤
    两项同真 → 背离计数累计 1。
    """
    history = [daily(i) for i in range(20)]
    bar = m5(14, 15, high=10.60, low=9.98, close=10.10, volume=9_000)
    recent = today_recent(bar, 33, 9_000)  # 累计 297,000
    strategy = armed_strategy()
    account = armed_account()
    seen = spy_risk_requests(strategy)
    strategy.on_intraday(history, recent, bar, account)

    # 297000 × 48/33 = 432000 < 800000 → 缩量 True。
    assert seen["bar_volume"] == pytest.approx(432_000)
    # 纯函数折算：任务给 300,000 → 436,363.6。
    assert strategy._daily_volume_equivalent(300_000, 33, "annualized") == pytest.approx(436_363.6)
    # running high = 10.60（≥ 20 日高点 10.20）→ 20 日新高成立。
    assert seen["bar_high"] == pytest.approx(10.60)
    # 缩量 True + 高回撤 True（(10.60-10.10)/10.10=4.95% ≥ threshold）→ 两项同真 → 累计。
    assert strategy.divergence_count == 1


def test_intraday_volume_item_degrades_false_on_single_bar():
    """单根 5 分钟 bar（elapsed=1）时不能折算，缩量项不误触发背离。

    只有 1 根已收盘 5m bar 时无法可靠判定全天量能（_daily_volume_equivalent 不参与，
    elapsed<2 保持累计量）；is_20d_high 不成立（daily 高点 10.20 > 单根 high 10.12）
    兜底保证 divergence_count 不累计。
    """
    history = [daily(i) for i in range(20)]
    bar = m5(14, 15)
    strategy = armed_strategy()
    account = armed_account()
    assert strategy._intraday_bars_elapsed(bar, [bar]) == 1
    strategy.on_intraday(history, [bar], bar, account)
    assert strategy.divergence_count == 0


def test_atr_series_excludes_intraday_bar_in_intraday_path(monkeypatch):
    """ATR 必须用纯日线序列（20 根历史日线 + 1 根合成今日日线），无任何 5m bar。

    修复前 on_intraday 把决策 bar（timeframe=5m）混入 history 喂 _atr，导致日线 ATR
    被 5 分钟 bar 的振幅污染。
    """
    history = [daily(i) for i in range(20)]
    bar = m5(14, 15)
    recent = today_recent(bar, 33, 20_000)
    strategy = armed_strategy()
    account = armed_account()
    seen: list = []
    orig = strategy._atr

    def spy(hist, period):
        seen.append(list(hist))
        return orig(hist, period)

    monkeypatch.setattr(strategy, "_atr", spy)
    strategy.on_intraday(history, recent, bar, account)

    assert seen, "_atr 应被调用（armed 且 len>=20）"
    assert "5m" not in [b.timeframe for b in seen[0]]
    assert seen[0][-1].timeframe == "daily"
    assert len(seen[0]) == 21  # 20 根历史日线 + 1 根合成今日日线


def test_daily_path_untouched():
    """回归哨兵：on_daily 行为零变化。

    - 核心：价格轨 15% 止盈立即清仓（close_reason 逐字不变）。
    - ATR：日线路径用当日完整日线（len == len(history)，末位 timeframe=daily）。
    """
    account = armed_account()
    strategy = LayeredTStrategy(account.config)
    strategy.last_add_price = 9.0
    strategy.last_add_session = 0
    requests = strategy.on_daily([daily(1, 11.6)], [], account, 1, False)
    assert any(item.side == "SELL" and item.purpose == "PERMANENT" for item in requests)
    assert strategy.liquidating is True
    assert strategy.close_reason == "价格轨收益达到15%"
    assert all(item.execution_mode == "NEXT_OPEN" for item in requests)

    # ATR 场景：20 根日线 + 浮盈 11.1%（Armed 区间，未到 15%）→ _atr 收到纯日线。
    strategy2 = LayeredTStrategy(account.config)
    strategy2.armed = True
    history = [daily(i) for i in range(20)]
    seen: list = []
    orig = strategy2._atr

    def spy(hist, period):
        seen.append(list(hist))
        return orig(hist, period)

    strategy2._atr = spy  # type: ignore[method-assign]
    strategy2.on_daily(history, [], account, 20, False)
    assert seen
    assert len(seen[0]) == 20  # 当日完整日线在列，无截断
    assert seen[0][-1].timeframe == "daily"
    assert all(b.timeframe == "daily" for b in seen[0])


def test_high_drawdown_threshold_before_after():
    """上影回撤阈值：ATR 用纯日线（fixed）vs 混入 5m bar（mixed）的差异。

    已实测：mixed ATR=0.658571 → threshold=0.026082，old_dd=(10.12-10.10)/10.10=0.198%
    → False；fixed ATR=0.694286 → threshold=0.027496，new_dd=(10.60-10.10)/10.10=4.95%
    → True。差异 0.035714。
    """
    history = [daily(i) for i in range(20)]  # high=10.20, low=9.50, close=10.00, vol=1e6
    bar_m5 = m5(14, 15, high=10.12, low=10.08, close=10.10)  # 当前 5m bar（旧路径混入）
    # 当日早盘冲高 bar：running high/low 的来源（high=10.60, low=9.98）。
    running = Bar(timestamp=TODAY.replace(hour=10, minute=30), symbol="TEST",
                  open=10.10, high=10.60, low=9.98, close=10.10, volume=20_000,
                  amount=10.10 * 20_000, timeframe="5m", prev_close=10.10)
    recent = [running, bar_m5]
    strategy = armed_strategy()
    # 旧口径：history 混入决策 5m bar（high=10.12）。
    mixed = list(history) + [bar_m5]
    atr_mixed = LayeredTStrategy._atr(mixed, 14)
    # 新口径：合成今日日线（running high=10.60 / running low=9.98 / 当前 close）。
    today_bar = strategy._today_daily_bar(history, bar_m5, recent, volume_caliber="annualized")
    fixed = list(history) + [today_bar]
    atr_fixed = LayeredTStrategy._atr(fixed, 14)

    assert atr_mixed == pytest.approx(0.658571)
    assert atr_fixed == pytest.approx(0.694286)
    assert atr_fixed - atr_mixed == pytest.approx(0.035714, abs=1e-6)

    threshold_old = min(0.03, max(0.008, 0.4 * atr_mixed / bar_m5.close))
    threshold_new = min(0.03, max(0.008, 0.4 * atr_fixed / today_bar.close))
    assert threshold_old == pytest.approx(0.026082, abs=1e-6)
    assert threshold_new == pytest.approx(0.027496, abs=1e-6)

    old_dd = (bar_m5.high - bar_m5.close) / bar_m5.close          # 用 m5 bar 的 high（10.12）
    new_dd = (today_bar.high - today_bar.close) / today_bar.close  # 用 running high（10.60）
    assert old_dd == pytest.approx(0.001980, abs=1e-6)
    assert not (old_dd >= threshold_old)                 # 旧口径：上影回撤不达标
    assert new_dd == pytest.approx(0.049505, abs=1e-6)
    assert new_dd >= threshold_new                        # 新口径：上影回撤达标


def _single_item_scenarios():
    """三组「恰好一项为 True」场景（is_20d_high 均成立）。返回 (bar, recent, 说明)。"""
    # 组 A：仅缩量 True（volume=9000 折算 432000<800000；close=10.40 高回撤 False；无上影）。
    bar_a = m5(14, 15, high=10.60, low=10.00, close=10.40, volume=9_000)
    recent_a = today_recent(bar_a, 33, 9_000)
    # 组 B：仅上影 True（volume=20000 折算 960000≥800000；close=10.40 高回撤 False；
    #       recent[-2] 手动插入长上影 bar）。
    bar_b = m5(14, 15, high=10.60, low=10.00, close=10.40, volume=20_000)
    recent_b = today_recent(bar_b, 33, 20_000)
    shadow = Bar(timestamp=TODAY.replace(hour=12, minute=10), symbol="TEST",
                 open=10.10, high=10.60, low=10.00, close=10.10, volume=20_000,
                 amount=10.10 * 20_000, timeframe="5m", prev_close=10.10)
    recent_b[-2] = shadow
    # 组 C：仅高回撤 True（volume=20000 缩量 False；close=10.10 高回撤 True；无上影）。
    bar_c = m5(14, 15, high=10.60, low=9.98, close=10.10, volume=20_000)
    recent_c = today_recent(bar_c, 33, 20_000)
    return [(bar_a, recent_a), (bar_b, recent_b), (bar_c, recent_c)]


def test_three_items_independently_controllable():
    """三选二各项独立可控：恰好一项为 True → 不累计；两项同真 → 累计。

    三项（缩量 / 上影 / 上影回撤）在 is_20d_high 成立时各自独立置位，验证
    「三选二」不是「某两项恒绑死」。
    """
    history = [daily(i) for i in range(20)]
    for bar, recent in _single_item_scenarios():
        strategy = armed_strategy()
        account = armed_account()
        strategy.on_intraday(history, recent, bar, account)
        # 恰好一项 True → sum=1 < 2 → 不累计。
        assert strategy.divergence_count == 0, f"一项为真时不应累计：{bar.timestamp}"

    # 两项同真（缩量 + 上影回撤）：bar_c 参数（close=10.10 高回撤 True）+ volume=9000 缩量 True。
    bar = m5(14, 15, high=10.60, low=9.98, close=10.10, volume=9_000)
    recent = today_recent(bar, 33, 9_000)
    strategy = armed_strategy()
    account = armed_account()
    strategy.on_intraday(history, recent, bar, account)
    assert strategy.divergence_count == 1


def test_divergence_count_three_triggers_liquidation():
    """连续 3 根不同 timestamp 的 5 分钟 K 均满足「两项同真」→ 计数 3 → 清仓。

    reason 必须含「背离」，与 on_daily 同构（Armed量价背离三选二_3根5分钟K线）。
    """
    history = [daily(i) for i in range(20)]
    strategy = armed_strategy()
    account = armed_account()
    last_requests = []
    # 三次调用，时间戳递增（14:15 / 14:20 / 14:25）。
    for minute in (15, 20, 25):
        bar = m5(14, minute, high=10.60, low=9.98, close=10.10, volume=9_000)
        recent = today_recent(bar, 33, 9_000)
        last_requests = strategy.on_intraday(history, recent, bar, account)
    assert strategy.divergence_count == 3
    assert strategy.liquidating is True
    assert "背离" in strategy.close_reason
    assert any(item.side == "SELL" and item.purpose == "PERMANENT" for item in last_requests)
    assert all(item.execution_mode == "NEXT_BAR" for item in last_requests)


def test_recent_long_upper_no_longer_latched():
    """P2 哨兵：recent_long_upper 按最近 5 根窗口重算，不再当日 latched。

    第 1 根 5m 为长上影、第 2~6 根形态正常。处理第 6 根时窗口为第 2~6 根（无上影）
    → recent_long_upper=False（修复前 `self.recent_long_upper or upper_shadow` 恒 True）。
    """
    history = [daily(i) for i in range(20)]
    first = Bar(timestamp=TODAY.replace(hour=9, minute=35), symbol="TEST",
                open=10.10, high=10.60, low=10.00, close=10.10, volume=20_000,
                amount=10.10 * 20_000, timeframe="5m", prev_close=10.10)
    recent = [first] + [m5(9 + (40 + 5 * k) // 60, (40 + 5 * k) % 60)
                        for k in range(5)]
    bar = recent[-1]
    strategy = armed_strategy()
    account = armed_account()
    strategy.on_intraday(history, recent, bar, account)
    # 第 1 根长上影仍在 recent 里，但不在最近 5 根窗口内 → 不 latched。
    assert strategy.recent_long_upper is False


def test_config_validation_rejects_bad_caliber():
    """非法 caliber / ratio 必须在源头 400（__post_init__），不让缩量项悄悄失效。"""
    with pytest.raises(ValueError):
        StrategyConfig(slippage_bps=0, volume_participation=1.0,
                       risk_volume_caliber="linear")
    with pytest.raises(ValueError):
        StrategyConfig(slippage_bps=0, volume_participation=1.0,
                       risk_volume_ratio=0.0)
    with pytest.raises(ValueError):
        StrategyConfig(slippage_bps=0, volume_participation=1.0,
                       risk_volume_ratio=1.5)
    # 合法值通过。
    cfg = StrategyConfig(slippage_bps=0, volume_participation=1.0,
                         risk_volume_caliber="debiased", risk_volume_ratio=0.7)
    assert cfg.risk_volume_caliber == "debiased"
    assert cfg.risk_volume_ratio == 0.7


def test_backtest_and_live_feed_identical_risk_view():
    """对称性哨兵：回测式与 live 式两条喂法产生同一 risk view。

    _intraday_bars_elapsed 只数「当日已收盘 5m bar」，不读墙钟：回测
    （backtest.py 决策前 append m5）与 live-tick（main.py 只保留 timestamp<=now 的
    当日 bar）两条路径得到同一个 elapsed，从而 _today_daily_bar 的折算一致。
    """
    history = [daily(i) for i in range(20)]
    bar = m5(14, 15, high=10.60, low=9.98, close=10.10)
    # live 式：recent 已含决策 bar（timestamp<=now 过滤）。
    recent_live = today_recent(bar, 33, 20_000)
    # 回测式：决策前 append m5 → recent 也含决策 bar（同 live 式构成）。
    recent_backtest = list(recent_live)

    strategy = armed_strategy()
    # 决策 bar 在不在 recent，elapsed 都归一为 33（+1 补偿）。
    assert strategy._intraday_bars_elapsed(bar, recent_live) == 33
    assert strategy._intraday_bars_elapsed(bar, recent_backtest) == 33
    assert strategy._intraday_bars_elapsed(bar, recent_backtest[:-1]) == 33  # 决策 bar 未入 recent

    views = []
    for recent in (recent_live, recent_backtest):
        s = armed_strategy()
        acct = armed_account()
        seen = spy_risk_requests(s)
        s.on_intraday(history, recent, bar, acct)
        views.append((seen["history_last_volume"], seen["bar_volume"], seen["bar_high"]))
    assert views[0] == views[1]
