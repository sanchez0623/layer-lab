from __future__ import annotations

import csv
import math
import random
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

from .domain import Bar, Dataset, money, price, utc_now


TIMEFRAMES = {"daily", "1m", "5m", "tick"}
ALIASES = {
    "timestamp": {"timestamp", "datetime", "date", "time", "日期", "时间", "交易日期", "交易时间"},
    "symbol": {"symbol", "code", "ticker", "证券代码", "股票代码", "代码", "标的"},
    "open": {"open", "openprice", "开盘", "开盘价"},
    "high": {"high", "highprice", "最高", "最高价"},
    "low": {"low", "lowprice", "最低", "最低价"},
    "close": {"close", "closeprice", "收盘", "收盘价", "最新价"},
    "volume": {"volume", "vol", "成交量", "成交量(股)", "成交量（股）"},
    "amount": {"amount", "turnover", "成交额", "成交金额", "成交额(元)", "成交额（元）"},
    "turnover_rate": {"turnover_rate", "turnoverrate", "换手率", "换手率(%)"},
    "prev_close": {"prev_close", "preclose", "previousclose", "昨收", "前收盘"},
    "bid": {"bid", "bid1", "买一", "买一价"},
    "ask": {"ask", "ask1", "卖一", "卖一价"},
}


def _normalized(text: str) -> str:
    return text.strip().lower().replace(" ", "").replace("_", "")


def _field_map(fieldnames: Iterable[str]) -> dict[str, str]:
    normalized_aliases = {
        _normalized(alias): canonical
        for canonical, aliases in ALIASES.items()
        for alias in aliases
    }
    result: dict[str, str] = {}
    for original in fieldnames:
        canonical = normalized_aliases.get(_normalized(original))
        if canonical and canonical not in result:
            result[canonical] = original
    return result


def parse_timestamp(value: str, timeframe: str) -> datetime:
    text = value.strip().replace("/", "-")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = datetime.strptime(text, "%Y%m%d")
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return datetime.combine(parsed.date(), time()) if timeframe == "daily" else parsed


def _number(row: dict[str, str], fields: dict[str, str], name: str, default: float | None = None) -> float | None:
    original = fields.get(name)
    if original is None or row.get(original) is None or str(row.get(original, "")).strip() == "":
        return default
    text = str(row[original]).strip().replace(",", "").replace("%", "")
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"字段 {original} 不是合法数字: {text}") from exc


def read_csv_bars(path: str | Path, timeframe: str, symbol: str = "") -> list[Bar]:
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"timeframe 必须是 {sorted(TIMEFRAMES)} 之一")
    source = Path(path)
    raw = source.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("CSV 编码不受支持，请使用 UTF-8 或 GB18030")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise ValueError("CSV 缺少表头")
    fields = _field_map(reader.fieldnames)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - fields.keys()
    if missing:
        raise ValueError(f"CSV 缺少必要字段: {', '.join(sorted(missing))}")

    deduplicated: dict[datetime, Bar] = {}
    for line_number, row in enumerate(reader, start=2):
        try:
            timestamp = parse_timestamp(str(row[fields["timestamp"]]), timeframe)
            values = {name: _number(row, fields, name) for name in ("open", "high", "low", "close")}
            assert all(value is not None for value in values.values())
            open_, high, low, close = (float(values[name]) for name in ("open", "high", "low", "close"))
            volume = float(_number(row, fields, "volume", 0.0) or 0.0)
            amount = float(_number(row, fields, "amount", close * volume) or 0.0)
            if min(open_, high, low, close) <= 0:
                raise ValueError("OHLC 必须为正数")
            if high < max(open_, close, low) or low > min(open_, close, high):
                raise ValueError("OHLC 关系不合法")
            if volume < 0 or amount < 0:
                raise ValueError("成交量和成交额不得为负")
            bar_symbol = str(row.get(fields.get("symbol", ""), "") or symbol or "UNKNOWN").strip()
            turnover = _number(row, fields, "turnover_rate")
            if turnover is not None and turnover > 1:
                turnover /= 100.0
            prev_close = _number(row, fields, "prev_close")
            bar = Bar(
                timestamp=timestamp,
                symbol=bar_symbol,
                open=price(open_), high=price(high), low=price(low), close=price(close),
                volume=volume, amount=money(amount), timeframe=timeframe,
                turnover_rate=turnover, prev_close=price(prev_close) if prev_close else None,
                bid=_number(row, fields, "bid"), ask=_number(row, fields, "ask"),
            )
            deduplicated[timestamp] = bar
        except (ValueError, AssertionError) as exc:
            raise ValueError(f"CSV 第{line_number}行: {exc}") from exc
    bars = sorted(deduplicated.values(), key=lambda item: item.timestamp)
    for index, bar in enumerate(bars):
        if bar.prev_close is None and index:
            bar.prev_close = bars[index - 1].close
    if not bars:
        raise ValueError("CSV 没有有效数据行")
    return bars


def write_bars_csv(path: str | Path, bars: Iterable[Bar]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    names = [
        "timestamp", "symbol", "open", "high", "low", "close", "volume", "amount",
        "timeframe", "turnover_rate", "prev_close", "bid", "ask",
    ]
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names)
        writer.writeheader()
        for bar in bars:
            row = bar.to_dict()
            writer.writerow({name: row.get(name) for name in names})


