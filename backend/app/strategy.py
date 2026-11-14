from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, time as dtime
from statistics import mean

from .broker import BrokerAccount
from .domain import Bar, Fill, OrderRequest, Position, StrategyConfig
from .trading_rules import rules_for


def board_buy_quantity(symbol: str, amount: float, price: float) -> int:
    """按标的所属板块口径，把「金额 + 参考价」折算成合法买入申报股数。

    各板块最小申报与递增单位不同（科创板 min_qty=200/step=1，创业板/主板
    min_qty=100/step=100），写死 100 股整手会让科创板高价股算出低于 200 股的
    申报而被拒，进而卡住 16 笔序列。此处统一按板块规则取整：
    - 常规情况按 step 向下取整；
    - 结果低于板块最小申报时抬到最小申报（方案 2：略微透支该笔预算也要建仓，
      避免 16 笔序列因「买不起最小申报」而空转、资金闲置）；
    - 仅当价格非法或该笔金额非正时返回 0，由调用方跳过。
    """
    if price <= 0 or amount <= 0:
        return 0
    rules = rules_for(symbol)
    step = max(1, rules.step)
    raw = int(amount / price / step) * step
    return max(raw, rules.min_qty)


def board_sell_threshold(symbol: str) -> int:
    """卖出申报的最小有效股数：低于板块最小申报的零头不单独发卖单。

    科创板最小 200 股、可 1 股递增；创业板/主板 100 股整手。清仓走 broker
    的「零股整笔卖出」通道，不受此阈值约束。
    """
    rules = rules_for(symbol)
    return max(rules.min_qty, rules.step)

# 尾盘首仓窗口：只在这段时间内允许盘中建立第一笔永久仓。
# 起点含、终点不含（14:50 及之后不再发首仓），保证信号 bar 最晚是 14:45，
# 由下一根已收盘 5 分钟 K 线（≤14:50）撮合，不会拖到收盘后才发单。
TAIL_ENTRY_START = dtime(14, 30)
TAIL_ENTRY_END = dtime(14, 50)


class Strategy(ABC):
    @abstractmethod
    def on_daily(
        self, history: list[Bar], index_history: list[Bar], account: BrokerAccount,
        session_index: int, has_intraday: bool,
    ) -> list[OrderRequest]:
        raise NotImplementedError

    @abstractmethod
    def on_intraday(
        self, history: list[Bar], recent_bars: list[Bar], bar: Bar,
        account: BrokerAccount, index_history: list[Bar] | None = None,
        session_index: int | None = None,
    ) -> list[OrderRequest]:
        raise NotImplementedError

    @abstractmethod
    def on_fill(self, fill: Fill, session_index: int) -> None:
        raise NotImplementedError


