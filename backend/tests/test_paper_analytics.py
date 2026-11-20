# -*- coding: utf-8 -*-
"""模拟盘读时派生指标（阶段 1）单测：metrics / beta 摘要 / 新端点 / created_at 兼容。

覆盖口径（见 AGENTS 架构原则 1）：
- ``net_profit / total_return / profit_withdrawn`` 与回测 ``performance_metrics``
  的「盈利提现加回」口径一致；
- ``max_invested`` 用与撮合同一份成本账本实现重放成交（不复制第二套算法）；
- ``paper_beta_summary`` 与回测 ``data_info.beta`` 同键同口径。
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.broker import BrokerAccount
from app.domain import Bar, FeeConfig, OrderRequest, StrategyConfig
from app.main import create_app
from app.paper_analytics import (
    max_invested_from_fills,
    paper_account_metrics,
    paper_beta_summary,
)

METRICS_KEYS = {
    "initial_cash", "final_equity", "total_fees", "t_net_profit", "total_withdrawn",
    "withdrawn_count", "profit_withdrawn", "net_profit", "total_return", "total_trades",
    "t_trade_count", "beta_pnl", "beta_trade_count", "max_invested",
}
# 与回测 data_info.beta 的同口径键（覆盖区间 source/coverage_* 不在本期范围）。
BETA_KEYS = {
    "enabled", "symbol", "trade_count", "buy_count", "sell_count", "entry_date",
    "last_exit_date", "exit_reason", "pnl", "position_qty", "fills",
}
BETA_FILL_KEYS = {"date", "side", "quantity", "price", "fee", "realized_pnl"}


def zero_fee_config(**overrides) -> StrategyConfig:
    """零费率 + 零滑点：让手算断言不受分项费用 / 四舍五入噪声影响。"""
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


def build_mixed_account() -> BrokerAccount:
    """构造一段含 PERMANENT / T / BETA 成交与两笔提现的账户流水。"""
    account = BrokerAccount(1_000_000, zero_fee_config(), name="读时派生")
    # 永久仓两笔建仓（2000 股 @10，成本 20,000）。
    run_fill(account, date="2025-01-02", symbol="TEST", side="BUY", quantity=2000, price=10.0)
    # 做 T：T 仓买 500 @10、次日卖 500 @10.5（净利 +250）。
    run_fill(account, date="2025-01-03", symbol="TEST", side="BUY", quantity=500, price=10.0, purpose="T")
    run_fill(account, date="2025-01-06", symbol="TEST", side="SELL", quantity=500, price=10.5, purpose="T")
    # 永久仓部分止盈：卖 1000 @11（已实现 +1,000），剩 1000 股。
    run_fill(account, date="2025-01-07", symbol="TEST", side="SELL", quantity=1000, price=11.0)
    # ETF beta 腿：买 1000 @2.5、卖 1000 @2.6（已实现 +100）。
    run_fill(account, date="2025-01-08", symbol="sz.159915", side="BUY", quantity=1000, price=2.5, purpose="BETA")
    run_fill(account, date="2025-01-09", symbol="sz.159915", side="SELL", quantity=1000, price=2.6, purpose="BETA")
    # 盈利提现 500（period_profit>0，加回净利）+ 非盈利提现 300（不加回）。
    account._execute_withdrawal(500.0, "monthly", 1000.0, "2025-01", "2025-02-03")
    account._execute_withdrawal(300.0, "monthly", -200.0, "2025-02", "2025-03-03")
    return account


# ---------------------------------------------------------------------------
# 1. 指标手算一致
# ---------------------------------------------------------------------------


def test_account_metrics_match_hand_calculation():
    account = build_mixed_account()
    metrics = paper_account_metrics(account)

    assert set(metrics) == METRICS_KEYS
    # 现金：1,000,000 −20,000 −5,000 +5,250 +11,000 −2,500 +2,600 −800(提现) = 990,550
    # 市值：剩 1000 股 × 11 = 11,000 → equity 1,001,550。
    assert account.cash == pytest.approx(990_550.0)
    assert metrics["final_equity"] == pytest.approx(1_001_550.0)
    assert metrics["initial_cash"] == pytest.approx(1_000_000.0)
    # 净利加回「盈利提现」500（不把非盈利提现 300 当收益）。
    assert metrics["profit_withdrawn"] == pytest.approx(500.0)
    assert metrics["total_withdrawn"] == pytest.approx(800.0)
    assert metrics["withdrawn_count"] == 2
    assert metrics["net_profit"] == pytest.approx(1_001_550.0 + 500.0 - 1_000_000.0)
    assert metrics["net_profit"] == pytest.approx(2_050.0)
    assert metrics["total_return"] == pytest.approx(2_050.0 / 1_000_000.0)
    # 成交口径：6 笔成交，其中 2 笔做 T；BETA 2 笔，已实现 +100。
    assert metrics["total_trades"] == 6
    assert metrics["t_trade_count"] == 2
    assert metrics["t_net_profit"] == pytest.approx(250.0)
    assert metrics["beta_trade_count"] == 2
    assert metrics["beta_pnl"] == pytest.approx(100.0)
    # 永久仓成本峰值：建仓后 20,000（T / BETA 成交不计入永久成本）。
    assert metrics["max_invested"] == pytest.approx(20_000.0)

    # 不返回依赖逐日净值的字段（留给净值落库阶段，避免口径先被写死）。
    for excluded in (
        "max_drawdown", "sharpe_ratio", "calmar_ratio", "volatility", "underwater",
        "exposure", "exposure_total", "exposure_stock", "exposure_beta",
        "idle_ratio", "monthly", "cashflow_adjusted", "annualized_return",
        "xirr_annualized", "max_float_loss",
    ):
        assert excluded not in metrics, excluded


def test_empty_account_metrics_are_zero():
    metrics = paper_account_metrics(BrokerAccount(500_000, zero_fee_config()))
    assert metrics["initial_cash"] == pytest.approx(500_000.0)
    assert metrics["final_equity"] == pytest.approx(500_000.0)
    assert metrics["net_profit"] == pytest.approx(0.0)
    assert metrics["total_return"] == pytest.approx(0.0)
    assert metrics["max_invested"] == pytest.approx(0.0)
    assert metrics["total_trades"] == 0
    assert metrics["t_trade_count"] == 0
    assert metrics["beta_pnl"] == pytest.approx(0.0)
    assert metrics["profit_withdrawn"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 2. max_invested = 按成本账本重放的峰值
# ---------------------------------------------------------------------------


def test_max_invested_equals_live_cost_ledger_peak():
    """含买入费用的账本峰值：读时重放必须与真实撮合后的 permanent_cost 峰值一致。"""
    config = StrategyConfig(
        slippage_bps=0, volume_participation=1.0,
        commission_rate=0.0003, min_commission=5.0, stamp_tax_rate=0.0005,
        handling_fee_rate=0.0000341, regulatory_fee_rate=0.00002, transfer_fee_rate=0.00001,
    )
    account = BrokerAccount(1_000_000, config, name="成本峰值")
    peaks: list[float] = []
    run_fill(account, date="2025-01-02", symbol="TEST", side="BUY", quantity=1000, price=10.0)
    peaks.append(account.permanent_cost)
    run_fill(account, date="2025-01-03", symbol="TEST", side="BUY", quantity=500, price=10.0)
    peaks.append(account.permanent_cost)
    # T 仓成交不改变永久仓成本，不应推高峰值。
    run_fill(account, date="2025-01-06", symbol="TEST", side="BUY", quantity=200, price=10.0, purpose="T")
    peaks.append(account.permanent_cost)
    run_fill(account, date="2025-01-07", symbol="TEST", side="SELL", quantity=700, price=10.5)
    peaks.append(account.permanent_cost)

    expected_peak = max(peaks)
    assert expected_peak > 15_000.0  # 两笔买入后含费成本 > 面额 15,000
    assert account.permanent_cost == pytest.approx(peaks[-1])
    assert max_invested_from_fills(account.trades) == pytest.approx(expected_peak)
    assert paper_account_metrics(account)["max_invested"] == pytest.approx(expected_peak)


def test_max_invested_closes_once_position_flat():
    """清仓后成本归零：第二次建仓的峰值独立计算，不回填已终结的成本。"""
    account = BrokerAccount(1_000_000, zero_fee_config())
    run_fill(account, date="2025-01-02", symbol="TEST", side="BUY", quantity=1000, price=10.0)
    run_fill(account, date="2025-01-03", symbol="TEST", side="SELL", quantity=1000, price=10.0)
    assert account.position("TEST").permanent_cost == pytest.approx(0.0)
    run_fill(account, date="2025-01-06", symbol="TEST", side="BUY", quantity=500, price=10.0)
    metrics = paper_account_metrics(account)
    assert metrics["max_invested"] == pytest.approx(10_000.0)


# ---------------------------------------------------------------------------
# 3. beta 摘要：与回测 data_info.beta 同键同口径
# ---------------------------------------------------------------------------


def test_paper_beta_summary_uses_backtest_caliber():
    account = BrokerAccount(1_000_000, zero_fee_config(), name="ETF 摘要")
    run_fill(account, date="2025-01-02", symbol="sz.159915", side="BUY", quantity=1000,
             price=2.5, purpose="BETA", reason="指数趋势向上，空仓建 beta 腿")
    run_fill(account, date="2025-01-03", symbol="sz.159915", side="SELL", quantity=400,
             price=2.6, purpose="BETA", reason="指数跌破20日线")
    run_fill(account, date="2025-01-06", symbol="sz.159915", side="SELL", quantity=600,
             price=2.7, purpose="BETA", reason="指数跌破20日线")

    summary = paper_beta_summary(account)
    assert set(summary) == BETA_KEYS
    assert summary["enabled"] is True
    assert summary["symbol"] == "sz.159915"
    assert summary["trade_count"] == 3
    assert summary["buy_count"] == 1
    assert summary["sell_count"] == 2
    # 与回测一致：entry_date 取首次建仓日，last_exit_date 取最近卖出日。
    assert summary["entry_date"] == "2025-01-02"
    assert summary["last_exit_date"] == "2025-01-06"
    assert summary["exit_reason"] == "指数跌破20日线"
    # 已实现口径：两笔卖出 realized_pnl 之和（40 + 120）。
    assert summary["pnl"] == pytest.approx(160.0)
    assert summary["position_qty"] == 0
    assert len(summary["fills"]) == 3
    for item in summary["fills"]:
        assert set(item) == BETA_FILL_KEYS
    assert summary["fills"][0] == {
        "date": "2025-01-02", "side": "BUY", "quantity": 1000,
        "price": 2.5, "fee": 0.0, "realized_pnl": 0.0,
    }
    # 与 metrics.beta_pnl / beta_trade_count 同源同值。
    metrics = paper_account_metrics(account)
    assert metrics["beta_pnl"] == summary["pnl"]
    assert metrics["beta_trade_count"] == summary["trade_count"]


def test_paper_beta_summary_reports_none_reason_while_holding():
    """持仓中（最新一笔 BETA 成交是买入）没有出场原因，与回测清空口径一致。"""
    account = BrokerAccount(1_000_000, zero_fee_config())
    run_fill(account, date="2025-01-02", symbol="sz.159915", side="BUY", quantity=1000,
             price=2.5, purpose="BETA")
    summary = paper_beta_summary(account)
    assert summary["exit_reason"] is None
    assert summary["last_exit_date"] is None
    assert summary["position_qty"] == 1000


# ---------------------------------------------------------------------------
# 4. 端点：/metrics、/beta（200 + 字段齐全 + 404 + 不塞轮询 payload）
# ---------------------------------------------------------------------------


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    return create_app()


def test_metrics_and_beta_endpoints(tmp_path, monkeypatch):
    application = _app(tmp_path, monkeypatch)
    client = TestClient(application)
    account = client.post("/api/paper/accounts", json={
        "name": "指标端点", "initial_cash": 1_000_000,
    }).json()

    # 空账户：指标端点先能跑通，且不出现除零 / 缺字段。
    empty = client.get(f"/api/paper/accounts/{account['id']}/metrics")
    assert empty.status_code == 200, empty.text
    empty_body = empty.json()
    assert set(empty_body) == {"metrics", "derived_from", "note"}
    assert empty_body["derived_from"] == "cashflow"
    assert "逐日净值" in empty_body["note"]
    assert set(empty_body["metrics"]) == METRICS_KEYS
    assert empty_body["metrics"]["net_profit"] == pytest.approx(0.0)
    assert empty_body["metrics"]["max_invested"] == pytest.approx(0.0)

    # 落一段真实流水后再取（直接走 storage，与线上账户落库结构一致）。
    stored = application.state.storage.get_paper(account["id"])
    broker_account = BrokerAccount.from_dict(stored)
    run_fill(broker_account, date="2025-01-02", symbol="TEST", side="BUY", quantity=1000, price=10.0)
    run_fill(broker_account, date="2025-01-03", symbol="sz.159915", side="BUY", quantity=1000,
             price=2.5, purpose="BETA", reason="指数趋势向上，空仓建 beta 腿")
    run_fill(broker_account, date="2025-01-06", symbol="sz.159915", side="SELL", quantity=1000,
             price=2.6, purpose="BETA", reason="指数跌破20日线")
    broker_account.strategy_state = {
        **broker_account.strategy_state, "round_entry_date": "2025-01-02",
    }
    application.state.storage.save_paper(broker_account.to_dict(include_internal=True))

    metrics_response = client.get(f"/api/paper/accounts/{account['id']}/metrics")
    assert metrics_response.status_code == 200, metrics_response.text
    metrics = metrics_response.json()["metrics"]
    assert set(metrics) == METRICS_KEYS
    assert metrics["total_trades"] == 3
    assert metrics["beta_trade_count"] == 2
    assert metrics["max_invested"] > 10_000.0  # 含买入费用
    assert metrics["net_profit"] == pytest.approx(
        metrics["final_equity"] + metrics["profit_withdrawn"] - metrics["initial_cash"]
    )

    beta_response = client.get(f"/api/paper/accounts/{account['id']}/beta")
    assert beta_response.status_code == 200, beta_response.text
    beta_body = beta_response.json()
    assert set(beta_body) == {"beta", "derived_from"}
    beta = beta_body["beta"]
    assert set(beta) == BETA_KEYS
    assert beta["trade_count"] == 2
    assert beta["entry_date"] == "2025-01-03"
    assert beta["last_exit_date"] == "2025-01-06"
    assert beta["exit_reason"] == "指数跌破20日线"
    assert metrics["beta_pnl"] == beta["pnl"]
    assert metrics["beta_trade_count"] == beta["trade_count"]

    # 轮询用的账户 payload 不塞指标（避免每次 15 秒轮询变重），但暴露只读派生字段。
    payload = client.get(f"/api/paper/accounts/{account['id']}").json()
    assert "metrics" not in payload
    assert "beta" not in payload
    assert payload["round_entry_date"] == "2025-01-02"
    assert payload["created_at"]
    assert payload["created_at_estimated"] is False


def test_metrics_and_beta_endpoints_404_for_missing_account(tmp_path, monkeypatch):
    application = _app(tmp_path, monkeypatch)
    client = TestClient(application)
    assert client.get("/api/paper/accounts/not-exist/metrics").status_code == 404
    assert client.get("/api/paper/accounts/not-exist/beta").status_code == 404


# ---------------------------------------------------------------------------
# 5. created_at 兼容旧 payload
# ---------------------------------------------------------------------------


def test_from_dict_backfills_created_at_for_legacy_payload():
    account = BrokerAccount(100_000, zero_fee_config(), name="旧账户")
    state = account.to_dict(include_internal=True)
    state.pop("created_at")
    state.pop("created_at_estimated")
    state["updated_at"] = "2025-03-04T05:06:07"
    restored = BrokerAccount.from_dict(state)
    # 用 payload 里的 updated_at 兜底，并标记为「推定」（后续年化指标要据此标注）。
    assert restored.created_at == "2025-03-04T05:06:07"
    assert restored.created_at_estimated is True
    assert restored.to_dict()["created_at"] == "2025-03-04T05:06:07"
    assert restored.to_dict()["created_at_estimated"] is True

    # 连 updated_at 都没有：用当前时间兜底，仍不炸。
    state.pop("updated_at")
    restored_again = BrokerAccount.from_dict(state)
    assert restored_again.created_at
    assert restored_again.created_at_estimated is True


def test_created_at_round_trips_for_new_account():
    account = BrokerAccount(100_000, zero_fee_config(), name="新账户")
    assert account.created_at_estimated is False
    restored = BrokerAccount.from_dict(account.to_dict(include_internal=True))
    assert restored.created_at == account.created_at
    assert restored.created_at_estimated is False


def test_round_entry_date_is_read_only_projection_of_strategy_state():
    account = BrokerAccount(100_000, zero_fee_config())
    assert account.to_dict()["round_entry_date"] is None
    account.strategy_state = {"round_entry_date": "2025-01-06", "armed": True}
    payload = account.to_dict()
    assert payload["round_entry_date"] == "2025-01-06"
    # strategy_state 本身不整体暴露到轮询 payload。
    assert "strategy_state" not in payload
