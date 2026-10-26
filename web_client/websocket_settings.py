"""Portal-only settings for the authenticated game WebSocket."""

from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from .models import PortalSetting
from .settings import Settings


GAME_WEBSOCKET_SETTING_KEY = "game_websocket"
DEFAULT_GAME_WEBSOCKET_SETTINGS = {
    "enabled": True,
    "game_websocket_enabled": False,
    "max_clients": 64,
    "call_timeout": 20.0,
    "portal_url": "",
    "endpoint_path": "/api/sync/v1/ws",
}

DEFAULT_GAME_EXECUTOR_SETTINGS = {
    "enabled": False,
    "type": "plugin_websocket",
    "host": "127.0.0.1",
    "port": 28016,
    "api_key": "",
    "websocket_scheme": "ws",
    "timeout_seconds": 10.0,
}


def game_websocket_user_view(
    value: object,
    *,
    group_authorized: bool | None = None,
    executor_available: bool = False,
    local_mode: bool = False,
) -> dict[str, Any]:
    """Build the safe transport view exposed by a customer group page.

    Backend credentials, plugin WebSocket details and the sync token stay in the
    admin-only settings object. A customer only needs to know whether the
    group can select the backend transport and why it is unavailable.
    """

    raw = value if isinstance(value, Mapping) else {}
    backend_configured = bool(
        game_websocket_runtime_enabled(raw)
        and raw.get("game_websocket_token_configured", raw.get("token_configured"))
    )
    if local_mode:
        reason = "当前网页与 Windows 本地直连，游戏功能由本地执行。"
    elif not backend_configured:
        reason = "后台 WebSocket 尚未完成开关和游戏入口令牌配置。"
    elif group_authorized is False:
        reason = "本群游戏卡密心跳未生效，暂不能调用后台执行器。"
    elif not executor_available:
        reason = "本群没有可用服务器档案，后台也没有启用全局执行器。"
    else:
        reason = "用户端选择 WebSocket 后，由 Windows 调用后台已配置的游戏执行器。"
    callable_backend = bool(
        not local_mode
        and backend_configured
        and executor_available
        and group_authorized is not False
    )
    return {
        "backend_configured": backend_configured,
        "group_authorized": group_authorized is not False,
        "executor_available": bool(executor_available),
        "callable": callable_backend,
        "endpoint_path": str(raw.get("endpoint_path", "/api/sync/v1/ws")),
        "reason": reason,
        "source": "本群服务器档案" if executor_available else "后台全局执行器",
        "modes": (
            {"value": "local", "label": "Windows 本地执行", "available": True},
            {
                "value": "websocket",
                "label": "调用后台 WebSocket",
                "available": callable_backend,
            },
        ),
    }

_WEBSOCKET_PATHS = frozenset({"/api/sync/v1/ws", "/api/game/v1/ws"})


def _fernet(settings: Settings) -> Fernet:
    key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.session_secret.encode("utf-8")).digest()
    )
    return Fernet(key)


def _decrypt_token(ciphertext: object, settings: Settings | None) -> str:
    if not ciphertext or settings is None:
        return ""
    try:
        return _fernet(settings).decrypt(str(ciphertext).encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError):
        return ""


def _encrypt_token(token: str, settings: Settings) -> str:
    return _fernet(settings).encrypt(token.encode("utf-8")).decode("ascii")


def _read_settings_fallback() -> Settings | None:
    try:
        return Settings.from_env()
    except (RuntimeError, ValueError):
        return None


def _normalize_portal_url(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or len(url) > 2048
    ):
        raise ValueError("网页地址必须是 http(s) 地址，且不能包含路径、查询参数或片段")
    return url


def _normalize_endpoint_path(value: object) -> str:
    path = str(value or "").strip() or DEFAULT_GAME_WEBSOCKET_SETTINGS["endpoint_path"]
    if path not in _WEBSOCKET_PATHS:
        raise ValueError("WebSocket 路径必须是 /api/sync/v1/ws 或 /api/game/v1/ws")
    return path


