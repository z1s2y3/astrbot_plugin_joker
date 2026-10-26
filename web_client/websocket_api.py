"""Authenticated WebSocket transport for Windows game operations.

The portal remains a configuration service and does not execute game code.  A
Windows worker that connects to :func:`serve_game_websocket` executes the
operation locally (where the game database/FTP credentials live) and returns a
small JSON result.  Browser-facing code can use :class:`GameWebSocketHub` to
route an operation to the client that owns a group.

The transport deliberately has no dependency on a particular game adapter.
Operation names are namespaced and bounded, so a future SCUM/database adapter
can be added without weakening the authentication boundary.  The existing
HTTPS exchange remains the source of truth for configuration synchronization;
this channel is an optional low-latency command path.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from starlette.websockets import WebSocket, WebSocketDisconnect


logger = logging.getLogger(__name__)

GAME_WS_PROTOCOL = "joker-game-v1"
GAME_WS_CLOSE_UNAUTHORIZED = 4401
GAME_WS_CLOSE_FORBIDDEN = 4403
GAME_WS_CLOSE_BAD_REQUEST = 4400
GAME_WS_CLOSE_TIMEOUT = 4408
GAME_WS_CLOSE_DUPLICATE = 4429

MAX_OPERATION_LENGTH = 64
MAX_CLIENT_ID_LENGTH = 64
MAX_CAPABILITIES = 64
MAX_PENDING_REQUESTS = 128
MAX_MESSAGE_BYTES = 512 * 1024
DEFAULT_CALL_TIMEOUT = 20.0

_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")

# Keep the operation names explicit.  The Windows worker may dispatch these
# tokens to privileged FTP/SQLite/Steam actions, so accepting an arbitrary
# ``game.*``/``scum.*`` suffix would turn a typo (or a compromised caller)
# into an unreviewed capability.  New operations must be added here together
# with the worker implementation and its tests.
ALLOWED_GAME_OPERATIONS = frozenset(
    {
        "query_online",
        "query_keyword",
        "query_server",
        "query_server_ip",
        "search_server",
        "bind_server_id",
        "unbind_server_id",
        "scum_news",
        "get_scum_news",
        "server_info",
        "bind_steam_id",
        "unbind_steam_id",
        "bind_steam_signin_id",
        "unbind_steam_signin_id",
        "signin",
        "reset_signin_record",
        "lottery",
        "database_query",
        "database_update",
        "steam_player_lookup",
        "steam_bind",
        "steam_unbind",
        "steam_signin",
        "steam_reward",
        "steam_lottery",
        "execute_game_command",
        "test_game_connection",
        "game.query_online",
        "game.query_keyword",
        "game.query_server",
        "game.query_server_ip",
        "game.search_server",
        "game.bind_server_id",
        "game.unbind_server_id",
        "game.scum_news",
        "game.get_scum_news",
        "game.server_info",
        "game.bind_steam_id",
        "game.unbind_steam_id",
        "game.bind_steam_signin_id",
        "game.unbind_steam_signin_id",
        "game.signin",
        "game.reset_signin_record",
        "game.lottery",
        "game.database_query",
        "game.database_update",
        "game.steam_player_lookup",
        "game.steam_bind",
        "game.steam_unbind",
        "game.steam_signin",
        "game.steam_reward",
        "game.steam_lottery",
        "game.execute_game_command",
        "game.test_game_connection",
    }
)


class GameWebSocketError(RuntimeError):
    """Base error raised by the game WebSocket transport."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = str(code)
        self.detail = str(detail)


class GameWebSocketUnavailable(GameWebSocketError):
    def __init__(self, client_id: str):
        super().__init__("client_offline", f"Windows 执行端未连接：{client_id}")


class GameWebSocketTimeout(GameWebSocketError):
    def __init__(self, request_id: str):
        super().__init__("request_timeout", f"游戏操作响应超时：{request_id}")


def normalize_client_id(value: Any) -> str:
    """Validate a stable Windows client identity."""

    if not isinstance(value, str):
        raise GameWebSocketError("invalid_client", "客户端标识格式无效")
    value = value.strip()
    if not value or len(value) > MAX_CLIENT_ID_LENGTH or not _CLIENT_ID_RE.fullmatch(value):
        raise GameWebSocketError("invalid_client", "客户端标识格式无效")
    return value


