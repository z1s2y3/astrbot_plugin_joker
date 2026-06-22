import json
import os
import time
import shutil
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api import logger

def get_plugin_data_path():
    data_path = Path(get_astrbot_data_path()) / "plugin_data" / "qqadmin"
    data_path.mkdir(parents=True, exist_ok=True)
    return data_path

def get_groups_config_dir():
    """获取群配置目录"""
    config_dir = get_plugin_data_path() / "group_configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir

def get_group_config_file(group_id: str):
    """获取指定群组的配置文件路径"""
    return get_groups_config_dir() / f"{group_id}.json"

def get_config_history_file():
    return os.path.join(get_plugin_data_path(), "config_history.json")

def get_default_feature_config():
    """获取默认的功能配置（所有功能默认关闭）"""
    return {
        "audit": {
            "enabled": False,
            "approval_mode": "math",
            "verification_timeout": 300,
            "max_attempts": 3,
            "admin_bypass": True,
            "enable_invite_audit": False
        },
        "blacklist": {
            "enabled": False,
            "auto_kick": True,
            "auto_blacklist_on_leave": False,
            "auto_blacklist_on_verify_fail": False
        },
        "welcome": {
            "enabled": False,
            "auto_welcome": False,
            "delay": 0,
            "message": "欢迎 {user_name} 加入本群！",
            "farewell_enabled": False,
            "farewell_message": "{user_name} 离开了本群",
            "keywords": ["欢迎", "welcome", "入群"],
            "auto_reply_enabled": False
        },
        "recall": {
            "enabled": False,
            "self_recall": False,
            "time_limit": 60,
            "mode": "keyword",
            "notification": False,
            "notification_message": "❌ 您的消息包含违规内容，已被撤回"
        },
        "mute": {
            "enabled": False,
            "level": 5,
            "duration": 300
        },
        "kick": {
            "enabled": False,
            "auto_blacklist": False,
            "limit": 3
        },
        "stats": {
            "enabled": False,
            "update_interval": 300
        },
        "reply": {
            "enabled": False,
            "cooldown": 0
        },
        "player_reply": {
            "enabled": False,
            "default_at_user": True
        },
        "text_to_image": {
            "enabled": False,
            "font_size": 16,
            "font_color": "#333333",
            "bg_color": "#ffffff",
            "line_spacing": 4,
            "padding": 20,
            "modify_permission": "admin"
        }
    }

def load_group_config(group_id: str) -> dict:
    """加载指定群组的配置文件"""
    config_file = get_group_config_file(group_id)
    try:
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"加载群组 {group_id} 配置失败: {e}")
    return {}

def load_group_configs() -> dict:
    """加载所有群组的配置文件"""
    config_dir = get_groups_config_dir()
    all_configs = {}
    try:
        for file in config_dir.iterdir():
            if file.suffix == ".json" and file.stem.isdigit():
                group_id = file.stem
                with open(file, "r", encoding="utf-8") as f:
                    all_configs[group_id] = json.load(f)
    except Exception as e:
        logger.error(f"加载所有群组配置失败: {e}")
    return all_configs

def save_group_configs(configs: dict):
    """批量保存群组配置文件"""
    for group_id, config in configs.items():
        save_group_config(group_id, config)

def save_group_config(group_id: str, config: dict):
    """保存指定群组的配置文件"""
    config_file = get_group_config_file(group_id)
    try:
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存群组 {group_id} 配置失败: {e}")

def get_group_data(group_id: str, data_key: str, default=None):
    """获取分群数据（如黑名单、禁言列表等），直接存储在配置根目录"""
    config = load_group_config(group_id)
    if not config:
        return default
    return config.get(data_key, default)

def set_group_data(group_id: str, data_key: str, data_value):
    """设置分群数据（如黑名单、禁言列表等），直接存储在配置根目录"""
    config = load_group_config(group_id)
    if not config:
        config = init_group_config(group_id)
    
    config[data_key] = data_value
    config["updated_at"] = int(time.time())
    save_group_config(group_id, config)

def update_group_data(group_id: str, data_key: str, update_func):
    """
    原子性更新分群数据，直接存储在配置根目录
    update_func: 一个函数，接收旧数据，返回新数据
    """
    config = load_group_config(group_id)
    if not config:
        config = init_group_config(group_id)
    
    old_data = config.get(data_key)
    new_data = update_func(old_data)
    config[data_key] = new_data
    config["updated_at"] = int(time.time())
    save_group_config(group_id, config)
    return new_data

def init_group_config(group_id: str, global_config: dict = None) -> dict:
    """初始化群组配置：创建默认配置或从全局配置克隆"""
    config = {
        "group_id": group_id,
        "enabled": True,
        "features": get_default_feature_config(),
        "created_at": int(time.time()),
        "updated_at": int(time.time())
    }

    # 如果提供了全局配置，使用全局配置初始化
    if global_config:
        config = clone_global_config_to_group_config(group_id, global_config)

    save_group_config(group_id, config)
    return config

