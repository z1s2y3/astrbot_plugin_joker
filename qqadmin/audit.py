import json
import time
import random
from .group_config import get_group_data, set_group_data, update_group_data, get_group_feature_setting

_global_config = {}

def set_global_config(config: dict):
    global _global_config
    _global_config = config

def get_audit_group_config(group_id: str, setting: str, default=None):
    value = get_group_feature_setting(group_id, "audit", setting, global_config=_global_config)
    if value is not None:
        return value
    return default

def generate_approval_code() -> str:
    return str(random.randint(1000, 9999))

def add_pending_request(group_id: str, user_id: str, user_name: str,
                       join_request: str = "", source: str = "normal",
                       flag: str = "", challenge_info: dict = None,
                       approval_mode: str = None) -> dict:
    if approval_mode is None:
        approval_mode = get_audit_approval_mode(group_id)
    
    if challenge_info is None:
        if approval_mode == "math":
            challenge_info = create_math_challenge(group_id, user_id)
        elif approval_mode == "id":
            challenge_info = create_id_challenge(group_id, user_id)

    approval_code = generate_approval_code()

    def _add(pending):
        if pending is None:
            pending = []
        
        for req in pending:
            if str(req.get("user_id")) == str(user_id) and str(req.get("status")) == "pending":
                return pending
        
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
            "approval_code": approval_code,
            "flag": flag,
            "attempts": 0
        })
        return pending
    
    update_group_data(group_id, "audit_pending", _add)
    return {"success": True, "approval_code": approval_code, "challenge_info": challenge_info or {}}

def remove_pending_request(group_id: str, user_id: str) -> bool:
    def _remove(pending):
        if pending is None:
            return []
        return [r for r in pending if r.get("user_id") != user_id]
    
    update_group_data(group_id, "audit_pending", _remove)
    return True

def increment_pending_attempts(group_id: str, user_id: str) -> int:
    def _increment(pending):
        if pending is None:
            return []
        for req in pending:
            if req.get("user_id") == user_id:
                req["attempts"] = req.get("attempts", 0) + 1
        return pending
    
    pending = update_group_data(group_id, "audit_pending", _increment)
    for req in pending:
        if req.get("user_id") == user_id:
            return req.get("attempts", 0)
    return 0

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

    challenge_id = f"{group_id}_{user_id}_{int(time.time())}"
    
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
    return {"challenge_id": challenge_id, "question": question}

def create_id_challenge(group_id: str, user_id: str) -> dict:
    challenge_id = f"{group_id}_{user_id}_{int(time.time())}"
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
    challenges = get_group_data(group_id, "audit_challenges", {})
    if not challenges or challenge_id not in challenges:
        return False, True
    
    challenge = challenges[challenge_id]
    challenge["attempts"] += 1
    
    if "answer" in challenge:
        correct = user_answer.strip() == challenge["answer"]
    else:
        correct = user_answer.strip() == challenge.get("verify_id", "")
    
    if correct or challenge["attempts"] >= max_attempts:
        remove_challenge(group_id, challenge_id)
        return correct, not correct
    
    return correct, False

def get_challenge_answer(group_id: str, challenge_id: str) -> str:
    challenges = get_group_data(group_id, "audit_challenges", {})
    if challenges and challenge_id in challenges:
        challenge = challenges[challenge_id]
        if "answer" in challenge:
            return challenge["answer"]
        return challenge.get("verify_id", "")
    return ""

# ==================== 邀请统计 ====================
def record_invitation(group_id: str, inviter_id: str, invitee_id: str, invitee_name: str = ""):
    def _add(stats):
        if stats is None:
            stats = {"invites": [], "inviter_counts": {}}
        
        stats["invites"].append({
            "inviter_id": inviter_id,
            "invitee_id": invitee_id,
            "invitee_name": invitee_name,
            "invited_at": int(time.time()),
            "status": "pending"
        })
        stats["inviter_counts"][inviter_id] = stats["inviter_counts"].get(inviter_id, 0) + 1
        return stats
    
    update_group_data(group_id, "invite_stats", _add)

def update_invite_status(group_id: str, invitee_id: str, status: str):
    def _update(stats):
        if stats is None:
            return stats
        for invite in stats.get("invites", []):
            if invite.get("invitee_id") == invitee_id and invite.get("status") == "pending":
                invite["status"] = status
                invite["updated_at"] = int(time.time())
        return stats
    
    update_group_data(group_id, "invite_stats", _update)

def get_inviter_count(group_id: str, inviter_id: str) -> int:
    stats = get_group_data(group_id, "invite_stats", {})
    return stats.get("inviter_counts", {}).get(inviter_id, 0)

def get_top_inviter(group_id: str, limit: int = 10) -> list:
    stats = get_group_data(group_id, "invite_stats", {})
    counts = stats.get("inviter_counts", {})
    sorted_list = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    return [{"user_id": uid, "count": cnt} for uid, cnt in sorted_list]

