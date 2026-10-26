"""Bridge between the MySQL portal and Joker's existing group modules."""

from __future__ import annotations

import importlib
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .feature_schema import (
    FEATURE_BY_KEY,
    SCUM_COMMAND_ALIASES,
    SCUM_COMMAND_ALIAS_GROUPS,
    SCUM_COMMAND_DEFAULTS,
    FieldSpec,
    default_database_profiles,
    default_steam_signin_profiles,
    normalize_game_server_profile_descriptor,
)
try:
    from feature_catalog import (
        FEATURE_BY_KEY as CATALOG_FEATURE_BY_KEY,
        permission_label,
        split_command_aliases,
    )
except ImportError:  # pragma: no cover - package imported through its parent
    from ..feature_catalog import (
        FEATURE_BY_KEY as CATALOG_FEATURE_BY_KEY,
        permission_label,
        split_command_aliases,
    )
from .models import AuditLog, GroupAccess, GroupConfigSnapshot, User, utcnow
from . import remote_store, reply_asset_service
from .security import card_fingerprint, mask_card, normalize_card, validate_group_id
from .settings import Settings
from .sync_projection import project_group_config
from .game_server_secrets import (
    redact_game_server_profile_records,
    store_game_server_profiles,
)


logger = logging.getLogger(__name__)

# Empty values from older group snapshots should show and execute the same
# defaults as a newly-created group.  Non-template fields keep their empty
# values because empty is meaningful for them (for example group notes).
_MESSAGE_TEMPLATE_FIELDS = frozenset({
    ("audit", "approval_success_message"), ("audit", "approval_failure_message"),
    ("audit", "rejection_success_message"),
    ("blacklist", "auto_kick_message"), ("blacklist", "leave_blacklist_message"),
    ("blacklist", "blacklist_success_message"), ("blacklist", "blacklist_failure_message"),
    ("blacklist", "unblacklist_success_message"), ("blacklist", "unblacklist_failure_message"),
    ("mute", "mute_success_message"), ("mute", "unmute_success_message"),
    ("mute", "mute_failure_message"), ("kick", "kick_success_message"),
    ("kick", "kick_failure_message"), ("recall", "notification_message"),
    ("recall", "manual_failure_message"),
    ("stats", "signin_message"), ("stats", "bind_success_message"),
    ("stats", "bind_failure_message"), ("stats", "unbind_success_message"),
    ("stats", "unbind_failure_message"), ("stats", "signin_reset_success_message"),
    ("stats", "signin_reset_failure_message"), ("stats", "duplicate_steam_message"),
    ("stats", "steam_not_found_message"), ("stats", "steam_database_error_message"),
    ("stats", "database_reward_error_message"), ("stats", "database_already_signed_message"),
    ("stats", "steam_reward_failure_message"), ("stats", "unverified_mute_message"),
    ("welcome", "message"), ("welcome", "farewell_message"),
    ("lottery", "success_message"), ("lottery", "empty_message"),
    ("keyword_filter", "warn_message"), ("keyword_filter", "mute_message"),
    ("keyword_filter", "kick_message"), ("keyword_filter", "blacklist_message"),
    ("game_commands", "success_message"), ("game_commands", "failure_message"),
})


# These are the permissions implemented by the Windows-side ``sub_admin``
# module.  ``reply`` only grants reply-content maintenance; it never grants a
# moderation action.  Keep the portal allow-list here instead of accepting
# arbitrary request values into a configuration that will be applied on the
# bot host.
SUB_ADMIN_PERMISSION_KEYS = ("reply", "mute", "kick", "recall", "blacklist")


class PortalError(ValueError):
    pass


def remote_data_mode() -> bool:
    return os.getenv("JOKER_WEB_DATA_MODE", "local").strip().lower() == "remote"


def _require_remote_db(db: Session | None) -> Session:
    if db is None:
        raise RuntimeError("remote portal operation requires a database session")
    return db


@lru_cache(maxsize=None)
def _plugin_module(relative_name: str):
    package = __package__ or "web_client"
    parent = package.rsplit(".", 1)[0] if "." in package else ""
    candidates = [f"{parent}.{relative_name}" if parent else ""]

    # When started as ``python -m web_client``, web_client is a top-level
    # package. Discover AstrBot's source root and import this plugin by its
    # full namespace so qqadmin's ``..auth`` imports remain valid.
    plugin_root = Path(__file__).resolve().parent.parent
    for ancestor in plugin_root.parents:
        if (ancestor / "astrbot").is_dir():
            ancestor_text = str(ancestor)
            if ancestor_text not in sys.path:
                sys.path.insert(0, ancestor_text)
            try:
                plugin_package = ".".join(plugin_root.relative_to(ancestor).parts)
            except ValueError:
                break
            candidates.append(f"{plugin_package}.{relative_name}")
            break

    candidates.append(relative_name)
    last_error: Exception | None = None
    for module_name in candidates:
        if not module_name:
            continue
        try:
            return importlib.import_module(module_name)
        except ImportError as exc:
            last_error = exc
    raise RuntimeError(
        "无法加载 Joker 插件模块。请在 AstrBot 使用的 Python 环境中启动网页端。"
    ) from last_error


def auth_manager():
    return _plugin_module("auth.manager")


def group_config_module():
    return _plugin_module("qqadmin.group_config")


def plugin_module(name: str):
    return _plugin_module(f"qqadmin.{name}")


def _safe_expire_value(value: Any, default: int = 0) -> int:
    """Normalize historical authorization timestamps before rendering pages."""
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed


_PORTAL_TIMEZONE = timezone(timedelta(hours=8))


def _format_runtime_timestamp(value: Any, empty: str = "-") -> str:
    """Format Windows epoch data for a Chinese portal without leaking raw IDs."""

    timestamp = _safe_expire_value(value)
    if timestamp <= 0:
        return empty
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(
            _PORTAL_TIMEZONE
        ).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return empty


def authorization_state(group_id: str, db: Session | None = None) -> dict[str, Any]:
    if remote_data_mode():
        return remote_store.get_auth_state(_require_remote_db(db), group_id)
    manager = auth_manager()
    state = manager.get_group_auth_state(str(group_id))
    info = manager.get_group_auth_info(str(group_id)) or {}
    expire = _safe_expire_value(info.get("expire", 0))
    return {
        "state": state,
        "active": state == "active",
        "expire": expire,
        "expire_text": _format_expire(expire),
    }


def get_group_game_status(group_id: str, db: Session | None = None) -> dict[str, Any]:
    """Build the read-only game/WebSocket heartbeat panel for one group."""
    if not remote_data_mode():
        return {
            "state": "local",
            "active": False,
            "expire": 0,
            "expire_text": "由 Windows 本地执行",
            "card_mask": "",
            "client_id": "",
            "last_heartbeat_text": "网页未启用远程同步",
            "message": "当前为本地模式，游戏执行器和数据库不会经过 Linux 网页。",
        }
    status = remote_store.get_game_authorization_status(
        _require_remote_db(db), group_id
    )
    state = str(status.get("state", "missing") or "missing")
    expire = _safe_expire_value(status.get("expire", 0))
    heartbeat = status.get("last_heartbeat_at")
    heartbeat_text = "尚未收到 Windows 游戏心跳"
    if isinstance(heartbeat, datetime):
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        heartbeat = heartbeat.astimezone(ZoneInfo("Asia/Shanghai"))
        heartbeat_text = f"最近心跳：{heartbeat.strftime('%Y-%m-%d %H:%M:%S')} CST"
    client_id = str(status.get("client_id", "") or "")[:64]
    if len(client_id) > 12:
        client_id = f"{client_id[:6]}…{client_id[-4:]}"
    if state == "active":
        message = "自建游戏授权卡密心跳有效，允许数据库快照拉取和游戏 WebSocket 调用。"
    elif state == "expired":
        message = "自建游戏授权卡密已过期；IM3 网站卡密不能替代它，请在 Windows 机器人端续期。"
    else:
        message = "尚未同步有效的自建游戏授权卡密；数据库快照和游戏 WebSocket 调用会被拒绝。"
    return {
        "state": state,
        "active": state == "active",
        "expire": expire,
        "expire_text": _format_expire(expire),
        "card_mask": str(status.get("card_mask", "") or "")[:64],
        "client_id": client_id,
        "last_heartbeat_text": heartbeat_text,
        "message": message,
    }


def _format_expire(expire: int) -> str:
    if expire == -1:
        return "永久授权"
    if expire <= 0:
        return "无授权记录"
    try:
        return datetime.fromtimestamp(expire).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "鏃犳巿鏉冭褰?"


def _expire_datetime(expire: int) -> datetime | None:
    if expire == -1 or expire <= 0:
        return None
    try:
        return datetime.fromtimestamp(expire)
    except (OSError, OverflowError, ValueError):
        return None


def claim_activated_card(
    db: Session,
    user: User,
    raw_card: str,
    settings: Settings,
) -> GroupAccess:
    """Claim the group attached to a card already activated in group chat."""
    card = normalize_card(raw_card)
    fingerprint = card_fingerprint(card, settings.card_pepper)

    if remote_data_mode():
        snapshot = db.scalar(
            select(GroupConfigSnapshot).where(
                GroupConfigSnapshot.auth_card_fingerprint == fingerprint
            )
        )
        if snapshot is None:
            raise PortalError(
                remote_store.missing_card_sync_message(
                    db, settings.offline_after_seconds
                )
            )
        group_id = validate_group_id(snapshot.group_id)
        snapshot_state = str(snapshot.auth_state or "missing").strip().lower()
        snapshot_expire = _safe_expire_value(snapshot.auth_expire)
        if (
            snapshot_state == "active"
            and snapshot_expire > 0
            and snapshot_expire <= int(time.time())
        ):
            snapshot_state = "expired"
        if snapshot_state == "missing":
            raise PortalError("该卡密对应的群聊授权已被删除或撤销，无法登记或登录。请联系管理员重新授权。")
        if snapshot_state == "unverified":
            raise PortalError(
                "Windows 授权文件暂时无法完整读取，当前不能验证这张卡密。"
                "请等待 Windows 同步恢复后再试。"
            )
        if snapshot_state != "active":
            raise PortalError("该卡密授权已过期，请先在群聊激活新卡密")
        owner = db.scalar(select(GroupAccess).where(GroupAccess.group_id == group_id))
        if owner is not None and owner.user_id != user.id:
            raise PortalError("该群已经绑定其他客户账号，请联系管理员处理")
        duplicate = db.scalar(
            select(GroupAccess).where(GroupAccess.card_fingerprint == fingerprint)
        )
        if duplicate is not None and (owner is None or duplicate.id != owner.id):
            raise PortalError("该卡密已经绑定过网页账号")
        try:
            activated_at = datetime.fromtimestamp(snapshot.auth_activated_at)
        except (OSError, OverflowError, TypeError, ValueError):
            activated_at = utcnow()
        if owner is None:
            owner = GroupAccess(
                user_id=user.id,
                group_id=group_id,
                role="owner",
                card_fingerprint=fingerprint,
                card_mask=snapshot.auth_card_mask or mask_card(card),
                activated_at=activated_at,
                expires_at=_expire_datetime(snapshot.auth_expire),
            )
            db.add(owner)
        else:
            owner.card_fingerprint = fingerprint
            owner.card_mask = snapshot.auth_card_mask or mask_card(card)
            owner.activated_at = activated_at
            owner.expires_at = _expire_datetime(snapshot.auth_expire)
        # A remote bootstrap may contain only authorization metadata.  Seed a
        # complete editable feature map on the first claim so the customer
        # immediately sees a stable set of group controls instead of an empty
        # page that depends on the next Windows upload.
        snapshot_config = snapshot.config_json if isinstance(snapshot.config_json, dict) else {}
        snapshot_features = snapshot_config.get("features") if isinstance(snapshot_config, dict) else None
        if not isinstance(snapshot_features, dict) or not snapshot_features:
            try:
                default_config = group_config_module().get_group_config(group_id)
                projected_defaults = project_group_config(default_config)
                if isinstance(projected_defaults, dict) and projected_defaults.get("features"):
                    projected_defaults["group_id"] = group_id
                    snapshot.config_json = projected_defaults
            except Exception:
                logger.debug("初始化远程群默认功能失败 group_id=%s", group_id, exc_info=True)
        # The first bootstrap exchange may have carried only the card
        # fingerprint (to avoid syncing an unclaimed group).  Once the web
        # account claims it, explicitly ask the next Windows heartbeat for the
        # complete group projection.
        snapshot.refresh_requested = True
        snapshot.sync_status = "pending"
        return owner

    manager = auth_manager()

    used_info = manager.get_used_keys().get(card)
    if not isinstance(used_info, dict):
        raise PortalError("该卡密尚未在群聊激活，或已被删除。请在对应群使用当前卡密重新激活。")
    try:
        group_id = validate_group_id(str(used_info.get("group_id", "")))
    except ValueError:
        raise PortalError("该卡密的群授权记录无效，请联系管理员处理") from None

    auth_info = manager.get_group_auth_info(group_id) or {}
    if auth_info.get("key") != card:
        raise PortalError("该卡密不是本群当前授权卡密，请使用群聊中最后激活的卡密")
    if manager.get_group_auth_state(group_id) != "active":
        raise PortalError("该卡密授权已过期，请先在群聊激活新卡密")

    owner = db.scalar(select(GroupAccess).where(GroupAccess.group_id == group_id))
    if owner is not None and owner.user_id != user.id:
        raise PortalError("该群已经绑定其他客户账号，请联系管理员处理")

    duplicate = db.scalar(
        select(GroupAccess).where(GroupAccess.card_fingerprint == fingerprint)
    )
    if duplicate is not None and (owner is None or duplicate.id != owner.id):
        raise PortalError("该卡密已经绑定过网页账号")

    expire = int(auth_info.get("expire", 0) or 0)
    try:
        activated_at = datetime.fromtimestamp(int(used_info.get("activated_at", 0)))
    except (OSError, OverflowError, TypeError, ValueError):
        activated_at = utcnow()
    if owner is None:
        owner = GroupAccess(
            user_id=user.id,
            group_id=group_id,
            role="owner",
            card_fingerprint=fingerprint,
            card_mask=mask_card(card),
            activated_at=activated_at,
            expires_at=_expire_datetime(expire),
        )
        db.add(owner)
    else:
        owner.card_fingerprint = fingerprint
        owner.card_mask = mask_card(card)
        owner.activated_at = activated_at
        owner.expires_at = _expire_datetime(expire)

    # Initialize the plugin-side group file with safe defaults when needed.
    config_api = group_config_module()
    config = config_api.get_group_config(group_id)
    upsert_snapshot(db, group_id, config, user.id)
    return owner


def get_user_group(db: Session, user_id: int, group_id: str) -> GroupAccess | None:
    return db.scalar(
        select(GroupAccess).where(
            GroupAccess.user_id == user_id,
            GroupAccess.group_id == str(group_id),
        )
    )


def _clear_cloned_database_profile_links(config: dict[str, Any]) -> dict[str, Any]:
    """Keep cloned stats policy but detach source-group database files."""

    result = deepcopy(config)
    features = result.get("features")
    if not isinstance(features, dict):
        return result
    stats = features.get("stats")
    if not isinstance(stats, dict):
        return result
    stats["database_profile"] = ""
    stats["database_profiles"] = []
    stats["database_path"] = ""
    stats.pop("sqlite_path", None)
    return result


def clone_group_config(
    source_group_id: str,
    target_group_id: str,
    db: Session | None = None,
    updated_by: int | None = None,
) -> dict[str, Any]:
    """Copy web-editable group-management settings to another group.

    Group authorization, card identity, and the target power state belong to
    the destination and are never copied.  In remote mode the source is the
    last Windows projection stored on Linux; the next Windows exchange applies
    the new target revision.  Local mode delegates to the existing atomic
    plugin clone helper.
    """
    source = validate_group_id(str(source_group_id))
    target = validate_group_id(str(target_group_id))
    if source == target:
        raise PortalError("源群和目标群不能相同")

    if remote_data_mode():
        remote_db = _require_remote_db(db)
        source_snapshot = remote_store.get_snapshot(remote_db, source)
        if source_snapshot is None:
            raise PortalError("源群尚未完成 Windows 同步，暂时不能复制")
        source_config = remote_store.get_config(remote_db, source)
        cloned = _clear_cloned_database_profile_links(
            project_group_config(source_config)
        )
        cloned["group_id"] = target
        cloned = reply_asset_service.clone_group_reply_assets(
            remote_db, source, target, cloned, updated_by
        )

        def mutate(target_config):
            candidate = deepcopy(cloned)
            # The destination keeps its own switch state.  All other projected
            # feature settings and web-managed rules are copied from the source.
            # Read the destination only after its row lock is held: otherwise a
            # concurrent power save can be lost together with its whole config.
            source_features = candidate.get("features")
            target_features = (
                target_config.get("features")
                if isinstance(target_config, dict)
                else {}
            )
            if (
                isinstance(source_features, dict)
                and isinstance(target_features, dict)
                and target_features.get("power_status") in {"on", "off"}
            ):
                source_features["power_status"] = target_features["power_status"]
            elif isinstance(source_features, dict):
                # A target without an explicit state starts in its normal off
                # state; never inherit the source group's live switch status.
                source_features.pop("power_status", None)
            return candidate

        return remote_store.mutate_web_config(remote_db, target, updated_by, mutate)

    module = group_config_module()
    if not module.load_group_config(source):
        raise PortalError("源群配置不存在")
    if not module.clone_group_config(source, target):
        raise PortalError("复制群配置失败")
    for setting, value in (
        ("database_profile", ""),
        ("database_profiles", []),
        ("database_path", ""),
        ("sqlite_path", ""),
    ):
        module.update_group_feature_config(
            target,
            "stats",
            setting,
            value,
            operator_id=str(updated_by or "web-clone"),
        )
    return module.get_group_config(target)


def user_has_login_entitlement(db: Session, user: User) -> bool:
    """Return whether a customer may still sign in to manage claimed groups.

    A portal username/password remains the authentication credential; a card is
    only the group-authorization credential.  Accounts with no claimed group
    are allowed to sign in so a newly registered customer can claim their
    first activated card.  Once an account owns a group, deleting/revoking all
    of its corresponding group authorizations blocks both new and existing
    sessions without deleting the account or its audit history.
    """
    if user.is_admin:
        return True
    group_ids = list(
        db.scalars(
            select(GroupAccess.group_id).where(GroupAccess.user_id == user.id)
        )
    )
    if not group_ids:
        return True
    if remote_data_mode():
        # One indexed join avoids one database round-trip per owned group on
        # every authenticated page request.
        return db.scalar(
            select(GroupAccess.id)
            .join(
                GroupConfigSnapshot,
                GroupConfigSnapshot.group_id == GroupAccess.group_id,
            )
            .where(
                GroupAccess.user_id == user.id,
                # A username/password authenticates the customer, while a
                # currently active group card authorizes portal access.  An
                # expired snapshot must not keep an otherwise revoked account
                # signed in merely because it still has historical data.
                GroupConfigSnapshot.auth_state == "active",
                (
                    (GroupConfigSnapshot.auth_expire == -1)
                    | (GroupConfigSnapshot.auth_expire > int(time.time()))
                ),
            )
            .limit(1)
        ) is not None

    # Local mode has the canonical authorization JSON available in one read.
    # ``list_all_authorizations`` intentionally returns historical records too
    # (the admin page needs to show expired entries), so membership alone is
    # not an entitlement check.  Require at least one currently active group;
    # otherwise an expired/revoked card would leave an existing web session
    # usable indefinitely.
    manager = auth_manager()
    authorizations = manager.list_all_authorizations()
    if not isinstance(authorizations, dict):
        return False
    now = int(time.time())
    for group_id in group_ids:
        info = authorizations.get(str(group_id))
        if not isinstance(info, dict):
            continue
        expire = _safe_expire_value(info.get("expire", 0))
        if expire == -1 or expire > now:
            return True
    return False


def create_admin_account(
    db: Session, username: str, password: str, reset_existing_password: bool = True
) -> User:
    """Create or promote a portal administrator."""
    from .security import hash_password, validate_password, validate_username

    normalized_username = validate_username(username)
    validated_password = validate_password(password)
    user = db.scalar(select(User).where(User.username == normalized_username))
    if user is None:
        user = User(
            username=normalized_username,
            password_hash=hash_password(validated_password),
            role="admin",
            is_active=True,
        )
        db.add(user)
    else:
        user.role = "admin"
        user.is_active = True
        if reset_existing_password:
            user.password_hash = hash_password(validated_password)
    db.commit()
    db.refresh(user)
    return user


def can_remove_admin(db: Session, target: User, actor: User) -> bool:
    if target.id == actor.id:
        return False
    if not target.is_admin:
        return True
    active_admins = db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == "admin", User.is_active.is_(True))
    ) or 0
    return active_admins > 1


def generate_admin_license_keys(days: int, count: int) -> list[str]:
    if remote_data_mode():
        raise PortalError("远程网页不保存授权密钥，请在本地群聊使用主人命令生成卡密")
    from .global_config import load_global_config

    if days == 0 or days < -1 or days > 36500:
        raise PortalError("卡密天数必须为 1-36500，-1 表示永久")
    if count < 1 or count > 100:
        raise PortalError("单次生成数量必须为 1-100")
    auth_key = str(load_global_config().get("auth", {}).get("auth_key", "")).strip()
    if not auth_key:
        raise PortalError("插件尚未配置授权密钥，无法生成卡密")
    manager = auth_manager()
    # Cards are customer-facing and one-time use. Avoid same-millisecond
    # collisions when an administrator generates several batches quickly.
    existing = set(manager.get_unused_keys()) | set(manager.get_used_keys())
    keys: list[str] = []
    index = 0
    while len(keys) < count and index < count * 200:
        key = manager.generate_license_key(auth_key, days, "", index)
        index += 1
        if key in existing or key in keys:
            continue
        keys.append(key)
        existing.add(key)
    if len(keys) != count:
        raise PortalError("卡密生成冲突，请稍后重试")
    manager.add_unused_keys(keys)
    return keys