def load_config_history() -> dict:
    history_file = get_config_history_file()
    try:
        if os.path.exists(history_file):
            with open(history_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"加载配置历史失败: {e}")
    return {}

def save_config_history(history: dict):
    history_file = get_config_history_file()
    try:
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存配置历史失败: {e}")

def get_group_config(group_id: str, global_config: dict = None) -> dict:
    """获取群组配置，如果不存在则初始化"""
    config = load_group_config(group_id)
    if not config:
        # 初始化新群组配置
        config = init_group_config(group_id, global_config)
    return config

def get_group_feature_setting(group_id: str, feature: str, setting: str = None, global_config: dict = None):
    """获取群功能配置（兼容带有 _enabled 后缀的功能名称）
    如果分群没有配置，自动克隆全局配置到分群
    如果分群配置缺少某个功能的配置，从全局配置中读取
    """
    config = get_group_config(group_id, global_config)
    features = config.get("features", {})

    # 如果 features 为空，说明是新的分群配置，需要克隆全局配置
    if not features and global_config:
        features = clone_global_to_group(group_id, global_config)
        config["features"] = features
        save_group_config(group_id, config)

    # 处理带有 _enabled 后缀的功能名称
    feature_name = feature
    if feature_name.endswith("_enabled"):
        feature_name = feature_name[:-8]

    # 获取分群的功能配置
    feature_config = features.get(feature_name, {})
    
    if setting:
        # 如果分群配置中没有该设置，从全局配置中读取
        if setting not in feature_config and global_config:
            qqadmin_config = global_config.get("qqadmin", {})
            global_feature_config = qqadmin_config.get(feature_name, {})
            return global_feature_config.get(setting)
        return feature_config.get(setting)

    # 如果没有指定 setting，返回功能的启用状态（兼容旧调用方式）
    # 如果分群配置中没有 enabled，从全局配置中读取
    if "enabled" not in feature_config and global_config:
        qqadmin_config = global_config.get("qqadmin", {})
        global_feature_config = qqadmin_config.get(feature_name, {})
        return global_feature_config.get("enabled", False)
    return feature_config.get("enabled", False)


def clone_global_to_group(group_id: str, global_config: dict) -> dict:
    """克隆全局配置到分群
    将 qqadmin.xxx 映射为 features.xxx
    注意：全局是 qqadmin.reply.enable_reply，分群是 features.reply.enabled
    """
    from copy import deepcopy
    qqadmin = global_config.get("qqadmin", {})

    features = {}
    for module_name, module_config in qqadmin.items():
        if isinstance(module_config, dict):
            features[module_name] = deepcopy(module_config)
            # 映射 enable_xxx 或 enabled 字段
            if module_name == "reply":
                if "enable_reply" in features[module_name]:
                    features[module_name]["enabled"] = features[module_name].pop("enable_reply")
            elif module_name == "recall":
                if "enable_recall" in features[module_name]:
                    features[module_name]["enabled"] = features[module_name].pop("enable_recall")
                # 保留enable_self_recall字段（用于自身撤回功能）
                if "enable_self_recall" in features[module_name]:
                    features[module_name]["self_recall_enabled"] = features[module_name].pop("enable_self_recall")
            elif module_name == "mute":
                if "enable_mute" in features[module_name]:
                    features[module_name]["enabled"] = features[module_name].pop("enable_mute")
            elif module_name == "kick":
                if "enable_kick" in features[module_name]:
                    features[module_name]["enabled"] = features[module_name].pop("enable_kick")
            elif module_name == "blacklist":
                if "enable_blacklist" in features[module_name]:
                    features[module_name]["enabled"] = features[module_name].pop("enable_blacklist")
            elif module_name == "audit":
                if "enable_audit" in features[module_name]:
                    features[module_name]["enabled"] = features[module_name].pop("enable_audit")
            elif module_name == "welcome":
                if "enable_welcome" in features[module_name]:
                    features[module_name]["enabled"] = features[module_name].pop("enable_welcome")
            elif module_name == "group_config":
                if "enable_group_config" in features[module_name]:
                    features[module_name]["enabled"] = features[module_name].pop("enable_group_config")
            elif module_name == "text_to_image":
                if "enable_text_to_image" in features[module_name]:
                    features[module_name]["enabled"] = features[module_name].pop("enable_text_to_image")
    return features

def update_group_config(group_id: str, key: str, value, operator_id: str = ""):
    """更新群组基础配置"""
    config = load_group_config(group_id)
    if not config:
        config = init_group_config(group_id)

    old_value = config.get(key)

    config[key] = value
    config["updated_at"] = int(time.time())
    save_group_config(group_id, config)

    history = load_config_history()
    if group_id not in history:
        history[group_id] = []

    history[group_id].append({
        "key": key,
        "old_value": old_value,
        "new_value": value,
        "operator_id": operator_id,
        "timestamp": int(time.time())
    })

    if len(history[group_id]) > 200:
        history[group_id] = history[group_id][-100:]

    save_config_history(history)

