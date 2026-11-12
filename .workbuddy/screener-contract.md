# LayerLab 选股漏斗（五层 L0–L4）实现契约 v1

> 本文是并行开发的唯一接口契约。**先读本文，再读目标文件**。任何需要偏离契约的改动，先发消息给 Lead（`lead`），不要自行改别人的文件。

## 0. 目标（用户已确认）

把策略文档第 6 节「选股池（方案 A 五层漏斗）」落成可运行功能：

1. 后端 `screener` 模块：全 A（默认双创）日线落库缓存 + 增量更新，L0→L1→L2→L3→L4 五层筛选，输出候选池与第一名。
2. `POST /api/screener/run` 等接口。
3. 前端新增「选股」页：数据覆盖/刷新进度、参数、漏斗计数、候选池表格、第一名卡片。
4. 模拟盘/回测「一键带入标的」。
5. 池内轮动（v2）：每次清仓完成后**重新运行 L0→L4**，取当时第一名作为新标的；L0 门关则空仓等待（次日重试），不降格选票；不以预生成候选池的固定顺序换票。

术语沿用文档：L0 大盘同步门、L1 A1 超跌信号、L2 一票否决、L3 容量、L4 排序取第一、L3' 做T准入（本轮不实现，仅文档说明）。

## 1. 数据源事实（Lead 已实测 2026-11-12，沙箱内真实网络）

| 能力 | 结论 |
|---|---|
| `baostock.query_stock_basic()` | ✅ 可用。字段 `code,code_name,ipoDate,outDate,type,status`，9037 行（含指数，`type=2`；股票 `type=1`），耗时 **~65s**。名称是正确 UTF-8（控制台乱码只是编码显示问题） |
| `baostock.query_all_stock(day)` | ❌ 实测挂死 >4 分钟，**禁止使用** |
| `baostock.query_history_k_data_plus(...)` 单票 249 根日线 | ✅ 可用，**0.7~2.1s/票**；一次 login 复用（login 本身 ~0.2s） |
| 腾讯批量快照 `https://qt.gtimg.cn/q=sz300001,sh688200,...` | ✅ 可用，150 票/请求，0.33s；返回 GBK 文本，用 `response.encoding="gbk"`/`decode("gbk")`；无效代码返回 `v_xxx="";` |
| 数据覆盖 | 截至 2026-11-11 收盘；2026-11-12 为盘中数据 |
| 全双创回补预估 | 约 1900 票 × ~1.2s ≈ **35–50 分钟**，必须后台线程跑，可断点续跑（增量） |

推论：universe 元数据用 `query_stock_basic()`（一次），日线逐票 `query_history_k_data_plus`（一次 login 全程复用）；实时叠加快照用腾讯批量。**不要**每票 login/logout，**不要**用 `query_all_stock`。

## 2. 文件所有权（写作用域，互不重叠）

| 角色 | 文件 |
|---|---|
| **backend-core**（T1） | 新增 `backend/app/universe.py`、`backend/app/screener.py`；修改 `backend/app/market.py`（新增批量快照/指数按代码取数）；新增 `backend/scripts/`（回补 CLI）；新增 `backend/tests/test_universe.py`、`backend/tests/test_screener.py`；数据文件 `backend/data/universe.db`（运行时产物，可改） |
| **backend-api**（T2） | 修改 `backend/app/main.py`、`backend/app/storage.py`、`backend/app/domain.py`、`backend/app/broker.py`；新增 `backend/app/rotation.py`；新增 `backend/tests/test_screener_api.py`、`backend/tests/test_rotation.py` |
| **frontend-ui**（T3） | `frontend/src/**` 全部 |
| **Lead** | `.workbuddy/**`、清理、联调、服务重启、真实数据验收；不改代码文件 |

约定：T1/T2/T3 都不要动别人的文件；`backend/data/app.db` 是真库，测试一律用 `tmp_path` / 环境变量隔离。

## 3. 数据层：`backend/app/universe.py`

SQLite 文件默认 `backend/data/universe.db`，环境变量 `WB_UNIVERSE_DB` 可覆盖（测试用 tmp）。WAL 模式，`threading.RLock`。表：

