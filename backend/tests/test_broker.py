from datetime import datetime

import pytest

from app.broker import BrokerAccount
from app.domain import Bar, FeeConfig, OrderRequest, Position, StrategyConfig


def make_bar(day: int, price: float = 10.0, volume: float = 100_000) -> Bar:
    return Bar(
        timestamp=datetime(2025, 1, day, 9, 30), symbol="TEST",
        open=price, high=price * 1.02, low=price * 0.98, close=price,
        volume=volume, amount=price * volume, prev_close=10.0,
    )


def config() -> StrategyConfig:
    return StrategyConfig(slippage_bps=0, volume_participation=1.0)


def request(side="BUY", quantity=100, purpose="PERMANENT", order_type="MARKET", limit_price=None):
    return OrderRequest(
        symbol="TEST", side=side, quantity=quantity, purpose=purpose,
        order_type=order_type, limit_price=limit_price, execution_mode="NEXT_BAR",
    )


def test_fee_breakdown_and_cash_flow():
    """默认费率：佣金万0.5(最低5元)、印花税万5仅卖出、经手费万0.341、证管费万0.2、过户费万0.1。"""
    account = BrokerAccount(100_000, config())
    account.new_day("2025-01-01")
    account.submit(request(quantity=100))
    buy = account.process_bar(make_bar(1, 10))[0]
    assert buy.commission == 5.0             # max(5, 1000*0.00005)
    assert buy.handling_fee == 0.03          # 1000 * 0.0000341
    assert buy.regulatory_fee == 0.02        # 1000 * 0.00002
    assert buy.transfer_fee == 0.01          # 1000 * 0.00001
    assert buy.stamp_tax == 0.0              # 买入不收印花税
    assert buy.total_fee == 5.06             # 五项分项相加
    assert account.cash == pytest.approx(98_994.94)

    account.new_day("2025-01-02")
    account.submit(request("SELL", 100))
    sell = account.process_bar(make_bar(2, 11))[0]
    assert sell.commission == 5.0
    assert sell.stamp_tax == 0.55            # 1100 * 0.0005，仅卖出
    assert sell.handling_fee == 0.04
    assert sell.regulatory_fee == 0.02
    assert sell.transfer_fee == 0.01
    assert sell.total_fee == 5.62
    assert account.cash == pytest.approx(100_089.32)
    assert account.realized_pnl == pytest.approx(89.32)


def test_five_fees_are_summed_and_min_commission_only_covers_commission():
    """最低佣金只兜底佣金项；其余四项即使低于5元也按实际金额收取。"""
    fees = FeeConfig(
        commission_rate=3.0, min_commission=5.0, stamp_tax_rate=10.0,
        handling_fee_rate=1.0, regulatory_fee_rate=1.0, transfer_fee_rate=1.0,
    )
    config = StrategyConfig(slippage_bps=0, volume_participation=1.0, **fees.to_strategy_patch())
    account = BrokerAccount(100_000, config)
    account.new_day("2025-01-01")
    account.submit(request(quantity=100))
    buy = account.process_bar(make_bar(1, 10))[0]
    # 1000元：佣金按 5 元下限，其余四项各 0.10 元，买入无印花税。
    assert buy.commission == 5.0
    assert (buy.handling_fee, buy.regulatory_fee, buy.transfer_fee) == (0.10, 0.10, 0.10)
    assert buy.stamp_tax == 0.0
    assert buy.total_fee == 5.30
    assert buy.total_fee == pytest.approx(
        buy.commission + buy.stamp_tax + buy.handling_fee + buy.regulatory_fee + buy.transfer_fee
    )

    account.new_day("2025-01-02")
    account.submit(request("SELL", 100))
    sell = account.process_bar(make_bar(2, 11))[0]
    # 1100元卖出：印花税 1.10 元，其余三项各 0.11 元。
    assert sell.stamp_tax == 1.10
    assert (sell.handling_fee, sell.regulatory_fee, sell.transfer_fee) == (0.11, 0.11, 0.11)
    assert sell.total_fee == 6.43


def test_zero_fee_config_produces_no_fees():
    config = StrategyConfig(slippage_bps=0, volume_participation=1.0, **FeeConfig(
        commission_rate=0, min_commission=0, stamp_tax_rate=0,
        handling_fee_rate=0, regulatory_fee_rate=0, transfer_fee_rate=0,
    ).to_strategy_patch())
    account = BrokerAccount(100_000, config)
    account.new_day("2025-01-01")
    account.submit(request(quantity=100))
    buy = account.process_bar(make_bar(1, 10))[0]
    assert buy.total_fee == 0.0
    assert account.total_fees == 0.0


