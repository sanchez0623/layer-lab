from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal


Side = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT"]
Purpose = Literal["PERMANENT", "T"]


def money(value: float) -> float:
    return round(float(value) + 1e-10, 2)


def price(value: float) -> float:
    return round(float(value) + 1e-10, 4)


def utc_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


FEE_RATE_FIELDS = (
    "commission_rate", "min_commission", "stamp_tax_rate",
    "handling_fee_rate", "regulatory_fee_rate", "transfer_fee_rate",
)


@dataclass(slots=True)
class FeeConfig:
    """界面/API 层的费用配置，费率单位统一为「万分之」。

    结算时按分项分别计算并相加；最低佣金只兜底佣金项，不影响其他四项。
    """

    commission_rate: float = 0.5        # 万分之，双边
    min_commission: float = 5.0         # 元/笔，仅作用于佣金项
    stamp_tax_rate: float = 5.0         # 万分之，仅卖出
    handling_fee_rate: float = 0.341    # 万分之，经手费，双边
    regulatory_fee_rate: float = 0.2    # 万分之，证管费，双边
    transfer_fee_rate: float = 0.1      # 万分之，过户费，双边

    def __post_init__(self) -> None:
        for name in FEE_RATE_FIELDS:
            value = float(getattr(self, name))
            if not 0 <= value <= (10_000 if name == "min_commission" else 100):
                raise ValueError(f"费用配置 {name} 必须在合理区间内且非负")
            setattr(self, name, value)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "FeeConfig":
        if not value:
            return cls()
        unknown = set(value) - set(FEE_RATE_FIELDS)
        if unknown:
            raise ValueError(f"未知费用配置: {', '.join(sorted(unknown))}")
        return cls(**value)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_strategy_patch(self) -> dict[str, float]:
        """万分之 → 结算用的小数比例。"""
        return {
            "commission_rate": self.commission_rate / 10_000,
            "min_commission": self.min_commission,
            "stamp_tax_rate": self.stamp_tax_rate / 10_000,
            "handling_fee_rate": self.handling_fee_rate / 10_000,
            "regulatory_fee_rate": self.regulatory_fee_rate / 10_000,
            "transfer_fee_rate": self.transfer_fee_rate / 10_000,
        }

    @classmethod
    def from_strategy(cls, config: "StrategyConfig") -> "FeeConfig":
        return cls(
            commission_rate=round(config.commission_rate * 10_000, 6),
            min_commission=config.min_commission,
            stamp_tax_rate=round(config.stamp_tax_rate * 10_000, 6),
            handling_fee_rate=round(config.handling_fee_rate * 10_000, 6),
            regulatory_fee_rate=round(config.regulatory_fee_rate * 10_000, 6),
            transfer_fee_rate=round(config.transfer_fee_rate * 10_000, 6),
        )


@dataclass(slots=True)
class FeeBreakdown:
    commission: float
    stamp_tax: float
    handling_fee: float
    regulatory_fee: float
    transfer_fee: float
    total_fee: float


