"""做T 与永久仓加仓「同日互斥、永久仓优先」的回归测试。

背景：三个去重闸原本各自独立、互不互斥——永久仓加仓用 ``add_submitted_date ==
bar.date`` 去重（日线 / 尾盘），做T用 ``t_order_submitted`` 去重。结果同一天既能加仓
又能做T（报告 300548 一轮有 7 天「T 买入 + 永久仓加仓」同日发生）。

改成：当日已发永久仓加仓（``add_submitted_date == bar.date``）则不做T——永久仓优先、
做T让位；反向不锁（已做T 不阻止后续加仓）。闸放 ``_t_eligible`` 开头（做T入场单一
真相源，回测盘中 / 模拟盘 live-tick 三条路径都经它）。

时序说明：做T入场窗口 ``09:35 ≤ t < 14:00``，尾盘加仓窗口 ``14:30 ≤ t < 14:50``，
两者不重叠。故「当日已加仓→再做T」这个被闸状态在自然时序下做T窗口已过，测试直接置
``add_submitted_date`` 模拟该状态，只验证闸本身（``_t_eligible`` / on_intraday 做T发单处）。
"""

from datetime import datetime, timedelta

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig
from app.strategy import LayeredTStrategy

SYM = "sz.301205"  # 创业板：min_qty=100, step=100

TODAY = datetime(2026, 11, 10, 10, 0)
YESTERDAY = datetime(2026, 11, 9, 15, 0)


def _daily(ts: datetime, close: float) -> Bar:
    """构造一根日线：振幅 ~6%（满足 ≥4% 门槛），换手率缺失（模拟 quan 源）。"""
    return Bar(
        timestamp=ts, symbol=SYM, open=close, high=close * 1.03, low=close * 0.97,
        close=close, volume=500_000, amount=close * 500_000, timeframe="daily",
        turnover_rate=None, prev_close=close,
    )


def _m5(ts: datetime, close: float) -> Bar:
    """构造一根 5 分钟 bar：无换手/盘口（模拟历史分钟 bar，靠 live_* 兜底）。"""
    return Bar(
        timestamp=ts, symbol=SYM, open=close, high=close * 1.01, low=close * 0.99,
        close=close, volume=500_000, amount=close * 500_000, timeframe="5m",
        turnover_rate=None, prev_close=close,
    )


def _funded_account_with_base() -> tuple[BrokerAccount, LayeredTStrategy, list[Bar]]:
    """建满5笔底仓（sequence≥4，做T启用）+ 昨日持仓（今日 T+1 可卖）的账户与日线历史。

    与 ``test_t_eligible_live.py`` 同构造口径：昨日 600 股底仓、今日浮盈约 1%
    （远低于止盈阈值，不会触发风控清仓 → on_intraday 能走到做T块）。
    """
    cfg = StrategyConfig()
    acct = BrokerAccount(initial_cash=1_000_000.0, config=cfg, name="t")
    acct.sequence_index = 5
    strat = LayeredTStrategy(cfg)

    base = datetime(2026, 11, 5, 15, 0)
    hist = []
    price = 140.0
    for i in range(5):
        ts = base + timedelta(days=i)
        close = price * (1.02 if i % 2 == 0 else 0.99)  # 涨跌交替，避免单向下跌过滤
        hist.append(_daily(ts, close))
        price = close

    # 昨日建600股底仓并撮合
    acct.submit(OrderRequest(symbol=SYM, side="BUY", quantity=600, purpose="PERMANENT",
                             execution_mode="NEXT_BAR"), hist[-1].timestamp)
    acct.process_bar(hist[-1], modes={"NEXT_BAR"})
    # 跨日到今天，T+1 解锁可卖
    acct.new_day(TODAY.date().isoformat(), cancel_previous=False)
    strat.start_day(True)
    return acct, strat, hist


def _live_kwargs() -> dict:
    """让 _t_eligible 通过准入的实时快照：换手 8%（落在[3%,12%]）、盘口有价差。"""
    return dict(live_turnover=0.08, live_bid=141.49, live_ask=141.51)


def test_same_day_add_blocks_t_permanent_priority():
    """当日已发永久仓加仓 → 不发做T单（永久仓优先，做T让位）。"""
    acct, strat, hist = _funded_account_with_base()
    m5 = _m5(TODAY, 141.5)

    # 先证明：不加闸时做T准入本可通过（对照，确认闸是唯一拦截点）。
    assert strat._t_eligible(history=hist, bar=m5, account=acct, **_live_kwargs()) is True

    # 当日已发永久仓加仓（add_submitted_date == bar.date）
    strat.add_submitted_date = TODAY.date().isoformat()
    # 做T准入被闸掉
    assert strat._t_eligible(history=hist, bar=m5, account=acct, **_live_kwargs()) is False
    # on_intraday 做T发单处也不发 purpose="T"
    orders = strat.on_intraday(history=hist, recent_bars=[m5], bar=m5, account=acct,
                               **_live_kwargs())
    assert all(o.purpose != "T" for o in orders)


def test_no_add_today_t_fires_normally():
    """当日未加仓 → 做T正常发单（回归护栏，闸不该误拦）。"""
    acct, strat, hist = _funded_account_with_base()
    m5 = _m5(TODAY, 141.5)
    assert strat.add_submitted_date is None  # 今日未加仓

    orders = strat.on_intraday(history=hist, recent_bars=[m5], bar=m5, account=acct,
                               **_live_kwargs())
    t_orders = [o for o in orders if o.purpose == "T"]
    assert len(t_orders) == 1
    assert t_orders[0].side == "BUY"


def test_next_day_t_resumes_after_add():
    """次日（add_submitted_date 已是昨日）→ 做T恢复发单。"""
    acct, strat, hist = _funded_account_with_base()
    m5 = _m5(TODAY, 141.5)
    # 昨日发的加仓，今日 bar.date 已不同 → 闸不拦
    strat.add_submitted_date = YESTERDAY.date().isoformat()

    assert strat._t_eligible(history=hist, bar=m5, account=acct, **_live_kwargs()) is True
    orders = strat.on_intraday(history=hist, recent_bars=[m5], bar=m5, account=acct,
                               **_live_kwargs())
    assert any(o.purpose == "T" for o in orders)