```sql
CREATE TABLE universe_codes (
  code TEXT PRIMARY KEY,          -- 标准代码 sh.688200 / sz.300855
  board TEXT NOT NULL,            -- 创业板 / 科创板 / 沪市主板 / 深市主板 ...
  name TEXT NOT NULL,
  ipo_date TEXT NOT NULL DEFAULT '',
  out_date TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '1',
  is_st INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE universe_daily (
  code TEXT NOT NULL, date TEXT NOT NULL,          -- date = YYYY-MM-DD
  open REAL, high REAL, low REAL, close REAL, prev_close REAL,
  volume REAL, amount REAL, turnover REAL,
  PRIMARY KEY (code, date)
);
CREATE INDEX idx_universe_daily_date ON universe_daily(date);
CREATE TABLE universe_index (                       -- L0 用：创业板指/科创50
  code TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, prev_close REAL,
  volume REAL, amount REAL,
  PRIMARY KEY (code, date)
);
CREATE TABLE universe_fundamentals (
  code TEXT PRIMARY KEY, checked_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE universe_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

接口（**签名照抄，T2 依赖**）：

```python
BOARD_GEM = "创业板"
BOARD_STAR = "科创板"
SUPPORTED_BOARDS = (BOARD_GEM, BOARD_STAR)

def board_of(code: str) -> str: ...
def is_st_name(name: str) -> bool: ...          # 名称含 ST（含 *ST）
def is_delisting_name(name: str) -> bool: ...   # 名称含“退”
def is_unprofitable_u_name(name: str) -> bool: ...  # 科创板未盈利后缀 -U（含中文名后接“-U”）

@dataclass(frozen=True)
class UniverseRow:
    code: str; board: str; name: str
    ipo_date: str = ""; out_date: str = ""; status: str = "1"; is_st: bool = False
    def to_dict(self) -> dict

class UniverseStore:
    def __init__(self, db_path: str | Path) -> None
    def coverage(self) -> dict
        # {"codes": int, "rows": int, "start": str|None, "end": str|None,
        #  "boards": {"创业板": int, "科创板": int},
        #  "fundamentals": int, "updated_at": str|None,
        #  "missing_codes": int,          # 有代码但无日线的票
        #  "stale_codes": int}            # 最新日线 < 全局最新交易日的票
    def list_codes(self, boards: Sequence[str] | None = None) -> list[UniverseRow]
    def get_code(self, code: str) -> UniverseRow | None
    def last_date(self, code: str) -> str | None
    def upsert_rows(self, rows: Sequence[UniverseRow]) -> int
    def upsert_daily(self, code: str, bars: Sequence[Bar]) -> int
    def load_history(self, codes: Sequence[str], max_rows: int = 260) -> dict[str, list[Bar]]
        # 升序；Bar.timeframe="daily"，带 prev_close/turnover_rate
    def upsert_index(self, index_code: str, bars: Sequence[Bar]) -> int
    def load_index(self, index_code: str, max_rows: int = 260) -> list[Bar]
    def get_fundamentals(self, code: str) -> dict | None
    def save_fundamentals(self, code: str, payload: dict) -> None
    def latest_trade_date(self) -> str | None
```

`UniverseBootstrap`（universe.py 内）：用一次 `query_stock_basic()` 抓全市场元数据 → 只落 `SUPPORTED_BOARDS`（`type=="1"` 且 `status=="1"`，剔除 `outDate` 非空）→ `upsert_rows`。结果存入 `universe_meta("bootstrap_at")`。超过 24h 或库内无数据才重新拉取（可 `force=True`）。

`UniverseRefresher`（universe.py 内，后台线程，单例）：

```python
@dataclass
class RefreshJob:
    running: bool; started_at: str|None; finished_at: str|None
    done: int; total: int; current: str; message: str
    fetched_rows: int; errors: list[str]; skipped: int
    def to_dict(self) -> dict

class UniverseRefresher:
    def __init__(self, store: UniverseStore, market: MarketDataService, *, sleep_seconds: float = 0.02)
    def start(self, *, boards=None, codes=None, lookback_days: int = 400,
              force: bool = False, bootstrap: bool = True) -> dict
        # 已在跑 → 抛 ValueError("刷新任务已在运行")（API 返回 409 由 main.py 处理）
        # 返回 {"status": "started", "job": {...}}
    def status(self) -> dict          # {"running": bool, "job": RefreshJob.to_dict(), "coverage": {...}}
    def run_once(...同 start 参数，同步执行) -> dict   # 供测试/CLI 用，返回 {"fetched_rows","skipped","errors","coverage"}
