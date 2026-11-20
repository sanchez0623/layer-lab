"""回测报告「每月盈亏」统计：monthly_breakdown 口径 + 旧报告读取时补算。

覆盖：
- 自然月切分、首月以初始资金为基期、后续月份以上月末权益为基期；
- pnl / return_rate / trading_days / cumulative_return 口径；
- 月内最大回撤（峰值基期取进入当月时的权益，含「月初第一天就跌破上月末」）；
- 空曲线 / 脏日期兜底；
- performance_metrics 把 monthly 一并返回；
- 旧报告（metrics 无 monthly）GET 详情时按权益曲线补算。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.backtest import monthly_breakdown, performance_metrics
from app.main import create_app


def _point(date: str, equity: float) -> dict:
    return {
        "date": date, "equity": equity, "cash": 0.0, "market_value": 0.0,
        "invested": 0.0, "drawdown": 0.0, "position_qty": 0,
    }


def test_monthly_breakdown_splits_by_calendar_month():
    curve = [
        _point("2025-01-02", 100_000),
        _point("2025-01-31", 102_000),
        _point("2025-02-03", 101_000),
        _point("2025-02-14", 103_000),
        _point("2025-02-28", 99_000),
        _point("2025-03-03", 105_000),
    ]
    monthly = monthly_breakdown(curve, 100_000)

    assert [item["month"] for item in monthly] == ["2025-01", "2025-02", "2025-03"]
    january, february, march = monthly

    # 首月基期 = 初始资金。（无逆回购字段 → repo_income 为 0）
    assert january == {
        "month": "2025-01",
        "start_equity": 100_000,
        "end_equity": 102_000,
        "pnl": 2_000,
        "return_rate": pytest.approx(0.02),
        "trading_days": 2,
        "max_drawdown": 0.0,
        "cumulative_return": pytest.approx(0.02),
        "repo_income": 0.0,
    }
    # 后续月份基期 = 上月末权益，pnl = 当月末权益 - 上月末权益。
    assert february["start_equity"] == 102_000
    assert february["end_equity"] == 99_000
    assert february["pnl"] == -3_000
    assert february["return_rate"] == pytest.approx(-3_000 / 102_000)
    assert february["trading_days"] == 3
    assert february["cumulative_return"] == pytest.approx(-0.01)

    assert march["pnl"] == 6_000
    assert march["return_rate"] == pytest.approx(6_000 / 99_000)
    assert march["trading_days"] == 1
    assert march["cumulative_return"] == pytest.approx(0.05)


def test_monthly_drawdown_uses_prior_month_equity_as_peak():
    """月初第一天就跌破上月末：峰值基期必须含上月末权益，否则漏记这段回撤。"""
    curve = [
        _point("2025-01-31", 100_000),
        _point("2025-02-03", 90_000),
        _point("2025-02-28", 95_000),
    ]
    monthly = monthly_breakdown(curve, 100_000)
    assert monthly[1]["max_drawdown"] == pytest.approx(0.10)


def test_monthly_drawdown_uses_intra_month_peak():
    curve = [
        _point("2025-01-31", 100_000),
        _point("2025-02-10", 120_000),
        _point("2025-02-20", 108_000),
    ]
    monthly = monthly_breakdown(curve, 100_000)
    # 峰值 120,000 → 108,000，回撤 10%；1 月末 100,000 的峰值已被覆盖。
    assert monthly[1]["max_drawdown"] == pytest.approx(0.10)


def test_monthly_breakdown_empty_and_invalid_dates():
    assert monthly_breakdown([], 100_000) == []
    # 缺 date / 非 ISO 日期直接跳过，不让坏点污染月份归集。
    monthly = monthly_breakdown([
        {"equity": 100.0},
        _point("2025/02/03", 100.0),
        _point("2025-02-03", 110.0),
    ], 100.0)
    assert [item["month"] for item in monthly] == ["2025-02"]
    assert monthly[0]["pnl"] == 10.0


def test_monthly_breakdown_single_day_month_has_zero_drawdown():
    monthly = monthly_breakdown([_point("2025-01-06", 88_000)], 100_000)
    assert monthly == [{
        "month": "2025-01",
        "start_equity": 100_000,
        "end_equity": 88_000,
        "pnl": -12_000,
        "return_rate": pytest.approx(-0.12),
        "trading_days": 1,
        "max_drawdown": pytest.approx(0.12),
        "cumulative_return": pytest.approx(-0.12),
        "repo_income": 0.0,
    }]


def test_performance_metrics_includes_monthly():
    curve = [
        _point("2025-01-02", 100.0),
        _point("2025-01-31", 110.0),
        _point("2025-02-28", 99.0),
    ]
    metrics = performance_metrics(curve, 100.0, [], [], 0.0, 0.0, 80.0, 6.5, 0.08)
    assert [item["month"] for item in metrics["monthly"]] == ["2025-01", "2025-02"]
    assert metrics["monthly"][1]["pnl"] == -11.0


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    application = create_app()
    return application, TestClient(application)


def test_get_backtest_backfills_monthly_for_legacy_report(api):
    """旧报告 payload 没有 metrics.monthly：详情端点读取时补算，前端面板不用区分新旧。"""
    application, client = api
    application.state.storage.save_backtest({
        "id": "legacy-no-monthly",
        "name": "旧报告",
        "status": "COMPLETED",
        "created_at": "2026-11-13T00:00:00",
        "config": {"initial_cash": 100_000},
        "metrics": {"initial_cash": 100_000, "final_equity": 110_000},
        "equity_curve": [
            _point("2025-01-02", 100_000),
            _point("2025-01-31", 104_000),
            _point("2025-02-28", 110_000),
        ],
        "trades": [], "orders": [], "rounds": [], "events": [],
        "warnings": [], "data_info": {},
    })

    response = client.get("/api/backtests/legacy-no-monthly")
    assert response.status_code == 200, response.text
    monthly = response.json()["metrics"]["monthly"]
    assert [item["month"] for item in monthly] == ["2025-01", "2025-02"]
    assert monthly[0]["pnl"] == 4_000
    assert monthly[1]["pnl"] == 6_000
    assert monthly[1]["start_equity"] == 104_000


def test_get_backtest_keeps_stored_monthly(api):
    """新报告已落库 monthly：读取时原样返回，不被重复计算覆盖。"""
    application, client = api
    stored = [{"month": "2025-01", "pnl": 1.0}]
    application.state.storage.save_backtest({
        "id": "new-with-monthly",
        "name": "新报告",
        "status": "COMPLETED",
        "created_at": "2026-11-13T00:00:00",
        "config": {},
        "metrics": {"initial_cash": 100_000, "monthly": stored},
        "equity_curve": [_point("2025-01-02", 100_000), _point("2025-01-31", 101_000)],
        "trades": [], "orders": [], "rounds": [], "events": [],
        "warnings": [], "data_info": {},
    })

    response = client.get("/api/backtests/new-with-monthly")
    assert response.status_code == 200, response.text
    assert response.json()["metrics"]["monthly"] == stored
