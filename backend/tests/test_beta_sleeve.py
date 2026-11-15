"""指数 beta 腿（ETF）纯逻辑单测：均线入场、双退出、仓位取整。"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.beta_sleeve import (
    entry_allowed, exit_reason, reentry_cooldown_ready, sma, target_quantity,
)
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


def test_entry_max_deviation_blocks_chasing():
    """不追高：指数偏离入场均线超过阈值时不建仓；0 = 关闭（保持旧行为）。"""
    # 前 9 根 10、最后一根 20：均线 11，偏离 +81.8%
    chased = _bars([10] * 9 + [20])
    assert entry_allowed(chased, 10) is True                     # 旧行为：站上均线就买
    assert entry_allowed(chased, 10, max_deviation=0.08) is False
    # 小幅偏离（均线 10.05，收盘 10.5 → +4.5%）允许入场
    mild = _bars([10] * 9 + [10.5])
    assert entry_allowed(mild, 10, max_deviation=0.08) is True
    # 未站上均线时与偏离保护无关，仍然拒绝
    below = _bars([10] * 10 + [9.5])
    assert entry_allowed(below, 10, max_deviation=0.08) is False


def test_reentry_cooldown_counts_trading_days():
    """清仓后冷却按交易日计；无平仓日 / 冷却关闭 / 日历缺失都放行。"""
    days = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08"]
    assert reentry_cooldown_ready(None, "2025-01-08", 3, days) is True
    assert reentry_cooldown_ready("2025-01-02", "2025-01-08", 0, days) is True
    # 只过了 2 个交易日（01-03、01-06）→ 未满足 3 日冷却
    assert reentry_cooldown_ready("2025-01-02", "2025-01-06", 3, days) is False
    # 已过 3 个交易日（01-03、01-06、01-07）→ 满足
    assert reentry_cooldown_ready("2025-01-02", "2025-01-07", 3, days) is True
    # 同一天不清仓（today <= exit_date）→ 未满足
    assert reentry_cooldown_ready("2025-01-07", "2025-01-07", 3, days) is False
    # 交易日历不可用 → 放行（避免因数据缺失永久空仓）
    assert reentry_cooldown_ready("2025-01-02", "2025-01-07", 3, None) is True


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
