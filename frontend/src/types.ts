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
  /** 滑点模型：fixed=固定 slippage（bps，旧行为）/ impact=平方根冲击成本模型（随成交量变化） */
  slippage_model?: 'fixed' | 'impact';
  /** 冲击成本系数 k（>=0，默认 1.0）：impact 模式下 rate = k × √(本笔量/当日成交量) */
  impact_coefficient?: number;
  /** 已废弃：做T平仓改用 t_target_net_spread（扣成本后净利止盈），本字段不再参与做T计算，仅为兼容后端 config 保留 */
  t_target_spread?: number;
  /** 做T净利止盈阈值（小数，0.03 = 扣完成本后净赚 3% 平T）；可在模拟盘账户配置里改，下一根K线起生效 */
  t_target_net_spread?: number;
  /** 做T的T+1日尾盘硬截止时间（HH:MM，默认 14:50），到点仍未止盈则无条件强平；T0 当日不强平 */
  t_exit_cutoff?: string;
  /** 已废弃：做T金额现跟随当前加仓档 installment_amounts[sequence_index]（满仓后取最大档），本字段不再参与做T计算，仅为兼容后端 config 保留 */
  t_order_amount?: number;
  /** 价格轨止盈阈值（小数，0.15 = 15%）：持仓浮盈达到该幅度即触发价格轨清仓；也是超期降阈的基准值 */
  price_take_profit?: number;
  /** 账户轨止盈阈值（小数，0.15 = 15%）：账户口径浮盈达到该幅度触发清仓（当前实际清仓判定沿用价格轨阈值） */
  account_take_profit?: number;
  /** 超期降阈：持有 ≥ 该交易日数后，价格轨/账户轨止盈阈值降为 holding_tp_override；与 holding_tp_override 必须同时配置 */
  max_holding_days?: number | null;
  /** 超期降阈后的止盈阈值（小数，0.03 = 3%）；与 max_holding_days 必须同时配置 */
  holding_tp_override?: number | null;
  /** 硬性最大持仓天数：持有 ≥ 该交易日数后无条件强平（0 = 不限/关闭）；独立于 max_holding_days（超期降阈） */
  force_exit_holding_days?: number;
  /** 加仓按档位先松后紧：A3 浮亏信号随 16 笔序列档位逐级加严（false = 全档统一原始 A3） */
  add_tier_strict?: boolean;
  /** 深跌幅当日做T：日内跌幅 ≥ 该值（小数，0.10 = 10%）绕过底仓笔数门槛；null/0 = 关闭 */
  t_deep_drop_pct?: number | null;
  /** 做T准入：近5根平均振幅下限（小数，0.04 = 4%） */
  t_min_amplitude?: number;
  /** 做T准入：换手率下限（小数，0.03 = 3%） */
  t_min_turnover?: number;
  /** 做T准入：换手率上限（小数，0.12 = 12%） */
  t_max_turnover?: number;
  /** 做T准入：期望净利下限（振幅扣掉一次往返成本后仍需 ≥ 该值，小数，0.03 = 3%） */
  t_min_expected_net_spread?: number;
  /** 做T底仓笔数门槛：累计底仓（sequence_index）达到该笔数才允许做T准入（1~16 笔，默认 4 = 第5笔起）；深跌幅当日可绕过 */
  t_min_base_entries?: number;
  /** 指数 beta 腿：空仓期间持有创业板 ETF，选到个股后卖出换股 */
  etf_beta_enabled?: boolean;
  etf_symbol?: string;
  /** ETF 仓位占账户权益比例（小数，0.2 = 20%） */
  etf_position_pct?: number;
  /** 入场趋势过滤：指数收盘站上 N 日均线才买 */
  etf_entry_ma_days?: number;
  /** 出场：指数收盘跌破 N 日均线清仓 */
  etf_exit_ma_days?: number;
  /** ETF 硬止损：较建仓成本回撤达到该比例清仓（小数，0.06 = 6%） */
  etf_hard_stop_pct?: number;
  /** 入场不追高：指数相对入场均线的偏离上限（小数，0.08 = 8%）；0 表示关闭 */
  etf_entry_max_deviation?: number;
  /** 清仓后冷却：至少 N 个交易日内不再建仓；0 表示关闭 */
  etf_reentry_cooldown_days?: number;
  /** beta 腿是否与个股并存：False=互斥（个股空仓才建、选到个股清仓）；True=同持，建仓额度按可用现金封顶 */
  etf_beta_coexist?: boolean;
  /** 盈利提现模式：none=关闭 / monthly=每月提现 / per_trade=每笔提现 */
  withdraw_mode?: 'none' | 'monthly' | 'per_trade';
  /** 盈利提现提取方式：ratio=按盈利比例 / fixed=按固定金额 */
  withdraw_method?: 'ratio' | 'fixed';
  /** 按盈利比例提取的比例（小数，0.2 = 20%） */
  withdraw_ratio?: number;
  /** 按固定金额提取的金额（元，>=0） */
  withdraw_fixed_amount?: number;
  /** 每月提现开关：当月不盈利（已实现盈亏 <= 0）则不提取；仅 monthly 模式生效 */
  withdraw_skip_when_no_profit?: boolean;
  /** Armed 量价背离缩量项口径：annualized=线性年化（默认）/debiased=实测去偏/cumulative=旧口径（回滚） */
  risk_volume_caliber?: string;
  /** 缩量阈值：折算后的当日量能低于「前 5 日全天均量 × 本比例」即视为缩量（小数，0.8 = 80%） */
  risk_volume_ratio?: number;
  /** 16 笔金额的规模口径：fixed=绝对金额（默认，向后兼容）/
   *  equity=按「(当前权益+已提现)/初始本金」等比缩放，账户增长后同步放大仓位、回撤时同步缩小 */
  installment_scale_mode?: 'fixed' | 'equity';
  /** equity 口径下的放大倍数下限（小数，0.5 = 权益跌到初始本金一半以下也至少按半仓规模下注） */
  installment_scale_floor?: number;
  /** 闲置资金逆回购（每日盘后结算）：空仓/未占用现金每日模拟国债逆回购收益 */
  idle_cash_repo_enabled?: boolean;
  /** 逆回购年化收益率下限（小数，0.010 = 1.00%）；校验 0 <= min <= max <= 0.1 */
  repo_yield_min?: number;
  /** 逆回购年化收益率上限（小数，0.015 = 1.50%）；上限最多 10% 防误填 */
  repo_yield_max?: number;
}

