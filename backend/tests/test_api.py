from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.broker import BrokerAccount
from app.domain import Bar, FeeConfig, OrderRequest
from app.main import (
    build_strategy_config,
    create_app,
    intraday_bucket,
    latest_closed_intraday_bar,
    snapshot_execution_bar,
    trading_intraday_bars_elapsed,
    trading_minutes_elapsed,
)
from app.storage import SQLiteStorage


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
    assert defaults["config"]["etf_beta_enabled"] is True  # 指数 beta 腿默认开启

    demo_response = client.post("/api/datasets/demo")
    assert demo_response.status_code == 200
    demo = demo_response.json()
    assert demo["daily"]["rows"] == 320
    assert demo["intraday"]["rows"] == 48 * 48
    assert demo["daily"]["is_synthetic"] is True
    assert demo["intraday"]["timeframe"] == "5m"
    assert "file_path" not in demo["daily"]
    assert len(client.get("/api/datasets").json()) == 3

    created = client.post("/api/paper/accounts", json={
        "name": "API模拟", "initial_cash": 100_000,
        "dataset_id": demo["intraday"]["id"],
    })
    assert created.status_code == 200
    account = created.json()
    assert account["config"]["etf_beta_enabled"] is True  # 新建账户默认带 beta 腿
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
        # 688200 属科创板，最小申报 200 股；旧用例的 100 股按新规则会被拒。
        response = client.post(f"/api/paper/accounts/{account['id']}/orders", json={
            "symbol": "688200", "side": "BUY", "order_type": "MARKET", "quantity": 200,
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
        "symbol": "688200", "side": "BUY", "order_type": "MARKET", "quantity": 200,
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
            "symbol": "688200", "side": "BUY", "order_type": "MARKET", "quantity": 200,
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
        volume=5_954_265.0, amount=1_460_230_000.0, timeframe="5m",
        turnover_rate=0.0295, prev_close=231.2, bid=245.5, ask=245.58,
    )


def test_snapshot_execution_bar_uses_current_price_not_day_open():
    snapshot = _snapshot_bar()
    execution = snapshot_execution_bar(snapshot)
    assert execution.open == execution.close == 245.58
    assert execution.low >= 245.5 and execution.high <= 245.58
    assert 0 < execution.volume < snapshot.volume
    assert execution.prev_close == 231.2
    assert execution.timeframe == "5m"
    assert trading_minutes_elapsed(snapshot.timestamp) == 108
    # 当日已开盘 108 分钟 → 已收盘 22 根 5 分钟 K 线（向上取整，至少 1 根）。
    assert trading_intraday_bars_elapsed(snapshot.timestamp) == 22
    assert execution.volume == pytest.approx(snapshot.volume / 22)

    account = BrokerAccount(initial_cash=4_000_000)
    account.submit(OrderRequest(
        symbol="sh.688200", side="BUY", quantity=400, order_type="MARKET",
        purpose="PERMANENT", execution_mode="NEXT_BAR",
    ), snapshot.timestamp)
    fills = account.process_bar(execution, modes={"NEXT_BAR"})
    assert len(fills) == 1
    assert abs(fills[0].price - 245.58 * 1.0001) < 0.0001
    assert fills[0].price > 240.0  # 不再按今开 235.16 成交


def test_latest_closed_intraday_bar_ignores_forming_bar():
    """5 分钟 bar 的 timestamp 是区间结束时间；未到结束时间不能拿来检测。"""
    bars = [
        Bar(timestamp=datetime(2026, 11, 12, 9, 35), symbol="sh.688200", open=1, high=1, low=1, close=1, volume=10, amount=10, timeframe="5m"),
        Bar(timestamp=datetime(2026, 11, 12, 9, 40), symbol="sh.688200", open=1, high=1, low=1, close=1, volume=10, amount=10, timeframe="5m"),
    ]
    assert intraday_bucket(datetime(2026, 11, 12, 9, 42, 30)) == datetime(2026, 11, 12, 9, 40)
    assert latest_closed_intraday_bar(bars, datetime(2026, 11, 12, 9, 42, 30)) is bars[-1]
    assert latest_closed_intraday_bar(bars, datetime(2026, 11, 12, 9, 39, 59)) is bars[0]
    assert latest_closed_intraday_bar(bars, datetime(2026, 11, 12, 9, 34, 0)) is None


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
    # 费率合并口径：全局打底 → 本次 fees 覆盖（万分之转小数），不再依赖单标的回测端点。
    global_fees = FeeConfig.from_dict(custom)
    merged = build_strategy_config(global_fees, {"commission_rate": 2.0}, {"enable_t": False})
    assert merged.commission_rate == pytest.approx(0.0002)   # 本次覆盖全局
    assert merged.stamp_tax_rate == pytest.approx(0.0008)    # 其余沿用全局
    assert merged.enable_t is False                          # 显式 config 最高优先
    assert demo["daily"]["rows"] == 320

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


def test_live_tick_without_orders_reports_quote_not_holding(tmp_path, monkeypatch):
    """只拉行情、没下任何委托时，账户不能出现持仓；最新价通过 quotes 返回。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    application = create_app()
    snapshot = _snapshot_bar()
    monkeypatch.setattr(application.state.market_service, "fetch_quote", lambda symbol: ({}, snapshot))
    client = TestClient(application)
    account = client.post("/api/paper/accounts", json={
        "name": "只看行情", "initial_cash": 4_000_000,
    }).json()
    assert client.get(f"/api/paper/accounts/{account['id']}").json()["positions"] == []

    tick = client.post(f"/api/paper/accounts/{account['id']}/live-tick", json={"symbol": "688200"})
    assert tick.status_code == 200, tick.text
    state = tick.json()
    assert state["orders"] == []
    assert state["trades"] == []
    assert state["positions"] == []
    assert state["quotes"] == [{"symbol": "sh.688200", "last_price": 245.58}]
    assert state["market_value"] == 0.0

    # 重新读账户（走 SQLite 持久化/反序列化）仍是 0 持仓 + 1 条行情。
    reloaded = client.get(f"/api/paper/accounts/{account['id']}").json()
    assert reloaded["positions"] == []
    assert reloaded["quotes"] == [{"symbol": "sh.688200", "last_price": 245.58}]


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


def test_paper_account_config_update_applies_to_new_operations(tmp_path, monkeypatch):
    """做T净利止盈/截止时间走 /config 局部更新：改完立即反映到账户 config，供下一根K线使用。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())
    demo = client.post("/api/datasets/demo").json()
    account = client.post("/api/paper/accounts", json={
        "name": "配置更新", "initial_cash": 4_000_000,
        "dataset_id": demo["intraday"]["id"],
    }).json()
    base = account["config"]["t_target_net_spread"]

    # 局部更新 t_target_net_spread（小数口径，0.03 → 0.04）
    updated = client.post(f"/api/paper/accounts/{account['id']}/config",
                          json={"t_target_net_spread": 0.04})
    assert updated.status_code == 200, updated.text
    assert updated.json()["config"]["t_target_net_spread"] == 0.04
    # 原值未被误改的其他字段保持
    assert updated.json()["config"]["enable_t"] == account["config"]["enable_t"]

    # 截止时间也能改（HH:MM）
    updated_cut = client.post(f"/api/paper/accounts/{account['id']}/config",
                             json={"t_exit_cutoff": "14:45"})
    assert updated_cut.status_code == 200, updated_cut.text
    assert updated_cut.json()["config"]["t_exit_cutoff"] == "14:45"

    # 截止日（T+0/T+1）局部更新：模拟盘面板下拉框选中即 PATCH
    updated_day = client.post(f"/api/paper/accounts/{account['id']}/config",
                              json={"t_exit_cutoff_day": "T+0"})
    assert updated_day.status_code == 200, updated_day.text
    assert updated_day.json()["config"]["t_exit_cutoff_day"] == "T+0"
    assert client.post(f"/api/paper/accounts/{account['id']}/config",
                       json={"t_exit_cutoff_day": "T+2"}).status_code == 422

    # 重读（走持久化往返）仍是新值
    reloaded = client.get(f"/api/paper/accounts/{account['id']}").json()
    assert reloaded["config"]["t_target_net_spread"] == 0.04
    assert reloaded["config"]["t_exit_cutoff"] == "14:45"
    assert reloaded["config"]["t_exit_cutoff_day"] == "T+0"

    # 费率字段归 /fees 管，这里显式拒绝
    rejected = client.post(f"/api/paper/accounts/{account['id']}/config",
                           json={"commission_rate": 0.001})
    assert rejected.status_code == 422

    # 空更新 → 400
    empty = client.post(f"/api/paper/accounts/{account['id']}/config", json={})
    assert empty.status_code == 400

    # 边界校验：净利止盈负数 / 超过 1 → 422
    assert client.post(f"/api/paper/accounts/{account['id']}/config",
                       json={"t_target_net_spread": -0.01}).status_code == 422
    assert client.post(f"/api/paper/accounts/{account['id']}/config",
                       json={"t_target_net_spread": 1.5}).status_code == 422
    # 截止时间非法：格式不匹配 pattern → 422；格式像但超出 24h → from_dict strptime 拒 → 400
    assert client.post(f"/api/paper/accounts/{account['id']}/config",
                       json={"t_exit_cutoff": "25:00"}).status_code == 400
    assert client.post(f"/api/paper/accounts/{account['id']}/config",
                       json={"t_exit_cutoff": "not-a-time"}).status_code == 422
    # 已废弃字段被 extra="forbid" 拒绝
    assert client.post(f"/api/paper/accounts/{account['id']}/config",
                       json={"t_target_spread": 0.02}).status_code == 422
    assert base is not None


