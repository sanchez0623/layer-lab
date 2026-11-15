# -*- coding: utf-8 -*-
"""layer-lab · quan_parquet 每日增量更新（工作日 19:00 由 cron 调用）。

增量刷新 /srv/data/quan/history 下的基础数据，供容器只读挂载的顶层 30 天
滚动窗口（由 post_quan_update.py 重建）使用。

更新范围：
- calendar     交易日历（滚动 60 天，幂等）
- index_daily  基准指数日线（000300/000905 等，滚动 60 天）
- stock_basic  在市/ST/退市元数据（全量快照）
- daily        全市场日线 + 复权因子（**只拉陈旧票**，分块串行）
- minute5      5 分钟线（**只拉陈旧票**，分块串行）

内存约束（关键）：宿主机仅 1.8G RAM。updater 的 _flush_daily 每次 flush 都会
全表读入 existing daily（~630 万行）再合并，峰值内存高。因此**绝不能用全市场
模式**（不传 codes 会遍历全部票、每 500 票 flush 一次、反复全表读 → OOM 并可能
连带杀掉 layer-lab 容器）。正确做法：先用 daily_max_dates（只扫 code/date 两列，
轻量）算出「最新日期落后上一交易日」的陈旧票，分小块（CHUNK）逐块喂给 updater，
单块失败只重试该块，不拖垮整轮。稳态（工作日晚上）陈旧票只有几十~几百只，很快。

设计要点：
- baostock 禁止并发：脚本级 flock 防重入（见 ll_quan_nightly.sh），全程单线程串行。
- 所有日期**动态计算**（相对今天），免维护。
- 单个 scope/块失败记录 error，最后以非 0 退出让 cron 日志可见。

用法：由 ll_quan_nightly.sh 以 /usr/bin/python3.11 调用；也可手动
    python3.11 /root/ll-quan/ll_quan_nightly.py
"""
import os
import sys
import time
import traceback
from datetime import date, timedelta

HISTORY_DIR = "/srv/data/quan/history"
# 必须在 import app.* 之前强制覆盖（不能用 setdefault）：config.DATA_DIR 在 import 时
# 就读死 os.environ["DATA_DIR"]，而 _load_dotenv 不覆盖已存在的变量。若父环境预设了
# 别的 DATA_DIR（如容器路径 /data/quan/history），setdefault 不会生效 → 读错目录。
# 硬覆盖保证始终写宿主机真实数据目录。
os.environ["DATA_DIR"] = HISTORY_DIR
sys.path.insert(0, "/root/quan_quant/backend")

import polars as pl  # noqa: E402

from app import config as _qconfig  # noqa: E402
from app import db  # noqa: E402

db.init_db()
from app.data import store, updater  # noqa: E402

# 路径守卫：config.DATA_DIR 必须解析到宿主机数据目录，否则立刻中止，
# 绝不让 updater 去读/写容器内只读路径（/data/quan/history）。
if str(_qconfig.DATA_DIR) != HISTORY_DIR:
    raise RuntimeError(
        f"DATA_DIR 解析错误：config.DATA_DIR={_qconfig.DATA_DIR}，期望 {HISTORY_DIR}；"
        "请检查环境变量 DATA_DIR 是否被父进程/.env 预设覆盖。"
    )
DAILY_LOOKBACK_DAYS = 30      # 日线回看窗口（自然日）：覆盖节假日缺口
MINUTE5_LOOKBACK_DAYS = 30    # 5 分钟线回看窗口（自然日）
CAL_INDEX_LOOKBACK_DAYS = 60  # 日历/指数滚动窗口（自然日）
CHUNK = 300                   # 每块喂给 updater 的票数：控制单次 flush 的增量规模


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def progress(pct, msg) -> None:
    try:
        log(f"    {float(pct):5.1f}% {msg}")
    except (TypeError, ValueError):
        log(f"    - {msg}")


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _last_open_day() -> str:
    """交易日历中 <= 今天 的最后一个开市日；无日历返回空串。"""
    cal = store.read_calendar(HISTORY_DIR)
    if cal is None or cal.height == 0:
        return ""
    today = date.today().isoformat()
    open_days = sorted(
        str(row["date"])[:10] for row in cal.iter_rows(named=True)
        if str(row["date"])[:10] <= today and int(row.get("is_open") or 0) == 1
    )
    return open_days[-1] if open_days else ""


