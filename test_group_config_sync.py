"""Regression tests for preserving group runtime data during global sync."""

import importlib.util
import logging
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import ModuleType

from web_client.feature_schema import FEATURE_BY_KEY, parse_field_value


def _load_group_config_module():
    astrbot = ModuleType("astrbot")
    astrbot.__path__ = []
    core = ModuleType("astrbot.core")
    core.__path__ = []
    utils = ModuleType("astrbot.core.utils")
    utils.__path__ = []
    astrbot_path = ModuleType("astrbot.core.utils.astrbot_path")
    astrbot_path.get_astrbot_data_path = lambda: tempfile.gettempdir()
    api = ModuleType("astrbot.api")
    api.logger = logging.getLogger("test-group-config-sync")

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.utils"] = utils
    sys.modules["astrbot.core.utils.astrbot_path"] = astrbot_path
    sys.modules["astrbot.api"] = api

    module_path = Path(__file__).parent / "qqadmin" / "group_config.py"
    spec = importlib.util.spec_from_file_location("_group_config_sync_test", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


group_config = _load_group_config_module()


class TestGlobalConfigSyncPreservation(unittest.TestCase):
    def test_fresh_empty_group_config_directory_is_not_authoritative(self):
        """First-run/recuperated empty directories must not tombstone portal rows."""

        with tempfile.TemporaryDirectory() as temporary_dir:
            original_data_path = group_config.get_plugin_data_path
            group_config._config_cache.clear()
            group_config.get_plugin_data_path = lambda: Path(temporary_dir)
            try:
                configs, complete = group_config.load_group_configs_with_health()
            finally:
                group_config._config_cache.clear()
                group_config.get_plugin_data_path = original_data_path

        self.assertEqual({}, configs)
        self.assertFalse(complete)

    def test_previously_initialized_empty_group_config_directory_is_complete(self):
        """After an actual local config existed, an empty set can be intentional."""

        with tempfile.TemporaryDirectory() as temporary_dir:
            original_data_path = group_config.get_plugin_data_path
            group_config._config_cache.clear()
            group_config.get_plugin_data_path = lambda: Path(temporary_dir)
            try:
                self.assertTrue(
                    group_config.save_group_config(
                        "1102579807", {"group_id": "1102579807", "features": {}}
                    )
                )
                group_config.get_group_config_file("1102579807").unlink()
                group_config._config_cache.clear()
                configs, complete = group_config.load_group_configs_with_health()
            finally:
                group_config._config_cache.clear()
                group_config.get_plugin_data_path = original_data_path

        self.assertEqual({}, configs)
        self.assertTrue(complete)

    def test_audit_id_mode_is_preserved_for_id_verification(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            original_data_path = group_config.get_plugin_data_path
            group_config._config_cache.clear()
            group_config.get_plugin_data_path = lambda: Path(temporary_dir)
            try:
                self.assertTrue(
                    group_config.save_group_config(
                        "1102579807",
                        {
                            "group_id": "1102579807",
                            "features": {"audit": {"approval_mode": "id"}},
                            "approval_mode": "id",
                        },
                    )
                )
                loaded = group_config.get_group_config("1102579807")
            finally:
                group_config._config_cache.clear()
                group_config.get_plugin_data_path = original_data_path
            self.assertEqual("id", loaded["features"]["audit"]["approval_mode"])
            self.assertEqual("id", loaded["approval_mode"])

    def test_default_stats_config_includes_signin_reward_tiers(self):
        stats = group_config.get_default_feature_config()["stats"]
        self.assertEqual(
            {
                "signin_base_reward": 10,
                "signin_reward_3_days": 30,
                "signin_reward_7_days": 100,
                "signin_reward_14_days": 200,
                "signin_reward_30_days": 500,
            },
            {key: stats[key] for key in (
                "signin_base_reward",
                "signin_reward_3_days",
                "signin_reward_7_days",
                "signin_reward_14_days",
                "signin_reward_30_days",
            )},
        )
        self.assertEqual(
            "steam-daily-example",
            stats["signin_profiles"][0]["id"],
        )
        self.assertFalse(stats["signin_profiles"][0]["enabled"])
        self.assertEqual("SteamID 绑定失败：{message}", stats["bind_failure_message"])
        self.assertEqual("", stats["database_path"])
        self.assertEqual([], stats["database_profiles"])

    def test_reset_game_configuration_restores_templates_and_preserves_runtime_data(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            original_data_path = group_config.get_plugin_data_path
            original_defaults = group_config._default_game_commands
            group_config._config_cache.clear()
            group_config.get_plugin_data_path = lambda: Path(temporary_dir)
            group_config._default_game_commands = lambda: [
                {"id": "currency", "trigger": "ChangeCurrencyBalance"},
                {"id": "spawnzombie", "trigger": "spawnzombie"},
                {"id": "addxiongb", "trigger": "addxiongb", "operation": "database_credit"},
                {"id": "redxiongb", "trigger": "redxiongb", "operation": "database_debit"},
            ]
            try:
                group_config.save_group_config(
                    "1102579807",
                    {
                        "group_id": "1102579807",
                        "features": {
                            "stats": {
                                "enabled": False,
                                "signin_mode": "basic",
                                "database_profile": "main",
                                "database_profiles": [{"id": "main", "path": "data/main.db"}],
                                "signin_profiles": [{"id": "old", "command": "旧签到"}],
                            },
                            "game_commands": {"enabled": False, "commands": [{"id": "old"}]},
                            "game_servers": {
                                "enabled": True,
                                "profiles": [{"id": "main", "host": "10.0.0.1", "password": "secret"}],
                            },
                        },
                        "steam_signin": {"bindings": {"76561199616499552": {"user_id": "42"}}},
                        "signin": {"42": {"last_signin": "2026-08-24"}},
                    },
                )
                restored = group_config.reset_game_configuration("1102579807")
            finally:
                group_config._config_cache.clear()
                group_config.get_plugin_data_path = original_data_path
                group_config._default_game_commands = original_defaults

        features = restored["features"]
        self.assertTrue(features["stats"]["enabled"])
        self.assertEqual("steam", features["stats"]["signin_mode"])
        self.assertEqual("websocket", features["stats"]["steam_reward_mode"])
        self.assertEqual("main", features["stats"]["database_profile"])
        self.assertEqual([{"id": "main", "path": "data/main.db"}], features["stats"]["database_profiles"])
        self.assertEqual(
            "steam-daily-example",
            features["stats"]["signin_profiles"][0]["id"],
        )
        self.assertFalse(features["stats"]["signin_profiles"][0]["enabled"])
        self.assertEqual(
            ["currency", "spawnzombie", "addxiongb", "redxiongb"],
            [row["id"] for row in features["game_commands"]["commands"]],
        )
        self.assertTrue(features["game_servers"]["enabled"])
        self.assertEqual("secret", features["game_servers"]["profiles"][0]["password"])
        self.assertIn("76561199616499552", restored["steam_signin"]["bindings"])
        self.assertEqual("2026-08-24", restored["signin"]["42"]["last_signin"])

    def test_message_cache_defaults_include_background_image_archive(self):
        cache = group_config.get_default_feature_config()["message_cache"]
        self.assertEqual(True, cache["archive_images"])
        self.assertEqual(20, cache["archive_image_max_mb"])

    def test_scum_defaults_include_per_group_command_names(self):
        scum = group_config.get_default_feature_config()["scum"]
        self.assertTrue(scum["enabled"])
        self.assertEqual("read_only", scum["permission_mode"])
        self.assertEqual("查询", scum["keyword_command"])
        self.assertEqual("查询在线", scum["online_command"])
        self.assertNotIn("server_command", scum)
        recall = group_config.get_default_feature_config()["recall"]
        self.assertEqual("all", recall["member_scope"])

    def test_web_and_windows_policy_defaults_match(self):
        windows = group_config.get_default_feature_config()
        for feature_key, field_keys in {
            "blacklist": (
                "auto_kick_on_join",
                "notify_on_kick",
                "kick_reason",
                "max_auto_kick_per_day",
            ),
            "welcome": ("auto_reply_enabled",),
            "recall": ("self_recall",),
            "keyword_filter": ("filter_keywords",),
            "group_config": ("nickname", "tags", "priority", "group_note", "timezone"),
            "entertainment": ("music_sources",),
        }.items():
            web_defaults = {
                field.key: field.default
                for field in FEATURE_BY_KEY[feature_key].fields
            }
            for field_key in field_keys:
                self.assertEqual(
                    web_defaults[field_key],
                    windows[feature_key][field_key],
                    f"{feature_key}.{field_key} drifted between web and Windows",
                )

    def test_music_sources_are_group_only_and_require_a_selection(self):
        field = next(
            item
            for item in FEATURE_BY_KEY["entertainment"].fields
            if item.key == "music_sources"
        )
        self.assertTrue(field.group_only)
        self.assertEqual(
            ["qq", "163"],
            parse_field_value(field, ["qq", "163"], True),
        )
        self.assertEqual(
            ["kugou", "soda"],
            parse_field_value(field, ["kugou", "soda"], True),
        )
        with self.assertRaisesRegex(ValueError, "至少选择一项"):
            parse_field_value(field, [], True)

    def test_global_stats_policy_copy_keeps_command_and_scheme_fields(self):
        target = {}
        group_config._copy_global_stats_policy(
            {
                "signin_command": "每日签到",
                "bind_command": "绑定游戏",
                "database_profiles": [{"id": "main", "path": "data/main.db"}],
                "signin_profiles": [{"id": "daily", "command": "日签"}],
            },
            target,
        )
        self.assertEqual("每日签到", target["signin_command"])
        self.assertEqual("绑定游戏", target["bind_command"])
        self.assertEqual(
            [{"id": "main", "path": "data/main.db"}],
            target["database_profiles"],
        )
        self.assertEqual(
            [{"id": "daily", "command": "日签"}],
            target["signin_profiles"],
        )

    def test_runtime_data_and_unknown_feature_settings_survive_sync(self):
        existing = {
            "group_id": "123",
            "features": {
                "power_status": "on",
                "timed_message": {"enabled": True, "local_label": "daily"},
                "future_feature": {"enabled": True, "value": 1},
            },
            "timed_messages": [{"id": "daily", "message": "hello"}],
            "timed_message_settings": {"enabled": True},
            "signin": {"42": {"last_signin": "2026-08-15"}},
            "invite_stats": {"invites": [{"invitee_id": "7"}]},
            "reply_data": {"hello": "world"},
        }
        replacement = {
            "group_id": "123",
            "features": {
                "timed_message": {"enabled": False},
                "stats": {"enabled": True},
            },
        }

        group_config._preserve_group_local_runtime_data(existing, replacement)
        group_config._preserve_power_status(existing, replacement)

        self.assertEqual(existing["timed_messages"], replacement["timed_messages"])
        self.assertEqual(existing["timed_message_settings"], replacement["timed_message_settings"])
        self.assertEqual(existing["signin"], replacement["signin"])
        self.assertEqual(existing["invite_stats"], replacement["invite_stats"])
        self.assertEqual(existing["reply_data"], replacement["reply_data"])
        self.assertEqual("on", replacement["features"]["power_status"])
        self.assertFalse(replacement["features"]["timed_message"]["enabled"])
        self.assertEqual("daily", replacement["features"]["timed_message"]["local_label"])
        self.assertEqual(
            {"enabled": True, "value": 1},
            replacement["features"]["future_feature"],
        )

    def test_global_sync_preserves_group_music_source_selection(self):
        existing = {
            "group_id": "123",
            "features": {"entertainment": {"music_sources": ["163"]}},
        }
        replacement = {
            "group_id": "123",
            "features": {"entertainment": {"music_sources": ["qq", "163"]}},
        }

        group_config._preserve_group_local_runtime_data(existing, replacement)

        self.assertEqual(
            ["163"], replacement["features"]["entertainment"]["music_sources"]
        )


class TestPortalSyncNotifications(unittest.TestCase):
    """Local edits should wake sync only when their web projection changes."""

    def setUp(self):
        self._temporary_dir = tempfile.TemporaryDirectory()
        self._original_data_path = group_config.get_plugin_data_path
        group_config._config_cache.clear()
        group_config.get_plugin_data_path = lambda: Path(self._temporary_dir.name)

    def tearDown(self):
        group_config.clear_group_config_change_listener()
        group_config._config_cache.clear()
        group_config.get_plugin_data_path = self._original_data_path
        self._temporary_dir.cleanup()

    def test_web_editable_local_changes_request_one_sync(self):
        notified = []
        token = group_config.set_group_config_change_listener(notified.append)

        group_config.update_group_data(
            "1102579807",
            "replies",
            lambda _existing: {"exact": {"你好": {"reply": "你好呀"}}},
        )

        self.assertEqual(["1102579807"], notified)
        group_config.clear_group_config_change_listener(token)

    def test_runtime_only_change_does_not_wake_portal_sync(self):
        notified = []
        group_config.set_group_config_change_listener(notified.append)

        group_config.update_group_data(
            "1102579807",
            "signin",
            lambda _existing: {"2650362143": {"days": 1}},
        )

        self.assertEqual([], notified)

    def test_blacklist_runtime_change_wakes_runtime_listener_only(self):
        config_notified = []
        runtime_notified = []
        group_config.set_group_config_change_listener(config_notified.append)
        group_config.set_group_runtime_change_listener(runtime_notified.append)

        group_config.update_group_data(
            "1102579807",
            "blacklist",
            lambda _existing: {},
        )

        self.assertEqual([], config_notified)
        self.assertEqual(["1102579807"], runtime_notified)

    def test_full_save_ignores_runtime_only_changes(self):
        initial = {
            "group_id": "1102579807",
            "features": {"recall": {"enabled": False}},
            "signin": {"2650362143": {"days": 1}},
        }
        self.assertTrue(group_config.save_group_config("1102579807", initial))

        notified = []
        group_config.set_group_config_change_listener(notified.append)

        runtime_only = deepcopy(initial)
        runtime_only["signin"] = {"2650362143": {"days": 2}}
        self.assertTrue(group_config.save_group_config("1102579807", runtime_only))
        self.assertEqual([], notified)

        portal_edit = deepcopy(runtime_only)
        portal_edit["features"]["recall"]["enabled"] = True
        self.assertTrue(group_config.save_group_config("1102579807", portal_edit))
        self.assertEqual(["1102579807"], notified)

    def test_command_feature_toggle_updates_web_compatibility_mirrors(self):
        group_config.save_group_config(
            "1102579807",
            {
                "group_id": "1102579807",
                "features": {
                    "mute": {"enabled": True},
                    "audit": {"enabled": True},
                },
                "mute_settings": {"enabled": True, "duration": 300},
                "audit_enabled": True,
            },
        )

        group_config.set_feature_enabled("1102579807", "mute_enabled", False)
        group_config.set_feature_enabled("1102579807", "audit_enabled", False)
        stored = group_config.load_group_config("1102579807")

        self.assertFalse(stored["features"]["mute"]["enabled"])
        self.assertFalse(stored["mute_settings"]["enabled"])
        self.assertFalse(stored["features"]["audit"]["enabled"])
        self.assertFalse(stored["audit_enabled"])


if __name__ == "__main__":
    unittest.main()
