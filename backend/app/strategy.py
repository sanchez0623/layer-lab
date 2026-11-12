from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from statistics import mean

from .broker import BrokerAccount
from .domain import Bar, Fill, OrderRequest, Position, StrategyConfig


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
        account: BrokerAccount,
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

    def start_day(self, has_intraday: bool) -> None:
        self.t_order_submitted = False
        self.t_exit_submitted = False
        self.t_traded_today = False
        self.recent_long_upper = False
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
        t_quantity = min(position.t_qty, position.permanent_sellable_qty)
        if t_quantity >= 100:
            requests.append(OrderRequest(
                symbol=position.symbol, side="SELL", quantity=t_quantity,
                purpose="T", execution_mode=mode, reason=self.close_reason or "整体止盈_先平T",
            ))
        permanent_quantity = position.permanent_sellable_qty
        if permanent_quantity >= 100:
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

    def _risk_requests(
        self, history: list[Bar], bar: Bar, account: BrokerAccount,
        recent_bars: list[Bar], mode: str,
    ) -> list[OrderRequest]:
        position = account.position(bar.symbol)
        price_return, account_return = self._returns(account, bar.symbol, bar.close, self.round_t_start)
        if self.event_halt:
            self.close_reason = self.close_reason or "事件熔断"
            return self._liquidation_requests(position, mode)
        if price_return >= self.config.price_take_profit:
            self.liquidating = True
            self.close_reason = "价格轨收益达到15%"
            return self._liquidation_requests(position, mode)
        if account_return >= self.config.account_take_profit:
            self.liquidating = True
            self.close_reason = "账户轨收益达到15%"
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
            self.close_reason = "Armed量价背离三选二_3根1分钟K线"
        elif self.vwap_count >= self.config.divergence_ticks:
            self.liquidating = True
            self.close_reason = "VWAP账户回撤4%_3根1分钟K线"
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

        price_return, _ = self._returns(account, bar.symbol, bar.close, self.round_t_start)
        signal = ""
        if price_return > 0 and len(history) >= 4:
            recent_high = max(item.high for item in history[-10:])
            prior_three_low = min(item.low for item in history[-4:-1])
            if (
                bar.close <= recent_high * (1 - self.config.right_drawdown)
                and bar.low >= prior_three_low
            ):
                signal = "浮盈右侧回撤5%"
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
                signal = "浮亏超跌A3"
        if signal:
            amount = self.config.installment_amounts[account.sequence_index]
            return [self._buy_request(bar, amount, signal)]
        return []

    @staticmethod
    def _buy_request(bar: Bar, amount: float, reason: str) -> OrderRequest:
        quantity = int(amount / bar.close / 100) * 100
        return OrderRequest(
            symbol=bar.symbol, side="BUY", quantity=max(0, quantity),
            execution_mode="NEXT_OPEN", reason=reason,
        )

    def _t_eligible(self, history: list[Bar], bar: Bar, account: BrokerAccount) -> bool:
        position = account.position(bar.symbol)
        if (
            not self.config.enable_t or account.sequence_index < 4 or self.armed
            or self.liquidating or self.t_traded_today or self.t_order_submitted
            or position.t_qty or len(history) < 5
        ):
            return False
        recent = history[-5:]
        average_amplitude = mean(
            (item.high - item.low) / (item.prev_close or item.open) for item in recent
        )
        turnover = recent[-1].turnover_rate
        if average_amplitude < self.config.t_min_amplitude or turnover is None:
            return False
        if not self.config.t_min_turnover <= turnover <= self.config.t_max_turnover:
            return False
        if bar.bid is None or bar.ask is None or bar.ask <= bar.bid or bar.volume < 100:
            return False
        # 一次完整 T 往返：四项双边费率各计两次、印花税仅卖出计一次，滑点买卖各一次。
        estimated_cost = (
            2 * (
                self.config.commission_rate + self.config.handling_fee_rate
                + self.config.regulatory_fee_rate + self.config.transfer_fee_rate
            )
            + self.config.stamp_tax_rate + 2 * self.config.slippage_bps / 10000
        )
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
        account: BrokerAccount,
    ) -> list[OrderRequest]:
        risk_history = history + [Bar(
            timestamp=bar.timestamp, symbol=bar.symbol, open=bar.open, high=bar.high,
            low=bar.low, close=bar.close, volume=bar.volume, amount=bar.amount,
            timeframe=bar.timeframe, turnover_rate=bar.turnover_rate,
            prev_close=bar.prev_close, bid=bar.bid, ask=bar.ask,
        )]
        risk = self._risk_requests(risk_history, bar, account, recent_bars, "NEXT_BAR")
        if risk:
            return risk
        position = account.position(bar.symbol)
        current_time = bar.timestamp.time()
        if position.t_qty > 0:
            if self.t_exit_submitted:
                return []
            average_t_price = position.t_cost / position.t_qty
            target = average_t_price * (1 + self.config.t_target_spread)
            carried = position.t_qty != position.today_t_buy_qty
            if current_time >= datetime.strptime("14:50", "%H:%M").time() or carried or bar.close >= target:
                quantity = min(position.t_qty, position.permanent_sellable_qty)
                if quantity >= 100:
                    self.t_exit_submitted = True
                    return [OrderRequest(
                        symbol=bar.symbol, side="SELL", quantity=quantity, purpose="T",
                        execution_mode="NEXT_BAR", reason="T目标价差" if bar.close >= target else "14:50旧仓轧平",
                    )]
            return []
        if current_time < datetime.strptime("09:35", "%H:%M").time() or current_time >= datetime.strptime("14:00", "%H:%M").time():
            return []
        if not self._t_eligible(history, bar, account):
            return []
        quantity = int(self.config.t_order_amount / bar.close / 100) * 100
        quantity = min(quantity, int(account.available_cash / bar.close / 100) * 100)
        quantity = min(quantity, position.permanent_sellable_qty)
        if quantity < 100:
            return []
        self.t_order_submitted = True
        return [OrderRequest(
            symbol=bar.symbol, side="BUY", quantity=quantity, purpose="T",
            execution_mode="NEXT_BAR", reason="第5笔起正T_准入通过",
        )]