def get_group_feature_detail(group_id: str, feature: str, global_config: dict = None) -> dict:
    """获取群功能的详细配置"""
    config = get_group_config(group_id, global_config)
    features = config.get("features", {})

    if feature in features:
        return features.get(feature, {})

    # 返回默认配置
    default_config = get_default_feature_config()
    return default_config.get(feature, {})

def get_group_all_features(group_id: str, global_config: dict = None) -> dict:
    """获取群的所有功能配置"""
    config = get_group_config(group_id, global_config)
    return config.get("features", get_default_feature_config())

def update_group_feature_config(group_id: str, feature: str, setting: str, value, operator_id: str = ""):
    """更新群功能的具体配置"""
    config = load_group_config(group_id)
    if not config:
        config = init_group_config(group_id)

    if "features" not in config:
        config["features"] = get_default_feature_config()

    if feature not in config["features"]:
        config["features"][feature] = {}

    old_value = config["features"][feature].get(setting)
    config["features"][feature][setting] = value
    config["updated_at"] = int(time.time())
    save_group_config(group_id, config)

    history = load_config_history()
    if group_id not in history:
        history[group_id] = []

    history[group_id].append({
        "key": f"features.{feature}.{setting}",
        "old_value": old_value,
        "new_value": value,
        "operator_id": operator_id,
        "timestamp": int(time.time())
    })

    if len(history[group_id]) > 200:
        history[group_id] = history[group_id][-100:]

    save_config_history(history)

def is_feature_enabled(group_id: str, feature: str, global_config: dict = None) -> bool:
    """检查群功能是否启用"""
    config = get_group_config(group_id, global_config)

    # 处理带有 _enabled 后缀的功能名称
    feature_name = feature
    if feature_name.endswith("_enabled"):
        feature_name = feature_name[:-8]

    # 新结构
    features = config.get("features", {})
    if features:
        return features.get(feature_name, {}).get("enabled", False)

    # 旧结构兼容
    return config.get(f"{feature}_enabled", False)

def set_feature_enabled(group_id: str, feature: str, enabled: bool, global_config: dict = None):
    """设置群功能启用状态"""
    config = load_group_config(group_id)
    if not config:
        config = init_group_config(group_id, global_config)

    # 处理带有 _enabled 后缀的功能名称
    feature_name = feature
    if feature_name.endswith("_enabled"):
        feature_name = feature_name[:-8]

    # 确保 features 存在
    if "features" not in config:
        config["features"] = get_default_feature_config()

    if feature_name not in config["features"]:
        config["features"][feature_name] = {}

    config["features"][feature_name]["enabled"] = enabled
    config["updated_at"] = int(time.time())
    save_group_config(group_id, config)

def list_all_groups() -> list:
    """列出所有有配置文件的群组"""
    config_dir = get_groups_config_dir()
    groups = []
    try:
        for file in config_dir.iterdir():
            if file.suffix == ".json" and file.stem.isdigit():
                groups.append(file.stem)
    except Exception as e:
        logger.error(f"列出群组配置失败: {e}")
    return groups

def remove_group_config(group_id: str):
    """删除群组配置文件"""
    config_file = get_group_config_file(group_id)
    try:
        if config_file.exists():
            config_file.unlink()
    except Exception as e:
        logger.error(f"删除群组 {group_id} 配置失败: {e}")

def get_config_history(group_id: str, limit: int = 50) -> list:
    history = load_config_history()
    if group_id not in history:
        return []
    return history[group_id][-limit:]

def reset_group_config(group_id: str):
    """重置群配置为默认值"""
    config = {
        "group_id": group_id,
        "enabled": True,
        "features": get_default_feature_config(),
        "created_at": int(time.time()),
        "updated_at": int(time.time())
    }
    save_group_config(group_id, config)

def clone_group_config(source_group_id: str, target_group_id: str):
    """克隆一个群的配置到另一个群（复制整个配置文件）"""
    source_config = load_group_config(source_group_id)
    if not source_config:
        logger.error(f"源群组 {source_group_id} 配置不存在")
        return False

    import copy
    target_config = copy.deepcopy(source_config)
    target_config["group_id"] = target_group_id
    target_config["updated_at"] = int(time.time())
    save_group_config(target_group_id, target_config)
    return True

