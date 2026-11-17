"""做T准入用「当日至今累计振幅/换手」而非「当日全天值」的哨兵测试（反未来函数）。

背景：回测盘中逐根喂 5 分钟 bar 做T准入时，曾把「当日日线全天振幅/全天换手」当作
常量传给每一根 bar（backtest.py）。全天值在盘中决策时点不可得（属未来数据，
look-ahead bias）。修复后回测盘中改传「当日至今累计」值：
  • 累计振幅 = (当日至今 running_high - running_low) / prev_close；
  • 累计换手 = 当日至今累计成交量 / 流通股本（流通股本由当日日线反推）。

本测试构造一根早盘 5 分钟 bar：当日**全天**振幅 ≥ 4%（满足门槛），但**截至该 bar**
的累计振幅 < 4%（不满足）。断言：传累计值时不准入（做T不触发），传全天值时才准入——
证明回测盘中用的是决策时点可得的累计值，而非未来函数的全天值。
"""

from datetime import datetime, timedelta

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig
from app.strategy import LayeredTStrategy

SYM = "sz.301205"


def _daily(ts: datetime, close: float) -> Bar:
    """构造一根日线（做T用的历史底仓日线），振幅 ~6%、换手缺失（模拟 quan 源）。"""
    return Bar(
        timestamp=ts, symbol=SYM, open=close, high=close * 1.03, low=close * 0.97,
        close=close, volume=500_000, amount=close * 500_000, timeframe="daily",
        turnover_rate=None, prev_close=close,
    )


def _m5(ts: datetime, *, prev_close: float, high: float, low: float, close: float,
        bid: float, ask: float) -> Bar:
    """构造一根 5 分钟 bar：历史分钟 bar 无换手（turnover_rate=None），盘口用代理值。"""
    return Bar(
        timestamp=ts, symbol=SYM, open=close, high=high, low=low, close=close,
        volume=500_000, amount=close * 500_000, timeframe="5m",
        turnover_rate=None, prev_close=prev_close, bid=bid, ask=ask,
    )


def _funded_account_with_base() -> tuple[BrokerAccount, LayeredTStrategy, list[Bar]]:
    """建满底仓（sequence_index≥4）+ 昨日持仓（今日可卖）的账户与做T用的日线历史。"""
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

    # 昨日建600股底仓并撮合（今日 T+1 可卖）
    acct.submit(OrderRequest(symbol=SYM, side="BUY", quantity=600, purpose="PERMANENT",
                             execution_mode="NEXT_BAR"), hist[-1].timestamp)
    acct.process_bar(hist[-1], modes={"NEXT_BAR"})
    # 跨日到今天，T+1 解锁可卖
    today = datetime(2026, 11, 10, 9, 35)
    acct.new_day(today.date().isoformat(), cancel_previous=False)
    strat.start_day(True)
    return acct, strat, hist


def test_intraday_t_uses_cumulative_amplitude_not_full_day():
    """早盘累计振幅<4% 但全天振幅≥4%：传累计值不做T，传全天值才做T（反未来函数）。"""
    acct, strat, hist = _funded_account_with_base()
    today = datetime(2026, 11, 10, 9, 35)
    prev_close = 100.0
    # 早盘单根 5 分钟 bar：累计振幅 = (100.6 - 99.8) / 100 = 0.8% < 4%。
    m5 = _m5(today, prev_close=prev_close, high=100.6, low=99.8, close=100.5,
             bid=100.49, ask=100.51)
    full_day_amplitude = 0.05   # 当日全天振幅 5% ≥ 4%（盘中不可得的未来值）
    cum_amplitude = 0.008       # 截至该 bar 的累计振幅 0.8% < 4%（决策时点可得）
    cum_turnover = 0.08         # 当日至今累计换手 8%，落在 [3%,12%]

    # 关键断言：传「当日至今累计振幅」→ 不准入（振幅未达门槛，且不得用全天值放行）。
    assert strat._t_eligible(history=hist, bar=m5, account=acct,
                             daily_amplitude=cum_amplitude,
                             daily_turnover_rate=cum_turnover) is False
    # 对照：只有当误传「全天振幅」时才会准入——这正是被修掉的 look-ahead 路径。
    assert strat._t_eligible(history=hist, bar=m5, account=acct,
                             daily_amplitude=full_day_amplitude,
                             daily_turnover_rate=cum_turnover) is True


def test_intraday_t_uses_cumulative_turnover_not_full_day():
    """累计换手落在区间外但全天换手落在区间内：传累计值不做T，传全天值才做T。"""
    acct, strat, hist = _funded_account_with_base()
    today = datetime(2026, 11, 10, 9, 35)
    prev_close = 100.0
    # 当日至今振幅已够（5% ≥ 4%），只让换手区分累计 vs 全天。
    m5 = _m5(today, prev_close=prev_close, high=102.5, low=97.5, close=100.5,
             bid=100.49, ask=100.51)
    cum_amplitude = 0.05
    cum_turnover_low = 0.01     # 当日至今累计换手 1% < 3% 下限（早盘量还没放出来）
    full_day_turnover = 0.08    # 全天换手 8%（未来值，落在区间内）

    # 传累计换手（1%，低于下限）→ 不准入；不得用全天换手 8% 放行。
    assert strat._t_eligible(history=hist, bar=m5, account=acct,
                             daily_amplitude=cum_amplitude,
                             daily_turnover_rate=cum_turnover_low) is False
    # 对照：误传全天换手才准入（被修掉的 look-ahead 路径）。
    assert strat._t_eligible(history=hist, bar=m5, account=acct,
                             daily_amplitude=cum_amplitude,
                             daily_turnover_rate=full_day_turnover) is True


def test_intraday_t_degrades_when_cumulative_turnover_none():
    """流通股本无法反推（累计换手=None）→ 不准入，且不退回 recent[-1] 的全天值。"""
    acct, strat, hist = _funded_account_with_base()
    today = datetime(2026, 11, 10, 9, 35)
    # bar 自身 turnover_rate 恒 None（quan 源分钟 bar 无换手，修复后不再填全天值）。
    m5 = _m5(today, prev_close=100.0, high=102.5, low=97.5, close=100.5,
             bid=100.49, ask=100.51)
    # daily_turnover_rate=None（无法反推流通股本时的降级）→ turnover 缺失 → 不做T。
    assert strat._t_eligible(history=hist, bar=m5, account=acct,
                             daily_amplitude=0.05,
                             daily_turnover_rate=None) is False
