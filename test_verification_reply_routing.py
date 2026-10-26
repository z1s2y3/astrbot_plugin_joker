"""Routing regressions for admission answers and SteamID binding prompts."""

import ast
import copy
import contextlib
import importlib.util
import io
import re
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
PLUGIN = next(
    node
    for node in TREE.body
    if isinstance(node, ast.ClassDef) and node.name == "MyPlugin"
)
METHODS = {
    node.name: node
    for node in PLUGIN.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}
CONSOLE_LOG_PATH = ROOT / "qqadmin" / "console_log.py"
CONSOLE_SPEC = importlib.util.spec_from_file_location(
    "joker_verification_console_log", CONSOLE_LOG_PATH
)
CONSOLE_LOG = importlib.util.module_from_spec(CONSOLE_SPEC)
assert CONSOLE_SPEC.loader is not None
CONSOLE_SPEC.loader.exec_module(CONSOLE_LOG)


class VerificationReplyRoutingTests(unittest.TestCase):
    def test_pending_math_answer_is_checked_before_auto_reply_matching(self):
        method = METHODS["handle_auto_reply"]
        body = ast.get_source_segment(SOURCE, method) or ""
        priority = next(
            keyword.value.value
            for decorator in method.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "event_message_type"
            for keyword in decorator.keywords
            if keyword.arg == "priority" and isinstance(keyword.value, ast.Constant)
        )

        self.assertEqual(11800, priority)
        self.assertIn('pending_mode in {"math", "id", "code", "direct", "steamid"}', body)
        self.assertLess(body.index("get_pending_verification"), body.index("match_any_reply"))

    def test_id_answers_use_configured_attempt_limit_and_do_not_echo_answer(self):
        body = ast.get_source_segment(SOURCE, METHODS["handle_direct_verification"]) or ""
        answer_branch = body[body.index('elif approval_mode == "id":'):]
        self.assertIn("max_attempts=max_attempts", answer_branch)
        self.assertIn("请发送验证 ID：{verification_id}", answer_branch)
        self.assertNotIn('hint = f"请输入你的验证ID: {correct_code}"', answer_branch)

        private_body = ast.get_source_segment(
            SOURCE, METHODS["_handle_answer_verification"]
        ) or ""
        private_id_branch = private_body[private_body.index('elif approval_mode == "id":'):]
        self.assertIn("max_attempts=max_attempts", private_id_branch)
        self.assertIn("请发送验证 ID：{verification_id}", private_id_branch)
        self.assertNotIn('hint = f"请输入你的验证ID: {correct_code}"', private_id_branch)

    def test_exhausted_group_verification_kicks_only_after_prompt_delivery(self):
        body = ast.get_source_segment(SOURCE, METHODS["handle_direct_verification"]) or ""
        failure_branch = body[body.index("current_attempts = increment_pending_attempts"):]

        self.assertIn('current_pending.get("verification_prompt_sent", False)', failure_branch)
        self.assertIn('current_pending.get("steamid_prompt_sent", False)', failure_branch)
        self.assertIn('action="set_group_kick"', failure_branch)
        self.assertIn("if kicked:", failure_branch)
        self.assertIn("add_operation_log", failure_branch)
        self.assertIn('"验证失败并移出"', failure_branch)
        self.assertIn('"踢出"', failure_branch)
        self.assertIn('"拉黑"', failure_branch)
        self.assertIn("should_kick_on_verification_failure", failure_branch)
        self.assertIn("未移出群聊", failure_branch)

    def test_expired_verification_is_kicked_only_after_a_delivered_prompt(self):
        body = ast.get_source_segment(SOURCE, METHODS["handle_direct_verification"]) or ""
        expiry_branch = body[body.index("is_verification_expired(group_id, user_id)"):]

        self.assertIn("kick_user_api", expiry_branch)
        self.assertIn('platform_id=self._get_event_platform_id(event)', expiry_branch)
        self.assertIn('remove_pending_request(group_id, user_id)', expiry_branch)
        self.assertIn('"验证超时"', expiry_branch)
        self.assertIn('"踢出"', expiry_branch)
        self.assertIn("should_kick_on_verification_failure", expiry_branch)

    def test_direct_verification_records_success_and_retry_failures(self):
        body = ast.get_source_segment(SOURCE, METHODS["handle_direct_verification"]) or ""
        success_branch = body[body.index('if is_correct:'):]
        self.assertIn('"验证通过"', success_branch)
        self.assertIn('"验证失败"', success_branch)
        self.assertIn('verification_mode_label', success_branch)

    def test_private_answer_path_ignores_undelivered_prompts(self):
        body = ast.get_source_segment(SOURCE, METHODS["_handle_answer_verification"]) or ""

        self.assertIn('pending.get("verification_prompt_sent", False)', body)
        self.assertIn('pending.get("steamid_prompt_sent", False)', body)
        self.assertIn("验证提示未确认发送，忽略 /回答验证", body)
        self.assertIn("is_verification_expired(group_id, user_id)", body)

    def test_group_steamid_prompt_requires_an_explicit_binding_command(self):
        body = ast.get_source_segment(
            SOURCE, METHODS["handle_group_steamid_privacy"]
        ) or ""

        gate = body.index("if not binding_command:")
        prompt = body.index("_prompt_steamid_submission(")
        self.assertLess(gate, prompt)
        self.assertIn("if not binding_command:\n            return", body)

    def test_group_request_keeps_searching_nested_onebot_request_fields(self):
        extractor = ast.get_source_segment(
            SOURCE, METHODS["_get_group_request_event_id"]
        ) or ""
        handler = ast.get_source_segment(
            SOURCE, METHODS["handle_group_request_auto_audit"]
        ) or ""

        self.assertIn('"request_data"', extractor)
        self.assertIn('"groupCode"', extractor)
        self.assertIn('"requester_uin"', extractor)
        self.assertIn("_resolve_missing_group_request_scope", handler)
        self.assertIn("未执行任何审核操作", handler)

    def test_nested_group_request_data_resolves_the_actual_group_id(self):
        """A request event must not confuse its applicant ID with a group ID."""

        class_body = [
            copy.deepcopy(METHODS["_normalize_positive_id"]),
            copy.deepcopy(METHODS["_get_group_request_event_id"]),
        ]
        module = ast.Module(
            body=[
                ast.ImportFrom(
                    module="__future__", names=[ast.alias(name="annotations")], level=0
                ),
                ast.ClassDef(
                    name="MyPlugin", bases=[], keywords=[], body=class_body, decorator_list=[]
                ),
            ],
            type_ignores=[],
        )
        namespace = {"re": re}
        exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), namespace)
        plugin = namespace["MyPlugin"]()
        event = types.SimpleNamespace(
            request_type="group",
            user_id="3693301461",
            request_data={"groupCode": "513920261", "requester_uin": "3693301461"},
        )
        raw_event = {"post_type": "request", "request_type": "group", "user_id": "3693301461"}

        self.assertEqual(
            "513920261",
            plugin._get_group_request_event_id(event, raw_event, kind="group"),
        )
        self.assertEqual(
            "3693301461",
            plugin._get_group_request_event_id(event, raw_event, kind="user"),
        )

    def test_successful_private_prompt_is_written_to_the_console(self):
        sender = ast.get_source_segment(SOURCE, METHODS["_send_private_text"]) or ""
        self.assertIn("print_private_action", sender)
        self.assertIn("report_private_send()", sender)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            CONSOLE_LOG.print_private_action(
                "发送", "12345", message="SteamID 私聊验证提示"
            )
        self.assertIn("[私聊] 发送", output.getvalue())
        self.assertIn("SteamID 私聊验证提示", output.getvalue())


if __name__ == "__main__":
    unittest.main()
