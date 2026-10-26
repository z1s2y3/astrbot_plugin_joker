import re
import time
from datetime import date, datetime, timedelta
from collections.abc import Mapping

from .group_config import get_group_data, set_group_data, update_group_data


SIGNIN_HISTORY_LIMIT = 366
MAX_SIGNIN_RANKING_LIMIT = 100
MAX_SIGNIN_REWARD = 1_000_000
SIGNIN_PROFILE_RECORDS_KEY = "profile_records"
_SIGNIN_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")

# Keep the original reward tiers as the fallback for groups that do not opt in
# to a custom reward policy.
DEFAULT_SIGNIN_REWARD_CONFIG = {
    "base_reward": 10,
    "reward_3_days": 30,
    "reward_7_days": 100,
    "reward_14_days": 200,
    "reward_30_days": 500,
}


def _as_int(value, default: int = 0) -> int:
    """Convert legacy numeric values without letting malformed data break stats."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_non_negative_int(value, default: int = 0) -> int:
    return max(0, _as_int(value, default))


def _parse_calendar_date(value) -> date | None:
    """Parse the persisted ISO calendar date used by the sign-in store."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _current_local_date() -> date:
    """Return today's local calendar date without using a fixed seconds offset."""
    return datetime.now().date()


def _same_signin_period(last_value: object, current: date, mode: object) -> bool:
    """Return whether a prior sign-in belongs to the active period."""
    last = _parse_calendar_date(last_value)
    if last is None:
        return False
    normalized_mode = str(mode or "daily").strip().lower()
    if normalized_mode == "weekly":
        return last.isocalendar()[:2] == current.isocalendar()[:2]
    if normalized_mode == "monthly":
        return (last.year, last.month) == (current.year, current.month)
    return last == current


def _normalize_signin_history(values, last_signin: date | None = None) -> list[str]:
    """Return distinct, chronological ISO dates capped at the history limit."""
    candidates = values if isinstance(values, (list, tuple, set)) else []
    parsed_dates = {
        parsed
        for value in candidates
        if (parsed := _parse_calendar_date(value)) is not None
    }
    if last_signin is not None:
        parsed_dates.add(last_signin)

    recent_dates = sorted(parsed_dates)[-SIGNIN_HISTORY_LIMIT:]
    return [value.isoformat() for value in recent_dates]


def _append_signin_history(record: dict, signin_date: date) -> list[str]:
    """Add one date to a record's bounded sign-in history."""
    return _normalize_signin_history(record.get("signin_history"), signin_date)


def normalize_daily_signin_record(record) -> dict:
    """Return a storage-compatible, safe sign-in record.

    The persisted representation remains a mapping with ISO ``YYYY-MM-DD``
    strings. Unknown keys are retained so old or extended records are not lost.
    """
    normalized = dict(record) if isinstance(record, dict) else {}
    last_signin = _parse_calendar_date(normalized.get("last_signin"))
    normalized["last_signin"] = last_signin.isoformat() if last_signin else ""
    normalized["consecutive_days"] = _as_non_negative_int(
        normalized.get("consecutive_days", 0)
    )
    normalized["total_days"] = _as_non_negative_int(normalized.get("total_days", 0))
    normalized["signin_history"] = _normalize_signin_history(
        normalized.get("signin_history"), last_signin
    )
    normalized.setdefault("user_name", "")
    return normalized


def get_daily_signin_status(record, today: date | None = None) -> dict:
    """Calculate a normalized daily sign-in status without reading or writing state."""
    normalized = normalize_daily_signin_record(record)
    current_date = _parse_calendar_date(today) or _current_local_date()
    last_signin = _parse_calendar_date(normalized["last_signin"])
    result = {
        "signed_today": last_signin == current_date,
        "last_signin": normalized["last_signin"],
        "consecutive_days": normalized["consecutive_days"],
        "total_days": normalized["total_days"],
        "user_name": normalized["user_name"],
    }
    return result


def calculate_next_consecutive_days(record, today: date | None = None) -> int:
    """Return the streak earned by signing in on ``today``.

    Calendar dates avoid the old fixed-24-hour subtraction, which is wrong on
    local daylight-saving transitions and less clear at calendar boundaries.
    """
    current_date = _parse_calendar_date(today) or _current_local_date()
    status = get_daily_signin_status(record, current_date)
    if status["signed_today"]:
        return status["consecutive_days"]

    last_signin = _parse_calendar_date(status["last_signin"])
    if last_signin == current_date - timedelta(days=1):
        return max(1, status["consecutive_days"] + 1)
    return 1


def _normalize_stats_payload(stats) -> dict:
    """Keep stats consumers safe while preserving the existing JSON layout."""
    normalized = dict(stats) if isinstance(stats, dict) else {}
    users = normalized.get("users")
    normalized["users"] = dict(users) if isinstance(users, dict) else {}
    return normalized


