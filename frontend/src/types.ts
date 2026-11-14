export type PageKey = 'backtest' | 'paper' | 'data' | 'strategy' | 'screener';
export type Timeframe = 'daily' | 'tick' | '1m' | '5m';
export type OrderSide = 'buy' | 'sell';
export type OrderType = 'market' | 'limit';
/** 委托用途：PERMANENT 计入 16 笔金额序列（买入），T 为做T仓，不占笔位。 */
export type OrderPurpose = 'PERMANENT' | 'T';

export interface Dataset {
  id: string;
  name: string;
  symbol: string;
  timeframe: Timeframe | string;
  start: string;
  end: string;
  rows: number;
  source: string;
  is_synthetic: boolean;
  created_at: string;
}

/** 界面层费用配置：费率单位一律「万分之」，最低佣金单位为元。 */
export interface FeeConfig {
  commission_rate: number;
  min_commission: number;
  stamp_tax_rate: number;
  handling_fee_rate: number;
  regulatory_fee_rate: number;
  transfer_fee_rate: number;
}

export interface FeeConfigResponse {
  version: string;
  fees: FeeConfig;
  defaults: FeeConfig;
  strategy_patch: Record<string, number>;
  note: string;
  updated_at?: string | null;
}

export interface StrategyConfig {
  enable_t: boolean;
  commission_rate: number;
  min_commission: number;
  stamp_tax_rate: number;
  handling_fee_rate?: number;
  regulatory_fee_rate?: number;
  transfer_fee_rate: number;
  slippage_bps: number;
  volume_participation: number;
  /** 已废弃：做T平仓改用 t_target_net_spread（扣成本后净利止盈），本字段不再参与做T计算，仅为兼容后端 config 保留 */
  t_target_spread?: number;
  /** 做T净利止盈阈值（小数，0.03 = 扣完成本后净赚 3% 平T）；可在模拟盘账户配置里改，下一根K线起生效 */
  t_target_net_spread?: number;
  /** 做T的T+1日尾盘硬截止时间（HH:MM，默认 14:50），到点仍未止盈则无条件强平；T0 当日不强平 */
  t_exit_cutoff?: string;
  /** 已废弃：做T金额现跟随当前加仓档 installment_amounts[sequence_index]（满仓后取最大档），本字段不再参与做T计算，仅为兼容后端 config 保留 */
  t_order_amount?: number;
}

export type StrategyDefaults = Partial<StrategyConfig> & {
  config?: Partial<StrategyConfig>;
  fees?: FeeConfig;
  initial_cash?: number;
};

export interface BacktestRequest {
  name: string;
  dataset_id: string;
  intraday_dataset_id?: string;
  index_dataset_id?: string;
  initial_cash: number;
  start_date?: string;
  end_date?: string;
  config: Partial<StrategyConfig>;
  fees?: FeeConfig;
}

/** 漏斗轮动回测请求：不选单标的，改为每次清仓后按选股漏斗重新选票。 */
export interface FunnelBacktestRequest {
  name: string;
  initial_cash: number;
  start_date?: string;
  end_date?: string;
  config: Partial<StrategyConfig>;
  fees?: FeeConfig;
  screener?: Partial<ScreenerConfig>;
}

/** 单轮换票轨迹（漏斗轮动回测 data_info.rotation_trail 元素）。 */
export interface RotationTrailItem {
  round: string;
  symbol: string | null;
  name: string;
  board: string;
  select_date: string;
  entry_date: string | null;
  exit_date: string | null;
  pnl: number | null;
  chg20d_at_select: number | null;
  gate_open: boolean;
  status: 'CLOSED' | 'OPEN' | 'WAITING' | string;
  /** 本轮逐股决策日志（为什么开门/关门/空仓/深度超跌旁路几只），挂在对应选股日。 */
  screen_warnings?: string[];
  /** 本轮漏斗计数 universe→L0→L1→L2→L3→eligible。 */
  funnel?: Record<string, number>;
}

