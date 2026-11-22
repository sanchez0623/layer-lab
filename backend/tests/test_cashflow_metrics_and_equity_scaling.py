"""P0/P1 新增功能的单测：现金流调整（TWR）指标块 + 16 笔金额的权益等比缩放。

- 现金流调整：把提现视为外部现金流而非亏损，重建 annualized/volatility/sharpe/maxDD/calmar。
  期望：无提现时与顶层口径一致；有提现时 Sharpe 显著高于「提现当亏损」口径、maxDD 更小。
- 权益等比缩放（installment_scale_mode=equity）：16 笔金额随 当前权益/初始本金 缩放
  （已提现金额不再加回，见 bugfix/equity-scale-no-withdraw）。
  期望：fixed 模式金额不变（向后兼容）；equity 模式放大/缩小、且受 scale_floor 兜底。
"""
from __future__ import annotations

import pytest

from app.backtest import cashflow_adjusted_metrics, performance_metrics
from app.domain import StrategyConfig
from app.strategy import LayeredTStrategy


def _curve(values: list[float], start: str = "2020-01-02") -> list[dict]:
    """构造最小 equity_curve：逐交易日推进（周末跳过，仅为了让日期合法）。"""
    from datetime import date, timedelta

    rows: list[dict] = []
    day = date.fromisoformat(start)
    for value in values:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        rows.append({
            "date": day.isoformat(),
            "equity": value,
            "market_value": 0.0,
            "beta_value": 0.0,
        })
        day += timedelta(days=1)
    return rows


def test_cashflow_adjusted_matches_top_level_when_no_withdrawals() -> None:
    """无提现时 TWR 块与顶层口径逐项一致（回归护栏：改动不影响无提现场景）。"""
    curve = _curve([1_000_000.0, 1_010_000.0, 1_005_000.0, 1_030_000.0, 1_050_000.0])
    metrics = performance_metrics(
        curve, 1_000_000.0, [], [], 0.0, 0.0, 0.0, 0.0, 0.0,
        withdrawals=[],
    )
    adjusted = metrics["cashflow_adjusted"]
    assert adjusted["max_drawdown"] == pytest.approx(metrics["max_drawdown"], abs=1e-12)
    assert adjusted["sharpe_ratio"] == pytest.approx(metrics["sharpe_ratio"], abs=1e-12)
    assert adjusted["volatility"] == pytest.approx(metrics["volatility"], abs=1e-12)


def test_cashflow_adjusted_reduces_drawdown_when_withdrawals_present() -> None:
    """提现被当成外部现金流而非亏损：maxDD 更小、Sharpe 显著更高。"""
    # 权益曲线里第 3 天「下跌」其实是 8 万提现造成的台阶，不是亏损。
    curve = _curve([1_000_000.0, 1_050_000.0, 970_000.0, 1_080_000.0, 1_100_000.0])
    withdrawals = [{"date": curve[2]["date"], "amount": 80_000.0, "period_profit": 50_000.0}]
    metrics = performance_metrics(
        curve, 1_000_000.0, [], [], 0.0, 0.0, 0.0, 0.0, 0.0,
        withdrawals=withdrawals,
    )
    adjusted = metrics["cashflow_adjusted"]
    # 顶层口径把提现当亏损 → 1-970k/1050k = 7.6% 计入 maxDD。
    assert metrics["max_drawdown"] == pytest.approx(1 - 970_000.0 / 1_050_000.0, abs=1e-12)
    # TWR 口径把提现加回 → 调整后权益单调上升，maxDD 归零。
    assert adjusted["max_drawdown"] == pytest.approx(0.0, abs=1e-12)
    # 调整后末值 1.10M + 已提现 80k = 1.18M → 总收益 18%。
    assert adjusted["total_return"] == pytest.approx(0.18, rel=1e-9)
    # Sharpe：TWR 口径没有提现日的假跌幅，显著高于顶层口径。
    assert adjusted["sharpe_ratio"] > metrics["sharpe_ratio"]


def test_cashflow_adjusted_annualized_uses_calendar_days() -> None:
    """年化按日历年（elapsed_days/365），与 annualized_return 口径一致。"""
    from datetime import date, timedelta

    start = date(2020, 1, 2)
    values = [1_000_000.0 * (1.1 ** (i / 252)) for i in range(253)]
    rows: list[dict] = []
    day = start
    for value in values:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        rows.append({"date": day.isoformat(), "equity": value, "market_value": 0.0, "beta_value": 0.0})
        day += timedelta(days=1)
    metrics = performance_metrics(rows, 1_000_000.0, [], [], 0.0, 0.0, 0.0, 0.0, 0.0)
    adjusted = metrics["cashflow_adjusted"]
    # ~1 年窗口 → TWR 年化与总收益接近（差异来自日历年 vs 252 交易日）。
    assert adjusted["annualized_return"] == pytest.approx(adjusted["total_return"], rel=0.05)


def test_installment_amount_fixed_mode_unchanged() -> None:
    """fixed 模式（默认）金额与配置逐字一致：向后兼容。"""
    config = StrategyConfig()
    strategy = LayeredTStrategy(config)
    from app.broker import BrokerAccount

    account = BrokerAccount(initial_cash=1_000_000.0, config=config)
    for index in range(16):
        assert strategy._installment_amount(account, index) == pytest.approx(config.installment_amounts[index])
    # sequence_index 越界时收敛到最后一笔（与原逻辑一致）。
    assert strategy._installment_amount(account, 99) == pytest.approx(config.installment_amounts[-1])


