from __future__ import annotations

import math
import os
import random
import uuid
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Callable

from .beta_sleeve import entry_allowed, exit_reason, reentry_cooldown_ready, target_quantity
from .broker import BrokerAccount
from .domain import Bar, Fill, OrderRequest, StrategyConfig, money, utc_now
from .etf_data import cache_path as etf_cache_path
from .etf_data import ensure_etf_bars
from .screener import ScreenerConfig, run_screener
from .strategy import LayeredTStrategy
from .trading_rules import rules_for
from .universe import BOARD_INDEX, UniverseStore


def _normalize_fill_timestamp(fill: Fill, bar: Bar) -> None:
    """把「日线口径成交」的时间戳改写为开盘时刻 09:30:00。

    日线 bar 的统一时间戳是收盘 15:00:00（``domain.daily_close_timestamp``），
    走日线口径的建仓/加仓/清仓成交（NEXT_OPEN 开盘撮合）继承的是 bar 时间戳，
    界面上会显示成「15:00 成交」；这里按 bar 粒度改写为同日 09:30:00，与
    「次日开盘成交」的真实语义一致（按 timeframe 判定，不再按 0 点字符串匹配，
    避免误伤真实的 15:00 五分钟收盘 bar）。

    5 分钟成交不受影响：真实 5 分钟 bar 的时间戳是 09:35/15:00 等盘中时刻，保留原值。
    """
    if bar.timeframe == "daily":
        fill.timestamp = f"{bar.date}T09:30:00"


def repo_rate_for_date(date_str: str, config: StrategyConfig) -> float:
    """某交易日的逆回购年化收益率：以日期为种子在 [repo_yield_min, repo_yield_max] 取值。

    用 ``random.Random(f"repo:{date_str}")`` 做确定性随机——同一日期永远取到同一个
    rate，保证同一份回测报告重跑结果完全一致（可复现）。真实国债逆回购利率每天
    随资金面波动，这里用「日内均匀随机」模拟这种日间抖动，而不是固定值。
    """
    low = float(config.repo_yield_min)
    high = float(config.repo_yield_max)
    if high <= low:
        return low
    return random.Random(f"repo:{date_str}").uniform(low, high)


def apply_repo_income(account: BrokerAccount, config: StrategyConfig, date_str: str,
                      calendar_days: int = 1, notional: float | None = None) -> tuple[float, float]:
    """闲置资金逆回购：每个交易日盘后结算，返回 ``(净收益, 计提前闲置资金)``。

    闲置资金 ``idle = account.equity - account.market_value``（= 未被持仓占用的现金，
    含 ETF beta 腿未占用部分——market_value 已含 beta 市值）。仅当
    ``idle_cash_repo_enabled`` 且 ``idle > 0`` 时计提。

    **按自然日计息**（贴合真实国债逆回购）：``利息 = notional × rate × calendar_days / 365``。
    ``calendar_days`` 是本次计提覆盖的自然日数 = 本次交易日 − 上一个交易日（首日为 1）。
    周四做逆回购、下个交易日是周五 → 覆盖 1 天；周五做、下个交易日是周一 → 覆盖 3 天
    （把周末算进去）；节假日前最后一个交易日 → 覆盖整个假期。这样资金在非交易日也
    持续计息，与真实机制一致。

    **手续费**：真实约成交金额的 0.001%（10 万收 1 元、30 元封顶），从利息里扣除，
    ``净收益 = 利息 − 手续费``。``notional`` 是计息本金（首次交收日的闲置资金），
    缺省用当前 ``idle``。

    返回 ``(0.0, 0.0)`` 表示当日未计提（功能关闭 / 净收益 ≤ 0 / 无闲置资金）。调用方把
    income / idle / rate 写进 equity_curve 点（``repo_income`` / ``repo_rate`` /
    ``repo_idle``），供明细弹窗；``repo_idle`` 取**计提前**的闲置资金。
    """
    if not bool(getattr(config, "idle_cash_repo_enabled", False)):
        return 0.0, 0.0
    idle = money(float(account.equity) - float(account.market_value))
    if idle <= 0:
        return 0.0, 0.0
    days = max(1, int(calendar_days))
    rate = repo_rate_for_date(date_str, config)
    principal = float(notional) if notional is not None else idle
    gross = money(idle * rate * days / 365.0)
    if gross <= 0:
        return 0.0, 0.0
    # 手续费：成交金额 × 0.001%，1 元起、30 元封顶（真实规则）。
    fee = min(max(principal * 0.00001, 1.0), 30.0)
    income = money(gross - fee)
    if income <= 0:
        return 0.0, 0.0
    account.cash = money(account.cash + income)
    return income, idle

# 回测指数加载：start 前推的日历天缓冲，保证回测首日往前 ≥21 根（L0 ret20 + 策略大盘门）。
_INDEX_LOOKBACK_BUFFER_DAYS = 45
# 持仓标的日线加载：start 前推的日历天缓冲，保证回测首日往前 ≥250 根（52周高/ATR）。
# 与 max_rows 一起构成「覆盖整个回测区间」的窗口（长区间回测不能只取最近 260 根）。
_DAILY_LOOKBACK_BUFFER_DAYS = 420
_DAILY_WINDOW_MAX_ROWS = 2000

# 「T+1 可卖数量不足」属于隔夜单提交时点的瞬时限制（真实券商次日也会放行，
# 见 broker 的 NEXT_OPEN 次日口径），是交易执行细节而不是数据/近似口径，
# 不进报告顶部「数据与近似口径提醒」（个股与 ETF 一视同仁；拒绝仍记 SIGNAL 事件）。
_TRANSIENT_REJECT_MARKERS = ("T+1可卖数量不足",)


def _is_transient_reject(message: str | None) -> bool:
    text = str(message or "")
    return any(marker in text for marker in _TRANSIENT_REJECT_MARKERS)


# 年化收益率「买入持有」三基准的指数代码：
# 1. 全仓沪深300；2. 全仓中证500；3. 半仓创业板指 + 半仓科创50（399006/000688）。
_BENCHMARK_INDEX_CODES = ("sh.000300", "sh.000905", "sz.399006", "sh.000688")


def _index_interval_return(bars: list[Bar], start: str, end: str) -> float | None:
    """区间 [start, end] 内指数收益：末根收盘 / 首根收盘 - 1；不足两根返回 None。

    只统计落在区间内的 bar（``start <= bar.date <= end``），避免把区间外的
    缓冲段（start 前推的 lookback 数据）算进收益基准。
    """
    window = [item for item in bars if start <= item.date <= end]
    if len(window) < 2 or window[0].close <= 0:
        return None
    return window[-1].close / window[0].close - 1


def compute_buy_hold_benchmarks(
    bars_by_code: dict[str, list[Bar]], timeline: list[str],
) -> list[dict[str, Any]]:
    """计算「买入持有」三基准收益，供年化收益率对比。

    1. 全仓沪深300（sh.000300）；
    2. 全仓中证500（sh.000905）；
    3. 半仓创业板指 + 半仓科创50 = 0.5×sz.399006 + 0.5×sh.000688。

    缺失指数数据（不足两根或首根收盘 <= 0）时对应基准降级为 ``0.0`` 占位，
    不抛错（旧报告 / 数据源缺该指数时优雅降级）。组合基准：一条腿缺失时该
    半仓按 0 收益（现金）处理，两条腿都缺失则 0.0。
    """
    if not timeline:
        return []
    start, end = timeline[0], timeline[-1]
    hs300 = _index_interval_return(bars_by_code.get("sh.000300", []), start, end)
    zz500 = _index_interval_return(bars_by_code.get("sh.000905", []), start, end)
    gem = _index_interval_return(bars_by_code.get("sz.399006", []), start, end)
    star = _index_interval_return(bars_by_code.get("sh.000688", []), start, end)
    composite = None
    if gem is not None or star is not None:
        composite = 0.5 * (gem or 0.0) + 0.5 * (star or 0.0)
    return [
        {"label": "沪深300", "symbol": "sh.000300",
         "return": hs300 if hs300 is not None else 0.0},
        {"label": "中证500", "symbol": "sh.000905",
         "return": zz500 if zz500 is not None else 0.0},
        {"label": "半仓创业板+半仓科创板", "symbol": "组合",
         "return": composite if composite is not None else 0.0},
    ]


def submit_split_requests(
    account: BrokerAccount, requests, timestamp: datetime,
    events: list[dict[str, Any]], warnings: list[str],
) -> None:
    """按板块单笔申报上限拆分策略委托后提交，并把每笔子单记入 SIGNAL 事件。

    策略层按「整笔持仓」生成清仓/止盈委托，但交易所对单笔市价申报有股数上限
    （创业板/科创板 5 万股）。这里统一拆分，回测与模拟盘自动策略共用同一口径。

    拒绝不再静默重复：同一拒绝原因只记一次 warning。历史 bug——176,200 股整笔
    卖出超创业板 5 万股上限，每根 K 线重发一次，单份报告刷出 2.4 万条相同拒绝。
    """
    for request in requests:
        if request.quantity <= 0:
            continue
        for order in account.submit_split(request, timestamp):
            events.append({
                "date": timestamp.isoformat(), "type": "SIGNAL", "order_id": order.id,
                "symbol": order.symbol, "side": order.side, "purpose": order.purpose,
                "quantity": order.quantity, "reason": order.reason, "status": order.status,
                "execution_mode": order.execution_mode,
            })
            if order.status == "REJECTED" and not _is_transient_reject(order.message):
                message = f"[委托被拒] {order.symbol} {order.side} {order.quantity}股：{order.message}"
                if message not in warnings:
                    warnings.append(message)

# 选股 warnings 分两类（整合进换票轨迹，见 _screen）：
# - 全局口径声明：整轮回测不变的口径/数据边界（做T粒度、财务深筛关闭、数据源覆盖盲区），
#   只需在报告顶部提示一次，去重后进 report.warnings。
# - 逐轮决策日志：每次选股「当天为什么这么决策」的动态结果（大盘门开关、有无候选、
#   深度超跌旁路几只），必须挂在对应选股日的轨迹项上，否则平铺成一串无时间戳的行、
#   无法对应到具体哪一轮。这类不再进全局 warnings。
_GLOBAL_WARNING_MARKERS = (
    "enable_fundamental_veto",   # 财务深筛关闭（防未来数据口径）
    "财务深筛已关闭",             # L4 不否决的口径重申
    "数据源未覆盖",               # 非标审计/资金占用等盲区需人工复核
    "漏斗轮动回测启用",           # 5分钟做T / 日线降级 口径
    "漏斗轮动回测为日线口径",
)


