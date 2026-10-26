"""Tests for the documented im3 Socket.IO configuration probe."""

import asyncio
import unittest
from unittest.mock import patch

import aiohttp

from web_client import im3_socketio


class _Message:
    def __init__(self, data):
        self.type = aiohttp.WSMsgType.TEXT
        self.data = data


class _Socket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def receive(self):
        if not self.frames:
            raise asyncio.TimeoutError
        return _Message(self.frames.pop(0))

    async def send_str(self, value):
        self.sent.append(value)


class _Session:
    def __init__(self, frames):
        self.socket = _Socket(frames)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def ws_connect(self, *_args, **_kwargs):
        return self.socket


def _session_factory(frames):
    return lambda **_kwargs: _Session(frames)


class Im3SocketIOTests(unittest.TestCase):
    def test_normalize_requires_safe_documented_identity(self):
        self.assertEqual(
            "bot_main",
            im3_socketio._normalize_bot_type("bot_main"),
        )
        with self.assertRaises(ValueError):
            im3_socketio._normalize_bot_type("phone_chat")

    def test_websocket_probe_performs_join_room_without_logging_key(self):
        config = {
            "server_url": "https://im3.vipscum.cn",
            "socketio_path": "/socket.io",
            "bot_type": "bot_main",
            "key": "0123456789abcdef0123456789abcdef",
        }
        with patch.object(
            im3_socketio.aiohttp,
            "ClientSession",
            _session_factory(
                [
                    '0{"sid":"test-session","upgrades":[]}',
                    '40{"sid":"namespace-session"}',
                    '42["joined",{"ok":true,"room":"room_test","type":"bot_main","authorized":true}]',
                ]
            ),
        ):
            result = im3_socketio.test_im3_socketio_connection(config)
        self.assertTrue(result["success"])
        self.assertEqual("im3_connected", result["code"])
        self.assertEqual("websocket", result["transport"])
        self.assertNotIn("0123456789abcdef0123456789abcdef", str(result))

    def test_websocket_probe_rejects_explicit_join_room_error(self):
        config = {
            "server_url": "https://im3.vipscum.cn",
            "socketio_path": "/socket.io",
            "bot_type": "bot_main",
            "key": "0123456789abcdef0123456789abcdef",
        }
        with patch.object(
            im3_socketio.aiohttp,
            "ClientSession",
            _session_factory(
                [
                    '0{"sid":"test-session","upgrades":[]}',
                    '40{"sid":"namespace-session"}',
                    '42["error",{"ok":false,"message":"invalid key"}]',
                ]
            ),
        ):
            result = im3_socketio.test_im3_socketio_connection(config)
        self.assertFalse(result["success"])
        self.assertEqual("im3_join_rejected", result["code"])
        self.assertNotIn("0123456789abcdef0123456789abcdef", str(result))

    def test_websocket_probe_rejects_non_website_card_before_connecting(self):
        result = im3_socketio.test_im3_socketio_connection(
            {
                "server_url": "https://im3.vipscum.cn",
                "socketio_path": "/socket.io",
                "bot_type": "bot_main",
                "key": "arbitrary-text",
            }
        )
        self.assertFalse(result["success"])
        self.assertEqual("im3_key_format_invalid", result["code"])

    def test_websocket_probe_does_not_accept_transport_without_join_confirmation(self):
        config = {
            "server_url": "https://im3.vipscum.cn",
            "socketio_path": "/socket.io",
            "bot_type": "bot_main",
            "key": "0123456789abcdef0123456789abcdef",
        }
        with patch.object(
            im3_socketio.aiohttp,
            "ClientSession",
            _session_factory(
                [
                    '0{"sid":"test-session","upgrades":[]}',
                    '40{"sid":"namespace-session"}',
                    '42["message",{"ok":true}]',
                ]
            ),
        ):
            result = im3_socketio.test_im3_socketio_connection(config, timeout=1)
        self.assertFalse(result["success"])
        self.assertEqual("im3_join_unconfirmed", result["code"])

    def test_websocket_probe_accepts_confirmed_join_when_server_omits_card_authorization(self):
        config = {
            "server_url": "https://im3.vipscum.cn",
            "socketio_path": "/socket.io",
            "bot_type": "bot_main",
            "key": "0123456789abcdef0123456789abcdef",
        }
        with patch.object(
            im3_socketio.aiohttp,
            "ClientSession",
            _session_factory(
                [
                    '0{"sid":"test-session","upgrades":[]}',
                    '40{"sid":"namespace-session"}',
                    '42["joined",{"room":"room_test","type":"bot_main"}]',
                ]
            ),
        ):
            result = im3_socketio.test_im3_socketio_connection(config)
        self.assertTrue(result["success"])
        self.assertEqual("im3_connected", result["code"])
        self.assertFalse(result["card_authorized"])
        self.assertEqual("not_provided", result["authorization_status"])

    def test_user_card_validation_rejects_join_without_explicit_authorization(self):
        config = {
            "server_url": "https://im3.vipscum.cn",
            "socketio_path": "/socket.io",
            "bot_type": "bot_main",
            "key": "0123456789abcdef0123456789abcdef",
            "require_card_authorization": True,
        }
        with patch.object(
            im3_socketio.aiohttp,
            "ClientSession",
            _session_factory(
                [
                    '0{"sid":"test-session","upgrades":[]}',
                    '40{"sid":"namespace-session"}',
                    '42["joined",{"room":"room_test","type":"bot_main"}]',
                ]
            ),
        ):
            result = im3_socketio.test_im3_socketio_connection(config)
        self.assertFalse(result["success"])
        self.assertEqual("im3_card_authorization_unavailable", result["code"])
        self.assertEqual("not_provided", result["authorization_status"])

    def test_user_card_validation_accepts_nested_explicit_authorization(self):
        config = {
            "server_url": "https://im3.vipscum.cn",
            "socketio_path": "/socket.io",
            "bot_type": "bot_main",
            "key": "0123456789abcdef0123456789abcdef",
            "require_card_authorization": True,
        }
        with patch.object(
            im3_socketio.aiohttp,
            "ClientSession",
            _session_factory(
                [
                    '0{"sid":"test-session","upgrades":[]}',
                    '40{"sid":"namespace-session"}',
                    '42["joined",{"room":"room_test","type":"bot_main","authorization":{"card_valid":true}}]',
                ]
            ),
        ):
            result = im3_socketio.test_im3_socketio_connection(config)
        self.assertTrue(result["success"])
        self.assertTrue(result["card_authorized"])
        self.assertEqual("authorized", result["authorization_status"])

    def test_user_card_validation_reports_explicit_rejection(self):
        config = {
            "server_url": "https://im3.vipscum.cn",
            "socketio_path": "/socket.io",
            "bot_type": "bot_main",
            "key": "0123456789abcdef0123456789abcdef",
            "require_card_authorization": True,
        }
        with patch.object(
            im3_socketio.aiohttp,
            "ClientSession",
            _session_factory(
                [
                    '0{"sid":"test-session","upgrades":[]}',
                    '40{"sid":"namespace-session"}',
                    '42["joined",{"room":"room_test","type":"bot_main","card_valid":false}]',
                ]
            ),
        ):
            result = im3_socketio.test_im3_socketio_connection(config)
        self.assertFalse(result["success"])
        self.assertEqual("im3_card_rejected", result["code"])
        self.assertEqual("rejected", result["authorization_status"])

    def test_admin_probe_checks_transport_without_a_room_card(self):
        config = {
            "server_url": "https://im3.vipscum.cn",
            "socketio_path": "/socket.io",
            "bot_type": "bot_main",
            "probe_only": True,
        }
        with patch.object(
            im3_socketio.aiohttp,
            "ClientSession",
            _session_factory(
                [
                    '0{"sid":"test-session","upgrades":[]}',
                    '40{"sid":"namespace-session"}',
                ]
            ),
        ):
            result = im3_socketio.test_im3_socketio_connection(config)
        self.assertTrue(result["success"])
        self.assertEqual("im3_transport_connected", result["code"])
        self.assertFalse(result["join_request_submitted"])

    def test_websocket_connection_error_is_reported_without_response_body(self):
        config = {
            "server_url": "https://im3.vipscum.cn",
            "socketio_path": "/socket.io",
            "bot_type": "bot_main",
            "probe_only": True,
        }
        class _BrokenSession:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def ws_connect(self, *_args, **_kwargs):
                raise aiohttp.ClientConnectionError("connection refused")

        with patch.object(
            im3_socketio.aiohttp,
            "ClientSession",
            _BrokenSession,
        ):
            result = im3_socketio.test_im3_socketio_connection(config)
        self.assertFalse(result["success"])
        self.assertEqual("im3_connection_failed", result["code"])
        self.assertIn("WebSocket", result["message"])


if __name__ == "__main__":
    unittest.main()
