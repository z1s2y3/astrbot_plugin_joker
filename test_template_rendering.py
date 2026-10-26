"""Regression tests for portal templates that receive dictionary contexts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest

from jinja2 import Environment, FileSystemLoader, select_autoescape

from web_client.feature_schema import FEATURE_BY_KEY, display_value, parse_field_value


BASE_DIR = Path(__file__).resolve().parent
MANAGED_FEATURE_KEYS = {"reply", "player_reply", "timed_message", "sub_admin"}


def _environment() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(str(BASE_DIR / "web_client" / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )
    environment.globals["display_value"] = display_value
    environment.globals["console_event_time"] = lambda _value: "08-19 09:00:00"
    return environment


def _feature_context() -> list[dict]:
    return [
        {
            "spec": spec,
            "values": {field.key: field.default for field in spec.fields},
        }
        for spec in FEATURE_BY_KEY.values()
    ]


class PortalTemplateTests(unittest.TestCase):
    def test_group_integrations_template_uses_one_server_bundle_editor(self):
        """FTP, database and WebSocket must share one visible server form."""

        environment = _environment()
        template = environment.get_template("group_integrations.html")
        request = SimpleNamespace(
            url=SimpleNamespace(path="/user/groups/766637236/integrations"),
            app=SimpleNamespace(
                state=SimpleNamespace(
                    settings=SimpleNamespace(data_mode="remote")
                )
            ),
        )
        rendered = template.render(
            request=request,
            portal_name="Joker",
            csrf_token="test",
            flashes=[],
            current_user=SimpleNamespace(is_admin=False, username="customer"),
            current_year=2026,
            request_id="test-request",
            theme={"mode": "auto", "day_url": "", "night_url": ""},
            integration_title="群 766637236 · 服务器与数据库",
            integration_group_id="766637236",
            integration_scope_label="仅群 766637236 · 当前账号",
            integration_back_url="/user/groups/766637236",
            integration_back_label="返回群配置",
            integration_prefix="/user/groups/766637236",
            integration_profiles=[
                {
                    "profile_id": "main",
                    "name": "主服务器",
                    "server_id": "38805679",
                    "profile_enabled": True,
                    "active": True,
                    "snapshot_revision": 2,
                    "snapshot_sha256": "a" * 64,
                    "windows_database_path": "sql/servers/766637236/main.db",
                    "ftp_connection_name": "主服务器 FTP",
                    "database": {"sqlite_path": "/BjxRobot/beijixiong.db"},
                    "game": {"id": "main", "enabled": True, "api_key_configured": True},
                }
            ],
            integration_profile_id="main",
            integration_creating_profile=False,
            integration_config={
                "ftp_connection_id": "ftp-main",
                "ftp": {
                    "enabled": True,
                    "host": "ftp.example.com",
                    "port": 21,
                    "username": "server",
                    "remote_path": "/",
                    "tls": False,
                    "passive": True,
                    "allow_parent_directory": False,
                    "password_configured": True,
                },
                "database": {
                    "enabled": True,
                    "sqlite_path": "/BjxRobot/beijixiong.db",
                    "selection_source": "manual",
                },
            },
            unified_ftp_connection_id="ftp-main",
            game_server_profile={
                "id": "main",
                "enabled": True,
                "timeout_seconds": 10,
                "api_key_configured": True,
            },
            ftp_browser=None,
            ftp_browser_requested=False,
            ftp_browser_path="",
        )
        self.assertIn("无需单独创建 FTP 连接", rendered)
        self.assertIn('action="/user/groups/766637236/integrations/server"', rendered)
        self.assertIn("保存这台游戏服务器", rendered)
        self.assertNotIn("保存 FTP 连接", rendered)
        self.assertIn("普通用户只需在群配置中验证对应游戏卡密", rendered)
        self.assertNotIn('name="websocket_url"', rendered)

    def test_group_template_renders_dictionary_values_key(self):
        """Jinja must use the ``values`` key, not dict.values()."""
        environment = _environment()
        template = environment.get_template("group.html")

        request = SimpleNamespace(
            url=SimpleNamespace(path="/user/groups/766637236"),
            app=SimpleNamespace(
                state=SimpleNamespace(
                    settings=SimpleNamespace(data_mode="remote")
                )
            ),
        )
        context = {
            "request": request,
            "portal_name": "Joker",
            "csrf_token": "test",
            "flashes": [],
            "current_user": SimpleNamespace(is_admin=False, username="customer"),
            "current_year": 2026,
            "request_id": "test-request",
            "theme": {"mode": "auto", "day_url": "", "night_url": ""},
            "group_id": "766637236",
            "group_prefix": "/user/groups",
            "access": SimpleNamespace(),
            "can_edit": True,
            "can_view_console": True,
            "console_enabled": True,
            "console_events": [
                SimpleNamespace(
                    occurred_at=None,
                    direction="inbound",
                    user_id="100001",
                    user_name="Alice",
                    message_content="hello",
                    message_type="text",
                    action="receive",
                    message_id="",
                    detail="",
                )
            ],
            "console_pagination": {
                "total": 51,
                "page": 1,
                "page_count": 2,
                "has_previous": False,
                "has_next": True,
                "previous_url": "",
                "next_url": "/user/groups/766637236?console_page=2#console-log",
                "links": [
                    {
                        "number": 1,
                        "url": "/user/groups/766637236?console_page=1#console-log",
                        "current": True,
                    },
                    {
                        "number": 2,
                        "url": "/user/groups/766637236?console_page=2#console-log",
                        "current": False,
                    },
                ],
            },
            "console_filter": "",
            "console_user_id": "",
            "console_user_name": "",
            "console_start": "",
            "console_end": "",
            "console_filter_error": "",
            "console_keywords": [],
            "clone_sources": [],
            "group_command_groups": [
                {
                    "title": "统计与签到",
                    "items": [
                        {
                            "command": "/签到",
                            "usage": "签到",
                            "enabled": True,
                        }
                    ],
                }
            ],
            "config": {"group_id": "766637236", "features": {}},
            "powered_on": False,
            "features": _feature_context(),
            "auth": {"active": False, "expire_text": "无授权记录"},
            "timed_messages": [],
            "replies": [
                {
                    "key": "测试回复",
                    "type": "exact",
                    "reply": "你好 {name}",
                    "probability": 35,
                    "max_uses": 8,
                    "has_images": False,
                    "image_count": 0,
                    "image_status": "",
                }
            ],
            "sub_admins": [],
            "player_replies": [],
            "sync": {
                "online": False,
                "pending": False,
                "status": "never",
                "display_text": "尚未连通",
                "text": "尚未连通",
                "revision": 0,
                "applied_revision": 0,
                "badge": "尚未连通",
                "last_seen_text": "服务器尚未收到同步",
                "detail": "等待 Windows 连接",
                "refresh_requested": False,
            },
        }

        rendered = template.render(**context)
        self.assertIn("群 766637236", rendered)
        self.assertIn("/签到", rendered)
        self.assertIn('name="console_user_id"', rendered)
        self.assertIn('name="console_start"', rendered)
        self.assertIn('action="/user/groups/766637236#console-log"', rendered)
        self.assertIn("console_page=2#console-log", rendered)
        self.assertIn("data-searchable-list", rendered)
        self.assertIn('class="group-nav-tree"', rendered)
        self.assertIn('data-sync-sidebar-badge', rendered)
        self.assertIn('data-sync-sidebar-text', rendered)
        self.assertIn('action="/user/groups/766637236/delete"', rendered)
        self.assertIn("恢复默认指令", rendered)
        self.assertIn("恢复默认游戏配置", rendered)
        self.assertNotIn("删除自定义指令", rendered)
        self.assertNotIn("group-nav-branch", rendered)
        self.assertIn("Alice", rendered)
        # These four features own management panels, so rendering them again
        # as generic cards would present duplicate settings to customers.
        self.assertEqual(
            rendered.count("feature-card"),
            len(FEATURE_BY_KEY) - len(MANAGED_FEATURE_KEYS),
        )
        for feature_key in MANAGED_FEATURE_KEYS:
            self.assertEqual(
                rendered.count(f'data-group-section-panel="feature-{feature_key}"'),
                1,
            )
        self.assertIn('value="reply" data-sub-admin-permission', rendered)
        self.assertIn("回复管理", rendered)
        self.assertIn('name="probability"', rendered)
        self.assertIn('name="max_uses"', rendered)
        self.assertIn("触发概率", rendered)
        self.assertIn("最大触发次数", rendered)
        self.assertIn("概率：35%", rendered)
        self.assertIn("最大触发：8", rendered)
        self.assertIn('data-reply-probability="35"', rendered)
        self.assertIn('data-reply-max-uses="8"', rendered)
        self.assertIn("每条规则最多 20 张图片", rendered)
        self.assertIn('data-group-section-panel="feature-lottery"', rendered)
        # Collection-heavy functions use nested navigation and their own
        # one-item-at-a-time panels.  The blacklist runtime table must remain
        # inside the blacklist subtree instead of becoming a global data tab.
        self.assertIn('class="group-nav-subtree group-nav-category"', rendered)
        self.assertIn('data-group-section-switch="blacklist-data"', rendered)
        self.assertIn('data-group-section-switch="signin-profiles"', rendered)
        self.assertIn('name="steam_reward_operation"', rendered)
        self.assertIn("Steam 奖励指令", rendered)
        self.assertNotIn("自定义说明</option>", rendered)
        self.assertIn('data-group-section-switch="lottery-awards"', rendered)
        self.assertIn('data-group-section-switch="lottery-campaigns"', rendered)
        for panel_key in ("signin-profiles", "lottery-awards", "lottery-campaigns"):
            self.assertIn(f'data-group-section-panel="{panel_key}"', rendered)
        blacklist_branch = rendered.index('class="group-nav-subtree group-nav-module"')
        blacklist_data = rendered.index('data-group-section-switch="blacklist-data"')
        member_branch_end = rendered.index("消息与回复")
        self.assertLess(blacklist_branch, blacklist_data)
        self.assertLess(blacklist_data, member_branch_end)
        self.assertIn("群显示名称", rendered)
        self.assertIn("群标签", rendered)
        self.assertIn("群备注", rendered)
        self.assertNotIn("4 MiB", rendered)
        self.assertNotIn("12 MiB", rendered)

    def test_group_schema_exposes_only_runtime_backed_choices(self):
        audit_fields = {field.key: field for field in FEATURE_BY_KEY["audit"].fields}
        self.assertEqual(
            {value for value, _label in audit_fields["approval_mode"].choices},
            {"code", "direct", "id", "math"},
        )
        keyword_filter_fields = {
            field.key: field for field in FEATURE_BY_KEY["keyword_filter"].fields
        }
        self.assertIn(
            "delete_warn",
            {value for value, _label in keyword_filter_fields["filter_action"].choices},
        )
        lottery_fields = {
            field.key: field for field in FEATURE_BY_KEY["lottery"].fields
        }
        self.assertEqual(
            set(lottery_fields),
            {
                "enabled",
                "keyword",
                "max_draws_per_user",
                "cooldown",
                "success_message",
                "empty_message",
                "campaigns",
            },
        )
        self.assertEqual(parse_field_value(lottery_fields["keyword"], "抽奖"), "抽奖")
        with self.assertRaisesRegex(ValueError, "命令名"):
            parse_field_value(lottery_fields["keyword"], "/抽奖")

    def test_reply_image_preview_matches_portal_rule_limit(self):
        script = (BASE_DIR / "web_client" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("slice(0, 20)", script)
        self.assertNotIn("slice(0, 6)", script)
        self.assertIn("selectedFiles.length > 20", script)
        self.assertIn("setCustomValidity", script)

    def test_group_sync_status_poll_has_client_deadline(self):
        script = (BASE_DIR / "web_client" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("AbortController", script)
        self.assertIn("controller.abort()", script)
        self.assertIn("8000", script)
        self.assertIn("状态读取超时", script)

    def test_group_collection_search_is_client_side_only(self):
        script = (BASE_DIR / "web_client" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("initGroupCollectionSearch", script)
        self.assertIn("[data-searchable-list]", script)
        self.assertIn(".group-section-panel .table-wrap:not(.console-table)", script)

    def test_timed_task_editor_uses_modal_and_supports_hourly_at_all(self):
        template = (BASE_DIR / "web_client" / "templates" / "group.html").read_text(
            encoding="utf-8"
        )
        script = (BASE_DIR / "web_client" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        stylesheet = (BASE_DIR / "web_client" / "static" / "app.css").read_text(
            encoding="utf-8"
        )

        self.assertIn('data-timed-task-editor-open="{{ task.id }}"', template)
        self.assertIn('data-timed-task-editor="{{ task.id }}"', template)
        self.assertIn("data-timed-at-all-help", template)
        self.assertNotIn('class="inline-edit-details"', template)
        self.assertIn("const canAtAll = isMessage || isHourly;", script)
        self.assertIn(".timed-task-modal-dialog", stylesheet)

    def test_admin_global_template_renders_dictionary_values_key(self):
        environment = _environment()
        template = environment.get_template("admin_global.html")
        rendered = template.render(
            request=SimpleNamespace(
                url=SimpleNamespace(path="/admin/global"),
                app=SimpleNamespace(
                    state=SimpleNamespace(
                        settings=SimpleNamespace(data_mode="local")
                    )
                ),
            ),
            portal_name="Joker",
            csrf_token="test",
            flashes=[],
            current_user=SimpleNamespace(is_admin=True, username="admin"),
            current_year=2026,
            request_id="test-request",
            theme={"mode": "auto", "day_url": "", "night_url": ""},
            group_count=1,
            updated_at="now",
            general_fields=[],
            general_values={},
            features=_feature_context(),
        )
        self.assertIn("全局功能默认值", rendered)
        self.assertEqual(rendered.count("feature-card"), len(FEATURE_BY_KEY))


if __name__ == "__main__":
    unittest.main()