def monthly_breakdown(
    equity_curve: list[dict[str, Any]], initial_cash: float,
    withdrawals: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """按自然月汇总账户盈亏，供回测报告「每月盈亏」面板。

    口径（与 performance_metrics 的净值口径一致，含未实现盈亏与手续费）：
    - ``pnl``：当月最后一个交易日权益 - 上月最后一个交易日权益（首月以初始资金为基期），
      再加回当月出金金额（提现会真实扣减权益，但仍是账户赚到的钱），
      即「当月真实盈亏（含出金）」，不把换仓当外部现金流；
    - ``return_rate``：``pnl / 基期权益``；
    - ``trading_days``：当月实际回测交易日数（净值曲线点数）；
    - ``max_drawdown``：当月内从峰值（峰值基期取进入当月时的权益）起的最大回撤，正数；
    - ``cumulative_return``：当月末权益（含截至当月末的累计出金）/ 初始资金 - 1。

    净值曲线按日期升序，顺序扫描天然按月切分；空曲线返回空列表（不抛错，
    供 ``performance_metrics([])`` 之外的读路径兜底）。
    """
    if not equity_curve:
        return []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in equity_curve:
        month = str(item.get("date") or "")[:7]
        if len(month) == 7 and month[4] == "-":
            buckets.setdefault(month, []).append(item)
    # 按月汇总出金：提现真实扣减 equity，但资金已落袋，应计入当月盈亏与累计收益，
    # 避免「盈利出金后统计显示为亏损」的口径偏差（统计数据层面把盈利出金金额考虑上）。
    month_withdrawn: dict[str, float] = {}
    for w in withdrawals or []:
        month = str(w.get("date") or "")[:7]
        if len(month) == 7 and month[4] == "-":
            month_withdrawn[month] = month_withdrawn.get(month, 0.0) + float(w.get("amount", 0.0) or 0.0)
    base_cash = float(initial_cash)
    previous_equity = base_cash
    cumulative_withdrawn = 0.0
    result: list[dict[str, Any]] = []
    for month in sorted(buckets):
        points = buckets[month]
        end_equity = float(points[-1]["equity"])
        month_out = month_withdrawn.get(month, 0.0)
        cumulative_withdrawn += month_out
        pnl = (end_equity - previous_equity) + month_out
        peak = previous_equity
        month_drawdown = 0.0
        for point in points:
            equity = float(point["equity"])
            peak = max(peak, equity)
            if peak > 0:
                month_drawdown = max(month_drawdown, 1 - equity / peak)
        result.append({
            "month": month,
            "start_equity": money(previous_equity),
            "end_equity": money(end_equity),
            "pnl": money(pnl),
            "return_rate": pnl / previous_equity if previous_equity > 0 else 0.0,
            "trading_days": len(points),
            "max_drawdown": month_drawdown,
            "cumulative_return": (end_equity + cumulative_withdrawn) / base_cash - 1
                if base_cash > 0 else 0.0,
            # 闲置资金逆回购当月累计收益（仅开启时 > 0；旧报告无 repo_income 键时为 0）。
            "repo_income": money(sum(float(point.get("repo_income") or 0.0) for point in points)),
        })
        previous_equity = end_equity
    return result


def xirr_annualized(
    cashflows: list[tuple[str, float]], *, lower: float = -0.9999, upper: float = 10.0,
) -> float | None:
    """资金加权年化收益率（XIRR）：让所有现金流按时间折现后净现值为 0 的年化利率。

    与 ``annualized_return``（期初期末两点的时间加权口径）互补：XIRR 把中途的
    盈利提现（真实资金离场）也当成现金流，反映「这笔钱实际什么时候进出」，对
    有出金的账户更贴近真实回报。口径：投入为负（-initial_cash），出金与期末权益
    为正（出金虽离开账户但属赚到的钱回流）。

    解法用二分法（不依赖 scipy，符号变化时必然收敛）：NPV 对 r 单调递减，
    ``[lower, upper]`` 两端 NPV 异号则必有唯一根；无符号变化（全正/全负现金流）
    返回 None 让前端优雅降级。现金流日期不足两条同样返回 None。
    """
    points: list[tuple[datetime, float]] = []
    for raw_date, amount in cashflows:
        amount = float(amount)
        if not amount:
            continue
        try:
            day = datetime.fromisoformat(str(raw_date)[:10])
        except ValueError:
            continue
        points.append((day, amount))
    if len(points) < 2:
        return None
    t0 = min(day for day, _ in points)
    flows = [((day - t0).days / 365.0, amount) for day, amount in points]

    def npv(rate: float) -> float:
        return sum(amount / (1.0 + rate) ** years for years, amount in flows)

    lo, hi = lower, upper
    npv_lo, npv_hi = npv(lo), npv(hi)
    if npv_lo * npv_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        npv_mid = npv(mid)
        if abs(npv_mid) < 1e-9:
            return mid
        if npv_lo * npv_mid <= 0:
            hi, npv_hi = mid, npv_mid
        else:
            lo, npv_lo = mid, npv_mid
    return (lo + hi) / 2


# 回撤深度分桶上限（小数口径）：水下日按「1 - equity/peak」落进 ≤2% / 2–5% /
# 5–10% / >10% 四档。低回撤策略「水下占比」高但多为浅水下，单看占比易误读为
# 「长期深套」，这里把水下天数按深度拆开，供前端并列展示。
DRAWDOWN_DEPTH_BUCKETS: tuple[tuple[str, float], ...] = (
    ("lte_2pct", 0.02), ("2_to_5pct", 0.05), ("5_to_10pct", 0.10),
)


def _empty_depth_distribution() -> dict[str, dict[str, float]]:
    """回撤深度分桶的空骨架（含 gt_10pct 兜底档）。"""
    dist: dict[str, dict[str, float]] = {}
    for key, _ in DRAWDOWN_DEPTH_BUCKETS:
        dist[key] = {"days": 0, "pct_of_underwater": 0.0, "pct_of_trading": 0.0}
    dist["gt_10pct"] = {"days": 0, "pct_of_underwater": 0.0, "pct_of_trading": 0.0}
    return dist


def underwater_stats(equity_curve: list[dict[str, Any]]) -> dict[str, Any]:
    """净值曲线水下时长：从峰值回落到收复的全部区间统计。

    - ``longest_underwater_days``：最长连续水下交易日数（equity 严格低于此前峰值）；
    - ``longest_underwater_calendar_days``：同一段水下区间跨越的日历天数；
    - ``longest_underwater_start`` / ``longest_underwater_end``：该区间起止日期；
    - ``underwater_days`` / ``underwater_ratio``：水下交易日数与占比；
    - ``currently_underwater`` / ``current_drawdown_days``：当前是否仍处水下及已持续天数；
    - ``drawdown_depth_distribution``：水下日按回撤深度（≤2% / 2–5% / 5–10% / >10%）
      的天数拆分，各档给出 ``days``、占水下日的比例 ``pct_of_underwater``、占全部
      交易日的比例 ``pct_of_trading``——把「水下占比高」到底是浅晃还是深套讲清楚。

    逐日比对「此前峰值」（含当日），equity < 峰值即记为水下一天；空曲线返回全 0。
    """
    if not equity_curve:
        return {
            "longest_underwater_days": 0, "longest_underwater_calendar_days": 0,
            "longest_underwater_start": None, "longest_underwater_end": None,
            "underwater_days": 0, "underwater_ratio": 0.0,
            "currently_underwater": False, "current_drawdown_days": 0,
            "drawdown_depth_distribution": _empty_depth_distribution(),
        }
    peak = float(equity_curve[0]["equity"])
    longest = current = total = 0
    longest_start: str | None = None
    longest_end: str | None = None
    current_start: str | None = None
    depth_days = {key: 0 for key, _ in DRAWDOWN_DEPTH_BUCKETS}
    depth_days["gt_10pct"] = 0
    for item in equity_curve:
        equity = float(item["equity"])
        day = str(item.get("date") or "")
        if equity >= peak:
            # 收复峰值（或创新高）：一段水下区间在此结束。
            if current > longest:
                longest, longest_start, longest_end = current, current_start, day
            current = 0
            current_start = None
            peak = max(peak, equity)
            continue
        if current == 0:
            current_start = day
        current += 1
        total += 1
        # 按当日回撤深度归档：depth = (peak - equity) / peak（peak>0，因 equity<peak）。
        # 用减法后再相除，而非 1 - equity/peak——后者在边界值（如 0.95/0.90）上会因
        # 浮点抵消误差产生 0.05000...044，把刚好 5%/10% 的深度错归到下一档。
        depth = (peak - equity) / peak if peak > 0 else 0.0
        for key, upper in DRAWDOWN_DEPTH_BUCKETS:
            if depth <= upper:
                depth_days[key] += 1
                break
        else:
            depth_days["gt_10pct"] += 1
    if current > longest:
        longest, longest_start, longest_end = current, current_start, str(equity_curve[-1].get("date") or "")
    calendar = 0
    if longest and longest_start and longest_end:
        try:
            calendar = max(0, (
                datetime.fromisoformat(longest_end[:10]) - datetime.fromisoformat(longest_start[:10])
            ).days)
        except ValueError:
            calendar = 0
    n = len(equity_curve)
    distribution: dict[str, dict[str, float]] = {}
    for key, _ in DRAWDOWN_DEPTH_BUCKETS:
        days = depth_days[key]
        distribution[key] = {
            "days": days,
            "pct_of_underwater": days / total if total else 0.0,
            "pct_of_trading": days / n if n else 0.0,
        }
    gt = depth_days["gt_10pct"]
    distribution["gt_10pct"] = {
        "days": gt,
        "pct_of_underwater": gt / total if total else 0.0,
        "pct_of_trading": gt / n if n else 0.0,
    }
    return {
        "longest_underwater_days": longest,
        "longest_underwater_calendar_days": calendar,
        "longest_underwater_start": longest_start,
        "longest_underwater_end": longest_end,
        "underwater_days": total,
        "underwater_ratio": total / n,
        "currently_underwater": current > 0,
        "current_drawdown_days": current,
        "drawdown_depth_distribution": distribution,
    }


def cashflow_adjusted_series(
    equity_curve: list[dict[str, Any]], withdrawals: list[dict[str, Any]] | None,
) -> tuple[list[float], list[float]]:
    """把提现视为外部现金流，重建「时间加权」口径的调整后权益序列与日收益序列。

    报告口径（``performance_metrics`` 现有字段）把提现当亏损记在净值曲线上：
    每笔盈利提现直接扣 cash → equity 形成负台阶，于是提现日的日收益、波动、
    maxDD、水下时长都被系统性污染（提现越多，Sharpe 越被压低）。

    这里重建 TWR（Time-Weighted Return）口径：
    - ``adjusted[t] = equity[t] + Σ(截至 t 的提现额)``：把已落袋的提现加回，
      得到「若无提现、利润全部留在账户」的复利净值；
    - ``returns[t] = adjusted[t] / adjusted[t-1] - 1``：把提现日不再记成亏损。

    与 ``annualized_return``（名义加回口径）和 ``xirr_annualized``（资金加权口径）
    互补：TWR 反映「策略本身的风险收益比」，XIRR 反映「这笔钱实际什么时候进出」。
    返回 ``(adjusted_equities, daily_returns)``，长度与 equity_curve 一致（首日收益为 0）。
    """
    wd_by_date: dict[str, float] = {}
    for w in withdrawals or []:
        day = str(w.get("date") or "")[:10]
        amount = float(w.get("amount", 0.0) or 0.0)
        if day and amount:
            wd_by_date[day] = wd_by_date.get(day, 0.0) + amount
    adjusted: list[float] = []
    cum = 0.0
    for item in equity_curve:
        day = str(item.get("date") or "")[:10]
        cum += wd_by_date.get(day, 0.0)
        adjusted.append(float(item["equity"]) + cum)
    returns: list[float] = [0.0]
    for previous, current in zip(adjusted, adjusted[1:]):
        returns.append(current / previous - 1 if previous > 0 else 0.0)
    return adjusted, returns


def cashflow_adjusted_curve(
    equity_curve: list[dict[str, Any]], adjusted_equities: list[float],
) -> list[dict[str, Any]]:
    """把调整后权益序列拼回一条带日期的曲线（供水下统计等按行取 equity/date）。

    复用原始 equity_curve 的所有字段（date/cash/market_value…），只把 ``equity``
    换成 TWR 口径的调整后权益——提现日的负台阶被抹平，水下时长不再被提现污染。
    """
    return [{**item, "equity": adj} for item, adj in zip(equity_curve, adjusted_equities)]


def cashflow_adjusted_metrics(
    equity_curve: list[dict[str, Any]], initial_cash: float,
    withdrawals: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[float]]:
    """TWR 口径的风险收益指标块：年化/波动/Sharpe/maxDD/Calmar/水下统计。

    与 ``performance_metrics`` 顶层字段（提现当亏损的「账户体验」口径）并列，
    供前端并列展示、让用户自行选择评估口径。旧报告可离线重算（用 equity_curve +
    withdrawals 即可），不需要重跑回测。

    返回 ``(指标块, adjusted_equities)``：后者即「把已落袋提现加回」的逐日权益序列，
    与 ``equity_curve`` 逐点对应，供调用方直接派生曲线字段（``equity_incl_withdrawal``），
    免得多算一遍 ``cashflow_adjusted_series``。
    """
    adjusted, returns = cashflow_adjusted_series(equity_curve, withdrawals)
    nav_returns = returns[1:] if returns else []
    volatility = stdev(nav_returns) * math.sqrt(252) if len(nav_returns) >= 2 else 0.0
    sharpe = (
        mean(nav_returns) / stdev(nav_returns) * math.sqrt(252)
        if len(nav_returns) >= 2 and stdev(nav_returns) > 0 else 0.0
    )
    # maxDD 直接在调整后权益序列上算（峰值/谷底都含加回的提现）。
    peak = adjusted[0] if adjusted else 0.0
    max_drawdown = 0.0
    for equity in adjusted:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, 1 - equity / peak)
    # TWR 累计收益 + 日历年化：与 annualized_return 的 elapsed_days/365 口径一致，
    # 但分母换成「首日调整后权益」（含已加回的提现），避免把提现当期末一次性收益。
    if adjusted and adjusted[0] > 0:
        twr_total_return = adjusted[-1] / adjusted[0] - 1
        start = datetime.fromisoformat(str(equity_curve[0]["date"])).date()
        end = datetime.fromisoformat(str(equity_curve[-1]["date"])).date()
        elapsed_days = max(1, (end - start).days)
        annualized = (
            (adjusted[-1] / adjusted[0]) ** (365 / elapsed_days) - 1
            if adjusted[-1] > 0 else -1.0
        )
    else:
        twr_total_return = 0.0
        annualized = 0.0
    calmar = annualized / max_drawdown if max_drawdown > 0 else 0.0
    # 水下统计按调整后权益重算：复用同一构造，把提现台阶替换为调整后权益。
    underwater = underwater_stats(cashflow_adjusted_curve(equity_curve, adjusted))
    return {
        "annualized_return": annualized,
        "total_return": twr_total_return,
        "volatility": volatility,
        "sharpe_ratio": sharpe,
        "max_drawdown": max_drawdown,
        "calmar_ratio": calmar,
        "underwater": underwater,
    }, adjusted


