import type {
  AccountState,
  AuthLoginRequest,
  AuthLoginResponse,
  AuthMeResponse,
  BacktestConfigDraft,
  BacktestConfigDraftResponse,
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
  PaperBetaResponse,
  PaperMetricsResponse,
  PaperRound,
  PaperStrategyMetricsResponse,
  PaperSwitchesResponse,
  PrepareResponse,
  RefreshStatus,
  Report,
  RepoDaily,
  Round,
  RotationRequest,
  RotationTrailPage,
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
  Trade,
  Withdrawal,
  TradingRulesAllResponse,
  TradingRulesResponse,
  TradingSession,
  RecordsPage,
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

/* ---------- 鉴权令牌（应用级登录） ----------
   持久化策略：remember_me=true → localStorage（关浏览器仍保留，跨刷新保持会话）；
   否则 → sessionStorage（关浏览器即失效，对应「不记住密码」）。
   恢复会话时优先 localStorage、再 sessionStorage。 */
const AUTH_TOKEN_KEY = 'layerlab.authToken';

let currentToken: string | null = readStoredToken();

function readStoredToken(): string | null {
  try {
    return window.localStorage.getItem(AUTH_TOKEN_KEY) ?? window.sessionStorage.getItem(AUTH_TOKEN_KEY);
  } catch {
    return null;
  }
}

/** 取当前令牌（供请求附带 Bearer 头）。 */
export function getAuthToken(): string | null {
  return currentToken;
}

/** 保存令牌：remember=true 存 localStorage（跨刷新），否则存 sessionStorage（关浏览器失效）。 */
export function setAuthToken(token: string, remember: boolean): void {
  currentToken = token;
  try {
    if (remember) {
      window.localStorage.setItem(AUTH_TOKEN_KEY, token);
      window.sessionStorage.removeItem(AUTH_TOKEN_KEY);
    } else {
      window.sessionStorage.setItem(AUTH_TOKEN_KEY, token);
      window.localStorage.removeItem(AUTH_TOKEN_KEY);
    }
  } catch {
    /* 存储不可用时退化为内存令牌（本次会话有效）。 */
  }
}

/** 清除令牌（登出 / 收到 401）。 */
export function clearAuthToken(): void {
  currentToken = null;
  try {
    window.localStorage.removeItem(AUTH_TOKEN_KEY);
    window.sessionStorage.removeItem(AUTH_TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

/** 401 时派发，让 AuthContext 感知并跳转登录页。 */
function emitUnauthorized(): void {
  if (typeof window !== 'undefined') window.dispatchEvent(new Event('layerlab:unauthorized'));
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    const token = currentToken;
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...init?.headers,
      },
    });
  } catch {
    throw new ApiError(`无法连接后端服务。请确认服务已启动，并检查 API 地址：${API_BASE}`, 0, true);
  }

  if (response.status === 401) {
    // 令牌失效 / 过期：清本地凭证并广播，触发全局跳登录页。
    clearAuthToken();
    emitUnauthorized();
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
  // 回测页表单草稿：修改即保存到服务端，下次进页面自动恢复；DELETE = 恢复默认配置。
  backtestConfigDraft: () => request<BacktestConfigDraftResponse>('/backtest/config-draft'),
  saveBacktestConfigDraft: (draft: BacktestConfigDraft) =>
    request<BacktestConfigDraftResponse>('/backtest/config-draft', { method: 'PUT', body: JSON.stringify(draft) }),
  resetBacktestConfigDraft: () =>
    request<BacktestConfigDraftResponse>('/backtest/config-draft', { method: 'DELETE' }),
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
  // 回测报告列表：按 Report ID / 名称搜索 + 后台分页（列表只返回轻量摘要）。
  backtests: (params: { q?: string; page?: number; pageSize?: number } = {}) =>
    request<Paginated<BacktestSummary>>(`/backtests${query({
      q: params.q, page: params.page === undefined ? undefined : String(params.page),
      page_size: params.pageSize === undefined ? undefined : String(params.pageSize),
    })}`),
  backtest: (id: string) => request<Report>(`/backtests/${encodeURIComponent(id)}`),
  // 删除报告并级联清理同 ID 的后台任务 / 检查点；运行中任务后端返回 409。
  deleteBacktest: (id: string) =>
    request<{ deleted: boolean; id: string }>(`/backtests/${encodeURIComponent(id)}`, { method: 'DELETE' }),
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
  /** 放弃并彻底删除一个已到终态的作业（作业行 + 检查点）；刷新后不再弹「可续传」横幅。 */
  deleteBacktestJob: (jobId: string) =>
    request<{ job_id: string; status: string; deleted: boolean }>(`/backtests/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' }),
  resumeBacktestJob: (jobId: string) =>
    request<{ job_id: string; status: string; resumed: boolean }>(`/backtests/jobs/${encodeURIComponent(jobId)}/resume`, { method: 'POST' }),
  // 换票轨迹后台分页：主报告已瘦身不含全量 trail，列表按页拉取。
  // collapseWaiting=true 时后端把连续同因空仓合并成区间（面板表格默认用），
  // 响应附带 summary 汇总块；传 false 可拿原始逐日轨迹。
  backtestRotationTrail: (id: string, page = 1, pageSize = 20, collapseWaiting = true) =>
    request<RotationTrailPage>(
      `/backtests/${encodeURIComponent(id)}/rotation-trail${query({
        page: String(page), page_size: String(pageSize), collapse_waiting: String(collapseWaiting),
      })}`,
    ),
  // 成交明细 / 委托记录 / 已结束轮次 后台分页：主报告只带 *_total，明细按页拉取。
  // filters 可选：symbol（子串）/ purpose（枚举或中文标签）/ side（BUY|SELL）组合过滤；
  // 仅 trades 返回 facets（各枚举条数，供筛选下拉展示与禁用 0 条选项）。
  backtestRecords: <K extends 'trades' | 'orders' | 'rounds' | 'withdrawals'>(
    id: string, kind: K, page = 1, pageSize = 20,
    filters?: { symbol?: string; purpose?: string; side?: string },
  ) => {
    const params: Record<string, string> = { page: String(page), page_size: String(pageSize) };
    if (filters?.symbol) params.symbol = filters.symbol;
    if (filters?.purpose) params.purpose = filters.purpose;
    if (filters?.side) params.side = filters.side;
    return request<RecordsPage<{ trades: Trade; orders: Order; rounds: Round; withdrawals: Withdrawal }[K]>>(
      `/backtests/${encodeURIComponent(id)}/records/${kind}${query(params)}`,
    );
  },
  /** 闲置资金逆回购逐日计提明细（后台分页）：主报告不含逐日 repo，点月份行按月拉取。
   *  month 为 YYYY-MM 时只下发该自然月；不传返回全部计提日。旧报告 / 关闭逆回购 → 空 items。 */
  repoDaily: (id: string, params: { page: number; pageSize: number; month?: string }) =>
    request<Paginated<RepoDaily>>(
      `/backtests/${encodeURIComponent(id)}/repo-daily${query({
        page: String(params.page),
        page_size: String(params.pageSize),
        month: params.month,
      })}`,
    ),
  createAccount: (payload: CreateAccountRequest) => request<AccountState>('/paper/accounts', { method: 'POST', body: JSON.stringify(payload) }),
  currentAccount: () => request<AccountState>('/paper/accounts/current'),
  account: (id: string) => request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}`),
  // 读时派生指标：不含依赖逐日净值的字段；单独拉取，不进 15 秒轮询的账户 payload。
  accountMetrics: (id: string) =>
    request<PaperMetricsResponse>(`/paper/accounts/${encodeURIComponent(id)}/metrics`),
  // ETF beta 腿摘要：与回测 data_info.beta 同键同口径。
  accountBeta: (id: string) =>
    request<PaperBetaResponse>(`/paper/accounts/${encodeURIComponent(id)}/beta`),
  // 轮次明细：后台分页，行与回测 rounds 同构（含三腿分拆 + 止盈执行诊断）。
  accountRounds: (id: string, page = 1, pageSize = 20) =>
    request<Paginated<PaperRound>>(
      `/paper/accounts/${encodeURIComponent(id)}/rounds${query({
        page: String(page), page_size: String(pageSize),
      })}`,
    ),
  // 策略指标汇总（胜率 / 盈亏比 / 平均单轮 / 加仓与退出原因分布）：只吃轮次 + 成交。
  accountRoundsSummary: (id: string) =>
    request<PaperStrategyMetricsResponse>(`/paper/accounts/${encodeURIComponent(id)}/rounds/summary`),
  // 策略开关状态：来自账户自己的 config 快照（在途逻辑定格，不读全局配置）。
  accountSwitches: (id: string) =>
    request<PaperSwitchesResponse>(`/paper/accounts/${encodeURIComponent(id)}/switches`),
  resetAccount: (id: string) => request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}/reset`, { method: 'POST' }),
  applyAccountFees: (id: string, fees: FeeConfig) =>
    request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}/fees`, { method: 'POST', body: JSON.stringify({ fees }) }),
  setAutoStrategy: (id: string, enabled: boolean) =>
    request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}/auto-strategy`, { method: 'POST', body: JSON.stringify({ enabled }) }),
  /** 局部更新模拟账户策略配置（非费率）；下一根K线起的新操作立刻生效。 */
  updateAccountConfig: (id: string, patch: Partial<Pick<StrategyConfig, 'enable_t' | 't_target_net_spread' | 't_exit_cutoff' | 't_min_amplitude' | 't_min_turnover' | 't_max_turnover' | 't_min_expected_net_spread' | 't_min_base_entries' | 'max_holding_days' | 'holding_tp_override' | 'force_exit_holding_days' | 'add_tier_strict' | 't_deep_drop_pct' | 'etf_beta_enabled' | 'etf_symbol' | 'etf_position_pct' | 'etf_entry_ma_days' | 'etf_exit_ma_days' | 'etf_hard_stop_pct' | 'etf_entry_max_deviation' | 'etf_reentry_cooldown_days' | 'etf_beta_coexist' | 'withdraw_mode' | 'withdraw_method' | 'withdraw_ratio' | 'withdraw_fixed_amount' | 'withdraw_skip_when_no_profit' | 'risk_volume_caliber' | 'risk_volume_ratio' | 'idle_cash_repo_enabled' | 'repo_yield_min' | 'repo_yield_max'>>) =>
    request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}/config`, { method: 'POST', body: JSON.stringify(patch) }),
  /** 把回测配置整套应用到模拟账户（策略字段 config 已 strip 费率 + 费率 fees 万分之）；下一根K线起生效。 */
  applyAccountConfig: (id: string, payload: { config: Partial<StrategyConfig>; fees?: FeeConfig }) =>
    request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}/apply-config`, { method: 'POST', body: JSON.stringify(payload) }),
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

  /* ---------- 应用级登录（替代 nginx HTTP Basic 认证） ---------- */
  /** 登录：remember_me=true 签发 7 天令牌，否则 12 小时。注意用裸 fetch 以免把可能已失效的旧令牌带上。 */
  login: async (payload: AuthLoginRequest): Promise<AuthLoginResponse> => {
    let response: Response;
    try {
      response = await fetch(`${API_BASE}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    } catch {
      throw new ApiError(`无法连接后端服务。请确认服务已启动，并检查 API 地址：${API_BASE}`, 0, true);
    }
    if (!response.ok) throw new ApiError(await parseError(response), response.status);
    return response.json() as Promise<AuthLoginResponse>;
  },
  /** 登出：撤销当前令牌（需 Bearer）。 */
  logout: () => request<{ ok: boolean }>('/auth/logout', { method: 'POST' }),
  /** 校验当前令牌归属用户。 */
  me: () => request<AuthMeResponse>('/auth/me'),
  /** 修改当前用户密码（需 Bearer）。 */
  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ ok: boolean }>('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    }),
};

export async function downloadBacktestCsv(id: string, kind: 'trades' | 'orders' | 'trail'): Promise<void> {
  let response: Response;
  try {
    const token = currentToken;
    response = await fetch(`${API_BASE}/backtests/${encodeURIComponent(id)}/export?kind=${kind}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
  } catch {
    throw new ApiError(`导出失败：无法连接 ${API_BASE}，请检查后端服务。`, 0, true);
  }
  if (response.status === 401) {
    clearAuthToken();
    emitUnauthorized();
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
