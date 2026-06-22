import re
import time
import asyncio
from astrbot.api import logger
from .group_config import get_group_data, set_group_data, update_group_data

def parse_time_duration(time_str: str) -> int:
    if not time_str:
        return 0
    time_str = str(time_str).strip().lower()
    match = re.match(r'^(\d+)([smhd])?$', time_str)
    if not match:
        try:
            return int(time_str)
        except ValueError:
            return 0
    value = int(match.group(1))
    unit = match.group(2) or ""
    if unit == "s":
        return value
    elif unit == "m":
        return value * 60
    elif unit == "h":
        return value * 3600
    elif unit == "d":
        return value * 86400
    return value

# 撤回设置
def set_recall_enabled(group_id: str, enabled: bool):
    """设置撤回功能启用状态"""
    from .group_config import load_group_config, save_group_config, init_group_config
    
    config = load_group_config(group_id)
    if not config:
        config = init_group_config(group_id)
    
    # 设置根级别配置
    config["recall_enabled"] = enabled
    
    # 设置features.recall下的配置
    if "features" not in config:
        config["features"] = {}
    if "recall" not in config["features"]:
        config["features"]["recall"] = {}
    config["features"]["recall"]["enabled"] = enabled
    
    save_group_config(group_id, config)

def is_recall_enabled(group_id: str) -> bool:
    """检查撤回功能是否启用"""
    from .group_config import load_group_config
    
    config = load_group_config(group_id)
    if not config:
        return False
    
    # 优先检查features.recall下的配置
    features = config.get("features", {})
    recall = features.get("recall", {})
    result = recall.get("enabled", None)
    if result is not None:
        return result
    
    # 兼容旧的根级别配置
    return config.get("recall_enabled", False)

def set_recall_time(group_id: str, seconds: int):
    set_group_data(group_id, "recall_time", seconds)

def get_recall_time(group_id: str) -> int:
    return get_group_data(group_id, "recall_time", 60)

def set_recall_mode(group_id: str, mode: str):
    set_group_data(group_id, "recall_mode", mode)

def get_recall_mode(group_id: str) -> str:
    return get_group_data(group_id, "recall_mode", "keyword")

# 关键词撤回
def add_recall_keyword(group_id: str, keyword: str):
    def _add(keywords):
        if keywords is None:
            keywords = []
        if keyword not in keywords:
            keywords.append(keyword)
        return keywords
    update_group_data(group_id, "recall_keywords", _add)

def remove_recall_keyword(group_id: str, keyword: str):
    def _remove(keywords):
        if keywords is None:
            return []
        if keyword in keywords:
            keywords.remove(keyword)
        return keywords
    update_group_data(group_id, "recall_keywords", _remove)

def get_recall_keywords(group_id: str) -> list:
    return get_group_data(group_id, "recall_keywords", [])

def should_recall_message(group_id: str, message: str) -> bool:
    if not is_recall_enabled(group_id):
        return False
    keywords = get_recall_keywords(group_id)
    if not keywords:
        return True
    for keyword in keywords:
        if keyword in message:
            return True
    return False

# 正则撤回
def add_recall_regex(group_id: str, pattern: str):
    def _add(regexes):
        if regexes is None:
            regexes = []
        if pattern not in regexes:
            regexes.append(pattern)
        return regexes
    update_group_data(group_id, "recall_regex", _add)

def remove_recall_regex(group_id: str, pattern: str):
    def _remove(regexes):
        if regexes is None:
            return []
        if pattern in regexes:
            regexes.remove(pattern)
        return regexes
    update_group_data(group_id, "recall_regex", _remove)

def get_recall_regex_list(group_id: str) -> list:
    return get_group_data(group_id, "recall_regex", [])

def should_recall_by_regex(group_id: str, message: str) -> bool:
    patterns = get_recall_regex_list(group_id)
    for pattern in patterns:
        try:
            if re.search(pattern, message):
                return True
        except re.error:
            continue
    return False

# 白名单用户（不被撤回）
def add_whitelist_user(group_id: str, user_id: str):
    def _add(whitelist):
        if whitelist is None:
            whitelist = []
        if user_id not in whitelist:
            whitelist.append(user_id)
        return whitelist
    update_group_data(group_id, "recall_whitelist", _add)

def remove_whitelist_user(group_id: str, user_id: str):
    def _remove(whitelist):
        if whitelist is None:
            return []
        if user_id in whitelist:
            whitelist.remove(user_id)
        return whitelist
    update_group_data(group_id, "recall_whitelist", _remove)

