"""Configuration and connectivity probe for the documented im3 Socket.IO bus."""

from __future__ import annotations

import json
import asyncio
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .models import PortalSetting
from .settings import Settings


IM3_SOCKETIO_SETTING_KEY = "im3_socketio"
DEFAULT_IM3_SOCKETIO_SETTINGS = {
    "enabled": False,
    "server_url": "https://im3.vipscum.cn",
    "socketio_path": "/socket.io",
    "bot_type": "bot_main",
}
IM3_ACCESS_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_AUTHORIZATION_FIELDS = (
    "authorized",
    "authenticated",
    "valid",
    "key_valid",
    "card_valid",
    "license_valid",
)
_AUTHORIZATION_CONTAINERS = (
    "auth",
    "authorization",
    "card",
    "license",
    "data",
    "result",
)


def _normalize_url(value: object) -> str:
    url = str(value or DEFAULT_IM3_SOCKETIO_SETTINGS["server_url"]).strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("im3 Socket.IO 地址必须是 http(s) 地址")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("im3 Socket.IO 地址不能包含路径、查询参数或片段")
    return url[:2048]


def _normalize_path(value: object) -> str:
    path = str(value or "/socket.io").strip()
    if not path.startswith("/") or len(path) > 128 or "?" in path or "#" in path:
        raise ValueError("Socket.IO 路径格式无效")
    path = path.rstrip("/") or "/socket.io"
    # The im3 reverse proxy redirects the directory endpoint without the
    # trailing slash. Keep it in the canonical Engine.IO form so polling and
    # WebSocket upgrades do not pay a redirect or lose handshake state.
    return path + "/"


def _normalize_bot_type(value: object) -> str:
    bot_type = str(value or "bot_main").strip()
    if bot_type != "bot_main" and not (
        bot_type.startswith("bot_child_") and len(bot_type) <= 128
    ):
        raise ValueError("机器人身份必须是 bot_main 或 bot_child_<guid>")
    return bot_type


def normalize_im3_access_key(value: object) -> str:
    """Normalize the 32-hex website card used by the im3 room service.

    The documented Socket.IO endpoint creates a room for arbitrary strings;
    it does not authenticate the card itself.  Rejecting values outside the
    website card grammar prevents a successful transport handshake from
    being presented to users as card validation.
    """

    key = re.sub(r"\s+", "", str(value or "")).strip()
    if not IM3_ACCESS_KEY_RE.fullmatch(key):
        raise ValueError("小程序 WebSocket 卡密必须是 32 位十六进制字符串")
    return key.lower()


def get_im3_socketio_settings(
    db, settings: Settings | None = None, *, include_secrets: bool = True
) -> dict[str, Any]:
    setting = db.get(PortalSetting, IM3_SOCKETIO_SETTING_KEY)
    raw = setting.value_json if setting and isinstance(setting.value_json, Mapping) else {}
    result: dict[str, Any] = {
        "enabled": bool(raw.get("enabled", False)),
        "server_url": _normalize_url(raw.get("server_url", DEFAULT_IM3_SOCKETIO_SETTINGS["server_url"])),
        "socketio_path": _normalize_path(raw.get("socketio_path", "/socket.io")),
        "bot_type": _normalize_bot_type(raw.get("bot_type", "bot_main")),
    }
    # ``key_ciphertext`` is intentionally no longer projected.  The only
    # room credential is the user-supplied website card, scoped per group.
    # Keeping the old JSON field readable here would accidentally reintroduce
    # a second backend-owned credential path.
    return result


def save_im3_socketio_settings(
    db,
    *,
    enabled: object,
    server_url: object,
    socketio_path: object = "/socket.io",
    bot_type: object = "bot_main",
    key: object = "",
    settings: Settings | None = None,
    operator_id: int | None = None,
) -> dict[str, Any]:
    setting = db.get(PortalSetting, IM3_SOCKETIO_SETTING_KEY)
    # ``key`` remains an ignored compatibility argument for older callers;
    # website room cards now belong exclusively to each user/group record.
    payload = {
        "enabled": str(enabled).strip().lower() in {"1", "true", "on", "yes"},
        "server_url": _normalize_url(server_url),
        "socketio_path": _normalize_path(socketio_path),
        "bot_type": _normalize_bot_type(bot_type),
    }
    if setting is None:
        db.add(PortalSetting(key=IM3_SOCKETIO_SETTING_KEY, value_json=payload, updated_by=operator_id))
    else:
        setting.value_json = payload
        setting.updated_by = operator_id
    return get_im3_socketio_settings(db, settings, include_secrets=False)


