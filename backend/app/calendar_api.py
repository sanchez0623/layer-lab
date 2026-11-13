"""A 股交易日历（同花顺 fuyao REST API）接入。

用途：「今天是否交易日 / 下一个交易日」的权威判定。
本地 quan 的 ``trade_calendar.parquet`` 只记录开市日、且通常滞后到上一交易日，
覆盖不到当天与未来节假日；fuyao 的交易日序列包含未来日期，能提前识别假期。

接口（https://fuyao.aicubes.cn/docs/api-reference/calendar/）：
    GET {base}/api/a-share/calendar/trading-days?start_date=YYYYMMDD&end_date=YYYYMMDD
    Header: X-api-key
    响应: {"code": 0, "data": {"item": [{"date": "20250523"}, ...]}}

API Key 来源（按顺序）：
1. 环境变量 ``FUYAO_API_KEY``
2. 环境变量 ``THS_API_KEY``（同花顺数据 API 的既有命名）
3. 数据目录下的 ``fuyao_api_key`` 文件（容器命名卷里持久化，避免改 compose 重建）

未配置 Key / 请求失败时 ``enabled`` 为 False、查询返回 None，调用方自行回落。
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

DEFAULT_BASE_URL = "https://fuyao.aicubes.cn"
TRADING_DAYS_PATH = "/api/a-share/calendar/trading-days"
KEY_ENV_NAMES = ("FUYAO_API_KEY", "THS_API_KEY")
KEY_FILE_NAME = "fuyao_api_key"
CACHE_FILE_NAME = "fuyao_calendar.json"

# 缓存窗口：往前 400 天（够回看）、往后 90 天（够覆盖长假与跨年）。
PAST_DAYS = 400
FUTURE_DAYS = 90
CACHE_TTL_SECONDS = 6 * 3600
REQUEST_TIMEOUT_SECONDS = 8.0


def _yyyymmdd(day: str) -> str:
    return str(day)[:10].replace("-", "")


def _iso(day: str) -> str:
    text = str(day).replace("-", "")
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"


class FuyaoCalendar:
    """fuyao 交易日历客户端（内存 + 磁盘缓存，失败静默回落）。"""

    def __init__(
        self, data_dir: str | Path, *, session: requests.Session | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.base_url = os.environ.get("FUYAO_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.api_key = self._resolve_key()
        self._session = session or requests.Session()
        self._days: list[str] | None = None
        self._day_set: set[str] | None = None
        self._loaded_at = 0.0
        self._last_error: str | None = None
        self._cache_path = self.data_dir / CACHE_FILE_NAME
        self._try_load_disk_cache()

    # -- 配置 / 缓存 -------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _resolve_key(self) -> str:
        for name in KEY_ENV_NAMES:
            value = str(os.environ.get(name, "") or "").strip()
            if value:
                return value
        path = self.data_dir / KEY_FILE_NAME
        try:
            if path.exists():
                return path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        return ""

    def _try_load_disk_cache(self) -> None:
        try:
            if not self._cache_path.exists():
                return
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return
            days = payload.get("days")
            fetched_at = float(payload.get("fetched_at") or 0)
            if not isinstance(days, list) or not days or not fetched_at:
                return
            self._apply_days([str(item) for item in days], fetched_at)
        except (OSError, ValueError, TypeError):
            pass

    def _apply_days(self, days: list[str], loaded_at: float) -> None:
        self._days = sorted(days)
        self._day_set = set(self._days)
        self._loaded_at = loaded_at

    def _write_disk_cache(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps({"days": self._days, "fetched_at": self._loaded_at}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _should_refresh(self) -> bool:
        if self._days is None or self._day_set is None:
            return True
        if time.time() - self._loaded_at > CACHE_TTL_SECONDS:
            return True
        today = date.today().isoformat()
        horizon = (date.today() + timedelta(days=30)).isoformat()
        # 窗口必须覆盖「今天」，并尽量覆盖未来 30 天（跨长假/跨年）
        return not (self._days[0] <= today <= self._days[-1] and self._days[-1] >= horizon)

    # -- 拉取 ---------------------------------------------------------------

    def _fetch(self) -> list[str] | None:
        if not self.enabled:
            return None
        start = (date.today() - timedelta(days=PAST_DAYS)).strftime("%Y%m%d")
        end = (date.today() + timedelta(days=FUTURE_DAYS)).strftime("%Y%m%d")
        try:
            response = self._session.get(
                f"{self.base_url}{TRADING_DAYS_PATH}",
                params={"start_date": start, "end_date": end},
                headers={"X-api-key": self.api_key},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload: Any = response.json()
            if not isinstance(payload, dict) or int(payload.get("code", -1)) != 0:
                raise ValueError(f"fuyao 交易日历返回异常: {payload!r}"[:200])
            items = (payload.get("data") or {}).get("item") or []
            days = sorted(
                _iso(str(item.get("date")))
                for item in items
                if isinstance(item, dict) and str(item.get("date", "")).isdigit()
            )
            if not days:
                raise ValueError("fuyao 交易日历返回空列表")
            self._last_error = None
            return days
        except Exception as exc:  # noqa: BLE001 - 网络/格式异常一律回落
            self._last_error = f"{exc.__class__.__name__}: {exc}"[:200]
            return None

    def _ensure(self) -> list[str] | None:
        if self._should_refresh():
            days = self._fetch()
            if days:
                self._apply_days(days, time.time())
                self._write_disk_cache()
            elif self._days is None:
                return None
        return self._days

    # -- 查询 ---------------------------------------------------------------

    def is_trading_day(self, day: str) -> bool | None:
        """``day`` 是否交易日；None 表示无法判断（未配置/拉取失败/超出覆盖范围）。"""
        days = self._ensure()
        if days is None or self._day_set is None:
            return None
        text = str(day)[:10]
        if text < days[0] or text > days[-1]:
            return None
        return text in self._day_set

    def next_trading_day(self, day: str) -> str | None:
        days = self._ensure()
        if not days:
            return None
        text = str(day)[:10]
        for item in days:
            if item > text:
                return item
        return None

    def refresh(self) -> bool:
        days = self._fetch()
        if not days:
            return False
        self._apply_days(days, time.time())
        self._write_disk_cache()
        return True

    def status(self) -> dict:
        days = self._days or []
        return {
            "provider": "fuyao",
            "enabled": self.enabled,
            "base_url": self.base_url,
            "days": len(days),
            "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None,
            "cached_at": self._loaded_at or None,
            "last_error": self._last_error,
        }
