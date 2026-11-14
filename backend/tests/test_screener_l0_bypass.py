"""L0 大盘门放宽（l0_ret20_open / l0_stock_bypass_chg20d）单测。"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.domain import Bar
from app.screener import ScreenerConfig, run_screener
from app.universe import BOARD_GEM, BOARD_INDEX, UniverseRow, UniverseStore


def _bars(code: str, closes: list[float], *, amount: float = 6e8) -> list[Bar]:
    result: list[Bar] = []
    previous: float | None = None
    for index, close in enumerate(closes):
        high = max(close, previous or close) * 1.01
        low = min(close, previous or close) * 0.99
        result.append(Bar(
            timestamp=datetime(2025, 1, 1) + timedelta(days=index), symbol=code,
            open=close, high=high, low=low, close=close,
            volume=1_000_000, amount=amount, timeframe="daily",
            turnover_rate=0.03, prev_close=previous if previous is not None else close,
        ))
        previous = close
    return result


def _build_store(path) -> UniverseStore:
    store = UniverseStore(path)
    # 指数持续缓涨 → ret20 恒 > 0 → L0 关门
    index_closes = [100.0 + index * 0.5 for index in range(260)]
    store.upsert_index(BOARD_INDEX[BOARD_GEM], _bars(BOARD_INDEX[BOARD_GEM], index_closes))
    # 个股持续阴跌 → 末段 Chg20D ≈ -33%（< -25% 深度超跌，> -15% L1 阈值）
    for code, base in (("sz.300001", 200.0), ("sz.300002", 100.0)):
        closes = [round(base * 0.98 ** index, 4) for index in range(260)]
        store.upsert_rows([UniverseRow(code=code, board=BOARD_GEM, name="阴跌样本")])
        store.upsert_daily(code, _bars(code, closes))
    return store


def test_l0_default_closes_gate_when_index_up(tmp_path):
    store = _build_store(tmp_path / "u.db")
    result = run_screener(store, None, ScreenerConfig(boards=[BOARD_GEM], enable_fundamental_veto=False))
    assert result.gate["open"] is False
    assert result.funnel["after_l0"] == 0
    assert result.first is None


def test_l0_ret20_open_relaxes_gate(tmp_path):
    """把开门阈值从 <0 放宽到 <+5%：缓涨指数也能开门。"""
    store = _build_store(tmp_path / "u.db")
    config = ScreenerConfig(
        boards=[BOARD_GEM], enable_fundamental_veto=False, l0_ret20_open=0.05,
    )
    result = run_screener(store, None, config)
    assert result.gate["open"] is True
    assert result.funnel["after_l0"] == 2


def test_l0_stock_bypass_opens_deep_oversold(tmp_path):
    """L0 关门但板块内存在 Chg20D < -25% 的标的时，为它们单独开门。"""
    store = _build_store(tmp_path / "u.db")
    config = ScreenerConfig(
        boards=[BOARD_GEM], enable_fundamental_veto=False, l0_stock_bypass_chg20d=-0.25,
    )
    result = run_screener(store, None, config)
    # 指数缓涨 → L0 关门，但 bypass 生效应开门
    assert result.gate["open"] is True
    # 样本个股 260 根 0.99^i 阴跌，末段 Chg20D 远低于 -25%
    assert result.funnel["after_l0"] >= 1


def test_l0_stock_bypass_off_by_default(tmp_path):
    """不配 l0_stock_bypass_chg20d 时，L0 关门就完全不选股（向后兼容）。"""
    store = _build_store(tmp_path / "u.db")
    config = ScreenerConfig(boards=[BOARD_GEM], enable_fundamental_veto=False)
    result = run_screener(store, None, config)
    assert result.gate["open"] is False
    assert result.first is None