```

刷新逻辑：
- 目标代码 = `codes`（先 `normalize_symbol` 成标准码）或 `store.list_codes(boards)`；若元数据为空且 `bootstrap=True`，先 bootstrap。
- 每个 code：`start = last_date(code)+1天`（增量）或 `今天-lookback_days`（首次/force）。若 `start > 今天` 跳过（计入 skipped）。
- 必须**一个 baostock login 复用全部查询**，用 `query_history_k_data_plus(code, "date,code,open,high,low,close,preclose,volume,amount,turn", frequency="d", adjustflag="2")`，解析同 `MarketDataService.fetch_baostock_daily`（前复权）。
- 失败重试 1 次；仍失败 → 记入 `errors`（最多保留 20 条）并继续。
- 每完成 1 票更新 job 进度（线程锁）；结束刷新两个指数，写 `universe_meta`，关闭 login。
- 增量判定：`last_date(code)` 已 ≥ `latest_trade_date` 则跳过（除非 force）。

`backend/scripts/refresh_universe.py`：CLI，`python -m scripts.refresh_universe --boards 创业板,科创板 --lookback-days 400 [--codes ...] [--limit N]`，每 20 票打印进度（unbuffered），结束后打印 coverage。供开发期回补、以及用户手动预热。

`backend/app/market.py` 需新增（T1，不破坏现有行为）：

```python
def fetch_quotes_batch(self, raw_symbols: Sequence[str]) -> dict[str, dict[str, Any]]
    # 每批 ≤80 个代码，返回 {标准代码: {"code","name","price","prev_close","change_pct","amount","turnover_rate","volume","board"}}
    # 腾讯 GBK 解码；无效代码跳过；网络异常抛 requests.RequestException
def fetch_index_daily_by_code(self, index_code: str, start_date: str, end_date: str) -> tuple[list[Bar], str]
    # index_code: "sz.399006" / "sh.000688"；sh.000688 沿用现有腾讯兜底，source="tencent_index"
```

## 4. 选股引擎：`backend/app/screener.py`

```python
@dataclass(slots=True)
class ScreenerConfig:
    boards: list[str] = field(default_factory=lambda: ["创业板", "科创板"])
    chg20_max: float = -0.15            # L1: Chg20D < -15%
    week52_drawdown: float = 0.30       # L1: Close < High250 × (1-0.30)
    day_change_max: float = 0.05        # L2: 当日涨幅 > 5% 否决
    min_amount: float = 5e8             # L3: 当日成交额 ≥ 5亿
    min_avg_amount_20d: float = 5e8     # L3: 20日均成交额 ≥ 5亿
    min_listing_sessions: int = 6       # L2: 上市不足 6 个交易日否决
    exclude_st: bool = True
    exclude_delisting: bool = True
    exclude_unprofitable_u: bool = True
    enable_fundamental_veto: bool = True  # L2 深筛：亏损且营收<1亿 / 净资产为负
    max_deep_checks: int = 10           # L4 逐只深筛上限
    blacklist: list[str] = field(default_factory=list)
    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, value: dict | None) -> "ScreenerConfig"   # 未知字段 → ValueError

@dataclass
class Candidate:
    code: str; name: str; board: str
    close: float; prev_close: float; as_of: str
    chg20d: float; week52_high: float; drawdown_52w: float
    day_change: float; amount: float; avg_amount_20d: float
    turnover: float | None; sessions: int
    score: float                                   # L4 排序键 = chg20d
    checks: list[dict]        # [{"layer":"L2","rule":"ST/*ST","status":"pass|veto|unknown","detail":"..."}]
    vetoed_by: str | None
    deep_checked: bool = False
    def to_dict(self) -> dict

