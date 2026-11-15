"""回测页表单草稿（GET/PUT/DELETE /api/backtest/config-draft）测试。

目标：用户改过的回测配置要落库持久化，下次进入回测页自动恢复，不再被
/strategy/defaults 重置；同时草稿的校验口径必须与真正跑回测一致。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    return TestClient(create_app())


def _payload() -> dict:
    return {
        "mode": "funnel",
        "name": "草稿回测",
        "initial_cash": 2_000_000,
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "config": {
            "enable_t": False,
            "etf_beta_enabled": False,
            "etf_position_pct": 0.3,
            "max_holding_days": None,
        },
        "fees": {"commission_rate": 0.4, "min_commission": 3},
        "screener": {"boards": ["科创板"], "chg20_max": -0.12},
    }


def test_backtest_config_draft_roundtrip_survives_restart(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    initial = client.get("/api/backtest/config-draft").json()
    assert initial["draft"] is None and initial["updated_at"] is None

    payload = _payload()
    saved = client.put("/api/backtest/config-draft", json=payload)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["draft"]["config"]["etf_beta_enabled"] is False
    assert body["draft"]["config"]["max_holding_days"] is None
    assert body["draft"]["fees"]["commission_rate"] == pytest.approx(0.4)
    assert body["draft"]["screener"]["boards"] == ["科创板"]
    assert body["updated_at"]

    # 重新建 app（等价于刷新 / 重启服务）后仍能读到同一份草稿。
    restarted = _client(tmp_path, monkeypatch)
    reread = restarted.get("/api/backtest/config-draft").json()
    assert reread["draft"] == body["draft"]
    assert reread["updated_at"] == body["updated_at"]


def test_backtest_config_draft_delete_resets(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.put("/api/backtest/config-draft", json=_payload()).status_code == 200

    deleted = client.delete("/api/backtest/config-draft")
    assert deleted.status_code == 200
    assert deleted.json()["draft"] is None
    assert client.get("/api/backtest/config-draft").json()["draft"] is None


def test_backtest_config_draft_rejects_invalid_payload(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    unknown_config = client.put(
        "/api/backtest/config-draft",
        json={"config": {"not_a_strategy_field": 1}},
    )
    assert unknown_config.status_code == 400

    unknown_screener = client.put(
        "/api/backtest/config-draft",
        json={"screener": {"boards": ["主板"]}},
    )
    assert unknown_screener.status_code == 400

    bad_range = client.put(
        "/api/backtest/config-draft",
        json={"start_date": "2025-02-01", "end_date": "2025-01-01"},
    )
    assert bad_range.status_code == 400

    bad_cash = client.put("/api/backtest/config-draft", json={"initial_cash": 0})
    assert bad_cash.status_code == 422
