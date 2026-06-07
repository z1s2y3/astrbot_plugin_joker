import json
import os
import time
import hashlib
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api import logger

def get_plugin_data_path():
    data_path = Path(get_astrbot_data_path()) / "plugin_data" / "auth"
    data_path.mkdir(parents=True, exist_ok=True)
    return data_path

def get_auth_file():
    return os.path.join(get_plugin_data_path(), "auth.json")

def get_bindings_file():
    return os.path.join(get_plugin_data_path(), "bindings.json")

def get_group_settings_file():
    return os.path.join(get_plugin_data_path(), "group_settings.json")

# ==================== 卡密相关 ====================

def generate_license_key(auth_key: str, days: int, group_id: str = "", index: int = 0) -> str:
    timestamp = int(time.time() * 1000)
    unique = hashlib.md5(f"{auth_key}{timestamp}{group_id}{index}".encode()).hexdigest()[:8]
    return f"JK{days}-{timestamp}{unique}".upper()

def verify_license_key(key: str, auth_key: str, group_id: str = "") -> dict:
    if not key.startswith("JK"):
        return {"valid": False, "error": "无效的卡密格式"}

    try:
        remaining = key[2:]

        if '-' in remaining:
            parts = remaining.split('-', 1)
            days_str = parts[0]
            rest = parts[1]

            if not days_str.isdigit():
                return {"valid": False, "error": "无效的卡密格式"}

            days = int(days_str)
            timestamp_str = rest[:13]
            unique = rest[13:]
        else:
            if len(remaining) < 21:
                return {"valid": False, "error": "无效的卡密格式"}

            unique = remaining[-8:]
            timestamp_str = remaining[-21:-8]
            days_str = remaining[:-21]

            if not days_str.isdigit():
                return {"valid": False, "error": "无效的卡密格式"}
            if not timestamp_str.isdigit():
                return {"valid": False, "error": "无效的卡密格式"}

            days = int(days_str)

        timestamp = int(timestamp_str)
        for i in range(100):
            expected_unique = hashlib.md5(f"{auth_key}{timestamp}{group_id}{i}".encode()).hexdigest()[:8].upper()
            if unique == expected_unique:
                return {"valid": True, "days": days, "timestamp": timestamp}
        return {"valid": False, "error": "卡密验证失败"}
    except Exception as e:
        logger.error(f"验证卡密失败: {e}")
        return {"valid": False, "error": "卡密验证失败"}

def load_auth_data() -> dict:
    auth_file = get_auth_file()
    try:
        if os.path.exists(auth_file):
            with open(auth_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"加载授权数据失败: {e}")
    return {"groups": {}, "unused_keys": [], "used_keys": {}}

