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
    # 当日买入当日卖（NEXT_BAR 盘中口径）不可卖（T+1）
    same_day = OrderRequest(
        symbol="sz.159915", side="SELL", quantity=1000, purpose="BETA",
        execution_mode="NEXT_BAR", order_type="MARKET", reason="测试",
    )
    acct.submit(same_day, datetime(2025, 1, 2, 15))
    assert acct.orders[-1].status == "REJECTED"
    # 当日买入 → 次开卖（NEXT_OPEN 隔夜单）：提交时按次日口径放行，不再误拒
    acct.submit(request("SELL", 1000), datetime(2025, 1, 2, 15))
    assert acct.orders[-1].status == "PENDING"
    # 次日超量申报（超过持仓总量）仍被拒
    acct.new_day("2025-01-03")
    acct.submit(request("SELL", 2000), datetime(2025, 1, 3, 15))
    assert acct.orders[-1].status == "REJECTED"


def test_next_open_sell_of_today_buys_fills_next_open():
    """复现线上误拒场景：T+1 开盘买入、当天收盘触发出场 → 次开清仓不被拒。

    旧逻辑在提交时刻按当日口径校验，「今买→明开卖」被拒（可卖0），清仓
    被迫延迟一天并刷 [ETF委托被拒] 告警；修复后隔夜卖单按次日口径放行。
    """
    acct = account()
    acct.new_day("2025-01-02")
    acct.submit(request(quantity=1000), datetime(2025, 1, 2, 15))
    acct.process_bar(make_bar("sz.159915", 2))          # T+1 开盘买入成交
    assert acct.position("sz.159915").beta_qty == 1000
    acct.submit(request("SELL", 1000), datetime(2025, 1, 2, 15))  # 当天收盘提交清仓
    assert acct.orders[-1].status == "PENDING"
    # 与回测引擎一致：隔夜 NEXT_OPEN 单要保留到次日开盘撮合（cancel_previous=False）
    acct.new_day("2025-01-03", cancel_previous=False)
    fills = acct.process_bar(make_bar("sz.159915", 3))  # 次开成交
    assert len(fills) == 1 and fills[0].side == "SELL"
    assert acct.position("sz.159915").beta_qty == 0


def test_permanent_next_open_sell_of_today_buys_accepted():
    """PERMANENT 账本同样适用：今买→明开卖的隔夜单不再被 T+1 误拒。"""
    acct = account()
    acct.new_day("2025-01-02")
    acct.submit(OrderRequest(
        symbol="TEST", side="BUY", quantity=100, purpose="PERMANENT",
        execution_mode="NEXT_OPEN", order_type="MARKET", reason="测试",
    ), datetime(2025, 1, 2, 15))
    acct.process_bar(Bar(
        timestamp=datetime(2025, 1, 2, 9, 30), symbol="TEST",
        open=10, high=10, low=10, close=10, volume=1e6, amount=1e7, prev_close=10,
    ))
    assert acct.position("TEST").permanent_qty == 100
    acct.submit(OrderRequest(
        symbol="TEST", side="SELL", quantity=100, purpose="PERMANENT",
        execution_mode="NEXT_OPEN", order_type="MARKET", reason="测试",
    ), datetime(2025, 1, 2, 15))
    assert acct.orders[-1].status == "PENDING"


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


def test_etf_fees_exempt_stamp_tax_and_transfer_fee():
    """场内 ETF 免征印花税与过户费；股票仍按原口径收取（5‱ 印花税 + 0.1‱ 过户费）。"""
    acct = account()
    etf_sell = acct._fees("SELL", 1_000_000, "sz.159915")
    assert etf_sell.stamp_tax == 0
    assert etf_sell.transfer_fee == 0
    assert etf_sell.commission > 0            # 佣金/经手费/证管费照收
    assert etf_sell.handling_fee > 0 and etf_sell.regulatory_fee > 0

    stock_sell = acct._fees("SELL", 1_000_000, "sz.300850")
    assert stock_sell.stamp_tax == pytest.approx(500.0)   # 5‱
    assert stock_sell.transfer_fee == pytest.approx(10.0)  # 0.1‱
    # 买入股票：不收取印花税，但过户费照收
    stock_buy = acct._fees("BUY", 1_000_000, "sz.300850")
    assert stock_buy.stamp_tax == 0
    assert stock_buy.transfer_fee == pytest.approx(10.0)
    # 合成标的（无板块）按股票口径，保持旧行为
    synthetic = acct._fees("SELL", 1_000_000, "TEST")
    assert synthetic.stamp_tax > 0 and synthetic.transfer_fee > 0


def test_etf_fill_has_no_stamp_tax_end_to_end():
    """成交层面同样生效：ETF 卖出成交的 stamp_tax 必须为 0（回测/模拟盘共用撮合）。"""
    acct = account()
    acct.new_day("2025-01-02")
    acct.submit(request(quantity=1000), datetime(2025, 1, 2, 15))
    buy = acct.process_bar(make_bar("sz.159915", 2))[0]
    acct.new_day("2025-01-03")
    acct.submit(request("SELL", 1000), datetime(2025, 1, 3, 15))
    fill = acct.process_bar(make_bar("sz.159915", 3, 2.6))[0]
    assert fill.side == "SELL" and fill.purpose == "BETA"
    assert fill.stamp_tax == 0
    assert fill.transfer_fee == 0
    # 净利不再被多扣印花税：卖出净额 − 建仓成本（含买入费用）= 毛利 − 双边费用
    gross = (2.6 - 2.5) * 1000
    assert fill.realized_pnl == pytest.approx(gross - fill.total_fee - buy.total_fee, abs=0.02)
