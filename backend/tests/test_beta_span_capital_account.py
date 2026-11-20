"""ETF 持仓区间（kind=BETA_SPAN）收益口径：资本账户法。

背景：``rotation_trail._beta_span_outcome`` 原来只把区间内卖出成交的
``realized_pnl`` 当区间收益，导致「只买未卖」区间显示 0.00%、「纯持有」显示
"—"、区间内加仓的本金被误当收益。现改为资本账户法（方案④）::

    pnl = 段末 beta_value + Σ区间内卖出所得 − 段初 beta_value − Σ区间内买入成本
    return_rate = pnl / (段初 beta_value + Σ区间内买入成本)

段初取 ``start_date`` 前一交易日收盘 beta_value（建仓段前一日为 0），买入成本
含 fee、卖出所得扣 fee。

真实数据基准：报告 ``6ceb50df-da91-414b-94ed-851b604b3280``（sz.159915）的四个
ETF 持仓区间，数据抽取自 ``.tmp/analysis/6ceb_full.json``。测试优先读取该本地
报告（存在时），否则用下面内嵌的同一份真实子集，保证无本地分析数据的 CI 也能
覆盖真实数值。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.rotation_trail import collapse_waiting_spans, enrich_trail

# --- 真实报告 6ceb 的 ETF 腿数据子集（2020-06-02 ~ 2020-07-28）--------------
# equity_curve 的 {date: beta_value}（市值，元）。
REAL_BETA_VALUES: dict[str, float] = {
    "2020-06-02": 0.0,
    "2020-06-03": 1029382.0,
    "2020-06-04": 2008115.1,
    "2020-06-05": 2021676.9,
    "2020-06-08": 2008115.1,
    "2020-06-09": 2036207.4,
    "2020-06-10": 2055581.4,
    "2020-06-11": 2052675.3,
    "2020-06-12": 2073986.7,
    "2020-06-15": 2073986.7,
    "2020-06-16": 2114672.1,
    "2020-06-17": 2119515.6,
    "2020-06-18": 2125327.8,
    "2020-06-19": 2167950.6,
    "2020-06-22": 2192168.1,
    "2020-06-23": 2234790.9,
    "2020-06-24": 2234790.9,
    "2020-06-29": 2223166.5,
    "2020-06-30": 2285163.3,
    "2020-07-01": 2268695.4,
    "2020-07-02": 2276445.0,
    "2020-07-03": 2305506.0,
    "2020-07-06": 2373315.0,
    "2020-07-07": 2424656.1,
    "2020-07-08": 2493433.8,
    "2020-07-09": 2590303.8,
    "2020-07-10": 2600959.5,
    "2020-07-13": 2714297.4,
    "2020-07-14": 2680392.9,
    "2020-07-15": 2636801.4,
    "2020-07-16": 2477934.6,
    "2020-07-17": 2502152.1,
    "2020-07-20": 2529275.7,
    "2020-07-21": 2560274.1,
    "2020-07-22": 2594178.6,
    "2020-07-23": 2621302.2,
    "2020-07-24": 2457591.9,
    "2020-07-27": 308864.0,
    "2020-07-28": 0.0,
}

# 报告 data_info.beta.fills（区间内三笔 + 次日清仓一笔）。
REAL_BETA_FILLS: list[dict] = [
    {"date": "2020-06-03", "side": "BUY", "quantity": 499_700, "price": 2.0752,
     "fee": 88.56, "realized_pnl": 0.0},
    {"date": "2020-06-04", "side": "BUY", "quantity": 469_000, "price": 2.0672,
     "fee": 82.8, "realized_pnl": 0.0},
    {"date": "2020-07-27", "side": "SELL", "quantity": 847_100, "price": 2.5437,
     "fee": 184.02, "realized_pnl": 399_813.5},
    {"date": "2020-07-28", "side": "SELL", "quantity": 121_600, "price": 2.5757,
     "fee": 26.74, "realized_pnl": 61_283.53},
]

#: 面板上的四个 ETF 持仓区间（连续持仓被 L0 门签名切开）。
REAL_SPANS = [
    ("2020-06-03", "2020-06-11"),
    ("2020-06-12", "2020-06-17"),
    ("2020-06-18", "2020-07-23"),
    ("2020-07-24", "2020-07-27"),
]

#: 含 fee 资本账户口径的期望区间 pnl（lead 已确认）。
EXPECTED_SPAN_PNL = [46_009.70, 66_840.30, 501_786.60, -157_853.95]
EXPECTED_TOTAL_PNL = 456_782.65


def _local_real_beta() -> tuple[dict[str, float], list[dict]]:
    """优先读取本地 6ceb 报告；没有分析数据时回退内嵌的真实子集。"""
    for root in Path(__file__).resolve().parents:
        candidate = root / ".tmp" / "analysis" / "6ceb_full.json"
        if not candidate.is_file():
            continue
        report = json.loads(candidate.read_text(encoding="utf-8"))
        values = {
            str(point.get("date"))[:10]: float(point.get("beta_value") or 0.0)
            for point in (report.get("equity_curve") or [])
            if point.get("beta_value") is not None
        }
        fills = ((report.get("data_info") or {}).get("beta") or {}).get("fills") or []
        if values and fills:
            return values, fills
    return dict(REAL_BETA_VALUES), [dict(item) for item in REAL_BETA_FILLS]


def _beta_item(day: str, *, gate_open: bool = True, symbol: str = "sz.159915") -> dict:
    return {
        "status": "BETA", "round": None, "symbol": None, "name": "",
        "select_date": day, "entry_date": None, "exit_date": None, "pnl": None,
        "gate_open": gate_open, "beta_symbol": symbol, "funnel": {},
        "gate": None, "screen_warnings": [],
    }


def _trail_for_spans(
    spans: list[tuple[str, str]], values: dict[str, float],
) -> list[dict]:
    """按给定区间造 BETA 逐日项；相邻区间换门状态，强制 collapse 切段。"""
    days = sorted(values)
    trail: list[dict] = []
    for index, (start, end) in enumerate(spans):
        gate_open = index % 2 == 0
        trail.extend(_beta_item(day, gate_open=gate_open) for day in days if start <= day <= end)
    return trail


def _collapsed_spans(
    spans: list[tuple[str, str]], values: dict[str, float], fills: list[dict],
) -> list[dict]:
    trail = _trail_for_spans(spans, values)
    collapsed = collapse_waiting_spans(
        enrich_trail(trail, [], sorted(values)),
        beta={"fills": fills},
        beta_values=values,
    )
    return [item for item in collapsed if item.get("kind") == "BETA_SPAN"]


def test_real_report_four_spans_use_capital_account():
    """真实报告四区间的 pnl / 收益率 / 投入分母按资本账户法（含 fee）计算。"""
    values, fills = _local_real_beta()
    spans = _collapsed_spans(REAL_SPANS, values, fills)

    assert [(span["start_date"], span["end_date"]) for span in spans] == REAL_SPANS
    pnls = [span["pnl"] for span in spans]
    for actual, expected in zip(pnls, EXPECTED_SPAN_PNL):
        assert actual == pytest.approx(expected, abs=1)

    # 第二、三段纯持有无成交：pnl 不再是 0 / None，而是市值变化。
    assert spans[1]["beta_buy_count"] == 0 and spans[1]["beta_sell_count"] == 0
    assert spans[2]["pnl"] > 0

    # 07/24~07/27 是 -6% 硬止损段，必须为负（不要「修」成正数）。
    assert spans[3]["pnl"] < 0
    assert spans[3]["return_rate"] == pytest.approx(-0.06021967, abs=1e-6)

    # 资金效率分母 = 段初市值 + 区间买入成本（含 fee）。
    assert spans[0]["beta_invested"] == pytest.approx(2_006_665.60, abs=0.01)
    assert spans[0]["return_rate"] == pytest.approx(46_009.70 / 2_006_665.60, abs=1e-9)
    assert spans[1]["beta_invested"] == pytest.approx(2_052_675.30, abs=0.01)
    assert spans[2]["beta_invested"] == pytest.approx(2_119_515.60, abs=0.01)
    assert spans[3]["beta_invested"] == pytest.approx(2_621_302.20, abs=0.01)

    assert sum(pnls) == pytest.approx(EXPECTED_TOTAL_PNL, abs=5)


def test_real_report_spans_reconcile_with_realized_plus_remaining_unrealized():
    """对账：四区间 pnl 合计 = 07/27 已实现 + 期末剩余股浮盈（成本含 fee）。

    独立重算：剩余 121,600 股 × (期末市值 − 含费均价)，加上区间内卖出已实现
    pnl，应与四个 BETA_SPAN 的 pnl 合计闭合（防止区间边界漏算 / 重复算）。
    """
    values, fills = _local_real_beta()
    spans = _collapsed_spans(REAL_SPANS, values, fills)

    in_span_fills = [
        item for item in fills
        if any(start <= str(item.get("date"))[:10] <= end for start, end in REAL_SPANS)
    ]
    buys = [item for item in in_span_fills if item.get("side") == "BUY"]
    sells = [item for item in in_span_fills if item.get("side") == "SELL"]
    buy_qty = sum(int(item["quantity"]) for item in buys)
    buy_cost = sum(
        float(item["price"]) * int(item["quantity"]) + float(item.get("fee") or 0.0)
        for item in buys
    )
    sell_qty = sum(int(item["quantity"]) for item in sells)
    realized = sum(float(item.get("realized_pnl") or 0.0) for item in sells)
    remaining_qty = buy_qty - sell_qty
    remaining_unrealized = values["2020-07-27"] - (buy_cost / buy_qty) * remaining_qty

    assert realized == pytest.approx(399_813.50, abs=0.01)
    assert remaining_qty == 121_600
    assert remaining_unrealized == pytest.approx(56_969.15, abs=0.01)

    reconciliation = realized + remaining_unrealized
    assert reconciliation == pytest.approx(EXPECTED_TOTAL_PNL, abs=5)
    assert sum(span["pnl"] for span in spans) == pytest.approx(reconciliation, abs=5)


def test_real_report_spans_close_to_realized_pnl_when_fully_liquidated():
    """期末清仓（07/28 卖出剩余股）时，全部区间 pnl 合计 = 整段已实现 pnl。

    07/27 收盘还剩 121,600 股，07/28 清仓。把 07/28 也作为一段，五段 pnl
    之和应等于两笔卖出的已实现盈亏之和（461,097.03），期末持仓为 0。
    """
    values, fills = _local_real_beta()
    spans = _collapsed_spans(REAL_SPANS + [("2020-07-28", "2020-07-28")], values, fills)

    assert len(spans) == len(REAL_SPANS) + 1
    assert sum(span["pnl"] for span in spans) == pytest.approx(399_813.50 + 61_283.53, abs=5)
    # 07/28 段的段初市值 = 07/27 收盘剩余股市值，卖出所得加回后归零。
    assert spans[-1]["pnl"] == pytest.approx(
        121_600 * 2.5757 - 26.74 - 308_864.0, abs=0.01,
    )


def test_pure_hold_span_reports_market_value_change():
    """合成：区间内无成交、纯持有 → pnl = 末值 − 初值，不再显示 0 / "—"。"""
    values = {"2025-01-02": 1_000.0, "2025-01-03": 1_100.0, "2025-01-06": 1_200.0}
    trail = [_beta_item("2025-01-03"), _beta_item("2025-01-06")]
    collapsed = collapse_waiting_spans(
        enrich_trail(trail, [], sorted(values)),
        beta={"fills": [], "pnl": None},
        beta_values=values,
    )
    spans = [item for item in collapsed if item.get("kind") == "BETA_SPAN"]
    assert len(spans) == 1
    span = spans[0]
    assert span["pnl"] == pytest.approx(200.0)
    assert span["beta_invested"] == pytest.approx(1_000.0)
    assert span["return_rate"] == pytest.approx(0.2)
    assert span["beta_buy_count"] == 0 and span["beta_sell_count"] == 0


def test_added_buy_principal_is_not_counted_as_profit():
    """合成：区间内追加买入，本金不能被算成收益（06/03~06/11 真实场景）。

    端点市值差是 +2,052,675（含 06/04 加仓现金注入），资本账户法只有 +46,009.70。
    """
    values = {
        day: REAL_BETA_VALUES[day]
        for day in ("2020-06-02", "2020-06-03", "2020-06-04", "2020-06-05",
                    "2020-06-08", "2020-06-09", "2020-06-10", "2020-06-11")
    }
    fills = [item for item in REAL_BETA_FILLS if item["date"] <= "2020-06-04"]
    spans = _collapsed_spans([("2020-06-03", "2020-06-11")], values, fills)

    assert len(spans) == 1
    span = spans[0]
    assert span["pnl"] == pytest.approx(46_009.70, abs=1)
    # 端点市值差会把 06/04 的 96.9 万加仓现金算成收益，资本账户法不会。
    assert span["pnl"] < 100_000
    assert span["beta_invested"] == pytest.approx(2_006_665.60, abs=0.01)
    assert span["beta_buy_count"] == 2 and span["beta_sell_count"] == 0


def test_without_beta_values_keeps_legacy_realized_only_behaviour():
    """回归：不传 beta_values（None / 空 dict）时行为与旧版一致。"""
    trail = [_beta_item("2025-01-03"), _beta_item("2025-01-06")]
    beta = {
        "fills": [
            {"date": "2025-01-03", "side": "BUY", "quantity": 100_000,
             "price": 1.0, "fee": 50.0, "realized_pnl": 0.0},
            {"date": "2025-01-06", "side": "BUY", "quantity": 100_000,
             "price": 1.1, "fee": 55.0, "realized_pnl": 0.0},
            {"date": "2025-01-06", "side": "SELL", "quantity": 200_000,
             "price": 1.2, "fee": 120.0, "realized_pnl": 6_000.0},
        ],
        "pnl": 6_000.0,
    }
    enriched = enrich_trail(trail, [], ["2025-01-02", "2025-01-03", "2025-01-06"])

    for beta_values in (None, {}):
        span = [
            item for item in collapse_waiting_spans(
                enriched, beta=beta, beta_values=beta_values,
            ) if item.get("kind") == "BETA_SPAN"
        ][0]
        # 旧口径：pnl = 区间内卖出 realized_pnl 之和；投入 = 买入成交额（不含 fee）。
        assert span["pnl"] == pytest.approx(6_000.0)
        assert span["beta_invested"] == pytest.approx(210_000.0)
        assert span["return_rate"] == pytest.approx(6_000.0 / 210_000.0)
        assert span["beta_buy_count"] == 2 and span["beta_sell_count"] == 1


# -------------------------------------------------- 端点透传 -------------------

def _report_with_beta_values() -> dict:
    trail = [_beta_item("2025-01-03"), _beta_item("2025-01-06")]
    return {
        "id": "bt-beta-capital", "name": "ETF 资本账户测试", "status": "COMPLETED",
        "created_at": "2026-11-20T00:00:00", "config": {}, "metrics": {},
        "equity_curve": [
            {"date": "2025-01-02", "equity": 4_000_000.0, "beta_value": 0.0},
            {"date": "2025-01-03", "equity": 4_000_000.0, "beta_value": 100_000.0},
            {"date": "2025-01-06", "equity": 4_000_000.0, "beta_value": 110_000.0},
        ],
        "trades": [], "orders": [], "rounds": [], "events": [], "warnings": [],
        "data_info": {
            "mode": "funnel_rotation", "screening_count": len(trail),
            "rotation_trail": trail,
            "beta": {
                "enabled": True, "symbol": "sz.159915", "pnl": 9_990.0,
                "fills": [
                    {"date": "2025-01-03", "side": "BUY", "quantity": 100_000,
                     "price": 1.0, "fee": 10.0, "realized_pnl": 0.0},
                ],
            },
        },
    }


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "app.db"))
    application = create_app()
    return application, TestClient(application)


def test_rotation_trail_endpoint_passes_equity_curve_beta_values(api):
    """端点从 equity_curve 构造 beta_values 透传：BETA_SPAN 用资本账户法。"""
    application, client = api
    application.state.storage.save_backtest(_report_with_beta_values())

    body = client.get(
        "/api/backtests/bt-beta-capital/rotation-trail"
        "?page=1&page_size=20&collapse_waiting=true"
    ).json()
    spans = [item for item in body["items"] if item["kind"] == "BETA_SPAN"]
    assert len(spans) == 1
    span = spans[0]
    # 110,000 (段末) − 0 (段初前收) − (100,000 + 10 fee) = 9,990。
    assert span["pnl"] == pytest.approx(9_990.0)
    assert span["beta_invested"] == pytest.approx(100_010.0)
    assert span["return_rate"] == pytest.approx(9_990.0 / 100_010.0)
