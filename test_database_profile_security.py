"""Security regression tests for Windows database profile projections."""

from __future__ import annotations

import json
import unittest

from web_client.feature_schema import FEATURE_BY_KEY, parse_field_value
from web_client.sync_projection import project_group_config


class DatabaseProfileSecurityTests(unittest.TestCase):
    @staticmethod
    def _field():
        return next(
            field
            for field in FEATURE_BY_KEY["stats"].fields
            if field.key == "database_profiles"
        )

    def test_profile_projection_drops_credentials_and_unknown_fields(self):
        raw = json.dumps(
            {
                "id": "main",
                "name": "主服",
                "server_id": "8888",
                "enabled": True,
                "database_path": "data/scum_databases/1102579807/main.db",
                "password": "must-not-sync",
                "password_ciphertext": "must-not-sync",
                "dsn": "mysql://must-not-sync",
                "ftp": {"host": "must-not-sync"},
            },
            ensure_ascii=False,
        )
        values = parse_field_value(self._field(), raw)
        self.assertEqual(1, len(values))
        projected = json.loads(values[0])
        self.assertEqual(
            {"id", "name", "server_id", "enabled", "path"},
            set(projected),
        )
        self.assertNotIn("must-not-sync", values[0])

    def test_profile_rejects_absolute_or_parent_database_paths(self):
        for path in ("C:/secret.db", "/etc/secret.db", "data/../secret.db"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                parse_field_value(
                    self._field(),
                    json.dumps({"id": "main", "path": path}),
                )

    def test_legacy_projection_also_strips_nested_secrets(self):
        projected = project_group_config(
            {
                "group_id": "1102579807",
                "features": {
                    "stats": {
                        "database_profile": "main",
                        "database_profiles": [
                            {
                                "id": "main",
                                "name": "主服",
                                "server_id": "8888",
                                "path": "data/main.db",
                                "ftp": {"password": "must-not-sync"},
                                "dsn": "mysql://must-not-sync",
                            },
                            {
                                "id": "bad",
                                "path": "C:/private.db",
                                "password": "must-not-sync",
                            },
                        ],
                    }
                },
            }
        )
        stats = projected["features"]["stats"]
        self.assertEqual("main", stats["database_profile"])
        self.assertEqual(1, len(stats["database_profiles"]))
        self.assertEqual(
            {"id", "name", "server_id", "enabled", "path"},
            set(stats["database_profiles"][0]),
        )
        self.assertNotIn("must-not-sync", repr(stats))


if __name__ == "__main__":
    unittest.main()
