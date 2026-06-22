import httpx
import json
import os
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api import logger

class NapCatAPI:
    """NapCat OneBot 11 HTTP API 客户端"""
    
    def __init__(self):
        self.config = self._load_config()
        self.base_url = self.config.get("api_url", "http://127.0.0.1:3000")
        self.timeout = httpx.Timeout(10.0, connect=5.0)
    
    def _load_config(self):
        config_path = Path(get_astrbot_data_path()) / "plugin_data" / "qqadmin" / "api_config.json"
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载 API 配置失败: {e}")
        return {}
    
    def _save_config(self, config):
        config_path = Path(get_astrbot_data_path()) / "plugin_data" / "qqadmin" / "api_config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存 API 配置失败: {e}")
    
    def set_api_url(self, url: str):
        """设置 API 地址"""
        self.base_url = url
        self.config["api_url"] = url
        self._save_config(self.config)
    
    async def _request(self, action: str, **params) -> dict:
        """发送 API 请求"""
        url = f"{self.base_url}/api"
        data = {
            "action": action,
            "params": params
        }
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=data)
                response.raise_for_status()
                result = response.json()
                
                if result.get("status") == "ok":
                    return {"success": True, "data": result.get("data")}
                else:
                    return {"success": False, "error": result.get("msg", "Unknown error")}
        
        except httpx.TimeoutException:
            logger.error(f"API 请求超时: {action}")
            return {"success": False, "error": "请求超时"}
        except httpx.HTTPStatusError as e:
            logger.error(f"API 请求失败 {e.response.status_code}: {action}")
            logger.error(f"请求URL: {url}, 参数: {params}")
            try:
                response_text = await e.response.aread()
                logger.error(f"响应内容: {response_text[:500]}")
            except:
                pass
            return {"success": False, "error": f"HTTP 错误 {e.response.status_code}"}
        except Exception as e:
            logger.error(f"API 请求异常 {action}: {e}")
            return {"success": False, "error": str(e)}
    
    # ==================== 群组管理接口 ====================
    
    async def set_group_kick(self, group_id: str, user_id: str, reject_add_request: bool = False) -> dict:
        """
        踢出群成员
        :param group_id: 群号
        :param user_id: QQ号
        :param reject_add_request: 是否拒绝此人的加群请求
        """
        # 尝试使用GET方式调用
        import httpx
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                url = f"{self.base_url}/set_group_kick?group_id={int(group_id)}&user_id={int(user_id)}&reject_add_request={str(reject_add_request).lower()}"
                response = await client.get(url)
                response.raise_for_status()
                result = response.json()
                
                if result.get("status") == "ok" or result.get("retcode") == 0:
                    return {"success": True, "data": result.get("data")}
                else:
                    return {"success": False, "error": result.get("msg", result.get("wording", "Unknown error"))}
        except Exception as e:
            # 如果GET方式失败，回退到POST方式
            logger.warning(f"GET方式调用set_group_kick失败，尝试POST方式: {e}")
            return await self._request(
                "set_group_kick",
                group_id=int(group_id),
                user_id=int(user_id),
                reject_add_request=reject_add_request
            )
    
    async def set_group_kick_members(self, group_id: str, user_ids: list) -> dict:
        """
        批量踢出群成员（扩展接口）
        :param group_id: 群号
        :param user_ids: QQ号列表
        """
        return await self._request(
            "set_group_kick_members",
            group_id=int(group_id),
            user_ids=[int(uid) for uid in user_ids]
        )
    
    async def set_group_ban(self, group_id: str, user_id: str, duration: int = 0) -> dict:
        """
        群成员禁言
        :param group_id: 群号
        :param user_id: QQ号
        :param duration: 禁言时长（秒），0为永久禁言
        """
        return await self._request(
            "set_group_ban",
            group_id=int(group_id),
            user_id=int(user_id),
            duration=duration
        )
    
    async def set_group_whole_ban(self, group_id: str, enable: bool = True) -> dict:
        """
        全体禁言
        :param group_id: 群号
        :param enable: 是否开启全体禁言
        """
        return await self._request(
            "set_group_whole_ban",
            group_id=int(group_id),
            enable=enable
        )
    
    async def set_group_admin(self, group_id: str, user_id: str, enable: bool = True) -> dict:
        """
        设置管理员
        :param group_id: 群号
        :param user_id: QQ号
        :param enable: True为设置管理员，False为取消管理员
        """
        return await self._request(
            "set_group_admin",
            group_id=int(group_id),
            user_id=int(user_id),
            enable=enable
        )
    
    async def set_group_card(self, group_id: str, user_id: str, card: str = "") -> dict:
        """
        设置群名片
        :param group_id: 群号
        :param user_id: QQ号
        :param card: 名片内容，空字符串为恢复默认
        """
        return await self._request(
            "set_group_card",
            group_id=int(group_id),
            user_id=int(user_id),
            card=card
        )
    
    async def set_group_name(self, group_id: str, group_name: str) -> dict:
        """
        设置群名
        :param group_id: 群号
        :param group_name: 新群名
        """
        return await self._request(
            "set_group_name",
            group_id=int(group_id),
            group_name=group_name
        )
    
    async def set_group_leave(self, group_id: str, is_dismiss: bool = False) -> dict:
        """
        退出群组
        :param group_id: 群号
        :param is_dismiss: 是否解散群（仅群主可用）
        """
        return await self._request(
            "set_group_leave",
            group_id=int(group_id),
            is_dismiss=is_dismiss
        )
    
    async def set_group_add_request(self, flag: str, approve: bool = True, reason: str = "") -> dict:
        """
        处理加群请求
        :param flag: 请求标识
        :param approve: 是否同意
        :param reason: 拒绝理由（仅拒绝时有效）
        """
        return await self._request(
            "set_group_add_request",
            flag=flag,
            approve=approve,
            reason=reason
        )
    
    # ==================== 信息获取接口 ====================
    
    async def get_group_info(self, group_id: str) -> dict:
        """
        获取群信息
        :param group_id: 群号
        """
        return await self._request("get_group_info", group_id=int(group_id))
    
    async def get_group_detail_info(self, group_id: str) -> dict:
        """
        获取群详细信息（扩展接口）
        :param group_id: 群号
        """
        return await self._request("get_group_detail_info", group_id=int(group_id))
    
    async def get_group_list(self) -> dict:
        """获取群列表"""
        return await self._request("get_group_list")
    
    async def get_group_member_list(self, group_id: str) -> dict:
        """
        获取群成员列表
        :param group_id: 群号
        """
        return await self._request("get_group_member_list", group_id=int(group_id))
    
    async def get_group_member_info(self, group_id: str, user_id: str) -> dict:
        """
        获取群成员信息
        :param group_id: 群号
        :param user_id: QQ号
        """
        return await self._request(
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(user_id)
        )
    
    async def get_stranger_info(self, user_id: str) -> dict:
        """
        获取陌生人信息
        :param user_id: QQ号
        """
        return await self._request("get_stranger_info", user_id=int(user_id))
    
    # ==================== 消息接口 ====================
    
    async def send_group_msg(self, group_id: str, message: str) -> dict:
        """
        发送群消息
        :param group_id: 群号
        :param message: 消息内容
        :return: 包含 message_id 的结果
        """
        result = await self._request(
            "send_group_msg",
            group_id=int(group_id),
            message=message
        )
        if result.get("success"):
            data = result.get("data", {})
            if isinstance(data, dict):
                result["message_id"] = data.get("message_id")
        return result
    
    async def send_private_msg(self, user_id: str, message: str) -> dict:
        """
        发送私聊消息
        :param user_id: QQ号
        :param message: 消息内容
        """
        return await self._request(
            "send_private_msg",
            user_id=int(user_id),
            message=message
        )
    
    async def delete_msg(self, message_id: str) -> dict:
        """
        删除消息
        :param message_id: 消息ID
        """
        return await self._request("delete_msg", message_id=int(message_id))

    async def get_group_msg_history(self, group_id: str, message_id: int = 0, count: int = 20) -> dict:
        """
        获取群消息历史
        :param group_id: 群号
        :param message_id: 起始消息ID（0表示从最新消息开始）
        :param count: 获取消息数量
        """
        return await self._request(
            "get_group_msg_history",
            group_id=int(group_id),
            message_id=message_id,
            count=count
        )

    # ==================== 核心接口 ====================
    
    async def group_poke(self, group_id: str, user_id: str) -> dict:
        """
        群内戳一戳
        :param group_id: 群号
        :param user_id: QQ号
        """
        return await self._request(
            "group_poke",
            group_id=int(group_id),
            user_id=int(user_id)
        )
    
    async def friend_poke(self, user_id: str) -> dict:
        """
        私聊戳一戳
        :param user_id: QQ号
        """
        return await self._request("friend_poke", user_id=int(user_id))
    
    # ==================== 系统接口 ====================
    
    async def get_login_info(self) -> dict:
        """获取登录信息"""
        return await self._request("get_login_info")
    
    async def get_version_info(self) -> dict:
        """获取版本信息"""
        return await self._request("get_version_info")
    
    async def get_status(self) -> dict:
        """获取状态"""
        return await self._request("get_status")
    
    # ==================== 扩展接口 ====================
    
    async def set_group_special_title(self, group_id: str, user_id: str, special_title: str = "") -> dict:
        """
        设置群成员专属头衔
        :param group_id: 群号
        :param user_id: QQ号
        :param special_title: 专属头衔，空字符串为清除
        """
        return await self._request(
            "set_group_special_title",
            group_id=int(group_id),
            user_id=int(user_id),
            special_title=special_title
        )
    
    async def get_group_honor_info(self, group_id: str, type: str = "all") -> dict:
        """
        获取群荣誉信息
        :param group_id: 群号
        :param type: 荣誉类型（all, talkative, performer, legend, strong_newbie, emotion）
        """
        return await self._request(
            "get_group_honor_info",
            group_id=int(group_id),
            type=type
        )

# 创建全局 API 实例
api_client = NapCatAPI()