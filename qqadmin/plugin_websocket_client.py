"""Client for the documented SCUM plugin WebSocket gateway.

The gateway is deliberately kept separate from the Linux portal WebSocket:
the portal carries control-plane calls, while this connection reaches the
game plugin configured for one server profile.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

import aiohttp


logger = logging.getLogger(__name__)
PLUGIN_WS_PATH = "/ws"
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MIN_TIMEOUT_SECONDS = 4.0
MAX_TIMEOUT_SECONDS = 300.0


def plugin_websocket_url(
    host: object,
    port: object,
    api_key: object,
    path: object = PLUGIN_WS_PATH,
    scheme: object = "ws",
) -> str:
    """Build the documented ``/ws?apiKey=...`` URL without logging secrets."""

    host_text = str(host or "").strip().strip("[]")
    key_text = str(api_key or "").strip()
    path_text = str(path or PLUGIN_WS_PATH).strip()
    scheme_text = str(scheme or "ws").strip().lower()
    if (
        not host_text
        or not key_text
        or "://" in host_text
        or "/" in host_text
        or "?" in host_text
        or "#" in host_text
        or path_text != PLUGIN_WS_PATH
        or scheme_text not in {"ws", "wss"}
    ):
        return ""
    try:
        port_number = int(port)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not 1 <= port_number <= 65535:
        return ""
    if ":" in host_text:
        host_text = f"[{host_text}]"
    return f"{scheme_text}://{host_text}:{port_number}{PLUGIN_WS_PATH}?apiKey={quote(key_text, safe='')}"


def parse_plugin_websocket_url(value: object) -> dict[str, object]:
    """Parse exactly the documented ``ws(s)://host:port/ws?apiKey=...`` URL."""
    text = str(value or "").strip()
    if not text or len(text) > 2048:
        raise ValueError("插件 WebSocket 链接不能为空且不能超过 2048 个字符")
    parsed = urlsplit(text)
    if (
        parsed.scheme.lower() not in {"ws", "wss"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path != PLUGIN_WS_PATH
        or parsed.fragment
    ):
        raise ValueError("插件 WebSocket 链接必须为 ws(s)://主机:端口/ws?apiKey=...")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("插件 WebSocket 链接端口无效") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError("插件 WebSocket 链接必须包含 1-65535 的端口")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if set(query) != {"apiKey"} or len(query["apiKey"]) != 1:
        raise ValueError("插件 WebSocket 链接只能包含一个 apiKey 参数")
    api_key = str(query["apiKey"][0] or "").strip()
    if not api_key or len(api_key) > 512 or "\r" in api_key or "\n" in api_key:
        raise ValueError("插件 WebSocket API Key 无效")
    return {
        "host": str(parsed.hostname).strip(),
        "port": port,
        "api_key": api_key,
        "websocket_path": PLUGIN_WS_PATH,
        "websocket_scheme": parsed.scheme.lower(),
    }


def _bounded_timeout(value: object) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        timeout = 8.0
    if timeout != timeout or timeout in {float("inf"), float("-inf")}:
        timeout = 8.0
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, timeout))


