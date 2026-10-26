import time
from .group_config import get_group_data, set_group_data, update_group_data

try:
    # Keep the root compatibility mirror and the portal's canonical feature
    # object in one atomic write when the current group-config module exposes
    # the batch helper.  Older hot-reloaded copies may not have it, so the
    # fallback below remains intentionally supported.
    from .group_config import batch_update_group_data
except ImportError:  # pragma: no cover - exercised by legacy plugin loaders
    batch_update_group_data = None


def _welcome_feature(group_id: str) -> dict:
    """Return the portal-backed welcome feature without leaking malformed data."""
    features = get_group_data(group_id, "features", {})
    if not isinstance(features, dict):
        return {}
    feature = features.get("welcome", {})
    return feature if isinstance(feature, dict) else {}


def _welcome_value(
    group_id: str,
    feature_key: str,
    root_keys=(),
    default=None,
    feature_aliases=(),
):
    """Read canonical ``features.welcome`` values before legacy root aliases.

    Web saves use the nested feature object.  Older Windows builds wrote root
    aliases, which can remain stale after a portal edit; checking key
    presence (rather than truthiness) preserves explicit ``False``/empty
    values from the web UI.
    """
    feature = _welcome_feature(group_id)
    if feature_key in feature:
        return feature[feature_key]
    for feature_alias in feature_aliases:
        if feature_alias in feature:
            return feature[feature_alias]
    for root_key in root_keys:
        value = get_group_data(group_id, root_key, None)
        if value is not None:
            return value
    return default


def _set_welcome_value(
    group_id: str,
    feature_key: str,
    value,
    root_keys=(),
    feature_aliases=(),
) -> None:
    """Persist one welcome setting to canonical and legacy locations."""
    root_keys = tuple(root_keys)
    feature_keys = (feature_key, *tuple(feature_aliases))

    def update_feature(current):
        feature_map = dict(current) if isinstance(current, dict) else {}
        nested = dict(feature_map.get("welcome", {}))
        for key in feature_keys:
            nested[key] = value
        feature_map["welcome"] = nested
        return feature_map

    if callable(batch_update_group_data):
        updates = [("features", update_feature)]
        updates.extend(
            (key, lambda _old, new_value=value: new_value) for key in root_keys
        )
        batch_update_group_data(group_id, updates)
        return

    # Compatibility path for old in-process modules.  It is still better than
    # writing only the root alias, because subsequent reads use the nested
    # canonical value whenever it exists.
    update_group_data(group_id, "features", update_feature)
    for key in root_keys:
        set_group_data(group_id, key, value)

# 新成员检测和跟踪
def mark_member_welcomed(group_id: str, user_id: str, user_name: str = ""):
    """标记成员已被欢迎（防止重复欢迎）
    
    Args:
        group_id: 群组ID
        user_id: 用户ID
        user_name: 用户昵称
    """
    def _mark(welcomed_members):
        if welcomed_members is None:
            welcomed_members = {}
        
        # 记录成员被欢迎的时间和昵称
        welcomed_members[str(user_id)] = {
            "user_name": user_name,
            "welcomed_at": int(time.time())
        }
        
        return welcomed_members
    
    update_group_data(group_id, "welcomed_members", _mark)

def is_member_welcomed(group_id: str, user_id: str) -> bool:
    """检查成员是否已被欢迎（防止重复欢迎）
    
    Args:
        group_id: 群组ID
        user_id: 用户ID
        
    Returns:
        bool: 如果已欢迎返回True，否则返回False
    """
    welcomed_members = get_group_data(group_id, "welcomed_members", {})
    return str(user_id) in welcomed_members

