"""Safe projection and merge rules shared by the local bot and remote portal.

Only customer-editable configuration is synchronized. Runtime records such as
messages, sign-ins, invitation statistics, blacklist history and authorization
secrets never leave the AstrBot machine.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
import re
import time

from .feature_schema import FEATURE_BY_KEY, normalize_database_profile_descriptor
try:
    from ..qqadmin.member_identity import normalize_member_rules, rules_as_json_lines
except ImportError:  # pragma: no cover - standalone web test loader
    try:
        # ``python -m web_client`` runs this package as a top-level module.
        # Keep the canonicalizer available there too; otherwise wildcard
        # member rules are silently removed from the web -> Windows payload.
        from qqadmin.member_identity import normalize_member_rules, rules_as_json_lines
    except ImportError:
        normalize_member_rules = None
        rules_as_json_lines = None


SYNC_ROOT_KEYS = frozenset(
    {
        "audit_enabled",
        "approval_mode",
        "verification_timeout",
        "max_attempts",
        "admin_bypass",
        "blacklist_settings",
        "mute_settings",
        "kick_settings",
        "welcome_enabled",
        "welcome_delay",
        "welcome_message",
        "farewell_enabled",
        "farewell_message",
        "welcome_keywords",
        "at_new_member",
        "welcome_auto_reply",
        "welcome_auto_reply_enabled",
        "recall_enabled",
        "recall_time",
        "recall_mode",
        "recall_keywords",
        "recall_regex",
        "enable_admin_recall",
        "recall_member_scope",
        "recall_sensitive_enabled",
        "recall_sensitive_words",
        "recall_notification",
        "recall_notification_message",
        "replies",
        "timed_messages",
        "timed_message_settings",
        "sub_admins",
        "player_replies",
        "lottery",
        "nickname",
        "database_player_name",
        "tags",
        "priority",
        "bot_account_priorities",
        "group_note",
        "timezone",
        # Compatibility mirrors for older Windows builds that read these
        # values at the group-config root instead of under features.
        "auto_member_name",
        "member_name_prefix",
        "member_name_prefix_include_admins",
        "member_names",
        "auto_member_title",
        "member_titles",
        # Short-lived commands created by the Linux portal and consumed once
        # by the Windows execution client.  They are validated again locally
        # before any OneBot call and are removed after processing.
        "portal_actions",
    }
)


# These root dictionaries are compatibility mirrors consumed by Windows-side
# runtime helpers.  The portal may edit only these policy fields; any local
# operational fields in the same dictionaries must survive a round trip.
SYNC_NESTED_POLICY_FIELDS: dict[str, frozenset[str]] = {
    "blacklist_settings": frozenset(
        {
            "enabled",
            "auto_kick",
            "auto_blacklist_on_leave",
            "auto_blacklist_on_verify_fail",
            "auto_kick_on_join",
            "recall_on_kick",
            "notify_on_kick",
            "kick_reason",
            "max_auto_kick_per_day",
            "auto_kick_message",
            "leave_blacklist_message",
            "blacklist_success_message",
            "blacklist_failure_message",
            "unblacklist_success_message",
            "unblacklist_failure_message",
        }
    ),
    "mute_settings": frozenset(
        {
            "enabled",
            "level",
            "duration",
            "auto_mute_new_member",
            "auto_mute_duration",
            "auto_escalate",
            "repeat_threshold",
            "repeat_extension",
            "max_level",
            "level_durations",
            "mention_mute_rules",
            "mute_success_message",
            "unmute_success_message",
            "mute_failure_message",
        }
    ),
    "kick_settings": frozenset(
        {
            "enabled",
            "auto_blacklist",
            "limit",
            "kick_success_message",
            "kick_failure_message",
        }
    ),
    # Lottery definitions are web-editable, but the root object may also be
    # touched by older Windows builds or future runtime extensions.  Keep the
    # wire projection limited to the policy fields that the portal owns;
    # local draw counters/diagnostics must stay in ``lottery_runtime`` (or in
    # any unknown root fields) and must never be sent to Linux.
    "lottery": frozenset(
        {
            "enabled",
            "keyword",
            "max_draws_per_user",
            "cooldown",
            "success_message",
            "empty_message",
            "awards",
            "campaigns",
        }
    ),
}

# Command names are edited in the Linux portal and are intentionally
# authoritative there.  Windows still owns runtime state, but a local delta
# must never make a newly saved portal command disappear on the next poll.
# Keep this list in the shared projection module so the portal and the Windows
# sync client apply the same ownership rule.
PORTAL_COMMAND_FIELDS: dict[str, frozenset[str]] = {
    "stats": frozenset({"signin_command", "bind_command", "signin_profiles"}),
    "scum": frozenset(
        {
            "keyword_command",
            "online_command",
            "allow_server_binding",
            "allow_online_query",
        }
    ),
    "entertainment": frozenset(
        {
            "time_command",
            "dice_command",
            "rps_command",
            "random_command",
            "coin_command",
            "fortune_command",
            "luck_command",
            "answer_command",
            "like_command",
            "music_command",
            "video_command",
        }
    ),
    "lottery": frozenset({"keyword"}),
    # Game command triggers and templates are both maintained by the portal.
    "game_commands": frozenset({"commands"}),
}


def merge_portal_command_projection(
    local_projection: dict[str, Any] | None,
    portal_projection: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return ``local_projection`` with Linux-owned command fields applied.

    This helper is used only when a Windows local delta races a web revision.
    It deliberately copies no runtime fields and leaves all non-command
    settings in the Windows projection untouched.  A sparse portal payload is
    accepted for compatibility with older web clients.
    """

    result = deepcopy(local_projection) if isinstance(local_projection, dict) else {}
    incoming = portal_projection if isinstance(portal_projection, dict) else {}
    local_features = result.get("features")
    if not isinstance(local_features, dict):
        local_features = {}
        result["features"] = local_features
    incoming_features = incoming.get("features")
    if not isinstance(incoming_features, dict):
        incoming_features = {}
    for feature_key, fields in PORTAL_COMMAND_FIELDS.items():
        source = incoming_features.get(feature_key)
        if not isinstance(source, dict):
            continue
        target = local_features.get(feature_key)
        if not isinstance(target, dict):
            target = {}
            local_features[feature_key] = target
        for field in fields:
            if field in source:
                target[field] = deepcopy(source[field])
    group_id = str(incoming.get("group_id", "") or "").strip()
    if group_id:
        result["group_id"] = group_id
    return result


