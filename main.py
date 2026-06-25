import time
import asyncio
import json
import os

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.star.filter.event_message_type import EventMessageType

from .scum import ServerQuery
from .auth import (
    generate_license_key, verify_license_key,
    load_bindings, save_bindings, bind_server, unbind_server, get_group_binding,
    load_auth_data, save_auth_data,
    load_group_settings, save_group_settings,
    get_group_setting, set_group_setting,
    authorize_group, deauthorize_group, add_auth_time, reduce_auth_time,
    is_group_authorized, get_group_auth_info, list_all_authorizations,
    delete_all_keys, delete_used_keys, get_unused_keys, get_used_keys, mark_key_used
)

from .qqadmin import (
    # 自定义回复
    add_reply, remove_reply, list_replies, clear_replies, get_reply, match_any_reply,
    set_reply_cooldown, get_reply_cooldown, is_in_cooldown, set_cooldown,
    
    # 禁言系统
    mute_user, unmute_user, is_muted, get_mute_info, list_muted_users,
    get_mute_settings, set_mute_settings, calculate_mute_duration,
    mute_user_auto,
    mute_user_api, unmute_user_api, set_group_whole_ban_api,
    
    # 踢出系统
    add_kick_record, is_kicked, remove_kick_record, get_kick_records,
    clear_kick_records, get_kick_settings, set_kick_settings, should_auto_ban,
    get_kick_count,
    kick_user_api, kick_users_api,
    
    # 黑名单系统
    add_to_blacklist, remove_from_blacklist, is_in_blacklist,
    get_blacklist_info, list_blacklist, clear_blacklist,
    get_blacklist_settings, set_blacklist_settings,
    
    # 入群审核
    add_audit_request, approve_join_request, reject_join_request,
    get_audit_requests, is_in_audit_pending, get_audit_settings,
    set_audit_settings, add_to_whitelist, remove_from_whitelist,
    get_verification_settings, set_verification_settings,
    create_math_challenge, create_id_challenge, verify_answer,
    get_pending_verification, remove_challenge, is_verification_expired,
    clean_expired_challenges, clean_expired_challenges_and_kick, get_verification_stats,
    check_expired_audit_requests, set_audit_timeout, get_audit_timeout,
    get_pending_by_code,
    # 邀请统计
    record_invitation, get_inviter_count, get_top_inviter,
    check_excessive_invites, get_all_excessive_inviter_stats, get_invited_users_by_inviter,
    remove_inviter_data, update_invite_status, load_invite_stats, clear_invite_stats,
    # 操作日志
    add_operation_log, get_operation_logs, get_operation_logs_by_operator,
    get_operation_logs_by_action, get_operation_logs_by_time,
    get_operation_stats, clear_operation_logs,
    
    # 文转图
    text_to_image, is_text_to_image_available,
    
    # 撤回系统
    enable_recall, is_recall_enabled, set_recall_time, get_recall_time,
    add_recall_keyword, remove_recall_keyword, get_recall_keywords,
    should_recall_message, set_recall_mode, get_recall_mode,
    add_recall_regex, remove_recall_regex, get_recall_regex_list,
    should_recall_by_regex, log_recall, get_recall_log,
    get_user_recall_history, set_whitelist_mode, is_whitelist_mode,
    add_whitelist_user, remove_whitelist_user, is_user_whitelisted,
    get_whitelist, get_recall_settings, set_recall_settings,
    get_recall_statistics, parse_time_duration, recall_user_messages,
    recall_recent_messages,
    
    # 入群欢迎
    set_welcome_message, get_welcome_message, enable_welcome,
    is_welcome_enabled, set_farewell_message, get_farewell_message,
    enable_farewell, is_farewell_enabled, set_welcome_keywords,
    get_welcome_keywords, should_send_welcome, set_welcome_type,
    get_welcome_type, add_welcome_image, remove_welcome_image,
    get_welcome_images, log_welcome, get_welcome_history,
    get_user_welcome_history, set_welcome_delay, get_welcome_delay,
    set_auto_reply, is_auto_reply_enabled, set_welcome_placeholders,
    get_welcome_placeholders, format_welcome_message, format_farewell_message,
    get_welcome_settings, set_welcome_settings, get_welcome_statistics,
    update_group_members, get_group_members, get_new_members,
    is_member_tracking_enabled, enable_member_tracking, should_welcome_new_member,
    
    # 分群设置
    get_group_config, get_group_feature_setting, get_group_feature_detail, get_group_all_features,
    update_group_config, update_group_feature_config, is_feature_enabled,
    set_feature_enabled, list_all_groups, reset_group_config, clone_group_config,
    load_group_configs, save_group_configs, remove_group_config, get_config_history,
    export_group_config, import_group_config, get_all_configs_summary, set_group_priority,
    get_group_priority, add_group_tag, remove_group_tag, get_group_tags,
    set_group_nickname, get_group_nickname, get_groups_by_tag, get_groups_by_feature,
    bulk_update_feature, clone_global_config_to_group,
    
    # 定时消息
    add_timed_message, get_timed_messages, get_timed_message, update_timed_message,
    delete_timed_message, enable_timed_message, get_due_messages, mark_sent,
    
    # 统计系统
    signin, get_signin_status, get_ranking, get_user_info, get_group_stats,
    add_points, deduct_points,
    
    # 分群管理员
    add_sub_admin, remove_sub_admin, is_sub_admin, get_sub_admins,
    get_sub_admin_permissions, set_sub_admin_permissions, has_permission,
    get_sub_admin_info
)

