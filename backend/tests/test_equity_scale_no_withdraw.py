"""equity 缩放分母去掉提现（bugfix/equity-scale-no-withdraw）。

现状（bug）：``_installment_amount`` 在 ``installment_scale_mode="equity"`` 下把
累计提现 ``total_withdrawn`` 加回权益当缩放基数 ``(equity + withdrawn) / initial``。
问题：提现已经离场、不再承担波动，把它加回会虚高 scale，在权益并未真正增长时
放大加仓/做T敞口（报告 300548：equity ~5.5M + 已提现 ~3.5M / initial 4M →
scale 2.25，剔除提现后应为 1.375）。

修复后：scale = 当前权益 / 初始本金，``installment_scale_floor`` 兜底逻辑不变。
"""
from __future__ import annotations

import pytest

from app.broker import BrokerAccount
from app.domain import StrategyConfig
from app.strategy import LayeredTStrategy


def _config() -> StrategyConfig:
    return StrategyConfig(
        installment_scale_mode="equity",
        installment_scale_floor=0.5,
    )


def test_scale_not_inflated_by_withdrawn() -> None:
    """有 total_withdrawn 时 scale 不被抬高：只用在场权益，不加回提现。

    equity 5.5M + 已提现 3.5M、initial 4M → scale = 5.5/4 = 1.375（不是 9/4 = 2.25），
    档基数 300_000 → 412_500（不是 675_000）。
    """
    config = _config()
    strategy = LayeredTStrategy(config)
    account = BrokerAccount(initial_cash=4_000_000.0, config=config)
    account.cash = 2_500_000.0
    account.total_withdrawn = 3_500_000.0
    # 造 3_000_000 持仓市值 → equity = 2.5M + 3M = 5.5M。
    from app.domain import Position

    position = Position(symbol="300548", permanent_qty=100_000, last_price=30.0)
    account.positions[position.symbol] = position
    assert account.equity == pytest.approx(5_500_000.0, rel=1e-9)

    # index=8 → 档基数 300_000（默认 16 笔为 4×10万 + 4×20万 + 4×30万 + 4×40万）。
    assert strategy._installment_amount(account, 8) == pytest.approx(412_500.0, rel=1e-9)
    # 若把提现加回会得到 675_000，明确断言不是这个值（防回归）。
    assert strategy._installment_amount(account, 8) != pytest.approx(675_000.0, rel=1e-9)


def test_no_withdrawal_behavior_unchanged() -> None:
    """无提现（total_withdrawn=0）时行为不变：equity == initial → 返回基数原值（回归护栏）。"""
    config = _config()
    strategy = LayeredTStrategy(config)
    account = BrokerAccount(initial_cash=4_000_000.0, config=config)
    assert account.total_withdrawn == 0.0
    assert account.equity == pytest.approx(account.initial_cash, rel=1e-9)
    for index in range(16):
        assert strategy._installment_amount(account, index) == pytest.approx(
            config.installment_amounts[index], rel=1e-9
        )


def test_floor_still_applies() -> None:
    """floor 兜底仍生效：equity 远低于 initial×floor 时，返回 基数×floor。

    equity 400_000 / initial 4_000_000 → scale 0.1 < floor 0.5 → 档基数 100_000 × 0.5 = 50_000。
    """
    config = _config()
    strategy = LayeredTStrategy(config)
    account = BrokerAccount(initial_cash=4_000_000.0, config=config)
    account.cash = 400_000.0
    assert strategy._installment_amount(account, 0) == pytest.approx(50_000.0, rel=1e-9)
