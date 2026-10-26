"""Small Socket.IO client for the Linux-configured im3 message bus."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

import aiohttp
from astrbot.api import logger


IM3_ACCESS_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def normalize_im3_socketio_config(value: object) -> dict[str, str | bool]:
    raw = value if isinstance(value, Mapping) else {}
    server_url = str(raw.get("server_url", "https://im3.vipscum.cn") or "").strip().rstrip("/")
    parsed = urlsplit(server_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        server_url = ""
    socketio_path = str(raw.get("socketio_path", "/socket.io") or "/socket.io").strip().rstrip("/")
    if not socketio_path.startswith("/") or "?" in socketio_path or "#" in socketio_path:
        socketio_path = "/socket.io"
    socketio_path = (socketio_path or "/socket.io") + "/"
    bot_type = str(raw.get("bot_type", "bot_main") or "bot_main").strip()[:128]
    key = re.sub(r"\s+", "", str(raw.get("key", "") or "")).strip().lower()
    if key and not IM3_ACCESS_KEY_RE.fullmatch(key):
        key = ""
    return {
        "enabled": bool(raw.get("enabled", False)),
        "server_url": server_url,
        "socketio_path": socketio_path or "/socket.io",
        "bot_type": bot_type or "bot_main",
        "key": key,
    }


def _join_room_response_rejected(value: object) -> bool:
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
    return isinstance(payload, Mapping) and (
        payload.get("ok") is False or payload.get("success") is False
    )


def _join_room_response_confirmed(value: object, bot_type: str) -> bool:
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
    return bool(
        str(payload.get("room", "") or "").strip()
        and (not response_type or response_type == bot_type)
    )


def _join_room_response_authorized(value: object, bot_type: str) -> bool:
    if not _join_room_response_confirmed(value, bot_type):
        return False
    try:
        packet = json.loads(str(value)[2:])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    payload = packet[1] if isinstance(packet, list) and len(packet) > 1 else None
    if not isinstance(payload, Mapping):
        return False
    for field in ("authorized", "authenticated", "valid", "key_valid", "card_valid"):
        if payload.get(field) is True:
            return True
    auth = payload.get("auth")
    return isinstance(auth, Mapping) and any(
        auth.get(field) is True
        for field in ("authorized", "authenticated", "valid", "key_valid", "card_valid")
    )


class Im3SocketIOClient:
    """Keep one documented im3 Socket.IO room connected on Windows."""

    def __init__(self, config: object, *, scope: str = "global") -> None:
        self.config = normalize_im3_socketio_config(config)
        self.scope = str(scope or "global")
        self.connected = False
        self.last_error = ""
        self._stop = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.config["enabled"] and self.config["server_url"] and self.config["key"])

    def update(self, config: object) -> None:
        self.config = normalize_im3_socketio_config(config)

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            if not self.enabled:
                self.connected = False
                await asyncio.sleep(5)
                continue
            try:
                await self._run_once()
                if not self._stop.is_set():
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = type(exc).__name__
                logger.warning(
                    "小程序 WebSocket 连接失败 scope=%s error_type=%s",
                    self.scope,
                    type(exc).__name__,
                )
                await asyncio.sleep(5)

    async def _run_once(self) -> None:
        server_url = str(self.config["server_url"])
        scheme = "wss" if server_url.startswith("https://") else "ws"
        host = server_url.split("://", 1)[1]
        path = str(self.config["socketio_path"])
        endpoint = f"{scheme}://{host}{path}?EIO=4&transport=websocket"
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(endpoint, heartbeat=20, autoping=True) as socket:
                joined = False
                join_confirmed = False
                join_deadline = 0.0
                loop = asyncio.get_running_loop()
                while not self._stop.is_set():
                    wait_timeout = 30.0
                    if joined and not join_confirmed and join_deadline:
                        wait_timeout = max(0.1, join_deadline - loop.time())
                    try:
                        message = await asyncio.wait_for(socket.receive(), wait_timeout)
                    except asyncio.TimeoutError:
                        if joined and not join_confirmed:
                            self.last_error = "socketio_join_unconfirmed"
                            self.connected = False
                            logger.warning(
                                "小程序 WebSocket 未收到 join_room 确认回执 scope=%s",
                                self.scope,
                            )
                            return
                        continue
                    if message.type != aiohttp.WSMsgType.TEXT:
                        if message.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            self.connected = False
                            self.last_error = "socketio_closed"
                            return
                        continue
                    data = str(message.data)
                    if data.startswith("0"):
                        await socket.send_str("40")
                    elif data.startswith("40") and not joined:
                        packet = "42" + json.dumps(
                            [
                                "join_room",
                                {
                                    "key": self.config["key"],
                                    "type": self.config["bot_type"],
                                },
                            ],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        await socket.send_str(packet)
                        joined = True
                        join_deadline = loop.time() + 8.0
                        self.connected = False
                        self.last_error = "socketio_join_pending"
                        logger.debug(
                            "小程序 WebSocket 已连接并提交 join_room scope=%s bot_type=%s",
                            self.scope,
                            self.config["bot_type"],
                        )
                    elif data == "2":
                        await socket.send_str("3")
                    elif data.startswith("42") and joined:
                        if _join_room_response_rejected(data):
                            self.last_error = "socketio_join_rejected"
                            self.connected = False
                            logger.warning(
                                "小程序 WebSocket join_room 被服务端拒绝 scope=%s",
                                self.scope,
                            )
                            await asyncio.sleep(5)
                            return
                        if _join_room_response_confirmed(data, str(self.config["bot_type"])):
                            authorized = _join_room_response_authorized(
                                data, str(self.config["bot_type"])
                            )
                            join_confirmed = True
                            self.connected = True
                            self.last_error = ""
                            logger.debug(
                                "小程序 WebSocket 已确认 join_room scope=%s bot_type=%s authorization=%s",
                                self.scope,
                                self.config["bot_type"],
                                "provided" if authorized else "not_provided",
                            )
                    if joined and not join_confirmed and join_deadline:
                        if loop.time() >= join_deadline:
                            self.last_error = "socketio_join_unconfirmed"
                            self.connected = False
                            logger.warning(
                                "小程序 WebSocket 未收到 join_room 确认回执 scope=%s",
                                self.scope,
                            )
                            return
                    elif data.startswith("4") and not data.startswith(("40", "42", "43")):
                        self.last_error = "socketio_server_error"
                        self.connected = False
                        logger.warning(
                            "小程序 WebSocket 收到服务端错误 scope=%s",
                            self.scope,
                        )
                self.connected = False


__all__ = ["Im3SocketIOClient", "normalize_im3_socketio_config"]
