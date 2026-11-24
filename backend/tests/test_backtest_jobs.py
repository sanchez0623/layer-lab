"""回测后台任务 / 断点续传 / 可取消 / 进度的 pytest 覆盖。

被测对象：
- app.backtest_jobs.BacktestJobManager（submit/resume/cancel/get_progress/recover_interrupted）
- 引擎侧协作契约（on_progress / should_cancel / run(resume_state=...)）用轻量替身引擎驱动；
  真实漏斗引擎的断点续传等价性见 test_backtest_funnel.py::test_funnel_resume_equivalence_*。

约定：全部用确定性替身（无网络、无行情），每个用例都在亚秒级完成。
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from datetime import datetime

import pytest

from app.backtest_jobs import BacktestBusyError, BacktestJobManager, Execution
from app.storage import SQLiteStorage

TERMINAL = {"COMPLETED", "CANCELLED", "FAILED", "INTERRUPTED"}


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
    total_days = 12
    calls: list[dict] = []
    engine = _PacedEngine(total_days=total_days, pace=None)
    engine.hooks = {
        "on_progress": lambda done, total, cursor_date, state:
            calls.append({"done": done, "total": total, "cursor_date": cursor_date}),
    }
    report = engine.run()
    assert report["status"] == "COMPLETED"

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
# 2. 串行不排队：运行中提交直接拒绝
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
# 3. 通过 manager 跑一个完整生命周期（替身引擎）
# --------------------------------------------------------------------------


def test_job_lifecycle_via_manager_completes_with_callback(tmp_path):
    """通过 manager 跑回测：终态 COMPLETED、进度 ~100、on_complete 被调用。"""
    storage = SQLiteStorage(tmp_path / "lifecycle.db")
    total_days = 12
    reports: list[dict] = []

    def builder(params, hooks, resume_state, checkpoint):
        engine = _PacedEngine(total_days=total_days, pace=None)
        engine.hooks = hooks
        return Execution(engine, lambda report: reports.append(report))

    manager = BacktestJobManager(storage, builder)
    submitted = manager.submit({"name": "生命周期"}, "生命周期回测")
    job = wait_for_terminal(manager, storage, submitted["job_id"])

    assert job["status"] == "COMPLETED"
    assert job["progress"] == pytest.approx(100.0)
    assert job["total_days"] == total_days
    assert len(reports) == 1 and reports[0]["status"] == "COMPLETED"   # on_complete 哨兵
    assert storage.get_checkpoint(submitted["job_id"]) is None         # 完成后清检查点


# --------------------------------------------------------------------------
# 4. 通过 manager 取消，再续传到完成
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
# 5. 进程重启恢复：RUNNING → INTERRUPTED → 可续传
# --------------------------------------------------------------------------


def _seed_orphan_job(storage: SQLiteStorage, status: str = "RUNNING") -> str:
    now = datetime.now().isoformat(timespec="seconds")
    orphan = str(uuid.uuid4())
    storage.create_job({
        "id": orphan, "name": "遗留任务", "status": status, "progress": 40.0,
        "params": {}, "created_at": now, "updated_at": now,
    })
    return orphan


def _seed_orphan_running_job(storage: SQLiteStorage) -> str:
    return _seed_orphan_job(storage, "RUNNING")


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


def test_recover_converts_zombie_cancelling_to_resumable_cancelled(tmp_path):
    """进程重启时正处 CANCELLING 的任务：取消事件随进程丢失、worker 已死，
    必须转为 CANCELLED（可续传终态），否则永远卡在非终态、前端刷新后无法续跑。"""
    storage = SQLiteStorage(tmp_path / "recover_cancelling.db")

    def builder(params, hooks, resume_state, checkpoint):  # pragma: no cover - 不会被调用
        raise AssertionError("本用例不应真正运行引擎")

    manager = BacktestJobManager(storage, builder)
    cancelling = _seed_orphan_job(storage, "CANCELLING")
    running = _seed_orphan_job(storage, "RUNNING")

    assert manager.recover_interrupted() == 2
    job = storage.get_job(cancelling)
    assert job["status"] == "CANCELLED"
    assert "可续传" in (job["error"] or "")
    assert storage.get_job(running)["status"] == "INTERRUPTED"

    # CANCELLED 在可续传集合内：能直接续跑到完成
    finished: list[dict] = []

    def resume_builder(params, hooks, resume_state, checkpoint):
        engine = _PacedEngine(total_days=2, pace=None)
        engine.hooks = hooks
        return Execution(engine, lambda report: finished.append(report))

    manager._builder = resume_builder
    manager.resume(cancelling)
    assert wait_for_terminal(manager, storage, cancelling)["status"] == "COMPLETED"
    assert len(finished) == 1


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


def test_update_job_error_none_clears_persisted_error(tmp_path):
    """update_job(error=None) 必须真把 error 写成 NULL（清空）。

    回归锁：旧实现里所有列共用「value is not None 才更新」的约定，导致 resume 时
    传 error=None 被当成「没传这个参数」跳过，中断时落库的错误文案永远清不掉，
    前端点「继续」后红字提示一路残留（2026-11-24 踩坑）。
    """
    storage = SQLiteStorage(tmp_path / "clear_error.db")
    job_id = _seed_orphan_job(storage, "INTERRUPTED")
    storage.update_job(job_id, error="进程重启导致回测中断，可从检查点续传。")
    assert storage.get_job(job_id)["error"] == "进程重启导致回测中断，可从检查点续传。"

    storage.update_job(job_id, error=None)   # resume 续跑时的清空动作
    assert storage.get_job(job_id)["error"] is None


def test_resume_clears_interrupted_error_in_progress_view(tmp_path):
    """resume 后轮询 get_progress 拿到的 error 必须为空——前端红字提示据此消失。

    复刻线上场景：recover_interrupted 写入中断 error → 点「继续」resume →
    轮询期间/终态 get_progress 都不应再带回中断文案（否则前端红字残留到终态）。
    """
    storage = SQLiteStorage(tmp_path / "resume_clear.db")
    finished: list[dict] = []

    def builder(params, hooks, resume_state, checkpoint):
        engine = _PacedEngine(total_days=2, pace=None)
        engine.hooks = hooks
        return Execution(engine, lambda report: finished.append(report))

    manager = BacktestJobManager(storage, builder)
    job_id = _seed_orphan_running_job(storage)
    manager.recover_interrupted()
    assert manager.get_progress(job_id)["error"]     # 中断时确有 error

    manager.resume(job_id)                           # 点「继续」
    assert manager.get_progress(job_id)["error"] is None   # 续跑即清（轮询不再带回红字）
    job = wait_for_terminal(manager, storage, job_id)
    assert job["status"] == "COMPLETED"
    assert job["error"] is None                      # 终态也不残留中断文案


# --------------------------------------------------------------------------
# 6. 边界
# --------------------------------------------------------------------------


def test_cancel_before_first_day_returns_cancelled_report():
    """取消在首个交易日处理前触发：引擎返回 CANCELLED + 空曲线，而不是抛错。"""
    engine = _PacedEngine(total_days=4, pace=None)
    engine.hooks = {"should_cancel": lambda: True}
    report = engine.run()
    assert report["status"] == "CANCELLED"
    assert report["equity_curve"] == []
    assert report["trades"] == []


# --------------------------------------------------------------------------
# 7. 放弃删除（前端「放弃」按钮 → DELETE /backtests/jobs/{id}）
# --------------------------------------------------------------------------


def test_delete_removes_terminal_job_and_checkpoint(tmp_path):
    """放弃删除：终态作业（含检查点）被彻底清掉，list_jobs 不再返回。"""
    storage = SQLiteStorage(tmp_path / "delete.db")
    cancelled = _seed_orphan_job(storage, "CANCELLED")
    storage.save_checkpoint(cancelled, "2025-01-24", 12, {}, {}, {"cursor_index": 12})
    assert storage.get_checkpoint(cancelled) is not None

    def builder(params, hooks, resume_state, checkpoint):  # pragma: no cover
        raise AssertionError("本用例不应真正运行引擎")

    manager = BacktestJobManager(storage, builder)
    result = manager.delete(cancelled)

    assert result["deleted"] is True
    assert result["status"] == "CANCELLED"
    assert storage.get_job(cancelled) is None           # 作业行已删
    assert storage.get_checkpoint(cancelled) is None    # 检查点一并清掉
    assert all(job["id"] != cancelled for job in manager.list_jobs(50))


def test_delete_refuses_running_job(tmp_path):
    """RUNNING 作业不可放弃删除：可能真有 worker 在跑，删了会失去唯一状态账本。"""
    storage = SQLiteStorage(tmp_path / "delete_running.db")

    def builder(params, hooks, resume_state, checkpoint):  # pragma: no cover
        raise AssertionError("本用例不应真正运行引擎")

    manager = BacktestJobManager(storage, builder)
    running = _seed_orphan_job(storage, "RUNNING")

    with pytest.raises(ValueError):
        manager.delete(running)
    assert storage.get_job(running) is not None   # 未被删除


def test_delete_missing_job_raises_key_error(tmp_path):
    storage = SQLiteStorage(tmp_path / "delete_missing.db")

    def builder(params, hooks, resume_state, checkpoint):  # pragma: no cover
        raise AssertionError("本用例不应真正运行引擎")

    manager = BacktestJobManager(storage, builder)
    with pytest.raises(KeyError):
        manager.delete("no-such-job")
