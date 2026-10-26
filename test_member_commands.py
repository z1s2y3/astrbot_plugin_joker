"""Regression checks for local member-name and group-title command wiring."""

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _decorated_command_names(function: ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call) or not decorator.args:
            continue
        target = decorator.func
        if not (
            isinstance(target, ast.Attribute)
            and target.attr == "command"
        ):
            continue
        first = decorator.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
    return names


class TestMemberCommandWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.functions = {
            node.name: node
            for node in ast.walk(cls.tree)
            if isinstance(node, ast.AsyncFunctionDef)
        }

    def test_member_name_command_updates_qq_card_and_local_name(self):
        command = self.functions["set_group_member_name_cmd"]
        self.assertIn("设置群员名称", _decorated_command_names(command))
        body = ast.get_source_segment(self.source, command) or ""
        self.assertIn('action="set_group_card"', body)
        self.assertIn("set_user_name(group_id, user_id, display_name)", body)
        self.assertIn("_is_admin_or_owner_async", body)
        self.assertIn("user_id = self._normalize_positive_id(user_id)", body)
        self.assertIn('_upsert_member_identity_rule(group_id, user_id, "name", display_name)', body)

    def test_group_title_command_uses_qq_platform_as_final_permission_check(self):
        command = self.functions["grant_group_title_cmd"]
        self.assertIn("授予称号", _decorated_command_names(command))
        body = ast.get_source_segment(self.source, command) or ""
        self.assertIn("_get_group_action_client", body)
        self.assertIn("_is_group_owner_async", body)
        self.assertIn("user_id = self._normalize_positive_id(user_id)", body)
        self.assertIn('action="set_group_special_title"', body)
        self.assertIn("_api_result_succeeded(result)", body)
        self.assertIn("_onebot_failure_detail", body)
        self.assertIn('_upsert_member_identity_rule(group_id, user_id, "title", title)', body)

    def test_title_policy_commands_are_separate_and_group_scoped(self):
        for name in (
            "set_self_group_title_cmd",
            "enable_auto_member_title_cmd",
            "disable_auto_member_title_cmd",
            "show_auto_member_title_cmd",
            "initialize_group_titles_cmd",
            "restore_default_member_title_cmd",
        ):
            self.assertIn(name, self.functions)
        enable_body = ast.get_source_segment(
            self.source, self.functions["enable_auto_member_title_cmd"]
        ) or ""
        disable_body = ast.get_source_segment(
            self.source, self.functions["disable_auto_member_title_cmd"]
        ) or ""
        init_body = ast.get_source_segment(
            self.source, self.functions["initialize_group_titles_cmd"]
        ) or ""
        self.assertIn("auto_member_title", enable_body)
        self.assertIn("auto_member_title", disable_body)
        self_body = ast.get_source_segment(
            self.source, self.functions["set_self_group_title_cmd"]
        ) or ""
        self.assertIn("自动授予称号", _decorated_command_names(self.functions["set_self_group_title_cmd"]))
        self.assertIn('action="set_group_special_title"', self_body)
        self.assertIn("_get_sender_id_from_event", self_body)
        self.assertIn('action="get_group_member_list"', init_body)
        self.assertIn('action="set_group_special_title"', init_body)
        self.assertIn("_onebot_failure_detail", init_body)
        restore_body = ast.get_source_segment(
            self.source, self.functions["restore_default_member_title_cmd"]
        ) or ""
        self.assertIn("_default_member_title_rules", restore_body)
        self.assertIn("auto_member_title", restore_body)
        self.assertIn("False", restore_body)

    def test_identity_clear_commands_require_qq_confirmation_before_rule_removal(self):
        name_command = self.functions["clear_group_member_name_cmd"]
        title_command = self.functions["clear_group_title_cmd"]
        self.assertIn("清除群员名称", _decorated_command_names(name_command))
        self.assertIn("清除群员称号", _decorated_command_names(title_command))
        name_body = ast.get_source_segment(self.source, name_command) or ""
        title_body = ast.get_source_segment(self.source, title_command) or ""
        self.assertIn('action="set_group_card"', name_body)
        self.assertIn('_remove_member_identity_rule(group_id, user_id, "name")', name_body)
        self.assertIn('action="set_group_special_title"', title_body)
        self.assertIn('_remove_member_identity_rule(group_id, user_id, "title")', title_body)

    def test_member_identity_commands_explain_packet_backend_failures(self):
        for name in (
            "set_group_member_name_cmd",
            "clear_group_member_name_cmd",
            "grant_group_title_cmd",
            "clear_group_title_cmd",
            "initialize_group_titles_cmd",
        ):
            body = ast.get_source_segment(self.source, self.functions[name]) or ""
            self.assertIn("_format_group_profile_failure", body)
        helper = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_format_group_profile_failure"
        )
        helper_body = ast.get_source_segment(self.source, helper) or ""
        self.assertIn("packetBackend", helper_body)
        self.assertIn("NapCat v3.6+", helper_body)

    def test_bot_owner_check_supports_adapter_owner_fallbacks(self):
        command = self.functions["_is_bot_group_owner_async"]
        body = ast.get_source_segment(self.source, command) or ""
        self.assertIn("_get_group_action_client", body)
        self.assertIn("owner_uin", body)
        self.assertIn('action="get_group_member_list"', body)

    def test_join_notice_remembers_the_event_client_before_identity_actions(self):
        handler = self.functions["_process_group_join_notice"]
        body = ast.get_source_segment(self.source, handler) or ""
        self.assertIn("_remember_group_bot(group_id, event)", body)

    def test_small_group_refresh_reconciles_stale_rows_without_deleting_unknowns(self):
        refresh = self.functions["_apply_portal_member_actions"]
        body = ast.get_source_segment(self.source, refresh) or ""
        self.assertIn("unknown_member_ids", body)
        self.assertIn("_member_info_explicitly_absent", body)
        self.assertIn("按实时成员详情清理群目录中的失效成员", body)
        helper = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_member_info_explicitly_absent"
        )
        helper_body = ast.get_source_segment(self.source, helper) or ""
        self.assertIn("not found", helper_body)
        self.assertIn("成员不存在", helper_body)

    def test_web_member_actions_keep_moderation_actions_outside_refresh_branch(self):
        refresh = self.functions["_apply_portal_member_actions"]
        body = ast.get_source_segment(self.source, refresh) or ""
        self.assertIn('elif action == "kick_member":', body)
        self.assertIn('action="set_group_kick"', body)
        self.assertIn("网页成员踢出", body)
        self.assertIn("到期=", body)

    def test_web_unmute_uses_typed_api_and_refreshes_member_state(self):
        refresh = self.functions["_apply_portal_member_actions"]
        body = ast.get_source_segment(self.source, refresh) or ""
        self.assertIn("call_group_ban", body)
        self.assertIn("member_clients", body)
        self.assertIn("_update_cached_member_mute_state", body)
        self.assertIn('"status": "ok"', body)
        self.assertIn("read_target_role", body)
        self.assertIn("目标成员是", body)
        self.assertIn("for attempt in range(3)", body)
        self.assertIn("接口返回成功但未确认成员已解禁", body)
        self.assertIn("unmute_confirmed is not True", body)
        self.assertIn('action="get_group_shut_list"', body)
        self.assertIn("retry_count = 2", body)

    def test_group_mute_commands_normalize_empty_success_and_deduplicate_bots(self):
        handler = self.functions["handle_mute_commands"]
        body = ast.get_source_segment(self.source, handler) or ""
        self.assertIn("_claim_group_moderation_command", body)
        self.assertIn("_event_allows_primary_group_bot", body)
        for name in ("_handle_mute", "_handle_unmute"):
            action_body = ast.get_source_segment(self.source, self.functions[name]) or ""
            self.assertIn("_normalize_action_result", action_body)

    def test_departed_member_quarantine_prevents_stale_refresh_rows(self):
        refresh = self.functions["_apply_portal_member_actions"]
        body = ast.get_source_segment(self.source, refresh) or ""
        self.assertIn("_recently_removed_members", body)
        self.assertIn("_remove_cached_group_member", body)
        self.assertIn("_clear_recently_removed_member", self.source)

    def test_explicit_member_refresh_never_uses_old_cache_as_success(self):
        refresh = self.functions["_apply_portal_member_actions"]
        body = ast.get_source_segment(self.source, refresh) or ""
        self.assertIn('if action == "refresh_members":', body)
        self.assertIn("未使用旧缓存覆盖成员目录", body)
        self.assertIn("Windows 实时群成员接口返回空列表", body)

    def test_steamid_gate_has_configurable_mute_and_operation_log(self):
        handler = self.functions["handle_unverified_steam_member"]
        body = ast.get_source_segment(self.source, handler) or ""
        helper = self.functions["_mute_unverified_steam_member"]
        helper_body = ast.get_source_segment(self.source, helper) or ""
        self.assertIn("steamid_verification_required", helper_body)
        self.assertIn("unverified_mute_duration", helper_body)
        self.assertIn("set_group_ban", helper_body)
        self.assertIn("bind_aliases", helper_body)
        self.assertIn("SteamID验证禁言", helper_body)
        self.assertIn("event.stop_event()", body)

    def test_steamid_prompt_never_falls_back_to_group_message(self):
        helper = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_prompt_steamid_submission"
        )
        body = ast.get_source_segment(self.source, helper) or ""
        self.assertNotIn("_send_group_msg_with_id", body)
        self.assertIn("未在群内公开提示", body)

    def test_join_and_leave_notices_schedule_live_member_refresh(self):
        for name in ("_process_group_join_notice", "handle_group_member_decrease"):
            body = ast.get_source_segment(self.source, self.functions[name]) or ""
            self.assertIn("_schedule_member_notice_refresh", body)

    def test_web_member_actions_deduplicate_and_use_one_moderation_client(self):
        refresh = self.functions["_apply_portal_member_actions"]
        body = ast.get_source_segment(self.source, refresh) or ""
        self.assertIn("unique_actions", body)
        self.assertIn("seen_action_keys", body)
        self.assertIn("ban_clients = [await select_moderation_client()]", body)
        self.assertIn("select_moderation_client", body)
        self.assertIn('action="get_group_member_info"', body)

    def test_group_answer_verification_keeps_steamid_private(self):
        handler = self.functions["_handle_answer_verification"]
        body = ast.get_source_segment(self.source, handler) or ""
        self.assertIn("steamid_pending", body)
        self.assertIn("_delete_msg", body)
        self.assertIn("_prompt_steamid_submission", body)

    def test_member_refresh_rejects_partial_live_directory(self):
        refresh = self.functions["_apply_portal_member_actions"]
        body = ast.get_source_segment(self.source, refresh) or ""
        self.assertIn("len(members) < expected_member_count", body)
        self.assertIn("未使用不完整数据覆盖成员目录", body)
        self.assertIn("best_records", body)

    def test_failed_portal_actions_are_consumed_once(self):
        source = (ROOT / "qqadmin" / "web_sync.py").read_text(encoding="utf-8")
        self.assertIn("网页群成员操作未成功但已消费", source)
        self.assertNotIn('merged["portal_actions"] = callback_result[:8]', source)
        self.assertIn("_portal_member_action_claims", self.source)
        self.assertIn("跳过已消费的网页群成员操作", self.source)
        self.assertIn('created_at = int(candidate.get("created_at", 0) or 0)', self.source)

    def test_normal_group_messages_apply_enabled_member_identity_policy(self):
        handler = self.functions["handle_message"]
        body = ast.get_source_segment(self.source, handler) or ""
        self.assertIn("sender_id = self._get_sender_id_from_event(event)", body)
        self.assertIn(
            "await self._apply_member_identity_policy(group_id, sender_id)",
            body,
        )

    def test_sub_admin_commands_use_adapter_tolerant_group_and_sender_ids(self):
        add_body = ast.get_source_segment(self.source, self.functions["add_sub_admin_cmd"]) or ""
        remove_body = ast.get_source_segment(self.source, self.functions["remove_sub_admin_cmd"]) or ""
        list_body = ast.get_source_segment(self.source, self.functions["list_sub_admins"]) or ""
        for body in (add_body, remove_body, list_body):
            self.assertIn("self._get_group_id_from_event(event)", body)
        self.assertIn("self._get_sender_id_from_event(event)", add_body)
        self.assertIn("self._normalize_positive_id(parts[1])", add_body)
        self.assertIn("self._normalize_positive_id(parts[1])", remove_body)


if __name__ == "__main__":
    unittest.main()
