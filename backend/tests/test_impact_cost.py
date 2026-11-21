"""平方根冲击成本模型（slippage_model="impact"）单测。

覆盖：
- 默认 fixed 模式：broker 成交价与旧公式完全一致（向后兼容哨兵）。
- impact 模式：单量越大滑点率越大（单调性）、量趋 0 时 rate→0。
- impact 校准：满额单 rate ≈ sqrt(volume_participation)，验证公式落地正确。
- impact_coefficient=0 → impact 模式 0 滑点。
- bar.volume<=0 → impact 模式不崩、无成交（参与率 0 兜底）。
- 配置校验：slippage_model 非法值 / impact_coefficient<0 → ValueError。
- _t_roundtrip_cost：fixed 与旧值一致；impact = ... + 2*k*sqrt(volume_participation)。
- 端到端：impact 模式下大单比小单成交价更差（同一 bar）。
"""
import math
from datetime import datetime

import pytest

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig
from app.strategy import LayeredTStrategy


def make_bar(day: int = 1, price: float = 10.0, volume: float = 1_000_000) -> Bar:
    return Bar(
        timestamp=datetime(2025, 1, day, 9, 30), symbol="TEST",
        open=price, high=price * 1.02, low=price * 0.98, close=price,
        volume=volume, amount=price * volume, prev_close=price,
    )


def request(side="BUY", quantity=100, purpose="PERMANENT", order_type="MARKET", limit_price=None):
    return OrderRequest(
        symbol="TEST", side=side, quantity=quantity, purpose=purpose,
        order_type=order_type, limit_price=limit_price, execution_mode="NEXT_BAR",
    )


def fill_buy(cfg: StrategyConfig, quantity: int, volume: float = 1_000_000, price: float = 10.0):
    """提交一笔市价买单并在同一 bar 成交，返回 (fill, base_price)。"""
    account = BrokerAccount(10_000_000, cfg)
    account.new_day("2025-01-01")
    account.submit(request("BUY", quantity))
    bar = make_bar(1, price, volume)
    fills = account.process_bar(bar)
    return fills[0], bar.close


# ---------------------------------------------------------------------------
# fixed 模式：完全保留旧行为
# ---------------------------------------------------------------------------

def test_fixed_model_matches_old_formula():
    """默认 fixed 模式：slippage_bps=1 → 买单成交价 = base*(1+1/10000)。"""
    cfg = StrategyConfig(slippage_bps=1, volume_participation=1.0)
    fill, base = fill_buy(cfg, quantity=100)
    expected = base * (1 + 1 / 10000)
    assert fill.price == pytest.approx(expected)


def test_fixed_model_zero_slippage_unchanged():
    """fixed 模式 slippage_bps=0 → 成交价等于基准价（大量既有单测依赖）。"""
    cfg = StrategyConfig(slippage_bps=0, volume_participation=1.0)
    fill, base = fill_buy(cfg, quantity=100)
    assert fill.price == pytest.approx(base)
    assert fill.slippage == 0.0


# ---------------------------------------------------------------------------
# impact 模式：单调性 / 趋零 / 校准 / k=0
# ---------------------------------------------------------------------------

def _implied_rate(fill, base):
    return abs(fill.price - base) / base


def test_impact_rate_monotonic_in_quantity():
    """同一 bar 下，单量越大 slippage_rate 越大（单调性）。

    取量使 sqrt(participation) < 0.2，成交价不触 20% 涨跌停（否则委托被涨跌停校验拦截）。
    """
    cfg = StrategyConfig(slippage_model="impact", impact_coefficient=1.0,
                         slippage_bps=0, volume_participation=1.0)
    small, base = fill_buy(cfg, quantity=1_000)      # rate = sqrt(0.001) ≈ 0.0316
    mid, _ = fill_buy(cfg, quantity=10_000)          # rate = sqrt(0.01)  = 0.1
    big, _ = fill_buy(cfg, quantity=25_000)          # rate = sqrt(0.025) ≈ 0.158
    r1 = _implied_rate(small, base)
    r2 = _implied_rate(mid, base)
    r3 = _implied_rate(big, base)
    assert 0 < r1 < r2 < r3


