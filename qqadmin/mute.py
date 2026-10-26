import math
import time
from .group_config import get_group_data, set_group_data, update_group_data
from .api_client import api_client


def _normalize_user_id(value: object) -> str:
    return str(value or "").strip()[:64]


def _api_succeeded(result: object) -> bool:
    """Accept both the current dict contract and old boolean adapters."""
    if isinstance(result, dict):
        if result.get("success") is False:
            return False
        if result.get("retcode") not in (None, 0, "0"):
            return False
        status = result.get("status")
        if status in {"ok", 0, "0"}:
            return True
        if status in {"failed", "failure", "error"}:
            return False
        # Some adapters return only ``success: true`` or ``retcode: 0``;
        # absence of an explicit failure is safe for those compatibility APIs.
        return bool(result.get("success")) or status is None
    return bool(result)


def _as_int(value, default: int, maximum: int = 2_592_000) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, min(parsed, maximum))


def _level_durations(settings: dict) -> list[int]:
    raw = settings.get("level_durations", [])
    if not isinstance(raw, (list, tuple)):
        return []
    result = []
    for item in raw[:100]:
        duration = _as_int(item, 0)
        if duration > 0:
            result.append(duration)
    return result


def _mute_level_for_count(settings: dict, cumulative_mutes: int) -> int:
    threshold = max(1, _as_int(settings.get("repeat_threshold", 1), 1, 1000))
    maximum = max(1, _as_int(settings.get("max_level", 5), 5, 100))
    if not settings.get("auto_escalate", True):
        return 1
    return max(1, min(maximum, int(math.ceil(max(1, cumulative_mutes) / threshold))))


def _duration_for_level(settings: dict, level: int) -> int:
    base = _as_int(
        settings.get("auto_mute_duration", settings.get("duration", 300)),
        300,
    )
    durations = _level_durations(settings)
    if durations:
        return durations[min(max(1, level), len(durations)) - 1]
    extension = _as_int(settings.get("repeat_extension", 0), 0)
    return min(2_592_000, base + extension * max(0, level - 1))


def _mute_history_count(group_id: str, user_id: str) -> int:
    """Read the persistent count used for repeated-mute escalation.

    ``mute_list`` is an active-state table and is intentionally deleted when
    a temporary mute expires.  Keeping the escalation counter separately
    means a second violation still reaches the configured threshold after
    the first mute has ended.
    """

    history = get_group_data(group_id, "mute_escalation", {})
    if not isinstance(history, dict):
        return 0
    record = history.get(str(user_id), {})
    if isinstance(record, dict):
        return _as_int(record.get("count", 0), 0, 1_000_000)
    return _as_int(record, 0, 1_000_000)


def _record_mute_history(group_id: str, user_id: str, count: int, level: int) -> None:
    """Persist bounded escalation metadata without syncing private history."""

    timestamp = int(time.time())

    def _update(history):
        history = history if isinstance(history, dict) else {}
        history[str(user_id)] = {
            "count": max(0, min(int(count), 1_000_000)),
            "last_level": max(1, min(int(level), 100)),
            "updated_at": timestamp,
        }
        # Keep the file bounded if a group has accumulated old user records.
        if len(history) > 5000:
            ordered = sorted(
                history.items(),
                key=lambda item: int(item[1].get("updated_at", 0))
                if isinstance(item[1], dict)
                else 0,
                reverse=True,
            )
            history = dict(ordered[:5000])
        return history

    update_group_data(group_id, "mute_escalation", _update)

def mute_user(group_id, user_id, duration=0, reason="", operator_id="", mute_type="normal"):
    """禁言用户"""
    group_id = str(group_id or "").strip()
    user_id = _normalize_user_id(user_id)
    if not group_id or not user_id:
        return False
    settings = get_mute_settings(group_id)
    history_count = _mute_history_count(group_id, user_id)
    requested_duration = _as_int(duration, 0)

    def _mute(mute_list):
        if mute_list is None:
            mute_list = {}
        
        old_info = mute_list.get(user_id)
        old_info = old_info if isinstance(old_info, dict) else {}
        cumulative_mutes = max(
            history_count,
            _as_int(old_info.get("cumulative_mutes", 0), 0, 1_000_000),
        ) + 1
        if mute_type == "auto":
            mute_level = _mute_level_for_count(settings, cumulative_mutes)
            # A zero duration means "use the configured duration for this
            # escalation level".  Positive values remain explicit overrides.
            effective_duration = (
                _duration_for_level(settings, mute_level)
                if requested_duration <= 0
                else requested_duration
            )
        else:
            mute_level = max(1, _as_int(old_info.get("mute_level", 1), 1, 100))
            effective_duration = requested_duration
        effective_duration = _as_int(effective_duration, 0)
        expire_time = 0 if effective_duration == 0 else int(time.time()) + effective_duration
        
        mute_list[user_id] = {
            "expire_time": expire_time,
            "duration": effective_duration,
            "reason": str(reason or "")[:500],
            "muted_at": int(time.time()),
            "operator_id": _normalize_user_id(operator_id),
            "mute_type": mute_type,
            "mute_level": mute_level,
            "cumulative_mutes": cumulative_mutes
        }
        return mute_list

    update_group_data(group_id, "mute_list", _mute)
    # Read the saved active record so the history level matches the exact
    # normalized value used by the updater above.
    saved_info = get_mute_info(group_id, user_id)
    _record_mute_history(
        group_id,
        user_id,
        saved_info.get("cumulative_mutes", history_count + 1),
        saved_info.get("mute_level", 1),
    )
    return True

