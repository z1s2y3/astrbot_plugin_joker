import os
import json
import time
from pathlib import Path
from typing import Optional, List
from astrbot.api import logger
from astrbot.core import get_astrbot_data_path


def get_plugin_data_path():
    data_path = Path(get_astrbot_data_path()) / "plugin_data" / "qqadmin"
    data_path.mkdir(parents=True, exist_ok=True)
    return data_path


def get_group_cache_dir(group_id: str) -> Path:
    """获取群的缓存根目录: plugin_data/qqadmin/message_cache/{group_id}/"""
    return get_plugin_data_path() / "message_cache" / group_id


def get_date_cache_dir(group_id: str, date_str: str = None) -> Path:
    """获取按日期分的缓存目录: plugin_data/qqadmin/message_cache/{group_id}/{date}/"""
    if date_str is None:
        date_str = time.strftime("%Y-%m-%d")
    cache_dir = get_group_cache_dir(group_id) / date_str
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_user_cache_file(group_id: str, user_id: str, date_str: str = None) -> Path:
    """获取指定用户某日期的缓存文件: plugin_data/qqadmin/message_cache/{group_id}/{date}/{user_id}.json"""
    cache_dir = get_date_cache_dir(group_id, date_str)
    return cache_dir / f"{user_id}.json"


def get_all_users_for_date(group_id: str, date_str: str = None) -> List[str]:
    """获取指定群和日期下的所有用户ID列表"""
    cache_dir = get_date_cache_dir(group_id, date_str)
    if not cache_dir.exists():
        return []
    
    users = []
    for f in cache_dir.iterdir():
        if f.is_file() and f.suffix == ".json":
            user_id = f.stem
            users.append(user_id)
    return users


class UserMessageCache:
    """单个用户在某日期的消息缓存"""

    def __init__(self, group_id: str, user_id: str, date_str: str = None):
        self.group_id = group_id
        self.user_id = user_id
        self.date_str = date_str or time.strftime("%Y-%m-%d")
        self.cache_file = get_user_cache_file(group_id, user_id, self.date_str)
        self.messages = self._load()

    def _load(self) -> List[dict]:
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get("messages", [])
            except Exception as e:
                logger.error(f"加载消息缓存失败 {self.cache_file}: {e}")
                return []
        return []

    def _save(self):
        try:
            data = {
                "group_id": self.group_id,
                "user_id": self.user_id,
                "date": self.date_str,
                "updated_at": int(time.time()),
                "messages": self.messages
            }
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存消息缓存失败 {self.cache_file}: {e}")

    def add_message(self, message_id, message: str, timestamp: int):
        """添加消息"""
        for msg in self.messages:
            if str(msg.get("message_id")) == str(message_id):
                msg.update({"message": message, "time": timestamp})
                self._save()
                return

        self.messages.append({
            "message_id": message_id,
            "user_id": self.user_id,  # 保存 user_id
            "message": message,
            "time": timestamp,
            "added_at": int(time.time())
        })
        self._save()

    def get_message(self, message_id) -> Optional[dict]:
        for msg in self.messages:
            if str(msg.get("message_id")) == str(message_id):
                return msg
        return None

    def get_all_messages(self) -> List[dict]:
        return sorted(self.messages, key=lambda x: int(x.get("message_id", 0)), reverse=True)

    def recall_message(self, message_id):
        for msg in self.messages:
            if str(msg.get("message_id")) == str(message_id):
                msg["recalled"] = True
                self._save()
                return True
        return False


class GroupMessageCache:
    """群消息缓存管理器"""

    def __init__(self, group_id: str):
        self.group_id = group_id
        self.date_str = time.strftime("%Y-%m-%d")

    def add_message(self, message_id, user_id: str, message: str, timestamp: int):
        """添加消息到用户缓存"""
        cache = UserMessageCache(self.group_id, user_id, self.date_str)
        cache.add_message(message_id, message, timestamp)

    def get_user_messages(self, user_id: str, date_str: str = None) -> List[dict]:
        """获取用户在指定日期的消息"""
        if date_str is None:
            date_str = self.date_str
        cache = UserMessageCache(self.group_id, user_id, date_str)
        return cache.get_all_messages()

    def get_user_message(self, user_id: str, message_id, date_str: str = None) -> Optional[dict]:
        """获取用户指定消息"""
        if date_str is None:
            date_str = self.date_str
        cache = UserMessageCache(self.group_id, user_id, date_str)
        return cache.get_message(message_id)

    def get_all_user_ids(self, date_str: str = None) -> List[str]:
        """获取群内所有用户ID"""
        if date_str is None:
            date_str = self.date_str
        return get_all_users_for_date(self.group_id, date_str)

    def get_recent_messages(self, limit: int = 50, date_str: str = None) -> List[dict]:
        """获取最近的消息（跨所有用户）"""
        if date_str is None:
            date_str = self.date_str

        all_messages = []
        cache_dir = get_date_cache_dir(self.group_id, date_str)

        if not cache_dir.exists():
            return []

        for f in cache_dir.iterdir():
            if f.is_file() and f.suffix == ".json":
                try:
                    with open(f, 'r', encoding='utf-8') as fp:
                        data = json.load(fp)
                        all_messages.extend(data.get("messages", []))
                except:
                    continue

        all_messages.sort(key=lambda x: int(x.get("message_id", 0)), reverse=True)
        return all_messages[:limit]

    def recall_message(self, message_id, user_id: str = None, date_str: str = None):
        """标记消息为已撤回"""
        if date_str is None:
            date_str = self.date_str

        if user_id:
            cache = UserMessageCache(self.group_id, user_id, date_str)
            return cache.recall_message(message_id)
        else:
            for uid in self.get_all_user_ids(date_str):
                cache = UserMessageCache(self.group_id, uid, date_str)
                if cache.recall_message(message_id):
                    return True
            return False


