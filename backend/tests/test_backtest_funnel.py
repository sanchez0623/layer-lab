"""选股漏斗轮动回测单测：tmp_path 造假 UniverseStore，不联网、不打 baostock。

覆盖：正常跑通出报告（rounds/screen 快照）、清仓后换票、L0 关门空仓等待、
强制关闭财务深筛、universe 日线为空报错。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.backtest import FunnelRotationBacktest
from app.domain import Bar, StrategyConfig
from app.etf_data import cache_path, save_etf_bars
from app.rotation_trail import aggregate_trail, enrich_trail
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
    # 每月盈亏：月度切分覆盖全部净值点（漏斗引擎同样走 performance_metrics）。
    monthly = report["metrics"]["monthly"]
    assert monthly and sum(item["trading_days"] for item in monthly) == len(report["equity_curve"])
    # 换票轨迹：选中轮次带 gate 与候选前三；读时派生能算出轮次收益与空仓汇总。
    trail = report["data_info"]["rotation_trail"]
    trading_dates = [point["date"] for point in report["equity_curve"]]
    enriched = enrich_trail(trail, report["rounds"], trading_dates)
    selected = [item for item in enriched if item["kind"] == "ROUND"]
    assert selected, "至少应有一次选中标的"
    assert all(item.get("candidates") for item in selected)
    assert all(item.get("gate") is not None for item in enriched)
    closed = [item for item in selected if item.get("status") == "CLOSED"]
    assert closed and all(item["return_rate"] is not None for item in closed)
    assert all(item["holding_days"] >= 1 for item in closed if item.get("holding_days") is not None)
    summary = aggregate_trail(enriched, len(trading_dates))
    assert summary["screens"] == len(trail)
    assert summary["waiting_days"] + summary["rounds"] == summary["screens"]
    assert summary["holding_days"] <= len(trading_dates)
    # JSON 安全
    import json
    json.dumps(report, ensure_ascii=False, allow_nan=False)
    # 降级说明
    assert any("日线口径" in item for item in report["warnings"])


def test_funnel_daily_fills_have_open_timestamp_not_midnight(tmp_path):
    """日线口径成交的时间戳必须是开盘时刻 09:30，不能是 0 点（界面显示 bug 回归）。

    日线 bar 的时间戳只解析到日期（00:00:00），走日线口径的建仓/加仓/清仓成交会继承
    这个 0 点，界面上显示成「0 点成交」。归一化后应为同日 09:30:00（次日开盘撮合语义）。
    """
    store = _build_store(tmp_path / "u.db")
    report = _run(store)

    trades = report["trades"]
    assert trades, "应有成交记录"
    midnight = [t for t in trades if t["timestamp"][11:] == "00:00:00"]
    assert midnight == [], f"仍有 0 点成交：{[t['timestamp'] for t in midnight[:5]]}"
    for t in trades:
        assert t["timestamp"][11:] == "09:30:00", (
            f"日线成交时间戳应为 09:30:00，实际 {t['timestamp']}"
        )


def test_funnel_orders_stamped_at_close_1500(tmp_path):
    """日线口径的委托/信号时间必须定格在收盘 15:00，不能是 0 点。

    日线 bar 统一为收盘 15:00:00 口径（domain.daily_close_timestamp）后，
    on_daily 信号提交与订单 created_at 继承 15:00；成交仍是次开 09:30（见
    test_funnel_daily_fills_have_open_timestamp_not_midnight）。
    """
    store = _build_store(tmp_path / "u.db")
    report = _run(store)

    orders = report["orders"]
    assert orders, "应有委托记录"
    midnight = [o for o in orders if str(o.get("created_at", ""))[11:16] == "00:00"]
    assert midnight == [], f"仍有 0 点委托：{[o.get('created_at') for o in midnight[:5]]}"


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


def test_funnel_no_same_day_reentry_after_close(tmp_path):
    """清仓当日不得再建仓：轮动语义是「清仓→次日收盘重筛」，同日追单会让持仓失去管理。

    回归：清仓后同日剩余 5 分钟 bar（或收盘 on_daily）重新下首笔单，次日 current_symbol
    被清空 → 新仓无人回放/止盈，整段回测卡死在单只标的上。
    """
    store = _build_store(tmp_path / "u.db")
    dates = [(START + timedelta(days=index)).date().isoformat() for index in range(260)]
    codes = ["sz.300101", "sz.300102", "sz.300103"]
    quan = _FakeQuan({code: dates for code in codes})
    report = _run(store, quan=quan)

    rounds = report["rounds"]
    assert len(rounds) >= 2, f"应出现至少两轮，实际 {len(rounds)}"
    for previous, following in zip(rounds, rounds[1:]):
        assert previous["end_date"] is not None
        assert following["start_date"] > previous["end_date"], (
            f"清仓当日又建仓：{previous['id']} 结束 {previous['end_date']}，"
            f"{following['id']} 开始在 {following['start_date']}"
        )
    # 若确实发生「清仓当日」场景，引擎应记下延后建仓事件（次日重筛）
    assert all(item["type"] in {"ROUND_START", "ROUND_CLOSED", "FILL", "SIGNAL", "SCREEN", "SELECT", "WAIT", "ENTRY_DEFERRED", "ENTRY_ABANDON", "INVESTED_2_4M", "FLOAT_LOSS_OVER_6_23PCT"} for item in report["events"])


def test_funnel_gate_closed_waits_flat(tmp_path):
    store = _build_store(tmp_path / "u.db", gate_up=True)
    report = _run(store)

    assert report["metrics"]["total_rounds"] == 0
    trail = report["data_info"]["rotation_trail"]
    assert trail and all(item["status"] == "WAITING" and item["symbol"] is None for item in trail)
    assert any(item["gate_open"] is False for item in trail)
    # 逐轮决策日志（大盘门关闭/空仓等待）挂在对应轮次的轨迹项上，不再平铺进全局 warnings。
    assert any(
        any("L0" in w or "空仓等待" in w for w in item.get("screen_warnings", []))
        for item in trail
    )
    assert not any("L0" in w or "空仓等待" in w for w in report["warnings"])


def test_funnel_etf_beta_sleeve_holds_when_stock_gate_closed(tmp_path, monkeypatch):
    """L0 全程关门时，ETF beta 腿承接 beta；个股空仓但账户不再 0 收益。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    store = _build_store(tmp_path / "u.db", gate_up=True)
    etf_bars = _bars("sz.159915", [3.0 + index * 0.01 for index in range(N_BARS)])
    save_etf_bars(cache_path(tmp_path / "data", "sz.159915"), etf_bars)

    report = _run(
        store,
        config=StrategyConfig(
            etf_beta_enabled=True, etf_position_pct=0.20,
            etf_entry_ma_days=5, etf_exit_ma_days=5,
        ),
        initial_cash=1_000_000.0,
    )

    assert report["metrics"]["total_rounds"] == 0          # 个股一轮都没开
    beta = report["data_info"]["beta"]
    assert beta["enabled"] and beta["symbol"] == "sz.159915"
    assert beta["trade_count"] >= 1
    assert beta["position_qty"] > 0                        # 趋势未破，继续持有
    assert report["metrics"]["final_equity"] > 1_000_000   # ETF 上涨计入净值
    assert any(event["type"] == "ETF_FILL" for event in report["events"])
    # ETF 日线从回测起点之前就有 → 不算部分覆盖，也不该有覆盖告警
    assert beta["partial_coverage"] is False
    assert not any("部分覆盖" in warning for warning in report["warnings"])


