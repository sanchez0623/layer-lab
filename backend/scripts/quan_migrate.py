"""把本服务需要的「新数据」增量迁移进 quan_quant 的数据目录（**只追加，不覆盖**）。

背景：quan_quant 的 ``index_daily.parquet`` 只有 000300 / 000905，缺少选股 L0 大盘门
需要的 创业板指 399006 与 科创50 000688。本脚本从腾讯行情拉这两个指数的日线，
**只把库里不存在的 (index_key, date) 行追加进去**：

- 已存在的行（任何 index_key / date）一律不修改、不删除；
- 同 key 同日的新行不存在才追加；重复行跳过（幂等，可反复执行）；
- 写盘前在数据目录留一份时间戳备份，再用临时文件 + os.replace 原子替换；
- 写盘后校验「原库全部 key 仍在、原有行数不变的新增行数」。
- 未收盘的当日 bar（交易日 15:05 前）不写入，避免把盘中半截数据固化。

为什么只迁移指数：腾讯个股日线是**前复权**价，而 quan 的 daily.parquet 约定是
**不复权原始价**；把前者写进后者会污染 quan 库。指数没有除权除息，前复权=原始价，
因此只有指数数据可安全迁移。个股日线仍由 quan_quant 自己的更新器维护。

用法::

    cd backend
    python -m scripts.quan_migrate index                 # 默认回填 399006 / 000688
    python -m scripts.quan_migrate index --dry-run       # 只看会新增多少行，不写盘
    python -m scripts.quan_migrate index --keys 399006,000688 --count 2000
    python -m scripts.quan_migrate index --data-dir D:\\path\\to\\quan_quant\\data

环境变量 ``WB_QUAN_DATA_DIR`` 可作为默认数据目录。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Any

import requests

DEFAULT_QUAN_DATA_DIR = r"D:\Sanchez\AI\TraeProjects\quan_quant\data"
DEFAULT_INDEX_KEYS = ("399006", "000688")
TENCENT_INDEX_DAILY_URL = "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{count},qfq"
HTTP_TIMEOUT = 20.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LayerLab/1.0"

INDEX_COLUMNS = ("index_key", "date", "open", "high", "low", "close", "volume", "amount")


def resolve_data_dir(raw: str | None) -> Path:
    if raw:
        return Path(raw)
    env = os.environ.get("WB_QUAN_DATA_DIR")
    if env:
        return Path(env)
    return Path(DEFAULT_QUAN_DATA_DIR)


def tencent_index_symbol(index_key: str) -> str:
    """6 位指数代码 → 腾讯符号。000xxx=沪市指数，399xxx=深市指数，899xxx=北证。"""
    key = str(index_key).strip()
    if key.startswith(("399", "159")):
        return f"sz{key}"
    if key.startswith("899"):
        return f"bj{key}"
    return f"sh{key}"


def fetch_index_daily(index_key: str, count: int = 2000) -> list[dict[str, Any]]:
    """腾讯指数日线（前复权=原始点位，指数无除权）。返回升序行。"""
    symbol = tencent_index_symbol(index_key)
    url = TENCENT_INDEX_DAILY_URL.format(symbol=symbol, count=max(1, min(int(count), 2000)))
    response = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise ValueError(f"腾讯指数日线返回错误: {payload.get('msg')}")
    node = (payload.get("data") or {}).get(symbol) or {}
    rows = node.get("day") or node.get("qfqday") or []
    result: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            date = str(row[0])[:10]
            open_ = float(row[1])
            close = float(row[2])
            high = float(row[3])
            low = float(row[4])
            volume = float(row[5])
        except (TypeError, ValueError):
            continue
        if min(open_, high, low, close) <= 0:
            continue
        result.append({
            "index_key": str(index_key),
            "date": date,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": 0.0,  # 腾讯指数日线不返回成交额
        })
    result.sort(key=lambda item: item["date"])
    if not result:
        raise ValueError(f"腾讯没有返回指数 {index_key}（{symbol}）的日线")
    return result


def _drop_unfinished_today(rows: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """交易日 15:05 前不把当日半截 bar 写库。"""
    moment = now or datetime.now()
    today = moment.date().isoformat()
    if moment.time() >= time(15, 5):
        return rows
    return [row for row in rows if row["date"] != today]


def merge_append_only(existing: Any, incoming: Any, pl: Any) -> tuple[Any, int, int]:
    """只追加库里不存在的 (index_key,date)；已存在的行原样保留。

    返回 ``(merged, added, duplicates)``。``existing`` 为空时按新表处理。
    """
    if existing is None or existing.height == 0:
        base = incoming.select(INDEX_COLUMNS)
        base = base.unique(subset=["index_key", "date"], keep="first").sort(["index_key", "date"])
        return base, int(base.height), 0
    if incoming.height == 0:
        return existing, 0, 0
    incoming = incoming.select(INDEX_COLUMNS)
    existing_keys = existing.select(["index_key", "date"]).unique()
    new_rows = incoming.join(existing_keys, on=["index_key", "date"], how="anti")
    duplicates = int(incoming.height - new_rows.height)
    if new_rows.height == 0:
        return existing, 0, duplicates
    merged = pl.concat([existing.select(INDEX_COLUMNS), new_rows], how="vertical")
    merged = merged.unique(subset=["index_key", "date"], keep="first").sort(["index_key", "date"])
    return merged, int(new_rows.height), duplicates


def migrate_index(
    data_dir: Path, keys: list[str], *, count: int = 2000, dry_run: bool = False,
) -> dict[str, Any]:
    """把指数新行增量并入 ``index_daily.parquet``（不覆盖已有行）。"""
    try:
        import polars as pl
    except ImportError as exc:  # pragma: no cover - 环境缺 polars
        raise SystemExit("需要 polars 读写 parquet：pip install 'polars>=1.0,<2'") from exc

    path = data_dir / "index_daily.parquet"
    if not path.exists():
        raise SystemExit(f"找不到 {path}；请确认 quan 数据目录")

    existing = pl.read_parquet(path)
    incoming_frames = []
    errors: list[str] = []
    for key in dict.fromkeys(keys):
        try:
            rows = _drop_unfinished_today(fetch_index_daily(key, count=count))
            incoming_frames.append(rows)
        except Exception as exc:  # noqa: BLE001 - 单指数失败不阻塞其他指数
            errors.append(f"{key}: {exc}")
    if not incoming_frames:
        return {
            "changed": False, "dry_run": dry_run, "errors": errors,
            "rows_before": int(existing.height), "rows_after": int(existing.height),
        }

    normalized = []
    for rows in incoming_frames:
        for row in rows:
            normalized.append(row)
    incoming = pl.DataFrame(normalized, schema={name: pl.Float64 for name in ("open", "high", "low", "close", "volume", "amount")} | {
        "index_key": pl.String, "date": pl.String,
    })
    merged, added, duplicates = merge_append_only(existing, incoming, pl)
    summary: dict[str, Any] = {
        "path": str(path),
        "dry_run": bool(dry_run),
        "rows_before": int(existing.height),
        "rows_after": int(merged.height),
        "added": added,
        "duplicates_skipped": duplicates,
        "errors": errors,
        "keys_before": sorted(existing["index_key"].unique().to_list()),
        "keys_after": sorted(merged["index_key"].unique().to_list()),
        "changed": False,
    }
    if added <= 0:
        return summary

    # ---- 不覆盖校验：原库每一行都必须原样还在 ----
    before_counts = existing.group_by("index_key").agg(pl.len().alias("n")).sort("index_key")
    after_counts = merged.group_by("index_key").agg(pl.len().alias("n")).sort("index_key")
    before_map = dict(zip(before_counts["index_key"].to_list(), before_counts["n"].to_list()))
    after_map = dict(zip(after_counts["index_key"].to_list(), after_counts["n"].to_list()))
    for key, count_before in before_map.items():
        if int(after_map.get(key, 0)) < int(count_before):
            raise SystemExit(f"追加后 {key} 行数 {after_map.get(key)} < 原 {count_before}，拒绝写盘")
    expected = int(existing.height) + added
    if int(merged.height) != expected:
        raise SystemExit(f"追加后行数 {merged.height} != 原 {existing.height} + 新增 {added}，拒绝写盘")
    if not set(before_map).issubset(set(after_map)):
        raise SystemExit("追加后丢失原有指数 key，拒绝写盘")
    summary["changed"] = True
    if dry_run:
        return summary

    # ---- 备份 + 临时文件原子替换 ----
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"index_daily.parquet.bak_layerlab_{stamp}")
    shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".layerlab.tmp")
    try:
        merged.write_parquet(tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    # ---- 写盘后回读校验 ----
    reloaded = pl.read_parquet(path)
    if int(reloaded.height) != int(merged.height):
        raise SystemExit(f"写盘后回读行数 {reloaded.height} != {merged.height}；备份在 {backup}")
    summary["backup"] = str(backup)
    summary["rows_after"] = int(reloaded.height)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="增量迁移新数据进 quan_quant 数据目录（只追加不覆盖）")
    sub = parser.add_subparsers(dest="command", required=True)

    index_parser = sub.add_parser("index", help="回填指数日线（399006/000688 等）")
    index_parser.add_argument("--data-dir", default=None, help="quan 数据目录；默认 WB_QUAN_DATA_DIR")
    index_parser.add_argument("--keys", default=",".join(DEFAULT_INDEX_KEYS), help="逗号分隔的 6 位指数代码")
    index_parser.add_argument("--count", type=int, default=2000, help="腾讯返回的最大 bar 数")
    index_parser.add_argument("--dry-run", action="store_true", help="只计算新增量，不写盘")

    args = parser.parse_args(argv)
    if args.command == "index":
        data_dir = resolve_data_dir(args.data_dir)
        keys = [item.strip() for item in str(args.keys).split(",") if item.strip()]
        summary = migrate_index(data_dir, keys, count=args.count, dry_run=args.dry_run)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if not summary.get("errors") else 1
    parser.error("未知子命令")
    return 2


if __name__ == "__main__":
    sys.exit(main())
