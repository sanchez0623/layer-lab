from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

import requests

from .data import dataset_from_bars, write_bars_csv
from .domain import Bar, Dataset, money, price

if TYPE_CHECKING:  # 避免与 quan_data -> universe -> market 的导入环
    from .quan_data import QuanStore


QUAN_DAILY_SOURCE = "quan_parquet"
QUAN_INDEX_SOURCE = "quan_index"


TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={symbol}"
# 盘中检测口径：5 分钟 K 线（腾讯 mkline 周期 m5），不再使用 1 分钟。
TENCENT_INTRADAY_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={symbol},{period},,{count}"
TENCENT_INTRADAY_PERIOD = "m5"
TENCENT_DAILY_URL = "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{count},qfq"
INTRADAY_TIMEFRAME = "5m"
INTRADAY_BAR_MINUTES = 5
REALTIME_INTERVAL_SECONDS = 300
# 腾讯 m5 接口实测单次最多返回 320 根（约 6~7 个交易日），请求更大的 count 也不会更多。
TENCENT_INTRADAY_MAX_BARS = 320
HTTP_TIMEOUT = 15.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LayerLab/1.0"


class SymbolInfo:
    def __init__(self, raw: str, standard: str, baostock: str, tencent: str, index_baostock: str, index_tencent: str) -> None:
        self.raw = raw
        self.standard = standard
        self.baostock = baostock
        self.tencent = tencent
        self.index_baostock = index_baostock
        self.index_tencent = index_tencent

    @property
    def code(self) -> str:
        """6 位纯代码，供前端输入/展示使用。"""
        return self.standard.split(".", 1)[1]

    @property
    def exchange(self) -> str:
        return self.standard.split(".", 1)[0]

    @property
    def board(self) -> str:
        if self.code.startswith("900"):
            return "沪市B股"
        if self.code.startswith("200"):
            return "深市B股"
        if self.exchange == "sh":
            return "科创板" if self.code.startswith(("688", "689")) else "沪市主板"
        return "创业板" if self.code.startswith(("300", "301", "302")) else "深市主板"


def _infer_exchange(code: str) -> str:
    """只输 6 位代码时由后端按 A 股代码段判定交易所。"""
    if code.startswith(("6", "900")):
        return "sh"
    if code.startswith(("0", "2", "3")):
        return "sz"
    if code.startswith(("4", "8")):
        return "bj"
    return ""


def normalize_symbol(raw: str) -> SymbolInfo:
    # NFKC 把全角字符/全角点号折成半角，避免从行情软件粘贴时带上不可见格式。
    text = unicodedata.normalize("NFKC", raw or "").strip().lower().replace(" ", "")
    if not text:
        raise ValueError("请输入标的代码")

    exchange = ""
    code = text
    if "." in text:
        prefix, tail = text.split(".", 1)
        if len(prefix) == 6 and prefix.isdigit() and len(tail) == 2:
            code, exchange = prefix, tail
        else:
            exchange, code = prefix, tail
    elif re.fullmatch(r"(sh|sz|bj)[0-9]{6}", text):
        exchange, code = text[:2], text[2:]
    elif re.fullmatch(r"[0-9]{6}", text):
        code = text
    elif re.fullmatch(r"[a-z]{1,5}[0-9a-z.]{1,10}", text):
        raise ValueError("当前仅支持A股6位数字代码或 sh/sz 前缀代码")

    if not re.fullmatch(r"[0-9]{6}", code or ""):
        raise ValueError(f"标的代码不合法: {raw}")
    if not exchange:
        exchange = _infer_exchange(code)
    if not exchange:
        raise ValueError(f"无法识别代码 {code} 的交易所，请确认是 A 股 6 位代码")
    if exchange not in {"sh", "sz", "bj"}:
        raise ValueError(f"不支持的交易所前缀: {exchange}")
    if exchange == "bj":
        raise ValueError("北交所暂不在本系统的数据源覆盖范围内")

    standard = f"{exchange}.{code}"
    is_star = code.startswith(("688", "689"))
    index_code = "000688" if exchange == "sh" and is_star else "399006"
    index_exchange = "sh" if index_code == "000688" else "sz"
    return SymbolInfo(
        raw=raw,
        standard=standard,
        baostock=standard,
        tencent=f"{exchange}{code}",
        index_baostock=f"{index_exchange}.{index_code}",
        index_tencent=f"{index_exchange}{index_code}",
    )


