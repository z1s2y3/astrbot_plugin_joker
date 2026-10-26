import asyncio
import base64
import importlib.util
import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"reply-image-test"


def _load_reply_module():
    package_name = "_reply_image_test_package"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).parent / "qqadmin")]
    sys.modules[package_name] = package

    previous_modules = {name: sys.modules.get(name) for name in ("astrbot", "astrbot.api")}
    api = ModuleType("astrbot.api")
    api.logger = logging.getLogger("test-reply-image-cache")
    astrbot = ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

    state = {"replies": {}}
    data_root = Path(tempfile.gettempdir())
    group_config = ModuleType(f"{package_name}.group_config")
    group_config.get_group_data = lambda _gid, key, default=None: state.get(key, default)
    group_config.set_group_data = lambda _gid, key, value: state.__setitem__(key, value)
    group_config.update_group_data = lambda _gid, key, update: state.__setitem__(
        key, update(state.get(key))
    ) or state[key]
    group_config.batch_update_group_data = lambda _gid, updates: [
        state.__setitem__(key, update(state.get(key))) for key, update in updates
    ]
    group_config.get_plugin_data_path = lambda: data_root
    sys.modules[group_config.__name__] = group_config

    module_path = Path(__file__).parent / "qqadmin" / "reply.py"
    spec = importlib.util.spec_from_file_location(f"{package_name}.reply", module_path)
    module = importlib.util.module_from_spec(spec)
    module._test_state = state
    module._test_group_config = group_config
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        for name, previous in previous_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module


reply = _load_reply_module()


class ReplyImageCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp_dir.name)
        reply._test_state.clear()
        reply._test_state["replies"] = {}
        reply._test_group_config.get_plugin_data_path = lambda: self.data_root
        reply._RUNTIME_COOLDOWNS.clear()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_remove_reply_reports_missing_rule_without_false_success(self):
        reply._test_state["replies"] = {
            "exact": {},
            "fuzzy": ["2"],
            "fuzzy_keywords": {"2": {"reply": "模糊答案", "type": "fuzzy"}},
            "regex": [],
        }

        self.assertFalse(reply.remove_reply("123", "2", "exact"))
        self.assertIn("2", reply._test_state["replies"]["fuzzy"])

    async def test_remove_reply_all_deletes_fuzzy_rule_and_its_payload(self):
        reply._test_state["replies"] = {
            "exact": {},
            "fuzzy": ["2"],
            "fuzzy_keywords": {"2": {"reply": "模糊答案", "type": "fuzzy"}},
            "regex": [],
        }

        self.assertTrue(reply.remove_reply("123", "2", "all"))
        self.assertNotIn("2", reply._test_state["replies"]["fuzzy"])
        self.assertNotIn("2", reply._test_state["replies"]["fuzzy_keywords"])

    async def test_remove_reply_all_removes_same_key_from_each_match_type(self):
        reply._test_state["replies"] = {
            "exact": {"重复": {"reply": "精确答案", "type": "exact"}},
            "fuzzy": ["重复"],
            "fuzzy_keywords": {"重复": {"reply": "模糊答案", "type": "fuzzy"}},
            "regex": [{"pattern": "重复", "reply": {"reply": "正则答案"}}],
        }

        self.assertTrue(reply.remove_reply("123", "重复", "all"))
        self.assertEqual(reply._test_state["replies"]["exact"], {})
        self.assertEqual(reply._test_state["replies"]["fuzzy"], [])
        self.assertEqual(reply._test_state["replies"]["fuzzy_keywords"], {})
        self.assertEqual(reply._test_state["replies"]["regex"], [])

    async def test_ordered_reply_entries_match_display_and_numeric_delete_order(self):
        reply._test_state["replies"] = {
            "exact": {
                "精确一": {"reply": "a", "type": "exact"},
                "精确二": {"reply": "b", "type": "exact"},
            },
            "fuzzy": ["模糊一"],
            "fuzzy_keywords": {
                "模糊一": {"reply": "c", "type": "fuzzy"},
                "模糊二": {"reply": "d", "type": "fuzzy"},
            },
            "regex": [
                {"pattern": r"正则.+", "reply": {"reply": "e"}},
            ],
        }

        entries = list(reply.iter_reply_entries("123"))
        self.assertEqual(
            [(index, item[0], item[1]) for index, item in enumerate(entries, 1)],
            [
                (1, "exact", "精确一"),
                (2, "exact", "精确二"),
                (3, "fuzzy", "模糊一"),
                (4, "fuzzy", "模糊二"),
                (5, "regex", r"正则.+"),
            ],
        )
        self.assertEqual(reply.get_reply_by_index("123", 4), ("fuzzy", "模糊二"))
        self.assertIsNone(reply.get_reply_by_index("123", 0))
        self.assertIsNone(reply.get_reply_by_index("123", 99))

        # The entry selected by ``/删除 4`` is a fuzzy rule, so deleting it
        # must remove both its display key and payload (not report a false
        # success against the exact map).
        selected = entries[3]
        self.assertTrue(reply.remove_reply("123", selected[1], selected[0]))
        self.assertNotIn("模糊二", reply._test_state["replies"]["fuzzy_keywords"])

    async def test_legacy_mapping_containers_are_listed_and_removed(self):
        reply._test_state["replies"] = {
            "exact": {},
            "fuzzy": {"旧模糊": {"reply": "f"}},
            "regex": {r"旧正则": {"reply": "r"}},
        }
        self.assertEqual(reply.list_replies("123")["fuzzy"], ["旧模糊"])
        self.assertEqual(reply.list_replies("123")["regex"], [r"旧正则"])
        self.assertTrue(reply.remove_reply("123", "旧模糊", "fuzzy"))
        self.assertTrue(reply.remove_reply("123", r"旧正则", "regex"))
        self.assertEqual(reply._test_state["replies"]["fuzzy"], {})
        self.assertEqual(reply._test_state["replies"]["regex"], {})

    async def test_base64_image_is_cached_and_resolved_locally(self):
        source = "base64://" + base64.b64encode(PNG_BYTES).decode()
        self.assertTrue(
            reply.add_reply_with_images(
                "123", "地图", [source], operator_id="9", reply_text="地图如下"
            )
        )

        result = await reply.download_and_persist_images("123", "地图", [source], True)
        entry = reply._test_state["replies"]["exact"]["地图"]
        resolved = reply.resolve_reply_images(entry)

        self.assertEqual(result, {"cached": 1, "failed": 0, "total": 1})
        self.assertEqual(entry["image_cache_status"], "ready")
        self.assertEqual(entry["reply"], "地图如下")
        self.assertEqual(len(resolved), 1)
        self.assertTrue(Path(resolved[0]).is_file())
        self.assertEqual(Path(resolved[0]).read_bytes(), PNG_BYTES)

    async def test_identical_images_share_one_content_hash_file(self):
        first = "base64://" + base64.b64encode(PNG_BYTES).decode()
        second = "base64://" + base64.b64encode(PNG_BYTES).decode()
        reply.add_reply_with_images("123", "重复", [first, second])

        await reply.download_and_persist_images("123", "重复", [first, second], True)
        entry = reply._test_state["replies"]["exact"]["重复"]
        cached_files = list((self.data_root / "images" / "123").glob("*"))

        self.assertEqual(len(cached_files), 1)
        self.assertEqual(entry["image_cache_refs"][0], entry["image_cache_refs"][1])
        self.assertEqual(len(reply.resolve_reply_images(entry)), 2)

    async def test_replacing_image_removes_unreferenced_cache_file(self):
        first_bytes = PNG_BYTES + b"-first"
        second_bytes = PNG_BYTES + b"-second"
        first = "base64://" + base64.b64encode(first_bytes).decode()
        second = "base64://" + base64.b64encode(second_bytes).decode()

        reply.add_reply_with_images("123", "轮播", [first])
        await reply.download_and_persist_images("123", "轮播", [first], True)
        images_dir = self.data_root / "images" / "123"
        first_files = list(images_dir.glob("*"))
        self.assertEqual(len(first_files), 1)

        reply.add_reply_with_images("123", "轮播", [second])
        await reply.download_and_persist_images("123", "轮播", [second], True)
        remaining = list(images_dir.glob("*"))

        self.assertEqual(len(remaining), 1)
        self.assertNotEqual(remaining[0].name, first_files[0].name)
        self.assertEqual(remaining[0].read_bytes(), second_bytes)

    async def test_missing_cache_falls_back_to_original_url(self):
        entry = {
            "has_images": True,
            "image_urls": ["cache://images/123/missing.png"],
            "image_cache_refs": ["cache://images/123/missing.png"],
            "image_source_urls": ["https://example.invalid/original.png"],
        }
        self.assertEqual(
            reply.resolve_reply_images(entry),
            ["https://example.invalid/original.png"],
        )

    async def test_cache_mode_never_resends_missing_temporary_url(self):
        entry = {
            "has_images": True,
            "image_cache_status": "pending",
            "image_urls": ["https://example.invalid/temporary.png"],
            "image_cache_refs": [""],
            "image_source_urls": ["https://example.invalid/temporary.png"],
        }

        self.assertEqual(
            reply.resolve_reply_images(entry, allow_remote_fallback=False),
            [],
        )

    async def test_cache_disabled_keeps_original_sources_without_pending_state(self):
        source = "https://example.invalid/original.png"
        self.assertTrue(
            reply.add_reply_with_images(
                "123", "原图", [source], cache_enabled=False
            )
        )
        entry = reply._test_state["replies"]["exact"]["原图"]

        self.assertEqual(entry["image_cache_status"], "disabled")
        self.assertEqual(entry["image_cache_refs"], [""])
        self.assertEqual(reply.resolve_reply_images(entry), [source])

    async def test_uploaded_image_rule_does_not_send_its_text_segment(self):
        self.assertFalse(
            reply.should_include_reply_text(
                {
                    "reply": "发图",
                    "has_images": True,
                    "image_source_urls": ["https://example.invalid/reply.webp"],
                }
            )
        )
        self.assertFalse(
            reply.should_include_reply_text(
                {
                    "reply": "发图",
                    "image_portal_assets": [{"id": 1, "sha256": "a" * 64}],
                }
            )
        )

    async def test_text_only_rule_keeps_its_text_segment(self):
        self.assertTrue(reply.should_include_reply_text({"reply": "你好"}))

    async def test_reply_probability_and_maximum_uses_are_independent(self):
        self.assertFalse(reply.should_trigger_reply({"probability": 0}))
        self.assertTrue(reply.should_trigger_reply({"probability": 100}))
        with patch.object(reply.random, "random", return_value=0.49):
            self.assertTrue(reply.should_trigger_reply({"probability": 50}))
        with patch.object(reply.random, "random", return_value=0.50):
            self.assertFalse(reply.should_trigger_reply({"probability": 50}))

        rule = {"type": "exact", "max_uses": 2}
        self.assertEqual(reply.claim_reply_use("123", "限次", rule), (True, 1))
        self.assertEqual(reply.claim_reply_use("123", "限次", rule), (True, 2))
        self.assertEqual(reply.claim_reply_use("123", "限次", rule), (False, 2))

    async def test_reply_template_keeps_unknown_tokens(self):
        text = reply.render_reply_template(
            "{name}|{qq}|{group}|{keyword}|{count}|{unknown}",
            user_name="测试用户",
            user_id="10001",
            group_id="20002",
            keyword="签到",
            use_count=3,
            now=0,
        )
        self.assertEqual(text, "测试用户|10001|20002|签到|3|{unknown}")

    async def test_runtime_cache_limits_are_clamped(self):
        source = "base64://" + base64.b64encode(PNG_BYTES).decode()
        reply.add_reply_with_images("123", "安全边界", [source])

        result = await reply.download_and_persist_images(
            "123",
            "安全边界",
            [source],
            max_image_bytes=10**12,
            download_concurrency=999,
        )

        self.assertEqual(result, {"cached": 1, "failed": 0, "total": 1})

    async def test_local_onebot_source_can_keep_original_source_identity(self):
        original_source = "https://example.invalid/temporary.png"
        local_source = self.data_root / "onebot-image.png"
        local_source.write_bytes(PNG_BYTES)
        reply.add_reply_with_images("123", "本地优先", [original_source])

        result = await reply.download_and_persist_images(
            "123",
            "本地优先",
            [str(local_source)],
            True,
            source_identity=[original_source],
        )
        entry = reply._test_state["replies"]["exact"]["本地优先"]

        self.assertEqual(result, {"cached": 1, "failed": 0, "total": 1})
        self.assertEqual(entry["image_source_urls"], [original_source])
        self.assertEqual(len(reply.resolve_reply_images(entry)), 1)

    async def test_portal_reply_asset_is_verified_and_cached_without_a_url(self):
        digest = __import__("hashlib").sha256(PNG_BYTES).hexdigest()
        reply._test_state["replies"] = {
            "exact": {
                "网页图片": {
                    "reply": "图片已准备",
                    "type": "exact",
                    "image_portal_assets": [{"id": 77, "sha256": digest}],
                }
            },
            "fuzzy": [],
            "fuzzy_keywords": {},
            "regex": [],
        }
        requested = []

        async def fetch_asset(asset_id):
            requested.append(asset_id)
            return PNG_BYTES, "image/png"

        result = await reply.cache_portal_reply_assets("123", fetch_asset)
        entry = reply._test_state["replies"]["exact"]["网页图片"]
        resolved = reply.resolve_reply_images(entry, allow_remote_fallback=False)

        self.assertEqual(result, {"rules": 1, "cached": 1, "failed": 0})
        self.assertEqual(requested, [77])
        self.assertEqual(entry["image_cache_status"], "ready")
        self.assertTrue(entry["image_cache_refs"][0].startswith("cache://images/123/"))
        self.assertEqual(len(resolved), 1)
        self.assertEqual(Path(resolved[0]).read_bytes(), PNG_BYTES)
        self.assertNotIn("http", str(entry["image_portal_assets"]))

    async def test_portal_reply_asset_hash_mismatch_never_becomes_a_reply_image(self):
        digest = __import__("hashlib").sha256(PNG_BYTES).hexdigest()
        reply._test_state["replies"] = {
            "exact": {
                "错误图片": {
                    "reply": "不会发送错误文件",
                    "type": "exact",
                    "image_portal_assets": [{"id": 88, "sha256": digest}],
                }
            },
            "fuzzy": [],
            "fuzzy_keywords": {},
            "regex": [],
        }

        async def fetch_asset(_asset_id):
            return PNG_BYTES + b"tampered", "image/png"

        result = await reply.cache_portal_reply_assets("123", fetch_asset)
        entry = reply._test_state["replies"]["exact"]["错误图片"]

        self.assertEqual(result, {"rules": 1, "cached": 0, "failed": 1})
        self.assertEqual(entry["image_cache_status"], "failed")
        self.assertEqual(reply.resolve_reply_images(entry, allow_remote_fallback=False), [])
        images_dir = self.data_root / "images" / "123"
        self.assertEqual(list(images_dir.glob("*")) if images_dir.exists() else [], [])

    async def test_ready_portal_asset_check_does_not_rewrite_group_configuration(self):
        digest = __import__("hashlib").sha256(PNG_BYTES).hexdigest()
        reply._test_state["replies"] = {
            "exact": {
                "不重复写入": {
                    "reply": "ok",
                    "type": "exact",
                    "image_portal_assets": [{"id": 99, "sha256": digest}],
                }
            },
            "fuzzy": [],
            "fuzzy_keywords": {},
            "regex": [],
        }

        async def fetch_asset(_asset_id):
            return PNG_BYTES, "image/png"

        await reply.cache_portal_reply_assets("123", fetch_asset)
        calls = []
        original_update = reply._test_group_config.update_group_data

        def counted_update(group_id, key, update):
            calls.append((group_id, key))
            return original_update(group_id, key, update)

        reply._test_group_config.update_group_data = counted_update
        try:
            second = await reply.cache_portal_reply_assets("123", fetch_asset)
        finally:
            reply._test_group_config.update_group_data = original_update

        self.assertEqual(second, {"rules": 1, "cached": 1, "failed": 0})
        self.assertEqual(calls, [])

    async def test_cooldown_hot_path_does_not_read_group_data(self):
        reply._RUNTIME_COOLDOWNS.clear()
        with patch.object(reply, "get_group_replies", side_effect=AssertionError("disk read")):
            reply.set_cooldown("123", "地图", "9", 30)
            self.assertTrue(reply.is_in_cooldown("123", "地图", "9", 30))
            self.assertGreater(reply.get_cooldown_remaining("123", "9", 30), 0)

    async def test_regex_patterns_are_compiled_once(self):
        replies = {
            "exact": {},
            "fuzzy": [],
            "regex": [{"pattern": r"服[务務]器", "reply": {"reply": "ok"}}],
        }
        reply._compiled_reply_pattern.cache_clear()
        for _ in range(5):
            keyword, data = reply.match_any_reply("123", "查询服务器", replies)
            self.assertEqual(keyword, "服务器")
            self.assertEqual(data["reply"], "ok")
        self.assertEqual(reply._compiled_reply_pattern.cache_info().misses, 1)
        self.assertEqual(reply._compiled_reply_pattern.cache_info().hits, 4)


if __name__ == "__main__":
    unittest.main()