def test_funnel_etf_partial_coverage_warns(tmp_path, monkeypatch):
    """ETF 日线首根晚于回测起点时：报告必须提示“部分覆盖”，且 data_info.beta 标注区间。

    背景：腾讯日线接口历史上只能取最近 640 根，导致 2022 年起的回测里 beta 腿在
    2024-03 之前静默不建仓；现在补采分页后可能仍受数据源限制，必须显式提示。
    """
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    store = _build_store(tmp_path / "u.db", gate_up=True)
    # 只造 2025-08-25 起的 ETF 日线（回测时间线起点是 2025-08-20）
    late_start = datetime(2025, 8, 25)
    etf_bars = [
        Bar(
            timestamp=late_start + timedelta(days=index), symbol="sz.159915",
            open=3.0 + index * 0.01, high=3.0 + index * 0.01, low=3.0 + index * 0.01,
            close=3.0 + index * 0.01, volume=1e7, amount=3e7, timeframe="daily",
        )
        for index in range(30)
    ]
    save_etf_bars(cache_path(tmp_path / "data", "sz.159915"), etf_bars)

    report = _run(
        store,
        config=StrategyConfig(
            etf_beta_enabled=True, etf_position_pct=0.20,
            etf_entry_ma_days=5, etf_exit_ma_days=5,
        ),
        initial_cash=1_000_000.0,
    )

    beta = report["data_info"]["beta"]
    assert beta["partial_coverage"] is True
    assert beta["coverage_start"] == "2025-08-25"
    assert any(
        "ETF beta 腿数据部分覆盖" in warning and "2025-08-25" in warning
        for warning in report["warnings"]
    ), report["warnings"]


