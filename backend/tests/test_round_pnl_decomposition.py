"""轮次盈亏分腿（个股腿 / ETF beta 腿 / 轮内提现）单测。

口径恒等式（已用真实报告 6ceb50df 的 R001 与成交明细互相验证）：

    rounds[].pnl = stock_pnl + beta_pnl - withdraw_in_round
    beta_pnl = beta_value_end - beta_value_start
               - Σ(买入金额 + 买入费用) + Σ(卖出金额 - 卖出费用)
    stock_pnl = pnl + withdraw_in_round - beta_pnl

覆盖：
- 含 ETF 腿交易 + 轮内提现：恒等式成立（含提现区间左开右闭的口径）；
- 无 ETF、无提现：stock_pnl == pnl、beta_pnl == 0、withdraw_in_round == 0；
- decompose_round_pnl 幂等：重复调用不改值、已有分腿字段不覆盖；
- 真实报告 6ceb50df-da91-414b-94ed-851b604b3280 的 5 个轮次回归：
  R001 个股腿 -161772.62 / ETF 腿 -30339.71 / 轮内提现 0，与成交明细完全吻合。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.backtest import _beta_fill_cash_flows, decompose_round_pnl
from app.main import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "round_pnl_decomposition_6ceb50df.json"


def _round(**overrides) -> dict:
    row = {
        "id": "R001", "symbol": "sh.688012", "start_date": "2024-01-02",
        "end_date": "2024-01-10", "status": "CLOSED", "pnl": 6000.0,
        "max_invested": 100000.0,
    }
    row.update(overrides)
    return row


def _equity_curve() -> list[dict]:
    """轮次前一日 beta 0 → 轮内买入持有 → 轮内清仓，末日 beta 归 0。"""
    return [
        {"date": "2024-01-01", "beta_value": 0.0},
        {"date": "2024-01-02", "beta_value": 0.0},
        {"date": "2024-01-03", "beta_value": 40200.0},
        {"date": "2024-01-08", "beta_value": 0.0},
        {"date": "2024-01-09", "beta_value": 0.0},
        {"date": "2024-01-10", "beta_value": 0.0},
    ]


def test_decompose_round_with_etf_leg_and_withdrawal_keeps_identity():
    """轮内 ETF 买入/卖出 + 提现：ETF 腿按资本账户法，恒等式成立。

    构造：买入 40000 元（费 10）→ 卖出 45000 元（费 11.25），ETF 腿净盈亏
    = 0 - 0 - 40010 + 44988.75 = 4978.75。提现 300（建仓日，属上一轮，不计）、
    500（轮内，计入）、100（轮后，不计）→ 轮内提现 500。
    个股腿 = 6000 + 500 - 4978.75 = 1521.25。
    """
    rounds = [_round()]
    equity_curve = _equity_curve()
    beta_fills = [
        {"date": "2024-01-03", "side": "BUY", "quantity": 40000, "price": 1.0, "fee": 10.0},
        {"date": "2024-01-08", "side": "SELL", "quantity": 45000, "price": 1.0, "fee": 11.25},
    ]
    withdrawals = [
        {"date": "2024-01-02", "amount": 300.0},
        {"date": "2024-01-09", "amount": 500.0},
        {"date": "2024-01-11", "amount": 100.0},
    ]

    out = decompose_round_pnl(rounds, equity_curve, withdrawals, beta_fills)
    row = out[0]

    assert row["withdraw_in_round"] == pytest.approx(500.0, abs=0.01)
    assert row["beta_pnl"] == pytest.approx(4978.75, abs=0.01)
    assert row["stock_pnl"] == pytest.approx(1521.25, abs=0.01)
    assert row["stock_pnl"] + row["beta_pnl"] - row["withdraw_in_round"] == pytest.approx(
        row["pnl"], abs=0.01,
    )


def test_decompose_round_without_etf_or_withdrawal_collapses_to_pnl():
    """无 ETF 腿、无提现：个股腿 == 原 pnl，其余两行归零（向后兼容）。"""
    rounds = [_round(pnl=-1234.56)]
    equity_curve = [
        {"date": "2024-01-01", "beta_value": 0.0},
        {"date": "2024-01-10", "beta_value": 0.0},
    ]

    row = decompose_round_pnl(rounds, equity_curve, [], [])[0]

    assert row["withdraw_in_round"] == 0.0
    assert row["beta_pnl"] == 0.0
    assert row["stock_pnl"] == pytest.approx(row["pnl"], abs=0.01)


def test_decompose_round_is_idempotent_and_skips_filled_rows():
    """重复调用不改值；已带 stock_pnl 的轮次不被覆盖（旧/新报告混存安全）。"""
    rounds = [_round()]
    first = decompose_round_pnl(
        rounds, _equity_curve(), [{"date": "2024-01-09", "amount": 500.0}],
        [{"date": "2024-01-03", "side": "BUY", "quantity": 40000, "price": 1.0, "fee": 10.0}],
    )
    snapshot = copy.deepcopy(first)

    twice = decompose_round_pnl(
        first, _equity_curve(), [{"date": "2024-01-09", "amount": 500.0}],
        [{"date": "2024-01-03", "side": "BUY", "quantity": 40000, "price": 1.0, "fee": 10.0}],
    )
    assert twice == snapshot

    pinned = _round(stock_pnl=42.0, beta_pnl=-1000.0, withdraw_in_round=7.0)
    out = decompose_round_pnl([pinned], _equity_curve(), [], [])
    assert out[0]["stock_pnl"] == 42.0
    assert out[0]["beta_pnl"] == -1000.0
    assert out[0]["withdraw_in_round"] == 7.0


def test_beta_fill_cash_flows_accepts_engine_fill_objects():
    """引擎路径传的是 Fill 对象（purpose/timestamp/amount/total_fee），口径与 dict 一致。"""
    fills = [
        SimpleNamespace(
            purpose="BETA", timestamp="2024-01-03T09:30:00", side="BUY",
            amount=40000.0, total_fee=10.0,
        ),
        SimpleNamespace(
            purpose="BETA", timestamp="2024-01-08T09:30:00", side="SELL",
            amount=45000.0, total_fee=11.25,
        ),
        # 非 BETA 成交不参与：同日的个股买卖不能算进 ETF 腿现金流。
        SimpleNamespace(
            purpose="PERMANENT", timestamp="2024-01-05T09:30:00", side="BUY",
            amount=99999.0, total_fee=1.0,
        ),
    ]

    buy, sell = _beta_fill_cash_flows(fills, "2024-01-02", "2024-01-10")

    assert buy == pytest.approx(40010.0, abs=0.01)
    assert sell == pytest.approx(44988.75, abs=0.01)


# ---------------------------------------------------------------------------
# 真实报告回归（报告 6ceb50df 的派生子集 fixture，见文件内 note）
# ---------------------------------------------------------------------------

#: 5 个抽查轮次的期望值（来自完整报告 6ceb50df 的离线重算，容差 0.05 覆盖分位误差）。
REAL_EXPECTED = {
    "R001": {"withdraw_in_round": 0.0, "beta_pnl": -30339.71, "stock_pnl": -161772.62},
    "R002": {"withdraw_in_round": 46370.74, "beta_pnl": -14755.55, "stock_pnl": 92741.48},
    "R003": {"withdraw_in_round": 23369.62, "beta_pnl": 0.0, "stock_pnl": 46739.24},
    "R005": {"withdraw_in_round": 0.0, "beta_pnl": -1855.89, "stock_pnl": -187553.74},
    "R006": {"withdraw_in_round": 29994.58, "beta_pnl": 0.0, "stock_pnl": 59989.13},
}


def test_real_report_6ceb50df_round_pnl_decomposition():
    """真实报告 6ceb50df：R001 与成交明细的个股腿已实现盈亏 -161772.62 完全吻合。

    报告 R001（688012 中微公司 2020-08-10 ~ 2020-09-21）：pnl=-192112.33、
    轮内提现 0、ETF 腿=-30339.71 → 个股腿 = -161772.62。
    """
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rounds = decompose_round_pnl(
        copy.deepcopy(data["rounds"]),
        data["equity_curve"],
        data["withdrawals"],
        data["beta_fills"],
    )
    by_id = {row["id"]: row for row in rounds}

    # 四列对账：pnl / 轮内提现 / ETF腿 / 个股腿（pytest -s 可见，失败时也会打印）。
    print("\n报告 6ceb50df 轮次盈亏分腿：")
    for row in rounds:
        print(
            f"  {row['id']}  pnl={row['pnl']:>13,.2f}  wd={row['withdraw_in_round']:>10,.2f}  "
            f"beta={row['beta_pnl']:>12,.2f}  stock={row['stock_pnl']:>13,.2f}"
        )

    for round_id, expected in REAL_EXPECTED.items():
        row = by_id[round_id]
        for key, value in expected.items():
            assert row[key] == pytest.approx(value, abs=0.05), f"{round_id}.{key} 口径回归"

    # 恒等式：每个轮次的两条腿 + 轮内提现必须能还原原 pnl。
    for row in rounds:
        assert row["stock_pnl"] + row["beta_pnl"] - row["withdraw_in_round"] == pytest.approx(
            row["pnl"], abs=0.01,
        ), f"{row['id']} 恒等式不成立"

    # 幂等：真实报告重复读取（GET 报告 + /records/rounds + /rotation-trail）不改值。
    snapshot = copy.deepcopy(rounds)
    assert decompose_round_pnl(
        rounds, data["equity_curve"], data["withdrawals"], data["beta_fills"],
    ) == snapshot


# ---------------------------------------------------------------------------
# API：旧报告读取时离线补齐，records/rounds 与换票轨迹都拿到分腿字段
# ---------------------------------------------------------------------------

REPORT_ID = "bt-round-pnl-split"


def _api_report() -> dict:
    rounds = [{
        "id": "R001", "symbol": "sz.300001", "name": "测试A", "board": "创业板",
        "start_date": "2024-01-02", "end_date": "2024-01-10", "status": "CLOSED",
        "pnl": 6000.0, "max_invested": 100000.0, "start_equity": 4_000_000.0,
        "close_reason": "整体止盈",
    }]
    trail = [{
        "round": "R001", "symbol": "sz.300001", "name": "测试A", "board": "创业板",
        "select_date": "2024-01-01", "entry_date": None, "exit_date": None,
        "pnl": None, "chg20d_at_select": -0.2, "gate_open": True,
        "status": "CLOSED", "screen_warnings": [], "funnel": {},
    }]
    return {
        "id": REPORT_ID, "name": "轮次分腿测试", "status": "COMPLETED",
        "created_at": "2026-11-20T00:00:00", "config": {}, "metrics": {},
        "equity_curve": [
            {**point, "equity": 4_000_000.0} for point in _equity_curve()
        ],
        "trades": [], "orders": [], "rounds": rounds, "events": [], "warnings": [],
        "withdrawals": [{"id": "W1", "date": "2024-01-09", "amount": 500.0}],
        "data_info": {
            "mode": "funnel_rotation", "screening_count": len(trail),
            "rotation_trail": trail,
            "beta": {"enabled": True, "pnl": 4978.75, "fills": [
                {"date": "2024-01-03", "side": "BUY", "quantity": 40000, "price": 1.0, "fee": 10.0},
                {"date": "2024-01-08", "side": "SELL", "quantity": 45000, "price": 1.0, "fee": 11.25},
            ]},
        },
    }


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    application = create_app()
    return application, TestClient(application)


def _assert_split(body: dict) -> None:
    assert body["withdraw_in_round"] == pytest.approx(500.0, abs=0.01)
    assert body["beta_pnl"] == pytest.approx(4978.75, abs=0.01)
    assert body["stock_pnl"] == pytest.approx(1521.25, abs=0.01)


def test_records_rounds_and_trail_expose_pnl_split_for_old_report(api):
    """旧报告（轮次无分腿字段）经读取端点派生：明细与轨迹都能拿到三行数字。"""
    application, client = api
    application.state.storage.save_backtest(_api_report())

    records = client.get(f"/api/backtests/{REPORT_ID}/records/rounds").json()
    assert records["total"] == 1
    _assert_split(records["items"][0])

    trail = client.get(f"/api/backtests/{REPORT_ID}/rotation-trail").json()
    assert trail["items"] and trail["items"][0]["kind"] == "ROUND"
    _assert_split(trail["items"][0])

    # 主报告仍按分页约定瘦身：rounds 抽走只留条数，但读取不因派生而报错。
    report = client.get(f"/api/backtests/{REPORT_ID}").json()
    assert report["rounds"] == []
    assert report["rounds_total"] == 1
