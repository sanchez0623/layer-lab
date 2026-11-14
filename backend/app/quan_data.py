"""quan_quant parquet 数据源适配层（只读）。

默认读取 ``D:\\Sanchez\\AI\\TraeProjects\\quan_quant\\data``（环境变量
``WB_QUAN_DATA_DIR`` 覆盖；设为空串表示完全不启用、回落到旧的 baostock/universe.db 链路）。

目录约定（由 quan_quant 数据更新器维护，本服务只读不写）::

    daily.parquet            code,date(str YYYY-MM-DD),open,high,low,close,volume,amount（不复权原始价）
    adj_factor.parquet       code,date,adj_factor（后复权累计因子，越早越接近 1）
    stock_basic.parquet      code,name,st,list_date,delisted（code 为 6 位纯代码）
    index_daily.parquet      index_key,date,open,high,low,close,volume,amount（index_key 为 6 位纯代码）
    trade_calendar.parquet   date,is_open
    minute5/{code}.parquet   code,date(str "YYYY-MM-DD HH:mm"),open,high,low,close,volume,amount

口径对齐（与原 baostock 链路保持一致，避免策略口径漂移）：

- 对外代码一律标准代码 ``sh.688200`` / ``sz.300855``；parquet 内部是 6 位纯代码。
- 价格默认**前复权**：``原始价 × 当日 adj_factor ÷ 最新（区间末）adj_factor``，
  因此最新一根的复权价等于原始价（与 baostock adjustflag=2 的口径一致）。
- ``volume`` / ``amount`` 保持 quan 原始口径（股 / 元），不复权。
- quan 数据没有换手率字段，``turnover_rate`` 统一为 ``None``（只影响展示，不参与漏斗判定）。
- 指数不做复权（索引即原始点位）。

本模块不负责刷新数据：quan_quant 自己的更新器写 parquet，本服务只读；
``QuanRefresher`` 只做状态展示，``start()`` 不会触发任何网络拉取。
"""

from __future__ import annotations

import bisect
import os
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .domain import Bar, money, price, utc_now
from .universe import (
    SUPPORTED_BOARDS,
    UniverseRow,
    board_of,
    is_st_name,
)

# 默认数据目录：与 quan_quant 项目同机的约定路径；WB_QUAN_DATA_DIR 可覆盖。
DEFAULT_QUAN_DATA_DIR = r"D:\Sanchez\AI\TraeProjects\quan_quant\data"

_EXCHANGE_PREFIX = re.compile(r"^(sh|sz|bj)\.?(\d{6})$")
_PURE_CODE = re.compile(r"^\d{6}$")
_DAILY_COLUMNS = ("open", "high", "low", "close")
_COVERAGE_TTL_SECONDS = 60.0
# 只取「最近 N 根」时，额外多取的安全交易日数：覆盖停牌/新股不足、节假日等边界，
# 保证窗口内仍能按代码 tail 出完整 N 根，同时避免把 1994 年以来的全量日线读进内存。
_WINDOW_BUFFER = 40


# ---------------------------------------------------------------------------
# 代码映射 / 目录解析
# ---------------------------------------------------------------------------


def infer_exchange(code: str) -> str:
    """6 位纯代码 → 交易所前缀（与原 market._infer_exchange 同口径）。"""
    text = str(code or "").strip()
    if text.startswith("6"):
        return "sh"
    if text.startswith(("0", "2", "3")):
        return "sz"
    if text.startswith(("4", "8")):
        return "bj"
    return ""


def pure_code(raw: str) -> str:
    """任意输入（688200 / sh.688200 / sh688200 / 688200.SH）→ 6 位纯代码。"""
    text = unicodedata.normalize("NFKC", str(raw or "")).strip().lower().replace(" ", "")
    match = _EXCHANGE_PREFIX.fullmatch(text)
    if match:
        return match.group(2)
    if "." in text:
        prefix, _, tail = text.partition(".")
        if _PURE_CODE.fullmatch(prefix):
            return prefix
        if _PURE_CODE.fullmatch(tail):
            return tail
    text = text.strip(".")
    if _PURE_CODE.fullmatch(text):
        return text
    raise ValueError(f"标的代码不合法: {raw}")


def standard_code(raw: str) -> str:
    """任意输入 → 标准代码 sh.688200 / sz.300855。"""
    code = pure_code(raw)
    exchange = infer_exchange(code)
    if not exchange:
        raise ValueError(f"无法识别代码 {code} 的交易所")
    return f"{exchange}.{code}"


def quan_data_dir() -> Path | None:
    """解析 quan 数据目录。

    - 环境变量 ``WB_QUAN_DATA_DIR`` 显式设置：空串 → 返回 None（显式关闭 quan 源）；
    - 未设置：使用 ``DEFAULT_QUAN_DATA_DIR``；
    - 目录不存在或缺少 ``daily.parquet`` 时返回 None（由调用方回落旧链路）。
    """
    if "WB_QUAN_DATA_DIR" in os.environ:
        raw = str(os.environ.get("WB_QUAN_DATA_DIR") or "").strip()
        if not raw:
            return None
        candidate = Path(raw)
    else:
        candidate = Path(DEFAULT_QUAN_DATA_DIR)
    if not (candidate / "daily.parquet").exists():
        return None
    return candidate


