"""临时真实数据验收脚本（回补完成后运行，用后即删）。

在 backend/ 下：python -u -m scripts._validate_screener_tmp
输出：控制台摘要 + backend/data/screener_result.json（UTF-8，便于核对中文名）。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.market import MarketDataService  # noqa: E402
from app.screener import ScreenerConfig, run_screener  # noqa: E402
from app.universe import UniverseStore  # noqa: E402

AS_OF = "2026-11-11"
DB = Path(sys.argv[1]) if len(sys.argv) > 1 else BACKEND_DIR / "data" / "universe.db"
OUT = BACKEND_DIR / "data" / "screener_result.json"


def summarize(label: str, result) -> dict:
    data = result.to_dict()
    pool = data["pool"]
    eligible = data["eligible"]
    print(f"\n===== {label} =====")
    print(
        "as_of:", data["as_of"],
        "elapsed:", data["elapsed_seconds"],
        "fundamental:", data["data_info"]["fundamental"],
        "realtime:", data["data_info"]["realtime"],
        "realtime_quotes:", data["data_info"]["realtime_quotes"],
    )
    print("gate:", json.dumps(data["gate"], ensure_ascii=False))
    print("funnel:", json.dumps(data["funnel"], ensure_ascii=False))
    print("pool: ", len(pool), " eligible:", len(eligible), " deep_checked:", sum(1 for c in pool if c["deep_checked"]))
    print("first: ", None if data["first"] is None else {key: data["first"][key] for key in ("code", "name", "board", "close", "chg20d", "drawdown_52w", "day_change", "amount", "avg_amount_20d", "turnover", "sessions")})
    print("warnings:", json.dumps(data["warnings"], ensure_ascii=False))
    json.dumps(data, ensure_ascii=False, allow_nan=False)
    print("json_safe: True")
    return data


store = UniverseStore(DB)
market = MarketDataService(BACKEND_DIR / "data" / "cache")
print("db:", DB)
print("coverage:", json.dumps(store.coverage(), ensure_ascii=False))

started = time.perf_counter()
default_run = run_screener(store, market, ScreenerConfig(), as_of=AS_OF)
data = summarize("验收1：默认配置 + 财务深筛（真实 baostock）", default_run)
OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
print("result json:", OUT)

started = time.perf_counter()
cached_run = run_screener(store, market, ScreenerConfig(), as_of=AS_OF)
summarize("验收2：同配置再跑（财务走缓存）", cached_run)
print("cached_run wall_seconds:", round(time.perf_counter() - started, 2))

realtime_run = run_screener(store, market, ScreenerConfig(), as_of=AS_OF, include_realtime=True)
summarize("验收3：include_realtime 腾讯快照叠加", realtime_run)

print("\ncoverage_after:", json.dumps(store.coverage(), ensure_ascii=False))
