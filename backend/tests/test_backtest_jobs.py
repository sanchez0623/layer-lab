"""回测后台任务 / 断点续传 / 可取消 / 进度的 pytest 覆盖。

被测对象：
- app.backtest.BacktestEngine 的协作钩子（on_progress / should_cancel）与 run(resume_state=...)
- app.backtest_jobs.BacktestJobManager（submit/resume/cancel/get_progress/recover_interrupted）

约定：全部用确定性合成日线（无网络、无 baostock、无真实行情），数据量 12~20 根，
每个用例都在亚秒级完成。合成数据不带指数（index=None）→ 大盘门降级开门，
首日收盘即发首笔 NEXT_OPEN 买单，次日开盘成交，便于观察进度/取消/续传边界。

⚠️ 已发现三处真实缺陷（详见本文件 xfail 标记与最终报告，未擅自改动 feature 代码）：
- 缺陷 A（backtest.py:243）：BacktestEngine 没有实现 restore_engine_state()，但 run() 会调用它，
  导致任何带 checkpoint 的续传（resume_state 非 None）抛 AttributeError。见 test_resume_equivalence_*。
- 缺陷 B（backtest.py:_finalize）：cancel 在首个交易日处理前触发时 equity_curve 为空，
  _finalize 调 performance_metrics 抛 ValueError（"净值曲线为空"），而非优雅返回 CANCELLED。
  见 test_cancel_before_first_day_is_cancelled_*。
- 缺陷 C（backtest_jobs.py:149-150）：_start_worker 里的 on_progress 闭包引用了 worker() 的
  局部变量 execution（越界），首次进度回调即抛 NameError，真实引擎经 manager 跑必然 FAILED。
  见 test_job_lifecycle_* / test_cancel_via_manager_* / test_resume_after_recover_*。
"""

from __future__ import annotations

import copy
import random
import threading
import time
import uuid
from datetime import datetime, timedelta

import pytest

from app.backtest import BacktestEngine
from app.backtest_jobs import BacktestBusyError, BacktestJobManager, Execution
from app.domain import Bar, StrategyConfig
from app.storage import SQLiteStorage

TERMINAL = {"COMPLETED", "CANCELLED", "FAILED", "INTERRUPTED"}


# --------------------------------------------------------------------------
# 合成数据 / 配置（沿用 test_broker.py 的构造口径）
# --------------------------------------------------------------------------


def make_config() -> StrategyConfig:
    """零滑点 + 满量参与，保证撮合确定、可逐字段复现。"""
    return StrategyConfig(slippage_bps=0, volume_participation=1.0)


def make_daily(n: int = 16, seed: int = 7, symbol: str = "sh.600000",
               start: float = 10.0) -> list[Bar]:
    """确定性随机游走日线（固定 seed），价格有涨有跌以触发建仓/加仓信号。"""
    rng = random.Random(seed)
    bars: list[Bar] = []
    price = start
    for index in range(n):
        price = max(1.0, price * (1 + rng.uniform(-0.04, 0.05)))
        timestamp = datetime(2025, 1, 1) + timedelta(days=index)
        high = price * (1 + abs(rng.uniform(0.0, 0.02)))
        low = price * (1 - abs(rng.uniform(0.0, 0.02)))
        volume = rng.uniform(800_000.0, 1_200_000.0)
        bars.append(Bar(
            timestamp=timestamp, symbol=symbol, open=price, high=high, low=low,
            close=price, volume=volume, amount=price * volume,
        ))
    return bars


