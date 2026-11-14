"""后台分页助手 + 换票轨迹分页端点测试。

覆盖：paginate 切片 / 越界收敛 / 空集合，以及
``GET /api/backtests/{id}/rotation-trail`` 的分页、跳转、页大小、404。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.pagination import MAX_PAGE_SIZE, paginate


# ---------------------------------------------------------------- paginate --

def test_paginate_basic_slices():
    items = list(range(45))
    page = paginate(items, page=2, page_size=20)
    assert page["items"] == list(range(20, 40))
    assert page["total"] == 45
    assert page["page"] == 2
    assert page["page_size"] == 20
    assert page["total_pages"] == 3
    assert page["has_prev"] is True
    assert page["has_next"] is True


def test_paginate_last_partial_page():
    page = paginate(list(range(45)), page=3, page_size=20)
    assert page["items"] == [40, 41, 42, 43, 44]
    assert page["has_next"] is False
    assert page["has_prev"] is True


def test_paginate_out_of_range_falls_to_last_page():
    page = paginate(list(range(45)), page=99, page_size=20)
    assert page["page"] == 3
    assert page["items"] == [40, 41, 42, 43, 44]
    assert page["has_next"] is False


def test_paginate_page_below_one_clamped():
    page = paginate(list(range(45)), page=0, page_size=20)
    assert page["page"] == 1
    assert page["has_prev"] is False


def test_paginate_page_size_capped_and_floor():
    assert paginate(list(range(10)), page=1, page_size=10_000)["page_size"] == MAX_PAGE_SIZE
    assert paginate(list(range(10)), page=1, page_size=0)["page_size"] == 1


def test_paginate_empty_collection():
    page = paginate([], page=1, page_size=20)
    assert page == {
        "items": [], "total": 0, "page": 1, "page_size": 20,
        "total_pages": 0, "has_prev": False, "has_next": False,
    }


# ------------------------------------------------------------------ 端点 ----

def _report_with_trail(report_id: str, n: int) -> dict:
    """造一份漏斗回测报告，data_info.rotation_trail 含 n 条。"""
    trail = [
        {
            "round": f"r{i}", "symbol": f"sz.{300000 + i}", "name": f"标的{i}",
            "board": "创业板", "select_date": "2026-01-05",
            "entry_date": "2026-01-06", "exit_date": "2026-02-06",
            "pnl": float(i), "chg20d_at_select": -0.2, "gate_open": True,
            "status": "CLOSED",
        }
        for i in range(n)
    ]
    return {
        "id": report_id, "name": "换票轨迹分页测试", "status": "COMPLETED",
        "created_at": "2026-11-13T00:00:00", "config": {}, "metrics": {},
        "equity_curve": [], "trades": [], "orders": [], "rounds": [],
        "events": [], "warnings": [],
        "data_info": {"mode": "funnel_rotation", "screening_count": n, "rotation_trail": trail},
    }


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    application = create_app()
    return application, TestClient(application)


def test_rotation_trail_paginates_and_slices(api):
    application, client = api
    application.state.storage.save_backtest(_report_with_trail("bt_1", 45))

    first = client.get("/api/backtests/bt_1/rotation-trail?page=1&page_size=20")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == 45
    assert body["page"] == 1
    assert body["total_pages"] == 3
    assert len(body["items"]) == 20
    assert body["items"][0]["round"] == "r0"
    assert body["has_prev"] is False and body["has_next"] is True

    third = client.get("/api/backtests/bt_1/rotation-trail?page=3&page_size=20").json()
    assert len(third["items"]) == 5
    assert third["items"][-1]["round"] == "r44"
    assert third["has_next"] is False


def test_rotation_trail_jump_and_page_size(api):
    application, client = api
    application.state.storage.save_backtest(_report_with_trail("bt_2", 50))

    jumped = client.get("/api/backtests/bt_2/rotation-trail?page=4&page_size=10").json()
    assert jumped["page"] == 4
    assert jumped["total_pages"] == 5
    assert jumped["items"][0]["round"] == "r30"

    # 跳转越界收敛到最后一页
    over = client.get("/api/backtests/bt_2/rotation-trail?page=999&page_size=10").json()
    assert over["page"] == 5
    assert over["has_next"] is False

    # 页大小超过上限被 Query(le=MAX_PAGE_SIZE) 拒绝（422），不是静默截断
    assert client.get("/api/backtests/bt_2/rotation-trail?page=1&page_size=99999").status_code == 422

    # 非法输入（page<1 / page_size<1）422
    assert client.get("/api/backtests/bt_2/rotation-trail?page=0").status_code == 422
    assert client.get("/api/backtests/bt_2/rotation-trail?page_size=0").status_code == 422


def test_rotation_trail_404_when_missing(api):
    _application, client = api
    assert client.get("/api/backtests/nope/rotation-trail").status_code == 404


def test_main_report_is_slimmed_no_full_trail(api):
    """主报告读取不再携带全量 trail，只留 rotation_trail_total。"""
    application, client = api
    application.state.storage.save_backtest(_report_with_trail("bt_3", 45))

    body = client.get("/api/backtests/bt_3").json()
    assert body["data_info"]["rotation_trail"] == []
    assert body["data_info"]["rotation_trail_total"] == 45
    # 分页端点仍能读到全量（落库未瘦身）
    paged = client.get("/api/backtests/bt_3/rotation-trail?page=1&page_size=45").json()
    assert paged["total"] == 45
    assert len(paged["items"]) == 45
