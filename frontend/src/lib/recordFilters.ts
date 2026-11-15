import type { Trade } from '../types';
import { lower, purposeLabel } from './format';

/** 成交明细筛选条件（空字符串表示该项不生效）。 */
export interface TradeFilters {
  /** 标的：trim 后子串匹配（如 688200 / sh.688200 / sz）。 */
  symbol: string;
  /** 用途：原始枚举（PERMANENT/T/BETA）或中文标签（永久仓/T仓/ETF对冲），全等匹配。 */
  purpose: string;
  /** 方向：BUY / SELL，全等匹配。 */
  side: string;
}

export const EMPTY_TRADE_FILTERS: TradeFilters = { symbol: '', purpose: '', side: '' };

/** 筛选用途选项（与后端 _PURPOSE_ENUMS 一致；展示名走 purposeLabel）。 */
export const PURPOSE_FILTER_OPTIONS = ['PERMANENT', 'T', 'BETA'] as const;

/** 筛选方向选项。 */
export const SIDE_FILTER_OPTIONS = ['BUY', 'SELL'] as const;

export function tradeFilterActive(filters: TradeFilters): boolean {
  return Boolean(filters.symbol.trim() || filters.purpose || filters.side);
}

/**
 * 旧报告本地回退路径的成交匹配（口径与后端 ``main.py::_record_matches`` 一致）：
 * 标的子串 + 用途/方向全等。改动任一侧时同步另一侧，避免离线回退与线上口径分叉。
 */
export function matchesTradeFilters(trade: Trade, filters: TradeFilters): boolean {
  const symbol = filters.symbol.trim().toLowerCase();
  if (symbol && !lower(trade.symbol).includes(symbol)) return false;
  if (filters.purpose) {
    const query = filters.purpose.trim().toLowerCase();
    const raw = lower(trade.purpose).trim();
    const label = purposeLabel(trade.purpose).toLowerCase();
    if (raw !== query && label !== query) return false;
  }
  if (filters.side && lower(trade.side).trim() !== filters.side.trim().toLowerCase()) return false;
  return true;
}
