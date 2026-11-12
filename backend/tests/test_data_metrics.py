import csv

import pytest

from app.backtest import performance_metrics
from app.data import read_csv_bars
from app.market import normalize_symbol


def test_chinese_aliases_sort_deduplicate_and_amount(tmp_path):
    path = tmp_path / "中文.csv"
    rows = [
        {"日期": "2025/01/03", "代码": "000001", "开盘价": 11, "最高价": 12, "最低价": 10, "收盘价": 11, "成交量": 2000, "换手率": "5%"},
        {"日期": "2025/01/02", "代码": "000001", "开盘价": 10, "最高价": 11, "最低价": 9, "收盘价": 10, "成交量": 1000, "换手率": "4%"},
        {"日期": "2025/01/03", "代码": "000001", "开盘价": 11, "最高价": 12, "最低价": 10, "收盘价": 11.2, "成交量": 2200, "换手率": "5%"},
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    bars = read_csv_bars(path, "daily")
    assert len(bars) == 2
    assert bars[0].date == "2025-01-02"
    assert bars[1].close == 11.2
    assert bars[1].volume == 2200
    assert bars[1].amount == pytest.approx(11.2 * 2200)
    assert bars[1].turnover_rate == 0.05
    assert bars[1].prev_close == 10


def test_market_symbol_normalization_for_baostock_and_tencent():
    star = normalize_symbol("688200.SH")
    growth = normalize_symbol("300855")
    assert star.standard == star.baostock == "sh.688200"
    assert star.tencent == "sh688200"
    assert star.index_baostock == "sh.000688"
    assert growth.standard == "sz.300855"
    assert growth.tencent == "sz300855"
    assert growth.index_baostock == "sz.399006"
    assert normalize_symbol("sh688200").standard == "sh.688200"
    with pytest.raises(ValueError):
        normalize_symbol("68820")
    with pytest.raises(ValueError):
        normalize_symbol("bj430047")


@pytest.mark.parametrize("raw,standard,exchange,board", [
    ("688200", "sh.688200", "sh", "科创板"),
    ("600000", "sh.600000", "sh", "沪市主板"),
    ("605123", "sh.605123", "sh", "沪市主板"),
    ("900901", "sh.900901", "sh", "沪市B股"),
    ("000001", "sz.000001", "sz", "深市主板"),
    ("002594", "sz.002594", "sz", "深市主板"),
    ("300855", "sz.300855", "sz", "创业板"),
    ("301001", "sz.301001", "sz", "创业板"),
])
def test_plain_six_digit_code_is_resolved_by_backend(raw, standard, exchange, board):
    """前端只发 6 位数字，交易所、板块与标准代码全部由后端适配。"""
    info = normalize_symbol(raw)
    assert info.standard == standard
    assert info.code == raw
    assert info.exchange == exchange
    assert info.board == board


@pytest.mark.parametrize("raw", [
    "688200", "688200.SH", "sh.688200", "SH688200", "sh688200",
    " 688200 ", "６８８２００", "688200.sh",
])
def test_symbol_input_formats_are_normalized(raw):
    assert normalize_symbol(raw).standard == "sh.688200"


def test_invalid_ohlc_is_rejected(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("date,open,high,low,close,volume\n2025-01-02,10,9,8,10,100\n", encoding="utf-8")
    with pytest.raises(ValueError, match="OHLC"):
        read_csv_bars(path, "daily")


def test_performance_metrics_drawdown_win_rate_profit_factor():
    curve = [
        {"date": "2025-01-01", "equity": 100.0, "market_value": 50.0},
        {"date": "2025-01-02", "equity": 120.0, "market_value": 60.0},
        {"date": "2025-01-03", "equity": 110.0, "market_value": 55.0},
    ]
    rounds = [
        {"status": "CLOSED", "pnl": 100.0},
        {"status": "CLOSED", "pnl": -50.0},
        {"status": "OPEN", "pnl": 10.0},
    ]
    metrics = performance_metrics(curve, 100, rounds, [], 1.23, 2.34, 80, 6.5, 0.08)
    assert metrics["total_return"] == pytest.approx(0.10)
    assert metrics["max_drawdown"] == pytest.approx(1 - 110 / 120)
    assert metrics["win_rate"] == 0.5
    assert metrics["profit_factor"] == 2.0
    assert metrics["total_rounds"] == 3
    assert metrics["closed_rounds"] == 2
    assert metrics["total_fees"] == 1.23
    assert metrics["t_net_profit"] == 2.34
    assert metrics["buy_hold_return"] == 0.08
    assert metrics["exposure"] == 0.5