def clone_global_config_to_group(group_id: str, global_config: dict = None):
    """从全局配置克隆到指定群组（只克隆配置，保留分群数据）"""
    # 先读取现有配置，保留分群数据
    existing_config = load_group_config(group_id)
    
    # 需要保留的数据字段（不克隆）
    data_fields = [
        "replies", "player_reply", "blacklist", "whitelist",
        "audit_pending", "audit_history", "audit_challenges",
        "welcome_history", "operation_logs", "invite_stats",
        "kick_records", "sub_admins", "cooldown_cache",
        "custom_welcome_message", "custom_farewell_message"
    ]
    
    # 保留的数据
    preserved_data = {}
    for field in data_fields:
        if field in existing_config:
            preserved_data[field] = existing_config[field]
    
    # 创建新配置（只包含配置部分）
    config = {
        "group_id": group_id,
        "enabled": True,
        "features": get_default_feature_config(),
        "created_at": existing_config.get("created_at", int(time.time())),
        "updated_at": int(time.time()),
        "authorized": False,
        "auth_info": {},
        "is_whitelist": False,
        "is_blacklist": False
    }

    # 检查授权状态（如果启用了授权系统）
    auth_enabled = False
    if global_config:
        auth_enabled = global_config.get("auth", {}).get("enabled", False)
    
    if auth_enabled:
        from ..auth.manager import is_group_authorized, get_group_auth_info
        config["authorized"] = is_group_authorized(group_id)
        config["auth_info"] = get_group_auth_info(group_id)

    # 检查白名单和黑名单
    if global_config:
        # 检查全局白名单
        whitelist_groups = global_config.get("basic", {}).get("whitelist_groups", [])
        if whitelist_groups and group_id in whitelist_groups:
            config["is_whitelist"] = True
        
        # 检查黑名单
        blacklist_groups = global_config.get("basic", {}).get("blacklist_groups", [])
        if blacklist_groups and group_id in blacklist_groups:
            config["is_blacklist"] = True

    if global_config:
        qqadmin_config = global_config.get("qqadmin", {})

        # 审计配置
        audit_config = qqadmin_config.get("audit", {})
        config["features"]["audit"]["enabled"] = audit_config.get("enable_audit", False)
        config["features"]["audit"]["approval_mode"] = audit_config.get("default_approval_mode", "math")
        config["features"]["audit"]["verification_timeout"] = audit_config.get("default_verification_timeout", 300)
        config["features"]["audit"]["max_attempts"] = audit_config.get("max_verify_attempts", 3)
        config["features"]["audit"]["admin_bypass"] = audit_config.get("admin_bypass_verify", True)
        config["features"]["audit"]["enable_invite_audit"] = audit_config.get("enable_invite_audit", False)
        
        # 直接写入分群配置根目录（供 get_group_data 读取）
        config["audit_enabled"] = config["features"]["audit"]["enabled"]
        config["approval_mode"] = config["features"]["audit"]["approval_mode"]
        config["verification_timeout"] = config["features"]["audit"]["verification_timeout"]
        config["max_attempts"] = config["features"]["audit"]["max_attempts"]
        config["admin_bypass"] = config["features"]["audit"]["admin_bypass"]

        # 黑名单配置
        blacklist_config = qqadmin_config.get("blacklist", {})
        config["features"]["blacklist"]["enabled"] = blacklist_config.get("enable_blacklist", False)
        config["features"]["blacklist"]["auto_kick"] = blacklist_config.get("auto_kick_blacklisted", True)
        config["features"]["blacklist"]["auto_blacklist_on_leave"] = blacklist_config.get("auto_blacklist_on_leave", False)
        config["features"]["blacklist"]["auto_blacklist_on_verify_fail"] = blacklist_config.get("auto_blacklist_on_verify_fail", False)
        
        # 直接写入分群配置根目录
        config["blacklist_settings"] = {
            "enabled": config["features"]["blacklist"]["enabled"],
            "auto_kick": config["features"]["blacklist"]["auto_kick"],
            "auto_blacklist_on_leave": config["features"]["blacklist"]["auto_blacklist_on_leave"],
            "auto_blacklist_on_verify_fail": config["features"]["blacklist"]["auto_blacklist_on_verify_fail"]
        }

        # 欢迎配置
        welcome_config = qqadmin_config.get("welcome", {})
        config["features"]["welcome"]["enabled"] = welcome_config.get("enable_welcome", False)
        config["features"]["welcome"]["auto_welcome"] = welcome_config.get("enable_auto_welcome", False)
        config["features"]["welcome"]["delay"] = welcome_config.get("default_welcome_delay", 0)
        config["features"]["welcome"]["message"] = welcome_config.get("default_welcome_message", "欢迎 {user_name} 加入本群！")
        config["features"]["welcome"]["farewell_enabled"] = welcome_config.get("enable_farewell", False)
        config["features"]["welcome"]["farewell_message"] = welcome_config.get("default_farewell_message", "{user_name} 离开了本群")
        config["features"]["welcome"]["keywords"] = welcome_config.get("welcome_keywords", ["欢迎", "welcome", "入群"])
        config["features"]["welcome"]["auto_reply_enabled"] = welcome_config.get("auto_reply_enabled", True)
        
        # 直接写入分群配置根目录
        config["welcome_enabled"] = config["features"]["welcome"]["enabled"]
        config["welcome_message"] = config["features"]["welcome"]["message"]
        config["welcome_farewell_enabled"] = config["features"]["welcome"]["farewell_enabled"]
        config["welcome_farewell_message"] = config["features"]["welcome"]["farewell_message"]
        config["welcome_keywords"] = config["features"]["welcome"]["keywords"]
        config["welcome_auto_reply_enabled"] = config["features"]["welcome"]["auto_reply_enabled"]
        config["welcome_auto_welcome"] = config["features"]["welcome"]["auto_welcome"]
        config["welcome_delay"] = config["features"]["welcome"]["delay"]

        # 撤回配置
        recall_config = qqadmin_config.get("recall", {})
        config["features"]["recall"]["enabled"] = recall_config.get("enable_recall", False)
        config["features"]["recall"]["self_recall"] = recall_config.get("enable_self_recall", True)
        config["features"]["recall"]["time_limit"] = recall_config.get("default_recall_time", 60)
        config["features"]["recall"]["mode"] = recall_config.get("default_recall_mode", "keyword")
        config["features"]["recall"]["notification"] = recall_config.get("recall_notification", False)
        config["features"]["recall"]["notification_message"] = recall_config.get("recall_notification_message", "❌ 您的消息包含违规内容，已被撤回")
        
        # 直接写入分群配置根目录
        config["recall_enabled"] = config["features"]["recall"]["enabled"]
        config["self_recall_enabled"] = config["features"]["recall"]["self_recall"]
        config["self_recall_time"] = recall_config.get("default_recall_time", 30)
        config["recall_time"] = config["features"]["recall"]["time_limit"]
        config["recall_mode"] = config["features"]["recall"]["mode"]
        config["recall_keywords"] = recall_config.get("recall_keywords", [])
        config["recall_regex"] = recall_config.get("recall_regex_patterns", [])
        config["recall_whitelist_mode"] = False
        config["recall_whitelist"] = []

        # 禁言配置
        mute_config = qqadmin_config.get("mute", {})
        config["features"]["mute"]["enabled"] = mute_config.get("enable_mute", True)
        config["features"]["mute"]["level"] = mute_config.get("default_mute_level", 5)
        config["features"]["mute"]["duration"] = mute_config.get("default_mute_duration", 300)
        
        # 直接写入分群配置根目录
        config["mute_settings"] = {
            "enabled": config["features"]["mute"]["enabled"],
            "level": config["features"]["mute"]["level"],
            "duration": config["features"]["mute"]["duration"]
        }

        # 踢出配置
        kick_config = qqadmin_config.get("kick", {})
        config["features"]["kick"]["enabled"] = kick_config.get("enable_kick", False)
        config["features"]["kick"]["auto_blacklist"] = kick_config.get("auto_blacklist_on_kick", False)
        config["features"]["kick"]["limit"] = kick_config.get("default_kick_limit", 3)
        
        # 直接写入分群配置根目录
        config["kick_settings"] = {
            "enabled": config["features"]["kick"]["enabled"],
            "auto_blacklist": config["features"]["kick"]["auto_blacklist"],
            "limit": config["features"]["kick"]["limit"]
        }

        # 统计配置
        stats_config = qqadmin_config.get("stats", {})
        config["features"]["stats"]["enabled"] = stats_config.get("enable_stats", True)
        config["stats_enabled"] = config["features"]["stats"]["enabled"]

        # 回复配置
        reply_config = qqadmin_config.get("reply", {})
        config["features"]["reply"]["enabled"] = reply_config.get("enable_reply", False)
        config["features"]["reply"]["cooldown"] = reply_config.get("default_reply_cooldown", 0)
        config["reply_enabled"] = config["features"]["reply"]["enabled"]
        config["reply_cooldown"] = config["features"]["reply"]["cooldown"]

        # 用户专属回复配置
        player_reply_config = qqadmin_config.get("player_reply", {})
        config["features"]["player_reply"]["enabled"] = player_reply_config.get("enable_player_reply", False)
        config["features"]["player_reply"]["default_at_user"] = player_reply_config.get("default_at_user", True)
        config["player_reply_enabled"] = config["features"]["player_reply"]["enabled"]

        # 文转图配置
        text_to_image_config = qqadmin_config.get("text_to_image", {})
        config["features"]["text_to_image"]["enabled"] = text_to_image_config.get("enable_text_to_image", False)
        config["features"]["text_to_image"]["font_size"] = text_to_image_config.get("font_size", 16)
        config["features"]["text_to_image"]["font_color"] = text_to_image_config.get("font_color", "#333333")
        config["features"]["text_to_image"]["bg_color"] = text_to_image_config.get("bg_color", "#ffffff")
        config["features"]["text_to_image"]["line_spacing"] = text_to_image_config.get("line_spacing", 4)
        config["features"]["text_to_image"]["padding"] = text_to_image_config.get("padding", 20)
        config["features"]["text_to_image"]["modify_permission"] = text_to_image_config.get("modify_permission", "admin")

    # 合并保留的分群数据
    for field, value in preserved_data.items():
        config[field] = value
    
    save_group_config(group_id, config)
    return config

