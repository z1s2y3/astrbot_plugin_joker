"""Private proxy client for the independent official QQ bot dashboard."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx


OFFICIAL_FEATURES: tuple[dict[str, str], ...] = (
    {"key": "scum_query", "label": "SCUM 查询", "note": "绑定服务器、在线查询和关键词查询。"},
    {"key": "statistics", "label": "签到统计", "note": "签到、积分排行和群统计。"},
    {"key": "auto_reply", "label": "自动回复", "note": "按关键词回复消息。"},
    {"key": "user_replies", "label": "用户专属回复", "note": "按群和用户保存独立回复。"},
    {"key": "lottery", "label": "抽奖", "note": "发起、报名和开奖。"},
    {"key": "entertainment", "label": "娱乐", "note": "骰子、猜拳、随机选择和时间。"},
    {"key": "blacklist", "label": "黑名单", "note": "查询、加入、移出和清空黑名单。"},
    {"key": "moderation", "label": "群管理", "note": "禁言、解禁、踢出、撤回和成员查询。"},
    {"key": "keyword_mute", "label": "关键词禁言", "note": "命中独立规则后禁言发言成员。"},
    {"key": "keyword_kick", "label": "关键词踢出", "note": "命中独立规则后移出发言成员。"},
    {"key": "keyword_blacklist", "label": "关键词黑名单", "note": "命中独立规则后移出并加入黑名单。"},
    {"key": "group_admins", "label": "分群管理员", "note": "本群独立维护分管管理员。"},
    {"key": "message_records", "label": "群消息记录", "note": "保存收到的文字消息。"},
    {"key": "scheduled_messages", "label": "定时消息", "note": "按间隔发送重复消息。"},
)


class OfficialBotError(RuntimeError):
    """Raised when the official bot dashboard cannot be reached or rejects a request."""


class OfficialBotClient:
    def __init__(self, base_url: str, token: str, timeout_seconds: int = 10) -> None:
        normalized_url = str(base_url or "").strip().rstrip("/")
        parsed = urlsplit(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise OfficialBotError("官方 QQ 机器人控制台地址未配置")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise OfficialBotError("官方 QQ 机器人控制台地址格式无效")
        if not str(token or "").strip():
            raise OfficialBotError("官方 QQ 机器人控制台令牌未配置")
        self.base_url = normalized_url
        self.token = str(token).strip()
        self.timeout = httpx.Timeout(max(3, min(30, int(timeout_seconds))))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers={"X-Dashboard-Token": self.token, "Accept": "application/json"},
                    params=params,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise OfficialBotError("官方 QQ 机器人控制台暂时无法连接") from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            detail = data.get("error") if isinstance(data, dict) else ""
            raise OfficialBotError(str(detail or f"官方 QQ 机器人控制台返回 HTTP {response.status_code}"))
        return data

    def list_groups(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/groups")
        if not isinstance(data, list):
            raise OfficialBotError("官方 QQ 机器人群组数据格式无效")
        return [item for item in data if isinstance(item, dict)]

    def _scope_for_group(self, group_id: str) -> tuple[str, dict[str, Any]]:
        target = str(group_id or "").strip()
        for item in self.list_groups():
            if str(item.get("numeric_group_id", "")).strip() == target:
                scope_id = str(item.get("scope_id", "")).strip()
                if scope_id:
                    return scope_id, item
        raise OfficialBotError("该群还没有绑定 QQ 官方机器人，请先在官方机器人中执行 /绑定官方群 群号")

    def load_group(self, group_id: str) -> dict[str, Any]:
        scope_id, summary = self._scope_for_group(group_id)
        config = self._request("GET", "/api/group-config", params={"scope_id": scope_id})
        replies = self._request("GET", "/api/replies", params={"scope_id": scope_id})
        try:
            blacklist = self._request("GET", "/api/blacklist", params={"scope_id": scope_id})
        except OfficialBotError:
            blacklist = []
        admins = self._request("GET", "/api/group-admins", params={"scope_id": scope_id})
        user_replies = self._request("GET", "/api/user-replies", params={"scope_id": scope_id})
        schedules = self._request("GET", "/api/schedules", params={"scope_id": scope_id})
        messages = self._request(
            "GET", "/api/messages", params={"scope_id": scope_id, "limit": "100"}
        )
        message_users = self._request(
            "GET", "/api/message-users", params={"scope_id": scope_id, "limit": "200"}
        )
        keyword_rules = self._request(
            "GET", "/api/keyword-rules", params={"scope_id": scope_id}
        )
        return {
            "available": True,
            "scope_id": scope_id,
            "summary": summary,
            "config": config if isinstance(config, dict) else {},
            "admins": admins if isinstance(admins, list) else [],
            "replies": replies if isinstance(replies, list) else [],
            "blacklist": blacklist if isinstance(blacklist, list) else [],
            "user_replies": user_replies if isinstance(user_replies, list) else [],
            "schedules": schedules if isinstance(schedules, list) else [],
            "messages": messages if isinstance(messages, list) else [],
            "message_users": message_users if isinstance(message_users, list) else [],
            "keyword_rules": keyword_rules if isinstance(keyword_rules, list) else [],
            "features": list(OFFICIAL_FEATURES),
        }

    def save_group_config(
        self,
        scope_id: str,
        *,
        features: dict[str, bool],
        server_id: str,
        auto_reply_enabled: bool,
    ) -> None:
        self._request(
            "POST",
            "/api/group-config",
            payload={
                "scope_id": scope_id,
                "features": features,
                "server_id": server_id,
                "auto_reply_enabled": auto_reply_enabled,
            },
        )

    def save_reply(self, scope_id: str, trigger: str, response: str) -> None:
        self._request(
            "POST",
            "/api/replies",
            payload={"scope_id": scope_id, "trigger": trigger, "response": response},
        )

    def delete_reply(self, scope_id: str, trigger: str) -> None:
        self._request(
            "POST",
            "/api/replies/delete",
            payload={"scope_id": scope_id, "trigger": trigger},
        )

    def save_blacklist(self, scope_id: str, user_id: str, reason: str) -> None:
        self._request(
            "POST",
            "/api/blacklist",
            payload={
                "scope_id": scope_id,
                "user_id": user_id,
                "reason": reason,
            },
        )

    def delete_blacklist(self, scope_id: str, user_id: str) -> None:
        self._request(
            "POST",
            "/api/blacklist",
            payload={"scope_id": scope_id, "user_id": user_id, "action": "remove"},
        )

    def save_group_admin(self, scope_id: str, user_id: str, display_name: str) -> None:
        self._request(
            "POST",
            "/api/group-admins",
            payload={
                "scope_id": scope_id,
                "user_id": user_id,
                "display_name": display_name,
            },
        )

    def delete_group_admin(self, scope_id: str, user_id: str) -> None:
        self._request(
            "POST",
            "/api/group-admins",
            payload={"scope_id": scope_id, "user_id": user_id, "action": "remove"},
        )

    def save_user_reply(self, scope_id: str, user_id: str, trigger: str, response: str) -> None:
        self._request(
            "POST",
            "/api/user-replies",
            payload={
                "scope_id": scope_id,
                "user_id": user_id,
                "trigger": trigger,
                "response": response,
            },
        )

    def delete_user_reply(self, scope_id: str, user_id: str, trigger: str) -> None:
        self._request(
            "POST",
            "/api/user-replies/delete",
            payload={"scope_id": scope_id, "user_id": user_id, "trigger": trigger},
        )

    def save_schedule(self, scope_id: str, interval_minutes: int, content: str) -> None:
        self._request(
            "POST",
            "/api/schedules",
            payload={
                "scope_id": scope_id,
                "interval_minutes": interval_minutes,
                "content": content,
            },
        )

    def delete_schedule(self, scope_id: str, schedule_id: int) -> None:
        self._request(
            "POST",
            "/api/schedules/delete",
            payload={"scope_id": scope_id, "schedule_id": schedule_id},
        )

    def list_keyword_rules(self, scope_id: str, action: str = "") -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/api/keyword-rules",
            params={"scope_id": scope_id, **({"action": action} if action else {})},
        )
        return data if isinstance(data, list) else []

    def save_keyword_rule(
        self, scope_id: str, action: str, trigger: str, duration_minutes: int = 10
    ) -> None:
        self._request(
            "POST",
            "/api/keyword-rules",
            payload={
                "scope_id": scope_id,
                "action": action,
                "trigger": trigger,
                "duration_minutes": duration_minutes,
            },
        )

    def delete_keyword_rule(self, scope_id: str, rule_id: int) -> None:
        self._request(
            "POST",
            "/api/keyword-rules/delete",
            payload={"scope_id": scope_id, "rule_id": rule_id},
        )

    def moderation(
        self,
        scope_id: str,
        action: str,
        *,
        user_id: str = "",
        message_id: str = "",
        duration_minutes: int = 10,
    ) -> None:
        self._request(
            "POST",
            "/api/moderation",
            payload={
                "scope_id": scope_id,
                "action": action,
                "user_id": user_id,
                "message_id": message_id,
                "duration_minutes": duration_minutes,
            },
        )