@dataclass(slots=True)
class StrategyConfig:
    installment_amounts: list[float] = field(
        default_factory=lambda: [100000.0] * 4 + [200000.0] * 4
        + [300000.0] * 4 + [400000.0] * 4
    )
    enable_t: bool = True
    t_order_amount: float = 100000.0
    commission_rate: float = 0.00005       # 万0.5，双边
    min_commission: float = 5.0             # 元/笔，仅作用于佣金项
    stamp_tax_rate: float = 0.0005          # 万5，仅卖出
    handling_fee_rate: float = 0.0000341    # 万0.341，经手费，双边
    regulatory_fee_rate: float = 0.00002    # 万0.2，证管费，双边
    transfer_fee_rate: float = 0.00001      # 万0.1，过户费，双边
    slippage_bps: float = 1.0
    volume_participation: float = 0.20
    # 仅用于合成标的（TEST/SYNTH001 等无板块代码）的涨跌幅回退口径；
    # 真实板块（科创板/创业板/沪深主板/B股）的涨跌幅一律取 trading_rules 的板块值，忽略本字段。
    limit_pct: float = 0.20
    right_drawdown: float = 0.05
    min_add_gap_days: int = 1
    oversold_volume_ratio: float = 0.50
    price_armed: float = 0.10
    price_disarm: float = 0.095
    price_take_profit: float = 0.15
    account_take_profit: float = 0.15
    atr_period: int = 14
    divergence_ticks: int = 3
    vwap_drawdown: float = 0.04
    t_min_amplitude: float = 0.04
    t_min_turnover: float = 0.03
    t_max_turnover: float = 0.12
    t_min_expected_net_spread: float = 0.03
    t_target_spread: float = 0.015
    deep_loss_threshold: float = -0.10

    def __post_init__(self) -> None:
        if len(self.installment_amounts) != 16:
            raise ValueError("installment_amounts 必须恰好包含16笔")
        if any(float(x) <= 0 for x in self.installment_amounts):
            raise ValueError("每笔金额必须为正数")
        if not 0 < self.volume_participation <= 1:
            raise ValueError("volume_participation 必须在(0,1]内")
        if self.divergence_ticks < 1:
            raise ValueError("divergence_ticks 必须至少为1")
        for name in ("commission_rate", "stamp_tax_rate", "handling_fee_rate", "regulatory_fee_rate", "transfer_fee_rate"):
            if not 0 <= getattr(self, name) < 0.1:
                raise ValueError(f"{name} 必须是非负且小于 10% 的小数比例")
        if self.min_commission < 0 or self.slippage_bps < 0:
            raise ValueError("min_commission 与 slippage_bps 不能为负")
        self.installment_amounts = [float(x) for x in self.installment_amounts]

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "StrategyConfig":
        if not value:
            return cls()
        names = cls.__dataclass_fields__.keys()
        unknown = set(value) - set(names)
        if unknown:
            raise ValueError(f"未知策略配置: {', '.join(sorted(unknown))}")
        return cls(**value)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Bar:
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    timeframe: str = "daily"
    turnover_rate: float | None = None
    prev_close: float | None = None
    bid: float | None = None
    ask: float | None = None

    @property
    def date(self) -> str:
        return self.timestamp.date().isoformat()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["timestamp"] = self.timestamp.isoformat()
        result["date"] = self.date
        return result


@dataclass(slots=True)
class OrderRequest:
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType = "MARKET"
    limit_price: float | None = None
    purpose: Purpose = "PERMANENT"
    execution_mode: str = "NEXT_OPEN"
    reason: str = ""


