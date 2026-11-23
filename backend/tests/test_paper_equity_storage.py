# -*- coding: utf-8 -*-
"""模拟盘逐日净值落库（paper_equity）：存储层 + 写入钩子 + /equity 端点。

背景：模拟盘以前只存「当前状态 JSON」，max_drawdown/sharpe/volatility/calmar/
underwater/cashflow_adjusted/monthly/exposure*/idle_ratio 全部依赖逐日净值序列，
且历史不可回补。本文件守住四件事：

1. storage 层：建表 / 写入 / 读取 roundtrip、同日多 ts 取最后一条、source 隔离、
   INSERT OR REPLACE 幂等；
2. 写入钩子接线：live-tick 日终、手工 /ticks 日终、/advance 数据集回放（统一写
   live 序列，与 live-tick 一致；source=replay 仅保留给历史行的读取端）；
3. /equity 端点契约：字段与回测 equity_curve 对齐、coverage、404、422；
4. 旧 DB（没有 paper_equity 表）启动自动建表，不影响既有账户读取。
"""

import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.data import dataset_from_bars, write_bars_csv
from app.domain import Bar
from app.main import create_app
from app.storage import SQLiteStorage

EQUITY_FIELDS = {
    "date", "ts", "equity", "cash", "market_value", "beta_value",
    "invested", "position_qty", "beta_qty",
}


def _row(day: str, ts: str, equity: float = 1_000_000.0, **overrides) -> dict:
    row = {
        "date": day, "ts": ts, "equity": equity, "cash": equity,
        "market_value": 0.0, "beta_value": 0.0, "invested": 0.0,
        "position_qty": 0, "beta_qty": 0,
    }
    row.update(overrides)
    return row


# -- storage 层 --------------------------------------------------------------


def test_paper_equity_roundtrip_and_daily_keeps_last_row(tmp_path):
    """写入/读取 roundtrip；同一天多 ts 时 list_paper_equity_daily 只取最后一条。"""
    storage = SQLiteStorage(tmp_path / "equity.db")
    storage.save_paper_equity("acc-1", _row(
        "2026-11-10", "2026-11-10T10:00:00", equity=1_000_100.0,
        cash=500_000.0, market_value=500_100.0, invested=500_000.0,
        position_qty=1_000, beta_value=0.0, beta_qty=0,
    ))
    storage.save_paper_equity("acc-1", _row(
        "2026-11-10", "2026-11-10T15:00:00", equity=1_000_200.0,
        cash=500_000.0, market_value=500_200.0, invested=500_000.0,
        position_qty=1_000, beta_value=0.0, beta_qty=0,
    ))
    storage.save_paper_equity("acc-1", _row(
        "2026-11-11", "2026-11-11T15:00:00", equity=1_000_300.0,
        cash=1_000_300.0,
    ))

    rows = storage.list_paper_equity("acc-1")
    assert [row["ts"] for row in rows] == [
        "2026-11-10T10:00:00", "2026-11-10T15:00:00", "2026-11-11T15:00:00",
    ]
    assert set(rows[0]) == EQUITY_FIELDS

    daily = storage.list_paper_equity_daily("acc-1")
    assert [row["date"] for row in daily] == ["2026-11-10", "2026-11-11"]
    # 同日取 ts 更晚那条（10:00 的 1_000_100 被 15:00 的 1_000_200 覆盖）。
    assert daily[0]["ts"] == "2026-11-10T15:00:00"
    assert daily[0]["equity"] == pytest.approx(1_000_200.0)
    assert daily[0]["position_qty"] == 1_000
    assert daily[1]["cash"] == pytest.approx(1_000_300.0)


def test_paper_equity_source_isolation_and_coverage(tmp_path):
    """live 与 replay 隔离：默认只读 live，绝不把缓存回放混进真实净值序列。"""
    storage = SQLiteStorage(tmp_path / "equity.db")
    storage.save_paper_equity("acc-1", _row("2026-11-10", "2026-11-10T15:00:00"), source="live")
    storage.save_paper_equity("acc-1", _row("2026-11-11", "2026-11-11T15:00:00"), source="live")
    storage.save_paper_equity("acc-1", _row("2026-10-01", "2026-10-01T15:00:00"), source="replay")
    storage.save_paper_equity("acc-1", _row("2026-10-02", "2026-10-02T15:00:00"), source="replay")

    live_dates = [row["date"] for row in storage.list_paper_equity_daily("acc-1")]
    replay_dates = [
        row["date"] for row in storage.list_paper_equity_daily("acc-1", source="replay")
    ]
    assert live_dates == ["2026-11-10", "2026-11-11"]
    assert replay_dates == ["2026-10-01", "2026-10-02"]

    assert storage.paper_equity_coverage("acc-1") == {
        "start": "2026-11-10", "end": "2026-11-11", "count": 2,
    }
    assert storage.paper_equity_coverage("acc-1", source="replay") == {
        "start": "2026-10-01", "end": "2026-10-02", "count": 2,
    }
    assert storage.paper_equity_coverage("acc-missing") == {
        "start": None, "end": None, "count": 0,
    }

    # 日期区间过滤（闭区间），且不影响完整 coverage。
    filtered = storage.list_paper_equity_daily("acc-1", start="2026-11-11", end="2026-11-11")
    assert [row["date"] for row in filtered] == ["2026-11-11"]


