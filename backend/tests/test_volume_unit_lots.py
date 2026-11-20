"""撮合容量单位换算（手 → 股）单测。

根因：broker.process_bar 的容量计算把 bar.volume（单位「手」，1 手 = 100 股）当成
「股」直接乘 volume_participation，导致容量少算 100 倍，把本可当日全成的大单误拆成
多日分批成交。证据：159915 2020-07-27 真实日线 volume=4,235,953 手、amount=10,759,320.62
元 → amount/volume≈2.54 元/股，与当日价格区间（2.513~2.577）吻合，确认 CSV 存的是手。

真实场景：159915 当日成交量 4.24 亿股（4,235,953 手），20% 参与度容量应约 8470 万股，
一笔 96.9 万股的 ETF 卖单当然单日全成；修复前容量被算成 847,190「股」，只成交 847,100
（step=100 向下取整），剩余滚到次日。
"""

from datetime import datetime

import pytest

from app.broker import LOTS_PER_SHARE, BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig


SYMBOL = "sz.159915"          # ETF，申报 step=100
LOTS = 4_235_953              # 2020-07-27 真实成交量（手）
PARTICIPATION = 0.20          # 默认成交量参与度


def make_bar(day: int, price: float = 2.54, volume: float = LOTS) -> Bar:
    return Bar(
        timestamp=datetime(2025, 1, day, 9, 30), symbol=SYMBOL,
        open=price, high=price * 1.01, low=price * 0.99, close=price,
        volume=volume, amount=price * volume * LOTS_PER_SHARE, prev_close=price,
    )


def sell_request(quantity: int) -> OrderRequest:
    return OrderRequest(
        symbol=SYMBOL, side="SELL", quantity=quantity, purpose="BETA",
        execution_mode="NEXT_BAR", order_type="MARKET", reason="测试",
    )


def seeded_account(beta_qty: int) -> BrokerAccount:
    """底仓 beta_qty 股（T+1 可卖），参与度固定为 PARTICIPATION。"""
    acct = BrokerAccount(
        10_000_000, StrategyConfig(slippage_bps=0, volume_participation=PARTICIPATION),
        name="ETF容量测试",
    )
    acct.new_day("2025-01-02")
    pos = acct.position(SYMBOL)
    pos.beta_qty = beta_qty
    pos.beta_cost = 2.0 * beta_qty
    # 跨日使其成为隔夜仓（T+1 可卖）
    acct.new_day("2025-01-03")
    return acct


def test_large_etf_sell_fills_single_day():
    """96.9 万股卖单 + 4,235,953 手成交量 → 单日全部成交（修复前只成 847,100）。"""
    acct = seeded_account(beta_qty=969_000)
    order = acct.submit(sell_request(969_000), datetime(2025, 1, 3, 15))
    assert order.status == "PENDING"

    fills = acct.process_bar(make_bar(3))

    assert len(fills) == 1, f"应单根 bar 全部成交，实际 {len(fills)} 笔"
    assert fills[0].quantity == 969_000
    assert order.status == "FILLED"
    assert order.remaining == 0
    assert acct.position(SYMBOL).beta_qty == 0


def test_large_order_capacity_not_capped_at_old_wrong_value():
    """容量按股算后应远超 96.9 万股，不再被旧的 847,190「股」卡住。"""
    acct = seeded_account(beta_qty=969_000)
    order = acct.submit(sell_request(969_000), datetime(2025, 1, 3, 15))
    fills = acct.process_bar(make_bar(3))
    # 修复前的错误容量 = int(4_235_953 * 0.20) 向下取整到 step = 847,100 股
    wrong_capacity = (int(LOTS * PARTICIPATION) // 100) * 100
    assert fills[0].quantity > wrong_capacity
    assert order.status == "FILLED"


def test_small_order_behavior_unchanged():
    """回归护栏：小单（远小于容量）行为不变，仍单日全成、数量精确。"""
    acct = seeded_account(beta_qty=1_000)
    order = acct.submit(sell_request(1_000), datetime(2025, 1, 3, 15))
    fills = acct.process_bar(make_bar(3, volume=1_000))  # 1000 手 = 10 万股，容量 2 万股
    assert len(fills) == 1
    assert fills[0].quantity == 1_000
    assert order.status == "FILLED"


def test_capacity_still_capped_by_participation():
    """超大单仍受参与度约束：委托量超过单日容量时按容量部分成交（护栏不被过度放宽）。

    ETF 市价单笔申报上限 100 万股，故委托取 99 万股；bar 成交量调小到 6,000 手
    （=60 万股，20% 容量=12 万股），远小于委托量，验证容量仍把大单截断成部分成交。
    """
    acct = seeded_account(beta_qty=990_000)
    order = acct.submit(sell_request(990_000), datetime(2025, 1, 3, 15))
    assert order.status == "PENDING"
    fills = acct.process_bar(make_bar(3, volume=6_000))
    assert order.status == "PARTIALLY_FILLED"
    expected = (int(6_000 * LOTS_PER_SHARE * PARTICIPATION) // 100) * 100  # 120,000 股
    assert fills[0].quantity == expected


def test_lots_per_share_constant():
    """常量语义：1 手 = 100 股。"""
    assert LOTS_PER_SHARE == 100


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