@register("astrbot_plugin_joker", "Joker", "SCUM服务器查询插件", "1.3.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.base_url = "https://api.battlemetrics.com"
        
        # 尝试从插件配置文件读取配置
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            config_path = os.path.join(get_astrbot_data_path(), "config", "astrbot_plugin_joker_config.json")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8-sig") as f:
                    self.config = json.load(f)
                    # 设置全局配置更新时间戳
                    if "_updated_at" not in self.config:
                        self.config["_updated_at"] = int(os.path.getmtime(config_path))
                    logger.info(f"成功从配置文件加载插件配置")
            else:
                logger.warning(f"插件配置文件不存在: {config_path}，使用默认配置")
                self.config = config or {}
                self.config["_updated_at"] = int(time.time())
        except Exception as e:
            logger.error(f"加载插件配置失败: {e}，使用默认配置")
            self.config = config or {}
            self.config["_updated_at"] = int(time.time())
        
        # 将配置传递给 audit 模块
        from .qqadmin.audit import set_global_config
        set_global_config(self.config)
        
        # Helper function to get nested config value
        def get_nested(key1, key2=None, key3=None, default=None):
            """
            Get nested config value, handling both flat and nested structures
            with possible 'items' wrapper
            """
            if key1 not in self.config:
                return default
            
            val = self.config[key1]
            
            # Check if first level has items
            if isinstance(val, dict) and 'items' in val:
                val = val['items']
            
            if not key2:
                # Return first level value
                if isinstance(val, dict) and 'default' in val:
                    return val['default']
                return val if val is not None else default
            
            # Get second level
            if not isinstance(val, dict) or key2 not in val:
                return default
            
            val = val[key2]
            
            # Check second level items
            if isinstance(val, dict) and 'items' in val:
                val = val['items']
            
            if not key3:
                if isinstance(val, dict) and 'default' in val:
                    return val['default']
                return val if val is not None else default
            
            # Get third level
            if not isinstance(val, dict) or key3 not in val:
                return default
            
            val = val[key3]
            
            if isinstance(val, dict) and 'default' in val:
                return val['default']
            return val if val is not None else default
        
        # 重新加载配置的方法
        def reload_config():
            """从插件配置文件重新加载配置"""
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path
                config_path = os.path.join(get_astrbot_data_path(), "config", "astrbot_plugin_joker_config.json")
                if os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8-sig") as f:
                        new_config = json.load(f)
                        self.config = new_config
                        # 重新加载所有配置属性
                        self.whitelist_groups = get_nested("basic", "whitelist_groups", default=[])
                        self.admin_groups = get_nested("basic", "admin_groups", default=[])
                        self.filter_groups = get_nested("basic", "filter_groups", default=[])
                        self.owner_ids = get_nested("basic", "owner_ids", default=[])
                        # 将配置传递给 audit 模块
                        from .qqadmin.audit import set_global_config
                        set_global_config(self.config)
                        logger.info(f"插件配置已重新加载: {json.dumps(self.config.get('qqadmin', {}).get('blacklist', {}), ensure_ascii=False)}")
                        return True
                else:
                    logger.warning(f"配置文件不存在: {config_path}")
            except Exception as e:
                logger.error(f"重新加载配置失败: {e}")
            return False
        
        # 添加重新加载配置的方法到实例
        self._reload_plugin_config = reload_config
        
        # 基础设置
        self.whitelist_groups = get_nested("basic", "whitelist_groups", default=[])
        self.admin_groups = get_nested("basic", "admin_groups", default=[])
        self.filter_groups = get_nested("basic", "filter_groups", default=[])
        self.owner_ids = get_nested("basic", "owner_ids", default=[])
        self.allow_slash_prefix = get_nested("basic", "allow_slash_prefix", default=True)
        self.enable_help = get_nested("basic", "enable_help", default=True)
        self.enable_settings = get_nested("basic", "enable_settings", default=True)
        
        # SCUM查询
        self.enable_query = get_nested("scum", "enable_query", default=True)
        self.enable_binding = get_nested("scum", "enable_binding", default=True)
        self.default_enabled = get_nested("scum", "default_enabled", default=True)
        self.allow_other_query = get_nested("scum", "allow_other_query", default=True)
        
        # 授权系统
        self.enable_auth = get_nested("auth", "enable_auth", default=False)
        self.auth_key = get_nested("auth", "auth_key", default="joker_plugin_secret_key_2024")
        self.auto_delete_used_keys_days = get_nested("auth", "auto_delete_used_keys_days", default=0)
        self.owner_ignore_auth = get_nested("auth", "owner_ignore_auth", default=True)
        self.owner_ignore_binding = get_nested("auth", "owner_ignore_binding", default=True)
        self.auth_detail_query = get_nested("auth", "auth_detail_query", default=False)
        
        # QQ群管 - 全局
        self.qqadmin_enable_group_manage = get_nested("qqadmin", "enable_group_manage", default=True)
        self.owner_ignore_group_manage = get_nested("qqadmin", "owner_ignore_group_manage", default=True)
        self.qqadmin_enable_group_config = get_nested("qqadmin", "enable_group_config", default=False)
        
        # QQ群管 - 自定义回复
        self.qqadmin_enable_reply = get_nested("qqadmin", "reply", "enable_reply", default=False)
        self.qqadmin_default_reply_cooldown = get_nested("qqadmin", "reply", "default_reply_cooldown", default=0)
        self.qqadmin_enable_keyword_reply = get_nested("qqadmin", "reply", "enable_keyword_reply", default=False)
        self.qqadmin_enable_exact_match = get_nested("qqadmin", "reply", "enable_exact_match", default=False)
        self.qqadmin_max_reply_length = get_nested("qqadmin", "reply", "max_reply_length", default=500)
        
        # QQ群管 - 禁言系统
        self.qqadmin_enable_mute = get_nested("qqadmin", "mute", "enable_mute", default=False)
        self.qqadmin_default_mute_level = get_nested("qqadmin", "mute", "default_mute_level", default=5)
        self.qqadmin_default_mute_duration = get_nested("qqadmin", "mute", "default_mute_duration", default=300)
        
        # QQ群管 - 踢出系统
        self.qqadmin_enable_kick = get_nested("qqadmin", "kick", "enable_kick", default=False)
        self.qqadmin_auto_blacklist_on_kick = get_nested("qqadmin", "kick", "auto_blacklist_on_kick", default=False)
        self.qqadmin_default_kick_limit = get_nested("qqadmin", "kick", "default_kick_limit", default=3)
        
        # QQ群管 - 黑名单系统
        self.qqadmin_enable_blacklist = get_nested("qqadmin", "blacklist", "enable_blacklist", default=False)
        self.qqadmin_auto_kick_blacklisted = get_nested("qqadmin", "blacklist", "auto_kick_blacklisted", default=True)
        self.qqadmin_auto_blacklist_on_leave = get_nested("qqadmin", "blacklist", "auto_blacklist_on_leave", default=False)
        self.qqadmin_auto_blacklist_on_verify_fail = get_nested("qqadmin", "blacklist", "auto_blacklist_on_verify_fail", default=False)
        
        # QQ群管 - 入群审核
        self.qqadmin_enable_audit = get_nested("qqadmin", "audit", "enable_audit", default=False)
        self.qqadmin_enable_invite_audit = get_nested("qqadmin", "audit", "enable_invite_audit", default=False)
        self.qqadmin_default_approval_mode = get_nested("qqadmin", "audit", "default_approval_mode", default="math")
        self.qqadmin_default_verification_timeout = get_nested("qqadmin", "audit", "default_verification_timeout", default=300)
        self.qqadmin_max_verify_attempts = get_nested("qqadmin", "audit", "max_verify_attempts", default=3)
        self.qqadmin_admin_bypass_verify = get_nested("qqadmin", "audit", "admin_bypass_verify", default=True)
        
        # QQ群管 - 统计系统
        self.qqadmin_enable_stats = get_nested("qqadmin", "stats", "enable_stats", default=True)
        
        # QQ群管 - 撤回系统
        self.qqadmin_enable_recall = get_nested("qqadmin", "recall", "enable_recall", default=False)
        self.qqadmin_enable_self_recall = get_nested("qqadmin", "recall", "enable_self_recall", default=True)
        self.qqadmin_default_recall_time = get_nested("qqadmin", "recall", "default_recall_time", default=60)
        self.qqadmin_default_recall_mode = get_nested("qqadmin", "recall", "default_recall_mode", default="keyword")
        self.qqadmin_recall_keywords = get_nested("qqadmin", "recall", "recall_keywords", default=["广告", "推广", "二维码", "vx", "微信", "qq"])
        self.qqadmin_recall_regex_patterns = get_nested("qqadmin", "recall", "recall_regex_patterns", default=[])
        self.qqadmin_enable_admin_recall = get_nested("qqadmin", "recall", "enable_admin_recall", default=True)
        self.qqadmin_recall_notification = get_nested("qqadmin", "recall", "recall_notification", default=False)
        
        # QQ群管 - 入群欢迎
        self.qqadmin_enable_welcome = get_nested("qqadmin", "welcome", "enable_welcome", default=False)
        self.qqadmin_enable_auto_welcome = get_nested("qqadmin", "welcome", "enable_auto_welcome", default=False)
        self.welcome_check_interval = get_nested("qqadmin", "welcome", "welcome_check_interval", default=30)
        self.qqadmin_default_welcome_delay = get_nested("qqadmin", "welcome", "default_welcome_delay", default=0)
        self.qqadmin_default_welcome_message = get_nested("qqadmin", "welcome", "default_welcome_message", default="欢迎 {user_name} 加入本群！")
        self.qqadmin_enable_farewell = get_nested("qqadmin", "welcome", "enable_farewell", default=False)
        self.qqadmin_default_farewell_message = get_nested("qqadmin", "welcome", "default_farewell_message", default="{user_name} 离开了本群")
        self.qqadmin_welcome_keywords = get_nested("qqadmin", "welcome", "welcome_keywords", default=["欢迎", "welcome", "入群"])
        self.qqadmin_auto_reply_enabled = get_nested("qqadmin", "welcome", "auto_reply_enabled", default=True)

        # 权限配置
        # SCUM权限模式: read_only(只读) / full_access(完全访问)
        self.scum_permission_mode = get_nested("scum", "permission_mode", default="read_only")
        # 授权系统权限模式: read_only / full_access
        self.auth_permission_mode = get_nested("auth", "permission_mode", default="read_only")
        
        # 群管功能权限配置
        self.reply_use_permission = get_nested("qqadmin", "reply", "use_permission", default="admin")
        self.reply_modify_permission = get_nested("qqadmin", "reply", "modify_permission", default="admin")
        
        self.mute_use_permission = get_nested("qqadmin", "mute", "use_permission", default="admin")
        self.mute_modify_permission = get_nested("qqadmin", "mute", "modify_permission", default="admin")
        
        self.kick_use_permission = get_nested("qqadmin", "kick", "use_permission", default="admin")
        self.kick_modify_permission = get_nested("qqadmin", "kick", "modify_permission", default="admin")
        
        self.blacklist_use_permission = get_nested("qqadmin", "blacklist", "use_permission", default="admin")
        self.blacklist_modify_permission = get_nested("qqadmin", "blacklist", "modify_permission", default="admin")
        
        self.audit_use_permission = get_nested("qqadmin", "audit", "use_permission", default="admin")
        self.audit_modify_permission = get_nested("qqadmin", "audit", "modify_permission", default="admin")
        
        self.recall_use_permission = get_nested("qqadmin", "recall", "use_permission", default="admin")
        self.recall_modify_permission = get_nested("qqadmin", "recall", "modify_permission", default="admin")
        
        self.stats_use_permission = get_nested("qqadmin", "stats", "use_permission", default="member")
        self.stats_modify_permission = get_nested("qqadmin", "stats", "modify_permission", default="admin")
        
        self.welcome_use_permission = get_nested("qqadmin", "welcome", "use_permission", default="member")
        self.welcome_modify_permission = get_nested("qqadmin", "welcome", "modify_permission", default="admin")

        # QQ群管 - 分群设置功能权限配置
        self.group_config_use_permission = get_nested("qqadmin", "group_config", "use_permission", default="admin")
        self.group_config_modify_permission = get_nested("qqadmin", "group_config", "modify_permission", default="admin")
        
        # QQ群管 - 全局配置功能权限配置
        self.global_config_use_permission = get_nested("qqadmin", "global_config", "use_permission", default="admin")

        # QQ群管 - 关键词过滤系统
        self.qqadmin_enable_keyword_filter = get_nested("qqadmin", "keyword_filter", "enable_keyword_filter", default=False)
        self.qqadmin_filter_keywords = get_nested("qqadmin", "keyword_filter", "filter_keywords", default=["广告", "推广", "二维码", "vx", "微信", "qq", "群号", "拉人"])
        self.qqadmin_filter_regex_patterns = get_nested("qqadmin", "keyword_filter", "filter_regex_patterns", default=[])
        self.qqadmin_filter_action = get_nested("qqadmin", "keyword_filter", "filter_action", default="warn")
        self.qqadmin_filter_mute_duration = get_nested("qqadmin", "keyword_filter", "filter_mute_duration", default=300)
        self.qqadmin_warn_message = get_nested("qqadmin", "keyword_filter", "warn_message", default="⚠️ 您的消息包含敏感内容，请遵守群规！")
        self.qqadmin_filter_enable_log = get_nested("qqadmin", "keyword_filter", "enable_log", default=True)
        self.qqadmin_admin_bypass_filter = get_nested("qqadmin", "keyword_filter", "admin_bypass_filter", default=True)
        self.keyword_filter_use_permission = get_nested("qqadmin", "keyword_filter", "use_permission", default="admin")
        self.keyword_filter_modify_permission = get_nested("qqadmin", "keyword_filter", "modify_permission", default="admin")

        # QQ群管 - 用户专属回复
        self.qqadmin_enable_player_reply = get_nested("qqadmin", "player_reply", "enable_player_reply", default=False)
        self.player_reply_use_permission = get_nested("qqadmin", "player_reply", "use_permission", default="admin")
        self.player_reply_default_at_user = get_nested("qqadmin", "player_reply", "default_at_user", default=True)

        self.server_query = ServerQuery(self.base_url)

        self._member_check_task = None
        self._context = context
        
        # 自身撤回任务集合
        self._recall_tasks = set()
        
        # 全局配置自动同步机制
        self._last_global_config_mtime = 0
        self._auto_sync_enabled = get_nested("qqadmin", "auto_sync_global_config", default=True)
        self._init_global_config_mtime()
        
        # 启动定时检查配置更新的任务（每5秒检查一次）
        if self._auto_sync_enabled:
            import asyncio
            self._config_sync_task = asyncio.create_task(self._periodic_config_sync())

    def _init_global_config_mtime(self):
        """初始化全局配置修改时间"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            config_path = os.path.join(get_astrbot_data_path(), "config", "astrbot_plugin_joker_config.json")
            if os.path.exists(config_path):
                self._last_global_config_mtime = int(os.path.getmtime(config_path))
                logger.info(f"全局配置初始修改时间: {self._last_global_config_mtime}")
        except Exception as e:
            logger.error(f"获取全局配置修改时间失败: {e}")

    def _check_and_auto_sync_global_config(self, group_id: str):
        """检查全局配置是否更新，自动同步到分群"""
        if not self._auto_sync_enabled:
            return False
        
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            config_path = os.path.join(get_astrbot_data_path(), "config", "astrbot_plugin_joker_config.json")
            if not os.path.exists(config_path):
                return False
            
            current_mtime = int(os.path.getmtime(config_path))
            if current_mtime <= self._last_global_config_mtime:
                return False
            
            # 全局配置已更新，自动同步到所有已授权的分群
            logger.info(f"🔄 检测到全局配置已更新，开始自动同步...")
            self._last_global_config_mtime = current_mtime
            
            from .qqadmin.group_config import clone_global_config_to_group
            from .auth.manager import load_auth_data
            
            auth_data = load_auth_data()
            authorized_groups = auth_data.get("groups", {})
            
            synced_count = 0
            for gid in authorized_groups:
                if authorized_groups[gid].get("authorized", False):
                    clone_global_config_to_group(gid, self.config)
                    synced_count += 1
                    logger.info(f"✅ 已同步配置到群 {gid}")
            
            if synced_count > 0:
                logger.info(f"✅ 全局配置已自动同步到 {synced_count} 个已授权群组")
            
            return True
        except Exception as e:
            logger.error(f"自动同步全局配置失败: {e}")
            return False

    async def _periodic_config_sync(self):
        """定时检查全局配置更新并自动同步到分群（每5秒检查一次）"""
        import asyncio
        while True:
            try:
                self._check_and_auto_sync_global_config(None)
            except Exception as e:
                logger.error(f"定时配置同步失败: {e}")
            await asyncio.sleep(5)

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        sender = event.message_obj.sender
        sender_id = sender.user_id if hasattr(sender, 'user_id') else ""
        return str(sender_id) in [str(oid) for oid in self.owner_ids]

    def _should_convert_to_image(self, event: AstrMessageEvent) -> bool:
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            return False
        
        # 主人不受关机状态限制
        if self._is_owner(event):
            from .qqadmin.group_config import get_group_data
            group_data = get_group_data(group_id, "features", {})
            text_to_image_config = group_data.get("text_to_image", {})
            return text_to_image_config.get("enable_text_to_image", False)
        
        from .qqadmin.group_config import get_group_data
        group_data = get_group_data(group_id, "features", {})
        
        # 检查关机状态（非主人用户）
        power_status = group_data.get("power_status", "")
        if power_status == "off":
            return False
        
        text_to_image_config = group_data.get("text_to_image", {})
        return text_to_image_config.get("enable_text_to_image", False)

    def _get_user_role(self, event: AstrMessageEvent) -> str:
        """
        获取用户在当前上下文中的角色级别
        返回: "owner" > "admin" > "member"
        """
        if self._is_owner(event):
            return "owner"
        if self._is_group_admin_or_owner(event):
            return "admin"
        return "member"

    def _check_permission(self, event: AstrMessageEvent, required_permission: str) -> bool:
        """
        检查用户是否具有指定的权限级别
        required_permission: "member", "admin", "owner"
        权限级别: owner > admin > member
        """
        user_role = self._get_user_role(event)
        
        # 权限级别映射
        permission_levels = {
            "member": 1,
            "admin": 2,
            "owner": 3
        }
        
        user_level = permission_levels.get(user_role, 1)
        required_level = permission_levels.get(required_permission, 1)
        
        return user_level >= required_level

    def _check_scum_permission(self, event: AstrMessageEvent, action: str = "use") -> bool:
        """
        检查SCUM功能的权限
        action: "use"(使用) / "modify"(修改)
        """
        # 主人不受限制
        if self._is_owner(event):
            return True
        
        # 检查权限模式
        if self.scum_permission_mode == "read_only":
            # 只读模式下只允许使用，不允许修改
            if action == "modify":
                # 但如果是管理员或群主，允许修改
                if self._is_group_admin_or_owner(event):
                    return True
                return False
        
        return True

    def _check_auth_permission(self, event: AstrMessageEvent, action: str = "use") -> bool:
        """
        检查授权系统的权限
        action: "use"(使用) / "modify"(修改)
        """
        # 主人不受限制
        if self._is_owner(event):
            return True
        
        # 检查权限模式
        if self.auth_permission_mode == "read_only":
            # 只读模式下只允许查询状态，不允许修改（生成卡密等）
            if action == "modify":
                # 但如果是管理员或群主，允许修改
                if self._is_group_admin_or_owner(event):
                    return True
                return False
        
        return True

    def _check_feature_permission(self, event: AstrMessageEvent, feature_name: str, action: str = "use") -> bool:
        """
        检查群管功能的权限（同步版本）
        feature_name: "reply"(自定义回复), "mute"(禁言系统), "kick"(踢出系统), "blacklist"(黑名单系统), 
                      "audit"(入群审核), "recall"(撤回系统), "stats"(统计系统), "welcome"(入群欢迎),
                      "group_config"(分群设置), "keyword_filter"(关键词过滤), "global_config"(全局配置)
        action: "use"(使用) / "modify"(修改)
        """
        # 主人不受限制
        if self._is_owner(event):
            return True
        
        # 获取该功能的权限配置
        permission_attr = f"{feature_name}_{action}_permission"
        required_permission = getattr(self, permission_attr, "admin")
        
        # 如果只需要 member 权限，直接通过
        if required_permission == "member":
            return True
        
        # 使用同步检查
        return self._check_permission(event, required_permission)

    async def _check_feature_permission_async(self, event: AstrMessageEvent, feature_name: str, action: str = "use") -> bool:
        """
        检查群管功能的权限（异步版本，使用 API 获取真实角色信息）
        feature_name: "reply"(自定义回复), "mute"(禁言系统), "kick"(踢出系统), "blacklist"(黑名单系统), 
                      "audit"(入群审核), "recall"(撤回系统), "stats"(统计系统), "welcome"(入群欢迎),
                      "group_config"(分群设置), "keyword_filter"(关键词过滤), "global_config"(全局配置)
        action: "use"(使用) / "modify"(修改)
        """
        # 主人不受限制
        if self._is_owner(event):
            return True
        
        # 获取该功能的权限配置
        permission_attr = f"{feature_name}_{action}_permission"
        required_permission = getattr(self, permission_attr, "admin")
        
        # 如果只需要 member 权限，直接通过
        if required_permission == "member":
            return True
        
        # 如果需要 admin 或 owner 权限，使用异步 API 检查
        if required_permission in ["admin", "owner"]:
            return await self._is_admin_or_owner_async(event)
        
        return self._check_permission(event, required_permission)

    async def _is_slash_prefix_allowed_async(self, event: AstrMessageEvent) -> bool:
        """异步版本的斜杠命令前缀检查，使用 API 获取真实权限信息"""
        group_id = event.message_obj.group_id
        if group_id:
            group_setting = get_group_setting(group_id, "allow_slash_prefix", None)
            if group_setting is not None:
                return group_setting
        
        # 检查是否是管理员或群主，如果是则允许
        if await self._is_admin_or_owner_async(event):
            return True
        
        # 检查是否是主人，如果是则允许
        if self._is_owner(event):
            return True
        
        return self.allow_slash_prefix

    def _is_slash_prefix_allowed(self, event: AstrMessageEvent) -> bool:
        group_id = event.message_obj.group_id
        if group_id:
            group_setting = get_group_setting(group_id, "allow_slash_prefix", None)
            if group_setting is not None:
                return group_setting
        # 检查是否是主人，如果是则允许
        if self._is_owner(event):
            return True
        return self.allow_slash_prefix

    def _is_authorized(self, event: AstrMessageEvent) -> bool:
        if not self.enable_auth:
            return True
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            return False
        if self._is_owner(event) and self.owner_ignore_auth:
            return True
        return is_group_authorized(group_id)

    def _is_in_whitelist(self, group_id: str) -> bool:
        if not group_id:
            return False
        if not self.whitelist_groups:
            return False
        return group_id in self.whitelist_groups

    def _is_admin_group(self, group_id: str) -> bool:
        if not self.admin_groups:
            return True
        return group_id in self.admin_groups

    async def initialize(self):
        if self.qqadmin_enable_welcome:
            self._member_check_task = asyncio.create_task(self._member_check_loop())
        
        self._timed_message_task = asyncio.create_task(self._timed_message_loop())

    async def _timed_message_loop(self):
        await asyncio.sleep(5)
        while True:
            try:
                await self._send_timed_messages()
            except Exception as e:
                logger.error(f"定时消息发送失败: {e}")
            await asyncio.sleep(60)
    
    async def _send_timed_messages(self):
        # 遍历所有群获取定时消息
        from .qqadmin.group_config import list_all_groups
        all_groups = list_all_groups()
        
        for group_id in all_groups:
            due_messages = get_due_messages(group_id)
            if not due_messages:
                continue
            
            for msg in due_messages:
                try:
                    message = msg["message"]
                    
                    platform = None
                    try:
                        platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                    except Exception as e:
                        logger.error(f"获取平台实例失败: {e}")
                    
                    if platform:
                        await platform.get_client().call_action(
                            action="send_group_msg",
                            group_id=int(group_id),
                            message=message
                        )
                        mark_sent(group_id, msg["id"])
                        logger.info(f"定时消息发送成功: group={group_id}, name={msg['name']}")
                except Exception as e:
                    logger.error(f"发送定时消息失败: {e}")

    async def _member_check_loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                await self._check_new_members()
                await self._check_verification_timeout()
            except Exception as e:
                logger.error(f"后台检查失败: {e}")
            await asyncio.sleep(self.welcome_check_interval)
    
    async def _check_verification_timeout(self):
        if not self.qqadmin_enable_audit:
            return
        try:
            from .qqadmin.audit import get_pending_verifications, remove_pending_request, get_verification_timeout
            pending_list = get_pending_verifications()
            logger.debug(f"检查验证超时: 待验证列表长度={len(pending_list)}")
            
            for pending in pending_list:
                group_id = pending.get("group_id")
                user_id = pending.get("user_id")
                requested_at = pending.get("requested_at", 0)
                
                if not group_id or not user_id:
                    continue
                
                current_time = int(time.time())
                elapsed = current_time - requested_at
                # 从分群配置获取超时时间
                timeout = get_verification_timeout(group_id)
                
                logger.debug(f"检查用户 {user_id} 的验证超时: requested_at={requested_at}, 当前时间={current_time}, 已过去={elapsed}秒, 超时时间={timeout}秒")
                
                if elapsed > timeout:
                    logger.info(f"用户 {user_id} 验证超时，已过去 {elapsed} 秒，超时时间 {timeout} 秒")
                    from .qqadmin.kick import kick_user_api
                    await kick_user_api(group_id, user_id, f"验证超时{timeout // 60}分钟", context=self.context)
                    remove_pending_request(group_id, user_id)
                    
                    # 更新邀请状态为已拒绝
                    update_invite_status(group_id, user_id, "rejected")
                    logger.info(f"更新邀请状态: user_id={user_id}, group_id={group_id}, status=rejected")
                    
                    logger.info(f"验证超时踢出用户: user_id={user_id}, group_id={group_id}")
                else:
                    remaining = timeout - elapsed
                    logger.debug(f"用户 {user_id} 还剩 {remaining} 秒验证时间")
        except Exception as e:
            logger.error(f"检查验证超时失败: {e}")

    async def _check_new_members(self):
        if not self.qqadmin_enable_welcome:
            return
        try:
            all_groups = list_all_groups()
            for group_id in all_groups:
                if not is_feature_enabled(group_id, "welcome_enabled"):
                    continue
                if not is_member_tracking_enabled(group_id):
                    continue
                from .qqadmin.api_client import NapCatAPI
                api_client = NapCatAPI()
                members_data = await api_client.get_group_member_list(group_id)
                if members_data.get("status") != "success":
                    continue
                members = members_data.get("data", [])
                if not members:
                    continue
                new_members = get_new_members(group_id, members)
                for member in new_members:
                    await self._send_welcome_to_new_member(group_id, member)
                update_group_members(group_id, members)
        except Exception as e:
            logger.error(f"检查新成员异常: {e}")

    async def _send_welcome_to_new_member(self, group_id: str, member: dict):
        try:
            user_id = str(member.get("user_id", ""))
            user_name = member.get("nickname", member.get("card", "新成员"))
            join_time = member.get("join_time", 0)
            inviter_id = member.get("inviter", "")
            
            from .qqadmin.blacklist import is_in_blacklist, get_blacklist_info
            from .qqadmin.group_config import is_feature_enabled, get_group_feature_detail
            
            if is_feature_enabled(group_id, "blacklist", self.config):
                blacklist_detail = get_group_feature_detail(group_id, "blacklist", self.config)
                auto_kick_blacklisted = blacklist_detail.get("auto_kick", True)
                if auto_kick_blacklisted and is_in_blacklist(group_id, user_id):
                    blacklist_info = get_blacklist_info(group_id, user_id)
                    reason = blacklist_info.get("reason", "黑名单用户")
                    
                    # 发送提示消息到群里
                    try:
                        platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                        if platform:
                            # 先发送提示消息
                            await platform.get_client().call_action(
                                action="send_group_msg",
                                group_id=int(group_id),
                                message=f"⚠️ 检测到黑名单用户: {user_id} ({user_name})\n📋 拉黑原因: {reason}"
                            )
                            logger.info(f"发送黑名单提示消息: {user_id} ({user_name})，原因: {reason}")
                            
                            # 然后踢出用户
                            await platform.get_client().call_action(
                                action="set_group_kick",
                                group_id=int(group_id),
                                user_id=int(user_id),
                                reject_add_request=False
                            )
                            logger.info(f"自动踢出黑名单用户: {user_id} ({user_name})，原因: {reason}")
                        else:
                            logger.error("无法获取平台实例")
                    except Exception as e:
                        logger.error(f"处理黑名单用户失败: {e}")
                    
                    return

            # 更新邀请状态为已入群
            if inviter_id:
                update_invite_status(group_id, user_id, "joined")
                logger.info(f"用户 {user_id}({user_name}) 加入群聊，邀请者: {inviter_id}")
            
            # 发送进群通知
            from .qqadmin.api_client import NapCatAPI
            api_client = NapCatAPI()
            
            # 如果有邀请者信息，发送邀请统计通知
            if inviter_id and inviter_id != "0":
                invite_count = get_inviter_count(group_id, inviter_id)
                await api_client.send_group_msg(group_id, f"🎉 {user_name} 加入了群聊\n🙋‍♂️ 邀请者: {inviter_id}\n📊 累计邀请: {invite_count} 人")
            
            welcome_msg = format_welcome_message(group_id, user_id, user_name)
            if welcome_msg:
                delay = get_welcome_delay(group_id)
                if delay > 0:
                    await asyncio.sleep(delay)
                result = await api_client.send_group_msg(group_id, welcome_msg)
                if result.get("success"):
                    message_id = result.get("message_id")
                    if message_id:
                        from .qqadmin.recall import is_self_recall_enabled, get_self_recall_time, schedule_self_recall
                        if is_self_recall_enabled(group_id):
                            recall_time = get_self_recall_time(group_id)
                            await schedule_self_recall(event.bot, group_id, message_id, recall_time)
            
            log_welcome(group_id, user_id, user_name, "auto")
        except Exception as e:
            logger.error(f"发送欢迎消息失败: {e}")

    def _is_admin_or_owner(self, event: AstrMessageEvent) -> bool:
        sender = event.message_obj.sender
        sender_id = sender.user_id if hasattr(sender, 'user_id') else ""
        if str(sender_id) in [str(oid) for oid in self.owner_ids]:
            return True
        if event.role in ["admin", "owner"]:
            return True
        group = event.message_obj.group
        if group:
            if group.group_owner and str(group.group_owner) == str(sender_id):
                return True
            if group.group_admins and str(sender_id) in [str(a) for a in group.group_admins]:
                return True
        return False

    async def _is_admin_or_owner_async(self, event: AstrMessageEvent) -> bool:
        """异步版本：通过 API 获取真实的群成员角色"""
        sender = event.message_obj.sender
        sender_id = sender.user_id if hasattr(sender, 'user_id') else ""
        
        logger.info(f"_is_admin_or_owner_async: sender_id={sender_id}, owner_ids={self.owner_ids}")
        
        # 先检查是否是机器人主人
        owner_ids_str = [str(oid) for oid in self.owner_ids]
        logger.info(f"_is_admin_or_owner_async: owner_ids_str={owner_ids_str}, sender_id_str={str(sender_id)}")
        if str(sender_id) in owner_ids_str:
            logger.info(f"_is_admin_or_owner_async: 用户 {sender_id} 是机器人主人")
            return True
        else:
            logger.info(f"_is_admin_or_owner_async: 用户 {sender_id} 不是机器人主人")
        
        # 检查是否是分群管理员
        group_id = str(event.message_obj.group_id)
        if group_id and group_id != "None":
            from .qqadmin.group_config import get_group_data
            sub_admins = get_group_data(group_id, "sub_admins", [])
            if str(sender_id) in [str(a) for a in sub_admins]:
                logger.info(f"_is_admin_or_owner_async: 用户 {sender_id} 是分群管理员")
                return True
        
        # 通过 API 获取真实角色
        logger.info(f"_is_admin_or_owner_async: group_id={group_id}")
        
        if group_id and group_id != "None":
            try:
                platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform:
                    member_info = await platform.get_client().call_action(
                        action="get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(sender_id)
                    )
                    role = member_info.get("role", "")
                    logger.info(f"_is_admin_or_owner_async: API返回角色: {role}")
                    if role in ["admin", "owner"]:
                        return True
            except Exception as e:
                logger.error(f"_is_admin_or_owner_async: 获取用户角色信息失败: {e}")
        
        # 降级检查 event.role 和 group 对象
        logger.info(f"_is_admin_or_owner_async: event.role={event.role}")
        if event.role in ["admin", "owner"]:
            return True
        group = event.message_obj.group
        if group:
            if group.group_owner and str(group.group_owner) == str(sender_id):
                return True
            if group.group_admins and str(sender_id) in [str(a) for a in group.group_admins]:
                return True
        logger.info(f"_is_admin_or_owner_async: 返回 False")
        return False

    def _is_group_admin_or_owner(self, event: AstrMessageEvent) -> bool:
        """只检查是否是群管理员或群主（不检查机器人主人）"""
        sender = event.message_obj.sender
        sender_id = sender.user_id if hasattr(sender, 'user_id') else ""
        
        # 首先尝试从事件中获取角色信息
        if event.role in ["admin", "owner"]:
            return True
        
        # 尝试从群对象中获取信息
        group = event.message_obj.group
        if group:
            if group.group_owner and str(group.group_owner) == str(sender_id):
                return True
            if group.group_admins and str(sender_id) in [str(a) for a in group.group_admins]:
                return True
        
        # 如果同步检查失败，尝试从消息对象的其他字段获取角色信息
        message_obj = event.message_obj
        if hasattr(message_obj, 'sender'):
            sender_info = message_obj.sender
            if hasattr(sender_info, 'role') and sender_info.role in ["admin", "owner"]:
                return True
        
        return False

    async def _is_group_admin_or_owner_async(self, event: AstrMessageEvent) -> bool:
        """异步版本：通过 API 获取真实的群成员角色"""
        sender = event.message_obj.sender
        sender_id = sender.user_id if hasattr(sender, 'user_id') else ""
        
        # 通过 API 获取真实角色
        group_id = str(event.message_obj.group_id)
        if group_id and group_id != "None":
            try:
                platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform:
                    member_info = await platform.get_client().call_action(
                        action="get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(sender_id)
                    )
                    role = member_info.get("role", "")
                    if role in ["admin", "owner"]:
                        return True
            except Exception as e:
                logger.error(f"获取用户角色信息失败: {e}")
        
        # 降级检查 event.role 和 group 对象
        if event.role in ["admin", "owner"]:
            return True
        group = event.message_obj.group
        if group:
            if group.group_owner and str(group.group_owner) == str(sender_id):
                return True
            if group.group_admins and str(sender_id) in [str(a) for a in group.group_admins]:
                return True
        return False

    def _check_qqadmin_feature(self, event: AstrMessageEvent, global_config: bool, feature_name: str) -> bool:
        """
        检查群管功能是否可用：
        1. 检查是否是机器人主人（主人不受任何限制）
        2. 检查分群开机状态（非主人用户受限制）
        3. 检查群管总开关是否开启
        4. 如果启用了授权系统，检查群组是否有授权
        5. 检查分群配置是否开启（优先使用分群配置）
        6. 如果分群没有单独配置，自动创建默认配置（所有功能关闭），然后使用全局配置
        """
        # 机器人主人不受任何限制
        if self._is_owner(event):
            logger.info(f"_check_qqadmin_feature: 用户是机器人主人，放行")
            return True

        # 检查分群开机状态（非主人用户受限制）
        group_id = str(event.message_obj.group_id)
        if group_id and group_id != "None":
            from .qqadmin.group_config import get_group_data
            features = get_group_data(group_id, "features", {})
            power_status = features.get("power_status", "")
            
            if power_status == "off":
                logger.info(f"_check_qqadmin_feature: 分群已关机，拒绝")
                return False

        is_whitelist_group = self._is_whitelist_group(event)
        logger.info(f"_check_qqadmin_feature: feature={feature_name}, is_whitelist={is_whitelist_group}, enable_group_manage={getattr(self, 'qqadmin_enable_group_manage', True)}")

        # 白名单群组不受任何限制
        if is_whitelist_group:
            logger.info(f"_check_qqadmin_feature: 白名单群组，放行")
            return True

        # 检查群管总开关
        if not getattr(self, 'qqadmin_enable_group_manage', True):
            logger.info(f"_check_qqadmin_feature: 群管总开关关闭，拒绝")
            return False

        # 如果启用了授权系统，检查群组是否有授权
        if self.enable_auth:
            if not self._is_authorized(event):
                logger.info(f"_check_qqadmin_feature: 授权检查失败，拒绝")
                return False

        # 检查分群配置
        if not group_id or group_id == "None":
            return global_config

        # 检查分群是否有自己的配置文件
        from .qqadmin.group_config import load_group_config, save_group_config, init_group_config, get_group_feature_setting
        group_config = load_group_config(group_id)
        
        # 分群没有配置文件，自动创建默认配置（所有功能关闭）
        if not group_config:
            group_config = init_group_config(group_id, self.config)
            save_group_config(group_id, group_config)
            logger.info(f"自动创建群组 {group_id} 默认配置，所有功能默认关闭")
            return False
        
        if group_config and group_config.get("features"):
            # 分群有单独的配置文件，只使用分群配置
            features = group_config.get("features", {})
            
            # 处理带有 _enabled 后缀的功能名称
            feature_name_check = feature_name
            if feature_name_check.endswith("_enabled"):
                feature_name_check = feature_name_check[:-8]
            
            # 检查分群中该功能是否启用（只从分群配置读取，不回退到全局）
            if feature_name_check in features:
                feature_enabled = features[feature_name_check].get("enabled", False)
                logger.info(f"_check_qqadmin_feature: 分群配置 feature={feature_name}, enabled={feature_enabled}")
                return feature_enabled
            
            # 分群配置中没有该功能，默认关闭
            logger.info(f"_check_qqadmin_feature: 分群配置无此功能，默认关闭 feature={feature_name}")
            return False

        # 分群配置不完整，默认关闭
        logger.info(f"_check_qqadmin_feature: 分群配置不完整，默认关闭 feature={feature_name}")
        return False

    def _is_whitelist_group(self, event: AstrMessageEvent) -> bool:
        """检查群组是否在白名单中"""
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            return False
        whitelist = getattr(self, 'whitelist_groups', [])
        return str(group_id) in [str(g) for g in whitelist]

    async def _send_welcome_message(self, event: AstrMessageEvent, group_id: str, user_id: str, user_name: str):
        """
        发送入群欢迎消息
        
        Args:
            event: AstrMessageEvent
            group_id: 群组ID
            user_id: 用户ID
            user_name: 用户名
        """
        try:
            from .qqadmin.welcome import (
                is_welcome_enabled, get_welcome_message, format_welcome_message
            )
            from astrbot.core.message.components import At, Plain
            from astrbot.core.message.message_event_result import MessageChain
            
            # 检查是否启用了入群欢迎
            if not is_welcome_enabled(group_id):
                # 如果没有配置入群欢迎，发送默认欢迎消息
                default_welcome = self.qqadmin_default_welcome_message.format(
                    user_name=user_name,
                    user_id=user_id
                )
                await event.send(event.plain_result(default_welcome))
                return
            
            # 获取格式化后的欢迎消息
            welcome_msg = format_welcome_message(group_id, user_id, user_name)
            if welcome_msg:
                # 检查是否需要艾特新成员
                from .qqadmin.welcome import is_at_new_member_enabled
                if is_at_new_member_enabled(group_id):
                    # 使用真正的艾特组件
                    chain = [At(qq=int(user_id)), Plain(" " + welcome_msg)]
                    await event.send(event.chain_result(chain))
                    logger.info(f"自定义欢迎消息发送成功（艾特 @{user_name}）")
                else:
                    await event.send(event.plain_result(welcome_msg))
                    logger.info(f"自定义欢迎消息发送成功")
            else:
                # 如果没有配置欢迎消息，使用默认
                default_welcome = self.qqadmin_default_welcome_message.format(
                    user_name=user_name,
                    user_id=user_id
                )
                await event.send(event.plain_result(default_welcome))
        except Exception as e:
            logger.error(f"发送入群欢迎消息失败: {e}")
            # 出错时发送默认欢迎消息（使用真正的艾特组件）
            try:
                from astrbot.core.message.components import At, Plain
                default_chain = [At(qq=int(user_id)), Plain(" 欢迎入群！")]
                await event.send(event.chain_result(default_chain))
            except Exception as e2:
                logger.error(f"发送默认欢迎消息也失败: {e2}")

    def _check_scum_feature(self, event: AstrMessageEvent, global_config: bool) -> bool:
        """
        检查SCUM查询功能是否可用（与群管功能共享授权系统）：
        1. 检查SCUM查询总开关
        2. 检查白名单群组（不受限制）
        3. 检查授权系统（与群管功能通用）
        4. 检查是否已绑定服务器或在白名单中
        """
        is_owner = self._is_owner(event)
        is_whitelist_group = self._is_whitelist_group(event)
        
        # 白名单群组和主人不受限制
        if is_whitelist_group or is_owner:
            return True
            
        # 检查全局SCUM查询开关
        if not global_config:
            return False
        
        # 如果启用了授权系统，检查群组是否有授权（与群管功能通用）
        if self.enable_auth:
            if not (is_owner and getattr(self, 'owner_ignore_auth', False)):
                if not self._is_authorized(event):
                    return False
            
        # 检查是否启用了绑定限制
        default_enabled = getattr(self, 'scum_default_enabled', True)
        if default_enabled:
            return True
            
        # 需要绑定才能使用
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            return False
            
        # 检查是否已绑定服务器
        binding = get_binding(group_id)
        return binding is not None

    @filter.on_decorating_result(priority=999)
    async def intercept_and_recall_bot_message(self, event: AstrMessageEvent):
        """
        拦截机器人发送的消息并安排撤回
        参考 astrbot_plugin_batchrecall 的实现方式
        使用 on_decorating_result 获取正确的 message_id
        """
        try:
            group_id = str(event.message_obj.group_id)
            if not group_id or group_id == "None":
                # 私聊场景
                group_id = str(event.message_obj.sender_id)
                if not group_id or group_id == "None":
                    return

            from .qqadmin.recall import is_self_recall_enabled, get_self_recall_time
            
            if not is_self_recall_enabled(group_id):
                return

            # 检查是否是 AiocqhttpMessageEvent
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            if not isinstance(event, AiocqhttpMessageEvent):
                return

            # 获取配置中的撤回时间
            recall_time = get_self_recall_time(group_id)
            logger.info(f"🎯 拦截到机器人消息，{recall_time}秒后撤回")

            # 获取原始消息链
            result = event.get_result()
            if not result or not result.chain:
                return

            original_chain = result.chain.copy()
            result.chain.clear()
            
            from astrbot.core.message.message_event_result import MessageChain
            message_chain = MessageChain(chain=original_chain)
            onebot_messages = await AiocqhttpMessageEvent._parse_onebot_json(message_chain)
            
            if not onebot_messages:
                return

            is_group = bool(event.message_obj.group_id and str(event.message_obj.group_id) != "None")
            session_id = str(event.message_obj.group_id) if is_group else str(event.message_obj.sender_id)

            try:
                if is_group:
                    send_result = await event.bot.call_action(
                        "send_group_msg",
                        group_id=int(session_id),
                        message=onebot_messages,
                    )
                else:
                    send_result = await event.bot.call_action(
                        "send_private_msg",
                        user_id=int(session_id),
                        message=onebot_messages,
                    )
            except Exception as send_exc:
                logger.error(f"发送消息失败: {send_exc}")
                return

            message_id = None
            if isinstance(send_result, dict):
                message_id = send_result.get("message_id")

            if not message_id:
                logger.error("❌ 发送消息失败，无法获取消息ID")
                return

            logger.info(f"📤 发送成功，获取到消息ID: {message_id}")
            from .qqadmin.recall import _safe_int
            task = asyncio.create_task(
                self._recall_bot_message(event.bot, _safe_int(message_id, message_id), recall_time)
            )
            task.add_done_callback(self._remove_recall_task)
            self._recall_tasks.add(task)
            logger.info(f"✅ 已安排消息在 {recall_time} 秒后撤回")

        except Exception as e:
            logger.error(f"消息拦截处理失败: {e}")

    def _remove_recall_task(self, task: asyncio.Task):
        """移除已完成的任务"""
        self._recall_tasks.discard(task)

    async def _recall_bot_message(self, bot, message_id: int, recall_seconds: int):
        """撤回机器人的消息"""
        try:
            await asyncio.sleep(recall_seconds)
            await bot.delete_msg(message_id=message_id)
            logger.info(f"✅ 已自动撤回消息: {message_id}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"❌ 自动撤回失败: {e}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cache_group_messages(self, event: AstrMessageEvent) -> None:
        """保存所有群消息到本地缓存，用于撤回功能"""
        try:
            group_id = str(event.message_obj.group_id)
            if not group_id or group_id == "None":
                return

            user_id = str(event.message_obj.sender.user_id)
            message_id = getattr(event.message_obj, "message_id", 0) or 0
            message_content = event.message_str or ""

            # 处理 message_id：支持十进制整数和十六进制字符串
            if isinstance(message_id, str):
                message_id = message_id.strip()
                if message_id.startswith('0x') or message_id.startswith('0X'):
                    message_id = int(message_id, 16)
                elif message_id.isdigit():
                    message_id = int(message_id)
                else:
                    # 保留原始字符串（十六进制格式）
                    pass
            elif not isinstance(message_id, int):
                message_id = str(message_id)

            # 保存到本地缓存
            from .qqadmin.message_cache import add_group_message
            import re
            content = re.sub(r'\[CQ:.*?\]', '', message_content).strip()

            add_group_message(
                group_id,
                message_id,
                user_id,
                content,
                int(time.time())
            )
            logger.debug(f"已缓存消息: group_id={group_id}, user_id={user_id}, message_id={message_id}")

        except Exception as e:
            logger.error(f"缓存群消息时出错: {e}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_auto_reply(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            return

        # 主人不受关机状态限制
        if not self._is_owner(event):
            # 检查分群开机状态
            from .qqadmin.group_config import get_group_data
            features = get_group_data(group_id, "features", {})
            power_status = features.get("power_status", "")
            if power_status == "off":
                return

        # 优先使用分群配置，如果分群未设置则使用全局配置
        group_setting = get_group_feature_setting(group_id, "reply_enabled", global_config=self.config)
        # 如果分群设置了值，使用分群设置；否则使用全局的 qqadmin_enable_reply
        if group_setting is None:
            enable_reply = self.qqadmin_enable_reply
        else:
            enable_reply = group_setting

        if not enable_reply:
            return

        message = (event.message_str or "").strip()
        if not message:
            return

        keyword, reply_data = match_any_reply(group_id, message)
        if not keyword or not reply_data:
            return

        reply_text = reply_data.get("reply")
        if "random_replies" in reply_data:
            import random
            reply_text = random.choice(reply_data["random_replies"])

        if not reply_text:
            return

        if is_in_cooldown(group_id, keyword):
            return

        set_cooldown(group_id, keyword)
        yield event.plain_result(reply_text)

    @filter.event_message_type(filter.EventMessageType.GROUP_REQUEST)
    async def handle_group_request_auto_audit(self, event: AstrMessageEvent) -> None:
        raw_event = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw_event, dict) and raw_event.get("post_type") == "request":
            await self._process_group_request(event, raw_event)
    
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_group_member_decrease(self, event: AstrMessageEvent) -> None:
        """处理群成员退出事件，自动将退群用户加入黑名单"""
        raw_event = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw_event, dict):
            return
        
        post_type = raw_event.get("post_type", "")
        notice_type = raw_event.get("notice_type", "")
        
        # 只处理群成员退出事件
        if post_type != "notice" or notice_type != "group_decrease":
            return
        
        group_id = str(raw_event.get("group_id", ""))
        user_id = str(raw_event.get("user_id", ""))
        sub_type = raw_event.get("sub_type", "")
        
        if not group_id or not user_id:
            return
        
        # 检查分群开机状态
        from .qqadmin.group_config import get_group_data
        features = get_group_data(group_id, "features", {})
        power_status = features.get("power_status", "")
        if power_status == "off":
            return
        
        # 每次处理事件前重新加载配置，确保配置更改立即生效
        self._reload_plugin_config()
        
        # 定义内部的 get_nested 函数
        def get_nested(data, key1, key2=None, key3=None, key4=None, default=None):
            if key1 not in data:
                return default
            if key2 is None:
                return data.get(key1, default)
            if key2 not in data[key1]:
                return default
            if key3 is None:
                return data[key1].get(key2, default)
            if key3 not in data[key1][key2]:
                return default
            if key4 is None:
                return data[key1][key2].get(key3, default)
            if key4 not in data[key1][key2][key3]:
                return default
            return data[key1][key2][key3].get(key4, default)
        
        # 使用分群配置检查黑名单功能
        from .qqadmin.group_config import is_feature_enabled, get_group_feature_detail
        enable_blacklist = is_feature_enabled(group_id, "blacklist", self.config)
        
        blacklist_detail = get_group_feature_detail(group_id, "blacklist", self.config)
        auto_blacklist_on_leave = blacklist_detail.get("auto_blacklist_on_leave", False)
        
        logger.info(f"检测到群成员退出事件: group_id={group_id}, user_id={user_id}, sub_type={sub_type}, enable_blacklist={enable_blacklist}, auto_blacklist_on_leave={auto_blacklist_on_leave}")
        
        # 只处理主动退群的情况，不处理被踢的情况
        if sub_type == "leave":
            # 清理审核队列中的记录（无论是否启用黑名单）
            from .qqadmin.audit import remove_pending_request
            remove_pending_request(group_id, user_id)
            
            # 更新邀请状态为已退群
            update_invite_status(group_id, user_id, "left")
            logger.info(f"更新邀请状态: user_id={user_id}, group_id={group_id}, status=left")
            
            # 获取用户昵称
            user_name = ""
            try:
                platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform:
                    result = await platform.get_client().call_action(
                        action="get_stranger_info",
                        user_id=int(user_id)
                    )
                    if result and "nickname" in result:
                        user_name = result["nickname"]
            except Exception as e:
                logger.error(f"获取退群用户昵称失败: {e}")
            
            # 如果启用了自动退群加入黑名单，则将用户加入黑名单
            if auto_blacklist_on_leave and enable_blacklist:
                from .qqadmin.blacklist import add_to_blacklist
                add_to_blacklist(group_id, user_id, reason="主动退群", operator_id="system", ban_type="permanent")
                logger.info(f"自动将退群用户加入黑名单: user_id={user_id}, group_id={group_id}")
                
                # 发送提示消息到群里
                try:
                    display_name = user_name if user_name else user_id
                    await event.send(event.plain_result(f"⚠️ 用户 {display_name}({user_id}) 主动退群，已加入黑名单"))
                except Exception as e:
                    logger.error(f"发送退群黑名单提示消息失败: {e}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_blacklist_check(self, event: AstrMessageEvent) -> None:
        """在用户发送消息时检测是否为黑名单用户，若是则踢出"""
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            return
        
        # 主人不受黑名单检测限制
        if self._is_owner(event):
            return
        
        # 检查分群开机状态
        from .qqadmin.group_config import get_group_data
        features = get_group_data(group_id, "features", {})
        power_status = features.get("power_status", "")
        if power_status == "off":
            return
        
        # 检查分群配置中黑名单功能是否启用
        from .qqadmin.group_config import is_feature_enabled, get_group_feature_detail
        if not is_feature_enabled(group_id, "blacklist", self.config):
            logger.info(f"黑名单检测: 分群 {group_id} 黑名单功能未启用，跳过检测")
            return
        
        # 获取分群配置的自动踢人设置
        blacklist_detail = get_group_feature_detail(group_id, "blacklist", self.config)
        auto_kick_blacklisted = blacklist_detail.get("auto_kick", True)
        
        logger.info(f"黑名单检测: group_id={group_id}, auto_kick_blacklisted={auto_kick_blacklisted}")
        
        if not auto_kick_blacklisted:
            logger.info("黑名单自动踢人未启用，跳过检测")
            return
        
        user_id = str(event.message_obj.sender.user_id)
        
        # 检查用户是否是机器人主人，如果是则跳过检测
        owner_ids_str = [str(oid) for oid in self.owner_ids]
        if str(user_id) in owner_ids_str:
            logger.info(f"用户 {user_id} 是机器人主人，跳过黑名单检测")
            return
        
        # 检查用户是否是管理员或群主，如果是则跳过检测
        is_admin_or_owner = False
        user_not_in_group = False
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if platform:
                member_info = await platform.get_client().call_action(
                    action="get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id)
                )
                role = member_info.get("role", "")
                if role in ["admin", "owner"]:
                    is_admin_or_owner = True
                    logger.info(f"用户 {user_id} 是管理员或群主（role={role}），跳过黑名单检测")
        except Exception as e:
            # 检查是否是用户不在群里的错误（retcode=1200）
            error_str = str(e)
            if "1200" in error_str or "不存在" in error_str or "not exist" in error_str.lower():
                logger.info(f"用户 {user_id} 不在群 {group_id} 中，跳过黑名单检测")
                user_not_in_group = True
            else:
                logger.error(f"获取用户角色信息失败: {e}")
        
        if is_admin_or_owner or user_not_in_group:
            return
        
        logger.info(f"检测用户 {user_id} 在群 {group_id} 是否在黑名单中")
        
        from .qqadmin.blacklist import is_in_blacklist, get_blacklist_info, load_blacklist
        
        # 添加调试信息
        blacklist_data = load_blacklist(group_id)
        logger.info(f"黑名单原始数据: {json.dumps(blacklist_data, ensure_ascii=False)}")
        
        if is_in_blacklist(group_id, user_id):
            blacklist_info = get_blacklist_info(group_id, user_id)
            reason = blacklist_info.get("reason", "黑名单用户")
            logger.info(f"用户 {user_id} 在黑名单中，信息: {json.dumps(blacklist_info, ensure_ascii=False)}")
            
            try:
                platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform:
                    # 先发送提示消息
                    await platform.get_client().call_action(
                        action="send_group_msg",
                        group_id=int(group_id),
                        message=f"⚠️ 检测到黑名单用户: {user_id}\n📋 拉黑原因: {reason}"
                    )
                    logger.info(f"发送黑名单提示消息: {user_id}，原因: {reason}")
                    
                    # 然后踢出用户
                    await platform.get_client().call_action(
                        action="set_group_kick",
                        group_id=int(group_id),
                        user_id=int(user_id),
                        reject_add_request=False
                    )
                    logger.info(f"自动踢出黑名单用户: {user_id} 从群 {group_id}，原因: {reason}")
                    
                    # 停止事件传播，防止重复处理
                    event.stop_event()
                else:
                    logger.error("无法获取平台实例")
            except Exception as e:
                logger.error(f"处理黑名单用户失败: {e}")
        else:
            logger.info(f"用户 {user_id} 不在黑名单中")
    
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_audit_approval(self, event: AstrMessageEvent) -> None:
        if not self._check_qqadmin_feature(event, self.qqadmin_enable_audit, "audit_enabled"):
            return

        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            return

        message = (event.message_str or "").strip()
        if not message.startswith("同意"):
            return

        code = message[2:].strip()
        if not code or len(code) < 4:
            return

        from .qqadmin.audit import get_pending_by_code
        pending_info = get_pending_by_code(group_id, code)
        if not pending_info:
            return

        user_id = pending_info.get("user_id")
        flag = pending_info.get("flag", "")
        
        try:
            # 调用QQ API同意入群请求
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if platform:
                await platform.get_client().call_action(
                    action="set_group_add_request",
                    flag=flag,
                    sub_type="add",
                    approve=True
                )
                
                if approve_join_request(group_id, user_id, str(event.message_obj.sender.user_id), "验证码审批通过"):
                    add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                                      "审核通过", user_id, "", "同意+验证码审批")
                    user_name = pending_info.get('user_name', '')
                    display_name = user_name if user_name and user_name != user_id else ''
                    yield event.plain_result(f"✅ 已批准用户 {display_name}({user_id}) 入群")
                    logger.info(f"管理员 {event.message_obj.sender.user_id} 同意用户 {user_id} 入群，审批码: {code}")
            else:
                yield event.plain_result("❌ 无法获取平台实例")
        except Exception as e:
            logger.error(f"同意入群失败: {e}")
            yield event.plain_result("❌ 同意入群失败")
        
        event.stop_event()  # 停止事件传播，避免重复处理

    @filter.command("绑定id")
    async def bind_server_id(self, event: AstrMessageEvent, server_id: str = "") -> None:
        if not await self._is_slash_prefix_allowed_async(event) or not self.enable_binding:
            return
        if not self._is_owner(event) and not await self._is_admin_or_owner_async(event):
            if self.scum_permission_mode == "read_only":
                yield event.plain_result("❌ 当前SCUM权限模式为只读，不允许修改绑定。")
                return
        async for result in self._bind_server(event, server_id):
            yield result

    async def _process_group_invite_notice(self, event: AstrMessageEvent, raw_event: dict):
        logger.info(f"_process_group_invite_notice 被调用，raw_event: {raw_event}")
        
        group_id = str(raw_event.get("group_id", ""))
        inviter_id = str(raw_event.get("operator_id", ""))
        invitee_id = str(raw_event.get("user_id", ""))
        
        logger.info(f"入群邀请通知: 邀请者 {inviter_id}, 被邀请者 {invitee_id}, 群 {group_id}")
        
        # 记录邀请（统计功能）
        if self._check_qqadmin_feature(event, self.qqadmin_enable_stats, "stats_enabled"):
            record_invitation(group_id, inviter_id, invitee_id, invitee_id)
            logger.info(f"成功记录入群邀请: 邀请者 {inviter_id}, 被邀请者 {invitee_id}")
        
        # 邀请审核（如果启用）
        if self._check_qqadmin_feature(event, self.qqadmin_enable_invite_audit, "invite_audit_enabled"):
            from .qqadmin.audit import add_audit_request, get_audit_settings
            
            # 检查邀请者是否是管理员或群主
            is_admin_or_owner = False
            try:
                platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform:
                    member_info = await platform.get_client().call_action(
                        action="get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(inviter_id)
                    )
                    role = member_info.get("role", "")
                    if role in ["admin", "owner"]:
                        is_admin_or_owner = True
                        logger.info(f"邀请者 {inviter_id} 是管理员或群主（role={role}），跳过验证")
            except Exception as e:
                logger.error(f"获取邀请者角色信息失败: {e}")
            
            if is_admin_or_owner:
                # 管理员或群主邀请，直接通过，不需要验证
                logger.info(f"管理员或群主邀请，直接通过: 邀请者 {inviter_id}, 被邀请者 {invitee_id}")
                
                # 更新邀请状态为已入群
                update_invite_status(group_id, invitee_id, "joined")
                logger.info(f"更新邀请状态: user_id={invitee_id}, group_id={group_id}, status=joined")
                
                yield event.plain_result(
                    f"✅ 用户 {invitee_id} 被 {inviter_id}（管理员/群主）邀请入群\n"
                    f"🎉 欢迎加入群聊！"
                )
                return
            
            settings = get_audit_settings(group_id)
            approval_mode = settings.get("approval_mode", "direct")
            
            if approval_mode != "direct":
                # 需要验证，添加到审核队列
                result = add_audit_request(group_id, invitee_id, invitee_id, "", "invite")
                
                if result.get("success"):
                    approval_code = result.get("approval_code", "")
                    approval_mode = result.get("approval_mode", "")
                    question = result.get("challenge_info", {}).get("question", "")
                    verify_id = result.get("challenge_info", {}).get("verify_id", "")
                    
                    # 从分群配置获取超时时间
                    from .qqadmin.audit import get_verification_timeout
                    timeout = get_verification_timeout(group_id)
                    
                    try:
                        if question:
                            # 数学验证：显示问题和审批码
                            await event.send(event.plain_result(
                                f"📝 用户 {invitee_id} 被 {inviter_id} 邀请入群\n"
                                f"🔢 请完成验证:\n{question}\n"
                                f"⏱️ 请在 {timeout // 60} 分钟内完成验证\n"
                                f"💡 回答格式: 审批{approval_code}"
                            ))
                        elif verify_id:
                            # ID验证：显示验证ID
                            await event.send(event.plain_result(
                                f"📝 用户 {invitee_id} 被 {inviter_id} 邀请入群\n"
                                f"🔢 验证码: {verify_id}\n"
                                f"⏱️ 请在 {timeout // 60} 分钟内完成验证\n"
                                f"💡 直接输入验证码即可"
                            ))
                        else:
                            # 其他验证：显示审批码
                            await event.send(event.plain_result(
                                f"📝 用户 {invitee_id} 被 {inviter_id} 邀请入群\n"
                                f"🔢 验证码: {approval_code}\n"
                                f"⏱️ 请在 {timeout // 60} 分钟内完成验证\n"
                                f"💡 回答格式: 审批{approval_code}"
                            ))
                        logger.info(f"邀请审核消息发送成功: user_id={invitee_id}, code={verify_id or approval_code}, mode={approval_mode}")
                    except Exception as e:
                        logger.error(f"发送邀请审核消息失败: {e}")
                else:
                    logger.info(f"邀请审核失败: {result.get('message')}")
                return
        
        # 发送欢迎消息（使用欢迎系统）
        if self._check_qqadmin_feature(event, self.qqadmin_enable_welcome, "welcome_enabled"):
            from .qqadmin.welcome import is_welcome_enabled, format_welcome_message, log_welcome, is_at_new_member_enabled
            from astrbot.core.message.components import At, Plain
            from astrbot.core.message.message_event_result import MessageChain
            
            if is_welcome_enabled(group_id):
                # 获取自定义欢迎消息（支持占位符）
                welcome_msg = format_welcome_message(group_id, invitee_id, invitee_id)
                
                if welcome_msg:
                    try:
                        # 检查是否需要艾特新成员
                        if is_at_new_member_enabled(group_id):
                            # 使用真正的艾特组件
                            chain = [At(qq=int(invitee_id)), Plain(" " + welcome_msg)]
                            await event.send(event.chain_result(chain))
                        else:
                            await event.send(event.plain_result(welcome_msg))
                        logger.info("自定义欢迎消息发送成功")
                        log_welcome(group_id, invitee_id, invitee_id, "invite")
                    except Exception as e:
                        logger.error(f"发送欢迎消息失败: {e}")
                else:
                    # 如果没有自定义欢迎消息，发送默认消息（使用艾特组件）
                    try:
                        chain = [At(qq=int(invitee_id)), Plain(
                            f" 🎉 欢迎加入群聊！\n"
                            f"📊 邀请记录已更新"
                        )]
                        await event.send(event.chain_result(chain))
                        logger.info("默认欢迎消息发送成功（艾特）")
                    except Exception as e:
                        logger.error(f"发送默认欢迎消息失败: {e}")
    
    async def _process_group_request(self, event: AstrMessageEvent, raw_event: dict) -> None:
        logger.info(f"_process_group_request 被调用，raw_event: {raw_event}")
        
        group_id = str(raw_event.get("group_id", ""))
        logger.info(f"group_id: {group_id}")
        
        if not group_id or group_id == "None":
            logger.info("group_id为空或None，返回")
            return
        
        request_type = raw_event.get("request_type", "")
        logger.info(f"request_type: {request_type}")
        
        if request_type != "group":
            logger.info(f"request_type不是group，返回")
            return
        
        sub_type = raw_event.get("sub_type", "")
        user_id = str(raw_event.get("user_id", ""))
        flag = raw_event.get("flag", "")
        
        # 尝试从多个字段获取用户名
        user_name = raw_event.get("nickname", "") or raw_event.get("comment", "")
        
        # 如果无法从事件中获取用户名，尝试通过API获取
        if not user_name or user_name == user_id:
            try:
                platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform:
                    # 尝试获取用户资料
                    user_info = await platform.get_client().call_action(
                        action="get_stranger_info",
                        user_id=int(user_id)
                    )
                    if user_info:
                        user_name = user_info.get("nickname", user_id)
                        logger.info(f"通过API获取到用户昵称: {user_name}")
            except Exception as e:
                logger.warning(f"获取用户昵称失败: {e}")
                user_name = user_id
        
        if not user_name:
            user_name = user_id
            
        logger.info(f"sub_type: {sub_type}, user_id: {user_id}, user_name: {user_name}, flag: {flag}")
        
        # 邀请请求：统计系统独立处理
        if sub_type == "invite":
            logger.info("检测到邀请请求")
            if not self._check_qqadmin_feature(event, self.qqadmin_enable_stats, "stats_enabled"):
                logger.info("统计功能未启用，返回")
                return
            
            inviter_id = str(raw_event.get("inviter", ""))
            invitee_id = user_id
            invitee_name = raw_event.get("nickname", invitee_id)
            
            logger.info(f"准备记录邀请: 邀请者 {inviter_id}, 被邀请者 {invitee_id}({invitee_name})")
            record_invitation(group_id, inviter_id, invitee_id, invitee_name)
            logger.info(f"成功记录群邀请请求: 邀请者 {inviter_id}, 被邀请者 {invitee_id}({invitee_name})")
            
            try:
                await event.send(event.plain_result(
                    f"📤 用户 {invitee_name} 被 {inviter_id} 邀请入群\n"
                    f"⏱️ 请等待管理员审批"
                ))
                logger.info("邀请请求消息发送成功")
            except Exception as e:
                logger.error(f"发送邀请请求消息失败: {e}")
            return
        
        # 入群申请：需要审核功能
        if not self._check_qqadmin_feature(event, self.qqadmin_enable_audit, "audit_enabled"):
            return
        
        if sub_type == "add":
            # 先检查用户是否已经在群里了
            try:
                platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform:
                    member_info = await platform.get_client().call_action(
                        action="get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(user_id)
                    )
                    if member_info and member_info.get("group_id"):
                        logger.info(f"用户 {user_id} 已在群 {group_id} 中，忽略重复入群请求")
                        return
            except Exception as e:
                logger.info(f"检查用户是否在群中失败（正常情况）: {e}")
            
            # 检查用户是否已经在待验证队列中，如果是则跳过
            from .qqadmin.audit import get_pending_verification
            existing_pending = get_pending_verification(group_id, user_id)
            if existing_pending:
                logger.info(f"用户 {user_id} 已在待验证队列中，跳过重复处理")
                return
            
            # 重新加载配置
            self._reload_plugin_config()
            
            # 重新读取配置值
            def get_nested(key1, key2=None, key3=None, default=None):
                if key1 not in self.config:
                    return default
                
                val = self.config[key1]
                
                if isinstance(val, dict) and 'items' in val:
                    val = val['items']
                
                if not key2:
                    if isinstance(val, dict) and 'default' in val:
                        return val['default']
                    return val if val is not None else default
                
                if not isinstance(val, dict) or key2 not in val:
                    return default
                
                val = val[key2]
                
                if isinstance(val, dict) and 'items' in val:
                    val = val['items']
                
                if not key3:
                    if isinstance(val, dict) and 'default' in val:
                        return val['default']
                    return val if val is not None else default
                
                if not isinstance(val, dict) or key3 not in val:
                    return default
                
                val = val[key3]
                
                if isinstance(val, dict) and 'default' in val:
                    return val['default']
                return val if val is not None else default
            
            # 使用分群配置检查黑名单功能
            from .qqadmin.group_config import is_feature_enabled
            from .qqadmin.blacklist import is_in_blacklist, get_blacklist_info
            
            enable_blacklist = is_feature_enabled(group_id, "blacklist", self.config)
            if enable_blacklist and is_in_blacklist(group_id, user_id):
                blacklist_info = get_blacklist_info(group_id, user_id)
                reason = blacklist_info.get("reason", "黑名单用户")
                
                # 拒绝入群申请
                try:
                    platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                    if platform:
                        await platform.get_client().call_action(
                            action="set_group_add_request",
                            flag=flag,
                            sub_type="add",
                            approve=False,
                            reason=f"您在黑名单中，原因: {reason}"
                        )
                        logger.info(f"拒绝黑名单用户入群申请: user_id={user_id}, group_id={group_id}, reason={reason}")
                        
                        # 发送通知
                        try:
                            await event.send(event.plain_result(
                                f"⚠️ 已拒绝用户 {user_id} 的入群申请\n"
                                f"📋 原因: 用户在黑名单中\n"
                                f"🔍 拉黑原因: {reason}"
                            ))
                        except Exception as e:
                            logger.error(f"发送拒绝通知失败: {e}")
                except Exception as e:
                    logger.error(f"拒绝黑名单用户入群申请失败: {e}")
                return
            
            from .qqadmin.audit import get_audit_settings, get_audit_approval_mode, add_audit_request
            settings = get_audit_settings(group_id)
            approval_mode = get_audit_approval_mode(group_id)
            
            # 检查申请者是否是管理员或群主（虽然管理员/群主一般不会自己申请进群，但为了完整性进行检查）
            is_admin_or_owner = False
            try:
                platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform:
                    member_info = await platform.get_client().call_action(
                        action="get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(user_id)
                    )
                    role = member_info.get("role", "")
                    if role in ["admin", "owner"]:
                        is_admin_or_owner = True
                        logger.info(f"申请者 {user_id} 是管理员或群主（role={role}），直接通过")
            except Exception as e:
                logger.error(f"获取申请者角色信息失败: {e}")
            
            if is_admin_or_owner:
                # 管理员或群主申请，直接通过，不需要验证
                try:
                    platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                    if platform:
                        await platform.get_client().call_action(
                            action="set_group_add_request",
                            flag=flag,
                            sub_type="add",
                            approve=True
                        )
                        logger.info(f"管理员或群主申请，直接通过: user_id={user_id}, group_id={group_id}")
                        
                        # 发送欢迎消息
                        try:
                            await event.send(event.plain_result(f"✅ 管理员/群主 {user_name} 已入群\n🎉 欢迎回来！"))
                        except Exception as e:
                            logger.error(f"发送欢迎消息失败: {e}")
                    else:
                        logger.error("无法获取平台实例")
                except Exception as e:
                    logger.error(f"批准管理员/群主入群请求失败: {e}")
                return
            
            # 导入需要的模块
            from .qqadmin.api_client import NapCatAPI
            
            if approval_mode == "direct":
                try:
                    platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                    if platform:
                        await platform.get_client().call_action(
                            action="set_group_add_request",
                            flag=flag,
                            sub_type="add",
                            approve=True
                        )
                        logger.info(f"直接批准入群请求: user_id={user_id}, group_id={group_id}")
                        # 直接模式，添加审核请求记录（approval_mode=direct）
                        from .qqadmin.audit import add_audit_request, approve_join_request
                        add_audit_request(group_id, user_id, user_name, "", "normal", flag=flag)
                        approve_join_request(group_id, user_id, "system", "直接入群")
                        # 直接发送欢迎消息
                        await self._send_welcome_message(event, group_id, user_id, user_name)
                    else:
                        logger.error("无法获取平台实例")
                except Exception as e:
                    logger.error(f"直接批准入群请求失败: {e}")
                return
            
            # 获取审批模式
            from .qqadmin.audit import get_audit_approval_mode
            approval_mode = get_audit_approval_mode(group_id)
            
            # 根据审批模式处理入群请求
            if approval_mode == "code":
                # 管理员审批模式：不批准入群请求，等待管理员审批后才入群
                result = add_audit_request(group_id, user_id, user_name, "", "normal", flag=flag)
                if not result.get("success"):
                    logger.error(f"添加审核请求失败: {result.get('message', '未知错误')}")
                    return
                
                approval_code = result.get("approval_code", "")
                
                from .qqadmin.audit import get_verification_timeout
                timeout = get_verification_timeout(group_id)
                
                # 发送通知给管理员（不批准入群，等待管理员审批）
                await event.send(event.plain_result(
                    f"🔔 新的入群申请！\n"
                    f"📋 用户: {user_name}({user_id})\n"
                    f"🔢 审批码: {approval_code}\n"
                    f"⏱️ 请在 {timeout // 60} 分钟内处理\n"
                    f"💡 /同意 {approval_code} - 同意入群\n"
                    f"💡 /拒绝 {approval_code} - 拒绝入群"
                ))
                logger.info(f"用户 {user_name}({user_id}) 申请入群，等待管理员审批，审批码: {approval_code}")
            else:
                # 自主验证模式（math、id、direct）：直接处理，不使用审核队列
                challenge_info = None
                if approval_mode == "math":
                    from .qqadmin.audit import create_math_challenge
                    challenge_info = create_math_challenge(group_id, user_id)
                elif approval_mode == "id":
                    from .qqadmin.audit import create_id_challenge
                    challenge_info = create_id_challenge(group_id, user_id)
                
                # 确保 challenge_info 不为 None
                if challenge_info is None:
                    challenge_info = {}
                
                question = challenge_info.get("question", "")
                verify_id = challenge_info.get("verify_id", "")
                
                # 获取最大验证尝试次数
                from .qqadmin.audit import get_max_verify_attempts
                max_attempts = get_max_verify_attempts(group_id)
                
                try:
                    if approval_mode == "math" and question:
                        # 算数验证：先批准入群，然后让用户验证
                        try:
                            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                            if platform:
                                await platform.get_client().call_action(
                                    action="set_group_add_request",
                                    flag=flag,
                                    sub_type="add",
                                    approve=True
                                )
                                logger.info(f"已批准入群请求，等待算数验证: user_id={user_id}, group_id={group_id}")
                            else:
                                logger.error("无法获取平台实例")
                                return
                        except Exception as e:
                            logger.error(f"批准入群请求失败: {e}")
                            return
                        
                        # 将用户添加到待验证队列（传入已生成的 challenge_info 和 approval_mode）
                        from .qqadmin.audit import add_audit_request, get_verification_timeout
                        result = add_audit_request(group_id, user_id, user_name, "", "normal", flag=flag, challenge_info=challenge_info, approval_mode=approval_mode)
                        if result.get("success"):
                            logger.info(f"已将用户 {user_name}({user_id}) 添加到待验证队列")
                        else:
                            logger.error(f"添加待验证队列失败: {result.get('message', '未知错误')}")
                            
                        # 从分群配置获取超时时间
                        timeout = get_verification_timeout(group_id)
                        
                        # 使用真正的艾特组件发送验证消息
                        from astrbot.core.message.components import At, Plain
                        chain = [At(qq=int(user_id)), Plain(
                            f" 欢迎加入群聊！\n"
                            f"🔢 请完成验证:\n{question}\n"
                            f"⏱️ 请在 {timeout // 60} 分钟内完成验证\n"
                            f"💡 直接输入答案即可\n"
                            f"⚠️ 验证失败最多{max_attempts}次，超过将被移出群聊"
                        )]
                        await event.send(event.chain_result(chain))
                        logger.info(f"用户 {user_name}({user_id}) 申请入群，算数验证: {question}")
                    elif approval_mode == "id" and verify_id:
                        # ID验证：先批准入群，然后让用户验证
                        try:
                            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                            if platform:
                                await platform.get_client().call_action(
                                    action="set_group_add_request",
                                    flag=flag,
                                    sub_type="add",
                                    approve=True
                                )
                                logger.info(f"已批准入群请求，等待ID验证: user_id={user_id}, group_id={group_id}")
                            else:
                                logger.error("无法获取平台实例")
                                return
                        except Exception as e:
                            logger.error(f"批准入群请求失败: {e}")
                            return
                        
                        # 将用户添加到待验证队列（传入已生成的 challenge_info 和 approval_mode）
                        from .qqadmin.audit import add_audit_request, get_verification_timeout
                        result = add_audit_request(group_id, user_id, user_name, "", "normal", flag=flag, challenge_info=challenge_info, approval_mode=approval_mode)
                        if result.get("success"):
                            logger.info(f"已将用户 {user_name}({user_id}) 添加到待验证队列")
                        else:
                            logger.error(f"添加待验证队列失败: {result.get('message', '未知错误')}")
                            
                        # 从分群配置获取超时时间
                        timeout = get_verification_timeout(group_id)
                        
                        # 使用真正的艾特组件发送验证消息
                        from astrbot.core.message.components import At, Plain
                        chain = [At(qq=int(user_id)), Plain(
                            f" 欢迎加入群聊！\n"
                            f"🔢 验证ID: {verify_id}\n"
                            f"⏱️ 请在 {timeout // 60} 分钟内完成验证\n"
                            f"💡 直接输入数字即可\n"
                            f"⚠️ 验证失败最多{max_attempts}次，超过将被移出群聊"
                        )]
                        await event.send(event.chain_result(chain))
                        logger.info(f"用户 {user_name}({user_id}) 申请入群，验证ID: {verify_id}")
                except Exception as e:
                    logger.error(f"发送入群申请审核消息失败: {e}")
    
    @filter.command("解绑")
    async def unbind_server_id(self, event: AstrMessageEvent) -> None:
        if not await self._is_slash_prefix_allowed_async(event) or not self.enable_binding:
            return
        if not self._is_owner(event) and not await self._is_admin_or_owner_async(event):
            if self.scum_permission_mode == "read_only":
                yield event.plain_result("❌ 当前SCUM权限模式为只读，不允许修改绑定。")
                return
        async for result in self._unbind_server(event):
            yield result

    @filter.command("绑定")
    async def bind_server_cmd(self, event: AstrMessageEvent, server_id: str = "") -> None:
        if not await self._is_slash_prefix_allowed_async(event) or not self.enable_binding:
            return
        if not self._is_owner(event) and not await self._is_admin_or_owner_async(event):
            if self.scum_permission_mode == "read_only":
                yield event.plain_result("❌ 当前SCUM权限模式为只读，不允许修改绑定。")
                return
        async for result in self._bind_server(event, server_id):
            yield result

    async def _bind_server(self, event: AstrMessageEvent, server_id: str = "") -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        if not self._is_admin_group(event.message_obj.group_id):
            yield event.plain_result("❌ 该群组不允许使用绑定功能。")
            return
        if not server_id or not server_id.strip().isdigit():
            yield event.plain_result("❌ 请提供有效的服务器ID（纯数字）。\n例如：/绑定id 38615532")
            return
        server_id = server_id.strip()
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        bindings = load_bindings()
        if group_id in bindings:
            old_server_id = bindings[group_id]
            yield event.plain_result(f"⚠️ 该群组已绑定服务器ID：{old_server_id}\n如需更换请先解绑。")
            return
        bindings[group_id] = server_id
        save_bindings(bindings)
        yield event.plain_result(f"✅ 成功绑定服务器ID：{server_id}\n群成员可使用 查询在线 查询状态。")

    async def _unbind_server(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        if not self._is_admin_group(event.message_obj.group_id):
            yield event.plain_result("❌ 该群组不允许使用解绑功能。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        bindings = load_bindings()
        if group_id not in bindings:
            yield event.plain_result("❌ 该群组尚未绑定任何服务器ID。")
            return
        server_id = bindings[group_id]
        del bindings[group_id]
        save_bindings(bindings)
        yield event.plain_result(f"✅ 成功解绑服务器ID：{server_id}")

    @filter.command("查询在线")
    async def query_online(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_query:
            return
        async for result in self._query_online(event):
            yield result

    @filter.command("id查询")
    async def query_by_id(self, event: AstrMessageEvent, keyword: str = "") -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_query:
            return
        async for result in self._query_by_id(event, keyword):
            yield result

    @filter.command("查服")
    async def query_server_cmd(self, event: AstrMessageEvent, keyword: str = "") -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_query:
            return
        async for result in self._query_by_id(event, keyword):
            yield result

    async def _query_online(self, event: AstrMessageEvent) -> None:
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群组未授权，请先激活卡密。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 私聊模式下请使用 /id查询 <服务器ID> 命令。")
            return
        bindings = load_bindings()
        if group_id not in bindings:
            yield event.plain_result("❌ 该群组尚未绑定服务器ID，请先使用 /绑定id <服务器ID> 绑定服务器。")
            return
        server_id = bindings.get(group_id)
        if server_id:
            result = await self.server_query.query_by_id_simple(server_id)
        else:
            result = await self.server_query.get_server_ranking()
        yield event.plain_result(result)

    async def _query_by_id(self, event: AstrMessageEvent, keyword: str = "") -> None:
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群组未授权，请先激活卡密。")
            return
        group_id = str(event.message_obj.group_id or "0")
        if not self._is_in_whitelist(group_id) and not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ ID查询仅在白名单群组可用，或由管理员/群主使用。")
            return
        if not keyword or keyword.strip() == "":
            result = await self.server_query.get_server_ranking()
        else:
            keyword = keyword.strip()
            if keyword.isdigit() and len(keyword) >= 8:
                result = await self.server_query.query_by_id_detailed(keyword)
            else:
                result = await self.server_query.search_servers(keyword)
        yield event.plain_result(result)

    async def _get_scum_news(self, event: AstrMessageEvent, count: int = 5) -> None:
        """获取SCUM最新资讯"""
        yield event.plain_result("📡 正在获取SCUM最新资讯，请稍候...")
        result = await self.server_query.get_news(count)
        yield event.plain_result(result)

    @filter.command("激活卡密")
    async def activate_license(self, event: AstrMessageEvent, key: str = "") -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_auth:
            return
        async for result in self._activate_license(event, key):
            yield result

    @filter.command("生成卡密")
    async def generate_keys(self, event: AstrMessageEvent, days: str = "", count: str = "1") -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_auth:
            return
        # 检查授权系统权限模式（生成卡密属于修改操作）
        if not self._check_auth_permission(event, "modify"):
            yield event.plain_result("❌ 当前授权系统权限模式为只读，不允许生成卡密。")
            return
        async for result in self._generate_keys(event, days, count):
            yield result

    @filter.command("查询卡密")
    async def query_license_keys(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_auth:
            return
        async for result in self._query_license_keys(event):
            yield result

    @filter.command("删除全部卡密")
    async def delete_all_keys(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_auth:
            return
        # 检查授权系统权限模式（删除卡密属于修改操作）
        if not self._check_auth_permission(event, "modify"):
            yield event.plain_result("❌ 当前授权系统权限模式为只读，不允许删除卡密。")
            return
        async for result in self._delete_all_keys(event):
            yield result

    @filter.command("查看授权")
    async def query_auth_status(self, event: AstrMessageEvent) -> None:
        if not await self._is_slash_prefix_allowed_async(event) or not self.enable_auth:
            return
        async for result in self._query_auth_status(event):
            yield result

    @filter.command("查看所有授权")
    async def view_authorizations(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_auth:
            return
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能查看所有授权信息。")
            return
        async for result in self._view_authorizations(event):
            yield result

    @filter.command("群组授权")
    async def authorize_group(self, event: AstrMessageEvent, group_id: str = "", days: str = "30") -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_auth:
            return
        # 检查授权系统权限模式（群组授权属于修改操作）
        if not self._check_auth_permission(event, "modify"):
            yield event.plain_result("❌ 当前授权系统权限模式为只读，不允许进行群组授权。")
            return
        async for result in self._authorize_group(event, group_id, days):
            yield result

    @filter.command("取消授权")
    async def deauthorize_group(self, event: AstrMessageEvent, group_id: str = "") -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_auth:
            return
        # 检查授权系统权限模式（取消授权属于修改操作）
        if not self._check_auth_permission(event, "modify"):
            yield event.plain_result("❌ 当前授权系统权限模式为只读，不允许取消授权。")
            return
        async for result in self._deauthorize_group(event, group_id):
            yield result

    @filter.command("增加时间")
    async def add_auth_time(self, event: AstrMessageEvent, group_id: str = "", days: str = "30") -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_auth:
            return
        # 检查授权系统权限模式（增加时间属于修改操作）
        if not self._check_auth_permission(event, "modify"):
            yield event.plain_result("❌ 当前授权系统权限模式为只读，不允许修改授权时间。")
            return
        async for result in self._add_auth_time(event, group_id, days):
            yield result

    @filter.command("减少时间")
    async def reduce_auth_time(self, event: AstrMessageEvent, group_id: str = "", days: str = "30") -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_auth:
            return
        # 检查授权系统权限模式（减少时间属于修改操作）
        if not self._check_auth_permission(event, "modify"):
            yield event.plain_result("❌ 当前授权系统权限模式为只读，不允许修改授权时间。")
            return
        async for result in self._reduce_auth_time(event, group_id, days):
            yield result

    @filter.command("删除已用卡密")
    async def delete_used_keys(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_auth:
            return
        # 检查授权系统权限模式（删除卡密属于修改操作）
        if not self._check_auth_permission(event, "modify"):
            yield event.plain_result("❌ 当前授权系统权限模式为只读，不允许删除卡密。")
            return
        async for result in self._delete_used_keys(event):
            yield result

    async def _activate_license(self, event: AstrMessageEvent, key: str = "") -> None:
        if not key or not key.strip():
            yield event.plain_result("❌ 请提供要激活的卡密。\n使用方法：/激活卡密 <卡密>")
            return
        key = key.strip().upper()
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        auth_key = self.auth_key
        if not auth_key:
            yield event.plain_result("❌ 系统未配置授权密钥，请联系管理员。")
            return
        
        auth_data = load_auth_data()
        
        if key in auth_data.get("used_keys", {}):
            yield event.plain_result("❌ 该卡密已被使用过。")
            return
        
        result = verify_license_key(key, auth_key, "")
        if not result["valid"]:
            yield event.plain_result(f"❌ {result['error']}")
            return
        
        user_id = str(event.message_obj.sender.user_id) if hasattr(event.message_obj.sender, 'user_id') else ""
        now = int(time.time())
        days = result["days"]

        if group_id in auth_data["groups"]:
            existing_expire = auth_data["groups"][group_id].get("expire", 0)
            if existing_expire > now:
                new_expire = existing_expire + (days * 86400)
                activated_time_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(now))
                expire_time_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(new_expire))
                yield event.plain_result(
                    f"✅ 授权已延长！\n"
                    f"├─ 激活时间: {activated_time_str}\n"
                    f"├─ 授权群组: {group_id}\n"
                    f"├─ 增加天数: {days} 天\n"
                    f"└─ 到期时间: {expire_time_str}\n\n"
                    f"💡 可使用 /查看授权 查看到期时间"
                )
            else:
                new_expire = now + (days * 86400)
                activated_time_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(now))
                expire_time_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(new_expire))
                yield event.plain_result(
                    f"✅ 已重新激活授权！\n"
                    f"├─ 激活时间: {activated_time_str}\n"
                    f"├─ 授权群组: {group_id}\n"
                    f"├─ 授权天数: {days} 天\n"
                    f"└─ 到期时间: {expire_time_str}\n\n"
                    f"💡 可使用 /查看授权 查看到期时间"
                )
        else:
            new_expire = now + (days * 86400)
            activated_time_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(now))
            expire_time_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(new_expire))
            yield event.plain_result(
                f"✅ 激活成功！\n"
                f"├─ 激活时间: {activated_time_str}\n"
                f"├─ 授权群组: {group_id}\n"
                f"├─ 授权天数: {days} 天\n"
                f"└─ 到期时间: {expire_time_str}\n\n"
                f"💡 可使用 /查看授权 查看到期时间"
            )
        
        auth_data["groups"][group_id] = {
            "expire": new_expire,
            "activated_at": now,
            "key": key,
            "days": days,
            "user_id": user_id
        }
        if key in auth_data["unused_keys"]:
            auth_data["unused_keys"].remove(key)
        auth_data["used_keys"][key] = {
            "group_id": group_id,
            "activated_at": now,
            "expire": new_expire,
            "user_id": user_id
        }
        save_auth_data(auth_data)


    async def _generate_keys(self, event: AstrMessageEvent, days: str = "", count: str = "1") -> None:
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能执行此操作。")
            return
        if not days or not days.strip().isdigit():
            yield event.plain_result("❌ 请提供有效的天数。\n使用方法：/生成卡密 <天数> [数量]")
            return
        days_int = int(days.strip())
        try:
            count_int = int(count.strip()) if count and count.strip().isdigit() else 1
        except ValueError:
            count_int = 1
        if count_int < 1:
            count_int = 1
        if count_int > 100:
            yield event.plain_result("❌ 单次最多生成100个卡密。")
            return
        auth_key = self.auth_key
        if not auth_key:
            yield event.plain_result("❌ 系统未配置授权密钥，请联系管理员。")
            return
        group_id = ""
        keys = []
        for i in range(count_int):
            key = generate_license_key(auth_key, days_int, group_id, i)
            keys.append(key)
        auth_data = load_auth_data()
        auth_data["unused_keys"].extend(keys)
        save_auth_data(auth_data)
        keys_str = "\n".join(keys)
        result = f"✅ 成功生成 {count_int} 个 {days_int} 天卡密：\n\n{keys_str}\n\n卡密已保存到未使用列表中。"
        sender_id = str(event.message_obj.sender.user_id) if hasattr(event.message_obj.sender, 'user_id') else ""
        try:
            if hasattr(self.context, 'send_private_message'):
                await self.context.send_private_message(sender_id, result)
            elif hasattr(event, 'reply'):
                await event.reply(result, private=True)
            else:
                yield event.plain_result(result)
                return
            yield event.plain_result("✅ 卡密已私聊发送给您，请查收。")
        except Exception as e:
            logger.error(f"发送私聊失败: {e}")
            yield event.plain_result(result)

    async def _query_license_keys(self, event: AstrMessageEvent) -> None:
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能执行此操作。")
            return
        auth_data = load_auth_data()
        unused_keys = auth_data.get("unused_keys", [])
        used_keys = auth_data.get("used_keys", {})
        result_lines = ["📋 卡密使用情况："]
        if unused_keys:
            result_lines.append(f"\n未使用卡密 ({len(unused_keys)} 个)：")
            for key in unused_keys[:20]:
                result_lines.append(f"• {key}")
            if len(unused_keys) > 20:
                result_lines.append(f"• ... 还有 {len(unused_keys) - 20} 个未显示")
        if used_keys:
            result_lines.append(f"\n已使用卡密 ({len(used_keys)} 个)：")
            for key, info in list(used_keys.items())[:20]:
                activated_at = info.get("activated_at", 0)
                group_id = info.get("group_id", "未知")
                user_id = info.get("user_id", "未知")
                activated_time = time.strftime('%Y-%m-%d %H:%M', time.localtime(activated_at)) if activated_at > 0 else "未知"
                actual_group_info = auth_data.get("groups", {}).get(group_id, {})
                actual_expire = actual_group_info.get("expire", 0) if actual_group_info else 0
                if actual_expire > 0:
                    expire_time = time.strftime('%Y-%m-%d %H:%M', time.localtime(actual_expire))
                    is_expired = actual_expire < int(time.time())
                    expire_status = " (已过期)" if is_expired else ""
                else:
                    expire_time = "未知"
                    expire_status = ""
                result_lines.append(f"• {key}")
                result_lines.append(f"  ├─ 群组ID: {group_id}")
                result_lines.append(f"  ├─ 使用人ID: {user_id}")
                result_lines.append(f"  ├─ 激活时间: {activated_time}")
                result_lines.append(f"  └─ 到期时间: {expire_time}{expire_status}")
            if len(used_keys) > 20:
                result_lines.append(f"• ... 还有 {len(used_keys) - 20} 个未显示")
        if not unused_keys and not used_keys:
            result_lines.append("\n暂无卡密数据。")
        yield event.plain_result("\n".join(result_lines))

    async def _delete_all_keys(self, event: AstrMessageEvent) -> None:
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能执行此操作。")
            return
        unused_count = len(get_unused_keys())
        used_count = len(get_used_keys())
        delete_all_keys()
        yield event.plain_result(f"✅ 已删除所有卡密！\n\n删除未使用卡密: {unused_count} 个\n删除已使用卡密: {used_count} 个")

    async def _delete_used_keys(self, event: AstrMessageEvent) -> None:
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能执行此操作。")
            return
        used_count = len(get_used_keys())
        delete_used_keys()
        yield event.plain_result(f"✅ 已删除所有已使用卡密！\n\n删除数量: {used_count} 个")

    async def _authorize_group(self, event: AstrMessageEvent, group_id: str = "", days: str = "30") -> None:
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能执行此操作。")
            return
        if not group_id or not group_id.strip():
            yield event.plain_result("❌ 请提供群组ID。\n使用方法：/群组授权 <群组ID> [天数]")
            return
        if not days.strip().isdigit():
            days = "30"
        days_int = int(days.strip())
        group_id = group_id.strip()
        operator_id = str(event.message_obj.sender.user_id) if hasattr(event.message_obj.sender, 'user_id') else ""
        if authorize_group(group_id, days_int, operator_id):
            # 授权成功后，自动复制全局配置到分群
            from .qqadmin.group_config import clone_global_config_to_group
            clone_global_config_to_group(group_id, self.config)
            logger.info(f"群组 {group_id} 授权成功，已自动复制全局配置")
            
            now = int(time.time())
            expire = now + (days_int * 86400)
            yield event.plain_result(
                f"✅ 群组授权成功！\n\n"
                f"群组ID: {group_id}\n"
                f"授权天数: {days_int} 天\n"
                f"到期时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire))}\n\n"
                f"全局配置已自动复制到该群组"
            )
        else:
            yield event.plain_result("❌ 群组授权失败。")

    async def _deauthorize_group(self, event: AstrMessageEvent, group_id: str = "") -> None:
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能执行此操作。")
            return
        if not group_id or not group_id.strip():
            yield event.plain_result("❌ 请提供群组ID。\n使用方法：/取消授权 <群组ID>")
            return
        group_id = group_id.strip()
        if deauthorize_group(group_id):
            yield event.plain_result(f"✅ 已取消群组 {group_id} 的授权。")
        else:
            yield event.plain_result("❌ 该群组未授权。")

    async def _add_auth_time(self, event: AstrMessageEvent, group_id: str = "", days: str = "30") -> None:
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能执行此操作。")
            return
        if not group_id or not group_id.strip():
            yield event.plain_result("❌ 请提供群组ID。\n使用方法：/增加时间 <群组ID> [天数]")
            return
        if not days.strip().isdigit():
            days = "30"
        days_int = int(days.strip())
        group_id = group_id.strip()
        if add_auth_time(group_id, days_int):
            info = get_group_auth_info(group_id)
            expire = info.get("expire", 0)
            yield event.plain_result(
                f"✅ 已为群组 {group_id} 增加 {days_int} 天授权！\n"
                f"新到期时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire))}"
            )
        else:
            yield event.plain_result("❌ 该群组未授权。")

    async def _reduce_auth_time(self, event: AstrMessageEvent, group_id: str = "", days: str = "30") -> None:
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能执行此操作。")
            return
        if not group_id or not group_id.strip():
            yield event.plain_result("❌ 请提供群组ID。\n使用方法：/减少时间 <群组ID> [天数]")
            return
        if not days.strip().isdigit():
            days = "30"
        days_int = int(days.strip())
        group_id = group_id.strip()
        if reduce_auth_time(group_id, days_int):
            info = get_group_auth_info(group_id)
            expire = info.get("expire", 0)
            status = "已过期" if expire < time.time() else "正常"
            yield event.plain_result(
                f"✅ 已为群组 {group_id} 减少 {days_int} 天授权！\n"
                f"新到期时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire))}\n"
                f"状态: {status}"
            )
        else:
            yield event.plain_result("❌ 该群组未授权。")

    async def _view_authorizations(self, event: AstrMessageEvent) -> None:
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能执行此操作。")
            return
        groups = list_all_authorizations()
        if not groups:
            yield event.plain_result("❌ 暂无授权群组。")
            return
        now = int(time.time())
        result_lines = ["📊 所有授权群组："]
        for group_id, info in groups.items():
            expire = info.get("expire", 0)
            activated_at = info.get("activated_at", 0)
            key = info.get("key", "未知")
            days = info.get("days", 0)
            user_id = info.get("user_id", "未知")
            is_expired = expire < now
            remaining_seconds = expire - now
            if is_expired:
                expired_days = abs(remaining_seconds) // 86400
                status = f"已过期 {expired_days} 天"
            else:
                remaining_days = remaining_seconds // 86400
                remaining_hours = (remaining_seconds % 86400) // 3600
                status = f"剩余 {remaining_days} 天 {remaining_hours} 小时"
            activated_time = time.strftime('%Y-%m-%d %H:%M', time.localtime(activated_at)) if activated_at > 0 else "未知"
            expire_time = time.strftime('%Y-%m-%d %H:%M', time.localtime(expire)) if expire > 0 else "未知"
            result_lines.append(f"\n群组ID: {group_id}")
            result_lines.append(f"卡密: {key}")
            result_lines.append(f"授权天数: {days} 天")
            result_lines.append(f"首次激活: {activated_time}")
            result_lines.append(f"到期时间: {expire_time}")
            result_lines.append(f"状态: {'⚠️ ' if is_expired else '✅ '}{status}")
            result_lines.append(f"使用人ID: {user_id}")
        yield event.plain_result("\n".join(result_lines))

    @filter.command("帮助", alias={"菜单", "命令"})
    async def show_help(self, event: AstrMessageEvent) -> None:
        logger.info(f"show_help 被调用: {event.message_str}")
        if not self.enable_help:
            logger.info(f"show_help 被拒绝: enable_help={self.enable_help}")
            return
        
        # 检查斜杠前缀权限（管理员和主人不受限制）
        if not self.allow_slash_prefix:
            group_id = event.message_obj.group_id
            if group_id:
                group_setting = get_group_setting(group_id, "allow_slash_prefix", None)
                if group_setting is not None and group_setting is False:
                    # 群设置禁止斜杠前缀，检查是否是管理员/主人
                    if not self._is_owner(event):
                        logger.info(f"show_help 被拒绝: 群设置禁止斜杠前缀且用户非管理员")
                        return
        
        help_text = """📋 Joker命令菜单

基础功能:
  /帮助       - 显示此菜单
  /群管功能   - 查看所有群管系统
  /主人指令   - 主人专属指令

常用群管命令:
  /审核       - 入群审核设置
  /黑名单系统 - 黑名单管理
  /撤回系统   - 撤回管理
  /欢迎系统   - 欢迎语设置
  /禁言系统   - 禁言管理
  /踢出系统   - 踢出管理
  /统计系统   - 邀请排行等
  /文转图     - 将文本转换为图片

SCUM服务器:
  /scum帮助   - 查看SCUM相关命令"""
        logger.info(f"show_help 发送帮助信息")
        yield event.plain_result(help_text)

    @filter.command("scum帮助", alias={"SCUM帮助"})
    async def show_scum_help(self, event: AstrMessageEvent) -> None:
        logger.info(f"show_scum_help 被调用: {event.message_str}, enable_help={self.enable_help}, allow_slash={self.allow_slash_prefix}")
        if not self.enable_help:
            return
        # 检查斜杠前缀权限（管理员和主人不受限制）
        if not self.allow_slash_prefix:
            group_id = event.message_obj.group_id
            if group_id:
                group_setting = get_group_setting(group_id, "allow_slash_prefix", None)
                if group_setting is not None and group_setting is False:
                    # 群设置禁止斜杠前缀，检查是否是主人
                    if not self._is_owner(event):
                        logger.info(f"show_scum_help 被拒绝: 群设置禁止斜杠前缀且非主人")
                        return
        help_text = f"""📖 SCUM服务器帮助

绑定命令:
  /绑定id <服务器ID>   # 绑定服务器
  /解绑               # 解除绑定

查询命令:
  /查询在线           # 查询服务器状态
  /id查询 <关键词>    # 搜索服务器
  /scum更新           # 获取SCUM最新资讯"""
        yield event.plain_result(help_text)

    @filter.command("scum更新", alias={"SCUM更新"})
    async def show_scum_news(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._get_scum_news(event):
            yield result

    @filter.command("签到")
    async def do_signin(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "0"
        user_id = str(event.message_obj.sender.user_id) if hasattr(event.message_obj.sender, 'user_id') else ""
        user_name = event.message_obj.sender.user_name if hasattr(event.message_obj.sender, 'user_name') else ""
        
        result = signin(group_id, user_id, user_name)
        yield event.plain_result(result["message"])

    @filter.command("排行榜", alias={"积分排行"})
    async def show_ranking(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "0"
        ranking = get_ranking(group_id, 10)
        
        if not ranking:
            yield event.plain_result("暂无积分排行数据")
            return
        
        lines = ["🏆 积分排行榜"]
        for i, item in enumerate(ranking, start=1):
            name = item.get("user_name", item["user_id"])
            lines.append(f"{i}. {name} - {item['points']} 积分")
        
        yield event.plain_result("\n".join(lines))

    @filter.command("我的信息", alias={"个人信息"})
    async def show_user_info(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "0"
        user_id = str(event.message_obj.sender.user_id) if hasattr(event.message_obj.sender, 'user_id') else ""
        
        info = get_user_info(group_id, user_id)
        if not info.get("user_name"):
            yield event.plain_result("暂无个人信息，请先签到")
            return
        
        joined_at = time.strftime("%Y-%m-%d", time.localtime(info["joined_at"])) if info["joined_at"] else "未知"
        
        text = f"""👤 个人信息
用户名: {info['user_name']}
积分: {info['points']}
签到次数: {info['total_signins']}
连续签到: {info['consecutive_days']} 天
累计签到: {info['total_signin_days']} 天
入群时间: {joined_at}"""
        
        yield event.plain_result(text)

    @filter.command("群统计", alias={"群数据"})
    async def show_group_stats(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "0"
        stats = get_group_stats(group_id)
        
        text = f"""📊 群统计
总用户数: {stats['total_users']}
今日签到: {stats['today_signins']}
总积分: {stats['total_points']}
平均积分: {stats['avg_points']}"""
        
        yield event.plain_result(text)

    @filter.command("添加定时消息")
    async def add_timed_msg(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 仅管理员/群主可添加定时消息")
            return
        
        group_id = str(event.message_obj.group_id)
        msg_str = event.message_str
        
        parts = msg_str.split(maxsplit=3)
        if len(parts) < 4:
            yield event.plain_result("❌ 格式错误: /添加定时消息 <名称> <cron表达式> <消息内容>")
            return
        
        name = parts[1]
        cron_expr = parts[2]
        message = parts[3]
        
        result = add_timed_message(group_id, message, cron_expr, at_all=False, enabled=True,
                                  operator_id=str(event.message_obj.sender.user_id) if hasattr(event.message_obj.sender, 'user_id') else "", name=name)
        
        if result["success"]:
            yield event.plain_result(f"✅ 定时消息添加成功，ID: {result['id']}")
        else:
            yield event.plain_result(f"❌ 添加失败: {result.get('message', '未知错误')}")

    @filter.command("查看定时消息")
    async def list_timed_msgs(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id)
        messages = get_timed_messages(group_id)
        
        if not messages:
            yield event.plain_result("暂无定时消息")
            return
        
        lines = ["⏰ 定时消息列表"]
        for msg in messages:
            status = "✅" if msg["enabled"] else "❌"
            lines.append(f"{status} {msg['name']} - {msg['cron_expr']}")
        
        yield event.plain_result("\n".join(lines))

    @filter.command("删除定时消息")
    async def del_timed_msg(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 仅管理员/群主可删除定时消息")
            return
        
        group_id = str(event.message_obj.group_id)
        msg_str = event.message_str
        
        parts = msg_str.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 格式错误: /删除定时消息 <ID>")
            return
        
        msg_id = parts[1]
        
        if delete_timed_message(group_id, msg_id):
            yield event.plain_result("✅ 定时消息删除成功")
        else:
            yield event.plain_result("❌ 删除失败，未找到该消息")

    @filter.command("添加分群管理员")
    async def add_sub_admin_cmd(self, event: AstrMessageEvent) -> None:
        if not self._is_owner(event):
            group_id = str(event.message_obj.group_id)
            sender_id = str(event.message_obj.sender.user_id) if hasattr(event.message_obj.sender, 'user_id') else ""
            
            is_group_owner = False
            if group_id and group_id != "None":
                try:
                    platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                    if platform:
                        member_info = await platform.get_client().call_action(
                            action="get_group_member_info",
                            group_id=int(group_id),
                            user_id=int(sender_id)
                        )
                        role = member_info.get("role", "")
                        if role == "owner":
                            is_group_owner = True
                except Exception:
                    pass
            
            if not is_group_owner:
                yield event.plain_result("❌ 仅群主可添加分群管理员")
                return
        
        group_id = str(event.message_obj.group_id)
        msg_str = event.message_str
        
        parts = msg_str.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 格式错误: /添加分群管理员 <QQ号>")
            return
        
        user_id = parts[1]
        
        if add_sub_admin(group_id, user_id, operator_id=str(event.message_obj.sender.user_id)):
            yield event.plain_result(f"✅ 已添加分群管理员: {user_id}")
        else:
            yield event.plain_result("❌ 添加失败，该用户已是分群管理员")

    @filter.command("删除分群管理员")
    async def remove_sub_admin_cmd(self, event: AstrMessageEvent) -> None:
        if not self._is_owner(event):
            group_id = str(event.message_obj.group_id)
            sender_id = str(event.message_obj.sender.user_id) if hasattr(event.message_obj.sender, 'user_id') else ""
            
            is_group_owner = False
            if group_id and group_id != "None":
                try:
                    platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                    if platform:
                        member_info = await platform.get_client().call_action(
                            action="get_group_member_info",
                            group_id=int(group_id),
                            user_id=int(sender_id)
                        )
                        role = member_info.get("role", "")
                        if role == "owner":
                            is_group_owner = True
                except Exception:
                    pass
            
            if not is_group_owner:
                yield event.plain_result("❌ 仅群主可删除分群管理员")
                return
        
        group_id = str(event.message_obj.group_id)
        msg_str = event.message_str
        
        parts = msg_str.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 格式错误: /删除分群管理员 <QQ号>")
            return
        
        user_id = parts[1]
        
        if remove_sub_admin(group_id, user_id):
            yield event.plain_result(f"✅ 已移除分群管理员: {user_id}")
        else:
            yield event.plain_result("❌ 删除失败，该用户不是分群管理员")

    @filter.command("查看分群管理员")
    async def list_sub_admins(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id)
        admins = get_sub_admins(group_id)
        
        if not admins:
            yield event.plain_result("暂无分群管理员")
            return
        
        lines = ["👥 分群管理员列表"]
        for admin in admins:
            lines.append(f"QQ: {admin['user_id']}")
        
        yield event.plain_result("\n".join(lines))

    @filter.command("群管功能", alias={"管理功能", "群管理"})
    async def show_qqadmin_help(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_help:
            return
        
        # 主人不受关机状态限制
        if not self._is_owner(event):
            # 检查分群开机状态
            group_id = str(event.message_obj.group_id) if event.message_obj.group_id else ""
            if group_id and group_id != "None":
                from .qqadmin.group_config import get_group_data
                features = get_group_data(group_id, "features", {})
                power_status = features.get("power_status", "")
                if power_status == "off":
                    return
        
        help_text = """群管功能系统

入群审核系统
黑名单系统
禁言系统
踢出系统
撤回系统
欢迎系统
自动回复系统
用户专属回复系统
统计系统
定时消息系统
分群管理员系统
群设置系统
文转图系统

发送对应系统名称查看详细指令
例如: /黑名单系统"""
        yield event.plain_result(help_text)

    @filter.command("入群审核系统", alias={"审核系统"})
    async def show_audit_help(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        help_text = """入群审核

/审核 - 查看审核设置
/审核 <模式> - 设置审核模式 (direct/code/math/id)
/开启审核 - 开启审核
/关闭审核 - 关闭审核
/审批 <QQ> - 同意入群
/拒绝 <QQ> [理由] - 拒绝入群
/查看待审核 - 查看待审核用户（可看到审批码）
/审核超时 <秒> - 设置验证超时时间
/审核次数 <次数> - 设置最大尝试次数
/验证提示 - 重新获取验证提示（用户用）"""
        yield event.plain_result(help_text)

    @filter.command("欢迎系统")
    async def show_welcome_help(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        help_text = """欢迎系统

/设置欢迎语 <内容>
/欢迎开关
/查看欢迎语"""
        yield event.plain_result(help_text)

    @filter.command("自动回复系统", alias={"回复系统"})
    async def show_reply_help(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        help_text = """自动回复系统

/添加回复 <关键词> <回复>
/删除回复 <关键词>
/回复列表
/回复设置"""
        yield event.plain_result(help_text)

    @filter.command("用户专属回复系统", alias={"专属回复系统"})
    async def show_player_reply_help(self, event: AstrMessageEvent) -> None:
        logger.info(f"show_player_reply_help 被调用: {event.message_str}")
        if not self.enable_help:
            return

        if not self.qqadmin_enable_player_reply:
            yield event.plain_result("❌ 用户专属回复功能已关闭")
            return

        group_id = event.message_obj.group_id
        if group_id:
            group_setting = get_group_setting(group_id, "allow_slash_prefix", None)
            if group_setting is not None and group_setting is False:
                if not self._is_owner(event):
                    return

        help_text = """用户专属回复系统

/添加专属回复 <QQ> <消息> - 添加一条回复(可多次添加)
/设置专属回复 <QQ> <消息> - 覆盖设置(清空原有)
/删除专属回复 <QQ> - 删除用户所有回复
/删除专属回复 <QQ> <序号> - 删除指定序号的回复
/专属回复@ <QQ> <开/关>
/查看专属回复列表

💡 支持多条消息，每次触发随机发送一条"""
        yield event.plain_result(help_text)

    @filter.command("定时消息系统")
    async def show_timed_msg_help(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        help_text = """定时消息系统

/添加定时消息 <名称> <cron> <内容>
/查看定时消息
/删除定时消息 <ID>

Cron格式: 分 时 日 月 周"""
        yield event.plain_result(help_text)

    @filter.command("分群管理员系统")
    async def show_sub_admin_help(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        help_text = """分群管理员系统

/添加分群管理员 <QQ>
/删除分群管理员 <QQ>
/查看分群管理员"""
        yield event.plain_result(help_text)

    @filter.command("文转图系统")
    async def show_text_to_image_help(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        help_text = """文转图系统

/文转图   - 开启文转图（机器人回复转为图片）
/图转文   - 关闭文转图（机器人回复转为文字）"""
        yield event.plain_result(help_text)

    @filter.command("群设置系统", alias={"设置系统"})
    async def show_settings_help(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        help_text = """群设置系统

/查看系统设置
/群设置
/功能开关
/添加白名单群 <群号>
/删除白名单群 <群号>
/添加过滤群 <群号>
/删除过滤群 <群号>"""
        yield event.plain_result(help_text)
    
    @filter.command("群设置")
    async def show_group_settings(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        if not (self._is_owner(event) or self._is_slash_prefix_allowed(event)):
            return
        if not self.enable_settings:
            return
        async for result in self._show_settings(event):
            yield result
    
    @filter.command("添加白名单群")
    async def add_whitelist_group(self, event: AstrMessageEvent) -> None:
        if not self._is_owner(event):
            return
        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /添加白名单群 <群号>")
            return
        group_id = parts[1].strip()
        if "basic" not in self.config:
            self.config["basic"] = {}
        if "whitelist_groups" not in self.config["basic"]:
            self.config["basic"]["whitelist_groups"] = []
        if group_id not in self.config["basic"]["whitelist_groups"]:
            self.config["basic"]["whitelist_groups"].append(group_id)
            self.whitelist_groups = self.config["basic"]["whitelist_groups"]
            yield event.plain_result(f"✅ 已添加群 {group_id} 到白名单")
        else:
            yield event.plain_result(f"⚠️ 群 {group_id} 已在白名单中")
    
    @filter.command("删除白名单群")
    async def remove_whitelist_group(self, event: AstrMessageEvent) -> None:
        if not self._is_owner(event):
            return
        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /删除白名单群 <群号>")
            return
        group_id = parts[1].strip()
        if "basic" in self.config and "whitelist_groups" in self.config["basic"]:
            if group_id in self.config["basic"]["whitelist_groups"]:
                self.config["basic"]["whitelist_groups"].remove(group_id)
                self.whitelist_groups = self.config["basic"]["whitelist_groups"]
                yield event.plain_result(f"✅ 已从白名单移除群 {group_id}")
            else:
                yield event.plain_result(f"⚠️ 群 {group_id} 不在白名单中")
        else:
            yield event.plain_result(f"⚠️ 白名单为空")
    
    @filter.command("添加过滤群")
    async def add_filter_group(self, event: AstrMessageEvent) -> None:
        if not self._is_owner(event):
            return
        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /添加过滤群 <群号>")
            return
        group_id = parts[1].strip()
        if "basic" not in self.config:
            self.config["basic"] = {}
        if "filter_groups" not in self.config["basic"]:
            self.config["basic"]["filter_groups"] = []
        if group_id not in self.config["basic"]["filter_groups"]:
            self.config["basic"]["filter_groups"].append(group_id)
            self.filter_groups = self.config["basic"]["filter_groups"]
            yield event.plain_result(f"✅ 已添加群 {group_id} 到过滤群")
        else:
            yield event.plain_result(f"⚠️ 群 {group_id} 已在过滤群中")
    
    @filter.command("删除过滤群")
    async def remove_filter_group(self, event: AstrMessageEvent) -> None:
        if not self._is_owner(event):
            return
        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /删除过滤群 <群号>")
            return
        group_id = parts[1].strip()
        if "basic" in self.config and "filter_groups" in self.config["basic"]:
            if group_id in self.config["basic"]["filter_groups"]:
                self.config["basic"]["filter_groups"].remove(group_id)
                self.filter_groups = self.config["basic"]["filter_groups"]
                yield event.plain_result(f"✅ 已从过滤群移除群 {group_id}")
            else:
                yield event.plain_result(f"⚠️ 群 {group_id} 不在过滤群中")
        else:
            yield event.plain_result(f"⚠️ 过滤群为空")
    
    @filter.command("SOLO", alias={"solo"})
    async def show_solo_info(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        yield event.plain_result("当前订阅版本暂不支持使用SOLO，请联系企业管理员升级。")

    @filter.command("主人指令", alias={"管理指令"})
    async def show_owner_help(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self.enable_help:
            return
        help_text = """👑 主人指令

配置:
  /配置全局

卡密:
  /生成卡密 <天数> [数量]
  /查询卡密 · /删除已用卡密

授权:
  /群组授权 <群ID> [天数]
  /取消授权 <群ID>
  /增加时间 · /减少时间
  /查看所有授权"""
        yield event.plain_result(help_text)

    @filter.command("禁言系统")
    async def show_mute_help(self, event: AstrMessageEvent) -> None:
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能使用此功能。")
            return
        if not self._is_owner(event) and not self._check_qqadmin_feature(event, self.qqadmin_enable_mute, "mute_enabled"):
            return
        async for result in self._show_mute_help(event):
            yield result

    @filter.command("踢出系统")
    async def show_kick_help(self, event: AstrMessageEvent) -> None:
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能使用此功能。")
            return
        if not self._is_owner(event) and not self._check_qqadmin_feature(event, self.qqadmin_enable_kick, "kick_enabled"):
            return
        async for result in self._show_kick_help(event):
            yield result

    @filter.command("黑名单系统")
    async def show_blacklist_help(self, event: AstrMessageEvent) -> None:
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能使用此功能。")
            return
        if not self._is_owner(event) and not self._check_qqadmin_feature(event, self.qqadmin_enable_blacklist, "blacklist_enabled"):
            return
        async for result in self._show_blacklist_help(event):
            yield result

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_module_help_commands(self, event: AstrMessageEvent) -> None:
        # 先检查是否是请求事件或通知事件
        raw_event = getattr(event.message_obj, "raw_message", None)
        
        if isinstance(raw_event, dict):
            # 处理请求事件（只有 GROUP_REQUEST 事件类型才会真正包含请求数据）
            # 检查是否是真正的请求事件，而不是消息事件中附带的数据
            if raw_event.get("post_type") == "request":
                # 进一步检查是否包含必要的请求字段
                if raw_event.get("request_type") == "group" and raw_event.get("flag"):
                    await self._process_group_request(event, raw_event)
                return
            
            # 处理通知事件（入群邀请）
            if raw_event.get("post_type") == "notice" and \
               raw_event.get("notice_type") == "group_increase" and \
               raw_event.get("sub_type") == "invite":
                async for result in self._process_group_invite_notice(event, raw_event):
                    yield result
                return
        
        message_str = event.message_str or ""
        cmd = message_str.lstrip('/').strip()

        # 检查是否是模块帮助命令
        module_help_commands = ["入群审核", "撤回系统", "统计系统", "入群欢迎"]
        
        # 主人不受关机状态限制
        if not self._is_owner(event):
            # 检查分群开机状态
            group_id = str(event.message_obj.group_id) if event.message_obj.group_id else ""
            if group_id and group_id != "None":
                from .qqadmin.group_config import get_group_data
                features = get_group_data(group_id, "features", {})
                power_status = features.get("power_status", "")
                if power_status == "off":
                    return
        if cmd not in module_help_commands:
            return

        if cmd == "入群审核":
            async for result in self._show_audit_help(event):
                yield result
            event.stop_event()
        elif cmd == "撤回系统":
            async for result in self._show_recall_help(event):
                yield result
            event.stop_event()
        elif cmd == "统计系统":
            async for result in self._show_stats_help(event):
                yield result
            event.stop_event()
        elif cmd == "入群欢迎":
            async for result in self._show_welcome_help(event):
                yield result
            event.stop_event()

    @filter.command("分群设置")
    async def show_group_config_help(self, event: AstrMessageEvent) -> None:
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能使用此功能。")
            return
        if not self._is_owner(event) and not self._check_qqadmin_feature(event, self.qqadmin_enable_group_config, "group_config_enabled"):
            return
        async for result in self._show_group_config_help(event):
            yield result

    @filter.command("设置")
    async def show_settings(self, event: AstrMessageEvent) -> None:
        if not (self._is_owner(event) or self._is_slash_prefix_allowed(event)):
            return
        if not self.enable_settings:
            return
        async for result in self._show_settings(event):
            yield result

    @filter.command("设置 开启斜杠")
    async def set_slash_on(self, event: AstrMessageEvent) -> None:
        if not (self._is_owner(event) or self._is_slash_prefix_allowed(event)):
            return
        if not self.enable_settings:
            return
        async for result in self._set_slash_setting(event, True):
            yield result

    @filter.command("设置 关闭斜杠")
    async def set_slash_off(self, event: AstrMessageEvent) -> None:
        if not (self._is_owner(event) or self._is_slash_prefix_allowed(event)):
            return
        if not self.enable_settings:
            return
        async for result in self._set_slash_setting(event, False):
            yield result

    # ==================== 群管总开关命令 ====================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_group_manage_commands(self, event: AstrMessageEvent) -> None:
        message_str = event.message_str or ""
        cmd = message_str.lstrip('/').strip()

        # 检查是否是群管总开关命令
        group_manage_commands = ["开启群管", "关闭群管", "开启黑名单系统", "关闭黑名单系统"]
        if cmd not in group_manage_commands:
            return

        if cmd == "开启群管":
            if not await self._is_admin_or_owner_async(event):
                yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
                return
            group_id = str(event.message_obj.group_id)
            if not group_id or group_id == "None":
                yield event.plain_result("❌ 此命令只能在群组中使用。")
                return

            set_feature_enabled(group_id, "mute_enabled", True, self.config)
            set_feature_enabled(group_id, "kick_enabled", True, self.config)
            set_feature_enabled(group_id, "blacklist_enabled", True, self.config)
            set_feature_enabled(group_id, "audit_enabled", True, self.config)
            set_feature_enabled(group_id, "recall_enabled", True, self.config)
            set_feature_enabled(group_id, "welcome_enabled", True, self.config)
            yield event.plain_result("✅ 已开启群管系统所有功能")
            event.stop_event()

        elif cmd == "关闭群管":
            if not await self._is_admin_or_owner_async(event):
                yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
                return
            group_id = str(event.message_obj.group_id)
            if not group_id or group_id == "None":
                yield event.plain_result("❌ 此命令只能在群组中使用。")
                return

            set_feature_enabled(group_id, "mute_enabled", False, self.config)
            set_feature_enabled(group_id, "kick_enabled", False, self.config)
            set_feature_enabled(group_id, "blacklist_enabled", False, self.config)
            set_feature_enabled(group_id, "audit_enabled", False, self.config)
            set_feature_enabled(group_id, "recall_enabled", False, self.config)
            set_feature_enabled(group_id, "welcome_enabled", False, self.config)
            yield event.plain_result("✅ 已关闭群管系统所有功能")
            event.stop_event()

        elif cmd == "开启黑名单系统":
            if not await self._is_admin_or_owner_async(event):
                yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
                return
            group_id = str(event.message_obj.group_id)
            if not group_id or group_id == "None":
                yield event.plain_result("❌ 此命令只能在群组中使用。")
                return

            set_feature_enabled(group_id, "blacklist_enabled", True, self.config)
            yield event.plain_result("✅ 已开启黑名单系统")
            event.stop_event()

        elif cmd == "关闭黑名单系统":
            if not await self._is_admin_or_owner_async(event):
                yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
                return
            group_id = str(event.message_obj.group_id)
            if not group_id or group_id == "None":
                yield event.plain_result("❌ 此命令只能在群组中使用。")
                return

            set_feature_enabled(group_id, "blacklist_enabled", False, self.config)
            yield event.plain_result("✅ 已关闭黑名单系统")
            event.stop_event()

    # ==================== 禁言系统命令 ====================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_mute_commands(self, event: AstrMessageEvent) -> None:
        message_str = event.message_str or ""
        cmd = message_str.lstrip('/').strip()

        # 检查是否是禁言相关命令
        mute_commands = ["禁言", "解除禁言", "查看禁言列表", "设置解禁提示", "查看解禁提示", "清除解禁提示"]
        is_mute_command = cmd in mute_commands or \
                         cmd.startswith("禁言 ") or \
                         cmd.startswith("解除禁言 ") or \
                         cmd.startswith("设置解禁提示 ")
        
        if not is_mute_command:
            return
        
        if not self._check_qqadmin_feature(event, self.qqadmin_enable_mute, "mute_enabled"):
            return
        
        if cmd == "禁言" or cmd.startswith("禁言 "):
            async for result in self._handle_mute(event):
                yield result
            event.stop_event()
        
        elif cmd in ["解除禁言", "解禁"] or cmd.startswith("解除禁言 ") or cmd.startswith("解禁 "):
            async for result in self._handle_unmute(event):
                yield result
            event.stop_event()
        
        elif cmd == "查看禁言列表":
            async for result in self._handle_list_muted(event):
                yield result
            event.stop_event()

        elif cmd.startswith("设置解禁提示 "):
            async for result in self._handle_set_unmute_notification(event):
                yield result
            event.stop_event()

        elif cmd == "查看解禁提示":
            async for result in self._handle_view_unmute_notification(event):
                yield result
            event.stop_event()

        elif cmd == "清除解禁提示":
            async for result in self._handle_clear_unmute_notification(event):
                yield result
            event.stop_event()


    # ==================== 管理员审批命令 ====================
    @filter.command("同意")
    async def approve_by_code(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_audit, "audit_enabled"):
            return
        async for result in self._handle_approve_by_code(event):
            yield result
    
    @filter.command("拒绝")
    async def reject_by_code(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_audit, "audit_enabled"):
            return
        async for result in self._handle_reject_by_code(event):
            yield result


    # ==================== 踢出系统命令 ====================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_kick_commands(self, event: AstrMessageEvent) -> None:
        message_str = event.message_str or ""
        cmd = message_str.lstrip('/').strip()

        # 检查是否是踢出相关命令
        kick_commands = ["踢出", "查看踢出记录", "清除踢出记录", "连带踢出"]
        is_kick_command = cmd in kick_commands or cmd.startswith("踢出 ") or cmd.startswith("连带踢出")
        
        if not is_kick_command:
            return
        
        if not self._check_qqadmin_feature(event, self.qqadmin_enable_kick, "kick_enabled"):
            return
        
        if cmd == "踢出" or cmd.startswith("踢出 ") or cmd.startswith("踢出@"):
            async for result in self._handle_kick(event):
                yield result
            event.stop_event()
        
        elif cmd == "连带踢出" or cmd.startswith("连带踢出 "):
            async for result in self._handle_kick_with_inviter(event):
                yield result
            event.stop_event()
        
        elif cmd == "查看踢出记录" or cmd.startswith("查看踢出记录 "):
            async for result in self._handle_list_kick_records(event):
                yield result
            event.stop_event()
        
        elif cmd == "清除踢出记录" or cmd.startswith("清除踢出记录 "):
            async for result in self._handle_clear_kick_records(event):
                yield result
            event.stop_event()

    # ==================== 黑名单系统命令 ====================
    @filter.command("拉黑")
    async def blacklist_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_blacklist, "blacklist_enabled"):
            return
        async for result in self._handle_blacklist(event):
            yield result
    
    @filter.command("拉黑@")
    async def blacklist_at_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_blacklist, "blacklist_enabled"):
            return
        async for result in self._handle_blacklist(event):
            yield result

    @filter.command("删黑")
    async def unblacklist_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_blacklist, "blacklist_enabled"):
            return
        async for result in self._handle_unblacklist(event):
            yield result

    @filter.command("查看黑名单")
    async def list_blacklist_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_blacklist, "blacklist_enabled"):
            return
        async for result in self._handle_list_blacklist(event):
            yield result

    @filter.command("清空黑名单")
    async def clear_blacklist_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_blacklist, "blacklist_enabled"):
            return
        async for result in self._handle_clear_blacklist(event):
            yield result

    @filter.command("设置退群入黑")
    async def set_blacklist_on_leave_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /设置退群入黑 <开/关>")
            return
        value = parts[1]
        if value not in ["开", "关", "0", "1"]:
            yield event.plain_result("❌ 请输入: /设置退群入黑 <开/关>")
            return
        enabled = value in ["开", "1"]
        group_id = str(event.message_obj.group_id)
        set_group_setting(group_id, "qqadmin.blacklist.auto_blacklist_on_leave", enabled)
        yield event.plain_result(f"✅ 退群入黑已{'开启' if enabled else '关闭'}")

    @filter.command("设置自动踢出")
    async def set_auto_kick_blacklisted_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /设置自动踢出 <开/关>")
            return
        value = parts[1]
        if value not in ["开", "关", "0", "1"]:
            yield event.plain_result("❌ 请输入: /设置自动踢出 <开/关>")
            return
        enabled = value in ["开", "1"]
        group_id = str(event.message_obj.group_id)
        set_group_setting(group_id, "qqadmin.blacklist.auto_kick_blacklisted", enabled)
        yield event.plain_result(f"✅ 自动踢出黑名单用户已{'开启' if enabled else '关闭'}")

    @filter.command("延长拉黑时间")
    async def extend_blacklist_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_blacklist, "blacklist_enabled"):
            return
        async for result in self._handle_extend_blacklist(event):
            yield result

    # ==================== 入群审核命令 ====================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_audit_commands(self, event: AstrMessageEvent) -> None:
        message_str = event.message_str or ""
        cmd = message_str.lstrip('/').strip()

        # 检查是否是入群审核相关命令
        audit_commands = ["开启审核", "关闭审核", "审核", "审批", "拒绝",
                         "查看待审核", "开启验证", "关闭验证", "验证提示"]
        is_audit_command = cmd in audit_commands or \
                          cmd.startswith("审核 ") or \
                          cmd.startswith("审批 ") or \
                          cmd.startswith("拒绝 ") or \
                          cmd.startswith("添加白名单") or \
                          cmd.startswith("移除白名单")
        
        if not is_audit_command:
            return

        if cmd == "开启审核":
            async for result in self._handle_enable_audit(event):
                yield result
            event.stop_event()
            return
        elif cmd == "关闭审核":
            async for result in self._handle_disable_audit(event):
                yield result
            event.stop_event()
            return
        
        # 设置命令（审核模式等）即使审核功能没开启也能执行
        if cmd == "审核" or cmd.startswith("审核 "):
            async for result in self._handle_set_approval_mode(event):
                yield result
            event.stop_event()
            return
        
        # 设置命令（审核超时、审核次数）即使审核功能没开启也能执行
        if cmd.startswith("审核超时 "):
            async for result in self._handle_set_verification_timeout(event):
                yield result
            event.stop_event()
            return
        elif cmd.startswith("审核次数 "):
            async for result in self._handle_set_max_verify_attempts(event):
                yield result
            event.stop_event()
            return
        
        # 其他命令需要检查审核功能是否开启
        if not self._check_qqadmin_feature(event, self.qqadmin_enable_audit, "audit_enabled"):
            return
        
        if cmd == "审批" or cmd.startswith("审批 "):
            async for result in self._handle_approve_audit_direct(event):
                yield result
            event.stop_event()
        elif cmd == "拒绝" or cmd.startswith("拒绝 "):
            async for result in self._handle_reject_by_code(event):
                yield result
            event.stop_event()
        elif cmd == "查看待审核":
            async for result in self._handle_list_pending_audit(event):
                yield result
            event.stop_event()
        elif cmd.startswith("添加白名单"):
            async for result in self._handle_add_whitelist(event):
                yield result
            event.stop_event()
        elif cmd.startswith("移除白名单"):
            async for result in self._handle_remove_whitelist(event):
                yield result
            event.stop_event()
        elif cmd == "开启验证":
            async for result in self._handle_enable_verification(event):
                yield result
            event.stop_event()
        elif cmd == "关闭验证":
            async for result in self._handle_disable_verification(event):
                yield result
            event.stop_event()
        elif cmd.startswith("设置验证类型"):
            async for result in self._handle_set_verification_type(event):
                yield result
            event.stop_event()
        elif cmd == "验证提示":
            async for result in self._handle_show_verification_hint(event):
                yield result
            event.stop_event()

    @filter.command("查看验证设置")
    async def view_verification_settings_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_audit, "audit_enabled"):
            return
        async for result in self._handle_view_verification_settings(event):
            yield result

    @filter.command("查看系统设置")
    async def view_all_settings_command(self, event: AstrMessageEvent) -> None:
        """查看所有系统设置"""
        if not self._is_slash_prefix_allowed(event):
            return
        
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能使用此功能。")
            return
        
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        result_parts = []
        result_parts.append("📋 系统设置概览")
        result_parts.append("━━━━━━━━━━━━━━")
        
        # ========== 入群审核设置 ==========
        from .qqadmin.audit import (
            get_max_verify_attempts, get_verification_timeout, get_audit_approval_mode,
            get_pending_verification, get_whitelist_users
        )
        max_attempts = get_max_verify_attempts(group_id)
        timeout = get_verification_timeout(group_id)
        approval_mode = get_audit_approval_mode(group_id)
        pending_list = get_pending_verification(group_id, user_id) if user_id else []
        whitelist = get_whitelist_users(group_id)
        
        result_parts.append("【入群审核】")
        result_parts.append(f"  状态: {'✅ 开启' if self._check_qqadmin_feature(event, self.qqadmin_enable_audit, 'audit_enabled') else '❌ 关闭'}")
        result_parts.append(f"  审批模式: {approval_mode} (direct/code/math/id)")
        result_parts.append(f"  最大尝试次数: {max_attempts}次")
        result_parts.append(f"  验证超时时间: {timeout}秒")
        result_parts.append(f"  待验证用户: {len(pending_list)}人")
        result_parts.append(f"  白名单用户: {len(whitelist)}人")
        result_parts.append("")
        
        # ========== 自定义回复设置 ==========
        from .qqadmin import list_replies, get_reply_cooldown
        replies = list_replies(group_id)
        exact_count = len(replies.get("exact", {}))
        fuzzy_count = len(replies.get("fuzzy", []))
        regex_count = len(replies.get("regex", []))
        cooldown = get_reply_cooldown(group_id)
        
        result_parts.append("【自定义回复】")
        result_parts.append(f"  状态: {'✅ 开启' if self._check_qqadmin_feature(event, self.qqadmin_enable_reply, 'reply_enabled') else '❌ 关闭'}")
        result_parts.append(f"  冷却时间: {cooldown}秒")
        result_parts.append(f"  精确回复: {exact_count}条")
        result_parts.append(f"  模糊回复: {fuzzy_count}条")
        result_parts.append(f"  正则回复: {regex_count}条")
        result_parts.append("")
        
        # ========== 撤回系统设置 ==========
        from .qqadmin.recall import (
            is_recall_enabled, get_recall_time, get_recall_mode,
            get_recall_keywords, get_recall_regex_list
        )
        recall_enabled = is_recall_enabled(group_id)
        recall_time = get_recall_time(group_id)
        recall_mode = get_recall_mode(group_id)
        keywords = get_recall_keywords(group_id)
        patterns = get_recall_regex_list(group_id)
        
        result_parts.append("【撤回系统】")
        result_parts.append(f"  状态: {'✅ 开启' if recall_enabled else '❌ 关闭'}")
        result_parts.append(f"  撤回模式: {recall_mode} (keyword/regex/all)")
        result_parts.append(f"  撤回时间: {recall_time}秒")
        result_parts.append(f"  关键词数量: {len(keywords)}个")
        result_parts.append(f"  正则数量: {len(patterns)}个")
        if keywords:
            result_parts.append(f"  关键词: {', '.join(keywords[:5])}")
            if len(keywords) > 5:
                result_parts.append(f"    ... 还有 {len(keywords) - 5} 个")
        result_parts.append("")
        
        # ========== 欢迎系统设置 ==========
        from .qqadmin.welcome import (
            is_welcome_enabled, get_welcome_message, get_farewell_message,
            is_farewell_enabled, get_welcome_delay, get_welcome_type
        )
        welcome_enabled = is_welcome_enabled(group_id)
        farewell_enabled = is_farewell_enabled(group_id)
        welcome_msg = get_welcome_message(group_id)
        farewell_msg = get_farewell_message(group_id)
        welcome_delay = get_welcome_delay(group_id)
        welcome_type = get_welcome_type(group_id)
        
        result_parts.append("【欢迎系统】")
        result_parts.append(f"  入群欢迎: {'✅ 开启' if welcome_enabled else '❌ 关闭'}")
        result_parts.append(f"  退群通知: {'✅ 开启' if farewell_enabled else '❌ 关闭'}")
        result_parts.append(f"  欢迎类型: {'自动' if welcome_type == 'auto' else '关键词'}")
        result_parts.append(f"  延迟发送: {welcome_delay}秒")
        result_parts.append(f"  欢迎语: {welcome_msg[:30] + '...' if welcome_msg and len(welcome_msg) > 30 else welcome_msg or '未设置'}")
        result_parts.append("")
        
        # ========== 禁言系统设置 ==========
        from .qqadmin.mute import get_mute_settings, list_muted_users
        mute_settings = get_mute_settings(group_id)
        muted_users = list_muted_users(group_id)
        
        result_parts.append("【禁言系统】")
        result_parts.append(f"  状态: {'✅ 开启' if self._check_qqadmin_feature(event, self.qqadmin_enable_mute, 'mute_enabled') else '❌ 关闭'}")
        result_parts.append(f"  默认级别: {mute_settings.get('level', 5)}级")
        result_parts.append(f"  默认时长: {mute_settings.get('duration', 300)}秒")
        result_parts.append(f"  当前禁言用户: {len(muted_users)}人")
        result_parts.append("")
        
        # ========== 踢出系统设置 ==========
        from .qqadmin.kick import get_kick_settings
        kick_settings = get_kick_settings(group_id)
        
        result_parts.append("【踢出系统】")
        result_parts.append(f"  状态: {'✅ 开启' if self._check_qqadmin_feature(event, self.qqadmin_enable_kick, 'kick_enabled') else '❌ 关闭'}")
        result_parts.append(f"  自动拉黑: {'✅ 是' if kick_settings.get('auto_blacklist') else '❌ 否'}")
        result_parts.append(f"  踢出上限: {kick_settings.get('limit', 3)}次")
        result_parts.append("")
        
        # ========== 关键词过滤设置 ==========
        from .qqadmin.group_config import get_group_config
        group_cfg = get_group_config(group_id)
        filter_cfg = group_cfg.get("features", {}).get("keyword_filter", {})
        
        result_parts.append("【关键词过滤】")
        result_parts.append(f"  状态: {'✅ 开启' if filter_cfg.get('enabled') else '❌ 关闭'}")
        filter_action = filter_cfg.get('filter_action', 'warn')
        action_names = {"warn": "警告", "mute": "禁言", "kick": "踢出", "blacklist": "拉黑"}
        result_parts.append(f"  过滤动作: {action_names.get(filter_action, filter_action)}")
        result_parts.append(f"  禁言时长: {filter_cfg.get('filter_mute_duration', 300)}秒")
        result_parts.append("")
        
        # ========== 黑名单设置 ==========
        from .qqadmin.blacklist import get_blacklist_settings, list_blacklist
        blacklist_settings = get_blacklist_settings(group_id)
        blacklist = list_blacklist(group_id)
        
        result_parts.append("【黑名单系统】")
        result_parts.append(f"  状态: {'✅ 开启' if self._check_qqadmin_feature(event, self.qqadmin_enable_blacklist, 'blacklist_enabled') else '❌ 关闭'}")
        result_parts.append(f"  自动踢出: {'✅ 是' if blacklist_settings.get('auto_kick_blacklisted') else '❌ 否'}")
        result_parts.append(f"  退群拉黑: {'✅ 是' if blacklist_settings.get('auto_blacklist_on_leave') else '❌ 否'}")
        result_parts.append(f"  黑名单用户: {len(blacklist)}人")
        result_parts.append("")
        
        result_parts.append("━━━━━━━━━━━━━━")
        result_parts.append("使用 /<系统>系统 查看各系统详细命令")
        
        yield event.plain_result("\n".join(result_parts))

    @filter.command("回答验证")
    async def answer_verification_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_answer_verification(event):
            yield result

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_direct_verification(self, event: AstrMessageEvent) -> None:
        if not self.qqadmin_enable_audit:
            return
        
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            return
        
        user_id = str(event.message_obj.sender.user_id)
        message_str = (event.message_str or "").strip()
        
        if not message_str:
            return
        
        # 跳过命令格式的消息（让专门的命令处理器处理）
        # 如果消息以"/"开头，说明是命令格式，跳过
        if message_str.startswith("/"):
            logger.info(f"跳过命令消息: {message_str}")
            return
        
        # 如果消息包含"回答验证"，说明是命令格式，跳过
        if "回答验证" in message_str:
            logger.info(f"跳过包含'回答验证'的消息: {message_str}")
            return
        
        # 如果消息包含"帮助"、"菜单"、"专属回复"等关键词，跳过（让命令处理器处理）
        help_keywords = ["帮助", "菜单", "命令", "专属回复", "黑名单", "禁言", "踢出", 
                        "撤回", "欢迎", "自动回复", "签到", "排行榜", "定时消息", 
                        "入群审核", "分群管理", "群设置", "群管功能", "审核设置",
                        "群白名单", "过滤群", "功能开关", "群数据", "统计数据",
                        "审核列表", "审核模式", "验证模式", "同意", "拒绝",
                        "用户专属回复系统", "专属回复系统", "设置验证", "查看系统设置",
                        "文转图", "开启文转图", "关闭文转图"]
        for keyword in help_keywords:
            if keyword in message_str:
                logger.info(f"跳过包含命令关键词的消息: {message_str}")
                # 不停止事件传播，让其他处理器（如 handle_audit_commands）处理
                return
        
        # 只处理纯数字消息（直接输入验证码的情况）
        if not message_str.isdigit():
            logger.info(f"跳过非数字消息: {message_str}")
            return
        
        from .qqadmin.audit import (get_pending_verification, verify_answer, approve_join_request, 
                                    reject_join_request, remove_pending_request,
                                    get_max_verify_attempts, increment_pending_attempts,
                                    get_group_data, get_verification_timeout)
        
        pending = get_pending_verification(group_id, user_id)
        if not pending:
            return
        
        logger.info(f"检测到待验证用户 {user_id} 发送消息: {message_str}")
        
        # 从 pending 中获取实际的 approval_mode（与入群时一致）
        approval_mode = pending.get("approval_mode", "direct")
        max_attempts = get_max_verify_attempts(group_id)
        
        # 处理不同验证模式
        if approval_mode == "math":
            challenge_id = pending.get("challenge_id", "")
            is_correct, should_reject = verify_answer(group_id, challenge_id, message_str)
            challenges = get_group_data(group_id, "audit_challenges", {})
            correct_code = challenges.get(challenge_id, {}).get("answer", "")
        elif approval_mode == "id":
            correct_code = pending.get("verification_question", "")
            is_correct = (message_str == correct_code)
            logger.info(f"ID验证比对: 用户输入='{message_str}', verify_id='{correct_code}', 结果={is_correct}")
        elif approval_mode in ["code", "direct"]:
            correct_code = pending.get("approval_code", "")
            is_correct = (message_str == correct_code)
            logger.info(f"验证码比对: 用户输入='{message_str}', approval_code='{correct_code}', 结果={is_correct}")
        else:
            correct_code = pending.get("approval_code", "")
            is_correct = (message_str == correct_code)
            logger.info(f"验证码比对(默认): 用户输入='{message_str}', approval_code='{correct_code}', 结果={is_correct}")
        
        if is_correct:
            approve_join_request(group_id, user_id, "system", "验证通过")
            # 清理挑战数据
            challenge_id = pending.get("challenge_id")
            if challenge_id:
                remove_challenge(group_id, challenge_id)
                logger.info(f"验证成功，清理挑战数据: {challenge_id}")
            
            from astrbot.core.message.components import At, Plain
            success_msg = f"✅ 验证成功！欢迎加入本群！"
            success_chain = [At(qq=int(user_id)), Plain(" " + success_msg)]
            await event.send(event.chain_result(success_chain))
            logger.info(f"用户 {user_id} 验证成功")
            
            # 更新邀请状态为已入群
            update_invite_status(group_id, user_id, "joined")
            logger.info(f"更新邀请状态: user_id={user_id}, group_id={group_id}, status=joined")
            
            # 验证成功后发送入群欢迎消息（已包含艾特）
            await self._send_welcome_message(event, group_id, user_id, pending.get("user_name", ""))
            event.stop_event()
        else:
            # 增加尝试次数
            current_attempts = increment_pending_attempts(group_id, user_id)
            remaining_attempts = max_attempts - current_attempts
            from astrbot.core.message.components import At, Plain
            
            def build_fail_chain_at(message_text: str):
                return [At(qq=int(user_id)), Plain(" " + message_text)]
            
            if current_attempts >= max_attempts:
                # 再次检查用户是否还在 pending 队列中（可能已验证通过）
                current_pending = get_pending_verification(group_id, user_id)
                if not current_pending:
                    logger.info(f"用户 {user_id} 已不在 pending 队列中，跳过踢人操作")
                    event.stop_event()
                    return
                
                # 超过最大尝试次数，踢出群聊
                try:
                    platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                    if platform:
                        # 先发送踢出原因通知（包含艾特）
                        kick_notice_chain = [At(qq=int(user_id)), Plain(
                            f" ⚠️ 验证失败次数过多\n"
                            f"📊 尝试次数: {current_attempts}/{max_attempts}\n"
                            f"🚪 已被移出群聊"
                        )]
                        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                        onebot_messages = await AiocqhttpMessageEvent._parse_onebot_json(kick_notice_chain)
                        await platform.get_client().call_action(
                            action="send_group_msg",
                            group_id=int(group_id),
                            message=onebot_messages
                        )
                        # 然后踢出用户
                        await platform.get_client().call_action(
                            action="set_group_kick",
                            group_id=int(group_id),
                            user_id=int(user_id),
                            reject_add_request=False
                        )
                        logger.info(f"验证失败踢出用户: user_id={user_id}, group_id={group_id}, 尝试次数={current_attempts}")
                    else:
                        logger.error("无法获取平台实例")
                except Exception as e:
                    logger.error(f"踢出用户失败: {e}")
                
                remove_pending_request(group_id, user_id)
                
                # 更新邀请状态为已拒绝
                update_invite_status(group_id, user_id, "rejected")
                logger.info(f"更新邀请状态: user_id={user_id}, group_id={group_id}, status=rejected")
                
                # 如果启用了验证失败自动加入黑名单，则将用户加入黑名单
                if self.qqadmin_auto_blacklist_on_verify_fail and self.qqadmin_enable_blacklist:
                    from .qqadmin.blacklist import add_to_blacklist
                    add_to_blacklist(group_id, user_id, reason="验证失败", operator_id="system", ban_type="permanent")
                    logger.info(f"验证失败自动加入黑名单: user_id={user_id}, group_id={group_id}")
                    fail_msg = (
                        f"❌ 验证失败！\n"
                        f"📊 已尝试 {current_attempts}/{max_attempts} 次\n"
                        f"🚫 已被移出群聊并加入黑名单\n"
                        f"🔒 您将无法再次加入本群"
                    )
                    yield event.chain_result(build_fail_chain_at(fail_msg))
                    event.stop_event()
                else:
                    fail_msg = (
                        f"❌ 验证失败！\n"
                        f"📊 已尝试 {current_attempts}/{max_attempts} 次\n"
                        f"🚪 已被移出群聊"
                    )
                    yield event.chain_result(build_fail_chain_at(fail_msg))
                    event.stop_event()
            else:
                timeout = get_verification_timeout(group_id)
                # 根据验证模式显示不同的提示
                if approval_mode == "math":
                    hint = "💡 请重新计算答案"
                elif approval_mode == "id":
                    hint = f"💡 请输入你的验证ID: {correct_code}"
                else:
                    hint = f"💡 请输入审批码: {correct_code}"
                
                error_msg = (
                    f"❌ 答案错误！\n"
                    f"📊 第 {current_attempts}/{max_attempts} 次尝试\n"
                    f"⏱️ 还剩 {remaining_attempts} 次机会\n"
                    f"⌛ 验证超时时间: {timeout}秒\n"
                    f"{hint}"
                )
                yield event.chain_result(build_fail_chain_at(error_msg))
                event.stop_event()

    # ==================== 统计系统命令 ====================
    @filter.command("查看邀请排行")
    async def view_invite_ranking_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_stats, "stats_enabled"):
            return
        async for result in self._handle_view_invite_ranking(event):
            yield result
        event.stop_event()

    @filter.command("查看邀请记录")
    async def view_invite_records_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_stats, "stats_enabled"):
            return
        async for result in self._handle_view_invite_records(event):
            yield result
        event.stop_event()

    @filter.command("检查异常邀请")
    async def check_abnormal_invites_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_stats, "stats_enabled"):
            return
        async for result in self._handle_check_abnormal_invites(event):
            yield result
        event.stop_event()

    @filter.command("我的邀请")
    async def view_my_invites_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_stats, "stats_enabled"):
            return
        async for result in self._handle_view_my_invites(event):
            yield result
        event.stop_event()

    @filter.command("邀请统计")
    async def view_invite_stats_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_stats, "stats_enabled"):
            return
        async for result in self._handle_view_invite_stats(event):
            yield result
        event.stop_event()

    # ==================== 操作日志命令 ====================
    @filter.command("查看操作日志")
    async def view_operation_logs_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_audit, "audit_enabled"):
            return
        async for result in self._handle_view_operation_logs(event):
            yield result

    @filter.command("查看操作统计")
    async def view_operation_stats_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_audit, "audit_enabled"):
            return
        async for result in self._handle_view_operation_stats(event):
            yield result

    @filter.command("清空操作日志")
    async def clear_operation_logs_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event) or not self._check_qqadmin_feature(event, self.qqadmin_enable_audit, "audit_enabled"):
            return
        async for result in self._handle_clear_operation_logs(event):
            yield result

    @filter.command("清空邀请记录")
    async def clear_invite_stats_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_clear_invite_stats(event):
            yield result

    @filter.command("文转图")
    async def text_to_image_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_text_to_image(event):
            yield result

    @filter.command("图转文")
    async def image_to_text_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_image_to_text(event):
            yield result

    @filter.command("开机")
    async def power_on_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_power_on(event):
            yield result

    @filter.command("关机")
    async def power_off_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_power_off(event):
            yield result

    # ==================== 撤回系统命令 ====================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_recall_commands(self, event: AstrMessageEvent) -> None:
        message_str = event.message_str or ""

        if not message_str.strip():
            return

        is_owner = self._is_owner(event)
        cmd = message_str.lstrip('/').strip()

        # 检查用户是否正在进行验证，如果是，则跳过撤回命令处理
        from .qqadmin.audit import get_pending_verification
        group_id = str(event.message_obj.group_id)
        sender = event.message_obj.sender
        user_id = str(sender.user_id if hasattr(sender, 'user_id') else "")
        if group_id and group_id != "None" and user_id:
            pending = get_pending_verification(group_id, user_id)
            if pending:
                # 用户正在进行验证，跳过撤回命令处理
                logger.info(f"用户 {user_id} 正在进行验证，跳过撤回命令处理")
                return

        # 检查是否是撤回相关命令
        recall_commands = ["开启撤回", "关闭撤回", "开启自身撤回", "关闭自身撤回",
                          "添加撤回关键词", "删除撤回关键词", "设置撤回模式",
                          "查看撤回关键词", "添加撤回白名单", "撤回", "设置撤回时间",
                          "开启撤回通知", "关闭撤回通知", "设置撤回通知",
                          "撤回帮助", "查看自身撤回"]
        
        # 如果不是撤回相关命令，直接返回，让事件继续传递
        # 注意：单独的数字命令不触发撤回，必须是"撤回 数字"格式
        is_recall_command = cmd in recall_commands or \
                          cmd.startswith("添加撤回关键词") or \
                          cmd.startswith("删除撤回关键词") or \
                          cmd.startswith("设置撤回模式") or \
                          cmd.startswith("添加撤回白名单") or \
                          cmd.startswith("撤回 ") or \
                          cmd.startswith("设置撤回时间") or \
                          cmd.startswith("设置撤回通知") or \
                          cmd.startswith("开启自身撤回") or \
                          cmd.startswith("关闭自身撤回") or \
                          cmd.startswith("设置自身撤回时间") or \
                          cmd.startswith("查看自身撤回")
        
        # 排除单独数字的命令（必须使用"撤回 数字"格式）
        if cmd.isdigit():
            is_recall_command = False
        
        if not is_recall_command:
            return
        
        logger.info(f"撤回命令处理: 原始message_str='{message_str}', cmd='{cmd}', is_owner={is_owner}")
        
        # 对于开启/关闭命令，只检查群管总开关和权限，不检查功能配置
        if cmd in ["开启撤回", "关闭撤回", "开启自身撤回", "关闭自身撤回"]:
            # 检查群管总开关
            if not getattr(self, 'qqadmin_enable_group_manage', True):
                return
            # 这些命令需要管理员权限，在处理函数中检查
        else:
            # 对于其他撤回命令，需要检查功能是否开启
            if not self._check_qqadmin_feature(event, self.qqadmin_enable_recall, "recall_enabled"):
                return

        if cmd == "开启撤回":
            async for result in self._handle_enable_recall(event):
                yield result
            event.stop_event()
        elif cmd == "关闭撤回":
            async for result in self._handle_disable_recall(event):
                yield result
            event.stop_event()
        elif cmd == "开启自身撤回":
            async for result in self._handle_enable_self_recall(event):
                yield result
            event.stop_event()
        elif cmd == "关闭自身撤回":
            async for result in self._handle_disable_self_recall(event):
                yield result
            event.stop_event()
        elif cmd.startswith("添加撤回关键词"):
            async for result in self._handle_add_recall_keyword(event):
                yield result
            event.stop_event()
        elif cmd.startswith("删除撤回关键词"):
            async for result in self._handle_remove_recall_keyword(event):
                yield result
            event.stop_event()
        elif cmd.startswith("设置撤回模式"):
            async for result in self._handle_set_recall_mode(event):
                yield result
            event.stop_event()
        elif cmd == "查看撤回关键词":
            async for result in self._handle_list_recall_keywords(event):
                yield result
            event.stop_event()
        elif cmd.startswith("添加撤回白名单"):
            async for result in self._handle_add_recall_whitelist(event):
                yield result
            event.stop_event()
        elif cmd.startswith("撤回 "):
            logger.info(f"收到撤回命令: {event.message_str}")
            async for result in self._handle_recall_user_messages(event):
                yield result
            event.stop_event()
        elif cmd.startswith("设置撤回时间"):
            async for result in self._handle_set_recall_time(event):
                yield result
            event.stop_event()
        elif cmd.startswith("设置撤回数量"):
            async for result in self._handle_set_recall_max(event):
                yield result
            event.stop_event()
        elif cmd == "开启撤回通知":
            async for result in self._handle_enable_recall_notification(event):
                yield result
            event.stop_event()
        elif cmd == "关闭撤回通知":
            async for result in self._handle_disable_recall_notification(event):
                yield result
            event.stop_event()
        elif cmd.startswith("设置撤回通知"):
            async for result in self._handle_set_recall_notification(event):
                yield result
            event.stop_event()
        elif cmd == "撤回帮助":
            from .qqadmin.recall import get_recall_help_text
            yield event.plain_result(get_recall_help_text())
            event.stop_event()
        elif cmd.startswith("查看自身撤回"):
            async for result in self._handle_view_self_recall(event):
                yield result
            event.stop_event()
        elif cmd.startswith("设置自身撤回时间"):
            async for result in self._handle_set_self_recall_time(event):
                yield result
            event.stop_event()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_recall_auto(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            return

        # 主人不受关机状态限制
        if not self._is_owner(event):
            # 检查分群开机状态
            from .qqadmin.group_config import get_group_data
            features = get_group_data(group_id, "features", {})
            power_status = features.get("power_status", "")
            if power_status == "off":
                return

        # 跳过机器人自己的消息
        user_id = str(event.message_obj.sender.user_id) if hasattr(event.message_obj.sender, 'user_id') else ""
        bot_self_id = str(event.get_self_id()) if hasattr(event, 'get_self_id') else ""
        if user_id == bot_self_id:
            return

        # 检查审核功能是否启用，只有启用时才检查过期验证
        if self._check_qqadmin_feature(event, self.qqadmin_enable_audit, "audit_enabled"):
            try:
                # 遍历所有群清理过期验证
                from .qqadmin.group_config import list_all_groups
                all_groups = list_all_groups()
                for gid in all_groups:
                    clean_expired_challenges_and_kick(gid)
            except Exception as e:
                logger.error(f"检查过期验证失败: {e}")

        is_owner = self._is_owner(event)

        if not self._check_qqadmin_feature(event, self.qqadmin_enable_recall, "recall_enabled"):
            return

        from .qqadmin.recall import get_recall_mode, get_recall_keywords, get_recall_regex_list
        recall_enabled = get_group_feature_setting(group_id, "recall_enabled", None, self.config)
        if recall_enabled is None:
            recall_enabled = self.qqadmin_enable_recall
        if not recall_enabled:
            return

        logger.info(f"handle_recall_auto: group_id={group_id}, is_owner={is_owner}")

        message = event.message_str or ""
        # 过滤空消息和命令消息（以/开头的消息是命令）
        if not message.strip() or message.strip().startswith('/'):
            return
        
        mode = get_recall_mode(group_id)
        logger.info(f"handle_recall_auto: mode={mode}, message='{message}'")

        should_recall = False
        if mode == "keyword":
            keywords = get_recall_keywords(group_id)
            if keywords:
                for keyword in keywords:
                    if keyword in message:
                        should_recall = True
                        logger.info(f"handle_recall_auto: 匹配到关键词'{keyword}'")
                        break
        elif mode == "regex":
            import re
            patterns = get_recall_regex_list(group_id)
            if patterns:
                for pattern in patterns:
                    try:
                        if re.search(pattern, message):
                            should_recall = True
                            break
                    except re.error:
                        continue
        elif mode == "all":
            should_recall = True

        if not should_recall:
            return

        from .qqadmin.recall import is_user_whitelisted, add_whitelist_user
        user_id = str(event.message_obj.sender.user_id) if hasattr(event.message_obj.sender, 'user_id') else ""
        bot_self_id = str(event.get_self_id()) if hasattr(event, 'get_self_id') else ""

        # 强检查：如果是机器人自己的消息，不处理（由 after_message_sent 处理）
        if user_id == bot_self_id:
            logger.info(f"handle_recall_auto: 消息来自机器人自身({bot_self_id})，跳过")
            return

        # 检查是否是主人
        owner_ids_str = [str(oid) for oid in self.owner_ids]
        sender_id_str = str(user_id)
        if sender_id_str in owner_ids_str:
            logger.info(f"handle_recall_auto: 用户{user_id}是机器人主人，跳过")
            return

        # 检查是否是管理员/群主
        if event.role in ["admin", "owner"]:
            logger.info(f"handle_recall_auto: 用户{user_id}是管理员/群主，跳过")
            return

        # 检查白名单
        if user_id and is_user_whitelisted(group_id, user_id):
            logger.info(f"handle_recall_auto: 用户{user_id}在白名单，跳过")
            return

        message_id = getattr(event.message_obj, 'message_id', None)
        if not message_id or message_id == 0 or message_id == "0":
            logger.info(f"handle_recall_auto: message_id无效={message_id}")
            return

        try:
            from .qqadmin.recall import _safe_int
            await event.bot.delete_msg(message_id=_safe_int(message_id, message_id))
            logger.info(f"关键词撤回: 群组 {group_id} 撤回消息 {message_id}")
            
            # 检查是否需要发送通知
            from .qqadmin.group_config import get_group_feature_detail
            recall_config = get_group_feature_detail(group_id, "recall", self.config)
            notification = recall_config.get("notification", False)
            notification_message = recall_config.get("notification_message", "❌ 您的消息包含违规内容，已被撤回")
            
            if notification:
                yield event.plain_result(notification_message)
            
            event.stop_event()
                
        except Exception as e:
            logger.error(f"关键词撤回失败: {e}")

    # ==================== 入群欢迎命令 ====================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_welcome_commands(self, event: AstrMessageEvent) -> None:
        message_str = event.message_str or ""
        cmd = message_str.lstrip('/').strip()
        
        # 检查是否是入群欢迎相关命令
        welcome_commands = ["开启欢迎", "关闭欢迎", "查看欢迎设置"]
        is_welcome_command = cmd in welcome_commands or \
                            cmd.startswith("设置欢迎语") or \
                            cmd.startswith("设置退群语") or \
                            cmd.startswith("设置艾特新人")
        
        if not is_welcome_command:
            return
        
        if not self._check_qqadmin_feature(event, self.qqadmin_enable_welcome, "welcome_enabled"):
            return
        
        if cmd == "开启欢迎":
            async for result in self._handle_enable_welcome(event):
                yield result
            event.stop_event()
        elif cmd == "关闭欢迎":
            async for result in self._handle_disable_welcome(event):
                yield result
            event.stop_event()
        elif cmd.startswith("设置欢迎语"):
            async for result in self._handle_set_welcome_message(event):
                yield result
            event.stop_event()
        elif cmd.startswith("设置退群语"):
            async for result in self._handle_set_farewell_message(event):
                yield result
            event.stop_event()
        elif cmd == "查看欢迎设置":
            async for result in self._handle_view_welcome_settings(event):
                yield result
            event.stop_event()
        elif cmd.startswith("设置艾特新人"):
            async for result in self._handle_set_at_new_member(event):
                yield result
            event.stop_event()
    async def enable_auto_welcome_command(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        enable_member_tracking(group_id, True)
        yield event.plain_result("✅ 已开启自动欢迎功能\n新成员加入群聊时将自动发送欢迎消息")

    # ==================== 分群设置命令 ====================
    @filter.command("启用功能")
    async def enable_feature_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_enable_feature(event):
            yield result

    @filter.command("禁用功能")
    async def disable_feature_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_disable_feature(event):
            yield result

    @filter.command("查看功能状态")
    async def list_features_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_list_features(event):
            yield result

    @filter.command("设置功能")
    async def set_feature_setting_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_set_feature_setting(event):
            yield result

    @filter.command("重置群组配置")
    async def reset_group_config_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_reset_group_config(event):
            yield result

    @filter.command("克隆全局")
    async def clone_global_config_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_clone_global_config(event):
            yield result

    @filter.command("清除分群数据")
    async def clear_group_data_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_clear_group_data(event):
            yield result

    @filter.command("同步黑名单")
    async def sync_global_blacklist_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_sync_global_blacklist(event):
            yield result

    @filter.command("同步配置")
    async def sync_group_config_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._sync_group_config(event):
            yield result

    @filter.command("配置全局")
    async def config_global_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_config_global(event):
            yield result

    @filter.command("克隆配置")
    async def clone_group_config_command(self, event: AstrMessageEvent) -> None:
        if not self._is_slash_prefix_allowed(event):
            return
        async for result in self._handle_clone_group_config(event):
            yield result

    # ==================== 自定义回复命令 ====================
    @filter.command("设置回复冷却")
    async def set_reply_cooldown_command(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            return
        async for result in self._handle_set_reply_cooldown(event):
            yield result

    async def _show_settings(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        result_parts = []
        result_parts.append("┌──────────────────────────────────────┐")
        result_parts.append("│          ⚙️  群组详细设置            │")
        result_parts.append("└──────────────────────────────────────┘")
        result_parts.append("")
        
        # ========== 全局配置更新检测 ==========
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            config_path = os.path.join(get_astrbot_data_path(), "config", "astrbot_plugin_joker_config.json")
            global_updated_at = int(os.path.getmtime(config_path)) if os.path.exists(config_path) else 0
        except:
            global_updated_at = 0
        
        from .qqadmin.group_config import load_group_config
        group_config = load_group_config(group_id)
        cloned_at = group_config.get("updated_at", 0)
        
        if global_updated_at > cloned_at and global_updated_at > 0:
            result_parts.append("⚠️ 【提示】全局配置已更新")
            result_parts.append("   💡 发送 /克隆全局 同步最新配置")
            result_parts.append("")
        
        # ========== 基础设置 ==========
        settings = load_group_settings()
        group_settings = settings.get(group_id, {})
        allow_slash = group_settings.get("allow_slash_prefix", self.allow_slash_prefix)
        
        from .qqadmin.group_config import get_group_data
        features = get_group_data(group_id, "features", {})
        power_status = features.get("power_status", "")
        
        result_parts.append("📋 【基础设置】")
        result_parts.append(f"   ├─ 斜杠命令: {'✅ 开启' if allow_slash else '❌ 关闭'}")
        result_parts.append(f"   └─ 开机状态: {'🔛 开机' if power_status == 'on' else '🔛 关机' if power_status == 'off' else '🔛 未设置'}")
        result_parts.append("")
        
        # ========== 入群审核设置 ==========
        from .qqadmin.audit import (
            get_max_verify_attempts, get_verification_timeout, get_audit_approval_mode,
            get_pending_verifications, get_whitelist_users, get_audit_settings
        )
        audit_settings = get_audit_settings(group_id)
        max_attempts = get_max_verify_attempts(group_id)
        timeout = get_verification_timeout(group_id)
        approval_mode = get_audit_approval_mode(group_id)
        all_pending = get_pending_verifications()
        pending = [p for p in all_pending if p.get("group_id") == group_id]
        whitelist = get_whitelist_users(group_id)
        
        result_parts.append("🔐 【入群审核】")
        result_parts.append(f"   ├─ 状态: {'✅ 开启' if self._check_qqadmin_feature(event, self.qqadmin_enable_audit, 'audit_enabled') else '❌ 关闭'}")
        result_parts.append(f"   ├─ 审批模式: {approval_mode}")
        result_parts.append(f"   ├─ 最大尝试: {max_attempts}次")
        result_parts.append(f"   ├─ 超时时间: {timeout}秒")
        result_parts.append(f"   ├─ 待验证: {len(pending)}人")
        if pending:
            pending_users = [f"{p.get('user_name', p.get('user_id', '未知'))}({p.get('user_id', '未知')})" for p in pending[:3]]
            result_parts.append(f"   │   {', '.join(pending_users)}")
            if len(pending) > 3:
                result_parts.append(f"   │   ... 还有 {len(pending) - 3} 人")
        result_parts.append(f"   └─ 白名单: {len(whitelist)}人")
        result_parts.append("")
        
        # ========== 自定义回复设置 ==========
        from .qqadmin import list_replies, get_reply_cooldown
        replies = list_replies(group_id)
        cooldown = get_reply_cooldown(group_id)
        exact_replies = replies.get("exact", [])
        fuzzy_replies = replies.get("fuzzy", [])
        regex_replies = replies.get("regex", [])
        
        result_parts.append("💬 【自定义回复】")
        result_parts.append(f"   ├─ 状态: {'✅ 开启' if self._check_qqadmin_feature(event, self.qqadmin_enable_reply, 'reply_enabled') else '❌ 关闭'}")
        result_parts.append(f"   ├─ 冷却时间: {cooldown}秒")
        result_parts.append(f"   ├─ 精确回复: {len(exact_replies)}条")
        if exact_replies:
            result_parts.append(f"   │   {', '.join(exact_replies[:3])}")
            if len(exact_replies) > 3:
                result_parts.append(f"   │   ... 还有 {len(exact_replies) - 3} 条")
        result_parts.append(f"   ├─ 模糊回复: {len(fuzzy_replies)}条")
        if fuzzy_replies:
            result_parts.append(f"   │   {', '.join(fuzzy_replies[:3])}")
            if len(fuzzy_replies) > 3:
                result_parts.append(f"   │   ... 还有 {len(fuzzy_replies) - 3} 条")
        result_parts.append(f"   └─ 正则回复: {len(regex_replies)}条")
        result_parts.append("")
        
        # ========== 撤回系统设置 ==========
        from .qqadmin.recall import (
            is_recall_enabled, get_recall_time, get_recall_mode,
            get_recall_keywords, get_recall_regex_list,
            is_self_recall_enabled, get_self_recall_time
        )
        recall_enabled = is_recall_enabled(group_id)
        recall_time = get_recall_time(group_id)
        recall_mode = get_recall_mode(group_id)
        keywords = get_recall_keywords(group_id)
        patterns = get_recall_regex_list(group_id)
        self_recall_enabled = is_self_recall_enabled(group_id)
        self_recall_time = get_self_recall_time(group_id)

        result_parts.append("🗑️ 【撤回系统】")
        result_parts.append(f"   ├─ 关键词撤回: {'✅ 开启' if recall_enabled else '❌ 关闭'}")
        result_parts.append(f"   ├─ 撤回模式: {recall_mode}")
        result_parts.append(f"   ├─ 撤回时间: {recall_time}秒")
        result_parts.append(f"   ├─ 关键词: {len(keywords)}个")
        if keywords:
            result_parts.append(f"   │   {', '.join(keywords[:3])}")
            if len(keywords) > 3:
                result_parts.append(f"   │   ... 还有 {len(keywords) - 3} 个")
        result_parts.append(f"   ├─ 正则: {len(patterns)}个")
        result_parts.append(f"   └─ 自身撤回: {'✅ 开启(' + str(self_recall_time) + '秒)' if self_recall_enabled else '❌ 关闭'}")
        result_parts.append("")
        
        # ========== 欢迎系统设置 ==========
        from .qqadmin.welcome import (
            is_welcome_enabled, get_welcome_message, get_farewell_message,
            is_farewell_enabled, get_welcome_delay, get_welcome_type
        )
        welcome_enabled = is_welcome_enabled(group_id)
        farewell_enabled = is_farewell_enabled(group_id)
        welcome_msg = get_welcome_message(group_id)
        farewell_msg = get_farewell_message(group_id)
        welcome_delay = get_welcome_delay(group_id)
        welcome_type = get_welcome_type(group_id)
        
        result_parts.append("👋 【欢迎系统】")
        result_parts.append(f"   ├─ 入群欢迎: {'✅ 开启' if welcome_enabled else '❌ 关闭'}")
        result_parts.append(f"   ├─ 退群通知: {'✅ 开启' if farewell_enabled else '❌ 关闭'}")
        result_parts.append(f"   ├─ 欢迎类型: {'自动' if welcome_type == 'auto' else '关键词'}")
        result_parts.append(f"   ├─ 延迟发送: {welcome_delay}秒")
        if welcome_msg:
            result_parts.append(f"   ├─ 欢迎语: {welcome_msg[:40] + '...' if len(welcome_msg) > 40 else welcome_msg}")
        result_parts.append(f"   └─ 退群语: {farewell_msg[:40] + '...' if len(farewell_msg) > 40 else farewell_msg}")
        result_parts.append("")
        
        # ========== 禁言系统设置 ==========
        from .qqadmin.mute import get_mute_settings, list_muted_users
        mute_settings = get_mute_settings(group_id)
        muted_users = list_muted_users(group_id)
        
        result_parts.append("🔇 【禁言系统】")
        result_parts.append(f"   ├─ 状态: {'✅ 开启' if self._check_qqadmin_feature(event, self.qqadmin_enable_mute, 'mute_enabled') else '❌ 关闭'}")
        result_parts.append(f"   ├─ 默认级别: {mute_settings.get('level', 5)}级")
        result_parts.append(f"   ├─ 默认时长: {mute_settings.get('duration', 300)}秒")
        result_parts.append(f"   └─ 当前禁言: {len(muted_users)}人")
        if muted_users:
            muted_list = [f"{u.get('user_name', u.get('user_id', '未知'))}({u.get('user_id', '未知')})" for u in muted_users[:3]]
            result_parts.append(f"       {', '.join(muted_list)}")
            if len(muted_users) > 3:
                result_parts.append(f"       ... 还有 {len(muted_users) - 3} 人")
        result_parts.append("")
        
        # ========== 踢出系统设置 ==========
        from .qqadmin.kick import get_kick_settings
        kick_settings = get_kick_settings(group_id)
        
        result_parts.append("🚫 【踢出系统】")
        result_parts.append(f"   ├─ 状态: {'✅ 开启' if self._check_qqadmin_feature(event, self.qqadmin_enable_kick, 'kick_enabled') else '❌ 关闭'}")
        result_parts.append(f"   ├─ 自动拉黑: {'✅ 是' if kick_settings.get('auto_blacklist') else '❌ 否'}")
        result_parts.append(f"   └─ 踢出上限: {kick_settings.get('limit', 3)}次")
        result_parts.append("")
        
        # ========== 关键词过滤设置 ==========
        from .qqadmin.group_config import get_group_config
        group_cfg = get_group_config(group_id)
        filter_cfg = group_cfg.get("features", {}).get("keyword_filter", {})
        
        result_parts.append("🔍 【关键词过滤】")
        result_parts.append(f"   ├─ 状态: {'✅ 开启' if filter_cfg.get('enabled') else '❌ 关闭'}")
        result_parts.append(f"   ├─ 过滤动作: {filter_cfg.get('filter_action', 'warn')}")
        result_parts.append(f"   ├─ 禁言时长: {filter_cfg.get('filter_mute_duration', 300)}秒")
        result_parts.append(f"   └─ 管理员跳过: {'✅ 是' if filter_cfg.get('admin_bypass') else '❌ 否'}")
        result_parts.append("")
        
        # ========== 黑名单设置 ==========
        from .qqadmin.blacklist import get_blacklist_settings, list_blacklist
        blacklist_settings = get_blacklist_settings(group_id)
        blacklist = list_blacklist(group_id)
        
        result_parts.append("⚫ 【黑名单系统】")
        result_parts.append(f"   ├─ 状态: {'✅ 开启' if self._check_qqadmin_feature(event, self.qqadmin_enable_blacklist, 'blacklist_enabled') else '❌ 关闭'}")
        result_parts.append(f"   ├─ 自动踢出: {'✅ 是' if blacklist_settings.get('auto_kick_blacklisted') else '❌ 否'}")
        result_parts.append(f"   ├─ 退群拉黑: {'✅ 是' if blacklist_settings.get('auto_blacklist_on_leave') else '❌ 否'}")
        result_parts.append(f"   └─ 黑名单用户: {len(blacklist)}人")
        if blacklist:
            blacklist_list = [f"{u.get('user_name', u.get('user_id', '未知'))}({u.get('user_id', '未知')})" for u in blacklist[:3]]
            result_parts.append(f"       {', '.join(blacklist_list)}")
            if len(blacklist) > 3:
                result_parts.append(f"       ... 还有 {len(blacklist) - 3} 人")
        result_parts.append("")
        
        # ========== 用户专属回复设置 ==========
        from .qqadmin.player_reply import list_player_replies
        player_replies = list_player_replies(group_id)

        result_parts.append("🎯 【用户专属回复】")
        result_parts.append(f"   ├─ 状态: {'✅ 开启' if self._check_qqadmin_feature(event, self.qqadmin_enable_player_reply, 'player_reply_enabled') else '❌ 关闭'}")
        result_parts.append(f"   └─ 专属回复用户: {len(player_replies)}人")
        if player_replies:
            reply_users = []
            for r in player_replies[:3]:
                msg_count = len(r.get('messages', []))
                user_name = r.get('user_name', r.get('user_id', '未知'))
                reply_users.append(f"{user_name}({r.get('user_id', '未知')})[{msg_count}条]")
            result_parts.append(f"       {', '.join(reply_users)}")
            if len(player_replies) > 3:
                result_parts.append(f"       ... 还有 {len(player_replies) - 3} 人")
        result_parts.append("")
        
        # ========== 邀请统计设置 ==========
        result_parts.append("📊 【邀请统计】")
        result_parts.append(f"   └─ 状态: {'✅ 开启' if self._check_qqadmin_feature(event, self.qqadmin_enable_stats, 'stats_enabled') else '❌ 关闭'}")
        result_parts.append("")
        
        # ========== 文转图设置 ==========
        from .qqadmin.group_config import get_group_data
        text_to_image_config = get_group_data(group_id, "features", {}).get("text_to_image", {})
        
        result_parts.append("🖼️ 【文转图】")
        result_parts.append(f"   ├─ 状态: {'✅ 开启' if text_to_image_config.get('enable_text_to_image') else '❌ 关闭'}")
        result_parts.append(f"   ├─ 字体大小: {text_to_image_config.get('font_size', 16)}")
        result_parts.append(f"   ├─ 字体颜色: {text_to_image_config.get('font_color', '#333333')}")
        result_parts.append(f"   ├─ 背景颜色: {text_to_image_config.get('bg_color', '#ffffff')}")
        result_parts.append(f"   ├─ 行间距: {text_to_image_config.get('line_spacing', 4)}")
        result_parts.append(f"   └─ 内边距: {text_to_image_config.get('padding', 20)}")
        result_parts.append("")
        
        # ========== 定时消息设置 ==========
        from .qqadmin.group_config import get_group_config
        group_cfg = get_group_config(group_id)
        timed_message_cfg = group_cfg.get("features", {}).get("timed_message", {})
        
        result_parts.append("⏰ 【定时消息】")
        result_parts.append(f"   ├─ 状态: {'✅ 开启' if timed_message_cfg.get('enabled') else '❌ 关闭'}")
        
        from .qqadmin.timed_message import list_timed_messages
        timed_messages = list_timed_messages(group_id)
        result_parts.append(f"   └─ 定时任务: {len(timed_messages)}个")
        if timed_messages:
            for tm in timed_messages[:3]:
                result_parts.append(f"       {tm.get('time', '未知')} - {tm.get('message', '')[:30]}")
            if len(timed_messages) > 3:
                result_parts.append(f"       ... 还有 {len(timed_messages) - 3} 个")
        result_parts.append("")
        
        # ========== 分群管理员设置 ==========
        sub_admin_cfg = group_cfg.get("features", {}).get("sub_admin", {})
        
        result_parts.append("🧑‍💼 【分群管理员】")
        result_parts.append(f"   ├─ 状态: {'✅ 开启' if sub_admin_cfg.get('enabled') else '❌ 关闭'}")
        
        from .qqadmin.group_config import get_group_admins
        sub_admins = get_group_admins(group_id)
        result_parts.append(f"   └─ 管理员: {len(sub_admins)}人")
        if sub_admins:
            admin_list = [f"{a.get('user_name', a.get('user_id', '未知'))}({a.get('user_id', '未知')})" for a in sub_admins[:3]]
            result_parts.append(f"       {', '.join(admin_list)}")
            if len(sub_admins) > 3:
                result_parts.append(f"       ... 还有 {len(sub_admins) - 3} 人")
        result_parts.append("")
        
        result_parts.append("┌──────────────────────────────────────┐")
        result_parts.append("│ 💡 使用 /<系统>系统 查看详细命令     │")
        result_parts.append("│ 💡 使用 /查看系统设置 查看功能开关   │")
        result_parts.append("└──────────────────────────────────────┘")
        
        yield event.plain_result("\n".join(result_parts))

    async def _set_slash_setting(self, event: AstrMessageEvent, value: bool) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        set_group_setting(group_id, "allow_slash_prefix", value)
        status = "✅ 已开启" if value else "❌ 已关闭"
        yield event.plain_result(f"{status} 斜杠前缀命令功能。")

    async def _query_auth_status(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群组未授权，请先激活卡密。")
            return
        auth_data = load_auth_data()
        if group_id not in auth_data["groups"]:
            yield event.plain_result("❌ 该群组未授权。")
            return
        info = auth_data["groups"][group_id]
        expire = info.get("expire", 0)
        activated_at = info.get("activated_at", 0)
        now = int(time.time())
        is_expired = expire < now
        remaining_seconds = expire - now
        
        if is_expired:
            expired_days = abs(remaining_seconds) // 86400
            status = f"已过期 {expired_days} 天"
        else:
            remaining_days = remaining_seconds // 86400
            remaining_hours = (remaining_seconds % 86400) // 3600
            status = f"剩余 {remaining_days} 天 {remaining_hours} 小时"
        
        if self.auth_detail_query and await self._is_admin_or_owner_async(event):
            activated_time = time.strftime('%Y-%m-%d %H:%M', time.localtime(activated_at)) if activated_at > 0 else "未知"
            expire_time = time.strftime('%Y-%m-%d %H:%M', time.localtime(expire)) if expire > 0 else "未知"
            yield event.plain_result(
                f"📊 群组授权状态\n\n"
                f"群组ID: {group_id}\n"
                f"状态: {'⚠️ ' if is_expired else '✅ '}{status}\n"
                f"首次激活: {activated_time}\n"
                f"到期时间: {expire_time}"
            )
        else:
            yield event.plain_result(
                f"📊 授权状态: {'⚠️ ' if is_expired else '✅ '}{status}"
            )

    async def _show_reply_help(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id) if event.message_obj else None
        # 从分群配置获取回复功能状态
        from .qqadmin.group_config import load_group_config
        group_config = load_group_config(group_id) if group_id else {}
        reply_feature = group_config.get("features", {}).get("reply", {}) if group_config else {}
        enable_reply = reply_feature.get("enabled", False)
        # 从回复数据中获取该群的冷却时间配置
        cooldown = get_reply_cooldown(group_id) if group_id else 0

        help_text = f"""📝 自定义回复
━━━━━━━━━━━━━━
【当前配置】
  功能开关: {'✅ 开启' if enable_reply else '❌ 关闭'}
  冷却时间: {cooldown}秒

【命令】
  /添加回复 <问题> <答案>   添加精确匹配回复
  /模糊添加 <问题> <答案>   添加模糊匹配回复
  /删除 <问题>              删除指定回复
  /查看回复列表             查看所有回复
  /清空回复                 清空所有回复
  /设置回复冷却 <秒数>      设置回复冷却时间

━━━━━━━━━━━━━━
示例:
/添加回复 今天吃什么 我请你吃鸡！
/模糊添加 游戏 你想玩什么游戏？"""

        yield event.plain_result(help_text)

    async def _show_mute_help(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能使用此功能。")
            return

        help_text = """禁言系统

/禁言 @用户 [时长] [理由]
/解除禁言 @用户
/查看禁言列表
/设置禁言级别 <级别>
/设置禁言时长 <秒数>"""

        yield event.plain_result(help_text)

    async def _show_kick_help(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能使用此功能。")
            return

        help_text = """踢出系统

/踢出 @用户 [理由]
/连带踢出 @用户
/查看踢出记录
/清除踢出记录
/设置踢出上限 <次数>
/设置踢出拉黑 <开/关>"""

        yield event.plain_result(help_text)

    async def _show_blacklist_help(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能使用此功能。")
            return

        help_text = """黑名单系统

/拉黑 @用户 [天数] [理由]
/删黑 <用户ID>
/查看黑名单
/清空黑名单
/设置退群入黑 <开/关>
/设置自动踢出 <开/关>"""
        yield event.plain_result(help_text)

    async def _show_audit_help(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能使用此功能。")
            return

        group_id = str(event.message_obj.group_id)

        enable_audit = get_group_setting(group_id, "qqadmin.audit.enable_audit", None)
        if enable_audit is None:
            enable_audit = self.config.get("qqadmin", {}).get("audit", {}).get("enable_audit", False)

        approval_mode = get_group_setting(group_id, "qqadmin.audit.default_approval_mode", None)
        if approval_mode is None:
            approval_mode = self.config.get("qqadmin", {}).get("audit", {}).get("default_approval_mode", "math")

        mode_names = {"direct": "直接审批", "code": "审批码", "math": "算数验证", "id": "ID验证"}

        help_text = f"""入群审核

/开启审核
/关闭审核
/审核通过 @用户
/拒绝 @用户
/查看待审核
/添加白名单
/设置审批模式 <模式>
/设置验证超时 <秒数>
/设置管理员免审 <开/关>

当前: {'✅ 开启' if enable_audit else '❌ 关闭'} · 模式: {mode_names.get(approval_mode, approval_mode)}"""

        yield event.plain_result(help_text)

    async def _show_stats_help(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能使用此功能。")
            return
        help_text = """统计系统

/查看邀请排行
/我的邀请
/邀请统计
/检查异常邀请
/清空邀请记录"""
        yield event.plain_result(help_text)

    async def _show_recall_help(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能使用此功能。")
            return

        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        # 从分群配置获取撤回功能状态
        from .qqadmin.group_config import load_group_config
        group_config = load_group_config(group_id)
        recall_feature = group_config.get("features", {}).get("recall", {}) if group_config else {}
        enable_recall = recall_feature.get("enabled", False)

        help_text = f"""撤回系统

/开启撤回
/关闭撤回
/撤回 <条数> [时间]
/撤回 @用户 <条数>
/开启自身撤回
/关闭自身撤回
/设置自身撤回时间 <秒数>
/查看自身撤回
/添加撤回关键词 <词>
/删除撤回关键词 <词>
/查看撤回关键词

当前: {'✅ 开启' if enable_recall else '❌ 关闭'}"""
        yield event.plain_result(help_text)

    async def _show_welcome_help(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能使用此功能。")
            return

        group_id = str(event.message_obj.group_id)

        welcome_cfg = self.config.get("qqadmin", {}).get("welcome", {})
        enable_welcome = get_group_setting(group_id, "qqadmin.welcome.enable_welcome", None)
        if enable_welcome is None:
            enable_welcome = welcome_cfg.get("enable_welcome", False)

        at_new_member = get_group_setting(group_id, "qqadmin.welcome.at_new_member", None)
        if at_new_member is None:
            at_new_member = True

        help_text = f"""入群欢迎

/开启欢迎
/关闭欢迎
/设置欢迎语 <内容>
/设置退群语 <内容>
/设置艾特新人 <开/关>
/查看欢迎设置

当前: {'✅ 开启' if enable_welcome else '❌ 关闭'} · 艾特新人{'✅ 开' if at_new_member else '❌ 关'}"""
        yield event.plain_result(help_text)

    async def _show_group_config_help(self, event: AstrMessageEvent) -> None:
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._check_feature_permission_async(event, "group_config", "use"):
            yield event.plain_result("❌ 权限不足，无法使用此功能。")
            return
        help_text = """分群设置

/克隆全局 - 同步全局配置（保留分群数据）
/清除分群数据 - 清除所有数据恢复默认
/克隆配置 <群ID>
/同步配置
/启用功能 <功能>
/禁用功能 <功能>
/查看功能状态
/设置功能 <功能> <项> <值>

功能名: reply · mute · kick · blacklist · audit · recall · welcome · keyword_filter"""
        yield event.plain_result(help_text)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_message(self, event: AstrMessageEvent) -> MessageEventResult:
        message = (event.message_str or "").strip()
        group_id = event.message_obj.group_id

        # 检查全局配置是否更新，自动同步到分群
        if group_id:
            self._check_and_auto_sync_global_config(str(group_id))

        # 保存消息到本地缓存
        if group_id:
            try:
                from .qqadmin.message_cache import add_group_message
                message_id = getattr(event.message_obj, "message_id", "") or ""
                user_id = str(event.get_sender_id())
                sender_nickname = event.get_sender_name() or ""
                bot_self_id = str(event.get_self_id())

                # 跳过机器人自己发送的消息（不缓存，用于自身撤回）
                if user_id == bot_self_id:
                    logger.info(f"跳过缓存机器人自己的消息: user_id={user_id}")
                else:
                    # 获取消息内容（去掉CQ码）
                    import re
                    content = re.sub(r'\[CQ:.*?\]', '', message).strip()

                    # 处理 message_id：支持十进制整数和十六进制字符串
                    if isinstance(message_id, str):
                        message_id = message_id.strip()
                        if message_id.startswith('0x'):
                            message_id = int(message_id, 16)
                        elif message_id.isdigit():
                            message_id = int(message_id)
                        else:
                            # 保留原始字符串（十六进制格式）
                            pass
                    elif not isinstance(message_id, int):
                        message_id = str(message_id)

                    add_group_message(
                        str(group_id),
                        message_id,
                        user_id,
                        content,
                        int(time.time())
                    )
            except Exception as e:
                logger.error(f"保存消息到缓存失败: {e}")

        if group_id:
            group_setting = get_group_setting(group_id, "allow_slash_prefix", None)
            if group_setting is True:
                return

        # ==================== 检查黑名单用户发言 ====================
        if group_id and self.qqadmin_enable_blacklist:
            user_id = str(event.get_sender_id())
            # 跳过机器人自己
            if user_id == str(event.get_self_id()):
                pass
            elif is_in_blacklist(str(group_id), user_id):
                blacklist_info = get_blacklist_info(str(group_id), user_id)
                reason = blacklist_info.get("reason", "黑名单用户")
                
                # 撤回用户所有消息
                recalled_count = await self._recall_all_user_messages(event, user_id)
                
                # 踢出用户
                kick_success = await self._kick_blacklisted_user(event, user_id, reason)
                
                if kick_success:
                    if recalled_count > 0:
                        yield event.plain_result(f"⚠️ 检测到黑名单用户发言！\n📋 用户: {user_id}\n🔍 原因: {reason}\n🗑️ 已撤回 {recalled_count} 条消息并踢出群聊")
                    else:
                        yield event.plain_result(f"⚠️ 检测到黑名单用户发言！\n📋 用户: {user_id}\n🔍 原因: {reason}\n🚪 已踢出群聊")
                else:
                    if recalled_count > 0:
                        yield event.plain_result(f"⚠️ 检测到黑名单用户发言！\n📋 用户: {user_id}\n🔍 原因: {reason}\n🗑️ 已撤回 {recalled_count} 条消息，踢出失败")
                    else:
                        yield event.plain_result(f"⚠️ 检测到黑名单用户发言！\n📋 用户: {user_id}\n🔍 原因: {reason}")
                return

        # ==================== 步骤1: 命令预处理 ====================
        # 获取无斜杠命令用于路由
        cmd = message.lstrip('/')
        
        # ==================== 步骤2: 自定义回复命令 ====================
        # 自定义回复命令未通过装饰器注册，在此处理
        async for result in self._handle_reply_commands(event, message, cmd):
            yield result
            return
        
        # ==================== 步骤3: 用户专属回复 ====================
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        
        # 主人不受关机状态限制
        if not self._is_owner(event):
            if group_id and group_id != "None":
                from .qqadmin.group_config import get_group_data
                features = get_group_data(group_id, "features", {})
                power_status = features.get("power_status", "")
                if power_status == "off":
                    return
        
        logger.info(f"用户专属回复检查: qqadmin_enable_player_reply={self.qqadmin_enable_player_reply}")
        if not self.qqadmin_enable_player_reply:
            return
            
        if not group_id or group_id == "None":
            return
            
        # 检查分群配置中专属回复功能是否启用
        from .qqadmin.group_config import is_feature_enabled
        if not is_feature_enabled(group_id, "player_reply", self.config):
            return
            
        user_id = str(event.get_sender_id())
        logger.info(f"用户专属回复: user_id={user_id}, group_id={group_id}")
        from .qqadmin.player_reply import get_player_reply, get_random_message
        
        player_reply = get_player_reply(group_id, user_id)
        logger.info(f"用户专属回复: player_reply={player_reply}")
        if player_reply and player_reply.get('enabled', True):
            messages = player_reply.get('messages', [])
            if messages:
                reply_msg = get_random_message(messages)
                if player_reply.get('at_user', True):
                    from astrbot.core.message.components import At, Plain
                    chain = [At(qq=int(user_id)), Plain(text=f" {reply_msg}")]
                    yield event.chain_result(chain)
                else:
                    yield event.plain_result(reply_msg)
                logger.info(f"用户专属回复触发: user_id={user_id}, reply={reply_msg}")

    # ==================== 命令分类处理函数 ====================
    
    async def _handle_reply_commands(self, event: AstrMessageEvent, message: str, cmd: str) -> None:
        """处理自定义回复相关命令"""
        if cmd == "自定义回复":
            async for result in self._show_reply_help(event):
                yield result
            return
        
        if cmd.startswith("添加回复"):
            async for result in self._handle_add_reply(event):
                yield result
            return
        
        if cmd.startswith("模糊添加"):
            async for result in self._handle_add_fuzzy_reply(event):
                yield result
            return
        
        if cmd.startswith("删除"):
            async for result in self._handle_remove_reply(event):
                yield result
            return
        
        if cmd == "查看回复列表":
            async for result in self._handle_list_replies(event):
                yield result
            return
        
        if cmd == "清空回复":
            async for result in self._handle_clear_replies(event):
                yield result
            return
    
    # ==================== 禁言系统处理函数 ====================
    def _parse_user_id(self, message: str, command_prefixes: list[str]) -> str | None:
        """从消息中解析用户ID，支持多种格式"""
        parts = message.strip().split()
        for part in parts:
            if part.startswith("[CQ:at,qq="):
                start = part.find("qq=") + 3
                end = part.find("]", start)
                if end > start:
                    return part[start:end]
            if part.startswith("[At:") and part.endswith("]"):
                return part[4:-1]
            if "@" in part and "(" in part and ")" in part:
                start = part.find("(") + 1
                end = part.find(")", start)
                if start > 0 and end > start:
                    return part[start:end]
            if part.startswith("@"):
                user_id = part[1:]
                start = user_id.find("(")
                end = user_id.find(")", start)
                if start > 0 and end > start:
                    return user_id[start+1:end]
        return None
    
    def _parse_mute_command(self, message: str) -> tuple[str | None, int, str]:
        """解析禁言命令，提取用户ID、时长和理由"""
        message = message.strip()
        parts = message.split()

        user_id = None
        duration = 0
        reason = ""
        command_prefixes = ["/禁言", "禁言"]

        logger.info(f"禁言解析: 原始消息='{message}', parts={parts}")

        for i, part in enumerate(parts):
            if part in command_prefixes:
                continue

            parsed_uid = self._parse_user_id(part, command_prefixes)
            logger.info(f"禁言解析: part='{part}', parsed_uid={parsed_uid}")
            if parsed_uid:
                user_id = parsed_uid
                continue

            if user_id is None:
                user_id = part
            elif duration == 0:
                if part.lower().endswith("s"):
                    try:
                        duration = int(part[:-1])
                    except ValueError:
                        reason = " ".join(parts[i:])
                        break
                elif part.lower().endswith("m"):
                    try:
                        duration = int(part[:-1]) * 60
                    except ValueError:
                        reason = " ".join(parts[i:])
                        break
                else:
                    try:
                        duration = int(part) * 60
                    except ValueError:
                        reason = " ".join(parts[i:])
                        break
            else:
                reason = " ".join(parts[i:])
                break

        return user_id, duration, reason
    
    async def _handle_mute(self, event: AstrMessageEvent) -> None:
        logger.info(f"收到禁言命令: {event.message_str}")
        
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._is_admin_or_owner_async(event):
            logger.warning(f"禁言命令: 用户权限不足")
            yield event.plain_result("❌ 您没有权限执行此操作。")
            return
        
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            logger.warning("禁言命令: 不在群组中")
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        user_id, duration, reason = self._parse_mute_command(event.message_str or "")
        
        if not user_id:
            logger.warning("禁言命令: 未找到用户ID")
            yield event.plain_result("❌ 用法: /禁言 @用户 [时长] [理由]\n时长单位: 分钟(默认), 秒(5s表示5秒)")
            return
        
        if duration < 0:
            logger.warning("禁言命令: 时长为负数")
            yield event.plain_result("❌ 时长不能为负数")
            return
        
        if duration == 0:
            # 从分群配置获取默认禁言时长
            from .qqadmin.mute import get_mute_settings
            mute_settings = get_mute_settings(group_id)
            duration = mute_settings.get("default_duration", 300)
        
        logger.info(f"禁言命令: 最终 - user_id={user_id}, duration={duration}秒({duration//60}分钟), reason={reason}")

        try:
            await event.bot.set_group_ban(
                group_id=int(group_id),
                user_id=int(user_id),
                duration=duration,
            )

            from .qqadmin.mute import mute_user
            mute_user(group_id, user_id, duration, reason, str(event.message_obj.sender.user_id), "manual")

            add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                              "禁言", user_id, "", reason)
            time_str = f"{duration}秒" if duration < 60 else f"{duration//60}分钟"
            response = f"✅ 已禁言用户 {user_id}，时长: {time_str}"
            if reason:
                response += f"\n📝 理由: {reason}"
            logger.info(f"禁言命令成功: {response}")
            
            yield event.plain_result(response)
        except Exception as e:
            logger.error(f"禁言命令失败: {e}")
            yield event.plain_result("❌ 禁言失败")

    async def _handle_unmute(self, event: AstrMessageEvent) -> None:
        logger.info(f"收到解除禁言命令: {event.message_str}")

        if not await self._is_admin_or_owner_async(event):
            logger.warning("解除禁言命令: 非管理员/群主尝试执行")
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return

        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            logger.warning("解除禁言命令: 不在群组中")
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        user_id = self._parse_user_id(event.message_str or "", ["/解除禁言", "解除禁言", "/解禁", "解禁"])

        if not user_id:
            logger.warning("解除禁言命令: 未找到用户ID")
            yield event.plain_result("❌ 用法: /解除禁言 @用户")
            return

        logger.info(f"解除禁言命令: 准备解除禁言 user_id={user_id}")

        try:
            await event.bot.set_group_ban(
                group_id=int(group_id),
                user_id=int(user_id),
                duration=0,
            )

            from .qqadmin.mute import unmute_user, get_unmute_notification
            unmute_user(group_id, user_id)

            add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                              "解除禁言", user_id, "", "")

            unmute_notification = get_unmute_notification(group_id)
            if unmute_notification:
                try:
                    if hasattr(self.context, 'send_private_message'):
                        await self.context.send_private_message(user_id, unmute_notification)
                    elif hasattr(event, 'reply'):
                        await event.reply(unmute_notification, private=True)
                except Exception as e:
                    logger.error(f"发送解禁私聊通知失败: {e}")

            response = f"✅ 已解除用户 {user_id} 的禁言"
            logger.info(f"解除禁言命令成功: {response}")
            yield event.plain_result(response)
        except Exception as e:
            logger.error(f"解除禁言命令失败: {e}")
            yield event.plain_result("❌ 解除禁言失败")

    async def _handle_list_muted(self, event: AstrMessageEvent) -> None:
        logger.info(f"收到查看禁言列表命令: {event.message_str}")

        if not await self._is_admin_or_owner_async(event):
            logger.warning("查看禁言列表命令: 非管理员/群主尝试执行")
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        
        group_id = str(event.message_obj.group_id)
        logger.info(f"查看禁言列表命令: group_id={group_id}")
        
        if not group_id or group_id == "None":
            logger.warning("查看禁言列表命令: 不在群组中")
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        logger.info("查看禁言列表命令: 调用 list_muted_users")
        muted_users = list_muted_users(group_id)
        logger.info(f"查看禁言列表命令: 查询结果 {len(muted_users)} 个用户")
        
        if not muted_users:
            logger.info("查看禁言列表命令: 当前没有被禁言的用户")
            yield event.plain_result("📋 当前没有被禁言的用户")
            return
        
        result = "📋 禁言列表:\n"
        current_time = int(time.time())
        for user in muted_users:
            expire_time = user.get("expire_time", 0)
            if expire_time == 0:
                time_str = "永久"
            else:
                remaining = expire_time - current_time
                if remaining <= 0:
                    continue
                hours = remaining // 3600
                minutes = (remaining % 3600) // 60
                seconds = remaining % 60
                if hours > 0:
                    time_str = f"剩余 {hours}小时{minutes}分钟{seconds}秒"
                elif minutes > 0:
                    time_str = f"剩余 {minutes}分钟{seconds}秒"
                else:
                    time_str = f"剩余 {seconds}秒"
            result += f"- {user['user_id']}: {time_str} | 理由: {user.get('reason', '')}\n"
        yield event.plain_result(result.strip())

    async def _handle_set_unmute_notification(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        if message.startswith("设置解禁提示"):
            notification = message[len("设置解禁提示"):].strip()
        elif message.startswith("/设置解禁提示"):
            notification = message[len("/设置解禁提示"):].strip()
        else:
            notification = ""

        if not notification:
            yield event.plain_result("❌ 用法: /设置解禁提示 <提示内容>\n示例: /设置解禁提示 您的禁言已解除，请遵守群规")
            return

        from .qqadmin.mute import set_unmute_notification
        set_unmute_notification(group_id, notification)
        yield event.plain_result(f"✅ 已设置解禁提示\n提示内容: {notification}")

    async def _handle_view_unmute_notification(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        from .qqadmin.mute import get_unmute_notification
        notification = get_unmute_notification(group_id)
        if notification:
            yield event.plain_result(f"📝 当前解禁提示:\n{notification}")
        else:
            yield event.plain_result("❌ 当前未设置解禁提示\n使用 /设置解禁提示 <内容> 来设置")

    async def _handle_clear_unmute_notification(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        from .qqadmin.mute import clear_unmute_notification
        clear_unmute_notification(group_id)
        yield event.plain_result("✅ 已清除解禁提示")

    # ==================== 踢出系统处理函数 ====================
    async def _handle_kick(self, event: AstrMessageEvent) -> None:
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._check_feature_permission_async(event, "kick", "use"):
            yield event.plain_result("❌ 您没有权限执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        user_id = self._parse_user_id(event.message_str or "", ["/踢出", "踢出"])
        if not user_id:
            yield event.plain_result("❌ 用法: /踢出 @用户 [理由]")
            return
        
        message = (event.message_str or "").strip()
        parts = message.split()
        reason = ""
        user_found = False
        
        for i, part in enumerate(parts):
            if part.startswith("/踢出") or part == "踢出":
                continue
            if part.startswith("[CQ:at,qq=") or part.startswith("[At:") or part.startswith("@"):
                user_found = True
                continue
            if user_found:
                reason = " ".join(parts[i:])
                break

        try:
            await event.bot.set_group_kick(
                group_id=int(group_id),
                user_id=int(user_id),
                reject_add_request=False,
            )
            add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                              "踢出", user_id, "", reason)
            add_kick_record(group_id, user_id, str(event.message_obj.sender.user_id), reason)
            
            kick_count = get_kick_count(group_id, user_id)
            settings = get_kick_settings(group_id)
            max_kicks = settings.get("max_kicks_before_ban", 3)
            
            if self.qqadmin_auto_blacklist_on_kick:
                add_to_blacklist(group_id, user_id, f"踢出次数达到{kick_count}次")
                yield event.plain_result(f"✅ 已踢出用户 {user_id} 并加入黑名单")
            elif kick_count >= max_kicks:
                yield event.plain_result(f"✅ 已踢出用户 {user_id}\n⚠️ 该用户已被踢出 {kick_count} 次，建议拉黑以防止再次入群\n命令: /拉黑 {user_id}")
            else:
                yield event.plain_result(f"✅ 已踢出用户 {user_id}")
        except Exception as e:
            logger.error(f"踢出用户 {user_id} 失败: {e}")
            yield event.plain_result(f"❌ 踢出失败: {str(e)}")

    async def _handle_kick_with_inviter(self, event: AstrMessageEvent) -> None:
        """连带踢出：根据邀请记录踢出邀请人和被邀请的所有用户，并加入黑名单"""
        if not await self._check_feature_permission_async(event, "kick", "use"):
            yield event.plain_result("❌ 您没有权限执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        user_id = self._parse_user_id(event.message_str or "", ["/连带踢出", "连带踢出"])
        if not user_id:
            yield event.plain_result("❌ 用法: /连带踢出 @用户 [理由]")
            return
        
        # 解析理由
        message = (event.message_str or "").strip()
        parts = message.split()
        reason = ""
        user_found = False
        for i, part in enumerate(parts):
            if part.startswith("/连带踢出") or part == "连带踢出":
                continue
            if part.startswith("[CQ:at,qq=") or part.startswith("[At:") or part.startswith("@"):
                user_found = True
                continue
            if user_found:
                reason = " ".join(parts[i:])
                break
        if not reason:
            reason = "发送垃圾信息"
        
        try:
            from .qqadmin.audit import get_inviter_of_user, get_invited_users_by_inviter
            from .qqadmin.message_cache import get_user_cached_messages
            
            # 查找该用户是被谁邀请的
            inviter_id = get_inviter_of_user(group_id, user_id)
            
            # 需要踢出的所有用户
            all_users_to_kick = [user_id]
            
            if inviter_id:
                # 获取邀请人邀请的所有用户
                invited_users = get_invited_users_by_inviter(group_id, inviter_id)
                invited_ids = [str(inv["invitee_id"]) for inv in invited_users]
                all_users_to_kick.extend(invited_ids)
                # 也把邀请人加入踢出列表
                all_users_to_kick.append(inviter_id)
            else:
                # 没有邀请人，只踢目标用户
                pass
            
            # 去重
            all_users_to_kick = list(set(all_users_to_kick))
            
            if not all_users_to_kick:
                yield event.plain_result("❌ 没有找到需要踢出的用户")
                return
            
            # 撤回这些用户的发言
            total_recalled = 0
            for uid in all_users_to_kick:
                try:
                    messages = get_user_cached_messages(group_id, uid, limit=20)
                    for msg in messages:
                        msg_id = msg.get("message_id")
                        if msg_id:
                            try:
                                await event.bot.delete_msg(message_id=int(msg_id))
                                total_recalled += 1
                            except:
                                pass
                except Exception as e:
                    logger.error(f"撤回用户 {uid} 消息失败: {e}")
            
            # 踢出并加入黑名单
            kicked_count = 0
            failed_count = 0
            for uid in all_users_to_kick:
                try:
                    await event.bot.set_group_kick(
                        group_id=int(group_id),
                        user_id=int(uid),
                        reject_add_request=True,  # 拒绝再次申请入群
                    )
                    # 加入黑名单并设置拒绝理由
                    add_to_blacklist(group_id, uid, f"发送垃圾信息 | 连带踢出 | {reason}")
                    add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                                      "连带踢出", uid, f"邀请人: {inviter_id if inviter_id else '无'}", reason)
                    add_kick_record(group_id, uid, str(event.message_obj.sender.user_id), f"连带踢出: {reason}")
                    kicked_count += 1
                except Exception as e:
                    logger.error(f"踢出用户 {uid} 失败: {e}")
                    failed_count += 1
            
            result = f"✅ 连带踢出完成\n"
            if inviter_id:
                result += f"📋 邀请人: {inviter_id}\n"
            result += f"👥 踢出并拉黑: {kicked_count}人"
            if failed_count > 0:
                result += f" | 失败: {failed_count}人"
            result += f"\n🗑️ 撤回消息: {total_recalled}条"
            yield event.plain_result(result)
            
        except Exception as e:
            logger.error(f"连带踢出失败: {e}")
            yield event.plain_result(f"❌ 连带踢出失败: {str(e)}")

    async def _handle_list_kick_records(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        records = get_kick_records(group_id)
        if not records:
            yield event.plain_result("📋 当前没有踢出记录")
            return
        
        result = "📋 踢出记录:\n"
        for record in records:
            user_id = record['user_id']
            reason = record.get('reason', '无理由')
            kick_count = record.get('踢出次数', 1)
            in_blacklist = is_in_blacklist(group_id, user_id)
            blacklist_status = "🚫 黑名单" if in_blacklist else "✅ 正常"
            result += f"- {user_id}: {reason} | 次数: {kick_count} | {blacklist_status}\n"
        yield event.plain_result(result.strip())

    async def _handle_clear_kick_records(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        user_id = self._parse_user_id(event.message_str or "", ["/清除踢出记录", "清除踢出记录"])
        
        if user_id:
            if remove_kick_record(group_id, user_id):
                yield event.plain_result(f"✅ 已清除用户 {user_id} 的踢出记录")
            else:
                yield event.plain_result(f"❌ 未找到用户 {user_id} 的踢出记录")
        else:
            clear_kick_records(group_id)
            yield event.plain_result("✅ 已清除所有踢出记录")

    # ==================== 黑名单系统处理函数 ====================
    async def _handle_blacklist(self, event: AstrMessageEvent) -> None:
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._check_feature_permission_async(event, "blacklist", "use"):
            yield event.plain_result("❌ 您没有权限执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        parts = message.split()
        
        user_id = None
        duration_days = 0
        reason = ""
        has_at = False
        
        for i, part in enumerate(parts):
            if part.startswith("[CQ:at,qq="):
                start = part.find("qq=") + 3
                end = part.find("]", start)
                if end > start:
                    user_id = part[start:end]
                    has_at = True
                    continue
            
            # 处理 /拉黑@用户 格式（命令和@之间没有空格）
            if part.startswith("/拉黑@"):
                # 提取@后面的内容
                at_part = part[4:]  # 去掉"/拉黑"
                # 尝试从括号中提取用户ID
                start = at_part.find("(")
                end = at_part.find(")", start)
                if start > 0 and end > start:
                    user_id = at_part[start+1:end]
                else:
                    # 如果没有括号格式，把@后面的内容当作用户名或ID
                    user_id = at_part[1:]  # 去掉@
                has_at = True
                continue
            
            if part.startswith("/拉黑"):
                continue
            
            # 处理 @用户名(ID) 格式
            if part.startswith("@"):
                # 尝试从括号中提取用户ID
                start = part.find("(")
                end = part.find(")", start)
                if start > 0 and end > start:
                    user_id = part[start+1:end]
                has_at = True
                continue
            
            # 如果已经找到用户ID（通过@提及），剩余的参数都是理由
            if has_at and user_id:
                reason = " ".join(parts[i:])
                break
            
            # 如果还没有用户ID，尝试解析参数
            if user_id is None:
                # 如果是@开头但没有提取到ID，去掉@符号
                if part.startswith("@"):
                    user_id = part[1:]
                else:
                    user_id = part
            elif duration_days == 0:
                try:
                    days = int(part)
                    if days == 0:
                        # 0表示永久拉黑，继续处理理由
                        reason = " ".join(parts[i+1:])
                        break
                    elif days <= 365:  # 合理的年数范围内
                        duration_days = days * 86400
                    else:
                        # 天数太大，可能是无效输入，当作理由
                        reason = " ".join(parts[i:])
                        break
                except ValueError:
                    # 如果不是数字，说明这是理由的开始
                    reason = " ".join(parts[i:])
                    break
            else:
                reason = " ".join(parts[i:])
                break
        
        if not user_id:
            yield event.plain_result("❌ 用法: /拉黑 @用户 [天数] [理由]\n💡 天数0或不填=永久，其他数字=天数")
            return
        
        ban_type = "permanent" if duration_days == 0 else "temporary"
        
        if add_to_blacklist(group_id, user_id, reason, str(event.message_obj.sender.user_id), ban_type, duration_days):
            add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                              "拉黑", user_id, "", reason)
            
            # 撤回用户所有消息
            recalled_count = await self._recall_all_user_messages(event, user_id)
            
            # 尝试踢出用户
            kick_success = await self._kick_blacklisted_user(event, user_id, reason)
            
            # 自动拉黑该用户邀请的所有人
            invited_users = get_invited_users_by_inviter(group_id, user_id)
            auto_blacklisted_count = 0
            auto_blacklisted_users = []
            
            for invited in invited_users:
                invited_user_id = invited.get("invited_user_id", "")
                if invited_user_id and not is_in_blacklist(group_id, invited_user_id):
                    # 拉黑被邀请人，理由为"邀请人{user_id}+{reason}"
                    new_reason = f"邀请人{user_id}" + (f"：{reason}" if reason else "")
                    if add_to_blacklist(group_id, invited_user_id, new_reason, 
                                        str(event.message_obj.sender.user_id), ban_type, duration_days):
                        add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                                          "自动拉黑", invited_user_id, user_id, new_reason)
                        auto_blacklisted_count += 1
                        auto_blacklisted_users.append(invited_user_id)
                        
                        # 尝试踢出被邀请人
                        await self._kick_blacklisted_user(event, invited_user_id, new_reason)
            
            # 构建返回消息
            base_msg = ""
            recall_msg = f"🗑️ 已撤回 {recalled_count} 条消息" if recalled_count > 0 else ""
            
            if duration_days == 0:
                if kick_success:
                    base_msg = f"✅ 已永久拉黑并踢出用户 {user_id}"
                else:
                    base_msg = f"✅ 已永久拉黑用户 {user_id}，踢出失败（用户可能已不在群内）"
            else:
                if kick_success:
                    base_msg = f"✅ 已拉黑并踢出用户 {user_id}，时长 {duration_days//86400} 天"
                else:
                    base_msg = f"✅ 已拉黑用户 {user_id}，时长 {duration_days//86400} 天，踢出失败（用户可能已不在群内）"
            
            if recall_msg:
                base_msg = f"{base_msg}\n{recall_msg}"
            
            if auto_blacklisted_count > 0:
                base_msg += f"\n\n🔗 已自动拉黑 {auto_blacklisted_count} 名被邀请人："
                for i, invited_id in enumerate(auto_blacklisted_users[:5], 1):
                    base_msg += f"\n  {i}. {invited_id}"
                if auto_blacklisted_count > 5:
                    base_msg += f"\n  ... 还有 {auto_blacklisted_count - 5} 人"
            
            yield event.plain_result(base_msg)
        else:
            yield event.plain_result("❌ 拉黑失败")

    async def _kick_blacklisted_user(self, event, user_id: str, reason: str = "") -> bool:
        """踢出黑名单用户"""
        try:
            group_id = str(event.message_obj.group_id)
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if platform:
                await platform.get_client().call_action(
                    action="set_group_kick",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    reject_add_request=False
                )
                logger.info(f"踢出黑名单用户成功: {user_id}，原因: {reason}")
                return True
            else:
                logger.error("无法获取平台实例")
                return False
        except Exception as e:
            logger.error(f"踢出黑名单用户失败: {e}")
            return False

    async def _recall_all_user_messages(self, event, user_id: str) -> int:
        """撤回用户所有缓存的消息"""
        recalled_count = 0
        try:
            group_id = str(event.message_obj.group_id)
            from .qqadmin.message_cache import get_user_cached_messages
            
            # 获取用户所有缓存消息
            messages = get_user_cached_messages(group_id, user_id)
            if not messages:
                return 0
            
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if not platform:
                logger.error("无法获取平台实例")
                return 0
            
            client = platform.get_client()
            
            # 按时间从早到晚撤回（避免消息顺序问题）
            messages.sort(key=lambda x: x.get("time", 0))
            
            for msg in messages:
                message_id = msg.get("message_id")
                if message_id:
                    try:
                        await client.call_action(
                            action="delete_msg",
                            message_id=message_id
                        )
                        recalled_count += 1
                    except Exception as e:
                        logger.debug(f"撤回消息失败 {message_id}: {e}")
            
            logger.info(f"撤回黑名单用户 {user_id} 的 {recalled_count} 条消息")
            return recalled_count
        except Exception as e:
            logger.error(f"撤回用户消息失败: {e}")
            return 0

    async def _handle_unblacklist(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        parts = message.split()
        
        user_id = None
        has_at = False  # 检测是否有艾特格式
        
        for part in parts:
            if part.startswith("[CQ:at,qq="):
                has_at = True
                start = part.find("qq=") + 3
                end = part.find("]", start)
                if end > start:
                    user_id = part[start:end]
                    break
            elif part.startswith("/删黑"):
                continue
            elif part.startswith("@"):
                has_at = True
                # 处理 @用户名(ID) 格式
                start = part.find("(")
                end = part.find(")", start)
                if start > 0 and end > start:
                    user_id = part[start+1:end]
                    break
            elif part.isdigit() and not user_id:
                # 处理 /删黑 123456789 格式
                user_id = part
        
        if not user_id:
            if has_at:
                # 有艾特但无法解析（用户不在群）
                yield event.plain_result("❌ 无法识别艾特的用户（可能用户不在群内）\n请使用: /删黑 用户ID\n先使用 /查看黑名单 查看用户ID")
            else:
                yield event.plain_result("❌ 用法: /删黑 用户ID\n提示: 先使用 /查看黑名单 查看黑名单列表")
            return
        
        if remove_from_blacklist(group_id, user_id):
            add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                              "解除拉黑", user_id, "", "")
            yield event.plain_result(f"✅ 已解除用户 {user_id} 的拉黑")
        else:
            yield event.plain_result("❌ 解除拉黑失败，用户可能不在黑名单中")

    # ==================== 管理员审批处理函数 ====================
    async def _handle_approve_by_code(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        
        group_id = str(event.message_obj.group_id)
        is_private = False
        
        # 如果是私聊，允许审批但需要遍历查找
        if not group_id or group_id == "None":
            is_private = True
        
        message = (event.message_str or "").strip()
        parts = message.split()
        
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /同意 <审批码>")
            return
        
        approval_code = parts[1]
        
        from .qqadmin.audit import get_pending_request_by_code, approve_join_request
        
        pending = None
        
        if is_private:
            # 私聊模式：遍历所有群查找审批码
            from .qqadmin.audit import load_audit_pending
            all_pending = load_audit_pending()
            for gid, requests in all_pending.items():
                for req in requests:
                    if req.get("approval_code") == approval_code and req.get("status") == "pending":
                        pending = req
                        group_id = gid
                        break
                if pending:
                    break
        else:
            # 群聊模式：只查找当前群
            pending = get_pending_request_by_code(group_id, approval_code)
        
        if not pending:
            yield event.plain_result(f"❌ 未找到审批码为 {approval_code} 的申请")
            return
        
        user_id = pending.get("user_id", "")
        user_name = pending.get("user_name", "")
        flag = pending.get("flag", "")
        
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if platform:
                await platform.get_client().call_action(
                    action="set_group_add_request",
                    flag=flag,
                    sub_type="add",
                    approve=True
                )
                
                approve_join_request(group_id, user_id, str(event.message_obj.sender.user_id), "管理员批准")
                add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                                  "同意入群", user_id, "", "审批码:" + approval_code)
                
                yield event.plain_result(f"✅ 已同意用户 {user_name}({user_id}) 入群")
                logger.info(f"管理员 {event.message_obj.sender.user_id} 同意用户 {user_id} 入群，审批码: {approval_code}")
                
                # 管理员同意后发送入群欢迎消息
                await self._send_welcome_message(event, group_id, user_id, user_name)
            else:
                yield event.plain_result("❌ 无法获取平台实例")
        except Exception as e:
            logger.error(f"同意入群失败: {e}")
            yield event.plain_result("❌ 同意入群失败")
    
    async def _handle_reject_by_code(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        
        group_id = str(event.message_obj.group_id)
        is_private = False
        
        # 如果是私聊，允许审批但需要遍历查找
        if not group_id or group_id == "None":
            is_private = True
        
        message = (event.message_str or "").strip()
        parts = message.split()
        
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /拒绝 <审批码> [理由]")
            return
        
        approval_code = parts[1]
        reason = " ".join(parts[2:]) if len(parts) > 2 else "管理员拒绝"
        
        from .qqadmin.audit import (get_pending_request_by_code, get_request_by_code_any_status,
                                     get_pending_request_by_code_all_groups, 
                                     get_request_by_code_any_status_all_groups, reject_join_request)
        
        pending = None
        
        if is_private:
            # 私聊模式：遍历所有群查找审批码
            gid, pending = get_pending_request_by_code_all_groups(approval_code)
            if gid:
                group_id = gid
        else:
            # 群聊模式：只查找当前群
            pending = get_pending_request_by_code(group_id, approval_code)
        
        if not pending:
            # 检查申请是否已被处理过
            if is_private:
                gid, any_status = get_request_by_code_any_status_all_groups(approval_code)
            else:
                gid = group_id
                any_status = get_request_by_code_any_status(group_id, approval_code)
            
            if any_status:
                status = any_status.get("status", "")
                user_name = any_status.get("user_name", "")
                user_id = any_status.get("user_id", "")
                if status == "rejected":
                    yield event.plain_result(f"⚠️ 用户 {user_name}({user_id}) 的申请已被拒绝过了")
                elif status == "approved":
                    yield event.plain_result(f"⚠️ 用户 {user_name}({user_id}) 的申请已被批准过了")
                else:
                    yield event.plain_result(f"⚠️ 用户 {user_name}({user_id}) 的申请已处理过了，状态: {status}")
            else:
                yield event.plain_result(f"❌ 未找到审批码为 {approval_code} 的申请")
            return
        
        user_id = pending.get("user_id", "")
        user_name = pending.get("user_name", "")
        flag = pending.get("flag", "")
        
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if platform:
                # 统一使用 set_group_add_request 拒绝入群请求
                await platform.get_client().call_action(
                    action="set_group_add_request",
                    flag=flag,
                    sub_type="add",
                    approve=False,
                    reason=reason
                )
                yield event.plain_result(f"❌ 已拒绝用户 {user_name}({user_id}) 的入群申请\n理由: {reason}")
                logger.info(f"管理员 {event.message_obj.sender.user_id} 拒绝用户 {user_id} 入群，审批码: {approval_code}，理由: {reason}")
                
                reject_join_request(group_id, user_id, str(event.message_obj.sender.user_id), reason)
                add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                                  "拒绝入群", user_id, "", f"审批码:{approval_code},理由:{reason}")
            else:
                yield event.plain_result("❌ 无法获取平台实例")
        except Exception as e:
            logger.error(f"拒绝入群失败: {e}")
            yield event.plain_result("❌ 拒绝入群失败")

    async def _handle_list_blacklist(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        blacklist = list_blacklist(group_id)
        if not blacklist:
            yield event.plain_result("📋 当前黑名单为空")
            return
        
        result = "📋 黑名单列表:\n"
        for user in blacklist:
            remaining = user.get("remaining_time", 0)
            if remaining > 0:
                days = remaining // 86400
                time_str = f"剩余 {days}天"
            else:
                time_str = "永久"
            result += f"- {user['user_id']}: {time_str} | 理由: {user.get('reason', '')}\n"
        yield event.plain_result(result.strip())

    async def _handle_clear_blacklist(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        clear_blacklist(group_id)
        yield event.plain_result("✅ 已清空黑名单")

    async def _handle_extend_blacklist(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        parts = message.split()
        
        user_id = None
        days = 0
        
        for i, part in enumerate(parts):
            if part.startswith("[CQ:at,qq="):
                start = part.find("qq=") + 3
                end = part.find("]", start)
                if end > start:
                    user_id = part[start:end]
                    continue
            
            if part.startswith("/延长拉黑时间"):
                continue
            
            if user_id is None:
                user_id = part
            elif days == 0:
                try:
                    days = int(part) * 86400
                except ValueError:
                    yield event.plain_result("❌ 用法: /延长拉黑时间 @用户 <天数>")
                    return
        
        if not user_id or days == 0:
            yield event.plain_result("❌ 用法: /延长拉黑时间 @用户 <天数>")
            return
        
        from .qqadmin.blacklist import update_blacklist_duration
        if update_blacklist_duration(group_id, user_id, days):
            add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                              "延长拉黑", user_id, "", f"延长 {days//86400} 天")
            yield event.plain_result(f"✅ 已延长用户 {user_id} 的拉黑时间 {days//86400} 天")
        else:
            yield event.plain_result("❌ 延长拉黑时间失败")

    # ==================== 入群审核处理函数 ====================
    async def _handle_enable_audit(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        set_audit_settings(group_id, "enabled", True)
        set_feature_enabled(group_id, "audit_enabled", True, self.config)
        yield event.plain_result("✅ 已开启入群审核\n💡 可使用 /克隆全局 一次性启用所有功能")

    async def _handle_disable_audit(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        set_audit_settings(group_id, "enabled", False)
        yield event.plain_result("✅ 已关闭入群审核")

    async def _handle_approve_audit(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /审核通过 <QQ号>")
            return
        
        user_id = parts[1]
        if approve_join_request(group_id, user_id, str(event.message_obj.sender.user_id)):
            yield event.plain_result(f"✅ 已通过用户 {user_id} 的入群申请")
        else:
            yield event.plain_result("❌ 审核通过失败")

    async def _handle_approve_audit_direct(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /审批 <4位审批码>")
            return

        code = parts[1]
        if len(code) != 4 or not code.isdigit():
            yield event.plain_result("❌ 审批码必须是4位数字")
            return

        from .qqadmin.audit import get_pending_by_code
        pending_info = get_pending_by_code(group_id, code)
        if not pending_info:
            yield event.plain_result("❌ 无效的审批码或该申请已处理")
            return

        user_id = pending_info.get("user_id")
        if approve_join_request(group_id, user_id, str(event.message_obj.sender.user_id), "审批码审批通过"):
            add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                              "审核通过", user_id, "", "审批码审批")
            user_name = pending_info.get('user_name', '')
            display_name = user_name if user_name and user_name != user_id else ''
            yield event.plain_result(f"✅ 已批准用户 {display_name}({user_id}) 入群")
        else:
            yield event.plain_result("❌ 审批失败")

    async def _handle_reject_audit(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /拒绝入群 <QQ号> [理由]")
            return
        
        user_id = parts[1]
        reason = " ".join(parts[2:]) if len(parts) > 2 else ""
        
        if reject_join_request(group_id, user_id, str(event.message_obj.sender.user_id), reason):
            yield event.plain_result(f"✅ 已拒绝用户 {user_id} 的入群申请")
        else:
            yield event.plain_result("❌ 拒绝入群失败")

    async def _handle_show_verification_hint(self, event: AstrMessageEvent) -> None:
        """显示验证提示，帮助用户重新获取验证信息"""
        sender = event.message_obj.sender
        user_id = str(sender.user_id if hasattr(sender, 'user_id') else "")
        group_id = str(event.message_obj.group_id)
        
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        from .qqadmin.audit import get_pending_verification, get_group_data, get_verification_timeout, get_max_verify_attempts
        from .qqadmin.blacklist import is_in_blacklist
        
        # 检查是否在黑名单
        if is_in_blacklist(group_id, user_id):
            yield event.plain_result("❌ 您在黑名单中，无法进行验证。")
            return
        
        # 获取用户的待验证信息
        pending = get_pending_verification(group_id, user_id)
        challenges = get_group_data(group_id, "audit_challenges", {})
        
        if not pending and not challenges:
            # 检查用户是否在 audit_pending 中
            audit_pending = get_group_data(group_id, "audit_pending", [])
            for req in audit_pending:
                if str(req.get('user_id', '')) == str(user_id) and req.get('status') == 'pending':
                    pending = req
                    break
        
        if not pending:
            yield event.plain_result("❌ 您没有待验证的信息。")
            return
        
        approval_mode = pending.get("approval_mode", "")
        max_attempts = get_max_verify_attempts(group_id)
        timeout = get_verification_timeout(group_id)
        attempts = pending.get("attempts", 0)
        remaining = max_attempts - attempts
        
        if remaining <= 0:
            yield event.plain_result("❌ 您的验证次数已用完。")
            return
        
        # 根据验证模式显示不同的提示
        if approval_mode == "math":
            challenge_id = pending.get("challenge_id", "")
            challenge = challenges.get(challenge_id, {})
            question = challenge.get("question", "")
            if question:
                yield event.plain_result(
                    f"🔢 请完成算数验证:\n{question}\n"
                    f"⏱️ 验证超时时间: {timeout}秒\n"
                    f"📊 剩余次数: {remaining}次"
                )
            else:
                yield event.plain_result("❌ 验证信息已过期，请联系管理员。")
        elif approval_mode == "id":
            verify_id = pending.get("verification_question", "")
            if verify_id:
                yield event.plain_result(
                    f"🔢 请输入验证ID:\n{verify_id}\n"
                    f"⏱️ 验证超时时间: {timeout}秒\n"
                    f"📊 剩余次数: {remaining}次"
                )
            else:
                yield event.plain_result("❌ 验证信息已过期，请联系管理员。")
        elif approval_mode == "code":
            approval_code = pending.get("approval_code", "")
            yield event.plain_result(
                f"🔢 请等待管理员审批\n"
                f"🔑 审批码: {approval_code}\n"
                f"⏱️ 审批超时时间: {timeout}秒"
            )
        else:
            yield event.plain_result("❌ 未知验证模式，请联系管理员。")

    async def _handle_list_pending_audit(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        requests = get_audit_requests(group_id)
        if not requests:
            yield event.plain_result("📋 当前没有待审核的入群申请")
            return
        
        result = "📋 待审核列表:\n\n"
        for i, req in enumerate(requests, 1):
            user_id = req.get('user_id', '')
            user_name = req.get('user_name', '') or '未知'
            approval_mode = req.get('approval_mode', 'direct')
            mode_name = {'direct': '直接', 'code': '管理员审批', 'math': '算数验证', 'id': 'ID验证'}.get(approval_mode, approval_mode)
            verification_question = req.get('verification_question', '')
            approval_code = req.get('approval_code', '')
            
            result += f"【{i}】{user_name}({user_id})\n"
            result += f"    模式: {mode_name}\n"
            if verification_question:
                result += f"    验证: {verification_question}\n"
            if approval_code:
                result += f"    审批码: {approval_code}\n"
            result += f"    操作: /同意 {approval_code} 或 /拒绝 {approval_code}\n\n"
        
        result = result.strip()
        yield event.plain_result(result)

    async def _handle_add_whitelist(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        parts = message.split()
        
        user_id = None
        for part in parts:
            if part.startswith("[CQ:at,qq="):
                start = part.find("qq=") + 3
                end = part.find("]", start)
                if end > start:
                    user_id = part[start:end]
                    break
            elif part.startswith("/添加白名单"):
                continue
            elif not user_id:
                user_id = part
        
        if not user_id:
            yield event.plain_result("❌ 用法: /添加白名单 @用户")
            return
        
        add_to_whitelist(group_id, user_id)
        add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                          "添加白名单", user_id, "", "")
        yield event.plain_result(f"✅ 已将用户 {user_id} 添加到白名单")

    async def _handle_remove_whitelist(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()
        
        user_id = None
        for part in parts:
            if part.startswith("[CQ:at,qq="):
                start = part.find("qq=") + 3
                end = part.find("]", start)
                if end > start:
                    user_id = part[start:end]
                    break
            elif part.startswith("/移除白名单"):
                continue
            elif not user_id:
                user_id = part
        
        if not user_id:
            yield event.plain_result("❌ 用法: /移除白名单 @用户")
            return
        
        remove_from_whitelist(group_id, user_id)
        add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                          "移除白名单", user_id, "", "")
        yield event.plain_result(f"✅ 已将用户 {user_id} 从白名单移除")

    async def _handle_enable_verification(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        set_verification_settings(group_id, "verification_enabled", True)
        yield event.plain_result("✅ 已开启入群验证功能\n📌 可用验证类型: math(算数验证)、id(ID验证)")

    async def _handle_disable_verification(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        set_verification_settings(group_id, "verification_enabled", False)
        yield event.plain_result("✅ 已关闭入群验证功能")

    async def _handle_set_verification_type(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /设置验证类型 <math|id>")
            return

        vtype = parts[1].lower()
        if vtype not in ["math", "id"]:
            yield event.plain_result("❌ 验证类型只能是: math(算数验证) 或 id(ID验证)")
            return

        set_verification_settings(group_id, "verification_type", vtype)
        type_name = "算数验证" if vtype == "math" else "ID验证"
        yield event.plain_result(f"✅ 已设置验证类型为: {type_name}")

    async def _handle_set_approval_mode(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()
        
        # 没有参数，显示当前设置
        if len(parts) < 2:
            async for result in self._handle_view_verification_settings(event):
                yield result
            return

        # 有参数，设置审批模式
        mode = parts[1].lower()
        if mode not in ["direct", "code", "math", "id"]:
            yield event.plain_result("❌ 审批模式只能是: direct, code, math, id")
            return

        from .qqadmin.audit import set_audit_approval_mode
        set_audit_approval_mode(group_id, mode)
        yield event.plain_result(f"✅ 已设置审批模式为: {mode} (direct/code/math/id)")

    async def _handle_set_max_verify_attempts(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        # 去掉命令前缀
        message = message.replace("审核次数 ", "").replace("设置验证最大尝试 ", "").strip()
        parts = message.split()
        if len(parts) < 1:
            yield event.plain_result("❌ 用法: /审核次数 <次数>")
            return

        try:
            attempts = int(parts[0])
            if attempts < 1 or attempts > 10:
                yield event.plain_result("❌ 尝试次数必须在1-10之间")
                return
        except ValueError:
            yield event.plain_result("❌ 尝试次数必须是数字")
            return

        from .qqadmin.audit import set_max_verify_attempts
        set_max_verify_attempts(group_id, attempts)
        yield event.plain_result(f"✅ 已设置验证最大尝试次数为: {attempts}次")

    async def _handle_set_verification_timeout(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        # 去掉命令前缀
        message = message.replace("审核超时 ", "").replace("设置验证超时 ", "").strip()
        parts = message.split()
        if len(parts) < 1:
            yield event.plain_result("❌ 用法: /审核超时 <秒数>")
            return

        try:
            timeout = int(parts[0])
            if timeout < 60:
                yield event.plain_result("❌ 超时时间不能少于60秒")
                return
        except ValueError:
            yield event.plain_result("❌ 超时时间必须是数字")
            return

        set_verification_settings(group_id, "verification_timeout", timeout)
        yield event.plain_result(f"✅ 已设置验证超时时间为: {timeout}秒")

    async def _handle_view_verification_settings(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        from .qqadmin.audit import get_audit_approval_mode, get_verification_timeout, get_max_verify_attempts

        approval_mode = get_audit_approval_mode(group_id)
        timeout = get_verification_timeout(group_id)
        max_attempts = get_max_verify_attempts(group_id)

        mode_names = {
            "direct": "直接审批",
            "code": "审批码审批",
            "math": "算数验证",
            "id": "ID验证"
        }

        result = f"""📋 入群审核设置:

审批模式: {approval_mode} (直接审批/code/审批码/math/算数验证/id/ID验证)
超时时间: {timeout}秒
最大尝试: {max_attempts}次"""
        yield event.plain_result(result)

    async def _handle_answer_verification(self, event: AstrMessageEvent) -> None:
        sender = event.message_obj.sender
        user_id = str(sender.user_id if hasattr(sender, 'user_id') else "")
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            return

        from .qqadmin.audit import (get_pending_verification, verify_answer, 
                                    is_verification_expired, approve_join_request, reject_join_request,
                                    get_max_verify_attempts, increment_pending_attempts, remove_pending_request,
                                    get_group_data, get_verification_timeout)

        pending = get_pending_verification(group_id, user_id)
        if not pending:
            return

        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /回答验证 <答案>")
            return

        answer = parts[1]
        max_attempts = get_max_verify_attempts(group_id)
        
        # 从 pending 中获取实际的 approval_mode（与入群时一致）
        approval_mode = pending.get("approval_mode", "direct")
        
        logger.info(f"验证回答: user_id={user_id}, answer={answer}, approval_mode={approval_mode}")
        
        # 处理不同验证模式
        if approval_mode == "math":
            # 算数验证：使用 challenge_id 验证
            if not pending.get("challenge_id"):
                yield event.plain_result("❌ 验证信息错误")
                return
            
            if is_verification_expired(group_id, user_id):
                yield event.plain_result("❌ 验证已超时，请重新申请入群")
                return
            
            challenge_id = pending["challenge_id"]
            is_correct, should_reject = verify_answer(group_id, challenge_id, answer)
            challenges = get_group_data(group_id, "audit_challenges", {})
            correct_code = challenges.get(challenge_id, {}).get("answer", "")
        elif approval_mode == "id":
            # ID验证：使用 verify_id 验证（存储在 verification_question 字段）
            correct_code = pending.get("verification_question", "")
            is_correct = (answer == correct_code)
            logger.info(f"ID验证比对: answer='{answer}', verify_id='{correct_code}', is_correct={is_correct}")
        elif approval_mode in ["code", "direct"]:
            # code模式或直接模式：使用approval_code验证
            correct_code = pending.get("approval_code", "")
            is_correct = (answer == correct_code)
            logger.info(f"验证码比对: answer='{answer}', approval_code='{correct_code}', is_correct={is_correct}")
        else:
            # 其他模式，默认使用 approval_code
            correct_code = pending.get("approval_code", "")
            is_correct = (answer == correct_code)
            logger.info(f"验证码比对(默认): answer='{answer}', approval_code='{correct_code}', is_correct={is_correct}")
        
        if is_correct:
            approve_join_request(group_id, user_id, "system", "验证通过")
            from .qqadmin.welcome import is_welcome_enabled, format_welcome_message, log_welcome
            from astrbot.core.message.components import At, Plain
            from astrbot.core.message.message_event_result import MessageChain
            
            user_name = pending.get("user_name", "")
            # 优先使用昵称，如果没有则使用用户ID
            display_name = user_name if user_name else user_id
            
            # 检查是否启用欢迎功能（全局开关 + 群组设置）
            welcome_enabled = self.qqadmin_enable_welcome or is_welcome_enabled(group_id)
            
            # 辅助函数：构建包含艾特的消息链
            def build_at_welcome_chain(message_text: str):
                return [At(qq=int(user_id)), Plain(" " + message_text)]
            
            if welcome_enabled:
                welcome_msg = format_welcome_message(group_id, user_id, display_name)
                if welcome_msg:
                    try:
                        # 使用真正的艾特组件发送欢迎消息
                        chain = build_at_welcome_chain(welcome_msg)
                        await event.send(event.chain_result(chain))
                        log_welcome(group_id, user_id, display_name, "audit")
                    except Exception as e:
                        logger.error(f"发送欢迎消息失败: {e}")
                        chain = build_at_welcome_chain("✅ 验证通过，欢迎加入群聊！")
                        yield event.chain_result(chain)
                else:
                    # 使用全局默认欢迎消息
                    default_msg = self.qqadmin_default_welcome_message.format(
                        user_id=user_id,
                        user_name=display_name,
                        group_id=group_id,
                        time=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    )
                    try:
                        # 使用真正的艾特组件发送默认欢迎消息
                        chain = build_at_welcome_chain(default_msg)
                        await event.send(event.chain_result(chain))
                        log_welcome(group_id, user_id, display_name, "audit")
                    except Exception as e:
                        logger.error(f"发送欢迎消息失败: {e}")
                        chain = build_at_welcome_chain("✅ 验证通过，欢迎加入群聊！")
                        yield event.chain_result(chain)
            else:
                chain = build_at_welcome_chain("✅ 验证通过，欢迎加入群聊！")
                yield event.chain_result(chain)
        else:
            # 增加尝试次数
            current_attempts = increment_pending_attempts(group_id, user_id)
            remaining_attempts = max_attempts - current_attempts
            from astrbot.core.message.components import At, Plain
            
            if current_attempts >= max_attempts:
                # 再次检查用户是否还在 pending 队列中（可能已验证通过）
                current_pending = get_pending_verification(group_id, user_id)
                if not current_pending:
                    logger.info(f"用户 {user_id} 已不在 pending 队列中，跳过踢人操作")
                    event.stop_event()
                    return
                
                # 超过最大尝试次数，踢出群聊
                try:
                    platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                    if platform:
                        # 先发送踢出原因通知（包含艾特）
                        kick_notice_chain = [At(qq=int(user_id)), Plain(
                            f" ⚠️ 验证失败次数过多\n"
                            f"📊 尝试次数: {current_attempts}/{max_attempts}\n"
                            f"🚪 已被移出群聊"
                        )]
                        # 将消息链转换为 OneBot 格式
                        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                        onebot_messages = await AiocqhttpMessageEvent._parse_onebot_json(kick_notice_chain)
                        await platform.get_client().call_action(
                            action="send_group_msg",
                            group_id=int(group_id),
                            message=onebot_messages
                        )
                        # 然后踢出用户
                        await platform.get_client().call_action(
                            action="set_group_kick",
                            group_id=int(group_id),
                            user_id=int(user_id),
                            reject_add_request=False
                        )
                        logger.info(f"验证失败踢出用户: user_id={user_id}, group_id={group_id}, 尝试次数={current_attempts}")
                    else:
                        logger.error("无法获取平台实例")
                except Exception as e:
                    logger.error(f"踢出用户失败: {e}")
                
                remove_pending_request(group_id, user_id)
                
                # 构建包含艾特的失败消息链
                def build_fail_chain(message_text: str):
                    return [At(qq=int(user_id)), Plain(" " + message_text)]
                
                # 如果启用了验证失败自动加入黑名单，则将用户加入黑名单
                if self.qqadmin_auto_blacklist_on_verify_fail and self.qqadmin_enable_blacklist:
                    from .qqadmin.blacklist import add_to_blacklist
                    add_to_blacklist(group_id, user_id, reason="验证失败", operator_id="system", ban_type="permanent")
                    logger.info(f"验证失败自动加入黑名单: user_id={user_id}, group_id={group_id}")
                    fail_msg = (
                        f"❌ 验证失败！\n"
                        f"📊 已尝试 {current_attempts}/{max_attempts} 次\n"
                        f"⚠️ 已被移出群聊并加入黑名单"
                    )
                    yield event.chain_result(build_fail_chain(fail_msg))
                else:
                    fail_msg = (
                        f"❌ 验证失败！\n"
                        f"📊 已尝试 {current_attempts}/{max_attempts} 次\n"
                        f"⚠️ 已被移出群聊"
                    )
                    yield event.chain_result(build_fail_chain(fail_msg))
            else:
                timeout = get_verification_timeout(group_id)
                error_msg = (
                    f"❌ 答案错误！\n"
                    f"📊 第 {current_attempts}/{max_attempts} 次尝试\n"
                    f"⏱️ 验证超时时间: {timeout}秒\n"
                    f"💡 正确答案: {correct_code}\n"
                    f"⏱️ 还剩 {remaining_attempts} 次机会"
                )
                chain = [At(qq=int(user_id)), Plain(" " + error_msg)]
                yield event.chain_result(chain)

    # ==================== 邀请统计处理函数 ====================
    async def _handle_view_invite_ranking(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        top_inviters = get_top_inviter(group_id, 10)
        if not top_inviters:
            yield event.plain_result("📊 当前还没有邀请排行数据\n\n💡 邀请好友入群后会自动统计邀请人数")
            return

        result = "🏆 邀请排行榜:\n\n"
        for i, inviter in enumerate(top_inviters, 1):
            result += f"{i}. QQ: {inviter['user_id']} | 邀请人数: {inviter['count']}\n"

        yield event.plain_result(result)

    async def _handle_view_invite_records(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) >= 2:
            inviter_id = parts[1]
            invites = get_invited_users_by_inviter(group_id, inviter_id)
        else:
            invites = []

        if not invites:
            yield event.plain_result("📋 当前没有相关邀请记录\n用法: /查看邀请记录 [邀请者QQ]")
            return

        result = f"📋 邀请记录 (邀请者: {inviter_id}):\n\n"
        for invite in invites:
            status = invite.get("status", "pending")
            status_text = {
                "pending": "待审核",
                "approved": "已通过",
                "rejected": "已拒绝",
                "joined": "已入群",
                "left": "已退群"
            }.get(status, status)
            result += f"• {invite['invitee_name']} ({invite['invitee_id']}) - {status_text}\n"

        yield event.plain_result(result)

    async def _handle_check_abnormal_invites(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()
        threshold = int(parts[1]) if len(parts) >= 2 else 5

        abnormal = get_all_excessive_inviter_stats(group_id, threshold)
        if not abnormal:
            yield event.plain_result(f"✅ 未发现异常邀请行为（阈值: {threshold}人/小时）")
            return

        result = f"⚠️ 检测到异常邀请行为（阈值: {threshold}人/小时）:\n\n"
        for item in abnormal:
            result += f"🚨 用户 {item['user_id']} 在1小时内邀请了 {item['count']} 人\n"
            result += f"   被邀请者: {', '.join([i['invitee_id'] for i in item['invitees'][:5]])}"
            if len(item['invitees']) > 5:
                result += f"...(共{len(item['invitees'])}人)"
            result += "\n\n"
        
        yield event.plain_result(result)

    async def _handle_view_my_invites(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        inviter_id = str(event.message_obj.sender.user_id)
        invites = get_invited_users_by_inviter(group_id, inviter_id)
        
        if not invites:
            yield event.plain_result("📊 你还没有邀请记录")
            return
        
        invite_count = get_inviter_count(group_id, inviter_id)
        joined_count = sum(1 for i in invites if i.get("status") == "joined")
        
        result = f"📊 我的邀请统计:\n\n"
        result += f"总邀请人数: {invite_count}\n"
        result += f"已入群人数: {joined_count}\n\n"
        result += "邀请列表:\n"
        
        for invite in invites:
            status = invite.get("status", "pending")
            status_text = {
                "pending": "待审核",
                "approved": "已通过",
                "rejected": "已拒绝",
                "joined": "已入群",
                "left": "已退群"
            }.get(status, status)
            result += f"• {invite['invitee_name']} ({invite['invitee_id']}) - {status_text}\n"
        
        yield event.plain_result(result)

    async def _handle_view_invite_stats(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        stats = load_invite_stats(group_id)
        if group_id not in stats:
            yield event.plain_result("📊 当前还没有邀请统计数据")
            return
        
        total_invites = len(stats[group_id]["invites"])
        total_inviters = len(stats[group_id]["inviter_counts"])
        joined_count = sum(1 for i in stats[group_id]["invites"] if i.get("status") == "joined")
        pending_count = sum(1 for i in stats[group_id]["invites"] if i.get("status") == "pending")
        
        result = f"📈 群邀请统计报告:\n\n"
        result += f"总邀请次数: {total_invites}\n"
        result += f"邀请人数: {total_inviters}\n"
        result += f"已入群人数: {joined_count}\n"
        result += f"待审核人数: {pending_count}\n\n"
        
        if total_inviters > 0:
            result += "🏆 邀请排行榜:\n"
            top_inviters = get_top_inviter(group_id, 5)
            for i, inviter in enumerate(top_inviters, 1):
                result += f"{i}. QQ: {inviter['user_id']} | 邀请人数: {inviter['count']}\n"
        
        yield event.plain_result(result)

    # ==================== 操作日志处理函数 ====================
    async def _handle_view_operation_logs(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        logs = get_operation_logs(group_id, 20)
        if not logs:
            yield event.plain_result("📝 当前没有操作日志")
            return

        result = "📝 最近操作日志:\n\n"
        for log in logs:
            time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(log["timestamp"]))
            result += f"[{time_str}] {log['operator_name']}({log['operator_id']}) "
            result += f"{log['action']}"
            if log['target_id']:
                result += f" → {log['target_name']}({log['target_id']})"
            if log['reason']:
                result += f" | 原因: {log['reason']}"
            result += "\n"

        yield event.plain_result(result)

    async def _handle_view_operation_stats(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        stats = get_operation_stats(group_id, 7)
        result = f"📊 近{stats['period_days']}天操作统计:\n\n"
        result += f"总操作次数: {stats['total']}\n\n"
        result += "操作类型统计:\n"
        for action, count in stats['actions'].items():
            result += f"  • {action}: {count}次\n"
        result += "\n操作者统计:\n"
        for operator, count in stats['operators'].items():
            result += f"  • {operator}: {count}次\n"

        yield event.plain_result(result)

    async def _handle_clear_operation_logs(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        removed = clear_operation_logs(group_id)
        yield event.plain_result(f"✅ 已清除 {removed} 条操作日志")

    async def _handle_clear_invite_stats(self, event: AstrMessageEvent) -> None:
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有机器人主人才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        removed = clear_invite_stats(group_id)
        yield event.plain_result(f"✅ 已清除 {removed} 条邀请记录")

    async def _handle_text_to_image(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能修改文转图设置。")
            return

        if not is_text_to_image_available():
            yield event.plain_result("❌ 文转图功能不可用，请安装 Pillow 库:\npip install pillow")
            return

        from .qqadmin.group_config import get_group_data, update_group_data
        
        def _enable_text_to_image(features):
            if features is None:
                features = {}
            text_to_image_config = features.get("text_to_image", {})
            features["text_to_image"] = {
                **text_to_image_config,
                "enable_text_to_image": True
            }
            return features
        
        update_group_data(group_id, "features", _enable_text_to_image)
        
        yield event.plain_result("✅ 文转图功能已开启\n机器人所有文字回复将转为图片发送")

    async def _handle_image_to_text(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能修改文转图设置。")
            return

        from .qqadmin.group_config import get_group_data, update_group_data
        
        def _disable_text_to_image(features):
            if features is None:
                features = {}
            text_to_image_config = features.get("text_to_image", {})
            features["text_to_image"] = {
                **text_to_image_config,
                "enable_text_to_image": False
            }
            return features
        
        update_group_data(group_id, "features", _disable_text_to_image)
        
        yield event.plain_result("✅ 文转图功能已关闭\n机器人将使用文字回复")

    async def _handle_power_on(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        
        if self.enable_auth:
            if not self._is_authorized(event):
                yield event.plain_result("❌ 当前群组未授权，请先激活卡密。")
                return
        
        from .qqadmin.group_config import update_group_data
        
        def _power_on(features):
            if features is None:
                features = {}
            features["power_status"] = "on"
            return features
        
        update_group_data(group_id, "features", _power_on)
        
        yield event.plain_result("🔛 已开机\n群管功能已启用，可以正常使用所有群管命令")

    async def _handle_power_off(self, event: AstrMessageEvent) -> None:
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        
        from .qqadmin.group_config import update_group_data
        
        def _power_off(features):
            if features is None:
                features = {}
            features["power_status"] = "off"
            return features
        
        update_group_data(group_id, "features", _power_off)
        
        yield event.plain_result("🔛 已关机\n群管功能已禁用，所有群管命令将不再响应")

    async def _handle_enable_recall(self, event: AstrMessageEvent) -> None:
        is_admin = await self._is_admin_or_owner_async(event)
        logger.info(f"_handle_enable_recall: is_admin={is_admin}")
        if not is_admin:
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        enable_recall(group_id, True)
        set_feature_enabled(group_id, "recall_enabled", True, self.config)
        yield event.plain_result("✅ 已开启消息撤回功能\n💡 可使用 /克隆全局 一次性启用所有功能")

    async def _handle_disable_recall(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        enable_recall(group_id, False)
        yield event.plain_result("✅ 已关闭消息撤回功能")

    async def _handle_add_recall_keyword(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        message = message.lstrip('/')
        # 找到第一个空格的位置，然后取后面的内容作为关键词
        space_pos = message.find(' ')
        if space_pos == -1:
            yield event.plain_result("❌ 用法: /添加撤回关键词 <关键词>")
            return
        keyword = message[space_pos+1:].strip()
        if not keyword:
            yield event.plain_result("❌ 用法: /添加撤回关键词 <关键词>")
            return

        add_recall_keyword(group_id, keyword)
        yield event.plain_result(f"✅ 已添加撤回关键词: {keyword}")

    async def _handle_remove_recall_keyword(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        message = message.lstrip('/')
        # 找到第一个空格的位置，然后取后面的内容作为关键词
        space_pos = message.find(' ')
        if space_pos == -1:
            yield event.plain_result("❌ 用法: /删除撤回关键词 <关键词>")
            return
        keyword = message[space_pos+1:].strip()
        if not keyword:
            yield event.plain_result("❌ 用法: /删除撤回关键词 <关键词>")
            return

        remove_recall_keyword(group_id, keyword)
        yield event.plain_result(f"✅ 已删除撤回关键词: {keyword}")

    async def _handle_set_recall_mode(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /设置撤回模式 <模式>")
            return
        
        mode = parts[1]
        if mode not in ["keyword", "regex", "all"]:
            yield event.plain_result("❌ 模式必须是 keyword/regex/all")
            return
        
        set_recall_mode(group_id, mode)
        yield event.plain_result(f"✅ 已设置撤回模式为: {mode}")

    async def _handle_enable_recall_notification(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        from .qqadmin.group_config import update_group_feature_config
        update_group_feature_config(group_id, "recall", "notification", True)
        yield event.plain_result("✅ 已开启撤回通知")

    async def _handle_disable_recall_notification(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        from .qqadmin.group_config import update_group_feature_config
        update_group_feature_config(group_id, "recall", "notification", False)
        yield event.plain_result("✅ 已关闭撤回通知")

    async def _handle_set_recall_notification(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        message = message.lstrip('/')
        space_pos = message.find(' ')
        if space_pos == -1:
            yield event.plain_result("❌ 用法: /设置撤回通知 <通知内容>")
            return
        notification_message = message[space_pos+1:].strip()
        if not notification_message:
            yield event.plain_result("❌ 用法: /设置撤回通知 <通知内容>")
            return
        
        from .qqadmin.group_config import update_group_feature_config
        update_group_feature_config(group_id, "recall", "notification_message", notification_message)
        yield event.plain_result(f"✅ 已设置撤回通知为: {notification_message}")

    async def _handle_list_recall_keywords(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        keywords = get_recall_keywords(group_id)
        if not keywords:
            yield event.plain_result("📋 当前没有撤回关键词")
            return
        
        result = "📋 撤回关键词列表:\n"
        for keyword in keywords:
            result += f"- {keyword}\n"
        yield event.plain_result(result.strip())

    async def _handle_add_recall_whitelist(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()

        user_id = None
        for part in parts:
            if part.startswith("[CQ:at,qq="):
                start = part.find("qq=") + 3
                end = part.find("]", start)
                if end > start:
                    user_id = part[start:end]
                    break
            elif part.startswith("/添加撤回白名单"):
                continue
            elif not user_id:
                user_id = part

        if not user_id:
            yield event.plain_result("❌ 用法: /添加撤回白名单 @用户")
            return

        add_whitelist_user(group_id, user_id)
        add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                          "添加撤回白名单", user_id, "", "")
        yield event.plain_result(f"✅ 已将用户 {user_id} 添加到撤回白名单")

    async def _handle_recall_user_messages(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()

        # 使用现有的解析函数来获取用户ID
        user_id = self._parse_user_id(message, ["/撤回", "撤回"])
        count = 1
        recall_time_str = None
        is_all = False

        # 解析数量和其他参数
        for part in parts:
            if part in ["撤回", "/撤回"]:
                continue
            elif part == "全部" or part == "all":
                is_all = True
            elif part.isdigit():
                count = int(part)
            elif "(" in part and ")" in part:  # 跳过可能是艾特的部分
                continue
            elif part.startswith("@"):  # 跳过可能是艾特的部分
                continue
            elif part.startswith("[CQ:"):  # 跳过CQ码部分
                continue
            else:
                recall_time_str = part

        # 如果有艾特用户，只看数量，忽略是否"全部"
        if user_id:
            is_all = False

        from .qqadmin.recall import (
            recall_messages,
            recall_user_messages_by_api,
            recall_recent_messages_by_api,
            get_max_recall_count,
            get_recall_help_text
        )

        max_count = get_max_recall_count(group_id)
        if count > max_count:
            count = max_count
        if count < 1:
            count = 1

        if user_id:
            # 撤回指定用户的消息
            result = await recall_user_messages_by_api(event, user_id, count)
            if result.get("success") is not False:
                res = result
                success_count = res.get("success", 0)
                failed_count = res.get("failed", 0)
                total_messages = res.get("total", 0)
                add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                                  "撤回用户消息", user_id, "", f"撤回{success_count}条")

                msg = f"✅ 已撤回用户 {user_id} 的 {success_count} 条消息"
                if failed_count > 0:
                    msg += f"\n⚠️ 失败: {failed_count} 条（可能是超过撤回时限）"
                yield event.plain_result(msg.strip())
            else:
                yield event.plain_result(f"❌ 撤回失败: {result.get('error', '未知错误')}")
        elif is_all or count > 1:
            # 撤回最近的群消息（排除管理员和群主）
            result = await recall_recent_messages_by_api(event, count, exclude_admins=True)
            if result.get("success") is not False:
                res = result
                success_count = res.get("success", 0)
                failed_count = res.get("failed", 0)
                total_messages = res.get("total", 0)
                add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                                  "撤回所有消息", "all", "", f"撤回{success_count}条")

                msg = f"✅ 已撤回 {success_count} 条消息"
                if failed_count > 0:
                    msg += f"\n⚠️ 失败: {failed_count} 条（可能是超过撤回时限）"
                yield event.plain_result(msg.strip())
            else:
                yield event.plain_result(f"❌ 撤回失败: {result.get('error', '未知错误')}")
        else:
            yield event.plain_result(get_recall_help_text())

    async def _handle_set_recall_time(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()

        if len(parts) < 2:
            yield event.plain_result("""❌ 用法: /设置撤回时间 <时间>
支持格式: 5s(秒), 5m(分), 5h(时), 5d(天)
示例:
/设置撤回时间 60s       设置60秒
/设置撤回时间 5m        设置5分钟
/设置撤回时间 1h        设置1小时""")
            return

        time_str = parts[1]
        from .qqadmin.recall import parse_time_duration, set_recall_time

        seconds = parse_time_duration(time_str)
        if seconds == 0:
            yield event.plain_result("❌ 无效的时间格式")
            return

        set_recall_time(group_id, seconds)
        yield event.plain_result(f"✅ 已设置撤回时间为: {seconds}秒")

    async def _handle_set_recall_max(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()

        if len(parts) < 2:
            yield event.plain_result("""❌ 用法: /设置撤回数量 <数量>
示例:
/设置撤回数量 10     设置最大撤回数量为10条""")
            return

        try:
            count = int(parts[1])
            if count < 1:
                count = 1
            if count > 50:
                count = 50
        except ValueError:
            yield event.plain_result("❌ 无效的数量格式")
            return

        from .qqadmin.recall import set_max_recall_count
        set_max_recall_count(group_id, count)
        yield event.plain_result(f"✅ 已设置最大撤回数量为: {count}条")

    async def _handle_enable_self_recall(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        from .qqadmin.recall import enable_self_recall, get_self_recall_time
        enable_self_recall(group_id, True)
        recall_time = get_self_recall_time(group_id)
        yield event.plain_result(f"✅ 已开启自身撤回功能\n⏱️ 机器人消息将在 {recall_time} 秒后自动撤回\n💡 提示: 撤回时会显示为当前账号操作（这是协议限制）")

    async def _handle_disable_self_recall(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        from .qqadmin.recall import enable_self_recall
        enable_self_recall(group_id, False)
        yield event.plain_result("✅ 已关闭自身撤回功能")

    async def _handle_set_self_recall_time(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()

        if len(parts) < 2:
            yield event.plain_result("""❌ 用法: /设置自身撤回时间 <秒数>
示例:
/设置自身撤回时间 30     设置30秒后撤回
/设置自身撤回时间 5m     设置5分钟后撤回
⏱️ 范围: 5-300秒""")
            return

        time_str = parts[1]
        from .qqadmin.recall import parse_time_duration, set_self_recall_time
        seconds = parse_time_duration(time_str)
        if seconds == 0 or seconds < 5:
            seconds = 5
        if seconds > 300:
            seconds = 300

        set_self_recall_time(group_id, seconds)
        yield event.plain_result(f"✅ 已设置自身撤回时间为: {seconds}秒")

    async def _handle_view_self_recall(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        from .qqadmin.recall import get_self_recall_status, get_pending_self_recalls_count
        status = get_self_recall_status(group_id)
        pending_count = get_pending_self_recalls_count()

        status_text = "✅ 已开启" if status["enabled"] else "❌ 已关闭"
        yield event.plain_result(f"""📋 自身撤回状态
━━━━━━━━━━━━━━
状态: {status_text}
撤回时间: {status["time"]}秒
待撤回任务: {pending_count}个
━━━━━━━━━━━━━━
命令:
/开启自身撤回   - 开启功能
/关闭自身撤回   - 关闭功能
/设置自身撤回时间 [秒] - 设置时间""")

    # ==================== 入群欢迎处理函数 ====================
    async def _handle_enable_welcome(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        enable_welcome(group_id, True)
        set_feature_enabled(group_id, "welcome_enabled", True, self.config)
        yield event.plain_result("✅ 已开启入群欢迎功能\n💡 可使用 /克隆全局 一次性启用所有功能")

    async def _handle_disable_welcome(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        enable_welcome(group_id, False)
        yield event.plain_result("✅ 已关闭入群欢迎功能")

    async def _handle_set_welcome_message(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        # 支持带斜杠和不带斜杠的格式
        if message.startswith("/设置欢迎语"):
            content = message[6:].strip()  # "/设置欢迎语" = 6个字符
        elif message.startswith("设置欢迎语"):
            content = message[5:].strip()  # "设置欢迎语" = 5个字符
        elif message.startswith("/设置欢迎"):
            content = message[5:].strip()  # "/设置欢迎" = 5个字符
        elif message.startswith("设置欢迎"):
            content = message[4:].strip()  # "设置欢迎" = 4个字符
        else:
            content = ""
        
        if not content:
            yield event.plain_result("❌ 用法: /设置欢迎语 <内容>\n可用占位符: {user_name}, {user_id}, {group_id}, {time}")
            return
        
        set_welcome_message(group_id, content)
        yield event.plain_result(f"✅ 已设置欢迎语: {content}")

    async def _handle_set_farewell_message(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        # 支持带斜杠和不带斜杠的格式
        if message.startswith("/设置退群语"):
            content = message[6:].strip()  # "/设置退群语" = 6个字符
        elif message.startswith("设置退群语"):
            content = message[5:].strip()  # "设置退群语" = 5个字符
        else:
            content = ""
        
        if not content:
            yield event.plain_result("❌ 用法: /设置退群语 <内容>")
            return
        
        set_farewell_message(group_id, content)
        yield event.plain_result(f"✅ 已设置退群语: {content}")

    async def _handle_view_welcome_settings(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        settings = get_welcome_settings(group_id)
        result = f"""🎉 当前欢迎设置:
启用状态: {'✅ 开启' if settings.get('enabled', False) else '❌ 关闭'}
欢迎语: {settings.get('welcome_message', '未设置')}
退群语: {settings.get('farewell_message', '未设置')}
艾特新人: {'✅ 开启' if settings.get('at_new_member', True) else '❌ 关闭'}"""
        yield event.plain_result(result)

    async def _handle_set_at_new_member(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /设置艾特新人 <开/关>")
            return
        
        enabled = parts[1]
        if enabled in ["开", "开启", "true", "True", "1"]:
            set_at_new_member(group_id, True)
            yield event.plain_result("✅ 已开启艾特新人功能")
        elif enabled in ["关", "关闭", "false", "False", "0"]:
            set_at_new_member(group_id, False)
            yield event.plain_result("❌ 已关闭艾特新人功能")
        else:
            yield event.plain_result("❌ 参数错误，应为: 开 或 关")

    # ==================== 分群设置处理函数 ====================
    async def _handle_enable_feature(self, event: AstrMessageEvent) -> None:
        if not await self._is_group_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /启用功能 <功能名>")
            return

        feature = parts[1]
        feature_map = {
            "reply": ("自定义回复", "reply_enabled"),
            "mute": ("禁言系统", "mute_enabled"),
            "kick": ("踢出系统", "kick_enabled"),
            "blacklist": ("黑名单系统", "blacklist_enabled"),
            "audit": ("入群审核", "audit_enabled"),
            "recall": ("撤回系统", "recall_enabled"),
            "welcome": ("入群欢迎", "welcome_enabled")
        }

        if feature not in feature_map:
            yield event.plain_result("❌ 未知功能名，可用: reply/mute/kick/blacklist/audit/recall/welcome")
            return

        feature_name, config_key = feature_map[feature]
        set_feature_enabled(group_id, config_key, True, self.config)
        yield event.plain_result(f"✅ 已启用【{feature_name}】")

    async def _handle_disable_feature(self, event: AstrMessageEvent) -> None:
        if not await self._is_group_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /禁用功能 <功能名>")
            return

        feature = parts[1]
        feature_map = {
            "reply": ("自定义回复", "reply_enabled"),
            "mute": ("禁言系统", "mute_enabled"),
            "kick": ("踢出系统", "kick_enabled"),
            "blacklist": ("黑名单系统", "blacklist_enabled"),
            "audit": ("入群审核", "audit_enabled"),
            "recall": ("撤回系统", "recall_enabled"),
            "welcome": ("入群欢迎", "welcome_enabled")
        }

        if feature not in feature_map:
            yield event.plain_result("❌ 未知功能名，可用: reply/mute/kick/blacklist/audit/recall/welcome")
            return

        feature_name, config_key = feature_map[feature]
        set_feature_enabled(group_id, config_key, False, self.config)
        yield event.plain_result(f"✅ 已禁用【{feature_name}】")

    async def _handle_list_features(self, event: AstrMessageEvent) -> None:
        if not await self._is_group_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()

        # 如果指定了功能名，显示详细配置
        if len(parts) >= 2 and parts[1] in ["audit", "blacklist", "welcome", "recall", "mute", "kick", "reply"]:
            feature = parts[1]
            await self._handle_view_feature_detail(event, feature)
            return

        config = get_group_config(group_id, self.config)

        result = """📋 功能状态列表:

【🔧 全局设置】(默认)
"""

        global_features = [
            ("reply", "自定义回复", self.qqadmin_enable_reply),
            ("mute", "禁言系统", self.qqadmin_enable_mute),
            ("kick", "踢出系统", self.qqadmin_enable_kick),
            ("blacklist", "黑名单系统", self.qqadmin_enable_blacklist),
            ("audit", "入群审核", self.qqadmin_enable_audit),
            ("recall", "撤回系统", self.qqadmin_enable_recall),
            ("welcome", "入群欢迎", self.qqadmin_enable_welcome),
            ("group_config", "分群设置", self.qqadmin_enable_group_config)
        ]

        for key, desc, global_val in global_features:
            result += f"  {desc}: {'✅' if global_val else '❌'}\n"

        custom_settings = []
        for key, desc, _ in global_features:
            group_val = get_group_feature_setting(group_id, key, "enabled", self.config)
            if group_val is not None:
                custom_settings.append((desc, group_val))

        if custom_settings:
            result += "\n【⚙️ 本群单独设置】\n"
            for desc, val in custom_settings:
                result += f"  {desc}: {'✅' if val else '❌'} (已修改)\n"
        else:
            result += "\n【⚙️ 本群单独设置】\n"
            result += "  (无，使用全局设置)\n"

        result += """
━━━━━━━━━━━━━━━
💡 提示: /查看功能状态 <功能名> 可查看详细配置
💡 可用功能: audit/blacklist/welcome/recall/mute/kick/reply"""
        yield event.plain_result(result)

    async def _handle_view_feature_detail(self, event: AstrMessageEvent, feature: str) -> None:
        """查看功能详细配置"""
        if not await self._is_group_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        feature_detail = get_group_feature_detail(group_id, feature, self.config)

        feature_names = {
            "audit": "入群审核",
            "blacklist": "黑名单",
            "welcome": "入群欢迎",
            "recall": "消息撤回",
            "mute": "禁言系统",
            "kick": "踢出系统",
            "reply": "自定义回复"
        }

        result = f"""{feature_names.get(feature, feature)}

状态: {'✅ 启用' if feature_detail.get('enabled', False) else '❌ 禁用'}
"""

        if feature == "audit":
            result += f"""审批模式: {feature_detail.get('approval_mode', 'math')}
验证超时: {feature_detail.get('verification_timeout', 300)}秒
最大尝试: {feature_detail.get('max_attempts', 3)}次
管理员免审: {'✅ 是' if feature_detail.get('admin_bypass', True) else '❌ 否'}
邀请审核: {'✅ 启用' if feature_detail.get('enable_invite_audit', False) else '❌ 禁用'}"""

        elif feature == "blacklist":
            result += f"""自动踢出: {'✅ 是' if feature_detail.get('auto_kick', True) else '❌ 否'}
退群入黑: {'✅ 是' if feature_detail.get('auto_blacklist_on_leave', False) else '❌ 否'}
验证失败入黑: {'✅ 是' if feature_detail.get('auto_blacklist_on_verify_fail', False) else '❌ 否'}"""

        elif feature == "welcome":
            result += f"""自动欢迎: {'✅ 是' if feature_detail.get('auto_welcome', False) else '❌ 否'}
欢迎延迟: {feature_detail.get('delay', 0)}秒
欢迎消息: {feature_detail.get('message', '欢迎 {{user_name}} 加入本群！')}
退群消息: {'✅ 启用' if feature_detail.get('farewell_enabled', False) else '❌ 禁用'}
关键词回复: {'✅ 启用' if feature_detail.get('auto_reply_enabled', True) else '❌ 禁用'}"""

        elif feature == "recall":
            result += f"""允许自撤: {'✅ 是' if feature_detail.get('self_recall', True) else '❌ 否'}
撤回时限: {feature_detail.get('time_limit', 60)}秒
触发模式: {feature_detail.get('mode', 'keyword')}"""

        elif feature == "mute":
            result += f"""禁言等级: {feature_detail.get('level', 5)}
默认时长: {feature_detail.get('duration', 300)}秒"""

        elif feature == "kick":
            result += f"""自动拉黑: {'✅ 是' if feature_detail.get('auto_blacklist', False) else '❌ 否'}
操作上限: {feature_detail.get('limit', 3)}次"""

        elif feature == "reply":
            result += f"""冷却时间: {feature_detail.get('cooldown', 0)}秒"""

        result += """
使用 /设置功能 <功能名> <设置项> <值> 修改配置"""

        yield event.plain_result(result)

    async def _handle_set_feature_setting(self, event: AstrMessageEvent) -> None:
        """设置功能的详细配置"""
        if not await self._is_group_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()

        if len(parts) < 3:
            yield event.plain_result("""用法: /设置功能 <功能名> <设置项> <值>

功能名: audit/blacklist/welcome/recall/mute/kick/reply

审核: mode <math/direct/id> | timeout <秒> | attempts <次数> | admin_bypass <0/1>
黑名单: auto_kick <0/1> | auto_blacklist_leave <0/1> | auto_blacklist_fail <0/1>
欢迎: auto_welcome <0/1> | delay <秒> | message <文本> | farewell <0/1>
撤回: self_recall <0/1> | time_limit <秒>
禁言: level <1-9> | duration <秒>

示例:
/设置功能 audit mode math
/设置功能 welcome message 欢迎{nick}入群""")
            return

        feature = parts[1]
        setting = parts[2]
        value_str = " ".join(parts[3:]) if len(parts) > 3 else ""

        # 首先确保群配置文件存在
        get_group_config(group_id, self.config)

        # 设置项映射
        setting_map = {
            "audit": {
                "mode": ("approval_mode", ["math", "direct", "id"]),
                "timeout": ("verification_timeout", int),
                "attempts": ("max_attempts", int),
                "admin_bypass": ("admin_bypass", lambda x: x == "1"),
                "invite": ("enable_invite_audit", lambda x: x == "1")
            },
            "blacklist": {
                "auto_kick": ("auto_kick", lambda x: x == "1"),
                "auto_blacklist_leave": ("auto_blacklist_on_leave", lambda x: x == "1"),
                "auto_blacklist_fail": ("auto_blacklist_on_verify_fail", lambda x: x == "1")
            },
            "welcome": {
                "auto_welcome": ("auto_welcome", lambda x: x == "1"),
                "delay": ("delay", int),
                "message": ("message", lambda x: x),
                "farewell": ("farewell_enabled", lambda x: x == "1")
            },
            "recall": {
                "self_recall": ("self_recall", lambda x: x == "1"),
                "time_limit": ("time_limit", int)
            },
            "mute": {
                "level": ("level", int),
                "duration": ("duration", int)
            },
            "kick": {
                "auto_blacklist": ("auto_blacklist", lambda x: x == "1"),
                "limit": ("limit", int)
            },
            "reply": {
                "cooldown": ("cooldown", int)
            }
        }

        if feature not in setting_map:
            yield event.plain_result(f"❌ 未知功能: {feature}\n可用: audit/blacklist/welcome/recall/mute/kick/reply")
            return

        if setting not in setting_map[feature]:
            available = "/".join(setting_map[feature].keys())
            yield event.plain_result(f"❌ 未知设置项: {setting}\n可用: {available}")
            return

        config_key, converter = setting_map[feature][setting]

        try:
            if feature == "welcome" and setting == "message":
                value = value_str
            else:
                value = converter(value_str)
        except (ValueError, TypeError):
            yield event.plain_result(f"❌ 值格式错误: {value_str}")
            return

        update_group_feature_config(group_id, feature, config_key, value)
        yield event.plain_result(f"✅ 已设置【{feature}】的【{setting}】为【{value}】\n已保存到分群配置")

    async def _handle_reset_group_config(self, event: AstrMessageEvent) -> None:
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._check_feature_permission_async(event, "group_config", "modify"):
            yield event.plain_result("❌ 权限不足，无法执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        reset_group_config(group_id)
        yield event.plain_result("✅ 已重置群组配置\n分群现在使用系统默认配置")

    async def _handle_clone_group_config(self, event: AstrMessageEvent) -> None:
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._check_feature_permission_async(event, "group_config", "modify"):
            yield event.plain_result("❌ 权限不足，无法执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /克隆配置 <源群组ID>")
            return

        source_group_id = parts[1]
        clone_group_config(source_group_id, group_id)
        yield event.plain_result(f"✅ 已从群组 {source_group_id} 克隆配置到当前群\n分群现在使用新的独立配置")

    async def _handle_clone_global_config(self, event: AstrMessageEvent) -> None:
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._check_feature_permission_async(event, "group_config", "modify"):
            yield event.plain_result("❌ 权限不足，无法执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        # 检查授权状态（如果启用了授权系统）
        auth_enabled = self.config.get("auth", {}).get("enabled", False)
        if auth_enabled:
            from .auth.manager import is_group_authorized
            if not is_group_authorized(group_id):
                yield event.plain_result("❌ 该群组未获得授权，无法克隆全局配置。")
                return

        # 使用新的克隆全局配置函数，传入插件配置
        clone_global_config_to_group(group_id, self.config)
        
        # 获取群组信息用于反馈
        group_name = ""
        try:
            # 尝试获取群名称（如果API支持）
            group_info = await self._get_group_info(group_id)
            group_name = group_info.get("group_name", "")
        except Exception:
            pass
        
        if group_name:
            yield event.plain_result(f"✅ 已同步全局配置到群组 {group_name}\n分群现在使用自己的独立配置")
        else:
            yield event.plain_result(f"✅ 已同步全局配置到群组 {group_id}\n分群现在使用自己的独立配置")

    async def _handle_clear_group_data(self, event: AstrMessageEvent) -> None:
        """清除分群所有数据"""
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._check_feature_permission_async(event, "group_config", "modify"):
            yield event.plain_result("❌ 权限不足，无法执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        from .qqadmin.group_config import clear_group_data
        clear_group_data(group_id)
        
        yield event.plain_result(
            f"✅ 已清除群组 {group_id} 的所有数据\n"
            f"📋 已清空的内容:\n"
            f"  - 回复列表\n"
            f"  - 用户专属回复\n"
            f"  - 黑名单/白名单\n"
            f"  - 审核队列/历史\n"
            f"  - 欢迎历史\n"
            f"  - 操作日志\n"
            f"  - 邀请统计\n"
            f"  - 踢出记录\n"
            f"  - 分群管理员\n"
            f"💡 所有功能已恢复到默认状态"
        )

    async def _handle_sync_global_blacklist(self, event: AstrMessageEvent) -> None:
        """添加黑名单到当前群"""
        if not await self._check_feature_permission_async(event, "blacklist", "use"):
            yield event.plain_result("❌ 权限不足，无法执行此操作。")
            return
        
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        # 提示用户使用正确的命令
        yield event.plain_result("ℹ️ 当前架构已改为分群独立存储，请使用 /拉黑 @用户 来添加黑名单\n每个群的黑名单是独立的")

    async def _handle_config_global(self, event: AstrMessageEvent) -> None:
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._check_feature_permission_async(event, "global_config", "use"):
            yield event.plain_result("❌ 权限不足，无法执行此操作。")
            return

        qqadmin = self.config.get("qqadmin", {})
        reply_cfg = qqadmin.get("reply", {})
        mute_cfg = qqadmin.get("mute", {})
        kick_cfg = qqadmin.get("kick", {})
        blacklist_cfg = qqadmin.get("blacklist", {})
        audit_cfg = qqadmin.get("audit", {})
        recall_cfg = qqadmin.get("recall", {})
        welcome_cfg = qqadmin.get("welcome", {})
        stats_cfg = qqadmin.get("stats", {})
        scum_cfg = self.config.get("scum", {})
        auth_cfg = self.config.get("auth", {})

        result = "全局配置:\n\n"

        result += "自定义回复\n"
        result += f"开关: {'✅ 开启' if reply_cfg.get('enable_reply') else '❌ 关闭'}\n"
        result += f"冷却时间: {reply_cfg.get('default_reply_cooldown', 0)}秒\n\n"

        result += "禁言系统\n"
        result += f"开关: {'✅ 开启' if mute_cfg.get('enable_mute') else '❌ 关闭'}\n"
        result += f"默认级别: {mute_cfg.get('default_mute_level', 5)}\n"
        result += f"默认时长: {mute_cfg.get('default_mute_duration', 300)}秒\n\n"

        result += "踢出系统\n"
        result += f"开关: {'✅ 开启' if kick_cfg.get('enable_kick') else '❌ 关闭'}\n\n"

        result += "黑名单系统\n"
        result += f"开关: {'✅ 开启' if blacklist_cfg.get('enable_blacklist') else '❌ 关闭'}\n"
        result += f"自动踢出: {'✅ 是' if blacklist_cfg.get('auto_kick_blacklisted') else '❌ 否'}\n"
        result += f"退群入黑: {'✅ 是' if blacklist_cfg.get('auto_blacklist_on_leave') else '❌ 否'}\n"
        result += f"验证失败入黑: {'✅ 是' if blacklist_cfg.get('auto_blacklist_on_verify_fail') else '❌ 否'}\n\n"

        result += "入群审核\n"
        result += f"开关: {'✅ 开启' if audit_cfg.get('enable_audit') else '❌ 关闭'}\n"
        result += f"审核方式: {audit_cfg.get('default_approval_mode', 'math')}\n"
        result += f"验证超时: {audit_cfg.get('default_verification_timeout', 300)}秒\n"
        result += f"最大尝试: {audit_cfg.get('max_verify_attempts', 3)}次\n"
        result += f"管理员免审: {'✅ 是' if audit_cfg.get('admin_bypass_verify') else '❌ 否'}\n"
        result += f"邀请审核: {'✅ 开启' if audit_cfg.get('enable_invite_audit') else '❌ 关闭'}\n\n"

        result += "消息撤回\n"
        result += f"开关: {'✅ 开启' if recall_cfg.get('enable_recall') else '❌ 关闭'}\n"
        result += f"允许自撤: {'✅ 是' if recall_cfg.get('enable_self_recall') else '❌ 否'}\n"
        result += f"撤回时限: {recall_cfg.get('default_recall_time', 60)}秒\n"
        result += f"撤回模式: {recall_cfg.get('default_recall_mode', 'keyword')}\n\n"

        result += "入群欢迎\n"
        result += f"开关: {'✅ 开启' if welcome_cfg.get('enable_welcome') else '❌ 关闭'}\n"
        result += f"自动欢迎: {'✅ 是' if welcome_cfg.get('enable_auto_welcome') else '❌ 否'}\n"
        result += f"欢迎延迟: {welcome_cfg.get('default_welcome_delay', 0)}秒\n"
        result += f"欢迎词: {welcome_cfg.get('default_welcome_message', '欢迎 {user_name} 加入本群！')}\n"
        result += f"退群消息: {'✅ 开启' if welcome_cfg.get('enable_farewell') else '❌ 关闭'}\n\n"

        result += "邀请统计\n"
        result += f"开关: {'✅ 开启' if stats_cfg.get('enable_stats') else '❌ 关闭'}\n\n"

        result += "SCUM功能\n"
        result += f"查询开关: {'✅ 开启' if scum_cfg.get('enable_query') else '❌ 关闭'}\n"
        result += f"绑定开关: {'✅ 开启' if scum_cfg.get('enable_binding') else '❌ 关闭'}\n\n"

        result += "授权系统\n"
        result += f"开关: {'✅ 开启' if auth_cfg.get('enabled') else '❌ 关闭'}\n"

        yield event.plain_result(result)

    # ==================== 自定义回复处理函数 ====================
    async def _handle_add_reply(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        message = (event.message_str or "").strip()
        parts = message.split(None, 2)
        if len(parts) < 3:
            yield event.plain_result("❌ 用法: /添加回复 <问题> <答案>")
            return

        keyword = parts[1]
        reply_content = parts[2]

        add_reply(group_id, keyword, reply_content, exact=True)

        # 自动开启该群组的自定义回复功能
        from .qqadmin.group_config import update_group_feature_config
        update_group_feature_config(group_id, "reply", "enabled", True)

        add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                          "添加问答", keyword, "", reply_content)
        yield event.plain_result(f"✅ 已添加问答: {keyword} -> {reply_content}")

    async def _handle_add_fuzzy_reply(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        parts = message.split(None, 2)
        if len(parts) < 3:
            yield event.plain_result("❌ 用法: /模糊添加 <问题> <答案>")
            return
        
        keyword = parts[1]
        reply_content = parts[2]
        
        add_reply(group_id, keyword, reply_content, exact=False)
        add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                          "添加模糊问答", keyword, "", reply_content)
        yield event.plain_result(f"✅ 已添加模糊问答: {keyword} -> {reply_content}")

    async def _handle_remove_reply(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /删除 <问题>")
            return
        
        keyword = parts[1]
        remove_reply(group_id, keyword)
        add_operation_log(group_id, str(event.message_obj.sender.user_id), "",
                          "删除问答", keyword, "", "")
        yield event.plain_result(f"✅ 已删除问答: {keyword}")

    async def _handle_list_replies(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        replies = list_replies(group_id)
        if not replies:
            yield event.plain_result("📋 当前没有自定义回复")
            return
        
        result = "📋 自定义回复列表:\n"
        if replies.get("exact"):
            result += "【精确匹配】\n"
            for kw in replies["exact"]:
                result += f"- {kw}\n"
        if replies.get("fuzzy"):
            result += "\n【模糊匹配】\n"
            for kw in replies["fuzzy"]:
                result += f"- {kw}\n"
        if replies.get("regex"):
            result += "\n【正则匹配】\n"
            for kw in replies["regex"]:
                result += f"- {kw}\n"
        if replies.get("cooldown", 0) > 0:
            result += f"\n冷却时间: {replies['cooldown']} 秒"
        yield event.plain_result(result.strip())

    async def _handle_clear_replies(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        clear_replies(group_id)
        yield event.plain_result("✅ 已清空所有自定义回复")

    async def _handle_set_reply_cooldown(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /设置回复冷却 <秒数>")
            return
        
        try:
            seconds = int(parts[1])
            set_reply_cooldown(group_id, seconds)
            yield event.plain_result(f"✅ 已设置回复冷却时间: {seconds} 秒")
        except ValueError:
            yield event.plain_result("❌ 秒数必须是数字")

    async def _check_keyword_filter(self, event: AstrMessageEvent) -> bool:
        """
        检查消息是否包含敏感关键词
        返回 True 表示消息被过滤，False 表示正常
        """
        if not self.qqadmin_enable_keyword_filter:
            return False
        
        message = (event.message_str or "").strip()
        if not message:
            return False
        
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            return False
        
        user_id = str(event.message_obj.sender.user_id)
        
        # 检查是否是管理员或群主，如果是则跳过过滤（如果配置允许）
        if self.qqadmin_admin_bypass_filter:
            is_admin = await self._is_admin_or_owner_async(event)
            if is_admin:
                return False
        
        # 获取分群配置
        enable_filter = get_group_setting(group_id, "qqadmin.keyword_filter.enable_keyword_filter", None)
        if enable_filter is not None:
            if not enable_filter:
                return False
        elif not self.qqadmin_enable_keyword_filter:
            return False
        
        # 获取分群关键词配置
        filter_keywords = get_group_setting(group_id, "qqadmin.keyword_filter.filter_keywords", None)
        if filter_keywords is None:
            filter_keywords = self.qqadmin_filter_keywords
        
        filter_regex_patterns = get_group_setting(group_id, "qqadmin.keyword_filter.filter_regex_patterns", None)
        if filter_regex_patterns is None:
            filter_regex_patterns = self.qqadmin_filter_regex_patterns
        
        # 检查关键词
        for keyword in filter_keywords:
            if keyword in message:
                if self.qqadmin_filter_enable_log:
                    logger.info(f"关键词过滤触发: 用户 {user_id}, 群组 {group_id}, 关键词: {keyword}")
                await self._handle_filter_action(event, keyword)
                return True
        
        # 检查正则表达式
        import re
        for pattern in filter_regex_patterns:
            try:
                if re.search(pattern, message):
                    if self.qqadmin_filter_enable_log:
                        logger.info(f"正则过滤触发: 用户 {user_id}, 群组 {group_id}, 模式: {pattern}")
                    await self._handle_filter_action(event, pattern)
                    return True
            except re.error:
                logger.error(f"无效的正则表达式: {pattern}")
        
        return False

    async def _handle_filter_action(self, event: AstrMessageEvent, matched_content: str) -> None:
        """
        处理关键词过滤后的动作
        """
        group_id = str(event.message_obj.group_id)
        user_id = str(event.message_obj.sender.user_id)
        
        # 获取分群配置的动作
        filter_action = get_group_setting(group_id, "qqadmin.keyword_filter.filter_action", None)
        if filter_action is None:
            filter_action = self.qqadmin_filter_action
        
        filter_mute_duration = get_group_setting(group_id, "qqadmin.keyword_filter.filter_mute_duration", None)
        if filter_mute_duration is None:
            filter_mute_duration = self.qqadmin_filter_mute_duration
        
        warn_message = get_group_setting(group_id, "qqadmin.keyword_filter.warn_message", None)
        if warn_message is None:
            warn_message = self.qqadmin_warn_message
        
        # 执行动作
        if filter_action == "warn":
            await event.reply(warn_message)
        elif filter_action == "mute":
            try:
                platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform:
                    await platform.get_client().call_action(
                        action="set_group_ban",
                        group_id=int(group_id),
                        user_id=int(user_id),
                        duration=filter_mute_duration
                    )
                    await event.reply(f"⚠️ 您的消息包含敏感内容，已被禁言 {filter_mute_duration // 60} 分钟")
            except Exception as e:
                logger.error(f"禁言失败: {e}")
                await event.reply(warn_message)
        elif filter_action == "kick":
            try:
                platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform:
                    await platform.get_client().call_action(
                        action="set_group_kick",
                        group_id=int(group_id),
                        user_id=int(user_id),
                        reject_add_request=False
                    )
            except Exception as e:
                logger.error(f"踢人失败: {e}")
        elif filter_action == "blacklist":
            # 添加到黑名单
            add_to_blacklist(group_id, user_id, f"关键词过滤: {matched_content}")
            # 踢出群
            try:
                platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform:
                    await platform.get_client().call_action(
                        action="set_group_kick",
                        group_id=int(group_id),
                        user_id=int(user_id),
                        reject_add_request=True
                    )
            except Exception as e:
                logger.error(f"拉黑并踢出失败: {e}")
        
        # 添加操作日志
        if self.qqadmin_filter_enable_log:
            add_operation_log(group_id, user_id, "", "关键词过滤", matched_content, filter_action, "")

    async def _sync_group_config(self, event: AstrMessageEvent) -> None:
        """
        同步全局配置到当前群
        """
        # 使用异步权限检查确保正确识别管理员身份
        if not await self._check_feature_permission_async(event, "group_config", "modify"):
            yield event.plain_result("❌ 权限不足，无法执行此操作。")
            return
        
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        # 同步各个功能的配置
        config_keys = [
            # 全局群管设置
            "qqadmin.enable_group_manage",
            "qqadmin.owner_ignore_group_manage",
            "qqadmin.enable_group_config",
            
            # 自定义回复配置
            "qqadmin.reply.enable_reply",
            "qqadmin.reply.default_reply_cooldown",
            "qqadmin.reply.enable_keyword_reply",
            "qqadmin.reply.enable_exact_match",
            "qqadmin.reply.max_reply_length",
            "qqadmin.reply.use_permission",
            "qqadmin.reply.modify_permission",
            
            # 禁言系统配置
            "qqadmin.mute.enable_mute",
            "qqadmin.mute.default_mute_level",
            "qqadmin.mute.default_mute_duration",
            "qqadmin.mute.use_permission",
            "qqadmin.mute.modify_permission",
            
            # 踢出系统配置
            "qqadmin.kick.enable_kick",
            "qqadmin.kick.auto_blacklist_on_kick",
            "qqadmin.kick.default_kick_limit",
            "qqadmin.kick.use_permission",
            "qqadmin.kick.modify_permission",
            
            # 黑名单系统配置
            "qqadmin.blacklist.enable_blacklist",
            "qqadmin.blacklist.auto_kick_blacklisted",
            "qqadmin.blacklist.auto_blacklist_on_leave",
            "qqadmin.blacklist.auto_blacklist_on_verify_fail",
            "qqadmin.blacklist.use_permission",
            "qqadmin.blacklist.modify_permission",
            
            # 入群审核配置
            "qqadmin.audit.enable_audit",
            "qqadmin.audit.enable_invite_audit",
            "qqadmin.audit.default_approval_mode",
            "qqadmin.audit.default_verification_timeout",
            "qqadmin.audit.max_verify_attempts",
            "qqadmin.audit.admin_bypass_verify",
            "qqadmin.audit.use_permission",
            "qqadmin.audit.modify_permission",
            
            # 撤回系统配置
            "qqadmin.recall.enable_recall",
            "qqadmin.recall.enable_self_recall",
            "qqadmin.recall.default_recall_time",
            "qqadmin.recall.default_recall_mode",
            "qqadmin.recall.recall_keywords",
            "qqadmin.recall.recall_regex_patterns",
            "qqadmin.recall.enable_admin_recall",
            "qqadmin.recall.recall_notification",
            "qqadmin.recall.use_permission",
            "qqadmin.recall.modify_permission",
            
            # 统计系统配置
            "qqadmin.stats.enable_stats",
            "qqadmin.stats.auto_update_on_join",
            "qqadmin.stats.abnormal_invite_threshold",
            "qqadmin.stats.use_permission",
            "qqadmin.stats.modify_permission",
            
            # 入群欢迎配置
            "qqadmin.welcome.enable_welcome",
            "qqadmin.welcome.enable_auto_welcome",
            "qqadmin.welcome.welcome_check_interval",
            "qqadmin.welcome.default_welcome_delay",
            "qqadmin.welcome.default_welcome_message",
            "qqadmin.welcome.enable_farewell",
            "qqadmin.welcome.default_farewell_message",
            "qqadmin.welcome.welcome_keywords",
            "qqadmin.welcome.auto_reply_enabled",
            "qqadmin.welcome.at_new_member",
            "qqadmin.welcome.use_permission",
            "qqadmin.welcome.modify_permission",
            
            # 关键词过滤配置
            "qqadmin.keyword_filter.enable_keyword_filter",
            "qqadmin.keyword_filter.filter_keywords",
            "qqadmin.keyword_filter.filter_regex_patterns",
            "qqadmin.keyword_filter.filter_action",
            "qqadmin.keyword_filter.filter_mute_duration",
            "qqadmin.keyword_filter.warn_message",
            "qqadmin.keyword_filter.enable_log",
            "qqadmin.keyword_filter.admin_bypass_filter",
            "qqadmin.keyword_filter.use_permission",
            "qqadmin.keyword_filter.modify_permission",
            
            # 配置管理权限
            "qqadmin.group_config.use_permission",
            "qqadmin.group_config.modify_permission",
            "qqadmin.global_config.use_permission"
        ]
        
        synced_count = 0
        for config_key in config_keys:
            value = get_nested(*config_key.split("."), default=None)
            if value is not None:
                set_group_setting(group_id, config_key, value)
                synced_count += 1
        
        yield event.plain_result(f"✅ 已同步 {synced_count} 项配置到当前群")

    @filter.command("添加专属回复")
    async def handle_add_player_reply(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        
        if not self.qqadmin_enable_player_reply:
            yield event.plain_result("❌ 用户专属回复功能已关闭")
            return

        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群组中使用")
            return

        message = (event.message_str or "").strip()
        parts = message.split(None, 2)
        if len(parts) < 3:
            yield event.plain_result("❌ 用法: /添加专属回复 <QQ> <消息>")
            return

        user_id = parts[1]
        reply_message = parts[2]

        from .qqadmin.player_reply import get_player_reply, add_player_reply

        existing = get_player_reply(group_id, user_id)
        if existing:
            messages = existing.get('messages', [])
            messages.append(reply_message)
            add_player_reply(group_id, user_id, messages, existing.get('at_user', True), existing.get('enabled', True))
            yield event.plain_result(f"✅ 已添加用户 {user_id} 的专属回复\n📊 当前共 {len(messages)} 条消息，触发时随机发送一条")
        else:
            add_player_reply(group_id, user_id, [reply_message], self.player_reply_default_at_user)
            yield event.plain_result(f"✅ 已为用户 {user_id} 设置专属回复\n💡 可继续使用 /添加专属回复 添加更多消息")

    @filter.command("删除专属回复")
    async def handle_remove_player_reply(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return

        if not self.qqadmin_enable_player_reply:
            yield event.plain_result("❌ 用户专属回复功能已关闭")
            return

        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群组中使用")
            return

        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /删除专属回复 <QQ> [序号]\n💡 不填序号则删除该用户所有回复")
            return

        user_id = parts[1]

        # 检查是否指定了序号
        if len(parts) >= 3 and parts[2].isdigit():
            index = int(parts[2])
            from .qqadmin.player_reply import remove_player_reply_message, get_player_reply

            player_reply = get_player_reply(group_id, user_id)
            if not player_reply:
                yield event.plain_result(f"❌ 用户 {user_id} 没有专属回复配置")
                return

            messages = player_reply.get('messages', [])
            if index < 1 or index > len(messages):
                yield event.plain_result(f"❌ 序号无效，当前有 {len(messages)} 条回复消息")
                return

            removed_msg = messages[index - 1][:30] + ('...' if len(messages[index - 1]) > 30 else '')
            remove_player_reply_message(group_id, user_id, index)

            # 检查是否还有剩余消息
            player_reply_after = get_player_reply(group_id, user_id)
            remaining = len(player_reply_after.get('messages', [])) if player_reply_after else 0

            if remaining > 0:
                yield event.plain_result(f"✅ 已删除第 {index} 条回复: {removed_msg}\n📊 剩余 {remaining} 条回复")
            else:
                yield event.plain_result(f"✅ 已删除第 {index} 条回复: {removed_msg}\n📊 该用户已无回复配置")
        else:
            from .qqadmin.player_reply import remove_player_reply

            if remove_player_reply(group_id, user_id):
                yield event.plain_result(f"✅ 已删除用户 {user_id} 的所有专属回复")
            else:
                yield event.plain_result(f"❌ 用户 {user_id} 没有专属回复配置")

    @filter.command("设置专属回复")
    async def handle_set_player_reply(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        
        if not self.qqadmin_enable_player_reply:
            yield event.plain_result("❌ 用户专属回复功能已关闭")
            return

        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群组中使用")
            return

        message = (event.message_str or "").strip()
        parts = message.split(None, 2)
        if len(parts) < 3:
            yield event.plain_result("❌ 用法: /设置专属回复 <QQ> <消息>")
            return

        user_id = parts[1]
        reply_message = parts[2]

        from .qqadmin.player_reply import get_player_reply, add_player_reply

        existing = get_player_reply(group_id, user_id)
        old_count = len(existing.get('messages', [])) if existing else 0
        add_player_reply(group_id, user_id, [reply_message], existing.get('at_user', True) if existing else self.player_reply_default_at_user, existing.get('enabled', True) if existing else True)
        if old_count > 1:
            yield event.plain_result(f"✅ 已覆盖用户 {user_id} 的专属回复\n⚠️ 原 {old_count} 条消息已被替换为 1 条")
        else:
            yield event.plain_result(f"✅ 已设置用户 {user_id} 的专属回复")

    @filter.command("专属回复@")
    async def handle_player_reply_at(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return
        
        if not self.qqadmin_enable_player_reply:
            yield event.plain_result("❌ 用户专属回复功能已关闭")
            return

        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群组中使用")
            return

        message = (event.message_str or "").strip()
        parts = message.split()
        if len(parts) < 3:
            yield event.plain_result("❌ 用法: /专属回复@ <QQ> <开/关>")
            return

        user_id = parts[1]
        at_flag = parts[2]

        from .qqadmin.player_reply import update_player_at, get_player_reply

        if at_flag in ["开", "开启", "true", "True", "1"]:
            if update_player_at(group_id, user_id, True):
                yield event.plain_result(f"✅ 用户 {user_id} 的专属回复已开启@")
            else:
                yield event.plain_result(f"❌ 用户 {user_id} 没有专属回复配置")
        elif at_flag in ["关", "关闭", "false", "False", "0"]:
            if update_player_at(group_id, user_id, False):
                yield event.plain_result(f"✅ 用户 {user_id} 的专属回复已关闭@")
            else:
                yield event.plain_result(f"❌ 用户 {user_id} 没有专属回复配置")
        else:
            yield event.plain_result("❌ 参数错误，请输入 开 或 关")

    @filter.command("查看专属回复列表")
    async def handle_list_player_replies(self, event: AstrMessageEvent) -> None:
        if not await self._is_admin_or_owner_async(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return

        if not self.qqadmin_enable_player_reply:
            yield event.plain_result("❌ 用户专属回复功能已关闭")
            return

        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群组中使用")
            return

        from .qqadmin.player_reply import get_all_player_replies

        replies = get_all_player_replies(group_id)
        if not replies:
            yield event.plain_result("📋 当前没有用户专属回复配置")
            return

        result = "👤 用户专属回复列表:\n"
        result += "💡 多条消息会随机发送一条\n"
        for user_id, config in replies.items():
            messages = config.get('messages', [])
            at_user = config.get('at_user', True)
            enabled = config.get('enabled', True)
            status = "✅" if enabled else "❌"
            at_status = "📌@" if at_user else "🔕"
            result += f"\n{status} {user_id} {at_status}\n"
            result += f"   📝 共 {len(messages)} 条回复:\n"
            for i, msg in enumerate(messages, 1):
                display_msg = msg[:40] + ('...' if len(msg) > 40 else '')
                result += f"     {i}. {display_msg}\n"

        yield event.plain_result(result.strip())

    def _get_effective_config(self, group_id: str, config_key: str, default=None):
        """
        获取有效的配置值（优先使用分群配置，否则使用全局配置）
        """
        # 尝试获取分群配置
        value = get_group_setting(group_id, config_key, None)
        if value is not None:
            return value
        
        # 使用全局配置
        keys = config_key.split(".")
        return get_nested(*keys, default=default)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """在发送消息前拦截，将文本转换为图片"""
        from .qqadmin.group_config import get_group_data
        from .qqadmin.text_to_image import text_to_image, is_text_to_image_available
        
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        if not group_id or group_id == "None":
            return
        
        # 主人不受关机状态限制
        if not self._is_owner(event):
            features = get_group_data(group_id, "features", {})
            power_status = features.get("power_status", "")
            if power_status == "off":
                return
        
        if not self._should_convert_to_image(event):
            return
        
        if not is_text_to_image_available():
            return
        
        result = event.get_result()
        if result is None:
            return
        
        from astrbot.core.message.components import Plain
        text = ""
        for comp in result.chain:
            if isinstance(comp, Plain):
                text += comp.text
        
        if not text or not text.strip():
            return
        
        from .qqadmin.group_config import get_group_feature_setting
        
        font_size = get_group_feature_setting(group_id, "text_to_image", "font_size", self.config) or 16
        font_color = get_group_feature_setting(group_id, "text_to_image", "font_color", self.config) or "#333333"
        bg_color = get_group_feature_setting(group_id, "text_to_image", "bg_color", self.config) or "#ffffff"
        line_spacing = get_group_feature_setting(group_id, "text_to_image", "line_spacing", self.config) or 4
        padding = get_group_feature_setting(group_id, "text_to_image", "padding", self.config) or 20
        
        def hex_to_rgb(hex_color):
            hex_color = hex_color.lstrip('#')
            return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
        
        image_path = text_to_image(
            text, 
            font_size=font_size,
            text_color=hex_to_rgb(font_color),
            background_color=hex_to_rgb(bg_color),
            line_spacing=line_spacing,
            padding=padding
        )
        if not image_path:
            return
        
        try:
            from astrbot.core.message.components import Image
            
            result.chain = [Image(file=image_path)]
            
            event.set_extra("_text_to_image_temp_path", image_path)
        except Exception as e:
            logger.error(f"文转图失败: {e}")

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """消息发送后清理临时文件"""
        image_path = event.get_extra("_text_to_image_temp_path", None)
        if image_path:
            import os
            if os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except:
                    pass

    async def terminate(self):
        """插件卸载时取消所有待撤回任务"""
        for task in self._recall_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._recall_tasks, return_exceptions=True)
        self._recall_tasks.clear()
        logger.info("自身撤回插件已卸载")
