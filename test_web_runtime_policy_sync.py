"""Regression coverage for portal settings consumed by Windows runtime helpers."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import unittest
from unittest.mock import patch

from web_client.feature_schema import FEATURE_BY_KEY
from web_client.services import (
    PortalError,
    add_auto_reply,
    add_group_sub_admin,
    add_timed_task,
    apply_feature_values,
    delete_group_sub_admin,
    get_feature_values,
    toggle_timed_task,
    update_timed_task,
)
from web_client.services import (
    _normalized_sub_admin_permissions,
    _portal_sub_admin_record,
)
from web_client.sync_projection import (
    merge_group_projection,
    merge_portal_command_projection,
    project_group_config,
)


class FakeGroupConfig:
    def __init__(self, initial: dict):
        self.configs = {"1102579807": deepcopy(initial)}

    def get_group_config(self, group_id: str) -> dict:
        return self.configs.setdefault(
            str(group_id), {"group_id": str(group_id), "features": {}}
        )

    def batch_update_group_data(self, group_id: str, updates: list) -> dict:
        config = self.get_group_config(group_id)
        for key, updater in updates:
            config[key] = updater(config.get(key))
        return config


def _feature_values(feature_key: str, **overrides) -> dict:
    values = {
        field.key: deepcopy(field.default)
        for field in FEATURE_BY_KEY[feature_key].fields
    }
    values.update(overrides)
    return values


class RuntimePolicyMirrorTests(unittest.TestCase):
    def test_linux_command_projection_wins_without_replacing_runtime_policy(self):
        local = {
            "group_id": "1102579807",
            "features": {
                "stats": {
                    "signin_command": "旧签到",
                    "bind_command": "旧绑定",
                    "signin_profiles": [{"id": "daily", "command": "旧方案"}],
                    "signin_base_reward": 99,
                },
                "scum": {
                    "keyword_command": "旧查询",
                    "online_command": "旧在线",
                    "permission_mode": "read_only",
                },
                "recall": {"enabled": True, "keywords": ["本地规则"]},
            },
            "blacklist": [{"user_id": "123456789", "reason": "runtime"}],
        }
        portal = {
            "group_id": "1102579807",
            "features": {
                "stats": {
                    "signin_command": "签到网页",
                    "bind_command": "绑定网页",
                    "signin_profiles": [{"id": "daily", "command": "网页方案"}],
                    "signin_base_reward": 10,
                },
                "scum": {
                    "keyword_command": "查网页",
                    "online_command": "在线网页",
                    "permission_mode": "full_access",
                },
                "recall": {"enabled": False, "keywords": ["网页规则"]},
            },
        }
        merged = merge_portal_command_projection(local, portal)
        self.assertEqual("签到网页", merged["features"]["stats"]["signin_command"])
        self.assertEqual("网页方案", merged["features"]["stats"]["signin_profiles"][0]["command"])
        self.assertEqual("查网页", merged["features"]["scum"]["keyword_command"])
        self.assertEqual("read_only", merged["features"]["scum"]["permission_mode"])
        self.assertEqual({"user_id": "123456789", "reason": "runtime"}, merged["blacklist"][0])
        self.assertEqual(["本地规则"], merged["features"]["recall"]["keywords"])

    def test_new_portal_sub_admin_defaults_to_reply_only(self):
        self.assertEqual(["reply"], _normalized_sub_admin_permissions(None))

    def test_legacy_sub_admin_display_matches_windows_reply_only_default(self):
        record = _portal_sub_admin_record("2650362143")
        self.assertIsNotNone(record)
        self.assertEqual(["reply"], record["permissions"])

    def test_web_recall_time_updates_bot_self_recall_time(self):
        fake = FakeGroupConfig({"group_id": "1102579807", "features": {}})
        values = _feature_values(
            "recall", enabled=True, self_recall=True, time_limit=95
        )
        with (
            patch("web_client.services.group_config_module", return_value=fake),
            patch("web_client.services.remote_data_mode", return_value=False),
        ):
            apply_feature_values("1102579807", "recall", values)

        recall = fake.configs["1102579807"]["features"]["recall"]
        self.assertTrue(recall["self_recall_enabled"])
        self.assertEqual(95, recall["self_recall_time"])

    def test_web_save_updates_legacy_runtime_mirrors_without_dropping_fields(self):
        fake = FakeGroupConfig(
            {
                "group_id": "1102579807",
                "features": {},
                "blacklist_settings": {"legacy_flag": "keep"},
                "mute_settings": {"local_only": "keep"},
                "kick_settings": {"notify_type": "private"},
            }
        )
        with (
            patch("web_client.services.group_config_module", return_value=fake),
            patch("web_client.services.remote_data_mode", return_value=False),
        ):
            apply_feature_values(
                "1102579807",
                "blacklist",
                _feature_values(
                    "blacklist",
                    enabled=True,
                    auto_kick=False,
                    auto_blacklist_on_leave=True,
                    auto_blacklist_on_verify_fail=True,
                ),
            )
            apply_feature_values(
                "1102579807",
                "mute",
                _feature_values("mute", enabled=True, level=8, duration=720),
            )
            apply_feature_values(
                "1102579807",
                "kick",
                _feature_values("kick", enabled=True, auto_blacklist=True, limit=9),
            )
            apply_feature_values(
                "1102579807",
                "audit",
                _feature_values("audit", enabled=True, admin_bypass=False),
            )
            displayed_mute = get_feature_values("1102579807", "mute")
            displayed_kick = get_feature_values("1102579807", "kick")

        config = fake.configs["1102579807"]
        self.assertEqual(
            {
                "legacy_flag": "keep",
                **_feature_values(
                    "blacklist",
                    enabled=True,
                    auto_kick=False,
                    auto_blacklist_on_leave=True,
                    auto_blacklist_on_verify_fail=True,
                ),
            },
            config["blacklist_settings"],
        )
        self.assertEqual(
            {
                "local_only": "keep",
                **_feature_values("mute", enabled=True, level=8, duration=720),
            },
            config["mute_settings"],
        )
        self.assertEqual(
            {
                "notify_type": "private",
                **_feature_values(
                    "kick", enabled=True, auto_blacklist=True, limit=9
                ),
            },
            config["kick_settings"],
        )
        self.assertFalse(config["admin_bypass"])
        self.assertEqual(720, displayed_mute["duration"])
        self.assertEqual(9, displayed_kick["limit"])

    def test_scum_command_names_round_trip_through_portal_projection(self):
        fake = FakeGroupConfig(
            {"group_id": "1102579807", "features": {}}
        )
        values = _feature_values(
            "scum",
            enabled=True,
            keyword_command="查服",
            online_command="在线状态",
        )
        with (
            patch("web_client.services.group_config_module", return_value=fake),
            patch("web_client.services.remote_data_mode", return_value=False),
        ):
            apply_feature_values("1102579807", "scum", values)

        projected = project_group_config(fake.configs["1102579807"])
        merged = merge_group_projection(
            {"group_id": "1102579807", "features": {}}, projected
        )
        self.assertEqual(values, merged["features"]["scum"])

    def test_scum_command_names_must_be_unique_and_not_cross_shadow_aliases(self):
        fake = FakeGroupConfig({"group_id": "1102579807", "features": {}})
        duplicate = _feature_values(
            "scum",
            keyword_command="查询1",
            online_command="查询1",
        )
        with (
            patch("web_client.services.group_config_module", return_value=fake),
            patch("web_client.services.remote_data_mode", return_value=False),
        ):
            with self.assertRaisesRegex(PortalError, "不能重复"):
                apply_feature_values("1102579807", "scum", duplicate)

            cross_shadow = _feature_values(
                "scum",
                keyword_command="查询在线",
                online_command="在线状态",
            )
            with self.assertRaisesRegex(PortalError, "保留指令"):
                apply_feature_values("1102579807", "scum", cross_shadow)

    def test_stats_database_path_projection_rejects_absolute_and_traversal(self):
        absolute = project_group_config(
            {
                "group_id": "1102579807",
                "features": {
                    "stats": {
                        "database_path": "C:/Windows/System32/config/SAM",
                        "database_profiles": [],
                    }
                },
            }
        )
        self.assertEqual("", absolute["features"]["stats"]["database_path"])

        traversal = project_group_config(
            {
                "group_id": "1102579807",
                "features": {
                    "stats": {
                        "database_path": "data/../outside.db",
                        "database_profiles": [],
                    }
                },
            }
        )
        self.assertEqual("", traversal["features"]["stats"]["database_path"])

        relative = project_group_config(
            {
                "group_id": "1102579807",
                "features": {
                    "stats": {
                        "database_path": "data/players.db",
                        "database_profiles": [],
                    }
                },
            }
        )
        self.assertEqual(
            "data/players.db", relative["features"]["stats"]["database_path"]
        )

    def test_remote_web_save_updates_the_same_windows_runtime_mirrors(self):
        """Remote portal saves must return a config Windows can execute as-is."""
        remote_config = {
            "group_id": "1102579807",
            "features": {},
            "mute_settings": {"local_only": "keep"},
            "kick_settings": {"notify_type": "private"},
        }
        saved: list[tuple[object, str, dict, int | None]] = []

        def save_web_config(db, group_id, config, updated_by):
            saved.append((db, group_id, deepcopy(config), updated_by))

        fake_db = object()
        with (
            patch("web_client.services.remote_data_mode", return_value=True),
            patch("web_client.services.remote_store.get_config", return_value=remote_config),
            patch("web_client.services.remote_store.save_web_config", side_effect=save_web_config),
        ):
            apply_feature_values(
                "1102579807",
                "mute",
                _feature_values("mute", enabled=True, level=6, duration=900),
                db=fake_db,
                updated_by=42,
            )
            apply_feature_values(
                "1102579807",
                "kick",
                _feature_values("kick", enabled=True, auto_blacklist=True, limit=7),
                db=fake_db,
                updated_by=42,
            )

        self.assertEqual(2, len(saved))
        self.assertEqual(
            {
                "local_only": "keep",
                **_feature_values("mute", enabled=True, level=6, duration=900),
            },
            remote_config["mute_settings"],
        )
        self.assertEqual(
            {
                "notify_type": "private",
                **_feature_values(
                    "kick", enabled=True, auto_blacklist=True, limit=7
                ),
            },
            remote_config["kick_settings"],
        )
        self.assertEqual(42, saved[-1][3])

    def test_projection_round_trip_updates_policy_and_preserves_local_extras(self):
        projected = project_group_config(
            {
                "group_id": "1102579807",
                "features": {
                    "mute": {"enabled": True, "level": 7, "duration": 600},
                    "kick": {"enabled": True, "auto_blacklist": True, "limit": 5},
                },
                "admin_bypass": False,
                "blacklist_settings": {
                    "enabled": True,
                    "auto_kick": False,
                    "auto_blacklist_on_leave": True,
                    "auto_blacklist_on_verify_fail": True,
                    "private_runtime_value": "must-not-upload",
                },
                "mute_settings": {
                    "enabled": True,
                    "level": 7,
                    "duration": 600,
                    "local_only": "must-not-upload",
                },
                "kick_settings": {
                    "enabled": True,
                    "auto_blacklist": True,
                    "limit": 5,
                    "notify_type": "private",
                },
            }
        )

        self.assertFalse(projected["admin_bypass"])
        self.assertNotIn("private_runtime_value", projected["blacklist_settings"])
        self.assertNotIn("local_only", projected["mute_settings"])
        self.assertNotIn("notify_type", projected["kick_settings"])

        merged = merge_group_projection(
            {
                "group_id": "1102579807",
                "features": {},
                "blacklist_settings": {"private_runtime_value": "keep"},
                "mute_settings": {"local_only": "keep", "duration": 1},
                "kick_settings": {"notify_type": "private", "limit": 1},
            },
            projected,
        )

        self.assertFalse(merged["admin_bypass"])
        self.assertEqual("keep", merged["blacklist_settings"]["private_runtime_value"])
        self.assertEqual("keep", merged["mute_settings"]["local_only"])
        self.assertEqual("private", merged["kick_settings"]["notify_type"])
        self.assertEqual(600, merged["mute_settings"]["duration"])
        self.assertEqual(5, merged["kick_settings"]["limit"])

    def test_old_remote_projection_does_not_remove_windows_policy_mirrors(self):
        """An unrelated portal save must not erase legacy Windows settings."""
        merged = merge_group_projection(
            {
                "group_id": "1102579807",
                "features": {"mute": {"enabled": True}},
                "blacklist_settings": {"enabled": True, "auto_kick": False},
                "mute_settings": {"enabled": True, "level": 7, "duration": 600},
                "kick_settings": {"enabled": True, "auto_blacklist": True, "limit": 5},
            },
            {
                "group_id": "1102579807",
                "features": {"welcome": {"enabled": True}},
                # This models a snapshot written before nested runtime
                # mirrors were included in the portal projection.
            },
        )

        self.assertEqual(
            {"enabled": True, "auto_kick": False}, merged["blacklist_settings"]
        )
        self.assertEqual(
            {"enabled": True, "level": 7, "duration": 600}, merged["mute_settings"]
        )
        self.assertEqual(
            {"enabled": True, "auto_blacklist": True, "limit": 5}, merged["kick_settings"]
        )
        self.assertEqual({"enabled": True}, merged["features"]["mute"])

    def test_sparse_projection_does_not_clear_unrelated_windows_features(self):
        """A web save from an older snapshot must preserve local-only defaults."""
        projected = project_group_config(
            {
                "group_id": "1102579807",
                "features": {"welcome": {"enabled": True}},
            }
        )
        self.assertEqual({"welcome": {"enabled": True}}, projected["features"])

        merged = merge_group_projection(
            {
                "group_id": "1102579807",
                "features": {
                    "welcome": {"enabled": False},
                    "mute": {"enabled": True, "duration": 600},
                },
            },
            projected,
        )

        self.assertTrue(merged["features"]["welcome"]["enabled"])
        self.assertEqual(
            {"enabled": True, "duration": 600}, merged["features"]["mute"]
        )

    def test_sparse_projection_does_not_clear_local_management_collections(self):
        """An older portal snapshot must not erase Windows-owned group data."""
        merged = merge_group_projection(
            {
                "group_id": "1102579807",
                "features": {"welcome": {"enabled": False}},
                "timed_messages": [{"id": "morning", "message": "早安"}],
                "replies": {"exact": {"你好": {"reply": "你好呀"}}},
                "sub_admins": ["2650362143"],
                "player_replies": {"10001": {"messages": ["欢迎"]}},
            },
            {
                "group_id": "1102579807",
                "features": {"welcome": {"enabled": True}},
                # No collection keys: models a snapshot from before their
                # corresponding portal management forms existed.
            },
        )

        self.assertEqual(
            [{"id": "morning", "message": "早安"}], merged["timed_messages"]
        )
        self.assertEqual(
            {"exact": {"你好": {"reply": "你好呀"}}}, merged["replies"]
        )
        self.assertEqual(["2650362143"], merged["sub_admins"])
        self.assertEqual(
            {"10001": {"messages": ["欢迎"]}}, merged["player_replies"]
        )

    def test_empty_management_collections_are_explicit_deletions(self):
        """The current web forms use empty collections to remove data."""
        merged = merge_group_projection(
            {
                "group_id": "1102579807",
                "features": {},
                "timed_messages": [{"id": "morning"}],
                "replies": {"exact": {"你好": {"reply": "你好呀"}}},
                "sub_admins": ["2650362143"],
                "player_replies": {"10001": {"messages": ["欢迎"]}},
            },
            {
                "group_id": "1102579807",
                "features": {},
                "timed_messages": [],
                "replies": {},
                "sub_admins": [],
                "player_replies": {},
            },
        )

        self.assertEqual([], merged["timed_messages"])
        self.assertEqual({}, merged["replies"])
        self.assertEqual([], merged["sub_admins"])
        self.assertEqual({}, merged["player_replies"])

    def test_empty_nested_policy_clears_managed_fields_but_keeps_local_extras(self):
        """An explicit empty policy mirror must not resurrect old web values."""

        merged = merge_group_projection(
            {
                "group_id": "1102579807",
                "features": {},
                "blacklist_settings": {
                    "enabled": True,
                    "auto_kick": True,
                    "local_runtime_flag": "keep",
                },
                "mute_settings": {
                    "enabled": True,
                    "duration": 600,
                    "local_runtime_flag": "keep",
                },
                "kick_settings": {
                    "enabled": True,
                    "limit": 3,
                    "local_runtime_flag": "keep",
                },
                "lottery": {
                    "enabled": True,
                    "keyword": "抽奖",
                    "local_runtime_flag": "keep",
                },
            },
            {
                "group_id": "1102579807",
                "features": {},
                "blacklist_settings": {},
                "mute_settings": {},
                "kick_settings": {},
                "lottery": {},
            },
            explicit_empty_deletes=True,
        )

        self.assertEqual(
            {"local_runtime_flag": "keep"}, merged["blacklist_settings"]
        )
        self.assertEqual({"local_runtime_flag": "keep"}, merged["mute_settings"])
        self.assertEqual({"local_runtime_flag": "keep"}, merged["kick_settings"])
        self.assertEqual({"local_runtime_flag": "keep"}, merged["lottery"])

        feature_cleared = merge_group_projection(
            {
                "group_id": "1102579807",
                "features": {
                    "reply": {"enabled": True},
                    "welcome": {"enabled": True},
                    "legacy_local": {"enabled": True},
                },
            },
            {"group_id": "1102579807", "features": {}},
            explicit_empty_deletes=True,
        )
        self.assertNotIn("reply", feature_cleared["features"])
        self.assertNotIn("welcome", feature_cleared["features"])
        self.assertIn("legacy_local", feature_cleared["features"])

    def test_customer_managed_collections_round_trip_to_windows(self):
        """Each add-form payload is retained in the remote group projection."""
        source = {
            "group_id": "1102579807",
            "features": {"reply": {"enabled": True}},
            "timed_messages": [{"id": "morning", "cron_expr": "0 9 * * *"}],
            "replies": {"exact": {"你好": {"reply": "你好呀", "type": "exact"}}},
            "sub_admins": [{"user_id": "2650362143", "permissions": ["mute"]}],
            "player_replies": {
                "10001": {"messages": ["欢迎"], "at_user": True, "enabled": True}
            },
        }

        projected = project_group_config(source)
        for key in ("timed_messages", "replies", "sub_admins", "player_replies"):
            self.assertEqual(source[key], projected[key])

        merged = merge_group_projection(
            {"group_id": "1102579807", "features": {}}, projected
        )
        for key in ("timed_messages", "replies", "sub_admins", "player_replies"):
            self.assertEqual(source[key], merged[key])

    def test_reply_projection_hides_windows_image_sources_but_keeps_cache(self):
        """Linux stores opaque asset metadata, never local paths or URLs."""
        source = {
            "group_id": "1102579807",
            "features": {},
            "replies": {
                "exact": {
                    "图片": {
                        "reply": "已收到",
                        "type": "exact",
                        "has_images": True,
                        "image_portal_assets": [
                            {"id": 7, "sha256": "A" * 64},
                        ],
                        "image_cache_refs": ["cache://windows-only"],
                        "image_urls": ["https://private.invalid/image"],
                        "image_source_urls": ["C:/private/image.webp"],
                        "image_cache_status": "ready",
                    }
                }
            },
        }

        projected = project_group_config(source)
        record = projected["replies"]["exact"]["图片"]
        self.assertEqual(
            [{"id": 7, "sha256": "a" * 64}],
            record["image_portal_assets"],
        )
        self.assertEqual("ready", record["image_cache_status"])
        for field in ("image_cache_refs", "image_urls", "image_source_urls"):
            self.assertNotIn(field, record)

        merged = merge_group_projection(source, projected)
        self.assertEqual(
            ["cache://windows-only"],
            merged["replies"]["exact"]["图片"]["image_cache_refs"],
        )


class RemoteOperationRetryTests(unittest.TestCase):
    """Web operations must be safe to retry after a proxy response timeout."""

    @contextmanager
    def _remote_patches(self, config: dict, saved: list[dict]):
        def save_web_config(_db, _group_id, saved_config, _updated_by):
            saved.append(deepcopy(saved_config))

        with (
            patch("web_client.services.remote_data_mode", return_value=True),
            patch("web_client.services.remote_store.get_config", return_value=config),
            patch(
                "web_client.services.remote_store.save_web_config",
                side_effect=save_web_config,
            ),
        ):
            yield

    def test_timed_task_content_prevents_timeout_duplicates(self):
        config = {"group_id": "1102579807", "features": {}, "timed_messages": []}
        saved: list[dict] = []
        payload = {
            "name": "每天早报",
            "cron_expr": "0 9 * * *",
            "message": "早上好",
            "action": "message",
            "at_all": False,
        }
        with self._remote_patches(config, saved):
            first = add_timed_task("1102579807", payload, "42", db=object())
            second = add_timed_task("1102579807", payload, "42", db=object())

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(config["timed_messages"]), 1)
        self.assertEqual(len(saved), 1)

    def test_remote_random_music_task_uses_automatic_interfaces_and_pool(self):
        config = {"group_id": "1102579807", "features": {}, "timed_messages": []}
        saved: list[dict] = []
        payload = {
            "name": "午间随机音乐",
            "cron_expr": "0 12 * * *",
            "message": "晴天|杨幂|稻香",
            "action": "random_music",
            "at_all": False,
        }
        with self._remote_patches(config, saved):
            result = add_timed_task("1102579807", payload, "42", db=object())

        self.assertEqual("random_music", result["action"])
        self.assertNotIn("music_provider", result)
        self.assertEqual("晴天|杨幂|稻香", config["timed_messages"][0]["message"])
        self.assertEqual(1, len(saved))

    def test_existing_hourly_task_can_enable_at_all_without_resetting_history(self):
        config = {
            "group_id": "1102579807",
            "features": {},
            "timed_messages": [
                {
                    "id": "hourly-report",
                    "name": "整点播报",
                    "action": "hourly_time",
                    "cron_expr": "0 * * * *",
                    "message": "请合理安排时间",
                    "at_all": False,
                    "enabled": True,
                    "sent_count": 24,
                    "last_sent_at": 1_700_000_000,
                }
            ],
        }
        saved: list[dict] = []
        payload = {
            "name": "整点播报",
            "action": "hourly_time",
            "cron_expr": "0 * * * *",
            "run_at": "",
            "message": "请合理安排时间",
            "at_all": True,
        }

        with self._remote_patches(config, saved):
            result = update_timed_task(
                "1102579807", "hourly-report", payload, "42", db=object()
            )

        task = config["timed_messages"][0]
        self.assertTrue(result["at_all"])
        self.assertTrue(task["at_all"])
        self.assertEqual(24, task["sent_count"])
        self.assertEqual(1_700_000_000, task["last_sent_at"])
        self.assertEqual(1, len(saved))

    def test_explicit_timed_task_state_is_safe_to_repeat(self):
        config = {
            "group_id": "1102579807",
            "features": {},
            "timed_messages": [{"id": "task-a", "enabled": True}],
        }
        saved: list[dict] = []
        with self._remote_patches(config, saved):
            self.assertFalse(
                toggle_timed_task(
                    "1102579807", "task-a", db=object(), updated_by=42, enabled=False
                )
            )
            self.assertFalse(
                toggle_timed_task(
                    "1102579807", "task-a", db=object(), updated_by=42, enabled=False
                )
            )

        self.assertFalse(config["timed_messages"][0]["enabled"])
        self.assertEqual(len(saved), 1)

    def test_reply_and_sub_admin_adds_upsert_instead_of_duplicate(self):
        config = {"group_id": "1102579807", "features": {}}
        saved: list[dict] = []
        with self._remote_patches(config, saved):
            add_auto_reply(
                "1102579807", "regex", r"^hello$", "你好", db=object(), updated_by=42
            )
            add_auto_reply(
                "1102579807", "regex", r"^hello$", "你好", db=object(), updated_by=42
            )
            add_group_sub_admin(
                "1102579807",
                "2650362143",
                "42",
                db=object(),
                permissions=["mute", "recall"],
            )
            add_group_sub_admin(
                "1102579807",
                "2650362143",
                "42",
                db=object(),
                permissions=["mute", "recall"],
            )

        self.assertEqual(len(config["replies"]["regex"]), 1)
        self.assertEqual(len(config["sub_admins"]), 1)
        self.assertEqual(config["sub_admins"][0]["permissions"], ["mute", "recall"])
        self.assertEqual(len(saved), 2)

    def test_legacy_qq_only_sub_admin_is_upgraded_not_duplicated(self):
        """Editing an old Windows record must keep one visible administrator."""
        config = {
            "group_id": "1102579807",
            "features": {},
            "sub_admins": ["2650362143"],
        }
        saved: list[dict] = []
        with self._remote_patches(config, saved):
            add_group_sub_admin(
                "1102579807",
                "2650362143",
                "42",
                db=object(),
                permissions=["kick"],
            )
            # A retry after a browser timeout must remain an idempotent save.
            add_group_sub_admin(
                "1102579807",
                "2650362143",
                "42",
                db=object(),
                permissions=["kick"],
            )

        self.assertEqual(len(config["sub_admins"]), 1)
        self.assertEqual(config["sub_admins"][0]["user_id"], "2650362143")
        self.assertEqual(config["sub_admins"][0]["permissions"], ["kick"])
        self.assertEqual(len(saved), 1)

    def test_legacy_qq_only_sub_admin_can_be_deleted_remotely(self):
        config = {
            "group_id": "1102579807",
            "features": {},
            "sub_admins": ["2650362143"],
        }
        saved: list[dict] = []
        with self._remote_patches(config, saved):
            delete_group_sub_admin(
                "1102579807", "2650362143", db=object(), updated_by=42
            )

        self.assertEqual([], config["sub_admins"])
        self.assertEqual(len(saved), 1)


if __name__ == "__main__":
    unittest.main()