async def probe_game_websocket_connection(
    value: object,
    *,
    timeout_seconds: float = 8.0,
    client_id: str = "linux-admin-probe",
) -> dict[str, Any]:
    """Verify the public Windows game WebSocket route and hello handshake."""

    raw = value if isinstance(value, Mapping) else {}
    base_url = _normalize_portal_url(raw.get("portal_url"))
    token = str(
        raw.get("game_websocket_token", raw.get("sync_token", "")) or ""
    ).strip()
    if not base_url:
        return {"success": False, "code": "portal_url_missing", "message": "网页公网地址未配置。"}
    if len(token) < 32:
        return {"success": False, "code": "game_websocket_token_missing", "message": "游戏 WebSocket 入口令牌未配置或长度不足 32 位。"}
    endpoint_path = _normalize_endpoint_path(raw.get("endpoint_path", "/api/sync/v1/ws"))
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme.lower() == "https" else "ws"
    endpoint = f"{scheme}://{parsed.netloc}{endpoint_path}"
    safe_client_id = re.sub(r"[^A-Za-z0-9._-]", "_", str(client_id or ""))[:64] or "linux-admin-probe"
    try:
        timeout = max(1.0, min(30.0, float(timeout_seconds or 8.0)))
    except (TypeError, ValueError, OverflowError):
        timeout = 8.0
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout, connect=min(8.0, timeout))
        async with aiohttp.ClientSession(timeout=client_timeout, trust_env=False) as client:
            async with client.ws_connect(
                endpoint,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Joker-Client": safe_client_id,
                },
                heartbeat=20,
                autoping=True,
            ) as websocket:
                await websocket.send_json(
                    {
                        "type": "hello",
                        "protocol": "joker-game-v1",
                        "client_id": safe_client_id,
                        "capabilities": [],
                    }
                )
                message = await websocket.receive()
                if message.type != aiohttp.WSMsgType.TEXT:
                    return {
                        "success": False,
                        "code": "invalid_handshake",
                        "message": "公网入口未返回有效的 WebSocket hello_ack。",
                    }
                try:
                    payload = json.loads(str(message.data))
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {}
                if isinstance(payload, Mapping) and payload.get("type") == "hello_ack":
                    return {
                        "success": True,
                        "code": "connected",
                        "message": "Windows 公网 WebSocket 连接入口和游戏入口令牌验证成功。",
                        "endpoint_path": endpoint_path,
                    }
                return {
                    "success": False,
                    "code": "invalid_handshake",
                    "message": "公网入口已连接，但协议不是 joker-game-v1。",
                }
    except aiohttp.WSServerHandshakeError as exc:
        status = int(getattr(exc, "status", 0) or 0)
        if status in {401, 403}:
            message = "游戏 WebSocket 入口令牌无效，或后台 WebSocket 开关未开启。"
        elif status == 404:
            message = "公网入口返回 404：网页服务或 Nginx 尚未配置该 WebSocket 路径。"
        else:
            message = f"公网 WebSocket 握手失败（HTTP {status or '错误'}）。"
        return {"success": False, "code": "handshake_failed", "message": message, "http_status": status}
    except asyncio.TimeoutError:
        return {"success": False, "code": "timeout", "message": "公网 WebSocket 连接超时。"}
    except aiohttp.ClientError as exc:
        return {
            "success": False,
            "code": "transport_error",
            "message": f"公网 WebSocket 无法连接（{type(exc).__name__}）。",
        }


def _normalize_game_executor(raw: object) -> dict[str, Any]:
    value = raw if isinstance(raw, Mapping) else {}
    try:
        port = int(value.get("port", 28016))
    except (TypeError, ValueError, OverflowError):
        port = 28016
    try:
        timeout = float(value.get("timeout_seconds", 10.0))
    except (TypeError, ValueError, OverflowError):
        timeout = 10.0
    enabled = value.get("enabled", False)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "on", "yes"}
    else:
        enabled = bool(enabled)
    return {
        "enabled": enabled,
        "type": "plugin_websocket",
        "host": str(value.get("host", "127.0.0.1") or "127.0.0.1").strip()[:255],
        "port": max(1, min(65535, port)),
        "api_key": str(value.get("api_key", "") or "")[:512],
        "websocket_scheme": str(value.get("websocket_scheme", "ws") or "ws").strip().lower(),
        "timeout_seconds": max(1.0, min(30.0, timeout)),
    }


