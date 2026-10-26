import logging
import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from .group_config import get_group_data, set_group_data, update_group_data
logger = logging.getLogger(__name__)


def _dispatch_lock_path(group_id: str, msg_id: str) -> Path:
    """Return a process-shared lease path for one scheduled task."""
    digest = hashlib.sha256(
        f"{str(group_id)}:{str(msg_id)}".encode("utf-8", "ignore")
    ).hexdigest()[:32]
    root = Path(__file__).resolve().parent / ".timed_dispatch_locks"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.lock"


def _acquire_dispatch_file_lock(group_id: str, msg_id: str, lease: int) -> Path | bool | None:
    """Atomically claim a short lease shared by separate bot processes."""
    try:
        path = _dispatch_lock_path(group_id, msg_id)
    except OSError as exc:
        # A read-only plugin directory must not disable all timed tasks. The
        # persisted group-config lease remains the fallback in this case.
        logger.debug("定时任务目录不可写，使用配置租约 error=%s", type(exc).__name__)
        return False
    now = int(time.time())
    for _attempt in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = path.read_text(encoding="ascii").strip().split(" ", 1)
                expires = int(existing[0]) if existing else 0
                owner_pid = int(existing[1]) if len(existing) > 1 else 0
            except (OSError, TypeError, ValueError):
                expires = 0
                owner_pid = 0
            try:
                clock_skewed_backwards = now + 86400 < int(path.stat().st_mtime)
            except OSError:
                clock_skewed_backwards = False
            if clock_skewed_backwards:
                expires = 0
            if expires > now:
                # Remove leases left by a crashed process. This also keeps a
                # previous test/runtime lock from blocking a fresh process.
                if owner_pid and owner_pid != os.getpid():
                    try:
                        os.kill(owner_pid, 0)
                    except ProcessLookupError:
                        pass
                    except OSError:
                        return None
                    else:
                        return None
                else:
                    return None
            try:
                path.unlink()
            except OSError:
                return None
            continue
        except OSError as exc:
            logger.debug("创建定时任务跨进程锁失败 path=%s error=%s", path, type(exc).__name__)
            return None
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(f"{now + lease} {os.getpid()}")
            return path
        except OSError:
            try:
                path.unlink()
            except OSError:
                pass
            return None
    return None


def _release_dispatch_file_lock(group_id: str, msg_id: str) -> None:
    try:
        _dispatch_lock_path(group_id, msg_id).unlink()
    except OSError:
        pass

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


_DAILY_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_AT_ALL_MARKERS = ("@全体", "＠全体")
# The scheduler polls every few seconds, but a slow QQ request, a music
# lookup, or a hot reload can still cross a minute boundary.  Treat a recently
# missed cron slot as due once instead of silently dropping it.  This is kept
# intentionally short so an offline bot never sends a backlog after restart.
_CRON_CATCHUP_SECONDS = 5 * 60
# Hourly reports are expected to recover after a plugin update or a brief
# Windows/QQ reconnect. Keep only the newest missed hour eligible for a quiet
# catch-up send, never an offline backlog.
_HOURLY_CATCHUP_SECONDS = 65 * 60


def cron_from_daily_time(value: str) -> str | None:
    """Convert an HH:MM daily broadcast time into the existing cron format."""
    normalized = str(value or "").strip()
    if not _DAILY_TIME_RE.fullmatch(normalized):
        return None
    hour, minute = normalized.split(":")
    return f"{int(minute)} {int(hour)} * * *"


def parse_one_time_datetime(value: str) -> int | None:
    """Parse a local one-time schedule into an epoch timestamp.

    The scheduler intentionally uses the Windows process local timezone, which
    is also the timezone used by existing Cron tasks.  Both a browser's
    ``datetime-local`` value and the command-friendly ``YYYY-MM-DD HH:MM``
    form are accepted.
    """
    normalized = str(value or "").strip().replace("T", " ")
    if not normalized:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        return int(time.mktime(parsed.timetuple()))
    return None


