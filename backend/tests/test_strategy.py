from datetime import datetime, timedelta

import pytest

from app.broker import BrokerAccount
from app.domain import Bar, Fill, OrderRequest, StrategyConfig
from app.strategy import LayeredTStrategy


def daily(day: int, close: float, low: float | None = None, volume: float = 1_000) -> Bar:
    low = low if low is not None else close * 0.95
    return Bar(
        timestamp=datetime(2025, 1, 1) + timedelta(days=day), symbol="TEST",
        open=close, high=close * 1.02, low=low, close=close,
        volume=volume, amount=close * volume, prev_close=close,
    )


def funded_account(price: float = 10.0) -> BrokerAccount:
    config = StrategyConfig(slippage_bps=0, volume_participation=1.0)
    account = BrokerAccount(4_000_000, config)
    account.new_day("2024-12-31")
    account.submit(OrderRequest(symbol="TEST", side="BUY", quantity=1_000, execution_mode="NEXT_BAR"))
    account.process_bar(daily(-1, price))
    account.new_day("2025-01-01")
    account.sequence_index = 4
    return account


def test_index_gate_opens_when_index_20d_return_is_negative():
    account = BrokerAccount(4_000_000, StrategyConfig(slippage_bps=0, volume_participation=1.0))
    strategy = LayeredTStrategy(account.config)
    history = [daily(1, 10.0)]
    index_history = [
        Bar(timestamp=datetime(2025, 1, 1) + timedelta(days=day), symbol="INDEX",
            open=100, high=101, low=99, close=100, volume=1000, amount=100000)
        for day in range(21)
    ]
    for item in index_history[:-1]:
        item.close = 110.0
    index_history[-1].close = 100.0
    requests = strategy.on_daily(history, index_history, account, 1, False)
    assert len(requests) == 1
    assert requests[0].reason == "换票日首笔"
    assert requests[0].execution_mode == "NEXT_OPEN"


