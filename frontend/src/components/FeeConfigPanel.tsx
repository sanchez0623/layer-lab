import { useState } from 'react';
import { Loader2, RotateCcw, Save, Settings2 } from 'lucide-react';
import { toast } from 'sonner';
import { api } from '../lib/api';
import { FEE_FIELDS } from '../lib/feeConfig';
import { Button } from '@/components/ui/button';
import { NumberInput } from '@/components/ui/number-input';
import type { AccountState, FeeConfig } from '../types';

type Busy = 'save' | 'reset' | 'apply' | null;

interface Props {
  value: FeeConfig;
  onChange: (next: FeeConfig) => void;
  accountId?: string;
  onAccountUpdated?: (state: AccountState) => void;
}

export default function FeeConfigPanel({ value, onChange, accountId, onAccountUpdated }: Props) {
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState('');

  const update = (key: keyof FeeConfig, raw: string) => {
    if (raw.trim() === '') return;
    const parsed = Number(raw);
    onChange({ ...value, [key]: Number.isFinite(parsed) && parsed >= 0 ? parsed : 0 });
  };

  const run = async (kind: Exclude<Busy, null>) => {
    setBusy(kind);
    setError('');
    try {
      if (kind === 'save') {
        const result = await api.saveFees(value);
        onChange(result.fees);
        toast.success('已保存为全局默认，之后新建的回测与模拟账户将使用这组费率。');
      } else if (kind === 'reset') {
        const result = await api.resetFees();
        onChange(result.defaults);
        toast.success('已恢复系统默认费率（佣金万0.5 / 印花税万5 / 经手费万0.341 / 证管费万0.2 / 过户费万0.1）。');
      } else {
        if (!accountId) return;
        const state = await api.applyAccountFees(accountId, value);
        onAccountUpdated?.(state);
        toast.success('已把当前费率应用到该模拟账户，仅对此后的成交生效。');
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '费用配置操作失败。');
    } finally {
      setBusy(null);
    }
  };

  const buySide = value.commission_rate + value.handling_fee_rate + value.regulatory_fee_rate + value.transfer_fee_rate;
  const sellSide = buySide + value.stamp_tax_rate;

  return (
    <div className="rounded-xl border bg-muted/25 p-3">
      <div className="grid gap-2 lg:grid-cols-[minmax(0,1fr)_220px]">
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {FEE_FIELDS.map((field) => (
            <label className="flex min-w-0 max-w-full flex-col gap-1 sm:max-w-[180px]" key={field.key}>
              <span className="truncate text-[12px] font-medium text-foreground/80" title={field.label}>{field.label}</span>
              <NumberInput
                min={0}
                max={field.max}
                step={field.step}
                unit={field.unit === '万分之' ? '‱' : field.unit}
                value={value[field.key]}
                onChange={(raw) => update(field.key, raw)}
                aria-label={field.label}
                className="h-8"
              />
              <small className="truncate text-[10px] leading-4 text-muted-foreground" title={field.note}>{field.note}</small>
            </label>
          ))}
        </div>
        <aside className="flex flex-col gap-2 rounded-lg border bg-card p-3">
          <span className="inline-flex w-fit items-center gap-1 rounded-md bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"><Settings2 className="size-3" />费用配置 v1</span>
          <div className="flex items-center justify-between text-[12px]"><span className="text-muted-foreground">买入合计</span><b className="font-mono">{buySide.toFixed(3)} <small className="font-normal text-muted-foreground">‱</small></b></div>
          <div className="flex items-center justify-between text-[12px]"><span className="text-muted-foreground">卖出合计</span><b className="font-mono">{sellSide.toFixed(3)} <small className="font-normal text-muted-foreground">‱</small></b></div>
          <div className="flex items-center justify-between text-[12px]"><span className="text-muted-foreground">最低佣金</span><b className="font-mono">{value.min_commission} <small className="font-normal text-muted-foreground">元/笔</small></b></div>
          <p className="mt-1 border-t pt-2 text-[10px] leading-4 text-muted-foreground">费率按万分之输入、分项相加；印花税仅卖出收取；滑点不计入费用，体现在成交价。</p>
        </aside>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button type="button" size="sm" className="h-8" onClick={() => void run('save')} disabled={busy !== null}>
          {busy === 'save' ? <Loader2 className="animate-spin" /> : <Save />}保存为全局默认
        </Button>
        <Button type="button" variant="outline" size="sm" className="h-8" onClick={() => void run('reset')} disabled={busy !== null}>
          <RotateCcw />恢复默认
        </Button>
        {accountId && (
          <Button type="button" variant="outline" size="sm" className="h-8" onClick={() => void run('apply')} disabled={busy !== null}>
            <Settings2 />应用到当前账户
          </Button>
        )}
        <span className="text-[10px] text-muted-foreground">当前编辑值会直接用于本次回测 / 新建账户；已有账户需单独应用。</span>
      </div>
      {error && <div className="mt-2 rounded-md border border-destructive/20 bg-destructive/5 px-2.5 py-1.5 text-[11px] text-destructive">{error}</div>}
    </div>
  );
}


