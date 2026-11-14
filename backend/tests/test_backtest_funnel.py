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
from app.strategy import LayeredTStrategy
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
# 长区间回测回归：回测区间跨过「最近 260 根日线 / 最近 2400 根 5 分钟」
# ---------------------------------------------------------------------------

LONG_N_BARS = 560


def _long_dates() -> list[str]:
    return [(START + timedelta(days=index)).date().isoformat() for index in range(LONG_N_BARS)]


def _build_long_store(path) -> tuple[UniverseStore, list[str]]:
    """560 根日线的 store：回测区间起点早于「最近 260 根」窗口。

    指数持续缓跌 → L0 开门；个股持续阴跌 → 任意时点都满足 L1 超跌条件，
    因此「能不能在区间前段建仓」只取决于回测的数据窗口是否正确。
    """
    store = UniverseStore(path)
    index_closes = [round(6000.0 * 0.999 ** index, 4) for index in range(LONG_N_BARS)]
    for board in (BOARD_GEM, BOARD_STAR):
        store.upsert_index(BOARD_INDEX[board], _bars(BOARD_INDEX[board], index_closes))
    codes: list[str] = []
    for code, name, base in (("sz.300201", "长窗一", 200.0), ("sz.300202", "长窗二", 120.0)):
        closes = [round(base * 0.99 ** index, 4) for index in range(LONG_N_BARS)]
        store.upsert_rows([UniverseRow(code=code, board=BOARD_GEM, name=name)])
        store.upsert_daily(code, _bars(code, closes))
        codes.append(code)
    return store, codes


def test_funnel_long_window_builds_position_before_recent_tail(tmp_path):
    """区间 > 260 根时，前半段必须仍能选股建仓（回归「只取最近 260 根」的窗口 bug）。"""
    store, _ = _build_long_store(tmp_path / "u.db")
    dates = _long_dates()
    report = _run(store, start_date=dates[100], end_date=dates[-1])

    assert report["data_info"]["screening_count"] >= 1
    assert report["metrics"]["total_rounds"] >= 1, "长区间前段应能建仓，实际没有轮次"
    # 最近 260 根从 dates[-260] 起；建仓必须发生在那之前，才证明窗口覆盖了区间前段。
    assert report["rounds"][0]["start_date"] <= dates[150]
    assert report["rounds"][0]["start_date"] < dates[-260]


def test_funnel_long_window_minute5_covers_early_holdings(tmp_path):
    """区间 > 2400 根 5 分钟 bar 时，前段持仓也要逐根处理 5 分钟 bar（回归分钟截断 bug）。"""
    store, codes = _build_long_store(tmp_path / "u.db")
    dates = _long_dates()
    quan = _FakeQuan({code: dates for code in codes})

    orig_on_intraday = LayeredTStrategy.on_intraday
    intraday_dates: list[str] = []

    def tracked_on_intraday(self, history, recent, bar, account, **kwargs):
        intraday_dates.append(bar.date)
        return orig_on_intraday(self, history, recent, bar, account, **kwargs)

    LayeredTStrategy.on_intraday = tracked_on_intraday
    try:
        _run(store, start_date=dates[100], end_date=dates[-1], quan=quan)
    finally:
        LayeredTStrategy.on_intraday = orig_on_intraday

    assert intraday_dates, "传 QuanStore 后应逐根处理 5 分钟 bar"
    # 最近 2400 根 5 分钟 bar ≈ 最后 50 个交易日；前段持仓被处理说明没有按 tail 截断。
    assert min(intraday_dates) < dates[200]


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


# ---------------------------------------------------------------------------
# 5 分钟做 T：传入 QuanStore 后逐根处理 minute5，触发日内 T
# ---------------------------------------------------------------------------


class _FakeMinute5Bar(Bar):
    """构造 5 分钟 bar 的辅助（继承 Bar，带 5m timeframe）。"""


