import json
import time
from .group_config import get_group_data, set_group_data, update_group_data
from .api_client import api_client

def mute_user(group_id, user_id, duration=0, reason="", operator_id="", mute_type="normal"):
    """禁言用户"""
    def _mute(mute_list):
        if mute_list is None:
            mute_list = {}
        
        old_info = mute_list.get(user_id)
        expire_time = 0 if duration == 0 else int(time.time()) + duration
        
        # 只有自动禁言才保留并升级禁言级别，手动禁言重置为1
        if mute_type == "auto" and old_info:
            mute_level = old_info.get("mute_level", 1)
            cumulative_mutes = old_info.get("cumulative_mutes", 0) + 1
        else:
            mute_level = 1
            cumulative_mutes = old_info.get("cumulative_mutes", 0) + 1 if old_info else 1
        
        mute_list[user_id] = {
            "expire_time": expire_time,
            "duration": duration,
            "reason": reason,
            "muted_at": int(time.time()),
            "operator_id": operator_id,
            "mute_type": mute_type,
            "mute_level": mute_level,
            "cumulative_mutes": cumulative_mutes
        }
        return mute_list
    
    update_group_data(group_id, "mute_list", _mute)
    return True

def unmute_user(group_id, user_id):
    """解禁用户"""
    def _unmute(mute_list):
        if mute_list is None or user_id not in mute_list:
            return mute_list
        del mute_list[user_id]
        return mute_list
    
    update_group_data(group_id, "mute_list", _unmute)
    return True

def is_muted(group_id, user_id):
    """检查用户是否被禁言"""
    mute_list = get_group_data(group_id, "mute_list", {})
    if mute_list and user_id in mute_list:
        expire_time = mute_list[user_id].get("expire_time", 0)
        if expire_time == 0 or expire_time > time.time():
            return True
        else:
            unmute_user(group_id, user_id)
    return False

def get_mute_info(group_id, user_id):
    """获取禁言信息"""
    mute_list = get_group_data(group_id, "mute_list", {})
    if mute_list and user_id in mute_list:
        return mute_list[user_id]
    return {}

def list_muted_users(group_id):
    """列出所有被禁言的用户"""
    mute_list = get_group_data(group_id, "mute_list", {})
    if not mute_list:
        return []
    
    result = []
    expired_users = []
    current_time = time.time()
    
    for user_id, info in mute_list.items():
        expire_time = info.get("expire_time", 0)
        if expire_time == 0 or expire_time > current_time:
            result.append({"user_id": user_id, **info})
        else:
            expired_users.append(user_id)
    
    # 清理过期数据
    if expired_users:
        def _clean(mute_list):
            for user_id in expired_users:
                if user_id in mute_list:
                    del mute_list[user_id]
            return mute_list
        update_group_data(group_id, "mute_list", _clean)
    
    return result

def calculate_mute_duration(group_id, user_id):
    """计算禁言时长"""
    return 300  # 默认5分钟

def mute_user_auto(group_id, user_id, reason="", operator_id=""):
    """自动禁言用户"""
    duration = calculate_mute_duration(group_id, user_id)
    return mute_user(group_id, user_id, duration, reason, operator_id, "auto")

def get_mute_remaining_time(group_id, user_id):
    """获取剩余禁言时间"""
    mute_info = get_mute_info(group_id, user_id)
    if not mute_info:
        return 0
    
    expire_time = mute_info.get("expire_time", 0)
    if expire_time == 0:
        return 0
    
    remaining = expire_time - int(time.time())
    return max(0, remaining)

def get_mute_level(group_id, user_id):
    """获取禁言级别"""
    mute_info = get_mute_info(group_id, user_id)
    return mute_info.get("mute_level", 1)

def increase_mute_level(group_id, user_id):
    """增加禁言级别"""
    def _increase(mute_list):
        if mute_list is None:
            mute_list = {}
        
        if user_id not in mute_list:
            mute_list[user_id] = {"mute_level": 1}
        
        current_level = mute_list[user_id].get("mute_level", 1)
        new_level = current_level + 1
        mute_list[user_id]["mute_level"] = new_level
        return mute_list
    
    update_group_data(group_id, "mute_list", _increase)
    return get_mute_level(group_id, user_id)

def get_mute_statistics(group_id):
    """获取禁言统计"""
    mute_list = get_group_data(group_id, "mute_list", {})
    if not mute_list:
        return {"total_muted": 0, "currently_muted": 0, "total_mute_times": 0}
    
    current_time = time.time()
    expired_users = []
    total_muted = len(mute_list)
    currently_muted = 0
    total_mute_times = 0
    
    for user_id, info in mute_list.items():
        total_mute_times += info.get("cumulative_mutes", 1)
        expire_time = info.get("expire_time", 0)
        if expire_time == 0 or expire_time > current_time:
            currently_muted += 1
        else:
            expired_users.append(user_id)
    
    # 清理过期数据
    if expired_users:
        def _clean(mute_list):
            for user_id in expired_users:
                if user_id in mute_list:
                    del mute_list[user_id]
            return mute_list
        update_group_data(group_id, "mute_list", _clean)
    
    return {"total_muted": total_muted, "currently_muted": currently_muted, "total_mute_times": total_mute_times}

def clean_expired_mutes(group_id):
    """清理过期禁言"""
    mute_list = get_group_data(group_id, "mute_list", {})
    if not mute_list:
        return 0
    
    current_time = time.time()
    expired_users = []
    
    for user_id, info in mute_list.items():
        expire_time = info.get("expire_time", 0)
        if expire_time > 0 and expire_time <= current_time:
            expired_users.append(user_id)
    
    if expired_users:
        def _clean(mute_list):
            for user_id in expired_users:
                if user_id in mute_list:
                    del mute_list[user_id]
            return mute_list
        update_group_data(group_id, "mute_list", _clean)
    
    return len(expired_users)

async def mute_user_api(group_id, user_id, duration=0, reason=""):
    """API禁言用户"""
    result = await api_client.set_group_ban(group_id, user_id, duration)
    if result.get("success"):
        mute_user(group_id, user_id, duration, reason, "system", "api")
        return True
    return False

async def unmute_user_api(group_id, user_id):
    """API解禁用户"""
    result = await api_client.set_group_ban(group_id, user_id, 0)
    if result.get("success"):
        unmute_user(group_id, user_id)
        return True
    return False

async def set_group_whole_ban_api(group_id, enable=True):
    """API全群禁言"""
    result = await api_client.set_group_whole_ban(group_id, enable)
    if result.get("success"):
        return True
    return False

def load_mute_list(group_id):
    return get_group_data(group_id, "mute_list", {})

def save_mute_list(group_id, mute_list):
    set_group_data(group_id, "mute_list", mute_list)

def get_mute_settings(group_id) -> dict:
    default = {
        "enabled": True,
        "auto_mute_new_member": False,
        "auto_mute_duration": 300,
        "max_level": 5,
        "level_durations": [300, 600, 1800, 3600, 7200]
    }
    return get_group_data(group_id, "mute_settings", default)

def set_mute_settings(group_id, key: str, value):
    def _update(settings):
        if settings is None:
            settings = {}
        settings[key] = value
        return settings
    update_group_data(group_id, "mute_settings", _update)

def get_unmute_notification(group_id):
    return get_group_data(group_id, "unmute_notification", True)

def set_unmute_notification(group_id, enabled: bool):
    set_group_data(group_id, "unmute_notification", enabled)

def clear_unmute_notification(group_id):
    set_group_data(group_id, "unmute_notification", True)