@dataclass(slots=True)
class Order:
    id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: int
    remaining: int
    limit_price: float | None
    purpose: Purpose
    execution_mode: str
    reason: str
    created_at: str
    status: str = "PENDING"
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    commission: float = 0.0
    stamp_tax: float = 0.0
    handling_fee: float = 0.0
    regulatory_fee: float = 0.0
    transfer_fee: float = 0.0
    total_fee: float = 0.0
    sequence_counted: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Fill:
    id: str
    order_id: str
    symbol: str
    side: Side
    quantity: int
    price: float
    amount: float
    commission: float
    stamp_tax: float
    transfer_fee: float
    total_fee: float
    slippage: float
    timestamp: str
    purpose: Purpose
    cash_flow: float
    realized_pnl: float = 0.0
    # 新增分项放在末尾并带默认值，兼容旧账户/旧报告里的成交记录反序列化。
    handling_fee: float = 0.0
    regulatory_fee: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Position:
    symbol: str
    permanent_qty: int = 0
    permanent_cost: float = 0.0
    t_qty: int = 0
    t_cost: float = 0.0
    today_permanent_buy_qty: int = 0
    today_t_buy_qty: int = 0
    last_price: float = 0.0
    # 当前持仓成本中仍包含的买入手续费（按成本分配口径随卖出递减）。
    # 新增字段放末尾并带默认值，兼容旧账户的 _positions_internal 反序列化。
    permanent_fee: float = 0.0
    t_fee: float = 0.0

    @property
    def total_qty(self) -> int:
        return self.permanent_qty + self.t_qty

    @property
    def today_buy_qty(self) -> int:
        return self.today_permanent_buy_qty + self.today_t_buy_qty

    @property
    def sellable_qty(self) -> int:
        return max(0, self.total_qty - self.today_buy_qty)

    @property
    def permanent_sellable_qty(self) -> int:
        return max(0, self.permanent_qty - self.today_permanent_buy_qty)

    @property
    def average_cost(self) -> float:
        if not self.total_qty:
            return 0.0
        return money((self.permanent_cost + self.t_cost) / self.total_qty)

    @property
    def buy_fee_in_cost(self) -> float:
        """当前持仓成本中包含的买入手续费（卖出费用不在此列）。"""
        return money(self.permanent_fee + self.t_fee)

    def public_dict(self) -> dict[str, Any]:
        # 平均成本已按「分」四舍五入，用它反算市值/浮盈会产生系统性误差
        # （如 400 股 × 0.0036 元 = 1.44 元），因此单独给出未除股数的精确成本。
        total_cost = money(self.permanent_cost + self.t_cost)
        market_value = money(self.last_price * self.total_qty)
        return {
            "symbol": self.symbol,
            "total_qty": self.total_qty,
            "sellable_qty": self.sellable_qty,
            "permanent_qty": self.permanent_qty,
            "permanent_cost": money(self.permanent_cost),
            "total_cost": total_cost,
            "average_cost": self.average_cost,
            "buy_fee": self.buy_fee_in_cost,
            "t_active_qty": self.t_qty,
            "last_price": price(self.last_price),
            "market_value": market_value,
            "float_pnl": money(market_value - total_cost),
        }


@dataclass(slots=True)
class Dataset:
    id: str
    name: str
    symbol: str
    timeframe: str
    start: str
    end: str
    rows: int
    source: str
    is_synthetic: bool
    created_at: str
    file_path: str = ""

    def to_dict(self, include_path: bool = False) -> dict[str, Any]:
        result = asdict(self)
        if not include_path:
            result.pop("file_path", None)
        return result


STRATEGY_RULES = [
    "初始资金400万元；16笔资金依次为10万×4、20万×4、30万×4、40万×4。",
    "永久仓只进不出，直至双轨整体止盈或事件熔断；T仓独立且不占加仓序列。",
    "第5笔起允许正T；每日最多一次，Armed暂停，14:50后用旧仓轧平。",
    "浮盈右侧加仓：近期小高点回撤≥5%、不破前三天低点、间隔≥1交易日。",
    "浮亏超跌加仓：曾创新低后出现A3，T日信号，T+1开盘执行，每日最多一笔。",
    "换票日检查指数20日收益；20日跌幅<0时不建立新永久仓。",
    "首仓：有5分钟数据时在尾盘14:30–14:50发NEXT_BAR市价单（下一根5分钟K撮合，当日仅一笔）；无盘中数据或非尾盘时按收盘信号次日开盘执行。",
    "永久仓价格轨收益≥15%立即清；10%-15%进入Armed，<9.5%解除。",
    "账户轨：(永久仓浮盈+本轮T净价差)≥永久投入成本15%立即清。",
    "Armed内20日新高且量价背离三选二，连续3根5分钟K线确认后清仓。",
    "VWAP下方且窗口账户收益回撤4%，连续3根5分钟K线确认后兜底清仓。",
    "止损默认不启用；事件熔断接口保留。",
    "日线无intraday时仅用上一收盘及更早信号，次日开盘执行并明确降级。",
    "行情接入：quan_quant历史日线（前复权）+腾讯实时快照/5分钟K线；模拟撮合只读行情，不连接券商。",
]