@dataclass
class ScreeningResult:
    id: str; created_at: str; as_of: str
    config: dict
    gate: dict      # 见下
    funnel: dict    # {"universe","after_l0","after_l1","after_l2","after_l3","eligible","deep_checked"}
    pool: list[Candidate]       # 通过 L0–L3，按 chg20d 升序（L4 顺序；含被深筛否决者，供 UI 展示）
    eligible: list[Candidate]   # 未被否决、可用于轮动的顺序列表
    first: Candidate | None
    warnings: list[str]
    data_info: dict  # {"universe_db","coverage":{...},"realtime":bool,"realtime_quotes":int,
                     #  "fundamental": "applied"|"partial"|"unavailable"|"disabled","elapsed_seconds":float}
    elapsed_seconds: float
    def to_dict(self) -> dict   # 必须 JSON 安全（allow_nan=False 可序列化，禁止 NaN/Infinity）

class FundamentalService:
    def __init__(self, store: UniverseStore, *, sleep_seconds: float = 0.0)
    def check(self, code: str) -> dict
        # 缓存优先（store.get_fundamentals）。返回
        # {"checked": bool, "net_profit": float|None, "revenue": float|None,
        #  "negative_equity": bool|None, "veto": str|None, "source": "baostock"|"cached"|"unavailable"}
        # 亏损（net_profit<0）且营收(revenue)<1e8 → veto="亏损且营收<1亿"
        # 净资产为负：数据源不覆盖时 negative_equity=None 且不否决
    # 数据源：baostock.query_profit_data(code, year, quarter)，取最近 4 个已结束季度里最新有效一条
    # 拉取失败/字段缺失 → checked=False，本票不否决，全轮 warnings 记一次“财务数据不可用，L2 深筛降级”

class ScreenerService:
    def __init__(self, data_dir: str | Path, market: MarketDataService, *, sleep_seconds: float = 0.0)
        self.store = UniverseStore(...)   # data_dir/"universe.db"
        self.refresher = UniverseRefresher(self.store, market)
        self.market = market
    def defaults(self) -> dict   # {"config": ScreenerConfig().to_dict(), "boards": [...],
                                 #  "coverage": self.store.coverage(), "sources": {...}, "notes": [...]}
    def coverage(self) -> dict   # {"coverage": ..., "refresh": self.refresher.status()}
    def refresh(self, **kwargs) -> dict      # 透传 refresher.start
    def refresh_status(self) -> dict
    def run(self, *, config: ScreenerConfig | None = None, as_of: str | None = None,
            include_realtime: bool = False) -> ScreeningResult

def run_screener(store: UniverseStore, market: MarketDataService,
                 config: ScreenerConfig | None = None, *, as_of: str | None = None,
                 include_realtime: bool = False) -> ScreeningResult   # 纯函数入口，便于测试
```

判定细则（实现必须逐条对齐）：
- **as_of**：`as_of` 指定则用截至该日的日线；否则用 `store.latest_trade_date()`。日线历史取最近 260 根。
- **L0**：`ret20 = close[-1]/close[-21]-1`（需 ≥21 根）。创业板用 `sz.399006`、科创板用 `sh.000688`。**`ret20 < 0` 为开门**。某板块指数缺失 → 该板块按开门处理（降级中性）并 warnings 记一条；两板块都关门 → `pool/eligible` 空，`funnel.after_l0=0`，warning“L0 大盘门关闭，本轮空仓等待”。`gate` 结构：

```json
{"boards": {
   "创业板": {"index": "sz.399006", "ret20": -0.1028, "open": true,  "date": "2026-11-11"},
   "科创板": {"index": "sh.000688", "ret20": -0.0579, "open": true,  "date": "2026-11-11"}},
 "open": true}