def _normalize_user_stats(record) -> dict:
    return dict(record) if isinstance(record, dict) else {}

def load_stats(group_id: str) -> dict:
    """加载群组统计"""
    return _normalize_stats_payload(get_group_data(group_id, "stats", {"users": {}}))

def save_stats(group_id: str, stats: dict):
    """保存群组统计"""
    set_group_data(group_id, "stats", stats)

def load_signin_data(group_id: str) -> dict:
    """加载群组签到数据"""
    signin_data = get_group_data(group_id, "signin", {})
    return dict(signin_data) if isinstance(signin_data, dict) else {}

def save_signin_data(group_id: str, data: dict):
    """保存群组签到数据"""
    set_group_data(group_id, "signin", data)

def normalize_signin_reward_config(reward_config: dict | None = None) -> dict:
    """Normalize an optional per-group sign-in reward policy.

    Invalid values deliberately fall back to their established defaults so a
    malformed setting can never deduct points or make sign-in fail. A value of
    zero is valid and can be used to disable a specific reward tier.
    """
    normalized = dict(DEFAULT_SIGNIN_REWARD_CONFIG)
    if not isinstance(reward_config, dict):
        return normalized

    for key, default in DEFAULT_SIGNIN_REWARD_CONFIG.items():
        value = reward_config.get(key, default)
        if isinstance(value, bool):
            continue
        try:
            reward = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 <= reward <= MAX_SIGNIN_REWARD:
            normalized[key] = reward
    return normalized


def get_signin_reward(days: int, reward_config: dict | None = None) -> int:
    """获取签到奖励"""
    rewards = normalize_signin_reward_config(reward_config)
    consecutive_days = _as_non_negative_int(days)
    tiers = (
        (30, "reward_30_days"),
        (14, "reward_14_days"),
        (7, "reward_7_days"),
        (3, "reward_3_days"),
    )
    for minimum_days, key in tiers:
        if consecutive_days >= minimum_days:
            return rewards[key]
    return rewards["base_reward"]


def preview_signin_reward(
    group_id: str,
    user_id: str,
    *,
    reward_config: dict | None = None,
    signin_policy: dict | None = None,
) -> dict:
    """Calculate the next claim without changing local or game state.

    WebSocket-backed rewards need the final amount before the game command is
    sent.  Keeping this preview next to ``signin`` ensures it uses the same
    profile record and daily/weekly/monthly claim window.
    """
    policy = signin_policy if isinstance(signin_policy, dict) else {}
    current_date = _current_local_date()
    profile_id = str(policy.get("profile_id", "") or "").strip()
    if not _SIGNIN_PROFILE_ID_RE.fullmatch(profile_id):
        profile_id = ""
    aggregate = normalize_daily_signin_record(load_signin_data(group_id).get(user_id, {}))
    profile_records = aggregate.get(SIGNIN_PROFILE_RECORDS_KEY)
    profile_records = profile_records if isinstance(profile_records, dict) else {}
    record = normalize_daily_signin_record(
        profile_records.get(profile_id, {}) if profile_id else aggregate
    )
    schedule_mode = str(policy.get("schedule_mode", "daily") or "daily").strip().lower()
    if schedule_mode not in {"daily", "weekly", "monthly", "streak", "custom"}:
        schedule_mode = "daily"
    if _same_signin_period(record.get("last_signin"), current_date, schedule_mode):
        period_label = {"weekly": "本周", "monthly": "本月"}.get(schedule_mode, "今天")
        return {
            "success": False,
            "code": "already_signed",
            "message": f"您{period_label}已经签到过了",
            "profile_id": profile_id,
            "sign_date": current_date.isoformat(),
        }
    consecutive_days = calculate_next_consecutive_days(record, current_date)
    return {
        "success": True,
        "code": "preview",
        "reward": get_signin_reward(consecutive_days, reward_config),
        "consecutive_days": consecutive_days,
        "sign_date": current_date.isoformat(),
        "profile_id": profile_id,
    }


