"""Tests for portal-persisted game WebSocket settings."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from web_client.websocket_settings import (
    GAME_WEBSOCKET_SETTING_KEY,
    game_websocket_user_view,
    game_websocket_runtime_enabled,
    get_game_websocket_settings,
    save_game_websocket_settings,
)
from web_client.settings import Settings, normalize_public_origin


TEST_SETTINGS = Settings(
    database_url="sqlite://",
    session_secret="session-secret-012345678901234567890123456789",
    card_pepper="card-pepper-012345678901234567890123456789",
    secure_cookies=False,
    portal_name="test",
    session_max_age=3600,
)


class _FakeSetting:
    def __init__(self, payload):
        self.value_json = payload
        self.updated_by = None


class _FakeDb:
    def __init__(self):
        self.settings = {}
        self.added = []

    def get(self, _model, key):
        return self.settings.get(key)

    def add(self, item):
        self.added.append(item)
        self.settings[item.key] = item


class WebSocketSettingsTests(unittest.TestCase):
    def test_public_origin_is_safe_and_falls_back_when_proxy_headers_are_missing(self):
        from web_client.app import _public_request_origin

        self.assertEqual(
            "https://panel.example.com",
            normalize_public_origin("https://panel.example.com/"),
        )
        with self.assertRaisesRegex(RuntimeError, "JOKER_WEB_PUBLIC_ORIGIN"):
            normalize_public_origin("https://user:pass@panel.example.com")

        configured = Settings(
            database_url="sqlite://",
            session_secret="session-secret-012345678901234567890123456789",
            card_pepper="card-pepper-012345678901234567890123456789",
            secure_cookies=False,
            portal_name="test",
            session_max_age=3600,
            public_origin="https://panel.example.com",
        )
        request = SimpleNamespace(
            base_url="http://127.0.0.1:8765/",
            headers={},
            app=SimpleNamespace(state=SimpleNamespace(settings=configured)),
        )
        self.assertEqual("https://panel.example.com", _public_request_origin(request))
        request.headers = {
            "x-forwarded-proto": "https",
            "x-forwarded-host": "cdn.example.com",
        }
        self.assertEqual("https://cdn.example.com", _public_request_origin(request))

    def test_admin_page_reads_runtime_settings_from_app_state(self):
        source_path = Path(__file__).with_name("web_client") / "app.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        route = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "admin_websocket_settings"
        )
        body = ast.get_source_segment(source_path.read_text(encoding="utf-8"), route) or ""
        self.assertIn("app.state", body)
        self.assertIn("current_websocket_settings", body)
        self.assertIn("websocket_settings=current_websocket_settings", body)
        self.assertNotIn("websocket_settings=websocket_settings", body)

    def test_defaults_are_bounded(self):
        settings = get_game_websocket_settings(_FakeDb())
        self.assertTrue(settings["enabled"])
        self.assertEqual(64, settings["max_clients"])
        self.assertEqual(20.0, settings["call_timeout"])
        self.assertEqual("/api/sync/v1/ws", settings["endpoint_path"])
        self.assertFalse(settings["token_configured"])
        self.assertFalse(settings["game_websocket_enabled"])
        self.assertNotIn("game_executor_password_configured", settings)
        self.assertFalse(settings["game_executor_api_key_configured"])
        self.assertEqual("plugin_websocket", settings["game_executor"]["type"])

    def test_runtime_gate_requires_both_backend_switches(self):
        self.assertFalse(
            game_websocket_runtime_enabled(
                {"enabled": True, "game_websocket_enabled": False}
            )
        )
        self.assertFalse(
            game_websocket_runtime_enabled(
                {"enabled": False, "game_websocket_enabled": True}
            )
        )
        self.assertTrue(
            game_websocket_runtime_enabled(
                {"enabled": True, "game_websocket_enabled": True}
            )
        )

    def test_customer_transport_view_is_safe_and_requires_real_executor(self):
        view = game_websocket_user_view(
            {
                "enabled": True,
                "game_websocket_enabled": True,
                "token_configured": True,
                "endpoint_path": "/api/sync/v1/ws",
                "game_executor": {
                    "enabled": True,
                    "host": "10.0.0.20",
                    "password": "secret",
                },
            },
            group_authorized=True,
            executor_available=True,
        )
        self.assertTrue(view["callable"])
        self.assertEqual("调用后台 WebSocket", view["modes"][1]["label"])
        self.assertNotIn("10.0.0.20", str(view))
        self.assertNotIn("secret", str(view))

    def test_customer_transport_view_explains_missing_backend_prerequisites(self):
        view = game_websocket_user_view(
            {
                "enabled": True,
                "game_websocket_enabled": True,
                "token_configured": True,
            },
            group_authorized=True,
            executor_available=False,
        )
        self.assertFalse(view["callable"])
        self.assertFalse(view["modes"][1]["available"])
        self.assertIn("全局执行器", view["reason"])

    def test_save_round_trips_and_rejects_unsafe_values(self):
        db = _FakeDb()
        saved = save_game_websocket_settings(
            db,
            enabled=False,
            max_clients="12",
            call_timeout="4.5",
            portal_url="https://panel.example.com",
            endpoint_path="/api/game/v1/ws",
            operator_id=7,
        )
        self.assertEqual(False, saved["enabled"])
        self.assertEqual(12, get_game_websocket_settings(db)["max_clients"])
        self.assertEqual("https://panel.example.com", saved["portal_url"])
        self.assertEqual("/api/game/v1/ws", saved["endpoint_path"])
        self.assertEqual(GAME_WEBSOCKET_SETTING_KEY, db.added[0].key)
        with self.assertRaises(ValueError):
            save_game_websocket_settings(
                db,
                enabled=True,
                max_clients=0,
                call_timeout=20,
                operator_id=7,
            )

    def test_token_is_encrypted_and_blank_input_keeps_it(self):
        db = _FakeDb()
        saved = save_game_websocket_settings(
            db,
            enabled=True,
            max_clients=4,
            call_timeout=3,
            sync_token="t" * 40,
            settings=TEST_SETTINGS,
            operator_id=7,
        )
        self.assertTrue(saved["token_configured"])
        raw = db.settings[GAME_WEBSOCKET_SETTING_KEY].value_json
        self.assertNotIn("t" * 40, str(raw))
        kept = save_game_websocket_settings(
            db,
            enabled=True,
            max_clients=4,
            call_timeout=3,
            sync_token="",
            settings=TEST_SETTINGS,
            operator_id=7,
        )
        self.assertEqual(saved["sync_token"], kept["sync_token"])

    def test_game_entry_token_is_separate_from_portal_sync_token(self):
        portal_settings = Settings(
            database_url="sqlite://",
            session_secret="session-secret-012345678901234567890123456789",
            card_pepper="card-pepper-012345678901234567890123456789",
            secure_cookies=False,
            portal_name="test",
            session_max_age=3600,
            sync_token="p" * 40,
        )
        db = _FakeDb()
        defaults = get_game_websocket_settings(db, portal_settings)
        self.assertTrue(defaults["portal_sync_token_configured"])
        self.assertFalse(defaults["game_websocket_token_configured"])
        self.assertEqual("", defaults["game_websocket_token"])

        saved = save_game_websocket_settings(
            db,
            enabled=True,
            game_websocket_enabled=True,
            max_clients=4,
            call_timeout=3,
            game_websocket_token="g" * 40,
            settings=portal_settings,
        )
        self.assertEqual("g" * 40, saved["game_websocket_token"])
        self.assertNotEqual(portal_settings.sync_token, saved["game_websocket_token"])
        raw = db.settings[GAME_WEBSOCKET_SETTING_KEY].value_json
        self.assertIn("game_websocket_token_ciphertext", raw)
        self.assertNotIn(portal_settings.sync_token, str(raw))

    def test_admin_save_can_generate_game_entry_token_once(self):
        db = _FakeDb()
        saved = save_game_websocket_settings(
            db,
            enabled=True,
            game_websocket_enabled=True,
            max_clients=4,
            call_timeout=3,
            portal_url="https://panel.example.com",
            auto_generate_game_websocket_token=True,
            settings=TEST_SETTINGS,
        )
        token = saved["game_websocket_token"]
        self.assertTrue(saved["game_websocket_token_configured"])
        self.assertEqual(72, len(token))
        kept = save_game_websocket_settings(
            db,
            enabled=True,
            game_websocket_enabled=True,
            max_clients=4,
            call_timeout=3,
            portal_url="https://panel.example.com",
            auto_generate_game_websocket_token=True,
            settings=TEST_SETTINGS,
        )
        self.assertEqual(token, kept["game_websocket_token"])

    def test_linux_plugin_websocket_is_encrypted_and_round_trips(self):
        db = _FakeDb()
        saved = save_game_websocket_settings(
            db,
            enabled=True,
            game_websocket_enabled=True,
            max_clients=4,
            call_timeout=3,
            game_executor_enabled=True,
            game_executor_websocket_url="wss://10.0.0.20:28016/ws?apiKey=plugin-secret",
            game_executor_timeout_seconds=8,
            settings=TEST_SETTINGS,
            operator_id=7,
        )
        self.assertTrue(saved["game_websocket_enabled"])
        self.assertTrue(saved["game_executor"]["enabled"])
        self.assertEqual("10.0.0.20", saved["game_executor"]["host"])
        self.assertEqual(28016, saved["game_executor"]["port"])
        self.assertEqual("plugin-secret", saved["game_executor"]["api_key"])
        self.assertEqual("wss", saved["game_executor"]["websocket_scheme"])
        self.assertEqual(
            "wss://10.0.0.20:28016/ws?apiKey=已保存",
            saved["game_executor_display_url"],
        )
        raw = db.settings[GAME_WEBSOCKET_SETTING_KEY].value_json
        self.assertNotIn("plugin-secret", str(raw))
        self.assertIn("api_key_ciphertext", raw["game_executor"])

        kept = save_game_websocket_settings(
            db,
            enabled=True,
            max_clients=4,
            call_timeout=3,
            game_executor_enabled=True,
            game_executor_timeout_seconds=8,
            game_executor_websocket_url="",
            settings=TEST_SETTINGS,
            operator_id=7,
        )
        self.assertEqual("plugin-secret", kept["game_executor"]["api_key"])

    def test_plugin_websocket_rejects_non_documented_url(self):
        with self.assertRaisesRegex(ValueError, "ws\(s\)"):
            save_game_websocket_settings(
                _FakeDb(),
                enabled=True,
                max_clients=4,
                call_timeout=3,
                game_executor_enabled=True,
                game_executor_websocket_url="https://game.example/ws?apiKey=secret",
                settings=TEST_SETTINGS,
            )


if __name__ == "__main__":
    unittest.main()