export type StrategyDefaults = Partial<StrategyConfig> & {
  config?: Partial<StrategyConfig>;
  fees?: FeeConfig;
  initial_cash?: number;
};

/** 回测页表单草稿：服务端持久化，进入回测页时优先恢复（而不是重置为默认值）。 */
export interface BacktestConfigDraft {
  mode?: 'single' | 'funnel';
  name?: string;
  initial_cash?: number;
  start_date?: string | null;
  end_date?: string | null;
  dataset_id?: string | null;
  intraday_dataset_id?: string | null;
  index_dataset_id?: string | null;
  config?: Partial<StrategyConfig>;
  fees?: FeeConfig;
  screener?: Partial<ScreenerConfig>;
}

export interface BacktestConfigDraftResponse {
  draft: BacktestConfigDraft | null;
  updated_at: string | null;
}

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

/** 后台回测作业请求：single 模式必须带 dataset_id，funnel 模式必须带非空 screener.boards。 */
export interface BacktestJobRequest {
  mode: 'single' | 'funnel';
  name: string;
  dataset_id?: string;
  intraday_dataset_id?: string;
  index_dataset_id?: string;
  initial_cash: number;
  start_date?: string;
  end_date?: string;
  config: Partial<StrategyConfig>;
  fees?: FeeConfig;
  screener?: Partial<ScreenerConfig>;
}