def _call_database_reward_adapter(
    adapter: object,
    steam_id: str,
    reward: int,
    *,
    player_name: str,
    sign_date: str,
    period_mode: str = "daily",
) -> dict:
    """Call the injected SCUM database writer using a small stable contract.

    The stats module deliberately does not import SQLite or know how a group
    database is located.  ``adapter`` may be a callable or an object exposing
    ``apply_signin_reward``.  Supporting both forms keeps the business layer
    easy to test and lets the Windows runtime inject the path-scoped adapter.
    """
    target = getattr(adapter, "apply_signin_reward", None)
    if not callable(target):
        target = adapter if callable(adapter) else None
    if target is None:
        return {
            "success": False,
            "code": "steam_database_unavailable",
            "message": "服务器数据库暂不可用，暂时无法发放签到奖励，请稍后重试。",
        }

    # Build the call shape before invoking the target.  It is important that
    # an exception raised *by* the target is never treated as a signature
    # mismatch: an adapter may have committed its SQLite transaction and then
    # raised while constructing its response.  Retrying in that case could
    # credit the same reward twice.
    call_kwargs: dict[str, object] = {
        "player_name": player_name,
        "sign_date": sign_date,
        "period_mode": period_mode,
    }
    try:
        import inspect

        signature = inspect.signature(target)
    except (TypeError, ValueError):
        # Some C-extension/mocked callables do not expose a signature.  Their
        # compatibility contract is the simple positional form; if that call
        # fails, surface the failure without attempting a second write.
        call_kwargs = {}
    else:
        parameters = signature.parameters
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if not accepts_kwargs:
            call_kwargs = {
                name: value
                for name, value in call_kwargs.items()
                if (
                    name in parameters
                    and parameters[name].kind
                    in {
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    }
                )
            }

    try:
        response = target(steam_id, reward, **call_kwargs)
    except Exception as exc:
        return {
            "success": False,
            "code": "steam_database_error",
            "message": "服务器数据库操作失败，请稍后重试。",
            "detail": type(exc).__name__,
        }

    if isinstance(response, Mapping):
        result = dict(response)
    else:
        result = {
            "success": False,
            "code": "invalid_database_response",
            "message": "数据库奖励接口未返回可确认的执行结果，未发放奖励。",
        }
    # The caller is allowed to announce a reward only after the adapter has
    # explicitly confirmed success.  Do not accept truthy strings such as
    # ``"false"`` from a loosely typed FTP/database bridge.
    if result.get("success") is not True:
        result["success"] = False
        result.setdefault("code", "steam_database_error")
        result.setdefault("message", "服务器数据库操作失败，请稍后重试。")
    else:
        result["success"] = True
    return result


def _call_database_reset_adapter(
    adapter: object,
    steam_id: str,
    sign_date: str,
    *,
    currency_column: str = "amount",
    reward: object = None,
    remove_reward: bool = True,
) -> dict:
    """Invoke the SCUM reset writer without ever retrying a mutation."""
    target = getattr(adapter, "reset_signin_record", None)
    if not callable(target):
        target = adapter if callable(adapter) else None
    if target is None:
        return {
            "success": False,
            "code": "steam_database_unavailable",
            "message": "服务器数据库暂不可用，暂时无法重置签到记录。",
        }
    kwargs: dict[str, object] = {
        "currency_column": currency_column,
        "remove_reward": bool(remove_reward),
    }
    if reward is not None:
        kwargs["reward"] = reward
    try:
        import inspect

        signature = inspect.signature(target)
        if not any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        ):
            kwargs = {
                key: value
                for key, value in kwargs.items()
                if key in signature.parameters
                and signature.parameters[key].kind
                in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
            }
    except (TypeError, ValueError):
        kwargs = {}
    try:
        response = target(steam_id, sign_date, **kwargs)
    except Exception as exc:
        return {
            "success": False,
            "code": "steam_database_error",
            "message": "服务器数据库操作失败，未执行签到重置。",
            "detail": type(exc).__name__,
        }
    if isinstance(response, Mapping):
        result = dict(response)
    else:
        result = {
            "success": False,
            "code": "invalid_database_response",
            "message": "数据库重置接口未返回可确认的执行结果，未修改签到记录。",
        }
    if result.get("success") is not True:
        result["success"] = False
        result.setdefault("code", "steam_database_error")
        result.setdefault("message", "服务器数据库操作失败，未执行签到重置。")
    return result


