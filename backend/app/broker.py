from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any, Iterable

from .domain import (
    Bar, FeeBreakdown, Fill, Order, OrderRequest, Position, StrategyConfig, Withdrawal, money, price, utc_now,
)
from .trading_rules import (
    BoardRules, is_etf_code, rules_for, split_quantity, validate_order_amount,
    validate_quantity,
)

# 注：行情 bar.volume 的单位口径统一在**数据入口**（market.py 的腾讯解析处）换算成「股」，
# broker 不再按 order.purpose 猜单位（旧的 LOTS_PER_SHARE / BETA-only ×100 已删除）。
# 腾讯快照 / 5 分钟 / 日线 volume 单位是「手」，已在 market.py ×100；quan parquet（回测、
# /advance）本来就是「股」。因此这里直接用 bar.volume 即可。


def add_position_buy_cost(
    cost: float, fee_in_cost: float, amount: float, total_fee: float,
) -> tuple[float, float]:
    """买入记入单个成本账本：成交金额 + 买入费用一并计入成本。

    返回 ``(cost, fee_in_cost)``，两条口径都由本函数维护：``cost`` 是含买入费的
    持仓成本（``permanent_cost`` / ``t_cost`` / ``beta_cost``），``fee_in_cost`` 是
    单独曝光给前端的「买入费用」分项。撮合（``_apply_execution``）与读时成本重放
    （``paper_analytics`` 的 ``max_invested``）共用同一份实现，不允许出现第二套
    成本分配口径（AGENTS 架构原则 1）。
    """
    return money(cost + amount + total_fee), money(fee_in_cost + total_fee)


def allocate_ledger_on_sell(
    cost: float, fee_in_cost: float, quantity: int, held: int,
) -> tuple[float, float]:
    """按剩余持仓比例分摊一次卖出的成本与买入费用。

    返回 ``(成本分摊, 买入费用分摊)``；与 ``add_position_buy_cost`` 配对，是成本
    账本的唯一分配实现（撮合与读时重放共用）。
    """
    if held <= 0:
        return 0.0, 0.0
    return money(cost * quantity / held), money(fee_in_cost * quantity / held)


def _replay_buy_fees(fills: Iterable[Fill]) -> tuple[float, float]:
    """按成交顺序重放，推算当前仍留在持仓成本内的买入手续费。

    仅用于兼容新增 buy_fee 字段之前的旧账户（其 _positions_internal 里没有
    permanent_fee/t_fee）；新成交会直接累加/分摊，不依赖此函数。
    ETF beta 腿（BETA）不计入本函数——旧账户不会有 BETA 成交。
    """
    permanent_qty = 0
    t_qty = 0
    permanent_fee = 0.0
    t_fee = 0.0
    for fill in fills:
        if fill.purpose == "BETA":
            continue
        if fill.side == "BUY":
            if fill.purpose == "PERMANENT":
                permanent_qty += fill.quantity
                permanent_fee += fill.total_fee
            else:
                t_qty += fill.quantity
                t_fee += fill.total_fee
            continue
        if fill.purpose == "PERMANENT":
            if permanent_qty <= 0:
                continue
            sold = min(fill.quantity, permanent_qty)
            _, allocated = allocate_ledger_on_sell(0.0, permanent_fee, sold, permanent_qty)
            permanent_qty -= sold
            permanent_fee = max(0.0, permanent_fee - allocated)
        else:
            if t_qty <= 0:
                continue
            sold = min(fill.quantity, t_qty)
            _, allocated = allocate_ledger_on_sell(0.0, t_fee, sold, t_qty)
            t_qty -= sold
            t_fee = max(0.0, t_fee - allocated)
    return money(permanent_fee), money(t_fee)


def _sellable_for(position: Position, purpose: str) -> int:
    """按账本口径返回 T+1 可卖数量（PERMANENT=永久仓、T=做T仓、BETA=ETF 腿）。"""
    if purpose == "PERMANENT":
        return position.permanent_sellable_qty
    if purpose == "T":
        return min(position.t_qty, position.permanent_sellable_qty)
    return position.beta_sellable_qty


