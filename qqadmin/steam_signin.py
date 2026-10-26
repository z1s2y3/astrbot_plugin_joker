"""Local SteamID bindings and sign-in policy helpers.

Windows is the authority for these records.  The portal may display a bounded
read-only projection, but it never writes this data.  Records live under a
dedicated group root key so normal web configuration projection cannot replace
sign-in history or bindings.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from collections.abc import Callable, Mapping
from typing import Any

from .group_config import get_group_data, update_group_data
from ..feature_catalog import split_command_aliases


# Steam's numeric identifier is an ASCII decimal string.  Do not use
# ``\d`` here: Python treats it as Unicode-aware and would accept full-width
# or other non-ASCII numerals that the game database cannot reliably match.
STEAM_ID_PATTERN = re.compile(r"^[0-9]{17}$")
STEAM_SIGNIN_DATA_KEY = "steam_signin"
MAX_BINDINGS = 5000
STEAM_REWARD_MODES = frozenset({"database", "websocket"})
DEFAULT_STEAM_REWARD_COMMAND = "#ChangeCurrencyBalance Normal {reward} {steam_id}"
DEFAULT_SIGNIN_MESSAGES = {
    "signin_message": "{name} 的签到成功，第 {count} 次，奖励 {reward}。",
    "bind_success_message": "绑定成功：{name} -> {steam_id}",
    "bind_failure_message": "SteamID 绑定失败：{message}",
    "duplicate_steam_message": "该 SteamID 已被其他 QQ 绑定，请联系管理员解绑。",
    "steam_not_found_message": "未找到该 SteamID，请确认已加入当前服务器。",
    "steam_database_error_message": "SteamID 数据库暂时不可用，请联系管理员。",
    "database_reward_error_message": "奖励发放失败，本次签到未完成，请稍后重试。",
    "database_already_signed_message": "你在当前周期已经签到过了。",
    "steam_reward_failure_message": "签到奖励执行失败，未完成本次签到，请稍后重试。",
}


def normalize_command_name(value: object, default: str) -> str:
    """Return the primary token for a single or separator-delimited setting."""
    return split_command_aliases(value, default)[0]


def normalize_command_alias_text(value: object, default: str) -> str:
    """Return the canonical separator form while retaining all aliases."""
    return "|".join(split_command_aliases(value, default))


def normalize_steam_id(value: object) -> str:
    """Return a canonical 17-digit SteamID, or an empty string."""
    candidate = str(value or "").strip()
    return candidate if STEAM_ID_PATTERN.fullmatch(candidate) else ""


def _empty_store() -> dict[str, Any]:
    return {"bindings": {}, "updated_at": 0}


def _normalize_store(value: object) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    bindings = raw.get("bindings")
    if not isinstance(bindings, dict):
        bindings = {}
    normalized: dict[str, Any] = {"bindings": {}}
    for raw_steam_id, raw_record in list(bindings.items())[:MAX_BINDINGS]:
        steam_id = normalize_steam_id(raw_steam_id)
        if not steam_id or not isinstance(raw_record, dict):
            continue
        user_id = str(raw_record.get("user_id", "")).strip()[:64]
        if not user_id:
            continue
        normalized["bindings"][steam_id] = {
            "user_id": user_id,
            "user_name": str(raw_record.get("user_name", "")).strip()[:128],
            "source": (
                "admission"
                if str(raw_record.get("source", "command") or "command").strip().lower()
                in {"admission", "steamid", "audit"}
                else "command"
            ),
            "bound_at": max(0, _as_int(raw_record.get("bound_at", 0))),
            "last_signin": str(raw_record.get("last_signin", ""))[:32],
            "signin_count": max(0, _as_int(raw_record.get("signin_count", 0))),
            "monthly_count": max(0, _as_int(raw_record.get("monthly_count", 0))),
            "weekly_count": max(0, _as_int(raw_record.get("weekly_count", 0))),
        }
    normalized["updated_at"] = max(0, _as_int(raw.get("updated_at", 0)))
    return normalized


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def load_steam_signin_data(group_id: str) -> dict[str, Any]:
    return _normalize_store(get_group_data(group_id, STEAM_SIGNIN_DATA_KEY, {}))


def save_steam_signin_data(group_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Persist normalized data through the existing atomic group-data writer."""
    normalized = _normalize_store(data)
    normalized["updated_at"] = int(time.time())
    update_group_data(group_id, STEAM_SIGNIN_DATA_KEY, lambda _old: normalized)
    return normalized