def test_impact_rate_tends_to_zero_for_tiny_order():
    """impact 模式：quantity 接近 0 时 rate→0（成交价≈基准价）。"""
    cfg = StrategyConfig(slippage_model="impact", impact_coefficient=1.0,
                         slippage_bps=0, volume_participation=1.0)
    fill, base = fill_buy(cfg, quantity=100, volume=1_000_000)
    # participation = 100/1e6 = 1e-4 → rate = 0.01，远小于 fixed 默认的 0.0001? 这里只需明显趋零。
    assert _implied_rate(fill, base) == pytest.approx(math.sqrt(100 / 1_000_000))
    assert fill.price == pytest.approx(base, abs=base * 0.01)


def test_impact_rate_calibration_full_capacity():
    """校准：k=1.0，单量填满参与度额度（= bar.volume*volume_participation）
    → rate = sqrt(volume_participation)。取 vp=0.01 保证 execution 不触涨跌停。"""
    vp = 0.01
    volume = 1_000_000
    cfg = StrategyConfig(slippage_model="impact", impact_coefficient=1.0,
                         slippage_bps=0, volume_participation=vp)
    capacity = int(volume * vp)
    fill, base = fill_buy(cfg, quantity=capacity, volume=volume)
    assert fill.quantity == capacity  # 满额成交
    expected_rate = math.sqrt(vp)
    assert _implied_rate(fill, base) == pytest.approx(expected_rate)
    assert fill.price == pytest.approx(base * (1 + expected_rate))


def test_impact_zero_coefficient_no_slippage():
    """impact_coefficient=0 → impact 模式 0 滑点（成交价=基准价）。"""
    cfg = StrategyConfig(slippage_model="impact", impact_coefficient=0.0,
                         slippage_bps=5, volume_participation=1.0)
    fill, base = fill_buy(cfg, quantity=500_000)
    assert fill.price == pytest.approx(base)
    assert fill.slippage == 0.0


def test_impact_zero_bar_volume_does_not_crash():
    """bar.volume<=0 → impact 模式不崩；参与率 0 兜底，无成交（容量为 0）。"""
    cfg = StrategyConfig(slippage_model="impact", impact_coefficient=1.0,
                         slippage_bps=0, volume_participation=1.0)
    account = BrokerAccount(10_000_000, cfg)
    account.new_day("2025-01-01")
    account.submit(request("BUY", 100))
    fills = account.process_bar(make_bar(1, 10.0, volume=0))
    assert fills == []  # 不抛异常，容量为 0 时不成交


def test_impact_sell_side_symmetric():
    """impact 模式买卖对称：SELL 成交价 = base*(1-rate)，低于基准价。"""
    cfg = StrategyConfig(slippage_model="impact", impact_coefficient=1.0,
                         slippage_bps=0, volume_participation=1.0)
    account = BrokerAccount(10_000_000, cfg)
    account.new_day("2025-01-01")
    account.submit(request("BUY", 20_000))
    account.process_bar(make_bar(1, 10.0, 1_000_000))
    account.new_day("2025-01-02")
    account.submit(request("SELL", 20_000))
    sell_fills = account.process_bar(make_bar(2, 10.0, 1_000_000))
    assert sell_fills
    base = 10.0
    assert sell_fills[0].price == pytest.approx(base * (1 - math.sqrt(20_000 / 1_000_000)), rel=1e-3)


# ---------------------------------------------------------------------------
# 端到端：impact 模式下大单成交价更差（同一 bar、两个账户）
# ---------------------------------------------------------------------------

def test_impact_bigger_order_worse_price_end_to_end():
    cfg = StrategyConfig(slippage_model="impact", impact_coefficient=1.0,
                         slippage_bps=0, volume_participation=1.0)
    small_fill, base = fill_buy(cfg, quantity=1_000)
    big_fill, _ = fill_buy(cfg, quantity=25_000)
    # 买单滑点方向为加价，大单加价更多 → 成交价更差。
    assert (big_fill.price - base) > (small_fill.price - base)
    assert big_fill.slippage > small_fill.slippage


# ---------------------------------------------------------------------------
# 配置校验
# ---------------------------------------------------------------------------

def test_invalid_slippage_model_raises():
    with pytest.raises(ValueError):
        StrategyConfig(slippage_model="sqrt")
    with pytest.raises(ValueError):
        StrategyConfig(slippage_model="")


def test_negative_impact_coefficient_raises():
    with pytest.raises(ValueError):
        StrategyConfig(impact_coefficient=-0.1)
    # 0 合法
    StrategyConfig(impact_coefficient=0.0)


# ---------------------------------------------------------------------------
# _t_roundtrip_cost
# ---------------------------------------------------------------------------

