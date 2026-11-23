"""生成日线换手率 parquet（第 2 步数据层）。

从 BaoStock 拉双创标的的日线 turn（换手率），落成 turnover_daily.parquet，
供漏斗回测做 T 准入读取（quan 分钟/日线 parquet 无换手字段）。

支持断点续传：已拉取结果写入 checkpoint（默认 /data/turnover_gen_progress.pkl），
中断后重跑自动跳过已完成标的，不重复拉取（start/end 变化则作废重来）。

用法:
    python backend/scripts/gen_turnover_parquet.py --start 2020-01-01 --end 2026-11-20 \\
        --out /data/turnover_daily.parquet
    python backend/scripts/gen_turnover_parquet.py --codes sz.300251,sh.688036  # 指定标的

注意: BaoStock 禁止并发，本脚本单线程串行拉取；对底层 socket 设 30s 读超时
（get_default_socket().settimeout），单只查询卡死（recvfrom 阻塞）会被 socket.timeout
打断 → 跳过该标的并重连（logout+login），保证单只卡死不阻塞整批。
"""
import argparse
import os
import pickle
import socket
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import baostock as bs
from baostock.util import socketutil as _su

QUERY_TIMEOUT = 30          # 每只标的查询 socket 读超时（秒）
CHECKPOINT_INTERVAL = 10    # 每拉取 N 只保存一次 checkpoint（小步进，减少卡死丢进度）


def _login():
    """登录并给底层 socket 设读超时；连接失败抛错。"""
    bs.login()
    try:
        _su.get_default_socket().settimeout(QUERY_TIMEOUT)
    except Exception:
        pass


def _intern_dates(results):
    """把每只标的的 date 键 intern 去重（同一日期共享同一个 str 对象）。

    checkpoint 里每只标的的日期本来是独立 str 对象（274万 × ~68B ≈ 186MB），
    intern 后只剩约 1700 个，把省下的内存留给写盘阶段。逐只重建（临时内存仅
    一只的 dict），峰值开销可忽略。
    """
    for code, by_date in list(results.items()):
        results[code] = {sys.intern(d): t for d, t in by_date.items()}
    return results


def _load_checkpoint(path, start, end):
    """读取 checkpoint；start/end 与本次不符（口径变了）则作废重来。"""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            ck = pickle.load(f)
        if ck.get("start") == start and ck.get("end") == end:
            return _intern_dates(ck.get("results", {}))
        print("checkpoint 区间与本次不符，作废重来", flush=True)
    except Exception:
        pass
    return {}


def _save_checkpoint(path, results, start, end):
    """原子写 checkpoint（tmp+rename），避免中断损坏。"""
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"start": start, "end": end, "results": results}, f)
    os.replace(tmp, path)


def fetch_turn(codes, start, end, checkpoint=None):
    """串行拉每只标的的日线 (date, turn%)。返回 {code: {date: turn小数}}。

    断点续传：checkpoint 中已完成的标的跳过；单只查询 socket 超时则跳过并重连。
    """
    results = _load_checkpoint(checkpoint, start, end)
    if results:
        print(f"断点续传: 已有 {len(results)} 只标的完成，跳过", flush=True)

    # 默认 socket 超时：BaoStock login/query 创建的 socket 都继承该超时，
    # 因此 login 与 query 的 recvfrom 阻塞都会被 socket.timeout 打断。
    socket.setdefaulttimeout(QUERY_TIMEOUT)

    # login 也纳入超时（socket 读超时作用于其上的收包）；失败重试，最多 3 次。
    for attempt in range(3):
        try:
            _login()
            break
        except socket.timeout:
            print(f"  login 超时(第{attempt + 1}次)，重试", flush=True)
        except Exception as e:
            print(f"  login 异常(第{attempt + 1}次): {type(e).__name__}，重试", flush=True)
    else:
        raise RuntimeError("login 连续失败")

    try:
        for i, code in enumerate(codes, 1):
            if code in results:
                continue  # 已完成，跳过
            timed_out = False
            try:
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
                        by_date[sys.intern(date)] = float(turn) / 100.0  # % -> 小数
                if by_date:
                    results[code] = by_date
            except (socket.timeout, OSError) as e:
                timed_out = True
                print(f"  TIMEOUT {code} -> 跳过 ({type(e).__name__})", flush=True)
            if timed_out:
                # 超时弄脏连接，重连复位。
                try:
                    bs.logout()
                except Exception:
                    pass
                try:
                    _login()
                except Exception:
                    pass
            if i % 10 == 0:
                print(f"  {i}/{len(codes)} ...", flush=True)
            if i % CHECKPOINT_INTERVAL == 0 or i == len(codes):
                _save_checkpoint(checkpoint, results, start, end)
    finally:
        try:
            bs.logout()
        except Exception:
            pass
    return results


