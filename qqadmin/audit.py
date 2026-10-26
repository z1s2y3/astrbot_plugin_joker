import time
import random
import re
import unicodedata
from decimal import Decimal, InvalidOperation
import logging

try:
    # AstrBot provides structured console logging in production.  The
    # module is also imported directly by offline invite-stat tests, where
    # AstrBot is intentionally absent, so keep a standard-library fallback.
    from astrbot.api import logger
except Exception:  # pragma: no cover - exercised by standalone imports
    logger = logging.getLogger(__name__)

from .group_config import get_group_data, set_group_data, update_group_data


def normalize_verification_answer(value: object, mode: str = "") -> str:
    """Normalize a member's verification reply without joining unrelated digits.

    QQ messages often contain labels such as ``答案是`` or ``验证 ID：``.
    Taking every digit from the complete message turns ``1 + 2 = 3`` into
    ``123`` and ``4.0`` into ``40``. Use the final numeric token instead,
    which is the answer shown to the member, while retaining a text fallback
    for legacy non-numeric approval values.
    """

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", text).strip()
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode == "math":
        tokens = re.findall(r"[-+]?(?:\d+(?:[.,]\d+)?|\.\d+)", text)
        if tokens:
            return tokens[-1].replace(",", ".")
    elif normalized_mode in {"id", "code", "direct"}:
        tokens = re.findall(r"\d+", text)
        if tokens:
            return tokens[-1]
    else:
        tokens = re.findall(r"\d+", text)
        if tokens and text != tokens[-1]:
            return tokens[-1]
    return text

def generate_approval_code() -> str:
    return str(random.randint(1000, 9999))

def add_pending_request(group_id: str, user_id: str, user_name: str,
                       join_request: str = "", source: str = "normal",
                       flag: str = "", challenge_info: dict = None,
                       approval_mode: str = None) -> dict:
    if approval_mode is None:
        approval_mode = get_audit_approval_mode(group_id)
    # Keep the approval mode selected by the group configuration.  ``id`` is
    # a real challenge (the member must send the generated verification ID),
    # while ``direct`` means the request is approved without a challenge.  A
    # previous web build incorrectly rewrote ``id`` to ``math``; doing that
    # here made an ID-enabled group impossible to use after synchronization.
    if approval_mode not in {"math", "id", "code", "direct", "steamid"}:
        approval_mode = "math"
    
    if is_in_audit_pending(group_id, user_id):
        return {"success": False, "message": "用户已在待验证队列中"}
    
    if challenge_info is None:
        if approval_mode == "math":
            challenge_info = create_math_challenge(group_id, user_id)
        elif approval_mode == "id":
            challenge_info = create_id_challenge(group_id, user_id)

    approval_code = generate_approval_code()

    def _add(pending):
        if pending is None:
            pending = []
        
        pending.append({
            "user_id": user_id,
            "user_name": user_name,
            "join_request": join_request,
            "source": source,
            "requested_at": int(time.time()),
            "status": "pending",
            "approval_mode": approval_mode,
            "challenge_id": challenge_info["challenge_id"] if challenge_info else None,
            "verification_question": challenge_info.get("question") or challenge_info.get("verify_id", "") if challenge_info else "",
            # Keep the ID on the pending row as well as in the challenge map.
            # The prompt may be recalled or the challenge cache may be
            # repaired while the member is still answering; the retry reply
            # must still tell the member exactly which ID to send.
            "verify_id": challenge_info.get("verify_id", "") if challenge_info else "",
            "answer": challenge_info.get("answer", "") if challenge_info else "",
            "approval_code": approval_code,
            "flag": flag,
            "attempts": 0,
            # The timeout window starts only after a prompt was delivered.
            # These fields are additive so existing pending rows remain valid.
            "verification_prompt_sent": False,
            "steamid_prompt_sent": False,
            "prompt_sent_at": 0,
            "verification_prompt_sent_at": 0,
            "steamid_prompt_sent_at": 0,
        })
        return pending
    
    update_group_data(group_id, "audit_pending", _add)
    # Return the effective mode as well as the challenge.  Callers handling
    # invitation notices used to infer the mode from whether a challenge was
    # present; that made ``direct`` indistinguishable from a malformed
    # request and caused it to fall through to the math prompt.
    return {
        "success": True,
        "approval_code": approval_code,
        "challenge_info": challenge_info or {},
        "approval_mode": approval_mode,
    }

def remove_pending_request(group_id: str, user_id: str) -> bool:
    def _remove(pending):
        if pending is None:
            return []
        return [r for r in pending if r.get("user_id") != user_id]
    
    update_group_data(group_id, "audit_pending", _remove)
    return True


