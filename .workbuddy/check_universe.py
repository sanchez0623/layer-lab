import sqlite3
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "data/universe.db"
connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
for query, label in [
    ("SELECT COUNT(*) FROM universe_codes", "codes"),
    ("SELECT COUNT(*) FROM universe_daily", "daily rows"),
    ("SELECT COUNT(DISTINCT code) FROM universe_daily", "codes with daily"),
    ("SELECT MIN(date), MAX(date) FROM universe_daily", "date span"),
    ("SELECT board, COUNT(*) FROM universe_codes GROUP BY board", "boards"),
]:
    try:
        rows = connection.execute(query).fetchall()
        print(label, rows)
    except Exception as exc:  # noqa: BLE001
        print(label, "ERR", exc)
try:
    print("meta", connection.execute("SELECT key, value FROM universe_meta").fetchall())
except Exception as exc:  # noqa: BLE001
    print("meta ERR", exc)
