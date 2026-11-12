# -*- coding: utf-8 -*-
"""一次性数据覆盖审计（只读），用于回答「1年/3年回测缺什么数据」。"""
import sqlite3
import json
from collections import Counter

DB = r"D:\Sanchez\AI\GithubProjects\layer-lab\backend\data\universe.db"
APP = r"D:\Sanchez\AI\GithubProjects\layer-lab\backend\data\app.db"

conn = sqlite3.connect(DB)
c = conn.cursor()

print("== universe_daily ==")
print("rows:", c.execute("select count(*) from universe_daily").fetchone()[0])
print("min/max date:", c.execute("select min(date),max(date) from universe_daily").fetchone())
print("distinct codes with daily:", c.execute("select count(distinct code) from universe_daily").fetchone()[0])

# per-code row count and first date distribution
rows = c.execute("select code, count(*) n, min(date) f, max(date) l from universe_daily group by code").fetchall()
print("codes:", len(rows))
n_counts = sorted(r[1] for r in rows)
print("rows per code: min %d p10 %d median %d p90 %d max %d" % (
    n_counts[0], n_counts[len(n_counts)//10], n_counts[len(n_counts)//2],
    n_counts[len(n_counts)*9//10], n_counts[-1]))

first_dates = Counter(r[2][:7] for r in rows)
print("first-date month distribution (top 12):", first_dates.most_common(12))
last_dates = Counter(r[3] for r in rows)
print("last-date distribution (top 5):", last_dates.most_common(5))

# trading-day calendar (union of dates)
dates = [r[0] for r in c.execute("select distinct date from universe_daily order by date")]
print("trading days in cache:", len(dates), dates[0], "..", dates[-1])

# per-month total rows
month_rows = Counter(d[:7] for d in dates)
print("month -> trading days:", sorted(month_rows.items()))

# 250 sessions back
print("250th trading day from end:", dates[-250] if len(dates) >= 250 else "N/A(<250)")

print("\n== universe_index ==")
for r in c.execute("select code, count(*) n, min(date) f, max(date) l from universe_index group by code"):
    print(r)

print("\n== universe_codes ==")
print("codes:", c.execute("select count(*) from universe_codes").fetchone()[0])
for r in c.execute("select board, count(*) from universe_codes group by board"):
    print(r)
print("by status:", c.execute("select status, count(*) from universe_codes group by status").fetchall())
print("with ipo_date:", c.execute("select count(*) from universe_codes where ipo_date<>''").fetchone()[0])

print("\n== fundamentals ==")
print("count:", c.execute("select count(*) from universe_fundamentals").fetchone()[0])

print("\n== meta ==")
for r in c.execute("select key, value from universe_meta"):
    print(r)

# app.db datasets
ac = sqlite3.connect(APP)
print("\n== app.db tables ==")
print([r[0] for r in ac.execute("select name from sqlite_master where type='table'")])
try:
    cols = [d[1] for d in ac.execute("pragma table_info(datasets)")]
    print("datasets cols:", cols)
    for r in ac.execute("select * from datasets order by rowid desc limit 15"):
        print(dict(zip(cols, r)))
except Exception as e:
    print("datasets read error:", e)
try:
    print("\nbacktests count:", ac.execute("select count(*) from backtests").fetchone()[0])
except Exception as e:
    print("backtests error:", e)
