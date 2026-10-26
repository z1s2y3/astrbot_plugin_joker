import random
import re
from typing import Dict, List, Optional
from .group_config import get_group_data, set_group_data, update_group_data, _config_cache

def _normalize_user_id(user_id: str) -> str:
    """标准化用户ID"""
    cq_match = re.search(r'\[CQ:at,qq=(\d+)\]', user_id)
    if cq_match:
        return cq_match.group(1)
    at_match = re.search(r'@.+?\((\d+)\)', user_id)
    if at_match:
        return at_match.group(1)
    if user_id.isdigit():
        return user_id
    return user_id

def load_player_replies(group_id: str) -> Dict[str, dict]:
    """加载群组的用户专属回复"""
    return get_group_data(group_id, "player_replies", {})

def save_player_replies(group_id: str, data: Dict[str, dict]):
    """保存群组的用户专属回复"""
    set_group_data(group_id, "player_replies", data)

def add_player_reply(group_id: str, user_id: str, messages: List[str], at_user: bool = True, enabled: bool = True) -> bool:
    """添加用户专属回复"""
    normalized_id = _normalize_user_id(user_id)
    
    def _add(data):
        if data is None:
            data = {}
        data[normalized_id] = {
            'messages': messages,
            'at_user': at_user,
            'enabled': enabled
        }
        return data
    
    update_group_data(group_id, "player_replies", _add)
    _config_cache.pop(group_id, None)
    return True

def remove_player_reply(group_id: str, user_id: str) -> bool:
    """移除用户专属回复"""
    normalized_id = _normalize_user_id(user_id)
    
    def _remove(data):
        if data is None:
            return {}
        if normalized_id in data:
            del data[normalized_id]
        return data
    
    update_group_data(group_id, "player_replies", _remove)
    # 清除缓存，确保下次读取时重新加载
    from .group_config import _config_cache
    _config_cache.pop(group_id, None)
    return True

def remove_player_reply_message(group_id: str, user_id: str, index: int) -> bool:
    """删除用户的指定序号的回复消息（序号从1开始）"""
    normalized_id = _normalize_user_id(user_id)

    def _remove_message(data):
        if data is None or normalized_id not in data:
            return data
        messages = data[normalized_id].get('messages', [])
        if 1 <= index <= len(messages):
            messages.pop(index - 1)
            data[normalized_id]['messages'] = messages
            # 如果消息列表为空，删除整个用户配置
            if not messages:
                del data[normalized_id]
        return data

    update_group_data(group_id, "player_replies", _remove_message)
    # 清除缓存
    from .group_config import _config_cache
    _config_cache.pop(group_id, None)
    return True

def get_player_reply(group_id: str, user_id: str) -> Optional[dict]:
    """获取用户专属回复"""
    replies = get_player_replies_for_user(group_id, user_id)
    return replies[0] if replies else None


def _normalise_reply_record(value: object) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    messages = value.get("messages", value.get("replies", []))
    if isinstance(messages, str):
        messages = [messages]
    if not isinstance(messages, (list, tuple)):
        messages = []
    normalised_messages = [str(message).strip() for message in messages if str(message).strip()]
    if not normalised_messages:
        return None
    return {
        "messages": normalised_messages,
        "at_user": bool(value.get("at_user", value.get("at", True))),
        "enabled": bool(value.get("enabled", True)),
    }


def get_player_replies_for_user(group_id: str, user_id: str) -> List[dict]:
    """Return all reply pools configured for one member."""
    data = load_player_replies(group_id)
    normalized_id = _normalize_user_id(user_id)
    records: List[dict] = []
    if isinstance(data, dict):
        direct = data.get(normalized_id)
        candidates = direct if isinstance(direct, list) else [direct]
        for candidate in candidates:
            record = _normalise_reply_record(candidate)
            if record:
                records.append(record)
        for value in data.values():
            if not isinstance(value, list):
                continue
            for candidate in value:
                if not isinstance(candidate, dict):
                    continue
                candidate_user = _normalize_user_id(str(candidate.get("user_id", candidate.get("target_user_id", ""))))
                if candidate_user != normalized_id:
                    continue
                record = _normalise_reply_record(candidate)
                if record:
                    records.append(record)
    elif isinstance(data, list):
        for candidate in data:
            if not isinstance(candidate, dict):
                continue
            candidate_user = _normalize_user_id(str(candidate.get("user_id", candidate.get("target_user_id", ""))))
            if candidate_user != normalized_id:
                continue
            record = _normalise_reply_record(candidate)
            if record:
                records.append(record)
    return records

def update_player_messages(group_id: str, user_id: str, messages: List[str]) -> bool:
    """更新用户回复消息"""
    normalized_id = _normalize_user_id(user_id)
    
    def _update(data):
        if data is None or normalized_id not in data:
            return data
        data[normalized_id]['messages'] = messages
        return data
    
    update_group_data(group_id, "player_replies", _update)
    _config_cache.pop(group_id, None)
    return True

def update_player_at(group_id: str, user_id: str, at_user: bool) -> bool:
    """更新用户@设置"""
    normalized_id = _normalize_user_id(user_id)
    
    def _update(data):
        if data is None or normalized_id not in data:
            return data
        data[normalized_id]['at_user'] = at_user
        return data
    
    update_group_data(group_id, "player_replies", _update)
    _config_cache.pop(group_id, None)
    return True

def toggle_player_reply(group_id: str, user_id: str, enabled: bool) -> bool:
    """启用/禁用用户专属回复"""
    normalized_id = _normalize_user_id(user_id)
    
    def _toggle(data):
        if data is None or normalized_id not in data:
            return data
        data[normalized_id]['enabled'] = enabled
        return data
    
    update_group_data(group_id, "player_replies", _toggle)
    _config_cache.pop(group_id, None)
    return True

def list_player_replies(group_id: str) -> List[dict]:
    """列出群组中所有用户专属回复"""
    data = load_player_replies(group_id)
    result = []
    for user_id, reply_data in data.items():
        result.append({
            'user_id': user_id,
            'messages': reply_data.get('messages', []),
            'at_user': reply_data.get('at_user', True),
            'enabled': reply_data.get('enabled', True)
        })
    return result

def get_all_player_replies(group_id: str) -> Dict[str, dict]:
    """获取群组所有用户专属回复"""
    return load_player_replies(group_id)

def get_random_message(messages: List[str]) -> str:
    """随机选择一条消息"""
    if not messages:
        return ""
    return random.choice(messages)

def clear_player_replies(group_id: str):
    """清空群组所有用户专属回复"""
    set_group_data(group_id, "player_replies", {})
    _config_cache.pop(group_id, None)

def get_player_reply_statistics(group_id: str) -> dict:
    """获取用户专属回复统计"""
    data = load_player_replies(group_id)
    enabled_count = sum(1 for r in data.values() if r.get('enabled', True))
    return {
        'total': len(data),
        'enabled': enabled_count,
        'disabled': len(data) - enabled_count
    }
