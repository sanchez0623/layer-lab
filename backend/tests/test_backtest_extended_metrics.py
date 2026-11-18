"""回测报告扩展指标单测：资金加权年化（XIRR）/ 净值水下时长 / 稳健性版本。

覆盖：
- ``xirr_annualized``：单笔投入 + 期末回流（与时间加权年化一致）、中途出金回流
  （出金抬高 XIRR）、无符号变化现金流返回 None、日期不足两条返回 None；
- ``underwater_stats``：峰→谷→收复的最长水下区间（交易日 + 自然日）、全程水下、
  从未水下、期末仍处水下、空曲线兜底；
- ``performance_metrics`` 三个新键一并返回：``xirr_annualized`` / ``robustness`` /
  ``underwater``，稳健性版本剔除最佳自然月 + 最大 3 笔盈利轮次后重算；
- 旧报告（metrics 缺三键）GET 详情时按权益曲线补算。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.backtest import performance_metrics, underwater_stats, xirr_annualized
from app.main import create_app


def _point(date: str, equity: float) -> dict:
    return {
        "date": date, "equity": equity, "cash": 0.0, "market_value": 0.0,
        "invested": 0.0, "drawdown": 0.0, "position_qty": 0,
    }


def _round(rid: str, pnl: float, status: str = "CLOSED") -> dict:
    return {"id": rid, "status": status, "pnl": pnl}


# ------------------------------------------------------------- XIRR ----

def test_xirr_single_inflow_matches_time_weighted_annualized():
    """单笔期初投入 + 期末一次回流：XIRR 与两点时间加权年化完全一致。"""
    # 100 万 → 一年后 110 万：年化 10%。
    flows = [("2025-01-01", -1_000_000.0), ("2026-01-01", 1_100_000.0)]
    assert xirr_annualized(flows) == pytest.approx(0.10, abs=1e-6)


def test_xirr_with_interim_withdrawal_exceeds_time_weighted():
    """中途盈利出金回流：XIRR 高于只看期初期末的时间加权口径。

    期初投 100 万，半年后出金 20 万（盈利落袋），年末权益 90 万（另有 20 万已离场）。
    时间加权只看 (90+? ) 会低估；XIRR 把 20 万按半年折现，资金实际回报更高。
    """
    flows = [
        ("2025-01-01", -1_000_000.0),
        ("2025-07-01", 200_000.0),
        ("2026-01-01", 900_000.0),
    ]
    xirr = xirr_annualized(flows)
    time_weighted = 900_000.0 / 1_000_000.0 - 1  # 只看期末权益会误判为亏损
    assert xirr is not None
    assert xirr > time_weighted
    # 总回流 110 万 > 投入 100 万，XIRR 必为正。
    assert xirr > 0


def test_xirr_returns_none_without_sign_change():
    """现金流无符号变化（全正或全负）：无根，返回 None 让前端降级隐藏。"""
    assert xirr_annualized([("2025-01-01", 100.0), ("2025-06-01", 200.0)]) is None
    assert xirr_annualized([("2025-01-01", -100.0), ("2025-06-01", -50.0)]) is None


def test_xirr_returns_none_for_fewer_than_two_points():
    assert xirr_annualized([("2025-01-01", -100.0)]) is None
    assert xirr_annualized([]) is None


def test_xirr_negative_return_is_negative():
    """亏损账户：期末回流低于投入，XIRR 为负。"""
    flows = [("2025-01-01", -1_000_000.0), ("2026-01-01", 800_000.0)]
    assert xirr_annualized(flows) == pytest.approx(-0.20, abs=1e-6)


# -------------------------------------------------------- underwater ----

def test_underwater_stats_measures_longest_peak_to_recovery():
    """经典水下区间：涨→跌破峰值→持续水下→收复，统计最长那一段。"""
    curve = [
        _point("2025-01-02", 100.0),  # 峰值起点
        _point("2025-01-03", 110.0),  # 新高
        _point("2025-01-06", 99.0),   # 跌破峰值 → 水下第 1 天
        _point("2025-01-07", 95.0),   # 水下第 2 天
        _point("2025-01-08", 105.0),  # 收复 110? 没有 → 仍水下
        _point("2025-01-09", 111.0),  # 创新高 → 水下结束
        _point("2025-01-10", 112.0),
    ]
    stats = underwater_stats(curve)
    # 01-06/07/08 三天水下（01-09 收复）。
    assert stats["longest_underwater_days"] == 3
    assert stats["longest_underwater_start"] == "2025-01-06"
    assert stats["longest_underwater_end"] == "2025-01-09"
    assert stats["longest_underwater_calendar_days"] == 3
    assert stats["underwater_days"] == 3
    assert stats["underwater_ratio"] == pytest.approx(3 / 7)
    assert stats["currently_underwater"] is False
    assert stats["current_drawdown_days"] == 0


def test_underwater_stats_still_underwater_at_end():
    """期末仍处水下：currently_underwater=True，末段计为当前回撤天数。"""
    curve = [
        _point("2025-01-02", 100.0),
        _point("2025-01-03", 120.0),  # 峰值
        _point("2025-01-06", 110.0),  # 水下
        _point("2025-01-07", 105.0),  # 水下
    ]
    stats = underwater_stats(curve)
    assert stats["currently_underwater"] is True
    assert stats["current_drawdown_days"] == 2
    assert stats["longest_underwater_days"] == 2
    assert stats["longest_underwater_end"] == "2025-01-07"


def test_underwater_stats_never_underwater():
    """全程创新高：从未水下，所有计数为 0。"""
    curve = [_point("2025-01-02", 100.0), _point("2025-01-03", 110.0), _point("2025-01-06", 120.0)]
    stats = underwater_stats(curve)
    assert stats["longest_underwater_days"] == 0
    assert stats["underwater_days"] == 0
    assert stats["currently_underwater"] is False
    assert stats["underwater_ratio"] == 0.0


def test_underwater_stats_empty_curve():
    stats = underwater_stats([])
    assert stats["longest_underwater_days"] == 0
    assert stats["currently_underwater"] is False


# ------------------------------------------------------ robustness ----

def _metrics(curve: list[dict], rounds: list[dict], *, initial: float = 1_000_000.0) -> dict:
    return performance_metrics(
        curve, initial, rounds, [], total_fees=0.0, t_net_profit=0.0,
        max_invested=initial, max_float_loss=0.0, buy_hold_return=0.0,
    )


def test_performance_metrics_returns_extended_keys():
    curve = [_point("2025-01-02", 1_000_000.0), _point("2025-06-30", 1_100_000.0), _point("2025-12-31", 1_200_000.0)]
    rounds = [_round("R001", 60_000.0), _round("R002", 40_000.0), _round("R003", -10_000.0)]
    metrics = _metrics(curve, rounds)

    assert "xirr_annualized" in metrics
    assert metrics["xirr_annualized"] is not None
    assert "robustness" in metrics and "underwater" in metrics
    robustness = metrics["robustness"]
    assert robustness["excluded_top3_win_pnl"] == pytest.approx(100_000.0)  # 60k + 40k
    # 净利 20 万 - 最佳月(≤6 万) - 最大 3 笔(10 万) → 稳健版净利被显著削减。
    assert robustness["net_profit"] < metrics["net_profit"]


def test_robustness_excludes_best_month_and_top3_wins():
    """稳健性口径：把最佳月与最大 3 笔盈利全剔掉，收益成色显著下降。"""
    # 全年只有两个盈利轮次 + 一个亏损轮次；净利全靠这几笔。
    curve = [_point("2025-01-02", 1_000_000.0), _point("2025-12-31", 1_090_000.0)]
    rounds = [_round("R001", 80_000.0), _round("R002", 30_000.0), _round("R003", -20_000.0)]
    metrics = _metrics(curve, rounds)

    assert metrics["net_profit"] == pytest.approx(90_000.0)
    robustness = metrics["robustness"]
    # 最佳月 pnl = 9 万（12 月），最大 3 笔盈利 = 8 万 + 3 万 = 11 万 → 合计剔 20 万。
    assert robustness["excluded_best_month_pnl"] == pytest.approx(90_000.0)
    assert robustness["excluded_top3_win_pnl"] == pytest.approx(110_000.0)
    assert robustness["net_profit"] == pytest.approx(90_000.0 - 90_000.0 - 110_000.0)
    assert robustness["total_return"] < 0  # 剔完转负 → 收益靠少数极端项撑起


def test_robustness_with_no_wins_excludes_only_best_month():
    curve = [_point("2025-01-02", 1_000_000.0), _point("2025-12-31", 950_000.0)]
    rounds = [_round("R001", -30_000.0), _round("R002", -20_000.0)]
    metrics = _metrics(curve, rounds)

    robustness = metrics["robustness"]
    assert robustness["excluded_top3_win_pnl"] == 0.0
    # 亏损月不参与「最佳月」剔除（best_month_pnl 取 max，亏损月为负，max 默认 0）。
    assert robustness["excluded_best_month_pnl"] == 0.0
    assert robustness["net_profit"] == pytest.approx(metrics["net_profit"])


# ------------------------------------------------- read-time backfill ----

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    application = create_app()
    return application, TestClient(application)


def test_old_report_backfills_extended_metrics_on_read(api):
    """旧报告 payload 缺三键：GET 详情时按权益曲线补算，历史报告同样可见。"""
    application, client = api
    curve = [
        _point("2025-01-02", 1_000_000.0),
        _point("2025-06-30", 1_100_000.0),
        _point("2025-12-31", 1_050_000.0),
    ]
    report = {
        "id": "bt-old", "name": "旧报告", "status": "COMPLETED",
        "created_at": "2026-01-01T00:00:00", "config": {"initial_cash": 1_000_000.0},
        "metrics": {"initial_cash": 1_000_000.0, "monthly": [{"month": "2025-01"}]},
        "equity_curve": curve, "trades": [], "orders": [], "rounds": [],
        "events": [], "warnings": [], "withdrawals": [],
        "data_info": {"mode": "funnel_rotation"},
    }
    application.state.storage.save_backtest(report)

    body = client.get("/api/backtests/bt-old").json()
    assert body["metrics"]["xirr_annualized"] is not None
    assert "robustness" in body["metrics"]
    assert "underwater" in body["metrics"]
    # 期末 105 万 < 峰值 110 万 → 期末仍处水下。
    assert body["metrics"]["underwater"]["currently_underwater"] is True


def test_new_report_keeps_persisted_extended_metrics(api):
    """新报告 payload 已含三键：读取端点不覆盖（setdefault 语义）。"""
    application, client = api
    report = {
        "id": "bt-new", "name": "新报告", "status": "COMPLETED",
        "created_at": "2026-06-01T00:00:00", "config": {"initial_cash": 1_000_000.0},
        "metrics": {
            "initial_cash": 1_000_000.0, "total_fees": 0.0, "t_net_profit": 0.0,
            "max_invested": 1_000_000.0, "max_float_loss": 0.0, "buy_hold_return": 0.0,
            "xirr_annualized": 0.42, "robustness": {"net_profit": 1.0},
            "underwater": {"longest_underwater_days": 7},
        },
        "equity_curve": [_point("2025-01-02", 1_000_000.0), _point("2025-12-31", 1_200_000.0)],
        "trades": [], "orders": [], "rounds": [], "events": [], "warnings": [],
        "data_info": {"mode": "funnel_rotation"},
    }
    application.state.storage.save_backtest(report)

    body = client.get("/api/backtests/bt-new").json()
    assert body["metrics"]["xirr_annualized"] == 0.42  # 未被重算覆盖
    assert body["metrics"]["robustness"] == {"net_profit": 1.0}
    assert body["metrics"]["underwater"] == {"longest_underwater_days": 7}