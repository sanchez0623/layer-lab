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


# ------------------------------------------------- 成交/委托/轮次 明细分页 ----

def _report_with_records(report_id: str, n: int) -> dict:
    """造一份单标的回测报告，trades / orders / rounds / events 各含 n 条。"""
    trades = [{"id": f"t{i}", "symbol": "sz.000001", "side": "buy",
               "timestamp": f"2026-01-{(i % 28) + 1:02d}", "price": 10.0,
               "quantity": 100, "amount": 1000.0, "total_fee": 1.0} for i in range(n)]
    orders = [{"id": f"o{i}", "symbol": "sz.000001", "side": "buy",
               "created_at": f"2026-01-{(i % 28) + 1:02d}", "order_type": "market",
               "quantity": 100, "filled_quantity": 100, "status": "filled"} for i in range(n)]
    rounds = [{"id": f"r{i}", "start_date": "2026-01-01", "end_date": "2026-02-01",
               "net_profit": float(i), "status": "closed"} for i in range(n)]
    events = [{"id": f"e{i}", "date": "2026-01-02", "type": "buy", "message": "m"} for i in range(n)]
    return {
        "id": report_id, "name": "明细分页测试", "status": "COMPLETED",
        "created_at": "2026-11-13T00:00:00", "config": {}, "metrics": {},
        "equity_curve": [], "trades": trades, "orders": orders, "rounds": rounds,
        "events": events, "warnings": [], "data_info": {"mode": "single"},
    }


def test_records_paginate_slices_all_kinds(api):
    application, client = api
    application.state.storage.save_backtest(_report_with_records("bt_10", 45))

    for kind in ("trades", "orders", "rounds", "events"):
        first = client.get(f"/api/backtests/bt_10/records/{kind}?page=1&page_size=20")
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["total"] == 45, kind
        assert body["total_pages"] == 3
        assert len(body["items"]) == 20
        assert body["items"][0]["id"].startswith(kind[0])  # t/o/r/e
        assert body["has_next"] is True

        last = client.get(f"/api/backtests/bt_10/records/{kind}?page=3&page_size=20").json()
        assert len(last["items"]) == 5
        assert last["has_next"] is False


def test_records_jump_out_of_range_and_validation(api):
    application, client = api
    application.state.storage.save_backtest(_report_with_records("bt_11", 50))

    jumped = client.get("/api/backtests/bt_11/records/trades?page=4&page_size=10").json()
    assert jumped["page"] == 4 and jumped["items"][0]["id"] == "t30"

    over = client.get("/api/backtests/bt_11/records/orders?page=999&page_size=10").json()
    assert over["page"] == 5 and over["has_next"] is False

    # 页大小越界 / 非法页码 → 422
    assert client.get("/api/backtests/bt_11/records/rounds?page=1&page_size=99999").status_code == 422
    assert client.get("/api/backtests/bt_11/records/rounds?page=0").status_code == 422

    # 未知 kind → 422（Literal 校验）
    assert client.get("/api/backtests/bt_11/records/bogus?page=1").status_code == 422


def test_records_404_when_backtest_missing(api):
    _application, client = api
    assert client.get("/api/backtests/nope/records/trades").status_code == 404


def test_main_report_is_slimmed_no_full_records(api):
    """主报告读取只留 trades_total / orders_total / rounds_total，明细数组清空。"""
    application, client = api
    application.state.storage.save_backtest(_report_with_records("bt_12", 45))

    body = client.get("/api/backtests/bt_12").json()
    assert body["trades"] == [] and body["trades_total"] == 45
    assert body["orders"] == [] and body["orders_total"] == 45
    assert body["rounds"] == [] and body["rounds_total"] == 45
    assert body["events"] == [] and body["events_total"] == 45
    # 分页端点仍能读到全量（落库未瘦身）
    paged = client.get("/api/backtests/bt_12/records/trades?page=1&page_size=45").json()
    assert paged["total"] == 45 and len(paged["items"]) == 45


# --------------------------------------- 成交明细 symbol/purpose/side 过滤 --

