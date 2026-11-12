# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"D:\Sanchez\AI\GithubProjects\layer-lab\backend")
from app.universe import UniverseStore, default_universe_db, BOARD_INDEX
from app.screener import _bar_window

print("db:", default_universe_db())
store = UniverseStore(default_universe_db())
for idx in BOARD_INDEX.values():
    bars = store.load_index(idx, max_rows=260)
    print(idx, "loaded", len(bars), "first", bars[0].date if bars else None, "last", bars[-1].date if bars else None)
    for as_of in ("2025-10-31", "2025-11-12", "2025-11-19", "2025-12-01"):
        w = _bar_window(bars, as_of)
        print("  as_of", as_of, "window", len(w))
