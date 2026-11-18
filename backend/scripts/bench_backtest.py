"""回测热点性能基准（before/after 对比）。

用途：量化 perf/backtest-hotspots 分支对 4 个已确认热点的优化效果。
可在任意机器运行（不依赖 6 年 minute5 全量数据）；对每个优化点构造
足够大的合成数据， measuring per-operation 吞吐，而不是端到端墙钟——
这样即使没有生产数据集也能稳定复现算法级收益。

对比方式（见 README/AGENTS）：
    # 1) 在优化分支上跑，存下结果
    python backend/scripts/bench_backtest.py --label after --json after.json
    # 2) 用 git worktree 检出优化前的 origin/main，在那份代码上再跑
    git worktree add ../layer-lab-before origin/main
    cd ../layer-lab-before && python backend/scripts/bench_backtest.py --label before --json before.json
    # 3) 对比
    python backend/scripts/bench_backtest.py --compare before.json after.json

每项基准都输出「每秒操作数」（ops/s，越高越快）。--compare 打印倍数加速比。
端到端真实回测计时请用 --e2e（仅在具备 quan parquet 全量数据的机器上有意义，
例如 ECS 服务器），它会用同一份配置跑一次 FunnelRotationBacktest 并报墙钟秒数。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 单测同款环境约定：不接真实数据目录、不起调度器、不联网探测交易日。
import os  # noqa: E402
os.environ.setdefault("WB_QUAN_DATA_DIR", "")
os.environ.setdefault("WB_SCHEDULER", "0")
os.environ.setdefault("WB_TRADING_PROBE", "0")

from app.domain import Bar  # noqa: E402
from app.strategy import LayeredTStrategy  # noqa: E402
from app.domain import StrategyConfig  # noqa: E402

# _ChainedBars 仅存在于优化分支；在 origin/main 基线上跑「before」时它不存在，
# 此时跳过视图基准（该基准本身就是在对比有无视图的差距）。
try:
    from app.strategy import _ChainedBars  # noqa: E402
    _HAS_CHAINED = True
except ImportError:
    _HAS_CHAINED = False

START = datetime(2019, 1, 1)


def _dbar(day: int, close: float, symbol: str = "TEST") -> Bar:
    return Bar(
        timestamp=START + timedelta(days=day), symbol=symbol,
        open=close, high=close * 1.02, low=close * 0.98, close=close,
        volume=1_000.0, amount=close * 1_000.0, timeframe="daily",
        turnover_rate=0.05, prev_close=close,
    )


def _time(label: str, fn, *, iterations: int) -> float:
    """跑 iterations 次 fn，返回 ops/s（越高越快）。"""
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter() - start
    ops = iterations / elapsed if elapsed > 0 else float("inf")
    print(f"  {label:<44} {ops:>14,.0f} ops/s")
    return ops


# ---------------------------------------------------------------- 热点 #5
def bench_bar_date(n_days: int = 1500, reads: int = 200) -> float:
    """Bar.date 访问：优化前每根 K 线都 timestamp.date().isoformat() 重新算。"""
    bars = [_dbar(d, 10.0 + d * 0.01) for d in range(n_days)]
    counter = 0

    def run() -> None:
        nonlocal counter
        acc = 0
        for _ in range(reads):
            for bar in bars:
                acc += len(bar.date)  # 强制读取 date 字符串
        counter = acc

    print(f"[#5 Bar.date] {n_days} bars x {reads} reads")
    return _time("Bar.date 读取", run, iterations=3)


# ---------------------------------------------------------------- 热点 #2
def bench_holding_days(n_days: int = 1500, calls: int = 200) -> float:
    """_holding_days：优化前对全量日线 sum() 线性扫描，每根 5 分钟 bar 一次。"""
    history = [_dbar(d, 10.0 + d * 0.01) for d in range(n_days)]
    entry = (START + timedelta(days=n_days // 2)).date().isoformat()

    def make() -> LayeredTStrategy:
        s = LayeredTStrategy(StrategyConfig())
        s.round_entry_date = entry
        return s

    strategy = make()

    def run() -> None:
        for _ in range(calls):
            strategy._holding_days(history)

    print(f"[#2 _holding_days] {n_days}-bar history x {calls} calls")
    return _time("_holding_days (bisect)", run, iterations=5)


def bench_chained_bars(n_days: int = 1500, tail: int = 20, reads: int = 200) -> float:
    """_ChainedBars 无复制视图 vs list(history)+[today]：构造 + 有界尾部读取。

    优化前每根 5 分钟 bar 都 list(history) 完整复制 ~1500 个引用；优化后 O(1) 视图。
    这里测「构造视图/复制 + 读尾部窗口」的整段成本。
    """
    history = [_dbar(d, 10.0 + d * 0.01) for d in range(n_days)]
    today = _dbar(n_days, 40.0)

    def slow_build() -> list:
        combined = list(history) + [today]  # 旧：完整复制
        return combined[-tail:]

    def fast_build() -> list:
        view = _ChainedBars(history, (today,))  # 新：O(1) 视图
        return view[-tail:]

    print(f"[#2 sleeve 复制/视图] {n_days}-bar history, 读尾部 {tail}")
    slow_ops = _time("list()+尾部 (优化前形态)", slow_build, iterations=max(1, reads // 10))
    fast_ops = _time("_ChainedBars+尾部 (优化后)", fast_build, iterations=reads)
    return fast_ops / max(slow_ops, 1e-9)


# ---------------------------------------------------------------- 汇总
def run_all() -> dict:
    results: dict[str, float] = {}
    results["bar_date_ops"] = bench_bar_date()
    results["holding_days_ops"] = bench_holding_days()
    if _HAS_CHAINED:
        ratio = bench_chained_bars()
        results["chained_bars_speedup_x"] = round(ratio, 2)
    else:
        print("  (_ChainedBars 不存在于本代码版本，跳过视图基准 —— 这是优化前基线)")
    return results


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compare(before_path: str, after_path: str) -> None:
    before, after = _load(before_path), _load(after_path)
    print("\n=== before/after 对比（ops/s，越高越快）===")
    print(f"{'benchmark':<28}{'before':>16}{'after':>16}{'speedup':>10}")
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        if isinstance(b, (int, float)) and isinstance(a, (int, float)) and b:
            print(f"{key:<28}{b:>16,.0f}{a:>16,.0f}{a / b:>9.2f}x")
        else:
            print(f"{key:<28}{str(b):>16}{str(a):>16}{'-':>10}")


def run_e2e() -> None:
    """端到端真实回测计时（需 quan parquet 全量数据，如 ECS）。"""
    from app.backtest import FunnelRotationBacktest
    from app.screener import ScreenerConfig
    from app.universe import BOARD_GEM

    options = {
        "screener_config": ScreenerConfig(boards=[BOARD_GEM], enable_fundamental_veto=False),
        "initial_cash": 4_000_000.0, "config": StrategyConfig(),
        "start_date": "2020-01-01", "end_date": "2025-12-31",
    }
    print("[e2e] 跑一次真实区间回测（同配置计时）…")
    start = time.perf_counter()
    report = FunnelRotationBacktest(store=None, market=None, name="bench", **options).run()
    elapsed = time.perf_counter() - start
    print(f"  status={report.get('status')} trades={len(report.get('trades', []))} "
          f"wall={elapsed:.1f}s ({elapsed / 60:.1f} min)")


def main() -> None:
    parser = argparse.ArgumentParser(description="回测热点性能基准")
    parser.add_argument("--label", default="current", help="结果标签（如 before/after）")
    parser.add_argument("--json", dest="json_out", default=None, help="把结果写入 JSON 文件")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                        help="对比两份 JSON 结果并打印加速比")
    parser.add_argument("--e2e", action="store_true", help="跑端到端真实回测计时（需全量数据）")
    args = parser.parse_args()

    if args.compare:
        compare(args.compare[0], args.compare[1])
        return

    print(f"=== 回测热点基准 [{args.label}] ===")
    results = run_all()
    if args.e2e:
        run_e2e()
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"label": args.label, **results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n结果已写入 {args.json_out}")


if __name__ == "__main__":
    main()