def _project_feature_value(feature_key: str, value: Any) -> Any:
    """Project a feature while canonicalizing member identity rules."""
    projected = deepcopy(value)
    if not isinstance(projected, dict):
        return projected
    if feature_key == "stats":
        # ``database_path`` predates the scoped integration projection and
        # may still be present in a hand-edited or older Windows snapshot.
        # Never echo an absolute path (or traversal) into the web-owned
        # projection; the Windows resolver treats this field as executable
        # local input.  Current forms already apply the same constraints, but
        # projections must defend against historical data too.
        raw_database_path = str(projected.get("database_path", "") or "").strip().replace("\\", "/")
        path_parts = [part for part in raw_database_path.split("/") if part not in {"", "."}]
        if (
            not raw_database_path
            or raw_database_path.startswith("/")
            or re.match(r"^[A-Za-z]:", raw_database_path)
            or ".." in path_parts
            or "\x00" in raw_database_path
            or len(raw_database_path) > 512
        ):
            projected["database_path"] = ""
        else:
            projected["database_path"] = "/".join(path_parts)
        raw_profiles = projected.get("database_profiles")
        if isinstance(raw_profiles, str):
            candidates = raw_profiles.splitlines()
        elif isinstance(raw_profiles, (list, tuple)):
            candidates = raw_profiles
        else:
            candidates = ()
        profiles: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in list(candidates)[:50]:
            try:
                profile = normalize_database_profile_descriptor(candidate)
            except ValueError:
                continue
            if profile["id"] in seen:
                continue
            seen.add(profile["id"])
            profiles.append(profile)
        projected["database_profiles"] = profiles
        selected = str(projected.get("database_profile", "") or "").strip()
        projected["database_profile"] = selected if selected in seen else ""
        return projected
    if feature_key == "game_servers":
        raw_profiles = projected.get("profiles")
        if isinstance(raw_profiles, str):
            candidates = raw_profiles.splitlines()
        elif isinstance(raw_profiles, (list, tuple)):
            candidates = raw_profiles
        else:
            candidates = ()
        profiles: list[dict[str, Any]] = []
        seen: set[str] = set()
        from .feature_schema import normalize_game_server_profile_descriptor
        for candidate in list(candidates)[:100]:
            try:
                profile = normalize_game_server_profile_descriptor(candidate)
            except ValueError:
                continue
            if profile["id"] in seen:
                continue
            if isinstance(candidate, dict):
                api_key_ciphertext = str(candidate.get("api_key_ciphertext", "") or "")
                if api_key_ciphertext:
                    profile["api_key_ciphertext"] = api_key_ciphertext[:2048]
                    profile["api_key_configured"] = True
            seen.add(profile["id"])
            profiles.append(profile)
        projected["profiles"] = profiles
        projected["enabled"] = bool(projected.get("enabled", False))
        return projected
    if feature_key != "group_config":
        return projected
    if normalize_member_rules is None or rules_as_json_lines is None:
        return projected
    for field, kind in (("member_names", "name"), ("member_titles", "title")):
        if field in projected:
            projected[field] = rules_as_json_lines(projected.get(field), kind)
    projected["auto_member_name"] = bool(projected.get("auto_member_name", False))
    projected["member_name_prefix"] = str(
        projected.get("member_name_prefix", "") or ""
    ).replace("\r", " ").replace("\n", " ").strip()[:20]
    raw_include_admins = projected.get("member_name_prefix_include_admins", True)
    if isinstance(raw_include_admins, str):
        projected["member_name_prefix_include_admins"] = raw_include_admins.strip().lower() not in {
            "", "0", "false", "off", "no", "否", "关闭"
        }
    else:
        projected["member_name_prefix_include_admins"] = bool(raw_include_admins)
    projected["auto_member_title"] = bool(projected.get("auto_member_title", False))
    return projected

