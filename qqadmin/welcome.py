import time
from .group_config import get_group_data, set_group_data, update_group_data

# 欢迎设置
def set_welcome_message(group_id: str, message: str):
    set_group_data(group_id, "welcome_message", message)

def get_welcome_message(group_id: str) -> str:
    return get_group_data(group_id, "welcome_message", "")

def enable_welcome(group_id: str, enabled: bool = True):
    set_group_data(group_id, "welcome_enabled", enabled)

def is_welcome_enabled(group_id: str) -> bool:
    return get_group_data(group_id, "welcome_enabled", False)

def set_welcome_keywords(group_id: str, keywords: list):
    set_group_data(group_id, "welcome_keywords", keywords)

def get_welcome_keywords(group_id: str) -> list:
    return get_group_data(group_id, "welcome_keywords", ["欢迎", "welcome", "入群"])

def should_send_welcome(group_id: str, message: str) -> bool:
    if not is_welcome_enabled(group_id):
        return False
    keywords = get_welcome_keywords(group_id)
    for keyword in keywords:
        if keyword in message:
            return True
    return False

# 退群通知
def set_farewell_message(group_id: str, message: str):
    set_group_data(group_id, "farewell_message", message)

def get_farewell_message(group_id: str) -> str:
    return get_group_data(group_id, "farewell_message", "")

def enable_farewell(group_id: str, enabled: bool = True):
    set_group_data(group_id, "farewell_enabled", enabled)

def is_farewell_enabled(group_id: str) -> bool:
    return get_group_data(group_id, "farewell_enabled", False)

def set_welcome_type(group_id: str, welcome_type: str):
    set_group_data(group_id, "welcome_type", welcome_type)

def get_welcome_type(group_id: str) -> str:
    return get_group_data(group_id, "welcome_type", "auto")

def set_welcome_delay(group_id: str, seconds: int):
    set_group_data(group_id, "welcome_delay", seconds)

def get_welcome_delay(group_id: str) -> int:
    return get_group_data(group_id, "welcome_delay", 0)

def set_at_new_member(group_id: str, enabled: bool):
    set_group_data(group_id, "at_new_member", enabled)

def is_at_new_member_enabled(group_id: str) -> bool:
    return get_group_data(group_id, "at_new_member", True)

# 欢迎历史
def log_welcome(group_id: str, user_id: str, user_name: str, welcome_type: str):
    def _add(history):
        if history is None:
            history = []
        history.append({
            "user_id": user_id,
            "user_name": user_name,
            "type": welcome_type,
            "timestamp": int(time.time())
        })
        if len(history) > 250:
            history = history[-250:]
        return history
    update_group_data(group_id, "welcome_history", _add)

def get_welcome_history(group_id: str, limit: int = 50) -> list:
    history = get_group_data(group_id, "welcome_history", [])
    return history[-limit:] if history else []

def get_user_welcome_history(group_id: str, user_id: str) -> list:
    history = get_group_data(group_id, "welcome_history", [])
    return [h for h in history if h.get("user_id") == user_id]

def format_welcome_message(group_id: str, user_id: str, user_name: str) -> str:
    message = get_welcome_message(group_id)
    if not message:
        return ""

    replacements = {
        "{user_id}": user_id,
        "{user_name}": user_name,
        "{group_id}": group_id,
        "{time}": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    }

    for placeholder, value in replacements.items():
        message = message.replace(placeholder, str(value))

    return message

def format_farewell_message(group_id: str, user_id: str, user_name: str) -> str:
    message = get_farewell_message(group_id)
    if not message:
        return ""

    replacements = {
        "{user_id}": user_id,
        "{user_name}": user_name,
        "{group_id}": group_id,
        "{time}": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    }

    for placeholder, value in replacements.items():
        message = message.replace(placeholder, str(value))

    return message

# 成员缓存
def update_group_members(group_id: str, member_list: list):
    set_group_data(group_id, "cached_members", {
        "members": member_list,
        "updated_at": int(time.time())
    })

def get_group_members(group_id: str) -> list:
    data = get_group_data(group_id, "cached_members", {})
    return data.get("members", [])

