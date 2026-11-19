"""做T 与永久仓加仓「同日互斥、永久仓优先」的集成测试（走真实 on_intraday 时序）。

背景：三个去重闸原本各自独立、互不互斥——永久仓加仓用 ``add_submitted_date ==
bar.date`` 去重（日线 / 尾盘），做T用 ``t_order_submitted`` 去重。报告 300548 一轮有
7 天「T 买入 + 永久仓加仓」同日发生。

改成：当日已发永久仓加仓（``add_submitted_date == bar.date``）则不做T——永久仓优先、
做T让位；反向不锁（已做T 不阻止后续加仓）。闸放 ``_t_eligible`` 开头（做T入场单一
真相源，回测盘中 / 模拟盘 live-tick 三条路径都经它）。

时序与阻挡机制（重要，测试据此构造、注释据此断言）：
  on_intraday 单根 bar 的执行顺序是 _risk_requests → _tail_entry → _tail_add → 做T块。
  做T新开窗口 ``09:35 ≤ t < 14:00``，尾盘加仓窗口 ``14:30 ≤ t < 14:50``，两者不重叠。
  故「当日已加仓 → 同日再做T」在真实时序下由两层结构挡住：
    ①加仓 bar（14:30+）上 ``_tail_add_requests`` 命中即 return，早于做T块——同 bar 到不了做T；
    ②加仓只在 14:30+ 发生，此时做T窗口已关（on_intraday 对 ≥14:00 直接 return）。
  ``_t_eligible`` 开头的闸是把「永久仓优先」写成显式不变式的 defense-in-depth：
  一旦当日发过加仓，``_t_eligible`` 恒 False，即使将来窗口调整到重叠也不会再发做T单。
  下面的测试用真实 on_intraday 复现 14:35 触发加仓、同日后续 bar 满足做T准入的场景，
  断言同日加仓后不再发做T单；并单独断言闸本身（flag 由真实加仓置位，不手动设）。
"""

from datetime import datetime, timedelta

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig
from app.strategy import LayeredTStrategy

SYM = "sz.301205"  # 创业板：min_qty=100, step=100

DAY_D = datetime(2026, 11, 10)      # 加仓日 / 做T日
DAY_D1 = datetime(2026, 11, 11)     # 次日
BASE_DAY = datetime(2026, 11, 4, 15, 0)

# 让 _t_eligible 通过准入的实时快照：换手 8%（落在 [3%,12%]）、盘口有价差。
LIVE = dict(live_turnover=0.08, live_bid=107.49, live_ask=107.51)


def _daily(ts: datetime, close: float, high: float, low: float) -> Bar:
    """构造一根日线（显式 high/low 以精确控制振幅与 recent_high / prior_three_low）。"""
    return Bar(
        timestamp=ts, symbol=SYM, open=close, high=high, low=low,
        close=close, volume=500_000, amount=close * 500_000, timeframe="daily",
        turnover_rate=None, prev_close=close,
    )


def _m5(ts: datetime, close: float, *, high: float | None = None,
        low: float | None = None) -> Bar:
    """构造一根 5 分钟 bar：无换手/盘口（模拟历史分钟 bar，靠 live_* 兜底）。"""
    return Bar(
        timestamp=ts, symbol=SYM, open=close,
        high=high if high is not None else close * 1.005,
        low=low if low is not None else close * 0.995,
        close=close, volume=500_000, amount=close * 500_000, timeframe="5m",
        turnover_rate=None, prev_close=close,
    )


def _funded_account_with_base() -> tuple[BrokerAccount, LayeredTStrategy, list[Bar]]:
    """建满5笔底仓（sequence=5，做T启用）+ 底仓 T+1 可卖的账户与日线历史。

    价格路径（closes=[100,108,104,106,103]，day1 high=115）同时满足三条约束：
      • 底仓在 day4 低价(~103)成交 → 今日 107 仍浮盈（+3.9%，远低于止盈阈值，
        不触发风控清仓 → on_intraday 能走到做T块）；
      • day1 high=115 作 recent_high，今日合成日线 close=107 ≤ 115×0.95=109.25、
        low=106 ≥ prior_three_low=99 → 浮盈右侧回撤5% 加仓信号可触发；
      • 末三根日线 closes [104,106,103] 非严格下跌 → one_sided_down=False → 做T准入可过；
      • 日线振幅均值 ~0.069 > t_min_amplitude(0.04) → 做T振幅门槛可过。
    """
    cfg = StrategyConfig()
    acct = BrokerAccount(initial_cash=1_000_000.0, config=cfg, name="t")
    acct.sequence_index = 5
    strat = LayeredTStrategy(cfg)

    spec = [  # (close, high, low)
        (100, 103, 97), (108, 115, 105), (104, 107, 101),
        (106, 109, 102), (103, 106, 99),
    ]
    hist = [_daily(BASE_DAY + timedelta(days=i), c, h, l)
            for i, (c, h, l) in enumerate(spec)]

    # 昨日(day4=hist[-1]) 建立600股底仓并撮合；手动补 strat.on_fill（真实系统由
    # 策略循环调用）以置 last_add_price / last_add_session，尾盘加仓判定依赖它们。
    acct.submit(OrderRequest(symbol=SYM, side="BUY", quantity=600, purpose="PERMANENT",
                             execution_mode="NEXT_BAR"), hist[-1].timestamp)
    for fill in acct.process_bar(hist[-1], modes={"NEXT_BAR"}):
        strat.on_fill(fill, session_index=100)
    return acct, strat, hist


