"""选股漏斗 L0–L4（契约见 .workbuddy/screener-contract.md §4）。

层次语义：
- L0 大盘同步门：对应板块指数近 20 日收益 < 0 才开门；两板块都关 → 空仓等待。
- L1 A1 超跌：20 日跌幅 < ``chg20_max`` 且距 52 周高回撤 ≥ ``week52_drawdown``。
- L2 一票否决：ST/退市/-U/上市不足/当日涨幅过高/黑名单 + 财务深筛。
- L3 容量：当日成交额与 20 日均额门槛。
- L4 排序取第一：按 Chg20D 升序逐只深筛，通过者进 ``eligible`` 供池内轮动。

数据来自 ``UniverseStore``（双创日线缓存，见 universe.py）；实时叠加用腾讯批量快照。
"""

from __future__ import annotations

import math
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from .domain import Bar, utc_now
from .market import BAOSTOCK_LOCK, MarketDataService, normalize_symbol
from .universe import (
    BOARD_GEM,
    BOARD_INDEX,
    BOARD_STAR,
    SUPPORTED_BOARDS,
    UniverseRefresher,
    UniverseRow,
    UniverseStore,
    is_delisting_name,
    is_st_name,
    is_unprofitable_u_name,
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _num(value: Any, digits: int = 6, default: float = 0.0) -> float:
    """JSON 安全数值：NaN/Infinity/None/非数字统一回落 default。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return round(number, digits)


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _check(layer: str, rule: str, status: str, detail: str = "") -> dict:
    return {"layer": layer, "rule": rule, "status": status, "detail": detail}


def _bar_window(bars: Sequence[Bar], as_of: str) -> list[Bar]:
    """截至 as_of 的升序日线（历史回看场景）。"""
    if not bars:
        return []
    return [bar for bar in bars if bar.date <= as_of]


def _normalize_blacklist(values: Sequence[str]) -> set[str]:
    result: set[str] = set()
    for item in values or []:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            result.add(normalize_symbol(text).standard)
        except ValueError:
            result.add(text.lower())
    return result


# ---------------------------------------------------------------------------
# 配置 / 结果模型
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScreenerConfig:
    boards: list[str] = field(default_factory=lambda: ["创业板", "科创板"])
    chg20_max: float = -0.15            # L1: Chg20D < -15%
    week52_drawdown: float = 0.30       # L1: Close < High250 × (1-0.30)
    day_change_max: float = 0.05        # L2: 当日涨幅 > 5% 否决
    min_amount: float = 5e8             # L3: 当日成交额 ≥ 5亿
    min_avg_amount_20d: float = 5e8     # L3: 20日均成交额 ≥ 5亿
    min_listing_sessions: int = 6       # L2: 上市不足 6 个交易日否决
    exclude_st: bool = True
    exclude_delisting: bool = True
    exclude_unprofitable_u: bool = True
    enable_fundamental_veto: bool = True  # L2 深筛：亏损且营收<1亿 / 净资产为负
    max_deep_checks: int = 10           # L4 逐只深筛上限
    blacklist: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.boards:
            raise ValueError("boards 不能为空")
        normalized_boards: list[str] = []
        for board in self.boards:
            text = str(board).strip()
            if text not in SUPPORTED_BOARDS:
                raise ValueError(f"不支持的板块: {board}")
            if text not in normalized_boards:
                normalized_boards.append(text)
        self.boards = normalized_boards
        for name in ("chg20_max", "week52_drawdown", "day_change_max", "min_amount", "min_avg_amount_20d"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} 必须是有限数值")
            setattr(self, name, value)
        self.min_listing_sessions = max(0, int(self.min_listing_sessions))
        self.max_deep_checks = max(0, int(self.max_deep_checks))
        self.blacklist = [str(item) for item in (self.blacklist or [])]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict | None) -> "ScreenerConfig":
        if not value:
            return cls()
        if not isinstance(value, dict):
            raise ValueError("config 必须是对象")
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"未知选股参数: {', '.join(sorted(unknown))}")
        return cls(**value)


@dataclass
class Candidate:
    code: str
    name: str
    board: str
    close: float
    prev_close: float
    as_of: str
    chg20d: float
    week52_high: float
    drawdown_52w: float
    day_change: float
    amount: float
    avg_amount_20d: float
    turnover: float | None
    sessions: int
    score: float
    checks: list[dict]
    vetoed_by: str | None
    deep_checked: bool = False

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "board": self.board,
            "close": _num(self.close, 4),
            "prev_close": _num(self.prev_close, 4),
            "as_of": self.as_of,
            "chg20d": _num(self.chg20d),
            "week52_high": _num(self.week52_high, 4),
            "drawdown_52w": _num(self.drawdown_52w),
            "day_change": _num(self.day_change),
            "amount": _num(self.amount, 2),
            "avg_amount_20d": _num(self.avg_amount_20d, 2),
            "turnover": _num(self.turnover) if self.turnover is not None else None,
            "sessions": int(self.sessions),
            "score": _num(self.score),
            "checks": [dict(item) for item in self.checks],
            "vetoed_by": self.vetoed_by,
            "deep_checked": bool(self.deep_checked),
        }


@dataclass
class ScreeningResult:
    id: str
    created_at: str
    as_of: str
    config: dict
    gate: dict
    funnel: dict
    pool: list[Candidate]
    eligible: list[Candidate]
    first: Candidate | None
    warnings: list[str]
    data_info: dict
    elapsed_seconds: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "as_of": self.as_of,
            "config": dict(self.config),
            "gate": self.gate,
            "funnel": dict(self.funnel),
            "pool": [item.to_dict() for item in self.pool],
            "eligible": [item.to_dict() for item in self.eligible],
            "first": self.first.to_dict() if self.first else None,
            "warnings": list(self.warnings),
            "data_info": dict(self.data_info),
            "elapsed_seconds": _num(self.elapsed_seconds, 3),
        }


# ---------------------------------------------------------------------------
# 财务深筛
# ---------------------------------------------------------------------------

FUNDAMENTAL_UNAVAILABLE = {
    "checked": False,
    "net_profit": None,
    "revenue": None,
    "negative_equity": None,
    "veto": None,
    "source": "unavailable",
}


def _recent_quarters(count: int = 4) -> list[tuple[int, int]]:
    """最近 count 个「已结束」季度（新→旧）。"""
    today = date.today()
    year, quarter = today.year, (today.month - 1) // 3 + 1
    result: list[tuple[int, int]] = []
    year, quarter = (year, quarter - 1) if quarter > 1 else (year - 1, 4)
    for _ in range(max(1, int(count))):
        result.append((year, quarter))
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
    return result


class FundamentalService:
    """baostock 季度财务：亏损且营收<1亿 / 净资产为负 的深筛否决。

    缓存优先（``universe_fundamentals``）；拉取失败 → ``checked=False``，不否决该票。
    一个实例内复用同一次 baostock login（多次 check 不重复 login/logout），
    用完调用 ``close()`` 释放会话。
    """

    def __init__(self, store: UniverseStore, *, sleep_seconds: float = 0.0) -> None:
        self.store = store
        self.sleep_seconds = max(0.0, float(sleep_seconds))
        self._bs: Any = None
        self._session_started: float | None = None
        self.session_max_age_seconds = 300.0

    def close(self) -> None:
        bs, self._bs = self._bs, None
        self._session_started = None
        if bs is not None:
            try:
                bs.logout()
            except Exception:  # noqa: BLE001
                pass

    def _ensure_session(self) -> None:
        if self._bs is None:
            import baostock as bs

            self._bs = bs
        if self._session_started is not None:
            if time.monotonic() - self._session_started < self.session_max_age_seconds:
                return
            try:
                self._bs.logout()
            except Exception:  # noqa: BLE001
                pass
        login = self._bs.login()
        if getattr(login, "error_code", "0") != "0":
            raise ValueError(f"baostock 登录失败: {getattr(login, 'error_msg', '')}")
        self._session_started = time.monotonic()

    def check(self, code: str) -> dict:
        cached = self.store.get_fundamentals(code)
        if cached is not None and "net_profit" in cached:
            payload = dict(cached)
            payload["source"] = "cached"
            return payload
        payload = self._fetch(code)
        if payload.get("checked"):
            saved = {key: value for key, value in payload.items() if key != "source"}
            try:
                self.store.save_fundamentals(code, saved)
            except (TypeError, ValueError):
                pass  # 个别非 JSON 安全字段不应影响选股
        return payload

    def _fetch(self, code: str) -> dict:
        for attempt in range(2):
            try:
                return self._fetch_once(code)
            except Exception:  # noqa: BLE001 - 会话/网络异常：重连后重试一次
                self.close()
                if attempt == 0:
                    time.sleep(0.2)
        return dict(FUNDAMENTAL_UNAVAILABLE)

    def _fetch_once(self, code: str) -> dict:
        payload = dict(FUNDAMENTAL_UNAVAILABLE)
        # baostock 禁止并发：会话管理 + 季度查询整体持锁，避免与 market 回落链路撞车被拉黑。
        with BAOSTOCK_LOCK:
            self._ensure_session()
            assert self._bs is not None
            profit_row = self._latest_profit_row(self._bs, code)
            balance_row = None
            if profit_row is not None:
                balance_row = self._balance_row(self._bs, code, str(profit_row[2]))
        if profit_row is None:
            return payload
        net_profit = _opt_float(profit_row[6] if len(profit_row) > 6 else None)
        revenue = _opt_float(profit_row[8] if len(profit_row) > 8 else None)
        if net_profit is None or revenue is None:
            return payload  # 字段缺失 → 本票不否决

        negative_equity: bool | None = None
        if balance_row is not None and len(balance_row) > 8:
            # baostock 资产负债表没有 totalAsset/totalLiability 字段，
            # 用 liabilityToAsset > 1（或 assetToEquity < 0）等价判定净资产为负。
            liability_to_asset = _opt_float(balance_row[7])
            asset_to_equity = _opt_float(balance_row[8])
            if liability_to_asset is not None:
                negative_equity = liability_to_asset > 1.0
            elif asset_to_equity is not None:
                negative_equity = asset_to_equity < 0

        veto: str | None = None
        if negative_equity:
            veto = "净资产为负"
        elif net_profit < 0 and revenue < 1e8:
            veto = "亏损且营收<1亿"

        return {
            "checked": True,
            "net_profit": _num(net_profit, 2),
            "revenue": _num(revenue, 2),
            "negative_equity": negative_equity,
            "veto": veto,
            "source": "baostock",
            "stat_date": str(profit_row[2]) if len(profit_row) > 2 else "",
        }

    @staticmethod
    def _latest_profit_row(bs: Any, code: str) -> list[str] | None:
        today = date.today().isoformat()
        for year, quarter in _recent_quarters(4):
            try:
                result_set = bs.query_profit_data(code, year, quarter)
            except Exception:  # noqa: BLE001
                continue
            if getattr(result_set, "error_code", "0") != "0":
                continue
            latest: list[str] | None = None
            while result_set.next():
                row = list(result_set.get_row_data())
                if len(row) <= 8 or not row[2] or str(row[2]) > today:
                    continue
                # 「最新有效一条」：净利润与营业收入都必须有值，否则回退到上一季度。
                if _opt_float(row[6]) is None or _opt_float(row[8]) is None:
                    continue
                latest = row
            if latest is not None:
                return latest
        return None

    @staticmethod
    def _balance_row(bs: Any, code: str, stat_date: str) -> list[str] | None:
        match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(stat_date or ""))
        if not match:
            return None
        year = int(match.group(1))
        month = int(match.group(2))
        quarter = (month - 1) // 3 + 1
        try:
            result_set = bs.query_balance_data(code, year, quarter)
        except Exception:  # noqa: BLE001
            return None
        if getattr(result_set, "error_code", "0") != "0":
            return None
        row: list[str] | None = None
        while result_set.next():
            current = list(result_set.get_row_data())
            if len(current) > 8 and str(current[2]) == str(stat_date):
                row = current
        return row


# ---------------------------------------------------------------------------
# 服务门面
# ---------------------------------------------------------------------------


class ScreenerService:
    def __init__(
        self,
        data_dir: str | Path,
        market: MarketDataService,
        *,
        store: Any | None = None,
        refresher: Any | None = None,
        sleep_seconds: float = 0.0,
    ) -> None:
        """``store``/``refresher`` 可由 create_app 注入（quan_parquet 只读源）；
        未注入时回落本地 universe.db + baostock 旧链路。"""
        self.data_dir = Path(data_dir)
        self.market = market
        self.sleep_seconds = max(0.0, float(sleep_seconds))
        if store is not None:
            self.store = store
            if refresher is not None:
                self.refresher = refresher
            elif getattr(store, "provider", "") == "quan_parquet":
                from .quan_data import QuanRefresher

                self.refresher = QuanRefresher(store)
            else:
                self.refresher = UniverseRefresher(store, market)
        else:
            override = os.environ.get("WB_UNIVERSE_DB")
            db_path = Path(override) if override else self.data_dir / "universe.db"
            self.store = UniverseStore(db_path)
            self.refresher = refresher or UniverseRefresher(self.store, market)

    def _is_quan(self) -> bool:
        return getattr(self.store, "provider", "") == "quan_parquet"

    def defaults(self) -> dict:
        if self._is_quan():
            sources = {
                "provider": "quan_parquet",
                "quan_data_dir": str(self.store.path),
                "daily": "quan_quant daily.parquet + adj_factor.parquet（前复权，不复权原始价 × 因子 ÷ 最新因子）",
                "index": "quan_quant index_daily.parquet（399006/000688 由 scripts/quan_migrate.py 增量回填）",
                "realtime": "腾讯 qt.gtimg.cn 批量快照（GBK，≤80/批）",
                "fundamental": "baostock query_profit_data / query_balance_data（本地缓存，quan 数据无财务字段）",
                "turnover": "quan 数据无换手率字段，turnover 显示为空",
            }
        else:
            sources = {
                "universe_db": str(self.store.path),
                "daily": "baostock query_history_k_data_plus（前复权 adjustflag=2）",
                "index": "baostock 指数日线；sh.000688 用腾讯兜底",
                "realtime": "腾讯 qt.gtimg.cn 批量快照（GBK，≤80/批）",
                "fundamental": "baostock query_profit_data / query_balance_data 季度财务",
            }
        return {
            "config": ScreenerConfig().to_dict(),
            "boards": list(SUPPORTED_BOARDS),
            "coverage": self.store.coverage(),
            "sources": sources,
            "notes": [
                "L0 大盘同步门：板块指数近20日收益 < 0 才开门；指数缺失按开门降级处理。",
                "L1 A1 超跌：20日跌幅 < -15% 且距52周高回撤 ≥ 30%。",
                "L2 一票否决：ST/退市/-U/上市不足6个交易日/当日涨幅>5%/黑名单，外加财务深筛。",
                "L3 容量：当日成交额与20日均额均 ≥ 5亿。",
                "L4 排序：pool 按 Chg20D 升序逐只深筛，通过者进 eligible，first = eligible[0]。",
                "L2 部分否决项数据源未覆盖（非标审计/资金占用/立案调查/解禁洪峰），需人工复核。",
            ],
        }

    def coverage(self) -> dict:
        return {"coverage": self.store.coverage(), "refresh": self.refresher.status()}

    def refresh(self, **kwargs: Any) -> dict:
        return self.refresher.start(**kwargs)

    def refresh_status(self) -> dict:
        return self.refresher.status()

    def run(
        self,
        *,
        config: ScreenerConfig | None = None,
        as_of: str | None = None,
        include_realtime: bool = False,
    ) -> ScreeningResult:
        return run_screener(
            self.store,
            self.market,
            config,
            as_of=as_of,
            include_realtime=include_realtime,
        )


# ---------------------------------------------------------------------------
# 纯函数入口（便于测试）
# ---------------------------------------------------------------------------


def run_screener(
    store: UniverseStore,
    market: MarketDataService,
    config: ScreenerConfig | None = None,
    *,
    as_of: str | None = None,
    include_realtime: bool = False,
    index_history: dict[str, list[Bar]] | None = None,
) -> ScreeningResult:
    """``index_history``：按板块名预加载的指数日线（升序，截至最新交易日）。

    回测时间旅行场景必须传入：store.load_index(max_rows=260) 只回「截至最新日」
    的最近 260 根，回测 early as_of 一切窗口就不足 21 根（误报 L0 降级）。
    传入后 L0 直接复用，_bar_window(as_of) 切出的是 as_of 往前 260 根。
    """
    started = time.perf_counter()
    cfg = config or ScreenerConfig()
    warnings: list[str] = []

    latest = store.latest_trade_date()
    if as_of is not None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(as_of)):
            raise ValueError("as_of 必须为 YYYY-MM-DD")
        as_of_text = str(as_of)
    else:
        as_of_text = latest or date.today().isoformat()
        if latest is None:
            warnings.append("universe 日线缓存为空，请先刷新数据后再运行选股")

    boards = list(cfg.boards)
    rows = store.list_codes(boards)
    rows_by_code = {row.code: row for row in rows}

    # -- L0 大盘同步门 ---------------------------------------------------
    gate_boards: dict[str, dict] = {}
    for board in boards:
        index_code = BOARD_INDEX[board]
        if index_history is not None and board in index_history:
            index_all = index_history[board]
        else:
            index_all = store.load_index(index_code, max_rows=260)
        index_bars = _bar_window(index_all, as_of_text)
        entry: dict[str, Any] = {
            "index": index_code,
            "ret20": None,
            "open": True,  # 指数缺失 → 降级按开门处理
            "date": None,
        }
        if len(index_bars) >= 21 and index_bars[-21].close > 0:
            ret20 = index_bars[-1].close / index_bars[-21].close - 1
            entry = {
                "index": index_code,
                "ret20": _num(ret20),
                "open": bool(ret20 < 0),
                "date": index_bars[-1].date,
            }
        else:
            warnings.append(
                f"{board}指数 {index_code} 日线不足21根（缺失或未回补），L0 按开门降级处理"
            )
        gate_boards[board] = entry
    gate_open = any(entry["open"] for entry in gate_boards.values())
    gate = {"boards": gate_boards, "open": bool(gate_open)}

    funnel = {
        "universe": len(rows),
        "after_l0": 0,
        "after_l1": 0,
        "after_l2": 0,
        "after_l3": 0,
        "eligible": 0,
        "deep_checked": 0,
    }

    # -- L1 A1 超跌 ------------------------------------------------------
    open_boards = {board for board, entry in gate_boards.items() if entry["open"]}
    target_rows = [row for row in rows if row.board in open_boards]
    funnel["after_l0"] = len(target_rows)

    candidates: list[Candidate] = []
    bars_by_code: dict[str, list[Bar]] = {}
    if target_rows:
        # end=当次选股时点：历史回看（回测/指定 as_of）必须取「截至 as_of 的最近
        # 260 根」，否则 tail 会落在 as_of 之后 → 早期时点窗口为空、永远选不出票。
        history = store.load_history(
            [row.code for row in target_rows], max_rows=260, end=as_of_text,
        )
        for row in target_rows:
            bars = _bar_window(history.get(row.code, []), as_of_text)
            if len(bars) < 21:
                continue
            close = bars[-1].close
            base = bars[-21].close
            if close <= 0 or base <= 0:
                continue
            chg20d = close / base - 1
            week52_high = max((bar.high for bar in bars[-250:] if bar.high > 0), default=close)
            if week52_high <= 0:
                continue
            drawdown = 1 - close / week52_high
            if not (chg20d < cfg.chg20_max and close < week52_high * (1 - cfg.week52_drawdown)):
                continue
            prev_close = bars[-1].prev_close
            if prev_close is None or prev_close <= 0:
                prev_close = bars[-2].close if len(bars) >= 2 else close
            day_change = close / prev_close - 1 if prev_close > 0 else 0.0
            window20 = bars[-20:]
            avg_amount_20d = (
                sum(bar.amount for bar in window20) / len(window20) if window20 else 0.0
            )
            bars_by_code[row.code] = bars
            candidates.append(
                Candidate(
                    code=row.code,
                    name=row.name,
                    board=row.board,
                    close=close,
                    prev_close=prev_close,
                    as_of=as_of_text,
                    chg20d=chg20d,
                    week52_high=week52_high,
                    drawdown_52w=drawdown,
                    day_change=day_change,
                    amount=bars[-1].amount,
                    avg_amount_20d=avg_amount_20d,
                    turnover=bars[-1].turnover_rate,
                    sessions=len(bars),
                    score=chg20d,
                    checks=[],
                    vetoed_by=None,
                )
            )
        # 只保留通过 L1 的 Bar（bars_by_code），其余全量 Bar 立即释放，降低后续阶段峰值。
        history = None
    funnel["after_l1"] = len(candidates)

    # -- 实时快照叠加（仅对通过 L1 的票） --------------------------------
    realtime_applied = False
    realtime_quotes = 0
    if include_realtime and candidates and market is not None:
        try:
            quotes = market.fetch_quotes_batch([candidate.code for candidate in candidates])
        except Exception as exc:  # noqa: BLE001 - 快照失败整体降级
            quotes = {}
            warnings.append(f"实时快照拉取失败，已回落到日线口径: {exc}")
        matched = 0
        for candidate in candidates:
            quote = quotes.get(candidate.code)
            if not quote:
                continue
            price_now = _opt_float(quote.get("price"))
            if price_now is None or price_now <= 0:
                continue
            bars = bars_by_code.get(candidate.code, [])
            if len(bars) < 21 or bars[-21].close <= 0:
                continue
            prev_close = _opt_float(quote.get("prev_close")) or candidate.prev_close
            candidate.close = price_now
            candidate.prev_close = prev_close
            candidate.chg20d = price_now / bars[-21].close - 1
            candidate.day_change = price_now / prev_close - 1 if prev_close > 0 else candidate.day_change
            amount = _opt_float(quote.get("amount"))
            if amount is not None:
                candidate.amount = amount
            turnover = _opt_float(quote.get("turnover_rate"))
            if turnover is not None:
                candidate.turnover = turnover
            if candidate.week52_high > 0:
                candidate.drawdown_52w = 1 - price_now / candidate.week52_high
            candidate.score = candidate.chg20d
            candidate.checks.append(
                _check("L1", "实时快照", "pass", f"用腾讯快照 price={price_now:.3f} 重算")
            )
            matched += 1
        if quotes:
            realtime_applied = True
            realtime_quotes = matched
            if matched == 0:
                warnings.append("实时快照无有效价格，已回落到日线口径")
        elif not any("实时快照拉取失败" in item for item in warnings):
            warnings.append("实时快照无返回，已回落到日线口径")

    # -- L2 一票否决（快筛） --------------------------------------------
    blacklist = _normalize_blacklist(cfg.blacklist)
    warnings.append(
        "L2 部分否决项数据源未覆盖，需人工复核：非标审计/资金占用/立案调查/解禁洪峰"
    )
    l2_pass: list[Candidate] = []
    for candidate in candidates:
        row = rows_by_code.get(candidate.code)
        checks = candidate.checks
        vetoed_by: str | None = None

        def add_rule(rule: str, violated: bool, detail: str) -> None:
            nonlocal vetoed_by
            status = "veto" if violated else "pass"
            checks.append(_check("L2", rule, status, detail))
            if violated and vetoed_by is None:
                vetoed_by = rule

        add_rule(
            "ST/*ST",
            bool(cfg.exclude_st and is_st_name(candidate.name)),
            candidate.name,
        )
        add_rule(
            "退市风险",
            bool(cfg.exclude_delisting and is_delisting_name(candidate.name)),
            candidate.name,
        )
        add_rule(
            "科创板未盈利(-U)",
            bool(cfg.exclude_unprofitable_u and is_unprofitable_u_name(candidate.name)),
            candidate.name,
        )
        listing_sessions = candidate.sessions
        if row is not None and row.ipo_date:
            bars = bars_by_code.get(candidate.code, [])
            listing_sessions = sum(1 for bar in bars if bar.date >= row.ipo_date)
        add_rule(
            f"上市不足{cfg.min_listing_sessions}个交易日",
            bool(cfg.min_listing_sessions > 0 and listing_sessions < cfg.min_listing_sessions),
            f"上市{listing_sessions}个交易日" if row is not None and row.ipo_date else f"库内{listing_sessions}根",
        )
        add_rule(
            f"当日涨幅>{cfg.day_change_max:.0%}",
            bool(candidate.day_change > cfg.day_change_max),
            f"{candidate.day_change:.2%}",
        )
        add_rule(
            "黑名单",
            candidate.code in blacklist,
            "命中黑名单" if candidate.code in blacklist else "",
        )
        # 数据源未覆盖的否决项：明确标 unknown，人工复核。
        checks.append(
            _check(
                "L2",
                "非标审计/资金占用/立案调查/解禁洪峰",
                "unknown",
                "数据源未覆盖，需人工复核",
            )
        )
        candidate.vetoed_by = vetoed_by
        if vetoed_by is not None:
            continue
        l2_pass.append(candidate)
    funnel["after_l2"] = len(l2_pass)

    # -- L3 容量 ---------------------------------------------------------
    pool: list[Candidate] = []
    for candidate in l2_pass:
        checks = candidate.checks
        amount_ok = candidate.amount >= cfg.min_amount
        avg_ok = candidate.avg_amount_20d >= cfg.min_avg_amount_20d
        checks.append(
            _check(
                "L3",
                f"当日成交额≥{cfg.min_amount / 1e8:.1f}亿",
                "pass" if amount_ok else "veto",
                f"{candidate.amount / 1e8:.2f}亿",
            )
        )
        checks.append(
            _check(
                "L3",
                f"20日均额≥{cfg.min_avg_amount_20d / 1e8:.1f}亿",
                "pass" if avg_ok else "veto",
                f"{candidate.avg_amount_20d / 1e8:.2f}亿",
            )
        )
        if amount_ok and avg_ok:
            pool.append(candidate)
        else:
            candidate.vetoed_by = "L3 容量不足"
    pool.sort(key=lambda item: (item.chg20d, item.code))
    funnel["after_l3"] = len(pool)

    # -- L4 排序取第一 + 财务深筛 ---------------------------------------
    eligible: list[Candidate] = []
    fundamental_status = "disabled"
    if not cfg.enable_fundamental_veto:
        eligible = list(pool)
        warnings.append("财务深筛已关闭（enable_fundamental_veto=False），L4 不否决")
    else:
        fundamental = FundamentalService(store)
        deep_checked = 0
        applied = 0
        unavailable = 0
        try:
            for candidate in pool:
                if deep_checked >= cfg.max_deep_checks:
                    break
                candidate.deep_checked = True
                deep_checked += 1
                try:
                    info = fundamental.check(candidate.code)
                except Exception as exc:  # noqa: BLE001 - 深筛失败降级为未查
                    info = dict(FUNDAMENTAL_UNAVAILABLE)
                    warnings.append(f"财务深筛异常（{candidate.code}）: {exc}")
                if info.get("checked"):
                    applied += 1
                    veto = info.get("veto")
                    if veto:
                        candidate.vetoed_by = str(veto)
                        candidate.checks.append(_check("L2", "财务深筛", "veto", str(veto)))
                        continue
                    candidate.checks.append(
                        _check(
                            "L2",
                            "财务深筛",
                            "pass",
                            f"净利={info.get('net_profit')} 营收={info.get('revenue')}",
                        )
                    )
                else:
                    unavailable += 1
                    candidate.checks.append(
                        _check("L2", "财务深筛", "unknown", "财务数据不可用，未否决")
                    )
                eligible.append(candidate)
        finally:
            fundamental.close()
        # 超出深筛上限的候选没有被任何规则否决，保留为 eligible（deep_checked=false），
        # 避免池内轮动因上限截断而少票。
        unchecked = pool[deep_checked:]
        if unchecked:
            eligible.extend(unchecked)
            warnings.append(
                f"深度财务校验仅覆盖前 {cfg.max_deep_checks} 只，其余 {len(unchecked)} 只未深筛（deep_checked=false）"
            )
        funnel["deep_checked"] = deep_checked
        if unavailable and applied:
            fundamental_status = "partial"
        elif unavailable:
            fundamental_status = "unavailable"
        else:
            fundamental_status = "applied"
        if unavailable:
            warnings.append("财务数据不可用，L2 深筛降级（未覆盖的票不否决，需人工复核）")

    first = eligible[0] if eligible else None
    if not gate_open:
        warnings.append("L0 大盘门关闭，本轮空仓等待")
    elif not pool:
        warnings.append("L0–L3 后无候选，本轮空仓等待")
    elif first is None:
        warnings.append("候选池无通过财务深筛的标的，本轮无可用第一名")
    funnel["eligible"] = len(eligible)

    elapsed = round(time.perf_counter() - started, 3)
    return ScreeningResult(
        id="run_" + uuid.uuid4().hex[:12],
        created_at=utc_now(),
        as_of=as_of_text,
        config=cfg.to_dict(),
        gate=gate,
        funnel=funnel,
        pool=pool,
        eligible=eligible,
        first=first,
        warnings=warnings,
        data_info={
            "universe_db": str(getattr(store, "path", "")),
            "coverage": store.coverage(),
            "realtime": bool(realtime_applied),
            "realtime_quotes": int(realtime_quotes),
            "fundamental": fundamental_status,
            "elapsed_seconds": elapsed,
        },
        elapsed_seconds=elapsed,
    )
