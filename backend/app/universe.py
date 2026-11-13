"""全市场（默认双创）日线缓存与增量刷新。

本模块是选股漏斗的数据底座：
- ``UniverseStore``：SQLite 缓存（代码表/日线/指数/财务/元信息），WAL + 进程内锁。
- ``UniverseBootstrap``：一次 ``baostock.query_stock_basic()`` 抓全市场元数据，只落双创。
- ``UniverseRefresher``：后台线程增量回补日线（一次 login 复用全部查询），带进度。

约定：所有代码均使用标准代码 ``sh.688200`` / ``sz.300855``；日期一律 ``YYYY-MM-DD``。
数据源限制见 ``.workbuddy/screener-contract.md`` §1。
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .domain import Bar, money, price, utc_now
from .market import MarketDataService, normalize_symbol

BOARD_GEM = "创业板"
BOARD_STAR = "科创板"
SUPPORTED_BOARDS = (BOARD_GEM, BOARD_STAR)

# L0 大盘门使用的板块指数
BOARD_INDEX = {BOARD_GEM: "sz.399006", BOARD_STAR: "sh.000688"}

# 一次 baostock login 内逐票查询用到的字段（前复权，adjustflag=2）
DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn"
INDEX_FIELDS = "date,code,open,high,low,close,preclose,volume,amount"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS universe_codes (
  code TEXT PRIMARY KEY,
  board TEXT NOT NULL,
  name TEXT NOT NULL,
  ipo_date TEXT NOT NULL DEFAULT '',
  out_date TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '1',
  is_st INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS universe_daily (
  code TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, prev_close REAL,
  volume REAL, amount REAL, turnover REAL,
  PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_universe_daily_date ON universe_daily(date);
CREATE TABLE IF NOT EXISTS universe_index (
  code TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, prev_close REAL,
  volume REAL, amount REAL,
  PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS universe_fundamentals (
  code TEXT PRIMARY KEY, checked_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS universe_meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def board_of(code: str) -> str:
    """由标准代码/纯代码推断板块。无法识别返回 "未知"。"""
    text = (code or "").strip().lower()
    exchange, number = (text.split(".", 1) + [""])[:2] if "." in text else ("", text)
    number = number.strip()
    if not re.fullmatch(r"\d{6}", number):
        return "未知"
    if number.startswith("900"):
        return "沪市B股"
    if number.startswith("200"):
        return "深市B股"
    if not exchange:
        exchange = "sh" if number.startswith("6") else "sz"
    if exchange == "sh":
        return BOARD_STAR if number.startswith(("688", "689")) else "沪市主板"
    return BOARD_GEM if number.startswith(("300", "301", "302")) else "深市主板"


def is_st_name(name: str) -> bool:
    """名称含 ST（含 *ST）。"""
    return "ST" in (name or "").upper()


def is_delisting_name(name: str) -> bool:
    """名称含“退”（退市整理期）。"""
    return "退" in (name or "")


def is_unprofitable_u_name(name: str) -> bool:
    """科创板未盈利后缀 -U（兼容全角连字符）。"""
    text = unicodedata.normalize("NFKC", name or "").strip().upper()
    return bool(re.search(r"[-‐‑‒–—―]U$", text))


def default_universe_db() -> Path:
    """默认落库路径：backend/data/universe.db；环境变量 WB_UNIVERSE_DB 优先。"""
    override = os.environ.get("WB_UNIVERSE_DB")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "data" / "universe.db"


def _to_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def _bars_from_baostock_rows(rows: Sequence[Sequence[str]], symbol: str) -> list[Bar]:
    """把 baostock 日线行（date,code,open,high,low,close,preclose,volume,amount,turn）转 Bar。

    与 ``MarketDataService.fetch_baostock_daily`` 的解析口径保持一致（前复权）。
    """
    bars: list[Bar] = []
    previous_close: float | None = None
    for row in rows:
        if len(row) < 9 or not row[0] or not row[5]:
            continue
        try:
            timestamp = _to_date(str(row[0]))
            open_ = float(row[2] or 0)
            high = float(row[3] or 0)
            low = float(row[4] or 0)
            close = float(row[5] or 0)
            volume = float(row[7] or 0)
            amount = float(row[8] or 0)
            turnover = float(row[9]) / 100.0 if len(row) > 9 and row[9] not in ("", None) else None
        except (TypeError, ValueError):
            continue
        if min(open_, high, low, close) <= 0:
            continue
        if amount <= 0 and volume > 0:
            amount = close * volume
        if previous_close is None:
            previous_close = float(row[6]) if len(row) > 6 and row[6] not in ("", None) else close
        bars.append(
            Bar(
                timestamp=timestamp,
                symbol=symbol,
                open=price(open_),
                high=price(high),
                low=price(low),
                close=price(close),
                volume=volume,
                amount=money(amount),
                timeframe="daily",
                turnover_rate=turnover,
                prev_close=price(previous_close),
            )
        )
        previous_close = close
    return bars


def parse_stock_basic_rows(rows: Sequence[Sequence[str]]) -> list["UniverseRow"]:
    """解析 ``query_stock_basic()`` 的原始行；只保留双创正常上市股票。"""
    result: list[UniverseRow] = []
    seen: set[str] = set()
    for row in rows:
        if len(row) < 6:
            continue
        code = str(row[0] or "").strip().lower()
        name = str(row[1] or "").strip()
        ipo_date = str(row[2] or "").strip()
        out_date = str(row[3] or "").strip()
        type_ = str(row[4] or "").strip()
        status = str(row[5] or "").strip() or "1"
        board = board_of(code)
        if board not in SUPPORTED_BOARDS:
            continue
        if type_ != "1" or status != "1" or out_date or not name or code in seen:
            continue
        seen.add(code)
        result.append(
            UniverseRow(
                code=code,
                board=board,
                name=name,
                ipo_date=ipo_date,
                out_date=out_date,
                status=status,
                is_st=is_st_name(name),
            )
        )
    return result


# ---------------------------------------------------------------------------
# 行模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UniverseRow:
    code: str
    board: str
    name: str
    ipo_date: str = ""
    out_date: str = ""
    status: str = "1"
    is_st: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# SQLite 缓存
# ---------------------------------------------------------------------------


class UniverseStore:
    """双创日线本地缓存。

    每次操作独立打开连接（WAL 允许与后台回补线程/其他进程并发读），
    进程内用 ``threading.RLock`` 串行化写操作。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(self.path)
        self._lock = threading.RLock()
        self._initialize()

    # -- 连接管理 ---------------------------------------------------------

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=60)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.executescript(_SCHEMA)

    # -- 覆盖度 -----------------------------------------------------------

    def coverage(self) -> dict:
        with self._lock, self._connection() as connection:
            codes = int(connection.execute("SELECT COUNT(*) FROM universe_codes").fetchone()[0])
            rows = int(connection.execute("SELECT COUNT(*) FROM universe_daily").fetchone()[0])
            span = connection.execute("SELECT MIN(date), MAX(date) FROM universe_daily").fetchone()
            start, end = (span[0], span[1]) if span else (None, None)
            fundamentals = int(
                connection.execute("SELECT COUNT(*) FROM universe_fundamentals").fetchone()[0]
            )
            updated = connection.execute(
                "SELECT value FROM universe_meta WHERE key='updated_at'"
            ).fetchone()
            board_counts = {board: 0 for board in SUPPORTED_BOARDS}
            for row in connection.execute(
                "SELECT board, COUNT(*) FROM universe_codes GROUP BY board"
            ):
                board_counts[str(row[0])] = int(row[1])
            missing = int(
                connection.execute(
                    "SELECT COUNT(*) FROM universe_codes c "
                    "LEFT JOIN universe_daily d ON d.code = c.code WHERE d.code IS NULL"
                ).fetchone()[0]
            )
            stale = 0
            if end:
                stale = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM ("
                        "  SELECT c.code AS code, MAX(d.date) AS last_date "
                        "  FROM universe_codes c JOIN universe_daily d ON d.code = c.code "
                        "  GROUP BY c.code"
                        ") WHERE last_date < ?",
                        (end,),
                    ).fetchone()[0]
                )
        return {
            "codes": codes,
            "rows": rows,
            "start": start,
            "end": end,
            "boards": board_counts,
            "fundamentals": fundamentals,
            "updated_at": updated["value"] if updated else None,
            "missing_codes": missing,
            "stale_codes": stale,
        }

    # -- 代码表 -----------------------------------------------------------

    def list_codes(self, boards: Sequence[str] | None = None) -> list[UniverseRow]:
        sql = (
            "SELECT code, board, name, ipo_date, out_date, status, is_st "
            "FROM universe_codes"
        )
        params: list[Any] = []
        if boards:
            placeholders = ",".join("?" for _ in boards)
            sql += f" WHERE board IN ({placeholders})"
            params.extend(boards)
        sql += " ORDER BY code"
        with self._lock, self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_from_sql(row) for row in rows]

    @staticmethod
    def _row_from_sql(row: sqlite3.Row) -> UniverseRow:
        return UniverseRow(
            code=str(row["code"]),
            board=str(row["board"]),
            name=str(row["name"]),
            ipo_date=str(row["ipo_date"] or ""),
            out_date=str(row["out_date"] or ""),
            status=str(row["status"] or "1"),
            is_st=bool(row["is_st"]),
        )

    def get_code(self, code: str) -> UniverseRow | None:
        text = str(code or "").strip().lower()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT code, board, name, ipo_date, out_date, status, is_st "
                "FROM universe_codes WHERE code=?",
                (text,),
            ).fetchone()
            if row is None and "." not in text:
                try:
                    standard = normalize_symbol(text).standard
                except ValueError:
                    standard = ""
                if standard:
                    row = connection.execute(
                        "SELECT code, board, name, ipo_date, out_date, status, is_st "
                        "FROM universe_codes WHERE code=?",
                        (standard,),
                    ).fetchone()
        return self._row_from_sql(row) if row else None

    def upsert_rows(self, rows: Sequence[UniverseRow]) -> int:
        if not rows:
            return 0
        now = utc_now()
        payload = [
            (
                row.code,
                row.board,
                row.name,
                row.ipo_date or "",
                row.out_date or "",
                row.status or "1",
                1 if row.is_st else 0,
                now,
            )
            for row in rows
        ]
        with self._lock, self._connection() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO universe_codes"
                "(code,board,name,ipo_date,out_date,status,is_st,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                payload,
            )
        return len(payload)

    # -- 日线 -------------------------------------------------------------

    def upsert_daily(self, code: str, bars: Sequence[Bar]) -> int:
        if not bars:
            return 0
        payload = [
            (
                str(code),
                bar.date,
                price(bar.open),
                price(bar.high),
                price(bar.low),
                price(bar.close),
                price(bar.prev_close) if bar.prev_close is not None else None,
                float(bar.volume or 0),
                money(bar.amount or 0),
                round(float(bar.turnover_rate), 8) if bar.turnover_rate is not None else None,
            )
            for bar in bars
        ]
        with self._lock, self._connection() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO universe_daily"
                "(code,date,open,high,low,close,prev_close,volume,amount,turnover) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
        return len(payload)

    def last_date(self, code: str) -> str | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT MAX(date) FROM universe_daily WHERE code=?", (str(code),)
            ).fetchone()
        return row[0] if row and row[0] else None

    def load_history(self, codes: Sequence[str], max_rows: int = 260) -> dict[str, list[Bar]]:
        unique = [str(code) for code in dict.fromkeys(codes or [])]
        result: dict[str, list[Bar]] = {code: [] for code in unique}
        if not unique:
            return result
        limit = max(1, int(max_rows))
        with self._lock, self._connection() as connection:
            for start in range(0, len(unique), 400):
                chunk = unique[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    "SELECT code, date, open, high, low, close, prev_close, volume, amount, turnover "
                    "FROM ("
                    "  SELECT * , ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn"
                    f"  FROM universe_daily WHERE code IN ({placeholders})"
                    ") WHERE rn <= ? ORDER BY code, date",
                    (*chunk, limit),
                ).fetchall()
                for row in rows:
                    result[str(row["code"])].append(self._bar_from_payload(row, str(row["code"])))
        return result

    def list_trade_dates(
        self, codes: Sequence[str], *, start: str | None = None, end: str | None = None,
    ) -> list[str]:
        """窗口内这些代码出现过日线的 distinct 交易日（升序）。

        与 QuanStore.list_trade_dates 同名同语义：漏斗轮动回测构建交易日时间线用，
        只查 distinct 日期，不构建 Bar 对象，避免全池日线驻留内存。
        """
        unique = [str(code) for code in dict.fromkeys(codes or [])]
        if not unique:
            return []
        placeholders = ",".join("?" for _ in unique)
        sql = f"SELECT DISTINCT date FROM universe_daily WHERE code IN ({placeholders})"
        params: list[Any] = list(unique)
        if start:
            sql += " AND date >= ?"
            params.append(str(start)[:10])
        if end:
            sql += " AND date <= ?"
            params.append(str(end)[:10])
        sql += " ORDER BY date"
        with self._lock, self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [str(row["date"]) for row in rows if row["date"]]

    def count_codes_with_daily(self, codes: Sequence[str]) -> int:
        """这些代码里在日线表中至少有一根 K 线的数量（仅计数，不驻留 Bar）。"""
        unique = [str(code) for code in dict.fromkeys(codes or [])]
        if not unique:
            return 0
        placeholders = ",".join("?" for _ in unique)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                f"SELECT COUNT(DISTINCT code) FROM universe_daily WHERE code IN ({placeholders})",
                unique,
            ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _bar_from_payload(row: Any, code: str) -> Bar:
        return Bar(
            timestamp=_to_date(str(row["date"])),
            symbol=code,
            open=float(row["open"] or 0),
            high=float(row["high"] or 0),
            low=float(row["low"] or 0),
            close=float(row["close"] or 0),
            volume=float(row["volume"] or 0),
            amount=float(row["amount"] or 0),
            timeframe="daily",
            turnover_rate=float(row["turnover"]) if row["turnover"] is not None else None,
            prev_close=float(row["prev_close"]) if row["prev_close"] is not None else None,
        )

    def latest_trade_date(self) -> str | None:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT MAX(date) FROM universe_daily").fetchone()
        return row[0] if row and row[0] else None

    # -- 指数 -------------------------------------------------------------

    def upsert_index(self, index_code: str, bars: Sequence[Bar]) -> int:
        if not bars:
            return 0
        payload = [
            (
                str(index_code),
                bar.date,
                price(bar.open),
                price(bar.high),
                price(bar.low),
                price(bar.close),
                price(bar.prev_close) if bar.prev_close is not None else None,
                float(bar.volume or 0),
                money(bar.amount or 0),
            )
            for bar in bars
        ]
        with self._lock, self._connection() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO universe_index"
                "(code,date,open,high,low,close,prev_close,volume,amount) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                payload,
            )
        return len(payload)

    def load_index(self, index_code: str, max_rows: int = 260) -> list[Bar]:
        limit = max(1, int(max_rows))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT date, open, high, low, close, prev_close, volume, amount "
                "FROM ("
                "  SELECT *, ROW_NUMBER() OVER (ORDER BY date DESC) AS rn"
                "  FROM universe_index WHERE code=?"
                ") WHERE rn <= ? ORDER BY date",
                (str(index_code), limit),
            ).fetchall()
        return [
            Bar(
                timestamp=_to_date(str(row["date"])),
                symbol=str(index_code),
                open=float(row["open"] or 0),
                high=float(row["high"] or 0),
                low=float(row["low"] or 0),
                close=float(row["close"] or 0),
                volume=float(row["volume"] or 0),
                amount=float(row["amount"] or 0),
                timeframe="daily",
                prev_close=float(row["prev_close"]) if row["prev_close"] is not None else None,
            )
            for row in rows
        ]

    # -- 财务缓存 ---------------------------------------------------------

    def get_fundamentals(self, code: str) -> dict | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT checked_at, payload FROM universe_fundamentals WHERE code=?",
                (str(code),),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        payload.setdefault("checked_at", row["checked_at"])
        return payload

    def save_fundamentals(self, code: str, payload: dict) -> None:
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO universe_fundamentals(code,checked_at,payload) "
                "VALUES(?,?,?)",
                (str(code), utc_now(), text),
            )

    # -- 元信息 -----------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM universe_meta WHERE key=?", (str(key),)
            ).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO universe_meta(key,value) VALUES(?,?)",
                (str(key), str(value)),
            )