def is_user_whitelisted(group_id: str, user_id: str) -> bool:
    whitelist = get_group_data(group_id, "recall_whitelist", [])
    return user_id in whitelist

def get_whitelist(group_id: str) -> list:
    return get_group_data(group_id, "recall_whitelist", [])

# 撤回日志
def log_recall(group_id: str, user_id: str, message_id: str, content: str,
               operator_id: str = "system", reason: str = ""):
    def _add(logs):
        if logs is None:
            logs = []
        logs.append({
            "user_id": user_id,
            "message_id": message_id,
            "content": content,
            "operator_id": operator_id,
            "reason": reason,
            "recalled_at": int(time.time())
        })
        if len(logs) > 500:
            logs = logs[-250:]
        return logs
    update_group_data(group_id, "recall_logs", _add)

def get_recall_log(group_id: str, limit: int = 50) -> list:
    logs = get_group_data(group_id, "recall_logs", [])
    return logs[-limit:] if logs else []

def get_user_recall_history(group_id: str, user_id: str, limit: int = 20) -> list:
    logs = get_group_data(group_id, "recall_logs", [])
    return [l for l in logs if l.get("user_id") == user_id][-limit:]

# 自身撤回
def enable_self_recall(group_id: str, enabled: bool = True):
    """启用/禁用自身撤回"""
    from .group_config import load_group_config, save_group_config, init_group_config
    config = load_group_config(group_id)
    if not config:
        config = init_group_config(group_id)
    
    # 设置根级别配置（兼容性）
    config["self_recall_enabled"] = enabled
    
    # 设置features.recall下的配置
    if "features" not in config:
        config["features"] = {}
    if "recall" not in config["features"]:
        config["features"]["recall"] = {}
    config["features"]["recall"]["self_recall_enabled"] = enabled
    
    save_group_config(group_id, config)
    logger.info(f"enable_self_recall: 已设置群 {group_id} 的自身撤回为 {enabled}")

def is_self_recall_enabled(group_id: str) -> bool:
    """检查自身撤回是否启用"""
    from .group_config import load_group_config
    config = load_group_config(group_id)
    
    if not config:
        return False
    
    # 优先检查features.recall下的配置
    features = config.get("features", {})
    recall = features.get("recall", {})
    result = recall.get("self_recall_enabled", None)
    if result is not None:
        return result
    
    # 兼容旧的根级别配置
    return config.get("self_recall_enabled", False)

def set_self_recall_time(group_id: str, seconds: int):
    """设置自身撤回时间"""
    from .group_config import load_group_config, save_group_config, init_group_config
    
    seconds = max(5, min(seconds, 300))
    
    config = load_group_config(group_id)
    if not config:
        config = init_group_config(group_id)
    
    # 设置根级别配置
    config["self_recall_time"] = seconds
    
    # 设置features.recall下的配置
    if "features" not in config:
        config["features"] = {}
    if "recall" not in config["features"]:
        config["features"]["recall"] = {}
    config["features"]["recall"]["self_recall_time"] = seconds
    
    save_group_config(group_id, config)

def get_self_recall_time(group_id: str) -> int:
    """获取自身撤回时间"""
    from .group_config import load_group_config
    
    config = load_group_config(group_id)
    if not config:
        return 30
    
    # 优先检查features.recall下的配置
    features = config.get("features", {})
    recall = features.get("recall", {})
    result = recall.get("self_recall_time", None)
    if result is not None:
        return result
    
    # 兼容旧的根级别配置
    return config.get("self_recall_time", 30)

def get_self_recall_status(group_id: str) -> dict:
    return {
        "enabled": is_self_recall_enabled(group_id),
        "time": get_self_recall_time(group_id)
    }

# 待撤回任务（内存存储）
_pending_self_recalls = {}
_MAX_PENDING_SELF_RECALLS = 200

