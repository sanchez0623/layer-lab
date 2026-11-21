"""顶层 ``metrics["underwater"]`` 改用现金流调整（TWR）口径的回归护栏。

背景：提现在原始 equity 曲线上是负台阶（提现日 equity 直接下跳），用原始曲线算水下时长
会把提现日误判成「水下一天」。P0 已加 ``cashflow_adjusted_series`` / ``cashflow_adjusted_metrics``，
本分支把顶层 ``performance_metrics`` 的 ``underwater`` 也切到调整后曲线，与
``cashflow_adjusted`` 块口径一致。

覆盖：
- 提现台阶日的大跌其实来自提现 → 顶层 underwater 不再报「当前水下」（原始口径会误报）；
- 无提现曲线 → 顶层 underwater 与直接用原始曲线算的 ``underwater_stats`` 逐字段一致（护栏）；
- 有提现场景下 ``longest_underwater_days`` ≤ 原始口径（提现台阶只会缩短、不会拉长水下区间）。
"""

from __future__ import annotations

import pytest

from app.backtest import cashflow_adjusted_curve, cashflow_adjusted_series, performance_metrics, underwater_stats


def _point(date: str, equity: float) -> dict:
    return {
        "date": date, "equity": equity, "cash": 0.0, "market_value": 0.0,
        "invested": 0.0, "drawdown": 0.0, "position_qty": 0,
    }


def _curve(equities: list[float]) -> list[dict]:
    return [
        _point(f"2025-01-{day:02d}", equity)
        for day, equity in enumerate(equities, start=1)
    ]


def _metrics(curve: list[dict], withdrawals: list[dict] | None = None) -> dict:
    return performance_metrics(
        curve, initial_cash=1_000_000.0, rounds=[], trades=[],
        total_fees=0.0, t_net_profit=0.0, max_invested=0.0,
        max_float_loss=0.0, buy_hold_return=0.0,
        withdrawals=withdrawals,
    )


def test_withdrawal_step_no_longer_counts_as_underwater():
    """提现造成的台阶日不该被记成水下：顶层口径下当前不在水下、天数为 0。

    原始曲线 [1M, 1.08M, 0.97M, 1.02M]，第二天是峰值、第三天 -8 万其实是提现台阶
    （0.97M 里含提现挖出的坑）+ 当天真实亏 3 万：
    - 原始口径：末段 1.02M < 峰值 1.08M →「当前水下 True / 持续 2 天」（把提现坑也算进去）；
    - 调整后曲线 [1M, 1.08M, 1.05M, 1.10M]：第三天只剩真实亏 3 万、末日已创新高
      →「当前水下 False / 0 天」，顶层不再被提现台阶污染。
    """
    curve = _curve([1_000_000.0, 1_080_000.0, 970_000.0, 1_020_000.0])
    withdrawals = [{"date": "2025-01-03", "amount": 80_000.0, "period_profit": 50_000.0}]
    metrics = _metrics(curve, withdrawals)

    assert metrics["underwater"]["currently_underwater"] is False
    assert metrics["underwater"]["current_drawdown_days"] == 0
    # 与 cashflow_adjusted 块口径一致（同一个水下统计对象）。
    assert metrics["underwater"] == metrics["cashflow_adjusted"]["underwater"]

    # 反证：原始口径确实会把提现坑一起算进当前水下（1.02M < 峰值 1.08M，连续 2 天）。
    raw = underwater_stats(curve)
    assert raw["currently_underwater"] is True
    assert raw["current_drawdown_days"] == 2


def test_without_withdrawals_matches_raw_underwater_exactly():
    """无提现：调整口径退化为原始曲线，顶层 underwater 与原始统计逐字段一致。"""
    curve = _curve([1_000_000.0, 1_020_000.0, 990_000.0, 1_010_000.0, 1_050_000.0])
    metrics = _metrics(curve, withdrawals=None)

    assert metrics["underwater"] == underwater_stats(curve)


def test_longest_underwater_days_never_exceeds_raw_with_withdrawals():
    """有提现时最长水下天数只可能缩短或持平（加回提现只会抬高权益、提前收复峰值）。"""
    # 一段真实回撤 + 中途一笔提现台阶。
    curve = _curve([
        1_000_000.0, 1_050_000.0, 1_000_000.0, 950_000.0,
        900_000.0, 930_000.0, 980_000.0, 1_020_000.0, 1_060_000.0,
    ])
    withdrawals = [{"date": "2025-01-05", "amount": 60_000.0, "period_profit": 0.0}]
    metrics = _metrics(curve, withdrawals)

    raw_longest = underwater_stats(curve)["longest_underwater_days"]
    assert metrics["underwater"]["longest_underwater_days"] <= raw_longest
    # 提现加回确实抹平了台阶（调整后曲线 ≠ 原始曲线），护栏才非空转。
    adjusted, _ = cashflow_adjusted_series(curve, withdrawals)
    assert adjusted != [float(item["equity"]) for item in curve]
    assert cashflow_adjusted_curve(curve, adjusted)[-1]["date"] == "2025-01-09"


