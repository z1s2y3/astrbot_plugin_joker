import ast
import unittest
from pathlib import Path

from feature_catalog import (
    CONFIGURABLE_FEATURES,
    FEATURE_BY_COMMAND,
    FEATURE_BY_KEY,
    FEATURE_CATEGORIES,
    feature_alias_map,
)


ROOT = Path(__file__).resolve().parent


class FeatureCatalogTests(unittest.TestCase):
    def test_primary_categories_match_public_menu(self):
        self.assertEqual(
            [category.label for category in FEATURE_CATEGORIES],
            [
                "入群审核", "黑名单系统", "禁言系统", "踢出系统", "撤回系统",
                "入群欢迎", "自动回复", "抽奖", "统计系统", "签到", "签到状态", "签到日历",
                "签到排行", "排行榜", "我的信息", "群统计", "定时消息", "分群管理员",
                "文转图", "群设置", "关键词过滤", "用户专属回复", "SCUM 查询", "游戏指令",
            ],
        )

    def test_statistics_actions_share_stats_switch(self):
        for key in (
            "signin", "signin_status", "signin_calendar", "signin_ranking",
            "ranking", "user_info", "group_stats",
        ):
            self.assertEqual(FEATURE_BY_KEY[key].enabled_key, "stats_enabled")

    def test_compatibility_aliases(self):
        self.assertEqual(FEATURE_BY_COMMAND["自定义回复"].key, "reply")
        self.assertEqual(FEATURE_BY_COMMAND["分群设置"].key, "group_config")
        self.assertEqual(FEATURE_BY_COMMAND["我的签到"].key, "signin_status")
        self.assertEqual(FEATURE_BY_COMMAND["服务器查询"].key, "scum")

    def test_configurable_features_include_message_history(self):
        keys = [category.key for category in CONFIGURABLE_FEATURES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("message_cache", keys)
        self.assertEqual(feature_alias_map()["群消息记录"], "message_cache_enabled")

    def test_each_help_handler_exists_in_plugin_class(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        plugin_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MyPlugin"
        )
        methods = {
            node.name for node in plugin_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for category in FEATURE_CATEGORIES:
            self.assertIn(category.help_handler, methods, category.label)


if __name__ == "__main__":
    unittest.main()
