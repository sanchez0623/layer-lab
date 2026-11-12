"""轮动 v2 真实 e2e：模拟「刚清仓」→ live-tick 触发重新选股。"""
import json
import sqlite3
import sys

import requests

DB = "backend/data/app.db"
base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010/api"
account_id = sys.argv[2]

before_runs = len(requests.get(f"{base}/screener/runs?limit=100", timeout=20).json())

# 1) 直接把「刚清仓」状态写进账户（验证用账户，不改用户其他账户）
connection = sqlite3.connect(DB, timeout=30)
row = connection.execute("SELECT payload FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
if row is None:
    print("account not found")
    sys.exit(1)
state = json.loads(row[0])
state["rotation_pending_advance"] = True
connection.execute(
    "UPDATE paper_accounts SET payload=?, updated_at=datetime('now') WHERE id=?",
    (json.dumps(state, ensure_ascii=False), account_id),
)
connection.commit()
connection.close()
print("pending_advance set, rotation_pre=", json.dumps(
    (state.get("rotation") or {}), ensure_ascii=False)[:200], flush=True)

# 2) live-tick：应触发重筛（用缓存日线，include_realtime=False）
current = (state.get("rotation") or {}).get("current_symbol") or "sh.688200"
symbol = current.split(".", 1)[1]
response = requests.post(
    f"{base}/paper/accounts/{account_id}/live-tick", json={"symbol": symbol}, timeout=300,
)
print(f"live-tick HTTP {response.status_code}", flush=True)
if response.status_code != 200:
    print(response.text[:800])
    sys.exit(1)
result = response.json()
rotation = result.get("rotation") or {}
print("after: pending=", result.get("rotation_pending_advance"),
      "seq=", result["sequence_index"], flush=True)
print("rotation:", json.dumps({key: rotation.get(key) for key in (
    "enabled", "mode", "current_symbol", "index", "waiting", "note",
    "last_attempt_date", "last_run_id")}, ensure_ascii=False), flush=True)
print("last_screen:", json.dumps(rotation.get("last_screen"), ensure_ascii=False), flush=True)

after_runs = requests.get(f"{base}/screener/runs?limit=100", timeout=20).json()
print(f"runs before={before_runs} after={len(after_runs)} "
      f"newest={after_runs[0]['id'] if after_runs else None}", flush=True)
newest = requests.get(f"{base}/screener/runs/{after_runs[0]['id']}", timeout=20).json()
print("newest run first=", json.dumps(newest.get("first"), ensure_ascii=False)[:200],
      "funnel=", json.dumps(newest.get("funnel"), ensure_ascii=False), flush=True)