def get_new_members(group_id: str, current_members: list) -> list:
    cached_members = get_group_members(group_id)
    if not cached_members:
        return []
    cached_uids = set(str(m.get("user_id", "")) for m in cached_members)
    new_members = []
    for member in current_members:
        uid = str(member.get("user_id", ""))
        if uid and uid not in cached_uids:
            new_members.append(member)
    return new_members

def enable_member_tracking(group_id: str, enabled: bool = True):
    set_group_data(group_id, "member_tracking_enabled", enabled)

def is_member_tracking_enabled(group_id: str) -> bool:
    return get_group_data(group_id, "member_tracking_enabled", False)

def should_welcome_new_member(group_id: str) -> bool:
    if not is_welcome_enabled(group_id):
        return False
    if not is_member_tracking_enabled(group_id):
        return False
    return True

def load_welcome_settings(group_id: str) -> dict:
    return {
        "enabled": is_welcome_enabled(group_id),
        "message": get_welcome_message(group_id),
        "keywords": get_welcome_keywords(group_id),
        "farewell_enabled": is_farewell_enabled(group_id),
        "farewell_message": get_farewell_message(group_id),
        "welcome_type": get_welcome_type(group_id),
        "delay": get_welcome_delay(group_id),
        "at_new_member": is_at_new_member_enabled(group_id),
        "auto_reply": is_auto_reply_enabled(group_id),
        "member_tracking": is_member_tracking_enabled(group_id)
    }

def save_welcome_settings(group_id: str, settings: dict):
    for key, value in settings.items():
        if key == "enabled":
            enable_welcome(group_id, value)
        elif key == "message":
            set_welcome_message(group_id, value)
        elif key == "keywords":
            set_welcome_keywords(group_id, value)
        elif key == "farewell_enabled":
            enable_farewell(group_id, value)
        elif key == "farewell_message":
            set_farewell_message(group_id, value)
        elif key == "welcome_type":
            set_welcome_type(group_id, value)
        elif key == "delay":
            set_welcome_delay(group_id, value)
        elif key == "at_new_member":
            set_at_new_member(group_id, value)
        elif key == "auto_reply":
            set_auto_reply(group_id, value)
        elif key == "member_tracking":
            enable_member_tracking(group_id, value)

def add_welcome_image(group_id: str, image_url: str):
    def _add(images):
        if images is None:
            images = []
        if image_url not in images:
            images.append(image_url)
        return images
    update_group_data(group_id, "welcome_images", _add)

def remove_welcome_image(group_id: str, image_url: str):
    def _remove(images):
        if images is None:
            return []
        if image_url in images:
            images.remove(image_url)
        return images
    update_group_data(group_id, "welcome_images", _remove)

def get_welcome_images(group_id: str) -> list:
    return get_group_data(group_id, "welcome_images", [])

def set_auto_reply(group_id: str, enabled: bool):
    set_group_data(group_id, "welcome_auto_reply", enabled)

def is_auto_reply_enabled(group_id: str) -> bool:
    return get_group_data(group_id, "welcome_auto_reply", False)

def set_welcome_placeholders(group_id: str, placeholders: dict):
    set_group_data(group_id, "welcome_placeholders", placeholders)

def get_welcome_placeholders(group_id: str) -> dict:
    return get_group_data(group_id, "welcome_placeholders", {})

def get_welcome_settings(group_id: str) -> dict:
    return load_welcome_settings(group_id)

def set_welcome_settings(group_id: str, key: str, value):
    if key == "enabled":
        enable_welcome(group_id, value)
    elif key == "message":
        set_welcome_message(group_id, value)
    elif key == "keywords":
        set_welcome_keywords(group_id, value)
    elif key == "farewell_enabled":
        enable_farewell(group_id, value)
    elif key == "farewell_message":
        set_farewell_message(group_id, value)
    elif key == "welcome_type":
        set_welcome_type(group_id, value)
    elif key == "delay":
        set_welcome_delay(group_id, value)
    elif key == "at_new_member":
        set_at_new_member(group_id, value)

def get_welcome_statistics(group_id: str) -> dict:
    history = get_group_data(group_id, "welcome_history", [])
    return {
        "total_welcomed": len(history),
        "enabled": is_welcome_enabled(group_id),
        "farewell_enabled": is_farewell_enabled(group_id)
    }
