"""双创日线回补 CLI（开发期预热 / 用户手动更新）。

用法（在 backend/ 下）::

    python -m scripts.refresh_universe --boards 创业板,科创板 --lookback-days 400
    python -m scripts.refresh_universe --codes 688200,300855,688981 --lookback-days 400
    python -m scripts.refresh_universe --boards 创业板 --limit 20 --lookback-days 120

- 元数据为空时先 ``query_stock_basic`` bootstrap（约 65s），24h 内不重复拉取；
- 日线逐票增量：已有最新交易日的票跳过，可断点续跑；
- progress 每 20 票打印一行（stdout unbuffered），结束后打印 coverage。

注意：同一时间只允许一个写 universe.db 的刷新任务；不要和小样本/全量同时跑。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.market import MarketDataService  # noqa: E402
from app.universe import (  # noqa: E402
    SUPPORTED_BOARDS,
    UniverseRefresher,
    UniverseStore,
    default_universe_db,
)


def _split_list(text: str | None) -> list[str] | None:
    if not text:
        return None
    normalized = text.replace("，", ",").replace(";", ",").replace("；", ",")
    items = [item.strip() for item in normalized.split(",")]
    return [item for item in items if item]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.refresh_universe",
        description="双创日线缓存增量回补（baostock 前复权，一次 login 复用全部查询）",
    )
    parser.add_argument(
        "--boards",
        default=",".join(SUPPORTED_BOARDS),
        help=f"板块，逗号分隔，默认 {','.join(SUPPORTED_BOARDS)}",
    )
    parser.add_argument("--codes", default=None, help="指定代码（6 位或标准码），逗号分隔，优先于 --boards")
    parser.add_argument("--lookback-days", type=int, default=400, help="首次回看自然日数，默认 400")
    parser.add_argument("--limit", type=int, default=None, help="最多处理 N 票（调试用）")
    parser.add_argument("--force", action="store_true", help="忽略增量判断，强制重拉（覆盖已有区间）")
    parser.add_argument("--no-bootstrap", action="store_true", help="不自动 bootstrap 元数据")
    parser.add_argument("--sleep", type=float, default=0.02, help="每票之间的限速秒数，默认 0.02")
    parser.add_argument("--db", default=None, help="universe.db 路径，默认 backend/data/universe.db 或 WB_UNIVERSE_DB")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 老环境无 reconfigure
        pass

    args = _build_parser().parse_args(argv)
    db_path = Path(args.db) if args.db else default_universe_db()
    store = UniverseStore(db_path)
    market = MarketDataService(BACKEND_DIR / "data" / "cache")
    refresher = UniverseRefresher(store, market, sleep_seconds=max(0.0, args.sleep))

    boards = _split_list(args.boards)
    codes = _split_list(args.codes)
    if codes:
        boards = None
    label = f"codes={len(codes)}" if codes else f"boards={boards}"
    print(
        f"[refresh] db={db_path} {label} lookback={args.lookback_days}d "
        f"limit={args.limit} force={args.force}",
        flush=True,
    )

    last_printed = 0

    def progress(job: dict) -> None:
        nonlocal last_printed
        done = int(job.get("done") or 0)
        total = int(job.get("total") or 0)
        if done - last_printed >= 20 or (total and done >= total):
            last_printed = done
            print(
                f"[refresh] {done}/{total} current={job.get('current', '')} "
                f"rows={job.get('fetched_rows', 0)} skipped={job.get('skipped', 0)} "
                f"errors={len(job.get('errors') or [])}",
                flush=True,
            )

    started = time.perf_counter()
    try:
        result = refresher.run_once(
            boards=boards,
            codes=codes,
            lookback_days=args.lookback_days,
            force=args.force,
            bootstrap=not args.no_bootstrap,
            progress=progress,
            limit=args.limit,
        )
    except ValueError as exc:
        print(f"[refresh] 失败: {exc}", flush=True)
        return 2
    except KeyboardInterrupt:
        print("[refresh] 已中断（已写入的数据保留，可再次运行断点续跑）", flush=True)
        return 130

    elapsed = time.perf_counter() - started
    print(
        f"[refresh] 完成 fetched_rows={result['fetched_rows']} skipped={result['skipped']} "
        f"errors={len(result['errors'])} elapsed={elapsed:.0f}s",
        flush=True,
    )
    for message in result["errors"][:5]:
        print(f"[refresh] error: {message}", flush=True)
    print("[refresh] coverage=" + json.dumps(result["coverage"], ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
