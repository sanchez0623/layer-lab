"""换票轨迹（漏斗轮动回测）的读时派生：轮次成果、空仓区间合并、汇总与 CSV 导出。

背景：轨迹原始数据按「每个空仓交易日一条 WAITING」落库，线上 5 年报告 278 条里
253 条是同因空仓（91%），直接平铺成表看不出策略节奏。这里在读取时做三件事：

1. 用报告里已有的 ``rounds`` 派生轮次收益 / 持有交易日 / 建仓延迟 / 退出原因
   （不回写数据库，旧报告同样生效）；
2. 把连续同因的空仓合并成区间（前端表格与分页基于合并后的列表）；
3. 聚合成汇总指标（筛选 / 轮次 / 空仓 / 利用率 / 胜率 / 最长空仓 / 退出原因）。

全部为纯函数，输入输出都是普通 dict/list，口径集中在本模块一处，便于单测。
"""
from __future__ import annotations

from typing import Any

#: 轨迹项类型：轮次 / 空仓（原始逐日）/ ETF beta 腿持仓（原始逐日）。
KIND_ROUND = "ROUND"
KIND_WAITING = "WAITING"
KIND_BETA = "BETA"
#: 合并后的空仓区间 / ETF 持仓区间。
KIND_WAITING_SPAN = "WAITING_SPAN"
KIND_BETA_SPAN = "BETA_SPAN"

_WAITING_STATUS = "WAITING"
_BETA_STATUS = "BETA"

#: CSV 导出固定列（顺序即表头顺序）。数值为空时写空串，便于 Excel 透视。
TRAIL_CSV_FIELDS = [
    "kind", "round", "select_date", "entry_date", "exit_date", "symbol", "name", "board",
    "chg20d_at_select", "gate_open", "reason", "bottleneck",
    "after_l0", "after_l1", "after_l2", "after_l3", "eligible",
    "pnl", "return_rate", "holding_days", "entry_delay_days",
    "max_invested", "t_net_profit", "stock_pnl", "beta_pnl", "withdraw_in_round",
    "close_reason",
    "beta_symbol",
]


def funnel_bottleneck(funnel: dict[str, Any] | None) -> str | None:
    """空仓时漏斗卡在哪一层：返回 "L1" / "L2" / "L3" / "L4"；正常走完返回 None。

    口径与 screener 的 funnel 计数一致：依次检查 after_l1 → after_l2 → after_l3 →
    eligible，第一个为 0 的层即瓶颈。L0 关门不走这里（由 ``gate_open`` 单独表达）。
    """
    if not funnel:
        return None
    for stage, key in (("L1", "after_l1"), ("L2", "after_l2"), ("L3", "after_l3"), ("L4", "eligible")):
        value = funnel.get(key)
        if value is None:
            continue
        try:
            if int(value) <= 0:
                return stage
        except (TypeError, ValueError):
            continue
    return None


def _trading_index(trading_dates: list[str]) -> dict[str, int]:
    return {str(day): position for position, day in enumerate(trading_dates or [])}


def _round_return_rate(round_row: dict[str, Any]) -> float | None:
    """轮次收益率 = 净盈亏 / 峰值投入（资金效率口径）；峰值投入缺失时回退期初权益。"""
    pnl = round_row.get("pnl")
    if pnl is None:
        return None
    try:
        pnl = float(pnl)
    except (TypeError, ValueError):
        return None
    base = round_row.get("max_invested")
    if base in (None, 0, 0.0):
        base = round_row.get("start_equity")
    try:
        base = float(base)
    except (TypeError, ValueError):
        return None
    if base <= 0:
        return None
    return pnl / base


def _trading_span(index: dict[str, int], start: str | None, end: str | None, *, inclusive: bool) -> int | None:
    """按交易日序号算跨度；日期缺失或不在净值曲线上返回 None。

    ``inclusive=True`` 用于持仓天数（含首尾交易日）；``inclusive=False`` 用于
    建仓延迟（选股日 → 建仓日相差几个交易日）。
    """
    if not start or not end:
        return None
    start_position = index.get(str(start))
    end_position = index.get(str(end))
    if start_position is None or end_position is None:
        return None
    span = end_position - start_position
    return span + 1 if inclusive else span


