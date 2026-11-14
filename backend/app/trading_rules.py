"""A股分板块交易规则单一来源。

规则口径依据沪深交易所《交易规则》（2026年修订，2026-07-06 施行）分板块建模：
最小申报数量、数量递增单位、限价/市价单笔上限、零股卖出、涨跌幅、价格笼子。
原来「所有标的买入 100 股整数手 + 全局 ±20% 涨跌幅」的一刀切口径由此取代。

设计约束：
- **合成标的（TEST / SYNTH001 等）没有板块**，回退旧口径（100 股整数手 + config.limit_pct），
  不破坏既有回测与单元测试。
- 本模块只描述规则，不做 IO、不连行情，纯函数可单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache


# 未建模清单：这些规则系统当前没有逐条实现，UI 需如实标注，不要伪称已覆盖。
NOT_MODELED: tuple[str, ...] = (
    "集合竞价阶段（9:15-9:25 / 14:57-15:00）的申报与撤单时间窗",
    "市价单的具体委托类型（对手方最优/本方最优/最优五档即时成交等）",
    "有效申报价格区间的盘中临时调整（如科创板盘中临停、严重异常波动）",
    "新股上市首日（含无涨跌幅限制期）的特殊申报与价格限制",
    "大宗交易、盘后固定价格交易的申报门槛与时间",
    "融券/转融通、股份回购等特殊账户的可卖额度口径",
)


@dataclass(frozen=True)
class BoardRules:
    """单个板块的交易规则快照。

    数量字段（min_qty/step/odd_lot_threshold）单位为股；
    limit_order_cap / market_order_cap 是**单笔申报上限股数**（不是金额！）：
    沪深交易所对各板块市价/限价申报的笔数上限以「股」为单位，例如创业板市价单笔
    不超过 5 万股、限价单笔不超过 10 万股。早期版本曾误把该上限按「元」建模，
    导致回测里大额止盈卖单被误判 REJECTED，此处统一纠正为股数口径。
    """

    board: str                 # 板块名，如「科创板」
    min_qty: int               # 最小申报数量（买入）
    step: int                  # 数量递增单位：超出最小申报后的增量倍数
    limit_order_cap: float     # 限价单笔申报上限（股）
    market_order_cap: float    # 市价单笔申报上限（股）
    odd_lot_threshold: int     # 零股卖出阈值：持仓低于该值可一次性整笔卖出
    limit_pct: float           # 涨跌幅限制（小数，0.2 = ±20%）
    cage_pct: float            # 价格笼子百分比（相对基准价）
    cage_abs_floor: float      # 价格笼子绝对下限（元）；0 表示纯百分比口径
    rule_basis: str            # 规则依据（展示用）

    @property
    def cage_label(self) -> str:
        """价格笼子的展示口径。"""
        if self.cage_abs_floor <= 0:
            return f"纯 {self.cage_pct * 100:g}%"
        return f"{self.cage_pct * 100:g}% 与 {self.cage_abs_floor:g} 元孰高"

    @property
    def odd_lot_label(self) -> str:
        return f"余额 <{self.odd_lot_threshold} 一次性卖" if self.odd_lot_threshold > 0 else "不支持零股卖出"

    def to_dict(self) -> dict:
        return {
            "board": self.board,
            "min_qty": self.min_qty,
            "step": self.step,
            "limit_order_cap": self.limit_order_cap,
            "market_order_cap": self.market_order_cap,
            "odd_lot_threshold": self.odd_lot_threshold,
            "odd_lot_label": self.odd_lot_label,
            "limit_pct": self.limit_pct,
            "cage_pct": self.cage_pct,
            "cage_abs_floor": self.cage_abs_floor,
            "cage_label": self.cage_label,
            "rule_basis": self.rule_basis,
        }


_HU_SHEN_ZHU_BAN = "沪深交易所《交易规则》（2026年修订，2026-07-06施行）"
_RULE_BASIS = f"依据{_HU_SHEN_ZHU_BAN}分板块口径"


@lru_cache(maxsize=None)
def _all_rules() -> dict[str, BoardRules]:
    """六个板块的规则表。key 与 market.SymbolInfo.board 的板块名对齐。

    单笔申报上限均为**股数**（limit/market_order_cap）。
    北交所数据源暂未覆盖，未建模。
    """
    return {
        "科创板": BoardRules(
            board="科创板", min_qty=200, step=1,
            limit_order_cap=100_000.0, market_order_cap=50_000.0,
            odd_lot_threshold=200, limit_pct=0.20,
            cage_pct=0.02, cage_abs_floor=0.0, rule_basis=_RULE_BASIS,
        ),
        "创业板": BoardRules(
            board="创业板", min_qty=100, step=100,
            limit_order_cap=100_000.0, market_order_cap=50_000.0,
            odd_lot_threshold=100, limit_pct=0.20,
            cage_pct=0.02, cage_abs_floor=0.10, rule_basis=_RULE_BASIS,
        ),
        "沪市主板": BoardRules(
            board="沪市主板", min_qty=100, step=100,
            limit_order_cap=1_000_000.0, market_order_cap=100_000.0,
            odd_lot_threshold=100, limit_pct=0.10,
            cage_pct=0.02, cage_abs_floor=0.10, rule_basis=_RULE_BASIS,
        ),
        "深市主板": BoardRules(
            board="深市主板", min_qty=100, step=100,
            limit_order_cap=1_000_000.0, market_order_cap=100_000.0,
            odd_lot_threshold=100, limit_pct=0.10,
            cage_pct=0.02, cage_abs_floor=0.10, rule_basis=_RULE_BASIS,
        ),
        "沪市B股": BoardRules(
            board="沪市B股", min_qty=100, step=100,
            limit_order_cap=1_000_000.0, market_order_cap=100_000.0,
            odd_lot_threshold=100, limit_pct=0.10,
            cage_pct=0.02, cage_abs_floor=0.10, rule_basis=_RULE_BASIS,
        ),
        "深市B股": BoardRules(
            board="深市B股", min_qty=100, step=100,
            limit_order_cap=1_000_000.0, market_order_cap=100_000.0,
            odd_lot_threshold=100, limit_pct=0.10,
            cage_pct=0.02, cage_abs_floor=0.10, rule_basis=_RULE_BASIS,
        ),
    }


# 合成标的回退口径：与改动前的 broker 行为一致，保证旧回测/旧测试不被打断。
SYNTHETIC_FALLBACK = BoardRules(
    board="合成标的（回退旧口径）", min_qty=100, step=100,
    limit_order_cap=0.0, market_order_cap=0.0,
    odd_lot_threshold=100, limit_pct=0.20,
    cage_pct=0.0, cage_abs_floor=0.0,
    rule_basis="合成标的无板块，回退「100股整数手 + config.limit_pct」旧口径",
)


def board_of(symbol: str) -> str:
    """从标准代码（如 sh.688200 / sz.300001 / TEST）判定板块名；无板块返回空串。

    判定口径与 market.SymbolInfo.board 保持一致：B股/科创板/创业板按代码段，
    主板按交易所前缀（sh.6xxxxx→沪市主板、sz.0xxxxx→深市主板）。合成标的返回空串。
    北交所（bj 前缀 / 43/82/83/87/88/920）数据源未覆盖，未建模，返回空串走合成回退。
    """
    text = str(symbol or "").strip().lower()
    code = text.split(".")[-1]
    exchange = text.split(".", 1)[0] if "." in text else ""
    if not code.isdigit() or len(code) != 6:
        return ""
    if code.startswith("900"):
        return "沪市B股"
    if code.startswith("200"):
        return "深市B股"
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301", "302")):
        return "创业板"
    if exchange == "sh" or code.startswith(("600", "601", "603", "605")):
        return "沪市主板"
    if exchange == "sz" or code.startswith(("000", "001", "002", "003", "004")):
        return "深市主板"
    return ""


def rules_for(symbol: str) -> BoardRules:
    """取标的对应板块规则；合成标的（无板块）回退旧口径，不抛异常。"""
    board = board_of(symbol)
    if not board:
        return SYNTHETIC_FALLBACK
    return _all_rules()[board]


def all_board_rules() -> list[BoardRules]:
    """六板块规则表，按科创板/创业板/沪深主板顺序输出（B股附后）。"""
    order = ("科创板", "创业板", "沪市主板", "深市主板", "沪市B股", "深市B股")
    table = _all_rules()
    return [table[name] for name in order]


def validate_quantity(
    symbol: str, quantity: int, side: str, sellable_qty: int | None = None,
) -> str:
    """按板块校验申报数量，合法返回空串，不合法返回具体原因（可直接展示给用户）。

    - 买入：科创板 ≥200 股且可 1 股递增；创业板/主板 ≥100 股且 100 整数倍。
    - 卖出：同样按板块口径；但持仓余额低于板块零股阈值时允许一次性整笔卖出
      （如科创板持仓 150 股可整笔卖 150，创业板/主板持仓 <100 股可整笔卖）。
    """
    rules = rules_for(symbol)
    qty = int(quantity)
    if qty <= 0:
        return "申报数量必须为正整数"
    if side.upper() == "SELL":
        # 零股整笔卖出：余额不足一个最小申报单位时，允许一次性清仓。
        if sellable_qty is not None and 0 < sellable_qty < rules.min_qty:
            return "" if qty == sellable_qty else (
                f"{rules.board}零股余额 {sellable_qty} 股只能一次性整笔卖出"
            )
    if qty < rules.min_qty:
        return f"{rules.board}最小申报 {rules.min_qty} 股，当前 {qty} 股"
    if rules.step > 1 and qty % rules.step:
        return f"{rules.board}申报数量须为 {rules.step} 股整数倍，当前 {qty} 股"
    return ""


def validate_order_amount(
    symbol: str, quantity: int, order_type: str, unit_price: float | None = None,
) -> str:
    """按板块校验单笔申报数量上限（限价/市价分别计，单位为**股**）。

    沪深交易所对各板块的单笔申报上限以「股」计（如创业板市价单笔 ≤5 万股、
    限价单笔 ≤10 万股），与委托金额、价格无关，故本函数只看数量；``unit_price``
    参数仅为兼容旧调用方保留，不参与判定。上限为 0 表示未建模（合成标的回退
    口径），直接放行。注意：市价单没有委托价也能校验，broker 层不应因缺价格跳过。
    """
    rules = rules_for(symbol)
    cap = rules.limit_order_cap if order_type.upper() == "LIMIT" else rules.market_order_cap
    if cap <= 0:
        return ""
    qty = int(quantity)
    if qty > cap:
        label = "限价" if order_type.upper() == "LIMIT" else "市价"
        return (
            f"{rules.board}{label}单笔申报上限 {cap:,.0f} 股，"
            f"当前 {qty:,} 股"
        )
    return ""


def limit_pct_for(symbol: str) -> float:
    """标的的涨跌幅限制（小数）。合成标的回退 0.20，由调用方再叠加 config 覆盖。"""
    return rules_for(symbol).limit_pct


def price_cage_bounds(symbol: str, reference_price: float) -> tuple[float, float] | None:
    """价格笼子：返回 (下限, 上限)；reference_price 无效或该板块未建模笼子返回 None。

    - 科创板：纯 ±2%。
    - 创业板/沪深主板：±2% 与 ±0.1 元孰高（低价股用绝对值放宽）。
    """
    rules = rules_for(symbol)
    if rules.cage_pct <= 0 or reference_price <= 0:
        return None
    pct = rules.cage_pct * reference_price
    half = pct if rules.cage_abs_floor <= 0 else max(pct, rules.cage_abs_floor)
    lower = round(reference_price - half, 4)
    upper = round(reference_price + half, 4)
    return lower, upper


def validate_limit_price_cage(symbol: str, limit_price: float, reference_price: float) -> str:
    """限价委托价格笼子校验；越界返回具体原因，合法返回空串。"""
    rules = rules_for(symbol)
    bounds = price_cage_bounds(symbol, reference_price)
    if bounds is None:
        return ""
    lower, upper = bounds
    if limit_price < lower or limit_price > upper:
        return (
            f"{rules.board}限价委托须在价格笼子内（{lower} ~ {upper} 元，"
            f"基准价 {reference_price} 元的 {rules.cage_label}），当前 {limit_price} 元"
        )
    return ""
