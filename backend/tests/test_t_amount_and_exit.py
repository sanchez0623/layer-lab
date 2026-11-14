"""做T金额跟随加仓档 + 净利止盈 + T+1日尾盘截止的回归测试。

覆盖：
1. 做T单笔金额不再用固定 t_order_amount，改为跟随当前加仓档
   installment_amounts[sequence_index]（满仓 sequence_index>=16 取最大档）。
2. 做T平仓为「扣成本后净利止盈」：目标价 = 均价×(1 + t_target_net_spread + 往返成本率)，
   扣费后净赚 t_target_net_spread（默认 3%）即平T，T0 当天满足就当天做完。
3. 做T资金硬截止：T+1 日 t_exit_cutoff（默认 14:50）仍未止盈则无条件强平；
   T0 当日不强平（仅隔夜仓 t_qty>today_t_buy_qty 触发），保证资金最迟 T+1 收盘前了结。
"""

from datetime import datetime, timedelta

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, Position, StrategyConfig
from app.strategy import LayeredTStrategy, board_buy_quantity

SYM = "sz.301205"  # 创业板：min_qty=100 / step=100，便于股数预测


def _daily(ts: datetime, close: float) -> Bar:
    """日线：振幅约 6%（满足做T ≥4% 门槛），换手率缺失（模拟 quan 源）。"""
    return Bar(
        timestamp=ts, symbol=SYM, open=close, high=close * 1.03, low=close * 0.97,
        close=close, volume=500_000, amount=close * 500_000, timeframe="daily",
        prev_close=close,
    )


def _m5(ts: datetime, close: float) -> Bar:
    """5 分钟 bar：带盘口（做T准入需要 bid/ask）。"""
    return Bar(
        timestamp=ts, symbol=SYM, open=close, high=close * 1.01, low=close * 0.99,
        close=close, volume=500_000, amount=close * 500_000, timeframe="5m",
        prev_close=close, bid=close - 0.01, ask=close + 0.01,
    )


def _history() -> list[Bar]:
    """5 根日线，涨跌交替避免单边下跌过滤；末根 close=100 便于股数折算。"""
    base = datetime(2026, 11, 2, 15, 0)
    closes = [98.0, 100.5, 99.0, 101.0, 100.0]
    return [_daily(base + timedelta(days=i), c) for i, c in enumerate(closes)]


def _make_account(sequence_index: int) -> BrokerAccount:
    """建满底仓（今日可卖充足）+ 现金充裕的账户，序列位可指定。"""
    cfg = StrategyConfig()
    acct = BrokerAccount(initial_cash=2_000_000.0, config=cfg, name="t")
    acct.sequence_index = sequence_index
    # 昨日建 6000 股底仓，今日全部可卖（T+1 解锁），现价 100 时市值 60 万
    acct.positions[SYM] = Position(
        symbol=SYM, permanent_qty=6_000, permanent_cost=600_000.0,
        last_price=100.0,
    )
    return acct


def _t_buy_bar(minute: int = 40) -> Bar:
    """做T开仓时段（09:35–14:00）内的 5 分钟 bar，close=100。"""
    return _m5(datetime(2026, 11, 10, 10, minute), 100.0)


def test_t_amount_follows_current_installment_stage():
    """做T金额跟随当前加仓档：sequence_index=5 → installment_amounts[5]=20万 → 2000股。"""
    acct = _make_account(sequence_index=5)
    strat = LayeredTStrategy(acct.config)
    assert strat._t_order_amount(acct) == 200_000.0

    strat.start_day(True)
    bar = _t_buy_bar()
    hist = _history()
    live = {"live_turnover": 0.08}  # 换手 8%，落在 [3%,12%]
    requests = strat.on_intraday(hist, [bar], bar, acct, **live)
    assert len(requests) == 1 and requests[0].purpose == "T"
    # 20万 / 100元 / step100 = 2000 股（现金与可卖底仓均不封顶）
    assert requests[0].quantity == 2_000


def test_t_amount_uses_max_stage_when_sequence_full():
    """满仓后（sequence_index>=16）无「下一次加仓」，做T金额取最大档 40万 → 4000股。"""
    acct = _make_account(sequence_index=16)
    strat = LayeredTStrategy(acct.config)
    assert strat._t_order_amount(acct) == 400_000.0

    strat.start_day(True)
    bar = _t_buy_bar()
    requests = strat.on_intraday(_history(), [bar], bar, acct, live_turnover=0.08)
    assert len(requests) == 1 and requests[0].purpose == "T"
    assert requests[0].quantity == 4_000


def test_t_amount_matches_board_quantity_of_stage():
    """_t_order_amount 折算的股数与直接按该档金额算板块口径一致（口径统一）。"""
    acct = _make_account(sequence_index=6)
    strat = LayeredTStrategy(acct.config)
    amount = strat._t_order_amount(acct)
    assert amount == acct.config.installment_amounts[6]
    assert board_buy_quantity(SYM, amount, 100.0) == board_buy_quantity(
        SYM, acct.config.installment_amounts[6], 100.0
    )