def dataset_from_bars(
    bars: list[Bar], name: str, symbol: str, timeframe: str,
    source: str, is_synthetic: bool, file_path: str,
) -> Dataset:
    if not bars:
        raise ValueError("空数据集")
    return Dataset(
        id=str(uuid.uuid4()), name=name, symbol=symbol or bars[0].symbol,
        timeframe=timeframe, start=bars[0].timestamp.isoformat(), end=bars[-1].timestamp.isoformat(),
        rows=len(bars), source=source, is_synthetic=is_synthetic,
        created_at=utc_now(), file_path=file_path,
    )


def _business_days(end: datetime, count: int) -> list[datetime]:
    days: list[datetime] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(datetime.combine(cursor.date(), time()))
        cursor -= timedelta(days=1)
    return list(reversed(days))


def generate_demo_data(seed: int = 20261111) -> tuple[list[Bar], list[Bar], list[Bar]]:
    """固定种子的合成行情；日期和数值均不来自任何真实市场。"""
    rng = random.Random(seed)
    days = _business_days(datetime(2025, 12, 31), 320)
    stock: list[Bar] = []
    index: list[Bar] = []
    previous = 10.0
    index_previous = 3000.0
    for i, day in enumerate(days):
        cyclical = 0.011 * math.sin(i / 8.0) + 0.006 * math.sin(i / 23.0)
        daily_return = max(-0.065, min(0.065, 0.0012 + cyclical + rng.uniform(-0.009, 0.009)))
        close = max(2.0, previous * (1 + daily_return))
        open_ = previous * (1 + rng.uniform(-0.012, 0.012))
        spread = 0.018 + abs(daily_return) * 0.65 + rng.uniform(0.0, 0.012)
        high = max(open_, close) * (1 + spread)
        low = min(open_, close) * (1 - spread * 0.85)
        volume = float(7_000_000 + int(2_000_000 * (1 + math.sin(i / 7))) + rng.randint(0, 1_500_000))
        turnover = 0.035 + 0.035 * (0.5 + 0.5 * math.sin(i / 11.0))
        stock.append(Bar(
            timestamp=day, symbol="SYNTH001", open=price(open_), high=price(high),
            low=price(low), close=price(close), volume=volume,
            amount=money(volume * (open_ + close) / 2), timeframe="daily",
            turnover_rate=turnover, prev_close=price(previous),
            bid=price(close - 0.01), ask=price(close + 0.01),
        ))
        previous = close

        index_return = 0.00065 + 0.0025 * math.sin(i / 19.0) + rng.uniform(-0.0025, 0.0025)
        index_close = index_previous * (1 + index_return)
        index_open = index_previous * (1 + rng.uniform(-0.0015, 0.0015))
        index.append(Bar(
            timestamp=day, symbol="SYNTH_INDEX", open=price(index_open),
            high=price(max(index_open, index_close) * 1.004),
            low=price(min(index_open, index_close) * 0.996), close=price(index_close),
            volume=200_000_000.0, amount=money(index_close * 200_000_000),
            timeframe="daily", prev_close=price(index_previous),
        ))
        index_previous = index_close

    intraday: list[Bar] = []
    for day_index in range(len(days) - 48, len(days)):
        daily = stock[day_index]
        minutes = [time(9, 30)] * 240
        del minutes
        timestamps: list[datetime] = []
        cursor = datetime.combine(daily.timestamp.date(), time(9, 30))
        for _ in range(120):
            timestamps.append(cursor)
            cursor += timedelta(minutes=1)
        cursor = datetime.combine(daily.timestamp.date(), time(13, 0))
        for _ in range(120):
            timestamps.append(cursor)
            cursor += timedelta(minutes=1)
        last = daily.open
        for j, timestamp in enumerate(timestamps):
            progress = (j + 1) / len(timestamps)
            baseline = daily.open * (1 - progress) + daily.close * progress
            wave = math.sin(j / 13.0) * 0.004 + math.sin(j / 41.0) * 0.003
            close = baseline * (1 + wave)
            open_ = last
            high = max(open_, close) * 1.0018
            low = min(open_, close) * 0.9982
            volume = max(1000.0, daily.volume / 240 * (0.7 + 0.6 * rng.random()))
            intraday.append(Bar(
                timestamp=timestamp, symbol=daily.symbol, open=price(open_), high=price(high),
                low=price(low), close=price(close), volume=volume,
                amount=money(volume * (open_ + close) / 2), timeframe="1m",
                turnover_rate=daily.turnover_rate, prev_close=daily.prev_close,
                bid=price(close - 0.01), ask=price(close + 0.01),
            ))
            last = close
    return stock, intraday, index


def save_demo_datasets(directory: str | Path) -> tuple[Dataset, Dataset, Dataset]:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    daily, intraday, index = generate_demo_data()
    result: list[Dataset] = []
    for bars, name, symbol, timeframe, filename in (
        (daily, "synthetic_demo_daily_320", "SYNTH001", "daily", "synthetic_demo_daily.csv"),
        (intraday, "synthetic_demo_intraday_1m_48d", "SYNTH001", "1m", "synthetic_demo_intraday_1m.csv"),
        (index, "synthetic_demo_index_daily", "SYNTH_INDEX", "daily", "synthetic_demo_index_daily.csv"),
    ):
        path = target / filename
        write_bars_csv(path, bars)
        result.append(dataset_from_bars(bars, name, symbol, timeframe, "synthetic", True, str(path)))
    return result[0], result[1], result[2]