def parse_daily_broadcast_arguments(value: str) -> tuple[str, bool, str] | None:
    """Parse either supported daily broadcast command argument order.

    Accepted forms are ``HH:MM content``, ``HH:MM @all content``, and
    ``@all HH:MM content``.  The caller remains responsible for validating
    the time value and permissions.
    """
    text = str(value or "").strip()
    if not text:
        return None

    leading_parts = text.split(maxsplit=2)
    if leading_parts and leading_parts[0] in _AT_ALL_MARKERS:
        if len(leading_parts) < 3:
            return None
        return leading_parts[1], True, leading_parts[2].strip()

    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    time_text, content = parts[0], parts[1].strip()
    for marker in _AT_ALL_MARKERS:
        if content == marker:
            return time_text, True, ""
        if content.startswith(f"{marker} "):
            return time_text, True, content[len(marker):].strip()
    return time_text, False, content


def parse_one_time_broadcast_arguments(value: str) -> tuple[str, bool, str] | None:
    """Parse ``YYYY-MM-DD HH:MM content`` with the same @all forms."""
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(maxsplit=3)
    if parts and parts[0] in _AT_ALL_MARKERS:
        if len(parts) < 4:
            return None
        return f"{parts[1]} {parts[2]}", True, parts[3].strip()
    if len(parts) < 3:
        return None
    if parts[2] in _AT_ALL_MARKERS:
        return f"{parts[0]} {parts[1]}", True, parts[3].strip() if len(parts) == 4 else ""
    return f"{parts[0]} {parts[1]}", False, " ".join(parts[2:]).strip()

