import asyncio
import html
import json
import ipaddress
import re
import socket
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
try:  # AstrBot supplies the plugin logger in production.
    from astrbot.api import logger
except ImportError:
    try:
        from ..runtime_compat import logger
    except ImportError:
        from runtime_compat import logger
from .group_config import get_group_data, set_group_data, update_group_data


_WEB_CONTENT_MAX_BYTES = 512 * 1024
_WEB_CONTENT_TIMEOUT_SECONDS = 3.0
_WEB_CONTENT_MAX_CHARS = 12000
_WEB_CONTENT_MAX_URLS = 2


class _VisibleHtmlTextParser(HTMLParser):
    """Extract only bounded human-visible page text for moderation."""

    _ignored = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.description = ""
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = str(tag or "").casefold()
        if tag in self._ignored:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True
        attrs_map = {str(k).casefold(): str(v or "") for k, v in attrs if k}
        if tag == "meta":
            name = attrs_map.get("name", "").casefold()
            prop = attrs_map.get("property", "").casefold()
            if name in {"description", "keywords"} or prop in {"og:title", "og:description"}:
                value = attrs_map.get("content", "").strip()
                if value and not self.description:
                    self.description = value[:1000]

    def handle_endtag(self, tag: str) -> None:
        tag = str(tag or "").casefold()
        if tag == "title":
            self._in_title = False
        if tag in self._ignored and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", str(data or "")).strip()
        if not text or self._ignored_depth:
            return
        if self._in_title and not self.title:
            self.title = text[:500]
        self.parts.append(text)

    def result(self) -> str:
        values = [self.title, self.description, " ".join(self.parts)]
        return re.sub(r"\s+", " ", " ".join(item for item in values if item)).strip()[:_WEB_CONTENT_MAX_CHARS]


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_web_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_public_web_url(raw_url: object) -> str:
    parsed = urllib.parse.urlsplit(str(raw_url or "").strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("unsupported_url")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("unsafe_url")
    host = parsed.hostname.rstrip(".").casefold()
    if host == "localhost" or host.endswith(".local"):
        raise ValueError("private_host")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError):
        raise ValueError("dns_failed")
    if not addresses:
        raise ValueError("dns_failed")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if any((ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_multicast, ip.is_reserved, ip.is_unspecified)):
            raise ValueError("private_address")
    return urllib.parse.urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def _fetch_public_web_text(raw_url: object) -> tuple[str, str]:
    url = _validate_public_web_url(raw_url)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "JokerModeration/1.11", "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.8"},
    )
    with opener.open(request, timeout=_WEB_CONTENT_TIMEOUT_SECONDS) as response:
        final_url = _validate_public_web_url(response.geturl())
        content_type = str(response.headers.get("Content-Type", "")).casefold()
        if content_type and not any(kind in content_type for kind in ("text/", "html", "json", "xml")):
            return final_url, ""
        body = response.read(_WEB_CONTENT_MAX_BYTES + 1)
        if len(body) > _WEB_CONTENT_MAX_BYTES:
            body = body[:_WEB_CONTENT_MAX_BYTES]
        charset = response.headers.get_content_charset() or "utf-8"
        decoded = body.decode(charset, errors="replace")
        if "html" in content_type or "xhtml" in content_type or re.search(r"<\s*(html|title|body)\b", decoded, re.I):
            parser = _VisibleHtmlTextParser()
            parser.feed(decoded)
            return final_url, parser.result()
        return final_url, re.sub(r"\s+", " ", html.unescape(decoded)).strip()[:_WEB_CONTENT_MAX_CHARS]


def inspect_urls_for_violations(urls: object, group_id: str = "", global_config: dict | None = None) -> list[dict]:
    """Inspect public page content and classify violations without blocking chat."""
    if not isinstance(urls, (list, tuple, set)):
        return []
    results: list[dict] = []
    seen: set[str] = set()
    for raw_url in list(urls)[:_WEB_CONTENT_MAX_URLS]:
        normalized = str(raw_url or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            final_url, page_text = _fetch_public_web_text(normalized)
            if not page_text:
                continue
            categories = detect_violation_categories(page_text, final_url, group_id, global_config)
            if categories:
                results.append({"url": final_url[:512], "categories": categories, "source": "网页实际内容"})
        except Exception:
            logger.debug("网页内容检查失败 url=%s", normalized[:160], exc_info=True)
    return results

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

    if "features"not in config:
        config["features"] = {}
    if "recall"not in config["features"]:
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
    value = get_group_data(group_id, "recall_time", 60)
    try:
        # OneBot's history endpoint can return very old messages.  Keep the
        # window bounded so a malformed web value cannot turn bulk recall into
        # an unbounded history scan.
        return max(1, min(int(value), 7 * 86400))
    except (TypeError, ValueError, OverflowError):
        return 60


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"", "0", "false", "off", "no", "disabled"}:
            return False
        if text in {"1", "true", "on", "yes", "enabled"}:
            return True
    if value is None:
        return default
    return bool(value)


