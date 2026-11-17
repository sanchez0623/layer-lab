"""盈利提现：把已落袋盈利从账户现金中取出（真实资金离场）。

覆盖正常路径 + 边界：
- 默认关闭（withdraw_mode="none"）不产生提现；
- 每笔提现：盈利 SELL 后按比例 / 按固定金额提取，亏损不触发；
- 每月提现：跨月边界按上一自然月已实现盈亏提取一次，不重复；
- 上限：以可提现金封顶，现金永不为负；
- 持久化：snapshot/restore/to_dict/from_dict 往返一致 + 旧账户向后兼容；
- 配置校验：非法 mode/method/ratio 抛 ValueError。
"""
from datetime import datetime

import pytest

from app.backtest import monthly_breakdown, performance_metrics
from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig, Withdrawal


def make_bar(year: int, month: int, day: int, price: float = 10.0, volume: float = 100_000) -> Bar:
    return Bar(
        timestamp=datetime(year, month, day, 9, 30), symbol="TEST",
        open=price, high=price * 1.02, low=price * 0.98, close=price,
        volume=volume, amount=price * volume, prev_close=10.0,
    )


def config(**kwargs) -> StrategyConfig:
    base = dict(slippage_bps=0, volume_participation=1.0)
    base.update(kwargs)
    return StrategyConfig(**base)


def request(side="BUY", quantity=1000, purpose="PERMANENT"):
    return OrderRequest(
        symbol="TEST", side=side, quantity=quantity, purpose=purpose,
        order_type="MARKET", execution_mode="NEXT_BAR",
    )


def run_buy_sell(account: BrokerAccount, buy_day=(2026, 1, 1), sell_day=(2026, 1, 2),
                 buy_price=10.0, sell_price=11.0, quantity=1000):
    """建仓 + 平仓一轮，返回成交列表。"""
    y, m, d = buy_day
    account.new_day(f"{y:04d}-{m:02d}-{d:02d}")
    account.submit(request("BUY", quantity))
    account.process_bar(make_bar(y, m, d, buy_price))
    y, m, d = sell_day
    account.new_day(f"{y:04d}-{m:02d}-{d:02d}")
    account.submit(request("SELL", quantity))
    return account.process_bar(make_bar(y, m, d, sell_price))


# ---- 默认关闭 -----------------------------------------------------------

def test_withdrawal_disabled_by_default():
    account = BrokerAccount(100_000, config())
    run_buy_sell(account)
    assert account.realized_pnl > 0
    assert account.total_withdrawn == 0.0
    assert account.withdrawals == []
    assert account._last_month_eval is None


# ---- 每笔提现 -----------------------------------------------------------

def test_per_trade_ratio_withdrawal():
    account = BrokerAccount(100_000, config(withdraw_mode="per_trade", withdraw_method="ratio", withdraw_ratio=0.2))
    sells = run_buy_sell(account)
    realized = account.realized_pnl
    assert realized > 0
    assert len(account.withdrawals) == 1
    record = account.withdrawals[0]
    assert record.mode == "per_trade"
    assert record.method == "ratio"
    assert record.period_profit == pytest.approx(realized)
    assert record.amount == pytest.approx(realized * 0.2)
    assert record.trigger == sells[0].id
    assert account.total_withdrawn == pytest.approx(realized * 0.2)
    # 提现后现金 = 现金（未提现口径） - 提取额，且不再等于「初始+净利」。
    assert account.cash < 100_000 + realized


def test_per_trade_fixed_withdrawal_full_amount():
    # 固定金额：即使当期盈利 < 固定金额，仍提取完整固定金额（仅以现金余额封顶）。
    account = BrokerAccount(100_000, config(withdraw_mode="per_trade", withdraw_method="fixed", withdraw_fixed_amount=5000))
    run_buy_sell(account)
    assert account.realized_pnl > 0
    assert account.realized_pnl < 5000
    assert len(account.withdrawals) == 1
    assert account.withdrawals[0].amount == pytest.approx(5000)
    assert account.total_withdrawn == pytest.approx(5000)


def test_per_trade_fixed_withdrawal_capped_by_cash():
    # 可用现金不足时以 available_cash 封顶，现金不为负。
    account = BrokerAccount(2_000, config(withdraw_mode="per_trade", withdraw_method="fixed", withdraw_fixed_amount=5000))
    run_buy_sell(account, quantity=100)
    assert len(account.withdrawals) == 1
    record = account.withdrawals[0]
    # 卖出后现金 2089.32（未提现口径），固定金额 5000 被可提现金封顶。
    assert record.amount == pytest.approx(2_089.32, abs=0.01)
    assert account.cash == pytest.approx(0.0, abs=1e-6)


