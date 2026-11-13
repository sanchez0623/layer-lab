import type { ReactNode } from 'react';
import { Filter, SlidersHorizontal } from 'lucide-react';
import type { ScreenerConfig } from '../types';

const BOARD_OPTIONS = ['创业板', '科创板'] as const;

const percentValue = (value: number) => Number((value * 100).toFixed(2));

interface Props {
  value: ScreenerConfig;
  onChange: (next: ScreenerConfig) => void;
  /** 漏斗轮动回测强制关闭财务深筛（避免联网 baostock + 未来数据），置灰开关。 */
  fundamentalLockedOff?: boolean;
}

/** 阶段内单个参数：标签 / 控件 / 说明自上而下排布，避免长文案挤在标签行里。 */
function Param({ label, hint, children }: { label: string; hint: string; children: ReactNode }) {
  return (
    <label className="funnel-param">
      <span className="funnel-param-label">{label}</span>
      {children}
      <small className="funnel-param-hint">{hint}</small>
    </label>
  );
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
      <div className="funnel-config-head">
        <div className="funnel-config-heading">
          <span className="funnel-config-icon"><SlidersHorizontal size={14} /></span>
          <div>
            <strong>选股漏斗阈值</strong>
            <small>清仓后按 L0→L4 重新筛选，取当时第一名换票</small>
          </div>
        </div>
        <div className="funnel-board-row" title="板块决定每轮选股用哪个指数做 L0 大盘门，至少保留一个">
          <span className="funnel-board-label">板块</span>
          {BOARD_OPTIONS.map((board) => (
            <button
              key={board}
              type="button"
              className={`funnel-board-chip ${value.boards.includes(board) ? 'active' : ''}`}
              onClick={() => toggleBoard(board)}
              aria-pressed={value.boards.includes(board)}
            >
              {board}<small>{board === '创业板' ? 'sz.399006' : 'sh.000688'}</small>
            </button>
          ))}
        </div>
      </div>

      <div className="funnel-stage-grid">
        <section className="funnel-stage" data-stage="L1">
          <header className="funnel-stage-head">
            <span className="funnel-stage-badge">L1</span>
            <div><strong>超跌初筛</strong><small>先按跌幅与回撤锁定候选</small></div>
          </header>
          <div className="funnel-stage-body">
            <Param label="A1 20日跌幅阈值" hint="20 日涨跌幅低于阈值">
              <div className="input-with-unit"><input type="number" step="1" value={percentValue(value.chg20_max)} onChange={(event) => setRatio('chg20_max', event.target.value)} /><span>%</span></div>
            </Param>
            <Param label="距52周高回撤" hint="收盘价低于 250 日高点回撤线">
              <div className="input-with-unit"><input type="number" min="0" step="1" value={percentValue(value.week52_drawdown)} onChange={(event) => setRatio('week52_drawdown', event.target.value)} /><span>%</span></div>
            </Param>
          </div>
          <p className="funnel-stage-note">两条同时满足才进入下一层</p>
        </section>

        <section className="funnel-stage" data-stage="L2">
          <header className="funnel-stage-head">
            <span className="funnel-stage-badge">L2</span>
            <div><strong>风控否决</strong><small>剔除过热、次新与财务瑕疵</small></div>
          </header>
          <div className="funnel-stage-body">
            <Param label="当日涨幅上限" hint="当日涨幅超过即否决">
              <div className="input-with-unit"><input type="number" step="1" value={percentValue(value.day_change_max)} onChange={(event) => setRatio('day_change_max', event.target.value)} /><span>%</span></div>
            </Param>
            <Param label="上市最少交易日" hint="上市不足则否决">
              <div className="input-with-unit"><input type="number" min="1" step="1" value={value.min_listing_sessions} onChange={(event) => patch({ min_listing_sessions: Math.max(1, Number(event.target.value) || 1) })} /><span>日</span></div>
            </Param>
            <label
              className="switch-row funnel-param-switch"
              title={fundamentalLockedOff ? '回测为避免未来数据与联网财务，固定关闭财务深筛' : undefined}
            >
              <span><strong>财务深筛</strong><small>{fundamentalLockedOff ? '回测固定关闭（避免未来数据）' : '亏损且营收&lt;1亿 / 净资产为负'}</small></span>
              <input
                type="checkbox"
                checked={fundamentalLockedOff ? false : value.enable_fundamental_veto}
                disabled={fundamentalLockedOff}
                onChange={(event) => patch({ enable_fundamental_veto: event.target.checked })}
              />
              <span className="switch" />
            </label>
          </div>
          <p className="funnel-stage-note">任一条触发即否决</p>
        </section>

        <section className="funnel-stage" data-stage="L3">
          <header className="funnel-stage-head">
            <span className="funnel-stage-badge">L3</span>
            <div><strong>流动性门槛</strong><small>保证换仓时有足够成交额</small></div>
          </header>
          <div className="funnel-stage-body">
            <Param label="当日成交额门槛" hint="当日成交额不低于门槛">
              <div className="input-with-unit"><input type="number" min="0" step="0.5" value={Number((value.min_amount / 1e8).toFixed(2))} onChange={(event) => setYi('min_amount', event.target.value)} /><span>亿</span></div>
            </Param>
            <Param label="20日均额门槛" hint="20 日均成交额不低于门槛">
              <div className="input-with-unit"><input type="number" min="0" step="0.5" value={Number((value.min_avg_amount_20d / 1e8).toFixed(2))} onChange={(event) => setYi('min_avg_amount_20d', event.target.value)} /><span>亿</span></div>
            </Param>
          </div>
          <p className="funnel-stage-note">成交额不足即剔除</p>
        </section>

        <section className="funnel-stage" data-stage="L4">
          <header className="funnel-stage-head">
            <span className="funnel-stage-badge">L4</span>
            <div><strong>财务深筛</strong><small>逐只核验基本面</small></div>
          </header>
          <div className="funnel-stage-body">
            <Param label="深筛上限" hint="最多逐只做财务深筛的数量">
              <div className="input-with-unit"><input type="number" min="0" step="1" value={value.max_deep_checks} onChange={(event) => patch({ max_deep_checks: Math.max(0, Number(event.target.value) || 0) })} /><span>只</span></div>
            </Param>
          </div>
          <p className="funnel-stage-note">控制基本面核验次数</p>
        </section>
      </div>

      <div className="funnel-screener-footnote">
        <Filter size={13} />
        <span>每次清仓后按以上阈值重跑 L0→L4，取当时第一名换票；数据来自本地 universe.db 缓存，不联网。</span>
      </div>
    </div>
  );
}