```

- **L1**：`chg20d < config.chg20_max` **且** `close < week52_high × (1 - week52_drawdown)`；`week52_high = max(high, 最近 250 根)`，历史不足 250 根时用全部并记 `sessions`（`sessions < 250` 不否决，只影响精度）。
- **L2（快筛，无需财务）**：名称含 ST（`exclude_st`）、名称含“退”（`exclude_delisting`）、科创板未盈利 `-U` 后缀（`exclude_unprofitable_u`）、上市不足 `min_listing_sessions` 个交易日（优先 ipo_date 计算；无 ipo_date 用“库内可用根数 < 6”兜底）、当日涨幅 `day_change > day_change_max`、`blacklist` 命中。命中记 veto 后**从池中剔除**（`funnel.after_l2` 不含被否者），但该票仍写入一条 `checks` 记录（如果它通过了 L1）——即 `pool` 只放通过 L2 的票。
- **L3**：`amount ≥ min_amount` 且 `avg_amount_20d ≥ min_avg_amount_20d`（20 日含当日，用日线 amount 均值；`include_realtime=True` 且有实时快照时，当日 amount 用快照累计成交额）。
- **L4**：`pool` 按 `chg20d` 升序；从前往后逐只做深筛（最多 `max_deep_checks` 只）：财务否决 → 该票 `vetoed_by="亏损且营收<1亿"`、不进 `eligible`，继续下一只；通过 → 进 `eligible`。`first = eligible[0]`（无则 None，warning 说明）。
- **include_realtime**：仅对“通过 L1 的票”批量拉腾讯快照（≤80/批），用 `price` 重算 `chg20d`、`day_change`、`amount`（当日累计成交额）、`turnover`；20 日均额仍用日线（含最近收盘的 20 根）。快照失败 → 记 warning、回落到日线口径，不整体失败。
- **L2 深筛未覆盖项**（非标审计、资金占用、立案调查、解禁洪峰、净资产为负等数据源未覆盖）→ 每轮 warnings 记一条“L2 部分否决项数据源未覆盖，需人工复核：非标审计/资金占用/立案调查/解禁洪峰”，并在候选 `checks` 里写 `status="unknown"`。
- 排序稳定性：`sorted(key=(chg20d, code))`，保证可复现。

## 5. 存储 / 账户 / 轮动（T2）

### 5.1 storage.py
新增表与三个方法：

```sql
CREATE TABLE IF NOT EXISTS screener_runs (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, as_of TEXT NOT NULL, payload TEXT NOT NULL
);
```
```python
def save_screener_run(self, payload: dict) -> None
def get_screener_run(self, run_id: str) -> dict | None
def list_screener_runs(self, limit: int = 20) -> list[dict]
    # 摘要：[{"id","created_at","as_of","gate_open","eligible_count","first":{"code","name","chg20d"}|None}]
def delete_screener_run(self, run_id: str) -> bool
```

### 5.2 BrokerAccount 轮动字段（broker.py + domain.py）
`BrokerAccount` 增加：
```python
self.rotation_enabled: bool = False
self.rotation_pool: list[dict] = []   # [{"symbol":"sh.688200","name":"华峰测控","chg20d":-0.3153,"board":"科创板"}]
self.rotation_index: int = -1         # -1 未开始；0..n-1 当前标的序号
self.rotation_waiting: bool = False
self.rotation_note: str = ""
self.rotation_run_id: str | None = None
```
`to_dict(public)` 必须输出：
```json
"rotation": {"enabled": true, "pool": [...], "index": 0, "current_symbol": "sh.688200",
             "waiting": false, "note": "换票→sh.688200", "run_id": "run_xxx"}
```
`from_dict` 兼容旧账户（字段缺失 → 默认值）。`AccountState` 前端类型同步加 `rotation`。

### 5.3 rotation.py（v2：换票即重筛）

> 2026-11-12 用户更正：**不是按预生成候选池的固定顺序换票**，而是每次清仓完成后按当时行情重新跑五层选股、取当时第一名；L0 关门则空仓等待，不降格选票。以下为 v2 契约，替换 v1 的 `advance_if_flat` 顺序推进。

```python
def rotation_summary(account: BrokerAccount) -> dict
    # {"mode": "rescreen", "enabled", "pool": [...最近一次候选摘要...], "index", "current_symbol",
    #  "waiting", "note", "run_id", "last_attempt_date", "last_run_id", "last_screen"}
def set_rotation(account, run: dict, *, enabled: bool = True) -> None
    # 从一次 ScreeningResult dict 启用轮动：
    #   rotation_config = run["config"]；rotation_pool = 候选摘要（eligible 优先，含 deep_checked）；
    #   rotation_index 指向 first（若已持有该标的则指向它）；last_run_id/last_screen 写入；pending=False
