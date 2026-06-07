import time
import asyncio

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

from .scum import ServerQuery
from .auth import (
    generate_license_key, verify_license_key,
    load_bindings, save_bindings, bind_server, unbind_server, get_group_binding,
    load_auth_data, save_auth_data,
    load_group_settings, save_group_settings,
    get_group_setting, set_group_setting,
    authorize_group, deauthorize_group, add_auth_time, reduce_auth_time,
    is_group_authorized, get_group_auth_info, list_all_authorizations,
    delete_all_keys, delete_used_keys, get_unused_keys, get_used_keys, mark_key_used,
    get_auth_statistics
)


@register("astrbot_plugin_joker", "Joker", "SCUM服务器查询与卡密授权插件", "2.0.0")
class JokerPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.base_url = "https://api.battlemetrics.com"
        self.config = config or {}
        self.whitelist_groups = self.config.get("whitelist_groups", [])
        self.admin_groups = self.config.get("admin_groups", [])
        self.default_enabled = self.config.get("default_enabled", True)
        self.allow_other_query = self.config.get("allow_other_query", True)
        self.allow_slash_prefix = self.config.get("allow_slash_prefix", True)
        self.owner_ids = self.config.get("owner_ids", [])
        self.owner_ignore_auth = self.config.get("owner_ignore_auth", True)
        self.owner_ignore_binding = self.config.get("owner_ignore_binding", True)
        self.enable_auth = self.config.get("enable_auth", False)
        self.auto_delete_used_keys_days = self.config.get("auto_delete_used_keys_days", 0)
        self.enable_query = self.config.get("enable_query", True)
        self.enable_binding = self.config.get("enable_binding", True)
        self.enable_settings = self.config.get("enable_settings", True)
        self.enable_help = self.config.get("enable_help", True)
        self.auth_key = self.config.get("auth_key", "")

        self.server_query = ServerQuery(self.base_url)
        self._context = context

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        sender = event.message_obj.sender
        sender_id = sender.user_id if hasattr(sender, 'user_id') else ""
        return str(sender_id) in [str(oid) for oid in self.owner_ids]

    def _is_slash_prefix_allowed(self, event: AstrMessageEvent) -> bool:
        group_id = event.message_obj.group_id
        if group_id:
            group_setting = get_group_setting(group_id, "allow_slash_prefix", None)
            if group_setting is not None:
                return group_setting
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

    @filter.command("绑定id")
    async def bind_id_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群未授权，无法使用此功能")
            return
        if not self.enable_binding:
            yield event.plain_result("❌ 绑定功能已关闭")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 无法获取群ID")
            return
        args = event.message_str.strip().split()
        if len(args) < 2:
            current_binding = get_group_binding(group_id)
            if current_binding:
                yield event.plain_result(f"📋 当前绑定服务器ID: {current_binding}")
            else:
                yield event.plain_result("📋 使用方法: /绑定id <服务器ID>\n当前未绑定")
            return
        server_id = args[1]
        bind_server(group_id, server_id)
        yield event.plain_result(f"✅ 已绑定服务器ID: {server_id}")

    @filter.command("解绑")
    async def unbind_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群未授权，无法使用此功能")
            return
        if not self.enable_binding:
            yield event.plain_result("❌ 绑定功能已关闭")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 无法获取群ID")
            return
        current_binding = get_group_binding(group_id)
        if not current_binding:
            yield event.plain_result("⚠️ 该群尚未绑定任何服务器")
            return
        unbind_server(group_id)
        yield event.plain_result(f"✅ 已解绑服务器: {current_binding}")

    @filter.command("绑定")
    async def bind_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群未授权，无法使用此功能")
            return
        if not self.enable_binding:
            yield event.plain_result("❌ 绑定功能已关闭")
            return
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("❌ 无法获取群ID")
            return
        args = event.message_str.strip().split()
        if len(args) < 2:
            current_binding = get_group_binding(group_id)
            if current_binding:
                yield event.plain_result(f"📋 当前绑定服务器ID: {current_binding}\n使用方法: /绑定 <服务器ID> 解绑: /解绑")
            else:
                yield event.plain_result("📋 使用方法: /绑定 <服务器ID>\n解绑: /解绑\n当前未绑定")
            return
        server_id = args[1]
        bind_server(group_id, server_id)
        yield event.plain_result(f"✅ 已绑定服务器ID: {server_id}\n解绑: /解绑")

    @filter.command("查询在线")
    async def query_online_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群未授权，无法使用此功能")
            return
        if not self.enable_query:
            yield event.plain_result("❌ 查询功能已关闭")
            return
        group_id = str(event.message_obj.group_id)
        server_id = get_group_binding(group_id) if group_id and group_id != "None" else None
        if not server_id:
            if not self.allow_other_query:
                yield event.plain_result("❌ 请先使用 /绑定 <服务器ID> 绑定服务器")
                return
            args = event.message_str.strip().split()
            if len(args) < 2:
                yield event.plain_result("📋 使用方法: /查询在线 [服务器ID]")
                return
            server_id = args[1]
        result = await self.server_query.query_by_id_simple(server_id)
        yield event.plain_result(result)

    @filter.command("id查询")
    async def query_by_id_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群未授权，无法使用此功能")
            return
        if not self.enable_query:
            yield event.plain_result("❌ 查询功能已关闭")
            return
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("📋 使用方法: /id查询 <服务器ID>")
            return
        server_id = args[1]
        result = await self.server_query.query_by_id_detailed(server_id)
        yield event.plain_result(result)

    @filter.command("查服")
    async def search_server_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群未授权，无法使用此功能")
            return
        if not self.enable_query:
            yield event.plain_result("❌ 查询功能已关闭")
            return
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("📋 使用方法: /查服 <关键词>")
            return
        keyword = ' '.join(args[1:])
        result = await self.server_query.search_servers(keyword)
        yield event.plain_result(result)

    @filter.command("激活卡密")
    async def activate_license_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群未授权，无法使用此功能")
            return
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("📋 使用方法: /激活卡密 <卡密>")
            return
        key = args[1]
        group_id = str(event.message_obj.group_id)
        user_id = str(event.message_obj.sender.user_id) if hasattr(event.message_obj.sender, 'user_id') else ""
        if not self.auth_key:
            yield event.plain_result("❌ 管理员未配置auth_key，卡密功能不可用")
            return
        verify_result = verify_license_key(key, self.auth_key, group_id)
        if not verify_result.get("valid"):
            yield event.plain_result(f"❌ {verify_result.get('error', '卡密无效')}")
            return
        if mark_key_used(key, group_id, user_id):
            days = verify_result.get("days", 1)
            yield event.plain_result(f"✅ 卡密激活成功！授权 {days} 天\n群组: {group_id}")
        else:
            yield event.plain_result("❌ 卡密已被使用或无效")

    @filter.command("生成卡密")
    async def generate_license_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有管理员可以生成卡密")
            return
        if not self.auth_key:
            yield event.plain_result("❌ 管理员未配置auth_key")
            return
        args = event.message_str.strip().split()
        if len(args) < 3:
            yield event.plain_result("📋 使用方法: /生成卡密 <天数> <数量>")
            return
        try:
            days = int(args[1])
            count = int(args[2])
            if count > 100:
                yield event.plain_result("❌ 单次最多生成100个卡密")
                return
        except ValueError:
            yield event.plain_result("❌ 天数和数量必须是数字")
            return
        group_id = args[3] if len(args) > 3 else ""
        keys = []
        for i in range(count):
            key = generate_license_key(self.auth_key, days, group_id, i)
            keys.append(key)
            add_unused_key(key)
        yield event.plain_result(f"✅ 成功生成 {count} 个 {days} 天卡密:\n" + "\n".join(keys[:10]))
        if count > 10:
            yield event.plain_result(f"... 还有 {count - 10} 个卡密，请使用 /查询卡密 查看全部")

    @filter.command("查询卡密")
    async def query_license_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有管理员可以查询卡密")
            return
        unused = get_unused_keys()
        used = get_used_keys()
        unused_count = len(unused)
        used_count = len(used)
        result = f"""📋 卡密状态:
未使用: {unused_count} 个
已使用: {used_count} 个"""
        if unused:
            result += f"\n\n📜 未使用卡密 (前20个):\n" + "\n".join(unused[:20])
        if len(unused) > 20:
            result += f"\n... 还有 {len(unused) - 20} 个"
        yield event.plain_result(result)

    @filter.command("删除全部卡密")
    async def delete_all_license_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有管理员可以删除卡密")
            return
        args = event.message_str.strip().split()
        if len(args) < 2 or args[1] != "confirm":
            yield event.plain_result("⚠️ 确定要删除所有未使用的卡密吗？\n此操作不可恢复！\n请使用: /删除全部卡密 confirm")
            return
        delete_all_keys()
        yield event.plain_result("✅ 已删除所有未使用卡密")

    @filter.command("查看授权")
    async def view_auth_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群未授权，无法使用此功能")
            return
        if not self.enable_settings:
            yield event.plain_result("❌ 设置功能已关闭")
            return
        group_id = str(event.message_obj.group_id)
        auth_info = get_group_auth_info(group_id)
        if not auth_info:
            yield event.plain_result("❌ 该群未授权")
            return
        expire = auth_info.get("expire", 0)
        now = int(time.time())
        remaining = max(0, expire - now)
        days = remaining // 86400
        hours = (remaining % 86400) // 3600
        result = f"""📋 群组授权信息:
群组: {group_id}
剩余时间: {days} 天 {hours} 小时
激活时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(auth_info.get('activated_at', 0)))}
卡密: {auth_info.get('key', 'N/A')}"""
        yield event.plain_result(result)

    @filter.command("群组授权")
    async def authorize_group_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有管理员可以授权群组")
            return
        args = event.message_str.strip().split()
        if len(args) < 3:
            yield event.plain_result("📋 使用方法: /群组授权 <群组ID> <天数>")
            return
        target_group = args[1]
        try:
            days = int(args[2])
        except ValueError:
            yield event.plain_result("❌ 天数必须是数字")
            return
        if authorize_group(target_group, days):
            yield event.plain_result(f"✅ 已授权群组 {target_group}，有效期 {days} 天")
        else:
            yield event.plain_result("❌ 授权失败")

    @filter.command("取消授权")
    async def deauthorize_group_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有管理员可以取消授权")
            return
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("📋 使用方法: /取消授权 <群组ID>")
            return
        target_group = args[1]
        if deauthorize_group(target_group):
            yield event.plain_result(f"✅ 已取消群组 {target_group} 的授权")
        else:
            yield event.plain_result("❌ 群组未授权或取消失败")

    @filter.command("增加时间")
    async def add_time_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有管理员可以增加时间")
            return
        args = event.message_str.strip().split()
        if len(args) < 3:
            yield event.plain_result("📋 使用方法: /增加时间 <群组ID> <天数>")
            return
        target_group = args[1]
        try:
            days = int(args[2])
        except ValueError:
            yield event.plain_result("❌ 天数必须是数字")
            return
        if add_auth_time(target_group, days):
            yield event.plain_result(f"✅ 已为群组 {target_group} 增加 {days} 天")
        else:
            yield event.plain_result("❌ 群组未授权，增加时间失败")

    @filter.command("减少时间")
    async def reduce_time_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有管理员可以减少时间")
            return
        args = event.message_str.strip().split()
        if len(args) < 3:
            yield event.plain_result("📋 使用方法: /减少时间 <群组ID> <天数>")
            return
        target_group = args[1]
        try:
            days = int(args[2])
        except ValueError:
            yield event.plain_result("❌ 天数必须是数字")
            return
        if reduce_auth_time(target_group, days):
            yield event.plain_result(f"✅ 已为群组 {target_group} 减少 {days} 天")
        else:
            yield event.plain_result("❌ 群组未授权，减少时间失败")

    @filter.command("删除已用卡密")
    async def delete_used_keys_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有管理员可以删除已用卡密")
            return
        delete_used_keys()
        yield event.plain_result("✅ 已删除所有已用卡密记录")

    @filter.command("帮助")
    async def help_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self.enable_help:
            return
        help_text = """📋 Joker插件帮助 v2.0

🔍 SCUM服务器查询:
/绑定 <服务器ID> - 绑定服务器ID
/解绑 - 解绑当前服务器
/绑定id <服务器ID> - 绑定服务器ID
/查询在线 - 查询绑定服务器的在线人数
/id查询 <服务器ID> - 查询服务器详细信息
/查服 <关键词> - 搜索SCUM服务器

🔐 卡密授权 (管理员):
/激活卡密 <卡密> - 激活卡密授权
/生成卡密 <天数> <数量> - 生成卡密
/查询卡密 - 查看所有卡密状态
/删除全部卡密 confirm - 删除所有未使用卡密
/删除已用卡密 - 删除已用卡密记录

📊 授权管理 (管理员):
/群组授权 <群组ID> <天数> - 手动授权群组
/取消授权 <群组ID> - 取消群组授权
/增加时间 <群组ID> <天数> - 增加授权时间
/减少时间 <群组ID> <天数> - 减少授权时间
/查看授权 - 查看当前群组授权状态

⚙️ 设置:
/设置 allow_slash_prefix true/false - 允许/禁止斜杠命令"""
        yield event.plain_result(help_text)

    @filter.command("菜单")
    async def menu_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self.enable_help:
            return
        menu_text = """📋 Joker插件功能菜单

🔍 服务器查询: /查询在线 /id查询 /查服
🔗 绑定管理: /绑定 /解绑 /绑定id
🔐 卡密授权: /激活卡密 /生成卡密 /查询卡密
📊 授权管理: /群组授权 /取消授权 /增加时间 /减少时间 /查看授权
⚙️ 系统设置: /设置 /帮助

输入 /帮助 查看详细使用说明"""
        yield event.plain_result(menu_text)

    @filter.command("授权查询")
    @filter.command("查询授权")
    async def auth_query_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有管理员可以查询所有授权")
            return
        all_auth = list_all_authorizations()
        if not all_auth:
            yield event.plain_result("📋 当前没有任何授权记录")
            return
        stats = get_auth_statistics()
        now = int(time.time())
        result = [f"📊 授权统计: 共 {stats['total_groups']} 个群组, {stats['active_groups']} 个有效, {stats['expired_groups']} 个已过期"]
        result.append(f"📜 未使用卡密: {stats['unused_keys_count']} 个")
        result.append(f"✅ 已使用卡密: {stats['used_keys_count']} 个")
        result.append("")
        result.append("📋 授权群组列表:")
        for gid, info in list(all_auth.items())[:20]:
            expire = info.get("expire", 0)
            remaining = max(0, expire - now)
            days = remaining // 86400
            status = "✅" if remaining > 0 else "❌"
            result.append(f"{status} {gid}: {days}天")
        if len(all_auth) > 20:
            result.append(f"... 还有 {len(all_auth) - 20} 个群组")
        yield event.plain_result("\n".join(result))

    @filter.command("设置")
    async def settings_command(self, event: AstrMessageEvent):
        if not self._is_slash_prefix_allowed(event):
            yield event.plain_result("❌ 斜杠命令已关闭")
            return
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群未授权，无法使用此功能")
            return
        if not self.enable_settings:
            yield event.plain_result("❌ 设置功能已关闭")
            return
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("""📋 设置命令:
/设置 allow_slash_prefix true/false - 允许/禁止斜杠命令""")
            return
        setting_key = args[1].lower()
        if len(args) < 3:
            current = get_group_setting(str(event.message_obj.group_id), setting_key, "未设置")
            yield event.plain_result(f"📋 {setting_key}: {current}")
            return
        value = args[2]
        if value.lower() == "true":
            value = True
        elif value.lower() == "false":
            value = False
        set_group_setting(str(event.message_obj.group_id), setting_key, value)
        yield event.plain_result(f"✅ 已设置 {setting_key} = {value}")

    @filter.command("设置 开启斜杠")
    async def enable_slash_command(self, event: AstrMessageEvent):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群未授权，无法使用此功能")
            return
        set_group_setting(str(event.message_obj.group_id), "allow_slash_prefix", True)
        yield event.plain_result("✅ 已开启斜杠命令")

    @filter.command("设置 关闭斜杠")
    async def disable_slash_command(self, event: AstrMessageEvent):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 该群未授权，无法使用此功能")
            return
        set_group_setting(str(event.message_obj.group_id), "allow_slash_prefix", False)
        yield event.plain_result("✅ 已关闭斜杠命令")

    async def terminate(self):
        logger.info("Joker插件已卸载")
