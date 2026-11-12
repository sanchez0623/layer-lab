import json
import sys
import time

import requests

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020/api"
runs = requests.get(f"{base}/screener/runs?limit=5", timeout=20).json()
run = requests.get(f"{base}/screener/runs/{runs[0]['id']}", timeout=20).json()
print(f"run={run['id']} first={run['first']['code']} eligible={len(run['eligible'])}", flush=True)

started = time.time()
prepared = requests.post(
    f"{base}/screener/runs/{run['id']}/prepare",
    json={"target": "paper", "candidate_code": run["first"]["code"], "rotation": True},
    timeout=600,
)
print(f"prepare paper HTTP {prepared.status_code} in {time.time() - started:.1f}s", flush=True)
body = prepared.json()
if prepared.status_code != 200:
    print(json.dumps(body, ensure_ascii=False))
    sys.exit(1)
account = body["account"]
datasets = body["datasets"]
print("candidate:", json.dumps(body["candidate"], ensure_ascii=False))
print("datasets:", {key: (value["id"] if value else None) for key, value in datasets.items()})
print("account:", account["id"], "auto=", account["auto_strategy"], "dataset=", account["dataset_id"])
print("rotation:", json.dumps(account.get("rotation"), ensure_ascii=False))

toggled = requests.post(
    f"{base}/paper/accounts/{account['id']}/rotation", json={"enabled": False}, timeout=20,
).json()
print("rotation toggle off:", json.dumps(toggled.get("rotation"), ensure_ascii=False)[:300])
retoggled = requests.post(
    f"{base}/paper/accounts/{account['id']}/rotation", json={"enabled": True}, timeout=20,
).json()
print("rotation toggle on:", json.dumps(retoggled.get("rotation"), ensure_ascii=False)[:300])
