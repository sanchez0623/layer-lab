"""真实 live-tick 端到端：选股带入的轮动账户 → 腾讯快照 → 自动策略首笔。"""
import json
import sys

import requests

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020/api"
account_id = sys.argv[2]
symbol = sys.argv[3] if len(sys.argv) > 3 else "688200"

before = requests.get(f"{base}/paper/accounts/{account_id}", timeout=20).json()
print(
    "before: seq=", before["sequence_index"],
    "rotation_index=", before.get("rotation", {}).get("index"),
    "positions=", len(before["positions"]), "orders=", len(before["orders"]),
    flush=True,
)

for round_index in (1, 2):
    response = requests.post(
        f"{base}/paper/accounts/{account_id}/live-tick",
        json={"symbol": symbol}, timeout=180,
    )
    print(f"live-tick #{round_index} HTTP {response.status_code}", flush=True)
    if response.status_code != 200:
        print(response.text[:500])
        sys.exit(1)
    state = response.json()
    rotation = state.get("rotation") or {}
    print(
        "  seq=", state["sequence_index"],
        "armed=", state["armed_state"],
        "cash=", state["cash"],
        "positions=", json.dumps(state["positions"], ensure_ascii=False)[:220],
        flush=True,
    )
    print(
        "  rotation: index=", rotation.get("index"),
        "current=", rotation.get("current_symbol"),
        "waiting=", rotation.get("waiting"),
        "pending=", rotation.get("pending_advance"),
        "note=", rotation.get("note"),
        flush=True,
    )
    print("  orders tail:", json.dumps(state["orders"][-3:], ensure_ascii=False)[:400], flush=True)
    print("  trades tail:", json.dumps(state["trades"][-2:], ensure_ascii=False)[:300], flush=True)

after = requests.get(f"{base}/paper/accounts/{account_id}", timeout=20).json()
stored = requests.get(f"{base}/paper/accounts/{account_id}", timeout=20).json()
print("after: seq=", after["sequence_index"], "equity=", after["equity"],
      "market_value=", after["market_value"], flush=True)
