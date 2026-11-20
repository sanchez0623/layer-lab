"""模拟盘「读时派生」指标（阶段 1）：只吃账户已有流水，不新增业务表 / 历史快照。

设计约束（AGENTS 架构原则 1：单一真相源）：
- 与回测同名的字段必须同口径：``beta`` 摘要对齐回测 ``data_info.beta``；
  ``net_profit / total_return / profit_withdrawn`` 对齐回测 ``performance_metrics``
  的「盈利提现加回」口径；
- ``max_invested`` 的成本分配复用 ``broker.add_position_buy_cost`` /
  ``allocate_ledger_on_sell``（撮合 ``_apply_execution`` 用的同一份实现），
  不在模拟盘复制第二套成本账本算法。

本模块**刻意不返回**依赖逐日净值 / 逐日持仓序列的指标（留给逐日净值落库阶段）：
``max_drawdown / sharpe_ratio / volatility / calmar_ratio / annualized_return /
xirr_annualized / underwater / cashflow_adjusted / exposure / exposure_total /
exposure_stock / exposure_beta / idle_ratio / monthly / robustness /
max_float_loss / equity_curve``。这些字段若先用「把提现当亏损」或「起止点近似」的
口径定义下来，后续净值落库时会与回测同名指标冲突，故本期只交付能精确算出来的部分。
"""

from __future__ import annotations

from typing import Any, Iterable

from .broker import BrokerAccount, add_position_buy_cost, allocate_ledger_on_sell
from .domain import Fill, money


def _fill_date(fill: Fill) -> str:
    """成交日期（YYYY-MM-DD）：与回测 beta fills 的 date 口径一致。"""
    return str(fill.timestamp)[:10]


def max_invested_from_fills(fills: Iterable[Fill]) -> float:
    """按成交顺序重放永久仓成本账本，返回「永久仓成本（含买入费用）」的峰值。

    只重放 PERMANENT 账本：T 仓买入不计入永久成本、T 卖出也不改变永久成本
    （``_apply_execution`` 里 T 卖出是「永久仓减仓后又等量加回、成本原样保留」），
    BETA 腿完全独立。成本累加 / 分摊复用 ``broker.add_position_buy_cost`` 与
    ``allocate_ledger_on_sell``，与撮合是同一份实现，不复制第二套口径。

    口径说明：模拟盘没有逐日快照，这里取「每笔成交后的永久仓成本」峰值；回测
    ``max_invested`` 取「每根 bar 撮合完成后的持仓成本」峰值，两者只在「同一根 bar
    内先买后卖」的瞬时峰值上有差异（模拟盘会记住买后那一刻的峰值，更保守）。
    """
    ledger: dict[str, tuple[int, float]] = {}
    peak = 0.0
    for fill in fills:
        if fill.purpose != "PERMANENT":
            continue
        qty, cost = ledger.get(fill.symbol, (0, 0.0))
        if fill.side == "BUY":
            qty += int(fill.quantity)
            cost, _ = add_position_buy_cost(
                cost, 0.0, float(fill.amount), float(fill.total_fee),
            )
        else:
            if qty <= 0:
                continue
            sold = min(int(fill.quantity), qty)
            cost_allocated, _ = allocate_ledger_on_sell(cost, 0.0, sold, qty)
            cost = money(max(0.0, cost - cost_allocated))
            qty -= sold
            if qty == 0:
                # 与 _apply_execution 的「持仓清零 → 成本归零」口径一致，避免浮点残值。
                cost = 0.0
        ledger[fill.symbol] = (qty, cost)
        peak = max(peak, sum(item[1] for item in ledger.values()))
    return money(peak)


