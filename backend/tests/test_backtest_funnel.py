"""选股漏斗轮动回测单测：tmp_path 造假 UniverseStore，不联网、不打 baostock。

覆盖：正常跑通出报告（rounds/screen 快照）、清仓后换票、L0 关门空仓等待、
强制关闭财务深筛、universe 日线为空报错。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.backtest import FunnelRotationBacktest
from app.domain import Bar, StrategyConfig
from app.screener import ScreenerConfig
from app.universe import BOARD_GEM, BOARD_INDEX, BOARD_STAR, UniverseRow, UniverseStore

START = datetime(2025, 1, 1)
N_BARS = 260
AMOUNT = 6e8  # 满足 L3：当日/20日均额 ≥ 5亿


class _DummyMarket:
    """漏斗轮动回测不联网：include_realtime=False 且关闭财务深筛时不该触碰 market。"""


def _bars(code: str, closes: list[float], *, amounts: list[float] | None = None) -> list[Bar]:
    result: list[Bar] = []
    previous: float | None = None
    for index, close in enumerate(closes):
        high = max(close, previous or close) * 1.01
        low = min(close, previous or close) * 0.99
        result.append(Bar(
            timestamp=START + timedelta(days=index), symbol=code,
            open=close, high=high, low=low, close=close,
            volume=1e6, amount=(amounts[index] if amounts else AMOUNT),
            timeframe="daily", turnover_rate=0.05,
            prev_close=previous if previous is not None else close,
        ))
        previous = close
    return result


def _index_closes(*, gate_up: bool, count: int = N_BARS + 60) -> list[float]:
    """板块指数日线：全程单调，保证任意时点 20 日收益符号稳定。

    - gate_up=False（默认）：全程缓跌 → 选中日与建仓日 ret20 均 < 0，L0 开门，
      策略大盘门也放行首笔（index_return < 0）。
    - gate_up=True：全程缓涨 → ret20 均 > 0，L0 关门 + 策略大盘门挡首笔 → 空仓等待。
    覆盖 N_BARS + 60 根（比股票 tail 更长），保证窗口后段
    strategy.on_daily 的 ``len(index_history) >= 21`` 始终成立。
    """
    drift = 3.0 if gate_up else -3.0
    base = 6000.0
    return [max(1.0, base + drift * index) for index in range(count)]


def _stock_closes(drop_to: float, tail: list[float], *, base: float = 200.0, count: int = N_BARS) -> list[float]:
    """前段从 base 线性跌到 drop_to，末段用 tail 覆盖（选中时 drop_to 满足 L1）。

    回测窗口 2025-08-20 ≈ 第 231 根，落在 drop_to 平台期；tail 从第 231 根后才开始，
    用来制造「建仓后上涨→止盈」或「建仓后横盘」。
    """
    n_drop = count - len(tail)
    closes = [base - (base - drop_to) * index / max(1, n_drop - 1) for index in range(n_drop)]
    return closes + tail


def _build_store(path, *, gate_up: bool = False) -> UniverseStore:
    store = UniverseStore(path)
    for board in (BOARD_GEM, BOARD_STAR):
        # 指数：有界正值 + 覆盖 N_BARS+60 根（比股票 tail 更长），保证窗口后段
        # strategy.on_daily 的 len(index_history)>=21 大盘门始终成立。
        # 末段 21 根单边下行 → ret20<0（开门）；gate_up 则单边上行（关门）。
        store.upsert_index(BOARD_INDEX[board], _bars(BOARD_INDEX[board], _index_closes(gate_up=gate_up)))
    # 选中日（8/20）跌幅排序：300101 最深→排第一；300102 次之。
    # tail（第231根=8/20 起，共29根到 9/17）设计目标：断言「清仓后会换票到不同标的」。
    # - 300101：低位横盘 9 根（8/21 开盘@30 建仓）→ 4 根温和上行触发价格轨 15% 止盈清仓；
    #   止盈时它已涨到高位，chg20 转正跌出 L1，不会被立刻重选。
    # - 300102：全程持续阴跌（chg20 保持负），300101 退出 L1 后成为剩余最深 → 换票后的新 first；
    #   缓跌不触发止盈 → 持有到窗口结束（status=OPEN）。
    low_bars = 9
    climb = [20.0, 22.0, 24.0, 26.0]
    flat_high = [26.0] * (29 - low_bars - len(climb))
    for code, name, board, drop, tail in [
        ("sz.300101", "优选一", BOARD_GEM, 20.0, [20.0] * low_bars + climb + flat_high),
        ("sz.300102", "优选二", BOARD_GEM, 40.0, [40.0 - i * 0.2 for i in range(29)]),
        ("sz.300103", "备选三", BOARD_GEM, 50.0, [50.0 - i * 0.1 for i in range(29)]),
    ]:
        closes = _stock_closes(drop, tail)
        store.upsert_rows([UniverseRow(code=code, board=board, name=name)])
        store.upsert_daily(code, _bars(code, closes))
    return store


def _run(store, **overrides) -> dict:
    options: dict = {
        "screener_config": ScreenerConfig(boards=[BOARD_GEM], enable_fundamental_veto=False),
        "initial_cash": 4_000_000.0, "config": StrategyConfig(),
        "start_date": "2025-08-20", "end_date": "2025-12-31",
    }
    options.update(overrides)
    engine = FunnelRotationBacktest(store=store, market=_DummyMarket(), name="漏斗轮动测试", **options)
    return engine.run()


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_funnel_backtest_completes_with_round_and_screen(tmp_path):
    store = _build_store(tmp_path / "u.db")
    report = _run(store)

    assert report["status"] == "COMPLETED"
    assert report["data_info"]["mode"] == "funnel_rotation"
    assert report["metrics"]["total_rounds"] >= 1
    for closed in [item for item in report["rounds"] if item["status"] == "CLOSED"]:
        assert closed["symbol"]
        assert closed["screen"] is not None
        assert closed["screen"]["as_of"]
        assert "after_l1" in closed["screen"]["funnel"]
        assert closed["chg20d_at_select"] is not None
    assert report["data_info"]["screening_count"] >= 1
    # JSON 安全
    import json
    json.dumps(report, ensure_ascii=False, allow_nan=False)
    # 降级说明
    assert any("日线口径" in item for item in report["warnings"])


def test_funnel_switches_symbol_after_take_profit(tmp_path):
    store = _build_store(tmp_path / "u.db")
    report = _run(store)

    trail = report["data_info"]["rotation_trail"]
    symbols = [item["symbol"] for item in trail if item["symbol"]]
    assert len(set(symbols)) >= 2, f"应出现换票，实际轨迹={symbols}"
    assert report["data_info"]["screening_count"] >= 2
    # 每条轨迹字段齐全
    for item in trail:
        assert {"round", "symbol", "select_date", "gate_open", "status"} <= set(item)


def test_funnel_gate_closed_waits_flat(tmp_path):
    store = _build_store(tmp_path / "u.db", gate_up=True)
    report = _run(store)

    assert report["metrics"]["total_rounds"] == 0
    trail = report["data_info"]["rotation_trail"]
    assert trail and all(item["status"] == "WAITING" and item["symbol"] is None for item in trail)
    assert any(item["gate_open"] is False for item in trail)
    assert any("L0" in item or "空仓等待" in item for item in report["warnings"])


def test_funnel_forces_fundamental_veto_off(tmp_path):
    store = _build_store(tmp_path / "u.db")
    report = _run(store, screener_config=ScreenerConfig(
        boards=[BOARD_GEM], enable_fundamental_veto=True,
    ))

    assert report["config"]["screener"]["enable_fundamental_veto"] is False
    assert any("enable_fundamental_veto" in item for item in report["warnings"])


def test_funnel_empty_universe_raises(tmp_path):
    store = UniverseStore(tmp_path / "empty.db")
    with pytest.raises(ValueError):
        FunnelRotationBacktest(
            store=store, market=_DummyMarket(), screener_config=ScreenerConfig(boards=[BOARD_GEM]),
        )