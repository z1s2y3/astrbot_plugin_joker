import asyncio
import base64
import hashlib
import os
import re
import time
import random
import tempfile
import threading
from pathlib import Path
from functools import lru_cache
from typing import Awaitable, Callable

try:  # AstrBot supplies the plugin logger in production.
    from astrbot.api import logger
except ImportError:  # Standalone persistence tests do not install AstrBot.
    try:
        from ..runtime_compat import logger
    except ImportError:
        from runtime_compat import logger

from .group_config import get_group_data, set_group_data, update_group_data


_COOLDOWN_LOCK = threading.RLock()
_RUNTIME_COOLDOWNS: dict[tuple[str, str], int] = {}
# Unlimited replies have no quota state that needs to survive a restart. Keep
# their display counter in memory instead of rewriting the full group JSON for
# every ordinary message.
_RUNTIME_REPLY_USAGE: dict[tuple[str, str], int] = {}
_REPLY_IMAGE_CACHE_PREFIX = "cache://"
_DEFAULT_REPLY_IMAGE_BYTES = 20 * 1024 * 1024
_DEFAULT_PARALLEL_IMAGE_DOWNLOADS = 4
_PORTAL_REPLY_ASSET_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_MAX_PORTAL_REPLY_ASSETS_PER_RULE = 20
_REPLY_TEMPLATE_TOKEN_RE = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}")
_REPLY_USAGE_LIMIT = 500


