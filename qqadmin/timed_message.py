import json
import os
import time
from .group_config import get_group_data, set_group_data, update_group_data

_global_config = {}

def set_global_config(config: dict):
    global _global_config
    _global_config = config

def load_timed_messages(group_id: str) -> list:
    """加载群组的定时消息"""
    return get_group_data(group_id, "timed_messages", [])

def save_timed_messages(group_id: str, messages: list):
    """保存群组的定时消息"""
    set_group_data(group_id, "timed_messages", messages)

def load_timed_message_settings(group_id: str) -> dict:
    """加载群组的定时消息设置"""
    return get_group_data(group_id, "timed_message_settings", {"enabled": True})

def save_timed_message_settings(group_id: str, settings: dict):
    """保存群组的定时消息设置"""
    set_group_data(group_id, "timed_message_settings", settings)

def add_timed_message(group_id: str, message: str, cron_expr: str,
                     at_all: bool = False, enabled: bool = True,
                     operator_id: str = "", name: str = "") -> dict:
    """添加定时消息"""
    messages = load_timed_messages(group_id)
    
    msg_id = f"{group_id}_{int(time.time())}"
    entry = {
        "id": msg_id,
        "message": message,
        "cron_expr": cron_expr,
        "at_all": at_all,
        "enabled": enabled,
        "created_by": operator_id,
        "created_at": int(time.time()),
        "last_sent_at": 0,
        "sent_count": 0,
        "name": name or f"定时消息{len(messages) + 1}"
    }
    
    messages.append(entry)
    save_timed_messages(group_id, messages)
    
    return {"success": True, "id": msg_id, "message": "定时消息添加成功"}

def get_timed_messages(group_id: str) -> list:
    """获取群组所有定时消息"""
    return load_timed_messages(group_id)

def get_timed_message(group_id: str, msg_id: str) -> dict:
    """获取指定定时消息"""
    messages = load_timed_messages(group_id)
    for msg in messages:
        if msg.get("id") == msg_id:
            return msg
    return {}

def update_timed_message(group_id: str, msg_id: str, **kwargs) -> bool:
    """更新定时消息"""
    def _update(messages):
        if messages is None:
            return []
        for msg in messages:
            if msg.get("id") == msg_id:
                for key, value in kwargs.items():
                    if key in ["message", "cron_expr", "at_all", "enabled", "name"]:
                        msg[key] = value
        return messages
    
    update_group_data(group_id, "timed_messages", _update)
    return True

def delete_timed_message(group_id: str, msg_id: str) -> bool:
    """删除定时消息"""
    def _delete(messages):
        if messages is None:
            return []
        return [m for m in messages if m.get("id") != msg_id]
    
    update_group_data(group_id, "timed_messages", _delete)
    return True

def enable_timed_message(group_id: str, msg_id: str, enabled: bool) -> bool:
    """启用/禁用定时消息"""
    return update_timed_message(group_id, msg_id, enabled=enabled)

def parse_cron_expr(cron_expr: str) -> dict:
    """解析cron表达式"""
    parts = cron_expr.strip().split()
    if len(parts) < 5:
        return {"error": "无效的cron表达式，格式：分 时 日 月 周"}
    
    return {
        "minute": parts[0],
        "hour": parts[1],
        "day": parts[2],
        "month": parts[3],
        "weekday": parts[4] if len(parts) > 4 else "*"
    }

def is_cron_match(cron_expr: str, current_time: time.struct_time = None) -> bool:
    """检查cron表达式是否匹配当前时间"""
    if not current_time:
        current_time = time.localtime()
    
    cron = parse_cron_expr(cron_expr)
    if "error" in cron:
        return False
    
    minute = current_time.tm_min
    hour = current_time.tm_hour
    day = current_time.tm_mday
    month = current_time.tm_mon
    weekday = current_time.tm_wday
    
    def matches_field(value, field):
        if value == "*":
            return True
        if "," in value:
            return str(field) in value.split(",")
        if "-" in value:
            parts = value.split("-")
            return int(parts[0]) <= field <= int(parts[1])
        if "/" in value:
            parts = value.split("/")
            base = int(parts[0]) if parts[0] != "*" else 0
            step = int(parts[1])
            return field % step == base
        return str(field) == value
    
    return (matches_field(cron["minute"], minute) and
            matches_field(cron["hour"], hour) and
            matches_field(cron["day"], day) and
            matches_field(cron["month"], month) and
            matches_field(cron["weekday"], weekday))

def get_due_messages(group_id: str) -> list:
    """获取当前需要发送的定时消息"""
    messages = load_timed_messages(group_id)
    now = time.time()
    current_time = time.localtime()
    due = []
    
    for msg in messages:
        if not msg.get("enabled", True):
            continue
        if is_cron_match(msg["cron_expr"], current_time):
            last_sent = msg.get("last_sent_at", 0)
            if now - last_sent >= 60:
                due.append(msg)
    
    return due

def mark_sent(group_id: str, msg_id: str):
    """标记消息已发送"""
    def _mark(messages):
        if messages is None:
            return []
        for msg in messages:
            if msg.get("id") == msg_id:
                msg["last_sent_at"] = int(time.time())
                msg["sent_count"] = msg.get("sent_count", 0) + 1
        return messages
    
    update_group_data(group_id, "timed_messages", _mark)

def clear_timed_messages(group_id: str):
    """清空群组所有定时消息"""
    set_group_data(group_id, "timed_messages", [])

def get_timed_message_statistics(group_id: str) -> dict:
    """获取定时消息统计"""
    messages = load_timed_messages(group_id)
    enabled = sum(1 for m in messages if m.get("enabled", True))
    return {
        "total": len(messages),
        "enabled": enabled,
        "disabled": len(messages) - enabled
    }