import json
import os
import time
import tempfile
import threading
import re
from copy import deepcopy
from pathlib import Path
try:  # AstrBot supplies these in production.
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
    from astrbot.api import logger
except ImportError:  # Standalone config migration/tests.
    try:
        from ..runtime_compat import get_astrbot_data_path, logger
    except ImportError:
        from runtime_compat import get_astrbot_data_path, logger

# 内存缓存: {group_id: (config, file_mtime, cache_time)}
_config_cache = {}
# A new or freshly recreated empty directory is not proof that this Windows
# installation intentionally removed every group.  The sync loop treats an
# empty group list as authoritative only after this marker was created by a
# successful local group-config write.
_PORTAL_SYNC_INITIALIZED_MARKER = ".portal_group_configs_initialized"
# ``load_group_configs_with_health`` keeps the old two-value API for callers
# that only need the authoritative-deletion bit.  The sync client also needs
# to distinguish a readable, genuinely empty directory (common immediately
# after a manual group authorization) from a directory that could not be
# read.  Store that diagnostic on the loader function so old callers and
# hot-reloaded test doubles remain compatible.
_group_configs_source_readable = False
_cache_ttl = 5  # 缓存有效期 5 秒
_cache_lock = threading.RLock()  # 缓存并发安全锁
_history_lock = threading.RLock()

# The web portal is intentionally pull-only: Windows is the configuration
# authority and never accepts an inbound connection from the server.  A local
# edit can nevertheless wake the already configured outbound sync loop right
# away instead of waiting for its next regular heartbeat.  Keep one replaceable
# listener (rather than a list) so a hot-reloaded plugin cannot leave stale
# callbacks behind.
_config_change_listener = None
_config_change_listener_token = None
_runtime_change_listener = None
_runtime_change_listener_token = None

# Only these values are part of the safe portal projection.  Runtime data such
# as message caches, sign-in history, audit queues, and blacklist events may be
# written frequently; waking a network exchange for them would add needless
# traffic without changing anything that the Linux visual editor can display.
_PORTAL_SYNC_ROOT_KEYS = frozenset({
    "features",
    "audit_enabled",
    "approval_mode",
    "verification_timeout",
    "max_attempts",
    "kick_on_verification_failure",
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
    "replies",
    "timed_messages",
    "timed_message_settings",
    "sub_admins",
    "player_replies",
    "lottery",
    "nickname",
    "tags",
    "priority",
    "bot_account_priorities",
    "group_note",
    "timezone",
})

# Runtime records are intentionally excluded from the editable web
# projection.  These keys feed the read-only panels, so a write should mark
# its group for the next bounded runtime-summary upload.  High-volume message
# cache and reply-use writes remain on the normal round-robin heartbeat.
_PORTAL_RUNTIME_SYNC_KEYS = frozenset({
    # Blacklist records are the most common read-only panel edit and need to
    # disappear from Linux immediately after a Windows command removes them.
    # Other high-volume runtime stores remain on the bounded heartbeat to
    # avoid waking an HTTPS exchange for every moderation/statistics event.
    "blacklist",
    # A portal member refresh writes the Windows-side cache directly. Mark it
    # dirty so the refreshed list is uploaded on the very next exchange.
    "cached_members",
    # Compact feature failures are read-only diagnostics shown by the Linux
    # portal.  They are bounded by qqadmin.feature_diagnostics.
    "feature_errors",
    # Every moderation command and scheduled task writes an operation log.
    # Wake the runtime sync so the Linux record panel reflects the result
    # without waiting for the next bounded heartbeat.
    "operation_logs",
})


def _portal_projection_changed(previous: dict, current: dict) -> bool:
    """Return whether a save changed data the Linux visual editor can use.

    ``save_group_config`` is also used for high-frequency runtime state such
    as recall logs and message-cache maintenance.  Those writes must remain
    local: waking the portal exchange for each one would create needless
    traffic when several groups are active.  Compare only the customer-editable
    part of a group file, without serializing or copying large runtime records.
    """
    before = previous if isinstance(previous, dict) else {}
    after = current if isinstance(current, dict) else {}

    # ``features`` is the portal's main allow-listed configuration container,
    # including the per-group power state.  Missing or malformed legacy values
    # are equivalent to the empty feature map used by the portal projection.
    before_features = before.get("features")
    after_features = after.get("features")
    if (before_features if isinstance(before_features, dict) else {}) != (
        after_features if isinstance(after_features, dict) else {}
    ):
        return True

    missing = object()
    for key in _PORTAL_SYNC_ROOT_KEYS:
        if key == "features":
            continue
        if before.get(key, missing) != after.get(key, missing):
            return True
    return False


def set_group_config_change_listener(listener):
    """Install the current plugin's local-configuration change callback.

    The callback is deliberately given only a group ID.  It must never receive
    raw group messages or authorization material, and the outbound client will
    still apply its own privacy-limited projection before sending anything.
    Returning a token lets an old hot-reloaded instance clear only its own
    listener without disconnecting the replacement instance.
    """
    if listener is not None and not callable(listener):
        raise TypeError("group config change listener must be callable or None")
    token = object() if listener is not None else None
    global _config_change_listener, _config_change_listener_token
    with _cache_lock:
        _config_change_listener = listener
        _config_change_listener_token = token
    return token


def clear_group_config_change_listener(token=None) -> None:
    """Clear a listener, but never let a stale plugin clear a newer one."""
    global _config_change_listener, _config_change_listener_token
    with _cache_lock:
        if token is None or token is _config_change_listener_token:
            _config_change_listener = None
            _config_change_listener_token = None


def set_group_runtime_change_listener(listener):
    """Install a callback for read-only runtime-summary changes."""
    if listener is not None and not callable(listener):
        raise TypeError("group runtime change listener must be callable or None")
    token = object() if listener is not None else None
    global _runtime_change_listener, _runtime_change_listener_token
    with _cache_lock:
        _runtime_change_listener = listener
        _runtime_change_listener_token = token
    return token


def clear_group_runtime_change_listener(token=None) -> None:
    """Clear only the matching runtime listener during a plugin reload."""
    global _runtime_change_listener, _runtime_change_listener_token
    with _cache_lock:
        if token is None or token is _runtime_change_listener_token:
            _runtime_change_listener = None
            _runtime_change_listener_token = None


def _notify_portal_config_change(group_id: str) -> None:
    """Best-effort notification after one web-editable group change commits."""
    with _cache_lock:
        listener = _config_change_listener
    if listener is None:
        return
    try:
        listener(str(group_id))
    except Exception as exc:
        # The JSON write already succeeded.  A notification failure must not
        # report a false failure to a group administrator; periodic sync will
        # still detect the changed projection later.
        logger.debug("群配置即时同步通知失败: %s", exc)


def _notify_portal_runtime_change(group_id: str) -> None:
    """Best-effort notification for a read-only runtime panel update."""
    with _cache_lock:
        listener = _runtime_change_listener
    if listener is None:
        return
    try:
        listener(str(group_id))
    except Exception as exc:
        # The runtime write already committed; the periodic heartbeat remains
        # a safe fallback when a listener is unavailable during reload.
        logger.debug("runtime sync notification failed: %s", exc)


def _normalize_group_id(group_id: str) -> str:
    """Return a safe, canonical group ID for a config file name."""
    normalized = str(group_id).strip()
    if not normalized or not re.fullmatch(r"[A-Za-z0-9_-]+", normalized):
        raise ValueError("group_id contains unsafe characters")
    return normalized