def clear_group_data(group_id: str) -> dict:
    """清除分群所有数据，恢复到默认配置"""
    # 创建默认配置
    config = {
        "group_id": group_id,
        "enabled": True,
        "features": get_default_feature_config(),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "authorized": False,
        "auth_info": {},
        "is_whitelist": False,
        "is_blacklist": False,
        # 清空所有数据
        "replies": {},
        "player_reply": {},
        "blacklist": [],
        "whitelist": [],
        "audit_pending": [],
        "audit_history": [],
        "audit_challenges": {},
        "welcome_history": [],
        "operation_logs": [],
        "invite_stats": {},
        "kick_records": [],
        "sub_admins": [],
        "cooldown_cache": {},
        # 默认配置值
        "audit_enabled": False,
        "approval_mode": "math",
        "verification_timeout": 300,
        "max_attempts": 3,
        "admin_bypass": True,
        "blacklist_settings": {"enabled": False, "auto_kick": True},
        "welcome_enabled": False,
        "welcome_message": "欢迎 {user_name} 加入本群！",
        "welcome_farewell_enabled": False,
        "welcome_farewell_message": "{user_name} 离开了本群",
        "welcome_keywords": ["欢迎", "welcome", "入群"],
        "welcome_auto_reply_enabled": True,
        "welcome_auto_welcome": False,
        "welcome_delay": 0,
        "recall_enabled": False,
        "self_recall_enabled": False,
        "self_recall_time": 30,
        "recall_time": 60,
        "recall_mode": "keyword",
        "recall_keywords": [],
        "recall_regex": [],
        "recall_whitelist_mode": False,
        "recall_whitelist": [],
        "mute_settings": {"enabled": True, "level": 5, "duration": 300},
        "kick_settings": {"enabled": False, "auto_blacklist": False, "limit": 3},
        "stats_enabled": True,
        "reply_enabled": False,
        "reply_cooldown": 0,
        "player_reply_enabled": False
    }
    
    save_group_config(group_id, config)
    return config

