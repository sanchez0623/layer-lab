"""腾讯行情 volume 单位（手→股）解析单测。

腾讯实时快照 / 5 分钟 K 线 / 日线接口返回的 ``volume`` 单位都是**手**（1 手=100 股）。
``market.py`` 在数据入口统一 ×100 转成**股**，与 quan parquet（回测、/advance）及撮合容量
口径对齐；``amount`` 也随之修正（旧实现用「手」量乘价格，偏小 100 倍）。

本文件 mock 腾讯三种响应（快照 fields[36]、m5 row[5]、qfqday row[5]），断言
``fetch_quote`` / ``fetch_tencent_intraday`` / ``fetch_tencent_daily`` 返回的
``bar.volume`` 是股（×100），``amount`` 是股×价格量级。不联网。
"""

from datetime import datetime

import pytest
import requests

import app.market as market_module
from app.domain import Bar
from app.market import MarketDataService


CLOSE = 146.05
VOLUME_LOTS = 1296.0          # 腾讯返回的「手」
VOLUME_SHARES = VOLUME_LOTS * 100  # 期望换算后的「股」= 129,600


def _snapshot_text(*, amount_wan: str = "18900") -> str:
    """构造腾讯实时快照文本：fields[3]=close、[36]=volume(手)、[37]=amount(万元)。"""
    fields = [""] * 50
    fields[1] = "测试股"
    fields[3] = f"{CLOSE}"          # 现价（close）
    fields[4] = "145.00"           # 昨收
    fields[30] = "20261113143500"  # 时间戳
    fields[36] = f"{VOLUME_LOTS}"  # volume（手）
    fields[37] = amount_wan        # 成交额（万元）
    fields[38] = "2.5"             # 换手率（%）
    return 'v_sz301205="' + "~".join(fields) + '";'


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.encoding = "gbk"

    def raise_for_status(self) -> None:
        return None


def test_fetch_quote_volume_is_shares(monkeypatch, tmp_path):
    """fetch_quote：快照 volume（手）×100 → 股；amount 走真实成交额（万元×1e4）。"""
    service = MarketDataService(tmp_path)
    monkeypatch.setattr(
        market_module.requests, "get",
        lambda *a, **k: _FakeResponse(_snapshot_text()),
    )
    quote, bar = service.fetch_quote("301205")

    assert bar.volume == pytest.approx(VOLUME_SHARES), "快照 volume 应 ×100 转成股"
    assert quote["volume"] == pytest.approx(VOLUME_SHARES)
    # 成交额字段是真实「万元」，与量单位无关：18900 万 = 1.89 亿元
    assert bar.amount == pytest.approx(18900 * 10_000)


def test_fetch_quote_amount_fallback_uses_share_volume(monkeypatch, tmp_path):
    """快照成交额缺失（0）时回退 close×volume，应按**股**算（旧实现按手偏小 100 倍）。"""
    service = MarketDataService(tmp_path)
    monkeypatch.setattr(
        market_module.requests, "get",
        lambda *a, **k: _FakeResponse(_snapshot_text(amount_wan="0")),
    )
    _, bar = service.fetch_quote("301205")
    assert bar.amount == pytest.approx(CLOSE * VOLUME_SHARES)


def test_parse_tencent_batch_volume_is_shares():
    """批量快照解析：volume 同样 ×100 转股（与 fetch_quote 同字段口径）。"""
    result = market_module._parse_tencent_batch(_snapshot_text())
    assert "sz.301205" in result
    assert result["sz.301205"]["volume"] == pytest.approx(VOLUME_SHARES)


def test_fetch_tencent_intraday_volume_and_amount_are_shares(monkeypatch, tmp_path):
    """5 分钟 K 线：row[5]（手）×100 → 股；amount=close×股量。"""
    service = MarketDataService(tmp_path)
    monkeypatch.setattr(service, "fetch_quote", lambda symbol: (
        {"turnover_rate": 0.025, "prev_close": 145.0, "bid": None, "ask": None},
        # 当前快照（用于挂盘口）：volume 也是股
        Bar(
            timestamp=datetime(2026, 11, 13, 14, 35), symbol="sz.301205",
            open=CLOSE, high=CLOSE, low=CLOSE, close=CLOSE,
            volume=VOLUME_SHARES, amount=CLOSE * VOLUME_SHARES, timeframe="5m",
        ),
    ))

    def fake_get_json(url: str):
        return {
            "code": 0,
            "data": {"sz301205": {"m5": [
                # row: [时间, open, close, high, low, volume(手), {}, ...]
                ["202611131435", "146.00", f"{CLOSE}", "146.10", "145.90",
                 f"{VOLUME_LOTS}", {}, "7.10"],
            ]}},
        }

    monkeypatch.setattr(market_module, "_get_json", fake_get_json)
    bars, _ = service.fetch_tencent_intraday("301205")

    assert len(bars) == 1
    assert bars[0].volume == pytest.approx(VOLUME_SHARES), "m5 volume 应 ×100 转成股"
    assert bars[0].amount == pytest.approx(CLOSE * VOLUME_SHARES), "amount 应用股量乘价格"


def test_fetch_tencent_daily_volume_and_amount_are_shares(monkeypatch, tmp_path):
    """日线：qfqday row[5]（手）×100 → 股；amount 回退 close×股量。"""
    service = MarketDataService(tmp_path)

    def fake_get_json(url: str):
        return {
            "code": 0,
            "data": {"sz159915": {"qfqday": [
                # row: [日期, open, close, high, low, volume(手), amount, ...]
                ["2026-11-19", "2.220", "2.230", "2.240", "2.210",
                 "455647", "0", "0"],
            ]}},
        }

    monkeypatch.setattr(market_module, "_get_json", fake_get_json)
    bars = service.fetch_tencent_daily("sz159915", "2026-11-19", "2026-11-19")

    assert len(bars) == 1
    # 455,647 手 → 45,564,700 股（股量级，而非 45 万手）
    assert bars[0].volume == pytest.approx(45_564_700)
    assert bars[0].amount == pytest.approx(2.230 * 45_564_700)


def test_fetch_quote_raises_on_bad_payload(monkeypatch, tmp_path):
    """无效快照（无匹配字段）应报错，不静默返回错单位数据。"""
    service = MarketDataService(tmp_path)
    monkeypatch.setattr(
        market_module.requests, "get",
        lambda *a, **k: _FakeResponse('v_sz301205="";'),
    )
    with pytest.raises((ValueError, requests.RequestException)):
        service.fetch_quote("301205")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