def _atomic_write_json(path: Path | str, data: dict) -> None:
    """Persist JSON without exposing a truncated file if the process stops mid-write."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False, dir=target.parent,
            prefix=f".{target.name}.", suffix=".tmp"
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(data, temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, target)
    except Exception:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def _read_group_config_unlocked(group_id: str) -> dict:
    """Load a config while the caller holds _cache_lock."""
    config_file = get_group_config_file(group_id)
    if not config_file.exists():
        return {}
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config if isinstance(config, dict) else {}
    except Exception as e:
        logger.error(f"加载群组 {group_id} 配置失败: {e}")
        return {}


def _save_group_config_unlocked(group_id: str, config: dict) -> None:
    """Save a config while the caller holds _cache_lock."""
    config_file = get_group_config_file(group_id)
    _atomic_write_json(config_file, config)
    _config_cache[group_id] = (config, config_file.stat().st_mtime, time.monotonic())
    _mark_portal_sync_source_initialized()


def _portal_sync_marker_file() -> Path:
    """Return the durable marker for authoritative empty-list eligibility."""

    return get_plugin_data_path() / _PORTAL_SYNC_INITIALIZED_MARKER


def _portal_sync_source_initialized() -> bool:
    """Return whether at least one local group config was safely persisted."""

    try:
        return _portal_sync_marker_file().is_file()
    except OSError:
        return False


def _mark_portal_sync_source_initialized() -> None:
    """Persist an empty-state eligibility marker without storing group data."""

    try:
        _portal_sync_marker_file().touch(exist_ok=True)
    except OSError:
        # A failure is intentionally conservative: an empty source remains
        # non-authoritative rather than deleting portal snapshots.
        logger.warning("Unable to persist group-config sync initialization marker")

def get_plugin_data_path():
    data_path = Path(get_astrbot_data_path()) / "plugin_data" / "qqadmin"
    data_path.mkdir(parents=True, exist_ok=True)
    return data_path

def get_groups_config_dir():
    """获取群配置目录"""
    config_dir = get_plugin_data_path() / "group_configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def _default_game_commands():
    """Load starter templates without coupling config migration to runtime."""
    try:
        from .game_commands import get_default_game_commands

        return get_default_game_commands()
    except Exception:
        # Older standalone migrations remain usable if the optional game
        # command module is temporarily unavailable.
        return []


def _default_steam_signin_profiles() -> list[dict]:
    """Return safe, disabled starter rows for the web Steam sign-in editor.

    These rows are examples only: they make the required command, schedule,
    reward and SteamID flow visible on a fresh group without silently enabling
    a game reward command before the owner has selected a database/server.
    """

    return [
        {
            "id": "steam-daily-example",
            "name": "Steam 每日签到示例（修改后启用）",
            "command": "签到游戏",
            "mode": "steam",
            "schedule": "daily",
            "enabled": False,
            "base_reward": 10,
            "reward_3_days": 30,
            "reward_7_days": 100,
            "reward_14_days": 200,
            "reward_30_days": 500,
            "steam_reward_mode": "database",
            "steam_reward_operation": "execute_game_command",
            "steam_reward_command": "#ChangeCurrencyBalance Normal {reward} {steam_id}",
            "steam_reward_failure_message": "签到奖励执行失败，未完成本次签到，请稍后重试。",
            "signin_message": "{name} 的 Steam 签到成功，第 {count} 次，奖励 {reward}。",
        }
    ]


def _default_database_profiles() -> list[dict]:
    """Database profiles are created from a group FTP/server archive."""

    return []

def get_group_config_file(group_id: str):
    """获取指定群组的配置文件路径"""
    return get_groups_config_dir() / f"{_normalize_group_id(group_id)}.json"

def get_config_history_file():
    return os.path.join(get_plugin_data_path(), "config_history.json")

def get_default_feature_config():
    """获取默认的功能配置（所有功能默认关闭）"""
    return {
        "audit": {
            "enabled": False,
            "approval_mode": "math",
            "verification_timeout": 300,
            "max_attempts": 3,
            "kick_on_verification_failure": False,
            "admin_bypass": True,
            "enable_invite_audit": True,
            "verification_timeout_message": "用户 {user_name}({user_id}) 验证超时，{reason}。",
            "approval_success_message": "已批准用户 {user_name}({user_id}) 入群。",
            "approval_failure_message": "审核失败：{reason}",
            "rejection_success_message": "已拒绝用户 {user_name}({user_id}) 的入群申请。理由：{reason}",
        },
        "blacklist": {
            "enabled": False,
            "auto_kick": True,
            "auto_blacklist_on_leave": False,
            "auto_blacklist_on_verify_fail": False,
            "auto_kick_on_join": True,
            "notify_on_kick": True,
            "kick_reason": "已被加入黑名单",
            "max_auto_kick_per_day": 10,
            "auto_kick_message": "黑名单用户 {user_id} 已被移出本群，原因：{reason}",
            "leave_blacklist_message": "用户 {user_name}({user_id}) 主动退群，已加入黑名单。",
            "blacklist_success_message": "已拉黑用户 {user_id}。",
            "blacklist_failure_message": "拉黑失败：{reason}",
            "unblacklist_success_message": "已解除用户 {user_id} 的拉黑。",
            "unblacklist_failure_message": "解除拉黑失败：{reason}",
        },
        "welcome": {
            "enabled": False,
            "auto_welcome": False,
            "delay": 0,
            "message": "欢迎 {user_name} 加入本群！",
            "farewell_enabled": False,
            "farewell_message": "{user_name} 离开了本群",
            "keywords": ["欢迎", "welcome", "入群"],
            "at_new_member": True,
            "auto_reply_enabled": True
        },
        "recall": {
            "enabled": False,
            "self_recall": True,
            "time_limit": 60,
            "mode": "keyword",
            "keywords": [],
            "regex_patterns": [],
            "notification": False,
            "enable_admin_recall": True,
            "member_scope": "all",
            "enable_sensitive_detection": True,
            "kick_on_severe_violation": True,
            "sensitive_words": ["色情", "黄图", "黄片", "裸聊", "裸照", "成人视频"],
            "notification_message": "您的消息包含违规内容，已被撤回",
            "manual_failure_message": "撤回失败：{reason}"
        },
        "message_cache": {
            "enabled": True,
            "cache_raw_message": True,
            "retention_days": 0,
            "max_messages_per_user": 0,
            "archive_images": True,
            "archive_image_max_mb": 20,
        },
        "mute": {
            "enabled": False,
            "level": 5,
            "duration": 300,
            "auto_mute_new_member": False,
            "auto_mute_duration": 300,
            "auto_escalate": True,
            "repeat_threshold": 1,
            "repeat_extension": 300,
            "max_level": 5,
            "level_durations": [300, 600, 1800, 3600, 7200],
            "mention_mute_rules": [],
            "mute_success_message": "已禁言群「{group_name}」中的用户 {user_id}，时长：{duration}。",
            "unmute_success_message": "已解除群「{group_name}」中用户 {user_id} 的禁言。",
            "mute_failure_message": "禁言失败：{reason}",
        },
        "kick": {
            "enabled": False,
            "auto_blacklist": False,
            "limit": 3,
            "kick_success_message": "用户 {user_id} 已被移出群聊，原因：{reason}",
            "kick_failure_message": "踢出失败：{reason}"
        },
        "stats": {
            "enabled": False,
            "update_interval": 300,
            "auto_update_on_join": True,
            "abnormal_invite_threshold": 5,
            "signin_base_reward": 10,
            "signin_reward_3_days": 30,
            "signin_reward_7_days": 100,
            "signin_reward_14_days": 200,
            "signin_reward_30_days": 500,
            # Sign-in records and SteamID bindings are runtime-only root data;
            # these fields are the editable policy projected to the web.
            "signin_mode": "basic",
            "schedule_mode": "daily",
            "signin_command": "签到",
            "bind_command": "绑定SteamID",
            "steamid_verification_required": False,
            "unverified_mute_duration": 300,
            "unverified_mute_message": "请先完成 SteamID 验证后再发言。",
            "signin_message": "{name} 的签到成功，第 {count} 次，奖励 {reward}。",
            "bind_success_message": "绑定成功：{name} -> {steam_id}",
            "bind_failure_message": "SteamID 绑定失败：{message}",
            "unbind_success_message": "已解除 QQ {user_id} 的 SteamID 绑定。",
            "unbind_failure_message": "SteamID 解绑失败：{message}",
            "signin_reset_success_message": "签到重置成功：{user_id} {date}（{detail}）。",
            "signin_reset_failure_message": "签到重置失败：{message}",
            "duplicate_steam_message": "该 SteamID 已被其他 QQ 绑定，请联系管理员解绑。",
            "steam_not_found_message": "未找到该 SteamID，请确认已加入当前服务器。",
            "steam_database_error_message": "SteamID 数据库暂时不可用，请联系管理员。",
            "database_reward_error_message": "奖励发放失败，本次签到未完成，请稍后重试。",
            "database_already_signed_message": "你在当前周期已经签到过了。",
            "database_path": "",
            "database_profile": "",
            "database_profiles": _default_database_profiles(),
            "currency_column": "amount",
            "database_sync": False,
            "steam_reward_mode": "database",
            "steam_reward_operation": "execute_game_command",
            "steam_reward_command": "#ChangeCurrencyBalance Normal {reward} {steam_id}",
            "steam_reward_failure_message": "签到奖励执行失败，未完成本次签到，请稍后重试。",
            "custom_rewards": [],
            "signin_profiles": _default_steam_signin_profiles(),
        },
        "game_commands": {
            "enabled": False,
            "execution_mode": "local",
            "success_message": "游戏指令执行成功：{message}",
            "failure_message": "游戏指令执行失败：{reason}",
            "commands": [],
        },
        "game_servers": {
            "enabled": False,
            "active_profile_id": "",
            "profiles": [],
        },
        # SCUM query commands were historically global-only.  Keep the
        # feature enabled with the legacy command names so existing groups do
        # not lose querying when their config is upgraded, while allowing the
        # portal to override each name independently per group.
        "scum": {
            "enabled": True,
            "allow_server_binding": True,
            "allow_online_query": True,
            "permission_mode": "read_only",
            "keyword_command": "查询",
            "online_command": "查询在线",
        },
        "reply": {
            "enabled": False,
            "cooldown": 0
        },
        "player_reply": {
            "enabled": False,
            "default_at_user": True
        },
        "lottery": {
            "enabled": False,
            "keyword": "抽奖",
            "max_draws_per_user": 0,
            "cooldown": 0,
            "success_message": "🎉 {name} 抽中了「{prize}」！剩余 {remaining}",
            "empty_message": "{name} 很遗憾没有抽中，再试一次吧。",
            "campaigns": [],
        },
        "entertainment": {
            "enabled": False,
            "time_enabled": True,
            "dice_enabled": True,
            "rps_enabled": True,
            "random_enabled": True,
            "coin_enabled": True,
            "fortune_enabled": True,
            "luck_enabled": True,
            "answer_enabled": True,
            "like_enabled": True,
            "music_enabled": True,
            "video_enabled": True,
            "time_command": "当前时间",
            "dice_command": "骰子",
            "rps_command": "猜拳",
            "random_command": "随机",
            "coin_command": "抛硬币",
            "fortune_command": "今日运势",
            "luck_command": "幸运数字",
            "answer_command": "答案之书",
            "like_command": "赞我",
            "music_command": "点歌",
            "music_selection_timeout": 600,
            "music_sources": ["qq", "163"],
            "music_default_source": "qq",
            "video_command": "搜索视频",
            "like_daily_limit": 20,
            "cooldown": 0,
            "max_sides": 100,
            "max_options": 20,
        },
        "text_to_image": {
            "enabled": False,
            "font_size": 16,
            "font_color": "#333333",
            "bg_color": "#ffffff",
            "line_spacing": 4,
            "padding": 20,
            "modify_permission": "admin"
        },
        "timed_message": {
            "enabled": False
        },
        "sub_admin": {
            "enabled": False
        },
        "group_config": {
            "enabled": True,
            "help_command": "群管功能",
            "help_permission": "member",
            "nickname": "",
            # Authoritative SCUM ``user_list`` name used for new Steam binds
            # and the batch rename command.  Empty means use the QQ group name.
            "database_player_name": "",
            # Keep the QQ member card separate from the database player name.
            # New Steam bindings use the verified game name by default.
            "steam_name_as_group_card": True,
            "tags": [],
            "priority": 0,
            "bot_account_priorities": "",
            "group_note": "",
            "timezone": "Asia/Shanghai",
            # Portal-managed member identity rules.  The Windows plugin is
            # the execution authority; the web side only edits these lists.
            "auto_member_name": False,
            "member_name_prefix": "",
            "member_name_prefix_include_admins": True,
            "member_names": [],
            "auto_member_title": False,
            "member_titles": ["*=群成员"],
        },
        "keyword_filter": {
            "enabled": False,
            "filter_action": "warn",
            "filter_mute_duration": 300,
            "filter_keywords": ["广告", "推广", "二维码", "vx", "微信", "qq", "群号", "拉人"],
            "filter_regex_patterns": [],
            "admin_bypass_filter": True,
            "enable_log": True,
            # These texts are an editable per-group policy.  Keep the
            # singular warning for legacy configurations and use the list for
            # random warning/delete_warn replies.
            "warn_message": "您的消息包含敏感内容，请遵守群规！",
            "warning_messages": ["您的消息包含敏感内容，请遵守群规！"],
            "mute_message": "{user_id} 已被禁言，请遵守群规。",
            "kick_message": "{user_id} 已被移出本群。",
            "blacklist_message": "{user_id} 已加入黑名单。",
            "repeat_user_enabled": False,
            "repeat_user_threshold": 3,
            "repeat_user_window": 60,
            "repeat_user_action": "delete",
            "repeat_group_enabled": False,
            "repeat_group_threshold": 3,
            "repeat_group_window": 60,
            "repeat_group_action": "delete",
            "repeat_mute_duration": 300,
            "repeat_notice": "检测到重复刷屏，已处理。",
        }
    }


_GLOBAL_STATS_POLICY_KEYS = (
    "signin_mode",
    "schedule_mode",
    "signin_command",
    "bind_command",
    "signin_message",
    "bind_success_message",
    "bind_failure_message",
    "unbind_success_message",
    "unbind_failure_message",
    "signin_reset_success_message",
    "signin_reset_failure_message",
    "duplicate_steam_message",
    "steam_not_found_message",
    "steam_database_error_message",
    "database_reward_error_message",
    "database_already_signed_message",
    "database_path",
    "database_profile",
    "database_profiles",
    "currency_column",
    "database_sync",
    "steam_reward_mode",
    "steam_reward_operation",
    "steam_reward_command",
    "steam_reward_failure_message",
    "custom_rewards",
    "signin_profiles",
)


def _copy_global_stats_policy(stats_config: object, target_stats: dict) -> None:
    """Copy every web-editable global sign-in policy into a group snapshot."""
    if not isinstance(stats_config, dict) or not isinstance(target_stats, dict):
        return
    for key in _GLOBAL_STATS_POLICY_KEYS:
        if key in stats_config:
            target_stats[key] = deepcopy(stats_config[key])


def _preserve_power_status(existing_config: dict, target_config: dict) -> None:
    """Copy a group's explicit runtime power state into replacement config."""
    if not isinstance(existing_config, dict) or not isinstance(target_config, dict):
        return
    existing_features = existing_config.get("features", {})
    if not isinstance(existing_features, dict):
        return
    power_status = existing_features.get("power_status")
    if power_status in {"on", "off"}:
        target_config.setdefault("features", {})["power_status"] = power_status


# These payloads are group-owned runtime state, never global defaults.  Keep
# them explicitly even if a future global schema happens to add the same key.
_GROUP_RUNTIME_DATA_KEYS = frozenset({
    "timed_messages",
    "timed_message_settings",
})

# These settings are configured in a group's own portal page.  A global
# default sync must never replace the choice already made for that group.
_GROUP_OWNED_FEATURE_SETTINGS = frozenset({
    ("entertainment", "music_sources"),
    ("entertainment", "music_default_source"),
    ("entertainment", "music_selection_timeout"),
})


