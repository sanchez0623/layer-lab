"""选股数据层单测：SQLite 缓存 / bootstrap / 增量刷新。

不依赖网络：baostock 与 market 全部用 monkeypatch 的假对象替代。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

from app.domain import Bar
from app.universe import (
    BOARD_GEM,
    BOARD_INDEX,
    BOARD_STAR,
    SUPPORTED_BOARDS,
    RefreshJob,
    UniverseBootstrap,
    UniverseRefresher,
    UniverseRow,
    UniverseStore,
    board_of,
    default_universe_db,
    is_delisting_name,
    is_st_name,
    is_unprofitable_u_name,
    parse_stock_basic_rows,
)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeResult:
    def __init__(self, rows: list[list[str]], error_code: str = "0", error_msg: str = "") -> None:
        self._rows = list(rows)
        self._index = 0
        self.error_code = error_code
        self.error_msg = error_msg

    def next(self) -> bool:
        if self._index >= len(self._rows):
            return False
        self._index += 1
        return True

    def get_row_data(self) -> list[str]:
        return self._rows[self._index - 1]


class FakeBaostock:
    def __init__(self, basic_rows=None, daily_rows=None, fail_codes=(), fail_once_codes=()) -> None:
        self.basic_rows = basic_rows or []
        self.daily_rows = daily_rows or {}
        self.fail_codes = set(fail_codes)
        self.fail_once_codes = set(fail_once_codes)
        self.login_count = 0
        self.logout_count = 0
        self.basic_count = 0
        self.history_calls: list[str] = []
        self._attempts: dict[str, int] = {}

    def login(self):
        self.login_count += 1
        return FakeResult([], "0")

    def logout(self):
        self.logout_count += 1
        return FakeResult([], "0")

    def query_stock_basic(self):
        self.basic_count += 1
        return FakeResult(self.basic_rows)

    def query_history_k_data_plus(
        self, code, fields, start_date=None, end_date=None, frequency=None, adjustflag=None
    ):
        self.history_calls.append(code)
        if code in self.fail_codes:
            return FakeResult([], "1", "模拟失败")
        if code in self.fail_once_codes:
            self._attempts[code] = self._attempts.get(code, 0) + 1
            if self._attempts[code] == 1:
                return FakeResult([], "1", "会话断开")
        return FakeResult(self.daily_rows.get(code, []))


class FakeIndexMarket:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_index_daily_by_code(self, index_code: str, start_date: str, end_date: str):
        self.calls.append(index_code)
        closes = [100.0] * 19 + [95.0]
        return make_bars(index_code, closes), "baostock_index"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def make_bars(
    code: str,
    closes: list[float],
    *,
    start: str = "2025-01-01",
    amounts: list[float] | None = None,
) -> list[Bar]:
    bars: list[Bar] = []
    previous: float | None = None
    for index, close in enumerate(closes):
        timestamp = datetime.strptime(start, "%Y-%m-%d") + timedelta(days=index)
        amount = amounts[index] if amounts else 1e8
        bars.append(
            Bar(
                timestamp=timestamp,
                symbol=code,
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=1e6,
                amount=amount,
                timeframe="daily",
                turnover_rate=0.02,
                prev_close=previous if previous is not None else close,
            )
        )
        previous = close
    return bars


def daily_rows(code: str, closes: list[float], start: str = "2025-01-01") -> list[list[str]]:
    """baostock 日线原始行：date,code,open,high,low,close,preclose,volume,amount,turn。"""
    rows: list[list[str]] = []
    for index, close in enumerate(closes):
        day = (datetime.strptime(start, "%Y-%m-%d") + timedelta(days=index)).date().isoformat()
        prev = closes[index - 1] if index else close
        rows.append(
            [
                day,
                code,
                f"{close:.2f}",
                f"{close * 1.01:.2f}",
                f"{close * 0.99:.2f}",
                f"{close:.2f}",
                f"{prev:.2f}",
                "1000000",
                "100000000",
                "2.0",
            ]
        )
    return rows


@pytest.fixture()
def store(tmp_path) -> UniverseStore:
    return UniverseStore(tmp_path / "universe.db")


# ---------------------------------------------------------------------------
# 基础函数
# ---------------------------------------------------------------------------


def test_board_and_name_helpers():
    assert board_of("sh.688200") == BOARD_STAR
    assert board_of("688200") == BOARD_STAR
    assert board_of("sz.300855") == BOARD_GEM
    assert board_of("300855") == BOARD_GEM
    assert board_of("sh.600000") == "沪市主板"
    assert board_of("sz.000001") == "深市主板"
    assert board_of("sh.900901") == "沪市B股"
    assert board_of("sz.200001") == "深市B股"
    assert board_of("abc") == "未知"

    assert is_st_name("*ST海投") is True
    assert is_st_name("华峰测控") is False
    assert is_delisting_name("退市海润") is True
    assert is_delisting_name("华峰测控") is False
    assert is_unprofitable_u_name("科创-U") is True
    assert is_unprofitable_u_name("科创－U") is True
    assert is_unprofitable_u_name("U控股") is False


def test_parse_stock_basic_rows_filters_scope():
    rows = [
        ["sh.688200", "华峰测控", "2019-02-01", "", "1", "1"],
        ["sz.300855", "图南股份", "2020-07-15", "2026-01-01", "1", "1"],  # 已退市
        ["sz.300001", "特锐德", "2010-01-15", "", "1", "0"],  # 停牌/非正常
        ["sh.600000", "浦发银行", "1999-11-10", "", "1", "1"],  # 主板不落
        ["sh.000001", "上证指数", "1990-12-19", "", "2", "1"],  # 指数
        ["sz.300002", "*ST测试", "2010-01-15", "", "1", "1"],
    ]
    parsed = parse_stock_basic_rows(rows)
    assert [row.code for row in parsed] == ["sh.688200", "sz.300002"]
    assert parsed[0].board == BOARD_STAR
    assert parsed[1].is_st is True
    assert parsed[0].ipo_date == "2019-02-01"


def test_store_rows_daily_and_coverage(store: UniverseStore):
    store.upsert_rows(
        [
            UniverseRow("sh.688200", BOARD_STAR, "华峰测控", ipo_date="2019-02-01"),
            UniverseRow("sz.300855", BOARD_GEM, "图南股份"),
        ]
    )
    assert store.get_code("sh.688200").name == "华峰测控"
    assert store.get_code("688200").name == "华峰测控"  # 纯代码兜底
    assert store.get_code("sh.600000") is None
    assert len(store.list_codes()) == 2
    assert [row.code for row in store.list_codes([BOARD_GEM])] == ["sz.300855"]

    bars = make_bars("sh.688200", [10.0, 11.0, 12.0], amounts=[1e8, 2e8, 3e8])
    assert store.upsert_daily("sh.688200", bars) == 3
    assert store.upsert_daily("sh.688200", []) == 0
    assert store.last_date("sh.688200") == "2025-01-03"
    assert store.last_date("sz.300855") is None
    assert store.latest_trade_date() == "2025-01-03"

    coverage = store.coverage()
    assert coverage["codes"] == 2
    assert coverage["rows"] == 3
    assert coverage["start"] == "2025-01-01"
    assert coverage["end"] == "2025-01-03"
    assert coverage["boards"] == {BOARD_GEM: 1, BOARD_STAR: 1}
    assert coverage["missing_codes"] == 1
    assert coverage["stale_codes"] == 0
    assert coverage["fundamentals"] == 0

    history = store.load_history(["sh.688200", "sz.300855"])
    assert len(history["sh.688200"]) == 3
    assert history["sz.300855"] == []
    assert [bar.date for bar in history["sh.688200"]] == [
        "2025-01-01",
        "2025-01-02",
        "2025-01-03",
    ]
    last = history["sh.688200"][-1]
    assert last.timeframe == "daily"
    assert last.amount == 3e8
    assert last.prev_close == 11.0
    assert last.turnover_rate == 0.02

    # 追加 4 条后 stale 判断：中位数之前的票变 stale
    store.upsert_daily("sz.300855", make_bars("sz.300855", [20.0, 21.0, 22.0, 23.0, 24.0]))
    coverage = store.coverage()
    assert coverage["missing_codes"] == 0
    assert coverage["stale_codes"] == 1
    assert coverage["end"] == "2025-01-05"


def test_load_history_limit_and_index(store: UniverseStore):
    closes = [float(100 + index) for index in range(300)]
    store.upsert_daily("sh.688200", make_bars("sh.688200", closes))
    history = store.load_history(["sh.688200"], max_rows=260)["sh.688200"]
    assert len(history) == 260
    assert history[0].close == 140.0
    assert history[-1].close == 399.0

    index_bars = make_bars(BOARD_INDEX[BOARD_GEM], [100.0 + index for index in range(30)])
    assert store.upsert_index(BOARD_INDEX[BOARD_GEM], index_bars) == 30
    loaded = store.load_index(BOARD_INDEX[BOARD_GEM], max_rows=10)
    assert len(loaded) == 10
    assert loaded[-1].close == 129.0
    assert loaded[0].close == 120.0
    assert store.load_index("sh.000688") == []


def test_fundamentals_cache(store: UniverseStore):
    assert store.get_fundamentals("sh.688200") is None
    store.save_fundamentals(
        "sh.688200",
        {"checked": True, "net_profit": -1e7, "revenue": 8e7, "veto": "亏损且营收<1亿"},
    )
    cached = store.get_fundamentals("sh.688200")
    assert cached["veto"] == "亏损且营收<1亿"
    assert cached["checked_at"]
    assert store.coverage()["fundamentals"] == 1


def test_meta_roundtrip(store: UniverseStore):
    assert store.get_meta("bootstrap_at") is None
    store.set_meta("bootstrap_at", "2026-11-12T10:00:00")
    assert store.get_meta("bootstrap_at") == "2026-11-12T10:00:00"


def test_refresh_job_to_dict():
    job = RefreshJob(running=True, done=3, total=10, current="sh.688200", errors=["x"])
    data = job.to_dict()
    assert data["done"] == 3 and data["total"] == 10
    assert data["errors"] == ["x"]


def test_default_universe_db_env(tmp_path, monkeypatch):
    monkeypatch.delenv("WB_UNIVERSE_DB", raising=False)
    assert str(default_universe_db()).endswith("backend\\data\\universe.db") or str(
        default_universe_db()
    ).endswith("backend/data/universe.db")
    monkeypatch.setenv("WB_UNIVERSE_DB", str(tmp_path / "custom.db"))
    assert default_universe_db() == tmp_path / "custom.db"


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_uses_query_stock_basic_once(store: UniverseStore, monkeypatch):
    basic_rows = [
        ["sh.688200", "华峰测控", "2019-02-01", "", "1", "1"],
        ["sz.300855", "图南股份", "2020-07-15", "", "1", "1"],
        ["sh.000001", "上证指数", "1990-12-19", "", "2", "1"],
    ]
    fake = FakeBaostock(basic_rows=basic_rows)
    monkeypatch.setitem(sys.modules, "baostock", fake)

    bootstrap = UniverseBootstrap(store)
    assert bootstrap.needs_bootstrap() is True
    result = bootstrap.run()
    assert result["rows"] == 2
    assert result["skipped"] is False
    assert fake.basic_count == 1
    assert fake.login_count == 1
    assert store.get_meta("bootstrap_at")
    assert len(store.list_codes()) == 2

    # 24h 内重复调用不再拉全市场
    again = UniverseBootstrap(store).run()
    assert again["skipped"] is True
    assert fake.basic_count == 1

    # force 重拉
    forced = UniverseBootstrap(store).run(force=True)
    assert forced["skipped"] is False
    assert fake.basic_count == 2


def test_bootstrap_raises_without_rows(store: UniverseStore, monkeypatch):
    monkeypatch.setitem(sys.modules, "baostock", FakeBaostock(basic_rows=[]))
    with pytest.raises(ValueError):
        UniverseBootstrap(store, max_age_hours=0).run(force=True)
    assert store.list_codes() == []


# ---------------------------------------------------------------------------
# refresher
# ---------------------------------------------------------------------------


def test_refresher_run_once_is_incremental(store: UniverseStore, monkeypatch):
    store.upsert_rows(
        [
            UniverseRow("sh.688200", BOARD_STAR, "华峰测控"),
            UniverseRow("sz.300855", BOARD_GEM, "图南股份"),
        ]
    )
    fake = FakeBaostock(
        daily_rows={
            "sh.688200": daily_rows("sh.688200", [10.0, 11.0, 12.0]),
            "sz.300855": daily_rows("sz.300855", [20.0, 21.0, 22.0]),
        }
    )
    monkeypatch.setitem(sys.modules, "baostock", fake)
    market = FakeIndexMarket()
    refresher = UniverseRefresher(store, market, sleep_seconds=0.0)

    progress: list[dict] = []
    result = refresher.run_once(
        codes=["688200", "300855"],
        lookback_days=60,
        progress=progress.append,
    )
    assert result["fetched_rows"] == 6
    assert result["skipped"] == 0
    assert result["errors"] == []
    assert fake.login_count == 1  # 一次 login 复用全部查询
    assert fake.logout_count == 1
    assert store.last_date("sh.688200") == "2025-01-03"
    assert store.coverage()["rows"] == 6
    assert store.get_meta("updated_at")
    # 指数一并落库
    assert len(store.load_index(BOARD_INDEX[BOARD_GEM])) == 20
    assert set(market.calls) == set(BOARD_INDEX.values())
    assert progress and progress[-1]["done"] == 2

    # 二次运行：已是最新交易日 → 全部跳过，不再查询日线
    calls_before = len(fake.history_calls)
    second = refresher.run_once(codes=["688200", "300855"], lookback_days=60)
    assert second["skipped"] == 2
    assert second["fetched_rows"] == 0
    assert len(fake.history_calls) == calls_before

    status = refresher.status()
    assert status["running"] is False
    assert status["job"]["finished_at"]
    assert status["coverage"]["rows"] == 6


def test_refresher_records_errors_and_continues(store: UniverseStore, monkeypatch):
    store.upsert_rows(
        [
            UniverseRow("sh.688200", BOARD_STAR, "华峰测控"),
            UniverseRow("sz.300855", BOARD_GEM, "图南股份"),
        ]
    )
    fake = FakeBaostock(
        daily_rows={
            "sh.688200": daily_rows("sh.688200", [10.0, 11.0]),
            "sz.300855": daily_rows("sz.300855", [20.0]),
        },
        fail_codes={"sz.300855"},
    )
    monkeypatch.setitem(sys.modules, "baostock", fake)
    refresher = UniverseRefresher(store, FakeIndexMarket(), sleep_seconds=0.0)

    result = refresher.run_once(codes=["688200", "300855"], lookback_days=60)
    assert result["fetched_rows"] == 2
    assert len(result["errors"]) == 1
    assert "sz.300855" in result["errors"][0]
    assert store.last_date("sh.688200") == "2025-01-02"
    assert store.last_date("sz.300855") is None


def test_refresher_start_background_and_status(store: UniverseStore, monkeypatch):
    import time

    fake = FakeBaostock(daily_rows={"sh.688200": daily_rows("sh.688200", [10.0, 11.0])})
    monkeypatch.setitem(sys.modules, "baostock", fake)
    refresher = UniverseRefresher(store, FakeIndexMarket(), sleep_seconds=0.0)

    started = refresher.start(codes=["688200"], lookback_days=60)
    assert started["status"] == "started"
    assert started["job"]["running"] is True
    with pytest.raises(ValueError, match="刷新任务已在运行"):
        refresher.start(codes=["688200"], lookback_days=60)

    deadline = time.time() + 10
    while refresher.status()["running"] and time.time() < deadline:
        time.sleep(0.02)
    status = refresher.status()
    assert status["running"] is False
    assert status["job"]["finished_at"]
    assert status["coverage"]["rows"] == 2
    assert store.last_date("sh.688200") == "2025-01-02"


def test_refresher_rejects_invalid_codes_and_concurrency(store: UniverseStore, monkeypatch):
    monkeypatch.setitem(sys.modules, "baostock", FakeBaostock())
    refresher = UniverseRefresher(store, FakeIndexMarket(), sleep_seconds=0.0)
    with pytest.raises(ValueError):
        refresher.run_once(codes=["不是代码"], lookback_days=60)

    refresher._running = True  # 模拟后台任务进行中
    with pytest.raises(ValueError, match="刷新任务已在运行"):
        refresher.run_once(codes=["688200"], lookback_days=60)


def test_refresher_creates_fallback_codes_for_unknown_codes(store: UniverseStore, monkeypatch):
    fake = FakeBaostock(daily_rows={"sh.688981": daily_rows("sh.688981", [50.0, 51.0])})
    monkeypatch.setitem(sys.modules, "baostock", fake)
    refresher = UniverseRefresher(store, FakeIndexMarket(), sleep_seconds=0.0)
    result = refresher.run_once(codes=["688981"], lookback_days=60)
    assert result["fetched_rows"] == 2
    row = store.get_code("sh.688981")  # --codes 路径也要有元数据兜底
    assert row is not None and row.board == BOARD_STAR


def test_refresher_relogins_after_session_failure(store: UniverseStore, monkeypatch):
    store.upsert_rows([UniverseRow("sh.688200", BOARD_STAR, "华峰测控")])
    fake = FakeBaostock(
        daily_rows={"sh.688200": daily_rows("sh.688200", [10.0, 11.0, 12.0])},
        fail_once_codes={"sh.688200"},
    )
    monkeypatch.setitem(sys.modules, "baostock", fake)
    refresher = UniverseRefresher(store, FakeIndexMarket(), sleep_seconds=0.0)

    result = refresher.run_once(codes=["688200"], lookback_days=60)
    assert result["errors"] == []
    assert result["fetched_rows"] == 3
    assert fake.login_count == 2  # 初始 login + 失败后重连
    assert store.last_date("sh.688200") == "2025-01-03"


def test_refresher_rotates_long_session(store: UniverseStore, monkeypatch):
    store.upsert_rows([UniverseRow("sh.688200", BOARD_STAR, "华峰测控")])
    fake = FakeBaostock(daily_rows={"sh.688200": daily_rows("sh.688200", [10.0, 11.0])})
    monkeypatch.setitem(sys.modules, "baostock", fake)
    refresher = UniverseRefresher(store, FakeIndexMarket(), sleep_seconds=0.0)
    refresher.session_max_age_seconds = 0.0  # 强制每次查询前轮换会话

    result = refresher.run_once(codes=["688200"], lookback_days=60)
    assert result["fetched_rows"] == 2
    assert fake.login_count == 2  # 初始 login + 定时轮换