def test_oversold_a3_is_t_plus_one_without_lookahead():
    account = funded_account()
    strategy = LayeredTStrategy(account.config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    history = [daily(i, 10, low=9.5) for i in range(5)]
    history.append(daily(5, 9.0, low=8.8, volume=1_000))
    assert strategy.on_daily(history, [], account, 5, False) == []
    assert strategy.had_new_low is True

    initial_trade_count = len(account.trades)
    history.append(daily(6, 9.5, low=9.1, volume=100))
    requests = strategy.on_daily(history, [], account, 6, False)
    assert len(requests) == 1
    assert requests[0].reason == "浮亏超跌A3"
    assert requests[0].execution_mode == "NEXT_OPEN"
    expected_quantity = int(200_000 / 9.5 / 100) * 100
    assert requests[0].quantity == expected_quantity
    assert len(account.trades) == initial_trade_count

    # 下一交易日开盘价只影响成交，不影响已经形成的A3信号。
    next_open = daily(7, 9.2, low=9.0)
    order = account.submit(requests[0], history[-1].timestamp)
    fills = account.process_bar(next_open, modes={"NEXT_OPEN"})
    assert order.created_at == history[-1].timestamp.isoformat()
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(9.2)
    assert fills[0].timestamp == next_open.timestamp.isoformat()


def test_price_track_take_profit_liquidates():
    account = funded_account()
    strategy = LayeredTStrategy(account.config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    requests = strategy.on_daily([daily(1, 11.6)], [], account, 1, False)
    assert any(item.side == "SELL" and item.purpose == "PERMANENT" for item in requests)
    assert strategy.liquidating is True
    assert strategy.close_reason == "价格轨收益达到15%"
    assert all(item.execution_mode == "NEXT_OPEN" for item in requests)


def test_max_holding_days_overrides_take_profit_to_3pct():
    """持有超过 max_holding_days 交易日后，止盈阈值降为 holding_tp_override。

    回归：策略只有 15% 止盈 + Armed 量价清仓，没有硬止损；横盘/缓慢阴跌标的会让
    资金被长期套牢。降到 3% 让超期标的尽快让位给下一轮。
    """
    account = funded_account()
    config = StrategyConfig(
        slippage_bps=0, volume_participation=1.0,
        max_holding_days=22, holding_tp_override=0.03,
    )
    strategy = LayeredTStrategy(config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    strategy.round_entry_date = "2025-01-01"

    # 持有 20 天（未超期），浮盈 5%：未达 15%，也不该被 3% 提前清仓。
    history_short = [daily(i, 10.5) for i in range(1, 21)]
    assert strategy.on_daily(history_short, [], account, 20, False) == []
    assert strategy.liquidating is False

    # 持有 25 天（超期），同样 5% 浮盈：3% 阈值应立即触发清仓。
    strategy.liquidating = False
    strategy.close_reason = ""
    history_long = [daily(i, 10.5) for i in range(1, 26)]
    requests = strategy.on_daily(history_long, [], account, 25, False)
    assert any(item.side == "SELL" and item.purpose == "PERMANENT" for item in requests)
    assert strategy.liquidating is True
    # close_reason 带实际持有天数与降阈后的止盈百分比
    assert "3%" in strategy.close_reason
    assert "25" in strategy.close_reason


def test_max_holding_days_override_requires_both_fields():
    """只配 max_holding_days 不配 holding_tp_override：保持原止盈，不降阈。"""
    account = funded_account()
    config = StrategyConfig(
        slippage_bps=0, volume_participation=1.0, max_holding_days=22,
    )
    strategy = LayeredTStrategy(config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    strategy.round_entry_date = "2025-01-01"
    history = [daily(i, 10.5) for i in range(1, 30)]
    assert strategy.on_daily(history, [], account, 29, False) == []


def test_force_exit_holding_days_liquidates_when_held_long():
    """硬性最大持仓天数：持有 ≥N 个交易日后无条件强平（0 = 不限/关闭）。

    止盈未触发（+5% < 15%）、Armed 也未进时，靠天数硬兜底强制清仓，避免资金长期套牢。
    """
    account = funded_account()
    config = StrategyConfig(
        slippage_bps=0, volume_participation=1.0, force_exit_holding_days=5,
    )
    strategy = LayeredTStrategy(config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    strategy.round_entry_date = "2025-01-01"
    # 持有 10 天（≥ 5），浮盈 5%：未达 15% 止盈，但天数硬顶触发强平。
    history = [daily(i, 10.5) for i in range(1, 11)]
    requests = strategy.on_daily(history, [], account, 10, False)
    assert any(item.side == "SELL" and item.purpose == "PERMANENT" for item in requests)
    assert strategy.liquidating is True
    assert strategy.close_reason == "最大持仓5日强平"


def test_force_exit_holding_days_zero_disables():
    """force_exit_holding_days = 0（默认）：不强制清仓，长期持仓也不触发。"""
    account = funded_account()
    config = StrategyConfig(slippage_bps=0, volume_participation=1.0)
    strategy = LayeredTStrategy(config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    strategy.round_entry_date = "2025-01-01"
    history = [daily(i, 10.5) for i in range(1, 30)]
    assert strategy.on_daily(history, [], account, 29, False) == []
    assert strategy.liquidating is False


def test_force_exit_holding_days_validation():
    """force_exit_holding_days 必须在 0~250 之间（0 = 不限）。"""
    with pytest.raises(ValueError):
        StrategyConfig(slippage_bps=0, volume_participation=1.0, force_exit_holding_days=-1)
    with pytest.raises(ValueError):
        StrategyConfig(slippage_bps=0, volume_participation=1.0, force_exit_holding_days=251)
    assert StrategyConfig(
        slippage_bps=0, volume_participation=1.0, force_exit_holding_days=30
    ).force_exit_holding_days == 30


def test_stop_loss_liquidates_when_loss_hits_threshold():
    """硬止损：持仓价格收益 ≤ 止损线即立即清仓（不等最大持仓天数强平）。"""
    account = funded_account()  # 建仓成本 10.0
    config = StrategyConfig(slippage_bps=0, volume_participation=1.0, stop_loss_pct=-0.10)
    strategy = LayeredTStrategy(config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    strategy.round_entry_date = "2025-01-01"
    # 持有 3 天，浮亏 -10%（9.0/10.0-1）：触发硬止损清仓。
    history = [daily(i, 9.0) for i in range(1, 4)]
    requests = strategy.on_daily(history, [], account, 3, False)
    assert any(item.side == "SELL" and item.purpose == "PERMANENT" for item in requests)
    assert strategy.liquidating is True
    assert strategy.close_reason == "止损10%"


def test_stop_loss_zero_disables():
    """stop_loss_pct = 0（默认）：不触发硬止损，浮亏也不清仓（向后兼容）。"""
    account = funded_account()  # 建仓成本 10.0
    config = StrategyConfig(slippage_bps=0, volume_participation=1.0)  # stop_loss_pct 默认 0
    strategy = LayeredTStrategy(config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    strategy.round_entry_date = "2025-01-01"
    history = [daily(i, 9.0) for i in range(1, 30)]  # 浮亏 -10%，但止损关闭
    strategy.on_daily(history, [], account, 29, False)
    assert strategy.liquidating is False


def test_stop_loss_validation():
    """stop_loss_pct 必须在 [-1, 0] 内（0 = 关闭）。"""
    with pytest.raises(ValueError):
        StrategyConfig(slippage_bps=0, volume_participation=1.0, stop_loss_pct=-1.5)
    with pytest.raises(ValueError):
        StrategyConfig(slippage_bps=0, volume_participation=1.0, stop_loss_pct=0.5)
    assert StrategyConfig(
        slippage_bps=0, volume_participation=1.0, stop_loss_pct=-0.10
    ).stop_loss_pct == -0.10


def test_round_entry_date_survives_state_roundtrip():
    """state_dict/load_state 必须保留 round_entry_date：live 模拟盘重启后超期降阈仍生效。"""
    account = funded_account()
    strategy = LayeredTStrategy(account.config)
    strategy.round_entry_date = "2025-01-15"
    restored = LayeredTStrategy(account.config)
    restored.load_state(strategy.state_dict())
    assert restored.round_entry_date == "2025-01-15"


def test_holding_days_counts_only_bars_after_entry():
    account = funded_account()
    strategy = LayeredTStrategy(account.config)
    strategy.round_entry_date = "2025-01-05"
    history = [daily(i, 10.0) for i in range(1, 11)]
    # daily(i) 的 date = 2025-01-01 + i 天；i=1..10 → 01-02..01-11。
    # 严格晚于 01-05 的是 i=5..10 → 6 天（建仓次日起算）。
    assert strategy._holding_days(history) == 6
    strategy.round_entry_date = None
    assert strategy._holding_days(history) == 0


def test_t_min_base_entries_default_allows_first_entry():
    """做T门槛 t_min_base_entries：默认 1（首笔建仓后即可做T）；=4 回到旧行为。

    直接测 _t_eligible 的 sequence 门槛：构造满足振幅/换手/盘口/价差的 bar，
    只让 sequence_index 变化，验证门槛由 t_min_base_entries 控制。
    （合并 feat/t-gate-and-a3-relax 时把该分支的 t_min_sequence 收编到本字段，
    避免两套平行的做T门槛配置——单一真相源。）
    """
    def make_bar() -> Bar:
        bar = daily(6, 10.0, low=9.0, volume=100_000)
        bar.turnover_rate = 0.05
        bar.bid, bar.ask = 9.99, 10.01
        return bar

    history = [daily(i, 10.0, low=9.6) for i in range(1, 6)]

    # 默认 t_min_base_entries=1：sequence_index=1 即过门槛
    account = funded_account()
    account.sequence_index = 1
    strategy = LayeredTStrategy(StrategyConfig(slippage_bps=0, volume_participation=1.0))
    assert strategy._t_eligible(history, make_bar(), account,
                                daily_amplitude=0.05, daily_turnover_rate=0.05) is True

    # t_min_base_entries=4：sequence_index=1 不够，门槛挡住
    strategy2 = LayeredTStrategy(StrategyConfig(
        slippage_bps=0, volume_participation=1.0, t_min_base_entries=4,
    ))
    account2 = funded_account()
    account2.sequence_index = 1
    assert strategy2._t_eligible(history, make_bar(), account2,
                                 daily_amplitude=0.05, daily_turnover_rate=0.05) is False

    # t_min_base_entries=4 且 sequence_index=4：过门槛（回归旧行为）
    account3 = funded_account()
    account3.sequence_index = 4
    assert strategy2._t_eligible(history, make_bar(), account3,
                                 daily_amplitude=0.05, daily_turnover_rate=0.05) is True


def test_a3_relaxed_volume_and_no_new_low():
    """A3 放宽：oversold_volume_ratio=0.8 + a3_require_new_low=False 时更易触发加仓。

    取 sequence_index=12（档4 / tier 3）——只有这一档同时受「缩量比例」与
    「先破新低」两条约束，才能测到放宽的差异。
    """
    # 构造阴跌序列：建仓 @10，之后阴跌到 9.0，某日缩量反弹到前3根最低之上
    account = funded_account()
    account.sequence_index = 12
    # 放宽配置：缩量阈值 0.8 + 不要求破新低
    config = StrategyConfig(
        slippage_bps=0, volume_participation=1.0,
        oversold_volume_ratio=0.8, a3_require_new_low=False,
    )
    strategy = LayeredTStrategy(config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    # 阴跌：i=1..6，收盘从 9.8 跌到 9.0，最后一根温和缩量反弹
    closes = [10.0, 9.8, 9.6, 9.4, 9.2, 9.0, 9.1]
    history = []
    for i, close in enumerate(closes):
        volume = 1000 if i < 6 else 700  # 最后一根缩量（< 5日均量×0.8）
        history.append(daily(i, close, low=close * 0.99, volume=volume))
    # 放宽后应触发 A3
    assert strategy._add_signal(history, account) == "浮亏超跌A3"

    # 严格配置（旧默认 0.5 + 要求破新低）：同样数据不应触发
    strict = LayeredTStrategy(StrategyConfig(
        slippage_bps=0, volume_participation=1.0,
        oversold_volume_ratio=0.5, a3_require_new_low=True,
    ))
    strict.last_add_price = 10.0
    strict.last_add_session = 0
    assert strict._add_signal(history, account) == ""


def test_a3_tier_off_keeps_original_strict_a3():
    """add_tier_strict=False 保持原始 A3 严格口径：即使 a3_require_new_low=False 也要求破过新低。"""
    account = funded_account()
    account.sequence_index = 12
    strategy = LayeredTStrategy(StrategyConfig(
        slippage_bps=0, volume_participation=1.0,
        oversold_volume_ratio=0.8, a3_require_new_low=False,
        add_tier_strict=False,
    ))
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    closes = [10.0, 9.8, 9.6, 9.4, 9.2, 9.0, 9.1]
    history = [
        daily(i, close, low=close * 0.99, volume=1000 if i < 6 else 700)
        for i, close in enumerate(closes)
    ]
    # 未破过新低（had_new_low 初始 False）→ 原始 A3 要求 had_low_before → 不触发
    assert strategy._add_signal(history, account) == ""


def test_default_a3_relaxation_enabled():
    """默认值即放宽：oversold_volume_ratio=0.80、a3_require_new_low=False、t_min_base_entries=1。"""
    cfg = StrategyConfig()
    assert cfg.oversold_volume_ratio == 0.80
    assert cfg.a3_require_new_low is False
    assert cfg.t_min_base_entries == 1


def test_account_track_includes_t_net_spread():
    account = funded_account()
    account.t_net_profit = 600.0
    strategy = LayeredTStrategy(account.config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    price_return = (
        account.position("TEST").permanent_qty * 11.0
        - account.position("TEST").permanent_cost
    ) / account.position("TEST").permanent_cost
    assert price_return < 0.15
    requests = strategy.on_daily([daily(1, 11.0)], [], account, 1, False)
    assert strategy.liquidating is True
    assert strategy.close_reason == "账户轨收益达到15%"
    assert requests[0].side == "SELL"


def test_tiered_a3_add_strict_levels():
    """按档位「先松后紧」：档1（sequence 0-3）宽松即触发 A3，档4 需过缩量比例线。

    构造一根缩量阴跌 bar（量 = 5日均量×0.9，缩到 0.9 但未到档4 的 0.8 线）：
    - 档1（最松，仅要求缩量 < 5日均量）→ 触发；
    - 档4（最严，缩量须 < 均量×oversold_volume_ratio，默认 0.8）→ 不触发。
    """
    def build(closes, volumes):
        return [daily(i, c, low=c * 0.99, volume=v)
                for i, (c, v) in enumerate(zip(closes, volumes))]

    # 阴跌序列：末根 close 站回前3根最低之上、量 = 5日均量×0.9（缩量但 > 0.8 线）
    closes = [10.0, 9.8, 9.6, 9.4, 9.3, 9.1]
    volumes = [1000, 1000, 1000, 1000, 1000, 900]
    history = build(closes, volumes)

    # 档1（sequence_index=0）：最松，缩量即触发
    acct1 = funded_account()
    acct1.sequence_index = 0
    strat1 = LayeredTStrategy(StrategyConfig(slippage_bps=0, volume_participation=1.0))
    strat1.last_add_price = 10.0
    strat1.last_add_session = 0
    assert strat1._add_signal(history, acct1) == "浮亏超跌A3"

    # 档4（sequence_index=12）：缩量 900 ≥ 均量×0.8=800 → 不触发
    acct4 = funded_account()
    acct4.sequence_index = 12
    strat4 = LayeredTStrategy(StrategyConfig(slippage_bps=0, volume_participation=1.0))
    strat4.last_add_price = 10.0
    strat4.last_add_session = 0
    assert strat4._add_signal(history, acct4) == ""


def test_tiered_a3_disabled_uses_uniform_original():
    """add_tier_strict=False：全档统一用原始 A3（档1 位置也不再放宽）。"""
    closes = [10.0, 9.8, 9.6, 9.4, 9.3, 9.1]
    volumes = [1000, 1000, 1000, 1000, 1000, 900]
    history = [daily(i, c, low=c * 0.99, volume=v)
               for i, (c, v) in enumerate(zip(closes, volumes))]
    acct = funded_account()
    acct.sequence_index = 0  # 档1 位置
    strat = LayeredTStrategy(StrategyConfig(
        slippage_bps=0, volume_participation=1.0, add_tier_strict=False,
    ))
    strat.last_add_price = 10.0
    strat.last_add_session = 0
    strat.had_new_low = True  # 让「破新低」前置通过，缩量比例成为唯一决定项
    # 全档统一路径：缩量须 < 均量×oversold_volume_ratio（默认 0.8）→ 900 ≥ 800 不满足
    assert strat._add_signal(history, acct) == ""


def test_deep_drop_t_bypasses_sequence_gate():
    """深跌幅当日做T：当日跌幅 ≥ t_deep_drop_pct(10%) 时，sequence_index=1 也过门槛。

    显式 t_min_base_entries=4：本用例依赖「底仓笔数门槛拦下」，
    不能隐式依赖默认值（默认值已改为 1，见 feat/t-gate-and-a3-relax）。
    """
    def deep_drop_bar(drop_pct: float) -> Bar:
        bar = daily(6, 10.0 * (1 - drop_pct), low=10.0 * (1 - drop_pct) * 0.99,
                    volume=100_000)
        bar.prev_close = 10.0
        bar.turnover_rate = 0.05
        bar.bid, bar.ask = 9.99, 10.01
        return bar

    history = [daily(i, 10.0, low=9.6) for i in range(1, 6)]
    # 跌 12%（≥10%）→ 旁路生效，sequence_index=1 也过门槛
    account = funded_account()
    account.sequence_index = 1
    strategy = LayeredTStrategy(StrategyConfig(
        slippage_bps=0, volume_participation=1.0, t_min_base_entries=4,
    ))
    assert strategy._t_eligible(history, deep_drop_bar(0.12), account,
                                daily_amplitude=0.05, daily_turnover_rate=0.05) is True
    # 跌 5%（<10%）→ 旁路不生效，sequence_index=1 被底仓笔数门槛挡住
    assert strategy._t_eligible(history, deep_drop_bar(0.05), account,
                                daily_amplitude=0.05, daily_turnover_rate=0.05) is False


def test_t_min_base_entries_gate_configurable():
    """底仓笔数门槛可配置：t_min_base_entries=2 时 sequence_index=2 过门槛，=1 仍被挡。

    之前硬编码为 4（「第5笔起正T」）；可视化后可调，深跌幅旁路仍可绕过。
    """
    history = [daily(i, 10.0, low=9.6) for i in range(1, 6)]
    bar = daily(6, 10.0, low=9.6, volume=100_000)
    bar.turnover_rate = 0.05
    bar.bid, bar.ask = 9.99, 10.01
    strategy = LayeredTStrategy(StrategyConfig(
        slippage_bps=0, volume_participation=1.0, t_min_base_entries=2,
    ))
    account = funded_account()
    account.sequence_index = 2
    assert strategy._t_eligible(history, bar, account,
                                daily_amplitude=0.05, daily_turnover_rate=0.05) is True
    # seq=1 < 门槛 2 → 被底仓笔数门槛挡住
    account.sequence_index = 1
    assert strategy._t_eligible(history, bar, account,
                                daily_amplitude=0.05, daily_turnover_rate=0.05) is False


def test_t_min_base_entries_validation():
    """t_min_base_entries 必须在 1~16 笔之间。"""
    with pytest.raises(ValueError):
        StrategyConfig(slippage_bps=0, volume_participation=1.0, t_min_base_entries=0)
    with pytest.raises(ValueError):
        StrategyConfig(slippage_bps=0, volume_participation=1.0, t_min_base_entries=17)
    assert StrategyConfig(
        slippage_bps=0, volume_participation=1.0, t_min_base_entries=3
    ).t_min_base_entries == 3


def test_deep_drop_pct_validation():
    """t_deep_drop_pct 开启时须 (0,1]；10% 以下不再报错（可设低门槛做T回测）。"""
    # 10% 以下现在合法（放宽原 ≥10% 恐慌线约束）
    StrategyConfig(slippage_bps=0, volume_participation=1.0, t_deep_drop_pct=0.05)
    StrategyConfig(slippage_bps=0, volume_participation=1.0, t_deep_drop_pct=0.02)
    # None=关闭，合法
    StrategyConfig(slippage_bps=0, volume_participation=1.0, t_deep_drop_pct=None)
    # 0 视为关闭（归一化为 None）：前端开关关掉时传 0
    assert StrategyConfig(slippage_bps=0, volume_participation=1.0, t_deep_drop_pct=0).t_deep_drop_pct is None
    # 越界（<=0 已归一化为关闭；>1 或负数）报错
    with pytest.raises(ValueError):
        StrategyConfig(slippage_bps=0, volume_participation=1.0, t_deep_drop_pct=1.5)
    with pytest.raises(ValueError):
        StrategyConfig(slippage_bps=0, volume_participation=1.0, t_deep_drop_pct=-0.05)


def test_tiered_a3_spec_progression():
    """档位规范语义（feat/t-gate-and-a3-relax 合入后，2026-11-27 起）：

    缩量比例线只在档4（sequence 12-15）生效，比例 = oversold_volume_ratio（默认 0.80）；
    档3 的「此前破过新低」前置由 a3_require_new_low 控制（默认 False = 不要求）。

    场景A（默认配置）：量 = 5日均量×0.9（缩量但 > 0.8 线）、close 9.8 站回前3日低点
    （9.306）之上、无新低记录 →
      档1（seq 0-3）触发（仅缩量 < 均量）、档2（seq 4-7）触发（+站回前3日低点）、
      档3（seq 8-11）触发（a3_require_new_low=False 时不再要求破新低）、
      档4（seq 12-15）不触发（量须 < 均量×0.8）。
      若实现把缩量比例线提前到档3，seq 8 会不触发——本测试即分歧哨兵。

    场景B：a3_require_new_low=True → 档3 回到「必须破过新低」：无新低记录不触发，
    显式置位 had_new_low 后触发。
    """
    closes = [10.0, 10.2, 10.5, 9.6, 9.4, 9.8]
    volumes = [1000, 1000, 1000, 1000, 1000, 900]
    history = [daily(i, c, low=c * 0.99, volume=v)
               for i, (c, v) in enumerate(zip(closes, volumes))]

    def signal_for(sequence_index: int, *, require_new_low: bool,
                   had_new_low: bool) -> str:
        acct = funded_account()
        acct.sequence_index = sequence_index
        strat = LayeredTStrategy(StrategyConfig(
            slippage_bps=0, volume_participation=1.0,
            a3_require_new_low=require_new_low,
        ))
        strat.last_add_price = 10.0
        strat.last_add_session = 0
        strat.had_new_low = had_new_low
        return strat._add_signal(history, acct)

    # 场景A（默认）：档1/2/3 触发，档4 被缩量比例线挡住
    for sequence_index, expected in ((0, "浮亏超跌A3"), (4, "浮亏超跌A3"),
                                     (8, "浮亏超跌A3"), (12, "")):
        assert signal_for(sequence_index, require_new_low=False,
                         had_new_low=False) == expected, (
            f"默认配置 sequence_index={sequence_index} 应为 {expected!r}"
        )

    # 场景B：要求破新低时，档3 无新低记录不触发；显式置位后触发；档4 仍被比例线挡住
    assert signal_for(8, require_new_low=True, had_new_low=False) == ""
    assert signal_for(8, require_new_low=True, had_new_low=True) == "浮亏超跌A3"
    assert signal_for(12, require_new_low=True, had_new_low=True) == ""


def test_add_gap_uses_fill_date_immune_to_dataset_drift():
    """加仓间隔按成交日动态换算，免疫数据集行数漂移（2026-11-16 联特误拦事故哨兵）。

    周五（01-09）买入；周一的 dataset 仍只有至周五的 bar 且行数变少（5→3，去重/补数漂移）——
    旧行序号口径：session_index(3) - last_add_session(4) = -1 误拦；
    日期口径：gap = 1 个交易日 → 放行，周一即可加仓。
    """
    acct = funded_account()
    acct.sequence_index = 1
    acct.trades = [
        Fill(
            id="F1", order_id="O1", symbol="TEST", side="BUY", quantity=1000,
            price=10.0, amount=10000.0, commission=1.0, stamp_tax=0.0,
            transfer_fee=0.3, total_fee=1.3, slippage=0.0,
            timestamp="2025-01-09T14:35:00", purpose="PERMANENT", cash_flow=-10001.3,
        )
    ]
    strat = LayeredTStrategy(StrategyConfig(slippage_bps=0, volume_participation=1.0))
    strat.last_add_price = 10.0
    strat.last_add_session = 4  # 买入时按旧数据集（5 根）算的序号
    history = [daily(6, 10.0), daily(7, 10.0), daily(8, 10.0)]  # 01-07/08/09
    # 周一（01-10）：日期口径 gap=1 → 放行（旧行序号口径会因漂移误拦）
    assert strat._add_gap_blocked(history, daily(9, 9.8), acct, session_index=3) is False
    # 成交同日：gap=0 → 拦（当日不加仓）
    assert strat._add_gap_blocked(history, daily(8, 9.9), acct, session_index=3) is True
    # min_add_gap_days=2：周一（01-10）gap=1 仍拦；补上 01-10 的 bar 后次日（01-11）gap=2 放行
    strat.config.min_add_gap_days = 2
    assert strat._add_gap_blocked(history, daily(9, 9.8), acct, session_index=3) is True
    history2 = history + [daily(9, 10.1)]
    assert strat._add_gap_blocked(history2, daily(10, 9.8), acct, session_index=3) is False


def test_add_gap_fallback_to_session_index_without_fills():
    """无成交记录的老账户：回退旧行序号口径（兼容）。"""
    acct = funded_account()
    acct.trades = []
    acct.sequence_index = 1
    strat = LayeredTStrategy(StrategyConfig(slippage_bps=0, volume_participation=1.0))
    strat.last_add_price = 10.0
    strat.last_add_session = 4
    history = [daily(6, 10.0), daily(7, 10.0), daily(8, 10.0)]
    # session_index=3 → 3-4 = -1 < 1 → 拦（旧行为不变）
    assert strat._add_gap_blocked(history, daily(9, 10.0), acct, session_index=3) is True
    # session_index=5 → 1 → 放行
    assert strat._add_gap_blocked(history, daily(10, 10.0), acct, session_index=5) is False


def test_same_intraday_bar_is_only_evaluated_once():
    """live-tick 在同一根已收盘 5 分钟 K 线上重复轮询时，不能重复累加确认计数。"""
    account = funded_account()
    strategy = LayeredTStrategy(account.config)
    history = [daily(i, 11.2, low=10.64, volume=1_000) for i in range(5)]
    bar = daily(6, 11.2)
    bar.timestamp = bar.timestamp.replace(hour=10, minute=5)
    calls: list[Bar] = []
    strategy._risk_requests = lambda *args, **kwargs: calls.append(args[1]) or []  # type: ignore[method-assign]

    strategy.on_intraday(history, [bar], bar, account)
    strategy.on_intraday(history, [bar], bar, account)

    assert calls == [bar]
    assert strategy.last_intraday_timestamp == bar.timestamp


def test_armed_disables_t_entry():
    account = funded_account()
    strategy = LayeredTStrategy(account.config)
    strategy.armed = True
    history = [daily(i, 11.2, low=10.64, volume=1_000) for i in range(5)]
    for item in history:
        item.turnover_rate = 0.05
        item.high = 11.76
        item.low = 10.64
        item.prev_close = 11.2
    tick = daily(6, 11.2)
    tick.bid = 11.19
    tick.ask = 11.21
    tick.timestamp = tick.timestamp.replace(hour=10, minute=0)
    assert strategy.on_intraday(history, [tick], tick, account) == []


# ---------------------------------------------------------------------------
# 尾盘首仓（14:30–14:50，下一根 5 分钟 K 撮合）
# ---------------------------------------------------------------------------


def intraday_bar(hour: int, minute: int, close: float = 10.0, day: int = 6) -> Bar:
    bar = daily(day, close)
    return bar.__class__(
        timestamp=bar.timestamp.replace(hour=hour, minute=minute), symbol=bar.symbol,
        open=bar.open, high=bar.high, low=bar.low, close=bar.close,
        volume=bar.volume, amount=bar.amount, prev_close=bar.prev_close,
    )


def index_series(*, falling: bool = True, count: int = 21) -> list[Bar]:
    """21 根严格早于当日的指数日线；falling 时 20 日收益 < 0（大盘门开门）。"""
    start = datetime(2024, 12, 1)
    closes = [110.0 - index for index in range(count)] if falling else [
        100.0 + index for index in range(count)
    ]
    return [
        Bar(timestamp=start + timedelta(days=index), symbol="INDEX",
            open=close, high=close + 1, low=close - 1, close=close,
            volume=1_000, amount=100_000)
        for index, close in enumerate(closes)
    ]


def flat_account() -> BrokerAccount:
    return BrokerAccount(4_000_000, StrategyConfig(slippage_bps=0, volume_participation=1.0))


def test_tail_entry_buys_between_1430_and_1450():
    account = flat_account()
    strategy = LayeredTStrategy(account.config)
    bar = intraday_bar(14, 35)
    requests = strategy.on_intraday(
        [daily(1, 10.0)], [bar], bar, account, index_history=index_series(),
    )
    assert len(requests) == 1
    assert requests[0].side == "BUY" and requests[0].purpose == "PERMANENT"
    assert requests[0].reason == "尾盘首笔"
    assert requests[0].execution_mode == "NEXT_BAR"
    assert requests[0].quantity == 10_000  # 10万 / 10元 / 100 * 100
    assert strategy.entry_submitted_date == bar.date


def test_tail_entry_window_is_1430_inclusive_1450_exclusive():
    for hour, minute in ((14, 0), (14, 29), (14, 50), (14, 55), (15, 0)):
        account = flat_account()
        strategy = LayeredTStrategy(account.config)
        bar = intraday_bar(hour, minute)
        assert strategy.on_intraday(
            [daily(1, 10.0)], [bar], bar, account, index_history=index_series(),
        ) == [], f"{hour:02d}:{minute:02d} 不应发尾盘首仓"
        assert strategy.entry_submitted_date is None

    account = flat_account()
    strategy = LayeredTStrategy(account.config)
    bar = intraday_bar(14, 30)
    assert len(strategy.on_intraday(
        [daily(1, 10.0)], [bar], bar, account, index_history=index_series(),
    )) == 1


def test_tail_entry_respects_index_gate():
    account = flat_account()
    strategy = LayeredTStrategy(account.config)
    bar = intraday_bar(14, 35)
    # 指数 20 日收益 > 0 → 大盘门关门，不发首仓
    assert strategy.on_intraday(
        [daily(1, 10.0)], [bar], bar, account, index_history=index_series(falling=False),
    ) == []
    assert strategy.entry_submitted_date is None


def test_tail_entry_uses_only_prior_day_index_bars():
    """盘中不能用当日尚未收盘的指数日线（避免未来函数）。"""
    account = flat_account()
    strategy = LayeredTStrategy(account.config)
    bar = intraday_bar(14, 35)
    same_day = Bar(timestamp=bar.timestamp.replace(hour=0, minute=0), symbol="INDEX",
                   open=50, high=51, low=49, close=50, volume=1_000, amount=100_000)
    history = index_series(falling=True) + [same_day]
    assert len(strategy.on_intraday(
        [daily(1, 10.0)], [bar], bar, account, index_history=history,
    )) == 1


def test_tail_entry_submitted_once_per_day():
    account = flat_account()
    strategy = LayeredTStrategy(account.config)
    first = intraday_bar(14, 35)
    second = intraday_bar(14, 40)
    assert len(strategy.on_intraday(
        [daily(1, 10.0)], [first], first, account, index_history=index_series(),
    )) == 1
    assert strategy.on_intraday(
        [daily(1, 10.0)], [second], second, account, index_history=index_series(),
    ) == []


def test_daily_signal_does_not_repeat_first_buy_after_tail_entry():
    """同日尾盘已发首仓后，15:00 日终信号不能再发一笔首仓。"""
    account = flat_account()
    strategy = LayeredTStrategy(account.config)
    bar = intraday_bar(14, 35)
    assert len(strategy.on_intraday(
        [daily(1, 10.0)], [bar], bar, account, index_history=index_series(),
    )) == 1
    assert strategy.on_daily(
        [daily(6, 10.0)], index_series(), account, 6, True,
    ) == []


def test_tail_entry_does_not_touch_t_orders_after_sequence_started():
    """序列已开（≥1 笔）时尾盘不建首仓，做 T 逻辑口径不变。"""
    account = funded_account()  # sequence_index = 4
    strategy = LayeredTStrategy(account.config)
    bar = intraday_bar(14, 35, close=11.2)
    bar.turnover_rate = 0.05
    bar.bid = 11.19
    bar.ask = 11.21
    requests = strategy.on_intraday(
        [daily(index, 11.2) for index in range(5)], [bar], bar, account,
        index_history=index_series(),
    )
    assert all(item.reason != "尾盘首笔" for item in requests)


# ---------------------------------------------------------------------------
# 尾盘加仓（14:30–14:50，与日线加仓共用信号口径）
# ---------------------------------------------------------------------------


def held_account(price: float = 10.0, quantity: int = 1_000) -> BrokerAccount:
    """已建首仓的账户：永久仓 1000 股 @10，sequence_index=1。"""
    account = BrokerAccount(4_000_000, StrategyConfig(slippage_bps=0, volume_participation=1.0))
    account.new_day("2025-01-06")
    account.submit(OrderRequest(symbol="TEST", side="BUY", quantity=quantity, execution_mode="NEXT_BAR"))
    fill_bar = daily(5, price)
    fill_bar.timestamp = fill_bar.timestamp.replace(hour=9, minute=35)
    account.process_bar(fill_bar, modes={"NEXT_BAR"})
    assert account.sequence_index == 1
    return account


def add_history() -> list[Bar]:
    """前 5 根日线：低位横盘，其中一根高点 12（用于触发浮盈右侧回撤）。"""
    history = [daily(index, 10.0, low=10.4) for index in range(1, 6)]
    history[2].high = 12.0
    return history


def tail_add_bar(hour: int = 14, minute: int = 35, close: float = 10.9) -> Bar:
    bar = intraday_bar(hour, minute, close=close)
    bar.open = close
    bar.high = close
    bar.low = close
    return bar


def make_add_strategy(account: BrokerAccount) -> LayeredTStrategy:
    strategy = LayeredTStrategy(account.config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    return strategy


def test_tail_add_buys_between_1430_and_1450():
    account = held_account()
    strategy = make_add_strategy(account)
    bar = tail_add_bar()
    requests = strategy.on_intraday(
        add_history(), [bar], bar, account, index_history=index_series(), session_index=1,
    )
    assert len(requests) == 1
    assert requests[0].side == "BUY" and requests[0].purpose == "PERMANENT"
    assert requests[0].reason == "浮盈右侧回撤5%"
    assert requests[0].execution_mode == "NEXT_BAR"
    assert requests[0].quantity == 9_100  # 10万 / 10.9 / 100 * 100
    assert strategy.add_submitted_date == bar.date


def test_tail_add_requires_one_session_gap():
    """尾盘加仓需距最近一笔永久仓成交 ≥1 个交易日（按成交日期口径）。"""
    account = held_account()
    # 把成交日挪到与尾盘 bar 同日（01-07）：gap = 0 → 拦
    for fill in account.trades:
        fill.timestamp = "2025-01-07T09:35:00"
    strategy = make_add_strategy(account)
    bar = tail_add_bar()
    assert strategy.on_intraday(
        add_history(), [bar], bar, account, session_index=0,
    ) == []
    assert strategy.add_submitted_date is None


def test_tail_add_window_is_1430_inclusive_1450_exclusive():
    for hour, minute in ((14, 0), (14, 29), (14, 50), (15, 0)):
        account = held_account()
        strategy = make_add_strategy(account)
        bar = tail_add_bar(hour, minute)
        assert strategy.on_intraday(
            add_history(), [bar], bar, account, session_index=1,
        ) == [], f"{hour:02d}:{minute:02d} 不应发尾盘加仓"
        assert strategy.add_submitted_date is None


def test_tail_add_submitted_once_per_day():
    account = held_account()
    strategy = make_add_strategy(account)
    first = tail_add_bar(14, 35)
    assert len(strategy.on_intraday(add_history(), [first], first, account, session_index=1)) == 1
    second = tail_add_bar(14, 40)
    assert strategy.on_intraday(add_history(), [second], second, account, session_index=1) == []


def test_daily_signal_does_not_repeat_add_after_tail_add():
    """同日尾盘已发加仓后，15:00 日终信号不能再发一笔加仓。"""
    account = held_account()
    strategy = make_add_strategy(account)
    bar = tail_add_bar()
    assert len(strategy.on_intraday(add_history(), [bar], bar, account, session_index=1)) == 1
    today_daily = daily(6, 10.9, low=10.9)
    today_daily.high = 10.9
    assert strategy.on_daily(add_history() + [today_daily], [], account, 1, True) == []


def test_daily_add_still_works_without_intraday():
    """无盘中数据时，日线加仓路径保持原有 NEXT_OPEN 口径。"""
    account = held_account()
    strategy = make_add_strategy(account)
    today_daily = daily(6, 10.9, low=10.9)
    today_daily.high = 10.9
    requests = strategy.on_daily(add_history() + [today_daily], [], account, 1, False)
    assert len(requests) == 1
    assert requests[0].reason == "浮盈右侧回撤5%"
    assert requests[0].execution_mode == "NEXT_OPEN"
