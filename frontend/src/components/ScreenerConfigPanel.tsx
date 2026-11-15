import type { ReactNode } from 'react';
import { Filter, SlidersHorizontal } from 'lucide-react';
import { Badge } from './Ui';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { cn } from '@/lib/utils';
import type { ScreenerConfig } from '../types';

const BOARD_OPTIONS = ['创业板', '科创板'] as const;

const percentValue = (value: number) => Number((value * 100).toFixed(2));

interface Props {
  value: ScreenerConfig;
  onChange: (next: ScreenerConfig) => void;
  /** 漏斗轮动回测强制关闭财务深筛（避免联网 baostock + 未来数据），置灰开关。 */
  fundamentalLockedOff?: boolean;
}

function Param({ label, hint, children }: { label: string; hint: string; children: ReactNode }) {
  return (
    <label className="flex min-w-0 flex-col gap-1.5">
      <span className="text-[12px] font-medium text-foreground/80">{label}</span>
      {children}
      <small className="text-[10px] leading-4 text-muted-foreground">{hint}</small>
    </label>
  );
}

function UnitInput({ children, unit }: { children: ReactNode; unit: string }) {
  return (
    <div className="relative">
      {children}
      <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 font-mono text-[11px] text-muted-foreground">{unit}</span>
    </div>
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
    if (boards.length === 0) return;
    patch({ boards });
  };

  const stages = [
    {
      tag: 'L1', title: '超跌初筛', note: '两条同时满足才进入下一层',
      body: (
        <>
          <Param label="A1 20日跌幅阈值" hint="20 日涨跌幅低于阈值">
            <UnitInput unit="%"><Input type="number" step="1" value={percentValue(value.chg20_max)} onChange={(event) => setRatio('chg20_max', event.target.value)} className="pr-7" /></UnitInput>
          </Param>
          <Param label="距52周高回撤" hint="收盘价低于 250 日高点回撤线">
            <UnitInput unit="%"><Input type="number" min="0" step="1" value={percentValue(value.week52_drawdown)} onChange={(event) => setRatio('week52_drawdown', event.target.value)} className="pr-7" /></UnitInput>
          </Param>
        </>
      ),
    },
    {
      tag: 'L2', title: '风控否决', note: '任一条触发即否决',
      body: (
        <>
          <Param label="当日涨幅上限" hint="当日涨幅超过即否决">
            <UnitInput unit="%"><Input type="number" step="1" value={percentValue(value.day_change_max)} onChange={(event) => setRatio('day_change_max', event.target.value)} className="pr-7" /></UnitInput>
          </Param>
          <Param label="上市最少交易日" hint="上市不足则否决">
            <UnitInput unit="日"><Input type="number" min="1" step="1" value={value.min_listing_sessions} onChange={(event) => patch({ min_listing_sessions: Math.max(1, Number(event.target.value) || 1) })} className="pr-7" /></UnitInput>
          </Param>
          <label className="flex items-center justify-between gap-3 rounded-lg border bg-muted/35 px-2.5 py-2" title={fundamentalLockedOff ? '回测为避免未来数据与联网财务，固定关闭财务深筛' : undefined}>
            <span><strong className="block text-[12px] font-medium">财务深筛</strong><small className="block text-[10px] text-muted-foreground">{fundamentalLockedOff ? '回测固定关闭（避免未来数据）' : '亏损且营收<1亿 / 净资产为负'}</small></span>
            <Switch checked={fundamentalLockedOff ? false : value.enable_fundamental_veto} disabled={fundamentalLockedOff} onCheckedChange={(checked) => patch({ enable_fundamental_veto: checked })} />
          </label>
        </>
      ),
    },
    {
      tag: 'L3', title: '流动性门槛', note: '成交额不足即剔除',
      body: (
        <>
          <Param label="当日成交额门槛" hint="当日成交额不低于门槛">
            <UnitInput unit="亿"><Input type="number" min="0" step="0.5" value={Number((value.min_amount / 1e8).toFixed(2))} onChange={(event) => setYi('min_amount', event.target.value)} className="pr-7" /></UnitInput>
          </Param>
          <Param label="20日均额门槛" hint="20 日均成交额不低于门槛">
            <UnitInput unit="亿"><Input type="number" min="0" step="0.5" value={Number((value.min_avg_amount_20d / 1e8).toFixed(2))} onChange={(event) => setYi('min_avg_amount_20d', event.target.value)} className="pr-7" /></UnitInput>
          </Param>
        </>
      ),
    },
    {
      tag: 'L4', title: '财务深筛', note: '控制基本面核验次数',
      body: (
        <Param label="深筛上限" hint="最多逐只做财务深筛的数量">
          <UnitInput unit="只"><Input type="number" min="0" step="1" value={value.max_deep_checks} onChange={(event) => patch({ max_deep_checks: Math.max(0, Number(event.target.value) || 0) })} className="pr-7" /></UnitInput>
        </Param>
      ),
    },
  ];

  return (
    <div className="rounded-xl border bg-muted/25 p-3">
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-2">
          <span className="grid size-7 place-items-center rounded-md bg-accent text-accent-foreground"><SlidersHorizontal className="size-3.5" /></span>
          <div><strong className="block text-[13px]">选股漏斗阈值</strong><small className="block text-[11px] text-muted-foreground">清仓后按 L0→L4 重新筛选，取当时第一名换票</small></div>
        </div>
        <div className="flex items-center gap-1.5" title="板块决定每轮选股用哪个指数做 L0 大盘门，至少保留一个">
          <span className="text-[11px] text-muted-foreground">板块</span>
          {BOARD_OPTIONS.map((board) => {
            const active = value.boards.includes(board);
            return (
              <button
                key={board}
                type="button"
                className={cn('rounded-md border px-2 py-1 text-[11px] transition-colors', active ? 'border-primary/30 bg-accent text-accent-foreground' : 'bg-card text-muted-foreground hover:bg-muted')}
                onClick={() => toggleBoard(board)}
                aria-pressed={active}
              >
                {board}<small className="ml-1 font-mono text-[9px] opacity-70">{board === '创业板' ? 'sz.399006' : 'sh.000688'}</small>
              </button>
            );
          })}
        </div>
      </div>

      <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
        {stages.map((stage) => (
          <section key={stage.tag} className="flex min-w-0 flex-col gap-2.5 rounded-lg border bg-card p-2.5 shadow-xs">
            <header className="flex items-center gap-2">
              <Badge tone="indigo">{stage.tag}</Badge>
              <div><strong className="block text-[12px]">{stage.title}</strong><small className="block text-[10px] text-muted-foreground">{stage.note}</small></div>
            </header>
            <div className="grid gap-2">{stage.body}</div>
          </section>
        ))}
      </div>

      <div className="mt-2.5 flex items-start gap-1.5 text-[10px] leading-4 text-muted-foreground">
        <Filter className="mt-0.5 size-3 shrink-0" />
        <span>每次清仓后按以上阈值重跑 L0→L4，取当时第一名换票；数据来自本地 universe.db 缓存，不联网。</span>
      </div>
    </div>
  );
}
