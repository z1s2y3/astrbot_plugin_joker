"""Focused tests for the authenticated game WebSocket transport."""

from __future__ import annotations

import asyncio
import json
import unittest
from collections import deque

from starlette.websockets import WebSocketDisconnect

from web_client.websocket_api import (
    GAME_WS_PROTOCOL,
    GameWebSocketError,
    GameWebSocketHub,
    GameWebSocketUnavailable,
    MAX_MESSAGE_BYTES,
    normalize_operation,
    serve_game_websocket,
)


class _FakeWebSocket:
    def __init__(self, frames, *, token="secret", client_id="windows-a"):
        self.headers = {
            "authorization": f"Bearer {token}",
            "x-joker-client": client_id,
        }
        self.query_params = {}
        self._frames = deque(frames)
        self.sent: list[dict] = []
        self.accepted = False
        self.closed: list[int | None] = []
        self._wake = asyncio.Event()

    async def accept(self, **_kwargs):
        self.accepted = True

    async def close(self, code=None, **_kwargs):
        self.closed.append(code)

    async def receive_json(self):
        while not self._frames:
            await self._wake.wait()
            self._wake.clear()
        frame = self._frames.popleft()
        if isinstance(frame, BaseException):
            raise frame
        return frame

    def push(self, frame):
        self._frames.append(frame)
        self._wake.set()

    async def send_text(self, value):
        self.sent.append(json.loads(value))


class WebSocketTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticates_hello_and_replies_to_ping(self):
        websocket = _FakeWebSocket(
            [
                {
                    "type": "hello",
                    "protocol": GAME_WS_PROTOCOL,
                    "client_id": "windows-a",
                    "capabilities": ["game.query_online"],
                },
                {"type": "ping"},
                WebSocketDisconnect(code=1000),
            ]
        )
        hub = GameWebSocketHub()
        await serve_game_websocket(websocket, hub, expected_token="secret")
        self.assertTrue(websocket.accepted)
        self.assertEqual("hello_ack", websocket.sent[0]["type"])
        self.assertEqual("pong", websocket.sent[1]["type"])
        self.assertEqual([], await hub.status())

    async def test_call_correlates_result_and_rejects_unknown_operation(self):
        websocket = _FakeWebSocket(
            [
                {
                    "type": "hello",
                    "client_id": "windows-a",
                }
            ]
        )
        hub = GameWebSocketHub(call_timeout=1)
        serving = asyncio.create_task(serve_game_websocket(websocket, hub, expected_token="secret"))
        while not websocket.sent:
            await asyncio.sleep(0)
        result_task = asyncio.create_task(
            hub.call("windows-a", "game.query_online", {"server_id": "42"})
        )
        while len(websocket.sent) < 2:
            await asyncio.sleep(0)
        call_frame = websocket.sent[1]
        self.assertEqual("call", call_frame["type"])
        websocket.push(
            {
                "type": "result",
                "request_id": call_frame["request_id"],
                "ok": True,
                "result": {"online": 3},
            }
        )
        self.assertEqual({"online": 3}, await result_task)
        with self.assertRaises(GameWebSocketError):
            await hub.call("windows-a", "os.system", {})
        websocket.push(WebSocketDisconnect(code=1000))
        await serving

    async def test_offline_client_is_explicit(self):
        hub = GameWebSocketHub()
        with self.assertRaises(GameWebSocketUnavailable) as context:
            await hub.call("windows-a", "signin", {})
        self.assertEqual("client_offline", context.exception.code)

    async def test_capabilities_limit_calls_and_late_results_are_acknowledged(self):
        websocket = _FakeWebSocket(
            [{"type": "hello", "client_id": "windows-a", "capabilities": ["signin"]}]
        )
        hub = GameWebSocketHub(call_timeout=0.1)
        serving = asyncio.create_task(serve_game_websocket(websocket, hub, expected_token="secret"))
        while not websocket.sent:
            await asyncio.sleep(0)
        with self.assertRaises(GameWebSocketError) as unsupported:
            await hub.call("windows-a", "lottery", {})
        self.assertEqual("unsupported_operation", unsupported.exception.code)
        with self.assertRaises(GameWebSocketError) as invalid_timeout:
            await hub.call("windows-a", "signin", {}, timeout=0)
        self.assertEqual("invalid_timeout", invalid_timeout.exception.code)
        # Only the hello acknowledgement has been sent; the invalid request
        # must not reach the Windows worker as a call frame.
        self.assertEqual(1, len(websocket.sent))
        timeout_task = asyncio.create_task(hub.call("windows-a", "signin", {}))
        while len(websocket.sent) < 2:
            await asyncio.sleep(0)
        request_id = websocket.sent[1]["request_id"]
        with self.assertRaises(GameWebSocketError) as timeout:
            await timeout_task
        self.assertEqual("request_timeout", timeout.exception.code)
        websocket.push({"type": "result", "request_id": request_id, "ok": True, "result": {}})
        while len(websocket.sent) < 3:
            await asyncio.sleep(0)
        self.assertEqual("result_ack", websocket.sent[-1]["type"])
        websocket.push(WebSocketDisconnect(code=1000))
        await serving

    async def test_oversized_inbound_frame_is_rejected_without_dispatch(self):
        websocket = _FakeWebSocket(
            [
                {"type": "hello", "client_id": "windows-a"},
                {"type": "event", "body": "x" * MAX_MESSAGE_BYTES},
                WebSocketDisconnect(code=1000),
            ]
        )
        await serve_game_websocket(websocket, GameWebSocketHub(), expected_token="secret")
        self.assertEqual("hello_ack", websocket.sent[0]["type"])
        self.assertEqual("message_too_large", websocket.sent[1]["code"])

    async def test_bad_token_closes_before_accept(self):
        websocket = _FakeWebSocket([], token="wrong")
        await serve_game_websocket(websocket, GameWebSocketHub(), expected_token="secret")
        self.assertFalse(websocket.accepted)
        self.assertEqual([4401], websocket.closed)

    async def test_non_bearer_header_cannot_fall_back_to_query_token(self):
        websocket = _FakeWebSocket([], token="secret")
        websocket.headers["authorization"] = "Basic not-a-sync-token"
        websocket.query_params["token"] = "secret"
        await serve_game_websocket(websocket, GameWebSocketHub(), expected_token="secret")
        self.assertFalse(websocket.accepted)
        self.assertEqual([4401], websocket.closed)

    async def test_oversized_hello_is_rejected_before_registration(self):
        websocket = _FakeWebSocket(
            [{"type": "hello", "client_id": "windows-a", "padding": "x" * (512 * 1024)}]
        )
        hub = GameWebSocketHub()
        await serve_game_websocket(websocket, hub, expected_token="secret")
        self.assertTrue(websocket.accepted)
        self.assertEqual([4400], websocket.closed)
        self.assertEqual([], await hub.status())

    def test_operation_names_are_bounded(self):
        self.assertEqual("query_online", normalize_operation("QUERY_ONLINE"))
        self.assertEqual("reset_signin_record", normalize_operation("reset_signin_record"))
        self.assertEqual("game.bind_steam_signin_id", normalize_operation("game.bind_steam_signin_id"))
        self.assertEqual("test_game_connection", normalize_operation("test_game_connection"))
        self.assertEqual("game.test_game_connection", normalize_operation("game.test_game_connection"))
        with self.assertRaises(GameWebSocketError):
            normalize_operation("database.drop_all")
        with self.assertRaises(GameWebSocketError):
            normalize_operation("game.")
        # Namespaces are not a wildcard.  Every privileged Windows action is
        # reviewed and added to the explicit allowlist before it can route.
        with self.assertRaises(GameWebSocketError):
            normalize_operation("game.unreviewed_operation")


if __name__ == "__main__":
    unittest.main()