def normalize_reply_probability(value, default: int = 100) -> int:
    """Return one safe reply probability percentage (0-100)."""

    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def normalize_reply_max_uses(value, default: int = 0) -> int:
    """Return a safe lifetime reply-use cap; zero means unlimited."""

    try:
        return max(0, min(1_000_000, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def should_trigger_reply(reply_data: dict | None) -> bool:
    """Evaluate a configured response probability without mutating state."""

    probability = normalize_reply_probability(
        reply_data.get("probability", 100) if isinstance(reply_data, dict) else 100
    )
    return probability >= 100 or (
        probability > 0 and random.random() * 100 < probability
    )


def _reply_usage_key(keyword: object, reply_data: dict | None) -> str:
    reply_type = "exact"
    if isinstance(reply_data, dict):
        reply_type = str(reply_data.get("type", reply_type) or reply_type).strip()
    return f"{reply_type[:16]}:{str(keyword or '').strip()[:300]}"


def claim_reply_use(
    group_id: str, keyword: object, reply_data: dict | None
) -> tuple[bool, int]:
    """Atomically reserve one configured reply use in Windows-local state.

    Reply content is synchronized as part of the group configuration. Limited
    rules keep their quota in the separate ``reply_usage`` runtime key, while
    unlimited rules use an in-memory counter so a busy group does not rewrite
    the full JSON file for every automatic response.
    """

    maximum = normalize_reply_max_uses(
        reply_data.get("max_uses", 0) if isinstance(reply_data, dict) else 0
    )
    usage_key = _reply_usage_key(keyword, reply_data)
    if maximum <= 0:
        runtime_key = (str(group_id), usage_key)
        with _COOLDOWN_LOCK:
            current_count = int(_RUNTIME_REPLY_USAGE.get(runtime_key, 0) or 0) + 1
            _RUNTIME_REPLY_USAGE[runtime_key] = current_count
            if len(_RUNTIME_REPLY_USAGE) > _REPLY_USAGE_LIMIT * 4:
                oldest = next(iter(_RUNTIME_REPLY_USAGE), None)
                if oldest is not None:
                    _RUNTIME_REPLY_USAGE.pop(oldest, None)
        return True, current_count
    result = {"allowed": False, "count": 0}
    now = int(time.time())

    def _claim(existing):
        usage = dict(existing) if isinstance(existing, dict) else {}
        record = usage.get(usage_key)
        record = dict(record) if isinstance(record, dict) else {}
        try:
            previous_count = max(0, int(record.get("count", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            previous_count = 0
        if maximum and previous_count >= maximum:
            result["count"] = previous_count
            return usage

        current_count = previous_count + 1
        usage[usage_key] = {"count": current_count, "updated_at": now}
        if len(usage) > _REPLY_USAGE_LIMIT:
            ordered = sorted(
                usage.items(),
                key=lambda item: int(item[1].get("updated_at", 0) or 0)
                if isinstance(item[1], dict)
                else 0,
                reverse=True,
            )
            usage = dict(ordered[:_REPLY_USAGE_LIMIT])
        result["allowed"] = True
        result["count"] = current_count
        return usage

    update_group_data(group_id, "reply_usage", _claim)
    return bool(result["allowed"]), int(result["count"])


def render_reply_template(
    text: object,
    *,
    user_name: object = "",
    user_id: object = "",
    group_id: object = "",
    keyword: object = "",
    use_count: int = 0,
    now: float | None = None,
) -> str:
    """Expand the supported non-secret automatic-reply variables.

    Unknown tokens intentionally remain unchanged so future variables can be
    introduced without destroying a customer's existing rule.  ``str.format``
    is not used because it treats arbitrary braces as format syntax and can
    raise on user-authored messages.
    """

    timestamp = time.localtime(time.time() if now is None else now)
    resolved_name = str(user_name or user_id or "")
    values = {
        "name": resolved_name,
        "user_name": resolved_name,
        "qq": str(user_id or ""),
        "user_id": str(user_id or ""),
        "group": str(group_id or ""),
        "group_id": str(group_id or ""),
        "keyword": str(keyword or ""),
        "count": str(max(0, int(use_count or 0))),
        "reply_count": str(max(0, int(use_count or 0))),
        "time": time.strftime("%Y-%m-%d %H:%M:%S", timestamp),
        "date": time.strftime("%Y-%m-%d", timestamp),
    }

    def replace(match: re.Match) -> str:
        key = match.group(1)
        return values.get(key, match.group(0))

    return _REPLY_TEMPLATE_TOKEN_RE.sub(replace, str(text or ""))

# 回复数据
def get_group_replies(group_id: str) -> dict:
    return get_group_data(group_id, "replies", {})

def add_reply(group_id: str, keyword: str, reply: str, exact: bool = True,
              random_replies: list = None, regex_pattern: str = None) -> bool:
    def _add(replies):
        if replies is None:
            replies = {"exact": {}, "fuzzy": [], "regex": []}
        
        reply_data = {
            "reply": reply,
            "type": "exact"if exact else "fuzzy",
            "created_at": int(time.time())
        }
        
        if random_replies:
            reply_data["random_replies"] = random_replies
        
        if exact:
            replies["exact"][keyword] = reply_data
        else:
            if keyword not in replies["fuzzy"]:
                replies["fuzzy"].append(keyword)
            if "fuzzy_keywords"not in replies:
                replies["fuzzy_keywords"] = {}
            replies["fuzzy_keywords"][keyword] = reply_data
        
        return replies
    
    update_group_data(group_id, "replies", _add)
    return True

def add_regex_reply(group_id: str, pattern: str, reply: str) -> bool:
    try:
        re.compile(pattern)
    except re.error:
        return False
    
    def _add(replies):
        if replies is None:
            replies = {"exact": {}, "fuzzy": [], "regex": []}
        
        replies["regex"].append({
            "pattern": pattern,
            "reply": {
                "reply": reply,
                "type": "regex",
                "created_at": int(time.time())
            }
        })
        return replies
    
    update_group_data(group_id, "replies", _add)
    return True

def remove_reply(group_id: str, keyword: str, reply_type: str = "exact") -> bool:
    """Remove one reply rule and report whether it actually existed.

    ``reply_type="all"`` is used by the short ``/删除`` command.  Older
    releases always tried the exact-reply map and unconditionally returned
    ``True``; as a result a fuzzy/regex rule stayed in Windows while the bot
    told the operator it had been deleted.  Keep the explicit type used by the
    web editor, while allowing the group command to remove every matching
    representation in one atomic config update.
    """
    normalized_type = str(reply_type or "exact").strip().lower()
    if normalized_type not in {"exact", "fuzzy", "regex", "all"}:
        return False
    normalized_keyword = str(keyword or "").strip()
    if not normalized_keyword:
        return False

    removed = False

    def _remove(replies):
        nonlocal removed
        if replies is None:
            return {"exact": {}, "fuzzy": [], "regex": []}

        if not isinstance(replies, dict):
            return {"exact": {}, "fuzzy": [], "regex": []}

        if normalized_type in {"exact", "all"}:
            exact_replies = replies.get("exact")
            if isinstance(exact_replies, dict) and normalized_keyword in exact_replies:
                exact_replies.pop(normalized_keyword, None)
                removed = True

        if normalized_type in {"fuzzy", "all"}:
            fuzzy_replies = replies.get("fuzzy")
            if isinstance(fuzzy_replies, dict):
                if normalized_keyword in fuzzy_replies:
                    fuzzy_replies.pop(normalized_keyword, None)
                    removed = True
            elif isinstance(fuzzy_replies, list):
                filtered = [item for item in fuzzy_replies if item != normalized_keyword]
                if len(filtered) != len(fuzzy_replies):
                    replies["fuzzy"] = filtered
                    removed = True
            fuzzy_keywords = replies.get("fuzzy_keywords")
            if isinstance(fuzzy_keywords, dict) and normalized_keyword in fuzzy_keywords:
                fuzzy_keywords.pop(normalized_keyword, None)
                removed = True

        if normalized_type in {"regex", "all"}:
            regex_replies = replies.get("regex")
            if isinstance(regex_replies, dict):
                if normalized_keyword in regex_replies:
                    regex_replies.pop(normalized_keyword, None)
                    removed = True
            elif isinstance(regex_replies, list):
                filtered = [
                    item
                    for item in regex_replies
                    if not (
                        isinstance(item, dict)
                        and item.get("pattern") == normalized_keyword
                    )
                ]
                if len(filtered) != len(regex_replies):
                    replies["regex"] = filtered
                    removed = True
        
        return replies
    
    update_group_data(group_id, "replies", _remove)
    return removed

def get_reply(group_id: str, keyword: str) -> dict | None:
    """获取回复数据，返回完整的回复数据（包括文本、图片等）"""
    replies = get_group_replies(group_id)
    
    exact_replies = replies.get("exact", {})
    if keyword in exact_replies:
        reply_data = exact_replies[keyword]
        if "random_replies"in reply_data:
            return {"reply": random.choice(reply_data["random_replies"]), "has_images": False}
        return reply_data
    
    fuzzy_keywords = replies.get("fuzzy_keywords", {})
    for fuzzy_kw in replies.get("fuzzy", []):
        if fuzzy_kw in keyword and fuzzy_kw in fuzzy_keywords:
            reply_data = fuzzy_keywords[fuzzy_kw]
            if "random_replies"in reply_data:
                return {"reply": random.choice(reply_data["random_replies"]), "has_images": False}
            return reply_data
    
    for regex_entry in replies.get("regex", []):
        pattern = regex_entry.get("pattern", "")
        try:
            if re.search(pattern, keyword):
                reply_data = regex_entry.get("reply", {})
                if "random_replies"in reply_data:
                    return {"reply": random.choice(reply_data["random_replies"]), "has_images": False}
                return reply_data
        except re.error:
            continue
    
    return None

def get_reply_text(reply_data: dict | None) -> str | None:
    """从回复数据中获取纯文本内容"""
    if reply_data is None:
        return None
    return reply_data.get("reply")


def should_include_reply_text(reply_data: dict | None) -> bool:
    """Return whether an automatic-reply rule should send its text segment.

    A rule that contains an uploaded image is an image reply.  Its text field
    remains useful as an operator label in the web UI, but must not be sent to
    the group together with the image.  Text-only rules keep their existing
    behavior.
    """
    if not isinstance(reply_data, dict):
        return False
    if bool(reply_data.get("has_images")):
        return False
    for key in (
        "image_portal_assets",
        "image_cache_refs",
        "image_source_urls",
        "image_urls",
    ):
        value = reply_data.get(key)
        if isinstance(value, (list, tuple, set)) and any(value):
            return False
    return True

@lru_cache(maxsize=512)
def _compiled_reply_pattern(pattern: str):
    try:
        return re.compile(pattern)
    except re.error:
        return None


def match_any_reply(group_id: str, message: str, replies: dict | None = None) -> tuple:
    replies = replies if isinstance(replies, dict) else get_group_replies(group_id)
    
    exact_replies = replies.get("exact", {})
    if message in exact_replies:
        return message, exact_replies[message]
    
    fuzzy_keywords = replies.get("fuzzy_keywords", {})
    for fuzzy_kw in replies.get("fuzzy", []):
        if fuzzy_kw in message and fuzzy_kw in fuzzy_keywords:
            return fuzzy_kw, fuzzy_keywords[fuzzy_kw]
    
    for regex_entry in replies.get("regex", []):
        pattern = regex_entry.get("pattern", "")
        compiled = _compiled_reply_pattern(str(pattern))
        match = compiled.search(message) if compiled else None
        if match:
            reply_data = regex_entry.get("reply", {})
            return match.group(0), reply_data
    
    return None, None


def list_replies(group_id: str) -> dict:
    replies = get_group_replies(group_id)
    if not isinstance(replies, dict):
        replies = {}
    exact = replies.get("exact", {})
    exact_keys = list(exact.keys()) if isinstance(exact, dict) else []
    fuzzy = replies.get("fuzzy", [])
    if isinstance(fuzzy, dict):
        fuzzy_keys = list(fuzzy.keys())
    elif isinstance(fuzzy, (list, tuple)):
        fuzzy_keys = list(fuzzy)
    else:
        fuzzy_keys = []
    # Include payload-only legacy fuzzy entries while preserving the explicit
    # display order and avoiding duplicates.
    fuzzy_map = replies.get("fuzzy_keywords", {})
    if isinstance(fuzzy_map, dict):
        fuzzy_keys.extend(key for key in fuzzy_map if key not in fuzzy_keys)
    regex = replies.get("regex", [])
    if isinstance(regex, dict):
        regex_keys = list(regex.keys())
    elif isinstance(regex, (list, tuple)):
        regex_keys = [
            item.get("pattern") if isinstance(item, dict) else item
            for item in regex
        ]
    else:
        regex_keys = []
    return {
        "exact": exact_keys,
        "fuzzy": fuzzy_keys,
        "regex": regex_keys,
        "cooldown": replies.get("cooldown", 0)
    }


def iter_reply_entries(group_id: str, replies: dict | None = None):
    """Yield reply rules in the same order shown by ``list_replies``.

    The command UI historically displayed one-based rows, but the runtime
    only exposed keyword deletion.  Keeping the ordering in this module
    prevents the command and web editors from drifting when legacy fuzzy or
    regex containers are present.  Each yielded tuple is ``(kind, key)``;
    ``key`` is the exact/fuzzy keyword or the regex pattern.
    """
    source = replies if isinstance(replies, dict) else get_group_replies(group_id)
    exact = source.get("exact", {}) if isinstance(source, dict) else {}
    if isinstance(exact, dict):
        for key in exact:
            text = str(key) if key is not None else ""
            if text:
                yield "exact", text

    seen_fuzzy: set[str] = set()
    fuzzy = source.get("fuzzy", []) if isinstance(source, dict) else []
    if isinstance(fuzzy, dict):
        fuzzy_items = fuzzy.keys()
    elif isinstance(fuzzy, (list, tuple)):
        fuzzy_items = fuzzy
    else:
        fuzzy_items = ()
    for key in fuzzy_items:
        text = str(key) if key is not None else ""
        if text and text not in seen_fuzzy:
            seen_fuzzy.add(text)
            yield "fuzzy", text
    # Some old files only retained ``fuzzy_keywords``.  Include those rows
    # after the explicit order so they remain removable from the command UI.
    fuzzy_map = source.get("fuzzy_keywords", {}) if isinstance(source, dict) else {}
    if isinstance(fuzzy_map, dict):
        for key in fuzzy_map:
            text = str(key) if key is not None else ""
            if text and text not in seen_fuzzy:
                seen_fuzzy.add(text)
                yield "fuzzy", text

    regex = source.get("regex", []) if isinstance(source, dict) else []
    if isinstance(regex, dict):
        regex_items = regex.keys()
    elif isinstance(regex, (list, tuple)):
        regex_items = regex
    else:
        regex_items = ()
    for item in regex_items:
        if isinstance(item, dict):
            text = str(item.get("pattern")) if item.get("pattern") is not None else ""
        else:
            text = str(item) if item is not None else ""
        if text:
            yield "regex", text


def get_reply_by_index(group_id: str, index: object) -> tuple[str, str] | None:
    """Resolve a one-based command-list index to ``(kind, key)``."""
    try:
        number = int(str(index).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if number <= 0:
        return None
    for position, entry in enumerate(iter_reply_entries(group_id), 1):
        if position == number:
            return entry
    return None

def get_reply_stats(group_id: str) -> dict:
    replies = get_group_replies(group_id)
    exact = len(replies.get("exact", {}))
    fuzzy = len(replies.get("fuzzy", []))
    regex = len(replies.get("regex", []))
    
    return {
        "total": exact + fuzzy + regex,
        "exact": exact,
        "fuzzy": fuzzy,
        "regex": regex
    }

def clear_replies(group_id: str):
    set_group_data(group_id, "replies", {"exact": {}, "fuzzy": [], "regex": []})

# 冷却时间
def set_reply_cooldown(group_id: str, seconds: int):
    """保存回复冷却设置；消息热路径从群配置内存缓存读取。"""
    normalized_seconds = max(0, int(seconds))

    def _update(replies):
        if not isinstance(replies, dict):
            replies = {"exact": {}, "fuzzy": [], "regex": []}
        replies["cooldown"] = normalized_seconds
        return replies

    update_group_data(group_id, "replies", _update)
    if normalized_seconds <= 0:
        normalized_group_id = str(group_id)
        with _COOLDOWN_LOCK:
            for key in [key for key in _RUNTIME_COOLDOWNS if key[0] == normalized_group_id]:
                _RUNTIME_COOLDOWNS.pop(key, None)

def get_reply_cooldown(group_id: str, replies: dict | None = None) -> int:
    """从 group_config 的带 TTL 缓存读取冷却设置，避免每条消息直接读盘。"""
    replies = replies if isinstance(replies, dict) else get_group_replies(group_id)
    cd = replies.get("cooldown", 0) if isinstance(replies, dict) else 0
    try:
        return max(0, int(cd))
    except (ValueError, TypeError):
        return 0

def _cooldown_key(group_id: str, user_id: str | None) -> tuple[str, str]:
    return str(group_id), str(user_id or "_group")


def _prune_runtime_cooldowns(now: int, maximum_age: int) -> None:
    if len(_RUNTIME_COOLDOWNS) < 1000:
        return
    cutoff = now - max(60, maximum_age)
    for key in [key for key, timestamp in _RUNTIME_COOLDOWNS.items() if timestamp <= cutoff]:
        _RUNTIME_COOLDOWNS.pop(key, None)

def is_in_cooldown(
    group_id: str,
    keyword: str = None,
    user_id: str = None,
    cooldown_seconds: int | None = None,
) -> bool:
    """检查运行时冷却，不再为每次自动回复读写整份群配置。"""
    cd_seconds = (
        get_reply_cooldown(group_id)
        if cooldown_seconds is None
        else max(0, int(cooldown_seconds))
    )
    if cd_seconds <= 0:
        return False
    with _COOLDOWN_LOCK:
        last_time = _RUNTIME_COOLDOWNS.get(_cooldown_key(group_id, user_id), 0)
    return bool(last_time and int(time.time()) - last_time < cd_seconds)

def set_cooldown(
    group_id: str,
    keyword: str = None,
    user_id: str = None,
    cooldown_seconds: int | None = None,
):
    """设置内存冷却；插件重载后清空是预期行为，不阻塞正常群消息。"""
    now = int(time.time())
    cd_seconds = (
        get_reply_cooldown(group_id)
        if cooldown_seconds is None
        else max(0, int(cooldown_seconds))
    )
    if cd_seconds <= 0:
        return
    with _COOLDOWN_LOCK:
        _RUNTIME_COOLDOWNS[_cooldown_key(group_id, user_id)] = now
        _prune_runtime_cooldowns(now, cd_seconds)

def get_cooldown_remaining(
    group_id: str,
    user_id: str = None,
    cooldown_seconds: int | None = None,
) -> int:
    """获取指定用户的剩余冷却秒数（0表示未在冷却中）"""
    cd_seconds = (
        get_reply_cooldown(group_id)
        if cooldown_seconds is None
        else max(0, int(cooldown_seconds))
    )
    if cd_seconds <= 0:
        return 0
    with _COOLDOWN_LOCK:
        last_time = _RUNTIME_COOLDOWNS.get(_cooldown_key(group_id, user_id), 0)
    if last_time == 0:
        return 0
    elapsed = int(time.time()) - last_time
    remaining = cd_seconds - elapsed
    return max(0, remaining)

def load_replies(group_id: str) -> dict:
    return get_group_replies(group_id)

def save_replies(group_id: str, replies: dict):
    set_group_data(group_id, "replies", replies)

def add_random_replies(group_id: str, keyword: str, replies: list) -> bool:
    def _add(r):
        if r is None:
            r = {"exact": {}, "fuzzy": [], "regex": []}
        r["exact"][keyword] = {
            "reply": replies[0],
            "type": "exact",
            "random_replies": replies,
            "created_at": int(time.time())
        }
        return r
    update_group_data(group_id, "replies", _add)
    return True

def export_replies(group_id: str) -> dict:
    return get_group_replies(group_id)

def import_replies(group_id: str, data: dict):
    set_group_data(group_id, "replies", data)


def _normalize_image_source(value) -> str:
    source = str(value or "").strip()
    if source.startswith("file:///"):
        return source
    if source.startswith(("http://", "https://", "base64://", _REPLY_IMAGE_CACHE_PREFIX)):
        return source
    if source:
        return os.path.abspath(source)
    return ""


def _resolve_cache_reference(reference: str) -> str:
    reference = str(reference or "").strip()
    if reference.startswith(_REPLY_IMAGE_CACHE_PREFIX):
        relative = reference[len(_REPLY_IMAGE_CACHE_PREFIX):].replace("/", os.sep)
        if not relative or ".." in Path(relative).parts:
            return ""
        from .group_config import get_plugin_data_path

        return str(Path(get_plugin_data_path()) / relative)
    if reference.startswith("file:///"):
        return reference[8:]
    return reference


def resolve_reply_images(
    reply_data: dict | None,
    *,
    allow_remote_fallback: bool = True,
) -> list[str]:
    """返回可用图片，优先使用本地缓存。

    ``allow_remote_fallback`` 仅用于明确关闭本地缓存的旧兼容场景。
    图片缓存开启时，QQ 临时 URL 不能被再次发送：它可能已失效，且会造成
    OneBot 的富媒体传输失败。调用方应在本地文件尚未保存完成时提示稍后重试。
    """
    if not isinstance(reply_data, dict):
        return []
    legacy_values = reply_data.get("image_urls", [])
    legacy_values = legacy_values if isinstance(legacy_values, list) else []
    original_values = reply_data.get("image_source_urls", legacy_values)
    original_values = original_values if isinstance(original_values, list) else legacy_values
    cache_values = reply_data.get("image_cache_refs", [])
    cache_values = cache_values if isinstance(cache_values, list) else []

    resolved: list[str] = []
    length = max(len(legacy_values), len(original_values), len(cache_values))
    for index in range(length):
        cache_ref = cache_values[index] if index < len(cache_values) else ""
        legacy_ref = legacy_values[index] if index < len(legacy_values) else ""
        original_ref = original_values[index] if index < len(original_values) else ""
        preferred = cache_ref or legacy_ref
        local_path = _resolve_cache_reference(preferred)
        if local_path and not local_path.startswith(("http://", "https://", "base64://")):
            if os.path.isfile(local_path):
                resolved.append(local_path)
                continue
        fallback = original_ref or legacy_ref
        fallback_path = _resolve_cache_reference(fallback)
        if fallback_path and os.path.isfile(fallback_path):
            resolved.append(fallback_path)
        elif allow_remote_fallback and fallback.startswith(
            ("http://", "https://", "base64://")
        ):
            resolved.append(fallback)
    return resolved


def _normalize_portal_reply_assets(value: object) -> list[dict[str, object]]:
    """Return the strict, URL-free portal asset references for one reply.

    The Linux portal owns uploaded reply-image bytes.  Windows receives only
    an opaque asset id and its content hash in the synchronized group JSON;
    it must never receive a portal URL with an embedded credential or the
    image's base64 payload.  Invalid records are ignored rather than being
    allowed to turn a later cache operation into an arbitrary request.
    """
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for item in value[:_MAX_PORTAL_REPLY_ASSETS_PER_RULE]:
        if not isinstance(item, dict):
            continue
        try:
            asset_id = int(item.get("id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        digest = str(item.get("sha256", "") or "").strip().lower()
        if asset_id <= 0 or asset_id > 2**63 - 1:
            continue
        if not _PORTAL_REPLY_ASSET_SHA256_RE.fullmatch(digest):
            continue
        key = (asset_id, digest)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"id": asset_id, "sha256": digest})
    return normalized


def _iter_reply_entries(replies: object):
    """Yield stable ``(type, key, data)`` tuples for all reply layouts."""
    if not isinstance(replies, dict):
        return
    exact = replies.get("exact", {})
    if isinstance(exact, dict):
        for key, data in exact.items():
            if isinstance(data, dict):
                yield "exact", str(key), data

    fuzzy = replies.get("fuzzy", [])
    fuzzy_data = replies.get("fuzzy_keywords", {})
    if isinstance(fuzzy, list) and isinstance(fuzzy_data, dict):
        for key in fuzzy:
            data = fuzzy_data.get(key)
            if isinstance(data, dict):
                yield "fuzzy", str(key), data

    regex = replies.get("regex", [])
    if isinstance(regex, list):
        for item in regex:
            if not isinstance(item, dict):
                continue
            data = item.get("reply")
            if isinstance(data, dict):
                yield "regex", str(item.get("pattern", "")), data


def _find_reply_entry(replies: object, reply_type: str, key: str) -> dict | None:
    """Find a mutable reply record without changing the legacy layouts."""
    if not isinstance(replies, dict):
        return None
    if reply_type == "exact":
        data = replies.get("exact", {}).get(key) if isinstance(replies.get("exact"), dict) else None
        return data if isinstance(data, dict) else None
    if reply_type == "fuzzy":
        data = (
            replies.get("fuzzy_keywords", {}).get(key)
            if isinstance(replies.get("fuzzy_keywords"), dict)
            else None
        )
        return data if isinstance(data, dict) else None
    if reply_type == "regex":
        regex = replies.get("regex", [])
        if not isinstance(regex, list):
            return None
        for item in regex:
            if isinstance(item, dict) and str(item.get("pattern", "")) == key:
                data = item.get("reply")
                return data if isinstance(data, dict) else None
    return None


def _cache_ref_matches_portal_asset(reference: object, digest: str) -> bool:
    """Check whether a cache reference is one of our content-addressed files."""
    local_path = _resolve_cache_reference(str(reference or ""))
    if not local_path or local_path.startswith(("http://", "https://", "base64://")):
        return False
    try:
        path = Path(local_path)
        # ``_store_image_bytes`` uses the sha256 as the entire file stem.  This
        # cheap check avoids rereading images on every 30-second heartbeat.
        return path.is_file() and path.stem.lower() == digest
    except OSError:
        return False


def has_portal_reply_assets(group_id: str) -> bool:
    """Return whether a group's reply config references Linux-hosted images."""
    for _reply_type, _key, data in _iter_reply_entries(get_group_replies(group_id)):
        if _normalize_portal_reply_assets(data.get("image_portal_assets")):
            return True
    return False


async def cache_portal_reply_assets(
    group_id: str,
    fetch_asset: Callable[[int], Awaitable[tuple[bytes, str]]],
    *,
    max_image_bytes: int = _DEFAULT_REPLY_IMAGE_BYTES,
    download_concurrency: int = _DEFAULT_PARALLEL_IMAGE_DOWNLOADS,
) -> dict[str, int]:
    """Fetch portal-owned reply images and atomically populate local caches.

    ``fetch_asset`` is deliberately supplied by ``PortalSyncClient``.  This
    module therefore never knows the public portal domain or synchronization
    bearer token, and cannot accidentally follow a user-provided redirect with
    those credentials.  Existing URL/base64 image replies keep using
    :func:`download_and_persist_images`; only the new ``image_portal_assets``
    schema enters this path.
    """
    if not callable(fetch_asset):
        return {"rules": 0, "cached": 0, "failed": 0}
    try:
        max_image_bytes = max(1024 * 1024, min(int(max_image_bytes), 100 * 1024 * 1024))
    except (TypeError, ValueError):
        max_image_bytes = _DEFAULT_REPLY_IMAGE_BYTES
    try:
        download_concurrency = max(1, min(int(download_concurrency), 8))
    except (TypeError, ValueError):
        download_concurrency = _DEFAULT_PARALLEL_IMAGE_DOWNLOADS

    replies = get_group_replies(group_id)
    jobs: list[dict[str, object]] = []
    for reply_type, key, data in _iter_reply_entries(replies):
        assets = _normalize_portal_reply_assets(data.get("image_portal_assets"))
        if not assets:
            continue
        cached_refs = data.get("image_cache_refs", [])
        cached_refs = list(cached_refs) if isinstance(cached_refs, list) else []
        jobs.append(
            {
                "reply_type": reply_type,
                "key": key,
                "assets": assets,
                "cached_refs": cached_refs,
                "has_images": data.get("has_images"),
                "image_cache_status": data.get("image_cache_status"),
                "image_urls": data.get("image_urls"),
            }
        )
    if not jobs:
        return {"rules": 0, "cached": 0, "failed": 0}

    from .group_config import get_plugin_data_path, update_group_data

    images_dir = Path(get_plugin_data_path()) / "images" / str(group_id)
    images_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(download_concurrency)

    async def cache_job(job: dict[str, object]) -> dict[str, object]:
        assets = job["assets"]
        refs_before = job["cached_refs"]
        resolved_refs: list[str] = []
        cached = 0
        failed = 0
        for index, asset in enumerate(assets):
            asset_id = int(asset["id"])
            digest = str(asset["sha256"])
            existing_ref = refs_before[index] if index < len(refs_before) else ""
            if _cache_ref_matches_portal_asset(existing_ref, digest):
                resolved_refs.append(str(existing_ref))
                cached += 1
                continue
            try:
                async with semaphore:
                    payload = await fetch_asset(asset_id)
                if not isinstance(payload, tuple) or len(payload) != 2:
                    raise ValueError("invalid_portal_asset_payload")
                data, content_type = payload
                if not isinstance(data, bytes):
                    raise ValueError("invalid_portal_asset_bytes")
                if hashlib.sha256(data).hexdigest().lower() != digest:
                    raise ValueError("portal_asset_hash_mismatch")
                cache_ref = await asyncio.to_thread(
                    _store_image_bytes,
                    images_dir,
                    f"portal-asset-{asset_id}",
                    str(content_type or ""),
                    data,
                    max_image_bytes,
                )
                if not cache_ref:
                    raise ValueError("portal_asset_not_image")
                resolved_refs.append(cache_ref)
                cached += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Only retain a safe type label.  Remote URLs, bearer tokens,
                # and response bodies must never reach an AstrBot log.
                logger.warning(
                    "网页自动回复图片缓存失败: 群=%s 资源=%s 错误=%s",
                    group_id,
                    asset_id,
                    type(exc).__name__,
                )
                resolved_refs.append("")
                failed += 1
        status = "ready" if cached == len(assets) else "partial" if cached else "failed"
        return {
            **job,
            "refs": resolved_refs,
            "cached": cached,
            "failed": failed,
            "status": status,
        }

    completed = await asyncio.gather(*(cache_job(job) for job in jobs))
    cached_total = sum(int(job["cached"]) for job in completed)
    failed_total = sum(int(job["failed"]) for job in completed)
    now = int(time.time())

    def needs_update(job: dict[str, object]) -> bool:
        refs = list(job["refs"])
        if job.get("has_images") is not True:
            return True
        if job.get("cached_refs") != refs:
            return True
        if job.get("image_cache_status") != str(job["status"]):
            return True
        image_urls = job.get("image_urls")
        return not isinstance(image_urls, list) or not image_urls

    # A ready content-addressed cache is commonly checked after a normal
    # portal heartbeat.  It has no config mutation to persist, so avoid a
    # needless whole-file rewrite (which would trigger another outbound sync).
    if not any(needs_update(job) for job in completed):
        return {"rules": len(jobs), "cached": cached_total, "failed": failed_total}

    update_state = {"changed": False}

    def update_replies(current):
        if not isinstance(current, dict):
            return current
        changed = False
        for job in completed:
            entry = _find_reply_entry(
                current, str(job["reply_type"]), str(job["key"])
            )
            if entry is None:
                continue
            # Do not overwrite a newer browser/QQ command save that changed
            # the same reply while this download was underway.
            if _normalize_portal_reply_assets(entry.get("image_portal_assets")) != job["assets"]:
                continue
            refs = list(job["refs"])
            status = str(job["status"])
            entry_changed = False
            if entry.get("has_images") is not True:
                entry["has_images"] = True
                entry_changed = True
            if entry.get("image_cache_refs") != refs:
                entry["image_cache_refs"] = refs
                entry_changed = True
            if entry.get("image_cache_status") != status:
                entry["image_cache_status"] = status
                entry_changed = True
            # Do not refresh this timestamp merely because a periodic portal
            # heartbeat verified an already-complete local cache.  The caller
            # uses it to keep retry work bounded, and needless writes here can
            # otherwise trigger a fresh configuration sync every interval.
            # Pure portal-asset replies have no original URL.  Keeping the
            # cache refs in the legacy field lets existing ``resolve_reply_images``
            # deliver them without affecting old URL/base64 reply records.
            if not isinstance(entry.get("image_urls"), list) or not entry.get("image_urls"):
                if entry.get("image_urls") != refs:
                    entry["image_urls"] = list(refs)
                    entry_changed = True
            if entry_changed:
                entry["image_cached_at"] = now
                changed = True
        update_state["changed"] = changed
        return current if changed else current

    updated_replies = await asyncio.to_thread(
        update_group_data, group_id, "replies", update_replies
    )
    if update_state["changed"]:
        await asyncio.to_thread(cleanup_unused_reply_images, group_id, updated_replies)
    return {"rules": len(jobs), "cached": cached_total, "failed": failed_total}


def _iter_image_reply_entries(replies: dict) -> list[dict]:
    entries: list[dict] = []
    if not isinstance(replies, dict):
        return entries
    entries.extend(
        value for value in replies.get("exact", {}).values()
        if isinstance(value, dict) and value.get("has_images")
    )
    entries.extend(
        value for value in replies.get("fuzzy_keywords", {}).values()
        if isinstance(value, dict) and value.get("has_images")
    )
    for item in replies.get("regex", []):
        if not isinstance(item, dict):
            continue
        value = item.get("reply")
        if isinstance(value, dict) and value.get("has_images"):
            entries.append(value)
    return entries


def cleanup_unused_reply_images(group_id: str, replies: dict | None = None) -> int:
    """删除已经没有任何回复引用的本地图片缓存。"""
    from .group_config import get_plugin_data_path

    replies = replies if isinstance(replies, dict) else get_group_replies(group_id)
    images_dir = Path(get_plugin_data_path()) / "images" / str(group_id)
    if not images_dir.is_dir():
        return 0
    try:
        safe_root = images_dir.resolve()
    except OSError:
        return 0

    referenced: set[Path] = set()
    for entry in _iter_image_reply_entries(replies):
        for field_name in ("image_cache_refs", "image_urls"):
            values = entry.get(field_name, [])
            if not isinstance(values, list):
                continue
            for value in values:
                local_path = _resolve_cache_reference(value)
                if not local_path or local_path.startswith(("http://", "https://", "base64://")):
                    continue
                try:
                    path = Path(local_path).resolve()
                    path.relative_to(safe_root)
                except (OSError, ValueError):
                    continue
                referenced.add(path)

    deleted = 0
    for path in images_dir.iterdir():
        if not path.is_file() or path.name.startswith(".") or path.suffix == ".tmp":
            continue
        try:
            resolved_path = path.resolve()
            if resolved_path not in referenced:
                path.unlink()
                deleted += 1
        except OSError:
            continue
    return deleted


def add_reply_with_images(group_id: str, keyword: str, image_urls: list, exact: bool = True,
                          operator_id: str = "", action: str = "添加问答",
                          reply_text: str = "", cache_enabled: bool = True) -> bool:
    """添加带图片的回复（包含启用功能和添加日志，一次写入完成）

    image_urls 可以是本地路径（持久化存储）或网络 URL
    """
    from .group_config import batch_update_group_data

    sources = [
        source
        for source in (_normalize_image_source(value) for value in image_urls)
        if source
    ]
    if not sources:
        return False
    now = int(time.time())

    def _update_replies(replies):
        if not isinstance(replies, dict):
            replies = {}
        if not isinstance(replies.get("exact"), dict):
            replies["exact"] = {}
        if not isinstance(replies.get("fuzzy"), list):
            replies["fuzzy"] = []
        if not isinstance(replies.get("fuzzy_keywords"), dict):
            replies["fuzzy_keywords"] = {}
        if not isinstance(replies.get("regex"), list):
            replies["regex"] = []

        reply_data = {
            "reply": str(reply_text or ""),
            "type": "exact"if exact else "fuzzy",
            "has_images": True,
            # ``image_urls`` remains for compatibility.  The source/cache
            # split makes a deleted cache file fall back to the original URL.
            "image_urls": list(sources),
            "image_source_urls": list(sources),
            "image_cache_refs": [""] * len(sources),
            "image_cache_status": "pending" if cache_enabled else "disabled",
            "created_at": now
        }

        if exact:
            replies["exact"][keyword] = reply_data
        else:
            if keyword not in replies["fuzzy"]:
                replies["fuzzy"].append(keyword)
            replies["fuzzy_keywords"][keyword] = reply_data

        return replies

    def _update_features(features):
        if not isinstance(features, dict):
            features = {}
        if not isinstance(features.get("reply"), dict):
            features["reply"] = {}
        features["reply"]["enabled"] = True
        return features

    def _update_logs(logs):
        if not isinstance(logs, list):
            logs = []
        logs.append({
            "operator_id": operator_id,
            "operator_name": "",
            "action": action,
            "target_id": keyword,
            "target_name": "",
            "reason": "图片回复",
            "timestamp": now
        })
        if len(logs) > 500:
            logs = logs[-500:]
        return logs

    # 批量更新：一次文件读写完成所有操作
    batch_update_group_data(group_id, [
        ("replies", _update_replies),
        ("features", _update_features),
        ("operation_logs", _update_logs)
    ])
    return True


def _image_extension(source: str, content_type: str, data: bytes) -> str:
    content_type = str(content_type or "").split(";", 1)[0].strip().lower()
    by_type = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }
    if content_type in by_type:
        return by_type[content_type]
    signatures = (
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"GIF87a", ".gif"),
        (b"GIF89a", ".gif"),
        (b"\xff\xd8\xff", ".jpg"),
        (b"BM", ".bmp"),
    )
    for signature, extension in signatures:
        if data.startswith(signature):
            return extension
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    suffix = Path(source.split("?", 1)[0]).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"} else ".img"


def _looks_like_image(content_type: str, data: bytes) -> bool:
    normalized_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if normalized_type.startswith("image/"):
        return True
    return (
        data.startswith((b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"\xff\xd8\xff", b"BM"))
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )


def _store_image_bytes(
    images_dir: Path,
    source: str,
    content_type: str,
    data: bytes,
    max_bytes: int,
) -> str:
    if (
        not data
        or len(data) > max_bytes
        or not _looks_like_image(content_type, data)
    ):
        return ""
    digest = hashlib.sha256(data).hexdigest()
    extension = _image_extension(source, content_type, data)
    destination = images_dir / f"{digest}{extension}"
    if not destination.is_file():
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                delete=False,
                dir=images_dir,
                prefix=f".{digest}.",
                suffix=".tmp",
            ) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(data)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, destination)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
    return f"{_REPLY_IMAGE_CACHE_PREFIX}images/{images_dir.name}/{destination.name}"


def _read_local_image(source: str, max_bytes: int) -> tuple[bytes, str]:
    path = Path(_resolve_cache_reference(source))
    if not path.is_file() or path.stat().st_size > max_bytes:
        return b"", ""
    with path.open("rb") as file:
        data = file.read(max_bytes + 1)
    return (data, "") if len(data) <= max_bytes else (b"", "")


async def _cache_one_reply_image(
    source: str,
    images_dir: Path,
    session,
    semaphore,
    max_bytes: int,
) -> str:
    source = _normalize_image_source(source)
    if not source:
        return ""
    try:
        if source.startswith(_REPLY_IMAGE_CACHE_PREFIX):
            return source if os.path.isfile(_resolve_cache_reference(source)) else ""
        if source.startswith("base64://"):
            payload = source[len("base64://"):]
            data = await asyncio.to_thread(base64.b64decode, payload, validate=True)
            return await asyncio.to_thread(
                _store_image_bytes, images_dir, source, "", data, max_bytes
            )
        if source.startswith(("http://", "https://")):
            async with semaphore:
                async with session.get(source, allow_redirects=True) as response:
                    if response.status != 200:
                        return ""
                    try:
                        content_length = int(response.headers.get("Content-Length", 0) or 0)
                    except (TypeError, ValueError):
                        content_length = 0
                    if content_length > max_bytes:
                        return ""
                    data = await response.content.read(max_bytes + 1)
                    if len(data) > max_bytes:
                        return ""
                    content_type = response.headers.get("Content-Type", "")
            return await asyncio.to_thread(
                _store_image_bytes, images_dir, source, content_type, data, max_bytes
            )
        data, content_type = await asyncio.to_thread(_read_local_image, source, max_bytes)
        return await asyncio.to_thread(
            _store_image_bytes, images_dir, source, content_type, data, max_bytes
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("自动回复图片缓存失败: %s", type(exc).__name__)
        return ""


async def download_and_persist_images(
    group_id: str,
    keyword: str,
    image_urls: list,
    exact: bool = True,
    *,
    source_identity: list | None = None,
    max_image_bytes: int = _DEFAULT_REPLY_IMAGE_BYTES,
    download_concurrency: int = _DEFAULT_PARALLEL_IMAGE_DOWNLOADS,
) -> dict:
    """后台下载图片 URL 到本地持久化存储，并更新回复数据中的路径。

    在 /添加回复 命令响应后异步执行，不阻塞用户响应。
    下载完成后将临时 URL 替换为本地路径，避免 QQ 临时 URL 过期失效。
    """
    from .group_config import get_plugin_data_path, update_group_data

    sources = [
        source
        for source in (_normalize_image_source(value) for value in image_urls)
        if source
    ]
    if not sources:
        return {"cached": 0, "failed": 0, "total": 0}
    identity_sources = [
        source
        for source in (
            _normalize_image_source(value)
            for value in (source_identity if isinstance(source_identity, list) else sources)
        )
        if source
    ]
    # A source identity is used when OneBot first resolves an image ID to its
    # local cache file.  It keeps the command's original source list as the
    # race-protection key while the actual download reads the local file.
    if len(identity_sources) != len(sources):
        identity_sources = list(sources)

    try:
        max_image_bytes = max(
            1024 * 1024,
            min(int(max_image_bytes), 100 * 1024 * 1024),
        )
    except (TypeError, ValueError):
        max_image_bytes = _DEFAULT_REPLY_IMAGE_BYTES
    try:
        download_concurrency = max(1, min(int(download_concurrency), 8))
    except (TypeError, ValueError):
        download_concurrency = _DEFAULT_PARALLEL_IMAGE_DOWNLOADS

    images_dir = Path(get_plugin_data_path()) / "images" / str(group_id)
    images_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(download_concurrency)
    if any(source.startswith(("http://", "https://")) for source in sources):
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=20, connect=5, sock_read=15)
        connector = aiohttp.TCPConnector(limit=download_concurrency)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            cache_refs = await asyncio.gather(*(
                _cache_one_reply_image(
                    source, images_dir, session, semaphore, max_image_bytes
                )
                for source in sources
            ))
    else:
        cache_refs = await asyncio.gather(*(
            _cache_one_reply_image(
                source, images_dir, None, semaphore, max_image_bytes
            )
            for source in sources
        ))

    cached = sum(bool(reference) for reference in cache_refs)
    status = "ready" if cached == len(sources) else "partial" if cached else "failed"

    def _update(replies):
        if not isinstance(replies, dict):
            return replies
        if exact:
            entry = replies.get("exact", {}).get(keyword)
        else:
            entry = replies.get("fuzzy_keywords", {}).get(keyword)
        if not isinstance(entry, dict):
            return replies
        current_sources = entry.get("image_source_urls", entry.get("image_urls", []))
        current_sources = [
            _normalize_image_source(value)
            for value in current_sources
            if _normalize_image_source(value)
        ] if isinstance(current_sources, list) else []
        # A newer command may have replaced the same keyword while downloads
        # were running.  Never overwrite that newer reply.
        if current_sources != identity_sources:
            return replies
        entry["image_source_urls"] = list(identity_sources)
        entry["image_cache_refs"] = list(cache_refs)
        entry["image_urls"] = [
            reference or source
            for source, reference in zip(identity_sources, cache_refs)
        ]
        entry["image_cache_status"] = status
        entry["image_cached_at"] = int(time.time())
        return replies

    updated_replies = await asyncio.to_thread(
        update_group_data, group_id, "replies", _update
    )
    await asyncio.to_thread(cleanup_unused_reply_images, group_id, updated_replies)
    logger.debug(
        "自动回复图片缓存完成: 群=%s 数量=%s 成功=%s",
        group_id,
        len(sources),
        cached,
    )
    return {"cached": cached, "failed": len(sources) - cached, "total": len(sources)}

def add_reply_optimized(group_id: str, keyword: str, reply: str, exact: bool = True,
                        operator_id: str = "", action: str = "添加问答") -> bool:
    """添加文本回复（优化版，一次写入完成）"""
    from .group_config import batch_update_group_data
    import time as _time
    now = int(_time.time())
    
    def _update_replies(replies):
        if replies is None:
            replies = {"exact": {}, "fuzzy": [], "regex": []}
        
        reply_data = {
            "reply": reply,
            "type": "exact"if exact else "fuzzy",
            "created_at": now
        }
        
        if exact:
            replies["exact"][keyword] = reply_data
        else:
            if keyword not in replies["fuzzy"]:
                replies["fuzzy"].append(keyword)
            if "fuzzy_keywords"not in replies:
                replies["fuzzy_keywords"] = {}
            replies["fuzzy_keywords"][keyword] = reply_data
        
        return replies
    
    def _update_features(features):
        if features is None:
            features = {}
        if "reply"not in features:
            features["reply"] = {}
        features["reply"]["enabled"] = True
        return features
    
    def _update_logs(logs):
        if logs is None:
            logs = []
        logs.append({
            "operator_id": operator_id,
            "operator_name": "",
            "action": action,
            "target_id": keyword,
            "target_name": "",
            "reason": reply,
            "timestamp": now
        })
        if len(logs) > 500:
            logs = logs[-500:]
        return logs
    
    # 批量更新：一次文件读写完成所有操作
    batch_update_group_data(group_id, [
        ("replies", _update_replies),
        ("features", _update_features),
        ("operation_logs", _update_logs)
    ])
    return True