def add_group_message(group_id: str, message_id, user_id: str, message: str, timestamp: int):
    """添加群消息到缓存"""
    cache = GroupMessageCache(group_id)
    cache.add_message(message_id, user_id, message, timestamp)


def get_user_cached_messages(group_id: str, user_id: str, date_str: str = None, limit: int = None) -> List[dict]:
    """获取用户的消息（跨日期）"""
    if date_str:
        # 只获取指定日期的消息
        cache = GroupMessageCache(group_id)
        messages = cache.get_user_messages(user_id, date_str)
        if limit:
            return messages[:limit]
        return messages
    else:
        # 跨日期获取最近几天的消息
        group_cache_dir = get_group_cache_dir(group_id)
        if not group_cache_dir.exists():
            return []
        
        all_messages = []
        # 获取所有日期目录，按日期从新到旧排序
        date_dirs = sorted([d for d in group_cache_dir.iterdir() if d.is_dir()], 
                          key=lambda x: x.name, reverse=True)
        
        for date_dir in date_dirs:
            user_file = date_dir / f"{user_id}.json"
            if user_file.exists():
                try:
                    with open(user_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        all_messages.extend(data.get("messages", []))
                except:
                    continue
        
         # 按message_id从大到小排序（message_id是递增的，大的是最新的）
        all_messages.sort(key=lambda x: int(x.get("message_id", 0)), reverse=True)
        
        if limit:
            return all_messages[:limit]
        return all_messages


def get_cached_message(group_id: str, user_id: str, message_id, date_str: str = None) -> Optional[dict]:
    """获取指定消息"""
    cache = GroupMessageCache(group_id)
    return cache.get_user_message(user_id, message_id, date_str)


def get_latest_cached_bot_message(group_id: str, bot_id: str) -> Optional[dict]:
    """获取最新的机器人消息"""
    cache = GroupMessageCache(group_id)
    messages = cache.get_user_messages(bot_id)
    if messages:
        return messages[0]
    return None


def get_recent_cached_messages(group_id: str, limit: int = 50, date_str: str = None) -> List[dict]:
    """获取最近的缓存消息（跨日期）"""
    if date_str:
        # 只获取指定日期的消息
        cache = GroupMessageCache(group_id)
        return cache.get_recent_messages(limit, date_str)
    else:
        # 跨日期获取最近几天的消息
        group_cache_dir = get_group_cache_dir(group_id)
        if not group_cache_dir.exists():
            return []
        
        all_messages = []
        # 获取所有日期目录，按日期从新到旧排序
        date_dirs = sorted([d for d in group_cache_dir.iterdir() if d.is_dir()], 
                          key=lambda x: x.name, reverse=True)
        
        for date_dir in date_dirs:
            for f in date_dir.iterdir():
                if f.is_file() and f.suffix == ".json":
                    try:
                        with open(f, 'r', encoding='utf-8') as fp:
                            data = json.load(fp)
                            all_messages.extend(data.get("messages", []))
                    except:
                        continue
        
        # 按message_id从大到小排序（message_id是递增的，大的是最新的）
        all_messages.sort(key=lambda x: int(x.get("message_id", 0)), reverse=True)
        
        return all_messages[:limit]


def remove_group_message(group_id: str, message_id):
    """从缓存中删除指定消息"""
    group_cache_dir = get_group_cache_dir(group_id)
    if not group_cache_dir.exists():
        return
    
    for date_dir in group_cache_dir.iterdir():
        if date_dir.is_dir():
            for f in date_dir.iterdir():
                if f.is_file() and f.suffix == ".json":
                    try:
                        with open(f, 'r', encoding='utf-8') as fp:
                            data = json.load(fp)
                        
                        original_count = len(data.get("messages", []))
                        data["messages"] = [
                            msg for msg in data.get("messages", []) 
                            if str(msg.get("message_id")) != str(message_id)
                        ]
                        
                        if len(data["messages"]) != original_count:
                            with open(f, 'w', encoding='utf-8') as fp:
                                json.dump(data, fp, ensure_ascii=False, indent=2)
                            return True
                    except Exception as e:
                        logger.error(f"删除消息缓存失败 {f}: {e}")
    return False
