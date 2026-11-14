"""回测后台任务管理器：串行队列 + 断点续传 + 可取消 + 进度可见。

设计约束（对齐 AGENTS.md 与用户决策）：
- **单工作线程串行、不排队**：同一时刻只允许一个回测运行；运行期间新提交直接拒绝
  （返回「已有回测正在运行」提示），不进入等待队列。
- **断点续传**：每个交易日边界把账户/策略/引擎状态写检查点；被中断（取消、进程重启、
  发布新版本重建容器）后可从检查点接着跑，结果与一次性跑完逐字段一致。
- **不碰撮合语义**：本模块只做任务调度与状态落盘，撮合/决策全部在 backtest.py 引擎内。
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Callable

from .domain import utc_now

RESUMABLE_STATUSES = {"CANCELLED", "INTERRUPTED", "FAILED"}


class BacktestBusyError(RuntimeError):
    """已有回测正在运行，拒绝并发提交（串行不排队）。"""


class Execution:
    """builder 的返回值：引擎 + 完成回调（落最终报告 / 瘦身换票轨迹）。"""

    def __init__(self, engine: Any, on_complete: Callable[[dict[str, Any]], Any]) -> None:
        self.engine = engine
        self.on_complete = on_complete


class BacktestJobManager:
    """管理回测后台任务的生命周期。

    builder(params, hooks, resume_state, checkpoint) -> Execution：
      由 main.py 注入的数据依赖工厂，按 params 重建引擎；hooks 透传给引擎；
      resume_state/checkpoint 非空时把引擎账户/策略恢复到检查点。
      返回 Execution(engine, on_complete)，on_complete(report) 落最终报告并返回 API 响应。
    """

    def __init__(self, storage: Any, builder: Callable[..., Execution]) -> None:
        self._storage = storage
        self._builder = builder
        self._lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._current_job_id: str | None = None

    # -- 对外查询 ---------------------------------------------------------

    @property
    def current_job_id(self) -> str | None:
        with self._lock:
            return self._current_job_id

    def is_busy(self) -> bool:
        with self._lock:
            return self._current_job_id is not None

    @staticmethod
    def _job_view(job: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": job["id"], "name": job["name"], "status": job["status"],
            "progress": job["progress"], "cursor_date": job["cursor_date"],
            "total_days": job["total_days"], "error": job["error"],
            "created_at": job["created_at"], "updated_at": job["updated_at"],
        }

    def get_progress(self, job_id: str) -> dict[str, Any] | None:
        job = self._storage.get_job(job_id)
        return self._job_view(job) if job else None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self._job_view(job) for job in self._storage.list_jobs(limit=limit)]

    # -- 提交 / 取消 / 续传 ----------------------------------------------

    def submit(self, params: dict[str, Any], name: str) -> dict[str, Any]:
        with self._lock:
            if self._current_job_id is not None:
                raise BacktestBusyError("已有回测正在运行，请等待其完成后再提交（不支持并发/排队）。")
            job_id = str(uuid.uuid4())
            now = utc_now()
            self._storage.create_job({
                "id": job_id, "name": name, "status": "RUNNING", "progress": 0.0,
                "total_days": None, "params": params, "created_at": now, "updated_at": now,
            })
            self._current_job_id = job_id
            self._start_worker(job_id, params, resume_state=None)
            return {"job_id": job_id, "status": "RUNNING"}

    def resume(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if self._current_job_id is not None:
                raise BacktestBusyError("已有回测正在运行，请等待其完成后再续传。")
            job = self._storage.get_job(job_id)
            if not job:
                raise KeyError(job_id)
            if job["status"] not in RESUMABLE_STATUSES:
                raise ValueError(
                    f"任务状态为 {job['status']}，不可续传（仅 {sorted(RESUMABLE_STATUSES)} 可续传）。"
                )
            checkpoint = self._storage.get_checkpoint(job_id)
            resume_state = checkpoint["engine_state"] if checkpoint else None
            self._storage.update_job(job_id, status="RUNNING", error=None)
            self._current_job_id = job_id
            self._start_worker(job_id, job["params"], resume_state=resume_state, checkpoint=checkpoint)
            return {"job_id": job_id, "status": "RUNNING", "resumed": resume_state is not None}

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if self._current_job_id != job_id:
                job = self._storage.get_job(job_id)
                if not job:
                    raise KeyError(job_id)
                return {"job_id": job_id, "status": job["status"], "cancelled": False,
                        "reason": "该任务当前未在运行。"}
            self._cancel_event.set()
            return {"job_id": job_id, "status": "CANCELLING", "cancelled": True}

    def recover_interrupted(self) -> int:
        """进程启动时把遗留的 RUNNING 任务标记为 INTERRUPTED（容器重启=被打断），可续传。"""
        count = 0
        for job in self._storage.list_jobs_by_status("RUNNING"):
            self._storage.update_job(job["id"], status="INTERRUPTED",
                                     error="进程重启导致回测中断，可从检查点续传。")
            count += 1
        return count

    # -- 内部：工作线程 ---------------------------------------------------

    def _start_worker(self, job_id: str, params: dict[str, Any],
                      resume_state: dict[str, Any] | None,
                      checkpoint: dict[str, Any] | None = None) -> None:
        self._cancel_event.clear()
        # 可变容器：progress 回调在 builder 构造引擎之后才可能触发（引擎 run 时才回调），
        # 这里用 holder 让闭包在触发时读到已构造好的引擎，避免引用未赋值的局部变量。
        holder: dict[str, Any] = {}

        def on_progress(done: int, total: int, cursor_date: str, engine_state: dict[str, Any]) -> None:
            # 引擎交易日边界同步回调；读取账户/策略快照一并落检查点 + 更新进度。
            with self._lock:
                job = self._storage.get_job(job_id)
                if not job or job["status"] != "RUNNING":
                    return
                progress = round(100.0 * done / total, 2) if total else 0.0
                self._storage.update_job(job_id, progress=progress, cursor_date=cursor_date,
                                         cursor_index=engine_state.get("cursor_index"), total_days=total)
                engine = holder.get("engine")
                account_state = engine.account.snapshot_state() if engine is not None else {}
                strategy_state = engine.strategy.state_dict() if engine is not None else {}
                self._storage.save_checkpoint(
                    job_id, cursor_date, engine_state.get("cursor_index"),
                    account_state, strategy_state, engine_state,
                )

        def should_cancel() -> bool:
            return self._cancel_event.is_set()

        def worker() -> None:
            try:
                execution = self._builder(
                    params,
                    {"on_progress": on_progress, "should_cancel": should_cancel},
                    resume_state, checkpoint,
                )
                holder["engine"] = execution.engine
                self._restore_from_checkpoint(execution.engine, checkpoint)
                report = execution.engine.run(resume_state=resume_state)
                if report.get("status") == "CANCELLED":
                    self._storage.update_job(job_id, status="CANCELLED",
                                             error="已按请求在交易日边界停止，可从检查点续传。")
                else:
                    # job_id 即报告 id：前端完成后据 job_id 取完整报告（GET /api/backtests/{id}）。
                    report["id"] = job_id
                    execution.on_complete(report)
                    self._storage.update_job(job_id, status="COMPLETED", progress=100.0)
                    self._storage.delete_checkpoint(job_id)
            except Exception as cause:  # noqa: BLE001 任务线程兜底，错误落库供前端展示
                self._storage.update_job(job_id, status="FAILED", error=str(cause))
            finally:
                with self._lock:
                    if self._current_job_id == job_id:
                        self._current_job_id = None
                    self._cancel_event.clear()

        thread = threading.Thread(target=worker, name=f"backtest-job-{job_id[:8]}", daemon=True)
        thread.start()

    @staticmethod
    def _restore_from_checkpoint(engine: Any, checkpoint: dict[str, Any] | None) -> None:
        if not checkpoint:
            return
        account_state = checkpoint.get("account_state") or {}
        strategy_state = checkpoint.get("strategy_state") or {}
        if account_state and hasattr(engine, "account"):
            engine.account.restore_state(account_state)
        if strategy_state and hasattr(engine, "strategy"):
            engine.strategy.load_state(strategy_state)