def _preserve_group_local_runtime_data(existing_config: dict, target_config: dict) -> None:
    """Merge group-owned runtime data into a freshly generated config.

    ``clone_global_config_to_group`` intentionally replaces global policy
    fields.  Group history, statistics, invitations, replies, schedules, and
    future runtime payloads live at the config root and must survive that
    replacement.  Unknown feature fields are also retained for forward
    compatibility, while generated feature settings continue to follow the
    current global configuration.
    """
    if not isinstance(existing_config, dict) or not isinstance(target_config, dict):
        return

    existing_features = existing_config.get("features")
    target_features = target_config.get("features")
    if isinstance(existing_features, dict) and isinstance(target_features, dict):
        for feature_name, existing_feature in existing_features.items():
            if feature_name not in target_features:
                target_features[feature_name] = deepcopy(existing_feature)
                continue
            target_feature = target_features.get(feature_name)
            if not isinstance(existing_feature, dict) or not isinstance(target_feature, dict):
                continue
            for setting, value in existing_feature.items():
                if setting == "power_status":
                    continue
                if (
                    (feature_name, setting) in _GROUP_OWNED_FEATURE_SETTINGS
                    or setting not in target_feature
                ):
                    target_feature[setting] = deepcopy(value)

    for field, value in existing_config.items():
        if field in _GROUP_RUNTIME_DATA_KEYS or field not in target_config:
            target_config[field] = deepcopy(value)


def load_group_config(group_id: str) -> dict:
    """加载指定群组的配置文件（带内存缓存和ttl机制）
    
    缓存策略：
    1. 检查缓存是否存在且未过期（<5秒）
    2. 如果缓存有效，返回缓存数据
    3. 检查文件是否被修改（mtime比较）
    4. 如果文件被修改或缓存过期，重新加载
    """
    try:
        group_id = _normalize_group_id(group_id)
    except ValueError as e:
        logger.warning(f"拒绝无效群组配置 ID: {group_id!r} ({e})")
        return {}

    config_file = get_group_config_file(group_id)
    cache_key = group_id
    
    with _cache_lock:  # 线程安全访问缓存
        current_time = time.monotonic()
        
        try:
            file_mtime = config_file.stat().st_mtime if config_file.exists() else 0
            
            # 检查缓存
            if cache_key in _config_cache:
                cached_config, cached_mtime, cache_time = _config_cache[cache_key]
                cache_age = current_time - cache_time
                
                # 缓存有效：未过期且文件未修改
                if cache_age < _cache_ttl and cached_mtime == file_mtime and file_mtime > 0:
                    return cached_config
            
            # 缓存无效或过期，重新加载
            if config_file.exists():
                config = _read_group_config_unlocked(group_id)
                if config:
                    # 更新缓存：(配置, 文件修改时间, 缓存创建时间)
                    _config_cache[cache_key] = (config, file_mtime, current_time)
                    return config
        except Exception as e:
            logger.error(f"加载群组 {group_id} 配置失败: {e}")
    
    return {}