class _FakeQuan:
    """假 QuanStore：为每个候选返回振荡 5 分钟 bar（触发做 T 准入）。

    振荡设计：日内 48 根 5 分钟 bar 在 [low, high] 来回摆动，振幅 >4% 满足
    t_min_amplitude；换手率固定 6%（落在 [3%,12%]）；bid/ask 用 close±0.2% 代理
    （净价差 >3% 由振荡幅度保证）。on_intraday 会在满足准入时下 T 单。
    """

    def __init__(self, codes_dates: dict[str, list[str]]) -> None:
        # {standard_code: {date: [Bar x48]}}
        self._by_code: dict[str, dict[str, list[Bar]]] = {}
        for code, dates in codes_dates.items():
            per_date: dict[str, list[Bar]] = {}
            for date in dates:
                per_date[date] = self._oscillate(code, date)
            self._by_code[code] = per_date

    @staticmethod
    def _oscillate(code: str, date: str) -> list[Bar]:
        base = 20.0
        bars: list[Bar] = []
        # 09:35 起 48 根（9:30 开盘后每 5 分钟），收盘价在 20±0.3 温和振荡（日内振幅 ~3%）。
        # 幅度刻意压小：太大（±1.2）会让策略建仓首日就触发 15% 止盈清仓，底仓建不满 4 笔，
        # 做T 永远卡在 sequence_index<4。温和振荡让底仓逐步建满、持有期足够长，做T 才有机会触发。
        for i in range(48):
            minute = 35 + i * 5
            hour, mi = 9 + minute // 60, minute % 60
            if hour >= 12 and hour < 13:
                hour, mi = 13, mi  # 简化午休
            ts = datetime.fromisoformat(f"{date}T{hour:02d}:{mi:02d}:00")
            close = base + (0.3 if i % 2 == 0 else -0.3)
            high, low = close * 1.004, close * 0.996
            bid, ask = close * 0.998, close * 1.002
            bars.append(Bar(
                timestamp=ts, symbol=code, open=close, high=high, low=low, close=close,
                volume=1e5, amount=2e6, timeframe="5m", turnover_rate=0.06,
                bid=bid, ask=ask, prev_close=base,
            ))
        return bars

    def load_minute5(self, code: str, *, max_rows: int | None = None,
                     start: str | None = None, end: str | None = None,
                     daily_turnover: dict[str, float] | None = None) -> list[Bar]:
        per_date = self._by_code.get(code, {})
        result: list[Bar] = []
        for date in sorted(per_date):
            if start and date < start:
                continue
            if end and date > end:
                continue
            result.extend(per_date[date])
        return result[-int(max_rows):] if max_rows else result


def test_funnel_intraday_t_processes_minute5(tmp_path):
    """传 QuanStore 后，持仓期 5 分钟 bar 被逐根处理并喂给 on_intraday。

    注意：做 T「成交」依赖底仓建满 4 笔（sequence_index>=4）+ 当日准入（振幅/换手/盘口），
    是市场条件问题；本测试验证的是**机制**——传 QuanStore 后 5 分钟 bar 真的被逐根处理，
    而非像无 quan 时只调 on_daily。机制正确后，真实数据下底仓建满时做 T 自然触发。
    """
    store = _build_store(tmp_path / "u.db")
    # 给所有候选造 5 分钟数据（覆盖建仓窗口 8/20~9/17 的交易日）
    dates = [(START + timedelta(days=i)).date().isoformat() for i in range(260)]
    codes = ["sz.300101", "sz.300102", "sz.300103"]
    quan = _FakeQuan({code: dates for code in codes})

    # 跟踪 on_intraday 调用次数（确认 5 分钟 bar 被逐根处理）
    orig_on_intraday = LayeredTStrategy.on_intraday
    intraday_calls: list[tuple[str, str]] = []

    def tracked_on_intraday(self, history, recent, bar, account, **kwargs):
        intraday_calls.append((bar.symbol, bar.date))
        return orig_on_intraday(self, history, recent, bar, account, **kwargs)

    LayeredTStrategy.on_intraday = tracked_on_intraday
    try:
        report = _run(store, quan=quan)
    finally:
        LayeredTStrategy.on_intraday = orig_on_intraday

    # 启用 5 分钟做 T 的警告
    assert any("5分钟做T" in item for item in report["warnings"])
    # 5 分钟 bar 被逐根处理：on_intraday 被调用多次（每日 48 根 × 持有天数）
    assert len(intraday_calls) > 0, "传 QuanStore 后 on_intraday 应被调用（5 分钟 bar 逐根处理）"
    # 每根 5 分钟 bar 的换手/盘口被填充（做 T 准入数据源就绪）
    assert any(code == "sz.300101" for code, _ in intraday_calls)


def test_funnel_without_quan_stays_daily(tmp_path):
    """无 QuanStore 时保持日线口径，on_intraday 不被调用（回归保护）。"""
    store = _build_store(tmp_path / "u.db")

    orig_on_intraday = LayeredTStrategy.on_intraday
    intraday_calls: list[tuple[str, str]] = []

    def tracked_on_intraday(self, history, recent, bar, account, **kwargs):
        intraday_calls.append((bar.symbol, bar.date))
        return orig_on_intraday(self, history, recent, bar, account, **kwargs)

    LayeredTStrategy.on_intraday = tracked_on_intraday
    try:
        report = _run(store)
    finally:
        LayeredTStrategy.on_intraday = orig_on_intraday

    assert any("日线口径" in item for item in report["warnings"])
    assert len(intraday_calls) == 0, f"无 quan 时 on_intraday 不应被调用，实际 {len(intraday_calls)} 次"