"""Windows-side client for the portal game WebSocket execution channel."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp


logger = logging.getLogger(__name__)
GAME_WS_PROTOCOL = "joker-game-v1"
MAX_PAYLOAD_BYTES = 512 * 1024


def _handshake_error_details(exc: BaseException, endpoint_path: str = "") -> tuple[str, str]:
    """Convert a WebSocket HTTP handshake error into safe operator guidance."""
    status = int(getattr(exc, "status", 0) or 0)
    path = str(endpoint_path or "/api/sync/v1/ws").strip()
    if status in {401, 403}:
        return (
            f"game_websocket_handshake_http_{status}",
            "Linux 游戏 WebSocket 入口令牌无效，或网页游戏通道未开启；请等待网页同步完成后重试。",
        )
    if status == 404:
        return (
            "game_websocket_handshake_http_404",
            f"Linux WebSocket 路径不存在（{path}）；请先更新网页包并执行 joker-webctl --apply-nginx，"
            "确认 443 站点已转发 WebSocket Upgrade。Windows 会自动尝试兼容路径 /api/game/v1/ws。",
        )
    if status in {502, 503, 504}:
        return (
            f"game_websocket_handshake_http_{status}",
            f"Linux 游戏 WebSocket 上游返回 HTTP {status}（路径 {path}）；"
            "通常是 joker-web 正在重启、Nginx 配置未重载或公网反代未连到 8765。"
            "请在 Linux 执行 joker-webctl --check && joker-webctl --apply-nginx，"
            "然后 systemctl restart joker-web。Windows 会自动重试。",
        )
    if status == 400:
        return (
            "game_websocket_handshake_http_400",
            "反向代理拒绝了 WebSocket 升级，请开启 Upgrade 和 Connection 转发。",
        )
    if status == 525:
        return (
            "game_websocket_handshake_http_525",
            "CDN 到 Linux 源站的 TLS 握手失败，请检查源站证书和回源 HTTPS 配置。",
        )
    if status:
        return (
            f"game_websocket_handshake_http_{status}",
            f"Linux 游戏 WebSocket 握手被 HTTP {status} 拒绝，请检查公网入口和反向代理。",
        )
    return (
        "game_websocket_handshake_failed",
        "游戏 WebSocket 握手失败，网页入口没有返回有效的 HTTP 状态。",
    )


def websocket_url(
    base_url: object, endpoint_path: object = "/api/sync/v1/ws"
) -> str:
    """Convert the configured HTTPS portal origin to the selected WS endpoint."""
    parsed = urlsplit(str(base_url or "").strip().rstrip("/"))
    scheme = {"https": "wss", "http": "ws"}.get(parsed.scheme.lower(), "")
    path = str(endpoint_path or "/api/sync/v1/ws").strip()
    if path not in {"/api/sync/v1/ws", "/api/game/v1/ws"}:
        return ""
    if not scheme or not parsed.netloc or parsed.query or parsed.fragment:
        return ""
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


class GameWebSocketClient:
    """Reconnectable worker connection with a small reviewed operation hook."""

    def __init__(
        self,
        base_url: str,
        token: str,
        client_id: str,
        executor: Callable[..., Any] | None = None,
        connection_tester: Callable[..., Any] | None = None,
        *,
        endpoint_path: str = "/api/sync/v1/ws",
        reconnect_seconds: float = 5.0,
        capabilities: Iterable[str] | None = None,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.token = str(token or "").strip()
        self.client_id = str(client_id or "").strip()
        self.endpoint_path = str(endpoint_path or "/api/sync/v1/ws").strip()
        # ``endpoint_path`` is configuration.  A compatibility route may be
        # used for one connection attempt, but must never overwrite the
        # configured path and hide a fixed Linux deployment on the next sync.
        self.last_endpoint_path = ""
        self.executor = executor
        self.connection_tester = connection_tester
        self.reconnect_seconds = max(2.0, min(120.0, float(reconnect_seconds or 5.0)))
        base_capabilities = {
            "execute_game_command",
            "game.execute_game_command",
            "steam_reward",
            "game.steam_reward",
            "test_game_connection",
            "game.test_game_connection",
        }
        self.capabilities = tuple(
            sorted(
                base_capabilities.union(
                    str(value).strip().lower()
                    for value in (capabilities or ())
                    if str(value or "").strip()
                )
            )
        )
        self._stop_event = asyncio.Event()
        self._session: aiohttp.ClientSession | None = None
        self._websocket: aiohttp.ClientWebSocketResponse | None = None
        self.last_connected_at = 0.0
        self.last_error = ""
        self.last_error_detail = ""
        self._last_failure_log_key = ""
        self._last_failure_log_at = 0.0
        # Windows Proactor can report a remote WebSocket reset from a delayed
        # transport callback after aiohttp has already handled the disconnect.
        # Keep a short-lived loop handler so WinError 10054 does not become an
        # uncaught traceback while the reconnect worker is still healthy.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._previous_loop_exception_handler = None
        self._loop_exception_handler_installed = False

    @staticmethod
    def _is_remote_reset(exc: BaseException | None) -> bool:
        if not isinstance(exc, ConnectionResetError):
            return False
        return bool(
            getattr(exc, "winerror", None) == 10054
            or getattr(exc, "errno", None) in {10054, 104}
        )

    def _loop_exception_handler(self, loop, context: dict) -> None:
        """Ignore only delayed socket-reset callbacks for this game channel."""
        exc = context.get("exception") if isinstance(context, dict) else None
        callback_message = str(context.get("message", "")) if isinstance(context, dict) else ""
        is_transport_close = (
            "_call_connection_lost" in callback_message
            or "connection_lost" in callback_message
        )
        if self._is_remote_reset(exc) and is_transport_close:
            logger.debug(
                "游戏 WebSocket 远端已断开，忽略 Windows 传输层关闭回调: %s",
                str(exc),
            )
            return
        previous = self._previous_loop_exception_handler
        if callable(previous):
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    def _install_loop_exception_handler(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop_exception_handler_installed and self._loop is loop:
            return
        self._loop = loop
        self._previous_loop_exception_handler = loop.get_exception_handler()
        loop.set_exception_handler(self._loop_exception_handler)
        self._loop_exception_handler_installed = True

    def _restore_loop_exception_handler(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed() or not self._loop_exception_handler_installed:
            return
        current = loop.get_exception_handler()
        if current == self._loop_exception_handler:
            loop.set_exception_handler(self._previous_loop_exception_handler)
        self._loop_exception_handler_installed = False
        self._previous_loop_exception_handler = None
        self._loop = None

    def _log_connection_failure(self) -> None:
        """Avoid flooding the Windows console while a bad endpoint is retried."""

        key = f"{self.last_error}\n{self.last_error_detail}"
        now = asyncio.get_running_loop().time()
        if key == self._last_failure_log_key and now - self._last_failure_log_at < 60:
            return
        logger.warning(
            "游戏 WebSocket 连接失败 code=%s detail=%s",
            self.last_error,
            self.last_error_detail,
        )
        self._last_failure_log_key = key
        self._last_failure_log_at = now

    @staticmethod
    def _connection_failure_details(
        exc: BaseException, endpoint_path: str
    ) -> tuple[str, str]:
        """Return an actionable error without exposing connection credentials."""
        path = str(endpoint_path or "/api/sync/v1/ws").strip()
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return (
                "game_websocket_connect_timeout",
                f"连接 Linux 游戏 WebSocket 超时（路径 {path}）；请检查堡塔反向代理、端口和 WebSocket Upgrade。",
            )
        if isinstance(exc, ConnectionResetError):
            if GameWebSocketClient._is_remote_reset(exc):
                return (
                    "game_websocket_remote_reset",
                    f"Linux 游戏 WebSocket 已主动断开连接（路径 {path}）；Windows 将自动重连。",
                )
            return (
                "game_websocket_connection_reset",
                f"游戏 WebSocket 连接被重置（路径 {path}）；Windows 将自动重连。",
            )
        if isinstance(exc, aiohttp.ClientConnectorError):
            return (
                "game_websocket_connector_failed",
                f"无法连接 Linux 游戏 WebSocket（路径 {path}）；请检查域名、端口、证书和堡塔反向代理。",
            )
        return (
            type(exc).__name__ or "game_websocket_connection_failed",
            f"游戏 WebSocket 连接异常（路径 {path}），Windows 将自动重连。",
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self.token
            and self.client_id
            and websocket_url(self.base_url, self.endpoint_path)
        )

    async def run_forever(self) -> None:
        if not self.enabled:
            self.last_error = "WebSocket 配置不完整"
            self.last_error_detail = (
                "游戏 WebSocket 配置不完整，请等待网页同步入口、令牌和客户端编号。"
            )
            return
        self._install_loop_exception_handler()
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        # Keep the TCP/TLS connector alive across reconnects.  A dropped
        # WebSocket should not force another DNS and TLS handshake every time.
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self._session = session
            try:
                while not self._stop_event.is_set():
                    endpoint_path = self.endpoint_path
                    try:
                        await self._run_once(session, endpoint_path)
                    except asyncio.CancelledError:
                        raise
                    except aiohttp.WSServerHandshakeError as exc:
                        status = int(getattr(exc, "status", 0) or 0)
                        if status in {404, 502, 503, 504} and endpoint_path == "/api/sync/v1/ws":
                            fallback_path = "/api/game/v1/ws"
                            try:
                                await self._run_once(session, fallback_path)
                                self.last_endpoint_path = fallback_path
                                logger.debug(
                                    "游戏 WebSocket 已通过兼容路径连接：%s",
                                    fallback_path,
                                )
                                continue
                            except asyncio.CancelledError:
                                raise
                            except aiohttp.WSServerHandshakeError as fallback_exc:
                                self.last_error, self.last_error_detail = (
                                    _handshake_error_details(
                                        fallback_exc, fallback_path
                                    )
                                )
                                self.last_error_detail = (
                                    f"主路径 /api/sync/v1/ws（HTTP {status}）与兼容路径 {fallback_path} 均未连接；"
                                    f"{self.last_error_detail}"
                                )
                            except Exception:
                                self.last_error = "game_websocket_connection_failed"
                                self.last_error_detail = (
                                    f"主路径 /api/sync/v1/ws 返回 HTTP {status}，兼容路径 {fallback_path} 连接异常；"
                                    "请检查 Linux 网页服务和反向代理。"
                                )
                        else:
                            self.last_error, self.last_error_detail = (
                                _handshake_error_details(exc, endpoint_path)
                            )
                        self._log_connection_failure()
                    except Exception as exc:
                        self.last_error, self.last_error_detail = (
                            self._connection_failure_details(exc, endpoint_path)
                        )
                        self._log_connection_failure()
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=self.reconnect_seconds
                        )
                    except asyncio.TimeoutError:
                        continue
            finally:
                self._websocket = None
                self._session = None

    async def _run_once(
        self, session: aiohttp.ClientSession, endpoint_path: str | None = None
    ) -> None:
        endpoint_path = str(endpoint_path or self.endpoint_path).strip()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Joker-Client": self.client_id,
        }
        async with session.ws_connect(
            websocket_url(self.base_url, endpoint_path),
            headers=headers,
            heartbeat=20.0,
        ) as ws:
            self._websocket = ws
            self.last_endpoint_path = endpoint_path
            self.last_connected_at = asyncio.get_running_loop().time()
            self.last_error = ""
            self.last_error_detail = ""
            await ws.send_json(
                {
                    "type": "hello",
                    "protocol": GAME_WS_PROTOCOL,
                    "client_id": self.client_id,
                    "capabilities": list(self.capabilities),
                }
            )
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(ws, message.data)
                elif message.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                }:
                    break
        self._websocket = None

    async def _handle_message(
        self, websocket: aiohttp.ClientWebSocketResponse, raw: str
    ) -> None:
        try:
            if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                return
            message = json.loads(raw)
        except (UnicodeError, ValueError, json.JSONDecodeError):
            return
        if not isinstance(message, Mapping):
            return
        message_type = str(message.get("type", "")).strip().lower()
        if message_type == "ping":
            await websocket.send_json({"type": "ping"})
            return
        if message_type != "call":
            return
        request_id = str(message.get("request_id", "")).strip()[:80]
        operation = str(message.get("operation", "")).strip().lower()
        payload = message.get("payload")
        try:
            result = await self._execute(operation, payload)
            if isinstance(result, Mapping) and result.get("success") is False:
                await websocket.send_json(
                    {
                        "type": "result",
                        "request_id": request_id,
                        "ok": False,
                        "error": {
                            "code": str(result.get("code", "remote_error"))[:64],
                            "detail": str(
                                result.get("message", "Windows 游戏执行端返回失败")
                            )[:255],
                        },
                    }
                )
            else:
                await websocket.send_json(
                    {
                        "type": "result",
                        "request_id": request_id,
                        "ok": True,
                        "result": result,
                    }
                )
        except Exception as exc:
            await websocket.send_json(
                {
                    "type": "result",
                    "request_id": request_id,
                    "ok": False,
                    "error": {
                        "code": "executor_failed",
                        "detail": "Windows 游戏执行适配器失败",
                        "type": type(exc).__name__,
                    },
                }
            )

    async def _execute(self, operation: str, payload: Any) -> Any:
        if operation not in {
            "execute_game_command",
            "game.execute_game_command",
            "steam_reward",
            "game.steam_reward",
            "test_game_connection",
            "game.test_game_connection",
        }:
            return {"success": False, "code": "unsupported_operation"}
        if operation in {"test_game_connection", "game.test_game_connection"}:
            if not callable(self.connection_tester):
                return {"success": False, "code": "executor_unavailable"}
            payload = payload if isinstance(payload, Mapping) else {}
            result = self.connection_tester(
                group_id=str(payload.get("group_id", "") or ""),
                profile_id=str(payload.get("profile_id", "") or ""),
                operation=operation,
            )
            if inspect.isawaitable(result):
                result = await result
            # A game executor must return an explicit result object.  Treating
            # ``None`` or a bare scalar as success can send a Steam reward
            # reply even though the plugin WebSocket operation was not confirmed.
            if not isinstance(result, Mapping):
                return {
                    "success": False,
                    "code": "invalid_executor_response",
                    "message": "游戏连接检测未返回可确认的结果。",
                }
            return dict(result)
        if not callable(self.executor):
            return {"success": False, "code": "executor_unavailable"}
        payload = payload if isinstance(payload, Mapping) else {}
        command = str(payload.get("command", payload.get("rendered_command", "")) or "")
        result = self.executor(
            command,
            group_id=str(payload.get("group_id", "") or ""),
            profile_id=str(payload.get("profile_id", "") or ""),
            user_id=str(payload.get("user_id", "") or ""),
            steam_id=str(payload.get("steam_id", "") or ""),
            operation=operation,
        )
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            return {
                "success": False,
                "code": "invalid_executor_response",
                "message": "游戏执行端未返回可确认的执行结果。",
            }
        return dict(result)

    async def call_via_portal(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Ask the portal to route one call to this connected worker."""
        if not self.enabled:
            return {"success": False, "code": "websocket_unavailable"}
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError, OverflowError):
            return {"success": False, "code": "invalid_timeout", "message": "游戏操作超时时间无效。"}
        if not math.isfinite(timeout_value) or not 0.1 <= timeout_value <= 120:
            return {"success": False, "code": "invalid_timeout", "message": "游戏操作超时时间无效。"}
        url = f"{self.base_url}/api/sync/v1/game/call"
        request_timeout = aiohttp.ClientTimeout(total=timeout_value + 5, connect=8)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Joker-Client": self.client_id,
        }
        session = self._session
        owns_session = session is None or session.closed
        if owns_session:
            session = aiohttp.ClientSession(timeout=request_timeout)
        try:
            async with session.post(
                url,
                headers=headers,
                timeout=request_timeout,
                json={
                    "client_id": self.client_id,
                    "operation": operation,
                    "timeout": timeout_value,
                    "payload": dict(payload),
                },
            ) as response:
                try:
                    body = await response.json(content_type=None)
                except (ValueError, TypeError):
                    body = {}
                if response.status >= 400:
                    detail = (
                        str(
                            body.get("detail", body.get("message", ""))
                            or ""
                        ).strip()[:255]
                        if isinstance(body, Mapping)
                        else ""
                    )
                    return {
                        "success": False,
                        "code": str(body.get("code", "websocket_call_failed"))[:64]
                        if isinstance(body, Mapping)
                        else "websocket_call_failed",
                        "message": detail,
                    }
                result = body.get("result") if isinstance(body, Mapping) else body
                if not isinstance(result, Mapping):
                    return {
                        "success": False,
                        "code": "invalid_executor_response",
                        "message": "网页未返回可确认的游戏执行结果。",
                    }
                return dict(result)
        except asyncio.TimeoutError:
            return {
                "success": False,
                "code": "websocket_timeout",
                "message": "游戏 WebSocket 响应超时，无法确认指令是否完成。",
            }
        except aiohttp.ClientError:
            return {
                "success": False,
                "code": "websocket_transport_failed",
                "message": "游戏 WebSocket 网络连接失败，指令未执行。",
            }
        finally:
            if owns_session and session is not None and not session.closed:
                await session.close()

    async def close(self) -> None:
        self._stop_event.set()
        websocket = self._websocket
        if websocket is not None and not websocket.closed:
            try:
                await websocket.close(code=1000)
            except Exception:
                pass
        session = self._session
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:
                pass
        # aiohttp/Proactor may deliver the final connection_lost callback on
        # the next loop turn.  Keep the narrow reset filter active briefly so
        # that callback cannot reappear as an uncaught WinError 10054 trace.
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_later(1.0, self._restore_loop_exception_handler)
        else:
            self._restore_loop_exception_handler()


__all__ = ["GameWebSocketClient", "websocket_url", "_handshake_error_details"]