/**
 * 后台分页统一信封（与 backend/app/pagination.py 的 paginate() 对齐）。
 * 任何「可能变长」的列表一律走这套结构，前端配套组件见 components/PaginationBar.tsx。
 */
export interface Paginated<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  has_prev: boolean;
  has_next: boolean;
}

export interface EquityPoint {
  date: string;
  equity: number;
  cash: number;
  market_value: number;
  invested: number;
  drawdown: number;
  position_qty: number;
}

export interface ReportMetrics {
  initial_cash: number;
  final_equity: number;
  net_profit: number;
  total_return: number;
  annualized_return: number;
  max_drawdown: number;
  sharpe_ratio: number;
  volatility: number;
  calmar_ratio: number;
  win_rate: number;
  profit_factor: number;
  total_rounds: number;
  closed_rounds: number;
  total_trades: number;
  total_fees: number;
  t_net_profit: number;
  t_trade_count: number;
  max_invested: number;
  exposure: number;
  max_float_loss: number;
  buy_hold_return: number;
}

export interface Trade {
  id?: string | number;
  order_id?: string | number;
  side: OrderSide | string;
  quantity: number;
  price: number;
  amount?: number;
  total_fee?: number;
  notional?: number;
  fee?: number;
  commission?: number;
  stamp_tax?: number;
  handling_fee?: number;
  regulatory_fee?: number;
  transfer_fee?: number;
  slippage?: number;
  cash_flow?: number;
  timestamp: string;
  symbol: string;
  purpose?: string;
  realized_pnl?: number;
}

export interface Order {
  id: string | number;
  symbol: string;
  side: OrderSide | string;
  order_type?: OrderType | string;
  type?: OrderType | string;
  quantity: number;
  limit_price?: number | null;
  price?: number | null;
  status: string;
  created_at?: string;
  timestamp?: string;
  filled_quantity?: number;
  filled_qty?: number;
  purpose?: string;
}

export interface Round {
  id?: string | number;
  round?: number;
  index?: number;
  status?: string;
  start_date?: string;
  end_date?: string;
  invested?: number;
  max_invested?: number;
  pnl?: number;
  net_profit?: number;
  return_rate?: number;
  total_return?: number;
  trade_count?: number;
  [key: string]: unknown;
}

export interface BacktestEvent {
  timestamp?: string;
  date?: string;
  type?: string;
  message?: string;
  [key: string]: unknown;
}

export interface Report {
  id: string;
  name: string;
  status: string;
  created_at: string;
  config: StrategyConfig;
  metrics: ReportMetrics;
  equity_curve: EquityPoint[];
  trades: Trade[];
  orders: Order[];
  rounds: Round[];
  events: BacktestEvent[];
  warnings: string[];
  data_info: Record<string, unknown>;
}

export interface BacktestSummary {
  id: string;
  name: string;
  status: string;
  created_at: string;
  dataset_id?: string;
  metrics?: Partial<ReportMetrics>;
  [key: string]: unknown;
}

export interface Position {
  symbol: string;
  total_qty: number;
  sellable_qty: number;
  permanent_qty: number;
  permanent_cost: number;
  total_cost: number;
  average_cost: number;
  /** 当前持仓成本中包含的买入手续费（卖出费用计入已实现盈亏）。 */
  buy_fee: number;
  t_active_qty: number;
  last_price: number;
  market_value: number;
  float_pnl: number;
}

export interface PositionQuote {
  symbol: string;
  last_price: number;
}

export interface AccountState {
  id: string;
  name: string;
  initial_cash: number;
  cash: number;
  available_cash: number;
  equity: number;
  market_value: number;
  realized_pnl: number;
  total_fees: number;
  positions: Position[];
  /** 后端行情缓存（含未持仓标的的最新价）；旧后端未返回该字段时前端回退到 positions。 */
  quotes?: PositionQuote[];
  orders: Order[];
  trades: Trade[];
  t_net_profit: number;
  sequence_index: number;
  armed_state: 'Idle' | 'Armed' | 'Exit' | string;
  status: string;
  auto_strategy?: boolean;
  config: StrategyConfig;
  dataset_id?: string;
  cursor: number;
  /** 池内轮动状态；旧后端未返回该字段时为 undefined（契约 §5.2 / §6.3）。 */
  rotation?: RotationState | null;
}