def _api_result_succeeded(result: object) -> bool:
    """Accept old adapters while rejecting explicit OneBot failures."""
    if not isinstance(result, dict):
        return True
    if result.get("success") is False:
        return False
    retcode = result.get("retcode")
    if retcode not in (None, 0, "0"):
        return False
    return str(result.get("status", "") or "").strip().lower() not in {
        "failed", "failure", "error"
    }


def _recall_timeout_reported_success(value: object) -> bool:
    """Recognize NapCat's completed recall callback despite a local timeout.

    Some NapCat versions raise ``ActionFailed(retcode=1200)`` even after the
    QQ callback reports that ``recallMsg`` succeeded. Depending on the
    adapter build, that callback may live in ``message`` or ``wording`` rather
    than ``str(error)``. Only an explicit timed-out recall callback with
    ``result: 0`` counts as success; all other delete failures remain errors.
    """
    candidates = [value, repr(value)]
    for field in ("message", "wording", "detail", "error", "args"):
        candidate = getattr(value, field, None)
        if candidate not in (None, ""):
            candidates.append(candidate)
    raw_text = "\n".join(str(item) for item in candidates if item is not None)
    normalized = raw_text.lower()
    if (
        "timeout" not in normalized
        or "recallmsg" not in normalized
        or "eventret" not in normalized
    ):
        return False
    callback_text = raw_text.replace("\\", "")
    return bool(
        re.search(r'["\']?result["\']?\s*[:=]\s*0\b', callback_text)
    )


def _extract_history_messages(payload: object) -> list[dict]:
    """Normalize OneBot history responses from different adapters.

    OneBot implementations do not agree on where ``get_group_msg_history``
    puts the rows: older clients return ``messages`` at the top level, while
    aiocqhttp/NapCat commonly wrap them in ``data.messages`` or return the
    list directly as ``data``.  Treating only the first shape as valid makes
    manual ``/撤回`` appear to do nothing even though QQ returned history.
    """
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            messages = data.get("messages", data.get("message", []))
        elif isinstance(data, list):
            messages = data
        else:
            messages = payload.get("messages", [])
    else:
        data = getattr(payload, "data", None)
        if isinstance(data, dict):
            messages = data.get("messages", data.get("message", []))
        elif isinstance(data, list):
            messages = data
        else:
            messages = getattr(payload, "messages", [])
    if not isinstance(messages, (list, tuple)):
        return []

    normalized: list[dict] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        message = dict(item)
        if not message.get("message_id"):
            for key in ("messageId", "msg_id", "msgId", "id"):
                if message.get(key) not in (None, "", 0, "0"):
                    message["message_id"] = message[key]
                    break
        sender = message.get("sender")
        if not isinstance(sender, dict):
            sender = {}
        else:
            sender = dict(sender)
        if not sender.get("user_id"):
            for key in ("userId", "user_id", "qq", "uin", "id"):
                if sender.get(key) not in (None, "", 0, "0"):
                    sender["user_id"] = sender[key]
                    break
        if sender:
            message["sender"] = sender
        normalized.append(message)
    return normalized


def _history_sender_id(message: dict) -> str:
    """Read a sender QQ ID from standard and NapCat history shapes."""
    sender = message.get("sender", {}) if isinstance(message, dict) else {}
    if isinstance(sender, dict):
        for key in ("user_id", "userId", "qq", "uin", "id"):
            value = sender.get(key)
            if value not in (None, "", 0, "0"):
                return str(value)
    for key in ("user_id", "userId", "qq", "uin"):
        value = message.get(key) if isinstance(message, dict) else None
        if value not in (None, "", 0, "0"):
            return str(value)
    return ""


def _history_message_id(message: dict) -> object:
    """Read the deletable message ID without confusing it with message_seq."""
    if not isinstance(message, dict):
        return None
    for key in ("message_id", "messageId", "msg_id", "msgId", "id"):
        value = message.get(key)
        if value not in (None, "", 0, "0"):
            return value
    return None


def get_recall_member_scope(group_id: str) -> str:
    """Return ``members`` or ``all`` for bulk/targeted manual recall.

    The feature-level setting is authoritative for Web/Windows parity.  The
    old ``enable_admin_recall`` boolean remains a compatibility fallback.
    """
    from .group_config import load_group_config

    config = load_group_config(group_id) or {}
    features = config.get("features", {}) if isinstance(config, dict) else {}
    recall = features.get("recall", {}) if isinstance(features, dict) else {}
    if not isinstance(recall, dict):
        recall = {}
    # ``member_scope`` is the newer explicit setting and must win over the
    # legacy boolean.  Older configs often retain ``enable_admin_recall=True``
    # after a command changed the scope to ordinary members.
    raw = recall.get(
        "member_scope",
        recall.get("recall_member_scope", recall.get("admin_scope", None)),
    )
    if raw is None and "enable_admin_recall" in recall:
        raw = "all" if _as_bool(recall.get("enable_admin_recall"), True) else "members"
    if raw is None and isinstance(config, dict):
        raw = config.get("recall_member_scope", config.get("recall_admin_scope", None))
    if raw is None:
        legacy = recall.get("enable_admin_recall", config.get("enable_admin_recall", True))
        return "all" if _as_bool(legacy, True) else "members"
    text = str(raw).strip().lower()
    if text in {"all", "everyone", "all_members", "全部", "所有人", "admin"}:
        return "all"
    return "members"