def enrich_trail(
    items: list[dict[str, Any]] | None,
    rounds: list[dict[str, Any]] | None,
    trading_dates: list[str] | None,
) -> list[dict[str, Any]]:
    """把原始轨迹项补齐成可复盘的轮次行。

    - ``kind``：WAITING / BETA 之外一律 ROUND（含「选中但未建仓」的 OPEN）；
    - 轮次成果从 ``rounds``（按 round id 关联）补齐：``close_reason`` /
      ``max_invested`` / ``t_net_profit`` / ``stock_pnl`` / ``beta_pnl`` /
      ``withdraw_in_round`` / ``return_rate``；
    - ``holding_days``：建仓 → 出场之间的交易日数（含首尾）；BETA 逐日项为 1；
    - ``entry_delay_days``：选股 → 建仓之间的交易日数；
    - WAITING 行补 ``reason``（gate_closed / no_candidate）与 ``bottleneck``。

    不改入参；旧报告缺字段时优雅降级（不抛错）。
    """
    rounds_by_id = {
        str(row.get("id")): row for row in (rounds or []) if row.get("id") is not None
    }
    index = _trading_index(trading_dates or [])
    enriched: list[dict[str, Any]] = []
    for item in items or []:
        entry = dict(item)
        status = str(entry.get("status") or "").upper()
        if status == _WAITING_STATUS:
            entry["kind"] = KIND_WAITING
        elif status == _BETA_STATUS:
            entry["kind"] = KIND_BETA
        else:
            entry["kind"] = KIND_ROUND
        round_row = rounds_by_id.get(str(entry.get("round"))) if entry.get("round") else None
        if round_row:
            if not entry.get("entry_date"):
                entry["entry_date"] = round_row.get("start_date")
            if not entry.get("exit_date"):
                entry["exit_date"] = round_row.get("end_date")
            if entry.get("pnl") is None:
                entry["pnl"] = round_row.get("pnl")
            entry["close_reason"] = round_row.get("close_reason")
            entry["max_invested"] = round_row.get("max_invested")
            entry["t_net_profit"] = round_row.get("t_net_profit")
            # 轮次盈亏分腿（个股腿 / ETF 腿 / 轮内提现）：新报告闭合时写入，
            # 旧报告由 main._decompose_report_rounds 读时离线补齐后透传。
            entry["stock_pnl"] = round_row.get("stock_pnl")
            entry["beta_pnl"] = round_row.get("beta_pnl")
            entry["withdraw_in_round"] = round_row.get("withdraw_in_round")
            entry["return_rate"] = _round_return_rate(round_row)
            _apply_close_diagnostic(entry, round_row)
        else:
            entry.setdefault("close_reason", None)
            entry["return_rate"] = None
        entry["holding_days"] = _trading_span(
            index, entry.get("entry_date"), entry.get("exit_date"), inclusive=True,
        )
        if entry["kind"] == KIND_BETA:
            # ETF 持仓逐日项：单日覆盖；收益率依赖区间级 beta 账本，逐日不摊。
            entry["holding_days"] = 1
        entry["entry_delay_days"] = _trading_span(
            index, entry.get("select_date"), entry.get("entry_date"), inclusive=False,
        )
        if entry["kind"] == KIND_WAITING:
            entry["reason"] = "gate_closed" if not entry.get("gate_open") else "no_candidate"
            entry["bottleneck"] = funnel_bottleneck(entry.get("funnel"))
        enriched.append(entry)
    return enriched


def _waiting_signature(item: dict[str, Any]) -> tuple[bool, str, str | None]:
    """连续空仓合并的判定键：门状态 + 空仓原因 + 漏斗瓶颈。

    原始轨迹（旧报告）没有 ``reason`` 字段，默认值必须与区间行的推导口径一致，
    否则「首日无 reason、区间行有 reason」会被误判为不同原因而拆散区间。
    """
    gate_open = bool(item.get("gate_open"))
    reason = str(item.get("reason") or ("gate_closed" if not gate_open else "no_candidate"))
    return (gate_open, reason, item.get("bottleneck"))


def _beta_signature(item: dict[str, Any]) -> tuple[str | None, bool]:
    """连续 ETF 持仓合并的判定键：ETF 标的 + 是否开门（保留门状态展示）。"""
    return (str(item.get("beta_symbol") or ""), bool(item.get("gate_open")))