def _write_parquet_stream(out: str, data: dict) -> int:
    """分块流式写长表 parquet（code, date, turnover），返回总行数。

    容器只有约 1.9GB 内存（应用常驻 ~1.3GB），一次性构造全量 rows 列表
    （274万+ 个 dict）会被 OOM SIGKILL。这里按 CHUNK_ROWS 分块写成临时
    part 文件，再用 pl.scan_parquet(...).sink_parquet(out) **惰性流式合并**
    （不整体载入内存），最后清理 part 文件。容器无 pyarrow，只用 polars。

    schema 与旧文件一致：code/date 为字符串（回测用 str(row["date"])[:10]），
    turnover 为 Float64（小数，非百分比）。
    """
    import tempfile

    import polars as pl

    CHUNK_ROWS = 300_000
    tmpdir = tempfile.mkdtemp(prefix="turnover_parts_")
    part_files: list = []
    c_buf: list = []
    d_buf: list = []
    t_buf: list = []
    total = 0

    def _flush():
        nonlocal total
        if not t_buf:
            return
        total += len(t_buf)
        part = os.path.join(tmpdir, f"part_{len(part_files):05d}.parquet")
        pl.DataFrame({
            "code": pl.Series("code", c_buf),
            "date": pl.Series("date", d_buf),
            "turnover": pl.Series("turnover", t_buf, dtype=pl.Float64),
        }).write_parquet(part)
        part_files.append(part)
        c_buf.clear()
        d_buf.clear()
        t_buf.clear()

    for code, by_date in data.items():
        for date, turn in by_date.items():
            c_buf.append(code)
            d_buf.append(date)
            t_buf.append(turn)
        while len(t_buf) >= CHUNK_ROWS:
            _flush()
    _flush()

    if part_files:
        # 惰性合并 part 文件 → 最终文件（sink_parquet 流式，不整体载入内存）
        pl.scan_parquet(os.path.join(tmpdir, "part_*.parquet")).sink_parquet(out)
    else:
        pl.DataFrame({
            "code": pl.Series("code", [], dtype=pl.String),
            "date": pl.Series("date", [], dtype=pl.String),
            "turnover": pl.Series("turnover", [], dtype=pl.Float64),
        }).write_parquet(out)

    # 清理临时 part 文件
    for p in part_files:
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.rmdir(tmpdir)
    except OSError:
        pass
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None, help="逗号分隔的标的代码，默认双创全部")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-11-20")
    ap.add_argument("--out", default=None, help="输出 parquet 路径")
    ap.add_argument("--checkpoint", default="/data/turnover_gen_progress.pkl",
                    help="断点续传 checkpoint 路径（--checkpoint= 传空关闭）")
    args = ap.parse_args()

    # 默认双创全部标的（从 quan universe 取）
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        from app.quan_data import QuanStore, quan_data_dir
        store = QuanStore(quan_data_dir())
        codes = [r.code for r in store.list_codes()]
    print(f"标的数: {len(codes)}, 区间: {args.start}~{args.end}", flush=True)

    data = fetch_turn(codes, args.start, args.end, checkpoint=args.checkpoint)
    total = sum(len(v) for v in data.values())
    print(f"拉到 {len(data)} 只, 共 {total} 条日线 turn")

    # 落 parquet（长表: code, date, turnover）
    # 默认写到 quan 数据目录（与 minute5 同目录），load_minute5 自动发现
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
    # 分块流式写：容器内存只有 ~1.9GB，一次性构造全量 rows 列表会被 OOM 杀掉。
    n_rows = _write_parquet_stream(out, data)
    print(f"写入 {out} ({n_rows} 行)", flush=True)


if __name__ == "__main__":
    main()
