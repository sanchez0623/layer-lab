export type PageKey = 'backtest' | 'paper' | 'data' | 'strategy';
export type Timeframe = 'daily' | 'tick' | '1m' | '5m';
export type OrderSide = 'buy' | 'sell';
export type OrderType = 'market' | 'limit';

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
  t_active_qty: number;
  last_price: number;
  market_value: number;
  float_pnl: number;
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