def resolve_next(account, runner: Callable[[ScreenerConfig], dict], today: str,
                 *, current_symbol: str) -> dict | None
    # 触发条件（全部满足）：enabled；pending_advance=True；无任何持仓 qty>0；
    #   本次 tick 标的 == 当前轮动标的；rotation_last_attempt_date != today（每日最多重筛一次）
    # runner(account.rotation_config) 返回最新 ScreeningResult dict（main.py 注入，负责落库）
    # first is None → waiting=True；L0 关 → note="L0 大盘门关闭，空仓等待"；否则 note="本轮无符合条件的标的，空仓等待"；
    #     pending 保留（次日再试），返回 None
    # first.code == current_symbol → 不换票：pending=False、waiting=False、note="重新建仓→{code}（清仓后重新选股仍为该标的）"
    # first.code != current_symbol → 换票：更新 pool/index 指向 first、重置 strategy_state/sequence_index/
    #     last_daily_signal_date/armed_state、pending=False、note="换票→{code}（重新选股）"；
    #     返回 {"advanced": True, "symbol", "previous_symbol", "run_id", "as_of", ...}
# 说明：resolve_next 只做状态机；重筛、落库、dataset 切换由 main.py 注入/处理。
```

`BrokerAccount` 新增持久化字段：`rotation_config: dict`、`rotation_last_attempt_date: str | None`、`rotation_last_run_id: str | None`、`rotation_last_screen: dict`；旧账户缺失 → 默认空，重筛时回退 `ScreenerConfig()`。

> 补充（Lead 复核后加）：**启用轮动时若账户已有持仓**（尚未清仓），当前轮动标的必须指向该持仓（`set_from_run(..., current_symbol=held_symbol)`），否则该持仓清仓后的 tick 标的不等于 `current_symbol`，将永远不触发重筛。已实现并补测试 `test_rotation_targets_held_symbol_when_enabled_from_run`。

### 5.4 main.py 新增 API（全部挂在现有 `application` 上，风格与现有端点一致：同步函数、`ValueError` 由全局 handler 转 400、缺资源 404）

```python
# 请求模型（Pydantic，放文件顶部现有模型旁）
class ScreenerRefreshRequest(BaseModel):
    boards: list[str] | None = None
    codes: list[str] | None = None
    lookback_days: int = Field(default=400, ge=60, le=2000)
    force: bool = False

class ScreenerRunRequest(BaseModel):
    as_of: str | None = None
    include_realtime: bool = False
    config: dict | None = None

class ScreenerPrepareRequest(BaseModel):
    target: Literal["paper", "backtest"]
    candidate_code: str | None = None      # 默认取 result.first
    account_id: str | None = None          # target=paper 且传了则复用/重建该账户
    auto_strategy: bool = True
    rotation: bool = True

class RotationRequest(BaseModel):
    enabled: bool
    run_id: str | None = None
    pool: list[dict] | None = None         # 直接指定池（[{symbol,name,...}]）
```

端点：
| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/api/screener/defaults` | `service.defaults()` |
| GET | `/api/screener/coverage` | `service.coverage()` |
| POST | `/api/screener/refresh` | `service.refresh(**body)`；已在跑 → 409 `{"detail":"刷新任务已在运行"}` |
| GET | `/api/screener/refresh` | `service.refresh_status()` |
| POST | `/api/screener/run` | `result = service.run(...)`；`storage.save_screener_run(result.to_dict())`；返回 dict |
| GET | `/api/screener/runs?limit=20` | 摘要列表 |
| GET | `/api/screener/runs/{run_id}` | 完整结果，404 |
| DELETE | `/api/screener/runs/{run_id}` | `{"deleted": bool}` |
| POST | `/api/screener/runs/{run_id}/prepare` | 见下 |
| POST | `/api/paper/accounts/{account_id}/rotation` | 启用/停用轮动；启用时若给 `run_id` 则写 `rotation_config=run["config"]`、候选快照、last_run_id/last_screen；`pool` 参数保留为手动候选快照（**不再作为固定换票顺序**）；返回 `save_paper(account)` |