class LayeredTStrategy(Strategy):
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()
        self.last_add_price: float | None = None
        self.last_add_session: int | None = None
        self.had_new_low = False
        self.armed = False
        self.liquidating = False
        self.close_reason = ""
        self.divergence_count = 0
        self.vwap_count = 0
        self.account_return_peak = 0.0
        self.round_t_start = 0.0
        self.t_order_submitted = False
        self.t_exit_submitted = False
        self.t_traded_today = False
        self.recent_long_upper = False
        self.event_halt = False
        # 尾盘首仓已在本交易日提交（防同日 15:00 日终信号重复建仓）。
        self.entry_submitted_date: str | None = None
        # 尾盘加仓已在本交易日提交（防同日 15:00 日终信号重复加仓）。
        self.add_submitted_date: str | None = None
        # 本轮首个永久仓建仓日期（YYYY-MM-DD）：用于计算持有交易日数（超期降阈止盈）。
        self.round_entry_date: str | None = None
        # live-tick 可能在同一根已收盘 5 分钟 K 线上重复触发；按 bar 时间去重，
        # 避免背离/VWAP 确认计数被同一根 bar 叠加。
        self.last_intraday_timestamp: datetime | None = None

    def state_dict(self) -> dict:
        return {
            "last_add_price": self.last_add_price, "last_add_session": self.last_add_session,
            "had_new_low": self.had_new_low, "armed": self.armed,
            "liquidating": self.liquidating, "close_reason": self.close_reason,
            "divergence_count": self.divergence_count, "vwap_count": self.vwap_count,
            "account_return_peak": self.account_return_peak, "round_t_start": self.round_t_start,
            "t_order_submitted": self.t_order_submitted, "t_exit_submitted": self.t_exit_submitted,
            "t_traded_today": self.t_traded_today, "recent_long_upper": self.recent_long_upper,
            "event_halt": self.event_halt,
            "entry_submitted_date": self.entry_submitted_date,
            "add_submitted_date": self.add_submitted_date,
            "round_entry_date": self.round_entry_date,
            "last_intraday_timestamp": (
                self.last_intraday_timestamp.isoformat() if self.last_intraday_timestamp else None
            ),
        }

    def load_state(self, state: dict) -> None:
        self.last_add_price = state.get("last_add_price")
        self.last_add_session = state.get("last_add_session")
        self.had_new_low = bool(state.get("had_new_low", False))
        self.armed = bool(state.get("armed", False))
        self.liquidating = bool(state.get("liquidating", False))
        self.close_reason = state.get("close_reason") or ""
        self.divergence_count = int(state.get("divergence_count", 0))
        self.vwap_count = int(state.get("vwap_count", 0))
        self.account_return_peak = float(state.get("account_return_peak", 0.0))
        self.round_t_start = float(state.get("round_t_start", 0.0))
        self.t_order_submitted = bool(state.get("t_order_submitted", False))
        self.t_exit_submitted = bool(state.get("t_exit_submitted", False))
        self.t_traded_today = bool(state.get("t_traded_today", False))
        self.recent_long_upper = bool(state.get("recent_long_upper", False))
        self.event_halt = bool(state.get("event_halt", False))
        submitted_date = state.get("entry_submitted_date")
        self.entry_submitted_date = str(submitted_date) if submitted_date else None
        add_date = state.get("add_submitted_date")
        self.add_submitted_date = str(add_date) if add_date else None
        raw_entry_date = state.get("round_entry_date")
        self.round_entry_date = str(raw_entry_date) if raw_entry_date else None
        raw_timestamp = state.get("last_intraday_timestamp")
        try:
            self.last_intraday_timestamp = datetime.fromisoformat(raw_timestamp) if raw_timestamp else None
        except (TypeError, ValueError):
            self.last_intraday_timestamp = None

    def start_day(self, has_intraday: bool) -> None:
        self.t_order_submitted = False
        self.t_exit_submitted = False
        self.t_traded_today = False
        self.recent_long_upper = False
        # 新交易日：昨日未成交的首仓委托已在日切取消，允许今天重新发尾盘首仓。
        self.entry_submitted_date = None
        self.add_submitted_date = None
        if has_intraday:
            self.divergence_count = 0
            self.vwap_count = 0

    def reset_round(self, account: BrokerAccount) -> None:
        self.last_add_price = None
        self.last_add_session = None
        self.had_new_low = False
        self.armed = False
        self.liquidating = False
        self.close_reason = ""
        self.divergence_count = 0
        self.vwap_count = 0
        self.account_return_peak = 0.0
        self.round_t_start = account.t_net_profit
        self.t_order_submitted = False
        self.t_exit_submitted = False
        self.event_halt = False
        self.entry_submitted_date = None
        self.add_submitted_date = None
        self.round_entry_date = None
        account.sequence_index = 0
        account.armed_state = "OFF"

    def trigger_event_halt(self, reason: str = "事件熔断") -> None:
        self.event_halt = True
        self.liquidating = True
        self.close_reason = reason

    def on_fill(self, fill: Fill, session_index: int) -> None:
        if fill.side == "BUY" and fill.purpose == "PERMANENT":
            self.last_add_price = fill.price
            self.last_add_session = session_index
            self.had_new_low = False
            # 记录本轮首个永久仓建仓日期：超期持仓降阈止盈按此计算持有交易日数。
            if self.round_entry_date is None:
                self.round_entry_date = fill.timestamp[:10]
        if fill.purpose == "T":
            if fill.side == "BUY":
                self.t_traded_today = True
                self.t_order_submitted = True
            else:
                self.t_exit_submitted = True

    @staticmethod
    def _returns(account: BrokerAccount, symbol: str, current_price: float, round_t_start: float) -> tuple[float, float]:
        position = account.position(symbol)
        if position.permanent_cost <= 0:
            return 0.0, 0.0
        floating = position.permanent_qty * current_price - position.permanent_cost
        price_return = floating / position.permanent_cost
        account_return = (floating + account.t_net_profit - round_t_start) / position.permanent_cost
        return price_return, account_return

    def _set_armed(self, account: BrokerAccount, armed: bool, account_return: float) -> None:
        if armed and not self.armed:
            self.account_return_peak = account_return
            self.divergence_count = 0
            self.vwap_count = 0
        self.armed = armed
        account.armed_state = "ARMED" if armed else "OFF"

    def _liquidation_requests(self, position: Position, mode: str) -> list[OrderRequest]:
        requests: list[OrderRequest] = []
        # 卖出阈值按板块口径：科创板最小申报 200 股，创业板/主板 100 股整手。
        # 低于阈值的零头不单独发单（清仓走 broker 零股整笔卖出通道）。
        min_sell = board_sell_threshold(position.symbol)
        t_quantity = min(position.t_qty, position.permanent_sellable_qty)
        if t_quantity >= min_sell:
            requests.append(OrderRequest(
                symbol=position.symbol, side="SELL", quantity=t_quantity,
                purpose="T", execution_mode=mode, reason=self.close_reason or "整体止盈_先平T",
            ))
        permanent_quantity = position.permanent_sellable_qty
        if permanent_quantity >= min_sell:
            requests.append(OrderRequest(
                symbol=position.symbol, side="SELL", quantity=permanent_quantity,
                purpose="PERMANENT", execution_mode=mode,
                reason=self.close_reason or "整体止盈_永久仓",
            ))
        return requests

    @staticmethod
    def _atr(history: list[Bar], period: int) -> float:
        if len(history) < 2:
            return 0.0
        values = []
        for previous, current in zip(history[-period - 1:-1], history[-period:]):
            values.append(max(
                current.high - current.low,
                abs(current.high - (previous.close or current.close)),
                abs(current.low - (previous.close or current.close)),
            ))
        return mean(values) if values else 0.0

    def _holding_days(self, history: list[Bar]) -> int:
        """本轮持有交易日数：history 里严格晚于建仓日的 bar 数量。

        history 是升序日线，建仓次日起算第 1 天；未记录入口日期返回 0。
        """
        if not self.round_entry_date:
            return 0
        return sum(1 for item in history if item.date > self.round_entry_date)

    def _effective_take_profit(self, holding_days: int) -> tuple[float, bool]:
        """返回 (有效止盈阈值, 是否因超期降阈)。

        持有交易日数超过 ``max_holding_days`` 时，价格轨/账户轨止盈阈值降为
        ``holding_tp_override``（若配置）。两者任一未配置则维持原止盈。
        """
        base = self.config.price_take_profit
        max_days = self.config.max_holding_days
        override = self.config.holding_tp_override
        if max_days is None or override is None:
            return base, False
        if holding_days < int(max_days):
            return base, False
        return min(base, float(override)), True

    def _risk_requests(
        self, history: list[Bar], bar: Bar, account: BrokerAccount,
        recent_bars: list[Bar], mode: str,
    ) -> list[OrderRequest]:
        position = account.position(bar.symbol)
        price_return, account_return = self._returns(account, bar.symbol, bar.close, self.round_t_start)
        holding_days = self._holding_days(history)
        effective_tp, tp_overridden = self._effective_take_profit(holding_days)
        if self.event_halt:
            self.close_reason = self.close_reason or "事件熔断"
            return self._liquidation_requests(position, mode)
        if price_return >= effective_tp:
            self.liquidating = True
            self.close_reason = (
                f"超期持仓{holding_days}日_止盈降至{effective_tp * 100:.0f}%"
                if tp_overridden else f"价格轨收益达到{effective_tp * 100:.0f}%"
            )
            return self._liquidation_requests(position, mode)
        if account_return >= effective_tp:
            self.liquidating = True
            self.close_reason = (
                f"超期持仓{holding_days}日_账户轨止盈降至{effective_tp * 100:.0f}%"
                if tp_overridden else f"账户轨收益达到{effective_tp * 100:.0f}%"
            )
            return self._liquidation_requests(position, mode)
        if self.liquidating:
            return self._liquidation_requests(position, mode)
        if price_return >= self.config.price_armed:
            self._set_armed(account, True, account_return)
        elif price_return < self.config.price_disarm:
            self._set_armed(account, False, account_return)
        if not self.armed:
            self.divergence_count = 0
            self.vwap_count = 0
            return []

        self.account_return_peak = max(self.account_return_peak, account_return)
        if len(history) >= 20:
            is_20d_high = bar.high >= max(item.high for item in history[-20:])
            previous_five = history[-6:-1] if len(history) >= 6 else history[:-1]
            volume_average = mean(item.volume for item in previous_five) if previous_five else bar.volume
            volume_not_supporting = bar.volume < volume_average * 0.8
            upper_shadow = False
            for item in recent_bars[-5:]:
                body = abs(item.close - item.open)
                upper = item.high - max(item.open, item.close)
                full_range = max(item.high - item.low, 1e-9)
                if upper >= 2 * max(body, 0.01) and upper / full_range >= 0.45:
                    upper_shadow = True
                    break
            self.recent_long_upper = self.recent_long_upper or upper_shadow
            atr = self._atr(history, self.config.atr_period)
            threshold = min(0.03, max(0.008, 0.4 * atr / bar.close)) if bar.close else 0.03
            high_drawdown = (bar.high - bar.close) / bar.close >= threshold
            if is_20d_high and sum((volume_not_supporting, self.recent_long_upper, high_drawdown)) >= 2:
                self.divergence_count += 1
            else:
                self.divergence_count = 0
        total_volume = sum(item.volume for item in recent_bars if item.volume > 0)
        total_amount = sum(item.amount for item in recent_bars if item.volume > 0)
        vwap = total_amount / total_volume if total_volume else bar.close
        if bar.close < vwap and self.account_return_peak - account_return >= self.config.vwap_drawdown:
            self.vwap_count += 1
        else:
            self.vwap_count = 0
        if self.divergence_count >= self.config.divergence_ticks:
            self.liquidating = True
            self.close_reason = "Armed量价背离三选二_3根5分钟K线"
        elif self.vwap_count >= self.config.divergence_ticks:
            self.liquidating = True
            self.close_reason = "VWAP账户回撤4%_3根5分钟K线"
        return self._liquidation_requests(position, mode) if self.liquidating else []

    def on_daily(
        self, history: list[Bar], index_history: list[Bar], account: BrokerAccount,
        session_index: int, has_intraday: bool,
    ) -> list[OrderRequest]:
        if not history:
            return []
        bar = history[-1]
        position = account.position(bar.symbol)
        risk = self._risk_requests(history, bar, account, [bar], "NEXT_OPEN")
        if risk:
            return risk
        if position.total_qty == 0 and account.sequence_index == 0:
            # 同日尾盘已发首仓、或已有未结束的首仓委托时，日终不再重复申报。
            if self.entry_submitted_date == bar.date or self._has_pending_entry(account, bar.symbol):
                return []
            if len(index_history) >= 21:
                index_return = index_history[-1].close / index_history[-21].close - 1
                if index_return >= 0:
                    return []
            elif index_history:
                return []
            return [self._buy_request(bar, self.config.installment_amounts[0], "换票日首笔")]
        if (
            position.permanent_qty <= 0 or self.last_add_price is None
            or self.last_add_session is None or account.sequence_index >= 16
        ):
            return []
        if session_index - self.last_add_session < self.config.min_add_gap_days:
            return []
        # 同日尾盘已发加仓、或已有未结束的永久仓买入委托时，日终不再重复申报。
        if self.add_submitted_date == bar.date or self._has_pending_entry(account, bar.symbol):
            return []
        signal = self._add_signal(history, account)
        if signal:
            amount = self.config.installment_amounts[account.sequence_index]
            return [self._buy_request(bar, amount, signal)]
        return []

    def _add_signal(self, history: list[Bar], account: BrokerAccount) -> str:
        """加仓信号：浮盈右侧回撤 / 浮亏超跌A3（日线与盘中合成日线共用同一口径）。"""
        if not history:
            return ""
        bar = history[-1]
        price_return, _ = self._returns(account, bar.symbol, bar.close, self.round_t_start)
        if price_return > 0 and len(history) >= 4:
            recent_high = max(item.high for item in history[-10:])
            prior_three_low = min(item.low for item in history[-4:-1])
            if (
                bar.close <= recent_high * (1 - self.config.right_drawdown)
                and bar.low >= prior_three_low
            ):
                return "浮盈右侧回撤5%"
        elif price_return < 0 and len(history) >= 6:
            had_low_before = self.had_new_low
            previous_three_low = min(item.low for item in history[-4:-1])
            previous_five_volume = mean(item.volume for item in history[-6:-1])
            a3 = (
                had_low_before and bar.close > previous_three_low
                and bar.volume < previous_five_volume * self.config.oversold_volume_ratio
            )
            if bar.low < self.last_add_price and bar.low < previous_three_low:
                self.had_new_low = True
            if a3:
                return "浮亏超跌A3"
        return ""

    @staticmethod
    def _buy_request(bar: Bar, amount: float, reason: str) -> OrderRequest:
        quantity = board_buy_quantity(bar.symbol, amount, bar.close)
        return OrderRequest(
            symbol=bar.symbol, side="BUY", quantity=max(0, quantity),
            execution_mode="NEXT_OPEN", reason=reason,
        )

    @staticmethod
    def _has_pending_entry(account: BrokerAccount, symbol: str) -> bool:
        """是否已有未结束的永久仓买入委托（防止同一笔首仓重复申报）。"""
        return any(
            order.symbol == symbol and order.side == "BUY"
            and order.purpose == "PERMANENT"
            and order.status in {"PENDING", "PARTIALLY_FILLED"}
            for order in account.orders
        )

    @staticmethod
    def _index_gate_open(index_history: list[Bar] | None, bar: Bar) -> bool:
        """盘中大盘门：只用**严格早于当日**的指数日线，避免盘中读到当日未收盘的指数价。

        与 ``on_daily`` 同一口径：21 根以上按 20 日收益 < 0 开门；数据存在但不足
        21 根保守关门；完全没有指数数据时降级开门（与日线降级路径一致）。
        """
        prior = [item for item in (index_history or []) if item.date < bar.date]
        if len(prior) >= 21:
            base = prior[-21].close
            if base <= 0:
                return True
            return prior[-1].close / base - 1 < 0
        return not prior

    def _tail_entry_requests(
        self, bar: Bar, account: BrokerAccount, index_history: list[Bar] | None,
    ) -> list[OrderRequest]:
        """尾盘首仓：空仓、序列未开、14:30–14:50 且大盘门开门时发一笔 NEXT_BAR 市价买单。

        只影响「首笔永久仓」：做 T 要求 ``sequence_index ≥ 4``、加仓要求已有持仓，
        都与这里的空仓前提不重叠；风控/止盈在调用方更早执行，优先级更高。
        当日只发一笔（``entry_submitted_date`` 去重），且已有未结束首仓委托时不再重复。
        """
        if self.armed or self.liquidating or self.event_halt:
            return []
        position = account.position(bar.symbol)
        if position.total_qty != 0 or account.sequence_index != 0:
            return []
        if self.entry_submitted_date == bar.date:
            return []
        if self._has_pending_entry(account, bar.symbol):
            return []
        clock = bar.timestamp.time()
        if not (TAIL_ENTRY_START <= clock < TAIL_ENTRY_END):
            return []
        if not self._index_gate_open(index_history, bar):
            return []
        amount = self.config.installment_amounts[0]
        # board_buy_quantity 已保证 ≥ 板块最小申报（方案 2：金额不足最小申报也按最小申报建仓）
        quantity = board_buy_quantity(bar.symbol, amount, bar.close)
        self.entry_submitted_date = bar.date
        return [OrderRequest(
            symbol=bar.symbol, side="BUY", quantity=quantity,
            execution_mode="NEXT_BAR", reason="尾盘首笔",
        )]

    @staticmethod
    def _today_daily_bar(history: list[Bar], bar: Bar, recent_bars: list[Bar]) -> Bar:
        """用当日已有的 5 分钟 K 合成一根「截至当前」的日线，供盘中加仓判定。

        尾盘时段当日成交量已基本走完，用累计量近似日量，避免拿单根 5 分钟量
        与日均量比较（会把 A3 的缩量条件放大成假信号）。
        """
        today = [item for item in (recent_bars or []) if item.date == bar.date] or [bar]
        prev_close = history[-1].close if history else bar.prev_close
        return Bar(
            timestamp=bar.timestamp, symbol=bar.symbol,
            open=today[0].open,
            high=max(item.high for item in today),
            low=min(item.low for item in today),
            close=bar.close,
            volume=sum(item.volume for item in today),
            amount=sum(item.amount for item in today),
            timeframe="daily", turnover_rate=bar.turnover_rate, prev_close=prev_close,
        )

    def _tail_add_requests(
        self, history: list[Bar], recent_bars: list[Bar], bar: Bar,
        account: BrokerAccount, session_index: int | None,
    ) -> list[OrderRequest]:
        """尾盘加仓：14:30–14:50 用当日合成日线判定，NEXT_BAR 市价单下一根 5 分钟 K 成交。

        与日线加仓共用 ``_add_signal`` 口径；仅在「已有永久仓、间隔≥1交易日、
        序列<16」时生效，``session_index`` 缺失（无法判定交易日间隔）时不发单。
        当日只发一笔，且已有未结束买入委托时不再重复。
        """
        if session_index is None or self.liquidating or self.event_halt:
            return []
        position = account.position(bar.symbol)
        if (
            position.permanent_qty <= 0 or self.last_add_price is None
            or self.last_add_session is None or account.sequence_index >= 16
        ):
            return []
        if session_index - self.last_add_session < self.config.min_add_gap_days:
            return []
        if self.add_submitted_date == bar.date or self._has_pending_entry(account, bar.symbol):
            return []
        clock = bar.timestamp.time()
        if not (TAIL_ENTRY_START <= clock < TAIL_ENTRY_END):
            return []
        full_history = list(history) + [self._today_daily_bar(history, bar, recent_bars)]
        signal = self._add_signal(full_history, account)
        if not signal:
            return []
        amount = self.config.installment_amounts[account.sequence_index]
        # 同上：board_buy_quantity 保证 ≥ 板块最小申报，方案 2 下不再因买不起整手而跳过
        quantity = board_buy_quantity(bar.symbol, amount, bar.close)
        self.add_submitted_date = bar.date
        return [OrderRequest(
            symbol=bar.symbol, side="BUY", quantity=quantity,
            execution_mode="NEXT_BAR", reason=signal,
        )]

    def _t_order_amount(self, account: BrokerAccount) -> float:
        """做T单笔金额跟随当前加仓档：取「下一次要加仓」的那笔金额。

        做T在 ``sequence_index >= 4`` 才启用，此时加仓档位与做T金额同源，
        让做T的资金体量与当前加仓阶段匹配（如下一次加仓 20 万，做T也用 20 万）。
        序列建满（``sequence_index >= 16``，已无「下一次加仓」）后取最大档
        （最后一笔金额），保证满仓阶段做T体量与最大加仓档一致。
        """
        index = min(account.sequence_index, len(self.config.installment_amounts) - 1)
        return self.config.installment_amounts[index]

    def _t_roundtrip_cost(self) -> float:
        """一次完整做T往返的成本率估算（占投入金额的比例，小数）。

        四项双边费率各计两次、印花税仅卖出计一次、滑点买卖各一次。用于：
        ①做T准入的净价差门槛（_t_eligible）；②净利止盈的目标价换算
        （毛目标价差 = 净目标 + 本成本率）。百分比近似，忽略 min_commission
        固定佣金（做T金额 10 万~40 万时占比 <0.005%）。
        """
        return (
            2 * (
                self.config.commission_rate + self.config.handling_fee_rate
                + self.config.regulatory_fee_rate + self.config.transfer_fee_rate
            )
            + self.config.stamp_tax_rate + 2 * self.config.slippage_bps / 10000
        )

    def _t_eligible(self, history: list[Bar], bar: Bar, account: BrokerAccount,
                    live_turnover: float | None = None,
                    live_bid: float | None = None, live_ask: float | None = None,
                    daily_amplitude: float | None = None,
                    daily_turnover_rate: float | None = None) -> bool:
        position = account.position(bar.symbol)
        if (
            not self.config.enable_t or account.sequence_index < 4 or self.armed
            or self.liquidating or self.t_traded_today or self.t_order_submitted
            or position.t_qty or len(history) < 5
        ):
            return False
        recent = history[-5:]
        # 振幅/换手口径：漏斗回测等日线场景传 daily_amplitude/daily_turnover_rate
        # （当日日线振幅/换手），避免用 5 分钟 bar 局部振幅（天然 ~0.3%）过不了日线
        # 口径门槛（振幅≥4%）。未传时回退到 recent（日线 history 或 5 分钟 bar）。
        if daily_amplitude is not None:
            average_amplitude = daily_amplitude
        else:
            average_amplitude = mean(
                (item.high - item.low) / (item.prev_close or item.open) for item in recent
            )
        # 换手率取「当日实时快照」优先（live_turnover，腾讯快照字段[38]，做T是当日盘中决策）；
        # 历史日线 bar 无换手字段（quan 源 turnover_rate 恒 None），缺失时回落实时值。
        turnover = daily_turnover_rate if daily_turnover_rate is not None else recent[-1].turnover_rate
        if turnover is None and live_turnover is not None:
            turnover = live_turnover
        if average_amplitude < self.config.t_min_amplitude or turnover is None:
            return False
        if not self.config.t_min_turnover <= turnover <= self.config.t_max_turnover:
            return False
        # 盘口同理：历史 5 分钟 bar 无盘口（腾讯只挂在当前快照 bar），缺失时用实时快照兜底。
        bid = bar.bid if bar.bid is not None else live_bid
        ask = bar.ask if bar.ask is not None else live_ask
        if bid is None or ask is None or ask <= bid or bar.volume < 100:
            return False
        # 一次完整 T 往返成本率（四项双边费率各计两次、印花税仅卖出一次、滑点买卖各一次）。
        estimated_cost = self._t_roundtrip_cost()
        if average_amplitude - estimated_cost < self.config.t_min_expected_net_spread:
            return False
        closes = [item.close for item in recent]
        one_sided_down = (
            closes[-1] < closes[-2] < closes[-3]
            and (closes[-1] < closes[-3] * 0.97 or closes[-1] < closes[0] * 0.95)
        )
        return not one_sided_down

    def on_intraday(
        self, history: list[Bar], recent_bars: list[Bar], bar: Bar,
        account: BrokerAccount, index_history: list[Bar] | None = None,
        session_index: int | None = None,
        live_turnover: float | None = None,
        live_bid: float | None = None, live_ask: float | None = None,
        daily_amplitude: float | None = None,
        daily_turnover_rate: float | None = None,
    ) -> list[OrderRequest]:
        # 同一根已收盘 5 分钟 K 线只评估一次：live-tick 重试/同一 5 分钟窗口内多次
        # 轮询时，不能把「连续 3 根 5 分钟 K 线」的确认计数重复累加。
        if self.last_intraday_timestamp == bar.timestamp:
            return []
        self.last_intraday_timestamp = bar.timestamp
        risk_history = history + [Bar(
            timestamp=bar.timestamp, symbol=bar.symbol, open=bar.open, high=bar.high,
            low=bar.low, close=bar.close, volume=bar.volume, amount=bar.amount,
            timeframe=bar.timeframe, turnover_rate=bar.turnover_rate,
            prev_close=bar.prev_close, bid=bar.bid, ask=bar.ask,
        )]
        risk = self._risk_requests(risk_history, bar, account, recent_bars, "NEXT_BAR")
        if risk:
            return risk
        # 尾盘首仓：只在空仓时生效，且优先级低于风控/止盈（上面已处理）。
        entry = self._tail_entry_requests(bar, account, index_history)
        if entry:
            return entry
        # 尾盘加仓：已有永久仓时，按与日线相同的加仓信号在盘中成交。
        add = self._tail_add_requests(history, recent_bars, bar, account, session_index)
        if add:
            return add
        position = account.position(bar.symbol)
        current_time = bar.timestamp.time()
        if position.t_qty > 0:
            if self.t_exit_submitted:
                return []
            average_t_price = position.t_cost / position.t_qty
            # 净利止盈：目标价 = 均价 ×(1 + 净目标 + 往返成本率)。扣掉双边佣金/经手费/
            # 证管费/过户费/印花税/滑点后净赚 t_target_net_spread（默认 3%）。
            target = average_t_price * (1 + self.config.t_target_net_spread + self._t_roundtrip_cost())
            quantity = min(position.t_qty, position.permanent_sellable_qty)
            can_sell = quantity >= board_sell_threshold(bar.symbol)
            # T+1 尾盘硬截止：做T资金最迟在 T+1 日 t_exit_cutoff（默认 14:50）无条件平掉。
            # 仅隔夜仓触发（t_qty > today_t_buy_qty）：T0 当日新建仓 is_overnight=False 不强平，
            # 可持有到 T1，兑现「资金 T0 或 T1 做完、T1 的 14:50 是截止点」。
            cutoff = datetime.strptime(self.config.t_exit_cutoff, "%H:%M").time()
            is_overnight = position.t_qty > position.today_t_buy_qty
            if current_time >= cutoff and is_overnight:
                if can_sell:
                    self.t_exit_submitted = True
                    return [OrderRequest(
                        symbol=bar.symbol, side="SELL", quantity=quantity, purpose="T",
                        execution_mode="NEXT_BAR", reason="T+1尾盘截止平T",
                    )]
                return []
            # 净利止盈：达到目标价即平T（T0 当天满足就当天做完，无需等到 T1）。
            # 整体止盈/风控清仓时 _liquidation_requests 会一并平掉T仓，作为兜底退出路径。
            if bar.close >= target and can_sell:
                self.t_exit_submitted = True
                return [OrderRequest(
                    symbol=bar.symbol, side="SELL", quantity=quantity, purpose="T",
                    execution_mode="NEXT_BAR", reason="T净利止盈",
                )]
            return []
        if current_time < datetime.strptime("09:35", "%H:%M").time() or current_time >= datetime.strptime("14:00", "%H:%M").time():
            return []
        if not self._t_eligible(history, bar, account, live_turnover=live_turnover,
                                live_bid=live_bid, live_ask=live_ask,
                                daily_amplitude=daily_amplitude,
                                daily_turnover_rate=daily_turnover_rate):
            return []
        # T 仓按板块口径取股数：目标金额与可用资金分别折算后取小，
        # 再用可卖永久仓数量封顶（做T不超过底仓，避免裸卖）。
        # 做T金额跟随当前加仓档（_t_order_amount），与下一次加仓体量匹配。
        min_sell = board_sell_threshold(bar.symbol)
        quantity = min(
            board_buy_quantity(bar.symbol, self._t_order_amount(account), bar.close),
            board_buy_quantity(bar.symbol, account.available_cash, bar.close),
        )
        quantity = min(quantity, position.permanent_sellable_qty)
        if quantity < min_sell:
            return []
        self.t_order_submitted = True
        return [OrderRequest(
            symbol=bar.symbol, side="BUY", quantity=quantity, purpose="T",
            execution_mode="NEXT_BAR", reason="第5笔起正T_准入通过",
        )]