def _join_room_response_rejected(value: object) -> bool:
    """Detect an explicit Socket.IO rejection without requiring an optional ack."""
    text = str(value or "")
    if text.startswith("44"):
        return True
    if not text.startswith("42"):
        return False
    try:
        packet = json.loads(text[2:])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(packet, list) or not packet:
        return False
    event = str(packet[0] or "").strip().lower()
    if event in {
        "error",
        "join_error",
        "join_failed",
        "join_rejected",
        "unauthorized",
        "forbidden",
    }:
        return True
    payload = packet[1] if len(packet) > 1 else None
    if isinstance(payload, Mapping):
        return payload.get("ok") is False or payload.get("success") is False
    return False


def _join_room_response_confirmed(value: object, bot_type: str) -> bool:
    """Require an explicit positive ``joined`` response from im3."""

    text = str(value or "")
    if not text.startswith("42"):
        return False
    try:
        packet = json.loads(text[2:])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(packet, list) or len(packet) < 2:
        return False
    if str(packet[0] or "").strip().lower() != "joined":
        return False
    payload = packet[1]
    if not isinstance(payload, Mapping):
        return False
    if payload.get("ok") is False or payload.get("success") is False:
        return False
    response_type = str(payload.get("type", "") or "").strip()
    room = str(payload.get("room", "") or "").strip()
    return bool(room and (not response_type or response_type == bot_type))


def _join_room_authorization_status(value: object, bot_type: str) -> str:
    """Return the explicit card decision supplied with a ``joined`` event.

    A room being created is not evidence that the website card is valid.  The
    public im3 relay currently creates rooms for every format-valid key, so a
    customer card is accepted only when the server supplies a literal boolean
    authorization field.  A compatible im3 deployment may place that field
    directly in ``joined`` or in a documented authorization object.
    """

    if not _join_room_response_confirmed(value, bot_type):
        return "not_provided"
    try:
        packet = json.loads(str(value)[2:])
    except (TypeError, ValueError, json.JSONDecodeError):
        return "not_provided"
    payload = packet[1] if isinstance(packet, list) and len(packet) > 1 else None
    if not isinstance(payload, Mapping):
        return "not_provided"

    sources: list[Mapping] = [payload]
    for container in _AUTHORIZATION_CONTAINERS:
        nested = payload.get(container)
        if isinstance(nested, Mapping):
            sources.append(nested)

    saw_decision = False
    for source in sources:
        for field in _AUTHORIZATION_FIELDS:
            decision = source.get(field)
            if decision is True:
                return "authorized"
            if decision is False:
                saw_decision = True
    return "rejected" if saw_decision else "not_provided"


def _join_room_response_authorized(value: object, bot_type: str) -> bool:
    """Compatibility predicate for callers that only need a positive result."""

    return _join_room_authorization_status(value, bot_type) == "authorized"


