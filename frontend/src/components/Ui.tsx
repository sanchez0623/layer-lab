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
    <div className="mb-3 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
      <div className="min-w-0">
        {eyebrow && <div className="mb-1 font-mono text-[11px] font-medium tracking-[0.14em] text-primary">{eyebrow}</div>}
        <h1 className="text-[28px] font-semibold leading-tight tracking-[-0.025em] text-foreground">{title}</h1>
        <p className="mt-1 max-w-[680px] text-[13px] leading-6 text-muted-foreground">{description}</p>
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
        <CardHeader className="mb-2.5 flex flex-row items-start justify-between gap-3 space-y-0 px-3.5 pt-3.5">
          <div className="min-w-0">
            {title && <CardTitle className="text-[15px] font-semibold tracking-[-0.01em] text-foreground">{title}</CardTitle>}
            {description && <CardDescription className="mt-0.5 text-[12px] leading-5">{description}</CardDescription>}
          </div>
          {action && <div className="flex shrink-0 items-center gap-2 text-muted-foreground">{action}</div>}
        </CardHeader>
      )}
      {flush ? children : <CardContent className="px-3.5 pb-3.5">{children}</CardContent>}
    </Card>
  );
}

export function Field({
  label, hint, children, className = '', wide = false,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
  /** 名称 / 数据集等需要展示长文本的字段；默认紧凑宽度。 */
  wide?: boolean;
}) {
  return (
    <div className={cn(
      'flex w-full min-w-0 shrink-0 flex-col gap-1 [&_[data-slot=input]]:h-8 [&_[data-slot=select-trigger]]:h-8',
      wide ? 'sm:max-w-[360px]' : 'sm:max-w-[200px]',
      className,
    )}>
      <span className="block h-5 truncate text-[13px] font-medium leading-5 text-foreground/80" title={label}>{label}</span>
      {children}
      {hint
        ? <small className="block h-4 truncate text-[10px] leading-4 text-muted-foreground" title={hint}>{hint}</small>
        : <small className="block h-4" aria-hidden="true" />}
    </div>
  );
}

/** 紧凑型表单栅格：固定宽度字段左对齐排布，不再把每个输入框拉满整列。 */
export function FormGrid({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={cn('flex flex-wrap items-start gap-x-4 gap-y-2', className)}>{children}</div>;
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

export function TableEmpty({ colSpan, message = '暂无记录', action }: { colSpan: number; message?: string; action?: ReactNode }) {
  return (
    <tr>
      <td colSpan={colSpan}>
        <div className="flex h-24 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
          <span className="flex items-center gap-2"><Inbox className="size-4" />{message}</span>
          {action && <div className="flex items-center gap-2">{action}</div>}
        </div>
      </td>
    </tr>
  );
}

export function Metric({ label, value, sub, tone = '', icon }: { label: string; value: ReactNode; sub?: ReactNode; tone?: 'positive' | 'negative' | ''; icon?: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 rounded-lg border border-border bg-card px-3 py-2.5 shadow-xs">
      <div className="flex items-center justify-between gap-2 text-[11px] font-medium text-muted-foreground">{label}{icon}</div>
      <div className={cn('font-mono text-lg font-medium tracking-tight text-foreground', tone === 'positive' && 'text-destructive', tone === 'negative' && 'text-success')}>{value}</div>
      {sub && <div className="truncate text-[10px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

export function MetricGrid({ children, columns = 4, className = '' }: { children: ReactNode; columns?: 4 | 6; className?: string }) {
  return (
    <div className={cn('mb-2.5 grid gap-2', columns === 6 ? 'grid-cols-1 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-6' : 'grid-cols-1 sm:grid-cols-2 md:grid-cols-4', className)}>
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
  options: Array<{ value: string; label: string; disabled?: boolean }>;
  placeholder?: string;
  className?: string;
  ariaLabel?: string;
  disabled?: boolean;
}) {
  return (
    <Select value={value} onValueChange={(next) => onValueChange(String(next ?? ''))} disabled={disabled}>
      <SelectTrigger className={cn('h-8 w-full', className)} aria-label={ariaLabel}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {options.map((option) => <SelectItem key={option.value} value={option.value} disabled={option.disabled}>{option.label}</SelectItem>)}
      </SelectContent>
    </Select>
  );
}

/**
 * 下拉筛选：首项固定「全部」，内部用 ``__all__`` 哨兵映射空串。
 * Base UI Select 的 value 需要非空值，故空筛选态用哨兵表示（与全站 ``__none__`` 用法一致）。
 */
export function FilterSelect({
  value, onValueChange, options, allLabel = '全部', ariaLabel, className = '', disabled = false,
}: {
  value: string;
  onValueChange: (value: string) => void;
  options: Array<{ value: string; label: string; disabled?: boolean }>;
  allLabel?: string;
  ariaLabel?: string;
  className?: string;
  disabled?: boolean;
}) {
  return (
    <SelectField
      value={value || '__all__'}
      onValueChange={(next) => onValueChange(next === '__all__' ? '' : next)}
      options={[{ value: '__all__', label: allLabel }, ...options]}
      ariaLabel={ariaLabel}
      className={className}
      disabled={disabled}
    />
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