export interface CreateAccountRequest {
  name: string;
  initial_cash: number;
  dataset_id?: string;
  auto_strategy?: boolean;
  config?: Partial<StrategyConfig>;
  fees?: FeeConfig;
}

export interface CreateOrderRequest {
  symbol: string;
  side: OrderSide;
  order_type: OrderType;
  quantity: number;
  limit_price?: number;
  purpose?: OrderPurpose;
}

export interface DemoDatasets {
  daily: Dataset | string;
  intraday: Dataset | string;
  index: Dataset | string;
}

export interface MarketSourceStatus {
  baostock?: { role?: string; status?: string };
  tencent?: { role?: string; status?: string };
  realtime_interval_seconds?: number;
  note?: string;
  [key: string]: unknown;
}

export interface MarketQuote {
  symbol: string;
  code?: string;
  standard?: string;
  exchange?: string;
  board?: string;
  name?: string;
  price: number;
  prev_close?: number;
  change?: number;
  change_pct?: number;
  volume?: number;
  amount?: number;
  turnover_rate?: number | null;
  bid?: number | null;
  ask?: number | null;
  timestamp?: string;
  source?: string;
  [key: string]: unknown;
}

export interface MarketFetchRequest {
  symbol: string;
  start_date?: string;
  end_date?: string;
  include_intraday?: boolean;
}

export interface MarketFetchResponse {
  symbol: string;
  code?: string;
  exchange?: string;
  board?: string;
  name?: string;
  index_symbol?: string;
  daily: Dataset;
  index: Dataset;
  intraday?: Dataset | null;
  quote?: MarketQuote;
  source_info?: Record<string, unknown>;
}

export interface HealthResponse {
  status?: string;
  [key: string]: unknown;
}

/* ==========================================================================
 * 分板块 A 股交易规则（trading_rules.py 单一来源）
 * ========================================================================== */

/** 单个板块的交易规则快照；数量单位为股，金额单位为元。 */
export interface BoardRules {
  board: string;
  min_qty: number;
  step: number;
  limit_order_cap: number;
  market_order_cap: number;
  odd_lot_threshold: number;
  odd_lot_label: string;
  limit_pct: number;
  cage_pct: number;
  cage_abs_floor: number;
  cage_label: string;
  rule_basis: string;
}

/** GET /trading-rules/{symbol}：按标的返回板块规则 + 价格笼子 + 未建模清单。 */
export interface TradingRulesResponse {
  symbol: string;
  board: string;
  is_modeled_board: boolean;
  rules: BoardRules;
  price_cage: { lower: number; upper: number; reference_price: number } | null;
  not_modeled: string[];
}

/** GET /trading-rules：六板块规则全表。 */
export interface TradingRulesAllResponse {
  boards: BoardRules[];
  not_modeled: string[];
}

/* ==========================================================================
 * 选股漏斗（五层 L0–L4）类型，对应契约 §6.1
 * ========================================================================== */

/** 选股参数，与后端 ScreenerConfig 一一对应（比率字段为小数，例如 -0.15 = -15%）。 */
export interface ScreenerConfig {
  boards: string[];
  chg20_max: number;
  week52_drawdown: number;
  day_change_max: number;
  min_amount: number;
  min_avg_amount_20d: number;
  min_listing_sessions: number;
  exclude_st: boolean;
  exclude_delisting: boolean;
  exclude_unprofitable_u: boolean;
  enable_fundamental_veto: boolean;
  max_deep_checks: number;
  blacklist: string[];
}

