# -*- coding: utf-8 -*-
"""模拟盘轮次明细 / 三腿分拆 / 止盈执行诊断 / 策略指标 / 策略开关（阶段 3）单测。

覆盖口径（AGENTS 架构原则 1：单一真相源）：
- 轮次边界按「净个股持仓（PERMANENT+T，不含 BETA）0→正→0」切分；
- 分腿恒等式 ``pnl = stock_pnl + beta_pnl - withdraw_in_round``（复用回测
  ``decompose_round_pnl``，不复制第二套算法）；
- ``pnl`` 有逐日净值时用权益差，无净值数据时留 None（不瞎算）；
- close_diagnostic 信号侧来自 ``_set_close_signal`` 落进 ``strategy_state`` 的
  ``last_close_signal``，下一轮首笔建仓清空；
- 策略指标 / 开关均为读时派生，不依赖任何净值风险统计。
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.broker import BrokerAccount
from app.domain import Bar, FeeConfig, Fill, OrderRequest, StrategyConfig
from app.main import create_app
from app.paper_analytics import (
    CLOSE_REASON_LABELS,
    close_reason_label,
    max_invested_from_fills,
    paper_rounds,
    paper_strategy_metrics,
    paper_strategy_switches,
)
from app.strategy import STRATEGY_STATE_FIELDS, LayeredTStrategy

PAPER_ROUND_KEYS = {
    "id", "symbol", "name", "start_date", "end_date", "entry_date", "exit_date",
    "status", "installment_count", "invested", "max_invested", "t_net_profit",
    "pnl", "approx", "stock_pnl", "beta_pnl", "withdraw_in_round", "round_return",
    "holding_days", "close_reason", "close_reason_label", "close_diagnostic",
    "close_signal_trigger", "close_signal_trigger_label", "close_signal_price",
    "close_signal_time", "close_signal_return", "close_fill_price", "close_fill_time",
    "close_execution_gap", "close_round_return", "close_return_gap",
}
STRATEGY_METRIC_KEYS = {
    "total_rounds", "closed_rounds", "win_rate", "profit_factor",
    "avg_round_return", "avg_holding_days", "t_net_profit", "t_trade_count",
    "beta_pnl", "beta_trade_count", "max_invested", "avg_max_invested",
    "installment_avg", "installment_max", "installment_histogram",
    "close_reason_distribution",
}
SWITCH_ITEM_KEYS = {"key", "label", "enabled", "value", "group", "hint"}
SWITCH_GROUPS = ["止盈/风控", "做T", "加仓", "持仓/退出", "ETF beta"]


def zero_fee_config(**overrides) -> StrategyConfig:
    """零费率 + 零滑点：让手算断言不受分项费用噪声影响。"""
    patch = FeeConfig(
        commission_rate=0, min_commission=0, stamp_tax_rate=0,
        handling_fee_rate=0, regulatory_fee_rate=0, transfer_fee_rate=0,
    ).to_strategy_patch()
    return StrategyConfig(slippage_bps=0, volume_participation=1.0, **patch, **overrides)


def run_fill(
    account: BrokerAccount, *, date: str, symbol: str, side: str, quantity: int,
    price: float, purpose: str = "PERMANENT", reason: str | None = None,
):
    """提交一张 NEXT_BAR 委托并用同价位 bar 撮合，返回唯一一笔成交。"""
    account.new_day(date)
    order = account.submit(OrderRequest(
        symbol=symbol, side=side, quantity=quantity, purpose=purpose,
        execution_mode="NEXT_BAR", order_type="MARKET",
        reason=reason or f"{purpose} 测试",
    ), f"{date}T15:00:00")
    assert order.status == "PENDING", order.message
    bar = Bar(
        timestamp=datetime.fromisoformat(f"{date}T15:00:00"), symbol=symbol,
        open=price, high=price, low=price, close=price,
        volume=10_000_000.0, amount=price * 10_000_000.0, prev_close=price,
    )
    fills = account.process_bar(bar)
    assert len(fills) == 1, [fill.to_dict() for fill in fills]
    return fills[0]


def build_two_round_account(initial_cash: float = 1_000_000.0, account_id: str | None = None):
    """一轮完整买卖（含 T / BETA / 提现）+ 一轮未平；返回 (account, 成本峰值轨迹)。"""
    account = BrokerAccount(initial_cash, zero_fee_config(), name="轮次测试", account_id=account_id)
    peaks: list[float] = []
    # R001：两笔永久仓建仓（成本 10,000 → 15,000）。
    run_fill(account, date="2025-01-02", symbol="TEST", side="BUY", quantity=1000, price=10.0)
    peaks.append(account.permanent_cost)
    run_fill(account, date="2025-01-03", symbol="TEST", side="BUY", quantity=500, price=10.0)
    peaks.append(account.permanent_cost)
    # 做 T：买 200 @10、卖 200 @10.5（净利 +100），T 成交不应切分/推高永久成本。
    run_fill(account, date="2025-01-06", symbol="TEST", side="BUY", quantity=200,
             price=10.0, purpose="T")
    peaks.append(account.permanent_cost)
    run_fill(account, date="2025-01-07", symbol="TEST", side="SELL", quantity=200,
             price=10.5, purpose="T", reason="T净利止盈")
    # ETF beta 腿：买 1000 @2.5、卖 1000 @2.6（已实现 +100），不属于个股轮次。
    run_fill(account, date="2025-01-08", symbol="sz.159915", side="BUY", quantity=1000,
             price=2.5, purpose="BETA", reason="指数趋势向上，空仓建 beta 腿")
    run_fill(account, date="2025-01-09", symbol="sz.159915", side="SELL", quantity=1000,
             price=2.6, purpose="BETA", reason="指数跌破20日线")
    # 轮内提现 500（R001 区间内）。
    account._execute_withdrawal(500.0, "monthly", 1000.0, "2025-01", "2025-01-09")
    # 清仓：卖 1500 @12（已实现 +3,000），R001 结束。
    account.mark("TEST", 12.0)
    run_fill(account, date="2025-01-10", symbol="TEST", side="SELL", quantity=1500,
             price=12.0, reason="价格轨收益达到15%")
    peaks.append(account.permanent_cost)
    # R002：换票后未平（再买一笔 T 不影响永久成本 / 轮次边界）。
    run_fill(account, date="2025-01-13", symbol="TEST2", side="BUY", quantity=800, price=20.0)
    account.mark("TEST2", 20.0)
    peaks.append(account.permanent_cost)
    run_fill(account, date="2025-01-14", symbol="TEST2", side="BUY", quantity=100,
             price=20.0, purpose="T")
    account.mark("TEST2", 20.0)
    return account, peaks


def build_equity_curve(initial_cash: float = 1_000_000.0) -> list[dict]:
    """逐日净值：R001 收尾净利 2,700（T +100 / beta +100 / 提现 500 / 个股 +3,000）。"""
    final = initial_cash + 2_700.0
    rows = [
        ("2024-12-31", initial_cash),
        ("2025-01-02", initial_cash),
        ("2025-01-03", initial_cash),
        ("2025-01-06", initial_cash),
        ("2025-01-07", initial_cash + 100.0),
        ("2025-01-08", initial_cash + 100.0),
        ("2025-01-09", initial_cash + 200.0 - 500.0),
        ("2025-01-10", final),
        ("2025-01-13", final),
        ("2025-01-14", final),
    ]
    return [
        {"date": day, "ts": f"{day}T15:00:00", "equity": equity, "beta_value": 0.0}
        for day, equity in rows
    ]


# ---------------------------------------------------------------------------
# 1. 轮次边界：T / BETA 不切轮次，OPEN 轮用当前状态收尾
# ---------------------------------------------------------------------------


def test_paper_rounds_splits_by_net_stock_position_only():
    account, _peaks = build_two_round_account()
    rows = paper_rounds(account, build_equity_curve())

    assert [row["id"] for row in rows] == ["R001", "R002"]
    first, second = rows
    assert first["symbol"] == "TEST"
    assert first["start_date"] == "2025-01-02"
    assert first["end_date"] == "2025-01-10"
    assert first["entry_date"] == "2025-01-02"
    assert first["exit_date"] == "2025-01-10"
    assert first["status"] == "CLOSED"
    assert first["holding_days"] == 7
    assert first["close_reason"] == "价格轨收益达到15%"
    assert first["close_reason_label"] == "止盈（价格轨/账户轨）"
    assert second["symbol"] == "TEST2"
    assert second["start_date"] == "2025-01-13"
    assert second["status"] == "OPEN"
    assert second["end_date"] is None
    assert second["exit_date"] is None
    assert second["close_reason"] is None
    # T / BETA 成交既没有把 R001 切成多轮，也没有在 R002 后制造第三轮。
    assert len(rows) == 2
    for row in rows:
        assert set(row) == PAPER_ROUND_KEYS


# ---------------------------------------------------------------------------
# 2. 分腿恒等式（容差 0.01）
# ---------------------------------------------------------------------------


def test_round_pnl_decomposition_identity():
    account, _peaks = build_two_round_account()
    rows = paper_rounds(account, build_equity_curve())
    for row in rows:
        assert row["pnl"] is not None
        assert row["pnl"] == pytest.approx(
            row["stock_pnl"] + row["beta_pnl"] - row["withdraw_in_round"], abs=0.01,
        ), row

    first, _second = rows
    assert first["pnl"] == pytest.approx(2_700.0)
    assert first["beta_pnl"] == pytest.approx(100.0)
    assert first["withdraw_in_round"] == pytest.approx(500.0)
    assert first["stock_pnl"] == pytest.approx(3_100.0)
    assert first["round_return"] == pytest.approx(2_700.0 / 15_000.0)
    assert first["t_net_profit"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 3. max_invested / installment_count 与成本账本一致
# ---------------------------------------------------------------------------


def test_round_invested_peak_and_installments():
    account, peaks = build_two_round_account()
    rows = paper_rounds(account, build_equity_curve())
    first, second = rows

    round1_peak = max(peaks[0:3])
    assert round1_peak == pytest.approx(15_000.0)
    assert first["max_invested"] == pytest.approx(round1_peak)
    assert first["installment_count"] == 2
    # 清仓后投入归零（与回测 invest 收尾一致）。
    assert first["invested"] == pytest.approx(0.0)

    assert second["max_invested"] == pytest.approx(16_000.0)
    assert second["installment_count"] == 1
    assert second["invested"] == pytest.approx(16_000.0)
    # 全账户峰值 = 两轮峰值的较大者。
    assert max_invested_from_fills(account.trades) == pytest.approx(16_000.0)


# ---------------------------------------------------------------------------
# 4. pnl 用权益差；无 paper_equity 数据时 None
# ---------------------------------------------------------------------------


def test_round_pnl_uses_equity_difference_and_none_without_snapshots():
    account, _peaks = build_two_round_account()
    with_curve = paper_rounds(account, build_equity_curve())
    assert with_curve[0]["pnl"] == pytest.approx(2_700.0)
    assert with_curve[0]["approx"] is False
    assert with_curve[1]["pnl"] == pytest.approx(0.0)
    assert with_curve[1]["approx"] is False

    # 新账户首日：没有任何逐日净值 → pnl 留 None、不标近似，也不瞎算。
    no_curve = paper_rounds(account, [])
    assert len(no_curve) == 2
    for row in no_curve:
        assert row["pnl"] is None
        assert row["approx"] is False
        assert row["stock_pnl"] is None
        assert row["beta_pnl"] is None
        assert row["round_return"] is None
        assert row["holding_days"] is None
        # 提现仍可精确统计（只依赖提现流水日期，不依赖净值）。
        assert row["withdraw_in_round"] == pytest.approx(500.0 if row is no_curve[0] else 0.0)


def test_first_round_without_previous_equity_point_is_marked_approx():
    """净值序列从轮次中途才开始：首轮用 equity + 轮内提现 − initial 近似并标 approx。"""
    account, _peaks = build_two_round_account()
    curve = [row for row in build_equity_curve() if row["date"] >= "2025-01-03"]
    rows = paper_rounds(account, curve)
    first = rows[0]
    assert first["approx"] is True
    # 区间内最后一个净值点 01-10 = 1,002,700 + 轮内提现 500 − initial 1,000,000。
    assert first["pnl"] == pytest.approx(3_200.0)


# ---------------------------------------------------------------------------
# 5. close_diagnostic：信号侧落库 → 轮次收尾读取 → 下一轮首笔建仓清空
# ---------------------------------------------------------------------------


def test_close_diagnostic_reads_signal_from_strategy_state():
    account, _peaks = build_two_round_account()
    strategy = LayeredTStrategy(account.config)
    strategy.round_t_start = account.t_net_profit
    bar = Bar(
        timestamp=datetime.fromisoformat("2025-01-10T14:55:00"), symbol="TEST",
        open=12.0, high=12.0, low=12.0, close=12.0,
        volume=1000.0, amount=12_000.0, prev_close=12.0,
    )
    strategy._set_close_signal("价格轨止盈", bar, account)

    stored = account.strategy_state["last_close_signal"]
    assert stored["trigger"] == "价格轨止盈"
    assert stored["signal_price"] == pytest.approx(12.0)
    assert stored["signal_time"] == "2025-01-10T14:55:00"

    rows = paper_rounds(account, build_equity_curve())
    first = rows[0]
    assert first["close_signal_trigger"] == "价格轨止盈"
    assert first["close_signal_trigger_label"] == "止盈（价格轨/账户轨）"
    assert first["close_signal_price"] == pytest.approx(12.0)
    assert first["close_signal_time"] == "2025-01-10T14:55:00"
    assert first["close_fill_price"] == pytest.approx(12.0)
    assert first["close_execution_gap"] == pytest.approx(0.0)
    assert first["close_round_return"] == pytest.approx(2_700.0 / 15_000.0)
    diagnostic = first["close_diagnostic"]
    assert diagnostic["trigger"] == "价格轨止盈"
    assert diagnostic["trigger_label"] == "止盈（价格轨/账户轨）"
    # OPEN 轮不写执行诊断。
    assert rows[1]["close_diagnostic"] is None


def test_last_close_signal_registered_and_cleared_on_next_round_entry():
    names = {name for name, _kind, _default in STRATEGY_STATE_FIELDS}
    assert "last_close_signal" in names

    strategy = LayeredTStrategy(StrategyConfig())
    signal = {
        "trigger": "价格轨止盈", "signal_price": 12.0,
        "signal_time": "2025-01-10T14:55:00", "price_return": 0.15,
    }
    strategy.last_close_signal = dict(signal)
    dump = strategy.state_dict()
    restored = LayeredTStrategy(StrategyConfig())
    restored.load_state(dump)
    assert restored.last_close_signal == signal

    # 下一轮首笔永久仓建仓（round_entry_date 为空）→ 清空上一轮信号。
    buy = Fill(
        id="F1", order_id="O1", symbol="TEST2", side="BUY", quantity=100,
        price=20.0, amount=2_000.0, commission=0.0, stamp_tax=0.0,
        transfer_fee=0.0, total_fee=0.0, slippage=0.0,
        timestamp="2025-01-13T09:30:00", purpose="PERMANENT", cash_flow=-2_000.0,
    )
    restored.round_entry_date = None
    restored.on_fill(buy, 0)
    assert restored.last_close_signal is None
    assert restored.round_entry_date == "2025-01-13"


# ---------------------------------------------------------------------------
# 6. 端点：/rounds 分页信封 / /rounds/summary / /switches / 404 / 空账户
# ---------------------------------------------------------------------------


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    return create_app()


def _seed_rounds(application, account_id: str):
    account, _peaks = build_two_round_account(account_id=account_id)
    application.state.storage.save_paper(account.to_dict(include_internal=True))
    for row in build_equity_curve():
        application.state.storage.save_paper_equity(account_id, row, source="live")
    return account


def test_rounds_endpoints_paginate_and_expose_fields(tmp_path, monkeypatch):
    application = _app(tmp_path, monkeypatch)
    client = TestClient(application)
    account = client.post("/api/paper/accounts", json={
        "name": "轮次端点", "initial_cash": 1_000_000,
    }).json()
    _seed_rounds(application, account["id"])

    response = client.get(f"/api/paper/accounts/{account['id']}/rounds?page=1&page_size=1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "items", "total", "page", "page_size", "total_pages", "has_prev", "has_next",
    }
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["page_size"] == 1
    assert body["total_pages"] == 2
    assert body["has_prev"] is False
    assert body["has_next"] is True
    assert len(body["items"]) == 1
    assert set(body["items"][0]) == PAPER_ROUND_KEYS
    assert body["items"][0]["id"] == "R001"
    assert body["items"][0]["stock_pnl"] is not None

    second = client.get(f"/api/paper/accounts/{account['id']}/rounds?page=2&page_size=1").json()
    assert second["page"] == 2
    assert second["has_next"] is False
    assert second["items"][0]["id"] == "R002"

    # 越界页码收敛到最后一页。
    clamped = client.get(f"/api/paper/accounts/{account['id']}/rounds?page=99&page_size=1").json()
    assert clamped["page"] == 2

    summary = client.get(f"/api/paper/accounts/{account['id']}/rounds/summary")
    assert summary.status_code == 200, summary.text
    summary_body = summary.json()
    assert set(summary_body) == {"strategy_metrics", "derived_from"}
    assert summary_body["derived_from"] == "rounds"
    assert set(summary_body["strategy_metrics"]) == STRATEGY_METRIC_KEYS
    assert summary_body["strategy_metrics"]["total_rounds"] == 2
    assert summary_body["strategy_metrics"]["closed_rounds"] == 1
    assert summary_body["strategy_metrics"]["win_rate"] == pytest.approx(1.0)

    switches = client.get(f"/api/paper/accounts/{account['id']}/switches")
    assert switches.status_code == 200, switches.text
    switches_body = switches.json()
    assert set(switches_body) == {"switches", "config_snapshot_at"}
    assert list(switches_body["switches"]) == SWITCH_GROUPS


def test_rounds_endpoints_empty_account_and_422_and_404(tmp_path, monkeypatch):
    application = _app(tmp_path, monkeypatch)
    client = TestClient(application)
    account = client.post("/api/paper/accounts", json={
        "name": "空账户", "initial_cash": 500_000,
    }).json()

    empty = client.get(f"/api/paper/accounts/{account['id']}/rounds")
    assert empty.status_code == 200
    assert empty.json()["total"] == 0
    assert empty.json()["items"] == []
    assert empty.json()["total_pages"] == 0

    empty_summary = client.get(f"/api/paper/accounts/{account['id']}/rounds/summary").json()
    metrics = empty_summary["strategy_metrics"]
    assert metrics["total_rounds"] == 0
    assert metrics["win_rate"] == pytest.approx(0.0)
    assert metrics["profit_factor"] is None

    swapped = client.get(f"/api/paper/accounts/{account['id']}/switches")
    assert swapped.status_code == 200
    assert list(swapped.json()["switches"]) == SWITCH_GROUPS

    assert client.get(f"/api/paper/accounts/{account['id']}/rounds?page_size=0").status_code == 422
    assert client.get("/api/paper/accounts/not-exist/rounds").status_code == 404
    assert client.get("/api/paper/accounts/not-exist/rounds/summary").status_code == 404
    assert client.get("/api/paper/accounts/not-exist/switches").status_code == 404


# ---------------------------------------------------------------------------
# 7. 策略指标汇总（3 胜 2 负）
# ---------------------------------------------------------------------------


def _known_rounds() -> list[dict]:
    return [
        {"status": "CLOSED", "pnl": 100.0, "round_return": 0.10, "holding_days": 5,
         "max_invested": 1000.0, "installment_count": 2,
         "close_reason": "价格轨收益达到15%"},
        {"status": "CLOSED", "pnl": 200.0, "round_return": 0.20, "holding_days": 3,
         "max_invested": 1000.0, "installment_count": 4,
         "close_reason": "价格轨收益达到15%"},
        {"status": "CLOSED", "pnl": 300.0, "round_return": 0.30, "holding_days": 7,
         "max_invested": 1000.0, "installment_count": 6,
         "close_reason": "VWAP账户回撤4%_3根5分钟K线"},
        {"status": "CLOSED", "pnl": -100.0, "round_return": -0.10, "holding_days": 2,
         "max_invested": 1000.0, "installment_count": 1,
         "close_reason": "最大持仓20日强平"},
        {"status": "CLOSED", "pnl": -200.0, "round_return": -0.20, "holding_days": 4,
         "max_invested": 1000.0, "installment_count": 3,
         "close_reason": "最大持仓20日强平"},
        {"status": "OPEN", "pnl": 50.0, "round_return": 0.05, "holding_days": 1,
         "max_invested": 1000.0, "installment_count": 2, "close_reason": None},
    ]


def test_strategy_metrics_three_wins_two_losses():
    account = BrokerAccount(1_000_000, zero_fee_config())
    run_fill(account, date="2025-01-02", symbol="TEST", side="BUY", quantity=1000,
             price=10.0)
    run_fill(account, date="2025-01-03", symbol="TEST", side="BUY", quantity=100,
             price=10.0, purpose="T")
    run_fill(account, date="2025-01-06", symbol="TEST", side="SELL", quantity=100,
             price=10.5, purpose="T", reason="T净利止盈")
    run_fill(account, date="2025-01-07", symbol="sz.159915", side="BUY", quantity=100,
             price=2.5, purpose="BETA")
    run_fill(account, date="2025-01-08", symbol="sz.159915", side="SELL", quantity=100,
             price=2.6, purpose="BETA", reason="指数跌破20日线")

    metrics = paper_strategy_metrics(account, _known_rounds())
    assert set(metrics) == STRATEGY_METRIC_KEYS
    assert metrics["total_rounds"] == 6
    assert metrics["closed_rounds"] == 5
    assert metrics["win_rate"] == pytest.approx(3 / 5)
    # 盈亏比 = Σ盈利 / |Σ亏损| = 600 / 300。
    assert metrics["profit_factor"] == pytest.approx(2.0)
    assert metrics["avg_round_return"] == pytest.approx(0.06)
    assert metrics["avg_holding_days"] == pytest.approx(4.2)
    assert metrics["avg_max_invested"] == pytest.approx(1000.0)
    assert metrics["installment_avg"] == pytest.approx(3.0)
    assert metrics["installment_max"] == 6
    assert metrics["installment_histogram"] == {"1": 1, "2": 2, "3": 1, "4": 1, "6": 1}
    # 退出原因按中文标签归并（「最大持仓20日强平」→「超期强平（20 日）」）。
    assert metrics["close_reason_distribution"] == {
        "止盈（价格轨/账户轨）": 2,
        "VWAP 账户回撤（辅助清仓）": 1,
        "超期强平（20 日）": 2,
    }
    # 做 T / ETF 腿与账户流水同源。
    assert metrics["t_trade_count"] == 2
    assert metrics["t_net_profit"] == pytest.approx(50.0)
    assert metrics["beta_trade_count"] == 2
    assert metrics["beta_pnl"] == pytest.approx(10.0)
    assert metrics["max_invested"] == pytest.approx(10_000.0)


def test_strategy_metrics_empty_account_is_safe():
    account = BrokerAccount(1_000_000, zero_fee_config())
    metrics = paper_strategy_metrics(account, [])
    assert metrics["total_rounds"] == 0
    assert metrics["closed_rounds"] == 0
    assert metrics["win_rate"] == pytest.approx(0.0)
    assert metrics["profit_factor"] is None
    assert metrics["avg_round_return"] is None
    assert metrics["avg_holding_days"] is None
    assert metrics["avg_max_invested"] is None
    assert metrics["installment_avg"] is None
    assert metrics["installment_max"] == 0
    assert metrics["installment_histogram"] == {}
    assert metrics["close_reason_distribution"] == {}
    assert metrics["max_invested"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 8. 策略开关（来自账户 config 快照）
# ---------------------------------------------------------------------------


def test_paper_strategy_switches_reads_config_snapshot():
    config = zero_fee_config(
        enable_t=False, divergence_ticks=5, vwap_drawdown=0.03,
        add_tier_strict=False, force_exit_holding_days=20, max_holding_days=30,
        t_deep_drop_pct=None, etf_beta_enabled=False,
        installment_scale_mode="equity",
    )
    account = BrokerAccount(1_000_000, config, name="开关快照")
    body = paper_strategy_switches(account)
    assert set(body) == {"switches", "config_snapshot_at"}
    assert body["config_snapshot_at"] == account.created_at

    groups = body["switches"]
    assert list(groups) == SWITCH_GROUPS
    items: dict[str, dict] = {}
    for group, entries in groups.items():
        assert entries, group
        for item in entries:
            assert set(item) == SWITCH_ITEM_KEYS
            assert item["group"] == group
            items[item["key"]] = item

    required = {
        "price_take_profit", "account_take_profit", "armed_range",
        "divergence_ticks", "vwap_drawdown", "enable_t", "t_min_base_entries",
        "t_min_amplitude", "t_min_turnover", "t_max_turnover",
        "t_min_expected_net_spread", "t_target_net_spread", "t_exit_cutoff",
        "t_deep_drop_pct", "add_tier_strict", "min_add_gap_days",
        "installment_amounts", "installment_scale_mode", "installment_scale_floor",
        "max_holding_days", "force_exit_holding_days", "etf_beta_enabled",
        "etf_symbol", "etf_position_pct", "etf_entry_ma_days", "etf_exit_ma_days",
        "etf_hard_stop_pct", "etf_beta_coexist",
    }
    assert required <= set(items)

    assert items["divergence_ticks"]["value"] == "5 根（三选二）"
    assert items["vwap_drawdown"]["value"] == "3%"
    assert items["enable_t"]["enabled"] is False
    assert items["t_min_base_entries"]["enabled"] is False
    assert items["t_exit_cutoff"]["value"] == "14:50"
    assert items["t_deep_drop_pct"]["enabled"] is False
    assert items["add_tier_strict"]["enabled"] is False
    assert items["add_tier_strict"]["value"] == "全档统一"
    assert items["force_exit_holding_days"]["enabled"] is True
    assert items["force_exit_holding_days"]["value"] == "20 日"
    assert items["max_holding_days"]["enabled"] is True
    assert items["etf_beta_enabled"]["enabled"] is False
    assert items["etf_symbol"]["enabled"] is False
    assert items["installment_scale_mode"]["value"] == "按权益等比"

    # 默认配置的对照组：做 T 开、ETF 开、强平关。
    default = paper_strategy_switches(BrokerAccount(1_000_000, StrategyConfig()))
    default_items = {
        item["key"]: item for entries in default["switches"].values() for item in entries
    }
    assert default_items["enable_t"]["enabled"] is True
    assert default_items["etf_beta_enabled"]["enabled"] is True
    assert default_items["etf_symbol"]["enabled"] is True
    assert default_items["force_exit_holding_days"]["enabled"] is False


# ---------------------------------------------------------------------------
# 9. 退出原因中文标签
# ---------------------------------------------------------------------------


def test_close_reason_label_maps_known_reasons_and_passes_unknown():
    assert CLOSE_REASON_LABELS["Armed量价背离三选二_3根5分钟K线"] == "Armed 量价背离（辅助清仓）"
    assert close_reason_label("Armed量价背离三选二_3根5分钟K线") == "Armed 量价背离（辅助清仓）"
    assert close_reason_label("VWAP账户回撤4%_3根5分钟K线") == "VWAP 账户回撤（辅助清仓）"
    assert close_reason_label("最大持仓20日强平") == "超期强平（20 日）"
    assert close_reason_label("最大持仓7日强平") == "超期强平（7 日）"
    assert close_reason_label("价格轨收益达到15%") == "止盈（价格轨/账户轨）"
    assert close_reason_label("账户轨收益达到15%") == "止盈（价格轨/账户轨）"
    assert close_reason_label("T+1尾盘截止平T") == "T+1 尾盘强平"
    assert close_reason_label("某个全新退出原因") == "某个全新退出原因"
    assert close_reason_label(None) is None
    assert close_reason_label("") is None
