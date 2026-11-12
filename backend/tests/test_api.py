from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.broker import BrokerAccount
from app.domain import Bar, OrderRequest
from app.main import create_app, snapshot_execution_bar, trading_minutes_elapsed


def test_full_api_workflow(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["broker_connected"] is False
    defaults = client.get("/api/strategy/defaults").json()
    assert len(defaults["installment_amounts"]) == 16
    assert sum(defaults["installment_amounts"]) == 4_000_000

    demo_response = client.post("/api/datasets/demo")
    assert demo_response.status_code == 200
    demo = demo_response.json()
    assert demo["daily"]["rows"] == 320
    assert demo["intraday"]["rows"] == 48 * 240
    assert demo["daily"]["is_synthetic"] is True
    assert demo["intraday"]["timeframe"] == "1m"
    assert "file_path" not in demo["daily"]
    assert len(client.get("/api/datasets").json()) == 3

    backtest_response = client.post("/api/backtests", json={
        "name": "API日线降级测试", "dataset_id": demo["daily"]["id"],
        "index_dataset_id": demo["index"]["id"], "initial_cash": 4_000_000,
        "config": {"enable_t": False},
    })
    assert backtest_response.status_code == 200, backtest_response.text
    report = backtest_response.json()
    assert report["status"] == "COMPLETED"
    assert report["metrics"]["initial_cash"] == 4_000_000
    assert len(report["equity_curve"]) == 320
    assert report["data_info"]["actual_granularity"] == "daily_only"
    assert report["data_info"]["is_synthetic"] is True
    assert any("降级" in item for item in report["warnings"])
    assert client.get(f"/api/backtests/{report['id']}").status_code == 200
    assert len(client.get("/api/backtests").json()) == 1
    exported = client.get(f"/api/backtests/{report['id']}/export?kind=orders")
    assert exported.status_code == 200
    assert "text/csv" in exported.headers["content-type"]

    created = client.post("/api/paper/accounts", json={
        "name": "API模拟", "initial_cash": 100_000,
        "dataset_id": demo["intraday"]["id"],
    })
    assert created.status_code == 200
    account = created.json()
    order_response = client.post(f"/api/paper/accounts/{account['id']}/orders", json={
        "symbol": "SYNTH001", "side": "BUY", "order_type": "MARKET", "quantity": 100,
    })
    assert order_response.status_code == 200
    order_id = order_response.json()["order"]["id"]
    advanced = client.post(f"/api/paper/accounts/{account['id']}/advance", json={"bars": 1})
    assert advanced.status_code == 200, advanced.text
    state = advanced.json()
    assert state["cursor"] == 1
    assert state["positions"][0]["total_qty"] == 100
    assert state["positions"][0]["sellable_qty"] == 0
    assert state["trades"][0]["order_id"] == order_id
    current = client.get("/api/paper/accounts/current").json()
    assert current["id"] == account["id"]
    reset = client.post(f"/api/paper/accounts/{account['id']}/reset")
    assert reset.status_code == 200
    assert reset.json()["cash"] == 100_000
    assert reset.json()["cursor"] == 0
    assert reset.json()["positions"] == []


def test_multiple_paper_accounts_can_reuse_local_order_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())
    ids = []
    for index in range(2):
        account = client.post("/api/paper/accounts", json={
            "name": f"多账户{index}", "initial_cash": 100_000,
        }).json()
        ids.append(account["id"])
        response = client.post(f"/api/paper/accounts/{account['id']}/orders", json={
            "symbol": "688200", "side": "BUY", "order_type": "MARKET", "quantity": 100,
        })
        assert response.status_code == 200, response.text
        assert response.json()["order"]["symbol"] == "sh.688200"
    assert client.get(f"/api/paper/accounts/{ids[1]}").status_code == 200


def test_csv_upload_api(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())
    content = "日期,开盘价,最高价,最低价,收盘价,成交量\n2025-01-02,10,11,9,10.5,10000\n"
    response = client.post(
        "/api/datasets/upload?symbol=000001&name=测试&timeframe=daily",
        files={"file": ("测试.csv", content.encode("utf-8"), "text/csv")},
    )
    assert response.status_code == 200, response.text
    dataset = response.json()
    assert dataset["symbol"] == "000001"
    assert dataset["source"] == "upload"
    assert dataset["is_synthetic"] is False
    assert dataset["rows"] == 1


