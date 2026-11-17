import type { StrategyConfig } from '../types';

/**
 * 策略配置的前端预校验：返回「字段 → 提示文案」，用于在输入框下方直接显示 tips。
 *
 * 为什么要这一层：
 * - 约束的唯一真相源仍是后端 `StrategyConfig.__post_init__`（domain.py）：直连 API / 导入老报告时
 *   照样 400，fail fast 不放松。
 * - 但回测页是「改动即自动保存」——用户在输入框里敲一个 5（%）就会触发一次 PUT 并收到 400，
 *   而页面上只在底部显示一行「配置保存失败」，用户根本看不到原因（本次问题的由来）。
 * - 所以把同一套约束在输入当下算出来，作为字段下方的红色提示；存在错误时暂停自动保存并阻止提交，
 *   既把规则说清楚，也避免无意义的 400 往返。
 *
 * ⚠️ 改规则时两处同步：`backend/app/domain.py::StrategyConfig.__post_init__` 与本文件；
 * 后端边界断言见 `backend/tests/test_strategy.py`。
 */
export type ConfigIssueMap = Partial<Record<keyof StrategyConfig, string>>;

/** 字段中文名：弹窗/汇总提示里用（字段下方红字已紧贴输入框，不必重复）。 */
export const CONFIG_FIELD_LABELS: Partial<Record<keyof StrategyConfig, string>> = {
  t_deep_drop_pct: '深跌幅当日做T',
  t_target_net_spread: '做 T 净利止盈',
  t_exit_cutoff: '做 T T+1 截止时间',
  t_min_amplitude: '做 T 最小振幅',
  t_min_turnover: '做 T 换手率下限',
  t_max_turnover: '做 T 换手率上限',
  t_min_expected_net_spread: '做 T 最小期望净利',
  t_min_base_entries: '做 T 底仓笔数门槛',
  force_exit_holding_days: '最大持仓天数',
  volume_participation: '成交量参与上限',
  slippage_bps: '滑点',
  etf_symbol: 'ETF 代码',
  etf_position_pct: 'ETF 仓位',
  etf_hard_stop_pct: 'ETF 硬止损',
  etf_entry_max_deviation: '不追高上限',
  etf_reentry_cooldown_days: '清仓后冷却',
  etf_entry_ma_days: '入场均线',
  etf_exit_ma_days: '出场均线',
  withdraw_ratio: '提取比例',
  withdraw_fixed_amount: '固定金额',
};

/** 把「字段 + 文案」拼成一句可读提示：`深跌幅当日做T：开启时须 ≥ 10%…`。 */
export const describeConfigIssue = (field: keyof StrategyConfig, message: string): string =>
  `${CONFIG_FIELD_LABELS[field] ?? String(field)}：${message}`;


const isNum = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);

/** HH:MM（24 小时制）合法性判定，与后端 strptime("%H:%M") 同口径。 */
const isTimeOfDay = (value: string): boolean => {
  const match = /^(\d{1,2}):(\d{2})$/.exec(value.trim());
  if (!match) return false;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  return hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59;
};