def exposure_breakdown(equity_curve: list[dict[str, Any]]) -> dict[str, float]:
    """敞口拆分：总敞口 / 个股敞口 / ETF beta 敞口 / 完全空仓占比。

    总敞口（``exposure``）= mean(market_value / equity)，其中 market_value 含 ETF beta 仓
    （``Position.total_qty = permanent_qty + t_qty + beta_qty``），因此单看总敞口会误以为
    「个股只用了 23%」。这里把 ETF 部分单独拆出（``beta_value`` 已记录在 equity_curve），
    并统计完全空仓（既无个股也无 ETF）交易日占比，供前端并列展示。
    """
    if not equity_curve:
        return {"exposure_total": 0.0, "exposure_stock": 0.0, "exposure_beta": 0.0, "idle_ratio": 0.0}
    total: list[float] = []
    stock: list[float] = []
    beta: list[float] = []
    idle = 0
    for item in equity_curve:
        equity = float(item["equity"]) or 0.0
        if equity <= 0:
            total.append(0.0)
            stock.append(0.0)
            beta.append(0.0)
            idle += 1
            continue
        market_value = float(item.get("market_value") or 0.0)
        beta_value = float(item.get("beta_value") or 0.0)
        total.append(market_value / equity)
        stock.append(max(0.0, market_value - beta_value) / equity)
        beta.append(beta_value / equity)
        if market_value <= 0:
            idle += 1
    n = len(equity_curve)
    return {
        "exposure_total": mean(total),
        "exposure_stock": mean(stock),
        "exposure_beta": mean(beta),
        "idle_ratio": idle / n,
    }


