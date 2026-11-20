"""撮合容量单位口径单测（入口统一为「股」，broker 不再换算）。

背景（2026-11-20 腾讯行情量单位修复）：腾讯快照 / 5 分钟 / 日线的 volume 单位是「手」
（1 手=100 股），已在 ``market.py`` 数据入口统一 ×100 转成「股」；quan parquet（回测、
/advance）本来就是「股」。因此 ``broker.process_bar`` 的容量计算直接用 ``bar.volume``
（股）× ``volume_participation``，**对 BETA / PERMANENT / T 一视同仁，不再按 purpose 猜单位**
（旧的 ``LOTS_PER_SHARE`` 常量与 BETA-only ×100 分支已删除）。

本文件验证两件事：
1. broker 对 BETA 与 PERMANENT 都**不 ×100**——喂进来的 bar.volume 已经是股，容量按股算。
2. ``LOTS_PER_SHARE`` 常量已从 broker 删除（避免再出现「半手半股」混算）。
"""

from datetime import datetime

import pytest

import app.broker as broker_module
from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig


PARTICIPATION = 0.20          # 默认成交量参与度
ETF = "sz.159915"             # ETF，申报 step=100
STOCK = "sh.600000"           # 沪市主板，申报 step=100


def make_bar(symbol: str, day: int, price: float, volume: float) -> Bar:
    """volume 已是**股**（与修复后数据入口口径一致）。"""
    return Bar(
        timestamp=datetime(2025, 1, day, 9, 30), symbol=symbol,
        open=price, high=price * 1.01, low=price * 0.99, close=price,
        volume=volume, amount=price * volume, prev_close=price,
    )


def _seeded_account(symbol: str, qty_attr: str, qty: int) -> BrokerAccount:
    """底仓 qty 股（T+1 可卖），参与度固定为 PARTICIPATION。"""
    acct = BrokerAccount(
        10_000_000, StrategyConfig(slippage_bps=0, volume_participation=PARTICIPATION),
        name="容量单位测试",
    )
    acct.new_day("2025-01-02")
    pos = acct.position(symbol)
    setattr(pos, qty_attr, qty)
    setattr(pos, qty_attr.replace("_qty", "_cost"), 2.0 * qty)
    acct.new_day("2025-01-03")
    return acct


def _sell_request(symbol: str, purpose: str, quantity: int) -> OrderRequest:
    return OrderRequest(
        symbol=symbol, side="SELL", quantity=quantity, purpose=purpose,
        execution_mode="NEXT_BAR", order_type="MARKET", reason="测试",
    )


def test_beta_large_order_fills_single_bar_when_volume_is_shares():
    """BETA：bar.volume 已是股（4.24 亿股）→ 96.9 万股卖单单根 bar 全成。"""
    acct = _seeded_account(ETF, "beta_qty", 969_000)
    order = acct.submit(_sell_request(ETF, "BETA", 969_000), datetime(2025, 1, 3, 15))
    assert order.status == "PENDING"

    fills = acct.process_bar(make_bar(ETF, 3, 2.54, volume=424_000_000))

    assert len(fills) == 1, f"应单根 bar 全部成交，实际 {len(fills)} 笔"
    assert fills[0].quantity == 969_000
    assert order.status == "FILLED"


def test_beta_capacity_capped_by_participation_in_shares():
    """BETA：容量按股算并受参与度约束——bar 60 万股 → 20% 容量=12 万股。"""
    acct = _seeded_account(ETF, "beta_qty", 990_000)
    order = acct.submit(_sell_request(ETF, "BETA", 990_000), datetime(2025, 1, 3, 15))
    fills = acct.process_bar(make_bar(ETF, 3, 2.54, volume=600_000))
    expected = (int(600_000 * PARTICIPATION) // 100) * 100  # 120,000 股
    assert order.status == "PARTIALLY_FILLED"
    assert fills[0].quantity == expected


# ---------------- PERMANENT 与 BETA 同口径：都不 ×100 ----------------

def test_permanent_uses_raw_share_volume_not_scaled():
    """PERMANENT：bar.volume 已是股，容量按股算、**不再 ×100**（旧 BETA-only 语义作废）。

    旧实现曾对 PERMANENT 保留「不乘」、对 BETA ×100；入口统一后两者都不乘——
    喂进来的 volume 本身已是股，直接 × 参与度即容量。
    """
    acct = _seeded_account(STOCK, "permanent_qty", 99_000)
    order = acct.submit(_sell_request(STOCK, "PERMANENT", 99_000), datetime(2025, 1, 3, 15))
    assert order.status == "PENDING"

    # bar 成交量 100,000 股（已是股），20% 容量=20,000 股 < 委托 99,000 → 部分成交
    fills = acct.process_bar(make_bar(STOCK, 3, 2.54, volume=100_000))

    expected = (int(100_000 * PARTICIPATION) // 100) * 100  # 20,000 股
    assert len(fills) == 1
    assert fills[0].quantity == expected
    assert order.status == "PARTIALLY_FILLED"
    assert order.remaining == 99_000 - expected


def test_permanent_small_order_fills_single_bar():
    """PERMANENT 小单（远小于容量）单根 bar 全成、数量精确。"""
    acct = _seeded_account(STOCK, "permanent_qty", 1_000)
    order = acct.submit(_sell_request(STOCK, "PERMANENT", 1_000), datetime(2025, 1, 3, 15))
    fills = acct.process_bar(make_bar(STOCK, 3, 2.54, volume=100_000))
    assert len(fills) == 1
    assert fills[0].quantity == 1_000
    assert order.status == "FILLED"


def test_broker_no_longer_exposes_lots_per_share():
    """护栏：LOTS_PER_SHARE 常量已从 broker 删除，broker 不再自己做手→股换算。"""
    assert not hasattr(broker_module, "LOTS_PER_SHARE"), (
        "broker 不应再持有 LOTS_PER_SHARE：单位换算已统一到 market.py 数据入口"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