def test_funnel_etf_cache_symbol_is_normalized(tmp_path, monkeypatch):
    """缓存里 symbol 是腾讯风格（sz159915）时，撮合仍按标准代码 sz.159915 匹配持仓。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    store = _build_store(tmp_path / "u.db", gate_up=True)
    tencent_style = []
    for index in range(N_BARS):
        close = 3.0 + index * 0.01
        tencent_style.append(Bar(
            timestamp=START + timedelta(days=index), symbol="sz159915",
            open=close, high=close, low=close, close=close,
            volume=1e7, amount=3e7, timeframe="daily",
        ))
    save_etf_bars(cache_path(tmp_path / "data", "sz.159915"), tencent_style)

    report = _run(
        store,
        config=StrategyConfig(
            etf_beta_enabled=True, etf_position_pct=0.20,
            etf_entry_ma_days=5, etf_exit_ma_days=5,
        ),
        initial_cash=1_000_000.0,
    )

    beta = report["data_info"]["beta"]
    assert beta["trade_count"] >= 1, "symbol 未归一化时 beta 永远不成交"
    assert any(event["type"] == "ETF_FILL" for event in report["events"])
    fills = [event for event in report["events"] if event["type"] == "ETF_FILL"]
    assert fills and all(event.get("symbol") in (None, "sz.159915") for event in fills)


def test_funnel_etf_beta_can_be_disabled(tmp_path, monkeypatch):
    """显式关闭 beta 腿：报告标注 disabled、零成交（默认值是开启）。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    store = _build_store(tmp_path / "u.db", gate_up=True)
    report = _run(store, config=StrategyConfig(etf_beta_enabled=False))
    beta = report["data_info"]["beta"]
    assert beta["enabled"] is False
    assert beta["trade_count"] == 0


def test_strategy_config_enables_etf_beta_by_default():
    """StrategyConfig 默认开启指数 beta 腿（回测页 / 新建模拟账户共用此默认）。"""
    assert StrategyConfig().etf_beta_enabled is True


