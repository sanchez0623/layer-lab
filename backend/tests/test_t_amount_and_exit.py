"""做T金额跟随加仓档 + T仓纯目标价差退出的回归测试。

覆盖两条优化：
1. 做T单笔金额不再用固定 t_order_amount，改为跟随当前加仓档
   installment_amounts[sequence_index]（满仓 sequence_index>=16 取最大档）。
2. T仓退出为纯目标价差：去掉「隔夜强制T+1卖出」与「14:50尾盘强制轧平」，
   底仓兜底下可跨多日持有，只有价格达到 均价*(1+t_target_spread) 才平T。
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


def _exit_account(*, t_qty: int = 1_000, t_cost: float = 100_000.0) -> BrokerAccount:
    """已有 T 仓（均价 100）+ 充足可卖底仓的账户；永久仓均价也是 100（不触发止盈）。"""
    acct = _make_account(sequence_index=5)
    pos = acct.positions[SYM]
    pos.t_qty = t_qty
    pos.t_cost = t_cost
    return acct


def test_t_exit_only_on_target_spread_not_overnight_forced():
    """隔夜 T 仓（t_qty != today_t_buy_qty）价格未达目标 → 不强制 T+1 卖出。"""
    acct = _exit_account()
    strat = LayeredTStrategy(acct.config)
    strat.start_day(True)
    # 隔夜：today_t_buy_qty=0 而 t_qty=1000 → 旧逻辑会强制平，新逻辑不卖
    assert acct.position(SYM).t_qty != acct.position(SYM).today_t_buy_qty
    bar = _m5(datetime(2026, 11, 10, 10, 40), 100.9)  # < 目标 101.5
    assert strat.on_intraday(_history(), [bar], bar, acct, live_turnover=0.08) == []


def test_t_exit_not_forced_at_1450():
    """尾盘 14:50 价格未达目标 → 不再强制轧平（去掉旧 14:50 轧平）。"""
    acct = _exit_account()
    strat = LayeredTStrategy(acct.config)
    strat.start_day(True)
    bar = _m5(datetime(2026, 11, 10, 14, 50), 100.9)  # 未达目标 101.5
    assert strat.on_intraday(_history(), [bar], bar, acct, live_turnover=0.08) == []


def test_t_exit_triggers_on_target_spread():
    """价格达到 均价*(1+t_target_spread) → 平T，reason=T目标价差。"""
    acct = _exit_account(t_qty=1_000, t_cost=100_000.0)  # 均价 100，目标 101.5
    strat = LayeredTStrategy(acct.config)
    strat.start_day(True)
    bar = _m5(datetime(2026, 11, 10, 10, 40), 101.6)  # >= 目标 101.5
    requests = strat.on_intraday(_history(), [bar], bar, acct, live_turnover=0.08)
    assert len(requests) == 1
    assert requests[0].side == "SELL" and requests[0].purpose == "T"
    assert requests[0].reason == "T目标价差"
    assert requests[0].quantity == 1_000
    assert strat.t_exit_submitted is True


def test_t_exit_can_carry_multiple_days_until_target():
    """T 仓跨多日持有：连续多日未达目标都不卖，直到某日达目标才平。"""
    acct = _exit_account()
    strat = LayeredTStrategy(acct.config)
    strat.start_day(True)
    for day_offset, price in [(0, 100.5), (1, 101.0), (2, 101.4)]:
        ts = datetime(2026, 11, 10 + day_offset, 10, 40)
        bar = _m5(ts, price)  # 均未达 101.5
        assert strat.on_intraday(_history(), [bar], bar, acct, live_turnover=0.08) == [], (
            f"第{day_offset}日未达目标不应平T"
        )
    # 第三日达目标 → 平T
    bar = _m5(datetime(2026, 11, 13, 10, 40), 101.6)
    requests = strat.on_intraday(_history(), [bar], bar, acct, live_turnover=0.08)
    assert len(requests) == 1 and requests[0].reason == "T目标价差"
