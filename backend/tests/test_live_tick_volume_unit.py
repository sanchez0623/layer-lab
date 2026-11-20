# -*- coding: utf-8 -*-
"""live-tick 集成回归：腾讯 m5 量（手）经入口 ×100 转股后，PERMANENT 委托单笔成交。

bug 复现（修复前）：腾讯 5 分钟 bar volume 单位是「手」（单根 1,296 手），market.py 未 ×100
就交给撮合，broker 把「手」当「股」乘参与度 0.2 → 容量只有 200 股 → 一笔 600 股 PERMANENT
买入被拆成 3 笔（200×3，逐笔已在真实账户 64f692bb 复现）。

修复后：``fetch_tencent_intraday`` 在入口把 volume ×100（1,296 手 → 129,600 股），同一笔
600 股委托在第一根可成交 bar **单笔全成**。本测试在 HTTP 层 mock 腾讯响应，走真实的
``fetch_quote`` / ``fetch_tencent_intraday`` 解析（含 ×100），再经 live-tick 推进账户。
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app.market as market_module
from app.data import dataset_from_bars, write_bars_csv
from app.domain import Bar
from app.main import create_app
from app.strategy import LayeredTStrategy


SYMBOL = "301205"                 # 创业板，step=100
STANDARD = "sz.301205"
INDEX = "sz.399006"
CLOSE = 146.05
VOLUME_LOTS = 1296.0              # 单根 m5 腾讯返回的「手」
ORDER_QTY = 600                   # PERMANENT 买入委托（股）


def _snapshot_text() -> str:
    """腾讯实时快照：fields[3]=close、[36]=volume(手)、[37]=amount(万元)。"""
    fields = [""] * 50
    fields[1] = "测试股"
    fields[3] = f"{CLOSE}"
    fields[4] = "145.00"
    fields[30] = "20261113143500"   # 快照时间 14:35
    fields[36] = f"{VOLUME_LOTS}"   # 全天累计量（手）
    fields[37] = "18900"
    fields[38] = "2.5"
    return 'v_sz301205="' + "~".join(fields) + '";'


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.encoding = "gbk"

    def raise_for_status(self) -> None:
        return None


def _daily_bars(symbol: str, end: datetime, count: int) -> list[Bar]:
    bars: list[Bar] = []
    previous: float | None = None
    close = 140.0
    for offset in range(count - 1, -1, -1):
        ts = end - timedelta(days=offset)
        bars.append(Bar(
            timestamp=ts, symbol=symbol, open=close, high=close, low=close, close=close,
            volume=5_000_000.0, amount=close * 5_000_000.0,
            timeframe="daily", prev_close=previous,
        ))
        previous = close
    return bars


def _seed_daily(application, tmp_path, symbol: str, end: datetime) -> None:
    bars = _daily_bars(symbol, end, 30)
    path = tmp_path / "cache" / f"{symbol.replace('.', '_')}_daily.csv"
    write_bars_csv(path, bars)
    application.state.storage.save_dataset(dataset_from_bars(
        bars, f"{symbol} 测试日线", symbol, "daily", "baostock", False, str(path),
    ))


def test_live_tick_tencent_m5_volume_scaled_to_shares_single_fill(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    application = create_app()
    bar_date = datetime(2026, 11, 13)
    _seed_daily(application, tmp_path, STANDARD, bar_date)
    _seed_daily(application, tmp_path, INDEX, bar_date)
    client = TestClient(application)

    account = client.post("/api/paper/accounts", json={
        "name": "量单位live", "initial_cash": 4_000_000, "auto_strategy": True,
    }).json()

    # 挂一笔 600 股 PERMANENT 买入（NEXT_BAR）。
    created = client.post(f"/api/paper/accounts/{account['id']}/orders", json={
        "symbol": SYMBOL, "side": "BUY", "order_type": "MARKET",
        "quantity": ORDER_QTY, "purpose": "PERMANENT",
    })
    assert created.status_code == 200, created.text
    order_id = created.json()["order"]["id"]

    # HTTP 层 mock：fetch_quote 走 requests.get，fetch_tencent_intraday 走 _get_json，
    # 两处都经过 market.py 真实解析（含 volume ×100）。
    monkeypatch.setattr(
        market_module.requests, "get",
        lambda *a, **k: _FakeResponse(_snapshot_text()),
    )

    def fake_get_json(url: str):
        return {
            "code": 0,
            "data": {"sz301205": {"m5": [
                # 已收盘 5 分钟 bar（14:30）：volume=1,296 手
                ["202611131430", "146.00", f"{CLOSE}", "146.10", "145.90",
                 f"{VOLUME_LOTS}", {}, "7.10"],
            ]}},
        }

    monkeypatch.setattr(market_module, "_get_json", fake_get_json)

    # 屏蔽策略信号（本测试只验证撮合容量口径，不让策略额外发单干扰断言）。
    monkeypatch.setattr(LayeredTStrategy, "on_intraday", lambda self, *a, **k: [])
    monkeypatch.setattr(LayeredTStrategy, "on_daily", lambda self, *a, **k: [])

    response = client.post(
        f"/api/paper/accounts/{account['id']}/live-tick", json={"symbol": SYMBOL},
    )
    assert response.status_code == 200, response.text

    body = client.get(f"/api/paper/accounts/{account['id']}").json()
    target = next(o for o in body["orders"] if o["id"] == order_id)
    fills = [f for f in body["trades"] if f["order_id"] == order_id]
    assert target["status"] == "FILLED", target
    # 修复前：容量只有 200 股（1,296 当股 ×0.2），600 股要拆 3 笔且单次 tick 只成 200。
    # 修复后：1,296 手→129,600 股，容量 ~25,920 股 ≥ 600 → 单笔全成。
    assert len(fills) == 1, f"应单笔成交，实际 {len(fills)} 笔：{fills}"
    assert fills[0]["quantity"] == ORDER_QTY
    assert target["remaining"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