# ---------------------------------------------------------------------------
# bootstrap：一次 query_stock_basic 抓全市场元数据
# ---------------------------------------------------------------------------


def _query_stock_basic_rows() -> list[list[str]]:
    """baostock 全市场代码表（单次约 65s）。禁止使用 query_all_stock。"""
    try:
        import baostock as bs
    except ImportError as exc:  # pragma: no cover - 环境已装 baostock
        raise ValueError("baostock 未安装，无法 bootstrap 元数据") from exc
    login = bs.login()
    if getattr(login, "error_code", "0") != "0":
        raise ValueError(f"baostock 登录失败: {getattr(login, 'error_msg', '')}")
    try:
        result_set = bs.query_stock_basic()
        if getattr(result_set, "error_code", "0") != "0":
            raise ValueError(f"baostock 代码表查询失败: {getattr(result_set, 'error_msg', '')}")
        rows: list[list[str]] = []
        while result_set.next():
            rows.append(list(result_set.get_row_data()))
    finally:
        bs.logout()
    if not rows:
        raise ValueError("baostock query_stock_basic 没有返回数据")
    return rows


class UniverseBootstrap:
    """首次/过期时的元数据引导。默认 24h 内不重复拉取。"""

    def __init__(self, store: UniverseStore, *, max_age_hours: float = 24.0) -> None:
        self.store = store
        self.max_age_hours = float(max_age_hours)

    def age_hours(self) -> float | None:
        value = self.store.get_meta("bootstrap_at")
        if not value:
            return None
        try:
            return (datetime.now() - datetime.fromisoformat(value)).total_seconds() / 3600.0
        except ValueError:
            return None

    def needs_bootstrap(self) -> bool:
        if not self.store.list_codes():
            return True
        age = self.age_hours()
        return age is None or age >= self.max_age_hours

    def run(self, *, force: bool = False) -> dict:
        if not force:
            age = self.age_hours()
            if self.store.list_codes() and age is not None and age < self.max_age_hours:
                return {"rows": len(self.store.list_codes()), "skipped": True, "at": self.store.get_meta("bootstrap_at")}
        rows = parse_stock_basic_rows(_query_stock_basic_rows())
        count = self.store.upsert_rows(rows)
        now = utc_now()
        self.store.set_meta("bootstrap_at", now)
        self.store.set_meta("bootstrap_rows", str(count))
        return {"rows": count, "skipped": False, "at": now}


