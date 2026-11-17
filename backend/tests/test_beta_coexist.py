"""并存模式（etf_beta_coexist=True）下个股买入是否受 beta 比例限制的验证。

结论锚点（供 lead 复核）：
1. 个股买入**金额**只由 ``installment_amounts[sequence_index]`` 固定档位决定，
   ``board_buy_quantity`` 折算股数；``strategy.py`` 的 ``_buy_request`` /
   ``_tail_entry_requests`` / ``_tail_add_requests`` **从不读取** ``etf_position_pct``
   —— beta 比例只约束 ETF 腿自身的建仓预算
   （``backtest._beta_target_quantity`` / ``main.py`` live-tick 两条路径同口径）。
2. 并存模式下个股与 ETF 共用同一现金池；个股成交由 broker ``process_bar``
   以可用现金（``self.cash``）封顶（可能降档/拒单「可用资金不足」），是**间接**
   受现金限制，而非受 beta 比例参数限制。
"""

from datetime import datetime

import pytest

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig
from app.strategy import LayeredTStrategy, board_buy_quantity

ETF = "sz.159915"          # 场内 ETF（min_qty=100, step=100, 市价单笔上限 100 万份）
STOCK = "sz.300850"        # 创业板（min_qty=100, step=100, 市价单笔上限 5 万股）
INITIAL_CASH = 1_000_000.0
ETF_QTY = 80_000           # × 2.5 = 200,000 → 占用初始现金 20%
ETF_PRICE = 2.5
STOCK_PRICE = 10.0         # 低价股（首仓档位全额可负担）
STOCK_PRICE_HIGH = 100.0   # 高价股（大额档位超出剩余现金）


def make_bar(
    symbol: str, day: int, hour: int, minute: int,
    price: float, volume: float = 10_000_000,
) -> Bar:
    return Bar(
        timestamp=datetime(2025, 1, day, hour, minute), symbol=symbol,
        open=price, high=price * 1.01, low=price * 0.99, close=price,
        volume=volume, amount=price * volume, prev_close=price,
    )


def coexist_account(etf_position_pct: float = 0.20) -> BrokerAccount:
    """并存模式账户：先买入 ETF 占用约 20% 初始现金，返回已建立 ETF 持仓的账户。

    个股与 ETF 共用同一现金池：ETF 买入后 ``cash`` 从 100 万降到约 79.998 万，
    ``available_cash`` 同步下降（这是并存模式下个股买单被「间接」限制的现金来源）。
    """
    config = StrategyConfig(
        etf_beta_coexist=True,
        etf_position_pct=etf_position_pct,
        slippage_bps=0,
        volume_participation=1.0,
    )
    acct = BrokerAccount(INITIAL_CASH, config, name="共存测试")
    assert acct.config.etf_beta_coexist is True
    assert acct.config.etf_position_pct == etf_position_pct

    acct.new_day("2025-01-02")
    acct.submit(OrderRequest(
        symbol=ETF, side="BUY", quantity=ETF_QTY,
        purpose="BETA", execution_mode="NEXT_OPEN", order_type="MARKET", reason="测试",
    ), datetime(2025, 1, 2, 15, 0))
    fills = acct.process_bar(make_bar(ETF, 2, 9, 30, ETF_PRICE))
    assert len(fills) == 1 and fills[0].purpose == "BETA"
    assert fills[0].quantity == ETF_QTY
    # 现金被 ETF 占用约 20%（200,000 / 1,000,000），费用使实际略低
    assert acct.cash == pytest.approx(INITIAL_CASH - ETF_QTY * ETF_PRICE, abs=30.0)
    return acct


def _max_affordable_qty(acct: BrokerAccount, symbol: str, price: float, start_qty: int) -> int:
    """复刻 broker.process_bar 的现金降档循环，推导「现金可负担的最大整手」。"""
    from app.domain import money

    rules = acct.board_rules(symbol)
    step = max(1, rules.step)
    qty = start_qty
    while qty >= step:
        amount = money(price * qty)
        fees = acct._fees("BUY", amount, symbol)
        if amount + fees.total_fee <= acct.cash:
            return qty
        qty -= step
    return 0


