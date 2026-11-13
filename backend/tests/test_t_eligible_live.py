"""做T准入用「当日实时快照换手率/盘口」兜底的回归测试。

背景：历史日线 bar 无换手字段（quan 源 turnover_rate 恒 None），历史 5 分钟 bar
无盘口（腾讯只把换手/盘口挂在当前快照 bar）。做T是当日盘中决策，需要当日换手率
和盘口，故 _t_eligible 支持从实时快照（live_turnover/live_bid/live_ask）兜底。
"""

from datetime import datetime, timedelta

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig
from app.strategy import LayeredTStrategy

SYM = "sz.301205"


def _daily(ts: datetime, close: float, *, turnover: float | None = None) -> Bar:
    """构造一根日线：振幅 ~6%（满足 ≥4% 门槛），换手率默认缺失（模拟 quan 源）。"""
    return Bar(
        timestamp=ts, symbol=SYM, open=close, high=close * 1.03, low=close * 0.97,
        close=close, volume=500_000, amount=close * 500_000, timeframe="daily",
        turnover_rate=turnover, prev_close=close,
    )


def _m5(ts: datetime, close: float, *, turnover: float | None = None,
        bid: float | None = None, ask: float | None = None) -> Bar:
    """构造一根 5 分钟 bar：默认无换手/盘口（模拟历史分钟 bar）。"""
    return Bar(
        timestamp=ts, symbol=SYM, open=close, high=close * 1.01, low=close * 0.99,
        close=close, volume=500_000, amount=close * 500_000, timeframe="5m",
        turnover_rate=turnover, prev_close=close, bid=bid, ask=ask,
    )


def _funded_account_with_base() -> tuple[BrokerAccount, LayeredTStrategy, list[Bar]]:
    """建满5笔底仓（sequence≥4）+ 昨日持仓（今日可卖）的账户与做T用的日线历史。"""
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
    today = datetime(2026, 11, 10, 10, 0)
    acct.new_day(today.date().isoformat(), cancel_previous=False)
    strat.start_day(True)
    return acct, strat, hist


def test_t_eligible_uses_live_turnover_when_history_missing():
    """历史日线无换手字段时，用实时快照换手率兜底，做T准入应通过。"""
    acct, strat, hist = _funded_account_with_base()
    today = datetime(2026, 11, 10, 10, 0)
    m5 = _m5(today, 141.5, bid=141.49, ask=141.51)  # 历史5分钟bar无换手/盘口

    # 不传 live_turnover：换手缺失 → 不准入
    assert strat._t_eligible(history=hist, bar=m5, account=acct) is False
    # 传实时快照换手率(8%，落在[3%,12%])：准入通过
    assert strat._t_eligible(history=hist, bar=m5, account=acct,
                             live_turnover=0.08) is True


def test_t_eligible_uses_live_bid_ask_when_bar_missing():
    """历史5分钟bar无盘口时，用实时快照盘口兜底。"""
    acct, strat, hist = _funded_account_with_base()
    today = datetime(2026, 11, 10, 10, 0)
    # bar 无盘口 + 无换手 → 必须靠 live_* 三项兜底
    m5 = _m5(today, 141.5)

    assert strat._t_eligible(history=hist, bar=m5, account=acct,
                             live_turnover=0.08, live_bid=141.49,
                             live_ask=141.51) is True


def test_t_eligible_rejects_turnover_out_of_range():
    """实时换手率超出[3%,12%]区间 → 不准入（换手过低/过高都不做T）。"""
    acct, strat, hist = _funded_account_with_base()
    today = datetime(2026, 11, 10, 10, 0)
    m5 = _m5(today, 141.5, bid=141.49, ask=141.51)

    assert strat._t_eligible(history=hist, bar=m5, account=acct,
                             live_turnover=0.01) is False  # 低于下限
    assert strat._t_eligible(history=hist, bar=m5, account=acct,
                             live_turnover=0.20) is False  # 高于上限


def test_t_eligible_rejects_when_live_bid_ask_missing():
    """实时盘口缺失（live_bid/ask 均 None）→ 不准入（避免裸卖）。"""
    acct, strat, hist = _funded_account_with_base()
    today = datetime(2026, 11, 10, 10, 0)
    m5 = _m5(today, 141.5)  # bar 与 live 都无盘口

    assert strat._t_eligible(history=hist, bar=m5, account=acct,
                             live_turnover=0.08) is False
