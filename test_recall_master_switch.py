"""Regression coverage for the recall master switch and bot-message recall."""

from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import logging
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
RECALL_PATH = ROOT / "qqadmin" / "recall.py"
MAIN_PATH = ROOT / "main.py"
PACKAGE_ROOT = "_joker_recall_master_switch_test"
QQADMIN_PACKAGE = f"{PACKAGE_ROOT}.qqadmin"
RECALL_MODULE = f"{QQADMIN_PACKAGE}.recall"


def _make_modules(configurations: dict[str, dict]) -> dict[str, types.ModuleType]:
    """Provide only the AstrBot and group-config surface recall.py needs."""
    root_package = types.ModuleType(PACKAGE_ROOT)
    root_package.__path__ = []
    qqadmin_package = types.ModuleType(QQADMIN_PACKAGE)
    qqadmin_package.__path__ = []

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.logger = logging.getLogger("test_recall_master_switch")

    group_config = types.ModuleType(f"{QQADMIN_PACKAGE}.group_config")
    group_data: dict[tuple[str, str], object] = {}

    def load_group_config(group_id: str):
        return configurations.get(str(group_id))

    def get_group_data(group_id: str, key: str, default=None):
        return group_data.get((str(group_id), key), default)

    def set_group_data(group_id: str, key: str, value):
        group_data[(str(group_id), key)] = value

    def update_group_data(group_id: str, key: str, updater):
        value = updater(get_group_data(group_id, key))
        set_group_data(group_id, key, value)
        return value

    group_config.load_group_config = load_group_config
    group_config.get_group_data = get_group_data
    group_config.set_group_data = set_group_data
    group_config.update_group_data = update_group_data
    group_config.save_group_config = lambda group_id, config: configurations.update(
        {str(group_id): config}
    )
    group_config.init_group_config = lambda group_id: {
        "group_id": str(group_id),
        "features": {"recall": {}},
    }

    return {
        PACKAGE_ROOT: root_package,
        QQADMIN_PACKAGE: qqadmin_package,
        f"{QQADMIN_PACKAGE}.group_config": group_config,
        "astrbot": astrbot,
        "astrbot.api": astrbot_api,
    }