# ==================== 操作日志 ====================
def add_operation_log(group_id: str, operator_id: str, operator_name: str,
                      action: str, target_id: str = "", target_name: str = "",
                      reason: str = ""):
    def _add(logs):
        if logs is None:
            logs = []
        logs.append({
            "operator_id": operator_id,
            "operator_name": operator_name,
            "action": action,
            "target_id": target_id,
            "target_name": target_name,
            "reason": reason,
            "timestamp": int(time.time())
        })
        if len(logs) > 500:
            logs = logs[-500:]
        return logs
    
    update_group_data(group_id, "operation_logs", _add)

def get_operation_logs(group_id: str, limit: int = 50) -> list:
    logs = get_group_data(group_id, "operation_logs", [])
    return logs[-limit:] if logs else []

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

def set_audit_approval_mode(group_id: str, mode: str):
    set_group_data(group_id, "approval_mode", mode)

def get_audit_approval_mode(group_id: str) -> str:
    return get_group_data(group_id, "approval_mode", "direct")

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
    def _reject(pending):
        if pending is None:
            return []
        for req in pending:
            if req.get("user_id") == user_id:
                req["status"] = "rejected"
                req["processed_at"] = int(time.time())
                req["operator_id"] = operator_id
                req["reason"] = reason
        return pending
    update_group_data(group_id, "audit_pending", _reject)
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

def verify_answer(group_id: str, challenge_id: str, answer: str) -> tuple:
    return verify_challenge_answer(group_id, challenge_id, answer)

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

def is_verification_expired(group_id: str, user_id: str) -> bool:
    challenge = get_pending_verification(group_id, user_id)
    if not challenge:
        return True
    timeout = get_verification_timeout(group_id)
    created_at = challenge.get("created_at", 0)
    return int(time.time()) - created_at > timeout

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

def clean_expired_challenges_and_kick(group_id: str):
    clean_expired_challenges(group_id)

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

def check_excessive_invites(group_id: str, inviter_id: str, max_invites: int = 5) -> bool:
    count = get_inviter_count(group_id, inviter_id)
    return count > max_invites

def get_all_excessive_inviter_stats(group_id: str, max_invites: int = 5) -> list:
    """获取所有超过阈值的邀请者"""
    stats = get_group_data(group_id, "invite_stats", {})
    inviters = stats.get("inviter_counts", {})
    invites = stats.get("invites", [])
    
    result = []
    for inviter_id, count in inviters.items():
        if count > max_invites:
            # 获取该邀请者的所有邀请记录
            inviter_invites = [i for i in invites if str(i.get("inviter_id")) == str(inviter_id)]
            result.append({
                "user_id": inviter_id,
                "count": count,
                "invitees": inviter_invites
            })
    return result

def get_invited_users_by_inviter(group_id: str, inviter_id: str) -> list:
    stats = get_group_data(group_id, "invite_stats", {})
    return [i for i in stats.get("invites", []) if i.get("inviter_id") == inviter_id]

def get_inviter_of_user(group_id: str, user_id: str) -> str:
    stats = get_group_data(group_id, "invite_stats", {})
    for invite in stats.get("invites", []):
        if str(invite.get("invitee_id")) == str(user_id):
            return str(invite.get("inviter_id", ""))
    return ""

def remove_inviter_data(group_id: str, inviter_id: str):
    def _remove(stats):
        if stats is None:
            return stats
        stats["invites"] = [i for i in stats.get("invites", []) if i.get("inviter_id") != inviter_id]
        if "inviter_counts" in stats and inviter_id in stats["inviter_counts"]:
            del stats["inviter_counts"][inviter_id]
        return stats
    update_group_data(group_id, "invite_stats", _remove)

def load_invite_stats(group_id: str) -> dict:
    stats = get_group_data(group_id, "invite_stats", {})
    return {group_id: stats} if stats else {}

def clear_invite_stats(group_id: str) -> int:
    stats = get_group_data(group_id, "invite_stats", {})
    invite_count = len(stats.get("invites", []))
    set_group_data(group_id, "invite_stats", {})
    return invite_count

def get_operation_logs_by_operator(group_id: str, operator_id: str, limit: int = 50) -> list:
    logs = get_group_data(group_id, "operation_logs", [])
    return [l for l in logs if l.get("operator_id") == operator_id][-limit:]

def get_operation_logs_by_action(group_id: str, action: str, limit: int = 50) -> list:
    logs = get_group_data(group_id, "operation_logs", [])
    return [l for l in logs if l.get("action") == action][-limit:]

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
                    "requested_at": req.get("requested_at", 0),
                    "challenge_id": req.get("challenge_id"),
                    "attempts": req.get("attempts", 0)
                })
    
    return result