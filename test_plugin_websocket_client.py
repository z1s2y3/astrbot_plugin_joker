"""Tests for the documented game-plugin WebSocket protocol."""

from __future__ import annotations

import json
import importlib.util
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp



_MODULE_NAME = "_plugin_websocket_client_test_module"
_SPEC = importlib.util.spec_from_file_location(
    _MODULE_NAME, Path(__file__).parent / "qqadmin" / "plugin_websocket_client.py"
)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_MODULE_NAME] = _MODULE
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)
PluginWebSocketClient = _MODULE.PluginWebSocketClient
plugin_websocket_url = _MODULE.plugin_websocket_url
parse_plugin_websocket_url = _MODULE.parse_plugin_websocket_url


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def send_json(self, value):
        self.sent.append(value)

    async def receive(self):
        request_id = self.sent[0]["id"]
        return SimpleNamespace(
            type=aiohttp.WSMsgType.TEXT,
            data=json.dumps(
                {
                    "id": request_id,
                    "status": 200,
                    "body": json.dumps({"ok": True, "message": "pong"}),
                }
            ),
        )


class _FakeSession:
    def __init__(self, websocket: _FakeWebSocket) -> None:
        self.websocket = websocket
        self.url = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def ws_connect(self, url, **_kwargs):
        self.url = url
        return self.websocket


class PluginWebSocketClientTests(unittest.IsolatedAsyncioTestCase):
    def test_url_encodes_api_key_and_uses_documented_path(self):
        url = plugin_websocket_url("127.0.0.1", 28016, "key a?b")
        self.assertEqual("ws://127.0.0.1:28016/ws?apiKey=key%20a%3Fb", url)

    def test_parser_accepts_only_documented_websocket_link(self):
        parsed = parse_plugin_websocket_url(
            "wss://game.example:28016/ws?apiKey=key%20a"
        )
        self.assertEqual("game.example", parsed["host"])
        self.assertEqual(28016, parsed["port"])
        self.assertEqual("key a", parsed["api_key"])
        self.assertEqual("wss", parsed["websocket_scheme"])
        with self.assertRaises(ValueError):
            parse_plugin_websocket_url("ws://game.example:28016/other?apiKey=key")
        with self.assertRaises(ValueError):
            parse_plugin_websocket_url("ws://game.example:28016/ws?apiKey=key&other=1")

    async def test_ping_uses_correlated_json_frame_and_json_string_body(self):
        websocket = _FakeWebSocket()
        session = _FakeSession(websocket)
        client = PluginWebSocketClient("127.0.0.1", 28016, "secret?a")
        with patch.object(_MODULE.aiohttp, "ClientSession", return_value=session):
            result = await client.test_connection()

        self.assertTrue(result["success"])
        self.assertEqual("plugin_websocket_connected", result["code"])
        self.assertEqual("GET", websocket.sent[0]["method"])
        self.assertEqual("/ping", websocket.sent[0]["path"])
        self.assertIsInstance(websocket.sent[0]["body"], str)
        self.assertEqual({}, json.loads(websocket.sent[0]["body"]))
        self.assertIn("apiKey=secret%3Fa", session.url)

    async def test_run_command_follows_documented_player_payload(self):
        websocket = _FakeWebSocket()
        session = _FakeSession(websocket)
        client = PluginWebSocketClient("127.0.0.1", 28016, "secret")
        with patch.object(_MODULE.aiohttp, "ClientSession", return_value=session):
            result = await client.execute_command(
                "#spawnzombie BP_Zombie 1 location",
                steam_id="76561199616499552",
            )

        self.assertTrue(result["success"])
        payload = json.loads(websocket.sent[0]["body"])
        self.assertEqual("POST", websocket.sent[0]["method"])
        self.assertEqual("/run-command", websocket.sent[0]["path"])
        self.assertEqual("player", payload["target"])
        self.assertEqual("76561199616499552", payload["steamId"])
        self.assertEqual(["#spawnzombie BP_Zombie 1 location"], payload["commands"])
        self.assertTrue(payload["requestKey"].startswith("req-"))

    async def test_http_success_without_operation_ack_is_not_success(self):
        class _UnconfirmedWebSocket(_FakeWebSocket):
            async def receive(self):
                request_id = self.sent[0]["id"]
                return SimpleNamespace(
                    type=aiohttp.WSMsgType.TEXT,
                    data=json.dumps(
                        {
                            "id": request_id,
                            "body": json.dumps({"message": "accepted"}),
                        }
                    ),
                )

        websocket = _UnconfirmedWebSocket()
        session = _FakeSession(websocket)
        client = PluginWebSocketClient("127.0.0.1", 28016, "secret")
        with patch.object(_MODULE.aiohttp, "ClientSession", return_value=session):
            result = await client.test_connection()

        self.assertFalse(result["success"])
        self.assertEqual("plugin_websocket_request_failed", result["code"])


if __name__ == "__main__":
    unittest.main()
