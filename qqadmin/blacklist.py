import json
import time
from .group_config import get_group_data, set_group_data, update_group_data, load_group_config, init_group_config

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
    def _remove(blacklist):
        if blacklist is None or user_id not in blacklist:
            return blacklist
        
        blacklist[user_id]["is_active"] = False
        blacklist[user_id]["unban_reason"] = unban_reason
        blacklist[user_id]["unbanned_by"] = unbanned_by
        blacklist[user_id]["unbanned_at"] = int(time.time())
        return blacklist
    
    update_group_data(group_id, "blacklist", _remove)
    return True

def is_in_blacklist(group_id: str, user_id: str, check_active: bool = True) -> bool:
    """检查用户是否在黑名单中"""
    blacklist = get_group_data(group_id, "blacklist", {})
    if not blacklist or user_id not in blacklist:
        return False

    if check_active:
        entry = blacklist[user_id]
        if not entry.get("is_active", True):
            return False

        expire_time = entry.get("expire_time", 0)
        if expire_time > 0 and int(time.time()) > expire_time:
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
        if expire_time > 0 and now > expire_time:
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

    active = [r for r in records if r.get("is_active", True)]
    active_permanent = [r for r in active if r.get("expire_time", 0) == 0]
    active_temporary = [r for r in active if r.get("expire_time", 0) > 0]
    expired = [r for r in records if not r.get("is_active", True)]

    return {
        "total_records": len(records),
        "active_count": len(active),
        "active_permanent": len(active_permanent),
        "active_temporary": len(active_temporary),
        "expired_count": len(expired)
    }

def update_blacklist_duration(group_id: str, user_id: str, additional_duration: int) -> bool:
    """更新黑名单用户时长"""
    def _update(blacklist):
        if blacklist is None or user_id not in blacklist:
            return blacklist
        
        current_expire = blacklist[user_id].get("expire_time", 0)
        if current_expire == 0:
            blacklist[user_id]["expire_time"] = int(time.time()) + additional_duration
        else:
            blacklist[user_id]["expire_time"] += additional_duration
        blacklist[user_id]["duration"] += additional_duration
        return blacklist
    
    update_group_data(group_id, "blacklist", _update)
    return True

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
        "notify_on_kick": True,
        "kick_reason": "已被加入黑名单",
        "max_auto_kick_per_day": 10
    }
    return get_group_data(group_id, "blacklist_settings", default)

def set_blacklist_settings(group_id: str, key: str, value):
    def _update(settings):
        if settings is None:
            settings = {}
        settings[key] = value
        return settings
    update_group_data(group_id, "blacklist_settings", _update)