def _report_with_mixed_trades(report_id: str) -> dict:
    """造一份多标的、多用途、多方向的回测报告，供搜索过滤测试。"""
    trades = [
        {"id": "t0", "symbol": "sh.688200", "side": "buy", "purpose": "PERMANENT",
         "timestamp": "2026-01-05T09:30:00", "price": 10.0, "quantity": 100, "amount": 1000.0},
        {"id": "t1", "symbol": "sz.000001", "side": "sell", "purpose": "T",
         "timestamp": "2026-01-06T09:30:00", "price": 11.0, "quantity": 100, "amount": 1100.0},
        {"id": "t2", "symbol": "sh.600519", "side": "buy", "purpose": "T",
         "timestamp": "2026-01-07T09:30:00", "price": 12.0, "quantity": 100, "amount": 1200.0},
        {"id": "t3", "symbol": "sz.159915", "side": "buy", "purpose": "BETA",
         "timestamp": "2026-01-08T09:30:00", "price": 13.0, "quantity": 100, "amount": 1300.0},
    ]
    orders = [
        {"id": "o0", "symbol": "sh.688200", "side": "BUY", "purpose": "PERMANENT",
         "created_at": "2026-01-05T09:30:00", "quantity": 100, "status": "filled"},
        {"id": "o1", "symbol": "sz.000001", "side": "SELL", "purpose": "T",
         "created_at": "2026-01-06T09:30:00", "quantity": 100, "status": "filled"},
    ]
    return {
        "id": report_id, "name": "成交明细搜索测试", "status": "COMPLETED",
        "created_at": "2026-11-15T00:00:00", "config": {}, "metrics": {},
        "equity_curve": [], "trades": trades, "orders": orders, "rounds": [],
        "events": [], "warnings": [], "data_info": {"mode": "single"},
    }


def _ids(body: dict) -> list[str]:
    return [item["id"] for item in body["items"]]


def test_records_filter_by_symbol_substring(api):
    """标的子串匹配：6 位代码 / 带前缀代码都能命中，大小写不敏感。"""
    application, client = api
    application.state.storage.save_backtest(_report_with_mixed_trades("bt_20"))

    body = client.get("/api/backtests/bt_20/records/trades?symbol=688200").json()
    assert body["total"] == 1 and _ids(body) == ["t0"]

    body = client.get("/api/backtests/bt_20/records/trades?symbol=SH.600519").json()
    assert body["total"] == 1 and _ids(body) == ["t2"]

    # 无命中
    body = client.get("/api/backtests/bt_20/records/trades?symbol=999999").json()
    assert body["total"] == 0 and body["items"] == []


def test_records_filter_by_purpose_enum_and_label(api):
    """用途过滤：枚举值与其中文标签等价（全等匹配，非子串）。"""
    application, client = api
    application.state.storage.save_backtest(_report_with_mixed_trades("bt_21"))

    # 枚举值，大小写不敏感
    body = client.get("/api/backtests/bt_21/records/trades?purpose=T").json()
    assert body["total"] == 2 and _ids(body) == ["t1", "t2"]
    body = client.get("/api/backtests/bt_21/records/trades?purpose=permanent").json()
    assert body["total"] == 1 and _ids(body) == ["t0"]

    # 中文标签等价于枚举值
    body = client.get("/api/backtests/bt_21/records/trades?purpose=%E6%B0%B8%E4%B9%85%E4%BB%93").json()  # 永久仓
    assert body["total"] == 1 and _ids(body) == ["t0"]
    body = client.get("/api/backtests/bt_21/records/trades?purpose=t%E4%BB%93").json()  # t仓
    assert body["total"] == 2
    body = client.get("/api/backtests/bt_21/records/trades?purpose=ETF%E5%AF%B9%E5%86%B2").json()  # ETF对冲
    assert body["total"] == 1 and _ids(body) == ["t3"]

    # 子串不再生效：'t' 只匹配 T 仓，不会误命中 PERMANENT/BETA
    body = client.get("/api/backtests/bt_21/records/trades?purpose=t").json()
    assert body["total"] == 2 and _ids(body) == ["t1", "t2"]


def test_records_filter_by_side(api):
    """方向过滤：BUY / SELL 大小写不敏感。"""
    application, client = api
    application.state.storage.save_backtest(_report_with_mixed_trades("bt_24"))

    body = client.get("/api/backtests/bt_24/records/trades?side=BUY").json()
    assert body["total"] == 3 and _ids(body) == ["t0", "t2", "t3"]

    body = client.get("/api/backtests/bt_24/records/trades?side=sell").json()
    assert body["total"] == 1 and _ids(body) == ["t1"]


