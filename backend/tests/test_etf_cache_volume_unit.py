"""ETF 日线缓存 volume 单位（手→股）迁移单测。

历史 ETF 缓存（腾讯日线旧写入）volume 单位是**手**、amount 是 close×volume 合成（偏小
100 倍）。``etf_data`` 现按首行 ``# volume_unit=share`` 标记归一：无标记的旧文件视为「手」
并 ×100 成股；标记为 share 的文件原样读取（避免二次换算）。

本文件覆盖：旧「手」缓存加载后 ``bar.volume`` 是股量级（159915 某日 ~4500 万股而非
~45 万手）、save 写 share 标记、share 缓存不被二次 ×100。
"""

import csv
from datetime import datetime

import pytest

from app.etf_data import (
    VOLUME_UNIT_MARKER_PREFIX,
    VOLUME_UNIT_SHARE,
    cache_path,
    load_etf_bars,
    save_etf_bars,
)
from app.domain import Bar


FIELDS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
LOT_VOLUME = 455_647          # 2026-11-19 真实成交量（手）
SHARE_VOLUME = LOT_VOLUME * 100  # 期望换算后的股量 = 45,564,700


def _write_legacy_lot_cache(path, rows):
    """写一份无标记的历史缓存（volume/amount 为「手」口径）。"""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _legacy_row(date="2026-11-19", volume=LOT_VOLUME, amount=None):
    close = 2.23
    return {
        "symbol": "sz.159915", "date": date,
        "open": "2.22", "high": "2.24", "low": "2.21", "close": f"{close}",
        "volume": f"{volume}",
        "amount": f"{close * volume if amount is None else amount}",
    }


def test_legacy_lot_cache_loads_as_shares(tmp_path):
    """旧「手」缓存加载后 volume 是股量级（~4500 万股，而非 ~45 万手）。"""
    path = tmp_path / "etf_159915_daily.csv"
    _write_legacy_lot_cache(path, [_legacy_row()])

    bars = load_etf_bars(path, "sz.159915")

    assert len(bars) == 1
    assert bars[0].volume == pytest.approx(SHARE_VOLUME), (
        f"ETF 缓存加载后 volume 应是股（{SHARE_VOLUME}），实际 {bars[0].volume}"
    )
    # amount 也按同比例归一（旧值是 close×手量，偏小 100 倍）
    assert bars[0].amount == pytest.approx(2.23 * SHARE_VOLUME)


def test_save_writes_share_marker_and_roundtrip_not_double_scaled(tmp_path):
    """save 写 # volume_unit=share 标记；share 缓存再加载不二次 ×100。"""
    path = tmp_path / "etf_159915_daily.csv"
    bar = Bar(
        timestamp=datetime(2026, 11, 19, 15),
        symbol="sz.159915", open=2.22, high=2.24, low=2.21, close=2.23,
        volume=float(SHARE_VOLUME), amount=2.23 * SHARE_VOLUME, timeframe="daily",
    )
    save_etf_bars(path, [bar])

    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == f"{VOLUME_UNIT_MARKER_PREFIX}{VOLUME_UNIT_SHARE}"

    loaded = load_etf_bars(path, "sz.159915")
    assert loaded[0].volume == pytest.approx(float(SHARE_VOLUME)), (
        "share 缓存不应被二次 ×100"
    )


def test_ensure_etf_bars_cache_hit_returns_share_volume(tmp_path):
    """ensure_etf_bars 命中缓存时返回的 bar.volume 已是股（撮合容量口径正确）。"""
    from app.etf_data import ensure_etf_bars

    path = tmp_path / "etf_159915_daily.csv"
    _write_legacy_lot_cache(path, [_legacy_row()])

    bars, source = ensure_etf_bars(
        path, "sz.159915", "2026-11-19", "2026-11-19", fetcher=None,
    )
    assert source == "cache"
    assert bars[0].volume == pytest.approx(SHARE_VOLUME)


def test_cache_path_uses_etf_code(tmp_path):
    assert cache_path(tmp_path, "sz.159915").name == "etf_159915_daily.csv"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
