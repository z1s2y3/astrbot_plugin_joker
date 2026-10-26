import asyncio
import time
import threading
from .group_config import get_group_data, set_group_data, update_group_data, get_group_feature_detail
from .send_guard import can_send_group_message
from .console_log import print_group_action
try:  # AstrBot supplies the plugin logger in production.
    from astrbot.api import logger
except ImportError:
    try:
        from ..runtime_compat import logger
    except ImportError:
        from runtime_compat import logger

# 并发安全：踢出记录操作锁
_kick_lock = threading.RLock()


def _api_result_succeeded(result: object) -> bool:
    if not isinstance(result, dict):
        return True
    if result.get("success") is False:
        return False
    if result.get("retcode") not in (None, 0, "0"):
        return False
    return str(result.get("status", "") or "").strip().lower() not in {
        "failed", "failure", "error"
    }


async def _member_is_absent(client, group_id: str, user_id: str) -> bool:
    """Return true only when QQ confirms the member is no longer present."""
    if not callable(getattr(client, "call_action", None)):
        return False
    absent_tokens = (
        "not found", "not exist", "not in group", "not member",
        "member_not_found", "user_not_found", "不存在", "不在群", "不是群成员",
    )
    try:
        info = await client.call_action(
            action="get_group_member_info",
            group_id=int(group_id),
            user_id=int(user_id),
            no_cache=False,
        )
        if _api_result_succeeded(info):
            data = info.get("data", info) if isinstance(info, dict) else info
            if isinstance(data, dict) and any(
                str(data.get(key, "") or "").strip() == str(user_id)
                for key in ("user_id", "userId", "uid")
            ):
                return False
        detail = str(
            info.get("message") or info.get("msg") or info.get("wording") or info.get("retcode") or ""
        ).casefold() if isinstance(info, dict) else str(info or "").casefold()
        if any(token.casefold() in detail for token in absent_tokens):
            return True
    except Exception as exc:
        detail = str(exc).casefold()
        if any(token.casefold() in detail for token in absent_tokens):
            return True
    try:
        listing = await client.call_action(
            action="get_group_member_list",
            group_id=int(group_id),
        )
        if not _api_result_succeeded(listing):
            return False
        data = listing.get("data", listing) if isinstance(listing, dict) else listing
        if not isinstance(data, list):
            return False
        # An empty list is also returned by some QQ clients during a
        # temporary cache/transport failure.  Do not turn that uncertainty
        # into a false "already kicked" success.
        if not data:
            return False
        return not any(
            str(item.get("user_id", item.get("userId", item.get("uid", ""))) or "")
            == str(user_id)
            for item in data
            if isinstance(item, dict)
        )
    except Exception:
        return False