def _start_day(acct: BrokerAccount, strat: LayeredTStrategy, day: datetime) -> None:
    acct.new_day(day.date().isoformat(), cancel_previous=False)
    strat.start_day(True)


def test_same_day_add_blocks_t_permanent_priority():
    """同日 14:35 真实触发加仓后，当天不再发做T单（永久仓优先，做T让位）。"""
    acct, strat, hist = _funded_account_with_base()
    _start_day(acct, strat, DAY_D)

    # 14:35：尾盘加仓窗口内，浮盈右侧回撤5% 信号命中 → 发永久仓加仓单、置 add_submitted_date。
    m35 = _m5(datetime(2026, 11, 10, 14, 35), 107.0, high=108.0, low=106.0)
    orders_35 = strat.on_intraday(history=hist, recent_bars=[m35], bar=m35,
                                  account=acct, session_index=200)
    assert [o.purpose for o in orders_35] == ["PERMANENT"]  # 只发加仓单
    assert orders_35[0].side == "BUY"
    assert strat.add_submitted_date == DAY_D.date().isoformat()

    # 同日 14:45（加仓之后）：即使仍喂入满足做T准入的盘口/换手，也不发做T单。
    m45 = _m5(datetime(2026, 11, 10, 14, 45), 107.0)
    orders_45 = strat.on_intraday(history=hist, recent_bars=[m45], bar=m45,
                                  account=acct, session_index=200, **LIVE)
    assert all(o.purpose != "T" for o in orders_45)

    # 闸本身：flag 由上面真实加仓置位（非手动），同日任意 bar 的做T准入恒 False。
    assert strat._t_eligible(history=hist, bar=m45, account=acct, **LIVE) is False


def test_no_add_today_t_fires_normally():
    """当日未加仓（盘中 10:00，加仓窗口未到）→ 做T正常发单（回归护栏，闸不误拦）。"""
    acct, strat, hist = _funded_account_with_base()
    _start_day(acct, strat, DAY_D)
    assert strat.add_submitted_date is None  # 今日尚未加仓

    m10 = _m5(datetime(2026, 11, 10, 10, 0), 107.0)
    orders = strat.on_intraday(history=hist, recent_bars=[m10], bar=m10,
                               account=acct, session_index=200, **LIVE)
    t_orders = [o for o in orders if o.purpose == "T"]
    assert len(t_orders) == 1
    assert t_orders[0].side == "BUY"


def test_next_day_t_resumes_after_add():
    """次日（add_submitted_date 已随 new_day 清空 / 属昨日）→ 做T恢复发单。"""
    acct, strat, hist = _funded_account_with_base()
    _start_day(acct, strat, DAY_D)

    # Day D 尾盘加仓一次（置 add_submitted_date = D）。
    m35 = _m5(datetime(2026, 11, 10, 14, 35), 107.0, high=108.0, low=106.0)
    strat.on_intraday(history=hist, recent_bars=[m35], bar=m35,
                      account=acct, session_index=200)
    assert strat.add_submitted_date == DAY_D.date().isoformat()

    # 次日盘中做T：add_submitted_date 已被 new_day 清空（属昨日），闸不拦 → 做T恢复。
    _start_day(acct, strat, DAY_D1)
    assert strat.add_submitted_date is None
    m_next = _m5(datetime(2026, 11, 11, 10, 0), 107.0)
    orders = strat.on_intraday(history=hist, recent_bars=[m_next], bar=m_next,
                               account=acct, session_index=210, **LIVE)
    assert any(o.purpose == "T" for o in orders)