export function validateStrategyConfig(config: StrategyConfig): ConfigIssueMap {
  const issues: ConfigIssueMap = {};
  const issue = <K extends keyof StrategyConfig>(field: K, message: string) => {
    issues[field] = message;
  };

  // ---- 做 T 相关 -----------------------------------------------------------
  // 深跌幅当日做T：None / 0 = 关闭；开启时必须 >= 10%（20cm 板极端恐慌线）。
  const deepDrop = config.t_deep_drop_pct;
  if (deepDrop !== null && deepDrop !== undefined && isNum(deepDrop) && deepDrop !== 0 && deepDrop < 0.1) {
    issue('t_deep_drop_pct', '开启时须 ≥ 10%（20cm 板极端恐慌线）；填 0 = 关闭');
  }
  const netSpread = config.t_target_net_spread;
  if (isNum(netSpread) && !(netSpread > 0 && netSpread < 1)) {
    issue('t_target_net_spread', '须为大于 0 且小于 100% 的百分数');
  }
  const cutoff = config.t_exit_cutoff;
  if (cutoff !== undefined && !isTimeOfDay(cutoff)) {
    issue('t_exit_cutoff', '须为合法的 HH:MM 时间（如 14:50）');
  }
  const amplitude = config.t_min_amplitude;
  if (amplitude !== undefined && !(isNum(amplitude) && amplitude > 0 && amplitude <= 1)) {
    issue('t_min_amplitude', '须为 0~100%（不含 0）的百分数，否则做 T 准入恒为否');
  }
  const expectedNet = config.t_min_expected_net_spread;
  if (expectedNet !== undefined && !(isNum(expectedNet) && expectedNet > 0 && expectedNet <= 1)) {
    issue('t_min_expected_net_spread', '须为 0~100%（不含 0）的百分数，否则做 T 准入恒为否');
  }
  // 换手率区间：0 <= 下限 <= 上限 <= 100%，自相矛盾的值后端会 400。
  const minTurnover = config.t_min_turnover;
  const maxTurnover = config.t_max_turnover;
  if (isNum(minTurnover) && isNum(maxTurnover) && minTurnover > maxTurnover) {
    issue('t_min_turnover', '换手率下限不能高于上限');
    issue('t_max_turnover', '换手率上限不能低于下限');
  } else {
    if (isNum(minTurnover) && !(minTurnover >= 0 && minTurnover <= 1)) {
      issue('t_min_turnover', '须为 0~100% 的百分数');
    }
    if (isNum(maxTurnover) && !(maxTurnover > 0 && maxTurnover <= 1)) {
      issue('t_max_turnover', '须为 0~100%（不含 0）的百分数');
    }
  }
  // 做T底仓笔数门槛：1~16 笔整数（后端同口径 400）。
  const baseEntries = config.t_min_base_entries;
  if (baseEntries !== undefined && !(isNum(baseEntries) && Number.isInteger(baseEntries) && baseEntries >= 1 && baseEntries <= 16)) {
    issue('t_min_base_entries', '须为 1~16 笔之间的整数（做T的底仓笔数门槛）');
  }
  // 硬性最大持仓天数：0~250 交易日整数（0 = 不限）。
  const forceDays = config.force_exit_holding_days;
  if (forceDays !== undefined && !(isNum(forceDays) && Number.isInteger(forceDays) && forceDays >= 0 && forceDays <= 250)) {
    issue('force_exit_holding_days', '须为 0~250 个交易日之间的整数（0 = 不限）');
  }

  // ---- 交易摩擦 / 撮合 ------------------------------------------------------
  if (!(isNum(config.volume_participation) && config.volume_participation > 0 && config.volume_participation <= 1)) {
    issue('volume_participation', '须为 0~100%（不含 0）的百分数');
  }
  if (!(isNum(config.slippage_bps) && config.slippage_bps >= 0)) {
    issue('slippage_bps', '滑点不能为负');
  }

  // ---- 指数 beta 腿（ETF）：仅在开启时校验，和表单一并展示 --------------------
  if (config.etf_beta_enabled) {
    const symbol = (config.etf_symbol ?? '').trim().toLowerCase();
    if (!/^(sh|sz)\.\d{6}$/.test(symbol)) {
      issue('etf_symbol', '须为 sh/sz 前缀的 6 位代码（如 sz.159915）');
    }
    const position = config.etf_position_pct;
    if (!(isNum(position) && position > 0 && position <= 1)) {
      issue('etf_position_pct', '须为 1~100% 的百分数');
    }
    const stop = config.etf_hard_stop_pct;
    if (!(isNum(stop) && stop > 0 && stop < 1)) {
      issue('etf_hard_stop_pct', '须为 0~100%（不含 0 与 100）的百分数');
    }
    const deviation = config.etf_entry_max_deviation;
    if (deviation !== undefined && !(isNum(deviation) && deviation >= 0 && deviation < 1)) {
      issue('etf_entry_max_deviation', '须为 0~100%（不含 100）的百分数；0 = 关闭');
    }
    const cooldown = config.etf_reentry_cooldown_days;
    if (cooldown !== undefined && !(isNum(cooldown) && cooldown >= 0 && cooldown <= 60)) {
      issue('etf_reentry_cooldown_days', '须为 0~60 个交易日');
    }
    for (const field of ['etf_entry_ma_days', 'etf_exit_ma_days'] as const) {
      const value = config[field];
      if (value !== undefined && !(isNum(value) && value >= 2 && value <= 250)) {
        issue(field, '须为 2~250 个交易日');
      }
    }
  }

  // ---- 盈利提现 ------------------------------------------------------------
  const withdrawRatio = config.withdraw_ratio;
  if (withdrawRatio !== undefined && !(isNum(withdrawRatio) && withdrawRatio > 0 && withdrawRatio <= 1)) {
    issue('withdraw_ratio', '须为 0~100%（不含 0）的百分数');
  }
  const withdrawFixed = config.withdraw_fixed_amount;
  if (withdrawFixed !== undefined && !(isNum(withdrawFixed) && withdrawFixed >= 0)) {
    issue('withdraw_fixed_amount', '固定金额不能为负');
  }

  return issues;
}