def set_recall_member_scope(group_id: str, scope: object) -> str:
    """Persist the administrator-message scope and return its canonical value."""
    normalized = "all" if str(scope or "").strip().lower() in {
        "all", "everyone", "all_members", "全部", "所有人", "admin"
    } else "members"
    from .group_config import load_group_config, save_group_config, init_group_config

    config = load_group_config(group_id)
    if not config:
        config = init_group_config(group_id)
    config.setdefault("features", {})
    config["features"].setdefault("recall", {})
    config["features"]["recall"]["member_scope"] = normalized
    config["features"]["recall"]["enable_admin_recall"] = normalized == "all"
    # Keep a legacy mirror so an older Windows process sees the same choice.
    config["recall_member_scope"] = normalized
    config["enable_admin_recall"] = normalized == "all"
    save_group_config(group_id, config)
    return normalized

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
    """检查消息是否应该被撤回
    
    撤回逻辑取决于模式：
    - keyword: 匹配任何关键词时撤回
    - regex: 匹配任何正则表达式时撤回
    - all: 撤回所有消息
    - none: 不撤回任何消息
    """
    if not is_recall_enabled(group_id):
        return False
    
    mode = get_recall_mode(group_id)
    
    # 关键词撤回模式
    if mode == "keyword":
        keywords = get_recall_keywords(group_id)
        if not keywords:
            # 没有配置关键词时，不撤回
            return False
        for keyword in keywords:
            if keyword in message:
                return True
        return False
    
    # 正则表达式撤回模式
    elif mode == "regex":
        patterns = get_recall_regex_list(group_id)
        if not patterns:
            # 没有配置正则时，不撤回
            return False
        for pattern in patterns:
            try:
                if re.search(pattern, message):
                    return True
            except re.error:
                continue
        return False
    
    # 全部撤回模式
    elif mode == "all":
        return True
    
    # 无撤回模式或未知模式
    else:
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

    if "features"not in config:
        config["features"] = {}
    if "recall"not in config["features"]:
        config["features"]["recall"] = {}
    config["features"]["recall"]["self_recall_enabled"] = enabled

    save_group_config(group_id, config)
    logger.debug(f"enable_self_recall: 已设置群 {group_id} 的自身撤回为 {enabled}")

def is_self_recall_enabled(group_id: str) -> bool:
    """Return whether bot-message recall is effectively enabled for a group.

    The recall feature is the master switch.  ``self_recall_enabled`` remains
    the group's remembered preference, but it cannot schedule or execute a
    bot-message recall while the master switch is off.  Keeping the preference
    intact lets a later ``/开启撤回`` restore the user's chosen self-recall
    setting without silently changing the per-group configuration.
    """
    from .group_config import load_group_config
    config = load_group_config(group_id)

    if not config:
        return False

    features = config.get("features", {})
    recall = features.get("recall", {}) if isinstance(features, dict) else {}
    if not isinstance(recall, dict):
        recall = {}

    master_enabled = recall.get("enabled", None)
    if master_enabled is None:
        master_enabled = config.get("recall_enabled", False)
    if not master_enabled:
        return False

    result = recall.get("self_recall_enabled", None)
    if result is None:
        # Older group snapshots used ``self_recall`` inside the feature map.
        result = recall.get("self_recall", None)
    if result is not None:
        return bool(result)

    return bool(config.get("self_recall_enabled", False))

