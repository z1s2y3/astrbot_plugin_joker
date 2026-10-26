"""Regression checks for the shared group-feature command surface."""

import ast
import re
import textwrap
import unittest
from pathlib import Path

from web_client.services import _feature_config_summary, _group_command_groups


ROOT = Path(__file__).resolve().parent


class GroupFeatureCommandSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.plugin = next(
            node
            for node in cls.tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MyPlugin"
        )
        cls.methods = {
            node.name: node
            for node in cls.plugin.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_feature_names_accept_aliases_and_enabled_keys(self):
        self.assertIn("def _resolve_group_feature_category", self.source)
        self.assertIn("feature_alias_map()", self.source)
        for method_name in (
            "_handle_enable_feature",
            "_handle_disable_feature",
            "_handle_list_features",
            "_handle_set_feature_setting",
        ):
            body = ast.get_source_segment(self.source, self.methods[method_name]) or ""
            self.assertIn("_resolve_group_feature_category", body, method_name)

    def test_set_command_uses_the_portal_schema(self):
        body = ast.get_source_segment(self.source, self.methods["_handle_set_feature_setting"]) or ""
        self.assertIn("PORTAL_FEATURE_BY_KEY", body)
        self.assertIn("parse_field_value", body)
        self.assertIn("JSON_LINE_FIELD_KEYS", body)
        self.assertIn('"keyword", "regex", "all"', body)
        self.assertNotIn('"keyword", "illegal", "all"', body)

    def test_group_commands_have_dynamic_runtime_routes(self):
        for method_name in (
            "_match_signin_dynamic_command",
            "_get_signin_command_settings",
            "_match_entertainment_replaced_command",
        ):
            self.assertIn(method_name, self.methods)
        handle_body = ast.get_source_segment(self.source, self.methods["handle_message"]) or ""
        self.assertIn("_match_signin_dynamic_command", handle_body)
        self.assertIn("_match_entertainment_replaced_command", handle_body)
        self.assertIn("disabled_signin", handle_body)
        self.assertIn("disabled_bind", handle_body)

    def test_game_signin_and_multi_scheme_help_are_exposed(self):
        binding = ast.get_source_segment(
            self.source, self.methods["_steam_binding_requires_database"]
        ) or ""
        self.assertIn("database_sync", binding)
        self.assertIn("steam_reward_mode", binding)
        signin_help = ast.get_source_segment(self.source, self.methods["_show_stats_help"]) or ""
        self.assertIn("独立方案", signin_help)
        game_help = ast.get_source_segment(self.source, self.methods["_show_game_commands_help"]) or ""
        self.assertIn("本群 SteamID", game_help)

    def test_admission_steamid_verification_is_private_only(self):
        direct = ast.get_source_segment(
            self.source, self.methods["handle_direct_verification"]
        ) or ""
        private_guard = direct.find('if approval_mode == "steamid":')
        old_group_format_error = direct.find('if not re.fullmatch(r"[0-9]{17}", answer)')
        self.assertGreaterEqual(private_guard, 0)
        self.assertIn("Admission SteamID checks are private-only", direct)
        self.assertLess(private_guard, old_group_format_error)

    def test_player_signin_help_shows_effective_binding_and_profiles(self):
        command_settings = ast.get_source_segment(
            self.source, self.methods["_get_signin_command_settings"]
        ) or ""
        stats_help = ast.get_source_segment(self.source, self.methods["_show_stats_help"]) or ""
        signin_help = ast.get_source_segment(self.source, self.methods["_show_signin_help"]) or ""
        self.assertIn('"unbind": "解绑SteamID"', command_settings)
        self.assertIn("commands['unbind']", stats_help)
        self.assertIn("commands['unbind']", signin_help)
        self.assertIn("_get_signin_profile_help_lines", stats_help)
        self.assertIn("_get_signin_profile_help_lines", signin_help)
        self.assertIn("signin_mode", self.source)
        self.assertIn("schedule_mode", self.source)

    def test_configured_command_replies_are_not_hardcoded(self):
        entertainment = ast.get_source_segment(
            self.source, self.methods["_dispatch_entertainment_command"]
        ) or ""
        self.assertIn("_get_event_message_text", entertainment)
        self.assertIn("娱乐功能未开启，请管理员使用 /开启娱乐。", entertainment)
        self.assertIn("_ENTERTAINMENT_CONTROL_EXTRA", entertainment)
        self.assertIn('"我", "本人", "自己", "赞我", "给我"', entertainment)
        self.assertIn('"like_daily_limit"', entertainment)
        self.assertIn("claim_daily_likes", entertainment)
        self.assertIn("search_music_candidates", entertainment)
        self.assertIn("save_music_selection", entertainment)
        self.assertIn("select_music_candidate", entertainment)
        self.assertIn("build_music_fallback_text", entertainment)
        self.assertNotIn("sign_music_card(track)", entertainment)
        self.assertNotIn("music_card_json", entertainment)
        music_sender = ast.get_source_segment(
            self.source, self.methods["_send_music_track_card"]
        ) or ""
        self.assertIn("build_onebot_music_segment", music_sender)
        self.assertIn("build_onebot_custom_music_segment", music_sender)
        self.assertIn("build_onebot_json_segment", music_sender)
        self.assertIn("sign_music_card", music_sender)
        self.assertIn("后台音乐名片签名失败", music_sender)
        self.assertIn("settings['dice_command']", entertainment)
        self.assertIn("settings['rps_command']", entertainment)
        self.assertIn("settings['random_command']", entertainment)
        route_body = ast.get_source_segment(
            self.source, self.methods["_match_entertainment_command"]
        ) or ""
        self.assertIn("configured_commands", route_body)
        self.assertIn('"luck_command"', route_body)
        self.assertIn('"answer_command"', route_body)
        handle_body = ast.get_source_segment(self.source, self.methods["handle_message"]) or ""
        self.assertIn("_match_entertainment_command", handle_body)
        self.assertIn("self._dispatch_entertainment_command(event)", handle_body)
        self.assertIn("_is_entertainment_control_command", handle_body)
        dispatcher = ast.get_source_segment(
            self.source, self.methods["_dispatch_entertainment_command"]
        ) or ""
        self.assertIn("娱乐命令", dispatcher)
        self.assertIn("is_at_or_wake_command", dispatcher)
        static = ast.get_source_segment(
            self.source, self.methods["handle_entertainment_static_commands"]
        ) or ""
        self.assertIn("_dispatch_entertainment_command", static)
        self.assertIn('@filter.command("点歌")', self.source)
        self.assertIn('@filter.command("选歌")', self.source)
        self.assertIn('@filter.command("赞我")', self.source)
        self.assertIn('@filter.command("开启娱乐")', self.source)
        top_help = ast.get_source_segment(self.source, self.methods["show_help"]) or ""
        self.assertIn("_format_compact_feature_menu", top_help)

    def test_help_is_nested_and_supports_feature_drilldown(self):
        for method_name in (
            "_format_nested_command_help",
            "_command_help_args",
            "_group_help_sections",
            "_group_help_detail_handler",
        ):
            self.assertIn(method_name, self.methods)
        top_help = ast.get_source_segment(self.source, self.methods["show_help"]) or ""
        group_help = ast.get_source_segment(self.source, self.methods["show_qqadmin_help"]) or ""
        scum_help = ast.get_source_segment(self.source, self.methods["show_scum_help"]) or ""
        self.assertIn("_group_help_sections", top_help)
        self.assertNotIn("一级：", top_help)
        self.assertNotIn("/群管功能 <分类> <功能>", top_help)
        self.assertIn("args[0]", group_help)
        self.assertIn("_group_help_detail_handler", group_help)
        self.assertIn("_format_nested_command_help", group_help)
        self.assertIn("_is_scum_query_enabled", scum_help)
        self.assertIn("SCUM 查询未开启。", scum_help)
        self.assertNotIn("一级：", scum_help)
        self.assertNotIn("兼容：", scum_help)

    def test_scum_static_commands_respect_per_group_names(self):
        for method_name, command_key in (
            ("query", "keyword_command"),
            ("query_online", "online_command"),
        ):
            body = ast.get_source_segment(self.source, self.methods[method_name]) or ""
            self.assertIn(f'_is_configured_scum_command(event, "{command_key}")', body)
        matcher = ast.get_source_segment(
            self.source, self.methods["_match_scum_dynamic_command"]
        ) or ""
        self.assertIn('"disabled", configured_name', matcher)

    def test_renamed_scum_commands_have_an_early_runtime_route(self):
        source = self.source
        self.assertIn("def handle_custom_scum_command_early", source)
        self.assertIn("priority=19900", source)
        self.assertNotIn('@filter.regex(r"^/")', source)
        self.assertIn('getattr(event, "is_at_or_wake_command", False)', source)
        self.assertIn('command = message.lstrip("/").split(maxsplit=1)[0]', source)
        self.assertIn("SCUM 查询执行失败，请稍后重试。", source)

    def test_renamed_scum_command_disables_old_route(self):
        method = self.methods["_match_scum_dynamic_command"]
        namespace = {"AstrMessageEvent": object}
        module = ast.Module(body=[method], type_ignores=[])
        exec(compile(module, str(ROOT / "main.py"), "exec"), namespace)

        class PluginStub:
            query_enabled = True

            def _is_scum_query_enabled(self, _event):
                return self.query_enabled

            def _get_scum_group_config(self, _event):
                return {
                    "keyword_command": "查询2",
                    "keyword_command_aliases": ("查询2",),
                    "online_command": "查询在线",
                    "online_command_aliases": ("查询在线",),
                }

        matcher = namespace["_match_scum_dynamic_command"]
        plugin = PluginStub()
        event = object()
        self.assertEqual(matcher(plugin, event, "查询 8888"), ("disabled", "查询2"))
        self.assertEqual(matcher(plugin, event, "查询2 8888"), ("keyword", "8888"))
        self.assertEqual(
            matcher(plugin, event, "查询2关键词 8888"),
            ("keyword_missing_separator", "查询2"),
        )
        plugin.query_enabled = False
        self.assertEqual(
            matcher(plugin, event, "查询2 8888"),
            ("feature_disabled", "8888"),
        )

    def test_server_binding_commands_cannot_enter_steamid_routes(self):
        helper = self.methods["_is_server_binding_command"]
        namespace = {}
        exec(
            compile(
                ast.Module(body=[helper], type_ignores=[]),
                str(ROOT / "main.py"),
                "exec",
            ),
            namespace,
        )
        is_server_binding = namespace["_is_server_binding_command"]
        for message in ("/绑定id 41726514", "/id绑定 41726514", "/绑定 41726514"):
            self.assertTrue(is_server_binding(message))
        for message in ("/绑定SteamID 76561198012345678", "/绑定steam 76561198012345678"):
            self.assertFalse(is_server_binding(message))

        signin_matcher = self.methods["_match_signin_dynamic_command"]
        module = ast.Module(body=[helper, signin_matcher], type_ignores=[])
        exec(compile(module, str(ROOT / "main.py"), "exec"), namespace)

        class PluginStub:
            def _is_server_binding_command(self, message):
                return is_server_binding(message)

            def _get_group_id_from_event(self, _event):
                return "1132131381"

            def _get_signin_policy(self, _group_id):
                return {"signin_profiles": [], "bind_command": "绑定id"}

            def _command_aliases(self, value, default):
                return (str(value or default),)

        self.assertIsNone(
            namespace["_match_signin_dynamic_command"](
                PluginStub(), object(), "绑定id 41726514"
            )
        )

    def test_scum_static_fallbacks_explain_disabled_or_renamed_commands(self):
        for method_name in ("query", "query_online"):
            body = ast.get_source_segment(self.source, self.methods[method_name]) or ""
            self.assertIn("本群 SCUM 查询功能已停用", body)
            self.assertIn("指令已修改", body)

    def test_scum_server_binding_is_separate_from_steamid_binding(self):
        self.assertIn('@filter.command("绑定id", alias={"绑定", "id绑定"})', self.source)
        bind_handler = ast.get_source_segment(
            self.source, self.methods["bind_server_id"]
        ) or ""
        server_binding = ast.get_source_segment(
            self.source, self.methods["_bind_server"]
        ) or ""
        self.assertNotIn("_bind_steam_id_for_event", bind_handler)
        self.assertIn("normalize_steam_id(server_id)", server_binding)
        self.assertIn("/绑定SteamID", server_binding)

    def test_group_help_index_uses_compact_configured_format(self):
        group_help = ast.get_source_segment(self.source, self.methods["show_qqadmin_help"]) or ""
        self.assertIn("_group_help_sections", group_help)
        self.assertIn("_format_group_admin_feature_menu", group_help)
        self.assertIn("_group_help_detail_handler", group_help)
        self.assertNotIn("授权：{auth_state}", group_help)
        self.assertNotIn("签到数据库：{database_state}", group_help)

    def test_game_help_exposes_effective_templates(self):
        game_help = ast.get_source_segment(self.source, self.methods["_show_game_commands_help"]) or ""
        self.assertIn("item['template']", game_help)
        self.assertIn("模板：", game_help)

    def test_status_detail_covers_extended_feature_modules(self):
        body = ast.get_source_segment(self.source, self.methods["_handle_view_feature_detail"]) or ""
        for feature in ("entertainment", "game_commands", "game_servers", "group_config"):
            self.assertIn(f'feature == "{feature}"', body)

    def test_status_detail_shows_blacklist_policy_fields(self):
        body = ast.get_source_segment(self.source, self.methods["_handle_view_feature_detail"]) or ""
        for field in (
            "auto_kick_on_join",
            "notify_on_kick",
            "kick_reason",
            "max_auto_kick_per_day",
        ):
            self.assertIn(field, body)

    def test_web_feature_summary_is_bounded_and_secret_free(self):
        summary = _feature_config_summary(
            "game_servers",
            {
                "enabled": True,
                "profiles": [{"id": "main", "host": "127.0.0.1", "password": "secret"}],
            },
        )
        self.assertIn("1 个服务器档案", summary)
        self.assertNotIn("secret", summary)

    def test_web_feature_summary_shows_effective_commands(self):
        stats = _feature_config_summary(
            "stats",
            {
                "enabled": True,
                "signin_command": "每日签到",
                "bind_command": "绑定游戏",
                "signin_mode": "basic",
                "signin_base_reward": 10,
            },
        )
        entertainment = _feature_config_summary(
            "entertainment",
            {
                "enabled": True,
                "dice_command": "掷骰",
                "rps_command": "猜拳局",
                "random_command": "抽选",
            },
        )
        scum = _feature_config_summary(
            "scum",
            {
                "enabled": True,
                "keyword_command": "查关键词",
                "online_command": "看在线",
            },
        )
        self.assertIn("/每日签到", stats)
        self.assertIn("/绑定游戏", stats)
        self.assertIn("/掷骰/猜拳局/抽选", entertainment)
        self.assertIn("/查关键词", scum)
        self.assertIn("/看在线", scum)
        self.assertNotIn("/查地址", scum)

    def test_web_command_surface_lists_all_feature_routes(self):
        groups = _group_command_groups(
            {
                "features": {
                    "audit": {"enabled": True},
                    "blacklist": {"enabled": True},
                    "mute": {"enabled": True},
                    "kick": {"enabled": True},
                    "recall": {"enabled": True},
                    "welcome": {"enabled": True},
                    "reply": {"enabled": True},
                    "player_reply": {"enabled": True},
                    "keyword_filter": {"enabled": True},
                    "timed_message": {"enabled": True},
                    "sub_admin": {"enabled": True},
                    "group_config": {"enabled": True},
                    "text_to_image": {"enabled": True},
                    "lottery": {"enabled": True, "keyword": "摇奖"},
                    "stats": {"enabled": True, "signin_command": "打卡", "bind_command": "绑服"},
                    "scum": {"enabled": True, "online_command": "看在线"},
                    "entertainment": {
                        "enabled": True,
                        "luck_command": "幸运数|今日号码",
                        "answer_command": "答案书|问问看",
                    },
                }
            }
        )
        flattened = {
            item["command"]
            for group in groups
            for item in group["items"]
        }
        self.assertTrue(any("/打卡" in value for value in flattened))
        self.assertTrue(any("/看在线" in value for value in flattened))
        self.assertTrue(any("/摇奖" in value for value in flattened))
        self.assertTrue(any("/幸运数 /今日号码" in value for value in flattened))
        self.assertTrue(any("/答案书 /问问看" in value for value in flattened))
        self.assertTrue(any("/回答验证" in value for value in flattened))
        self.assertTrue(any("/清空全局黑名单" in value for value in flattened))
        self.assertTrue(any("/添加定时音乐" in value for value in flattened))
        self.assertTrue(any("/自动授予称号" in value for value in flattened))
        command_items = [
            item
            for group in groups
            for item in group["items"]
            if item["command"] == "/群管功能"
        ]
        self.assertEqual(len(command_items), 1)
        self.assertEqual(command_items[0]["usage"], "[分类] [功能]")
        self.assertEqual(command_items[0]["audience"], "所有成员")

    def test_group_help_command_is_configured_and_routed_before_static_dispatch(self):
        self.assertIn("help_command", self.source)
        self.assertIn("help_permission", self.source)
        for method_name in (
            "_get_group_help_settings",
            "_match_group_help_dynamic_command",
            "_group_help_permission_allowed",
        ):
            self.assertIn(method_name, self.methods)
        handle_body = ast.get_source_segment(self.source, self.methods["handle_message"]) or ""
        self.assertIn("_match_group_help_dynamic_command", handle_body)
        help_body = ast.get_source_segment(self.source, self.methods["show_qqadmin_help"]) or ""
        self.assertIn("configured_aliases", help_body)
        self.assertIn("help_permission", self.source)

    def test_plain_replies_use_one_compact_formatter(self):
        helper = next(
            node
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_compact_reply_text"
        )
        namespace = {"re": re, "textwrap": textwrap}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(ROOT / "main.py"), "exec"), namespace)
        compact = namespace["_compact_reply_text"]
        self.assertEqual(
            "状态\n\n  ✅️ 已成功",
            compact("  状态\n\n\n    ✅️ 已成功  \n"),
        )
        self.assertEqual(
            "只有管理员或群主才能执行此操作。",
            compact("只有管理员或群主才能执行此操作。"),
        )
        self.assertEqual("签到只能在群聊中使用", compact("签到只能在群聊中使用"))
        self.assertEqual("此命令只能在群组中使用", compact("此命令只能在群组中使用"))
        self.assertEqual("当前签到权限不足，无法绑定 SteamID。", compact("当前签到权限不足，无法绑定 SteamID。"))
        self.assertEqual("✅ 已保存到分群配置", compact("✅ 已保存到分群配置"))
        for method_name in ("_yield_plain", "_send_plain", "_send_group_msg"):
            body = ast.get_source_segment(self.source, self.methods[method_name]) or ""
            self.assertIn("_compact_reply_text", body, method_name)

    def test_image_reply_builds_a_message_chain_before_sending(self):
        body = ast.get_source_segment(self.source, self.methods["handle_auto_reply"]) or ""
        self.assertIn(
            "from astrbot.core.message.message_event_result import MessageChain",
            body,
        )
        self.assertIn("chain = MessageChain()", body)


if __name__ == "__main__":
    unittest.main()