def clone_global_config_to_group_config(group_id: str, global_config: dict = None) -> dict:
    """从全局配置克隆到指定群组（返回配置对象）"""
    config = {
        "group_id": group_id,
        "enabled": True,
        "features": get_default_feature_config(),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "authorized": False,
        "auth_info": {},
        "is_whitelist": False,
        "is_blacklist": False
    }

    # 检查授权状态（如果启用了授权系统）
    auth_enabled = False
    if global_config:
        auth_enabled = global_config.get("auth", {}).get("enabled", False)
    
    if auth_enabled:
        from ..auth.manager import is_group_authorized, get_group_auth_info
        config["authorized"] = is_group_authorized(group_id)
        config["auth_info"] = get_group_auth_info(group_id)

    # 检查白名单和黑名单
    if global_config:
        # 检查全局白名单
        whitelist_groups = global_config.get("basic", {}).get("whitelist_groups", [])
        if whitelist_groups and group_id in whitelist_groups:
            config["is_whitelist"] = True
        
        # 检查黑名单
        blacklist_groups = global_config.get("basic", {}).get("blacklist_groups", [])
        if blacklist_groups and group_id in blacklist_groups:
            config["is_blacklist"] = True

    if global_config:
        qqadmin_config = global_config.get("qqadmin", {})

        # 审计配置
        audit_config = qqadmin_config.get("audit", {})
        config["features"]["audit"]["enabled"] = audit_config.get("enable_audit", False)
        config["features"]["audit"]["approval_mode"] = audit_config.get("default_approval_mode", "math")
        config["features"]["audit"]["verification_timeout"] = audit_config.get("default_verification_timeout", 300)
        config["features"]["audit"]["max_attempts"] = audit_config.get("max_verify_attempts", 3)
        config["features"]["audit"]["admin_bypass"] = audit_config.get("admin_bypass_verify", True)
        config["features"]["audit"]["enable_invite_audit"] = audit_config.get("enable_invite_audit", False)
        
        # 直接写入分群配置根目录（供 get_group_data 读取）
        config["audit_enabled"] = config["features"]["audit"]["enabled"]
        config["approval_mode"] = config["features"]["audit"]["approval_mode"]
        config["verification_timeout"] = config["features"]["audit"]["verification_timeout"]
        config["max_attempts"] = config["features"]["audit"]["max_attempts"]
        config["admin_bypass"] = config["features"]["audit"]["admin_bypass"]

        # 黑名单配置
        blacklist_config = qqadmin_config.get("blacklist", {})
        config["features"]["blacklist"]["enabled"] = blacklist_config.get("enable_blacklist", False)
        config["features"]["blacklist"]["auto_kick"] = blacklist_config.get("auto_kick_blacklisted", True)
        config["features"]["blacklist"]["auto_blacklist_on_leave"] = blacklist_config.get("auto_blacklist_on_leave", False)
        config["features"]["blacklist"]["auto_blacklist_on_verify_fail"] = blacklist_config.get("auto_blacklist_on_verify_fail", False)
        
        # 直接写入分群配置根目录
        config["blacklist_settings"] = {
            "enabled": config["features"]["blacklist"]["enabled"],
            "auto_kick": config["features"]["blacklist"]["auto_kick"],
            "auto_blacklist_on_leave": config["features"]["blacklist"]["auto_blacklist_on_leave"],
            "auto_blacklist_on_verify_fail": config["features"]["blacklist"]["auto_blacklist_on_verify_fail"]
        }

        # 欢迎配置
        welcome_config = qqadmin_config.get("welcome", {})
        config["features"]["welcome"]["enabled"] = welcome_config.get("enable_welcome", False)
        config["features"]["welcome"]["auto_welcome"] = welcome_config.get("enable_auto_welcome", False)
        config["features"]["welcome"]["delay"] = welcome_config.get("default_welcome_delay", 0)
        config["features"]["welcome"]["message"] = welcome_config.get("default_welcome_message", "欢迎 {user_name} 加入本群！")
        config["features"]["welcome"]["farewell_enabled"] = welcome_config.get("enable_farewell", False)
        config["features"]["welcome"]["farewell_message"] = welcome_config.get("default_farewell_message", "{user_name} 离开了本群")
        config["features"]["welcome"]["keywords"] = welcome_config.get("welcome_keywords", ["欢迎", "welcome", "入群"])
        config["features"]["welcome"]["auto_reply_enabled"] = welcome_config.get("auto_reply_enabled", True)

        # 撤回配置
        recall_config = qqadmin_config.get("recall", {})
        config["features"]["recall"]["enabled"] = recall_config.get("enable_recall", False)
        config["features"]["recall"]["self_recall"] = recall_config.get("enable_self_recall", True)
        config["features"]["recall"]["self_recall_enabled"] = recall_config.get("enable_self_recall", True)
        config["features"]["recall"]["time_limit"] = recall_config.get("default_recall_time", 60)
        config["features"]["recall"]["mode"] = recall_config.get("default_recall_mode", "keyword")
        config["features"]["recall"]["notification"] = recall_config.get("recall_notification", False)
        config["features"]["recall"]["notification_message"] = recall_config.get("recall_notification_message", "❌ 您的消息包含违规内容，已被撤回")
        
        # 直接写入分群配置根目录（供 get_group_data 读取）
        config["recall_enabled"] = config["features"]["recall"]["enabled"]
        config["self_recall_enabled"] = config["features"]["recall"]["self_recall"]
        config["self_recall_time"] = recall_config.get("default_recall_time", 30)
        config["recall_time"] = config["features"]["recall"]["time_limit"]
        config["recall_mode"] = config["features"]["recall"]["mode"]

        # 禁言配置
        mute_config = qqadmin_config.get("mute", {})
        config["features"]["mute"]["enabled"] = mute_config.get("enable_mute", True)
        config["features"]["mute"]["level"] = mute_config.get("default_mute_level", 5)
        config["features"]["mute"]["duration"] = mute_config.get("default_mute_duration", 300)

        # 踢出配置
        kick_config = qqadmin_config.get("kick", {})
        config["features"]["kick"]["enabled"] = kick_config.get("enable_kick", False)
        config["features"]["kick"]["auto_blacklist"] = kick_config.get("auto_blacklist_on_kick", False)
        config["features"]["kick"]["limit"] = kick_config.get("default_kick_limit", 3)

        # 统计配置
        stats_config = qqadmin_config.get("stats", {})
        config["features"]["stats"]["enabled"] = stats_config.get("enable_stats", True)

        # 回复配置
        reply_config = qqadmin_config.get("reply", {})
        config["features"]["reply"]["enabled"] = reply_config.get("enable_reply", False)
        config["features"]["reply"]["cooldown"] = reply_config.get("default_reply_cooldown", 0)

        # 用户专属回复配置
        player_reply_config = qqadmin_config.get("player_reply", {})
        config["features"]["player_reply"]["enabled"] = player_reply_config.get("enable_player_reply", False)
        config["features"]["player_reply"]["default_at_user"] = player_reply_config.get("default_at_user", True)

        # 关键词过滤配置
        keyword_filter_config = qqadmin_config.get("keyword_filter", {})
        config["features"]["keyword_filter"] = {
            "enabled": keyword_filter_config.get("enable_keyword_filter", False),
            "filter_keywords": keyword_filter_config.get("filter_keywords", []),
            "filter_regex_patterns": keyword_filter_config.get("filter_regex_patterns", []),
            "filter_action": keyword_filter_config.get("filter_action", "warn"),
            "filter_mute_duration": keyword_filter_config.get("filter_mute_duration", 300),
            "warn_message": keyword_filter_config.get("warn_message", "⚠️ 您的消息包含敏感内容，请遵守群规！"),
            "enable_log": keyword_filter_config.get("enable_log", True),
            "admin_bypass": keyword_filter_config.get("admin_bypass_filter", True)
        }

    return config