def test_paper_order_api_enum_contract_is_uppercase(tmp_path, monkeypatch):
    """后端只接受大写枚举；前端 api.placeOrder 负责把小写 UI 状态归一化后再发送。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())
    account = client.post("/api/paper/accounts", json={
        "name": "枚举契约", "initial_cash": 100_000,
    }).json()
    url = f"/api/paper/accounts/{account['id']}/orders"

    accepted = client.post(url, json={
        "symbol": "688200", "side": "BUY", "order_type": "MARKET", "quantity": 100,
    })
    assert accepted.status_code == 200, accepted.text
    order = accepted.json()["order"]
    assert order["side"] == "BUY"
    assert order["order_type"] == "MARKET"
    assert order["purpose"] == "PERMANENT"

    rejected = client.post(url, json={
        "symbol": "688200", "side": "buy", "order_type": "market", "quantity": 100,
    })
    assert rejected.status_code == 422
    fields = {item["loc"][-1] for item in rejected.json()["detail"]}
    assert fields == {"side", "order_type"}


def test_cancel_order_only_affects_its_own_account(tmp_path, monkeypatch):
    """本地单号跨账户复用时，撤单必须精确落到指定账户，不能 LIMIT 1 猜归属。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())
    accounts = []
    for index in range(2):
        account = client.post("/api/paper/accounts", json={
            "name": f"撤单归属{index}", "initial_cash": 100_000,
        }).json()
        placed = client.post(f"/api/paper/accounts/{account['id']}/orders", json={
            "symbol": "688200", "side": "BUY", "order_type": "MARKET", "quantity": 100,
        })
        assert placed.status_code == 200, placed.text
        accounts.append((account["id"], placed.json()["order"]["id"]))

    target_id, order_id = accounts[0]
    other_id, other_order_id = accounts[1]
    assert order_id == other_order_id == "O00000001"

    legacy = client.post(f"/api/paper/orders/{order_id}/cancel")
    assert legacy.status_code == 400
    assert "必须指定账户" in legacy.json()["detail"]

    cancelled = client.post(f"/api/paper/accounts/{target_id}/orders/{order_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["id"] == target_id
    assert cancelled.json()["orders"][0]["status"] == "CANCELLED"
    assert cancelled.json()["available_cash"] == cancelled.json()["cash"]

    unaffected = client.get(f"/api/paper/accounts/{other_id}").json()
    assert unaffected["orders"][0]["status"] == "PENDING"

    other_cancelled = client.post(f"/api/paper/accounts/{other_id}/orders/{other_order_id}/cancel")
    assert other_cancelled.status_code == 200
    assert other_cancelled.json()["orders"][0]["status"] == "CANCELLED"

    repeat = client.post(f"/api/paper/accounts/{target_id}/orders/{order_id}/cancel")
    assert repeat.status_code == 400
    missing = client.post(f"/api/paper/accounts/not-exist/orders/{order_id}/cancel")
    assert missing.status_code == 404


def _snapshot_bar() -> Bar:
    """腾讯实时快照：open/high/low/volume 均为全天口径，close 为当前价。"""
    return Bar(
        timestamp=datetime(2026, 11, 12, 11, 18, 24), symbol="sh.688200",
        open=235.16, high=255.0, low=232.18, close=245.58,
        volume=5_954_265.0, amount=1_460_230_000.0, timeframe="1m",
        turnover_rate=0.0295, prev_close=231.2, bid=245.5, ask=245.58,
    )


def test_snapshot_execution_bar_uses_current_price_not_day_open():
    snapshot = _snapshot_bar()
    execution = snapshot_execution_bar(snapshot)
    assert execution.open == execution.close == 245.58
    assert execution.low >= 245.5 and execution.high <= 245.58
    assert 0 < execution.volume < snapshot.volume
    assert execution.prev_close == 231.2
    assert trading_minutes_elapsed(snapshot.timestamp) == 108

    account = BrokerAccount(initial_cash=4_000_000)
    account.submit(OrderRequest(
        symbol="sh.688200", side="BUY", quantity=400, order_type="MARKET",
        purpose="PERMANENT", execution_mode="NEXT_BAR",
    ), snapshot.timestamp)
    fills = account.process_bar(execution, modes={"NEXT_BAR"})
    assert len(fills) == 1
    assert abs(fills[0].price - 245.58 * 1.0001) < 0.0001
    assert fills[0].price > 240.0  # 不再按今开 235.16 成交


def test_snapshot_execution_bar_does_not_fill_limit_order_at_day_low():
    """限价 233 落在当日最低 232.18 以上，但不能因此在当前价 245.58 成交。"""
    snapshot = _snapshot_bar()
    account = BrokerAccount(initial_cash=4_000_000)
    order = account.submit(OrderRequest(
        symbol="sh.688200", side="BUY", quantity=400, order_type="LIMIT",
        limit_price=233.0, purpose="PERMANENT", execution_mode="NEXT_BAR",
    ), snapshot.timestamp)
    assert account.process_bar(snapshot_execution_bar(snapshot), modes={"NEXT_BAR"}) == []
    assert order.status == "PENDING"


def test_fee_config_defaults_customization_and_persistence(tmp_path, monkeypatch):
    """费用配置按万分之自定义、持久化，并透传到默认配置、回测与模拟账户。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())

    initial = client.get("/api/fees").json()
    assert initial["version"] == "v1"
    assert initial["fees"] == {
        "commission_rate": 0.5, "min_commission": 5.0, "stamp_tax_rate": 5.0,
        "handling_fee_rate": 0.341, "regulatory_fee_rate": 0.2, "transfer_fee_rate": 0.1,
    }
    defaults = client.get("/api/strategy/defaults").json()
    assert defaults["fees"]["commission_rate"] == 0.5
    assert defaults["config"]["commission_rate"] == pytest.approx(0.00005)
    assert defaults["config"]["handling_fee_rate"] == pytest.approx(0.0000341)
    assert defaults["config"]["regulatory_fee_rate"] == pytest.approx(0.00002)

    custom = {
        "commission_rate": 1.0, "min_commission": 2.0, "stamp_tax_rate": 8.0,
        "handling_fee_rate": 0.5, "regulatory_fee_rate": 0.4, "transfer_fee_rate": 0.3,
    }
    assert client.put("/api/fees", json=custom).status_code == 200
    assert client.get("/api/fees").json()["fees"] == custom
    # 新应用实例读同一个 SQLite，配置确实持久化而不是只留在内存里。
    reloaded = TestClient(create_app()).get("/api/strategy/defaults").json()
    assert reloaded["config"]["commission_rate"] == pytest.approx(0.0001)
    assert reloaded["config"]["min_commission"] == 2.0

    demo = client.post("/api/datasets/demo").json()
    report = client.post("/api/backtests", json={
        "name": "费用自定义回测", "dataset_id": demo["daily"]["id"],
        "index_dataset_id": demo["index"]["id"], "initial_cash": 4_000_000,
        "config": {"enable_t": False}, "fees": {"commission_rate": 2.0},
    }).json()
    assert report["config"]["commission_rate"] == pytest.approx(0.0002)  # 本次覆盖全局
    assert report["config"]["stamp_tax_rate"] == pytest.approx(0.0008)   # 其余沿用全局

    account = client.post("/api/paper/accounts", json={
        "name": "费用账户", "initial_cash": 100_000,
    }).json()
    assert account["config"]["commission_rate"] == pytest.approx(0.0001)
    applied = client.post(
        f"/api/paper/accounts/{account['id']}/fees",
        json={"fees": {"commission_rate": 3.0}},
    ).json()
    assert applied["config"]["commission_rate"] == pytest.approx(0.0003)
    assert applied["config"]["stamp_tax_rate"] == pytest.approx(0.0008)  # 未提供的项沿用全局

    assert client.put("/api/fees", json={"commission_rate": -1}).status_code == 422
    assert client.put("/api/fees", json={"unknown_fee": 1}).status_code == 422
    assert client.post(
        f"/api/paper/accounts/{account['id']}/fees", json={"fees": {"unknown_fee": 1}},
    ).status_code == 400
    reset = client.post("/api/fees/reset").json()
    assert reset["fees"]["commission_rate"] == 0.5
    assert client.get("/api/strategy/defaults").json()["config"]["commission_rate"] == pytest.approx(0.00005)


def test_live_tick_fills_market_order_at_current_price(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    application = create_app()
    snapshot = _snapshot_bar()
    monkeypatch.setattr(application.state.market_service, "fetch_quote", lambda symbol: ({}, snapshot))
    client = TestClient(application)
    account = client.post("/api/paper/accounts", json={
        "name": "快照撮合", "initial_cash": 4_000_000,
    }).json()
    placed = client.post(f"/api/paper/accounts/{account['id']}/orders", json={
        "symbol": "688200", "side": "BUY", "order_type": "MARKET", "quantity": 400,
    })
    assert placed.status_code == 200
    tick = client.post(f"/api/paper/accounts/{account['id']}/live-tick", json={"symbol": "688200"})
    assert tick.status_code == 200, tick.text
    state = tick.json()
    assert state["orders"][0]["status"] == "FILLED"
    assert state["positions"][0]["last_price"] == 245.58
    assert abs(state["trades"][0]["price"] - 245.58 * 1.0001) < 0.0001


def test_market_and_paper_endpoints_reject_invalid_plain_code(tmp_path, monkeypatch):
    """前端只传 6 位数字；位数不对时各入口统一返回 400，而不是 500 或联网请求。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())
    assert client.post("/api/market/fetch", json={"symbol": "68820"}).status_code == 400
    assert client.get("/api/market/quote/68820").status_code == 400
    account = client.post("/api/paper/accounts", json={
        "name": "代码校验", "initial_cash": 100_000,
    }).json()
    response = client.post(f"/api/paper/accounts/{account['id']}/live-tick", json={"symbol": "68820"})
    assert response.status_code == 400
