import type { ReactNode } from 'react';
import { AlertCircle, CheckCircle2, FlaskConical, Inbox, LoaderCircle, X } from 'lucide-react';

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description: string; actions?: ReactNode }) {
  return (
    <div className="page-heading">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

export function Panel({ title, description, action, children, className = '' }: { title?: string; description?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`panel ${className}`}>
      {(title || action) && (
        <div className="panel-header">
          <div>{title && <h2>{title}</h2>}{description && <p>{description}</p>}</div>
          {action && <div className="panel-action">{action}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function Field({ label, hint, children, className = '' }: { label: string; hint?: string; children: ReactNode; className?: string }) {
  return (
    <label className={`field ${className}`}>
      <span className="field-label">{label}{hint && <small>{hint}</small>}</span>
      {children}
    </label>
  );
}

export function Loading({ label = '正在读取数据' }: { label?: string }) {
  return <div className="loading-state"><LoaderCircle size={18} className="spin" /><span>{label}</span></div>;
}

export function EmptyState({ icon, title, description, action, compact = false }: { icon?: ReactNode; title: string; description: string; action?: ReactNode; compact?: boolean }) {
  return (
    <div className={`empty-state ${compact ? 'compact' : ''}`}>
      <div className="empty-icon">{icon || <Inbox size={24} />}</div>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}

export function ErrorBanner({ message, onRetry, onClose }: { message: string; onRetry?: () => void; onClose?: () => void }) {
  return (
    <div className="alert error-alert" role="alert">
      <AlertCircle size={18} />
      <div><strong>操作未完成</strong><p>{message}</p></div>
      {onRetry && <button className="text-button" onClick={onRetry}>重试</button>}
      {onClose && <button className="icon-button small" onClick={onClose} aria-label="关闭提示"><X size={16} /></button>}
    </div>
  );
}

export function SuccessBanner({ children, onClose }: { children: ReactNode; onClose?: () => void }) {
  return (
    <div className="alert success-alert" role="status">
      <CheckCircle2 size={18} /><div>{children}</div>
      {onClose && <button className="icon-button small" onClick={onClose} aria-label="关闭提示"><X size={16} /></button>}
    </div>
  );
}

export function SyntheticNotice({ compact = false }: { compact?: boolean }) {
  return <span className={`synthetic-notice ${compact ? 'compact' : ''}`}><FlaskConical size={13} />合成行情，仅用于功能验证</span>;
}

export function Badge({ children, tone = 'neutral', title }: { children: ReactNode; tone?: 'neutral' | 'indigo' | 'red' | 'green' | 'amber'; title?: string }) {
  return <span className={`badge badge-${tone}`} title={title}>{children}</span>;
}

export function TableEmpty({ colSpan, message = '暂无记录' }: { colSpan: number; message?: string }) {
  return <tr><td colSpan={colSpan}><div className="table-empty"><Inbox size={18} />{message}</div></td></tr>;
}

export function Metric({ label, value, sub, tone = '', icon }: { label: string; value: ReactNode; sub?: ReactNode; tone?: 'positive' | 'negative' | ''; icon?: ReactNode }) {
  return (
    <div className="metric-card">
      <div className="metric-label">{label}{icon}</div>
      <div className={`metric-value ${tone}`}>{value}</div>
      {sub && <div className="metric-sub">{sub}</div>}
    </div>
  );
}