def reset_signin(
    group_id: str,
    user_id: str,
    sign_date: object,
    *,
    signin_policy: dict | None = None,
    database_adapter: object | None = None,
    reward: object = None,
    remove_reward: bool = True,
    currency_column: str = "amount",
    backend: str | None = None,
) -> dict:
    """Reset one sign-in claim and reconcile the local mirror.

    In Steam/database mode the adapter is called first.  Local JSON is changed
    only after that transaction succeeds, so a failed or unavailable game DB
    cannot silently create a second claim window.
    """
    date_value = str(sign_date or "").strip()[:32]
    try:
        parsed_date = date.fromisoformat(date_value[:10])
    except (TypeError, ValueError):
        return {"success": False, "code": "invalid_sign_date", "message": "签到日期无效。"}
    date_value = parsed_date.isoformat()
    policy = signin_policy if isinstance(signin_policy, dict) else {}
    requested_backend = str(backend or "").strip().lower()
    if requested_backend not in {"local", "database", "websocket"}:
        raw_sync = policy.get("database_sync", False)
        if isinstance(raw_sync, str):
            raw_sync = raw_sync.strip().lower() not in {"", "0", "false", "off", "no"}
        signin_mode = str(policy.get("signin_mode", "basic") or "basic").strip().lower()
        reward_mode = str(
            policy.get("steam_reward_mode", policy.get("reward_mode", "database"))
            or "database"
        ).strip().lower()
        requested_backend = (
            "local"
            if signin_mode == "steam" and reward_mode == "websocket"
            else "database"
            if bool(raw_sync) or (signin_mode == "steam" and reward_mode == "database")
            else "local"
        )
    database_sync = requested_backend == "database"
    steam_binding = None
    database_result: dict = {}
    if database_sync:
        from .steam_signin import get_steam_binding

        steam_binding = get_steam_binding(group_id, user_id=user_id)
        if not steam_binding:
            return {
                "success": False,
                "code": "steam_id_required",
                "message": "请先绑定 SteamID，再重置签到记录。",
            }
        if database_adapter is None:
            return {
                "success": False,
                "code": "steam_database_unavailable",
                "message": "服务器数据库暂不可用，未执行签到重置。",
            }
        database_result = _call_database_reset_adapter(
            database_adapter,
            str(steam_binding.get("steam_id", "")),
            date_value,
            currency_column=currency_column,
            reward=reward,
            remove_reward=remove_reward,
        )
        if database_result.get("success") is not True:
            return {
                "success": False,
                "code": database_result.get("code", "steam_database_error"),
                "message": database_result.get("message", "服务器数据库操作失败，未执行签到重置。"),
                "database_result": database_result,
                "steam_id": steam_binding.get("steam_id", ""),
            }

    signin_data = load_signin_data(group_id)
    stats = load_stats(group_id)
    aggregate = normalize_daily_signin_record(signin_data.get(user_id, {}))
    profile_id = str(policy.get("profile_id", "") or "").strip()
    if not _SIGNIN_PROFILE_ID_RE.fullmatch(profile_id):
        profile_id = ""
    profile_records = aggregate.get(SIGNIN_PROFILE_RECORDS_KEY)
    profile_records = dict(profile_records) if isinstance(profile_records, dict) else {}
    record = normalize_daily_signin_record(profile_records.get(profile_id, {}) if profile_id else aggregate)
    history = [value for value in record.get("signin_history", []) if str(value)[:10] != date_value]
    had_record = len(history) != len(record.get("signin_history", [])) or str(record.get("last_signin", ""))[:10] == date_value
    if not database_sync and not had_record:
        return {"success": False, "code": "sign_not_found", "message": "未找到该日期的签到记录。"}
    record["signin_history"] = history
    record["total_days"] = max(0, _as_non_negative_int(record.get("total_days", 0)) - (1 if had_record else 0))
    if str(record.get("last_signin", ""))[:10] == date_value:
        record["last_signin"] = history[-1] if history else ""
        record["consecutive_days"] = 0
    if profile_id:
        profile_records[profile_id] = record
        aggregate[SIGNIN_PROFILE_RECORDS_KEY] = profile_records
        if had_record:
            aggregate["total_days"] = max(0, _as_non_negative_int(aggregate.get("total_days", 0)) - 1)
            if str(aggregate.get("last_signin", ""))[:10] == date_value:
                aggregate["last_signin"] = history[-1] if history else ""
                aggregate["consecutive_days"] = 0
        signin_data[user_id] = aggregate
    else:
        signin_data[user_id] = record
    user_stats = _normalize_user_stats(stats.get("users", {}).get(user_id, {}))
    before_points = _as_int(user_stats.get("points", 0))
    if had_record:
        user_stats["signin_count"] = max(0, _as_non_negative_int(user_stats.get("signin_count", 0)) - 1)
        user_stats["total_signins"] = max(0, _as_non_negative_int(user_stats.get("total_signins", 0)) - 1)
    if database_result.get("balance") is not None:
        try:
            user_stats["points"] = int(database_result["balance"])
        except (TypeError, ValueError, OverflowError):
            pass
    elif reward is not None and remove_reward:
        try:
            user_stats["points"] = max(0, _as_int(user_stats.get("points", 0)) - int(reward))
        except (TypeError, ValueError, OverflowError):
            pass
    stats.setdefault("users", {})[user_id] = user_stats
    try:
        save_signin_data(group_id, signin_data)
        save_stats(group_id, stats)
    except Exception:
        return {
            "success": False,
            "code": "local_write_failed",
            "message": "服务器签到记录已重置，但本地镜像保存失败。",
            "database_result": database_result,
        }
    result = {
        "success": True,
        "code": database_result.get("code", "reset") if database_result else "reset",
        "sign_date": date_value,
        "removed_local_record": bool(had_record),
        "database_result": database_result,
        "before_points": before_points,
        "after_points": _as_int(user_stats.get("points", before_points)),
    }
    if steam_binding:
        result["steam_id"] = steam_binding.get("steam_id", "")
    return result

