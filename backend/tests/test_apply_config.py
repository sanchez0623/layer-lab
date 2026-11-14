"""回测配置一键应用到模拟盘账户（POST /paper/accounts/{id}/apply-config）的回归测试。

覆盖：策略字段 + 费率整套应用、费率万分之→小数换算、未指定字段保留、空请求 400、
非法字段 400。数据结构与回测提交一致（config 已 strip 费率 + fees 万分之）。
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    return TestClient(create_app())


@pytest.fixture
def account_id(client):
    demo = client.post("/api/datasets/demo").json()
    account = client.post("/api/paper/accounts", json={
        "name": "一键应用", "initial_cash": 4_000_000,
        "dataset_id": demo["intraday"]["id"],
    }).json()
    return account["id"]


def test_apply_config_updates_strategy_and_takes_effect(client, account_id):
    """策略字段（做T净利止盈/截止/滑点/参与度）整套应用后反映到账户 config。"""
    resp = client.post(f"/api/paper/accounts/{account_id}/apply-config", json={
        "config": {
            "enable_t": True,
            "t_target_net_spread": 0.04,
            "t_exit_cutoff": "14:45",
            "slippage_bps": 2.0,
            "volume_participation": 0.3,
        },
    })
    assert resp.status_code == 200, resp.text
    config = resp.json()["config"]
    assert config["t_target_net_spread"] == 0.04
    assert config["t_exit_cutoff"] == "14:45"
    assert config["slippage_bps"] == 2.0
    assert config["volume_participation"] == 0.3
    assert config["enable_t"] is True

    # 持久化往返后仍是新值
    reloaded = client.get(f"/api/paper/accounts/{account_id}").json()["config"]
    assert reloaded["t_target_net_spread"] == 0.04
    assert reloaded["t_exit_cutoff"] == "14:45"


def test_apply_config_converts_fee_units_wan_to_decimal(client, account_id):
    """费率按万分之提交，应用后转成小数比例（佣金 1 万分之 → 0.0001）。"""
    resp = client.post(f"/api/paper/accounts/{account_id}/apply-config", json={
        "config": {"enable_t": True},
        "fees": {
            "commission_rate": 1.0,      # 万分之
            "stamp_tax_rate": 5.0,       # 万分之
            "min_commission": 8,         # 元，保持
        },
    })
    assert resp.status_code == 200, resp.text
    config = resp.json()["config"]
    assert config["commission_rate"] == pytest.approx(0.0001)
    assert config["stamp_tax_rate"] == pytest.approx(0.0005)
    assert config["min_commission"] == 8


def test_apply_config_preserves_unspecified_fields(client, account_id):
    """未指定的字段保留账户原值（只覆盖提交的字段），如序列金额层级不变。"""
    before = client.get(f"/api/paper/accounts/{account_id}").json()["config"]
    resp = client.post(f"/api/paper/accounts/{account_id}/apply-config", json={
        "config": {"t_target_net_spread": 0.05},
    })
    assert resp.status_code == 200, resp.text
    after = resp.json()["config"]
    assert after["t_target_net_spread"] == 0.05
    # 未提交的字段沿用原值
    assert after["installment_amounts"] == before["installment_amounts"]


def test_apply_config_empty_rejected(client, account_id):
    """空 config 且空 fees → 400（没有要应用的内容）。"""
    assert client.post(f"/api/paper/accounts/{account_id}/apply-config",
                       json={"config": {}}).status_code == 400
    assert client.post(f"/api/paper/accounts/{account_id}/apply-config",
                       json={"config": {}, "fees": {}}).status_code == 400


def test_apply_config_invalid_field_rejected(client, account_id):
    """非法字段值（如净利止盈越界、截止时间格式错）→ from_dict 抛错 → 400。"""
    assert client.post(f"/api/paper/accounts/{account_id}/apply-config", json={
        "config": {"t_target_net_spread": 1.5},
    }).status_code == 400
    assert client.post(f"/api/paper/accounts/{account_id}/apply-config", json={
        "config": {"t_exit_cutoff": "25:00"},
    }).status_code == 400
    # 未知字段 → StrategyConfig.from_dict 抛「未知策略配置」→ 400
    assert client.post(f"/api/paper/accounts/{account_id}/apply-config", json={
        "config": {"nonexistent_field": 1},
    }).status_code == 400
