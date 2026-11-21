from __future__ import annotations

from abc import ABC, abstractmethod
from bisect import bisect_left, bisect_right
from datetime import datetime, time as dtime
import math
from statistics import mean

from .broker import BrokerAccount
from .domain import Bar, Fill, OrderRequest, Position, StrategyConfig
from .trading_rules import rules_for


class _ChainedBars:
    """只读序列视图：把 ``base + extra`` 拼成一根「逻辑日线序列」，不复制 base。

    盘中（on_intraday / _tail_add_requests）需要把「截至当前的合成日线」追加到
    全量 history 尾部再喂给 _risk_requests / _add_signal。旧实现用
    ``list(history) + [today_bar]`` 每根 5 分钟 bar 都完整复制一份（~1500-2000 个
    Bar 引用），是 6 年回测的主要热点之一。这里用一个 O(1) 的视图替代复制：
    仅当被索引到时才取元素，且 _risk_requests / _add_signal 只读有界尾部
    （[-20:] / [-6:-1] / _atr 窗口）与 ``len``，故整段读取成本恒定。

    仅实现调用方用到的协议：``__len__`` / ``__getitem__``（int 与 slice）。
    bisect（_holding_days）只依赖这两者，可直接作用于本视图。
    """

    __slots__ = ("_base", "_extra", "_base_len", "_total")

    def __init__(self, base: list[Bar], extra: tuple[Bar, ...]) -> None:
        self._base = base
        self._extra = extra
        self._base_len = len(base)
        self._total = self._base_len + len(extra)

    def __len__(self) -> int:
        return self._total

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self._total))]
        if index < 0:
            index += self._total
        if index < self._base_len:
            return self._base[index]
        return self._extra[index - self._base_len]


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

# ---------------------------------------------------------------------------
# Armed「量价背离」量能口径：盘中把「当日至今累计量」折算成日量当量的当量单位。
# 一个 A 股交易日的盘中当量 = 9:30-11:30 + 13:00-15:00 = 240 分钟 = 48 根 5 分钟 K。
# 只有折算到日量当量，才能和日线口径的「前 5 日全天均量」同尺度比较（C2 修复）。
# ---------------------------------------------------------------------------
TRADING_MINUTES_PER_DAY = 240
INTRADAY_BARS_PER_DAY = 48

_RISK_VOLUME_CALIBERS = ("annualized", "debiased", "cumulative")

# 「debiased」口径用的实测累计分布：_VOLUME_CUM_FRACTION[k-1] = 第 k 根 5 分钟 K 收盘时，
# 当日已完成量占全天的比例（均值）。60 只 × 806 个交易日 = 48385 个完整交易日实测
# （2026-11-17，quan_quant 本地 minute5 parquet 只读统计）。
# 线性年化（annualized）等价于假设该比例 = k/48，实测早盘严重偏离：
#   09:35 实测 0.0923 vs k/48=0.0208；10:00 0.2925 vs 0.125；11:30 0.6075 vs 0.500；
#   14:00 0.7824 vs 0.750；15:00 1.0000 vs 1.000。
_VOLUME_CUM_FRACTION: tuple[float, ...] = (
    0.0923, 0.1456, 0.1896, 0.2282, 0.2623, 0.2925, 0.3228, 0.3482,
    0.3718, 0.3937, 0.4140, 0.4328, 0.4520, 0.4692, 0.4853, 0.5010,
    0.5159, 0.5298, 0.5444, 0.5576, 0.5704, 0.5832, 0.5956, 0.6075,
    0.6293, 0.6442, 0.6587, 0.6732, 0.6871, 0.7006, 0.7152, 0.7289,
    0.7424, 0.7559, 0.7693, 0.7824, 0.7975, 0.8115, 0.8255, 0.8394,
    0.8535, 0.8678, 0.8854, 0.9025, 0.9222, 0.9449, 0.9736, 1.0000,
)

# ------------------------------------------------------------------
# 策略状态单一来源：字段清单只写这一份，state_dict / load_state 由它生成。
# （属性名, 类型, 缺省值）。新增状态字段必须同步登记这张表——
# test_strategy_state.py 校验「实例属性 ⇆ 字段表」双向完备，漏加即测试失败。
# ------------------------------------------------------------------
STATE_VERSION = 1