def _get_json(url: str) -> dict[str, Any]:
    response = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    return response.json()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


def _parse_tencent_batch(text: str) -> dict[str, dict[str, Any]]:
    """解析腾讯批量快照文本（GBK 已解码）→ {标准代码: quote}。无效代码跳过。"""
    result: dict[str, dict[str, Any]] = {}
    for match in re.finditer(r'v_([a-z]{2})(\d{6})="([^"]*)"', text):
        exchange, code, payload = match.groups()
        fields = payload.split("~")
        if len(fields) < 45 or not fields[3]:
            continue
        close = _float(fields[3])
        if close <= 0:
            continue
        prev_close = _float(fields[4], close)
        volume = _float(fields[36])
        amount = _float(fields[37]) * 10000.0
        if amount <= 0 and volume > 0:
            amount = close * volume
        standard = f"{exchange}.{code}"
        try:
            symbol = normalize_symbol(standard)
        except ValueError:
            continue
        turnover = _float(fields[38]) / 100.0 if len(fields) > 38 and fields[38] else None
        result[standard] = {
            "symbol": standard,
            "code": code,
            "name": fields[1],
            "board": symbol.board,
            "price": price(close),
            "prev_close": price(prev_close),
            "change": _float(fields[31]),
            "change_pct": round(_float(fields[32]) / 100.0, 6) if fields[32] else 0.0,
            "amount": money(amount),
            "turnover_rate": round(turnover, 6) if turnover is not None else None,
            "volume": volume,
            "source": "tencent",
        }
    return result


