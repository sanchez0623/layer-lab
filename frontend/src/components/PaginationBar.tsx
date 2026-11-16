import { useEffect, useState } from 'react';
import { ChevronDownIcon, ChevronLeft, ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

/**
 * 通用后台分页控件（与 backend/app/pagination.py 的 paginate() 信封配套）。
 *
 * 项目约定（见 AGENTS.md「分页约定」）：任何「可能变长」的列表都用后台分页 +
 * 本控件，支持上一页 / 下一页 / 跳转指定页 / 切换每页条数，避免一次性渲染全量。
 *
 * 受控组件：分页状态由父级持有（page / pageSize），本控件只发起回调。
 */
export function PaginationBar({
  page,
  pageSize,
  total,
  totalPages,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = [10, 20, 50, 100],
  disabled = false,
  className = '',
}: {
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
  onPageChange: (page: number) => void;
  onPageSizeChange?: (size: number) => void;
  pageSizeOptions?: number[];
  disabled?: boolean;
  className?: string;
}) {
  const [jump, setJump] = useState(String(page));
  // 外部 page 变化（如切页大小后回落）时同步跳转输入框。
  useEffect(() => { setJump(String(page)); }, [page]);

  const clamp = (value: number) => Math.max(1, Math.min(value, Math.max(totalPages, 1)));
  const goto = (value: number) => {
    const target = clamp(value);
    if (!disabled && target !== page) onPageChange(target);
  };
  const submitJump = () => {
    const parsed = Number.parseInt(jump, 10);
    if (Number.isFinite(parsed)) goto(parsed);
    else setJump(String(page));
  };

  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(page * pageSize, total);

  return (
    <div className={`flex flex-col gap-3 border-t pt-3 text-xs text-muted-foreground sm:flex-row sm:items-center sm:justify-between ${className}`} role="navigation" aria-label="分页">
      <span>
        第 <strong className="font-mono font-medium text-foreground">{from}</strong>–<strong className="font-mono font-medium text-foreground">{to}</strong> 条 / 共 <strong className="font-mono font-medium text-foreground">{total}</strong> 条
      </span>

      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" size="sm" onClick={() => goto(page - 1)} disabled={disabled || page <= 1} aria-label="上一页">
          <ChevronLeft />上一页
        </Button>

        <span className="px-1">
          第 <strong className="font-mono font-medium text-foreground">{page}</strong> / {Math.max(totalPages, 1)} 页
        </span>

        <Button type="button" variant="outline" size="sm" onClick={() => goto(page + 1)} disabled={disabled || page >= totalPages} aria-label="下一页">
          下一页<ChevronRight />
        </Button>

        <span className="flex items-center gap-1.5">
          <Input
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            min={1}
            max={Math.max(totalPages, 1)}
            value={jump}
            disabled={disabled || totalPages <= 1}
            onChange={(event) => setJump(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') submitJump(); }}
            onBlur={submitJump}
            aria-label="跳转到页"
            className="h-7 w-16 px-2 font-mono text-xs"
          />
          <Button type="button" variant="outline" size="sm" onClick={submitJump} disabled={disabled || totalPages <= 1}>跳转</Button>
        </span>

        {onPageSizeChange && (
          <label className="flex items-center gap-1.5">
            每页
            <span className="relative inline-block">
              <select
                value={String(pageSize)}
                onChange={(event) => onPageSizeChange(Number(event.target.value))}
                disabled={disabled}
                aria-label="每页条数"
                className={cn(
                  'h-7 min-w-[64px] cursor-pointer appearance-none rounded-lg border border-input bg-transparent pl-2 pr-6 text-xs outline-none transition-colors',
                  'focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50',
                  'disabled:cursor-not-allowed disabled:opacity-50',
                  'dark:bg-input/30 dark:hover:bg-input/50',
                )}
              >
                {pageSizeOptions.map((size) => <option key={size} value={String(size)}>{size}</option>)}
              </select>
              <ChevronDownIcon className="pointer-events-none absolute right-1.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            </span>
          </label>
        )}
      </div>
    </div>
  );
}

export default PaginationBar;
