import json
import sys
import time

import requests

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020/api"
boards = sys.argv[2].split(",") if len(sys.argv) > 2 else ["科创板"]
payload = {
    "include_realtime": False,
    "config": {
        "boards": boards,
        "enable_fundamental_veto": False,
    },
}
started = time.time()
response = requests.post(f"{base}/screener/run", json=payload, timeout=600)
print(f"HTTP {response.status_code} in {time.time() - started:.1f}s")
run = response.json()
if response.status_code != 200:
    print(json.dumps(run, ensure_ascii=False, indent=2))
    sys.exit(1)
print("gate:", json.dumps(run["gate"], ensure_ascii=False))
print("funnel:", json.dumps(run["funnel"], ensure_ascii=False))
print("warnings:", json.dumps(run["warnings"], ensure_ascii=False))
print("data_info:", json.dumps(run["data_info"], ensure_ascii=False))
first = run.get("first")
if first:
    print(
        f"first: {first['code']} {first['name']} {first['board']} chg20={first['chg20d']:+.2%} "
        f"dd52={first['drawdown_52w']:+.2%} day={first['day_change']:+.2%} "
        f"amount={first['amount'] / 1e8:.2f}亿 avg20={first['avg_amount_20d'] / 1e8:.2f}亿"
    )
print(f"pool={len(run.get('pool') or [])} eligible={len(run.get('eligible') or [])}")
for item in (run.get("pool") or [])[:20]:
    print(
        f"  {item['code']} {item['name']:<8} chg20={item['chg20d']:+.2%} "
        f"dd52={item['drawdown_52w']:+.1%} day={item['day_change']:+.2%} "
        f"amt={item['amount'] / 1e8:.2f}亿 avg20={item['avg_amount_20d'] / 1e8:.2f}亿 "
        f"veto={item.get('vetoed_by')}"
    )
run_id = run["id"]
requests.get(f"{base}/screener/runs/{run_id}", timeout=30).raise_for_status()
print(f"saved run id={run_id}")
