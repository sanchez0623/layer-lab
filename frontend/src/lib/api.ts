import type {
  AccountState,
  BacktestRequest,
  BacktestSummary,
  CreateAccountRequest,
  CreateOrderRequest,
  Dataset,
  DemoDatasets,
  FeeConfig,
  FeeConfigResponse,
  HealthResponse,
  MarketFetchRequest,
  MarketFetchResponse,
  MarketQuote,
  MarketSourceStatus,
  Order,
  Report,
  StrategyDefaults,
  Timeframe,
} from '../types';

export const API_BASE = (import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8010/api').replace(/\/$/, '');

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
  backtests: () => request<BacktestSummary[]>('/backtests'),
  backtest: (id: string) => request<Report>(`/backtests/${encodeURIComponent(id)}`),
  createAccount: (payload: CreateAccountRequest) => request<AccountState>('/paper/accounts', { method: 'POST', body: JSON.stringify(payload) }),
  currentAccount: () => request<AccountState>('/paper/accounts/current'),
  account: (id: string) => request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}`),
  resetAccount: (id: string) => request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}/reset`, { method: 'POST' }),
  applyAccountFees: (id: string, fees: FeeConfig) =>
    request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}/fees`, { method: 'POST', body: JSON.stringify({ fees }) }),
  setAutoStrategy: (id: string, enabled: boolean) =>
    request<AccountState>(`/paper/accounts/${encodeURIComponent(id)}/auto-strategy`, { method: 'POST', body: JSON.stringify({ enabled }) }),
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
