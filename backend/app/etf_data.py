"""ETF 日线缓存：回测读本地 CSV，缺失时用腾讯日线补采一次。

为什么单独一份缓存：漏斗回测的行情全部来自本地 parquet（不联网、可复现），
而 quan_parquet 源只覆盖 A 股个股与指数、没有基金。ETF beta 腿需要真实 ETF
价格，所以这里落一份独立 CSV（字段少、体量小），由首次联网补采写入，之后
回测只读文件；模拟盘实时撮合仍用腾讯快照（同一交易所代码口径）。

缓存格式（CSV，utf-8）：
    symbol,date,open,high,low,close,volume,amount
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from .domain import Bar, money, price

FIELDS = ("symbol", "date", "open", "high", "low", "close", "volume", "amount")


def cache_path(data_dir: str | Path, symbol: str) -> Path:
    """单只 ETF 一份缓存：data/etf_<code>_daily.csv。"""
    code = str(symbol).split(".")[-1]
    return Path(data_dir) / f"etf_{code}_daily.csv"


def load_etf_bars(path: str | Path, symbol: str) -> list[Bar]:
    """读取缓存并链上前收盘；文件不存在/为空返回空列表。

    ``symbol`` 以调用方为准（标准代码如 ``sz.159915``），不用 CSV 里的 symbol 列：
    缓存可能由不同来源写入（腾讯接口返回 ``sz159915``、回测补采写入标准代码），
    若直接采用文件里的值，撮合时会用 ``sz159915`` 去匹配 ``sz.159915`` 的持仓/委托，
    导致 beta 腿永远不成交。symbol 列仍保留写出（便于人工查看）。
    """
    file = Path(path)
    if not file.exists():
        return []
    rows: list[dict[str, str]] = []
    with file.open("r", encoding="utf-8", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("date")]
    rows.sort(key=lambda row: row["date"])
    bars: list[Bar] = []
    previous: float | None = None
    for row in rows:
        try:
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if close <= 0:
            continue
        bar = Bar(
            timestamp=datetime.fromisoformat(f"{row['date'][:10]}T15:00:00"),
            symbol=str(symbol),
            open=price(float(row.get("open") or close)),
            high=price(float(row.get("high") or close)),
            low=price(float(row.get("low") or close)),
            close=price(close),
            volume=float(row.get("volume") or 0.0),
            amount=money(float(row.get("amount") or 0.0)),
            timeframe="daily",
            prev_close=previous,
        )
        bars.append(bar)
        previous = bar.close
    return bars


def save_etf_bars(path: str | Path, bars: Sequence[Bar]) -> None:
    """原子写缓存（先写临时文件再替换，避免半截文件）。"""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(file.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for bar in sorted(bars, key=lambda item: item.date):
            writer.writerow({
                "symbol": bar.symbol, "date": bar.date,
                "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close,
                "volume": bar.volume, "amount": bar.amount,
            })
    tmp.replace(file)


def merge_bars(existing: Sequence[Bar], incoming: Sequence[Bar]) -> list[Bar]:
    """按日期去重合并（incoming 覆盖同日旧值），返回升序列表。"""
    by_date: dict[str, Bar] = {bar.date: bar for bar in existing}
    for bar in incoming:
        by_date[bar.date] = bar
    return [by_date[key] for key in sorted(by_date)]


def coverage_gap(bars: Sequence[Bar], start: str | None, end: str | None) -> bool:
    """缓存是否覆盖 [start, end]；无区间要求时只看是否为空。"""
    if not bars:
        return True
    if start and bars[0].date > start:
        return True
    if end and bars[-1].date < end:
        return True
    return False


def ensure_etf_bars(
    path: str | Path, symbol: str, start: str | None, end: str | None,
    fetcher: Callable[[str, str | None, str | None], Sequence[Bar]] | None = None,
) -> tuple[list[Bar], str]:
    """确保本地缓存覆盖区间：命中返回 (bars, "cache")，补采返回 (bars, "tencent")。

    ``fetcher`` 由调用方注入（回测/模拟盘各自持有 MarketDataService），
    便于单测替身；缺省时只读缓存，不联网。
    """
    bars = load_etf_bars(path, symbol)
    if not coverage_gap(bars, start, end):
        return bars, "cache"
    if fetcher is None:
        return bars, "cache"
    incoming = list(fetcher(symbol, start, end) or [])
    if incoming:
        bars = merge_bars(bars, incoming)
        save_etf_bars(path, bars)
        return bars, "tencent"
    return bars, "cache"


def previous_close(bars: Sequence[Bar], day: str) -> float | None:
    """day 之前最近一根的收盘价（用于涨跌幅限制/参考价）。"""
    previous = None
    for bar in bars:
        if bar.date >= day:
            break
        previous = bar.close
    return previous
