"""Small, dependency-free pagination helpers shared by portal views.

The web portal intentionally keeps pagination deterministic and server-side for
large tables.  This module has no database or template dependencies so the
normalisation rules can be tested without starting FastAPI.
"""

from __future__ import annotations

from math import ceil
from typing import Any, Iterable, Sequence


DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def parse_page(value: Any, *, default: int = 1, maximum: int = 10_000) -> int:
    """Return a safe one-based page number for an untrusted query value."""

    try:
        page = int(str(value or "").strip())
    except (TypeError, ValueError, OverflowError):
        page = default
    return max(1, min(maximum, page))


def parse_page_size(
    value: Any,
    *,
    default: int = DEFAULT_PAGE_SIZE,
    maximum: int = MAX_PAGE_SIZE,
) -> int:
    """Return a bounded page size.

    The portal currently supplies fixed sizes from its routes, but accepting a
    query value makes the helper useful for future tables while keeping a
    malicious ``size`` from allocating an unbounded response.
    """

    try:
        size = int(str(value or "").strip())
    except (TypeError, ValueError, OverflowError):
        size = default
    return max(1, min(maximum, size))


def page_numbers(page: int, page_count: int, *, max_links: int = 7) -> list[int]:
    """Return compact page-number links (with ``0`` as an ellipsis marker)."""

    page_count = max(1, int(page_count or 1))
    page = max(1, min(page_count, int(page or 1)))
    max_links = max(3, int(max_links or 7))
    if page_count <= max_links:
        return list(range(1, page_count + 1))
    interior_slots = max_links - 2
    start = max(2, page - (interior_slots // 2))
    end = min(page_count - 1, start + interior_slots - 1)
    while end - start + 1 < interior_slots:
        if start > 2:
            start -= 1
        elif end < page_count - 1:
            end += 1
        else:
            break
    result = [1]
    if start > 2:
        result.append(0)  # template renders this as an ellipsis
    result.extend(range(start, end + 1))
    if end < page_count - 1:
        result.append(0)
    result.append(page_count)
    return result


def paginate(
    items: Sequence[Any] | Iterable[Any],
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
    *,
    max_links: int = 7,
) -> dict[str, Any]:
    """Slice an in-memory sequence and return template-ready metadata."""

    values = list(items)
    metadata = pagination_metadata(
        len(values), page, page_size, max_links=max_links
    )
    size = metadata["page_size"]
    current = metadata["page"]
    start = (current - 1) * size
    metadata["items"] = values[start : start + size]
    return metadata


def pagination_metadata(
    total: Any,
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
    *,
    max_links: int = 7,
) -> dict[str, Any]:
    """Return pager metadata when the database performs the row slicing."""

    try:
        safe_total = max(0, int(total))
    except (TypeError, ValueError, OverflowError):
        safe_total = 0
    size = parse_page_size(page_size)
    page_count = max(1, ceil(safe_total / size))
    current = min(parse_page(page), page_count)
    return {
        "page": current,
        "page_size": size,
        "total": safe_total,
        "page_count": page_count,
        "page_numbers": page_numbers(current, page_count, max_links=max_links),
        "has_previous": current > 1,
        "has_next": current < page_count,
        "previous_page": current - 1 if current > 1 else None,
        "next_page": current + 1 if current < page_count else None,
    }
