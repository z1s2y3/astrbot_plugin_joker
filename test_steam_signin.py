import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


def _load_steam_module():
    root_name = "_steam_signin_test_root"
    root_package = ModuleType(root_name)
    root_package.__path__ = [str(Path(__file__).parent)]
    sys.modules[root_name] = root_package

    feature_catalog = ModuleType(f"{root_name}.feature_catalog")
    def split_command_aliases(value, default):
        fallback = str(default or "").strip().lstrip("/")
        raw = str(value or "").strip() or fallback
        tokens = []
        for item in raw.replace(",", "|").split("|"):
            token = item.strip().lstrip("/")
            if token and not any(char.isspace() for char in token) and token not in tokens:
                tokens.append(token)
        return tokens or [fallback]

    feature_catalog.split_command_aliases = split_command_aliases
    sys.modules[feature_catalog.__name__] = feature_catalog

    package_name = f"{root_name}.qqadmin"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).parent / "qqadmin")]
    sys.modules[package_name] = package

    group_config = ModuleType(f"{package_name}.group_config")
    group_config.get_group_data = lambda *_args, **_kwargs: {}
    group_config.update_group_data = lambda *_args, **_kwargs: None
    sys.modules[group_config.__name__] = group_config

    module_path = Path(__file__).parent / "qqadmin" / "steam_signin.py"
    spec = importlib.util.spec_from_file_location(f"{package_name}.steam_signin", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


steam_signin = _load_steam_module()


class SteamSigninTests(unittest.TestCase):
    def setUp(self):
        self.data = {}
        self.get_patch = patch(
            f"{steam_signin.__name__}.get_group_data",
            side_effect=lambda _group, key, default: self.data.get(key, default),
        )
        self.update_patch = patch(
            f"{steam_signin.__name__}.update_group_data",
            side_effect=self._update,
        )
        self.get_patch.start()
        self.update_patch.start()

    def tearDown(self):
        self.update_patch.stop()
        self.get_patch.stop()

    def _update(self, _group, key, callback):
        self.data[key] = callback(self.data.get(key))
        return self.data[key]

    def test_requires_exactly_seventeen_digits(self):
        self.assertFalse(steam_signin.normalize_steam_id("123"))
        self.assertFalse(steam_signin.normalize_steam_id("1234567890123456x"))
        self.assertEqual(
            steam_signin.normalize_steam_id("76561198000000000"),
            "76561198000000000",
        )

    def test_binding_is_one_to_one_and_owner_can_remove_it(self):
        group_id = "1102579807"
        steam_id = "76561198000000000"
        first = steam_signin.bind_steam_id(group_id, "10001", steam_id, "Alice")
        self.assertTrue(first["success"])
        self.assertEqual(
            steam_signin.bind_steam_id(group_id, "10002", steam_id)["code"],
            "steam_in_use",
        )
        self.assertEqual(
            steam_signin.bind_steam_id(group_id, "10001", "76561198000000001")["code"],
            "user_already_bound",
        )
        self.assertEqual(
            steam_signin.get_steam_binding(group_id, user_id="10001")["steam_id"],
            steam_id,
        )
        self.assertTrue(steam_signin.unbind_steam_id(group_id, "10001")["success"])
        self.assertIsNone(steam_signin.get_steam_binding(group_id, user_id="10001"))

    def test_malformed_explicit_lookup_does_not_return_first_binding(self):
        group_id = "1102579807"
        self.assertTrue(
            steam_signin.bind_steam_id(
                group_id, "10001", "76561198000000000"
            )["success"]
        )
        self.assertIsNone(
            steam_signin.get_steam_binding(group_id, steam_id="not-a-steamid")
        )

    def test_write_failure_is_reported_without_raising(self):
        group_id = "1102579807"
        with patch(
            f"{steam_signin.__name__}.update_group_data",
            side_effect=OSError("disk full"),
        ):
            result = steam_signin.bind_steam_id(
                group_id, "10001", "76561198000000000"
            )
        self.assertEqual(result["code"], "write_failed")

    def test_database_validator_runs_before_local_write(self):
        group_id = "1102579807"
        steam_id = "76561198000000000"
        rejected = steam_signin.bind_steam_id(
            group_id,
            "10001",
            steam_id,
            database_validator=lambda _steam: {
                "success": False,
                "code": "not_found",
                "message": "该 SteamID 不在当前服务器数据库中，无法绑定。",
            },
        )
        self.assertEqual(rejected["code"], "not_found")
        self.assertEqual(self.data.get("steam_signin", {}).get("bindings", {}), {})

        accepted = steam_signin.bind_steam_id(
            group_id,
            "10001",
            steam_id,
            database_validator=lambda _steam: {"success": True, "code": "found"},
        )
        self.assertTrue(accepted["success"])

    def test_local_duplicate_wins_when_database_is_unavailable(self):
        group_id = "1102579807"
        steam_id = "76561198000000000"
        self.assertTrue(
            steam_signin.bind_steam_id(group_id, "10001", steam_id)["success"]
        )
        validator = unittest.mock.Mock(
            return_value={
                "success": False,
                "code": "db_unavailable",
                "message": "数据库不可用",
            }
        )
        result = steam_signin.bind_steam_id(
            group_id, "10002", steam_id, database_validator=validator
        )
        self.assertEqual("steam_in_use", result["code"])
        validator.assert_not_called()

    def test_runtime_signin_counts_remain_local(self):
        group_id = "1102579807"
        steam_id = "76561198000000000"
        self.assertTrue(steam_signin.bind_steam_id(group_id, "10001", steam_id)["success"])
        record = steam_signin.record_steam_signin(group_id, "10001", "Alice")
        self.assertEqual(record["signin_count"], 1)
        self.assertEqual(record["monthly_count"], 1)
        self.assertEqual(record["weekly_count"], 1)
        self.assertEqual(len(steam_signin.list_steam_bindings(group_id)), 1)

    def test_runtime_signin_write_failure_is_explicit(self):
        with patch(
            f"{steam_signin.__name__}.update_group_data",
            side_effect=OSError("disk full"),
        ):
            result = steam_signin.record_steam_signin(
                "1102579807", "10001", "Alice"
            )
        self.assertFalse(result["success"])
        self.assertEqual("write_failed", result["code"])

    def test_multiple_profiles_keep_independent_commands_and_modes(self):
        policy = steam_signin.signin_policy(
            {
                "signin_mode": "basic",
                "signin_profiles": [
                    {
                        "id": "daily",
                        "name": "每日签到",
                        "command": "每日签到",
                        "mode": "basic",
                        "base_reward": 20,
                    },
                    {
                        "id": "steam-weekly",
                        "name": "Steam周签",
                        "command": "周签到",
                        "mode": "steam",
                        "schedule": "weekly",
                        "steam_reward_mode": "websocket",
                        "steam_reward_command": "#ChangeCurrencyBalance Normal {reward} {steam_id}",
                    },
                ],
            }
        )

        daily = steam_signin.select_signin_profile(policy, "每日签到")
        weekly = steam_signin.select_signin_profile(policy, "周签到")
        self.assertEqual(daily["profile_id"], "daily")
        self.assertEqual(daily["profile_name"], "每日签到")
        self.assertEqual(daily["signin_command"], "每日签到")
        self.assertEqual(daily["base_reward"], 20)
        self.assertEqual(weekly["signin_command"], "周签到")
        self.assertEqual(weekly["signin_mode"], "steam")
        self.assertEqual(weekly["schedule_mode"], "weekly")
        self.assertEqual(weekly["steam_reward_mode"], "websocket")
        self.assertIn("{reward}", weekly["steam_reward_command"])

    def test_steam_reward_policy_defaults_to_database(self):
        policy = steam_signin.signin_policy({"signin_mode": "steam"})
        self.assertEqual("database", policy["steam_reward_mode"])
        self.assertEqual(
            "#ChangeCurrencyBalance Normal {reward} {steam_id}",
            policy["steam_reward_command"],
        )
        self.assertEqual(
            "绑定成功：{name} -> {steam_id}",
            policy["bind_success_message"],
        )
        self.assertEqual(
            "SteamID 绑定失败：{message}",
            policy["bind_failure_message"],
        )
        self.assertIn("{count}", policy["signin_message"])

    def test_bind_failures_keep_machine_reason_inside_default_template(self):
        result = steam_signin.bind_steam_id("1102579807", "10001", "bad-id")
        self.assertEqual("invalid_steam_id", result["code"])
        policy = steam_signin.signin_policy({})
        rendered = policy["bind_failure_message"].replace(
            "{message}", result["message"]
        )
        self.assertIn("17 位数字", rendered)

    def test_top_level_commands_are_normalized_to_one_token(self):
        policy = steam_signin.signin_policy(
            {
                "signin_command": "/每日签到",
                "bind_command": "绑定 SteamID",
            }
        )
        self.assertEqual("每日签到", policy["signin_command"])
        self.assertEqual("绑定SteamID", policy["bind_command"])

    def test_group_binding_privacy_defaults_and_limits(self):
        default_policy = steam_signin.signin_policy({})
        self.assertFalse(default_policy["allow_group_steam_binding"])
        self.assertTrue(default_policy["recall_group_steam_binding_message"])
        self.assertEqual(5, default_policy["group_steam_binding_recall_seconds"])

        policy = steam_signin.signin_policy(
            {
                "allow_group_steam_binding": "true",
                "recall_group_steam_binding_message": "false",
                "group_steam_binding_recall_seconds": 9999,
            }
        )
        self.assertTrue(policy["allow_group_steam_binding"])
        self.assertFalse(policy["recall_group_steam_binding_message"])
        self.assertEqual(600, policy["group_steam_binding_recall_seconds"])

    def test_duplicate_profile_commands_are_not_ambiguous(self):
        profiles = steam_signin.normalize_signin_profiles(
            [
                {"id": "first", "command": "签到活动"},
                {"id": "second", "command": "签到活动"},
                {"id": "third", "command": "周签到"},
            ]
        )
        self.assertEqual(["first", "third"], [row["id"] for row in profiles])

    def test_profile_without_reward_inherits_group_reward(self):
        policy = steam_signin.signin_policy(
            {
                "base_reward": 35,
                "reward_3_days": 90,
                "signin_profiles": [
                    {"id": "plain", "name": "普通方案", "command": "普通签到"},
                    {"id": "zero", "name": "零积分方案", "command": "零积分签到", "base_reward": 0},
                ],
            }
        )

        inherited = steam_signin.select_signin_profile(policy, "plain")
        explicit_zero = steam_signin.select_signin_profile(policy, "zero")
        self.assertEqual(inherited["base_reward"], 35)
        self.assertEqual(inherited["reward_3_days"], 90)
        self.assertEqual(explicit_zero["base_reward"], 0)

    def test_policy_parses_string_profiles_and_false_enabled(self):
        policy = steam_signin.signin_policy(
            {
                "enabled": "false",
                "database_profiles": [
                    '{"id":"main","path":"data/main.db",'
                    '"snapshot_revision":3,"snapshot_sha256":"'
                    + "a" * 64
                    + '"}'
                ],
            }
        )
        self.assertFalse(policy["enabled"])
        self.assertEqual(policy["database_profiles"][0]["id"], "main")
        self.assertEqual(policy["database_profiles"][0]["snapshot_revision"], 3)
        self.assertEqual(policy["database_profiles"][0]["snapshot_sha256"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
