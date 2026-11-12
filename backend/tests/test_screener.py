"""选股引擎 L0–L4 单测：合成 260 根日线，不依赖网络。

覆盖：L0 开/关/缺失降级、L1 边界、L2 各否决项、L3 门槛、L4 排序与深筛逐只否决、
realtime 叠加、gate/funnel/pool 结构、to_dict JSON 安全。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

import pytest

import app.screener as screener
from app.domain import Bar
from app.screener import ScreenerConfig, run_screener
from app.universe import BOARD_GEM, BOARD_INDEX, BOARD_STAR, UniverseRow, UniverseStore

DEFAULT_AMOUNT = 6e8


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def make_bars(
    code: str,
    closes: list[float],
    *,
    amounts: list[float] | None = None,
    highs: list[float] | None = None,
    start: str = "2025-01-01",
) -> list[Bar]:
    dates = [
        (datetime.strptime(start, "%Y-%m-%d") + timedelta(days=index)).strftime("%Y-%m-%d")
        for index in range(len(closes))
    ]
    bars: list[Bar] = []
    previous: float | None = None
    for index, close in enumerate(closes):
        high = highs[index] if highs is not None else close * 1.01
        amount = amounts[index] if amounts is not None else DEFAULT_AMOUNT
        bars.append(
            Bar(
                timestamp=datetime.strptime(dates[index], "%Y-%m-%d"),
                symbol=code,
                open=close,
                high=high,
                low=close * 0.99,
                close=close,
                volume=1e6,
                amount=amount,
                timeframe="daily",
                turnover_rate=0.03,
                prev_close=previous if previous is not None else close,
            )
        )
        previous = close
    return bars


def add_stock(
    store: UniverseStore,
    code: str,
    name: str,
    board: str,
    *,
    last: float = 60.0,
    base: float = 100.0,
    count: int = 40,
    prev_tail: float | None = None,
    amounts: list[float] | None = None,
    highs: list[float] | None = None,
    ipo_date: str = "",
) -> None:
    closes = [base] * (count - 1) + [last]
    if prev_tail is not None:
        closes[-2] = prev_tail
    store.upsert_rows([UniverseRow(code=code, board=board, name=name, ipo_date=ipo_date)])
    store.upsert_daily(code, make_bars(code, closes, amounts=amounts, highs=highs))


def add_index(store: UniverseStore, board: str, closes: list[float]) -> None:
    store.upsert_index(BOARD_INDEX[board], make_bars(BOARD_INDEX[board], closes))


def open_indices(store: UniverseStore) -> None:
    add_index(store, BOARD_GEM, [100.0] * 20 + [90.0])
    add_index(store, BOARD_STAR, [100.0] * 20 + [90.0])


def cfg(**overrides) -> ScreenerConfig:
    base: dict = {"enable_fundamental_veto": False, "boards": [BOARD_GEM, BOARD_STAR]}
    base.update(overrides)
    return ScreenerConfig(**base)


def fake_fundamental(veto_codes=(), unavailable_codes=()):
    """返回可在 monkeypatch 里替换 FundamentalService.check 的假实现。"""
    veto_codes = set(veto_codes)
    unavailable_codes = set(unavailable_codes)
    calls: list[str] = []

    def fake_check(self, code):  # noqa: ANN001
        calls.append(code)
        if code in unavailable_codes:
            return {
                "checked": False, "net_profit": None, "revenue": None,
                "negative_equity": None, "veto": None, "source": "unavailable",
            }
        veto = "亏损且营收<1亿" if code in veto_codes else None
        return {
            "checked": True, "net_profit": 1e6, "revenue": 1e9,
            "negative_equity": False, "veto": veto, "source": "baostock",
        }

    fake_check.calls = calls  # type: ignore[attr-defined]
    return fake_check


# ---------------------------------------------------------------------------
# L0 / 结构
# ---------------------------------------------------------------------------


def test_gate_open_funnel_and_first(tmp_path):
    store = UniverseStore(tmp_path / "u.db")
    open_indices(store)
    add_stock(store, "sh.688200", "华峰测控", BOARD_STAR, last=60.0)  # L1 命中
    add_stock(store, "sz.300001", "特锐德", BOARD_GEM, last=90.0)      # 20日 -10%，不命中
    add_stock(store, "sz.300002", "退市测试", BOARD_GEM, last=60.0)    # L2 退市否决

    result = run_screener(store, None, cfg())
    data = result.to_dict()

    assert data["funnel"] == {
        "universe": 3, "after_l0": 3, "after_l1": 2, "after_l2": 1,
        "after_l3": 1, "eligible": 1, "deep_checked": 0,
    }
    assert data["gate"]["open"] is True
    for board in (BOARD_GEM, BOARD_STAR):
        entry = data["gate"]["boards"][board]
        assert entry["open"] is True
        assert entry["ret20"] == pytest.approx(-0.1, abs=1e-6)
        assert entry["date"] == "2025-01-21"
    assert data["as_of"] == "2025-02-09"
    assert [item["code"] for item in data["pool"]] == ["sh.688200"]
    assert data["first"]["code"] == "sh.688200"
    assert data["first"]["name"] == "华峰测控"
    assert data["data_info"]["fundamental"] == "disabled"
    assert data["data_info"]["realtime"] is False
    assert data["data_info"]["realtime_quotes"] == 0
    assert data["id"].startswith("run_")

    # 候选 checks：L2 各规则 + 数据源未覆盖的 unknown
    checks = data["first"]["checks"]
    rules = {item["rule"] for item in checks}
    assert "ST/*ST" in rules
    assert any(item["status"] == "unknown" for item in checks)
    assert {item["layer"] for item in checks} >= {"L2", "L3"}
    # JSON 安全（allow_nan=False 可序列化）
    json.dumps(data, ensure_ascii=False, allow_nan=False)


def test_gate_closed_short_circuits(tmp_path):
    store = UniverseStore(tmp_path / "u.db")
    add_index(store, BOARD_GEM, [100.0] * 20 + [105.0])
    add_index(store, BOARD_STAR, [100.0] * 20 + [103.0])
    add_stock(store, "sh.688200", "华峰测控", BOARD_STAR, last=60.0)

    result = run_screener(store, None, cfg())

    assert result.gate["open"] is False
    assert result.funnel["after_l0"] == 0
    assert result.funnel["after_l1"] == 0
    assert result.pool == []
    assert result.eligible == []
    assert result.first is None
    assert any("L0 大盘门关闭" in item for item in result.warnings)


def test_missing_index_degrades_to_open(tmp_path):
    store = UniverseStore(tmp_path / "u.db")
    add_index(store, BOARD_GEM, [100.0] * 20 + [105.0])  # 创业板门关
    # 科创板指数缺失 → 降级开门
    add_stock(store, "sh.688200", "华峰测控", BOARD_STAR, last=60.0)
    add_stock(store, "sz.300001", "特锐德", BOARD_GEM, last=60.0)

    result = run_screener(store, None, cfg())

    assert result.gate["open"] is True
    assert result.gate["boards"][BOARD_STAR]["open"] is True
    assert result.gate["boards"][BOARD_STAR]["ret20"] is None
    assert result.gate["boards"][BOARD_GEM]["open"] is False
    assert result.funnel["after_l0"] == 1  # 只保留科创板
    assert [item.code for item in result.pool] == ["sh.688200"]
    assert any("按开门降级" in item for item in result.warnings)


# ---------------------------------------------------------------------------
# L1
# ---------------------------------------------------------------------------


def test_l1_boundaries(tmp_path):
    store = UniverseStore(tmp_path / "u.db")
    open_indices(store)
    add_stock(store, "sz.300001", "跌十个点", BOARD_GEM, last=90.0)             # chg20d -10%
    add_stock(store, "sz.300002", "回撤不够", BOARD_GEM, last=60.0, base=80.0)  # chg -25%，回撤 25.7%
    add_stock(store, "sz.300003", "双条件满足", BOARD_GEM, last=60.0)           # chg -40%，回撤 40.6%

    result = run_screener(store, None, cfg())
    assert result.funnel["after_l1"] == 1
    assert result.pool[0].code == "sz.300003"
    assert result.pool[0].chg20d == pytest.approx(-0.4, abs=1e-9)
    assert result.pool[0].drawdown_52w == pytest.approx(1 - 60 / 101.0, abs=1e-9)
    assert result.pool[0].sessions == 40


def test_as_of_truncates_history(tmp_path):
    store = UniverseStore(tmp_path / "u.db")
    open_indices(store)
    add_stock(store, "sh.688200", "华峰测控", BOARD_STAR, last=60.0)

    full = run_screener(store, None, cfg())
    assert full.funnel["after_l1"] == 1
    assert full.as_of == "2025-02-09"

    truncated = run_screener(store, None, cfg(), as_of="2025-01-30")
    assert truncated.as_of == "2025-01-30"
    assert truncated.funnel["after_l1"] == 0  # 截断后 20 日跌幅为 0

    with pytest.raises(ValueError):
        run_screener(store, None, cfg(), as_of="2025/01/30")


# ---------------------------------------------------------------------------
# L2
# ---------------------------------------------------------------------------


def test_l2_veto_rules(tmp_path):
    store = UniverseStore(tmp_path / "u.db")
    open_indices(store)
    add_stock(store, "sz.300010", "正常公司", BOARD_GEM, last=60.0)
    add_stock(store, "sz.300011", "*ST海投", BOARD_GEM, last=60.0)
    add_stock(store, "sz.300012", "退市海润", BOARD_GEM, last=60.0)
    add_stock(store, "sh.688100", "未盈利-U", BOARD_STAR, last=60.0)
    add_stock(store, "sz.300013", "次新股", BOARD_GEM, last=60.0, ipo_date="2025-02-05")
    add_stock(store, "sz.300014", "涨停股", BOARD_GEM, last=57.0, prev_tail=50.0)
    add_stock(store, "sz.300015", "黑名单股", BOARD_GEM, last=60.0)

    result = run_screener(store, None, cfg(blacklist=["300015"]))
    assert result.funnel["after_l1"] == 7
    assert result.funnel["after_l2"] == 1
    assert [item.code for item in result.pool] == ["sz.300010"]
    assert result.first.code == "sz.300010"

    # 关闭排除开关后，ST/退市/-U/次新/涨停 不再被 L2 规则否决（黑名单仍生效）
    relaxed = run_screener(
        store,
        None,
        cfg(
            blacklist=["300015"],
            exclude_st=False,
            exclude_delisting=False,
            exclude_unprofitable_u=False,
            min_listing_sessions=0,
            day_change_max=1.0,
        ),
    )
    assert relaxed.funnel["after_l2"] == 6


# ---------------------------------------------------------------------------
# L3
# ---------------------------------------------------------------------------


def test_l3_amount_thresholds(tmp_path):
    store = UniverseStore(tmp_path / "u.db")
    open_indices(store)
    add_stock(store, "sz.300020", "双达标", BOARD_GEM, last=60.0)
    add_stock(
        store, "sz.300021", "当日缩量", BOARD_GEM, last=60.0,
        amounts=[DEFAULT_AMOUNT] * 39 + [1e8],
    )
    add_stock(
        store, "sz.300022", "持续缩量", BOARD_GEM, last=60.0,
        amounts=[1e8] * 40,
    )
    add_stock(store, "sz.300023", "双达标二", BOARD_GEM, last=60.0)

    result = run_screener(store, None, cfg())
    assert result.funnel["after_l2"] == 4
    assert result.funnel["after_l3"] == 2
    assert [item.code for item in result.pool] == ["sz.300020", "sz.300023"]
    assert result.pool[0].avg_amount_20d == pytest.approx(DEFAULT_AMOUNT)

    # 放宽门槛后 4 只全部通过
    loose = run_screener(store, None, cfg(min_amount=1e5, min_avg_amount_20d=1e5))
    assert loose.funnel["after_l3"] == 4


# ---------------------------------------------------------------------------
# L4
# ---------------------------------------------------------------------------


def test_l4_ordering_and_deep_veto(tmp_path, monkeypatch):
    store = UniverseStore(tmp_path / "u.db")
    open_indices(store)
    add_stock(store, "sz.300030", "跌幅最大", BOARD_GEM, last=60.0)  # -40%
    add_stock(store, "sz.300031", "跌幅居中", BOARD_GEM, last=65.0)  # -35%
    add_stock(store, "sz.300032", "跌幅最小", BOARD_GEM, last=70.0)  # -30%

    monkeypatch.setattr(
        screener.FundamentalService, "check", fake_fundamental(veto_codes={"sz.300030"})
    )
    result = run_screener(store, None, cfg(enable_fundamental_veto=True))

    assert [item.code for item in result.pool] == ["sz.300030", "sz.300031", "sz.300032"]
    assert [item.code for item in result.eligible] == ["sz.300031", "sz.300032"]
    assert result.first.code == "sz.300031"
    assert result.funnel["deep_checked"] == 3
    assert result.funnel["eligible"] == 2
    assert result.pool[0].vetoed_by == "亏损且营收<1亿"
    assert result.pool[0].deep_checked is True
    assert result.eligible[0].deep_checked is True
    assert result.data_info["fundamental"] == "applied"
    # 被深筛否决的票仍在 pool 里（供 UI 展示），但不在 eligible
    assert "sz.300030" not in [item.code for item in result.eligible]


def test_l4_deep_check_limit(tmp_path, monkeypatch):
    store = UniverseStore(tmp_path / "u.db")
    open_indices(store)
    add_stock(store, "sz.300030", "跌幅最大", BOARD_GEM, last=60.0)
    add_stock(store, "sz.300031", "跌幅居中", BOARD_GEM, last=65.0)

    fake = fake_fundamental(veto_codes={"sz.300030"})
    monkeypatch.setattr(screener.FundamentalService, "check", fake)
    result = run_screener(store, None, cfg(enable_fundamental_veto=True, max_deep_checks=1))

    assert fake.calls == ["sz.300030"]
    assert result.funnel["deep_checked"] == 1
    # 超出上限的票未被否决，保留为 eligible（deep_checked=False）
    assert [item.code for item in result.eligible] == ["sz.300031"]
    assert result.first.code == "sz.300031"
    assert result.pool[1].deep_checked is False
    assert any("深度财务校验仅覆盖前 1 只" in item for item in result.warnings)


def test_l4_unchecked_tail_keeps_eligible(tmp_path, monkeypatch):
    store = UniverseStore(tmp_path / "u.db")
    open_indices(store)
    add_stock(store, "sz.300030", "跌幅最大", BOARD_GEM, last=60.0)
    add_stock(store, "sz.300031", "跌幅居中", BOARD_GEM, last=65.0)
    add_stock(store, "sz.300032", "跌幅最小", BOARD_GEM, last=70.0)

    fake = fake_fundamental(veto_codes={"sz.300030"})
    monkeypatch.setattr(screener.FundamentalService, "check", fake)
    result = run_screener(store, None, cfg(enable_fundamental_veto=True, max_deep_checks=2))

    assert [item.code for item in result.eligible] == ["sz.300031", "sz.300032"]
    assert result.first.code == "sz.300031"
    assert result.pool[2].deep_checked is False
    assert result.funnel["deep_checked"] == 2
    assert result.funnel["eligible"] == 2


def test_l4_fundamental_unavailable_degrades(tmp_path, monkeypatch):
    store = UniverseStore(tmp_path / "u.db")
    open_indices(store)
    add_stock(store, "sz.300030", "财务缺失", BOARD_GEM, last=60.0)

    monkeypatch.setattr(
        screener.FundamentalService,
        "check",
        fake_fundamental(unavailable_codes={"sz.300030"}),
    )
    result = run_screener(store, None, cfg(enable_fundamental_veto=True))

    assert result.data_info["fundamental"] == "unavailable"
    assert result.first.code == "sz.300030"  # 数据不可用不否决
    assert any("财务数据不可用" in item for item in result.warnings)


# ---------------------------------------------------------------------------
# include_realtime
# ---------------------------------------------------------------------------


class FakeMarket:
    def __init__(self, quotes=None, error: Exception | None = None) -> None:
        self.quotes = quotes or {}
        self.error = error
        self.calls: list[list[str]] = []

    def fetch_quotes_batch(self, raw_symbols):
        self.calls.append(list(raw_symbols))
        if self.error is not None:
            raise self.error
        return self.quotes


def test_include_realtime_overlays_quotes(tmp_path):
    store = UniverseStore(tmp_path / "u.db")
    open_indices(store)
    add_stock(
        store, "sz.300040", "实时放量", BOARD_GEM, last=60.0,
        amounts=[DEFAULT_AMOUNT] * 39 + [1e8],
    )
    add_stock(store, "sz.300041", "实时大涨", BOARD_GEM, last=60.0)
    market = FakeMarket(
        quotes={
            "sz.300040": {
                "price": 60.0, "prev_close": 100.0, "amount": 6e8, "turnover_rate": 0.05,
            },
            "sz.300041": {
                "price": 114.0, "prev_close": 100.0, "amount": 6e8, "turnover_rate": 0.05,
            },
        }
    )

    daily_only = run_screener(store, None, cfg())
    assert daily_only.funnel["after_l3"] == 1  # 300040 当日/均额不足；300041 通过
    assert [item.code for item in daily_only.pool] == ["sz.300041"]

    result = run_screener(store, market, cfg(), include_realtime=True)
    assert market.calls == [["sz.300040", "sz.300041"]]  # 仅对 L1 命中的票拉快照
    assert result.data_info["realtime"] is True
    assert result.data_info["realtime_quotes"] == 2
    assert [item.code for item in result.pool] == ["sz.300040"]
    first = result.first
    assert first.code == "sz.300040"
    assert first.chg20d == pytest.approx(-0.4, abs=1e-9)
    assert first.amount == pytest.approx(6e8)
    assert first.turnover == pytest.approx(0.05)
    # 300041 用快照重算当日涨幅 14% → 被 L2 否决
    assert result.funnel["after_l2"] == 1


def test_include_realtime_failure_falls_back(tmp_path):
    import requests

    store = UniverseStore(tmp_path / "u.db")
    open_indices(store)
    add_stock(store, "sz.300040", "实时放量", BOARD_GEM, last=60.0, amounts=[1e8] * 40)
    market = FakeMarket(error=requests.RequestException("boom"))

    result = run_screener(store, market, cfg(), include_realtime=True)
    assert result.data_info["realtime"] is False
    assert result.data_info["realtime_quotes"] == 0
    assert any("实时快照拉取失败" in item for item in result.warnings)
    assert result.funnel["after_l3"] == 0  # 回落日线口径：当日成交额不足


# ---------------------------------------------------------------------------
# 配置 / 财务服务
# ---------------------------------------------------------------------------


def test_config_validation():
    default = ScreenerConfig()
    assert default.to_dict()["boards"] == [BOARD_GEM, BOARD_STAR]
    assert ScreenerConfig.from_dict(None).chg20_max == -0.15
    with pytest.raises(ValueError):
        ScreenerConfig.from_dict({"unknown_field": 1})
    with pytest.raises(ValueError):
        ScreenerConfig(boards=["沪市主板"])

    parsed = ScreenerConfig.from_dict({"chg20_max": -0.2, "boards": [BOARD_GEM], "blacklist": ["688200"]})
    assert parsed.chg20_max == -0.2
    assert parsed.boards == [BOARD_GEM]
    assert parsed.blacklist == ["688200"]


class FakeResult:
    def __init__(self, rows, error_code: str = "0") -> None:
        self._rows = list(rows)
        self._index = 0
        self.error_code = error_code

    def next(self) -> bool:
        if self._index >= len(self._rows):
            return False
        self._index += 1
        return True

    def get_row_data(self):
        return self._rows[self._index - 1]


class FakeFundamentalBaostock:
    def __init__(self, profit_rows: dict, balance_rows: dict | None = None, fail: bool = False) -> None:
        self.profit_rows = profit_rows
        self.balance_rows = balance_rows or {}
        self.fail = fail
        self.login_count = 0
        self.logout_count = 0

    def login(self):
        if self.fail:
            raise RuntimeError("login down")
        self.login_count += 1
        return FakeResult([])

    def logout(self):
        self.logout_count += 1
        return FakeResult([])

    def query_profit_data(self, code, year, quarter):
        return FakeResult(self.profit_rows.get(code, []))

    def query_balance_data(self, code, year, quarter):
        return FakeResult(self.balance_rows.get(code, []))


def test_fundamental_service_cache_and_veto(tmp_path, monkeypatch):
    store = UniverseStore(tmp_path / "u.db")
    profit_rows = {
        "sh.688200": [
            ["sh.688200", "2026-10-30", "2026-09-30", "0.1", "0.2", "0.3",
             "-50000000", "0.01", "80000000", "1000000", "900000"],
        ],
        "sz.300001": [
            ["sz.300001", "2026-10-30", "2026-09-30", "0.1", "0.2", "0.3",
             "-50000000", "0.01", "80000000", "1000000", "900000"],
        ],
    }
    balance_rows = {
        # liabilityToAsset=1.2 > 1 → 净资产为负
        "sh.688200": [
            ["sh.688200", "2026-10-30", "2026-09-30", "1.5", "1.2", "0.3",
             "0.1", "1.2", "-5.0"],
        ],
        # 净资产为正
        "sz.300001": [
            ["sz.300001", "2026-10-30", "2026-09-30", "1.5", "1.2", "0.3",
             "0.1", "0.2", "1.25"],
        ],
    }
    monkeypatch.setitem(
        sys.modules,
        "baostock",
        FakeFundamentalBaostock(profit_rows, balance_rows),
    )
    service = screener.FundamentalService(store)
    fake_bs = sys.modules["baostock"]

    negative = service.check("sh.688200")
    assert negative["checked"] is True
    assert negative["negative_equity"] is True
    assert negative["veto"] == "净资产为负"
    assert negative["source"] == "baostock"
    assert store.get_fundamentals("sh.688200") is not None

    loss = service.check("sz.300001")
    assert loss["checked"] is True
    assert loss["negative_equity"] is False
    assert loss["veto"] == "亏损且营收<1亿"

    cached = service.check("sh.688200")
    assert cached["source"] == "cached"
    assert cached["veto"] == "净资产为负"
    # 一个实例内只 login 一次，多次 check 复用会话
    assert fake_bs.login_count == 1
    service.close()
    assert fake_bs.logout_count == 1


def test_fundamental_service_unavailable_not_cached(tmp_path, monkeypatch):
    store = UniverseStore(tmp_path / "u.db")
    monkeypatch.setitem(
        sys.modules, "baostock", FakeFundamentalBaostock({}, fail=True)
    )
    info = screener.FundamentalService(store).check("sh.688200")
    assert info["checked"] is False
    assert info["source"] == "unavailable"
    assert store.get_fundamentals("sh.688200") is None  # 失败不缓存，下次重试


class PerQuarterBaostock(FakeFundamentalBaostock):
    """按 (code, year, quarter) 返回不同行，用于验证「最新有效一条」回退逻辑。"""

    def __init__(self, rows_by_quarter: dict) -> None:
        super().__init__({})
        self.rows_by_quarter = rows_by_quarter

    def query_profit_data(self, code, year, quarter):
        return FakeResult(self.rows_by_quarter.get((code, year, quarter), []))


def _quarter_stat_date(year: int, quarter: int) -> str:
    month = quarter * 3
    day = 31 if month in (3, 12) else 30
    return f"{year}-{month:02d}-{day}"


def test_fundamental_skips_incomplete_latest_quarter(tmp_path, monkeypatch):
    """最新季度 MBRevenue 缺失时要回退到上一季度，而不是整票降级。"""
    newest, previous = screener._recent_quarters(2)
    code = "sh.688200"
    rows_by_quarter = {
        (code, newest[0], newest[1]): [
            [code, _quarter_stat_date(newest[0], newest[1]),
             _quarter_stat_date(newest[0], newest[1]), "0.1", "0.2", "0.3",
             "-50000000", "0.01", "", "1000000", "900000"],
        ],
        (code, previous[0], previous[1]): [
            [code, _quarter_stat_date(previous[0], previous[1]),
             _quarter_stat_date(previous[0], previous[1]), "0.1", "0.2", "0.3",
             "-50000000", "0.01", "80000000", "1000000", "900000"],
        ],
    }
    monkeypatch.setitem(sys.modules, "baostock", PerQuarterBaostock(rows_by_quarter))
    store = UniverseStore(tmp_path / "u.db")
    info = screener.FundamentalService(store).check(code)

    assert info["checked"] is True
    assert info["net_profit"] == pytest.approx(-5e7)
    assert info["revenue"] == pytest.approx(8e7)
    assert info["veto"] == "亏损且营收<1亿"


def test_recent_quarters_are_descending():
    quarters = screener._recent_quarters(4)
    assert len(quarters) == 4
    indexes = [year * 4 + quarter for year, quarter in quarters]
    assert indexes == sorted(indexes, reverse=True)
    assert len(set(quarters)) == 4
