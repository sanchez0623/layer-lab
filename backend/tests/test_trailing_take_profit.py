"""方案A 两段式止盈（达标转 trailing）回归测试。

覆盖：
1. 开关关 = 完全旧行为（到 price_take_profit 即一次性清仓）。
2. 开关开 = 首次到激活点不立即清仓，转 trailing（trailing_armed=True）。
3. 激活后从浮盈峰值回撤 ≥ trailing_drawdown_pct 才清仓（trigger=移动止盈）。
4. 回撤未到位 → 不清仓，且不进 Armed 背离分支（15% 以上只听 trailing 的）。
5. reset_round 清零 trailing 运行态。
6. 配置校验：trailing_drawdown_pct 越界抛 ValueError；from_dict 缺键走默认。
"""
from datetime import datetime, timedelta

import pytest

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig
from app.strategy import LayeredTStrategy


def daily(day: int, close: float, volume: float = 1_000) -> Bar:
    return Bar(
        timestamp=datetime(2025, 1, 1) + timedelta(days=day), symbol="TEST",
        open=close, high=close * 1.02, low=close * 0.95, close=close,
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


def strategy_with_position(account: BrokerAccount, config: StrategyConfig | None = None) -> LayeredTStrategy:
    strategy = LayeredTStrategy(config or account.config)
    strategy.last_add_price = 10.0
    strategy.last_add_session = 0
    return strategy


# funded_account 建仓后的真实持仓成本（含滑点）从账户读，不硬猜：
# 用 target_return(成本价 ×(1+r)) 反推目标价，期望浮盈直接用 r 断言，避免依赖成本细节。
def _cost(account: BrokerAccount) -> float:
    pos = account.position("TEST")
    return pos.permanent_cost / pos.permanent_qty


def _price_at(account: BrokerAccount, target_return: float) -> float:
    return _cost(account) * (1 + target_return)


def test_trailing_disabled_keeps_legacy_behavior():
    """开关关（默认）= 旧行为：到 15% 立即一次性清仓，reason=价格轨收益达到15%。"""
    account = funded_account()
    strategy = strategy_with_position(account)
    requests = strategy.on_daily([daily(1, _price_at(account, 0.20))], [], account, 1, False)
    assert any(item.side == "SELL" and item.purpose == "PERMANENT" for item in requests)
    assert strategy.liquidating is True
    assert strategy.trailing_armed is False
    assert strategy.close_reason == "价格轨收益达到15%"


def test_trailing_enabled_arms_instead_of_liquidating():
    """开关开：首次到激活点不立即清仓，转 trailing（trailing_armed=True、liquidating 闩锁）。"""
    account = funded_account()
    config = StrategyConfig(
        slippage_bps=0, volume_participation=1.0,
        trailing_take_profit_enabled=True, trailing_drawdown_pct=0.06,
    )
    strategy = strategy_with_position(account, config)
    # 浮盈 20% ≥ 15% 激活点：本根不清仓、不设 close_signal。
    assert strategy.on_daily([daily(1, _price_at(account, 0.20))], [], account, 1, False) == []
    assert strategy.trailing_armed is True
    assert strategy.liquidating is True
    assert strategy.trailing_peak_return == pytest.approx(0.20, abs=1e-6)
    assert strategy._close_signal is None
    # 激活后浮盈维持 20%（回撤 0 < 6%）→ 仍不清仓。
    assert strategy.on_daily([daily(2, _price_at(account, 0.20))], [], account, 2, False) == []
    assert strategy._close_signal is None


def test_trailing_drawdown_triggers_move_stop():
    """激活后抬峰值到 +30%，回落到 +20% → 回撤 10% ≥ 6% 触发清仓（trigger=移动止盈）。"""
    account = funded_account()
    config = StrategyConfig(
        slippage_bps=0, volume_participation=1.0,
        trailing_take_profit_enabled=True, trailing_drawdown_pct=0.06,
    )
    strategy = strategy_with_position(account, config)
    # 激活：+20%
    assert strategy.on_daily([daily(1, _price_at(account, 0.20))], [], account, 1, False) == []
    # 抬峰值：+30%
    assert strategy.on_daily([daily(2, _price_at(account, 0.30))], [], account, 2, False) == []
    assert strategy.trailing_peak_return == pytest.approx(0.30, abs=1e-6)
    # 回撤：+20% → 30% - 20% = 10% ≥ 6% → 清仓。
    requests = strategy.on_daily([daily(3, _price_at(account, 0.20))], [], account, 3, False)
    assert any(item.side == "SELL" and item.purpose == "PERMANENT" for item in requests)
    assert strategy._close_signal is not None
    assert strategy._close_signal["trigger"] == "移动止盈"
    assert strategy._close_signal["trailing_peak_return"] == pytest.approx(0.30, abs=1e-6)
    assert strategy._close_signal["trailing_drawdown_pct"] == pytest.approx(0.06)
    assert "移动止盈回撤6%" in strategy.close_reason


def test_trailing_not_drawn_down_stays_in_position():
    """回撤未到位（+23%→+21%，回撤 2% < 6%）→ 不清仓；且不进 Armed 背离分支。"""
    account = funded_account()
    config = StrategyConfig(
        slippage_bps=0, volume_participation=1.0,
        trailing_take_profit_enabled=True, trailing_drawdown_pct=0.06,
        price_armed=0.10, price_disarm=0.095,
    )
    strategy = strategy_with_position(account, config)
    # 激活：+20%
    assert strategy.on_daily([daily(1, _price_at(account, 0.20))], [], account, 1, False) == []
    # 抬峰值：+23%
    assert strategy.on_daily([daily(2, _price_at(account, 0.23))], [], account, 2, False) == []
    # 回落：+21%（回撤 2% < 6%）→ 不清仓。
    assert strategy.on_daily([daily(3, _price_at(account, 0.21))], [], account, 3, False) == []
    assert strategy._close_signal is None
    assert strategy.liquidating is True  # 仍处 trailing 闩锁，等待回撤到位


def test_reset_round_clears_trailing_state():
    """轮次闭合 reset_round 清零 trailing 运行态，新一轮从 0 重新判定。"""
    account = funded_account()
    config = StrategyConfig(
        slippage_bps=0, volume_participation=1.0,
        trailing_take_profit_enabled=True, trailing_drawdown_pct=0.06,
    )
    strategy = strategy_with_position(account, config)
    strategy.on_daily([daily(1, 11.6)], [], account, 1, False)
    assert strategy.trailing_armed is True
    strategy.reset_round(account)
    assert strategy.trailing_armed is False
    assert strategy.trailing_peak_return == 0.0


def test_trailing_drawdown_pct_validation():
    """trailing_drawdown_pct 须在 (0,1]；from_dict 缺键走默认（向后兼容）。"""
    with pytest.raises(ValueError):
        StrategyConfig(trailing_drawdown_pct=0.0)
    with pytest.raises(ValueError):
        StrategyConfig(trailing_drawdown_pct=1.5)
    with pytest.raises(ValueError):
        StrategyConfig(trailing_drawdown_pct=-0.1)
    ok = StrategyConfig(trailing_drawdown_pct=0.08)
    assert ok.trailing_drawdown_pct == pytest.approx(0.08)
    assert ok.trailing_take_profit_enabled is False
    # 老账户/回测报告缺这两个键 → 走默认（关闭 + 6%），行为不变。
    legacy = StrategyConfig.from_dict({})
    assert legacy.trailing_take_profit_enabled is False
    assert legacy.trailing_drawdown_pct == pytest.approx(0.06)
    assert StrategyConfig().to_dict()["trailing_take_profit_enabled"] is False
    assert StrategyConfig().to_dict()["trailing_drawdown_pct"] == pytest.approx(0.06)


def test_trailing_state_round_trips_through_state_dict():
    """trailing_armed / trailing_peak_return 进 STRATEGY_STATE_FIELDS，可 state_dict↔load_state 往返。"""
    account = funded_account()
    config = StrategyConfig(
        slippage_bps=0, volume_participation=1.0,
        trailing_take_profit_enabled=True, trailing_drawdown_pct=0.06,
    )
    strategy = strategy_with_position(account, config)
    strategy.on_daily([daily(1, _price_at(account, 0.20))], [], account, 1, False)
    dump = strategy.state_dict()
    assert dump["trailing_armed"] is True
    assert dump["trailing_peak_return"] == pytest.approx(0.20, abs=1e-6)
    # 新策略实例 load 回来，运行态恢复。
    restored = LayeredTStrategy(config)
    restored.load_state(dump)
    assert restored.trailing_armed is True
    assert restored.trailing_peak_return == pytest.approx(0.20, abs=1e-6)
