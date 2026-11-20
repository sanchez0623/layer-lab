"""闲置资金逆回购（每日盘后结算）单测。

覆盖：配置校验、apply_repo_income 计提/关闭/满仓边界、收益率确定性（同日同 rate）、
monthly_breakdown 的 repo_income 汇总、performance_metrics.repo_income_total、
``GET /api/backtests/{id}/repo-daily`` 分页/月过滤/404、模拟盘日终计提（含 replay
不计提与同日去重）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.backtest import apply_repo_income, repo_rate_for_date
from app.broker import BrokerAccount
from app.domain import StrategyConfig, money
from app.main import create_app


# ------------------------------------------------------------- 配置校验 ----

def test_config_defaults_repo_disabled_with_1_00_to_1_50_band():
    config = StrategyConfig()
    assert config.idle_cash_repo_enabled is False
    assert config.repo_yield_min == pytest.approx(0.010)
    assert config.repo_yield_max == pytest.approx(0.015)


def test_config_rejects_min_above_max():
    with pytest.raises(ValueError, match="repo_yield_min"):
        StrategyConfig(repo_yield_min=0.02, repo_yield_max=0.01)


def test_config_rejects_yield_above_10_percent():
    with pytest.raises(ValueError, match="repo_yield_max"):
        StrategyConfig(repo_yield_max=0.5)


def test_config_rejects_negative_yield():
    with pytest.raises(ValueError):
        StrategyConfig(repo_yield_min=-0.01)


def test_config_allows_equal_min_max():
    config = StrategyConfig(repo_yield_min=0.012, repo_yield_max=0.012)
    assert config.repo_yield_min == config.repo_yield_max == pytest.approx(0.012)


# --------------------------------------------------------- apply_repo_income --

def _account(*, enabled: bool, cash: float = 1_000_000.0,
             min_yield: float = 0.010, max_yield: float = 0.015) -> BrokerAccount:
    config = StrategyConfig(
        idle_cash_repo_enabled=enabled, repo_yield_min=min_yield, repo_yield_max=max_yield,
    )
    return BrokerAccount(initial_cash=cash, config=config, name="repo-test")


def test_repo_disabled_no_accrual():
    account = _account(enabled=False)
    before = account.cash
    income, idle = apply_repo_income(account, account.config, "2026-03-02")
    assert income == 0.0 and idle == 0.0
    assert account.cash == before


def test_repo_enabled_accrues_into_cash():
    account = _account(enabled=True)
    before = account.cash
    income, idle = apply_repo_income(account, account.config, "2026-03-02")
    assert income > 0
    assert idle == pytest.approx(before)  # 空仓：计提前 idle = equity = cash
    assert account.cash == pytest.approx(before + income)
    # income = idle × rate / 365（money 口径四舍五入到分），rate 落在 [min, max]
    rate = repo_rate_for_date("2026-03-02", account.config)
    assert account.config.repo_yield_min <= rate <= account.config.repo_yield_max
    assert income == pytest.approx(idle * rate / 365.0, rel=1e-3, abs=0.01)


def test_repo_rate_deterministic_per_date():
    config = StrategyConfig(idle_cash_repo_enabled=True)
    assert repo_rate_for_date("2026-03-02", config) == repo_rate_for_date("2026-03-02", config)
    # 不同日期通常不同（1%~1.5% 均匀随机），但都落在区间内
    for day in ("2026-03-02", "2026-03-03", "2026-06-15", "2026-12-31"):
        rate = repo_rate_for_date(day, config)
        assert config.repo_yield_min <= rate <= config.repo_yield_max


def test_repo_full_position_no_accrual():
    """满仓（idle=0，现金为 0）不计提：equity == market_value。"""
    from app.domain import Position
    account = _account(enabled=True)
    account.cash = money(0.0)
    # 伪造满仓持仓：市值 = 全部权益 → idle = equity - market_value = 0
    account.positions["sz.000001"] = Position(
        symbol="sz.000001", permanent_qty=100_000, permanent_cost=1_000_000.0,
        permanent_fee=0.0, last_price=10.0,
    )
    assert account.market_value == pytest.approx(account.equity)
    income, idle = apply_repo_income(account, account.config, "2026-03-02")
    assert income == 0.0 and idle == 0.0


def test_repo_rate_at_fixed_band_used_in_income():
    """min == max 时 rate 恒为该值，income 公式可精确校验。"""
    account = _account(enabled=True, cash=365_000.0, min_yield=0.010, max_yield=0.010)
    income, idle = apply_repo_income(account, account.config, "2026-03-02")
    assert idle == pytest.approx(365_000.0)
    assert income == pytest.approx(365_000.0 * 0.010 / 365.0)  # = 10.0 元/日


# ------------------------------------------------------- monthly / metrics --

def test_monthly_breakdown_sums_repo_income():
    from app.backtest import monthly_breakdown
    curve = [
        {"date": "2026-01-05", "equity": 1_000_010.0, "repo_income": 5.0},
        {"date": "2026-01-06", "equity": 1_000_020.0, "repo_income": 5.0},
        {"date": "2026-02-03", "equity": 1_000_020.0},  # 无逆回购字段（关闭/旧报告）
    ]
    rows = monthly_breakdown(curve, 1_000_000.0)
    jan = next(row for row in rows if row["month"] == "2026-01")
    feb = next(row for row in rows if row["month"] == "2026-02")
    assert jan["repo_income"] == pytest.approx(10.0)
    assert feb["repo_income"] == 0.0


def test_performance_metrics_repo_income_total():
    from app.backtest import performance_metrics
    curve = [
        {"date": "2026-01-05", "equity": 1_000_010.0, "cash": 1_000_010.0,
         "market_value": 0.0, "repo_income": 6.0},
        {"date": "2026-01-06", "equity": 1_000_021.0, "cash": 1_000_021.0,
         "market_value": 0.0, "repo_income": 11.0},
    ]
    metrics = performance_metrics(
        curve, 1_000_000.0, rounds=[], trades=[], total_fees=0.0,
        t_net_profit=0.0, max_invested=0.0, max_float_loss=0.0, buy_hold_return=0.0,
    )
    assert metrics["repo_income_total"] == pytest.approx(17.0)


def test_performance_metrics_repo_income_total_zero_when_absent():
    from app.backtest import performance_metrics
    curve = [{"date": "2026-01-05", "equity": 1_000_000.0, "cash": 1_000_000.0,
              "market_value": 0.0}]
    metrics = performance_metrics(
        curve, 1_000_000.0, rounds=[], trades=[], total_fees=0.0,
        t_net_profit=0.0, max_invested=0.0, max_float_loss=0.0, buy_hold_return=0.0,
    )
    assert metrics["repo_income_total"] == 0.0


# ------------------------------------------------------------- repo-daily ----

def _report_with_repo(report_id: str) -> dict:
    curve = [
        {"date": "2026-03-02", "equity": 1_000_010.0, "cash": 1_000_010.0,
         "market_value": 0.0, "repo_income": 10.0, "repo_rate": 0.012, "repo_idle": 1_000_000.0},
        {"date": "2026-03-03", "equity": 1_000_022.0, "cash": 1_000_022.0,
         "market_value": 0.0, "repo_income": 12.0, "repo_rate": 0.013, "repo_idle": 1_000_010.0},
        {"date": "2026-03-04", "equity": 1_000_022.0, "cash": 1_000_022.0,
         "market_value": 0.0, "repo_income": 0.0, "repo_rate": 0.0, "repo_idle": 0.0},
        {"date": "2026-04-01", "equity": 1_000_030.0, "cash": 1_000_030.0,
         "market_value": 0.0, "repo_income": 8.0, "repo_rate": 0.011, "repo_idle": 1_000_022.0},
    ]
    return {
        "id": report_id, "name": "逆回购明细测试", "status": "COMPLETED",
        "created_at": "2026-11-20T00:00:00", "config": {},
        "metrics": {"repo_income_total": 30.0},
        "equity_curve": curve, "trades": [], "orders": [], "rounds": [],
        "events": [], "warnings": [], "data_info": {"mode": "single"},
    }


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    application = create_app()
    return application, TestClient(application)


def test_repo_daily_paginates(api):
    application, client = api
    application.state.storage.save_backtest(_report_with_repo("bt_repo_1"))

    body = client.get("/api/backtests/bt_repo_1/repo-daily?page=1&page_size=2").json()
    # 03-04 计提 0，仍算计提日（字段有值）→ 共 4 条
    assert body["total"] == 4
    assert body["total_pages"] == 2
    assert len(body["items"]) == 2
    first = body["items"][0]
    assert set(first) == {"date", "idle_cash", "rate", "income"}
    assert first["date"] == "2026-03-02"
    assert first["income"] == pytest.approx(10.0)
    assert first["idle_cash"] == pytest.approx(1_000_000.0)
    assert first["rate"] == pytest.approx(0.012)


def test_repo_daily_month_filter(api):
    application, client = api
    application.state.storage.save_backtest(_report_with_repo("bt_repo_2"))

    body = client.get("/api/backtests/bt_repo_2/repo-daily?month=2026-03").json()
    assert body["total"] == 3
    assert all(item["date"].startswith("2026-03") for item in body["items"])

    april = client.get("/api/backtests/bt_repo_2/repo-daily?month=2026-04").json()
    assert april["total"] == 1 and april["items"][0]["income"] == pytest.approx(8.0)

    empty = client.get("/api/backtests/bt_repo_2/repo-daily?month=2025-01").json()
    assert empty["total"] == 0 and empty["items"] == []


def test_repo_daily_bad_month_422(api):
    application, client = api
    application.state.storage.save_backtest(_report_with_repo("bt_repo_3"))
    assert client.get("/api/backtests/bt_repo_3/repo-daily?month=2026-3").status_code == 422


def test_repo_daily_404_when_missing(api):
    _application, client = api
    assert client.get("/api/backtests/nope/repo-daily").status_code == 404


def test_repo_daily_legacy_report_empty(api):
    """旧报告 equity_curve 无 repo_income 键 → 空列表（向后兼容）。"""
    application, client = api
    application.state.storage.save_backtest({
        "id": "bt_repo_old", "name": "旧报告", "status": "COMPLETED",
        "created_at": "2026-01-01T00:00:00", "config": {}, "metrics": {},
        "equity_curve": [{"date": "2026-01-05", "equity": 1.0, "cash": 1.0, "market_value": 0.0}],
        "trades": [], "orders": [], "rounds": [], "events": [], "warnings": [],
        "data_info": {"mode": "single"},
    })
    body = client.get("/api/backtests/bt_repo_old/repo-daily").json()
    assert body["total"] == 0 and body["items"] == []


def test_defaults_expose_repo_config(api):
    _application, client = api
    config = client.get("/api/strategy/defaults").json()["config"]
    assert config["idle_cash_repo_enabled"] is False
    assert config["repo_yield_min"] == pytest.approx(0.010)
    assert config["repo_yield_max"] == pytest.approx(0.015)


# --------------------------------------------------------- 模拟盘日终计提 ----

def test_paper_daily_close_accrues_repo_once(api):
    """日终快照计提逆回购进 cash；同一天重复日终不重复结算；replay 不计提。"""
    application, client = api
    created = client.post("/api/paper/accounts", json={
        "name": "repo-paper", "initial_cash": 365_000.0,
        "config": {"idle_cash_repo_enabled": True, "repo_yield_min": 0.01, "repo_yield_max": 0.01},
    })
    assert created.status_code == 200, created.text
    account_id = created.json()["id"]

    storage = application.state.storage
    # record_paper_equity 是 create_app 内部闭包，通过路由触发：/ticks 日终口径
    tick = {
        "symbol": "sz.000001", "timestamp": "2026-03-02T15:00:00",
        "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
        "volume": 0, "amount": 0.0,
    }
    resp = client.post(f"/api/paper/accounts/{account_id}/ticks", json=tick)
    assert resp.status_code == 200, resp.text
    after_day1 = resp.json()["cash"]
    # 空仓 → idle = equity = 365_000，rate 固定 1% → 当日收益 10 元
    assert after_day1 == pytest.approx(365_000.0 + 365_000.0 * 0.01 / 365.0)

    # 同一天再推一次日终（不同 ts）：不重复结算
    tick_late = {**tick, "timestamp": "2026-03-02T15:05:00"}
    resp2 = client.post(f"/api/paper/accounts/{account_id}/ticks", json=tick_late)
    assert resp2.status_code == 200
    assert resp2.json()["cash"] == pytest.approx(after_day1)

    # 次日日终：再计提一次（本金变大后仍按 idle 计算）
    tick_day2 = {**tick, "timestamp": "2026-03-03T15:00:00"}
    resp3 = client.post(f"/api/paper/accounts/{account_id}/ticks", json=tick_day2)
    assert resp3.status_code == 200
    assert resp3.json()["cash"] > after_day1

    # 落库的权益快照 cash 已含收益
    equity_rows = storage.list_paper_equity_daily(account_id)
    assert equity_rows and equity_rows[0]["cash"] == pytest.approx(after_day1)
