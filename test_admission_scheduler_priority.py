"""Offline behavioral regressions for admission and multi-account dispatch."""

import ast
import asyncio
import copy
import logging
import os
import re
import types
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parent
TREE = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
PLUGIN = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "MyPlugin")
METHODS = {node.name: node for node in PLUGIN.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def load_plugin(*method_names):
    methods = [copy.deepcopy(METHODS[name]) for name in method_names]
    for method in methods:
        method.decorator_list = [decorator for decorator in method.decorator_list if isinstance(decorator, ast.Name) and decorator.id == "staticmethod"]
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        ast.ClassDef(name="Plugin", bases=[], keywords=[], body=methods, decorator_list=[]),
    ], type_ignores=[])
    namespace = {
        "asyncio": asyncio,
        "os": os,
        "re": re,
        "time": time,
        "logger": logging.getLogger(__name__),
        "filter": types.SimpleNamespace(
            PlatformAdapterType=types.SimpleNamespace(AIOCQHTTP="onebot")
        ),
    }
    exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), namespace)
    return namespace["Plugin"](), namespace


class PriorityTests(unittest.TestCase):
    def setUp(self):
        self.plugin, self.namespace = load_plugin(
            "_normalize_positive_id", "_group_bot_priority_map", "_timed_account_priority",
            "_is_primary_group_account", "_is_primary_group_bot", "_select_timed_task_client",
            "_claim_group_event_action",
        )
        self.namespace["_GROUP_EVENT_ACTION_CLAIMS"] = {}
        self.plugin._GROUP_BOT_ACTIVITY_TTL_SECONDS = 120
        self.plugin.config = {}

    def configure(self, value):
        self.namespace["get_group_feature_detail"] = lambda *_args: {"bot_account_priorities": value}

    def test_structured_priorities_override_environment_for_scheduled_work(self):
        for setting in ({"20": 80, "3": 80}, [("20", 80), ("3", 80)], "20=80;3:80"):
            with self.subTest(setting=setting), patch.dict(os.environ, {"JOKER_ACCOUNT_PRIORITY": "99"}):
                self.configure(setting)
                self.assertEqual(80, self.plugin._timed_account_priority("100", "3"))

    def test_no_group_priority_allows_every_connected_account_to_reply(self):
        self.configure("")
        self.plugin._get_event_self_id = lambda event: event.self_id
        self.assertTrue(
            self.plugin._is_primary_group_bot(types.SimpleNamespace(self_id="100"), "1000")
        )
        self.assertTrue(
            self.plugin._is_primary_group_bot(types.SimpleNamespace(self_id="200"), "1000")
        )

    def test_wildcard_group_priority_allows_every_connected_account_to_reply(self):
        self.configure("*=0")
        self.plugin._get_event_self_id = lambda event: event.self_id
        self.assertTrue(
            self.plugin._is_primary_group_bot(types.SimpleNamespace(self_id="100"), "1000")
        )
        self.assertTrue(
            self.plugin._is_primary_group_bot(types.SimpleNamespace(self_id="200"), "1000")
        )

    def test_no_group_priority_scopes_action_deduplication_per_account(self):
        self.configure("")
        self.plugin._get_group_id_from_event = lambda _event: "1000"
        self.plugin._get_event_message_id = lambda event: event.message_id
        self.plugin._get_sender_id_from_event = lambda _event: "300"
        self.plugin._get_event_self_id = lambda event: event.self_id
        first = types.SimpleNamespace(self_id="100", message_id="message-1")
        second = types.SimpleNamespace(self_id="200", message_id="message-1")
        self.assertTrue(self.plugin._claim_group_event_action(first, "reply", "hello"))
        self.assertTrue(self.plugin._claim_group_event_action(second, "reply", "hello"))
        self.assertFalse(self.plugin._claim_group_event_action(first, "reply", "hello"))

    def test_tied_accounts_use_numeric_order_and_ignore_recent_messages(self):
        self.configure("20=80;3=80")
        first, recent = types.SimpleNamespace(call_action=AsyncMock()), types.SimpleNamespace(call_action=AsyncMock())
        self.plugin._group_platform_bot_candidates = {"100": {
            "3": {"account_id": "3", "bot": first, "seen_at": 1},
            "20": {"account_id": "20", "bot": recent, "seen_at": 999},
        }}
        self.plugin._get_event_self_id = lambda event: event.self_id
        self.assertTrue(self.plugin._is_primary_group_bot(types.SimpleNamespace(self_id="3"), "100"))
        self.assertFalse(self.plugin._is_primary_group_bot(types.SimpleNamespace(self_id="20"), "100"))
        self.assertEqual((first, "3", 80), self.plugin._select_timed_task_client("100"))

    def test_unlisted_environment_priority_cannot_hide_configured_winner(self):
        self.configure({"3": 5})
        winner = types.SimpleNamespace(call_action=AsyncMock())
        self.plugin._group_platform_bot_candidates = {"100": {
            "3": {"account_id": "3", "bot": winner},
            "20": {"account_id": "20", "bot": types.SimpleNamespace(call_action=AsyncMock())},
        }}
        with patch.dict(os.environ, {"JOKER_ACCOUNT_PRIORITY": "100"}):
            self.assertEqual((winner, "3", 5), self.plugin._select_timed_task_client("100"))

    def test_lower_priority_account_takes_over_when_configured_winner_is_stale(self):
        self.configure("100=90;200=10")
        now = int(time.time())
        self.plugin._group_platform_bot_candidates = {"100": {
            "100": {
                "account_id": "100",
                "bot": types.SimpleNamespace(call_action=AsyncMock()),
                "seen_at": now - 300,
            },
            "200": {
                "account_id": "200",
                "bot": types.SimpleNamespace(call_action=AsyncMock()),
                "seen_at": now,
            },
        }}
        self.plugin._get_event_self_id = lambda event: event.self_id

        self.assertTrue(self.plugin._is_primary_group_bot(
            types.SimpleNamespace(self_id="200"), "100"
        ))

    def test_recovered_high_priority_account_reclaims_group(self):
        self.configure("100=90;200=10")
        now = int(time.time())
        self.plugin._group_platform_bot_candidates = {"100": {
            "100": {
                "account_id": "100",
                "bot": types.SimpleNamespace(call_action=AsyncMock()),
                "seen_at": now,
            },
            "200": {
                "account_id": "200",
                "bot": types.SimpleNamespace(call_action=AsyncMock()),
                "seen_at": now,
            },
        }}
        self.plugin._get_event_self_id = lambda event: event.self_id

        self.assertTrue(self.plugin._is_primary_group_bot(
            types.SimpleNamespace(self_id="100"), "100"
        ))
        self.assertFalse(self.plugin._is_primary_group_bot(
            types.SimpleNamespace(self_id="200"), "100"
        ))

    def test_high_priority_gate_stops_secondary_group_event_before_handlers(self):
        plugin, namespace = load_plugin(
            "_normalize_positive_id",
            "_group_bot_priority_map",
            "_is_primary_group_bot",
            "_get_event_self_id",
            "_get_group_id_from_event",
            "_event_allows_primary_group_bot",
            "gate_nonprimary_group_bot",
        )
        plugin.config = {}
        namespace["get_group_feature_detail"] = lambda *_args: {
            "bot_account_priorities": "100=90;200=10"
        }
        event = types.SimpleNamespace(self_id="200", stop_event=Mock())
        plugin._get_event_self_id = lambda value: value.self_id
        plugin._get_group_id_from_event = lambda _event: "123"

        # The lower-priority account is stopped before auto-reply or command
        # handlers can mutate cooldowns, draw state, or send a response.
        asyncio.run(plugin.gate_nonprimary_group_bot(event))
        event.stop_event.assert_called_once_with()

    def test_primary_group_event_passes_the_gate(self):
        plugin, namespace = load_plugin(
            "_normalize_positive_id",
            "_group_bot_priority_map",
            "_is_primary_group_bot",
            "_get_event_self_id",
            "_get_group_id_from_event",
            "_event_allows_primary_group_bot",
            "gate_nonprimary_group_bot",
        )
        plugin.config = {}
        namespace["get_group_feature_detail"] = lambda *_args: {
            "bot_account_priorities": "100=90;200=10"
        }
        event = types.SimpleNamespace(self_id="100", stop_event=Mock())
        plugin._get_event_self_id = lambda value: value.self_id
        plugin._get_group_id_from_event = lambda _event: "123"

        asyncio.run(plugin.gate_nonprimary_group_bot(event))
        event.stop_event.assert_not_called()

    def test_inbound_message_cache_runs_before_nonprimary_gate(self):
        def priority(method_name):
            method = METHODS[method_name]
            for decorator in method.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if not isinstance(decorator.func, ast.Attribute):
                    continue
                if decorator.func.attr != "event_message_type":
                    continue
                for keyword in decorator.keywords:
                    if keyword.arg == "priority" and isinstance(keyword.value, ast.Constant):
                        return keyword.value.value
            return 0

        self.assertGreater(
            priority("handle_group_message_cache"),
            priority("gate_nonprimary_group_bot"),
            "入站群消息缓存必须先于非主账号拦截器执行，否则网页只会显示机器人消息",
        )
        self.assertEqual(21000, priority("handle_group_message_cache"))
        self.assertEqual(20000, priority("gate_nonprimary_group_bot"))


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_request_does_not_retry_flag_on_another_account(self):
        plugin, _namespace = load_plugin("_call_group_request_action")
        owner = types.SimpleNamespace(call_action=AsyncMock(return_value={"status": "failed"}))
        other = types.SimpleNamespace(call_action=AsyncMock(return_value={"status": "ok"}))
        plugin.context = types.SimpleNamespace(get_platform=lambda *_args: types.SimpleNamespace(get_client=lambda: other))
        plugin._api_result_succeeded = lambda result: result.get("status") == "ok"
        plugin._onebot_failure_detail = lambda _result: "rejected"
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            await plugin._call_group_request_action(types.SimpleNamespace(bot=owner), action="set_group_add_request", flag="account-scoped", approve=True)
        owner.call_action.assert_awaited_once()
        other.call_action.assert_not_awaited()

    async def test_request_without_bound_client_uses_platform(self):
        plugin, _namespace = load_plugin("_call_group_request_action")
        client = types.SimpleNamespace(call_action=AsyncMock(return_value={"status": "ok"}))
        plugin.context = types.SimpleNamespace(get_platform=lambda *_args: types.SimpleNamespace(get_client=lambda: client))
        plugin._api_result_succeeded = lambda result: result.get("status") == "ok"
        self.assertEqual({"status": "ok"}, await plugin._call_group_request_action(types.SimpleNamespace(), action="set_group_add_request", flag="flag"))

    async def test_friend_request_is_not_group_admission(self):
        plugin, _namespace = load_plugin("handle_group_request_auto_audit")
        plugin._get_event_raw_payload = lambda _event: {"post_type": "request", "request_type": "friend"}
        plugin._process_group_request = AsyncMock()
        await plugin.handle_group_request_auto_audit(types.SimpleNamespace())
        plugin._process_group_request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