def test_paper_account_config_withdrawal_fields(tmp_path, monkeypatch):
    """盈利提现字段走 /config 局部更新：改完立即反映到账户 config，下一根K线起生效。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())
    demo = client.post("/api/datasets/demo").json()
    account = client.post("/api/paper/accounts", json={
        "name": "提现配置", "initial_cash": 4_000_000,
        "dataset_id": demo["intraday"]["id"],
    }).json()
    # 默认关闭
    assert account["config"]["withdraw_mode"] == "none"

    updated = client.post(f"/api/paper/accounts/{account['id']}/config", json={
        "withdraw_mode": "per_trade", "withdraw_method": "fixed",
        "withdraw_fixed_amount": 12_000,
    })
    assert updated.status_code == 200, updated.text
    cfg = updated.json()["config"]
    assert cfg["withdraw_mode"] == "per_trade"
    assert cfg["withdraw_method"] == "fixed"
    assert cfg["withdraw_fixed_amount"] == 12_000

    # 重读（持久化往返）仍是新值
    reloaded = client.get(f"/api/paper/accounts/{account['id']}").json()["config"]
    assert reloaded["withdraw_mode"] == "per_trade"
    assert reloaded["withdraw_fixed_amount"] == 12_000

    # 非法值 → 422（Pydantic 校验：模式/方式枚举、比例 (0,1]、金额 >=0）
    assert client.post(f"/api/paper/accounts/{account['id']}/config",
                       json={"withdraw_mode": "invalid"}).status_code == 422
    assert client.post(f"/api/paper/accounts/{account['id']}/config",
                       json={"withdraw_method": "invalid"}).status_code == 422
    assert client.post(f"/api/paper/accounts/{account['id']}/config",
                       json={"withdraw_ratio": 1.5}).status_code == 422
    assert client.post(f"/api/paper/accounts/{account['id']}/config",
                       json={"withdraw_fixed_amount": -1}).status_code == 422


def test_backtest_job_flow_submit_progress_complete(tmp_path, monkeypatch):
    """后台回测任务端到端（HTTP 契约）：提交→轮询进度→COMPLETED→取报告；忙时 409。

    引擎用毫秒级替身替换（真实漏斗引擎的生命周期由 test_backtest_funnel 覆盖）。
    """
    import time
    from app.backtest_jobs import Execution

    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    application = create_app()
    client = TestClient(application)

    class _StubAccount:
        def snapshot_state(self) -> dict:
            return {"cash": 1.0}

        def restore_state(self, state) -> None:
            self.state = state

    class _StubStrategy:
        def state_dict(self) -> dict:
            return {"armed": False}

        def load_state(self, state) -> None:
            self.state = state

    class _StubEngine:
        def __init__(self) -> None:
            self.account = _StubAccount()
            self.strategy = _StubStrategy()
            self.hooks: dict = {}

        def run(self, resume_state=None) -> dict:
            self.hooks["on_progress"](1, 1, "2025-01-02", {"cursor_index": 0})
            return {
                "status": "COMPLETED", "metrics": {}, "trades": [], "orders": [],
                "equity_curve": [], "rounds": [], "events": [], "warnings": [],
                "data_info": {}, "config": {}, "name": "stub", "created_at": "2026-01-01T00:00:00",
            }

    def fake_builder(params, hooks, resume_state, checkpoint):
        engine = _StubEngine()
        engine.hooks = hooks
        report_storage = application.state.storage

        def on_complete(report):
            report["data_info"]["daily_source"] = "stub"
            report_storage.save_backtest(report)

        return Execution(engine, on_complete)

    application.state.backtest_jobs._builder = fake_builder

    funnel_payload = {
        "mode": "funnel", "name": "后台任务回测", "initial_cash": 4_000_000,
        "screener": {"boards": ["创业板"]}, "config": {},
    }

    # 缺板块 → 400
    assert client.post("/api/backtests/jobs", json={"mode": "funnel"}).status_code == 400
    # mode=single 已下线 → 422（pydantic Literal 校验）
    assert client.post("/api/backtests/jobs", json={
        "mode": "single", "dataset_id": "whatever",
    }).status_code == 422

    submitted = client.post("/api/backtests/jobs", json=funnel_payload)
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]

    # 轮询直到终态（替身引擎立即完成）
    deadline = time.time() + 30
    job = None
    while time.time() < deadline:
        job = client.get(f"/api/backtests/jobs/{job_id}").json()
        if job["status"] in {"COMPLETED", "CANCELLED", "FAILED", "INTERRUPTED"}:
            break
        time.sleep(0.05)
    assert job is not None and job["status"] == "COMPLETED", job
    assert job["progress"] == 100.0

    # 完成后 job_id 即报告 id，可取完整报告
    report = client.get(f"/api/backtests/{job_id}")
    assert report.status_code == 200
    assert report.json()["status"] == "COMPLETED"

    # 任务已结束，再次提交不再 409（串行槽已释放）
    again = client.post("/api/backtests/jobs", json={**funnel_payload, "name": "第二次"})
    assert again.status_code == 202
    second_id = again.json()["job_id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        job2 = client.get(f"/api/backtests/jobs/{second_id}").json()
        if job2["status"] in {"COMPLETED", "CANCELLED", "FAILED", "INTERRUPTED"}:
            break
        time.sleep(0.05)
    assert job2["status"] == "COMPLETED"

    # 不存在的任务 → 404
    assert client.get("/api/backtests/jobs/nope").status_code == 404


def test_abandon_deletes_terminal_job(tmp_path, monkeypatch):
    """「放弃」删除终态作业：DELETE 后 list_jobs 不再返回，刷新不再弹「可续传」横幅。

    回归：旧「放弃」只清前端状态 + 写 sessionStorage（刷新即丢），服务端那行
    CANCELLED/FAILED 任务永远还在，一刷新又被 list_jobs 拉回来重新弹横幅。
    """
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    application = create_app()
    client = TestClient(application)
    storage = application.state.storage
    # 直接落一条 CANCELLED 终态作业（模拟「已撤单」的残留任务）。
    storage.create_job({
        "id": "job-cancelled", "name": "残留任务", "status": "CANCELLED", "progress": 29.31,
        "total_days": 58, "params": {}, "created_at": "2026-11-19T09:00:00",
        "updated_at": "2026-11-19T09:05:00",
    })
    storage.save_checkpoint("job-cancelled", "2025-01-24", 12, {}, {}, {"cursor_index": 12})
    assert any(j["id"] == "job-cancelled" for j in client.get("/api/backtests/jobs").json()["jobs"])

    # 放弃 → DELETE 删掉作业行 + 检查点。
    deleted = client.delete("/api/backtests/jobs/job-cancelled")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True
    assert all(j["id"] != "job-cancelled" for j in client.get("/api/backtests/jobs").json()["jobs"])
    assert storage.get_job("job-cancelled") is None
    assert storage.get_checkpoint("job-cancelled") is None

    # 再删一次 → 404（已不存在）。
    assert client.delete("/api/backtests/jobs/job-cancelled").status_code == 404

    # RUNNING 作业不可删 → 409（可能真有 worker 在跑）。
    storage.create_job({
        "id": "job-running", "name": "在跑", "status": "RUNNING", "progress": 0.0,
        "total_days": None, "params": {}, "created_at": "2026-11-19T09:10:00",
        "updated_at": "2026-11-19T09:10:00",
    })
    assert client.delete("/api/backtests/jobs/job-running").status_code == 409
    assert storage.get_job("job-running") is not None


def test_auto_strategy_toggle_keeps_round_state(tmp_path, monkeypatch):
    """关闭自动策略=暂停调度：strategy_state 必须保留（64f692bb 加仓卡死事故回归）。

    旧实现关闭时清空 strategy_state，导致有持仓的账户重开后永远无法加仓、
    A3 超跌信号无法触发、超期降阈止盈失效（有持仓但 last_add_price=null 的
    「半重置」状态）。新语义：重开按原状态继续推进；重新开始一轮走平仓自动
    reset_round，或 POST /reset 整账户重置。
    """
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    client = TestClient(create_app())
    created = client.post("/api/paper/accounts", json={
        "name": "开关状态回归", "initial_cash": 100_000,
    })
    assert created.status_code == 200
    account_id = created.json()["id"]

    # 直接构造「本轮进行中」状态：序列已建到第 2 笔、带加仓记忆与建仓日期
    storage = SQLiteStorage(str(tmp_path / "test.db"))
    state = storage.get_paper(account_id)
    state["sequence_index"] = 1
    state["strategy_state"] = {
        "last_add_price": 145.9046, "last_add_session": 241,
        "round_entry_date": "2026-11-13", "armed": False,
    }
    storage.save_paper(state)

    off = client.post(f"/api/paper/accounts/{account_id}/auto-strategy", json={"enabled": False})
    assert off.status_code == 200
    body = off.json()
    assert body["auto_strategy"] is False
    assert body["armed_state"] == "OFF"
    assert body["sequence_index"] == 1
    # strategy_state 是内部字段，公开响应里没有；从落库状态断言保留
    persisted = storage.get_paper(account_id)
    assert persisted["strategy_state"]["last_add_price"] == 145.9046
    assert persisted["strategy_state"]["round_entry_date"] == "2026-11-13"

    on = client.post(f"/api/paper/accounts/{account_id}/auto-strategy", json={"enabled": True})
    assert on.status_code == 200
    resumed = on.json()
    assert resumed["auto_strategy"] is True
    assert resumed["sequence_index"] == 1
    persisted = storage.get_paper(account_id)
    assert persisted["strategy_state"]["last_add_price"] == 145.9046
    assert persisted["strategy_state"]["last_add_session"] == 241


def test_ensure_beta_win_rate_backfills_from_persisted_fills():
    """旧报告缺 beta_win_rate：从已持久化的 data_info.beta.fills 重算并挂进 metrics。

    构造 3 笔 SELL（2 盈 1 亏）+ 1 笔 BUY → win_rate=2/3、sell_rounds=3、sell_wins=2。
    已有 beta_win_rate 的新报告不被覆盖（setdefault）。
    """
    from app.main import _ensure_beta_win_rate

    report = {
        "metrics": {},
        "data_info": {"beta": {"enabled": True, "fills": [
            {"date": "2025-01-02", "side": "BUY", "realized_pnl": 0.0},
            {"date": "2025-01-03", "side": "SELL", "realized_pnl": 200.0},
            {"date": "2025-01-04", "side": "SELL", "realized_pnl": 100.0},
            {"date": "2025-01-05", "side": "SELL", "realized_pnl": -50.0},
        ]}},
    }
    out = _ensure_beta_win_rate(report)
    assert out["metrics"]["beta_win_rate"] == pytest.approx(2 / 3)
    assert out["metrics"]["beta_sell_rounds"] == 3
    assert out["metrics"]["beta_sell_wins"] == 2


def test_ensure_beta_win_rate_no_overwrite_and_skips_when_absent():
    """已有 beta_win_rate 不覆盖；缺 beta/fills 或无 SELL 时跳过（保持原样）。"""
    from app.main import _ensure_beta_win_rate

    # 已有值：不覆盖
    has = {"metrics": {"beta_win_rate": 0.9},
           "data_info": {"beta": {"fills": [{"side": "SELL", "realized_pnl": -1.0}]}}}
    assert _ensure_beta_win_rate(has)["metrics"]["beta_win_rate"] == 0.9

    # 缺 beta / fills：原样返回
    no_beta = {"metrics": {}, "data_info": {}}
    assert _ensure_beta_win_rate(no_beta)["metrics"] == {}
    empty_fills = {"metrics": {}, "data_info": {"beta": {"fills": []}}}
    assert _ensure_beta_win_rate(empty_fills)["metrics"] == {}

    # 只有 BUY、无 SELL：跳过（win_rate 无定义）
    only_buy = {"metrics": {}, "data_info": {"beta": {"fills": [{"side": "BUY", "realized_pnl": 0.0}]}}}
    assert _ensure_beta_win_rate(only_buy)["metrics"] == {}


def test_ensure_extended_metrics_backfills_stock_win_rate():
    """旧报告 metrics 已有 xirr/robustness/underwater 但缺 stock_win_rate：读时仍要触发重算补上。

    回归护栏——stock_win_rate 加进 _ensure_extended_metrics 的重算触发条件之前，
    这类「三项齐全的旧报告」不会重算 performance_metrics，导致选股胜率一直缺失。
    """
    from app.main import _ensure_extended_metrics

    # 两轮已闭合：账户口径两轮都胜（win_rate=1.0），选股口径只 1 轮胜（stock_win_rate=0.5）。
    report = {
        "metrics": {
            "initial_cash": 100.0, "total_fees": 0.0, "t_net_profit": 0.0,
            "max_invested": 0.0, "max_float_loss": 0.0, "buy_hold_return": 0.0,
            "total_withdrawn": 0.0, "withdrawn_count": 0,
            # 三项齐全 → 修复前不会触发重算
            "xirr_annualized": 0.1, "robustness": {}, "underwater": {},
        },
        "equity_curve": [
            {"date": "2025-01-01", "equity": 100.0, "market_value": 50.0},
            {"date": "2025-01-02", "equity": 120.0, "market_value": 60.0},
            {"date": "2025-01-03", "equity": 130.0, "market_value": 65.0},
        ],
        "rounds": [
            {"status": "CLOSED", "pnl": 100.0, "stock_pnl": 80.0},
            {"status": "CLOSED", "pnl": 30.0, "stock_pnl": -20.0},
        ],
        "trades": [], "withdrawals": [],
    }
    out = _ensure_extended_metrics(report)
    assert out["metrics"]["stock_win_rate"] == pytest.approx(0.5)
    # win_rate 是核心字段（真实报告本就带），本函数只兜底 4 个扩展键、不回填它。


def test_ensure_equity_incl_withdrawal_backfills_per_point():
    """旧报告 equity_curve 缺「含出金权益」：按累计提现逐点补齐，提现日之前等于原始权益。"""
    from app.main import _ensure_equity_incl_withdrawal

    curve = [
        {"date": "2025-01-01", "equity": 100.0},
        {"date": "2025-01-02", "equity": 110.0},
        {"date": "2025-01-03", "equity": 80.0},
    ]
    report = {"equity_curve": curve, "withdrawals": [{"date": "2025-01-03", "amount": 40.0}]}
    out = _ensure_equity_incl_withdrawal(report)
    assert [point["equity_incl_withdrawal"] for point in out["equity_curve"]] == [
        pytest.approx(100.0), pytest.approx(110.0), pytest.approx(120.0),
    ]


def test_ensure_equity_incl_withdrawal_idempotent_and_skips_without_data():
    """已有该字段不覆盖；缺 equity_curve 或无提现明细时原样返回（幂等）。"""
    from app.main import _ensure_equity_incl_withdrawal

    done = {"equity_curve": [{"date": "2025-01-01", "equity": 100.0,
                             "equity_incl_withdrawal": 200.0}]}
    assert _ensure_equity_incl_withdrawal(done)["equity_curve"][0]["equity_incl_withdrawal"] == 200.0

    no_curve = {"withdrawals": []}
    assert _ensure_equity_incl_withdrawal(no_curve) == no_curve
    empty_curve = {"equity_curve": [], "withdrawals": []}
    assert _ensure_equity_incl_withdrawal(empty_curve) == empty_curve
