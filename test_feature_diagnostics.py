import unittest
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

from web_client.remote_store import _safe_runtime_summary
from web_client.services import _independent_action_features, _normalize_runtime_panels


_GROUP_CONFIG_STUB = types.ModuleType("group_config")
_GROUP_CONFIG_STUB.get_group_data = lambda *_args: []
_GROUP_CONFIG_STUB.update_group_data = lambda *_args: None
sys.modules.setdefault("group_config", _GROUP_CONFIG_STUB)
_DIAGNOSTICS_SPEC = importlib.util.spec_from_file_location(
    "feature_diagnostics_test", Path(__file__).with_name("qqadmin") / "feature_diagnostics.py"
)
diagnostics = importlib.util.module_from_spec(_DIAGNOSTICS_SPEC)
assert _DIAGNOSTICS_SPEC.loader is not None
_DIAGNOSTICS_SPEC.loader.exec_module(diagnostics)


class FeatureDiagnosticsTests(unittest.TestCase):
    def test_record_is_bounded_and_coalesces_consecutive_failures(self):
        stored = {"rows": []}

        def read(_group_id, _key, default):
            return list(stored["rows"]) or default

        def write(_group_id, _key, mutate):
            stored["rows"] = mutate(list(stored["rows"]))

        with patch.object(diagnostics, "get_group_data", side_effect=read), patch.object(
            diagnostics, "update_group_data", side_effect=write
        ):
            first = diagnostics.record_feature_error(
                "1102579807", "游戏指令", "ws_timeout", "连接失败\npassword=secret", operation="签到"
            )
            second = diagnostics.record_feature_error(
                "1102579807", "游戏指令", "ws_timeout", "连接失败\npassword=secret", operation="签到"
            )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(len(stored["rows"]), 1)
        self.assertEqual(stored["rows"][0]["count"], 2)
        self.assertIn("password=[redacted]", stored["rows"][0]["message"])

    def test_web_runtime_summary_keeps_error_rows_safe(self):
        summary = _safe_runtime_summary(
            {
                "errors": {
                    "count": 999999999,
                    "recent": [
                        {
                            "timestamp": 123,
                            "feature": "x" * 100,
                            "code": "timeout",
                            "operation": "op",
                            "message": "bad\nmessage",
                            "count": 2,
                        }
                    ],
                }
            }
        )
        self.assertEqual(summary["errors"]["count"], 100000)
        self.assertEqual(len(summary["errors"]["recent"]), 1)
        self.assertNotIn("\n", summary["errors"]["recent"][0]["message"])
        panels = _normalize_runtime_panels(summary)
        self.assertTrue(panels["errors"]["available"])
        self.assertEqual(panels["errors"]["recent"][0]["timestamp_text"], "1970-01-01 08:02")

    def test_web_runtime_summary_keeps_blacklist_and_operation_rows(self):
        summary = _safe_runtime_summary(
            {
                "blacklist": {
                    "total": 1,
                    "active": 1,
                    "entries": [{"user_id": "123456789", "expire_time": 0}],
                },
                "global_blacklist": {
                    "total": 1,
                    "active": 1,
                    "entries": [
                        {
                            "user_id": "987654321",
                            "added_at": 200,
                            "allowed_in_group": True,
                        }
                    ],
                },
                "operations": {
                    "count": 2,
                    "recent": [
                        {"action": "禁言", "timestamp": 100},
                        {"action": "入群审核", "timestamp": 200},
                    ],
                },
            }
        )
        self.assertEqual(summary["global_blacklist"]["entries"][0]["added_at"], 200)
        self.assertTrue(summary["global_blacklist"]["entries"][0]["allowed_in_group"])
        self.assertEqual(
            [row["timestamp"] for row in summary["operations"]["recent"]],
            [200, 100],
        )

    def test_statistics_actions_are_individually_visible(self):
        rows = _independent_action_features(
            {"features": {"stats": {"enabled": True}}}
        )
        self.assertEqual(
            {row["key"] for row in rows},
            {"signin", "signin_status", "signin_calendar", "signin_ranking", "ranking", "user_info", "group_stats"},
        )
        self.assertTrue(all(row["enabled"] for row in rows))
        self.assertTrue(all(row["readonly"] for row in rows))

    def test_runtime_panels_decode_legacy_text_json(self):
        raw = '{"members":{"count":1,"members":[{"user_id":"123456789","user_name":"测试成员"}]}}'
        panels = _normalize_runtime_panels(raw)
        self.assertEqual(1, panels["members"]["valid_count"])
        self.assertEqual("123456789", panels["members"]["members"][0]["user_id"])


if __name__ == "__main__":
    unittest.main()