def set_self_recall_time(group_id: str, seconds: int):
    """设置自身撤回时间"""
    from .group_config import load_group_config, save_group_config, init_group_config

    seconds = max(5, min(seconds, 300))

    config = load_group_config(group_id)
    if not config:
        config = init_group_config(group_id)

    config["self_recall_time"] = seconds

    if "features"not in config:
        config["features"] = {}
    if "recall"not in config["features"]:
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
    from .group_config import load_group_config

    config = load_group_config(group_id) or {}
    features = config.get("features", {}) if isinstance(config, dict) else {}
    recall = features.get("recall", {}) if isinstance(features, dict) else {}
    if not isinstance(recall, dict):
        recall = {}
    configured = recall.get("self_recall_enabled", recall.get("self_recall", None))
    if configured is None:
        configured = config.get("self_recall_enabled", False)
    return {
        "enabled": is_self_recall_enabled(group_id),
        "time": get_self_recall_time(group_id),
        "master_enabled": is_recall_enabled(group_id),
        "configured": bool(configured),
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
            try:
                result = await bot.delete_msg(message_id=int(message_id))
            except asyncio.CancelledError:
                raise
            except Exception as delete_error:
                if _recall_timeout_reported_success(delete_error):
                    result = {"status": "ok", "retcode": 0}
                    logger.debug(
                        "NapCat 已确认机器人消息撤回，忽略本地超时: group=%s message_id=%s",
                        group_id,
                        message_id,
                    )
                else:
                    logger.warning(
                        "自动撤回跳过：机器人消息撤回调用失败 "
                        "group=%s message_id=%s error_type=%s retcode=%s",
                        group_id,
                        message_id,
                        type(delete_error).__name__,
                        getattr(delete_error, "retcode", None),
                    )
                    return
            if not _api_result_succeeded(result):
                logger.warning(
                    "自动撤回跳过：QQ 平台拒绝机器人消息撤回 "
                    "group=%s message_id=%s",
                    group_id,
                    message_id,
                )
                return
            logger.debug(f"已自动撤回消息: {message_id}")
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
        "member_scope": get_recall_member_scope(group_id),
        "recall_member_scope": get_recall_member_scope(group_id),
        "enable_admin_recall": get_recall_member_scope(group_id) == "all",
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
        elif key in {"member_scope", "recall_member_scope", "enable_admin_recall"}:
            set_recall_member_scope(
                group_id,
                ("all" if _as_bool(value, True) else "members")
                if key == "enable_admin_recall"
                else value,
            )
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
    elif key in {"member_scope", "recall_member_scope", "enable_admin_recall"}:
        set_recall_member_scope(
            group_id,
            ("all" if _as_bool(value, True) else "members")
            if key == "enable_admin_recall"
            else value,
        )

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

# ==================== 内容类型检测 ====================

# URL检测正则
_URL_PATTERNS = [
    re.compile(r'https?://\S+', re.IGNORECASE),
    re.compile(r'www\.\S+', re.IGNORECASE),
    re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?(/\S*)?'),
]

# 敏感词列表（黄色相关违规词）
_DEFAULT_SENSITIVE_WORDS = [
    '色情', '淫秽', '淫荡', '黄图', '黄片', '裸聊', '裸照', '裸体',
    '成人视频', '成人片', '不雅视频', '艳照', '裸露', '性感视频',
    '性爱', '约炮', '援交', '性交易', 'porn', 'hentai',
]

# High-confidence categories used by automatic moderation. Keep these lists
# explicit so ordinary conversation is not treated as a violation. Groups can
# extend the sensitive-word list through the existing web/command settings.
_VIOLATION_CATEGORY_WORDS = {
    "黄色": (
        "色情", "淫秽", "淫荡", "黄图", "黄片", "裸聊", "裸照", "裸体",
        "成人视频", "成人片", "不雅视频", "艳照", "性爱", "约炮", "援交", "性交易",
        "porn", "hentai",
    ),
    "暴力": (
        "血腥", "暴力", "虐杀", "杀人", "砍人", "爆头", "屠杀", "肢解",
        "自残", "自杀教程", "炸弹制作", "枪械改装", "gore", "beheading",
    ),
    "政治": (
        "恐怖主义", "恐怖组织", "分裂国家", "颠覆国家", "台独", "藏独", "疆独",
        "暴恐", "法轮功", "terrorist", "terrorism",
    ),
    "诈骗/赌博": (
        "诈骗", "杀猪盘", "洗钱", "赌博", "博彩", "赌场", "下注", "网赌",
        "返利骗局", "刷单返利", "casino", "gambling", "phishing",
    ),
    "外挂/违法引流": (
        "透视", "自瞄", "锁头", "子弹锁", "人物骨骸", "外挂", "辅助",
        "一键注入", "内部注入", "手动注入", "防封", "注入器", "cheat", "trainer",
    ),
}

_CHEAT_PROMOTION_MARKERS = (
    "下载", "加群", "进群", "群号", "内测", "免费", "注入", "上分", "推广", "获取",
)

# 违规域名关键词（URL中包含这些词的网址判定为违规）
_VIOLATION_URL_KEYWORDS = [
    'porn', 'sex', 'xxx', 'adult', 'escort', 'camgirl', 'hentai',
    'casino', 'gambl', 'bet', 'lottery', 'poker', 'slot',
    'drug', 'weed', 'cannabis', 'marijuana',
    'hack', 'crack', 'warez', 'piracy',
    '色情', '赌博', '彩票', '裸聊', '约炮', '一夜情',
]

def set_url_recall_enabled(group_id: str, enabled: bool):
    """启用/禁用网址检测撤回"""
    set_group_data(group_id, "recall_url_enabled", enabled)

def is_url_recall_enabled(group_id: str) -> bool:
    """检查网址检测是否启用"""
    return get_group_data(group_id, "recall_url_enabled", False)

def set_card_recall_enabled(group_id: str, enabled: bool):
    """启用/禁用名片检测撤回"""
    set_group_data(group_id, "recall_card_enabled", enabled)

def is_card_recall_enabled(group_id: str) -> bool:
    """检查名片检测是否启用"""
    return get_group_data(group_id, "recall_card_enabled", False)

def set_image_recall_enabled(group_id: str, enabled: bool):
    """启用/禁用图片检测撤回"""
    set_group_data(group_id, "recall_image_enabled", enabled)

def is_image_recall_enabled(group_id: str) -> bool:
    """检查图片检测是否启用"""
    return get_group_data(group_id, "recall_image_enabled", False)

def set_sensitive_recall_enabled(group_id: str, enabled: bool):
    """启用/禁用敏感词检测撤回"""
    set_group_data(group_id, "recall_sensitive_enabled", enabled)

def is_sensitive_recall_enabled(group_id: str) -> bool:
    """检查敏感词检测是否启用"""
    return get_group_data(group_id, "recall_sensitive_enabled", False)

def add_sensitive_word(group_id: str, word: str):
    """添加敏感词"""
    def _add(words):
        if words is None:
            words = list(_DEFAULT_SENSITIVE_WORDS)
        if word not in words:
            words.append(word)
        return words
    update_group_data(group_id, "recall_sensitive_words", _add)

def remove_sensitive_word(group_id: str, word: str):
    """移除敏感词"""
    def _remove(words):
        if words is None:
            return list(_DEFAULT_SENSITIVE_WORDS)
        if word in words:
            words.remove(word)
        return words
    update_group_data(group_id, "recall_sensitive_words", _remove)

def get_sensitive_words(group_id: str, global_config=None) -> list:
    """获取敏感词列表，优先使用分群配置，其次使用全局配置，最后使用默认值"""
    words = get_group_data(group_id, "recall_sensitive_words", None)
    if words is not None:
        words = list(words) if isinstance(words, (list, tuple, set)) else [words]
    else:
        words = []
    if global_config:
        try:
            if isinstance(global_config, dict):
                sensitive_words = (
                    global_config.get("qqadmin", {})
                    .get("recall", {})
                    .get("sensitive_words")
                )
            else:
                sensitive_words = global_config.get_nested("qqadmin", "recall", "sensitive_words", default=None)
            if sensitive_words:
                words.extend(sensitive_words if isinstance(sensitive_words, (list, tuple, set)) else [sensitive_words])
        except Exception:
            pass
    # Keyword filtering and content moderation share one effective vocabulary
    # so a word added in either page is not silently ignored by URL/card checks.
    if group_id:
        try:
            from .group_config import get_group_feature_detail

            detail = get_group_feature_detail(group_id, "keyword_filter", global_config)
            if isinstance(detail, dict):
                words.extend(detail.get("filter_keywords", detail.get("keywords", [])) or [])
            words.extend(get_group_data(group_id, "custom_filter_keywords", []) or [])
        except Exception:
            logger.debug("读取关键词过滤词合并到内容检测失败 group=%s", group_id, exc_info=True)
    if not words:
        words = list(_DEFAULT_SENSITIVE_WORDS)
    result: list[str] = []
    seen: set[str] = set()
    for word in words:
        normalized = str(word or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result or list(_DEFAULT_SENSITIVE_WORDS)

def detect_url(message: str) -> list:
    """检测消息中的URL"""
    urls = []
    for pattern in _URL_PATTERNS:
        found = pattern.findall(message)
        urls.extend(found)
    return list(set(urls))

def detect_cq_content(raw_message: str, cq_type: str) -> list:
    """检测CQ码内容（如名片、图片等）"""
    pattern = re.compile(rf'\[CQ:{cq_type}[^\]]*\]')
    return pattern.findall(raw_message)

def detect_sensitive_words(message: str, group_id: str = None, global_config=None) -> list:
    """检测消息中的敏感词"""
    if not message:
        return []
    words = get_sensitive_words(group_id, global_config) if group_id else _DEFAULT_SENSITIVE_WORDS
    found = []
    for word in words:
        if word in message:
            found.append(word)
    return found

def detect_violation_url(message: str, group_id: str = None, global_config=None) -> list:
    """检测消息中的URL是否包含违规关键词（内置+用户自定义敏感词）"""
    urls = detect_url(message)
    if not urls:
        return []
    # 合并违规域名关键词 + 敏感词列表
    all_keywords = list(_VIOLATION_URL_KEYWORDS)
    if group_id:
        all_keywords.extend(get_sensitive_words(group_id, global_config))
    violation_urls = []
    for url in urls:
        url_str = str(url).lower()
        for kw in all_keywords:
            if kw and kw.lower() in url_str:
                violation_urls.append(url)
                break
    return violation_urls


def _decode_embedded_text(value: object) -> str:
    """Decode CQ/JSON/URL escaped card text before classification."""
    text = str(value or "")
    if not text:
        return ""
    text = html.unescape(text)
    text = urllib.parse.unquote(text)
    try:
        decoded = json.loads(text)
        if isinstance(decoded, (dict, list)):
            text += " " + json.dumps(decoded, ensure_ascii=False)
        elif isinstance(decoded, str):
            text += " " + decoded
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    text = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        text,
    )
    return text.replace("\u200b", "").replace("\ufeff", "")


def _content_detection_text(message: str, raw_message: str = "") -> str:
    """Combine visible text and embedded card/forward payloads safely."""
    parts = [_decode_embedded_text(message), _decode_embedded_text(raw_message)]
    for payload in re.findall(r"\[CQ:[^,\]]+,[^\]]*\]", raw_message or "", re.I):
        parts.append(_decode_embedded_text(payload))
    return "\n".join(part for part in parts if part).lower()


def detect_violation_categories(
    message: str, raw_message: str = "", group_id: str = None, global_config=None
) -> list[dict]:
    """Return classified violations in deterministic priority order."""
    text = _content_detection_text(message, raw_message)
    if not text:
        return []
    results: list[dict] = []
    custom_words = get_sensitive_words(group_id, global_config) if group_id else []
    if isinstance(custom_words, str):
        custom_words = [custom_words]
    normalized_custom = [str(word).strip().lower() for word in custom_words if str(word).strip()]
    violation_urls = detect_violation_url(text, group_id, global_config)
    if violation_urls:
        results.append({"category": "违规网址", "matches": [str(violation_urls[0])[:80]]})
    for category, words in _VIOLATION_CATEGORY_WORDS.items():
        matches = []
        for word in tuple(words) + tuple(normalized_custom):
            if word and word in text and word not in matches:
                matches.append(word)
        if category == "外挂/违法引流":
            # A single mention can be discussion. Require a promotion signal
            # too, or at least two distinct cheat terms, to reduce false hits.
            promotional = any(marker in text for marker in _CHEAT_PROMOTION_MARKERS)
            if len(matches) < 2 and not (matches and promotional):
                matches = []
        if matches:
            results.append({"category": category, "matches": matches[:5]})

    raw_lower = (raw_message or "").lower()
    if re.search(r"\[cq:(?:json|xml|forward|share|contact|card)", raw_lower) and results:
        results[0]["source"] = "消息卡片/转发内容"
    return results

def should_recall_by_content_type(group_id: str, message: str, raw_message: str = "", global_config=None) -> tuple:
    """
    检查消息内容是否包含违规信息需要撤回（撤回系统开启时自动检测）

    检测项（只检测违规内容，正常内容不撤回）：
    1. 敏感词（黄色等违法词汇）
    2. 违规网址（域名含赌博/色情等违规关键词，正常网址不撤回）
    3. 违规名片（名片内容含敏感词）
    4. 违规图片消息（图片附带文本含敏感词）

    Returns:
        tuple: (是否需要撤回, 原因描述)
    """
    categories = detect_violation_categories(message, raw_message, group_id, global_config)
    if not categories:
        return False, ""
    first = categories[0]
    category = str(first.get("category", "违规内容"))
    matches = ", ".join(str(item) for item in first.get("matches", [])[:3])
    source = str(first.get("source", "文字内容"))
    reason = f"{category}违规（{source}"
    if matches:
        reason += f"，命中：{matches}"
    return True, reason + "）"

def get_content_recall_status(group_id: str) -> dict:
    """获取内容检测状态"""
    return {
        "sensitive": is_sensitive_recall_enabled(group_id),
        "url": is_url_recall_enabled(group_id),
        "card": is_card_recall_enabled(group_id),
        "image": is_image_recall_enabled(group_id),
    }

def get_recall_help_text() -> str:
    """获取撤回系统帮助文本"""
    return """撤回系统使用说明

【命令格式】
/撤回 @用户 [数量] - 撤回指定用户的消息
/撤回 [数量] - 撤回最近的群消息
/设置撤回范围 普通成员|所有人 - 控制手动批量撤回是否包含管理员
/查看撤回关键词 - 查看关键词模式的当前关键词
/查看撤回正则 - 查看正则模式的当前规则
/添加撤回正则 (?i)广告|推广 - 添加一条 Python 正则，不要写前后斜杠

【参数说明】
- @用户：可选，指定要撤回消息的用户
- 数量：可选，撤回的消息数量（未指定时1条，最大值由“设置撤回数量”控制，最多200条）

【使用示例】
/撤回 @张三 5    - 撤回用户张三最近5条消息
/撤回 20        - 撤回群内最近20条消息
/撤回 机器人 100 - 撤回当前机器人最近100条已记录消息
/添加撤回关键词 广告 - 命中“广告”时自动撤回
/添加撤回正则 (?i)广告|推广 - 不区分大小写匹配广告或推广

【注意事项】
- 手动批量撤回不受“撤回时间”设置限制，会尽量处理历史记录；平台拒绝或找不到的消息会自动跳过
- “普通成员”范围会跳过群主和管理员；“所有人”才处理管理员消息
- 机器人自身消息可用“/撤回 机器人 条数”手动清理；不受普通成员范围限制"""

# ==================== 核心撤回功能 ====================

def is_message_within_time_limit(msg: dict, max_seconds: int | None = 120) -> bool:
    """检查消息是否在可撤回时限内；max_seconds=None表示不限制消息年龄。"""
    if not isinstance(msg, dict):
        return False
    if max_seconds is None:
        return True
    msg_time = msg.get("time", 0)
    if msg_time in (None, "", 0, "0"):
        # Some OneBot implementations omit timestamps.  We cannot prove that
        # such a message is old, so retain the historical permissive behavior.
        return True
    try:
        timestamp = int(float(msg_time))
    except (TypeError, ValueError, OverflowError):
        return False
    age = int(time.time()) - timestamp
    if age < 0:
        # Clock skew should not make a fresh message fail the limit.
        age = 0
    try:
        limit = max(1, min(int(max_seconds), 7 * 86400))
    except (TypeError, ValueError, OverflowError):
        limit = 120
    return age <= limit


async def recall_messages(
    event,
    target_user_ids: set = None,
    count: int = 10,
    exclude_admins: bool = True,
    *,
    max_age_seconds: int | None = None,
):
    """
    撤回消息的核心实现

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

    try:
        count = max(1, min(int(count), get_max_recall_count(group_id_str)))
    except (TypeError, ValueError, OverflowError):
        count = 10
    if max_age_seconds is not None:
        try:
            max_age_seconds = max(1, min(int(max_age_seconds), 7 * 86400))
        except (TypeError, ValueError, OverflowError):
            max_age_seconds = 60

    # 统计
    success = 0
    failed = 0
    skipped_admin = 0
    skipped_old = 0
    # 获取管理员列表（如果需要排除）
    admin_ids = set()
    if exclude_admins:
        try:
            member_response = await client.call_action(
                action="get_group_member_list",
                group_id=int(group_id)
            )
            member_list = (
                member_response.get("members", [])
                if isinstance(member_response, dict)
                else member_response
            )
            if not isinstance(member_list, (list, tuple)):
                member_list = []
            for member in member_list:
                if not isinstance(member, dict):
                    continue
                role = member.get("role", "")
                if role in ["admin", "owner"]:
                    admin_ids.add(str(member.get("user_id", "")))
        except Exception as e:
            logger.warning(f"获取管理员列表失败: {e}")

    messages = []
    skipped_no_message_id = 0

    def build_result(error: str = "") -> dict:
        attempted = success + failed
        result = {
            "success": success,
            "failed": failed,
            # ``total`` remains the number of delete attempts for backward
            # compatibility.  The additional fields explain every requested
            # slot that was not attempted instead of silently losing it.
            "total": attempted,
            "attempted": attempted,
            "requested": count,
            "fetched": len(messages),
            "unprocessed": max(0, count - attempted),
            "skipped_admin": skipped_admin,
            "skipped_old": skipped_old,
            "skipped_no_message_id": skipped_no_message_id,
        }
        if error:
            result["error"] = error
        return result
    
    # 如果有目标用户，从API获取消息并筛选
    if target_user_ids:
        target_user_ids = {str(value) for value in target_user_ids if str(value or "").strip()}
        try:
            fetch_count = min(max(count * 3, count + 10), 100)
            payloads = {
                "group_id": int(group_id),
                "count": fetch_count,
            }
            result = await client.call_action("get_group_msg_history", **payloads)
            api_messages = _extract_history_messages(result)
            logger.debug(f"从API获取到 {len(api_messages)} 条群消息")

            api_messages.sort(key=lambda item: item.get("time", 0), reverse=True)

            for msg in api_messages:
                if not isinstance(msg, dict):
                    continue
                sender_id = _history_sender_id(msg)
                if sender_id in target_user_ids:
                    messages.append(msg)

        except Exception as e:
            logger.error(f"获取群消息历史失败: {e}")

        if not messages:
            return build_result("用户最近没有发言记录")

    # 如果没有目标用户（撤回所有人的消息），使用API获取
    else:
        payloads = {
            "group_id": int(group_id),
            # Fetch enough history to replace admin/expired/invalid entries,
            # while keeping the platform request bounded.
            "count": min(max(count * 3, count + 10), 100),
        }

        try:
            result = await client.call_action("get_group_msg_history", **payloads)
            api_messages = _extract_history_messages(result)
            api_messages.sort(key=lambda item: item.get("time", 0), reverse=True)
            messages = [item for item in api_messages if isinstance(item, dict)][:count * 3]
            logger.debug(f"获取到 {len(messages)} 条群消息")
        except Exception as e:
            logger.error(f"获取群消息历史失败: {e}")

    if not messages:
        return build_result("没有获取到消息")

    # 过滤处理：排除管理员（如需要）
    filtered_messages = []
    for msg in messages:
        message_id = _history_message_id(msg)
        if not message_id:
            skipped_no_message_id += 1
            continue

        # 获取发送者ID
        sender_id = _history_sender_id(msg)

        # 排除管理员（仅当调用方要求时）
        if exclude_admins and sender_id in admin_ids:
            skipped_admin += 1
            continue

        # Manual bulk recall passes None by default so configured automatic
        # recall timing cannot hide older violation messages.
        if not is_message_within_time_limit(msg, max_age_seconds):
            skipped_old += 1
            continue
        filtered_messages.append(msg)
        if len(filtered_messages) >= count:
            break

    if not filtered_messages:
        return build_result("没有可撤回的消息")

    # 执行撤回，添加请求间隔
    cache_group_id = ""
    cache_settings = None
    cache_operator_id = str(event.get_sender_id()) if hasattr(event, "get_sender_id") else ""
    try:
        from .group_config import load_group_config
        from .send_guard import can_send_group_message
        cache_group_id = str(getattr(event.message_obj, "group_id", "") or "")
        if cache_group_id and can_send_group_message(cache_group_id):
            group_config = load_group_config(cache_group_id)
            feature_settings = group_config.get("features", {}).get("message_cache", {}) if group_config else {}
            cache_settings = feature_settings if isinstance(feature_settings, dict) else {}
    except Exception as cache_exc:
        logger.warning(f"读取消息缓存设置失败: {cache_exc}")

    try:
        from .console_log import print_group_action
    except Exception:
        print_group_action = None

    def log_attempt(message_id: object, detail: str) -> None:
        if print_group_action is None:
            return
        try:
            print_group_action(
                "撤回",
                group_id,
                message_id=message_id,
                detail=detail,
            )
        except Exception:
            pass

    for i, msg in enumerate(filtered_messages):
        message_id = _history_message_id(msg)
        if not message_id:
            continue

        try:
            delete_result = await client.delete_msg(message_id=message_id)
            if not _api_result_succeeded(delete_result):
                raise RuntimeError("平台拒绝撤回")
            success += 1
            logger.debug(f"成功撤回消息: {message_id}")
            log_attempt(message_id, "成功")
            sender_id = _history_sender_id(msg)
            content = str(msg.get("raw_message", msg.get("message", "")) or "")[:500]
            log_recall(
                group_id,
                sender_id,
                str(message_id),
                content,
                operator_id=cache_operator_id or "system",
                reason="管理员手动撤回",
            )
            try:
                if cache_group_id and cache_settings is not None:
                    cached_user_id = _history_sender_id(msg)
                    if cached_user_id:
                        from .message_cache import cache_onebot_message, mark_message_recalled
                        await asyncio.to_thread(
                            cache_onebot_message,
                            cache_group_id,
                            cached_user_id,
                            msg,
                            cache_settings,
                        )
                        await asyncio.to_thread(
                            mark_message_recalled,
                            cache_group_id,
                            message_id,
                            "管理员手动撤回",
                            cache_operator_id,
                        )
            except Exception as cache_exc:
                logger.warning(f"标记撤回消息缓存失败: {cache_exc}")
        except Exception as e:
            failed += 1
            err_str = str(e)
            if "1003"in err_str or "超时"in err_str:
                reason = "超过撤回时限"
                logger.warning(f"撤回消息 {message_id} 失败: {reason}")
            else:
                reason = err_str[:100] or "平台拒绝撤回"
                logger.warning(f"撤回消息 {message_id} 失败: {reason}")
            log_attempt(message_id, f"失败: {reason}")

        # 添加请求间隔，避免API限流
        if i < len(filtered_messages) - 1:
            await asyncio.sleep(0.3)

    result_dict = build_result()
    logger.debug(f"撤回完成: {result_dict}")
    return result_dict


async def recall_user_messages_by_api(
    event,
    user_id: str,
    count: int = 10,
    *,
    exclude_admins: bool = True,
    max_age_seconds: int | None = None,
):
    """
    撤回指定用户的消息（使用API获取消息）
    """
    target_ids = {str(user_id)}
    return await recall_messages(
        event,
        target_ids,
        count,
        exclude_admins=exclude_admins,
        max_age_seconds=max_age_seconds,
    )


async def recall_recent_messages_by_api(
    event,
    count: int = 10,
    exclude_admins: bool = True,
    *,
    max_age_seconds: int | None = None,
):
    """
    撤回最近的群消息（使用API获取消息）
    """
    return await recall_messages(
        event,
        target_user_ids=None,
        count=count,
        exclude_admins=exclude_admins,
        max_age_seconds=max_age_seconds,
    )


# 向后兼容：为旧函数名提供别名
recall_user_messages = recall_user_messages_by_api
recall_recent_messages = recall_recent_messages_by_api


def get_max_recall_count(group_id: str = None) -> int:
    """获取最大撤回数量限制"""
    value = get_group_data(group_id, "recall_max_count", 200) if group_id else 200
    try:
        return max(1, min(int(value), 200))
    except (TypeError, ValueError, OverflowError):
        return 200


def set_max_recall_count(group_id: str, count: object) -> int:
    """Persist the per-group bulk recall limit used by CMD and Web settings."""
    try:
        normalized = max(1, min(int(count), 200))
    except (TypeError, ValueError, OverflowError):
        normalized = 10
    set_group_data(group_id, "recall_max_count", normalized)
    return normalized


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
    "recall_user_messages_by_api", "recall_recent_messages_by_api",
    "recall_user_messages", "recall_recent_messages",
    "recall_messages",
    "is_message_within_time_limit",
    "get_max_recall_count", "set_max_recall_count",
    "get_recall_member_scope", "set_recall_member_scope",
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
    "set_sensitive_recall_enabled", "is_sensitive_recall_enabled",
    "add_sensitive_word", "remove_sensitive_word", "get_sensitive_words",
    "detect_sensitive_words",
    "detect_violation_categories", "inspect_urls_for_violations",
    "should_recall_by_content_type", "get_content_recall_status",
]