STRATEGY_STATE_FIELDS: list[tuple[str, str, object]] = [
    ("last_add_price", "float", None),
    ("last_add_session", "int", None),
    ("had_new_low", "bool", False),
    ("armed", "bool", False),
    ("liquidating", "bool", False),
    ("close_reason", "str", ""),
    ("divergence_count", "int", 0),
    ("vwap_count", "int", 0),
    ("account_return_peak", "float", 0.0),
    ("round_t_start", "float", 0.0),
    ("t_order_submitted", "bool", False),
    ("t_exit_submitted", "bool", False),
    ("t_traded_today", "bool", False),
    ("recent_long_upper", "bool", False),
    ("event_halt", "bool", False),
    ("entry_submitted_date", "date", None),
    ("add_submitted_date", "date", None),
    ("round_entry_date", "date", None),
    ("last_intraday_timestamp", "ts", None),
    # 轮次闭合时的清仓信号快照（trigger/signal_price/signal_time/price_return）：
    # 模拟盘没有独立的收尾事件，轮次明细是读时从成交流水重建的，所以必须把「决策
    # 瞬间」的信号落进账户状态，下一次轮次开始时才清空（见 on_fill 首笔建仓分支）。
    ("last_close_signal", "json", None),
]


def _coerce(kind: str, raw: object, default: object) -> object:
    """把落库的原始值收敛成字段类型；显式 null / 非法值一律回落缺省（防御式边界）。

    与旧手写 load_state 的口径逐字段对齐；差异只有一处改进：旧实现遇到
    「显式 null 的 int 字段」会 int(None) 直接抛 TypeError，这里回落缺省。
    """
    if raw is None:
        return default
    try:
        if kind == "bool":
            return bool(raw)
        if kind == "int":
            return int(raw)
        if kind == "float":
            return float(raw)
        if kind == "str":
            return str(raw)
        if kind == "date":
            return str(raw) if raw else default
        if kind == "ts":
            return datetime.fromisoformat(str(raw)) if raw else default
        if kind == "json":
            # 嵌套 dict 状态（如 last_close_signal）：落库后是 JSON 对象；显式 null /
            # 非对象脏数据回落缺省，避免下游 .get() 炸在非 dict 上。
            return dict(raw) if isinstance(raw, dict) else default
    except (TypeError, ValueError):
        pass
    return default


