"""5 分钟 bar 的 prev_close = 前一交易日最后一根 5 分钟收盘（昨收）的哨兵测试。

根因：``load_minute5`` 曾用 ``previous`` = 上一根 5 分钟 bar 的收盘（跨日不重置），
导致每根 5 分钟 bar 的 ``prev_close`` 不是「昨收」，做T深跌旁路（``t_deep_drop_pct``）
退化成「单根 5 分钟跌幅 ≥ 阈值」，几乎永不成立。

修复后：当日**每一根** 5 分钟 bar 的 ``prev_close`` = 前一交易日最后一根 5 分钟 bar
的复权收盘价，与模拟盘 ``market.py`` 给实时 5 分钟 bar 挂 ``quote["prev_close"]`` 的
口径一致（架构原则 1：单一真相源）。连带收益：``broker._tradable`` 的涨跌停校验
基准从「5 分钟前价」自动变回「昨收」。

本文件分三组：
- A 组（数据层）：``load_minute5`` 的 prev_close 语义；
- B 组（策略层）：``strategy._t_eligible`` 的深跌旁路在真实昨收口径下触发；
- C 组（broker 连带）：``broker.process_bar`` 的涨跌停校验用昨收而非 5 分钟前价。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

pl = pytest.importorskip("polars")

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, Position, StrategyConfig, price
from app.quan_data import QuanStore
from app.strategy import LayeredTStrategy


# ---------------------------------------------------------------------------
# 造数工具（复用 test_quan_data.py 的 fixture 写法，不重复造）
# ---------------------------------------------------------------------------


DAILY_SCHEMA = {
    "code": pl.String, "date": pl.String, "open": pl.Float64, "high": pl.Float64,
    "low": pl.Float64, "close": pl.Float64, "volume": pl.Int64, "amount": pl.Float64,
}
ADJ_SCHEMA = {"code": pl.String, "date": pl.String, "adj_factor": pl.Float64}
MINUTE_SCHEMA = {
    "code": pl.String, "date": pl.String, "open": pl.Float64, "high": pl.Float64,
    "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64,
}


def write_parquet(path: Path, rows: list[dict], schema: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema=schema).write_parquet(path)


def _drow(day: str, close: float, code: str = "300001") -> dict:
    return {
        "code": code, "date": day, "open": close, "high": close, "low": close,
        "close": close, "volume": 1000, "amount": close * 1000.0,
    }


def _arow(day: str, factor: float, code: str = "300001") -> dict:
    return {"code": code, "date": day, "adj_factor": factor}


def _m5row(day: str, clock: str, close: float, code: str = "300001") -> dict:
    return {
        "code": code, "date": f"{day} {clock}", "open": close, "high": close,
        "low": close, "close": close, "volume": 1000.0, "amount": close * 1000.0,
    }


def _store(
    tmp_path: Path, minute_rows: list[dict], adj_rows: list[dict], daily_rows: list[dict],
) -> QuanStore:
    root = tmp_path / "quan"
    root.mkdir()
    write_parquet(root / "daily.parquet", daily_rows, DAILY_SCHEMA)
    write_parquet(root / "adj_factor.parquet", adj_rows, ADJ_SCHEMA)
    if minute_rows:
        write_parquet(root / "minute5" / "300001.parquet", minute_rows, MINUTE_SCHEMA)
    return QuanStore(root)


# ---------------------------------------------------------------------------
# A 组：数据层 load_minute5 的 prev_close 语义
# ---------------------------------------------------------------------------


def test_minute5_prev_close_is_previous_session_last_close(tmp_path):
    """日 N 首根/次根 == 日 N-1 末根 close；首日无前驱降级为本根收盘价。"""
    minute_rows = [
        _m5row("2026-11-10", "09:35", 20.0),
        _m5row("2026-11-10", "09:40", 20.5),
        _m5row("2026-11-11", "09:35", 21.0),
        _m5row("2026-11-11", "09:40", 21.4),
    ]
    adj_rows = [_arow("2026-11-10", 2.0), _arow("2026-11-11", 2.0)]
    daily_rows = [_drow("2026-11-10", 20.0), _drow("2026-11-11", 21.0)]
    store = _store(tmp_path, minute_rows, adj_rows, daily_rows)

    bars = store.load_minute5("300001")
    assert len(bars) == 4
    # 核心：日 N 首根 == 日 N-1 末根 close（昨收，而非上一根 5 分钟 bar 收盘）。
    assert bars[2].prev_close == 20.5
    assert bars[3].prev_close == 20.5
    # 首日无前驱，降级为本根收盘价（close/prev_close-1==0，deep_drop 分支恒 False）。
    assert bars[0].prev_close == 20.0
    assert bars[1].prev_close == 20.5


def test_minute5_prev_close_not_last_bar(tmp_path):
    """旧语义（上一根 5 分钟 bar 的收盘）回归哨兵：同日第 2 根必须用昨收，非上一根 5m 收盘。

    布局：11-09 一根（末收盘 20.0）+ 11-10 两根（20.4 / 20.5）。
    bars = [11-09, 11-10首根, 11-10次根]。旧语义会把 11-10 次根的 prev_close 写成
    11-10 首根收盘 20.4；正确语义必须写 11-09 末收盘 20.0。
    """
    minute_rows = [
        _m5row("2026-11-09", "09:35", 20.0),
        _m5row("2026-11-10", "09:35", 20.4),
        _m5row("2026-11-10", "09:40", 20.5),
    ]
    adj_rows = [_arow("2026-11-09", 2.0), _arow("2026-11-10", 2.0)]
    daily_rows = [_drow("2026-11-09", 20.0), _drow("2026-11-10", 20.4)]
    store = _store(tmp_path, minute_rows, adj_rows, daily_rows)

    bars = store.load_minute5("300001")
    assert len(bars) == 3
    # 11-10 两根均用 11-09 末收盘 20.0。
    assert bars[1].prev_close == 20.0
    assert bars[2].prev_close == 20.0
    # 关键哨兵：11-10 次根的 prev_close ≠ 11-10 首根收盘（旧语义会等于 20.4）。
    assert bars[2].prev_close != bars[1].close


def test_minute5_first_day_without_predecessor_degrades_to_own_close(tmp_path):
    """窗口首日无前驱：每根降级为本根收盘价（close/prev_close-1==0）。"""
    minute_rows = [
        _m5row("2026-11-11", "09:35", 21.0),
        _m5row("2026-11-11", "09:40", 21.4),
    ]
    adj_rows = [_arow("2026-11-11", 2.0)]
    daily_rows = [_drow("2026-11-11", 21.0)]
    store = _store(tmp_path, minute_rows, adj_rows, daily_rows)

    bars = store.load_minute5("300001")
    assert len(bars) == 2
    assert bars[0].prev_close == bars[0].close
    assert bars[1].prev_close == bars[1].close
    assert bars[1].prev_close != bars[0].close  # 降级为自身 close，不是首根 close


def test_minute5_gap_after_suspension_uses_last_available_close(tmp_path):
    """停牌（11-10 无数据）后复牌，昨收取最近可用交易日（11-09）末收盘。"""
    minute_rows = [
        _m5row("2026-11-09", "09:35", 19.8),
        _m5row("2026-11-09", "09:40", 20.0),   # 11-09 末收盘 = 20.0
        # 11-10 停牌无数据
        _m5row("2026-11-11", "09:35", 21.0),
        _m5row("2026-11-11", "09:40", 21.4),
    ]
    adj_rows = [_arow("2026-11-09", 2.0), _arow("2026-11-11", 2.0)]
    daily_rows = [_drow("2026-11-09", 20.0), _drow("2026-11-11", 21.0)]
    store = _store(tmp_path, minute_rows, adj_rows, daily_rows)

    bars = store.load_minute5("300001")
    assert len(bars) == 4
    assert bars[2].prev_close == 20.0
    assert bars[3].prev_close == 20.0


def test_minute5_prev_close_same_adjustment_basis_on_ex_dividend_day(tmp_path):
    """除权日：昨收与当日 bar 必须同复权基准，不伪造除权跳变。

    负向对照：若改取日线 ``load_series`` 的 prev_close（因子基准被 ``end`` 截断），
    会跨复权基准混算，把 11-11 相对 11-10 的「真实 0 涨跌」错算成 ≈ -33% 假跌幅。
    """
    minute_rows = [
        _m5row("2026-11-09", "09:35", 20.0),
        _m5row("2026-11-09", "09:40", 20.0),
        _m5row("2026-11-10", "09:35", 20.0),
        _m5row("2026-11-10", "09:40", 20.0),
        _m5row("2026-11-11", "09:35", 20.0),
        _m5row("2026-11-11", "09:40", 20.0),
    ]
    adj_rows = [
        _arow("2026-11-09", 1.0),
        _arow("2026-11-10", 2.0),
        _arow("2026-11-11", 2.0),
        _arow("2026-11-12", 3.0),
    ]
    daily_rows = [
        _drow("2026-11-09", 20.0), _drow("2026-11-10", 20.0), _drow("2026-11-11", 20.0),
    ]
    store = _store(tmp_path, minute_rows, adj_rows, daily_rows)

    bars = store.load_minute5("300001")
    # load_minute5 的因子基准 = 因子序列末值 3.0（11-12，无 end 过滤）；11-11 复权价 = 20*2/3。
    expected = price(20 * 2 / 3)
    m11 = [b for b in bars if b.date == "2026-11-11"]
    assert len(m11) == 2
    for b in m11:
        assert b.prev_close == expected  # 与同日 bar 同复权基准
        assert b.close / b.prev_close - 1 == pytest.approx(0.0, abs=1e-9)  # 不伪造除权跳变

    # 负向对照：日线 load_series 的因子基准被 end=11-11 截断为 2.0，prev_close=20.0。
    daily = store.load_series("sz.300001", end="2026-11-11")
    assert daily[-1].prev_close == 20.0
    # 证明若改取日线前收会产生 −33% 假跌幅（13.33/20.0 - 1 ≈ -33.3%）。
    assert abs(expected / 20.0 - 1 + 0.10) > 0.2


def test_load_minute5_max_rows_truncation_keeps_prev_close(tmp_path):
    """max_rows 截断只发生在所有 bar 构建之后：被保留首根的 prev_close 仍是真实昨收。"""
    minute_rows = [
        _m5row("2026-11-09", "09:35", 19.5),
        _m5row("2026-11-10", "09:35", 20.0),   # 11-10 末收盘 = 20.0
        _m5row("2026-11-11", "09:35", 21.0),
        _m5row("2026-11-11", "09:40", 21.2),
        _m5row("2026-11-11", "09:45", 21.4),
    ]
    adj_rows = [_arow(d, 2.0) for d in ("2026-11-09", "2026-11-10", "2026-11-11")]
    daily_rows = [_drow("2026-11-09", 19.5), _drow("2026-11-10", 20.0), _drow("2026-11-11", 21.0)]
    store = _store(tmp_path, minute_rows, adj_rows, daily_rows)

    bars = store.load_minute5("300001", max_rows=3)
    assert len(bars) == 3
    assert bars[0].date == "2026-11-11"       # 截断后首根 = 11-11 09:35
    assert bars[0].prev_close == 20.0         # 仍为 11-10 末收盘，非降级


# ---------------------------------------------------------------------------
# B 组：策略层 _t_eligible 的深跌旁路（t_deep_drop_pct）
# ---------------------------------------------------------------------------

SYM = "sz.301205"


def _daily(ts: datetime, close: float) -> Bar:
    """构造一根日线（做T用的历史底仓日线）。"""
    return Bar(
        timestamp=ts, symbol=SYM, open=close, high=close * 1.03, low=close * 0.97,
        close=close, volume=500_000, amount=close * 500_000, timeframe="daily",
        turnover_rate=None, prev_close=close,
    )


def _m5(ts: datetime, *, prev_close: float | None, high: float, low: float, close: float,
        bid: float, ask: float) -> Bar:
    """构造一根 5 分钟 bar（历史分钟 bar 无换手，盘口用代理值）。"""
    return Bar(
        timestamp=ts, symbol=SYM, open=close, high=high, low=low, close=close,
        volume=500_000, amount=close * 500_000, timeframe="5m",
        turnover_rate=None, prev_close=prev_close, bid=bid, ask=ask,
    )


def _account_and_strategy(
    sequence_index: int = 1, t_deep_drop_pct: float | None = 0.10,
) -> tuple[BrokerAccount, LayeredTStrategy]:
    """底仓笔数不足（sequence_index < t_min_base_entries=4）的账户：只有深跌旁路能准入。"""
    cfg = StrategyConfig(t_deep_drop_pct=t_deep_drop_pct)
    acct = BrokerAccount(initial_cash=1_000_000.0, config=cfg, name="t")
    acct.sequence_index = sequence_index
    strat = LayeredTStrategy(cfg)
    return acct, strat


def _history() -> list[Bar]:
    """5 根涨跌交替的日线历史（避免单向下跌过滤命中 one_sided_down）。"""
    base = datetime(2026, 11, 5, 15, 0)
    hist: list[Bar] = []
    px = 140.0
    for i in range(5):
        ts = base + timedelta(days=i)
        close = px * (1.02 if i % 2 == 0 else 0.99)
        hist.append(_daily(ts, close))
        px = close
    return hist


def test_deep_drop_bypass_triggers_on_gap_vs_prev_session_close():
    """昨收 100 → 现价 88（-12%）触发深跌旁路，绕过底仓笔数门槛。"""
    acct, strat = _account_and_strategy(sequence_index=1, t_deep_drop_pct=0.10)
    hist = _history()
    today = datetime(2026, 11, 10, 9, 35)
    m5 = _m5(today, prev_close=100.0, high=99.0, low=88.0, close=88.0,
             bid=87.99, ask=88.01)
    assert strat._t_eligible(history=hist, bar=m5, account=acct,
                             daily_amplitude=0.05, daily_turnover_rate=0.08) is True
    # 对照：同一 bar 但昨收 92 → 88/92-1=-4.3%，不触发深跌，被底仓门槛拦下。
    m5_2 = _m5(today, prev_close=92.0, high=99.0, low=88.0, close=88.0,
               bid=87.99, ask=88.01)
    assert strat._t_eligible(history=hist, bar=m5_2, account=acct,
                             daily_amplitude=0.05, daily_turnover_rate=0.08) is False


def test_single_5m_bar_drop_does_not_bypass():
    """单根 5 分钟 bar 跌幅不足 10%（-5% / -3.06%）不触发深跌旁路。"""
    acct, strat = _account_and_strategy(sequence_index=1, t_deep_drop_pct=0.10)
    hist = _history()
    today = datetime(2026, 11, 10, 9, 35)
    m5a = _m5(today, prev_close=100.0, high=96.0, low=95.0, close=95.0,
              bid=94.99, ask=95.01)
    assert strat._t_eligible(history=hist, bar=m5a, account=acct,
                             daily_amplitude=0.05, daily_turnover_rate=0.08) is False
    m5b = _m5(today, prev_close=98.0, high=96.0, low=95.0, close=95.0,
              bid=94.99, ask=95.01)
    assert strat._t_eligible(history=hist, bar=m5b, account=acct,
                             daily_amplitude=0.05, daily_turnover_rate=0.08) is False


def test_deep_drop_disabled_skips_branch():
    """t_deep_drop_pct=None（关闭）：即便 -12% 也不旁路，被底仓门槛拦下。"""
    acct, strat = _account_and_strategy(sequence_index=1, t_deep_drop_pct=None)
    hist = _history()
    today = datetime(2026, 11, 10, 9, 35)
    m5 = _m5(today, prev_close=100.0, high=99.0, low=88.0, close=88.0,
             bid=87.99, ask=88.01)
    assert strat._t_eligible(history=hist, bar=m5, account=acct,
                             daily_amplitude=0.05, daily_turnover_rate=0.08) is False


def test_deep_drop_branch_skipped_when_prev_close_none():
    """prev_close=None（数据首日无昨收）：不抛异常，直接走底仓门槛拦下。"""
    acct, strat = _account_and_strategy(sequence_index=1, t_deep_drop_pct=0.10)
    hist = _history()
    today = datetime(2026, 11, 10, 9, 35)
    m5 = _m5(today, prev_close=None, high=99.0, low=88.0, close=88.0,
             bid=87.99, ask=88.01)
    assert strat._t_eligible(history=hist, bar=m5, account=acct,
                             daily_amplitude=0.05, daily_turnover_rate=0.08) is False


# ---------------------------------------------------------------------------
# C 组：broker 连带影响（_tradable 涨跌停校验基准变昨收）
# ---------------------------------------------------------------------------


def _limit_bar(prev_close: float) -> Bar:
    """主板 sh.600000 的一根 5 分钟 bar（±10%），市价 79.0。"""
    return Bar(
        timestamp=datetime(2026, 11, 10, 9, 35), symbol="sh.600000",
        open=79.0, high=79.0, low=79.0, close=79.0, volume=1_000_000,
        amount=79_000_000.0, timeframe="5m", prev_close=prev_close,
        bid=78.99, ask=79.01,
    )


def _seeded_sell_account() -> BrokerAccount:
    """持有 sh.600000 1000 股（无当日买入锁定，T+1 可卖）的账户。"""
    acct = BrokerAccount(initial_cash=1_000_000.0, config=StrategyConfig(), name="b")
    acct.positions["sh.600000"] = Position(
        symbol="sh.600000", permanent_qty=1000, permanent_cost=79_000.0, last_price=79.0,
    )
    return acct


def test_broker_limit_check_uses_prev_session_close_for_5m_bars():
    """昨收 100 → 涨跌停下限 90：市价 SELL 79 被拦（message 指明涨跌停）。"""
    acct = _seeded_sell_account()
    order = acct.submit(
        OrderRequest(symbol="sh.600000", side="SELL", quantity=1000,
                     purpose="PERMANENT", execution_mode="NEXT_BAR"),
        datetime(2026, 11, 10, 9, 30),
    )
    acct.process_bar(_limit_bar(100.0), modes={"NEXT_BAR"})
    assert order.message == "涨跌停或超出价格限制，不可成交"
    assert order.status != "FILLED"


def test_broker_limit_check_control_old_semantics_fills():
    """对照：昨收 80（旧语义≈5 分钟前价 79）→ 下限 72，同一委托能成交。"""
    acct = _seeded_sell_account()
    order = acct.submit(
        OrderRequest(symbol="sh.600000", side="SELL", quantity=1000,
                     purpose="PERMANENT", execution_mode="NEXT_BAR"),
        datetime(2026, 11, 10, 9, 30),
    )
    acct.process_bar(_limit_bar(80.0), modes={"NEXT_BAR"})
    assert order.status == "FILLED"
