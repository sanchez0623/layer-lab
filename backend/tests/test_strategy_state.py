"""策略状态加固测试：单一字段来源、状态版本化、不变式自愈（64f692bb 事故防线）。"""
from datetime import datetime

from app.broker import BrokerAccount
from app.domain import Bar, Fill, StrategyConfig
from app.strategy import (
    STATE_VERSION,
    STRATEGY_STATE_FIELDS,
    LayeredTStrategy,
    ensure_round_state,
)


def _fill(fill_id: str, date_text: str, price: float) -> Fill:
    return Fill(
        id=fill_id, order_id="O1", symbol="sz.301205", side="BUY", quantity=600,
        price=price, amount=price * 600, commission=1.0, stamp_tax=0.0,
        transfer_fee=0.3, total_fee=1.3, slippage=1.0,
        timestamp=f"{date_text}T14:50:33", purpose="PERMANENT", cash_flow=-price * 600,
    )


def _daily_history(dates: list[str]) -> list[Bar]:
    return [
        Bar(timestamp=datetime.fromisoformat(f"{d}T15:00:00"), symbol="sz.301205",
            open=1.0, high=2.0, low=0.5, close=1.5, volume=1000.0, amount=1500.0)
        for d in dates
    ]


def test_state_fields_are_single_source_of_truth():
    """实例属性 ⇆ 字段表双向完备：__init__ 新增状态字段漏登记 STRATEGY_STATE_FIELDS 即失败。

    config 不算策略状态（由账户配置注入、不进 state_dict），排除在外。
    """
    strategy = LayeredTStrategy(StrategyConfig())
    attrs = set(vars(strategy).keys()) - {"config"}
    spec_names = {name for name, _kind, _default in STRATEGY_STATE_FIELDS}
    assert attrs == spec_names, (
        f"字段表与实例属性不一致: 未登记={attrs - spec_names}, 多余={spec_names - attrs}"
    )


def test_state_dict_round_trip_preserves_all_fields():
    strategy = LayeredTStrategy(StrategyConfig())
    strategy.last_add_price = 145.9046
    strategy.last_add_session = 241
    strategy.had_new_low = True
    strategy.armed = True
    strategy.liquidating = False
    strategy.close_reason = "测试原因"
    strategy.divergence_count = 2
    strategy.vwap_count = 1
    strategy.account_return_peak = 0.123
    strategy.round_t_start = -10.5
    strategy.t_order_submitted = True
    strategy.t_exit_submitted = True
    strategy.t_traded_today = True
    strategy.recent_long_upper = True
    strategy.event_halt = False
    strategy.entry_submitted_date = "2026-11-13"
    strategy.add_submitted_date = None
    strategy.round_entry_date = "2026-11-10"
    strategy.last_intraday_timestamp = datetime(2026, 11, 16, 11, 20, 0)

    dump = strategy.state_dict()
    assert dump["state_version"] == STATE_VERSION

    restored = LayeredTStrategy(StrategyConfig())
    restored.load_state(dump)
    for name, _kind, _default in STRATEGY_STATE_FIELDS:
        assert getattr(restored, name) == getattr(strategy, name), name

    # 二次落库幂等（backtest checkpoint 依赖这一性质）
    assert restored.state_dict() == dump


def test_load_state_tolerates_explicit_nulls_and_garbage():
    """显式 null / 非法值一律回落缺省：老 payload 里的 null 字段不再炸 int()。"""
    strategy = LayeredTStrategy(StrategyConfig())
    dirty = {name: None for name, _kind, _default in STRATEGY_STATE_FIELDS}
    dirty["divergence_count"] = "not-a-number"
    strategy.load_state(dirty)
    assert strategy.last_add_price is None
    assert strategy.divergence_count == 0
    assert strategy.close_reason == ""
    assert strategy.last_intraday_timestamp is None

    # 无 state_version 的老结构也能读
    strategy.load_state({"last_add_price": 10.0})
    assert strategy.last_add_price == 10.0


def test_ensure_round_state_repairs_from_fills():
    """64f692bb 事故防线：序列>0 但状态被清空 → 从最近 sequence_index 笔永久仓买入自愈。"""
    strategy = LayeredTStrategy(StrategyConfig())  # 全空状态 = 被外部清空的场景
    account = BrokerAccount(initial_cash=1_000_000.0)
    account.sequence_index = 1
    account.trades = [_fill("F1", "2026-11-13", 145.9046)]
    history = _daily_history(["2026-11-10", "2026-11-11", "2026-11-12"])

    note = ensure_round_state(strategy, account, history)
    assert "自愈" in note
    assert strategy.last_add_price == 145.9046
    # 成交日 11-13 在日线里的会话序号 = 日期 ≤ 11-13 的 bar 数 - 1 = 2（与 on_fill 同口径）
    assert strategy.last_add_session == 2
    assert strategy.round_entry_date == "2026-11-13"


def test_ensure_round_state_uses_last_sequence_index_fills():
    """trades 里有跨轮成交时，本轮 = 最近 sequence_index 笔永久仓买入。"""
    strategy = LayeredTStrategy(StrategyConfig())
    account = BrokerAccount(initial_cash=1_000_000.0)
    account.sequence_index = 2
    account.trades = [
        _fill("F0", "2026-11-05", 100.0),  # 上一轮的成交，不参与本轮自愈
        _fill("F1", "2026-11-10", 120.0),
        _fill("F2", "2026-11-13", 145.9046),
    ]
    history = _daily_history(["2026-11-10", "2026-11-11", "2026-11-12"])

    note = ensure_round_state(strategy, account, history)
    assert "自愈" in note
    assert strategy.last_add_price == 145.9046
    assert strategy.round_entry_date == "2026-11-10"
    assert strategy.last_add_session == 2


def test_ensure_round_state_noop_when_consistent_and_warns_without_fills():
    strategy = LayeredTStrategy(StrategyConfig())
    strategy.last_add_price = 10.0
    strategy.last_add_session = 3
    strategy.round_entry_date = "2026-11-10"
    account = BrokerAccount(initial_cash=1_000_000.0)
    account.sequence_index = 1
    assert ensure_round_state(strategy, account, []) == ""
    assert strategy.last_add_price == 10.0  # 状态一致时不做任何改写

    # 序列>0 但一笔永久仓成交都没有：无法自愈，返回告警而非异常
    broken = LayeredTStrategy(StrategyConfig())
    account2 = BrokerAccount(initial_cash=1_000_000.0)
    account2.sequence_index = 1
    note = ensure_round_state(broken, account2, [])
    assert "无法自愈" in note


def test_ensure_round_state_ignores_flat_account():
    """空仓（sequence_index=0）时不校验：首轮开始前的空状态是合法的。"""
    strategy = LayeredTStrategy(StrategyConfig())
    account = BrokerAccount(initial_cash=1_000_000.0)
    account.sequence_index = 0
    assert ensure_round_state(strategy, account, []) == ""
