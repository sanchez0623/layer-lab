import type { ReactNode } from 'react';
import { AlertCircle, FlaskConical, Inbox, Loader2, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge as ShadcnBadge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description: string; actions?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
      <div className="min-w-0">
        {eyebrow && <div className="mb-1.5 font-mono text-xs font-medium tracking-[0.14em] text-primary">{eyebrow}</div>}
        <h1 className="text-3xl font-semibold tracking-[-0.025em] text-foreground">{title}</h1>
        <p className="mt-1.5 max-w-[680px] text-sm leading-7 text-muted-foreground">{description}</p>
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Panel({
  title, description, action, children, className = '', flush = false,
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  /** 无内边距模式：表格 / 自定义布局直接贴边。 */
  flush?: boolean;
}) {
  return (
    <Card className={cn('gap-0 py-0 shadow-xs', className)}>
      {(title || description || action) && (
        <CardHeader className="mb-3 flex flex-row items-start justify-between gap-3 space-y-0 px-4 pt-4">
          <div className="min-w-0">
            {title && <CardTitle className="text-[15px] font-semibold tracking-[-0.01em] text-foreground">{title}</CardTitle>}
            {description && <CardDescription className="mt-1 text-[13px] leading-6">{description}</CardDescription>}
          </div>
          {action && <div className="flex shrink-0 items-center gap-2 text-muted-foreground">{action}</div>}
        </CardHeader>
      )}
      {flush ? children : <CardContent className="px-4 pb-4">{children}</CardContent>}
    </Card>
  );
}

export function Field({ label, hint, children, className = '' }: { label: string; hint?: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn('flex min-w-0 flex-col gap-1.5', className)}>
      <span className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5 text-[13px] font-medium text-foreground/80">
        <span className="min-w-0">{label}</span>
        {hint && <small className="min-w-0 max-w-full break-words text-right text-[11px] font-normal leading-4 text-muted-foreground">{hint}</small>}
      </span>
      {children}
    </div>
  );
}

export function Loading({ label = '正在读取数据' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 px-1 py-4 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" />
      <span>{label}</span>
    </div>
  );
}

export function EmptyState({
  icon, title, description, action, compact = false,
}: { icon?: ReactNode; title: string; description: string; action?: ReactNode; compact?: boolean }) {
  return (
    <div className={cn('flex flex-col items-center justify-center text-center', compact ? 'gap-2 py-6' : 'gap-3 py-12')}>
      <div className="grid size-11 place-items-center rounded-xl bg-muted text-muted-foreground">{icon || <Inbox className="size-6" />}</div>
      <h3 className="text-[15px] font-semibold text-foreground">{title}</h3>
      <p className="max-w-md text-[13px] leading-6 text-muted-foreground">{description}</p>
      {action}
    </div>
  );
}

export function ErrorBanner({ message, onRetry, onClose }: { message: string; onRetry?: () => void; onClose?: () => void }) {
  return (
    <Alert variant="destructive" className="mb-3">
      <AlertCircle />
      <AlertTitle>操作未完成</AlertTitle>
      <AlertDescription>{message}</AlertDescription>
      {(onRetry || onClose) && (
        <div className="col-start-2 mt-2 flex items-center gap-2">
          {onRetry && <Button variant="outline" size="sm" onClick={onRetry}>重试</Button>}
          {onClose && <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="关闭提示"><X /></Button>}
        </div>
      )}
    </Alert>
  );
}

export function SyntheticNotice({ compact = false }: { compact?: boolean }) {
  return (
    <span className={cn('inline-flex items-center gap-1 rounded-md border border-warning/25 bg-warning/10 px-1.5 py-0.5 text-[11px] font-medium text-warning', compact && 'text-[11px]')}>
      <FlaskConical className="size-3" />合成行情，仅用于功能验证
    </span>
  );
}

const badgeToneClass: Record<string, string> = {
  neutral: 'border-border bg-muted text-muted-foreground',
  indigo: 'border-primary/20 bg-accent text-accent-foreground',
  red: 'border-destructive/20 bg-destructive/10 text-destructive',
  green: 'border-success/20 bg-success/10 text-success',
  amber: 'border-warning/20 bg-warning/10 text-warning',
};

export function Badge({ children, tone = 'neutral', title, className }: { children: ReactNode; tone?: 'neutral' | 'indigo' | 'red' | 'green' | 'amber'; title?: string; className?: string }) {
  return <ShadcnBadge variant="outline" title={title} className={cn('h-5 rounded-md px-2 font-medium', badgeToneClass[tone], className)}>{children}</ShadcnBadge>;
}

export function TableEmpty({ colSpan, message = '暂无记录' }: { colSpan: number; message?: string }) {
  return (
    <tr>
      <td colSpan={colSpan}>
        <div className="flex h-24 items-center justify-center gap-2 text-sm text-muted-foreground">
          <Inbox className="size-4" />{message}
        </div>
      </td>
    </tr>
  );
}

export function Metric({ label, value, sub, tone = '', icon }: { label: string; value: ReactNode; sub?: ReactNode; tone?: 'positive' | 'negative' | ''; icon?: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-xl border border-border bg-card px-3.5 py-3 shadow-xs">
      <div className="flex items-center justify-between gap-2 text-xs font-medium text-muted-foreground">{label}{icon}</div>
      <div className={cn('font-mono text-xl font-medium tracking-tight text-foreground', tone === 'positive' && 'text-destructive', tone === 'negative' && 'text-success')}>{value}</div>
      {sub && <div className="truncate text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

export function MetricGrid({ children, columns = 4, className = '' }: { children: ReactNode; columns?: 4 | 6; className?: string }) {
  return (
    <div className={cn('mb-3 grid gap-2.5', columns === 6 ? 'grid-cols-1 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-6' : 'grid-cols-1 sm:grid-cols-2 md:grid-cols-4', className)}>
      {children}
    </div>
  );
}

export function StatusBadge({ status }: { status?: string }) {
  const value = String(status || '').toLowerCase();
  const tone = value === 'completed' || value === 'closed' || value === 'active' ? 'green'
    : value === 'failed' || value === 'rejected' ? 'red'
      : value === 'running' || value === 'armed' ? 'indigo'
        : 'neutral';
  return <Badge tone={tone}>{status || '—'}</Badge>;
}

export function SelectField({
  value, onValueChange, options, placeholder = '请选择', className = '', ariaLabel, disabled = false,
}: {
  value: string;
  onValueChange: (value: string) => void;
  options: Array<{ value: string; label: string }>;
  placeholder?: string;
  className?: string;
  ariaLabel?: string;
  disabled?: boolean;
}) {
  return (
    <Select value={value} onValueChange={(next) => onValueChange(String(next ?? ''))} disabled={disabled}>
      <SelectTrigger className={cn('w-full', className)} aria-label={ariaLabel}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {options.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}
      </SelectContent>
    </Select>
  );
}

export function SwitchRow({
  label, description, checked, onCheckedChange, className = '',
}: {
  label: string;
  description?: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  className?: string;
}) {
  return (
    <label className={cn('flex cursor-pointer items-center justify-between gap-4 rounded-lg border bg-card px-3 py-2.5', className)}>
      <span className="min-w-0">
        <strong className="block text-[13px] font-medium text-foreground">{label}</strong>
        {description && <small className="mt-0.5 block text-[11px] text-muted-foreground">{description}</small>}
      </span>
      <Switch checked={checked} onCheckedChange={onCheckedChange} />
    </label>
  );
}
