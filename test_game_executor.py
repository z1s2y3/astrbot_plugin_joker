"""Tests for the Windows-local documented game plugin WebSocket executor."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


def _load_executor_module():
    root = Path(__file__).resolve().parent
    package_name = "_game_executor_test_package"
    package = ModuleType(package_name)
    package.__path__ = [str(root)]
    sys.modules[package_name] = package
    child_package = ModuleType(f"{package_name}.qqadmin")
    child_package.__path__ = [str(root / "qqadmin")]
    sys.modules[child_package.__name__] = child_package
    name = f"{package_name}.qqadmin.game_executor"
    spec = importlib.util.spec_from_file_location(name, root / "qqadmin" / "game_executor.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


game_executor = _load_executor_module()


class GameExecutorTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_disables_legacy_transport_without_migrating_it(self):
        settings = game_executor.normalize_game_executor_config(
            {
                "enabled": "true",
                "type": "rcon",
                "host": " 127.0.0.1 ",
                "port": "28016",
                "password": "secret",
                "timeout_seconds": 99,
            }
        )
        self.assertFalse(settings["enabled"])
        self.assertEqual("plugin_websocket", settings["type"])
        self.assertNotIn("password", settings)
        self.assertEqual("127.0.0.1", settings["host"])
        self.assertEqual(30.0, settings["timeout_seconds"])

    def test_profiles_route_groups_and_keep_first_duplicate(self):
        profiles = game_executor.normalize_game_executor_profiles(
            [
                '{"id":"main","name":"主服","groups":["100","200"],"websocket_url":"ws://10.0.0.1:28016/ws?apiKey=a"}',
                '{"id":"secondary","groups":["200","300"],"websocket_url":"ws://10.0.0.2:28017/ws?apiKey=b"}',
            ]
        )
        self.assertEqual(["100", "200"], profiles[0]["groups"])
        self.assertEqual(["300"], profiles[1]["groups"])
        selected = game_executor.select_game_executor_config(profiles, "300", {})
        self.assertEqual("secondary", selected["profile_id"])
        self.assertEqual("10.0.0.2", selected["host"])

    def test_profile_can_be_explicitly_disabled(self):
        selected = game_executor.select_game_executor_config(
            [{"id": "offline", "groups": ["100"], "enabled": False}],
            "100",
            {"enabled": True, "host": "fallback", "api_key": "fallback-key"},
        )
        self.assertEqual("offline", selected["profile_id"])
        self.assertFalse(selected["enabled"])

    def test_explicit_profile_selection_does_not_fall_back_to_global(self):
        selected = game_executor.select_game_executor_config(
            [
                {
                    "id": "main",
                    "groups": ["100"],
                    "enabled": True,
                    "websocket_url": "ws://10.0.0.1:28016/ws?apiKey=a",
                },
            ],
            "100",
            {"enabled": True, "websocket_url": "ws://fallback:28016/ws?apiKey=b"},
            profile_id="missing",
        )
        self.assertEqual("missing", selected["profile_id"])
        self.assertFalse(selected["enabled"])

    def test_group_route_can_keep_multiple_servers_for_explicit_selection(self):
        profiles = game_executor.normalize_game_executor_profiles(
            [
                {"id": "main", "websocket_url": "ws://10.0.0.1:28016/ws?apiKey=a"},
                {"id": "backup", "websocket_url": "ws://10.0.0.2:28017/ws?apiKey=b"},
            ],
            default_group_id="100",
            allow_multiple_for_group=True,
        )
        self.assertEqual(2, len(profiles))
        self.assertEqual(["100"], profiles[1]["groups"])

    async def test_plugin_websocket_adapter_receives_rendered_command(self):
        with patch.object(
            game_executor,
            "execute_plugin_websocket_command",
            return_value={"success": True, "code": "executed"},
        ) as execute:
            result = await game_executor.execute_configured_game_command(
                {
                    "enabled": True,
                    "type": "plugin_websocket",
                    "websocket_url": "ws://127.0.0.1:28016/ws?apiKey=secret",
                    "timeout_seconds": 5,
                },
                "#ChangeCurrencyBalance Normal 30 76561199616499552",
                group_id="100",
                user_id="42",
                steam_id="76561199616499552",
                profiles=[],
            )

        self.assertTrue(result["success"])
        self.assertEqual("76561199616499552", result["steam_id"])
        settings = execute.call_args.args[0]
        self.assertEqual("127.0.0.1", settings["host"])
        self.assertEqual("secret", settings["api_key"])
        self.assertEqual(5.0, settings["timeout_seconds"])
        self.assertEqual("#ChangeCurrencyBalance Normal 30 76561199616499552", execute.call_args.args[1])

    async def test_connection_test_uses_selected_plugin_websocket_profile(self):
        with patch.object(
            game_executor,
            "test_plugin_websocket_connection",
            return_value={"success": True, "code": "plugin_websocket_connected"},
        ) as test_connection:
            result = await game_executor.test_configured_game_connection(
                {},
                group_id="200",
                profile_id="secondary",
                profiles=[
                    {
                        "id": "secondary",
                        "groups": ["200"],
                        "enabled": True,
                        "websocket_url": "ws://10.0.0.2:28017/ws?apiKey=b",
                        "timeout_seconds": 6,
                    },
                ],
            )

        self.assertTrue(result["success"])
        self.assertEqual("secondary", result["profile_id"])
        self.assertEqual("200", result["group_id"])
        settings = test_connection.call_args.args[0]
        self.assertEqual("10.0.0.2", settings["host"])
        self.assertEqual("b", settings["api_key"])
        self.assertEqual(6.0, settings["timeout_seconds"])

    async def test_connection_test_can_select_second_profile_in_same_group(self):
        with patch.object(
            game_executor,
            "test_plugin_websocket_connection",
            return_value={"success": True, "code": "plugin_websocket_connected"},
        ) as test_connection:
            result = await game_executor.test_configured_game_connection(
                {},
                group_id="200",
                profile_id="secondary",
                profiles=[
                    {"id": "main", "groups": ["200"], "websocket_url": "ws://10.0.0.1:28016/ws?apiKey=a"},
                    {"id": "secondary", "groups": ["200"], "websocket_url": "ws://10.0.0.2:28017/ws?apiKey=b"},
                ],
            )

        self.assertTrue(result["success"])
        self.assertEqual("secondary", result["profile_id"])
        self.assertEqual("10.0.0.2", test_connection.call_args.args[0]["host"])


if __name__ == "__main__":
    unittest.main()
