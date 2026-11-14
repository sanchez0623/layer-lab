"""quan_parquet 数据源适配层单测（不联网、不碰真实 D 盘数据）。

覆盖：代码映射、前复权换算、prev_close、指数/5分钟、覆盖度、
MarketDataService 接入、create_app 数据源选择与选股端到端、增量迁移只追加不覆盖。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

pl = pytest.importorskip("polars")

from fastapi.testclient import TestClient

from app.domain import Bar
from app.market import MarketDataService
from app.quan_data import (
    QuanRefresher,
    QuanStore,
    infer_exchange,
    pure_code,
    quan_data_dir,
    standard_code,
)


# ---------------------------------------------------------------------------
# 造数工具
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
MINUTE_SCHEMA = {
    "code": pl.String, "date": pl.String, "open": pl.Float64, "high": pl.Float64,
    "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64,
}


@pytest.fixture()
def quan_dir(tmp_path: Path) -> Path:
    """造一个覆盖双创 + 指数的只读 quan 数据目录。"""
    root = tmp_path / "quan"
    root.mkdir()
    dates = business_days(date(2026, 11, 11), 320)
    daily: list[dict] = []
    adj: list[dict] = []

    def add_stock(code: str, closes: list[float], *, amount: float = 1.2e9) -> None:
        for day, close in zip(dates[-len(closes):], closes):
            daily.append({
                "code": code, "date": day, "open": close * 0.997,
                "high": close * 1.01, "low": close * 0.99, "close": close,
                "volume": 10_000_000, "amount": amount,
            })
            adj.append({"code": code, "date": day, "adj_factor": 2.0})

    # 创业板：持续阴跌（末 20 日 < -10%，距 52 周高回撤远大于 20%）
    add_stock("300001", [round(100 * 0.994 ** i, 4) for i in range(320)])
    add_stock("300002", [round(50 * 0.994 ** i, 4) for i in range(320)])
    # 科创板：同样阴跌
    add_stock("688001", [round(80 * 0.994 ** i, 4) for i in range(320)])
    # 沪深主板 / 退市：不应进入双创代码表
    add_stock("600000", [round(10 * 0.999 ** i, 4) for i in range(320)])
    add_stock("300009", [round(20 * 0.994 ** i, 4) for i in range(320)])

    write_parquet(root / "daily.parquet", daily, DAILY_SCHEMA)
    write_parquet(root / "adj_factor.parquet", adj, ADJ_SCHEMA)
    write_parquet(
        root / "stock_basic.parquet",
        [
            {"code": "300001", "name": "创业一号", "st": False, "list_date": "2020-01-02", "delisted": False},
            {"code": "300002", "name": "*ST创业二", "st": True, "list_date": "2015-06-01", "delisted": False},
            {"code": "688001", "name": "科创一号", "st": False, "list_date": "2019-07-22", "delisted": False},
            {"code": "600000", "name": "浦发银行", "st": False, "list_date": "1999-11-10", "delisted": False},
            {"code": "300009", "name": "退市样本", "st": False, "list_date": "2010-01-04", "delisted": True},
        ],
        BASIC_SCHEMA,
    )
    index: list[dict] = []
    for key, closes in (
        ("399006", [round(3000 * 0.999 ** i, 4) for i in range(320)]),
        ("000688", [1000.0] * 320),
    ):
        for day, close in zip(dates, closes):
            index.append({
                "index_key": key, "date": day, "open": close * 0.999,
                "high": close * 1.002, "low": close * 0.998, "close": close,
                "volume": 2e8, "amount": 0.0,
            })
    write_parquet(root / "index_daily.parquet", index, INDEX_SCHEMA)

    minute_rows = []
    for offset, clock in enumerate(("09:35", "09:40", "09:45", "09:50")):
        minute_rows.append({
            "code": "300001", "date": f"2026-11-11 {clock}", "open": 20.0 + offset,
            "high": 20.5 + offset, "low": 19.5 + offset, "close": 20.2 + offset,
            "volume": 1000.0, "amount": 20000.0,
        })
    write_parquet(root / "minute5" / "300001.parquet", minute_rows, MINUTE_SCHEMA)
    return root


# ---------------------------------------------------------------------------
# 代码映射 / 目录解析
# ---------------------------------------------------------------------------


def test_quan_trade_calendar_trade_days(quan_dir):
    """trade_calendar.parquet 的 is_open 决定交易日；节假日必须被识别为休市。"""
    calendar = [
        {"date": day, "is_open": 1}
        for day in business_days(date(2026, 11, 11), 320)
    ]
    # 模拟 2026-10-01 ~ 10-07 长假休市（其中含工作日）
    calendar = [item for item in calendar if not ("2026-10-01" <= item["date"] <= "2026-10-07")]
    write_parquet(
        quan_dir / "trade_calendar.parquet", calendar,
        {"date": pl.String, "is_open": pl.Int8},
    )
    store = QuanStore(quan_dir)
    assert store.calendar_span() == (calendar[0]["date"], calendar[-1]["date"])
    days = store.trade_days("2026-09-28", "2026-10-09")
    assert days is not None
    assert "2026-10-01" not in days and "2026-10-05" not in days
    assert any("2026-09-28" <= day <= "2026-09-30" for day in days)
    assert any("2026-10-08" <= day <= "2026-10-09" for day in days)
    # 请求窗口完全在日历之外 → 空列表（表示「没有交易日」，而非「无法判断」）
    assert store.trade_days("2030-01-01", "2030-01-31") == []


def test_quan_trade_days_without_calendar(quan_dir):
    store = QuanStore(quan_dir)
    assert store.trade_days("2026-11-01", "2026-11-30") is None
    assert store.calendar_span() is None


def test_code_mapping_roundtrip():
    assert infer_exchange("688200") == "sh"
    assert infer_exchange("300855") == "sz"
    assert pure_code("sh.688200") == "688200"
    assert pure_code("688200.SH") == "688200"
    assert pure_code("sh688200") == "688200"
    assert standard_code("300855") == "sz.300855"
    assert standard_code("688200") == "sh.688200"
    with pytest.raises(ValueError):
        pure_code("NOT-A-CODE")


def test_quan_data_dir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_QUAN_DATA_DIR", "")
    assert quan_data_dir() is None
    monkeypatch.setenv("WB_QUAN_DATA_DIR", str(tmp_path / "missing"))
    assert quan_data_dir() is None
    target = tmp_path / "quan"
    target.mkdir()
    (target / "daily.parquet").write_bytes(b"")
    monkeypatch.setenv("WB_QUAN_DATA_DIR", str(target))
    assert quan_data_dir() == target


# ---------------------------------------------------------------------------
# 前复权 / prev_close / 窗口
# ---------------------------------------------------------------------------


def test_qfq_adjustment_and_prev_close(tmp_path):
    root = tmp_path / "micro"
    root.mkdir()
    dates = ["2026-11-09", "2026-11-10", "2026-11-11"]
    closes = [10.0, 10.0, 10.0]
    write_parquet(
        root / "daily.parquet",
        [
            {"code": "300001", "date": day, "open": c, "high": c, "low": c, "close": c,
             "volume": 100, "amount": 1000.0}
            for day, c in zip(dates, closes)
        ],
        DAILY_SCHEMA,
    )
    write_parquet(
        root / "adj_factor.parquet",
        [
            {"code": "300001", "date": "2026-11-09", "adj_factor": 1.0},
            {"code": "300001", "date": "2026-11-10", "adj_factor": 2.0},
            {"code": "300001", "date": "2026-11-11", "adj_factor": 2.0},
        ],
        ADJ_SCHEMA,
    )
    store = QuanStore(root)
    bars = store.load_series("sz.300001", end="2026-11-11")
    # 因子基准=末日 2.0：原始价 10 → 复权价 5 / 10 / 10
    assert [bar.close for bar in bars] == [5.0, 10.0, 10.0]
    assert bars[0].prev_close == 5.0
    assert bars[1].prev_close == 5.0
    assert bars[2].prev_close == 10.0

    limited = store.load_series("sz.300001", end="2026-11-11", max_rows=2)
    assert [bar.close for bar in limited] == [10.0, 10.0]
    assert limited[0].prev_close == 5.0  # 窗口外前一日收盘，仍能正确推出


def test_adj_factor_rollback_falls_back_to_raw(tmp_path):
    """adj_factor 回退（不可能出现）时降级为原始价，避免伪造涨跌。"""
    root = tmp_path / "rollback"
    root.mkdir()
    dates = ["2026-11-09", "2026-11-10", "2026-11-11"]
    write_parquet(
        root / "daily.parquet",
        [
            {"code": "300001", "date": day, "open": 10.0, "high": 10.0, "low": 10.0,
             "close": 10.0, "volume": 100, "amount": 1000.0}
            for day in dates
        ],
        DAILY_SCHEMA,
    )
    write_parquet(
        root / "adj_factor.parquet",
        [
            {"code": "300001", "date": "2026-11-09", "adj_factor": 1.0},
            {"code": "300001", "date": "2026-11-10", "adj_factor": 2.0},
            {"code": "300001", "date": "2026-11-11", "adj_factor": 1.0},
        ],
        ADJ_SCHEMA,
    )
    store = QuanStore(root)
    bars = store.load_series("sz.300001", end="2026-11-11")
    assert [bar.close for bar in bars] == [10.0, 10.0, 10.0]
    coverage = store.coverage()
    assert coverage["factor_anomaly_codes"] == 1
    assert coverage["adjustment"] == "qfq_with_raw_fallback"


def test_load_history_and_latest_trade_date(quan_dir):
    store = QuanStore(quan_dir)
    assert store.latest_trade_date() == "2026-11-11"
    history = store.load_history(["sz.300001", "300002"], max_rows=30)
    assert set(history) == {"sz.300001", "sz.300002"}
    assert len(history["sz.300001"]) == 30
    assert history["sz.300001"][-1].date == "2026-11-11"
    assert store.last_date("sz.300001") == "2026-11-11"


def test_load_history_with_end_returns_window_before_end(quan_dir):
    """end 用于历史时点回看：取「截至 end 的最近 N 根」，而非最新交易日往前 N 根。"""
    store = QuanStore(quan_dir)
    latest = store.load_history(["sz.300001"], max_rows=40)
    as_of = latest["sz.300001"][10].date  # 最新窗口内的一个早期日期
    truncated = store.load_history(["sz.300001"], max_rows=20, end=as_of)
    assert len(truncated["sz.300001"]) == 20
    assert truncated["sz.300001"][-1].date == as_of
    # 起点 = as_of 往前 19 个交易日，与「最新往前 20 根」不同
    assert truncated["sz.300001"][0].date < latest["sz.300001"][0].date


def test_load_index(quan_dir):
    store = QuanStore(quan_dir)
    bars = store.load_index("sz.399006", max_rows=30)
    assert len(bars) == 30
    assert bars[0].symbol == "sz.399006"
    assert bars[-1].date == "2026-11-11"
    assert bars[-1].prev_close == bars[-2].close
    assert store.list_index_keys() == ["000688", "399006"]
    assert store.load_index("sh.000001") == []


def test_load_minute5(quan_dir):
    store = QuanStore(quan_dir)
    assert store.has_minute5("sz.300001") is True
    bars = store.load_minute5("300001")
    assert [bar.timeframe for bar in bars] == ["5m"] * 4
    assert bars[-1].timestamp == datetime(2026, 11, 11, 9, 50)
    assert store.load_minute5("sz.688001") == []


# ---------------------------------------------------------------------------
# 代码表 / 覆盖度 / refresher
# ---------------------------------------------------------------------------


def test_list_codes_filters_boards_and_delisted(quan_dir):
    store = QuanStore(quan_dir)
    gem = store.list_codes(["创业板"])
    assert [row.code for row in gem] == ["sz.300001", "sz.300002"]
    assert gem[1].is_st is True
    star = store.list_codes(["科创板"])
    assert [row.code for row in star] == ["sh.688001"]
    all_rows = {row.code for row in store.list_codes()}
    assert "sh.600000" not in all_rows and "sz.300009" not in all_rows
    assert store.get_code("300001").name == "创业一号"
    assert store.get_code("999999") is None


def test_coverage(quan_dir):
    store = QuanStore(quan_dir)
    coverage = store.coverage()
    assert coverage["provider"] == "quan_parquet"
    assert coverage["codes"] == 5
    assert coverage["start"] == business_days(date(2026, 11, 11), 320)[0]
    assert coverage["end"] == "2026-11-11"
    assert coverage["boards"] == {"创业板": 2, "科创板": 1}
    assert coverage["missing_codes"] == 0
    assert coverage["stale_codes"] == 0
    assert coverage["turnover_available"] is False


def test_refresher_is_readonly(quan_dir):
    store = QuanStore(quan_dir)
    refresher = QuanRefresher(store)
    status = refresher.status()
    assert status["running"] is False
    assert status["provider"] == "quan_parquet"
    started = refresher.start()
    assert started["status"] == "skipped"
    assert refresher.run_once()["status"] == "skipped"


# ---------------------------------------------------------------------------
# MarketDataService 接入
# ---------------------------------------------------------------------------


def test_market_bundle_uses_quan(quan_dir, tmp_path, monkeypatch):
    store = QuanStore(quan_dir)
    service = MarketDataService(tmp_path / "cache", quan=store)
    monkeypatch.setattr(
        service, "fetch_quote",
        lambda symbol: ({"name": "创业一号", "price": 1.0, "turnover_rate": None}, None),
    )
    bundle = service.fetch_history_bundle(
        "300001", start_date="2025-11-12", end_date="2026-11-11",
    )
    assert bundle["daily"].source == "quan_parquet"
    assert bundle["daily"].timeframe == "daily"
    assert bundle["index"].source == "quan_index"
    assert bundle["index"].symbol == "sz.399006"
    assert bundle["intraday"].timeframe == "5m"
    assert bundle["source_info"]["daily_source"] == "quan_parquet"
    assert Path(bundle["daily"].file_path).exists()
    assert "quan_parquet" in service.source_status()
    assert "baostock" not in service.source_status()


def test_market_bundle_requires_index(quan_dir, tmp_path, monkeypatch):
    store = QuanStore(quan_dir)
    (quan_dir / "index_daily.parquet").unlink()
    service = MarketDataService(tmp_path / "cache", quan=store)
    monkeypatch.setattr(service, "fetch_quote", lambda symbol: ({}, None))
    with pytest.raises(ValueError) as excinfo:
        service.fetch_history_bundle("300001")
    assert "quan_migrate.py index" in str(excinfo.value)


def test_market_bundle_falls_back_to_tencent_5m_when_quan_minute5_missing(
    quan_dir, tmp_path, monkeypatch,
):
    """服务器未同步 quan minute5 时，5 分钟数据回落腾讯 m5 并如实标记来源。"""
    store = QuanStore(quan_dir)
    service = MarketDataService(tmp_path / "cache", quan=store)
    monkeypatch.setattr(service, "fetch_quote", lambda symbol: ({"name": "科创一号"}, None))
    bar = Bar(
        timestamp=datetime(2026, 11, 11, 15, 0), symbol="sh.688001",
        open=1, high=1, low=1, close=1, volume=100, amount=100, timeframe="5m",
    )
    monkeypatch.setattr(
        service, "fetch_tencent_intraday", lambda symbol, count=320: ([bar], {}),
    )
    bundle = service.fetch_history_bundle(
        "688001", start_date="2025-11-12", end_date="2026-11-11",
    )
    assert bundle["intraday"].timeframe == "5m"
    assert bundle["intraday"].source == "tencent"
    assert bundle["source_info"]["intraday_source"] == "tencent"


# ---------------------------------------------------------------------------
# create_app：quan 源选择 + 选股端到端
# ---------------------------------------------------------------------------


def test_app_trading_session_uses_quan_calendar(quan_dir, tmp_path, monkeypatch):
    """日历覆盖当天时，节假日必须判为休市，并给出下一个交易日。"""
    today = date.today()
    next_open = (today + timedelta(days=3)).isoformat()
    calendar = [
        {"date": (today - timedelta(days=5)).isoformat(), "is_open": 1},
        {"date": next_open, "is_open": 1},
    ]
    write_parquet(
        quan_dir / "trade_calendar.parquet", calendar,
        {"date": pl.String, "is_open": pl.Int8},
    )
    app_data = tmp_path / "appdata"
    monkeypatch.setenv("WB_QUAN_DATA_DIR", str(quan_dir))
    monkeypatch.setenv("WB_DATA_DIR", str(app_data))
    monkeypatch.setenv("WB_DB_PATH", str(app_data / "app.db"))
    from app.main import create_app

    client = TestClient(create_app())
    payload = client.get("/api/trading/session").json()
    assert payload["today"] == today.isoformat()
    assert payload["source"] == "quan_calendar"
    assert payload["is_trading_day"] is False
    assert payload["next_trading_day"] == next_open
    assert payload["in_session"] is False
    scheduler = client.get("/api/paper/scheduler").json()
    assert scheduler["trading"]["is_trading_day"] is False


def test_app_uses_quan_source_and_screener_runs(quan_dir, tmp_path, monkeypatch):
    app_data = tmp_path / "appdata"
    monkeypatch.setenv("WB_QUAN_DATA_DIR", str(quan_dir))
    monkeypatch.setenv("WB_DATA_DIR", str(app_data))
    monkeypatch.setenv("WB_DB_PATH", str(app_data / "app.db"))
    from app.main import create_app

    client = TestClient(create_app())
    health = client.get("/api/health").json()
    assert "quan_parquet" in health["data_sources"]
    assert health["quan_data_dir"] == str(quan_dir)
    assert health["data_end"] == "2026-11-11"
    assert not (app_data / "universe.db").exists()  # 不再创建本地行情库

    defaults = client.get("/api/screener/defaults").json()
    assert defaults["sources"]["provider"] == "quan_parquet"
    assert defaults["coverage"]["provider"] == "quan_parquet"

    response = client.post(
        "/api/screener/run",
        json={"config": {
            "enable_fundamental_veto": False, "min_amount": 0,
            "min_avg_amount_20d": 0, "chg20_max": -0.10, "week52_drawdown": 0.2,
        }},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["as_of"] == "2026-11-11"
    assert data["gate"]["open"] is True
    assert data["gate"]["boards"]["创业板"]["open"] is True
    assert data["first"]["code"] == "sz.300001"
    assert data["data_info"]["coverage"]["provider"] == "quan_parquet"


# ---------------------------------------------------------------------------
# 增量迁移：只追加不覆盖
# ---------------------------------------------------------------------------


def test_merge_append_only_keeps_existing_rows():
    from scripts.quan_migrate import merge_append_only

    schema = {name: dtype for name, dtype in INDEX_SCHEMA.items()}
    existing = pl.DataFrame(
        [
            {"index_key": "000300", "date": "2026-11-10", "open": 1.0, "high": 2.0,
             "low": 0.5, "close": 1.5, "volume": 10.0, "amount": 0.0},
            {"index_key": "000300", "date": "2026-11-11", "open": 1.5, "high": 2.5,
             "low": 1.0, "close": 2.0, "volume": 11.0, "amount": 0.0},
        ],
        schema=schema,
    )
    incoming = pl.DataFrame(
        [
            # 与现有行同日但数值不同：必须保留现有值，不覆盖
            {"index_key": "000300", "date": "2026-11-11", "open": 9.0, "high": 9.0,
             "low": 9.0, "close": 9.0, "volume": 9.0, "amount": 9.0},
            {"index_key": "399006", "date": "2026-11-10", "open": 3.0, "high": 3.1,
             "low": 2.9, "close": 3.0, "volume": 5.0, "amount": 0.0},
            {"index_key": "399006", "date": "2026-11-11", "open": 3.1, "high": 3.2,
             "low": 3.0, "close": 3.1, "volume": 6.0, "amount": 0.0},
        ],
        schema=schema,
    )
    merged, added, duplicates = merge_append_only(existing, incoming, pl)
    assert added == 2
    assert duplicates == 1
    assert merged.height == existing.height + added
    kept = merged.filter(
        (pl.col("index_key") == "000300") & (pl.col("date") == "2026-11-11")
    ).row(0, named=True)
    assert kept["close"] == 2.0 and kept["open"] == 1.5  # 原值未被覆盖
    assert set(merged["index_key"].unique().to_list()) == {"000300", "399006"}


def test_merge_append_only_idempotent():
    from scripts.quan_migrate import merge_append_only

    frame = pl.DataFrame(
        [{"index_key": "399006", "date": "2026-11-11", "open": 3.0, "high": 3.2,
          "low": 2.9, "close": 3.1, "volume": 5.0, "amount": 0.0}],
        schema=INDEX_SCHEMA,
    )
    merged, added, duplicates = merge_append_only(frame, frame, pl)
    assert added == 0 and duplicates == 1
    assert merged.height == 1


def test_drop_unfinished_today():
    from scripts.quan_migrate import _drop_unfinished_today

    rows = [
        {"index_key": "399006", "date": "2026-11-12", "open": 1.0, "high": 1.0,
         "low": 1.0, "close": 1.0, "volume": 1.0, "amount": 0.0},
        {"index_key": "399006", "date": "2026-11-11", "open": 1.0, "high": 1.0,
         "low": 1.0, "close": 1.0, "volume": 1.0, "amount": 0.0},
    ]
    before_close = datetime(2026, 11, 12, 11, 30)
    assert len(_drop_unfinished_today(rows, now=before_close)) == 1
    after_close = datetime(2026, 11, 12, 15, 30)
    assert len(_drop_unfinished_today(rows, now=after_close)) == 2
