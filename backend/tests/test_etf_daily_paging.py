"""腾讯日线适配层：带日期区间 + 640 根/页向前分页补采（不联网，monkeypatch HTTP）。

背景：旧实现 URL 里起止日期为空、只传 count，腾讯只回区间内最新 640 根，导致
ETF 159915 缓存永远从 2024-03 起、每次回测重复触发"缓存缺失"补采。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import app.market as market_module
from app.market import MarketDataService


def _trading_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _rows(days: list[date]) -> list[list]:
    rows = []
    for index, day in enumerate(days):
        close = 2.0 + index * 0.001
        rows.append([
            day.isoformat(), f"{close - 0.01:.4f}", f"{close:.4f}",
            f"{close + 0.01:.4f}", f"{close - 0.02:.4f}", "1000", "2000",
        ])
    return rows


def _fake_tencent(rows: list[list]):
    """模拟腾讯：区间内最多返回最新 640 根。"""
    calls: list[tuple[str, str]] = []

    def fake_get_json(url: str):
        match = re.search(r"param=([a-z0-9]+),day,([\d-]+),([\d-]+),(\d+),qfq", url)
        assert match, url
        symbol, start_text, end_text, count = match.groups()
        calls.append((start_text, end_text))
        in_range = [row for row in rows if start_text <= row[0] <= end_text]
        limit = min(int(count), market_module.TENCENT_DAILY_MAX_BARS)
        return {
            "code": 0,
            "data": {symbol: {"qfqday": in_range[-limit:]}},
        }

    return fake_get_json, calls


def test_fetch_tencent_daily_pages_back_until_start(monkeypatch, tmp_path):
    """长区间请求必须向前翻页，直到覆盖请求起点（旧实现只能拿到最近 640 根）。"""
    all_days = _trading_days(date(2022, 1, 3), date(2026, 11, 12))
    rows = _rows(all_days)
    fake, calls = _fake_tencent(rows)
    monkeypatch.setattr(market_module, "_get_json", fake)

    service = MarketDataService(tmp_path / "cache")
    bars = service.fetch_tencent_daily("sz159915", "2022-01-04", "2026-11-12")

    assert len(calls) >= 2, f"应至少两页，实际请求 {calls}"
    assert bars[0].date == "2022-01-04", bars[0].date
    assert bars[-1].date == "2026-11-12"
    # 前复权链：后一根的 prev_close = 前一根收盘
    assert bars[1].prev_close == bars[0].close
    assert all(item.symbol == "sz159915" for item in bars)
    # 交易日数量一致（无重复、无缺口）
    expected = [d for d in all_days if "2022-01-04" <= d.isoformat() <= "2026-11-12"]
    assert len(bars) == len(expected)


def test_fetch_tencent_daily_short_range_single_page(monkeypatch, tmp_path):
    """短区间（<640 根）只需一页，不应多余翻页。"""
    all_days = _trading_days(date(2026, 6, 1), date(2026, 11, 12))
    fake, calls = _fake_tencent(_rows(all_days))
    monkeypatch.setattr(market_module, "_get_json", fake)

    service = MarketDataService(tmp_path / "cache")
    bars = service.fetch_tencent_daily("sz159915", "2026-06-01", "2026-11-12")
    assert len(calls) == 1
    assert bars[0].date >= "2026-06-01" and bars[-1].date == "2026-11-12"


def test_fetch_tencent_daily_raises_when_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        market_module, "_get_json",
        lambda url: {"code": 0, "data": {"sz159915": {"qfqday": []}}},
    )
    service = MarketDataService(tmp_path / "cache")
    try:
        service.fetch_tencent_daily("sz159915", "2022-01-04", "2022-02-01")
    except ValueError as exc:
        assert "没有返回" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("空数据应抛 ValueError")
