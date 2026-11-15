"""指数 beta 腿（ETF）的纯决策逻辑：入场、出场、仓位。

设计约束（见 AGENTS.md 架构原则 1）：回测与模拟盘共用这里的同一份判定，
不在两边各写一套阈值。本模块不碰网络、不碰账户，只吃「指数收盘序列 + 配置」，
方便单测。

语义（用户确认）：
- 触发不是「L0 大门关闭」，而是「个股轮动当前空仓」——即使大门开着，只要
  L3/L4 选不出股票，也继续持有 ETF。
- 一旦选出个股，ETF 让位（卖出换股）；ETF 与个股互斥。
- ETF 自己的退出只由两条决定：指数收盘跌破出场均线，或 ETF 价格触及硬止损。
- 入场保护（可选，默认关闭）：指数相对入场均线偏离过大时不追高；清仓后冷却
  N 个交易日内不再入场。两项都是为了压掉「均线附近反复打脸」的高频磨损。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .domain import Bar, money


def sma(values: Sequence[float], window: int) -> float | None:
    """简单移动平均；样本不足返回 None。"""
    if window <= 0 or len(values) < window:
        return None
    total = sum(float(item) for item in values[-window:])
    return total / window


def index_closes(bars: Sequence[Bar]) -> list[float]:
    return [float(item.close) for item in bars]


def entry_allowed(
    index_bars: Sequence[Bar], entry_ma_days: int, exit_ma_days: int | None = None,
    *, max_deviation: float = 0.0,
) -> bool:
    """入场：指数收盘站上 N 日均线（趋势向上才吃 beta）。

    同时要求站上出场均线：否则「刚因跌破 20 日线清仓、次日又因仍在 60 日线上方买回」
    会在 T+1 限制下反复空转（买了当天卖不掉，只能白扛一天）。

    ``max_deviation``：不追高保护（0~1 小数，0 表示关闭）。指数收盘高于入场均线的
    幅度超过该比例时不建仓——避免在「反弹确认」的末段追买。回测复盘显示，偏离
    入场均线 >8% 的入场几乎全是亏损单（追高），而趋势启动初期的入场偏离度较小。
    """
    closes = index_closes(index_bars)
    average = sma(closes, entry_ma_days)
    if average is None or average <= 0 or closes[-1] <= average:
        return False
    if max_deviation and max_deviation > 0:
        if closes[-1] > average * (1 + float(max_deviation)):
            return False
    if exit_ma_days:
        exit_average = sma(closes, exit_ma_days)
        if exit_average is None or closes[-1] <= exit_average:
            return False
    return True


def reentry_cooldown_ready(
    exit_date: str | None, today: str, cooldown_days: int,
    trade_days: Sequence[str] | None,
) -> bool:
    """清仓后的再入场冷却是否已满足（按交易日计）。

    ``trade_days`` 为覆盖 [exit_date, today] 的升序交易日列表；``None`` 表示日历
    不可用——此时放行（宁可恢复交易，也不因数据缺失永久空仓）。
    """
    if not exit_date or not cooldown_days or int(cooldown_days) <= 0:
        return True
    if trade_days is None:
        return True
    exit_text, today_text = str(exit_date)[:10], str(today)[:10]
    if today_text <= exit_text:
        return False
    elapsed = sum(1 for day in trade_days if exit_text < str(day)[:10] <= today_text)
    return elapsed >= int(cooldown_days)


def exit_reason(
    index_bars: Sequence[Bar], entry_cost: float, last_price: float,
    *, exit_ma_days: int, hard_stop_pct: float,
) -> str | None:
    """出场判定，返回可读原因；继续持有返回 None。

    - 硬止损优先：ETF 价格较建仓成本回撤 ≥ hard_stop_pct。
    - 趋势出场：指数收盘跌破 exit_ma_days 日均线。
    """
    if entry_cost > 0 and last_price > 0:
        if last_price <= entry_cost * (1 - hard_stop_pct):
            return f"ETF硬止损{hard_stop_pct * 100:.1f}%"
    closes = index_closes(index_bars)
    average = sma(closes, exit_ma_days)
    if average is not None and closes[-1] < average:
        return f"指数跌破{exit_ma_days}日线"
    return None


def target_quantity(
    equity: float, position_pct: float, price: float, *, min_qty: int = 100, step: int = 100,
) -> int:
    """按「账户权益 × 仓位比例」计算可买份数，向下取整到申报单位。"""
    if equity <= 0 or position_pct <= 0 or price <= 0:
        return 0
    budget = equity * position_pct
    raw = int(budget / price)
    step = max(1, int(step))
    qty = (raw // step) * step
    if qty < min_qty:
        return 0
    return qty


@dataclass(slots=True)
class BetaSnapshot:
    """报告/前端展示用的 beta 腿摘要。"""

    symbol: str
    enabled: bool
    entry_date: str | None = None
    exit_date: str | None = None
    entry_cost: float = 0.0
    exit_price: float = 0.0
    pnl: float = 0.0
    holding_days: int = 0
    exit_reason: str | None = None
    trade_count: int = 0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "enabled": self.enabled,
            "entry_date": self.entry_date, "exit_date": self.exit_date,
            "entry_cost": money(self.entry_cost), "exit_price": self.exit_price,
            "pnl": money(self.pnl), "holding_days": self.holding_days,
            "exit_reason": self.exit_reason, "trade_count": self.trade_count,
        }
