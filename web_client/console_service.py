"""Access rules and filtering for the customer-facing runtime console."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from .models import PortalSetting, User


CONSOLE_SETTING_KEY = "console_display"
DEFAULT_CONSOLE_KEYWORDS = (
    "发送",
    "撤回",
    "禁言",
    "解除禁言",
    "踢出",
    "入群审核",
    "入群欢迎",
    "自动回复",
    "签到",
    "定时消息",
    "关键词过滤",
    "SCUM查询",
    "授权",
    "开机",
    "关机",
    "图片",
    "系统错误",
)
MAX_KEYWORDS = 100
MAX_KEYWORD_LENGTH = 64


def normalize_console_keywords(raw: Any) -> list[str]:
    """Normalize newline/comma separated keywords without storing user text."""
    if raw is None:
        return []
    if isinstance(raw, str):
        values: Iterable[Any] = raw.replace("，", ",").replace("\r", "\n").replace(",", "\n").split("\n")
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        return []
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item:
            continue
        item = item[:MAX_KEYWORD_LENGTH]
        if item not in result:
            result.append(item)
        if len(result) >= MAX_KEYWORDS:
            break
    return result


def get_console_settings(db: Session) -> dict[str, Any]:
    setting = db.get(PortalSetting, CONSOLE_SETTING_KEY)
    payload: Mapping[str, Any] = (
        setting.value_json if setting and isinstance(setting.value_json, dict) else {}
    )
    keywords = normalize_console_keywords(payload.get("keywords"))
    try:
        retention = int(payload.get("retention", 2000) or 2000)
    except (TypeError, ValueError):
        retention = 2000
    enabled = bool(payload.get("enabled", False))
    messages_enabled = enabled and bool(payload.get("messages_enabled", False))
    return {
        "enabled": enabled,
        # Message bodies are opt-in independently from the existing action
        # log.  Old portal databases therefore keep their previous
        # privacy-limited behaviour after an upgrade.
        "messages_enabled": messages_enabled,
        "retention": max(100, min(10000, retention)),
        "keywords": keywords,
        "suggested_keywords": list(DEFAULT_CONSOLE_KEYWORDS),
    }


def save_console_settings(
    db: Session,
    *,
    enabled: bool,
    messages_enabled: bool = False,
    retention: Any,
    keywords: Any,
    operator_id: int | None,
) -> dict[str, Any]:
    try:
        retention_value = int(str(retention or "2000"))
    except (TypeError, ValueError):
        raise ValueError("日志保留数量必须是数字") from None
    if not 100 <= retention_value <= 10000:
        raise ValueError("日志保留数量必须在 100-10000 之间")
    normalized = normalize_console_keywords(keywords)
    setting = db.get(PortalSetting, CONSOLE_SETTING_KEY)
    effective_messages_enabled = bool(enabled) and bool(messages_enabled)
    payload = {
        "enabled": bool(enabled),
        "messages_enabled": effective_messages_enabled,
        "retention": retention_value,
        # An empty list intentionally means不按关键词过滤; the defaults are
        # shown only as administrator suggestions in the UI.
        "keywords": normalized,
    }
    if setting is None:
        db.add(PortalSetting(key=CONSOLE_SETTING_KEY, value_json=payload, updated_by=operator_id))
    else:
        setting.value_json = payload
        setting.updated_by = operator_id
    return {
        "enabled": bool(enabled),
        "messages_enabled": effective_messages_enabled,
        "retention": retention_value,
        "keywords": normalized,
        "suggested_keywords": list(DEFAULT_CONSOLE_KEYWORDS),
    }


def user_can_view_console(user: User, settings: Mapping[str, Any]) -> bool:
    return bool(settings.get("enabled")) and bool(user.is_admin or user.can_view_console)