def _response_id(message: Mapping[str, Any]) -> str:
    for key in ("id", "requestId", "request_id", "operationId", "operation_id"):
        value = str(message.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _decode_body(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _response_success(message: Mapping[str, Any], body: object) -> bool:
    def inspect(value: object, depth: int = 0) -> tuple[bool, bool]:
        if depth > 5 or not isinstance(value, Mapping):
            return False, False
        explicit = False
        positive = False
        for key in ("ok", "success", "sent"):
            if key in value:
                explicit = True
                # Strings such as "true"/"false" are not protocol proof.
                if value.get(key) is not True:
                    return False, True
                positive = True
        if value.get("duplicate") is True:
            positive = True
            explicit = True
        status = value.get("status", value.get("statusCode", value.get("status_code")))
        try:
            if status is not None:
                status_code = int(status)
                if status_code >= 400:
                    return False, True
                if 200 <= status_code < 300:
                    explicit = True
                    positive = True
        except (TypeError, ValueError, OverflowError):
            pass
        if value.get("error") not in (None, "", False, {}):
            return False, True
        for key in ("body", "result", "data", "items", "results"):
            nested_success, nested_explicit = inspect(value.get(key), depth + 1)
            if nested_explicit:
                return nested_success, True
        return positive, explicit

    success, confirmed = inspect(message)
    if confirmed:
        return success
    success, confirmed = inspect(body)
    return success if confirmed else False


def _response_message(message: Mapping[str, Any], body: object) -> str:
    for payload in (message, body if isinstance(body, Mapping) else {}):
        if not isinstance(payload, Mapping):
            continue
        for key in ("message", "error", "detail", "errMsg"):
            value = payload.get(key)
            if value not in (None, "", False, {}):
                return str(value)[:300]
    if isinstance(body, str):
        return body[:300]
    return ""


class PluginWebSocketClient:
    """Short-lived request client for one game plugin WebSocket endpoint."""

    def __init__(
        self,
        host: str,
        port: int,
        api_key: str,
        *,
        path: str = PLUGIN_WS_PATH,
        scheme: str = "ws",
        timeout: float = 8.0,
    ) -> None:
        self.host = str(host or "").strip()
        self.port = port
        self.api_key = str(api_key or "").strip()
        self.path = str(path or PLUGIN_WS_PATH).strip()
        self.scheme = str(scheme or "ws").strip().lower()
        self.timeout = _bounded_timeout(timeout)

    @property
    def url(self) -> str:
        return plugin_websocket_url(
            self.host, self.port, self.api_key, self.path, self.scheme
        )

    async def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.url
        if not url:
            return {
                "success": False,
                "code": "plugin_websocket_config_invalid",
                "message": "插件 WebSocket 地址、端口或 API Key 配置无效。",
            }
        request_id = f"op-{uuid4().hex[:20]}"
        frame: dict[str, Any] = {
            "id": request_id,
            "method": str(method or "GET").upper(),
            "path": str(path or "/").strip() or "/",
            # The plugin protocol requires body to be a JSON string.
            "body": json.dumps(dict(body or {}), ensure_ascii=False, separators=(",", ":")),
        }
        started = time.monotonic()
        timeout = aiohttp.ClientTimeout(
            total=self.timeout + 2.0,
            connect=min(self.timeout, 10.0),
        )
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(
                    url,
                    heartbeat=10.0,
                    receive_timeout=self.timeout,
                    max_msg_size=MAX_PAYLOAD_BYTES,
                ) as websocket:
                    await websocket.send_json(frame)
                    deadline = time.monotonic() + self.timeout
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return {
                                "success": False,
                                "code": "plugin_websocket_timeout",
                                "message": "插件 WebSocket 响应超时，未确认游戏指令结果。",
                                "request_id": request_id,
                            }
                        try:
                            message = await asyncio.wait_for(websocket.receive(), remaining)
                        except asyncio.TimeoutError:
                            return {
                                "success": False,
                                "code": "plugin_websocket_timeout",
                                "message": "插件 WebSocket 响应超时，未确认游戏指令结果。",
                                "request_id": request_id,
                            }
                        if message.type == aiohttp.WSMsgType.TEXT:
                            raw = str(message.data or "")
                            if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                                return {
                                    "success": False,
                                    "code": "plugin_websocket_payload_too_large",
                                    "message": "插件 WebSocket 返回数据过大，已停止处理。",
                                    "request_id": request_id,
                                }
                            try:
                                decoded = json.loads(raw)
                            except (TypeError, ValueError, json.JSONDecodeError):
                                continue
                            if not isinstance(decoded, Mapping):
                                continue
                            if str(decoded.get("type", "")).lower() == "ping":
                                await websocket.send_json({"type": "pong"})
                                continue
                            if _response_id(decoded) != request_id:
                                continue
                            response_body = _decode_body(decoded.get("body"))
                            success = _response_success(decoded, response_body)
                            result = {
                                "success": success,
                                "code": "plugin_websocket_ok" if success else "plugin_websocket_request_failed",
                                "message": _response_message(decoded, response_body),
                                "request_id": request_id,
                                "elapsed_ms": round((time.monotonic() - started) * 1000),
                                "body": response_body,
                            }
                            if isinstance(response_body, Mapping):
                                result.update(
                                    {
                                        key: value
                                        for key, value in response_body.items()
                                        if key not in {"success", "message", "error"}
                                    }
                                )
                            return result
                        if message.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            break
        except asyncio.TimeoutError:
            return {
                "success": False,
                "code": "plugin_websocket_timeout",
                "message": "插件 WebSocket 连接超时，未确认请求结果。",
            }
        except (aiohttp.ClientError, OSError) as exc:
            logger.warning("插件 WebSocket 连接失败 host=%s error_type=%s", self.host, type(exc).__name__)
            return {
                "success": False,
                "code": "plugin_websocket_connect_failed",
                "message": f"插件 WebSocket 无法连接：{type(exc).__name__}。",
            }
        return {
            "success": False,
            "code": "plugin_websocket_closed",
            "message": "插件 WebSocket 在返回结果前关闭了连接。",
        }

    async def test_connection(self) -> dict[str, Any]:
        """Verify the documented gateway with GET /ping before game commands."""

        result = await self._request("GET", "/ping")
        if result.get("success"):
            result["code"] = "plugin_websocket_connected"
            result["message"] = result.get("message") or "插件 WebSocket 已连接，/ping 验证成功。"
        return result

    async def execute_command(
        self,
        command: str,
        *,
        steam_id: str = "",
        request_key: str = "",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        command_text = str(command or "").strip()
        if not command_text:
            return {
                "success": False,
                "code": "game_command_invalid",
                "message": "游戏指令为空，未执行。",
            }
        target_steam_id = str(steam_id or "").strip()
        payload = {
            "mode": "command",
            "command": command_text,
            "commands": [command_text],
            "target": "player" if target_steam_id else "global",
            "steamId": target_steam_id,
            "steamID": target_steam_id,
            "steam_id": target_steam_id,
            "timeoutSeconds": _bounded_timeout(timeout_seconds or self.timeout),
            "bypassPluginCompat": True,
            "requestKey": str(request_key or f"req-{uuid4().hex[:20]}")[:96],
        }
        return await self._request("POST", "/run-command", payload)


async def test_plugin_websocket_connection(config: Mapping[str, Any]) -> dict[str, Any]:
    client = PluginWebSocketClient(
        config.get("host", ""),
        config.get("port", 0),
        config.get("api_key", ""),
        path=config.get("websocket_path", PLUGIN_WS_PATH),
        scheme=config.get("websocket_scheme", "ws"),
        timeout=config.get("timeout_seconds", 8),
    )
    return await client.test_connection()


async def execute_plugin_websocket_command(
    config: Mapping[str, Any],
    command: str,
    *,
    steam_id: str = "",
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    client = PluginWebSocketClient(
        config.get("host", ""),
        config.get("port", 0),
        config.get("api_key", ""),
        path=config.get("websocket_path", PLUGIN_WS_PATH),
        scheme=config.get("websocket_scheme", "ws"),
        timeout=config.get("timeout_seconds", 8),
    )
    if not client.api_key:
        return {
            "success": False,
            "code": "plugin_websocket_key_missing",
            "message": "插件 WebSocket API Key 未配置，指令未执行。",
        }
    return await client.execute_command(
        command,
        steam_id=steam_id,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "MAX_PAYLOAD_BYTES",
    "PLUGIN_WS_PATH",
    "PluginWebSocketClient",
    "execute_plugin_websocket_command",
    "parse_plugin_websocket_url",
    "plugin_websocket_url",
    "test_plugin_websocket_connection",
]
