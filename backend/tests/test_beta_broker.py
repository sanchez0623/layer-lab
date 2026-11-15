"""ETF beta 腿账本单测：BETA 账本独立、T+1、互不影响个股轮动序列。"""

from datetime import datetime

import pytest

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig


def make_bar(symbol: str, day: int, price: float = 2.5, volume: float = 10_000_000) -> Bar:
    return Bar(
        timestamp=datetime(2025, 1, day, 9, 30), symbol=symbol,
        open=price, high=price * 1.01, low=price * 0.99, close=price,
        volume=volume, amount=price * volume, prev_close=price,
    )


def request(side="BUY", quantity=1000, purpose="BETA") -> OrderRequest:
    return OrderRequest(
        symbol="sz.159915", side=side, quantity=quantity, purpose=purpose,
        execution_mode="NEXT_OPEN", order_type="MARKET", reason="测试",
    )


def account() -> BrokerAccount:
    return BrokerAccount(
        1_000_000, StrategyConfig(slippage_bps=0, volume_participation=1.0),
        name="ETF测试",
    )


def test_beta_buy_and_sell_uses_separate_ledger():
    acct = account()
    acct.new_day("2025-01-02")
    acct.submit(request(quantity=1000), datetime(2025, 1, 2, 15))
    fill = acct.process_bar(make_bar("sz.159915", 2))[0]
    assert fill.purpose == "BETA"
    position = acct.position("sz.159915")
    assert position.beta_qty == 1000
    assert position.permanent_qty == 0
    assert acct.has_stock_position is False       # ETF 不算个股持仓
    assert acct.sequence_index == 0               # 不占轮动加仓序列

    acct.new_day("2025-01-03")
    acct.submit(request("SELL", 1000), datetime(2025, 1, 3, 15))
    sell = acct.process_bar(make_bar("sz.159915", 3, 2.6))[0]
    assert sell.purpose == "BETA"
    assert acct.position("sz.159915").beta_qty == 0
    assert sell.realized_pnl > 0
    assert acct.realized_pnl == pytest.approx(sell.realized_pnl)


def test_beta_is_t_plus_one_and_cannot_oversell():
    acct = account()
    acct.new_day("2025-01-02")
    acct.submit(request(quantity=1000), datetime(2025, 1, 2, 15))
    acct.process_bar(make_bar("sz.159915", 2))
    # 当日买入不可卖（T+1）
    acct.submit(request("SELL", 1000), datetime(2025, 1, 2, 15))
    assert acct.orders[-1].status == "REJECTED"
    # 次日可卖，但超量申报被拒
    acct.new_day("2025-01-03")
    acct.submit(request("SELL", 2000), datetime(2025, 1, 3, 15))
    assert acct.orders[-1].status == "REJECTED"


def test_beta_does_not_touch_permanent_position():
    acct = account()
    acct.new_day("2025-01-02")
    acct.submit(OrderRequest(
        symbol="TEST", side="BUY", quantity=100, purpose="PERMANENT",
        execution_mode="NEXT_BAR",
    ))
    acct.process_bar(Bar(
        timestamp=datetime(2025, 1, 2, 9, 30), symbol="TEST",
        open=10, high=10, low=10, close=10, volume=1e6, amount=1e7, prev_close=10,
    ))
    acct.submit(request(quantity=1000), datetime(2025, 1, 2, 15))
    acct.process_bar(make_bar("sz.159915", 2))
    assert acct.position("TEST").permanent_qty == 100
    assert acct.position("sz.159915").beta_qty == 1000
    assert acct.has_stock_position is True
