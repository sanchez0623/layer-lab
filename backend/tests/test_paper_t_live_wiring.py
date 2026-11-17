# -*- coding: utf-8 -*-
"""回归：模拟盘 live-tick 必须把实时快照的换手率/盘口传给做T准入。

40f24d6 给 ``_t_eligible`` / ``on_intraday`` 加了 ``live_turnover`` / ``live_bid`` /
``live_ask`` 入参来修「模拟盘做T一次都不触发」，但当时只改了 strategy.py（入参 + 单测），
``main.apply_bar`` 里的调用点从未把实时快照值传下去 → 换手率与盘口在模拟盘恒缺失，
做T准入恒 False。回测路径不受影响（quan parquet 代理 bid/ask + 显式传日线换手），
所以两边行为长期不一致。

本文件拦下「加了参数却没接线」这类回归，并覆盖新暴露到 /config 的做T准入四阈值。
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.data import dataset_from_bars, write_bars_csv
from app.domain import Bar, StrategyConfig
from app.main import create_app
from app.strategy import LayeredTStrategy


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


def _seed_daily(application, tmp_path, symbol: str, closes: list[float]) -> None:
    """live-tick 的自动策略分支需要日线 + 指数数据集，全部用本地 CSV 避免联网。"""
    bars = _daily_bars(symbol, closes)
    path = tmp_path / "cache" / f"{symbol.replace('.', '_')}_daily.csv"
    write_bars_csv(path, bars)
    application.state.storage.save_dataset(dataset_from_bars(
        bars, f"{symbol} 测试日线", symbol, "daily", "baostock", False, str(path),
    ))


def _snapshot(*, turnover_rate: float, bid: float, ask: float) -> Bar:
    """腾讯实时快照 bar：换手率与盘口只挂在这里，历史已收盘 5 分钟 bar 上没有。"""
    return Bar(
        timestamp=datetime(2026, 11, 12, 10, 40), symbol="sh.688200",
        open=245.5, high=245.7, low=245.3, close=245.5,
        volume=1_000_000.0, amount=245_500_000.0, timeframe="5m",
        turnover_rate=turnover_rate, prev_close=240.0, bid=bid, ask=ask,
    )


def _closed_5m_bar() -> Bar:
    """截至快照时刻已收盘的最后一根 5 分钟 bar（换手率/盘口均为 None）。"""
    return Bar(
        timestamp=datetime(2026, 11, 12, 10, 35), symbol="sh.688200",
        open=245.4, high=245.6, low=245.2, close=245.5,
        volume=10_000.0, amount=2_455_000.0, timeframe="5m", prev_close=240.0,
    )


def test_live_tick_forwards_snapshot_turnover_and_book_to_strategy(tmp_path, monkeypatch):
    """回归哨兵：on_intraday 必须收到快照的换手率/盘口，否则做T准入恒 False。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    application = create_app()
    _seed_daily(application, tmp_path, "sh.688200", [245.0] * 30)
    _seed_daily(application, tmp_path, "sz.399006", [100.0] * 5 + [100.0 - i for i in range(25)])
    client = TestClient(application)
    account = client.post("/api/paper/accounts", json={
        "name": "做T接线", "initial_cash": 4_000_000, "auto_strategy": True,
    }).json()

    snapshot = _snapshot(turnover_rate=0.08, bid=245.45, ask=245.55)
    monkeypatch.setattr(application.state.market_service, "fetch_quote", lambda symbol: ({}, snapshot))
    monkeypatch.setattr(
        application.state.market_service, "fetch_tencent_intraday",
        lambda symbol, count=320: ([_closed_5m_bar()], {}),
    )

    captured: dict = {}

    def fake_on_intraday(self, history, recent_bars, bar, account, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(LayeredTStrategy, "on_intraday", fake_on_intraday)

    response = client.post(f"/api/paper/accounts/{account['id']}/live-tick", json={"symbol": "688200"})
    assert response.status_code == 200, response.text
    assert captured, "自动策略分支没有走到 on_intraday（检测被跳过）"
    assert captured["live_turnover"] == 0.08
    assert captured["live_bid"] == 245.45
    assert captured["live_ask"] == 245.55


def test_paper_config_accepts_t_eligibility_thresholds(tmp_path, monkeypatch):
    """做T准入四阈值已可视化可调：/config 局部更新后立刻反映到账户 config。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    client = TestClient(create_app())
    account = client.post("/api/paper/accounts", json={
        "name": "做T门槛", "initial_cash": 4_000_000,
    }).json()
    patch = {
        "t_min_amplitude": 0.05, "t_min_turnover": 0.04,
        "t_max_turnover": 0.15, "t_min_expected_net_spread": 0.02,
    }
    response = client.post(f"/api/paper/accounts/{account['id']}/config", json=patch)
    assert response.status_code == 200, response.text
    config = response.json()["config"]
    for key, value in patch.items():
        assert config[key] == pytest.approx(value), key


def test_paper_config_rejects_inverted_turnover_band(tmp_path, monkeypatch):
    """换手上限低于下限属自相矛盾，必须在源头拦下（400），而不是让做T准入悄悄恒 False。"""
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    client = TestClient(create_app())
    account = client.post("/api/paper/accounts", json={
        "name": "做T门槛越界", "initial_cash": 4_000_000,
    }).json()
    response = client.post(f"/api/paper/accounts/{account['id']}/config", json={
        "t_min_turnover": 0.20, "t_max_turnover": 0.05,
    })
    assert response.status_code == 400, response.text


def test_strategy_config_rejects_zero_amplitude_gate():
    """振幅门槛为 0 会让振幅校验形同虚设，需在 StrategyConfig 校验里拒掉。"""
    with pytest.raises(ValueError, match="t_min_amplitude"):
        StrategyConfig(t_min_amplitude=0.0)