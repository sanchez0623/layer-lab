"""池内轮动 v2：清仓后重新运行五层选股，取当时第一名。

语义（用户确认）：不是按预生成候选池的固定顺序换票，而是**每次清仓完成后重新
按 L0→L4 规则选出当时的第一名**：

1. ``apply_bar`` 检测到清仓归零时置 ``rotation_pending_advance=True``；
2. live-tick 在「auto_strategy + rotation_enabled + pending + 空仓 +
   本次 tick 标的是当前轮动标的」时处理，每个交易日最多重筛一次
   （``rotation_last_attempt_date`` 去重）；
3. 重筛结果 first 为空 → 空仓等待（L0 关门 / 无候选两种 note），pending 保留、
   次日再试；first 与当前标的相同 → 不换票，清 pending；first 不同 → 换票并
   重置策略状态；
4. 本模块只做状态机与结果解释，不碰网络：重筛由调用方以 ``runner`` 闭包注入
   （main.py 里负责调用 ScreenerService 并把结果落库）。
"""

from __future__ import annotations

from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .broker import BrokerAccount


MODE_RESCREEN = "rescreen"
NOTE_L0_CLOSED = "L0 大盘门关闭，空仓等待"
NOTE_NO_CANDIDATE = "本轮无符合条件的标的，空仓等待"
NOTE_SAME_SYMBOL = "重新建仓→{symbol}（清仓后重新选股仍为该标的）"
NOTE_SWITCH = "换票→{symbol}（重新选股）"
NOTE_FAILED = "重新选股失败，空仓等待：{error}"
NOTE_EMPTY_POOL = "轮动池为空，空仓等待"


def _candidate_summary(item: dict) -> dict:
    symbol = str(item.get("code") or item.get("symbol") or "").strip()
    return {
        "symbol": symbol,
        "name": item.get("name") or "",
        "chg20d": item.get("chg20d"),
        "board": item.get("board") or "",
        "deep_checked": bool(item.get("deep_checked", False)),
    }


def _pool_item(item: dict, position: int) -> dict:
    if not isinstance(item, dict):
        raise ValueError(f"轮动池第 {position} 项不是对象")
    symbol = str(item.get("symbol") or item.get("code") or "").strip()
    if not symbol:
        raise ValueError(f"轮动池第 {position} 项缺少 symbol")
    normalized = _candidate_summary(item)
    normalized["symbol"] = symbol
    return normalized


def candidate_pool(run: dict | None) -> list[dict]:
    """选股结果里的候选摘要列表（L4 顺序，eligible 优先）。"""
    run = run or {}
    source = run.get("eligible") or run.get("pool") or []
    entries: list[dict] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        summary = _candidate_summary(item)
        if summary["symbol"]:
            entries.append(summary)
    return entries


def screen_snapshot(run: dict | None) -> dict:
    """最近一次重筛的轻量快照（保存进账户，供前端展示）。"""
    run = run or {}
    first = run.get("first") if isinstance(run.get("first"), dict) else None
    first_summary = None
    if first is not None:
        code = str(first.get("code") or first.get("symbol") or "").strip()
        if code:
            first_summary = {
                "code": code,
                "name": first.get("name") or "",
                "chg20d": first.get("chg20d"),
            }
    gate = run.get("gate") if isinstance(run.get("gate"), dict) else {}
    gate_open = gate.get("open")
    warnings = [str(item) for item in (run.get("warnings") or [])][:3]
    return {
        "run_id": run.get("id"),
        "as_of": run.get("as_of"),
        "first": first_summary,
        "eligible_count": len(run.get("eligible") or []),
        "gate_open": bool(gate_open) if gate_open is not None else None,
        "warnings": warnings,
    }


