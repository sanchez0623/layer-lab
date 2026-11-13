import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 单测默认不接真实 quan_parquet 数据目录（避免依赖本机 D 盘数据与耗时）；
# 需要覆盖 quan 链路的测试自己 monkeypatch WB_QUAN_DATA_DIR 指向 tmp_path 造的小数据集。
os.environ["WB_QUAN_DATA_DIR"] = ""

# 单测不启动后端定时器（否则每个 TestClient 都会起一个真实轮询线程）。
os.environ["WB_SCHEDULER"] = "0"

# 单测不做「用实时快照探测今天是否交易日」的联网判断（会打腾讯行情）。
os.environ["WB_TRADING_PROBE"] = "0"
