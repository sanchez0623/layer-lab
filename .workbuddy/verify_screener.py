"""LayerLab 选股漏斗端到端验收脚本（对运行中的 8010 服务发真实 HTTP）。

用法：python .workbuddy/verify_screener.py [--base http://127.0.0.1:8010/api] [--prepare]
不写任何文件（除 stdout），仅调用只读 + run/prepare 接口。prepare 会创建数据集/模拟账户，
属于用户已确认的功能验收副作用。
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import requests


def show(title: str, value) -> None:
    print(f"\n== {title} ==")
    print(json.dumps(value, ensure_ascii=False, indent=2)[:4000])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010/api")
    parser.add_argument("--prepare", action="store_true", help="同时验收一键带入（会写数据集/建账户）")
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args()
    base = args.base.rstrip("/")

    health = requests.get(f"{base}/health", timeout=10)
    print(f"health {health.status_code}: {health.text[:200]}")

    defaults = requests.get(f"{base}/screener/defaults", timeout=30).json()
    show("defaults", defaults)
    coverage = requests.get(f"{base}/screener/coverage", timeout=30).json()
    show("coverage", coverage)

    refresh = requests.get(f"{base}/screener/refresh", timeout=30).json()
    show("refresh status", refresh)

    started = time.time()
    response = requests.post(
        f"{base}/screener/run",
        json={"as_of": args.as_of, "include_realtime": False, "config": None},
        timeout=600,
    )
    print(f"\nrun HTTP {response.status_code} in {time.time() - started:.1f}s")
    run = response.json()
    if response.status_code != 200:
        show("run error", run)
        return 1
    show("gate", run.get("gate"))
    show("funnel", run.get("funnel"))
    show("warnings", run.get("warnings"))
    show("data_info", run.get("data_info"))
    first = run.get("first")
    show("first", first)
    pool = run.get("pool") or []
    print(f"pool size={len(pool)}")
    for item in pool[:8]:
        print(
            f"  {item['code']} {item['name']:<8} {item['board']:<4} "
            f"chg20={item['chg20d']:+.2%} dd52={item['drawdown_52w']:+.2%} "
            f"day={item['day_change']:+.2%} amount={item['amount'] / 1e8:.2f}亿 "
            f"avg20={item['avg_amount_20d'] / 1e8:.2f}亿 veto={item.get('vetoed_by')}"
        )

    runs = requests.get(f"{base}/screener/runs", timeout=30).json()
    show("runs summary", runs[:3])

    run_id = run["id"]
    detail = requests.get(f"{base}/screener/runs/{run_id}", timeout=30).json()
    print(f"\ndetail id={detail['id']} eligible={len(detail.get('eligible') or [])}")

    if args.prepare and first:
        prepared = requests.post(
            f"{base}/screener/runs/{run_id}/prepare",
            json={"target": "backtest", "candidate_code": first["code"]},
            timeout=600,
        )
        print(f"\nprepare(backtest) HTTP {prepared.status_code}")
        body = prepared.json()
        show("prepare backtest", {k: body.get(k) for k in ("target", "candidate", "datasets")})

        prepared_paper = requests.post(
            f"{base}/screener/runs/{run_id}/prepare",
            json={"target": "paper", "candidate_code": first["code"], "rotation": True},
            timeout=600,
        )
        print(f"\nprepare(paper) HTTP {prepared_paper.status_code}")
        paper_body = prepared_paper.json()
        account = paper_body.get("account") or {}
        print(f"account id={account.get('id')} dataset={account.get('dataset_id')} "
              f"auto={account.get('auto_strategy')} rotation={json.dumps(account.get('rotation'), ensure_ascii=False)}")
        if account.get("id"):
            rotation = requests.post(
                f"{base}/paper/accounts/{account['id']}/rotation",
                json={"enabled": True},
                timeout=30,
            )
            print(f"rotation toggle HTTP {rotation.status_code}: "
                  f"{json.dumps((rotation.json() or {}).get('rotation'), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
