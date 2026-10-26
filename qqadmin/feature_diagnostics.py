"""Bounded, group-scoped diagnostics for Windows-executed features.

The web portal is a read-only viewer for this data.  Keep entries compact and
sanitized because they travel through the runtime summary heartbeat.
"""

from __future__ import annotations

import re
import time
from uuid import uuid4
from typing import Any

try:  # AstrBot supplies the production logger.
    from astrbot.api import logger
except ImportError:  # pragma: no cover - standalone tests
    try:
        from ..runtime_compat import logger
    except ImportError:
        from runtime_compat import logger

try:
    from .group_config import get_group_data, update_group_data
except ImportError:  # pragma: no cover - direct module tests
    from group_config import get_group_data, update_group_data


FEATURE_ERROR_KEY = "feature_errors"
MAX_FEATURE_ERRORS = 30
MAX_TEXT_LENGTH = 240
_SAFE_GROUP_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _text(value: Any, limit: int = MAX_TEXT_LENGTH) -> str:
    """Flatten untrusted text before it enters a web-visible runtime row."""

    value = str(value or "").replace("\r", " ").replace("\n", " ")
    # Do not persist common credential-bearing fields if an adapter includes
    # them in an exception message.  Callers should still pass a short reason.
    value = re.sub(r"(?i)(password|passwd|token|secret|api[_-]?key)=([^\s,;]+)", r"\1=[redacted]", value)
    return value.strip()[:limit]


def _valid_group_id(group_id: Any) -> str:
    value = str(group_id or "").strip()
    if not _SAFE_GROUP_ID.fullmatch(value):
        raise ValueError("invalid group id")
    return value


def list_feature_errors(group_id: str, limit: int = MAX_FEATURE_ERRORS) -> list[dict[str, Any]]:
    """Return recent diagnostics in newest-first order."""

    try:
        normalized_group = _valid_group_id(group_id)
    except ValueError:
        return []
    try:
        size = max(1, min(int(limit or MAX_FEATURE_ERRORS), MAX_FEATURE_ERRORS))
    except (TypeError, ValueError, OverflowError):
        size = MAX_FEATURE_ERRORS
    raw = get_group_data(normalized_group, FEATURE_ERROR_KEY, [])
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)][-size:][::-1]


def record_feature_error(
    group_id: str,
    feature: str,
    code: str,
    message: str = "",
    *,
    operation: str = "",
    user_id: str = "",
) -> dict[str, Any] | None:
    """Persist one compact error and log the full exception at the caller.

    Identical consecutive failures are coalesced so a broken remote service
    cannot grow a group JSON file indefinitely.
    """

    try:
        normalized_group = _valid_group_id(group_id)
    except ValueError:
        return None
    entry = {
        "id": uuid4().hex[:16],
        "timestamp": max(0, int(time.time())),
        "feature": _text(feature, 64) or "unknown",
        "code": _text(code, 64) or "feature_error",
        "operation": _text(operation, 80),
        "message": _text(message),
        "user_id": _text(user_id, 32),
        "count": 1,
    }

    def mutate(previous: Any) -> list[dict[str, Any]]:
        rows = [row for row in previous if isinstance(row, dict)] if isinstance(previous, list) else []
        if rows:
            latest = rows[-1]
            same = all(latest.get(key, "") == entry[key] for key in ("feature", "code", "operation", "message", "user_id"))
            if same:
                updated = dict(latest)
                updated["timestamp"] = entry["timestamp"]
                try:
                    previous_count = int(updated.get("count", 1) or 1)
                except (TypeError, ValueError, OverflowError):
                    previous_count = 1
                updated["count"] = min(9999, max(1, previous_count) + 1)
                rows[-1] = updated
                return rows[-MAX_FEATURE_ERRORS:]
        rows.append(entry)
        return rows[-MAX_FEATURE_ERRORS:]

    try:
        update_group_data(normalized_group, FEATURE_ERROR_KEY, mutate)
    except Exception:
        logger.exception("保存功能错误诊断失败 group=%s feature=%s code=%s", normalized_group, entry["feature"], entry["code"])
        return None
    logger.warning(
        "功能执行失败 group=%s feature=%s code=%s operation=%s message=%s",
        normalized_group,
        entry["feature"],
        entry["code"],
        entry["operation"] or "-",
        entry["message"] or "-",
    )
    return entry


def clear_feature_errors(group_id: str) -> bool:
    """Clear local diagnostics; the next runtime heartbeat removes them from web."""

    try:
        normalized_group = _valid_group_id(group_id)
        update_group_data(normalized_group, FEATURE_ERROR_KEY, lambda _old: [])
        return True
    except Exception:
        logger.exception("清理功能错误诊断失败 group=%s", group_id)
        return False
