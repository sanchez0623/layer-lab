import type {
  AccountState,
  BacktestJobRequest,
  BacktestJobView,
  BacktestRequest,
  BacktestSummary,
  CreateAccountRequest,
  CreateOrderRequest,
  Dataset,
  DemoDatasets,
  FeeConfig,
  FunnelBacktestRequest,
  FeeConfigResponse,
  HealthResponse,
  MarketFetchRequest,
  MarketFetchResponse,
  MarketQuote,
  MarketSourceStatus,
  Order,
  Paginated,
  PrepareResponse,
  RefreshStatus,
  Report,
  RotationRequest,
  RotationTrailItem,
  ScreenerCoverageResponse,
  ScreenerDefaults,
  ScreenerPrepareRequest,
  ScreenerRefreshRequest,
  ScreenerRefreshResponse,
  ScreenerRunRequest,
  ScreenerRunSummary,
  ScreeningRun,
  SchedulerStatus,
  StrategyConfig,
  StrategyDefaults,
  Timeframe,
  TradingRulesAllResponse,
  TradingRulesResponse,
  TradingSession,
} from '../types';

// 部署可配置：
// - 默认走同源 `/api`：生产由容器内 nginx 反代到 127.0.0.1:8010；
//   dev server 通过 vite.config.ts 的 proxy 把 `/api` 转发到 127.0.0.1:8010。
// - 仍可用 VITE_API_BASE 覆盖（例如临时指向其他后端）。
//   不设默认 127.0.0.1:8010，避免误构建的 dist 发到线上后所有远程用户 API 指向自己电脑。
//   HTTPS 站点若写死 http://IP:端口 会被浏览器当 mixed content 拦截，故部署一律用同源相对路径。
export const API_BASE = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '');

export class ApiError extends Error {
  status: number;
  isNetworkError: boolean;

  constructor(message: string, status = 0, isNetworkError = false) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.isNetworkError = isNetworkError;
  }
}

async function parseError(response: Response): Promise<string> {
  try {
    const body = await response.json() as { detail?: string | Array<{ msg?: string }>; message?: string };
    if (Array.isArray(body.detail)) return body.detail.map((item) => item.msg).filter(Boolean).join('；');
    return body.detail || body.message || `请求失败（HTTP ${response.status}）`;
  } catch {
    return `请求失败（HTTP ${response.status}）`;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        ...init?.headers,
      },
    });
  } catch {
    throw new ApiError(`无法连接后端服务。请确认服务已启动，并检查 API 地址：${API_BASE}`, 0, true);
  }

  if (!response.ok) throw new ApiError(await parseError(response), response.status);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function query(params: Record<string, string | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value) search.set(key, value);
  });
  const value = search.toString();
  return value ? `?${value}` : '';
}