def test_paper_equity_insert_or_replace_is_idempotent(tmp_path):
    """重复写同一 (account_id,date,ts) 不产生重复行，后者覆盖前者。"""
    storage = SQLiteStorage(tmp_path / "equity.db")
    storage.save_paper_equity("acc-1", _row("2026-11-10", "2026-11-10T15:00:00", equity=1.0))
    storage.save_paper_equity("acc-1", _row("2026-11-10", "2026-11-10T15:00:00", equity=2.0))

    rows = storage.list_paper_equity("acc-1")
    assert len(rows) == 1
    assert rows[0]["equity"] == pytest.approx(2.0)


def test_paper_equity_rejects_unknown_source_and_missing_keys(tmp_path):
    storage = SQLiteStorage(tmp_path / "equity.db")
    with pytest.raises(ValueError, match="未知的净值来源"):
        storage.save_paper_equity("acc-1", _row("2026-11-10", "ts"), source="bogus")
    with pytest.raises(ValueError, match="未知的净值来源"):
        storage.list_paper_equity("acc-1", source="bogus")
    with pytest.raises(ValueError, match="date 与 ts"):
        storage.save_paper_equity("acc-1", {"equity": 1.0})


def test_delete_paper_equity_removes_all_sources_of_one_account(tmp_path):
    """删除按账户隔离，live/replay 全清，返回删除行数。"""
    storage = SQLiteStorage(tmp_path / "equity.db")
    storage.save_paper_equity("acc-1", _row("2026-11-10", "2026-11-10T15:00:00"))
    storage.save_paper_equity("acc-1", _row("2026-10-01", "2026-10-01T15:00:00"), source="replay")
    storage.save_paper_equity("acc-2", _row("2026-11-10", "2026-11-10T15:00:00"))

    assert storage.delete_paper_equity("acc-1") == 2
    assert storage.list_paper_equity("acc-1") == []
    assert storage.list_paper_equity("acc-1", source="replay") == []
    assert len(storage.list_paper_equity("acc-2")) == 1
    assert storage.delete_paper_equity("acc-1") == 0


def test_legacy_db_without_paper_equity_table_gets_it_on_startup(tmp_path):
    """旧 DB 没有该表时启动自动建表，且既有 paper_accounts 记录仍可读。"""
    db_path = tmp_path / "legacy.db"
    payload = json.dumps({"id": "legacy-1", "initial_cash": 100_000.0, "name": "旧账户"})
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE paper_accounts (id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO paper_accounts(id,updated_at,payload) VALUES(?,?,?)",
        ("legacy-1", "2026-01-01T00:00:00", payload),
    )
    connection.commit()
    connection.close()

    storage = SQLiteStorage(db_path)  # 启动即 _initialize

    assert storage.get_paper("legacy-1")["name"] == "旧账户"
    assert storage.latest_paper()["id"] == "legacy-1"
    # 新表可用（不存在则建表）。
    storage.save_paper_equity("legacy-1", _row("2026-11-10", "2026-11-10T15:00:00"))
    assert len(storage.list_paper_equity_daily("legacy-1")) == 1


# -- 端点契约 ----------------------------------------------------------------