def _next_day_sellable_for(position: Position, purpose: str) -> int:
    """隔夜单（NEXT_OPEN）次开执行前的可卖口径：不含「今日买入」锁定。

    NEXT_OPEN 委托在下一交易日开盘撮合，撮合前 ``new_day`` 必然清零
    ``today_*_buy_qty``，所以「今日买入的份额」明日即可卖——真实券商对
    收盘后委托的次开卖单也按次日持仓放行。若提交时仍按当日口径校验，
    「今买→明开卖」的合法隔夜卖单会被误拒（清仓被迫延迟一天）。
    注意：不改变持仓总量校验——卖超过实际持仓仍会被拒。
    """
    if purpose == "PERMANENT":
        return max(0, position.permanent_qty)
    if purpose == "T":
        return max(0, min(position.t_qty, position.permanent_qty))
    return max(0, position.beta_qty)


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
        # 账户创建时间：读时派生指标（年化/XIRR 等）需要「起点」，不落独立表。
        # 旧 payload 没有该字段时由 from_dict 回填并置 created_at_estimated=True。
        self.created_at: str = utc_now()
        self.created_at_estimated: bool = False
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
        # 池内轮动 v2（清仓后重新选股）配置：与账户一起持久化，由 rotation.py 维护。
        self.rotation_enabled: bool = False
        self.rotation_pool: list[dict] = []
        self.rotation_index: int = -1
        self.rotation_waiting: bool = False
        self.rotation_note: str = ""
        self.rotation_run_id: str | None = None
        # 轮动 v2：清仓后重新运行五层选股（rescreen）所需的配置与最近一次筛选快照。
        self.rotation_config: dict = {}
        self.rotation_last_attempt_date: str | None = None
        self.rotation_last_run_id: str | None = None
        self.rotation_last_screen: dict = {}
        # 只有「刚结束一轮」才允许轮动推进；新账户空仓不能直接跳到池内第二只。
        self.rotation_pending_advance: bool = False
        self._next_order_number = 1
        self._next_fill_number = 1
        # 盈利提现：已提取金额累计、明细记录、序号、上次已评估的月份（防跨月重复评估）。
        self.total_withdrawn = 0.0
        self.withdrawals: list[Withdrawal] = []
        self._next_withdrawal_number = 1
        self._last_month_eval: str | None = None
        # 性能索引（派生状态，不参与快照/序列化，restore/from_dict 时按 orders 重建）：
        # - _open_by_symbol：每标的「未结」（PENDING/PARTIALLY_FILLED）委托桶，桶内保持
        #   创建顺序。让 process_bar 只扫该标的的小桶，而不是每次遍历 append-only 的
        #   self.orders 全表（6 年回测会积累数千笔，O(n_bars×n_orders) → O(n_bars×桶)）。
        # - _open_orders：全部未结委托，保持全局创建顺序。reserved_cash 按它求和，
        #   与旧实现「遍历 self.orders 全表但跳过已结」的浮点累加顺序逐项一致，保证
        #   reserved_cash 数值 bit 级不变；且只扫未结（通常个位数）而非全表数千笔。
        #   orders 本身保持完整（含终态）用于报表/序列化，snapshot_state 照旧全量输出。
        self._open_by_symbol: dict[str, list[Order]] = {}
        self._open_orders: list[Order] = []

    # -- 快照 / 恢复（供回测断点续传使用） --------------------------------
    #
    # 目标：把账户在某个「交易日边界」的完整可变状态序列化成 JSON，之后能原样
    # 还原，使续传后的撮合/决策与一次性跑完逐字节一致。采用「遍历 __dict__ +
    # 对已知 dataclass 类型做 to_dict/重建」的通用做法，避免逐字段白名单
    # （新增字段自动被覆盖，不会漏）。config 不参与快照：它由构造时传入且运行期
    # 不变（交易进行中配置定格，见 AGENTS.md 架构原则 3），还原时保留当前 config。

    def snapshot_state(self) -> dict[str, Any]:
        """序列化账户可变状态为 JSON 友好的 dict（不含 config）。"""
        out: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if key == "config":
                continue
            # 未结委托索引是 orders 的派生状态（内含 Order 对象，非 JSON 友好），
            # 不序列化；restore_state 会按还原后的 orders 重建，故这里直接跳过。
            if key in ("_open_by_symbol", "_open_orders"):
                continue
            if key == "positions" and isinstance(value, dict):
                out[key] = {code: asdict(pos) for code, pos in value.items()}
            elif key == "orders" and isinstance(value, list):
                out[key] = [asdict(item) for item in value]
            elif key == "trades" and isinstance(value, list):
                out[key] = [asdict(item) for item in value]
            elif key == "withdrawals" and isinstance(value, list):
                out[key] = [asdict(item) for item in value]
            else:
                out[key] = value
        return out

    def restore_state(self, state: dict[str, Any]) -> None:
        """从 snapshot_state() 的输出还原账户状态（保留当前 config 对象）。"""
        current_config = self.config
        for key, value in state.items():
            if key == "config":
                continue
            if key == "positions" and isinstance(value, dict):
                self.positions = {code: Position(**pos) for code, pos in value.items()}
            elif key == "orders" and isinstance(value, list):
                self.orders = [Order(**item) for item in value]
                # orders 被整体替换，派生的未结索引必须按新 orders 重建。
                self._rebuild_open_index()
            elif key == "trades" and isinstance(value, list):
                self.trades = [Fill(**item) for item in value]
            elif key == "withdrawals" and isinstance(value, list):
                self.withdrawals = [Withdrawal(**item) for item in value]
            else:
                setattr(self, key, value)
        self.config = current_config

    # -- 未结委托索引（性能） ------------------------------------------------
    #
    # self.orders 是 append-only、从不清理的（6 年回测会积累数千笔）。若每次都
    # 遍历全表找未结委托，process_bar 会退化成 O(n_bars×n_orders)。这里额外维护
    # 两份**派生**索引（不参与快照/序列化，restore/from_dict 时按 orders 重建）：
    #   _open_by_symbol —— 每标的未结委托桶（桶内创建顺序），供 process_bar 精确取用；
    #   _open_orders   —— 全部未结委托（全局创建顺序），供 reserved_cash 求和。
    # 终态（FILLED/CANCELLED/REJECTED）委托一旦离开未结集合就从索引摘除；orders
    # 本身保持完整，snapshot_state/to_dict 仍全量输出每一笔。

    def _index_add(self, order: Order) -> None:
        """订单进入未结集合（PENDING/PARTIALLY_FILLED）时登记到两份索引。"""
        if order.status not in {"PENDING", "PARTIALLY_FILLED"}:
            return
        self._open_orders.append(order)
        self._open_by_symbol.setdefault(order.symbol, []).append(order)

    def _index_remove(self, order: Order) -> None:
        """订单离开未结集合（成交完/撤单/拒绝）时从两份索引摘除。"""
        try:
            self._open_orders.remove(order)
        except ValueError:
            pass
        bucket = self._open_by_symbol.get(order.symbol)
        if bucket is not None:
            try:
                bucket.remove(order)
            except ValueError:
                pass
            if not bucket:
                del self._open_by_symbol[order.symbol]

    def _rebuild_open_index(self) -> None:
        """按 self.orders（创建顺序）重建两份未结索引；restore/from_dict 后调用。"""
        self._open_orders = []
        self._open_by_symbol = {}
        for order in self.orders:
            self._index_add(order)

    @property
    def reserved_cash(self) -> float:
        # 只遍历未结买单（_open_orders，通常个位数），不再全表扫描数千笔委托。
        # 每笔的预留仍按「限价或该标的最新价」现算——因为 reference 依赖会随行情
        # 变动的 last_price，无法用纯增量标量维护；但遍历集合从「全部历史委托」
        # 收敛到「当前未结委托」，复杂度从 O(n_orders) 降到 O(n_open)。遍历保持
        # 创建顺序，与旧实现逐项累加顺序一致，浮点结果 bit 级相同。
        reserved = 0.0
        for order in self._open_orders:
            if order.side != "BUY":
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
    def has_stock_position(self) -> bool:
        """是否持有轮动个股（永久仓或做T仓）；ETF beta 腿不算。"""
        return any(position.stock_qty > 0 for position in self.positions.values())

    @property
    def beta_cost(self) -> float:
        return money(sum(position.beta_cost for position in self.positions.values()))

    @property
    def beta_market_value(self) -> float:
        return money(sum(position.beta_qty * position.last_price for position in self.positions.values()))

    @property
    def permanent_market_value(self) -> float:
        return money(sum(position.permanent_qty * position.last_price for position in self.positions.values()))

    def position(self, symbol: str) -> Position:
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol)
        return self.positions[symbol]

    def new_day(self, date: str, cancel_previous: bool = True) -> None:
        is_new_date = self.current_date != date
        previous_date = self.current_date
        if cancel_previous and self.current_date and is_new_date:
            self.cancel_open_orders("DAY_END")
        self.current_date = date
        # 只有真正跨交易日才清零「今日买入」锁定计数。撮合路径每根 K 线都会调用
        # new_day，若无条件 reset，当日成交后的下一根同日 K 线就会把 T+1 锁定
        # 抹掉，导致当日买入立刻变为可卖（可卖数量虚高）。
        if is_new_date:
            for position in self.positions.values():
                position.today_permanent_buy_qty = 0
                position.today_t_buy_qty = 0
                position.today_beta_buy_qty = 0
            # 盈利提现（每月模式）：跨月时评估「上一完整自然月」的已实现盈亏，
            # 在跨月首个交易日扣款。previous_date 为 None（账户首根 bar）时不评估。
            if previous_date is not None and previous_date[:7] != date[:7]:
                self._evaluate_monthly_withdrawal(previous_date[:7], date)

    # -- 盈利提现 ------------------------------------------------------------
    # 把已落袋盈利从账户现金中取出（真实资金离场）。两种模式：
    #   monthly  —— 每月跨月时按「上一自然月已实现盈亏」提取一次；
    #   per_trade—— 每笔盈利 SELL 成交后按该笔已实现盈亏提取。
    # 两种提取方式：
    #   ratio —— 提取当期盈利的 withdraw_ratio 比例；
    #   fixed —— 提取固定金额（当期盈利不足该金额时仍提取该金额，仅以现金余额封顶）。
    # 提现只扣 cash（进而降 equity 与可买资金），不改 realized_pnl（仍为毛口径）。

    def _withdrawal_raw_amount(self, period_profit: float) -> float:
        """按配置计算当期提现原始金额（未封顶）；不适用时返回 0。

        - ratio：当期盈利 × withdraw_ratio（负/零盈利 → 0）。
        - fixed：固定金额（若当月不盈利开关开启且无盈利 → 0；否则取固定金额）。
        """
        profit = money(period_profit)
        if self.config.withdraw_method == "ratio":
            if profit <= 0:
                return 0.0
            return money(profit * float(self.config.withdraw_ratio))
        # fixed 方式
        if self.config.withdraw_skip_when_no_profit and profit <= 0:
            return 0.0
        return money(float(self.config.withdraw_fixed_amount))

    def _execute_withdrawal(
        self, amount: float, mode: str, period_profit: float, trigger: str, date: str,
    ) -> Withdrawal | None:
        """封顶到可提现金并扣款记账；金额非正或无可提现金时跳过。"""
        cap = self.available_cash
        actual = money(min(amount, cap))
        if actual <= 0:
            return None
        self.cash = money(self.cash - actual)
        self.total_withdrawn = money(self.total_withdrawn + actual)
        record = Withdrawal(
            id=f"W{self._next_withdrawal_number:08d}",
            date=date, amount=actual, mode=mode,
            method=self.config.withdraw_method,
            period_profit=money(period_profit), trigger=trigger,
            cash_after=self.cash,
        )
        self._next_withdrawal_number += 1
        self.withdrawals.append(record)
        return record

    def _evaluate_monthly_withdrawal(self, prev_month: str, date: str) -> None:
        """跨月时评估上一完整自然月的每月提现（只执行一次，防重复/断点续传）。"""
        if self.config.withdraw_mode != "monthly":
            return
        if self._last_month_eval == prev_month:
            return
        self._last_month_eval = prev_month
        period_profit = sum(
            float(f.realized_pnl) for f in self.trades if str(f.timestamp)[:7] == prev_month
        )
        amount = self._withdrawal_raw_amount(period_profit)
        self._execute_withdrawal(amount, "monthly", period_profit, prev_month, date)

    def _evaluate_per_trade_withdrawal(self, fill: Fill) -> None:
        """每笔盈利 SELL 成交后的每笔提现。"""
        if self.config.withdraw_mode != "per_trade":
            return
        if fill.side != "SELL" or fill.realized_pnl <= 0:
            return
        amount = self._withdrawal_raw_amount(fill.realized_pnl)
        self._execute_withdrawal(amount, "per_trade", fill.realized_pnl, fill.id, fill.timestamp[:10])

    def board_rules(self, symbol: str) -> BoardRules:
        """标的对应板块的交易规则；合成标的回退旧口径。"""
        return rules_for(symbol)

    def submit(self, request: OrderRequest, timestamp: datetime | str | None = None) -> Order:
        side = request.side.upper()
        order_type = request.order_type.upper()
        purpose = request.purpose.upper()
        if side not in {"BUY", "SELL"} or order_type not in {"MARKET", "LIMIT"}:
            raise ValueError("side/order_type 不合法")
        if purpose not in {"PERMANENT", "T", "BETA"}:
            raise ValueError("purpose 必须为 PERMANENT、T 或 BETA")
        if isinstance(request.quantity, bool) or int(request.quantity) != request.quantity or request.quantity <= 0:
            raise ValueError("quantity 必须为正整数")
        if order_type == "LIMIT" and (request.limit_price is None or request.limit_price <= 0):
            raise ValueError("限价单必须提供正的 limit_price")
        quantity = int(request.quantity)
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
        # 分板块数量口径：科创板 ≥200 股可 1 股递增，创业板/主板 100 股整数倍；
        # 不合规申报记为 REJECTED 订单（不再抛异常），避免长周期回测被一笔申报打断。
        rules = self.board_rules(request.symbol)
        quantity_reason = validate_quantity(
            request.symbol, quantity, side,
            sellable_qty=position.permanent_sellable_qty if side == "SELL" else None,
        )
        # 单笔申报数量上限（按股数）：限价/市价分别校验，与价格/金额无关。
        # 注意市价单没有委托价也能校验，不能因缺 reference 就跳过（旧版曾漏检市价超额）。
        amount_reason = validate_order_amount(request.symbol, quantity, order_type)
        reject_reason = quantity_reason or amount_reason
        if reject_reason:
            order.status = "REJECTED"
            order.message = reject_reason
            self.orders.append(order)
            return order
        if side == "SELL":
            sellable = _sellable_for(position, purpose)
            if order.execution_mode == "NEXT_OPEN":
                # 隔夜单次开才成交，提交时按次日口径校验（见 _next_day_sellable_for）；
                # 当日口径（NEXT_BAR/即时撮合）仍用含 T+1 锁定的 sellable。
                sellable = max(sellable, _next_day_sellable_for(position, purpose))
            if quantity > sellable:
                order.status = "REJECTED"
                order.message = f"T+1可卖数量不足: 请求{quantity}, 可卖{sellable}"
        self.orders.append(order)
        self._index_add(order)  # 仅 PENDING/PARTIALLY_FILLED 会真正入索引（REJECTED 自动跳过）
        return order

    def submit_split(self, request: OrderRequest, timestamp: datetime | str | None = None) -> list[Order]:
        """按板块单笔申报上限拆分后逐笔提交策略委托。

        策略层（清仓/止盈/做 T）按「整笔数量」生成请求，但交易所对单笔市价/限价
        申报有股数上限（创业板/科创板市价 5 万股），整笔提交会被拒。这里统一
        拆分，保证回测与模拟盘自动策略共用同一口径；手工委托仍走 ``submit``，
        保持「超额即拒绝」的真实反馈。子单复用同一 reason/execution_mode，
        成交与费用在撮合时逐笔累计。
        """
        orders: list[Order] = []
        for quantity in split_quantity(request.symbol, request.quantity, request.order_type):
            orders.append(self.submit(OrderRequest(
                symbol=request.symbol, side=request.side, quantity=quantity,
                order_type=request.order_type, limit_price=request.limit_price,
                purpose=request.purpose, execution_mode=request.execution_mode,
                reason=request.reason,
            ), timestamp))
        return orders

    def cancel(self, order_id: str) -> bool:
        for order in self.orders:
            if order.id == order_id and order.status in {"PENDING", "PARTIALLY_FILLED"}:
                order.status = "CANCELLED"
                order.message = "用户撤单"
                self._index_remove(order)  # 离开未结集合，从索引摘除
                return True
        return False

    def cancel_open_orders(self, message: str = "日终取消", modes: Iterable[str] | None = None) -> None:
        allowed = set(modes) if modes is not None else None
        # 只遍历未结索引（_open_orders 保持创建顺序、且成员状态恒为 PENDING/PARTIALLY_
        # FILLED），与旧实现遍历 self.orders 全表再过滤未结完全等价，但不再扫数千笔历史。
        cancelled = [
            order for order in self._open_orders
            if allowed is None or order.execution_mode in allowed
        ]
        for order in cancelled:
            order.status = "CANCELLED"
            order.message = message
        # 遍历 _open_orders 时不能边遍历边改索引，撤完统一从索引摘除。
        for order in cancelled:
            self._index_remove(order)

    def _fees(self, side: str, amount: float, symbol: str = "") -> FeeBreakdown:
        """五项费用分项计算后相加；最低佣金只兜底佣金项。

        场内 ETF/LOF（代码段判定）与股票费用口径不同：**免征印花税、免过户费**，
        只收佣金、经手费、证管费。旧实现不分标的、一律按股票口径收卖出印花税 +
        过户费，导致 ETF beta 腿被多扣成本（回测复盘中 beta 腿 4 年多扣约 1.5 万元）。
        合成标的（TEST/SYNTH001 等非 6 位代码）按普通股票口径，保持旧行为。
        """
        fund = is_etf_code(symbol)
        commission = money(max(self.config.min_commission, amount * self.config.commission_rate))
        stamp_tax = money(amount * self.config.stamp_tax_rate) if side == "SELL" and not fund else 0.0
        handling_fee = money(amount * self.config.handling_fee_rate)
        regulatory_fee = money(amount * self.config.regulatory_fee_rate)
        transfer_fee = 0.0 if fund else money(amount * self.config.transfer_fee_rate)
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

    def _limit_pct(self, symbol: str) -> float:
        """涨跌幅口径：真实板块按板块规则，合成标的沿用 config.limit_pct（旧口径）。"""
        from .trading_rules import board_of

        board = board_of(symbol)
        return rules_for(symbol).limit_pct if board else self.config.limit_pct

    def _tradable(self, side: str, base_price: float, previous_close: float | None, symbol: str = "") -> bool:
        if previous_close is None or previous_close <= 0:
            return True
        limit_pct = self._limit_pct(symbol)
        upper = round(previous_close * (1 + limit_pct), 2)
        lower = round(previous_close * (1 - limit_pct), 2)
        if base_price < lower or base_price > upper:
            return False
        if side == "BUY" and base_price >= upper:
            return False
        if side == "SELL" and base_price <= lower:
            return False
        return True

    def _fill_step(self, order: Order) -> int:
        """按板块返回可成交数量的递增步长；零股整笔卖出的最小成交=委托数量本身。

        科创板买入可 1 股递增；创业板/主板 100 股一手。持仓低于板块最小申报单位的
        整笔清仓委托，最小成交等于委托数量（不能被 step 截断成 0 而拒绝成交）。
        """
        rules = self.board_rules(order.symbol)
        if order.side == "SELL" and order.quantity < rules.min_qty:
            return max(1, order.quantity)
        return max(1, rules.step)

    def process_bar(self, bar: Bar, modes: Iterable[str] | None = None) -> list[Fill]:
        position = self.position(bar.symbol)
        previous_close = bar.prev_close or position.last_price
        position.last_price = bar.close
        allowed = set(modes) if modes is not None else None
        # 只取该标的的未结委托桶（桶内保持创建顺序，与旧实现遍历 self.orders 全表
        # 过滤出的顺序一致），再套 execution_mode 过滤。先快照成 list：下面的成交
        # 循环会把成交完的委托从索引摘除（改动 bucket），不能边遍历 bucket 边删。
        pending = [
            order for order in self._open_by_symbol.get(bar.symbol, ())
            if allowed is None or order.execution_mode in allowed
        ]
        if not pending:
            return []
        filled: list[Fill] = []
        used_volume = 0.0
        for order in pending:
            base = self._limit_price(order, bar)
            if base is None:
                continue
            if not self._tradable(order.side, base, bar.prev_close or previous_close, order.symbol):
                order.message = "涨跌停或超出价格限制，不可成交"
                continue
            step = self._fill_step(order)
            # bar.volume 已在数据入口统一为「股」（腾讯源 ×100，quan 源本就是股），
            # 这里不再按 purpose 猜单位。used_volume 累计的也是股，两边同单位。
            capacity = max(
                0,
                int((bar.volume * self.config.volume_participation - used_volume) / step) * step,
            )
            if capacity <= 0:
                order.message = "成交量参与度额度不足"
                continue
            quantity = min(order.remaining, capacity)
            position = self.position(order.symbol)
            if order.side == "SELL":
                quantity = min(quantity, _sellable_for(position, order.purpose))
            if quantity < step:
                order.message = "可成交数量不足一个申报单位"
                continue
            slippage_rate = self.config.slippage_bps / 10000.0
            execution = price(base * (1 + slippage_rate if order.side == "BUY" else 1 - slippage_rate))
            if not self._tradable(order.side, execution, bar.prev_close or previous_close, order.symbol):
                continue
            amount = money(execution * quantity)
            fees = self._fees(order.side, amount, order.symbol)
            if order.side == "BUY":
                while quantity >= step and amount + fees.total_fee > self.cash:
                    quantity -= step
                    if quantity:
                        amount = money(execution * quantity)
                        fees = self._fees(order.side, amount, order.symbol)
                if quantity < step:
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
            # 盈利提现（每笔模式）：盈利卖出落袋后立即评估。
            self._evaluate_per_trade_withdrawal(fill)
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
            if order.status == "FILLED":
                # 全部成交 → 离开未结集合，从两份索引摘除（部分成交仍留在索引里）。
                self._index_remove(order)
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
            if order.purpose == "BETA":
                position.beta_qty += quantity
                position.beta_cost, position.beta_fee = add_position_buy_cost(
                    position.beta_cost, position.beta_fee, amount, total_fee,
                )
                position.today_beta_buy_qty += quantity
                return 0.0
            if order.purpose == "PERMANENT":
                position.permanent_qty += quantity
                position.permanent_cost, position.permanent_fee = add_position_buy_cost(
                    position.permanent_cost, position.permanent_fee, amount, total_fee,
                )
                position.today_permanent_buy_qty += quantity
            else:
                position.t_qty += quantity
                position.t_cost, position.t_fee = add_position_buy_cost(
                    position.t_cost, position.t_fee, amount, total_fee,
                )
                position.today_t_buy_qty += quantity
            return 0.0

        self.cash = money(self.cash + amount - total_fee)
        net_proceeds = money(amount - total_fee)
        if order.purpose == "BETA":
            if position.beta_qty < quantity:
                raise RuntimeError("ETF beta 账本可卖数量校验失败")
            cost_allocated, fee_allocated = allocate_ledger_on_sell(
                position.beta_cost, position.beta_fee, quantity, position.beta_qty,
            )
            position.beta_qty -= quantity
            position.beta_cost = money(max(0.0, position.beta_cost - cost_allocated))
            position.beta_fee = money(max(0.0, position.beta_fee - fee_allocated))
            realized = money(net_proceeds - cost_allocated)
        elif order.purpose == "PERMANENT":
            if position.permanent_qty < quantity:
                raise RuntimeError("永久仓账本可卖数量校验失败")
            cost_allocated, fee_allocated = allocate_ledger_on_sell(
                position.permanent_cost, position.permanent_fee, quantity, position.permanent_qty,
            )
            position.permanent_qty -= quantity
            position.permanent_cost = money(max(0.0, position.permanent_cost - cost_allocated))
            position.permanent_fee = money(max(0.0, position.permanent_fee - fee_allocated))
            realized = money(net_proceeds - cost_allocated)
        else:
            if position.t_qty < quantity or position.permanent_sellable_qty < quantity:
                raise RuntimeError("T账本或旧仓可卖数量校验失败")
            t_cost_allocated, t_fee_allocated = allocate_ledger_on_sell(
                position.t_cost, position.t_fee, quantity, position.t_qty,
            )
            permanent_cost_before = position.permanent_cost
            # 正T卖出旧仓，同时把等量当日T买入转入永久仓；永久仓原始成本不变。
            position.permanent_qty -= quantity
            position.t_qty -= quantity
            position.t_cost = money(max(0.0, position.t_cost - t_cost_allocated))
            position.t_fee = money(max(0.0, position.t_fee - t_fee_allocated))
            position.permanent_qty += quantity
            position.permanent_cost = permanent_cost_before
            realized = money(net_proceeds - t_cost_allocated)
            self.t_net_profit = money(self.t_net_profit + realized)
        self.realized_pnl = money(self.realized_pnl + realized)
        if position.total_qty == 0:
            position.permanent_cost = 0.0
            position.t_cost = 0.0
            position.permanent_fee = 0.0
            position.t_fee = 0.0
            position.beta_cost = 0.0
            position.beta_fee = 0.0
        return realized

    def mark(self, symbol: str, last_price: float) -> None:
        self.position(symbol).last_price = price(last_price)

    def to_dict(self, include_internal: bool = False) -> dict:
        from .rotation import rotation_summary  # 延迟导入，避免 broker ↔ rotation 循环依赖

        # positions 只返回**真实持仓**：仅收到行情/挂过被拒委托但未成交时，
        # 内部 Position 只缓存 last_price，不能算持仓，否则 UI 会出现
        # 「没订单没成交却有持仓」的幽灵行。行情缓存单独放 quotes。
        positions = [position for position in self.positions.values() if position.total_qty > 0]
        quotes = [
            {"symbol": position.symbol, "last_price": price(position.last_price)}
            for position in self.positions.values() if position.last_price > 0
        ]

        result = {
            "id": self.id, "name": self.name, "initial_cash": self.initial_cash,
            # 创建时间：读时派生指标（年化/XIRR）需要起点；旧账户回填时 estimated=True。
            "created_at": self.created_at, "created_at_estimated": self.created_at_estimated,
            # 当前轮入口日：只读派生字段（来自 strategy_state），前端据此显示「本轮持有天数」。
            # 注意：strategy_state 本身仍只在 include_internal 里落库，不在这里整体暴露。
            "round_entry_date": (self.strategy_state or {}).get("round_entry_date"),
            "cash": self.cash, "available_cash": self.available_cash,
            "equity": self.equity, "market_value": self.market_value,
            "realized_pnl": self.realized_pnl, "total_fees": self.total_fees,
            "total_withdrawn": self.total_withdrawn,
            "withdrawals": [w.to_dict() for w in self.withdrawals],
            "positions": [position.public_dict() for position in positions],
            "quotes": quotes,
            "orders": [order.to_dict() for order in self.orders],
            "trades": [fill.to_dict() for fill in self.trades],
            "t_net_profit": self.t_net_profit, "sequence_index": self.sequence_index,
            "armed_state": self.armed_state, "status": self.status,
            "auto_strategy": self.auto_strategy,
            "config": self.config.to_dict(), "dataset_id": self.dataset_id, "cursor": self.cursor,
            "rotation": rotation_summary(self),
        }
        if include_internal:
            result.update({
                "_positions_internal": {key: asdict(value) for key, value in self.positions.items()},
                "_next_order_number": self._next_order_number,
                "_next_fill_number": self._next_fill_number,
                "_next_withdrawal_number": self._next_withdrawal_number,
                "_last_month_eval": self._last_month_eval,
                "current_date": self.current_date,
                "strategy_state": self.strategy_state,
                "last_daily_signal_date": self.last_daily_signal_date,
                "rotation_pending_advance": self.rotation_pending_advance,
                "rotation_config": self.rotation_config,
                "rotation_last_attempt_date": self.rotation_last_attempt_date,
                "rotation_last_run_id": self.rotation_last_run_id,
                "rotation_last_screen": self.rotation_last_screen,
            })
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "BrokerAccount":
        account = cls(
            initial_cash=data["initial_cash"], config=StrategyConfig.from_dict(data.get("config")),
            name=data["name"], account_id=data["id"], dataset_id=data.get("dataset_id"),
        )
        for field in ("cash", "cursor", "status", "armed_state", "sequence_index", "realized_pnl", "total_fees", "t_net_profit", "current_date", "auto_strategy", "last_daily_signal_date", "total_withdrawn", "_last_month_eval"):
            if field in data:
                setattr(account, field, data[field])
        # created_at 兼容旧 payload：新字段缺失时用 payload 里的 updated_at（若有）或当前
        # 时间兜底，并标记为「推定」，供后续年化/XIRR 等指标标注「自该日起，样本可能不完整」。
        stored_created_at = data.get("created_at")
        if stored_created_at:
            account.created_at = str(stored_created_at)
            account.created_at_estimated = bool(data.get("created_at_estimated", False))
        else:
            fallback = data.get("updated_at")
            account.created_at = str(fallback) if fallback else utc_now()
            account.created_at_estimated = True
        account.strategy_state = dict(data.get("strategy_state", {}))
        rotation = data.get("rotation") or {}
        account.rotation_enabled = bool(rotation.get("enabled", data.get("rotation_enabled", False)))
        account.rotation_pool = list(rotation.get("pool") or data.get("rotation_pool") or [])
        account.rotation_index = int(rotation.get("index", data.get("rotation_index", -1)))
        account.rotation_waiting = bool(rotation.get("waiting", data.get("rotation_waiting", False)))
        account.rotation_note = str(rotation.get("note", data.get("rotation_note", "")) or "")
        run_id = rotation.get("run_id", data.get("rotation_run_id"))
        account.rotation_run_id = str(run_id) if run_id else None
        config = data.get("rotation_config", rotation.get("config"))
        account.rotation_config = dict(config) if isinstance(config, dict) else {}
        last_attempt = data.get("rotation_last_attempt_date", rotation.get("last_attempt_date"))
        account.rotation_last_attempt_date = str(last_attempt) if last_attempt else None
        last_run_id = data.get("rotation_last_run_id", rotation.get("last_run_id"))
        account.rotation_last_run_id = str(last_run_id) if last_run_id else None
        last_screen = data.get("rotation_last_screen", rotation.get("last_screen"))
        account.rotation_last_screen = dict(last_screen) if isinstance(last_screen, dict) else {}
        account.rotation_pending_advance = bool(
            data.get("rotation_pending_advance", rotation.get("pending_advance", False))
        )
        account.positions = {
            symbol: Position(**value) for symbol, value in data.get("_positions_internal", {}).items()
        }
        account.orders = [Order(**item) for item in data.get("orders", [])]
        # orders 被整体替换，派生的未结索引必须按新 orders 重建（快照不含索引）。
        account._rebuild_open_index()
        account.trades = [Fill(**item) for item in data.get("trades", [])]
        account.withdrawals = [Withdrawal(**item) for item in data.get("withdrawals", [])]
        account._next_withdrawal_number = data.get(
            "_next_withdrawal_number", len(account.withdrawals) + 1,
        )
        # 兼容旧账户：_positions_internal 里没有 permanent_fee/t_fee 时按成交重放补齐。
        raw_positions = data.get("_positions_internal", {})
        for symbol, raw in raw_positions.items():
            if "permanent_fee" in raw or "t_fee" in raw:
                continue
            position = account.positions.get(symbol)
            if position is None:
                continue
            permanent_fee, t_fee = _replay_buy_fees(
                fill for fill in account.trades if fill.symbol == symbol
            )
            position.permanent_fee = permanent_fee
            position.t_fee = t_fee
        account._next_order_number = data.get("_next_order_number", len(account.orders) + 1)
        account._next_fill_number = data.get("_next_fill_number", len(account.trades) + 1)
        return account
