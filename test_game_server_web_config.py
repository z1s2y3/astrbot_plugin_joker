"""Tests for Linux-managed SCUM server profiles."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from web_client.feature_schema import FEATURE_BY_KEY, parse_field_value
from web_client.game_server_secrets import (
    materialize_game_server_secrets,
    redact_game_server_profiles,
    store_game_server_profiles,
)
from web_client.settings import Settings


TEST_SETTINGS = Settings(
    database_url="sqlite://",
    session_secret="session-secret-012345678901234567890123456789",
    card_pepper="card-pepper-012345678901234567890123456789",
    secure_cookies=False,
    portal_name="test",
    session_max_age=3600,
)


class GameServerWebConfigTests(unittest.TestCase):
    def test_profile_actions_add_switch_and_delete_without_json(self):
        from web_client import services

        class FakeGroupConfig:
            def __init__(self):
                self.config = {"features": {"game_servers": {"enabled": False, "profiles": []}}}

            def update_group_data(self, _group_id, key, updater):
                self.config[key] = updater(self.config.get(key))
                return self.config

        fake = FakeGroupConfig()
        with patch.object(services, "remote_data_mode", return_value=False), patch.object(
            services, "group_config_module", return_value=fake
        ):
            services.apply_game_server_profile_action(
                "100",
                "save",
                name="主服务器",
                websocket_url="ws://10.0.0.1:28016/ws?apiKey=secret-a",
            )
            services.apply_game_server_profile_action(
                "100",
                "save",
                name="备用服务器",
                websocket_url="ws://10.0.0.2:28017/ws?apiKey=secret-b",
            )

            profiles = fake.config["features"]["game_servers"]["profiles"]
            self.assertEqual(2, len(profiles))
            first_id = profiles[0]["id"]
            second_id = profiles[1]["id"]
            self.assertEqual(first_id, fake.config["features"]["game_servers"]["active_profile_id"])

            services.apply_game_server_profile_action(
                "100", "activate", profile_id=second_id
            )
            self.assertEqual(second_id, fake.config["features"]["game_servers"]["active_profile_id"])

            services.apply_game_server_profile_action(
                "100", "delete", profile_id=second_id
            )
            self.assertEqual([first_id], [item["id"] for item in fake.config["features"]["game_servers"]["profiles"]])
            self.assertEqual(first_id, fake.config["features"]["game_servers"]["active_profile_id"])

    def test_profile_keeps_ftp_database_link(self):
        from web_client import services

        class FakeGroupConfig:
            def __init__(self):
                self.config = {"features": {"game_servers": {"profiles": []}}}

            def update_group_data(self, _group_id, key, updater):
                self.config[key] = updater(self.config.get(key))
                return self.config

        fake = FakeGroupConfig()
        with patch.object(services, "remote_data_mode", return_value=False), patch.object(
            services, "group_config_module", return_value=fake
        ):
            services.apply_game_server_profile_action(
                "100",
                "save",
                name="主服务器",
                websocket_url="ws://10.0.0.1:28016/ws?apiKey=secret-a",
                integration_profile_id="server_a",
            )

        profile = fake.config["features"]["game_servers"]["profiles"][0]
        self.assertEqual("server_a", profile["integration_profile_id"])

    def test_web_field_accepts_and_canonicalizes_documented_websocket_url(self):
        field = FEATURE_BY_KEY["game_servers"].fields[1]
        values = parse_field_value(
            field,
            '{"id":"main","websocket_url":"wss://game.example:28016/ws?apiKey=secret"}',
        )
        parsed = json.loads(values[0])
        self.assertEqual("main", parsed["id"])
        self.assertEqual(28016, parsed["port"])
        self.assertEqual("plugin_websocket", parsed["type"])
        self.assertEqual("wss", parsed["websocket_scheme"])

    def test_api_key_is_encrypted_redacted_and_materialized_for_windows(self):
        profiles = store_game_server_profiles(
            [{"id": "main", "name": "主服", "type": "plugin_websocket", "host": "127.0.0.1", "port": 28016, "api_key": "secret"}],
            settings=TEST_SETTINGS,
        )
        self.assertNotIn("api_key", profiles[0])
        self.assertTrue(profiles[0]["api_key_ciphertext"])
        redacted = json.loads(redact_game_server_profiles(profiles)[0])
        self.assertEqual("", redacted["api_key"])
        self.assertTrue(redacted["api_key_configured"])
        materialized = materialize_game_server_secrets(
            {"features": {"game_servers": {"profiles": profiles}}},
            TEST_SETTINGS,
        )
        self.assertEqual(
            "secret",
            materialized["features"]["game_servers"]["profiles"][0]["api_key"],
        )

    def test_blank_api_key_keeps_existing_secret(self):
        previous = store_game_server_profiles(
            [{"id": "main", "type": "plugin_websocket", "host": "127.0.0.1", "port": 28016, "api_key": "secret"}],
            settings=TEST_SETTINGS,
        )
        current = store_game_server_profiles(
            [{"id": "main", "type": "plugin_websocket", "host": "127.0.0.1", "port": 28016, "api_key": ""}],
            previous,
            settings=TEST_SETTINGS,
        )
        self.assertEqual(previous[0]["api_key_ciphertext"], current[0]["api_key_ciphertext"])

    def test_plugin_websocket_api_key_is_encrypted_and_materialized(self):
        profiles = store_game_server_profiles(
            [{
                "id": "plugin",
                "type": "plugin_websocket",
                "host": "127.0.0.1",
                "port": 28016,
                "api_key": "plugin-secret",
            }],
            settings=TEST_SETTINGS,
        )
        self.assertNotIn("api_key", profiles[0])
        self.assertTrue(profiles[0]["api_key_ciphertext"])
        redacted = json.loads(redact_game_server_profiles(profiles)[0])
        self.assertEqual("plugin_websocket", redacted["type"])
        self.assertEqual("", redacted["api_key"])
        self.assertTrue(redacted["api_key_configured"])
        materialized = materialize_game_server_secrets(
            {"features": {"game_servers": {"profiles": profiles}}},
            TEST_SETTINGS,
        )
        self.assertEqual(
            "plugin-secret",
            materialized["features"]["game_servers"]["profiles"][0]["api_key"],
        )


if __name__ == "__main__":
    unittest.main()
