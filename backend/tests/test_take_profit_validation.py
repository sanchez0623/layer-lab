"""止盈阈值校验测试：price_take_profit / account_take_profit 必须落在 (0,1]。

回测 UI 已暴露这两个阈值（默认 0.15），StrategyConfig.__post_init__ 在源头拦截
非法值（0/负数会让止盈永不触发或立即触发；>1 无意义）。
"""

import pytest

from app.domain import StrategyConfig


def test_valid_default_take_profit_constructs():
    """默认 0.15 构造成功。"""
    cfg = StrategyConfig()
    assert cfg.price_take_profit == 0.15
    assert cfg.account_take_profit == 0.15


def test_valid_explicit_take_profit_constructs():
    """显式合法值（0.15 / 0.2）构造成功。"""
    cfg = StrategyConfig(price_take_profit=0.15, account_take_profit=0.2)
    assert cfg.price_take_profit == 0.15
    assert cfg.account_take_profit == 0.2


def test_price_take_profit_zero_raises():
    """price_take_profit=0 非法（止盈永不触发）。"""
    with pytest.raises(ValueError, match="price_take_profit"):
        StrategyConfig(price_take_profit=0)


def test_account_take_profit_over_one_raises():
    """account_take_profit=1.5 (>1) 非法。"""
    with pytest.raises(ValueError, match="account_take_profit"):
        StrategyConfig(account_take_profit=1.5)


def test_boundary_one_accepted():
    """边界值 1.0 合法（(0,1] 闭区间上界）。"""
    cfg = StrategyConfig(price_take_profit=1.0, account_take_profit=1.0)
    assert cfg.price_take_profit == 1.0
    assert cfg.account_take_profit == 1.0
