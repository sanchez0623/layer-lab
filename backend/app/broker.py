from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Iterable

from .domain import (
    Bar, FeeBreakdown, Fill, Order, OrderRequest, Position, StrategyConfig, money, price, utc_now,
)


class BrokerAccount:
    """本地确定性撮合账户，不包含任何行情或券商网络连接。"""

    def __init__(
        self, initial_cash: float = 4_000_000.0,
        config: StrategyConfig | None = None, name: str = "默认模拟账户",
        account_id: str | None = None, dataset_id: str | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash 必须为正数")
        self.id = account_id or str(uuid.uuid4())
        self.name = name
        self.initial_cash = money(initial_cash)
        self.cash = money(initial_cash)
        self.config = config or StrategyConfig()
        self.dataset_id = dataset_id
        self.cursor = 0
        self.status = "ACTIVE"
        self.armed_state = "OFF"
        self.sequence_index = 0
        self.auto_strategy = False
        self.strategy_state: dict = {}
        self.last_daily_signal_date: str | None = None
        self.realized_pnl = 0.0
        self.total_fees = 0.0
        self.t_net_profit = 0.0
        self.positions: dict[str, Position] = {}
        self.orders: list[Order] = []
        self.trades: list[Fill] = []
        self.current_date: str | None = None
        self._next_order_number = 1
        self._next_fill_number = 1

    @property
    def reserved_cash(self) -> float:
        reserved = 0.0
        for order in self.orders:
            if order.side != "BUY" or order.status not in {"PENDING", "PARTIALLY_FILLED"}:
                continue
            reference = order.limit_price
            if reference is None and order.symbol in self.positions:
                reference = self.positions[order.symbol].last_price
            if reference:
                reserved += order.remaining * reference * 1.002
        return money(reserved)

    @property
    def available_cash(self) -> float:
        return money(max(0.0, self.cash - self.reserved_cash))

    @property
    def market_value(self) -> float:
        return money(sum(position.total_qty * position.last_price for position in self.positions.values()))

    @property
    def equity(self) -> float:
        return money(self.cash + self.market_value)

    @property
    def permanent_cost(self) -> float:
        return money(sum(position.permanent_cost for position in self.positions.values()))

    @property
    def permanent_market_value(self) -> float:
        return money(sum(position.permanent_qty * position.last_price for position in self.positions.values()))

    def position(self, symbol: str) -> Position:
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol)
        return self.positions[symbol]

    def new_day(self, date: str, cancel_previous: bool = True) -> None:
        if cancel_previous and self.current_date and self.current_date != date:
            self.cancel_open_orders("DAY_END")
        self.current_date = date
        for position in self.positions.values():
            position.today_permanent_buy_qty = 0
            position.today_t_buy_qty = 0

    def submit(self, request: OrderRequest, timestamp: datetime | str | None = None) -> Order:
        side = request.side.upper()
        order_type = request.order_type.upper()
        purpose = request.purpose.upper()
        if side not in {"BUY", "SELL"} or order_type not in {"MARKET", "LIMIT"}:
            raise ValueError("side/order_type 不合法")
        if purpose not in {"PERMANENT", "T"}:
            raise ValueError("purpose 必须为 PERMANENT 或 T")
        if isinstance(request.quantity, bool) or int(request.quantity) != request.quantity or request.quantity <= 0:
            raise ValueError("quantity 必须为正整数")
        quantity = int(request.quantity)
        if side == "BUY" and quantity % 100:
            raise ValueError("A股买入数量必须为100股整数手")
        if order_type == "LIMIT" and (request.limit_price is None or request.limit_price <= 0):
            raise ValueError("限价单必须提供正的 limit_price")
        created = timestamp.isoformat() if isinstance(timestamp, datetime) else (timestamp or utc_now())
        order = Order(
            id=f"O{self._next_order_number:08d}", symbol=request.symbol,
            side=side, order_type=order_type, quantity=quantity, remaining=quantity,
            limit_price=price(request.limit_price) if request.limit_price else None,
            purpose=purpose, execution_mode=request.execution_mode,
            reason=request.reason, created_at=created,
        )
        self._next_order_number += 1
        position = self.position(request.symbol)
        if side == "SELL":
            sellable = position.permanent_sellable_qty if purpose == "PERMANENT" else min(
                position.t_qty, position.permanent_sellable_qty
            )
            if quantity > sellable:
                order.status = "REJECTED"
                order.message = f"T+1可卖数量不足: 请求{quantity}, 可卖{sellable}"
        self.orders.append(order)
        return order

    def cancel(self, order_id: str) -> bool:
        for order in self.orders:
            if order.id == order_id and order.status in {"PENDING", "PARTIALLY_FILLED"}:
                order.status = "CANCELLED"
                order.message = "用户撤单"
                return True
        return False

    def cancel_open_orders(self, message: str = "日终取消", modes: Iterable[str] | None = None) -> None:
        allowed = set(modes) if modes is not None else None
        for order in self.orders:
            if order.status in {"PENDING", "PARTIALLY_FILLED"} and (allowed is None or order.execution_mode in allowed):
                order.status = "CANCELLED"
                order.message = message

    def _fees(self, side: str, amount: float) -> FeeBreakdown:
        """五项费用分项计算后相加；最低佣金只兜底佣金项；印花税仅卖出收取。"""
        commission = money(max(self.config.min_commission, amount * self.config.commission_rate))
        stamp_tax = money(amount * self.config.stamp_tax_rate) if side == "SELL" else 0.0
        handling_fee = money(amount * self.config.handling_fee_rate)
        regulatory_fee = money(amount * self.config.regulatory_fee_rate)
        transfer_fee = money(amount * self.config.transfer_fee_rate)
        return FeeBreakdown(
            commission=commission, stamp_tax=stamp_tax,
            handling_fee=handling_fee, regulatory_fee=regulatory_fee,
            transfer_fee=transfer_fee,
            total_fee=money(commission + stamp_tax + handling_fee + regulatory_fee + transfer_fee),
        )

    def _limit_price(self, order: Order, bar: Bar) -> float | None:
        if order.order_type == "MARKET":
            return bar.open
        assert order.limit_price is not None
        limit = order.limit_price
        if order.side == "BUY":
            if bar.open <= limit:
                return bar.open
            if bar.low <= limit <= bar.high:
                return limit
        else:
            if bar.open >= limit:
                return bar.open
            if bar.low <= limit <= bar.high:
                return limit
        return None

    def _tradable(self, side: str, base_price: float, previous_close: float | None) -> bool:
        if previous_close is None or previous_close <= 0:
            return True
        upper = round(previous_close * (1 + self.config.limit_pct), 2)
        lower = round(previous_close * (1 - self.config.limit_pct), 2)
        if base_price < lower or base_price > upper:
            return False
        if side == "BUY" and base_price >= upper:
            return False
        if side == "SELL" and base_price <= lower:
            return False
        return True

    def process_bar(self, bar: Bar, modes: Iterable[str] | None = None) -> list[Fill]:
        position = self.position(bar.symbol)
        previous_close = bar.prev_close or position.last_price
        position.last_price = bar.close
        allowed = set(modes) if modes is not None else None
        pending = [
            order for order in self.orders
            if order.symbol == bar.symbol and order.status in {"PENDING", "PARTIALLY_FILLED"}
            and (allowed is None or order.execution_mode in allowed)
        ]
        filled: list[Fill] = []
        used_volume = 0.0
        for order in pending:
            base = self._limit_price(order, bar)
            if base is None:
                continue
            if not self._tradable(order.side, base, bar.prev_close or previous_close):
                order.message = "涨跌停或超出价格限制，不可成交"
                continue
            capacity = max(0, int((bar.volume * self.config.volume_participation - used_volume) / 100) * 100)
            if capacity <= 0:
                order.message = "成交量参与度额度不足"
                continue
            quantity = min(order.remaining, capacity)
            position = self.position(order.symbol)
            if order.side == "SELL":
                sellable = position.permanent_sellable_qty if order.purpose == "PERMANENT" else min(
                    position.t_qty, position.permanent_sellable_qty
                )
                quantity = min(quantity, sellable)
            if quantity < 100:
                order.message = "可成交数量不足一手"
                continue
            slippage_rate = self.config.slippage_bps / 10000.0
            execution = price(base * (1 + slippage_rate if order.side == "BUY" else 1 - slippage_rate))
            if not self._tradable(order.side, execution, bar.prev_close or previous_close):
                continue
            amount = money(execution * quantity)
            fees = self._fees(order.side, amount)
            if order.side == "BUY":
                while quantity >= 100 and amount + fees.total_fee > self.cash:
                    quantity -= 100
                    if quantity:
                        amount = money(execution * quantity)
                        fees = self._fees(order.side, amount)
                if quantity < 100:
                    order.message = "可用资金不足"
                    continue
            realized = self._apply_execution(
                order, position, quantity, execution, amount, fees.total_fee,
            )
            used_volume += quantity
            self.total_fees = money(self.total_fees + fees.total_fee)
            fill = Fill(
                id=f"F{self._next_fill_number:08d}", order_id=order.id, symbol=order.symbol,
                side=order.side, quantity=quantity, price=execution, amount=amount,
                commission=fees.commission, stamp_tax=fees.stamp_tax,
                handling_fee=fees.handling_fee, regulatory_fee=fees.regulatory_fee,
                transfer_fee=fees.transfer_fee, total_fee=fees.total_fee,
                slippage=money(abs(execution - base) * quantity),
                timestamp=bar.timestamp.isoformat(), purpose=order.purpose,
                cash_flow=money(-amount - fees.total_fee if order.side == "BUY" else amount - fees.total_fee),
                realized_pnl=money(realized),
            )
            self._next_fill_number += 1
            self.trades.append(fill)
            filled.append(fill)
            old_filled = order.filled_quantity
            order.filled_quantity += quantity
            order.remaining -= quantity
            order.avg_fill_price = price(
                (order.avg_fill_price * old_filled + execution * quantity) / order.filled_quantity
            )
            order.commission = money(order.commission + fees.commission)
            order.stamp_tax = money(order.stamp_tax + fees.stamp_tax)
            order.handling_fee = money(order.handling_fee + fees.handling_fee)
            order.regulatory_fee = money(order.regulatory_fee + fees.regulatory_fee)
            order.transfer_fee = money(order.transfer_fee + fees.transfer_fee)
            order.total_fee = money(order.total_fee + fees.total_fee)
            order.status = "FILLED" if order.remaining == 0 else "PARTIALLY_FILLED"
            order.message = ""
            if order.side == "BUY" and order.purpose == "PERMANENT" and not order.sequence_counted:
                self.sequence_index += 1
                order.sequence_counted = True
        return filled

    def _apply_execution(
        self, order: Order, position: Position, quantity: int,
        execution: float, amount: float, total_fee: float,
    ) -> float:
        position.last_price = execution
        if order.side == "BUY":
            self.cash = money(self.cash - amount - total_fee)
            if order.purpose == "PERMANENT":
                position.permanent_qty += quantity
                position.permanent_cost = money(position.permanent_cost + amount + total_fee)
                position.today_permanent_buy_qty += quantity
            else:
                position.t_qty += quantity
                position.t_cost = money(position.t_cost + amount + total_fee)
                position.today_t_buy_qty += quantity
            return 0.0

        self.cash = money(self.cash + amount - total_fee)
        net_proceeds = money(amount - total_fee)
        if order.purpose == "PERMANENT":
            if position.permanent_qty < quantity:
                raise RuntimeError("永久仓账本可卖数量校验失败")
            cost_allocated = money(position.permanent_cost * quantity / position.permanent_qty)
            position.permanent_qty -= quantity
            position.permanent_cost = money(max(0.0, position.permanent_cost - cost_allocated))
            realized = money(net_proceeds - cost_allocated)
        else:
            if position.t_qty < quantity or position.permanent_sellable_qty < quantity:
                raise RuntimeError("T账本或旧仓可卖数量校验失败")
            t_cost_allocated = money(position.t_cost * quantity / position.t_qty)
            permanent_cost_before = position.permanent_cost
            # 正T卖出旧仓，同时把等量当日T买入转入永久仓；永久仓原始成本不变。
            position.permanent_qty -= quantity
            position.t_qty -= quantity
            position.t_cost = money(max(0.0, position.t_cost - t_cost_allocated))
            position.permanent_qty += quantity
            position.permanent_cost = permanent_cost_before
            realized = money(net_proceeds - t_cost_allocated)
            self.t_net_profit = money(self.t_net_profit + realized)
        self.realized_pnl = money(self.realized_pnl + realized)
        if position.total_qty == 0:
            position.permanent_cost = 0.0
            position.t_cost = 0.0
        return realized

    def mark(self, symbol: str, last_price: float) -> None:
        self.position(symbol).last_price = price(last_price)

    def to_dict(self, include_internal: bool = False) -> dict:
        result = {
            "id": self.id, "name": self.name, "initial_cash": self.initial_cash,
            "cash": self.cash, "available_cash": self.available_cash,
            "equity": self.equity, "market_value": self.market_value,
            "realized_pnl": self.realized_pnl, "total_fees": self.total_fees,
            "positions": [position.public_dict() for position in self.positions.values() if position.total_qty or position.last_price],
            "orders": [order.to_dict() for order in self.orders],
            "trades": [fill.to_dict() for fill in self.trades],
            "t_net_profit": self.t_net_profit, "sequence_index": self.sequence_index,
            "armed_state": self.armed_state, "status": self.status,
            "auto_strategy": self.auto_strategy,
            "config": self.config.to_dict(), "dataset_id": self.dataset_id, "cursor": self.cursor,
        }
        if include_internal:
            result.update({
                "_positions_internal": {key: asdict(value) for key, value in self.positions.items()},
                "_next_order_number": self._next_order_number,
                "_next_fill_number": self._next_fill_number,
                "current_date": self.current_date,
                "strategy_state": self.strategy_state,
                "last_daily_signal_date": self.last_daily_signal_date,
            })
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "BrokerAccount":
        account = cls(
            initial_cash=data["initial_cash"], config=StrategyConfig.from_dict(data.get("config")),
            name=data["name"], account_id=data["id"], dataset_id=data.get("dataset_id"),
        )
        for field in ("cash", "cursor", "status", "armed_state", "sequence_index", "realized_pnl", "total_fees", "t_net_profit", "current_date", "auto_strategy", "last_daily_signal_date"):
            if field in data:
                setattr(account, field, data[field])
        account.strategy_state = dict(data.get("strategy_state", {}))
        account.positions = {
            symbol: Position(**value) for symbol, value in data.get("_positions_internal", {}).items()
        }
        account.orders = [Order(**item) for item in data.get("orders", [])]
        account.trades = [Fill(**item) for item in data.get("trades", [])]
        account._next_order_number = data.get("_next_order_number", len(account.orders) + 1)
        account._next_fill_number = data.get("_next_fill_number", len(account.trades) + 1)
        return account