def _parse_plugin_websocket_url(value: object) -> dict[str, Any]:
    """Accept only the full URL defined by the game plugin documentation."""
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if (
        not text
        or len(text) > 2048
        or parsed.scheme.lower() not in {"ws", "wss"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path != "/ws"
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
        "websocket_scheme": parsed.scheme.lower(),
    }


def _executor_display_url(executor: Mapping[str, Any]) -> str:
    if not executor.get("api_key"):
        return ""
    scheme = str(executor.get("websocket_scheme", "ws") or "ws").lower()
    host = str(executor.get("host", "") or "").strip()
    try:
        port = int(executor.get("port", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return ""
    if scheme not in {"ws", "wss"} or not host or not 1 <= port <= 65535:
        return ""
    return f"{scheme}://{host}:{port}/ws?apiKey=已保存"


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "on", "yes"}:
            return True
        if normalized in {"", "0", "false", "off", "no"}:
            return False
        return default
    return default if value is None else bool(value)


def game_websocket_runtime_enabled(value: object) -> bool:
    """Return whether the backend is allowed to serve game WebSocket calls."""

    raw = value if isinstance(value, Mapping) else {}
    return _as_bool(raw.get("enabled", True), True) and _as_bool(
        raw.get("game_websocket_enabled", False), False
    )


def get_game_websocket_settings(
    db: Session, settings: Settings | None = None
) -> dict[str, Any]:
    setting = db.get(PortalSetting, GAME_WEBSOCKET_SETTING_KEY)
    raw = setting.value_json if setting and isinstance(setting.value_json, Mapping) else {}
    current_settings = settings or _read_settings_fallback()
    # ``sync_token_ciphertext`` was the old, ambiguous field name.  It is
    # read only as a migration fallback and is never allowed to replace the
    # HTTP portal sync token from ``JOKER_WEB_SYNC_TOKEN``.
    game_websocket_token = _decrypt_token(
        raw.get("game_websocket_token_ciphertext"), current_settings
    ) or _decrypt_token(raw.get("sync_token_ciphertext"), current_settings)
    portal_sync_token_configured = bool(
        str(current_settings.sync_token).strip() if current_settings else ""
    )
    try:
        max_clients = int(raw.get("max_clients", 64))
    except (TypeError, ValueError, OverflowError):
        max_clients = 64
    try:
        call_timeout = float(raw.get("call_timeout", 20.0))
    except (TypeError, ValueError, OverflowError):
        call_timeout = 20.0
    raw_executor = raw.get("game_executor")
    raw_executor = raw_executor if isinstance(raw_executor, Mapping) else {}
    executor = _normalize_game_executor(raw_executor)
    executor["api_key"] = _decrypt_token(
        raw_executor.get("api_key_ciphertext"), current_settings
    )
    return {
        "enabled": _as_bool(raw.get("enabled", True), True),
        "game_websocket_enabled": _as_bool(
            raw.get("game_websocket_enabled", False)
        ),
        "max_clients": max(1, min(256, max_clients)),
        "call_timeout": max(0.1, min(120.0, call_timeout)),
        "portal_url": str(raw.get("portal_url", "") or "").strip().rstrip("/"),
        "endpoint_path": (
            str(raw.get("endpoint_path", "") or "").strip()
            if str(raw.get("endpoint_path", "") or "").strip() in _WEBSOCKET_PATHS
            else DEFAULT_GAME_WEBSOCKET_SETTINGS["endpoint_path"]
        ),
        "game_websocket_token": game_websocket_token,
        "game_websocket_token_configured": bool(game_websocket_token),
        "portal_sync_token_configured": portal_sync_token_configured,
        # Compatibility aliases for callers released before the two token
        # channels were split.  They refer to the game entry token only.
        "sync_token": game_websocket_token,
        "token_configured": bool(game_websocket_token),
        "game_executor": executor,
        "game_executor_api_key_configured": bool(executor["api_key"]),
        "game_executor_display_url": _executor_display_url(executor),
    }


def save_game_websocket_settings(
    db: Session,
    *,
    enabled: bool,
    game_websocket_enabled: object | None = None,
    max_clients: object,
    call_timeout: object,
    portal_url: object = "",
    endpoint_path: object = "/api/sync/v1/ws",
    sync_token: object = "",
    game_websocket_token: object | None = None,
    game_executor_enabled: object | None = None,
    game_executor_timeout_seconds: object | None = None,
    game_executor_websocket_url: object | None = None,
    settings: Settings | None = None,
    operator_id: int | None = None,
    auto_generate_game_websocket_token: bool = False,
) -> dict[str, Any]:
    try:
        clients = int(str("64" if max_clients is None else max_clients))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("WebSocket 最大连接数必须是整数") from None
    if not 1 <= clients <= 256:
        raise ValueError("WebSocket 最大连接数必须在 1-256 之间")
    try:
        timeout = float(str("20" if call_timeout is None else call_timeout))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("WebSocket 调用超时必须是数字") from None
    if not 0.1 <= timeout <= 120:
        raise ValueError("WebSocket 调用超时必须在 0.1-120 秒之间")
    normalized_url = _normalize_portal_url(portal_url)
    normalized_path = _normalize_endpoint_path(endpoint_path)
    current_settings = settings or _read_settings_fallback()
    setting = db.get(PortalSetting, GAME_WEBSOCKET_SETTING_KEY)
    previous = get_game_websocket_settings(db, current_settings)
    # ``sync_token`` remains accepted for older callers, but it is now only
    # an alias for the dedicated game WebSocket entry token.
    supplied_token = str(
        game_websocket_token if game_websocket_token is not None else sync_token
        or ""
    ).strip()
    if (
        not supplied_token
        and auto_generate_game_websocket_token
        and not previous.get("game_websocket_token")
    ):
        supplied_token = secrets.token_hex(36)
    if supplied_token and len(supplied_token) < 32:
        raise ValueError("游戏 WebSocket 入口令牌至少需要 32 位")
    payload = {
        "enabled": bool(enabled),
        "max_clients": clients,
        "call_timeout": timeout,
        "portal_url": normalized_url,
        "endpoint_path": normalized_path,
    }
    previous_executor = previous["game_executor"]
    try:
        executor_timeout = float(
            previous_executor["timeout_seconds"]
            if game_executor_timeout_seconds is None
            else game_executor_timeout_seconds
        )
    except (TypeError, ValueError, OverflowError):
        raise ValueError("游戏执行器超时必须是数字") from None
    if not 1 <= executor_timeout <= 30:
        raise ValueError("游戏执行器超时必须在 1-30 秒之间")
    supplied_executor_url = (
        "" if game_executor_websocket_url is None else str(game_executor_websocket_url).strip()
    )
    parsed_executor_url = (
        _parse_plugin_websocket_url(supplied_executor_url)
        if supplied_executor_url
        else {}
    )
    enabled_executor = (
        previous_executor["enabled"]
        if game_executor_enabled is None
        else _as_bool(game_executor_enabled)
    )
    if enabled_executor and not (parsed_executor_url or previous_executor.get("api_key")):
        raise ValueError(
            "默认游戏插件已开启，但没有有效链接。请填写 "
            "ws(s)://游戏主机:端口/ws?apiKey=插件APIKey；如果使用分群服务器档案，"
            "请关闭默认连接后到对应群的“游戏服务器”中配置。"
        )
    executor_payload = {
        "enabled": enabled_executor,
        "type": "plugin_websocket",
        "host": parsed_executor_url.get("host", previous_executor["host"]),
        "port": parsed_executor_url.get("port", previous_executor["port"]),
        "websocket_scheme": parsed_executor_url.get(
            "websocket_scheme", previous_executor.get("websocket_scheme", "ws")
        ),
        "timeout_seconds": executor_timeout,
    }
    previous_raw_executor = setting
    previous_raw_executor = (
        previous_raw_executor.value_json.get("game_executor", {})
        if previous_raw_executor is not None
        and isinstance(previous_raw_executor.value_json, Mapping)
        else {}
    )
    previous_raw_executor = (
        previous_raw_executor if isinstance(previous_raw_executor, Mapping) else {}
    )
    if parsed_executor_url:
        if current_settings is None:
            raise ValueError("网页加密配置不可用，请检查 session secret")
        executor_payload["api_key_ciphertext"] = _encrypt_token(
            str(parsed_executor_url["api_key"]), current_settings
        )
    else:
        previous_ciphertext = str(
            previous_raw_executor.get("api_key_ciphertext", "") or ""
        )
        if previous_ciphertext:
            executor_payload["api_key_ciphertext"] = previous_ciphertext
    payload["game_websocket_enabled"] = (
        previous["game_websocket_enabled"]
        if game_websocket_enabled is None
        else str(game_websocket_enabled).lower() in {"on", "1", "true", "yes"}
    )
    payload["game_executor"] = executor_payload
    if supplied_token:
        if current_settings is None:
            raise ValueError("网页加密配置不可用，请检查 session secret")
        payload["game_websocket_token_ciphertext"] = _encrypt_token(
            supplied_token, current_settings
        )
    elif setting is not None and isinstance(setting.value_json, Mapping):
        previous_ciphertext = str(
            setting.value_json.get("game_websocket_token_ciphertext", "")
            or setting.value_json.get("sync_token_ciphertext", "")
            or ""
        )
        if previous_ciphertext:
            payload["game_websocket_token_ciphertext"] = previous_ciphertext
    if setting is None:
        db.add(PortalSetting(key=GAME_WEBSOCKET_SETTING_KEY, value_json=payload, updated_by=operator_id))
    else:
        setting.value_json = payload
        setting.updated_by = operator_id
    return get_game_websocket_settings(db, current_settings)


__all__ = [
    "DEFAULT_GAME_EXECUTOR_SETTINGS",
    "DEFAULT_GAME_WEBSOCKET_SETTINGS",
    "GAME_WEBSOCKET_SETTING_KEY",
    "game_websocket_user_view",
    "game_websocket_runtime_enabled",
    "get_game_websocket_settings",
    "probe_game_websocket_connection",
    "save_game_websocket_settings",
]