def test_per_trade_no_withdrawal_on_loss():
    account = BrokerAccount(100_000, config(withdraw_mode="per_trade", withdraw_method="ratio", withdraw_ratio=0.2))
    run_buy_sell(account, sell_price=9.0)
    assert account.realized_pnl < 0
    assert account.withdrawals == []
    assert account.total_withdrawn == 0.0


def test_per_trade_zero_profit_no_withdrawal():
    account = BrokerAccount(100_000, config(withdraw_mode="per_trade", withdraw_method="ratio", withdraw_ratio=0.2))
    run_buy_sell(account, sell_price=10.0)
    assert account.realized_pnl < 0  # 手续费致实际为亏
    assert account.withdrawals == []


# ---- 每月提现 -----------------------------------------------------------

def test_monthly_ratio_withdrawal_at_boundary():
    account = BrokerAccount(100_000, config(withdraw_mode="monthly", withdraw_method="ratio", withdraw_ratio=0.2))
    # 一月内盈利一轮，当月不提取。
    run_buy_sell(account, buy_day=(2026, 1, 1), sell_day=(2026, 1, 2))
    jan_realized = account.realized_pnl
    assert account.withdrawals == []
    assert account.total_withdrawn == 0.0
    # 跨入二月：按一月已实现盈亏 × 比例提取一次。
    account.new_day("2026-02-02")
    account.process_bar(make_bar(2026, 2, 2))
    assert len(account.withdrawals) == 1
    record = account.withdrawals[0]
    assert record.mode == "monthly"
    assert record.trigger == "2026-01"
    assert record.period_profit == pytest.approx(jan_realized)
    assert record.amount == pytest.approx(jan_realized * 0.2)
    # 同月后续交易日不再重复评估。
    account.new_day("2026-02-03")
    account.process_bar(make_bar(2026, 2, 3))
    assert len(account.withdrawals) == 1


def test_monthly_fixed_withdrawal_exceeds_profit():
    account = BrokerAccount(100_000, config(withdraw_mode="monthly", withdraw_method="fixed", withdraw_fixed_amount=5000))
    run_buy_sell(account, buy_day=(2026, 1, 1), sell_day=(2026, 1, 2))
    assert 0 < account.realized_pnl < 5000
    account.new_day("2026-02-02")
    account.process_bar(make_bar(2026, 2, 2))
    assert len(account.withdrawals) == 1
    assert account.withdrawals[0].amount == pytest.approx(5000)


def test_monthly_skip_when_no_profit():
    # 当月不盈利（含亏损/持平）且开关开启 → 不提取。
    account = BrokerAccount(100_000, config(withdraw_mode="monthly", withdraw_method="fixed", withdraw_fixed_amount=5000, withdraw_skip_when_no_profit=True))
    run_buy_sell(account, buy_day=(2026, 1, 1), sell_day=(2026, 1, 2), sell_price=9.0)
    assert account.realized_pnl < 0
    account.new_day("2026-02-02")
    account.process_bar(make_bar(2026, 2, 2))
    assert account.withdrawals == []
    assert account._last_month_eval == "2026-01"  # 已评估过，即便跳过


def test_monthly_no_skip_when_profit_negative_and_switch_off():
    # 无盈利但开关关闭 → 仍按固定金额提取（仅以现金余额封顶）。
    account = BrokerAccount(100_000, config(withdraw_mode="monthly", withdraw_method="fixed", withdraw_fixed_amount=5000, withdraw_skip_when_no_profit=False))
    run_buy_sell(account, buy_day=(2026, 1, 1), sell_day=(2026, 1, 2), sell_price=9.0)
    assert account.realized_pnl < 0
    account.new_day("2026-02-02")
    account.process_bar(make_bar(2026, 2, 2))
    assert len(account.withdrawals) == 1
    assert account.withdrawals[0].amount == pytest.approx(5000)


