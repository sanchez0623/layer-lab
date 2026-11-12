from __future__ import annotations

import math
import uuid
from collections import defaultdict
from datetime import datetime
from statistics import mean, stdev
from typing import Any

from .broker import BrokerAccount
from .domain import Bar, Fill, StrategyConfig, money, utc_now
from .strategy import LayeredTStrategy


def performance_metrics(
    equity_curve: list[dict[str, Any]], initial_cash: float,
    rounds: list[dict[str, Any]], trades: list[dict[str, Any]],
    total_fees: float, t_net_profit: float, max_invested: float,
    max_float_loss: float, buy_hold_return: float,
) -> dict[str, Any]:
    if not equity_curve:
        raise ValueError("净值曲线为空")
    equities = [float(item["equity"]) for item in equity_curve]
    final_equity = equities[-1]
    net_profit = money(final_equity - initial_cash)
    total_return = final_equity / initial_cash - 1
    start = datetime.fromisoformat(str(equity_curve[0]["date"])).date()
    end = datetime.fromisoformat(str(equity_curve[-1]["date"])).date()
    elapsed_days = max(1, (end - start).days)
    annualized = (final_equity / initial_cash) ** (365 / elapsed_days) - 1 if final_equity > 0 else -1.0
    returns = [current / previous - 1 for previous, current in zip(equities, equities[1:]) if previous > 0]
    volatility = stdev(returns) * math.sqrt(252) if len(returns) >= 2 else 0.0
    sharpe = mean(returns) / stdev(returns) * math.sqrt(252) if len(returns) >= 2 and stdev(returns) > 0 else 0.0
    peak = equities[0]
    max_drawdown = 0.0
    for item, equity in zip(equity_curve, equities):
        peak = max(peak, equity)
        drawdown = equity / peak - 1 if peak else 0.0
        item["drawdown"] = drawdown
        max_drawdown = max(max_drawdown, -drawdown)
    calmar = annualized / max_drawdown if max_drawdown > 0 else 0.0
    closed = [item for item in rounds if item["status"] == "CLOSED"]
    wins = [item for item in closed if item["pnl"] > 0]
    losses = [item for item in closed if item["pnl"] < 0]
    gross_profit = sum(item["pnl"] for item in wins)
    gross_loss = -sum(item["pnl"] for item in losses)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    exposure = mean(
        item["market_value"] / item["equity"] if item["equity"] else 0.0
        for item in equity_curve
    )
    return {
        "initial_cash": money(initial_cash), "final_equity": money(final_equity),
        "net_profit": net_profit, "total_return": total_return,
        "annualized_return": annualized, "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe, "volatility": volatility, "calmar_ratio": calmar,
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "profit_factor": profit_factor, "total_rounds": len(rounds),
        "closed_rounds": len(closed), "total_trades": len(trades),
        "total_fees": money(total_fees), "t_net_profit": money(t_net_profit),
        "t_trade_count": sum(1 for item in trades if item["purpose"] == "T"),
        "max_invested": money(max_invested), "exposure": exposure,
        "max_float_loss": money(max_float_loss), "buy_hold_return": buy_hold_return,
    }