/** 全市场日线缓存覆盖情况（UniverseStore.coverage）。 */
export interface ScreenerCoverage {
  codes: number;
  rows: number;
  start: string | null;
  end: string | null;
  boards: Record<string, number>;
  fundamentals: number;
  updated_at: string | null;
  /** 有代码但无日线的票。 */
  missing_codes: number;
  /** 最新日线落后于全局最新交易日的票。 */
  stale_codes: number;
}

/** 后台回补任务快照（RefreshJob.to_dict）。 */
export interface RefreshJob {
  running: boolean;
  started_at: string | null;
  finished_at: string | null;
  done: number;
  total: number;
  current: string;
  message: string;
  fetched_rows: number;
  errors: string[];
  skipped: number;
}

/** GET /screener/refresh 与 coverage 内嵌的刷新状态（UniverseRefresher.status）。 */
export interface RefreshStatus {
  running: boolean;
  job: RefreshJob;
  coverage: ScreenerCoverage;
}

/** GET /screener/coverage 返回体。 */
export interface ScreenerCoverageResponse {
  coverage: ScreenerCoverage;
  refresh: RefreshStatus;
}

/** 单个板块的 L0 大盘门状态。 */
export interface ScreenerGateBoard {
  index: string;
  ret20: number;
  open: boolean;
  date: string;
}

/** L0 大盘同步门：ret20 < 0 视为开门。 */
export interface ScreenerGate {
  boards: Record<string, ScreenerGateBoard>;
  open: boolean;
}

/** 六段漏斗计数（eligible 之后另有 deep_checked）。 */
export interface ScreenerFunnel {
  universe: number;
  after_l0: number;
  after_l1: number;
  after_l2: number;
  after_l3: number;
  eligible: number;
  deep_checked: number;
}

/** 候选票逐条检查记录：status 为 pass / veto / unknown。 */
export interface ScreenerCheck {
  layer: string;
  rule: string;
  status: 'pass' | 'veto' | 'unknown' | string;
  detail: string;
}

/** 候选池中的股票。 */
export interface ScreenerCandidate {
  code: string;
  name: string;
  board: string;
  close: number;
  prev_close: number;
  as_of: string;
  chg20d: number;
  week52_high: number;
  drawdown_52w: number;
  day_change: number;
  amount: number;
  avg_amount_20d: number;
  turnover: number | null;
  sessions: number;
  score: number;
  checks: ScreenerCheck[];
  vetoed_by: string | null;
  deep_checked: boolean;
}

export interface ScreenerDataInfo {
  universe_db?: string;
  coverage?: ScreenerCoverage;
  realtime?: boolean;
  realtime_quotes?: number;
  fundamental?: string;
  elapsed_seconds?: number;
  [key: string]: unknown;
}

/** 一次完整选股结果（GET /screener/runs/{id}）。 */
export interface ScreeningRun {
  id: string;
  created_at: string;
  as_of: string;
  config: ScreenerConfig;
  gate: ScreenerGate;
  funnel: ScreenerFunnel;
  pool: ScreenerCandidate[];
  eligible: ScreenerCandidate[];
  first: ScreenerCandidate | null;
  warnings: string[];
  data_info: ScreenerDataInfo;
  elapsed_seconds: number;
}

/** 历史运行摘要（GET /screener/runs）。 */
export interface ScreenerRunSummary {
  id: string;
  created_at: string;
  as_of: string;
  gate_open: boolean;
  eligible_count: number;
  first: { code: string; name: string; chg20d: number } | null;
}

/** GET /screener/defaults。 */
export interface ScreenerDefaults {
  config: ScreenerConfig;
  boards: string[];
  coverage: ScreenerCoverage;
  sources?: Record<string, unknown>;
  notes?: string[];
}

export interface ScreenerRefreshRequest {
  boards?: string[];
  codes?: string[];
  lookback_days?: number;
  force?: boolean;
}

export interface ScreenerRefreshResponse {
  status: string;
  job: RefreshJob;
}