def signin(
    group_id: str,
    user_id: str,
    user_name: str = "",
    reward_config: dict | None = None,
    signin_policy: dict | None = None,
    database_adapter: object | None = None,
    backend: str | None = None,
) -> dict:
    """签到"""
    signin_data = load_signin_data(group_id)
    stats = load_stats(group_id)

    current_date = _current_local_date()
    today = current_date.isoformat()
    now = int(time.time())

    aggregate_user_data = normalize_daily_signin_record(signin_data.get(user_id, {}))
    user_stats = _normalize_user_stats(stats["users"].get(user_id, {}))
    # Steam mode is deliberately checked before mutating ordinary sign-in
    # records.  A missing binding is a user-facing validation error, not a
    # partially completed sign-in.
    policy = signin_policy if isinstance(signin_policy, dict) else {}
    profile_id = str(policy.get("profile_id", "") or "").strip()
    if not _SIGNIN_PROFILE_ID_RE.fullmatch(profile_id):
        profile_id = ""
    raw_profile_records = aggregate_user_data.get(SIGNIN_PROFILE_RECORDS_KEY)
    profile_records = (
        dict(raw_profile_records) if isinstance(raw_profile_records, dict) else {}
    )
    user_data = normalize_daily_signin_record(
        profile_records.get(profile_id, {}) if profile_id else aggregate_user_data
    )
    requested_backend = str(backend or "").strip().lower()
    if requested_backend not in {"local", "database", "websocket"}:
        signin_mode = str(policy.get("signin_mode", "basic") or "basic").strip().lower()
        reward_mode = str(
            policy.get("steam_reward_mode", policy.get("reward_mode", "database"))
            or "database"
        ).strip().lower()
        require_steam = signin_mode == "steam" and reward_mode != "websocket"
        raw_database_sync = policy.get("database_sync", False)
        if isinstance(raw_database_sync, str):
            raw_database_sync = raw_database_sync.strip().lower() not in {
                "",
                "0",
                "false",
                "off",
                "no",
            }
        # An explicit WebSocket Steam reward always wins over an inherited
        # ``database_sync`` flag from an older global policy.  Otherwise a
        # database flag preserves the old database-sign-in behaviour.
        requested_backend = (
            "local"
            if signin_mode == "steam" and reward_mode == "websocket"
            else "database" if require_steam or bool(raw_database_sync) else "local"
        )
    # New callers select the backend explicitly.  A WebSocket reward records
    # only the local claim and can never accidentally demand a database
    # adapter because the policy still says ``signin_mode=steam``.
    database_sync = requested_backend == "database"
    require_steam = database_sync
    steam_binding = None
    if database_sync:
        from .steam_signin import get_steam_binding

        steam_binding = get_steam_binding(group_id, user_id=user_id)
        if not steam_binding:
            return {
                "success": False,
                "code": "steam_id_required",
                "message": "请先使用 /绑定SteamID <17位SteamID> 绑定账号，再进行签到。",
            }
        if database_adapter is None:
            # A Steam sign-in without a writable game database would award a
            # local JSON-only balance and drift from the server economy.
            return {
                "success": False,
                "code": "steam_database_unavailable",
                "message": "服务器数据库暂不可用，暂时无法发放签到奖励，请稍后重试。",
            }
    # Sender display-name fields differ between adapters.  Prefer the current
    # event value, then retain an administratively stored name from either
    # persistence store instead of replacing it with an empty string.
    resolved_user_name = (
        str(user_name or "").strip()
        or str(user_data.get("user_name", "") or "").strip()
        or str(aggregate_user_data.get("user_name", "") or "").strip()
        or str(user_stats.get("user_name", "") or "").strip()
    )
    schedule_mode = str(policy.get("schedule_mode", "daily") or "daily").strip().lower()
    if schedule_mode not in {"daily", "weekly", "monthly", "streak", "custom"}:
        schedule_mode = "daily"
    daily_status = get_daily_signin_status(user_data, current_date)
    period_signed = _same_signin_period(user_data.get("last_signin"), current_date, schedule_mode)
    # In Steam/database mode the SQLite sign_log is authoritative.  A stale
    # local mirror must not suppress a legitimate reward; the adapter performs
    # the duplicate check under the same write transaction.
    if period_signed and not database_sync:
        period_label = {"weekly": "本周", "monthly": "本月"}.get(schedule_mode, "今天")
        return {
            "success": False,
            "code": "already_signed",
            "message": f"您{period_label}已经签到过了",
            "consecutive_days": daily_status["consecutive_days"],
            "total_days": daily_status["total_days"],
            "profile_id": profile_id,
        }

    consecutive_days = calculate_next_consecutive_days(user_data, current_date)
    reward = get_signin_reward(consecutive_days, reward_config)

    # The game database is authoritative for Steam-mode rewards.  Complete
    # its transaction before touching either local JSON store so a failed
    # SQLite write cannot be reported as a successful sign-in.
    database_result: dict = {}
    if database_sync:
        database_result = _call_database_reward_adapter(
            database_adapter,
            steam_binding.get("steam_id", "") if steam_binding else "",
            reward,
            player_name=resolved_user_name,
            sign_date=today,
            period_mode=schedule_mode,
        )
        if database_result.get("success") is not True:
            return {
                "success": False,
                "code": database_result.get("code", "steam_database_error"),
                "message": database_result.get(
                    "message", "服务器数据库操作失败，请稍后重试。"
                ),
                "database_result": database_result,
                "reward": reward,
                "steam_id": steam_binding.get("steam_id", "") if steam_binding else "",
                "profile_id": profile_id,
            }
        database_reward = database_result.get("change", reward)
        try:
            database_reward = int(database_reward)
        except (TypeError, ValueError, OverflowError):
            database_reward = reward
        if not 0 <= database_reward <= MAX_SIGNIN_REWARD:
            database_reward = reward
        reward = database_reward
        consecutive_days = _as_non_negative_int(
            database_result.get("consecutive_signins", consecutive_days),
            consecutive_days,
        ) or 1

    database_history = (
        _normalize_signin_history(database_result.get("sign_dates"))
        if database_result
        else []
    )
    database_total_days = (
        _as_non_negative_int(
            database_result.get("total_signins", daily_status["total_days"] + 1)
        )
        if database_result
        else daily_status["total_days"] + 1
    )

    updated_signin = dict(user_data)
    updated_signin.update({
        "last_signin": today,
        "consecutive_days": consecutive_days,
        "total_days": database_total_days,
        "signin_history": database_history or _append_signin_history(user_data, current_date),
        "user_name": resolved_user_name
    })
    if profile_id:
        # A configured scheme has an independent claim window and streak.
        # Keep those records below the existing per-user root so legacy
        # status/ranking readers still see one aggregate member record rather
        # than treating every scheme as a separate QQ account.
        profile_records[profile_id] = updated_signin
        updated_aggregate = dict(aggregate_user_data)
        aggregate_status = get_daily_signin_status(aggregate_user_data, current_date)
        if database_result:
            updated_aggregate.update(
                {
                    "last_signin": today,
                    "consecutive_days": consecutive_days,
                    "total_days": database_total_days,
                    "signin_history": database_history
                    or _append_signin_history(aggregate_user_data, current_date),
                }
            )
        elif not aggregate_status["signed_today"]:
            aggregate_consecutive = calculate_next_consecutive_days(
                aggregate_user_data, current_date
            )
            updated_aggregate.update(
                {
                    "last_signin": today,
                    "consecutive_days": aggregate_consecutive,
                    "total_days": aggregate_status["total_days"] + 1,
                    "signin_history": _append_signin_history(
                        aggregate_user_data, current_date
                    ),
                }
            )
        updated_aggregate["user_name"] = resolved_user_name
        updated_aggregate[SIGNIN_PROFILE_RECORDS_KEY] = profile_records
        signin_data[user_id] = updated_aggregate
    else:
        signin_data[user_id] = updated_signin

    updated_stats = dict(user_stats)
    database_balance = database_result.get("balance") if database_result else None
    try:
        mirrored_points = int(database_balance) if database_balance is not None else None
    except (TypeError, ValueError, OverflowError):
        mirrored_points = None
    updated_stats.update({
        "points": (
            mirrored_points
            if mirrored_points is not None
            else _as_int(user_stats.get("points", 0)) + reward
        ),
        "signin_count": _as_non_negative_int(user_stats.get("signin_count", 0)) + 1,
        "total_signins": (
            database_total_days
            if database_result
            else _as_non_negative_int(user_stats.get("total_signins", 0)) + 1
        ),
        "user_name": resolved_user_name,
        "joined_at": user_stats.get("joined_at", now)
    })
    stats["users"][user_id] = updated_stats

    try:
        save_signin_data(group_id, signin_data)
        save_stats(group_id, stats)
    except Exception:
        # SQLite has already committed at this point.  Surface the split-store
        # state explicitly instead of claiming that the local mirror is in
        # sync; the next status read can still recover from the DB source.
        return {
            "success": False,
            "code": "local_write_failed",
            "message": "签到奖励已写入服务器，但本地签到记录保存失败，请稍后重试。",
            "database_result": database_result,
            "reward": reward,
            "steam_id": steam_binding.get("steam_id", "") if steam_binding else "",
            "profile_id": profile_id,
        }

    if steam_binding:
        from .steam_signin import record_steam_signin

        steam_record = record_steam_signin(group_id, user_id, resolved_user_name)
        if (
            not isinstance(steam_record, dict)
            or (
                "success" in steam_record
                and steam_record.get("success") is not True
            )
        ):
            # The database reward may already have committed.  Do not report
            # a completed Steam sign-in while its local binding mirror cannot
            # be updated; this is a recoverable, auditable partial failure.
            return {
                "success": False,
                "code": "steam_binding_record_failed",
                "message": "签到奖励已执行，但 SteamID 关联记录保存失败，请联系管理员核对后重试。",
                "database_result": database_result,
                "reward": reward,
                "steam_id": steam_binding.get("steam_id", ""),
                "profile_id": profile_id,
            }
    
    result = {
        "success": True,
        "backend": requested_backend,
        "message": (
            f"签到成功！连续签到 {consecutive_days} 天，获得 {reward} 数据库奖励"
            if database_result
            else f"签到成功！连续签到 {consecutive_days} 天，获得 {reward} 积分"
        ),
        "consecutive_days": consecutive_days,
        "total_days": updated_signin["total_days"],
        "points": stats["users"][user_id]["points"],
        "reward": reward,
        "profile_id": profile_id,
        "profile_name": str(policy.get("profile_name", "") or "")[:80],
    }
    if database_result:
        result["database_result"] = database_result
        result["database_balance"] = database_result.get(
            "balance", database_result.get("after")
        )
    if steam_binding:
        result["steam_id"] = steam_binding.get("steam_id", "")
    return result