def test_funnel_etf_sells_once_stock_selected(tmp_path, monkeypatch):
    """互斥规则：一旦选出个股，ETF 即使指数趋势未破也要清仓。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    store = _build_store(tmp_path / "u.db", gate_up=True)
    engine = FunnelRotationBacktest(
        store=store, market=_DummyMarket(),
        screener_config=ScreenerConfig(boards=[BOARD_GEM], enable_fundamental_veto=False),
        initial_cash=1_000_000.0,
        config=StrategyConfig(etf_beta_enabled=False),
        start_date="2025-08-20", end_date="2025-08-25", name="etf互斥测试",
    )
    # 手工开启 beta 腿并造一笔 ETF 持仓（等价于前一日已买入）。
    engine.etf_enabled = True
    engine.etf_symbol = "sz.159915"
    engine.etf_bars_by_date = {
        "2025-08-22": Bar(
            timestamp=datetime(2025, 8, 22, 15), symbol="sz.159915",
            open=3.0, high=3.0, low=3.0, close=3.0, volume=1e7, amount=3e7,
            timeframe="daily",
        )
    }
    position = engine.account.position("sz.159915")
    position.beta_qty = 10_000
    position.beta_cost = 30_000.0
    engine.account.mark("sz.159915", 3.0)

    engine._current_symbol = "sz.300101"          # 当日选出了个股
    engine._manage_beta("2025-08-22")

    sells = [
        order for order in engine.account.orders
        if order.purpose == "BETA" and order.side == "SELL"
    ]
    assert sells, "选出个股后必须提交 ETF 清仓委托"
    assert sells[0].quantity == 10_000


def test_funnel_etf_does_not_enter_when_stock_target_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    store = _build_store(tmp_path / "u.db", gate_up=True)
    engine = FunnelRotationBacktest(
        store=store, market=_DummyMarket(),
        screener_config=ScreenerConfig(boards=[BOARD_GEM], enable_fundamental_veto=False),
        initial_cash=1_000_000.0,
        config=StrategyConfig(etf_beta_enabled=False),
        start_date="2025-08-20", end_date="2025-08-25", name="etf互斥测试2",
    )
    engine.etf_enabled = True
    engine.etf_symbol = "sz.159915"
    engine.etf_bars_by_date = {
        "2025-08-22": Bar(
            timestamp=datetime(2025, 8, 22, 15), symbol="sz.159915",
            open=3.0, high=3.0, low=3.0, close=3.0, volume=1e7, amount=3e7,
            timeframe="daily",
        )
    }
    engine._current_symbol = "sz.300101"
    engine._manage_beta("2025-08-22")
    assert not [o for o in engine.account.orders if o.purpose == "BETA"]


def test_funnel_round_warnings_carry_select_date(tmp_path):
    """逐轮决策日志必须带选股日上下文：挂轨迹项 + 对应 SCREEN 事件，全局不再平铺。"""
    store = _build_store(tmp_path / "u.db", gate_up=True)
    report = _run(store)

    trail = report["data_info"]["rotation_trail"]
    assert trail and all("screen_warnings" in item for item in trail)
    screens = [e for e in report["events"] if e["type"] == "SCREEN"]
    assert screens and all("screen_warnings" in e for e in screens)
    # 全局 warnings 只剩全程不变的口径声明（含 enable_fundamental_veto），无逐轮动态行。
    assert any("enable_fundamental_veto" in w for w in report["warnings"])
    assert not any("旁路" in w or "大盘门关闭" in w for w in report["warnings"])


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


# ---------------------------------------------------------------------------
# ETF beta 腿：入场「不追高」过滤 + 清仓冷却 + 委托被拒日志口径
# ---------------------------------------------------------------------------


def _beta_engine(tmp_path, monkeypatch, *, config: StrategyConfig,
                 index_closes: list[float], etf_days=("2025-08-22",)) -> FunnelRotationBacktest:
    """构造一个手工 beta 引擎：只关心收盘决策，不跑完整 run。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    store = _build_store(tmp_path / "u.db", gate_up=True)
    engine = FunnelRotationBacktest(
        store=store, market=_DummyMarket(),
        screener_config=ScreenerConfig(boards=[BOARD_GEM], enable_fundamental_veto=False),
        initial_cash=1_000_000.0,
        config=StrategyConfig(etf_beta_enabled=False),
        start_date="2025-08-20", end_date="2025-08-25", name="beta保护测试",
    )
    engine.etf_enabled = True
    engine.etf_symbol = "sz.159915"
    engine.config = config
    engine.index_history = {"创业板": _bars("sz.399006", index_closes)}
    engine.etf_bars_by_date = {
        day: Bar(
            timestamp=datetime.fromisoformat(f"{day}T15:00:00"), symbol="sz.159915",
            open=3.0, high=3.0, low=3.0, close=3.0, volume=1e7, amount=3e7,
            timeframe="daily",
        )
        for day in etf_days
    }
    return engine