def unmute_user(group_id, user_id):
    """解禁用户"""
    group_id = str(group_id or "").strip()
    user_id = _normalize_user_id(user_id)
    if not group_id or not user_id:
        return False
    removed = False
    def _unmute(mute_list):
        nonlocal removed
        if mute_list is None or user_id not in mute_list:
            return mute_list
        del mute_list[user_id]
        removed = True
        return mute_list
    
    update_group_data(group_id, "mute_list", _unmute)
    return removed

def is_muted(group_id, user_id):
    """检查用户是否被禁言
    
    返回值：
        True: 用户被禁言（永久或未过期）
        False: 用户未被禁言或禁言已过期
    """
    user_id = _normalize_user_id(user_id)
    mute_list = get_group_data(group_id, "mute_list", {})
    if not mute_list or user_id not in mute_list:
        return False
    
    mute_info = mute_list[user_id]
    if not isinstance(mute_info, dict):
        return False
    expire_time = _as_int(mute_info.get("expire_time", 0), 0, 2**31 - 1)
    current_time = int(time.time())
    
    # 永久禁言（expire_time == 0）
    if expire_time == 0:
        return True
    
    # 临时禁言：检查是否过期
    if expire_time > current_time:
        return True
    else:
        # 禁言已过期，自动清理
        unmute_user(group_id, user_id)
        return False

def get_mute_info(group_id, user_id):
    """获取禁言信息"""
    user_id = _normalize_user_id(user_id)
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
    current_time = int(time.time())
    
    for user_id, info in mute_list.items():
        if not isinstance(info, dict):
            expired_users.append(user_id)
            continue
        expire_time = _as_int(info.get("expire_time", 0), 0, 2**31 - 1)
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
    """Return the configured automatic mute duration for the next violation."""
    settings = get_mute_settings(group_id)
    info = get_mute_info(group_id, user_id)
    previous_count = max(
        _mute_history_count(group_id, user_id),
        _as_int(
            info.get("cumulative_mutes", 0) if isinstance(info, dict) else 0,
            0,
            1_000_000,
        ),
    )
    level = _mute_level_for_count(settings, previous_count + 1)
    return _duration_for_level(settings, level)

def mute_user_auto(group_id, user_id, reason="", operator_id=""):
    """自动禁言用户"""
    # Let the state updater calculate the level-specific duration. Passing a
    # precomputed positive duration bypassed the escalation table.
    return mute_user(group_id, user_id, 0, reason, operator_id, "auto")

def get_mute_remaining_time(group_id, user_id):
    """获取剩余禁言时间"""
    mute_info = get_mute_info(group_id, user_id)
    if not mute_info:
        return 0
    
    expire_time = _as_int(mute_info.get("expire_time", 0), 0, 2**31 - 1)
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
        
        settings = get_mute_settings(group_id)
        current_level = _as_int(mute_list[user_id].get("mute_level", 1), 1, 100)
        max_level = max(1, _as_int(settings.get("max_level", 5), 5, 100))
        new_level = min(max_level, current_level + 1)
        mute_list[user_id]["mute_level"] = new_level
        return mute_list
    
    update_group_data(group_id, "mute_list", _increase)
    return get_mute_level(group_id, user_id)