def paper_account_metrics(account: BrokerAccount) -> dict[str, Any]:
    """模拟盘账户指标（只含不依赖逐日净值序列的字段）。

    提现口径与回测 ``performance_metrics`` 一致：只有「以盈利为基准」
    （``withdrawals[].period_profit > 0``）的提现才加回净利，不能把亏损月份的
    固定金额提现也算成收益。``net_profit = equity + profit_withdrawn - initial_cash``。
    """
    initial_cash = money(account.initial_cash)
    final_equity = money(account.equity)
    withdrawals = list(account.withdrawals or [])
    profit_withdrawn = money(sum(
        float(item.amount) for item in withdrawals
        if float(item.period_profit or 0.0) > 0
    ))
    net_profit = money(final_equity + profit_withdrawn - initial_cash)
    trades = list(account.trades or [])
    beta_fills = [fill for fill in trades if fill.purpose == "BETA"]
    return {
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "total_fees": money(account.total_fees),
        "t_net_profit": money(account.t_net_profit),
        "total_withdrawn": money(account.total_withdrawn),
        "withdrawn_count": len(withdrawals),
        "profit_withdrawn": profit_withdrawn,
        "net_profit": net_profit,
        "total_return": net_profit / initial_cash if initial_cash > 0 else 0.0,
        "total_trades": len(trades),
        "t_trade_count": sum(1 for fill in trades if fill.purpose == "T"),
        # ETF beta 腿：与回测 metrics.beta_pnl / data_info.beta.pnl 同口径
        # （BETA 卖出的 realized_pnl 求和）。
        "beta_pnl": money(sum(
            float(fill.realized_pnl) for fill in beta_fills if fill.side == "SELL"
        )),
        "beta_trade_count": len(beta_fills),
        "max_invested": max_invested_from_fills(trades),
    }


def _beta_exit_reason(account: BrokerAccount, last_fill: Fill) -> str | None:
    """最近一次 ETF 清仓原因：取该笔卖出成交对应委托的 reason。

    与回测 ``_beta_summary`` 同口径：回测在「买入成交」时把 ``_beta_last_exit_reason``
    清空，所以只有最后一笔 BETA 成交仍是卖出时才有出场原因；模拟盘的出场原因写在
    ``apply_beta_bar`` 提交的卖单 ``reason`` 上（Fill.order_id → Order.reason）。
    """
    for order in account.orders or []:
        if order.id == last_fill.order_id:
            return order.reason or None
    return None


def paper_beta_summary(account: BrokerAccount) -> dict[str, Any]:
    """ETF beta 腿摘要：与回测 ``data_info.beta`` 同键名同口径。

    覆盖区间字段（``source / coverage_start / coverage_end / partial_coverage``）不在
    本期范围——模拟盘没有回测的数据集元信息，强行编一个只会制造「两边数值不同」的
    误解（见审计文档 C07，P2）。
    """
    fills = [fill for fill in (account.trades or []) if fill.purpose == "BETA"]
    buys = [fill for fill in fills if fill.side == "BUY"]
    sells = [fill for fill in fills if fill.side == "SELL"]
    exit_reason = _beta_exit_reason(account, fills[-1]) if fills and fills[-1].side == "SELL" else None
    return {
        "enabled": bool(account.config.etf_beta_enabled),
        "symbol": account.config.etf_symbol,
        "trade_count": len(fills),
        "buy_count": len(buys),
        "sell_count": len(sells),
        # 与回测一致：首次建仓日（不是「本轮」建仓日）；最近一次卖出的日期即最近清仓日。
        "entry_date": _fill_date(buys[0]) if buys else None,
        "last_exit_date": _fill_date(sells[-1]) if sells else None,
        "exit_reason": exit_reason,
        "pnl": money(sum(float(fill.realized_pnl) for fill in sells)),
        "position_qty": sum(int(position.beta_qty) for position in account.positions.values()),
        # 逐笔成交（紧凑字段）：与回测 data_info.beta.fills 同键，供后续轨迹/面板复用。
        "fills": [
            {
                "date": _fill_date(fill), "side": fill.side,
                "quantity": int(fill.quantity), "price": fill.price,
                "fee": money(fill.total_fee), "realized_pnl": money(fill.realized_pnl),
            }
            for fill in fills
        ],
    }