def _load_recall_module(configurations: dict[str, dict]):
    modules = _make_modules(configurations)
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(RECALL_MODULE, RECALL_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[RECALL_MODULE] = module
        spec.loader.exec_module(module)
    modules[RECALL_MODULE] = module
    return module, modules


def _load_recall_bot_message_method():
    """Compile just the plugin method so this test stays AstrBot-independent."""
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    method_node = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MyPlugin":
            method_node = next(
                (
                    child
                    for child in node.body
                    if isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "_recall_bot_message"
                ),
                None,
            )
            break
    assert method_node is not None, "MyPlugin._recall_bot_message was not found"

    namespace = {
        "__package__": PACKAGE_ROOT,
        "asyncio": asyncio,
        "logger": logging.getLogger("test_recall_master_switch.main"),
        "print_group_action": lambda *args, **kwargs: None,
    }
    extracted = ast.Module(body=[copy.deepcopy(method_node)], type_ignores=[])
    exec(compile(ast.fix_missing_locations(extracted), str(MAIN_PATH), "exec"), namespace)
    return namespace["_recall_bot_message"]


def _load_plugin_static_method(method_name: str):
    """Compile one dependency-free plugin static method for focused tests."""
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MyPlugin":
            method_node = next(
                (
                    child
                    for child in node.body
                    if isinstance(child, ast.FunctionDef) and child.name == method_name
                ),
                None,
            )
            assert method_node is not None, f"MyPlugin.{method_name} was not found"
            namespace = {"re": re}
            extracted = ast.Module(body=[copy.deepcopy(method_node)], type_ignores=[])
            exec(compile(ast.fix_missing_locations(extracted), str(MAIN_PATH), "exec"), namespace)
            return namespace[method_name]
    raise AssertionError("MyPlugin was not found")


def _load_message_id_method():
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MyPlugin":
            method_node = next(
                child
                for child in node.body
                if isinstance(child, ast.FunctionDef)
                and child.name == "_get_event_message_id"
            )
            namespace = {}
            extracted = ast.Module(body=[copy.deepcopy(method_node)], type_ignores=[])
            exec(compile(ast.fix_missing_locations(extracted), str(MAIN_PATH), "exec"), namespace)
            return namespace["_get_event_message_id"]
    raise AssertionError("MyPlugin._get_event_message_id was not found")


def _load_event_self_id_method():
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MyPlugin":
            method_node = next(
                child
                for child in node.body
                if isinstance(child, ast.FunctionDef)
                and child.name == "_get_event_self_id"
            )
            namespace = {"AstrMessageEvent": object}
            extracted = ast.Module(body=[copy.deepcopy(method_node)], type_ignores=[])
            exec(compile(ast.fix_missing_locations(extracted), str(MAIN_PATH), "exec"), namespace)
            return namespace["_get_event_self_id"]
    raise AssertionError("MyPlugin._get_event_self_id was not found")


def _load_event_bot_method():
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MyPlugin":
            method_node = next(
                child
                for child in node.body
                if isinstance(child, ast.AsyncFunctionDef)
                and child.name == "_is_event_from_bot"
            )
            namespace = {
                "AstrMessageEvent": object,
                "asyncio": asyncio,
                "time": __import__("time"),
                "logger": logging.getLogger("test_recall_master_switch.main"),
            }
            extracted = ast.Module(body=[copy.deepcopy(method_node)], type_ignores=[])
            exec(compile(ast.fix_missing_locations(extracted), str(MAIN_PATH), "exec"), namespace)
            return namespace["_is_event_from_bot"]
    raise AssertionError("MyPlugin._is_event_from_bot was not found")


def _load_reply_command_method():
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MyPlugin":
            method_node = next(
                child
                for child in node.body
                if isinstance(child, ast.AsyncFunctionDef)
                and child.name == "_handle_reply_commands"
            )
            namespace = {"AstrMessageEvent": object}
            extracted = ast.Module(body=[copy.deepcopy(method_node)], type_ignores=[])
            exec(compile(ast.fix_missing_locations(extracted), str(MAIN_PATH), "exec"), namespace)
            return namespace["_handle_reply_commands"]
    raise AssertionError("MyPlugin._handle_reply_commands was not found")


class _FakeBot:
    def __init__(self):
        self.deleted_message_ids: list[int] = []

    async def delete_msg(self, *, message_id: int):
        self.deleted_message_ids.append(message_id)


class _RecallActionFailedBot(_FakeBot):
    async def delete_msg(self, *, message_id: int):
        error = RuntimeError("decode failed")
        setattr(error, "retcode", 1200)
        raise error


class _RecallCompletedThenTimedOutBot(_FakeBot):
    async def delete_msg(self, *, message_id: int):
        error = RuntimeError(
            "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/recallMsg "
            "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate "
            'EventRet:\\n{\\n    "result": 0,\\n    "errMsg": ""\\n}'
        )
        setattr(error, "retcode", 1200)
        raise error


class _OpaqueRecallCompletedThenTimedOutBot(_FakeBot):
    async def delete_msg(self, *, message_id: int):
        error = RuntimeError("ActionFailed")
        error.message = (
            "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/recallMsg "
            "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate "
            'EventRet: {"result": 0, "errMsg": ""}'
        )
        setattr(error, "retcode", 1200)
        raise error


class _RecallPlugin:
    @staticmethod
    def _api_result_succeeded(result):
        if not isinstance(result, dict):
            return True
        if result.get("success") is False:
            return False
        if result.get("retcode") not in (None, 0, "0"):
            return False
        return str(result.get("status", "") or "").strip().lower() not in {
            "failed", "failure", "error"
        }

    @staticmethod
    def _recall_timeout_reported_success(error):
        return False


class _HistoryBot(_FakeBot):
    def __init__(self, messages, members=None, failures=None, api_failures=None):
        super().__init__()
        self.messages = list(messages)
        self.members = list(members or [])
        self.failures = set(failures or [])
        self.api_failures = set(api_failures or [])
        self.actions = []

    async def call_action(self, action=None, **kwargs):
        self.actions.append((action, kwargs))
        if action == "get_group_member_list":
            return {"members": self.members}
        if action == "get_group_msg_history":
            return {"messages": list(self.messages)}
        return {}

    async def delete_msg(self, *, message_id: int):
        if message_id in self.failures:
            raise RuntimeError("cannot recall")
        if message_id in self.api_failures:
            return {"status": "failed", "retcode": 100}
        await super().delete_msg(message_id=message_id)


class _RecallEvent:
    def __init__(self, bot):
        self.bot = bot
        self.message_obj = type("Message", (), {"group_id": RecallMasterSwitchTests.GROUP_ID})()

    def get_group_id(self):
        return RecallMasterSwitchTests.GROUP_ID

    def get_sender_id(self):
        return "operator"


class RecallMasterSwitchTests(unittest.TestCase):
    GROUP_ID = "1102579807"

    def setUp(self):
        self.configurations: dict[str, dict] = {}
        self.recall, self.modules = _load_recall_module(self.configurations)
        self.recall_bot_message = _load_recall_bot_message_method()
        self.recall_timeout_reported_success = _load_plugin_static_method(
            "_recall_timeout_reported_success"
        )

    def _set_recall_config(self, *, master: bool, self_recall: bool):
        self.configurations[self.GROUP_ID] = {
            "recall_enabled": master,
            "self_recall_enabled": self_recall,
            "features": {
                "recall": {
                    "enabled": master,
                    "self_recall_enabled": self_recall,
                }
            },
        }

    def test_master_switch_controls_effective_self_recall(self):
        with patch.dict(sys.modules, self.modules):
            self._set_recall_config(master=False, self_recall=True)
            self.assertFalse(self.recall.is_self_recall_enabled(self.GROUP_ID))

            status = self.recall.get_self_recall_status(self.GROUP_ID)
            self.assertFalse(status["enabled"])
            self.assertFalse(status["master_enabled"])
            self.assertTrue(status["configured"])

            self._set_recall_config(master=True, self_recall=True)
            self.assertTrue(self.recall.is_self_recall_enabled(self.GROUP_ID))

    def test_cheat_download_ad_is_classified_but_single_word_is_not(self):
        sample = (
            "三角洲 PUBG 一键注入，人物透视、子弹锁头、独家防封；"
            "免费内测，加入群725079073获取下载。"
        )
        categories = self.recall.detect_violation_categories(sample)
        self.assertTrue(any(item["category"] == "外挂/违法引流" for item in categories))
        self.assertEqual(
            [],
            self.recall.detect_violation_categories("讨论游戏透视效果的开发原理"),
        )

    def test_queued_module_recall_does_not_delete_after_master_is_closed(self):
        async def scenario():
            self._set_recall_config(master=True, self_recall=True)
            bot = _FakeBot()
            await self.recall.schedule_self_recall(bot, self.GROUP_ID, 42, 0.02)
            self._set_recall_config(master=False, self_recall=True)
            await asyncio.sleep(0.04)
            return bot

        with patch.dict(sys.modules, self.modules):
            bot = asyncio.run(scenario())
        self.assertEqual([], bot.deleted_message_ids)

    def test_queued_plugin_recall_does_not_delete_after_master_is_closed(self):
        async def scenario():
            self._set_recall_config(master=True, self_recall=True)
            bot = _FakeBot()
            task = asyncio.create_task(
                self.recall_bot_message(_RecallPlugin(), bot, 84, 0.02, self.GROUP_ID)
            )
            await asyncio.sleep(0)
            self._set_recall_config(master=False, self_recall=True)
            await task
            return bot

        with patch.dict(sys.modules, self.modules):
            bot = asyncio.run(scenario())
        self.assertEqual([], bot.deleted_message_ids)

    def test_plugin_recall_still_deletes_when_both_switches_are_on(self):
        async def scenario():
            self._set_recall_config(master=True, self_recall=True)
            bot = _FakeBot()
            await self.recall_bot_message(_RecallPlugin(), bot, 126, 0, self.GROUP_ID)
            return bot

        with patch.dict(sys.modules, self.modules):
            bot = asyncio.run(scenario())
        self.assertEqual([126], bot.deleted_message_ids)

    def test_plugin_recall_skips_platform_delete_exception(self):
        async def scenario():
            self._set_recall_config(master=True, self_recall=True)
            await self.recall_bot_message(
                _RecallPlugin(),
                _RecallActionFailedBot(),
                127,
                0,
                self.GROUP_ID,
            )

        with patch.dict(sys.modules, self.modules):
            with self.assertLogs(
                "test_recall_master_switch.main", level="WARNING"
            ) as captured:
                asyncio.run(scenario())
        self.assertTrue(any("自动撤回跳过" in line for line in captured.output))
        self.assertTrue(any("retcode=1200" in line for line in captured.output))

    def test_plugin_recall_accepts_napcat_success_callback_after_timeout(self):
        async def scenario():
            self._set_recall_config(master=True, self_recall=True)
            plugin = types.SimpleNamespace(
                _api_result_succeeded=_RecallPlugin._api_result_succeeded,
                _recall_timeout_reported_success=self.recall_timeout_reported_success,
            )
            await self.recall_bot_message(
                plugin,
                _RecallCompletedThenTimedOutBot(),
                128,
                0,
                self.GROUP_ID,
            )

        with patch.dict(sys.modules, self.modules):
            with self.assertNoLogs("test_recall_master_switch.main", level="WARNING"):
                asyncio.run(scenario())

    def test_plugin_recall_reads_completed_timeout_from_adapter_message_field(self):
        async def scenario():
            self._set_recall_config(master=True, self_recall=True)
            plugin = types.SimpleNamespace(
                _api_result_succeeded=_RecallPlugin._api_result_succeeded,
                _recall_timeout_reported_success=self.recall_timeout_reported_success,
            )
            await self.recall_bot_message(
                plugin,
                _OpaqueRecallCompletedThenTimedOutBot(),
                129,
                0,
                self.GROUP_ID,
            )

        with patch.dict(sys.modules, self.modules):
            with self.assertNoLogs("test_recall_master_switch.main", level="WARNING"):
                asyncio.run(scenario())

    def test_recall_timeout_success_match_requires_a_successful_recall_callback(self):
        success = RuntimeError(
            'Timeout: recallMsg EventRet: {"result": 0, "errMsg": ""}'
        )
        failure = RuntimeError(
            'Timeout: recallMsg EventRet: {"result": 7, "errMsg": "decode failed"}'
        )
        unrelated = RuntimeError('Timeout: sendMsg EventRet: {"result": 0}')
        opaque_success = RuntimeError("ActionFailed")
        opaque_success.wording = (
            'Timeout: recallMsg EventRet: {"result": 0, "errMsg": ""}'
        )
        self.assertTrue(self.recall_timeout_reported_success(success))
        self.assertTrue(self.recall_timeout_reported_success(opaque_success))
        self.assertFalse(self.recall_timeout_reported_success(failure))
        self.assertFalse(self.recall_timeout_reported_success(unrelated))
        with patch.dict(sys.modules, self.modules):
            self.assertTrue(self.recall._recall_timeout_reported_success(opaque_success))
            self.assertFalse(self.recall._recall_timeout_reported_success(failure))

    def test_empty_reply_command_is_ignored(self):
        handle_reply_commands = _load_reply_command_method()

        async def scenario():
            return [
                item
                async for item in handle_reply_commands(
                    object(),
                    object(),
                    "",
                    "   ",
                )
            ]

        self.assertEqual([], asyncio.run(scenario()))

    def test_recall_command_payload_parsing_does_not_use_empty_separator(self):
        source = MAIN_PATH.read_text(encoding="utf-8")
        # An empty separator raises ValueError in str.find and made sensitive
        # word/recall notification commands silently fail at runtime.
        self.assertNotIn("message.find('')", source)
        self.assertNotIn('message.find("")', source)

    def test_recall_scope_and_max_count_are_persisted(self):
        with patch.dict(sys.modules, self.modules):
            self.assertEqual(self.recall.get_recall_member_scope(self.GROUP_ID), "all")
            self.assertEqual(self.recall.set_recall_member_scope(self.GROUP_ID, "普通成员"), "members")
            self.assertEqual(self.recall.get_recall_member_scope(self.GROUP_ID), "members")
            self.assertEqual(self.recall.set_max_recall_count(self.GROUP_ID, 9), 9)
            self.assertEqual(self.recall.get_max_recall_count(self.GROUP_ID), 9)

    def test_explicit_member_scope_overrides_legacy_admin_flag(self):
        configurations = {
            self.GROUP_ID: {
                "features": {
                    "recall": {
                        "enable_admin_recall": True,
                    }
                }
            }
        }
        self.recall, self.modules = _load_recall_module(configurations)
        with patch.dict(sys.modules, self.modules):
            self.assertEqual(
                self.recall.set_recall_member_scope(self.GROUP_ID, "普通成员"),
                "members",
            )
            self.assertEqual(self.recall.get_recall_member_scope(self.GROUP_ID), "members")
            self.assertFalse(
                configurations[self.GROUP_ID]["features"]["recall"]["enable_admin_recall"]
            )

    def test_recall_skips_old_and_admin_messages_but_continues_after_api_failure(self):
        now = int(__import__("time").time())
        messages = [
            {"message_id": 1, "time": now - 1000, "sender": {"user_id": "member"}},
            {"message_id": 2, "time": now, "sender": {"user_id": "admin"}},
            {"message_id": 3, "time": now, "sender": {"user_id": "member"}},
            {"message_id": 4, "time": now, "sender": {"user_id": "member"}},
        ]

        async def scenario():
            bot = _HistoryBot(messages, members=[{"user_id": "admin", "role": "admin"}], failures={4})
            result = await self.recall.recall_messages(
                _RecallEvent(bot), count=3, exclude_admins=True, max_age_seconds=60
            )
            return bot, result

        with patch.dict(sys.modules, self.modules):
            bot, result = asyncio.run(scenario())
        self.assertEqual(result["skipped_admin"], 1)
        self.assertEqual(result["skipped_old"], 1)
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["requested"], 3)
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["unprocessed"], 1)
        self.assertEqual(bot.deleted_message_ids, [3])

    def test_manual_recall_without_explicit_limit_attempts_old_messages(self):
        now = int(__import__("time").time())
        messages = [
            {"message_id": 31, "time": now - (8 * 86400), "sender": {"user_id": "member"}},
            {"message_id": 32, "time": now, "sender": {"user_id": "member"}},
        ]
        self.recall.set_recall_time(self.GROUP_ID, 1)

        async def scenario():
            bot = _HistoryBot(messages)
            result = await self.recall.recall_messages(
                _RecallEvent(bot), count=2, exclude_admins=False
            )
            return bot, result

        with patch.dict(sys.modules, self.modules):
            bot, result = asyncio.run(scenario())
        self.assertEqual(result["success"], 2)
        self.assertEqual(result["skipped_old"], 0)
        self.assertEqual(bot.deleted_message_ids, [32, 31])

    def test_targeted_admin_message_obeys_exclude_admins_flag(self):
        now = int(__import__("time").time())
        messages = [{"message_id": 9, "time": now, "sender": {"user_id": "admin"}}]

        async def scenario(exclude):
            bot = _HistoryBot(messages, members=[{"user_id": "admin", "role": "admin"}])
            result = await self.recall.recall_user_messages_by_api(
                _RecallEvent(bot), "admin", 1, exclude_admins=exclude
            )
            return bot, result

        with patch.dict(sys.modules, self.modules):
            excluded_bot, excluded = asyncio.run(scenario(True))
            allowed_bot, allowed = asyncio.run(scenario(False))
        self.assertEqual(excluded["success"], 0)
        self.assertEqual(excluded["skipped_admin"], 1)
        self.assertEqual(allowed["success"], 1)
        self.assertEqual(allowed_bot.deleted_message_ids, [9])

    def test_recall_treats_explicit_platform_failure_as_failed(self):
        now = int(__import__("time").time())
        messages = [
            {"message_id": 21, "time": now, "sender": {"user_id": "member"}},
            {"message_id": 22, "time": now, "sender": {"user_id": "member"}},
        ]

        async def scenario():
            bot = _HistoryBot(messages, api_failures={21})
            result = await self.recall.recall_messages(
                _RecallEvent(bot), count=2, exclude_admins=False, max_age_seconds=60
            )
            return bot, result

        with patch.dict(sys.modules, self.modules):
            bot, result = asyncio.run(scenario())
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(bot.deleted_message_ids, [22])

    def test_single_recall_count_is_handled_as_a_recall_request(self):
        source = MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn("count_specified = False", source)
        self.assertIn("elif is_all or count_specified:", source)

    def test_manual_bot_recall_uses_bounded_outbound_cache_when_switch_is_off(self):
        source = MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn("manual_recall_command", source)
        self.assertIn('"机器人消息"', source)
        self.assertIn("get_cached_messages", source)
        self.assertIn('direction != "outbound" and sender_role != "bot"', source)
        self.assertIn('"reason": "平台未确认"', source)

    def test_event_message_id_falls_back_to_onebot_raw_message(self):
        get_message_id = _load_message_id_method()
        event = types.SimpleNamespace(
            message_obj=types.SimpleNamespace(
                raw_message={"message_id": 987654, "message": "广告"}
            )
        )
        self.assertEqual(get_message_id(event), 987654)

    def test_event_self_id_falls_back_to_message_object(self):
        get_self_id = _load_event_self_id_method()
        plugin = types.SimpleNamespace(
            _normalize_positive_id=lambda value: str(value or "").strip()
            if str(value or "").strip().isdigit()
            else ""
        )
        event = types.SimpleNamespace(
            message_obj=types.SimpleNamespace(self_id="1986369935")
        )
        self.assertEqual(get_self_id(plugin, event), "1986369935")

    def test_bot_sender_uses_login_info_when_event_self_id_is_stale(self):
        is_event_from_bot = _load_event_bot_method()

        class LoginBot:
            def __init__(self):
                self.calls = 0

            async def call_action(self, *, action):
                self.assertEqual(action, "get_login_info")
                self.calls += 1
                return {"data": {"user_id": "1986369935"}}

            def assertEqual(self, left, right):
                if left != right:
                    raise AssertionError((left, right))

        bot = LoginBot()
        plugin = types.SimpleNamespace(
            _normalize_positive_id=lambda value: str(value or "").strip()
            if str(value or "").strip().isdigit()
            else "",
            _get_event_self_id=lambda event: "999999",
            _get_sender_id_from_event=lambda event: "1986369935",
            _bot_self_id_cache="",
            _bot_self_id_cache_checked_at=0.0,
            _bot_self_id_cache_ttl=300,
        )
        event = types.SimpleNamespace(
            bot=bot,
            message_obj=types.SimpleNamespace(self_id="999999", raw_message={}),
        )

        async def scenario():
            first = await is_event_from_bot(plugin, event, "1986369935")
            second = await is_event_from_bot(plugin, event, "1986369935")
            return first, second

        self.assertEqual(asyncio.run(scenario()), (True, True))
        self.assertEqual(bot.calls, 1)


if __name__ == "__main__":
    unittest.main()