def _exit_account(*, t_qty: int = 1_000, t_cost: float = 100_000.0,
                  today_t_qty: int = 0) -> BrokerAccount:
    """已有 T 仓（均价 100）+ 充足可卖底仓的账户；永久仓均价也是 100（不触发止盈）。

    today_t_qty=0 表示隔夜仓（T1）；today_t_qty=t_qty 表示当日新建仓（T0）。
    """
    acct = _make_account(sequence_index=5)
    pos = acct.positions[SYM]
    pos.t_qty = t_qty
    pos.t_cost = t_cost
    pos.today_t_buy_qty = today_t_qty
    return acct


def _net_target(strat: LayeredTStrategy, avg_price: float = 100.0) -> float:
    """净利止盈目标价 = 均价 ×(1 + 净目标 + 往返成本率)，与 strategy 实现同口径。"""
    return avg_price * (1 + strat.config.t_target_net_spread + strat._t_roundtrip_cost())


def test_t_exit_net_profit_target_triggers_same_day():
    """T0 当日达到净利目标价 → 平T，reason=T净利止盈（当天做完，不等 T1）。"""
    acct = _exit_account(t_qty=1_000, t_cost=100_000.0, today_t_qty=1_000)  # 当日新建仓
    strat = LayeredTStrategy(acct.config)
    strat.start_day(True)
    target = _net_target(strat, 100.0)  # ≈103.09
    bar = _m5(datetime(2026, 11, 10, 10, 40), round(target, 2) + 0.1)
    requests = strat.on_intraday(_history(), [bar], bar, acct, live_turnover=0.08)
    assert len(requests) == 1
    assert requests[0].side == "SELL" and requests[0].purpose == "T"
    assert requests[0].reason == "T净利止盈"
    assert requests[0].quantity == 1_000
    assert strat.t_exit_submitted is True


def test_net_profit_target_includes_roundtrip_cost():
    """目标价含往返成本：价格刚好等于 均价×(1+净目标)（未含成本）时不应平T。"""
    acct = _exit_account(t_qty=1_000, t_cost=100_000.0, today_t_qty=1_000)
    strat = LayeredTStrategy(acct.config)
    strat.start_day(True)
    assert strat._t_roundtrip_cost() > 0  # 成本率 > 0，才会抬高目标价
    bare_target = 100.0 * (1 + strat.config.t_target_net_spread)  # 103.0，未含成本
    bar = _m5(datetime(2026, 11, 10, 10, 40), round(bare_target, 2))
    assert bar.close < _net_target(strat, 100.0)  # 确认它低于含成本目标
    assert strat.on_intraday(_history(), [bar], bar, acct, live_turnover=0.08) == []


def test_t1_cutoff_forces_exit_at_1450():
    """T1 隔夜仓到 14:50 截止仍未止盈 → 无条件强平，reason=T+1尾盘截止平T。"""
    acct = _exit_account(t_qty=1_000, t_cost=100_000.0, today_t_qty=0)  # 隔夜仓
    strat = LayeredTStrategy(acct.config)
    strat.start_day(True)
    bar = _m5(datetime(2026, 11, 10, 14, 50), 101.0)  # 未达净利目标 ≈103.09
    requests = strat.on_intraday(_history(), [bar], bar, acct, live_turnover=0.08)
    assert len(requests) == 1
    assert requests[0].side == "SELL" and requests[0].purpose == "T"
    assert requests[0].reason == "T+1尾盘截止平T"
    assert requests[0].quantity == 1_000


def test_t0_not_forced_at_cutoff():
    """T0 当日新建仓到 14:50 截止 → 不强平（可持有到 T1），仅隔夜仓触发截止。"""
    acct = _exit_account(t_qty=1_000, t_cost=100_000.0, today_t_qty=1_000)  # 当日仓
    strat = LayeredTStrategy(acct.config)
    strat.start_day(True)
    bar = _m5(datetime(2026, 11, 10, 14, 50), 101.0)  # 未达净利目标
    assert strat.on_intraday(_history(), [bar], bar, acct, live_turnover=0.08) == []


def test_t1_before_cutoff_not_forced():
    """T1 隔夜仓在截止时间前（10:40）未达止盈 → 不强平（还有时间等止盈）。"""
    acct = _exit_account(t_qty=1_000, t_cost=100_000.0, today_t_qty=0)
    strat = LayeredTStrategy(acct.config)
    strat.start_day(True)
    bar = _m5(datetime(2026, 11, 10, 10, 40), 101.0)  # 未达净利目标
    assert strat.on_intraday(_history(), [bar], bar, acct, live_turnover=0.08) == []


def test_t_exit_config_validation():
    """新配置项校验：净利止盈须在 (0,1)，截止时间须为合法 HH:MM。"""
    import pytest
    with pytest.raises(ValueError):
        StrategyConfig(t_target_net_spread=0.0)
    with pytest.raises(ValueError):
        StrategyConfig(t_target_net_spread=1.5)
    with pytest.raises(ValueError):
        StrategyConfig(t_exit_cutoff="25:00")
    with pytest.raises(ValueError):
        StrategyConfig(t_exit_cutoff="not-a-time")
    # 合法值可正常构造
    ok = StrategyConfig(t_target_net_spread=0.03, t_exit_cutoff="14:50")
    assert ok.t_target_net_spread == 0.03 and ok.t_exit_cutoff == "14:50"