async def schedule_self_recall(bot, group_id: str, message_id: int, recall_seconds: int):
    """安排消息延迟撤回"""
    if not is_self_recall_enabled(group_id):
        return

    task_key = f"{group_id}_{message_id}"
    if task_key in _pending_self_recalls:
        return

    if len(_pending_self_recalls) >= _MAX_PENDING_SELF_RECALLS:
        oldest_key = next(iter(_pending_self_recalls))
        oldest_task = _pending_self_recalls.pop(oldest_key, None)
        if oldest_task:
            oldest_task.cancel()

    async def delayed_recall():
        try:
            await asyncio.sleep(recall_seconds)
            if not is_self_recall_enabled(group_id):
                return
            await bot.delete_msg(message_id=int(message_id))
            logger.info(f"✅ 已自动撤回消息: {message_id}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"❌ 自动撤回失败: {e}")
        finally:
            _pending_self_recalls.pop(task_key, None)

    _pending_self_recalls[task_key] = asyncio.create_task(delayed_recall())

def cancel_self_recall(group_id: str, message_id: int):
    task_key = f"{group_id}_{message_id}"
    if task_key in _pending_self_recalls:
        _pending_self_recalls[task_key].cancel()
        del _pending_self_recalls[task_key]
        return True
    return False

def cancel_all_self_recalls(group_id: str = None):
    cancelled = 0
    if group_id:
        keys_to_cancel = [k for k in _pending_self_recalls if k.startswith(f"{group_id}_")]
    else:
        keys_to_cancel = list(_pending_self_recalls.keys())

    for key in keys_to_cancel:
        task = _pending_self_recalls.pop(key, None)
        if task:
            task.cancel()
            cancelled += 1
    return cancelled

def get_pending_self_recalls(group_id: str = None) -> list:
    if group_id:
        return [k for k in _pending_self_recalls if k.startswith(f"{group_id}_")]
    return list(_pending_self_recalls.keys())

def get_pending_self_recalls_count() -> int:
    return len(_pending_self_recalls)

def load_recall_settings(group_id: str) -> dict:
    return {
        "enabled": is_recall_enabled(group_id),
        "time": get_recall_time(group_id),
        "mode": get_recall_mode(group_id),
        "keywords": get_recall_keywords(group_id),
        "regex": get_recall_regex_list(group_id),
        "whitelist": get_whitelist(group_id),
        "self_recall_enabled": is_self_recall_enabled(group_id),
        "self_recall_time": get_self_recall_time(group_id)
    }

def save_recall_settings(group_id: str, settings: dict):
    for key, value in settings.items():
        if key == "enabled":
            set_recall_enabled(group_id, value)
        elif key == "time":
            set_recall_time(group_id, value)
        elif key == "mode":
            set_recall_mode(group_id, value)
        elif key == "self_recall_enabled":
            enable_self_recall(group_id, value)
        elif key == "self_recall_time":
            set_self_recall_time(group_id, value)

def enable_recall(group_id: str, enabled: bool):
    set_recall_enabled(group_id, enabled)

def get_recall_settings(group_id: str) -> dict:
    return load_recall_settings(group_id)

def set_recall_settings(group_id: str, key: str, value):
    if key == "enabled":
        set_recall_enabled(group_id, value)
    elif key == "time":
        set_recall_time(group_id, value)
    elif key == "mode":
        set_recall_mode(group_id, value)

def get_recall_statistics(group_id: str) -> dict:
    logs = get_group_data(group_id, "recall_logs", [])
    keywords = get_recall_keywords(group_id)
    regex = get_recall_regex_list(group_id)
    whitelist = get_whitelist(group_id)
    return {
        "total_recalls": len(logs),
        "keywords_count": len(keywords),
        "regex_count": len(regex),
        "whitelist_count": len(whitelist),
        "self_recall_enabled": is_self_recall_enabled(group_id)
    }

def set_whitelist_mode(group_id: str, enabled: bool):
    set_group_data(group_id, "recall_whitelist_mode", enabled)

def is_whitelist_mode(group_id: str) -> bool:
    return get_group_data(group_id, "recall_whitelist_mode", False)

async def recall_user_messages(group_id: str, user_id: str, count: int = 10, api_client=None, recall_time: int = 43200, bot=None):
    """撤回用户的消息"""
    try:
        if not bot:
            return {"success": False, "error": "Bot实例未提供"}
        
        # 使用 message_cache 获取用户消息
        from .message_cache import get_user_cached_messages
        messages = get_user_cached_messages(group_id, user_id, limit=count)
        
        success = 0
        failed = 0
        now = time.time()
        
        for msg in messages:
            msg_time = msg.get("time", 0)
            if now - msg_time > recall_time:
                continue  # 超过撤回时间
            
            msg_id = msg.get("message_id")
            if msg_id:
                try:
                    await bot.delete_msg(message_id=int(msg_id))
                    success += 1
                except Exception:
                    failed += 1
        
        return {"success": True, "result": {"success": success, "failed": failed, "skipped": 0}}

    except Exception as e:
        return {"success": False, "error": str(e)}

async def recall_recent_messages(group_id: str, count: int = 10, api_client=None, recall_time: int = 43200, bot=None, exclude_admins: bool = True, exclude_message_ids: list = None):
    """撤回最近的群消息"""
    try:
        if not bot:
            return {"success": False, "error": "Bot实例未提供"}
        
        # 使用 message_cache 获取最近消息（多取一些作为缓冲）
        from .message_cache import get_recent_cached_messages
        messages = get_recent_cached_messages(group_id, limit=count + 10)
        
        success = 0
        failed = 0
        skipped = 0
        skipped_admin = 0
        now = time.time()
        
        # 需要排除的消息ID列表（撤回命令本身）
        exclude_ids = set(exclude_message_ids or [])
        
        # 获取管理员和群主列表
        admin_ids = set()
        if exclude_admins:
            try:
                # 通过API获取群管理员列表
                member_list = await bot.call_action(
                    action="get_group_member_list",
                    group_id=int(group_id)
                )
                for member in member_list:
                    role = member.get("role", "")
                    if role in ["admin", "owner"]:
                        admin_ids.add(str(member.get("user_id", "")))
            except Exception as e:
                logger.warning(f"获取管理员列表失败: {e}")
        
        # 按message_id从小到大排序，从旧到新撤回
        messages.sort(key=lambda x: int(x.get("message_id", 0)))
        
        for msg in messages:
            msg_time = msg.get("time", 0)
            if now - msg_time > recall_time:
                continue
            
            # 跳过排除的消息（撤回命令本身）
            msg_id = msg.get("message_id")
            if msg_id and int(msg_id) in exclude_ids:
                skipped += 1
                continue
            
            # 跳过管理员
            msg_user_id = str(msg.get("user_id", ""))
            if msg_user_id in admin_ids:
                skipped_admin += 1
                continue
            
            if msg_id:
                try:
                    await bot.delete_msg(message_id=int(msg_id))
                    success += 1
                    if success >= count:
                        break
                except Exception:
                    failed += 1
        
        return {"success": True, "result": {
            "success": success, 
            "failed": failed, 
            "skipped": skipped,
            "skipped_admin": skipped_admin
        }}

    except Exception as e:
        return {"success": False, "error": str(e)}

def get_max_recall_count(group_id: str = None) -> int:
    """获取最大撤回数量限制"""
    return 50

def get_recall_help_text() -> str:
    """获取撤回系统帮助文本"""
    return """📋 撤回系统使用说明

【命令格式】
/撤回 @用户 [数量] - 撤回指定用户的消息
/撤回 [数量] - 撤回最近的群消息

【参数说明】
- @用户：可选，指定要撤回消息的用户
- 数量：可选，撤回的消息数量（默认10条，最大50条）
- 时间：可选，撤回的时间范围，如 12h、30m

【使用示例】
/撤回 @张三 5    - 撤回用户张三最近5条消息
/撤回 20        - 撤回群内最近20条消息
/撤回 5 24h     - 撤回最近24小时内的5条消息

【注意事项】
- 只能撤回机器人发送的消息
- 部分消息可能已超过撤回时限
- 管理员消息不会被撤回"""

def get_group_admins_and_owner(group_id: str) -> list:
    return []

__all__ = [
    "set_recall_enabled", "is_recall_enabled",
    "set_recall_time", "get_recall_time",
    "set_recall_mode", "get_recall_mode",
    "add_recall_keyword", "remove_recall_keyword", "get_recall_keywords",
    "add_recall_regex", "remove_recall_regex", "get_recall_regex_list",
    "add_whitelist_user", "remove_whitelist_user", "get_whitelist",
    "is_user_whitelisted", "set_whitelist_mode", "is_whitelist_mode",
    "log_recall", "get_recall_log", "get_user_recall_history",
    "enable_recall",
    "set_recall_settings", "get_recall_settings", "get_recall_statistics",
    "is_self_recall_enabled", "enable_self_recall",
    "get_self_recall_time", "set_self_recall_time",
    "get_self_recall_status",
    "recall_user_messages", "recall_recent_messages",
    "get_max_recall_count",
    "parse_time_duration",
    "get_recall_help_text",
    "schedule_self_recall",
    "cancel_self_recall",
    "cancel_all_self_recalls",
    "get_pending_self_recalls",
    "get_pending_self_recalls_count"
]