def get_admin_license_overview(db: Session | None = None) -> dict[str, Any]:
    if remote_data_mode():
        remote_db = _require_remote_db(db)
        used = []
        for snapshot in remote_db.scalars(
            select(GroupConfigSnapshot).where(
                GroupConfigSnapshot.auth_card_mask != ""
            )
        ):
            activated_at = int(snapshot.auth_activated_at or 0)
            used.append(
                {
                    "card_mask": snapshot.auth_card_mask,
                    "group_id": snapshot.group_id,
                    "user_id": snapshot.auth_user_id,
                    "activated_at": activated_at,
                    "activated_text": (
                        datetime.fromtimestamp(activated_at).strftime("%Y-%m-%d %H:%M")
                        if activated_at > 0
                        else "未知"
                    ),
                    "auth": authorization_state(snapshot.group_id, remote_db),
                }
            )
        used.sort(key=lambda item: item["activated_at"], reverse=True)
        return {"unused": [], "used": used, "remote_read_only": True}
    manager = auth_manager()
    unused = list(manager.get_unused_keys())
    used = []
    for card, info in manager.get_used_keys().items():
        if not isinstance(info, dict):
            continue
        group_id = str(info.get("group_id", ""))
        auth = authorization_state(group_id) if group_id else {
            "state": "missing",
            "active": False,
            "expire": 0,
            "expire_text": "无授权记录",
        }
        activated_at = int(info.get("activated_at", 0) or 0)
        try:
            activated_text = (
                datetime.fromtimestamp(activated_at).strftime("%Y-%m-%d %H:%M")
                if activated_at > 0
                else "未知"
            )
        except (OSError, OverflowError, ValueError):
            activated_text = "未知"
        used.append(
            {
                "card_mask": mask_card(card),
                "group_id": group_id,
                "user_id": str(info.get("user_id", "")),
                "activated_at": activated_at,
                "activated_text": activated_text,
                "auth": auth,
            }
        )
    used.sort(key=lambda item: item["activated_at"], reverse=True)
    return {"unused": unused, "used": used}


def delete_unused_license_key(raw_card: str) -> None:
    if remote_data_mode():
        raise PortalError("远程网页不保存未使用卡密，请在本地群聊管理卡密")
    card = normalize_card(raw_card)
    manager = auth_manager()
    if card not in manager.get_unused_keys():
        raise PortalError("未使用卡密不存在")
    manager.remove_unused_key(card)


def _local_sync_state() -> dict[str, Any]:
    """Return the full status shape used by remote-mode dashboard cards.

    Local mode reads the Windows plugin files in the same process.  Returning
    only ``online`` and ``text`` used to make templates fall back to the
    misleading “尚未连接/连接失败” labels even though the configuration was
    immediately usable.
    """

    return {
        "online": True,
        "pending": False,
        "status": "local",
        "connection_state": "local",
        "badge": "本地直连",
        "text": "网页与 Windows 本地直连",
        "display_text": "网页与 Windows 本地直连",
        "detail": "当前网页直接读取 Windows 插件配置，无需等待远程心跳。",
        "revision": 0,
        "applied_revision": 0,
        "last_seen_at": None,
        "last_seen_text": "本地插件正在运行",
        "saved_at": "",
        "saved_by": 0,
        "refresh_requested": False,
    }


def get_group_display_name(config: object, group_id: object) -> str:
    """Return the configured portal name, falling back to the QQ number.

    ``nickname`` is mirrored at the config root for older Windows workers and
    under ``features.group_config`` for the current schema.  Prefer a
    non-empty value from either location so an empty legacy mirror cannot hide
    a name that was just saved by the web portal.
    """
    normalized_id = str(group_id or "").strip()
    if not isinstance(config, dict):
        return normalized_id
    root_name = str(config.get("nickname", "") or "").strip()
    features = config.get("features")
    feature = features.get("group_config") if isinstance(features, dict) else None
    feature_name = str(feature.get("nickname", "") or "").strip() if isinstance(feature, dict) else ""
    return (root_name or feature_name or normalized_id)[:100]


def list_user_groups(db: Session, user_id: int) -> list[dict[str, Any]]:
    accesses = list(
        db.scalars(
            select(GroupAccess)
            .where(GroupAccess.user_id == user_id)
            .order_by(GroupAccess.group_id)
        )
    )
    remote = remote_data_mode()
    snapshots = (
        remote_store.list_snapshots(db, [access.group_id for access in accesses])
        if remote
        else {}
    )
    offline_after_seconds = Settings.from_env().offline_after_seconds if remote else 0
    result = []
    for access in accesses:
        snapshot = snapshots.get(str(access.group_id)) if remote else None
        auth = (
            remote_store.auth_state_from_snapshot(snapshot)
            if remote
            else authorization_state(access.group_id, db)
        )
        config = (
            deepcopy(snapshot.config_json)
            if remote and snapshot is not None and isinstance(snapshot.config_json, dict)
            else (
                {"group_id": str(access.group_id), "features": {}}
                if remote
                else group_config_module().get_group_config(access.group_id)
            )
        )
        features = config.get("features", {}) if isinstance(config, dict) else {}
        features = features if isinstance(features, dict) else {}
        enabled_count = sum(
            1
            for key in FEATURE_BY_KEY
            if isinstance(features.get(key), dict) and features[key].get("enabled") is True
        )
        # ``authorization_state``/the remote snapshot is the source of truth
        # for the current expiry.  Do not copy it back to ``GroupAccess`` while
        # merely rendering a dashboard: that turns every page view into a
        # database UPDATE and can contend with the Windows exchange/ACK row
        # locks, which is especially visible as a save timeout on a busy portal.
        # Claim/renew operations still persist ``expires_at`` explicitly.
        expires_at = _expire_datetime(auth["expire"])
        result.append(
            {
                "access": access,
                "group_display_name": get_group_display_name(config, access.group_id),
                "auth": auth,
                "expires_at": expires_at,
                "powered_on": features.get("power_status") == "on",
                "enabled_count": enabled_count,
                "sync": remote_store.sync_state(snapshot, offline_after_seconds)
                if remote
                else _local_sync_state(),
            }
        )
    return result


def known_group_ids(db: Session) -> set[str]:
    if remote_data_mode():
        group_ids = remote_store.known_group_ids(db)
        group_ids.update(str(group_id) for group_id in db.scalars(select(GroupAccess.group_id)))
        return group_ids
    config_api = group_config_module()
    group_ids = {str(group_id) for group_id in config_api.list_all_groups()}
    group_ids.update(
        str(group_id) for group_id in auth_manager().list_all_authorizations().keys()
    )
    group_ids.update(str(group_id) for group_id in db.scalars(select(GroupAccess.group_id)))
    return group_ids


def list_admin_groups(db: Session) -> list[dict[str, Any]]:
    accesses = {
        access.group_id: access
        for access in db.scalars(select(GroupAccess).order_by(GroupAccess.group_id))
    }
    group_ids = sorted(known_group_ids(db), key=lambda value: (len(value), value))
    remote = remote_data_mode()
    snapshots = remote_store.list_snapshots(db, group_ids) if remote else {}
    offline_after_seconds = Settings.from_env().offline_after_seconds if remote else 0
    result = []
    for group_id in group_ids:
        snapshot = snapshots.get(str(group_id)) if remote else None
        config = (
            deepcopy(snapshot.config_json)
            if remote and snapshot is not None and isinstance(snapshot.config_json, dict)
            else (
                {"group_id": str(group_id), "features": {}}
                if remote
                else group_config_module().load_group_config(group_id)
            )
        )
        features = config.get("features", {}) if isinstance(config, dict) else {}
        access = accesses.get(group_id)
        result.append(
            {
                "group_id": group_id,
                "group_display_name": get_group_display_name(config, group_id),
                "access": access,
                "owner": access.user if access is not None else None,
                "auth": (
                    remote_store.auth_state_from_snapshot(snapshot)
                    if remote
                    else authorization_state(group_id, db)
                ),
                "powered_on": isinstance(features, dict)
                and features.get("power_status") == "on",
                "configured": bool(config),
                "enabled_count": sum(
                    1
                    for key in FEATURE_BY_KEY
                    if isinstance(features, dict)
                    and isinstance(features.get(key), dict)
                    and features[key].get("enabled") is True
                ),
                "sync": remote_store.sync_state(snapshot, offline_after_seconds)
                if remote
                else _local_sync_state(),
            }
        )
    return result


ROOT_MIRRORS: dict[str, dict[str, str]] = {
    "audit": {
        "enabled": "audit_enabled",
        "approval_mode": "approval_mode",
        "verification_timeout": "verification_timeout",
        "max_attempts": "max_attempts",
        "kick_on_verification_failure": "kick_on_verification_failure",
        # Older Windows-side helpers still read this compatibility key.
        "admin_bypass": "admin_bypass",
    },
    "welcome": {
        "enabled": "welcome_enabled",
        "delay": "welcome_delay",
        "message": "welcome_message",
        "farewell_enabled": "farewell_enabled",
        "farewell_message": "farewell_message",
        "keywords": "welcome_keywords",
        "at_new_member": "at_new_member",
        "auto_reply_enabled": "welcome_auto_reply",
    },
    "recall": {
        "enabled": "recall_enabled",
        "time_limit": "recall_time",
        "mode": "recall_mode",
        "keywords": "recall_keywords",
        "regex_patterns": "recall_regex",
        "enable_admin_recall": "enable_admin_recall",
        "enable_sensitive_detection": "recall_sensitive_enabled",
        "sensitive_words": "recall_sensitive_words",
        "notification": "recall_notification",
        "notification_message": "recall_notification_message",
    },
    # Basic per-group metadata is kept at the configuration root so older
    # Windows builds (which do not know the portal feature schema) can still
    # read it.  The feature card remains the single web save boundary.
    "group_config": {
        "nickname": "nickname",
        "database_player_name": "database_player_name",
        "tags": "tags",
        "priority": "priority",
        "bot_account_priorities": "bot_account_priorities",
        "group_note": "group_note",
        "timezone": "timezone",
        # Older Windows-side member-identity helpers read these values from
        # the group-config root. Keep them mirrored with the feature payload
        # so a Linux save is effective on both old and new workers.
        "auto_member_name": "auto_member_name",
        "member_name_prefix": "member_name_prefix",
        "member_name_prefix_include_admins": "member_name_prefix_include_admins",
        "member_names": "member_names",
        "auto_member_title": "auto_member_title",
        "member_titles": "member_titles",
    },
}


# Several Windows group-management modules predate the common ``features``
# object and still consume their policy from root-level dictionaries.  Keep
# those canonical runtime settings synchronized whenever the portal saves a
# feature form.  Only the web-exposed policy fields are mirrored; operational
# records (mute lists, kick logs, blacklists, etc.) remain local-only.
NESTED_ROOT_MIRRORS: dict[str, tuple[str, tuple[str, ...]]] = {
    "blacklist": (
        "blacklist_settings",
        (
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
        ),
    ),
    "mute": (
        "mute_settings",
        (
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
        ),
    ),
    "kick": (
        "kick_settings",
        (
            "enabled",
            "auto_blacklist",
            "limit",
            "kick_success_message",
            "kick_failure_message",
        ),
    ),
}


# The editable lottery definition lives at the projection root so older
# Windows clients (which read ``group_config["lottery"]``) can consume it.
# Keep the same bounded limits as the Windows runtime here as well; otherwise
# a malformed browser request could make the next heartbeat unnecessarily
# large or fail while applying a config revision.
_LOTTERY_SCALAR_KEYS = (
    "enabled",
    "keyword",
    "max_draws_per_user",
    "cooldown",
    "success_message",
    "empty_message",
    "campaigns",
)
_LOTTERY_MAX_AWARDS = 100
_LOTTERY_MAX_NAME = 100
_LOTTERY_MAX_MESSAGE = 1000
_LOTTERY_MAX_STOCK = 1_000_000
_LOTTERY_MAX_DRAWS = 1_000_000
_LOTTERY_MAX_COOLDOWN = 31_536_000
_LOTTERY_AWARD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SIGNIN_PROFILE_REWARD_KEYS = (
    "base_reward",
    "reward_3_days",
    "reward_7_days",
    "reward_14_days",
    "reward_30_days",
)
_SIGNIN_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_SIGNIN_PROFILE_MAX = 50
_SIGNIN_PROFILE_MODES = frozenset({"basic", "steam"})
_SIGNIN_PROFILE_SCHEDULES = frozenset(
    {"daily", "weekly", "monthly", "streak", "custom"}
)
_SIGNIN_REWARD_MODES = frozenset({"database", "websocket"})
_SIGNIN_REWARD_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_DEFAULT_SIGNIN_REWARD_COMMAND = "#ChangeCurrencyBalance Normal {reward} {steam_id}"


def _validate_signin_profile_commands(
    profiles: object,
    *,
    signin_command: object = "签到",
    bind_command: object = "绑定SteamID",
) -> None:
    """Reject scheme commands that collide with the group's fixed commands."""
    top_level_signin_aliases = split_command_aliases(signin_command, "签到")
    top_level_bind_aliases = split_command_aliases(bind_command, "绑定SteamID")
    reserved = {
        alias.casefold()
        for alias in top_level_bind_aliases
    } | {"绑定steamid", "绑定steam"}
    reserved.update(
        alias.casefold()
        for alias in top_level_signin_aliases
        if alias != "签到"
    )
    # A profile may intentionally own the built-in /签到 token while the
    # group keeps the default top-level command.  Once that top-level command
    # is renamed, /签到 becomes a retired alias and must stay reserved.
    if "签到" not in top_level_signin_aliases:
        reserved.add("签到")
    for profile in profiles if isinstance(profiles, list) else []:
        if not isinstance(profile, dict) or not profile.get("enabled", True):
            continue
        raw_command = profile.get("command", profile.get("signin_command", ""))
        command_aliases = split_command_aliases(raw_command, "") if str(raw_command or "").strip() else ()
        if any(alias.casefold() in reserved for alias in command_aliases):
            raise PortalError("签到方案命令不能与当前签到或 SteamID 绑定命令重复")


def _as_lottery_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _as_portal_bool(value: Any, default: bool = False) -> bool:
    """Normalize a browser/legacy JSON boolean without truthy-string traps."""

    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _signin_profiles_for_view(config: object) -> list[dict[str, Any]]:
    """Return bounded, template-safe independent sign-in definitions.

    Older releases stored one compact JSON object per list row while current
    Windows builds store dictionaries.  The visual editor accepts both shapes
    but never lets a malformed historical row make the whole group page fail.
    """

    source = config if isinstance(config, dict) else {}
    features = source.get("features")
    stats = features.get("stats") if isinstance(features, dict) else None
    raw_profiles = stats.get("signin_profiles", []) if isinstance(stats, dict) else []
    if not isinstance(raw_profiles, (list, tuple)):
        return []

    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_commands: set[str] = set()
    for raw in raw_profiles[:_SIGNIN_PROFILE_MAX]:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        if not isinstance(raw, dict):
            continue
        profile_id = str(raw.get("id", "") or "").strip()
        command = "|".join(
            split_command_aliases(
                raw.get("command", raw.get("signin_command", "")), "签到"
            )
        ) if str(raw.get("command", raw.get("signin_command", "")) or "").strip() else ""
        command_aliases = split_command_aliases(command, "签到") if command else ()
        if (
            not _SIGNIN_PROFILE_ID_RE.fullmatch(profile_id)
            or profile_id in seen_ids
            or not command
            or len(command) > 256
            or any(character.isspace() for character in command)
            or any(alias.casefold() in seen_commands for alias in command_aliases)
        ):
            continue
        mode = str(raw.get("mode", raw.get("signin_mode", "basic")) or "basic").strip().lower()
        if mode not in _SIGNIN_PROFILE_MODES:
            mode = "basic"
        schedule = str(raw.get("schedule", raw.get("schedule_mode", "daily")) or "daily").strip().lower()
        if schedule not in _SIGNIN_PROFILE_SCHEDULES:
            schedule = "daily"
        profile = {
            "id": profile_id,
            "name": str(raw.get("name", profile_id) or profile_id).strip()[:80],
            "command": command,
            "mode": mode,
            "schedule": schedule,
            "enabled": _as_portal_bool(raw.get("enabled"), True),
            "signin_message": str(raw.get("signin_message", "") or "")[:1000],
            "steam_reward_mode": str(
                raw.get("steam_reward_mode", raw.get("reward_mode", "database"))
                or "database"
            ).strip().lower(),
            "steam_reward_operation": str(
                raw.get("steam_reward_operation", "execute_game_command")
                or "execute_game_command"
            ).strip().lower()[:64],
            "steam_reward_command": str(
                raw.get("steam_reward_command", _DEFAULT_SIGNIN_REWARD_COMMAND)
                or _DEFAULT_SIGNIN_REWARD_COMMAND
            ).strip()[:512],
            "steam_reward_failure_message": str(
                raw.get(
                    "steam_reward_failure_message",
                    "签到奖励执行失败，未完成本次签到，请稍后重试。",
                )
                or "签到奖励执行失败，未完成本次签到，请稍后重试。"
            )[:500],
        }
        if profile["steam_reward_mode"] not in _SIGNIN_REWARD_MODES:
            profile["steam_reward_mode"] = "database"
        if not _SIGNIN_REWARD_OPERATION_RE.fullmatch(profile["steam_reward_operation"]):
            profile["steam_reward_operation"] = "execute_game_command"
        for reward_key in _SIGNIN_PROFILE_REWARD_KEYS:
            if reward_key not in raw or raw.get(reward_key) in {None, ""}:
                continue
            try:
                profile[reward_key] = max(
                    0, min(1_000_000, int(raw.get(reward_key)))
                )
            except (TypeError, ValueError, OverflowError):
                continue
        custom_rewards = raw.get("custom_rewards", [])
        profile["custom_rewards"] = (
            [str(item)[:500] for item in custom_rewards[:50]]
            if isinstance(custom_rewards, list)
            else []
        )
        profile["base_reward"] = profile.get("base_reward")
        result.append(profile)
        seen_ids.add(profile_id)
        seen_commands.update(alias.casefold() for alias in command_aliases)
    return result


def _lottery_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, number))


def _lottery_probability(value: Any) -> float:
    if isinstance(value, bool):
        raise PortalError("奖项概率必须是 0 到 100 之间的数字")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise PortalError("奖项概率必须是 0 到 100 之间的数字") from None
    if not math.isfinite(number) or number < 0 or number > 100:
        raise PortalError("奖项概率必须是 0 到 100 之间的数字")
    return number


def _parse_json_object_rows(value: object, label: str, maximum: int) -> list[dict[str, Any]]:
    """Parse native dictionaries or one-JSON-object-per-line form values."""

    raw_rows: list[Any]
    if isinstance(value, str):
        raw_rows = [line.strip() for line in value.splitlines() if line.strip()]
    elif isinstance(value, list):
        raw_rows = list(value)
    else:
        raise PortalError(f"{label}格式无效")
    if len(raw_rows) > maximum:
        raise PortalError(f"{label}最多可设置 {maximum} 个")
    result: list[dict[str, Any]] = []
    for raw in raw_rows:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                raise PortalError(f"{label}必须使用网页编辑器保存，不能包含无效 JSON") from None
        if not isinstance(raw, dict):
            raise PortalError(f"{label}中的每一项都必须是对象")
        result.append(raw)
    return result


def _validate_signin_profiles(value: object) -> list[dict[str, Any]]:
    """Return strict, bounded multi-sign-in definitions for Windows."""

    rows = _parse_json_object_rows(value, "签到方案", 50)
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    seen_commands: set[str] = set()
    for raw in rows:
        profile_id = str(raw.get("id", "") or "").strip()
        name = str(raw.get("name", "") or "").strip()
        raw_command = raw.get("command", raw.get("signin_command", ""))
        command_aliases = split_command_aliases(raw_command, "签到") if str(raw_command or "").strip() else ()
        command = "|".join(command_aliases)
        mode = str(raw.get("mode", raw.get("signin_mode", "basic")) or "basic").strip().lower()
        schedule = str(raw.get("schedule", raw.get("schedule_mode", "daily")) or "daily").strip().lower()
        if not _SIGNIN_PROFILE_ID_RE.fullmatch(profile_id):
            raise PortalError("签到方案 ID 只能包含字母、数字、下划线和短横线")
        if not name or len(name) > 80:
            raise PortalError("签到方案名称不能为空且不能超过 80 个字符")
        if (
            not command_aliases
        ):
            raise PortalError("签到方案命令只能填写不带斜杠和空格的命令名")
        if profile_id in seen_ids or name.casefold() in seen_names or any(
            alias.casefold() in seen_commands for alias in command_aliases
        ):
            raise PortalError("签到方案 ID、名称和命令不能重复")
        if mode not in {"basic", "steam"}:
            raise PortalError("签到方案的签到方式无效")
        if schedule not in {"daily", "weekly", "monthly", "streak", "custom"}:
            raise PortalError("签到方案的签到周期无效")
        profile: dict[str, Any] = {
            "id": profile_id,
            "name": name,
            "command": command,
            "mode": mode,
            "schedule": schedule,
            "enabled": _as_lottery_bool(raw.get("enabled"), True),
            "signin_message": str(raw.get("signin_message", "") or "")[:1000],
            "steam_reward_mode": str(
                raw.get("steam_reward_mode", raw.get("reward_mode", "database"))
                or "database"
            ).strip().lower(),
            "steam_reward_operation": str(
                raw.get("steam_reward_operation", "execute_game_command")
                or "execute_game_command"
            ).strip().lower()[:64],
            "steam_reward_command": str(
                raw.get("steam_reward_command", _DEFAULT_SIGNIN_REWARD_COMMAND)
                or _DEFAULT_SIGNIN_REWARD_COMMAND
            ).strip()[:512],
            "steam_reward_failure_message": str(
                raw.get(
                    "steam_reward_failure_message",
                    "签到奖励执行失败，未完成本次签到，请稍后重试。",
                )
                or "签到奖励执行失败，未完成本次签到，请稍后重试。"
            )[:500],
        }
        if profile["steam_reward_mode"] not in _SIGNIN_REWARD_MODES:
            profile["steam_reward_mode"] = "database"
        if not _SIGNIN_REWARD_OPERATION_RE.fullmatch(profile["steam_reward_operation"]):
            profile["steam_reward_operation"] = "execute_game_command"
        for reward_key in _SIGNIN_PROFILE_REWARD_KEYS:
            reward_value = raw.get(reward_key)
            if reward_key not in raw or reward_value is None or reward_value == "":
                continue
            reward = _lottery_int(reward_value, -1, 0, 1_000_000)
            if reward < 0:
                raise PortalError("签到方案奖励必须是 0 到 1000000 之间的整数")
            profile[reward_key] = reward
        custom_rewards = raw.get("custom_rewards", [])
        if not isinstance(custom_rewards, list) or len(custom_rewards) > 50:
            raise PortalError("签到方案的自定义奖励说明最多可设置 50 条")
        profile["custom_rewards"] = [
            str(item)[:500] for item in custom_rewards if str(item).strip()
        ]
        seen_ids.add(profile_id)
        seen_names.add(name.casefold())
        seen_commands.update(alias.casefold() for alias in command_aliases)
        result.append(profile)
    return result


