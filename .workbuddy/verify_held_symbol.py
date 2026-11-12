"""真实 e2e：账户已持有 sz.300209 时启用轮动，current_symbol 应指向持仓。"""
import json
import sqlite3
import sys

import requests

DB = "backend/data/app.db"
base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010/api"
account_id = sys.argv[2]
run_id = sys.argv[3]

connection = sqlite3.connect(DB, timeout=30)
row = connection.execute("SELECT payload FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
state = json.loads(row[0])
state.setdefault("_positions_internal", {})["sz.300209"] = {
    "symbol": "sz.300209", "permanent_qty": 1000, "permanent_cost": 140000.0,
    "t_qty": 0, "t_cost": 0.0, "today_permanent_buy_qty": 0,
    "today_t_buy_qty": 0, "last_price": 140.0,
}
connection.execute(
    "UPDATE paper_accounts SET payload=? WHERE id=?",
    (json.dumps(state, ensure_ascii=False), account_id),
)
connection.commit()
connection.close()
print("seeded sz.300209 holding", flush=True)

response = requests.post(
    f"{base}/paper/accounts/{account_id}/rotation",
    json={"enabled": True, "run_id": run_id}, timeout=60,
)
print("rotation HTTP", response.status_code, flush=True)
rotation = response.json()["rotation"]
print(json.dumps({key: rotation.get(key) for key in (
    "mode", "enabled", "current_symbol", "index", "last_run_id", "waiting", "note")},
    ensure_ascii=False), flush=True)
print("pool[0..2]=", json.dumps(rotation["pool"][:3], ensure_ascii=False), flush=True)
