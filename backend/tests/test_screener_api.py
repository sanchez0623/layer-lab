"""选股 API + 轮动 v2（清仓后重新选股）API 测试。

外部行情与选股引擎全部用替身：不联网；数据库与数据目录通过 tmp_path +
WB_DATA_DIR/WB_DB_PATH 隔离。
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.broker import BrokerAccount
from app.data import dataset_from_bars, write_bars_csv
from app.domain import Bar, Dataset, Position
from app.main import create_app
from app.rotation import set_from_run as set_rotation_from_run


def make_bars(symbol: str, closes: list[float]) -> list[Bar]:
    start = datetime(2026, 10, 1)
    bars: list[Bar] = []
    previous: float | None = None
    for offset, close in enumerate(closes):
        bars.append(Bar(
            timestamp=start + timedelta(days=offset), symbol=symbol,
            open=close, high=close, low=close, close=close,
            volume=1_000_000.0, amount=close * 1_000_000.0,
            timeframe="daily", prev_close=previous,
        ))
        previous = close
    return bars


def candidate(code: str, name: str, board: str, chg20d: float) -> dict:
    return {
        "code": code, "name": name, "board": board, "close": 245.58,
        "prev_close": 231.2, "as_of": "2026-11-11", "chg20d": chg20d,
        "week52_high": 420.0, "drawdown_52w": 0.415, "day_change": 0.02,
        "amount": 1.46e9, "avg_amount_20d": 9.8e8, "turnover": 0.0295,
        "sessions": 250, "score": chg20d, "checks": [], "vetoed_by": None,
        "deep_checked": True,
    }


HUA_FENG = candidate("sh.688200", "华峰测控", "科创板", -0.3153)
TU_NAN = candidate("sz.300855", "图南股份", "创业板", -0.2812)


def run_payload(run_id: str = "run_0001", gate_open: bool = True) -> dict:
    eligible = [HUA_FENG, TU_NAN] if gate_open else []
    return {
        "id": run_id, "created_at": "2026-11-12T10:00:00", "as_of": "2026-11-11",
        "config": {"chg20_max": -0.15},
        "gate": {
            "boards": {
                "创业板": {"index": "sz.399006", "ret20": -0.1028, "open": True, "date": "2026-11-11"},
                "科创板": {"index": "sh.000688", "ret20": -0.0579, "open": True, "date": "2026-11-11"},
            },
            "open": gate_open,
        },
        "funnel": {"universe": 1900, "after_l0": 1800, "after_l1": 191,
                   "after_l2": 180, "after_l3": 20, "eligible": len(eligible), "deep_checked": 2},
        "pool": [HUA_FENG, TU_NAN], "eligible": eligible,
        "first": HUA_FENG if gate_open else None,
        "warnings": [], "data_info": {"realtime": False}, "elapsed_seconds": 1.25,
    }


def rescreen_payload(
    *, run_id: str, first: dict | None, eligible: list[dict],
    gate_open: bool = True, warnings: list[str] | None = None,
) -> dict:
    payload = run_payload(run_id=run_id, gate_open=gate_open)
    payload.update({
        "eligible": eligible, "pool": eligible, "first": first,
        "warnings": list(warnings or []),
        "gate": {"boards": payload["gate"]["boards"], "open": gate_open},
    })
    return payload


class FakeResult:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def to_dict(self) -> dict:
        return self.payload


class FakeScreenerService:
    """只实现 main.py 用到的服务接口，行情/引擎均为替身。"""

    def __init__(self) -> None:
        self.refresh_calls: list[dict] = []
        self.run_calls: list[dict] = []
        self.refresh_error: str | None = None
        self.run_error: Exception | None = None
        self.run_payload: dict = run_payload()

    def defaults(self) -> dict:
        return {
            "config": {"boards": ["创业板", "科创板"], "chg20_max": -0.15},
            "boards": ["创业板", "科创板"],
            "coverage": {"codes": 0, "rows": 0, "boards": {}},
            "sources": {"baostock": "installed"},
            "notes": ["测试替身"],
        }

    def coverage(self) -> dict:
        return {
            "coverage": {"codes": 12, "rows": 2400, "missing_codes": 0},
            "refresh": {"running": False, "job": {"done": 12, "total": 12}},
        }

    def refresh(self, **kwargs) -> dict:
        if self.refresh_error:
            raise ValueError(self.refresh_error)
        self.refresh_calls.append(kwargs)
        return {"status": "started", "job": {"running": True, "done": 0, "total": 12}}

    def refresh_status(self) -> dict:
        return {
            "running": False, "job": {"done": 12, "total": 12, "errors": []},
            "coverage": {"codes": 12},
        }

    def run(self, *, config=None, as_of=None, include_realtime=False):
        self.run_calls.append({
            "config": config, "as_of": as_of, "include_realtime": include_realtime,
        })
        if self.run_error is not None:
            raise self.run_error
        return FakeResult(self.run_payload)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    application = create_app()
    service = FakeScreenerService()
    application.state.screener_service = service
    return application, TestClient(application), service


def fake_bundle(tmp_path) -> dict:
    """prepare 用替身数据集：只消费元数据，不需要真实 CSV 文件。"""

    def dataset(dataset_id: str, symbol: str, timeframe: str, created_at: str) -> Dataset:
        return Dataset(
            id=dataset_id, name=f"{symbol} 测试数据", symbol=symbol,
            timeframe=timeframe, start="2026-10-01T00:00:00", end="2026-11-11T00:00:00",
            rows=40, source="baostock", is_synthetic=False, created_at=created_at,
            file_path=str(tmp_path / "cache" / f"{dataset_id}.csv"),
        )

    return {
        "daily": dataset("ds_fake_daily", "SYNTH001", "daily", "2099-11-12T09:00:00"),
        "index": dataset("ds_fake_index", "sh.000688", "daily", "2099-11-12T09:00:01"),
        "intraday": None,
    }


def seed_daily(application, tmp_path, symbol: str, closes: list[float]) -> Dataset:
    bars = make_bars(symbol, closes)
    path = tmp_path / "cache" / f"{symbol.replace('.', '_')}_daily.csv"
    write_bars_csv(path, bars)
    dataset = dataset_from_bars(
        bars, f"{symbol} 测试日线", symbol, "daily", "baostock", False, str(path),
    )
    application.state.storage.save_dataset(dataset)
    return dataset


def snapshot_bar(symbol: str = "sh.688200", minute: int = 0, close: float = 245.58) -> Bar:
    return Bar(
        timestamp=datetime(2026, 11, 12, 10, minute), symbol=symbol,
        open=close, high=close, low=close, close=close,
        volume=1_000_000.0, amount=close * 1_000_000.0, timeframe="1m",
        prev_close=240.0, bid=close - 0.05, ask=close,
    )


def patch_quotes(monkeypatch, application, *bars: Bar) -> None:
    """依次返回给定的实时快照 bar。"""
    sequence = list(bars)

    def fake_fetch_quote(symbol):
        bar = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return {}, bar

    monkeypatch.setattr(application.state.market_service, "fetch_quote", fake_fetch_quote)
    monkeypatch.setattr(
        application.state.market_service, "fetch_tencent_minutes",
        lambda symbol, count=320: ([], {}),
    )


def seed_live_tick_inputs(application, tmp_path, *symbols: str) -> dict[str, Dataset]:
    """live-tick 自动策略分支需要日线 + 指数数据集，全部用本地 CSV 避免联网。"""
    datasets = {
        symbol: seed_daily(application, tmp_path, symbol, [245.0] * 30)
        for symbol in symbols
    }
    seed_daily(application, tmp_path, "sz.399006", [100.0] * 5 + [100.0 - index for index in range(25)])
    return datasets


def prepare_account_with_pending_rotation(application, account_id: str, *, pending: bool = True) -> None:
    """把一个模拟账户落成「轮动已启用、本轮刚清仓」的状态。"""
    stored = application.state.storage.get_paper(account_id)
    account = BrokerAccount.from_dict(stored)
    account.auto_strategy = True
    set_rotation_from_run(account, run_payload(), enabled=True, current_symbol="sh.688200")
    account.rotation_pending_advance = pending
    application.state.storage.save_paper(account.to_dict(include_internal=True))


# ---------------------------------------------------------------------------
# 选股端点
# ---------------------------------------------------------------------------


def test_screener_defaults_and_coverage(api):
    _, client, _ = api
    defaults = client.get("/api/screener/defaults")
    assert defaults.status_code == 200
    assert defaults.json()["boards"] == ["创业板", "科创板"]
    assert defaults.json()["config"]["chg20_max"] == -0.15

    coverage = client.get("/api/screener/coverage")
    assert coverage.status_code == 200
    payload = coverage.json()
    assert payload["coverage"]["codes"] == 12
    assert payload["refresh"]["running"] is False


def test_screener_refresh_starts_conflicts_and_validation(api):
    _, client, service = api
    started = client.post("/api/screener/refresh", json={
        "boards": ["创业板"], "lookback_days": 120, "force": True,
    })
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "started"
    assert service.refresh_calls == [{
        "boards": ["创业板"], "codes": None, "lookback_days": 120, "force": True,
    }]
    status = client.get("/api/screener/refresh")
    assert status.status_code == 200
    assert status.json()["job"]["done"] == 12

    service.refresh_error = "刷新任务已在运行"
    conflict = client.post("/api/screener/refresh", json={})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "刷新任务已在运行"

    service.refresh_error = "非法代码: XYZ"
    invalid = client.post("/api/screener/refresh", json={"codes": ["XYZ"]})
    assert invalid.status_code == 400
    assert "非法代码" in invalid.json()["detail"]

    assert client.post("/api/screener/refresh", json={"lookback_days": 10}).status_code == 422
    assert client.post("/api/screener/refresh", json={"lookback_days": 2001}).status_code == 422


def test_screener_run_persists_and_lists_runs(api):
    application, client, service = api
    payload = run_payload()
    service.run_payload = payload

    response = client.post("/api/screener/run", json={"as_of": "2026-11-11"})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == "run_0001"
    assert service.run_calls == [{
        "config": None, "as_of": "2026-11-11", "include_realtime": False,
    }]
    storage = application.state.storage
    assert storage.get_screener_run("run_0001") == payload

    runs = client.get("/api/screener/runs").json()
    assert len(runs) == 1
    assert runs[0] == {
        "id": "run_0001", "created_at": "2026-11-12T10:00:00", "as_of": "2026-11-11",
        "gate_open": True, "eligible_count": 2,
        "first": {"code": "sh.688200", "name": "华峰测控", "chg20d": -0.3153},
    }

    detail = client.get("/api/screener/runs/run_0001")
    assert detail.status_code == 200
    assert detail.json()["first"]["code"] == "sh.688200"
    assert client.get("/api/screener/runs/not-exist").status_code == 404

    assert client.delete("/api/screener/runs/run_0001").json() == {"deleted": True}
    assert client.get("/api/screener/runs/run_0001").status_code == 404
    assert client.delete("/api/screener/runs/run_0001").json() == {"deleted": False}


def test_screener_run_passes_config_and_realtime_flags(api):
    _, client, service = api
    response = client.post("/api/screener/run", json={
        "config": {"chg20_max": -0.20, "min_amount": 1e9}, "include_realtime": True,
    })
    assert response.status_code == 200, response.text
    passed = service.run_calls[-1]
    assert passed["config"].to_dict()["chg20_max"] == -0.20
    assert passed["include_realtime"] is True


def test_screener_run_rejects_unknown_config_fields(api):
    _, client, _ = api
    response = client.post("/api/screener/run", json={"config": {"unknown_flag": 1}})
    assert response.status_code == 400
    assert "未知选股参数" in response.json()["detail"]


def test_prepare_requires_existing_run_and_candidate(api):
    application, client, _ = api
    storage = application.state.storage
    assert client.post(
        "/api/screener/runs/missing/prepare", json={"target": "backtest"},
    ).status_code == 404

    storage.save_screener_run(run_payload(gate_open=False))
    no_candidate = client.post(
        "/api/screener/runs/run_0001/prepare", json={"target": "backtest"},
    )
    assert no_candidate.status_code == 400
    assert "没有可用候选标的" in no_candidate.json()["detail"]

    storage.save_screener_run(run_payload())
    unknown = client.post("/api/screener/runs/run_0001/prepare", json={
        "target": "backtest", "candidate_code": "sh.600000",
    })
    assert unknown.status_code == 400
    assert "不在本次选股结果中" in unknown.json()["detail"]


def test_prepare_backtest_saves_datasets_without_account(api, tmp_path, monkeypatch):
    application, client, _ = api
    application.state.storage.save_screener_run(run_payload())
    bundle = fake_bundle(tmp_path)
    monkeypatch.setattr(
        application.state.market_service, "fetch_history_bundle",
        lambda symbol, **kwargs: bundle,
    )

    response = client.post("/api/screener/runs/run_0001/prepare", json={"target": "backtest"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["target"] == "backtest"
    assert payload["candidate"]["code"] == "sh.688200"
    assert payload["datasets"]["daily"]["id"] == "ds_fake_daily"
    assert payload["datasets"]["intraday"] is None
    assert payload["account"] is None
    assert application.state.storage.get_dataset("ds_fake_daily") is not None


def test_prepare_paper_builds_rescreen_rotation_account(api, tmp_path, monkeypatch):
    application, client, _ = api
    application.state.storage.save_screener_run(run_payload())
    bundle = fake_bundle(tmp_path)
    monkeypatch.setattr(
        application.state.market_service, "fetch_history_bundle",
        lambda symbol, **kwargs: bundle,
    )

    created = client.post("/api/screener/runs/run_0001/prepare", json={
        "target": "paper", "auto_strategy": True,
    })
    assert created.status_code == 200, created.text
    payload = created.json()
    account = payload["account"]
    assert payload["target"] == "paper"
    assert account["dataset_id"] == "ds_fake_daily"
    assert account["auto_strategy"] is True
    rotation = account["rotation"]
    assert rotation["mode"] == "rescreen"
    assert rotation["enabled"] is True
    assert rotation["run_id"] == "run_0001"
    assert rotation["last_run_id"] == "run_0001"
    assert rotation["last_attempt_date"] is None
    assert rotation["index"] == 0
    assert rotation["current_symbol"] == "sh.688200"
    assert rotation["note"] == "换票→sh.688200"
    assert [item["symbol"] for item in rotation["pool"]] == ["sh.688200", "sz.300855"]
    assert rotation["last_screen"]["first"]["code"] == "sh.688200"
    assert rotation["last_screen"]["eligible_count"] == 2
    assert rotation["last_screen"]["gate_open"] is True
    stored = application.state.storage.get_paper(account["id"])
    assert stored["rotation_config"] == {"chg20_max": -0.15}

    # 指定候选时序号指向该候选，但配置/快照仍来自同一个 run。
    other = client.post("/api/screener/runs/run_0001/prepare", json={
        "target": "paper", "candidate_code": "sz.300855",
    })
    assert other.status_code == 200, other.text
    other_rotation = other.json()["account"]["rotation"]
    assert other_rotation["index"] == 1
    assert other_rotation["current_symbol"] == "sz.300855"
    assert other_rotation["note"] == "换票→sz.300855"

    # 传入已存在账户时复用其初始资金/名称与账户 id。
    existing = client.post("/api/paper/accounts", json={
        "name": "旧账户", "initial_cash": 123_456,
    }).json()
    reused = client.post("/api/screener/runs/run_0001/prepare", json={
        "target": "paper", "account_id": existing["id"], "rotation": False,
    })
    assert reused.status_code == 200, reused.text
    reused_account = reused.json()["account"]
    assert reused_account["id"] == existing["id"]
    assert reused_account["initial_cash"] == 123_456
    assert reused_account["rotation"]["enabled"] is False
    assert reused_account["rotation"]["pool"] == []
    assert reused_account["rotation"]["last_screen"] == {}


# ---------------------------------------------------------------------------
# 轮动端点（v2：记录配置 + 最近筛选快照）
# ---------------------------------------------------------------------------


def test_set_rotation_from_run_records_config_snapshot_and_toggle(api):
    application, client, _ = api
    application.state.storage.save_screener_run(run_payload())
    account = client.post("/api/paper/accounts", json={
        "name": "轮动账户", "initial_cash": 100_000,
    }).json()

    enabled = client.post(f"/api/paper/accounts/{account['id']}/rotation", json={
        "enabled": True, "run_id": "run_0001",
    })
    assert enabled.status_code == 200, enabled.text
    rotation = enabled.json()["rotation"]
    assert rotation["mode"] == "rescreen"
    assert rotation["enabled"] is True
    assert rotation["run_id"] == "run_0001"
    assert rotation["last_run_id"] == "run_0001"
    assert rotation["last_attempt_date"] is None
    assert rotation["index"] == 0
    assert rotation["current_symbol"] == "sh.688200"
    assert [item["symbol"] for item in rotation["pool"]] == ["sh.688200", "sz.300855"]
    assert rotation["last_screen"]["run_id"] == "run_0001"
    assert rotation["last_screen"]["first"]["code"] == "sh.688200"
    assert rotation["last_screen"]["as_of"] == "2026-11-11"
    stored = application.state.storage.get_paper(account["id"])
    assert stored["rotation_config"] == {"chg20_max": -0.15}
    assert stored["rotation_pending_advance"] is False

    # 仅关闭开关：保留配置和最近一次筛选快照。
    disabled = client.post(f"/api/paper/accounts/{account['id']}/rotation", json={"enabled": False})
    assert disabled.status_code == 200
    disabled_rotation = disabled.json()["rotation"]
    assert disabled_rotation["enabled"] is False
    assert disabled_rotation["last_run_id"] == "run_0001"
    assert disabled_rotation["last_screen"]["first"]["code"] == "sh.688200"
    assert disabled_rotation["index"] == 0

    # 重新开启（不带 run）：快照仍在。
    reenabled = client.post(f"/api/paper/accounts/{account['id']}/rotation", json={"enabled": True})
    assert reenabled.json()["rotation"]["enabled"] is True
    assert reenabled.json()["rotation"]["last_run_id"] == "run_0001"


def test_reset_keeps_rotation_config_and_snapshot_but_resets_state(api):
    application, client, _ = api
    application.state.storage.save_screener_run(run_payload())
    account = client.post("/api/paper/accounts", json={
        "name": "重置轮动", "initial_cash": 100_000,
    }).json()
    client.post(f"/api/paper/accounts/{account['id']}/rotation", json={
        "enabled": True, "run_id": "run_0001",
    })

    reset = client.post(f"/api/paper/accounts/{account['id']}/reset")
    assert reset.status_code == 200
    rotation = reset.json()["rotation"]
    assert rotation["enabled"] is True
    assert rotation["run_id"] == "run_0001"
    assert [item["symbol"] for item in rotation["pool"]] == ["sh.688200", "sz.300855"]
    assert rotation["last_run_id"] == "run_0001"
    assert rotation["last_screen"]["first"]["code"] == "sh.688200"
    assert rotation["index"] == -1
    assert rotation["current_symbol"] is None
    assert rotation["waiting"] is False
    assert rotation["note"] == ""
    stored = application.state.storage.get_paper(account["id"])
    assert stored["rotation_config"] == {"chg20_max": -0.15}
    assert stored["rotation_pending_advance"] is False


def test_set_rotation_manual_pool_and_error_paths(api):
    application, client, _ = api
    application.state.storage.save_screener_run(run_payload())
    account = client.post("/api/paper/accounts", json={
        "name": "手工池", "initial_cash": 100_000,
    }).json()

    manual = client.post(f"/api/paper/accounts/{account['id']}/rotation", json={
        "enabled": True,
        "pool": [{"code": "688200", "name": "华峰测控", "board": "科创板"}],
    })
    assert manual.status_code == 200, manual.text
    manual_rotation = manual.json()["rotation"]
    assert manual_rotation["pool"][0]["symbol"] == "sh.688200"
    assert manual_rotation["index"] == -1
    assert manual_rotation["current_symbol"] is None

    missing = client.post(f"/api/paper/accounts/{account['id']}/rotation", json={
        "enabled": True, "run_id": "missing-run",
    })
    assert missing.status_code == 404

    bad_pool = client.post(f"/api/paper/accounts/{account['id']}/rotation", json={
        "enabled": True, "pool": [{"name": "缺代码"}],
    })
    assert bad_pool.status_code == 400


# ---------------------------------------------------------------------------
# live-tick 轮动 v2 接线
# ---------------------------------------------------------------------------


def test_live_tick_rescreens_and_switches_to_new_first(api, tmp_path, monkeypatch):
    application, client, service = api
    account = client.post("/api/paper/accounts", json={
        "name": "轮动实盘", "initial_cash": 4_000_000, "auto_strategy": True,
    }).json()
    prepare_account_with_pending_rotation(application, account["id"])
    datasets = seed_live_tick_inputs(application, tmp_path, "sh.688200", "sz.300855")
    # 清仓后重筛：最新第一名换成 sz.300855。
    service.run_payload = rescreen_payload(
        run_id="run_rescreen_switch", first=TU_NAN, eligible=[TU_NAN, HUA_FENG],
    )
    patch_quotes(monkeypatch, application, snapshot_bar())

    response = client.post(f"/api/paper/accounts/{account['id']}/live-tick", json={"symbol": "688200"})
    assert response.status_code == 200, response.text
    assert len(service.run_calls) == 1
    call = service.run_calls[0]
    assert call["include_realtime"] is False
    assert call["config"].to_dict()["chg20_max"] == -0.15

    state = response.json()
    rotation = state["rotation"]
    assert rotation["mode"] == "rescreen"
    assert rotation["current_symbol"] == "sz.300855"
    assert rotation["index"] == 0
    assert rotation["waiting"] is False
    assert rotation["note"] == "换票→sz.300855（重新选股）"
    assert rotation["last_run_id"] == "run_rescreen_switch"
    assert rotation["last_attempt_date"] == "2026-11-12"
    assert rotation["last_screen"]["first"]["code"] == "sz.300855"
    assert rotation["last_screen"]["eligible_count"] == 2
    assert state["dataset_id"] == datasets["sz.300855"].id
    stored = application.state.storage.get_paper(account["id"])
    assert stored["strategy_state"] == {}
    assert stored["sequence_index"] == 0
    assert stored["last_daily_signal_date"] is None
    assert stored["armed_state"] == "OFF"
    assert stored["rotation_pending_advance"] is False
    # 重筛结果已落库，可被 /api/screener/runs 回看。
    assert application.state.storage.get_screener_run("run_rescreen_switch") is not None


def test_live_tick_rescreen_same_first_does_not_switch(api, tmp_path, monkeypatch):
    application, client, service = api
    account = client.post("/api/paper/accounts", json={
        "name": "同标的重筛", "initial_cash": 4_000_000, "auto_strategy": True,
    }).json()
    prepare_account_with_pending_rotation(application, account["id"])
    seed_live_tick_inputs(application, tmp_path, "sh.688200")
    service.run_payload = rescreen_payload(
        run_id="run_rescreen_same", first=HUA_FENG, eligible=[HUA_FENG, TU_NAN],
    )
    patch_quotes(monkeypatch, application, snapshot_bar())

    response = client.post(f"/api/paper/accounts/{account['id']}/live-tick", json={"symbol": "688200"})
    assert response.status_code == 200, response.text
    state = response.json()
    rotation = state["rotation"]
    assert rotation["current_symbol"] == "sh.688200"
    assert rotation["waiting"] is False
    assert rotation["note"] == "重新建仓→sh.688200（清仓后重新选股仍为该标的）"
    assert rotation["last_run_id"] == "run_rescreen_same"
    assert state["dataset_id"] is None  # 不换票，账户数据集保持原样（本来未绑定）
    stored = application.state.storage.get_paper(account["id"])
    assert stored["rotation_pending_advance"] is False


def test_live_tick_l0_closed_waits_and_rescreens_once_per_day(api, tmp_path, monkeypatch):
    application, client, service = api
    account = client.post("/api/paper/accounts", json={
        "name": "L0关门", "initial_cash": 4_000_000, "auto_strategy": True,
    }).json()
    prepare_account_with_pending_rotation(application, account["id"])
    seed_live_tick_inputs(application, tmp_path, "sh.688200")
    service.run_payload = rescreen_payload(
        run_id="run_l0_closed", first=None, eligible=[], gate_open=False,
    )
    patch_quotes(monkeypatch, application, snapshot_bar(minute=0), snapshot_bar(minute=5))

    first = client.post(f"/api/paper/accounts/{account['id']}/live-tick", json={"symbol": "688200"})
    assert first.status_code == 200, first.text
    rotation = first.json()["rotation"]
    assert rotation["waiting"] is True
    assert rotation["note"] == "L0 大盘门关闭，空仓等待"
    assert rotation["last_run_id"] == "run_l0_closed"
    assert rotation["last_attempt_date"] == "2026-11-12"

    # 同一交易日第二次 tick：不再重筛，pending 保留等次日。
    second = client.post(f"/api/paper/accounts/{account['id']}/live-tick", json={"symbol": "688200"})
    assert second.status_code == 200, second.text
    assert len(service.run_calls) == 1
    stored = application.state.storage.get_paper(account["id"])
    assert stored["rotation_pending_advance"] is True
    assert stored["rotation"]["waiting"] is True


def test_live_tick_non_current_symbol_does_not_rescreen(api, tmp_path, monkeypatch):
    application, client, service = api
    account = client.post("/api/paper/accounts", json={
        "name": "非当前标的", "initial_cash": 4_000_000, "auto_strategy": True,
    }).json()
    prepare_account_with_pending_rotation(application, account["id"])
    seed_live_tick_inputs(application, tmp_path, "sh.688200", "sz.300855")
    patch_quotes(monkeypatch, application, snapshot_bar(symbol="sz.300855", close=40.0))

    response = client.post(f"/api/paper/accounts/{account['id']}/live-tick", json={"symbol": "300855"})
    assert response.status_code == 200, response.text
    assert service.run_calls == []
    rotation = response.json()["rotation"]
    assert rotation["current_symbol"] == "sh.688200"
    assert rotation["last_attempt_date"] is None


def test_fresh_rotation_account_does_not_rescreen_before_first_round(api, tmp_path, monkeypatch):
    """新带入的轮动账户虽然空仓，但 pending=False，不能还没建仓就重新选股。"""
    application, client, service = api
    application.state.storage.save_screener_run(run_payload())
    account = client.post("/api/paper/accounts", json={
        "name": "新轮动账户", "initial_cash": 4_000_000, "auto_strategy": True,
    }).json()
    stored = application.state.storage.get_paper(account["id"])
    broker_account = BrokerAccount.from_dict(stored)
    broker_account.auto_strategy = True
    set_rotation_from_run(broker_account, run_payload(), enabled=True)
    application.state.storage.save_paper(broker_account.to_dict(include_internal=True))
    seed_live_tick_inputs(application, tmp_path, "sh.688200")
    patch_quotes(monkeypatch, application, snapshot_bar())

    response = client.post(f"/api/paper/accounts/{account['id']}/live-tick", json={"symbol": "688200"})
    assert response.status_code == 200, response.text
    assert service.run_calls == []
    rotation = response.json()["rotation"]
    assert rotation["index"] == 0
    assert rotation["current_symbol"] == "sh.688200"
    assert rotation["waiting"] is False


def test_live_tick_rescreen_failure_keeps_account_usable(api, tmp_path, monkeypatch):
    application, client, service = api
    account = client.post("/api/paper/accounts", json={
        "name": "重筛失败", "initial_cash": 4_000_000, "auto_strategy": True,
    }).json()
    prepare_account_with_pending_rotation(application, account["id"])
    seed_live_tick_inputs(application, tmp_path, "sh.688200")
    service.run_error = RuntimeError("选股引擎暂不可用")
    patch_quotes(monkeypatch, application, snapshot_bar())

    response = client.post(f"/api/paper/accounts/{account['id']}/live-tick", json={"symbol": "688200"})
    assert response.status_code == 200, response.text
    rotation = response.json()["rotation"]
    assert rotation["waiting"] is True
    assert "重新选股失败" in rotation["note"]
    assert rotation["current_symbol"] == "sh.688200"
    stored = application.state.storage.get_paper(account["id"])
    assert stored["rotation_pending_advance"] is True
    assert stored["rotation_last_attempt_date"] == "2026-11-12"


def test_rotation_targets_held_symbol_when_enabled_from_run(api):
    """账户已持有标的（≠ 选股 first）时启用轮动，当前轮动标的需要指向持仓，
    否则该持仓清仓后的 tick 永远不会触发重筛。"""
    application, client, _ = api
    application.state.storage.save_screener_run(run_payload())
    account = client.post("/api/paper/accounts", json={
        "name": "持仓中启用轮动", "initial_cash": 4_000_000,
    }).json()

    # 直接落一个 sz.300855 持仓（run_payload 的 first 是 sh.688200）。
    stored = application.state.storage.get_paper(account["id"])
    broker_account = BrokerAccount.from_dict(stored)
    broker_account.positions["sz.300855"] = Position(
        symbol="sz.300855", permanent_qty=100, permanent_cost=40.0 * 100,
        last_price=40.0,
    )
    application.state.storage.save_paper(broker_account.to_dict(include_internal=True))

    response = client.post(f"/api/paper/accounts/{account['id']}/rotation", json={
        "enabled": True, "run_id": "run_0001",
    })
    assert response.status_code == 200, response.text
    rotation = response.json()["rotation"]
    assert rotation["current_symbol"] == "sz.300855"
    assert rotation["pool"][rotation["index"]]["symbol"] == "sz.300855"
    assert rotation["last_run_id"] == "run_0001"
    assert rotation["last_screen"]["first"]["code"] == "sh.688200"
