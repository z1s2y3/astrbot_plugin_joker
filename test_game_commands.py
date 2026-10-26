"""Tests for per-group SteamID-bound game command templates."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from web_client.feature_schema import FEATURE_BY_KEY, parse_field_value


_MODULE_PATH = Path(__file__).resolve().parent / "qqadmin" / "game_commands.py"
_SPEC = importlib.util.spec_from_file_location("joker_test_game_commands", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
match_game_command = _MODULE.match_game_command
normalize_game_commands = _MODULE.normalize_game_commands
resolve_game_command = _MODULE.resolve_game_command
resolve_game_commands = _MODULE.resolve_game_commands
split_game_commands = _MODULE.split_game_commands
get_default_game_commands = _MODULE.get_default_game_commands


class GameCommandTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {
                "id": "currency",
                "name": "设置金币",
                "trigger": "ChangeCurrencyBalance",
                "template": "#ChangeCurrencyBalance Normal 10000000",
                "permission": "member",
            },
            {
                "id": "zombie",
                "name": "生成僵尸",
                "trigger": "spawnzombie",
                "template": "#spawnzombie BP_Zombie_Civilian_Muscular_Male 1",
                "permission": "admin",
            },
        ]

    def test_templates_are_normalized_and_matched_case_insensitively(self):
        commands = normalize_game_commands(self.rows)
        self.assertEqual(2, len(commands))
        self.assertEqual("currency", match_game_command("#changecurrencybalance", commands)["id"])

    def test_default_templates_are_isolated_and_game_ready(self):
        first = get_default_game_commands()
        second = get_default_game_commands()
        self.assertEqual(
            ["currency", "spawnzombie", "addxiongb", "redxiongb"],
            [item["id"] for item in first],
        )
        self.assertEqual("member", first[0]["permission"])
        self.assertEqual("admin", first[1]["permission"])
        first[0]["template"] = "changed"
        self.assertNotEqual(first[0]["template"], second[0]["template"])

    def test_database_balance_operations_keep_operation_and_currency(self):
        commands = normalize_game_commands(
            [{
                "id": "add-coins",
                "name": "增加金币",
                "trigger": "addcoins",
                "template": "#addcoins 20",
                "operation": "database_credit",
                "currency_column": "coins",
            }]
        )
        self.assertEqual("database_credit", commands[0]["operation"])
        self.assertEqual("coins", commands[0]["currency_column"])

    def test_currency_implicitly_uses_sender_binding(self):
        command = resolve_game_command(
            self.rows[0]["template"],
            "76561199616499552",
            user_id="10001",
            group_id="20002",
        )
        self.assertEqual(
            "#ChangeCurrencyBalance Normal 10000000 76561199616499552",
            command,
        )

    def test_spawn_command_adds_location_binding(self):
        command = resolve_game_command(
            self.rows[1]["template"],
            "76561199881980718",
        )
        self.assertTrue(command.endswith("location 76561199881980718"))

    def test_reward_placeholder_uses_computed_signin_amount(self):
        command = resolve_game_command(
            "#ChangeCurrencyBalance Normal {reward} {steam_id}",
            "76561199616499552",
            reward=30,
        )
        self.assertEqual(
            "#ChangeCurrencyBalance Normal 30 76561199616499552",
            command,
        )

    def test_explicit_location_id_is_replaced_by_bound_id(self):
        command = resolve_game_command(
            "#spawnzombie BP_Zombie 1 location 76561190000000000",
            "76561199881980718",
        )
        self.assertTrue(command.endswith("location 76561199881980718"))

    def test_location_marker_gets_bound_id_in_the_middle(self):
        command = resolve_game_command(
            "#spawnzombie BP_Zombie 1 location",
            "76561199881980718",
        )
        self.assertEqual(
            "#spawnzombie BP_Zombie 1 location 76561199881980718",
            command,
        )

    def test_arbitrary_command_replaces_existing_player_id(self):
        command = resolve_game_command(
            "#ChangeGoldBalance 250 76561190000000000",
            "76561199881980718",
        )
        self.assertEqual(
            "#ChangeGoldBalance 250 76561199881980718",
            command,
        )

    def test_multiple_commands_use_comma_or_pipe_and_bind_each_id(self):
        value = "#ChangeCurrencyBalance Normal 10000000,#spawnzombie BP_Zombie 1 location"
        self.assertEqual(
            [
                "#ChangeCurrencyBalance Normal 10000000",
                "#spawnzombie BP_Zombie 1 location",
            ],
            split_game_commands(value),
        )
        commands = resolve_game_commands(value, "76561199881980718")
        self.assertEqual(
            [
                "#ChangeCurrencyBalance Normal 10000000 76561199881980718",
                "#spawnzombie BP_Zombie 1 location 76561199881980718",
            ],
            commands,
        )

    def test_plain_full_command_configuration_is_normalized(self):
        commands = normalize_game_commands(
            "#ChangeCurrencyBalance Normal 10000000|#ChangeGoldBalance 20"
        )
        self.assertEqual(1, len(commands))
        self.assertEqual("ChangeCurrencyBalance", commands[0]["trigger"])
        self.assertEqual(
            [
                "#ChangeCurrencyBalance Normal 10000000 76561199881980718",
                "#ChangeGoldBalance 20 76561199881980718",
            ],
            resolve_game_commands(commands[0]["template"], "76561199881980718"),
        )

    def test_json_full_command_can_derive_id_and_trigger(self):
        commands = normalize_game_commands(
            [{"template": "#ChangeCurrencyBalance Normal 10000000"}]
        )
        self.assertEqual(1, len(commands))
        self.assertEqual("ChangeCurrencyBalance", commands[0]["trigger"])
        self.assertEqual("auto-1", commands[0]["id"])

    def test_web_form_keeps_commas_inside_game_command_rows(self):
        field = next(
            item for item in FEATURE_BY_KEY["game_commands"].fields
            if item.key == "commands"
        )
        values = parse_field_value(
            field,
            '#ChangeCurrencyBalance Normal 10000000,#ChangeGoldBalance 20',
        )
        self.assertEqual(
            ['#ChangeCurrencyBalance Normal 10000000,#ChangeGoldBalance 20'],
            values,
        )

    def test_invalid_or_unsupported_templates_are_rejected(self):
        with self.assertRaises(ValueError):
            resolve_game_command("#ChangeCurrencyBalance Normal 1", "123")
        with self.assertRaises(ValueError):
            resolve_game_command("ChangeCurrencyBalance Normal 1", "76561199881980718")
        with self.assertRaises(ValueError):
            resolve_game_command("#Invalid\nCommand", "76561199881980718")


if __name__ == "__main__":
    unittest.main()