class MarketDataService:
    """历史行情 + 腾讯实时/5分钟数据适配层。

    - 历史日线/指数/5分钟：``quan`` 数据源（quan_quant parquet，只读）优先；
      未注入 ``quan`` 时回落 baostock + 腾讯日线兜底（旧链路）。
    - 实时快照与盘中 5 分钟 K 线：始终来自腾讯行情。

    输出统一为 Bar；真实来源在 Dataset.source / data_info 中保留可追溯字段。
    """

    def __init__(self, cache_dir: str | Path, *, quan: "QuanStore | None" = None) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.quan = quan

    def source_status(self) -> dict[str, Any]:
        sources: dict[str, Any] = {}
        if self.quan is not None:
            sources["quan_parquet"] = {
                "role": "历史日线（前复权）/ 指数 / 5分钟K线（只读 parquet）",
                "status": "active",
                "path": str(self.quan.path),
            }
        else:
            sources["baostock"] = {"role": "历史日线（前复权）", "status": "installed"}
        sources["tencent"] = {"role": "实时快照与5分钟K线", "status": "configured"}
        sources["realtime_interval_seconds"] = REALTIME_INTERVAL_SECONDS
        sources["intraday_timeframe"] = INTRADAY_TIMEFRAME
        sources["note"] = (
            "系统只读取行情，不向任何券商或交易接口发送委托。"
            + ("历史数据来自 quan_quant 的 parquet 数据目录。" if self.quan is not None else "")
        )
        return sources

    def fetch_quote(self, raw_symbol: str) -> tuple[dict[str, Any], Bar]:
        symbol = normalize_symbol(raw_symbol)
        url = TENCENT_QUOTE_URL.format(symbol=symbol.tencent)
        response = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        text = response.text
        match = re.search(r'v_[A-Za-z0-9]+="([^"]*)"', text)
        if not match:
            raise ValueError(f"腾讯实时行情返回格式异常: {text[:80]}")
        fields = match.group(1).split("~")
        if len(fields) < 45 or not fields[3]:
            raise ValueError(f"腾讯实时行情缺少有效价格: {symbol.tencent}")

        timestamp_text = fields[30]
        try:
            timestamp = datetime.strptime(timestamp_text, "%Y%m%d%H%M%S")
        except ValueError:
            timestamp = datetime.now()
        close = _float(fields[3])
        prev_close = _float(fields[4], close)
        open_ = _float(fields[5], close)
        high = _float(fields[33], close)
        low = _float(fields[34], close)
        volume = _float(fields[36])
        amount = _float(fields[37]) * 10000.0
        turnover = _float(fields[38]) / 100.0 if fields[38] else None
        bid = _float(fields[9]) if len(fields) > 9 and fields[9] else None
        ask = _float(fields[19]) if len(fields) > 19 and fields[19] else None
        if amount <= 0 and volume > 0:
            amount = close * volume
        bar = Bar(
            timestamp=timestamp, symbol=symbol.standard, open=price(open_), high=price(high),
            low=price(low), close=price(close), volume=volume, amount=money(amount),
            # 快照 bar 标记为盘中检测粒度 5m；其 OHLC/volume 是全天口径，
            # 只用于策略信号与展示，撮合前必须经 snapshot_execution_bar 转换。
            timeframe=INTRADAY_TIMEFRAME, turnover_rate=turnover, prev_close=price(prev_close),
            bid=price(bid) if bid else None, ask=price(ask) if ask else None,
        )
        quote = {
            "symbol": symbol.standard, "code": symbol.code, "standard": symbol.standard,
            "exchange": symbol.exchange, "board": symbol.board, "name": fields[1], "price": price(close),
            "prev_close": price(prev_close), "change": _float(fields[31]),
            "change_pct": _float(fields[32]) / 100.0 if fields[32] else 0.0,
            "volume": volume, "amount": money(amount), "turnover_rate": turnover,
            "bid": price(bid) if bid else None, "ask": price(ask) if ask else None,
            "timestamp": timestamp.isoformat(timespec="seconds"), "source": "tencent",
        }
        return quote, bar

    def fetch_quotes_batch(self, raw_symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        """腾讯批量快照：每批 ≤80 个代码，返回 {标准代码: quote}。

        GBK 解码；无效代码（``v_xxx="";``）跳过；网络异常抛 ``requests.RequestException``。
        """
        symbols: list[SymbolInfo] = []
        seen: set[str] = set()
        for raw in raw_symbols or []:
            try:
                symbol = normalize_symbol(str(raw))
            except ValueError:
                continue
            if symbol.standard in seen:
                continue
            seen.add(symbol.standard)
            symbols.append(symbol)

        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(symbols), 80):
            chunk = symbols[start : start + 80]
            url = TENCENT_QUOTE_URL.format(symbol=",".join(item.tencent for item in chunk))
            response = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            response.encoding = "gbk"
            result.update(_parse_tencent_batch(response.text))
        return result

    def fetch_index_daily_by_code(
        self, index_code: str, start_date: str, end_date: str
    ) -> tuple[list[Bar], str]:
        """按标准指数代码取日线：``sz.399006`` / ``sh.000688``。

        ``sh.000688``（科创50）baostock 不覆盖，沿用腾讯兜底，source="tencent_index"。
        """
        text = unicodedata.normalize("NFKC", str(index_code or "")).strip().lower().replace(" ", "")
        if "." not in text and re.fullmatch(r"\d{6}", text):
            text = ("sh." if text.startswith("000") else "sz.") + text
        exchange, _, code = text.partition(".")
        if exchange not in {"sh", "sz"} or not re.fullmatch(r"\d{6}", code or ""):
            raise ValueError(f"指数代码不合法: {index_code}")
        standard = f"{exchange}.{code}"
        tencent_symbol = f"{exchange}{code}"

        if self.quan is not None:
            bars = self.quan.load_index(standard, max_rows=4000, start=start_date, end=end_date)
            if bars:
                return bars, QUAN_INDEX_SOURCE

        if standard == "sh.000688":
            bars = self.fetch_tencent_daily(tencent_symbol, start_date, end_date)
            for bar in bars:
                bar.symbol = standard
            return bars, "tencent_index"

        try:
            import baostock as bs
        except ImportError as exc:
            raise ValueError("baostock 未安装，无法获取指数日线") from exc
        login = bs.login()
        if login.error_code != "0":
            raise ValueError(f"baostock 登录失败: {login.error_msg}")
        try:
            fields = "date,code,open,high,low,close,preclose,volume,amount"
            result_set = bs.query_history_k_data_plus(
                standard, fields, start_date=start_date, end_date=end_date,
                frequency="d", adjustflag="3",
            )
            if result_set.error_code != "0":
                raise ValueError(f"baostock 指数查询失败: {result_set.error_msg}")
            rows: list[list[str]] = []
            while result_set.next():
                rows.append(list(result_set.get_row_data()))
        finally:
            bs.logout()

        if not rows and standard == "sh.000688":
            bars = self.fetch_tencent_daily(tencent_symbol, start_date, end_date)
            for bar in bars:
                bar.symbol = standard
            return bars, "tencent_index"

        bars: list[Bar] = []
        previous_close: float | None = None
        for row in rows:
            if len(row) < 9 or not row[0] or not row[5]:
                continue
            timestamp = datetime.strptime(row[0], "%Y-%m-%d")
            open_ = _float(row[2])
            high = _float(row[3])
            low = _float(row[4])
            close = _float(row[5])
            volume = _float(row[7])
            amount = _float(row[8])
            if min(open_, high, low, close) <= 0:
                continue
            if amount <= 0 and volume > 0:
                amount = close * volume
            if previous_close is None:
                previous_close = _float(row[6], close)
            bars.append(Bar(
                timestamp=timestamp, symbol=standard, open=price(open_),
                high=price(high), low=price(low), close=price(close), volume=volume,
                amount=money(amount), timeframe="daily", prev_close=price(previous_close),
            ))
            previous_close = close
        if not bars:
            bars = self.fetch_tencent_daily(tencent_symbol, start_date, end_date)
            for bar in bars:
                bar.symbol = standard
            return bars, "tencent_index"
        return bars, "baostock_index"

    def fetch_baostock_daily(self, raw_symbol: str, start_date: str, end_date: str) -> list[Bar]:
        symbol = normalize_symbol(raw_symbol)
        try:
            import baostock as bs
        except ImportError as exc:
            raise ValueError("baostock 未安装，无法获取历史日线") from exc

        login = bs.login()
        if login.error_code != "0":
            raise ValueError(f"baostock 登录失败: {login.error_msg}")
        try:
            fields = "date,code,open,high,low,close,preclose,volume,amount,turn"
            result_set = bs.query_history_k_data_plus(
                symbol.baostock, fields, start_date=start_date, end_date=end_date,
                frequency="d", adjustflag="2",
            )
            if result_set.error_code != "0":
                raise ValueError(f"baostock 日线查询失败: {result_set.error_msg}")
            rows: list[list[str]] = []
            while result_set.next():
                rows.append(result_set.get_row_data())
        finally:
            bs.logout()

        bars: list[Bar] = []
        previous_close: float | None = None
        for row in rows:
            if not row[0] or not row[5]:
                continue
            timestamp = datetime.strptime(row[0], "%Y-%m-%d")
            open_ = _float(row[2])
            high = _float(row[3])
            low = _float(row[4])
            close = _float(row[5])
            volume = _float(row[7])
            amount = _float(row[8])
            turnover = _float(row[9]) / 100.0 if row and len(row) > 9 and row[9] else None
            if min(open_, high, low, close) <= 0:
                continue
            if amount <= 0 and volume > 0:
                amount = close * volume
            if previous_close is None:
                previous_close = _float(row[6], close)
            bars.append(Bar(
                timestamp=timestamp, symbol=symbol.standard, open=price(open_),
                high=price(high), low=price(low), close=price(close), volume=volume,
                amount=money(amount), timeframe="daily", turnover_rate=turnover,
                prev_close=price(previous_close),
            ))
            previous_close = close
        if not bars:
            raise ValueError(f"baostock 在 {start_date}~{end_date} 没有返回 {symbol.standard} 的日线")
        return bars

    def fetch_index_daily(self, raw_symbol: str, start_date: str, end_date: str) -> tuple[list[Bar], str]:
        symbol = normalize_symbol(raw_symbol)
        try:
            import baostock as bs
        except ImportError as exc:
            raise ValueError("baostock 未安装，无法获取指数日线") from exc
        login = bs.login()
        if login.error_code != "0":
            raise ValueError(f"baostock 登录失败: {login.error_msg}")
        try:
            fields = "date,code,open,high,low,close,preclose,volume,amount"
            result_set = bs.query_history_k_data_plus(
                symbol.index_baostock, fields, start_date=start_date, end_date=end_date,
                frequency="d", adjustflag="3",
            )
            if result_set.error_code != "0":
                raise ValueError(f"baostock 指数查询失败: {result_set.error_msg}")
            rows: list[list[str]] = []
            while result_set.next():
                rows.append(result_set.get_row_data())
        finally:
            bs.logout()

        if not rows and symbol.index_baostock == "sh.000688":
            # baostock 当前不覆盖科创50；降级到腾讯指数日线，而不是编造数据。
            bars = self.fetch_tencent_daily(symbol.index_tencent, start_date, end_date)
            for bar in bars:
                bar.symbol = symbol.index_baostock
            return bars, "tencent_index"

        bars: list[Bar] = []
        previous_close: float | None = None
        for row in rows:
            if not row[0] or not row[5]:
                continue
            timestamp = datetime.strptime(row[0], "%Y-%m-%d")
            open_ = _float(row[2])
            high = _float(row[3])
            low = _float(row[4])
            close = _float(row[5])
            volume = _float(row[7])
            amount = _float(row[8])
            if min(open_, high, low, close) <= 0:
                continue
            if amount <= 0 and volume > 0:
                amount = close * volume
            if previous_close is None:
                previous_close = _float(row[6], close)
            bars.append(Bar(
                timestamp=timestamp, symbol=symbol.index_baostock, open=price(open_),
                high=price(high), low=price(low), close=price(close), volume=volume,
                amount=money(amount), timeframe="daily", prev_close=price(previous_close),
            ))
            previous_close = close
        if not bars:
            raise ValueError(f"baostock 没有返回指数 {symbol.index_baostock} 的日线")
        return bars, "baostock_index"

    def fetch_tencent_daily(self, tencent_symbol: str, start_date: str, end_date: str) -> list[Bar]:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
        approximate_days = max(320, int((end - start).days * 1.6) + 40)
        url = TENCENT_DAILY_URL.format(symbol=tencent_symbol, count=min(approximate_days, 2000))
        payload = _get_json(url)
        if payload.get("code") != 0:
            raise ValueError(f"腾讯指数日线返回错误: {payload.get('msg')}")
        node = payload.get("data", {}).get(tencent_symbol, {})
        rows = node.get("day", [])
        bars: list[Bar] = []
        previous_close: float | None = None
        for row in rows:
            try:
                timestamp = datetime.strptime(str(row[0]), "%Y-%m-%d")
            except ValueError:
                continue
            if not (start <= timestamp.date() <= end):
                previous_close = _float(row[2]) if len(row) > 2 else None
                continue
            open_ = _float(row[1])
            close = _float(row[2])
            high = _float(row[3])
            low = _float(row[4])
            volume = _float(row[5]) if len(row) > 5 else 0.0
            amount = _float(row[6]) if len(row) > 6 else 0.0
            if min(open_, high, low, close) <= 0:
                continue
            if amount <= 0 and volume > 0:
                amount = close * volume
            if previous_close is None:
                previous_close = _float(row[2], close)
            bars.append(Bar(
                timestamp=timestamp, symbol=tencent_symbol, open=price(open_),
                high=price(high), low=price(low), close=price(close), volume=volume,
                amount=money(amount), timeframe="daily", prev_close=price(previous_close),
            ))
            previous_close = close
        if not bars:
            raise ValueError(f"腾讯没有返回指数 {tencent_symbol} 在指定区间的日线")
        return bars

    def fetch_tencent_intraday(
        self, raw_symbol: str, count: int = TENCENT_INTRADAY_MAX_BARS,
    ) -> tuple[list[Bar], dict[str, Any]]:
        """盘中 5 分钟 K 线（腾讯 mkline ``m5``）。

        腾讯该接口单次最多返回 320 根 5 分钟 K 线（约 6~7 个交易日），
        请求更大的 ``count`` 也不会返回更多，长周期回测需以日线为主并明示覆盖范围。
        """
        symbol = normalize_symbol(raw_symbol)
        quote, current = self.fetch_quote(symbol.standard)
        url = TENCENT_INTRADAY_URL.format(
            symbol=symbol.tencent,
            period=TENCENT_INTRADAY_PERIOD,
            count=max(1, min(count, TENCENT_INTRADAY_MAX_BARS)),
        )
        payload = _get_json(url)
        if payload.get("code") != 0:
            raise ValueError(f"腾讯5分钟K线返回错误: {payload.get('msg')}")
        node = payload.get("data", {}).get(symbol.tencent, {})
        rows = node.get(TENCENT_INTRADAY_PERIOD, [])
        if not rows:
            raise ValueError(f"腾讯没有返回 {symbol.standard} 的5分钟K线")

        bars: list[Bar] = []
        for row in rows:
            try:
                timestamp = datetime.strptime(str(row[0]), "%Y%m%d%H%M")
            except ValueError:
                continue
            open_ = _float(row[1])
            close = _float(row[2])
            high = _float(row[3])
            low = _float(row[4])
            volume = _float(row[5])
            if min(open_, high, low, close) <= 0:
                continue
            amount = close * volume
            is_last = timestamp == current.timestamp
            bars.append(Bar(
                timestamp=timestamp, symbol=symbol.standard, open=price(open_),
                high=price(high), low=price(low), close=price(close), volume=volume,
                amount=money(amount), timeframe=INTRADAY_TIMEFRAME,
                turnover_rate=quote.get("turnover_rate") if is_last else None,
                prev_close=price(quote.get("prev_close") or close),
                bid=price(quote.get("bid")) if is_last and quote.get("bid") else None,
                ask=price(quote.get("ask")) if is_last and quote.get("ask") else None,
            ))
        if not bars:
            raise ValueError(f"腾讯5分钟K线解析后没有有效数据: {symbol.standard}")
        # 腾讯分钟接口不带逐 bar 盘口；仅把当前快照的盘口/换手挂在最后一根 bar，
        # 供模拟盘实时撮合和做T准入使用，不伪造成历史盘口。
        bars[-1].turnover_rate = quote.get("turnover_rate")
        if quote.get("bid"):
            bars[-1].bid = price(quote["bid"])
        if quote.get("ask"):
            bars[-1].ask = price(quote["ask"])
        return bars, quote

    def _cache_path(self, prefix: str, symbol: SymbolInfo, start: str, end: str) -> Path:
        safe = f"{prefix}_{symbol.baostock.replace('.', '_')}_{start}_{end}".replace("-", "")
        return self.cache_dir / f"{safe}.csv"

    def _dataset(
        self, bars: list[Bar], name: str, symbol: str, timeframe: str,
        source: str, file_path: Path,
    ) -> Dataset:
        dataset = dataset_from_bars(
            bars=bars, name=name, symbol=symbol, timeframe=timeframe,
            source=source, is_synthetic=False, file_path=str(file_path),
        )
        digest = hashlib.sha1(
            f"{source}|{symbol}|{timeframe}|{bars[0].timestamp.isoformat()}|{bars[-1].timestamp.isoformat()}".encode("utf-8")
        ).hexdigest()[:16]
        dataset.id = f"ds_{digest}"
        return dataset

    def _fetch_quan_bundle(
        self, symbol: SymbolInfo, start_text: str, end_text: str, include_intraday: bool,
    ) -> dict[str, Any]:
        """quan_parquet 只读历史数据 → 与旧链路一致的数据集 bundle。"""
        assert self.quan is not None
        name = self.quan.stock_name(symbol.standard) or symbol.standard
        daily = self.quan.load_series(symbol.standard, start=start_text, end=end_text)
        if not daily:
            raise ValueError(
                f"quan_parquet 在 {start_text}~{end_text} 没有 {symbol.standard} 的日线"
            )
        daily_path = self._cache_path("quan_daily", symbol, start_text, end_text)
        write_bars_csv(daily_path, daily)
        daily_dataset = self._dataset(
            daily, f"{name} {symbol.standard} 日线", symbol.standard, "daily",
            QUAN_DAILY_SOURCE, daily_path,
        )

        index_bars = self.quan.load_index(
            symbol.index_baostock, max_rows=4000, start=start_text, end=end_text,
        )
        if not index_bars:
            raise ValueError(
                f"quan_parquet 缺少指数 {symbol.index_baostock} 的日线；"
                "请先运行 backend/scripts/quan_migrate.py index 回填 399006/000688"
            )
        index_path = self._cache_path("quan_index", symbol, start_text, end_text)
        write_bars_csv(index_path, index_bars)
        index_dataset = self._dataset(
            index_bars, f"对应指数 {symbol.index_baostock} 日线", symbol.index_baostock,
            "daily", QUAN_INDEX_SOURCE, index_path,
        )

        intraday_dataset: Dataset | None = None
        intraday_source: str | None = None
        if include_intraday:
            minutes = self.quan.load_minute5(symbol.standard, max_rows=2400)
            source = QUAN_DAILY_SOURCE
            path_prefix = "quan_m5"
            if not minutes:
                # 服务器上 quan 的 minute5 可能尚未同步；5 分钟数据回落到腾讯 m5，
                # 仍带真实来源标记，不伪称来自 quan。
                try:
                    minutes, _ = self.fetch_tencent_intraday(symbol.standard)
                    source = "tencent"
                    path_prefix = "tencent_m5"
                except (ValueError, requests.RequestException):
                    minutes = []
            if minutes:
                intraday_path = self._cache_path(path_prefix, symbol, start_text, end_text)
                write_bars_csv(intraday_path, minutes)
                intraday_dataset = self._dataset(
                    minutes, f"{name} {symbol.standard} 5分钟", symbol.standard,
                    "5m", source, intraday_path,
                )
                intraday_source = source

        quote, _ = self.fetch_quote(symbol.standard)
        return {
            "symbol": symbol.standard, "code": symbol.code,
            "exchange": symbol.exchange, "board": symbol.board,
            "name": quote.get("name") or name,
            "index_symbol": symbol.index_baostock, "daily": daily_dataset,
            "index": index_dataset, "intraday": intraday_dataset, "quote": quote,
            "source_info": {
                "daily_source": QUAN_DAILY_SOURCE,
                "intraday_source": intraday_source,
                "index_source": QUAN_INDEX_SOURCE,
                "realtime_interval_seconds": REALTIME_INTERVAL_SECONDS,
            },
        }

    def fetch_history_bundle(
        self, raw_symbol: str, start_date: str | None = None, end_date: str | None = None,
        include_intraday: bool = True,
    ) -> dict[str, Any]:
        symbol = normalize_symbol(raw_symbol)
        end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else datetime.now().date()
        start = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else end - timedelta(days=365)
        if start > end:
            raise ValueError("开始日期不能晚于结束日期")
        start_text = start.isoformat()
        end_text = end.isoformat()

        if self.quan is not None:
            return self._fetch_quan_bundle(symbol, start_text, end_text, include_intraday)

        quote, _ = self.fetch_quote(symbol.standard)
        daily = self.fetch_baostock_daily(symbol.standard, start_text, end_text)
        index, index_source = self.fetch_index_daily(symbol.standard, start_text, end_text)

        daily_path = self._cache_path("baostock_daily", symbol, start_text, end_text)
        write_bars_csv(daily_path, daily)
        daily_dataset = self._dataset(
            daily, f"{quote.get('name', symbol.standard)} {symbol.standard} 日线",
            symbol.standard, "daily", "baostock", daily_path,
        )
        index_path = self._cache_path("baostock_index", symbol, start_text, end_text)
        write_bars_csv(index_path, index)
        index_dataset = self._dataset(
            index, f"对应指数 {symbol.index_baostock} 日线", symbol.index_baostock,
            "daily", index_source, index_path,
        )

        intraday_dataset: Dataset | None = None
        if include_intraday:
            minutes, _ = self.fetch_tencent_intraday(symbol.standard, count=TENCENT_INTRADAY_MAX_BARS)
            intraday_path = self._cache_path("tencent_m5", symbol, start_text, end_text)
            write_bars_csv(intraday_path, minutes)
            intraday_dataset = self._dataset(
                minutes, f"{quote.get('name', symbol.standard)} 腾讯5分钟",
                symbol.standard, INTRADAY_TIMEFRAME, "tencent", intraday_path,
            )

        return {
            "symbol": symbol.standard, "code": symbol.code,
            "exchange": symbol.exchange, "board": symbol.board,
            "name": quote.get("name", symbol.standard),
            "index_symbol": symbol.index_baostock, "daily": daily_dataset,
            "index": index_dataset, "intraday": intraday_dataset, "quote": quote,
            "source_info": {
                "daily_source": "baostock", "intraday_source": "tencent",
                "index_source": index_source,
                "realtime_interval_seconds": REALTIME_INTERVAL_SECONDS,
            },
        }