def _lottery_award_for_view(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    name = str(value.get("name", "") or "").strip()[:_LOTTERY_MAX_NAME]
    if not name:
        return None
    try:
        probability = float(value.get("probability", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        probability = 0.0
    if not math.isfinite(probability):
        probability = 0.0
    probability = max(0.0, min(100.0, probability))
    award_id = str(value.get("id", "") or "").strip()
    if not _LOTTERY_AWARD_ID_RE.fullmatch(award_id):
        award_id = "award-" + uuid4().hex[:16]
    return {
        "id": award_id,
        "name": name,
        "probability": probability,
        "stock": _lottery_int(value.get("stock", 0), 0, 0, _LOTTERY_MAX_STOCK),
        "enabled": _as_lottery_bool(value.get("enabled"), True),
    }


def _lottery_config_for_view(config: object) -> dict[str, Any]:
    """Return a bounded, template-safe editable lottery definition."""

    source = config if isinstance(config, dict) else {}
    root = source.get("lottery")
    root = deepcopy(root) if isinstance(root, dict) else {}
    feature_map = source.get("features")
    feature = feature_map.get("lottery") if isinstance(feature_map, dict) else None
    feature = feature if isinstance(feature, dict) else {}
    defaults = {
        "enabled": False,
        "keyword": "抽奖",
        "max_draws_per_user": 0,
        "cooldown": 0,
        "success_message": "🎉 {name} 抽中了「{prize}」！剩余 {remaining}",
        "empty_message": "{name} 很遗憾没有抽中，再试一次吧。",
        "campaigns": [],
    }
    result: dict[str, Any] = {}
    for key, default in defaults.items():
        # Root values are canonical for awards and for current Windows clients;
        # feature values remain a compatibility fallback for old snapshots.
        result[key] = deepcopy(root.get(key, feature.get(key, default)))
    result["keyword"] = "|".join(
        split_command_aliases(result["keyword"], "抽奖")
    )
    result["enabled"] = _as_lottery_bool(result["enabled"])
    result["max_draws_per_user"] = _lottery_int(
        result["max_draws_per_user"], 0, 0, _LOTTERY_MAX_DRAWS
    )
    result["cooldown"] = _lottery_int(result["cooldown"], 0, 0, _LOTTERY_MAX_COOLDOWN)
    result["success_message"] = str(result["success_message"] or "")[:_LOTTERY_MAX_MESSAGE]
    result["empty_message"] = str(result["empty_message"] or "")[:_LOTTERY_MAX_MESSAGE]
    raw_campaigns = root.get("campaigns")
    if not raw_campaigns and feature.get("campaigns"):
        raw_campaigns = feature.get("campaigns")
    if raw_campaigns is None:
        raw_campaigns = []
    if not isinstance(raw_campaigns, list):
        raw_campaigns = []
    # Campaign rows are accepted as dictionaries or one-JSON-per-line strings
    # from the HTML list field. Keep them bounded; Windows performs the final
    # normalization and rejects malformed rows without affecting legacy mode.
    campaigns: list[Any] = []
    for row in raw_campaigns[:50]:
        if isinstance(row, dict):
            campaigns.append(deepcopy(row))
        elif isinstance(row, str) and row.strip():
            try:
                parsed = json.loads(row)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                campaigns.append(parsed)
    result["campaigns"] = campaigns
    raw_awards = root.get("awards", feature.get("awards", []))
    if not isinstance(raw_awards, list):
        raw_awards = []
    awards: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for raw in raw_awards[:_LOTTERY_MAX_AWARDS]:
        award = _lottery_award_for_view(raw)
        if award is None:
            continue
        name_key = award["name"].casefold()
        if award["id"] in seen_ids or name_key in seen_names:
            continue
        seen_ids.add(award["id"])
        seen_names.add(name_key)
        awards.append(award)
    result["awards"] = awards
    return result


def _validate_lottery_awards(raw_awards: object, *, context: str = "") -> list[dict[str, Any]]:
    """Validate one default/campaign award list with a useful error prefix."""

    prefix = f"{context}的" if context else ""
    if not isinstance(raw_awards, list):
        raise PortalError(f"{prefix}奖项清单格式无效")
    if len(raw_awards) > _LOTTERY_MAX_AWARDS:
        raise PortalError(f"{prefix}奖项最多可设置 {_LOTTERY_MAX_AWARDS} 项")
    awards: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for raw in raw_awards:
        if not isinstance(raw, dict):
            raise PortalError(f"{prefix}奖项清单包含无效项目")
        name = str(raw.get("name", "") or "").strip()
        if not name or len(name) > _LOTTERY_MAX_NAME:
            raise PortalError(f"{prefix}奖项名称不能为空且不能超过 100 个字符")
        award_id = str(raw.get("id", "") or "").strip()
        if not _LOTTERY_AWARD_ID_RE.fullmatch(award_id):
            raise PortalError(f"{prefix}奖项 ID 格式无效")
        if award_id in seen_ids or name.casefold() in seen_names:
            raise PortalError(f"{prefix}奖项 ID 和名称不能重复")
        probability = _lottery_probability(raw.get("probability", 0))
        stock = _lottery_int(raw.get("stock", 0), -1, 0, _LOTTERY_MAX_STOCK)
        if stock < 0:
            raise PortalError(f"{prefix}奖项库存必须是 0 到 1000000 之间的整数")
        seen_ids.add(award_id)
        seen_names.add(name.casefold())
        awards.append(
            {
                "id": award_id,
                "name": name,
                "probability": probability,
                "stock": stock,
                "enabled": _as_lottery_bool(raw.get("enabled"), True),
            }
        )
    total_probability = sum(
        item["probability"] for item in awards if item["enabled"]
    )
    if total_probability > 100.000001:
        raise PortalError(f"{prefix}启用奖项的概率总和不能超过 100%")
    return awards


def _validate_lottery_config(value: object) -> dict[str, Any]:
    """Strictly validate a complete editable lottery definition."""

    result = _lottery_config_for_view({"lottery": value})
    source = value if isinstance(value, dict) else {}
    awards = _validate_lottery_awards(source.get("awards", []))
    result["awards"] = awards
    raw_campaigns = _parse_json_object_rows(source.get("campaigns", []), "抽奖活动", 50)
    campaigns: list[dict[str, Any]] = []
    seen_campaign_ids: set[str] = set()
    seen_campaign_names: set[str] = set()
    # A custom activity must not shadow the default activity or another
    # activity. Windows resolves one command token to one activity only.
    seen_campaign_keywords: set[str] = {
        alias.casefold()
        for alias in split_command_aliases(result.get("keyword"), "抽奖")
    }
    for raw_campaign in raw_campaigns:
        campaign_id = str(raw_campaign.get("id", "") or "").strip()
        campaign_name = str(raw_campaign.get("name", "") or "").strip()
        campaign_keyword = "|".join(
            split_command_aliases(
                raw_campaign.get("keyword", raw_campaign.get("command", "")),
                campaign_name or "抽奖",
            )
        )
        if not _LOTTERY_AWARD_ID_RE.fullmatch(campaign_id):
            raise PortalError("抽奖活动 ID 格式无效")
        if (
            campaign_id in seen_campaign_ids
            or campaign_name.casefold() in seen_campaign_names
            or not campaign_name
            or len(campaign_name) > 100
        ):
            raise PortalError("抽奖活动 ID 或名称重复/无效")
        campaign_aliases = split_command_aliases(campaign_keyword, campaign_name or "抽奖")
        campaign_keyword_keys = {alias.casefold() for alias in campaign_aliases}
        if not campaign_aliases or len(campaign_keyword) > 256 or campaign_keyword_keys & seen_campaign_keywords:
            raise PortalError("抽奖活动指令只能填写不带空格的命令名")
        seen_campaign_ids.add(campaign_id)
        seen_campaign_names.add(campaign_name.casefold())
        seen_campaign_keywords.update(campaign_keyword_keys)
        campaign = {
            "id": campaign_id,
            "name": campaign_name,
            "keyword": campaign_keyword,
            "enabled": _as_lottery_bool(raw_campaign.get("enabled"), True),
            "max_draws_per_user": _lottery_int(
                raw_campaign.get("max_draws_per_user", 0),
                0,
                0,
                _LOTTERY_MAX_DRAWS,
            ),
            "cooldown": _lottery_int(
                raw_campaign.get("cooldown", 0),
                0,
                0,
                _LOTTERY_MAX_COOLDOWN,
            ),
            "success_message": str(
                raw_campaign.get("success_message", result["success_message"]) or ""
            )[:_LOTTERY_MAX_MESSAGE],
            "empty_message": str(
                raw_campaign.get("empty_message", result["empty_message"]) or ""
            )[:_LOTTERY_MAX_MESSAGE],
            "awards": _validate_lottery_awards(
                raw_campaign.get("awards", []), context=f"抽奖活动“{campaign_name}”"
            ),
        }
        campaigns.append(campaign)
    result["campaigns"] = campaigns
    return result


def _merge_nested_root_mirror(
    config: dict[str, Any], feature_key: str, values: dict[str, Any]
) -> None:
    """Copy web policy fields into the legacy root setting dictionary."""
    mirror = NESTED_ROOT_MIRRORS.get(feature_key)
    if mirror is None:
        return
    root_key, field_names = mirror
    current = config.get(root_key)
    settings = deepcopy(current) if isinstance(current, dict) else {}
    for field_name in field_names:
        settings[field_name] = deepcopy(values[field_name])
    config[root_key] = settings


def _safe_group_config_for_view(config: object, group_id: str) -> dict[str, Any]:
    """Return a read-only-safe group mapping for the configuration page.

    A remote snapshot is JSON supplied by an older Windows plugin.  It is not
    safe to assume that a hand-edited or historically malformed group file is
    a mapping just because current clients always upload one.  This helper is
    deliberately view-only: it never writes a repaired value back to MySQL,
    so merely opening a page cannot discard a customer's local configuration.
    """
    if not isinstance(config, dict):
        return {"group_id": str(group_id), "features": {}}

    # Do not hand the template a mutable plugin/SQLAlchemy object.  A local
    # group module returns its cached dictionary directly, and a malformed
    # historic ``features`` value (for example a list or null) used to leak
    # into the view and fail later while a feature card was rendered.
    # Shallow-copy the projection so opening the page remains read-only while
    # preserving unrelated runtime keys for the save/clone paths.
    safe = dict(config)
    safe["group_id"] = str(group_id)
    if not isinstance(safe.get("features"), dict):
        safe["features"] = {}
    return safe


def _display_feature_value(field: FieldSpec, value: Any) -> Any:
    """Normalize historical values to types that every form control accepts.

    Existing group JSON predates the portal and may contain strings such as
    ``\"false\"``, an integer where a keyword list is expected, or an object
    in a numeric field.  Rendering those values directly can either make a
    switch look enabled incorrectly or cause a Jinja/form-rendering failure.
    Use the field default for an incompatible value; this changes only the
    displayed draft and does not mutate the stored group data.
    """
    if field.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "on", "yes", "enabled"}:
                return True
            if normalized in {"0", "false", "off", "no", "disabled", ""}:
                return False
        return bool(field.default)

    if field.kind == "int":
        try:
            # ``bool`` is an ``int`` subclass, but it is never a meaningful
            # value for a duration, threshold, or other numeric web field.
            if isinstance(value, bool):
                raise ValueError
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            return field.default
        if (
            (field.minimum is not None and number < field.minimum)
            or (field.maximum is not None and number > field.maximum)
        ):
            return field.default
        return number

    if field.kind == "choice":
        candidate = value if isinstance(value, str) else field.default
        allowed = {option_value for option_value, _label in field.choices}
        return candidate if candidate in allowed else field.default

    if field.kind == "list":
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple, set)):
            rendered: list[Any] = []
            for item in value:
                if isinstance(item, (str, int, float, bool)):
                    rendered.append(item)
                elif field.key in {"profiles", "database_profiles"} and isinstance(item, dict):
                    rendered.append(deepcopy(item))
                elif field.key in {"signin_profiles", "campaigns"} and isinstance(item, dict):
                    # Multi-instance definitions use one compact JSON object
                    # per textarea line. This keeps normal list fields strict
                    # while making existing Windows dictionaries editable.
                    rendered.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            return rendered
        return []

    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return field.default