@pytest.fixture
def application(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    return create_app()


@pytest.fixture
def client(application):
    return TestClient(application)


def _create_account(client, **overrides) -> dict:
    body = {"name": "净值落库", "initial_cash": 4_000_000, **overrides}
    response = client.post("/api/paper/accounts", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_equity_endpoint_contract_fields_coverage_source_and_errors(application, client):
    account = _create_account(client)
    account_id = account["id"]
    storage = application.state.storage
    storage.save_paper_equity(account_id, _row(
        "2026-11-10", "2026-11-10T15:00:00", equity=4_000_100.0, cash=4_000_100.0,
    ))
    storage.save_paper_equity(account_id, _row(
        "2026-11-11", "2026-11-11T15:00:00", equity=4_000_200.0, cash=4_000_200.0,
    ))
    storage.save_paper_equity(account_id, _row(
        "2026-10-01", "2026-10-01T15:00:00", equity=3_900_000.0,
    ), source="replay")

    response = client.get(f"/api/paper/accounts/{account_id}/equity")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "live"
    assert body["count"] == 2
    assert [item["date"] for item in body["items"]] == ["2026-11-10", "2026-11-11"]
    assert set(body["items"][0]) == EQUITY_FIELDS
    assert body["coverage"] == {"start": "2026-11-10", "end": "2026-11-11", "count": 2}

    # 区间过滤只影响 items，coverage 仍报完整覆盖区间（前端据此标注「自某日起」）。
    ranged = client.get(
        f"/api/paper/accounts/{account_id}/equity",
        params={"start": "2026-11-11", "end": "2026-11-11"},
    ).json()
    assert ranged["count"] == 1
    assert ranged["coverage"] == body["coverage"]

    # source=replay 只返回回放行。
    replay = client.get(
        f"/api/paper/accounts/{account_id}/equity", params={"source": "replay"},
    ).json()
    assert replay["source"] == "replay"
    assert replay["count"] == 1
    assert replay["items"][0]["date"] == "2026-10-01"

    # 空账户：200 + 空列表 + 全 None 覆盖区间（不是 404）。
    empty_account = _create_account(client, name="空账户")
    empty = client.get(f"/api/paper/accounts/{empty_account['id']}/equity")
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert empty.json()["coverage"] == {"start": None, "end": None, "count": 0}

    assert client.get("/api/paper/accounts/nope/equity").status_code == 404
    assert client.get(
        f"/api/paper/accounts/{account_id}/equity", params={"source": "bogus"},
    ).status_code == 422
    assert client.get(
        f"/api/paper/accounts/{account_id}/equity",
        params={"start": "2026-11-12", "end": "2026-11-10"},
    ).status_code == 422
    assert client.get(
        f"/api/paper/accounts/{account_id}/equity", params={"start": "2026/11/10"},
    ).status_code == 422


# -- reset 清空旧净值 --------------------------------------------------------


def test_reset_clears_equity_snapshots_of_that_account_only(application, client):
    """reset 语义是「重开账户」：清空该账户 live+replay 快照，其它账户行不受影响。"""
    storage = application.state.storage
    target_id = _create_account(client, name="待重置")["id"]
    other_id = _create_account(client, name="其它账户")["id"]
    for day, ts in (
        ("2026-11-10", "2026-11-10T15:00:00"),
        ("2026-11-11", "2026-11-11T15:00:00"),
    ):
        storage.save_paper_equity(target_id, _row(day, ts))
        storage.save_paper_equity(other_id, _row(day, ts))
    storage.save_paper_equity(
        target_id, _row("2026-10-01", "2026-10-01T15:00:00"), source="replay",
    )

    response = client.post(f"/api/paper/accounts/{target_id}/reset")
    assert response.status_code == 200, response.text

    assert storage.list_paper_equity(target_id) == []
    assert storage.list_paper_equity(target_id, source="replay") == []
    body = client.get(f"/api/paper/accounts/{target_id}/equity").json()
    assert body["items"] == []
    assert body["coverage"] == {"start": None, "end": None, "count": 0}
    # 删除按账户隔离，别的账户历史净值不受影响。
    assert len(storage.list_paper_equity(other_id)) == 2


# -- 写入钩子接线 ------------------------------------------------------------


def _snapshot(timestamp: datetime, symbol: str = "sh.688200", close: float = 10.0) -> Bar:
    return Bar(
        timestamp=timestamp, symbol=symbol,
        open=close, high=close, low=close, close=close,
        volume=1_000_000.0, amount=close * 1_000_000.0, timeframe="5m",
    )


def _daily_bars(symbol: str, closes: list[float]) -> list[Bar]:
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


def test_live_tick_writes_live_snapshot_only_at_day_close(application, client, monkeypatch):
    account = _create_account(client, name="live日终", auto_strategy=False)
    account_id = account["id"]
    market = application.state.market_service

    # 盘中快照（10:40）不写日终快照。
    intraday = _snapshot(datetime(2026, 11, 12, 10, 40))
    monkeypatch.setattr(market, "fetch_quote", lambda symbol: ({}, intraday))
    assert client.post(
        f"/api/paper/accounts/{account_id}/live-tick", json={"symbol": "688200"},
    ).status_code == 200
    assert client.get(f"/api/paper/accounts/{account_id}/equity").json()["items"] == []

    # 收盘快照（15:00）写一条 live 快照，字段与回测 equity_curve 同口径。
    close_bar = _snapshot(datetime(2026, 11, 12, 15, 0), close=10.5)
    monkeypatch.setattr(market, "fetch_quote", lambda symbol: ({}, close_bar))
    response = client.post(
        f"/api/paper/accounts/{account_id}/live-tick", json={"symbol": "688200"},
    )
    assert response.status_code == 200, response.text
    body = client.get(f"/api/paper/accounts/{account_id}/equity").json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["date"] == "2026-11-12"
    assert item["ts"] == "2026-11-12T15:00:00"
    assert item["equity"] == pytest.approx(4_000_000.0)
    assert item["cash"] == pytest.approx(4_000_000.0)
    assert item["market_value"] == pytest.approx(0.0)
    assert item["position_qty"] == 0
    assert item["beta_qty"] == 0


def test_beta_bar_day_close_also_writes_snapshot(application, client, monkeypatch):
    """ETF beta 腿日终同样落一条（与个股日终同口径，读取取当日最后一条）。"""
    account = _create_account(client, name="beta日终", auto_strategy=False)
    account_id = account["id"]
    etf_bar = _snapshot(datetime(2026, 11, 12, 15, 0), symbol="sz.159915", close=1.234)
    monkeypatch.setattr(
        application.state.market_service, "fetch_quote", lambda symbol: ({}, etf_bar)
    )
    assert client.post(
        f"/api/paper/accounts/{account_id}/live-tick", json={"symbol": "sz.159915"},
    ).status_code == 200

    body = client.get(f"/api/paper/accounts/{account_id}/equity").json()
    assert body["count"] == 1
    assert body["items"][0]["date"] == "2026-11-12"


def test_manual_tick_writes_snapshot_only_at_day_close(client):
    """POST /ticks 与 live-tick 同一日终口径：盘中 tick 不写，15:00 后写一条。"""
    account = _create_account(client, name="手工tick", auto_strategy=False)
    account_id = account["id"]
    payload = {
        "symbol": "688200", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
        "volume": 1_000.0,
    }
    intraday = client.post(
        f"/api/paper/accounts/{account_id}/ticks",
        json={**payload, "timestamp": "2026-11-12T10:00:00"},
    )
    assert intraday.status_code == 200, intraday.text
    assert client.get(f"/api/paper/accounts/{account_id}/equity").json()["count"] == 0

    close = client.post(
        f"/api/paper/accounts/{account_id}/ticks",
        json={**payload, "timestamp": "2026-11-12T15:00:00"},
    )
    assert close.status_code == 200, close.text
    body = client.get(f"/api/paper/accounts/{account_id}/equity").json()
    assert body["count"] == 1
    assert body["items"][0]["ts"] == "2026-11-12T15:00:00"


def test_advance_writes_live_source_and_is_idempotent(application, client, tmp_path):
    """/advance 是真实的账户状态推进：日终净值随状态落 live 序列，与 live-tick 一致。

    回归保护：修复前 /advance 写 source=replay（不进 live），导致「选股带入」这类
    只靠 advance 驱动的账户 live 净值序列永远为空，pnl/持有交易日全废。同时验证
    重复 advance 同一段（PK account_id+date+ts）幂等覆盖，不产生重复行。
    """
    bars = _daily_bars("sh.688200", [10.0, 11.0])
    path = tmp_path / "cache" / "688200_daily.csv"
    write_bars_csv(path, bars)
    dataset = dataset_from_bars(
        bars, "688200 测试日线", "sh.688200", "daily", "baostock", False, str(path),
    )
    application.state.storage.save_dataset(dataset)
    account = _create_account(
        client, name="回放推进", auto_strategy=False, dataset_id=dataset.id,
    )
    account_id = account["id"]

    advanced = client.post(
        f"/api/paper/accounts/{account_id}/advance", json={"bars": 2},
    )
    assert advanced.status_code == 200, advanced.text

    live = client.get(f"/api/paper/accounts/{account_id}/equity").json()
    assert live["count"] == 2, "advance 日终净值应落 live 序列"
    assert [item["date"] for item in live["items"]] == ["2026-10-01", "2026-10-02"]
    assert live["coverage"] == {
        "start": "2026-10-01", "end": "2026-10-02", "count": 2,
    }

    # 重复 advance 同一段：幂等覆盖，live 行数不增。
    repeat = client.post(
        f"/api/paper/accounts/{account_id}/advance", json={"bars": 2},
    )
    assert repeat.status_code == 200, repeat.text
    again = client.get(f"/api/paper/accounts/{account_id}/equity").json()
    assert again["count"] == 2, "重复 advance 不应产生重复 live 快照"
