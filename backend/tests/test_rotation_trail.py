"""换票轨迹读时派生：轮次成果 / 空仓区间合并 / 汇总 / CSV 导出。

覆盖：
- funnel_bottleneck 各层；
- enrich_trail 从 rounds 关联收益率、持有交易日、建仓延迟、退出原因，旧数据优雅降级；
- collapse_waiting_spans 连续同因合并、门状态/瓶颈变化拆分、轮次打断、首尾区间；
- aggregate_trail 胜率/收益/空仓构成/利用率/最长空仓/退出原因；
- API：collapse_waiting 开关、summary、旧报告派生、404/422、CSV 导出。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.rotation_trail import (
    TRAIL_CSV_FIELDS,
    aggregate_trail,
    collapse_waiting_spans,
    enrich_trail,
    flatten_for_csv,
    funnel_bottleneck,
)


def test_funnel_bottleneck_reports_first_empty_stage():
    assert funnel_bottleneck({"after_l1": 5, "after_l2": 3, "after_l3": 1, "eligible": 1}) is None
    assert funnel_bottleneck({"after_l1": 0, "after_l2": 0, "after_l3": 0, "eligible": 0}) == "L1"
    assert funnel_bottleneck({"after_l1": 5, "after_l2": 0, "after_l3": 0, "eligible": 0}) == "L2"
    assert funnel_bottleneck({"after_l1": 5, "after_l2": 3, "after_l3": 0, "eligible": 0}) == "L3"
    assert funnel_bottleneck({"after_l1": 5, "after_l2": 3, "after_l3": 1, "eligible": 0}) == "L4"
    assert funnel_bottleneck(None) is None
    assert funnel_bottleneck({}) is None


def _waiting(select_date: str, *, gate_open: bool, funnel: dict | None = None) -> dict:
    return {
        "kind": "WAITING",
        "round": None, "symbol": None, "name": "", "board": "",
        "select_date": select_date, "entry_date": None, "exit_date": None,
        "pnl": None, "chg20d_at_select": None, "gate_open": gate_open,
        "status": "WAITING", "screen_warnings": [], "funnel": funnel or {},
    }


def _round(**overrides) -> dict:
    row = {
        "id": "R001", "symbol": "sz.300001", "start_date": "2025-01-03",
        "end_date": "2025-01-07", "status": "CLOSED", "pnl": 9500.0,
        "max_invested": 100000.0, "start_equity": 4_000_000.0,
        "close_reason": "整体止盈", "t_net_profit": 500.0,
    }
    row.update(overrides)
    return row


TRADING_DATES = [
    "2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08",
]


def test_enrich_trail_derives_round_outcome_from_rounds():
    trail = [{
        "round": "R001", "symbol": "sz.300001", "name": "测试A", "board": "创业板",
        "select_date": "2025-01-02", "entry_date": None, "exit_date": None,
        "pnl": None, "chg20d_at_select": -0.2, "gate_open": True,
        "status": "CLOSED", "screen_warnings": [], "funnel": {},
    }]
    enriched = enrich_trail(trail, [_round()], TRADING_DATES)
    item = enriched[0]

    assert item["kind"] == "ROUND"
    assert item["entry_date"] == "2025-01-03" and item["exit_date"] == "2025-01-07"
    assert item["pnl"] == 9500.0
    assert item["return_rate"] == pytest.approx(0.095)
    assert item["holding_days"] == 3          # 01-03 → 01-07，含首尾
    assert item["entry_delay_days"] == 1      # 01-02 选股 → 01-03 建仓
    assert item["close_reason"] == "整体止盈"
    assert item["t_net_profit"] == 500.0
    assert trail[0]["entry_date"] is None     # 不改入参


def test_enrich_trail_marks_waiting_reason_and_bottleneck():
    trail = [
        _waiting("2025-01-02", gate_open=False, funnel={"after_l1": 0}),
        _waiting("2025-01-03", gate_open=True, funnel={"after_l1": 5, "after_l2": 4, "after_l3": 0, "eligible": 0}),
    ]
    enriched = enrich_trail(trail, [], TRADING_DATES)
    assert enriched[0]["kind"] == "WAITING"
    assert enriched[0]["reason"] == "gate_closed"
    assert enriched[1]["reason"] == "no_candidate"
    assert enriched[1]["bottleneck"] == "L3"
    assert enriched[0]["holding_days"] is None


def test_enrich_trail_is_idempotent_and_tolerates_missing_round():
    trail = [
        _waiting("2025-01-02", gate_open=False),
        {"round": "R404", "symbol": "sz.300099", "name": "", "board": "",
         "select_date": "2025-01-06", "status": "CLOSED", "gate_open": True},
    ]
    once = enrich_trail(trail, [], TRADING_DATES)
    twice = enrich_trail(once, [], TRADING_DATES)
    assert once == twice
    assert once[1]["return_rate"] is None
    assert once[1]["close_reason"] is None


def test_collapse_waiting_spans_merges_and_splits():
    trail = [
        _waiting("2025-01-02", gate_open=False),
        _waiting("2025-01-03", gate_open=False),
        _waiting("2025-01-06", gate_open=False),
        {"round": "R001", "symbol": "sz.300001", "name": "A", "board": "创业板",
         "select_date": "2025-01-07", "status": "CLOSED", "gate_open": True},
        _waiting("2025-01-08", gate_open=True, funnel={"after_l1": 5, "after_l2": 0, "after_l3": 0}),
        _waiting("2025-01-09", gate_open=True, funnel={"after_l1": 5, "after_l2": 4, "after_l3": 0}),
        _waiting("2025-01-10", gate_open=False),
    ]
    enriched = collapse_waiting_spans(enrich_trail(trail, [], []))
    kinds = [item["kind"] for item in enriched]
    assert kinds == ["WAITING_SPAN", "ROUND", "WAITING_SPAN", "WAITING_SPAN", "WAITING_SPAN"]

    first = enriched[0]
    assert first["start_date"] == "2025-01-02" and first["end_date"] == "2025-01-06"
    assert first["days"] == 3 and first["reason"] == "gate_closed"
    assert enriched[1]["symbol"] == "sz.300001"
    # L2 瓶颈的区间与紧随其后的 L3 区间不合并（信号变化要保留）。
    assert enriched[2]["bottleneck"] == "L2" and enriched[2]["days"] == 1
    assert enriched[3]["bottleneck"] == "L3" and enriched[3]["days"] == 1
    assert enriched[4]["days"] == 1


def test_collapse_waiting_spans_dedupes_warnings_and_keeps_last_funnel():
    trail = [
        {**_waiting("2025-01-02", gate_open=False, funnel={"after_l1": 0}), "screen_warnings": ["A", "B"]},
        {**_waiting("2025-01-03", gate_open=False, funnel={"after_l1": 0}), "screen_warnings": ["B", "C"]},
    ]
    spans = collapse_waiting_spans(enrich_trail(trail, [], []))
    assert spans[0]["screen_warnings"] == ["A", "B", "C"]
    assert spans[0]["funnel"] == {"after_l1": 0}


def test_aggregate_trail_summarises_rounds_and_waiting():
    items = [
        _waiting("2025-01-02", gate_open=False),
        _waiting("2025-01-03", gate_open=False),
        _waiting("2025-01-06", gate_open=False),
        _waiting("2025-01-07", gate_open=True, funnel={"after_l1": 5, "after_l2": 4, "after_l3": 0}),
        {"kind": "ROUND", "status": "CLOSED", "round": "R001", "symbol": "sz.300001", "name": "A",
         "pnl": 100.0, "return_rate": 0.10, "holding_days": 5, "entry_delay_days": 1,
         "close_reason": "整体止盈"},
        {"kind": "ROUND", "status": "CLOSED", "round": "R002", "symbol": "sz.300002", "name": "B",
         "pnl": -50.0, "return_rate": -0.05, "holding_days": 3, "entry_delay_days": 2,
         "close_reason": "轮动换票"},
        {"kind": "ROUND", "status": "CLOSED", "round": "R003", "symbol": "sz.300003", "name": "C",
         "pnl": 200.0, "return_rate": 0.20, "holding_days": 10, "entry_delay_days": 3,
         "close_reason": "整体止盈"},
        {"kind": "ROUND", "status": "OPEN", "round": None, "symbol": "sz.300004", "name": "D",
         "pnl": None, "return_rate": None, "holding_days": None, "entry_delay_days": 2},
    ]
    summary = aggregate_trail(items, trading_days=100)

    assert summary["screens"] == 8
    assert summary["rounds"] == 4 and summary["closed_rounds"] == 3
    assert summary["open_rounds"] == 1
    assert summary["waiting_days"] == 4
    assert summary["gate_closed_days"] == 3
    assert summary["no_candidate_days"] == 1
    assert summary["no_candidate_by_stage"] == {"L3": 1}
    assert summary["waiting_ratio"] == pytest.approx(0.5)
    assert summary["holding_days"] == 18
    assert summary["holding_ratio"] == pytest.approx(0.18)
    assert summary["win_rate"] == pytest.approx(2 / 3)
    assert summary["avg_return"] == pytest.approx((0.10 - 0.05 + 0.20) / 3)
    assert summary["total_pnl"] == 250.0
    assert summary["avg_pnl"] == pytest.approx(250 / 3, abs=0.01)
    assert summary["avg_holding_days"] == pytest.approx(6.0)
    assert summary["avg_entry_delay_days"] == pytest.approx(2.0)
    assert summary["best"]["symbol"] == "sz.300003" and summary["worst"]["symbol"] == "sz.300002"
    assert summary["longest_wait"]["days"] == 3
    assert summary["longest_wait"]["start_date"] == "2025-01-02"
    assert summary["exit_reasons"] == {"整体止盈": 2, "轮动换票": 1}


def test_aggregate_trail_on_empty_items():
    summary = aggregate_trail([], trading_days=0)
    assert summary["screens"] == 0
    assert summary["win_rate"] == 0.0
    assert summary["avg_return"] is None
    assert summary["longest_wait"] is None
    assert summary["best"] is None and summary["worst"] is None


# ------------------------------------------------------------------ BETA ----

def _beta(select_date: str, *, gate_open: bool = True, beta_symbol: str = "sz.159915") -> dict:
    return {
        "round": None, "symbol": None, "name": "", "board": "",
        "select_date": select_date, "entry_date": None, "exit_date": None,
        "pnl": None, "chg20d_at_select": None, "gate_open": gate_open,
        "status": "BETA", "screen_warnings": [], "funnel": {},
        "beta_symbol": beta_symbol,
    }


def test_enrich_trail_marks_beta_kind_and_holding_days():
    trail = [_beta("2025-01-02")]
    enriched = enrich_trail(trail, [], TRADING_DATES)
    assert enriched[0]["kind"] == "BETA"
    assert enriched[0]["holding_days"] == 1
    assert enriched[0]["return_rate"] is None
    assert trail[0]["status"] == "BETA"  # 不改入参


def test_collapse_beta_breaks_waiting_and_merges_beta_spans():
    trail = [
        _waiting("2025-01-02", gate_open=False),
        _beta("2025-01-03"),
        _beta("2025-01-06"),
        _waiting("2025-01-07", gate_open=False),
        {"round": "R001", "symbol": "sz.300001", "name": "A", "board": "创业板",
         "select_date": "2025-01-08", "status": "CLOSED", "gate_open": True},
    ]
    collapsed = collapse_waiting_spans(enrich_trail(trail, [], TRADING_DATES))
    kinds = [item["kind"] for item in collapsed]
    # beta 打断空仓；连续 beta 合并成区间；轮次独立。
    assert kinds == ["WAITING_SPAN", "BETA_SPAN", "WAITING_SPAN", "ROUND"]
    beta_span = collapsed[1]
    assert beta_span["start_date"] == "2025-01-03" and beta_span["end_date"] == "2025-01-06"
    assert beta_span["days"] == 2
    assert beta_span["beta_symbol"] == "sz.159915"
    assert beta_span["holding_days"] == 2
    # 空仓区间不跨 beta 合并：01-02 与 01-07 各自独立。
    assert collapsed[0]["days"] == 1 and collapsed[2]["days"] == 1
    assert collapsed[3]["symbol"] == "sz.300001"


def test_collapse_beta_span_splits_on_symbol_change():
    trail = [
        _beta("2025-01-02", beta_symbol="sz.159915"),
        _beta("2025-01-03", beta_symbol="sh.510300"),
        _beta("2025-01-06", beta_symbol="sh.510300"),
    ]
    collapsed = collapse_waiting_spans(enrich_trail(trail, [], TRADING_DATES))
    assert [item["kind"] for item in collapsed] == ["BETA_SPAN", "BETA_SPAN"]
    assert collapsed[0]["beta_symbol"] == "sz.159915" and collapsed[0]["days"] == 1
    assert collapsed[1]["beta_symbol"] == "sh.510300" and collapsed[1]["days"] == 2


def test_aggregate_trail_excludes_beta_from_waiting_and_counts_covered():
    trail = [
        _waiting("2025-01-02", gate_open=False),
        _beta("2025-01-03"),
        _beta("2025-01-06"),
        _waiting("2025-01-07", gate_open=False),
    ]
    enriched = enrich_trail(trail, [], TRADING_DATES)
    summary = aggregate_trail(enriched, trading_days=len(TRADING_DATES))

    assert summary["screens"] == 4
    assert summary["waiting_days"] == 2            # beta 日不算空仓
    assert summary["beta_holding_days"] == 2
    assert summary["waiting_ratio"] == pytest.approx(2 / 4)
    assert summary["covered_days"] == 2            # 无轮次持仓 + 2 天 beta
    assert summary["covered_ratio"] == pytest.approx(2 / 5)


def test_aggregate_trail_beta_pnl_from_beta_param_and_items():
    trail = [_beta("2025-01-02"), _beta("2025-01-03")]
    enriched = enrich_trail(trail, [], TRADING_DATES)
    from_beta = aggregate_trail(enriched, trading_days=5, beta={"pnl": 1234.5})
    assert from_beta["beta_pnl"] == 1234.5
    # 未传 beta 汇总时回退逐日项上的 beta_pnl。
    enriched[0]["beta_pnl"] = 500.0
    enriched[1]["beta_pnl"] = 300.0
    from_items = aggregate_trail(enriched, trading_days=5)
    assert from_items["beta_pnl"] == 800.0


def test_flatten_for_csv_has_fixed_columns():
    trail = [_waiting("2025-01-02", gate_open=False, funnel={"after_l0": 0})]
    rows = flatten_for_csv(enrich_trail(trail, [], []))
    assert len(rows) == 1
    assert list(rows[0]) == TRAIL_CSV_FIELDS
    assert rows[0]["gate_open"] == "否"
    assert rows[0]["after_l0"] == 0


# ------------------------------------------------------------------ API ----

REPORT_ID = "bt-trail"
TRADING_DATES_API = [
    "2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08",
    "2025-01-09", "2025-01-10", "2025-01-13", "2025-01-14", "2025-01-15",
    "2025-01-16", "2025-01-17", "2025-01-20",
]


def _report() -> dict:
    trail = [
        _waiting("2025-01-02", gate_open=False),
        _waiting("2025-01-03", gate_open=False),
        {"round": "R001", "symbol": "sz.300001", "name": "测试A", "board": "创业板",
         "select_date": "2025-01-06", "entry_date": None, "exit_date": None,
         "pnl": None, "chg20d_at_select": -0.2, "gate_open": True,
         "status": "CLOSED", "screen_warnings": [], "funnel": {}},
        {"round": None, "symbol": "sz.300002", "name": "测试B", "board": "创业板",
         "select_date": "2025-01-07", "entry_date": None, "exit_date": None,
         "pnl": None, "chg20d_at_select": -0.18, "gate_open": True,
         "status": "OPEN", "screen_warnings": [], "funnel": {}},
        _waiting("2025-01-08", gate_open=False),
    ]
    rounds = [{
        "id": "R001", "symbol": "sz.300001", "start_date": "2025-01-07",
        "end_date": "2025-01-20", "status": "CLOSED", "pnl": 1234.5,
        "max_invested": 100000.0, "start_equity": 4_000_000.0,
        "close_reason": "整体止盈", "t_net_profit": 50.0,
    }]
    return {
        "id": REPORT_ID, "name": "换票轨迹测试", "status": "COMPLETED",
        "created_at": "2026-11-13T00:00:00", "config": {}, "metrics": {},
        "equity_curve": [{"date": day, "equity": 4_000_000.0} for day in TRADING_DATES_API],
        "trades": [], "orders": [], "rounds": rounds, "events": [], "warnings": [],
        "data_info": {
            "mode": "funnel_rotation", "screening_count": len(trail),
            "rotation_trail": trail,
        },
    }


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    application = create_app()
    return application, TestClient(application)


def test_rotation_trail_collapses_waiting_and_returns_summary(api):
    application, client = api
    application.state.storage.save_backtest(_report())

    collapsed = client.get(
        f"/api/backtests/{REPORT_ID}/rotation-trail?page=1&page_size=20&collapse_waiting=true"
    )
    assert collapsed.status_code == 200, collapsed.text
    body = collapsed.json()
    assert body["total"] == 4                      # 2 段空仓 + 1 轮次 + 1 未建仓
    assert [item["kind"] for item in body["items"]] == ["WAITING_SPAN", "ROUND", "ROUND", "WAITING_SPAN"]
    span = body["items"][0]
    assert span["start_date"] == "2025-01-02" and span["end_date"] == "2025-01-03"
    assert span["days"] == 2 and span["reason"] == "gate_closed"

    summary = body["summary"]
    assert summary["screens"] == 5
    assert summary["rounds"] == 2 and summary["closed_rounds"] == 1
    assert summary["waiting_days"] == 3
    assert summary["gate_closed_days"] == 3
    assert summary["win_rate"] == 1.0
    assert summary["total_pnl"] == 1234.5
    assert summary["holding_days"] == 10           # 01-07 → 01-20，含首尾
    assert summary["avg_entry_delay_days"] == pytest.approx(1.0)


def test_rotation_trail_default_returns_raw_daily_rows(api):
    application, client = api
    application.state.storage.save_backtest(_report())

    body = client.get(f"/api/backtests/{REPORT_ID}/rotation-trail?page=1&page_size=20").json()
    assert body["total"] == 5
    assert body["items"][0]["kind"] == "WAITING"
    assert body["items"][2]["kind"] == "ROUND"
    # 旧报告没有落库收益率：读取时从 rounds 派生。
    assert body["items"][2]["return_rate"] == pytest.approx(1234.5 / 100000.0)
    assert body["items"][2]["close_reason"] == "整体止盈"


def test_rotation_trail_pagination_over_collapsed_rows(api):
    application, client = api
    application.state.storage.save_backtest(_report())

    page = client.get(
        f"/api/backtests/{REPORT_ID}/rotation-trail?page=2&page_size=3&collapse_waiting=true"
    ).json()
    assert page["total"] == 4 and page["total_pages"] == 2
    assert page["page"] == 2
    assert len(page["items"]) == 1
    assert page["has_next"] is False

    assert client.get(
        f"/api/backtests/{REPORT_ID}/rotation-trail?page=0&collapse_waiting=true"
    ).status_code == 422
    assert client.get("/api/backtests/nope/rotation-trail").status_code == 404


def test_export_trail_csv(api):
    application, client = api
    application.state.storage.save_backtest(_report())

    response = client.get(f"/api/backtests/{REPORT_ID}/export?kind=trail")
    assert response.status_code == 200, response.text
    assert "text/csv" in response.headers["content-type"]
    text = response.content.decode("utf-8-sig")
    lines = [line for line in text.splitlines() if line]
    assert lines[0].split(",")[0] == "kind"
    assert len(lines) == 5 + 1                     # 原始逐日 5 条 + 表头
    assert "ROUND" in text and "WAITING" in text


def test_export_trades_still_works(api):
    application, client = api
    application.state.storage.save_backtest(_report())
    response = client.get(f"/api/backtests/{REPORT_ID}/export?kind=trades")
    assert response.status_code == 200


# ------------------------------------------------------------------ BETA API ----

def _report_with_beta() -> dict:
    trail = [
        _waiting("2025-01-02", gate_open=False),
        _beta("2025-01-03"),
        _beta("2025-01-06"),
        _waiting("2025-01-07", gate_open=False),
    ]
    return {
        "id": "bt-trail-beta", "name": "换票轨迹 beta 测试", "status": "COMPLETED",
        "created_at": "2026-11-13T00:00:00", "config": {}, "metrics": {},
        "equity_curve": [{"date": day, "equity": 4_000_000.0} for day in TRADING_DATES_API],
        "trades": [], "orders": [], "rounds": [], "events": [], "warnings": [],
        "data_info": {
            "mode": "funnel_rotation", "screening_count": len(trail),
            "rotation_trail": trail,
            "beta": {"enabled": True, "symbol": "sz.159915", "pnl": 888.0,
                     "last_exit_date": "2025-01-06"},
        },
    }


def test_rotation_trail_beta_kind_flows_through_endpoint(api):
    application, client = api
    application.state.storage.save_backtest(_report_with_beta())

    raw = client.get(f"/api/backtests/bt-trail-beta/rotation-trail?page=1&page_size=20").json()
    assert raw["total"] == 4
    assert [item["kind"] for item in raw["items"]] == ["WAITING", "BETA", "BETA", "WAITING"]
    assert raw["items"][1]["beta_symbol"] == "sz.159915"

    collapsed = client.get(
        f"/api/backtests/bt-trail-beta/rotation-trail?page=1&page_size=20&collapse_waiting=true"
    ).json()
    assert [item["kind"] for item in collapsed["items"]] == ["WAITING_SPAN", "BETA_SPAN", "WAITING_SPAN"]
    assert collapsed["items"][1]["days"] == 2

    summary = collapsed["summary"]
    assert summary["beta_holding_days"] == 2
    assert summary["beta_pnl"] == 888.0
    assert summary["waiting_days"] == 2
    assert summary["covered_days"] == 2


def test_export_trail_csv_includes_beta_kind(api):
    application, client = api
    application.state.storage.save_backtest(_report_with_beta())

    response = client.get("/api/backtests/bt-trail-beta/export?kind=trail")
    assert response.status_code == 200, response.text
    text = response.content.decode("utf-8-sig")
    lines = [line for line in text.splitlines() if line]
    assert "BETA" in text
    assert "beta_symbol" in lines[0]
    assert "sz.159915" in text


# -------------------------------------------------- BETA_SPAN 成果字段 ----

def _report_with_beta_fills() -> dict:
    """beta 腿一段完整持仓：买入 2 笔建仓、卖出 1 笔清仓（含已实现盈亏）。"""
    trail = [
        _waiting("2025-01-02", gate_open=False),
        _beta("2025-01-03"),
        _beta("2025-01-06"),
        _waiting("2025-01-07", gate_open=False),
    ]
    return {
        "id": "bt-trail-beta-fills", "name": "换票轨迹 beta 成果测试", "status": "COMPLETED",
        "created_at": "2026-11-18T00:00:00", "config": {}, "metrics": {},
        "equity_curve": [{"date": day, "equity": 4_000_000.0} for day in TRADING_DATES_API],
        "trades": [], "orders": [], "rounds": [], "events": [], "warnings": [],
        "data_info": {
            "mode": "funnel_rotation", "screening_count": len(trail),
            "rotation_trail": trail,
            "beta": {
                "enabled": True, "symbol": "sz.159915", "pnl": 6000.0,
                "fills": [
                    {"date": "2025-01-03", "side": "BUY", "quantity": 100_000,
                     "price": 1.0, "fee": 50.0, "realized_pnl": 0.0},
                    {"date": "2025-01-06", "side": "BUY", "quantity": 100_000,
                     "price": 1.1, "fee": 55.0, "realized_pnl": 0.0},
                    {"date": "2025-01-06", "side": "SELL", "quantity": 200_000,
                     "price": 1.2, "fee": 120.0, "realized_pnl": 6000.0},
                ],
            },
        },
    }


def test_beta_span_carries_holding_window_days_and_pnl(api):
    """BETA_SPAN 区间行补齐四列：持仓区间 / 持有交易日 / 本轮收益率 / 净盈亏。"""
    application, client = api
    application.state.storage.save_backtest(_report_with_beta_fills())

    body = client.get(
        f"/api/backtests/bt-trail-beta-fills/rotation-trail?page=1&page_size=20&collapse_waiting=true"
    ).json()
    spans = [item for item in body["items"] if item["kind"] == "BETA_SPAN"]
    assert len(spans) == 1
    span = spans[0]

    # 持仓区间 + 持有交易日（01-03 → 01-06，含首尾）。
    assert span["start_date"] == "2025-01-03" and span["end_date"] == "2025-01-06"
    assert span["holding_days"] == 2
    # 净盈亏 = 区间内卖出成交的已实现盈亏之和。
    assert span["pnl"] == pytest.approx(6000.0)
    # 本轮收益率 = 净盈亏 / 区间内买入成本（含费），资金效率口径。
    invested = 100_000 * 1.0 + 100_000 * 1.1
    assert span["return_rate"] == pytest.approx(6000.0 / invested)
    assert span["beta_invested"] == pytest.approx(invested)
    assert span["beta_buy_count"] == 1 + 1 and span["beta_sell_count"] == 1


def test_beta_span_without_fills_falls_back_to_summary_pnl(api):
    """旧报告无逐笔 fills：优雅降级，整腿汇总 pnl 只挂在 beta 实际清仓的那一段。"""
    application, client = api
    application.state.storage.save_backtest(_report_with_beta())

    body = client.get(
        f"/api/backtests/bt-trail-beta/rotation-trail?page=1&page_size=20&collapse_waiting=true"
    ).json()
    spans = [item for item in body["items"] if item["kind"] == "BETA_SPAN"]
    assert len(spans) == 1
    # 单区间且含 last_exit_date（01-06）→ 汇总 pnl 挂上。
    assert spans[0]["pnl"] == pytest.approx(888.0)
    assert spans[0]["return_rate"] is None  # 无投入分母，收益率留空


def test_beta_span_without_fills_does_not_duplicate_pnl_across_spans(api):
    """回归：旧报告多段持仓、无 fills 时，整腿 pnl 不得复制到每一段。

    只有包含 last_exit_date 的那一段（beta 实际清仓处）才挂汇总 pnl，
    其余段留空——否则会让人误以为每段都赚了整腿那么多。
    """
    trail = [
        _beta("2025-01-03"),
        _beta("2025-01-06"),
        _waiting("2025-01-07", gate_open=False),
        _beta("2025-02-03"),
        _beta("2025-02-04"),
    ]
    beta = {"enabled": True, "symbol": "sz.159915", "pnl": 888.0,
            "last_exit_date": "2025-02-04"}
    collapsed = collapse_waiting_spans(enrich_trail(trail, [], TRADING_DATES + [
        "2025-02-03", "2025-02-04"]), beta=beta)
    spans = [item for item in collapsed if item["kind"] == "BETA_SPAN"]
    assert len(spans) == 2
    pnls = [span["pnl"] for span in spans]
    # 第一段（01-03→01-06）不含 last_exit_date → 留空；第二段（含 02-04）→ 挂 pnl。
    assert pnls[0] is None
    assert pnls[1] == pytest.approx(888.0)


def test_beta_span_without_beta_info_leaves_pnl_none(api):
    """beta 未启用 / 无汇总：pnl 为 None，不抛错。"""
    trail = [_beta("2025-01-03"), _beta("2025-01-06")]
    collapsed = collapse_waiting_spans(enrich_trail(trail, [], TRADING_DATES), beta=None)
    spans = [item for item in collapsed if item["kind"] == "BETA_SPAN"]
    assert len(spans) == 1
    assert spans[0]["pnl"] is None
    assert spans[0]["holding_days"] == 2  # 持仓交易日照常