def rearm_pending_request(group_id: str, user_id: str) -> dict:
    """Restart a stale verification record when a member joins again.

    QQ can deliver a new request/join notice while the previous pending row is
    still present. Reusing its old timeout and ``steamid_prompt_sent`` flag
    silently kicks the member without showing a new prompt. Rearming keeps the
    selected verification mode but starts a fresh window and prompt cycle.
    """
    target = str(user_id or "").strip()
    result: dict = {}
    now = int(time.time())

    def _rearm(pending):
        for request in pending or []:
            if str(request.get("user_id", "")) != target:
                continue
            if str(request.get("status", "pending")) != "pending":
                continue
            try:
                request_age = max(0, now - int(request.get("requested_at", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                request_age = 10**9
            # A request event and its join notice can be emitted together.
            # Do not rearm that same event twice; only stale records or rows
            # that already consumed their prompt get a new window.
            if request_age < 30 and not bool(request.get("steamid_prompt_sent", False)):
                result.update(request)
                break
            request["requested_at"] = now
            request["joined_at"] = now
            request["attempts"] = 0
            request["steamid_prompt_sent"] = False
            request["verification_prompt_sent"] = False
            request["prompt_sent_at"] = 0
            request["verification_prompt_sent_at"] = 0
            request["steamid_prompt_sent_at"] = 0
            request.pop("verification_prompt_failed", None)
            if request.get("steamid_stage"):
                request["steamid_requested_at"] = now
            result.update(request)
            break
        return pending or []

    update_group_data(group_id, "audit_pending", _rearm)
    return result

def increment_pending_attempts(group_id: str, user_id: str) -> int:
    def _increment(pending):
        if pending is None:
            return []
        for req in pending:
            if str(req.get("user_id", "")) == str(user_id):
                req["attempts"] = req.get("attempts", 0) + 1
        return pending
    
    pending = update_group_data(group_id, "audit_pending", _increment)
    for req in pending:
        if str(req.get("user_id", "")) == str(user_id):
            return req.get("attempts", 0)
    return 0


def mark_pending_kick_exhausted(
    group_id: str, user_id: str, error: str = ""
) -> bool:
    """Freeze a max-attempt row after the kick request could not be confirmed.

    QQ can reject a kick while the member is still present (for example when
    the request was sent through the wrong bot account).  Keeping the row at
    the configured limit prevents every subsequent group message from being
    counted as a new attempt and producing misleading ``4/3`` or ``5/3``
    records.  The row remains pending so the timeout/retry path can still
    reconcile it once the platform becomes available.
    """

    changed = False
    detail = str(error or "").strip()[:500]

    def _mark(pending):
        nonlocal changed
        if not isinstance(pending, list):
            return pending or []
        for request in pending:
            if str(request.get("user_id", "")) != str(user_id):
                continue
            if str(request.get("status", "pending")) != "pending":
                continue
            request["verification_kick_exhausted"] = True
            request["verification_kick_failed_at"] = int(time.time())
            if detail:
                request["verification_kick_error"] = detail
            try:
                request["attempts"] = max(0, int(request.get("attempts", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                request["attempts"] = 0
            changed = True
            break
        return pending

    update_group_data(group_id, "audit_pending", _mark)
    return changed


def mark_pending_steamid_stage(group_id: str, user_id: str) -> bool:
    """Switch a successful challenge to the database SteamID step.

    The original challenge remains in the audit record until the SteamID is
    accepted, so a process restart cannot accidentally approve a member before
    the authoritative database lookup has completed.
    """

    changed = False

    def _mark(pending):
        nonlocal changed
        if not isinstance(pending, list):
            return pending or []
        for request in pending:
            if str(request.get("user_id", "")) != str(user_id):
                continue
            if str(request.get("status", "pending")) != "pending":
                continue
            request["steamid_stage"] = True
            request["attempts"] = 0
            request["steamid_requested_at"] = int(time.time())
            changed = True
            break
        return pending

    update_group_data(group_id, "audit_pending", _mark)
    return changed


def mark_pending_steamid_warning(group_id: str, user_id: str, warning: str) -> bool:
    """Record a one-time SteamID input warning on the pending request."""
    key = str(warning or "").strip().lower()
    if key not in {"format", "not_found"}:
        return False
    field = f"steamid_{key}_warned"
    changed = False

    def _mark(pending):
        nonlocal changed
        if not isinstance(pending, list):
            return pending or []
        for request in pending:
            if str(request.get("user_id", "")) != str(user_id):
                continue
            if str(request.get("status", "pending")) != "pending":
                continue
            if request.get(field):
                break
            request[field] = True
            changed = True
            break
        return pending

    update_group_data(group_id, "audit_pending", _mark)
    return changed


def switch_pending_approval_mode(
    group_id: str,
    user_id: str,
    approval_mode: str,
    challenge_info: dict | None = None,
) -> dict:
    """Move a pending request to a non-Steam fallback verification mode."""
    mode = str(approval_mode or "").strip().lower()
    if mode not in {"math", "id", "code", "direct"}:
        return {"success": False, "message": "不支持的备用验证方式"}

    info = challenge_info if isinstance(challenge_info, dict) else {}
    changed = False
    old_challenge_id = ""
    result: dict = {}

    def _switch(pending):
        nonlocal changed, old_challenge_id, result
        if not isinstance(pending, list):
            return pending or []
        for request in pending:
            if str(request.get("user_id", "")) != str(user_id):
                continue
            if str(request.get("status", "pending")) != "pending":
                continue
            old_challenge_id = str(request.get("challenge_id", "") or "")
            request["approval_mode"] = mode
            request["steamid_fallback"] = True
            request["steamid_stage"] = False
            request["challenge_id"] = info.get("challenge_id") if info else None
            request["verification_question"] = (
                info.get("question") or info.get("verify_id", "")
            ) if info else ""
            request["verify_id"] = info.get("verify_id", "") if info else ""
            request["answer"] = info.get("answer", "") if info else ""
            request["approval_code"] = (
                generate_approval_code() if mode == "code" else ""
            )
            request["attempts"] = 0
            request["fallback_selected_at"] = int(time.time())
            result = dict(request)
            changed = True
            break
        return pending

    update_group_data(group_id, "audit_pending", _switch)
    if not changed:
        return {"success": False, "message": "待验证记录不存在或已处理"}
    if old_challenge_id:
        update_group_data(
            group_id,
            "audit_challenges",
            lambda challenges: {
                key: value
                for key, value in (challenges or {}).items()
                if key != old_challenge_id
            },
        )
    return {
        "success": True,
        "approval_mode": mode,
        "challenge_info": info,
        "approval_code": result.get("approval_code", ""),
    }

def get_pending_requests(group_id: str, include_processed: bool = False) -> list:
    pending = get_group_data(group_id, "audit_pending", [])
    if include_processed:
        return pending
    return [r for r in pending if str(r.get("status")) == "pending"]

def is_in_audit_pending(group_id: str, user_id: str) -> bool:
    pending = get_group_data(group_id, "audit_pending", [])
    for req in pending:
        if str(req.get("user_id")) == str(user_id) and str(req.get("status")) == "pending":
            return True
    return False

def get_pending_request_by_code(group_id: str, approval_code: str) -> dict:
    pending = get_group_data(group_id, "audit_pending", [])
    for req in pending:
        if req.get("approval_code") == approval_code and req.get("status") == "pending":
            return req
    return {}

def get_pending_by_code(group_id: str, code: str) -> dict:
    """别名，保持向后兼容"""
    return get_pending_request_by_code(group_id, code)

def get_request_by_code_any_status(group_id: str, approval_code: str) -> dict:
    """查找审批码对应的申请，不检查状态"""
    pending = get_group_data(group_id, "audit_pending", [])
    for req in pending:
        if req.get("approval_code") == approval_code:
            return req
    return {}

def get_pending_request_by_code_all_groups(approval_code: str) -> tuple:
    """在所有群中查找审批码，返回 (group_id, request)"""
    from .group_config import list_all_groups
    all_groups = list_all_groups()
    for gid in all_groups:
        pending = get_group_data(gid, "audit_pending", [])
        for req in pending:
            if req.get("approval_code") == approval_code and req.get("status") == "pending":
                return (gid, req)
    return (None, {})

def get_pending_request_by_user_all_groups(user_id: str) -> tuple:
    """Find a pending request for a member who has not joined a group yet."""
    from .group_config import list_all_groups
    target = str(user_id or "")
    for gid in list_all_groups():
        pending = get_group_data(gid, "audit_pending", [])
        for request in pending:
            if (
                str(request.get("user_id", "")) == target
                and request.get("status", "pending") == "pending"
            ):
                return gid, request
    return None, {}


def get_pending_steamid_request_by_user_all_groups(user_id: str) -> tuple:
    """Find the user's pending SteamID stage without mixing other groups."""
    from .group_config import list_all_groups

    target = str(user_id or "")
    for gid in list_all_groups():
        pending = get_group_data(gid, "audit_pending", [])
        for request in pending:
            if (
                str(request.get("user_id", "")) == target
                and request.get("status", "pending") == "pending"
                and (
                    str(request.get("approval_mode", "") or "") == "steamid"
                    or bool(request.get("steamid_stage", False))
                )
            ):
                return gid, request
    return None, {}

def get_request_by_code_any_status_all_groups(approval_code: str) -> tuple:
    """在所有群中查找审批码（不检查状态），返回 (group_id, request)"""
    from .group_config import list_all_groups
    all_groups = list_all_groups()
    for gid in all_groups:
        pending = get_group_data(gid, "audit_pending", [])
        for req in pending:
            if req.get("approval_code") == approval_code:
                return (gid, req)
    return (None, {})

def add_audit_history(group_id: str, user_id: str, user_name: str,
                     action: str, operator_id: str = "", reason: str = ""):
    def _add(history):
        if history is None:
            history = []
        history.append({
            "user_id": user_id,
            "user_name": user_name,
            "action": action,
            "operator_id": operator_id,
            "reason": reason,
            "timestamp": int(time.time())
        })
        if len(history) > 500:
            history = history[-500:]
        return history
    
    update_group_data(group_id, "audit_history", _add)

def get_audit_history(group_id: str, limit: int = 50) -> list:
    history = get_group_data(group_id, "audit_history", [])
    return history[-limit:] if history else []

def get_pending_count(group_id: str) -> int:
    pending = get_group_data(group_id, "audit_pending", [])
    return len([r for r in pending if r.get("status") == "pending"])

def add_to_whitelist(group_id: str, user_id: str):
    def _add(whitelist):
        if whitelist is None:
            whitelist = []
        if user_id not in whitelist:
            whitelist.append(user_id)
        return whitelist
    
    update_group_data(group_id, "audit_whitelist", _add)

def remove_from_whitelist(group_id: str, user_id: str):
    def _remove(whitelist):
        if whitelist is None:
            return []
        if user_id in whitelist:
            whitelist.remove(user_id)
        return whitelist
    
    update_group_data(group_id, "audit_whitelist", _remove)

def is_in_whitelist(group_id: str, user_id: str) -> bool:
    whitelist = get_group_data(group_id, "audit_whitelist", [])
    return user_id in whitelist

def get_whitelist_users(group_id: str) -> list:
    return get_group_data(group_id, "audit_whitelist", [])

# ==================== 验证挑战相关 ====================
def create_math_challenge(group_id: str, user_id: str) -> dict:
    num1 = random.randint(1, 20)
    num2 = random.randint(1, 10)
    op = random.choice(["+", "-", "*", "/"])
    if op == "+":
        answer = num1 + num2
        question = f"{num1} + {num2} = ?"
    elif op == "-":
        if num1 < num2:
            num1, num2 = num2, num1
        answer = num1 - num2
        question = f"{num1} - {num2} = ?"
    elif op == "*":
        answer = num1 * num2
        question = f"{num1} × {num2} = ?"
    else:
        num2 = random.randint(1, 10)
        answer = num1 * num2
        question = f"{answer} ÷ {num2} = ?"
        answer = num1

    # Multiple QQ accounts can receive the same request within one second.
    # A nanosecond id ensures a duplicate cleanup cannot remove the first
    # account's active challenge.
    challenge_id = f"{group_id}_{user_id}_{time.time_ns()}"
    
    def _save(challenges):
        if challenges is None:
            challenges = {}
        challenges[challenge_id] = {
            "challenge_id": challenge_id,
            "group_id": group_id,
            "user_id": user_id,
            "question": question,
            "answer": str(answer),
            "created_at": int(time.time()),
            "attempts": 0
        }
        return challenges
    
    update_group_data(group_id, "audit_challenges", _save)
    return {"challenge_id": challenge_id, "question": question, "answer": str(answer)}

def create_id_challenge(group_id: str, user_id: str) -> dict:
    challenge_id = f"{group_id}_{user_id}_{time.time_ns()}"
    verify_id = str(random.randint(10000, 99999))
    
    def _save(challenges):
        if challenges is None:
            challenges = {}
        challenges[challenge_id] = {
            "challenge_id": challenge_id,
            "group_id": group_id,
            "user_id": user_id,
            "verify_id": verify_id,
            "created_at": int(time.time()),
            "attempts": 0
        }
        return challenges
    
    update_group_data(group_id, "audit_challenges", _save)
    return {"challenge_id": challenge_id, "verify_id": verify_id}

def get_challenge(challenge_id: str) -> dict:
    return None

def remove_challenge(group_id: str, challenge_id: str):
    def _remove(challenges):
        if challenges and challenge_id in challenges:
            del challenges[challenge_id]
        return challenges
    
    update_group_data(group_id, "audit_challenges", _remove)

def verify_challenge_answer(group_id: str, challenge_id: str, user_answer: str, max_attempts: int = 3) -> tuple:
    """Verify and persist one math/ID attempt atomically.

    Some storage backends deserialize ``get_group_data`` results.  Mutating
    that returned dictionary alone therefore lost the attempt counter and
    made every wrong answer look like the first attempt.  The updater below
    writes the counter and challenge removal in one transaction.
    """
    try:
        attempt_limit = max(1, int(max_attempts))
    except (TypeError, ValueError, OverflowError):
        attempt_limit = 3

    result = {"found": False, "correct": False, "reject": False}

    def _verify(challenges):
        if not isinstance(challenges, dict) or challenge_id not in challenges:
            result["reject"] = True
            return challenges or {}

        challenge = challenges.get(challenge_id) or {}
        result["found"] = True
        try:
            attempts = max(0, int(challenge.get("attempts", 0) or 0)) + 1
        except (TypeError, ValueError, OverflowError):
            attempts = 1
        challenge["attempts"] = attempts

        is_math = "answer" in challenge
        expected = challenge.get("answer") if is_math else challenge.get("verify_id", "")
        submitted = normalize_verification_answer(
            user_answer,
            "math" if is_math else "id",
        )
        expected_text = normalize_verification_answer(
            expected,
            "math" if is_math else "id",
        )
        # A math answer copied from a calculator may be ``2.0``.  Normalize
        # integral decimals while leaving ID values exact.
        if is_math:
            try:
                submitted_number = Decimal(submitted)
                expected_number = Decimal(expected_text)
                correct = submitted_number == expected_number
            except (InvalidOperation, ValueError):
                correct = submitted == expected_text
        else:
            correct = submitted == expected_text
        result["correct"] = bool(correct)
        result["reject"] = not correct and attempts >= attempt_limit
        if correct or result["reject"]:
            challenges.pop(challenge_id, None)
        return challenges

    update_group_data(group_id, "audit_challenges", _verify)
    if not result["found"]:
        return False, True
    return bool(result["correct"]), bool(result["reject"])

def get_challenge_answer(group_id: str, challenge_id: str) -> str:
    challenges = get_group_data(group_id, "audit_challenges", {})
    if challenges and challenge_id in challenges:
        challenge = challenges[challenge_id]
        if "answer" in challenge:
            return challenge["answer"]
        return challenge.get("verify_id", "")
    return ""

# ==================== 邀请统计 ====================
# A group invite may be reported by both a request event and a join notice.
# The two events usually arrive seconds apart and must not inflate statistics.
_INVITE_DEDUP_WINDOW_SECONDS = 5 * 60
_INVITE_STATUS_KEYS = ("pending", "joined", "rejected", "left")


def _empty_invite_status_counts() -> dict:
    return {status: 0 for status in _INVITE_STATUS_KEYS}


def _coerce_invite_timestamp(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_local_invite_date(timestamp) -> str:
    """Return the local calendar date used for invitation rate limits."""
    return time.strftime("%Y-%m-%d", time.localtime(_coerce_invite_timestamp(timestamp)))


def _get_invite_date(invite: dict) -> str:
    saved_date = invite.get("invite_date")
    if isinstance(saved_date, str) and saved_date.strip():
        return saved_date.strip()
    return _get_local_invite_date(invite.get("invited_at", 0))


def _normalize_invite_status(status) -> str:
    """Map legacy status values into the four reporting states."""
    value = str(status or "").strip().lower()
    if value in {"joined", "approved"}:
        return "joined"
    if value in {"rejected", "timeout_rejected", "denied"}:
        return "rejected"
    if value in {"left", "left_group"}:
        return "left"
    return "pending"


def _invite_was_successful(invite: dict) -> bool:
    """A member who later left still counts as a successful invitation."""
    if invite.get("joined_at"):
        return True
    return _normalize_invite_status(invite.get("status")) in {"joined", "left"}


def _get_invite_records(stats) -> list:
    if not isinstance(stats, dict):
        return []
    invites = stats.get("invites", [])
    if not isinstance(invites, list):
        return []
    return [invite for invite in invites if isinstance(invite, dict)]


def _build_invite_statistics(invites: list) -> tuple[dict, dict, dict]:
    """Build all derived invite counters from the append-only event records."""
    inviter_counts = {}
    status_counts = _empty_invite_status_counts()
    daily_counts = {}

    for invite in invites:
        inviter_id = str(invite.get("inviter_id", ""))
        invite_date = _get_invite_date(invite)
        status = _normalize_invite_status(invite.get("status"))

        inviter_counts[inviter_id] = inviter_counts.get(inviter_id, 0) + 1
        status_counts[status] += 1

        day = daily_counts.setdefault(
            invite_date,
            {
                "total": 0,
                "inviter_counts": {},
                "status_counts": _empty_invite_status_counts(),
            },
        )
        day["total"] += 1
        day["inviter_counts"][inviter_id] = (
            day["inviter_counts"].get(inviter_id, 0) + 1
        )
        day["status_counts"][status] += 1

    return inviter_counts, status_counts, daily_counts


def _refresh_invite_statistics(stats: dict) -> dict:
    """Refresh derived fields while retaining the legacy invite_stats layout."""
    if not isinstance(stats, dict):
        stats = {}
    invites = stats.get("invites")
    if not isinstance(invites, list):
        invites = []
        stats["invites"] = invites

    records = _get_invite_records(stats)
    inviter_counts, status_counts, daily_counts = _build_invite_statistics(records)
    # inviter_counts is retained because existing callers read it directly.
    stats["inviter_counts"] = inviter_counts
    stats["status_counts"] = status_counts
    stats["daily_counts"] = daily_counts
    return stats


def _is_duplicate_invitation(
    invites: list,
    inviter_id: str,
    invitee_id: str,
    event_id: str,
    invited_at: int,
) -> bool:
    """Identify duplicated notices without suppressing a later re-invitation."""
    for invite in reversed(invites):
        if not isinstance(invite, dict):
            continue
        if event_id and str(invite.get("event_id", "")) == event_id:
            return True
        if (
            str(invite.get("inviter_id", "")) != inviter_id
            or str(invite.get("invitee_id", "")) != invitee_id
        ):
            continue

        previous_timestamp = _coerce_invite_timestamp(invite.get("invited_at", 0))
        if abs(invited_at - previous_timestamp) <= _INVITE_DEDUP_WINDOW_SECONDS:
            return True
        if _normalize_invite_status(invite.get("status")) in {"pending", "joined"}:
            return True
    return False


def record_invitation(
    group_id: str,
    inviter_id: str,
    invitee_id: str,
    invitee_name: str = "",
    event_id: str = "",
    occurred_at: int | None = None,
) -> None:
    """Record one logical invitation.

    ``event_id`` is optional for old callers.  When it is unavailable, a
    short local time window plus the inviter/invitee pair suppresses duplicate
    notices while permitting a new invitation after rejection or departure.
    """
    inviter_id = str(inviter_id)
    invitee_id = str(invitee_id)
    event_id = str(event_id or "").strip()
    invited_at = (
        int(time.time())
        if occurred_at is None
    else _coerce_invite_timestamp(occurred_at, int(time.time()))
    )

    def _add(stats):
        stats = _refresh_invite_statistics(stats)
        invites = stats["invites"]
        if _is_duplicate_invitation(
            invites, inviter_id, invitee_id, event_id, invited_at
        ):
            return stats

        invites.append(
            {
                "inviter_id": inviter_id,
                "invitee_id": invitee_id,
                "invitee_name": invitee_name,
                "invited_at": invited_at,
                "invite_date": _get_local_invite_date(invited_at),
                "event_id": event_id,
                "status": "pending",
            }
        )
        return _refresh_invite_statistics(stats)

    update_group_data(group_id, "invite_stats", _add)


def update_invite_status(group_id: str, invitee_id: str, status: str):
    """Update the relevant invitation record and keep derived counters in sync."""
    invitee_id = str(invitee_id)
    status = str(status or "pending")
    updated_at = int(time.time())

    def _update(stats):
        if not isinstance(stats, dict):
            return stats
        stats = _refresh_invite_statistics(stats)
        matching_invites = [
            invite
            for invite in stats["invites"]
            if isinstance(invite, dict)
            and str(invite.get("invitee_id", "")) == invitee_id
        ]
        target_status = _normalize_invite_status(status)
        if target_status == "left":
            # A departure follows a join, so update that joined record first.
            eligible = [
                invite
                for invite in matching_invites
                if _normalize_invite_status(invite.get("status")) in {"joined", "pending"}
            ]
            invites_to_update = eligible[-1:]
        else:
            # Pending duplicates from older data describe the same unresolved event.
            invites_to_update = [
                invite
                for invite in matching_invites
                if _normalize_invite_status(invite.get("status")) == "pending"
            ]

        for invite in invites_to_update:
            invite["status"] = status
            invite["updated_at"] = updated_at
            if target_status in {"joined", "left"}:
                invite.setdefault("joined_at", updated_at)
        return _refresh_invite_statistics(stats)

    update_group_data(group_id, "invite_stats", _update)


def get_inviter_count(
    group_id: str, inviter_id: str, local_date: str | None = None
) -> int:
    """Return the legacy total, or a local-date total when requested."""
    inviter_id = str(inviter_id)
    stats = get_group_data(group_id, "invite_stats", {})
    records = _get_invite_records(stats)
    inviter_counts, _status_counts, daily_counts = _build_invite_statistics(records)

    if local_date is not None:
        day = daily_counts.get(str(local_date), {})
        return int(day.get("inviter_counts", {}).get(inviter_id, 0))

    if records:
        return int(inviter_counts.get(inviter_id, 0))
    if isinstance(stats, dict):
        counts = stats.get("inviter_counts", {})
        if isinstance(counts, dict):
            return _coerce_invite_timestamp(counts.get(inviter_id, 0))
    return 0


def get_invite_status_summary(group_id: str, local_date: str | None = None) -> dict:
    """Return pending/joined/rejected/left counts for all or one local date."""
    stats = get_group_data(group_id, "invite_stats", {})
    records = _get_invite_records(stats)
    _inviter_counts, status_counts, daily_counts = _build_invite_statistics(records)
    if local_date is not None:
        day = daily_counts.get(str(local_date), {})
        status_counts = day.get("status_counts", _empty_invite_status_counts())
    return {
        status: _coerce_invite_timestamp(status_counts.get(status, 0))
        for status in _INVITE_STATUS_KEYS
    }


def get_daily_invite_statistics(group_id: str, local_date: str | None = None) -> dict:
    """Return one local calendar day's invitation totals and status breakdown."""
    selected_date = str(local_date) if local_date is not None else _get_local_invite_date(time.time())
    stats = get_group_data(group_id, "invite_stats", {})
    records = _get_invite_records(stats)
    _inviter_counts, _status_counts, daily_counts = _build_invite_statistics(records)
    day = daily_counts.get(selected_date, {})
    return {
        "date": selected_date,
        "total": _coerce_invite_timestamp(day.get("total", 0)),
        "inviter_counts": dict(day.get("inviter_counts", {})),
        "status_counts": {
            status: _coerce_invite_timestamp(
                day.get("status_counts", {}).get(status, 0)
            )
            for status in _INVITE_STATUS_KEYS
        },
    }


def get_top_inviter(group_id: str, limit: int = 10) -> list:
    """Rank inviters by successful joins, then total invitations."""
    stats = get_group_data(group_id, "invite_stats", {})
    records = _get_invite_records(stats)
    inviter_counts, _status_counts, _daily_counts = _build_invite_statistics(records)

    if not records and isinstance(stats, dict):
        legacy_counts = stats.get("inviter_counts", {})
        if isinstance(legacy_counts, dict):
            inviter_counts = {
                str(inviter_id): _coerce_invite_timestamp(count)
                for inviter_id, count in legacy_counts.items()
            }

    successful_counts = {}
    current_joined_counts = {}
    for invite in records:
        inviter_id = str(invite.get("inviter_id", ""))
        if _invite_was_successful(invite):
            successful_counts[inviter_id] = successful_counts.get(inviter_id, 0) + 1
        if _normalize_invite_status(invite.get("status")) == "joined":
            current_joined_counts[inviter_id] = (
                current_joined_counts.get(inviter_id, 0) + 1
            )

    sorted_list = sorted(
        inviter_counts.items(),
        key=lambda item: (
            -successful_counts.get(item[0], 0),
            -item[1],
            item[0],
        ),
    )[: max(0, int(limit))]
    return [
        {
            "user_id": inviter_id,
            "count": count,
            "successful_count": successful_counts.get(inviter_id, 0),
            "joined_count": current_joined_counts.get(inviter_id, 0),
        }
        for inviter_id, count in sorted_list
    ]

# ==================== 操作日志 ====================
def add_operation_log(group_id: str, operator_id: str, operator_name: str,
                      action: str, target_id: str = "", target_name: str = "",
                      reason: str = "", before=None, after=None):
    def _add(logs):
        if logs is None:
            logs = []
        entry = {
            "operator_id": operator_id,
            "operator_name": operator_name,
            "action": action,
            "target_id": target_id,
            "target_name": target_name,
            "reason": reason,
            "timestamp": int(time.time())
        }
        if before is not None:
            entry["before"] = before
        if after is not None:
            entry["after"] = after
        logs.append(entry)
        if len(logs) > 500:
            logs = logs[-500:]
        return logs
    
    update_group_data(group_id, "operation_logs", _add)

def get_operation_logs(group_id: str, limit: int = 50) -> list:
    logs = get_group_data(group_id, "operation_logs", [])
    if not isinstance(logs, list):
        return []
    normalized = [entry for entry in logs if isinstance(entry, dict)]
    normalized.sort(
        key=lambda entry: (
            int(entry.get("timestamp", 0) or 0)
            if str(entry.get("timestamp", "")).strip().lstrip("-").isdigit()
            else 0
        ),
        reverse=True,
    )
    return normalized[: max(0, int(limit))]

def clear_operation_logs(group_id: str):
    set_group_data(group_id, "operation_logs", [])

# ==================== 设置相关 ====================
def set_verification_timeout(group_id: str, seconds: int):
    set_group_data(group_id, "verification_timeout", seconds)

def get_verification_timeout(group_id: str) -> int:
    return get_group_data(group_id, "verification_timeout", 300)

def set_max_verify_attempts(group_id: str, attempts: int):
    set_group_data(group_id, "max_attempts", attempts)

def get_max_verify_attempts(group_id: str) -> int:
    return get_group_data(group_id, "max_attempts", 3)

def set_kick_on_verification_failure(group_id: str, enabled: bool):
    set_group_data(group_id, "kick_on_verification_failure", bool(enabled))

def should_kick_on_verification_failure(group_id: str) -> bool:
    root_value = get_group_data(group_id, "kick_on_verification_failure", None)
    feature_value = None
    try:
        from .group_config import get_group_feature_detail
        feature_config = get_group_feature_detail(group_id, "audit")
        if isinstance(feature_config, dict) and "kick_on_verification_failure" in feature_config:
            feature_value = feature_config.get("kick_on_verification_failure")
    except Exception:
        feature_value = None
    value = feature_value if feature_value is not None else root_value
    if value is None:
        value = False
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no", "否", "关闭"}
    return bool(value)

def set_audit_approval_mode(group_id: str, mode: str):
    mode = str(mode or "math").strip().lower()
    if mode not in {"math", "id", "code", "direct", "steamid"}:
        mode = "math"
    set_group_data(group_id, "approval_mode", mode)

def get_audit_approval_mode(group_id: str) -> str:
    mode = str(get_group_data(group_id, "approval_mode", "math") or "math").strip().lower()
    if mode not in {"math", "id", "code", "direct", "steamid"}:
        mode = "math"
        set_group_data(group_id, "approval_mode", mode)
    return mode

def load_audit_pending(group_id: str) -> list:
    return get_group_data(group_id, "audit_pending", [])

def save_audit_pending(group_id: str, pending: list):
    set_group_data(group_id, "audit_pending", pending)

def add_audit_request(group_id: str, user_id: str, user_name: str,
                      join_request: str = "", source: str = "normal",
                      flag: str = "", challenge_info: dict = None,
                      approval_mode: str = None) -> dict:
    return add_pending_request(group_id, user_id, user_name, join_request, source, flag, challenge_info, approval_mode)

def approve_join_request(group_id: str, user_id: str, operator_id: str = "", reason: str = "") -> bool:
    # 先获取 challenge_id，以便清理
    pending = get_group_data(group_id, "audit_pending", [])
    challenge_id = None
    for req in pending:
        if str(req.get("user_id", "")) == str(user_id):
            challenge_id = req.get("challenge_id")
            break
    
    def _approve(pending):
        if pending is None:
            return []
        # 完全移除该用户的待审核记录（而不是只改变状态）
        new_pending = [req for req in pending if str(req.get("user_id", "")) != str(user_id)]
        return new_pending
    update_group_data(group_id, "audit_pending", _approve)
    
    # 同时清理 audit_challenges 中的记录
    if challenge_id:
        remove_challenge(group_id, challenge_id)
    
    add_audit_history(group_id, user_id, "", "approved", operator_id, reason)
    return True

def reject_join_request(group_id: str, user_id: str, operator_id: str = "", reason: str = "") -> bool:
    """拒绝入群请求
    
    Args:
        group_id: 群组ID
        user_id: 用户ID
        operator_id: 操作者ID
        reason: 拒绝原因
    
    Returns:
        bool: 是否成功拒绝
    """
    # 第一步：获取待处理请求，找出关联的验证挑战ID
    pending = get_group_data(group_id, "audit_pending", [])
    challenge_id = None
    
    for req in pending:
        if str(req.get("user_id", "")) == str(user_id) and req.get("status") == "pending":
            challenge_id = req.get("challenge_id")
            break
    
    # 第二步：从待审核列表中完全移除该请求
    def _reject(pending):
        if pending is None:
            return []
        # 完全删除而不是只改状态（这样可以回收存储空间）
        new_pending = [
            req for req in pending 
            if str(req.get("user_id", "")) != str(user_id)
        ]
        return new_pending
    
    update_group_data(group_id, "audit_pending", _reject)
    
    # 第三步：清理关联的验证挑战
    if challenge_id:
        remove_challenge(group_id, challenge_id)
    
    # 第四步：记录到审核历史
    add_audit_history(group_id, user_id, "", "rejected", operator_id, reason)
    
    return True

def get_audit_requests(group_id: str, include_processed: bool = False) -> list:
    return get_pending_requests(group_id, include_processed)

def get_audit_settings(group_id: str) -> dict:
    return {
        "enabled": get_group_data(group_id, "audit_enabled", False),
        "approval_mode": get_audit_approval_mode(group_id),
        "verification_timeout": get_verification_timeout(group_id),
        "max_attempts": get_max_verify_attempts(group_id),
        "auto_approve": get_group_data(group_id, "auto_approve", False)
    }

def set_audit_settings(group_id: str, key: str, value):
    set_group_data(group_id, key, value)

def get_user_audit_history(group_id: str, user_id: str) -> list:
    history = get_group_data(group_id, "audit_history", [])
    return [h for h in history if h.get("user_id") == user_id]

def set_auto_approve(group_id: str, enabled: bool):
    set_group_data(group_id, "auto_approve", enabled)

def get_audit_statistics(group_id: str) -> dict:
    pending = get_pending_requests(group_id)
    history = get_group_data(group_id, "audit_history", [])
    approved = len([h for h in history if h.get("action") == "approved"])
    rejected = len([h for h in history if h.get("action") == "rejected"])
    return {
        "pending_count": len(pending),
        "total_processed": len(history),
        "approved": approved,
        "rejected": rejected
    }

def get_verification_settings(group_id: str) -> dict:
    return {
        "timeout": get_verification_timeout(group_id),
        "max_attempts": get_max_verify_attempts(group_id),
        "approval_mode": get_audit_approval_mode(group_id)
    }

def set_verification_settings(group_id: str, key: str, value):
    if key == "timeout":
        set_verification_timeout(group_id, value)
    elif key == "max_attempts":
        set_max_verify_attempts(group_id, value)
    elif key == "approval_mode":
        set_audit_approval_mode(group_id, value)

def verify_answer(
    group_id: str,
    challenge_id: str,
    answer: str,
    max_attempts: int = 3,
) -> tuple:
    """Verify a math/ID answer using the caller's configured retry limit.

    Keep the small compatibility wrapper in sync with
    ``verify_challenge_answer``. Older callers may still omit the fourth
    argument, while the Windows admission handler passes the per-group limit.
    """
    return verify_challenge_answer(
        group_id,
        challenge_id,
        answer,
        max_attempts=max_attempts,
    )

def get_pending_verification(group_id: str, user_id: str) -> dict:
    pending = get_group_data(group_id, "audit_pending", [])
    for req in pending:
        if str(req.get("user_id", "")) == str(user_id) and req.get("status") == "pending":
            challenge_id = req.get("challenge_id")
            if challenge_id:
                challenges = get_group_data(group_id, "audit_challenges", {})
                challenge = challenges.get(challenge_id, {})
                req.update(challenge)
            return req
    
    challenges = get_group_data(group_id, "audit_challenges", {})
    for challenge_id, challenge in challenges.items():
        if str(challenge.get("user_id", "")) == str(user_id):
            return challenge
    return {}


def restore_pending_challenge(group_id: str, pending: dict) -> bool:
    """Restore a challenge removed by a duplicate join event.

    Pending rows retain the answer shown in the prompt. If an older duplicate
    join event removed only ``audit_challenges`` after the prompt was sent,
    recreating the small challenge record lets the member's already-correct
    reply complete verification instead of being reported as invalid.
    """

    if not isinstance(pending, dict):
        return False
    challenge_id = str(pending.get("challenge_id", "") or "").strip()
    user_id = str(pending.get("user_id", "") or "").strip()
    mode = str(pending.get("approval_mode", "") or "").strip().lower()
    if not challenge_id or not user_id or mode not in {"math", "id"}:
        return False
    expected_key = "answer" if mode == "math" else "verify_id"
    expected = str(pending.get(expected_key, "") or "").strip()
    if not expected:
        return False
    current = get_group_data(group_id, "audit_challenges", {})
    if isinstance(current, dict) and isinstance(current.get(challenge_id), dict):
        return False

    def _restore(challenges):
        challenges = dict(challenges) if isinstance(challenges, dict) else {}
        if isinstance(challenges.get(challenge_id), dict):
            return challenges
        restored = {
            "challenge_id": challenge_id,
            "group_id": str(group_id),
            "user_id": user_id,
            "created_at": int(pending.get("created_at", 0) or pending.get("requested_at", 0) or time.time()),
            "attempts": max(0, int(pending.get("attempts", 0) or 0)),
        }
        if mode == "math":
            restored["answer"] = expected
            restored["question"] = str(
                pending.get("verification_question", "")
                or pending.get("question", "")
            )
        else:
            restored["verify_id"] = expected
        challenges[challenge_id] = restored
        return challenges

    update_group_data(group_id, "audit_challenges", _restore)
    return True

def is_verification_expired(group_id: str, user_id: str) -> bool:
    pending = get_pending_verification(group_id, user_id)
    if not pending:
        return True
    timeout = get_verification_timeout(group_id)
    # A challenge can be created while join-event processing is still doing
    # network lookups.  Expiry must begin when the member could actually see
    # the prompt, with legacy records falling back to their request time.
    created_at = (
        pending.get("prompt_sent_at")
        or pending.get("verification_prompt_sent_at")
        or pending.get("steamid_prompt_sent_at")
        or pending.get("requested_at")
        or pending.get("created_at", 0)
    )
    try:
        created_at = int(created_at or 0)
    except (TypeError, ValueError, OverflowError):
        created_at = 0
    return created_at <= 0 or int(time.time()) - created_at > timeout

def clean_expired_challenges(group_id: str) -> int:
    challenges = get_group_data(group_id, "audit_challenges", {})
    if not challenges:
        return 0
    timeout = get_verification_timeout(group_id)
    expired = []
    for challenge_id, challenge in challenges.items():
        if int(time.time()) - challenge.get("created_at", 0) > timeout:
            expired.append(challenge_id)
    if expired:
        def _clean(challenges):
            for cid in expired:
                if cid in challenges:
                    del challenges[cid]
            return challenges
        update_group_data(group_id, "audit_challenges", _clean)
    return len(expired)

def clean_expired_challenges_and_kick(group_id: str) -> list:
    """清理过期挑战并返回按分群策略需要踢出的用户。
    
    超时处理流程：
    1. 检查 audit_challenges 中已过期的验证
    2. 检查 audit_pending 中已过期的请求
    3. 清理过期的验证数据
    4. 记录审核历史（便于后续统计分析）
    5. 仅在启用“验证失败后踢出成员”时返回踢人列表
    
    Returns:
        list[dict]: 需踢出的用户列表 [{'group_id':str, 'user_id':str, 'reason':str}, ...]
    """
    challenges = get_group_data(group_id, "audit_challenges", {})
    timeout = get_verification_timeout(group_id)
    now = int(time.time())
    
    expired_user_ids = []
    expired_challenge_ids = []

    # A challenge can outlive its request after an older build crashed between
    # the two writes. Such an orphan is safe to delete, but it must never be
    # used as evidence for kicking a current group member. Only a challenge
    # still referenced by the same pending request may expire into a kick.
    pending = get_pending_requests(group_id)
    pending_by_challenge = {
        str(req.get("challenge_id", "")): req
        for req in pending
        if req.get("status", "pending") == "pending"
        and str(req.get("challenge_id", ""))
    }
    
    if challenges:
        for challenge_id, challenge in challenges.items():
            if now - challenge.get("created_at", 0) > timeout:
                expired_challenge_ids.append(challenge_id)
                challenge_key = str(challenge_id or "")
                request = pending_by_challenge.get(challenge_key)
                if request:
                    # The request timestamp is authoritative. A re-join can
                    # rearm the request while retaining an old challenge row;
                    # using challenge.created_at here would kick the member
                    # immediately after the fresh join.
                    try:
                        request_age = now - int(
                            request.get("prompt_sent_at")
                            or request.get("verification_prompt_sent_at")
                            or request.get("steamid_prompt_sent_at")
                            or request.get("requested_at", 0)
                            or 0
                        )
                    except (TypeError, ValueError, OverflowError):
                        request_age = timeout + 1
                    if request_age <= timeout:
                        expired_challenge_ids.remove(challenge_id)
    
    # 再检查 audit_pending 中是否有请求过期（可能challenge被意外删除但pending还在）
    undelivered_user_ids = []
    undelivered_challenge_ids = []
    for req in pending:
        if req.get("status") != "pending":
            continue
        if not (
            bool(req.get("verification_prompt_sent", False))
            or bool(req.get("steamid_prompt_sent", False))
        ):
            # A failed send must never cause a kick or a second verification.
            # Drop the unusable local audit row and challenge only.
            uid = str(req.get("user_id", ""))
            if uid:
                undelivered_user_ids.append(uid)
            challenge_id = str(req.get("challenge_id", "") or "")
            if challenge_id:
                undelivered_challenge_ids.append(challenge_id)
            continue
        try:
            request_started_at = int(
                req.get("prompt_sent_at")
                or req.get("verification_prompt_sent_at")
                or req.get("steamid_prompt_sent_at")
                or req.get("requested_at", 0)
                or 0
            )
        except (TypeError, ValueError, OverflowError):
            request_started_at = 0
        if request_started_at and now - request_started_at > timeout:
            uid = str(req.get("user_id", ""))
            if uid and uid not in expired_user_ids:
                expired_user_ids.append(uid)
    
    # Only challenges belonging to a delivered prompt may be removed as part
    # of timeout processing. An unsent prompt must remain retryable.
    expired_challenge_ids = [
        challenge_id
        for challenge_id in expired_challenge_ids
        if (
            pending_by_challenge.get(str(challenge_id), {}).get(
                "verification_prompt_sent", False
            )
            or pending_by_challenge.get(str(challenge_id), {}).get(
                "steamid_prompt_sent", False
            )
        )
    ]

    if undelivered_user_ids:
        update_group_data(
            group_id,
            "audit_pending",
            lambda rows: [
                row
                for row in (rows or [])
                if str(row.get("user_id", "")) not in set(undelivered_user_ids)
            ],
        )
        for challenge_id in undelivered_challenge_ids:
            remove_challenge(group_id, challenge_id)

    # 1. 清除过期challenge
    if expired_challenge_ids:
        def _clean_challenges(c):
            if not c:
                return c
            for cid in expired_challenge_ids:
                if cid in c:
                    del c[cid]
            return c
        update_group_data(group_id, "audit_challenges", _clean_challenges)
    
    # 2. 拒绝所有过期用户的待验证请求 + 清理pending记录
    kick_list = []
    kick_on_failure = should_kick_on_verification_failure(group_id)
    for uid in expired_user_ids:
        try:
            # 拒绝请求
            reject_join_request(group_id, uid, "system", "验证超时自动拒绝")
            
            # 添加操作日志（用于管理员审计和数据分析）
            add_operation_log(
                group_id=group_id,
                operator_id="system",
                operator_name="自动系统",
                action="验证超时",
                target_id=uid,
                reason="验证超时"
            )
            update_invite_status(group_id, uid, "timeout_rejected")
            if kick_on_failure:
                kick_list.append({
                    "group_id": group_id,
                    "user_id": uid,
                    "reason": "验证超时未通过"
                })
        except Exception as e:
            logger.error(f"处理超时请求失败 group={group_id} user={uid}: {e}")
    
    return kick_list

def get_verification_stats(group_id: str) -> dict:
    challenges = get_group_data(group_id, "audit_challenges", {})
    return {"active_challenges": len(challenges)}

def check_expired_audit_requests(group_id: str) -> list:
    pending = get_pending_requests(group_id)
    timeout = get_verification_timeout(group_id)
    expired = []
    for req in pending:
        if int(time.time()) - req.get("requested_at", 0) > timeout:
            expired.append(req)
            reject_join_request(group_id, req.get("user_id"), "system", "验证超时")
    return expired

def set_audit_timeout(group_id: str, seconds: int):
    set_verification_timeout(group_id, seconds)

def get_audit_timeout(group_id: str) -> int:
    return get_verification_timeout(group_id)

def check_excessive_invites(
    group_id: str,
    inviter_id: str,
    max_invites: int = 5,
    now: int | None = None,
) -> bool:
    """Return whether this inviter exceeds the threshold on the local day."""
    local_date = _get_local_invite_date(time.time() if now is None else now)
    count = get_inviter_count(group_id, inviter_id, local_date=local_date)
    return count > max_invites


def get_all_excessive_inviter_stats(
    group_id: str, max_invites: int = 5, now: int | None = None
) -> list:
    """Return inviters whose invitations exceed today's local-date threshold."""
    local_date = _get_local_invite_date(time.time() if now is None else now)
    stats = get_group_data(group_id, "invite_stats", {})
    invites = [
        invite
        for invite in _get_invite_records(stats)
        if _get_invite_date(invite) == local_date
    ]
    inviter_counts, _status_counts, _daily_counts = _build_invite_statistics(invites)

    result = []
    for inviter_id, count in inviter_counts.items():
        if count > max_invites:
            inviter_invites = [
                invite
                for invite in invites
                if str(invite.get("inviter_id", "")) == inviter_id
            ]
            result.append(
                {
                    "user_id": inviter_id,
                    "count": count,
                    "invitees": inviter_invites,
                    "date": local_date,
                }
            )
    return sorted(result, key=lambda item: (-item["count"], item["user_id"]))

def get_invited_users_by_inviter(group_id: str, inviter_id: str) -> list:
    stats = get_group_data(group_id, "invite_stats", {})
    inviter_id = str(inviter_id)
    return [
        invite
        for invite in _get_invite_records(stats)
        if str(invite.get("inviter_id", "")) == inviter_id
    ]

def get_inviter_of_user(group_id: str, user_id: str) -> str:
    stats = get_group_data(group_id, "invite_stats", {})
    user_id = str(user_id)
    for invite in reversed(_get_invite_records(stats)):
        if str(invite.get("invitee_id")) == str(user_id):
            return str(invite.get("inviter_id", ""))
    return ""

def remove_inviter_data(group_id: str, inviter_id: str):
    inviter_id = str(inviter_id)

    def _remove(stats):
        if not isinstance(stats, dict):
            return stats
        stats = _refresh_invite_statistics(stats)
        stats["invites"] = [
            invite
            for invite in stats["invites"]
            if not isinstance(invite, dict)
            or str(invite.get("inviter_id", "")) != inviter_id
        ]
        return _refresh_invite_statistics(stats)

    update_group_data(group_id, "invite_stats", _remove)

def load_invite_stats(group_id: str) -> dict:
    stats = get_group_data(group_id, "invite_stats", {})
    return {group_id: stats} if isinstance(stats, dict) and stats else {}

def clear_invite_stats(group_id: str) -> int:
    stats = get_group_data(group_id, "invite_stats", {})
    invite_count = len(_get_invite_records(stats))
    set_group_data(group_id, "invite_stats", {})
    return invite_count

def get_operation_logs_by_operator(group_id: str, operator_id: str, limit: int = 50) -> list:
    logs = get_operation_logs(group_id, limit=500)
    return [l for l in logs if l.get("operator_id") == operator_id][: max(0, int(limit))]

def get_operation_logs_by_action(group_id: str, action: str, limit: int = 50) -> list:
    logs = get_operation_logs(group_id, limit=500)
    return [l for l in logs if l.get("action") == action][: max(0, int(limit))]

def get_operation_logs_by_time(group_id: str, start_time: int, end_time: int) -> list:
    logs = get_group_data(group_id, "operation_logs", [])
    return [l for l in logs if start_time <= l.get("timestamp", 0) <= end_time]

def get_operation_stats(group_id: str) -> dict:
    logs = get_group_data(group_id, "operation_logs", [])
    actions = {}
    for log in logs:
        action = log.get("action", "")
        actions[action] = actions.get(action, 0) + 1
    return {"total_actions": len(logs), "action_counts": actions}

def get_pending_verifications() -> list:
    from .group_config import list_all_groups, get_group_data
    all_groups = list_all_groups()
    result = []
    
    for group_id in all_groups:
        pending = get_group_data(group_id, "audit_pending", [])
        for req in pending:
            if req.get("status") == "pending":
                result.append({
                    "group_id": group_id,
                    "user_id": req.get("user_id"),
                    "user_name": req.get("user_name", ""),
                    "requested_at": req.get("requested_at", 0),
                    "challenge_id": req.get("challenge_id"),
                    "approval_mode": req.get("approval_mode", ""),
                    "platform_id": req.get("platform_id", ""),
                    "bot_account_id": req.get("bot_account_id", ""),
                    "attempts": req.get("attempts", 0),
                    "verification_prompt_sent": bool(
                        req.get("verification_prompt_sent", False)
                    ),
                    "steamid_prompt_sent": bool(
                        req.get("steamid_prompt_sent", False)
                    ),
                    "prompt_sent_at": req.get("prompt_sent_at", 0),
                    "verification_prompt_sent_at": req.get(
                        "verification_prompt_sent_at", 0
                    ),
                    "steamid_prompt_sent_at": req.get("steamid_prompt_sent_at", 0),
                })
    
    return result


def clear_legacy_completed_math_verifications() -> list:
    """Clear rows older builds left after math/ID verification succeeded."""
    from .group_config import list_all_groups

    recovered = []
    for group_id in list_all_groups():
        group_key = str(group_id)
        pending = get_group_data(group_key, "audit_pending", [])
        legacy = [
            request
            for request in (pending if isinstance(pending, list) else [])
            if request.get("status", "pending") == "pending"
            and str(request.get("approval_mode", "") or "").lower() in {"math", "id"}
            and bool(request.get("steamid_stage", False))
            and not bool(request.get("steamid_fallback", False))
        ]
        if not legacy:
            continue

        legacy_users = {str(request.get("user_id", "")) for request in legacy}
        challenge_ids = {
            str(request.get("challenge_id", ""))
            for request in legacy
            if request.get("challenge_id")
        }

        def _remove_legacy(rows):
            return [
                request
                for request in (rows if isinstance(rows, list) else [])
                if not (
                    str(request.get("user_id", "")) in legacy_users
                    and request.get("status", "pending") == "pending"
                    and str(request.get("approval_mode", "") or "").lower() in {"math", "id"}
                    and bool(request.get("steamid_stage", False))
                    and not bool(request.get("steamid_fallback", False))
                )
            ]

        update_group_data(group_key, "audit_pending", _remove_legacy)
        if challenge_ids:
            update_group_data(
                group_key,
                "audit_challenges",
                lambda challenges: {
                    key: value
                    for key, value in (challenges or {}).items()
                    if str(key) not in challenge_ids
                },
            )

        for request in legacy:
            user_id = str(request.get("user_id", ""))
            add_audit_history(
                group_key,
                user_id,
                str(request.get("user_name", "") or ""),
                "approved",
                "system",
                "旧版数学/ID验证已通过，清理错误的 SteamID 待验证状态",
            )
            update_invite_status(group_key, user_id, "joined")
            recovered.append({"group_id": group_key, "user_id": user_id})

    return recovered
