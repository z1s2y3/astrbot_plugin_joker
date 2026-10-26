"""Focused tests for the Windows-side game WebSocket worker."""

from __future__ import annotations

import unittest
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch


_MODULE_PATH = Path(__file__).resolve().parent / "qqadmin" / "game_websocket_client.py"
_SPEC = importlib.util.spec_from_file_location("joker_test_game_websocket_client", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
GameWebSocketClient = _MODULE.GameWebSocketClient
websocket_url = _MODULE.websocket_url
handshake_error_details = _MODULE._handshake_error_details


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


class GameWebSocketClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_404_retries_legacy_websocket_path(self):
        client = GameWebSocketClient(
            "https://portal.example",
            "token",
            "windows-a",
        )
        calls = []

        async def fake_run_once(_session, endpoint_path=None):
            calls.append(endpoint_path)
            if len(calls) == 1:
                raise _MODULE.aiohttp.WSServerHandshakeError(
                    None, (), status=404, message="not found", headers={}
                )
            client._stop_event.set()

        class FakeSessionContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        client._run_once = fake_run_once
        with patch.object(
            _MODULE.aiohttp, "ClientSession", return_value=FakeSessionContext()
        ):
            await client.run_forever()

        self.assertEqual(
            ["/api/sync/v1/ws", "/api/game/v1/ws"],
            calls,
        )
        self.assertEqual("/api/sync/v1/ws", client.endpoint_path)
        self.assertEqual("/api/game/v1/ws", client.last_endpoint_path)

    def test_handshake_errors_expose_actionable_status_without_secret(self):
        class _HandshakeError:
            def __init__(self, status):
                self.status = status

        code, detail = handshake_error_details(_HandshakeError(401))
        self.assertEqual("game_websocket_handshake_http_401", code)
        self.assertIn("入口令牌无效", detail)
        self.assertNotIn("token", detail.lower())

        code, detail = handshake_error_details(_HandshakeError(404), "/api/sync/v1/ws")
        self.assertEqual("game_websocket_handshake_http_404", code)
        self.assertIn("/api/sync/v1/ws", detail)

        code, detail = handshake_error_details(_HandshakeError(400))
        self.assertEqual("game_websocket_handshake_http_400", code)
        self.assertIn("WebSocket 升级", detail)

        code, detail = handshake_error_details(_HandshakeError(502), "/api/sync/v1/ws")
        self.assertEqual("game_websocket_handshake_http_502", code)
        self.assertIn("joker-webctl --check", detail)

    def test_websocket_url_uses_configured_endpoint(self):
        self.assertEqual(
            "wss://portal.example/api/sync/v1/ws",
            websocket_url("https://portal.example/"),
        )
        self.assertEqual(
            "wss://portal.example/api/sync/v1/ws",
            websocket_url("https://portal.example/path"),
        )
        self.assertEqual(
            "wss://portal.example/api/game/v1/ws",
            websocket_url("https://portal.example/", "/api/game/v1/ws"),
        )
        self.assertEqual("", websocket_url("https://portal.example/", "/custom/ws"))
        self.assertEqual("", websocket_url("https://portal.example?token=x"))

    def test_remote_reset_is_narrowly_filtered_and_timeout_is_actionable(self):
        reset = ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")
        self.assertTrue(GameWebSocketClient._is_remote_reset(reset))
        self.assertFalse(GameWebSocketClient._is_remote_reset(ConnectionResetError(5)))

        code, detail = GameWebSocketClient._connection_failure_details(
            TimeoutError(), "/api/sync/v1/ws"
        )
        self.assertEqual("game_websocket_connect_timeout", code)
        self.assertIn("堡塔反向代理", detail)

        code, detail = GameWebSocketClient._connection_failure_details(
            reset, "/api/sync/v1/ws"
        )
        self.assertEqual("game_websocket_remote_reset", code)
        self.assertIn("自动重连", detail)

    def test_loop_handler_does_not_hide_unrelated_reset_callbacks(self):
        client = GameWebSocketClient("https://portal.example", "token", "windows-a")
        forwarded = []

        def previous(_loop, context):
            forwarded.append(context)

        client._previous_loop_exception_handler = previous
        reset = ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")
        client._loop_exception_handler(None, {"message": "other callback", "exception": reset})
        self.assertEqual(1, len(forwarded))
        client._loop_exception_handler(
            None,
            {
                "message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
                "exception": reset,
            },
        )
        self.assertEqual(1, len(forwarded))

    async def test_hello_capabilities_can_report_plugin_websocket(self):
        client = GameWebSocketClient(
            "https://portal.example",
            "token",
            "windows-a",
            endpoint_path="/api/game/v1/ws",
            capabilities=("plugin_websocket",),
        )
        self.assertIn("plugin_websocket", client.capabilities)
        self.assertIn("steam_reward", client.capabilities)
        self.assertEqual("/api/game/v1/ws", client.endpoint_path)

    async def test_execute_passes_bound_identity_to_adapter(self):
        calls = []

        async def executor(command, **kwargs):
            calls.append((command, kwargs))
            return {"success": True, "before": 10, "after": 40}

        client = GameWebSocketClient(
            "https://portal.example",
            "token",
            "windows-a",
            executor=executor,
        )
        result = await client._execute(
            "steam_reward",
            {
                "command": "#ChangeCurrencyBalance Normal 30 76561199616499552",
                "group_id": "100",
                "user_id": "42",
                "steam_id": "76561199616499552",
            },
        )
        self.assertEqual({"success": True, "before": 10, "after": 40}, result)
        self.assertEqual("#ChangeCurrencyBalance Normal 30 76561199616499552", calls[0][0])
        self.assertEqual("76561199616499552", calls[0][1]["steam_id"])

    async def test_execute_rejects_missing_confirmation(self):
        async def executor(_command, **_kwargs):
            return None

        client = GameWebSocketClient(
            "https://portal.example",
            "token",
            "windows-a",
            executor=executor,
        )
        result = await client._execute("steam_reward", {"command": "#reward"})

        self.assertFalse(result["success"])
        self.assertEqual("invalid_executor_response", result["code"])

    async def test_connection_test_uses_windows_tester(self):
        calls = []

        async def connection_tester(**kwargs):
            calls.append(kwargs)
            return {"success": True, "code": "plugin_websocket_connected", "profile_id": "main"}

        client = GameWebSocketClient(
            "https://portal.example",
            "token",
            "windows-a",
            connection_tester=connection_tester,
        )
        result = await client._execute(
            "test_game_connection",
            {"group_id": "100", "profile_id": "main"},
        )
        self.assertEqual("plugin_websocket_connected", result["code"])
        self.assertEqual(
            {"group_id": "100", "profile_id": "main", "operation": "test_game_connection"},
            calls[0],
        )

    async def test_failed_adapter_result_is_sent_as_error_frame(self):
        async def executor(_command, **_kwargs):
            return {"success": False, "code": "executor_unavailable", "message": "未配置游戏执行器"}

        client = GameWebSocketClient(
            "https://portal.example",
            "token",
            "windows-a",
            executor=executor,
        )
        websocket = _FakeWebSocket()
        await client._handle_message(
            websocket,
            '{"type":"call","request_id":"req-1","operation":"steam_reward","payload":{}}',
        )
        self.assertEqual(
            {
                "type": "result",
                "request_id": "req-1",
                "ok": False,
                "error": {
                    "code": "executor_unavailable",
                    "detail": "未配置游戏执行器",
                },
            },
            websocket.sent[0],
        )


if __name__ == "__main__":
    unittest.main()
