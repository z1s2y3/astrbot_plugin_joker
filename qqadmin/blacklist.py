import json
import time
import os
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api import logger
from .group_config import get_group_data, set_group_data, update_group_data

def get_global_blacklist_file():
    data_path = Path(get_astrbot_data_path()) / "plugin_data" / "global_blacklist"
    data_path.mkdir(parents=True, exist_ok=True)
    return os.path.join(data_path, "global_blacklist.json")

def load_global_blacklist() -> dict:
    """加载全局黑名单"""
    blacklist_file = get_global_blacklist_file()
    try:
        if os.path.exists(blacklist_file):
            with open(blacklist_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"加载全局黑名单失败: {e}")
    return {}

def save_global_blacklist(blacklist: dict):
    """保存全局黑名单"""
    blacklist_file = get_global_blacklist_file()
    try:
        with open(blacklist_file, "w", encoding="utf-8") as f:
            json.dump(blacklist, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存全局黑名单失败: {e}")

def add_to_blacklist(group_id: str, user_id: str, reason: str = "", operator_id: str = "",
                     ban_type: str = "temporary", duration: int = 0) -> bool:
    """添加用户到黑名单"""
    def _add(blacklist):
        if blacklist is None:
            blacklist = {}
        
        expire_time = 0
        if ban_type == "temporary" and duration > 0:
            expire_time = int(time.time()) + duration

        blacklist[user_id] = {
            "reason": reason,
            "added_by": operator_id,
            "added_at": int(time.time()),
            "ban_type": ban_type,
            "expire_time": expire_time,
            "duration": duration,
            "is_active": True,
            "unban_reason": "",
            "unbanned_by": "",
            "unbanned_at": 0
        }
        return blacklist
    
    update_group_data(group_id, "blacklist", _add)
    return True

def remove_from_blacklist(group_id: str, user_id: str, unban_reason: str = "",
                         unbanned_by: str = "") -> bool:
    """从黑名单移除用户"""
    changed = False

    def _remove(blacklist):
        nonlocal changed
        if blacklist is None or user_id not in blacklist:
            return blacklist
        
        blacklist[user_id]["is_active"] = False
        blacklist[user_id]["unban_reason"] = unban_reason
        blacklist[user_id]["unbanned_by"] = unbanned_by
        blacklist[user_id]["unbanned_at"] = int(time.time())
        changed = True
        return blacklist
    
    update_group_data(group_id, "blacklist", _remove)
    return changed


def delete_blacklist_record(group_id: str, user_id: str) -> bool:
    """Physically delete one current-group blacklist record.

    The normal ``/删黑`` command keeps an inactive audit row for history.  The
    Linux blacklist page has a separate delete action, so it must remove the
    row from the displayed source instead of only marking it inactive.  Global
    blacklist records intentionally do not use this function; their web action
    creates a current-group exception and keeps the global record intact.
    """
    changed = False

    def _delete(blacklist):
        nonlocal changed
        if not isinstance(blacklist, dict) or user_id not in blacklist:
            return blacklist
        blacklist.pop(user_id, None)
        changed = True
        return blacklist

    update_group_data(group_id, "blacklist", _delete)
    if not changed:
        return False
    # Verify the durable store after the write.  A reported change is not
    # enough when a concurrent sync or a failed local write restored the row.
    current = get_group_data(group_id, "blacklist", {})
    return not isinstance(current, dict) or user_id not in current

def is_in_blacklist(group_id: str, user_id: str, check_active: bool = True) -> bool:
    """检查用户是否在黑名单中"""
    blacklist = get_group_data(group_id, "blacklist", {})
    if not blacklist or user_id not in blacklist:
        return False

    if check_active:
        entry = blacklist[user_id]
        if not entry.get("is_active", True):
            return False

        # 检查临时黑名单是否过期
        expire_time = entry.get("expire_time", 0)
        if expire_time > 0:
            current_time = int(time.time())
            # 使用 >= 确保精确的过期时间
            if current_time >= expire_time:
                return False

    return True

def get_blacklist_info(group_id: str, user_id: str) -> dict:
    """获取黑名单用户详情"""
    blacklist = get_group_data(group_id, "blacklist", {})
    if user_id in blacklist:
        return blacklist[user_id]
    return None

def list_blacklist(group_id: str, include_inactive: bool = False) -> list:
    """列出黑名单用户"""
    blacklist = get_group_data(group_id, "blacklist", {})
    if not blacklist:
        return []

    now = int(time.time())
    result = []

    for user_id, info in blacklist.items():
        if not include_inactive and not info.get("is_active", True):
            continue

        expire_time = info.get("expire_time", 0)
        if expire_time > 0 and now >= expire_time:
            continue

        result.append({
            "user_id": user_id,
            **info,
            "remaining_time": max(0, expire_time - now) if expire_time > 0 else 0
        })

    return result

def list_all_blacklist_records(group_id: str) -> list:
    """列出所有黑名单记录（包括已移除的）"""
    blacklist = get_group_data(group_id, "blacklist", {})
    if not blacklist:
        return []

    return [
        {"user_id": user_id, **info}
        for user_id, info in blacklist.items()
    ]

def clear_blacklist(group_id: str):
    """清空指定群的黑名单"""
    set_group_data(group_id, "blacklist", {})

def get_blacklist_statistics(group_id: str) -> dict:
    """获取黑名单统计"""
    records = list_all_blacklist_records(group_id)
    now = int(time.time())

    def _expire_time(record):
        try:
            return max(0, int(record.get("expire_time", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    active = [
        r for r in records
        if r.get("is_active", True) and (_expire_time(r) == 0 or _expire_time(r) > now)
    ]
    active_permanent = [r for r in active if r.get("expire_time", 0) == 0]
    active_temporary = [r for r in active if r.get("expire_time", 0) > 0]
    expired = [r for r in records if not r.get("is_active", True) or (
        _expire_time(r) > 0 and _expire_time(r) <= now
    )]

    return {
        "total_records": len(records),
        "active_count": len(active),
        "active_permanent": len(active_permanent),
        "active_temporary": len(active_temporary),
        "expired_count": len(expired)
    }

def update_blacklist_duration(group_id: str, user_id: str, additional_duration: int) -> bool:
    """更新黑名单用户时长"""
    changed = False

    def _update(blacklist):
        nonlocal changed
        if blacklist is None or user_id not in blacklist:
            return blacklist
        
        current_expire = blacklist[user_id].get("expire_time", 0)
        if current_expire == 0:
            return blacklist
        try:
            additional = max(0, int(additional_duration))
        except (TypeError, ValueError, OverflowError):
            return blacklist
        if additional == 0:
            return blacklist
        current_expire = max(int(time.time()), int(current_expire))
        blacklist[user_id]["expire_time"] = current_expire + additional
        blacklist[user_id]["duration"] = max(
            0, int(blacklist[user_id].get("duration", 0) or 0)
        ) + additional
        blacklist[user_id]["ban_type"] = "temporary"
        changed = True
        return blacklist
    
    update_group_data(group_id, "blacklist", _update)
    return changed

def is_permanently_banned(group_id: str, user_id: str) -> bool:
    """检查用户是否永久拉黑"""
    info = get_blacklist_info(group_id, user_id)
    if not info:
        return False
    return info.get("ban_type") == "permanent" and info.get("is_active", True)

def load_blacklist(group_id: str) -> dict:
    return get_group_data(group_id, "blacklist", {})

def save_blacklist(group_id: str, blacklist: dict):
    set_group_data(group_id, "blacklist", blacklist)

def get_blacklist_settings(group_id: str) -> dict:
    default = {
        "enabled": True,
        "auto_kick": True,
        "auto_kick_on_join": True,
        "recall_on_kick": True,
        "notify_on_kick": True,
        "kick_reason": "已被加入黑名单",
        "max_auto_kick_per_day": 10
    }
    stored = get_group_data(group_id, "blacklist_settings", {})
    if not isinstance(stored, dict):
        stored = {}
    # Keep fields from a web save and an older local configuration together.
    # Returning the raw dictionary used to make absent legacy keys look like
    # a disabled switch in command output even though the effective default
    # was enabled.
    return {**default, **stored}


def can_auto_kick_today(group_id: str, now: int | None = None) -> bool:
    """Return whether this group still has automatic-kick quota today.

    ``max_auto_kick_per_day`` is a safety guard for automatic actions.  A
    value of zero deliberately disables automatic kicks while retaining the
    blacklist records and manual moderation commands.
    """

    settings = get_blacklist_settings(group_id)
    try:
        limit = max(0, int(settings.get("max_auto_kick_per_day", 10)))
    except (TypeError, ValueError, OverflowError):
        limit = 10
    if limit == 0:
        return False
    timestamp = int(now if now is not None else time.time())
    day = time.strftime("%Y-%m-%d", time.localtime(timestamp))
    history = get_group_data(group_id, "blacklist_auto_kick_history", {})
    if not isinstance(history, dict):
        return True
    day_record = history.get(day, {})
    if not isinstance(day_record, dict):
        return True
    try:
        used = max(0, int(day_record.get("count", 0)))
    except (TypeError, ValueError, OverflowError):
        used = 0
    return used < limit


def record_auto_kick(group_id: str, user_id: str, now: int | None = None) -> int:
    """Persist one successful automatic kick and return today's count."""

    timestamp = int(now if now is not None else time.time())
    day = time.strftime("%Y-%m-%d", time.localtime(timestamp))

    def _record(history):
        history = history if isinstance(history, dict) else {}
        # Retain only a compact recent window; this is runtime accounting and
        # is intentionally not synchronized to the portal.
        history = {
            key: value
            for key, value in history.items()
            if isinstance(key, str) and key >= time.strftime(
                "%Y-%m-%d", time.localtime(timestamp - 7 * 86400)
            )
        }
        record = history.get(day)
        record = record if isinstance(record, dict) else {}
        try:
            count = max(0, int(record.get("count", 0))) + 1
        except (TypeError, ValueError, OverflowError):
            count = 1
        history[day] = {
            "count": count,
            "last_user_id": str(user_id or "")[:32],
            "updated_at": timestamp,
        }
        return history

    history = update_group_data(group_id, "blacklist_auto_kick_history", _record)
    record = history.get(day, {}) if isinstance(history, dict) else {}
    try:
        return max(0, int(record.get("count", 0)))
    except (TypeError, ValueError, OverflowError):
        return 0

def set_blacklist_settings(group_id: str, key: str, value):
    def _update(settings):
        if settings is None:
            settings = {}
        settings[key] = value
        return settings
    update_group_data(group_id, "blacklist_settings", _update)

# ==================== 全局黑名单功能 ====================

def add_to_global_blacklist(user_id: str, reason: str = "", operator_id: str = "",
                            ban_type: str = "permanent", duration: int = 0) -> bool:
    """添加用户到全局黑名单（仅主人可用）"""
    user_id = str(user_id or "").strip()
    if not user_id.isdigit() or not 5 <= len(user_id) <= 20:
        return False
    blacklist = load_global_blacklist()
    
    expire_time = 0
    if ban_type == "temporary" and duration > 0:
        expire_time = int(time.time()) + duration

    blacklist[user_id] = {
        "reason": reason,
        "added_by": operator_id,
        "added_at": int(time.time()),
        "ban_type": ban_type,
        "expire_time": expire_time,
        "duration": duration,
        "is_active": True,
        "unban_reason": "",
        "unbanned_by": "",
        "unbanned_at": 0
    }
    
    save_global_blacklist(blacklist)
    return True

def remove_from_global_blacklist(user_id: str, unban_reason: str = "",
                                 unbanned_by: str = "") -> bool:
    """从全局黑名单移除用户"""
    blacklist = load_global_blacklist()
    if user_id not in blacklist:
        return False
    
    blacklist[user_id]["is_active"] = False
    blacklist[user_id]["unban_reason"] = unban_reason
    blacklist[user_id]["unbanned_by"] = unbanned_by
    blacklist[user_id]["unbanned_at"] = int(time.time())
    
    save_global_blacklist(blacklist)
    return True

def allow_global_blacklist_in_group(
    user_id: str, group_id: str, reason: str = "", operator_id: str = ""
) -> bool:
    """Allow a global-blacklisted user in one group without lifting globally."""
    user_id = str(user_id or "").strip()
    group_id = str(group_id or "").strip()
    if not user_id.isdigit() or not group_id:
        return False
    blacklist = load_global_blacklist()
    record = blacklist.get(user_id)
    if not isinstance(record, dict):
        return False
    groups = record.get("allowed_groups", [])
    groups = [str(item).strip() for item in groups] if isinstance(groups, list) else []
    if group_id not in groups:
        groups.append(group_id)
    record["allowed_groups"] = groups[-500:]
    record["last_group_override_reason"] = str(reason or "")[:240]
    record["last_group_override_by"] = str(operator_id or "")[:32]
    record["last_group_override_at"] = int(time.time())
    blacklist[user_id] = record
    save_global_blacklist(blacklist)
    return True

def is_in_global_blacklist(
    user_id: str, check_active: bool = True, group_id: str = ""
) -> bool:
    """检查用户是否在全局黑名单中"""
    blacklist = load_global_blacklist()
    if not blacklist or user_id not in blacklist:
        return False

    entry = blacklist[user_id]
    if check_active:
        if not entry.get("is_active", True):
            return False

        expire_time = entry.get("expire_time", 0)
        if expire_time > 0 and int(time.time()) >= expire_time:
            return False
    if group_id:
        allowed_groups = entry.get("allowed_groups", [])
        if isinstance(allowed_groups, list) and str(group_id).strip() in {
            str(item).strip() for item in allowed_groups
        }:
            return False

    return True

def get_global_blacklist_info(user_id: str, group_id: str = "") -> dict:
    """获取全局黑名单用户详情"""
    blacklist = load_global_blacklist()
    if user_id in blacklist:
        entry = dict(blacklist[user_id])
        allowed_groups = entry.get("allowed_groups", [])
        entry["allowed_in_group"] = bool(
            group_id and isinstance(allowed_groups, list)
            and str(group_id).strip() in {str(item).strip() for item in allowed_groups}
        )
        return entry
    return None

def list_global_blacklist(include_inactive: bool = False, group_id: str = "") -> list:
    """列出全局黑名单用户，可按群过滤已允许进入该群的用户。"""
    blacklist = load_global_blacklist()
    if not blacklist:
        return []

    now = int(time.time())
    group_id = str(group_id or "").strip()
    result = []

    for user_id, info in blacklist.items():
        if group_id:
            allowed_groups = info.get("allowed_groups", []) if isinstance(info, dict) else []
            if isinstance(allowed_groups, list) and group_id in {
                str(item).strip() for item in allowed_groups
            }:
                continue
        if not include_inactive and not info.get("is_active", True):
            continue

        expire_time = info.get("expire_time", 0)
        if expire_time > 0 and now >= expire_time:
            continue

        result.append({
            "user_id": user_id,
            **info,
            "remaining_time": max(0, expire_time - now) if expire_time > 0 else 0
        })

    return result

def clear_global_blacklist():
    """清空全局黑名单"""
    save_global_blacklist({})

def get_global_blacklist_statistics() -> dict:
    """获取全局黑名单统计"""
    blacklist = load_global_blacklist()
    now = int(time.time())

    def _active(record: object) -> bool:
        if not isinstance(record, dict) or not record.get("is_active", True):
            return False
        try:
            expire_time = int(record.get("expire_time", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            expire_time = 0
        return expire_time <= 0 or expire_time > now

    active = [r for r in blacklist.values() if _active(r)]
    active_permanent = [r for r in active if r.get("expire_time", 0) == 0]
    active_temporary = [r for r in active if r.get("expire_time", 0) > 0]
    expired = [r for r in blacklist.values() if not _active(r)]

    return {
        "total_records": len(blacklist),
        "active_count": len(active),
        "active_permanent": len(active_permanent),
        "active_temporary": len(active_temporary),
        "expired_count": len(expired)
    }

def update_global_blacklist_duration(user_id: str, additional_duration: int) -> bool:
    """更新全局黑名单用户时长"""
    blacklist = load_global_blacklist()
    if user_id not in blacklist:
        return False
    
    current_expire = blacklist[user_id].get("expire_time", 0)
    if current_expire == 0:
        blacklist[user_id]["expire_time"] = int(time.time()) + additional_duration
    else:
        blacklist[user_id]["expire_time"] += additional_duration
    blacklist[user_id]["duration"] += additional_duration
    
    save_global_blacklist(blacklist)
    return True

def is_permanently_banned_global(user_id: str) -> bool:
    """检查用户是否永久加入全局黑名单"""
    info = get_global_blacklist_info(user_id)
    if not info:
        return False
    return info.get("ban_type") == "permanent" and info.get("is_active", True)