export const api = {
  health: () => request<HealthResponse>('/health'),
  strategyDefaults: () => request<StrategyDefaults>('/strategy/defaults'),
  // 分板块 A 股交易规则：下单页按 symbol 拉取，驱动快捷档位 / 默认数量 / 实时校验。
  tradingRules: (symbol: string) =>
    request<TradingRulesResponse>(`/trading-rules/${encodeURIComponent(symbol)}`),
  tradingRulesAll: () => request<TradingRulesAllResponse>('/trading-rules'),
  fees: () => request<FeeConfigResponse>('/fees'),
  saveFees: (fees: FeeConfig) => request<FeeConfigResponse>('/fees', { method: 'PUT', body: JSON.stringify(fees) }),
  resetFees: () => request<FeeConfigResponse>('/fees/reset', { method: 'POST' }),
  datasets: () => request<Dataset[]>('/datasets'),
  marketSources: () => request<MarketSourceStatus>('/market/sources'),
  fetchMarketData: (payload: MarketFetchRequest) =>
    request<MarketFetchResponse>('/market/fetch', { method: 'POST', body: JSON.stringify(payload) }),
  marketQuote: (symbol: string) => request<MarketQuote>(`/market/quote/${encodeURIComponent(symbol)}`),
  createDemoDatasets: () => request<DemoDatasets>('/datasets/demo', { method: 'POST' }),
  uploadDataset: (file: File, symbol: string, name: string, timeframe: Timeframe) => {
    const body = new FormData();
    body.append('file', file);
    return request<Dataset>(`/datasets/upload${query({ symbol, name, timeframe })}`, { method: 'POST', body });
  },
  runBacktest: (payload: BacktestRequest) => request<Report>('/backtests', { method: 'POST', body: JSON.stringify(payload) }),
  // 漏斗轮动回测：每次清仓后按选股漏斗重新选票，与模拟盘 rotation v2 语义一致。
  runFunnelBacktest: (payload: FunnelBacktestRequest) => request<Report>('/backtests/funnel', { method: 'POST', body: JSON.stringify(payload) }),
  backtests: () => request<BacktestSummary[]>('/backtests'),
  backtest: (id: string) => request<Report>(`/backtests/${encodeURIComponent(id)}`),
  /* ---------- 后台回测作业（进度 / 取消 / 续跑） ----------
     复用 request<T>，409（已有回测在跑）等错误会抛 ApiError 且带 .status，由调用方识别「忙」。 */
  runBacktestJob: (payload: BacktestJobRequest) =>
    request<{ job_id: string; status: string }>('/backtests/jobs', { method: 'POST', body: JSON.stringify(payload) }),
  backtestJobProgress: (jobId: string) =>
    request<BacktestJobView>(`/backtests/jobs/${encodeURIComponent(jobId)}`),
  backtestJobList: (limit?: number) =>
    request<{ jobs: BacktestJobView[] }>(`/backtests/jobs${query({ limit: limit === undefined ? undefined : String(limit) })}`),
  cancelBacktestJob: (jobId: string) =>
    request<{ job_id: string; status: string }>(`/backtests/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }),
  resumeBacktestJob: (jobId: string) =>
    request<{ job_id: string; status: string; resumed: boolean }>(`/backtests/jobs/${encodeURIComponent(jobId)}/resume`, { method: 'POST' }),
  // 换票轨迹后台分页：主报告已瘦身不含全量 trail，列表按页拉取。
  backtestRotationTrail: (id: string, page = 1, pageSize = 20) =>
    request<Paginated<RotationTrailItem>>(
      `/backtests/${encodeURIComponent(id)}/rotation-trail${query({ page: String(page), page_size: String(pageSize) })}`,
    ),
  createAccount: (payload: CreateAccountRequest) => request<AccountState>('/paper/accounts', { method: 'POST', body: JSON.stringify(payload) }),
  currentAccount: () => request<AccountState>('/paper/accounts/current'),
  account: (id: string) => request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}`),
  resetAccount: (id: string) => request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}/reset`, { method: 'POST' }),
  applyAccountFees: (id: string, fees: FeeConfig) =>
    request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}/fees`, { method: 'POST', body: JSON.stringify({ fees }) }),
  setAutoStrategy: (id: string, enabled: boolean) =>
    request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}/auto-strategy`, { method: 'POST', body: JSON.stringify({ enabled }) }),
  /** 局部更新模拟账户策略配置（非费率）；下一根K线起的新操作立刻生效。 */
  updateAccountConfig: (id: string, patch: Partial<Pick<StrategyConfig, 'enable_t' | 't_target_net_spread' | 't_exit_cutoff'>>) =>
    request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}/config`, { method: 'POST', body: JSON.stringify(patch) }),
  // 后端 domain 枚举为大写（BUY/SELL、MARKET/LIMIT），前端 UI 状态为小写；
  // 统一在 API 边界上行归一化，避免 Pydantic Literal 校验返回 422。
  placeOrder: (accountId: string, payload: CreateOrderRequest) =>
    request<{ order: Order; account: AccountState }>(
      `/paper/accounts/${encodeURIComponent(accountId)}/orders`,
      {
        method: 'POST',
        body: JSON.stringify({
          ...payload,
          side: payload.side.toUpperCase(),
          order_type: payload.order_type.toUpperCase(),
        }),
      }
    ),
  // 本地单号 O00000001 在每个账户内都会复用，撤单必须带上账户 ID 才能定位。
  cancelOrder: (accountId: string, orderId: string | number) =>
    request<AccountState>(
      `/paper/accounts/${encodeURIComponent(accountId)}/orders/${encodeURIComponent(String(orderId))}/cancel`,
      { method: 'POST' }
    ),
  advance: (accountId: string, bars: number) =>
    request<AccountState>(`/paper/accounts/${encodeURIComponent(accountId)}/advance`, { method: 'POST', body: JSON.stringify({ bars }) }),
  liveTick: (accountId: string, symbol: string) =>
    request<AccountState>(`/paper/accounts/${encodeURIComponent(accountId)}/live-tick`, { method: 'POST', body: JSON.stringify({ symbol }) }),
  // 后端定时器状态：前端轮询它来显示「最近一次自动成交推进」时间。
  scheduler: () => request<SchedulerStatus>('/paper/scheduler'),
  // 交易日/交易时段：周末、节假日前端不轮询。
  tradingSession: () => request<TradingSession>('/trading/session'),

  /* ---------- 选股漏斗（契约 §5.4 / §6.1） ---------- */
  screenerDefaults: () => request<ScreenerDefaults>('/screener/defaults'),
  screenerCoverage: () => request<ScreenerCoverageResponse>('/screener/coverage'),
  screenerRefresh: (payload: ScreenerRefreshRequest = {}) =>
    request<ScreenerRefreshResponse>('/screener/refresh', { method: 'POST', body: JSON.stringify(payload) }),
  screenerRefreshStatus: () => request<RefreshStatus>('/screener/refresh'),
  runScreener: (payload: ScreenerRunRequest = {}) =>
    request<ScreeningRun>('/screener/run', { method: 'POST', body: JSON.stringify(payload) }),
  screenerRuns: (limit = 20) => request<ScreenerRunSummary[]>(`/screener/runs${query({ limit: String(limit) })}`),
  screenerRun: (id: string) => request<ScreeningRun>(`/screener/runs/${encodeURIComponent(id)}`),
  deleteScreenerRun: (id: string) =>
    request<{ deleted: boolean }>(`/screener/runs/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  prepareScreenerRun: (id: string, payload: ScreenerPrepareRequest) =>
    request<PrepareResponse>(`/screener/runs/${encodeURIComponent(id)}/prepare`, { method: 'POST', body: JSON.stringify(payload) }),
  setRotation: (accountId: string, payload: RotationRequest) =>
    request<AccountState>(`/paper/accounts/${encodeURIComponent(accountId)}/rotation`, { method: 'POST', body: JSON.stringify(payload) }),
};

export async function downloadBacktestCsv(id: string, kind: 'trades' | 'orders'): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/backtests/${encodeURIComponent(id)}/export?kind=${kind}`);
  } catch {
    throw new ApiError(`导出失败：无法连接 ${API_BASE}，请检查后端服务。`, 0, true);
  }
  if (!response.ok) throw new ApiError(await parseError(response), response.status);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `layerlab-${id}-${kind}.csv`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
