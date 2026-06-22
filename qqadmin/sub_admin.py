import json
import os
import time
from .group_config import get_group_data, set_group_data, update_group_data

_global_config = {}

def set_global_config(config: dict):
    global _global_config
    _global_config = config

def load_sub_admins(group_id: str) -> list:
    """加载群组的子管理员"""
    return get_group_data(group_id, "sub_admins", [])

def save_sub_admins(group_id: str, admins: list):
    """保存群组的子管理员"""
    set_group_data(group_id, "sub_admins", admins)

def load_permissions(group_id: str) -> dict:
    """加载群组的权限设置"""
    return get_group_data(group_id, "permissions", {})

def save_permissions(group_id: str, permissions: dict):
    """保存群组的权限设置"""
    set_group_data(group_id, "permissions", permissions)

def get_default_permissions() -> list:
    """获取默认权限"""
    return ["mute", "kick", "recall", "blacklist"]

def add_sub_admin(group_id: str, user_id: str, operator_id: str = "") -> bool:
    """添加子管理员"""
    admins = load_sub_admins(group_id)
    
    if any(a.get("user_id") == user_id for a in admins):
        return False
    
    admins.append({
        "user_id": user_id,
        "added_by": operator_id,
        "added_at": int(time.time()),
        "permissions": get_default_permissions()
    })
    
    save_sub_admins(group_id, admins)
    return True

def remove_sub_admin(group_id: str, user_id: str) -> bool:
    """移除子管理员"""
    def _remove(admins):
        if admins is None:
            return []
        return [a for a in admins if a.get("user_id") != user_id]
    
    update_group_data(group_id, "sub_admins", _remove)
    return True

def is_sub_admin(group_id: str, user_id: str) -> bool:
    """检查是否是子管理员"""
    admins = load_sub_admins(group_id)
    return any(a.get("user_id") == user_id for a in admins)

def get_sub_admins(group_id: str) -> list:
    """获取群组所有子管理员"""
    return load_sub_admins(group_id)

def set_sub_admin_permissions(group_id: str, user_id: str, permissions: list) -> bool:
    """设置子管理员权限"""
    def _update(admins):
        if admins is None:
            return []
        for admin in admins:
            if admin.get("user_id") == user_id:
                admin["permissions"] = permissions
        return admins
    
    update_group_data(group_id, "sub_admins", _update)
    return True

def get_sub_admin_permissions(group_id: str, user_id: str) -> list:
    """获取子管理员权限"""
    admins = load_sub_admins(group_id)
    for admin in admins:
        if admin.get("user_id") == user_id:
            return admin.get("permissions", get_default_permissions())
    return []

def has_permission(group_id: str, user_id: str, permission: str) -> bool:
    """检查是否有权限"""
    if not is_sub_admin(group_id, user_id):
        return False
    permissions = get_sub_admin_permissions(group_id, user_id)
    return permission in permissions

def check_permission(group_id: str, user_id: str, permission: str) -> bool:
    """检查权限"""
    return has_permission(group_id, user_id, permission)

def clear_sub_admins(group_id: str):
    """清空群组所有子管理员"""
    set_group_data(group_id, "sub_admins", [])

def get_sub_admin_info(group_id: str, user_id: str) -> dict:
    """获取子管理员信息"""
    admins = load_sub_admins(group_id)
    for admin in admins:
        if admin.get("user_id") == user_id:
            return admin
    return {}

def update_sub_admin(group_id: str, user_id: str, **kwargs) -> bool:
    """更新子管理员信息"""
    def _update(admins):
        if admins is None:
            return []
        for admin in admins:
            if admin.get("user_id") == user_id:
                admin.update(kwargs)
        return admins
    
    update_group_data(group_id, "sub_admins", _update)
    return True

def get_sub_admin_statistics(group_id: str) -> dict:
    """获取子管理员统计"""
    admins = load_sub_admins(group_id)
    return {"total": len(admins)}