def test_stock_entry_amount_not_scaled_by_beta_ratio():
    """并存 + ETF 持仓下，尾盘首仓金额 = installment_amounts[0]，未被按 0.80 缩放。"""
    acct = coexist_account(etf_position_pct=0.20)
    tier0 = acct.config.installment_amounts[0]
    assert tier0 == 100_000.0

    acct.new_day("2025-01-03")
    strategy = LayeredTStrategy(acct.config)
    signal_bar = make_bar(STOCK, 3, 14, 35, STOCK_PRICE)
    requests = strategy._tail_entry_requests(signal_bar, acct, None)
    assert len(requests) == 1
    req = requests[0]

    # 数量按「全额档位金额」折算，而非 0.80 × 档位
    full_qty = board_buy_quantity(STOCK, tier0, STOCK_PRICE)
    scaled_qty = board_buy_quantity(STOCK, tier0 * 0.80, STOCK_PRICE)
    assert full_qty == 10_000
    assert scaled_qty == 8_000
    assert req.quantity == full_qty
    assert req.quantity != scaled_qty

    # 剩余现金充足 → 全额成交（金额未被 beta 缩放）
    acct.submit(req, signal_bar.timestamp)
    fills = acct.process_bar(make_bar(STOCK, 3, 14, 40, STOCK_PRICE))
    assert len(fills) == 1
    assert fills[0].quantity == full_qty
    assert fills[0].amount == pytest.approx(tier0, rel=1e-6)


def test_stock_buy_capped_by_cash_not_beta_ratio():
    """个股买单超出剩余可用现金 → broker 按现金降档；结果由现金决定，非 beta 比例。"""
    acct = coexist_account(etf_position_pct=0.20)
    remaining_cash = acct.cash
    assert remaining_cash == pytest.approx(INITIAL_CASH - ETF_QTY * ETF_PRICE, abs=30.0)

    acct.new_day("2025-01-03")
    amount = 900_000.0  # 远超剩余现金（约 79.998 万）
    strategy = LayeredTStrategy(acct.config)
    req = strategy._buy_request(make_bar(STOCK, 3, 14, 40, STOCK_PRICE_HIGH), amount, "测试")

    full_qty = board_buy_quantity(STOCK, amount, STOCK_PRICE_HIGH)          # 9,000（未缩放）
    scaled_qty = board_buy_quantity(STOCK, amount * 0.80, STOCK_PRICE_HIGH)  # 7,200（0.80 缩放）
    assert req.quantity == full_qty == 9_000
    assert scaled_qty == 7_200
    # 精确预期：broker 现金降档循环推出的「最大可负担整手」（在成交前按当前现金推导）
    expected = _max_affordable_qty(acct, STOCK, STOCK_PRICE_HIGH, full_qty)
    assert expected == 7_900

    acct.submit(req, datetime(2025, 1, 3, 14, 40))
    fills = acct.process_bar(make_bar(STOCK, 3, 14, 45, STOCK_PRICE_HIGH))
    assert len(fills) == 1
    fill = fills[0]

    # 现金封顶（降档）：成交金额 + 费用 ≤ 剩余现金
    assert fill.amount + fill.total_fee <= remaining_cash + 1e-6
    # 结果既不是「全额」也不是「0.80 缩放」——由现金决定，而非 beta 比例
    assert fill.quantity != full_qty
    assert fill.quantity != scaled_qty
    assert fill.quantity == expected
    assert fill.amount == pytest.approx(STOCK_PRICE_HIGH * expected, abs=0.01)


def test_stock_buy_quantity_invariant_to_etf_position_pct():
    """个股买入成交数量不随 etf_position_pct 变化（该参数只约束 ETF 腿）。"""
    quantities = []
    for pct in (0.20, 0.50):
        acct = coexist_account(etf_position_pct=pct)
        acct.new_day("2025-01-03")
        strategy = LayeredTStrategy(acct.config)
        req = strategy._buy_request(
            make_bar(STOCK, 3, 14, 40, STOCK_PRICE_HIGH), 900_000.0, "测试",
        )
        acct.submit(req, datetime(2025, 1, 3, 14, 40))
        fills = acct.process_bar(make_bar(STOCK, 3, 14, 45, STOCK_PRICE_HIGH))
        assert len(fills) == 1
        quantities.append(fills[0].quantity)
    # 两个不同的 beta 比例（0.20 vs 0.50）→ 个股买入成交数量完全相同（不读该参数）
    assert quantities[0] == quantities[1] == 7_900
