import json
import time
from .group_config import get_group_data, set_group_data, update_group_data
from .api_client import api_client

def add_kick_record(group_id: str, user_id: str, operator_id: str, reason: str = "",
                    kick_type: str = "normal", notice: bool = True) -> bool:
    """添加踢出记录"""
    def _add(records):
        if records is None:
            records = []

        # 检查是否已有该用户的记录
        for existing_record in records:
            if existing_record.get("user_id") == user_id:
                existing_record["踢出次数"] = existing_record.get("踢出次数", 0) + 1
                existing_record["last_kicked_at"] = int(time.time())
                existing_record["last_reason"] = reason
                existing_record["last_operator"] = operator_id
                return records

        # 新增记录
        record = {
            "user_id": user_id,
            "operator_id": operator_id,
            "reason": reason,
            "kicked_at": int(time.time()),
            "kick_type": kick_type,
            "notice_sent": notice,
            "踢出次数": 1
        }
        records.append(record)
        return records

    update_group_data(group_id, "kick_records", _add)
    return True

def is_kicked(group_id: str, user_id: str) -> bool:
    """检查用户是否被踢出过"""
    records = get_group_data(group_id, "kick_records", [])
    for record in records:
        if record.get("user_id") == user_id:
            return True
    return False

def get_kick_count(group_id: str, user_id: str) -> int:
    """获取用户被踢出次数"""
    records = get_group_data(group_id, "kick_records", [])
    for record in records:
        if record.get("user_id") == user_id:
            return record.get("踢出次数", 1)
    return 0

def remove_kick_record(group_id: str, user_id: str) -> bool:
    """移除踢出记录"""
    def _remove(records):
        if records is None:
            return []
        return [r for r in records if r.get("user_id") != user_id]

    new_records = update_group_data(group_id, "kick_records", _remove)
    return True

def get_kick_records(group_id: str, limit: int = 20) -> list:
    """获取踢出记录"""
    records = get_group_data(group_id, "kick_records", [])
    return records[-limit:] if records else []

def get_user_kick_history(group_id: str, user_id: str) -> list:
    """获取用户踢出历史"""
    records = get_group_data(group_id, "kick_records", [])
    return [r for r in records if r.get("user_id") == user_id]

def clear_kick_records(group_id: str):
    """清空踢出记录"""
    set_group_data(group_id, "kick_records", [])

def get_kick_settings(group_id: str) -> dict:
    """获取踢出设置"""
    default_settings = {
        "enabled": True,
        "max_kicks_before_ban": 3,
        "auto_ban_after_max": True,
        "notify_user": True,
        "notify_type": "private",
        "kick_cooldown": 3600
    }
    return get_group_data(group_id, "kick_settings", default_settings)

def set_kick_settings(group_id: str, key: str, value):
    """设置踢出设置"""
    def _update(settings):
        if settings is None:
            settings = {}
        settings[key] = value
        return settings

    update_group_data(group_id, "kick_settings", _update)

def should_auto_ban(group_id: str, user_id: str) -> bool:
    """检查是否应该自动拉黑"""
    settings = get_kick_settings(group_id)
    if not settings.get("auto_ban_after_max", True):
        return False

    kick_count = get_kick_count(group_id, user_id)
    max_kicks = settings.get("max_kicks_before_ban", 3)

    return kick_count >= max_kicks

def get_kick_statistics(group_id: str) -> dict:
    """获取踢出统计"""
    records = get_group_data(group_id, "kick_records", [])
    if not records:
        return {
            "total_kicks": 0,
            "unique_users": 0,
            "today_kicks": 0,
            "week_kicks": 0
        }

    now = int(time.time())
    today_start = now - (now % 86400)
    week_start = now - (7 * 86400)

    total_kicks = 0
    unique_users = set()
    today_kicks = 0
    week_kicks = 0

    for record in records:
        total_kicks += record.get("踢出次数", 1)
        unique_users.add(record.get("user_id"))

        kicked_at = record.get("kicked_at", 0)
        if kicked_at >= today_start:
            today_kicks += record.get("踢出次数", 1)
        if kicked_at >= week_start:
            week_kicks += record.get("踢出次数", 1)

    return {
        "total_kicks": total_kicks,
        "unique_users": len(unique_users),
        "today_kicks": today_kicks,
        "week_kicks": week_kicks
    }

def is_in_kick_cooldown(group_id: str, user_id: str) -> bool:
    """检查是否在踢出冷却中"""
    records = get_group_data(group_id, "kick_records", [])
    if not records:
        return False

    settings = get_kick_settings(group_id)
    cooldown = settings.get("kick_cooldown", 3600)

    for record in records:
        if record.get("user_id") == user_id:
            last_kick = record.get("last_kicked_at") or record.get("kicked_at", 0)
            if int(time.time()) - last_kick < cooldown:
                return True
    return False

# ==================== 异步 API 调用函数 ====================

async def kick_user_api(group_id: str, user_id: str, reason: str = "", context=None) -> bool:
    """
    使用框架内置方法踢出群成员
    """
    try:
        from astrbot.api.event import filter as event_filter
        
        # 获取平台实例
        platform = None
        if context:
            try:
                platform = context.get_platform(event_filter.PlatformAdapterType.AIOCQHTTP)
            except Exception:
                pass
        
        if platform:
            # 先发送踢出原因通知
            if reason:
                try:
                    await platform.get_client().call_action(
                        action="send_group_msg",
                        group_id=int(group_id),
                        message=f"⚠️ 用户 {user_id} 已被移出群聊\n📋 原因: {reason}"
                    )
                except Exception:
                    pass
            
            # 然后踢出用户
            await platform.get_client().call_action(
                action="set_group_kick",
                group_id=int(group_id),
                user_id=int(user_id),
                reject_add_request=False
            )
            add_kick_record(group_id, user_id, "system", reason, kick_type="api")
            return True
        else:
            return False
    except Exception:
        return False

async def kick_users_api(group_id: str, user_ids: list, reason: str = "") -> dict:
    """批量踢出群成员"""
    success_count = 0
    failed_users = []
    for user_id in user_ids:
        if await kick_user_api(group_id, user_id, reason):
            success_count += 1
        else:
            failed_users.append(user_id)
    
    return {
        "success": success_count > 0,
        "count": success_count,
        "failed": failed_users
    }

def load_kick_records(group_id: str) -> list:
    return get_group_data(group_id, "kick_records", [])

def save_kick_records(group_id: str, records: list):
    set_group_data(group_id, "kick_records", records)
