"""Executable regression test for the SteamID admission database lookup.

This intentionally uses the production ``lookup_user_list_steam_id`` helper
and a temporary SQLite file matching the SCUM ``user_list`` layout.  It can be
run without pytest and also proves that read-only verification leaves the game
database untouched.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
from pathlib import Path


_DATABASE_MODULE_PATH = Path(__file__).resolve().parent / "scum" / "database.py"
_DATABASE_SPEC = importlib.util.spec_from_file_location(
    "joker_steamid_database_test_module", _DATABASE_MODULE_PATH
)
assert _DATABASE_SPEC is not None and _DATABASE_SPEC.loader is not None
_DATABASE_MODULE = importlib.util.module_from_spec(_DATABASE_SPEC)
_DATABASE_SPEC.loader.exec_module(_DATABASE_MODULE)
lookup_user_list_steam_id = _DATABASE_MODULE.lookup_user_list_steam_id

_PLAYER_DATABASE_MODULE_PATH = Path(__file__).resolve().parent / "scum" / "player_database.py"
_PLAYER_DATABASE_SPEC = importlib.util.spec_from_file_location(
    "joker_signin_database_test_module", _PLAYER_DATABASE_MODULE_PATH
)
assert _PLAYER_DATABASE_SPEC is not None and _PLAYER_DATABASE_SPEC.loader is not None
_PLAYER_DATABASE_MODULE = importlib.util.module_from_spec(_PLAYER_DATABASE_SPEC)
_PLAYER_DATABASE_SPEC.loader.exec_module(_PLAYER_DATABASE_MODULE)
apply_signin_reward = _PLAYER_DATABASE_MODULE.apply_signin_reward


STEAM_ID = "76561198012345678"


def main() -> None:
    descriptor, raw_path = tempfile.mkstemp(prefix="joker-steamid-test-", suffix=".db")
    os.close(descriptor)
    path = Path(raw_path)
    try:
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE user_list (user_id INTEGER, user_name TEXT, nick_name TEXT, steam_id TEXT, amount INTEGER)"
            )
            connection.execute(
                "CREATE TABLE sign_log (player_name TEXT, player_steamid TEXT, sign_date TEXT, sign_time TEXT)"
            )
            connection.execute(
                "CREATE TABLE user_amount_change_log (created_at TEXT, steamid TEXT, change_type TEXT, change_action TEXT, before_value INTEGER, change_value INTEGER, after_value INTEGER, method_name TEXT)"
            )
            connection.execute(
                "INSERT INTO user_list (user_id, user_name, nick_name, steam_id, amount) VALUES (?, ?, ?, ?, ?)",
                (42, "测试玩家", "北极熊", STEAM_ID, 10),
            )
            connection.commit()
        finally:
            connection.close()

        found = lookup_user_list_steam_id(path, STEAM_ID)
        assert found["success"] is True, found
        assert found["code"] == "found", found
        assert found["player"]["user_name"] == "测试玩家", found
        assert found["player"]["steam_id"] == STEAM_ID, found

        missing = lookup_user_list_steam_id(path, "76561198000000000")
        assert missing["success"] is False and missing["code"] == "not_found", missing

        invalid = lookup_user_list_steam_id(path, "not-a-steam-id")
        assert invalid["success"] is False and invalid["code"] == "invalid_steam_id", invalid

        signed = apply_signin_reward(
            path, STEAM_ID, 5, player_name="测试玩家", sign_date="2026-09-30", currency_column="amount"
        )
        assert signed["success"] is True and signed["code"] == "signed", signed
        assert signed["before"] == 10 and signed["after"] == 15, signed
        duplicate = apply_signin_reward(
            path, STEAM_ID, 5, player_name="测试玩家", sign_date="2026-09-30", currency_column="amount"
        )
        assert duplicate["success"] is False and duplicate["code"] == "already_signed", duplicate

        connection = sqlite3.connect(path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM user_list").fetchone()[0]
            name, balance = connection.execute("SELECT user_name, amount FROM user_list WHERE steam_id = ?", (STEAM_ID,)).fetchone()
            sign_count = connection.execute("SELECT COUNT(*) FROM sign_log WHERE player_steamid = ?", (STEAM_ID,)).fetchone()[0]
        finally:
            connection.close()
        assert count == 1 and name == "测试玩家" and balance == 15 and sign_count == 1, (count, name, balance, sign_count)
        print("PASS: SteamID found=测试玩家; missing=not_found; sign-in committed=10->15; duplicate protected")
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
