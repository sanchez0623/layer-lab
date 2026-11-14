from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .domain import Dataset, utc_now


class SQLiteStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backtests (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_orders (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_orders_account ON paper_orders(account_id);
                CREATE INDEX IF NOT EXISTS idx_paper_trades_account ON paper_trades(account_id);
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS screener_runs (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, as_of TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backtest_jobs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0, cursor_date TEXT, cursor_index INTEGER,
                    total_days INTEGER, params TEXT NOT NULL, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backtest_checkpoints (
                    job_id TEXT PRIMARY KEY, cursor_date TEXT, cursor_index INTEGER,
                    account_state TEXT NOT NULL, strategy_state TEXT NOT NULL,
                    engine_state TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)

    def get_setting(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
                (key, value, utc_now()),
            )

    def delete_setting(self, key: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM app_settings WHERE key=?", (key,))

    def save_dataset(self, dataset: Dataset) -> None:
        payload = json.dumps(dataset.to_dict(include_path=True), ensure_ascii=False)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO datasets(id,created_at,payload) VALUES(?,?,?)",
                (dataset.id, dataset.created_at, payload),
            )

    def get_dataset(self, dataset_id: str) -> Dataset | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        return Dataset(**json.loads(row["payload"])) if row else None

    def list_datasets(self) -> list[Dataset]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM datasets ORDER BY created_at DESC, id").fetchall()
        return [Dataset(**json.loads(row["payload"])) for row in rows]

    def save_backtest(self, report: dict[str, Any]) -> None:
        payload = json.dumps(report, ensure_ascii=False, allow_nan=False)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO backtests(id,name,status,created_at,payload) VALUES(?,?,?,?,?)",
                (report["id"], report["name"], report["status"], report["created_at"], payload),
            )

    def get_backtest(self, backtest_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM backtests WHERE id=?", (backtest_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_backtests(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id,name,status,created_at,payload FROM backtests ORDER BY created_at DESC,id"
            ).fetchall()
        result = []
        for row in rows:
            report = json.loads(row["payload"])
            metrics = report.get("metrics", {})
            result.append({
                "id": row["id"], "name": row["name"], "status": row["status"],
                "created_at": row["created_at"],
                "final_equity": metrics.get("final_equity"),
                "total_return": metrics.get("total_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "total_trades": metrics.get("total_trades"),
                "data_info": report.get("data_info", {}),
            })
        return result

    def save_paper(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=False, allow_nan=False)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO paper_accounts(id,updated_at,payload) VALUES(?,?,?)",
                (state["id"], utc_now(), payload),
            )
            connection.execute("DELETE FROM paper_orders WHERE account_id=?", (state["id"],))
            connection.execute("DELETE FROM paper_trades WHERE account_id=?", (state["id"],))
            connection.executemany(
                "INSERT INTO paper_orders(id,account_id,payload) VALUES(?,?,?)",
                [
                    (f"{state['id']}:{item['id']}", state["id"], json.dumps(item, ensure_ascii=False))
                    for item in state.get("orders", [])
                ],
            )
            connection.executemany(
                "INSERT INTO paper_trades(id,account_id,payload) VALUES(?,?,?)",
                [
                    (f"{state['id']}:{item['id']}", state["id"], json.dumps(item, ensure_ascii=False))
                    for item in state.get("trades", [])
                ],
            )

    def get_paper(self, account_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def latest_paper(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM paper_accounts ORDER BY updated_at DESC,id DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_paper(self) -> list[dict[str, Any]]:
        """全部模拟账户（后端定时器据此驱动所有开启自动策略的账户）。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM paper_accounts ORDER BY updated_at DESC,id DESC"
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_screener_run(self, payload: dict[str, Any]) -> None:
        """保存一次完整的选股结果（payload 即 ScreeningResult.to_dict()）。"""
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO screener_runs(id,created_at,as_of,payload) VALUES(?,?,?,?)",
                (
                    payload["id"], payload["created_at"],
                    payload.get("as_of") or "", body,
                ),
            )

    def get_screener_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM screener_runs WHERE id=?", (run_id,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_screener_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """历史运行摘要，供列表页使用，不返回整份候选池。"""
        limit = max(1, int(limit))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id,created_at,as_of,payload FROM screener_runs "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        summaries: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload"])
            gate = payload.get("gate") or {}
            eligible = payload.get("eligible") or []
            first = payload.get("first") or (eligible[0] if eligible else None)
            summaries.append({
                "id": row["id"],
                "created_at": row["created_at"],
                "as_of": row["as_of"],
                "gate_open": gate.get("open"),
                "eligible_count": len(eligible),
                "first": (
                    {
                        "code": first.get("code"),
                        "name": first.get("name"),
                        "chg20d": first.get("chg20d"),
                    } if isinstance(first, dict) else None
                ),
            })
        return summaries

    def delete_screener_run(self, run_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM screener_runs WHERE id=?", (run_id,))
        return cursor.rowcount > 0

    # -- 回测后台任务 / 断点续传 ------------------------------------------

    def create_job(self, job: dict[str, Any]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO backtest_jobs(id,name,status,progress,cursor_date,cursor_index,"
                "total_days,params,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job["id"], job["name"], job["status"], job.get("progress", 0.0),
                    job.get("cursor_date"), job.get("cursor_index"), job.get("total_days"),
                    json.dumps(job.get("params", {}), ensure_ascii=False),
                    job.get("error"), job["created_at"], job["updated_at"],
                ),
            )

    def update_job(
        self, job_id: str, *, status: str | None = None, progress: float | None = None,
        cursor_date: str | None = None, cursor_index: int | None = None,
        total_days: int | None = None, error: str | None = None,
    ) -> None:
        fields = ["updated_at"]
        values: list[Any] = [utc_now()]
        for column, value in (
            ("status", status), ("progress", progress), ("cursor_date", cursor_date),
            ("cursor_index", cursor_index), ("total_days", total_days), ("error", error),
        ):
            if value is not None:
                fields.append(column)
                values.append(value)
        placeholders = ", ".join(f"{name}=?" for name in fields)
        values.append(job_id)
        with self._lock, self._connect() as connection:
            connection.execute(f"UPDATE backtest_jobs SET {placeholders} WHERE id=?", values)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM backtest_jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_from_row(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, int(limit))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM backtest_jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def list_jobs_by_status(self, status: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM backtest_jobs WHERE status=? ORDER BY created_at DESC, id DESC",
                (status,),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "status": row["status"],
            "progress": row["progress"], "cursor_date": row["cursor_date"],
            "cursor_index": row["cursor_index"], "total_days": row["total_days"],
            "params": json.loads(row["params"]), "error": row["error"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def save_checkpoint(
        self, job_id: str, cursor_date: str | None, cursor_index: int,
        account_state: dict[str, Any], strategy_state: dict[str, Any],
        engine_state: dict[str, Any],
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO backtest_checkpoints(job_id,cursor_date,cursor_index,"
                "account_state,strategy_state,engine_state,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    job_id, cursor_date, cursor_index,
                    json.dumps(account_state, ensure_ascii=False, allow_nan=False),
                    json.dumps(strategy_state, ensure_ascii=False, allow_nan=False),
                    json.dumps(engine_state, ensure_ascii=False, allow_nan=False),
                    utc_now(),
                ),
            )

    def get_checkpoint(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM backtest_checkpoints WHERE job_id=?", (job_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "job_id": row["job_id"], "cursor_date": row["cursor_date"],
            "cursor_index": row["cursor_index"],
            "account_state": json.loads(row["account_state"]),
            "strategy_state": json.loads(row["strategy_state"]),
            "engine_state": json.loads(row["engine_state"]),
            "updated_at": row["updated_at"],
        }

    def delete_checkpoint(self, job_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM backtest_checkpoints WHERE job_id=?", (job_id,))

    def find_paper_by_order(self, order_id: str, account_id: str) -> dict[str, Any] | None:
        """按 (account_id, order_id) 精确定位账户。

        本地单号（如 O00000001）在每个账户内都从 1 开始，不能用无账户限定的
        LIMIT 1 猜归属，否则撤单会落到别的账户上。
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT account_id FROM paper_orders "
                "WHERE account_id=? AND (id=? OR json_extract(payload, '$.id')=?) LIMIT 1",
                (account_id, f"{account_id}:{order_id}", order_id),
            ).fetchone()
        return self.get_paper(row["account_id"]) if row else None
