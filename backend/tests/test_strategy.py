from datetime import datetime, timedelta

import pytest

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig
from app.strategy import LayeredTStrategy


def daily(day: int, close: float, low: float | None = None, volume: float = 1_000) -> Bar:
    low = low if low is not None else close * 0.95
    return Bar(
        timestamp=datetime(2025, 1, 1) + timedelta(days=day), symbol="TEST",
        open=close, high=close * 1.02, low=low, close=close,
        volume=volume, amount=close * volume, prev_close=close,
    )


def funded_account(price: float = 10.0) -> BrokerAccount:
    config = StrategyConfig(slippage_bps=0, volume_participation=1.0)
    account = BrokerAccount(4_000_000, config)
    account.new_day("2024-12-31")
    account.submit(OrderRequest(symbol="TEST", side="BUY", quantity=1_000, execution_mode="NEXT_BAR"))
    account.process_bar(daily(-1, price))
    account.new_day("2025-01-01")
    account.sequence_index = 4
    return account


def test_index_gate_opens_when_index_20d_return_is_negative():
    account = BrokerAccount(4_000_000, StrategyConfig(slippage_bps=0, volume_participation=1.0))
    strategy = LayeredTStrategy(account.config)
    history = [daily(1, 10.0)]
    index_history = [
        Bar(timestamp=datetime(2025, 1, 1) + timedelta(days=day), symbol="INDEX",
            open=100, high=101, low=99, close=100, volume=1000, amount=100000)
        for day in range(21)
    ]
    for item in index_history[:-1]:
        item.close = 110.0
    index_history[-1].close = 100.0
    requests = strategy.on_daily(history, index_history, account, 1, False)
    assert len(requests) == 1
    assert requests[0].reason == "换票日首笔"
    assert requests[0].execution_mode == "NEXT_OPEN"


