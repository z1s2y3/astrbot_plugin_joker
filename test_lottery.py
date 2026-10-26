"""Focused regression tests for the Windows-local per-group lottery runtime."""

from __future__ import annotations

from copy import deepcopy
import importlib.util
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _load_lottery_module():
    package_name = "_lottery_runtime_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT / "qqadmin")]
    sys.modules[package_name] = package

    storage: dict[str, dict] = {}
    group_config = types.ModuleType(f"{package_name}.group_config")

    def get_group_data(group_id, key, default=None):
        return deepcopy(storage.get(str(group_id), {}).get(key, default))

    def set_group_data(group_id, key, value):
        storage.setdefault(str(group_id), {})[key] = deepcopy(value)

    def update_group_data(group_id, key, updater):
        group = storage.setdefault(str(group_id), {})
        group[key] = deepcopy(updater(deepcopy(group.get(key))))
        return deepcopy(group[key])

    def update_group_feature_config(group_id, feature, setting, value):
        group = storage.setdefault(str(group_id), {})
        features = group.setdefault("features", {})
        features.setdefault(str(feature), {})[str(setting)] = deepcopy(value)

    group_config.get_group_data = get_group_data
    group_config.set_group_data = set_group_data
    group_config.update_group_data = update_group_data
    group_config.update_group_feature_config = update_group_feature_config
    sys.modules[group_config.__name__] = group_config

    module_path = ROOT / "qqadmin" / "lottery.py"
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.lottery", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, storage


class LotteryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.lottery, self.storage = _load_lottery_module()

    def _enable(self, group_id="100"):
        self.lottery.set_lottery_enabled(group_id, True)

    def test_campaigns_are_independent_and_legacy_ids_are_stable(self):
        self.lottery.save_lottery_config(
            "100",
            {
                "enabled": True,
                "keyword": "抽奖",
                "campaigns": [
                    {
                        "id": "daily",
                        "name": "每日活动",
                        "keyword": "每日抽奖",
                        "enabled": True,
                        "awards": [{"name": "日卡", "probability": 100, "stock": 1}],
                    },
                    {
                        "id": "weekly",
                        "name": "每周活动",
                        "keyword": "每周抽奖",
                        "enabled": True,
                        "awards": [{"name": "周卡", "probability": 100, "stock": 1}],
                    },
                ],
                "awards": [],
            },
        )
        first = self.lottery.get_lottery_config("100")
        second = self.lottery.get_lottery_config("100")
        first_ids = [c["awards"][0]["id"] for c in first["campaigns"]]
        second_ids = [c["awards"][0]["id"] for c in second["campaigns"]]
        self.assertEqual(first_ids, second_ids)

        daily = self.lottery.draw_lottery(
            "100", "u1", "Alice", campaign_id="daily", now=1, random_value=0
        )
        weekly = self.lottery.draw_lottery(
            "100", "u1", "Alice", campaign_id="weekly", now=1, random_value=0
        )
        self.assertEqual("prize", daily["code"])
        self.assertEqual("日卡", daily["award"]["name"])
        self.assertEqual("prize", weekly["code"])
        self.assertEqual("周卡", weekly["award"]["name"])
        self.assertEqual(1, len(self.lottery.get_lottery_records("100", campaign_id="daily")))
        self.assertEqual(1, len(self.lottery.get_lottery_records("100", campaign_id="weekly")))

    def test_campaign_aliases_share_id_runtime_and_unknown_selector_uses_root(self):
        self.lottery.save_lottery_config(
            "100",
            {
                "enabled": True,
                "awards": [{"id": "root-prize", "name": "根奖", "probability": 100}],
                "campaigns": [
                    {
                        "id": "daily",
                        "name": "每日活动",
                        "keyword": "每日抽奖",
                        "enabled": True,
                        "awards": [
                            {"id": "daily-prize", "name": "日奖", "probability": 100}
                        ],
                    }
                ],
            },
        )

        by_name = self.lottery.draw_lottery(
            "100", "u1", "Alice", campaign_id="每日活动", now=1, random_value=0
        )
        by_id = self.lottery.draw_lottery(
            "100", "u2", "Bob", campaign_id="daily", now=2, random_value=0
        )
        self.assertEqual("日奖", by_name["award"]["name"])
        self.assertEqual("日奖", by_id["award"]["name"])
        self.assertEqual(
            2, len(self.lottery.get_lottery_records("100", campaign_id="每日活动"))
        )
        self.assertNotIn("lottery_runtime:每日活动", self.storage["100"])

        # An unknown selector falls back to the root activity and must not
        # create a private runtime bucket that bypasses root quotas/history.
        root = self.lottery.draw_lottery(
            "100", "u3", "Carol", campaign_id="does-not-exist", now=3, random_value=0
        )
        self.assertEqual("根奖", root["award"]["name"])
        self.assertEqual(1, len(self.lottery.get_lottery_records("100")))
        self.assertNotIn("lottery_runtime:does-not-exist", self.storage["100"])

    def test_root_keyword_wins_over_duplicate_campaign_keyword(self):
        raw = {
            "keyword": "抽奖",
            "campaigns": [
                {"id": "same", "name": "冲突活动", "keyword": "抽奖"},
                {"id": "other", "name": "其他活动", "keyword": "其他抽奖"},
            ],
        }
        config = self.lottery.normalize_lottery_config(raw)
        self.assertEqual(["other"], [row["id"] for row in config["campaigns"]])
        with self.assertRaisesRegex(ValueError, "指令不能重复"):
            self.lottery.validate_lottery_config(raw)

    def test_configuration_and_awards_are_per_group(self):
        self._enable("100")
        award = self.lottery.add_lottery_award("100", "月卡", 100, 3)

        self.assertEqual("月卡", award["name"])
        self.assertTrue(self.lottery.get_lottery_config("100")["enabled"])
        self.assertEqual([], self.lottery.get_lottery_config("200")["awards"])
        self.assertTrue(
            self.storage["100"]["features"]["lottery"]["enabled"]
        )

    def test_string_enabled_values_use_false_semantics(self):
        """Text form values from command/web payloads must not use bool(str)."""

        self.lottery.set_lottery_enabled("100", "true")
        self.assertTrue(self.lottery.get_lottery_config("100")["enabled"])
        self.lottery.set_lottery_enabled("100", "false")
        self.assertFalse(self.lottery.get_lottery_config("100")["enabled"])
        self.lottery.set_lottery_enabled("100", "0")
        self.assertFalse(self.lottery.get_lottery_config("100")["enabled"])

    def test_complete_save_updates_feature_scalar_mirror(self):
        self.lottery.save_lottery_config(
            "100",
            {
                "enabled": True,
                "keyword": "抽卡",
                "max_draws_per_user": 4,
                "cooldown": 12,
                "success_message": "{name} -> {prize}",
                "empty_message": "未中",
                "awards": [],
            },
        )
        mirror = self.storage["100"]["features"]["lottery"]
        self.assertEqual(True, mirror["enabled"])
        self.assertEqual("抽卡", mirror["keyword"])
        self.assertEqual(4, mirror["max_draws_per_user"])
        self.assertEqual(12, mirror["cooldown"])
        self.assertEqual("{name} -> {prize}", mirror["success_message"])
        self.assertEqual("未中", mirror["empty_message"])

    def test_stock_is_decremented_locally_and_records_do_not_cross_groups(self):
        self._enable("100")
        self.lottery.add_lottery_award("100", "限定", 100, 2)

        first = self.lottery.draw_lottery(
            "100", "11", "小明", now=100, random_value=0.0
        )
        second = self.lottery.draw_lottery(
            "100", "22", "小红", now=101, random_value=0.0
        )
        exhausted = self.lottery.draw_lottery(
            "100", "33", "小蓝", now=102, random_value=0.0
        )

        self.assertEqual("prize", first["code"])
        self.assertEqual(1, first["remaining"])
        self.assertEqual("prize", second["code"])
        self.assertEqual(0, second["remaining"])
        self.assertEqual("empty", exhausted["code"])
        self.assertEqual(3, len(self.lottery.get_lottery_records("100")))
        self.assertEqual([], self.lottery.get_lottery_records("200"))
        self.assertEqual(
            2,
            self.lottery.get_lottery_config("100")["awards"][0]["stock"],
        )

    def test_probability_limit_cooldown_and_templates_are_deterministic(self):
        self._enable()
        self.lottery.add_lottery_award("100", "半概率", 50, 0)
        self.lottery.set_lottery_messages(
            "100",
            success_message="{name}/{qq}/{prize}/{remaining}/{draw_count}/{time}",
            empty_message="{name}/{qq}/{prize}/{draw_count}/{unknown}",
        )
        self.lottery.set_lottery_limits("100", max_draws_per_user=2, cooldown=10)

        won = self.lottery.draw_lottery(
            "100", "11", "小明", now=1_700_000_000, random_value=0.1
        )
        waiting = self.lottery.draw_lottery(
            "100", "11", "小明", now=1_700_000_001, random_value=0.1
        )
        missed = self.lottery.draw_lottery(
            "100", "11", "小明", now=1_700_000_010, random_value=0.9
        )
        capped = self.lottery.draw_lottery(
            "100", "11", "小明", now=1_700_000_020, random_value=0.1
        )

        self.assertEqual("prize", won["code"])
        self.assertIn("小明/11/半概率/不限量/1/", won["message"])
        self.assertEqual("cooldown", waiting["code"])
        self.assertEqual(9, waiting["remaining_seconds"])
        self.assertEqual("empty", missed["code"])
        self.assertIn("小明/11/未中奖/2/{unknown}", missed["message"])
        self.assertEqual("draw_limit", capped["code"])

    def test_malformed_local_runtime_records_do_not_break_status_or_history(self):
        self._enable()
        self.lottery.add_lottery_award("100", "奖", 100, 0)
        self.storage["100"]["lottery_runtime"] = {
            "users": {"11": {"draw_count": 1}},
            "records": ["bad", None, {"user_id": "ok"}],
            "awards": {"award-x": "bad", "award-y": {"remaining": 1}},
        }

        self.assertEqual(
            [{"user_id": "ok"}],
            self.lottery.get_lottery_records("100"),
        )
        status = self.lottery.get_lottery_status("100")
        self.assertEqual(1, status["record_count"])
        drawn = self.lottery.draw_lottery("100", "22", now=10, random_value=0.0)
        self.assertTrue(drawn["success"])

    def test_keyword_uses_portal_feature_values_and_invalid_probability_is_rejected(self):
        self._enable()
        self.lottery.set_lottery_keyword("100", "抽卡")
        self.assertTrue(self.lottery.lottery_keyword_matches("100", "/抽卡"))
        self.assertFalse(self.lottery.lottery_keyword_matches("100", "/抽奖"))

        self.lottery.add_lottery_award("100", "甲", 60, 0)
        with self.assertRaisesRegex(ValueError, "概率总和"):
            self.lottery.add_lottery_award("100", "乙", 41, 0)
        self.assertEqual(1, len(self.lottery.get_lottery_config("100")["awards"]))

    def test_portal_projection_keeps_lottery_config_but_excludes_runtime_records(self):
        from web_client.sync_projection import project_group_config

        source = {
            "group_id": "100",
            "features": {"lottery": {"enabled": True, "keyword": "抽卡"}},
            "lottery": {"awards": [{"id": "award-a", "name": "奖", "probability": 100}]},
            "lottery_runtime": {"records": [{"user_id": "secret"}]},
        }
        projected = project_group_config(source)

        self.assertIn("lottery", projected)
        self.assertEqual("抽卡", projected["features"]["lottery"]["keyword"])
        self.assertNotIn("lottery_runtime", projected)

    def test_lottery_projection_drops_unknown_root_runtime_fields(self):
        """Only editable lottery policy crosses the Windows/Linux boundary."""

        from web_client.sync_projection import project_group_config

        source = {
            "group_id": "100",
            "features": {"lottery": {"enabled": True}},
            "lottery": {
                "enabled": True,
                "keyword": "抽卡",
                "awards": [],
                # A legacy/future Windows build may leave operational state
                # beside the editable definition. It must not be uploaded.
                "runtime": {"users": {"private": {"draw_count": 9}}},
                "records": [{"user_id": "private"}],
                "local_secret": "do-not-project",
            },
        }

        projected = project_group_config(source)
        self.assertEqual(
            {"enabled", "keyword", "awards"},
            set(projected["lottery"]),
        )

    def test_lottery_merge_preserves_local_unknown_root_fields(self):
        """A portal save cannot erase Windows-only lottery extensions."""

        from web_client.sync_projection import merge_group_projection

        existing = {
            "group_id": "100",
            "lottery": {
                "enabled": True,
                "keyword": "抽卡",
                "awards": [],
                "local_runtime_marker": "keep-local",
            },
        }
        projected = {
            "group_id": "100",
            "lottery": {
                "enabled": False,
                "keyword": "抽奖",
                "awards": [],
                "unknown_remote_field": "ignore",
            },
        }

        merged = merge_group_projection(existing, projected)
        self.assertFalse(merged["lottery"]["enabled"])
        self.assertEqual("抽奖", merged["lottery"]["keyword"])
        self.assertEqual("keep-local", merged["lottery"]["local_runtime_marker"])
        self.assertNotIn("unknown_remote_field", merged["lottery"])

    def test_award_definition_round_trips_through_projection(self):
        from web_client.sync_projection import merge_group_projection, project_group_config

        source = {
            "group_id": "100",
            "features": {
                "lottery": {
                    "enabled": True,
                    "keyword": "抽卡",
                    "success_message": "{name} 获得 {prize}",
                }
            },
            "lottery": {
                "enabled": True,
                "keyword": "抽卡",
                "max_draws_per_user": 3,
                "cooldown": 30,
                "success_message": "{name} 获得 {prize}",
                "empty_message": "再接再厉",
                "awards": [
                    {
                        "id": "award-month",
                        "name": "月卡",
                        "probability": 25.5,
                        "stock": 4,
                        "enabled": True,
                    }
                ],
            },
            "lottery_runtime": {"records": [{"user_id": "private"}]},
        }
        projected = project_group_config(source)
        self.assertEqual("月卡", projected["lottery"]["awards"][0]["name"])
        self.assertEqual(25.5, projected["lottery"]["awards"][0]["probability"])
        merged = merge_group_projection(
            {"group_id": "100", "lottery_runtime": {"records": [1]}}, projected
        )
        self.assertEqual("award-month", merged["lottery"]["awards"][0]["id"])
        self.assertEqual({"records": [1]}, merged["lottery_runtime"])

    def test_web_award_action_updates_root_and_feature_mirror(self):
        from web_client import services

        stored = {
            "group_id": "100",
            "features": {"lottery": {"enabled": True, "keyword": "抽奖"}},
            "lottery": {
                "enabled": True,
                "keyword": "抽奖",
                "max_draws_per_user": 0,
                "cooldown": 0,
                "success_message": "{name} {prize}",
                "empty_message": "未中奖",
                "awards": [],
            },
        }
        saved_updates = {}

        class FakeConfig:
            def get_group_config(self, _group_id):
                return deepcopy(stored)

            def batch_update_group_data(self, _group_id, updates):
                result = deepcopy(stored)
                for key, updater in updates:
                    result[key] = updater(result.get(key))
                    saved_updates[key] = deepcopy(result[key])
                return result

        with patch.object(services, "remote_data_mode", return_value=False), patch.object(
            services, "group_config_module", return_value=FakeConfig()
        ):
            result = services.apply_lottery_award_action(
                "100",
                "add",
                name="月卡",
                probability="25.5",
                stock="3",
                enabled=True,
            )
        self.assertEqual("月卡", saved_updates["lottery"]["awards"][0]["name"])
        self.assertEqual(25.5, saved_updates["lottery"]["awards"][0]["probability"])
        self.assertTrue(saved_updates["features"]["lottery"]["enabled"])
        self.assertEqual("月卡", result["lottery"]["awards"][0]["name"])


if __name__ == "__main__":
    unittest.main()
