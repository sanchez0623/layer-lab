from __future__ import annotations

import math
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, stdev
from typing import Any

from .broker import BrokerAccount
from .domain import Bar, Fill, StrategyConfig, money, utc_now
from .screener import ScreenerConfig, run_screener
from .strategy import LayeredTStrategy
from .universe import BOARD_INDEX, UniverseStore

# 回测指数加载：start 前推的日历天缓冲，保证回测首日往前 ≥21 根（L0 ret20 + 策略大盘门）。
_INDEX_LOOKBACK_BUFFER_DAYS = 45


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
                    requests = self.strategy.on_intraday(
                        previous_daily, recent, bar, self.account,
                        index_history=index_history, session_index=session_index,
                    )
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
                f"{len(missing_intraday_dates)}个交易日无5分钟数据（{missing_intraday_dates[0]}起）；"
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


class FunnelRotationBacktest:
    """选股漏斗轮动回测：每次清仓后重新跑 L0→L4，取当时第一名换票。

    与模拟盘 rotation v2 语义对齐（清仓→重筛→次日建仓），但全程日线口径：
    无 5 分钟数据，T 交易与 tick 级风控降级。数据只读 universe.db 缓存，不联网。
    """

    def __init__(
        self, store: UniverseStore, market: Any,
        screener_config: ScreenerConfig | None = None,
        initial_cash: float = 4_000_000.0, config: StrategyConfig | None = None,
        start_date: str | None = None, end_date: str | None = None,
        name: str = "选股漏斗轮动回测",
    ) -> None:
        self.store = store
        self.market = market
        # 引擎内部强制关闭财务深筛：既避免联网财务，也避免用「今天」的季报回看历史（未来数据）。
        base = screener_config or ScreenerConfig()
        self.screener_config = ScreenerConfig.from_dict({**base.to_dict(), "enable_fundamental_veto": False})
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
        self._prepare_data()

    # -- 数据准备 ---------------------------------------------------------

    def _prepare_data(self) -> None:
        """读入候选清单、板块指数与交易日时间线（不联网、不打 baostock）。

        内存关键：不再一次性把全池候选的日线读进 self.history（2G ECS 上全池
        52 万根 Bar 会 OOM）。选股由 run_screener 自己按需 load；持仓标的与
        buy_hold 参照改为用时单标的 load（见 _bars_for / _load_bars）。这里只
        保留候选元数据、指数日线与 distinct 交易日时间线，均为轻量对象。
        """
        if self.store.latest_trade_date() is None:
            raise ValueError("universe 日线缓存为空，请先在选股页刷新数据")
        self.candidates = self.store.list_codes(self.screener_config.boards)
        self.name_by_code = {row.code: row.name for row in self.candidates}
        self.board_by_code = {row.code: row.board for row in self.candidates}
        codes = [row.code for row in self.candidates]
        # 全局交易日时间线：候选出现过日线的 distinct 日期 ∩ [start_date, end_date]
        # （轻量：只查日期列，不构建 Bar 对象）。
        timeline = self.store.list_trade_dates(
            codes, start=self.start_date.isoformat() if self.start_date else None,
            end=self.end_date.isoformat() if self.end_date else None,
        )
        self.timeline = sorted(set(timeline))
        # 有日线的候选数（供报告展示；仅计数，不驻留 Bar）。
        self.codes_with_daily = self.store.count_codes_with_daily(codes)
        if not self.codes_with_daily or not self.timeline:
            raise ValueError("universe 日线缓存为空，请先在选股页刷新数据")
        # 板块指数（L0 门 + 策略大盘门）：锚定回测区间，start 前推缓冲天数字段
        # 保证回测首日往前 ≥21 根（否则 load_index「截至最新日最近 260 根」会让
        # 首日 L0 误判「不足 21 根」降级）。end 用回测区间末，避免引入未来数据。
        index_start = (
            datetime.fromisoformat(self.timeline[0]).date()
            - timedelta(days=_INDEX_LOOKBACK_BUFFER_DAYS)
        ).isoformat()
        index_end = self.timeline[-1]
        self.index_history: dict[str, list[Bar]] = {}
        for board in self.screener_config.boards:
            index_code = BOARD_INDEX[board]
            self.index_history[board] = self.store.load_index(
                index_code, max_rows=4000,
                start=index_start, end=index_end,
            )
        # 持仓标的日线惰性缓存：任意时刻最多驻留一只标的的历史。
        self._held_code: str | None = None
        self._held_bars: list[Bar] = []
        # 日线换手率缓存 {code: {date: turn}}，来自 turnover_daily.parquet（做 T 准入）。
        self._turnover_cache: dict[str, dict[str, float]] | None = None

    def _load_bars(self, code: str) -> list[Bar]:
        """单标的日线（最近 260 根）；供持仓回放与 buy_hold 参照按需加载。

        与改造前一致取 max_rows=260（tail 最近 N 根），窗口裁剪由 timeline 与
        buy_hold 处的日期过滤负责，这里不额外按 start/end 截断。
        做 T 准入需要日线换手率，而 quan 源日线无换手字段（turnover_rate 恒 None），
        这里从 turnover_daily.parquet（BaoStock 日线 turn，见 gen_turnover_parquet.py）
        按日期填充——与模拟盘实时快照换手同口径（架构原则 1）。
        """
        bars = self.store.load_history([code], max_rows=260).get(code, [])
        turn_map = self._daily_turnover(code)
        if turn_map:
            for bar in bars:
                if bar.turnover_rate is None:
                    bar.turnover_rate = turn_map.get(bar.date)
        return bars

    def _daily_turnover(self, code: str) -> dict[str, float]:
        """某标的的 {date: 当日日线换手率}，来自同目录 turnover_daily.parquet（懒加载）。"""
        if self._turnover_cache is None:
            self._turnover_cache = {}
            turn_path = self.store.turnover_path if hasattr(self.store, "turnover_path") else None
            if turn_path and Path(turn_path).exists():
                pl = self.store._pl
                df = pl.read_parquet(turn_path)
                for row in df.iter_rows(named=True):
                    self._turnover_cache.setdefault(str(row["code"]), {})[
                        str(row["date"])[:10]
                    ] = float(row["turnover"])
        return self._turnover_cache.get(code, {})

    def _bars_for(self, code: str | None) -> list[Bar]:
        """当前持仓标的的日线，带单条目缓存（换票时自然替换）。"""
        if not code:
            return []
        if self._held_code == code:
            return self._held_bars
        bars = self._load_bars(code)
        self._held_code = code
        self._held_bars = bars
        return bars

    # -- 与 BacktestEngine 一致的轮次记账 --------------------------------

    def _new_round(self, fill: Fill, session_index: int) -> None:
        self.current_round = {
            "id": f"R{len(self.rounds) + 1:03d}", "symbol": fill.symbol,
            "name": self.name_by_code.get(fill.symbol, ""),
            "board": self.board_by_code.get(fill.symbol, ""),
            "chg20d_at_select": self._chg20d_at_select.get(fill.symbol),
            "screen": self._screen_by_symbol.get(fill.symbol),
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
            # 轮次闭合：清掉当前持有标的，让收盘重筛（对齐 rotation v2「清仓后重新选股」）。
            self._round_just_closed = True

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

    # -- 选股 ------------------------------------------------------------

    def _screen(self, as_of: str) -> dict[str, Any]:
        """在 as_of 时点跑一次 L0→L4（不联网），返回轻量快照。"""
        result = run_screener(
            self.store, self.market, self.screener_config,
            as_of=as_of, include_realtime=False,
            index_history=self.index_history,
        )
        first = result.first
        snapshot = {
            "as_of": result.as_of,
            "gate_open": bool(result.gate.get("open")),
            "funnel": dict(result.funnel),
            "first_code": first.code if first else "",
            "first_name": first.name if first else "",
            "first_chg20d": (first.chg20d if first else None),
        }
        for warning in result.warnings:
            if warning not in self.warnings:
                self.warnings.append(warning)
        self.events.append({
            "date": as_of, "type": "SCREEN", "as_of": result.as_of,
            "gate_open": snapshot["gate_open"], "first_code": snapshot["first_code"],
            "first_name": snapshot["first_name"], "funnel": dict(result.funnel),
            "eligible_count": len(result.eligible),
        })
        return snapshot

    def run(self) -> dict[str, Any]:
        self.warnings.append("漏斗轮动回测为日线口径，5分钟做T与tick级风控降级")
        self.warnings.append("财务深筛已强制关闭（enable_fundamental_veto=False）：避免联网财务与未来数据")
        self._chg20d_at_select: dict[str, float | None] = {}
        self._screen_by_symbol: dict[str, dict] = {}
        # 每次选股的轨迹（含空仓等待的选股），轮次闭合时回填 entry/exit/pnl
        self._trail: list[dict[str, Any]] = []
        current_symbol: str | None = None
        flat_wait_days = 0
        session_index = -1
        self._round_just_closed = False

        equity_curve: list[dict[str, Any]] = []
        used_dates: list[str] = []
        for date_str in self.timeline:
            session_index += 1
            used_dates.append(date_str)
            # cancel_previous=False：隔夜 NEXT_OPEN 挂单要保留到次日开盘撮合（与 BacktestEngine 一致）。
            self.account.new_day(date_str, cancel_previous=False)
            submitted_signal = False
            if self._round_just_closed:
                # 前一交易日轮次闭合：清掉持有标的，今日收盘重新选股（rotation v2 语义）。
                current_symbol = None
                flat_wait_days = 0
                self._round_just_closed = False
            if current_symbol is not None:
                self.strategy.start_day(False)
                bars = self._bars_for(current_symbol)
                bar = next((item for item in bars if item.date == date_str), None)
                if bar is not None:
                    fills = self.account.process_bar(bar, modes={"NEXT_OPEN"})
                    self._handle_fills(fills, session_index, bar)
                    self.account.cancel_open_orders("T+1开盘未成交取消", modes={"NEXT_OPEN"})
                    self.account.mark(current_symbol, bar.close)
                    history = [item for item in bars if item.date <= date_str]
                    board = self.board_by_code.get(current_symbol, "")
                    index_history = [
                        item for item in self.index_history.get(board, [])
                        if item.timestamp.date().isoformat() <= date_str
                    ]
                    self._update_round(bar, session_index)
                    requests = self.strategy.on_daily(
                        history, index_history, self.account, session_index, False,
                    )
                    self._submit(requests, bar.timestamp)
                    submitted_signal = bool(requests)
                # 首笔成交后把轨迹项关联到新开的轮次（供结尾回填 entry/exit/pnl）
                if self.current_round is not None:
                    round_id = self.current_round["id"]
                    for entry in reversed(self._trail):
                        if entry["symbol"] == current_symbol and entry["round"] is None:
                            entry["round"] = round_id
                            break
            # 清仓后（无持仓）重新选股；与 rotation v2「清仓后重新选股」一致
            flat = not any(position.total_qty > 0 for position in self.account.positions.values())
            has_live_order = any(order.status in {"PENDING", "PARTIALLY_FILLED"} for order in self.account.orders)
            if current_symbol is not None and flat and self.current_round is None:
                if has_live_order or submitted_signal:
                    # 首笔挂单还在（次日开盘成交）或当晚刚出首笔信号：给成交机会，不放弃。
                    flat_wait_days = 0
                else:
                    flat_wait_days += 1
                    if flat_wait_days > 3:
                        # 迟迟建不起仓（建仓日指数门关闭 / 首笔被撤 / 长期停牌）：
                        # 视为本次选股作废，置空 current_symbol 让下一交易日重新选股，避免卡死。
                        self.events.append({
                            "date": date_str, "type": "ENTRY_ABANDON", "symbol": current_symbol,
                            "reason": "选中标的连续多日未能建仓，放弃并重新选股",
                        })
                        current_symbol = None
                        flat_wait_days = 0
            else:
                flat_wait_days = 0
            if flat and current_symbol is None:
                snapshot = self._screen(date_str)
                first_code = snapshot["first_code"]
                # 复用本次选股结果里 first 的 Chg20D（与漏斗口径一致）
                chg20d_at_select = snapshot["first_chg20d"]
                self._trail.append({
                    "round": None, "symbol": first_code or None,
                    "name": snapshot["first_name"], "board": (
                        self.board_by_code.get(first_code, "") if first_code else ""
                    ),
                    "select_date": snapshot["as_of"], "entry_date": None,
                    "exit_date": None, "pnl": None,
                    "chg20d_at_select": chg20d_at_select,
                    "gate_open": snapshot["gate_open"], "status": "WAITING",
                })
                if first_code:
                    current_symbol = first_code
                    flat_wait_days = 0
                    self._chg20d_at_select[first_code] = self._trail[-1]["chg20d_at_select"]
                    self._screen_by_symbol[first_code] = snapshot
                    self._trail[-1]["status"] = "OPEN"
                    self.events.append({
                        "date": date_str, "type": "SELECT", "symbol": first_code,
                        "note": "选中标的，次一交易日开盘建仓",
                    })
                else:
                    self.events.append({
                        "date": date_str, "type": "WAIT",
                        "reason": "L0 大盘门关闭" if not snapshot["gate_open"] else "L0–L4 后无候选，空仓等待",
                    })
            # 净值曲线点
            held = current_symbol if current_symbol else None
            position = self.account.position(held) if held else None
            equity_curve.append({
                "date": date_str, "equity": self.account.equity, "cash": self.account.cash,
                "market_value": self.account.market_value,
                "invested": position.permanent_cost if position else 0.0,
                "drawdown": 0.0,
                "position_qty": position.total_qty if position else 0,
                "symbol": held,
            })

        # 收尾：未闭合的轮次按 OPEN 记录；清掉剩余挂单
        if self.current_round:
            item = self.current_round
            item["pnl"] = money(self.account.equity - item["start_equity"])
            item["close_reason"] = None
            item.pop("_deep_loss_start_session", None)
            self.rounds.append(item)
            self.current_round = None
        self.account.cancel_open_orders("回测结束")

        # 回填轨迹里的 entry/exit/pnl（按 round_id 关联）
        rounds_by_id = {item["id"]: item for item in self.rounds}
        for entry in self._trail:
            round_id = entry.get("round")
            if round_id and round_id in rounds_by_id:
                closed = rounds_by_id[round_id]
                entry["entry_date"] = closed["start_date"]
                entry["exit_date"] = closed["end_date"]
                entry["pnl"] = closed["pnl"]
                entry["status"] = "CLOSED" if closed["status"] == "CLOSED" else "OPEN"
        # 选股了但从未建仓（选中即回测结束）的轨迹补 status=OPEN
        for entry in self._trail:
            if entry["round"] is None and entry["symbol"] and entry["status"] == "OPEN":
                entry["status"] = "OPEN"

        # 业绩指标：buy_hold 用「第一只入选标的」全程持有作参照；从未建仓则回退板块指数
        reference = self.rounds[0]["symbol"] if self.rounds else None
        buy_hold_return = 0.0
        reference_symbol = None
        reference_bars = self._bars_for(reference) if reference else []
        if reference and reference_bars:
            bars = [
                item for item in reference_bars
                if self.timeline[0] <= item.date <= self.timeline[-1]
            ]
            if bars:
                reference_symbol = reference
                buy_hold_return = bars[-1].close / bars[0].close - 1
        elif self.rounds:
            # 首轮标的日线缺失（罕见）：回退到其所属板块指数全程，仍给出可复核参照。
            board = self.board_by_code.get(self.rounds[0]["symbol"], "")
            index_bars = [
                item for item in self.index_history.get(board, [])
                if self.timeline[0] <= item.date <= self.timeline[-1]
            ]
            if len(index_bars) >= 2 and index_bars[0].close > 0:
                reference_symbol = self.index_history and BOARD_INDEX.get(board)
                buy_hold_return = index_bars[-1].close / index_bars[0].close - 1
        metrics = performance_metrics(
            equity_curve, self.account.initial_cash, self.rounds,
            [item.to_dict() for item in self.account.trades], self.account.total_fees,
            self.account.t_net_profit, self.max_invested, self.max_float_loss,
            buy_hold_return,
        )
        screening_count = len(self._trail)
        data_info = {
            "mode": "funnel_rotation", "boards": list(self.screener_config.boards),
            "universe_codes": len(self.candidates), "codes_with_daily": self.codes_with_daily,
            "daily_timeframe": "daily", "intraday_rows": 0, "index_rows": sum(
                len(item) for item in self.index_history.values()
            ),
            "execution": "T日收盘选股/T+1开盘建仓；T日收盘信号/T+1开盘；无未来价",
            "start": used_dates[0], "end": used_dates[-1],
            "date_range": [self.timeline[0], self.timeline[-1]],
            "screening_count": screening_count,
            "reference_symbol": reference_symbol,
            "rotation_trail": self._trail,
        }
        return {
            "id": str(uuid.uuid4()), "name": self.name, "status": "COMPLETED",
            "created_at": utc_now(), "config": {
                **self.config.to_dict(), "initial_cash": self.account.initial_cash,
                "screener": self.screener_config.to_dict(),
                "start_date": self.start_date.isoformat() if self.start_date else None,
                "end_date": self.end_date.isoformat() if self.end_date else None,
            },
            "metrics": metrics, "equity_curve": equity_curve,
            "trades": [item.to_dict() for item in self.account.trades],
            "orders": [item.to_dict() for item in self.account.orders],
            "rounds": self.rounds, "events": self.events,
            "warnings": list(dict.fromkeys(self.warnings)), "data_info": data_info,
        }


def run_funnel_backtest(**kwargs) -> dict[str, Any]:
    return FunnelRotationBacktest(**kwargs).run()
