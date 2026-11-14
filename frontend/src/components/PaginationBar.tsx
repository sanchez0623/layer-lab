import { useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';

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
    <div className={`pagination-bar ${className}`} role="navigation" aria-label="分页">
      <span className="pagination-summary">
        第 <strong>{from}</strong>–<strong>{to}</strong> 条 / 共 <strong>{total}</strong> 条
      </span>

      <div className="pagination-controls">
        <button
          type="button"
          className="page-btn"
          onClick={() => goto(page - 1)}
          disabled={disabled || !hasPrev(page)}
          aria-label="上一页"
        >
          <ChevronLeft size={15} />上一页
        </button>

        <span className="page-indicator">
          第 <strong>{page}</strong> / {Math.max(totalPages, 1)} 页
        </span>

        <button
          type="button"
          className="page-btn"
          onClick={() => goto(page + 1)}
          disabled={disabled || !hasNext(page, totalPages)}
          aria-label="下一页"
        >
          下一页<ChevronRight size={15} />
        </button>

        <span className="page-jump">
          <input
            type="number"
            min={1}
            max={Math.max(totalPages, 1)}
            value={jump}
            disabled={disabled || totalPages <= 1}
            onChange={(event) => setJump(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') submitJump(); }}
            onBlur={submitJump}
            aria-label="跳转到页"
          />
          <button
            type="button"
            className="page-btn"
            onClick={submitJump}
            disabled={disabled || totalPages <= 1}
          >
            跳转
          </button>
        </span>

        {onPageSizeChange && (
          <label className="page-size">
            每页
            <select
              value={pageSize}
              disabled={disabled}
              onChange={(event) => onPageSizeChange(Number(event.target.value))}
              aria-label="每页条数"
            >
              {pageSizeOptions.map((size) => <option key={size} value={size}>{size}</option>)}
            </select>
          </label>
        )}
      </div>
    </div>
  );
}

function hasPrev(page: number): boolean { return page > 1; }
function hasNext(page: number, totalPages: number): boolean { return page < totalPages; }

export default PaginationBar;