def collapse_waiting_spans(
    items: list[dict[str, Any]] | None,
    beta: dict[str, Any] | None = None,
    beta_values: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """把连续同因的空仓日合并成区间行（轮次行原样保留，不跨轮次合并）。

    区间行字段：``kind=WAITING_SPAN`` / ``start_date`` / ``end_date`` / ``days`` /
    ``gate_open`` / ``reason`` / ``bottleneck`` / ``funnel``（末日口径）/ ``gate``（末日）/
    ``screen_warnings``（去重）。``select_date`` 保留为区间起始日，兼容旧前端排序。

    ETF beta 持仓项（``kind=BETA``）会打断空仓区间（像 ROUND 那样），并把连续
    持仓日合并成 ``kind=BETA_SPAN`` 区间（``days`` / ``start_date`` / ``end_date`` /
    ``beta_symbol`` / ``funnel`` / ``gate``），避免逐日刷屏。``beta``（报告 data_info
    里的 beta 汇总，含逐笔 ``fills``）传入时给 BETA_SPAN 补「净盈亏 / 本轮收益率 /
    区间投入」，补齐用户看到的四个空列（持仓区间 / 持有交易日 / 本轮收益率 / 净盈亏）。

    ``beta_values`` 为 equity_curve 的 ``{date: beta_value}`` 映射；传入时 BETA_SPAN
    改用资本账户口径（见 :func:`_beta_span_outcome`），不再只算已实现 pnl。旧调用 /
    旧报告不传时保持原来的「仅已实现 pnl」行为，向后兼容。
    """
    result: list[dict[str, Any]] = []
    current_waiting: dict[str, Any] | None = None
    current_beta: dict[str, Any] | None = None

    def flush_waiting() -> None:
        nonlocal current_waiting
        if current_waiting is not None:
            result.append(current_waiting)
            current_waiting = None

    def flush_beta() -> None:
        nonlocal current_beta
        if current_beta is not None:
            _beta_span_outcome(current_beta, beta, beta_values)
            result.append(current_beta)
            current_beta = None

    for item in items or []:
        kind = item.get("kind")
        if kind == KIND_WAITING:
            flush_beta()
            signature = _waiting_signature(item)
            if current_waiting is not None and _waiting_signature(current_waiting) == signature:
                current_waiting["end_date"] = item.get("select_date") or current_waiting["end_date"]
                current_waiting["days"] += 1
                current_waiting["funnel"] = dict(item.get("funnel") or {})
                if item.get("gate") is not None:
                    current_waiting["gate"] = item.get("gate")
                for warning in item.get("screen_warnings") or []:
                    if warning not in current_waiting["screen_warnings"]:
                        current_waiting["screen_warnings"].append(warning)
                continue
            flush_waiting()
            current_waiting = {
                "kind": KIND_WAITING_SPAN,
                "status": _WAITING_STATUS,
                "round": None,
                "symbol": None,
                "name": "",
                "select_date": item.get("select_date"),
                "start_date": item.get("select_date"),
                "end_date": item.get("select_date"),
                "days": 1,
                "gate_open": bool(item.get("gate_open")),
                "reason": str(item.get("reason") or ("gate_closed" if not item.get("gate_open") else "no_candidate")),
                "bottleneck": item.get("bottleneck"),
                "funnel": dict(item.get("funnel") or {}),
                "gate": item.get("gate"),
                "screen_warnings": list(dict.fromkeys(item.get("screen_warnings") or [])),
            }
        elif kind == KIND_BETA:
            flush_waiting()
            signature = _beta_signature(item)
            if current_beta is not None and _beta_signature(current_beta) == signature:
                current_beta["end_date"] = item.get("select_date") or current_beta["end_date"]
                current_beta["days"] += 1
                current_beta["holding_days"] = current_beta.get("holding_days", 1) + 1
                current_beta["funnel"] = dict(item.get("funnel") or {})
                if item.get("gate") is not None:
                    current_beta["gate"] = item.get("gate")
                for warning in item.get("screen_warnings") or []:
                    if warning not in current_beta["screen_warnings"]:
                        current_beta["screen_warnings"].append(warning)
                continue
            flush_beta()
            current_beta = {
                "kind": KIND_BETA_SPAN,
                "status": _BETA_STATUS,
                "round": None,
                "symbol": None,
                "name": "",
                "select_date": item.get("select_date"),
                "start_date": item.get("select_date"),
                "end_date": item.get("select_date"),
                "days": 1,
                "gate_open": bool(item.get("gate_open")),
                "beta_symbol": item.get("beta_symbol"),
                "holding_days": 1,
                "funnel": dict(item.get("funnel") or {}),
                "gate": item.get("gate"),
                "screen_warnings": list(dict.fromkeys(item.get("screen_warnings") or [])),
            }
        else:
            flush_waiting()
            flush_beta()
            result.append(dict(item))
    flush_waiting()
    flush_beta()
    return result


def _apply_close_diagnostic(entry: dict[str, Any], round_row: dict[str, Any]) -> None:
    """把轮次的 ``close_diagnostic``（止盈执行诊断）摊平成 ``close_*`` 字段挂到轨迹行。

    后端在轮次闭合时把诊断存成嵌套 dict（``round.close_diagnostic``），前端要的是
    扁平字段（``close_signal_price`` 等）。这里做一层读时映射，旧报告无诊断时全部
    留空、优雅降级。``close_round_return`` 优先用 ``_round_return_rate``（带峰值投入
    缺失时回退期初权益的兜底），与轮次行 ``return_rate`` 同口径。
    """
    diagnostic = round_row.get("close_diagnostic")
    if not isinstance(diagnostic, dict):
        return
    entry["close_signal_trigger"] = diagnostic.get("trigger")
    entry["close_signal_price"] = diagnostic.get("signal_price")
    entry["close_signal_time"] = diagnostic.get("signal_time")
    entry["close_signal_return"] = diagnostic.get("price_return")
    entry["close_fill_price"] = diagnostic.get("fill_price")
    entry["close_fill_time"] = diagnostic.get("fill_time")
    entry["close_execution_gap"] = diagnostic.get("execution_gap")
    entry["close_round_return"] = _round_return_rate(round_row) or diagnostic.get("round_return")
    entry["close_return_gap"] = diagnostic.get("return_gap")


def _beta_span_outcome(
    span: dict[str, Any],
    beta: dict[str, Any] | None,
    beta_values: dict[str, float] | None = None,
) -> None:
    """给合并后的 ETF beta 持仓区间行（``kind=BETA_SPAN``）补上成果字段。

    用户看到的空列：持仓区间 / 持有交易日 / 本轮收益率 / 净盈亏。前三者区间合并时
    已有（``start_date``/``end_date``/``days``/``holding_days``），这里补「净盈亏」
    与「本轮收益率」。两种口径：

    **资本账户法**（传入 ``beta_values``，即 equity_curve 的 ``{date: beta_value}``）：
    把区间当成一个 mini 账户，按资金进出核算::

        pnl = 段末 beta_value + Σ区间内卖出所得 − 段初 beta_value − Σ区间内买入成本

    其中「段初」取 ``start_date`` **前一交易日**收盘的 beta_value（建仓段前一日
    beta_qty=0 → 0；找不到则回退 0，避免把首日买入重复计入），「段末」取
    ``end_date`` 收盘 beta_value；买入成本 = Σ(price×qty + fee)，卖出所得 =
    Σ(price×qty − fee)。这样段内加仓的本金会被扣掉、不会冒充收益；段内只买入
    未卖出也能算出浮盈；纯持有无成交则退化为「末值 − 初值」。同一段连续持仓被
    门状态切成多段时，各段 pnl 可加总闭合。

    收益率分母 = 段初市值 + 区间买入成本（期初参与资本，含费）；``beta_invested``
    同步改为这个分母（资金效率口径），不再只是「区间买入成交额」。分母 > 0 时
    才给 ``return_rate``，否则留空。

    **旧的已实现口径**（未传 ``beta_values``，向后兼容旧调用 / 旧报告）：用落在区间
    内的 beta 卖出成交汇总 ``realized_pnl``，除以区间买入成交额；``beta.fills`` 缺失
    时回退整个 beta 腿的汇总 pnl（仅含 ``last_exit_date`` 的那一段），收益率留空。
    """
    start = str(span.get("start_date") or "")
    end = str(span.get("end_date") or start)
    fills = [
        item for item in ((beta or {}).get("fills") or [])
        if start <= str(item.get("date") or "")[:10] <= end
    ]
    buys = [item for item in fills if item.get("side") == "BUY"]
    sells = [item for item in fills if item.get("side") == "SELL"]

    if beta_values:
        # 段初取 start_date 的前一交易日收盘市值：建仓段前一日尚未持有（0），
        # 若改用 start_date 当日收盘值会把当日买入重复计入。
        previous = max((day for day in beta_values if str(day) < start), default=None)
        start_value = float(beta_values.get(previous) or 0.0) if previous else 0.0
        end_value = float(beta_values.get(end) or 0.0)
        buy_cost = sum(
            float(item.get("price", 0.0)) * int(item.get("quantity", 0))
            + float(item.get("fee", 0.0) or 0.0)
            for item in buys
        )
        sell_proceeds = sum(
            float(item.get("price", 0.0)) * int(item.get("quantity", 0))
            - float(item.get("fee", 0.0) or 0.0)
            for item in sells
        )
        pnl = sell_proceeds + end_value - start_value - buy_cost
        invested = start_value + buy_cost
        span["beta_invested"] = round(invested, 2)
        span["pnl"] = round(pnl, 2)
        span["return_rate"] = (pnl / invested) if invested > 0 else None
        span["beta_buy_count"] = len(buys)
        span["beta_sell_count"] = len(sells)
        return

    if fills:
        invested = sum(float(item.get("price", 0.0)) * int(item.get("quantity", 0)) for item in buys)
        pnl = sum(float(item.get("realized_pnl", 0.0) or 0.0) for item in sells)
        span["beta_invested"] = round(invested, 2)
        span["pnl"] = round(pnl, 2)
        span["return_rate"] = (pnl / invested) if invested > 0 else None
        span["beta_buy_count"] = len(buys)
        span["beta_sell_count"] = len(sells)
    else:
        # 旧报告没有逐笔 fills：优雅降级。整腿盈亏只在「beta 腿实际清仓的那一段」
        # （含 beta.last_exit_date 的区间）挂上——多段持仓时把汇总 pnl 复制到每一段
        # 会让人误以为每段都赚了那么多。无 last_exit_date（ beta 仍持仓 / 数据缺失）
        # 时一律留空。收益率无投入分母，一并留空。
        span["beta_invested"] = None
        span["pnl"] = None
        span["return_rate"] = None
        last_exit = str((beta or {}).get("last_exit_date") or "")
        overall = (beta or {}).get("pnl")
        if last_exit and overall is not None and start <= last_exit <= end:
            span["pnl"] = float(overall)


def _round_digest(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "round": item.get("round"),
        "symbol": item.get("symbol"),
        "name": item.get("name"),
        "pnl": item.get("pnl"),
        "return_rate": item.get("return_rate"),
        "entry_date": item.get("entry_date"),
        "exit_date": item.get("exit_date"),
        "holding_days": item.get("holding_days"),
        "close_reason": item.get("close_reason"),
    }


def _longest_wait(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    spans = [span for span in collapse_waiting_spans(items) if span.get("kind") == KIND_WAITING_SPAN]
    if not spans:
        return None
    longest = max(spans, key=lambda span: int(span.get("days") or 0))
    return {
        "start_date": longest.get("start_date"),
        "end_date": longest.get("end_date"),
        "days": int(longest.get("days") or 0),
        "gate_open": bool(longest.get("gate_open")),
        "reason": longest.get("reason"),
        "bottleneck": longest.get("bottleneck"),
    }


def aggregate_trail(
    items: list[dict[str, Any]] | None, trading_days: int = 0,
    beta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """汇总指标：筛选/轮次/空仓构成/持仓利用率/胜率/收益/最长空仓/退出原因。

    ``items`` 为 :func:`enrich_trail` 的输出（未合并的逐日轨迹）。全部口径在此集中：
    - ``waiting_ratio`` = 空仓筛选日 / 筛选总数（ETF beta 持仓日**不算空仓**）；
    - ``holding_ratio`` = 已结束轮次持仓交易日之和 / 回测交易日数；
    - ``covered_ratio`` = （轮次持仓 + ETF 持仓）交易日 / 回测交易日数；
    - ``beta_holding_days`` / ``beta_pnl``：ETF beta 腿持仓天数与已实现盈亏
      （``beta_pnl`` 优先取 ``beta`` 汇总的 pnl，缺失时回退逐日项的 ``beta_pnl``）；
    - 胜率/平均收益只统计 CLOSED 且有 pnl 的轮次。
    """
    rows = list(items or [])
    screens = len(rows)
    waiting = [item for item in rows if item.get("kind") == KIND_WAITING]
    rounds = [item for item in rows if item.get("kind") == KIND_ROUND]
    beta_rows = [item for item in rows if item.get("kind") == KIND_BETA]
    closed = [
        item for item in rounds
        if str(item.get("status") or "").upper() == "CLOSED" and item.get("pnl") is not None
    ]
    wins = [item for item in closed if float(item["pnl"]) > 0]
    returns = [
        float(item["return_rate"]) for item in closed
        if item.get("return_rate") is not None
    ]
    holding_days = sum(int(item.get("holding_days") or 0) for item in closed)
    beta_holding_days = len(beta_rows)
    beta_pnl = None
    if beta is not None and beta.get("pnl") is not None:
        beta_pnl = float(beta.get("pnl"))
    elif beta_rows:
        item_pnls = [
            float(item["beta_pnl"]) for item in beta_rows
            if item.get("beta_pnl") is not None
        ]
        if item_pnls:
            beta_pnl = sum(item_pnls)
    entry_delays = [
        int(item["entry_delay_days"]) for item in rounds
        if item.get("entry_delay_days") is not None
    ]
    gate_closed = [item for item in waiting if not item.get("gate_open")]
    no_candidate = [item for item in waiting if item.get("gate_open")]
    stage_counts: dict[str, int] = {}
    for item in no_candidate:
        stage = str(item.get("bottleneck") or funnel_bottleneck(item.get("funnel")) or "unknown")
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
    exit_reasons: dict[str, int] = {}
    for item in closed:
        reason = str(item.get("close_reason") or "未记录")
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    total_days = int(trading_days or 0)
    total_pnl = round(sum(float(item["pnl"]) for item in closed), 2) if closed else 0.0
    best = max(closed, key=lambda item: float(item["pnl"])) if closed else None
    worst = min(closed, key=lambda item: float(item["pnl"])) if closed else None
    covered_days = holding_days + beta_holding_days
    return {
        "screens": screens,
        "rounds": len(rounds),
        "closed_rounds": len(closed),
        "open_rounds": len(rounds) - len(closed),
        "waiting_days": len(waiting),
        "gate_closed_days": len(gate_closed),
        "no_candidate_days": len(no_candidate),
        "no_candidate_by_stage": stage_counts,
        "waiting_ratio": len(waiting) / screens if screens else 0.0,
        "trading_days": total_days,
        "holding_days": holding_days,
        "holding_ratio": holding_days / total_days if total_days else 0.0,
        "beta_holding_days": beta_holding_days,
        "beta_pnl": beta_pnl,
        "covered_days": covered_days,
        "covered_ratio": covered_days / total_days if total_days else 0.0,
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "avg_return": (sum(returns) / len(returns)) if returns else None,
        "avg_pnl": round(total_pnl / len(closed), 2) if closed else None,
        "total_pnl": total_pnl,
        "avg_holding_days": (holding_days / len(closed)) if closed else None,
        "avg_entry_delay_days": (sum(entry_delays) / len(entry_delays)) if entry_delays else None,
        "best": _round_digest(best) if best else None,
        "worst": _round_digest(worst) if worst else None,
        "longest_wait": _longest_wait(rows),
        "exit_reasons": exit_reasons,
    }


def flatten_for_csv(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """把 enriched 轨迹摊平成固定列（原始逐日粒度，空值写空串），供 CSV 导出。"""
    rows: list[dict[str, Any]] = []
    for item in items or []:
        funnel = item.get("funnel") or {}
        rows.append({
            "kind": item.get("kind") or "",
            "round": item.get("round") or "",
            "select_date": item.get("select_date") or "",
            "entry_date": item.get("entry_date") or "",
            "exit_date": item.get("exit_date") or "",
            "symbol": item.get("symbol") or "",
            "name": item.get("name") or "",
            "board": item.get("board") or "",
            "chg20d_at_select": "" if item.get("chg20d_at_select") is None else item["chg20d_at_select"],
            "gate_open": "是" if item.get("gate_open") else "否",
            "reason": item.get("reason") or "",
            "bottleneck": item.get("bottleneck") or "",
            "after_l0": funnel.get("after_l0", ""),
            "after_l1": funnel.get("after_l1", ""),
            "after_l2": funnel.get("after_l2", ""),
            "after_l3": funnel.get("after_l3", ""),
            "eligible": funnel.get("eligible", ""),
            "pnl": "" if item.get("pnl") is None else item["pnl"],
            "return_rate": "" if item.get("return_rate") is None else item["return_rate"],
            "holding_days": "" if item.get("holding_days") is None else item["holding_days"],
            "entry_delay_days": "" if item.get("entry_delay_days") is None else item["entry_delay_days"],
            "max_invested": "" if item.get("max_invested") is None else item["max_invested"],
            "t_net_profit": "" if item.get("t_net_profit") is None else item["t_net_profit"],
            "stock_pnl": "" if item.get("stock_pnl") is None else item["stock_pnl"],
            "beta_pnl": "" if item.get("beta_pnl") is None else item["beta_pnl"],
            "withdraw_in_round": "" if item.get("withdraw_in_round") is None else item["withdraw_in_round"],
            "close_reason": item.get("close_reason") or "",
            "beta_symbol": item.get("beta_symbol") or "",
        })
    return rows
