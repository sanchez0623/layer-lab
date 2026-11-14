"""通用后台分页助手。

约定（写入 AGENTS.md 项目记忆）：任何「可能随时间变长」的列表——回测换票轨迹、
成交明细、委托记录、历史运行、持仓流水等——后端一律走这里做服务端分页切片，
不要一次性把全量数据塞进响应再让前端自行截断，避免大数据量下的序列化 / 传输 /
渲染性能问题。

用法（FastAPI 端点）::

    from .pagination import paginate, page_params

    @application.get("/api/xxx/{xxx_id}/items")
    def list_items(xxx_id: str, params: PageParams = Depends(page_params)):
        items = load_items(xxx_id)            # 全量（或 DB 侧已排序）
        return paginate(items, params.page, params.page_size)

返回结构统一为::

    {
        "items": [...],        # 当前页切片
        "total": 184,          # 总条数
        "page": 1,             # 当前页（1 起）
        "page_size": 20,       # 每页条数
        "total_pages": 10,     # 总页数
        "has_prev": False,     # 是否有上一页
        "has_next": True,      # 是否有下一页
    }

前端配套组件见 ``frontend/src/components/PaginationBar.tsx``。
"""

from __future__ import annotations

from typing import Any, Sequence

from fastapi import Query

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200


def page_params(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="每页条数"
    ),
) -> tuple[int, int]:
    """FastAPI 依赖：从查询串解析并校验 ``page`` / ``page_size``。

    端点里用 ``params: tuple[int, int] = Depends(page_params)`` 引入，
    或直接 ``page: int = Query(...)`` 后调 :func:`paginate` 也可。
    """
    return page, page_size


def paginate(
    items: Sequence[Any],
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """把全量 ``items`` 切成统一的一页结构。

    越界自动收敛到合法页：``page`` 超过总页数回落到最后一页，小于 1 视为 1；
    空集合返回 ``page=1, total_pages=0``。
    """
    total = len(items)
    size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    total_pages = (total + size - 1) // size
    current = max(1, min(int(page), total_pages)) if total_pages > 0 else 1
    start = (current - 1) * size
    window = list(items[start:start + size])
    return {
        "items": window,
        "total": total,
        "page": current,
        "page_size": size,
        "total_pages": total_pages,
        "has_prev": current > 1,
        "has_next": current < total_pages,
    }
