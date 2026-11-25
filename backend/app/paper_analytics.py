"""模拟盘「读时派生」指标（阶段 1）：只吃账户已有流水，不新增业务表 / 历史快照。

设计约束（AGENTS 架构原则 1：单一真相源）：
- 与回测同名的字段必须同口径：``beta`` 摘要对齐回测 ``data_info.beta``；
  ``net_profit / total_return / profit_withdrawn`` 对齐回测 ``performance_metrics``
  的「盈利提现加回」口径；
- ``max_invested`` 的成本分配复用 ``broker.add_position_buy_cost`` /
  ``allocate_ledger_on_sell``（撮合 ``_apply_execution`` 用的同一份实现），
  不在模拟盘复制第二套成本账本算法。

本模块**刻意不返回**依赖逐日净值 / 逐日持仓序列的指标（留给逐日净值落库阶段）：
``max_drawdown / sharpe_ratio / volatility / calmar_ratio / annualized_return /
xirr_annualized / underwater / cashflow_adjusted / exposure / exposure_total /
exposure_stock / exposure_beta / idle_ratio / monthly / robustness /
max_float_loss / equity_curve``。这些字段若先用「把提现当亏损」或「起止点近似」的
口径定义下来，后续净值落库时会与回测同名指标冲突，故本期只交付能精确算出来的部分。
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import Counter
from typing import Any, Iterable

from .broker import BrokerAccount, add_position_buy_cost, allocate_ledger_on_sell
from .domain import Fill, money


def _fill_date(fill: Fill) -> str:
    """成交日期（YYYY-MM-DD）：与回测 beta fills 的 date 口径一致。"""
    return str(fill.timestamp)[:10]


def _replay_permanent_ledger(fills: Iterable[Fill]) -> tuple[float, float]:
    """按成交顺序重放永久仓成本账本，返回 ``(成本峰值, 期末成本)``。

    只重放 PERMANENT 账本：T 仓买入不计入永久成本、T 卖出也不改变永久成本
    （``_apply_execution`` 里 T 卖出是「永久仓减仓后又等量加回、成本原样保留」），
    BETA 腿完全独立。成本累加 / 分摊复用 ``broker.add_position_buy_cost`` 与
    ``allocate_ledger_on_sell``，与撮合是同一份实现，不复制第二套口径。
    """
    ledger: dict[str, tuple[int, float]] = {}
    peak = 0.0
    for fill in fills:
        if fill.purpose != "PERMANENT":
            continue
        qty, cost = ledger.get(fill.symbol, (0, 0.0))
        if fill.side == "BUY":
            qty += int(fill.quantity)
            cost, _ = add_position_buy_cost(
                cost, 0.0, float(fill.amount), float(fill.total_fee),
            )
        else:
            if qty <= 0:
                continue
            sold = min(int(fill.quantity), qty)
            cost_allocated, _ = allocate_ledger_on_sell(cost, 0.0, sold, qty)
            cost = money(max(0.0, cost - cost_allocated))
            qty -= sold
            if qty == 0:
                # 与 _apply_execution 的「持仓清零 → 成本归零」口径一致，避免浮点残值。
                cost = 0.0
        ledger[fill.symbol] = (qty, cost)
        peak = max(peak, sum(item[1] for item in ledger.values()))
    return money(peak), money(sum(item[1] for item in ledger.values()))


def max_invested_from_fills(fills: Iterable[Fill]) -> float:
    """按成交顺序重放永久仓成本账本，返回「永久仓成本（含买入费用）」的峰值。

    口径说明：模拟盘没有逐日快照，这里取「每笔成交后的永久仓成本」峰值；回测
    ``max_invested`` 取「每根 bar 撮合完成后的持仓成本」峰值，两者只在「同一根 bar
    内先买后卖」的瞬时峰值上有差异（模拟盘会记住买后那一刻的峰值，更保守）。
    """
    peak, _final = _replay_permanent_ledger(fills)
    return peak


def paper_account_metrics(account: BrokerAccount) -> dict[str, Any]:
    """模拟盘账户指标（只含不依赖逐日净值序列的字段）。

    提现口径与回测 ``performance_metrics`` 一致：只有「以盈利为基准」
    （``withdrawals[].period_profit > 0``）的提现才加回净利，不能把亏损月份的
    固定金额提现也算成收益。``net_profit = equity + profit_withdrawn - initial_cash``。
    """
    initial_cash = money(account.initial_cash)
    final_equity = money(account.equity)
    withdrawals = list(account.withdrawals or [])
    profit_withdrawn = money(sum(
        float(item.amount) for item in withdrawals
        if float(item.period_profit or 0.0) > 0
    ))
    net_profit = money(final_equity + profit_withdrawn - initial_cash)
    trades = list(account.trades or [])
    beta_fills = [fill for fill in trades if fill.purpose == "BETA"]
    return {
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "total_fees": money(account.total_fees),
        "t_net_profit": money(account.t_net_profit),
        "total_withdrawn": money(account.total_withdrawn),
        "withdrawn_count": len(withdrawals),
        "profit_withdrawn": profit_withdrawn,
        "net_profit": net_profit,
        "total_return": net_profit / initial_cash if initial_cash > 0 else 0.0,
        "total_trades": len(trades),
        "t_trade_count": sum(1 for fill in trades if fill.purpose == "T"),
        # ETF beta 腿：与回测 metrics.beta_pnl / data_info.beta.pnl 同口径
        # （BETA 卖出的 realized_pnl 求和）。
        "beta_pnl": money(sum(
            float(fill.realized_pnl) for fill in beta_fills if fill.side == "SELL"
        )),
        "beta_trade_count": len(beta_fills),
        "max_invested": max_invested_from_fills(trades),
    }


def _beta_exit_reason(account: BrokerAccount, last_fill: Fill) -> str | None:
    """最近一次 ETF 清仓原因：取该笔卖出成交对应委托的 reason。

    与回测 ``_beta_summary`` 同口径：回测在「买入成交」时把 ``_beta_last_exit_reason``
    清空，所以只有最后一笔 BETA 成交仍是卖出时才有出场原因；模拟盘的出场原因写在
    ``apply_beta_bar`` 提交的卖单 ``reason`` 上（Fill.order_id → Order.reason）。
    """
    for order in account.orders or []:
        if order.id == last_fill.order_id:
            return order.reason or None
    return None


def paper_beta_summary(account: BrokerAccount) -> dict[str, Any]:
    """ETF beta 腿摘要：与回测 ``data_info.beta`` 同键名同口径。

    覆盖区间字段（``source / coverage_start / coverage_end / partial_coverage``）不在
    本期范围——模拟盘没有回测的数据集元信息，强行编一个只会制造「两边数值不同」的
    误解（见审计文档 C07，P2）。
    """
    fills = [fill for fill in (account.trades or []) if fill.purpose == "BETA"]
    buys = [fill for fill in fills if fill.side == "BUY"]
    sells = [fill for fill in fills if fill.side == "SELL"]
    exit_reason = _beta_exit_reason(account, fills[-1]) if fills and fills[-1].side == "SELL" else None
    return {
        "enabled": bool(account.config.etf_beta_enabled),
        "symbol": account.config.etf_symbol,
        "trade_count": len(fills),
        "buy_count": len(buys),
        "sell_count": len(sells),
        # 与回测一致：首次建仓日（不是「本轮」建仓日）；最近一次卖出的日期即最近清仓日。
        "entry_date": _fill_date(buys[0]) if buys else None,
        "last_exit_date": _fill_date(sells[-1]) if sells else None,
        "exit_reason": exit_reason,
        "pnl": money(sum(float(fill.realized_pnl) for fill in sells)),
        "position_qty": sum(int(position.beta_qty) for position in account.positions.values()),
        # 逐笔成交（紧凑字段）：与回测 data_info.beta.fills 同键，供后续轨迹/面板复用。
        "fills": [
            {
                "date": _fill_date(fill), "side": fill.side,
                "quantity": int(fill.quantity), "price": fill.price,
                "fee": money(fill.total_fee), "realized_pnl": money(fill.realized_pnl),
            }
            for fill in fills
        ],
    }


# ---------------------------------------------------------------------------
# 轮次明细 + 三腿分拆 + 止盈执行诊断（阶段 3）
# ---------------------------------------------------------------------------
# 轮次边界口径（与回测 BacktestEngine 的轮次记账对齐）：
#   按「标的净个股持仓（PERMANENT + T，不含 BETA）」从 0 → 正 → 0 切分；
#   本轮最后一次卖出（净持仓归零那一笔）即轮次出场；当前未平的轮次用当前状态收尾
#   （status=OPEN，end_date 留 None，与回测 _finalize 对 OPEN 轮的处理一致）。
#   BETA 成交不参与切分（ETF 腿是空仓期的替补，不属于个股轮次）。
# 分腿口径直接复用回测 ``decompose_round_pnl``（单一真相源）：
#   pnl = 轮次区间权益差；stock_pnl = pnl + 轮内提现 - beta_pnl。
# ---------------------------------------------------------------------------

# 退出原因中文标签（对齐回测报告文案）。键是策略 / 委托里实际出现的 reason，
# 动态文案（最大持仓 N 日强平等）由 close_reason_label 正则兜底。
CLOSE_REASON_LABELS: dict[str, str] = {
    "价格轨止盈": "止盈（价格轨/账户轨）",
    "账户轨止盈": "止盈（价格轨/账户轨）",
    "量价背离": "Armed 量价背离（辅助清仓）",
    "Armed量价背离三选二_3根5分钟K线": "Armed 量价背离（辅助清仓）",
    "VWAP回撤": "VWAP 账户回撤（辅助清仓）",
    "VWAP账户回撤4%_3根5分钟K线": "VWAP 账户回撤（辅助清仓）",
    "移动止盈": "移动止盈（达标转trailing回撤）",
    "移动止盈回撤": "移动止盈（达标转trailing回撤）",
    "超期强平": "超期强平",
    "事件熔断": "事件熔断",
    "T+1尾盘截止平T": "T+1 尾盘强平",
    "T日尾盘截止平T": "T 日尾盘强平",
    "T净利止盈": "T 净利止盈",
    "整体止盈_先平T": "整体止盈",
    "整体止盈_永久仓": "整体止盈",
    "模拟盘清仓": "模拟盘清仓",
}

_FORCE_EXIT_PATTERN = re.compile(r"最大持仓\s*(\d+)\s*日强平")
_ROUND_STOP_PATTERN = re.compile(r"超期持仓\s*(\d+)\s*日")
_STOP_LOSS_PATTERN = re.compile(r"止损\s*(\d+)\s*%")
_TRAILING_PATTERN = re.compile(r"移动止盈回撤")


def close_reason_label(reason: str | None) -> str | None:
    """把策略 / 委托的原始退出原因映射成用户可懂的中文标签。

    未知原因原样返回（不吞信息、不抛错）；空值返回 None。
    """
    if reason is None:
        return None
    text = str(reason).strip()
    if not text:
        return None
    if text in CLOSE_REASON_LABELS:
        return CLOSE_REASON_LABELS[text]
    matched = _FORCE_EXIT_PATTERN.search(text)
    if matched:
        return f"超期强平（{int(matched.group(1))} 日）"
    matched = _ROUND_STOP_PATTERN.search(text)
    if matched:
        # 超期持仓 N 日 → 止盈阈值降档，本质仍是止盈。
        return "止盈（价格轨/账户轨）"
    matched = _STOP_LOSS_PATTERN.search(text)
    if matched:
        return f"止损（{int(matched.group(1))}%）"
    if _TRAILING_PATTERN.search(text):
        return "移动止盈（达标转trailing回撤）"
    if "价格轨" in text or "账户轨" in text:
        return "止盈（价格轨/账户轨）"
    if "Armed" in text or "量价背离" in text:
        return "Armed 量价背离（辅助清仓）"
    if "VWAP" in text:
        return "VWAP 账户回撤（辅助清仓）"
    if ("T+1" in text or "T日" in text) and ("强平" in text or "截止" in text):
        return "T+1 尾盘强平" if "T+1" in text else "T 日尾盘强平"
    if "强平" in text:
        return "超期强平"
    if "止盈" in text:
        return "止盈（价格轨/账户轨）"
    return text


def _symbol_name_map(account: BrokerAccount) -> dict[str, str]:
    """标的 → 名称：从轮动池与最近一次筛选快照里取（模拟盘没有历史名称快照）。"""
    names: dict[str, str] = {}
    for item in account.rotation_pool or []:
        if isinstance(item, dict) and item.get("symbol"):
            names[str(item["symbol"])] = str(item.get("name") or "")
    screen = account.rotation_last_screen or {}
    first = screen.get("first") if isinstance(screen, dict) else None
    if isinstance(first, dict):
        code = str(first.get("code") or first.get("symbol") or "")
        if code:
            names.setdefault(code, str(first.get("name") or ""))
    return names


def _order_reason(account: BrokerAccount, fill: Fill) -> str:
    for order in account.orders or []:
        if order.id == fill.order_id:
            return str(order.reason or "")
    return ""


def _equity_point_index(dates: list[str], day: str) -> int:
    """最后一个 ``date <= day`` 的净值点下标；无则 -1。"""
    return bisect_right(dates, day) - 1


def _round_pnl(
    account: BrokerAccount, dates: list[str], equity_by_date: dict[str, float],
    start_date: str, end_day: str, *, closed: bool, is_first_round: bool,
    withdraw_in_round: float,
) -> tuple[float | None, bool]:
    """轮次 pnl + 是否近似（``approx``）。

    口径与回测一致：``pnl = 区间最后一个交易日 equity − 轮次起始前一日 equity``。
    - 有前一日净值点：CLOSED 用区间末净值差；OPEN 用**当前** ``account.equity``
      收尾（当前未平轮次没有「已实现收尾」，只能按当前状态算），都精确；
    - 缺前一日净值点（净值序列从本轮中途才开始）：首轮用
      ``equity + 轮内提现 − initial_cash`` 近似，标 ``approx=True``；后续轮次不瞎补；
    - 完全没有 paper_equity 数据（新账户首日）：``pnl=None``、``approx=False``，
      不用现金流口径硬算一个假的轮次收益。
    """
    if not dates:
        return None, False
    end_index = _equity_point_index(dates, end_day)
    if closed and (end_index < 0 or dates[end_index] < start_date):
        return None, False
    # 严格早于建仓日的最后一个净值点 = 轮次前一日基准。
    previous_index = bisect_left(dates, start_date) - 1
    if previous_index >= 0:
        baseline = equity_by_date[dates[previous_index]]
        if not closed:
            return money(account.equity + withdraw_in_round - baseline), False
        # CLOSED：区间末日不是净值点时（例如盘中清仓、当日快照未落），用最后一个
        # 不晚于末日的净值点，并标记近似，避免把缺口当成「当日 0 波动」。
        approx = dates[end_index] != end_day
        return money(equity_by_date[dates[end_index]] - baseline), approx
    if not is_first_round:
        return None, False
    if closed:
        return money(equity_by_date[dates[end_index]] + withdraw_in_round - account.initial_cash), True
    return money(account.equity + withdraw_in_round - account.initial_cash), True


def _holding_days(
    dates: list[str], start_date: str, end_day: str | None,
    trading_dates: list[str] | None = None,
) -> int | None:
    """持有交易日数（含首尾）。

    优先按交易日历 ``trading_dates``（升序，[start, end] 闭区间）计数——净值快照
    序列稀疏（如回放建仓/重置后 live 序列只有零星几天）时，按快照计数会严重低估，
    日历口径不受快照密度影响。日历不可用（None/空）或日历计数为 0（节假日/未覆盖）
    时，回落到旧的「按已落库逐日净值日期计」口径。两者都拿不到则返回 None（不瞎算）。
    """
    if not end_day:
        return None
    start_text, end_text = str(start_date)[:10], str(end_day)[:10]
    if trading_dates:
        count = bisect_right(trading_dates, end_text) - bisect_left(trading_dates, start_text)
        if count > 0:
            return count
    if not dates:
        return None
    return max(0, bisect_right(dates, end_text) - bisect_left(dates, start_text))


def _build_close_diagnostic(
    account: BrokerAccount, *, start_date: str, end_date: str,
    close_fill: Fill | None, round_return: float | None,
) -> dict[str, Any] | None:
    """构造与回测 ``rounds[].close_diagnostic`` 同构的止盈执行诊断。

    信号侧（trigger/signal_price/signal_time/price_return）来自
    ``account.strategy_state["last_close_signal"]``（``_set_close_signal`` 落库）；
    成交侧来自轮次最后一笔卖出成交。只有信号时间落在本轮区间内才采用——策略状态里
    只保留「最近一次」清仓信号，历史轮次不能张冠李戴。
    """
    diagnostic: dict[str, Any] = {}
    if close_fill is not None:
        diagnostic["fill_price"] = close_fill.price
        diagnostic["fill_time"] = close_fill.timestamp
    signal = (account.strategy_state or {}).get("last_close_signal")
    if isinstance(signal, dict):
        signal_day = str(signal.get("signal_time") or "")[:10]
        if signal_day and start_date <= signal_day <= end_date:
            for key in ("trigger", "signal_price", "signal_time", "price_return",
                        "trailing_peak_return", "trailing_drawdown_pct"):
                diagnostic[key] = signal.get(key)
            diagnostic["trigger_label"] = close_reason_label(signal.get("trigger"))
            signal_price = signal.get("signal_price")
            if close_fill is not None and signal_price:
                diagnostic["execution_gap"] = close_fill.price / float(signal_price) - 1
    if round_return is not None:
        diagnostic["round_return"] = round_return
        price_return = diagnostic.get("price_return")
        if price_return is not None:
            diagnostic["return_gap"] = round_return - float(price_return)
    return diagnostic or None


def paper_rounds(
    account: BrokerAccount, equity_curve: list[dict[str, Any]] | None = None,
    trading_dates: list[str] | None = None,
) -> list[dict[str, Any]]:
    """从成交流水重建模拟盘轮次明细（字段与回测 ``rounds`` 同构）。

    参数 ``equity_curve``：``storage.list_paper_equity_daily`` 的逐日行
    （date/equity/beta_value...，与回测 equity_curve 对齐）。空列表 = 账户还没有
    任何逐日净值快照，此时 ``pnl`` 留 None（不瞎算），三腿分拆也无从谈起。

    参数 ``trading_dates``：升序交易日历（覆盖各轮区间），供 ``holding_days`` 按
    交易日计（不受净值快照密度影响）。None/空时 ``holding_days`` 降级为按净值快照
    日期计数（旧行为）。

    返回按建仓顺序编号（R001 起）的轮次列表；``status=OPEN`` 的轮次
    ``end_date=None``，pnl 用当前账户权益收尾。
    """
    # 延迟导入：paper_analytics 保持轻量（不把回测引擎的 universe/screener 依赖
    # 在指标读时全部拉起），同时分腿口径与回测共用同一份实现（单一真相源）。
    from .backtest import _round_withdraw_total, decompose_round_pnl

    curve = sorted(
        [dict(point) for point in (equity_curve or []) if str(point.get("date") or "")],
        key=lambda point: str(point["date"]),
    )
    dates = [str(point["date"]) for point in curve]
    equity_by_date = {str(point["date"]): float(point.get("equity") or 0.0) for point in curve}
    trades = list(account.trades or [])
    withdrawals = list(account.withdrawals or [])
    beta_fills = [fill for fill in trades if fill.purpose == "BETA"]
    names = _symbol_name_map(account)

    # 1) 切轮次：按标的净个股持仓 0 → 正 → 0。
    windows: list[dict[str, Any]] = []
    net_qty: dict[str, int] = {}
    current: dict[str, Any] | None = None
    for index, fill in enumerate(trades):
        if fill.purpose == "BETA":
            continue
        symbol = fill.symbol
        before = net_qty.get(symbol, 0)
        delta = int(fill.quantity) if fill.side == "BUY" else -int(fill.quantity)
        after = max(0, before + delta)
        net_qty[symbol] = after
        if current is None:
            if before <= 0 and after > 0:
                current = {
                    "symbol": symbol, "start_date": _fill_date(fill), "start_index": index,
                    "end_date": None, "end_index": None,
                }
        elif symbol == current["symbol"] and after == 0:
            current["end_date"] = _fill_date(fill)
            current["end_index"] = index
            windows.append(current)
            current = None
    if current is not None:
        windows.append(current)

    # 2) 逐轮重放成本账本 / 统计 T 与提现 / 用权益差算 pnl / 调 decompose 填三腿。
    rows: list[dict[str, Any]] = []
    for number, window in enumerate(windows, start=1):
        start_index = int(window["start_index"])
        closed = window["end_index"] is not None
        end_index = int(window["end_index"]) if closed else len(trades) - 1
        window_fills = trades[start_index:end_index + 1]
        stock_fills = [fill for fill in window_fills if fill.purpose != "BETA"]
        start_date = str(window["start_date"])
        end_date = str(window["end_date"]) if closed else None
        as_of = end_date or (dates[-1] if dates else _fill_date(stock_fills[-1]))
        max_invested, invested = _replay_permanent_ledger(stock_fills)
        installment_count = sum(
            1 for fill in stock_fills if fill.side == "BUY" and fill.purpose == "PERMANENT"
        )
        t_net_profit = money(sum(
            float(fill.realized_pnl) for fill in stock_fills
            if fill.purpose == "T" and fill.side == "SELL"
        ))
        withdraw_in_round = money(_round_withdraw_total(withdrawals, start_date, as_of))
        pnl, approx = _round_pnl(
            account, dates, equity_by_date, start_date, as_of,
            closed=closed, is_first_round=number == 1,
            withdraw_in_round=withdraw_in_round,
        )
        holding_days = _holding_days(dates, start_date, as_of, trading_dates)

        stock_pnl = beta_pnl = None
        if pnl is not None:
            decomposed = decompose_round_pnl(
                [{
                    "id": f"R{number:03d}", "symbol": window["symbol"],
                    "start_date": start_date, "end_date": as_of,
                    "status": "CLOSED" if closed else "OPEN", "pnl": pnl,
                }],
                curve, withdrawals, beta_fills,
            )[0]
            stock_pnl = decomposed.get("stock_pnl")
            beta_pnl = decomposed.get("beta_pnl")
            if decomposed.get("withdraw_in_round") is not None:
                withdraw_in_round = decomposed["withdraw_in_round"]

        round_return = (
            pnl / max_invested if pnl is not None and max_invested > 0 else None
        )
        close_fill = None
        if closed:
            close_fill = next(
                (fill for fill in reversed(stock_fills) if fill.side == "SELL"), None,
            )
        raw_reason = _order_reason(account, close_fill) if close_fill is not None else ""
        if closed:
            close_reason = (raw_reason or "").strip() or "模拟盘清仓"
        else:
            close_reason = None
        diagnostic = None
        if closed:
            diagnostic = _build_close_diagnostic(
                account, start_date=start_date, end_date=str(end_date),
                close_fill=close_fill, round_return=round_return,
            )

        rows.append({
            "id": f"R{number:03d}",
            "symbol": window["symbol"],
            "name": names.get(window["symbol"], ""),
            "start_date": start_date,
            "end_date": end_date,
            "entry_date": start_date,
            "exit_date": end_date,
            "status": "CLOSED" if closed else "OPEN",
            "installment_count": installment_count,
            "invested": invested,
            "max_invested": max_invested,
            "t_net_profit": t_net_profit,
            "pnl": pnl,
            "approx": approx,
            "stock_pnl": stock_pnl,
            "beta_pnl": beta_pnl,
            "withdraw_in_round": withdraw_in_round,
            "round_return": round_return,
            "holding_days": holding_days,
            "close_reason": close_reason,
            "close_reason_label": close_reason_label(close_reason),
            "close_diagnostic": diagnostic,
            # 摊平字段与回测换票轨迹的 close_* 同名同义，前端表格可直接用。
            "close_signal_trigger": (diagnostic or {}).get("trigger"),
            "close_signal_trigger_label": (diagnostic or {}).get("trigger_label"),
            "close_signal_price": (diagnostic or {}).get("signal_price"),
            "close_signal_time": (diagnostic or {}).get("signal_time"),
            "close_signal_return": (diagnostic or {}).get("price_return"),
            "close_fill_price": (diagnostic or {}).get("fill_price"),
            "close_fill_time": (diagnostic or {}).get("fill_time"),
            "close_execution_gap": (diagnostic or {}).get("execution_gap"),
            "close_round_return": round_return if closed else None,
            "close_return_gap": (diagnostic or {}).get("return_gap"),
        })
    return rows


def paper_strategy_metrics(
    account: BrokerAccount, rounds: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """策略指标汇总：与回测 ``metrics`` 同名同口径，且不依赖净值曲线。

    只聚合「轮次 + 成交」能精确算出的策略字段（胜率 / 盈亏比 / 平均单轮收益 /
    平均持有交易日 / 做 T 与 ETF 贡献 / 加仓分布 / 退出原因分布）。回测里
    ``avg_entry_delay_days`` 需要选股日，模拟盘历史选股快照会被覆盖、无法精确回放，
    这里**不返回**该字段（不近似编造）。
    """
    rows = list(rounds or [])
    closed = [row for row in rows if str(row.get("status") or "").upper() == "CLOSED"]
    wins = [row for row in closed if float(row.get("pnl") or 0.0) > 0]
    losses = [row for row in closed if float(row.get("pnl") or 0.0) < 0]
    gross_profit = sum(float(row.get("pnl") or 0.0) for row in wins)
    gross_loss = -sum(float(row.get("pnl") or 0.0) for row in losses)

    round_returns = [
        float(row["round_return"]) for row in closed if row.get("round_return") is not None
    ]
    holding_days = [
        float(row["holding_days"]) for row in closed if row.get("holding_days") is not None
    ]
    max_invested_values = [
        float(row["max_invested"]) for row in rows if row.get("max_invested") is not None
    ]
    installments = [
        int(row["installment_count"]) for row in rows if row.get("installment_count") is not None
    ]
    histogram: dict[str, int] = {}
    for count in installments:
        histogram[str(count)] = histogram.get(str(count), 0) + 1
    reasons = Counter(
        close_reason_label(row.get("close_reason")) or str(row.get("close_reason"))
        for row in closed if row.get("close_reason")
    )

    trades = list(account.trades or [])
    beta_fills = [fill for fill in trades if fill.purpose == "BETA"]
    return {
        "total_rounds": len(rows),
        "closed_rounds": len(closed),
        # 与回测 performance_metrics 一致：胜率按已结束轮计；盈亏比无亏损轮时为 None。
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        # 回测 metrics 没有平均单轮字段，这里按 lead 要求补（仅已结束轮，缺值跳过）。
        "avg_round_return": (
            sum(round_returns) / len(round_returns) if round_returns else None
        ),
        "avg_holding_days": (
            sum(holding_days) / len(holding_days) if holding_days else None
        ),
        "t_net_profit": money(account.t_net_profit),
        "t_trade_count": sum(1 for fill in trades if fill.purpose == "T"),
        "beta_pnl": money(sum(
            float(fill.realized_pnl) for fill in beta_fills if fill.side == "SELL"
        )),
        "beta_trade_count": len(beta_fills),
        "max_invested": max_invested_from_fills(trades),
        "avg_max_invested": (
            sum(max_invested_values) / len(max_invested_values)
            if max_invested_values else None
        ),
        "installment_avg": (
            sum(installments) / len(installments) if installments else None
        ),
        "installment_max": max(installments) if installments else 0,
        "installment_histogram": {
            key: histogram[key] for key in sorted(histogram, key=int)
        },
        "close_reason_distribution": dict(reasons),
    }


# ---------------------------------------------------------------------------
# 策略开关状态（来自账户自己的 config 快照）
# ---------------------------------------------------------------------------

_SWITCH_GROUP_RISK = "止盈/风控"
_SWITCH_GROUP_T = "做T"
_SWITCH_GROUP_ADD = "加仓"
_SWITCH_GROUP_EXIT = "持仓/退出"
_SWITCH_GROUP_BETA = "ETF beta"


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "关闭"
    return f"{round(float(value) * 100, digits):g}%"


def _fmt_wan(value: float) -> str:
    return f"{float(value) / 10_000:g}万"


def _switch(
    key: str, label: str, enabled: bool, value: Any, group: str, hint: str = "",
) -> dict[str, Any]:
    return {
        "key": key, "label": label, "enabled": bool(enabled),
        "value": value, "group": group, "hint": hint,
    }


def paper_strategy_switches(account: BrokerAccount) -> dict[str, Any]:
    """从**账户配置快照** ``account.config`` 读取当前生效的策略开关。

    架构原则 3：在途账户的配置是创建 / 更新时定格的快照，这里绝不回读全局配置，
    否则「改了全局设置，历史账户行为跟着变」会破坏可复现性。

    返回 ``{"switches": {分组: [{key,label,enabled,value,group,hint}]},
    "config_snapshot_at": 账户创建时间}``；``value`` 已是展示文本（百分比 / 笔数 /
    时间 / 金额），前端等宽字体直出即可。
    """
    config = account.config
    t_on = bool(config.enable_t)
    beta_on = bool(config.etf_beta_enabled)
    amounts = [float(item) for item in (config.installment_amounts or [])]
    amount_preview = " / ".join(_fmt_wan(item) for item in amounts[:3])
    if len(amounts) > 3:
        amount_preview += f" … 共 {len(amounts)} 档"

    switches = {
        _SWITCH_GROUP_RISK: [
            _switch(
                "price_take_profit", "价格轨止盈", True,
                _fmt_pct(config.price_take_profit), _SWITCH_GROUP_RISK,
                "止盈（价格轨/账户轨）",
            ),
            _switch(
                "account_take_profit", "账户轨止盈", True,
                _fmt_pct(config.account_take_profit), _SWITCH_GROUP_RISK,
                "止盈（价格轨/账户轨）",
            ),
            _switch(
                "armed_range", "Armed 区间", float(config.price_armed) > 0,
                f"{_fmt_pct(config.price_armed)} ~ {_fmt_pct(config.price_take_profit)}",
                _SWITCH_GROUP_RISK,
                "量价背离 / VWAP 回撤辅助清仓仅在 Armed 区间生效",
            ),
            _switch(
                "divergence_ticks", "量价背离确认", int(config.divergence_ticks) > 0,
                f"{int(config.divergence_ticks)} 根（三选二）", _SWITCH_GROUP_RISK,
                "Armed 量价背离（辅助清仓）",
            ),
            _switch(
                "vwap_drawdown", "VWAP 账户回撤", float(config.vwap_drawdown) > 0,
                _fmt_pct(config.vwap_drawdown), _SWITCH_GROUP_RISK,
                "VWAP 账户回撤（辅助清仓）",
            ),
        ],
        _SWITCH_GROUP_T: [
            _switch(
                "enable_t", "做 T 开关", t_on, "开启" if t_on else "关闭",
                _SWITCH_GROUP_T, "做 T（正 T）",
            ),
            _switch(
                "t_min_base_entries", "底仓笔数门槛", t_on,
                f"{int(config.t_min_base_entries)} 笔", _SWITCH_GROUP_T,
                "底仓不足时不允许做 T",
            ),
            _switch(
                "t_min_amplitude", "最小振幅", t_on,
                _fmt_pct(config.t_min_amplitude), _SWITCH_GROUP_T,
            ),
            _switch(
                "t_min_turnover", "换手下限", t_on,
                _fmt_pct(config.t_min_turnover), _SWITCH_GROUP_T,
            ),
            _switch(
                "t_max_turnover", "换手上限", t_on,
                _fmt_pct(config.t_max_turnover), _SWITCH_GROUP_T,
            ),
            _switch(
                "t_min_expected_net_spread", "期望净利下限", t_on,
                _fmt_pct(config.t_min_expected_net_spread), _SWITCH_GROUP_T,
            ),
            _switch(
                "t_target_net_spread", "净利止盈（扣成本）", t_on,
                _fmt_pct(config.t_target_net_spread), _SWITCH_GROUP_T,
            ),
            _switch(
                "t_exit_cutoff", f"{config.t_exit_cutoff_day} 尾盘截止", t_on,
                f"{config.t_exit_cutoff_day} {config.t_exit_cutoff}",
                _SWITCH_GROUP_T, f"{config.t_exit_cutoff_day} 尾盘强平",
            ),
            _switch(
                "t_deep_drop_pct", "深跌幅当日做 T",
                config.t_deep_drop_pct is not None,
                _fmt_pct(config.t_deep_drop_pct), _SWITCH_GROUP_T,
                "深跌幅当日做 T",
            ),
        ],
        _SWITCH_GROUP_ADD: [
            _switch(
                "add_tier_strict", "加仓逐档加严", bool(config.add_tier_strict),
                "逐档加严" if config.add_tier_strict else "全档统一",
                _SWITCH_GROUP_ADD,
            ),
            _switch(
                "min_add_gap_days", "加仓最短间隔", int(config.min_add_gap_days) > 0,
                f"{int(config.min_add_gap_days)} 个交易日", _SWITCH_GROUP_ADD,
            ),
            _switch(
                "installment_amounts", "16 笔金额序列", True,
                amount_preview, _SWITCH_GROUP_ADD,
            ),
            _switch(
                "installment_scale_mode", "规模口径",
                config.installment_scale_mode == "equity",
                "按权益等比" if config.installment_scale_mode == "equity" else "固定金额",
                _SWITCH_GROUP_ADD,
            ),
            _switch(
                "installment_scale_floor", "缩放下限",
                config.installment_scale_mode == "equity",
                _fmt_pct(config.installment_scale_floor), _SWITCH_GROUP_ADD,
            ),
        ],
        _SWITCH_GROUP_EXIT: [
            _switch(
                "max_holding_days", "超期降阈止盈",
                config.max_holding_days is not None,
                f"{int(config.max_holding_days)} 个交易日"
                if config.max_holding_days is not None else "关闭",
                _SWITCH_GROUP_EXIT, "超期持仓降阈止盈",
            ),
            _switch(
                "force_exit_holding_days", "最大持仓天数（强平）",
                int(config.force_exit_holding_days) > 0,
                f"{int(config.force_exit_holding_days)} 日"
                if int(config.force_exit_holding_days) > 0 else "关闭",
                _SWITCH_GROUP_EXIT,
                "超期强平（{n} 日）".format(n=int(config.force_exit_holding_days))
                if int(config.force_exit_holding_days) > 0 else "超期强平",
            ),
            _switch(
                "stop_loss_pct", "硬止损",
                float(config.stop_loss_pct) < 0,
                f"{abs(float(config.stop_loss_pct)) * 100:.0f}%"
                if float(config.stop_loss_pct) < 0 else "关闭",
                _SWITCH_GROUP_EXIT,
                f"止损{abs(float(config.stop_loss_pct)) * 100:.0f}%"
                if float(config.stop_loss_pct) < 0 else "硬止损",
            ),
        ],
        _SWITCH_GROUP_BETA: [
            _switch(
                "etf_beta_enabled", "ETF beta 腿", beta_on,
                "开启" if beta_on else "关闭", _SWITCH_GROUP_BETA,
            ),
            _switch(
                "etf_symbol", "ETF 标的", beta_on,
                str(config.etf_symbol), _SWITCH_GROUP_BETA,
            ),
            _switch(
                "etf_position_pct", "仓位占权益", beta_on,
                _fmt_pct(config.etf_position_pct), _SWITCH_GROUP_BETA,
            ),
            _switch(
                "etf_entry_ma_days", "入场均线", beta_on,
                f"{int(config.etf_entry_ma_days)} 日", _SWITCH_GROUP_BETA,
            ),
            _switch(
                "etf_exit_ma_days", "出场均线", beta_on,
                f"{int(config.etf_exit_ma_days)} 日", _SWITCH_GROUP_BETA,
            ),
            _switch(
                "etf_hard_stop_pct", "ETF 硬止损", beta_on,
                _fmt_pct(config.etf_hard_stop_pct), _SWITCH_GROUP_BETA, "ETF 硬止损",
            ),
            _switch(
                "etf_beta_coexist", "与个股并存", beta_on,
                "与个股并存" if config.etf_beta_coexist else "空仓期替补",
                _SWITCH_GROUP_BETA,
            ),
        ],
    }
    return {
        "switches": switches,
        "config_snapshot_at": getattr(account, "created_at", None),
    }
