# -*- coding: utf-8 -*-
"""实测：1 年 / 3 年窗口在现有 universe.db 下的选股漏斗与数据可用性。"""
import sys
from collections import Counter
from datetime import date

sys.path.insert(0, r"D:\Sanchez\AI\GithubProjects\layer-lab\backend")

from app.universe import UniverseStore, default_universe_db  # noqa: E402
from app.screener import ScreenerConfig, run_screener  # noqa: E402

store = UniverseStore(default_universe_db())
cfg = ScreenerConfig(enable_fundamental_veto=False)

dates = []
import sqlite3
conn = sqlite3.connect(str(default_universe_db()))
dates = [r[0] for r in conn.execute("select distinct date from universe_daily order by date")]
print("cache trading days:", len(dates), dates[0], "..", dates[-1])

def sessions_between(a, b):
    return sum(1 for d in dates if a <= d <= b)

print("sessions 2025-11-12..2026-11-11:", sessions_between("2025-11-12", "2026-11-11"))
print("sessions 2023-11-12..2026-11-11:", sessions_between("2023-11-12", "2026-11-11"), "(cache only has)", sessions_between("1900-01-01", "2026-11-11"))

# 各代码历史长度分档
rows = conn.execute("select code, count(*) n, min(date) from universe_daily group by code").fetchall()
buckets = Counter()
for code, n, f in rows:
    if n >= 250: buckets[">=250"] += 1
    elif n >= 100: buckets["100-249"] += 1
    elif n >= 21: buckets["21-99"] += 1
    else: buckets["<21"] += 1
print("per-code history depth buckets:", dict(buckets))
short = [r for r in rows if r[1] < 250]
print("codes with <250 sessions:", len(short), "examples:", short[:3])

for as_of in ("2025-11-12", "2026-11-11", "2024-11-12"):
    print("\n== run_screener as_of", as_of, "==")
    try:
        res = run_screener(store, None, cfg, as_of=as_of, include_realtime=False)
        print("gate:", {b: (v["open"], round(v["ret20"], 4) if v.get("ret20") is not None else None) for b, v in res.gate["boards"].items()})
        print("funnel:", res.funnel, "first:", res.first.code if res.first else None)
        for w in res.warnings:
            print("warning:", w)
    except Exception as exc:
        print("ERROR:", type(exc).__name__, exc)

# 漏斗回测实际会载入多少历史（260 截断）
hist = store.load_history([r[0] for r in rows], max_rows=260)
lens = [len(v) for v in hist.values() if v]
print("\nload_history(260) rows per code: min", min(lens), "max", max(lens))

# 模拟漏斗回测 timeline（start=2025-11-12 与 2023-11-12）
for start in ("2025-11-12", "2023-11-12"):
    tl = sorted({b.date for v in hist.values() for b in v if b.date >= start and b.date <= "2026-11-11"})
    print(f"funnel timeline start={start}: {len(tl)} sessions", (tl[0], tl[-1]) if tl else None)
