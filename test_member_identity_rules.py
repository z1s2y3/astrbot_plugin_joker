"""Regression tests for member card/title rule normalization."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import unittest

# Loading this leaf module directly keeps the test independent of the large
# qqadmin package initializer, which expects the AstrBot runtime to be loaded.
_MODULE_PATH = Path(__file__).resolve().parent / "qqadmin" / "member_identity.py"
_SPEC = importlib.util.spec_from_file_location("_member_identity_rules_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

WILDCARD_USER_ID = _MODULE.WILDCARD_USER_ID
member_rule_for_user = _MODULE.member_rule_for_user
normalize_member_rule = _MODULE.normalize_member_rule
normalize_member_rules = _MODULE.normalize_member_rules
rules_as_json_lines = _MODULE.rules_as_json_lines
apply_member_name_prefix = _MODULE.apply_member_name_prefix
strip_member_name_prefix = _MODULE.strip_member_name_prefix


def _project_group_config(config):
    """Load the projection helper without requiring the AstrBot runtime."""
    from web_client import sync_projection

    # The standalone test import intentionally leaves the optional qqadmin
    # dependency unset. Inject the same leaf functions used in production so
    # this test exercises the actual feature projection path.
    sync_projection.normalize_member_rules = normalize_member_rules
    sync_projection.rules_as_json_lines = rules_as_json_lines
    return sync_projection.project_group_config(config)


class MemberIdentityRuleTests(unittest.TestCase):
    def test_wildcard_aliases_are_canonicalized(self):
        aliases = ("*", "all", "everyone", "全体", "所有人")
        for alias in aliases:
            with self.subTest(alias=alias):
                self.assertEqual(
                    {"user_id": WILDCARD_USER_ID, "name": "玩家"},
                    normalize_member_rule({"user_id": alias, "name": "玩家"}, "name"),
                )

    def test_projection_wire_lines_keep_wildcard_rule(self):
        lines = rules_as_json_lines(
            [
                "所有人=SCUM {user_id}",
                '{"user_id":"2650362143","name":"管理员"}',
            ],
            "name",
        )
        self.assertEqual(2, len(lines))
        self.assertEqual(
            [
                {"user_id": "*", "name": "SCUM {user_id}"},
                {"user_id": "2650362143", "name": "管理员"},
            ],
            [json.loads(line) for line in lines],
        )

    def test_exact_member_rule_overrides_wildcard(self):
        rules = [
            "all=默认 {user_id}",
            "2650362143=专属",
        ]
        self.assertEqual(
            {"user_id": "2650362143", "name": "专属"},
            member_rule_for_user(rules, "name", "2650362143"),
        )
        self.assertEqual(
            {"user_id": "*", "name": "默认 {user_id}"},
            member_rule_for_user(rules, "name", "123456789"),
        )

    def test_duplicate_wildcard_aliases_last_value_wins(self):
        rules = normalize_member_rules(
            ["all=旧名称", "所有人=新名称"],
            "name",
        )
        self.assertEqual(
            [{"user_id": "*", "name": "新名称"}],
            rules,
        )

    def test_legacy_multiline_string_keeps_each_rule(self):
        self.assertEqual(
            [
                {"user_id": "*", "name": "SCUM {user_name}"},
                {"user_id": "2650362143", "name": "管理员"},
            ],
            normalize_member_rules(
                "所有人=SCUM {user_name}\n2650362143=管理员",
                "name",
            ),
        )

    def test_full_width_digits_are_not_accepted_as_qq_id(self):
        self.assertIsNone(normalize_member_rule("２６５０３６２１４３=玩家", "name"))

    def test_web_projection_round_trip_retains_wildcard(self):
        local = {
            "group_id": "1102579807",
            "features": {
                "group_config": {
                    "auto_member_name": True,
                    "member_names": ["所有人=SCUM {user_id}"],
                }
            },
        }
        projected = _project_group_config(local)
        self.assertEqual(
            '{"user_id":"*","name":"SCUM {user_id}"}',
            projected["features"]["group_config"]["member_names"][0],
        )

    def test_global_prefix_is_idempotent_and_bounded(self):
        self.assertEqual("scum-张先生", apply_member_name_prefix("scum-", "张先生"))
        self.assertEqual("scum-张先生", apply_member_name_prefix("scum-", "scum-张先生"))
        self.assertEqual("scum-成员", apply_member_name_prefix("scum-", ""))
        self.assertEqual("scum-1234567890123456789012345", apply_member_name_prefix("scum-", "123456789012345678901234567890", 30))

    def test_prefix_migration_removes_nested_old_prefixes(self):
        self.assertEqual("张先生", strip_member_name_prefix("scum-", "scum-scum-张先生"))
        self.assertEqual("vip-张先生", apply_member_name_prefix("vip-", strip_member_name_prefix("scum-", "scum-张先生")))


if __name__ == "__main__":
    unittest.main()
