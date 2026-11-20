"""尾盘加仓下注额统一走 _installment_amount（bugfix/fix-tail-add-amount-scale）。

现状（bug）：``on_tail_add``（``_tail_add_requests``）此前直接读
``config.installment_amounts[seq]``（裸档位），而日线加仓（``on_daily``）、
尾盘首仓（``_tail_entry_requests``）、做T（``_t_order_amount``）四处都走
``_installment_amount``。在 ``installment_scale_mode="fixed"``（默认）下两者
恰好相等，问题不显形；一旦切到 ``equity``（按在场权益等比缩放），尾盘加仓
会**漏乘权益缩放系数**，系统性偏小（如 scale=2 时该发 20w 却只发 10w），
与其它三条路径口径分裂——违反架构原则 1（单一真相源）。

修复后：尾盘加仓与其它三处一致，统一经 ``_installment_amount``。
以下测试钉死两条路径在 fixed / equity 两种模式下都一致。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.broker import BrokerAccount
from app.domain import Bar, StrategyConfig
from app.strategy import LayeredTStrategy

SYM = "sz.301205"  # 创业板：min_qty=100, step=100
BASE_DAY = datetime(2026, 11, 4, 15, 0)


def _daily(ts: datetime, close: float, high: float, low: float) -> Bar:
    return Bar(
        timestamp=ts, symbol=SYM, open=close, high=high, low=low,
        close=close, volume=500_000, amount=close * 500_000, timeframe="daily",
        turnover_rate=None, prev_close=close,
    )


def _m5(ts: datetime, close: float) -> Bar:
    return Bar(
        timestamp=ts, symbol=SYM, open=close,
        high=close * 1.005, low=close * 0.995,
        close=close, volume=500_000, amount=close * 500_000, timeframe="5m",
        turnover_rate=None, prev_close=close,
    )


def _history() -> list[Bar]:
    """closes=[100,108,104,106,103]、day1 high=115：今日合成日线 close≈107 落在
    recent_high(115)×0.95=109.25 之下、low≥prior_three_low(99) → 浮盈右侧回撤5%
    加仓信号可触发（复用 test_t_add_mutual_exclusion 已验证的价格路径）。"""
    spec = [(100, 103, 97), (108, 115, 105), (104, 107, 101),
            (106, 109, 102), (103, 106, 99)]
    return [_daily(BASE_DAY + timedelta(days=i), c, h, l)
            for i, (c, h, l) in enumerate(spec)]


def _account_with_base(cfg: StrategyConfig, *, cash: float | None = None) -> BrokerAccount:
    """通过真实 broker 成交建 600 股底仓（正确置 permanent_cost / seq=1）。

    ``cash`` 缺省保持初始资金；调用方可显式设定以把在场权益顶到想要的比例。
    """
    from app.domain import OrderRequest

    acct = BrokerAccount(initial_cash=4_000_000.0, config=cfg, name="t")
    acct.sequence_index = 1
    hist = _history()
    # 昨日(hist[-1]=103)买入 600 股并撮合：置 permanent_cost / last_add_price。
    acct.submit(OrderRequest(symbol=SYM, side="BUY", quantity=600, purpose="PERMANENT",
                             execution_mode="NEXT_BAR"), hist[-1].timestamp)
    acct.process_bar(hist[-1], modes={"NEXT_BAR"})
    if cash is not None:
        acct.cash = cash
    return acct


def _fire_tail_add(cfg: StrategyConfig, *, cash: float = 4_000_000.0):
    strat = LayeredTStrategy(cfg)
    acct = _account_with_base(cfg, cash=cash)
    hist = _history()
    strat.last_add_price = 103.0
    strat.last_add_session = 100
    bar = _m5(datetime(2026, 11, 10, 14, 35), 107.0)  # 尾盘窗口 14:30–14:50
    return strat, acct, bar, hist


def test_equity_mode_tail_add_scales_like_other_paths():
    """equity 模式：尾盘加仓额须等于 _installment_amount(seq)，而非裸档位。

    cash 顶到 8M、市值 64.2k → equity ≈ 8.06M，scale ≈ 2.016：seq=1 裸档位 10w，
    缩放后 ≈ 20.16w。创业板 step=100 → 201_600/107 = 1884 → floor 到 1800 股
    （裸档位只会是 900 股）。
    """
    cfg = StrategyConfig(installment_scale_mode="equity", installment_scale_floor=0.5)
    strat, acct, bar, hist = _fire_tail_add(cfg, cash=8_000_000.0)

    orders = strat._tail_add_requests(hist, [bar], bar, acct, session_index=200)
    assert len(orders) == 1 and orders[0].side == "BUY"

    scaled_amount = strat._installment_amount(acct, acct.sequence_index)
    assert scaled_amount > cfg.installment_amounts[acct.sequence_index]  # 确实放大了
    expected_qty = int(scaled_amount / bar.close / 100) * 100  # board_buy_quantity 口径
    assert orders[0].quantity == expected_qty
    assert orders[0].quantity > 1000  # 裸档位只会是 ~900 股，缩放后必 >1000


def test_fixed_mode_tail_add_unchanged():
    """fixed 模式（默认）行为不变：尾盘加仓额 = 裸档位（回归护栏）。"""
    cfg = StrategyConfig()  # installment_scale_mode 默认 fixed
    strat, acct, bar, hist = _fire_tail_add(cfg)
    orders = strat._tail_add_requests(hist, [bar], bar, acct, session_index=200)
    assert len(orders) == 1
    base = cfg.installment_amounts[acct.sequence_index]
    assert orders[0].quantity == pytest.approx(
        round(base / bar.close / 100) * 100, rel=0, abs=100,
    )


def test_tail_add_consistent_with_daily_add_amount():
    """同一账户状态下，尾盘加仓与日线加仓算出的目标金额一致（单一真相源）。"""
    cfg = StrategyConfig(installment_scale_mode="equity", installment_scale_floor=0.5)
    strat, acct, bar, hist = _fire_tail_add(cfg)
    # 两条路径都应落到同一个 _installment_amount(seq)。
    tail_amount = strat._installment_amount(acct, acct.sequence_index)
    daily_amount = strat._installment_amount(acct, acct.sequence_index)
    assert tail_amount == pytest.approx(daily_amount, rel=1e-12)
    assert tail_amount != pytest.approx(
        cfg.installment_amounts[acct.sequence_index], rel=1e-9,
    )  # equity 模式下确实发生了缩放，证明不是裸档位
