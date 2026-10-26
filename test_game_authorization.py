import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auth import game_manager


class GameAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.auth_file = Path(self.temp_dir.name) / "game_auth.json"
        self.path_patch = patch.object(
            game_manager, "get_game_auth_path", return_value=self.auth_file
        )
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_generation_is_one_time_and_extension_keeps_card(self):
        now = 1_800_000_000
        with patch.object(game_manager.time, "time", return_value=now):
            issued = game_manager.issue_game_authorization(
                "766637236", 1, "server-secret", "2650362143"
            )
            repeated = game_manager.issue_game_authorization(
                "766637236", 30, "server-secret", "2650362143"
            )
            extended = game_manager.add_game_authorization_time("766637236", 2)

        self.assertEqual(issued["status"], "issued")
        self.assertTrue(game_manager.is_supported_game_license_key(issued["key"]))
        self.assertEqual(repeated["status"], "existing")
        self.assertEqual(repeated["key"], issued["key"])
        self.assertEqual(extended["status"], "extended")
        self.assertEqual(extended["key"], issued["key"])
        self.assertEqual(extended["expire"], now + 3 * 86400)

    def test_expired_record_can_be_extended_within_retention(self):
        issued_at = 1_800_000_000
        with patch.object(game_manager.time, "time", return_value=issued_at):
            issued = game_manager.issue_game_authorization(
                "766637236", 1, "server-secret"
            )
        expired_at = issued_at + 2 * 86400
        with patch.object(game_manager.time, "time", return_value=expired_at):
            info = game_manager.get_game_authorization_info("766637236")
            extended = game_manager.add_game_authorization_time("766637236", 1)
        self.assertEqual(info["state"], "expired")
        self.assertEqual(extended["status"], "extended")
        self.assertEqual(extended["key"], issued["key"])
        self.assertEqual(extended["expire"], expired_at + 86400)

    def test_expired_record_is_removed_after_seven_days(self):
        issued_at = 1_800_000_000
        with patch.object(game_manager.time, "time", return_value=issued_at):
            game_manager.issue_game_authorization("766637236", 1, "server-secret")
        cleanup_at = issued_at + 8 * 86400
        with patch.object(game_manager.time, "time", return_value=cleanup_at):
            removed = game_manager.cleanup_expired_game_authorizations(now=cleanup_at)
            new_issue = game_manager.issue_game_authorization(
                "766637236", 1, "server-secret"
            )
        self.assertEqual(removed, 1)
        self.assertEqual(new_issue["status"], "issued")

    def test_malformed_numeric_values_are_read_as_missing_safely(self):
        game_manager._atomic_write_json(
            self.auth_file,
            {
                "version": 1,
                "groups": {
                    "766637236": {
                        "key": "JG1-not-a-card",
                        "expire": "broken",
                        "activated_at": {},
                        "last_extended_at": "broken",
                    }
                },
            },
        )
        info, healthy = game_manager.load_game_auth_data_with_health()
        self.assertTrue(healthy)
        self.assertEqual(info["groups"]["766637236"]["state"], "missing")
        self.assertEqual(game_manager.game_authorization_state("766637236"), "missing")


if __name__ == "__main__":
    unittest.main()