def get_steam_binding(group_id: str, user_id: object = "", steam_id: object = "") -> dict[str, Any] | None:
    store = load_steam_signin_data(group_id)
    wanted_user = str(user_id or "").strip()
    raw_steam = str(steam_id or "").strip()
    wanted_steam = normalize_steam_id(raw_steam)
    # A malformed explicit SteamID must not degrade into an unfiltered lookup
    # (which would otherwise return the first member's binding).
    if raw_steam and not wanted_steam:
        return None
    for current_steam, record in store["bindings"].items():
        if wanted_steam and current_steam != wanted_steam:
            continue
        if wanted_user and str(record.get("user_id", "")) != wanted_user:
            continue
        return {"steam_id": current_steam, **record}
    return None


def bind_steam_id(
    group_id: str,
    user_id: object,
    steam_id: object,
    user_name: object = "",
    database_validator: Callable[[str], Mapping[str, Any]] | None = None,
    source: str = "command",
) -> dict[str, Any]:
    """Bind one SteamID to one QQ account without allowing overwrite.

    ``database_validator`` is optional for backwards compatibility.  When
    provided, it must return the result contract used by the SCUM database
    helper; a failed validation is returned before the local JSON store is
    touched.  The callback is intentionally injected so this module remains
    independent of FTP credentials and portal runtime objects.
    """
    normalized_user = str(user_id or "").strip()[:64]
    normalized_steam = normalize_steam_id(steam_id)
    if not normalized_user:
        return {"success": False, "code": "invalid_user", "message": "无法识别 QQ 账号。"}
    if not normalized_steam:
        return {"success": False, "code": "invalid_steam_id", "message": "SteamID 必须是 17 位数字。"}

    # Resolve local ownership before consulting the game database.  A
    # duplicate binding is a deterministic local conflict and should retain
    # its custom reply even when FTP/SQLite is temporarily unavailable.  The
    # atomic update below repeats this check to close the race between this
    # read and the eventual write.
    existing_for_steam = get_steam_binding(group_id, steam_id=normalized_steam)
    if existing_for_steam:
        owner = str(existing_for_steam.get("user_id", ""))
        if owner == normalized_user:
            return {
                "success": False,
                "code": "already_bound",
                "binding": existing_for_steam,
            }
        return {
            "success": False,
            "code": "steam_in_use",
            "message": "该 SteamID 已被其他 QQ 绑定。",
        }
    existing_for_user = get_steam_binding(group_id, user_id=normalized_user)
    if existing_for_user:
        return {
            "success": False,
            "code": "user_already_bound",
            "old_steam_id": existing_for_user.get("steam_id", ""),
            "message": "该 QQ 已绑定其他 SteamID，请联系群主处理。",
        }

    if database_validator is not None:
        try:
            validation = database_validator(normalized_steam)
        except Exception:
            validation = {
                "success": False,
                "code": "db_unavailable",
                "message": "服务器数据库暂不可用，暂时无法验证 SteamID，请稍后重试。",
            }
        if not isinstance(validation, Mapping):
            return {
                "success": False,
                "code": "db_unavailable",
                "message": "服务器数据库暂不可用，暂时无法验证 SteamID，请稍后重试。",
            }
        if not bool(validation.get("success")):
            result = dict(validation)
            result.setdefault("success", False)
            result.setdefault("code", "db_unavailable")
            result.setdefault("message", "SteamID 数据库验证失败，请稍后重试。")
            return result

    result: dict[str, Any] = {}

    def _update(old: object) -> dict[str, Any]:
        store = _normalize_store(old)
        bindings = store["bindings"]
        existing_for_steam = bindings.get(normalized_steam)
        existing_for_user = next(
            (item for item in bindings.items() if str(item[1].get("user_id", "")) == normalized_user),
            None,
        )
        if existing_for_steam:
            owner = str(existing_for_steam.get("user_id", ""))
            if owner == normalized_user:
                result.update({"success": False, "code": "already_bound", "binding": {"steam_id": normalized_steam, **existing_for_steam}})
            else:
                result.update({"success": False, "code": "steam_in_use", "message": "该 SteamID 已被其他 QQ 绑定。"})
            return store
        if existing_for_user:
            old_steam, old_record = existing_for_user
            result.update({"success": False, "code": "user_already_bound", "old_steam_id": old_steam, "message": "该 QQ 已绑定其他 SteamID，请联系群主处理。"})
            return store
        binding_source = (
            "admission"
            if str(source or "command").strip().lower() in {"admission", "steamid", "audit"}
            else "command"
        )
        bindings[normalized_steam] = {
            "user_id": normalized_user,
            "user_name": str(user_name or "").strip()[:128],
            "source": binding_source,
            "bound_at": int(time.time()),
            "last_signin": "",
            "signin_count": 0,
            "monthly_count": 0,
            "weekly_count": 0,
        }
        store["updated_at"] = int(time.time())
        result.update({"success": True, "code": "bound", "binding": {"steam_id": normalized_steam, **bindings[normalized_steam]}})
        return store

    try:
        update_group_data(group_id, STEAM_SIGNIN_DATA_KEY, _update)
    except Exception:
        return {
            "success": False,
            "code": "write_failed",
            "message": "绑定记录保存失败，请稍后重试。",
        }
    return result or {"success": False, "code": "write_failed", "message": "绑定记录保存失败。"}