/** 后台回测作业视图（GET /backtests/jobs/{id}）。progress 为 0–100 浮点，cursor_date 为最近完成的交易日。 */
export type BacktestJobStatus = 'RUNNING' | 'COMPLETED' | 'CANCELLED' | 'INTERRUPTED' | 'FAILED';

export interface BacktestJobView {
  id: string;
  name: string;
  status: BacktestJobStatus;
  progress: number;
  cursor_date: string | null;
  total_days: number | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

/** 选股日 L4 候选前三（仅新报告写入；旧报告为 undefined）。 */
export interface RotationCandidate {
  code: string;
  name: string;
  board: string;
  chg20d: number;
  score: number;
  deep_checked: boolean;
}

/** 板块大盘门快照（L0）：ret20 < 开门阈值才开门。 */
export interface RotationGateBoard {
  ret20?: number | null;
  open?: boolean;
  bypass?: boolean;
}

export interface RotationGate {
  open?: boolean;
  boards?: Record<string, RotationGateBoard>;
}

/** 单轮换票轨迹（漏斗轮动回测 data_info.rotation_trail 元素；读取端点会补派生字段）。 */
export interface RotationTrailItem {
  round: string | null;
  symbol: string | null;
  name: string;
  board: string;
  select_date: string;
  entry_date: string | null;
  exit_date: string | null;
  pnl: number | null;
  chg20d_at_select: number | null;
  gate_open: boolean;
  status: 'CLOSED' | 'OPEN' | 'WAITING' | 'BETA' | string;
  /** 本轮逐股决策日志（为什么开门/关门/空仓/深度超跌旁路几只），挂在对应选股日。 */
  screen_warnings?: string[];
  /** 本轮漏斗计数 universe→L0→L1→L2→L3→eligible。 */
  funnel?: Record<string, number>;
  /* ---------- 读取端点派生 / 新报告增强（旧报告可能缺省） ---------- */
  /** ROUND = 轮次行；WAITING = 原始逐日空仓；WAITING_SPAN = 合并后的空仓区间；BETA = ETF beta 腿持仓日；BETA_SPAN = 合并后的 ETF 持仓区间。 */
  kind?: 'ROUND' | 'WAITING' | 'WAITING_SPAN' | 'BETA' | 'BETA_SPAN' | string;
  /** 本轮收益率 = 净盈亏 / 峰值投入（资金效率口径）。 */
  return_rate?: number | null;
  /** 持仓交易日数（含首尾）。 */
  holding_days?: number | null;
  /** 选股日 → 建仓日相差的交易日数。 */
  entry_delay_days?: number | null;
  close_reason?: string | null;
  max_invested?: number | null;
  t_net_profit?: number | null;
  /** 轮次盈亏分腿（读时派生，旧报告可能缺省）：个股腿 / ETF 腿 / 轮内提现。 */
  stock_pnl?: number | null;
  beta_pnl?: number | null;
  withdraw_in_round?: number | null;
  /** 选股日候选前三（按 Chg20D 升序），仅选中标的的轮次行有。 */
  candidates?: RotationCandidate[];
  /** 各板块 ret20 / 开门状态，解释「为什么空仓」。 */
  gate?: RotationGate | null;
  /* ---------- 空仓区间行（kind=WAITING_SPAN） ---------- */
  start_date?: string;
  end_date?: string;
  days?: number;
  reason?: 'gate_closed' | 'no_candidate' | string;
  /** 空仓时漏斗卡在哪一层：L1 / L2 / L3 / L4。 */
  bottleneck?: string | null;
  /* ---------- ETF beta 腿持仓行（kind=BETA / BETA_SPAN） ---------- */
  /** 持仓的 ETF 标的（如 sz.159915）；个股空仓期间 beta 腿持有不算空仓。 */
  beta_symbol?: string | null;
  /* ---------- ETF beta 持仓区间行（kind=BETA_SPAN）读时派生 ---------- */
  /** 区间内 beta 买入成本（含费），作收益率分母；旧报告无逐笔 fills 时为 null。 */
  beta_invested?: number | null;
  /** 区间内 beta 买入笔数。 */
  beta_buy_count?: number | null;
  /** 区间内 beta 卖出笔数。 */
  beta_sell_count?: number | null;
  /* ---------- 轮次行（kind=ROUND）止盈执行诊断 ---------- */
  /** 清仓信号触发类型（价格轨止盈/账户轨止盈/量价背离/VWAP回撤/超期强平/事件熔断）。 */
  close_signal_trigger?: string | null;
  /** 清仓信号价 = 判定清仓那根 K 线的 close。 */
  close_signal_price?: number | null;
  /** 清仓信号时间（决策 K 线时间戳）。 */
  close_signal_time?: string | null;
  /** 信号价对应的价格轨收益（信号时点浮盈）。 */
  close_signal_return?: number | null;
  /** 实际成交价 = 出场 bar 的成交均价。 */
  close_fill_price?: number | null;
  /** 实际成交时间。 */
  close_fill_time?: string | null;
  /** 执行落差 = 成交价 / 信号价 - 1（负 = 出场比信号价低）。 */
  close_execution_gap?: number | null;
  /** 轮次账户收益 = pnl / max_invested。 */
  close_round_return?: number | null;
  /** 总落差 = 轮次收益 - 信号收益（负 = 落袋比信号少）。 */
  close_return_gap?: number | null;
}

/** 轮次摘要（汇总里的最佳/最差轮）。 */
export interface RotationRoundDigest {
  round: string | null;
  symbol: string | null;
  name: string;
  pnl: number | null;
  return_rate: number | null;
  entry_date: string | null;
  exit_date: string | null;
  holding_days: number | null;
  close_reason: string | null;
}

/** 换票轨迹汇总（GET /backtests/{id}/rotation-trail 的 summary）。 */
export interface RotationTrailSummary {
  screens: number;
  rounds: number;
  closed_rounds: number;
  open_rounds: number;
  waiting_days: number;
  gate_closed_days: number;
  no_candidate_days: number;
  no_candidate_by_stage: Record<string, number>;
  waiting_ratio: number;
  trading_days: number;
  holding_days: number;
  holding_ratio: number;
  /** ETF beta 腿持仓天数（个股空仓期间 beta 持有，不算空仓）。 */
  beta_holding_days?: number;
  /** ETF beta 腿已实现盈亏。 */
  beta_pnl?: number | null;
  /** 被覆盖（个股或 ETF）总交易日。 */
  covered_days?: number;
  /** 被覆盖（个股或 ETF）交易日 / 回测交易日数。 */
  covered_ratio?: number;
  win_rate: number;
  avg_return: number | null;
  avg_pnl: number | null;
  total_pnl: number;
  avg_holding_days: number | null;
  avg_entry_delay_days: number | null;
  best: RotationRoundDigest | null;
  worst: RotationRoundDigest | null;
  longest_wait: {
    start_date: string | null;
    end_date: string | null;
    days: number;
    gate_open: boolean;
    reason: string | null;
    bottleneck: string | null;
  } | null;
  exit_reasons: Record<string, number>;
}

/** 换票轨迹分页响应：统一分页信封 + 汇总块。 */
export interface RotationTrailPage extends Paginated<RotationTrailItem> {
  summary: RotationTrailSummary;
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

/** 成交明细分面计数：键为原始枚举（大写），仅 trades 明细端点返回。 */
export interface RecordFacets {
  purpose?: Record<string, number>;
  side?: Record<string, number>;
}

/** 明细分页响应：统一信封 + 可选 facets（筛选下拉展示条数用）。 */
export interface RecordsPage<T> extends Paginated<T> {
  facets?: RecordFacets;
}

/** 闲置资金逆回购逐日计提明细一行（GET /backtests/{id}/repo-daily 的 items 元素）。 */
export interface RepoDaily {
  /** 计提日（YYYY-MM-DD）。 */
  date: string;
  /** 当日计提的闲置资金（元）。 */
  idle_cash: number;
  /** 年化收益率（小数，如 0.0123 = 1.23%）。 */
  rate: number;
  /** 当日收益（元）。 */
  income: number;
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

/** 回测报告「每月盈亏」：按自然月汇总的账户权益变动（后端 monthly_breakdown 口径）。 */
export interface MonthlyPnl {
  /** 自然月，格式 YYYY-MM。 */
  month: string;
  /** 基期权益（首月为初始资金，其余为上月末权益）。 */
  start_equity: number;
  /** 当月末权益。 */
  end_equity: number;
  /** 当月盈亏（含未实现盈亏与手续费）。 */
  pnl: number;
  /** 当月收益率 = pnl / start_equity。 */
  return_rate: number;
  /** 当月实际回测交易日数。 */
  trading_days: number;
  /** 月内最大回撤（正数，0 表示无回撤）。 */
  max_drawdown: number;
  /** 当月末累计收益率（相对初始资金）。 */
  cumulative_return: number;
  /** 当月闲置资金逆回购收益合计（元）；仅开启逆回购的新报告有值，旧报告为 undefined。 */
  repo_income?: number;
}

/** 回撤深度分桶：某深度区间的水下交易日数，及其占水下日 / 占全部交易日的比例。 */
export interface DepthBucket {
  /** 落在该深度区间的水下交易日数。 */
  days: number;
  /** 占全部水下交易日的比例（0–1，四档合计 ≈ 1）。 */
  pct_of_underwater: number;
  /** 占全部交易日的比例（0–1，四档合计 ≈ 水下占比 underwater_ratio）。 */
  pct_of_trading: number;
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
  /**
   * 选股胜率：用轮次「个股腿」盈亏（stock_pnl，已剔除 ETF beta 腿）判胜负。
   * 顶层 win_rate 用 pnl（含 ETF），ETF 跨轮盯市盈亏会污染胜负判定——本字段剥掉该干扰。
   * 旧报告轮次缺 stock_pnl 时为 null，前端降级隐藏。
   */
  stock_win_rate?: number | null;
  /**
   * ETF 交易回合胜率：ETF beta 腿自己的每一笔平仓（sell）按已实现盈亏判胜负的占比。
   * 仅启用 ETF 的新报告有值；与选股胜率分母/口径不同（按 ETF 回合 vs 按个股轮次）。
   */
  beta_win_rate?: number | null;
  beta_sell_rounds?: number | null;
  beta_sell_wins?: number | null;
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
  /** 年化收益率「买入持有」三基准对比（沪深300 / 中证500 / 半仓双创）；旧报告缺省时前端回退 buy_hold_return。 */
  buy_hold_benchmarks?: { label: string; symbol: string; return: number }[];
  /** 盈利提现：累计已提现金额（真实离开账户的资金）与提现笔数。 */
  total_withdrawn?: number;
  withdrawn_count?: number;
  /** 盈利出金金额：提现里以盈利为基准（period_profit > 0）的提取额；已计入净利/总收益口径 */
  profit_withdrawn?: number;
  /** 每月盈亏。旧报告由后端读取时按权益曲线补算；极端情况下缺失则前端隐藏该面板。 */
  monthly?: MonthlyPnl[];
  /** 资金加权年化（XIRR）：把盈利出金当成回流现金流；无符号变化现金流时为 null，前端隐藏。 */
  xirr_annualized?: number | null;
  /** 闲置资金逆回购总收益（元，Σ equity_curve.repo_income）；仅开启逆回购的新报告有值，旧报告为 undefined。 */
  repo_income_total?: number;
  /** 稳健性版本：剔除最佳自然月 + 最大 3 笔盈利轮次后重算，判断收益是否靠少数极端项撑起。 */
  robustness?: {
    excluded_best_month_pnl: number;
    excluded_top3_win_pnl: number;
    net_profit: number;
    total_return: number;
    annualized_return: number;
  } | null;
  /** 净值曲线水下时长：最长连续低于此前峰值的区间 + 水下交易日占比 + 当前是否仍处水下。 */
  underwater?: {
    longest_underwater_days: number;
    longest_underwater_calendar_days: number;
    longest_underwater_start: string | null;
    longest_underwater_end: string | null;
    underwater_days: number;
    underwater_ratio: number;
    currently_underwater: boolean;
    current_drawdown_days: number;
    /**
     * 回撤深度分布：把水下日按回撤深度（≤2% / 2–5% / 5–10% / >10%）拆开的天数与占比。
     * 「水下占比高」可能只是长期在峰值附近浅晃，也可能是深度回撤——这组分布把两者区分开。
     * 旧报告缺失时前端降级隐藏。
     */
    drawdown_depth_distribution?: {
      lte_2pct: DepthBucket;
      '2_to_5pct': DepthBucket;
      '5_to_10pct': DepthBucket;
      gt_10pct: DepthBucket;
    } | null;
  } | null;
  /**
   * 现金流调整（TWR，时间加权收益率）口径的收益/风险指标。
   * 顶层旧口径把「盈利提现」当成亏损，会压低夏普/Calmar、夸大回撤；
   * 本块按 TWR 修正后重算，旧报告缺失时前端回退顶层字段。
   */
  cashflow_adjusted?: {
    annualized_return: number;
    total_return: number;
    volatility: number;
    sharpe_ratio: number;
    max_drawdown: number;
    calmar_ratio: number;
    underwater?: ReportMetrics['underwater'];
  };
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
  /** 轮次盈亏分腿：个股腿真实盈亏 = pnl + 轮内提现 - ETF 腿（新报告 / 读时派生）。 */
  stock_pnl?: number;
  /** 轮内 ETF beta 腿盈亏（含未实现）。 */
  beta_pnl?: number;
  /** 轮次区间 (start_date, end_date] 内的提现合计。 */
  withdraw_in_round?: number;
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

/** 报告快照里的配置：策略字段 + 回测边界与数据集选择（供「带入回测」恢复表单）。 */
export interface ReportConfig extends StrategyConfig {
  initial_cash?: number;
  start_date?: string | null;
  end_date?: string | null;
  dataset_id?: string | null;
  intraday_dataset_id?: string | null;
  index_dataset_id?: string | null;
  screener?: ScreenerConfig;
}

export interface Report {
  id: string;
  name: string;
  status: string;
  created_at: string;
  config: ReportConfig;
  metrics: ReportMetrics;
  equity_curve: EquityPoint[];
  /**
   * 成交 / 委托 / 轮次 / 事件 明细已改为后台分页下发：主报告只带 `*_total` 条数，
   * 全量数组走 GET /backtests/{id}/records/{kind}。字段保留为可选以兼容旧缓存报告。
   * events 前端当前不渲染，但同样可能上万条，主报告一并瘦身只留 events_total。
   */
  trades?: Trade[];
  orders?: Order[];
  rounds?: Round[];
  trades_total?: number;
  orders_total?: number;
  rounds_total?: number;
  events?: BacktestEvent[];
  events_total?: number;
  /** 盈利提现明细（后台分页下发，主报告只带 withdrawals_total）。 */
  withdrawals?: Withdrawal[];
  withdrawals_total?: number;
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
  /** ETF beta 腿持仓（份）；旧后端不返回时为 undefined。 */
  beta_qty?: number;
  /** ETF beta 腿成本（含买入费用）。 */
  beta_cost?: number;
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

/** 一笔盈利提现记录：从账户现金中取出已实现盈利（真实资金离场）。 */
export interface Withdrawal {
  id: string;
  /** 扣款所在交易日（YYYY-MM-DD）。 */
  date: string;
  /** 实际提取金额（已按可提现金封顶，永不为负）。 */
  amount: number;
  /** monthly / per_trade。 */
  mode: 'monthly' | 'per_trade' | string;
  /** ratio / fixed。 */
  method: 'ratio' | 'fixed' | string;
  /** 当期已实现盈亏基准（提取比例的基数）。 */
  period_profit: number;
  /** per_trade=来源成交 id；monthly=基准月份（如 2026-01）。 */
  trigger: string;
  /** 提现后账户现金余额。 */
  cash_after: number;
}

export interface AccountState {
  id: string;
  name: string;
  /** 账户创建时间（读时派生指标的时间起点）。 */
  created_at?: string;
  /** created_at 是否为「旧账户回填的推定值」（旧 payload 没有创建时间）。 */
  created_at_estimated?: boolean;
  /** 当前轮入口日（来自 strategy_state 的只读投影）；空仓/未开始一轮时为 null。 */
  round_entry_date?: string | null;
  initial_cash: number;
  cash: number;
  available_cash: number;
  equity: number;
  market_value: number;
  realized_pnl: number;
  total_fees: number;
  /** 盈利提现：累计已提现金额（真实离开账户的资金）。 */
  total_withdrawn: number;
  /** 盈利提现明细（日期/金额/方式/触发来源）。 */
  withdrawals: Withdrawal[];
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

/**
 * 模拟盘读时派生指标（`GET /api/paper/accounts/{id}/metrics`）。
 *
 * 只含**不依赖逐日净值序列**的字段；回撤 / 夏普 / 波动 / Calmar / 水下 / 敞口 /
 * 空仓占比 / 每月盈亏等要等逐日净值落库后再补。
 */
export interface PaperMetrics {
  initial_cash: number;
  final_equity: number;
  total_fees: number;
  t_net_profit: number;
  total_withdrawn: number;
  withdrawn_count: number;
  /** 盈利出金：提现明细里 period_profit > 0 的金额之和（已加回净利/总收益）。 */
  profit_withdrawn: number;
  /** 净利 = equity + profit_withdrawn − initial_cash（与回测同口径）。 */
  net_profit: number;
  total_return: number;
  total_trades: number;
  t_trade_count: number;
  /** ETF beta 腿已实现盈亏（BETA 卖出 realized_pnl 求和）。 */
  beta_pnl: number;
  beta_trade_count: number;
  /** 永久仓成本峰值（按成交流水重放成本账本）。 */
  max_invested: number;
}

export interface PaperMetricsResponse {
  metrics: PaperMetrics;
  /** 数据来源口径标记：当前为 cashflow（只吃成交/委托/提现流水）。 */
  derived_from: string;
  note?: string;
}

/** 一笔 ETF beta 腿成交（与回测 data_info.beta.fills 同键）。 */
export interface PaperBetaFill {
  date: string;
  side: OrderSide | string;
  quantity: number;
  price: number;
  fee: number;
  realized_pnl: number;
}

/** ETF beta 腿摘要（`GET /api/paper/accounts/{id}/beta`）：与回测 data_info.beta 同键同口径。 */
export interface PaperBetaSummary {
  enabled: boolean;
  symbol: string;
  trade_count: number;
  buy_count: number;
  sell_count: number;
  entry_date: string | null;
  last_exit_date: string | null;
  exit_reason: string | null;
  pnl: number;
  position_qty: number;
  fills: PaperBetaFill[];
}

export interface PaperBetaResponse {
  beta: PaperBetaSummary;
  derived_from: string;
}

/**
 * 模拟盘轮次明细（`GET /api/paper/accounts/{id}/rounds`）：与回测 `rounds` 同构。
 *
 * 轮次按「净个股持仓（PERMANENT+T，不含 BETA）0→正→0」读时重建；pnl 用逐日
 * 净值差（无净值快照时为 null，不近似硬算）；三腿口径与回测 `decompose_round_pnl`
 * 同一份实现（pnl = stock_pnl + beta_pnl - withdraw_in_round）。
 */
export interface PaperRoundCloseDiagnostic {
  trigger?: string | null;
  trigger_label?: string | null;
  signal_price?: number | null;
  signal_time?: string | null;
  price_return?: number | null;
  fill_price?: number | null;
  fill_time?: string | null;
  execution_gap?: number | null;
  round_return?: number | null;
  return_gap?: number | null;
}

export interface PaperRound {
  id: string;
  symbol: string;
  name: string;
  start_date: string;
  end_date: string | null;
  entry_date: string;
  exit_date: string | null;
  status: 'CLOSED' | 'OPEN' | string;
  /** PERMANENT 买入笔数（轮内加仓次数）。 */
  installment_count: number;
  invested: number;
  max_invested: number;
  t_net_profit: number;
  pnl: number | null;
  /** pnl 是否近似（净值序列未覆盖轮次起点 / 盘中清仓当日无快照时为 true）。 */
  approx: boolean;
  stock_pnl: number | null;
  beta_pnl: number | null;
  withdraw_in_round: number;
  round_return: number | null;
  holding_days: number | null;
  /** 原始退出原因（策略 / 委托 reason）。 */
  close_reason: string | null;
  /** 退出原因中文标签（与回测报告文案对齐）。 */
  close_reason_label: string | null;
  close_signal_trigger?: string | null;
  close_signal_trigger_label?: string | null;
  close_signal_price?: number | null;
  close_signal_time?: string | null;
  close_signal_return?: number | null;
  close_fill_price?: number | null;
  close_fill_time?: string | null;
  close_execution_gap?: number | null;
  close_round_return?: number | null;
  close_return_gap?: number | null;
  close_diagnostic?: PaperRoundCloseDiagnostic | null;
}

/** 模拟盘策略指标汇总（`GET /api/paper/accounts/{id}/rounds/summary`）。 */
export interface PaperStrategyMetrics {
  total_rounds: number;
  closed_rounds: number;
  win_rate: number;
  profit_factor: number | null;
  avg_round_return: number | null;
  avg_holding_days: number | null;
  t_net_profit: number;
  t_trade_count: number;
  beta_pnl: number;
  beta_trade_count: number;
  max_invested: number;
  avg_max_invested: number | null;
  installment_avg: number | null;
  installment_max: number;
  installment_histogram: Record<string, number>;
  close_reason_distribution: Record<string, number>;
}

export interface PaperStrategyMetricsResponse {
  strategy_metrics: PaperStrategyMetrics;
  derived_from: string;
}

/** 单个策略开关（来自账户 config 快照，不回读全局配置）。 */
export interface PaperSwitchItem {
  key: string;
  label: string;
  enabled: boolean;
  /** 已格式化的展示值（百分比 / 笔数 / 时间 / 金额 / 文本）。 */
  value: string;
  group: string;
  hint: string;
}

export interface PaperSwitchesResponse {
  switches: Record<string, PaperSwitchItem[]>;
  /** 配置快照时间（账户创建时间）；后端旧版本可能为 null。 */
  config_snapshot_at: string | null;
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

/* ---------- 应用级登录（替代 nginx HTTP Basic 认证） ---------- */

/** POST /api/auth/login 请求体；remember_me=true 签发 7 天令牌，否则 12 小时。 */
export interface AuthLoginRequest {
  username: string;
  password: string;
  remember_me: boolean;
}

/** POST /api/auth/login 响应：令牌 + 用户名 + 过期时间（ISO 字符串）。 */
export interface AuthLoginResponse {
  token: string;
  username: string;
  expires_at: string;
}

/** GET /api/auth/me 响应：当前令牌归属用户名。 */
export interface AuthMeResponse {
  username: string;
}
