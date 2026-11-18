"""日线面板缓存（DailyPanel）+ load_bars 反向映射 单测。

覆盖：
- load_bars 反向映射 O(1) 查表后仍把纯代码正确改名为标准代码（键 + Bar.symbol）。
- DailyPanel 逐日窗口与「每次现读 parquet 的 load_history(end=as_of)」逐字节一致，
  且含 adj_factor 中途变化（拆股）的标的也能对齐（因为面板逐日仍用 as_of 因子基准复权）。
- run_screener 传 daily_panel 与不传，筛选结果（gate / funnel / first / 候选指标）完全一致。
- 面板确实被复用：跨两个 as_of 选股日，默认路径每天都调 load_bars，面板路径 0 次。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

pl = pytest.importorskip("polars")

from app.quan_data import QuanStore
from app.screener import ScreenerConfig, run_screener


# ---------------------------------------------------------------------------
# 造数工具（镜像 test_quan_data.py 的 micro fixture）
# ---------------------------------------------------------------------------


def business_days(end: date, count: int) -> list[str]:
    days: list[str] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return list(reversed(days))


def write_parquet(path: Path, rows: list[dict], schema: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema=schema).write_parquet(path)


DAILY_SCHEMA = {
    "code": pl.String, "date": pl.String, "open": pl.Float64, "high": pl.Float64,
    "low": pl.Float64, "close": pl.Float64, "volume": pl.Int64, "amount": pl.Float64,
}
ADJ_SCHEMA = {"code": pl.String, "date": pl.String, "adj_factor": pl.Float64}
BASIC_SCHEMA = {
    "code": pl.String, "name": pl.String, "st": pl.Boolean,
    "list_date": pl.String, "delisted": pl.Boolean,
}
INDEX_SCHEMA = {
    "index_key": pl.String, "date": pl.String, "open": pl.Float64, "high": pl.Float64,
    "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64,
}

_CODES = ("300001", "688001")  # 创业板 / 科创板各一只
_STANDARD = {"300001": "sz.300001", "688001": "sh.688001"}


def _build_micro(tmp_path: Path, *, n_days: int = 40) -> Path:
    """两只候选 + 创业板指数；300001 的 adj_factor 中途 1.0→2.0（拆股）。"""
    root = tmp_path / "quan"
    root.mkdir()
    dates = business_days(date(2026, 11, 11), n_days)
    split_at = n_days // 2  # 拆股日：前半段因子 1.0，后半段 2.0

    daily: list[dict] = []
    adj: list[dict] = []
    for index, day in enumerate(dates):
        # 创业板：持续阴跌
        close_gem = round(100 * 0.99 ** index, 4)
        daily.append({
            "code": "300001", "date": day, "open": close_gem * 0.997,
            "high": close_gem * 1.01, "low": close_gem * 0.99, "close": close_gem,
            "volume": 10_000_000, "amount": 1.2e9,
        })
        adj.append({
            "code": "300001", "date": day,
            "adj_factor": 1.0 if index < split_at else 2.0,
        })
        # 科创板：同样阴跌，因子恒 1.0
        close_star = round(80 * 0.99 ** index, 4)
        daily.append({
            "code": "688001", "date": day, "open": close_star * 0.997,
            "high": close_star * 1.01, "low": close_star * 0.99, "close": close_star,
            "volume": 8_000_000, "amount": 9e8,
        })
        adj.append({"code": "688001", "date": day, "adj_factor": 1.0})

    write_parquet(root / "daily.parquet", daily, DAILY_SCHEMA)
    write_parquet(root / "adj_factor.parquet", adj, ADJ_SCHEMA)
    write_parquet(
        root / "stock_basic.parquet",
        [
            {"code": "300001", "name": "创业一号", "st": False,
             "list_date": "2020-01-02", "delisted": False},
            {"code": "688001", "name": "科创一号", "st": False,
             "list_date": "2019-07-22", "delisted": False},
        ],
        BASIC_SCHEMA,
    )
    # 创业板指数：持续下跌 → ret20 < 0 → L0 开门
    index: list[dict] = []
    for day, index_close in zip(
        dates, [round(3000 * 0.999 ** i, 4) for i in range(n_days)],
    ):
        index.append({
            "index_key": "399006", "date": day, "open": index_close * 0.999,
            "high": index_close * 1.002, "low": index_close * 0.998,
            "close": index_close, "volume": 2e8, "amount": 0.0,
        })
    write_parquet(root / "index_daily.parquet", index, INDEX_SCHEMA)
    return root


def _screener_config() -> ScreenerConfig:
    return ScreenerConfig(
        boards=["创业板", "科创板"],
        enable_fundamental_veto=False,
        min_amount=0.0,
        min_avg_amount_20d=0.0,
    )


# ---------------------------------------------------------------------------
# FIX #4：反向映射 O(1) 查表，纯代码仍正确改名
# ---------------------------------------------------------------------------


def test_load_bars_reverse_mapping_renames_to_standard(tmp_path):
    store = QuanStore(_build_micro(tmp_path))
    # 混合传「标准代码」和「纯代码」，键与 Bar.symbol 都应是标准代码
    bars = store.load_bars(["sz.300001", "688001"], max_rows=5)
    assert set(bars) == {"sz.300001", "sh.688001"}
    for code, rows in bars.items():
        assert rows, f"{code} 应有日线"
        for bar in rows:
            assert bar.symbol == code
    # 每只窗口正确（5 根，末日为最新交易日）
    assert all(len(rows) == 5 for rows in bars.values())
    assert all(rows[-1].date == store.latest_trade_date() for rows in bars.values())


def test_load_bars_reverse_mapping_ignores_invalid_code(tmp_path):
    store = QuanStore(_build_micro(tmp_path))
    bars = store.load_bars(["sz.300001", "NOT-A-CODE"], max_rows=5)
    assert set(bars) == {"sz.300001"}


# ---------------------------------------------------------------------------
# FIX #1/#2：DailyPanel 逐日窗口与 load_history(end=as_of) 逐字节一致
# ---------------------------------------------------------------------------


def _assert_windows_equal(expected: dict, actual: dict, codes: list[str]) -> None:
    for code in codes:
        eb = expected.get(code, [])
        ab = actual.get(code, [])
        assert len(ab) == len(eb), f"{code} 窗口长度不一致：{len(ab)} vs {len(eb)}"
        for x, y in zip(ab, eb):
            assert x.date == y.date
            assert x.timestamp == y.timestamp
            assert x.symbol == y.symbol
            assert x.open == y.open and x.high == y.high
            assert x.low == y.low and x.close == y.close
            assert x.volume == y.volume and x.amount == y.amount
            assert x.prev_close == y.prev_close


def test_panel_history_matches_load_history_across_split(tmp_path):
    store = QuanStore(_build_micro(tmp_path))
    codes = [f"{_STANDARD[pure]}" for pure in _CODES]
    latest = store.latest_trade_date()
    panel = store.load_daily_panel(codes, end=latest)
    # 取三个 as_of：拆股前 / 拆股日 / 拆股后，验证含因子变化也逐字节对齐
    as_ofs = [
        store.load_history([codes[0]], max_rows=260, end=latest)[codes[0]][10].date,
        store.load_history([codes[0]], max_rows=260, end=latest)[codes[0]][
            40 // 2
        ].date,
        latest,
    ]
    for as_of in as_ofs:
        expected = store.load_history(codes, max_rows=260, end=as_of)
        actual = panel.history(codes, as_of, max_rows=260)
        _assert_windows_equal(expected, actual, codes)


def test_panel_history_max_rows_none_returns_full_range(tmp_path):
    store = QuanStore(_build_micro(tmp_path))
    codes = [_STANDARD[pure] for pure in _CODES]
    latest = store.latest_trade_date()
    panel = store.load_daily_panel(codes, end=latest)
    expected = store.load_bars(codes, max_rows=None, end=latest)
    actual = panel.history(codes, latest, max_rows=None)
    _assert_windows_equal(expected, actual, codes)


# ---------------------------------------------------------------------------
# run_screener：传 daily_panel 与不传，筛选结果完全一致
# ---------------------------------------------------------------------------


def _screening_payload(store, config, *, as_of, panel=None):
    result = run_screener(
        store, None, config, as_of=as_of, include_realtime=False,
        daily_panel=panel,
    )
    return {
        "gate": result.gate,
        "funnel": result.funnel,
        "pool": [c.to_dict() for c in result.pool],
        "eligible": [c.to_dict() for c in result.eligible],
        "first": result.first.to_dict() if result.first else None,
    }


def test_screener_result_identical_with_panel(tmp_path):
    store = QuanStore(_build_micro(tmp_path))
    config = _screener_config()
    latest = store.latest_trade_date()
    universe = [row.code for row in store.list_codes(config.boards)]
    panel = store.load_daily_panel(universe, end=latest)
    as_of = latest
    baseline = _screening_payload(store, config, as_of=as_of)
    with_panel = _screening_payload(store, config, as_of=as_of, panel=panel)
    assert baseline == with_panel
    # 前提自检：确有候选通过（否则等价断言空转）
    assert baseline["funnel"]["after_l0"] > 0
    assert baseline["first"] is not None


def test_screener_result_identical_with_panel_early_as_of(tmp_path):
    """早期 as_of（窗口不足 260 根）下两条路径也一致。"""
    store = QuanStore(_build_micro(tmp_path))
    config = _screener_config()
    latest = store.latest_trade_date()
    universe = [row.code for row in store.list_codes(config.boards)]
    panel = store.load_daily_panel(universe, end=latest)
    early = store.load_index("sz.399006", max_rows=260)[25].date
    baseline = _screening_payload(store, config, as_of=early)
    with_panel = _screening_payload(store, config, as_of=early, panel=panel)
    assert baseline == with_panel


# ---------------------------------------------------------------------------
# 缓存复用：面板路径跨日不再调 load_bars
# ---------------------------------------------------------------------------


def test_panel_reuses_cache_across_screening_days(tmp_path, monkeypatch):
    store = QuanStore(_build_micro(tmp_path))
    config = _screener_config()
    latest = store.latest_trade_date()
    index_bars = store.load_index("sz.399006", max_rows=260)
    day_a = index_bars[-5].date
    day_b = index_bars[-1].date
    days = [day_a, day_b]

    calls = {"load_bars": 0}
    original_load_bars = store.load_bars

    def counting_load_bars(codes, **kwargs):
        calls["load_bars"] += 1
        return original_load_bars(codes, **kwargs)

    monkeypatch.setattr(store, "load_bars", counting_load_bars)

    # 默认路径：每个选股日都要为候选池现读一次日线
    for day in days:
        run_screener(store, None, config, as_of=day, include_realtime=False)
    default_calls = calls["load_bars"]

    # 面板路径：建面板（一次 scan，不经 load_bars）后，逐日选股 0 次 load_bars
    calls["load_bars"] = 0
    universe = [row.code for row in store.list_codes(config.boards)]
    panel = store.load_daily_panel(universe, end=latest)
    assert calls["load_bars"] == 0, "建面板本身不应触发 load_bars"
    for day in days:
        run_screener(
            store, None, config, as_of=day, include_realtime=False,
            daily_panel=panel,
        )
    panel_calls = calls["load_bars"]

    assert default_calls >= len(days), "默认路径每个选股日至少现读一次日线"
    assert panel_calls == 0, "面板路径应完全复用内存面板，不再现读日线"