`prepare` 语义：
1. 取 run；候选 = `candidate_code` 或 `run["first"]["code"]`；找不到 → 400。
2. 本地已有覆盖最新交易日的日线/指数数据集 → 直接复用（0 网络）；否则 `market_service.fetch_history_bundle(code)` 并保存；行情失败再回落本地，最后 502。
3. `target == "backtest"` → 返回 `{"target":"backtest","candidate":..., "datasets":{"daily":...,"intraday":...,"index":...}, "account":None}`。
4. `target == "paper"` → 新建 `BrokerAccount`（initial_cash 沿用旧账户或默认 400 万；`dataset_id=daily.id`，`auto_strategy=auto_strategy`），轮动开启：`rotation_config` 取 run 的 config、候选快照、index 指向该候选、last_run_id/last_screen 写入；返回 `{"target":"paper","candidate":...,"datasets":{...},"account":...}`。
   - 注意：`rotation_pool` 里的 symbol 是标准码；建池后 `current_symbol` 必须等于候选代码。

### 5.5 live-tick 轮动接线（main.py，v2）
- `apply_bar` 清仓归零时置 `rotation_pending_advance=True`（保留）。
- `paper_live_tick` 在 `apply_bar(...)` 之后：若 `account.auto_strategy and account.rotation_enabled and account.rotation_pending_advance`：
  - 空仓且本次 tick 标的 == 当前轮动标的 → 调 `rotation.resolve_next(account, runner, bar.date, current_symbol=...)`，其中 `runner = lambda cfg: storage.save_screener_run + service.run(config=ScreenerConfig.from_dict(cfg), include_realtime=False).to_dict()`；每日最多重筛一次（`rotation_last_attempt_date`）。
  - 换票成功后：按新 symbol 找/拉日线数据集，更新 `account.dataset_id`（helper，不影响非轮动账户）。
- `current_paper_account` / `reset` / `apply-paper` 的返回里 `rotation` 字段随 `to_dict` 自动出现。
- `reset_paper_account` 保留 rotation 配置与候选快照，复位 index=-1、waiting=False、pending=False。

## 6. 前端契约（T3）

### 6.1 类型与 API（types.ts / lib/api.ts）
- `PageKey` 增加 `'screener'`。
- 新增接口类型：`ScreenerConfig`、`ScreenerCoverage`、`RefreshStatus`、`ScreenerGate`、`ScreenerFunnel`、`ScreenerCandidate`、`ScreenerCheck`、`ScreeningRun`、`ScreenerRunSummary`、`PrepareResponse`、`RotationState`；`AccountState` 加 `rotation?: RotationState | null`。
- api.ts 新增：`screenerDefaults()`、`screenerCoverage()`、`screenerRefresh(payload)`、`screenerRefreshStatus()`、`runScreener(payload)`、`screenerRuns()`、`screenerRun(id)`、`deleteScreenerRun(id)`、`prepareScreenerRun(id, payload)`、`setRotation(accountId, payload)`。全部走现有 `request<T>`。

### 6.2 新页面 `pages/ScreenerPage.tsx`（懒加载，路由 `#/screener`，导航「选股漏斗 / 五层筛选」，图标用 lucide 的 `Filter` 或 `ListFilter`）
布局模块（沿用 DataPage/StrategyPage 的 `Panel`/`Field`/`Badge`/`EmptyState`/`Loading` 等 `components/Ui.tsx` 组件与现有 CSS 风格）：
1. **数据覆盖卡**：代码数、日线行数、区间、板块分布、未回补/过期票数、最后更新；按钮「更新全市场日线」+「bootstrap 元数据」；刷新中显示进度条（done/total、当前代码、错误数），2s 轮询 `screenerRefreshStatus()`，结束后刷新 coverage。
2. **参数面板**：板块多选（创业板/科创板）、A1 20日跌幅阈值（默认 -15%）、距52周高回撤（30%）、L3 当日成交额门槛（5亿）、L3 20日均额门槛（5亿）、当日涨幅上限（5%）、`enable_fundamental_veto`、`include_realtime` 开关、「运行选股」按钮。参数直接映射 `ScreenerConfig` 字段（注意百分比→小数）。
3. **漏斗结果**：L0 门状态（两指数 ret20 + 开/闭徽标）、六段漏斗计数（universe→after_l0→after_l1→after_l2→after_l3→eligible）、warnings 列表、耗时、as_of。
4. **第一名卡**：代码/名称/板块/chg20d/距52周高/当日涨幅/成交额，两个按钮：「带入回测」「带入模拟盘」。
5. **候选池表格**：排名、代码、名称、板块、Chg20D、距52周高、当日涨幅、成交额、20日均额、换手、状态（可用/被否+原因/深度未查）；行内按钮同上两个「带入」动作。
6. **历史运行**：列表（时间、as_of、门状态、可用数、第一名），点击回看（GET run 详情），可删除。

