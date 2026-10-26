import ast
import asyncio
import copy
import contextlib
import importlib.util
import io
import logging
import re
import textwrap
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parent / "qqadmin" / "console_log.py"
MAIN_PATH = Path(__file__).resolve().parent / "main.py"
SPEC = importlib.util.spec_from_file_location("joker_console_log", MODULE_PATH)
CONSOLE_LOG = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CONSOLE_LOG)


def _load_main_send_method(method_name: str):
    """Compile a small main.py send wrapper without importing AstrBot."""
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    method_node = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MyPlugin":
            method_node = next(
                (
                    child
                    for child in node.body
                    if isinstance(child, ast.AsyncFunctionDef)
                    and child.name == method_name
                ),
                None,
            )
            break
    assert method_node is not None, f"MyPlugin.{method_name} was not found"

    namespace = {
        "AstrMessageEvent": object,
        "logger": logging.getLogger("test_console_log.send_wrapper"),
        "re": re,
        "textwrap": textwrap,
    }
    helper_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_compact_reply_text"
    )
    extracted = ast.Module(
        body=[copy.deepcopy(helper_node), copy.deepcopy(method_node)],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(extracted), str(MAIN_PATH), "exec"), namespace)
    return namespace[method_name]


class ConsoleLogTests(unittest.TestCase):
    def test_runtime_modules_do_not_use_info_or_direct_print(self):
        root = Path(__file__).resolve().parent
        runtime_files = [root / "main.py", *(root / "qqadmin").glob("*.py")]
        for path in runtime_files:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("logger.info", source, path.name)
            if path.name != "console_log.py":
                self.assertNotIn("print(", source, path.name)

    def test_plain_text_is_shown_in_cmd_but_not_persisted(self):
        secret = "这段群消息正文应显示在CMD"
        self.assertEqual(CONSOLE_LOG.summarize_group_message(secret), "文本")

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "console_events.jsonl"
            with patch.object(CONSOLE_LOG, "_event_file", return_value=event_path):
                with contextlib.redirect_stdout(output):
                    CONSOLE_LOG.print_group_action(
                        "发送", "1102579807", message=secret, message_id=123456
                    )
                events = CONSOLE_LOG.get_console_events()

        line = output.getvalue().strip()
        self.assertIn("类型:文本", line)
        self.assertIn(f"内容:{secret}", line)
        self.assertIn("消息:123456", line)
        self.assertNotIn(secret, str(events))

    def test_mixed_segments_show_content_without_asset_source(self):
        image_url = "https://example.invalid/private-image.png"
        message = [
            {"type": "text", "data": {"text": "私密正文"}},
            {"type": "image", "data": {"file": image_url}},
            {"type": "face", "data": {"id": "14"}},
            {"type": "image", "data": {"file": image_url}},
        ]

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "console_events.jsonl"
            with patch.object(CONSOLE_LOG, "_event_file", return_value=event_path):
                with contextlib.redirect_stdout(output):
                    CONSOLE_LOG.print_group_action("发送", 568586484, message=message)

        line = output.getvalue().strip()
        self.assertIn("类型:文本+图片+表情", line)
        self.assertIn("内容:私密正文[图片][表情:14][图片]", line)
        self.assertNotIn(image_url, line)

    def test_astrbot_message_chain_is_rendered_for_cmd(self):
        class Plain:
            def __init__(self, text):
                self.text = text

        class At:
            def __init__(self, qq):
                self.qq = qq

        class Image:
            pass

        class MessageChain:
            chain = [Plain("你好\n世界"), At(2650362143), Image()]

        message = MessageChain()
        self.assertEqual(CONSOLE_LOG.summarize_group_message(message), "文本+艾特+图片")
        self.assertEqual(
            CONSOLE_LOG.render_group_message_for_console(message),
            "你好\\n世界@2650362143[图片]",
        )

    def test_direct_event_send_is_logged_for_a_command_event(self):
        class Sender:
            def __init__(self):
                self.logged = []

            def _can_owner_bypass_group_authorization(self, event):
                return False

            def _can_send_event_message(self, event):
                return True

            def _get_group_id_from_event(self, event):
                return "1102579807"

            def _log_send(self, group_id, message):
                self.logged.append((group_id, message))

            @staticmethod
            def _chain_has_content(chain):
                return bool(chain)

        class Event:
            message_str = "/普通命令"

            def __init__(self):
                self.sent = []

            @staticmethod
            def plain_result(text):
                return ("plain", text)

            @staticmethod
            def chain_result(chain):
                return ("chain", chain)

            async def send(self, result):
                self.sent.append(result)

        async def scenario():
            sender = Sender()
            event = Event()
            await _load_main_send_method("_send_plain")(sender, event, "命令回复")
            await _load_main_send_method("_send_chain")(sender, event, ["链式回复"])
            return sender, event

        sender, event = asyncio.run(scenario())
        self.assertEqual(
            sender.logged,
            [("1102579807", "命令回复"), ("1102579807", ["链式回复"])],
        )
        self.assertEqual(
            event.sent,
            [("plain", "命令回复"), ("chain", ["链式回复"])],
        )

    def test_runtime_event_is_sanitized_and_has_no_message_content(self):
        secret = "不得同步的聊天正文"
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "console_events.jsonl"
            with patch.object(CONSOLE_LOG, "_event_file", return_value=event_path):
                CONSOLE_LOG.print_group_action(
                    "自动回复",
                    "1102579807",
                    message=[
                        {"type": "text", "data": {"text": secret}},
                        {"type": "image", "data": {"file": "private-image-url"}},
                    ],
                    user_id="123456",
                    message_id="654321",
                    detail="命中规则",
                )
                events = CONSOLE_LOG.get_console_events()

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["group_id"], "1102579807")
        self.assertEqual(event["message_type"], "文本+图片")
        self.assertEqual(event["detail"], "命中规则")
        self.assertNotIn(secret, str(event))
        self.assertNotIn("private-image-url", str(event))

    def test_sender_name_is_printed_and_kept_in_the_sync_event(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "console_events.jsonl"
            with patch.object(CONSOLE_LOG, "_event_file", return_value=event_path):
                with contextlib.redirect_stdout(output):
                    CONSOLE_LOG.print_group_action(
                        "receive",
                        "1102579807",
                        message="hello",
                        user_id="100001",
                        user_name="Alice",
                    )
                events = CONSOLE_LOG.get_console_events()

        self.assertIn("昵称:Alice", output.getvalue())
        self.assertEqual("100001", events[0]["user_id"])
        self.assertEqual("Alice", events[0]["user_name"])

    def test_persisted_group_send_wakes_listener_without_message_content(self):
        secret = "这段群消息正文不能传给同步唤醒回调"
        notifications = []
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "console_events.jsonl"
            with patch.object(CONSOLE_LOG, "_event_file", return_value=event_path):
                token = CONSOLE_LOG.set_console_event_listener(
                    lambda: notifications.append("woken")
                )
                try:
                    CONSOLE_LOG.print_group_action(
                        "发送", "1102579807", message=secret, message_id="123456"
                    )
                finally:
                    CONSOLE_LOG.clear_console_event_listener(token)

        self.assertEqual(notifications, ["woken"])
        self.assertNotIn(secret, str(notifications))

    def test_portal_message_tracking_persists_safe_preview_only_after_opt_in(self):
        image_url = "https://example.invalid/private-image.png"
        card = "JK30-1234567890123ABCDEF123456"
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "console_events.jsonl"
            with patch.object(CONSOLE_LOG, "_event_file", return_value=event_path):
                CONSOLE_LOG.set_portal_message_content_enabled(True)
                try:
                    CONSOLE_LOG.print_group_action(
                        "接收",
                        "1102579807",
                        message=[
                            {"type": "text", "data": {"text": f"你好 {card}"}},
                            {"type": "image", "data": {"file": image_url}},
                        ],
                        user_id="123456",
                        message_id="654321",
                        direction="inbound",
                    )
                    events = CONSOLE_LOG.get_console_events()
                finally:
                    CONSOLE_LOG.set_portal_message_content_enabled(False)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["direction"], "inbound")
        self.assertIn("你好", events[0]["message_content"])
        self.assertIn("[图片]", events[0]["message_content"])
        self.assertNotIn(image_url, events[0]["message_content"])
        self.assertNotIn(card, events[0]["message_content"])

    def test_invalid_group_id_does_not_wake_listener(self):
        notifications = []
        token = CONSOLE_LOG.set_console_event_listener(
            lambda: notifications.append("woken")
        )
        try:
            CONSOLE_LOG.print_group_action("发送", "not-a-group", message="hello")
        finally:
            CONSOLE_LOG.clear_console_event_listener(token)

        self.assertEqual(notifications, [])


if __name__ == "__main__":
    unittest.main()
