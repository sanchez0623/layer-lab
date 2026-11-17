"""选股池幸存者偏差修复（方案二-A）单测。

背景：QuanStore._all_codes 旧实现按 stock_basic.parquet 的「今天的 delisted 布尔标」
整体剔除退市票。历史回测 as_of 早于退市日时，该票当时仍正常交易，凭未来退市结果
回溯剔除 → 幸存者偏差（高估轮动收益、低估尾部风险）。

覆盖：
(a) _all_codes / list_codes 不再剔除退市票；
(b) 进程级缓存保持「全量含退市票」，多次 list_codes / 不同 boards 调用结果一致、
    不被任何按 as_of 的过滤污染；
(c) screener 兜底过滤：as_of 前最近 N 个交易日内无有效日线（已退市/长期停牌、
    末根日线陈旧）的候选票被剔除，正常在交易的票保留。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

pl = pytest.importorskip("polars")

from app.domain import Bar
from app.quan_data import QuanStore
from app.screener import ScreenerConfig, run_screener
from app.universe import BOARD_GEM, BOARD_INDEX, UniverseRow, UniverseStore


# ---------------------------------------------------------------------------
# (a)(b) QuanStore 代码表：造一个含退市票的只读 quan 目录
# ---------------------------------------------------------------------------


def _business_days(end: date, count: int) -> list[str]:
    days: list[str] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return list(reversed(days))


DAILY_SCHEMA = {
    "code": pl.String, "date": pl.String, "open": pl.Float64, "high": pl.Float64,
    "low": pl.Float64, "close": pl.Float64, "volume": pl.Int64, "amount": pl.Float64,
}
BASIC_SCHEMA = {
    "code": pl.String, "name": pl.String, "st": pl.Boolean,
    "list_date": pl.String, "delisted": pl.Boolean,
}


@pytest.fixture()
def quan_dir_with_delisted(tmp_path: Path) -> Path:
    """创业板两只：300001 正常交易，300009 为 delisted=True 但有完整日线（历史当时在交易）。"""
    root = tmp_path / "quan"
    root.mkdir()
    dates = _business_days(date(2026, 11, 11), 320)
    daily: list[dict] = []
    for code, base in (("300001", 100.0), ("300009", 20.0)):
        for day in dates:
            close = base
            daily.append({
                "code": code, "date": day, "open": close, "high": close,
                "low": close, "close": close, "volume": 1_000_000, "amount": 1.2e9,
            })
    pl.DataFrame(daily, schema=DAILY_SCHEMA).write_parquet(root / "daily.parquet")
    pl.DataFrame(
        [
            {"code": "300001", "name": "创业一号", "st": False, "list_date": "2020-01-02", "delisted": False},
            {"code": "300009", "name": "历史退市样本", "st": False, "list_date": "2010-01-04", "delisted": True},
        ],
        schema=BASIC_SCHEMA,
    ).write_parquet(root / "stock_basic.parquet")
    return root


def test_all_codes_keeps_delisted_stock(quan_dir_with_delisted):
    """(a) 退市票不再被 _all_codes 剔除，照常进入代码表。"""
    store = QuanStore(quan_dir_with_delisted)
    codes = {row.code for row in store._all_codes()}
    assert "sz.300009" in codes  # delisted=True 仍保留
    assert "sz.300001" in codes


def test_list_codes_keeps_delisted_and_board_filter(quan_dir_with_delisted):
    """(a) list_codes 保留退市票，板块过滤仍正常（仅按 SUPPORTED_BOARDS）。"""
    store = QuanStore(quan_dir_with_delisted)
    gem = {row.code for row in store.list_codes(["创业板"])}
    assert "sz.300009" in gem and "sz.300001" in gem
    assert {row.code for row in store.list_codes()} == {"sz.300001", "sz.300009"}


def test_process_cache_not_polluted_across_calls(quan_dir_with_delisted):
    """(b) 进程缓存保持全量含退市票，多次/多板块调用结果一致，不被按 as_of 的过滤污染。

    QuanStore 的 self._codes 缓存必须是「全量含退市票」；任何时点化过滤只能出现在
    list_codes 出口（本次未加 as_of 形参，兜底过滤在 screener 侧）。此测试守护：
    无论调用多少次、传什么 boards，退市票始终在缓存与全量结果里，不会被某次过滤写回。
    """
    store = QuanStore(quan_dir_with_delisted)
    # 首次全量调用（填充缓存）
    full_once = {row.code for row in store.list_codes()}
    assert "sz.300009" in full_once
    # 多次板块子集调用
    for _ in range(3):
        subset = {row.code for row in store.list_codes(["创业板"])}
        assert "sz.300009" in subset
    # 再次全量调用：缓存未被任何子集/过滤污染
    full_again = {row.code for row in store.list_codes()}
    assert full_again == full_once
    assert "sz.300009" in full_again
    # 底层缓存对象本身仍是全量
    cached = store._codes
    assert cached is not None
    assert "sz.300009" in {row.code for row in cached}
    # get_code 同样能取到退市票
    assert store.get_code("300009") is not None


# ---------------------------------------------------------------------------
# (c) screener 兜底过滤：as_of 前 N 个交易日无有效日线则剔除
# ---------------------------------------------------------------------------


def _daily_bars(code: str, dates: list[str], closes: list[float], *, amount: float = 6e8) -> list[Bar]:
    result: list[Bar] = []
    previous: float | None = None
    for day, close in zip(dates, closes):
        high = max(close, previous or close) * 1.01
        low = min(close, previous or close) * 0.99
        result.append(Bar(
            timestamp=datetime.fromisoformat(day), symbol=code,
            open=close, high=high, low=low, close=close,
            volume=1_000_000, amount=amount, timeframe="daily",
            turnover_rate=0.03, prev_close=previous if previous is not None else close,
        ))
        previous = close
    return result


def _build_store_with_delisted(path) -> tuple[UniverseStore, list[str]]:
    """指数平盘（配合 l0_ret20_open 开门）+ 两只创业板：

    - sz.300001：正常在交易，日线覆盖全部交易日（末根=as_of）。
    - sz.300009：早已退市，日线止于早期（仅前 40 个交易日），末根远早于 as_of。
    两只都持续阴跌，若不考虑兜底，都会通过 L1 的 21 根窗口与超跌阈值。
    """
    store = UniverseStore(path)
    trading_dates = _business_days(date(2025, 12, 31), 260)  # 末位即 as_of
    # 指数轻微下行 → ret20 < 0 → L0 默认开门
    index_closes = [round(100.0 - index * 0.01, 4) for index in range(260)]
    index_code = BOARD_INDEX[BOARD_GEM]
    store.upsert_index(index_code, _daily_bars(index_code, trading_dates, index_closes))
    # 两只都阴跌：末段 Chg20D ≈ 0.98^20 - 1 ≈ -33%
    decl_closes = [round(100.0 * 0.98 ** index, 4) for index in range(260)]
    store.upsert_rows([UniverseRow(code="sz.300001", board=BOARD_GEM, name="正常样本")])
    store.upsert_daily("sz.300001", _daily_bars("sz.300001", trading_dates, decl_closes))
    # 退市票：只有前 40 个交易日有日线（历史够 21 根，但末根远早于 as_of）。
    # 名字刻意不含「退/ST」，确保它只会被「最近 N 日有效日线」兜底剔除，
    # 不会被 L2 的 is_delisting_name（按名称含「退」否决）抢先否决。
    delisted_closes = [round(20.0 * 0.98 ** index, 4) for index in range(40)]
    store.upsert_rows([UniverseRow(code="sz.300009", board=BOARD_GEM, name="历史样本B")])
    store.upsert_daily("sz.300009", _daily_bars("sz.300009", trading_dates[:40], delisted_closes))
    return store, trading_dates


def test_recency_filter_excludes_long_delisted(tmp_path):
    """(c) 默认 N=5：末根日线远早于 as_of 的已退市票被兜底剔除，正常票保留。"""
    store, _ = _build_store_with_delisted(tmp_path / "u.db")
    config = ScreenerConfig(
        boards=[BOARD_GEM], enable_fundamental_veto=False,
        l0_ret20_open=0.05,  # 平盘指数 ret20=0 < 0.05 → 开门
    )
    result = run_screener(store, None, config)
    selected = {candidate.code for candidate in result.pool}
    assert "sz.300001" in selected  # 正常在交易 → 保留
    assert "sz.300009" not in selected  # 末根日线远早于 as_of → 兜底剔除


def test_recency_filter_is_what_excludes_delisted(tmp_path):
    """对照：把 N 放大到 260（等于「最近 260 个交易日内有日线即可」），
    退市票就不再被兜底剔除——证明 (c) 中是「最近 N 日有效日线」这道过滤在起作用，
    而非 L1 的 21 根窗口（退市票历史 40 根，本就能过 21 根窗口）。
    """
    store, _ = _build_store_with_delisted(tmp_path / "u.db")
    config = ScreenerConfig(
        boards=[BOARD_GEM], enable_fundamental_veto=False,
        l0_ret20_open=0.05, min_recent_trade_days=260,
    )
    result = run_screener(store, None, config)
    selected = {candidate.code for candidate in result.pool}
    # 放宽 N 后退市票重新出现，说明默认 N=5 的兜底才是剔除它的机制
    assert "sz.300009" in selected


def test_recency_filter_uses_as_of_not_today(tmp_path):
    """兜底过滤必须以 as_of 口径判定，而非 date.today()。

    把 as_of 定格到早期（退市票尚未退市、仍在交易的时间点），此时退市票末根日线
    就在 as_of 附近，应被保留；正常票也保留。若误用 today()，早期 as_of 下两者都会
    因「末根离今天太远」被错杀。
    """
    store, trading_dates = _build_store_with_delisted(tmp_path / "u.db")
    early_as_of = trading_dates[39]  # 退市票最后一个交易日当天
    config = ScreenerConfig(
        boards=[BOARD_GEM], enable_fundamental_veto=False,
        l0_ret20_open=0.05,
    )
    result = run_screener(store, None, config, as_of=early_as_of)
    selected = {candidate.code for candidate in result.pool}
    # as_of=退市票末日：退市票末根就在 as_of，窗口内近端有效 → 保留（不靠未来退市结果剔除）
    assert "sz.300009" in selected
    assert "sz.300001" in selected