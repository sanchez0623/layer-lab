"""生成 5 分钟换手率 parquet（goal 第五点：股本反推方案）。

免费源里唯一能拿「长历史 + 5 分钟换手率」的路径：
  1. BaoStock 日线 turn（换手率%）+ volume（股）→ 反推当日自由流通股本
     free_float = volume / (turn/100)   # turn 是 %，除 100 转小数
  2. BaoStock 5 分钟 K 线 volume（股）→ 每根换手率 = bar.volume / free_float
  3. 落 minute5_turnover.parquet：code, date, turnover（小数）

口径说明：
  - BaoStock 日线 turn 是「换手率%」，volume 是「成交股数」，两者同源 → 反推的
    free_float 是「当日参与交易的自由流通股」口径，与行情软件换手率同口径。
  - 5 分钟 bar volume 与日线 volume 同为「股」（quan parquet 保持原始口径），
    前复权不影响量（只折价），故 bar.volume / free_float 即该 5 分钟换手率。
  - 单日 free_float 用当日日线算，避免用最新股本回看历史（送转/增发会变股本）。

用法:
    python backend/scripts/gen_minute5_turnover.py --codes sz.300251,sh.688036 \
        --start 2021-01-01 --end 2026-11-12
    python backend/scripts/gen_minute5_turnover.py --start 2021-01-01  # 默认双创全部

注意: BaoStock 禁止并发，本脚本单线程串行拉取；5 分钟 K 线按标的逐只拉。
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import baostock as bs


def _fmt_code(code: str) -> str:
    """统一成 BaoStock 格式 sh.600000 / sz.000001。"""
    c = code.strip().lower()
    if "." in c:
        return c
    pure = c[-6:]
    prefix = "sh" if c.startswith(("6", "9")) else "sz"
    return f"{prefix}.{pure}"


def fetch_free_float(codes, start, end):
    """日线 turn+volume → {code: {date: free_float股}}。"""
    by_code = {}
    for i, code in enumerate(codes, 1):
        rs = bs.query_history_k_data_plus(
            code, "date,code,volume,turn",
            start_date=start, end_date=end, frequency="d", adjustflag="2",
        )
        by_date = {}
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            date, vol, turn = row[0], row[2], row[3]
            if turn in ("", None) or vol in ("", None):
                continue
            turn_f = float(turn) / 100.0
            if turn_f <= 0:
                continue
            by_date[date] = float(vol) / turn_f  # 自由流通股本（股）
        if by_date:
            by_code[code] = by_date
        if i % 50 == 0:
            print(f"  日线股本反推 {i}/{len(codes)} ...", flush=True)
    return by_code


def fetch_minute5_turnover(code, start, end, free_float_by_date):
    """5 分钟 K 线 → [{code,date,turnover}]，换手=bar.volume/当日free_float。"""
    rows = []
    rs = bs.query_history_k_data_plus(
        code, "date,time,volume",
        start_date=start, end_date=end, frequency="5", adjustflag="2",
    )
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        # BaoStock 分钟 time 格式: 20210318093500000 → 取 YYYY-MM-DD
        t = row[1]
        date = f"{t[0:4]}-{t[4:6]}-{t[6:8]}"
        vol = row[2]
        if vol in ("", None):
            continue
        ff = free_float_by_date.get(date)
        if not ff:
            continue  # 当日无日线（停牌/新股）跳过
        rows.append({
            "code": code, "date": date,
            "turnover": float(vol) / ff,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None, help="逗号分隔标的，默认双创全部")
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-11-12")
    ap.add_argument("--out", default=None, help="输出 parquet 路径")
    args = ap.parse_args()

    if args.codes:
        codes = [_fmt_code(c) for c in args.codes.split(",") if c.strip()]
    else:
        from app.quan_data import QuanStore, quan_data_dir
        store = QuanStore(quan_data_dir())
        codes = [r.code for r in store.list_codes()]
    print(f"标的数: {len(codes)}, 区间: {args.start}~{args.end}")

    bs.login()
    try:
        print("Step1: 日线反推自由流通股本 ...")
        free_float = fetch_free_float(codes, args.start, args.end)
        print(f"  反推到 {len(free_float)} 只的股本")

        print("Step2: 5 分钟量 ÷ 股本 → 每根换手 ...")
        all_rows = []
        for i, code in enumerate(codes, 1):
            ff = free_float.get(code, {})
            if not ff:
                continue
            all_rows.extend(fetch_minute5_turnover(code, args.start, args.end, ff))
            if i % 50 == 0:
                print(f"  5分钟换手 {i}/{len(codes)} ...", flush=True)
    finally:
        bs.logout()

    print(f"共 {len(all_rows)} 条 5 分钟换手")
    import polars as pl
    df = pl.DataFrame(all_rows)
    if args.out:
        out = os.path.abspath(args.out)
    else:
        try:
            from app.quan_data import quan_data_dir
            out = os.path.join(quan_data_dir(), "minute5_turnover.parquet")
        except Exception:
            out = "/data/quan/history/minute5_turnover.parquet"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.write_parquet(out)
    print(f"写入 {out} ({df.height} 行)")


if __name__ == "__main__":
    main()