def rotation_summary(account: "BrokerAccount") -> dict:
    """账户对外暴露的 rotation 结构（契约 §5.2 + v2 字段）。"""
    pool = [dict(item) for item in account.rotation_pool]
    index = int(account.rotation_index)
    current_symbol = pool[index].get("symbol") if 0 <= index < len(pool) else None
    return {
        "mode": MODE_RESCREEN,
        "enabled": bool(account.rotation_enabled),
        "pool": pool,
        "index": index,
        "current_symbol": current_symbol,
        "waiting": bool(account.rotation_waiting),
        "note": account.rotation_note or "",
        "run_id": account.rotation_run_id,
        "last_attempt_date": account.rotation_last_attempt_date,
        "last_run_id": account.rotation_last_run_id,
        "last_screen": dict(account.rotation_last_screen),
    }


def _current_symbol(account: "BrokerAccount") -> str | None:
    index = int(account.rotation_index)
    if 0 <= index < len(account.rotation_pool):
        return account.rotation_pool[index].get("symbol")
    return None


def _is_flat(account: "BrokerAccount") -> bool:
    return not any(position.total_qty > 0 for position in account.positions.values())


def should_rescreen(
    account: "BrokerAccount", *, today: str, current_symbol: str,
) -> bool:
    """轮动 v2 触发条件：已开轮动、本轮刚清仓、空仓、当天未筛过、tick 的是当前标的。"""
    if not account.rotation_enabled or not account.rotation_pending_advance:
        return False
    if not _is_flat(account):
        return False
    if str(account.rotation_last_attempt_date or "") == str(today or ""):
        return False
    target = _current_symbol(account)
    return bool(target) and str(current_symbol or "") == str(target)


def _reset_strategy(account: "BrokerAccount") -> None:
    """换票后新标的从零开始：策略状态、加仓序列、日线信号日期全部重置。"""
    account.strategy_state = {}
    account.sequence_index = 0
    account.last_daily_signal_date = None
    account.armed_state = "OFF"


def apply_rescreen(account: "BrokerAccount", run: dict, *, today: str | None = None) -> dict:
    """把一次选股结果应用到账户：更新快照/池，再决定「重新建仓、换票或空仓等待」。"""
    run = run or {}
    if today:
        account.rotation_last_attempt_date = today
    account.rotation_last_run_id = run.get("id")
    account.rotation_last_screen = screen_snapshot(run)
    # 先记录旧池的当前标的，再替换成最新候选池（否则序号会指向新池的同位置标的）。
    previous_symbol = _current_symbol(account)
    pool = candidate_pool(run)
    if pool:
        account.rotation_pool = pool
    first = run.get("first") if isinstance(run.get("first"), dict) else None
    first_code = str(first.get("code") or first.get("symbol") or "").strip() if first else ""
    gate = run.get("gate") if isinstance(run.get("gate"), dict) else {}
    gate_open = gate.get("open")
    if not first_code:
        account.rotation_waiting = True
        account.rotation_note = NOTE_L0_CLOSED if gate_open is False else NOTE_NO_CANDIDATE
        return {
            "rescreened": True, "advanced": False, "waiting": True,
            "symbol": None, "previous_symbol": previous_symbol,
            "run_id": run.get("id"), "gate_open": gate_open,
            "pool_size": len(account.rotation_pool), "note": account.rotation_note,
        }

    new_index = next(
        (position for position, item in enumerate(account.rotation_pool)
         if item.get("symbol") == first_code),
        -1,
    )
    if new_index < 0:
        account.rotation_pool.append(_candidate_summary({**first, "symbol": first_code}))
        new_index = len(account.rotation_pool) - 1

    if previous_symbol is not None and first_code == previous_symbol:
        # 重新选股仍是当前标的：不换票，只清掉「待换票」状态让策略重新建仓。
        account.rotation_index = new_index
        account.rotation_pending_advance = False
        account.rotation_waiting = False
        account.rotation_note = NOTE_SAME_SYMBOL.format(symbol=first_code)
        return {
            "rescreened": True, "advanced": False, "same_symbol": True,
            "symbol": first_code, "previous_symbol": previous_symbol,
            "run_id": run.get("id"), "index": new_index, "gate_open": gate_open,
            "pool_size": len(account.rotation_pool), "note": account.rotation_note,
        }

    account.rotation_index = new_index
    account.rotation_pending_advance = False
    account.rotation_waiting = False
    _reset_strategy(account)
    account.rotation_note = NOTE_SWITCH.format(symbol=first_code)
    return {
        "rescreened": True, "advanced": True, "same_symbol": False,
        "symbol": first_code, "previous_symbol": previous_symbol,
        "dataset_symbol_hint": first_code, "board": (first.get("board") or ""),
        "run_id": run.get("id"), "index": new_index, "gate_open": gate_open,
        "pool_size": len(account.rotation_pool), "note": account.rotation_note,
    }


