import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


def _load_database_module():
    module_path = Path(__file__).parent / "scum" / "database.py"
    spec = importlib.util.spec_from_file_location("_joker_scum_database", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


database = _load_database_module()


class ScumDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "beijixiong.db"
        connection = sqlite3.connect(self.path)
        connection.execute(
            "CREATE TABLE user_list (user_id INTEGER PRIMARY KEY, user_name TEXT, nick_name TEXT, steam_id TEXT)"
        )
        connection.execute(
            "INSERT INTO user_list(user_id, user_name, nick_name, steam_id) VALUES (?, ?, ?, ?)",
            (1, "Alice", "A", "76561198000000000"),
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_lookup_returns_player_for_exact_seventeen_digit_id(self):
        result = database.lookup_user_list_steam_id(self.path, " 76561198000000000 ")
        self.assertTrue(result["success"])
        self.assertEqual(result["code"], "found")
        self.assertEqual(result["player"]["user_name"], "Alice")

    def test_lookup_rejects_missing_or_malformed_id(self):
        self.assertEqual(
            database.lookup_user_list_steam_id(self.path, "76561198000000001")["code"],
            "not_found",
        )
        self.assertEqual(
            database.lookup_user_list_steam_id(self.path, "123")["code"],
            "invalid_steam_id",
        )

    def test_lookup_rejects_duplicate_or_malformed_schema(self):
        connection = sqlite3.connect(self.path)
        connection.execute(
            "INSERT INTO user_list(user_id, user_name, nick_name, steam_id) VALUES (?, ?, ?, ?)",
            (2, "Bob", "B", "76561198000000000"),
        )
        connection.commit()
        connection.close()
        self.assertEqual(
            database.lookup_user_list_steam_id(self.path, "76561198000000000")["code"],
            "ambiguous",
        )

        malformed = Path(self.tempdir.name) / "malformed.db"
        connection = sqlite3.connect(malformed)
        connection.execute("CREATE TABLE user_list (user_id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()
        self.assertEqual(
            database.lookup_user_list_steam_id(malformed, "76561198000000000")["code"],
            "schema_error",
        )

    def test_lookup_accepts_minimal_user_list_with_only_steam_id(self):
        """Only user_list.steam_id is required by the binding contract."""
        minimal = Path(self.tempdir.name) / "minimal.db"
        connection = sqlite3.connect(minimal)
        connection.execute("CREATE TABLE user_list (steam_id TEXT)")
        connection.execute(
            "INSERT INTO user_list(steam_id) VALUES (?)",
            ("76561198000000000",),
        )
        connection.commit()
        connection.close()

        result = database.lookup_user_list_steam_id(minimal, "76561198000000000")
        self.assertTrue(result["success"])
        self.assertEqual(result["code"], "found")
        self.assertEqual(result["player"]["steam_id"], "76561198000000000")
        self.assertEqual(result["player"]["user_id"], "")

    def test_lookup_supports_sqlite_row_factory(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE user_list (user_id INTEGER, user_name TEXT, nick_name TEXT, steam_id TEXT)"
        )
        connection.execute(
            "INSERT INTO user_list VALUES (?, ?, ?, ?)",
            (7, "Row User", "R", "76561198000000000"),
        )
        result = database.lookup_user_list_steam_id(connection, "76561198000000000")
        self.assertTrue(result["success"])
        self.assertEqual(result["player"]["user_name"], "Row User")
        connection.close()

    def test_relative_database_path_cannot_escape_plugin_root(self):
        root = Path(self.tempdir.name) / "plugin"
        root.mkdir()
        outside = Path(self.tempdir.name) / "outside.db"
        outside.touch()
        self.assertIsNone(
            database.resolve_group_database_path(
                {"features": {"stats": {"sqlite_path": "../outside.db"}}},
                plugin_root=root,
            )
        )

    def test_resolve_group_path_prefers_group_setting_and_falls_back(self):
        custom = Path(self.tempdir.name) / "custom.db"
        custom.touch()
        root = Path(self.tempdir.name)
        resolved = database.resolve_group_database_path(
            {"features": {"stats": {"sqlite_path": str(custom)}}},
            plugin_root=root,
        )
        self.assertEqual(resolved, custom.resolve())
        fallback = database.resolve_group_database_path({}, plugin_root=root)
        self.assertIsNone(fallback)

    def test_default_resolver_rejects_unconfigured_local_database(self):
        resolved = database.resolve_group_database_path(
            {}, plugin_root=Path(__file__).parent
        )
        self.assertIsNone(resolved)

    def test_resolver_supports_selected_group_database_profile(self):
        selected = Path(self.tempdir.name) / "selected.db"
        selected.touch()
        other = Path(self.tempdir.name) / "other.db"
        other.touch()
        resolved = database.resolve_group_database_path(
            {
                "features": {
                    "stats": {
                        "database_profile": "selected",
                        "database_profiles": [
                            {"id": "other", "path": str(other)},
                            {"id": "selected", "path": str(selected)},
                        ],
                    }
                }
            },
            plugin_root=self.tempdir.name,
        )
        self.assertEqual(resolved, selected.resolve())

    def test_active_game_server_selects_its_linked_database_profile(self):
        alpha = Path(self.tempdir.name) / "alpha.db"
        alpha.touch()
        beta = Path(self.tempdir.name) / "beta.db"
        beta.touch()
        resolved = database.resolve_group_database_path(
            {
                "features": {
                    "game_servers": {
                        "active_profile_id": "game-b",
                        "profiles": [
                            {
                                "id": "game-a",
                                "enabled": True,
                                "integration_profile_id": "alpha",
                            },
                            {
                                "id": "game-b",
                                "enabled": True,
                                "integration_profile_id": "beta",
                            },
                        ],
                    },
                    "stats": {
                        "database_profile": "alpha",
                        "database_profiles": [
                            {"id": "alpha", "path": str(alpha)},
                            {"id": "beta", "path": str(beta)},
                        ],
                    },
                }
            },
            plugin_root=self.tempdir.name,
        )
        self.assertEqual(resolved, beta.resolve())

    def test_resolver_accepts_legacy_json_string_profile_rows(self):
        selected = Path(self.tempdir.name) / "selected-legacy.db"
        selected.touch()
        resolved = database.resolve_group_database_path(
            {
                "features": {
                    "stats": {
                        "database_profile": "selected",
                        "database_profiles": [
                            json.dumps({"id": "selected", "path": str(selected)}),
                        ],
                    }
                }
            },
            plugin_root=self.tempdir.name,
        )
        self.assertEqual(resolved, selected.resolve())

    def test_resolver_rejects_unknown_explicit_profile(self):
        resolved = database.resolve_group_database_path(
            {
                "features": {
                    "stats": {
                        "database_profile": "missing",
                        "database_profiles": [{"id": "main", "path": "main.db"}],
                    }
                }
            },
            plugin_root=self.tempdir.name,
        )
        self.assertIsNone(resolved)

    def test_resolver_does_not_fall_back_when_declared_profile_has_no_path(self):
        resolved = database.resolve_group_database_path(
            {
                "features": {
                    "scum": {
                        "server_profiles": [
                            {"id": "main", "server_id": "8888", "enabled": True}
                        ]
                    }
                }
            },
            plugin_root=self.tempdir.name,
        )
        self.assertIsNone(resolved)

    def test_resolver_supports_web_server_profiles_alias(self):
        selected = Path(self.tempdir.name) / "web-selected.db"
        selected.touch()
        resolved = database.resolve_group_database_path(
            {
                "features": {
                    "scum": {
                        "integration_managed": True,
                        "integration_profile": "selected",
                        "server_profiles": [
                            {"id": "other", "path": "other.db", "enabled": True},
                            {"id": "selected", "path": str(selected), "enabled": True},
                        ],
                    }
                }
            },
            plugin_root=self.tempdir.name,
        )
        self.assertEqual(resolved, selected.resolve())

    def test_resolver_fails_closed_for_disabled_web_server_profiles(self):
        fallback = Path(self.tempdir.name) / "fallback.db"
        fallback.touch()
        resolved = database.resolve_group_database_path(
            {
                "features": {
                    "scum": {
                        "integration_managed": True,
                        "server_profiles": [
                            {"id": "main", "path": str(fallback), "enabled": False}
                        ],
                    }
                }
            },
            plugin_root=self.tempdir.name,
        )
        self.assertIsNone(resolved)

    def test_resolver_does_not_fall_back_when_all_profiles_are_disabled(self):
        bundled = Path(self.tempdir.name) / "bundled.db"
        bundled.touch()
        resolved = database.resolve_group_database_path(
            {
                "features": {
                    "stats": {
                        "database_profiles": [
                            {
                                "id": "main",
                                "path": str(bundled),
                                "enabled": False,
                            }
                        ]
                    },
                    "scum": {"integration_managed": True},
                }
            },
            plugin_root=self.tempdir.name,
        )
        self.assertIsNone(resolved)

    def test_resolver_fails_closed_for_managed_group_without_snapshot(self):
        resolved = database.resolve_group_database_path(
            {
                "features": {
                    "scum": {"integration_managed": "true"},
                }
            },
            plugin_root=self.tempdir.name,
        )
        self.assertIsNone(resolved)


if __name__ == "__main__":
    unittest.main()