def get_signin_status(group_id: str, user_id: str) -> dict:
    """获取签到状态"""
    signin_data = load_signin_data(group_id)
    stats = load_stats(group_id)
    daily_status = get_daily_signin_status(signin_data.get(user_id, {}))
    user_stats = _normalize_user_stats(stats["users"].get(user_id, {}))

    result = {
        "signed_today": daily_status["signed_today"],
        "consecutive_days": daily_status["consecutive_days"],
        "total_days": daily_status["total_days"],
        "points": _as_int(user_stats.get("points", 0)),
        "user_name": daily_status["user_name"] or user_stats.get("user_name", "")
    }
    return result

def get_monthly_signin_summary(
    group_id: str,
    user_id: str,
    today: date | None = None,
) -> dict:
    """Return the requested month's sign-in dates and the user's current status.

    ``today`` is primarily useful to callers rendering a previous/current
    month consistently in tests; invalid values resolve to the local date.
    """
    current_date = _parse_calendar_date(today) or _current_local_date()
    signin_data = load_signin_data(group_id)
    user_data = normalize_daily_signin_record(signin_data.get(user_id, {}))
    history = [
        value
        for value in user_data["signin_history"]
        if (parsed := _parse_calendar_date(value))
        and parsed.year == current_date.year
        and parsed.month == current_date.month
    ]
    status = get_daily_signin_status(user_data, current_date)

    return {
        "month": current_date.strftime("%Y-%m"),
        "signin_days": len(history),
        "dates": history,
        "signed_today": status["signed_today"],
        "consecutive_days": status["consecutive_days"],
        "total_days": status["total_days"],
    }


