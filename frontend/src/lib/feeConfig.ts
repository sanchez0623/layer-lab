import type { FeeConfig, StrategyConfig, Trade } from '../types';

/** 与后端 FeeConfig 默认值保持一致：费率单位「万分之」。 */
export const defaultFeeConfig: FeeConfig = {
  commission_rate: 0.5,
  min_commission: 5,
  stamp_tax_rate: 5,
  handling_fee_rate: 0.341,
  regulatory_fee_rate: 0.2,
  transfer_fee_rate: 0.1,
};

export const FEE_FIELDS: Array<{
  key: keyof FeeConfig;
  label: string;
  unit: string;
  note: string;
  step: number;
  max: number;
}> = [
  { key: 'commission_rate', label: '佣金率（万分之）', unit: '万分之', note: '双边', step: 0.001, max: 100 },
  { key: 'min_commission', label: '最低佣金（元）', unit: '元', note: '仅作用于佣金项', step: 0.5, max: 10000 },
  { key: 'stamp_tax_rate', label: '印花税（万分之）', unit: '万分之', note: '仅卖出', step: 0.01, max: 100 },
  { key: 'handling_fee_rate', label: '经手费（万分之）', unit: '万分之', note: '双边', step: 0.001, max: 100 },
  { key: 'regulatory_fee_rate', label: '证管费（万分之）', unit: '万分之', note: '双边', step: 0.001, max: 100 },
  { key: 'transfer_fee_rate', label: '过户费（万分之）', unit: '万分之', note: '双边', step: 0.001, max: 100 },
];

const STRATEGY_FEE_KEYS = [
  'commission_rate', 'min_commission', 'stamp_tax_rate',
  'handling_fee_rate', 'regulatory_fee_rate', 'transfer_fee_rate',
] as const;

/** 万分之 → 结算用的小数比例；最低佣金保持「元」。 */
export function feeConfigToStrategyPatch(fees: FeeConfig): Partial<StrategyConfig> {
  return {
    commission_rate: fees.commission_rate / 10000,
    min_commission: fees.min_commission,
    stamp_tax_rate: fees.stamp_tax_rate / 10000,
    handling_fee_rate: fees.handling_fee_rate / 10000,
    regulatory_fee_rate: fees.regulatory_fee_rate / 10000,
    transfer_fee_rate: fees.transfer_fee_rate / 10000,
  };
}

/** 后端账户/报告里的 config 是小数比例，反推成界面上的万分之。 */
export function strategyToFeeConfig(config?: Partial<StrategyConfig>): FeeConfig {
  if (!config) return { ...defaultFeeConfig };
  const toWan = (value: number | undefined, fallback: number) =>
    value === undefined ? fallback : Math.round(value * 10000 * 1_000_000) / 1_000_000;
  return {
    commission_rate: toWan(config.commission_rate, defaultFeeConfig.commission_rate),
    min_commission: config.min_commission ?? defaultFeeConfig.min_commission,
    stamp_tax_rate: toWan(config.stamp_tax_rate, defaultFeeConfig.stamp_tax_rate),
    handling_fee_rate: toWan(config.handling_fee_rate, defaultFeeConfig.handling_fee_rate),
    regulatory_fee_rate: toWan(config.regulatory_fee_rate, defaultFeeConfig.regulatory_fee_rate),
    transfer_fee_rate: toWan(config.transfer_fee_rate, defaultFeeConfig.transfer_fee_rate),
  };
}

/** 提交时费用单独走 fees 字段，避免 config 里的旧费率覆盖自定义值。 */
export function stripFeeFields(config: StrategyConfig): Partial<StrategyConfig> {
  const result: Record<string, unknown> = { ...config };
  STRATEGY_FEE_KEYS.forEach((key) => { delete result[key]; });
  return result as Partial<StrategyConfig>;
}

export function feeBreakdownTitle(trade: Trade): string {
  const parts = [
    ['佣金', trade.commission], ['印花税', trade.stamp_tax], ['经手费', trade.handling_fee],
    ['证管费', trade.regulatory_fee], ['过户费', trade.transfer_fee],
  ] as const;
  const lines = parts
    .filter(([, value]) => typeof value === 'number')
    .map(([label, value]) => `${label} ${(value as number).toFixed(2)}`);
  return lines.length > 0 ? `分项相加：${lines.join(' + ')}` : '暂无分项明细';
}
