import re
import time
import asyncio
from astrbot.api import logger
from .group_config import get_group_data, set_group_data, update_group_data
from . import message_cache

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

    config["recall_enabled"] = enabled

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

    features = config.get("features", {})
    recall = features.get("recall", {})
    result = recall.get("enabled", None)
    if result is not None:
        return result

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

# 白名单用户
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

    config["self_recall_enabled"] = enabled

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

    features = config.get("features", {})
    recall = features.get("recall", {})
    result = recall.get("self_recall_enabled", None)
    if result is not None:
        return result

    return config.get("self_recall_enabled", False)

def set_self_recall_time(group_id: str, seconds: int):
    """设置自身撤回时间"""
    from .group_config import load_group_config, save_group_config, init_group_config

    seconds = max(5, min(seconds, 300))

    config = load_group_config(group_id)
    if not config:
        config = init_group_config(group_id)

    config["self_recall_time"] = seconds

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

    features = config.get("features", {})
    recall = features.get("recall", {})
    result = recall.get("self_recall_time", None)
    if result is not None:
        return result

    return config.get("self_recall_time", 30)

def get_self_recall_status(group_id: str) -> dict:
    return {
        "enabled": is_self_recall_enabled(group_id),
        "time": get_self_recall_time(group_id)
    }

# 待撤回任务
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
            logger.info(f"已自动撤回消息: {message_id}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"自动撤回失败: {e}")
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

def get_recall_help_text() -> str:
    """获取撤回系统帮助文本"""
    return """📋 撤回系统使用说明

【命令格式】
/撤回 @用户 [数量] - 撤回指定用户的消息
/撤回 [数量] - 撤回最近的群消息

【参数说明】
- @用户：可选，指定要撤回消息的用户
- 数量：可选，撤回的消息数量（默认10条，最大50条）

【使用示例】
/撤回 @张三 5    - 撤回用户张三最近5条消息
/撤回 20        - 撤回群内最近20条消息

【注意事项】
- 只能撤回5分钟内的消息
- 部分消息可能已超过撤回时限"""

# ==================== 核心撤回功能 ====================

async def recall_messages(event, target_user_ids: set = None, count: int = 10, exclude_admins: bool = True):
    """
    撤回消息的核心实现
    参考 astrbot_plugin_qqadmin 的实现方式

    Args:
        event: 消息事件对象
        target_user_ids: 目标用户ID集合，None表示所有用户
        count: 撤回数量
        exclude_admins: 是否排除管理员

    Returns:
        dict: 包含成功、失败数量的结果
    """
    client = event.bot
    group_id = event.get_group_id()
    group_id_str = str(group_id)

    # 统计
    success = 0
    failed = 0
    skipped_admin = 0
    skipped_not_target = 0
    failed_reasons = {}

    # 获取管理员列表（如果需要排除）
    admin_ids = set()
    if exclude_admins:
        try:
            member_list = await client.call_action(
                action="get_group_member_list",
                group_id=int(group_id)
            )
            for member in member_list:
                role = member.get("role", "")
                if role in ["admin", "owner"]:
                    admin_ids.add(str(member.get("user_id", "")))
        except Exception as e:
            logger.warning(f"获取管理员列表失败: {e}")

    # 如果有目标用户，从API获取消息并筛选
    messages = []
    if target_user_ids:
        # 使用event.bot.call_action获取消息（参考astrbot_plugin_batchrecall）
        try:
            fetch_count = min(count * 3, 100)
            payloads = {
                "group_id": int(group_id),
                "count": fetch_count,
            }
            result = await client.call_action("get_group_msg_history", **payloads)
            api_messages = result.get("messages", []) if isinstance(result, dict) else []
            logger.info(f"从API获取到 {len(api_messages)} 条群消息")

            # 按time倒序排序（时间戳倒序，最新的在前）
            api_messages.sort(key=lambda item: item.get("time", 0), reverse=True)

            # 从最新消息往前找，找到目标用户的发言
            logger.info(f"开始从最新消息往前找目标用户 {target_user_ids} 的发言...")
            for msg in api_messages:
                sender = msg.get("sender", {})
                if isinstance(sender, dict):
                    sender_id = str(sender.get("user_id", ""))
                else:
                    sender_id = str(sender) if sender else ""

                if sender_id in target_user_ids:
                    msg_id = msg.get("message_id", "?")
                    msg_time = msg.get("time", 0)
                    logger.info(f"✅ 找到目标用户发言: msg_id={msg_id}, time={msg_time}, sender={sender_id}")
                    messages.append(msg)
                    if len(messages) >= count:
                        break
                else:
                    logger.info(f"⏭️ 跳过: sender={sender_id}, 需要={target_user_ids}")

        except Exception as e:
            logger.error(f"获取群消息历史失败: {e}")
            return {"success": 0, "failed": 0, "error": str(e)}

        # 如果API消息不够，从缓存补充
        if len(messages) < count:
            logger.info(f"API只找到 {len(messages)} 条，从缓存补充...")
            for user_id in target_user_ids:
                cached_msgs = message_cache.get_user_cached_messages(
                    group_id_str, user_id, limit=count * 5, include_recalled=True
                )
                logger.info(f"从缓存获取用户 {user_id} 的 {len(cached_msgs)} 条消息")
                # 按message_id倒序排序
                cached_msgs.sort(key=lambda x: message_cache._safe_int(x.get("message_id"), 0), reverse=True)

                for msg in cached_msgs:
                    msg_user_id = str(msg.get("user_id", ""))
                    if msg_user_id in target_user_ids:
                        # 避免重复
                        msg_id = msg.get("message_id")
                        if not any(m.get("message_id") == msg_id for m in messages):
                            messages.append(msg)
                            if len(messages) >= count:
                                break
                if len(messages) >= count:
                    break

        logger.info(f"获取到目标用户 {len(messages)} 条消息，需要撤回 {count} 条")

        if not messages:
            return {"success": 0, "failed": 0, "error": f"用户最近没有发言记录"}

    # 如果没有目标用户（撤回所有人的消息），使用API获取
    if not target_user_ids:
        payloads = {
            "group_id": int(group_id),
            "message_seq": 0,
            "count": count * 2,
            "reverseOrder": True,
        }

        try:
            result = await client.api.call_action("get_group_msg_history", **payloads)
            messages = list(reversed(result.get("messages", [])))
            logger.info(f"获取到 {len(messages)} 条群消息，需要撤回 {count} 条")
        except Exception as e:
            logger.error(f"获取群消息历史失败: {e}")
            return {"success": 0, "failed": 0, "error": str(e)}

    if not messages:
        return {"success": 0, "failed": 0, "error": "没有获取到消息"}

    # 参考astrbot_plugin_batchrecall的执行方式：直接遍历，不做复杂并发控制
    success = 0
    failed = 0

    for msg in messages:
        message_id = msg.get("message_id")
        if not message_id:
            continue

        try:
            await client.delete_msg(message_id=message_id)
            success += 1
            logger.info(f"✅ 成功撤回消息: {message_id}")
        except Exception as e:
            failed += 1
            err_str = str(e)
            # 简化错误信息
            if "1003" in err_str or "超时" in err_str:
                logger.warning(f"撤回消息 {message_id} 失败: 超过撤回时限或超时")
            else:
                logger.warning(f"撤回消息 {message_id} 失败: {err_str[:100]}")

    result_dict = {
        "success": success,
        "failed": failed,
        "total": len(messages)
    }
    logger.info(f"撤回完成: {result_dict}")
    return result_dict


async def recall_user_messages_by_api(event, user_id: str, count: int = 10):
    """
    撤回指定用户的消息（使用API获取消息）
    """
    target_ids = {str(user_id)}
    return await recall_messages(event, target_ids, count, exclude_admins=False)


async def recall_recent_messages_by_api(event, count: int = 10, exclude_admins: bool = True):
    """
    撤回最近的群消息（使用API获取消息）
    """
    return await recall_messages(event, target_user_ids=None, count=count, exclude_admins=exclude_admins)


# 保持向后兼容的旧接口
async def recall_user_messages(group_id: str, user_id: str, count: int = 10, api_client=None, recall_time: int = 43200, bot=None):
    """撤回用户的消息（向后兼容接口）"""
    # 这个接口需要事件对象，旧的参数格式不再支持
    return {"success": False, "error": "请使用新的recall_user_messages_by_api接口，需要事件对象"}


async def recall_recent_messages(group_id: str, count: int = 10, api_client=None, recall_time: int = 43200, bot=None, exclude_admins: bool = True, exclude_message_ids: list = None):
    """撤回最近的群消息（向后兼容接口）"""
    return {"success": False, "error": "请使用新的recall_recent_messages_by_api接口，需要事件对象"}


def get_max_recall_count(group_id: str = None) -> int:
    """获取最大撤回数量限制"""
    return 50


def _safe_int(value, default=0):
    """安全地将值转换为整数，失败时返回默认值"""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        try:
            if isinstance(value, str):
                v = value.strip()
                if v.startswith('0x') or v.startswith('0X'):
                    return int(v, 16)
        except (ValueError, TypeError):
            pass
        return default


def _normalize_msg_id(value):
    """将消息ID规范化为字符串形式用于比较"""
    if value is None:
        return ""
    return str(value).strip().lower()


def get_group_admins_and_owner_sync(group_id: str) -> dict:
    """获取群的管理员和群主信息（同步版本，返回空字典）"""
    return {}


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
    "recall_user_messages_by_api", "recall_recent_messages_by_api",
    "recall_messages",
    "get_max_recall_count",
    "get_group_admins_and_owner_sync",
    "_safe_int",
    "_normalize_msg_id",
    "parse_time_duration",
    "get_recall_help_text",
    "schedule_self_recall",
    "cancel_self_recall",
    "cancel_all_self_recalls",
    "get_pending_self_recalls",
    "get_pending_self_recalls_count",
]
