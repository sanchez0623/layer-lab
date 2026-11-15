from __future__ import annotations

import csv
import io
import json
import os
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import requests
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .backtest import BacktestEngine, FunnelRotationBacktest, run_backtest, run_funnel_backtest
from .backtest_jobs import BacktestBusyError, BacktestJobManager, Execution
from .beta_sleeve import entry_allowed, exit_reason, reentry_cooldown_ready, target_quantity
from .broker import BrokerAccount
from .calendar_api import FuyaoCalendar
from .data import dataset_from_bars, read_csv_bars, save_demo_datasets
from .domain import Bar, FeeConfig, OrderRequest, STRATEGY_RULES, StrategyConfig, money, price, utc_now
from .market import (
    INTRADAY_BAR_MINUTES,
    INTRADAY_TIMEFRAME,
    REALTIME_INTERVAL_SECONDS,
    TENCENT_INTRADAY_MAX_BARS,
    MarketDataService,
    normalize_symbol,
)
from .pagination import paginate, page_params
from .quan_data import QuanRefresher, QuanStore, quan_data_dir
from .rotation import rescreen_and_advance, set_from_run as set_rotation_from_run, set_pool as set_rotation_pool
from .screener import ScreenerConfig, ScreenerService, ScreeningResult
from .storage import SQLiteStorage
from .strategy import LayeredTStrategy
from .trading_rules import NOT_MODELED, all_board_rules, board_of, price_cage_bounds, rules_for


BASE_DIR = Path(__file__).resolve().parents[1]
FEE_SETTING_KEY = "fee_config_v1"
FEE_CONFIG_VERSION = "v1"

# 可复用的日线数据集来源：quan_parquet（当前）与 baostock（旧链路）。
DAILY_DATASET_SOURCES = ("quan_parquet", "baostock")


def is_daily_dataset(dataset, symbol: str | None = None) -> bool:
    if dataset is None or dataset.timeframe != "daily":
        return False
    if symbol is not None and dataset.symbol != symbol:
        return False
    return dataset.source in DAILY_DATASET_SOURCES


def trading_minutes_elapsed(timestamp: datetime) -> int:
    """当日已开盘分钟数（9:30-11:30 / 13:00-15:00），用于把当日累计量折算为盘中当量。"""
    clock = timestamp.hour * 60 + timestamp.minute
    morning = min(120, max(0, clock - 9 * 60 - 30))
    afternoon = min(120, max(0, clock - 13 * 60)) if clock >= 13 * 60 else 0
    return max(1, morning + afternoon)


