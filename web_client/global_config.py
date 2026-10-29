"""Safe editing of Joker's global group-management defaults."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .feature_schema import FEATURE_BY_KEY, FieldSpec, choice


_CONFIG_LOCK = threading.RLock()


@dataclass(frozen=True)
class GlobalField:
    path: tuple[str, ...]
    spec: FieldSpec


GENERAL_FIELDS: tuple[GlobalField, ...] = (
    GlobalField(
        ("qqadmin", "enable_group_manage"),
        FieldSpec("enable_group_manage", "启用群管系统", "bool", True),
    ),
    GlobalField(
        ("qqadmin", "enable_group_config"),
        FieldSpec("enable_group_config", "启用分群配置命令", "bool", True),
    ),
    GlobalField(
        ("qqadmin", "auto_sync_global_config"),
        FieldSpec(
            "auto_sync_global_config",
            "自动同步全局配置",
            "bool",
            True,
            "机器人检测到全局文件更新后同步到已有分群",
        ),
    ),
    GlobalField(
        ("qqadmin", "message_prompts", "power_on_prompt"),
        FieldSpec(
            "power_on_prompt",
            "关机时开机提示",
            "text",
            "群管尚未开机，请先发送 /开机。",
            max_length=1000,
        ),
    ),
    GlobalField(
        ("qqadmin", "message_prompts", "power_on_success"),
        FieldSpec(
            "power_on_success",
            "开机成功提示",
            "text",
            "✅️ 已开机",
            max_length=1000,
        ),
    ),
    GlobalField(
        ("qqadmin", "message_prompts", "power_off_success"),
        FieldSpec(
            "power_off_success",
            "关机成功提示",
            "text",
            "❌️ 已关机",
            max_length=1000,
        ),
    ),
    GlobalField(
        ("qqadmin", "message_prompts", "power_off_already"),
        FieldSpec(
            "power_off_already",
            "重复关机提示",
            "text",
            "当前群管已经关机。",
            max_length=1000,
        ),
    ),
    GlobalField(
        ("auth", "permission_mode"),
        choice(
            "auth_permission_mode",
            "卡密管理权限模式",
            "read_only",
            (
                ("read_only", "只读"),
                ("full_access", "完全访问"),
            ),
            "只读时仅机器人主人可修改卡密管理；完全访问时群主和管理员也可操作。",
        ),
    ),
    GlobalField(
        ("qqadmin", "stats", "use_permission"),
        choice(
            "stats_use_permission",
            "签到查看权限",
            "member",
            (
                ("member", "普通成员"),
                ("admin", "群管理员"),
                ("owner", "仅群主"),
            ),
            "控制谁可以查看签到状态、签到日历、签到排行和个人签到数据。",
        ),
    ),
    GlobalField(
        ("qqadmin", "stats", "modify_permission"),
        choice(
            "stats_modify_permission",
            "签到修改权限",
            "admin",
            (
                ("member", "普通成员"),
                ("admin", "群管理员"),
                ("owner", "仅群主"),
            ),
            "控制谁可以绑定或解绑 SteamID，以及重置签到记录。",
        ),
    ),
    GlobalField(
        ("scum", "permission_mode"),
        choice(
            "scum_permission_mode",
            "SCUM 权限模式",
            "read_only",
            (
                ("read_only", "只读"),
                ("full_access", "完全访问"),
            ),
            "只读时仅机器人主人可修改 SCUM 查询设置；完全访问时群主和管理员也可操作。",
        ),
    ),
)


GLOBAL_FEATURE_KEYS: dict[str, dict[str, str]] = {
    "audit": {
        "enabled": "enable_audit",
        "approval_mode": "default_approval_mode",
        "verification_timeout": "default_verification_timeout",
        "max_attempts": "max_verify_attempts",
        "kick_on_verification_failure": "kick_on_verification_failure",
        "admin_bypass": "admin_bypass_verify",
    },
    "welcome": {
        "enabled": "enable_welcome",
        "auto_welcome": "enable_auto_welcome",
        "delay": "default_welcome_delay",
        "message": "default_welcome_message",
        "farewell_enabled": "enable_farewell",
        "farewell_message": "default_farewell_message",
        "keywords": "welcome_keywords",
    },
    "blacklist": {
        "enabled": "enable_blacklist",
        "auto_kick": "auto_kick_blacklisted",
    },
    "mute": {
        "enabled": "enable_mute",
        "level": "default_mute_level",
        "duration": "default_mute_duration",
    },
    "kick": {
        "enabled": "enable_kick",
        "auto_blacklist": "auto_blacklist_on_kick",
        "limit": "default_kick_limit",
    },
    "recall": {
        "enabled": "enable_recall",
        "mode": "default_recall_mode",
        "time_limit": "default_recall_time",
        "keywords": "recall_keywords",
        "regex_patterns": "recall_regex_patterns",
        "self_recall": "enable_self_recall",
        "notification": "recall_notification",
        "notification_message": "recall_notification_message",
        "enable_sensitive_detection": "enable_sensitive_detection",
        "kick_on_severe_violation": "kick_on_severe_violation",
        "sensitive_words": "sensitive_words",
    },
    "message_cache": {"enabled": "enable_message_cache"},
    "reply": {
        "enabled": "enable_reply",
        "cooldown": "default_reply_cooldown",
        "cache_reply_images": "cache_reply_images",
        "reply_image_max_mb": "reply_image_max_mb",
        "reply_image_download_concurrency": "reply_image_download_concurrency",
        "reply_image_retry_interval": "reply_image_retry_interval",
    },
    "player_reply": {"enabled": "enable_player_reply"},
    "entertainment": {"enabled": "enable_entertainment"},
    "stats": {"enabled": "enable_stats"},
    "keyword_filter": {"enabled": "enable_keyword_filter"},
    "timed_message": {"enabled": "enable_timed_message"},
    "sub_admin": {"enabled": "enable_sub_admin"},
    "text_to_image": {"enabled": "enable_text_to_image"},
    # SCUM settings live at the plugin root (``scum``), unlike the QQ group
    # management modules under ``qqadmin``.  The special root handling below
    # keeps the global settings page from writing a dead ``qqadmin.scum``
    # section that the Windows plugin never reads.
    "scum": {
        "enabled": "enable_query",
        "permission_mode": "permission_mode",
        "keyword_command": "keyword_command",
        "online_command": "online_command",
    },
}


def _global_feature_module(config: dict, feature_key: str, *, create: bool = False) -> dict:
    """Return the persisted global module for a feature."""
    root_key = "scum" if feature_key == "scum" else "qqadmin"
    if create:
        module = config.get(root_key)
        if not isinstance(module, dict):
            module = {}
            config[root_key] = module
    else:
        module = config.get(root_key, {})
    return module if isinstance(module, dict) else {}


def get_global_config_path() -> Path:
    from .services import group_config_module

    plugin_data = Path(group_config_module().get_plugin_data_path())
    data_root = plugin_data.parent.parent
    return data_root / "config" / "astrbot_plugin_joker_config.json"


def load_global_config(path: Path | None = None) -> dict:
    target = path or get_global_config_path()
    with _CONFIG_LOCK:
        try:
            with target.open("r", encoding="utf-8-sig") as handle:
                config = json.load(handle)
        except FileNotFoundError:
            return {"qqadmin": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"读取全局配置失败：{exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("全局配置文件格式无效")
    return config


def save_global_config(config: dict, path: Path | None = None) -> None:
    target = path or get_global_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    config = deepcopy(config)
    config["_updated_at"] = int(time.time())
    temporary_path: Path | None = None
    with _CONFIG_LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(config, temporary, ensure_ascii=False, indent=2)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
        except Exception as exc:
            if temporary_path and temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
            raise ValueError(f"保存全局配置失败：{exc}") from exc


def _get_path(config: dict, path: tuple[str, ...], default: Any) -> Any:
    current: Any = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return deepcopy(default)
        current = current[key]
    return deepcopy(current)


def _set_path(config: dict, path: tuple[str, ...], value: Any) -> None:
    current = config
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = deepcopy(value)


def get_general_values(config: dict | None = None) -> dict[str, Any]:
    source = config if config is not None else load_global_config()
    return {
        field.spec.key: _get_path(source, field.path, field.spec.default)
        for field in GENERAL_FIELDS
    }


def update_general_values(values: dict[str, Any]) -> dict:
    expected = {field.spec.key for field in GENERAL_FIELDS}
    if set(values) != expected:
        raise ValueError("全局基础配置字段不完整或包含未知字段")
    config = load_global_config()
    for field in GENERAL_FIELDS:
        _set_path(config, field.path, values[field.spec.key])
    save_global_config(config)
    return config


def get_global_feature_values(feature_key: str, config: dict | None = None) -> dict:
    spec = FEATURE_BY_KEY[feature_key]
    source = config if config is not None else load_global_config()
    root = _global_feature_module(source, feature_key)
    module = root if feature_key == "scum" else root.get(feature_key, {})
    if not isinstance(module, dict):
        module = {}
    key_map = GLOBAL_FEATURE_KEYS.get(feature_key, {})
    return {
        field.key: deepcopy(module.get(key_map.get(field.key, field.key), field.default))
        for field in spec.fields
    }


def update_global_feature_values(feature_key: str, values: dict[str, Any]) -> dict:
    if feature_key not in FEATURE_BY_KEY:
        raise ValueError("未知全局功能模块")
    expected = {field.key for field in FEATURE_BY_KEY[feature_key].fields}
    if set(values) != expected:
        raise ValueError("全局功能配置字段不完整或包含未知字段")
    config = load_global_config()
    root = _global_feature_module(config, feature_key, create=True)
    module = root if feature_key == "scum" else root.get(feature_key)
    if not isinstance(module, dict):
        module = {}
        if feature_key == "scum":
            config["scum"] = module
        else:
            root[feature_key] = module
    key_map = GLOBAL_FEATURE_KEYS.get(feature_key, {})
    group_only_keys = {
        field.key
        for field in FEATURE_BY_KEY[feature_key].fields
        if field.group_only
    }
    for key, value in values.items():
        if key in group_only_keys:
            continue
        module[key_map.get(key, key)] = deepcopy(value)
    save_global_config(config)
    return config


def sync_global_to_groups(config: dict | None = None) -> int:
    from .services import group_config_module

    source = config if config is not None else load_global_config()
    try:
        return int(group_config_module().sync_global_config_to_all_groups(source))
    except Exception as exc:
        raise ValueError(f"同步全局配置失败：{exc}") from exc
