export const money = (value?: number | null, digits = 2): string => {
  if (value === undefined || value === null || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency: 'CNY',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
};

export const number = (value?: number | null, digits = 2): string => {
  if (value === undefined || value === null || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
};

export const integer = (value?: number | null): string => {
  if (value === undefined || value === null || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value);
};

export const percent = (value?: number | null, digits = 2): string => {
  if (value === undefined || value === null || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
};

export const signedMoney = (value?: number | null): string => {
  if (value === undefined || value === null || !Number.isFinite(value)) return '—';
  return `${value > 0 ? '+' : ''}${money(value)}`;
};

export const signedPercent = (value?: number | null): string => {
  if (value === undefined || value === null || !Number.isFinite(value)) return '—';
  return `${value > 0 ? '+' : ''}${percent(value)}`;
};

export const shortDate = (value?: string | null): string => {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
};

export const dateTime = (value?: string | null): string => {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date);
};

/** 后端枚举返回大写、前端 UI 状态为小写，比较与查表前统一归一化。 */
export const lower = (value?: string | null): string => (value ?? '').toLowerCase();

export const sideLabel = (side?: string): string => {
  const value = lower(side);
  return value === 'buy' ? '买入' : value === 'sell' ? '卖出' : side || '—';
};
export const orderTypeLabel = (type?: string | null): string => {
  const value = lower(type);
  return value === 'market' ? '市价' : value === 'limit' ? '限价' : type || '—';
};
export const statusLabel = (status?: string): string => {
  const value = lower(status);
  return ({
    pending: '待成交', open: '待成交', partial: '部分成交', partially_filled: '部分成交', filled: '已成交',
    cancelled: '已撤单', canceled: '已撤单', rejected: '已拒绝', completed: '已完成', running: '运行中', failed: '失败',
    active: '运行中', closed: '已结束', idle: '空闲', armed: '已触发', exit: '退出中', off: '未触发',
  }[value] || status || '—');
};

export const purposeLabel = (purpose?: string): string => {
  const value = lower(purpose);
  return ({
    permanent: '永久仓', t: 'T仓', t_buy: 'T仓买入', t_sell: 'T仓卖出', open: '建仓', close: '清仓', take_profit: '止盈',
  }[value] || purpose || '—');
};

/** 标的代码输入清洗：只保留 6 位数字，自动剥离 sh./sz./.SH/.SZ 与空格等粘贴噪声。 */
export const cleanSymbolInput = (raw: string): string => {
  const text = raw.toUpperCase().replace(/[^0-9A-Z.]/g, '');
  const withoutPrefix = text.replace(/^(SH|SZ|BJ)\.?/, '');
  const withoutSuffix = withoutPrefix.replace(/\.(SH|SZ|BJ)$/, '');
  return withoutSuffix.replace(/\D/g, '').slice(0, 6);
};

/** 展示用：把后端标准代码 sh.688200 收敛为 6 位代码；非标准代码原样返回。 */
export const displaySymbol = (value?: string | null): string => {
  if (!value) return '—';
  const match = value.match(/^(?:SH|SZ|BJ)\.?(\d{6})$/i);
  return match ? match[1] : value;
};