def trading_intraday_bars_elapsed(timestamp: datetime) -> int:
    """当日已收盘的 5 分钟 K 线数量，用于把当日累计量折算为 5 分钟当量。"""
    return max(1, (trading_minutes_elapsed(timestamp) + INTRADAY_BAR_MINUTES - 1) // INTRADAY_BAR_MINUTES)


def intraday_bucket(timestamp: datetime, minutes: int = INTRADAY_BAR_MINUTES) -> datetime:
    """把时间向下取整到盘中 K 线边界（腾讯 5 分钟 bar 的 timestamp 是区间结束时间）。"""
    return timestamp.replace(minute=timestamp.minute - timestamp.minute % minutes, second=0, microsecond=0)


def latest_closed_intraday_bar(bars: list[Bar], now: datetime) -> Bar | None:
    """返回截至 ``now`` 已收盘的最后一根盘中 K 线；不含仍在形成的当前 bar。"""
    cutoff = intraday_bucket(now)
    closed = [bar for bar in bars if bar.timestamp <= cutoff]
    return closed[-1] if closed else None


def snapshot_execution_bar(
    bar: Bar, bar_volume: float | None = None, bar_amount: float | None = None,
) -> Bar:
    """把腾讯实时快照转换成**仅用于撮合**的 5 分钟 bar。

    快照的 open/high/low 是全天口径（今开 / 当日最高 / 当日最低），volume/amount
    是当日累计值。直接交给 process_bar 会让市价单按今日开盘价成交、限价单按全天
    区间判定、成交量参与度额度被全天量放大。策略信号使用已收盘的 5 分钟 bar，
    这里的 ``bar_volume`` / ``bar_amount`` 应是最近一根已收盘 5 分钟 bar 的量额。
    """
    reference = bar.close
    bid = bar.bid if bar.bid and bar.bid > 0 else reference
    ask = bar.ask if bar.ask and bar.ask > 0 else reference
    elapsed = trading_intraday_bars_elapsed(bar.timestamp)
    volume = bar_volume if bar_volume and bar_volume > 0 else bar.volume / elapsed
    amount = bar_amount if bar_amount and bar_amount > 0 else bar.amount / elapsed
    return Bar(
        timestamp=bar.timestamp, symbol=bar.symbol, open=reference,
        high=max(reference, ask, bid), low=min(reference, ask, bid), close=reference,
        volume=volume, amount=money(amount), timeframe=INTRADAY_TIMEFRAME,
        turnover_rate=bar.turnover_rate, prev_close=bar.prev_close,
        bid=bar.bid, ask=bar.ask,
    )


class BacktestRequest(BaseModel):
    name: str = "分层加仓做T回测"
    dataset_id: str
    intraday_dataset_id: str | None = None
    index_dataset_id: str | None = None
    initial_cash: float = Field(default=4_000_000.0, gt=0)
    start_date: str | None = None
    end_date: str | None = None
    config: dict = Field(default_factory=dict)
    fees: dict | None = None  # 万分之口径的自定义费用，覆盖全局默认


class FunnelBacktestRequest(BaseModel):
    """选股漏斗轮动回测请求；screener 为 ScreenerConfig 局部覆盖（未知键→400）。"""

    name: str = "选股漏斗轮动回测"
    initial_cash: float = Field(default=4_000_000.0, gt=0)
    start_date: str | None = None
    end_date: str | None = None
    config: dict = Field(default_factory=dict)
    fees: dict | None = None
    screener: dict | None = None


class BacktestJobRequest(BaseModel):
    """后台回测任务请求：mode 决定单标的还是漏斗轮动，其余字段同对应同步请求。"""

    mode: Literal["single", "funnel"] = "single"
    name: str = "回测"
    dataset_id: str | None = None
    intraday_dataset_id: str | None = None
    index_dataset_id: str | None = None
    initial_cash: float = Field(default=4_000_000.0, gt=0)
    start_date: str | None = None
    end_date: str | None = None
    config: dict = Field(default_factory=dict)
    fees: dict | None = None
    screener: dict | None = None


class PaperAccountRequest(BaseModel):
    name: str = "默认模拟账户"
    initial_cash: float = Field(default=4_000_000.0, gt=0)
    dataset_id: str | None = None
    auto_strategy: bool = False
    config: dict = Field(default_factory=dict)
    fees: dict | None = None


class FeeConfigRequest(BaseModel):
    """PUT /api/fees 的局部更新：未提供的字段保留当前值。"""

    model_config = ConfigDict(extra="forbid")

    commission_rate: float | None = Field(default=None, ge=0, le=100)
    min_commission: float | None = Field(default=None, ge=0, le=10_000)
    stamp_tax_rate: float | None = Field(default=None, ge=0, le=100)
    handling_fee_rate: float | None = Field(default=None, ge=0, le=100)
    regulatory_fee_rate: float | None = Field(default=None, ge=0, le=100)
    transfer_fee_rate: float | None = Field(default=None, ge=0, le=100)


class AccountFeesRequest(BaseModel):
    fees: dict | None = None


class AutoStrategyRequest(BaseModel):
    enabled: bool


class AccountConfigRequest(BaseModel):
    """POST /api/paper/accounts/{id}/config 的局部更新：只接受策略（非费率）字段。

    费率字段（commission_rate 等）归 /fees 端点管，这里显式拒绝，避免两处写同一
    状态打架。改完立刻生效：策略每根 K 线都从 account.config 读取（如做T净利止盈
    t_target_net_spread、T+1截止 t_exit_cutoff），下一根 K 的操作即用新值。
    """

    model_config = ConfigDict(extra="forbid")

    enable_t: bool | None = None
    t_target_net_spread: float | None = Field(default=None, ge=0, le=1)
    t_exit_cutoff: str | None = Field(default=None, pattern=r"^\d{1,2}:\d{2}$")
    # 超期降阈：持有 ≥N 交易日后止盈降到 override。两项需同时配置才生效
    # （strategy._effective_take_profit 任一为 None 即维持原止盈）。
    max_holding_days: int | None = Field(default=None, ge=1, le=250)
    holding_tp_override: float | None = Field(default=None, ge=0, le=1)
    # 指数 beta 腿（ETF）：空仓期间用创业板 ETF 承接 beta，选到个股后卖出换股。
    etf_beta_enabled: bool | None = None
    etf_symbol: str | None = Field(default=None, pattern=r"^(sh|sz)\.\d{6}$")
    etf_position_pct: float | None = Field(default=None, gt=0, le=1)
    etf_entry_ma_days: int | None = Field(default=None, ge=2, le=250)
    etf_exit_ma_days: int | None = Field(default=None, ge=2, le=250)
    etf_hard_stop_pct: float | None = Field(default=None, gt=0, lt=1)
    # 入场保护：不追高（相对入场均线偏离上限，0 关闭）+ 清仓后冷却（交易日，0 关闭）。
    etf_entry_max_deviation: float | None = Field(default=None, ge=0, lt=1)
    etf_reentry_cooldown_days: int | None = Field(default=None, ge=0, le=60)


class ApplyConfigRequest(BaseModel):
    """POST /api/paper/accounts/{id}/apply-config：把回测配置整套应用到模拟账户。

    回测页调参验证后，一键把同一套配置（策略字段 + 费率）同步到模拟盘当前账户，
    免去在模拟盘手动重配一遍。数据结构与回测提交一致：
    - ``config``：策略字段（前端已 stripFeeFields 剥离费率，避免两处写同一费率键打架）。
    - ``fees``：费率（万分之口径），走 merge_fees 统一换算成小数比例。
    两者合并后整体覆盖账户 config，作为「下一根 K 起的新操作」口径（与既有 config/fees
    端点一致，不追溯历史记账）。
    """

    config: dict[str, Any] = Field(default_factory=dict)
    fees: dict[str, Any] | None = None


class PaperOrderRequest(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    quantity: int = Field(gt=0)
    limit_price: float | None = Field(default=None, gt=0)
    purpose: Literal["PERMANENT", "T"] = "PERMANENT"


class TickRequest(BaseModel):
    symbol: str
    timestamp: str
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    amount: float | None = Field(default=None, ge=0)
    turnover_rate: float | None = None
    prev_close: float | None = Field(default=None, gt=0)
    bid: float | None = Field(default=None, gt=0)
    ask: float | None = Field(default=None, gt=0)


class AdvanceRequest(BaseModel):
    bars: int = Field(default=1, ge=1, le=1000)


class MarketFetchRequest(BaseModel):
    symbol: str
    start_date: str | None = None
    end_date: str | None = None
    include_intraday: bool = True


class LiveTickRequest(BaseModel):
    symbol: str


class ScreenerRefreshRequest(BaseModel):
    boards: list[str] | None = None
    codes: list[str] | None = None
    lookback_days: int = Field(default=400, ge=60, le=2000)
    force: bool = False


class ScreenerRunRequest(BaseModel):
    as_of: str | None = None
    include_realtime: bool = False
    config: dict | None = None


class ScreenerPrepareRequest(BaseModel):
    target: Literal["paper", "backtest"]
    candidate_code: str | None = None
    account_id: str | None = None
    auto_strategy: bool = True
    rotation: bool = True


class RotationRequest(BaseModel):
    enabled: bool
    run_id: str | None = None
    pool: list[dict] | None = None


def _slim_rotation_trail(report: dict) -> dict:
    """把主报告里的大体积 ``rotation_trail`` 抽走，只留条数。

    换票轨迹改由 ``GET /api/backtests/{id}/rotation-trail`` 分页下发，
    主报告（创建 / 读取）不再一次性携带全量轨迹，避免序列化 / 传输 / 渲染膨胀，
    也避免 data_info 概览网格把整条轨迹 JSON.stringify 进 DOM。
    全量数据仍完整落库（save_backtest 在瘦身处之前调用）。
    """
    info = report.get("data_info")
    if isinstance(info, dict):
        trail = info.get("rotation_trail")
        if isinstance(trail, list) and trail:
            info["rotation_trail_total"] = len(trail)
            info["rotation_trail"] = []
    return report


# 主报告里随数据量变长的大数组（成交 / 委托 / 轮次 / 事件）。
# 读取 / 创建响应只保留条数，全量改由 /records/{kind} 分页下发，
# 避免长回测一次性序列化 / 传输 / 渲染成千上万行导致 REPORT 卡顿。
# 实测 4ecfe242 报告 payload 19MB：orders 13MB + events 5.9MB，其余字段均 <0.15MB。
_RECORD_FIELDS = {
    "trades": "trades_total",
    "orders": "orders_total",
    "rounds": "rounds_total",
    "events": "events_total",
}


def _slim_records(report: dict) -> dict:
    """把主报告里的全量 ``trades`` / ``orders`` / ``rounds`` / ``events`` 抽走，只留条数。

    全量数据仍完整落库（save_backtest 在瘦身处之前调用），分页端点从库里读全量再切片。
    events 前端当前不渲染，但同样可能上万条，一并瘦身避免白白下发。
    """
    for field, total_key in _RECORD_FIELDS.items():
        rows = report.get(field)
        if isinstance(rows, list) and rows:
            report[total_key] = len(rows)
            report[field] = []
    return report


# 用途（purpose）的中文展示标签，与前端 lib/format.ts purposeLabel 口径一致；
# 用途过滤既接受原始枚举值（PERMANENT/T/BETA，大小写不敏感），也接受这里的中文标签。
_PURPOSE_LABELS: dict[str, str] = {
    "permanent": "永久仓",
    "t": "t仓",
    "beta": "etf对冲",
    "t_buy": "t仓买入",
    "t_sell": "t仓卖出",
    "open": "建仓",
    "close": "清仓",
    "take_profit": "止盈",
}

# 回测成交实际出现的用途枚举（小写）；purpose 过滤只认这三种，避免 't' 误命中 permanent/beta。
_PURPOSE_ENUMS = {"permanent", "t", "beta"}

# 标签 → 枚举（小写）反查表：把「永久仓 / T仓 / ETF对冲」规范化为枚举值。
_PURPOSE_LABEL_TO_ENUM = {label: enum for enum, label in _PURPOSE_LABELS.items()}


def _normalize_purpose(value: str) -> str | None:
    """把用途参数规范化为小写枚举值：接受原始枚举或中文标签；无法识别返回 None。"""
    query = (value or "").strip().lower()
    if not query:
        return None
    if query in _PURPOSE_ENUMS:
        return query
    return _PURPOSE_LABEL_TO_ENUM.get(query)


def _normalize_side(value: str) -> str | None:
    """把方向参数规范化为大写 BUY/SELL；无法识别返回 None。"""
    side = (value or "").strip().upper()
    return side if side in {"BUY", "SELL"} else None


def _record_matches(row: dict, *, symbol_q: str, purpose_q: str, side_q: str) -> bool:
    """单行是否命中成交明细筛选：symbol 子串 + purpose/side 全等（AND）。

    ``purpose_q`` / ``side_q`` 须已由 :func:`_normalize_purpose` /
    :func:`_normalize_side` 规范化；空字符串表示该条件不生效。
    前端旧报告本地回退路径 lib/recordFilters.ts 与此口径保持一致。
    """
    if symbol_q and symbol_q not in str(row.get("symbol") or "").lower():
        return False
    if purpose_q and str(row.get("purpose") or "").strip().lower() != purpose_q:
        return False
    if side_q and str(row.get("side") or "").strip().upper() != side_q:
        return False
    return True


def _filter_records(
    rows: list[dict], *, symbol_q: str = "", purpose_q: str = "", side_q: str = ""
) -> list[dict]:
    """按成交明细筛选条件过滤全量行（条件间为 AND，空条件不生效）。"""
    return [
        row for row in rows
        if _record_matches(row, symbol_q=symbol_q, purpose_q=purpose_q, side_q=side_q)
    ]


def _facet_counts(
    rows: list[dict], *, symbol_q: str, purpose_q: str, side_q: str
) -> dict[str, dict[str, int]]:
    """成交明细分面计数：purpose facet 剔除自身条件、其余条件生效（side facet 同理）。

    供前端筛选下拉展示「永久仓 (101) / T仓 (48) / ETF对冲 (0)」并禁用 0 条选项。
    复杂度 O(n)，n 为报告成交条数；仅在 trades 明细端点按请求计算，不做缓存。
    """
    purpose_counts: dict[str, int] = {}
    for row in _filter_records(rows, symbol_q=symbol_q, purpose_q="", side_q=side_q):
        key = str(row.get("purpose") or "").strip().upper()
        if key:
            purpose_counts[key] = purpose_counts.get(key, 0) + 1
    side_counts: dict[str, int] = {}
    for row in _filter_records(rows, symbol_q=symbol_q, purpose_q=purpose_q, side_q=""):
        key = str(row.get("side") or "").strip().upper()
        if key:
            side_counts[key] = side_counts.get(key, 0) + 1
    return {"purpose": purpose_counts, "side": side_counts}


def create_app() -> FastAPI:
    data_dir = Path(os.environ.get("WB_DATA_DIR", str(BASE_DIR / "data")))
    upload_dir = data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    storage = SQLiteStorage(os.environ.get("WB_DB_PATH", str(data_dir / "app.db")))
    application = FastAPI(
        title="A股分层加仓做T回测与模拟交易",
        version="2.1.2",
        description="历史行情来自 quan_quant parquet 数据目录（前复权），实时快照与5分钟K线来自腾讯行情；本地撮合，不连接券商。",
    )
    application.state.storage = storage
    application.state.data_dir = data_dir
    # 交易日历（同花顺 fuyao，含未来交易日）：周末/节假日不轮询的权威来源。
    application.state.fuyao_calendar = FuyaoCalendar(data_dir)

    # 行情数据源选择：
    # - 默认接入 quan_quant 的 parquet 数据目录（WB_QUAN_DATA_DIR 覆盖；空串=关闭）；
    # - quan 源只读历史数据，实时快照仍走腾讯；数据目录不可用时回落旧的 baostock/universe.db 链路。
    quan_dir = quan_data_dir()
    quan_store: QuanStore | None = None
    if quan_dir is not None:
        try:
            quan_store = QuanStore(quan_dir, cache_db=data_dir / "quan_cache.db")
        except ImportError as exc:
            raise RuntimeError(str(exc)) from exc
        except ValueError:
            quan_store = None
    application.state.quan_store = quan_store
    application.state.market_service = MarketDataService(data_dir / "cache", quan=quan_store)
    # 选股服务挂在 application.state，数据目录、行情服务与数据源由 create_app 统一注入。
    application.state.screener_service = ScreenerService(
        data_dir,
        application.state.market_service,
        store=quan_store,
        refresher=QuanRefresher(quan_store) if quan_store is not None else None,
    )

    def build_backtest_execution(params, hooks, resume_state, checkpoint):
        """按任务参数重建引擎（断点续传时同样走此工厂，保证口径一致）。"""
        mode = params.get("mode", "single")
        config = resolve_config(params.get("config") or {}, params.get("fees"))
        name = params.get("name") or "回测"
        if mode == "funnel":
            screener = ScreenerConfig.from_dict({**(params.get("screener") or {}), "enable_fundamental_veto": False})
            service = screener_service()
            engine = FunnelRotationBacktest(
                store=service.store, market=application.state.market_service,
                screener_config=screener, initial_cash=params["initial_cash"],
                config=config, start_date=params.get("start_date"), end_date=params.get("end_date"),
                name=name, quan=getattr(application.state, "quan_store", None), hooks=hooks,
            )

            def on_complete(report):
                report["data_info"]["daily_source"] = getattr(service.store, "provider", "universe_db")
                report["data_info"]["universe_db"] = str(service.store.path)
                storage.save_backtest(report)
                return _slim_records(_slim_rotation_trail(report))

            return Execution(engine, on_complete)

        daily = load_dataset(params["dataset_id"], {"daily"})
        intraday = load_dataset(params["intraday_dataset_id"], {"1m", "5m", "tick"}) if params.get("intraday_dataset_id") else None
        index = load_dataset(params["index_dataset_id"], {"daily"}) if params.get("index_dataset_id") else None
        daily_dataset = get_dataset(params["dataset_id"])
        intraday_dataset = get_dataset(params["intraday_dataset_id"]) if params.get("intraday_dataset_id") else None
        index_dataset = get_dataset(params["index_dataset_id"]) if params.get("index_dataset_id") else None
        engine = BacktestEngine(
            daily=daily, intraday=intraday, index=index,
            initial_cash=params["initial_cash"], config=config,
            start_date=params.get("start_date"), end_date=params.get("end_date"),
            name=name, hooks=hooks,
        )

        def on_complete(report):
            report["data_info"].update({
                "daily_source": daily_dataset.source,
                "intraday_source": intraday_dataset.source if intraday_dataset else None,
                "index_source": index_dataset.source if index_dataset else None,
            })
            storage.save_backtest(report)
            return _slim_records(report)

        return Execution(engine, on_complete)

    backtest_jobs = BacktestJobManager(storage, build_backtest_execution)
    application.state.backtest_jobs = backtest_jobs
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5175", "http://127.0.0.1:5175",
            "http://localhost:5180", "http://127.0.0.1:5180",
            "http://localhost:5173", "http://127.0.0.1:5173",
        ],
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
    )

    @application.exception_handler(ValueError)
    async def value_error_handler(_, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    def get_dataset(dataset_id: str):
        dataset = storage.get_dataset(dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="数据集不存在")
        return dataset

    def load_dataset(dataset_id: str, expected: set[str] | None = None) -> list[Bar]:
        dataset = get_dataset(dataset_id)
        if expected and dataset.timeframe not in expected:
            raise ValueError(f"数据集{dataset.name}粒度{dataset.timeframe}不符合要求{sorted(expected)}")
        if not dataset.file_path or not Path(dataset.file_path).exists():
            raise HTTPException(status_code=410, detail="数据集CSV文件已丢失")
        return read_csv_bars(dataset.file_path, dataset.timeframe, dataset.symbol)

    def save_paper(account: BrokerAccount) -> dict:
        internal = account.to_dict(include_internal=True)
        storage.save_paper(internal)
        return account.to_dict()

    def get_paper(account_id: str) -> BrokerAccount:
        state = storage.get_paper(account_id)
        if state is None:
            raise HTTPException(status_code=404, detail="模拟账户不存在")
        return BrokerAccount.from_dict(state)

    def load_fee_config() -> FeeConfig:
        raw = storage.get_setting(FEE_SETTING_KEY)
        if not raw:
            return FeeConfig()
        try:
            return FeeConfig.from_dict(json.loads(raw))
        except (ValueError, TypeError):
            return FeeConfig()

    def save_fee_config(fees: FeeConfig) -> None:
        storage.set_setting(FEE_SETTING_KEY, json.dumps(fees.to_dict(), ensure_ascii=False))
        storage.set_setting(FEE_SETTING_KEY + "_at", utc_now())

    def fee_response(fees: FeeConfig) -> dict:
        return {
            "version": FEE_CONFIG_VERSION,
            "fees": fees.to_dict(),
            "defaults": FeeConfig().to_dict(),
            "strategy_patch": fees.to_strategy_patch(),
            "note": "费率按万分之输入，分项相加；最低佣金仅适用于佣金项。",
            "updated_at": storage.get_setting(FEE_SETTING_KEY + "_at"),
        }

    def merge_fees(raw_fees: dict | None) -> FeeConfig:
        """以全局配置为基准做局部覆盖；未提供的项不会被系统默认值悄悄替换。"""
        merged = load_fee_config().to_dict()
        if raw_fees:
            merged.update(raw_fees)
        return FeeConfig.from_dict(merged)

    def resolve_config(raw_config: dict | None, raw_fees: dict | None) -> StrategyConfig:
        """全局费用配置打底 → 本次请求 fees 覆盖 → 显式 config 字段最高优先级。"""
        return StrategyConfig.from_dict({**merge_fees(raw_fees).to_strategy_patch(), **(raw_config or {})})

    def standard_symbol(raw: str) -> str:
        try:
            return normalize_symbol(raw).standard
        except ValueError:
            # 单元测试和本地合成数据使用 SYNTH001 等非真实代码；保留原样。
            return raw.strip().upper()

    def screener_service() -> ScreenerService:
        """选股服务句柄；测试可直接替换 application.state.screener_service。"""
        return application.state.screener_service

    def normalize_rotation_pool(raw_pool: list[dict]) -> list[dict]:
        pool: list[dict] = []
        for position, item in enumerate(raw_pool or []):
            if not isinstance(item, dict):
                raise ValueError(f"轮动池第 {position} 项不是对象")
            raw_symbol = item.get("symbol") or item.get("code")
            if not raw_symbol:
                raise ValueError(f"轮动池第 {position} 项缺少 symbol")
            entry = dict(item)
            entry["symbol"] = standard_symbol(str(raw_symbol))
            entry.setdefault("name", "")
            entry.setdefault("chg20d", None)
            entry.setdefault("board", "")
            pool.append(entry)
        return pool

    def ensure_daily_dataset(symbol: str) -> str | None:
        """换票后保证新标的有可回放的日线数据集，返回 dataset_id。"""
        for dataset in storage.list_datasets():
            if (
                dataset.timeframe == "daily" and dataset.symbol == symbol
                and dataset.file_path and Path(dataset.file_path).exists()
            ):
                return dataset.id
        try:
            bundle = application.state.market_service.fetch_history_bundle(
                symbol, include_intraday=False,
            )
        except (requests.RequestException, ValueError):
            return None
        if not bundle.get("daily"):
            return None
        for key in ("daily", "index"):
            dataset = bundle.get(key)
            if dataset is not None:
                storage.save_dataset(dataset)
        return bundle["daily"].id

    def rotation_screener_config(account: BrokerAccount) -> ScreenerConfig | None:
        """账户轮动配置 → 本轮重筛配置；旧账户/脏配置回落到默认配置。"""
        try:
            return ScreenerConfig.from_dict(account.rotation_config)
        except (ValueError, TypeError):
            return None

    def maybe_advance_rotation(account: BrokerAccount, bar: Bar) -> dict | None:
        """live-tick 轮动接线：清仓后重新运行五层选股，取当时第一名。

        只有「刚结束一轮」（apply_bar 里清仓归零时置 rotation_pending_advance）
        且空仓、tick 的正好是当前轮动标的时处理，每个交易日最多重筛一次。
        """
        if not (account.auto_strategy and account.rotation_enabled):
            return None
        if not account.rotation_pending_advance:
            return None
        config = rotation_screener_config(account)

        def runner() -> dict:
            # 重筛用最新日线（include_realtime=False），结果落库供历史回看。
            result = screener_service().run(config=config, include_realtime=False)
            payload = result.to_dict()
            storage.save_screener_run(payload)
            return payload

        event = rescreen_and_advance(
            account, runner, today=bar.date, current_symbol=bar.symbol,
        )
        if event and event.get("advanced"):
            dataset_id = ensure_daily_dataset(event["symbol"])
            if dataset_id and dataset_id != account.dataset_id:
                account.dataset_id = dataset_id
                account.cursor = 0
        return event

    def apply_bar(
        account: BrokerAccount, bar: Bar, daily_history: list[Bar] | None = None,
        index_history: list[Bar] | None = None, recent_bars: list[Bar] | None = None,
        daily_close: bool = False, execution_bar: Bar | None = None,
        signal_bar: Bar | None = None, detect_intraday: bool = True,
    ) -> None:
        """推进一根行情。

        live-tick 路径把「最近一根已收盘 5 分钟 bar」作为 ``signal_bar`` 交给策略，
        快照 ``bar`` 只负责撮合与日终信号；``detect_intraday=False`` 时（当日还没有
        已收盘 5 分钟 bar）跳过盘中检测，避免用未完成 bar 反复计数。
        """
        strategy: LayeredTStrategy | None = None
        is_new_day = account.current_date != bar.date
        if account.auto_strategy:
            strategy = LayeredTStrategy(account.config)
            strategy.load_state(account.strategy_state)
            if is_new_day:
                strategy.start_day(bar.timeframe != "daily")

        account.new_day(bar.date)
        fills = account.process_bar(execution_bar or bar, modes={"NEXT_BAR", "NEXT_OPEN"})
        account.mark(bar.symbol, bar.close)
        position = account.position(bar.symbol)

        if strategy is not None:
            session_index = max(0, len([item for item in (daily_history or []) if item.date <= bar.date]) - 1)
            for fill in fills:
                strategy.on_fill(fill, session_index)
            if position.total_qty == 0 and strategy.liquidating:
                strategy.reset_round(account)
                account.last_daily_signal_date = None
                # 一轮刚结束：允许下一次 tick 把轮动推进到池内下一只。
                account.rotation_pending_advance = True
            elif (
                position.total_qty == 0 and account.sequence_index != 0
                and not strategy.liquidating
                and not any(o.status in {"PENDING", "PARTIALLY_FILLED"} for o in account.orders)
            ):
                # 兜底：持仓已平但清仓标志（liquidating）未置位的路径（尾盘轧平/手动卖/
                # 外部清仓等），会让 reset_round 永不触发，sequence_index 卡死在中间值，
                # on_daily 的首仓条件（total_qty==0 AND sequence_index==0）永不满足，
                # 账户再也建不起仓。这里在「确认空仓 + 无在途委托」时强制收尾一轮，
                # 让序列归零、账户恢复建仓能力。不等 liquidating 是因为它可能永不到来。
                strategy.reset_round(account)
                account.last_daily_signal_date = None
                account.rotation_pending_advance = True

            if bar.timeframe != "daily" and detect_intraday:
                previous_daily = [item for item in (daily_history or []) if item.date < bar.date]
                detection_bar = signal_bar or bar
                recent = list(recent_bars or [])
                if not recent or recent[-1].timestamp != detection_bar.timestamp:
                    recent.append(detection_bar)
                intraday_requests = strategy.on_intraday(
                    previous_daily, recent, detection_bar, account,
                    index_history=index_history, session_index=session_index,
                )
                for request in intraday_requests:
                    if request.quantity > 0:
                        account.submit_split(request, bar.timestamp)

            if daily_close:
                daily_close_history = [item for item in (daily_history or []) if item.date <= bar.date]
                if daily_close_history and daily_close_history[-1].date != bar.date and bar.timeframe != "daily":
                    daily_close_history.append(Bar(
                        timestamp=bar.timestamp, symbol=bar.symbol, open=bar.open, high=bar.high,
                        low=bar.low, close=bar.close, volume=bar.volume, amount=bar.amount,
                        timeframe="daily", turnover_rate=bar.turnover_rate, prev_close=bar.prev_close,
                    ))
                if daily_close_history and account.last_daily_signal_date != bar.date:
                    index_close_history = [
                        item for item in (index_history or []) if item.date <= bar.date
                    ]
                    daily_requests = strategy.on_daily(
                        daily_close_history, index_close_history, account, session_index,
                        bar.timeframe != "daily",
                    )
                    for request in daily_requests:
                        if request.quantity > 0:
                            account.submit_split(request, bar.timestamp)
                    account.last_daily_signal_date = bar.date
            account.strategy_state = strategy.state_dict()
            account.armed_state = "ARMED" if strategy.armed else ("EXIT" if strategy.liquidating else "OFF")
            return

        if position.permanent_cost > 0:
            permanent_return = (
                position.permanent_qty * bar.close - position.permanent_cost
            ) / position.permanent_cost
            if permanent_return >= account.config.price_armed:
                account.armed_state = "ARMED"
            elif permanent_return < account.config.price_disarm:
                account.armed_state = "OFF"

    # ------------------------------------------------------------------
    # 后端定时驱动：live-tick 由进程内调度器推进，不再依赖前端定时器
    # ------------------------------------------------------------------
    SCHEDULER_INTERVAL_SECONDS = REALTIME_INTERVAL_SECONDS
    scheduler_enabled = os.environ.get("WB_SCHEDULER", "1") != "0"
    scheduler_state: dict = {
        "enabled": scheduler_enabled,
        "running": False,
        "interval_seconds": SCHEDULER_INTERVAL_SECONDS,
        "last_slot": None,
        "last_tick_at": None,
        "last_error": None,
        "ticks": 0,
        "failures": 0,
        "last_result": {},
    }
    scheduler_stop = threading.Event()
    tick_lock = threading.Lock()
    daily_refresh_marks: dict[str, str] = {}

    def is_trading_session(moment: datetime) -> bool:
        if moment.weekday() >= 5:
            return False
        clock = moment.hour * 60 + moment.minute
        return (9 * 60 + 30) <= clock <= (11 * 60 + 30) or (13 * 60) <= clock <= (15 * 60)

    def trading_day_info(today_text: str | None = None) -> dict:
        """今天是否交易日 + 下一个交易日：quan 交易日历优先，其次行情探测，最后工作日回落。

        quan 的 trade_calendar 只记录「开市日」且通常滞后到上一交易日，覆盖不到今天；
        节假日当天它无法判定。因此日历未覆盖今天时，在 09:35 之后用一笔实时快照探测：
        快照日期 == 今天 → 今天开市；快照仍是上一交易日 → 今天休市（节假日）。
        探测结果按天缓存，每天最多一次。
        """
        today_text = today_text or date.today().isoformat()
        is_today = today_text == date.today().isoformat()
        # 1) fuyao 交易日历（含未来日期，能提前识别长假）——最权威，优先。
        provider = application.state.fuyao_calendar
        if provider is not None and provider.enabled:
            opened = provider.is_trading_day(today_text)
            if opened is not None:
                return {
                    "today": today_text, "is_trading_day": opened,
                    "next_trading_day": provider.next_trading_day(today_text),
                    "source": "fuyao_calendar",
                }
        # 2) 本地 quan 日历：覆盖当天时以 is_open 为准。
        store = application.state.quan_store
        if store is not None:
            try:
                span = store.calendar_span()
                if span is not None and span[0] <= today_text <= span[1]:
                    open_today = bool(store.trade_days(today_text, today_text))
                    horizon = (date.fromisoformat(today_text) + timedelta(days=31)).isoformat()
                    future = store.trade_days(today_text, horizon) or []
                    next_day = next((day for day in future if day > today_text), None)
                    return {
                        "today": today_text, "is_trading_day": open_today,
                        "next_trading_day": next_day, "source": "quan_calendar",
                    }
            except Exception:  # noqa: BLE001 - 日历异常回落工作日
                pass

        cursor = date.fromisoformat(today_text)
        if is_today and cursor.weekday() < 5 and probe_trading_day_enabled():
            probed = probe_trading_day(today_text)
            if probed is not None:
                # 行情探测无法预知未来节假日，下一个候选先给「下一个工作日」；
                # 到那天若仍是节假日，会再次被探测判为休市。
                return {
                    "today": today_text, "is_trading_day": probed,
                    "next_trading_day": _next_weekday(today_text), "source": "quote_probe",
                }

        nxt = date.fromisoformat(today_text) + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return {
            "today": today_text, "is_trading_day": cursor.weekday() < 5,
            "next_trading_day": nxt.isoformat(), "source": "weekday_fallback",
        }

    probe_enabled = os.environ.get("WB_TRADING_PROBE", "1") != "0"
    day_probe_cache: dict[str, bool] = {}

    def probe_trading_day_enabled() -> bool:
        return bool(probe_enabled)

    def _next_weekday(today_text: str) -> str:
        nxt = date.fromisoformat(today_text) + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt.isoformat()

    def probe_trading_day(today_text: str) -> bool | None:
        """用一笔实时快照判断今天是否开市；09:35 前行情仍是上一交易日，不做判断。

        返回 None 表示无法判断（未到探测时间/网络失败），调用方回落工作日启发式。
        """
        now = datetime.now()
        if (now.hour * 60 + now.minute) < (9 * 60 + 35):
            return None
        cached = day_probe_cache.get(today_text)
        if cached is not None:
            return cached
        try:
            _, bar = application.state.market_service.fetch_quote("000001")
        except Exception:  # noqa: BLE001 - 探测失败不能误伤交易日
            return None
        opened = bar.date == today_text
        day_probe_cache.clear()
        day_probe_cache[today_text] = opened
        return opened

    def _rotation_target(account: BrokerAccount) -> str | None:
        """轮动当前目标标的（已选出、可能尚未建仓）；无目标返回 None。"""
        if not account.rotation_enabled:
            return None
        index = int(account.rotation_index)
        if 0 <= index < len(account.rotation_pool):
            return str(account.rotation_pool[index].get("symbol") or "") or None
        return None

    def _beta_last_exit_date(account: BrokerAccount) -> str | None:
        """最近一次 ETF 清仓日：取 BETA 账本卖出成交日期的最大值。

        beta 持仓清空后才会进入再入场分支，因此「最近一笔 BETA 卖出」就是最近
        一次清仓（ETF 只做整仓进出，不会在空仓状态下卖出）。
        """
        dates = [
            str(fill.timestamp)[:10] for fill in account.trades
            if fill.purpose == "BETA" and fill.side == "SELL"
        ]
        return max(dates) if dates else None

    def _beta_cooldown_ready(account: BrokerAccount, today: str) -> bool:
        """清仓后冷却（交易日）：日历不可用时放行，避免因数据缺失永久空仓。"""
        cooldown = int(account.config.etf_reentry_cooldown_days or 0)
        exit_date = _beta_last_exit_date(account)
        if cooldown <= 0 or not exit_date:
            return True
        quan = application.state.quan_store
        trade_days = None
        if quan is not None:
            try:
                trade_days = quan.trade_days(exit_date, today)
            except Exception:  # noqa: BLE001 - 日历异常按「已满足冷却」降级
                trade_days = None
        return reentry_cooldown_ready(exit_date, today, cooldown, trade_days)

    def apply_beta_bar(
        account: BrokerAccount, bar: Bar, index_history: list[Bar] | None = None,
    ) -> None:
        """ETF beta 腿的模拟盘 tick：撮合 → 标记 → 出场/换股 → 空仓入场。

        与回测共用 beta_sleeve 的判定（架构原则 1）；差别只在行情来源：
        回测读本地 ETF 日线，模拟盘用腾讯快照，指数趋势用本地指数日线。
        """
        account.new_day(bar.date)
        account.process_bar(bar, modes={"NEXT_BAR", "NEXT_OPEN"})
        account.mark(bar.symbol, bar.close)
        if not account.config.etf_beta_enabled:
            return
        config = account.config
        position = account.position(bar.symbol)
        series = list(index_history or [])
        target = _rotation_target(account)
        if position.beta_qty > 0:
            entry_cost = position.beta_cost / position.beta_qty
            reason = exit_reason(
                series, entry_cost, bar.close,
                exit_ma_days=config.etf_exit_ma_days,
                hard_stop_pct=config.etf_hard_stop_pct,
            )
            if reason is None and (account.has_stock_position or target):
                # 个股与 ETF 不允许同时持有：一旦选出个股，ETF 先清仓。
                reason = "选到个股换仓"
            pending = any(
                order.symbol == bar.symbol and order.purpose == "BETA" and order.side == "SELL"
                and order.status in {"PENDING", "PARTIALLY_FILLED"}
                for order in account.orders
            )
            if reason and not pending:
                account.submit_split(OrderRequest(
                    symbol=bar.symbol, side="SELL", quantity=position.beta_qty,
                    execution_mode="NEXT_BAR", purpose="BETA", reason=reason,
                ), bar.timestamp)
            return
        if account.has_stock_position or target:
            return
        pending_buy = any(
            order.symbol == bar.symbol and order.purpose == "BETA" and order.side == "BUY"
            and order.status in {"PENDING", "PARTIALLY_FILLED"}
            for order in account.orders
        )
        if pending_buy or not _beta_cooldown_ready(account, bar.date):
            return
        if not entry_allowed(
            series, config.etf_entry_ma_days, config.etf_exit_ma_days,
            max_deviation=config.etf_entry_max_deviation,
        ):
            return
        rules = rules_for(bar.symbol)
        quantity = target_quantity(
            account.equity, config.etf_position_pct, bar.close,
            min_qty=rules.min_qty, step=rules.step,
        )
        if quantity <= 0:
            return
        account.submit_split(OrderRequest(
            symbol=bar.symbol, side="BUY", quantity=quantity,
            execution_mode="NEXT_BAR", purpose="BETA",
            reason="指数趋势向上，空仓建 beta 腿",
        ), bar.timestamp)

    def scheduled_symbol(account: BrokerAccount) -> str | None:
        """定时器选标的：轮动当前标的优先，其次 beta 腿 ETF，最后账户绑定的日线数据集。"""
        if account.rotation_enabled:
            index = int(account.rotation_index)
            if 0 <= index < len(account.rotation_pool):
                symbol = str(account.rotation_pool[index].get("symbol") or "")
                if symbol:
                    return symbol
        if account.config.etf_beta_enabled:
            return account.config.etf_symbol
        if account.dataset_id:
            dataset = storage.get_dataset(account.dataset_id)
            if dataset is not None and dataset.symbol:
                return dataset.symbol
        return None

    def _beta_qty(account: BrokerAccount) -> int:
        position = account.positions.get(account.config.etf_symbol)
        return position.beta_qty if position else 0

    def _beta_index_history() -> list[Bar]:
        """ETF beta 腿的趋势序列：创业板指日线（本地 quan 缓存，收盘后含当日）。"""
        quan = application.state.quan_store
        if quan is None:
            return []
        try:
            return quan.load_index("sz.399006", max_rows=320)
        except Exception:  # noqa: BLE001 - 数据缺失时按「不建仓」降级，不影响个股 tick
            return []

    def perform_live_tick(
        account: BrokerAccount, raw_symbol: str, *, require_today: bool = False,
    ) -> BrokerAccount | None:
        """拉一次实时快照并推进账户；手动 live-tick 与后端定时器共用同一实现。

        ``require_today=True`` 时若快照不属于当天（休市/节假日）直接返回 None，不做任何推进。
        """
        symbol_info = normalize_symbol(raw_symbol)
        # 用标准代码（sz.159915）取行情：ETF 纯 6 位代码以 1 开头，无法自动判交易所。
        _, bar = application.state.market_service.fetch_quote(symbol_info.standard)
        if require_today and bar.date != date.today().isoformat():
            # 休市/节假日腾讯返回的是上一交易日快照，定时器不能把它当新交易日推进。
            return None
        # ETF beta 腿：独立 tick，不参与个股策略/轮动状态机。
        if (
            account.config.etf_beta_enabled
            and symbol_info.standard == account.config.etf_symbol
        ):
            apply_beta_bar(account, bar, index_history=_beta_index_history())
            save_paper(account)
            return account
        # 个股与 ETF 互斥：ETF 没清完之前不推进个股，避免同一账户同时持有两者。
        if account.config.etf_beta_enabled and _beta_qty(account) > 0:
            return None

        daily_history: list[Bar] = []
        index_history: list[Bar] = []
        recent_bars: list[Bar] = []
        if account.auto_strategy:
            symbol = symbol_info.standard
            registered = storage.list_datasets()
            daily_dataset = next(
                (item for item in registered if is_daily_dataset(item, symbol)), None
            )
            index_dataset = next((
                item for item in registered
                if item.timeframe == "daily" and item.symbol in {"sh.000688", "sz.399006"}
            ), None)
            # 日线数据集落后于当日 → 每个标的每天最多刷新一次。
            # session_index（加仓「间隔≥1交易日」判定）取自日线数据集长度，不刷新会永久冻结。
            if (
                daily_dataset is not None
                and str(daily_dataset.end)[:10] < bar.date
                and daily_refresh_marks.get(symbol) != bar.date
            ):
                daily_refresh_marks[symbol] = bar.date
                try:
                    bundle = application.state.market_service.fetch_history_bundle(
                        raw_symbol, include_intraday=False,
                    )
                    for key in ("daily", "index"):
                        dataset = bundle.get(key)
                        if dataset is not None:
                            storage.save_dataset(dataset)
                    daily_dataset = bundle.get("daily") or daily_dataset
                    index_dataset = bundle.get("index") or index_dataset
                except (requests.RequestException, ValueError):
                    pass  # 刷新失败保持旧数据集，次日再试
            if daily_dataset is None or index_dataset is None:
                bundle = application.state.market_service.fetch_history_bundle(
                    raw_symbol, include_intraday=False,
                )
                storage.save_dataset(bundle["daily"])
                storage.save_dataset(bundle["index"])
                daily_dataset = bundle["daily"]
                index_dataset = bundle["index"]
            daily_history = read_csv_bars(daily_dataset.file_path, "daily", daily_dataset.symbol)
            index_history = read_csv_bars(index_dataset.file_path, "daily", index_dataset.symbol)
            try:
                intraday_bars, _ = application.state.market_service.fetch_tencent_intraday(
                    symbol, count=TENCENT_INTRADAY_MAX_BARS,
                )
                # 只保留截至当前快照的 K 线，并收敛到当日；VWAP/背离只在当日盘中计算。
                recent_bars = [
                    item for item in intraday_bars
                    if item.timestamp <= bar.timestamp and item.date == bar.date
                ]
            except (requests.RequestException, ValueError):
                recent_bars = []

        daily_close = bar.timestamp.hour >= 15
        # 盘中检测只看最近一根**已收盘**的 5 分钟 K 线；当日尚无收盘 bar 时跳过检测。
        closed_bar = latest_closed_intraday_bar(recent_bars, bar.timestamp)
        execution_bar = snapshot_execution_bar(
            bar,
            bar_volume=closed_bar.volume if closed_bar else None,
            bar_amount=closed_bar.amount if closed_bar else None,
        )
        apply_bar(
            account, bar, daily_history=daily_history, index_history=index_history,
            recent_bars=recent_bars, daily_close=daily_close, execution_bar=execution_bar,
            signal_bar=closed_bar, detect_intraday=closed_bar is not None,
        )
        # 池内轮动：空仓且 tick 标的 == 池内当前标的时，按 L4 顺序检查 L0 后换票。
        maybe_advance_rotation(account, bar)
        save_paper(account)
        return account

    def scheduler_run_once() -> dict:
        """驱动所有「开启自动策略且状态 ACTIVE」的模拟账户各跑一次 live-tick。"""
        result: dict = {"ticked": [], "skipped": [], "errors": []}
        if not tick_lock.acquire(blocking=False):
            result["skipped"].append("busy")
            return result
        try:
            for state in storage.list_paper():
                account = BrokerAccount.from_dict(state)
                if account.status != "ACTIVE" or not account.auto_strategy:
                    continue
                try:
                    symbol = scheduled_symbol(account)
                    etf_symbol = account.config.etf_symbol
                    # beta 腿先 tick：先把 ETF 出场/换股单挂出去，再推进个股；
                    # 个股路径在 ETF 未清完时会直接跳过（互斥）。
                    if account.config.etf_beta_enabled:
                        etf_ticked = perform_live_tick(account, etf_symbol, require_today=True)
                        if etf_ticked is not None:
                            result["ticked"].append(f"{account.id}:{etf_symbol}")
                    if not symbol:
                        result["skipped"].append(f"{account.id}:no-symbol")
                        continue
                    if symbol == etf_symbol:
                        continue
                    ticked = perform_live_tick(account, symbol, require_today=True)
                    if ticked is None:
                        result["skipped"].append(f"{account.id}:not-today")
                    else:
                        result["ticked"].append(f"{account.id}:{symbol}")
                except (ValueError, requests.RequestException) as exc:
                    result["errors"].append(f"{account.id}:{exc}")
                except Exception as exc:  # noqa: BLE001 - 单账户失败不能拖垮整轮
                    result["errors"].append(f"{account.id}:{exc.__class__.__name__}: {exc}")
            scheduler_state["last_tick_at"] = utc_now()
            scheduler_state["ticks"] += 1
        finally:
            tick_lock.release()
        return result

    def scheduler_loop() -> None:
        """每 20 秒醒一次；交易时段内每个 5 分钟整点的 +30s 跑一轮。"""
        scheduler_state["running"] = True
        try:
            while not scheduler_stop.is_set():
                try:
                    now = datetime.now()
                    # 交易日历门：周末/节假日直接跳过，连行情请求都不发。
                    trading = trading_day_info(now.date().isoformat())
                    if trading["is_trading_day"] and is_trading_session(now):
                        slot = now.replace(
                            minute=now.minute - now.minute % 5, second=0, microsecond=0
                        )
                        if (
                            scheduler_state["last_slot"] != slot.isoformat()
                            and (now - slot).total_seconds() >= 30
                        ):
                            scheduler_state["last_slot"] = slot.isoformat()
                            scheduler_state["last_result"] = scheduler_run_once()
                except Exception as exc:  # noqa: BLE001 - 调度线程不能退出
                    scheduler_state["failures"] += 1
                    scheduler_state["last_error"] = str(exc) or exc.__class__.__name__
                scheduler_stop.wait(20)
        finally:
            scheduler_state["running"] = False

    @application.on_event("startup")
    def start_paper_scheduler() -> None:
        # 进程启动：把上次遗留的 RUNNING 回测标记为 INTERRUPTED（容器重启=被打断），可续传。
        backtest_jobs.recover_interrupted()
        if not scheduler_enabled or scheduler_state["running"]:
            return
        threading.Thread(target=scheduler_loop, name="paper-scheduler", daemon=True).start()

    @application.get("/api/paper/scheduler")
    def paper_scheduler_status():
        return {**scheduler_state, "trading": trading_day_info()}

    @application.get("/api/trading/session")
    def trading_session_status():
        """交易日/交易时段判定：供前端决定是否自动轮询（周末、节假日不轮询）。"""
        info = trading_day_info()
        info["in_session"] = bool(info["is_trading_day"] and is_trading_session(datetime.now()))
        info["session_windows"] = ["09:30-11:30", "13:00-15:00"]
        return info

    @application.post("/api/paper/scheduler/run")
    def paper_scheduler_run_once():
        """立即跑一轮定时 tick（运维/调试用；交易时段由后台线程自动调用）。"""
        return scheduler_run_once()

    @application.get("/api/health")
    def health():
        sources = ["quan_parquet", "tencent"] if quan_store is not None else ["baostock", "tencent"]
        return {
            "status": "ok", "service": "layered-t-backtest", "broker_connected": False,
            "database": "sqlite", "data_sources": sources,
            "quan_data_dir": str(quan_store.path) if quan_store is not None else None,
            "data_end": quan_store.latest_trade_date() if quan_store is not None else None,
            "trading_calendar": application.state.fuyao_calendar.status(),
            "realtime_interval_seconds": REALTIME_INTERVAL_SECONDS,
        }

    @application.get("/api/strategy/defaults")
    def strategy_defaults():
        fees = load_fee_config()
        config = StrategyConfig.from_dict(fees.to_strategy_patch())
        return {
            "config": config.to_dict(),
            "fees": fees.to_dict(),
            "installment_amounts": config.installment_amounts,
            "initial_cash": 4_000_000.0,
            "rules": STRATEGY_RULES,
        }

    @application.get("/api/fees")
    def get_fees():
        return fee_response(load_fee_config())

    @application.put("/api/fees")
    def update_fees(request: FeeConfigRequest):
        current = load_fee_config().to_dict()
        current.update({key: value for key, value in request.model_dump().items() if value is not None})
        fees = FeeConfig.from_dict(current)
        save_fee_config(fees)
        return fee_response(fees)

    @application.post("/api/fees/reset")
    def reset_fees():
        storage.delete_setting(FEE_SETTING_KEY)
        storage.delete_setting(FEE_SETTING_KEY + "_at")
        return fee_response(FeeConfig())

    @application.get("/api/datasets")
    def datasets():
        return [item.to_dict() for item in storage.list_datasets()]

    @application.get("/api/market/sources")
    def market_sources():
        return application.state.market_service.source_status()

    @application.post("/api/market/fetch")
    def fetch_market_data(request: MarketFetchRequest):
        try:
            # 前端只传 6 位代码，交易所/板块/数据源符号全部由后端适配。
            info = normalize_symbol(request.symbol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            bundle = application.state.market_service.fetch_history_bundle(
                info.code, request.start_date, request.end_date, request.include_intraday,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except requests.RequestException:
            raise HTTPException(status_code=502, detail="行情源网络请求失败，请稍后重试")
        for key in ("daily", "index", "intraday"):
            dataset = bundle.get(key)
            if dataset is not None:
                storage.save_dataset(dataset)
        return {
            "symbol": bundle["symbol"], "code": bundle["code"],
            "exchange": bundle["exchange"], "board": bundle["board"],
            "name": bundle["name"], "index_symbol": bundle["index_symbol"],
            "daily": bundle["daily"].to_dict(), "index": bundle["index"].to_dict(),
            "intraday": bundle["intraday"].to_dict() if bundle.get("intraday") else None,
            "quote": bundle["quote"], "source_info": bundle["source_info"],
        }

    @application.get("/api/market/quote/{symbol}")
    def market_quote(symbol: str):
        try:
            quote, _ = application.state.market_service.fetch_quote(symbol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except requests.RequestException:
            raise HTTPException(status_code=502, detail="腾讯实时行情请求失败")
        return quote

    @application.get("/api/screener/defaults")
    def screener_defaults():
        return screener_service().defaults()

    @application.get("/api/screener/coverage")
    def screener_coverage():
        return screener_service().coverage()

    @application.post("/api/screener/refresh")
    def screener_refresh(request: ScreenerRefreshRequest):
        try:
            return screener_service().refresh(**request.model_dump())
        except ValueError as exc:
            if "已在运行" in str(exc):
                raise HTTPException(status_code=409, detail="刷新任务已在运行") from exc
            raise

    @application.get("/api/screener/refresh")
    def screener_refresh_status():
        return screener_service().refresh_status()

    @application.post("/api/screener/run")
    def run_screener(request: ScreenerRunRequest):
        config = ScreenerConfig.from_dict(request.config) if request.config is not None else None
        result: ScreeningResult = screener_service().run(
            config=config, as_of=request.as_of, include_realtime=request.include_realtime,
        )
        payload = result.to_dict()
        storage.save_screener_run(payload)
        return payload

    @application.get("/api/screener/runs")
    def screener_runs(limit: int = Query(default=20, ge=1, le=200)):
        return storage.list_screener_runs(limit=limit)

    @application.get("/api/screener/runs/{run_id}")
    def screener_run_detail(run_id: str):
        run = storage.get_screener_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="选股运行不存在")
        return run

    @application.delete("/api/screener/runs/{run_id}")
    def delete_screener_run(run_id: str):
        return {"deleted": storage.delete_screener_run(run_id)}

    @application.post("/api/screener/runs/{run_id}/prepare")
    def prepare_screener_run(run_id: str, request: ScreenerPrepareRequest):
        run = storage.get_screener_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="选股运行不存在")
        first = run.get("first") or {}
        candidates = run.get("eligible") or run.get("pool") or []
        if request.candidate_code:
            wanted = standard_symbol(request.candidate_code)
            candidate = next(
                (item for item in candidates
                 if standard_symbol(str(item.get("code") or item.get("symbol") or "")) == wanted),
                None,
            )
            if candidate is None:
                raise ValueError(f"候选标的 {wanted} 不在本次选股结果中")
        else:
            wanted = standard_symbol(str(first.get("code") or "")) if first.get("code") else ""
            if not wanted:
                raise ValueError("本次选股结果没有可用候选标的")
            candidate = next(
                (item for item in candidates
                 if standard_symbol(str(item.get("code") or item.get("symbol") or "")) == wanted),
                first,
            )
        code = standard_symbol(str(candidate.get("code") or wanted))
        # 一键带入优先复用本地已注册数据集：行情源限速时仍然可用，也避免重复拉取。
        # 只有本地缺失或数据未覆盖最新交易日时才走行情服务；行情失败再回落本地，最后才 502。
        index_symbol = "sh.000688" if code.split(".", 1)[-1].startswith(("688", "689")) else "sz.399006"
        datasets = storage.list_datasets()
        cached_daily = next(
            (item for item in datasets if is_daily_dataset(item, code)),
            None,
        )
        cached_index = next(
            (item for item in datasets
             if item.timeframe == "daily" and item.symbol == index_symbol),
            None,
        )
        cached_intraday = next(
            (item for item in datasets
             if item.timeframe == INTRADAY_TIMEFRAME and item.symbol == code),
            None,
        )
        try:
            latest_trade = screener_service().store.latest_trade_date()
        except Exception:  # noqa: BLE001 - 元数据不可用时不阻塞带入
            latest_trade = None
        cache_fresh = bool(
            cached_daily and cached_index and latest_trade and cached_daily.end >= latest_trade
        )
        saved: dict = {}
        if cache_fresh:
            saved = {
                "daily": cached_daily.to_dict(),
                "index": cached_index.to_dict(),
                "intraday": cached_intraday.to_dict() if cached_intraday else None,
            }
            bundle = None
        else:
            try:
                bundle = application.state.market_service.fetch_history_bundle(code)
            except requests.RequestException:
                if cached_daily and cached_index:
                    saved = {
                        "daily": cached_daily.to_dict(),
                        "index": cached_index.to_dict(),
                        "intraday": cached_intraday.to_dict() if cached_intraday else None,
                    }
                    bundle = None
                else:
                    raise HTTPException(status_code=502, detail="行情源网络请求失败，请稍后重试")
        if bundle is not None:
            for key in ("daily", "index", "intraday"):
                dataset = bundle.get(key)
                if dataset is not None:
                    storage.save_dataset(dataset)
                    saved[key] = dataset.to_dict()
                else:
                    saved[key] = None
        candidate_info = {
            "code": code,
            "name": candidate.get("name") or first.get("name") or "",
            "board": candidate.get("board") or "",
            "chg20d": candidate.get("chg20d"),
        }
        if request.target == "backtest":
            return {
                "target": "backtest", "candidate": candidate_info,
                "datasets": saved, "account": None,
            }

        previous = get_paper(request.account_id) if request.account_id else None
        account = BrokerAccount(
            initial_cash=previous.initial_cash if previous else 4_000_000.0,
            name=previous.name if previous else "选股带入模拟盘",
            config=previous.config if previous else None,
            account_id=previous.id if previous else None,
            dataset_id=saved["daily"]["id"],
        )
        account.auto_strategy = request.auto_strategy
        if request.rotation:
            # 轮动 v2：记录该次选股的参数/快照，池=最新候选，序号指向本次带入的候选。
            set_rotation_from_run(
                account, run, enabled=True,
                note=f"换票→{code}", current_symbol=code,
            )
        else:
            account.rotation_enabled = False
            account.rotation_pool = []
            account.rotation_index = -1
            account.rotation_waiting = False
            account.rotation_pending_advance = False
            account.rotation_note = ""
            account.rotation_run_id = None
            account.rotation_config = {}
            account.rotation_last_run_id = None
            account.rotation_last_screen = {}
            account.rotation_last_attempt_date = None
        return {
            "target": "paper", "candidate": candidate_info,
            "datasets": saved, "account": save_paper(account),
        }

    @application.post("/api/datasets/demo")
    def demo_datasets():
        daily, intraday, index = save_demo_datasets(data_dir)
        for item in (daily, intraday, index):
            storage.save_dataset(item)
        return {"daily": daily.to_dict(), "intraday": intraday.to_dict(), "index": index.to_dict()}

    @application.post("/api/datasets/upload")
    async def upload_dataset(
        file: UploadFile = File(...), symbol: str = Query(""),
        name: str | None = Query(None), timeframe: str = Query("daily"),
    ):
        suffix = Path(file.filename or "upload.csv").suffix.lower() or ".csv"
        if suffix != ".csv":
            raise ValueError("仅支持CSV文件")
        destination = upload_dir / f"{uuid.uuid4().hex}.csv"
        destination.write_bytes(await file.read())
        try:
            bars = read_csv_bars(destination, timeframe, symbol)
            actual_symbol = symbol or bars[0].symbol
            dataset = dataset_from_bars(
                bars, name or Path(file.filename or "upload.csv").stem,
                actual_symbol, timeframe, "upload", False, str(destination),
            )
            storage.save_dataset(dataset)
            return dataset.to_dict()
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    @application.post("/api/backtests")
    def create_backtest(request: BacktestRequest):
        daily_dataset = get_dataset(request.dataset_id)
        intraday_dataset = get_dataset(request.intraday_dataset_id) if request.intraday_dataset_id else None
        index_dataset = get_dataset(request.index_dataset_id) if request.index_dataset_id else None
        daily = load_dataset(request.dataset_id, {"daily"})
        intraday = load_dataset(request.intraday_dataset_id, {"1m", "5m", "tick"}) if request.intraday_dataset_id else None
        index = load_dataset(request.index_dataset_id, {"daily"}) if request.index_dataset_id else None
        config = resolve_config(request.config, request.fees)
        report = run_backtest(
            daily=daily, intraday=intraday, index=index,
            initial_cash=request.initial_cash, config=config,
            start_date=request.start_date, end_date=request.end_date, name=request.name,
        )
        report["data_info"].update({
            "daily_source": daily_dataset.source,
            "intraday_source": intraday_dataset.source if intraday_dataset else None,
            "index_source": index_dataset.source if index_dataset else None,
        })
        storage.save_backtest(report)
        return report

    @application.post("/api/backtests/funnel")
    def create_funnel_backtest(request: FunnelBacktestRequest):
        """选股漏斗轮动回测：每次清仓后重跑 L0→L4 取当时第一名换票（不联网）。"""
        config = resolve_config(request.config, request.fees)
        screener = ScreenerConfig.from_dict({
            **(request.screener or {}), "enable_fundamental_veto": False,
        })
        service = screener_service()
        report = run_funnel_backtest(
            store=service.store, market=application.state.market_service,
            screener_config=screener, initial_cash=request.initial_cash,
            config=config, start_date=request.start_date, end_date=request.end_date,
            name=request.name,
            quan=getattr(application.state, "quan_store", None),
        )
        report["data_info"].update({
            "daily_source": getattr(service.store, "provider", "universe_db"),
            "universe_db": str(service.store.path),
        })
        storage.save_backtest(report)
        return _slim_records(_slim_rotation_trail(report))

    # -- 后台回测任务：进度可见 / 可停止 / 断点续传 ----------------------

    @application.post("/api/backtests/jobs", status_code=202)
    def create_backtest_job(request: BacktestJobRequest):
        """提交后台回测任务，立即返回 job_id；串行不排队，忙时返回 409。"""
        if request.mode == "single" and not request.dataset_id:
            raise HTTPException(status_code=400, detail="单标的模式必须选择主数据集。")
        if request.mode == "funnel" and not (request.screener or {}).get("boards"):
            raise HTTPException(status_code=400, detail="漏斗轮动模式必须至少选择一个板块。")
        params = request.model_dump()
        try:
            return backtest_jobs.submit(params, request.name)
        except BacktestBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @application.get("/api/backtests/jobs")
    def list_backtest_jobs(limit: int = 20):
        return {"jobs": backtest_jobs.list_jobs(limit=limit)}

    @application.get("/api/backtests/jobs/{job_id}")
    def backtest_job_progress(job_id: str):
        progress = backtest_jobs.get_progress(job_id)
        if progress is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return progress

    @application.post("/api/backtests/jobs/{job_id}/cancel")
    def cancel_backtest_job(job_id: str):
        try:
            return backtest_jobs.cancel(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="任务不存在")

    @application.post("/api/backtests/jobs/{job_id}/resume", status_code=202)
    def resume_backtest_job(job_id: str):
        try:
            return backtest_jobs.resume(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="任务不存在")
        except BacktestBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @application.get("/api/backtests")
    def backtests(
        q: str | None = Query(None, max_length=200),
        params: tuple[int, int] = Depends(page_params),
    ):
        """回测报告列表：支持按 Report ID / 名称搜索，后台分页返回轻量摘要。"""
        page, page_size = params
        return storage.query_backtests((q or "").strip() or None, page, page_size)

    @application.delete("/api/backtests/{backtest_id}")
    def delete_backtest(backtest_id: str):
        """删除回测报告，并级联清理同 ID 的后台任务与断点。运行中的任务禁止删除。"""
        job = storage.get_job(backtest_id)
        if job is not None and job["status"] in {"RUNNING", "CANCELLING"}:
            raise HTTPException(status_code=409, detail="回测正在运行，不能删除；请先停止或等待完成。")
        if storage.get_backtest(backtest_id) is None:
            raise HTTPException(status_code=404, detail="回测不存在")
        if not storage.delete_backtest(backtest_id):
            raise HTTPException(status_code=404, detail="回测不存在")
        return {"deleted": True, "id": backtest_id}

    @application.get("/api/backtests/{backtest_id}")
    def backtest(backtest_id: str):
        report = storage.get_backtest(backtest_id)
        if report is None:
            raise HTTPException(status_code=404, detail="回测不存在")
        return _slim_records(_slim_rotation_trail(report))

    @application.get("/api/backtests/{backtest_id}/rotation-trail")
    def backtest_rotation_trail(
        backtest_id: str,
        params: tuple[int, int] = Depends(page_params),
    ):
        """换票轨迹分页：后台切片返回，支持上下页 / 跳转 / 自定义页大小。

        全量轨迹已落库；这里只返回请求的那一页，避免一次性下发大数据量。
        """
        report = storage.get_backtest(backtest_id)
        if report is None:
            raise HTTPException(status_code=404, detail="回测不存在")
        trail = report.get("data_info", {}).get("rotation_trail") or []
        page, page_size = params
        return paginate(trail, page=page, page_size=page_size)

    @application.get("/api/backtests/{backtest_id}/records/{kind}")
    def backtest_records(
        backtest_id: str,
        kind: Literal["trades", "orders", "rounds", "events"],
        params: tuple[int, int] = Depends(page_params),
        symbol: str | None = Query(None, description="按标的过滤：子串匹配，如 688200 / sh.688200"),
        purpose: str | None = Query(None, description="按用途过滤：枚举（PERMANENT/T/BETA）或中文标签（永久仓/T仓/ETF对冲），全等匹配"),
        side: str | None = Query(None, description="按方向过滤：BUY / SELL（大小写不敏感）"),
    ):
        """成交明细 / 委托记录 / 已结束轮次 / 事件 分页：后台切片返回。

        主报告已瘦身不含全量明细（只留 ``*_total``），这里按页下发，
        避免长回测一次性传输 / 渲染成千上万行导致 REPORT 卡顿。

        支持可选 ``symbol``（子串）/ ``purpose``（枚举或中文标签，全等）/
        ``side``（BUY/SELL，全等）组合过滤（条件间 AND），过滤在切片前完成，
        ``total`` 反映过滤后条数。``kind=trades`` 时额外返回 ``facets``
        （各用途 / 方向条数，按「剔除自身条件、其余条件生效」口径计算），
        供前端筛选下拉展示条数并禁用 0 条选项。
        """
        report = storage.get_backtest(backtest_id)
        if report is None:
            raise HTTPException(status_code=404, detail="回测不存在")
        rows = report.get(kind) or []
        symbol_q = (symbol or "").strip().lower()
        purpose_q = ""
        if (purpose or "").strip():
            normalized_purpose = _normalize_purpose(purpose)
            if normalized_purpose is None:
                raise HTTPException(
                    status_code=422,
                    detail="purpose 需为 PERMANENT/T/BETA 或中文标签（永久仓/T仓/ETF对冲）",
                )
            purpose_q = normalized_purpose
        side_q = ""
        if (side or "").strip():
            normalized_side = _normalize_side(side)
            if normalized_side is None:
                raise HTTPException(status_code=422, detail="side 需为 BUY 或 SELL")
            side_q = normalized_side
        filtered = _filter_records(rows, symbol_q=symbol_q, purpose_q=purpose_q, side_q=side_q)
        page, page_size = params
        body = paginate(filtered, page=page, page_size=page_size)
        if kind == "trades":
            body["facets"] = _facet_counts(
                rows, symbol_q=symbol_q, purpose_q=purpose_q, side_q=side_q,
            )
        return body

    @application.get("/api/backtests/{backtest_id}/export")
    def export_backtest(backtest_id: str, kind: Literal["trades", "orders"]):
        report = storage.get_backtest(backtest_id)
        if report is None:
            raise HTTPException(status_code=404, detail="回测不存在")
        rows = report[kind]
        fields = list(rows[0].keys()) if rows else (["id", "order_id", "symbol", "side", "quantity", "price"] if kind == "trades" else ["id", "symbol", "side", "quantity", "status"])
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return Response(
            content=output.getvalue().encode("utf-8-sig"), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{backtest_id}_{kind}.csv"'},
        )

    @application.post("/api/paper/accounts")
    def create_paper_account(request: PaperAccountRequest):
        if request.dataset_id:
            get_dataset(request.dataset_id)
        account = BrokerAccount(
            initial_cash=request.initial_cash, name=request.name,
            config=resolve_config(request.config, request.fees), dataset_id=request.dataset_id,
        )
        account.auto_strategy = request.auto_strategy
        return save_paper(account)

    @application.get("/api/paper/accounts/current")
    def current_paper_account():
        state = storage.latest_paper()
        if state is None:
            account = BrokerAccount(config=StrategyConfig.from_dict(load_fee_config().to_strategy_patch()))
            return save_paper(account)
        return BrokerAccount.from_dict(state).to_dict()

    @application.get("/api/paper/accounts/{account_id}")
    def paper_account(account_id: str):
        return get_paper(account_id).to_dict()

    @application.post("/api/paper/accounts/{account_id}/reset")
    def reset_paper_account(account_id: str):
        previous = get_paper(account_id)
        account = BrokerAccount(
            initial_cash=previous.initial_cash, name=previous.name,
            config=previous.config, account_id=previous.id, dataset_id=previous.dataset_id,
        )
        account.auto_strategy = previous.auto_strategy
        # 重置账户保留轮动配置与最近一次筛选快照，但回到「未开始」状态。
        account.rotation_enabled = previous.rotation_enabled
        account.rotation_pool = [dict(item) for item in previous.rotation_pool]
        account.rotation_run_id = previous.rotation_run_id
        account.rotation_config = dict(previous.rotation_config)
        account.rotation_last_run_id = previous.rotation_last_run_id
        account.rotation_last_screen = dict(previous.rotation_last_screen)
        account.rotation_last_attempt_date = None  # 重置后同一天也能重新筛选
        account.rotation_index = -1
        account.rotation_waiting = False
        account.rotation_pending_advance = False
        account.rotation_note = ""
        return save_paper(account)

    @application.post("/api/paper/accounts/{account_id}/fees")
    def apply_paper_fees(account_id: str, request: AccountFeesRequest):
        """把费用配置同步到已存在的账户；仅影响此后成交，不追溯历史记账。"""
        account = get_paper(account_id)
        fees = merge_fees(request.fees)
        for key, value in fees.to_strategy_patch().items():
            setattr(account.config, key, value)
        return save_paper(account)

    @application.post("/api/paper/accounts/{account_id}/config")
    def apply_paper_config(account_id: str, request: AccountConfigRequest):
        """局部更新模拟账户的策略配置（非费率）；下一根 K 线起的新操作立刻生效。

        策略每根 K 线都从 account.config 读取（如做T净利止盈 t_target_net_spread、
        T+1截止 t_exit_cutoff），因此改完无需重启或重建账户。费率字段请走 /fees 端点，这里不收。
        """
        account = get_paper(account_id)
        patch = request.model_dump(exclude_none=True)
        if not patch:
            raise HTTPException(status_code=400, detail="没有要更新的配置字段")
        # 只放行策略字段：AccountConfigRequest 已 extra="forbid" 且不含费率键，
        # 这里再过一遍 from_dict 兜底（未知键会抛 ValueError → 400）。
        try:
            updated = StrategyConfig.from_dict({**account.config.to_dict(), **patch})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        account.config = updated
        return save_paper(account)

    @application.post("/api/paper/accounts/{account_id}/apply-config")
    def apply_backtest_config(account_id: str, request: ApplyConfigRequest):
        """把回测配置整套应用到模拟账户（策略字段 + 费率）；下一根 K 起的新操作生效。

        回测页调参验证后一键同步到模拟盘当前账户，免去手动重配。数据结构与回测提交
        一致：config（策略字段，前端已 strip 费率）+ fees（万分之）。两者合并成完整
        StrategyConfig 整体覆盖账户 config；不追溯历史记账，与既有 config/fees 端点
        口径一致。空 config 且空 fees → 400。
        """
        account = get_paper(account_id)
        if not request.config and not request.fees:
            raise HTTPException(status_code=400, detail="没有要应用的配置字段")
        # 费率走 merge_fees 统一口径（全局打底 + 本次 fees 覆盖，万分之 → 小数比例）。
        fee_patch = merge_fees(request.fees).to_strategy_patch() if request.fees else {}
        # 策略字段打底，费率最高优先级（与回测「fees 单独覆盖费率」口径一致）。
        merged = {**request.config, **fee_patch}
        try:
            updated = StrategyConfig.from_dict(merged)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        account.config = updated
        return save_paper(account)

    @application.post("/api/paper/accounts/{account_id}/auto-strategy")
    def set_paper_auto_strategy(account_id: str, request: AutoStrategyRequest):
        account = get_paper(account_id)
        account.auto_strategy = request.enabled
        if not request.enabled:
            account.strategy_state = {}
            account.armed_state = "OFF"
        return save_paper(account)

    @application.post("/api/paper/accounts/{account_id}/rotation")
    def set_paper_rotation(account_id: str, request: RotationRequest):
        account = get_paper(account_id)
        if request.run_id:
            run = storage.get_screener_run(request.run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="选股运行不存在")
            # 轮动 v2：启用时记录该次选股配置/快照，池=最新候选，序号=first。
            # 若账户已持有某标的（尚未清仓），当前轮动标的必须指向它，
            # 否则该持仓清仓后的 tick 标的不等于 current_symbol，永远不会触发重筛。
            held_symbol = next(
                (symbol for symbol, position in account.positions.items() if position.stock_qty > 0),
                None,
            )
            set_rotation_from_run(
                account, run, enabled=request.enabled, current_symbol=held_symbol,
            )
            if request.pool is not None:
                # pool 显式给出时优先用它覆盖候选摘要（保留上面记录的配置/快照）。
                set_rotation_pool(
                    account, normalize_rotation_pool(request.pool),
                    run_id=request.run_id, enabled=request.enabled,
                )
        elif request.pool is not None:
            set_rotation_pool(
                account, normalize_rotation_pool(request.pool),
                run_id=account.rotation_run_id,
                enabled=request.enabled,
            )
        else:
            account.rotation_enabled = request.enabled
            account.rotation_pending_advance = False
            if not request.enabled:
                account.rotation_waiting = False
                account.rotation_note = ""
            # 仅开关：保留旋转配置与最近筛选快照（disabled 也保留）。
        return save_paper(account)

    @application.get("/api/trading-rules")
    def trading_rules_all():
        """六板块规则全表 + 未建模清单，供规则面板与选股带入参考。"""
        return {
            "boards": [item.to_dict() for item in all_board_rules()],
            "not_modeled": list(NOT_MODELED),
        }

    @application.get("/api/trading-rules/{symbol}")
    def trading_rules(symbol: str):
        """按标的返回其板块交易规则；合成标的回退旧口径并显式标注。"""
        try:
            code = standard_symbol(symbol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        board = board_of(code)
        rules = rules_for(code)
        reference = 0.0
        try:
            _, bar = application.state.market_service.fetch_quote(code)
            reference = bar.close
        except (ValueError, requests.RequestException):
            reference = 0.0
        cage = price_cage_bounds(code, reference) if reference > 0 else None
        return {
            "symbol": code,
            "board": board or rules.board,
            "is_modeled_board": bool(board),
            "rules": rules.to_dict(),
            "price_cage": {"lower": cage[0], "upper": cage[1], "reference_price": price(reference)} if cage else None,
            "not_modeled": list(NOT_MODELED),
        }

    @application.post("/api/paper/accounts/{account_id}/orders")
    def create_paper_order(account_id: str, request: PaperOrderRequest):
        account = get_paper(account_id)
        order = account.submit(OrderRequest(
            symbol=standard_symbol(request.symbol), side=request.side,
            order_type=request.order_type, quantity=request.quantity,
            limit_price=request.limit_price, purpose=request.purpose,
            execution_mode="NEXT_BAR", reason="模拟手工单",
        ), utc_now())
        # 不合规申报（板块数量/单笔上限/T+1 可卖）转 400 + 具体原因，且不落库，
        # 避免前端把被拒单误当成已挂单。合法订单才持久化账户。
        if order.status == "REJECTED":
            raise HTTPException(status_code=400, detail=order.message or "委托被拒绝")
        save_paper(account)
        return {"order": order.to_dict(), "account": account.to_dict()}

    @application.post("/api/paper/accounts/{account_id}/orders/{order_id}/cancel")
    def cancel_paper_order(account_id: str, order_id: str):
        state = storage.find_paper_by_order(order_id, account_id)
        if state is None:
            raise HTTPException(status_code=404, detail="订单不存在或不属于该账户")
        account = BrokerAccount.from_dict(state)
        if not account.cancel(order_id):
            raise HTTPException(status_code=400, detail="订单已结束，无法撤销")
        return save_paper(account)

    @application.post("/api/paper/orders/{order_id}/cancel")
    def cancel_paper_order_without_account(order_id: str):
        """旧端点不再接受无账户撤单；本地单号跨账户重复时无法判断归属。"""
        raise HTTPException(
            status_code=400,
            detail=f"撤单必须指定账户：/api/paper/accounts/{{account_id}}/orders/{order_id}/cancel",
        )

    @application.post("/api/paper/accounts/{account_id}/ticks")
    def paper_tick(account_id: str, request: TickRequest):
        account = get_paper(account_id)
        timestamp = request.timestamp.replace("Z", "+00:00")
        parsed = __import__("datetime").datetime.fromisoformat(timestamp)
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        bar = Bar(
            timestamp=parsed, symbol=standard_symbol(request.symbol), open=request.open,
            high=request.high, low=request.low, close=request.close,
            volume=request.volume, amount=money(request.amount if request.amount is not None else request.close * request.volume),
            timeframe="tick", turnover_rate=request.turnover_rate,
            prev_close=request.prev_close, bid=request.bid, ask=request.ask,
        )
        if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close, bar.high):
            raise ValueError("tick OHLC关系不合法")
        apply_bar(account, bar)
        return save_paper(account)

    @application.post("/api/paper/accounts/{account_id}/live-tick")
    def paper_live_tick(account_id: str, request: LiveTickRequest):
        account = get_paper(account_id)
        with tick_lock:
            try:
                perform_live_tick(account, request.symbol)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except requests.RequestException:
                raise HTTPException(status_code=502, detail="腾讯实时行情请求失败")
            return account.to_dict()

    @application.post("/api/paper/accounts/{account_id}/advance")
    def advance_paper(account_id: str, request: AdvanceRequest):
        account = get_paper(account_id)
        if not account.dataset_id:
            raise ValueError("账户未绑定dataset_id")
        bars = load_dataset(account.dataset_id)
        bound_dataset = get_dataset(account.dataset_id)
        daily_history: list[Bar] = []
        index_history: list[Bar] = []
        if account.auto_strategy:
            if bound_dataset.timeframe == "daily":
                daily_history = bars
                index_dataset = next((
                    item for item in storage.list_datasets()
                    if item.timeframe == "daily" and item.symbol in {"sh.000688", "sz.399006"}
                ), None)
                if index_dataset is not None:
                    index_history = read_csv_bars(index_dataset.file_path, "daily", index_dataset.symbol)
            else:
                daily_dataset = next(
                    (item for item in storage.list_datasets()
                     if is_daily_dataset(item, bound_dataset.symbol)),
                    None,
                )
                index_dataset = next((
                    item for item in storage.list_datasets()
                    if item.timeframe == "daily" and item.symbol in {"sh.000688", "sz.399006"}
                ), None)
                if daily_dataset is not None:
                    daily_history = read_csv_bars(daily_dataset.file_path, "daily", daily_dataset.symbol)
                if index_dataset is not None:
                    index_history = read_csv_bars(index_dataset.file_path, "daily", index_dataset.symbol)
        for _ in range(request.bars):
            if account.cursor >= len(bars):
                account.status = "COMPLETED"
                break
            bar = bars[account.cursor]
            next_bar = bars[account.cursor + 1] if account.cursor + 1 < len(bars) else None
            daily_close = next_bar is None or next_bar.date != bar.date
            # 盘中上下文只取当日 K 线，与回测引擎逐日回放的 recent 口径一致。
            session_start = account.cursor
            while session_start > 0 and bars[session_start - 1].date == bar.date:
                session_start -= 1
            recent = bars[session_start:account.cursor + 1]
            apply_bar(
                account, bar, daily_history=daily_history, index_history=index_history,
                recent_bars=recent, daily_close=daily_close,
            )
            account.cursor += 1
            account.status = "ACTIVE" if account.cursor < len(bars) else "COMPLETED"
        return save_paper(account)

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8010, reload=False)
