"""fuyao 交易日历客户端单测（不联网：用 fake session 替代 HTTP）。"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from app.calendar_api import FuyaoCalendar


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return FakeResponse(self.payload)


def _payload(open_days: list[str]) -> dict:
    return {
        "code": 0,
        "message": "success",
        "data": {"timestamp": 1, "item": [{"date": day} for day in open_days]},
    }


def test_fuyao_calendar_disabled_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("FUYAO_API_KEY", raising=False)
    monkeypatch.delenv("THS_API_KEY", raising=False)
    calendar = FuyaoCalendar(tmp_path)
    assert calendar.enabled is False
    assert calendar.is_trading_day("2026-11-13") is None
    assert calendar.next_trading_day("2026-11-13") is None


def test_fuyao_calendar_reads_key_file(tmp_path, monkeypatch):
    monkeypatch.delenv("FUYAO_API_KEY", raising=False)
    monkeypatch.delenv("THS_API_KEY", raising=False)
    (tmp_path / "fuyao_api_key").write_text("sk-test\n", encoding="utf-8")
    assert FuyaoCalendar(tmp_path).enabled is True


def test_fuyao_calendar_parses_days_and_next(tmp_path, monkeypatch):
    monkeypatch.setenv("FUYAO_API_KEY", "sk-test")
    today = date.today()
    open_days = [(today - timedelta(days=2)).strftime("%Y%m%d"), today.strftime("%Y%m%d"),
                 (today + timedelta(days=3)).strftime("%Y%m%d")]
    session = FakeSession(_payload(open_days))
    calendar = FuyaoCalendar(tmp_path, session=session)

    today_iso = today.isoformat()
    next_iso = (today + timedelta(days=3)).isoformat()
    assert calendar.is_trading_day(today_iso) is True
    # 区间内非开市日（模拟节假日）→ False
    holiday = (today + timedelta(days=1)).isoformat()
    assert calendar.is_trading_day(holiday) is False
    assert calendar.next_trading_day(today_iso) == next_iso
    assert session.calls and session.calls[0]["headers"]["X-api-key"] == "sk-test"

    # 磁盘缓存：新实例不再发请求即可判断
    cached = FuyaoCalendar(tmp_path, session=FakeSession({"code": -1}))
    assert cached.is_trading_day(today_iso) is True


def test_fuyao_calendar_returns_none_on_bad_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("FUYAO_API_KEY", "sk-test")
    calendar = FuyaoCalendar(tmp_path, session=FakeSession({"code": 1002, "message": "bad"}))
    assert calendar.is_trading_day(date.today().isoformat()) is None
    assert calendar.status()["last_error"]
