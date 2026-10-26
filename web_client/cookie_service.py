"""Persistent browser-session cookie policy for the Linux portal.

The environment variable remains the first-install default.  Administrators
can override it from the web UI without editing a root-owned ``.env`` file;
the value is kept in the portal database and is never sent to Windows.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .models import PortalSetting


SESSION_COOKIE_SETTING_KEY = "session_cookie_policy"
MIN_SESSION_MAX_AGE = 300
MAX_SESSION_MAX_AGE = 2_592_000


def normalize_session_max_age(value: Any, default: int) -> int:
    """Return a bounded cookie lifetime in seconds."""

    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = int(default)
    return max(MIN_SESSION_MAX_AGE, min(MAX_SESSION_MAX_AGE, parsed))


def get_session_max_age(db: Session, fallback: int) -> int:
    """Read the database override, falling back safely to environment config."""

    setting = db.get(PortalSetting, SESSION_COOKIE_SETTING_KEY)
    payload = setting.value_json if setting is not None else None
    value = payload.get("max_age") if isinstance(payload, dict) else None
    if value is None:
        return normalize_session_max_age(fallback, fallback)
    return normalize_session_max_age(value, fallback)


def save_session_max_age(db: Session, seconds: Any, operator_id: int) -> int:
    """Persist a validated lifetime and return the normalized value."""

    normalized = normalize_session_max_age(seconds, 604800)
    setting = db.get(PortalSetting, SESSION_COOKIE_SETTING_KEY)
    if setting is None:
        setting = PortalSetting(
            key=SESSION_COOKIE_SETTING_KEY,
            value_json={"max_age": normalized},
            updated_by=operator_id,
        )
        db.add(setting)
    else:
        setting.value_json = {"max_age": normalized}
        setting.updated_by = operator_id
    return normalized


def format_session_max_age(seconds: Any) -> str:
    """Render a bounded duration for the administrator page."""

    value = normalize_session_max_age(seconds, 604800)
    days, remainder = divmod(value, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    if secs or not parts:
        parts.append(f"{secs} 秒")
    return " ".join(parts)
