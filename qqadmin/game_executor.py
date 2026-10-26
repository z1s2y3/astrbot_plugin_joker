"""Execution-side adapter for Linux-managed game command settings."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from .plugin_websocket_client import (
    execute_plugin_websocket_command,
    parse_plugin_websocket_url,
    test_plugin_websocket_connection,
)


GAME_EXECUTOR_TYPES = frozenset({"plugin_websocket"})
GAME_EXECUTOR_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
MAX_GAME_EXECUTOR_PROFILES = 100
MAX_GAME_EXECUTOR_GROUPS = 256
DEFAULT_GAME_EXECUTOR_CONFIG = {
    "enabled": False,
    "type": "plugin_websocket",
    "host": "127.0.0.1",
    "port": 28016,
    "api_key": "",
    "websocket_path": "/ws",
    "websocket_scheme": "ws",
    "timeout_seconds": 10.0,
}


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "0", "false", "off", "no"}:
            return False
        if normalized in {"1", "true", "on", "yes"}:
            return True
    return default if value is None else bool(value)


def normalize_game_executor_config(value: object) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    raw_type = str(raw.get("type", "") or "").strip().lower()
    executor_type = "plugin_websocket"
    # Old non-WebSocket records must never be interpreted as a different transport.
    # They remain disabled until an administrator replaces them with the full
    # documented plugin-WebSocket URL.
    legacy_transport = bool(raw_type and raw_type != "plugin_websocket") or bool(
        raw.get("password") and not raw.get("api_key") and not raw.get("websocket_url")
    )
    try:
        port = int(raw.get("port", 28016))
    except (TypeError, ValueError, OverflowError):
        port = 28016
    try:
        timeout = float(raw.get("timeout_seconds", 10.0))
    except (TypeError, ValueError, OverflowError):
        timeout = 10.0
    result = {
        "enabled": _as_bool(raw.get("enabled", False)) and not legacy_transport,
        "type": executor_type,
        "host": str(raw.get("host", "127.0.0.1") or "127.0.0.1").strip()[:255],
        "port": max(1, min(65535, port)),
        "timeout_seconds": max(1.0, min(30.0, timeout)),
    }
    parsed_url: dict[str, object] = {}
    raw_url = str(raw.get("websocket_url", "") or "").strip()
    if raw_url:
        try:
            parsed_url = parse_plugin_websocket_url(raw_url)
        except ValueError:
            parsed_url = {}
    result.update(
        {
            "host": str(parsed_url.get("host", result["host"]) or result["host"]),
            "port": int(parsed_url.get("port", result["port"]) or result["port"]),
            "api_key": str(parsed_url.get("api_key", raw.get("api_key", "")) or "")[:512],
            "websocket_path": str(parsed_url.get("websocket_path", raw.get("websocket_path", "/ws")) or "/ws").strip()[:32],
            "websocket_scheme": str(parsed_url.get("websocket_scheme", raw.get("websocket_scheme", "ws")) or "ws").strip().lower(),
        }
    )
    return result


def _normalize_group_ids(value: object) -> list[str]:
    """Normalize one profile's QQ group mapping without accepting wildcards."""
    if isinstance(value, str):
        values = re.split(r"[\s,，]+", value.strip())
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        values = [value] if value is not None else []
    result: list[str] = []
    for item in values:
        group_id = str(item or "").strip()
        if not group_id or not group_id.isdigit() or len(group_id) > 32:
            continue
        if group_id not in result:
            result.append(group_id)
        if len(result) >= MAX_GAME_EXECUTOR_GROUPS:
            break
    return result