def _fee_part(cfg: StrategyConfig) -> float:
    return (
        2 * (cfg.commission_rate + cfg.handling_fee_rate
             + cfg.regulatory_fee_rate + cfg.transfer_fee_rate)
        + cfg.stamp_tax_rate
    )


def test_roundtrip_cost_fixed_matches_old():
    cfg = StrategyConfig(slippage_bps=1.0)
    strategy = LayeredTStrategy(cfg)
    expected = _fee_part(cfg) + 2 * cfg.slippage_bps / 10000
    assert strategy._t_roundtrip_cost() == pytest.approx(expected)


def test_roundtrip_cost_fixed_default_unchanged():
    """fixed 模式（默认）返回值与旧版实现完全一致（不破坏做T准入/止盈测试）。"""
    cfg = StrategyConfig()
    strategy = LayeredTStrategy(cfg)
    assert strategy._t_roundtrip_cost() == pytest.approx(
        _fee_part(cfg) + 2 * cfg.slippage_bps / 10000
    )


def test_roundtrip_cost_impact_uses_volume_participation():
    k = 1.5
    vp = 0.20
    cfg = StrategyConfig(slippage_model="impact", impact_coefficient=k,
                         volume_participation=vp)
    strategy = LayeredTStrategy(cfg)
    expected = _fee_part(cfg) + 2 * k * math.sqrt(vp)
    assert strategy._t_roundtrip_cost() == pytest.approx(expected)
    # impact 单边滑点 = k*sqrt(vp) = 1.5*0.4472 ≈ 0.67，显著大于 fixed 默认 1bps。
    assert strategy._t_roundtrip_cost() > _fee_part(cfg) + 2 * 1.0 / 10000


# ------------------------------------------------------- HTTP 端点端到端 ----
# 回归哨兵：前端「模拟盘保存滑点模型」走 POST /paper/accounts/{id}/config，
# 该端点的 AccountConfigRequest 必须显式收 slippage_model / impact_coefficient，
# 否则 extra="forbid" 会拒绝（422）——真实事故：小系数 0.01 保存直接报错。
# 下面用 TestClient 走完整 HTTP 链路，确保端点层不漏这两个字段。

def test_paper_config_endpoint_accepts_impact_model(tmp_path, monkeypatch):
    """模拟盘 /config 端点能接收 slippage_model=impact + 小系数 0.01 并落库。"""
    from fastapi.testclient import TestClient
    from app.main import create_app

    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    client = TestClient(create_app())
    account = client.post("/api/paper/accounts", json={
        "name": "冲击模型", "initial_cash": 4_000_000,
    }).json()

    resp = client.post(f"/api/paper/accounts/{account['id']}/config", json={
        "slippage_model": "impact", "impact_coefficient": 0.01,
    })
    assert resp.status_code == 200, resp.text
    config = resp.json()["config"]
    assert config["slippage_model"] == "impact"
    assert config["impact_coefficient"] == pytest.approx(0.01)

    # 持久化往返后仍是新值
    reloaded = client.get(f"/api/paper/accounts/{account['id']}").json()["config"]
    assert reloaded["slippage_model"] == "impact"
    assert reloaded["impact_coefficient"] == pytest.approx(0.01)


def test_paper_config_endpoint_rejects_bad_impact_model(tmp_path, monkeypatch):
    """非法 slippage_model / 负系数在端点层被拦下（4xx），不让脏值落库。

    两条拦截路径不同但都拒绝：
    - 非法模型名：Pydantic 放行（只是 str），到 StrategyConfig.from_dict 抛 ValueError → 400。
    - 负系数：AccountConfigRequest 的 Field(ge=0) 在 Pydantic 层直接拒 → 422。
    """
    from fastapi.testclient import TestClient
    from app.main import create_app

    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    client = TestClient(create_app())
    account = client.post("/api/paper/accounts", json={
        "name": "冲击模型越界", "initial_cash": 4_000_000,
    }).json()
    url = f"/api/paper/accounts/{account['id']}/config"

    # 非法模型名 → StrategyConfig 校验抛 ValueError → 400
    resp = client.post(url, json={"slippage_model": "quadratic", "impact_coefficient": 0.01})
    assert resp.status_code == 400, resp.text
    # 负系数 → Pydantic Field(ge=0) 拒绝 → 422（仍在端点层拦下，不落库）
    resp = client.post(url, json={"slippage_model": "impact", "impact_coefficient": -0.5})
    assert resp.status_code == 422, resp.text