def is_new_member(group_id: str, user_id: str) -> bool:
    """检查用户是否是新成员（基于欢迎历史）
    
    返回False的条件：
    1. 用户已在欢迎历史中
    2. 用户已被标记为欢迎过
    
    Args:
        group_id: 群组ID
        user_id: 用户ID
        
    Returns:
        bool: 如果是新成员返回True，否则返回False
    """
    # 检查欢迎历史
    history = get_group_data(group_id, "welcome_history", [])
    for h in history:
        if h.get("user_id") == user_id:
            return False
    
    # 检查已欢迎成员记录
    return not is_member_welcomed(group_id, user_id)

def get_new_members_count(group_id: str, hours: int = 24) -> int:
    """获取指定时间内的新成员数量
    
    Args:
        group_id: 群组ID
        hours: 时间范围（小时）
        
    Returns:
        int: 新成员数量
    """
    history = get_group_data(group_id, "welcome_history", [])
    now = int(time.time())
    time_threshold = now - (hours * 3600)
    
    count = 0
    for h in history:
        if h.get("timestamp", 0) > time_threshold:
            count += 1
    
    return count

def clear_welcomed_member(group_id: str, user_id: str):
    """清除成员的欢迎记录（例如在成员离群后）
    
    Args:
        group_id: 群组ID
        user_id: 用户ID
    """
    def _clear(welcomed_members):
        if welcomed_members is None:
            welcomed_members = {}
        
        welcomed_members.pop(str(user_id), None)
        return welcomed_members
    
    update_group_data(group_id, "welcomed_members", _clear)

# 欢迎设置
def set_welcome_message(group_id: str, message: str):
    _set_welcome_value(
        group_id,
        "message",
        str(message or ""),
        ("welcome_message",),
        ("welcome_message",),
    )

def get_welcome_message(group_id: str) -> str:
    return str(_welcome_value(
        group_id,
        "message",
        ("welcome_message",),
        "",
        ("welcome_message",),
    ) or "")

def enable_welcome(group_id: str, enabled: bool = True):
    _set_welcome_value(
        group_id,
        "enabled",
        bool(enabled),
        ("welcome_enabled",),
    )

def is_welcome_enabled(group_id: str) -> bool:
    return bool(_welcome_value(
        group_id,
        "enabled",
        ("welcome_enabled",),
        False,
    ))

def set_welcome_keywords(group_id: str, keywords: list):
    if isinstance(keywords, str):
        keywords = keywords.splitlines()
    normalized = [
        str(keyword).strip()
        for keyword in (keywords or [])
        if str(keyword).strip()
    ]
    _set_welcome_value(
        group_id,
        "keywords",
        normalized,
        ("welcome_keywords",),
    )

def get_welcome_keywords(group_id: str) -> list:
    keywords = _welcome_value(
        group_id,
        "keywords",
        ("welcome_keywords",),
        ["欢迎", "welcome", "入群"],
    )
    if isinstance(keywords, str):
        keywords = keywords.splitlines()
    if not isinstance(keywords, (list, tuple, set)):
        return []
    return [str(keyword).strip() for keyword in keywords if str(keyword).strip()]

def should_send_welcome(group_id: str, message: str) -> bool:
    """Return whether an ordinary group message should receive a welcome reply.

    Member-join welcomes are handled separately.  This path is intentionally
    gated by both the welcome feature and its keyword-reply switch so a portal
    save cannot unexpectedly turn normal chat into an automatic reply.
    """
    if not is_welcome_enabled(group_id) or not is_auto_reply_enabled(group_id):
        return False
    normalized_message = str(message or "").casefold()
    if not normalized_message:
        return False
    keywords = get_welcome_keywords(group_id)
    for keyword in keywords:
        if keyword.casefold() in normalized_message:
            return True
    return False

# 退群通知
def set_farewell_message(group_id: str, message: str):
    _set_welcome_value(
        group_id,
        "farewell_message",
        str(message or ""),
        ("farewell_message", "welcome_farewell_message"),
    )

def get_farewell_message(group_id: str) -> str:
    return str(_welcome_value(
        group_id,
        "farewell_message",
        ("farewell_message", "welcome_farewell_message"),
        "",
    ) or "")