# ---------------------------------------------------------------------------
# 只读 parquet store（接口对齐 universe.UniverseStore 中 screener/backtest 用到的部分）
# ---------------------------------------------------------------------------


class QuanStore:
    """quan_quant parquet 只读数据层。

    与 ``UniverseStore`` 的公共接口保持一致（``list_codes`` / ``get_code`` /
    ``load_history`` / ``load_index`` / ``latest_trade_date`` / ``coverage`` /
    ``get_fundamentals`` / ``save_fundamentals``），供选股漏斗与漏斗轮动回测复用。

    ``cache_db`` 只存放财务深筛缓存（quan 数据没有财务字段），不存任何行情。
    """

    provider = "quan_parquet"

    def __init__(self, data_dir: str | Path, *, cache_db: str | Path | None = None) -> None:
        self._pl = _import_polars()
        self.path = Path(data_dir)
        self.daily_path = self.path / "daily.parquet"
        if not self.daily_path.exists():
            raise ValueError(f"quan 数据目录缺少 daily.parquet: {self.path}")
        self.adj_path = self.path / "adj_factor.parquet"
        self.basic_path = self.path / "stock_basic.parquet"
        self.index_path = self.path / "index_daily.parquet"
        self.calendar_path = self.path / "trade_calendar.parquet"
        self.minute5_dir = self.path / "minute5"
        # 日线换手率 parquet 多候选路径：优先可写卷 /data/（服务器 /data/quan/history 只读，
        # turn parquet 由部署放到可写卷），回退原路径。load_minute5/_daily_turnover_map 自动读取。
        self._turnover_candidates = [
            Path("/data/turnover_daily.parquet"),
            self.path / "turnover_daily.parquet",
        ]
        self.turnover_path = next(
            (p for p in self._turnover_candidates if p.exists()),
            self._turnover_candidates[-1],
        )
        self._lock = threading.RLock()
        self._basic: Any = None
        self._codes: list[UniverseRow] | None = None
        self._codes_by_standard: dict[str, UniverseRow] | None = None
        self._coverage_cache: tuple[float, dict] | None = None
        self._latest_cache: tuple[float, str | None] | None = None
        self._calendar_cache: tuple[float, list[str]] | None = None
        self._turnover_cache: dict[str, dict[str, float]] | None = None
        self._cache_db = Path(cache_db) if cache_db else None
        if self._cache_db is not None:
            self._cache_db.parent.mkdir(parents=True, exist_ok=True)
            self._initialize_cache()

    # -- 基础 -------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """数据源摘要，供 /api/health 与页面展示。"""
        coverage = self.coverage()
        return {
            "provider": self.provider,
            "path": str(self.path),
            "daily": str(self.daily_path),
            "start": coverage.get("start"),
            "end": coverage.get("end"),
            "codes": coverage.get("codes"),
            "updated_at": coverage.get("updated_at"),
        }

    def _mtime(self, path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _stock_basic(self) -> Any:
        with self._lock:
            if self._basic is None:
                if not self.basic_path.exists():
                    self._basic = self._pl.DataFrame(
                        schema={
                            "code": self._pl.String,
                            "name": self._pl.String,
                            "st": self._pl.Boolean,
                            "list_date": self._pl.String,
                            "delisted": self._pl.Boolean,
                        }
                    )
                else:
                    self._basic = self._pl.read_parquet(self.basic_path)
            return self._basic

    # -- 代码表 -----------------------------------------------------------

    def _all_codes(self) -> list[UniverseRow]:
        with self._lock:
            if self._codes is not None:
                return self._codes
        rows: list[UniverseRow] = []
        try:
            basic = self._stock_basic()
        except Exception as exc:  # noqa: BLE001 - 基础表损坏时降级为空代码表
            raise ValueError(f"读取 quan stock_basic 失败: {exc}") from exc
        for item in basic.iter_rows(named=True):
            raw = str(item.get("code") or "").strip()
            if not _PURE_CODE.fullmatch(raw):
                continue
            if bool(item.get("delisted")):
                continue
            exchange = infer_exchange(raw)
            if not exchange or exchange == "bj":
                continue
            code = f"{exchange}.{raw}"
            board = board_of(code)
            if board not in SUPPORTED_BOARDS:
                continue
            name = str(item.get("name") or code).strip() or code
            rows.append(
                UniverseRow(
                    code=code,
                    board=board,
                    name=name,
                    ipo_date=str(item.get("list_date") or "").strip(),
                    out_date="",
                    status="1",
                    is_st=bool(item.get("st")) or is_st_name(name),
                )
            )
        rows.sort(key=lambda row: row.code)
        with self._lock:
            self._codes = rows
            self._codes_by_standard = {row.code: row for row in rows}
        return rows

    def list_codes(self, boards: Sequence[str] | None = None) -> list[UniverseRow]:
        rows = self._all_codes()
        if not boards:
            return list(rows)
        wanted = {str(board) for board in boards}
        return [row for row in rows if row.board in wanted]

    def get_code(self, code: str) -> UniverseRow | None:
        try:
            standard = standard_code(code)
        except ValueError:
            return None
        self._all_codes()
        return (self._codes_by_standard or {}).get(standard)

    def stock_name(self, code: str) -> str:
        row = self.get_code(code)
        return row.name if row is not None else ""

    # -- 日线 / 指数 -------------------------------------------------------

    def latest_trade_date(self) -> str | None:
        mtime = self._mtime(self.daily_path)
        with self._lock:
            cached = self._latest_cache
        if cached is not None and cached[0] == mtime:
            return cached[1]
        frame = (
            self._pl.scan_parquet(self.daily_path)
            .select(self._pl.col("date").max().alias("end"))
            .collect()
        )
        value = str(frame["end"][0]) if frame.height and frame["end"][0] else None
        with self._lock:
            self._latest_cache = (mtime, value)
        return value

    def last_date(self, code: str) -> str | None:
        pure = pure_code(code)
        frame = (
            self._pl.scan_parquet(self.daily_path)
            .filter(self._pl.col("code") == pure)
            .select(self._pl.col("date").max().alias("end"))
            .collect()
        )
        return str(frame["end"][0]) if frame.height and frame["end"][0] else None

    def _open_trade_dates(self) -> list[str] | None:
        """trade_calendar.parquet 中 is_open=1 的日期（升序）；无日历/损坏返回 None。

        按文件 mtime 缓存：定时器每轮都要判断「今天是否交易日」，不能每次重读 parquet。
        """
        if not self.calendar_path.exists():
            return None
        mtime = self._mtime(self.calendar_path)
        with self._lock:
            cached = self._calendar_cache
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            pl = self._pl
            cal = pl.read_parquet(self.calendar_path)
            if "is_open" in cal.columns:
                cal = cal.filter(pl.col("is_open") == 1)
            dates = sorted(str(item)[:10] for item in cal["date"].to_list())
        except Exception:  # noqa: BLE001 - 日历损坏时由调用方回落
            return None
        with self._lock:
            self._calendar_cache = (mtime, dates)
        return dates

    def trade_days(self, start: str, end: str) -> list[str] | None:
        """[start, end] 内的交易日（YYYY-MM-DD 升序）；无可用日历时返回 None。

        返回空列表表示「日历覆盖了该区间但全部休市」，与 None（无法判断）语义不同，
        调用方据此区分节假日与日历缺失。
        """
        dates = self._open_trade_dates()
        if dates is None:
            return None
        start_text, end_text = str(start)[:10], str(end)[:10]
        return [day for day in dates if start_text <= day <= end_text]

    def calendar_span(self) -> tuple[str, str] | None:
        """日历覆盖区间 (最早交易日, 最晚交易日)；无日历时返回 None。"""
        dates = self._open_trade_dates()
        if not dates:
            return None
        return dates[0], dates[-1]

    def _recent_cutoff(self, sessions: int, end: str | None = None) -> str | None:
        """最近 ``sessions`` 个交易日窗口的起始日（把全历史扫描收敛到窗口内）。

        选股/回测只需要最近 260 根日线，但原实现会先 collect 1994 年以来的全量
        ``daily.parquet``（600 万行）再做 join/复权，峰值内存可达 1GB，在 2G 内存的
        ECS 上会被 OOM 杀掉（表现为 nginx 502）。这里先算出窗口起点，把过滤下推到
        列存扫描，峰值随窗口线性下降。

        优先用 ``trade_calendar.parquet``（几十 KB）；缺失/异常时回落到日线日期。
        返回值保证不晚于日线实际最新交易日，避免日历超前把窗口切空。
        """
        pl = self._pl
        limit = max(1, int(sessions))
        try:
            bound = self.latest_trade_date()
        except Exception:  # noqa: BLE001 - 元数据不可用时不设上限
            bound = None
        if end and (bound is None or str(end)[:10] < bound):
            bound = str(end)[:10]
        try:
            if self.calendar_path.exists():
                cal = pl.read_parquet(self.calendar_path)
                if "is_open" in cal.columns:
                    cal = cal.filter(pl.col("is_open") == 1)
                if bound:
                    cal = cal.filter(pl.col("date") <= bound)
                dates = [str(item)[:10] for item in cal.sort("date")["date"].to_list()]
                if len(dates) >= limit:
                    return dates[-limit]
                if dates:
                    return dates[0]
        except Exception:  # noqa: BLE001 - 日历损坏时回落日线日期
            pass
        try:
            frame = (
                pl.scan_parquet(self.daily_path)
                .select("date")
                .unique()
                .sort("date")
                .collect()
            )
            if bound:
                frame = frame.filter(pl.col("date") <= bound)
            dates = [str(item)[:10] for item in frame["date"].to_list()]
            if len(dates) >= limit:
                return dates[-limit]
            return dates[0] if dates else None
        except Exception:  # noqa: BLE001
            return None

    def _adj_frame(self, codes: Iterable[str], end: str | None = None) -> Any:
        pl = self._pl
        if not self.adj_path.exists():
            return pl.DataFrame({"code": [], "date": [], "adj_factor": []})
        lf = pl.scan_parquet(self.adj_path).filter(pl.col("code").is_in(list(codes)))
        if end:
            lf = lf.filter(pl.col("date") <= end)
        return lf.collect().sort(["code", "date"])

    def _unreliable_factor_codes(self, adj: Any) -> set[str]:
        """因子序列「后复权累计因子」本应单调不减；出现回退说明该票因子数据不可用。

        quan 库当前存在部分标的的 adj_factor 在同一天序列里来回跳变（例如
        127.66 ↔ 165.02），若照此复权会伪造 ±20% 以上的假涨跌，污染 L1 超跌判定。
        这类票降级为**不复权原始价**（并计入 coverage.factor_anomaly_codes）。
        """
        if adj is None or adj.height == 0:
            return set()
        pl = self._pl
        changes = (
            adj.sort(["code", "date"])
            .with_columns(pl.col("adj_factor").diff().over("code").alias("_diff"))
            .filter(pl.col("_diff") < -1e-9)
            .select("code")
            .unique()
        )
        return {str(code) for code in changes["code"].to_list()}

    def _adjust_frame(
        self, daily: Any, codes: Sequence[str], end: str | None, cutoff: str | None = None,
    ) -> Any:
        """把不复权 OHLC 转成「以区间末日因子为基准」的前复权价。

        因子回退的标的自动降级为原始价（见 ``_unreliable_factor_codes``）。

        ``cutoff`` 为窗口起点（仅窗口化读取时传入）：窗口内首根之前可能整段没有
        adj_factor 事件，此时用窗口前最后一个因子（``_factor_base``）回填，
        保证窗口内每一根的复权基准仍然正确。
        """
        pl = self._pl
        adj = self._adj_frame(codes, end=end)
        if adj.height == 0:
            return daily
        unreliable = self._unreliable_factor_codes(adj)
        merged = (
            daily.join(adj, on=["code", "date"], how="left")
            .sort(["code", "date"])
        )
        if cutoff:
            base = (
                adj.filter(pl.col("date") < cutoff)
                .sort(["code", "date"])
                .group_by("code", maintain_order=True)
                .agg(pl.col("adj_factor").last().alias("_factor_base"))
            )
            merged = merged.join(base, on="code", how="left")
            # 先按窗口内事件前向填充；仍为 null 的（窗口起点前没有事件）用窗口前因子兜底。
            filled = pl.coalesce(
                [pl.col("adj_factor").forward_fill().over("code"),
                 pl.col("_factor_base"), pl.lit(1.0)]
            )
        else:
            filled = pl.col("adj_factor").forward_fill().over("code").fill_null(1.0)
        merged = merged.with_columns(filled.alias("adj_factor"))
        refs = (
            adj.sort(["code", "date"])
            .group_by("code", maintain_order=True)
            .agg(pl.col("adj_factor").last().alias("_factor_ref"))
        )
        merged = merged.join(refs, on="code", how="left").with_columns(
            pl.col("_factor_ref").fill_null(1.0)
        )
        if unreliable:
            merged = merged.with_columns(
                pl.col("code").is_in(sorted(unreliable)).alias("_unreliable")
            )
            scale = (
                pl.when(pl.col("_unreliable"))
                .then(1.0)
                .otherwise(pl.col("adj_factor") / pl.col("_factor_ref"))
            )
        else:
            scale = pl.col("adj_factor") / pl.col("_factor_ref")
        merged = merged.with_columns(
            [(pl.col(column) * scale).alias(column) for column in _DAILY_COLUMNS]
        )
        drop = ["adj_factor", "_factor_ref"]
        if cutoff:
            drop.append("_factor_base")
        if unreliable:
            drop.append("_unreliable")
        return merged.drop(drop)

    @staticmethod
    def _bar_from_row(
        row: dict[str, Any], symbol: str, timeframe: str, previous_close: float | None,
    ) -> Bar:
        close = float(row.get("close") or 0)
        prev = previous_close if previous_close not in (None, 0) else close
        if timeframe == "daily":
            timestamp = datetime.strptime(str(row["date"])[:10], "%Y-%m-%d")
        else:
            text = str(row["date"])
            timestamp = datetime.strptime(text, "%Y-%m-%d %H:%M") if len(text) > 10 else (
                datetime.strptime(text[:10], "%Y-%m-%d")
            )
        return Bar(
            timestamp=timestamp,
            symbol=symbol,
            open=price(float(row.get("open") or 0)),
            high=price(float(row.get("high") or 0)),
            low=price(float(row.get("low") or 0)),
            close=price(close),
            volume=float(row.get("volume") or 0),
            amount=money(float(row.get("amount") or 0)),
            timeframe=timeframe,
            turnover_rate=None,
            prev_close=price(prev),
        )

    def _tail_frame(self, frame: Any, max_rows: int | None) -> Any:
        pl = self._pl
        if not max_rows or frame.height == 0:
            return frame
        limit = max(1, int(max_rows))
        return frame.sort(["code", "date"]).group_by("code", maintain_order=True).tail(limit + 1)

    def load_bars(
        self,
        codes: Sequence[str],
        *,
        max_rows: int | None = 260,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, list[Bar]]:
        """批量读取日线（前复权），返回 ``{标准代码: [Bar...]}``（升序）。"""
        pl = self._pl
        mapping: dict[str, str] = {}
        for raw in codes or []:
            try:
                pure = pure_code(raw)
            except ValueError:
                continue
            mapping[f"{infer_exchange(pure)}.{pure}"] = pure
        if not mapping:
            return {}
        result: dict[str, list[Bar]] = {code: [] for code in mapping}
        lf = pl.scan_parquet(self.daily_path)
        lf = lf.filter(pl.col("code").is_in(list(mapping.values())))
        if end:
            lf = lf.filter(pl.col("date") <= end)
        cutoff: str | None = None
        if max_rows and not start:
            # 只要「最近 N 根」时，把日期过滤下推到列存扫描：避免把 1994 年以来的
            # 全量日线（600 万行）读进内存 —— 2G ECS 上正是这里触发 OOM/502。
            cutoff = self._recent_cutoff(int(max_rows) + _WINDOW_BUFFER, end)
            if cutoff:
                lf = lf.filter(pl.col("date") >= cutoff)
        daily = lf.collect()
        if daily.height == 0:
            return result
        daily = self._adjust_frame(daily, list(mapping.values()), end, cutoff)
        if start:
            daily = daily.filter(pl.col("date") >= start)
        daily = self._tail_frame(daily, max_rows)
        for group_key, frame in daily.sort(["code", "date"]).group_by("code", maintain_order=True):
            # polars 的 group_by 迭代键在部分版本里是 1 元 tuple。
            pure = group_key[0] if isinstance(group_key, tuple) else group_key
            symbol = next((code for code, value in mapping.items() if value == pure), None)
            if symbol is None:
                continue
            rows = list(frame.iter_rows(named=True))
            bars: list[Bar] = []
            previous: float | None = None
            for row in rows:
                bar = self._bar_from_row(row, symbol, "daily", previous)
                bars.append(bar)
                previous = bar.close
            if max_rows and len(bars) > max_rows:
                bars = bars[-int(max_rows):]
            result[symbol] = bars
        return result

    def load_history(
        self, codes: Sequence[str], max_rows: int = 260,
        *, start: str | None = None, end: str | None = None,
    ) -> dict[str, list[Bar]]:
        """批量日线：``end`` 用于历史时点回看（漏斗回测 as_of / 长区间持仓窗口）。

        传 ``end`` 时取「截至 end 的最近 max_rows 根」而不是「截至最新交易日的
        最近 max_rows 根」，否则回测首年拿不到任何 Bar（tail 落在回测区间之后）。
        """
        return self.load_bars(codes, max_rows=max_rows, start=start, end=end)

    def list_trade_dates(
        self, codes: Sequence[str], *, start: str | None = None, end: str | None = None,
    ) -> list[str]:
        """窗口内这些代码出现过日线的 distinct 交易日（升序）。

        只 select 日期列并下推 code/date 过滤，避免把全量日线读进内存构建 Bar 对象
        （2G ECS 上全池 52 万根 Bar 会 OOM）。供漏斗轮动回测构建交易日时间线。
        """
        pure_codes: list[str] = []
        for raw in codes or []:
            try:
                pure_codes.append(pure_code(raw))
            except ValueError:
                continue
        if not pure_codes:
            return []
        pl = self._pl
        lf = pl.scan_parquet(self.daily_path).filter(pl.col("code").is_in(pure_codes))
        if start:
            lf = lf.filter(pl.col("date") >= str(start)[:10])
        if end:
            lf = lf.filter(pl.col("date") <= str(end)[:10])
        frame = lf.select(pl.col("date")).unique().sort("date").collect()
        return [str(value)[:10] for value in frame["date"].to_list() if value]

    def count_codes_with_daily(self, codes: Sequence[str]) -> int:
        """这些代码里在日线库中至少有一根 K 线的数量（仅计数，不驻留 Bar）。"""
        pure_codes: list[str] = []
        for raw in codes or []:
            try:
                pure_codes.append(pure_code(raw))
            except ValueError:
                continue
        if not pure_codes:
            return 0
        pl = self._pl
        frame = (
            pl.scan_parquet(self.daily_path)
            .filter(pl.col("code").is_in(pure_codes))
            .select(pl.col("code").unique())
            .collect()
        )
        return int(frame.height)

    def load_series(
        self,
        code: str,
        *,
        start: str | None = None,
        end: str | None = None,
        max_rows: int | None = None,
    ) -> list[Bar]:
        """单标的日线（前复权）；首根 prev_close 尽量取区间前一交易日的收盘。"""
        symbol = standard_code(code)
        bars = self.load_bars([symbol], max_rows=max_rows, start=start, end=end).get(symbol, [])
        if not bars:
            return []
        if start:
            # 补一根区间前日线，用它的收盘作为首根 prev_close（与 baostock preclose 口径一致）。
            before = self.load_bars([symbol], max_rows=1, end=_previous_day(start)).get(symbol, [])
            if before:
                first = bars[0]
                bars[0] = Bar(
                    timestamp=first.timestamp, symbol=first.symbol, open=first.open,
                    high=first.high, low=first.low, close=first.close, volume=first.volume,
                    amount=first.amount, timeframe=first.timeframe,
                    turnover_rate=first.turnover_rate, prev_close=price(before[0].close),
                )
        return bars

    def load_index(
        self,
        index_code: str,
        max_rows: int = 260,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> list[Bar]:
        """指数日线（不复权），``index_code`` 支持 ``sh.000688`` / ``000688``。"""
        if not self.index_path.exists():
            return []
        try:
            key = pure_code(index_code)
        except ValueError:
            return []
        pl = self._pl
        lf = pl.scan_parquet(self.index_path).filter(pl.col("index_key") == key)
        if start:
            lf = lf.filter(pl.col("date") >= start)
        if end:
            lf = lf.filter(pl.col("date") <= end)
        frame = lf.collect().sort("date")
        if frame.height == 0:
            return []
        if max_rows:
            frame = frame.tail(int(max_rows) + 1)
        bars: list[Bar] = []
        previous: float | None = None
        for row in frame.iter_rows(named=True):
            bar = self._bar_from_row(row, f"{infer_exchange(key)}.{key}", "daily", previous)
            bars.append(bar)
            previous = bar.close
        if max_rows and len(bars) > max_rows:
            bars = bars[-int(max_rows):]
        return bars

    def list_index_keys(self) -> list[str]:
        if not self.index_path.exists():
            return []
        frame = (
            self._pl.scan_parquet(self.index_path)
            .select(self._pl.col("index_key").unique().sort())
            .collect()
        )
        return [str(item) for item in frame["index_key"].to_list()]

    # -- 5 分钟 ------------------------------------------------------------

    def minute5_path(self, code: str) -> Path:
        return self.minute5_dir / f"{pure_code(code)}.parquet"

    def has_minute5(self, code: str) -> bool:
        try:
            return self.minute5_path(code).exists()
        except ValueError:
            return False

    def _daily_turnover_map(self, code: str) -> dict[str, float]:
        """某标的的 {date: 当日日线换手率}，来自同目录 turnover_daily.parquet。

        懒加载 + 按 code 缓存；parquet 缺失/无该 code 返回空 dict（做 T 准入降级）。
        """
        if self._turnover_cache is None:
            self._turnover_cache = {}
            if self.turnover_path.exists():
                pl = self._pl
                df = pl.read_parquet(self.turnover_path)
                for row in df.iter_rows(named=True):
                    self._turnover_cache.setdefault(str(row["code"]), {})[
                        str(row["date"])[:10]
                    ] = float(row["turnover"])
        return self._turnover_cache.get(code, {})

    def load_minute5(
        self, code: str, *, max_rows: int | None = 1200, start: str | None = None,
        end: str | None = None, daily_turnover: dict[str, float] | None = None,
    ) -> list[Bar]:
        """5 分钟 K 线（前复权，按 bar 当日因子折算）；文件缺失返回空列表。

        ``start``/``end`` 下推日期过滤（长期持仓只取持有期，避免全表扫描）。
        ``daily_turnover``：{YYYY-MM-DD: 当日日线换手率}，用于给 5 分钟 bar 代理
        填充 turnover_rate（分钟 parquet 无换手字段，做 T 准入需要）。
        未传时自动从同目录 turnover_daily.parquet 加载（若存在）。
        历史 5 分钟无真实盘口，bid/ask 用 close±0.1% 代理（真实成本由 slippage 体现），
        仅当缺失时填充，实盘真盘口不受影响。
        """
        path = self.minute5_path(code)
        if not path.exists():
            return []
        if daily_turnover is None:
            daily_turnover = self._daily_turnover_map(code)
        symbol = standard_code(code)
        pure = pure_code(code)
        pl = self._pl
        lf = pl.scan_parquet(path)
        if start:
            lf = lf.filter(pl.col("date") >= str(start)[:10])
        if end:
            lf = lf.filter(pl.col("date") <= end)
        frame = lf.collect().sort("date")
        if frame.height == 0:
            return []
        factors = self._adj_frame([pure])
        factor_by_day: list[tuple[str, float]] = []
        factor_ref = 1.0
        unreliable = self._unreliable_factor_codes(factors)
        if factors.height:
            factor_by_day = sorted(
                (str(row["date"]), float(row["adj_factor"]))
                for row in factors.iter_rows(named=True)
            )
            factor_ref = factor_by_day[-1][1] or 1.0
        days = [day for day, _ in factor_by_day]
        frame = frame.with_columns(pl.col("date").str.slice(0, 10).alias("_day"))
        bars: list[Bar] = []
        previous: float | None = None
        for row in frame.iter_rows(named=True):
            bar = self._bar_from_row(row, symbol, "5m", previous)
            factor = _factor_on(days, factor_by_day, str(row.get("_day") or str(row["date"])[:10]))
            scale = 1.0 if (unreliable or not factor_ref) else factor / factor_ref
            if scale != 1.0:
                bar.open = price(bar.open * scale)
                bar.high = price(bar.high * scale)
                bar.low = price(bar.low * scale)
                bar.close = price(bar.close * scale)
            # 回测代理：分钟 parquet 无盘口/换手字段，做 T 准入需要它们。
            # bid/ask 用 close±0.1% 代理（真实成本由 slippage 体现）；换手率取当日日线值。
            if bar.bid is None:
                bar.bid = price(bar.close * 0.999)
            if bar.ask is None:
                bar.ask = price(bar.close * 1.001)
            if bar.turnover_rate is None and daily_turnover:
                bar.turnover_rate = daily_turnover.get(str(row.get("_day") or str(row["date"])[:10]))
            bars.append(bar)
            previous = bar.close
        if max_rows and len(bars) > max_rows:
            bars = bars[-int(max_rows):]
        return bars

    # -- 覆盖度 ------------------------------------------------------------

    @staticmethod
    def _collect_streaming(lazy):
        """优先用流式收集，降低全量 parquet 聚合的内存峰值（服务器 1.8G 内存）。"""
        try:
            return lazy.collect(engine="streaming")
        except TypeError:  # 旧版 polars 没有 engine 参数
            return lazy.collect()

    def factor_anomaly_codes(self) -> set[str]:
        """adj_factor 序列出现回退（不可能出现）的标的集合；缓存按文件 mtime 失效。

        内存口径（服务器 1.8G）：不做全表 collect/排序，先用全局 shift 差分 + 代码块
        连续性校验；仅在文件不是按 code 连续、且块内日期有序时才回落到排序版。
        """
        mtime = self._mtime(self.adj_path)
        with self._lock:
            cached = getattr(self, "_factor_anomaly_cache", None)
        if cached is not None and cached[0] == mtime:
            return set(cached[1])
        result: set[str] = set()
        if self.adj_path.exists():
            pl = self._pl
            lf = pl.scan_parquet(self.adj_path).select(["code", "date", "adj_factor"])
            meta = lf.with_columns(
                pl.col("code").shift(1).alias("_prev_code"),
                pl.col("date").shift(1).alias("_prev_date"),
            ).select(
                (pl.col("code") != pl.col("_prev_code")).sum().alias("transitions"),
                pl.col("code").n_unique().alias("codes"),
                (
                    (pl.col("code") == pl.col("_prev_code"))
                    & (pl.col("date") < pl.col("_prev_date"))
                ).sum().alias("order_violations"),
            ).collect()
            transitions = int(meta["transitions"][0] or 0)
            codes = int(meta["codes"][0] or 0)
            violations = int(meta["order_violations"][0] or 0)
            if transitions == max(0, codes - 1) and violations == 0:
                # 按文件顺序做全局差分；跨代码边界的差分用 _prev_code 过滤掉。
                frame = (
                    lf.with_columns(
                        pl.col("code").shift(1).alias("_prev_code"),
                        pl.col("adj_factor").diff().alias("_diff"),
                    )
                    .filter(
                        (pl.col("code") == pl.col("_prev_code"))
                        & (pl.col("_diff") < -1e-9)
                    )
                    .select("code")
                    .unique()
                    .collect(engine="streaming")
                )
            else:
                frame = (
                    lf.sort(["code", "date"])
                    .with_columns(pl.col("adj_factor").diff().over("code").alias("_diff"))
                    .filter(pl.col("_diff") < -1e-9)
                    .select("code")
                    .unique()
                    .collect(engine="streaming")
                )
            result = {str(code) for code in frame["code"].to_list()}
        with self._lock:
            self._factor_anomaly_cache = (mtime, result)
        return set(result)

    def coverage(self) -> dict:
        mtime = self._mtime(self.daily_path)
        with self._lock:
            cached = self._coverage_cache
        if cached is not None and cached[0] == mtime:
            return dict(cached[1])
        pl = self._pl
        lf = pl.scan_parquet(self.daily_path)
        # 只做聚合，不把 600 万行明细 collect 进内存。
        stats = lf.select(
            pl.len().alias("rows"),
            pl.col("code").n_unique().alias("codes"),
            pl.col("date").min().alias("start"),
            pl.col("date").max().alias("end"),
        ).collect()
        rows = int(stats["rows"][0] or 0)
        codes = int(stats["codes"][0] or 0)
        start = str(stats["start"][0]) if rows and stats["start"][0] is not None else None
        end = str(stats["end"][0]) if rows and stats["end"][0] is not None else None
        per_code = self._collect_streaming(
            lf.group_by("code").agg(pl.col("date").max().alias("last"))
        )
        stale = int(per_code.filter(pl.col("last") < (end or "")).height) if end else 0
        daily_codes = {str(code) for code in per_code["code"].to_list()} if rows else set()
        all_rows = self._all_codes()
        board_counts = {board: 0 for board in SUPPORTED_BOARDS}
        missing = 0
        for row in all_rows:
            board_counts[row.board] = board_counts.get(row.board, 0) + 1
            if pure_code(row.code) not in daily_codes:
                missing += 1
        payload = {
            "codes": codes,
            "rows": rows,
            "start": start,
            "end": end,
            "boards": board_counts,
            "fundamentals": self._fundamentals_count(),
            "updated_at": datetime.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else None,
            "missing_codes": missing,
            "stale_codes": stale,
            "provider": self.provider,
            "path": str(self.path),
            "turnover_available": False,
            "factor_anomaly_codes": len(self.factor_anomaly_codes()),
            "adjustment": "qfq_with_raw_fallback",
        }
        with self._lock:
            self._coverage_cache = (mtime, payload)
        return dict(payload)

    # -- 财务缓存（仅缓存，不作为行情来源） --------------------------------

    def _initialize_cache(self) -> None:
        if self._cache_db is None:
            return
        with sqlite3.connect(self._cache_db, timeout=30) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS universe_fundamentals ("
                "code TEXT PRIMARY KEY, checked_at TEXT NOT NULL, payload TEXT NOT NULL)"
            )

    def _fundamentals_count(self) -> int:
        if self._cache_db is None or not self._cache_db.exists():
            return 0
        try:
            with sqlite3.connect(self._cache_db, timeout=10) as connection:
                return int(
                    connection.execute("SELECT COUNT(*) FROM universe_fundamentals").fetchone()[0]
                )
        except sqlite3.Error:
            return 0

    def get_fundamentals(self, code: str) -> dict | None:
        if self._cache_db is None:
            return None
        import json

        try:
            standard = standard_code(code)
        except ValueError:
            return None
        try:
            with sqlite3.connect(self._cache_db, timeout=10) as connection:
                row = connection.execute(
                    "SELECT checked_at, payload FROM universe_fundamentals WHERE code=?",
                    (standard,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        try:
            payload = json.loads(row[1])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        payload.setdefault("checked_at", row[0])
        return payload

    def save_fundamentals(self, code: str, payload: dict) -> None:
        if self._cache_db is None:
            return
        import json

        try:
            standard = standard_code(code)
        except ValueError:
            return
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self._lock, sqlite3.connect(self._cache_db, timeout=30) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO universe_fundamentals(code,checked_at,payload) "
                "VALUES(?,?,?)",
                (standard, utc_now(), text),
            )


def _previous_day(value: str) -> str:
    try:
        from datetime import date, timedelta

        return (date.fromisoformat(str(value)[:10]) - timedelta(days=1)).isoformat()
    except ValueError:
        return str(value)


def _factor_on(days: list[str], rows: list[tuple[str, float]], day: str) -> float:
    """取 ``day`` 当日（或之前最近一日）的复权因子。"""
    if not rows:
        return 1.0
    index = bisect.bisect_right(days, str(day)[:10]) - 1
    if index < 0:
        index = 0
    return float(rows[index][1] or 1.0)


def _import_polars() -> Any:
    try:
        import polars  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 环境未安装 polars
        raise ImportError(
            "quan_parquet 数据源需要 polars 读取 parquet："
            "pip install 'polars>=1.0,<2'；或设置 WB_QUAN_DATA_DIR='' 回落旧数据链路"
        ) from exc
    return polars


# ---------------------------------------------------------------------------
# 只读 refresher（quan_parquet 由 quan_quant 更新器负责刷新，本服务不写数据）
# ---------------------------------------------------------------------------


class QuanRefresher:
    """接口对齐 ``universe.UniverseRefresher``，但只读：不触发任何网络/写库。"""

    def __init__(self, store: QuanStore) -> None:
        self.store = store
        self._message = "quan_parquet 为只读外部数据源，请用 quan_quant 的数据更新器刷新后再运行选股"

    def status(self) -> dict:
        return {
            "running": False,
            "job": {
                "running": False,
                "started_at": None,
                "finished_at": None,
                "done": 0,
                "total": 0,
                "current": "",
                "message": self._message,
                "fetched_rows": 0,
                "errors": [],
                "skipped": 0,
            },
            "coverage": self.store.coverage(),
            "provider": self.store.provider,
        }

    def start(self, **_kwargs: Any) -> dict:
        return {"status": "skipped", "job": self.status()["job"], "message": self._message}

    def run_once(self, **_kwargs: Any) -> dict:
        return {"status": "skipped", "message": self._message, "coverage": self.store.coverage()}