def export_group_config(group_id: str) -> dict:
    return load_group_config(group_id)

def import_group_config(group_id: str, config: dict):
    import copy
    config = copy.deepcopy(config)
    config["group_id"] = group_id
    config["updated_at"] = int(time.time())
    save_group_config(group_id, config)

def get_all_configs_summary() -> list:
    """获取所有群配置的摘要信息"""
    groups = list_all_groups()
    summary = []

    for group_id in groups:
        config = load_group_config(group_id)
        if config:
            features = config.get("features", {})
            summary.append({
                "group_id": group_id,
                "enabled": config.get("enabled", True),
                "authorized": config.get("authorized", False),
                "is_whitelist": config.get("is_whitelist", False),
                "is_blacklist": config.get("is_blacklist", False),
                "features": {
                    "reply": features.get("reply", {}).get("enabled", True),
                    "mute": features.get("mute", {}).get("enabled", True),
                    "kick": features.get("kick", {}).get("enabled", True),
                    "blacklist": features.get("blacklist", {}).get("enabled", True),
                    "audit": features.get("audit", {}).get("enabled", False),
                    "recall": features.get("recall", {}).get("enabled", False),
                    "welcome": features.get("welcome", {}).get("enabled", False),
                    "stats": features.get("stats", {}).get("enabled", True)
                },
                "created_at": config.get("created_at", 0),
                "updated_at": config.get("updated_at", 0),
                "auth_info": config.get("auth_info", {})
            })

    return summary

