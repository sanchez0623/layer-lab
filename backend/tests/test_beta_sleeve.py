"""指数 beta 腿（ETF）纯逻辑单测：均线入场、双退出、仓位取整。"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.beta_sleeve import entry_allowed, exit_reason, sma, target_quantity
from app.domain import Bar


def _bars(closes: list[float], symbol: str = "sz.399006") -> list[Bar]:
    start = datetime(2025, 1, 1)
    return [
        Bar(
            timestamp=start + timedelta(days=index), symbol=symbol,
            open=close, high=close, low=close, close=close,
            volume=1e6, amount=1e8, timeframe="daily",
        )
        for index, close in enumerate(closes)
    ]


def test_sma_needs_full_window():
    assert sma([1, 2, 3], 3) == 2
    assert sma([1, 2], 3) is None
    assert sma([], 20) is None


def test_entry_requires_close_above_ma():
    assert entry_allowed(_bars([10, 11, 12, 13, 14]), 5)
    assert not entry_allowed(_bars([14, 13, 12, 11, 10]), 5)
    # 样本不足：不建仓（宁可空仓，不用半截均线）
    assert not entry_allowed(_bars([10, 11, 12]), 5)


def test_entry_refuses_below_exit_ma():
    """跌破出场均线后不得第二天就买回（否则 T+1 下买入当天卖不掉，白扛一天）。"""
    bars = _bars([20, 20, 10, 10, 10, 12])
    assert entry_allowed(bars, 3) is True
    assert entry_allowed(bars, 3, 5) is False


def test_exit_ma_break():
    bars = _bars([10, 11, 12, 11, 9])
    reason = exit_reason(bars, 2.0, 2.0, exit_ma_days=3, hard_stop_pct=0.06)
    assert reason == "指数跌破3日线"


def test_exit_hard_stop_takes_priority():
    bars = _bars([10, 11, 12, 13, 14])
    reason = exit_reason(bars, 2.0, 1.80, exit_ma_days=3, hard_stop_pct=0.06)
    assert reason == "ETF硬止损6.0%"


def test_no_exit_while_trend_up():
    bars = _bars([10, 11, 12, 13, 14])
    assert exit_reason(bars, 2.0, 2.2, exit_ma_days=3, hard_stop_pct=0.06) is None


def test_target_quantity_rounds_to_lot():
    # 100 万权益 × 20% = 20 万；价格 2.0 → 10 万份，按 100 份取整
    assert target_quantity(1_000_000, 0.20, 2.0) == 100_000
    # 不足一手 → 0（不建仓）
    assert target_quantity(500, 0.20, 2.0) == 0
    assert target_quantity(1_000_000, 0.20, 0) == 0
    assert target_quantity(0, 0.20, 2.0) == 0
