import json
import os
import time
from .group_config import get_group_data, set_group_data, update_group_data

_global_config = {}

def set_global_config(config: dict):
    global _global_config
    _global_config = config

def load_stats(group_id: str) -> dict:
    """加载群组统计"""
    return get_group_data(group_id, "stats", {"users": {}})

def save_stats(group_id: str, stats: dict):
    """保存群组统计"""
    set_group_data(group_id, "stats", stats)

def load_signin_data(group_id: str) -> dict:
    """加载群组签到数据"""
    return get_group_data(group_id, "signin", {})

def save_signin_data(group_id: str, data: dict):
    """保存群组签到数据"""
    set_group_data(group_id, "signin", data)

def get_signin_reward(days: int) -> int:
    """获取签到奖励"""
    rewards = {
        1: 10,
        3: 30,
        7: 100,
        14: 200,
        30: 500
    }
    for d in sorted(rewards.keys(), reverse=True):
        if days >= d:
            return rewards[d]
    return 10

def signin(group_id: str, user_id: str, user_name: str = "") -> dict:
    """签到"""
    signin_data = load_signin_data(group_id)
    stats = load_stats(group_id)
    
    today = time.strftime("%Y-%m-%d", time.localtime())
    now = int(time.time())
    
    user_data = signin_data.get(user_id, {})
    last_signin = user_data.get("last_signin", "")
    consecutive_days = user_data.get("consecutive_days", 0)
    
    if last_signin == today:
        return {
            "success": False,
            "message": "您今天已经签到过了",
            "consecutive_days": consecutive_days,
            "total_days": user_data.get("total_days", 0)
        }
    
    yesterday = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
    if last_signin == yesterday:
        consecutive_days += 1
    else:
        consecutive_days = 1
    
    reward = get_signin_reward(consecutive_days)
    
    signin_data[user_id] = {
        "last_signin": today,
        "consecutive_days": consecutive_days,
        "total_days": user_data.get("total_days", 0) + 1,
        "user_name": user_name or user_data.get("user_name", "")
    }
    
    user_stats = stats.get("users", {}).get(user_id, {})
    stats["users"][user_id] = {
        "points": user_stats.get("points", 0) + reward,
        "signin_count": user_stats.get("signin_count", 0) + 1,
        "total_signins": user_stats.get("total_signins", 0) + 1,
        "user_name": user_name or user_stats.get("user_name", ""),
        "joined_at": user_stats.get("joined_at", now)
    }
    
    save_signin_data(group_id, signin_data)
    save_stats(group_id, stats)
    
    return {
        "success": True,
        "message": f"✅ 签到成功！连续签到 {consecutive_days} 天，获得 {reward} 积分",
        "consecutive_days": consecutive_days,
        "total_days": signin_data[user_id]["total_days"],
        "points": stats["users"][user_id]["points"],
        "reward": reward
    }

def get_signin_status(group_id: str, user_id: str) -> dict:
    """获取签到状态"""
    signin_data = load_signin_data(group_id)
    stats = load_stats(group_id)
    
    user_data = signin_data.get(user_id, {})
    user_stats = stats.get("users", {}).get(user_id, {})
    
    today = time.strftime("%Y-%m-%d", time.localtime())
    
    return {
        "signed_today": user_data.get("last_signin") == today,
        "consecutive_days": user_data.get("consecutive_days", 0),
        "total_days": user_data.get("total_days", 0),
        "points": user_stats.get("points", 0),
        "user_name": user_data.get("user_name", "")
    }

def get_ranking(group_id: str, limit: int = 10) -> list:
    """获取积分排行"""
    stats = load_stats(group_id)
    users = stats.get("users", {})
    
    ranking = []
    for user_id, data in users.items():
        ranking.append({
            "user_id": user_id,
            "user_name": data.get("user_name", ""),
            "points": data.get("points", 0),
            "signin_count": data.get("signin_count", 0),
            "total_signins": data.get("total_signins", 0)
        })
    
    ranking.sort(key=lambda x: x["points"], reverse=True)
    return ranking[:limit]

def get_user_info(group_id: str, user_id: str) -> dict:
    """获取用户信息"""
    stats = load_stats(group_id)
    signin_data = load_signin_data(group_id)
    
    user_stats = stats.get("users", {}).get(user_id, {})
    user_signin = signin_data.get(user_id, {})
    
    return {
        "user_id": user_id,
        "user_name": user_stats.get("user_name", ""),
        "points": user_stats.get("points", 0),
        "signin_count": user_stats.get("signin_count", 0),
        "total_signins": user_stats.get("total_signins", 0),
        "consecutive_days": user_signin.get("consecutive_days", 0),
        "total_signin_days": user_signin.get("total_days", 0),
        "last_signin": user_signin.get("last_signin", ""),
        "joined_at": user_stats.get("joined_at", 0)
    }

def get_group_stats(group_id: str) -> dict:
    """获取群组统计"""
    stats = load_stats(group_id)
    signin_data = load_signin_data(group_id)
    
    users = stats.get("users", {})
    today_signins = 0
    total_users = len(users)
    total_points = sum(u.get("points", 0) for u in users.values())
    
    today = time.strftime("%Y-%m-%d", time.localtime())
    for user_id, data in signin_data.items():
        if data.get("last_signin") == today:
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
        if "users" not in stats:
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
        if stats is None or "users" not in stats:
            return stats
        if user_id in stats["users"]:
            stats["users"][user_id]["points"] -= points
        return stats
    
    update_group_data(group_id, "stats", _update)
    return True

def set_user_name(group_id: str, user_id: str, user_name: str):
    """设置用户名"""
    # 更新统计中的用户名
    def _update_stats(stats):
        if stats is None:
            stats = {"users": {}}
        if "users" not in stats:
            stats["users"] = {}
        if user_id in stats["users"]:
            stats["users"][user_id]["user_name"] = user_name
        return stats
    update_group_data(group_id, "stats", _update_stats)
    
    # 更新签到中的用户名
    def _update_signin(signin):
        if signin is None:
            signin = {}
        if user_id in signin:
            signin[user_id]["user_name"] = user_name
        return signin
    update_group_data(group_id, "signin", _update_signin)

def clear_stats(group_id: str):
    """清空群组统计"""
    set_group_data(group_id, "stats", {"users": {}})
    set_group_data(group_id, "signin", {})