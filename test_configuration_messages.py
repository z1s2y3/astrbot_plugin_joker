import json
import ast
import unittest
from pathlib import Path


class ConfigurationMessageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schema_path = Path(__file__).with_name("_conf_schema.json")
        with schema_path.open("r", encoding="utf-8") as handle:
            cls.schema = json.load(handle)

    def test_auth_expiry_message_settings_are_exposed(self):
        auth_items = self.schema["auth"]["items"]
        self.assertIn("auth_expired_command_reply_message", auth_items)
        self.assertIn("auth_expired_command_reply_interval", auth_items)
        self.assertIn("allow_owner_when_expired", auth_items)
        self.assertFalse(auth_items["allow_owner_when_expired"]["default"])
        self.assertTrue(auth_items["auth_expired_command_reply_message"]["default"])

    def test_schema_objects_always_define_items_and_keep_runtime_maps_opaque(self):
        def check_items(node: dict, path: str = "") -> None:
            for key, field in node.items():
                if not isinstance(field, dict):
                    continue
                current_path = f"{path}.{key}" if path else key
                if field.get("type") == "object":
                    self.assertIsInstance(
                        field.get("items"),
                        dict,
                        f"{current_path} is an object without items",
                    )
                if isinstance(field.get("items"), dict):
                    check_items(field["items"], current_path)

        check_items(self.schema)
        web_sync = self.schema["web_sync"]["items"]
        for key in ("music_source_services", "game_executor", "im3_socketio"):
            self.assertEqual("text", web_sync[key]["type"])

    def test_group_message_image_archive_settings_are_exposed(self):
        items = self.schema["qqadmin"]["items"]["message_cache"]["items"]
        self.assertEqual(True, items["archive_images"]["default"])
        self.assertEqual(20, items["archive_image_max_mb"]["default"])
        self.assertIn("群号/日期/用户ID", items["archive_images"]["hint"])

    def test_group_status_message_settings_are_exposed(self):
        prompt_items = self.schema["qqadmin"]["items"]["message_prompts"]["items"]
        self.assertEqual(
            set(prompt_items),
            {
                "power_on_prompt",
                "power_on_success",
                "power_off_success",
                "power_off_already",
            },
        )
        for item in prompt_items.values():
            self.assertEqual(item["type"], "text")
            self.assertTrue(item["default"])

    def test_existing_recall_notification_message_is_configurable(self):
        recall_items = self.schema["qqadmin"]["items"]["recall"]["items"]
        self.assertIn("recall_notification_message", recall_items)
        self.assertEqual(recall_items["recall_notification_message"]["type"], "text")
        self.assertTrue(recall_items["recall_notification_message"]["hint"])

    def test_welcome_mentions_the_at_new_member_switch(self):
        welcome_items = self.schema["qqadmin"]["items"]["welcome"]["items"]
        self.assertIn("at_new_member", welcome_items)
        self.assertEqual(welcome_items["at_new_member"]["type"], "bool")
        self.assertTrue(welcome_items["at_new_member"]["hint"])

    def test_reply_image_cache_settings_are_exposed_with_safe_defaults(self):
        reply_items = self.schema["qqadmin"]["items"]["reply"]["items"]
        self.assertEqual(
            {
                "cache_reply_images": True,
                "reply_image_max_mb": 20,
                "reply_image_download_concurrency": 4,
                "reply_image_retry_interval": 30,
            },
            {
                key: reply_items[key]["default"]
                for key in (
                    "cache_reply_images",
                    "reply_image_max_mb",
                    "reply_image_download_concurrency",
                    "reply_image_retry_interval",
                )
            },
        )
        self.assertIn("1-100", reply_items["reply_image_max_mb"]["hint"])
        self.assertIn("1-8", reply_items["reply_image_download_concurrency"]["hint"])
        self.assertIn("30-86400", reply_items["reply_image_retry_interval"]["hint"])

    def test_reply_runtime_settings_use_one_loader_for_start_and_reload(self):
        main_path = Path(__file__).with_name("main.py")
        tree = ast.parse(main_path.read_text(encoding="utf-8"))
        plugin = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MyPlugin"
        )
        init_method = next(
            node
            for node in plugin.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        loader_calls = [
            node
            for node in ast.walk(init_method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_load_reply_runtime_settings"
        ]

        self.assertEqual(len(loader_calls), 2)

    def test_signin_rewards_and_extended_commands_are_configurable(self):
        stats = self.schema["qqadmin"]["items"]["stats"]
        items = stats["items"]
        self.assertIn("/签到日历", stats["hint"])
        self.assertIn("/签到排行", items["enable_stats"]["hint"])
        self.assertEqual(
            {
                "signin_base_reward": 10,
                "signin_reward_3_days": 30,
                "signin_reward_7_days": 100,
                "signin_reward_14_days": 200,
                "signin_reward_30_days": 500,
            },
            {
                key: items[key]["default"]
                for key in (
                    "signin_base_reward",
                    "signin_reward_3_days",
                    "signin_reward_7_days",
                    "signin_reward_14_days",
                    "signin_reward_30_days",
                )
            },
        )


if __name__ == "__main__":
    unittest.main()