# --- 回撤深度分桶（drawdown_depth_distribution）-------------------------------

BUCKET_KEYS = ["lte_2pct", "2_to_5pct", "5_to_10pct", "gt_10pct"]


def _dist(curve: list[dict]) -> dict:
    return underwater_stats(curve)["drawdown_depth_distribution"]


def test_depth_distribution_empty_curve_returns_zeroed_skeleton():
    """空曲线：四个分桶都在、天数与比例全 0，不抛错。"""
    dist = underwater_stats([])["drawdown_depth_distribution"]
    assert list(dist.keys()) == BUCKET_KEYS
    for key in BUCKET_KEYS:
        assert dist[key]["days"] == 0
        assert dist[key]["pct_of_underwater"] == 0.0
        assert dist[key]["pct_of_trading"] == 0.0


def test_depth_distribution_buckets_and_ratios():
    """浅水下落 ≤2% 档、深水下落 >10% 档，days 与两个比例口径自洽。

    曲线：首日 1M 为峰值 → 全程再没回到 1M。各天回撤深度刻意避开 2%/5%/10% 的
    浮点边界（如 0.98M 的深度是 0.020000000000000018，会被归到 2–5% 档）：
      - 第2/3天 0.999M / 0.99M → 回撤 0.1% / 1% → ≤2% 档；
      - 第4天 0.96M → 回撤 4% → 2–5% 档；
      - 第5天 0.92M → 回撤 8% → 5–10% 档；
      - 第6天 0.85M → 回撤 15% → >10% 档。
    """
    curve = _curve([1_000_000.0, 999_000.0, 990_000.0, 960_000.0, 920_000.0, 850_000.0])
    dist = _dist(curve)

    assert [dist[k]["days"] for k in BUCKET_KEYS] == [2, 1, 1, 1]
    # 深度天数合计 = 水下天数（首日是峰值，不算水下）。
    assert sum(dist[k]["days"] for k in BUCKET_KEYS) == 5
    assert underwater_stats(curve)["underwater_days"] == 5
    # pct_of_trading 合计 ≈ 水下占比（分桶逐日除后求和，与一次性除有浮点末位差）。
    assert sum(dist[k]["pct_of_trading"] for k in BUCKET_KEYS) == \
        pytest.approx(underwater_stats(curve)["underwater_ratio"])
    # pct_of_underwater 合计 ≈ 1（所有水下日都被归到某一档）。
    assert sum(dist[k]["pct_of_underwater"] for k in BUCKET_KEYS) == pytest.approx(1.0)


def test_depth_distribution_boundary_inclusive_upper():
    """分桶上界是闭区间（depth <= upper）。深度正好 5% 归 2–5% 档、正好 10% 归 5–10% 档。

    用能被整除的值规避浮点误差：0.95M/1M=5%、0.90M/1M=10%。
    """
    curve = _curve([1_000_000.0, 950_000.0, 900_000.0])
    dist = _dist(curve)
    # 第2天深度 5% → 2_to_5pct；第3天深度 10% → 5_to_10pct。
    assert dist["2_to_5pct"]["days"] == 1
    assert dist["5_to_10pct"]["days"] == 1
    assert dist["gt_10pct"]["days"] == 0


def test_depth_distribution_all_time_high_day_not_counted():
    """全程创新高（无水下）：所有分桶天数 0，比例 0，不抛错。"""
    curve = _curve([1_000_000.0, 1_010_000.0, 1_020_000.0, 1_015_000.0])
    # 第4天 1.015M < 峰值 1.02M → 1 天浅水下。
    dist = _dist(curve)
    assert sum(dist[k]["days"] for k in BUCKET_KEYS) == 1
    assert dist["lte_2pct"]["days"] == 1  # 回撤 0.49% → ≤2% 档

    monotic = _curve([1_000_000.0, 1_010_000.0, 1_020_000.0])
    dist_mono = _dist(monotic)
    assert sum(dist_mono[k]["days"] for k in BUCKET_KEYS) == 0


def test_depth_distribution_present_in_full_metrics():
    """顶层 metrics['underwater'] 与 cashflow_adjusted['underwater'] 都带该字段且一致。"""
    curve = _curve([1_000_000.0, 1_050_000.0, 980_000.0, 1_010_000.0])
    metrics = _metrics(curve, withdrawals=None)
    assert "drawdown_depth_distribution" in metrics["underwater"]
    assert metrics["underwater"]["drawdown_depth_distribution"] == \
        metrics["cashflow_adjusted"]["underwater"]["drawdown_depth_distribution"]