def normalize_operation(value: Any) -> str:
    """Return a safe operation token accepted by the transport."""

    if not isinstance(value, str):
        raise GameWebSocketError("invalid_operation", "游戏操作名称格式无效")
    operation = value.strip().lower()
    if (
        not operation
        or len(operation) > MAX_OPERATION_LENGTH
        or not _OPERATION_RE.fullmatch(operation)
        or operation not in ALLOWED_GAME_OPERATIONS
    ):
        raise GameWebSocketError("invalid_operation", "不支持的游戏操作")
    return operation


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise GameWebSocketError("invalid_payload", "游戏操作参数必须是 JSON 数据") from exc


def normalize_payload(value: Any) -> Any:
    """Validate and bound a command payload without mutating it."""

    if value is None:
        value = {}
    if _json_size(value) > MAX_MESSAGE_BYTES:
        raise GameWebSocketError("payload_too_large", "游戏操作参数过大")
    # Round-trip through JSON catches NaN/unsupported objects while retaining
    # the caller's normal dict/list/scalar shape.
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise GameWebSocketError("invalid_payload", "游戏操作参数必须是 JSON 数据") from exc


def _error_message(error: GameWebSocketError | Exception) -> dict[str, Any]:
    if isinstance(error, GameWebSocketError):
        return {"code": error.code, "detail": error.detail}
    return {"code": "internal_error", "detail": "游戏操作执行失败"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _Connection:
    websocket: WebSocket
    client_id: str
    capabilities: frozenset[str] = frozenset()
    connected_at: str = field(default_factory=_now_iso)
    pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


class GameWebSocketHub:
    """Track authenticated Windows connections and correlate game calls."""

    def __init__(
        self,
        *,
        max_clients: int = 64,
        max_pending_requests: int = MAX_PENDING_REQUESTS,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        self.max_clients = max(1, int(max_clients))
        self.max_pending_requests = max(1, int(max_pending_requests))
        self.call_timeout = max(0.1, float(call_timeout))
        self._connections: dict[str, _Connection] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        websocket: WebSocket,
        client_id: str,
        capabilities: list[str] | tuple[str, ...] | set[str] = (),
    ) -> _Connection:
        client_id = normalize_client_id(client_id)
        if not isinstance(capabilities, (list, tuple, set, frozenset)):
            capabilities = ()
        normalized_capabilities = frozenset(
            str(item).strip().lower()[:MAX_OPERATION_LENGTH]
            for item in capabilities
            if isinstance(item, str) and str(item).strip()
        )
        async with self._lock:
            if len(self._connections) >= self.max_clients and client_id not in self._connections:
                raise GameWebSocketError("capacity", "游戏执行端连接数已达上限")
            previous = self._connections.get(client_id)
            connection = _Connection(
                websocket=websocket,
                client_id=client_id,
                capabilities=normalized_capabilities,
            )
            self._connections[client_id] = connection
        if previous is not None:
            # A reconnect wins deterministically.  Closing outside the lock
            # avoids blocking all clients on a broken network connection.
            previous.closed = True
            for future in tuple(previous.pending.values()):
                if not future.done():
                    future.set_exception(GameWebSocketUnavailable(client_id))
            try:
                await previous.websocket.close(code=GAME_WS_CLOSE_DUPLICATE)
            except Exception:
                pass
        return connection

    async def unregister(self, connection: _Connection) -> None:
        async with self._lock:
            if self._connections.get(connection.client_id) is connection:
                self._connections.pop(connection.client_id, None)
        connection.closed = True
        error = GameWebSocketUnavailable(connection.client_id)
        for future in tuple(connection.pending.values()):
            if not future.done():
                future.set_exception(error)
        connection.pending.clear()

    async def _send(self, connection: _Connection, message: Mapping[str, Any]) -> None:
        if connection.closed:
            raise GameWebSocketUnavailable(connection.client_id)
        # Serialize before taking the lock so unsupported values fail before a
        # partially written WebSocket frame can leave the peer out of sync.
        try:
            encoded = json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise GameWebSocketError("invalid_message", "游戏消息格式无效") from exc
        if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise GameWebSocketError("message_too_large", "游戏消息过大")
        async with connection.send_lock:
            # The payload is already serialized for the size check. Sending
            # the text frame avoids serializing the same JSON a second time.
            send_text = getattr(connection.websocket, "send_text", None)
            if callable(send_text):
                await send_text(encoded)
            else:
                send_json = getattr(connection.websocket, "send_json", None)
                if not callable(send_json):
                    raise GameWebSocketError("send_failed", "游戏消息发送接口不可用")
                await send_json(json.loads(encoded))

    async def send_event(self, client_id: str, event: Mapping[str, Any]) -> None:
        """Send a one-way event to a connected Windows worker."""

        connection = await self.get(client_id)
        if connection is None:
            raise GameWebSocketUnavailable(client_id)
        payload = dict(event)
        # This helper is intentionally one-way; callers must use ``call`` for
        # an operation so its allowlist and request correlation are applied.
        payload["type"] = "event"
        await self._send(connection, payload)

    async def call(
        self,
        client_id: str,
        operation: str,
        payload: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Run one game operation on a connected Windows worker."""

        operation = normalize_operation(operation)
        payload = normalize_payload(payload)
        # Validate the wait budget before looking up/dispatching to a worker.
        # An invalid timeout must never emit a call frame that the Windows
        # process could execute after the caller has already failed locally.
        if isinstance(timeout, bool):
            raise GameWebSocketError("invalid_timeout", "游戏操作超时时间无效")
        try:
            wait_seconds = self.call_timeout if timeout is None else float(timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise GameWebSocketError("invalid_timeout", "游戏操作超时时间无效") from exc
        if not math.isfinite(wait_seconds) or wait_seconds < 0.1 or wait_seconds > 120:
            raise GameWebSocketError("invalid_timeout", "游戏操作超时时间无效")
        connection = await self.get(client_id)
        if connection is None:
            raise GameWebSocketUnavailable(client_id)
        if connection.capabilities and operation not in connection.capabilities:
            # An empty capability list means a legacy worker supports the
            # reviewed default set.  Once a worker advertises capabilities,
            # honor that declaration so a database-only worker cannot receive
            # an unrelated QQ operation by mistake.
            aliases = {
                operation.removeprefix("game."),
                f"game.{operation}",
            }
            if not aliases.intersection(connection.capabilities):
                raise GameWebSocketError("unsupported_operation", "执行端未声明该游戏操作")
        if len(connection.pending) >= self.max_pending_requests:
            raise GameWebSocketError("busy", "游戏执行端请求过多")
        loop = asyncio.get_running_loop()
        request_id = uuid4().hex
        future: asyncio.Future[Any] = loop.create_future()
        connection.pending[request_id] = future
        try:
            await self._send(
                connection,
                {
                    "type": "call",
                    "protocol": GAME_WS_PROTOCOL,
                    "request_id": request_id,
                    "operation": operation,
                    "payload": payload,
                },
            )
            try:
                return await asyncio.wait_for(future, wait_seconds)
            except asyncio.TimeoutError as exc:
                raise GameWebSocketTimeout(request_id) from exc
        finally:
            connection.pending.pop(request_id, None)

    async def get(self, client_id: str) -> _Connection | None:
        try:
            client_id = normalize_client_id(client_id)
        except GameWebSocketError:
            return None
        async with self._lock:
            connection = self._connections.get(client_id)
            if connection is None or connection.closed:
                return None
            return connection

    async def status(self) -> list[dict[str, Any]]:
        """Return non-sensitive connection state for diagnostics."""

        async with self._lock:
            return [
                {
                    "client_id": connection.client_id,
                    "connected_at": connection.connected_at,
                    "pending": len(connection.pending),
                    "capabilities": sorted(connection.capabilities),
                }
                for connection in self._connections.values()
                if not connection.closed
            ]

    async def close(self, code: int = 1001) -> None:
        """Close all workers during an orderly portal shutdown."""

        async with self._lock:
            connections = tuple(self._connections.values())
            self._connections.clear()
        for connection in connections:
            connection.closed = True
            error = GameWebSocketUnavailable(connection.client_id)
            for future in tuple(connection.pending.values()):
                if not future.done():
                    future.set_exception(error)
            connection.pending.clear()
            try:
                await connection.websocket.close(code=code)
            except Exception:
                pass

    async def _resolve_result(self, connection: _Connection, message: Mapping[str, Any]) -> None:
        request_id = message.get("request_id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 80:
            await self._send(
                connection,
                {"type": "error", "code": "invalid_request_id", "detail": "请求编号无效"},
            )
            return
        future = connection.pending.get(request_id)
        if future is None or future.done():
            # Late results are harmless (for example, a client response after
            # a timeout), but acknowledge them so a worker can drop its cache.
            await self._send(connection, {"type": "result_ack", "request_id": request_id, "accepted": False})
            return
        ok = message.get("ok", True)
        if not isinstance(ok, bool):
            future.set_exception(
                GameWebSocketError("invalid_result", "游戏执行端响应状态无效")
            )
            return
        if ok is False:
            raw_error = message.get("error")
            if isinstance(raw_error, Mapping):
                code = str(raw_error.get("code", "remote_error"))[:64]
                detail = str(raw_error.get("detail", "游戏执行端返回失败"))[:255]
            else:
                code, detail = "remote_error", "游戏执行端返回失败"
            future.set_exception(GameWebSocketError(code, detail))
        else:
            try:
                result = normalize_payload(message.get("result"))
            except GameWebSocketError as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    async def serve(
        self,
        websocket: WebSocket,
        connection: _Connection,
    ) -> None:
        """Consume frames until a Windows worker disconnects."""

        try:
            while True:
                try:
                    message = await websocket.receive_json()
                except WebSocketDisconnect:
                    break
                except (TypeError, ValueError, json.JSONDecodeError):
                    await self._send(
                        connection,
                        {"type": "error", "code": "invalid_json", "detail": "消息格式无效"},
                    )
                    continue
                if not isinstance(message, Mapping):
                    await self._send(
                        connection,
                        {"type": "error", "code": "invalid_message", "detail": "消息必须是 JSON 对象"},
                    )
                    continue
                # ``receive_json`` has already decoded the frame, but an
                # authenticated worker can still send an unexpectedly large
                # result/event object.  Enforce the same bounded frame budget
                # used by outbound messages before inspecting or correlating
                # any fields.
                try:
                    message_size = _json_size(message)
                except GameWebSocketError as exc:
                    await self._send(connection, {"type": "error", **_error_message(exc)})
                    continue
                if message_size > MAX_MESSAGE_BYTES:
                    await self._send(
                        connection,
                        {"type": "error", "code": "message_too_large", "detail": "游戏消息过大"},
                    )
                    continue
                message_type = str(message.get("type", "")).strip().lower()
                if message_type == "ping":
                    await self._send(connection, {"type": "pong", "at": _now_iso()})
                elif message_type in {"result", "response"}:
                    await self._resolve_result(connection, message)
                elif message_type == "event":
                    # Events are intentionally not executed by the portal. A
                    # caller may attach an event callback in a future adapter;
                    # acknowledge receipt so the Windows journal can retry
                    # only when the transport itself failed.
                    event_id = str(message.get("event_id", "")).strip()[:80]
                    await self._send(
                        connection,
                        {"type": "event_ack", "event_id": event_id, "accepted": True},
                    )
                elif message_type == "close":
                    try:
                        await websocket.close(code=1000)
                    except Exception:
                        pass
                    break
                else:
                    await self._send(
                        connection,
                        {"type": "error", "code": "unsupported_message", "detail": "不支持的游戏消息类型"},
                    )
        finally:
            await self.unregister(connection)


def extract_websocket_token(websocket: WebSocket) -> str:
    """Read a bearer token from headers or an explicit compatibility query.

    Query tokens are supported for clients that cannot set WebSocket headers,
    but the server never echoes them and deployments should prefer the header.
    """

    authorization = str(websocket.headers.get("authorization", "")).strip()
    header_token = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        # Match the HTTPS sync endpoint: a present Authorization header with
        # any non-Bearer scheme is invalid, even when a query token is also
        # supplied.  Otherwise ``Basic ...?token=...`` could silently bypass
        # proxy/header validation and make diagnostics misleading.
        if scheme.lower() != "bearer" or not value.strip():
            raise GameWebSocketError("invalid_token", "游戏 WebSocket 入口令牌无效")
        header_token = value.strip()
    query_token = str(
        websocket.query_params.get("token")
        or websocket.query_params.get("access_token")
        or ""
    ).strip()
    if header_token and query_token and not hmac.compare_digest(header_token, query_token):
        raise GameWebSocketError("invalid_token", "游戏 WebSocket 入口令牌无效")
    token = header_token or query_token
    if not token:
        raise GameWebSocketError("missing_token", "缺少游戏 WebSocket 入口令牌")
    return token


def websocket_client_hint(websocket: WebSocket) -> str:
    """Return the optional client ID supplied during the HTTP handshake."""

    value = str(
        websocket.headers.get("x-joker-client")
        or websocket.query_params.get("client_id")
        or ""
    ).strip()
    return value


async def serve_game_websocket(
    websocket: WebSocket,
    hub: GameWebSocketHub,
    *,
    expected_token: str,
) -> None:
    """Authenticate and serve one WebSocket connection.

    The first frame is a bounded ``hello`` object.  Requiring the identity in
    the frame as well as optionally in ``X-Joker-Client`` prevents a proxy from
    silently attaching a connection to another Windows installation.
    """

    if not expected_token:
        await websocket.close(code=GAME_WS_CLOSE_FORBIDDEN)
        return
    try:
        supplied = extract_websocket_token(websocket)
        if not hmac.compare_digest(supplied, expected_token):
            raise GameWebSocketError("invalid_token", "同步令牌无效")
    except GameWebSocketError:
        await websocket.close(code=GAME_WS_CLOSE_UNAUTHORIZED)
        return

    await websocket.accept()
    connection: _Connection | None = None
    try:
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), 10.0)
        except asyncio.TimeoutError:
            await websocket.close(code=GAME_WS_CLOSE_TIMEOUT)
            return
        except (WebSocketDisconnect, TypeError, ValueError, json.JSONDecodeError):
            await websocket.close(code=GAME_WS_CLOSE_BAD_REQUEST)
            return
        try:
            hello_size = _json_size(hello)
        except GameWebSocketError:
            await websocket.close(code=GAME_WS_CLOSE_BAD_REQUEST)
            return
        if hello_size > MAX_MESSAGE_BYTES:
            await websocket.close(code=GAME_WS_CLOSE_BAD_REQUEST)
            return
        if not isinstance(hello, Mapping) or str(hello.get("type", "")).lower() != "hello":
            await websocket.close(code=GAME_WS_CLOSE_BAD_REQUEST)
            return
        protocol = str(hello.get("protocol", GAME_WS_PROTOCOL) or GAME_WS_PROTOCOL)
        if protocol != GAME_WS_PROTOCOL:
            await websocket.close(code=GAME_WS_CLOSE_BAD_REQUEST)
            return
        client_id = normalize_client_id(hello.get("client_id") or websocket_client_hint(websocket))
        hinted = websocket_client_hint(websocket)
        if hinted:
            hinted = normalize_client_id(hinted)
            if not hmac.compare_digest(hinted, client_id):
                await websocket.close(code=GAME_WS_CLOSE_BAD_REQUEST)
                return
        raw_capabilities = hello.get("capabilities", [])
        if raw_capabilities is None:
            raw_capabilities = []
        if not isinstance(raw_capabilities, (list, tuple, set)) or len(raw_capabilities) > MAX_CAPABILITIES:
            await websocket.close(code=GAME_WS_CLOSE_BAD_REQUEST)
            return
        capabilities = [str(item).strip() for item in raw_capabilities if isinstance(item, str)]
        connection = await hub.register(websocket, client_id, capabilities)
        await hub._send(
            connection,
            {
                "type": "hello_ack",
                "protocol": GAME_WS_PROTOCOL,
                "client_id": client_id,
                "server_time": _now_iso(),
            },
        )
        await hub.serve(websocket, connection)
    except GameWebSocketError as exc:
        if connection is not None:
            try:
                await hub._send(connection, {"type": "error", **_error_message(exc)})
            except Exception:
                pass
            await hub.unregister(connection)
        try:
            await websocket.close(code=GAME_WS_CLOSE_BAD_REQUEST)
        except Exception:
            pass
    except WebSocketDisconnect:
        if connection is not None:
            await hub.unregister(connection)
    except Exception:
        logger.exception("Game WebSocket connection failed")
        if connection is not None:
            await hub.unregister(connection)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


__all__ = [
    "ALLOWED_GAME_OPERATIONS",
    "DEFAULT_CALL_TIMEOUT",
    "GAME_WS_PROTOCOL",
    "GameWebSocketError",
    "GameWebSocketHub",
    "GameWebSocketTimeout",
    "GameWebSocketUnavailable",
    "extract_websocket_token",
    "normalize_client_id",
    "normalize_operation",
    "normalize_payload",
    "serve_game_websocket",
    "websocket_client_hint",
]
