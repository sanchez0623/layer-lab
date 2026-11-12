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