def test_oversold_a3_is_t_plus_one_without_lookahead():
    account = funded_account()
    strategy = LayeredTStrategy(account.config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    history = [daily(i, 10, low=9.5) for i in range(5)]
    history.append(daily(5, 9.0, low=8.8, volume=1_000))
    assert strategy.on_daily(history, [], account, 5, False) == []
    assert strategy.had_new_low is True

    initial_trade_count = len(account.trades)
    history.append(daily(6, 9.5, low=9.1, volume=100))
    requests = strategy.on_daily(history, [], account, 6, False)
    assert len(requests) == 1
    assert requests[0].reason == "浮亏超跌A3"
    assert requests[0].execution_mode == "NEXT_OPEN"
    expected_quantity = int(200_000 / 9.5 / 100) * 100
    assert requests[0].quantity == expected_quantity
    assert len(account.trades) == initial_trade_count

    # 下一交易日开盘价只影响成交，不影响已经形成的A3信号。
    next_open = daily(7, 9.2, low=9.0)
    order = account.submit(requests[0], history[-1].timestamp)
    fills = account.process_bar(next_open, modes={"NEXT_OPEN"})
    assert order.created_at == history[-1].timestamp.isoformat()
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(9.2)
    assert fills[0].timestamp == next_open.timestamp.isoformat()


def test_price_track_take_profit_liquidates():
    account = funded_account()
    strategy = LayeredTStrategy(account.config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    requests = strategy.on_daily([daily(1, 11.6)], [], account, 1, False)
    assert any(item.side == "SELL" and item.purpose == "PERMANENT" for item in requests)
    assert strategy.liquidating is True
    assert strategy.close_reason == "价格轨收益达到15%"
    assert all(item.execution_mode == "NEXT_OPEN" for item in requests)


def test_max_holding_days_overrides_take_profit_to_3pct():
    """持有超过 max_holding_days 交易日后，止盈阈值降为 holding_tp_override。

    回归：策略只有 15% 止盈 + Armed 量价清仓，没有硬止损；横盘/缓慢阴跌标的会让
    资金被长期套牢。降到 3% 让超期标的尽快让位给下一轮。
    """
    account = funded_account()
    config = StrategyConfig(
        slippage_bps=0, volume_participation=1.0,
        max_holding_days=22, holding_tp_override=0.03,
    )
    strategy = LayeredTStrategy(config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    strategy.round_entry_date = "2025-01-01"

    # 持有 20 天（未超期），浮盈 5%：未达 15%，也不该被 3% 提前清仓。
    history_short = [daily(i, 10.5) for i in range(1, 21)]
    assert strategy.on_daily(history_short, [], account, 20, False) == []
    assert strategy.liquidating is False

    # 持有 25 天（超期），同样 5% 浮盈：3% 阈值应立即触发清仓。
    strategy.liquidating = False
    strategy.close_reason = ""
    history_long = [daily(i, 10.5) for i in range(1, 26)]
    requests = strategy.on_daily(history_long, [], account, 25, False)
    assert any(item.side == "SELL" and item.purpose == "PERMANENT" for item in requests)
    assert strategy.liquidating is True
    # close_reason 带实际持有天数与降阈后的止盈百分比
    assert "3%" in strategy.close_reason
    assert "25" in strategy.close_reason


def test_max_holding_days_override_requires_both_fields():
    """只配 max_holding_days 不配 holding_tp_override：保持原止盈，不降阈。"""
    account = funded_account()
    config = StrategyConfig(
        slippage_bps=0, volume_participation=1.0, max_holding_days=22,
    )
    strategy = LayeredTStrategy(config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    strategy.round_entry_date = "2025-01-01"
    history = [daily(i, 10.5) for i in range(1, 30)]
    assert strategy.on_daily(history, [], account, 29, False) == []
    assert strategy.liquidating is False


def test_round_entry_date_survives_state_roundtrip():
    """state_dict/load_state 必须保留 round_entry_date：live 模拟盘重启后超期降阈仍生效。"""
    account = funded_account()
    strategy = LayeredTStrategy(account.config)
    strategy.round_entry_date = "2025-01-15"
    restored = LayeredTStrategy(account.config)
    restored.load_state(strategy.state_dict())
    assert restored.round_entry_date == "2025-01-15"


def test_holding_days_counts_only_bars_after_entry():
    account = funded_account()
    strategy = LayeredTStrategy(account.config)
    strategy.round_entry_date = "2025-01-05"
    history = [daily(i, 10.0) for i in range(1, 11)]
    # daily(i) 的 date = 2025-01-01 + i 天；i=1..10 → 01-02..01-11。
    # 严格晚于 01-05 的是 i=5..10 → 6 天（建仓次日起算）。
    assert strategy._holding_days(history) == 6
    strategy.round_entry_date = None
    assert strategy._holding_days(history) == 0


def test_account_track_includes_t_net_spread():
    account = funded_account()
    account.t_net_profit = 600.0
    strategy = LayeredTStrategy(account.config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    price_return = (
        account.position("TEST").permanent_qty * 11.0
        - account.position("TEST").permanent_cost
    ) / account.position("TEST").permanent_cost
    assert price_return < 0.15
    requests = strategy.on_daily([daily(1, 11.0)], [], account, 1, False)
    assert strategy.liquidating is True
    assert strategy.close_reason == "账户轨收益达到15%"
    assert requests[0].side == "SELL"


def test_same_intraday_bar_is_only_evaluated_once():
    """live-tick 在同一根已收盘 5 分钟 K 线上重复轮询时，不能重复累加确认计数。"""
    account = funded_account()
    strategy = LayeredTStrategy(account.config)
    history = [daily(i, 11.2, low=10.64, volume=1_000) for i in range(5)]
    bar = daily(6, 11.2)
    bar.timestamp = bar.timestamp.replace(hour=10, minute=5)
    calls: list[Bar] = []
    strategy._risk_requests = lambda *args, **kwargs: calls.append(args[1]) or []  # type: ignore[method-assign]

    strategy.on_intraday(history, [bar], bar, account)
    strategy.on_intraday(history, [bar], bar, account)

    assert calls == [bar]
    assert strategy.last_intraday_timestamp == bar.timestamp


def test_armed_disables_t_entry():
    account = funded_account()
    strategy = LayeredTStrategy(account.config)
    strategy.armed = True
    history = [daily(i, 11.2, low=10.64, volume=1_000) for i in range(5)]
    for item in history:
        item.turnover_rate = 0.05
        item.high = 11.76
        item.low = 10.64
        item.prev_close = 11.2
    tick = daily(6, 11.2)
    tick.bid = 11.19
    tick.ask = 11.21
    tick.timestamp = tick.timestamp.replace(hour=10, minute=0)
    assert strategy.on_intraday(history, [tick], tick, account) == []


# ---------------------------------------------------------------------------
# 尾盘首仓（14:30–14:50，下一根 5 分钟 K 撮合）
# ---------------------------------------------------------------------------


def intraday_bar(hour: int, minute: int, close: float = 10.0, day: int = 6) -> Bar:
    bar = daily(day, close)
    return bar.__class__(
        timestamp=bar.timestamp.replace(hour=hour, minute=minute), symbol=bar.symbol,
        open=bar.open, high=bar.high, low=bar.low, close=bar.close,
        volume=bar.volume, amount=bar.amount, prev_close=bar.prev_close,
    )


def index_series(*, falling: bool = True, count: int = 21) -> list[Bar]:
    """21 根严格早于当日的指数日线；falling 时 20 日收益 < 0（大盘门开门）。"""
    start = datetime(2024, 12, 1)
    closes = [110.0 - index for index in range(count)] if falling else [
        100.0 + index for index in range(count)
    ]
    return [
        Bar(timestamp=start + timedelta(days=index), symbol="INDEX",
            open=close, high=close + 1, low=close - 1, close=close,
            volume=1_000, amount=100_000)
        for index, close in enumerate(closes)
    ]


def flat_account() -> BrokerAccount:
    return BrokerAccount(4_000_000, StrategyConfig(slippage_bps=0, volume_participation=1.0))


def test_tail_entry_buys_between_1430_and_1450():
    account = flat_account()
    strategy = LayeredTStrategy(account.config)
    bar = intraday_bar(14, 35)
    requests = strategy.on_intraday(
        [daily(1, 10.0)], [bar], bar, account, index_history=index_series(),
    )
    assert len(requests) == 1
    assert requests[0].side == "BUY" and requests[0].purpose == "PERMANENT"
    assert requests[0].reason == "尾盘首笔"
    assert requests[0].execution_mode == "NEXT_BAR"
    assert requests[0].quantity == 10_000  # 10万 / 10元 / 100 * 100
    assert strategy.entry_submitted_date == bar.date


def test_tail_entry_window_is_1430_inclusive_1450_exclusive():
    for hour, minute in ((14, 0), (14, 29), (14, 50), (14, 55), (15, 0)):
        account = flat_account()
        strategy = LayeredTStrategy(account.config)
        bar = intraday_bar(hour, minute)
        assert strategy.on_intraday(
            [daily(1, 10.0)], [bar], bar, account, index_history=index_series(),
        ) == [], f"{hour:02d}:{minute:02d} 不应发尾盘首仓"
        assert strategy.entry_submitted_date is None

    account = flat_account()
    strategy = LayeredTStrategy(account.config)
    bar = intraday_bar(14, 30)
    assert len(strategy.on_intraday(
        [daily(1, 10.0)], [bar], bar, account, index_history=index_series(),
    )) == 1


def test_tail_entry_respects_index_gate():
    account = flat_account()
    strategy = LayeredTStrategy(account.config)
    bar = intraday_bar(14, 35)
    # 指数 20 日收益 > 0 → 大盘门关门，不发首仓
    assert strategy.on_intraday(
        [daily(1, 10.0)], [bar], bar, account, index_history=index_series(falling=False),
    ) == []
    assert strategy.entry_submitted_date is None


def test_tail_entry_uses_only_prior_day_index_bars():
    """盘中不能用当日尚未收盘的指数日线（避免未来函数）。"""
    account = flat_account()
    strategy = LayeredTStrategy(account.config)
    bar = intraday_bar(14, 35)
    same_day = Bar(timestamp=bar.timestamp.replace(hour=0, minute=0), symbol="INDEX",
                   open=50, high=51, low=49, close=50, volume=1_000, amount=100_000)
    history = index_series(falling=True) + [same_day]
    assert len(strategy.on_intraday(
        [daily(1, 10.0)], [bar], bar, account, index_history=history,
    )) == 1


def test_tail_entry_submitted_once_per_day():
    account = flat_account()
    strategy = LayeredTStrategy(account.config)
    first = intraday_bar(14, 35)
    second = intraday_bar(14, 40)
    assert len(strategy.on_intraday(
        [daily(1, 10.0)], [first], first, account, index_history=index_series(),
    )) == 1
    assert strategy.on_intraday(
        [daily(1, 10.0)], [second], second, account, index_history=index_series(),
    ) == []


def test_daily_signal_does_not_repeat_first_buy_after_tail_entry():
    """同日尾盘已发首仓后，15:00 日终信号不能再发一笔首仓。"""
    account = flat_account()
    strategy = LayeredTStrategy(account.config)
    bar = intraday_bar(14, 35)
    assert len(strategy.on_intraday(
        [daily(1, 10.0)], [bar], bar, account, index_history=index_series(),
    )) == 1
    assert strategy.on_daily(
        [daily(6, 10.0)], index_series(), account, 6, True,
    ) == []


def test_tail_entry_does_not_touch_t_orders_after_sequence_started():
    """序列已开（≥1 笔）时尾盘不建首仓，做 T 逻辑口径不变。"""
    account = funded_account()  # sequence_index = 4
    strategy = LayeredTStrategy(account.config)
    bar = intraday_bar(14, 35, close=11.2)
    bar.turnover_rate = 0.05
    bar.bid = 11.19
    bar.ask = 11.21
    requests = strategy.on_intraday(
        [daily(index, 11.2) for index in range(5)], [bar], bar, account,
        index_history=index_series(),
    )
    assert all(item.reason != "尾盘首笔" for item in requests)


# ---------------------------------------------------------------------------
# 尾盘加仓（14:30–14:50，与日线加仓共用信号口径）
# ---------------------------------------------------------------------------


def held_account(price: float = 10.0, quantity: int = 1_000) -> BrokerAccount:
    """已建首仓的账户：永久仓 1000 股 @10，sequence_index=1。"""
    account = BrokerAccount(4_000_000, StrategyConfig(slippage_bps=0, volume_participation=1.0))
    account.new_day("2025-01-06")
    account.submit(OrderRequest(symbol="TEST", side="BUY", quantity=quantity, execution_mode="NEXT_BAR"))
    fill_bar = daily(5, price)
    fill_bar.timestamp = fill_bar.timestamp.replace(hour=9, minute=35)
    account.process_bar(fill_bar, modes={"NEXT_BAR"})
    assert account.sequence_index == 1
    return account


def add_history() -> list[Bar]:
    """前 5 根日线：低位横盘，其中一根高点 12（用于触发浮盈右侧回撤）。"""
    history = [daily(index, 10.0, low=10.4) for index in range(1, 6)]
    history[2].high = 12.0
    return history


def tail_add_bar(hour: int = 14, minute: int = 35, close: float = 10.9) -> Bar:
    bar = intraday_bar(hour, minute, close=close)
    bar.open = close
    bar.high = close
    bar.low = close
    return bar


def make_add_strategy(account: BrokerAccount) -> LayeredTStrategy:
    strategy = LayeredTStrategy(account.config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    return strategy


def test_tail_add_buys_between_1430_and_1450():
    account = held_account()
    strategy = make_add_strategy(account)
    bar = tail_add_bar()
    requests = strategy.on_intraday(
        add_history(), [bar], bar, account, index_history=index_series(), session_index=1,
    )
    assert len(requests) == 1
    assert requests[0].side == "BUY" and requests[0].purpose == "PERMANENT"
    assert requests[0].reason == "浮盈右侧回撤5%"
    assert requests[0].execution_mode == "NEXT_BAR"
    assert requests[0].quantity == 9_100  # 10万 / 10.9 / 100 * 100
    assert strategy.add_submitted_date == bar.date


def test_tail_add_requires_one_session_gap():
    account = held_account()
    strategy = make_add_strategy(account)
    bar = tail_add_bar()
    assert strategy.on_intraday(
        add_history(), [bar], bar, account, session_index=0,
    ) == []
    assert strategy.add_submitted_date is None


def test_tail_add_window_is_1430_inclusive_1450_exclusive():
    for hour, minute in ((14, 0), (14, 29), (14, 50), (15, 0)):
        account = held_account()
        strategy = make_add_strategy(account)
        bar = tail_add_bar(hour, minute)
        assert strategy.on_intraday(
            add_history(), [bar], bar, account, session_index=1,
        ) == [], f"{hour:02d}:{minute:02d} 不应发尾盘加仓"
        assert strategy.add_submitted_date is None


def test_tail_add_submitted_once_per_day():
    account = held_account()
    strategy = make_add_strategy(account)
    first = tail_add_bar(14, 35)
    assert len(strategy.on_intraday(add_history(), [first], first, account, session_index=1)) == 1
    second = tail_add_bar(14, 40)
    assert strategy.on_intraday(add_history(), [second], second, account, session_index=1) == []


def test_daily_signal_does_not_repeat_add_after_tail_add():
    """同日尾盘已发加仓后，15:00 日终信号不能再发一笔加仓。"""
    account = held_account()
    strategy = make_add_strategy(account)
    bar = tail_add_bar()
    assert len(strategy.on_intraday(add_history(), [bar], bar, account, session_index=1)) == 1
    today_daily = daily(6, 10.9, low=10.9)
    today_daily.high = 10.9
    assert strategy.on_daily(add_history() + [today_daily], [], account, 1, True) == []


def test_daily_add_still_works_without_intraday():
    """无盘中数据时，日线加仓路径保持原有 NEXT_OPEN 口径。"""
    account = held_account()
    strategy = make_add_strategy(account)
    today_daily = daily(6, 10.9, low=10.9)
    today_daily.high = 10.9
    requests = strategy.on_daily(add_history() + [today_daily], [], account, 1, False)
    assert len(requests) == 1
    assert requests[0].reason == "浮盈右侧回撤5%"
    assert requests[0].execution_mode == "NEXT_OPEN"
