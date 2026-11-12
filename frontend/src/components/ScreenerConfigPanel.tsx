import { Filter } from 'lucide-react';
import { Field } from './Ui';
import type { ScreenerConfig } from '../types';

const BOARD_OPTIONS = ['创业板', '科创板'] as const;

const percentValue = (value: number) => Number((value * 100).toFixed(2));

interface Props {
  value: ScreenerConfig;
  onChange: (next: ScreenerConfig) => void;
  /** 漏斗轮动回测强制关闭财务深筛（避免联网 baostock + 未来数据），置灰开关。 */
  fundamentalLockedOff?: boolean;
}

/** 选股漏斗参数面板；漏斗轮动回测与选股页共用同一套阈值语义（比率用小数，界面显示百分比）。 */
export default function ScreenerConfigPanel({ value, onChange, fundamentalLockedOff = true }: Props) {
  const patch = (patchValue: Partial<ScreenerConfig>) => onChange({ ...value, ...patchValue });

  const setRatio = (key: 'chg20_max' | 'week52_drawdown' | 'day_change_max', raw: string) => {
    if (raw.trim() === '') return;
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return;
    patch({ [key]: parsed / 100 } as Partial<ScreenerConfig>);
  };

  const setYi = (key: 'min_amount' | 'min_avg_amount_20d', raw: string) => {
    if (raw.trim() === '') return;
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return;
    patch({ [key]: parsed * 1e8 } as Partial<ScreenerConfig>);
  };

  const toggleBoard = (board: string) => {
    const boards = value.boards.includes(board)
      ? value.boards.filter((item) => item !== board)
      : [...value.boards, board];
    if (boards.length === 0) return; // 至少保留一个板块，否则 L0 无指数可判
    patch({ boards });
  };

  return (
    <div className="funnel-screener-config">
      <div className="screener-board-row">
        <span className="screener-board-label">板块</span>
        {BOARD_OPTIONS.map((board) => (
          <button
            key={board}
            type="button"
            className={`screener-board-chip ${value.boards.includes(board) ? 'active' : ''}`}
            onClick={() => toggleBoard(board)}
            aria-pressed={value.boards.includes(board)}
          >
            {board}<small>{board === '创业板' ? 'sz.399006' : 'sh.000688'}</small>
          </button>
        ))}
        <span className="screener-hint">板块决定每轮选股用哪个指数做 L0 大盘门；至少保留一个。</span>
      </div>
      <div className="form-grid funnel-screener-grid">
        <Field label="A1 20日跌幅阈值" hint="Chg20D < 阈值">
          <div className="input-with-unit"><input type="number" step="1" value={percentValue(value.chg20_max)} onChange={(event) => setRatio('chg20_max', event.target.value)} /><span>%</span></div>
        </Field>
        <Field label="距52周高回撤" hint="Close < High250 × (1 - 回撤)">
          <div className="input-with-unit"><input type="number" min="0" step="1" value={percentValue(value.week52_drawdown)} onChange={(event) => setRatio('week52_drawdown', event.target.value)} /><span>%</span></div>
        </Field>
        <Field label="L3 当日成交额门槛" hint="当日成交额 ≥">
          <div className="input-with-unit"><input type="number" min="0" step="0.5" value={Number((value.min_amount / 1e8).toFixed(2))} onChange={(event) => setYi('min_amount', event.target.value)} /><span>亿</span></div>
        </Field>
        <Field label="L3 20日均额门槛" hint="20 日均成交额 ≥">
          <div className="input-with-unit"><input type="number" min="0" step="0.5" value={Number((value.min_avg_amount_20d / 1e8).toFixed(2))} onChange={(event) => setYi('min_avg_amount_20d', event.target.value)} /><span>亿</span></div>
        </Field>
        <Field label="当日涨幅上限" hint="L2 超过即否决">
          <div className="input-with-unit"><input type="number" step="1" value={percentValue(value.day_change_max)} onChange={(event) => setRatio('day_change_max', event.target.value)} /><span>%</span></div>
        </Field>
        <Field label="上市最少交易日" hint="L2 上市不足否决">
          <input type="number" min="1" step="1" value={value.min_listing_sessions} onChange={(event) => patch({ min_listing_sessions: Math.max(1, Number(event.target.value) || 1) })} />
        </Field>
        <Field label="L4 深筛上限" hint="逐只财务深筛上限">
          <input type="number" min="0" step="1" value={value.max_deep_checks} onChange={(event) => patch({ max_deep_checks: Math.max(0, Number(event.target.value) || 0) })} />
        </Field>
        <label className="switch-row screener-switch" title={fundamentalLockedOff ? '回测为避免未来数据与联网财务，固定关闭财务深筛' : undefined}>
          <span><strong>L2 财务深筛</strong><small>{fundamentalLockedOff ? '回测固定关闭（避免未来数据）' : '亏损且营收&lt;1亿 / 净资产为负'}</small></span>
          <input
            type="checkbox"
            checked={fundamentalLockedOff ? false : value.enable_fundamental_veto}
            disabled={fundamentalLockedOff}
            onChange={(event) => patch({ enable_fundamental_veto: event.target.checked })}
          />
          <span className="switch" />
        </label>
      </div>
      <div className="funnel-screener-footnote">
        <span><Filter size={13} />每次清仓后按以上阈值重跑 L0→L4，取当时第一名换票；数据来自本地 universe.db 缓存，不联网。</span>
      </div>
    </div>
  );
}