def _normalize_signin_ranking_limit(limit: int) -> int:
    """Cap ranking output while preserving the default for malformed limits."""
    try:
        normalized = int(limit)
    except (TypeError, ValueError, OverflowError):
        normalized = 10
    return min(MAX_SIGNIN_RANKING_LIMIT, max(0, normalized))


def get_signin_ranking(group_id: str, limit: int = 10) -> list:
    """Return a deterministic ranking by total sign-in days.

    Sign-in data is authoritative. Existing stats are used as a legacy fallback
    for user names and pre-history sign-in totals.
    """
    signin_data = load_signin_data(group_id)
    stats = load_stats(group_id)
    users = stats.get("users", {})
    ranking = []

    for user_id in set(signin_data) | set(users):
        user_signin = normalize_daily_signin_record(signin_data.get(user_id, {}))
        user_stats = _normalize_user_stats(users.get(user_id, {}))
        total_days = user_signin["total_days"]
        if not total_days:
            total_days = _as_non_negative_int(
                user_stats.get("total_signins", user_stats.get("signin_count", 0))
            )
        if not total_days:
            continue

        ranking.append({
            "user_id": user_id,
            "user_name": user_signin["user_name"] or user_stats.get("user_name", ""),
            "total_days": total_days,
            "consecutive_days": user_signin["consecutive_days"],
            "last_signin": user_signin["last_signin"],
        })

    ranking.sort(
        key=lambda item: (
            -item["total_days"],
            -item["consecutive_days"],
            str(item["user_id"]),
        )
    )
    return [
        {"rank": index, **entry}
        for index, entry in enumerate(ranking[:_normalize_signin_ranking_limit(limit)], 1)
    ]


def get_ranking(group_id: str, limit: int = 10) -> list:
    """获取积分排行"""
    stats = load_stats(group_id)
    users = stats.get("users", {})
    
    ranking = []
    for user_id, data in users.items():
        data = _normalize_user_stats(data)
        ranking.append({
            "user_id": user_id,
            "user_name": data.get("user_name", ""),
            "points": _as_int(data.get("points", 0)),
            "signin_count": _as_non_negative_int(data.get("signin_count", 0)),
            "total_signins": _as_non_negative_int(data.get("total_signins", 0))
        })
    
    ranking.sort(key=lambda x: x["points"], reverse=True)
    return ranking[:limit]

