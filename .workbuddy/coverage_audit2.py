# -*- coding: utf-8 -*-
import sqlite3
DB = r"D:\Sanchez\AI\GithubProjects\layer-lab\backend\data\universe.db"
c = sqlite3.connect(DB).cursor()
print("sample codes with <=40 rows:")
for code, n, f, l, name, ipo, board in c.execute(
    "select d.code, count(*), min(d.date), max(d.date), u.name, u.ipo_date, u.board "
    "from universe_daily d join universe_codes u on u.code=d.code "
    "group by d.code having count(*)<=40 order by d.code limit 20"):
    print(code, n, f, l, name, ipo, board)
print()
print("ipo_date year distribution for codes with <=40 rows:")
for r in c.execute(
    "select substr(u.ipo_date,1,7), count(distinct d.code) from universe_daily d "
    "join universe_codes u on u.code=d.code group by d.code having count(*)<=40"):
    pass
rows = c.execute(
    "select u.ipo_date, count(*) from universe_daily d join universe_codes u on u.code=d.code "
    "group by d.code having count(*)<=40").fetchall()
from collections import Counter
print(Counter(r[0][:7] for r in rows).most_common(10))
print("total <=40 rows codes:", len(rows))
print()
print("codes >=200 rows by board:")
for r in c.execute(
    "select u.board, count(*) from (select code from universe_daily group by code having count(*)>=200) x "
    "join universe_codes u on u.code=x.code group by u.board"):
    print(r)
print()
print("distribution: rows>=200:", c.execute("select count(*) from (select code from universe_daily group by code having count(*)>=200)").fetchone()[0])
print("rows 100-199:", c.execute("select count(*) from (select code from universe_daily group by code having count(*) between 100 and 199)").fetchone()[0])
print("rows 40-99:", c.execute("select count(*) from (select code from universe_daily group by code having count(*) between 40 and 99)").fetchone()[0])
print()
print("first-date for >=200 rows:")
for r in c.execute("select min(date), max(date), count(distinct code) from universe_daily where code in (select code from universe_daily group by code having count(*)>=200)"):
    print(r)