def wait_for_terminal(manager: BacktestJobManager, storage: SQLiteStorage,
                      job_id: str, timeout: float = 5.0) -> dict:
    """轮询直到任务进入终态，返回最终 job 视图。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = storage.get_job(job_id)
        if job and job["status"] in TERMINAL:
            return job
        time.sleep(0.02)
    raise AssertionError(f"任务 {job_id} 在 {timeout}s 内未进入终态："
                         f"{storage.get_job(job_id)}")


def wait_for_checkpoint(storage: SQLiteStorage, job_id: str,
                        timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        checkpoint = storage.get_checkpoint(job_id)
        if checkpoint:
            return checkpoint
        time.sleep(0.02)
    raise AssertionError(f"任务 {job_id} 在 {timeout}s 内未写入检查点")


# --------------------------------------------------------------------------
# 测试用假引擎（仅用于驱动 BacktestJobManager 的调度逻辑，不依赖真实撮合）
# --------------------------------------------------------------------------


class _StubAccount:
    def snapshot_state(self) -> dict:
        return {"cash": 1.0}

    def restore_state(self, state: dict) -> None:
        self.state = state


class _StubStrategy:
    def state_dict(self) -> dict:
        return {"armed": False}

    def load_state(self, state: dict) -> None:
        self.state = state


class _BlockingEngine:
    """run() 阻塞在 release 上，供测试在「运行中」窗口内做并发提交/取消。"""

    def __init__(self, release: threading.Event, started: threading.Event) -> None:
        self.release = release
        self.started = started
        self.account = _StubAccount()
        self.strategy = _StubStrategy()
        self.hooks: dict = {}

    def run(self, resume_state=None) -> dict:
        self.started.set()
        self.release.wait(timeout=5.0)
        return {"status": "COMPLETED", "metrics": {}, "trades": [], "orders": [],
                "equity_curve": [], "rounds": [], "events": [], "warnings": [],
                "data_info": {}, "config": {}, "name": "blocking"}


class _PacedEngine:
    """逐日推进并在每日之间等待 pace 事件，可确定性地在「处理中」被取消；

    支持从 resume_state["cursor_index"] 续跑，用于验证 manager 的
    取消 → 写检查点 → 续传 → 完成 的完整生命周期。
    """

    def __init__(self, total_days: int = 4, pace: threading.Event | None = None) -> None:
        self.total_days = total_days
        self.pace = pace
        self.account = _StubAccount()
        self.strategy = _StubStrategy()
        self.hooks: dict = {}

    def run(self, resume_state=None) -> dict:
        start_after = int(resume_state.get("cursor_index", -1)) if resume_state else -1
        equity = list(resume_state.get("equity_curve", [])) if resume_state else []
        for index in range(start_after + 1, self.total_days):
            if self.hooks.get("should_cancel") and self.hooks["should_cancel"]():
                return {"status": "CANCELLED", "equity_curve": equity, "metrics": {},
                        "trades": [], "orders": [], "rounds": []}
            date = f"2025-01-{index + 1:02d}"
            equity.append({"date": date, "equity": 1.0})
            self.hooks["on_progress"](index + 1, self.total_days, date,
                                      {"cursor_index": index, "equity_curve": equity})
            if self.pace is not None and index < self.total_days - 1:
                self.pace.wait(timeout=5.0)
        return {"status": "COMPLETED", "equity_curve": equity, "metrics": {},
                "trades": [], "orders": [], "rounds": [], "events": [], "warnings": [],
                "data_info": {}, "config": {}, "name": "paced"}


# --------------------------------------------------------------------------
# 1. 进度发射
# --------------------------------------------------------------------------


def test_progress_hook_fires_once_per_day_and_is_monotonic():
    daily = make_daily(n=12)
    calls: list[dict] = []

    def on_progress(done, total, cursor_date, engine_state):
        calls.append({"done": done, "total": total, "cursor_date": cursor_date})

    engine = BacktestEngine(daily=daily, config=make_config(), initial_cash=4_000_000.0,
                            name="进度", hooks={"on_progress": on_progress})
    report = engine.run()
    assert report["status"] == "COMPLETED"

    total_days = len(daily)
    assert len(calls) == total_days                      # 每个交易日恰好一次
    assert [call["done"] for call in calls] == list(range(1, total_days + 1))
    assert {call["total"] for call in calls} == {total_days}
    dates = [call["cursor_date"] for call in calls]
    assert dates == sorted(dates)                        # cursor_date 单调推进
    assert len(set(dates)) == total_days                 # 无重复日期

    progress = [100.0 * call["done"] / call["total"] for call in calls]
    assert all(b >= a for a, b in zip(progress, progress[1:]))   # 非递减
    assert progress[0] == pytest.approx(100.0 / total_days)
    assert progress[-1] == pytest.approx(100.0)


# --------------------------------------------------------------------------
# 2. 在交易日边界取消
# --------------------------------------------------------------------------


def test_cancel_at_boundary_returns_partial_report():
    daily = make_daily(n=16)
    k = 4
    state = {"done": 0}

    def should_cancel():
        return state["done"] >= k

    engine = BacktestEngine(
        daily=daily, config=make_config(), initial_cash=4_000_000.0, name="取消",
        hooks={"should_cancel": should_cancel,
               "on_progress": lambda done, total, cursor_date, engine_state:
                   state.__setitem__("done", done)},
    )
    cancelled = engine.run()

    assert cancelled["status"] == "CANCELLED"
    assert len(cancelled["equity_curve"]) == k           # 只覆盖已处理的 K 天
    full = BacktestEngine(daily=daily, config=make_config(), initial_cash=4_000_000.0,
                          name="全量").run()
    full_dates = [point["date"] for point in full["equity_curve"]]
    cancelled_dates = [point["date"] for point in cancelled["equity_curve"]]
    assert cancelled_dates == full_dates[:k]              # 是全量曲线的前缀
    # 已成交部分是全量成交的前缀（取消点之后不再有新成交）
    assert cancelled["trades"] == full["trades"][:len(cancelled["trades"])]


# --------------------------------------------------------------------------
# 3. 续传等价（最关键）
# --------------------------------------------------------------------------


def test_resume_equivalence_matches_full_run():
    """分段跑（第 K 天边界打断 + 续传）结果必须与一次性跑完逐字段一致。"""
    daily = make_daily(n=16)
    config = make_config()
    k = 5
    captured: dict = {}

    def on_progress(done, total, cursor_date, engine_state):
        if done == k:
            captured["engine_state"] = copy.deepcopy(engine_state)

    def should_cancel():
        return "engine_state" in captured

    segment1 = BacktestEngine(
        daily=daily, config=config, initial_cash=4_000_000.0, name="段1",
        hooks={"on_progress": on_progress, "should_cancel": should_cancel},
    )
    segment1.run()
    assert captured["engine_state"]["cursor_index"] == k - 1

    engine_state = captured["engine_state"]
    account_state = copy.deepcopy(segment1.account.snapshot_state())
    strategy_state = copy.deepcopy(segment1.strategy.state_dict())

    segment2 = BacktestEngine(daily=daily, config=config, initial_cash=4_000_000.0,
                              name="段2")
    segment2.account.restore_state(account_state)
    segment2.strategy.load_state(strategy_state)
    resumed = segment2.run(resume_state=engine_state)

    full = BacktestEngine(daily=daily, config=config, initial_cash=4_000_000.0,
                          name="全量").run()
    assert resumed["status"] == "COMPLETED"
    assert full["status"] == "COMPLETED"
    # 核心保证：trades / orders / equity_curve / rounds / metrics 逐字段完全一致
    assert resumed["trades"] == full["trades"]
    assert resumed["orders"] == full["orders"]
    assert resumed["equity_curve"] == full["equity_curve"]
    assert resumed["rounds"] == full["rounds"]
    assert resumed["metrics"] == full["metrics"]


# --------------------------------------------------------------------------
# 4. 串行不排队：运行中提交直接拒绝
# --------------------------------------------------------------------------


def test_submit_while_running_raises_busy_then_recovers(tmp_path):
    storage = SQLiteStorage(tmp_path / "busy.db")
    release = threading.Event()
    started = threading.Event()
    completed: list[dict] = []

    def builder(params, hooks, resume_state, checkpoint):
        engine = _BlockingEngine(release, started)
        engine.hooks = hooks
        return Execution(engine, lambda report: completed.append(report))

    manager = BacktestJobManager(storage, builder)
    first = manager.submit({}, "任务1")
    job1 = first["job_id"]
    assert started.wait(timeout=5.0), "第一个任务未开始运行"

    with pytest.raises(BacktestBusyError):
        manager.submit({}, "任务2")          # 运行中串行拒绝，不排队

    release.set()                            # 放行，让第一个任务完成
    job = wait_for_terminal(manager, storage, job1)
    assert job["status"] == "COMPLETED"
    assert len(completed) == 1               # on_complete 被调用

    second = manager.submit({}, "任务3")     # 空闲后可再次提交
    job2 = second["job_id"]
    release.set()
    assert wait_for_terminal(manager, storage, job2)["status"] == "COMPLETED"


# --------------------------------------------------------------------------
# 5. 通过 manager 跑一个真实（小、快）回测的完整生命周期
# --------------------------------------------------------------------------


def test_job_lifecycle_via_manager_completes_with_callback(tmp_path):
    """通过 manager 跑真实回测：终态 COMPLETED、进度 ~100、on_complete 被调用。"""
    storage = SQLiteStorage(tmp_path / "lifecycle.db")
    daily = make_daily(n=12)
    config = make_config()
    reports: list[dict] = []

    def builder(params, hooks, resume_state, checkpoint):
        engine = BacktestEngine(daily=daily, config=config, initial_cash=4_000_000.0,
                                name="生命周期", hooks=hooks)
        return Execution(engine, lambda report: reports.append(report))

    manager = BacktestJobManager(storage, builder)
    submitted = manager.submit({"name": "生命周期"}, "生命周期回测")
    job = wait_for_terminal(manager, storage, submitted["job_id"])

    assert job["status"] == "COMPLETED"
    assert job["progress"] == pytest.approx(100.0)
    assert job["total_days"] == len(daily)
    assert len(reports) == 1 and reports[0]["status"] == "COMPLETED"   # on_complete 哨兵
    assert storage.get_checkpoint(submitted["job_id"]) is None         # 完成后清检查点


# --------------------------------------------------------------------------
# 6. 通过 manager 取消，再续传到完成
# --------------------------------------------------------------------------


def test_cancel_via_manager_then_resume_completes(tmp_path):
    storage = SQLiteStorage(tmp_path / "cancel_resume.db")
    pace = threading.Event()                 # 初始不置位 → 引擎每日之间阻塞
    finished: list[dict] = []

    def builder(params, hooks, resume_state, checkpoint):
        engine = _PacedEngine(total_days=4, pace=pace)
        engine.hooks = hooks
        return Execution(engine, lambda report: finished.append(report))

    manager = BacktestJobManager(storage, builder)
    submitted = manager.submit({}, "可取消任务")
    job_id = submitted["job_id"]

    wait_for_checkpoint(storage, job_id)     # 至少跑完一天、写了检查点
    result = manager.cancel(job_id)
    assert result["cancelled"] is True
    pace.set()                               # 放行，让引擎在下一日边界命中取消

    job = wait_for_terminal(manager, storage, job_id)
    assert job["status"] == "CANCELLED"      # 不是 COMPLETED
    assert finished == []                    # 取消不触发 on_complete

    resumed = manager.resume(job_id)         # 从检查点续传
    assert resumed["resumed"] is True
    job = wait_for_terminal(manager, storage, job_id)
    assert job["status"] == "COMPLETED"
    assert len(finished) == 1                # 续传跑完后 on_complete 被调用


# --------------------------------------------------------------------------
# 7. 进程重启恢复：RUNNING → INTERRUPTED → 可续传
# --------------------------------------------------------------------------


def _seed_orphan_running_job(storage: SQLiteStorage) -> str:
    now = datetime.now().isoformat(timespec="seconds")
    orphan = str(uuid.uuid4())
    storage.create_job({
        "id": orphan, "name": "遗留任务", "status": "RUNNING", "progress": 40.0,
        "params": {}, "created_at": now, "updated_at": now,
    })
    return orphan


def test_recover_interrupted_flags_running_jobs_as_interrupted(tmp_path):
    """进程重启后遗留的 RUNNING 任务应被 recover_interrupted 标为 INTERRUPTED（可续传态）。"""
    storage = SQLiteStorage(tmp_path / "recover_flag.db")

    def builder(params, hooks, resume_state, checkpoint):  # pragma: no cover - 不会被调用
        raise AssertionError("本用例不应真正运行引擎")

    manager = BacktestJobManager(storage, builder)
    orphan = _seed_orphan_running_job(storage)

    assert manager.recover_interrupted() == 1
    job = storage.get_job(orphan)
    assert job["status"] == "INTERRUPTED"
    assert job["status"] in {"CANCELLED", "INTERRUPTED", "FAILED"}   # 属可续传集合


def test_resume_after_recover_completes(tmp_path):
    storage = SQLiteStorage(tmp_path / "recover_resume.db")
    finished: list[dict] = []

    def builder(params, hooks, resume_state, checkpoint):
        engine = _PacedEngine(total_days=3, pace=None)
        engine.hooks = hooks
        return Execution(engine, lambda report: finished.append(report))

    manager = BacktestJobManager(storage, builder)
    orphan = _seed_orphan_running_job(storage)
    manager.recover_interrupted()

    manager.resume(orphan)                   # INTERRUPTED 在可续传集合内
    job = wait_for_terminal(manager, storage, orphan)
    assert job["status"] == "COMPLETED"
    assert len(finished) == 1


# --------------------------------------------------------------------------
# 8. 边界
# --------------------------------------------------------------------------


def test_resume_with_no_checkpoint_behaves_like_fresh_run():
    """resume_state=None 时应等同一次性全量跑完（不触碰 restore 路径）。"""
    daily = make_daily(n=12)
    config = make_config()
    seeded = BacktestEngine(daily=daily, config=config, initial_cash=4_000_000.0,
                            name="空续传").run(resume_state=None)
    fresh = BacktestEngine(daily=daily, config=config, initial_cash=4_000_000.0,
                           name="全量").run()
    assert seeded["status"] == "COMPLETED"
    assert seeded["trades"] == fresh["trades"]
    assert seeded["equity_curve"] == fresh["equity_curve"]
    assert seeded["rounds"] == fresh["rounds"]
    assert seeded["metrics"] == fresh["metrics"]


def test_cancel_before_first_day_is_cancelled_with_empty_curve():
    daily = make_daily(n=12)
    engine = BacktestEngine(daily=daily, config=make_config(), initial_cash=4_000_000.0,
                            name="立即取消", hooks={"should_cancel": lambda: True})
    report = engine.run()
    assert report["status"] == "CANCELLED"
    assert report["equity_curve"] == []
    assert report["trades"] == []