def test_monthly_uses_previous_full_month_not_current():
    # 2 月内先盈利再跨 3 月：3 月基准是「2 月」已实现盈亏，而非累计。
    account = BrokerAccount(100_000, config(withdraw_mode="monthly", withdraw_method="ratio", withdraw_ratio=0.2))
    run_buy_sell(account, buy_day=(2026, 2, 1), sell_day=(2026, 2, 3))
    feb_realized = account.realized_pnl
    account.new_day("2026-03-02")
    account.process_bar(make_bar(2026, 3, 2))
    assert len(account.withdrawals) == 1
    assert account.withdrawals[0].trigger == "2026-02"
    assert account.withdrawals[0].amount == pytest.approx(feb_realized * 0.2)


# ---- 上限与现金不为负 ---------------------------------------------------

def test_withdrawal_never_negative_cash():
    # 固定金额远超现金：提现被可提现金封顶，现金归零但永不为负。
    account = BrokerAccount(2_000, config(withdraw_mode="per_trade", withdraw_method="fixed", withdraw_fixed_amount=999_999))
    run_buy_sell(account, quantity=100)
    assert len(account.withdrawals) == 1
    assert account.withdrawals[0].amount == pytest.approx(2_089.32, abs=0.01)
    assert account.cash >= 0
    assert account.available_cash >= 0


def test_execute_withdrawal_skips_when_no_cash():
    # 无可提现金时提现跳过，不产生记录。
    account = BrokerAccount(100_000, config(withdraw_mode="monthly", withdraw_method="fixed", withdraw_fixed_amount=5000))
    account.cash = 0
    assert account._execute_withdrawal(5000, "monthly", 100, "2026-01", "2026-02-02") is None
    assert account.withdrawals == []
    assert account.total_withdrawn == 0.0


# ---- 持久化 -------------------------------------------------------------

def test_withdrawal_snapshot_restore_roundtrip():
    account = BrokerAccount(100_000, config(withdraw_mode="per_trade", withdraw_method="ratio", withdraw_ratio=0.2))
    run_buy_sell(account)
    account._last_month_eval = "2026-01"
    state = account.snapshot_state()
    restored = BrokerAccount(100_000, config(withdraw_mode="per_trade", withdraw_method="ratio", withdraw_ratio=0.2))
    restored.restore_state(state)
    assert restored.total_withdrawn == pytest.approx(account.total_withdrawn)
    assert len(restored.withdrawals) == len(account.withdrawals)
    assert restored.withdrawals[0].to_dict() == account.withdrawals[0].to_dict()
    assert restored._last_month_eval == "2026-01"


def test_withdrawal_to_dict_from_dict_roundtrip():
    account = BrokerAccount(100_000, config(withdraw_mode="per_trade", withdraw_method="ratio", withdraw_ratio=0.2))
    run_buy_sell(account)
    payload = account.to_dict(include_internal=True)
    assert "total_withdrawn" in payload and "withdrawals" in payload
    rebuilt = BrokerAccount.from_dict(payload)
    assert rebuilt.total_withdrawn == pytest.approx(account.total_withdrawn)
    assert len(rebuilt.withdrawals) == len(account.withdrawals)
    assert rebuilt._next_withdrawal_number == account._next_withdrawal_number
    assert rebuilt.withdrawals[0].date == account.withdrawals[0].date


def test_withdrawal_legacy_account_without_keys():
    # 旧账户快照无提现字段：默认关闭、0 累计、空明细。
    legacy = {
        "initial_cash": 100_000, "name": "旧账户", "id": "legacy-1",
        "config": {}, "cash": 100_000, "trades": [], "orders": [],
    }
    account = BrokerAccount.from_dict(legacy)
    assert account.total_withdrawn == 0.0
    assert account.withdrawals == []
    assert account._next_withdrawal_number == 1
    assert account._last_month_eval is None


# ---- 配置校验 -----------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("withdraw_mode", "invalid"),
    ("withdraw_method", "invalid"),
    ("withdraw_ratio", 0.0),
    ("withdraw_ratio", 1.5),
    ("withdraw_fixed_amount", -1),
])
def test_withdrawal_config_validation(field, value):
    with pytest.raises(ValueError):
        StrategyConfig(**{field: value})


def test_withdrawal_defaults():
    cfg = StrategyConfig()
    assert cfg.withdraw_mode == "none"
    assert cfg.withdraw_method == "ratio"
    assert cfg.withdraw_ratio == 0.2
    assert cfg.withdraw_fixed_amount == 5000
    assert cfg.withdraw_skip_when_no_profit is True


# ---- 回测报告指标 ---------------------------------------------------------

