import { useState } from 'react';
import { CheckCircle2, RotateCcw, Save, Settings2 } from 'lucide-react';
import { api } from '../lib/api';
import { FEE_FIELDS } from '../lib/feeConfig';
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
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  const update = (key: keyof FeeConfig, raw: string) => {
    const parsed = Number(raw);
    onChange({ ...value, [key]: Number.isFinite(parsed) && parsed >= 0 ? parsed : 0 });
  };

  const run = async (kind: Exclude<Busy, null>) => {
    setBusy(kind);
    setError('');
    setMessage('');
    try {
      if (kind === 'save') {
        const result = await api.saveFees(value);
        onChange(result.fees);
        setMessage('已保存为全局默认，之后新建的回测与模拟账户将使用这组费率。');
      } else if (kind === 'reset') {
        const result = await api.resetFees();
        onChange(result.defaults);
        setMessage('已恢复系统默认费率（佣金万0.5 / 印花税万5 / 经手费万0.341 / 证管费万0.2 / 过户费万0.1）。');
      } else {
        if (!accountId) return;
        const state = await api.applyAccountFees(accountId, value);
        onAccountUpdated?.(state);
        setMessage('已把当前费率应用到该模拟账户，仅对此后的成交生效。');
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
    <div className="fee-config-card">
      <div className="fee-config-layout">
        <div className="fee-config-grid">
          {FEE_FIELDS.map((field) => (
            <label className="fee-field" key={field.key}>
              <span className="fee-field-label">{field.label}</span>
              <div className="input-with-unit">
                <input
                  type="number"
                  min="0"
                  max={field.max}
                  step={field.step}
                  value={value[field.key]}
                  onChange={(event) => update(field.key, event.target.value)}
                  aria-label={field.label}
                />
                <span>{field.unit === '万分之' ? '‱' : field.unit}</span>
              </div>
              <small className="fee-field-note">{field.note}</small>
            </label>
          ))}
        </div>
        <aside className="fee-config-side">
          <span className="fee-version"><Settings2 size={12} />费用配置 v1</span>
          <div className="fee-total"><span>买入合计</span><b>{buySide.toFixed(3)} <small>‱</small></b></div>
          <div className="fee-total"><span>卖出合计</span><b>{sellSide.toFixed(3)} <small>‱</small></b></div>
          <div className="fee-total"><span>最低佣金</span><b>{value.min_commission} <small>元/笔</small></b></div>
          <p className="fee-config-note">费率按万分之输入、分项相加；印花税仅卖出收取；滑点不计入费用，体现在成交价。</p>
        </aside>
      </div>
      <div className="fee-config-actions">
        <button type="button" className="button primary" onClick={() => void run('save')} disabled={busy !== null}>
          {busy === 'save' ? <span className="button-spinner" /> : <Save size={14} />}保存为全局默认
        </button>
        <button type="button" className="button secondary" onClick={() => void run('reset')} disabled={busy !== null}>
          <RotateCcw size={14} />恢复默认
        </button>
        {accountId && (
          <button type="button" className="button secondary" onClick={() => void run('apply')} disabled={busy !== null}>
            <Settings2 size={14} />应用到当前账户
          </button>
        )}
        <span className="fee-config-hint">当前编辑值会直接用于本次回测 / 新建账户；已有账户需单独应用。</span>
      </div>
      {message && <div className="fee-config-message ok"><CheckCircle2 size={14} />{message}</div>}
      {error && <div className="fee-config-message err">{error}</div>}
    </div>
  );
}
