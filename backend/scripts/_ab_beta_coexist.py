"""A/B：beta 腿「互斥」vs「与个股并存」的空窗价值（复用回测 0b0cb477 的原始配置）。

本地只读分析：不改产品逻辑、不落库、不部署。

- arm=mutual：etf_beta_coexist=False（默认，互斥：个股在仓/选中时不建 beta，选中个股就清 beta）
- arm=coexist：etf_beta_coexist=True（并存：beta 只由指数均线/硬止损决定，额度按可用现金封顶）

配置来自服务器回测 0b0cb477-7f1d-4738-8c46-ec1f20f80592（部署时先落到
.reports/ab_beta_0b0cb477_config.json，避免手工抄错）。

用法（backend/ 目录）：python scripts/_ab_beta_coexist.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest import run_funnel_backtest  # noqa: E402
from app.domain import StrategyConfig  # noqa: E402
from app.market import MarketDataService  # noqa: E402
from app.quan_data import QuanStore, quan_data_dir  # noqa: E402
from app.screener import ScreenerConfig  # noqa: E402

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parent
# 路径可用环境变量覆盖（服务器容器内 reports/ 不存在，走 AB_* 指定）。
DATA_DIR = Path(os.environ.get("AB_DATA_DIR", HERE / "data"))
CONFIG_PATH = Path(os.environ.get("AB_CONFIG", ROOT / "reports" / "ab_beta_0b0cb477_config.json"))
REPORTS = Path(os.environ.get("AB_OUT", ROOT / "reports"))

# 服务器旧报告里的字段：t_target_spread 已废弃（保留字段兼容），新字段 etf_beta_coexist 需显式补。
LEGACY_KEYS = {"t_target_spread"}
ARMS = ("mutual", "coexist")


def load_base_config() -> dict:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg = {k: v for k, v in raw["config"].items() if k not in LEGACY_KEYS}
    cfg["etf_beta_coexist"] = False
    return raw, cfg


def summarize(report: dict) -> dict:
    m = report.get("metrics", {}) or {}
    eq = report.get("equity_curve") or []
    di = report.get("data_info", {}) or {}
    beta = di.get("beta") or {}
    stock_days = sum(1 for e in eq if (e.get("position_qty") or 0) > 0)
    beta_days = sum(1 for e in eq if (e.get("beta_qty") or 0) > 0)
    both_days = sum(1 for e in eq
                     if (e.get("position_qty") or 0) > 0 and (e.get("beta_qty") or 0) > 0)
    idle_days = sum(1 for e in eq
                    if (e.get("position_qty") or 0) == 0 and (e.get("beta_qty") or 0) == 0)
    # 月度净值：抽 2025-06 ~ 2025-10 与 beta 持有天数对照
    monthly: dict[str, dict] = {}
    for row in m.get("monthly") or []:
        month = str(row.get("month", ""))
        if month.startswith(("2025-0", "2025-1")):
            monthly[month] = {"equity": row.get("end_equity"),
                              "pnl": row.get("pnl"),
                              "return_rate": row.get("return_rate")}
    return {
        "final_equity": m.get("final_equity"),
        "net_profit": m.get("net_profit"),
        "total_return": m.get("total_return"),
        "annualized_return": m.get("annualized_return"),
        "max_drawdown": m.get("max_drawdown"),
        "sharpe_ratio": m.get("sharpe_ratio"),
        "calmar_ratio": m.get("calmar_ratio"),
        "total_fees": m.get("total_fees"),
        "total_rounds": m.get("total_rounds"),
        "total_trades": m.get("total_trades"),
        "t_trade_count": m.get("t_trade_count"),
        "beta_trade_count": beta.get("trade_count"),
        "beta_buy": beta.get("buy_count"),
        "beta_pnl": beta.get("pnl"),
        "beta_entry": beta.get("entry_date"),
        "beta_last_exit": beta.get("last_exit_date"),
        "beta_holding_days": beta.get("holding_days"),
        "equity_days": len(eq),
        "stock_days": stock_days,
        "beta_days": beta_days,
        "both_days": both_days,
        "idle_days": idle_days,
        "event_types": dict(Counter(str(e.get("type")) for e in report.get("events") or [])),
        "monthly_2025": monthly,
        "warnings": report.get("warnings") or [],
    }


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        raw, base_cfg = load_base_config()
    else:
        print(f"缺少配置快照 {CONFIG_PATH}，先部署 ab_beta_0b0cb477_config.json")
        sys.exit(2)
    screener = ScreenerConfig.from_dict(raw["screener"])
    start, end = raw["start"], raw["end"]
    cash = float(raw["initial_cash"])

    quan_dir = quan_data_dir()
    print(f"数据目录 {quan_dir} | {start} → {end} | 本金 {cash:,.0f}", flush=True)

    out: dict = {"window": {"start": start, "end": end, "initial_cash": cash},
                 "base_config": base_cfg, "arms": {}}
    for arm in ARMS:
        patch = {"etf_beta_coexist": arm == "coexist"}
        cfg = StrategyConfig.from_dict({**base_cfg, **patch})
        print(f"\n=== arm={arm} (etf_beta_coexist={patch['etf_beta_coexist']}) ===",
              flush=True)
        t0 = time.time()
        quan = QuanStore(quan_dir, cache_db=DATA_DIR / "quan_cache.db")
        market = MarketDataService(DATA_DIR / "cache", quan=quan)
        report = run_funnel_backtest(
            store=quan, market=market, screener_config=screener,
            initial_cash=cash, config=cfg,
            start_date=start, end_date=end, name=f"ab-{arm}", quan=quan,
        )
        elapsed = time.time() - t0
        row = summarize(report)
        row["seconds"] = round(elapsed, 1)
        out["arms"][arm] = row
        (REPORTS / f"ab_beta_{arm}_full.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   用时 {elapsed:.0f}s 年化 {row['annualized_return']} "
              f"回撤 {row['max_drawdown']} Sharpe {row['sharpe_ratio']} "
              f"beta笔数 {row['beta_trade_count']} betaPnl {row['beta_pnl']} "
              f"beta天数 {row['beta_days']}/{row['equity_days']} "
              f"双持 {row['both_days']} 空仓 {row['idle_days']}", flush=True)
        out["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        (REPORTS / "ab_beta_coexist_results.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    a, b = out["arms"].get("mutual"), out["arms"].get("coexist")
    if a and b:
        print("\n=== 对比（coexist - mutual）===")
        print(f"  净收益: {a['net_profit']:.2f} → {b['net_profit']:.2f} "
              f"({b['net_profit'] - a['net_profit']:+.2f})")
        print(f"  年化: {a['annualized_return']} → {b['annualized_return']}")
        print(f"  最大回撤: {a['max_drawdown']} → {b['max_drawdown']}")
        print(f"  Sharpe: {a['sharpe_ratio']} → {b['sharpe_ratio']}")
        print(f"  beta 笔数: {a['beta_trade_count']} → {b['beta_trade_count']}")
        print(f"  beta 持有天数: {a['beta_days']} → {b['beta_days']} "
              f"（个股+ETF 双持 {b['both_days']} 天）")
        print(f"  beta PnL: {a['beta_pnl']} → {b['beta_pnl']}")
    print("\n结果:", REPORTS / "ab_beta_coexist_results.json")


if __name__ == "__main__":
    main()