「一键带入」：
- 回测：`prepareScreenerRun(runId, {target:'backtest', candidate_code})` → `sessionStorage.setItem('layerlab.pendingDatasets', JSON.stringify({runId, candidate, datasets}))` → `window.location.hash='#/backtest'`。
- 模拟盘：`prepareScreenerRun(runId, {target:'paper', candidate_code})` → 成功后 `window.location.hash='#/paper'`，并 `sessionStorage.setItem('layerlab.pendingPaper', ...)` 提示。
- 失败用页面级错误条展示 `ApiError.message`。

### 6.3 PaperPage 接线
- 自动刷新 tick 的 symbol：优先 `account.rotation?.current_symbol`，否则用现有输入框。
- 「池内轮动」卡（v2）：开关（`setRotation(accountId,{enabled})`）；文案「清仓后自动重新运行五层选股；L0 大盘门关闭则空仓等待，不降格选票」；展示当前标的、状态（正常推进/空仓等待）、最近重筛（时间/as_of/run_id/第一名/候选数/L0 门开闭/warnings）、候选列表标题为「最近一次选股结果（第 1 名高亮）」，不再有「池序号 N/M / 待轮动 / 已轮过」。
- 账户重置后刷新。

### 6.4 BacktestPage 接线
- 挂载时读取 `sessionStorage['layerlab.pendingDatasets']`：若存在，自动把 `daily/intraday/index` dataset id 填入对应选择器，展示提示条「已从选股结果带入：sh.xxxxxx 名称（运行 run_xxx）」，然后删除该 key。

## 7. 测试与验收

- 后端：`cd backend && python -m pytest`（现有 22 项必须全绿）＋ 新增测试。
  - T1：`test_universe.py`（内存/tmp SQLite：upsert/增量/coverage/load_history 升序/index/fundamentals 缓存；bootstrap 解析用 monkeypatch 模拟 baostock）；`test_screener.py`（构造合成 260 根日线：L0 开/关、L1 边界、L2 各否决项、L3 门槛、L4 排序与深筛逐只否决取下一只、gate/funnel/pool 结构、`to_dict` 可 JSON 序列化）。
  - T2：`test_screener_api.py`（TestClient：defaults/coverage/refresh 409/run 落库/runs 列表/get/delete/prepare-backtest 用 monkeypatch market service 返回合成 bundle/prepare-paper 建账户并带 rotation）；`test_rotation.py`（池推进行为：未空仓不推进、空仓推进、L0 门关等待、池完等待、非当前标的 tick 不推进、序列化 roundtrip）。
- 前端：`cd frontend && pnpm build`（等价 `tsc --noEmit && vite build`）必须通过；不要改端口/代理。
- 真实数据验收（Lead 执行）：start 全双创回补 → run screener → 与文档快照（2026-11-11：L1 双创 191 只、容量后 20 只、排除 -U 后 17 只、first=sh688200 华峰测控）**量级对齐**（允许数据源/日期差异，结论写进 `.workbuddy/memory`）。
- 禁止事项：不要改端口（8010/5180）；不要重启用户正在跑的 8010 服务（Lead 统一处理）；不要动 `backend/data/app.db` 里的真实账户数据（除用户明确要求的验证账户）；避免在 shell 里用管道接外部程序输出（沙箱会拒绝），跑测试直接 `python -m pytest`。

## 8. 协作
- 进度/阻塞用 `send_message` 给 `lead`，不要静默等待。
- T1 先把 `universe.py`/`screener.py` 的公开签名按契约落好（可先 stub），立即通知 T2（`backend-api`）可以 import；随后补实现。
- 每完成一个文件/阶段，给 Lead 发一条简短消息（做了什么、下一步、风险）。
