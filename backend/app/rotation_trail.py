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

#: 轨迹项类型：轮次 / 空仓（原始逐日）。
KIND_ROUND = "ROUND"
KIND_WAITING = "WAITING"
#: 合并后的空仓区间。
KIND_WAITING_SPAN = "WAITING_SPAN"

_WAITING_STATUS = "WAITING"

#: CSV 导出固定列（顺序即表头顺序）。数值为空时写空串，便于 Excel 透视。
TRAIL_CSV_FIELDS = [
    "kind", "round", "select_date", "entry_date", "exit_date", "symbol", "name", "board",
    "chg20d_at_select", "gate_open", "reason", "bottleneck",
    "after_l0", "after_l1", "after_l2", "after_l3", "eligible",
    "pnl", "return_rate", "holding_days", "entry_delay_days",
    "max_invested", "t_net_profit", "close_reason",
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

    - ``kind``：WAITING 之外一律 ROUND（含「选中但未建仓」的 OPEN）；
    - 轮次成果从 ``rounds``（按 round id 关联）补齐：``close_reason`` /
      ``max_invested`` / ``t_net_profit`` / ``return_rate``；
    - ``holding_days``：建仓 → 出场之间的交易日数（含首尾）；
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
        entry["kind"] = KIND_WAITING if status == _WAITING_STATUS else KIND_ROUND
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
            entry["return_rate"] = _round_return_rate(round_row)
        else:
            entry.setdefault("close_reason", None)
            entry["return_rate"] = None
        entry["holding_days"] = _trading_span(
            index, entry.get("entry_date"), entry.get("exit_date"), inclusive=True,
        )
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


def collapse_waiting_spans(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """把连续同因的空仓日合并成区间行（轮次行原样保留，不跨轮次合并）。

    区间行字段：``kind=WAITING_SPAN`` / ``start_date`` / ``end_date`` / ``days`` /
    ``gate_open`` / ``reason`` / ``bottleneck`` / ``funnel``（末日口径）/ ``gate``（末日）/
    ``screen_warnings``（去重）。``select_date`` 保留为区间起始日，兼容旧前端排序。
    """
    result: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            result.append(current)
            current = None

    for item in items or []:
        if item.get("kind") != KIND_WAITING:
            flush()
            result.append(dict(item))
            continue
        signature = _waiting_signature(item)
        if current is not None and _waiting_signature(current) == signature:
            current["end_date"] = item.get("select_date") or current["end_date"]
            current["days"] += 1
            current["funnel"] = dict(item.get("funnel") or {})
            if item.get("gate") is not None:
                current["gate"] = item.get("gate")
            for warning in item.get("screen_warnings") or []:
                if warning not in current["screen_warnings"]:
                    current["screen_warnings"].append(warning)
            continue
        flush()
        current = {
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
    flush()
    return result


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


def aggregate_trail(items: list[dict[str, Any]] | None, trading_days: int = 0) -> dict[str, Any]:
    """汇总指标：筛选/轮次/空仓构成/持仓利用率/胜率/收益/最长空仓/退出原因。

    ``items`` 为 :func:`enrich_trail` 的输出（未合并的逐日轨迹）。全部口径在此集中：
    - ``waiting_ratio`` = 空仓筛选日 / 筛选总数；
    - ``holding_ratio`` = 已结束轮次持仓交易日之和 / 回测交易日数；
    - 胜率/平均收益只统计 CLOSED 且有 pnl 的轮次。
    """
    rows = list(items or [])
    screens = len(rows)
    waiting = [item for item in rows if item.get("kind") == KIND_WAITING]
    rounds = [item for item in rows if item.get("kind") == KIND_ROUND]
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
            "close_reason": item.get("close_reason") or "",
        })
    return rows