def _profile_candidates(value: object) -> list[Mapping[str, Any]]:
    """Accept AstrBot's JSON-lines list and direct JSON/list forms."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = [line for line in text.splitlines() if line.strip()]
    if isinstance(value, Mapping):
        if "id" in value or "profile_id" in value or "host" in value:
            return [value]
        candidates: list[Mapping[str, Any]] = []
        for profile_id, item in value.items():
            if isinstance(item, Mapping):
                candidate = dict(item)
                candidate.setdefault("id", profile_id)
                candidates.append(candidate)
        return candidates
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    candidates = []
    for item in value:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        if isinstance(item, Mapping):
            candidates.append(item)
    return candidates


def normalize_game_executor_profiles(
    value: object,
    *,
    default_group_id: object = "",
    allow_multiple_for_group: bool = False,
) -> list[dict[str, Any]]:
    """Normalize game executor profiles mapped to QQ groups.

    The first profile wins when two profiles claim the same group.  Keeping
    the rule deterministic prevents a restart or a UI reorder from silently
    sending rewards to a different server.
    """
    profiles: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    claimed_groups: set[str] = set()
    for raw in _profile_candidates(value)[:MAX_GAME_EXECUTOR_PROFILES]:
        profile_id = str(raw.get("id", raw.get("profile_id", "")) or "").strip()
        if not GAME_EXECUTOR_PROFILE_ID_RE.fullmatch(profile_id) or profile_id in seen_ids:
            continue
        groups = _normalize_group_ids(
            raw.get("groups", raw.get("group_ids", raw.get("group_id", [])))
        )
        if not groups and str(default_group_id or "").strip().isdigit():
            groups = [str(default_group_id).strip()]
        if not allow_multiple_for_group:
            groups = [group_id for group_id in groups if group_id not in claimed_groups]
        if not groups:
            continue
        config = dict(raw)
        # A profile is an explicit route, so it is enabled by default.  The
        # legacy global config keeps its old disabled-by-default behavior.
        config.setdefault("enabled", True)
        normalized = normalize_game_executor_config(config)
        profile = {
            **normalized,
            "id": profile_id,
            "name": str(raw.get("name", profile_id) or profile_id).strip()[:80],
            "groups": groups,
        }
        profiles.append(profile)
        seen_ids.add(profile_id)
        if not allow_multiple_for_group:
            claimed_groups.update(groups)
    return profiles


def select_game_executor_config(
    profiles: object,
    group_id: object,
    fallback: object = None,
    *,
    profile_id: object = "",
) -> dict[str, Any]:
    """Select one server route without allowing cross-server fallback.

    ``profile_id`` is used by diagnostics and future multi-server callers. If
    it is supplied, the requested profile must explicitly own the group;
    callers can then report a missing route instead of silently using the
    global/default server.
    """
    target = str(group_id or "").strip()
    requested = str(profile_id or "").strip()
    normalized_profiles = normalize_game_executor_profiles(
        profiles,
        allow_multiple_for_group=bool(requested),
    )
    if requested:
        for profile in normalized_profiles:
            if (
                str(profile.get("id", "") or "") == requested
                and target in profile.get("groups", [])
            ):
                selected = normalize_game_executor_config(profile)
                selected["profile_id"] = requested[:48]
                selected["profile_name"] = str(profile.get("name", "") or "")[:80]
                selected["groups"] = list(profile.get("groups", []))
                return selected
        return {
            **normalize_game_executor_config({"enabled": False}),
            "profile_id": requested[:48],
            "profile_name": "未找到服务器",
            "groups": [target] if target else [],
        }
    for profile in normalized_profiles:
        if not isinstance(profile, Mapping) or target not in profile.get("groups", []):
            continue
        selected = normalize_game_executor_config(profile)
        selected["profile_id"] = str(profile.get("id", "") or "")[:48]
        selected["profile_name"] = str(profile.get("name", "") or "")[:80]
        selected["groups"] = list(profile.get("groups", []))
        return selected
    selected = normalize_game_executor_config(fallback)
    selected["profile_id"] = "global"
    selected["profile_name"] = "全局配置"
    return selected


async def execute_configured_game_command(
    config: object,
    command: str,
    *,
    group_id: str = "",
    profile_id: str = "",
    user_id: str = "",
    steam_id: str = "",
    operation: str = "execute_game_command",
    profiles: object = None,
) -> dict[str, Any]:
    """Execute through the local adapter; credentials stay on the executor."""
    requested_profile = str(profile_id or "").strip()
    if requested_profile:
        available = normalize_game_executor_profiles(
            profiles, allow_multiple_for_group=True
        )
        if not any(
            str(profile.get("id", "") or "") == requested_profile
            and str(group_id or "") in profile.get("groups", [])
            for profile in available
        ):
            return {
                "success": False,
                "code": "game_profile_not_found",
                "message": "未找到本群指定的游戏服务器。",
                "profile_id": requested_profile[:48],
            }
    settings = select_game_executor_config(
        profiles, group_id, config, profile_id=requested_profile
    )
    if not settings["enabled"]:
        return {"success": False, "code": "executor_unavailable", "message": "游戏执行器未启用。"}
    if settings["type"] != "plugin_websocket":
        return {"success": False, "code": "executor_type_unsupported", "message": "不支持的游戏执行器类型。"}
    result = await execute_plugin_websocket_command(
        settings,
        command,
        steam_id=steam_id,
        timeout_seconds=settings["timeout_seconds"],
    )
    if isinstance(result, dict):
        result.setdefault("operation", operation)
        result.setdefault("group_id", group_id)
        result.setdefault("profile_id", settings.get("profile_id", "global"))
        result.setdefault("user_id", user_id)
        result.setdefault("steam_id", steam_id)
    return result


async def test_configured_game_connection(
    config: object,
    *,
    group_id: str = "",
    profile_id: str = "",
    profiles: object = None,
) -> dict[str, Any]:
    """Test one selected Windows-local game profile without a game command."""
    requested_profile = str(profile_id or "").strip()
    settings = select_game_executor_config(
        profiles, group_id, config, profile_id=requested_profile
    )
    if requested_profile and settings.get("profile_id") != requested_profile:
        return {
            "success": False,
            "code": "game_profile_not_found",
            "message": "未找到本群对应的游戏服务器。",
        }
    if not settings["enabled"]:
        return {"success": False, "code": "executor_unavailable", "message": "本群游戏执行器未启用。"}
    if settings["type"] != "plugin_websocket":
        return {"success": False, "code": "executor_type_unsupported", "message": "不支持的游戏执行器类型。"}
    result = await test_plugin_websocket_connection(settings)
    if isinstance(result, dict):
        result.setdefault("profile_id", settings.get("profile_id", "global"))
        result.setdefault("group_id", group_id)
    return result


__all__ = [
    "DEFAULT_GAME_EXECUTOR_CONFIG",
    "GAME_EXECUTOR_TYPES",
    "execute_configured_game_command",
    "GAME_EXECUTOR_PROFILE_ID_RE",
    "MAX_GAME_EXECUTOR_GROUPS",
    "MAX_GAME_EXECUTOR_PROFILES",
    "normalize_game_executor_config",
    "normalize_game_executor_profiles",
    "select_game_executor_config",
    "test_configured_game_connection",
]