async def _test_im3_socketio_websocket(
    server_url: str,
    socketio_path: str,
    bot_type: str,
    key: str,
    probe_only: bool,
    timeout: float,
    require_card_authorization: bool = False,
) -> dict[str, Any]:
    """Run the same Engine.IO WebSocket handshake used by the Windows client."""
    scheme = "wss" if server_url.startswith("https://") else "ws"
    host = server_url.split("://", 1)[1]
    endpoint = f"{scheme}://{host}{socketio_path}?EIO=4&transport=websocket"
    total_timeout = max(1.0, min(float(timeout), 30.0))
    client_timeout = aiohttp.ClientTimeout(
        total=total_timeout,
        connect=min(8.0, total_timeout),
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout
    async with aiohttp.ClientSession(timeout=client_timeout) as client:
        async with client.ws_connect(
            endpoint,
            heartbeat=20,
            autoping=True,
            autoclose=True,
        ) as socket:
            opening = await socket.receive()
            if opening.type != aiohttp.WSMsgType.TEXT:
                return {
                    "success": False,
                    "code": "im3_invalid_engineio",
                    "message": "im3 未返回文本形式的 Engine.IO 握手。",
                }
            opening_text = str(opening.data)
            if not opening_text.startswith("0"):
                return {
                    "success": False,
                    "code": "im3_invalid_engineio",
                    "message": "im3 返回的 Engine.IO 握手格式无效。",
                }
            try:
                json.loads(opening_text[1:])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("im3 Engine.IO 握手内容无法解析") from exc

            await socket.send_str("40")
            join_submitted = False
            join_deadline: float | None = None
            while True:
                now = loop.time()
                current_deadline = join_deadline or deadline
                remaining = current_deadline - now
                if remaining <= 0:
                    if join_submitted:
                        return {
                            "success": False,
                            "code": "im3_join_unconfirmed",
                            "message": "im3 已收到 join_room，但未返回有效的卡密确认回执。",
                            "server_url": server_url,
                            "bot_type": bot_type,
                            "transport": "websocket",
                            "join_request_submitted": True,
                            "ack_received": False,
                        }
                    raise asyncio.TimeoutError
                try:
                    message = await asyncio.wait_for(socket.receive(), remaining)
                except asyncio.TimeoutError:
                    if join_submitted:
                        return {
                            "success": False,
                            "code": "im3_join_unconfirmed",
                            "message": "im3 已收到 join_room，但未返回有效的卡密确认回执。",
                            "server_url": server_url,
                            "bot_type": bot_type,
                            "transport": "websocket",
                            "join_request_submitted": True,
                            "ack_received": False,
                        }
                    raise
                if message.type == aiohttp.WSMsgType.TEXT:
                    data = str(message.data)
                    if data.startswith("40"):
                        if probe_only:
                            return {
                                "success": True,
                                "code": "im3_transport_connected",
                                "message": "im3 Socket.IO WebSocket 传输层连接成功。",
                                "server_url": server_url,
                                "bot_type": bot_type,
                                "transport": "websocket",
                                "join_request_submitted": False,
                                "ack_received": False,
                            }
                        if not join_submitted:
                            await socket.send_str(
                                "42"
                                + json.dumps(
                                    [
                                        "join_room",
                                        {"key": key, "type": bot_type},
                                    ],
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                            )
                            join_submitted = True
                            join_deadline = min(deadline, loop.time() + 3.0)
                    elif data == "2":
                        await socket.send_str("3")
                    elif data.startswith("42") and join_submitted:
                        if _join_room_response_rejected(data):
                            return {
                                "success": False,
                                "code": "im3_join_rejected",
                                "message": "im3 网站拒绝了 join_room 请求，请检查网站卡密和机器人身份。",
                                "server_url": server_url,
                                "bot_type": bot_type,
                                "transport": "websocket",
                            }
                        if not _join_room_response_confirmed(data, bot_type):
                            continue
                        authorization_status = _join_room_authorization_status(
                            data, bot_type
                        )
                        card_authorized = authorization_status == "authorized"
                        if require_card_authorization and authorization_status == "rejected":
                            return {
                                "success": False,
                                "code": "im3_card_rejected",
                                "message": "im3 网站明确拒绝了该卡密，未保存。请检查卡密、机器人身份和网站授权状态。",
                                "server_url": server_url,
                                "bot_type": bot_type,
                                "transport": "websocket",
                                "join_request_submitted": True,
                                "ack_received": True,
                                "card_authorized": False,
                                "authorization_status": "rejected",
                            }
                        if require_card_authorization and not card_authorized:
                            return {
                                "success": False,
                                "code": "im3_card_authorization_unavailable",
                                "message": (
                                    "im3 已确认入房，但网站没有返回明确的卡密授权回执；"
                                    "无法安全判断该卡密是否有效，未保存。请让网站返回 authorized/card_valid 等授权字段后重试。"
                                ),
                                "server_url": server_url,
                                "bot_type": bot_type,
                                "transport": "websocket",
                                "join_request_submitted": True,
                                "ack_received": True,
                                "card_authorized": False,
                                "authorization_status": authorization_status,
                            }
                        return {
                            "success": True,
                            "code": "im3_connected",
                            "message": (
                                "im3 Socket.IO 已完成 WebSocket 握手并确认 join_room。"
                                if card_authorized
                                else "im3 Socket.IO 已完成 WebSocket 握手并确认 join_room；网站未提供卡密授权回执，已按已确认入房连接处理。"
                            ),
                            "server_url": server_url,
                            "bot_type": bot_type,
                            "transport": "websocket",
                            "join_request_submitted": True,
                            "ack_received": True,
                            "card_authorized": card_authorized,
                            "authorization_status": authorization_status,
                        }
                    elif data.startswith("44") and join_submitted:
                        return {
                            "success": False,
                            "code": "im3_join_rejected",
                            "message": "im3 网站拒绝了 join_room 请求，请检查网站卡密和机器人身份。",
                            "server_url": server_url,
                            "bot_type": bot_type,
                            "transport": "websocket",
                        }
                    elif data.startswith("43") and join_submitted:
                        # An empty Socket.IO callback is not an authorization
                        # decision. Wait for the joined event instead.
                        continue
                    elif data.startswith("4") and not data.startswith(("40", "42", "43", "44")):
                        return {
                            "success": False,
                            "code": "im3_server_error",
                            "message": "im3 Socket.IO 返回服务端错误，请检查网站配置。",
                            "server_url": server_url,
                            "bot_type": bot_type,
                            "transport": "websocket",
                        }
                elif message.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                }:
                    raise aiohttp.ClientConnectionError(
                        "im3 Socket.IO WebSocket 提前关闭"
                    )


def test_im3_socketio_connection(value: object, *, timeout: float = 10.0) -> dict[str, Any]:
    """Perform the documented Engine.IO WebSocket and join_room handshake."""
    raw = value if isinstance(value, Mapping) else {}
    server_url = _normalize_url(raw.get("server_url"))
    socketio_path = _normalize_path(raw.get("socketio_path"))
    bot_type = _normalize_bot_type(raw.get("bot_type"))
    probe_only = bool(raw.get("probe_only", False))
    require_card_authorization = bool(raw.get("require_card_authorization", False))
    key = ""
    if not probe_only:
        if not str(raw.get("key", "") or "").strip():
            return {
                "success": False,
                "code": "im3_key_missing",
                "message": "im3 Socket.IO 房间密钥未配置。",
            }
        try:
            key = normalize_im3_access_key(raw.get("key", ""))
        except ValueError as exc:
            return {
                "success": False,
                "code": "im3_key_format_invalid",
                "message": str(exc),
            }
    last_error: dict[str, Any] | None = None
    for attempt in range(3):
        try:
            result = asyncio.run(
                _test_im3_socketio_websocket(
                    server_url,
                    socketio_path,
                    bot_type,
                    key,
                    probe_only,
                    timeout,
                    require_card_authorization,
                )
            )
            if result.get("success") or result.get("code") != "im3_connection_failed":
                return result
            last_error = result
        except aiohttp.ClientResponseError as exc:
            status_code = int(getattr(exc, "status", 0) or 0)
            if status_code in {401, 403}:
                message = "im3 网站拒绝了 Socket.IO 请求，请检查网站权限、反向代理和机器人身份。"
            elif status_code == 404:
                message = "im3 Socket.IO 路径不存在，请检查后台配置的路径。"
            elif status_code >= 500:
                message = "im3 网站源站返回服务错误，请检查网站服务和反向代理。"
            else:
                message = f"im3 网站返回 HTTP {status_code or '错误'}，请检查网站地址和 Socket.IO 路径。"
            return {
                "success": False,
                "code": "im3_http_error",
                "message": message,
                "http_status": status_code,
            }
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, json.JSONDecodeError) as exc:
            last_error = {
                "success": False,
                "code": "im3_connection_failed",
                "message": f"im3 Socket.IO WebSocket 连接失败：{type(exc).__name__}。",
            }
        if attempt < 2:
            time.sleep(0.4)
    if last_error is not None:
        last_error["message"] = str(last_error.get("message", "")) + " 已自动重试 3 次。"
        return last_error
    return {
        "success": False,
        "code": "im3_connection_failed",
        "message": "im3 Socket.IO WebSocket 连接失败，已自动重试 3 次。",
    }


__all__ = [
    "DEFAULT_IM3_SOCKETIO_SETTINGS",
    "IM3_ACCESS_KEY_RE",
    "IM3_SOCKETIO_SETTING_KEY",
    "get_im3_socketio_settings",
    "save_im3_socketio_settings",
    "normalize_im3_access_key",
    "test_im3_socketio_connection",
]