def save_auth_data(data: dict):
    auth_file = get_auth_file()
    try:
        with open(auth_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存授权数据失败: {e}")

def add_unused_key(key: str):
    data = load_auth_data()
    if key not in data["unused_keys"]:
        data["unused_keys"].append(key)
        save_auth_data(data)

def remove_unused_key(key: str):
    data = load_auth_data()
    if key in data["unused_keys"]:
        data["unused_keys"].remove(key)
        save_auth_data(data)

def get_unused_keys() -> list:
    data = load_auth_data()
    return data.get("unused_keys", [])

def get_used_keys() -> dict:
    data = load_auth_data()
    return data.get("used_keys", {})

def mark_key_used(key: str, group_id: str, user_id: str = "") -> bool:
    data = load_auth_data()
    if key not in data["unused_keys"]:
        return False
    
    now = int(time.time())
    
    # 解析天数（兼容新旧格式）
    if '-' in key:
        # 新格式：JK{days}-{timestamp}{unique}
        days = int(key[2:key.index('-')])
    else:
        # 旧格式：JK{days}{timestamp}{unique}
        days_str = ""
        for char in key[2:]:
            if char.isdigit():
                days_str += char
            else:
                break
        days = int(days_str) if days_str else 1
    
    expire = now + (days * 86400)
    
    data["unused_keys"].remove(key)
    data["used_keys"][key] = {
        "group_id": group_id,
        "activated_at": now,
        "expire": expire,
        "user_id": user_id
    }
    
    if group_id not in data["groups"]:
        data["groups"][group_id] = {}
    data["groups"][group_id] = {
        "expire": expire,
        "activated_at": now,
        "key": key,
        "days": days,
        "user_id": user_id
    }
    
    save_auth_data(data)
    return True

def delete_all_keys():
    data = load_auth_data()
    data["unused_keys"] = []
    data["used_keys"] = {}
    save_auth_data(data)

def delete_used_keys():
    data = load_auth_data()
    data["used_keys"] = {}
    save_auth_data(data)

# ==================== 授权管理 ====================

def authorize_group(group_id: str, days: int, operator_id: str = "") -> bool:
    data = load_auth_data()
    now = int(time.time())
    expire = now + (days * 86400)
    
    data["groups"][group_id] = {
        "expire": expire,
        "activated_at": now,
        "key": f"MANUAL_{group_id}_{now}",
        "days": days,
        "user_id": operator_id
    }
    
    save_auth_data(data)
    return True

def deauthorize_group(group_id: str) -> bool:
    data = load_auth_data()
    if group_id not in data["groups"]:
        return False
    del data["groups"][group_id]
    save_auth_data(data)
    return True

def is_group_authorized(group_id: str) -> bool:
    data = load_auth_data()
    if group_id not in data["groups"]:
        return False
    expire = data["groups"][group_id].get("expire", 0)
    return expire > time.time()

def get_group_auth_info(group_id: str) -> dict:
    data = load_auth_data()
    return data.get("groups", {}).get(group_id, {})

def add_auth_time(group_id: str, days: int) -> bool:
    data = load_auth_data()
    if group_id not in data["groups"]:
        return False
    
    expire = data["groups"][group_id].get("expire", 0)
    if expire < time.time():
        expire = int(time.time())
    expire += days * 86400
    data["groups"][group_id]["expire"] = expire
    
    save_auth_data(data)
    return True

def reduce_auth_time(group_id: str, days: int) -> bool:
    data = load_auth_data()
    if group_id not in data["groups"]:
        return False
    
    expire = data["groups"][group_id].get("expire", 0)
    expire -= days * 86400
    if expire < time.time():
        expire = int(time.time()) - 1
    data["groups"][group_id]["expire"] = expire
    
    save_auth_data(data)
    return True

def list_all_authorizations() -> dict:
    data = load_auth_data()
    return data.get("groups", {})

def get_auth_statistics() -> dict:
    data = load_auth_data()
    groups = data.get("groups", {})
    now = int(time.time())
    
    total = len(groups)
    active = sum(1 for info in groups.values() if info.get("expire", 0) > now)
    expired = total - active
    
    return {
        "total_groups": total,
        "active_groups": active,
        "expired_groups": expired,
        "unused_keys_count": len(data.get("unused_keys", [])),
        "used_keys_count": len(data.get("used_keys", {}))
    }

# ==================== 绑定管理 ====================

def load_bindings() -> dict:
    bindings_file = get_bindings_file()
    try:
        if os.path.exists(bindings_file):
            with open(bindings_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"加载绑定数据失败: {e}")
    return {}

def save_bindings(bindings: dict):
    bindings_file = get_bindings_file()
    try:
        with open(bindings_file, "w", encoding="utf-8") as f:
            json.dump(bindings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存绑定数据失败: {e}")

def bind_server(group_id: str, server_id: str) -> bool:
    bindings = load_bindings()
    bindings[group_id] = server_id
    save_bindings(bindings)
    return True

def unbind_server(group_id: str) -> bool:
    bindings = load_bindings()
    if group_id not in bindings:
        return False
    del bindings[group_id]
    save_bindings(bindings)
    return True

def get_group_binding(group_id: str) -> str:
    bindings = load_bindings()
    return bindings.get(group_id, "")

# ==================== 设置管理 ====================

def load_group_settings() -> dict:
    settings_file = get_group_settings_file()
    try:
        if os.path.exists(settings_file):
            with open(settings_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"加载群组设置失败: {e}")
    return {}

def save_group_settings(settings: dict):
    settings_file = get_group_settings_file()
    try:
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存群组设置失败: {e}")

def get_group_setting(group_id: str, key: str, default=None):
    settings = load_group_settings()
    return settings.get(group_id, {}).get(key, default)

def set_group_setting(group_id: str, key: str, value):
    settings = load_group_settings()
    if group_id not in settings:
        settings[group_id] = {}
    settings[group_id][key] = value
    save_group_settings(settings)