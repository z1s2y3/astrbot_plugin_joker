"""Regression tests for the mutable SCUM player database adapter."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest


_MODULE_PATH = Path(__file__).parent / "scum" / "player_database.py"
_SPEC = importlib.util.spec_from_file_location("_joker_player_database", _MODULE_PATH)
assert _SPEC and _SPEC.loader
player_database = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(player_database)


class PlayerDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "players.db"
        self.connection = sqlite3.connect(self.path)
        self.connection.executescript(
            """
            CREATE TABLE user_list (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name TEXT,
                nick_name TEXT,
                amount VARCHAR(40) DEFAULT '0',
                integral VARCHAR(40) DEFAULT '0',
                steam_id VARCHAR(40)
            );
            CREATE TABLE sign_log (
                sign_id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_name TEXT,
                player_steamid TEXT,
                sign_date TEXT,
                sign_time TEXT
            );
            CREATE TABLE user_amount_change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                steamid TEXT NOT NULL,
                change_type TEXT NOT NULL,
                change_action TEXT NOT NULL,
                before_value INTEGER NOT NULL,
                change_value INTEGER NOT NULL,
                after_value INTEGER NOT NULL,
                method_name TEXT NOT NULL
            );
            INSERT INTO user_list(user_name, amount, integral, steam_id)
                VALUES ('Alice', '10', '3', '76561198000000000');
            INSERT INTO user_list(user_name, amount, integral, steam_id)
                VALUES ('Bob', '0', '9', '76561198000000001');
            """
        )
        self.connection.commit()
        self.connection.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_strict_ascii_steam_id(self):
        self.assertEqual(
            player_database.normalize_steam_id("76561198000000000"),
            "76561198000000000",
        )
        self.assertEqual(player_database.normalize_steam_id("１２３４５６７８９０１２３４５６７"), "")
        self.assertEqual(player_database.normalize_steam_id("7656119800000000x"), "")

    def test_credit_and_debit_update_balance_and_audit(self):
        credited = player_database.credit_player(
            self.path,
            "76561198000000000",
            5,
            method_name="testCredit",
        )
        self.assertTrue(credited["success"])
        self.assertEqual((credited["before"], credited["after"]), (10, 15))
        self.assertTrue(credited["audit_logged"])

        debited = player_database.debit_player(
            self.path,
            "76561198000000000",
            3,
            method_name="testDebit",
        )
        self.assertEqual((debited["before"], debited["after"]), (15, 12))
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT amount FROM user_list WHERE steam_id = ?",
                    ("76561198000000000",),
                ).fetchone()[0],
                "12",
            )
        finally:
            connection.close()

    def test_sign_in_is_database_idempotent_and_logs_reward(self):
        first = player_database.apply_signin_reward(
            self.path,
            "76561198000000000",
            7,
            player_name="Alice",
            sign_date="2099-01-02",
            sign_time="08:09:10",
        )
        self.assertTrue(first["success"])
        self.assertEqual(first["code"], "signed")
        self.assertEqual(first["after"], 17)
        duplicate = player_database.apply_signin_reward(
            self.path,
            "76561198000000000",
            7,
            sign_date="2099-01-02",
        )
        self.assertEqual(duplicate["code"], "already_signed")
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sign_log WHERE player_steamid = ? AND sign_date = ?",
                    ("76561198000000000", "2099-01-02"),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT amount FROM user_list WHERE steam_id = ?",
                    ("76561198000000000",),
                ).fetchone()[0],
                "17",
            )
        finally:
            connection.close()

    def test_custom_currency_column_is_whitelisted_and_missing_player_fails(self):
        result = player_database.credit_player(
            self.path,
            "76561198000000001",
            4,
            currency_column="integral",
        )
        self.assertEqual(result["currency"], "integral")
        self.assertEqual(result["after"], 13)
        self.assertEqual(
            player_database.credit_player(
                self.path,
                "76561198000000099",
                1,
            )["code"],
            "not_found",
        )

    def test_duplicate_database_rows_are_rejected_without_write(self):
        connection = sqlite3.connect(self.path)
        connection.execute(
            "INSERT INTO user_list(user_name, amount, steam_id) VALUES (?, ?, ?)",
            ("Duplicate", "99", "76561198000000000"),
        )
        connection.commit()
        connection.close()
        result = player_database.credit_player(self.path, "76561198000000000", 1)
        self.assertEqual(result["code"], "ambiguous")

    def test_sign_in_requires_sign_log_and_does_not_change_balance(self):
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TABLE sign_log")
        connection.commit()
        connection.close()
        result = player_database.apply_signin_reward(
            self.path,
            "76561198000000000",
            100,
            sign_date="2099-02-01",
        )
        self.assertEqual(result["code"], "schema_error")
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT amount FROM user_list WHERE steam_id = ?",
                    ("76561198000000000",),
                ).fetchone()[0],
                "10",
            )
        finally:
            connection.close()

    def test_reset_signin_reverses_reward_and_writes_reverse_audit(self):
        signed = player_database.apply_signin_reward(
            self.path,
            "76561198000000000",
            7,
            player_name="Alice",
            sign_date="2099-03-04",
            sign_time="08:00:00",
        )
        self.assertEqual(signed["code"], "signed")
        reset = player_database.reset_signin_record(
            self.path,
            "76561198000000000",
            "2099-03-04",
        )
        self.assertEqual(reset["code"], "reset")
        self.assertEqual((reset["before"], reset["after"], reset["reward"]), (17, 10, 7))
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sign_log WHERE player_steamid = ? AND sign_date = ?",
                    ("76561198000000000", "2099-03-04"),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT amount FROM user_list WHERE steam_id = ?",
                    ("76561198000000000",),
                ).fetchone()[0],
                "10",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM user_amount_change_log WHERE method_name = ?",
                    ("reset_signin_record",),
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_reset_signin_fails_closed_when_balance_changed(self):
        player_database.apply_signin_reward(
            self.path,
            "76561198000000000",
            7,
            sign_date="2099-03-05",
        )
        player_database.credit_player(self.path, "76561198000000000", 1)
        reset = player_database.reset_signin_record(
            self.path,
            "76561198000000000",
            "2099-03-05",
        )
        self.assertEqual(reset["code"], "balance_changed")
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sign_log WHERE player_steamid = ? AND sign_date = ?",
                    ("76561198000000000", "2099-03-05"),
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_reset_signin_can_remove_record_without_touching_balance(self):
        player_database.apply_signin_reward(
            self.path,
            "76561198000000000",
            7,
            sign_date="2099-03-06",
        )
        reset = player_database.reset_signin_record(
            self.path,
            "76561198000000000",
            "2099-03-06",
            remove_reward=False,
        )
        self.assertEqual(reset["code"], "reset_record_only")
        self.assertEqual(reset["reward"], 0)
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT amount FROM user_list WHERE steam_id = ?",
                    ("76561198000000000",),
                ).fetchone()[0],
                "17",
            )
        finally:
            connection.close()

    def test_reset_signin_can_remove_record_without_reward_audit(self):
        connection = sqlite3.connect(self.path)
        connection.execute(
            "INSERT INTO sign_log(player_name, player_steamid, sign_date, sign_time) VALUES (?, ?, ?, ?)",
            ("Alice", "76561198000000000", "2099-03-08", "08:00:00"),
        )
        connection.commit()
        connection.close()

        reset = player_database.reset_signin_record(
            self.path,
            "76561198000000000",
            "2099-03-08",
            remove_reward=False,
        )
        self.assertEqual(reset["code"], "reset_record_only")
        self.assertEqual(reset["reward"], 0)
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sign_log WHERE player_steamid = ? AND sign_date = ?",
                    ("76561198000000000", "2099-03-08"),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT amount FROM user_list WHERE steam_id = ?",
                    ("76561198000000000",),
                ).fetchone()[0],
                "10",
            )
        finally:
            connection.close()

    def test_reset_signin_supports_without_rowid_sign_log(self):
        # A number of server snapshots use a composite primary key and
        # WITHOUT ROWID for the sign-in table.  The reset path must use the
        # declared key rather than relying on SQLite's implicit rowid.
        path = self.path.parent / "without-rowid.db"
        source = sqlite3.connect(self.path)
        connection = sqlite3.connect(path)
        source.backup(connection)
        source.close()
        connection.executescript(
            """
            DROP TABLE sign_log;
            CREATE TABLE sign_log (
                player_steamid TEXT NOT NULL,
                sign_date TEXT NOT NULL,
                player_name TEXT,
                sign_time TEXT,
                PRIMARY KEY (player_steamid, sign_date)
            ) WITHOUT ROWID;
            """
        )
        connection.commit()
        connection.close()

        signed = player_database.apply_signin_reward(
            path,
            "76561198000000000",
            7,
            sign_date="2099-03-07",
        )
        self.assertEqual(signed["code"], "signed")
        reset = player_database.reset_signin_record(
            path,
            "76561198000000000",
            "2099-03-07",
        )
        self.assertEqual(reset["code"], "reset")
        connection = sqlite3.connect(path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sign_log WHERE player_steamid = ?",
                    ("76561198000000000",),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT amount FROM user_list WHERE steam_id = ?",
                    ("76561198000000000",),
                ).fetchone()[0],
                "10",
            )
        finally:
            connection.close()

    def test_malformed_balance_is_not_overwritten(self):
        connection = sqlite3.connect(self.path)
        connection.execute(
            "UPDATE user_list SET amount = ? WHERE steam_id = ?",
            ("not-a-number", "76561198000000000"),
        )
        connection.commit()
        connection.close()
        result = player_database.credit_player(self.path, "76561198000000000", 1)
        self.assertEqual(result["code"], "schema_error")
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT amount FROM user_list WHERE steam_id = ?",
                    ("76561198000000000",),
                ).fetchone()[0],
                "not-a-number",
            )
        finally:
            connection.close()

    def test_fractional_reward_is_rejected_without_writing(self):
        result = player_database.credit_player(
            self.path,
            "76561198000000000",
            1.5,
        )
        self.assertEqual(result["code"], "invalid_amount")
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT amount FROM user_list WHERE steam_id = ?",
                    ("76561198000000000",),
                ).fetchone()[0],
                "10",
            )
        finally:
            connection.close()

    def test_fractional_balance_is_not_overwritten(self):
        connection = sqlite3.connect(self.path)
        connection.execute(
            "UPDATE user_list SET amount = ? WHERE steam_id = ?",
            (1.5, "76561198000000000"),
        )
        connection.commit()
        connection.close()
        result = player_database.credit_player(
            self.path,
            "76561198000000000",
            1,
        )
        self.assertEqual(result["code"], "schema_error")
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT amount FROM user_list WHERE steam_id = ?",
                    ("76561198000000000",),
                ).fetchone()[0],
                "1.5",
            )
        finally:
            connection.close()

    def test_read_player_status_uses_database_sign_log_for_calendar_stats(self):
        connection = sqlite3.connect(self.path)
        connection.executemany(
            "INSERT INTO sign_log(player_name, player_steamid, sign_date, sign_time) VALUES (?, ?, ?, ?)",
            [
                ("Alice", "76561198000000000", "2098-12-31", "08:00:00"),
                ("Alice", "76561198000000000", "2099-01-01", "08:00:00"),
                ("Alice", "76561198000000000", "2099-01-02", "08:00:00"),
            ],
        )
        connection.commit()
        connection.close()

        result = player_database.read_player_status(
            self.path,
            "76561198000000000",
            today="2099-01-02",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["code"], "found")
        self.assertEqual(result["balance"], 10)
        self.assertTrue(result["signed_today"])
        self.assertEqual(result["total_signins"], 3)
        self.assertEqual(result["month_signins"], 2)
        self.assertEqual(result["last_signin"], "2099-01-02")
        self.assertEqual(result["consecutive_signins"], 3)

    def test_read_player_status_without_sign_log_still_returns_balance(self):
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TABLE sign_log")
        connection.commit()
        connection.close()

        result = player_database.read_player_status(
            self.path,
            "76561198000000000",
            today="2099-01-02",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["balance"], 10)
        self.assertFalse(result["sign_log_available"])
        self.assertFalse(result["signed_today"])
        self.assertEqual(result["total_signins"], 0)

    def test_signin_reward_tier_uses_database_streak(self):
        connection = sqlite3.connect(self.path)
        connection.executemany(
            "INSERT INTO sign_log(player_name, player_steamid, sign_date, sign_time) VALUES (?, ?, ?, ?)",
            [
                ("Alice", "76561198000000000", "2099-01-01", "08:00:00"),
                ("Alice", "76561198000000000", "2099-01-02", "08:00:00"),
            ],
        )
        connection.commit()
        connection.close()

        result = player_database.apply_signin_reward(
            self.path,
            "76561198000000000",
            10,
            player_name="Alice",
            sign_date="2099-01-03",
            reward_config={"base_reward": 10, "reward_3_days": 55},
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["reward_streak"], 3)
        self.assertEqual(result["change"], 55)
        self.assertEqual(result["after"], 65)


if __name__ == "__main__":
    unittest.main()