def test_etf_entry_deviation_guard_blocks_chasing(tmp_path, monkeypatch):
    """指数偏离入场均线 > 阈值时不建仓；阈值置 0 时恢复旧行为（站上均线就买）。"""
    closes = [100.0] * 9 + [130.0]   # 均线 103 → 偏离 +26%
    guarded = _beta_engine(
        tmp_path / "a", monkeypatch,
        config=StrategyConfig(etf_entry_ma_days=10, etf_exit_ma_days=5, etf_entry_max_deviation=0.08),
        index_closes=closes,
    )
    guarded._current_symbol = None
    guarded._manage_beta("2025-08-22")
    assert not [o for o in guarded.account.orders if o.purpose == "BETA"], "追高入场应被拦下"

    legacy = _beta_engine(
        tmp_path / "b", monkeypatch,
        config=StrategyConfig(etf_entry_ma_days=10, etf_exit_ma_days=5, etf_entry_max_deviation=0.0),
        index_closes=closes,
    )
    legacy._current_symbol = None
    legacy._manage_beta("2025-08-22")
    buys = [o for o in legacy.account.orders if o.purpose == "BETA" and o.side == "BUY"]
    assert buys, "关闭保护后仍应在站上均线时建仓"


def test_etf_reentry_cooldown_blocks_then_releases(tmp_path, monkeypatch):
    """清仓后冷却 N 个交易日内不建仓，满 N 日后放行。"""
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
    engine = _beta_engine(
        tmp_path, monkeypatch,
        config=StrategyConfig(etf_entry_ma_days=5, etf_exit_ma_days=3, etf_reentry_cooldown_days=3),
        index_closes=closes,
        etf_days=("2025-08-22", "2025-08-23"),
    )
    engine._current_symbol = None
    engine._beta_fills = [{"date": "2025-08-20", "side": "SELL"}]
    # 冷却第 2 个交易日：仍处冷却期
    assert engine._beta_cooldown_ready("2025-08-22") is False
    engine._manage_beta("2025-08-22")
    assert not [o for o in engine.account.orders if o.purpose == "BETA"]
    # 冷却第 3 个交易日：放行
    assert engine._beta_cooldown_ready("2025-08-23") is True
    engine._manage_beta("2025-08-23")
    assert [o for o in engine.account.orders if o.purpose == "BETA" and o.side == "BUY"]


def test_transient_t1_reject_not_in_global_warnings(tmp_path, monkeypatch):
    """T+1 可卖数量不足属于瞬时执行限制：只记 SIGNAL，不进「数据与近似口径提醒」。"""
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]
    engine = _beta_engine(
        tmp_path, monkeypatch,
        config=StrategyConfig(etf_entry_ma_days=3, etf_exit_ma_days=3),
        index_closes=closes,
    )
    # 无 ETF 持仓时卖出 → broker 以「T+1可卖数量不足」拒绝
    engine._submit_beta_order("2025-08-22", "SELL", 1000, "测试拒单")
    rejected = [e for e in engine.events if e["type"] == "SIGNAL" and e["status"] == "REJECTED"]
    assert rejected, "拒单仍应留痕在 SIGNAL 事件里"
    assert not [w for w in engine.warnings if "ETF委托被拒" in w or "委托被拒" in w]

    # 非瞬时原因（如申量不足最小申报单位）仍进 warnings，避免掩盖真实规则问题
    engine._submit_beta_order("2025-08-22", "BUY", 50, "测试不合规申报")
    assert [w for w in engine.warnings if "ETF委托被拒" in w]
