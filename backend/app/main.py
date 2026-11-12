from __future__ import annotations

import csv
import io
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

import requests
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .backtest import run_backtest
from .broker import BrokerAccount
from .data import dataset_from_bars, read_csv_bars, save_demo_datasets
from .domain import Bar, FeeConfig, OrderRequest, STRATEGY_RULES, StrategyConfig, money, utc_now
from .market import MarketDataService, normalize_symbol
from .storage import SQLiteStorage
from .strategy import LayeredTStrategy


BASE_DIR = Path(__file__).resolve().parents[1]
FEE_SETTING_KEY = "fee_config_v1"
FEE_CONFIG_VERSION = "v1"


def trading_minutes_elapsed(timestamp: datetime) -> int:
    """当日已开盘分钟数（9:30-11:30 / 13:00-15:00），用于把当日累计量折算为 1 分钟当量。"""
    clock = timestamp.hour * 60 + timestamp.minute
    morning = min(120, max(0, clock - 9 * 60 - 30))
    afternoon = min(120, max(0, clock - 13 * 60)) if clock >= 13 * 60 else 0
    return max(1, morning + afternoon)


def snapshot_execution_bar(
    bar: Bar, minute_volume: float | None = None, minute_amount: float | None = None,
) -> Bar:
    """把腾讯实时快照转换成**仅用于撮合**的 1 分钟 bar。

    快照的 open/high/low 是全天口径（今开 / 当日最高 / 当日最低），volume/amount
    是当日累计值。直接交给 process_bar 会让市价单按今日开盘价成交、限价单按全天
    区间判定、成交量参与度额度被全天量放大。策略信号仍使用原始快照 bar。
    """
    reference = bar.close
    bid = bar.bid if bar.bid and bar.bid > 0 else reference
    ask = bar.ask if bar.ask and bar.ask > 0 else reference
    elapsed = trading_minutes_elapsed(bar.timestamp)
    volume = minute_volume if minute_volume and minute_volume > 0 else bar.volume / elapsed
    amount = minute_amount if minute_amount and minute_amount > 0 else bar.amount / elapsed
    return Bar(
        timestamp=bar.timestamp, symbol=bar.symbol, open=reference,
        high=max(reference, ask, bid), low=min(reference, ask, bid), close=reference,
        volume=volume, amount=money(amount), timeframe="1m",
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


def create_app() -> FastAPI:
    data_dir = Path(os.environ.get("WB_DATA_DIR", str(BASE_DIR / "data")))
    upload_dir = data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    storage = SQLiteStorage(os.environ.get("WB_DB_PATH", str(data_dir / "app.db")))
    application = FastAPI(
        title="A股分层加仓做T回测与模拟交易",
        version="1.1.0",
        description="历史日线来自baostock，实时快照与1分钟K线来自腾讯行情；本地撮合，不连接券商。",
    )
    application.state.storage = storage
    application.state.data_dir = data_dir
    application.state.market_service = MarketDataService(data_dir / "cache")
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

    def apply_bar(
        account: BrokerAccount, bar: Bar, daily_history: list[Bar] | None = None,
        index_history: list[Bar] | None = None, recent_bars: list[Bar] | None = None,
        daily_close: bool = False, execution_bar: Bar | None = None,
    ) -> None:
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

            if bar.timeframe != "daily":
                previous_daily = [item for item in (daily_history or []) if item.date < bar.date]
                recent = list(recent_bars or [])
                if not recent or recent[-1].timestamp != bar.timestamp:
                    recent.append(bar)
                intraday_requests = strategy.on_intraday(previous_daily, recent, bar, account)
                for request in intraday_requests:
                    if request.quantity >= 100:
                        account.submit(request, bar.timestamp)

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
                        if request.quantity >= 100:
                            account.submit(request, bar.timestamp)
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

    @application.get("/api/health")
    def health():
        return {
            "status": "ok", "service": "layered-t-backtest", "broker_connected": False,
            "database": "sqlite", "data_sources": ["baostock", "tencent"],
            "realtime_interval_seconds": 60,
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
        return MarketDataService.source_status()

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

    @application.get("/api/backtests")
    def backtests():
        return storage.list_backtests()

    @application.get("/api/backtests/{backtest_id}")
    def backtest(backtest_id: str):
        report = storage.get_backtest(backtest_id)
        if report is None:
            raise HTTPException(status_code=404, detail="回测不存在")
        return report

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
        return save_paper(account)

    @application.post("/api/paper/accounts/{account_id}/fees")
    def apply_paper_fees(account_id: str, request: AccountFeesRequest):
        """把费用配置同步到已存在的账户；仅影响此后成交，不追溯历史记账。"""
        account = get_paper(account_id)
        fees = merge_fees(request.fees)
        for key, value in fees.to_strategy_patch().items():
            setattr(account.config, key, value)
        return save_paper(account)

    @application.post("/api/paper/accounts/{account_id}/auto-strategy")
    def set_paper_auto_strategy(account_id: str, request: AutoStrategyRequest):
        account = get_paper(account_id)
        account.auto_strategy = request.enabled
        if not request.enabled:
            account.strategy_state = {}
            account.armed_state = "OFF"
        return save_paper(account)

    @application.post("/api/paper/accounts/{account_id}/orders")
    def create_paper_order(account_id: str, request: PaperOrderRequest):
        account = get_paper(account_id)
        order = account.submit(OrderRequest(
            symbol=standard_symbol(request.symbol), side=request.side,
            order_type=request.order_type, quantity=request.quantity,
            limit_price=request.limit_price, purpose=request.purpose,
            execution_mode="NEXT_BAR", reason="模拟手工单",
        ), utc_now())
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
        try:
            symbol_info = normalize_symbol(request.symbol)
            _, bar = application.state.market_service.fetch_quote(symbol_info.code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except requests.RequestException:
            raise HTTPException(status_code=502, detail="腾讯实时行情请求失败")

        daily_history: list[Bar] = []
        index_history: list[Bar] = []
        recent_bars: list[Bar] = []
        if account.auto_strategy:
            symbol = symbol_info.standard
            registered = storage.list_datasets()
            daily_dataset = next((
                item for item in registered
                if item.symbol == symbol and item.timeframe == "daily" and item.source == "baostock"
            ), None)
            index_dataset = next((
                item for item in registered
                if item.timeframe == "daily" and item.symbol in {"sh.000688", "sz.399006"}
            ), None)
            if daily_dataset is None or index_dataset is None:
                bundle = application.state.market_service.fetch_history_bundle(
                    request.symbol, include_intraday=False,
                )
                storage.save_dataset(bundle["daily"])
                storage.save_dataset(bundle["index"])
                daily_dataset = bundle["daily"]
                index_dataset = bundle["index"]
            daily_history = read_csv_bars(daily_dataset.file_path, "daily", daily_dataset.symbol)
            index_history = read_csv_bars(index_dataset.file_path, "daily", index_dataset.symbol)
            try:
                minutes, _ = application.state.market_service.fetch_tencent_minutes(symbol, count=320)
                recent_bars = [item for item in minutes if item.timestamp <= bar.timestamp]
            except (requests.RequestException, ValueError):
                recent_bars = []

        daily_close = bar.timestamp.hour >= 15
        # 自动策略分支已拉取腾讯 1 分钟 K 线，优先用真实分钟量；否则按已开盘分钟数折算。
        last_minute = recent_bars[-1] if recent_bars else None
        execution_bar = snapshot_execution_bar(
            bar,
            minute_volume=last_minute.volume if last_minute else None,
            minute_amount=last_minute.amount if last_minute else None,
        )
        apply_bar(
            account, bar, daily_history=daily_history, index_history=index_history,
            recent_bars=recent_bars, daily_close=daily_close, execution_bar=execution_bar,
        )
        return save_paper(account)

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
                daily_dataset = next((
                    item for item in storage.list_datasets()
                    if item.timeframe == "daily" and item.symbol == bound_dataset.symbol and item.source == "baostock"
                ), None)
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
            recent = bars[max(0, account.cursor - 239):account.cursor + 1]
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
