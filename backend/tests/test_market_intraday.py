"""腾讯盘中 5 分钟 K 线适配层测试（不联网，monkeypatch 掉 HTTP）。"""

from datetime import datetime

import pytest

import app.market as market_module
from app.domain import Bar
from app.market import MarketDataService


def _quote_and_current() -> tuple[dict, Bar]:
    current = Bar(
        timestamp=datetime(2026, 11, 12, 15, 0), symbol="sh.688200",
        open=239.2, high=240.19, low=239.2, close=239.78,
        volume=1_000, amount=239_780, timeframe="5m",
    )
    quote = {
        "turnover_rate": 0.0295, "prev_close": 231.2,
        "bid": 239.7, "ask": 239.78,
    }
    return quote, current


def test_fetch_tencent_intraday_uses_m5_endpoint_and_parses(monkeypatch, tmp_path):
    service = MarketDataService(tmp_path)
    monkeypatch.setattr(service, "fetch_quote", lambda symbol: _quote_and_current())
    captured: dict[str, str] = {}

    def fake_get_json(url: str):
        captured["url"] = url
        return {
            "code": 0,
            "data": {
                "sh688200": {
                    "m5": [
                        ["202611120935", "239.20", "239.50", "240.19", "239.20", "142822.00", {}, "7.10"],
                        ["202611120940", "239.50", "239.78", "239.78", "239.21", "125309.00", {}, "6.23"],
                        ["bad-row"],
                    ]
                }
            },
        }

    monkeypatch.setattr(market_module, "_get_json", fake_get_json)
    bars, quote = service.fetch_tencent_intraday("688200", count=1200)

    assert "mkline" in captured["url"]
    assert ",m5," in captured["url"]
    assert ",m1," not in captured["url"]
    assert len(bars) == 2
    assert {bar.timeframe for bar in bars} == {"5m"}
    assert bars[0].timestamp == datetime(2026, 11, 12, 9, 35)
    assert bars[0].open == pytest.approx(239.20)
    assert bars[-1].close == pytest.approx(239.78)
    assert bars[-1].amount == pytest.approx(239.78 * 125_309)
    assert quote["prev_close"] == pytest.approx(231.2)
    # 只有最后一根 bar 挂当前快照的盘口/换手，不伪造历史盘口。
    assert bars[0].bid is None and bars[0].ask is None
    assert bars[-1].bid == pytest.approx(239.7)
    assert bars[-1].ask == pytest.approx(239.78)


def test_fetch_tencent_intraday_raises_when_m5_missing(monkeypatch, tmp_path):
    service = MarketDataService(tmp_path)
    monkeypatch.setattr(service, "fetch_quote", lambda symbol: _quote_and_current())
    monkeypatch.setattr(
        market_module, "_get_json",
        lambda url: {"code": 0, "data": {"sh688200": {"m5": []}}},
    )
    with pytest.raises(ValueError, match="5分钟K线"):
        service.fetch_tencent_intraday("688200")


def test_source_status_declares_5m_and_300s(tmp_path):
    status = MarketDataService(tmp_path).source_status()
    assert status["realtime_interval_seconds"] == 300
    assert status["intraday_timeframe"] == "5m"
    assert "5分钟" in status["tencent"]["role"]
