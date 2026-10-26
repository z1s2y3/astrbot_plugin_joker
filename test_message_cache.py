import importlib.util
import logging
import asyncio
import base64
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


def _load_message_cache_module():
    package_name = "_message_cache_test_package"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).parent / "qqadmin")]
    sys.modules[package_name] = package

    previous_modules = {
        name: sys.modules.get(name)
        for name in ("astrbot", "astrbot.api")
    }
    api = ModuleType("astrbot.api")
    api.logger = logging.getLogger("test-message-cache")
    astrbot = ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

    group_config = ModuleType(f"{package_name}.group_config")
    group_config.get_group_config_file = lambda group_id: Path(f"{group_id}.json")
    group_config.get_plugin_data_path = lambda: Path(tempfile.gettempdir())
    sys.modules[group_config.__name__] = group_config

    module_path = Path(__file__).parent / "qqadmin" / "message_cache.py"
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.message_cache", module_path
    )
    module = importlib.util.module_from_spec(spec)
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


message_cache = _load_message_cache_module()


class MessageCacheImageTests(unittest.TestCase):
    def _build_event(self):
        timestamp = int(time.time())
        raw_event = {
            "post_type": "message",
            "message_type": "group",
            "message_id": 778899,
            "time": timestamp,
            "sender": {
                "user_id": 2650362143,
                "nickname": "测试用户",
                "role": "member",
            },
            "message": [
                {"type": "text", "data": {"text": "图片说明"}},
                {
                    "type": "image",
                    "data": {
                        "file": "qq-image-abc.jpg",
                        "file_id": "image-file-id-123",
                        "url": "https://gchat.qpic.cn/example/qq-image-abc.jpg?term=2",
                        "width": 1280,
                        "height": 720,
                        "sub_type": 0,
                    },
                },
                {"type": "face", "data": {"id": "14"}},
            ],
        }
        message_obj = SimpleNamespace(
            raw_message=raw_event,
            message_id=raw_event["message_id"],
            sender=SimpleNamespace(user_id=2650362143, nickname="测试用户"),
        )
        return SimpleNamespace(
            message_obj=message_obj,
            message_str="图片说明",
            get_messages=lambda: [],
        )

    def test_image_metadata_is_preserved_in_record(self):
        record = message_cache.build_message_record(
            "1102579807",
            "2650362143",
            self._build_event(),
            {"cache_raw_message": True},
        )

        self.assertEqual(
            [segment["type"] for segment in record["segments"]],
            ["text", "image", "face"],
        )
        self.assertEqual(record["nickname"], "测试用户")
        self.assertEqual(record["sender"]["card"], "")
        image = record["segments"][1]
        self.assertEqual(image["data"]["file"], "qq-image-abc.jpg")
        self.assertEqual(image["data"]["file_id"], "image-file-id-123")
        self.assertEqual(image["data"]["width"], 1280)
        self.assertEqual(image["data"]["height"], 720)
        self.assertIn("url", image["data"])
        self.assertEqual(image["summary"], "[图片: image-file-id-123]")
        self.assertEqual(
            message_cache.format_cached_message(record),
            "图片说明 [图片: image-file-id-123] [表情: 14]",
        )

    def test_image_metadata_survives_json_round_trip(self):
        group_id = "1102579807"
        user_id = "2650362143"
        record = message_cache.build_message_record(
            group_id,
            user_id,
            self._build_event(),
            {"cache_raw_message": True},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            config_file = data_dir / "group_configs" / f"{group_id}.json"
            config_file.parent.mkdir(parents=True)
            config_file.write_text("{}", encoding="utf-8")

            with (
                patch.object(message_cache, "get_plugin_data_path", return_value=data_dir),
                patch.object(message_cache, "get_group_config_file", return_value=config_file),
            ):
                self.assertTrue(
                    message_cache.cache_group_message_record(
                        record,
                        {
                            "enabled": True,
                            "cache_raw_message": True,
                            "retention_days": 7,
                            "max_messages_per_user": 500,
                        },
                    )
                )
                stored = message_cache.get_cached_messages(group_id, user_id, 10)

        self.assertEqual(len(stored), 1)
        image = stored[0]["segments"][1]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["data"]["file_id"], "image-file-id-123")
        self.assertTrue(image["data"]["url"].startswith("https://gchat.qpic.cn/"))

    def test_mirrored_adapter_event_is_deduplicated_by_content_key(self):
        """The same QQ message can arrive through more than one adapter."""
        group_id = "1102579807"
        user_id = "2650362143"
        record = message_cache.build_message_record(
            group_id,
            user_id,
            self._build_event(),
            {"cache_raw_message": True},
        )
        mirrored = dict(record)
        mirrored["message_id"] = "mirror-778899"
        settings = {
            "enabled": True,
            "cache_raw_message": True,
            "retention_days": 7,
            "max_messages_per_user": 500,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            config_file = data_dir / "group_configs" / f"{group_id}.json"
            config_file.parent.mkdir(parents=True)
            config_file.write_text("{}", encoding="utf-8")
            with (
                patch.object(message_cache, "get_plugin_data_path", return_value=data_dir),
                patch.object(message_cache, "get_group_config_file", return_value=config_file),
            ):
                self.assertTrue(message_cache.cache_group_message_record(record, settings))
                self.assertTrue(message_cache.cache_group_message_record(mirrored, settings))
                stored = message_cache.get_cached_messages(group_id, user_id, 10)

        self.assertEqual(1, len(stored))
        self.assertEqual(record["dedupe_key"], stored[0]["dedupe_key"])

    def test_distinct_same_second_messages_keep_distinct_ids_without_sequence(self):
        first_event = self._build_event()
        second_event = self._build_event()
        second_event.message_obj.raw_message["message_id"] = 778900
        second_event.message_obj.message_id = 778900
        first = message_cache.build_message_record(
            "1102579807", "2650362143", first_event, {"cache_raw_message": True}
        )
        second = message_cache.build_message_record(
            "1102579807", "2650362143", second_event, {"cache_raw_message": True}
        )
        self.assertNotEqual(first["dedupe_key"], second["dedupe_key"])

    def test_different_receiver_accounts_merge_mirrored_message_ids(self):
        first_event = self._build_event()
        mirrored_event = self._build_event()
        first_event.message_obj.raw_message["self_id"] = 958856515
        mirrored_event.message_obj.raw_message["self_id"] = 123456789
        mirrored_event.message_obj.raw_message["message_id"] = 778900
        mirrored_event.message_obj.message_id = 778900
        first = message_cache.build_message_record(
            "1102579807", "2650362143", first_event, {"cache_raw_message": True}
        )
        mirrored = message_cache.build_message_record(
            "1102579807", "2650362143", mirrored_event, {"cache_raw_message": True}
        )
        self.assertEqual(first["mirror_key"], mirrored["mirror_key"])
        self.assertNotEqual(first["source_account_id"], mirrored["source_account_id"])

        settings = {
            "enabled": True,
            "cache_raw_message": True,
            "retention_days": 7,
            "max_messages_per_user": 500,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            config_file = data_dir / "group_configs" / "1102579807.json"
            config_file.parent.mkdir(parents=True)
            config_file.write_text("{}", encoding="utf-8")
            with (
                patch.object(message_cache, "get_plugin_data_path", return_value=data_dir),
                patch.object(message_cache, "get_group_config_file", return_value=config_file),
            ):
                self.assertTrue(message_cache.cache_group_message_record(first, settings))
                self.assertTrue(message_cache.cache_group_message_record(mirrored, settings))
                stored = message_cache.get_cached_messages("1102579807", "2650362143", 10)
        self.assertEqual(1, len(stored))

    def test_mirrored_image_urls_keep_the_same_message_fingerprint(self):
        first_event = self._build_event()
        mirrored_event = self._build_event()
        first_event.message_obj.raw_message["self_id"] = 958856515
        mirrored_event.message_obj.raw_message["self_id"] = 123456789
        mirrored_event.message_obj.raw_message["message_id"] = 778900
        mirrored_event.message_obj.message_id = 778900
        mirrored_event.message_obj.raw_message["message"][1]["data"]["url"] = (
            "https://other.qpic.cn/same-file?sign=adapter-b"
        )
        first = message_cache.build_message_record(
            "1102579807", "2650362143", first_event, {"cache_raw_message": True}
        )
        mirrored = message_cache.build_message_record(
            "1102579807", "2650362143", mirrored_event, {"cache_raw_message": True}
        )
        self.assertEqual(first["mirror_key"], mirrored["mirror_key"])

    def test_base64_image_archives_by_group_date_and_user(self):
        image_data = b"\x89PNG\r\n\x1a\nmessage-media-test"
        timestamp = int(time.time())
        record = {
            "segments": [
                {
                    "type": "image",
                    "data": {"url": "base64://" + base64.b64encode(image_data).decode()},
                }
            ]
        }

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            message_cache, "get_plugin_data_path", return_value=Path(temp_dir)
        ):
            sources = message_cache.extract_image_sources(record)
            saved = asyncio.run(
                message_cache.archive_group_message_images(
                    "1102579807", "2650362143", timestamp, "778899", sources
                )
            )
            day = time.strftime("%Y-%m-%d", time.localtime(timestamp))
            archived_files = list(
                (Path(temp_dir) / "message_images" / "1102579807" / day / "2650362143").glob("*")
            )
            archived_content = archived_files[0].read_bytes() if archived_files else b""

        self.assertEqual(saved, 1)
        self.assertEqual(len(archived_files), 1)
        self.assertEqual(archived_content, image_data)

    def test_same_image_with_different_message_ids_is_archived_once(self):
        image_data = b"\x89PNG\r\n\x1a\nmessage-media-duplicate-test"
        source = "base64://" + base64.b64encode(image_data).decode()
        timestamp = int(time.time())
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            message_cache, "get_plugin_data_path", return_value=Path(temp_dir)
        ):
            first = asyncio.run(
                message_cache.archive_group_message_images(
                    "1102579807", "2650362143", timestamp, "message-a", [source]
                )
            )
            second = asyncio.run(
                message_cache.archive_group_message_images(
                    "1102579807", "2650362143", timestamp, "message-b", [source]
                )
            )
            day = time.strftime("%Y-%m-%d", time.localtime(timestamp))
            archived_files = list(
                (Path(temp_dir) / "message_images" / "1102579807" / day / "2650362143").glob("*")
            )

        self.assertEqual(1, first)
        self.assertEqual(0, second)
        self.assertEqual(1, len(archived_files))

    def test_pending_cache_spool_round_trip_and_cleanup(self):
        record = {
            "group_id": "1102579807",
            "user_id": "2650362143",
            "message_id": "pending-778899",
            "dedupe_key": "pending-dedupe",
            "timestamp": int(time.time()),
            "segments": [{"type": "text", "data": {"text": "待恢复"}}],
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            message_cache, "get_plugin_data_path", return_value=Path(temp_dir)
        ):
            self.assertTrue(message_cache.queue_pending_message_cache_record(record))
            pending = message_cache.load_pending_message_cache_records("1102579807")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["message_id"], "pending-778899")
            self.assertTrue(
                message_cache.remove_pending_message_cache_records(
                    "1102579807", pending
                )
            )
            self.assertEqual(
                message_cache.load_pending_message_cache_records("1102579807"), []
            )


if __name__ == "__main__":
    unittest.main()