def performance_metrics(
    equity_curve: list[dict[str, Any]], initial_cash: float,
    rounds: list[dict[str, Any]], trades: list[dict[str, Any]],
    total_fees: float, t_net_profit: float, max_invested: float,
    max_float_loss: float, buy_hold_return: float,
    total_withdrawn: float = 0.0, withdrawn_count: int = 0,
    withdrawals: list[dict[str, Any]] | None = None,
    buy_hold_benchmarks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not equity_curve:
        raise ValueError("净值曲线为空")
    equities = [float(item["equity"]) for item in equity_curve]
    final_equity = equities[-1]
    # 盈利出金金额：提现明细里「以盈利为基准」（period_profit > 0）的提取额。
    # 提现会真实扣减 equity，但资金已落袋，仍是账户赚到的钱——统计口径要加回来，
    # 否则「盈利出金后净利/收益被低估、甚至显示亏损」。
    if withdrawals:
        profit_withdrawn = sum(
            float(w.get("amount", 0.0) or 0.0) for w in withdrawals
            if float(w.get("period_profit", 0.0) or 0.0) > 0
        )
        if not total_withdrawn:
            total_withdrawn = sum(float(w.get("amount", 0.0) or 0.0) for w in withdrawals)
            withdrawn_count = len(withdrawals)
    else:
        # 未传提现明细时，按「全部出金视为盈利出金」兜底（默认配置下提现仅来自盈利）。
        profit_withdrawn = total_withdrawn
    adjusted_final = final_equity + profit_withdrawn
    net_profit = money(adjusted_final - initial_cash)
    total_return = adjusted_final / initial_cash - 1
    start = datetime.fromisoformat(str(equity_curve[0]["date"])).date()
    end = datetime.fromisoformat(str(equity_curve[-1]["date"])).date()
    elapsed_days = max(1, (end - start).days)
    annualized = (adjusted_final / initial_cash) ** (365 / elapsed_days) - 1 if adjusted_final > 0 else -1.0
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
    # 选股胜率：用轮次的「个股腿」盈亏（stock_pnl，已剔除 ETF beta 腿）判胜负，
    # 与顶层 win_rate（用 pnl，含 ETF）并列。ETF beta 腿跨轮独立持有，其盯市盈亏会被
    # 记进轮次 pnl，导致「选股选错但 ETF 顺手抬成胜」的误判——选股胜率把这条干扰剥掉。
    # 旧报告轮次缺 stock_pnl 时降级为 None（前端隐藏），不参与分母。
    stock_closed = [item for item in closed if item.get("stock_pnl") is not None]
    stock_wins = [item for item in stock_closed if float(item["stock_pnl"]) > 0]
    stock_win_rate = (
        len(stock_wins) / len(stock_closed) if stock_closed else None
    )
    exposure = mean(
        item["market_value"] / item["equity"] if item["equity"] else 0.0
        for item in equity_curve
    )
    # 资金加权年化（XIRR）：把盈利出金当成回流现金流，与时间加权 annualized 互补。
    xirr_flows: list[tuple[str, float]] = [(str(equity_curve[0]["date"]), -float(initial_cash))]
    for withdrawal in withdrawals or []:
        amount = float(withdrawal.get("amount", 0.0) or 0.0)
        if amount:
            xirr_flows.append((str(withdrawal.get("date") or ""), amount))
    xirr_flows.append((str(equity_curve[-1]["date"]), adjusted_final))
    xirr = xirr_annualized(xirr_flows)
    # 现金流调整（TWR）口径：把提现视为外部现金流而非亏损，重建日收益/波动/Sharpe/
    # maxDD/Calmar/水下。与顶层字段（账户体验口径）并列输出，供前端并列展示。
    # 旧报告可离线重算（只需 equity_curve + withdrawals），不需重跑回测。
    cashflow_adjusted, adjusted_equities = cashflow_adjusted_metrics(
        equity_curve, initial_cash, withdrawals,
    )
    # 逐点派生「含出金」权益：原始 equity 已扣掉提现已落袋的钱（提现日是负台阶），
    # 这里把「截至当日的累计提现」加回，与 TWR 口径同源。复用上方同一次
    # ``cashflow_adjusted_series`` 的结果，不再另写累加逻辑（铁律 1：口径收敛在共用逻辑）。
    for item, equity_incl in zip(equity_curve, adjusted_equities):
        item["equity_incl_withdrawal"] = equity_incl
    # 敞口拆分：总敞口含 ETF beta 仓；个股/ETF 分项 + 完全空仓占比，避免误读
    # 「exposure 23% = 个股只用 23%」。
    exposure_parts = exposure_breakdown(equity_curve)
    # 顶层水下时长改用现金流调整（TWR）后的曲线：提现日在原始 equity 曲线上是负台阶，
    # 会被误判为「水下一天」。与下方 cashflow_adjusted 块复用同一次 underwater 计算，
    # 保证两个口径一致（不再算两遍）。顶层口径即用户看到的「当前水下/最长水下」。
    underwater = cashflow_adjusted["underwater"]
    # 注意区分：逐行写回 equity_curve 的 item["drawdown"]（上方 L~491）保持**原始口径**，
    # 供前端画原始净值曲线的回撤着色，不受提现调整影响；只有 underwater 块换成了调整口径。
    # 稳健性版本：剔除最佳自然月 + 最大 3 笔盈利轮次后重算，看收益是否靠少数极端项撑起。
    monthly_list = monthly_breakdown(equity_curve, initial_cash, withdrawals)
    # 闲置资金逆回购总收益（Σ equity_curve.repo_income）：仅开启逆回购时有值，
    # 自然已含在权益里，这里单独给出供「总收益」块展示构成。
    repo_income_total = money(sum(float(item.get("repo_income") or 0.0) for item in equity_curve))
    best_month_pnl = max((float(row["pnl"]) for row in monthly_list), default=0.0)
    top3_win_pnl = sum(item["pnl"] for item in sorted(wins, key=lambda row: row["pnl"], reverse=True)[:3])
    robust_net_profit = net_profit - best_month_pnl - top3_win_pnl
    robust_total_return = (initial_cash + robust_net_profit) / initial_cash - 1 if initial_cash > 0 else 0.0
    robust_annualized = (
        (initial_cash + robust_net_profit) / initial_cash
    ) ** (365 / elapsed_days) - 1 if initial_cash > 0 and initial_cash + robust_net_profit > 0 else -1.0
    return {
        "initial_cash": money(initial_cash), "final_equity": money(final_equity),
        "net_profit": net_profit, "total_return": total_return,
        "annualized_return": annualized, "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe, "volatility": volatility, "calmar_ratio": calmar,
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        # 选股胜率：剔 ETF 腿后的纯选股胜负（旧报告轮次缺 stock_pnl 时为 None，前端隐藏）。
        "stock_win_rate": stock_win_rate,
        "profit_factor": profit_factor, "total_rounds": len(rounds),
        "closed_rounds": len(closed), "total_trades": len(trades),
        "total_fees": money(total_fees), "t_net_profit": money(t_net_profit),
        "repo_income_total": repo_income_total,
        "t_trade_count": sum(1 for item in trades if item["purpose"] == "T"),
        "total_withdrawn": money(total_withdrawn), "withdrawn_count": withdrawn_count,
        "profit_withdrawn": money(profit_withdrawn),
        "max_invested": money(max_invested), "exposure": exposure,
        "exposure_total": exposure_parts["exposure_total"],
        "exposure_stock": exposure_parts["exposure_stock"],
        "exposure_beta": exposure_parts["exposure_beta"],
        "idle_ratio": exposure_parts["idle_ratio"],
        "max_float_loss": money(max_float_loss), "buy_hold_return": buy_hold_return,
        "buy_hold_benchmarks": buy_hold_benchmarks or [],
        "monthly": monthly_list,
        # 资金加权年化（XIRR）：出金当成回流现金流，与时间加权 annualized 互补；
        # 无符号变化的现金流（从未出金且亏损）返回 None，前端降级隐藏。
        "xirr_annualized": xirr,
        # 稳健性版本：剔除最佳自然月 + 最大 3 笔盈利轮次后的净利/收益/年化，
        # 用于判断收益是否靠少数极端项撑起（``*_excluded`` 记被剔除的金额）。
        "robustness": {
            "excluded_best_month_pnl": money(best_month_pnl),
            "excluded_top3_win_pnl": money(top3_win_pnl),
            "net_profit": money(robust_net_profit),
            "total_return": robust_total_return,
            "annualized_return": robust_annualized,
        },
        # 净值曲线水下时长：最长连续水下区间 + 水下交易日占比 + 当前是否仍处水下。
        "underwater": underwater,
        # 现金流调整（TWR）口径指标块：把提现当外部现金流而非亏损，重建
        # annualized/total/volatility/sharpe/max_drawdown/calmar/underwater。
        # 与顶层同名字段并列输出（顶层是「提现当亏损」的账户体验口径），
        # 让用户自选评估口径；旧报告可用 equity_curve + withdrawals 离线重算。
        "cashflow_adjusted": cashflow_adjusted,
    }


# -- 轮次盈亏分腿（个股腿 / ETF beta 腿 / 轮内提现） --------------------------
# 口径（已用报告 6ceb50df 的 R001 与成交明细互相验证）：
#   rounds[].pnl = stock_pnl + beta_pnl - withdraw_in_round
# rounds[].pnl 是「轮内权益变化」，混合了个股腿真实盈亏、ETF beta 腿盈亏（含未实现）
# 与轮内提现。ETF 腿用「资本账户法」计算：
#   beta_pnl = beta_value_end - beta_value_start
#              - Σ(买入金额 + 买入费用) + Σ(卖出金额 - 卖出费用)
# 个股腿用恒等式取残差：stock_pnl = pnl + withdraw_in_round - beta_pnl。
# R001（688012，2020-08-10~09-21）：pnl=-192112.33、轮内提现 0、ETF 腿=-30339.71
# → 个股腿 = -161772.62，与成交明细里的个股已实现盈亏完全一致。


def _beta_fill_cash_flows(
    beta_fills: list[Any] | None, start_date: str, end_date: str,
) -> tuple[float, float]:
    """汇总轮次区间 ``[start_date, end_date]`` 内 ETF beta 腿的买入 / 卖出现金流。

    兼容两种成交形态：引擎的 ``Fill`` 对象（``account.trades``，带 purpose）与
    报告 ``data_info.beta.fills`` 的紧凑 dict（quantity/price/fee）。买入计
    「成交金额 + 费用」、卖出计「成交金额 - 费用」。区间左闭右闭：互斥模式下
    「选到个股换仓」的 ETF 卖出在轮次建仓日开盘成交，属于本轮 beta 腿的退出。
    """
    buy_total = 0.0
    sell_total = 0.0
    for fill in beta_fills or []:
        if isinstance(fill, dict):
            day = str(fill.get("date") or "")
            side = fill.get("side")
            amount = fill.get("amount")
            if amount is None:
                amount = float(fill.get("quantity") or 0.0) * float(fill.get("price") or 0.0)
            fee = fill.get("total_fee", fill.get("fee", 0.0)) or 0.0
        else:
            if getattr(fill, "purpose", None) != "BETA":
                continue
            day = str(getattr(fill, "timestamp", "") or "")[:10]
            side = getattr(fill, "side", None)
            amount = getattr(fill, "amount", 0.0)
            fee = getattr(fill, "total_fee", 0.0)
        if not day or not (start_date <= day <= end_date):
            continue
        amount = float(amount or 0.0)
        fee = float(fee or 0.0)
        if side == "BUY":
            buy_total += amount + fee
        elif side == "SELL":
            sell_total += amount - fee
    return money(buy_total), money(sell_total)


def _round_withdraw_total(
    withdrawals: list[Any] | None, start_date: str, end_date: str,
) -> float:
    """轮次区间 ``(start_date, end_date]`` 内的提现合计。

    左开右闭：建仓日 ``new_day`` 的月度提现发生在建仓撮合之前，属于上一轮；
    轮次结束日当天的提现算本轮。兼容 ``Withdrawal`` 对象与已落库的 dict。
    """
    total = 0.0
    for item in withdrawals or []:
        if isinstance(item, dict):
            day = str(item.get("date") or "")
            amount = item.get("amount")
        else:
            day = str(getattr(item, "date", "") or "")
            amount = getattr(item, "amount", 0.0)
        if start_date < day <= end_date:
            total += float(amount or 0.0)
    return money(total)


def decompose_round_pnl(
    rounds: list[dict[str, Any]] | None,
    equity_curve: list[dict[str, Any]] | None,
    withdrawals: list[dict[str, Any]] | None,
    beta_fills: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """给缺少分腿字段的轮次就地补齐 ``stock_pnl`` / ``beta_pnl`` / ``withdraw_in_round``。

    新回测在轮次闭合时已写好这三个字段；历史报告没有，这里用报告自带的
    ``equity_curve`` + ``withdrawals`` + ``data_info.beta.fills`` 离线重算，
    幂等（已有 ``stock_pnl`` 的轮次跳过）。一趟扫描净值曲线建「日期→序号 /
    日期→beta_value」映射，单轮 O(1) 取前一日基准，买卖成交与提现按区间过滤。

    未结束（OPEN）轮次没有 ``end_date``：用净值曲线最后一个交易日作区间终点
    （其 ``pnl`` 本来就是截至该日的权益变化）；CLOSED 却缺 ``end_date`` 的脏数据
    跳过，不瞎补。返回同一个 ``rounds`` 列表（便于调用方直接使用）。
    """
    rows = [row for row in (rounds or []) if isinstance(row, dict)]
    if not rows:
        return []
    dates: list[str] = []
    beta_by_date: dict[str, float] = {}
    for point in equity_curve or []:
        day = str(point.get("date") or "")
        if not day:
            continue
        dates.append(day)
        beta_by_date[day] = float(point.get("beta_value") or 0.0)
    index = {day: position for position, day in enumerate(dates)}
    last_day = dates[-1] if dates else None

    def beta_value_on(day: str) -> float:
        position = index.get(day)
        if position is not None:
            return beta_by_date[dates[position]]
        # 不在曲线上（停牌 / 脏数据）：退到不晚于该日的最后一个净值点。
        before = bisect_right(dates, day) - 1
        return beta_by_date[dates[before]] if before >= 0 else 0.0

    for row in rows:
        if row.get("stock_pnl") is not None:
            continue
        start = str(row.get("start_date") or "")
        end = str(row.get("end_date") or "")
        if not start:
            continue
        if not end:
            if str(row.get("status") or "").upper() == "CLOSED":
                continue
            end = last_day or ""
        pnl = row.get("pnl")
        if not end or pnl is None:
            continue
        position = index.get(start)
        previous = dates[position - 1] if position is not None and position > 0 else None
        beta_start = money(beta_by_date.get(previous, 0.0)) if previous else 0.0
        beta_end = money(beta_value_on(end))
        buy, sell = _beta_fill_cash_flows(beta_fills, start, end)
        beta_pnl = money(beta_end - beta_start - buy + sell)
        withdraw = _round_withdraw_total(withdrawals, start, end)
        row["withdraw_in_round"] = withdraw
        row["beta_pnl"] = beta_pnl
        row["stock_pnl"] = money(float(pnl) + withdraw - beta_pnl)
    return rows


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
        quan: Any = None, hooks: dict[str, Callable[..., Any]] | None = None,
    ) -> None:
        self.store = store
        self.market = market
        # 可选 QuanStore：提供 5 分钟数据源（做 T 用）。缺省 None → 降级日线口径。
        self.quan = quan
        # 持仓标的 5 分钟惰性缓存：任意时刻最多驻留一只标的的持有期分钟数据。
        self._held_m5_code: str | None = None
        self._held_m5_bars: list[Bar] = []
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
        # 后台任务协作钩子（默认不传 = 与旧同步行为完全一致），语义同 BacktestEngine。
        self.hooks = hooks or {}
        # 指数 beta 腿（ETF）：与个股轮动共用账户与同一份决策逻辑
        # （beta_sleeve，模拟盘复用），ETF 成交记 BETA 账本，不占轮动序列。
        self.etf_enabled = bool(self.config.etf_beta_enabled)
        self.etf_symbol = self.config.etf_symbol
        self.etf_bars: list[Bar] = []
        self.etf_bars_by_date: dict[str, Bar] = {}
        self._beta_fills: list[dict[str, Any]] = []
        # 轮次分腿的「前一日 beta 市值」基准：日期 → 当日收盘 beta_value。
        # 建仓/续传时由 equity_curve 重建，之后随每日净值点滚动更新。
        self._beta_value_by_date: dict[str, float] = {}
        self._beta_entry_cost = 0.0
        self._beta_entry_date: str | None = None
        self._beta_last_exit_reason: str | None = None
        self._beta_source = ""
        self._prepare_data()
        # 交易日 → 序号：beta 冷却按「平仓后第 N 个交易日」计，避免逐日扫 timeline。
        self._timeline_index: dict[str, int] = {
            day: index for index, day in enumerate(self.timeline)
        }
        if self.etf_enabled:
            self._prepare_etf()

    # -- ETF beta 腿数据 --------------------------------------------------

    @staticmethod
    def _data_dir() -> Path:
        """与 main.py 同口径的数据目录（ETF 缓存落这里，双端共用）。"""
        default = Path(__file__).resolve().parent.parent / "data"
        return Path(os.environ.get("WB_DATA_DIR", str(default)))

    def _prepare_etf(self) -> None:
        """装入 ETF 日线：优先本地缓存，缺失时联网补采一次并落盘。"""
        start = self.timeline[0] if self.timeline else None
        end = self.timeline[-1] if self.timeline else None
        path = etf_cache_path(self._data_dir(), self.etf_symbol)
        fetcher = None
        if getattr(self, "market", None) is not None and hasattr(self.market, "fetch_tencent_daily"):
            exchange, code = self.etf_symbol.split(".", 1)

            def fetcher(symbol: str, start_date: str | None, end_date: str | None):
                bars = self.market.fetch_tencent_daily(
                    f"{exchange}{code}", start_date or "2020-01-01", end_date or utc_now()[:10],
                )
                for bar in bars:
                    bar.symbol = symbol
                return bars

        self.etf_bars, self._beta_source = ensure_etf_bars(
            path, self.etf_symbol, start, end, fetcher=fetcher,
        )
        # 统一标的代码：缓存/补采可能带腾讯风格（sz159915），撮合按标准代码匹配持仓，
        # 不归一化会出现「有行情但永远不成交」的静默 bug。
        for etf_bar in self.etf_bars:
            etf_bar.symbol = self.etf_symbol
        if not self.etf_bars:
            self.etf_enabled = False
            self.warnings.append(
                f"ETF beta 腿已启用，但 {self.etf_symbol} 日线缓存与联网补采均不可用，本轮按关闭处理"
            )
            return
        self.etf_bars_by_date = {bar.date: bar for bar in self.etf_bars}
        # 部分覆盖：ETF 日线首根晚于回测起点时，早于该日的时段没有 ETF 行情、
        # beta 腿不会建仓（静默空窗）。必须显式提示，避免误以为 beta 逻辑失效。
        self._beta_coverage_start = self.etf_bars[0].date
        self._beta_coverage_end = self.etf_bars[-1].date
        self._beta_partial_coverage = bool(
            self.timeline and self._beta_coverage_start > self.timeline[0]
        )
        if self._beta_partial_coverage:
            self.warnings.append(
                f"ETF beta 腿数据部分覆盖：{self.etf_symbol} 日线仅从 {self._beta_coverage_start} 起"
                f"（回测起点 {self.timeline[0]}），早于该日期的时段不会建仓 ETF"
            )
        if self._beta_source != "cache":
            self.warnings.append(
                f"ETF beta 腿：{self.etf_symbol} 日线本地缓存未覆盖回测区间，已联网补采并写入 "
                f"{path.name}（之后回测优先读本地缓存）"
            )
        guards: list[str] = []
        if self.config.etf_entry_max_deviation > 0:
            guards.append(
                f"指数偏离入场均线 >{self.config.etf_entry_max_deviation * 100:.0f}% 不追高建仓"
            )
        if self.config.etf_reentry_cooldown_days > 0:
            guards.append(
                f"清仓后冷却 {self.config.etf_reentry_cooldown_days} 个交易日再入场"
            )
        guard_text = f"；入场保护：{'、'.join(guards)}" if guards else ""
        # 并存模式：ETF 与个股可同时持有，建仓额度以「权益×仓位」和「可用现金」的较小者为准。
        mode_text = (
            "；并存模式：与个股同时持有，建仓额度以可用现金封顶"
            if self.config.etf_beta_coexist
            else "；个股空仓期间持有、选到个股后卖出换股"
        )
        self.warnings.append(
            f"ETF beta 腿已启用：{self.etf_symbol}，仓位 {self.config.etf_position_pct * 100:.0f}% 权益，"
            f"入场 指数>{self.config.etf_entry_ma_days}日线，出场 指数<{self.config.etf_exit_ma_days}日线 "
            f"或 ETF 硬止损 {self.config.etf_hard_stop_pct * 100:.1f}%{guard_text}{mode_text}"
        )

    def _etf_bar_for(self, day: str) -> Bar | None:
        return self.etf_bars_by_date.get(day)

    def _beta_index_history(self, day: str) -> list[Bar]:
        """ETF 对应的板块指数日线（截至 day 收盘，含当日）。创业板 ETF → 创业板指。

        用 ETF 跟踪指数做趋势信号，而不是用 ETF 自身价格算均线：指数不受 ETF
        折溢价/分红影响，信号更稳；成交与止损仍用 ETF 真实价格。
        """
        bars = self.index_history.get("创业板") or []
        return [bar for bar in bars if bar.date <= day]

    def _has_pending_beta(self, side: str) -> bool:
        return any(
            order.purpose == "BETA" and order.side == side
            and order.status in {"PENDING", "PARTIALLY_FILLED"}
            for order in self.account.orders
        )

    def _beta_position(self):
        return self.account.positions.get(self.etf_symbol)

    def _beta_qty(self) -> int:
        position = self._beta_position()
        return position.beta_qty if position else 0

    def _beta_last_exit_date(self) -> str | None:
        """最近一次 ETF 清仓日（按 BETA 卖出成交日期取最大值）。"""
        dates = [item["date"] for item in self._beta_fills if item["side"] == "SELL"]
        return dates[-1] if dates else None

    def _beta_cooldown_ready(self, day: str) -> bool:
        """清仓后冷却：按交易日数判断是否已过再入场冷却期。"""
        cooldown = int(self.config.etf_reentry_cooldown_days or 0)
        exit_date = self._beta_last_exit_date()
        if cooldown <= 0 or not exit_date:
            return True
        start = self._timeline_index.get(exit_date)
        end = self._timeline_index.get(day)
        if start is None or end is None:
            return True
        return reentry_cooldown_ready(
            exit_date, day, cooldown, self.timeline[start:end + 1],
        )

    def _manage_beta(self, day: str) -> None:
        """收盘后的 ETF 决策：先处理出场/换股，再处理空仓入场。"""
        if not self.etf_enabled:
            return
        position = self._beta_position()
        beta_qty = position.beta_qty if position else 0
        index_bars = self._beta_index_history(day)
        etf_bar = self._etf_bar_for(day)
        if beta_qty > 0:
            entry_cost = (
                position.beta_cost / position.beta_qty if position.beta_qty else 0.0
            )
            last_price = etf_bar.close if etf_bar else position.last_price
            reason = exit_reason(
                index_bars, entry_cost, last_price,
                exit_ma_days=self.config.etf_exit_ma_days,
                hard_stop_pct=self.config.etf_hard_stop_pct,
            )
            if reason is None and not self.config.etf_beta_coexist \
                and (self._has_stock_target() or self.account.has_stock_position):
                # 互斥模式：一旦选出个股（或个股已建仓），ETF 立即清仓让位。
                # 并存模式（etf_beta_coexist）下 ETF 只由指数均线/硬止损决定进出。
                reason = "选到个股换仓"
            if reason and not self._has_pending_beta("SELL"):
                self._beta_last_exit_reason = reason
                self._submit_beta_sell(day, beta_qty, reason)
            return
        # 空仓（或并存模式下的空 beta 腿）且指数趋势向上 → 建 beta 腿。
        # 互斥模式要求个股腿完全空仓；并存模式不看个股，只按可用现金定额度。
        if not self.config.etf_beta_coexist:
            if self.account.has_stock_position or self._has_stock_target():
                return
        if self._has_pending_beta("BUY"):
            return
        if not self._beta_cooldown_ready(day):
            return
        if not entry_allowed(
            index_bars, self.config.etf_entry_ma_days,
            self.config.etf_exit_ma_days,
            max_deviation=self.config.etf_entry_max_deviation,
        ):
            return
        if etf_bar is None or etf_bar.close <= 0:
            return
        rules = rules_for(self.etf_symbol)
        qty = self._beta_target_quantity(
            etf_bar.close, rules.min_qty, rules.step,
        )
        if qty <= 0:
            return
        self._submit_beta_order(day, "BUY", qty, "指数趋势向上，空仓建 beta 腿")

    def _beta_target_quantity(self, price: float, min_qty: int, step: int) -> int:
        """beta 腿建仓份数：按权益×仓位比例，并存模式下再以可用现金封顶。

        并存模式下个股与 ETF 分摊同一笔现金：若按权益算出的预算超过可用现金，
        就只用现金（留 0.2% 给费用），避免出现「额度足够但买不进」的空转，
        也不会挤占个股的加仓资金造成现金透支。
        """
        equity = self.account.equity
        if self.config.etf_beta_coexist:
            affordable = self.account.available_cash / 1.002
            equity = min(equity, affordable)
        return target_quantity(
            equity, self.config.etf_position_pct, price,
            min_qty=min_qty, step=step,
        )

    def _has_stock_target(self) -> bool:
        return bool(getattr(self, "_current_symbol", None))

    def _submit_beta_sell(self, day: str, qty: int, reason: str) -> None:
        self._submit_beta_order(day, "SELL", qty, reason)

    def _submit_beta_order(self, day: str, side: str, qty: int, reason: str) -> None:
        timestamp = datetime.fromisoformat(f"{day}T15:00:00")
        request = OrderRequest(
            symbol=self.etf_symbol, side=side, quantity=int(qty),
            order_type="MARKET", purpose="BETA", execution_mode="NEXT_OPEN",
            reason=reason,
        )
        for order in self.account.submit_split(request, timestamp):
            self.events.append({
                "date": timestamp.isoformat(), "type": "SIGNAL", "order_id": order.id,
                "symbol": order.symbol, "side": order.side, "purpose": order.purpose,
                "quantity": order.quantity, "reason": order.reason,
                "status": order.status, "execution_mode": order.execution_mode,
            })
            if order.status == "REJECTED" and not _is_transient_reject(order.message):
                message = f"[ETF委托被拒] {order.symbol} {order.side} {order.quantity}份：{order.message}"
                if message not in self.warnings:
                    self.warnings.append(message)

    def _handle_beta_fills(self, fills: list[Fill], bar: Bar | None = None) -> None:
        sold = False
        for fill in fills:
            if fill.purpose != "BETA":
                continue
            # ETF 日线成交与个股日线成交同口径：日线粒度成交 = 次开 09:30:00
            if bar is not None:
                _normalize_fill_timestamp(fill, bar)
            self._beta_fills.append({
                "date": fill.timestamp[:10], "side": fill.side,
                "quantity": fill.quantity, "price": fill.price,
                "fee": fill.total_fee, "realized_pnl": fill.realized_pnl,
            })
            if fill.side == "BUY":
                if self._beta_entry_date is None:
                    self._beta_entry_date = fill.timestamp[:10]
                    self._beta_last_exit_reason = None
            else:
                sold = True
            self.events.append({
                "date": fill.timestamp, "type": "ETF_FILL", "fill_id": fill.id,
                "side": fill.side, "quantity": fill.quantity, "price": fill.price,
                "fee": fill.total_fee, "reason": self._beta_last_exit_reason,
            })
        if sold and self._beta_qty() == 0:
            self.events.append({
                "date": fills[-1].timestamp, "type": "ETF_EXIT",
                "reason": self._beta_last_exit_reason or "ETF 清仓",
            })
            self._beta_entry_date = None

    def _beta_summary(self) -> dict[str, Any]:
        buys = [item for item in self._beta_fills if item["side"] == "BUY"]
        sells = [item for item in self._beta_fills if item["side"] == "SELL"]
        pnl = money(sum(item["realized_pnl"] for item in sells))
        # ETF 交易回合胜率：ETF 腿自己的每一笔卖出（平仓）按已实现盈亏判胜负，
        # 独立于个股轮次——回答「ETF 这条腿自己有多少比例的回合是赚的」。
        # 与选股胜率（按轮次 stock_pnl）分母不同、口径不同，前端并列展示、各自标注。
        sell_rounds = len(sells)
        sell_wins = sum(1 for item in sells if float(item.get("realized_pnl") or 0.0) > 0)
        win_rate = (sell_wins / sell_rounds) if sell_rounds else None
        return {
            "enabled": self.etf_enabled, "symbol": self.etf_symbol,
            "source": self._beta_source,
            "trade_count": len(self._beta_fills),
            "buy_count": len(buys), "sell_count": len(sells),
            "sell_wins": sell_wins,
            "win_rate": win_rate,
            "entry_date": buys[0]["date"] if buys else None,
            "last_exit_date": sells[-1]["date"] if sells else None,
            "exit_reason": self._beta_last_exit_reason,
            "pnl": pnl,
            "position_qty": self._beta_qty(),
            # 逐笔成交（紧凑字段）：供换票轨迹的 BETA_SPAN 区间行拆出「持仓区间 /
            # 持有交易日 / 本轮收益率 / 净盈亏」。beta 成交笔数很少（趋势跟踪，
            # 一年几笔），随报告落库体积可忽略。
            "fills": [
                {
                    "date": item["date"], "side": item["side"],
                    "quantity": item["quantity"], "price": item["price"],
                    "fee": item["fee"], "realized_pnl": item["realized_pnl"],
                }
                for item in self._beta_fills
            ],
            # 数据覆盖：ETF 日线首根晚于回测起点时，早期时段不会建仓（见 _prepare_etf）。
            "coverage_start": getattr(self, "_beta_coverage_start", None),
            "coverage_end": getattr(self, "_beta_coverage_end", None),
            "partial_coverage": bool(getattr(self, "_beta_partial_coverage", False)),
        }

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
        # 日线面板：候选池日线预加载一次，逐日选股内存切片（不再每天重扫 parquet +
        # 重建 33 万根 Bar 且不释放 —— 本地 32G 上正是这个逐日累积导致 OOM）。
        # DailyPanel 驻留约 0.5-0.8GB；逐日窗口与 load_history(end=as_of) 逐字节一致，
        # 输出与「每天现读 parquet」完全相同，只是省掉逐日磁盘扫描 + Bar 重建。
        # QuanStore（生产数据源）预加载日线面板；UniverseStore（单测桩）无此方法时
        # 置 None，_screen 回落到每日 store.load_history（原口径，测试/无 parquet 兼容）。
        _daily_loader = getattr(self.store, "load_daily_panel", None)
        self._daily_panel = (
            _daily_loader(
                codes,
                start=self.start_date.isoformat() if self.start_date else None,
                end=self.end_date.isoformat() if self.end_date else None,
            )
            if _daily_loader is not None
            else None
        )
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
        # 年化收益率「买入持有」三基准指数日线：000300/000905 不一定在 universe.db，
        # 缺失时回退 market.fetch_index_daily_by_code（内部先试 quan 缓存→baostock→tencent）。
        # 窗口与板块指数一致（start 前推 _INDEX_LOOKBACK_BUFFER_DAYS，end=timeline[-1]）。
        self._benchmark_bars: dict[str, list[Bar]] = {}
        for code in _BENCHMARK_INDEX_CODES:
            self._benchmark_bars[code] = self._load_index_series(code, index_start, index_end)
        # 持仓标的日线惰性缓存：任意时刻最多驻留一只标的的历史。
        self._held_code: str | None = None
        self._held_bars: list[Bar] = []
        # 与 _held_bars 平行的加速结构（换票时一起重建）：
        #   _held_dates：升序 date 字符串数组（供 bisect 切「截至某日」的历史）；
        #   _held_bar_by_date：{date: 当日日线 bar}（供 O(1) 取当日 bar）。
        self._held_dates: list[str] = []
        self._held_bar_by_date: dict[str, Bar] = {}
        # 板块指数升序 date 数组（供 bisect 切「<= 某日」的指数历史，替代逐元素
        # .timestamp.date().isoformat() 过滤）。
        self._index_dates: dict[str, list[str]] = {
            board: [item.date for item in bars]
            for board, bars in self.index_history.items()
        }
        # 日线换手率缓存 {code: {date: turn}}，来自 turnover_daily.parquet（做 T 准入）。
        self._turnover_cache: dict[str, dict[str, float]] | None = None

    def _load_index_series(self, code: str, index_start: str, index_end: str) -> list[Bar]:
        """加载指定指数日线：优先 store（universe.db / quan parquet），空时回退 market。

        若 ``self.store.load_index`` 返回空且 ``self.market`` 具备
        ``fetch_index_daily_by_code``（内部先试 quan 缓存→baostock→tencent），
        用其兜底；任何异常都降级为空列表（三基准缺数据时占位 0.0，不抛错）。
        """
        bars = self.store.load_index(code, max_rows=4000, start=index_start, end=index_end)
        if not bars and self.market is not None and hasattr(self.market, "fetch_index_daily_by_code"):
            try:
                fetched, _ = self.market.fetch_index_daily_by_code(code, index_start, index_end)
                bars = list(fetched or [])
            except Exception:
                bars = []
        return bars

    def _compute_buy_hold_benchmarks(self) -> list[dict[str, Any]]:
        """三基准买入持有收益（供年化收益率对比）；空时间线返回空列表。"""
        return compute_buy_hold_benchmarks(
            getattr(self, "_benchmark_bars", {}), self.timeline,
        )

    def _load_bars(self, code: str) -> list[Bar]:
        """单标的日线：覆盖「回测区间 + 起点前推缓冲」的窗口（按需单只加载）。

        长区间回测的关键：不能只取「截至最新交易日的最近 260 根」——那样回测前半段
        的一天在 tail 窗口之外，持仓回放拿不到 bar、信号/撮合直接静默停摆。
        这里显式按 [timeline[0]-缓冲, timeline[-1]] 取窗口，回测任意一天都有历史。

        做 T 准入需要日线换手率，而 quan 源日线无换手字段（turnover_rate 恒 None），
        这里从 turnover_daily.parquet（BaoStock 日线 turn，见 gen_turnover_parquet.py）
        按日期填充——与模拟盘实时快照换手同口径（架构原则 1）。
        """
        start = end = None
        if self.timeline:
            start = (
                datetime.fromisoformat(self.timeline[0]).date()
                - timedelta(days=_DAILY_LOOKBACK_BUFFER_DAYS)
            ).isoformat()
            end = self.timeline[-1]
        bars = self.store.load_history(
            [code], max_rows=_DAILY_WINDOW_MAX_ROWS, start=start, end=end,
        ).get(code, [])
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
        """当前持仓标的的日线，带单条目缓存（换票时自然替换）。

        同时维护两个平行加速结构（换票时一起重建）：
        - ``_held_dates``：升序 date 数组，供 run 循环用 bisect 切「截至某日」历史，
          替代每根 5 分钟 bar 对全量 bars 的线性重过滤；
        - ``_held_bar_by_date``：{date: bar}，供 O(1) 取当日日线，
          替代 run 循环里 ``next((i for i in bars if i.date==date), None)`` 的全量扫描。
        """
        if not code:
            return []
        if self._held_code == code:
            return self._held_bars
        bars = self._load_bars(code)
        self._held_code = code
        self._held_bars = bars
        # bars 由 load_history 返回即按日期升序（每日一根），直接建平行索引。
        self._held_dates = [item.date for item in bars]
        self._held_bar_by_date = {item.date: item for item in bars}
        return bars

    def _intraday_for(self, code: str | None) -> list[Bar]:
        """当前持仓标的的 5 分钟 bar，带单条目缓存（换票时自然替换）。

        只取回测区间内的分钟数据（start/end 下推），**不设 max_rows 上限**：长区间
        回测里持仓可能发生在区间任意位置，若按「最近 2400 根」截断，中段持仓会拿不到
        当日分钟 bar → 做 T 静默降级。单只标的全区间约 2 万根 Bar，内存可接受。
        """
        if not code or self.quan is None:
            return []
        if self._held_m5_code == code:
            return self._held_m5_bars
        start = self.start_date.isoformat() if self.start_date else None
        end = self.end_date.isoformat() if self.end_date else None
        bars = self.quan.load_minute5(code, max_rows=None, start=start, end=end)
        self._held_m5_code = code
        self._held_m5_bars = bars
        return bars

    def _intraday_by_date(self, code: str) -> dict[str, list[Bar]]:
        """某标的的 {date: [5分钟bar]}，供 run() 按日取当日分钟序列。"""
        by_date: dict[str, list[Bar]] = defaultdict(list)
        for bar in self._intraday_for(code):
            by_date[bar.date].append(bar)
        return by_date

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

    # -- 轮次盈亏分腿（个股 / ETF beta / 轮内提现）------------------------

    def _previous_trading_day(self, day: str) -> str | None:
        """轮次建仓日的前一交易日（beta 市值基准取该日收盘）。"""
        position = self._timeline_index.get(day)
        if position is None or position <= 0:
            return None
        return self.timeline[position - 1]

    def _current_beta_value(self) -> float:
        """当前 ETF beta 腿市值（按最近一次 ETF 收盘价盯市）。"""
        position = self._beta_position()
        if position is None:
            return 0.0
        return money(position.beta_qty * position.last_price)

    def _decompose_round(self, item: dict[str, Any], end_date: str | None) -> None:
        """轮次闭合 / 收尾时写入三行分腿字段（口径见 ``decompose_round_pnl``）。

        与读时离线重算同一套恒等式：``stock_pnl = pnl + 轮内提现 - ETF腿``，
        ETF 腿 = 末日市值 - 前一日市值 - 买入现金流 + 卖出现金流。
        """
        start = str(item.get("start_date") or "")
        end = str(end_date or item.get("end_date") or "")
        if not start or not end or item.get("pnl") is None:
            return
        previous = self._previous_trading_day(start)
        beta_start = money(self._beta_value_by_date.get(previous, 0.0)) if previous else 0.0
        if end in self._beta_value_by_date:
            beta_end = money(self._beta_value_by_date[end])
        else:
            # 轮次在当天盘中闭合：当日净值点尚未追加，用当前 beta 持仓市值
            # （已按 ETF 当日收盘价盯市，与随后写入 equity_curve 的值一致）。
            beta_end = self._current_beta_value()
        buy, sell = _beta_fill_cash_flows(self._beta_fills, start, end)
        beta_pnl = money(beta_end - beta_start - buy + sell)
        withdraw = _round_withdraw_total(self.account.withdrawals, start, end)
        item["withdraw_in_round"] = withdraw
        item["beta_pnl"] = beta_pnl
        item["stock_pnl"] = money(float(item["pnl"]) + withdraw - beta_pnl)

    def _build_close_diagnostic(self, close_fill: Fill | None, bar: Bar) -> dict[str, Any]:
        """轮次闭合时的「止盈执行诊断」：拆出信号价 → 成交价 的落差。

        用户困惑「价格轨收益达到 15%，实际只落袋 8.81%」的根因是：15% 是**决策 K 线
        收盘价**相对持仓成本的浮盈（``strategy._returns``），而成交走下一根 bar 的
        开盘价（含滑点），再扣掉双边费用。这里把三个口径都记下来，报告页直接展示：
        - ``signal_price`` / ``signal_return``：判定清仓那根 K 线的 close 与当时的价格轨收益；
        - ``fill_price`` / ``fill_time``：实际成交价与时间（出场 bar）；
        - ``execution_gap``：signal_price → fill_price 的落差（负 = 出场比信号价低）；
        - ``return_gap``：信号收益 → 轮次账户收益（pnl / 峰值投入）的总落差。

        缺信号（旧状态续跑 / 日线路径未触发 ``_set_close_signal``）时优雅降级，
        只返回成交侧能算的字段，不抛错。
        """
        signal = self.strategy._close_signal
        diagnostic: dict[str, Any] = {}
        if close_fill is not None:
            diagnostic["fill_price"] = close_fill.price
            diagnostic["fill_time"] = close_fill.timestamp
        if signal:
            diagnostic.update(signal)
            if close_fill is not None and signal.get("signal_price"):
                # 执行落差：成交价相对信号价的偏移（负 = 出场比信号价低，收益被吃掉）。
                diagnostic["execution_gap"] = close_fill.price / signal["signal_price"] - 1
        item = self.current_round
        if item:
            round_return = item["pnl"] / item["max_invested"] if item.get("max_invested") else None
            diagnostic["round_return"] = round_return
            if signal and signal.get("price_return") is not None and round_return is not None:
                diagnostic["return_gap"] = round_return - signal["price_return"]
        return diagnostic

    def _handle_fills(self, fills: list[Fill], session_index: int, bar: Bar) -> None:
        close_fill: Fill | None = None
        for fill in fills:
            _normalize_fill_timestamp(fill, bar)
            if fill.side == "BUY" and fill.purpose == "PERMANENT" and self.current_round is None:
                self._new_round(fill, session_index)
            self.strategy.on_fill(fill, session_index)
            self.events.append({
                "date": fill.timestamp, "type": "FILL", "fill_id": fill.id,
                "side": fill.side, "purpose": fill.purpose, "quantity": fill.quantity,
                "price": fill.price, "fee": fill.total_fee,
            })
            if fill.side == "SELL" and fill.purpose in ("PERMANENT", "T"):
                # 记最后一笔卖出成交：轮次闭合时它就是「实际出场价」，用于止盈执行诊断。
                close_fill = fill
            self._update_round(bar, session_index)
        position = self.account.position(bar.symbol)
        if self.current_round and position.total_qty == 0:
            item = self.current_round
            item["status"] = "CLOSED"
            item["end_date"] = bar.date
            item["pnl"] = money(self.account.equity - item["start_equity"])
            item["close_reason"] = self.strategy.close_reason or "整体止盈/事件熔断"
            item["t_net_profit"] = money(self.account.t_net_profit - item["t_net_profit_start"])
            item["close_diagnostic"] = self._build_close_diagnostic(close_fill, bar)
            self._decompose_round(item, bar.date)
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
        submit_split_requests(self.account, requests, timestamp, self.events, self.warnings)

    # -- 选股 ------------------------------------------------------------

    def _screen(self, as_of: str) -> dict[str, Any]:
        """在 as_of 时点跑一次 L0→L4（不联网），返回轻量快照。"""
        result = run_screener(
            self.store, self.market, self.screener_config,
            as_of=as_of, include_realtime=False,
            index_history=self.index_history,
            daily_panel=getattr(self, "_daily_panel", None),
        )
        first = result.first
        # 拆分本轮 warnings：全局口径声明去重进 self.warnings；逐轮决策日志留在本轮，
        # 由调用方挂到该选股日的轨迹项（否则平铺成无时间戳的一长串，无法对应轮次）。
        round_warnings = [
            warning for warning in result.warnings
            if not any(marker in warning for marker in _GLOBAL_WARNING_MARKERS)
        ]
        for warning in result.warnings:
            if any(marker in warning for marker in _GLOBAL_WARNING_MARKERS):
                if warning not in self.warnings:
                    self.warnings.append(warning)
        # 大盘门与候选前三：写入轨迹便于复盘「为什么关门 / 为什么选它」。
        # gate 只保留决策相关信息（板块 / ret20 / 是否开门 / 是否走超跌旁路），
        # 去掉 index/date 等冗余字段，避免逐日轨迹与断点检查点膨胀。
        gate_detail = {
            "open": bool(result.gate.get("open")),
            "boards": {
                str(board): {
                    "ret20": entry.get("ret20"),
                    "open": bool(entry.get("open")),
                    "bypass": bool(entry.get("bypass")),
                }
                for board, entry in (result.gate.get("boards") or {}).items()
            },
        }
        candidates = [
            {
                "code": candidate.code, "name": candidate.name, "board": candidate.board,
                "chg20d": candidate.chg20d, "score": candidate.score,
                "deep_checked": bool(candidate.deep_checked),
            }
            for candidate in (result.eligible or [])[:3]
        ]
        snapshot = {
            "as_of": result.as_of,
            "gate_open": bool(result.gate.get("open")),
            "gate": gate_detail,
            "funnel": dict(result.funnel),
            "first_code": first.code if first else "",
            "first_name": first.name if first else "",
            "first_chg20d": (first.chg20d if first else None),
            "candidates": candidates,
            "screen_warnings": round_warnings,
        }
        self.events.append({
            "date": as_of, "type": "SCREEN", "as_of": result.as_of,
            "gate_open": snapshot["gate_open"], "first_code": snapshot["first_code"],
            "first_name": snapshot["first_name"], "funnel": dict(result.funnel),
            "eligible_count": len(result.eligible),
            "screen_warnings": round_warnings,
        })
        return snapshot

    def run(self, resume_state: dict[str, Any] | None = None) -> dict[str, Any]:
        has_quan = self.quan is not None
        if has_quan:
            self.warnings.append("漏斗轮动回测启用5分钟做T：持仓期逐根处理5分钟bar，准入换手/盘口用日线turn+代理盘口")
        else:
            self.warnings.append("漏斗轮动回测为日线口径（无5分钟数据源），T交易与tick级风控降级")
        self.warnings.append("财务深筛已强制关闭（enable_fundamental_veto=False）：避免联网财务与未来数据")
        # 断点续传：还原引擎可变状态 + 已跑完的进度边界，跳到 cursor 之后继续。
        resume_state = resume_state or {}
        start_after = int(resume_state.get("cursor_index", -1))
        self._chg20d_at_select = dict(resume_state.get("chg20d_at_select", {}))
        self._screen_by_symbol = dict(resume_state.get("screen_by_symbol", {}))
        self._trail = list(resume_state.get("trail", []))
        self._round_just_closed = bool(resume_state.get("round_just_closed", False))
        self._beta_fills = list(resume_state.get("beta_fills", []))
        self._beta_entry_date = resume_state.get("beta_entry_date")
        self._beta_last_exit_reason = resume_state.get("beta_last_exit_reason")
        self.rounds = list(resume_state.get("rounds", []))
        self.current_round = resume_state.get("current_round")
        self.events = list(resume_state.get("events", []))
        self.warnings = list(resume_state.get("warnings", self.warnings))
        self.max_invested = float(resume_state.get("max_invested", 0.0))
        self.max_float_loss = float(resume_state.get("max_float_loss", 0.0))
        current_symbol: str | None = resume_state.get("current_symbol")
        flat_wait_days = int(resume_state.get("flat_wait_days", 0))
        session_index = int(resume_state.get("session_index", -1))
        # 持仓标的的 5 分钟序列（按日分组），换票时重新加载
        intraday_by_date: dict[str, list[Bar]] = {}
        t_trade_count = int(resume_state.get("t_trade_count", 0))

        equity_curve: list[dict[str, Any]] = list(resume_state.get("equity_curve", []))
        used_dates: list[str] = list(resume_state.get("used_dates", []))
        # 轮次分腿基准：续传时从已跑完的净值曲线重建，保证闭合轮次的
        # beta_value_start（轮次前一日）在恢复后仍可查。
        self._beta_value_by_date = {
            str(point.get("date")): float(point.get("beta_value") or 0.0)
            for point in equity_curve if point.get("date")
        }
        total = len(self.timeline)
        for date_str in self.timeline:
            session_index += 1
            if session_index <= start_after:
                continue
            if self.hooks.get("should_cancel") and self.hooks["should_cancel"]():
                session_index -= 1
                return self._finalize(equity_curve, used_dates, status="CANCELLED")
            used_dates.append(date_str)
            # cancel_previous=False：隔夜 NEXT_OPEN 挂单要保留到次日开盘撮合（与 BacktestEngine 一致）。
            self.account.new_day(date_str, cancel_previous=False)
            # ETF beta 腿：先撮合 ETF 的隔夜单（卖出回款优先到账，再给个股建仓用）。
            if self.etf_enabled:
                etf_bar = self._etf_bar_for(date_str)
                if etf_bar is not None:
                    beta_fills = self.account.process_bar(etf_bar, modes={"NEXT_OPEN"})
                    self._handle_beta_fills(beta_fills, etf_bar)
                    self.account.mark(self.etf_symbol, etf_bar.close)
            self._current_symbol = current_symbol
            submitted_signal = False
            if self._round_just_closed:
                # 前一交易日轮次闭合：清掉持有标的，今日收盘重新选股（rotation v2 语义）。
                current_symbol = None
                flat_wait_days = 0
                self._round_just_closed = False
            if current_symbol is not None:
                self.strategy.start_day(False)
                bars = self._bars_for(current_symbol)
                # O(1) 取当日日线（_bars_for 已建 {date: bar}），替代逐元素线性查找。
                bar = self._held_bar_by_date.get(date_str)
                if bar is not None:
                    # 日线 bar 只负责隔夜 NEXT_OPEN 撮合（漏斗建仓/加仓/清仓信号次开执行，
                    # 保留漏斗原有建仓语义）；随后 5 分钟 bar 走 NEXT_BAR 做日内 T。
                    fills = self.account.process_bar(bar, modes={"NEXT_OPEN"})
                    self._handle_fills(fills, session_index, bar)
                    self.account.cancel_open_orders("T+1开盘未成交取消", modes={"NEXT_OPEN"})
                    # 「截至当日」的日线历史：bars 升序，用 bisect 一次切到当日（含），
                    # 替代每根 5 分钟 bar 都对全量 bars 的线性重过滤。切片含当日日线，
                    # 与旧 `item.date <= date_str` 完全一致（on_daily 用 history[-1]=当日）。
                    history = bars[: bisect_right(self._held_dates, date_str)]
                    board = self.board_by_code.get(current_symbol, "")
                    # 指数历史同样一次切片到当日（含）：用预计算的升序 date 数组 bisect，
                    # 替代旧逐元素 .timestamp.date().isoformat() <= date_str 过滤（等价，
                    # 因 Bar.date 即 timestamp.date().isoformat() 的缓存值）。
                    index_dates = self._index_dates.get(board, [])
                    index_bars = self.index_history.get(board, [])
                    index_history = index_bars[: bisect_right(index_dates, date_str)]
                    # 5 分钟做 T：逐根喂 on_intraday（信号下一 5 分钟 bar 开盘撮合）。
                    m5_today = intraday_by_date.get(date_str, [])
                    if m5_today:
                        # 做 T 准入的振幅/换手：用「当日至今累计」而非当日全天值，避免未来函数。
                        # 盘中逐根决策时点只能看到已收盘 5 分钟 bar，全天振幅/全天换手在盘中
                        # 不可得（属未来数据）。因此随每根已收盘 m5 累积：
                        #   累计振幅 cum_amp = (当日至今 running_high - running_low) / prev_close；
                        #   累计换手 cum_turnover = 当日至今累计成交量 / 流通股本，
                        # 流通股本由当日日线反推（shares_out ≈ bar.volume / daily_turn，日线
                        # 收盘后可得口径），当天固定、只让分子（累计量）随盘中推进长大。
                        # ⚠️ daily_amplitude/daily_turnover_rate 两个入参名沿用旧名（on_daily
                        # 日线路径传的是真正的当日全天值，收盘后合法），此处盘中传的是累计值，
                        # 语义见 strategy._t_eligible 注释。
                        prev_close = bar.prev_close or bar.open
                        daily_turn = bar.turnover_rate
                        shares_out = (bar.volume / daily_turn) if daily_turn else None
                        running_high = running_low = None
                        cum_volume = 0.0
                        recent: list[Bar] = []
                        for m5_index, m5 in enumerate(m5_today):
                            m5_fills = self.account.process_bar(m5, modes={"NEXT_BAR"})
                            self._handle_fills(m5_fills, session_index, m5)
                            if self._round_just_closed:
                                # 清仓当日不再建仓：轮动语义是「清仓→次日收盘重筛」，
                                # 否则同日剩余 5 分钟 bar 会让策略立刻重新建仓，
                                # 与次日清空 current_symbol 的换票逻辑冲突（持仓失去管理）。
                                self.events.append({
                                    "date": m5.date, "type": "ENTRY_DEFERRED",
                                    "symbol": current_symbol,
                                    "reason": "清仓当日不再建仓，次日收盘重新选股",
                                })
                                break
                            self.account.mark(current_symbol, m5.close)
                            recent.append(m5)
                            # 随已收盘 m5 累积当日至今的 running_high/low 与累计成交量。
                            running_high = m5.high if running_high is None else max(running_high, m5.high)
                            running_low = m5.low if running_low is None else min(running_low, m5.low)
                            cum_volume += m5.volume
                            cum_amp = ((running_high - running_low) / prev_close) if prev_close else None
                            # 流通股本可反推时用「累计量/流通股本」；否则降级为 None，
                            # 让 _t_eligible 走「turnover is None → 不做T」路径（绝不退回全天值）。
                            cum_turnover = (cum_volume / shares_out) if shares_out else None
                            m5_requests = self.strategy.on_intraday(
                                history[:-1], recent, m5, self.account,
                                index_history=index_history, session_index=session_index,
                                daily_amplitude=cum_amp, daily_turnover_rate=cum_turnover,
                            )
                            self._submit(m5_requests, m5.timestamp)
                        # 日终撤未成交的日内单（下一 5 分钟 bar 开盘模式），与 BacktestEngine 一致。
                        self.account.cancel_open_orders("日内订单日终取消", modes={"NEXT_BAR"})
                        t_trade_count += len(recent)
                    self.account.mark(current_symbol, bar.close)
                    self._update_round(bar, session_index)
                    if not self._round_just_closed:
                        requests = self.strategy.on_daily(
                            history, index_history, self.account, session_index, bool(m5_today),
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
            # 个股空仓（不含 ETF beta 腿）：ETF 持有期间仍要每天跑筛选，选到个股后换仓。
            flat = not self.account.has_stock_position
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
                # ETF beta 腿持仓期间不算空仓：个股空仓但 beta 被持有 → 轨迹记
                # BETA（「ETF持仓」覆盖），不记 WAITING。筛选照跑（选到个股仍记 ROUND）。
                beta_held = self.etf_enabled and self._beta_qty() > 0
                snapshot = self._screen(date_str)
                first_code = snapshot["first_code"]
                # 复用本次选股结果里 first 的 Chg20D（与漏斗口径一致）
                chg20d_at_select = snapshot["first_chg20d"]
                trail_item = {
                    "round": None, "symbol": first_code or None,
                    "name": snapshot["first_name"], "board": (
                        self.board_by_code.get(first_code, "") if first_code else ""
                    ),
                    "select_date": snapshot["as_of"], "entry_date": None,
                    "exit_date": None, "pnl": None,
                    "chg20d_at_select": chg20d_at_select,
                    "gate_open": snapshot["gate_open"], "status": "WAITING",
                    # 逐轮决策日志：本轮为什么开门/关门/空仓/旁路几只，挂在对应选股日。
                    "screen_warnings": snapshot["screen_warnings"],
                    # 漏斗计数（universe→L0→L1→L2→L3→eligible），供展开行显示 narrowing。
                    "funnel": snapshot["funnel"],
                    # 各板块 ret20 / 开门状态，解释「为什么空仓」；空仓日同样保留。
                    "gate": snapshot["gate"],
                }
                if beta_held:
                    trail_item["beta_symbol"] = self.etf_symbol
                if first_code:
                    # 候选前三（按 Chg20D 升序）只在真正选中标的时记录，空仓日不存。
                    trail_item["candidates"] = snapshot["candidates"]
                self._trail.append(trail_item)
                if first_code:
                    current_symbol = first_code
                    flat_wait_days = 0
                    # 换票：重载新标的的 5 分钟序列（按日分组），供做 T 逐根处理
                    intraday_by_date = self._intraday_by_date(first_code)
                    self._chg20d_at_select[first_code] = self._trail[-1]["chg20d_at_select"]
                    self._screen_by_symbol[first_code] = snapshot
                    self._trail[-1]["status"] = "OPEN"
                    self.events.append({
                        "date": date_str, "type": "SELECT", "symbol": first_code,
                        "note": "选中标的，次一交易日开盘建仓",
                    })
                elif beta_held:
                    # 个股空仓但 ETF beta 腿被持有：覆盖日，不算空仓。
                    self._trail[-1]["status"] = "BETA"
                    self.events.append({
                        "date": date_str, "type": "BETA_HOLD",
                        "reason": "个股空仓、ETF beta 腿持有（不算空仓）",
                    })
                else:
                    self.events.append({
                        "date": date_str, "type": "WAIT",
                        "reason": "L0 大盘门关闭" if not snapshot["gate_open"] else "L0–L4 后无候选，空仓等待",
                    })
            # ETF beta 腿收盘决策：指数趋势向上→持有/建仓；互斥模式下选到个股→卖 ETF 换股，
            # 并存模式（etf_beta_coexist）下 ETF 与个股同存，只按可用现金封顶建仓额度。
            self._current_symbol = current_symbol
            self._manage_beta(date_str)
            # 闲置资金逆回购：盘后结算，收益进现金（自然流入每月盈亏 / 总收益）。
            # 按资金实际占用自然日计息（贴合真实国债逆回购）：T 日收盘下单 1 天期，
            # 首次交收日 = T 的下一个交易日（含），到期交收日 = 再下一个交易日（不含），
            # 占款天数 = 到期交收日 − 首次交收日。周四下单 → 首次交收周五、到期下周一，
            # 占款 3 天（跨周末）；周五下单 → 首次交收/到期都在下周一，占款 1 天。
            _ti = bisect_right(self.timeline, date_str)
            _t1 = self.timeline[_ti] if _ti < len(self.timeline) else None       # 首次交收日
            _t2 = self.timeline[_ti + 1] if _ti + 1 < len(self.timeline) else None  # 到期交收日
            repo_calendar_days = 1
            if _t1 and _t2:
                try:
                    repo_calendar_days = max(1, (
                        datetime.fromisoformat(_t2) - datetime.fromisoformat(_t1)
                    ).days)
                except ValueError:
                    repo_calendar_days = 1
            repo_idle = float(self.account.equity) - float(self.account.market_value)  # 计提前闲置
            repo_income, repo_idle = apply_repo_income(
                self.account, self.config, date_str, calendar_days=repo_calendar_days,
                notional=repo_idle)
            repo_rate = repo_rate_for_date(date_str, self.config) if repo_income else 0.0
            # 净值曲线点
            held = current_symbol if current_symbol else None
            position = self.account.position(held) if held else None
            beta_position = self._beta_position()
            beta_value = money(beta_position.beta_qty * beta_position.last_price) if beta_position else 0.0
            equity_curve.append({
                "date": date_str, "equity": self.account.equity, "cash": self.account.cash,
                "market_value": self.account.market_value,
                "invested": position.permanent_cost if position else 0.0,
                "drawdown": 0.0,
                "position_qty": position.total_qty if position else 0,
                "symbol": held,
                "beta_qty": beta_position.beta_qty if beta_position else 0,
                "beta_value": beta_value,
                "beta_symbol": self.etf_symbol if (beta_position and beta_position.beta_qty) else None,
                # 闲置资金逆回购（盘后结算）：有计提才带字段，旧报告 / 关闭时无此键，
                # 明细端点靠「repo_income 是否有值」过滤出真正的计提日。
                "repo_income": repo_income,
                "repo_rate": repo_rate,
                "repo_idle": repo_idle,
            })
            # 供后续轮次闭合时查「轮次前一日」的 beta 市值基准（O(1)，不扫曲线）。
            self._beta_value_by_date[date_str] = beta_value
            if self.hooks.get("on_progress"):
                self.hooks["on_progress"](len(used_dates), total, date_str, self._engine_snapshot(
                    session_index, date_str, equity_curve, used_dates,
                    current_symbol=current_symbol, flat_wait_days=flat_wait_days,
                    intraday_by_date_keys=list(intraday_by_date.keys()),
                    t_trade_count=t_trade_count,
                ))

        return self._finalize(equity_curve, used_dates, status="COMPLETED")

    def _engine_snapshot(self, cursor_index: int, cursor_date: str,
                         equity_curve: list[dict[str, Any]], used_dates: list[str], *,
                         current_symbol: str | None, flat_wait_days: int,
                         intraday_by_date_keys: list[str], t_trade_count: int) -> dict[str, Any]:
        """引擎可变状态快照（账户/策略状态由任务层另存）。供断点续传写检查点。"""
        return {
            "cursor_index": cursor_index, "cursor_date": cursor_date,
            "equity_curve": list(equity_curve), "used_dates": list(used_dates),
            "chg20d_at_select": self._chg20d_at_select,
            "screen_by_symbol": self._screen_by_symbol, "trail": self._trail,
            "round_just_closed": self._round_just_closed,
            "rounds": self.rounds, "current_round": self.current_round,
            "events": self.events, "warnings": self.warnings,
            "max_invested": self.max_invested, "max_float_loss": self.max_float_loss,
            "current_symbol": current_symbol, "flat_wait_days": flat_wait_days,
            "intraday_by_date_keys": intraday_by_date_keys,
            "t_trade_count": t_trade_count,
            "beta_fills": self._beta_fills, "beta_entry_date": self._beta_entry_date,
            "beta_last_exit_reason": self._beta_last_exit_reason,
        }

    def _finalize(self, equity_curve: list[dict[str, Any]], used_dates: list[str],
                  status: str) -> dict[str, Any]:
        """收尾并生成报告。CANCELLED 保留未闭合轮次为 OPEN、不强行结束，用于断点续传。"""
        cancelled = status == "CANCELLED"
        if self.current_round:
            item = self.current_round
            item["pnl"] = money(self.account.equity - item["start_equity"])
            item["close_reason"] = None
            self._decompose_round(item, used_dates[-1] if used_dates else None)
            item.pop("_deep_loss_start_session", None)
            self.rounds.append(item)
            self.current_round = None
        if not cancelled:
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
            total_withdrawn=self.account.total_withdrawn,
            withdrawn_count=len(self.account.withdrawals),
            withdrawals=[item.to_dict() for item in self.account.withdrawals],
            buy_hold_benchmarks=self._compute_buy_hold_benchmarks(),
        )
        screening_count = len(self._trail)
        beta = self._beta_summary()
        if beta["enabled"]:
            metrics["beta_pnl"] = beta["pnl"]
            metrics["beta_trade_count"] = beta["trade_count"]
            # ETF 交易回合胜率（按 beta 腿自己的每笔平仓 realized_pnl 判胜负）。
            # 与选股胜率（按轮次 stock_pnl）口径不同、分母不同，前端并列、各自标注。
            metrics["beta_win_rate"] = beta.get("win_rate")
            metrics["beta_sell_rounds"] = beta.get("sell_count")
            metrics["beta_sell_wins"] = beta.get("sell_wins")
        data_info = {
            "mode": "funnel_rotation", "boards": list(self.screener_config.boards),
            "universe_codes": len(self.candidates), "codes_with_daily": self.codes_with_daily,
            "daily_timeframe": "daily", "intraday_rows": 0, "index_rows": sum(
                len(item) for item in self.index_history.values()
            ),
            "execution": "T日收盘选股/T+1开盘建仓；T日收盘信号/T+1开盘；无未来价",
            "start": used_dates[0] if used_dates else None, "end": used_dates[-1] if used_dates else None,
            "date_range": [self.timeline[0], self.timeline[-1]],
            "screening_count": screening_count,
            "reference_symbol": reference_symbol,
            "rotation_trail": self._trail,
            "beta": beta,
        }
        return {
            "id": str(uuid.uuid4()), "name": self.name, "status": status,
            "created_at": utc_now(), "config": {
                **self.config.to_dict(), "initial_cash": self.account.initial_cash,
                "screener": self.screener_config.to_dict(),
                "start_date": self.start_date.isoformat() if self.start_date else None,
                "end_date": self.end_date.isoformat() if self.end_date else None,
            },
            "metrics": metrics, "equity_curve": equity_curve,
            "trades": [item.to_dict() for item in self.account.trades],
            "orders": [item.to_dict() for item in self.account.orders],
            "withdrawals": [item.to_dict() for item in self.account.withdrawals],
            "rounds": self.rounds, "events": self.events,
            "warnings": list(dict.fromkeys(self.warnings)), "data_info": data_info,
        }


def run_funnel_backtest(**kwargs) -> dict[str, Any]:
    resume_state = kwargs.pop("resume_state", None)
    return FunnelRotationBacktest(**kwargs).run(resume_state=resume_state)
