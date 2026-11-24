from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .domain import Dataset, utc_now
from .pagination import paginate

# 模拟盘净值快照来源：实时 tick 与 /advance 数据集回放，统一写入 live 序列
# （/advance 是真实的账户状态推进，净值随状态落库，与 live-tick 路径一致）。
# ``replay`` 标签仍保留：读取端（list_paper_equity_daily 的 source 参数）可查
# 历史 replay 行，但当前写入路径不再产生新的 replay 数据。
PAPER_EQUITY_SOURCES = ("live", "replay")

# 与回测 equity_curve 对齐的字段（回测见 backtest.py 的 equity_curve.append）。
PAPER_EQUITY_FIELDS = (
    "date", "ts", "equity", "cash", "market_value", "beta_value",
    "invested", "position_qty", "beta_qty",
)


def _equity_number(value: Any) -> float | None:
    return None if value is None else float(value)


def _equity_quantity(value: Any) -> int | None:
    return None if value is None else int(value)


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
                CREATE TABLE IF NOT EXISTS paper_equity (
                    account_id TEXT NOT NULL, date TEXT NOT NULL, ts TEXT NOT NULL,
                    equity REAL, cash REAL, market_value REAL, beta_value REAL,
                    invested REAL, position_qty INTEGER, beta_qty INTEGER,
                    source TEXT NOT NULL DEFAULT 'live',
                    PRIMARY KEY (account_id, date, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_equity_account_source
                    ON paper_equity(account_id, source, date);
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
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, username TEXT NOT NULL,
                    expires_at TEXT NOT NULL, created_at TEXT NOT NULL
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

    def query_backtests(
        self, query: str | None = None, page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        """回测报告列表：按 Report ID / 名称筛选 + 后台分页。

        只查摘要列，不解析 payload：长回测单份 payload 可能达十几 MB，
        列表若逐份读取并 JSON 解析会拖慢接口；指标与 data_info 由详情端点下发。
        """
        where = ""
        args: list[Any] = []
        text = (query or "").strip()
        if text:
            # 先转义反斜杠，再转义 LIKE 通配符，避免用户输入的 % / _ 被当成通配符。
            escaped = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            where = " WHERE lower(id) LIKE ? ESCAPE '\\' OR lower(name) LIKE ? ESCAPE '\\'"
            args = [pattern, pattern]
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT id,name,status,created_at FROM backtests{where} ORDER BY created_at DESC,id",
                args,
            ).fetchall()
        headers = [
            {
                "id": row["id"], "name": row["name"],
                "status": row["status"], "created_at": row["created_at"],
            }
            for row in rows
        ]
        return paginate(headers, page=page, page_size=page_size)

    def delete_backtest(self, backtest_id: str) -> bool:
        """删除回测报告，并级联清理同 ID 的后台任务与断点。

        报告 payload 本身已包含成交 / 委托 / 轮次 / 事件 / 换票轨迹，删行即一并清除；
        数据集、选股运行记录、模拟账户属于共享资源，不在此处删除。
        """
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM backtests WHERE id=?", (backtest_id,))
            connection.execute("DELETE FROM backtest_checkpoints WHERE job_id=?", (backtest_id,))
            connection.execute("DELETE FROM backtest_jobs WHERE id=?", (backtest_id,))
        return cursor.rowcount > 0

    def rename_backtest(self, backtest_id: str, new_name: str) -> bool:
        """重命名回测报告：同步更新 name 摘要列与 payload 里的 name 字段。

        列表搜索按 name 列过滤，详情端点按 payload 下发，两者必须一致，否则会出现
        「列表已改名、详情还是旧名」的割裂。payload 单份可能十几 MB，这里只改一个字段，
        仍需整体读改写；改名是低频操作，可接受。
        """
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM backtests WHERE id=?", (backtest_id,),
            ).fetchone()
            if row is None:
                return False
            payload = json.loads(row["payload"])
            payload["name"] = new_name
            connection.execute(
                "UPDATE backtests SET name=?, payload=? WHERE id=?",
                (new_name, json.dumps(payload, ensure_ascii=False, allow_nan=False), backtest_id),
            )
        return True

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

    # -- 模拟盘逐日/逐笔净值快照 ------------------------------------------
    # paper_accounts 只存「当前状态」，会被每次 save_paper 覆盖；逐日净值必须独立
    # 落库才能算 max_drawdown/sharpe/volatility/…。越早开始积累越好（历史不可回补）。

    @staticmethod
    def _check_equity_source(source: str) -> str:
        if source not in PAPER_EQUITY_SOURCES:
            raise ValueError(
                f"未知的净值来源: {source}（仅支持 {'/'.join(PAPER_EQUITY_SOURCES)}）"
            )
        return source

    def save_paper_equity(
        self, account_id: str, row: dict[str, Any], source: str = "live",
    ) -> None:
        """写入一条权益快照（同一 ``(account_id, date, ts)`` 幂等覆盖）。

        ``row`` 至少给 ``date`` / ``ts``，其余字段与回测 ``equity_curve`` 同名；
        缺失的数值列存 NULL。``source`` 区分实时（live）与缓存回放（replay）。
        """
        self._check_equity_source(source)
        day = str(row.get("date") or "")
        ts = str(row.get("ts") or "")
        if not day or not ts:
            raise ValueError("净值快照必须包含 date 与 ts")
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO paper_equity("
                "account_id,date,ts,equity,cash,market_value,beta_value,invested,"
                "position_qty,beta_qty,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account_id, day, ts,
                    _equity_number(row.get("equity")),
                    _equity_number(row.get("cash")),
                    _equity_number(row.get("market_value")),
                    _equity_number(row.get("beta_value")),
                    _equity_number(row.get("invested")),
                    _equity_quantity(row.get("position_qty")),
                    _equity_quantity(row.get("beta_qty")),
                    source,
                ),
            )

    def list_paper_equity(
        self, account_id: str, source: str = "live",
        start: str | None = None, end: str | None = None,
    ) -> list[dict[str, Any]]:
        """逐笔快照（按 date,ts 升序）；``start``/``end`` 为交易日闭区间（YYYY-MM-DD）。"""
        self._check_equity_source(source)
        clauses = ["account_id=?", "source=?"]
        args: list[Any] = [account_id, source]
        if start:
            clauses.append("date>=?")
            args.append(str(start)[:10])
        if end:
            clauses.append("date<=?")
            args.append(str(end)[:10])
        query = (
            "SELECT " + ",".join(PAPER_EQUITY_FIELDS) + " FROM paper_equity "
            "WHERE " + " AND ".join(clauses) + " ORDER BY date ASC, ts ASC"
        )
        with self._connect() as connection:
            rows = connection.execute(query, args).fetchall()
        return [{field: row[field] for field in PAPER_EQUITY_FIELDS} for row in rows]

    def list_paper_equity_daily(
        self, account_id: str, source: str = "live",
        start: str | None = None, end: str | None = None,
    ) -> list[dict[str, Any]]:
        """每交易日只取当日最后一条（ts 最大）——下游指标/曲线默认用这个口径。

        跨日首条不补齐：曲线连续性（如停牌/未 tick 日）由消费方按需 forward-fill。
        """
        by_date: dict[str, dict[str, Any]] = {}
        for row in self.list_paper_equity(account_id, source=source, start=start, end=end):
            by_date[row["date"]] = row
        return list(by_date.values())

    def paper_equity_coverage(self, account_id: str, source: str = "live") -> dict[str, Any]:
        """该账户在某来源下已落库的覆盖区间（按交易日）：``{start,end,count}``。

        无数据时 ``start``/``end`` 为 None、``count`` 为 0；前端可据此标注
        「自 YYYY-MM-DD 起，样本 N 天」。区间不受查询 start/end 过滤影响。
        """
        self._check_equity_source(source)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MIN(date) AS start, MAX(date) AS end, COUNT(DISTINCT date) AS count "
                "FROM paper_equity WHERE account_id=? AND source=?",
                (account_id, source),
            ).fetchone()
        return {
            "start": row["start"] if row else None,
            "end": row["end"] if row else None,
            "count": int(row["count"] or 0) if row else 0,
        }

    def delete_paper_equity(self, account_id: str) -> int:
        """删除某账户的全部净值快照（live + replay），返回删除行数。

        ``POST /reset`` 用它清空旧序列：reset 是「重开账户」（同一 account_id、
        初始资金归位），保留旧行会让净值曲线从旧权益跳回初始资金，max_drawdown/
        sharpe/TWR 等全部失真；旧净值对当前账户无意义，历史不保留可接受。
        """
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM paper_equity WHERE account_id=?", (account_id,),
            )
        return cursor.rowcount

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
        # 其它列沿用「None 即未传、跳过」的约定（避免把已有值抹成 NULL 撞 NOT NULL 约束）。
        for column, value in (
            ("status", status), ("progress", progress), ("cursor_date", cursor_date),
            ("cursor_index", cursor_index), ("total_days", total_days),
        ):
            if value is not None:
                fields.append(column)
                values.append(value)
        # error 例外：必须支持显式清空。resume 续跑时要写 NULL 抹掉中断时落库的错误文案，
        # 若沿用上面的「None 即跳过」，error=None 会被当成没传、永远清不掉，前端点「继续」
        # 后红字提示一路残留（2026-11-24 实测踩坑）。这里始终写入（有文案写文案，None 写 NULL）。
        fields.append("error")
        values.append(error)
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

    def delete_job(self, job_id: str) -> None:
        """删除一条后台作业行（「放弃」用）。调用方负责只对终态作业调用。"""
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM backtest_jobs WHERE id=?", (job_id,))

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

    # ------------------------------------------------------------------
    # 应用级登录：用户 / 会话持久化（替代 nginx HTTP Basic 认证）
    # ------------------------------------------------------------------
    def count_users(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"]) if row else 0

    def create_user(self, username: str, password_hash: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                (username, password_hash, utc_now()),
            )

    def get_user(self, username: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT username,password_hash,created_at FROM users WHERE username=?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return {
            "username": row["username"],
            "password_hash": row["password_hash"],
            "created_at": row["created_at"],
        }

    def create_session(self, username: str, expires_at: str) -> str:
        """新建一条会话并返回令牌；过期时间由调用方按 remember/默认口径算好传入。"""
        from .auth import new_token

        token = new_token()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions(token,username,expires_at,created_at) VALUES(?,?,?,?)",
                (token, username, expires_at, utc_now()),
            )
        return token

    def get_session_username(self, token: str) -> str | None:
        """返回令牌对应用户名；令牌不存在或已过期返回 None（过期会话顺手清掉）。"""
        if not token:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT username,expires_at FROM sessions WHERE token=?", (token,)
            ).fetchone()
        if row is None:
            return None
        if str(row["expires_at"]) <= utc_now():
            self.delete_session(token)
            return None
        return row["username"]

    def delete_session(self, token: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token=?", (token,))

    def change_password(self, username: str, password_hash: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE users SET password_hash=? WHERE username=?",
                (password_hash, username),
            )

    def purge_expired_sessions(self) -> int:
        """清理已过期的会话行，返回删除条数（进程启动时调用，避免表无限膨胀）。"""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (utc_now(),)
            )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