def test_records_filter_combined_and_total_reflects_filter(api):
    """symbol + purpose + side 组合过滤；total / 分页口径基于过滤后条数。"""
    application, client = api
    application.state.storage.save_backtest(_report_with_mixed_trades("bt_22"))

    body = client.get("/api/backtests/bt_22/records/trades?symbol=sz&purpose=T&side=SELL").json()
    assert body["total"] == 1 and _ids(body) == ["t1"]

    # 组合无命中
    body = client.get("/api/backtests/bt_22/records/trades?symbol=688200&purpose=T").json()
    assert body["total"] == 0 and body["total_pages"] == 0

    # 过滤后分页：total_pages 按过滤后条数计算
    body = client.get("/api/backtests/bt_22/records/trades?side=BUY&page=1&page_size=2").json()
    assert body["total"] == 3 and body["total_pages"] == 2 and len(body["items"]) == 2
    body = client.get("/api/backtests/bt_22/records/trades?side=BUY&page=2&page_size=2").json()
    assert _ids(body) == ["t3"]

    # 不带过滤参数时行为不变（回归）
    body = client.get("/api/backtests/bt_22/records/trades?page=1&page_size=2").json()
    assert body["total"] == 4 and len(body["items"]) == 2


def test_records_filter_invalid_params_422(api):
    """非法 purpose / side → 422（含合法值提示），而不是静默返回空。"""
    application, client = api
    application.state.storage.save_backtest(_report_with_mixed_trades("bt_25"))

    bad_purpose = client.get("/api/backtests/bt_25/records/trades?purpose=xx")
    assert bad_purpose.status_code == 422
    assert "purpose" in bad_purpose.json()["detail"]

    bad_side = client.get("/api/backtests/bt_25/records/trades?side=foo")
    assert bad_side.status_code == 422
    assert "side" in bad_side.json()["detail"]


def test_records_filter_blank_params_ignored(api):
    """空串 / 纯空白的过滤参数视同不过滤。"""
    application, client = api
    application.state.storage.save_backtest(_report_with_mixed_trades("bt_23"))

    body = client.get("/api/backtests/bt_23/records/trades?symbol=&purpose=%20%20&side=").json()
    assert body["total"] == 4


def test_records_facets_exclude_own_condition(api):
    """facets：各分面只剔除自身条件，其余条件生效（供下拉实时显示条数）。"""
    application, client = api
    application.state.storage.save_backtest(_report_with_mixed_trades("bt_26"))

    # 无过滤：全量分面计数
    body = client.get("/api/backtests/bt_26/records/trades").json()
    assert body["facets"]["purpose"] == {"PERMANENT": 1, "T": 2, "BETA": 1}
    assert body["facets"]["side"] == {"BUY": 3, "SELL": 1}

    # 方向=SELL：purpose facet 反映 SELL 下的用途分布（T 1），side facet 不受自身条件影响（仍 BUY 3/SELL 1）
    body = client.get("/api/backtests/bt_26/records/trades?side=SELL").json()
    assert body["facets"]["purpose"] == {"T": 1}
    assert body["facets"]["side"] == {"BUY": 3, "SELL": 1}

    # 标的=sz：两个 facet 都受 symbol 约束
    body = client.get("/api/backtests/bt_26/records/trades?symbol=sz").json()
    assert body["facets"]["purpose"] == {"T": 1, "BETA": 1}
    assert body["facets"]["side"] == {"BUY": 1, "SELL": 1}

    # 非 trades kind 不带 facets
    body = client.get("/api/backtests/bt_26/records/orders").json()
    assert "facets" not in body


def test_records_filter_applies_to_orders_kind(api):
    """过滤参数对任意 kind 通用（委托记录同样可按标的/方向过滤）。"""
    application, client = api
    application.state.storage.save_backtest(_report_with_mixed_trades("bt_27"))

    body = client.get("/api/backtests/bt_27/records/orders?symbol=688200&side=BUY").json()
    assert body["total"] == 1 and _ids(body) == ["o0"]

    body = client.get("/api/backtests/bt_27/records/orders?side=SELL").json()
    assert body["total"] == 1 and _ids(body) == ["o1"]
