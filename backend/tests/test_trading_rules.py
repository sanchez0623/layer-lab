"""分板块 A 股交易规则测试：数量口径、单笔上限、涨跌幅、价格笼子、零股卖出与 API。"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest, StrategyConfig
from app.main import create_app
from app.trading_rules import (
    NOT_MODELED, SYNTHETIC_FALLBACK, all_board_rules, board_of, limit_pct_for,
    order_share_cap, price_cage_bounds, rules_for, split_quantity,
    validate_limit_price_cage, validate_order_amount, validate_quantity,
)


def make_bar(symbol: str, day: int, price: float = 10.0, volume: float = 100_000) -> Bar:
    return Bar(
        timestamp=datetime(2025, 1, day, 9, 30), symbol=symbol,
        open=price, high=price * 1.02, low=price * 0.98, close=price,
        volume=volume, amount=price * volume, prev_close=price,
    )


def config() -> StrategyConfig:
    return StrategyConfig(slippage_bps=0, volume_participation=1.0)


def request(symbol, side="BUY", quantity=100, order_type="MARKET", limit_price=None):
    return OrderRequest(
        symbol=symbol, side=side, quantity=quantity, purpose="PERMANENT",
        order_type=order_type, limit_price=limit_price, execution_mode="NEXT_BAR",
    )


# ---------------- board_of 板块判定 ----------------

def test_board_of_maps_star_gem_and_main_boards():
    assert board_of("sh.688200") == "科创板"
    assert board_of("689999") == "科创板"
    assert board_of("sz.300001") == "创业板"
    assert board_of("301999") == "创业板"
    assert board_of("sh.600519") == "沪市主板"
    assert board_of("sz.000001") == "深市主板"


def test_board_of_b_shares_and_unknown_codes():
    assert board_of("sh.900901") == "沪市B股"
    assert board_of("sz.200002") == "深市B股"
    assert board_of("TEST") == ""          # 合成标的无板块
    assert board_of("SYNTH001") == ""
    assert board_of("") == ""
    assert board_of("12345") == ""         # 非 6 位
    assert board_of("TEST") == ""          # 合成标的无板块
    assert board_of("SYNTH001") == ""
    assert board_of("") == ""
    assert board_of("12345") == ""         # 非 6 位


# ---------------- validate_quantity 数量口径 ----------------

def test_star_board_minimum_200_and_1_share_increment():
    assert validate_quantity("sh.688200", 200, "BUY") == ""
    assert validate_quantity("sh.688200", 201, "BUY") == ""    # 科创板可 1 股递增
    assert validate_quantity("sh.688200", 250, "BUY") == ""
    assert "最小申报 200" in validate_quantity("sh.688200", 150, "BUY")


def test_gem_board_100_lot_multiples():
    assert validate_quantity("sz.300001", 100, "BUY") == ""
    assert validate_quantity("sz.300001", 500, "BUY") == ""
    assert "100 股整数倍" in validate_quantity("sz.300001", 150, "BUY")
    assert "最小申报 100" in validate_quantity("sz.300001", 50, "BUY")


def test_main_board_100_lot_multiples():
    for symbol in ("sh.600519", "sz.000001"):
        assert validate_quantity(symbol, 100, "BUY") == ""
        assert validate_quantity(symbol, 1000, "BUY") == ""
        assert validate_quantity(symbol, 250, "BUY") != ""


def test_sell_odd_lot_liquidation_allowed_once():
    # 科创板持仓 150 股（低于最小申报 200）可一次性整笔卖出。
    assert validate_quantity("sh.688200", 150, "SELL", sellable_qty=150) == ""
    # 但卖的数量不等于余额时不允许。
    assert validate_quantity("sh.688200", 100, "SELL", sellable_qty=150) != ""
    # 创业板/主板持仓 <100 股可整笔卖出。
    assert validate_quantity("sz.300001", 60, "SELL", sellable_qty=60) == ""
    # 余额充足时卖出仍须整手。
    assert validate_quantity("sz.300001", 150, "SELL", sellable_qty=1000) != ""


def test_non_positive_quantity_rejected():
    assert validate_quantity("sh.688200", 0, "BUY") != ""
    assert validate_quantity("sz.300001", -100, "BUY") != ""


# ---------------- 单笔申报上限（按股数，非金额） ----------------

def test_star_order_amount_caps():
    # 科创板：限价单笔 ≤10 万股，市价单笔 ≤5 万股。
    assert validate_order_amount("sh.688200", 100_000, "LIMIT") == ""
    assert validate_order_amount("sh.688200", 100_001, "LIMIT") != ""
    assert validate_order_amount("sh.688200", 50_000, "MARKET") == ""
    assert validate_order_amount("sh.688200", 50_001, "MARKET") != ""


def test_gem_and_main_order_amount_caps():
    # 创业板：限价 ≤10 万股、市价 ≤5 万股；沪市主板：限价 ≤100 万股、市价 ≤10 万股。
    assert validate_order_amount("sz.300001", 100_000, "LIMIT") == ""
    assert validate_order_amount("sz.300001", 100_001, "LIMIT") != ""
    assert validate_order_amount("sz.300001", 50_000, "MARKET") == ""
    assert validate_order_amount("sz.300001", 50_001, "MARKET") != ""
    assert validate_order_amount("sh.600519", 1_000_000, "LIMIT") == ""
    assert validate_order_amount("sh.600519", 1_000_001, "LIMIT") != ""
    assert validate_order_amount("sh.600519", 100_000, "MARKET") == ""
    assert validate_order_amount("sh.600519", 100_001, "MARKET") != ""


def test_synthetic_symbol_has_no_amount_cap():
    assert validate_order_amount("TEST", 10_000_000, "LIMIT") == ""


# ---------------- 涨跌幅 ----------------

def test_limit_pct_by_board():
    assert limit_pct_for("sh.688200") == 0.20
    assert limit_pct_for("sz.300001") == 0.20
    assert limit_pct_for("sh.600519") == 0.10
    assert limit_pct_for("sz.000001") == 0.10
    assert limit_pct_for("TEST") == SYNTHETIC_FALLBACK.limit_pct


def test_broker_tradable_uses_board_limit_pct():
    account = BrokerAccount(1_000_000, config())
    # 沪市主板 ±10%：prev_close 10，价格 11.5 超限不可成交。
    assert account._tradable("BUY", 11.5, 10.0, "sh.600519") is False
    assert account._tradable("BUY", 10.9, 10.0, "sh.600519") is True
    # 科创板 ±20%：同价位 11.5 仍可成交。
    assert account._tradable("BUY", 11.5, 10.0, "sh.688200") is True
    assert account._tradable("BUY", 12.1, 10.0, "sh.688200") is False


# ---------------- 价格笼子 ----------------

def test_price_cage_bounds_star_is_pure_2pct():
    lower, upper = price_cage_bounds("sh.688200", 100.0)
    assert lower == pytest.approx(98.0)
    assert upper == pytest.approx(102.0)


def test_price_cage_bounds_gem_and_main_use_abs_floor():
    # 低价股：2% 只有 0.08 元 < 0.1 元，取绝对值 0.1 元。
    lower, upper = price_cage_bounds("sz.300001", 4.0)
    assert lower == pytest.approx(3.9)
    assert upper == pytest.approx(4.1)
    # 高价股：2% = 2 元 > 0.1 元，用百分比。
    lower, upper = price_cage_bounds("sh.600519", 100.0)
    assert lower == pytest.approx(98.0)
    assert upper == pytest.approx(102.0)


def test_price_cage_validation_message():
    assert validate_limit_price_cage("sh.688200", 101.0, 100.0) == ""
    assert "价格笼子" in validate_limit_price_cage("sh.688200", 103.0, 100.0)


# ---------------- broker.submit 分板块拒单 ----------------

def test_submit_rejects_star_order_below_minimum_as_rejected_order():
    account = BrokerAccount(1_000_000, config())
    order = account.submit(request("sh.688200", quantity=100))
    assert order.status == "REJECTED"
    assert "最小申报 200" in order.message
    # 被拒单仍落账本，便于审计，但不进入撮合。
    assert len(account.orders) == 1


def test_submit_rejects_gem_non_lot_order():
    account = BrokerAccount(1_000_000, config())
    order = account.submit(request("sz.300001", quantity=150))
    assert order.status == "REJECTED"
    assert "100 股整数倍" in order.message


def test_submit_accepts_star_201_shares():
    account = BrokerAccount(1_000_000, config())
    order = account.submit(request("sh.688200", quantity=201))
    assert order.status == "PENDING"


def test_submit_rejects_star_order_over_share_cap():
    """科创板限价单笔申报上限 10 万股（按股数，非金额），超额申报记 REJECTED。"""
    account = BrokerAccount(100_000_000, config())
    order = account.submit(request("sh.688200", quantity=100_001, order_type="LIMIT", limit_price=10.0))
    assert order.status == "REJECTED"
    assert "上限" in order.message
    # 市价单笔上限 5 万股。
    market = account.submit(request("sh.688200", quantity=50_001, order_type="MARKET", side="BUY"))
    assert market.status == "REJECTED"
    assert "上限" in market.message


def test_synthetic_symbol_still_allows_100_share_buy():
    account = BrokerAccount(1_000_000, config())
    order = account.submit(request("TEST", quantity=100))
    assert order.status == "PENDING"   # 回退旧口径，旧回测不被打断


def test_star_board_can_fill_1_share_increment_partial():
    """科创板可 1 股递增：资金不足时按 1 股降档，而不是 100 股。"""
    account = BrokerAccount(1_000_000, config())
    account.submit(request("sh.688200", quantity=1000, order_type="LIMIT", limit_price=10.0))
    bar = make_bar("sh.688200", 1, price=10.0, volume=100_000)
    account.config.volume_participation = 0.001   # 容量 100 股
    fills = account.process_bar(bar)
    assert len(fills) == 1
    assert fills[0].quantity == 100


def test_odd_lot_sell_can_fill_full_position():
    """科创板持仓 150 股（零股）整笔卖出应能全部成交，不被 step 截断。"""
    account = BrokerAccount(1_000_000, config())
    from app.domain import Position
    account.positions["sh.688200"] = Position(symbol="sh.688200", permanent_qty=150, last_price=10.0)
    order = account.submit(request("sh.688200", "SELL", quantity=150))
    assert order.status == "PENDING"
    fills = account.process_bar(make_bar("sh.688200", 1, price=10.0, volume=100_000))
    assert len(fills) == 1
    assert fills[0].quantity == 150
    assert account.position("sh.688200").permanent_qty == 0


# ---------------- 规则表 / API ----------------

def test_all_board_rules_cover_six_boards():
    boards = {item.board for item in all_board_rules()}
    assert boards == {
        "科创板", "创业板", "沪市主板", "深市主板", "沪市B股", "深市B股", "ETF",
    }
    star = rules_for("sh.688200")
    assert star.min_qty == 200 and star.step == 1
    assert star.cage_label == "纯 2%"
    assert rules_for("sz.300001").cage_label == "2% 与 0.1 元孰高"
    assert "集合竞价" in NOT_MODELED[0]


def test_etf_board_rules_and_detection():
    assert board_of("sz.159915") == "ETF"
    assert board_of("sh.510300") == "ETF"
    assert board_of("sh.588000") == "ETF"
    etf = rules_for("sz.159915")
    assert etf.min_qty == 100 and etf.step == 100
    assert etf.limit_pct == 0.20          # 创业板 ETF 涨跌幅 ±20%
    assert etf.cage_label == "2% 与 0.1 元孰高"
    # 普通个股不会被误判成 ETF
    assert board_of("sz.300001") == "创业板"
    assert board_of("sh.600519") == "沪市主板"


def test_etf_order_quantity_and_amount_caps():
    assert validate_quantity("sz.159915", 100, "BUY") == ""
    assert "整数倍" in validate_quantity("sz.159915", 150, "BUY")
    assert "最小申报" in validate_quantity("sz.159915", 50, "BUY")
    assert validate_order_amount("sz.159915", 50_000, "MARKET") == ""
    assert "上限" in validate_order_amount("sz.159915", 60_000, "MARKET")


def test_trading_rules_api_for_star_symbol(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())
    response = client.get("/api/trading-rules/688200")
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "sh.688200"
    assert data["board"] == "科创板"
    assert data["is_modeled_board"] is True
    assert data["rules"]["min_qty"] == 200
    assert data["rules"]["step"] == 1
    assert data["not_modeled"]


def test_trading_rules_api_falls_back_for_synthetic(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())
    data = client.get("/api/trading-rules/TEST").json()
    assert data["is_modeled_board"] is False
    assert data["rules"]["board"].startswith("合成标的")
    assert data["rules"]["min_qty"] == 100


def test_manual_order_rejected_returns_400_without_persisting(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())
    account = client.post("/api/paper/accounts", json={"name": "拒单", "initial_cash": 1_000_000}).json()
    response = client.post(f"/api/paper/accounts/{account['id']}/orders", json={
        "symbol": "688200", "side": "BUY", "order_type": "MARKET", "quantity": 100,
    })
    assert response.status_code == 400
    assert "最小申报 200" in response.json()["detail"]
    # 不落库：账户里没有这笔被拒单。
    state = client.get(f"/api/paper/accounts/{account['id']}").json()
    assert state["orders"] == []


def test_manual_star_order_201_accepted_by_api(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())
    account = client.post("/api/paper/accounts", json={"name": "科创板合法单", "initial_cash": 1_000_000}).json()
    response = client.post(f"/api/paper/accounts/{account['id']}/orders", json={
        "symbol": "688200", "side": "BUY", "order_type": "MARKET", "quantity": 201,
    })
    assert response.status_code == 200
    assert response.json()["order"]["status"] == "PENDING"


# ---------------- 单笔申报上限拆分 ----------------

def test_order_share_cap_per_board_and_order_type():
    assert order_share_cap("sz.300045", "MARKET") == 50_000
    assert order_share_cap("sz.300045", "LIMIT") == 100_000
    assert order_share_cap("sh.688200", "MARKET") == 50_000
    assert order_share_cap("sh.600519", "MARKET") == 100_000
    assert order_share_cap("TEST", "MARKET") == 0


def test_split_quantity_gem_large_liquidation():
    """回归：创业板 176,200 股整笔市价卖出超 5 万股上限，必须拆成 4 笔。"""
    chunks = split_quantity("sz.300045", 176_200, "MARKET")
    assert chunks == [50_000, 50_000, 50_000, 26_200]
    assert all(0 < qty <= 50_000 and qty % 100 == 0 for qty in chunks)
    assert sum(chunks) == 176_200


def test_split_quantity_uses_limit_cap_for_limit_orders():
    assert split_quantity("sz.300045", 176_200, "LIMIT") == [100_000, 76_200]


def test_split_quantity_main_board_and_synthetic():
    assert split_quantity("sh.600519", 120_000, "MARKET") == [100_000, 20_000]
    # 合成标的未建模上限：原样一笔，不改变旧回测/旧测试行为。
    assert split_quantity("TEST", 500_000, "MARKET") == [500_000]


def test_split_quantity_star_remainder_borrows_to_min_qty():
    """科创板 min_qty=200：余数不足 200 时从上一笔借，避免产生非法尾单。"""
    chunks = split_quantity("sh.688200", 50_050, "MARKET")
    assert chunks == [49_850, 200]
    assert all(qty >= 200 for qty in chunks)


def test_split_quantity_odd_lot_liquidation_single_chunk():
    """整笔持仓低于最小申报的零股清仓：单笔返回，走 broker 零股通道。"""
    assert split_quantity("sh.688200", 150, "MARKET") == [150]
    assert split_quantity("sz.300045", 50, "MARKET") == [50]


def test_submit_split_emits_cap_conformant_child_orders():
    account = BrokerAccount(100_000_000, config())
    orders = account.submit_split(request("sz.300045", quantity=176_200))
    assert len(orders) == 4
    assert sum(order.quantity for order in orders) == 176_200
    assert all(order.status == "PENDING" and order.quantity <= 50_000 for order in orders)


def test_submit_still_rejects_oversize_manual_order():
    """手工委托保持「超额即拒绝」的真实反馈，不走自动拆分。"""
    account = BrokerAccount(100_000_000, config())
    order = account.submit(request("sz.300045", quantity=176_200))
    assert order.status == "REJECTED"
    assert "上限" in order.message


def test_liquidation_of_large_position_splits_and_never_rejected():
    """回归 REPORT/4ECFE242：整笔 176,200 股清仓被拆成 4 笔合规卖单，不再刷拒绝。

    历史 bug：策略把整个持仓当一笔市价卖单提交，超创业板 5 万股上限被拒，
    且 liquidating 状态每根 K 线重发，单份报告刷出 2.4 万条相同拒绝。
    """
    from datetime import datetime

    from app.backtest import submit_split_requests
    from app.domain import Position
    from app.strategy import LayeredTStrategy

    account = BrokerAccount(100_000_000, config())
    account.positions["sz.300045"] = Position(
        symbol="sz.300045", permanent_qty=176_200, permanent_cost=1_762_000.0, last_price=10.0,
    )
    strategy = LayeredTStrategy(config())
    strategy.close_reason = "Armed量价背离三选二_3根5分钟K线"
    requests = strategy._liquidation_requests(account.position("sz.300045"), "NEXT_BAR")

    events: list[dict] = []
    warnings: list[str] = []
    submit_split_requests(account, requests, datetime(2024, 10, 8, 13, 25), events, warnings)

    sells = [order for order in account.orders if order.side == "SELL"]
    assert len(sells) == 4
    assert sum(order.quantity for order in sells) == 176_200
    assert all(order.status == "PENDING" for order in sells)
    assert warnings == []
    assert len(events) == 4
