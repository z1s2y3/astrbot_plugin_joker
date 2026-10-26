import time
from .group_config import get_group_data, set_group_data, update_group_data


def _normalize_user_id(user_id: object) -> str:
    """Use one representation for QQ IDs stored by older and newer configs."""
    return str(user_id or "").strip()


def _admin_user_id(admin: object) -> str:
    """Return a sub-admin's QQ ID from the current or legacy storage shape."""
    if isinstance(admin, dict):
        return _normalize_user_id(admin.get("user_id"))
    # Older group configuration files stored plain QQ strings.  Keep them
    # usable instead of crashing or silently denying their permissions.
    if isinstance(admin, (str, int)):
        return _normalize_user_id(admin)
    return ""


def _admin_record(admin: object) -> dict | None:
    """Project a legacy entry into the dict shape used by the web portal."""
    user_id = _admin_user_id(admin)
    if not user_id:
        return None
    if isinstance(admin, dict):
        record = dict(admin)
        record["user_id"] = user_id
        permissions = record.get("permissions")
        if permissions is None:
            record["permissions"] = get_default_permissions()
        elif isinstance(permissions, (list, tuple, set)):
            record["permissions"] = [str(item).strip() for item in permissions]
        else:
            record["permissions"] = []
        return record
    return {
        "user_id": user_id,
        "added_by": "",
        "added_at": 0,
        # Legacy entries predate per-feature permission selection.  Upgrade
        # them to the same least-privilege reply-only default; a group owner
        # can explicitly grant moderation permissions from the web panel.
        "permissions": get_default_permissions(),
    }


def load_sub_admins(group_id: str) -> list:
    """加载群组的子管理员"""
    admins = get_group_data(group_id, "sub_admins", [])
    return admins if isinstance(admins, list) else []

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
    # Reply management is deliberately separate from moderation actions.  It
    # lets a normal member maintain replies/interactive content without also
    # gaining the ability to mute, kick, recall, or blacklist anyone.
    return ["reply"]

def add_sub_admin(group_id: str, user_id: str, operator_id: str = "") -> bool:
    """添加子管理员"""
    user_id = _normalize_user_id(user_id)
    if not user_id:
        return False
    admins = load_sub_admins(group_id)
    
    if any(_admin_user_id(admin) == user_id for admin in admins):
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
    user_id = _normalize_user_id(user_id)

    def _remove(admins):
        if not isinstance(admins, list):
            return []
        return [admin for admin in admins if _admin_user_id(admin) != user_id]
    
    update_group_data(group_id, "sub_admins", _remove)
    return True

def is_sub_admin(group_id: str, user_id: str) -> bool:
    """检查是否是子管理员"""
    user_id = _normalize_user_id(user_id)
    admins = load_sub_admins(group_id)
    return bool(user_id) and any(_admin_user_id(admin) == user_id for admin in admins)

def get_sub_admins(group_id: str) -> list:
    """获取群组所有子管理员"""
    return [record for admin in load_sub_admins(group_id) if (record := _admin_record(admin))]

def set_sub_admin_permissions(group_id: str, user_id: str, permissions: list) -> bool:
    """设置子管理员权限"""
    user_id = _normalize_user_id(user_id)
    normalized_permissions = [
        str(permission).strip()
        for permission in (permissions or [])
        if str(permission).strip()
    ]

    def _update(admins):
        if not isinstance(admins, list):
            return []
        updated = []
        for admin in admins:
            if _admin_user_id(admin) != user_id:
                updated.append(admin)
                continue
            record = _admin_record(admin)
            if record:
                record["permissions"] = normalized_permissions
                updated.append(record)
        return updated
    
    update_group_data(group_id, "sub_admins", _update)
    return True

def get_sub_admin_permissions(group_id: str, user_id: str) -> list:
    """获取子管理员权限"""
    user_id = _normalize_user_id(user_id)
    admins = load_sub_admins(group_id)
    for admin in admins:
        if _admin_user_id(admin) == user_id:
            record = _admin_record(admin)
            return list(record.get("permissions", [])) if record else []
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
    user_id = _normalize_user_id(user_id)
    admins = load_sub_admins(group_id)
    for admin in admins:
        if _admin_user_id(admin) == user_id:
            return _admin_record(admin) or {}
    return {}

def update_sub_admin(group_id: str, user_id: str, **kwargs) -> bool:
    """更新子管理员信息"""
    user_id = _normalize_user_id(user_id)

    def _update(admins):
        if not isinstance(admins, list):
            return []
        updated = []
        for admin in admins:
            if _admin_user_id(admin) != user_id:
                updated.append(admin)
                continue
            record = _admin_record(admin)
            if record:
                record.update(kwargs)
                record["user_id"] = user_id
                updated.append(record)
        return updated
    
    update_group_data(group_id, "sub_admins", _update)
    return True

def get_sub_admin_statistics(group_id: str) -> dict:
    """获取子管理员统计"""
    admins = load_sub_admins(group_id)
    return {"total": len(admins)}