def test_installment_amount_equity_mode_scales_with_account() -> None:
    """equity 模式：金额随 (权益+已提现)/初始本金 缩放，并受 scale_floor 兜底。"""
    config = StrategyConfig(
        installment_scale_mode="equity",
        installment_scale_floor=0.5,
    )
    strategy = LayeredTStrategy(config)
    from app.broker import BrokerAccount

    # 权益翻倍 → 金额同比放大。
    grown = BrokerAccount(initial_cash=4_000_000.0, config=config)
    grown.cash = 8_000_000.0
    assert strategy._installment_amount(grown, 0) == pytest.approx(200_000.0, rel=1e-9)
    # 权益腰斩 → floor 兜底（0.5），不再往下压。
    halved = BrokerAccount(initial_cash=4_000_000.0, config=config)
    halved.cash = 1_000_000.0
    assert strategy._installment_amount(halved, 0) == pytest.approx(50_000.0, rel=1e-9)
    # 已提现的盈利不再加回（bugfix：提现已离场，不作为加仓依据）：
    # equity 4M / initial 4M → scale 1.0 → 与 fixed 模式同额。
    withdrawn = BrokerAccount(initial_cash=4_000_000.0, config=config)
    withdrawn.total_withdrawn = 4_000_000.0
    assert strategy._installment_amount(withdrawn, 0) == pytest.approx(100_000.0, rel=1e-9)


def test_installment_scale_mode_validation() -> None:
    """非法 mode / floor 必须在源头 400（fail fast），而不是让该项悄悄失效。"""
    with pytest.raises(ValueError, match="installment_scale_mode"):
        StrategyConfig(installment_scale_mode="percent")
    with pytest.raises(ValueError, match="installment_scale_floor"):
        StrategyConfig(installment_scale_mode="equity", installment_scale_floor=1.5)
    with pytest.raises(ValueError, match="installment_scale_floor"):
        StrategyConfig(installment_scale_mode="equity", installment_scale_floor=0.0)
    # 合法值通过。
    config = StrategyConfig(installment_scale_mode="equity", installment_scale_floor=0.5)
    assert config.installment_scale_mode == "equity"
    assert config.installment_scale_floor == 0.5


def test_performance_metrics_exposes_exposure_breakdown() -> None:
    """敞口拆分字段：总/个股/ETF 三项 + 完全空仓占比，供前端并列展示。"""
    curve = [
        {"date": "2020-01-02", "equity": 1_000_000.0, "market_value": 0.0, "beta_value": 0.0},
        {"date": "2020-01-03", "equity": 1_000_000.0, "market_value": 300_000.0, "beta_value": 100_000.0},
        {"date": "2020-01-06", "equity": 1_000_000.0, "market_value": 500_000.0, "beta_value": 500_000.0},
    ]
    metrics = performance_metrics(curve, 1_000_000.0, [], [], 0.0, 0.0, 0.0, 0.0, 0.0)
    assert metrics["exposure"] == pytest.approx(metrics["exposure_total"], rel=1e-12)
    assert metrics["exposure_stock"] == pytest.approx((0.0 + 0.2 + 0.0) / 3, rel=1e-12)
    assert metrics["exposure_beta"] == pytest.approx((0.0 + 0.1 + 0.5) / 3, rel=1e-12)
    assert metrics["idle_ratio"] == pytest.approx(1 / 3, rel=1e-12)


def test_performance_metrics_writes_equity_incl_withdrawal_per_point() -> None:
    """含出金权益逐点落库：提现日前等于原始 equity，提现日及其后加回累计提现。

    原始 equity 已扣掉提现已落袋的钱（负台阶），``equity_incl_withdrawal`` 把「截至当日的
    累计提现」加回，与 TWR 口径同源——曲线字段复用同一次累计计算，不另写累加逻辑。
    """
    curve = _curve([1_000_000.0, 1_050_000.0, 970_000.0, 1_080_000.0, 1_100_000.0])
    withdrawals = [{"date": curve[2]["date"], "amount": 80_000.0, "period_profit": 50_000.0}]
    performance_metrics(
        curve, 1_000_000.0, [], [], 0.0, 0.0, 0.0, 0.0, 0.0,
        withdrawals=withdrawals,
    )
    for index in range(2):  # 提现日之前：累计提现为 0，含出金 = 原始权益。
        assert curve[index]["equity_incl_withdrawal"] == pytest.approx(curve[index]["equity"], rel=1e-12)
    for index in range(2, 5):  # 提现日及其后：加回 8 万。
        assert curve[index]["equity_incl_withdrawal"] == pytest.approx(curve[index]["equity"] + 80_000.0, rel=1e-12)


def test_no_withdrawal_keeps_equity_incl_withdrawal_identical() -> None:
    """从未出金时派生字段与原始权益逐点相等（不引入无提现报告的额外台阶）。"""
    curve = _curve([1_000_000.0, 1_010_000.0, 1_005_000.0])
    performance_metrics(curve, 1_000_000.0, [], [], 0.0, 0.0, 0.0, 0.0, 0.0, withdrawals=[])
    for point in curve:
        assert point["equity_incl_withdrawal"] == pytest.approx(point["equity"], rel=1e-12)
