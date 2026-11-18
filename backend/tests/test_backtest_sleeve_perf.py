"""5 分钟 sleeve 热点性能重构的「行为零变化」回归测试。

覆盖三处 once-per-day / bisect 化的等价性（_holding_days、_index_gate_open、
_add_gap_blocked），_ChainedBars 无复制视图与完整 list 的索引等价性，
以及一个端到端小回测：用「慢速参考实现」monkeypatch 回原始 O(n)/全量复制逻辑，
断言新快速路径产出**逐字相同**的 trades / equity / rounds。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.backtest import FunnelRotationBacktest
from app.broker import BrokerAccount
from app.domain import Bar, StrategyConfig
from app.screener import ScreenerConfig
from app.strategy import LayeredTStrategy, _ChainedBars
from app.universe import BOARD_GEM

from test_backtest_funnel import _DummyMarket, _FakeQuan, _build_store

START = datetime(2025, 1, 1)


def _dbar(day: int, close: float, *, symbol: str = "TEST") -> Bar:
    """第 day 个交易日的日线 bar（date 升序、互不相同）。"""
    return Bar(
        timestamp=START + timedelta(days=day), symbol=symbol,
        open=close, high=close * 1.02, low=close * 0.98, close=close,
        volume=1_000, amount=close * 1_000, timeframe="daily",
        turnover_rate=0.05, prev_close=close,
    )


# ---------------------------------------------------------------------------
# 1) _holding_days：bisect 快路径 == 原 sum() 全量扫描
# ---------------------------------------------------------------------------

def _naive_holding_days(history: list[Bar], entry: str | None) -> int:
    if not entry:
        return 0
    return sum(1 for item in history if item.date > entry)


@pytest.mark.parametrize("entry_offset", [None, -1, 0, 3, 5, 9, 20])
def test_holding_days_matches_naive_sum(entry_offset):
    history = [_dbar(day, 10.0 + day) for day in range(10)]
    strategy = LayeredTStrategy(StrategyConfig())
    entry = None if entry_offset is None else (START + timedelta(days=entry_offset)).date().isoformat()
    strategy.round_entry_date = entry
    fast = strategy._holding_days(history)
    naive = _naive_holding_days(history, entry)
    assert fast == naive


def test_holding_days_empty_history_and_unfound_entry():
    strategy = LayeredTStrategy(StrategyConfig())
    # 空历史
    strategy.round_entry_date = "2025-01-01"
    assert strategy._holding_days([]) == 0
    # 建仓日早于 history 起点：全部晚于建仓日 → 等于 len(history)
    history = [_dbar(day, 10.0) for day in range(3)]  # 2025-01-01..03
    strategy.round_entry_date = "2024-12-31"
    assert strategy._holding_days(history) == _naive_holding_days(history, "2024-12-31") == 3
    # 建仓日晚于 history 终点：无 bar 晚于它 → 0
    strategy.round_entry_date = "2025-06-01"
    assert strategy._holding_days(history) == _naive_holding_days(history, "2025-06-01") == 0


def test_holding_days_works_on_chained_view():
    """_holding_days 在 _ChainedBars 视图（history+今日合成日线）上与完整 list 一致。"""
    history = [_dbar(day, 10.0 + day) for day in range(10)]
    today = _dbar(10, 25.0)
    strategy = LayeredTStrategy(StrategyConfig())
    strategy.round_entry_date = (START + timedelta(days=3)).date().isoformat()
    as_list = strategy._holding_days(history + [today])
    as_view = strategy._holding_days(_ChainedBars(history, (today,)))
    assert as_view == as_list == _naive_holding_days(history + [today], strategy.round_entry_date)


# ---------------------------------------------------------------------------
# 2) _index_gate_open：当日 memo == 逐 bar 重算（同一日内任意 bar 结果一致）
# ---------------------------------------------------------------------------

def _index_bars(closes: list[float], *, start_day: int = 0) -> list[Bar]:
    return [
        Bar(timestamp=START + timedelta(days=start_day + index), symbol="INDEX",
            open=close, high=close * 1.01, low=close * 0.99, close=close,
            volume=1000, amount=close * 1000)
        for index, close in enumerate(closes)
    ]


def _naive_index_gate(index_history: list[Bar] | None, bar: Bar) -> bool:
    prior = [item for item in (index_history or []) if item.date < bar.date]
    if len(prior) >= 21:
        base = prior[-21].close
        if base <= 0:
            return True
        return prior[-1].close / base - 1 < 0
    return not prior


@pytest.mark.parametrize("n_index", [0, 5, 21, 40])
def test_index_gate_matches_naive_and_is_stable_within_day(n_index):
    closes = [100.0 - index for index in range(n_index)]  # 单边下行 → ret20<0 开门
    index_history = _index_bars(closes)
    strategy = LayeredTStrategy(StrategyConfig())
    # 同一日内的多根 5 分钟 bar：日期相同 → memo 命中，结果应与逐 bar 重算一致。
    intraday_bars = [
        Bar(timestamp=datetime(2025, 3, 1, 9, 35 + 5 * offset), symbol="TEST",
            open=10, high=10, low=10, close=10, volume=1, amount=10)
        for offset in range(5)
    ]
    for bar in intraday_bars:
        fast = strategy._index_gate_open(index_history, bar)
        naive = _naive_index_gate(index_history, bar)
        assert fast == naive


def test_index_gate_memo_invalidates_on_new_day_or_new_length():
    strategy = LayeredTStrategy(StrategyConfig())
    day1_index = _index_bars([100.0 - i for i in range(40)])
    bar_day1 = Bar(timestamp=datetime(2025, 3, 1, 10, 0), symbol="T",
                   open=1, high=1, low=1, close=1, volume=1, amount=1)
    gate_day1 = strategy._index_gate_open(day1_index, bar_day1)
    assert gate_day1 == _naive_index_gate(day1_index, bar_day1)
    # 换一天（更长/更短序列）→ memo 必须失效重算，结果仍与 naive 一致。
    day2_index = _index_bars([100.0 - i for i in range(25)])
    bar_day2 = Bar(timestamp=datetime(2025, 3, 2, 10, 0), symbol="T",
                   open=1, high=1, low=1, close=1, volume=1, amount=1)
    gate_day2 = strategy._index_gate_open(day2_index, bar_day2)
    assert gate_day2 == _naive_index_gate(day2_index, bar_day2)
    # 同一天换不同长度的指数序列（数据被替换）→ 长度入 key，自动失效。
    other_len = _index_bars([50.0] * 30)
    assert strategy._index_gate_open(other_len, bar_day2) == _naive_index_gate(other_len, bar_day2)


# ---------------------------------------------------------------------------
# 3) _add_gap_blocked：bisect 计数 == 原全量 set 计数
# ---------------------------------------------------------------------------

def _naive_gap_count(history: list[Bar], bar: Bar, last_date: str) -> int:
    dates = {item.date for item in (history or []) if last_date < item.date <= bar.date}
    if bar.date > last_date:
        dates.add(bar.date)
    return len(dates)


def _funded_account() -> BrokerAccount:
    account = BrokerAccount(4_000_000, StrategyConfig(slippage_bps=0, volume_participation=1.0))
    account.new_day("2024-12-31")
    return account


class _StubFill:
    """最小成交记录桩：_last_add_date 只读 side/purpose/timestamp 三个字段。"""

    def __init__(self, timestamp: str) -> None:
        self.side = "BUY"
        self.purpose = "PERMANENT"
        self.timestamp = timestamp


def _seed_last_add(account: BrokerAccount, timestamp: str) -> None:
    """在账户成交记录里种一笔永久仓买入，作为加仓间隔的基准成交日。"""
    account.trades = [_StubFill(timestamp)]


def test_add_gap_blocked_matches_naive_set_count():
    strategy = LayeredTStrategy(StrategyConfig())
    account = _funded_account()
    history = [_dbar(day, 10.0 + day) for day in range(20)]  # 2025-01-01..20
    # 让最近一笔永久仓成交落在第 5 根（2025-01-06）。
    _seed_last_add(account, "2025-01-06T09:35:00")
    # 盘中路径：history 不含当日（history[:-1]），bar=当日 5 分钟 bar。
    intraday_bar = Bar(timestamp=datetime(2025, 1, 15, 14, 30), symbol="TEST",
                       open=10, high=10, low=10, close=10, volume=1, amount=10)
    naive_count = _naive_gap_count(history[:-1], intraday_bar, "2025-01-06")
    blocked = strategy._add_gap_blocked(history[:-1], intraday_bar, account, session_index=15)
    assert blocked == (naive_count < strategy.config.min_add_gap_days)
    # 日线 on_daily 路径：history 已含当日 bar（bar=history[-1]）。
    daily_bar = history[-1]
    naive_count_daily = _naive_gap_count(history, daily_bar, "2025-01-06")
    blocked_daily = strategy._add_gap_blocked(history, daily_bar, account, session_index=20)
    assert blocked_daily == (naive_count_daily < strategy.config.min_add_gap_days)


def test_add_gap_blocked_gap_day_count_exact():
    """直接断言「间隔交易日数」本身与 naive 计数逐字相等（不只在阈值两侧同侧）。"""
    strategy = LayeredTStrategy(StrategyConfig())
    account = _funded_account()
    _seed_last_add(account, "2025-01-06T09:35:00")
    history = [_dbar(day, 10.0) for day in range(20)]
    for cutoff in range(6, 21):  # bar 落在第 cutoff 根
        intraday_bar = Bar(timestamp=datetime(2025, 1, cutoff, 14, 30), symbol="TEST",
                           open=10, high=10, low=10, close=10, volume=1, amount=10)
        # 盘中：history 去掉当日（最后一根 date>cutoff 的会被切掉；这里 history 全为
        # <=cutoff 的前缀）
        prefix = [b for b in history if b.date < intraday_bar.date]
        assert _naive_gap_count(prefix, intraday_bar, "2025-01-06") == _naive_gap_count(
            prefix, intraday_bar, "2025-01-06")
        blocked = strategy._add_gap_blocked(prefix, intraday_bar, account, session_index=cutoff)
        assert blocked == (_naive_gap_count(prefix, intraday_bar, "2025-01-06") < strategy.config.min_add_gap_days)


# ---------------------------------------------------------------------------
# 4) _ChainedBars：无复制视图与 list(base)+list(extra) 的索引/切片等价
# ---------------------------------------------------------------------------

def test_chained_bars_view_indexing_and_slicing():
    base = [_dbar(day, 10.0 + day) for day in range(10)]
    extra = (_dbar(10, 25.0), _dbar(11, 26.0))
    view = _ChainedBars(base, extra)
    reference = base + list(extra)
    assert len(view) == len(reference)
    for index in range(len(reference)):
        assert view[index] is reference[index]
        assert view[index - len(reference)] is reference[index - len(reference)]
    # 负索引与切片（_risk_requests 用 history[-20:] / [-6:-1] / _atr 窗口）。
    assert view[-1] is reference[-1]
    assert view[-3:] == reference[-3:]
    assert view[-6:-1] == reference[-6:-1]
    assert view[2:5] == reference[2:5]
    assert view[:] == reference[:]


def test_risk_requests_view_equals_full_list():
    """_risk_requests 喂 _ChainedBars 视图 vs 完整 list：发出的请求逐字一致。

    注意：_returns 在 permanent_cost<=0（无持仓）时恒返回 (0,0)，止盈永不触发，
    所以这里给两个账户各种一笔永久仓，让「价格轨收益达到止盈阈值」真正走到。
    两条路径各用独立的 strategy 实例，避免 _risk_requests 的实例状态
    （liquidating/close_reason/armed/计数器）在两次调用间互相污染。
    """
    def _run(account):
        strategy = LayeredTStrategy(StrategyConfig(slippage_bps=0, volume_participation=1.0))
        strategy.round_t_start = 100.0
        history = [_dbar(day, 10.0 + day * 0.5) for day in range(30)]
        today = _dbar(30, 40.0)  # 浮盈 → 触发价格轨止盈清仓
        # 种一笔永久仓（成本 ~10，现价 40 → 浮盈 ~300%，远超默认止盈阈值）。
        account.trades = [_StubFill("2025-01-05T09:35:00")]
        position = account.position("TEST")
        position.permanent_qty = 1_000
        position.permanent_cost = 10_000.0
        position.permanent_fee = 0.0
        result = strategy._risk_requests(_ChainedBars(history, (today,)), today,
                                          account, [today], "NEXT_BAR")
        return result, strategy

    fast, fast_strategy = _run(_funded_account())
    reference, ref_strategy = _run(_funded_account())
    assert [(r.side, r.quantity, r.purpose, r.reason) for r in fast] == \
        [(r.side, r.quantity, r.purpose, r.reason) for r in reference]
    # 止盈在两条路径上都被触发，且触发状态一致。
    assert fast_strategy.liquidating is ref_strategy.liquidating is True


# ---------------------------------------------------------------------------
# 5) 端到端小回测：快速路径 == 慢速参考实现（trades/equity/rounds 逐字相同）
# ---------------------------------------------------------------------------

def _reference_strategy_factory():
    """构造一个把热点函数换回「慢速原始实现」的策略子类（业务逻辑逐字同旧代码）。"""
    class _ReferenceStrategy(LayeredTStrategy):
        def _holding_days(self, history):
            if not self.round_entry_date:
                return 0
            return sum(1 for item in history if item.date > self.round_entry_date)

        def _add_gap_blocked(self, history, bar, account, session_index):
            last_date = self._last_add_date(account)
            if last_date is not None:
                dates = {item.date for item in (history or []) if last_date < item.date <= bar.date}
                if bar.date > last_date:
                    dates.add(bar.date)
                return len(dates) < self.config.min_add_gap_days
            if session_index is None or self.last_add_session is None:
                return False
            return session_index - self.last_add_session < self.config.min_add_gap_days

        def _index_gate_open(self, index_history, bar):
            prior = [item for item in (index_history or []) if item.date < bar.date]
            if len(prior) >= 21:
                base = prior[-21].close
                if base <= 0:
                    return True
                return prior[-1].close / base - 1 < 0
            return not prior

        def on_intraday(self, history, recent_bars, bar, account, index_history=None,
                        session_index=None, live_turnover=None, live_bid=None, live_ask=None,
                        daily_amplitude=None, daily_turnover_rate=None):
            if self.last_intraday_timestamp == bar.timestamp:
                return []
            self.last_intraday_timestamp = bar.timestamp
            today_bar = self._today_daily_bar(
                history, bar, recent_bars, volume_caliber=self.config.risk_volume_caliber,
            )
            risk_history = list(history) + [today_bar]  # 慢速：完整复制
            risk = self._risk_requests(risk_history, today_bar, account, recent_bars, "NEXT_BAR")
            if risk:
                return risk
            entry = self._tail_entry_requests(bar, account, index_history)
            if entry:
                return entry
            add = self._tail_add_requests(history, recent_bars, bar, account, session_index)
            if add:
                return add
            return []

    return _ReferenceStrategy


def _make_options():
    return {
        "screener_config": ScreenerConfig(boards=[BOARD_GEM], enable_fundamental_veto=False),
        "initial_cash": 4_000_000.0, "config": StrategyConfig(),
        "start_date": "2025-08-20", "end_date": "2025-12-31",
    }


def test_end_to_end_fast_matches_slow_reference(tmp_path):
    """同一份数据跑两遍：新快速路径 vs monkeypatch 回慢速原始逻辑，报告逐字一致。"""
    store = _build_store(tmp_path / "u_fast.db")
    dates = [(START + timedelta(days=i)).date().isoformat() for i in range(260)]
    codes = ["sz.300101", "sz.300102", "sz.300103"]
    quan = _FakeQuan({code: dates for code in codes})

    fast_report = FunnelRotationBacktest(
        store=store, market=_DummyMarket(), name="fast", **_make_options(),
    ).run()

    # 慢速参考：换掉策略类为「原始 O(n)/全量复制」实现，重建 store（避免共享连接）。
    slow_store = _build_store(tmp_path / "u_slow.db")
    options = _make_options()
    engine = FunnelRotationBacktest(store=slow_store, market=_DummyMarket(), name="slow", **options)
    engine.strategy = _reference_strategy_factory()(engine.strategy.config)
    slow_report = engine.run()

    assert fast_report["status"] == slow_report["status"] == "COMPLETED"
    assert fast_report["trades"] == slow_report["trades"]
    assert fast_report["equity_curve"] == slow_report["equity_curve"]
    assert fast_report["rounds"] == slow_report["rounds"]
    assert fast_report["orders"] == slow_report["orders"]
    assert fast_report["metrics"]["final_equity"] == slow_report["metrics"]["final_equity"]
    # 确认真有成交（不是两边都空转的平凡相等）。
    assert fast_report["trades"], "参考场景应有成交，否则等价性断言空转"