# ---------------------------------------------------------------------------
# 后台增量刷新
# ---------------------------------------------------------------------------


@dataclass
class RefreshJob:
    running: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    done: int = 0
    total: int = 0
    current: str = ""
    message: str = ""
    fetched_rows: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class UniverseRefresher:
    """后台线程回补日线；同一实例同时只允许一个任务。"""

    def __init__(
        self,
        store: UniverseStore,
        market: MarketDataService,
        *,
        sleep_seconds: float = 0.02,
    ) -> None:
        self.store = store
        self.market = market
        self.sleep_seconds = max(0.0, float(sleep_seconds))
        self._lock = threading.RLock()
        self._running = False
        self._job = RefreshJob()
        self._thread: threading.Thread | None = None
        # baostock 会话：一个 login 复用全部查询；长会话会被服务端掐断/降速，
        # 因此按失败重连 + 定时轮换，而不是每票 login/logout。
        # 实测：同一会话连续查询几分钟后会从 ~1.5s/票 退化到 5~40s/票，
        # 每 120s 主动重登一次能显著平滑吞吐（代价只是每次 login ~0.2s）。
        self._bs: Any = None
        self._session_started: float | None = None
        self.session_max_age_seconds = 120.0
        # 单次 recv 的 socket 超时；卡住时抛 socket.timeout 走重试/跳过，避免整轮僵死。
        self.socket_timeout_seconds = 20.0

    # -- 对外接口 ---------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            running = self._running
            job = self._job.to_dict()
        return {"running": running, "job": job, "coverage": self.store.coverage()}

    def start(
        self,
        *,
        boards: Sequence[str] | None = None,
        codes: Sequence[str] | None = None,
        lookback_days: int = 400,
        force: bool = False,
        bootstrap: bool = True,
    ) -> dict:
        # 代码非法要在起线程前抛 ValueError，让 API 返回 400。
        normalized = self._normalize_codes(codes) if codes else None
        with self._lock:
            if self._running:
                raise ValueError("刷新任务已在运行")
            self._running = True
            self._job = RefreshJob(
                running=True,
                started_at=utc_now(),
                message="准备刷新…",
            )
            job_snapshot = self._job.to_dict()
            self._thread = threading.Thread(
                target=self._thread_main,
                kwargs={
                    "boards": boards,
                    "codes": normalized,
                    "lookback_days": int(lookback_days),
                    "force": bool(force),
                    "bootstrap": bool(bootstrap),
                },
                daemon=True,
                name="universe-refresh",
            )
            self._thread.start()
        return {"status": "started", "job": job_snapshot}

    def run_once(
        self,
        *,
        boards: Sequence[str] | None = None,
        codes: Sequence[str] | None = None,
        lookback_days: int = 400,
        force: bool = False,
        bootstrap: bool = True,
        progress: Callable[[dict], None] | None = None,
        limit: int | None = None,
    ) -> dict:
        normalized = self._normalize_codes(codes) if codes else None
        with self._lock:
            if self._running:
                raise ValueError("刷新任务已在运行")
            self._running = True
            self._job = RefreshJob(
                running=True,
                started_at=utc_now(),
                message="准备刷新…",
            )
        try:
            return self._execute(
                boards=boards,
                codes=normalized,
                lookback_days=int(lookback_days),
                force=bool(force),
                bootstrap=bool(bootstrap),
                progress=progress,
                limit=limit,
            )
        finally:
            with self._lock:
                self._running = False
                self._job.running = False
                self._job.finished_at = utc_now()

    # -- 内部实现 ---------------------------------------------------------

    def _thread_main(self, **kwargs: Any) -> None:
        try:
            self._execute(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 后台线程必须吞掉异常并记录
            self._record_error(f"刷新失败: {exc}")
            with self._lock:
                self._job.message = f"刷新失败: {exc}"
        finally:
            with self._lock:
                self._running = False
                self._job.running = False
                self._job.current = ""
                self._job.finished_at = utc_now()
                if not self._job.message:
                    self._job.message = "刷新结束"

    def _normalize_codes(self, codes: Sequence[str]) -> list[str]:
        result: list[str] = []
        for raw in codes:
            if raw is None or str(raw).strip() == "":
                continue
            info = normalize_symbol(str(raw))
            if info.standard not in result:
                result.append(info.standard)
        if not result:
            raise ValueError("codes 不能为空")
        return result

    def _record_error(self, message: str) -> None:
        with self._lock:
            if len(self._job.errors) < 20:
                self._job.errors.append(message)

    def _snapshot(self) -> dict:
        with self._lock:
            return self._job.to_dict()

    def _resolve_targets(
        self,
        boards: Sequence[str] | None,
        codes: list[str] | None,
        bootstrap: bool,
        limit: int | None = None,
    ) -> list[UniverseRow]:
        if codes:
            targets: list[UniverseRow] = []
            missing: list[UniverseRow] = []
            for code in codes:
                row = self.store.get_code(code)
                if row is None:
                    row = UniverseRow(code=code, board=board_of(code), name=code)
                    missing.append(row)
                targets.append(row)
            if missing:
                # 保底元数据，避免 --codes 小样本回补后 screener 看不到这些票；
                # 之后 bootstrap 会用真实名称/ipo_date 覆盖。
                self.store.upsert_rows(missing)
            return targets[:limit] if limit else targets

        board_filter = [b for b in (boards or SUPPORTED_BOARDS) if b in SUPPORTED_BOARDS]
        if not board_filter:
            raise ValueError(f"不支持的板块: {', '.join(boards or [])}")
        if bootstrap and not self.store.list_codes():
            UniverseBootstrap(self.store).run()
        targets = self.store.list_codes(board_filter)
        if not targets and bootstrap:
            UniverseBootstrap(self.store).run()
            targets = self.store.list_codes(board_filter)
        if not targets:
            raise ValueError("没有可刷新的标的，请先 bootstrap 元数据")
        return targets[:limit] if limit else targets

    def _execute(
        self,
        *,
        boards: Sequence[str] | None,
        codes: list[str] | None,
        lookback_days: int,
        force: bool,
        bootstrap: bool,
        progress: Callable[[dict], None] | None = None,
        limit: int | None = None,
    ) -> dict:
        started = time.perf_counter()
        if bootstrap and not self.store.list_codes():
            # 契约：元数据为空且 bootstrap=True，先拉一次全市场代码表（约 65s）。
            # 显式 --codes 场景下 bootstrap 失败不阻塞（仍可按代码回补并建兜底元数据）。
            if codes:
                try:
                    UniverseBootstrap(self.store).run()
                except Exception as exc:  # noqa: BLE001
                    self._record_error(f"bootstrap 失败，按显式代码继续: {exc}")
            else:
                UniverseBootstrap(self.store).run()
        targets = self._resolve_targets(boards, codes, bootstrap, limit=limit)
        latest_trade = self.store.latest_trade_date()
        today = date.today()
        lookback = max(60, int(lookback_days))

        with self._lock:
            self._job.total = len(targets)
            self._job.message = f"开始刷新 {len(targets)} 票（{lookback} 天回看）"

        try:
            import baostock as bs
        except ImportError as exc:  # pragma: no cover
            raise ValueError("baostock 未安装，无法刷新日线") from exc

        # baostock 的 socket 没有读超时：服务端偶尔会卡住单次 recv，
        # 没有超时会表现为“单票卡死几分钟”。给全局 socket 设一个读超时，
        # 卡住时抛 socket.timeout 走重试/跳过，保证整体回补能继续推进。
        previous_socket_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.socket_timeout_seconds)
        self._bs = bs
        try:
            self._login()
        except Exception:
            self._bs = None
            socket.setdefaulttimeout(previous_socket_timeout)
            raise

        try:
            for index, row in enumerate(targets, start=1):
                code = row.code
                with self._lock:
                    self._job.current = code
                    self._job.done = index - 1
                last = self.store.last_date(code)
                if not force and last and latest_trade and last >= latest_trade:
                    with self._lock:
                        self._job.skipped += 1
                        self._job.done = index
                    self._notify(progress)
                    continue

                if not force and last:
                    start_date = (_to_date(last) + timedelta(days=1)).date()
                else:
                    start_date = today - timedelta(days=lookback)
                if start_date > today:
                    with self._lock:
                        self._job.skipped += 1
                        self._job.done = index
                    self._notify(progress)
                    continue

                try:
                    bars = self._fetch_with_retry(code, start_date.isoformat(), today.isoformat())
                except Exception as exc:  # noqa: BLE001 - 单票失败不终止全量
                    self._record_error(f"{code}: {exc}")
                    with self._lock:
                        self._job.done = index
                    self._notify(progress)
                    if self.sleep_seconds:
                        time.sleep(self.sleep_seconds)
                    continue

                if bars:
                    written = self.store.upsert_daily(code, bars)
                    with self._lock:
                        self._job.fetched_rows += written
                with self._lock:
                    self._job.done = index
                self._notify(progress)
                if self.sleep_seconds:
                    time.sleep(self.sleep_seconds)
        finally:
            try:
                if self._bs is not None:
                    self._bs.logout()
            except Exception:  # noqa: BLE001 - 登出失败不影响已完成数据
                pass
            finally:
                self._bs = None
                self._session_started = None
                socket.setdefaulttimeout(previous_socket_timeout)

        # 指数日线（L0 用）；一次刷新两个指数，失败只记 warning 不中断。
        index_start = (today - timedelta(days=max(lookback, 260))).isoformat()
        for index_code in BOARD_INDEX.values():
            try:
                bars, _source = self.market.fetch_index_daily_by_code(
                    index_code, index_start, today.isoformat()
                )
                self.store.upsert_index(index_code, bars)
            except Exception as exc:  # noqa: BLE001
                self._record_error(f"指数 {index_code}: {exc}")

        finished = utc_now()
        summary = {
            "finished_at": finished,
            "codes": len(targets),
            "fetched_rows": self._job.fetched_rows,
            "skipped": self._job.skipped,
            "errors": len(self._job.errors),
        }
        self.store.set_meta("updated_at", finished)
        self.store.set_meta("last_refresh", json.dumps(summary, ensure_ascii=False))
        with self._lock:
            self._job.message = (
                f"完成：{self._job.total} 票，新增 {self._job.fetched_rows} 行，"
                f"跳过 {self._job.skipped}，错误 {len(self._job.errors)}，"
                f"耗时 {time.perf_counter() - started:.0f}s"
            )
        return {
            "fetched_rows": self._job.fetched_rows,
            "skipped": self._job.skipped,
            "errors": list(self._job.errors),
            "coverage": self.store.coverage(),
        }

    def _notify(self, progress: Callable[[dict], None] | None) -> None:
        if progress is None:
            return
        try:
            progress(self._snapshot())
        except Exception:  # noqa: BLE001 - 进度回调异常不影响刷新
            pass

    # -- baostock 会话 ----------------------------------------------------

    def _login(self) -> None:
        if self._bs is None:
            import baostock as bs

            self._bs = bs
        login = self._bs.login()
        if getattr(login, "error_code", "0") != "0":
            raise ValueError(f"baostock 登录失败: {getattr(login, 'error_msg', '')}")
        self._session_started = time.monotonic()

    def _ensure_session(self) -> None:
        if self._session_started is None:
            self._login()
            return
        if time.monotonic() - self._session_started >= self.session_max_age_seconds:
            self._relogin()

    def _relogin(self) -> None:
        if self._bs is not None:
            try:
                self._bs.logout()
            except Exception:  # noqa: BLE001
                pass
        self._session_started = None
        self._login()

    def _fetch_with_retry(self, code: str, start_date: str, end_date: str) -> list[Bar]:
        """单票日线：失败重试 1 次，重试前重建会话（长会话被服务端断开时自愈）。"""
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                self._ensure_session()
                assert self._bs is not None
                result_set = self._bs.query_history_k_data_plus(
                    code,
                    DAILY_FIELDS,
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag="2",
                )
                if getattr(result_set, "error_code", "0") != "0":
                    raise ValueError(
                        f"baostock 日线查询失败: {getattr(result_set, 'error_msg', '')}"
                    )
                rows: list[list[str]] = []
                while result_set.next():
                    rows.append(list(result_set.get_row_data()))
                return _bars_from_baostock_rows(rows, code)
            except Exception as exc:  # noqa: BLE001 - 重试一次
                last_error = exc
                if attempt == 0:
                    try:
                        self._relogin()
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(0.5)
        raise last_error if last_error else ValueError(f"{code} 日线查询失败")