def get_user_info(group_id: str, user_id: str) -> dict:
    """获取用户信息"""
    stats = load_stats(group_id)
    signin_data = load_signin_data(group_id)
    
    user_stats = _normalize_user_stats(stats["users"].get(user_id, {}))
    user_signin = normalize_daily_signin_record(signin_data.get(user_id, {}))
    
    return {
        "user_id": user_id,
        "user_name": user_stats.get("user_name", "") or user_signin.get("user_name", ""),
        "points": _as_int(user_stats.get("points", 0)),
        "signin_count": _as_non_negative_int(user_stats.get("signin_count", 0)),
        "total_signins": _as_non_negative_int(user_stats.get("total_signins", 0)),
        "consecutive_days": user_signin["consecutive_days"],
        "total_signin_days": user_signin["total_days"],
        "last_signin": user_signin["last_signin"],
        "joined_at": user_stats.get("joined_at", 0)
    }

def get_group_stats(group_id: str) -> dict:
    """获取群组统计"""
    stats = load_stats(group_id)
    signin_data = load_signin_data(group_id)
    
    users = stats.get("users", {})
    today_signins = 0
    total_users = len(users)
    total_points = sum(
        _as_int(_normalize_user_stats(user).get("points", 0))
        for user in users.values()
    )

    today = _current_local_date()
    for data in signin_data.values():
        if get_daily_signin_status(data, today)["signed_today"]:
            today_signins += 1
    
    return {
        "total_users": total_users,
        "today_signins": today_signins,
        "total_points": total_points,
        "avg_points": int(total_points / total_users) if total_users > 0 else 0
    }

def add_points(group_id: str, user_id: str, points: int, reason: str = "") -> bool:
    """添加积分"""
    def _update(stats):
        if stats is None:
            stats = {"users": {}}
        if "users"not in stats:
            stats["users"] = {}
        if user_id not in stats["users"]:
            stats["users"][user_id] = {"points": 0}
        stats["users"][user_id]["points"] += points
        return stats
    
    update_group_data(group_id, "stats", _update)
    return True

def deduct_points(group_id: str, user_id: str, points: int, reason: str = "") -> bool:
    """扣除积分"""
    stats = load_stats(group_id)
    user_stats = stats.get("users", {}).get(user_id, {})
    current = user_stats.get("points", 0)
    
    if current < points:
        return False
    
    def _update(stats):
        if stats is None or "users"not in stats:
            return stats
        if user_id in stats["users"]:
            stats["users"][user_id]["points"] -= points
        return stats
    
    update_group_data(group_id, "stats", _update)
    return True

def set_user_name(group_id: str, user_id: str, user_name: str) -> bool:
    """设置并持久化分群成员显示名。

    旧实现只更新已经签到过的成员，导致管理员无法提前初始化名称。
    现在会创建零签到记录，不会增加积分或签到次数。
    """
    normalized_user_id = str(user_id or "").strip()
    normalized_name = str(user_name or "").strip()[:128]
    if not normalized_user_id or not normalized_name:
        return False

    # 更新统计中的用户名，并在需要时初始化用户记录。
    def _update_stats(stats):
        if not isinstance(stats, dict):
            stats = {"users": {}}
        if not isinstance(stats.get("users"), dict):
            stats["users"] = {}
        user = stats["users"].get(normalized_user_id)
        if not isinstance(user, dict):
            user = {}
            stats["users"][normalized_user_id] = user
        user["user_name"] = normalized_name
        user.setdefault("joined_at", int(time.time()))
        return stats
    update_group_data(group_id, "stats", _update_stats)

    # 更新签到中的用户名，并初始化零签到记录。
    def _update_signin(signin):
        if not isinstance(signin, dict):
            signin = {}
        user = signin.get(normalized_user_id)
        if not isinstance(user, dict):
            user = normalize_daily_signin_record({})
            signin[normalized_user_id] = user
        user["user_name"] = normalized_name
        return signin
    update_group_data(group_id, "signin", _update_signin)
    return True


def get_user_name(group_id: str, user_id: str) -> str:
    """读取分群成员的持久化显示名。"""
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return ""
    stats = load_stats(group_id)
    user_stats = _normalize_user_stats(stats.get("users", {}).get(normalized_user_id, {}))
    if user_stats.get("user_name"):
        return str(user_stats["user_name"])
    signin = normalize_daily_signin_record(
        load_signin_data(group_id).get(normalized_user_id, {})
    )
    return str(signin.get("user_name", "") or "")

def clear_stats(group_id: str):
    """清空群组统计"""
    set_group_data(group_id, "stats", {"users": {}})
    set_group_data(group_id, "signin", {})
