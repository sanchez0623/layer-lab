"""回测报告管理：列表搜索 / 分页 / 删除级联。

覆盖：
- ``GET /api/backtests`` 分页信封 + 按 Report ID / 名称搜索（大小写、通配符转义）；
- ``DELETE /api/backtests/{id}`` 正常删除、级联清理 job/checkpoint、运行中 409、缺失 404。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def _report(report_id: str, name: str, created_at: str = "2026-11-13T00:00:00") -> dict:
    """构造一份最小可落库报告；列表端点只读摘要列，不依赖 payload 其余字段。"""
    return {
        "id": report_id,
        "name": name,
        "status": "COMPLETED",
        "created_at": created_at,
        "config": {},
        "metrics": {},
        "equity_curve": [],
        "trades": [],
        "orders": [],
        "rounds": [],
        "events": [],
        "warnings": [],
        "data_info": {"mode": "single"},
    }


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    application = create_app()
    return application, TestClient(application)


def _seed(application) -> None:
    storage = application.state.storage
    storage.save_backtest(_report("bt-alpha-001", "创业板轮动 Alpha 实验", "2026-11-13T10:00:00"))
    storage.save_backtest(_report("bt-beta-002", "科创板单标的回测", "2026-11-13T11:00:00"))
    storage.save_backtest(_report("bt-gamma-003", "含 % 与 _ 字面量", "2026-11-13T12:00:00"))


def test_list_backtests_paginated_envelope(api):
    application, client = api
    _seed(application)

    response = client.get("/api/backtests")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["total"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert body["total_pages"] == 1
    assert body["has_prev"] is False and body["has_next"] is False
    assert [item["id"] for item in body["items"]] == ["bt-gamma-003", "bt-beta-002", "bt-alpha-001"]
    # 列表只下发轻量摘要，不解析 / 传输大 payload 与指标。
    assert set(body["items"][0]) == {"id", "name", "status", "created_at"}


def test_search_by_name_and_report_id(api):
    application, client = api
    _seed(application)

    by_name = client.get("/api/backtests?q=alpha").json()
    assert by_name["total"] == 1
    assert by_name["items"][0]["id"] == "bt-alpha-001"

    # Report ID 搜索大小写不敏感；首尾空格被 trim。
    by_id = client.get("/api/backtests?q=%20%20BT-ALPHA%20%20").json()
    assert by_id["total"] == 1
    assert by_id["items"][0]["name"] == "创业板轮动 Alpha 实验"

    by_chinese = client.get("/api/backtests?q=科创板").json()
    assert by_chinese["total"] == 1
    assert by_chinese["items"][0]["id"] == "bt-beta-002"

    none = client.get("/api/backtests?q=不存在的报告").json()
    assert none["total"] == 0 and none["items"] == []
    assert none["total_pages"] == 0

    # 空串 / 纯空格等价于不筛选。
    assert client.get("/api/backtests?q=").json()["total"] == 3
    assert client.get("/api/backtests?q=%20%20%20").json()["total"] == 3


def test_search_escapes_like_wildcards(api):
    application, client = api
    _seed(application)

    # 未转义时 % 会匹配全部 3 份、_ 会匹配任意单字符；转义后只命中字面量。
    percent = client.get("/api/backtests?q=%25").json()
    assert percent["total"] == 1
    assert percent["items"][0]["id"] == "bt-gamma-003"

    underscore = client.get("/api/backtests?q=_").json()
    assert underscore["total"] == 1
    assert underscore["items"][0]["id"] == "bt-gamma-003"


def test_search_with_pagination_and_bounds(api):
    application, client = api
    _seed(application)

    first = client.get("/api/backtests?page=1&page_size=1").json()
    assert first["page"] == 1 and first["total_pages"] == 3
    assert first["items"][0]["id"] == "bt-gamma-003"
    assert first["has_prev"] is False and first["has_next"] is True

    second = client.get("/api/backtests?page=2&page_size=1").json()
    assert second["items"][0]["id"] == "bt-beta-002"

    # 越界页自动收敛到最后一页。
    over = client.get("/api/backtests?page=99&page_size=1").json()
    assert over["page"] == 3 and over["has_next"] is False
    assert over["items"][0]["id"] == "bt-alpha-001"

    # 搜索命中后同样分页。
    searched = client.get("/api/backtests?q=回测&page=1&page_size=1").json()
    assert searched["total"] == 1
    assert searched["items"][0]["id"] == "bt-beta-002"

    # 非法分页参数由 page_params 校验为 422，而不是静默截断。
    assert client.get("/api/backtests?page=0").status_code == 422
    assert client.get("/api/backtests?page_size=0").status_code == 422
    assert client.get("/api/backtests?page_size=99999").status_code == 422


def test_delete_backtest_removes_report(api):
    application, client = api
    storage = application.state.storage
    storage.save_backtest(_report("bt-del", "待删除报告"))

    deleted = client.delete("/api/backtests/bt-del")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True, "id": "bt-del"}
    assert client.get("/api/backtests/bt-del").status_code == 404
    assert client.get("/api/backtests").json()["total"] == 0

    # 重复删除 / 不存在 → 404。
    assert client.delete("/api/backtests/bt-del").status_code == 404
    assert client.delete("/api/backtests/nope").status_code == 404


def test_delete_backtest_cascades_job_and_checkpoint(api):
    application, client = api
    storage = application.state.storage
    job_id = "job-with-report"
    storage.create_job({
        "id": job_id, "name": "级联删除任务", "status": "COMPLETED",
        "progress": 100.0, "params": {},
        "created_at": "2026-11-13T09:00:00", "updated_at": "2026-11-13T10:00:00",
    })
    storage.save_checkpoint(
        job_id, "2026-11-13", 3, {"cash": 1.0}, {"state": 1}, {"cursor_index": 3},
    )
    storage.save_backtest(_report(job_id, "带后台任务的报告"))

    assert client.delete(f"/api/backtests/{job_id}").status_code == 200
    assert storage.get_job(job_id) is None
    assert storage.get_checkpoint(job_id) is None


def test_delete_backtest_rejects_running_job(api):
    application, client = api
    storage = application.state.storage
    job_id = "job-running"
    storage.create_job({
        "id": job_id, "name": "运行中任务", "status": "RUNNING",
        "progress": 30.0, "params": {},
        "created_at": "2026-11-13T09:00:00", "updated_at": "2026-11-13T09:30:00",
    })

    response = client.delete(f"/api/backtests/{job_id}")
    assert response.status_code == 409
    assert storage.get_job(job_id) is not None


# ---------------------------------------------------------------------------
# 重命名：PATCH /api/backtests/{id} 同步 name 列与 payload.name
# ---------------------------------------------------------------------------


def test_rename_backtest_updates_name_and_payload(api):
    application, client = api
    storage = application.state.storage
    storage.save_backtest(_report("bt-ren", "旧名称"))

    response = client.patch("/api/backtests/bt-ren", json={"name": "  新名称  "})
    assert response.status_code == 200, response.text
    assert response.json() == {"id": "bt-ren", "name": "新名称"}

    # 详情 payload 的 name 同步更新。
    detail = client.get("/api/backtests/bt-ren").json()
    assert detail["name"] == "新名称"
    # 列表摘要列（name）同步更新，可按新名称搜到。
    listed = client.get("/api/backtests", params={"q": "新名称"}).json()
    assert listed["total"] == 1
    assert listed["items"][0]["name"] == "新名称"


def test_rename_backtest_missing_id_returns_404(api):
    _application, client = api
    assert client.patch("/api/backtests/nope", json={"name": "任意"}).status_code == 404


def test_rename_backtest_rejects_blank_name(api):
    application, client = api
    storage = application.state.storage
    storage.save_backtest(_report("bt-blank", "原名"))
    assert client.patch("/api/backtests/bt-blank", json={"name": "   "}).status_code == 400
    assert client.patch("/api/backtests/bt-blank", json={"name": ""}).status_code == 400
    # 非法输入不改动原名称。
    assert client.get("/api/backtests/bt-blank").json()["name"] == "原名"


def test_rename_backtest_rejects_overlong_name(api):
    application, client = api
    storage = application.state.storage
    storage.save_backtest(_report("bt-long", "原名"))
    assert client.patch("/api/backtests/bt-long", json={"name": "名" * 201}).status_code == 400
    # 边界内 200 字允许。
    assert client.patch("/api/backtests/bt-long", json={"name": "名" * 200}).status_code == 200


# ---------------------------------------------------------------------------
# 年化收益率「买入持有」三基准（compute_buy_hold_benchmarks 纯函数）
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta  # noqa: E402

from app.backtest import compute_buy_hold_benchmarks  # noqa: E402
from app.domain import Bar  # noqa: E402


def _index_bars(code: str, closes: list[float], base: str = "2025-01-02") -> list[Bar]:
    base_dt = datetime.fromisoformat(base).date()
    return [
        Bar(
            timestamp=datetime.combine(base_dt + timedelta(days=index), datetime.min.time()),
            symbol=code, open=close, high=close, low=close, close=close,
            volume=1.0, amount=1.0, timeframe="daily",
        )
        for index, close in enumerate(closes)
    ]


def test_buy_hold_benchmarks_three_baselines():
    """三基准：沪深300 / 中证500 / 半仓创业板+半仓科创板。"""
    timeline = ["2025-01-02", "2025-01-03", "2025-01-06"]
    bars = {
        "sh.000300": _index_bars("sh.000300", [100.0, 110.0, 121.0]),
        "sh.000905": _index_bars("sh.000905", [200.0, 205.0, 210.0]),
        "sz.399006": _index_bars("sz.399006", [3000.0, 3150.0, 3300.0]),
        "sh.000688": _index_bars("sh.000688", [1000.0, 1050.0, 1100.0]),
    }
    result = compute_buy_hold_benchmarks(bars, timeline)
    assert [item["symbol"] for item in result] == ["sh.000300", "sh.000905", "组合"]
    assert result[0]["return"] == pytest.approx(121 / 100 - 1)
    assert result[1]["return"] == pytest.approx(210 / 200 - 1)
    assert result[2]["label"] == "半仓创业板+半仓科创板"
    assert result[2]["return"] == pytest.approx(
        0.5 * (3300 / 3000 - 1) + 0.5 * (1100 / 1000 - 1)
    )


def test_buy_hold_benchmarks_missing_index_degrades_to_zero():
    """缺失指数数据（如 QuanStore 无该指数）→ 对应基准降级 0.0，不抛错。"""
    timeline = ["2025-01-02", "2025-01-03", "2025-01-06"]
    bars = {
        "sh.000300": _index_bars("sh.000300", [100.0, 110.0, 121.0]),
        # sh.000905 缺失
        "sz.399006": _index_bars("sz.399006", [3000.0, 3300.0]),
        # sh.000688 缺失 → 组合只按创业板半仓算
    }
    result = compute_buy_hold_benchmarks(bars, timeline)
    assert result[0]["return"] == pytest.approx(0.21)
    assert result[1]["return"] == 0.0
    assert result[2]["return"] == pytest.approx(0.5 * (3300 / 3000 - 1))


def test_buy_hold_benchmarks_filters_bars_outside_interval():
    """区间外的缓冲段 bar 不计入基准（避免把 lookback 段算进收益）。"""
    timeline = ["2025-01-03", "2025-01-06"]
    bars = {
        "sh.000300": _index_bars("sh.000300", [50.0, 80.0, 100.0, 121.0], base="2025-01-01"),
    }
    result = compute_buy_hold_benchmarks(bars, timeline)
    assert result[0]["return"] == pytest.approx(121 / 100 - 1)


def test_buy_hold_benchmarks_empty_timeline_and_insufficient_bars():
    assert compute_buy_hold_benchmarks({}, []) == []
    # 区间内不足两根 bar → 0.0 占位
    result = compute_buy_hold_benchmarks(
        {"sh.000300": _index_bars("sh.000300", [100.0])},
        ["2025-01-02"],
    )
    assert result[0]["return"] == 0.0
