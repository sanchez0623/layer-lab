"""生成换手率 parquet 的看门狗。

BaoStock 免费源会间歇性挂死（recvfrom 永久阻塞；socket 超时被其内部重试吞掉），
单次跑不可靠。本脚本循环启动 gen_turnover_parquet.py（带 checkpoint 断点续传），
若 checkpoint 超过 STALL_SECONDS 无更新则强杀并重启，直到成功写出（退出码 0）。

用法（服务器）:
    python /app/backend/scripts/gen_turnover_watchdog.py
"""
import os
import subprocess
import sys
import time

CHECKPOINT = "/data/turnover_gen_progress.pkl"
STALL_SECONDS = 180     # checkpoint 无更新的秒数阈值，超过则强杀重启
POLL_INTERVAL = 10      # 轮询间隔（秒）
GEN_LOG = "/data/turnover_gen.log"
MAX_FAST_FAIL = 5       # 连续快速失败（<60s，多为真 bug）上限，避免无限重启
CMD = [
    sys.executable, "/app/backend/scripts/gen_turnover_parquet.py",
    "--start", "2020-01-01", "--end", "2026-11-20",
    "--out", "/data/turnover_daily.parquet",
    "--checkpoint", CHECKPOINT,
]


def ckpt_mtime():
    try:
        return os.path.getmtime(CHECKPOINT)
    except OSError:
        return 0.0


def main():
    attempt = 0
    fast_fail = 0
    while True:
        attempt += 1
        print(f"[watchdog] attempt {attempt}: start gen {time.strftime('%H:%M:%S')}", flush=True)
        t0 = time.time()
        log = open(GEN_LOG, "w")
        proc = subprocess.Popen(CMD, stdout=log, stderr=subprocess.STDOUT)
        last_progress = time.time()
        last_ckpt = ckpt_mtime()
        while proc.poll() is None:
            time.sleep(POLL_INTERVAL)
            m = ckpt_mtime()
            if m > last_ckpt:
                last_ckpt = m
                last_progress = time.time()
            elif time.time() - last_progress > STALL_SECONDS:
                print(f"[watchdog] checkpoint stall {STALL_SECONDS}s -> kill pid {proc.pid}", flush=True)
                proc.kill()
                proc.wait()
                break
        rc = proc.returncode
        log.close()
        if rc == 0:
            print("[watchdog] gen completed", flush=True)
            break
        elapsed = time.time() - t0
        if elapsed < 60:
            fast_fail += 1
            print(f"[watchdog] attempt {attempt} rc={rc} in {elapsed:.0f}s (fast fail {fast_fail})", flush=True)
            if fast_fail >= MAX_FAST_FAIL:
                print(f"[watchdog] {MAX_FAST_FAIL} 次快速失败，放弃", flush=True)
                sys.exit(1)
        else:
            fast_fail = 0
            print(f"[watchdog] attempt {attempt} rc={rc} after {elapsed:.0f}s, restart (resume)", flush=True)


if __name__ == "__main__":
    main()
