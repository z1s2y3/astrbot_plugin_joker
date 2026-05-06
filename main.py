import httpx
import json
import os
import time
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# 代理列表（免费代理可能随时失效）
PROXIES_LIST = [
    None,
]

def get_plugin_data_path():
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
    data_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_joker"
    data_path.mkdir(parents=True, exist_ok=True)
    return data_path

def get_bindings_file():
    return get_plugin_data_path() / "group_bindings.json"

def load_bindings():
    binding_file = get_bindings_file()
    if binding_file.exists():
        try:
            with open(binding_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_bindings(bindings):
    binding_file = get_bindings_file()
    try:
        with open(binding_file, "w", encoding="utf-8") as f:
            json.dump(bindings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存绑定数据失败: {e}")

def get_auth_file():
    return os.path.join(get_plugin_data_path(), "auth_data.json")

def load_auth_data():
    auth_file = get_auth_file()
    try:
        if os.path.exists(auth_file):
            with open(auth_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"加载授权数据失败: {e}")
    return {"licenses": {}, "groups": {}}

def save_auth_data(data):
    auth_file = get_auth_file()
    try:
        with open(auth_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存授权数据失败: {e}")

def get_settings_file():
    return os.path.join(get_plugin_data_path(), "group_settings.json")

def load_group_settings():
    """加载群组设置"""
    settings_file = get_settings_file()
    try:
        if os.path.exists(settings_file):
            with open(settings_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"加载群组设置失败: {e}")
    return {}

def save_group_settings(settings):
    """保存群组设置"""
    settings_file = get_settings_file()
    try:
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存群组设置失败: {e}")

def get_group_setting(group_id: str, setting_name: str, default_value):
    """获取群组设置，如果没有设置则返回默认值"""
    if not group_id:
        return default_value
    settings = load_group_settings()
    if group_id in settings and setting_name in settings[group_id]:
        return settings[group_id][setting_name]
    return default_value

def set_group_setting(group_id: str, setting_name: str, value):
    """设置群组配置"""
    if not group_id:
        return
    settings = load_group_settings()
    if group_id not in settings:
        settings[group_id] = {}
    settings[group_id][setting_name] = value
    save_group_settings(settings)

def clone_group_settings(source_group_id: str, target_group_id: str, include_binding: bool = False):
    """克隆群组设置（可选择是否包含服务器绑定）"""
    if not source_group_id or not target_group_id:
        return False
    
    settings = load_group_settings()
    if source_group_id not in settings:
        return False
    
    settings[target_group_id] = settings[source_group_id].copy()
    save_group_settings(settings)
    
    if include_binding:
        bindings = load_bindings()
        if source_group_id in bindings:
            bindings[target_group_id] = bindings[source_group_id]
            save_bindings(bindings)
    
    return True

def generate_license_key(auth_key: str, days: int, group_id: str = "") -> str:
    import hashlib
    import time
    timestamp = int(time.time())
    expire = timestamp + (days * 86400)
    data = f"{auth_key}_{timestamp}_{days}_{group_id}"
    signature = hashlib.md5(data.encode()).hexdigest()[:8]
    # 格式: JK + 天数(2位) + 时间戳(10位) + 群组ID标记(1位) + 群组ID(0或更多位) + 签名(8位)
    # 群组ID标记: 0=无限制, 1=有限制
    group_flag = "1" if group_id else "0"
    key = f"JK{days:02d}{timestamp}{group_flag}{group_id}{signature}"
    import sys
    print(f"[DEBUG] 生成卡密: auth_key='{auth_key}', data='{data}', signature={signature}, key={key}", file=sys.stderr)
    return key.upper()

def verify_license_key(key: str, auth_key: str, group_id: str = "") -> dict:
    import hashlib
    import time
    import sys
    key = key.strip().upper()
    print(f"[DEBUG] 开始验证卡密: key={key}, auth_key='{auth_key}', group_id={group_id}", file=sys.stderr)
    if not key.startswith("JK"):
        return {"valid": False, "error": "无效的卡密格式（必须以JK开头）"}
    if len(key) < 16:
        return {"valid": False, "error": "卡密长度不足"}
    try:
        days = int(key[2:4])
        timestamp = int(key[4:14])
        group_flag = key[14]
        print(f"[DEBUG] 解析卡密: days={days}, timestamp={timestamp}, group_flag={group_flag}", file=sys.stderr)
        # 提取群组ID和签名
        if group_flag == "1":
            # 有限制卡密：需要至少8位签名
            if len(key) < 23:  # JK + 2 + 10 + 1 + 0 + 8 = 22
                return {"valid": False, "error": "卡密长度不足"}
            signature = key[-8:]
            encoded_group_id = key[15:-8]
            expected_data = f"{auth_key}_{timestamp}_{days}_{encoded_group_id}"
            # 验证群组ID
            if group_id and str(group_id) != encoded_group_id:
                return {"valid": False, "error": "卡密与当前群组不匹配"}
        else:
            # 无限制卡密
            if len(key) < 22:  # JK + 2 + 10 + 1 + 8 = 22
                return {"valid": False, "error": "卡密长度不足"}
            signature = key[-8:]  # 统一使用末尾8位
            encoded_group_id = ""
            expected_data = f"{auth_key}_{timestamp}_{days}_"
        
        expected_signature = hashlib.md5(expected_data.encode()).hexdigest()[:8]
        print(f"[DEBUG] 签名验证: signature={signature}, expected_signature={expected_signature}, expected_data='{expected_data}'", file=sys.stderr)
        if signature.lower() != expected_signature.lower():
            return {"valid": False, "error": f"卡密验证失败（签名不匹配）\n期望签名: {expected_signature}\n实际签名: {signature}"}
        expire = timestamp + (days * 86400)
        if time.time() > expire:
            return {"valid": False, "error": "卡密已过期"}
        return {"valid": True, "days": days, "expire": expire, "group_id": encoded_group_id}
    except ValueError as e:
        return {"valid": False, "error": f"卡密格式错误: {str(e)}"}
    except Exception as e:
        return {"valid": False, "error": f"验证异常: {str(e)}"}

@register("astrbot_plugin_joker", "Joker", "SCUM服务器查询插件", "1.3.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.base_url = "https://api.battlemetrics.com"
        self.proxy_index = 0
        self.config = config or {}
        self.whitelist_groups = self.config.get("whitelist_groups", [])
        self.admin_groups = self.config.get("admin_groups", [])
        self.default_enabled = self.config.get("default_enabled", True)
        self.allow_other_query = self.config.get("allow_other_query", True)
        self.owner_ids = self.config.get("owner_ids", [])
        self.owner_ignore_auth = self.config.get("owner_ignore_auth", True)
        self.owner_ignore_binding = self.config.get("owner_ignore_binding", True)
        self.enable_auth = self.config.get("enable_auth", False)

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        """检查是否为所有者"""
        sender = event.message_obj.sender
        sender_id = sender.user_id if hasattr(sender, 'user_id') else ""
        return str(sender_id) in [str(oid) for oid in self.owner_ids]

    def _is_authorized(self, event: AstrMessageEvent) -> bool:
        """检查群组是否已授权"""
        if not self.enable_auth:
            return True
        group_id = event.message_obj.group_id
        if not group_id:
            return False
        if self._is_owner(event) and self.owner_ignore_auth:
            return True
        auth_data = load_auth_data()
        if group_id not in auth_data["groups"]:
            return False
        expire = auth_data["groups"][group_id].get("expire", 0)
        return expire > time.time()

    def _is_in_whitelist(self, group_id: str) -> bool:
        """检查群组是否在白名单中（白名单群组不需要绑定ID即可使用）"""
        if not group_id:
            return False
        if not self.whitelist_groups:
            return False
        return group_id in self.whitelist_groups

    def _is_admin_group(self, group_id: str) -> bool:
        """检查群组是否需要管理员权限"""
        if not self.admin_groups:
            return True
        return group_id in self.admin_groups

    async def _get_proxy_client(self):
        """获取带代理的HTTP客户端（自动轮换代理）"""
        # 尝试使用当前代理，如果失败则轮换到下一个
        for _ in range(len(PROXIES_LIST)):
            proxy = PROXIES_LIST[self.proxy_index]
            self.proxy_index = (self.proxy_index + 1) % len(PROXIES_LIST)
            try:
                client = httpx.AsyncClient(
                    timeout=20.0,
                    proxies=proxy,
                    follow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    }
                )
                return client
            except Exception:
                continue
        return httpx.AsyncClient(timeout=20.0)

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    def _is_admin_or_owner(self, event: AstrMessageEvent) -> bool:
        """检查用户是否为管理员、群主或全局主人"""
        sender = event.message_obj.sender
        sender_id = sender.user_id if hasattr(sender, 'user_id') else ""

        if str(sender_id) in [str(oid) for oid in self.owner_ids]:
            return True

        if event.role == "admin":
            return True

        group = event.message_obj.group
        if group:
            if group.group_owner and str(group.group_owner) == str(sender_id):
                return True
            if group.group_admins and str(sender_id) in [str(a) for a in group.group_admins]:
                return True

        return False

    @filter.command("绑定id")
    async def bind_server_id(self, event: AstrMessageEvent, server_id: str = "") -> None:
        """绑定群组服务器ID（仅管理员/群主可用）

        使用方法：
        /绑定id <服务器ID> - 绑定服务器ID到当前群组
        """
        async for result in self._bind_server(event, server_id):
            yield result

    @filter.command("解绑")
    async def unbind_server_id(self, event: AstrMessageEvent) -> None:
        """解除群组服务器ID绑定（仅管理员/群主可用）

        使用方法：
        /解绑 - 解除当前群组的服务器ID绑定
        """
        async for result in self._unbind_server(event):
            yield result

    async def _bind_server(self, event: AstrMessageEvent, server_id: str = "") -> None:
        """绑定服务器ID的通用处理逻辑"""
        if not self._is_admin_or_owner(event):
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
            if old_server_id == server_id:
                yield event.plain_result(f"⚠️ 该群组已绑定服务器ID：{old_server_id}\n\n如需更换服务器ID，请先使用 解绑 解除当前绑定。")
            else:
                yield event.plain_result(f"⚠️ 该群组已绑定服务器ID：{old_server_id}\n如需更换为 {server_id}，请先使用 解绑 解除当前绑定。")
            return

        bindings[group_id] = server_id
        save_bindings(bindings)

        yield event.plain_result(f"✅ 成功绑定服务器ID：{server_id}\n\n现在群组成员可以直接使用 查询在线 来查询该服务器状态。")

    async def _unbind_server(self, event: AstrMessageEvent) -> None:
        """解绑服务器ID的通用处理逻辑"""
        if not self._is_admin_or_owner(event):
            yield event.plain_result("❌ 只有管理员或群主才能执行此操作。")
            return

        if not self._is_admin_group(event.message_obj.group_id):
            yield event.plain_result("❌ 该群组不允许使用解绑功能。")
            return

        group_id = event.message_obj.group_id

        if not group_id:
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        bindings = load_bindings()
        if group_id in bindings:
            del bindings[group_id]
            save_bindings(bindings)
            yield event.plain_result("✅ 已解除该群组的服务器ID绑定。")
        else:
            yield event.plain_result("❌ 该群组未绑定任何服务器ID。")

    @filter.regex(r"^(?!/)id查询")
    async def query_server_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的查询命令"""
        # 如果消息以 / 开头，不处理（交给带前缀的命令处理）
        if event.message_str.strip().startswith("/"):
            return
            
        import re
        match = re.search(r"id查询\s*(.*)", event.message_str.strip())
        if not match:
            return
        message_str = match.group(1).strip()
        parts = message_str.split(maxsplit=1)
        action = parts[0] if parts else ""
        keyword = parts[1] if len(parts) > 1 else ""

        async for result in self._handle_query_command(event, action, keyword):
            yield result

    @filter.regex(r"^(?!/)查询在线\s*$")
    async def query_online_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的查询在线命令（绑定后使用）"""
        # 如果消息以 / 开头，不处理（交给带前缀的命令处理）
        if event.message_str.strip().startswith("/"):
            return
        
        import sys
        group_id = event.message_obj.group_id
        bindings = load_bindings()
        print(f"[DEBUG] query_online_no_prefix: message='{event.message_str}', group_id={group_id}, bindings={bindings}", file=sys.stderr)

        is_owner = self._is_owner(event)
        
        # 检查授权
        if not self._is_authorized(event):
            if is_owner and self.owner_ignore_auth:
                pass  # 主人忽略授权限制
            else:
                yield event.plain_result("❌ 该群组尚未授权，请联系管理员进行授权。")
                return
        
        # 检查绑定
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        if group_id not in bindings:
            if is_owner and self.owner_ignore_binding:
                pass  # 主人忽略绑定限制
            else:
                yield event.plain_result("❌ 该群组未绑定服务器ID，请管理员使用 /绑定id <服务器ID> 进行绑定。")
                return
        
        if group_id and group_id in bindings:
            server_id = bindings[group_id]
            print(f"[DEBUG] 查询: server_id={server_id}", file=sys.stderr)
            result = await self._query_by_id_simple(server_id)
            yield event.plain_result(result)
        else:
            if is_owner:
                yield event.plain_result("❌ 该群组未绑定服务器ID，请联系管理员进行绑定。")

    @filter.regex(r"^(?!/)绑定id")
    async def bind_server_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的绑定命令"""
        import re
        if event.message_str.strip().startswith("/"):
            return
        match = re.search(r"绑定id\s+(.+)", event.message_str.strip())
        if not match:
            return
        server_id = match.group(1).strip()
        async for result in self._bind_server(event, server_id):
            yield result

    @filter.regex(r"^(?!/)解绑")
    async def unbind_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的解绑命令"""
        if event.message_str.strip().startswith("/"):
            return
        async for result in self._unbind_server(event):
            yield result

    @filter.regex(r"^(?!/)生成卡密")
    async def generate_license_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的生成卡密命令"""
        import re
        if event.message_str.strip().startswith("/"):
            return
        match = re.search(r"生成卡密\s+(\d+)(?:\s+(\d+))?", event.message_str.strip())
        if not match:
            return
        days = match.group(1)
        group_id = match.group(2) if match.group(2) else ""
        async for result in self._generate_license(event, days, group_id):
            yield result

    @filter.regex(r"^(?!/)激活卡密")
    async def activate_license_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的激活卡密命令"""
        import re
        if event.message_str.strip().startswith("/"):
            return
        match = re.search(r"激活卡密\s+(.+)", event.message_str.strip())
        if not match:
            return
        key = match.group(1).strip()
        async for result in self._activate_license(event, key):
            yield result

    @filter.regex(r"^(?!/)授权查询")
    async def query_auth_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的授权查询命令"""
        if event.message_str.strip().startswith("/"):
            return
        async for result in self._query_auth(event):
            yield result

    @filter.regex(r"^(?!/)群组授权")
    async def group_license_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的群组授权命令"""
        import re
        if event.message_str.strip().startswith("/"):
            return
        match = re.search(r"群组授权\s+(\d+)\s+(\d+)", event.message_str.strip())
        if not match:
            return
        group_id = match.group(1)
        days = match.group(2)
        async for result in self._group_license(event, group_id, days):
            yield result

    @filter.regex(r"^(?!/)查看设置")
    async def show_settings_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的查看设置命令"""
        if event.message_str.strip().startswith("/"):
            return
        async for result in self._show_settings(event):
            yield result

    @filter.regex(r"^(?!/)设置\s")
    async def set_settings_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的设置命令"""
        import re
        if event.message_str.strip().startswith("/"):
            return
        # 匹配：设置 重置
        if re.search(r"^设置\s+重置", event.message_str.strip(), re.IGNORECASE):
            async for result in self._reset_group_settings(event):
                yield result
            return
        # 匹配：设置 允许查询其他服务器 true
        match = re.search(r"^设置\s+(?:允许查询其他服务器|allow_other_query|允许查询)\s+(\S+)", event.message_str.strip(), re.IGNORECASE)
        if match:
            value = match.group(1)
            async for result in self._set_allow_other_query(event, value):
                yield result
            return

    @filter.regex(r"^(?!/)克隆设置\s")
    async def clone_settings_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的克隆设置命令"""
        import re
        if event.message_str.strip().startswith("/"):
            return
        # 匹配：克隆设置 <源> [目标] [是否包含绑定]
        match = re.search(r"^克隆设置\s+(\d+)(?:\s+(\d+))?(?:\s+(\S+))?", event.message_str.strip())
        if not match:
            return
        source_group_id = match.group(1)
        target_group_id = match.group(2) if match.group(2) else ""
        include_binding_str = match.group(3) if match.group(3) else ""
        include_binding_bool = include_binding_str.lower() in ["true", "1", "是", "包含", "包含绑定"]
        async for result in self._clone_settings(event, source_group_id, target_group_id, include_binding_bool):
            yield result

    @filter.regex(r"^(?!/)scum帮助\s*$")
    async def show_help_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的帮助命令"""
        if event.message_str.strip().startswith("/"):
            return
        async for result in self._show_help(event):
            yield result

    @filter.regex(r"^(?!/)取消授权")
    async def revoke_license_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的取消授权命令"""
        import re
        if event.message_str.strip().startswith("/"):
            return
        match = re.search(r"取消授权\s*(\d*)", event.message_str.strip())
        if not match:
            return
        group_id = match.group(1) if match.group(1) else ""
        async for result in self._revoke_license(event, group_id):
            yield result

    @filter.regex(r"^(?!/)增加时间\s")
    async def add_time_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的增加时间命令"""
        import re
        if event.message_str.strip().startswith("/"):
            return
        match = re.search(r"增加时间\s+(\d+)(?:\s+(\d+))?", event.message_str.strip())
        if not match:
            return
        group_id = match.group(1)
        days = match.group(2) if match.group(2) else ""
        async for result in self._add_time(event, group_id, days):
            yield result

    @filter.regex(r"^(?!/)减少时间\s")
    async def reduce_time_no_prefix(self, event: AstrMessageEvent) -> None:
        """不带前缀的减少时间命令"""
        import re
        if event.message_str.strip().startswith("/"):
            return
        match = re.search(r"减少时间\s+(\d+)(?:\s+(\d+))?", event.message_str.strip())
        if not match:
            return
        group_id = match.group(1)
        days = match.group(2) if match.group(2) else ""
        async for result in self._reduce_time(event, group_id, days):
            yield result

    @filter.command("激活卡密")
    async def activate_license(self, event: AstrMessageEvent, key: str = "") -> None:
        """激活卡密授权

        使用方法：
        /激活卡密 <卡密> - 激活卡密授权当前群组
        """
        async for result in self._activate_license(event, key):
            yield result

    @filter.command("授权查询")
    async def query_auth(self, event: AstrMessageEvent) -> None:
        """查询当前群组授权状态"""
        async for result in self._query_auth(event):
            yield result

    @filter.command("生成卡密")
    async def generate_license(self, event: AstrMessageEvent, days: str = "", group_id: str = "") -> None:
        """生成卡密（仅主人可用）

        使用方法：
        /生成卡密 <天数> [群组ID] - 生成指定天数的卡密
        """
        async for result in self._generate_license(event, days, group_id):
            yield result

    async def _activate_license(self, event: AstrMessageEvent, key: str) -> None:
        """激活卡密"""
        if not self.config.get("enable_auth", False):
            yield event.plain_result("❌ 授权系统未启用。")
            return

        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        auth_key = self.config.get("auth_key", "")
        if not auth_key:
            yield event.plain_result("❌ 管理密钥未配置，请联系管理员。")
            return
            
        logger.info(f"激活卡密: key={key}, auth_key={auth_key[:10]}..., group_id={group_id}")
        result = verify_license_key(key, auth_key, str(group_id))

        if not result["valid"]:
            yield event.plain_result(f"❌ 卡密激活失败：{result['error']}")
            return

        auth_data = load_auth_data()
        expire = result["expire"]

        if group_id not in auth_data["groups"]:
            auth_data["groups"][group_id] = {"expire": expire, "activated_at": int(time.time())}
        else:
            current_expire = auth_data["groups"][group_id].get("expire", 0)
            if current_expire > time.time():
                auth_data["groups"][group_id]["expire"] = current_expire + (result["days"] * 86400)
            else:
                auth_data["groups"][group_id]["expire"] = expire

        auth_data["groups"][group_id]["last_key"] = key
        save_auth_data(auth_data)

        from datetime import datetime
        expire_date = datetime.fromtimestamp(expire).strftime("%Y-%m-%d %H:%M:%S")
        yield event.plain_result(f"✅ 卡密激活成功！\n授权到期时间：{expire_date}")

    async def _query_auth(self, event: AstrMessageEvent) -> None:
        """查询授权状态"""
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        auth_data = load_auth_data()

        if group_id not in auth_data["groups"]:
            yield event.plain_result("❌ 当前群组未授权。")
            return

        group_auth = auth_data["groups"][group_id]
        expire = group_auth.get("expire", 0)

        from datetime import datetime
        if expire > time.time():
            expire_date = datetime.fromtimestamp(expire).strftime("%Y年%m月%d日 %H时%M分")
            remaining_seconds = int(expire - time.time())
            days = remaining_seconds // 86400
            hours = (remaining_seconds % 86400) // 3600
            minutes = (remaining_seconds % 3600) // 60
            remaining_str = f"{days} 天 {hours} 小时 {minutes} 分钟"
            yield event.plain_result(f"✅ 当前群组已授权\n到期时间：{expire_date}\n剩余时间：{remaining_str}")
        else:
            yield event.plain_result(f"❌ 当前群组授权已过期（{datetime.fromtimestamp(expire).strftime('%Y年%m月%d日 %H时%M分')}）")

    async def _generate_license(self, event: AstrMessageEvent, days: str, group_id: str) -> None:
        """生成卡密（仅主人可用）"""
        if not self._is_admin_or_owner(event):
            yield event.plain_result("❌ 只有管理员或主人才能生成卡密。")
            return

        if not days or not days.isdigit():
            yield event.plain_result("❌ 请提供有效的天数（纯数字）。\n例如：/生成卡密 30")
            return

        days_int = int(days)
        if days_int <= 0 or days_int > 365:
            yield event.plain_result("❌ 天数必须在 1-365 之间。")
            return

        auth_key = self.config.get("auth_key", "")
        key = generate_license_key(auth_key, days_int, group_id)

        msg = f"✅ 卡密生成成功！\n\n天数：{days_int} 天"
        if group_id:
            msg += f"\n限定群组：{group_id}"
        else:
            msg += "\n限定群组：无（可在任意群组激活）"
        msg += f"\n卡密：\n{key}"
        yield event.plain_result(msg)

    async def _group_license(self, event: AstrMessageEvent, group_id: str, days: str) -> None:
        """直接为群组授权（仅主人可用）"""
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能执行此操作。")
            return

        if not self.config.get("enable_auth", False):
            yield event.plain_result("❌ 授权系统未启用。")
            return

        if not group_id or not group_id.isdigit():
            yield event.plain_result("❌ 请提供有效的群组ID（纯数字）。\n例如：/群组授权 123456789 30")
            return

        if not days or not days.isdigit():
            yield event.plain_result("❌ 请提供有效的天数（纯数字）。\n例如：/群组授权 123456789 30")
            return

        days_int = int(days)
        if days_int <= 0 or days_int > 365:
            yield event.plain_result("❌ 天数必须在 1-365 之间。")
            return

        auth_data = load_auth_data()
        expire = int(time.time()) + (days_int * 86400)

        if group_id in auth_data["groups"]:
            current_expire = auth_data["groups"][group_id].get("expire", 0)
            if current_expire > time.time():
                expire = current_expire + (days_int * 86400)

        auth_data["groups"][group_id] = {"expire": expire, "activated_at": int(time.time()), "last_key": "direct"}
        save_auth_data(auth_data)

        from datetime import datetime
        expire_date = datetime.fromtimestamp(expire).strftime("%Y年%m月%d日 %H时%M分")
        yield event.plain_result(f"✅ 群组授权成功！\n\n群组ID：{group_id}\n授权天数：{days_int} 天\n到期时间：{expire_date}")

    async def _revoke_license(self, event: AstrMessageEvent, group_id: str = "") -> None:
        """取消群组授权（仅主人可用）"""
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能执行此操作。")
            return

        if not self.config.get("enable_auth", False):
            yield event.plain_result("❌ 授权系统未启用。")
            return

        target_group_id = group_id if group_id else str(event.message_obj.group_id)
        
        if not target_group_id or not target_group_id.isdigit():
            yield event.plain_result("❌ 请提供有效的群组ID（纯数字）。\n例如：/取消授权 123456789")
            return

        auth_data = load_auth_data()
        
        if target_group_id not in auth_data["groups"]:
            yield event.plain_result("❌ 该群组尚未授权。")
            return

        del auth_data["groups"][target_group_id]
        save_auth_data(auth_data)

        yield event.plain_result(f"✅ 已取消群组授权！\n\n群组ID：{target_group_id}")

    async def _add_time(self, event: AstrMessageEvent, group_id: str, days: str) -> None:
        """增加授权时间（仅主人可用）"""
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能执行此操作。")
            return

        if not self.config.get("enable_auth", False):
            yield event.plain_result("❌ 授权系统未启用。")
            return

        if not group_id or not group_id.isdigit():
            yield event.plain_result("❌ 请提供有效的群组ID（纯数字）。\n例如：/增加时间 123456789 30")
            return

        if not days or not days.isdigit():
            yield event.plain_result("❌ 请提供有效的天数（纯数字）。\n例如：/增加时间 123456789 30")
            return

        days_int = int(days)
        if days_int <= 0 or days_int > 365:
            yield event.plain_result("❌ 天数必须在 1-365 之间。")
            return

        auth_data = load_auth_data()
        
        if group_id not in auth_data["groups"]:
            yield event.plain_result("❌ 该群组尚未授权。")
            return

        current_expire = auth_data["groups"][group_id].get("expire", 0)
        new_expire = current_expire + (days_int * 86400)
        auth_data["groups"][group_id]["expire"] = new_expire
        save_auth_data(auth_data)

        from datetime import datetime
        expire_date = datetime.fromtimestamp(new_expire).strftime("%Y年%m月%d日 %H时%M分")
        yield event.plain_result(f"✅ 增加授权时间成功！\n\n群组ID：{group_id}\n增加天数：{days_int} 天\n新到期时间：{expire_date}")

    async def _reduce_time(self, event: AstrMessageEvent, group_id: str, days: str) -> None:
        """减少授权时间（仅主人可用）"""
        if not self._is_owner(event):
            yield event.plain_result("❌ 只有主人才能执行此操作。")
            return

        if not self.config.get("enable_auth", False):
            yield event.plain_result("❌ 授权系统未启用。")
            return

        if not group_id or not group_id.isdigit():
            yield event.plain_result("❌ 请提供有效的群组ID（纯数字）。\n例如：/减少时间 123456789 30")
            return

        if not days or not days.isdigit():
            yield event.plain_result("❌ 请提供有效的天数（纯数字）。\n例如：/减少时间 123456789 30")
            return

        days_int = int(days)
        if days_int <= 0 or days_int > 365:
            yield event.plain_result("❌ 天数必须在 1-365 之间。")
            return

        auth_data = load_auth_data()
        
        if group_id not in auth_data["groups"]:
            yield event.plain_result("❌ 该群组尚未授权。")
            return

        current_expire = auth_data["groups"][group_id].get("expire", 0)
        new_expire = current_expire - (days_int * 86400)
        
        if new_expire < time.time():
            yield event.plain_result("❌ 减少后的到期时间已在当前时间之前，取消此操作。")
            return

        auth_data["groups"][group_id]["expire"] = new_expire
        save_auth_data(auth_data)

        from datetime import datetime
        expire_date = datetime.fromtimestamp(new_expire).strftime("%Y年%m月%d日 %H时%M分")
        yield event.plain_result(f"✅ 减少授权时间成功！\n\n群组ID：{group_id}\n减少天数：{days_int} 天\n新到期时间：{expire_date}")

    @filter.command("群组授权")
    async def group_license(self, event: AstrMessageEvent, group_id: str = "", days: str = "") -> None:
        """直接为群组授权（仅主人可用）

        使用方法：
        /群组授权 <群组ID> <天数> - 直接为指定群组授权
        """
        async for result in self._group_license(event, group_id, days):
            yield result

    @filter.command("取消授权")
    async def revoke_license(self, event: AstrMessageEvent, group_id: str = "") -> None:
        """取消群组授权（仅主人可用）

        使用方法：
        /取消授权 [群组ID] - 取消当前或指定群组的授权
        """
        async for result in self._revoke_license(event, group_id):
            yield result

    @filter.command("增加时间")
    async def add_time(self, event: AstrMessageEvent, group_id: str = "", days: str = "") -> None:
        """增加授权时间（仅主人可用）

        使用方法：
        /增加时间 <群组ID> <天数> - 为指定群组增加授权时间
        """
        async for result in self._add_time(event, group_id, days):
            yield result

    @filter.command("减少时间")
    async def reduce_time(self, event: AstrMessageEvent, group_id: str = "", days: str = "") -> None:
        """减少授权时间（仅主人可用）

        使用方法：
        /减少时间 <群组ID> <天数> - 为指定群组减少授权时间
        """
        async for result in self._reduce_time(event, group_id, days):
            yield result

    async def _show_settings(self, event: AstrMessageEvent) -> None:
        """显示当前群组设置"""
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        settings = load_group_settings()
        group_settings = settings.get(group_id, {})

        msg = "📋 当前群组设置：\n"
        msg += f"  允许查询其他服务器: {get_group_setting(group_id, 'allow_other_query', self.allow_other_query)}\n"
        msg += f"\n当前设置来源: {'群组自定义' if group_settings else '全局默认'}"
        yield event.plain_result(msg)

    async def _set_allow_other_query(self, event: AstrMessageEvent, enabled: str) -> None:
        """设置是否允许查询其他服务器"""
        if not self._is_admin_or_owner(event):
            yield event.plain_result("❌ 只有管理员或主人才能修改设置。")
            return

        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        if enabled.lower() in ["true", "1", "是", "启用", "开启"]:
            set_group_setting(group_id, "allow_other_query", True)
            yield event.plain_result("✅ 已启用：允许查询其他服务器")
        elif enabled.lower() in ["false", "0", "否", "禁用", "关闭"]:
            set_group_setting(group_id, "allow_other_query", False)
            yield event.plain_result("✅ 已禁用：不允许查询其他服务器")
        else:
            yield event.plain_result("❌ 参数错误，请使用 true/是/启用 或 false/否/禁用")

    async def _reset_group_settings(self, event: AstrMessageEvent) -> None:
        """重置群组设置为全局默认"""
        if not self._is_admin_or_owner(event):
            yield event.plain_result("❌ 只有管理员或主人才能修改设置。")
            return

        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return

        settings = load_group_settings()
        if group_id in settings:
            del settings[group_id]
            save_group_settings(settings)
            yield event.plain_result("✅ 已重置群组设置为全局默认")
        else:
            yield event.plain_result("该群组没有自定义设置，无需重置")

    async def _clone_settings(self, event: AstrMessageEvent, source_group_id: str, target_group_id: str = "", include_binding: bool = False) -> None:
        """克隆群组设置（支持是否包含服务器绑定）"""
        # 检查权限：主人或管理员都可以使用
        if not (self._is_owner(event) or self._is_admin(event)):
            yield event.plain_result("❌ 只有主人或管理员才能克隆设置。")
            return

        if not source_group_id:
            help_text = """❌ 请提供源群组ID
使用方法：
/克隆设置 <源群组ID> [目标群组ID] [是否包含绑定]
示例：
  /克隆设置 123456789                - 克隆 123456789 的设置到当前群组
  /克隆设置 123456789 987654321      - 克隆到指定群组
  /克隆设置 123456789 987654321 true - 克隆设置和服务器绑定"""
            yield event.plain_result(help_text)
            return

        if not target_group_id:
            target_group_id = event.message_obj.group_id

        if not target_group_id:
            yield event.plain_result("❌ 无法确定目标群组，请提供目标群组ID")
            return

        # 加载源群组信息
        settings = load_group_settings()
        bindings = load_bindings()
        
        source_has_settings = source_group_id in settings
        source_has_binding = include_binding and (source_group_id in bindings)
        
        if not source_has_settings and not (include_binding and source_has_binding):
            yield event.plain_result(f"❌ 源群组 {source_group_id} 没有可克隆的内容（既没有自定义设置也没有绑定）")
            return

        success = clone_group_settings(source_group_id, target_group_id, include_binding)
        if success:
            result_parts = [f"✅ 已成功克隆！"]
            result_parts.append(f"源群组：{source_group_id}")
            result_parts.append(f"目标群组：{target_group_id}")
            if source_has_settings:
                result_parts.append(f"✓ 已克隆群组设置")
            if source_has_binding:
                server_id = bindings.get(source_group_id, "")
                result_parts.append(f"✓ 已克隆服务器绑定 (ID: {server_id})")
            yield event.plain_result("\n".join(result_parts))
        else:
            yield event.plain_result(f"❌ 克隆失败，请确认源群组 {source_group_id} 有自定义设置")

    @filter.command("查看设置")
    async def show_settings(self, event: AstrMessageEvent) -> None:
        """查看当前群组设置"""
        async for result in self._show_settings(event):
            yield result

    @filter.command("设置")
    async def set_settings(self, event: AstrMessageEvent, setting_name: str = "", value: str = "") -> None:
        """修改群组设置

        使用方法：
        /设置 允许查询其他服务器 <true/false> - 设置是否允许绑定后查询其他服务器
        /设置 重置 - 重置为全局默认设置
        """
        if setting_name == "重置":
            async for result in self._reset_group_settings(event):
                yield result
            return

        if setting_name in ["允许查询其他服务器", "allow_other_query", "允许查询"]:
            async for result in self._set_allow_other_query(event, value):
                yield result
            return

        yield event.plain_result("❌ 未知设置项\n可用设置：\n  - 允许查询其他服务器 <true/false>\n  - 重置\n使用示例：/设置 允许查询其他服务器 true")

    @filter.command("克隆设置")
    async def clone_settings(self, event: AstrMessageEvent, source_group_id: str = "", target_group_id: str = "", include_binding: str = "") -> None:
        """克隆群组设置（主人/管理员可用）

        使用方法：
        /克隆设置 <源群组ID> [目标群组ID] [是否包含绑定] - 从源群组复制设置
        如果在目标群组中发送命令，可以省略目标群组ID
        是否包含绑定: true/是 表示同时克隆服务器绑定
        """
        include_binding_bool = include_binding.lower() in ["true", "1", "是", "包含", "包含绑定"]
        async for result in self._clone_settings(event, source_group_id, target_group_id, include_binding_bool):
            yield result

    async def _show_help(self, event: AstrMessageEvent) -> None:
        """显示帮助信息"""
        help_msg = """🎮 SCUM服务器查询插件帮助

📋 普通命令：
  id查询 <关键词/ID>  - 查询服务器
  查询在线           - 查询绑定的服务器（简化格式）
  查看设置           - 查看当前群组设置
  授权查询           - 查询授权状态
  scum帮助           - 显示此帮助

🔧 管理命令（管理员/群主）：
  绑定id <服务器ID>  - 绑定服务器
  解绑              - 解绑服务器
  激活卡密 <卡密>    - 激活卡密
  设置 <选项> <值>   - 修改群组设置
  设置 重置         - 重置为全局默认

👑 主人/管理员命令：
  生成卡密 <天数> [群组ID]  - 生成卡密
  群组授权 <群组ID> <天数> - 直接授权群组
  取消授权 [群组ID]        - 取消群组授权
  增加时间 <群组ID> <天数>  - 增加授权时间
  减少时间 <群组ID> <天数>  - 减少授权时间
  克隆设置 <源> [目标] [true] - 克隆设置（true同时克隆绑定）

⚙️ 设置说明：
  - 允许查询其他服务器: 绑定后是否可以查询其他ID的服务器
  - 使用全局设置或群组自定义设置
  - 详细文档请查看 README.md
"""
        yield event.plain_result(help_msg)

    @filter.command("scum帮助")
    async def show_help(self, event: AstrMessageEvent) -> None:
        """显示SCUM插件帮助"""
        async for result in self._show_help(event):
            yield result

    @filter.command("id查询")
    async def query_server(self, event: AstrMessageEvent, action: str = "", keyword: str = "") -> None:
        """查询 Battlemetrics SCUM 服务器状态

        使用方法：
        /id查询 - 返回SCUM服务器排名前10（群组已绑定时显示简化格式）
        /id查询 <8位以上数字> - 查询指定ID的服务器
        /id查询 <短数字/关键词> - 搜索SCUM服务器
        /id查询 search <关键词> - 搜索SCUM服务器
        """
        async for result in self._handle_query_command(event, action, keyword):
            yield result

    @filter.command("查询在线")
    async def query_online(self, event: AstrMessageEvent) -> None:
        """查询已绑定服务器的在线状态（简化格式）"""
        import sys
        group_id = event.message_obj.group_id
        bindings = load_bindings()
        print(f"[DEBUG] query_online (/查询在线): message='{event.message_str}', group_id={group_id}, bindings={bindings}", file=sys.stderr)

        is_owner = self._is_owner(event)
        
        # 检查授权
        if not self._is_authorized(event):
            if is_owner and self.owner_ignore_auth:
                pass  # 主人忽略授权限制
            else:
                yield event.plain_result("❌ 该群组尚未授权，请联系管理员进行授权。")
                return
        
        # 检查绑定
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群组中使用。")
            return
        
        if group_id not in bindings:
            if is_owner and self.owner_ignore_binding:
                pass  # 主人忽略绑定限制
            else:
                yield event.plain_result("❌ 该群组未绑定服务器ID，请管理员使用 /绑定id <服务器ID> 进行绑定。")
                return
        
        if group_id and group_id in bindings:
            server_id = bindings[group_id]
            print(f"[DEBUG] /查询在线 查询: server_id={server_id}", file=sys.stderr)
            result = await self._query_by_id_simple(server_id)
            yield event.plain_result(result)
        else:
            if is_owner:
                yield event.plain_result("❌ 该群组未绑定服务器ID。")

    async def _handle_query_command(self, event: AstrMessageEvent, action: str = "", keyword: str = "") -> None:
        """处理查询命令（带/不带前缀通用）"""
        group_id = event.message_obj.group_id
        bindings = load_bindings()

        is_owner = self._is_owner(event)
        
        if not self._is_in_whitelist(group_id):
            # 检查授权
            if not self._is_authorized(event):
                if is_owner and self.owner_ignore_auth:
                    pass  # 主人忽略授权限制
                else:
                    yield event.plain_result("❌ 该群组尚未授权，请联系管理员进行授权。")
                    return
            
            # 检查绑定
            if group_id and group_id not in bindings:
                if is_owner and self.owner_ignore_binding:
                    pass  # 主人忽略绑定限制
                else:
                    yield event.plain_result("❌ 该群组未绑定服务器ID，请管理员使用 /绑定id <服务器ID> 进行绑定。")
                    return

        if group_id and group_id in bindings:
            if not action:
                server_id = bindings[group_id]
                result = await self._query_by_id_simple(server_id)
                yield event.plain_result(result)
                return

            # 使用群组设置或全局设置
            allow_other = get_group_setting(group_id, "allow_other_query", self.allow_other_query)
            if not allow_other:
                if is_owner and self.owner_ignore_binding:
                    pass  # 主人忽略绑定限制
                else:
                    yield event.plain_result(f"❌ 该群组已绑定服务器ID {bindings[group_id]}，不允许查询其他服务器。")
                    return

        async for result in self._handle_query(event, action, keyword):
            yield result

    async def _handle_query(self, event: AstrMessageEvent, action: str = "", keyword: str = "") -> None:
        """处理查询逻辑"""
        action = action.strip()
        keyword = keyword.strip()
        
        if not action:
            # 不带参数时返回服务器排名（与网站一致）
            result = await self._get_server_ranking()
            yield event.plain_result(result)
            return
        
        if action.lower() == "search":
            # 搜索模式
            if not keyword:
                yield event.plain_result("请提供搜索关键词。例如：id查询 search China")
                return
            result = await self._search_servers(keyword)
            yield event.plain_result(result)
        elif action.isdigit() and len(action) >= 8:
            # 8位或以上数字当作服务器ID查询
            result = await self._query_by_id(action)
            yield event.plain_result(result)
        elif action.isdigit() and len(action) < 8:
            # 短数字自动触发搜索
            search_keyword = action
            if keyword:
                search_keyword += " " + keyword
            result = await self._search_servers(search_keyword)
            yield event.plain_result(result)
        else:
            # 默认当作搜索关键词
            search_keyword = action
            if keyword:
                search_keyword += " " + keyword
            result = await self._search_servers(search_keyword)
            yield event.plain_result(result)

    async def _reverse_geocode(self, lat: float, lon: float) -> str:
        """将坐标转换为地区名称"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                headers = {
                    "User-Agent": "AstrBot-Joker-Plugin/1.0",
                }
                response = await client.get(
                    f"https://nominatim.openstreetmap.org/reverse",
                    params={
                        "format": "json",
                        "lat": lat,
                        "lon": lon,
                        "addressdetails": 1,
                        "accept-language": "zh-CN",
                    },
                    headers=headers,
                )
                if response.status_code == 200:
                    data = response.json()
                    address = data.get("address", {})
                    # 优先获取中文地区名称
                    city = address.get("city") or address.get("town") or address.get("village") or address.get("county") or address.get("suburb") or ""
                    province = address.get("state") or address.get("province") or ""
                    country = address.get("country") or ""
                    
                    result = []
                    if province:
                        result.append(province)
                    if city:
                        result.append(city)
                    if country:
                        result.append(f"({country})")
                    
                    return " ".join(result).strip() or "未知"
                elif response.status_code == 429:
                    return "API限流中"
        except Exception:
            pass
        return "未知"

    async def _query_by_id(self, server_id: str) -> str:
        """根据服务器ID查询信息"""
        
        try:
            async with await self._get_proxy_client() as client:
                response = await client.get(
                    f"{self.base_url}/servers/{server_id}",
                    params={"include": "player"},
                )

                if response.status_code == 404:
                    return f"❌ 未找到ID为 {server_id} 的服务器。\n提示：可以使用 /id查询 search <关键词> 来搜索服务器。"

                if response.status_code == 401:
                    return "⚠️ API需要授权令牌。"

                response.raise_for_status()
                data = response.json()

            attrs = data.get("data", {}).get("attributes", {})
            name = attrs.get("name", "未知")
            status = attrs.get("status", "unknown")
            players = attrs.get("players", 0)
            max_players = attrs.get("maxPlayers", 0)
            ip = attrs.get("ip", "未知")
            port = attrs.get("port", "未知")
            country = attrs.get("country", "未知")
            location = attrs.get("location", [])
            is_online = status == "online"
            
            # 获取游戏时间
            details = attrs.get("details", {})
            game_time = details.get("gameTime", "未知")

            location_str = "未知"
            region_name = "未知"
            if location and len(location) == 2:
                location_str = f"{location[0]}, {location[1]}"
                # location[0] 是经度(lon)，location[1] 是纬度(lat)
                region_name = await self._reverse_geocode(location[1], location[0])
            
            # 如果服务器已移除，显示警告
            if status == "removed":
                return (
                    f"⚠️ 警告：服务器 {server_id} 已被移除\n"
                    f"服务器名称: {name}\n"
                    f"状态: 已移除 (removed)\n\n"
                    f"建议：使用 /id查询 search <关键词> 搜索其他有效服务器。"
                )

            result_text = (
                f"ID: {server_id}\n"
                f"服务器名称: {name}\n"
                f"在线人数: {players}/{max_players}\n"
                f"地区: {country}\n"
                f"是否开机: {'是 ✅' if is_online else '否 ❌'}\n"
                f"IP地址: {ip}:{port}\n"
                f"坐标位置: {location_str}\n"
                f"真实地区: {region_name}\n"
                f"游戏时间: {game_time}\n"
                f"状态: {status}"
            )

            return result_text

        except httpx.TimeoutException:
            return "⏰ 查询超时，请稍后重试。"
        except httpx.HTTPStatusError as e:
            return f"❌ API请求失败: {e.response.status_code}"
        except httpx.ConnectError:
            return "❌ 网络连接失败，请检查网络设置或稍后重试。"
        except Exception as e:
            error_msg = str(e)
            if "connection" in error_msg.lower() or "failed" in error_msg.lower():
                return "❌ 网络连接失败，请检查网络设置或稍后重试。"
            return f"❌ 查询失败: {error_msg}"

    async def _query_by_id_simple(self, server_id: str) -> str:
        """根据服务器ID查询信息（简化格式）"""
        try:
            async with await self._get_proxy_client() as client:
                response = await client.get(
                    f"{self.base_url}/servers/{server_id}",
                    params={"include": "player"},
                )

                if response.status_code == 404:
                    return f"❌ 未找到ID为 {server_id} 的服务器。"

                if response.status_code == 401:
                    return "⚠️ API需要授权令牌。"

                response.raise_for_status()
                data = response.json()

            attrs = data.get("data", {}).get("attributes", {})
            name = attrs.get("name", "未知")
            status = attrs.get("status", "unknown")
            players = attrs.get("players", 0)
            max_players = attrs.get("maxPlayers", 0)
            ip = attrs.get("ip", "未知")
            port = attrs.get("port", "未知")
            country = attrs.get("country", "未知")
            is_online = status == "online"

            if status == "removed":
                return f"❌ 查询不到该服务器"

            from datetime import datetime
            current_time = datetime.now().strftime("%H:%M")

            result_text = (
                f"【{name}】\n"
                f"人数: {players}/{max_players}\n"
                f"时间: {current_time}\n"
                f"状态: {'在线' if is_online else '离线'}\n"
                f"区服: {country}\n"
                f"IP: {ip}:{port}"
            )

            return result_text

        except httpx.TimeoutException:
            return "⏰ 查询超时，请稍后重试。"
        except httpx.HTTPStatusError as e:
            return f"❌ API请求失败: {e.response.status_code}"
        except httpx.ConnectError:
            return "❌ 网络连接失败，请检查网络设置或稍后重试。"
        except Exception as e:
            error_msg = str(e)
            if "connection" in error_msg.lower() or "failed" in error_msg.lower():
                return "❌ 网络连接失败，请检查网络设置或稍后重试。"
            return f"❌ 查询失败: {error_msg}"

    async def _search_servers(self, keyword: str) -> str:
        """搜索SCUM服务器"""
        try:
            async with await self._get_proxy_client() as client:
                params = {
                    "filter[game]": "scum",
                    "filter[search]": keyword,
                    "page[size]": 10,
                }
                response = await client.get(
                    f"{self.base_url}/servers",
                    params=params,
                )

                if response.status_code == 401:
                    return "⚠️ API需要授权令牌。"

                response.raise_for_status()
                data = response.json()

            servers = data.get("data", [])
            
            if not servers:
                return f"❌ 未找到包含 '{keyword}' 的SCUM服务器。"

            result_lines = [f"🔍 找到 {len(servers)} 个服务器："]
            for index, server in enumerate(servers[:10], start=1):
                attrs = server.get("attributes", {})
                server_id = server.get("id", "未知")
                name = attrs.get("name", "未知")
                status = attrs.get("status", "unknown")
                players = attrs.get("players", 0)
                max_players = attrs.get("maxPlayers", 0)
                ip = attrs.get("ip", "未知")
                port = attrs.get("port", "未知")
                country = attrs.get("country", "未知")
                location = attrs.get("location", [])
                is_online = status == "online"
                
                location_str = "未知"
                region_name = "未知"
                if location and len(location) == 2:
                    location_str = f"{location[0]}, {location[1]}"
                    # location[0] 是经度(lon)，location[1] 是纬度(lat)
                    region_name = await self._reverse_geocode(location[1], location[0])
                
                # 获取游戏时间（搜索结果可能不包含details字段）
                game_time = "未知"
                details = attrs.get("details", {})
                if details:
                    game_time = details.get("gameTime", "未知")
                
                result_lines.append(
                    f"\n#{index}"
                    f"\nID: {server_id}"
                    f"\n服务器名称: {name}"
                    f"\n在线人数: {players}/{max_players}"
                    f"\n地区: {country}"
                    f"\n是否开机: {'是 ✅' if is_online else '否 ❌'}"
                    f"\nIP地址: {ip}:{port}"
                    f"\n坐标位置: {location_str}"
                    f"\n真实地区: {region_name}"
                    f"\n状态: {status}"
                )

            return "\n".join(result_lines)

        except httpx.TimeoutException:
            return "⏰ 搜索超时，请稍后重试。"
        except httpx.HTTPStatusError as e:
            return f"❌ API请求失败: {e.response.status_code}"
        except httpx.ConnectError:
            return "❌ 网络连接失败，请检查网络设置或稍后重试。"
        except Exception as e:
            error_msg = str(e)
            if "connection" in error_msg.lower() or "failed" in error_msg.lower():
                return "❌ 网络连接失败，请检查网络设置或稍后重试。"
            return f"❌ 搜索失败: {error_msg}"

    async def _get_server_ranking(self) -> str:
        """获取SCUM服务器排名（显示10个）"""
        try:
            async with await self._get_proxy_client() as client:
                params = {
                    "filter[game]": "scum",
                    "page[size]": 10,
                    "sort": "-players",
                }
                response = await client.get(
                    f"{self.base_url}/servers",
                    params=params,
                )

                response.raise_for_status()
                data = response.json()

            servers = data.get("data", [])
            
            if not servers:
                return "❌ 未能获取服务器排名。"

            result_lines = ["🏆 SCUM 服务器排名（按在线人数）："]
            for index, server in enumerate(servers[:10], start=1):  # 显示前10个
                attrs = server.get("attributes", {})
                server_id = server.get("id", "未知")
                name = attrs.get("name", "未知")
                status = attrs.get("status", "unknown")
                players = attrs.get("players", 0)
                max_players = attrs.get("maxPlayers", 0)
                country = attrs.get("country", "未知")
                location = attrs.get("location", [])
                is_online = status == "online"
                
                location_str = "未知"
                region_name = "未知"
                if location and len(location) == 2:
                    location_str = f"{location[0]}, {location[1]}"
                    # location[0] 是经度(lon)，location[1] 是纬度(lat)
                    region_name = await self._reverse_geocode(location[1], location[0])
                
                result_lines.append(
                    f"\n#{index}"
                    f"\nID: {server_id}"
                    f"\n服务器名称: {name}"
                    f"\n在线人数: {players}/{max_players}"
                    f"\n地区: {country}"
                    f"\n是否开机: {'是 ✅' if is_online else '否 ❌'}"
                    f"\n坐标位置: {location_str}"
                    f"\n真实地区: {region_name}"
                    f"\n状态: {status}"
                )

            return "\n".join(result_lines)

        except httpx.TimeoutException:
            return "⏰ 获取排名超时，请稍后重试。"
        except httpx.HTTPStatusError as e:
            return f"❌ API请求失败: {e.response.status_code}"
        except httpx.ConnectError:
            return "❌ 网络连接失败，请检查网络设置或稍后重试。"
        except Exception as e:
            error_msg = str(e)
            if "connection" in error_msg.lower() or "failed" in error_msg.lower():
                return "❌ 网络连接失败，请检查网络设置或稍后重试。"
            return f"❌ 获取排名失败: {error_msg}"

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