def _stale_codes(max_dates: dict, expected: str) -> list[str]:
    """max_dates 里最新日期 < expected 的 code（含缺失/空）。expected 为空返回空。"""
    if not expected:
        return []
    return [code for code, last in max_dates.items() if str(last)[:10] < expected]


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def run() -> int:
    failures: list[str] = []
    expected = _last_open_day()
    log(f"期望最新交易日={expected or '未知(无日历)'}")

    # ---- 轻量 scope：日历 / 指数 / 元数据 ----
    def step_light(name: str, **kwargs) -> None:
        log(f"START {name}")
        started = time.time()
        try:
            result = updater.update(data_dir=HISTORY_DIR, progress_cb=progress, **kwargs)
            log(f"DONE {name} {time.time() - started:.0f}s {result}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            log(f"FAIL {name}: {exc}")
            traceback.print_exc()

    cal_window = {"start_date": _days_ago(CAL_INDEX_LOOKBACK_DAYS), "end_date": _days_ago(0)}
    step_light("calendar", scope="calendar", **cal_window)
    step_light("index_daily", scope="index_daily", **cal_window)
    step_light("stock_basic", scope="stock_basic")

    # ---- daily：只拉陈旧票，分块 ----
    daily_max = store.daily_max_dates(HISTORY_DIR)
    stale_daily = _stale_codes(daily_max, expected)
    log(f"daily 陈旧票={len(stale_daily)} / 库内 {len(daily_max)}")
    for idx, chunk in enumerate(_chunked(stale_daily, CHUNK), 1):
        log(f"START daily chunk {idx}（{len(chunk)} 票）")
        started = time.time()
        try:
            result = updater.update(
                data_dir=HISTORY_DIR, progress_cb=progress, scope="daily", codes=chunk,
                start_date=_days_ago(DAILY_LOOKBACK_DAYS), end_date=_days_ago(0),
            )
            log(f"DONE daily chunk {idx} {time.time() - started:.0f}s daily_rows={result.get('daily_rows')}")
        except Exception as exc:  # noqa: BLE001 - 单块失败不阻断其它块
            failures.append(f"daily-chunk{idx}: {type(exc).__name__}: {exc}")
            log(f"FAIL daily chunk {idx}: {exc}")

    # ---- minute5：只拉陈旧票，分块 ----
    minute5_codes = store.list_minute5_codes(HISTORY_DIR)
    minute5_max = {}
    for code in minute5_codes:
        frame = store.read_minute5(code, data_dir=HISTORY_DIR)
        if frame is None or frame.height == 0:
            minute5_max[code] = ""
        else:
            minute5_max[code] = str(frame["date"].max())[:10]
    stale_minute5 = _stale_codes(minute5_max, expected)
    log(f"minute5 陈旧票={len(stale_minute5)} / 库内 {len(minute5_codes)}")
    for idx, chunk in enumerate(_chunked(stale_minute5, CHUNK), 1):
        log(f"START minute5 chunk {idx}（{len(chunk)} 票）")
        started = time.time()
        try:
            result = updater.update(
                data_dir=HISTORY_DIR, progress_cb=progress, scope="minute5", codes=chunk,
                start_date=_days_ago(MINUTE5_LOOKBACK_DAYS), end_date=_days_ago(0),
            )
            log(f"DONE minute5 chunk {idx} {time.time() - started:.0f}s rows={result.get('minute5_rows')}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"minute5-chunk{idx}: {type(exc).__name__}: {exc}")
            log(f"FAIL minute5 chunk {idx}: {exc}")

    if failures:
        log(f"FINISHED_WITH_ERRORS ({len(failures)}) {failures[:5]}")
        return 1
    log("ALL_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())