# Reply-image bytes and source locations belong to the Windows execution side.
# They must never be copied into the Linux projection.  ``image_cache_status``
# and ``image_cached_at`` are intentionally retained because they are safe,
# useful progress indicators for the portal UI.
_REPLY_LOCAL_IMAGE_FIELDS = frozenset(
    {
        "image_cache_refs",
        "image_urls",
        "image_source_urls",
        "image_paths",
        "image_files",
        "image_base64",
        "base64_images",
    }
)
_REPLY_ASSET_FIELDS = frozenset(
    {"image_portal_assets", "image_cache_status", "image_cached_at"}
)


def _normalize_reply_assets(value: Any) -> list[dict[str, Any]]:
    """Keep only opaque, bounded Linux reply-asset references."""

    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        try:
            asset_id = int(item.get("id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        digest = str(item.get("sha256", "") or "").strip().lower()
        if asset_id <= 0 or asset_id > 2**63 - 1 or len(digest) != 64:
            continue
        if any(character not in "0123456789abcdef" for character in digest):
            continue
        pair = (asset_id, digest)
        if pair in seen:
            continue
        seen.add(pair)
        result.append({"id": asset_id, "sha256": digest})
    return result


def _project_reply_record(value: Any) -> Any:
    if not isinstance(value, dict):
        return deepcopy(value)
    result = deepcopy(value)
    for field in _REPLY_LOCAL_IMAGE_FIELDS:
        local_value = result.pop(field, None)
        # Keep the explicit empty marker emitted by a newly-created portal
        # image reply.  It lets the Linux UI render a stable shape while any
        # non-empty URL/path/cache reference remains Windows-only and is
        # always removed from the remote projection.
        if field == "image_urls" and local_value == []:
            result[field] = []
    if "image_portal_assets" in result:
        assets = _normalize_reply_assets(result.get("image_portal_assets"))
        result["image_portal_assets"] = assets
        # An empty list is an explicit image deletion marker and must survive
        # projection instead of being mistaken for an omitted legacy field.
    return result


def _project_replies(value: Any) -> Any:
    """Project reply text and opaque asset metadata without local paths."""

    if not isinstance(value, dict):
        return deepcopy(value)
    result = deepcopy(value)
    exact = result.get("exact")
    if isinstance(exact, dict):
        result["exact"] = {
            key: _project_reply_record(record)
            for key, record in exact.items()
        }
    fuzzy_data = result.get("fuzzy_keywords")
    if isinstance(fuzzy_data, dict):
        result["fuzzy_keywords"] = {
            key: _project_reply_record(record)
            for key, record in fuzzy_data.items()
        }
    regex = result.get("regex")
    if isinstance(regex, list):
        projected_regex = []
        for entry in regex:
            if not isinstance(entry, dict):
                continue
            item = deepcopy(entry)
            if "reply" in item:
                item["reply"] = _project_reply_record(item["reply"])
            projected_regex.append(item)
        result["regex"] = projected_regex
    return result


def _reply_entries(replies: Any):
    """Yield mutable reply records keyed exactly as Windows matches them."""

    if not isinstance(replies, dict):
        return
    exact = replies.get("exact")
    if isinstance(exact, dict):
        for key, record in exact.items():
            if isinstance(record, dict):
                yield ("exact", str(key)), record
    fuzzy_data = replies.get("fuzzy_keywords")
    if isinstance(fuzzy_data, dict):
        for key, record in fuzzy_data.items():
            if isinstance(record, dict):
                yield ("fuzzy", str(key)), record
    regex = replies.get("regex")
    if isinstance(regex, list):
        for entry in regex:
            if isinstance(entry, dict) and isinstance(entry.get("reply"), dict):
                yield ("regex", str(entry.get("pattern", ""))), entry["reply"]


def _asset_signature(record: Any) -> tuple[tuple[int, str], ...]:
    if not isinstance(record, dict):
        return ()
    assets = _normalize_reply_assets(record.get("image_portal_assets"))
    return tuple((int(item["id"]), str(item["sha256"])) for item in assets)


def _merge_replies(existing: Any, projected: Any) -> Any:
    """Merge remote reply edits while retaining Windows-only image caches."""

    if not isinstance(projected, dict):
        return deepcopy(projected)
    if projected == {}:
        return {}
    result = deepcopy(projected)
    old_by_key = dict(_reply_entries(existing))
    for key, record in _reply_entries(result):
        old = old_by_key.get(key)
        if not isinstance(old, dict):
            continue
        new_signature = _asset_signature(record)
        old_signature = _asset_signature(old)
        explicit_image_update = (
            "image_portal_assets" in record
            or ("has_images" in record and record.get("has_images") is False)
        )
        if explicit_image_update and new_signature != old_signature:
            # New portal assets invalidate the previous Windows cache.  The
            # downloader will populate fresh references asynchronously.
            continue
        if explicit_image_update and not new_signature:
            for field in _REPLY_LOCAL_IMAGE_FIELDS:
                record.pop(field, None)
            continue
        if not explicit_image_update:
            # A text-only edit from an older portal snapshot has no image
            # marker. Preserve local images so unrelated saves do not disable
            # a working Windows rule.
            for field in _REPLY_LOCAL_IMAGE_FIELDS:
                if field in old and field not in record:
                    record[field] = deepcopy(old[field])
        elif new_signature == old_signature:
            # Same asset IDs: keep a ready cache and its status while the
            # remote text/config edit is applied.
            for field in ("image_cache_refs", "image_cache_status", "image_cached_at"):
                if field in old and field not in record:
                    record[field] = deepcopy(old[field])
    return result


_TIMED_TASK_RUNTIME_FIELDS = frozenset(
    {
        "last_sent_at",
        "last_scheduled_at",
        "sent_count",
        "last_attempt_at",
        "dispatch_lock_until",
        "dispatch_owner",
        "dispatch_priority",
        "dispatch_scheduled_at",
        "next_retry_at",
        "failure_count",
        "last_error",
        "last_error_at",
    }
)


def _merge_timed_messages(existing: Any, projected: Any) -> Any:
    """Merge web task definitions without resetting Windows execution state.

    The task schedule itself is editable on Linux.  The dispatch timestamp and
    count are produced only after the Windows QQ client receives a successful
    result.  Treating them as regular web fields lets an older web snapshot
    reset ``last_sent_at`` to zero and replay a task repeatedly in one minute.
    """
    if not isinstance(projected, list):
        return deepcopy(projected)
    if not projected:
        # An explicitly empty list is the supported web deletion operation.
        return []

    local_by_id = {
        str(item.get("id", "")): item
        for item in (existing if isinstance(existing, list) else [])
        if isinstance(item, dict) and str(item.get("id", ""))
    }
    merged: list[Any] = []
    for item in projected:
        if not isinstance(item, dict):
            merged.append(deepcopy(item))
            continue
        record = deepcopy(item)
        local = local_by_id.get(str(record.get("id", "")))
        if isinstance(local, dict):
            for field in _TIMED_TASK_RUNTIME_FIELDS:
                if field in local:
                    record[field] = deepcopy(local[field])
        merged.append(record)
    return merged


def _project_root_value(key: str, value: Any) -> Any:
    """Project only portal-managed fields from legacy policy dictionaries."""
    if key == "portal_actions":
        if not isinstance(value, (list, tuple)):
            return []
        actions: list[dict[str, Any]] = []
        for raw in list(value)[:32]:
            if not isinstance(raw, dict):
                continue
            action = str(raw.get("action", "")).strip().lower()
            if action not in {"refresh_members", "mute_member", "unmute_member", "kick_member", "blacklist_member", "unblacklist_member", "delete_blacklist_record", "unblacklist_global", "add_global_blacklist", "remove_global_blacklist", "set_member_content", "unbind_steam_id", "apply_member_name_prefix"}:
                continue
            request_id = str(raw.get("request_id", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", request_id):
                continue
            try:
                created_at = int(raw.get("created_at", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                created_at = 0
            # portal_actions are one-shot commands. Rows from before the
            # timestamped action format are stale and must never be replayed
            # when a remote snapshot is retried or restored.
            if created_at <= 0 or abs(int(time.time()) - created_at) > 900:
                continue
            item = {
                "action": action,
                "request_id": request_id,
                "created_at": created_at,
            }
            raw_user_ids = raw.get("user_ids")
            if isinstance(raw_user_ids, (list, tuple, set)):
                normalized_user_ids = []
                for raw_user_id in raw_user_ids:
                    normalized_user_id = str(raw_user_id or "").strip()
                    if normalized_user_id.isdigit() and 5 <= len(normalized_user_id) <= 20 and normalized_user_id not in normalized_user_ids:
                        normalized_user_ids.append(normalized_user_id)
                normalized_user_ids = normalized_user_ids[:200]
            else:
                normalized_user_ids = []
            user_id = str(raw.get("user_id", "")).strip()
            if action not in {"refresh_members", "apply_member_name_prefix"}:
                if normalized_user_ids:
                    item["user_ids"] = normalized_user_ids
                    user_id = normalized_user_ids[0]
                if not user_id.isdigit() or not 5 <= len(user_id) <= 20:
                    continue
                item["user_id"] = user_id
            if action in {"mute_member", "unmute_member"}:
                try:
                    item["duration"] = (
                        0
                        if action == "unmute_member"
                        else max(0, min(2592000, int(raw.get("duration", 0) or 0)))
                    )
                except (TypeError, ValueError, OverflowError):
                    item["duration"] = 0
            if action == "add_global_blacklist":
                reason = str(raw.get("reason", "") or "").replace("\r", " ").replace("\n", " ").strip()
                ban_type = str(raw.get("ban_type", "permanent") or "permanent").strip().lower()
                if ban_type not in {"permanent", "temporary"}:
                    ban_type = "permanent"
                try:
                    duration = max(0, min(2592000, int(raw.get("duration", 0) or 0)))
                except (TypeError, ValueError, OverflowError):
                    duration = 0
                if ban_type == "temporary" and duration <= 0:
                    continue
                item["reason"] = reason[:240]
                item["ban_type"] = ban_type
                item["duration"] = duration if ban_type == "temporary" else 0
            if action == "set_member_content":
                content = str(raw.get("content", "")).replace("\r", " ").replace("\n", " ").strip()
                if content:
                    item["content"] = content[:500]
            if action == "apply_member_name_prefix":
                prefix = str(raw.get("prefix", "") or "").replace("\r", " ").replace("\n", " ").strip()[:20]
                strip_prefix = str(raw.get("strip_prefix", "") or "").replace("\r", " ").replace("\n", " ").strip()[:20]
                raw_include_admins = raw.get("include_admins", True)
                if isinstance(raw_include_admins, str):
                    include_admins = raw_include_admins.strip().lower() not in {
                        "", "0", "false", "off", "no", "否", "关闭"
                    }
                else:
                    include_admins = bool(raw_include_admins)
                item["prefix"] = prefix
                item["strip_prefix"] = strip_prefix
                item["include_admins"] = include_admins
            actions.append(item)
        return actions
    if key == "replies":
        return _project_replies(value)
    fields = SYNC_NESTED_POLICY_FIELDS.get(key)
    if fields is None:
        return deepcopy(value)
    if not isinstance(value, dict):
        return None
    return {field: deepcopy(value[field]) for field in fields if field in value}


def project_group_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the complete web-editable projection of a local group config."""
    source = config if isinstance(config, dict) else {}
    source_features = source.get("features")
    source_features = source_features if isinstance(source_features, dict) else {}
    # Keep this projection sparse.  Older Windows group files can legitimately
    # omit a newly added feature and rely on the local/global default instead.
    # Sending an empty object for every omitted feature would turn that absence
    # into an explicit remote overwrite on the next web save, potentially
    # clearing unrelated Windows-side settings.
    features: dict[str, Any] = {}
    for key in FEATURE_BY_KEY:
        value = source_features.get(key)
        if isinstance(value, dict):
            features[key] = _project_feature_value(key, value)
    power_status = source_features.get("power_status")
    if power_status in {"on", "off"}:
        features["power_status"] = power_status

    projected: dict[str, Any] = {
        "group_id": str(source.get("group_id", "")),
        "features": features,
    }
    for key in SYNC_ROOT_KEYS:
        if key in source:
            value = _project_root_value(key, source[key])
            if value is not None:
                projected[key] = value
    return projected


def merge_group_projection(
    existing: dict[str, Any] | None,
    projected: dict[str, Any] | None,
    *,
    explicit_empty_deletes: bool = False,
) -> dict[str, Any]:
    """Apply a remote projection without overwriting local-only runtime data.

    Older portal snapshots are sparse: an omitted feature/root must not erase
    a Windows-only value.  A current web revision, however, is a deliberate
    save and may use an explicitly empty object to clear the corresponding
    portal-managed values.  ``explicit_empty_deletes`` is set by the Windows
    sync client only for those revision envelopes (identified by the server's
    ``source=web`` metadata), preserving compatibility with sparse snapshots.
    """
    current = deepcopy(existing) if isinstance(existing, dict) else {}
    update = project_group_config(projected)
    update_group_id = str(update.get("group_id", "")).strip()
    if update_group_id:
        current["group_id"] = update_group_id

    current_features = current.get("features")
    if not isinstance(current_features, dict):
        current_features = {}
    # Keep the original feature map too, so an old/incomplete server snapshot
    # cannot turn every omitted Windows feature into an empty (and therefore
    # disabled) configuration during an unrelated web save.
    raw_features = projected.get("features") if isinstance(projected, dict) else {}
    raw_features = raw_features if isinstance(raw_features, dict) else {}
    update_features = update.get("features", {})
    if (
        explicit_empty_deletes
        and isinstance(projected, dict)
        and projected.get("features") == {}
    ):
        # An explicitly empty current web feature map is a complete clear. Do
        # not remove unknown legacy/local feature keys that are outside the
        # portal catalog.
        current_features = {
            key: value
            for key, value in current_features.items()
            if key not in FEATURE_BY_KEY
        }
    for key in FEATURE_BY_KEY:
        value = update_features.get(key)
        if key in raw_features and isinstance(value, dict):
            current_features[key] = deepcopy(value)
    if (
        "power_status" in raw_features
        and update_features.get("power_status") in {"on", "off"}
    ):
        current_features["power_status"] = update_features["power_status"]
    current["features"] = current_features

    for key in SYNC_ROOT_KEYS:
        if key in update:
            policy_fields = SYNC_NESTED_POLICY_FIELDS.get(key)
            if policy_fields is None:
                if key == "replies":
                    current[key] = _merge_replies(current.get(key), update[key])
                elif key == "timed_messages":
                    current[key] = _merge_timed_messages(current.get(key), update[key])
                else:
                    current[key] = deepcopy(update[key])
                continue
            existing_value = current.get(key)
            merged_value = (
                deepcopy(existing_value) if isinstance(existing_value, dict) else {}
            )
            incoming_value = update[key]
            if isinstance(incoming_value, dict):
                if explicit_empty_deletes and not incoming_value:
                    # An explicitly present empty policy object is a delete
                    # instruction for the portal-managed fields.  Older
                    # snapshots often contain additional Windows-only keys
                    # in the same mirror; retain those local fields while
                    # removing stale values that the web form deliberately
                    # cleared.  An omitted root key is still handled above
                    # as a legacy/sparse projection and leaves the local
                    # policy untouched.
                    for field in policy_fields:
                        merged_value.pop(field, None)
                else:
                    for field in policy_fields:
                        if field in incoming_value:
                            merged_value[field] = deepcopy(incoming_value[field])
            current[key] = merged_value
        # An absent root collection is not a delete instruction.  All current
        # web forms express a deletion explicitly with an empty ``{}`` or
        # ``[]`` (for example ``timed_messages=[]``); an omitted key instead
        # occurs when an older portal snapshot predates that form.  Preserving
        # it prevents an unrelated web save from erasing local auto replies,
        # scheduled tasks, sub-admins, or player replies on Windows.
    return current