def enable_farewell(group_id: str, enabled: bool = True):
    _set_welcome_value(
        group_id,
        "farewell_enabled",
        bool(enabled),
        ("farewell_enabled", "welcome_farewell_enabled"),
    )

def is_farewell_enabled(group_id: str) -> bool:
    return bool(_welcome_value(
        group_id,
        "farewell_enabled",
        ("farewell_enabled", "welcome_farewell_enabled"),
        False,
    ))

def set_welcome_type(group_id: str, welcome_type: str):
    set_group_data(group_id, "welcome_type", welcome_type)

def get_welcome_type(group_id: str) -> str:
    return get_group_data(group_id, "welcome_type", "auto")

def set_welcome_delay(group_id: str, seconds: int):
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        seconds = 0
    _set_welcome_value(group_id, "delay", seconds, ("welcome_delay",))

def get_welcome_delay(group_id: str) -> int:
    try:
        return max(0, int(_welcome_value(
            group_id, "delay", ("welcome_delay",), 0
        )))
    except (TypeError, ValueError):
        return 0

def set_at_new_member(group_id: str, enabled: bool):
    _set_welcome_value(group_id, "at_new_member", bool(enabled), ("at_new_member",))

def is_at_new_member_enabled(group_id: str) -> bool:
    value = _welcome_value(group_id, "at_new_member", ("at_new_member",), None)
    if value is not None:
        return bool(value)
    placeholders = get_group_data(group_id, "welcome_placeholders", {})
    if isinstance(placeholders, dict) and "at_new_member" in placeholders:
        return bool(placeholders["at_new_member"])
    return True

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
def update_group_members(group_id: str, member_list: list, levels_pending: bool = False):
    set_group_data(group_id, "cached_members", {
        "members": member_list,
        "updated_at": int(time.time()),
        "message": "",
        "levels_pending": bool(levels_pending),
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
    message = get_welcome_message(group_id)
    auto_reply = is_auto_reply_enabled(group_id)
    return {
        "enabled": is_welcome_enabled(group_id),
        "message": message,
        "welcome_message": message,
        "keywords": get_welcome_keywords(group_id),
        "farewell_enabled": is_farewell_enabled(group_id),
        "farewell_message": get_farewell_message(group_id),
        "welcome_type": get_welcome_type(group_id),
        "delay": get_welcome_delay(group_id),
        "at_new_member": is_at_new_member_enabled(group_id),
        "auto_reply": auto_reply,
        "auto_reply_enabled": auto_reply,
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
        elif key in {"auto_reply", "auto_reply_enabled"}:
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
    _set_welcome_value(
        group_id,
        "auto_reply_enabled",
        bool(enabled),
        ("welcome_auto_reply", "welcome_auto_reply_enabled"),
    )

def is_auto_reply_enabled(group_id: str) -> bool:
    return bool(_welcome_value(
        group_id,
        "auto_reply_enabled",
        ("welcome_auto_reply", "welcome_auto_reply_enabled"),
        False,
    ))

def set_welcome_placeholders(group_id: str, placeholders: dict):
    set_group_data(group_id, "welcome_placeholders", placeholders)

def get_welcome_placeholders(group_id: str) -> dict:
    placeholders = get_group_data(group_id, "welcome_placeholders", None)
    if isinstance(placeholders, dict):
        placeholders = dict(placeholders)
        placeholders.setdefault("at_new_member", is_at_new_member_enabled(group_id))
        return placeholders
    return {"at_new_member": is_at_new_member_enabled(group_id)}

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
    elif key in {"auto_reply", "auto_reply_enabled"}:
        set_auto_reply(group_id, value)

def get_welcome_statistics(group_id: str) -> dict:
    history = get_group_data(group_id, "welcome_history", [])
    return {
        "total_welcomed": len(history),
        "enabled": is_welcome_enabled(group_id),
        "farewell_enabled": is_farewell_enabled(group_id)
    }