def rescreen_and_advance(
    account: "BrokerAccount", runner: Callable[[], dict], *,
    today: str, current_symbol: str,
) -> dict | None:
    """满足触发条件时重筛一次并处理结果；返回事件字典，未触发返回 None。

    ``runner`` 是调用方注入的闭包：调用选股服务并返回 run payload（调用方负责
    落库）。本函数保证每个交易日最多调用一次 runner，失败也计入当天已尝试。
    """
    if not should_rescreen(account, today=today, current_symbol=current_symbol):
        return None
    account.rotation_last_attempt_date = today
    try:
        run = runner() or {}
    except Exception as exc:  # noqa: BLE001 - 行情/数据源异常时保持账户可用，次日再试
        account.rotation_waiting = True
        account.rotation_note = NOTE_FAILED.format(error=str(exc) or exc.__class__.__name__)
        return {
            "rescreened": False, "advanced": False, "waiting": True,
            "error": str(exc), "run_id": None, "note": account.rotation_note,
        }
    return apply_rescreen(account, run, today=today)


def set_pool(
    account: "BrokerAccount", pool: list[dict], *,
    run_id: str | None = None, enabled: bool = True,
) -> None:
    """直接指定候选池（手工建池，不携带选股配置/快照）。"""
    items = [_pool_item(item, position) for position, item in enumerate(pool or [])]
    account.rotation_pool = items
    account.rotation_enabled = bool(enabled)
    if run_id is not None:
        account.rotation_run_id = run_id
    account.rotation_index = -1
    account.rotation_waiting = False
    account.rotation_pending_advance = False
    account.rotation_note = NOTE_EMPTY_POOL if not items else ""
    held = {symbol for symbol, position in account.positions.items() if position.total_qty > 0}
    if held:
        for position, item in enumerate(items):
            if item["symbol"] in held:
                account.rotation_index = position
                account.rotation_note = f"换票→{item['symbol']}"
                break


def set_from_run(
    account: "BrokerAccount", run: dict, *, enabled: bool = True,
    note: str | None = None, current_symbol: str | None = None,
) -> None:
    """启用轮动：记录该次选股的配置与快照，池=最新候选，序号指向 first 或指定标的。"""
    run = run or {}
    config = run.get("config")
    account.rotation_config = dict(config) if isinstance(config, dict) else {}
    account.rotation_enabled = bool(enabled)
    account.rotation_run_id = run.get("id")
    account.rotation_last_run_id = run.get("id")
    account.rotation_last_attempt_date = None
    account.rotation_last_screen = screen_snapshot(run)
    pool = candidate_pool(run)
    account.rotation_pool = pool
    account.rotation_pending_advance = False
    account.rotation_waiting = False

    first = run.get("first") if isinstance(run.get("first"), dict) else None
    first_code = str(first.get("code") or first.get("symbol") or "").strip() if first else ""
    target = str(current_symbol or first_code or "").strip()
    index = next(
        (position for position, item in enumerate(pool) if item.get("symbol") == target),
        -1,
    )
    if index < 0 and target:
        account.rotation_pool.append(_candidate_summary({**(first or {}), "symbol": target}))
        index = len(account.rotation_pool) - 1
    account.rotation_index = index
    if not enabled:
        account.rotation_note = note if note is not None else ""
    elif target:
        account.rotation_note = note if note is not None else f"换票→{target}"
    else:
        gate = run.get("gate") if isinstance(run.get("gate"), dict) else {}
        account.rotation_waiting = True
        account.rotation_note = (
            NOTE_L0_CLOSED if gate.get("open") is False else NOTE_NO_CANDIDATE
        )