class BacktestEngine:
    def __init__(
        self, daily: list[Bar], intraday: list[Bar] | None = None,
        index: list[Bar] | None = None, initial_cash: float = 4_000_000.0,
        config: StrategyConfig | None = None, start_date: str | None = None,
        end_date: str | None = None, name: str = "分层加仓做T回测",
    ) -> None:
        self.daily_all = sorted(daily, key=lambda item: item.timestamp)
        if not self.daily_all:
            raise ValueError("日线数据为空")
        self.intraday_all = sorted(intraday or [], key=lambda item: item.timestamp)
        self.index_all = sorted(index or [], key=lambda item: item.timestamp)
        self.config = config or StrategyConfig()
        self.account = BrokerAccount(initial_cash=initial_cash, config=self.config, name=name)
        self.strategy = LayeredTStrategy(self.config)
        self.start_date = datetime.fromisoformat(start_date).date() if start_date else None
        self.end_date = datetime.fromisoformat(end_date).date() if end_date else None
        self.name = name
        self.events: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.rounds: list[dict[str, Any]] = []
        self.current_round: dict[str, Any] | None = None
        self.max_invested = 0.0
        self.max_float_loss = 0.0

    def _selected_days(self) -> list[int]:
        result = []
        for index, bar in enumerate(self.daily_all):
            date = bar.timestamp.date()
            if self.start_date and date < self.start_date:
                continue
            if self.end_date and date > self.end_date:
                continue
            result.append(index)
        if not result:
            raise ValueError("指定日期范围内没有日线")
        return result

    def _new_round(self, fill: Fill, session_index: int) -> None:
        self.current_round = {
            "id": f"R{len(self.rounds) + 1:03d}", "symbol": fill.symbol,
            "start_date": fill.timestamp[:10], "end_date": None, "status": "OPEN",
            "start_equity": money(self.account.equity + fill.total_fee),
            "installment_count": 0, "invested": 0.0, "max_invested": 0.0,
            "max_float_loss": 0.0, "float_loss_at_2_4m": None,
            "float_loss_pct_at_2_4m": None, "float_loss_over_6_23pct": False,
            "deep_loss_start_date": None, "deep_loss_recovery_days": None,
            "deep_loss_threshold": self.config.deep_loss_threshold,
            "t_net_profit_start": self.account.t_net_profit,
            "t_net_profit": 0.0, "pnl": 0.0, "close_reason": None,
            "_deep_loss_start_session": None,
        }
        self.strategy.round_t_start = self.account.t_net_profit
        self.events.append({"date": fill.timestamp, "type": "ROUND_START", "round_id": self.current_round["id"]})

    def _update_round(self, bar: Bar, session_index: int) -> None:
        position = self.account.position(bar.symbol)
        invested = position.permanent_cost
        floating = position.permanent_qty * bar.close - invested
        self.max_invested = max(self.max_invested, invested)
        self.max_float_loss = max(self.max_float_loss, -floating)
        if not self.current_round:
            return
        item = self.current_round
        item["installment_count"] = self.account.sequence_index
        item["invested"] = money(invested)
        item["max_invested"] = money(max(item["max_invested"], invested))
        item["max_float_loss"] = money(max(item["max_float_loss"], -floating))
        item["t_net_profit"] = money(self.account.t_net_profit - item["t_net_profit_start"])
        if invested > 0:
            loss_pct = floating / invested
            if invested >= 2_400_000 and item["float_loss_at_2_4m"] is None:
                item["float_loss_at_2_4m"] = money(floating)
                item["float_loss_pct_at_2_4m"] = loss_pct
                self.events.append({
                    "date": bar.date, "type": "INVESTED_2_4M", "float_loss": money(floating),
                    "float_loss_pct": loss_pct, "round_id": item["id"],
                })
            if loss_pct <= -0.0623 and not item["float_loss_over_6_23pct"]:
                item["float_loss_over_6_23pct"] = True
                self.events.append({
                    "date": bar.date, "type": "FLOAT_LOSS_OVER_6_23PCT",
                    "float_loss_pct": loss_pct, "round_id": item["id"],
                })
            if loss_pct <= self.config.deep_loss_threshold and item["deep_loss_start_date"] is None:
                item["deep_loss_start_date"] = bar.date
                item["_deep_loss_start_session"] = session_index
            if (
                item["deep_loss_start_date"] is not None and item["deep_loss_recovery_days"] is None
                and floating >= 0 and session_index > item["_deep_loss_start_session"]
            ):
                item["deep_loss_recovery_days"] = session_index - item["_deep_loss_start_session"]

    def _handle_fills(self, fills: list[Fill], session_index: int, bar: Bar) -> None:
        for fill in fills:
            if fill.side == "BUY" and fill.purpose == "PERMANENT" and self.current_round is None:
                self._new_round(fill, session_index)
            self.strategy.on_fill(fill, session_index)
            self.events.append({
                "date": fill.timestamp, "type": "FILL", "fill_id": fill.id,
                "side": fill.side, "purpose": fill.purpose, "quantity": fill.quantity,
                "price": fill.price, "fee": fill.total_fee,
            })
            self._update_round(bar, session_index)
        position = self.account.position(bar.symbol)
        if self.current_round and position.total_qty == 0:
            item = self.current_round
            item["status"] = "CLOSED"
            item["end_date"] = bar.date
            item["pnl"] = money(self.account.equity - item["start_equity"])
            item["close_reason"] = self.strategy.close_reason or "整体止盈/事件熔断"
            item["t_net_profit"] = money(self.account.t_net_profit - item["t_net_profit_start"])
            item.pop("_deep_loss_start_session", None)
            self.events.append({
                "date": bar.date, "type": "ROUND_CLOSED", "round_id": item["id"],
                "pnl": item["pnl"], "reason": item["close_reason"],
            })
            self.rounds.append(item)
            self.current_round = None
            self.strategy.reset_round(self.account)

    def _submit(self, requests, timestamp: datetime) -> None:
        for request in requests:
            if request.quantity < 100:
                continue
            order = self.account.submit(request, timestamp)
            self.events.append({
                "date": timestamp.isoformat(), "type": "SIGNAL", "order_id": order.id,
                "symbol": order.symbol, "side": order.side, "purpose": order.purpose,
                "quantity": order.quantity, "reason": order.reason, "status": order.status,
                "execution_mode": order.execution_mode,
            })

    def run(self) -> dict[str, Any]:
        selected = self._selected_days()
        intraday_by_date: dict[str, list[Bar]] = defaultdict(list)
        for bar in self.intraday_all:
            intraday_by_date[bar.date].append(bar)
        index_by_date: dict[str, list[Bar]] = defaultdict(list)
        for bar in self.index_all:
            index_by_date[bar.date].append(bar)
        if not self.intraday_all:
            self.warnings.append("未提供intraday：T交易和tick级风控降级；仅收盘出信号，次一交易日开盘执行，不使用未来价。")
        else:
            actual = sorted({bar.timeframe for bar in self.intraday_all})
            self.warnings.append(f"intraday实际粒度为{','.join(actual)}；信号在bar收盘形成，下一bar开盘撮合。")
        if not self.index_all:
            self.warnings.append("未提供指数数据：大盘门无法计算，按中性通过处理，属于降级模式。")
        if any(getattr(bar, "symbol", "").startswith("SYNTH") for bar in self.daily_all):
            self.warnings.append("数据为确定性synthetic演示行情，不代表任何真实历史。")

        equity_curve: list[dict[str, Any]] = []
        used_dates: list[str] = []
        missing_intraday_dates: list[str] = []
        first_open = self.daily_all[selected[0]].open
        for session_index in selected:
            daily = self.daily_all[session_index]
            history = self.daily_all[:session_index + 1]
            index_history = [
                item for item in self.index_all if item.timestamp.date() <= daily.timestamp.date()
            ]
            bars_today = intraday_by_date.get(daily.date, [])
            has_intraday = bool(bars_today)
            self.account.new_day(daily.date, cancel_previous=False)
            self.strategy.start_day(has_intraday)
            used_dates.append(daily.date)

            if has_intraday:
                recent: list[Bar] = []
                previous_daily = history[:-1]
                for bar_index, bar in enumerate(bars_today):
                    modes = {"NEXT_BAR", "NEXT_OPEN"} if bar_index == 0 else {"NEXT_BAR"}
                    fills = self.account.process_bar(bar, modes=modes)
                    self._handle_fills(fills, session_index, daily)
                    if bar_index == 0:
                        self.account.cancel_open_orders("T+1开盘未成交取消", modes={"NEXT_OPEN"})
                    self.account.mark(bar.symbol, bar.close)
                    recent.append(bar)
                    requests = self.strategy.on_intraday(previous_daily, recent, bar, self.account)
                    self._submit(requests, bar.timestamp)
                self.account.cancel_open_orders("日内订单日终取消", modes={"NEXT_BAR"})
            else:
                fills = self.account.process_bar(daily, modes={"NEXT_OPEN"})
                self._handle_fills(fills, session_index, daily)
                self.account.cancel_open_orders("T+1开盘未成交取消", modes={"NEXT_OPEN"})
                if self.intraday_all:
                    missing_intraday_dates.append(daily.date)

            self.account.mark(daily.symbol, daily.close)
            self._update_round(daily, session_index)
            requests = self.strategy.on_daily(history, index_history, self.account, session_index, has_intraday)
            self._submit(requests, daily.timestamp)
            position = self.account.position(daily.symbol)
            equity_curve.append({
                "date": daily.date, "equity": self.account.equity, "cash": self.account.cash,
                "market_value": self.account.market_value, "invested": position.permanent_cost,
                "drawdown": 0.0, "position_qty": position.total_qty,
            })

        if missing_intraday_dates:
            self.warnings.append(
                f"{len(missing_intraday_dates)}个交易日无1分钟数据（{missing_intraday_dates[0]}起）；"
                "这些日期仅执行既有次日开盘订单，不做盘中信号。"
            )
        if self.current_round:
            item = self.current_round
            item["pnl"] = money(self.account.equity - item["start_equity"])
            item["close_reason"] = None
            item.pop("_deep_loss_start_session", None)
            self.rounds.append(item)
            self.current_round = None
        self.account.cancel_open_orders("回测结束")
        last_close = self.daily_all[selected[-1]].close
        metrics = performance_metrics(
            equity_curve, self.account.initial_cash, self.rounds,
            [item.to_dict() for item in self.account.trades], self.account.total_fees,
            self.account.t_net_profit, self.max_invested, self.max_float_loss,
            last_close / first_open - 1,
        )
        data_info = {
            "daily_timeframe": "daily", "daily_rows_total": len(self.daily_all),
            "daily_rows_used": len(selected), "intraday_timeframe": (
                self.intraday_all[0].timeframe if self.intraday_all else None
            ),
            "intraday_rows": len(self.intraday_all), "index_rows": len(self.index_all),
            "is_synthetic": any(bar.symbol.startswith("SYNTH") for bar in self.daily_all),
            "data_label": "synthetic" if any(bar.symbol.startswith("SYNTH") for bar in self.daily_all) else "market_source",
            "actual_granularity": self.intraday_all[0].timeframe if self.intraday_all else "daily_only",
            "execution": "T日收盘信号/T+1开盘；intraday信号下一bar；无未来价",
            "start": used_dates[0], "end": used_dates[-1],
        }
        return {
            "id": str(uuid.uuid4()), "name": self.name, "status": "COMPLETED",
            "created_at": utc_now(), "config": {
                **self.config.to_dict(), "initial_cash": self.account.initial_cash,
                "start_date": self.start_date.isoformat() if self.start_date else None,
                "end_date": self.end_date.isoformat() if self.end_date else None,
            },
            "metrics": metrics, "equity_curve": equity_curve,
            "trades": [item.to_dict() for item in self.account.trades],
            "orders": [item.to_dict() for item in self.account.orders],
            "rounds": self.rounds, "events": self.events, "warnings": list(dict.fromkeys(self.warnings)),
            "data_info": data_info,
        }


def run_backtest(**kwargs) -> dict[str, Any]:
    return BacktestEngine(**kwargs).run()
