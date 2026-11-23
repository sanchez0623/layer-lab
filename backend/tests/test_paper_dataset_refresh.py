# -*- coding: utf-8 -*-
"""live-tick 刷新后账户数据集指针持久化（bugfix：account.dataset_id 永不前进）。

bug（修复前）：live-tick 刷新块检测到绑定日线数据集落后当日时，会 ``fetch_history_bundle``
拉新数据并 ``save_dataset`` 注册成**新**数据集，但只更新局部变量 ``daily_dataset``，
**从不回写 ``account.dataset_id``**。后果：
- 账户指针永远停在首次带入的旧 CSV，每天重复拉取 + 堆积孤儿数据集；
- 容器重启后第一次 tick 仍用旧日线算 ``previous_three_low`` / ``previous_five_volume``
  等历史量价指标，加仓判定被污染。

修复后：刷新拿到新 daily 数据集且 id 变化时，把 ``account.dataset_id`` 挪过去并
``save_paper`` 落库。本文件守住这一行为。
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app.market as market_module
from app.data import dataset_from_bars, write_bars_csv
from app.domain import Bar
from app.main import create_app
from app.strategy import LayeredTStrategy

SYMBOL = "301205"
STANDARD = "sz.301205"
INDEX = "sz.399006"


def _daily_bars(symbol: str, end: datetime, count: int) -> list[Bar]:
    bars: list[Bar] = []
    previous: float | None = None
    close = 100.0
    for offset in range(count - 1, -1, -1):
        ts = end - timedelta(days=offset)
        bars.append(Bar(
            timestamp=ts, symbol=symbol, open=close, high=close, low=close, close=close,
            volume=5_000_000.0, amount=close * 5_000_000.0,
            timeframe="daily", prev_close=previous,
        ))
        previous = close
    return bars


def _seed_dataset(application, tmp_path, symbol: str, end: datetime, tag: str) -> object:
    bars = _daily_bars(symbol, end, 10)
    path = tmp_path / "cache" / f"{symbol.replace('.', '_')}_{tag}_daily.csv"
    write_bars_csv(path, bars)
    dataset = dataset_from_bars(
        bars, f"{symbol} {tag}日线", symbol, "daily", "baostock", False, str(path),
    )
    application.state.storage.save_dataset(dataset)
    return dataset


def test_live_tick_refresh_advances_account_dataset_id(tmp_path, monkeypatch):
    """日线数据集落后当日 → 刷新后 account.dataset_id 指向新数据集并落库。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    application = create_app()
    client = TestClient(application)

    # bar 日期（今天）：绑定数据集 end 停在更早，触发刷新条件 daily_dataset.end < bar.date。
    bar_date = datetime(2026, 11, 20)
    old_daily = _seed_dataset(application, tmp_path, STANDARD, datetime(2026, 11, 12), "old")
    _seed_dataset(application, tmp_path, INDEX, datetime(2026, 11, 12), "old")

    account = client.post("/api/paper/accounts", json={
        "name": "数据集指针", "initial_cash": 4_000_000, "auto_strategy": True,
        "dataset_id": old_daily.id,
    }).json()
    account_id = account["id"]
    assert account["dataset_id"] == old_daily.id

    # stub：刷新拉到一份 end 更晚的新日线数据集（id 必然不同于旧的）。
    new_daily = _seed_dataset(application, tmp_path, STANDARD, bar_date, "new")
    assert new_daily.id != old_daily.id

    def fake_fetch_history_bundle(raw_symbol, *args, **kwargs):
        return {"daily": new_daily, "index": None, "intraday": None}

    monkeypatch.setattr(
        market_module.MarketDataService, "fetch_history_bundle", fake_fetch_history_bundle,
    )
    # 屏蔽策略信号与盘中拉取（本测试只验证数据集指针前进，不让策略/网络干扰）。
    monkeypatch.setattr(LayeredTStrategy, "on_intraday", lambda self, *a, **k: [])
    monkeypatch.setattr(LayeredTStrategy, "on_daily", lambda self, *a, **k: [])
    monkeypatch.setattr(
        market_module.MarketDataService, "fetch_tencent_intraday",
        lambda self, *a, **k: ([], None),
    )

    response = client.post(
        f"/api/paper/accounts/{account_id}/live-tick", json={"symbol": SYMBOL},
    )
    assert response.status_code == 200, response.text

    # 指针前进到新数据集。
    body = client.get(f"/api/paper/accounts/{account_id}").json()
    assert body["dataset_id"] == new_daily.id, "刷新后账户数据集指针应前进到新数据集"

    # 落库验证：重读存储里的账户，dataset_id 已持久化（不是只在内存）。
    persisted = application.state.storage.get_paper(account_id)
    assert persisted["dataset_id"] == new_daily.id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])