def test_performance_metrics_include_withdrawal():
    curve = [{"date": "2026-01-02", "equity": 110_000.0, "market_value": 0.0},
             {"date": "2026-01-03", "equity": 119_663.15, "market_value": 0.0}]
    metrics = performance_metrics(
        equity_curve=curve, initial_cash=100_000, rounds=[], trades=[],
        total_fees=0.0, t_net_profit=0.0, max_invested=0.0,
        max_float_loss=0.0, buy_hold_return=0.0,
        total_withdrawn=19_663.15, withdrawn_count=4,
    )
    assert metrics["total_withdrawn"] == pytest.approx(19_663.15)
    assert metrics["withdrawn_count"] == 4
    # 未传提现明细时按「全部出金视为盈利出金」兜底：净利/总收益应加回出金金额
    assert metrics["profit_withdrawn"] == pytest.approx(19_663.15)
    assert metrics["net_profit"] == pytest.approx(119_663.15 + 19_663.15 - 100_000)
    assert metrics["total_return"] == pytest.approx((119_663.15 + 19_663.15) / 100_000 - 1)


def test_performance_metrics_profit_withdrawn_from_details():
    """盈利出金只统计以盈利为基准（period_profit>0）的提现，并加回净利/总收益。

    提现明细传入时：profit_withdrawn 精确过滤；total_withdrawn 若未显式传则由明细补全。
    """
    curve = [{"date": "2026-01-02", "equity": 100_000.0, "market_value": 0.0},
             {"date": "2026-01-03", "equity": 105_000.0, "market_value": 0.0}]
    withdrawals = [
        {"id": "W1", "date": "2026-01-03", "amount": 3_000.0, "period_profit": 5_000.0},
        {"id": "W2", "date": "2026-01-03", "amount": 2_000.0, "period_profit": 0.0},
    ]
    metrics = performance_metrics(
        equity_curve=curve, initial_cash=100_000, rounds=[], trades=[],
        total_fees=0.0, t_net_profit=0.0, max_invested=0.0,
        max_float_loss=0.0, buy_hold_return=0.0,
        withdrawals=withdrawals,
    )
    assert metrics["profit_withdrawn"] == pytest.approx(3_000.0)
    assert metrics["total_withdrawn"] == pytest.approx(5_000.0)
    assert metrics["net_profit"] == pytest.approx(105_000.0 + 3_000.0 - 100_000)
    assert metrics["total_return"] == pytest.approx((105_000.0 + 3_000.0) / 100_000 - 1)


def test_monthly_breakdown_includes_withdrawals():
    """月度统计把盈利出金金额计入当月盈亏与累计收益（统计数据层面考虑出金）。"""
    curve = [
        {"date": "2026-01-02", "equity": 100_000.0, "market_value": 0.0},
        {"date": "2026-01-30", "equity": 110_000.0, "market_value": 0.0},
        {"date": "2026-02-03", "equity": 108_000.0, "market_value": 0.0},
    ]
    withdrawals = [
        {"id": "W1", "date": "2026-01-20", "amount": 5_000.0, "period_profit": 10_000.0},
        {"id": "W2", "date": "2026-02-02", "amount": 2_000.0, "period_profit": 3_000.0},
    ]
    monthly = monthly_breakdown(curve, 100_000, withdrawals)
    jan, feb = monthly[0], monthly[1]
    assert jan["month"] == "2026-01"
    assert jan["pnl"] == pytest.approx(15_000.0)          # +10k 权益 + 5k 出金
    assert jan["cumulative_return"] == pytest.approx(0.15)  # (110k+5k)/100k-1
    assert feb["month"] == "2026-02"
    assert feb["pnl"] == pytest.approx(0.0)                # -2k 权益 + 2k 出金
    assert feb["cumulative_return"] == pytest.approx(0.15)  # (108k+7k)/100k-1


def test_performance_metrics_withdrawal_default_zero():
    curve = [{"date": "2026-01-02", "equity": 100_000.0, "market_value": 0.0},
             {"date": "2026-01-03", "equity": 100_500.0, "market_value": 0.0}]
    metrics = performance_metrics(
        equity_curve=curve, initial_cash=100_000, rounds=[], trades=[],
        total_fees=0.0, t_net_profit=0.0, max_invested=0.0,
        max_float_loss=0.0, buy_hold_return=0.0,
    )
    assert metrics["total_withdrawn"] == 0.0
    assert metrics["withdrawn_count"] == 0