async def _recall_kick_notice(platform, group_id: str, message_id: int):
    """如果群启用了自动撤回，定时撤回踢出通知消息"""
    try:
        from .recall import is_self_recall_enabled, get_self_recall_time
        if not is_self_recall_enabled(group_id):
            return
        recall_time = get_self_recall_time(group_id)
        await asyncio.sleep(recall_time)
        # The master recall switch or the self-recall preference may have
        # changed during the delay.  Never withdraw a notice after either was
        # turned off.
        if not is_self_recall_enabled(group_id):
            return
        result = await platform.get_client().call_action(
            action="delete_msg",
            message_id=message_id
        )
        if not _api_result_succeeded(result):
            raise RuntimeError("QQ 平台拒绝了踢出通知撤回请求")
        logger.debug(f"已自动撤回踢出通知: {message_id}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"踢出通知自动撤回失败: {e}，消息ID:{message_id}")

def add_kick_record(group_id: str, user_id: str, operator_id: str, reason: str = "",
                    kick_type: str = "normal", notice: bool = True) -> bool:
    """添加踢出记录 (原子操作，线程安全)
    
    该函数确保在高并发场景下，同一用户的踢出记录只被处理一次。
    使用 RLock 保证操作的原子性，防止并发竞争。
    """
    with _kick_lock:  # 获取锁，确保原子操作
        def _add(records):
            if records is None:
                records = []

            # 检查是否已有该用户的记录
            for existing_record in records:
                if existing_record.get("user_id") == user_id:
                    existing_record["kicked_at"] = int(time.time())
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
    removed = False

    def _remove(records):
        nonlocal removed
        if records is None:
            return []
        kept = [r for r in records if r.get("user_id") != user_id]
        removed = len(kept) != len(records)
        return kept

    update_group_data(group_id, "kick_records", _remove)
    return removed

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
    settings = get_group_data(group_id, "kick_settings", default_settings)
    if not isinstance(settings, dict):
        return dict(default_settings)
    normalized = {**default_settings, **settings}
    normalized["limit"] = settings.get("limit", settings.get("max_kicks_before_ban", 3))
    normalized["auto_blacklist"] = settings.get(
        "auto_blacklist", settings.get("auto_ban_after_max", False)
    )
    normalized["max_kicks_before_ban"] = normalized["limit"]
    normalized["auto_ban_after_max"] = normalized["auto_blacklist"]
    return normalized

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
    if not settings.get("auto_blacklist", settings.get("auto_ban_after_max", True)):
        return False

    kick_count = get_kick_count(group_id, user_id)
    max_kicks = settings.get("limit", settings.get("max_kicks_before_ban", 3))

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

# 踢出去重缓存：防止短时间内对同一用户重复发送踢出通知
_kick_notice_cache = {}

async def kick_user_api(
    group_id: str,
    user_id: str,
    reason: str = "",
    context=None,
    platform_id: str = "",
    bot=None,
) -> bool:
    """
    使用框架内置方法踢出群成员。

    ``bot`` is the concrete OneBot client that delivered the related event.
    This matters on hosts with more than one QQ account: resolving the
    default platform can send ``set_group_kick`` through an account that is
    not in the target group and QQ then returns ``get Uid Error``.
    """
    try:
        from astrbot.api.event import filter as event_filter

        action_client = bot if callable(getattr(bot, "call_action", None)) else None
        platform = None
        if context:
            try:
                if platform_id and callable(getattr(context, "get_platform_inst", None)):
                    platform = context.get_platform_inst(str(platform_id))
                if platform is None:
                    platform = context.get_platform(event_filter.PlatformAdapterType.AIOCQHTTP)
            except Exception:
                platform = None
        if action_client is None and platform is not None:
            action_client = platform.get_client()
        
        # 踢出通知去重：同一用户在同一群60秒内只发送一次通知
        if reason:
            cache_key = f"{group_id}_{user_id}"
            current_time = int(time.time())
            if cache_key in _kick_notice_cache:
                last_sent = _kick_notice_cache[cache_key]
                if current_time - last_sent < 60:
                    logger.debug(f"用户 {user_id} 在群 {group_id} 的踢出通知已在60秒内发送过，跳过通知")
                    # 仍然执行踢出操作，但不发送重复通知
                    try:
                        if action_client:
                            result = await action_client.call_action(
                                action="set_group_kick",
                                group_id=int(group_id),
                                user_id=int(user_id),
                                reject_add_request=False
                            )
                            if not _api_result_succeeded(result):
                                raise RuntimeError("QQ 平台拒绝了踢出请求")
                            add_kick_record(group_id, user_id, "system", reason, kick_type="api")
                            return True
                    except Exception:
                        if await _member_is_absent(action_client, group_id, user_id):
                            add_kick_record(group_id, user_id, "system", reason, kick_type="api")
                            return True
                    return False
            _kick_notice_cache[cache_key] = current_time
        
        # 清理过期缓存（超过5分钟的记录）
        if len(_kick_notice_cache) > 100:
            now = int(time.time())
            expired = [k for k, v in _kick_notice_cache.items() if now - v > 300]
            for k in expired:
                del _kick_notice_cache[k]
        
        if action_client:
            # 先发送踢出原因通知
            if reason and can_send_group_message(group_id):
                try:
                    display_name = str(user_id)
                    try:
                        member_result = await action_client.call_action(
                            action="get_group_member_info",
                            group_id=int(group_id),
                            user_id=int(user_id),
                            no_cache=False,
                        )
                        member_data = (
                            member_result.get("data", member_result)
                            if isinstance(member_result, dict)
                            else member_result
                        )
                        if isinstance(member_data, dict):
                            display_name = str(
                                member_data.get("card")
                                or member_data.get("nickname")
                                or member_data.get("user_name")
                                or user_id
                            ).strip() or str(user_id)
                    except Exception:
                        logger.debug(
                            "获取踢出通知成员名称失败 group=%s user=%s",
                            group_id,
                            user_id,
                        )
                    kick_notice = (
                        f"用户 {display_name}（QQ：{user_id}）已被移出群聊\n"
                        f"原因：{reason}"
                    )
                    try:
                        detail = get_group_feature_detail(group_id, "kick")
                        template = str(detail.get("kick_success_message", "") or "").strip()
                        if template:
                            for key, value in {
                                "user_id": user_id,
                                "qq": user_id,
                                "user_name": display_name,
                                "nickname": display_name,
                                "reason": reason or "管理员操作",
                                "group_id": group_id,
                            }.items():
                                template = template.replace("{" + key + "}", str(value))
                            kick_notice = template[:2000]
                    except Exception:
                        pass
                    notice_result = await action_client.call_action(
                        action="send_group_msg",
                        group_id=int(group_id),
                        message=kick_notice
                    )
                    notice_msg_id = notice_result.get("message_id", 0) if notice_result else 0
                    print_group_action(
                        "踢出通知",
                        group_id,
                        user_id=user_id,
                        message=kick_notice,
                        message_id=notice_msg_id,
                    )
                    # A bound bot may differ from the context's default
                    # platform.  Do not attempt to recall through the wrong
                    # account in that case.
                    if notice_msg_id and platform is not None and bot is None:
                        asyncio.create_task(_recall_kick_notice(platform, str(group_id), notice_msg_id))
                except Exception:
                    pass
            
            # 然后踢出用户
            result = await action_client.call_action(
                action="set_group_kick",
                group_id=int(group_id),
                user_id=int(user_id),
                reject_add_request=False
            )
            if not _api_result_succeeded(result):
                detail = ""
                if isinstance(result, dict):
                    detail = str(
                        result.get("message")
                        or result.get("msg")
                        or result.get("wording")
                        or result.get("retcode")
                        or ""
                    ).strip()
                # A few NapCat/OneBot builds resolve a member UID only when
                # the raw user_id is sent as a string.  Retry once through the
                # same, account-bound client before reporting failure.
                if "uid" in detail.lower() or "user" in detail.lower():
                    try:
                        retry_result = await action_client.call_action(
                            action="set_group_kick",
                            group_id=str(group_id),
                            user_id=str(user_id),
                            reject_add_request=False,
                        )
                        if _api_result_succeeded(retry_result):
                            result = retry_result
                        else:
                            result = retry_result
                            if isinstance(retry_result, dict):
                                detail = str(
                                    retry_result.get("message")
                                    or retry_result.get("msg")
                                    or retry_result.get("wording")
                                    or retry_result.get("retcode")
                                    or detail
                                ).strip()
                    except Exception as retry_error:
                        detail = str(retry_error).strip() or detail
                if _api_result_succeeded(result):
                    add_kick_record(group_id, user_id, "system", reason, kick_type="api")
                    return True
                if await _member_is_absent(action_client, group_id, user_id):
                    add_kick_record(group_id, user_id, "system", reason, kick_type="api")
                    logger.info(
                        "QQ 已确认用户不在群内，按已踢出处理 group=%s user=%s",
                        group_id,
                        user_id,
                    )
                    return True
                logger.warning(
                    "QQ 踢人接口未确认成功 group=%s user=%s platform=%s detail=%s",
                    group_id,
                    user_id,
                    platform_id or "bound_bot",
                    detail or "unknown",
                )
                raise RuntimeError("QQ 平台拒绝了踢出请求")
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
