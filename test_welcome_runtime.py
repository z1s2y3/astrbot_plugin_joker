"""Focused regression coverage for portal-backed welcome keyword replies."""

from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType


def _load_welcome_module():
    package_name = "_welcome_runtime_test_package"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).parent / "qqadmin")]
    sys.modules[package_name] = package

    state: dict[str, object] = {}
    group_config = ModuleType(f"{package_name}.group_config")
    group_config.get_group_data = lambda _group_id, key, default=None: state.get(key, default)
    group_config.set_group_data = lambda _group_id, key, value: state.__setitem__(key, value)
    group_config.update_group_data = lambda _group_id, key, update: state.__setitem__(
        key, update(state.get(key))
    ) or state[key]

    def batch_update(_group_id, updates):
        for key, update in updates:
            state[key] = update(state.get(key))
        return state

    group_config.batch_update_group_data = batch_update
    sys.modules[group_config.__name__] = group_config

    module_path = Path(__file__).parent / "qqadmin" / "welcome.py"
    spec = importlib.util.spec_from_file_location(f"{package_name}.welcome", module_path)
    module = importlib.util.module_from_spec(spec)
    module._test_state = state
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


welcome = _load_welcome_module()


class WelcomeRuntimeTests(unittest.TestCase):
    def setUp(self):
        welcome._test_state.clear()

    def test_portal_feature_fields_trigger_keyword_reply(self):
        welcome._test_state["features"] = {
            "welcome": {
                "enabled": True,
                "auto_reply_enabled": True,
                "keywords": ["Hello", "欢迎"],
                "message": "欢迎 {user_name} 加入本群！",
            }
        }

        self.assertTrue(welcome.should_send_welcome("1102579807", "say HELLO now"))
        self.assertTrue(welcome.should_send_welcome("1102579807", "欢迎大家"))
        self.assertEqual(
            "欢迎 {user_name} 加入本群！",
            welcome.get_welcome_message("1102579807"),
        )

    def test_keyword_reply_requires_its_own_switch(self):
        welcome._test_state["features"] = {
            "welcome": {
                "enabled": True,
                "auto_reply_enabled": False,
                "keywords": ["欢迎"],
            }
        }

        self.assertFalse(welcome.should_send_welcome("1102579807", "欢迎"))

    def test_legacy_root_mirrors_remain_supported(self):
        welcome._test_state.update(
            {
                "welcome_enabled": True,
                "welcome_auto_reply_enabled": True,
                "welcome_keywords": "hello\n欢迎",
            }
        )

        self.assertTrue(welcome.should_send_welcome("1102579807", "HELLO"))
        self.assertEqual(["hello", "欢迎"], welcome.get_welcome_keywords("1102579807"))

    def test_portal_feature_values_win_over_stale_legacy_mirrors(self):
        # A group file can contain root aliases written by an older Windows
        # build.  The web editor writes the nested feature projection, which
        # must be the runtime source of truth even when the aliases disagree.
        welcome._test_state.update(
            {
                "welcome_enabled": False,
                "welcome_auto_reply_enabled": False,
                "welcome_keywords": ["旧关键词"],
                "welcome_message": "旧欢迎语",
                "features": {
                    "welcome": {
                        "enabled": True,
                        "auto_reply_enabled": True,
                        "keywords": ["新关键词"],
                        "message": "新欢迎语",
                    }
                },
            }
        )

        self.assertTrue(welcome.should_send_welcome("1102579807", "新关键词"))
        self.assertFalse(welcome.should_send_welcome("1102579807", "旧关键词"))
        self.assertEqual("新欢迎语", welcome.get_welcome_message("1102579807"))

    def test_older_nested_welcome_message_alias_remains_readable(self):
        welcome._test_state["features"] = {
            "welcome": {"welcome_message": "旧网页欢迎语"}
        }
        self.assertEqual("旧网页欢迎语", welcome.get_welcome_message("1102579807"))

    def test_setters_update_nested_projection_and_legacy_mirror(self):
        welcome.set_welcome_keywords("1102579807", ["图片", "欢迎"])
        welcome.set_welcome_message("1102579807", "欢迎 {user_name}")
        welcome.enable_welcome("1102579807", True)

        feature = welcome._test_state["features"]["welcome"]
        self.assertEqual(["图片", "欢迎"], feature["keywords"])
        self.assertEqual("欢迎 {user_name}", feature["message"])
        self.assertTrue(feature["enabled"])
        self.assertEqual(["图片", "欢迎"], welcome._test_state["welcome_keywords"])
        self.assertEqual("欢迎 {user_name}", welcome._test_state["welcome_message"])
        self.assertTrue(welcome._test_state["welcome_enabled"])

    def test_group_message_handler_uses_welcome_and_image_reply_policies(self):
        tree = ast.parse(Path(__file__).with_name("main.py").read_text(encoding="utf-8"))
        plugin = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MyPlugin"
        )
        handler = next(
            node
            for node in plugin.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_auto_reply"
        )
        names = {
            node.func.id
            for node in ast.walk(handler)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        self.assertIn("should_send_welcome", names)
        self.assertIn("should_include_reply_text", names)

        # The keyword branch should return only after a non-empty welcome was
        # actually produced.  An empty portal message must fall through to
        # ordinary automatic replies instead of swallowing the incoming text.
        handler_source = ast.get_source_segment(
            Path(__file__).with_name("main.py").read_text(encoding="utf-8"), handler
        ) or ""
        self.assertIn("if welcome_msg:", handler_source)
        self.assertIn("                    return", handler_source)

    def test_verification_success_does_not_send_duplicate_welcome(self):
        tree = ast.parse(Path(__file__).with_name("main.py").read_text(encoding="utf-8"))
        plugin = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MyPlugin"
        )
        handlers = {
            node.name: node
            for node in plugin.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name in {"handle_direct_verification", "_handle_answer_verification"}
        }
        self.assertEqual(set(handlers), {"handle_direct_verification", "_handle_answer_verification"})
        for name, handler in handlers.items():
            duplicate_calls = [
                node for node in ast.walk(handler)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"_send_welcome_message", "_send_welcome_message_direct"}
            ]
            self.assertEqual([], duplicate_calls, name)


if __name__ == "__main__":
    unittest.main()