def load_group_configs_with_health() -> tuple[dict, bool]:
    """Load all group configs and report whether the directory was complete.

    An empty result is ambiguous when a JSON file is corrupt or the data
    directory is temporarily unavailable.  The Windows-to-Linux sync protocol
    must not treat that ambiguity as an authoritative deletion of every group.
    """
    global _group_configs_source_readable
    all_configs = {}
    healthy = True
    source_readable = False
    try:
        config_dir = get_groups_config_dir()
        # Reaching and enumerating the directory is meaningful even when it
        # contains no JSON files.  An empty directory is readable, but remains
        # non-authoritative until the initialization marker exists.
        source_readable = True
        for file in config_dir.iterdir():
            if file.suffix != ".json":
                continue
            try:
                group_id = _normalize_group_id(file.stem)
            except ValueError:
                # A JSON-looking file is part of the source set even when its
                # name is malformed.  Ignoring it would make an incomplete
                # or partially restored directory look authoritative and
                # could let the portal tombstone a real group.  Keep the
                # valid subset for diagnostics, but disable authoritative
                # deletion until the operator removes/repairs the file.
                logger.error(f"加载群组配置失败：文件名无效 {file.name}")
                healthy = False
                source_readable = False
                continue
            try:
                with open(file, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception as e:
                logger.error(f"加载群组 {group_id} 配置失败: {e}")
                healthy = False
                source_readable = False
                continue
            if isinstance(config, dict):
                all_configs[group_id] = config
            else:
                logger.error(f"加载群组 {group_id} 配置失败: JSON 根节点不是对象")
                healthy = False
                source_readable = False
    except Exception as e:
        logger.error(f"加载所有群组配置失败: {e}")
        healthy = False
        source_readable = False
    # A non-empty, fully readable directory establishes the marker for older
    # installations that predate it.  Conversely, an empty directory without
    # that evidence is a first-run/recovery state and must not authorize an
    # outbound empty-list tombstone.
    if healthy and all_configs:
        _mark_portal_sync_source_initialized()
    _group_configs_source_readable = bool(source_readable)
    # An empty directory without the marker is readable but not authoritative;
    # keep those two states separate for the outbound protocol.  The public
    # return value remains the historical ``(configs, complete)`` pair.
    complete = bool(healthy and (all_configs or _portal_sync_source_initialized()))
    # Keep a readability attribute for the outbound sync module.  A
    # monkey-patched legacy loader will not have it and is conservatively
    # treated as readable exactly when it reports ``healthy=True``.
    try:
        load_group_configs_with_health.last_readable = bool(source_readable)
    except Exception:
        pass
    return all_configs, complete


def load_group_configs() -> dict:
    """加载所有群组的配置文件（兼容旧调用方）。"""
    configs, _healthy = load_group_configs_with_health()
    return configs

def save_group_configs(configs: dict):
    """批量保存群组配置文件"""
    for group_id, config in configs.items():
        save_group_config(group_id, config)

def save_group_config(group_id: str, config: dict):
    """保存指定群组的配置文件（并更新缓存）"""
    try:
        group_id = _normalize_group_id(group_id)
    except ValueError as e:
        logger.error(f"保存群组配置失败: {e}")
        return False
    try:
        with _cache_lock:
            previous = _read_group_config_unlocked(group_id)
            portal_projection_changed = _portal_projection_changed(previous, config)
            _save_group_config_unlocked(group_id, config)
        if portal_projection_changed:
            _notify_portal_config_change(group_id)
        return True
    except Exception as e:
        logger.error(f"保存群组 {group_id} 配置失败: {e}")
        return False

def get_group_data(group_id: str, data_key: str, default=None):
    """获取分群数据（如黑名单、禁言列表等），直接存储在配置根目录"""
    config = load_group_config(group_id)
    if not config:
        return default
    return config.get(data_key, default)

def set_group_data(group_id: str, data_key: str, data_value):
    """设置分群数据（如黑名单、禁言列表等），直接存储在配置根目录"""
    update_group_data(group_id, data_key, lambda _old_data: data_value)

def update_group_data(group_id: str, data_key: str, update_func):
    """
    原子性更新分群数据，直接存储在配置根目录
    update_func: 一个函数，接收旧数据，返回新数据
    """
    group_id = _normalize_group_id(group_id)
    with _cache_lock:
        config = _read_group_config_unlocked(group_id)
        if not config:
            config = {
                "group_id": group_id,
                "enabled": True,
                "features": get_default_feature_config(),
                "created_at": int(time.time()),
            }

        new_data = update_func(config.get(data_key))
        config[data_key] = new_data
        config["updated_at"] = int(time.time())
        _save_group_config_unlocked(group_id, config)
    if data_key in _PORTAL_SYNC_ROOT_KEYS:
        _notify_portal_config_change(group_id)
    elif data_key in _PORTAL_RUNTIME_SYNC_KEYS:
        _notify_portal_runtime_change(group_id)
    return new_data

def batch_update_group_data(group_id: str, updates: list):
    """
    批量更新分群数据，在一次文件读写中完成多个更新操作
    updates: 一个列表，每个元素是 (data_key, update_func) 的元组
    返回: 更新后的配置
    """
    group_id = _normalize_group_id(group_id)
    changed_keys = set()
    with _cache_lock:
        config = _read_group_config_unlocked(group_id)
        if not config:
            config = {
                "group_id": group_id,
                "enabled": True,
                "features": get_default_feature_config(),
                "created_at": int(time.time()),
            }

        for data_key, update_func in updates:
            config[data_key] = update_func(config.get(data_key))
            changed_keys.add(data_key)

        config["updated_at"] = int(time.time())
        _save_group_config_unlocked(group_id, config)
    if changed_keys & _PORTAL_SYNC_ROOT_KEYS:
        _notify_portal_config_change(group_id)
    if changed_keys & _PORTAL_RUNTIME_SYNC_KEYS:
        _notify_portal_runtime_change(group_id)
    return config

def init_group_config(group_id: str, global_config: dict = None) -> dict:
    """初始化群组配置：创建默认配置或从全局配置克隆"""
    group_id = _normalize_group_id(group_id)
    config = {
        "group_id": group_id,
        "enabled": True,
        "features": get_default_feature_config(),
        "created_at": int(time.time()),
        "updated_at": int(time.time())
    }

    # 如果提供了全局配置，使用全局配置初始化
    if global_config:
        config = clone_global_config_to_group_config(group_id, global_config)

    save_group_config(group_id, config)
    return config

def load_config_history() -> dict:
    history_file = get_config_history_file()
    try:
        if os.path.exists(history_file):
            with open(history_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"加载配置历史失败: {e}")
    return {}

def save_config_history(history: dict):
    history_file = get_config_history_file()
    try:
        with _history_lock:
            _atomic_write_json(history_file, history)
    except Exception as e:
        logger.error(f"保存配置历史失败: {e}")

def get_group_config(group_id: str, global_config: dict = None) -> dict:
    """获取群组配置，如果不存在则初始化，自动补全缺失的功能模块"""
    config = load_group_config(group_id)
    if not config:
        # 初始化新群组配置
        config = init_group_config(group_id, global_config)
    else:
        # 检查并补全缺失的功能模块（兼容旧版本配置）
        features = config.get("features", {})
        if not isinstance(features, dict):
            features = {}
        default_features = get_default_feature_config()
        updated = not isinstance(config.get("features", {}), dict)
        
        for feature_name, default_config in default_features.items():
            if feature_name not in features:
                # Preserve the value from legacy root/nested mirrors when a
                # partially synchronized group file has not got this
                # feature node yet.  Without this migration, a saved
                # ``audit_enabled: true`` would be replaced by the default
                # ``features.audit.enabled: false`` on the next read.
                feature_value = (
                    dict(default_config)
                    if isinstance(default_config, dict)
                    else default_config
                )
                root_mirror = {
                    "audit": "audit_enabled",
                    "welcome": "welcome_enabled",
                    "recall": "recall_enabled",
                    "stats": "stats_enabled",
                    "reply": "reply_enabled",
                    "player_reply": "player_reply_enabled",
                    "entertainment": "entertainment_enabled",
                }.get(feature_name)
                nested_mirror = {
                    "blacklist": "blacklist_settings",
                    "mute": "mute_settings",
                    "kick": "kick_settings",
                }.get(feature_name)
                if isinstance(feature_value, dict):
                    if root_mirror in config:
                        feature_value["enabled"] = bool(config[root_mirror])
                    elif nested_mirror and isinstance(config.get(nested_mirror), dict):
                        mirror = config[nested_mirror]
                        if "enabled" in mirror:
                            feature_value["enabled"] = bool(mirror["enabled"])
                features[feature_name] = feature_value
                updated = True
            elif isinstance(default_config, dict):
                if not isinstance(features[feature_name], dict):
                    features[feature_name] = default_config
                    updated = True
                    continue
                # 补全已有功能中缺失的字段
                for key, value in default_config.items():
                    if key not in features[feature_name]:
                        features[feature_name][key] = value
                        updated = True

        # ``id`` was exposed by an early portal build as "QQ 号验证", but the
        # Windows audit workflow has no independent QQ-number challenge to
        # execute.  Migrate persisted legacy values to the supported math
        # challenge instead of leaving a group on a dead mode after upgrade.
        # Keep the ID verification mode intact.  It is a supported challenge
        # type; rewriting it to math here made a web-selected ID workflow
        # disappear on the next configuration read.  The audit helpers
        # normalize only unknown values.
        
        if updated:
            config["features"] = features
            save_group_config(group_id, config)
    
    return config

def get_group_feature_setting(group_id: str, feature: str, setting: str = None, global_config: dict = None):
    """获取群功能配置（兼容带有 _enabled 后缀的功能名称）
    如果分群没有配置，自动克隆全局配置到分群
    如果分群配置缺少某个功能的配置，从全局配置中读取
    """
    config = get_group_config(group_id, global_config)
    features = config.get("features", {})

    # 如果 features 为空，说明是新的分群配置，需要克隆全局配置
    if not features and global_config:
        features = clone_global_to_group(group_id, global_config)
        config["features"] = features
        save_group_config(group_id, config)

    # 处理带有 _enabled 后缀的功能名称
    feature_name = feature
    if feature_name.endswith("_enabled"):
        feature_name = feature_name[:-8]

    # 获取分群的功能配置
    feature_config = features.get(feature_name, {})
    
    if setting:
        # 如果分群配置中没有该设置，从全局配置中读取
        if setting not in feature_config and global_config:
            qqadmin_config = global_config.get("qqadmin", {})
            global_feature_config = qqadmin_config.get(feature_name, {})
            return global_feature_config.get(setting)
        return feature_config.get(setting)

    # 如果没有指定 setting，返回功能的启用状态（兼容旧调用方式）
    # 如果分群配置中没有 enabled，从全局配置中读取
    if "enabled"not in feature_config and global_config:
        qqadmin_config = global_config.get("qqadmin", {})
        global_feature_config = qqadmin_config.get(feature_name, {})
        return global_feature_config.get("enabled", False)
    return feature_config.get("enabled", False)


def clone_global_to_group(group_id: str, global_config: dict) -> dict:
    """克隆全局配置到分群
    将 qqadmin.xxx 映射为 features.xxx
    注意：全局是 qqadmin.reply.enable_reply，分群是 features.reply.enabled
    """
    from copy import deepcopy
    qqadmin = global_config.get("qqadmin", {})

    # 需要映射 enable_xxx 到 enabled 的模块列表
    enable_mapping = {
        "reply": "enable_reply",
        "recall": "enable_recall",
        "message_cache": "enable_message_cache",
        "mute": "enable_mute",
        "kick": "enable_kick",
        "blacklist": "enable_blacklist",
        "audit": "enable_audit",
        "welcome": "enable_welcome",
        "group_config": "enable_group_config",
        "text_to_image": "enable_text_to_image",
        "timed_message": "enable_timed_message",
        "sub_admin": "enable_sub_admin",
        "keyword_filter": "enable_keyword_filter",
        "player_reply": "enable_player_reply",
        "stats": "enable_stats",
        "lottery": "enable_lottery",
    }

    features = {}
    for module_name, module_config in qqadmin.items():
        if isinstance(module_config, dict):
            features[module_name] = deepcopy(module_config)
            # 映射 enable_xxx 到 enabled 字段
            if module_name in enable_mapping:
                enable_field = enable_mapping[module_name]
                if enable_field in features[module_name]:
                    features[module_name]["enabled"] = features[module_name].pop(enable_field)
            # recall 特殊处理：保留 enable_self_recall -> self_recall_enabled
            if module_name == "recall":
                if "enable_self_recall"in features[module_name]:
                    features[module_name]["self_recall_enabled"] = features[module_name].pop("enable_self_recall")
    return features

def update_group_config(group_id: str, key: str, value, operator_id: str = ""):
    """更新群组基础配置"""
    group_id = _normalize_group_id(group_id)
    with _cache_lock:
        config = _read_group_config_unlocked(group_id)
        if not config:
            config = {
                "group_id": group_id,
                "enabled": True,
                "features": get_default_feature_config(),
                "created_at": int(time.time()),
            }
        old_value = config.get(key)
        config[key] = value
        config["updated_at"] = int(time.time())
        _save_group_config_unlocked(group_id, config)

    if key in _PORTAL_SYNC_ROOT_KEYS:
        _notify_portal_config_change(group_id)
    elif key in _PORTAL_RUNTIME_SYNC_KEYS:
        _notify_portal_runtime_change(group_id)

    history = load_config_history()
    if group_id not in history:
        history[group_id] = []

    history[group_id].append({
        "key": key,
        "old_value": old_value,
        "new_value": value,
        "operator_id": operator_id,
        "timestamp": int(time.time())
    })

    if len(history[group_id]) > 200:
        history[group_id] = history[group_id][-100:]

    save_config_history(history)

def get_group_feature_detail(group_id: str, feature: str, global_config: dict = None) -> dict:
    """获取群功能的详细配置"""
    config = get_group_config(group_id, global_config)
    features = config.get("features", {})

    if feature in features:
        result = features.get(feature, {})
        if feature == "recall" and isinstance(result, dict):
            # New moderation controls are additive; do not overwrite an
            # administrator's explicit false value in an older group file.
            result.setdefault("enable_sensitive_detection", True)
            result.setdefault("kick_on_severe_violation", True)
        return result

    # 返回默认配置
    default_config = get_default_feature_config()
    return default_config.get(feature, {})

def get_group_all_features(group_id: str, global_config: dict = None) -> dict:
    """获取群的所有功能配置"""
    config = get_group_config(group_id, global_config)
    return config.get("features", get_default_feature_config())

def update_group_feature_config(group_id: str, feature: str, setting: str, value, operator_id: str = ""):
    """更新群功能的具体配置"""
    group_id = _normalize_group_id(group_id)
    with _cache_lock:
        config = _read_group_config_unlocked(group_id)
        if not config:
            config = {
                "group_id": group_id,
                "enabled": True,
                "features": get_default_feature_config(),
                "created_at": int(time.time()),
            }
        if not isinstance(config.get("features"), dict):
            config["features"] = get_default_feature_config()
        if feature not in config["features"]:
            config["features"][feature] = {}
        old_value = config["features"][feature].get(setting)
        config["features"][feature][setting] = value
        config["updated_at"] = int(time.time())
        _save_group_config_unlocked(group_id, config)

    _notify_portal_config_change(group_id)

    history = load_config_history()
    if group_id not in history:
        history[group_id] = []

    history[group_id].append({
        "key": f"features.{feature}.{setting}",
        "old_value": old_value,
        "new_value": value,
        "operator_id": operator_id,
        "timestamp": int(time.time())
    })

    if len(history[group_id]) > 200:
        history[group_id] = history[group_id][-100:]

    save_config_history(history)

def is_feature_enabled(group_id: str, feature: str, global_config: dict = None) -> bool:
    """检查群功能是否启用"""
    config = get_group_config(group_id, global_config)

    # 处理带有 _enabled 后缀的功能名称
    feature_name = feature
    if feature_name.endswith("_enabled"):
        feature_name = feature_name[:-8]

    # 新结构。旧版本/网页同步可能只写入了部分 features 节点，同时保留
    # 根级镜像（例如 audit_enabled）。节点缺失时必须回退到镜像，否则
    # 入群申请会被静默当成“审核关闭”。
    features = config.get("features", {})
    if isinstance(features, dict) and feature_name in features:
        feature_config = features.get(feature_name)
        if isinstance(feature_config, dict) and "enabled" in feature_config:
            return bool(feature_config.get("enabled"))
        if isinstance(feature_config, bool):
            return bool(feature_config)

    # 旧结构兼容及部分 features 配置的根级镜像。
    root_value = config.get(f"{feature_name}_enabled", None)
    if root_value is not None:
        return bool(root_value)
    nested_mirror = {
        "blacklist": "blacklist_settings",
        "mute": "mute_settings",
        "kick": "kick_settings",
    }.get(feature_name)
    if nested_mirror:
        mirror = config.get(nested_mirror)
        if isinstance(mirror, dict) and "enabled" in mirror:
            return bool(mirror.get("enabled"))
    return False


# Older Windows-side helpers and the web compatibility view still read a
# handful of feature switches from the group-config root. Keep those mirrors
# in lockstep when a group command changes the canonical ``features`` map.
_FEATURE_ENABLED_ROOT_MIRRORS = {
    "audit": "audit_enabled",
    "welcome": "welcome_enabled",
    "recall": "recall_enabled",
    "stats": "stats_enabled",
    "reply": "reply_enabled",
    "player_reply": "player_reply_enabled",
    "entertainment": "entertainment_enabled",
}
_FEATURE_ENABLED_NESTED_MIRRORS = {
    "blacklist": "blacklist_settings",
    "mute": "mute_settings",
    "kick": "kick_settings",
}


def _sync_feature_enabled_mirrors(
    config: dict, feature_name: str, enabled: bool
) -> None:
    """Update legacy root mirrors used by the portal and old runtime helpers."""
    root_key = _FEATURE_ENABLED_ROOT_MIRRORS.get(feature_name)
    if root_key:
        config[root_key] = bool(enabled)
    nested_key = _FEATURE_ENABLED_NESTED_MIRRORS.get(feature_name)
    if nested_key:
        mirror = config.get(nested_key)
        if not isinstance(mirror, dict):
            mirror = {}
            config[nested_key] = mirror
        mirror["enabled"] = bool(enabled)


def set_feature_enabled(group_id: str, feature: str, enabled: bool, global_config: dict = None):
    """设置群功能启用状态"""
    group_id = _normalize_group_id(group_id)
    with _cache_lock:
        config = _read_group_config_unlocked(group_id)
        if not config:
            config = clone_global_config_to_group_config(group_id, global_config) if global_config else {
                "group_id": group_id,
                "enabled": True,
                "features": get_default_feature_config(),
                "created_at": int(time.time()),
            }
        feature_name = feature[:-8] if feature.endswith("_enabled") else feature
        if not isinstance(config.get("features"), dict):
            config["features"] = get_default_feature_config()
        config["features"].setdefault(feature_name, {})["enabled"] = enabled
        _sync_feature_enabled_mirrors(config, feature_name, enabled)
        config["updated_at"] = int(time.time())
        _save_group_config_unlocked(group_id, config)
    _notify_portal_config_change(group_id)

def list_all_groups() -> list:
    """列出所有有配置文件的群组"""
    config_dir = get_groups_config_dir()
    groups = []
    try:
        for file in config_dir.iterdir():
            if file.suffix != ".json":
                continue
            try:
                groups.append(_normalize_group_id(file.stem))
            except ValueError:
                continue
    except Exception as e:
        logger.error(f"列出群组配置失败: {e}")
    return groups

def remove_group_config(group_id: str) -> bool:
    """Delete a group configuration file and return whether cleanup succeeded."""
    try:
        group_id = _normalize_group_id(group_id)
        config_file = get_group_config_file(group_id)
        with _cache_lock:
            if config_file.exists():
                config_file.unlink()
            _config_cache.pop(group_id, None)
        from .message_cache import remove_group_message_cache
        if not remove_group_message_cache(group_id):
            logger.error(f"删除群组 {group_id} 的消息缓存失败")
            return False
        # A removed local file is also meaningful to the outbound sync
        # reconciler.  It will send the new authoritative group list and let
        # the portal mark the stale projection accordingly.
        _notify_portal_config_change(group_id)
        return True
    except Exception as e:
        logger.error(f"删除群组 {group_id} 配置失败: {e}")
        return False

def get_config_history(group_id: str, limit: int = 50) -> list:
    history = load_config_history()
    if group_id not in history:
        return []
    return history[group_id][-limit:]

def reset_group_config(group_id: str):
    """重置群配置为默认值"""
    group_id = _normalize_group_id(group_id)
    existing_config = load_group_config(group_id)
    config = {
        "group_id": group_id,
        "enabled": True,
        "features": get_default_feature_config(),
        "created_at": int(time.time()),
        "updated_at": int(time.time())
    }
    _preserve_power_status(existing_config, config)
    save_group_config(group_id, config)
    return config


def reset_game_configuration(group_id: str):
    """Restore game defaults while retaining the group's game/runtime data."""
    group_id = _normalize_group_id(group_id)
    existing = load_group_config(group_id)
    features = deepcopy(existing.get("features")) if isinstance(existing.get("features"), dict) else {}
    defaults = get_default_feature_config()
    default_stats = defaults.get("stats", {})
    stats = deepcopy(features.get("stats")) if isinstance(features.get("stats"), dict) else {}
    servers = deepcopy(features.get("game_servers")) if isinstance(features.get("game_servers"), dict) else {}
    profiles = servers.get("profiles", [])
    if not isinstance(profiles, list):
        profiles = []
    # Only replace game/sign-in policy. Database paths, profiles, reward
    # adapters and other group-specific connection settings are preserved.
    for key in (
        "enabled",
        "signin_mode",
        "schedule_mode",
        "signin_command",
        "bind_command",
        "signin_message",
        "bind_failure_message",
        "steam_reward_mode",
        "steam_reward_operation",
        "steam_reward_command",
        "steam_reward_failure_message",
        "database_sync",
        "signin_base_reward",
        "signin_reward_3_days",
        "signin_reward_7_days",
        "signin_reward_14_days",
        "signin_reward_30_days",
    ):
        if key in default_stats:
            stats[key] = deepcopy(default_stats[key])
    stats.update(
        {
            "enabled": True,
            "signin_mode": "steam",
            "steam_reward_mode": "websocket",
            "steam_reward_operation": "execute_game_command",
            "steam_reward_command": "#ChangeCurrencyBalance Normal {reward} {steam_id}",
        }
    )
    if not isinstance(stats.get("database_profiles"), list) or not stats.get("database_profiles"):
        stats["database_profiles"] = _default_database_profiles()
    stats["signin_profiles"] = _default_steam_signin_profiles()
    servers["profiles"] = profiles
    servers["enabled"] = bool(profiles)
    features["stats"] = stats
    features["game_commands"] = {
        "enabled": True,
        "execution_mode": "local",
        "success_message": "游戏指令执行成功：{message}",
        "failure_message": "游戏指令执行失败：{reason}",
        "commands": _default_game_commands(),
    }
    features["game_servers"] = servers
    if not existing:
        existing = {
            "group_id": group_id,
            "enabled": True,
            "created_at": int(time.time()),
        }
    existing["features"] = features
    existing["updated_at"] = int(time.time())
    save_group_config(group_id, existing)
    return existing

def clone_group_config(source_group_id: str, target_group_id: str):
    """克隆一个群的配置到另一个群（复制整个配置文件）"""
    source_group_id = _normalize_group_id(source_group_id)
    target_group_id = _normalize_group_id(target_group_id)
    source_config = load_group_config(source_group_id)
    if not source_config:
        logger.error(f"源群组 {source_group_id} 配置不存在")
        return False

    import copy
    existing_target_config = load_group_config(target_group_id)
    # Keep the destination as the base so local-only runtime data (message
    # caches, audit queues, invite/sign-in history, and image paths) is not
    # reassigned to the source group.  Replace only keys exposed by the Linux
    # portal's safe outbound projection.
    target_config = (
        copy.deepcopy(existing_target_config)
        if isinstance(existing_target_config, dict) and existing_target_config
        else {
            "group_id": target_group_id,
            "enabled": True,
            "features": get_default_feature_config(),
            "created_at": int(time.time()),
        }
    )
    for key in _PORTAL_SYNC_ROOT_KEYS:
        if key in source_config:
            target_config[key] = copy.deepcopy(source_config[key])
    target_config["group_id"] = target_group_id
    target_config["updated_at"] = int(time.time())
    _preserve_power_status(existing_target_config, target_config)
    # Authorization and list membership are target-group state, not settings
    # that may be copied from another group.  The auth manager remains the
    # source of truth, but retaining these target values also keeps status
    # displays from contradicting the actual outbound gate.
    for field_name, default in (
        ("authorized", False),
        ("auth_info", {}),
        ("is_whitelist", False),
        ("is_blacklist", False),
    ):
        target_config[field_name] = copy.deepcopy(
            existing_target_config.get(field_name, default)
        )
    save_group_config(target_group_id, target_config)
    return True

def clone_global_config_to_group(group_id: str, global_config: dict = None):
    """从全局配置克隆到指定群组（只克隆配置，保留分群数据）"""
    group_id = _normalize_group_id(group_id)
    existing_group_config = load_group_config(group_id)
    existing_scum = (
        existing_group_config.get("features", {}).get("scum", {})
        if isinstance(existing_group_config, dict)
        and isinstance(existing_group_config.get("features"), dict)
        and isinstance(existing_group_config.get("features", {}).get("scum"), dict)
        else {}
    )
    # Build the new policy first.  The existing group config is re-read under
    # the write lock below so concurrent sign-in, invite, and schedule updates
    # cannot be overwritten by a global settings sync.
    config = {
        "group_id": group_id,
        "enabled": True,
        "features": get_default_feature_config(),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "authorized": False,
        "auth_info": {},
        "is_whitelist": False,
        "is_blacklist": False
    }

    # 检查授权状态（如果启用了授权系统）
    if global_config:
        from ..auth.manager import is_group_authorized, get_group_auth_info
        config["authorized"] = is_group_authorized(group_id)
        config["auth_info"] = get_group_auth_info(group_id)

    # 检查白名单和黑名单
    if global_config:
        # 检查全局白名单
        whitelist_groups = {str(item) for item in global_config.get("basic", {}).get("whitelist_groups", [])}
        if group_id in whitelist_groups:
            config["is_whitelist"] = True
        
        # 检查黑名单
        blacklist_groups = {str(item) for item in global_config.get("basic", {}).get("blacklist_groups", [])}
        if group_id in blacklist_groups:
            config["is_blacklist"] = True

    if global_config:
        qqadmin_config = global_config.get("qqadmin", {})

        # 审计配置
        audit_config = qqadmin_config.get("audit", {})
        config["features"]["audit"]["enabled"] = audit_config.get("enable_audit", False)
        config["features"]["audit"]["approval_mode"] = audit_config.get("default_approval_mode", "math")
        config["features"]["audit"]["verification_timeout"] = audit_config.get("default_verification_timeout", 300)
        config["features"]["audit"]["max_attempts"] = audit_config.get("max_verify_attempts", 3)
        config["features"]["audit"]["kick_on_verification_failure"] = audit_config.get("kick_on_verification_failure", False)
        config["features"]["audit"]["admin_bypass"] = audit_config.get("admin_bypass_verify", True)
        config["features"]["audit"]["enable_invite_audit"] = audit_config.get("enable_invite_audit", True)
        
        # 直接写入分群配置根目录（供 get_group_data 读取）
        config["audit_enabled"] = config["features"]["audit"]["enabled"]
        config["approval_mode"] = config["features"]["audit"]["approval_mode"]
        config["verification_timeout"] = config["features"]["audit"]["verification_timeout"]
        config["max_attempts"] = config["features"]["audit"]["max_attempts"]
        config["kick_on_verification_failure"] = config["features"]["audit"]["kick_on_verification_failure"]
        config["admin_bypass"] = config["features"]["audit"]["admin_bypass"]

        # 黑名单配置
        blacklist_config = qqadmin_config.get("blacklist", {})
        config["features"]["blacklist"]["enabled"] = blacklist_config.get("enable_blacklist", False)
        config["features"]["blacklist"]["auto_kick"] = blacklist_config.get("auto_kick_blacklisted", True)
        config["features"]["blacklist"]["auto_blacklist_on_leave"] = blacklist_config.get("auto_blacklist_on_leave", False)
        config["features"]["blacklist"]["auto_blacklist_on_verify_fail"] = blacklist_config.get("auto_blacklist_on_verify_fail", False)
        
        # 直接写入分群配置根目录
        config["blacklist_settings"] = {
            "enabled": config["features"]["blacklist"]["enabled"],
            "auto_kick": config["features"]["blacklist"]["auto_kick"],
            "auto_blacklist_on_leave": config["features"]["blacklist"]["auto_blacklist_on_leave"],
            "auto_blacklist_on_verify_fail": config["features"]["blacklist"]["auto_blacklist_on_verify_fail"]
        }

        # 欢迎配置
        welcome_config = qqadmin_config.get("welcome", {})
        config["features"]["welcome"]["enabled"] = welcome_config.get("enable_welcome", False)
        config["features"]["welcome"]["auto_welcome"] = welcome_config.get("enable_auto_welcome", False)
        config["features"]["welcome"]["delay"] = welcome_config.get("default_welcome_delay", 0)
        config["features"]["welcome"]["message"] = welcome_config.get("default_welcome_message", "欢迎 {user_name} 加入本群！")
        config["features"]["welcome"]["farewell_enabled"] = welcome_config.get("enable_farewell", False)
        config["features"]["welcome"]["farewell_message"] = welcome_config.get("default_farewell_message", "{user_name} 离开了本群")
        config["features"]["welcome"]["keywords"] = welcome_config.get("welcome_keywords", ["欢迎", "welcome", "入群"])
        config["features"]["welcome"]["at_new_member"] = welcome_config.get("at_new_member", True)
        config["features"]["welcome"]["auto_reply_enabled"] = welcome_config.get("auto_reply_enabled", True)
        # 直接写入分群配置根目录
        config["welcome_enabled"] = config["features"]["welcome"]["enabled"]
        config["welcome_message"] = config["features"]["welcome"]["message"]
        # Keep the canonical keys used by qqadmin.welcome in sync with the
        # legacy ``welcome_*`` aliases written by older plugin versions.
        config["farewell_enabled"] = config["features"]["welcome"]["farewell_enabled"]
        config["farewell_message"] = config["features"]["welcome"]["farewell_message"]
        config["welcome_farewell_enabled"] = config["features"]["welcome"]["farewell_enabled"]
        config["welcome_farewell_message"] = config["features"]["welcome"]["farewell_message"]
        config["welcome_keywords"] = config["features"]["welcome"]["keywords"]
        config["at_new_member"] = welcome_config.get("at_new_member", True)
        config["welcome_auto_reply"] = config["features"]["welcome"]["auto_reply_enabled"]
        config["welcome_auto_reply_enabled"] = config["features"]["welcome"]["auto_reply_enabled"]
        config["welcome_auto_welcome"] = config["features"]["welcome"]["auto_welcome"]
        config["welcome_delay"] = config["features"]["welcome"]["delay"]

        # 撤回配置
        recall_config = qqadmin_config.get("recall", {})
        config["features"]["recall"]["enabled"] = recall_config.get("enable_recall", False)
        config["features"]["recall"]["self_recall"] = recall_config.get("enable_self_recall", True)
        config["features"]["recall"]["enable_admin_recall"] = recall_config.get("enable_admin_recall", True)
        config["features"]["recall"]["member_scope"] = (
            "all" if config["features"]["recall"]["enable_admin_recall"] else "members"
        )
        config["features"]["recall"]["enable_sensitive_detection"] = recall_config.get("enable_sensitive_detection", True)
        config["features"]["recall"]["kick_on_severe_violation"] = recall_config.get("kick_on_severe_violation", True)
        config["features"]["recall"]["sensitive_words"] = recall_config.get("sensitive_words", [])
        config["features"]["recall"]["time_limit"] = recall_config.get("default_recall_time", 60)
        config["features"]["recall"]["mode"] = recall_config.get("default_recall_mode", "keyword")
        config["features"]["recall"]["notification"] = recall_config.get("recall_notification", False)
        config["features"]["recall"]["notification_message"] = recall_config.get("recall_notification_message", "您的消息包含违规内容，已被撤回")
        
        # 直接写入分群配置根目录
        config["recall_enabled"] = config["features"]["recall"]["enabled"]
        config["self_recall_enabled"] = config["features"]["recall"]["self_recall"]
        config["self_recall_time"] = recall_config.get("default_recall_time", 30)
        config["recall_time"] = config["features"]["recall"]["time_limit"]
        config["recall_mode"] = config["features"]["recall"]["mode"]
        config["recall_keywords"] = recall_config.get("recall_keywords", [])
        config["recall_regex"] = recall_config.get("recall_regex_patterns", [])
        config["recall_member_scope"] = config["features"]["recall"]["member_scope"]
        config["enable_admin_recall"] = config["features"]["recall"]["enable_admin_recall"]
        config["recall_whitelist_mode"] = False
        config["recall_whitelist"] = []

        # Message cache settings are policy only; chat history is stored separately.
        message_cache_config = qqadmin_config.get("message_cache", {})
        config["features"]["message_cache"]["enabled"] = message_cache_config.get("enable_message_cache", True)
        config["features"]["message_cache"]["cache_raw_message"] = message_cache_config.get("cache_raw_message", True)
        config["features"]["message_cache"]["retention_days"] = message_cache_config.get("retention_days", 0)
        config["features"]["message_cache"]["max_messages_per_user"] = message_cache_config.get("max_messages_per_user", 0)
        config["features"]["message_cache"]["archive_images"] = message_cache_config.get("archive_images", True)
        config["features"]["message_cache"]["archive_image_max_mb"] = message_cache_config.get("archive_image_max_mb", 20)

        # 禁言配置
        mute_config = qqadmin_config.get("mute", {})
        config["features"]["mute"]["enabled"] = mute_config.get("enable_mute", True)
        config["features"]["mute"]["level"] = mute_config.get("default_mute_level", 5)
        config["features"]["mute"]["duration"] = mute_config.get("default_mute_duration", 300)
        
        # 直接写入分群配置根目录
        config["mute_settings"] = {
            "enabled": config["features"]["mute"]["enabled"],
            "level": config["features"]["mute"]["level"],
            "duration": config["features"]["mute"]["duration"]
        }

        # 踢出配置
        kick_config = qqadmin_config.get("kick", {})
        config["features"]["kick"]["enabled"] = kick_config.get("enable_kick", False)
        config["features"]["kick"]["auto_blacklist"] = kick_config.get("auto_blacklist_on_kick", False)
        config["features"]["kick"]["limit"] = kick_config.get("default_kick_limit", 3)
        
        # 直接写入分群配置根目录
        config["kick_settings"] = {
            "enabled": config["features"]["kick"]["enabled"],
            "auto_blacklist": config["features"]["kick"]["auto_blacklist"],
            "limit": config["features"]["kick"]["limit"]
        }

        # 统计配置
        stats_config = qqadmin_config.get("stats", {})
        config["features"]["stats"]["enabled"] = stats_config.get("enable_stats", True)
        config["features"]["stats"]["update_interval"] = stats_config.get("update_interval", 300)
        config["features"]["stats"]["auto_update_on_join"] = stats_config.get("auto_update_on_join", True)
        config["features"]["stats"]["abnormal_invite_threshold"] = stats_config.get("abnormal_invite_threshold", 5)
        config["features"]["stats"]["signin_base_reward"] = stats_config.get("signin_base_reward", 10)
        config["features"]["stats"]["signin_reward_3_days"] = stats_config.get("signin_reward_3_days", 30)
        config["features"]["stats"]["signin_reward_7_days"] = stats_config.get("signin_reward_7_days", 100)
        config["features"]["stats"]["signin_reward_14_days"] = stats_config.get("signin_reward_14_days", 200)
        config["features"]["stats"]["signin_reward_30_days"] = stats_config.get("signin_reward_30_days", 500)
        _copy_global_stats_policy(stats_config, config["features"]["stats"])
        config["stats_enabled"] = config["features"]["stats"]["enabled"]

        # SCUM commands are stored at the plugin root rather than qqadmin.
        # Copy them into each group's feature map when an administrator
        # explicitly clones/syncs global defaults.
        scum_config = global_config.get("scum", {})
        if not isinstance(scum_config, dict):
            scum_config = {}
        config["features"]["scum"] = {
            "enabled": scum_config.get("enable_query", True),
            "allow_server_binding": existing_scum.get("allow_server_binding", True),
            "allow_online_query": existing_scum.get("allow_online_query", True),
            "permission_mode": scum_config.get("permission_mode", "read_only"),
            "keyword_command": scum_config.get("keyword_command", "查询"),
            "online_command": scum_config.get("online_command", "查询在线"),
        }

        # 回复配置
        reply_config = qqadmin_config.get("reply", {})
        config["features"]["reply"]["enabled"] = reply_config.get("enable_reply", False)
        config["features"]["reply"]["cooldown"] = reply_config.get("default_reply_cooldown", 0)
        config["features"]["reply"]["cache_reply_images"] = reply_config.get("cache_reply_images", True)
        config["features"]["reply"]["reply_image_max_mb"] = reply_config.get("reply_image_max_mb", 20)
        config["features"]["reply"]["reply_image_download_concurrency"] = reply_config.get("reply_image_download_concurrency", 4)
        config["features"]["reply"]["reply_image_retry_interval"] = reply_config.get("reply_image_retry_interval", 300)
        config["reply_enabled"] = config["features"]["reply"]["enabled"]
        config["reply_cooldown"] = config["features"]["reply"]["cooldown"]

        lottery_config = qqadmin_config.get("lottery", {})
        if not isinstance(lottery_config, dict):
            lottery_config = {}
        config["features"].setdefault("lottery", {})
        config["features"]["lottery"].update({
            "enabled": lottery_config.get("enable_lottery", lottery_config.get("enabled", False)),
            "keyword": lottery_config.get("keyword", "抽奖"),
            "max_draws_per_user": lottery_config.get("max_draws_per_user", 0),
            "cooldown": lottery_config.get("cooldown", 0),
            "success_message": lottery_config.get(
                "success_message", "🎉 {name} 抽中了「{prize}」！剩余 {remaining}"
            ),
            "empty_message": lottery_config.get(
                "empty_message", "{name} 很遗憾没有抽中，再试一次吧。"
            ),
        })

        # 娱乐配置：游戏本身是 Windows 本地执行的轻量互动，不写入
        # 任何 Steam/数据库状态；这里只同步分群策略和命令名称。
        entertainment_config = qqadmin_config.get("entertainment", {})
        if not isinstance(entertainment_config, dict):
            entertainment_config = {}
        like_command = entertainment_config.get("like_command", "赞我")
        if str(like_command or "").strip() == "点赞":
            like_command = "赞我"
        config["features"].setdefault("entertainment", {})
        config["features"]["entertainment"].update({
            "enabled": entertainment_config.get("enable_entertainment", entertainment_config.get("enabled", False)),
            "time_enabled": entertainment_config.get("time_enabled", True),
            "dice_enabled": entertainment_config.get("dice_enabled", True),
            "rps_enabled": entertainment_config.get("rps_enabled", True),
            "random_enabled": entertainment_config.get("random_enabled", True),
            "like_enabled": entertainment_config.get("like_enabled", True),
            "music_enabled": entertainment_config.get("music_enabled", True),
            "video_enabled": entertainment_config.get("video_enabled", True),
            "time_command": entertainment_config.get("time_command", "当前时间"),
            "dice_command": entertainment_config.get("dice_command", "骰子"),
            "rps_command": entertainment_config.get("rps_command", "猜拳"),
            "random_command": entertainment_config.get("random_command", "随机"),
            "like_command": like_command,
            "music_command": entertainment_config.get("music_command", "点歌"),
            "music_selection_timeout": entertainment_config.get("music_selection_timeout", 600),
            "music_sources": entertainment_config.get("music_sources", ["qq", "163"]),
            "music_default_source": entertainment_config.get("music_default_source", "qq"),
            "video_command": entertainment_config.get("video_command", "搜索视频"),
            "like_daily_limit": entertainment_config.get("like_daily_limit", 20),
            "cooldown": entertainment_config.get("cooldown", 0),
            "max_sides": entertainment_config.get("max_sides", 100),
            "max_options": entertainment_config.get("max_options", 20),
        })
        config["entertainment_enabled"] = config["features"]["entertainment"]["enabled"]

        # 用户专属回复配置
        player_reply_config = qqadmin_config.get("player_reply", {})
        config["features"]["player_reply"]["enabled"] = player_reply_config.get("enable_player_reply", False)
        config["features"]["player_reply"]["default_at_user"] = player_reply_config.get("default_at_user", True)
        config["player_reply_enabled"] = config["features"]["player_reply"]["enabled"]

        # 文转图配置
        text_to_image_config = qqadmin_config.get("text_to_image", {})
        config["features"]["text_to_image"]["enabled"] = text_to_image_config.get("enable_text_to_image", False)
        config["features"]["text_to_image"]["font_size"] = text_to_image_config.get("font_size", 16)
        config["features"]["text_to_image"]["font_color"] = text_to_image_config.get("font_color", "#333333")
        config["features"]["text_to_image"]["bg_color"] = text_to_image_config.get("bg_color", "#ffffff")
        config["features"]["text_to_image"]["line_spacing"] = text_to_image_config.get("line_spacing", 4)
        config["features"]["text_to_image"]["padding"] = text_to_image_config.get("padding", 20)
        config["features"]["text_to_image"]["modify_permission"] = text_to_image_config.get("modify_permission", "admin")

        # 关键词过滤配置
        keyword_filter_config = qqadmin_config.get("keyword_filter", {})
        config["features"]["keyword_filter"] = {
            "enabled": keyword_filter_config.get("enable_keyword_filter", False),
            "filter_keywords": keyword_filter_config.get("filter_keywords", []),
            "filter_regex_patterns": keyword_filter_config.get("filter_regex_patterns", []),
            "filter_action": keyword_filter_config.get("filter_action", "warn"),
            "filter_mute_duration": keyword_filter_config.get("filter_mute_duration", 300),
            "warn_message": keyword_filter_config.get(
                "warn_message", "您的消息包含敏感内容，请遵守群规！"
            ),
            "enable_log": keyword_filter_config.get("enable_log", True),
            "admin_bypass_filter": keyword_filter_config.get("admin_bypass_filter", True),
        }

        # 定时消息和分群管理员配置
        timed_message_config = qqadmin_config.get("timed_message", {})
        config["features"]["timed_message"] = {
            "enabled": timed_message_config.get("enable_timed_message", False)
        }
        sub_admin_config = qqadmin_config.get("sub_admin", {})
        config["features"]["sub_admin"] = {
            "enabled": sub_admin_config.get("enable_sub_admin", False)
        }

    with _cache_lock:
        existing_config = _read_group_config_unlocked(group_id)
        if isinstance(existing_config, dict):
            config["created_at"] = existing_config.get(
                "created_at", config["created_at"]
            )
        _preserve_group_local_runtime_data(existing_config, config)
        # Power is runtime state, not a global default.  Preserve only a valid
        # explicit value so a malformed legacy config cannot open the send gate.
        _preserve_power_status(existing_config, config)
        _save_group_config_unlocked(group_id, config)
    _notify_portal_config_change(group_id)
    return config

def clear_group_data(group_id: str) -> dict:
    """清除分群所有数据，恢复到默认配置"""
    group_id = _normalize_group_id(group_id)
    existing_config = load_group_config(group_id)
    # 创建默认配置
    config = {
        "group_id": group_id,
        "enabled": True,
        "features": get_default_feature_config(),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "authorized": False,
        "auth_info": {},
        "is_whitelist": False,
        "is_blacklist": False,
        # 清空所有数据
        "replies": {},
        "player_reply": {},
        "blacklist": [],
        "whitelist": [],
        "audit_pending": [],
        "audit_history": [],
        "audit_challenges": {},
        "welcome_history": [],
        "operation_logs": [],
        "invite_stats": {},
        "kick_records": [],
        "sub_admins": [],
        "cooldown_cache": {},
        # 默认配置值
        "audit_enabled": False,
        "approval_mode": "math",
        "verification_timeout": 300,
        "max_attempts": 3,
        "admin_bypass": True,
        "blacklist_settings": {"enabled": False, "auto_kick": True},
        "welcome_enabled": False,
        "welcome_message": "欢迎 {user_name} 加入本群！",
        "farewell_enabled": False,
        "farewell_message": "{user_name} 离开了本群",
        "welcome_farewell_enabled": False,
        "welcome_farewell_message": "{user_name} 离开了本群",
        "welcome_keywords": ["欢迎", "welcome", "入群"],
        "at_new_member": True,
        "welcome_auto_reply": True,
        "welcome_auto_reply_enabled": True,
        "welcome_auto_welcome": False,
        "welcome_delay": 0,
        "recall_enabled": False,
        "self_recall_enabled": True,
        "self_recall_time": 30,
        "recall_time": 60,
        "recall_mode": "keyword",
        "recall_keywords": [],
        "recall_regex": [],
        "recall_whitelist_mode": False,
        "recall_whitelist": [],
        "mute_settings": {"enabled": True, "level": 5, "duration": 300},
        "kick_settings": {"enabled": False, "auto_blacklist": False, "limit": 3},
        "stats_enabled": True,
        "reply_enabled": False,
        "reply_cooldown": 0,
        "player_reply_enabled": False,
        "entertainment_enabled": False,
    }
    _preserve_power_status(existing_config, config)
    
    save_group_config(group_id, config)
    try:
        from .message_cache import remove_group_message_cache
        if not remove_group_message_cache(group_id):
            logger.warning(f"清除群组 {group_id} 数据时未能删除消息缓存")
    except Exception as e:
        logger.error(f"清除群组 {group_id} 的消息缓存失败: {e}")
    return config

def clone_global_config_to_group_config(group_id: str, global_config: dict = None) -> dict:
    """从全局配置克隆到指定群组（返回配置对象）"""
    existing_group_config = load_group_config(group_id)
    existing_scum = (
        existing_group_config.get("features", {}).get("scum", {})
        if isinstance(existing_group_config, dict)
        and isinstance(existing_group_config.get("features"), dict)
        and isinstance(existing_group_config.get("features", {}).get("scum"), dict)
        else {}
    )
    config = {
        "group_id": group_id,
        "enabled": True,
        "features": get_default_feature_config(),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "authorized": False,
        "auth_info": {},
        "is_whitelist": False,
        "is_blacklist": False
    }

    # 检查授权状态（如果启用了授权系统）
    if global_config:
        from ..auth.manager import is_group_authorized, get_group_auth_info
        config["authorized"] = is_group_authorized(group_id)
        config["auth_info"] = get_group_auth_info(group_id)

    # 检查白名单和黑名单
    if global_config:
        # 检查全局白名单
        whitelist_groups = {str(item) for item in global_config.get("basic", {}).get("whitelist_groups", [])}
        if str(group_id) in whitelist_groups:
            config["is_whitelist"] = True
        
        # 检查黑名单
        blacklist_groups = {str(item) for item in global_config.get("basic", {}).get("blacklist_groups", [])}
        if str(group_id) in blacklist_groups:
            config["is_blacklist"] = True

    if global_config:
        qqadmin_config = global_config.get("qqadmin", {})

        # 审计配置
        audit_config = qqadmin_config.get("audit", {})
        config["features"]["audit"]["enabled"] = audit_config.get("enable_audit", False)
        config["features"]["audit"]["approval_mode"] = audit_config.get("default_approval_mode", "math")
        config["features"]["audit"]["verification_timeout"] = audit_config.get("default_verification_timeout", 300)
        config["features"]["audit"]["max_attempts"] = audit_config.get("max_verify_attempts", 3)
        config["features"]["audit"]["kick_on_verification_failure"] = audit_config.get("kick_on_verification_failure", False)
        config["features"]["audit"]["admin_bypass"] = audit_config.get("admin_bypass_verify", True)
        config["features"]["audit"]["enable_invite_audit"] = audit_config.get("enable_invite_audit", True)
        # 直接写入分群配置根目录（供 get_group_data 读取）
        config["audit_enabled"] = config["features"]["audit"]["enabled"]
        config["approval_mode"] = config["features"]["audit"]["approval_mode"]
        config["verification_timeout"] = config["features"]["audit"]["verification_timeout"]
        config["max_attempts"] = config["features"]["audit"]["max_attempts"]
        config["kick_on_verification_failure"] = config["features"]["audit"]["kick_on_verification_failure"]
        config["admin_bypass"] = config["features"]["audit"]["admin_bypass"]

        # 黑名单配置
        blacklist_config = qqadmin_config.get("blacklist", {})
        config["features"]["blacklist"]["enabled"] = blacklist_config.get("enable_blacklist", False)
        config["features"]["blacklist"]["auto_kick"] = blacklist_config.get("auto_kick_blacklisted", True)
        config["features"]["blacklist"]["auto_blacklist_on_leave"] = blacklist_config.get("auto_blacklist_on_leave", False)
        config["features"]["blacklist"]["auto_blacklist_on_verify_fail"] = blacklist_config.get("auto_blacklist_on_verify_fail", False)
        
        # 直接写入分群配置根目录
        config["blacklist_settings"] = {
            "enabled": config["features"]["blacklist"]["enabled"],
            "auto_kick": config["features"]["blacklist"]["auto_kick"],
            "auto_blacklist_on_leave": config["features"]["blacklist"]["auto_blacklist_on_leave"],
            "auto_blacklist_on_verify_fail": config["features"]["blacklist"]["auto_blacklist_on_verify_fail"]
        }

        # 欢迎配置
        welcome_config = qqadmin_config.get("welcome", {})
        config["features"]["welcome"]["enabled"] = welcome_config.get("enable_welcome", False)
        config["features"]["welcome"]["auto_welcome"] = welcome_config.get("enable_auto_welcome", False)
        config["features"]["welcome"]["delay"] = welcome_config.get("default_welcome_delay", 0)
        config["features"]["welcome"]["message"] = welcome_config.get("default_welcome_message", "欢迎 {user_name} 加入本群！")
        config["features"]["welcome"]["farewell_enabled"] = welcome_config.get("enable_farewell", False)
        config["features"]["welcome"]["farewell_message"] = welcome_config.get("default_farewell_message", "{user_name} 离开了本群")
        config["features"]["welcome"]["keywords"] = welcome_config.get("welcome_keywords", ["欢迎", "welcome", "入群"])
        config["features"]["welcome"]["at_new_member"] = welcome_config.get("at_new_member", True)
        config["features"]["welcome"]["auto_reply_enabled"] = welcome_config.get("auto_reply_enabled", True)

        # The older clone helper above writes root-level compatibility keys;
        # keep this public helper equivalent so both code paths behave alike.
        config["welcome_enabled"] = config["features"]["welcome"]["enabled"]
        config["welcome_message"] = config["features"]["welcome"]["message"]
        config["farewell_enabled"] = config["features"]["welcome"]["farewell_enabled"]
        config["farewell_message"] = config["features"]["welcome"]["farewell_message"]
        config["welcome_farewell_enabled"] = config["features"]["welcome"]["farewell_enabled"]
        config["welcome_farewell_message"] = config["features"]["welcome"]["farewell_message"]
        config["welcome_keywords"] = config["features"]["welcome"]["keywords"]
        config["at_new_member"] = welcome_config.get("at_new_member", True)
        config["welcome_auto_reply"] = config["features"]["welcome"]["auto_reply_enabled"]
        config["welcome_auto_reply_enabled"] = config["features"]["welcome"]["auto_reply_enabled"]
        config["welcome_auto_welcome"] = config["features"]["welcome"]["auto_welcome"]
        config["welcome_delay"] = config["features"]["welcome"]["delay"]

        # 撤回配置
        recall_config = qqadmin_config.get("recall", {})
        config["features"]["recall"]["enabled"] = recall_config.get("enable_recall", False)
        config["features"]["recall"]["self_recall"] = recall_config.get("enable_self_recall", True)
        config["features"]["recall"]["self_recall_enabled"] = recall_config.get("enable_self_recall", True)
        config["features"]["recall"]["enable_admin_recall"] = recall_config.get("enable_admin_recall", True)
        config["features"]["recall"]["member_scope"] = (
            "all" if config["features"]["recall"]["enable_admin_recall"] else "members"
        )
        config["features"]["recall"]["enable_sensitive_detection"] = recall_config.get("enable_sensitive_detection", True)
        config["features"]["recall"]["kick_on_severe_violation"] = recall_config.get("kick_on_severe_violation", True)
        config["features"]["recall"]["sensitive_words"] = recall_config.get("sensitive_words", [])
        config["features"]["recall"]["time_limit"] = recall_config.get("default_recall_time", 60)
        config["features"]["recall"]["mode"] = recall_config.get("default_recall_mode", "keyword")
        config["features"]["recall"]["notification"] = recall_config.get("recall_notification", False)
        config["features"]["recall"]["notification_message"] = recall_config.get("recall_notification_message", "您的消息包含违规内容，已被撤回")
        
        # 直接写入分群配置根目录（供 get_group_data 读取）
        config["recall_enabled"] = config["features"]["recall"]["enabled"]
        config["self_recall_enabled"] = config["features"]["recall"]["self_recall"]
        config["self_recall_time"] = recall_config.get("default_recall_time", 30)
        config["recall_time"] = config["features"]["recall"]["time_limit"]
        config["recall_mode"] = config["features"]["recall"]["mode"]
        config["recall_keywords"] = recall_config.get("recall_keywords", [])
        config["recall_regex"] = recall_config.get("recall_regex_patterns", [])
        config["recall_member_scope"] = config["features"]["recall"]["member_scope"]
        config["enable_admin_recall"] = config["features"]["recall"]["enable_admin_recall"]

        # Message cache settings are policy only; chat history is stored separately.
        message_cache_config = qqadmin_config.get("message_cache", {})
        config["features"]["message_cache"]["enabled"] = message_cache_config.get("enable_message_cache", True)
        config["features"]["message_cache"]["cache_raw_message"] = message_cache_config.get("cache_raw_message", True)
        config["features"]["message_cache"]["retention_days"] = message_cache_config.get("retention_days", 0)
        config["features"]["message_cache"]["max_messages_per_user"] = message_cache_config.get("max_messages_per_user", 0)
        config["features"]["message_cache"]["archive_images"] = message_cache_config.get("archive_images", True)
        config["features"]["message_cache"]["archive_image_max_mb"] = message_cache_config.get("archive_image_max_mb", 20)

        # 禁言配置
        mute_config = qqadmin_config.get("mute", {})
        config["features"]["mute"]["enabled"] = mute_config.get("enable_mute", True)
        config["features"]["mute"]["level"] = mute_config.get("default_mute_level", 5)
        config["features"]["mute"]["duration"] = mute_config.get("default_mute_duration", 300)

        # 踢出配置
        kick_config = qqadmin_config.get("kick", {})
        config["features"]["kick"]["enabled"] = kick_config.get("enable_kick", False)
        config["features"]["kick"]["auto_blacklist"] = kick_config.get("auto_blacklist_on_kick", False)
        config["features"]["kick"]["limit"] = kick_config.get("default_kick_limit", 3)

        # 统计配置
        stats_config = qqadmin_config.get("stats", {})
        config["features"]["stats"]["enabled"] = stats_config.get("enable_stats", True)
        config["features"]["stats"]["update_interval"] = stats_config.get("update_interval", 300)
        config["features"]["stats"]["auto_update_on_join"] = stats_config.get("auto_update_on_join", True)
        config["features"]["stats"]["abnormal_invite_threshold"] = stats_config.get("abnormal_invite_threshold", 5)
        config["features"]["stats"]["signin_base_reward"] = stats_config.get("signin_base_reward", 10)
        config["features"]["stats"]["signin_reward_3_days"] = stats_config.get("signin_reward_3_days", 30)
        config["features"]["stats"]["signin_reward_7_days"] = stats_config.get("signin_reward_7_days", 100)
        config["features"]["stats"]["signin_reward_14_days"] = stats_config.get("signin_reward_14_days", 200)
        config["features"]["stats"]["signin_reward_30_days"] = stats_config.get("signin_reward_30_days", 500)
        _copy_global_stats_policy(stats_config, config["features"]["stats"])

        scum_config = global_config.get("scum", {})
        if not isinstance(scum_config, dict):
            scum_config = {}
        config["features"]["scum"] = {
            "enabled": scum_config.get("enable_query", True),
            "allow_server_binding": existing_scum.get("allow_server_binding", True),
            "allow_online_query": existing_scum.get("allow_online_query", True),
            "permission_mode": scum_config.get("permission_mode", "read_only"),
            "keyword_command": scum_config.get("keyword_command", "查询"),
            "online_command": scum_config.get("online_command", "查询在线"),
        }

        # 回复配置
        reply_config = qqadmin_config.get("reply", {})
        config["features"]["reply"]["enabled"] = reply_config.get("enable_reply", False)
        config["features"]["reply"]["cooldown"] = reply_config.get("default_reply_cooldown", 0)
        config["features"]["reply"]["cache_reply_images"] = reply_config.get("cache_reply_images", True)
        config["features"]["reply"]["reply_image_max_mb"] = reply_config.get("reply_image_max_mb", 20)
        config["features"]["reply"]["reply_image_download_concurrency"] = reply_config.get("reply_image_download_concurrency", 4)
        config["features"]["reply"]["reply_image_retry_interval"] = reply_config.get("reply_image_retry_interval", 300)

        # 抽奖配置：定义和开关复制到分群，抽奖记录/库存运行态仍只在
        # Windows 本地维护。这样新群首次加载时也能直接使用全局默认值。
        lottery_config = qqadmin_config.get("lottery", {})
        if not isinstance(lottery_config, dict):
            lottery_config = {}
        config["features"].setdefault("lottery", {})
        config["features"]["lottery"].update({
            "enabled": lottery_config.get("enable_lottery", lottery_config.get("enabled", False)),
            "keyword": lottery_config.get("keyword", "抽奖"),
            "max_draws_per_user": lottery_config.get("max_draws_per_user", 0),
            "cooldown": lottery_config.get("cooldown", 0),
            "success_message": lottery_config.get(
                "success_message", "🎉 {name} 抽中了「{prize}」！剩余 {remaining}"
            ),
            "empty_message": lottery_config.get(
                "empty_message", "{name} 很遗憾没有抽中，再试一次吧。"
            ),
        })

        entertainment_config = qqadmin_config.get("entertainment", {})
        if not isinstance(entertainment_config, dict):
            entertainment_config = {}
        like_command = entertainment_config.get("like_command", "赞我")
        if str(like_command or "").strip() == "点赞":
            like_command = "赞我"
        config["features"].setdefault("entertainment", {})
        config["features"]["entertainment"].update({
            "enabled": entertainment_config.get("enable_entertainment", entertainment_config.get("enabled", False)),
            "time_enabled": entertainment_config.get("time_enabled", True),
            "dice_enabled": entertainment_config.get("dice_enabled", True),
            "rps_enabled": entertainment_config.get("rps_enabled", True),
            "random_enabled": entertainment_config.get("random_enabled", True),
            "like_enabled": entertainment_config.get("like_enabled", True),
            "music_enabled": entertainment_config.get("music_enabled", True),
            "video_enabled": entertainment_config.get("video_enabled", True),
            "time_command": entertainment_config.get("time_command", "当前时间"),
            "dice_command": entertainment_config.get("dice_command", "骰子"),
            "rps_command": entertainment_config.get("rps_command", "猜拳"),
            "random_command": entertainment_config.get("random_command", "随机"),
            "like_command": like_command,
            "music_command": entertainment_config.get("music_command", "点歌"),
            "music_selection_timeout": entertainment_config.get("music_selection_timeout", 600),
            "music_sources": entertainment_config.get("music_sources", ["qq", "163"]),
            "music_default_source": entertainment_config.get("music_default_source", "qq"),
            "video_command": entertainment_config.get("video_command", "搜索视频"),
            "like_daily_limit": entertainment_config.get("like_daily_limit", 20),
            "cooldown": entertainment_config.get("cooldown", 0),
            "max_sides": entertainment_config.get("max_sides", 100),
            "max_options": entertainment_config.get("max_options", 20),
        })
        config["entertainment_enabled"] = config["features"]["entertainment"]["enabled"]

        # 用户专属回复配置
        player_reply_config = qqadmin_config.get("player_reply", {})
        config["features"]["player_reply"]["enabled"] = player_reply_config.get("enable_player_reply", False)
        config["features"]["player_reply"]["default_at_user"] = player_reply_config.get("default_at_user", True)

        # 关键词过滤配置
        keyword_filter_config = qqadmin_config.get("keyword_filter", {})
        config["features"]["keyword_filter"] = {
            "enabled": keyword_filter_config.get("enable_keyword_filter", False),
            "filter_keywords": keyword_filter_config.get("filter_keywords", []),
            "filter_regex_patterns": keyword_filter_config.get("filter_regex_patterns", []),
            "filter_action": keyword_filter_config.get("filter_action", "warn"),
            "filter_mute_duration": keyword_filter_config.get("filter_mute_duration", 300),
            "warn_message": keyword_filter_config.get("warn_message", "您的消息包含敏感内容，请遵守群规！"),
            "enable_log": keyword_filter_config.get("enable_log", True),
            "admin_bypass_filter": keyword_filter_config.get("admin_bypass_filter", True),
            "repeat_user_enabled": keyword_filter_config.get("repeat_user_enabled", False),
            "repeat_user_threshold": keyword_filter_config.get("repeat_user_threshold", 3),
            "repeat_user_window": keyword_filter_config.get("repeat_user_window", 60),
            "repeat_user_action": keyword_filter_config.get("repeat_user_action", "delete"),
            "repeat_group_enabled": keyword_filter_config.get("repeat_group_enabled", False),
            "repeat_group_threshold": keyword_filter_config.get("repeat_group_threshold", 3),
            "repeat_group_window": keyword_filter_config.get("repeat_group_window", 60),
            "repeat_group_action": keyword_filter_config.get("repeat_group_action", "delete"),
            "repeat_mute_duration": keyword_filter_config.get("repeat_mute_duration", 300),
            "repeat_notice": keyword_filter_config.get("repeat_notice", "检测到重复刷屏，已处理。"),
        }

        # 文转图配置
        text_to_image_config = qqadmin_config.get("text_to_image", {})
        config["features"]["text_to_image"] = {
            "enabled": text_to_image_config.get("enable_text_to_image", False),
            "font_size": text_to_image_config.get("font_size", 16),
            "font_color": text_to_image_config.get("font_color", "#333333"),
            "bg_color": text_to_image_config.get("bg_color", "#ffffff"),
            "line_spacing": text_to_image_config.get("line_spacing", 4),
            "padding": text_to_image_config.get("padding", 20)
        }

        # 定时消息配置
        timed_message_config = qqadmin_config.get("timed_message", {})
        config["features"]["timed_message"] = {
            "enabled": timed_message_config.get("enable_timed_message", False)
        }

        # 分群管理员配置
        sub_admin_config = qqadmin_config.get("sub_admin", {})
        config["features"]["sub_admin"] = {
            "enabled": sub_admin_config.get("enable_sub_admin", False)
        }

    return config

def export_group_config(group_id: str) -> dict:
    return load_group_config(group_id)

def import_group_config(group_id: str, config: dict):
    import copy
    config = copy.deepcopy(config)
    config["group_id"] = group_id
    config["updated_at"] = int(time.time())
    save_group_config(group_id, config)

def get_all_configs_summary() -> list:
    """获取所有群配置的摘要信息"""
    groups = list_all_groups()
    summary = []

    for group_id in groups:
        config = load_group_config(group_id)
        if config:
            features = config.get("features", {})
            summary.append({
                "group_id": group_id,
                "enabled": config.get("enabled", True),
                "authorized": config.get("authorized", False),
                "is_whitelist": config.get("is_whitelist", False),
                "is_blacklist": config.get("is_blacklist", False),
                "features": {
                    "reply": features.get("reply", {}).get("enabled", True),
                    "mute": features.get("mute", {}).get("enabled", True),
                    "kick": features.get("kick", {}).get("enabled", True),
                    "blacklist": features.get("blacklist", {}).get("enabled", True),
                    "audit": features.get("audit", {}).get("enabled", False),
                    "recall": features.get("recall", {}).get("enabled", False),
                    "message_cache": features.get("message_cache", {}).get("enabled", True),
                    "welcome": features.get("welcome", {}).get("enabled", False),
                    "stats": features.get("stats", {}).get("enabled", True),
                    "text_to_image": features.get("text_to_image", {}).get("enabled", False),
                    "timed_message": features.get("timed_message", {}).get("enabled", False),
                    "sub_admin": features.get("sub_admin", {}).get("enabled", False),
                    "keyword_filter": features.get("keyword_filter", {}).get("enabled", False),
                    "player_reply": features.get("player_reply", {}).get("enabled", False),
                    "group_config": features.get("group_config", {}).get("enabled", False)
                },
                "created_at": config.get("created_at", 0),
                "updated_at": config.get("updated_at", 0),
                "auth_info": config.get("auth_info", {})
            })

    return summary

def get_group_info_for_display(group_id: str, global_config: dict = None) -> dict:
    """获取群组信息用于显示，过滤掉不应在分群设置中显示的配置"""
    config = get_group_config(group_id, global_config)
    
    # 过滤敏感配置（不应在分群设置中修改的）
    filtered_config = {
        "group_id": config.get("group_id", group_id),
        "enabled": config.get("enabled", True),
        "authorized": config.get("authorized", False),
        "is_whitelist": config.get("is_whitelist", False),
        "created_at": config.get("created_at", 0),
        "updated_at": config.get("updated_at", 0),
        "features": {}
    }
    
    # 只包含可修改的功能配置
    features = config.get("features", {})
    for feature_name, feature_config in features.items():
        if feature_name not in ["admin_settings", "system"]:  # 排除系统级配置
            filtered_config["features"][feature_name] = feature_config
    
    return filtered_config

def sync_global_config_to_all_groups(global_config: dict):
    """同步全局配置到所有已存在的群组配置"""
    groups = list_all_groups()
    synced_count = 0
    for group_id in groups:
        # 跳过白名单群组（不受限制）
        whitelist_groups = global_config.get("basic", {}).get("whitelist_groups", [])
        if whitelist_groups and group_id in whitelist_groups:
            continue
        
        # 克隆全局配置到该群组
        clone_global_config_to_group(group_id, global_config)
        synced_count += 1
    
    return synced_count

def set_group_priority(group_id: str, priority: int):
    update_group_config(group_id, "priority", priority)

def get_group_priority(group_id: str) -> int:
    config = load_group_config(group_id)
    return config.get("priority", 0)

def add_group_tag(group_id: str, tag: str):
    config = get_group_config(group_id)
    tags = config.get("tags", [])

    if tag not in tags:
        tags.append(tag)
        update_group_config(group_id, "tags", tags)

def remove_group_tag(group_id: str, tag: str):
    config = get_group_config(group_id)
    tags = config.get("tags", [])

    if tag in tags:
        tags.remove(tag)
        update_group_config(group_id, "tags", tags)

def get_group_tags(group_id: str) -> list:
    config = load_group_config(group_id)
    return config.get("tags", [])

def set_group_nickname(group_id: str, nickname: str):
    update_group_config(group_id, "nickname", nickname)

def get_group_nickname(group_id: str) -> str:
    config = load_group_config(group_id)
    return config.get("nickname", "")

def get_groups_by_tag(tag: str) -> list:
    groups = list_all_groups()
    matching_groups = []

    for group_id in groups:
        config = load_group_config(group_id)
        tags = config.get("tags", [])
        if tag in tags:
            matching_groups.append(group_id)

    return matching_groups

def get_groups_by_feature(feature: str) -> list:
    groups = list_all_groups()
    matching_groups = []

    for group_id in groups:
        config = load_group_config(group_id)
        if config.get(feature, False):
            matching_groups.append(group_id)

    return matching_groups

def bulk_update_feature(groups: list, feature: str, enabled: bool):
    for group_id in groups:
        config = load_group_config(group_id)
        if config:
            config[feature] = enabled
            config["updated_at"] = int(time.time())
            save_group_config(group_id, config)
