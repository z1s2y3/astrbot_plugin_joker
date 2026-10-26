"""Joker 群聊操作的紧凑控制台输出。"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from uuid import uuid4


_EVENT_LOCK = threading.RLock()
_EVENT_LISTENER_LOCK = threading.RLock()
_MAX_LOCAL_EVENTS = 1000
_EVENT_FILE_NAME = "console_events.jsonl"
_CONSOLE_MESSAGE_MAX_LENGTH = 2000
_console_event_listener = None
_console_event_listener_token = None
# The portal must explicitly opt in before the Windows client persists a
# readable message preview.  This is updated by ``web_sync`` after its
# authenticated exchange and defaults to disabled for both old portals and
# first startup.
_portal_message_content_enabled = False
_PORTAL_MESSAGE_CONTENT_LOCK = threading.RLock()
# Normal inbound/outbound records are persisted for Linux synchronization but
# are silent in the Windows terminal by default.  Set the environment flag to
# ``1`` only while diagnosing a live installation.
_SHOW_NORMAL_CONSOLE_EVENTS = str(
    os.environ.get("JOKER_SHOW_NORMAL_CONSOLE_LOGS", "")
).strip().lower() in {"1", "true", "yes", "on"}
_SENSITIVE_PREVIEW_PATTERNS = (
    # Joker license keys (current and historical shapes).
    re.compile(r"(?i)\bJK(?:-?[0-9]+)?-[0-9]{13}[0-9A-F]{12}\b"),
    # Do not mirror copied authorization headers or obvious credential pairs
    # into a customer-visible group page.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(?:token|password|密码|卡密)\s*[:=：]\s*\S+"),
)


def set_console_event_listener(listener):
    """Install the active plugin's notification for a persisted CMD event.

    The callback intentionally receives no message content, group data, or
    authorization material.  It is only a best-effort wake-up for the
    already-running outbound web sync loop.  A token prevents an old plugin
    instance from disconnecting a replacement during a hot reload.
    """
    if listener is not None and not callable(listener):
        raise TypeError("console event listener must be callable or None")
    token = object() if listener is not None else None
    global _console_event_listener, _console_event_listener_token
    with _EVENT_LISTENER_LOCK:
        _console_event_listener = listener
        _console_event_listener_token = token
    return token


def clear_console_event_listener(token=None) -> None:
    """Clear a listener without letting a stale plugin clear a newer one."""
    global _console_event_listener, _console_event_listener_token
    with _EVENT_LISTENER_LOCK:
        if token is None or token is _console_event_listener_token:
            _console_event_listener = None
            _console_event_listener_token = None


def set_portal_message_content_enabled(enabled: bool) -> None:
    """Set whether new CMD events may retain a portal message preview.

    Only the authenticated outbound sync client calls this setter.  Keeping
    the flag in memory means an administrator can turn message tracking off
    without leaving fresh chat content in the local sync journal.
    """

    global _portal_message_content_enabled
    with _PORTAL_MESSAGE_CONTENT_LOCK:
            _portal_message_content_enabled = bool(enabled)


def set_normal_console_output_enabled(enabled: bool) -> None:
    """Toggle human-readable normal event output without changing storage."""
    global _SHOW_NORMAL_CONSOLE_EVENTS
    _SHOW_NORMAL_CONSOLE_EVENTS = bool(enabled)


def _portal_message_content_is_enabled() -> bool:
    with _PORTAL_MESSAGE_CONTENT_LOCK:
        return _portal_message_content_enabled


def _notify_console_event_listener() -> None:
    """Wake the portal loop after a sanitized event is safely on disk."""
    with _EVENT_LISTENER_LOCK:
        listener = _console_event_listener
    if listener is None:
        return
    try:
        listener()
    except Exception:
        # Message delivery has already succeeded and the periodic heartbeat
        # will still upload this event.  Logging must not affect QQ handling.
        return


def _event_file() -> Path:
    # Import lazily so standalone unit tests can load this file without AstrBot.
    from .group_config import get_plugin_data_path

    return Path(get_plugin_data_path()) / _EVENT_FILE_NAME


def _clean_event_text(value: object, limit: int) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:limit]


def _redact_portal_message_preview(value: str) -> str:
    """Remove credential-looking fragments from a readable message preview."""

    result = value
    for pattern in _SENSITIVE_PREVIEW_PATTERNS:
        result = pattern.sub("[已隐藏]", result)
    return result


def _read_recent_events_unlocked(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    events: list[dict] = []
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _write_events_unlocked(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".jsonl.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        for event in events[-_MAX_LOCAL_EVENTS:]:
            file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            file.write("\n")
    os.replace(temporary, path)


def _append_console_event(
    action: str,
    group_id: object,
    *,
    message=None,
    user_id: object = "",
    user_name: object = "",
    message_id: object = "",
    detail: str = "",
    direction: str = "",
) -> bool:
    """Append a bounded event for the outbound portal sync client.

    A readable ``message_content`` is present only after the portal's
    administrator has enabled message tracking.  Asset URLs and file paths
    are never rendered by ``render_group_message_for_console``.
    """
    group = _clean_event_text(group_id, 32)
    if not group.isdigit():
        return False
    normalized_direction = _clean_event_text(direction, 16).lower()
    if normalized_direction not in {"inbound", "outbound"}:
        normalized_direction = (
            "inbound"
            if str(action or "").strip() == "接收" and message is not None
            else ("outbound" if message is not None else "")
        )
    message_content = ""
    if message is not None and _portal_message_content_is_enabled():
        message_content = _redact_portal_message_preview(
            render_group_message_for_console(message, _CONSOLE_MESSAGE_MAX_LENGTH)
        )
        message_content = _clean_event_text(message_content, _CONSOLE_MESSAGE_MAX_LENGTH)
    event = {
        "event_id": uuid4().hex,
        "timestamp": int(time.time()),
        "group_id": group,
        "action": _clean_event_text(action, 64),
        "user_id": _clean_event_text(user_id, 32),
        "user_name": _clean_event_text(user_name, 128),
        "message_id": _clean_event_text(message_id, 64),
        "message_type": summarize_group_message(message) if message is not None else "",
        "detail": _clean_event_text(detail, 160),
        "direction": normalized_direction,
        "message_content": message_content,
    }
    try:
        with _EVENT_LOCK:
            path = _event_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Normal message handling only appends one tiny line.  Compacting
            # occurs occasionally, so disk logging cannot noticeably delay a
            # QQ reply or a moderation action.
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                file.write("\n")
            if path.stat().st_size > 512 * 1024:
                _write_events_unlocked(
                    path, _read_recent_events_unlocked(path, _MAX_LOCAL_EVENTS)
                )
        return True
    except Exception:
        # Console persistence must never make a QQ management action fail.
        return False


def get_console_events(limit: int = 100) -> list[dict]:
    """Read a bounded snapshot for the portal sync client."""
    safe_limit = max(1, min(_MAX_LOCAL_EVENTS, int(limit or 100)))
    try:
        with _EVENT_LOCK:
            return _read_recent_events_unlocked(_event_file(), safe_limit)
    except Exception:
        return []


def _segment_kind(segment) -> str:
    if isinstance(segment, str):
        return "text"
    if isinstance(segment, dict):
        kind = str(segment.get("type", "message") or "message").lower()
    else:
        kind = type(segment).__name__.lower() or "message"
    if any(token in kind for token in ("json", "music", "card", "xml")):
        return "card"
    return {
        "plain": "text",
        "at": "at",
        "image": "image",
        "face": "face",
        "reply": "reply",
    }.get(kind, kind)


def _message_segments(message) -> tuple:
    """Normalize OneBot and AstrBot message containers into ordered segments."""
    if isinstance(message, dict) and "message" in message:
        message = message.get("message")
    elif hasattr(message, "chain"):
        message = getattr(message, "chain", message)

    if isinstance(message, (list, tuple)):
        return tuple(message)
    return (message,)


def _console_at_target(value: object) -> str:
    target = str(value or "").strip()
    if target.lower() in {"all", "all_members"}:
        return "@全体成员"
    return f"@{target}" if target else "@成员"


def _render_console_segment(segment) -> str:
    """Render a local-only readable representation without asset locations."""
    if segment is None:
        return ""
    if isinstance(segment, str):
        return segment

    if isinstance(segment, dict):
        kind = _segment_kind(segment)
        data = segment.get("data", {})
        if not isinstance(data, dict):
            data = {}
        if kind in {"text", "plain"}:
            return str(data.get("text", segment.get("text", "")) or "")
        if kind in {"at", "at_all"}:
            return _console_at_target(
                data.get("qq", data.get("user_id", data.get("target", "")))
            )
        if kind == "image":
            return "[图片]"
        if kind == "face":
            face = data.get("name", data.get("id", ""))
            return f"[表情:{face}]" if face not in (None, "") else "[表情]"
        if kind in {"json", "card", "music", "xml"}:
            title = _card_visible_title(data)
            return f"[卡片:{title}]" if title else "[卡片]"
        labels = {
            "reply": "[回复]",
            "record": "[语音]",
            "video": "[视频]",
            "file": "[文件]",
        }
        return labels.get(kind, f"[{summarize_group_message(segment)}]")

    text = getattr(segment, "text", None)
    if text is not None:
        return str(text)

    kind = _segment_kind(segment)
    if kind in {"at", "at_all"}:
        for attribute in ("qq", "user_id", "target"):
            target = getattr(segment, attribute, None)
            if target not in (None, ""):
                return _console_at_target(target)
        return "@成员"
    labels = {
        "image": "[图片]",
        "face": "[表情]",
        "reply": "[回复]",
        "record": "[语音]",
        "video": "[视频]",
        "file": "[文件]",
    }
    if kind in {"json", "card", "music", "xml"}:
        title = _card_visible_title(segment)
        return f"[卡片:{title}]" if title else "[卡片]"
    return labels.get(kind, f"[{summarize_group_message(segment)}]")


def _card_visible_title(value: object) -> str:
    """Extract a short human-readable card title without printing raw JSON."""
    candidates = []
    if isinstance(value, dict):
        candidates.extend(value.get(key) for key in ("title", "prompt", "summary", "desc", "text"))
        nested = value.get("data")
        if isinstance(nested, dict):
            candidates.extend(
                nested.get(key) for key in ("title", "prompt", "summary", "desc", "text")
            )
        elif isinstance(nested, str):
            try:
                decoded = json.loads(nested)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, dict):
                candidates.extend(
                    decoded.get(key)
                    for key in ("title", "prompt", "summary", "desc", "text")
                )
    else:
        for key in ("title", "prompt", "summary", "desc", "text"):
            candidates.append(getattr(value, key, None))
        nested = getattr(value, "data", None)
        if isinstance(nested, dict):
            candidates.extend(
                nested.get(key) for key in ("title", "prompt", "summary", "desc", "text")
            )
    for candidate in candidates:
        text = _clean_event_text(candidate, 120)
        if text:
            return _redact_portal_message_preview(text)
    return ""


def render_group_message_for_console(message, limit: int = _CONSOLE_MESSAGE_MAX_LENGTH) -> str:
    """Return a readable, asset-location-free representation of a message.

    Newlines are rendered as ``\\n`` to keep one action on one CMD line.  It
    is used for Windows CMD output and, after an explicit portal opt-in, a
    bounded customer-visible preview.
    """
    rendered = "".join(_render_console_segment(segment) for segment in _message_segments(message))
    rendered = rendered.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    if not rendered:
        rendered = f"[{summarize_group_message(message)}]"
    safe_limit = max(1, int(limit or _CONSOLE_MESSAGE_MAX_LENGTH))
    if len(rendered) > safe_limit:
        return f"{rendered[:safe_limit]}…（内容过长，已截断）"
    return rendered


def summarize_group_message(message) -> str:
    """仅返回消息段类型，不输出正文、图片地址或文件地址。"""
    segments = _message_segments(message)

    labels = {
        "text": "文本",
        "at": "艾特",
        "at_all": "全体艾特",
        "image": "图片",
        "face": "表情",
        "reply": "回复",
        "record": "语音",
        "video": "视频",
        "file": "文件",
    }
    kinds: list[str] = []
    for segment in segments:
        label = labels.get(_segment_kind(segment), "消息")
        if label not in kinds:
            kinds.append(label)
    return "+".join(kinds) or "消息"


def print_group_action(
    action: str,
    group_id: object,
    *,
    message=None,
    user_id: object = "",
    user_name: object = "",
    message_id: object = "",
    detail: str = "",
    direction: str = "",
    force_output: bool = False,
) -> None:
    """Show a group action in CMD and persist a bounded portal event."""
    # Successful bot sends are useful for verifying command and admission
    # flows, while normal inbound traffic remains quiet unless opted in.
    action_text = str(action or "").strip()
    failure_markers = ("失败", "错误", "异常", "拒绝", "超时", "不可用", "跳过")
    normal_action = not any(marker in action_text for marker in failure_markers)
    should_print = (
        force_output
        or _SHOW_NORMAL_CONSOLE_EVENTS
        or not normal_action
        or action_text in {"发送", "发送并计划撤回"}
    )
    parts = [f"[群聊] {action}", f"群:{group_id}"]
    if user_id not in ("", None):
        parts.append(f"用户:{user_id}")
    if user_name not in ("", None):
        parts.append(f"昵称:{_clean_event_text(user_name, 128)}")
    if message is not None:
        parts.append(f"类型:{summarize_group_message(message)}")
        parts.append(f"内容:{render_group_message_for_console(message)}")
    if message_id not in ("", None, 0, "0"):
        parts.append(f"消息:{message_id}")
    if detail:
        parts.append(detail)
    if should_print:
        print(" | ".join(parts), flush=True)
    # Message history has its own durable queue.  Avoid duplicating every
    # normal chat line in the diagnostic journal unless the portal explicitly
    # opted into readable message previews.
    event_saved = False
    if not normal_action or _portal_message_content_is_enabled():
        event_saved = _append_console_event(
            action,
            group_id,
            message=message,
            user_id=user_id,
            user_name=user_name,
            message_id=message_id,
            detail=detail,
            direction=direction,
        )
    if event_saved:
        _notify_console_event_listener()


def print_private_action(
    action: str,
    user_id: object,
    *,
    message: object = "",
) -> None:
    """Show an explicit private-message send in the Windows terminal."""
    text = _clean_event_text(message, _CONSOLE_MESSAGE_MAX_LENGTH)
    for pattern in _SENSITIVE_PREVIEW_PATTERNS:
        text = pattern.sub("[已隐藏]", text)
    print(
        f"[私聊] {str(action or '发送').strip()} | 用户:{_clean_event_text(user_id, 32)}"
        + (f" | 内容:{text}" if text else ""),
        flush=True,
    )