def validate_cron_expr(cron_expr: str) -> dict:
    """验证cron表达式的有效性
    
    Args:
        cron_expr: cron表达式
    
    Returns:
        验证结果，包含error字段表示验证失败
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return {"error": f"cron表达式参数不足，需要5个参数，当前{len(parts)}个"}
    
    def is_valid_value(value, min_val, max_val, field_name):
        for token in value.split(","):
            token = token.strip()
            if not token:
                return False, f"{field_name}列表中存在空值"
            base, separator, step_text = token.partition("/")
            if separator:
                try:
                    step = int(step_text)
                except ValueError:
                    return False, f"{field_name}步长无效: {token}"
                if step <= 0:
                    return False, f"{field_name}步长必须大于0"
            if base == "*":
                continue
            bounds = base.split("-")
            if len(bounds) > 2:
                return False, f"{field_name}范围格式错误: {token}"
            try:
                numbers = [int(item) for item in bounds]
            except ValueError:
                return False, f"{field_name}值无效: {token}"
            if any(number < min_val or number > max_val for number in numbers):
                return False, f"{field_name}值超出有效范围"
            if len(numbers) == 2 and numbers[0] > numbers[1]:
                return False, f"{field_name}范围起始值不能大于结束值"
        return True, ""

    fields = (
        (parts[0], 0, 59, "分钟"),
        (parts[1], 0, 23, "小时"),
        (parts[2], 1, 31, "日期"),
        (parts[3], 1, 12, "月份"),
        (parts[4], 0, 6, "星期"),
    )
    for expression, minimum, maximum, name in fields:
        valid, err = is_valid_value(expression, minimum, maximum, name)
        if not valid:
            return {"error": err}

    return {"success": True}


def add_timed_message(group_id: str, message: str, cron_expr: str,
                     at_all: bool = False, enabled: bool = True,
                     operator_id: str = "", name: str = "", action: str = "message",
                     run_at: str | int | None = None) -> dict:
    """添加定时消息
    
    Args:
        group_id: 群ID
        message: 消息内容（action为message时使用）
        cron_expr: cron表达式
        at_all: 是否@全体成员
        enabled: 是否启用
        operator_id: 操作人ID
        name: 消息名称
        action: 操作类型，可选值: message(发送消息), random_music(随机音乐),
        hourly_time(整点报时), whole_ban(全体禁言), whole_unban(全体解禁)
    
    Returns:
        添加结果
    """
    if action not in {"message", "random_music", "hourly_time", "whole_ban", "whole_unban"}:
        return {"success": False, "message": "定时任务类型无效"}
    # 整点报时始终是循环任务：每个小时的 00 分发送一次。不要让旧网页
    # 表单、同步数据或手动调用把它保存成其他 Cron / 一次性任务。
    if action == "hourly_time":
        cron_expr = "0 * * * *"
        run_at = None
    if action == "message" and not str(message or "").strip():
        return {"success": False, "message": "定时消息内容不能为空"}
    run_at_epoch = 0
    if isinstance(run_at, (int, float)) and not isinstance(run_at, bool):
        run_at_epoch = int(run_at)
    elif str(run_at or "").strip():
        run_at_epoch = parse_one_time_datetime(str(run_at)) or 0
    if run_at_epoch:
        if run_at_epoch <= int(time.time()):
            return {"success": False, "message": "一次性发送时间必须晚于当前时间"}
        cron_expr = ""
    else:
        cron_valid = validate_cron_expr(cron_expr)
        if "error" in cron_valid:
            return {"success": False, "message": cron_valid["error"]}
    
    messages = load_timed_messages(group_id)
    
    # Second-resolution IDs collide when multiple tasks are created together.
    msg_id = f"{group_id}_{uuid4().hex}"
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
        "name": name or f"定时消息{len(messages) + 1}",
        "action": action,
        "run_at": run_at_epoch,
        "one_time": bool(run_at_epoch),
    }
    
    messages.append(entry)
    save_timed_messages(group_id, messages)
    
    action_names = {
        "message": "定时消息",
        "random_music": "随机音乐",
        "hourly_time": "整点报时",
        "whole_ban": "定时全体禁言",
        "whole_unban": "定时全体解禁",
    }
    return {"success": True, "id": msg_id, "message": f"{action_names.get(action, '定时任务')}添加成功"}

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
    updated = False

    def _update(messages):
        nonlocal updated
        if messages is None:
            return []
        for msg in messages:
            if msg.get("id") == msg_id:
                updated = True
                for key, value in kwargs.items():
                    if key in [
                        "message", "cron_expr", "at_all", "enabled", "name", "run_at",
                        "one_time", "action",
                    ]:
                        msg[key] = value
                if str(msg.get("action", "message")) == "hourly_time":
                    msg["cron_expr"] = "0 * * * *"
                    msg["run_at"] = 0
                    msg["one_time"] = False
        return messages
    
    update_group_data(group_id, "timed_messages", _update)
    return updated

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

def _matches_cron_field(expression: str, value: int) -> bool:
    """Match one validated cron field, including lists, ranges, and steps."""
    for raw_part in expression.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "/" in part:
            base, step_text = part.split("/", 1)
            step = int(step_text)
            if "-" in base:
                lower_text, upper_text = base.split("-", 1)
                lower, upper = int(lower_text), int(upper_text)
                if lower <= value <= upper and (value - lower) % step == 0:
                    return True
                continue
            lower = 0 if base == "*" else int(base)
            if value >= lower and (value - lower) % step == 0:
                return True
            continue
        if part == "*":
            return True
        if "-" in part:
            lower_text, upper_text = part.split("-", 1)
            if int(lower_text) <= value <= int(upper_text):
                return True
            continue
        if int(part) == value:
            return True
    return False


def is_cron_match(cron_expr: str, current_time: time.struct_time = None) -> bool:
    """检查cron表达式是否匹配当前时间"""
    if not current_time:
        current_time = time.localtime()
    
    if "error" in validate_cron_expr(cron_expr):
        return False
    cron = parse_cron_expr(cron_expr)
    if "error" in cron:
        return False
    
    minute = current_time.tm_min
    hour = current_time.tm_hour
    day = current_time.tm_mday
    month = current_time.tm_mon
    weekday = current_time.tm_wday
    
    return (_matches_cron_field(cron["minute"], minute) and
            _matches_cron_field(cron["hour"], hour) and
            _matches_cron_field(cron["day"], day) and
            _matches_cron_field(cron["month"], month) and
            _matches_cron_field(cron["weekday"], weekday))


def _latest_due_cron_slot(
    cron_expr: str,
    now: int,
    last_sent_at: int,
    created_at: int = 0,
    catchup_seconds: int = _CRON_CATCHUP_SECONDS,
) -> int:
    """Return the newest matching minute slot that may still be sent.

    A recurring task is tied to its planned minute, rather than whichever
    polling pass happened to run during that minute.  Selecting only the
    newest matching slot makes recovery safe: a delayed worker sends one
    current report, never a burst of every report missed while offline.
    """
    current_slot = int(now) - (int(now) % 60)
    earliest_slot = max(0, current_slot - max(0, int(catchup_seconds)))
    for scheduled_at in range(current_slot, earliest_slot - 1, -60):
        if scheduled_at <= int(last_sent_at or 0):
            break
        # A task created after a slot must wait for its next scheduled run.
        if created_at and scheduled_at < created_at:
            continue
        if is_cron_match(cron_expr, time.localtime(scheduled_at)):
            return scheduled_at
    return 0


def get_due_messages(group_id: str) -> list:
    """获取当前需要发送的定时消息，并标注计划执行时间。"""
    messages = load_timed_messages(group_id)
    now = int(time.time())
    due = []
    
    for msg in messages:
        if not msg.get("enabled", True):
            continue
        # A send can fail after the QQ client has already accepted the
        # request but before its acknowledgement reaches us.  Retrying that
        # task every scheduler pass turns one uncertain send into a flood.
        # Keep a persisted back-off so hot reloads and web sync cannot lose
        # this protection.
        try:
            next_retry_at = int(msg.get("next_retry_at", 0) or 0)
        except (TypeError, ValueError):
            next_retry_at = 0
        if next_retry_at > now:
            continue
        try:
            one_time_at = int(msg.get("run_at", 0) or 0)
        except (TypeError, ValueError):
            one_time_at = 0
        if one_time_at:
            if int(msg.get("last_sent_at", 0) or 0) or now < one_time_at:
                continue
            scheduled_at = one_time_at
        else:
            cron_expr = (
                "0 * * * *"
                if str(msg.get("action", "message")) == "hourly_time"
                else msg.get("cron_expr", "")
            )
            if not isinstance(cron_expr, str):
                continue
            try:
                last_sent = int(msg.get("last_scheduled_at", msg.get("last_sent_at", 0)) or 0)
            except (TypeError, ValueError):
                last_sent = 0
            try:
                created_at = int(msg.get("created_at", 0) or 0)
            except (TypeError, ValueError):
                created_at = 0
            is_hourly_report = str(msg.get("action", "message")) == "hourly_time"
            scheduled_at = _latest_due_cron_slot(
                cron_expr,
                now,
                last_sent,
                created_at,
                _HOURLY_CATCHUP_SECONDS if is_hourly_report else _CRON_CATCHUP_SECONDS,
            )
            if not scheduled_at:
                continue
        try:
            last_sent = int(msg.get("last_scheduled_at", msg.get("last_sent_at", 0)) or 0)
        except (TypeError, ValueError):
            last_sent = 0
        if scheduled_at <= last_sent:
            continue
        
        action_type = msg.get("action", "message")
        if action_type == "message":
            message_content = msg.get("message", "")
            if not isinstance(message_content, str) or not message_content.strip():
                logger.warning(
                    f"定时任务内容为空，跳过: group={group_id}, name={msg.get('name')}"
                )
                continue
        
        # Do not persist this runtime-only value into the group configuration.
        # It is used by the sender to display the planned hour when a report is
        # delivered a few seconds late.
        due.append({**msg, "_scheduled_at": scheduled_at})
    
    return due


def claim_timed_message(
    group_id: str,
    msg_id: str,
    lease_seconds: int = 120,
    owner_id: str = "",
    priority: int = 0,
    scheduled_at: int = 0,
) -> bool:
    """Atomically claim one due task before sending it.

    The scheduler can briefly overlap during a hot reload or while a web
    configuration is being applied.  ``last_sent_at`` is written only after
    QQ accepts the message, so it is not sufficient to stop those workers
    from sending the same task several times.  A short persisted lease makes
    the task single-flight; a failed send becomes retryable after the lease.
    """
    now = int(time.time())
    try:
        lease = max(30, int(lease_seconds))
    except (TypeError, ValueError):
        lease = 120
    try:
        priority = max(0, min(100, int(priority)))
    except (TypeError, ValueError):
        priority = 0
    try:
        scheduled_at = max(0, int(scheduled_at or 0))
    except (TypeError, ValueError):
        scheduled_at = 0
    file_lock = _acquire_dispatch_file_lock(group_id, msg_id, lease)
    if file_lock is None:
        return False
    claimed = False

    def _claim(messages):
        nonlocal claimed
        if not isinstance(messages, list):
            return messages or []
        for message in messages:
            if str(message.get("id", "")) != str(msg_id):
                continue
            if not message.get("enabled", True):
                return messages
            try:
                last_sent = int(message.get("last_scheduled_at", message.get("last_sent_at", 0)) or 0)
            except (TypeError, ValueError):
                last_sent = 0
            try:
                lock_until = int(message.get("dispatch_lock_until", 0) or 0)
            except (TypeError, ValueError):
                lock_until = 0
            try:
                next_retry_at = int(message.get("next_retry_at", 0) or 0)
            except (TypeError, ValueError):
                next_retry_at = 0
            # For cron tasks, use the planned minute rather than a generic
            # 60-second throttle.  This preserves short catch-up delivery at
            # hh:00 while still blocking a second worker from sending it.
            if scheduled_at and last_sent >= scheduled_at:
                return messages
            if not scheduled_at and now - last_sent < 60:
                return messages
            if next_retry_at > now:
                return messages
            # A live lease is never pre-empted.  Account priority is resolved
            # before claiming (the lower account waits briefly), so taking an
            # existing lease here could make two QQ accounts send the same
            # message when the first request is already in flight.
            if lock_until > now:
                return messages
            message["dispatch_lock_until"] = now + lease
            message["last_attempt_at"] = now
            message["dispatch_owner"] = str(owner_id or "")[:64]
            message["dispatch_priority"] = priority
            message["dispatch_scheduled_at"] = scheduled_at
            claimed = True
            return messages
        return messages

    try:
        update_group_data(group_id, "timed_messages", _claim)
    except Exception:
        if file_lock:
            _release_dispatch_file_lock(group_id, msg_id)
        raise
    if not claimed:
        if file_lock:
            _release_dispatch_file_lock(group_id, msg_id)
    return claimed

def mark_timed_message_retry(
    group_id: str,
    msg_id: str,
    *,
    retry_after_seconds: int = 300,
    reason: str = "",
) -> None:
    """Back off a task that could not be confirmed by QQ.

    We intentionally do not set ``last_sent_at`` here: the task remains due
    on its next normal schedule, but never replays in a tight loop while an
    acknowledgement is ambiguous or a music provider is down.
    """
    try:
        delay = max(60, min(int(retry_after_seconds), 3600))
    except (TypeError, ValueError):
        delay = 300
    now = int(time.time())

    def _mark_retry(messages):
        if messages is None:
            return []
        for msg in messages:
            if str(msg.get("id", "")) != str(msg_id):
                continue
            msg["next_retry_at"] = now + delay
            msg["last_attempt_at"] = now
            msg["failure_count"] = max(0, int(msg.get("failure_count", 0) or 0)) + 1
            if reason:
                msg["last_error"] = str(reason)[:240]
            msg.pop("dispatch_lock_until", None)
            msg.pop("dispatch_owner", None)
            msg.pop("dispatch_priority", None)
            msg.pop("dispatch_scheduled_at", None)
        return messages

    update_group_data(group_id, "timed_messages", _mark_retry)
    _release_dispatch_file_lock(group_id, msg_id)


def mark_sent(
    group_id: str,
    msg_id: str,
    *,
    music_keyword: str = "",
    music_track: str = "",
    scheduled_at: int = 0,
):
    """标记消息已发送"""
    def _mark(messages):
        if messages is None:
            return []
        for msg in messages:
            if msg.get("id") == msg_id:
                msg["last_sent_at"] = int(time.time())
                # Keep the completed slot separate from the acknowledgement
                # time: a slow send may finish after the next hour starts.
                completed_slot = scheduled_at or msg.get("dispatch_scheduled_at", 0)
                if completed_slot:
                    msg["last_scheduled_at"] = int(completed_slot)
                msg["sent_count"] = msg.get("sent_count", 0) + 1
                msg.pop("dispatch_lock_until", None)
                msg.pop("dispatch_owner", None)
                msg.pop("dispatch_priority", None)
                msg.pop("dispatch_scheduled_at", None)
                msg.pop("next_retry_at", None)
                msg.pop("last_error", None)
                msg["failure_count"] = 0
                if music_keyword:
                    msg["last_music_keyword"] = str(music_keyword)[:160]
                if music_track:
                    msg["last_music_track"] = str(music_track)[:320]
                if msg.get("one_time") or msg.get("run_at"):
                    msg["enabled"] = False
        return messages
    
    update_group_data(group_id, "timed_messages", _mark)
    _release_dispatch_file_lock(group_id, msg_id)

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
