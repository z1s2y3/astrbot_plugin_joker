"""Per-group member card and special-title rules.

The portal stores these rules as JSON-line strings for backwards compatible
forms, while Windows is the execution authority.  This module deliberately
contains no OneBot or web dependencies so both sides can validate the same
bounded shape without importing the plugin runtime.
"""

from __future__ import annotations

import json
import re
from typing import Any


MAX_RULES = 200
MAX_USER_ID_LENGTH = 20
MAX_MEMBER_NAME_LENGTH = 60
MAX_MEMBER_TITLE_LENGTH = 18
MAX_MEMBER_PREFIX_LENGTH = 20

# A wildcard rule is intentionally represented by one stable wire value.
# The portal accepts a few localized aliases for operator convenience, while
# Windows and the sync projection should exchange only ``*`` so a save/load
# round trip cannot silently discard the rule.
WILDCARD_USER_ID = "*"
WILDCARD_ALIASES = frozenset({"*", "all", "everyone", "全体", "所有人"})

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if not text or _CONTROL_RE.search(text):
        return ""
    return text[:limit]


def apply_member_name_prefix(prefix: Any, name: Any, limit: int = MAX_MEMBER_NAME_LENGTH) -> str:
    """Return a stable group-card name with one global prefix.

    The helper is idempotent so a refresh or repeated message cannot turn
    ``scum-张先生`` into ``scum-scum-张先生``.  Prefixes and names are kept
    within the OneBot group-card limit before being sent to QQ.
    """
    try:
        max_length = max(1, min(int(limit), MAX_MEMBER_NAME_LENGTH))
    except (TypeError, ValueError, OverflowError):
        max_length = MAX_MEMBER_NAME_LENGTH
    clean_prefix = _clean_text(prefix, MAX_MEMBER_PREFIX_LENGTH)
    clean_name = _clean_text(name, max_length) or "成员"
    if not clean_prefix:
        return clean_name[:max_length]
    while clean_name.startswith(clean_prefix) and clean_name != clean_prefix:
        clean_name = clean_name[len(clean_prefix):].lstrip()
    return (clean_prefix + clean_name)[:max_length]


def strip_member_name_prefix(prefix: Any, name: Any, limit: int = MAX_MEMBER_NAME_LENGTH) -> str:
    """Remove every leading copy of one configured prefix from a name.

    Prefix changes must be migrations, not concatenations.  Repeating the
    removal also repairs legacy cards such as ``scum-scum-player`` when the
    operator clears or changes the current prefix.
    """
    try:
        max_length = max(1, min(int(limit), MAX_MEMBER_NAME_LENGTH))
    except (TypeError, ValueError, OverflowError):
        max_length = MAX_MEMBER_NAME_LENGTH
    clean_prefix = _clean_text(prefix, MAX_MEMBER_PREFIX_LENGTH)
    clean_name = _clean_text(name, max_length)
    if not clean_prefix:
        return clean_name[:max_length]
    while clean_name.startswith(clean_prefix) and clean_name != clean_prefix:
        clean_name = clean_name[len(clean_prefix):].lstrip()
    return clean_name[:max_length] or "成员"


def _clean_user_id(value: Any) -> str:
    text = str(value or "").strip()
    if text.casefold() in WILDCARD_ALIASES:
        return WILDCARD_USER_ID
    if not re.fullmatch(r"[0-9]+", text) or len(text) > MAX_USER_ID_LENGTH or int(text) <= 0:
        return ""
    return text


def normalize_member_rule(value: Any, kind: str) -> dict[str, str] | None:
    """Normalize one rule to ``{"user_id", "name/title"}``.

    Accepted input is a mapping, a JSON object string, or the compact
    ``QQ=value`` form shown in the web form.  Invalid rows are ignored by
    ``normalize_member_rules`` instead of making a malformed legacy group file
    prevent all other group-management features from running.
    """

    if kind not in {"name", "title"}:
        raise ValueError("kind must be name or title")
    item: Any = value
    if isinstance(item, str):
        item = item.strip()
        if not item:
            return None
        if item.startswith("{"):
            try:
                item = json.loads(item)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
        else:
            # Keep the right hand side intact; names/titles may contain ':' or
            # commas, so only the first '=' is treated as a separator.
            if "=" not in item:
                return None
            user_id, display = item.split("=", 1)
            item = {"user_id": user_id, kind: display}
    if not isinstance(item, dict):
        return None

    user_id = _clean_user_id(
        item.get("user_id")
        or item.get("userId")
        or item.get("qq")
        or item.get("id")
    )
    field_names = (kind, "name" if kind == "name" else "title")
    display = ""
    for field_name in field_names:
        if field_name in item:
            display = _clean_text(item.get(field_name), MAX_MEMBER_NAME_LENGTH if kind == "name" else MAX_MEMBER_TITLE_LENGTH)
            if display:
                break
    if not user_id or not display:
        return None
    return {"user_id": user_id, kind: display}


def normalize_member_rules(value: Any, kind: str) -> list[dict[str, str]]:
    """Return bounded, de-duplicated rules in stable input order.

    When a user appears more than once, the last row wins.  This makes an
    edited row behave predictably even when a browser submitted a stale copy
    of an earlier row.
    """

    if isinstance(value, str):
        serialized = value.strip()
        if serialized.startswith("["):
            try:
                parsed = json.loads(serialized)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            raw_items = parsed if isinstance(parsed, list) else serialized.splitlines()
        else:
            raw_items = serialized.splitlines()
    elif isinstance(value, (bytes, dict)):
        raw_items = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_items = value
    else:
        raw_items = ()
    by_user: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for raw in list(raw_items)[:MAX_RULES]:
        rule = normalize_member_rule(raw, kind)
        if rule is None:
            continue
        user_id = rule["user_id"]
        if user_id not in by_user:
            order.append(user_id)
        by_user[user_id] = rule
    return [by_user[user_id] for user_id in order]


def member_rule_for_user(value: Any, kind: str, user_id: Any) -> dict[str, str] | None:
    target = _clean_user_id(user_id)
    # A caller asks for a concrete member.  Wildcard is a rule, not a valid
    # target member ID, so resolve the exact rule first and then fall back to
    # the canonical wildcard entry.
    if not target or target == WILDCARD_USER_ID:
        return None
    rules = normalize_member_rules(value, kind)
    exact = next((rule for rule in rules if rule["user_id"] == target), None)
    if exact is not None:
        return exact
    return next((rule for rule in rules if rule["user_id"] == WILDCARD_USER_ID), None)


def rules_as_json_lines(value: Any, kind: str) -> list[str]:
    """Canonical representation used by the web form and sync projection."""

    return [json.dumps(rule, ensure_ascii=False, separators=(",", ":")) for rule in normalize_member_rules(value, kind)]


__all__ = [
    "MAX_RULES",
    "MAX_MEMBER_NAME_LENGTH",
    "MAX_MEMBER_TITLE_LENGTH",
    "MAX_MEMBER_PREFIX_LENGTH",
    "WILDCARD_USER_ID",
    "WILDCARD_ALIASES",
    "normalize_member_rule",
    "normalize_member_rules",
    "member_rule_for_user",
    "rules_as_json_lines",
    "apply_member_name_prefix",
]
