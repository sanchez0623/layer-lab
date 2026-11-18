from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal


Side = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT"]
# PERMANENT=永久仓（轮动个股），T=做T仓，BETA=指数 beta 腿（ETF，独立于个股轮动）
Purpose = Literal["PERMANENT", "T", "BETA"]


def daily_close_timestamp(value: str | datetime) -> datetime:
    """日线 bar 的统一时间戳口径：交易日收盘 15:00:00。

    所有日线数据源（quan parquet / baostock / CSV 上传）都必须走这里，
    保证成交/委托时间不是 0 点，也与 ETF 日线数据源（etf_data 15:00）一致。
    日期部分不变，只把时刻定格到 15:00；date 属性与排序不受影响。
    """
    if isinstance(value, datetime):
        return value.replace(hour=15, minute=0, second=0, microsecond=0)
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").replace(
        hour=15, minute=0, second=0, microsecond=0
    )


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
    # 16 笔金额的规模口径：
    #   fixed（默认，向后兼容）——绝对金额，不随账户权益变化。
    #     问题：账户盈利/提现后相对敞口被稀释（4M 阶梯在权益 6.5M 时只覆盖 ~62%）。
    #   equity——按「(当前权益 + 已提现)/初始本金」等比缩放（回撤时同步缩小，
    #     盈利时放大，避免相对仓位被账户增长自动稀释）。回测复盘显示该口径
    #     能同时提升平均资金敞口与 TWR 年化（代价：maxDD 同步放大，见研究报告）。
    # 校验：__post_init__ 只允许 fixed/equity；scale_floor 为放大倍数下限
    #   （默认 0.5 = 权益跌到初始本金一半以下也至少按半仓规模下注，防止尘粒单）。
    installment_scale_mode: str = "fixed"
    installment_scale_floor: float = 0.5
    enable_t: bool = True
    # 已废弃：做T金额现跟随当前加仓档 installment_amounts[sequence_index]
    # （满仓后取最大档），不再使用本字段。保留字段仅为兼容老账户已快照的 config，
    # 避免 from_dict 因「未知字段」报错；新逻辑请勿再读取。
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
    # 做T准入四阈值（可视化可调，见模拟盘「做 T 准入门槛」面板与回测页）：
    # 近 5 根平均振幅下限（0.04 = 4%）、换手率区间 [0.03, 0.12]、
    # 期望净利下限（振幅扣掉一次往返成本后仍需 ≥ 0.03）。
    t_min_amplitude: float = 0.04
    t_min_turnover: float = 0.03
    t_max_turnover: float = 0.12
    t_min_expected_net_spread: float = 0.03
    # 已废弃：做T平仓改用 t_target_net_spread（扣成本后净利止盈）。本字段不再驱动平仓判定，
    # 仅为兼容老账户已快照的 config 保留，避免 from_dict 因「未知字段」报错。
    t_target_spread: float = 0.015
    # 做T净利止盈阈值：扣掉双边佣金/经手费/证管费/过户费/印花税/滑点后净赚该比例即平T（0~1 小数）。
    # 目标价 = T仓均价 ×(1 + t_target_net_spread + 往返成本估算)，成本估算见 strategy._t_roundtrip_cost。
    t_target_net_spread: float = 0.03
    # 做T资金硬截止：T+1 日到达该时刻（HH:MM，24小时制）仍未止盈则无条件尾盘强平。
    # T0 当日不强平（可持有到 T1），保证做T资金最迟在 T+1 收盘前了结。
    t_exit_cutoff: str = "14:50"
    deep_loss_threshold: float = -0.10
    # 超期持仓降阈止盈：持有超过 max_holding_days 个交易日后，价格轨/账户轨止盈阈值
    # 降为 holding_tp_override（0~1 小数）。None 表示关闭该逻辑（维持 price_take_profit）。
    # 目的：避免资金被长期套牢在「既不到 15% 止盈、也不触发 Armed 背离」的缓慢阴跌标的里。
    max_holding_days: int | None = None
    holding_tp_override: float | None = None
    # 硬性最大持仓天数：持有 ≥N 个交易日后无条件强平（0 = 不限/关闭）。
    # 独立于 max_holding_days（超期降阈止盈）——那个只是降阈，这个是到期强制清仓兜底。
    force_exit_holding_days: int = 0
    # 加仓按档位「先松后紧」：16 笔序列分 4 档（每档 4 笔，档1=10万…档4=40万）。
    # 开启后 A3 加仓信号按档位分级——档1 最松（仅浮亏+缩量<5日均量）、
    # 档2 +收盘站回前3日低点、档3 +此前破过新低、档4 +缩量收紧到 oversold_volume_ratio
    # （= 原始 A3 条件，最严）。目的：早期仓位易加让资金打满，后段高门槛封顶暴露，
    # 避免无差别摊进输家。False = 全档统一用原始 A3 条件（向后兼容）。
    add_tier_strict: bool = True
    # 做T底仓笔数门槛：累计底仓（sequence_index）达到该笔数才允许做T准入（1~16 笔）。
    # 之前硬编码为 4（「第5笔起正T」），现改为可配置。深跌幅当日做T仍可绕过（见 t_deep_drop_pct）。
    t_min_base_entries: int = 4
    # 深跌幅当日做T：当日盘中跌幅（相对前收盘）≥ 此阈值时，绕过底仓笔数门槛
    # （sequence_index≥4）允许做T。创业板/科创板 20cm 涨跌幅，-10% 已是极端恐慌事件
    # 才值得抢反弹。None = 关闭；__post_init__ 校验：非 None 时必须 >= 0.10。
    t_deep_drop_pct: float | None = 0.10
    # ---- 指数 beta 腿（ETF）------------------------------------------------
    # 个股轮动空仓期间，用创业板 ETF 承接 beta，避免「大门关闭 → 整段上涨 0 收益」。
    # 与个股互斥：选到股票时先卖 ETF 再建个股仓；ETF 期间不占用轮动序列。
    # 默认开启：回测页 / 新建模拟账户均直接带 beta 腿（仍可显式关闭）。
    # 老账户在创建时已快照 config，不受本默认值变化影响（架构原则 3）。
    etf_beta_enabled: bool = True
    etf_symbol: str = "sz.159915"           # 创业板 ETF（易方达）
    # 仓位占「当前账户权益」的比例（0~1）。0.2 = 20%。
    etf_position_pct: float = 0.20
    # 入场趋势过滤：指数收盘 > N 日均线才买 ETF。
    etf_entry_ma_days: int = 60
    # 出场：指数收盘 < N 日均线清仓。
    etf_exit_ma_days: int = 20
    # 硬止损：ETF 收盘较建仓成本回撤达到该比例即清仓（0~1）。
    etf_hard_stop_pct: float = 0.06
    # 入场「不追高」保护：指数收盘相对入场均线的偏离上限（0~1 小数，0.08 = 8%）。
    # 超过则本次不建仓，等回踩到均线附近再确认；0 表示关闭（旧行为）。
    # 依据：回测复盘里偏离 MA60 >8% 的追高入场几乎全是亏损单。
    etf_entry_max_deviation: float = 0.0
    # 再入场冷却：ETF 清仓后至少 N 个交易日内不再建仓（0 表示关闭）。
    # 目的：抑制指数在均线附近来回摆动导致的连续「买-卖-买」磨损。
    etf_reentry_cooldown_days: int = 0
    # beta 腿是否与个股「并存」：
    #   False（默认，互斥）—— 个股有持仓或刚选出个股时不建 beta 腿；ETF 持仓期间一旦选到
    #     个股就清仓让位。适合「ETF 只是空仓期的替补」的口径。
    #   True（并存）—— beta 腿只由指数均线/硬止损决定进出，个股在不在都不影响；建仓额度
    #     另受可用现金封顶（equity×仓位 与 available_cash 取小），自然与个股分摊同一笔现金。
    #     代价：同一时点可能同时持有个股 + ETF，风格上更偏 beta 暴露。
    # 回测与模拟盘共用本开关（架构原则 1），随账户/回测快照生效（架构原则 3）。
    etf_beta_coexist: bool = False
    # ---- 盈利提现 ----------------------------------------------------------
    # 把已落袋盈利从账户现金中取出（模拟真实提现，减少可买资金与权益）。
    # 模拟盘与回测共用同一套逻辑（BrokerAccount 统一执行），配置随账户快照。
    # 模式：none=关闭 / monthly=每月提现 / per_trade=每笔提现。
    withdraw_mode: str = "none"
    # 提取方式：ratio=按当期盈利比例提取 / fixed=按固定金额提取。
    withdraw_method: str = "ratio"
    # 按盈利比例提取时的比例（0~1 小数，0.20 = 提取当期盈利的 20%）。
    withdraw_ratio: float = 0.20
    # 按固定金额提取时的金额（元，>=0；当期盈利不足该金额时仍提取该金额，仅以现金余额封顶）。
    withdraw_fixed_amount: float = 5000.0
    # 每月提现开关：当月不盈利（已实现盈亏 <= 0）则不提取。仅 monthly 模式生效。
    withdraw_skip_when_no_profit: bool = True
    # ---- Armed 量价背离（缩量项）口径 ----
    # 盘中「缩量」项的量能口径：annualized=当日累计量按 48/已过5分钟根数 线性年化成日量
    # 当量（只会高估早盘量→缩量项偏保守，本次修复默认）；debiased=用实测累计分布去偏；
    # cumulative=旧口径（累计量直比全天均量，系统性偏松，C3 已知），仅留作回滚。
    risk_volume_caliber: str = "annualized"
    # 缩量阈值：折算后的当日量能低于「前 5 日全天均量 × 本比例」即视为缩量（0~1 小数）。
    risk_volume_ratio: float = 0.8

    def __post_init__(self) -> None:
        if len(self.installment_amounts) != 16:
            raise ValueError("installment_amounts 必须恰好包含16笔")
        if any(float(x) <= 0 for x in self.installment_amounts):
            raise ValueError("每笔金额必须为正数")
        # 16 笔规模口径：fixed（绝对金额）或 equity（按账户权益等比缩放）。
        if self.installment_scale_mode not in ("fixed", "equity"):
            raise ValueError("installment_scale_mode 必须是 fixed/equity 之一")
        if not 0 < float(self.installment_scale_floor) <= 1:
            raise ValueError("installment_scale_floor 必须在 (0,1] 内（放大倍数下限）")
        self.installment_scale_floor = float(self.installment_scale_floor)
        if not 0 < self.volume_participation <= 1:
            raise ValueError("volume_participation 必须在(0,1]内")
        if self.divergence_ticks < 1:
            raise ValueError("divergence_ticks 必须至少为1")
        for name in ("commission_rate", "stamp_tax_rate", "handling_fee_rate", "regulatory_fee_rate", "transfer_fee_rate"):
            if not 0 <= getattr(self, name) < 0.1:
                raise ValueError(f"{name} 必须是非负且小于 10% 的小数比例")
        if self.min_commission < 0 or self.slippage_bps < 0:
            raise ValueError("min_commission 与 slippage_bps 不能为负")
        if not 0 < self.t_target_net_spread < 1:
            raise ValueError("t_target_net_spread 必须是大于0且小于1的小数（做T净利止盈阈值）")
        try:
            datetime.strptime(self.t_exit_cutoff, "%H:%M")
        except (TypeError, ValueError):
            raise ValueError("t_exit_cutoff 必须是合法的 HH:MM 时间（做T的T+1日尾盘强平截止点）")
        # 深跌幅当日做T阈值：20cm 板（创业板/科创板）日内 -10% 才算极端恐慌事件，
        # 阈值过低会频繁触发低质量做T。0 视为关闭（归一化为 None，便于前端开关传值）；
        # 开启时必须 >= 10%。
        if self.t_deep_drop_pct is not None:
            if float(self.t_deep_drop_pct) == 0:
                self.t_deep_drop_pct = None
            elif float(self.t_deep_drop_pct) < 0.10:
                raise ValueError("t_deep_drop_pct 必须 >= 10%（20cm 板极端恐慌线，保证做T成功率）")
        # 做T准入四阈值：已可视化可调，须在源头拦下自相矛盾或无意义的值
        # （如换手上限低于下限、振幅门槛为 0），避免改完阈值后做T准入悄悄恒 False。
        if not 0 < float(self.t_min_amplitude) <= 1:
            raise ValueError("t_min_amplitude 必须在 (0,1] 内（做T准入的最小平均振幅）")
        if not 0 < float(self.t_min_expected_net_spread) <= 1:
            raise ValueError("t_min_expected_net_spread 必须在 (0,1] 内（做T准入的最小期望净利）")
        if not 0 <= float(self.t_min_turnover) <= float(self.t_max_turnover) <= 1:
            raise ValueError("做T换手率门槛需满足 0 <= t_min_turnover <= t_max_turnover <= 1")
        if not 1 <= int(self.t_min_base_entries) <= 16:
            raise ValueError("t_min_base_entries 必须在 1~16 笔之间（做T的底仓笔数门槛）")
        self.t_min_base_entries = int(self.t_min_base_entries)
        forced = int(self.force_exit_holding_days or 0)
        if not 0 <= forced <= 250:
            raise ValueError("force_exit_holding_days 必须在 0~250 个交易日之间（0 = 不限）")
        self.force_exit_holding_days = forced
        self.installment_amounts = [float(x) for x in self.installment_amounts]
        # ---- ETF beta 腿校验 -------------------------------------------------
        symbol = str(self.etf_symbol or "").strip().lower()
        if not re.fullmatch(r"(sh|sz)\.\d{6}", symbol):
            raise ValueError("etf_symbol 必须是 sh/sz 前缀的 6 位代码（如 sz.159915）")
        self.etf_symbol = symbol
        if not 0 < float(self.etf_position_pct) <= 1:
            raise ValueError("etf_position_pct 必须在 (0,1] 内（占账户权益比例）")
        if not 0 < float(self.etf_hard_stop_pct) < 1:
            raise ValueError("etf_hard_stop_pct 必须在 (0,1) 内")
        if not 0 <= float(self.etf_entry_max_deviation or 0.0) < 1:
            raise ValueError("etf_entry_max_deviation 必须在 [0,1) 内（0 表示关闭不追高保护）")
        self.etf_entry_max_deviation = float(self.etf_entry_max_deviation or 0.0)
        cooldown = int(self.etf_reentry_cooldown_days or 0)
        if not 0 <= cooldown <= 60:
            raise ValueError("etf_reentry_cooldown_days 必须在 0~60 个交易日之间（0 表示关闭冷却）")
        self.etf_reentry_cooldown_days = cooldown
        for name in ("etf_entry_ma_days", "etf_exit_ma_days"):
            value = int(getattr(self, name))
            if not 2 <= value <= 250:
                raise ValueError(f"{name} 必须在 2~250 个交易日之间")
            setattr(self, name, value)
        # ---- 盈利提现校验 ----------------------------------------------------
        if self.withdraw_mode not in {"none", "monthly", "per_trade"}:
            raise ValueError("withdraw_mode 必须是 none/monthly/per_trade 之一")
        if self.withdraw_method not in {"ratio", "fixed"}:
            raise ValueError("withdraw_method 必须是 ratio/fixed 之一")
        if not 0 < float(self.withdraw_ratio) <= 1:
            raise ValueError("withdraw_ratio 必须在 (0,1] 内（提取当期盈利的比例）")
        if float(self.withdraw_fixed_amount) < 0:
            raise ValueError("withdraw_fixed_amount 不能为负")
        self.withdraw_ratio = float(self.withdraw_ratio)
        self.withdraw_fixed_amount = float(self.withdraw_fixed_amount)
        # Armed 缩量口径：非法值必须在源头 400，而不是让该项悄悄失效。
        if self.risk_volume_caliber not in ("annualized", "debiased", "cumulative"):
            raise ValueError(
                "risk_volume_caliber 必须是 annualized/debiased/cumulative 之一，"
                f"实际 {self.risk_volume_caliber}"
            )
        if not 0 < float(self.risk_volume_ratio) <= 1:
            raise ValueError("risk_volume_ratio 必须在 (0, 1]（缩量阈值占前 5 日均量的比例）")

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
    # date 计算结果缓存（timestamp 构造后不变）。compare/repr/init 均关闭，
    # 不影响相等性、repr 与既有构造点。slots=True 下用 object.__setattr__ 写入。
    _date_cache: str | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @property
    def date(self) -> str:
        cached = self._date_cache
        if cached is None:
            cached = self.timestamp.date().isoformat()
            object.__setattr__(self, "_date_cache", cached)
        return cached

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("_date_cache", None)
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
class Withdrawal:
    """一笔盈利提现记录：从账户现金中取出已实现盈利，模拟真实资金离场。

    提现会真实扣减 ``cash``（进而降低 ``equity`` 与可买资金），并单独记账，
    不改动 ``realized_pnl``（仍为毛口径已实现盈亏）。``amount`` 已按
    ``min(原始金额, 当时可提现金)`` 封顶，永不为负。
    """

    id: str
    date: str                          # 扣款所在交易日（YYYY-MM-DD）
    amount: float                      # 实际提取金额（已封顶）
    mode: str                          # monthly / per_trade
    method: str                        # ratio / fixed
    period_profit: float               # 当期已实现盈亏基准（提取比例的基数）
    trigger: str                       # per_trade=来源成交 id；monthly=基准月份如 2026-10
    cash_after: float = 0.0            # 提现后账户现金余额

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
    # 指数 beta 腿（ETF）独立账本：与永久仓/做T仓互不影响，卖出时按 beta 成本结算。
    beta_qty: int = 0
    beta_cost: float = 0.0
    beta_fee: float = 0.0
    today_beta_buy_qty: int = 0

    @property
    def total_qty(self) -> int:
        return self.permanent_qty + self.t_qty + self.beta_qty

    @property
    def stock_qty(self) -> int:
        """个股持仓（永久仓 + 做T仓），不含 ETF beta 腿。轮动/策略只看这一项。"""
        return self.permanent_qty + self.t_qty

    @property
    def today_buy_qty(self) -> int:
        return self.today_permanent_buy_qty + self.today_t_buy_qty + self.today_beta_buy_qty

    @property
    def sellable_qty(self) -> int:
        return max(0, self.total_qty - self.today_buy_qty)

    @property
    def permanent_sellable_qty(self) -> int:
        return max(0, self.permanent_qty - self.today_permanent_buy_qty)

    @property
    def beta_sellable_qty(self) -> int:
        return max(0, self.beta_qty - self.today_beta_buy_qty)

    @property
    def average_cost(self) -> float:
        if not self.total_qty:
            return 0.0
        return money((self.permanent_cost + self.t_cost + self.beta_cost) / self.total_qty)

    @property
    def buy_fee_in_cost(self) -> float:
        """当前持仓成本中包含的买入手续费（卖出费用不在此列）。"""
        return money(self.permanent_fee + self.t_fee + self.beta_fee)

    def public_dict(self) -> dict[str, Any]:
        # 平均成本已按「分」四舍五入，用它反算市值/浮盈会产生系统性误差
        # （如 400 股 × 0.0036 元 = 1.44 元），因此单独给出未除股数的精确成本。
        total_cost = money(self.permanent_cost + self.t_cost + self.beta_cost)
        market_value = money(self.last_price * self.total_qty)
        return {
            "symbol": self.symbol,
            "total_qty": self.total_qty,
            "sellable_qty": self.sellable_qty,
            "permanent_qty": self.permanent_qty,
            "permanent_cost": money(self.permanent_cost),
            "beta_qty": self.beta_qty,
            "beta_cost": money(self.beta_cost),
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