def get_mute_statistics(group_id):
    """获取禁言统计"""
    mute_list = get_group_data(group_id, "mute_list", {})
    if not mute_list:
        return {"total_muted": 0, "currently_muted": 0, "total_mute_times": 0}
    
    current_time = int(time.time())
    expired_users = []
    total_muted = len(mute_list)
    currently_muted = 0
    total_mute_times = 0
    
    for user_id, info in mute_list.items():
        if not isinstance(info, dict):
            expired_users.append(user_id)
            continue
        total_mute_times += _as_int(info.get("cumulative_mutes", 1), 1, 1_000_000)
        expire_time = _as_int(info.get("expire_time", 0), 0, 2**31 - 1)
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
    
    current_time = int(time.time())
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
    try:
        result = await api_client.set_group_ban(group_id, user_id, duration)
    except Exception:
        return False
    if _api_succeeded(result):
        mute_user(group_id, user_id, duration, reason, "system", "api")
        return True
    return False

async def unmute_user_api(group_id, user_id):
    """API解禁用户"""
    try:
        result = await api_client.set_group_ban(group_id, user_id, 0)
    except Exception:
        return False
    if _api_succeeded(result):
        unmute_user(group_id, user_id)
        return True
    return False

async def set_group_whole_ban_api(group_id, enable=True):
    """API全群禁言"""
    try:
        result = await api_client.set_group_whole_ban(group_id, enable)
    except Exception:
        return False
    if _api_succeeded(result):
        return True
    return False

def load_mute_list(group_id):
    return get_group_data(group_id, "mute_list", {})

def save_mute_list(group_id, mute_list):
    set_group_data(group_id, "mute_list", mute_list)

def get_mute_settings(group_id) -> dict:
    default = {
        "enabled": True,
        "level": 5,
        "duration": 300,
        "auto_mute_new_member": False,
        "auto_mute_duration": 300,
        "auto_escalate": True,
        "repeat_threshold": 1,
        "repeat_extension": 300,
        "max_level": 5,
        "level_durations": [300, 600, 1800, 3600, 7200],
        "mention_mute_rules": [],
    }
    settings = get_group_data(group_id, "mute_settings", default)
    if not isinstance(settings, dict):
        return dict(default)
    normalized = {**default, **settings}
    # Read legacy names without rewriting user data during a status query.
    normalized["level"] = _as_int(
        settings.get("level", settings.get("max_level", 5)), 5, 100
    )
    normalized["duration"] = _as_int(
        settings.get("duration", settings.get("default_duration", settings.get("auto_mute_duration", 300))),
        300,
    )
    normalized["auto_mute_duration"] = _as_int(
        settings.get("auto_mute_duration", normalized["duration"]), normalized["duration"]
    )
    normalized["max_level"] = max(1, _as_int(settings.get("max_level", 5), 5, 100))
    normalized["repeat_threshold"] = max(
        1, _as_int(settings.get("repeat_threshold", 1), 1, 1000)
    )
    normalized["repeat_extension"] = _as_int(settings.get("repeat_extension", 300), 300)
    normalized["auto_escalate"] = (
        str(settings.get("auto_escalate", True)).strip().lower()
        not in {"", "0", "false", "off", "no"}
        if isinstance(settings.get("auto_escalate", True), str)
        else bool(settings.get("auto_escalate", True))
    )
    normalized["level_durations"] = _level_durations(normalized) or list(
        default["level_durations"]
    )
    return normalized

def set_mute_settings(group_id, key: str, value):
    allowed = {
        "enabled", "level", "duration", "default_duration",
        "auto_mute_new_member", "auto_mute_duration", "auto_escalate",
        "repeat_threshold", "repeat_extension", "max_level", "level_durations",
        "mention_mute_rules",
    }
    key = str(key or "").strip()
    if key not in allowed:
        return get_mute_settings(group_id)

    if key in {"enabled", "auto_mute_new_member", "auto_escalate"}:
        if isinstance(value, str):
            value = value.strip().lower() not in {"", "0", "false", "off", "no"}
        else:
            value = bool(value)
    elif key == "level_durations":
        value = _level_durations({"level_durations": value})
    elif key == "mention_mute_rules":
        value = list(value) if isinstance(value, (list, tuple)) else []
    elif key not in {"default_duration"}:
        value = _as_int(value, 0)

    def _update(settings):
        if settings is None:
            settings = {}
        settings[key] = value
        return settings
    update_group_data(group_id, "mute_settings", _update)
    return get_mute_settings(group_id)

def get_unmute_notification(group_id):
    return get_group_data(group_id, "unmute_notification", True)

def set_unmute_notification(group_id, enabled: bool):
    set_group_data(group_id, "unmute_notification", enabled)

def clear_unmute_notification(group_id):
    set_group_data(group_id, "unmute_notification", True)