def get_feature_values(
    group_id: str,
    feature_key: str,
    db: Session | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = FEATURE_BY_KEY[feature_key]
    # ``get_group_view`` already loaded the complete projection.  Reusing it
    # avoids one database round-trip per feature card (there can be 15 cards)
    # on every remote group page.  The public/default call path still loads a
    # fresh group config for callers that request one feature directly.
    raw_config = config
    if raw_config is None:
        raw_config = (
            remote_store.get_config(_require_remote_db(db), group_id)
            if remote_data_mode()
            else group_config_module().get_group_config(group_id)
        )
    config = _safe_group_config_for_view(raw_config, group_id)
    # A projection created by current Windows clients always uses a mapping,
    # but an old hand-edited group file can contain ``features: null`` or a
    # list.  A read-only page must never turn that one legacy value into a 500.
    features = config.get("features", {}) if isinstance(config, dict) else {}
    features = features if isinstance(features, dict) else {}
    stored = features.get(feature_key, {})
    if not isinstance(stored, dict):
        stored = {}
    values = {}
    for field in spec.fields:
        value = stored.get(field.key, field.default)
        if (feature_key, field.key) in _MESSAGE_TEMPLATE_FIELDS and isinstance(value, str) and not value.strip():
            value = field.default
        values[field.key] = deepcopy(value)

    if feature_key == "entertainment" and "music_sources" not in stored:
        legacy_source = str(stored.get("music_provider", "") or "").strip().casefold()
        if legacy_source in {"qq", "qqmusic", "qq音乐"}:
            values["music_sources"] = ["qq"]
        elif legacy_source in {"163", "网易云", "网易云音乐", "netease"}:
            values["music_sources"] = ["163"]
        elif legacy_source in {"kugou", "酷狗", "酷狗音乐", "kg"}:
            values["music_sources"] = ["kugou"]
        elif legacy_source in {"soda", "汽水", "汽水音乐"}:
            values["music_sources"] = ["soda"]
    if feature_key == "entertainment":
        sources = values.get("music_sources", [])
        if not isinstance(sources, list):
            sources = [sources]
        default_source = str(values.get("music_default_source", "") or "").strip().lower()
        values["music_default_source"] = (
            default_source if default_source in sources else (sources[0] if sources else "qq")
        )

    # Commands in older releases stored several values at the config root.
    # Read those aliases so the web UI always shows the value the bot uses.
    for field_key, root_key in ROOT_MIRRORS.get(feature_key, {}).items():
        if root_key in config:
            root_value = config[root_key]
            # A legacy root mirror may still contain its default empty value
            # while the current feature payload already has the saved name.
            # Do not let that stale empty mirror hide a non-empty web value.
            if (
                feature_key == "group_config"
                and field_key == "nickname"
                and not str(root_value or "").strip()
                and str(values.get(field_key, "") or "").strip()
            ):
                continue
            values[field_key] = deepcopy(root_value)

    # Lottery settings were introduced with a root-level ``lottery`` object so
    # old Windows plugins can consume the award list without knowing the web
    # feature schema.  Prefer that canonical object for the six editable
    # scalar fields when it is present; feature-map values remain the fallback
    # for pre-lottery snapshots.
    if feature_key == "lottery":
        lottery_root = config.get("lottery")
        if isinstance(lottery_root, dict):
            for field_key in _LOTTERY_SCALAR_KEYS:
                if field_key in lottery_root:
                    values[field_key] = deepcopy(lottery_root[field_key])

    nested_mirror = NESTED_ROOT_MIRRORS.get(feature_key)
    if nested_mirror is not None:
        root_key, field_names = nested_mirror
        root_values = config.get(root_key)
        if isinstance(root_values, dict):
            for field_name in field_names:
                if field_name in root_values:
                    values[field_name] = deepcopy(root_values[field_name])

    if feature_key == "recall":
        if "self_recall_enabled" in stored:
            values["self_recall"] = bool(stored["self_recall_enabled"])
    elif feature_key == "reply":
        replies = config.get("replies", {})
        if isinstance(replies, dict) and "cooldown" in replies:
            values["cooldown"] = replies["cooldown"]
    elif feature_key == "game_servers":
        values["profiles"] = redact_game_server_profile_records(values.get("profiles", []))
    return {
        field.key: _display_feature_value(field, values.get(field.key, field.default))
        for field in spec.fields
    }


def _safe_db_rollback(db: Session | None) -> None:
    """Rollback an optional read transaction without masking the original error."""

    if db is None:
        return
    try:
        db.rollback()
    except Exception:
        logger.debug("Unable to rollback after a malformed group projection", exc_info=True)


def _safe_remote_snapshot(db: Session, group_id: str) -> Any:
    """Read a snapshot for display while isolating corrupt optional metadata."""

    try:
        return remote_store.get_snapshot(db, group_id)
    except Exception:
        # The configuration page should still expose its forms when an old
        # optional sync row is malformed.  The underlying exception is kept in
        # the service log for an administrator to repair the row/schema.
        _safe_db_rollback(db)
        logger.exception("Unable to load group sync snapshot group_id=%s", group_id)
        return None


def _remote_runtime_summaries(snapshot: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Convert the Windows runtime projection into the existing panel shape."""
    unavailable_blacklist = {
        "available": False,
        "message": "等待 Windows 同步黑名单数据；网页不会主动连接 Windows。",
        "total": 0,
        "active": 0,
        "entries": [],
    }
    unavailable_stats = {
        "available": False,
        "message": "等待 Windows 同步统计数据；网页不会主动连接 Windows。",
        "group": {"total_users": 0, "today_signins": 0, "total_points": 0, "avg_points": 0},
        "ranking": [],
        "steam_bindings": [],
    }
    raw = getattr(snapshot, "runtime_json", None) if snapshot is not None else None
    if not isinstance(raw, dict):
        return unavailable_blacklist, unavailable_stats
    blacklist = raw.get("blacklist")
    stats = raw.get("stats")
    if not isinstance(blacklist, dict):
        blacklist_summary = unavailable_blacklist
    else:
        entries = blacklist.get("entries", [])
        entries = entries if isinstance(entries, list) else []
        normalized_entries = []
        for item in entries[:200]:
            if not isinstance(item, dict):
                continue
            active = bool(item.get("active", item.get("is_active", True)))
            normalized_entries.append(
                {
                    "user_id": str(item.get("user_id", ""))[:32],
                    "active": active,
                    "ban_type": str(item.get("ban_type", ""))[:24],
                    "reason": str(item.get("reason", ""))[:240],
                    "expire_time": item.get("expire_time", 0),
                    "allowed_in_group": bool(item.get("allowed_in_group", False)),
                    "expire_text": _format_runtime_timestamp(
                        item.get("expire_time", 0), "永久"
                    ),
                }
            )
        try:
            total = max(0, int(blacklist.get("total", len(normalized_entries)) or 0))
        except (TypeError, ValueError, OverflowError):
            total = len(normalized_entries)
        try:
            active = max(0, int(blacklist.get("active", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            active = sum(1 for item in normalized_entries if item["active"])
        blacklist_summary = {
            "available": True,
            "message": "",
            "total": total,
            "active": active,
            "entries": normalized_entries,
        }
    if not isinstance(stats, dict):
        stats_summary = unavailable_stats
    else:
        raw_group = stats.get("group", {})
        raw_group = raw_group if isinstance(raw_group, dict) else {}
        group: dict[str, int] = {}
        for key in ("total_users", "today_signins", "total_points", "avg_points"):
            try:
                group[key] = max(0, int(raw_group.get(key, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                group[key] = 0
        ranking = stats.get("ranking", [])
        ranking = ranking if isinstance(ranking, list) else []
        normalized_ranking = []
        for index, item in enumerate(ranking[:100], 1):
            if not isinstance(item, dict):
                continue
            normalized_ranking.append(
                {
                    "rank": index,
                    "user_id": str(item.get("user_id", ""))[:32],
                    "user_name": str(item.get("user_name", ""))[:80],
                    "points": max(0, _safe_expire_value(item.get("points", 0))),
                    "signin_count": max(0, _safe_expire_value(item.get("signin_count", 0))),
                }
            )
        raw_bindings = stats.get("steam_bindings", [])
        raw_bindings = raw_bindings if isinstance(raw_bindings, list) else []
        steam_bindings = []
        for item in raw_bindings[:100]:
            if not isinstance(item, dict):
                continue
            steam_id = str(item.get("steam_id", "")).strip()
            user_id = str(item.get("user_id", "")).strip()
            if not steam_id.isdigit() or len(steam_id) != 17 or not user_id:
                continue
            steam_bindings.append(
                {
                    "steam_id": steam_id,
                    "user_id": user_id[:32],
                    "user_name": str(item.get("user_name", ""))[:80],
                    "bound_at": _safe_expire_value(item.get("bound_at", 0)),
                    "last_signin": str(item.get("last_signin", ""))[:16],
                    "signin_count": max(0, _safe_expire_value(item.get("signin_count", 0))),
                    "monthly_count": max(0, _safe_expire_value(item.get("monthly_count", 0))),
                    "weekly_count": max(0, _safe_expire_value(item.get("weekly_count", 0))),
                }
            )
        stats_summary = {
            "available": True,
            "message": "",
            "group": group,
            "ranking": normalized_ranking,
            "steam_bindings": steam_bindings,
        }
    return blacklist_summary, stats_summary


def _group_command_groups(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the effective command projection shown on a group page.

    Linux is the command authority. Windows receives this same projection on
    the next sync and executes the configured aliases; local runtime records
    and moderation state remain Windows-owned.
    """
    features = config.get("features", {}) if isinstance(config, dict) else {}
    features = features if isinstance(features, dict) else {}

    def detail(key: str) -> dict[str, Any]:
        value = features.get(key, {})
        return value if isinstance(value, dict) else {}

    def enabled(key: str) -> bool:
        return bool(detail(key).get("enabled", False))

    def configured_permission(key: str, default: str) -> str:
        """Resolve the same use-permission setting used by Windows."""
        local_value = detail(key).get("use_permission")
        if str(local_value or "").strip().lower() in {"member", "admin", "owner"}:
            return str(local_value).strip().lower()
        try:
            from .global_config import load_global_config

            global_config = load_global_config()
            section = global_config.get("qqadmin", {}).get(key, {})
            value = section.get("use_permission") if isinstance(section, dict) else None
            if str(value or "").strip().lower() in {"member", "admin", "owner"}:
                return str(value).strip().lower()
        except Exception:
            pass
        return default

    groups: list[dict[str, Any]] = []

    def command_display(value: object, default: str) -> str:
        return " ".join(
            f"/{alias}" for alias in split_command_aliases(value, default)
        )

    def add_group(
        title: str,
        rows: list[tuple[str, str, bool] | tuple[str, str, bool, str]],
        default_permission: str = "member",
    ) -> None:
        items = []
        for row in rows:
            command, usage, is_enabled = row[:3]
            audience = row[3] if len(row) > 3 else default_permission
            if not str(command or "").strip():
                continue
            items.append(
                {
                    "command": command,
                    "usage": usage,
                    "audience": permission_label(audience, default_permission),
                    "enabled": bool(is_enabled),
                }
            )
        if items:
            groups.append({"title": title, "items": items})

    stats = detail("stats")
    signin_rows = [
        (command_display(stats.get("signin_command"), "签到"), "直接发送", enabled("stats"), "member"),
        (command_display(stats.get("bind_command"), "绑定SteamID"), "群号 17位SteamID", enabled("stats"), "admin"),
    ]
    profiles = stats.get("signin_profiles", [])
    if isinstance(profiles, list):
        for profile in profiles[:50]:
            if not isinstance(profile, dict):
                continue
            command = command_display(profile.get("command"), "签到")
            if command:
                signin_rows.append((command, "直接发送", enabled("stats") and bool(profile.get("enabled", True)), "member"))
    add_group("统计与签到", signin_rows + [
        ("/查看SteamID绑定 /绑定列表", "直接发送", enabled("stats"), "admin"),
        ("/解绑SteamID /解绑steam /解除SteamID /删除SteamID", "QQ号或SteamID", enabled("stats"), "admin"),
        ("/签到状态", "直接发送", enabled("stats"), "member"),
        ("/签到日历", "直接发送", enabled("stats"), "member"),
        ("/重置签到", "@成员 日期", enabled("stats"), "admin"),
        ("/排行榜 /积分排行", "直接发送", enabled("stats"), "member"),
        ("/签到排行", "直接发送", enabled("stats"), "member"),
        ("/我的信息 /个人信息", "直接发送", enabled("stats"), "member"),
        ("/群统计", "直接发送", enabled("stats"), "member"),
        ("/邀请统计 /我的邀请 /查看邀请排行 /查看邀请记录", "可选 QQ号", enabled("stats"), "member"),
    ], configured_permission("stats", "member"))

    scum = detail("scum")
    add_group("SCUM 查询", [
        (command_display(scum.get("online_command"), "查询在线"), "直接发送", enabled("scum"), "member"),
        (command_display(scum.get("keyword_command"), "查询"), "关键词", enabled("scum"), "member"),
        ("/绑定id /id绑定", "服务器ID", enabled("scum"), "admin"),
        ("/解绑", "直接发送", enabled("scum"), "admin"),
    ])

    add_group("群成员资料", [
        ("/群资料状态 /群名片状态 /群称号状态", "直接发送", enabled("group_config"), "admin"),
        ("/设置群员名称", "QQ号或@成员 名称", enabled("group_config"), "admin"),
        ("/设置群名片前缀", "前缀 [包含管理员|不修改管理员]", enabled("group_config"), "admin"),
        ("/自动授予称号", "称号", enabled("group_config"), "admin"),
        ("/开启自动称号 /关闭自动称号 /查看称号设置 /恢复默认称号设置", "直接发送", enabled("group_config"), "admin"),
        ("/授予称号", "QQ号或@成员 称号", enabled("group_config"), "owner"),
        ("/清除群员称号 /初始化群员称号", "QQ号或@成员", enabled("group_config"), "owner"),
    ], configured_permission("group_config", "admin"))

    entertainment = detail("entertainment")
    like_command = entertainment.get("like_command", "赞我")
    if str(like_command or "").strip() == "点赞":
        like_command = "赞我"
    entertainment_rows = [
        (command_display(entertainment.get("time_command"), "当前时间"), "直接发送", enabled("entertainment") and bool(entertainment.get("time_enabled", True))),
        (command_display(like_command, "赞我"), "QQ号|我|本人|@成员", enabled("entertainment") and bool(entertainment.get("like_enabled", True))),
        (command_display(entertainment.get("music_command"), "点歌"), "歌名", enabled("entertainment") and bool(entertainment.get("music_enabled", True))),
        (command_display(entertainment.get("video_command"), "搜索视频"), "关键词", enabled("entertainment") and bool(entertainment.get("video_enabled", True))),
        (command_display(entertainment.get("dice_command"), "骰子"), "[面数]", enabled("entertainment") and bool(entertainment.get("dice_enabled", True))),
        (command_display(entertainment.get("rps_command"), "猜拳"), "石头|剪刀|布", enabled("entertainment") and bool(entertainment.get("rps_enabled", True))),
        (command_display(entertainment.get("random_command"), "随机"), "选项1 选项2", enabled("entertainment") and bool(entertainment.get("random_enabled", True))),
        (command_display(entertainment.get("coin_command"), "抛硬币"), "直接发送", enabled("entertainment") and bool(entertainment.get("coin_enabled", True))),
        (command_display(entertainment.get("fortune_command"), "今日运势"), "直接发送", enabled("entertainment") and bool(entertainment.get("fortune_enabled", True))),
        (command_display(entertainment.get("luck_command"), "幸运数字"), "直接发送", enabled("entertainment") and bool(entertainment.get("luck_enabled", True))),
        (command_display(entertainment.get("answer_command"), "答案之书"), "问题", enabled("entertainment") and bool(entertainment.get("answer_enabled", True))),
        ("/选歌", "序号", enabled("entertainment") and bool(entertainment.get("music_enabled", True))),
        ("/娱乐 /娱乐帮助 /小游戏", "直接发送", enabled("entertainment")),
        ("/开启娱乐 /关闭娱乐", "直接发送", enabled("entertainment"), "admin"),
    ]
    add_group("娱乐", entertainment_rows, configured_permission("entertainment", "member"))

    game = detail("game_commands")
    game_rows: list[tuple[str, str, bool]] = []
    try:
        game_module = _plugin_module("game_commands")
        commands = game_module.normalize_game_commands(game.get("commands", []))
    except Exception:
        commands = []
    for command in commands:
        game_rows.append((f"#{command['trigger']}", "按游戏指令模板", enabled("game_commands") and bool(command.get("enabled", True)), str(command.get("permission", "member"))))
    add_group("游戏指令", game_rows)

    add_group("入群审核", [
        ("/入群审核", "直接发送", enabled("audit")),
        ("/同意", "审批码", enabled("audit")),
        ("/拒绝", "审批码 [理由]", enabled("audit")),
        ("/查看验证设置", "直接发送", enabled("audit")),
        ("/回答验证", "验证ID 答案", enabled("audit"), "member"),
    ], configured_permission("audit", "admin"))
    add_group("黑名单", [
        ("/黑名单系统", "直接发送", enabled("blacklist")),
        ("/拉黑 /拉黑@", "@成员 [天数] [理由]", enabled("blacklist")),
        ("/删黑", "用户ID", enabled("blacklist")),
        ("/查看黑名单 /清空黑名单", "直接发送", enabled("blacklist")),
        ("/开启黑名单 /关闭黑名单", "直接发送", enabled("blacklist")),
        ("/设置退群入黑 /设置拉黑撤回 /设置自动踢出", "开|关", enabled("blacklist")),
        ("/全局拉黑 /全局删黑 /查看全局黑名单 /清空全局黑名单", "用户ID [天数]", enabled("blacklist")),
    ], configured_permission("blacklist", "admin"))
    add_group("禁言与踢出", [
        ("/禁言", "@成员 [时长] [理由]", enabled("mute")),
        ("/解除禁言 /解禁", "@成员", enabled("mute")),
        ("/全体禁言 /全体解禁", "直接发送", enabled("mute")),
        ("/设置禁言 /查看禁言", "参数", enabled("mute")),
        ("/设置艾特禁言 /查看艾特禁言 /清空艾特禁言", "@成员 [秒数]", enabled("mute")),
        ("/踢出", "@成员 [理由]", enabled("kick"), configured_permission("kick", "admin")),
    ], configured_permission("mute", "admin"))
    add_group("撤回与消息", [
        ("/撤回系统", "直接发送", enabled("recall")),
        ("/撤回", "[@成员] 条数", enabled("recall")),
        ("/开启撤回 /关闭撤回", "直接发送", enabled("recall")),
        ("/查看撤回关键词", "直接发送", enabled("recall")),
    ], configured_permission("recall", "admin"))
    add_group("欢迎与回复", [
        ("/入群欢迎 /开启欢迎 /关闭欢迎", "直接发送", enabled("welcome"), configured_permission("welcome", "member")),
        ("/设置欢迎语 /设置退群语 /设置欢迎延迟 /设置艾特新人", "内容|秒数|开|关", enabled("welcome"), configured_permission("welcome", "admin")),
        ("/自动回复 /添加回复 /删除回复 /查看回复列表 /清空回复", "问题 [答案]", enabled("reply"), configured_permission("reply", "admin")),
        ("/用户专属回复 /添加专属回复 /删除专属回复 /查看专属回复列表", "@成员 内容", enabled("player_reply"), configured_permission("player_reply", "admin")),
    ], configured_permission("reply", "admin"))
    add_group("过滤与定时", [
        ("/关键词过滤 /查看过滤词列表", "直接发送", enabled("keyword_filter"), configured_permission("keyword_filter", "admin")),
        ("/定时消息 /添加定时消息 /查看定时消息", "名称 内容", enabled("timed_message"), configured_permission("timed_message", "admin")),
        ("/添加定时音乐 /添加定时禁言 /添加定时解禁", "名称 分 时 日 月 周 [内容]", enabled("timed_message"), configured_permission("timed_message", "admin")),
    ], configured_permission("timed_message", "admin"))
    group_config = detail("group_config")
    help_command = command_display(group_config.get("help_command"), "群管功能")
    help_permission = str(group_config.get("help_permission", "member") or "member")
    add_group("群设置与工具", [
        (help_command, "[分类] [功能]", enabled("group_config"), help_permission),
        ("/群设置 /分群设置", "直接发送", enabled("group_config")),
        ("/文转图 /图转文", "文本", enabled("text_to_image"), configured_permission("text_to_image", "admin")),
        ("/添加分群管理员 /删除分群管理员 /查看分群管理员", "QQ号", enabled("sub_admin"), configured_permission("sub_admin", "admin")),
        ("/启用功能 /禁用功能 /查看功能状态 /设置功能", "功能名 [配置项] [值]", enabled("group_config")),
    ], configured_permission("group_config", "admin"))
    add_group("抽奖", [
        (command_display(detail("lottery").get("keyword"), "抽奖"), "直接发送", enabled("lottery"), "member"),
        ("/开启抽奖 /关闭抽奖 /查看奖项 /抽奖状态", "直接发送", enabled("lottery"), "admin"),
        ("/添加奖项 /修改奖项 /删除奖项 /查看抽奖记录", "名称 概率 [库存]", enabled("lottery"), "admin"),
    ], configured_permission("lottery", "member"))

    # The summary is intentionally read-only, but each category now points to
    # the existing editable feature form. This makes command names discoverable
    # and gives operators one clear place to change aliases; the saved feature
    # projection is already synchronized to Windows and used by dispatch.
    section_ids = {
        "统计与签到": "command-editor-stats",
        "SCUM 查询": "command-editor-scum",
        "娱乐": "command-editor-entertainment",
        "游戏指令": "command-editor-game_commands",
        "抽奖": "command-editor-lottery",
        "群设置与工具": "command-editor-group_config",
        "群成员资料": "command-editor-group_config",
    }
    for group in groups:
        group["edit_section"] = section_ids.get(group.get("title", ""), "")
    return groups


def _group_command_editors(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Build command-only editor rows for the group page.

    The regular feature forms intentionally expose switches, policies and
    messages.  Command aliases have a separate, narrow editor so changing a
    command name cannot accidentally change unrelated feature settings.
    """
    features = config.get("features", {}) if isinstance(config, dict) else {}
    features = features if isinstance(features, dict) else {}
    definitions = (
        ("stats", "统计与签到", ("signin_command", "bind_command")),
        ("scum", "SCUM 查询", ("online_command", "keyword_command")),
        (
            "entertainment",
            "娱乐",
            (
                "time_command", "like_command", "music_command", "video_command",
                "dice_command", "rps_command", "random_command", "coin_command",
                "fortune_command", "luck_command", "answer_command",
            ),
        ),
        ("lottery", "抽奖", ("keyword",)),
        ("group_config", "群设置与工具", ("help_command",)),
    )
    editors: list[dict[str, Any]] = []
    for feature_key, title, command_keys in definitions:
        values = features.get(feature_key, {})
        values = values if isinstance(values, dict) else {}
        spec = FEATURE_BY_KEY.get(feature_key)
        fields_by_key = {
            field.key: field for field in (spec.fields if spec else ())
        }
        fields = []
        for key in command_keys:
            field = fields_by_key.get(key)
            if field is None:
                continue
            fields.append(
                {
                    "key": key,
                    "label": field.label,
                    "value": str(values.get(key, field.default) or ""),
                    "feature_key": feature_key,
                }
            )
        editor = {"feature_key": feature_key, "title": title, "fields": fields, "profiles": [], "campaigns": [], "game_commands": []}
        if feature_key == "stats":
            profiles = values.get("signin_profiles", [])
            editor["profiles"] = [
                {
                    "id": str(profile.get("id", "")),
                    "name": str(profile.get("name", "签到方案")),
                    "command": str(profile.get("command", "签到游戏")),
                }
                for profile in profiles
                if isinstance(profile, dict) and str(profile.get("id", "")).strip()
            ]
        if feature_key == "lottery":
            campaigns = values.get("campaigns", [])
            editor["campaigns"] = [
                {
                    "id": str(campaign.get("id", "")),
                    "name": str(campaign.get("name", "抽奖活动")),
                    "command": str(campaign.get("keyword", campaign.get("command", "抽奖"))),
                }
                for campaign in campaigns
                if isinstance(campaign, dict) and str(campaign.get("id", "")).strip()
            ]
        editors.append(editor)

    game_values = features.get("game_commands", {})
    game_values = game_values if isinstance(game_values, dict) else {}
    game_commands = game_values.get("commands", [])
    normalized_game_commands: list[dict[str, Any]] = []
    try:
        module = _plugin_module("game_commands")
        game_commands = module.normalize_game_commands(game_commands)
    except Exception:
        game_commands = game_commands if isinstance(game_commands, list) else []
    for index, command in enumerate(game_commands):
        if not isinstance(command, dict):
            continue
        normalized_game_commands.append(
            {
                "index": index,
                "name": str(command.get("name", command.get("trigger", "游戏指令"))),
                "command": str(command.get("trigger", "")),
            }
        )
    editors.append(
        {
            "feature_key": "game_commands",
            "title": "游戏指令",
            "fields": [],
            "profiles": [],
            "campaigns": [],
            "game_commands": normalized_game_commands,
        }
    )
    return editors


def _runtime_panel_defaults() -> dict[str, Any]:
    """Return template-safe defaults for read-only Windows runtime panels."""

    return {
        "audit": {"available": False, "pending_count": 0, "total_processed": 0, "approved": 0, "rejected": 0, "active_challenges": 0, "pending": [], "recent": []},
        "kick": {"available": False, "total_kicks": 0, "unique_users": 0, "today_kicks": 0, "week_kicks": 0, "records": []},
        "mute": {"available": False, "total_muted": 0, "currently_muted": 0, "total_mute_times": 0, "active": []},
        "recall": {"available": False, "total_recalls": 0, "keywords_count": 0, "regex_count": 0, "whitelist_count": 0, "self_recall_enabled": False, "recent": []},
        "blacklist": {"available": False, "total": 0, "active": 0, "entries": []},
        "global_blacklist": {"available": False, "total": 0, "active": 0, "permanent": 0, "temporary": 0, "inactive": 0, "entries": []},
        "group_activity": {"available": False, "window_days": 30, "message_count": 0, "active_members": 0, "last_message_at": 0, "inactive_seconds": 0},
        "message_cache": {"available": False, "count": 0, "records": []},
        "errors": {"available": False, "count": 0, "recent": []},
        "operations": {"available": False, "count": 0, "recent": [], "categories": {}},
        "members": {"available": False, "count": 0, "valid_count": 0, "updated_at": 0, "message": "", "levels_pending": False, "upload_pending": False, "partial": False, "expected_count": 0, "members": []},
    }


def _normalize_runtime_panels(
    raw: Any,
    *,
    include_members: bool = True,
) -> dict[str, Any]:
    """Normalize optional runtime panels without allowing malformed data to 500."""

    panels = _runtime_panel_defaults()
    # MySQL JSON columns normally deserialize to a dict, but older installs
    # created ``runtime_json`` as TEXT and return the same payload as a JSON
    # string. Decode that legacy representation so a successful Windows upload
    # is rendered instead of falling back to “等待 Windows 同步成员”.
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return panels
        raw = decoded
    if not isinstance(raw, dict):
        return panels
    for name, default in panels.items():
        # Member directories can contain thousands of rows. Group pages and
        # the five-second runtime poll do not need them in the main payload;
        # they are loaded through the dedicated member-directory endpoint.
        if name == "members" and not include_members:
            continue
        value = raw.get(name)
        if not isinstance(value, dict):
            continue
        current = dict(default)
        current["available"] = True
        for key in default:
            if key not in value:
                continue
            # ``mute.active`` is a list of members. Blacklist ``active`` is a
            # numeric counter, so only normalize the list-shaped variant.
            if key in {"pending", "recent", "records", "entries", "members"} or (
                key == "active" and name == "mute"
            ):
                current[key] = _normalize_runtime_rows(name, value.get(key))
            elif key == "categories" and name == "operations":
                raw_categories = value.get(key, {})
                if isinstance(raw_categories, dict):
                    current[key] = {
                        str(category)[:32]: _normalize_runtime_rows(
                            name, rows
                        )
                        for category, rows in raw_categories.items()
                        if isinstance(rows, list)
                    }
            elif key == "message":
                current[key] = str(value.get(key, "") or "")[:500]
            elif key == "valid_count":
                try:
                    current[key] = max(0, min(10000, int(value.get(key, 0) or 0)))
                except (TypeError, ValueError, OverflowError):
                    current[key] = 0
            elif key == "self_recall_enabled":
                current[key] = bool(value.get(key))
            elif key in {"levels_pending", "upload_pending"}:
                current[key] = bool(value.get(key))
            elif key == "partial":
                current[key] = bool(value.get(key))
            else:
                try:
                    current[key] = max(0, int(value.get(key, 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    current[key] = 0
        if name == "members":
            # Older Windows clients sent only the member rows and omitted
            # ``valid_count``.  Derive it from the validated rows so the
            # selector and the summary do not report zero members.
            member_rows = current.get("members", [])
            if isinstance(member_rows, list):
                if not current.get("valid_count"):
                    current["valid_count"] = len(member_rows)
                if not current.get("count"):
                    current["count"] = len(member_rows)
        panels[name] = current
    return panels


def _normalize_runtime_rows(panel: str, rows: object) -> list[dict[str, Any]]:
    """Return safe, readable rows from the Windows runtime projection.

    Runtime data is never written back from the portal.  This is only a view
    boundary: it guards group pages against historic malformed records and
    adds a human-readable Beijing-time field for the timestamps Windows sends.
    """

    if not isinstance(rows, list):
        return []
    timestamp_fields = {
        "audit": ("timestamp", "timestamp_text"),
        "kick": ("last_kicked_at", "last_kicked_text"),
        "mute": ("expire_time", "expire_text"),
        "recall": ("recalled_at", "recalled_at_text"),
        "blacklist": ("expire_time", "expire_time_text"),
        "global_blacklist": ("added_at", "added_at_text"),
        "message_cache": ("timestamp", "timestamp_text"),
        "errors": ("timestamp", "timestamp_text"),
        "operations": ("timestamp", "timestamp_text"),
    }
    result: list[dict[str, Any]] = []
    # Member directories and message history are user-facing data panels. Keep
    # their bounded records available for large groups and member inspection;
    # compact audit/operation panels retain the smaller limit.
    row_limit = 10000 if panel in {"members", "message_cache"} else 100
    for raw in rows[:row_limit]:
        if not isinstance(raw, dict):
            continue
        # Older Windows snapshots may contain a global record already allowed
        # into this group. Hide it at the Linux rendering boundary.
        if panel == "global_blacklist" and raw.get("allowed_in_group"):
            continue
        row: dict[str, Any] = {}
        for raw_key, raw_value in raw.items():
            key = str(raw_key)[:64]
            if isinstance(raw_value, str):
                value_limit = 1200 if panel == "message_cache" and key in {
                    "content", "recall_reason", "user_name"
                } else 500
                row[key] = raw_value[:value_limit]
            elif (
                panel == "message_cache"
                and key == "media_urls"
                and isinstance(raw_value, list)
            ):
                row[key] = [
                    str(item or "")[:1024]
                    for item in raw_value[:8]
                    if str(item or "").strip()
                ]
            elif (
                panel == "message_cache"
                and key == "media_items"
                and isinstance(raw_value, list)
            ):
                # Media must remain structured here.  Older projections only
                # exposed image URLs, which made audio/video/file records look
                # like plain placeholder text in the member history dialog.
                media_items: list[dict[str, str]] = []
                for item in raw_value[:8]:
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("url", "") or "").strip()
                    if not (
                        url.startswith("https://")
                        or (url.startswith("data:image/") and ";base64," in url and len(url) <= 700000)
                    ):
                        continue
                    media_items.append(
                        {
                            "kind": str(item.get("kind", "file") or "file")[:16],
                            "url": url[:700000],
                            "name": str(item.get("name", "") or "")[:160],
                        }
                    )
                row[key] = media_items
            elif isinstance(raw_value, (int, float, bool)) or raw_value is None:
                row[key] = raw_value
        timestamp = timestamp_fields.get(panel)
        if panel == "audit" and "requested_at" in row and "timestamp" not in row:
            timestamp = ("requested_at", "requested_at_text")
        if timestamp is not None:
            source_key, display_key = timestamp
            row[display_key] = _format_runtime_timestamp(
                row.get(source_key, 0), "长期" if panel == "blacklist" else ("永久" if panel == "mute" else "-")
            )
        if panel == "global_blacklist":
            row["expire_time_text"] = _format_runtime_timestamp(
                row.get("expire_time", 0), "长期"
            )
        if panel == "members":
            row["join_time_text"] = _format_runtime_timestamp(
                row.get("join_time", 0), "-"
            )
            row["last_message_at_text"] = _format_runtime_timestamp(
                row.get("last_message_at", 0), "-"
            )
            row["last_sent_time_text"] = _format_runtime_timestamp(
                row.get("last_sent_time", 0), "-"
            )
        result.append(row)
    # The Windows side normally sends these feeds newest-first, but sorting at
    # the Linux rendering boundary also handles older snapshots and database
    # rows written by previous plugin versions.
    sort_fields = {
        "audit": ("timestamp", "requested_at"),
        "kick": ("last_kicked_at", "timestamp"),
        "mute": ("expire_time", "timestamp"),
        "recall": ("recalled_at", "timestamp"),
        "blacklist": ("added_at", "expire_time", "timestamp"),
        "global_blacklist": ("added_at", "timestamp"),
        "errors": ("timestamp",),
        "operations": ("timestamp",),
        "message_cache": ("timestamp",),
    }
    fields = sort_fields.get(panel)
    if fields:
        result.sort(
            key=lambda item: max(
                (_safe_expire_value(item.get(field), 0) for field in fields),
                default=0,
            ),
            reverse=True,
        )
    return result


def _local_runtime_panels(group_id: str) -> dict[str, Any]:
    """Read the same bounded summaries directly when the portal runs locally."""

    try:
        from . import runtime_summary  # optional local helper, if installed
        return _normalize_runtime_panels(runtime_summary.build(group_id))
    except Exception:
        # Reuse the Windows summary builder in local mode.  It is import-safe
        # in AstrBot and all of its module calls are already best-effort.
        try:
            module = plugin_module("web_sync")
            return _normalize_runtime_panels(module.build_runtime_summary(group_id))
        except Exception:
            logger.debug("Unable to load local runtime panels group_id=%s", group_id, exc_info=True)
            return _runtime_panel_defaults()


def _independent_action_features(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose catalogue actions that intentionally have no edit form.

    Sign-in/status/ranking commands share the ``stats`` switch, but they are
    separate user-facing functions.  Keeping them read-only here avoids
    inventing duplicate settings while making their effective state visible.
    """

    features = config.get("features", {}) if isinstance(config, dict) else {}
    features = features if isinstance(features, dict) else {}
    stats = features.get("stats", {})
    stats = stats if isinstance(stats, dict) else {}
    stats_enabled = bool(stats.get("enabled", False))
    editable_keys = set(FEATURE_BY_KEY)
    result: list[dict[str, Any]] = []
    for key, category in CATALOG_FEATURE_BY_KEY.items():
        if key in editable_keys:
            continue
        result.append(
            {
                "key": key,
                "label": category.label,
                "summary": category.summary,
                "enabled": stats_enabled,
                "section_id": "stats-data",
                "readonly": True,
            }
        )
    return result


def _safe_sync_state(snapshot: Any, offline_after_seconds: int) -> dict[str, Any]:
    """Return a complete template-safe sync state for legacy snapshots."""

    try:
        return remote_store.sync_state(snapshot, offline_after_seconds)
    except Exception:
        # ``revision`` and ``last_seen_at`` were nullable in an early schema;
        # converting them inside ``remote_store.sync_state`` can therefore
        # fail before the group forms are rendered.  Keep the state conservative
        # (offline/pending) and let the next Windows exchange repair it.
        revision = _safe_expire_value(getattr(snapshot, "revision", 0))
        applied_revision = _safe_expire_value(
            getattr(snapshot, "applied_revision", 0)
        )
        logger.exception("Unable to normalize group sync state")
        view = {
            "online": False,
            "pending": revision > applied_revision,
            "status": "unknown",
            "connection_state": "offline",
            "badge": "同步状态待修复",
            "text": "Windows 同步状态暂时无法读取",
            "display_text": "Windows 同步状态暂时无法读取",
            "detail": "分群配置仍可编辑；Windows 下次联网同步后会重新读取状态。",
            "revision": revision,
            "applied_revision": applied_revision,
            "last_seen_at": None,
            "last_seen_text": "尚未收到 Windows 同步请求",
            "saved_at": "",
            "saved_by": 0,
            "refresh_requested": bool(getattr(snapshot, "refresh_requested", False)),
        }
        return view


def _feature_config_summary(feature_key: str, values: dict[str, Any]) -> str:
    """Build a safe one-line summary for the group feature overview."""
    values = values if isinstance(values, dict) else {}
    enabled = "已开启" if values.get("enabled") else "未开启"

    if feature_key == "audit":
        return f"{enabled} · {values.get('approval_mode', 'math')} · 超时 {values.get('verification_timeout', 300)} 秒"
    if feature_key == "blacklist":
        return f"{enabled} · 自动踢出 {'开' if values.get('auto_kick', True) else '关'} · 每日上限 {values.get('max_auto_kick_per_day', 10)}"
    if feature_key == "mute":
        return f"{enabled} · 等级 {values.get('level', 5)} · {values.get('duration', 300)} 秒"
    if feature_key == "kick":
        return f"{enabled} · 自动拉黑 {'开' if values.get('auto_blacklist', False) else '关'} · 阈值 {values.get('limit', 3)}"
    if feature_key == "recall":
        return f"{enabled} · {values.get('mode', 'keyword')} · 关键词 {len(values.get('keywords', []) or [])} 个"
    if feature_key == "message_cache":
        return f"{enabled} · 保留 {values.get('retention_days', 7)} 天 · 图片归档 {'开' if values.get('archive_images', True) else '关'}"
    if feature_key == "welcome":
        return f"{enabled} · 自动欢迎 {'开' if values.get('auto_welcome', False) else '关'} · 关键词 {len(values.get('keywords', []) or [])} 个"
    if feature_key == "reply":
        return f"{enabled} · 冷却 {values.get('cooldown', 0)} 秒 · 规则在下方维护"
    if feature_key == "player_reply":
        return f"{enabled} · 默认 @ {'开' if values.get('default_at_user', True) else '关'} · 成员规则在下方维护"
    if feature_key == "stats":
        return (
            f"{enabled} · /{str(values.get('signin_command', '签到')).lstrip('/')}"
            f" · 绑定 /{str(values.get('bind_command', '绑定SteamID')).lstrip('/')}"
            f" · {values.get('signin_mode', 'basic')} · 基础奖励 {values.get('signin_base_reward', 10)}"
        )
    if feature_key == "lottery":
        return f"{enabled} · 命令 /{str(values.get('keyword', '抽奖')).lstrip('/')} · 每人上限 {values.get('max_draws_per_user', 0) or '不限'}"
    if feature_key == "entertainment":
        commands = "/".join(
            str(values.get(key, default)).strip().lstrip("/")
            for key, default in (
                ("time_command", "当前时间"),
                ("dice_command", "骰子"),
                ("rps_command", "猜拳"),
                ("random_command", "随机"),
                ("like_command", "赞我"),
                ("music_command", "点歌"),
                ("video_command", "搜索视频"),
            )
        )
        daily_limit = values.get("like_daily_limit", 20)
        raw_sources = values.get("music_sources", ["qq", "163"])
        sources = raw_sources if isinstance(raw_sources, list) else [raw_sources]
        source_labels = {
            "qq": "QQ音乐",
            "163": "网易云音乐",
            "kugou": "酷狗音乐",
            "soda": "汽水音乐",
        }
        default_source = str(values.get("music_default_source", "") or "").strip()
        music_source = source_labels.get(default_source, "未选择")
        selection_timeout = values.get("music_selection_timeout", 600)
        return (
            f"{enabled} · /{commands} · "
            f"点歌默认 {music_source} · 候选保留 {selection_timeout} 秒 · 每日点赞 {daily_limit} 次 · "
            f"{'、'.join('开' if values.get(key, True) else '关' for key in ('time_enabled', 'dice_enabled', 'rps_enabled', 'random_enabled', 'like_enabled', 'music_enabled', 'video_enabled'))}"
        )
    if feature_key == "scum":
        return (
            f"{enabled} · /{str(values.get('keyword_command', '查询')).lstrip('/')}"
            f" · /{str(values.get('online_command', '查询在线')).lstrip('/')}"
        )
    if feature_key == "keyword_filter":
        return f"{enabled} · 动作 {values.get('filter_action', 'warn')} · 关键词 {len(values.get('filter_keywords', []) or [])} 个"
    if feature_key == "timed_message":
        return f"{enabled} · 任务在下方维护"
    if feature_key == "game_commands":
        return f"{enabled} · {len(values.get('commands', []) or [])} 个游戏指令模板"
    if feature_key == "game_servers":
        profiles = values.get("profiles", []) or []
        active_id = str(values.get("active_profile_id", "") or "").strip()
        active_name = next(
            (
                str(item.get("name", item.get("id", "")) or "")
                for item in profiles
                if isinstance(item, dict) and str(item.get("id", "") or "") == active_id
            ),
            "自动选择" if not active_id else active_id,
        )
        return f"{enabled} · {len(profiles)} 个服务器档案 · 当前 {active_name}"
    if feature_key == "sub_admin":
        return f"{enabled} · 成员权限在下方维护"
    if feature_key == "group_config":
        help_aliases = "/".join(
            alias for alias in split_command_aliases(values.get("help_command"), "群管功能")
        )
        return (
            f"{enabled} · 目录 /{help_aliases} · 用户 {permission_label(values.get('help_permission', 'member'))}"
            f" · 标签 {len(values.get('tags', []) or [])} 个 · 时区 {values.get('timezone', 'Asia/Shanghai')}"
        )
    if feature_key == "text_to_image":
        return f"{enabled} · 字号 {values.get('font_size', 16)} · 修改权限 {values.get('modify_permission', 'admin')}"
    return enabled


def _default_game_commands() -> list[dict[str, Any]]:
    """Read the canonical Windows starter templates for portal resets."""
    try:
        module = _plugin_module("qqadmin.game_commands")
        factory = getattr(module, "get_default_game_commands", None)
        result = factory() if callable(factory) else []
        return result if isinstance(result, list) else []
    except Exception:
        logger.exception("Unable to load default game command templates")
        return []


def reset_game_configuration(
    group_id: str,
    db: Session | None = None,
    updated_by: int | None = None,
) -> dict[str, Any]:
    """Restore game starter settings without deleting runtime/player data.

    Steam bindings, sign-in history, database paths and game authorization are
    deliberately retained. Only the game-facing policy is
    reset, so a group can immediately see the standard command templates.
    """
    default_commands = _default_game_commands()

    def update_features(existing: object) -> dict[str, Any]:
        features = deepcopy(existing) if isinstance(existing, dict) else {}
        stats = deepcopy(features.get("stats")) if isinstance(features.get("stats"), dict) else {}
        servers = deepcopy(features.get("game_servers")) if isinstance(features.get("game_servers"), dict) else {}
        profiles = servers.get("profiles", [])
        if not isinstance(profiles, list):
            profiles = []
        stats.update(
            {
                "enabled": True,
                "signin_mode": "steam",
                "schedule_mode": "daily",
                "signin_command": "签到",
                "bind_command": "绑定SteamID",
                "signin_profiles": default_steam_signin_profiles(),
                "bind_failure_message": "SteamID 绑定失败：{message}",
                "steam_reward_mode": "websocket",
                "steam_reward_operation": "execute_game_command",
                "steam_reward_command": "#ChangeCurrencyBalance Normal {reward} {steam_id}",
                "steam_reward_failure_message": "签到奖励执行失败，未完成本次签到，请稍后重试。",
                "database_sync": False,
                "signin_base_reward": 10,
                "signin_reward_3_days": 30,
                "signin_reward_7_days": 100,
                "signin_reward_14_days": 200,
                "signin_reward_30_days": 500,
            }
        )
        if not isinstance(stats.get("database_profiles"), list) or not stats.get("database_profiles"):
            stats["database_profiles"] = default_database_profiles()
        servers["profiles"] = profiles
        servers["enabled"] = bool(profiles)
        features["stats"] = stats
        features["game_commands"] = {
            "enabled": True,
            "execution_mode": "local",
            "success_message": "游戏指令执行成功：{message}",
            "failure_message": "游戏指令执行失败：{reason}",
            "commands": deepcopy(default_commands),
        }
        features["game_servers"] = servers
        return features

    if remote_data_mode():
        def mutate(config: dict[str, Any]) -> dict[str, Any]:
            config["features"] = update_features(config.get("features"))
            return config

        return remote_store.mutate_web_config(
            _require_remote_db(db), group_id, updated_by, mutate
        )

    config_api = group_config_module()
    return config_api.update_group_data(group_id, "features", update_features)


def apply_game_server_profile_action(
    group_id: str,
    operation: str,
    *,
    profile_id: str = "",
    name: str = "",
    host: str = "",
    port: Any = 28016,
    api_key: str = "",
    executor_type: str = "plugin_websocket",
    websocket_url: str = "",
    timeout_seconds: Any = 10,
    profile_enabled: Any = True,
    integration_profile_id: str = "",
    enabled: Any = True,
    active_profile_id: str = "",
    db: Session | None = None,
    updated_by: int | None = None,
) -> dict[str, Any]:
    """Maintain human-friendly per-group plugin WebSocket rows."""

    operation = str(operation or "").strip().lower()
    if operation not in {"save", "delete", "activate", "settings"}:
        raise PortalError("未知的游戏服务器操作")

    def as_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() not in {"", "0", "false", "off", "no"}

    def update_features(existing: object) -> dict[str, Any]:
        features = deepcopy(existing) if isinstance(existing, dict) else {}
        servers = deepcopy(features.get("game_servers")) if isinstance(features.get("game_servers"), dict) else {}
        previous = servers.get("profiles", [])
        if not isinstance(previous, list):
            previous = []
        profiles = [deepcopy(item) for item in previous if isinstance(item, dict)]
        current_active = str(servers.get("active_profile_id", "") or "").strip()

        if operation == "save":
            normalized_id = str(profile_id or "").strip()
            if not normalized_id:
                normalized_id = "server-" + uuid4().hex[:12]
            previous_item = next(
                (
                    item for item in profiles
                    if str(item.get("id", "") or "").strip() == normalized_id
                ),
                {},
            )
            # A saved profile already has its API Key encrypted at rest.  The
            # edit form intentionally leaves that secret blank, so only a new
            # profile must supply the complete documented WebSocket URL.
            if (
                not previous_item
                and str(executor_type or "plugin_websocket").strip().lower()
                == "plugin_websocket"
                and not str(websocket_url or "").strip()
            ):
                raise PortalError("新建游戏服务器必须填写开发文档中的完整 WebSocket 链接")
            try:
                candidate = normalize_game_server_profile_descriptor(
                    {
                        "id": normalized_id,
                        "name": name or normalized_id,
                        "host": host or previous_item.get("host", ""),
                        "port": port or previous_item.get("port", 28016),
                        "type": executor_type,
                        "api_key": api_key,
                        "websocket_url": websocket_url,
                        "timeout_seconds": timeout_seconds,
                        "integration_profile_id": integration_profile_id,
                        "enabled": as_bool(profile_enabled, True),
                    }
                )
            except ValueError as exc:
                raise PortalError(str(exc)) from exc
            found = False
            for index, item in enumerate(profiles):
                if str(item.get("id", "") or "").strip() == normalized_id:
                    profiles[index] = candidate
                    found = True
                    break
            if not found:
                profiles.append(candidate)
            if not current_active:
                current_active = normalized_id
            servers["enabled"] = True
        elif operation == "delete":
            target = str(profile_id or "").strip()
            if not target:
                raise PortalError("请选择要删除的游戏服务器")
            if not any(str(item.get("id", "") or "").strip() == target for item in profiles):
                raise PortalError("游戏服务器不存在，可能已被删除")
            profiles = [
                item for item in profiles
                if str(item.get("id", "") or "").strip() != target
            ]
            if current_active == target:
                current_active = ""
        elif operation == "activate":
            target = str(profile_id or "").strip()
            if not any(str(item.get("id", "") or "").strip() == target for item in profiles):
                raise PortalError("游戏服务器不存在，无法切换")
            current_active = target
        else:
            servers["enabled"] = as_bool(enabled, False)
            requested = str(active_profile_id or "").strip()
            if requested and not any(
                str(item.get("id", "") or "").strip() == requested for item in profiles
            ):
                raise PortalError("当前使用的游戏服务器不存在")
            current_active = requested

        stored_profiles = store_game_server_profiles(
            profiles,
            previous,
            encrypt_secrets=remote_data_mode(),
        )
        servers["profiles"] = stored_profiles
        available_ids = [str(item.get("id", "") or "").strip() for item in stored_profiles]
        if current_active not in available_ids:
            current_active = next(
                (
                str(item.get("id", "") or "").strip()
                for item in stored_profiles
                    if as_bool(item.get("enabled", True), True)
                ),
                available_ids[0] if available_ids else "",
            )
        servers["active_profile_id"] = current_active
        if not stored_profiles:
            servers["enabled"] = False
        features["game_servers"] = servers
        return features

    if remote_data_mode():
        def mutate(config: dict[str, Any]) -> dict[str, Any]:
            config["features"] = update_features(config.get("features"))
            return config

        return remote_store.mutate_web_config(
            _require_remote_db(db), group_id, updated_by, mutate
        )

    return group_config_module().update_group_data(
        group_id, "features", update_features
    )


def get_group_view(
    group_id: str,
    db: Session | None = None,
    *,
    include_members: bool = True,
) -> dict[str, Any]:
    remote_snapshot = None
    if remote_data_mode():
        remote_db = _require_remote_db(db)
        try:
            remote_snapshot = remote_store.get_snapshot(remote_db, group_id)
            raw_config = (
                deepcopy(remote_snapshot.config_json)
                if remote_snapshot is not None
                and isinstance(remote_snapshot.config_json, dict)
                else {"group_id": str(group_id), "features": {}}
            )
        except Exception:
            _safe_db_rollback(remote_db)
            logger.exception("Unable to load group projection group_id=%s", group_id)
            raw_config = {"group_id": str(group_id), "features": {}}
    else:
        try:
            raw_config = group_config_module().get_group_config(group_id)
        except Exception:
            logger.exception("Unable to load local group projection group_id=%s", group_id)
            raw_config = {"group_id": str(group_id), "features": {}}

    config = _safe_group_config_for_view(raw_config, group_id)
    features = config.get("features", {})
    features = features if isinstance(features, dict) else {}

    # Show the global policy beside each subgroup's editable value.  A group
    # may have copied an older snapshot, so this is informational and never
    # silently overwrites a subgroup override.
    global_defaults: list[dict[str, Any]] = []
    try:
        from .global_config import get_global_feature_values, load_global_config

        global_config = load_global_config()
        for spec in FEATURE_BY_KEY.values():
            values = get_global_feature_values(spec.key, global_config)
            global_defaults.append({"key": spec.key, "label": spec.label, "enabled": bool(values.get("enabled")), "values": values})
    except Exception:
        global_defaults = []

    try:
        auth = (
            remote_store.auth_state_from_snapshot(remote_snapshot)
            if remote_data_mode()
            else authorization_state(group_id, db)
        )
    except Exception:
        _safe_db_rollback(db)
        logger.exception("Unable to load group authorization state group_id=%s", group_id)
        auth = {
            "state": "missing",
            "active": False,
            "expire": 0,
            "expire_text": "authorization unavailable",
        }

    try:
        timed_source = (
            config.get("timed_messages", [])
            if remote_data_mode()
            else plugin_module("timed_message").get_timed_messages(group_id)
        )
        timed_messages = _normalized_timed_tasks(timed_source)
    except Exception:
        logger.exception("Unable to load timed messages group_id=%s", group_id)
        timed_messages = []

    try:
        replies = _list_reply_details(group_id, db, config=config)
    except Exception:
        _safe_db_rollback(db)
        logger.exception("Unable to load group replies group_id=%s", group_id)
        replies = []

    try:
        sub_admin_source = (
            config.get("sub_admins", [])
            if remote_data_mode()
            else plugin_module("sub_admin").get_sub_admins(group_id)
        )
        sub_admins = _normalized_portal_sub_admins(sub_admin_source)
    except Exception:
        logger.exception("Unable to load group sub-admins group_id=%s", group_id)
        sub_admins = []

    try:
        if remote_data_mode():
            player_replies = _list_player_reply_details(config)
        else:
            raw_player_replies = plugin_module("player_reply").list_player_replies(group_id)
            player_replies = (
                _list_player_reply_details(
                    {
                        "player_replies": {
                            str(item.get("user_id", "")): item
                            for item in raw_player_replies
                            if isinstance(item, dict)
                        }
                    }
                )
                if isinstance(raw_player_replies, list)
                else []
            )
    except Exception:
        logger.exception("Unable to load player replies group_id=%s", group_id)
        player_replies = []

    if remote_data_mode():
        snapshot = remote_snapshot
        if snapshot is None:
            # Keep the old defensive behavior when the initial projection
            # read failed or the row has not been created yet.
            snapshot = _safe_remote_snapshot(_require_remote_db(db), group_id)
        try:
            offline_after_seconds = Settings.from_env().offline_after_seconds
        except Exception:
            offline_after_seconds = 180
        sync = _safe_sync_state(snapshot, offline_after_seconds)
        blacklist_summary, stats_summary = _remote_runtime_summaries(snapshot)
        runtime_panels = _normalize_runtime_panels(
            getattr(snapshot, "runtime_json", None) if snapshot is not None else None,
            include_members=include_members,
        )
    else:
        sync = _local_sync_state()

        # Local mode reads the Windows-side files directly.
        blacklist_summary = _local_blacklist_summary(group_id)
        stats_summary = _local_stats_summary(group_id)
        runtime_panels = _local_runtime_panels(group_id)

    feature_rows: list[dict[str, Any]] = []
    for spec in FEATURE_BY_KEY.values():
        try:
            feature_values = get_feature_values(group_id, spec.key, db, config=config)
            if not isinstance(feature_values, dict):
                feature_values = {}
            feature_rows.append(
                {
                    "spec": spec,
                    "values": feature_values,
                    "summary": _feature_config_summary(spec.key, feature_values),
                }
            )
        except Exception:
            # A malformed legacy feature should be visible as disabled while
            # the remaining group settings stay usable.
            logger.exception("Unable to render feature group_id=%s feature=%s", group_id, spec.key)
            feature_rows.append(
                {
                    "spec": spec,
                    "values": {"enabled": False},
                    "summary": "配置读取失败，请查看 Windows/网页错误日志",
                }
            )

    return {
        "config": config,
        "group_display_name": get_group_display_name(config, group_id),
        "group_command_groups": _group_command_groups(config),
        "command_editors": _group_command_editors(config),
        # The generic feature card edits the scalar lottery settings.  The
        # award editor below needs the complete root definition (including
        # probability, stock and enabled state) without another DB round trip.
        "lottery_config": _lottery_config_for_view(config),
        "signin_profiles": _signin_profiles_for_view(config),
        "powered_on": features.get("power_status") == "on",
        "features": feature_rows,
        "action_features": _independent_action_features(config),
        "auth": auth,
        "timed_messages": timed_messages,
        "replies": replies,
        "sub_admins": sub_admins,
        "player_replies": player_replies,
        "blacklist_summary": blacklist_summary,
        "stats_summary": stats_summary,
        "runtime_panels": runtime_panels,
        "global_defaults": global_defaults,
        "sync": sync,
    }


def get_group_member_directory(
    group_id: str,
    db: Session | None = None,
) -> dict[str, Any]:
    """Load only the normalized member directory for the members panel.

    The normal group page deliberately omits this potentially large runtime
    panel. Keeping this read path separate prevents a large group directory
    from slowing unrelated configuration pages and runtime polling.
    """

    if remote_data_mode():
        snapshot = remote_store.get_snapshot(_require_remote_db(db), group_id)
        raw_runtime = getattr(snapshot, "runtime_json", None) if snapshot is not None else None
        if isinstance(raw_runtime, (str, bytes, bytearray)):
            try:
                raw_runtime = json.loads(raw_runtime)
            except (TypeError, ValueError, json.JSONDecodeError):
                raw_runtime = {}
        raw_members = raw_runtime.get("members") if isinstance(raw_runtime, dict) else None
    else:
        raw_members = _local_runtime_panels(group_id).get("members")
    normalized = _normalize_runtime_panels({"members": raw_members})
    return normalized.get("members", _runtime_panel_defaults()["members"])

def apply_feature_values(
    group_id: str,
    feature_key: str,
    values: dict[str, Any],
    db: Session | None = None,
    updated_by: int | None = None,
) -> dict[str, Any]:
    if feature_key not in FEATURE_BY_KEY:
        raise PortalError("未知功能模块")
    allowed = {field.key for field in FEATURE_BY_KEY[feature_key].fields}
    if set(values) != allowed:
        raise PortalError("提交的配置字段不完整或包含未知字段")
    values = deepcopy(values)
    if feature_key == "scum":
        commands = {
            key: "|".join(
                split_command_aliases(values.get(key), default)
            )
            for key, default in SCUM_COMMAND_DEFAULTS.items()
        }
        command_aliases = {
            key: split_command_aliases(value, SCUM_COMMAND_DEFAULTS[key])
            for key, value in commands.items()
        }
        normalized = [
            alias.casefold()
            for aliases in command_aliases.values()
            for alias in aliases
        ]
        if len(set(normalized)) != len(normalized):
            raise PortalError("SCUM 查询的指令及别名不能重复，请分别设置后再保存")
        aliases = {alias.casefold() for alias in SCUM_COMMAND_ALIASES}
        for key, values_for_command in command_aliases.items():
            own_aliases = {
                alias.casefold() for alias in SCUM_COMMAND_ALIAS_GROUPS[key]
            }
            for value in values_for_command:
                if value.casefold() in aliases and value.casefold() not in own_aliases:
                    raise PortalError(
                        f"{value} 是系统保留指令，请使用自定义名称，避免与其他查询功能冲突"
                    )
        values.update(commands)
    if feature_key == "stats":
        command_defaults = {"signin_command": "签到", "bind_command": "绑定SteamID"}
        command_values = {
            key: "|".join(split_command_aliases(values.get(key), default))
            for key, default in command_defaults.items()
        }
        command_aliases = {
            key: split_command_aliases(value, command_defaults[key])
            for key, value in command_values.items()
        }
        all_aliases = [
            alias.casefold()
            for aliases in command_aliases.values()
            for alias in aliases
        ]
        if len(set(all_aliases)) != len(all_aliases):
            raise PortalError("签到命令及 SteamID 绑定命令的别名不能重复")
        values.update(command_values)
        # The generic statistics form carries the dedicated editor's current
        # collection in a hidden field. Normalize it before persisting so a
        # scalar settings save cannot turn dictionaries back into JSON strings
        # or revive a Windows-deleted row.
        values["signin_profiles"] = _validate_signin_profiles(
            values.get("signin_profiles", [])
        )
        _validate_signin_profile_commands(
            values["signin_profiles"],
            signin_command=values.get("signin_command", "签到"),
            bind_command=values.get("bind_command", "绑定SteamID"),
        )
    if feature_key == "entertainment":
        command_defaults = {
            "time_command": "当前时间",
            "dice_command": "骰子",
            "rps_command": "猜拳",
            "random_command": "随机",
            "like_command": "赞我",
            "music_command": "点歌",
            "video_command": "搜索视频",
        }
        command_values = {
            key: "|".join(split_command_aliases(values.get(key), default))
            for key, default in command_defaults.items()
        }
        if command_values.get("like_command") == "点赞":
            command_values["like_command"] = "赞我"
        command_aliases = {
            key: split_command_aliases(value, command_defaults[key])
            for key, value in command_values.items()
        }
        all_aliases = [
            alias.casefold()
            for aliases in command_aliases.values()
            for alias in aliases
        ]
        if len(set(all_aliases)) != len(all_aliases):
            raise PortalError("娱乐命令及别名不能重复")
        if "选歌" in set(all_aliases):
            raise PortalError("选歌是音乐候选选择指令，不能设置为其他娱乐命令")
        try:
            daily_limit = int(values.get("like_daily_limit", 20))
        except (TypeError, ValueError, OverflowError):
            raise PortalError("每日点赞上限必须是 1-20 的整数")
        if not 1 <= daily_limit <= 20:
            raise PortalError("每日点赞上限必须是 1-20 的整数")
        try:
            selection_timeout = int(values.get("music_selection_timeout", 600))
        except (TypeError, ValueError, OverflowError):
            raise PortalError("点歌等待选歌时间必须是 10-3600 秒的整数")
        if not 10 <= selection_timeout <= 3600:
            raise PortalError("点歌等待选歌时间必须是 10-3600 秒的整数")
        sources = values.get("music_sources")
        allowed_sources = {"qq", "163", "kugou", "soda"}
        if not isinstance(sources, list) or not sources:
            raise PortalError("请至少选择一个音乐源平台")
        if any(source not in allowed_sources for source in sources):
            raise PortalError("音乐源平台仅支持 QQ音乐、网易云音乐、酷狗音乐、汽水音乐")
        values["music_sources"] = list(dict.fromkeys(sources))
        default_source = str(values.get("music_default_source", "") or "").strip().lower()
        if default_source not in values["music_sources"]:
            raise PortalError("默认点歌平台必须在已启用的音乐源平台中")
        values["music_default_source"] = default_source
        values.pop("music_provider", None)
        values["like_daily_limit"] = daily_limit
        values["music_selection_timeout"] = selection_timeout
        values.update(command_values)
    if feature_key == "game_commands":
        try:
            game_module = _plugin_module("game_commands")
            normalized_commands = game_module.normalize_game_commands(
                values.get("commands", [])
            )
        except Exception as exc:
            raise PortalError("游戏指令模板格式无效") from exc
        if values.get("commands") and not normalized_commands:
            raise PortalError("游戏指令模板格式无效，请检查每行代码或 JSON 对象")
        values["commands"] = normalized_commands

    def update_features(existing):
        features = deepcopy(existing) if isinstance(existing, dict) else {}
        module_config = features.get(feature_key)
        if not isinstance(module_config, dict):
            module_config = {}
        next_values = deepcopy(values)
        if feature_key == "game_servers":
            next_values["profiles"] = store_game_server_profiles(
                next_values.get("profiles", []),
                module_config.get("profiles", []),
                encrypt_secrets=remote_data_mode(),
            )
        module_config.update(next_values)
        if feature_key == "recall":
            module_config["self_recall_enabled"] = bool(values.get("self_recall"))
            # Keep the legacy web checkbox and the command-era explicit
            # scope in lockstep.  The Windows runtime prefers member_scope
            # when present, so leaving an old value here makes web changes
            # appear to have no effect.
            module_config["member_scope"] = (
                "all" if bool(values.get("enable_admin_recall")) else "members"
            )
            # The portal intentionally exposes one time-limit control for
            # both member-message recall and bot-message self recall.  Keep
            # an explicit runtime mirror so the Windows helper does not fall
            # back to an old root-level ``self_recall_time`` after a web save.
            module_config["self_recall_time"] = int(values["time_limit"])
        elif feature_key == "text_to_image":
            module_config["enable_text_to_image"] = bool(values.get("enabled"))
        features[feature_key] = module_config
        return features

    def update_lottery_root(existing):
        """Mirror the generic lottery fields while retaining award rows."""

        current = _lottery_config_for_view({"lottery": existing})
        for key in _LOTTERY_SCALAR_KEYS:
            current[key] = deepcopy(values[key])
        # Generic feature saves must never erase rows maintained by the
        # dedicated award editor (or by a Windows-local command).
        return _validate_lottery_config(current)

    if remote_data_mode():
        remote_db = _require_remote_db(db)

        def mutate(config):
            previous_features = config.get("features")
            previous_group_config = (
                previous_features.get("group_config")
                if isinstance(previous_features, dict)
                else {}
            )
            previous_group_config = (
                previous_group_config if isinstance(previous_group_config, dict) else {}
            )
            previous_prefix = str(
                previous_group_config.get(
                    "member_name_prefix", config.get("member_name_prefix", "")
                )
                or ""
            ).replace("\r", " ").replace("\n", " ").strip()[:20]
            previous_include_admins = previous_group_config.get(
                "member_name_prefix_include_admins",
                config.get("member_name_prefix_include_admins", True),
            )

            def apply_update(key: str, updater):
                config[key] = updater(config.get(key))

            apply_update("features", update_features)
            for field_key, root_key in ROOT_MIRRORS.get(feature_key, {}).items():
                config[root_key] = deepcopy(values[field_key])
            if feature_key == "lottery":
                config["lottery"] = update_lottery_root(config.get("lottery"))
            if feature_key == "recall":
                config["recall_member_scope"] = (
                    "all" if bool(values.get("enable_admin_recall")) else "members"
                )
            _merge_nested_root_mirror(config, feature_key, values)
            if feature_key == "welcome" and "auto_reply_enabled" in values:
                config["welcome_auto_reply_enabled"] = bool(values["auto_reply_enabled"])
            if feature_key == "reply":
                replies = deepcopy(config.get("replies", {}))
                replies.setdefault("exact", {})
                replies.setdefault("fuzzy", [])
                replies.setdefault("regex", [])
                replies["cooldown"] = int(values["cooldown"])
                config["replies"] = replies
            if feature_key == "group_config":
                next_prefix = str(values.get("member_name_prefix", "") or "")
                next_prefix = next_prefix.replace("\r", " ").replace("\n", " ").strip()[:20]
                next_include_admins = bool(values.get("member_name_prefix_include_admins", True))
                old_include_admins = previous_include_admins
                if isinstance(old_include_admins, str):
                    old_include_admins = old_include_admins.strip().lower() not in {
                        "", "0", "false", "off", "no", "否", "关闭"
                    }
                else:
                    old_include_admins = bool(old_include_admins)
                # Saving a prefix is a configuration change, but the actual
                # QQ card update must run on Windows where OneBot is online.
                # Queue one short-lived action so existing members are updated
                # immediately after the next authenticated sync. New members
                # continue to use the persisted feature values automatically.
                if next_prefix != previous_prefix or next_include_admins != old_include_admins:
                    actions = config.get("portal_actions", [])
                    actions = [
                        entry
                        for entry in (actions if isinstance(actions, list) else [])
                        if not isinstance(entry, dict)
                        or str(entry.get("action", "")).strip().lower()
                        != "apply_member_name_prefix"
                    ]
                    actions.append(
                        {
                            "action": "apply_member_name_prefix",
                            "request_id": "prefix-" + uuid4().hex,
                            "created_at": int(time.time()),
                            "prefix": next_prefix,
                            "include_admins": next_include_admins,
                            "strip_prefix": previous_prefix,
                        }
                    )
                    config["portal_actions"] = actions[-8:]
            return config

        return remote_store.mutate_web_config(
            remote_db, group_id, updated_by, mutate
        )

    config_api = group_config_module()

    updates: list[tuple[str, Any]] = [("features", update_features)]
    for field_key, root_key in ROOT_MIRRORS.get(feature_key, {}).items():
        value = deepcopy(values[field_key])
        updates.append((root_key, lambda _old, current=value: deepcopy(current)))

    if feature_key == "lottery":
        updates.append(("lottery", update_lottery_root))
    if feature_key == "recall":
        scope = "all" if bool(values.get("enable_admin_recall")) else "members"
        updates.append(("recall_member_scope", lambda _old, current=scope: current))

    nested_mirror = NESTED_ROOT_MIRRORS.get(feature_key)
    if nested_mirror is not None:
        root_key, field_names = nested_mirror
        mirror_values = {
            field_name: deepcopy(values[field_name]) for field_name in field_names
        }

        def update_nested_mirror(existing, current=mirror_values):
            settings = deepcopy(existing) if isinstance(existing, dict) else {}
            settings.update(deepcopy(current))
            return settings

        updates.append((root_key, update_nested_mirror))

    if feature_key == "welcome" and "auto_reply_enabled" in values:
        value = bool(values["auto_reply_enabled"])
        updates.append(("welcome_auto_reply_enabled", lambda _old, current=value: current))

    if feature_key == "reply":
        cooldown = int(values["cooldown"])

        def update_replies(existing):
            replies = deepcopy(existing) if isinstance(existing, dict) else {}
            replies.setdefault("exact", {})
            replies.setdefault("fuzzy", [])
            replies.setdefault("regex", [])
            replies["cooldown"] = cooldown
            return replies

        updates.append(("replies", update_replies))

    return config_api.batch_update_group_data(group_id, updates)


def apply_signin_profile_action(
    group_id: str,
    operation: str,
    *,
    profile_id: str = "",
    name: str = "",
    command: str = "",
    mode: str = "basic",
    schedule: str = "daily",
    enabled: Any = True,
    base_reward: Any = "",
    reward_3_days: Any = "",
    reward_7_days: Any = "",
    reward_14_days: Any = "",
    reward_30_days: Any = "",
    signin_message: str = "",
    steam_reward_mode: str = "database",
    steam_reward_operation: str = "execute_game_command",
    steam_reward_command: str = _DEFAULT_SIGNIN_REWARD_COMMAND,
    steam_reward_failure_message: str = "签到奖励执行失败，未完成本次签到，请稍后重试。",
    db: Session | None = None,
    updated_by: int | None = None,
) -> dict[str, Any]:
    """Add, update or delete one independent sign-in scheme atomically."""

    operation = str(operation or "").strip().lower()
    if operation not in {"add", "update", "delete"}:
        raise PortalError("未知的签到方案操作")
    normalized_id = str(profile_id or "").strip()
    if operation == "add" and not normalized_id:
        normalized_id = "profile-" + uuid4().hex[:12]
    if not _SIGNIN_PROFILE_ID_RE.fullmatch(normalized_id):
        raise PortalError("签到方案 ID 只能包含字母、数字、下划线和短横线")

    clean_name = str(name or "").strip()
    clean_command = str(command or "").strip().lstrip("/")
    clean_mode = str(mode or "basic").strip().lower()
    clean_schedule = str(schedule or "daily").strip().lower()
    clean_message = str(signin_message or "").strip()
    clean_reward_mode = str(steam_reward_mode or "database").strip().lower()
    clean_reward_operation = str(steam_reward_operation or "execute_game_command").strip().lower()
    clean_reward_command = str(steam_reward_command or _DEFAULT_SIGNIN_REWARD_COMMAND).strip()
    clean_reward_failure_message = str(
        steam_reward_failure_message
        or "签到奖励执行失败，未完成本次签到，请稍后重试。"
    ).strip()
    if operation != "delete":
        if not clean_name or len(clean_name) > 80:
            raise PortalError("签到方案名称不能为空且不能超过 80 个字符")
        if (
            not clean_command
            or len(clean_command) > 32
            or any(character.isspace() for character in clean_command)
        ):
            raise PortalError("签到指令只能填写不带斜杠和空格的命令名")
        if clean_mode not in _SIGNIN_PROFILE_MODES:
            raise PortalError("签到方式无效")
        if clean_schedule not in _SIGNIN_PROFILE_SCHEDULES:
            raise PortalError("签到周期无效")
        if len(clean_message) > 1000:
            raise PortalError("签到成功文案不能超过 1000 个字符")
        if clean_reward_mode not in _SIGNIN_REWARD_MODES:
            raise PortalError("Steam 奖励方式无效")
        if not _SIGNIN_REWARD_OPERATION_RE.fullmatch(clean_reward_operation):
            raise PortalError("WebSocket 游戏操作名称无效")
        if not clean_reward_command or len(clean_reward_command) > 512:
            raise PortalError("Steam 奖励游戏指令不能为空且不能超过 512 个字符")
        if len(clean_reward_failure_message) > 500:
            raise PortalError("游戏奖励失败文案不能超过 500 个字符")
    reward_values: dict[str, int | None] = {}
    if operation != "delete":
        for reward_key, raw_value in (
            ("base_reward", base_reward),
            ("reward_3_days", reward_3_days),
            ("reward_7_days", reward_7_days),
            ("reward_14_days", reward_14_days),
            ("reward_30_days", reward_30_days),
        ):
            if not str(raw_value or "").strip():
                reward_values[reward_key] = None
                continue
            try:
                reward = int(raw_value)
            except (TypeError, ValueError, OverflowError):
                raise PortalError("签到奖励必须是 0 到 1000000 之间的整数") from None
            if reward < 0 or reward > 1_000_000:
                raise PortalError("签到奖励必须是 0 到 1000000 之间的整数")
            reward_values[reward_key] = reward

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        profiles = _signin_profiles_for_view(config)
        index = next(
            (position for position, item in enumerate(profiles) if item["id"] == normalized_id),
            -1,
        )
        if operation == "add" and index >= 0:
            raise PortalError("相同 ID 的签到方案已经存在")
        if operation != "add" and index < 0:
            raise PortalError("未找到该签到方案，可能已被 Windows 端删除")
        if operation == "delete":
            profiles.pop(index)
        else:
            if any(
                item["command"].casefold() == clean_command.casefold()
                and item["id"] != normalized_id
                for item in profiles
            ):
                raise PortalError("该签到指令已经被其他方案使用")
            stats = config.get("features", {}).get("stats", {})
            stats = stats if isinstance(stats, dict) else {}
            _validate_signin_profile_commands(
                profiles + [{"command": clean_command, "enabled": _as_portal_bool(enabled, True)}],
                signin_command=stats.get("signin_command", "签到"),
                bind_command=stats.get("bind_command", "绑定SteamID"),
            )
            previous = profiles[index] if index >= 0 else {}
            profile: dict[str, Any] = deepcopy(previous)
            profile.update({
                "id": normalized_id,
                "name": clean_name,
                "command": clean_command,
                "signin_command": clean_command,
                "mode": clean_mode,
                "signin_mode": clean_mode,
                "schedule": clean_schedule,
                "schedule_mode": clean_schedule,
                "enabled": _as_portal_bool(enabled, True),
                "signin_message": clean_message,
                "steam_reward_mode": clean_reward_mode,
                "steam_reward_operation": clean_reward_operation,
                "steam_reward_command": clean_reward_command,
                "steam_reward_failure_message": clean_reward_failure_message,
            })
            # Empty reward fields inherit the group's normal tier; explicit
            # zero remains a valid zero-reward value for that scheme.
            for reward_key, reward in reward_values.items():
                if reward is None:
                    profile.pop(reward_key, None)
                else:
                    profile[reward_key] = reward
            if operation == "add":
                if len(profiles) >= _SIGNIN_PROFILE_MAX:
                    raise PortalError(f"签到方案最多可设置 {_SIGNIN_PROFILE_MAX} 个")
                profiles.append(profile)
            else:
                profiles[index] = profile

        features = config.get("features")
        features = deepcopy(features) if isinstance(features, dict) else {}
        stats = features.get("stats")
        stats = deepcopy(stats) if isinstance(stats, dict) else {}
        stats["signin_profiles"] = profiles
        features["stats"] = stats
        config["features"] = features
        return config

    if remote_data_mode():
        return remote_store.mutate_web_config(
            _require_remote_db(db), group_id, updated_by, mutate
        )
    config_api = group_config_module()
    current = config_api.get_group_config(group_id)
    next_config = mutate(deepcopy(current))
    return config_api.batch_update_group_data(
        group_id,
        [
            ("features", lambda _old, value=deepcopy(next_config["features"]): value),
        ],
    )


def _mirror_lottery_definition(
    config: dict[str, Any], validated: dict[str, Any]
) -> dict[str, Any]:
    """Write one validated lottery definition and its feature compatibility mirror."""

    config["lottery"] = deepcopy(validated)
    features = config.get("features")
    features = deepcopy(features) if isinstance(features, dict) else {}
    lottery_feature = features.get("lottery")
    lottery_feature = deepcopy(lottery_feature) if isinstance(lottery_feature, dict) else {}
    for key in _LOTTERY_SCALAR_KEYS:
        lottery_feature[key] = deepcopy(validated[key])
    features["lottery"] = lottery_feature
    config["features"] = features
    return config


def _save_local_lottery_mutation(
    group_id: str, mutate
) -> dict[str, Any]:
    config_api = group_config_module()
    current_config = config_api.get_group_config(group_id)
    next_config = mutate(deepcopy(current_config))
    return config_api.batch_update_group_data(
        group_id,
        [
            ("lottery", lambda _old, value=deepcopy(next_config["lottery"]): value),
            ("features", lambda _old, value=deepcopy(next_config["features"]): value),
        ],
    )


def apply_lottery_campaign_action(
    group_id: str,
    operation: str,
    *,
    campaign_id: str = "",
    name: str = "",
    keyword: str = "",
    enabled: Any = True,
    max_draws_per_user: Any = 0,
    cooldown: Any = 0,
    success_message: str = "",
    empty_message: str = "",
    db: Session | None = None,
    updated_by: int | None = None,
) -> dict[str, Any]:
    """Maintain a named lottery activity with its own command and prizes."""

    operation = str(operation or "").strip().lower()
    if operation not in {"add", "update", "delete"}:
        raise PortalError("未知的抽奖活动操作")
    normalized_id = str(campaign_id or "").strip()
    if operation == "add" and not normalized_id:
        normalized_id = "campaign-" + uuid4().hex[:12]
    if operation != "delete" and not _LOTTERY_AWARD_ID_RE.fullmatch(normalized_id):
        raise PortalError("抽奖活动 ID 只能包含字母、数字、下划线和短横线")
    clean_name = str(name or "").strip()
    clean_keyword = "|".join(split_command_aliases(keyword, clean_name or "抽奖"))
    clean_success = str(success_message or "").strip()
    clean_empty = str(empty_message or "").strip()

    def strict_int(value: Any, label: str, maximum: int) -> int:
        if isinstance(value, bool):
            raise PortalError(f"{label}必须是整数")
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            raise PortalError(f"{label}必须是整数") from None
        if number < 0 or number > maximum:
            raise PortalError(f"{label}必须是 0 到 {maximum} 之间的整数")
        return number

    draw_limit = (
        strict_int(max_draws_per_user, "每人抽取次数", _LOTTERY_MAX_DRAWS)
        if operation != "delete"
        else 0
    )
    cooldown_seconds = (
        strict_int(cooldown, "抽奖冷却", _LOTTERY_MAX_COOLDOWN)
        if operation != "delete"
        else 0
    )
    if operation != "delete":
        if not clean_name or len(clean_name) > 100:
            raise PortalError("抽奖活动名称不能为空且不能超过 100 个字符")
        if not clean_keyword or len(clean_keyword) > 256:
            raise PortalError("抽奖活动指令只能填写不带斜杠和空格的命令名")
        if len(clean_success) > _LOTTERY_MAX_MESSAGE or len(clean_empty) > _LOTTERY_MAX_MESSAGE:
            raise PortalError("抽奖回复文案不能超过 1000 个字符")

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        current = _lottery_config_for_view(config)
        campaigns = current.get("campaigns", [])
        campaigns = deepcopy(campaigns) if isinstance(campaigns, list) else []
        index = next(
            (
                position
                for position, item in enumerate(campaigns)
                if isinstance(item, dict) and str(item.get("id", "")) == normalized_id
            ),
            -1,
        )
        # Older snapshots and browser pages could submit the visible
        # activity name (or an old id) when the campaign list changed between
        # render and submit. Deletion is safe to make tolerant here: match a
        # unique name only when the id did not resolve, while updates still
        # require the canonical id to avoid editing the wrong activity.
        if index < 0 and operation == "delete" and clean_name:
            matching = [
                position
                for position, item in enumerate(campaigns)
                if isinstance(item, dict)
                and str(item.get("name", "") or "").strip().casefold()
                == clean_name.casefold()
            ]
            if len(matching) == 1:
                index = matching[0]
        if operation == "add" and index >= 0:
            raise PortalError("相同 ID 的抽奖活动已经存在")
        if operation != "add" and index < 0:
            raise PortalError("未找到该抽奖活动，可能已被 Windows 端删除")
        if operation == "delete":
            campaigns.pop(index)
        else:
            used_keywords = {
                alias.casefold()
                for item in campaigns
                if isinstance(item, dict)
                and str(item.get("id", "")) != normalized_id
                for alias in split_command_aliases(item.get("keyword"), "抽奖")
            }
            used_keywords.update(
                alias.casefold()
                for alias in split_command_aliases(current.get("keyword"), "抽奖")
            )
            if any(
                alias.casefold() in used_keywords
                for alias in split_command_aliases(clean_keyword, clean_name or "抽奖")
            ):
                raise PortalError("该抽奖指令已经被默认活动或其他活动使用")
            previous = campaigns[index] if index >= 0 and isinstance(campaigns[index], dict) else {}
            campaign = deepcopy(previous)
            campaign.update(
                {
                    "id": normalized_id,
                    "name": clean_name,
                    "keyword": clean_keyword,
                    "enabled": _as_portal_bool(enabled, True),
                    "max_draws_per_user": draw_limit,
                    "cooldown": cooldown_seconds,
                    "success_message": clean_success or current["success_message"],
                    "empty_message": clean_empty or current["empty_message"],
                    "awards": deepcopy(previous.get("awards", [])) if isinstance(previous, dict) else [],
                }
            )
            if operation == "add":
                if len(campaigns) >= 50:
                    raise PortalError("抽奖活动最多可设置 50 个")
                campaigns.append(campaign)
            else:
                campaigns[index] = campaign
        current["campaigns"] = campaigns
        validated = _validate_lottery_config(current)
        return _mirror_lottery_definition(config, validated)

    if remote_data_mode():
        return remote_store.mutate_web_config(
            _require_remote_db(db), group_id, updated_by, mutate
        )
    return _save_local_lottery_mutation(group_id, mutate)


def apply_lottery_award_action(
    group_id: str,
    operation: str,
    *,
    award_id: str = "",
    name: str = "",
    probability: Any = 0,
    stock: Any = 0,
    enabled: Any = False,
    campaign_id: str = "",
    db: Session | None = None,
    updated_by: int | None = None,
) -> dict[str, Any]:
    """Add, edit or remove one web-managed lottery award.

    Award rows are intentionally a separate operation from the six scalar
    lottery fields.  This keeps the existing feature schema/API compatible
    while giving the Linux page a usable table editor.  The whole validated
    root definition is committed in one remote mutation (or one local batch
    update), so a Windows heartbeat receives an atomic revision and ACKs it as
    one change.
    """

    operation = str(operation or "").strip().lower()
    if operation not in {"add", "update", "delete"}:
        raise PortalError("未知的奖项操作")
    normalized_id = str(award_id or "").strip()
    normalized_campaign_id = str(campaign_id or "").strip()
    if normalized_campaign_id and not _LOTTERY_AWARD_ID_RE.fullmatch(normalized_campaign_id):
        raise PortalError("抽奖活动 ID 格式无效")
    if operation != "add" and not _LOTTERY_AWARD_ID_RE.fullmatch(normalized_id):
        raise PortalError("奖项 ID 格式无效")

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        current = _lottery_config_for_view(config)
        campaign: dict[str, Any] | None = None
        if normalized_campaign_id:
            campaign = next(
                (
                    item
                    for item in current.get("campaigns", [])
                    if isinstance(item, dict)
                    and str(item.get("id", "")) == normalized_campaign_id
                ),
                None,
            )
            if campaign is None:
                raise PortalError("未找到该抽奖活动，可能已被 Windows 端删除")
            raw_awards = campaign.get("awards", [])
        else:
            raw_awards = current["awards"]
        raw_awards = raw_awards if isinstance(raw_awards, list) else []
        awards = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for raw_award in raw_awards:
            award = _lottery_award_for_view(raw_award)
            if award is None:
                continue
            name_key = award["name"].casefold()
            if award["id"] in seen_ids or name_key in seen_names:
                continue
            seen_ids.add(award["id"])
            seen_names.add(name_key)
            awards.append(award)
        if operation == "add":
            clean_name = str(name or "").strip()
            if not clean_name:
                raise PortalError("奖项名称不能为空")
            if any(item["name"].casefold() == clean_name.casefold() for item in awards):
                raise PortalError("同名奖项已经存在")
            new_id = "award-" + uuid4().hex[:16]
            awards.append(
                {
                    "id": new_id,
                    "name": clean_name,
                    "probability": _lottery_probability(probability),
                    "stock": _lottery_int(stock, -1, 0, _LOTTERY_MAX_STOCK),
                    "enabled": _as_lottery_bool(enabled, False),
                }
            )
            if awards[-1]["stock"] < 0:
                raise PortalError("奖项库存必须是 0 到 1000000 之间的整数")
        else:
            index = next(
                (idx for idx, item in enumerate(awards) if item["id"] == normalized_id),
                -1,
            )
            if index < 0:
                raise PortalError("未找到该奖项，可能已被 Windows 端删除")
            if operation == "delete":
                awards.pop(index)
            else:
                clean_name = str(name or "").strip()
                if not clean_name:
                    raise PortalError("奖项名称不能为空")
                if any(
                    idx != index and item["name"].casefold() == clean_name.casefold()
                    for idx, item in enumerate(awards)
                ):
                    raise PortalError("同名奖项已经存在")
                next_stock = _lottery_int(stock, -1, 0, _LOTTERY_MAX_STOCK)
                if next_stock < 0:
                    raise PortalError("奖项库存必须是 0 到 1000000 之间的整数")
                awards[index] = {
                    "id": normalized_id,
                    "name": clean_name,
                    "probability": _lottery_probability(probability),
                    "stock": next_stock,
                    "enabled": _as_lottery_bool(enabled, False),
                }
        if sum(item["probability"] for item in awards if item["enabled"]) > 100.000001:
            raise PortalError("启用奖项的概率总和不能超过 100%")
        if campaign is not None:
            campaign["awards"] = awards
        else:
            current["awards"] = awards
        validated = _validate_lottery_config(current)
        return _mirror_lottery_definition(config, validated)

    if remote_data_mode():
        return remote_store.mutate_web_config(
            _require_remote_db(db), group_id, updated_by, mutate
        )

    # Local mode uses one atomic group-file write. The feature mirror and root
    # definition therefore reach the next heartbeat in the same revision.
    return _save_local_lottery_mutation(group_id, mutate)


def set_power(
    group_id: str, powered_on: bool, db: Session | None = None, updated_by: int | None = None
) -> dict:
    def update_features(existing):
        features = deepcopy(existing) if isinstance(existing, dict) else {}
        features["power_status"] = "on" if powered_on else "off"
        return features

    if remote_data_mode():
        remote_db = _require_remote_db(db)

        def mutate(config):
            config["features"] = update_features(config.get("features"))
            return config

        return remote_store.mutate_web_config(
            remote_db, group_id, updated_by, mutate
        )
    group_config_module().update_group_data(group_id, "features", update_features)
    return group_config_module().get_group_config(group_id)


def upsert_snapshot(
    db: Session, group_id: str, config: dict, updated_by: int | None
) -> GroupConfigSnapshot:
    snapshot = db.scalar(
        select(GroupConfigSnapshot).where(GroupConfigSnapshot.group_id == group_id)
    )
    if snapshot is None:
        snapshot = GroupConfigSnapshot(
            group_id=group_id,
            revision=1,
            config_json=deepcopy(config),
            updated_by=updated_by,
        )
        db.add(snapshot)
    else:
        next_config = deepcopy(config)
        if snapshot.config_json != next_config or snapshot.updated_by != updated_by:
            snapshot.revision += 1
            snapshot.config_json = next_config
            snapshot.updated_by = updated_by
            snapshot.updated_at = utcnow()
    return snapshot


def add_audit_log(
    db: Session,
    user: User | None,
    group_id: str | None,
    action: str,
    summary: str,
    request=None,
    changes: dict | None = None,
) -> AuditLog:
    forwarded = request.headers.get("x-forwarded-for", "") if request else ""
    ip_address = (
        forwarded.split(",", 1)[0].strip()
        if forwarded
        else (request.client.host if request and request.client else "")
    )
    log = AuditLog(
        user_id=user.id if user else None,
        group_id=group_id,
        action=action,
        summary=summary[:255],
        changes=changes,
        ip_address=ip_address[:64],
        user_agent=(request.headers.get("user-agent", "") if request else "")[:1000],
    )
    db.add(log)
    return log


def _list_reply_details(
    group_id: str,
    db: Session | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    raw = (
        (config if isinstance(config, dict) else remote_store.get_config(_require_remote_db(db), group_id)).get("replies", {})
        if remote_data_mode()
        else plugin_module("reply").get_group_replies(group_id)
    )
    if not isinstance(raw, dict):
        raw = {}
    result: list[dict[str, Any]] = []

    def display_item(key: object, reply_type: str, data: object) -> dict[str, Any]:
        data = data if isinstance(data, dict) else {}
        portal_assets = reply_asset_service.normalize_asset_references(
            data.get("image_portal_assets")
        )
        legacy_images = data.get("image_source_urls", data.get("image_urls", []))
        legacy_images = legacy_images if isinstance(legacy_images, list) else []
        image_count = len(portal_assets) or len(legacy_images)
        view = {
            "key": str(key),
            "type": reply_type,
            "reply": str(data.get("reply", "")),
            "probability": _reply_probability(data.get("probability", 100)),
            "max_uses": _reply_max_uses(data.get("max_uses", 0)),
            "has_images": bool(data.get("has_images")) or bool(image_count),
            "image_count": image_count,
            "image_status": str(data.get("image_cache_status", ""))[:32],
            "portal_assets": portal_assets,
        }
        # Keep only opaque IDs in the view model when there are actual portal
        # assets.  Omitting the optional field for legacy/text-only rules
        # preserves the small, backwards-compatible projection used by older
        # callers while the template's ``default`` filter remains safe.
        if portal_assets:
            view["portal_asset_ids"] = [int(item["id"]) for item in portal_assets]
        return view

    exact = raw.get("exact", {})
    exact = exact if isinstance(exact, dict) else {}
    for keyword, data in exact.items():
        data = data if isinstance(data, dict) else {}
        result.append(display_item(keyword, "exact", data))

    fuzzy_data = raw.get("fuzzy_keywords", {})
    fuzzy_data = fuzzy_data if isinstance(fuzzy_data, dict) else {}
    fuzzy = raw.get("fuzzy", [])
    fuzzy = fuzzy if isinstance(fuzzy, list) else []
    for keyword in fuzzy:
        # Older plugin versions occasionally wrote a nested object/list into
        # the fuzzy-keyword list.  Passing such a value to ``dict.get`` raises
        # ``TypeError: unhashable type`` and used to turn the entire group page
        # into a 500.  Keywords are text at runtime; coerce scalar legacy
        # values and ignore structured garbage while keeping the valid rules.
        if not isinstance(keyword, (str, int, float, bool)):
            continue
        normalized_keyword = str(keyword)
        data = fuzzy_data.get(normalized_keyword, {})
        data = data if isinstance(data, dict) else {}
        result.append(display_item(normalized_keyword, "fuzzy", data))

    regex = raw.get("regex", [])
    regex = regex if isinstance(regex, list) else []
    for entry in regex:
        if not isinstance(entry, dict):
            continue
        data = entry.get("reply", {})
        data = data if isinstance(data, dict) else {}
        result.append(display_item(entry.get("pattern", ""), "regex", data))
    return result


def _reply_probability(
    value: object, default: int = 100, *, strict: bool = False
) -> int:
    """Validate the persisted per-rule trigger probability."""

    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not 0 <= result <= 100:
        if strict:
            raise PortalError("自动回复触发概率必须在 0 到 100 之间")
        return default
    return result


def _reply_max_uses(
    value: object, default: int = 0, *, strict: bool = False
) -> int:
    """Validate the Windows-local lifetime trigger cap; zero is unlimited."""

    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not 0 <= result <= 1_000_000:
        if strict:
            raise PortalError("自动回复最大触发次数必须在 0 到 1000000 之间")
        return default
    return result


def _list_player_reply_details(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw = config.get("player_replies", {})
    if not isinstance(raw, dict):
        return []
    details: list[dict[str, Any]] = []
    for user_id, data in raw.items():
        if not isinstance(data, dict):
            continue
        messages = data.get("messages", [])
        # Older imports occasionally stored a single string; a malformed
        # number/object must not reach Jinja's ``join`` filter and blank the
        # whole group page.
        if isinstance(messages, str):
            normalized_messages = [messages] if messages else []
        elif isinstance(messages, (list, tuple, set)):
            normalized_messages = [
                str(message) for message in messages if isinstance(message, (str, int, float))
            ]
        else:
            normalized_messages = []
        details.append(
            {
                "user_id": str(user_id),
                "messages": normalized_messages,
                "at_user": bool(data.get("at_user", True)),
                "enabled": bool(data.get("enabled", True)),
            }
        )
    return details


def _local_blacklist_summary(group_id: str) -> dict[str, Any]:
    """Build a bounded, read-only blacklist view for local portal mode.

    Blacklist records are runtime/user data and are deliberately excluded from
    the remote projection.  Keep this helper local-only and normalize every
    value before it reaches Jinja so one malformed legacy record cannot break
    the whole group page.
    """
    unavailable = {
        "available": False,
        "message": "等待 Windows 同步黑名单数据；运行时记录不会上传到 Linux。",
        "total": 0,
        "active": 0,
        "entries": [],
    }
    if remote_data_mode():
        return unavailable
    try:
        module = _plugin_module("qqadmin.blacklist")
        list_records = getattr(module, "list_all_blacklist_records", None)
        if callable(list_records):
            raw_records = list_records(group_id)
        else:
            list_records = getattr(module, "list_blacklist", None)
            raw_records = list_records(group_id, include_inactive=True) if callable(list_records) else []
        raw_records = raw_records if isinstance(raw_records, list) else []
        entries: list[dict[str, Any]] = []
        active_count = 0
        for raw in raw_records[:100]:
            if not isinstance(raw, dict):
                continue
            user_id = str(raw.get("user_id", "") or "").strip()[:32]
            if not user_id:
                continue
            active = bool(raw.get("is_active", True))
            if active:
                active_count += 1
            try:
                expire_time = max(0, int(raw.get("expire_time", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                expire_time = 0
            entries.append(
                {
                    "user_id": user_id,
                    "active": active,
                    "reason": str(raw.get("reason", "") or "")[:200],
                    "ban_type": str(raw.get("ban_type", "") or "")[:24],
                    "expire_time": expire_time,
                    "expire_text": _format_runtime_timestamp(expire_time, "永久"),
                }
            )
        return {
            "available": True,
            "message": "",
            "total": len(entries),
            "active": active_count,
            "entries": entries,
        }
    except Exception:
        logger.warning("Unable to load local blacklist summary group_id=%s", group_id, exc_info=True)
        return {
            "available": False,
            "message": "Windows 黑名单数据暂时无法读取，请查看本地机器人日志。",
            "total": 0,
            "active": 0,
            "entries": [],
        }


def _local_stats_summary(group_id: str) -> dict[str, Any]:
    """Return read-only local statistics without synchronizing user records."""
    unavailable = {
        "available": False,
        "message": "等待 Windows 同步统计数据；运行时用户数据不会上传到 Linux。",
        "group": {"total_users": 0, "today_signins": 0, "total_points": 0, "avg_points": 0},
        "ranking": [],
    }
    if remote_data_mode():
        return unavailable
    try:
        module = _plugin_module("qqadmin.stats")
        get_group_stats = getattr(module, "get_group_stats", None)
        get_ranking = getattr(module, "get_ranking", None)
        raw_group = get_group_stats(group_id) if callable(get_group_stats) else {}
        raw_group = raw_group if isinstance(raw_group, dict) else {}
        group = {}
        for key in ("total_users", "today_signins", "total_points", "avg_points"):
            try:
                group[key] = max(0, int(raw_group.get(key, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                group[key] = 0
        raw_ranking = get_ranking(group_id, 10) if callable(get_ranking) else []
        ranking: list[dict[str, Any]] = []
        if isinstance(raw_ranking, list):
            for index, raw in enumerate(raw_ranking[:10], 1):
                if not isinstance(raw, dict):
                    continue
                user_id = str(raw.get("user_id", "") or "").strip()[:32]
                if not user_id:
                    continue
                def _count(name: str) -> int:
                    try:
                        return max(0, int(raw.get(name, 0) or 0))
                    except (TypeError, ValueError, OverflowError):
                        return 0
                ranking.append(
                    {
                        "rank": index,
                        "user_id": user_id,
                        "user_name": str(raw.get("user_name", "") or "")[:80],
                        "points": _count("points"),
                        "signin_count": _count("signin_count"),
                    }
                )
        return {"available": True, "message": "", "group": group, "ranking": ranking}
    except Exception:
        logger.warning("Unable to load local statistics summary group_id=%s", group_id, exc_info=True)
        return {
            "available": False,
            "message": "Windows 统计数据暂时无法读取，请查看本地机器人日志。",
            "group": {"total_users": 0, "today_signins": 0, "total_points": 0, "avg_points": 0},
            "ranking": [],
        }


def _normalized_timed_tasks(raw: Any) -> list[dict[str, Any]]:
    """Normalize display data while retaining old Windows task defaults.

    Older task records predate the explicit ``enabled`` field.  The Windows
    scheduler treats its absence as enabled, so the portal must show and
    submit the same state rather than making an old task look stopped.
    """
    if not isinstance(raw, list):
        return []
    tasks: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # Keep the display projection small and scalar. Older Windows files
        # occasionally contain null/object values in a task record; passing
        # those through to the template can break URL construction or text
        # rendering for the entire group page.
        task = {
            "id": str(item.get("id", "")).strip()[:128],
            "name": str(item.get("name", "")).strip()[:100],
            "cron_expr": str(item.get("cron_expr", "")).strip()[:100],
            "run_at": _safe_expire_value(item.get("run_at", 0)),
            "action": str(item.get("action", "message")).strip()[:32],
            "message": str(item.get("message", ""))[:4000]
            if isinstance(item.get("message", ""), (str, int, float, bool))
            else "",
            "at_all": bool(item.get("at_all", False)),
            "sent_count": _safe_expire_value(item.get("sent_count", 0)),
            "last_sent_at": _safe_expire_value(item.get("last_sent_at", 0)),
            "created_at": _safe_expire_value(item.get("created_at", 0)),
        }
        task["last_sent_text"] = _format_runtime_timestamp(
            task["last_sent_at"], "尚未执行"
        )
        task["run_at_text"] = _format_runtime_timestamp(task["run_at"], "")
        if task["run_at"]:
            try:
                task["run_at_input"] = datetime.fromtimestamp(
                    int(task["run_at"])
                ).strftime("%Y-%m-%dT%H:%M")
            except (TypeError, ValueError, OverflowError, OSError):
                task["run_at_input"] = ""
        else:
            task["run_at_input"] = ""
        task["created_text"] = _format_runtime_timestamp(task["created_at"], "-")
        task["enabled"] = bool(item.get("enabled", True))
        tasks.append(task)
    return tasks


def _validate_remote_cron(expression: str) -> None:
    parts = expression.strip().split()
    if len(parts) != 5:
        raise PortalError("Cron 必须是五段：分 时 日 月 周")
    limits = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
    token_re = re.compile(r"^(?:\*|\d+|\d+-\d+)(?:/\d+)?$")
    for field, (minimum, maximum) in zip(parts, limits):
        for token in field.split(","):
            if not token_re.fullmatch(token):
                raise PortalError("Cron 格式无效，仅支持 *、步长、范围和列表")
            base, _, step_text = token.partition("/")
            if step_text and int(step_text) <= 0:
                raise PortalError("Cron 步长必须大于 0")
            if base == "*":
                continue
            bounds = [int(value) for value in base.split("-")]
            if any(value < minimum or value > maximum for value in bounds):
                raise PortalError("Cron 字段超出有效范围")
            if len(bounds) == 2 and bounds[0] > bounds[1]:
                raise PortalError("Cron 范围起始值不能大于结束值")


def _stable_timed_task_id(
    group_id: str,
    payload: dict[str, Any],
) -> str:
    """Return a retry-stable task id for one logical group task.

    A proxy timeout can happen after MySQL has committed but before the
    browser receives its redirect.  The retry must not create a second timed
    task, even if the user refreshes the page before retrying.  Use the
    normalized task content as its identity: adding an identical schedule to
    the same group would otherwise make the Windows bot run it twice.

    The digest is safe to store in the Windows-bound configuration and keeps
    portal request details out of the bot's group JSON.
    """
    stable_payload = {
        "group_id": str(group_id),
        "name": str(payload.get("name", "")).strip(),
        "cron_expr": str(payload.get("cron_expr", "")).strip(),
        "run_at": str(payload.get("run_at", "")).strip(),
        "message": str(payload.get("message", "")).strip(),
        "action": str(payload.get("action", "message")),
        "at_all": bool(payload.get("at_all")),
    }
    encoded = json.dumps(
        stable_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{group_id}_{hashlib.sha256(encoded).hexdigest()[:32]}"


def add_timed_task(
    group_id: str,
    payload: dict[str, Any],
    operator_id: str,
    db: Session | None = None,
) -> dict:
    action = payload.get("action", "message")
    if action not in {"message", "random_music", "hourly_time", "whole_ban", "whole_unban"}:
        raise PortalError("定时任务类型无效")
    message = str(payload.get("message", "")).strip()
    if action == "message" and not message:
        raise PortalError("定时播报内容不能为空")
    if len(message) > 4000:
        raise PortalError("定时消息内容不能超过 4000 字")
    if action == "random_music":
        pool = [
            item.strip()
            for item in re.split(r"[,|，｜\n\r]+", message)
            if item.strip()
        ]
        if not pool:
            pool = []
        if len(pool) > 50:
            raise PortalError("随机音乐最多设置 50 个关键词")
        if any(len(item) > 80 for item in pool):
            raise PortalError("每个随机音乐关键词不能超过 80 个字符")
    cron_expr = str(payload.get("cron_expr", "")).strip()
    run_at_text = str(payload.get("run_at", "") or "").strip()
    if action == "hourly_time":
        run_at_text = ""
        cron_expr = "0 * * * *"
    run_at_epoch = 0
    if run_at_text:
        try:
            parsed_run_at = datetime.strptime(
                run_at_text.replace("T", " "), "%Y-%m-%d %H:%M"
            )
            run_at_epoch = int(parsed_run_at.replace(tzinfo=None).timestamp())
        except ValueError as exc:
            raise PortalError("一次性发送时间格式应为 YYYY-MM-DD HH:MM") from exc
        if run_at_epoch <= int(time.time()):
            raise PortalError("一次性发送时间必须晚于当前时间")
        cron_expr = ""
    if remote_data_mode():
        if not run_at_epoch:
            _validate_remote_cron(cron_expr)
        remote_db = _require_remote_db(db)
        task_id = _stable_timed_task_id(group_id, payload)
        result: dict[str, Any] = {}

        def mutate(config):
            messages = deepcopy(config.get("timed_messages", []))
            if not isinstance(messages, list):
                messages = []
            for existing in messages:
                if isinstance(existing, dict) and str(existing.get("id")) == task_id:
                    # The same request already committed before a browser/proxy
                    # timeout.  Return its original result without producing a
                    # second portal revision or a duplicate Windows task.
                    result.update(deepcopy(existing))
                    return config
            entry = {
                "id": task_id,
                "message": message,
                "cron_expr": cron_expr,
                "run_at": run_at_epoch,
                "one_time": bool(run_at_epoch),
                "at_all": bool(payload.get("at_all")),
                "enabled": True,
                "created_by": operator_id,
                "created_at": int(time.time()),
                "last_sent_at": 0,
                "sent_count": 0,
                "name": str(payload.get("name", "")).strip()[:100]
                or f"定时消息{len(messages) + 1}",
        "action": action,
            }
            messages.append(entry)
            config["timed_messages"] = messages
            result.update(deepcopy(entry))
            return config

        remote_store.mutate_web_config(remote_db, group_id, int(operator_id), mutate)
        return result

    module = plugin_module("timed_message")
    result = module.add_timed_message(
        group_id,
        message,
        cron_expr,
        at_all=bool(payload.get("at_all")),
        enabled=True,
        operator_id=operator_id,
        name=str(payload.get("name", "")).strip()[:100],
        action=action,
        run_at=run_at_epoch,
    )
    if not result.get("success"):
        raise PortalError(result.get("message", "添加定时任务失败"))
    return result


def delete_timed_task(
    group_id: str, task_id: str, db: Session | None = None, updated_by: int | None = None
) -> None:
    if remote_data_mode():
        remote_db = _require_remote_db(db)

        def mutate(config):
            messages = config.get("timed_messages", [])
            if not isinstance(messages, list):
                messages = []
            # DELETE is intentionally retry-safe: a timeout after a successful
            # commit must not turn a second click into a misleading failure.
            config["timed_messages"] = [
                item for item in messages
                if not isinstance(item, dict) or str(item.get("id")) != task_id
            ]
            return config

        remote_store.mutate_web_config(remote_db, group_id, updated_by, mutate)
        return
    module = plugin_module("timed_message")
    if not module.get_timed_message(group_id, task_id):
        return
    module.delete_timed_message(group_id, task_id)


def update_timed_task(
    group_id: str,
    task_id: str,
    payload: dict[str, Any],
    operator_id: str,
    db: Session | None = None,
) -> dict:
    """Update mutable fields of an existing timed task in place.

    The task id and runtime counters are intentionally preserved, so editing a
    schedule does not reset its execution history or create a duplicate task.
    """
    action = payload.get("action", "message")
    if action not in {"message", "random_music", "hourly_time", "whole_ban", "whole_unban"}:
        raise PortalError("定时任务类型无效")
    message = str(payload.get("message", "")).strip()
    if action == "message" and not message:
        raise PortalError("定时播报内容不能为空")
    if len(message) > 4000:
        raise PortalError("定时消息内容不能超过 4000 字")
    if action == "random_music":
        pool = [item.strip() for item in re.split(r"[,|，｜\n\r]+", message) if item.strip()]
        if not pool:
            pool = []
        if len(pool) > 50 or any(len(item) > 80 for item in pool):
            raise PortalError("随机音乐最多设置 50 个关键词，且每项不能超过 80 个字符")
    cron_expr = str(payload.get("cron_expr", "")).strip()
    run_at_text = str(payload.get("run_at", "") or "").strip()
    if action == "hourly_time":
        run_at_text = ""
        cron_expr = "0 * * * *"
    run_at_epoch = 0
    if run_at_text:
        try:
            parsed_run_at = datetime.strptime(
                run_at_text.replace("T", " "), "%Y-%m-%d %H:%M"
            )
            run_at_epoch = int(parsed_run_at.replace(tzinfo=None).timestamp())
        except ValueError as exc:
            raise PortalError("一次性发送时间格式应为 YYYY-MM-DD HH:MM") from exc
        if run_at_epoch <= int(time.time()):
            raise PortalError("一次性发送时间必须晚于当前时间")
        cron_expr = ""
    elif not cron_expr:
        if action == "hourly_time":
            cron_expr = "0 * * * *"
        else:
            raise PortalError("请填写 Cron 或一次性发送时间")
    if not run_at_epoch:
        _validate_remote_cron(cron_expr)

    if remote_data_mode():
        remote_db = _require_remote_db(db)
        result: dict[str, Any] = {}

        def mutate(config):
            messages = deepcopy(config.get("timed_messages", []))
            if not isinstance(messages, list):
                messages = []
            for task in messages:
                if isinstance(task, dict) and str(task.get("id")) == str(task_id):
                    task.update(
                        {
                            "name": str(payload.get("name", "")).strip()[:100]
                            or str(task.get("name", "定时任务")),
                            "message": message,
                            "cron_expr": cron_expr,
                            "run_at": run_at_epoch,
                            "one_time": bool(run_at_epoch),
                            "at_all": bool(payload.get("at_all")),
                            "action": action,
                        }
                    )
                    result.update(deepcopy(task))
                    config["timed_messages"] = messages
                    return config
            raise PortalError("定时任务不存在")

        remote_store.mutate_web_config(remote_db, group_id, int(operator_id), mutate)
        return result

    module = plugin_module("timed_message")
    if not module.get_timed_message(group_id, task_id):
        raise PortalError("定时任务不存在")
    if not module.update_timed_message(
        group_id,
        task_id,
        name=str(payload.get("name", "")).strip()[:100],
        message=message,
        cron_expr=cron_expr,
        run_at=run_at_epoch,
        one_time=bool(run_at_epoch),
        at_all=bool(payload.get("at_all")),
        action=action,
    ):
        raise PortalError("定时任务更新失败")
    return module.get_timed_message(group_id, task_id)


def toggle_timed_task(
    group_id: str,
    task_id: str,
    db: Session | None = None,
    updated_by: int | None = None,
    enabled: bool | None = None,
) -> bool:
    """Set a timed task state, or toggle it for older callers.

    The group page always submits the desired state.  Unlike a blind toggle,
    submitting an explicit target is safe to repeat after a reverse-proxy
    timeout: a retry cannot accidentally flip the task back.
    """
    if remote_data_mode():
        remote_db = _require_remote_db(db)
        result_enabled: list[bool] = []

        def mutate(config):
            messages = deepcopy(config.get("timed_messages", []))
            if not isinstance(messages, list):
                messages = []
            for task in messages:
                if isinstance(task, dict) and str(task.get("id")) == task_id:
                    current_enabled = bool(task.get("enabled", True))
                    target_enabled = (
                        (not current_enabled)
                        if enabled is None
                        else bool(enabled)
                    )
                    result_enabled.append(target_enabled)
                    if current_enabled != target_enabled:
                        task["enabled"] = target_enabled
                    config["timed_messages"] = messages
                    return config
            raise PortalError("定时任务不存在")

        remote_store.mutate_web_config(remote_db, group_id, updated_by, mutate)
        return result_enabled[0]
    module = plugin_module("timed_message")
    task = module.get_timed_message(group_id, task_id)
    if not task:
        raise PortalError("定时任务不存在")
    current_enabled = bool(task.get("enabled", True))
    enabled = (not current_enabled) if enabled is None else bool(enabled)
    if current_enabled == enabled:
        return enabled
    module.enable_timed_message(group_id, task_id, enabled)
    return enabled


def add_auto_reply(
    group_id: str,
    reply_type: str,
    key: str,
    value: str,
    db: Session | None = None,
    updated_by: int | None = None,
    *,
    image_assets: list[dict[str, object]] | None = None,
    clear_images: bool = False,
    probability: object | None = None,
    max_uses: object | None = None,
) -> None:
    key = key.strip()
    value = value.strip()
    if not key:
        raise PortalError("触发词不能为空")
    if len(key) > 300 or len(value) > 4000:
        raise PortalError("自动回复内容过长")
    if reply_type == "regex":
        try:
            re.compile(key)
        except re.error:
            raise PortalError("正则表达式无效") from None
    elif reply_type not in {"exact", "fuzzy"}:
        raise PortalError("回复匹配类型无效")
    if remote_data_mode():
        remote_db = _require_remote_db(db)
        submitted_assets: list[dict[str, object]] | None = None
        if image_assets is not None:
            try:
                submitted_assets = reply_asset_service.verify_asset_references_for_group(
                    remote_db, group_id, image_assets
                )
            except ValueError as exc:
                raise PortalError(str(exc)) from None
        changed = False

        def mutate(config):
            nonlocal changed
            replies = deepcopy(config.get("replies", {}))
            if not isinstance(replies, dict):
                replies = {}
            replies.setdefault("exact", {})
            replies.setdefault("fuzzy", [])
            replies.setdefault("fuzzy_keywords", {})
            replies.setdefault("regex", [])
            existing: dict[str, Any] | None = None
            regex_entry: dict[str, Any] | None = None
            if reply_type == "exact":
                candidate = replies["exact"].get(key)
                existing = candidate if isinstance(candidate, dict) else None
            elif reply_type == "fuzzy":
                candidate = replies["fuzzy_keywords"].get(key)
                existing = candidate if isinstance(candidate, dict) else None
            else:
                for entry in replies["regex"]:
                    if isinstance(entry, dict) and entry.get("pattern") == key:
                        regex_entry = entry
                        candidate = entry.get("reply")
                        existing = candidate if isinstance(candidate, dict) else None
                        break

            # A text-only edit deliberately keeps a prior image reply.  Uploading
            # new pictures replaces the prior set, while the explicit checkbox
            # removes both portal assets and legacy image fields.
            data = deepcopy(existing) if isinstance(existing, dict) else {}
            if submitted_assets is not None:
                data.update(
                    {
                        "has_images": bool(submitted_assets),
                        "image_portal_assets": submitted_assets,
                        # The Windows client atomically replaces these with its
                        # cache:// references after the next authenticated pull.
                        "image_cache_refs": [""] * len(submitted_assets),
                        "image_cache_status": "pending" if submitted_assets else "",
                        "image_urls": [],
                        "image_source_urls": [],
                    }
                )
            elif clear_images:
                for field in (
                    "has_images",
                    "image_portal_assets",
                    "image_cache_refs",
                    "image_cache_status",
                    "image_cached_at",
                    "image_urls",
                    "image_source_urls",
                ):
                    data.pop(field, None)
                # Preserve an explicit deletion marker in the remote projection.
                # Without it, a Windows-side cached image could be mistaken for an
                # omitted legacy field and be restored during the next web save.
                data["has_images"] = False
                data["image_portal_assets"] = []
                data["image_cache_status"] = ""
            portal_assets = reply_asset_service.normalize_asset_references(
                data.get("image_portal_assets")
            )
            has_existing_image = bool(data.get("has_images")) or bool(portal_assets)
            if not value and not has_existing_image:
                raise PortalError("请填写回复内容或至少上传一张图片")
            data["reply"] = value
            data["type"] = reply_type
            # An omitted value is a backward-compatible text/image-only edit:
            # retain the rule's existing delivery policy rather than resetting it.
            data["probability"] = _reply_probability(
                data.get("probability", 100) if probability is None else probability,
                strict=probability is not None,
            )
            data["max_uses"] = _reply_max_uses(
                data.get("max_uses", 0) if max_uses is None else max_uses,
                strict=max_uses is not None,
            )
            data.setdefault("created_at", int(time.time()))

            if reply_type == "exact":
                replies["exact"][key] = data
            elif reply_type == "fuzzy":
                if key not in replies["fuzzy"]:
                    replies["fuzzy"].append(key)
                replies["fuzzy_keywords"][key] = data
            elif regex_entry is not None:
                regex_entry["reply"] = data
            else:
                replies["regex"].append({"pattern": key, "reply": data})
            # A browser/proxy timeout can cause the same form to be submitted
            # again.  If the canonical reply projection is already identical,
            # keep the operation idempotent and avoid another sync revision.
            if config.get("replies") == replies:
                return config
            config["replies"] = replies
            changed = True
            return config

        config = remote_store.mutate_web_config(remote_db, group_id, updated_by, mutate)
        if changed:
            reply_asset_service.cleanup_unreferenced_reply_assets(
                remote_db, group_id, config
            )
        return

    if image_assets is not None or clear_images:
        raise PortalError("图片自动回复仅支持已关联 Windows 机器人的 Linux 网页")
    if not value:
        raise PortalError("回复内容不能为空")
    module = plugin_module("reply")
    if reply_type == "regex":
        if not module.add_regex_reply(group_id, key, value):
            raise PortalError("正则表达式无效")
    elif reply_type in {"exact", "fuzzy"}:
        module.add_reply(group_id, key, value, exact=reply_type == "exact")
    else:
        raise PortalError("回复匹配类型无效")


def delete_auto_reply(
    group_id: str,
    reply_type: str,
    key: str,
    db: Session | None = None,
    updated_by: int | None = None,
) -> None:
    if reply_type not in {"exact", "fuzzy", "regex"}:
        raise PortalError("回复匹配类型无效")
    if remote_data_mode():
        remote_db = _require_remote_db(db)
        def mutate(config):
            replies = deepcopy(config.get("replies", {}))
            if not isinstance(replies, dict):
                replies = {}
            if reply_type == "exact":
                replies.setdefault("exact", {}).pop(key, None)
            elif reply_type == "fuzzy":
                replies["fuzzy"] = [
                    item for item in replies.get("fuzzy", []) if item != key
                ]
                replies.setdefault("fuzzy_keywords", {}).pop(key, None)
            else:
                replies["regex"] = [
                    item for item in replies.get("regex", [])
                    if not isinstance(item, dict) or item.get("pattern") != key
                ]
            config["replies"] = replies
            return config

        config = remote_store.mutate_web_config(
            remote_db, group_id, updated_by, mutate
        )
        reply_asset_service.cleanup_unreferenced_reply_assets(
            remote_db, group_id, config
        )
        return
    plugin_module("reply").remove_reply(group_id, key, reply_type)


def _normalized_sub_admin_permissions(permissions: list[object] | None) -> list[str]:
    """Validate the exact permission subset supported by Windows AstrBot."""
    # New sub-admins are reply operators by default.  Moderation permissions
    # remain opt-in for an owner who explicitly selects them, but a normal
    # member added from the portal must not unexpectedly receive kick/mute/
    # blacklist access just because the browser omitted checkbox values.
    if permissions is None:
        return ["reply"]
    selected = {str(permission).strip() for permission in permissions}
    unknown = selected.difference(SUB_ADMIN_PERMISSION_KEYS)
    if unknown:
        raise PortalError("分群管理员权限包含未知项目")
    result = [permission for permission in SUB_ADMIN_PERMISSION_KEYS if permission in selected]
    if not result:
        raise PortalError("请至少选择一项分群管理员权限")
    return result


def _portal_sub_admin_record(item: object) -> dict[str, Any] | None:
    """Normalize an old or current sub-admin entry for the remote portal.

    The Windows plugin historically stored a plain QQ number.  The portal
    stores a richer record so it can edit the scoped permissions.  Old plain
    QQ records now use the same reply-only default as the Windows executor,
    instead of being displayed as moderation-capable when they are not.
    """
    raw_user_id = item.get("user_id") if isinstance(item, dict) else item
    user_id = str(raw_user_id or "").strip()
    if not re.fullmatch(r"\d{5,20}", user_id):
        return None

    if isinstance(item, dict):
        raw_permissions = item.get("permissions")
        added_by = str(item.get("added_by", ""))[:32]
        try:
            added_at = int(item.get("added_at", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            added_at = 0
    else:
        raw_permissions = None
        added_by = ""
        added_at = 0

    # Missing permissions is the pre-scoped legacy representation.  The
    # current Windows runtime treats that representation as reply-only, so the
    # web view must use the same safe default until an owner selects more.
    if raw_permissions is None:
        permissions = ["reply"]
    elif isinstance(raw_permissions, (list, tuple, set)):
        selected = {str(permission).strip() for permission in raw_permissions}
        permissions = [
            permission
            for permission in SUB_ADMIN_PERMISSION_KEYS
            if permission in selected
        ]
    else:
        permissions = []
    return {
        "user_id": user_id,
        "added_by": added_by,
        "added_at": added_at,
        "permissions": permissions,
    }


def _normalized_portal_sub_admins(raw_admins: object) -> list[dict[str, Any]]:
    """Return one editable, canonical record per QQ number.

    A duplicate can exist after a previous web version appended a dict record
    beside a legacy string.  Keep the first record because that is also the
    record the existing Windows permission helper currently reads; saving an
    edit then replaces the duplicate list with this single canonical record.
    """
    if not isinstance(raw_admins, list):
        return []
    records: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for item in raw_admins:
        record = _portal_sub_admin_record(item)
        if record is None:
            continue
        user_id = record["user_id"]
        if user_id not in positions:
            positions[user_id] = len(records)
            records.append(record)
    return records


def add_group_sub_admin(
    group_id: str,
    user_id: str,
    operator_id: str,
    db: Session | None = None,
    permissions: list[object] | None = None,
) -> None:
    user_id = str(user_id or "").strip()
    if not re.fullmatch(r"\d{5,20}", user_id):
        raise PortalError("请输入正确的管理员 QQ 号")
    selected_permissions = _normalized_sub_admin_permissions(permissions)
    if remote_data_mode():
        remote_db = _require_remote_db(db)

        def mutate(config):
            raw_admins = config.get("sub_admins", [])
            admins = _normalized_portal_sub_admins(raw_admins)
            existing = next(
                (
                    item
                    for item in admins
                    if isinstance(item, dict) and str(item.get("user_id")) == user_id
                ),
                None,
            )
            if existing is not None:
                # Treat a timeout retry as an update to the requested permission
                # subset instead of reporting a false "already exists" error.
                existing["permissions"] = selected_permissions
            else:
                admins.append(
                    {
                        "user_id": user_id,
                        "added_by": operator_id,
                        "added_at": int(time.time()),
                        "permissions": selected_permissions,
                    }
                )
            config["sub_admins"] = admins
            return config

        remote_store.mutate_web_config(
            remote_db, group_id, int(operator_id), mutate
        )
        return
    module = plugin_module("sub_admin")
    # The Windows module's add operation preserves its existing behaviour,
    # then its supported setter applies the portal-selected subset.  Repeating
    # a timed-out request simply sets the same permissions again.
    module.add_sub_admin(group_id, user_id, operator_id)
    module.set_sub_admin_permissions(group_id, user_id, selected_permissions)


def delete_group_sub_admin(
    group_id: str, user_id: str, db: Session | None = None, updated_by: int | None = None
) -> None:
    if remote_data_mode():
        remote_db = _require_remote_db(db)
        normalized_user_id = str(user_id or "").strip()

        def mutate(config):
            admins = _normalized_portal_sub_admins(config.get("sub_admins", []))
            config["sub_admins"] = [
                item for item in admins if item["user_id"] != normalized_user_id
            ]
            return config

        remote_store.mutate_web_config(remote_db, group_id, updated_by, mutate)
        return
    plugin_module("sub_admin").remove_sub_admin(group_id, user_id)


# Keep the older service name available for callers outside the web routes.
remove_group_sub_admin = delete_group_sub_admin


def add_user_reply(
    group_id: str,
    user_id: str,
    messages: list[str],
    at_user: bool,
    db: Session | None = None,
    updated_by: int | None = None,
    enabled: bool = True,
) -> None:
    user_id = str(user_id or "").strip()
    if not re.fullmatch(r"\d{5,20}", user_id):
        raise PortalError("请输入正确的用户 QQ 号")
    clean = [message.strip() for message in messages if message.strip()]
    if not clean:
        raise PortalError("至少填写一条回复")
    if len(clean) > 30 or any(len(message) > 1000 for message in clean):
        raise PortalError("专属回复最多 30 条，每条不超过 1000 字")
    if remote_data_mode():
        remote_db = _require_remote_db(db)

        def mutate(config):
            replies = deepcopy(config.get("player_replies", {}))
            if not isinstance(replies, dict):
                replies = {}
            replies[user_id] = {
                "messages": clean,
                "at_user": at_user,
                "enabled": bool(enabled),
            }
            config["player_replies"] = replies
            return config

        remote_store.mutate_web_config(remote_db, group_id, updated_by, mutate)
        return
    plugin_module("player_reply").add_player_reply(group_id, user_id, clean, at_user, bool(enabled))


def delete_user_reply(
    group_id: str, user_id: str, db: Session | None = None, updated_by: int | None = None
) -> None:
    if remote_data_mode():
        remote_db = _require_remote_db(db)

        def mutate(config):
            replies = deepcopy(config.get("player_replies", {}))
            if not isinstance(replies, dict):
                replies = {}
            replies.pop(user_id, None)
            config["player_replies"] = replies
            return config

        remote_store.mutate_web_config(remote_db, group_id, updated_by, mutate)
        return
    plugin_module("player_reply").remove_player_reply(group_id, user_id)
