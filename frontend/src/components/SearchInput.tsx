import { Search, X } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

/**
 * 统一搜索输入框：左侧 Search 图标，非空时右侧 X 一键清除，Enter 立即提交。
 *
 * 回测报告列表与成交明细筛选栏共用，保证全站搜索交互一致（防抖由调用方负责）。
 */
export function SearchInput({
  value, onChange, onEnter, placeholder, ariaLabel, className = '', disabled = false,
}: {
  value: string;
  onChange: (value: string) => void;
  /** Enter 立即提交（跳过调用方的防抖等待）。 */
  onEnter?: () => void;
  placeholder?: string;
  ariaLabel?: string;
  className?: string;
  disabled?: boolean;
}) {
  return (
    <div className={cn('relative', className)}>
      <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
      <Input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => { if (event.key === 'Enter') onEnter?.(); }}
        placeholder={placeholder}
        aria-label={ariaLabel}
        disabled={disabled}
        className="pl-8 pr-8"
      />
      {value && (
        <button
          type="button"
          onClick={() => onChange('')}
          aria-label="清空输入"
          title="清空"
          className="absolute right-2 top-1/2 -translate-y-1/2 rounded text-muted-foreground transition-colors hover:text-foreground"
        >
          <X className="size-3.5" />
        </button>
      )}
    </div>
  );
}

export default SearchInput;
