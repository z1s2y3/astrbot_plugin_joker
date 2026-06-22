import re
import time
import random
from .group_config import get_group_data, set_group_data, update_group_data

# 回复数据
def get_group_replies(group_id: str) -> dict:
    return get_group_data(group_id, "replies", {})

def add_reply(group_id: str, keyword: str, reply: str, exact: bool = True,
              random_replies: list = None, regex_pattern: str = None) -> bool:
    def _add(replies):
        if replies is None:
            replies = {"exact": {}, "fuzzy": [], "regex": []}
        
        reply_data = {
            "reply": reply,
            "type": "exact" if exact else "fuzzy",
            "created_at": int(time.time())
        }
        
        if random_replies:
            reply_data["random_replies"] = random_replies
        
        if exact:
            replies["exact"][keyword] = reply_data
        else:
            if keyword not in replies["fuzzy"]:
                replies["fuzzy"].append(keyword)
            if "fuzzy_keywords" not in replies:
                replies["fuzzy_keywords"] = {}
            replies["fuzzy_keywords"][keyword] = reply_data
        
        return replies
    
    update_group_data(group_id, "replies", _add)
    return True

def add_regex_reply(group_id: str, pattern: str, reply: str) -> bool:
    try:
        re.compile(pattern)
    except re.error:
        return False
    
    def _add(replies):
        if replies is None:
            replies = {"exact": {}, "fuzzy": [], "regex": []}
        
        replies["regex"].append({
            "pattern": pattern,
            "reply": {
                "reply": reply,
                "type": "regex",
                "created_at": int(time.time())
            }
        })
        return replies
    
    update_group_data(group_id, "replies", _add)
    return True

def remove_reply(group_id: str, keyword: str, reply_type: str = "exact") -> bool:
    def _remove(replies):
        if replies is None:
            return {"exact": {}, "fuzzy": [], "regex": []}
        
        if reply_type == "exact":
            replies["exact"].pop(keyword, None)
        elif reply_type == "fuzzy":
            if keyword in replies.get("fuzzy", []):
                replies["fuzzy"].remove(keyword)
            if "fuzzy_keywords" in replies:
                replies["fuzzy_keywords"].pop(keyword, None)
        elif reply_type == "regex":
            replies["regex"] = [r for r in replies.get("regex", []) if r.get("pattern") != keyword]
        
        return replies
    
    update_group_data(group_id, "replies", _remove)
    return True

def get_reply(group_id: str, keyword: str) -> str:
    replies = get_group_replies(group_id)
    
    exact_replies = replies.get("exact", {})
    if keyword in exact_replies:
        reply_data = exact_replies[keyword]
        if "random_replies" in reply_data:
            return random.choice(reply_data["random_replies"])
        return reply_data.get("reply")
    
    fuzzy_keywords = replies.get("fuzzy_keywords", {})
    for fuzzy_kw in replies.get("fuzzy", []):
        if fuzzy_kw in keyword and fuzzy_kw in fuzzy_keywords:
            reply_data = fuzzy_keywords[fuzzy_kw]
            if "random_replies" in reply_data:
                return random.choice(reply_data["random_replies"])
            return reply_data.get("reply")
    
    for regex_entry in replies.get("regex", []):
        pattern = regex_entry.get("pattern", "")
        try:
            if re.search(pattern, keyword):
                reply_data = regex_entry.get("reply", {})
                if "random_replies" in reply_data:
                    return random.choice(reply_data["random_replies"])
                return reply_data.get("reply")
        except re.error:
            continue
    
    return None

def match_any_reply(group_id: str, message: str) -> tuple:
    replies = get_group_replies(group_id)
    
    exact_replies = replies.get("exact", {})
    if message in exact_replies:
        return message, exact_replies[message]
    
    fuzzy_keywords = replies.get("fuzzy_keywords", {})
    for fuzzy_kw in replies.get("fuzzy", []):
        if fuzzy_kw in message and fuzzy_kw in fuzzy_keywords:
            return fuzzy_kw, fuzzy_keywords[fuzzy_kw]
    
    for regex_entry in replies.get("regex", []):
        pattern = regex_entry.get("pattern", "")
        try:
            match = re.search(pattern, message)
            if match:
                reply_data = regex_entry.get("reply", {})
                return match.group(0), reply_data
        except re.error:
            continue
    
    return None, None

def list_replies(group_id: str) -> dict:
    replies = get_group_replies(group_id)
    return {
        "exact": list(replies.get("exact", {}).keys()),
        "fuzzy": replies.get("fuzzy", []),
        "regex": [r.get("pattern") for r in replies.get("regex", [])],
        "cooldown": replies.get("cooldown", 0)
    }

def get_reply_stats(group_id: str) -> dict:
    replies = get_group_replies(group_id)
    exact = len(replies.get("exact", {}))
    fuzzy = len(replies.get("fuzzy", []))
    regex = len(replies.get("regex", []))
    
    return {
        "total": exact + fuzzy + regex,
        "exact": exact,
        "fuzzy": fuzzy,
        "regex": regex
    }

def clear_replies(group_id: str):
    set_group_data(group_id, "replies", {"exact": {}, "fuzzy": [], "regex": []})

# 冷却时间
def set_reply_cooldown(group_id: str, seconds: int):
    def _update(replies):
        if replies is None:
            replies = {"exact": {}, "fuzzy": [], "regex": [], "cooldown": seconds}
        else:
            replies["cooldown"] = seconds
        return replies
    update_group_data(group_id, "replies", _update)

def get_reply_cooldown(group_id: str) -> int:
    replies = get_group_replies(group_id)
    return replies.get("cooldown", 0)

def is_in_cooldown(group_id: str, keyword: str) -> bool:
    cooldown_data = get_group_data(group_id, "cooldown_cache", {})
    key = f"{group_id}:{keyword}"
    
    cd_seconds = get_reply_cooldown(group_id)
    if cd_seconds == 0:
        return False
    
    last_time = cooldown_data.get(key, 0)
    if int(time.time()) - last_time < cd_seconds:
        return True
    
    # 清理
    def _clean(data):
        if data and key in data:
            del data[key]
        return data
    update_group_data(group_id, "cooldown_cache", _clean)
    return False

def set_cooldown(group_id: str, keyword: str):
    def _update(cooldown_data):
        if cooldown_data is None:
            cooldown_data = {}
        key = f"{group_id}:{keyword}"
        cooldown_data[key] = int(time.time())
        return cooldown_data
    update_group_data(group_id, "cooldown_cache", _update)

def load_replies(group_id: str) -> dict:
    return get_group_replies(group_id)

def save_replies(group_id: str, replies: dict):
    set_group_data(group_id, "replies", replies)

def add_random_replies(group_id: str, keyword: str, replies: list) -> bool:
    def _add(r):
        if r is None:
            r = {"exact": {}, "fuzzy": [], "regex": []}
        r["exact"][keyword] = {
            "reply": replies[0],
            "type": "exact",
            "random_replies": replies,
            "created_at": int(time.time())
        }
        return r
    update_group_data(group_id, "replies", _add)
    return True

def export_replies(group_id: str) -> dict:
    return get_group_replies(group_id)

def import_replies(group_id: str, data: dict):
    set_group_data(group_id, "replies", data)