def unbind_steam_id(group_id: str, target: object) -> dict[str, Any]:
    """Remove a binding by QQ or SteamID.  Caller must enforce owner rights."""
    wanted = str(target or "").strip()
    if not wanted:
        return {"success": False, "code": "missing_target", "message": "请提供 QQ 号或 17 位 SteamID。"}
    result: dict[str, Any] = {}

    def _update(old: object) -> dict[str, Any]:
        store = _normalize_store(old)
        bindings = store["bindings"]
        found = None
        for steam_id, record in bindings.items():
            if steam_id == wanted or str(record.get("user_id", "")) == wanted:
                found = (steam_id, record)
                break
        if not found:
            result.update({"success": False, "code": "not_found", "message": "未找到对应 SteamID 绑定。"})
            return store
        steam_id, record = found
        bindings.pop(steam_id, None)
        store["updated_at"] = int(time.time())
        result.update({"success": True, "code": "unbound", "binding": {"steam_id": steam_id, **record}})
        return store

    try:
        update_group_data(group_id, STEAM_SIGNIN_DATA_KEY, _update)
    except Exception:
        return {
            "success": False,
            "code": "write_failed",
            "message": "解绑记录保存失败，请稍后重试。",
        }
    return result or {"success": False, "code": "write_failed", "message": "解绑记录保存失败。"}


def record_steam_signin(group_id: str, user_id: object, user_name: object = "") -> dict[str, Any]:
    """Update the bounded runtime counters after a successful local sign-in."""
    normalized_user = str(user_id or "").strip()
    if not normalized_user:
        return {
            "success": False,
            "code": "invalid_user",
            "message": "Steam 签到记录缺少 QQ 号，未写入关联记录。",
        }
    result: dict[str, Any] = {}

    def _update(old: object) -> dict[str, Any]:
        store = _normalize_store(old)
        for steam_id, record in store["bindings"].items():
            if str(record.get("user_id", "")) != normalized_user:
                continue
            today = datetime.now().date()
            last = str(record.get("last_signin", ""))
            record["last_signin"] = today.isoformat()
            record["signin_count"] = max(0, _as_int(record.get("signin_count", 0))) + 1
            # Counts are informational; reset boundaries are calculated by the
            # web/command layer from the stored date rather than trusting them.
            record["user_name"] = str(user_name or record.get("user_name", "")).strip()[:128]
            if last[:7] == today.strftime("%Y-%m"):
                record["monthly_count"] = max(0, _as_int(record.get("monthly_count", 0))) + 1
            else:
                record["monthly_count"] = 1
            try:
                previous_week = date.fromisoformat(last).isocalendar()[:2] if last else None
            except ValueError:
                previous_week = None
            current_week = today.isocalendar()[:2]
            record["weekly_count"] = max(0, _as_int(record.get("weekly_count", 0))) + 1 if previous_week == current_week else 1
            store["updated_at"] = int(time.time())
            result.update(
                {
                    "success": True,
                    "code": "recorded",
                    "steam_id": steam_id,
                    **record,
                }
            )
            break
        return store

    try:
        update_group_data(group_id, STEAM_SIGNIN_DATA_KEY, _update)
    except Exception:
        return {
            "success": False,
            "code": "write_failed",
            "message": "Steam 签到关联记录保存失败，未确认本地记录。",
        }
    return result or {
        "success": False,
        "code": "not_bound",
        "message": "未找到对应 SteamID 绑定，未写入关联记录。",
    }