export interface ScreenerRunRequest {
  as_of?: string | null;
  include_realtime?: boolean;
  config?: Partial<ScreenerConfig>;
}

export interface ScreenerPrepareRequest {
  target: 'paper' | 'backtest';
  candidate_code?: string;
  account_id?: string;
  auto_strategy?: boolean;
  rotation?: boolean;
}

/** prepare 保存并返回的三个数据集（分钟/指数可能缺失）。 */
export interface ScreenerPrepareDatasets {
  daily: Dataset;
  intraday?: Dataset | null;
  index?: Dataset | null;
}

/** prepare 返回的候选摘要（后端只回传 code / name / board / chg20d）。 */
export interface ScreenerPreparedCandidate {
  code: string;
  name: string;
  board: string;
  chg20d: number | null;
}

/** POST /screener/runs/{id}/prepare 返回体。 */
export interface PrepareResponse {
  target: 'paper' | 'backtest' | string;
  candidate: ScreenerPreparedCandidate;
  datasets: ScreenerPrepareDatasets;
  account: AccountState | null;
}

/** 轮动池条目（BrokerAccount.rotation_pool）。 */
export interface RotationPoolItem {
  symbol: string;
  name: string;
  chg20d?: number;
  board?: string;
  [key: string]: unknown;
}

/** 最近一次重筛的第一名摘要。 */
export interface RotationScreenFirst {
  code: string;
  name: string;
  chg20d: number | null;
}

/** 轮动 v2：清仓后重新跑五层选股的结果摘要（契约 rotation.last_screen）。 */
export interface RotationLastScreen {
  run_id?: string | null;
  as_of?: string | null;
  first?: RotationScreenFirst | null;
  eligible_count?: number;
  gate_open?: boolean;
  warnings?: string[];
}

/** 账户池内轮动状态（契约 §5.2 rotation，v2 语义：清仓后重新选股取第一名）。 */
export interface RotationState {
  enabled: boolean;
  /** rescreen = 每次清仓后重新运行五层选股并取当时第一名。 */
  mode?: 'rescreen' | string;
  pool: RotationPoolItem[];
  /** 旧版固定池语义保留字段；v2 不再按池序号推进。 */
  index: number;
  current_symbol: string | null;
  waiting: boolean;
  note: string;
  run_id: string | null;
  /** 最近一次触发重筛的日期（YYYY-MM-DD）。 */
  last_attempt_date?: string | null;
  /** 最近一次成功重筛的 run id。 */
  last_run_id?: string | null;
  /** 最近一次重筛摘要；尚未成功重筛时为 null。 */
  last_screen?: RotationLastScreen | null;
}

export interface RotationRequest {
  enabled: boolean;
  run_id?: string;
  pool?: RotationPoolItem[];
}

/** GET /paper/scheduler：后端定时器状态（成交由它推进，前端只做展示与轮询同步）。 */
export interface SchedulerStatus {
  enabled: boolean;
  running: boolean;
  interval_seconds: number;
  /** 最近一次触发的 5 分钟槽位（ISO）。 */
  last_slot: string | null;
  /** 最近一次实际 tick 完成时间（ISO）。 */
  last_tick_at: string | null;
  last_error: string | null;
  ticks: number;
  failures: number;
  last_result: { ticked?: string[]; skipped?: string[]; errors?: string[] };
  /** 交易日历判定（quan 日历优先，缺失回落工作日）。 */
  trading?: TradingSession;
}

/** GET /trading/session：交易日 / 交易时段判定，用于决定前端是否自动轮询。 */
export interface TradingSession {
  today: string;
  is_trading_day: boolean;
  next_trading_day: string | null;
  /** quan_calendar = 真实交易日历（能识别节假日）；weekday_fallback = 仅工作日启发式。 */
  source: 'quan_calendar' | 'weekday_fallback' | string;
  in_session?: boolean;
  session_windows?: string[];
}
