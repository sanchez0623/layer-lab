"""生成日线换手率 parquet（第 2 步数据层）。

从 BaoStock 拉双创标的的日线 turn（换手率），落成 turnover_daily.parquet，
供漏斗回测做 T 准入读取（quan 分钟/日线 parquet 无换手字段）。

用法:
    python backend/scripts/gen_turnover_parquet.py --start 2025-11-01 --end 2026-11-12
    python backend/scripts/gen_turnover_parquet.py --codes sz.300251,sh.688036  # 指定标的

注意: BaoStock 禁止并发，本脚本单线程串行拉取。
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import baostock as bs


def fetch_turn(codes, start, end):
    """串行拉每只标的的日线 (date, turn%)。返回 {code: {date: turn小数}}。"""
    bs.login()
    result = {}
    try:
        for i, code in enumerate(codes, 1):
            rs = bs.query_history_k_data_plus(
                code,
                "date,code,open,high,low,close,preclose,volume,amount,turn",
                start_date=start, end_date=end, frequency="d", adjustflag="2",
            )
            by_date = {}
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                date, turn = row[0], row[9]
                if turn not in ("", None):
                    by_date[date] = float(turn) / 100.0  # % -> 小数
            if by_date:
                result[code] = by_date
            if i % 100 == 0:
                print(f"  {i}/{len(codes)} ...", flush=True)
    finally:
        bs.logout()
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None, help="逗号分隔的标的代码，默认双创全部")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-11-12")
    ap.add_argument("--out", default=None, help="输出 parquet 路径")
    args = ap.parse_args()

    # 默认双创全部标的（从 quan universe 取）
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        from app.quan_data import QuanStore, quan_data_dir
        store = QuanStore(quan_data_dir())
        codes = [r.code for r in store.list_codes()]
    print(f"标的数: {len(codes)}, 区间: {args.start}~{args.end}")

    data = fetch_turn(codes, args.start, args.end)
    total = sum(len(v) for v in data.values())
    print(f"拉到 {len(data)} 只, 共 {total} 条日线 turn")

    # 落 parquet（长表: code, date, turnover）
    # 默认写到 quan 数据目录（与 minute5 同目录），load_minute5 自动发现
    import polars as pl
    rows = [
        {"code": code, "date": date, "turnover": turn}
        for code, by_date in data.items()
        for date, turn in by_date.items()
    ]
    df = pl.DataFrame(rows)
    if args.out:
        out = os.path.abspath(args.out)
    else:
        # 默认写到 quan 数据目录（与 minute5 同目录），load_minute5 自动发现。
        # 优先用 app 的 quan_data_dir()；脚本独立运行（如 /tmp）时回落实测路径。
        try:
            from app.quan_data import quan_data_dir
            out = os.path.join(quan_data_dir(), "turnover_daily.parquet")
        except Exception:
            out = "/data/quan/history/turnover_daily.parquet"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.write_parquet(out)
    print(f"写入 {out} ({df.height} 行)")


if __name__ == "__main__":
    main()