def list_steam_bindings(group_id: str, limit: int = 200) -> list[dict[str, Any]]:
    store = load_steam_signin_data(group_id)
    try:
        size = max(0, min(int(limit), 500))
    except (TypeError, ValueError, OverflowError):
        size = 200
    rows = [{"steam_id": steam_id, **record} for steam_id, record in store["bindings"].items()]
    rows.sort(key=lambda row: (str(row.get("user_name", "")), str(row.get("user_id", ""))))
    return rows[:size]


def signin_policy(detail: object) -> dict[str, Any]:
    """Normalize web-editable policy fields without trusting arbitrary input."""
    raw = detail if isinstance(detail, dict) else {}
    mode = str(raw.get("signin_mode", "basic") or "basic").strip().lower()
    if mode not in {"basic", "steam"}:
        mode = "basic"
    schedule = str(raw.get("schedule_mode", "daily") or "daily").strip().lower()
    if schedule not in {"daily", "weekly", "monthly", "streak", "custom"}:
        schedule = "daily"
    profiles = normalize_signin_profiles(raw.get("signin_profiles"))
    database_profile = str(
        raw.get("database_profile", raw.get("active_database_profile", "")) or ""
    ).strip()[:64]
    raw_database_profiles = raw.get("database_profiles", raw.get("databases", []))
    database_profiles: list[dict[str, Any]] = []
    if isinstance(raw_database_profiles, str):
        rows: list[Any] = []
        for line in raw_database_profiles.splitlines():
            try:
                rows.append(json.loads(line))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    elif isinstance(raw_database_profiles, (list, tuple)):
        rows = list(raw_database_profiles)
    else:
        rows = []
    for row in rows[:50]:
        if isinstance(row, str):
            try:
                row = json.loads(row)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        if not isinstance(row, dict):
            continue
        profile_id = str(row.get("id", row.get("profile_id", "")) or "").strip()[:64]
        path = str(row.get("path", row.get("database_path", row.get("sqlite_path", ""))) or "").strip()[:1024]
        if not profile_id or not path or "\x00" in path:
            continue
        enabled = row.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"", "0", "false", "off", "no"}
        normalized_profile = {
            "id": profile_id,
            "name": str(row.get("name", profile_id) or profile_id).strip()[:100],
            "path": path,
            "enabled": bool(enabled),
        }
        # The portal projection carries the immutable snapshot identity used
        # by Windows for FTP write-back.  Keep it through policy normalization
        # so a database reward can publish the changed SQLite file against the
        # exact revision it read, while ignoring malformed metadata.
        try:
            snapshot_revision = int(row.get("snapshot_revision", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            snapshot_revision = 0
        if 0 < snapshot_revision <= 2**31 - 1:
            normalized_profile["snapshot_revision"] = snapshot_revision
        snapshot_sha256 = str(row.get("snapshot_sha256", "") or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256):
            normalized_profile["snapshot_sha256"] = snapshot_sha256
        database_profiles.append(normalized_profile)
    database_path = str(raw.get("database_path", raw.get("sqlite_path", "")) or "").strip()[:1024]
    if "\x00" in database_path:
        database_path = ""
    currency_column = str(
        raw.get("currency_column", raw.get("database_currency", "amount"))
        or "amount"
    ).strip().casefold()
    if currency_column not in {
        "amount",
        "integral",
        "normal_integral",
        "coins",
        "fragments",
        "dlb_num",
    }:
        currency_column = "amount"
    database_sync = raw.get("database_sync", False)
    if isinstance(database_sync, str):
        database_sync = database_sync.strip().lower() not in {
            "",
            "0",
            "false",
            "off",
            "no",
        }
    else:
        database_sync = bool(database_sync)
    reward_mode = str(
        raw.get("steam_reward_mode", raw.get("reward_mode", "database"))
        or "database"
    ).strip().lower()
    if reward_mode not in STEAM_REWARD_MODES:
        reward_mode = "database"
    reward_operation = str(
        raw.get("steam_reward_operation", "execute_game_command")
        or "execute_game_command"
    ).strip().lower()[:64]
    if not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", reward_operation):
        reward_operation = "execute_game_command"
    reward_command = str(
        raw.get("steam_reward_command", DEFAULT_STEAM_REWARD_COMMAND)
        or DEFAULT_STEAM_REWARD_COMMAND
    ).strip()[:512]
    reward_failure_message = str(
        raw.get(
            "steam_reward_failure_message",
            "签到奖励执行失败，未完成本次签到，请稍后重试。",
        )
        or "签到奖励执行失败，未完成本次签到，请稍后重试。"
    )[:500]
    allow_group_binding = raw.get("allow_group_steam_binding", False)
    if isinstance(allow_group_binding, str):
        allow_group_binding = allow_group_binding.strip().lower() not in {
            "", "0", "false", "off", "no"
        }
    recall_group_binding = raw.get("recall_group_steam_binding_message", True)
    if isinstance(recall_group_binding, str):
        recall_group_binding = recall_group_binding.strip().lower() not in {
            "", "0", "false", "off", "no"
        }
    try:
        group_binding_recall_seconds = int(
            raw.get("group_steam_binding_recall_seconds", 5) or 0
        )
    except (TypeError, ValueError, OverflowError):
        group_binding_recall_seconds = 5
    group_binding_recall_seconds = max(0, min(group_binding_recall_seconds, 600))
    unbind_on_leave = raw.get("unbind_steam_on_leave", False)
    if isinstance(unbind_on_leave, str):
        unbind_on_leave = unbind_on_leave.strip().lower() not in {
            "", "0", "false", "off", "no"
        }
    verification_required = raw.get("steamid_verification_required", False)
    if isinstance(verification_required, str):
        verification_required = verification_required.strip().lower() not in {
            "", "0", "false", "off", "no"
        }
    try:
        unverified_mute_duration = int(
            raw.get("unverified_mute_duration", 300) or 300
        )
    except (TypeError, ValueError, OverflowError):
        unverified_mute_duration = 300
    unverified_mute_duration = max(1, min(unverified_mute_duration, 2_592_000))
    unverified_mute_message = str(
        raw.get("unverified_mute_message", "请先完成 SteamID 验证后再发言。")
        or "请先完成 SteamID 验证后再发言。"
    ).strip()[:500]
    return {
        "signin_mode": mode,
        "schedule_mode": schedule,
        "signin_command": normalize_command_alias_text(raw.get("signin_command"), "签到"),
        "bind_command": normalize_command_alias_text(raw.get("bind_command"), "绑定SteamID"),
        "allow_group_steam_binding": bool(allow_group_binding),
        "recall_group_steam_binding_message": bool(recall_group_binding),
        "group_steam_binding_recall_seconds": group_binding_recall_seconds,
        "unbind_steam_on_leave": bool(unbind_on_leave),
        "steamid_verification_required": bool(verification_required),
        "unverified_mute_duration": unverified_mute_duration,
        "unverified_mute_message": unverified_mute_message,
        "custom_rewards": raw.get("custom_rewards", []) if isinstance(raw.get("custom_rewards"), list) else [],
        "signin_message": _profile_text(raw.get("signin_message"), DEFAULT_SIGNIN_MESSAGES["signin_message"], 1000),
        "bind_success_message": _profile_text(raw.get("bind_success_message"), DEFAULT_SIGNIN_MESSAGES["bind_success_message"], 500),
        "bind_failure_message": _profile_text(raw.get("bind_failure_message"), DEFAULT_SIGNIN_MESSAGES["bind_failure_message"], 500),
        "duplicate_steam_message": _profile_text(raw.get("duplicate_steam_message"), DEFAULT_SIGNIN_MESSAGES["duplicate_steam_message"], 500),
        "steam_not_found_message": _profile_text(raw.get("steam_not_found_message"), DEFAULT_SIGNIN_MESSAGES["steam_not_found_message"], 500),
        "steam_database_error_message": _profile_text(raw.get("steam_database_error_message"), DEFAULT_SIGNIN_MESSAGES["steam_database_error_message"], 500),
        "database_reward_error_message": _profile_text(raw.get("database_reward_error_message"), DEFAULT_SIGNIN_MESSAGES["database_reward_error_message"], 500),
        "database_already_signed_message": _profile_text(raw.get("database_already_signed_message"), DEFAULT_SIGNIN_MESSAGES["database_already_signed_message"], 500),
        "database_profile": database_profile,
        "database_path": database_path,
        "database_profiles": database_profiles,
        "currency_column": currency_column,
        "database_sync": database_sync,
        "steam_reward_mode": reward_mode,
        "steam_reward_operation": reward_operation,
        "steam_reward_command": reward_command,
        "steam_reward_failure_message": reward_failure_message,
        "signin_profiles": profiles,
        "enabled": (
            raw.get("enabled", True).strip().lower()
            not in {"", "0", "false", "off", "no"}
            if isinstance(raw.get("enabled", True), str)
            else bool(raw.get("enabled", True))
        ),
        **{
            key: raw.get(key)
            for key in ("base_reward", "reward_3_days", "reward_7_days", "reward_14_days", "reward_30_days")
            if key in raw
        },
    }


_SIGNIN_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_MAX_SIGNIN_PROFILES = 50


def _profile_text(value: object, default: str, maximum: int) -> str:
    text = str(value or "").strip()
    return (text or default)[:maximum]


def normalize_signin_profiles(value: object) -> list[dict[str, Any]]:
    """Normalize the optional multi-sign-in definitions from the portal.

    The web form accepts one JSON object per line for backwards-compatible
    HTML forms, while the Windows runtime also accepts a native list of
    dictionaries. Invalid rows are ignored rather than disabling legacy
    ``/签到``. Each profile is independent and can select ordinary or SteamID
    sign-in, its own command and reward policy.
    """
    raw_items: list[Any]
    if isinstance(value, str):
        raw_items = []
        for line in value.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw_items.append(json.loads(line))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raw_items = []

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_commands: set[str] = set()
    for index, item in enumerate(raw_items[:_MAX_SIGNIN_PROFILES]):
        if not isinstance(item, dict):
            continue
        profile_id = _profile_text(item.get("id"), f"profile-{index + 1}", 48)
        if not _SIGNIN_PROFILE_ID_RE.fullmatch(profile_id) or profile_id in seen:
            continue
        command_aliases = split_command_aliases(
            item.get("command", item.get("signin_command")), "签到"
        )
        command = "|".join(command_aliases)
        command_key = {alias.casefold() for alias in command_aliases}
        if (
            any(ch.isspace() for ch in command)
            or command_key.intersection(seen_commands)
        ):
            continue
        mode = str(item.get("mode", item.get("signin_mode", "basic")) or "basic").strip().lower()
        if mode not in {"basic", "steam"}:
            mode = "basic"
        schedule = str(item.get("schedule", item.get("schedule_mode", "daily")) or "daily").strip().lower()
        if schedule not in {"daily", "weekly", "monthly", "streak", "custom"}:
            schedule = "daily"
        enabled = item.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"", "0", "false", "off", "no"}
        profile: dict[str, Any] = {
            "id": profile_id,
            "name": _profile_text(item.get("name"), profile_id, 80),
            "command": command,
            "signin_command": command,
            "mode": mode,
            "signin_mode": mode,
            "schedule": schedule,
            "schedule_mode": schedule,
            "enabled": bool(enabled),
            "signin_message": _profile_text(item.get("signin_message"), "", 1000),
        }
        if "currency_column" in item or "database_currency" in item:
            profile_currency = str(
                item.get("currency_column", item.get("database_currency", "amount"))
                or "amount"
            ).strip().casefold()
            profile["currency_column"] = (
                profile_currency
                if profile_currency
                in {
                    "amount",
                    "integral",
                    "normal_integral",
                    "coins",
                    "fragments",
                    "dlb_num",
                }
                else "amount"
            )
        if "database_sync" in item:
            profile_sync = item.get("database_sync", False)
            if isinstance(profile_sync, str):
                profile_sync = profile_sync.strip().lower() not in {
                    "",
                    "0",
                    "false",
                    "off",
                    "no",
                }
            profile["database_sync"] = bool(profile_sync)
        if "steam_reward_mode" in item or "reward_mode" in item:
            profile_reward_mode = str(
                item.get("steam_reward_mode", item.get("reward_mode", "database"))
                or "database"
            ).strip().lower()
            if profile_reward_mode not in STEAM_REWARD_MODES:
                profile_reward_mode = "database"
            profile["steam_reward_mode"] = profile_reward_mode
        if "steam_reward_operation" in item:
            operation = str(item.get("steam_reward_operation") or "execute_game_command").strip().lower()
            profile["steam_reward_operation"] = (
                operation if re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", operation)
                else "execute_game_command"
            )
        if "steam_reward_command" in item:
            profile["steam_reward_command"] = str(
                item.get("steam_reward_command") or DEFAULT_STEAM_REWARD_COMMAND
            ).strip()[:512]
        if "steam_reward_failure_message" in item:
            profile["steam_reward_failure_message"] = str(
                item.get("steam_reward_failure_message") or "签到奖励执行失败，未完成本次签到，请稍后重试。"
            ).strip()[:500]
        # Keep only explicitly configured reward values.  A profile that
        # merely defines its own command/mode should inherit the group's
        # normal reward policy; writing an implicit zero here used to turn a
        # newly added scheme into a zero-point sign-in.  An explicit ``0`` is
        # still retained and can deliberately disable that profile's reward.
        for key in ("base_reward", "reward_3_days", "reward_7_days", "reward_14_days", "reward_30_days"):
            if key not in item:
                continue
            try:
                profile[key] = max(0, min(1_000_000, int(item.get(key))))
            except (TypeError, ValueError, OverflowError):
                profile[key] = 0
        custom_rewards = item.get("custom_rewards", [])
        profile["custom_rewards"] = [str(row)[:500] for row in custom_rewards[:50]] if isinstance(custom_rewards, list) else []
        seen.add(profile_id)
        seen_commands.update(command_key)
        result.append(profile)
    return result


def select_signin_profile(policy: object, selector: object = "") -> dict[str, Any]:
    """Return one profile merged over the legacy policy.

    ``selector`` may be a profile ID, display name, or command. Empty/unknown
    selectors intentionally fall back to the legacy policy to preserve old
    command behaviour.
    """
    base = signin_policy(policy)
    wanted = str(selector or "").strip().lstrip("/")
    if not wanted:
        return base
    for profile in base.get("signin_profiles", []):
        if wanted in {
            str(profile.get("id", "")),
            str(profile.get("name", "")),
        } or wanted in split_command_aliases(profile.get("command"), "签到"):
            merged = dict(base)
            merged.update(profile)
            merged["signin_profiles"] = base.get("signin_profiles", [])
            selected = signin_policy(merged)
            # Keep the selected identity outside the editable profile list.
            # The stats store uses this stable ID to give every configured
            # sign-in scheme its own daily/weekly/monthly claim window while
            # continuing to aggregate points and rankings for the QQ member.
            selected["profile_id"] = str(profile.get("id", ""))
            selected["profile_name"] = str(
                profile.get("name") or profile.get("id") or ""
            )[:80]
            return selected
    return base