def ensure_round_state(
    strategy: "LayeredTStrategy", account: "BrokerAccount", daily_history: list[Bar] | None,
) -> str:
    """载入策略状态后的一致性校验与自愈（64f692bb 卡死事故防线）。

    不变式：本轮序列号 sequence_index>0 时，加仓基准价 / 加仓会话 / 建仓日期
    必须非空——否则加仓永远不发、A3 超跌永不触发、超期降阈止盈失效。
    违反时从账户成交记录自愈：本轮的永久仓买入 = 最近 sequence_index 笔；
    last_add_session 取最后一笔成交日所在日线会话序号（与 on_fill 同口径）。
    无法自愈（找不到永久仓成交）时返回告警文案，由调用方落日志。

    返回："" 表示状态一致；非空表示已自愈或需人工关注的原因说明。
    """
    if account.sequence_index <= 0:
        return ""
    missing = [
        name for name in ("last_add_price", "last_add_session", "round_entry_date")
        if getattr(strategy, name) is None
    ]
    if not missing:
        return ""
    buys = [
        fill for fill in (account.trades or [])
        if fill.side == "BUY" and fill.purpose == "PERMANENT"
    ]
    if not buys:
        return f"round-state missing {missing} 且无永久仓成交记录，无法自愈，请人工核对"
    round_fills = buys[-int(account.sequence_index):]
    last = round_fills[-1]
    if strategy.last_add_price is None:
        strategy.last_add_price = last.price
    if strategy.last_add_session is None:
        fill_date = str(last.timestamp)[:10]
        strategy.last_add_session = max(
            0, len([bar for bar in (daily_history or []) if bar.date <= fill_date]) - 1
        )
    if strategy.round_entry_date is None:
        strategy.round_entry_date = str(round_fills[0].timestamp)[:10]
    return (
        f"round-state 自愈：{missing} <- price={strategy.last_add_price} "
        f"session={strategy.last_add_session} entry={strategy.round_entry_date}"
    )


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
        # 大盘门当日 memo：门只依赖「严格早于当日」的指数日线，全天不随 5 分钟 bar 变化。
        # 按 (当日日期, 指数序列长度) 记一笔，避免每根 5 分钟 bar 重扫 ~4000 根指数 bar。
        self._index_gate_memo_key: tuple[str, int] | None = None
        self._index_gate_memo_value: bool = False
        # 清仓信号诊断（见 _set_close_signal）：本轮首次判定清仓的 K 线快照。
        self._close_signal: dict | None = None
        # 已落库的清仓信号（跨 tick 保留）：回测在轮次闭合时直接读 _close_signal；
        # 模拟盘轮次是事后从成交流水重建的，只能把信号留在账户状态里，等 paper_rounds
        # 读走后再由下一轮首笔建仓清空（见 on_fill）。
        self.last_close_signal: dict | None = None

    def _installment_amount(self, account: BrokerAccount, index: int) -> float:
        """取第 ``index`` 笔的实际下注金额（应用 16 笔规模口径）。

        - ``fixed``（默认）：直接取配置的绝对金额，行为与历史完全一致。
        - ``equity``：按「当前权益 / 初始本金」等比缩放。
          分母只用当前在场权益（cash + 持仓市值），**不含已提现金额**：
          提现已经离场，不再承担波动、也不参与后续加仓的规模口径，
          若把它加回会虚高 scale、在权益并未真正增长时放大加仓敞口
          （如 equity 5.5M + 已提现 3.5M、initial 4M 时，加回提现得 scale 2.25，
          剔除后为 1.375，后者才反映真实的在场资金规模）；
          再套 ``installment_scale_floor`` 下限（默认 0.5），防止权益大幅回撤时
          把下单金额压到尘粒（``board_buy_quantity`` 仍保证 ≥ 板块最小申报）。

        四处读 ``installment_amounts``（首仓/加仓/尾盘首笔/做T）统一走这里，
        单一真相源（架构原则 1）；新增口径时只改这一处。
        """
        index = max(0, min(int(index), len(self.config.installment_amounts) - 1))
        base = float(self.config.installment_amounts[index])
        if self.config.installment_scale_mode != "equity":
            return base
        # 缩放基数只用当前在场权益（cash + 持仓市值），不含已提现金额：
        # 提现已离场，不再承担波动，也不应作为加仓规模的依据；加回它会虚高 scale。
        base_equity = float(account.equity)
        initial = float(getattr(account, "initial_cash", 0.0) or 0.0)
        if initial <= 0 or base_equity <= 0:
            return base
        scale = base_equity / initial
        return base * max(scale, float(self.config.installment_scale_floor))

    def state_dict(self) -> dict:
        dump: dict = {"state_version": STATE_VERSION}
        for name, kind, _default in STRATEGY_STATE_FIELDS:
            value = getattr(self, name)
            dump[name] = value.isoformat() if kind == "ts" and value is not None else value
        return dump

    def load_state(self, state: dict) -> None:
        # 字段迁移按 version 分支：当前 1 代内所有落库结构同构，直接收敛。
        # 未来改字段语义时在这里按 _coerce("int", state.get("state_version"), 1) 分支迁移。
        for name, kind, default in STRATEGY_STATE_FIELDS:
            setattr(self, name, _coerce(kind, state.get(name), default))

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
        self._close_signal = None
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

    def _set_close_signal(self, trigger: str, bar: Bar, account: BrokerAccount) -> None:
        """记录清仓信号诊断：本轮首次判定清仓那一根 K 线的收盘价与触发类型。

        信号价 = 决策 K 线的 close；成交价 = 下一根 bar 开盘（含滑点）。两者之差
        就是「信号 +15%、落袋 +8.8%」里执行落差的来源，报告据此做止盈执行诊断。
        只记录首次触发（``_close_signal`` 已存在则不覆盖），保证拿到的是最初信号价。
        ``_close_signal`` 只活在内存、轮次闭合时由回测读走；另写一份
        ``last_close_signal`` 进 ``STRATEGY_STATE_FIELDS``（并同步到
        ``account.strategy_state``），让模拟盘的读时轮次重建也能拿到「决策瞬间」的
        信号价——这是历史无法回补、只能从现在开始落库的字段。
        """
        if self._close_signal is not None:
            return
        price_return, _ = self._returns(account, bar.symbol, bar.close, self.round_t_start)
        timestamp = bar.timestamp
        signal = {
            "trigger": trigger,
            "signal_price": bar.close,
            "signal_time": timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
            "price_return": price_return,
        }
        self._close_signal = signal
        self.last_close_signal = dict(signal)
        # 立即写一份到账户状态（本 tick 结束还会由 state_dict() 整体覆盖，双写保证
        # 即使本轮 tick 后续提前返回也不会丢信号）。
        account.strategy_state["last_close_signal"] = dict(signal)

    def on_fill(self, fill: Fill, session_index: int) -> None:
        if fill.side == "BUY" and fill.purpose == "PERMANENT":
            if self.round_entry_date is None and self.last_close_signal is not None:
                # 新一轮首笔建仓：上一轮的清仓信号已完成使命，清空避免被 paper_rounds
                # 误挂到新轮（轮次是读时重建的，只能保留最近一次闭合的信号）。
                self.last_close_signal = None
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

        性能：原实现 ``sum(1 for item in history if item.date > entry)`` 是每根
        5 分钟 bar 一次 O(n) 全量扫描（~7 万 bar × ~1500 = ~2 亿次比较）。这里改用
        ``bisect_right``（按 Bar.date 升序）定位建仓日的插入点，晚于建仓日的 bar 数
        即 ``len(history) - 插入点``，O(log n)，结果与原 sum 逐字一致（含建仓日不在
        history / 早于 history 起点 / 晚于 history 终点的边界）。
        """
        if not self.round_entry_date:
            return 0
        entry = self.round_entry_date
        # bisect_right 按 date 升序找「最后一个 <= entry 的位置」：其右侧全部 > entry。
        index = bisect_right(history, entry, key=lambda item: item.date)
        return len(history) - index

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
            self._set_close_signal("事件熔断", bar, account)
            return self._liquidation_requests(position, mode)
        if price_return >= effective_tp:
            self.liquidating = True
            self.close_reason = (
                f"超期持仓{holding_days}日_止盈降至{effective_tp * 100:.0f}%"
                if tp_overridden else f"价格轨收益达到{effective_tp * 100:.0f}%"
            )
            self._set_close_signal("价格轨止盈", bar, account)
            return self._liquidation_requests(position, mode)
        if account_return >= effective_tp:
            self.liquidating = True
            self.close_reason = (
                f"超期持仓{holding_days}日_账户轨止盈降至{effective_tp * 100:.0f}%"
                if tp_overridden else f"账户轨收益达到{effective_tp * 100:.0f}%"
            )
            self._set_close_signal("账户轨止盈", bar, account)
            return self._liquidation_requests(position, mode)
        # 硬性最大持仓天数：持有 ≥N 个交易日后无条件强平（0 = 不限/关闭）。
        # 放在止盈检查之后（价/账户轨止盈优先，触发即走上面），但先于 Armed 背离
        # 辅助清仓——超期是硬兜底，不再等背离/回撤信号。
        if self.config.force_exit_holding_days and int(self.config.force_exit_holding_days) > 0 \
                and holding_days >= int(self.config.force_exit_holding_days):
            self.liquidating = True
            self.close_reason = f"最大持仓{int(self.config.force_exit_holding_days)}日强平"
            self._set_close_signal("超期强平", bar, account)
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
            # 0.8 → 配置项（架构原则 2：可调参数可视化可编辑）
            volume_average = mean(item.volume for item in previous_five) if previous_five else bar.volume
            volume_not_supporting = bar.volume < volume_average * self.config.risk_volume_ratio
            upper_shadow = False
            for item in recent_bars[-5:]:
                body = abs(item.close - item.open)
                upper = item.high - max(item.open, item.close)
                full_range = max(item.high - item.low, 1e-9)
                if upper >= 2 * max(body, 0.01) and upper / full_range >= 0.45:
                    upper_shadow = True
                    break
            # 上影线证据源去重，不再当日 latched。
            # 旧写法 `self.recent_long_upper or upper_shadow` 让「当日出现过一次长上影」后
            # 该项全天恒 True（白送一票），与「上影回撤」在同一根 bar 上重复计数 → 三选二
            # 退化成「上影线 OR 上影回撤」。按最近 5 根窗口重算后，三项都需要当前时点的新鲜证据。
            self.recent_long_upper = upper_shadow
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
            self._set_close_signal("量价背离", bar, account)
        elif self.vwap_count >= self.config.divergence_ticks:
            self.liquidating = True
            self.close_reason = "VWAP账户回撤4%_3根5分钟K线"
            self._set_close_signal("VWAP回撤", bar, account)
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
            return [self._buy_request(bar, self._installment_amount(account, 0), "换票日首笔")]
        if (
            position.permanent_qty <= 0 or self.last_add_price is None
            or self.last_add_session is None or account.sequence_index >= 16
        ):
            return []
        if self._add_gap_blocked(history, bar, account, session_index):
            return []
        # 同日尾盘已发加仓、或已有未结束的永久仓买入委托时，日终不再重复申报。
        if self.add_submitted_date == bar.date or self._has_pending_entry(account, bar.symbol):
            return []
        signal = self._add_signal(history, account)
        if signal:
            amount = self._installment_amount(account, account.sequence_index)
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
            # 按档位「先松后紧」：sequence_index // 4 → 档1(0-3)/档2(4-7)/档3(8-11)/档4(12-15)。
            # 逐档叠加条件：档1 最松（仅浮亏+缩量<5日均量），档2 +收盘站回前3日低点，
            # 档3 +此前破过新低，档4 +缩量收紧到 oversold_volume_ratio（= 原始 A3，最严）。
            # add_tier_strict=False 时全档统一用原始 A3（向后兼容）。
            if self.config.add_tier_strict:
                tier = min(account.sequence_index // 4, 3)  # 0..3
            else:
                tier = 3
            a3 = (
                bar.volume < previous_five_volume
                and (tier == 0 or bar.close > previous_three_low)
                and (tier <= 1 or had_low_before)
                and (
                    tier <= 2
                    or bar.volume < previous_five_volume * self.config.oversold_volume_ratio
                )
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
    def _last_add_date(account: BrokerAccount) -> str | None:
        """最近一笔永久仓买入的成交日（YYYY-MM-DD）；无成交记录返回 None。

        以成交记录为单一真相源（手动买入同样刷新间隔时钟），数据集刷新导致的
        行数变化不会影响本值——2026-11-16 联特科技「间隔门槛误拦」事故的根因修复。
        """
        buys = [
            fill for fill in (account.trades or [])
            if fill.side == "BUY" and fill.purpose == "PERMANENT"
        ]
        return str(buys[-1].timestamp)[:10] if buys else None

    def _add_gap_blocked(
        self, history: list[Bar], bar: Bar, account: BrokerAccount,
        session_index: int | None,
    ) -> bool:
        """加仓间隔是否未到：距最近一笔永久仓成交的交易日数 < min_add_gap_days。

        交易日数 = history ∪ {bar} 中 (成交日, bar.date] 的交易日数，按成交日期
        动态换算，天然免疫数据集刷新导致的行数漂移；无成交记录的老账户回退
        旧行序号口径（last_add_session）。

        性能：history 升序且每日一根（date 去重），(last_date, bar.date] 区间内的
        交易日数 = ``bisect_right(bar.date) - bisect_right(last_date)``；再补上当且仅当
        bar.date 未落在 history 里时的那 1 天（盘中路径 history 不含当日日线）。
        与旧的全量 set 推导逐字一致，但 O(log n) 而非 O(n)。
        """
        last_date = self._last_add_date(account)
        if last_date is not None:
            history = history or []
            # 升序日线中 (last_date, bar.date] 的根数 = 右端插入点 - 左端插入点。
            count = (
                bisect_right(history, bar.date, key=lambda item: item.date)
                - bisect_right(history, last_date, key=lambda item: item.date)
            )
            # bar.date 本身是否已在 history 里：在（日线 on_daily 路径，history 含当日）
            # 则已被上面区间计入；不在（盘中路径，history=history[:-1] 不含当日）则补 1 天。
            pos = bisect_left(history, bar.date, key=lambda item: item.date)
            bar_in_history = pos < len(history) and history[pos].date == bar.date
            if bar.date > last_date and not bar_in_history:
                count += 1
            return count < self.config.min_add_gap_days
        if session_index is None or self.last_add_session is None:
            return False
        return session_index - self.last_add_session < self.config.min_add_gap_days

    def _index_gate_open(self, index_history: list[Bar] | None, bar: Bar) -> bool:
        """盘中大盘门：只用**严格早于当日**的指数日线，避免盘中读到当日未收盘的指数价。

        与 ``on_daily`` 同一口径：21 根以上按 20 日收益 < 0 开门；数据存在但不足
        21 根保守关门；完全没有指数数据时降级开门（与日线降级路径一致）。

        性能：门只随「日」变化（prior = 严格早于当日的指数日线，全天不变），旧实现
        每根 5 分钟 bar 都重扫整个 index_history（~4000 根）。这里按
        ``(当日日期, 指数序列长度)`` memo 当日结果，全天只算一次；长度入 key 是为了
        在指数序列当日被替换/补齐时自动失效，结果仍与逐 bar 重算逐字一致。
        """
        index_history = index_history or []
        memo_key = (bar.date, len(index_history))
        if self._index_gate_memo_key == memo_key:
            return self._index_gate_memo_value
        prior = [item for item in index_history if item.date < bar.date]
        if len(prior) >= 21:
            base = prior[-21].close
            if base <= 0:
                result = True
            else:
                result = prior[-1].close / base - 1 < 0
        else:
            result = not prior
        self._index_gate_memo_key = memo_key
        self._index_gate_memo_value = result
        return result

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
        amount = self._installment_amount(account, 0)
        # board_buy_quantity 已保证 ≥ 板块最小申报（方案 2：金额不足最小申报也按最小申报建仓）
        quantity = board_buy_quantity(bar.symbol, amount, bar.close)
        self.entry_submitted_date = bar.date
        return [OrderRequest(
            symbol=bar.symbol, side="BUY", quantity=quantity,
            execution_mode="NEXT_BAR", reason="尾盘首笔",
        )]

    @staticmethod
    def _intraday_bars_elapsed(bar: Bar, recent_bars: list[Bar]) -> int:
        """当日已知已收盘 5 分钟 K 的根数（含决策 bar 自身）。

        只数 recent_bars 里当日的已收盘 bar，**不读墙钟**：回测（backtest.py:941
        在决策前 append(m5)）与 live-tick（main.py:1170 只保留 ``timestamp <= now``
        的当日 bar）两条路径由此得到同一个数，满足架构原则 1（单一真相源）。
        """
        today = [item for item in (recent_bars or []) if item.date == bar.date]
        if not today:
            return 0
        return len(today) + (0 if today[-1].timestamp == bar.timestamp else 1)

    @staticmethod
    def _daily_volume_equivalent(
        cum_volume: float, elapsed_bars: int, caliber: str,
    ) -> float:
        """把「当日至今累计量」折算成日量当量。

        - ``annualized``：线性年化 ``cum × 48/已过根数``。早盘只会**高估**今日量
          （09:35 +343%、10:00 +134%、11:30 +21.5%、14:00 +4.3%），因此缩量项
          偏保守、**绝不会误判**；代价是早盘缩量项基本失效（见方案报告 §七）。
        - ``debiased``  ：用 _VOLUME_CUM_FRACTION 去偏，全天近似无偏。
        - ``cumulative``：不折算（旧口径，系统性偏松，C3 已记录），仅留作回滚。
        已过根数 < 2 时调用方直接判「无法判定」（缩量项 False），不走这里。
        """
        if caliber == "cumulative":
            return cum_volume
        if caliber == "debiased":
            index = min(max(elapsed_bars, 1), INTRADAY_BARS_PER_DAY) - 1
            fraction = _VOLUME_CUM_FRACTION[index]
            return cum_volume / max(fraction, 1e-9)
        return cum_volume * INTRADAY_BARS_PER_DAY / max(2, elapsed_bars)

    @staticmethod
    def _today_daily_bar(
        history: list[Bar], bar: Bar, recent_bars: list[Bar] | None = None,
        volume_caliber: str = "cumulative",
    ) -> Bar:
        """用当日已有的 5 分钟 K 合成一根「截至当前」的日线，供盘中加仓判定。

        尾盘时段当日成交量已基本走完，用累计量近似日量，避免拿单根 5 分钟量
        与日均量比较（会把 A3 的缩量条件放大成假信号）。

        ``volume_caliber``：volume 的折算口径。默认 ``cumulative`` 保持既有行为
        （尾盘 A3 缩量仍在用这个近似，C3 已记录为已知偏松，本次不动）；
        Armed 背离路径传 ``config.risk_volume_caliber``。
        """
        today = [item for item in (recent_bars or []) if item.date == bar.date] or [bar]
        volume = sum(item.volume for item in today)
        if volume_caliber != "cumulative":
            elapsed = LayeredTStrategy._intraday_bars_elapsed(bar, recent_bars or [bar])
            if elapsed >= 2:
                volume = LayeredTStrategy._daily_volume_equivalent(volume, elapsed, volume_caliber)
        prev_close = history[-1].close if history else bar.prev_close
        return Bar(
            timestamp=bar.timestamp, symbol=bar.symbol,
            open=today[0].open,
            high=max(item.high for item in today),
            low=min(item.low for item in today),
            close=bar.close,
            volume=volume,
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
        if self._add_gap_blocked(history, bar, account, session_index):
            return []
        if self.add_submitted_date == bar.date or self._has_pending_entry(account, bar.symbol):
            return []
        clock = bar.timestamp.time()
        if not (TAIL_ENTRY_START <= clock < TAIL_ENTRY_END):
            return []
        # 同 on_intraday：用 O(1) 视图拼接 history+当日合成日线，避免每根 5 分钟 bar
        # 复制整条 history。_add_signal 仅读 history[-1]/[-10:]/[-4:-1]/[-6:-1]。
        full_history = _ChainedBars(
            history, (self._today_daily_bar(history, bar, recent_bars),),
        )
        signal = self._add_signal(full_history, account)
        if not signal:
            return []
        # 下注额统一走 _installment_amount（单一真相源）：与 on_daily 日线加仓、
        # _tail_entry_requests 首仓、做T 同一口径，应用 16 笔规模口径
        # （fixed=裸档位；equity=按在场权益等比缩放）。此前这里直接读
        # installment_amounts[seq]，equity 模式下尾盘加仓会系统性偏小（漏乘
        # 权益缩放系数），与其它三条路径口径分裂——违反架构原则 1。
        amount = self._installment_amount(account, account.sequence_index)
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
        return self._installment_amount(account, index)

    def _t_roundtrip_cost(self) -> float:
        """一次完整做T往返的成本率估算（占投入金额的比例，小数）。

        四项双边费率各计两次、印花税仅卖出计一次、滑点买卖各一次。用于：
        ①做T准入的净价差门槛（_t_eligible）；②净利止盈的目标价换算
        （毛目标价差 = 净目标 + 本成本率）。百分比近似，忽略 min_commission
        固定佣金（做T金额 10 万~40 万时占比 <0.005%）。
        """
        # 滑点项按模型分支：fixed 用 2*slippage_bps（旧行为不变）；impact 用代表性
        # 参与率 volume_participation 做单边滑点估算 one_way = k*sqrt(volume_participation)。
        if self.config.slippage_model == "impact":
            slippage = 2 * self.config.impact_coefficient * math.sqrt(self.config.volume_participation)
        else:
            slippage = 2 * self.config.slippage_bps / 10000
        return (
            2 * (
                self.config.commission_rate + self.config.handling_fee_rate
                + self.config.regulatory_fee_rate + self.config.transfer_fee_rate
            )
            + self.config.stamp_tax_rate + slippage
        )

    def _t_eligible(self, history: list[Bar], bar: Bar, account: BrokerAccount,
                    live_turnover: float | None = None,
                    live_bid: float | None = None, live_ask: float | None = None,
                    daily_amplitude: float | None = None,
                    daily_turnover_rate: float | None = None) -> bool:
        # 与永久仓加仓同日互斥（永久仓优先、做T让位）：当日已发永久仓加仓
        # （add_submitted_date == bar.date）则不做T。反向不锁（已做T 不阻止后续加仓）。
        # 说明阻挡机制：做T新开窗口 09:35–14:00，尾盘加仓窗口 14:30–14:50，两者不重叠。
        # 在当前窗口布局下，"当日已加仓→同日再做T" 主要由 on_intraday 的结构挡住——
        # ①加仓 bar 上 _tail_add_requests 命中即 return，早于做T块；②加仓只在 14:30+ 发生，
        # 此时做T窗口已关（on_intraday 对 ≥14:00 直接 return）。本闸是把"永久仓优先"写成
        # 显式不变式的 defense-in-depth：一旦当日发过加仓，_t_eligible 恒 False，即使将来
        # 做T/加仓窗口调整到重叠、或有同日更晚的做T决策，也不会再发做T单。
        if self.add_submitted_date == bar.date:
            return False
        position = account.position(bar.symbol)
        # 深跌幅当日做T旁路：当日盘中跌幅（相对前收盘）≥ t_deep_drop_pct 时，
        # 绕过底仓笔数门槛（sequence_index ≥ t_min_base_entries）。20cm 板日内 -10% 是
        # 极端恐慌事件，抢反弹做T，退出仍走净利止盈 + T+1 尾盘强平，风险有界。
        deep_drop = False
        if self.config.t_deep_drop_pct is not None and bar.prev_close and bar.prev_close > 0:
            intraday_drop = bar.close / bar.prev_close - 1
            deep_drop = intraday_drop <= -float(self.config.t_deep_drop_pct)
        if (
            not self.config.enable_t
            or (account.sequence_index < self.config.t_min_base_entries and not deep_drop)
            or self.armed
            or self.liquidating or self.t_traded_today or self.t_order_submitted
            or position.t_qty or len(history) < 5
        ):
            return False
        recent = history[-5:]
        # 振幅/换手口径：调用方经 daily_amplitude/daily_turnover_rate 显式传入（参数名沿用
        # 旧名，语义随调用路径不同）：
        #   • on_daily 日线路径：传当日「全天」振幅/换手——收盘后决策，全天值合法；
        #   • on_intraday 盘中路径（回测）：传「当日至今累计」振幅/换手——逐根已收盘 5 分钟
        #     bar 累积，决策时点完全可得，无未来数据（回测曾误传全天值 → look-ahead bias）；
        #   • 模拟盘 live-tick：不传这两个（振幅走 recent 回退、换手走 live_turnover 快照）。
        # 未传时振幅回退到 recent 局部均值（避免单根 5 分钟局部振幅 ~0.3% 直接卡门槛）。
        if daily_amplitude is not None:
            average_amplitude = daily_amplitude
        else:
            average_amplitude = mean(
                (item.high - item.low) / (item.prev_close or item.open) for item in recent
            )
        # 换手率取值优先级：① 调用方显式传入的 daily_turnover_rate（回测盘中=当日至今累计，
        # 日线=全天；模拟盘不传）；② recent[-1].turnover_rate 回退（历史 bar 无换手字段时为
        # None）；③ live_turnover 实时快照兜底（腾讯快照字段[38]，做T是当日盘中决策）。
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
        # 【C2 口径修复】Armed 背离/VWAP 判定不再把「当前 5 分钟 bar」与「日线序列」混喂。
        # 先把当日已收盘数据合成一根日线（量按 caliber 折算成日量当量、high/low 用
        # running 极值、close 用当前价），再和 history 拼成**纯日线**序列：
        # 「前 5 日均量 / 日线 ATR / 20 日新高 / 上影回撤」四项全部落到日线口径，与
        # on_daily 逐字同构。_risk_requests 本体与 on_daily 均不改动，日线路径行为零变化。
        # VWAP 仍用 recent（5 分钟）与 bar.close（当前价），口径不变。
        today_bar = self._today_daily_bar(
            history, bar, recent_bars, volume_caliber=self.config.risk_volume_caliber,
        )
        # 只读有界尾部的合成日线序列：用 O(1) 视图拼接 history+today_bar，不再每根
        # 5 分钟 bar 复制整条 history（~1500-2000 引用）。_risk_requests 仅读
        # history[-20:]/[-6:-1]/_atr 窗口与 len，视图语义与完整 list 逐字一致。
        risk_history = _ChainedBars(history, (today_bar,))
        risk = self._risk_requests(risk_history, today_bar, account, recent_bars, "NEXT_BAR")
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
            execution_mode="NEXT_BAR",
            reason=f"底仓{self.config.t_min_base_entries}笔起正T_准入通过",
        )]
