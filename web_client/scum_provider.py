"""SCUM server lookups through the authenticated BattleMetrics relay only."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import httpx


logger = logging.getLogger(__name__)


COUNTRY_CODES = {
    "CN": "中国",
    "CHINA": "中国",
    "US": "美国",
    "USA": "美国",
    "UNITED STATES": "美国",
    "JP": "日本",
    "JAPAN": "日本",
    "KR": "韩国",
    "KOREA": "韩国",
    "GB": "英国",
    "UNITED KINGDOM": "英国",
    "UK": "英国",
    "DE": "德国",
    "GERMANY": "德国",
    "FR": "法国",
    "FRANCE": "法国",
    "RU": "俄罗斯",
    "RUSSIA": "俄罗斯",
    "HK": "香港",
    "HONG KONG": "香港",
    "TW": "台湾",
    "TAIWAN": "台湾",
    "SG": "新加坡",
    "SINGAPORE": "新加坡",
    "TH": "泰国",
    "THAILAND": "泰国",
    "VN": "越南",
    "VIETNAM": "越南",
    "MY": "马来西亚",
    "MALAYSIA": "马来西亚",
    "AU": "澳大利亚",
    "AUSTRALIA": "澳大利亚",
    "NZ": "新西兰",
    "NEW ZEALAND": "新西兰",
    "CA": "加拿大",
    "EU": "欧洲",
    "EUROPE": "欧洲",
    "ID": "印度尼西亚",
    "INDONESIA": "印度尼西亚",
    "PH": "菲律宾",
    "PHILIPPINES": "菲律宾",
    "IN": "印度",
    "INDIA": "印度",
}


class LinuxScumProvider:
    """Read-only SCUM server lookups executed by the Linux portal."""

    DEFAULT_RELAY_URL = "https://ba.yooyun.top"
    REQUEST_TIMEOUT_SECONDS = 60.0

    def __init__(self, *, relay_url: str = "", relay_key: str = "") -> None:
        configured_url = str(relay_url or "").strip()
        self.relay_url = (
            self._normalize_relay_url(configured_url)
            if configured_url
            else self.DEFAULT_RELAY_URL
        )
        self.relay_key = str(relay_key or "").strip()
        if configured_url and not self.relay_url:
            logger.warning("Ignoring malformed SCUM_RELAY_URL")

    @staticmethod
    def _normalize_relay_url(value: object) -> str:
        raw = str(value or "").strip().rstrip("/")
        try:
            parsed = urlsplit(raw)
            _ = parsed.port
        except ValueError:
            return ""
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            return ""
        return raw

    @staticmethod
    def _clean_text(value: object, limit: int = 256) -> str:
        text = str(value if value is not None else "未知")
        text = "".join(
            " " if ord(char) < 32 or ord(char) == 127 else char for char in text
        )
        return text.strip()[:limit] or "未知"

    @staticmethod
    def _pick(mapping: object, *keys: str, default: object = None) -> object:
        if not isinstance(mapping, dict):
            return default
        for key in keys:
            value = mapping.get(key)
            if value is not None and value != "":
                return value
        return default

    @classmethod
    def _format_server(
        cls,
        server: dict[str, Any],
        *,
        index: int | None = None,
        detail: bool = False,
    ) -> str:
        server_id = cls._pick(server, "id", "serverId", "server_id", default="未知")
        name = cls._pick(server, "name", "serverName", "server_name", default="未知")
        players = cls._pick(server, "players", "playerCount", "player_count")
        maximum = cls._pick(server, "maxPlayers", "max_players", "maxPlayerCount")
        ip = cls._pick(server, "ip", "host", "address")
        port = cls._pick(server, "port", "serverPort", "server_port")
        country = cls._pick(server, "country", "countryCode", "country_code")
        if isinstance(country, str):
            country = COUNTRY_CODES.get(country.upper(), country)

        raw_status = cls._pick(server, "status", "state")
        status = cls._clean_text(raw_status) if raw_status is not None else "未知"
        normalized_status = str(raw_status or "").strip().lower()
        if normalized_status in {"online", "on", "true", "1", "在线"}:
            running = "是"
        elif normalized_status in {"offline", "dead", "off", "false", "0", "离线"}:
            running = "否"
        else:
            running = "未知"

        address = "未知"
        if ip is not None:
            address = cls._clean_text(ip, 128)
            if port is not None:
                address = f"{address}:{cls._clean_text(port, 16)}"
        header = "服务器详细信息：" if detail else f"{index}. SCUM 服务器"
        lines = [
            f"{header}\n"
            f"ID: {cls._clean_text(server_id, 64)}\n"
            f"服务器名称: {cls._clean_text(name, 128)}\n"
            f"在线人数: {cls._clean_text(players, 32)}/{cls._clean_text(maximum, 32)}\n"
            f"IP地址: {address}\n"
            f"状态: {status}\n"
            f"地区: {cls._clean_text(country, 64)}\n"
            f"是否开机: {running}"
        ]
        rank = cls._pick(server, "rank", "ranking", "position")
        if rank is not None:
            rank_text = cls._clean_text(rank, 32)
            lines.append(f"排名: {rank_text if rank_text.startswith('#') else f'#{rank_text}'}")
        version = cls._pick(server, "version", "gameVersion", "game_version")
        if version is not None:
            lines.append(f"版本: {cls._clean_text(version, 64)}")
        game_time = cls._pick(
            server,
            "gameTime",
            "game_time",
            "serverTime",
            "server_time",
            "inGameTime",
            "in_game_time",
            "time",
            # The current relay exposes the server's in-game clock through
            # this legacy field name. It is not a plugin-side fetch time.
            "updatedAt",
            "updated_at",
            "lastUpdated",
        )
        if game_time is not None:
            lines.append(f"游戏时间: {cls._clean_text(game_time, 64)}")
        return "\n".join(lines)

    async def _relay_get(
        self, path: str, *, params: dict[str, str | int] | None = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not self.relay_key:
            return (
                None,
                "SCUM 中转密钥未配置，请在 Linux 网页服务器 web_client/.env 设置 SCUM_RELAY_KEY。",
            )
        if not self.relay_url:
            return None, "SCUM 中转地址无效，请检查 Linux 网页服务器的 SCUM_RELAY_URL。"

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.REQUEST_TIMEOUT_SECONDS, connect=10.0),
                follow_redirects=True,
                trust_env=False,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.relay_key}",
                    "User-Agent": "Joker-Linux-Portal/1.11",
                },
            ) as client:
                response = await client.get(f"{self.relay_url}{path}", params=params)
            try:
                payload = response.json()
            except (TypeError, ValueError):
                payload = None

            if response.status_code >= 400:
                detail = self._pick(payload, "error", "detail")
                if response.status_code == 401:
                    return None, "SCUM 中转站密钥缺失或无效，请检查 SCUM_RELAY_KEY。"
                if response.status_code == 404:
                    if path.startswith("/api/servers/"):
                        return None, "未找到该 SCUM 服务器，请检查服务器 ID。"
                    return None, "SCUM 中转站接口不存在，请检查 SCUM_RELAY_URL。"
                if response.status_code == 429:
                    return None, "SCUM 中转站正在处理其他查询，请稍后重试。"
                if detail:
                    return None, self._clean_text(detail, 240)
                if response.status_code in {502, 503}:
                    return None, "SCUM 中转站暂时无法读取 BattleMetrics 数据，请稍后重试。"
                return None, f"SCUM 中转站请求失败：HTTP {response.status_code}。"
            if not isinstance(payload, dict):
                return None, "SCUM 中转站返回了无效数据，请稍后重试。"
            return payload, None
        except httpx.TimeoutException:
            logger.warning("SCUM relay request timed out: %s", path)
            return None, "SCUM 中转站请求超时，请稍后重试。"
        except httpx.RequestError as exc:
            logger.warning("SCUM relay request failed: %s", type(exc).__name__)
            return None, "无法连接 SCUM 中转站，请检查 Linux 网页服务器的出站网络。"
        except Exception as exc:  # pragma: no cover - defensive provider boundary
            logger.warning("SCUM relay request failed: %s", type(exc).__name__)
            return None, "SCUM 中转站查询失败，请稍后重试。"

    async def search_servers(self, keyword: str) -> str:
        value = str(keyword or "").strip()
        if not value:
            return "请提供要查询的服务器关键词。"
        if len(value) > 120:
            return "查询关键词不能超过 120 个字符。"
        if value.isdigit() and len(value) >= 5:
            return await self.get_server_detail(value)

        payload, error = await self._relay_get("/api/search", params={"q": value})
        if error:
            return error
        records = payload.get("servers")
        if not isinstance(records, list):
            return "SCUM 中转站返回的服务器列表格式无效，请稍后重试。"
        if any(not isinstance(item, dict) for item in records):
            records = [item for item in records if isinstance(item, dict)]
            if not records:
                return "SCUM 中转站返回的服务器列表格式无效，请稍后重试。"
        if not records:
            return f"未找到包含 '{self._clean_text(value, 120)}' 的 SCUM 服务器。"
        results = [f"中转站找到 {len(records)} 个 SCUM 服务器："]
        for index, item in enumerate(records, 1):
            results.append(self._format_server(item, index=index))
        return "\n\n".join(results)

    async def get_server_detail(self, server_id: str) -> str:
        value = str(server_id or "").strip()
        if not value:
            return "请提供服务器 ID。"
        if len(value) > 20 or not value.isdigit():
            return "服务器 ID 必须为 1 到 20 位数字。"

        payload, error = await self._relay_get(f"/api/servers/{value}")
        if error:
            return error
        record = payload.get("server")
        if not isinstance(record, dict):
            return "SCUM 中转站返回的服务器详情格式无效，请稍后重试。"
        return self._format_server(record, detail=True)