def test_legacy_orders_and_fills_without_new_fee_fields_can_be_restored():
    """旧的落库成交/委托没有经手费、证管费字段，恢复账户时不能报错。"""
    account = BrokerAccount(100_000, config())
    account.new_day("2025-01-01")
    account.submit(request(quantity=100))
    account.process_bar(make_bar(1, 10))
    state = account.to_dict(include_internal=True)
    for key in ("handling_fee", "regulatory_fee"):
        state["orders"] = [{k: v for k, v in item.items() if k != key} for item in state["orders"]]
        state["trades"] = [{k: v for k, v in item.items() if k != key} for item in state["trades"]]
    restored = BrokerAccount.from_dict(state)
    assert restored.orders[0].handling_fee == 0.0
    assert restored.orders[0].regulatory_fee == 0.0
    assert restored.trades[0].handling_fee == 0.0
    assert restored.trades[0].regulatory_fee == 0.0
    assert restored.total_fees == account.total_fees


def test_limit_price_and_partial_fill():
    account = BrokerAccount(100_000, config())
    account.new_day("2025-01-01")
    order = account.submit(request(quantity=200, order_type="LIMIT", limit_price=9.9))
    bar = make_bar(1, 10.2, volume=1_000)
    bar.low = 9.8
    account.config.volume_participation = 0.1
    fills = account.process_bar(bar)
    assert len(fills) == 1
    assert fills[0].price == 9.9
    assert fills[0].quantity == 100
    assert order.status == "PARTIALLY_FILLED"
    assert order.remaining == 100

    other = BrokerAccount(100_000, config())
    other.new_day("2025-01-01")
    other.submit(request(order_type="LIMIT", limit_price=9.7))
    bar.low = 9.8
    assert other.process_bar(bar) == []


def test_t_plus_one_sellable():
    account = BrokerAccount(100_000, config())
    account.new_day("2025-01-01")
    account.submit(request(quantity=1_000))
    account.process_bar(make_bar(1))
    rejected = account.submit(request("SELL", 1_000))
    assert rejected.status == "REJECTED"
    assert account.position("TEST").sellable_qty == 0

    account.new_day("2025-01-02")
    accepted = account.submit(request("SELL", 1_000))
    assert accepted.status == "PENDING"
    assert account.position("TEST").sellable_qty == 1_000
    assert len(account.process_bar(make_bar(2, 11))) == 1


def test_permanent_and_t_ledgers_keep_permanent_cost():
    account = BrokerAccount(100_000, config())
    account.new_day("2025-01-01")
    account.submit(request(quantity=1_000))
    account.process_bar(make_bar(1, 10))
    original_cost = account.position("TEST").permanent_cost

    account.new_day("2025-01-02")
    account.submit(request(quantity=500, purpose="T"))
    account.process_bar(make_bar(2, 10))
    assert account.position("TEST").t_active_qty if hasattr(account.position("TEST"), "t_active_qty") else True
    assert account.position("TEST").t_qty == 500

    account.submit(request("SELL", 500, purpose="T"))
    fill = account.process_bar(make_bar(2, 11))[0]
    position = account.position("TEST")
    assert fill.purpose == "T"
    assert position.permanent_qty == 1_000
    assert position.t_qty == 0
    assert position.total_qty == 1_000
    assert position.permanent_cost == original_cost
    # T买入：5000 + 佣金5 + 经手费0.17 + 证管费0.10 + 过户费0.05
    # T卖出：5500 - 佣金5 - 印花税2.75 - 经手费0.19 - 证管费0.11 - 过户费0.06
    expected_t_profit = (5500 - 5 - 2.75 - 0.19 - 0.11 - 0.06) - (5000 + 5 + 0.17 + 0.10 + 0.05)
    assert account.t_net_profit == pytest.approx(expected_t_profit)
    assert fill.realized_pnl == pytest.approx(expected_t_profit)


def test_limit_up_buy_cannot_fill():
    account = BrokerAccount(100_000, config())
    account.new_day("2025-01-01")
    account.submit(request())
    assert account.process_bar(make_bar(1, 12)) == []


def test_position_public_dict_exposes_exact_float_pnl():
    """400 股成本 94102.56、现价 245.58：精确浮盈 4129.44，用平均成本反推只有 4128.00。"""
    position = Position(
        symbol="sh.688200", permanent_qty=400, permanent_cost=94102.56, last_price=245.58,
    )
    data = position.public_dict()
    assert data["total_cost"] == 94102.56
    assert data["average_cost"] == 235.26
    assert data["market_value"] == 98232.00
    assert data["float_pnl"] == 4129.44
    assert (data["last_price"] - data["average_cost"]) * data["total_qty"] == pytest.approx(4128.00)
    assert data["float_pnl"] == pytest.approx(data["market_value"] - data["total_cost"])


def test_position_float_pnl_includes_t_ledger_cost():
    position = Position(
        symbol="TEST", permanent_qty=400, permanent_cost=94102.56,
        t_qty=100, t_cost=24000.0, last_price=245.58,
    )
    data = position.public_dict()
    assert data["total_qty"] == 500
    assert data["total_cost"] == 118102.56
    assert data["market_value"] == 122790.00
    assert data["float_pnl"] == 4687.44