def get_group_info_for_display(group_id: str, global_config: dict = None) -> dict:
    """获取群组信息用于显示，过滤掉不应在分群设置中显示的配置"""
    config = get_group_config(group_id, global_config)
    
    # 过滤敏感配置（不应在分群设置中修改的）
    filtered_config = {
        "group_id": config.get("group_id", group_id),
        "enabled": config.get("enabled", True),
        "authorized": config.get("authorized", False),
        "is_whitelist": config.get("is_whitelist", False),
        "created_at": config.get("created_at", 0),
        "updated_at": config.get("updated_at", 0),
        "features": {}
    }
    
    # 只包含可修改的功能配置
    features = config.get("features", {})
    for feature_name, feature_config in features.items():
        if feature_name not in ["admin_settings", "system"]:  # 排除系统级配置
            filtered_config["features"][feature_name] = feature_config
    
    return filtered_config

def sync_global_config_to_all_groups(global_config: dict):
    """同步全局配置到所有已存在的群组配置"""
    groups = list_all_groups()
    for group_id in groups:
        # 跳过白名单群组（不受限制）
        whitelist_groups = global_config.get("qqadmin", {}).get("whitelist_groups", [])
        if whitelist_groups and group_id in whitelist_groups:
            continue
        
        # 克隆全局配置到该群组
        clone_global_config_to_group(group_id, global_config)
    
    return len(groups)

def set_group_priority(group_id: str, priority: int):
    update_group_config(group_id, "priority", priority)

def get_group_priority(group_id: str) -> int:
    config = load_group_config(group_id)
    return config.get("priority", 0)

def add_group_tag(group_id: str, tag: str):
    config = get_group_config(group_id)
    tags = config.get("tags", [])

    if tag not in tags:
        tags.append(tag)
        update_group_config(group_id, "tags", tags)

def remove_group_tag(group_id: str, tag: str):
    config = get_group_config(group_id)
    tags = config.get("tags", [])

    if tag in tags:
        tags.remove(tag)
        update_group_config(group_id, "tags", tags)

def get_group_tags(group_id: str) -> list:
    config = load_group_config(group_id)
    return config.get("tags", [])

def set_group_nickname(group_id: str, nickname: str):
    update_group_config(group_id, "nickname", nickname)

def get_group_nickname(group_id: str) -> str:
    config = load_group_config(group_id)
    return config.get("nickname", "")

def get_groups_by_tag(tag: str) -> list:
    groups = list_all_groups()
    matching_groups = []

    for group_id in groups:
        config = load_group_config(group_id)
        tags = config.get("tags", [])
        if tag in tags:
            matching_groups.append(group_id)

    return matching_groups

def get_groups_by_feature(feature: str) -> list:
    groups = list_all_groups()
    matching_groups = []

    for group_id in groups:
        config = load_group_config(group_id)
        if config.get(feature, False):
            matching_groups.append(group_id)

    return matching_groups

def bulk_update_feature(groups: list, feature: str, enabled: bool):
    for group_id in groups:
        config = load_group_config(group_id)
        if config:
            config[feature] = enabled
            config["updated_at"] = int(time.time())
            save_group_config(group_id, config)