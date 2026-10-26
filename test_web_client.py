import asyncio
import os
import json
import runpy
import sys
import tempfile
import types
import unittest
import zipfile
import hashlib
import hmac
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateTable

from web_client.database import Base, _migrate_database
from web_client import remote_store, reply_asset_service, theme_service
from web_client.app import (
    ADMIN_SESSION_COOKIE,
    USER_SESSION_COOKIE,
    create_app,
    _extract_sync_client_id,
    _group_edit_status,
    _process_sync_ack,
    _process_sync_exchange,
    _request_group_page_refresh_once,
    _session_cookie_for_scope,
)
from web_client.feature_schema import (
    FEATURE_BY_KEY,
    default_database_profiles,
    default_steam_signin_profiles,
    display_value,
    parse_field_value,
)
from feature_catalog import CONFIGURABLE_FEATURES
from web_client.global_config import (
    get_general_values,
    get_global_feature_values,
    load_global_config,
    update_general_values,
    update_global_feature_values,
)
from web_client.models import (
    EmailVerification,
    GameAuthorization,
    GroupAccess,
    GroupConfigSnapshot,
    Im3Authorization,
    PortalConsoleEvent,
    PortalFeedback,
    PortalReplyAsset,
    PortalSetting,
    PortalThemeAsset,
    PortalThemePreference,
    User,
    utcnow,
)
from web_client.sync_projection import merge_group_projection, project_group_config
from web_client.security import (
    card_pepper_proof,
    card_fingerprint,
    game_card_fingerprint,
    hash_password,
    mask_card,
    normalize_card,
    normalize_game_card,
    validate_email,
    validate_group_id,
    validate_root_relative_redirect,
    validate_username,
    verify_password,
)
from web_client.services import (
    PortalError,
    _plugin_module,
    apply_feature_values,
    can_remove_admin,
    claim_activated_card,
    clone_group_config,
    create_admin_account,
    delete_unused_license_key,
    generate_admin_license_keys,
    get_admin_license_overview,
    get_group_view,
    get_feature_values,
    get_user_group,
    _list_reply_details,
    _list_player_reply_details,
    user_has_login_entitlement,
    _safe_sync_state,
    _normalize_runtime_panels,
)
from web_client.settings import Settings
from web_client.email_service import (
    PASSWORD_RESET_PURPOSE,
    VerificationCodeError,
    consume_password_reset_code,
    consume_registration_code,
    request_password_reset_code,
    request_registration_code,
    get_feedback_recipient,
    save_email_config,
)
from web_client.integration_service import (
    _normalize_database_file_path,
    _normalize_remote_path,
    _path_within_ftp_root,
    create_business_database_engine,
    get_integration_config_view,
    save_integration_config,
    save_ftp_database_selection,
    test_integration_connection,
)
from web_client.runner_interface import (
    get_runner_deployment,
    set_linux_migration_prepared,
)
from web_client.release_service import get_installed_release_info
from web_client.self_update import (
    MAX_ARCHIVE_BYTES,
    MAX_UPDATE_REQUEST_BYTES,
    PENDING_RELEASE_VERSION,
    discard_staged_update,
    queue_staged_update,
    stage_update_package,
)


class FakeGroupConfig:
    def __init__(self):
        self.configs = {}

    def get_group_config(self, group_id):
        return self.configs.setdefault(
            str(group_id),
            {
                "group_id": str(group_id),
                "features": {},
            },
        )

    def batch_update_group_data(self, group_id, updates):
        config = self.get_group_config(group_id)
        for key, updater in updates:
            config[key] = updater(config.get(key))
        return config

    def update_group_data(self, group_id, key, updater):
        config = self.get_group_config(group_id)
        config[key] = updater(config.get(key))
        return config[key]


class FakeAuthManager:
    def __init__(self, key, group_id="1102579807", active=True):
        self.used = {
            key: {
                "group_id": group_id,
                "activated_at": 1_800_000_000,
                "expire": 1_900_000_000,
                "user_id": "2650362143",
            }
        }
        self.groups = {
            group_id: {
                "key": key,
                "expire": 1_900_000_000 if active else 1,
                "user_id": "2650362143",
            }
        }
        self.unused = []

    def get_used_keys(self):
        return self.used

    def get_group_auth_info(self, group_id):
        return self.groups.get(str(group_id), {})

    def get_group_auth_state(self, group_id):
        info = self.get_group_auth_info(group_id)
        return "active" if int(info.get("expire", 0)) > 1 else "expired"

    def get_unused_keys(self):
        return list(self.unused)

    def generate_license_key(self, _auth_key, days, _group_id="", index=0):
        return f"JK{days}-1234567890123{index:012d}"

    def add_unused_keys(self, keys):
        self.unused.extend(key for key in keys if key not in self.unused)

    def remove_unused_key(self, key):
        self.unused.remove(key)


class SecurityTests(unittest.TestCase):
    def test_runtime_summary_keeps_blacklist_counters_and_operation_categories(self):
        panels = _normalize_runtime_panels(
            {
                "global_blacklist": {
                    "available": True,
                    "total": 4,
                    "active": 3,
                    "permanent": 2,
                    "temporary": 1,
                    "inactive": 1,
                    "entries": [{"user_id": "12345", "active": True}],
                },
                "operations": {
                    "available": True,
                    "recent": [{"action": "禁言", "category": "成员处理"}],
                    "categories": {"成员处理": [{"action": "禁言"}]},
                },
            }
        )

        self.assertEqual(3, panels["global_blacklist"]["active"])
        self.assertEqual(1, len(panels["global_blacklist"]["entries"]))
        self.assertEqual(
            "成员处理", panels["operations"]["recent"][0]["category"]
        )
        self.assertEqual(
            1, len(panels["operations"]["categories"]["成员处理"])
        )

    def test_runtime_summary_keeps_admin_global_blacklist_panel(self):
        panels = remote_store._safe_runtime_summary(
            {
                "global_blacklist_admin": {
                    "total": 1,
                    "active": 1,
                    "permanent": 1,
                    "entries": [
                        {
                            "user_id": "12345",
                            "active": True,
                            "operator_id": "web:1",
                            "allowed_groups": ["1102579807"],
                        }
                    ],
                }
            }
        )

        entry = panels["global_blacklist_admin"]["entries"][0]
        self.assertEqual(1, panels["global_blacklist_admin"]["total"])
        self.assertEqual("web:1", entry["operator_id"])
        self.assertEqual(["1102579807"], entry["allowed_groups"])

    def test_global_blacklist_portal_action_projection_keeps_details(self):
        projected = project_group_config(
            {
                "group_id": "1102579807",
                "portal_actions": [
                    {
                        "action": "add_global_blacklist",
                        "request_id": "a" * 32,
                        "created_at": int(time.time()),
                        "user_id": "12345",
                        "reason": "网页管理操作",
                        "ban_type": "temporary",
                        "duration": 86400,
                    }
                ],
            }
        )

        action = projected["portal_actions"][0]
        self.assertEqual("add_global_blacklist", action["action"])
        self.assertEqual("网页管理操作", action["reason"])
        self.assertEqual("temporary", action["ban_type"])
        self.assertEqual(86400, action["duration"])

    def test_runtime_message_normalizer_preserves_cross_adapter_dedupe_key(self):
        panels = remote_store._safe_runtime_summary(
            {
                "message_cache": {
                    "records": [
                        {
                            "timestamp": 1760000000,
                            "user_id": "2650362143",
                            "nickname": "QQ昵称",
                            "message_id": "adapter-local-id",
                            "dedupe_key": "same-qq-message-across-adapters",
                            "content": "测试消息",
                        }
                    ]
                }
            }
        )
        record = panels["message_cache"]["records"][0]
        self.assertEqual(
            "same-qq-message-across-adapters", record["dedupe_key"]
        )
        self.assertEqual("QQ昵称", record["nickname"])
        self.assertEqual("QQ昵称", record["user_name"])

    def test_runtime_message_mirror_dedupes_different_receiver_accounts(self):
        records = []
        seen = set()
        mirror_sources = {}
        first = {
            "timestamp": 1760000000,
            "user_id": "2650362143",
            "message_id": "adapter-a-id",
            "dedupe_key": "adapter-a-key",
            "mirror_key": "same-content-fingerprint",
            "source_account_id": "958856515",
            "content": "测试消息",
        }
        mirrored = dict(first)
        mirrored.update(
            message_id="adapter-b-id",
            dedupe_key="adapter-b-key",
            source_account_id="123456789",
        )
        self.assertTrue(
            remote_store._append_runtime_message(
                records, seen, mirror_sources, first
            )
        )
        self.assertFalse(
            remote_store._append_runtime_message(
                records, seen, mirror_sources, mirrored
            )
        )
        self.assertEqual(1, len(records))

    def test_game_card_is_separate_from_group_card(self):
        game_card = "JG30-1234567890123ABCDEF123456"
        self.assertEqual(normalize_game_card(game_card), game_card)
        self.assertNotEqual(
            game_card_fingerprint(game_card, "p" * 32),
            card_fingerprint("JK30-1234567890123ABCDEF123456", "p" * 32),
        )
        with self.assertRaises(ValueError):
            normalize_game_card("JK30-1234567890123ABCDEF123456")

    def test_sync_state_compatibility_fallback_returns_template_safe_mapping(self):
        """A legacy/malformed snapshot must not make group.html receive None."""

        snapshot = types.SimpleNamespace(
            revision="7",
            applied_revision="3",
            refresh_requested=True,
        )
        with patch(
            "web_client.services.remote_store.sync_state",
            side_effect=RuntimeError("legacy nullable snapshot"),
        ):
            state = _safe_sync_state(snapshot, 180)

        self.assertIsInstance(state, dict)
        self.assertEqual(state["status"], "unknown")
        self.assertTrue(state["pending"])
        self.assertTrue(state["refresh_requested"])
        self.assertIn("display_text", state)

    def test_remote_group_edits_require_a_recent_windows_heartbeat(self):
        """Offline projections stay viewable but must not accept a new write."""

        snapshot = object()
        db = MagicMock()
        with (
            patch("web_client.app.remote_data_mode", return_value=True),
            patch(
                "web_client.app.authorization_state",
                return_value={"active": True},
            ),
            patch("web_client.app.remote_store.get_snapshot", return_value=snapshot),
            patch(
                "web_client.app.remote_store.sync_state",
                return_value={"online": False},
            ),
        ):
            editable, reason = _group_edit_status("1102579807", db)
        self.assertTrue(editable)
        self.assertEqual(reason, "")

        with (
            patch("web_client.app.remote_data_mode", return_value=True),
            patch(
                "web_client.app.authorization_state",
                return_value={"active": True},
            ),
        ):
            self.assertEqual(
                (True, ""),
                _group_edit_status(
                    "1102579807", db, sync={"online": True}
                ),
            )

        with (
            patch("web_client.app.remote_data_mode", return_value=True),
            patch(
                "web_client.app.authorization_state",
                return_value={"active": True},
            ),
        ):
            editable, reason = _group_edit_status(
                "1102579807", db, sync={"online": True, "status": "protected"}
            )
        self.assertFalse(editable)
        self.assertIn("读取不完整", reason)

    def test_group_page_explains_that_offline_web_edits_are_queued(self):
        template = (Path(__file__).parent / "web_client" / "templates" / "group.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("网页配置仍可保存", template)
        self.assertIn("Windows 恢复通联后自动拉取并应用", template)

    def test_admin_and_customer_session_cookie_names_are_route_scoped(self):
        self.assertEqual(
            _session_cookie_for_scope({"path": "/admin", "headers": []}),
            ADMIN_SESSION_COOKIE,
        )
        self.assertEqual(
            _session_cookie_for_scope({"path": "/user/groups/12345", "headers": []}),
            USER_SESSION_COOKIE,
        )
        self.assertEqual(
            _session_cookie_for_scope(
                {
                    "path": "/theme/assets/4",
                    "headers": [(b"referer", b"https://portal.example/admin/appearance")],
                }
            ),
            ADMIN_SESSION_COOKIE,
        )

    def test_group_page_open_does_not_queue_a_windows_refresh(self):
        """Loading or reloading a page must not create sync traffic."""
        request = types.SimpleNamespace(
            state=types.SimpleNamespace(request_id="test-refresh"),
            headers={},
            client=types.SimpleNamespace(host="127.0.0.1"),
        )
        user = types.SimpleNamespace(id=7)
        db = MagicMock()
        snapshot = types.SimpleNamespace(refresh_requested=False)

        with patch("web_client.app.remote_data_mode", return_value=True), patch(
            "web_client.app.remote_store.get_snapshot", return_value=snapshot
        ), patch(
            "web_client.app.remote_store.request_group_refresh",
            side_effect=lambda _db, _groups: (
                setattr(snapshot, "refresh_requested", True) or 1
            ),
        ) as request_refresh, patch("web_client.app.add_audit_log") as add_log:
            self.assertFalse(
                _request_group_page_refresh_once(request, db, user, "1102579807")
            )
            self.assertFalse(
                _request_group_page_refresh_once(request, db, user, "1102579807")
            )

        request_refresh.assert_not_called()
        add_log.assert_not_called()
        db.commit.assert_not_called()
        source = (Path(__file__).parent / "web_client" / "app.py").read_text(
            encoding="utf-8"
        )
        group_detail_start = source.index("    def group_detail(")
        group_detail_end = source.index("\n    @app.post(\"/user/groups/{group_id}/clone\")", group_detail_start)
        self.assertNotIn(
            "_request_group_page_refresh_once(",
            source[group_detail_start:group_detail_end],
        )
        self.assertEqual(
            _session_cookie_for_scope(
                {
                    "path": "/",
                    "headers": [
                        (
                            b"cookie",
                            f"{ADMIN_SESSION_COOKIE}=signed".encode("ascii"),
                        )
                    ],
                }
            ),
            ADMIN_SESSION_COOKIE,
        )

    def test_navigation_busy_state_does_not_disable_group_links(self):
        """Busy styling must not cancel the browser's native page navigation."""
        stylesheet = (Path(__file__).parent / "web_client" / "static" / "app.css").read_text(
            encoding="utf-8"
        )
        self.assertIn('a[aria-busy="true"] { opacity: .68; }', stylesheet)
        self.assertNotIn(
            'a[aria-busy="true"] { opacity: .68; pointer-events: none; }',
            stylesheet,
        )

    def test_sync_client_identity_requires_safe_matching_id(self):
        def request(headers=None):
            return types.SimpleNamespace(headers=headers or {})

        client_id, error = _extract_sync_client_id(
            request({"x-joker-client": "windows-a"}),
            {"client_id": "windows-a"},
        )
        self.assertEqual(client_id, "windows-a")
        self.assertIsNone(error)

        client_id, error = _extract_sync_client_id(request(), {})
        self.assertIsNone(client_id)
        self.assertEqual(error.status_code, 400)
        self.assertIn(b'"code":"sync_client_missing"', error.body)

        client_id, error = _extract_sync_client_id(
            request({"x-joker-client": "windows-a"}),
            {"client_id": "windows-b"},
        )
        self.assertIsNone(client_id)
        self.assertEqual(error.status_code, 400)
        self.assertIn(b'"code":"sync_client_mismatch"', error.body)

        for value in ("../windows-a", 123):
            client_id, error = _extract_sync_client_id(
                request(), {"client_id": value}
            )
            self.assertIsNone(client_id)
            self.assertEqual(error.status_code, 400)
            self.assertIn(b'"code":"sync_client_invalid"', error.body)

        # A blank payload source is treated as omitted when the header
        # supplies the valid identity, preserving compatibility with old
        # clients that only sent the header.
        client_id, error = _extract_sync_client_id(
            request({"x-joker-client": "windows-a"}), {"client_id": ""}
        )
        self.assertEqual(client_id, "windows-a")
        self.assertIsNone(error)

    def test_application_redirects_are_always_same_origin_paths(self):
        self.assertEqual(
            validate_root_relative_redirect("/user/login?next=dashboard#top"),
            "/user/login?next=dashboard#top",
        )

        for target in (
            "https://183.136.132.24/user",
            "http://origin.example/user",
            "//183.136.132.24/user",
            r"/\183.136.132.24/user",
            "user/login",
        ):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    validate_root_relative_redirect(target)

    def test_password_round_trip(self):
        stored = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", stored))
        self.assertFalse(verify_password("wrong password", stored))

    def test_identity_validation(self):
        self.assertEqual(validate_username("Demo_User"), "demo_user")
        self.assertEqual(validate_email(" Demo@Example.com "), "demo@example.com")
        self.assertEqual(validate_group_id("1102579807"), "1102579807")
        with self.assertRaises(ValueError):
            validate_username("bad name")
        with self.assertRaises(ValueError):
            validate_group_id("../../auth")
        with self.assertRaises(ValueError):
            validate_email("not-an-email")

    def test_card_fingerprint_does_not_expose_card(self):
        card = "JK30-1234567890123ABCDEF123456"
        fingerprint = card_fingerprint(card, "pepper")
        self.assertNotIn(card, fingerprint)
        self.assertIn("****", mask_card(card))

    def test_card_normalization_accepts_supported_formats_only(self):
        current = "JK30-1234567890123ABCDEF123456"
        permanent = "JK-1-1234567890123ABCDEF123456"
        legacy = "JK301234567890123ABCDEF123456"
        for card in (current, permanent, legacy):
            with self.subTest(card=card):
                self.assertEqual(normalize_card(card.lower()), card)

        for card in ("JK30-not-a-card", "JK30-1234567890123NOTHEX123456", "MANUAL_1"):
            with self.subTest(card=card):
                with self.assertRaisesRegex(ValueError, "卡密格式不正确"):
                    normalize_card(card)

        self.assertIn("****", mask_card("JK30-not-a-card"))

    def test_card_pepper_proof_uses_a_domain_separated_hmac(self):
        pepper = "p" * 48
        expected = hmac.new(
            pepper.encode("utf-8"),
            b"joker-portal-card-pepper-proof-v1",
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(card_pepper_proof(pepper), expected)
        self.assertNotEqual(card_pepper_proof(pepper), card_pepper_proof("q" * 48))

    def test_settings_reject_placeholder_secrets(self):
        with patch.dict(
            os.environ,
            {
                "JOKER_WEB_SESSION_SECRET": "replace_with_a_long_random_session_secret",
                "JOKER_WEB_CARD_PEPPER": "replace_with_a_different_long_random_card_pepper",
            },
            clear=True,
        ):
            with self.assertRaises(RuntimeError):
                Settings.from_env()

    def test_remote_mode_requires_sync_token(self):
        with patch.dict(
            os.environ,
            {
                "JOKER_WEB_SESSION_SECRET": "s" * 48,
                "JOKER_WEB_CARD_PEPPER": "p" * 48,
                "JOKER_WEB_DATA_MODE": "remote",
                "JOKER_WEB_SYNC_TOKEN": "short",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "SYNC_TOKEN"):
                Settings.from_env()

    def test_custom_admin_path_is_validated_and_loaded(self):
        with patch.dict(
            os.environ,
            {
                "JOKER_WEB_SESSION_SECRET": "s" * 48,
                "JOKER_WEB_CARD_PEPPER": "p" * 48,
                "JOKER_WEB_ADMIN_PATH": "/control-panel",
            },
            clear=True,
        ):
            self.assertEqual("/control-panel", Settings.from_env().admin_path)
        with patch.dict(
            os.environ,
            {
                "JOKER_WEB_SESSION_SECRET": "s" * 48,
                "JOKER_WEB_CARD_PEPPER": "p" * 48,
                "JOKER_WEB_ADMIN_PATH": "/user",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "JOKER_WEB_ADMIN_PATH"):
                Settings.from_env()

    def test_custom_admin_path_routes_and_rewrites_admin_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "JOKER_WEB_DATABASE_URL": f"sqlite:///{Path(temp_dir) / 'portal.db'}",
                    "JOKER_WEB_SESSION_SECRET": "s" * 48,
                    "JOKER_WEB_CARD_PEPPER": "p" * 48,
                    "JOKER_WEB_ADMIN_PATH": "/control-panel",
                    "JOKER_WEB_DATA_MODE": "local",
                    "JOKER_WEB_SECURE_COOKIES": "false",
                },
                clear=True,
            ):
                with TestClient(create_app()) as client:
                    page = client.get("/control-panel/login")
                    self.assertEqual(200, page.status_code)
                    self.assertIn('/control-panel/login', page.text)
                    self.assertNotIn('href="/admin', page.text)
                    legacy = client.get("/admin/login", follow_redirects=False)
                    self.assertEqual(307, legacy.status_code)
                    self.assertEqual("/control-panel/login", legacy.headers["location"])
            from web_client.database import get_engine, get_session_factory

            get_session_factory.cache_clear()
            engine = get_engine()
            engine.dispose()
            get_engine.cache_clear()


class SyncWorkerSessionTests(unittest.TestCase):
    """The sync workers own their thread-local database transaction."""

    def test_exchange_worker_creates_commits_and_closes_its_own_session(self):
        session = MagicMock()
        session_factory = MagicMock(return_value=session)
        with (
            patch(
                "web_client.app.get_session_factory", return_value=session_factory
            ),
            patch(
                "web_client.app.remote_store.apply_bot_exchange",
                return_value=[{"group_id": "1102579807", "revision": 2}],
            ) as apply_exchange,
            patch(
                "web_client.app.remote_store.record_bot_sync_failures",
                return_value=1,
            ),
            patch(
                "web_client.app.get_console_settings",
                return_value={
                    "enabled": True,
                    "retention": 100,
                    "messages_enabled": True,
                },
            ),
            patch(
                "web_client.app.remote_store.ingest_console_events", return_value=1
            ),
            patch(
                "web_client.app.remote_store.missing_bot_group_ids",
                return_value=["568586484"],
            ),
            patch(
                "web_client.app.remote_store.requested_group_refresh_ids",
                return_value=["1102579807"],
            ),
            patch(
                "web_client.app.remote_store.bound_group_ids",
                return_value={"1102579807"},
            ),
            patch(
                "web_client.app.remote_store.claimable_group_ids",
                return_value={"568586484"},
            ),
            patch("web_client.app.get_runner_deployment", return_value={"enabled": False}),
        ):
            result = _process_sync_exchange(
                "windows-test",
                [],
                ["1102579807"],
                [],
                [],
                True,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["console_events"], 1)
        self.assertEqual(result["managed_group_ids"], ["1102579807"])
        self.assertEqual(result["claimable_group_ids"], ["568586484"])
        self.assertIn("game_executor", result)
        self.assertIn("game_websocket_enabled", result)
        self.assertEqual("/api/sync/v1/ws", result["game_websocket_endpoint_path"])
        self.assertIs(apply_exchange.call_args.args[0], session)
        session.commit.assert_called_once_with()
        session.rollback.assert_not_called()
        session.close.assert_called_once_with()

    def test_exchange_worker_never_routes_unbound_group_work(self):
        session = MagicMock()
        session_factory = MagicMock(return_value=session)
        bound = "1102579807"
        unbound = "1102579808"
        with (
            patch("web_client.app.get_session_factory", return_value=session_factory),
            patch(
                "web_client.app.remote_store.apply_bot_exchange",
                return_value=[
                    {"group_id": bound, "revision": 2},
                    {"group_id": unbound, "revision": 3},
                ],
            ),
            patch("web_client.app.remote_store.record_bot_sync_failures", return_value=0),
            patch(
                "web_client.app.get_console_settings",
                return_value={"enabled": False, "retention": 100, "messages_enabled": False},
            ),
            patch(
                "web_client.app.remote_store.missing_bot_group_ids",
                return_value=[bound, unbound],
            ),
            patch(
                "web_client.app.remote_store.requested_group_refresh_ids",
                return_value=[bound, unbound],
            ),
            patch("web_client.app.remote_store.bound_group_ids", return_value={bound}),
            patch("web_client.app.remote_store.claimable_group_ids", return_value={unbound}),
            patch("web_client.app.get_runner_deployment", return_value={"enabled": False}),
        ):
            result = _process_sync_exchange(
                "windows-test",
                [],
                [bound, unbound],
                [],
                [],
                False,
                {},
                True,
                True,
                True,
            )

        self.assertEqual([item["group_id"] for item in result["changes"]], [bound])
        self.assertEqual(result["missing_group_ids"], [bound])
        self.assertEqual(result["refresh_group_ids"], [bound])
        self.assertEqual(result["claimable_group_ids"], [unbound])

    def test_incomplete_source_exchange_is_heartbeat_only(self):
        """The server must not accept partial projections or consume hints."""
        session = MagicMock()
        session_factory = MagicMock(return_value=session)
        with (
            patch(
                "web_client.app.get_session_factory", return_value=session_factory
            ),
            patch(
                "web_client.app.remote_store.apply_bot_exchange", return_value=[]
            ) as apply_exchange,
            patch(
                "web_client.app.remote_store.record_bot_sync_failures", return_value=0
            ) as record_failures,
            patch(
                "web_client.app.remote_store.touch_client_heartbeat", return_value=1
            ) as touch_heartbeat,
            patch(
                "web_client.app.get_console_settings",
                return_value={
                    "enabled": False,
                    "retention": 100,
                    "messages_enabled": False,
                },
            ),
            patch(
                "web_client.app.remote_store.missing_bot_group_ids"
            ) as missing_groups,
            patch(
                "web_client.app.remote_store.requested_group_refresh_ids"
            ) as refresh_groups,
            patch("web_client.app.remote_store.bound_group_ids", return_value=set()),
            patch("web_client.app.remote_store.claimable_group_ids", return_value=set()),
            patch("web_client.app.get_runner_deployment", return_value={"enabled": False}),
        ):
            result = _process_sync_exchange(
                "windows-test",
                [
                    {
                        "group_id": "1102579807",
                        "config": {"group_id": "1102579807", "features": {"bad": True}},
                        "auth": {"state": "active"},
                    }
                ],
                ["1102579807"],
                [],
                [{"group_id": "1102579807", "revision": 1, "error": "bad"}],
                False,
                {"1102579807": {"stats": {"group": {"total_users": 99}}}},
            )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["source_protected"])
        self.assertEqual(result["missing_group_ids"], [])
        self.assertEqual(result["refresh_group_ids"], [])
        apply_exchange.assert_called_once()
        apply_args = apply_exchange.call_args
        self.assertEqual(apply_args.args[2], [])
        self.assertEqual(apply_args.args[3], [])
        self.assertEqual(apply_args.kwargs["runtime_summaries"], {})
        record_failures.assert_called_once_with(session, "windows-test", [])
        touch_heartbeat.assert_called_once_with(
            session, "windows-test", mark_auth_unverified=True
        )
        missing_groups.assert_not_called()
        refresh_groups.assert_not_called()
        session.commit.assert_called_once_with()
        session.close.assert_called_once_with()

    def test_sync_workers_roll_back_and_close_their_own_sessions_on_failure(self):
        exchange_session = MagicMock()
        ack_session = MagicMock()
        exchange_factory = MagicMock(return_value=exchange_session)
        ack_factory = MagicMock(return_value=ack_session)

        with patch(
            "web_client.app.get_session_factory",
            side_effect=[exchange_factory, ack_factory],
        ), patch(
            "web_client.app.remote_store.apply_bot_exchange",
            side_effect=RuntimeError("exchange failed"),
        ), patch(
            "web_client.app.remote_store.acknowledge_bot_changes",
            side_effect=RuntimeError("ack failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "exchange failed"):
                _process_sync_exchange("windows-test", [], [], [], [], False)
            with self.assertRaisesRegex(RuntimeError, "ack failed"):
                _process_sync_ack("windows-test", [])

        for session in (exchange_session, ack_session):
            with self.subTest(session=session):
                session.commit.assert_not_called()
                session.rollback.assert_called_once_with()
                session.close.assert_called_once_with()


class PortalRouteIsolationTests(unittest.TestCase):
    """Keep the two browser roles separate at the route and template boundary."""

    def test_customer_only_routes_use_the_customer_guard(self):
        source = (Path(__file__).parent / "web_client" / "app.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def _require_customer(", source)
        for function_name in (
            "def dashboard(",
            "async def request_all_user_group_sync_refresh(",
            "def user_feedback(",
            "async def submit_user_feedback(",
            "async def claim_license(",
        ):
            with self.subTest(function=function_name):
                start = source.index(function_name)
                end = source.find("\n    @app.", start + 1)
                block = source[start:] if end == -1 else source[start:end]
                self.assertIn("_require_customer(request, db)", block)

    def test_update_status_is_admin_scoped_and_templates_are_distinct(self):
        source = (Path(__file__).parent / "web_client" / "app.py").read_text(
            encoding="utf-8"
        )
        route_start = source.index('@app.get("/admin/update/status")')
        route_end = source.find("\n    @app.post(\"/admin/update\")", route_start)
        self.assertNotEqual(route_end, -1)
        self.assertIn("_require_admin(request, db)", source[route_start:route_end])
        self.assertIn('response.headers["Cache-Control"] = "no-store"', source[route_start:route_end])

        admin_template = (
            Path(__file__).parent / "web_client" / "templates" / "admin_base.html"
        ).read_text(encoding="utf-8")
        user_template = (
            Path(__file__).parent / "web_client" / "templates" / "user_base.html"
        ).read_text(encoding="utf-8")
        self.assertIn("管理员导航", admin_template)
        self.assertIn("客户导航", user_template)
        self.assertNotIn("/admin/update", user_template)

    def test_authenticated_html_is_not_cached_and_group_sync_has_manual_refresh(self):
        source = (Path(__file__).parent / "web_client" / "app.py").read_text(
            encoding="utf-8"
        )
        middleware_start = source.index('    @app.middleware("http")')
        middleware_end = source.index("\n    @app.exception_handler", middleware_start)
        middleware = source[middleware_start:middleware_end]
        self.assertIn('response.headers["Cache-Control"] = "private, no-store"', middleware)

        group_template = (
            Path(__file__).parent / "web_client" / "templates" / "group.html"
        ).read_text(encoding="utf-8")
        self.assertIn('href="{{ group_prefix }}/{{ group_id }}#sync-flow-title"', group_template)
        self.assertIn("刷新状态", group_template)

    def test_account_and_appearance_routes_are_role_scoped(self):
        source = (Path(__file__).parent / "web_client" / "app.py").read_text(
            encoding="utf-8"
        )
        for function_name in (
            "async def change_password(",
            "def appearance_settings(",
            "async def save_appearance_settings(",
        ):
            with self.subTest(function=function_name):
                start = source.index(function_name)
                end = source.find("\n    @app.", start + 1)
                block = source[start:] if end == -1 else source[start:end]
                self.assertIn("_require_customer(request, db)", block)
                self.assertIn("_require_admin(request, db)", block)


class FeatureSchemaTests(unittest.TestCase):
    def test_audit_offers_id_and_direct_approval(self):
        field = next(
            item for item in FEATURE_BY_KEY["audit"].fields if item.key == "approval_mode"
        )
        choices = {value for value, _label in field.choices}
        self.assertTrue({"id", "direct", "code", "math"}.issubset(choices))
        self.assertEqual(parse_field_value(field, "id"), "id")
        self.assertEqual(parse_field_value(field, "direct"), "direct")

    def test_stats_defaults_require_a_group_ftp_database_archive(self):
        fields = {field.key: field for field in FEATURE_BY_KEY["stats"].fields}
        self.assertEqual(
            default_steam_signin_profiles(),
            fields["signin_profiles"].default,
        )
        self.assertEqual(
            default_database_profiles(),
            fields["database_profiles"].default,
        )
        self.assertFalse(fields["signin_profiles"].default[0]["enabled"])
        self.assertEqual([], fields["database_profiles"].default)
        self.assertEqual("", fields["database_path"].default)
        self.assertEqual(
            "SteamID 绑定失败：{message}",
            fields["bind_failure_message"].default,
        )

    def test_stats_default_examples_render_as_editable_json_lines(self):
        from web_client import services

        fake = FakeGroupConfig()
        with patch.object(services, "group_config_module", return_value=fake):
            values = services.get_feature_values("1102579807", "stats")

        self.assertIn('"id":"steam-daily-example"', values["signin_profiles"][0])
        self.assertEqual([], values["database_profiles"])

    def test_recall_page_exposes_sensitive_detection_policy(self):
        keys = {field.key for field in FEATURE_BY_KEY["recall"].fields}
        self.assertTrue({"enable_admin_recall", "enable_sensitive_detection", "sensitive_words"}.issubset(keys))

    def test_group_config_feature_is_exposed_to_linux_portal(self):
        self.assertIn("group_config", FEATURE_BY_KEY)
        self.assertEqual(FEATURE_BY_KEY["group_config"].label, "群设置")
        self.assertEqual(
            [field.key for field in FEATURE_BY_KEY["group_config"].fields],
            [
                "enabled",
                "help_command",
                "help_permission",
                "nickname",
                "database_player_name",
                "steam_name_as_group_card",
                "tags",
                "priority",
                "bot_account_priorities",
                "group_note",
                "timezone",
                "auto_member_name",
                "member_name_prefix",
                "member_name_prefix_include_admins",
                "member_names",
                "auto_member_title",
                "member_titles",
            ],
        )
        fields = {field.key: field for field in FEATURE_BY_KEY["group_config"].fields}
        self.assertEqual("", fields["member_name_prefix"].default)
        self.assertTrue(fields["member_name_prefix_include_admins"].default)
        self.assertEqual(["*=群成员"], fields["member_titles"].default)

    def test_group_config_save_mirrors_visible_metadata_to_windows_root(self):
        fake = FakeGroupConfig()
        values = {
            field.key: field.default for field in FEATURE_BY_KEY["group_config"].fields
        }
        values.update(
            {
                "enabled": True,
                "nickname": "活动管理群",
                "tags": ["活动群", "主群"],
                "priority": 12,
                "group_note": "仅管理员可见",
                "timezone": "Asia/Shanghai",
            }
        )
        with patch("web_client.services.group_config_module", return_value=fake):
            apply_feature_values("1102579807", "group_config", values)
            displayed = get_feature_values("1102579807", "group_config")

        config = fake.configs["1102579807"]
        self.assertEqual(config["nickname"], "活动管理群")
        self.assertEqual(config["tags"], ["活动群", "主群"])
        self.assertEqual(config["priority"], 12)
        self.assertEqual(config["group_note"], "仅管理员可见")
        self.assertEqual(config["timezone"], "Asia/Shanghai")
        self.assertEqual(displayed["nickname"], "活动管理群")

    def test_generic_stats_save_normalizes_hidden_signin_profiles(self):
        fake = FakeGroupConfig()
        values = {
            field.key: field.default for field in FEATURE_BY_KEY["stats"].fields
        }
        values["signin_profiles"] = [
            '{"id":"daily","name":"每日签到","command":"签到1","mode":"basic","schedule":"daily","base_reward":20}'
        ]
        with patch("web_client.services.group_config_module", return_value=fake):
            apply_feature_values("1102579807", "stats", values)

        profiles = fake.configs["1102579807"]["features"]["stats"]["signin_profiles"]
        self.assertEqual(profiles[0]["id"], "daily")
        self.assertEqual(profiles[0]["command"], "签到1")
        self.assertEqual(profiles[0]["base_reward"], 20)
        self.assertIsInstance(profiles[0], dict)

        duplicate = dict(values)
        duplicate["signin_profiles"] = [
            '{"id":"a","name":"方案一","command":"同一命令"}',
            '{"id":"b","name":"方案二","command":"同一命令"}',
        ]
        with patch("web_client.services.group_config_module", return_value=fake):
            with self.assertRaisesRegex(ValueError, "不能重复"):
                apply_feature_values("1102579807", "stats", duplicate)

    def test_signin_profile_actions_add_update_and_delete(self):
        from web_client import services

        fake = FakeGroupConfig()
        with patch.object(services, "remote_data_mode", return_value=False), patch.object(
            services, "group_config_module", return_value=fake
        ):
            services.apply_signin_profile_action(
                "1102579807",
                "add",
                profile_id="daily",
                name="Daily check-in",
                command="/daily-signin",
                mode="basic",
                schedule="daily",
                base_reward="20",
                reward_3_days="30",
                reward_7_days="70",
                reward_14_days="140",
                reward_30_days="300",
                signin_message="Welcome {name}",
            )
            profiles = services._signin_profiles_for_view(
                fake.configs["1102579807"]
            )
            self.assertEqual(1, len(profiles))
            self.assertEqual("daily-signin", profiles[0]["command"])
            self.assertEqual(20, profiles[0]["base_reward"])
            self.assertEqual(30, profiles[0]["reward_3_days"])
            self.assertEqual(70, profiles[0]["reward_7_days"])
            self.assertEqual(140, profiles[0]["reward_14_days"])
            self.assertEqual(300, profiles[0]["reward_30_days"])

            services.apply_signin_profile_action(
                "1102579807",
                "update",
                profile_id="daily",
                name="Weekly check-in",
                command="weekly-signin",
                mode="steam",
                schedule="weekly",
                enabled=False,
                base_reward="0",
                reward_3_days="0",
                reward_7_days="",
                reward_14_days="",
                reward_30_days="",
                signin_message="Checked in: {name}",
            )
            profiles = services._signin_profiles_for_view(
                fake.configs["1102579807"]
            )
            self.assertEqual("Weekly check-in", profiles[0]["name"])
            self.assertEqual("weekly-signin", profiles[0]["command"])
            self.assertEqual("steam", profiles[0]["mode"])
            self.assertEqual("weekly", profiles[0]["schedule"])
            self.assertFalse(profiles[0]["enabled"])
            self.assertEqual(0, profiles[0]["base_reward"])
            self.assertEqual(0, profiles[0]["reward_3_days"])
            self.assertNotIn("reward_7_days", profiles[0])

            with self.assertRaisesRegex(PortalError, "不能与当前签到或 SteamID 绑定命令重复"):
                services.apply_signin_profile_action(
                    "1102579807",
                    "add",
                    profile_id="bind-collision",
                    name="绑定冲突",
                    command="绑定SteamID",
                )

            services.apply_signin_profile_action(
                "1102579807", "delete", profile_id="daily"
            )
            self.assertEqual(
                [], services._signin_profiles_for_view(fake.configs["1102579807"])
            )

    def test_normal_stats_save_preserves_existing_signin_profiles(self):
        fake = FakeGroupConfig()
        config = fake.get_group_config("1102579807")
        config["features"]["stats"] = {
            "enabled": True,
            "signin_profiles": [
                {
                    "id": "daily",
                    "name": "Daily check-in",
                    "command": "daily-signin",
                    "mode": "basic",
                    "schedule": "daily",
                    "enabled": True,
                    "base_reward": 15,
                }
            ],
        }
        with patch("web_client.services.group_config_module", return_value=fake):
            values = get_feature_values("1102579807", "stats")
            values["enabled"] = False
            apply_feature_values("1102579807", "stats", values)

        stored = fake.configs["1102579807"]["features"]["stats"]
        self.assertFalse(stored["enabled"])
        self.assertEqual(1, len(stored["signin_profiles"]))
        self.assertEqual("daily", stored["signin_profiles"][0]["id"])
        self.assertEqual(15, stored["signin_profiles"][0]["base_reward"])

    def test_game_reset_restores_starters_and_preserves_group_connections(self):
        from web_client import services

        fake = FakeGroupConfig()
        config = fake.get_group_config("1102579807")
        config.update(
            {
                "steam_signin": {"bindings": {"76561199616499552": {"user_id": "42"}}},
                "signin": {"42": {"last_signin": "2026-08-24"}},
                "features": {
                    "stats": {
                        "enabled": False,
                        "database_profile": "main",
                        "database_profiles": [{"id": "main", "path": "data/main.db"}],
                        "signin_profiles": [{"id": "old", "command": "旧签到"}],
                    },
                    "game_commands": {"enabled": False, "commands": [{"id": "old"}]},
                    "game_servers": {"enabled": True, "profiles": [{"id": "main", "password": "secret"}]},
                },
            }
        )
        starters = [
            {"id": "currency", "trigger": "ChangeCurrencyBalance"},
            {"id": "spawnzombie", "trigger": "spawnzombie"},
        ]
        with patch.object(services, "remote_data_mode", return_value=False), patch.object(
            services, "group_config_module", return_value=fake
        ), patch.object(services, "_default_game_commands", return_value=starters):
            services.reset_game_configuration("1102579807")

        restored = fake.configs["1102579807"]
        features = restored["features"]
        self.assertTrue(features["stats"]["enabled"])
        self.assertEqual("steam", features["stats"]["signin_mode"])
        self.assertEqual("websocket", features["stats"]["steam_reward_mode"])
        self.assertEqual("main", features["stats"]["database_profile"])
        self.assertEqual(
            "steam-daily-example",
            features["stats"]["signin_profiles"][0]["id"],
        )
        self.assertFalse(features["stats"]["signin_profiles"][0]["enabled"])
        self.assertEqual(starters, features["game_commands"]["commands"])
        self.assertTrue(features["game_servers"]["enabled"])
        self.assertEqual("secret", features["game_servers"]["profiles"][0]["password"])
        self.assertIn("76561199616499552", restored["steam_signin"]["bindings"])
        self.assertEqual("2026-08-24", restored["signin"]["42"]["last_signin"])

    def test_lottery_campaign_and_nested_award_actions_round_trip(self):
        from web_client import services

        fake = FakeGroupConfig()
        with patch.object(services, "remote_data_mode", return_value=False), patch.object(
            services, "group_config_module", return_value=fake
        ):
            services.apply_lottery_campaign_action(
                "1102579807",
                "add",
                campaign_id="weekend",
                name="Weekend draw",
                keyword="weekend-draw",
                max_draws_per_user="2",
                cooldown="30",
                success_message="{name} won {prize}",
                empty_message="No prize",
            )
            campaign = fake.configs["1102579807"]["lottery"]["campaigns"][0]
            self.assertEqual("weekend", campaign["id"])
            self.assertEqual("weekend-draw", campaign["keyword"])

            services.apply_lottery_campaign_action(
                "1102579807",
                "update",
                campaign_id="weekend",
                name="Weekend special",
                keyword="weekend-special",
                enabled=False,
                max_draws_per_user="3",
                cooldown="45",
                success_message="{name}: {prize}",
                empty_message="Try later",
            )
            campaign = fake.configs["1102579807"]["lottery"]["campaigns"][0]
            self.assertEqual("Weekend special", campaign["name"])
            self.assertEqual("weekend-special", campaign["keyword"])
            self.assertFalse(campaign["enabled"])
            self.assertEqual(3, campaign["max_draws_per_user"])

            services.apply_lottery_award_action(
                "1102579807",
                "add",
                campaign_id="weekend",
                name="VIP pass",
                probability="25.5",
                stock="4",
                enabled=True,
            )
            campaign = fake.configs["1102579807"]["lottery"]["campaigns"][0]
            self.assertEqual(1, len(campaign["awards"]))
            award_id = campaign["awards"][0]["id"]

            services.apply_lottery_award_action(
                "1102579807",
                "update",
                campaign_id="weekend",
                award_id=award_id,
                name="VIP pass plus",
                probability="30",
                stock="2",
                enabled=True,
            )
            campaign = fake.configs["1102579807"]["lottery"]["campaigns"][0]
            self.assertEqual("VIP pass plus", campaign["awards"][0]["name"])
            self.assertEqual(30.0, campaign["awards"][0]["probability"])
            self.assertEqual(2, campaign["awards"][0]["stock"])

            services.apply_lottery_award_action(
                "1102579807",
                "delete",
                campaign_id="weekend",
                award_id=award_id,
            )
            campaign = fake.configs["1102579807"]["lottery"]["campaigns"][0]
            self.assertEqual([], campaign["awards"])

            services.apply_lottery_campaign_action(
                "1102579807", "delete", campaign_id="weekend"
            )
            self.assertEqual(
                [], fake.configs["1102579807"]["lottery"]["campaigns"]
            )

    def test_linux_feature_cards_cover_the_canonical_windows_catalog(self):
        """Every configurable Windows category must have a Linux form card."""
        catalog_keys = {category.key for category in CONFIGURABLE_FEATURES}
        self.assertEqual(catalog_keys, set(FEATURE_BY_KEY))

        message_cache_fields = {
            field.key for field in FEATURE_BY_KEY["message_cache"].fields
        }
        self.assertTrue(
            {"archive_images", "archive_image_max_mb"}.issubset(message_cache_fields)
        )
        text_to_image_fields = {
            field.key for field in FEATURE_BY_KEY["text_to_image"].fields
        }
        self.assertIn("modify_permission", text_to_image_fields)

    def test_list_and_regex_validation(self):
        field = next(
            item
            for item in FEATURE_BY_KEY["keyword_filter"].fields
            if item.key == "filter_keywords"
        )
        self.assertEqual(parse_field_value(field, "广告\n推广,广告"), ["广告", "推广"])

        regex_field = next(
            item
            for item in FEATURE_BY_KEY["keyword_filter"].fields
            if item.key == "filter_regex_patterns"
        )
        with self.assertRaises(ValueError):
            parse_field_value(regex_field, "([")

        self.assertEqual(display_value(field, "单条旧配置"), "单条旧配置")
        self.assertEqual(display_value(field, 123), "")

    def test_feature_update_mirrors_legacy_runtime_fields(self):
        fake = FakeGroupConfig()
        values = {
            field.key: field.default for field in FEATURE_BY_KEY["welcome"].fields
        }
        values["enabled"] = True
        values["message"] = "欢迎 {user_name}"
        with patch("web_client.services.group_config_module", return_value=fake):
            apply_feature_values("1102579807", "welcome", values)
            displayed = get_feature_values("1102579807", "welcome")

        config = fake.configs["1102579807"]
        self.assertTrue(config["features"]["welcome"]["enabled"])
        self.assertTrue(config["welcome_enabled"])
        self.assertEqual(config["welcome_message"], "欢迎 {user_name}")
        self.assertEqual(displayed["message"], "欢迎 {user_name}")

    def test_recall_web_scope_mirrors_explicit_runtime_scope(self):
        fake = FakeGroupConfig()
        values = {
            field.key: field.default for field in FEATURE_BY_KEY["recall"].fields
        }
        values["enable_admin_recall"] = False
        with patch("web_client.services.group_config_module", return_value=fake):
            apply_feature_values("1102579807", "recall", values)

        stored = fake.configs["1102579807"]["features"]["recall"]
        self.assertFalse(stored["enable_admin_recall"])
        self.assertEqual("members", stored["member_scope"])
        self.assertEqual("members", fake.configs["1102579807"]["recall_member_scope"])

    def test_recall_scope_round_trips_between_web_projection_and_windows(self):
        existing = {
            "group_id": "1102579807",
            "features": {
                "recall": {
                    "enabled": True,
                    "enable_admin_recall": False,
                    "member_scope": "members",
                }
            },
            "enable_admin_recall": False,
            "recall_member_scope": "members",
        }
        projected = project_group_config(existing)
        self.assertEqual("members", projected["recall_member_scope"])
        merged = merge_group_projection(
            existing,
            {
                "group_id": "1102579807",
                "features": {
                    "recall": {
                        "enabled": True,
                        "enable_admin_recall": True,
                        "member_scope": "all",
                    }
                },
                "enable_admin_recall": True,
                "recall_member_scope": "all",
            },
        )
        self.assertEqual("all", merged["features"]["recall"]["member_scope"])
        self.assertTrue(merged["enable_admin_recall"])
        self.assertEqual("all", merged["recall_member_scope"])

    def test_plugin_modules_use_full_namespace_from_top_level_web_package(self):
        sentinel = object()
        imported = []

        def fake_import(name):
            imported.append(name)
            return sentinel

        _plugin_module.cache_clear()
        try:
            with patch("web_client.services.importlib.import_module", side_effect=fake_import):
                result = _plugin_module("qqadmin.example")
        finally:
            _plugin_module.cache_clear()

        self.assertIs(result, sentinel)
        self.assertEqual(
            imported[0],
            "data.plugins.astrbot_plugin_joker.qqadmin.example",
        )

    def test_group_feature_updates_are_isolated_by_group_id(self):
        fake = FakeGroupConfig()
        group_a = {
            field.key: field.default for field in FEATURE_BY_KEY["welcome"].fields
        }
        group_b = {
            field.key: field.default for field in FEATURE_BY_KEY["welcome"].fields
        }
        group_a["message"] = "A 群欢迎语"
        group_b["message"] = "B 群欢迎语"

        with patch("web_client.services.group_config_module", return_value=fake):
            apply_feature_values("1102579807", "welcome", group_a)
            apply_feature_values("568586484", "welcome", group_b)

        self.assertEqual(fake.configs["1102579807"]["welcome_message"], "A 群欢迎语")
        self.assertEqual(fake.configs["568586484"]["welcome_message"], "B 群欢迎语")
        self.assertNotEqual(
            fake.configs["1102579807"]["features"]["welcome"]["message"],
            fake.configs["568586484"]["features"]["welcome"]["message"],
        )

    def test_remote_projection_never_contains_runtime_or_auth_data(self):
        source = {
            "group_id": "1102579807",
            "features": {"power_status": "on", "welcome": {"enabled": True}},
            "timed_messages": [{"id": "task"}],
            "stats": {"secret_history": [1]},
            "message_cache": [{"raw_message": "private"}],
            "blacklist": [{"user_id": "1"}],
            "auth_key": "must-not-leave-local",
        }
        projected = project_group_config(source)
        self.assertEqual(projected["timed_messages"], [{"id": "task"}])
        self.assertNotIn("stats", projected)
        self.assertNotIn("message_cache", projected)
        self.assertNotIn("blacklist", projected)
        self.assertNotIn("auth_key", projected)

    def test_remote_merge_preserves_local_runtime_data(self):
        existing = {
            "group_id": "1102579807",
            "features": {"welcome": {"enabled": False}},
            "stats": {"signin": 9},
            "message_cache": [{"text": "local"}],
        }
        update = {
            "group_id": "1102579807",
            "features": {"welcome": {"enabled": True}},
        }
        merged = merge_group_projection(existing, update)
        self.assertTrue(merged["features"]["welcome"]["enabled"])
        self.assertEqual(merged["stats"], {"signin": 9})
        self.assertEqual(merged["message_cache"], [{"text": "local"}])

    def test_remote_task_definition_cannot_reset_windows_dispatch_state(self):
        existing = {
            "group_id": "1102579807",
            "timed_messages": [
                {
                    "id": "morning",
                    "name": "早报",
                    "cron_expr": "0 9 * * *",
                    "message": "早上好",
                    "enabled": True,
                    "last_sent_at": 1_700_000_000,
                    "sent_count": 3,
                    "dispatch_lock_until": 1_700_000_100,
                    "last_scheduled_at": 1_699_999_980,
                    "dispatch_scheduled_at": 1_700_000_040,
                    "next_retry_at": 1_700_000_200,
                    "failure_count": 2,
                }
            ],
        }
        update = {
            "group_id": "1102579807",
            "timed_messages": [
                {
                    "id": "morning",
                    "name": "新的早报名称",
                    "cron_expr": "0 9 * * *",
                    "message": "新的内容",
                    "enabled": True,
                    "last_sent_at": 0,
                    "sent_count": 0,
                }
            ],
        }

        merged = merge_group_projection(existing, update, explicit_empty_deletes=True)
        task = merged["timed_messages"][0]
        self.assertEqual("新的早报名称", task["name"])
        self.assertEqual("新的内容", task["message"])
        self.assertEqual(1_700_000_000, task["last_sent_at"])
        self.assertEqual(3, task["sent_count"])
        self.assertEqual(1_700_000_100, task["dispatch_lock_until"])
        self.assertEqual(1_699_999_980, task["last_scheduled_at"])
        self.assertEqual(1_700_000_040, task["dispatch_scheduled_at"])
        self.assertEqual(1_700_000_200, task["next_retry_at"])
        self.assertEqual(2, task["failure_count"])

    def test_group_page_helpers_ignore_malformed_legacy_collections(self):
        """One old group field must not make its entire portal page a 500."""
        legacy_replies = {
            "exact": ["old-format"],
            "fuzzy": "not-a-list",
            "fuzzy_keywords": ["not-a-mapping"],
            "regex": ["not-a-record", {"pattern": "ok", "reply": "not-a-record"}],
        }
        reply_module = types.SimpleNamespace(
            get_group_replies=lambda _group_id: legacy_replies
        )
        with patch("web_client.services.remote_data_mode", return_value=False), patch(
            "web_client.services.plugin_module", return_value=reply_module
        ):
            details = _list_reply_details("1102579807")

        self.assertEqual(
            details,
            [
                {
                    "key": "ok",
                    "type": "regex",
                    "reply": "",
                    "probability": 100,
                    "max_uses": 0,
                    "has_images": False,
                    "image_count": 0,
                    "image_status": "",
                    "portal_assets": [],
                }
            ],
        )

        fake = FakeGroupConfig()
        fake.configs["1102579807"] = {
            "group_id": "1102579807",
            "features": ["legacy-invalid-value"],
        }
        with patch("web_client.services.remote_data_mode", return_value=False), patch(
            "web_client.services.group_config_module", return_value=fake
        ):
            values = get_feature_values("1102579807", "welcome")
        self.assertEqual(
            values,
            {field.key: field.default for field in FEATURE_BY_KEY["welcome"].fields},
        )

        player_replies = _list_player_reply_details(
            {
                "player_replies": {
                    "10001": {"messages": "单条旧回复"},
                    "10002": {"messages": 42},
                    "10003": {"messages": ["正常", 7, {"bad": "value"}]},
                }
            }
        )
        self.assertEqual(
            player_replies,
            [
                {
                    "user_id": "10001",
                    "messages": ["单条旧回复"],
                    "at_user": True,
                    "enabled": True,
                },
                {
                    "user_id": "10002",
                    "messages": [],
                    "at_user": True,
                    "enabled": True,
                },
                {
                    "user_id": "10003",
                    "messages": ["正常", "7"],
                    "at_user": True,
                    "enabled": True,
                },
            ],
        )

    def test_group_feature_display_coerces_legacy_scalars_without_writing_them(self):
        """A malformed old snapshot must still render every group form safely."""
        fake = FakeGroupConfig()
        fake.configs["1102579807"] = {
            "group_id": "1102579807",
            "features": {
                "mute": {"enabled": "false", "level": "not-a-number", "duration": {}},
                "welcome": {"keywords": {"not": "a list"}},
                "recall": {"mode": ["invalid"], "time_limit": "999999999"},
            },
        }

        with patch("web_client.services.remote_data_mode", return_value=False), patch(
            "web_client.services.group_config_module", return_value=fake
        ):
            mute = get_feature_values("1102579807", "mute")
            welcome = get_feature_values("1102579807", "welcome")
            recall = get_feature_values("1102579807", "recall")

        self.assertFalse(mute["enabled"])
        self.assertEqual(mute["level"], FEATURE_BY_KEY["mute"].fields[1].default)
        self.assertEqual(mute["duration"], FEATURE_BY_KEY["mute"].fields[2].default)
        self.assertEqual(welcome["keywords"], [])
        self.assertEqual(recall["mode"], "keyword")
        self.assertEqual(recall["time_limit"], FEATURE_BY_KEY["recall"].fields[2].default)
        # Opening the page is read-only: a legacy value remains untouched
        # until the owner explicitly saves that feature form.
        self.assertEqual(
            fake.configs["1102579807"]["features"]["welcome"]["keywords"],
            {"not": "a list"},
        )


class ThemeServiceTests(unittest.TestCase):
    @staticmethod
    def _png_bytes() -> bytes:
        # A tiny valid PNG fixture for the real Pillow validation/re-encoding
        # path. Pillow is a required web-client deployment dependency.
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    def test_background_is_private_until_owner_enables_sharing(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            owner = User(username="theme_owner", password_hash=hash_password("password123"))
            other = User(username="theme_other", password_hash=hash_password("password123"))
            db.add_all((owner, other))
            db.flush()

            with patch(
                "web_client.theme_service._validate_image_bytes",
                return_value=("image/png", 32, 18),
            ):
                asset = theme_service.store_theme_asset(db, owner, self._png_bytes())
            theme_service.save_theme_preference(db, owner, "auto", asset.id, None)
            self.assertEqual(
                theme_service.get_theme_context(db, owner)["day_url"],
                f"/theme/assets/{asset.id}",
            )
            self.assertIsNone(theme_service.get_accessible_asset(db, other, asset.id))
            with self.assertRaisesRegex(ValueError, "无权使用"):
                theme_service.save_theme_preference(db, other, "day", asset.id, None)

            theme_service.set_asset_sharing(db, owner, asset.id, True)
            self.assertEqual(
                theme_service.get_accessible_asset(db, other, asset.id).id,
                asset.id,
            )
            preference = theme_service.save_theme_preference(
                db, other, "night", asset.id, asset.id
            )
            self.assertEqual(preference.mode, "night")
            self.assertEqual(preference.day_asset_id, asset.id)
            self.assertEqual(preference.night_asset_id, asset.id)
            self.assertEqual(db.scalar(select(func.count()).select_from(PortalThemeAsset)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(PortalThemePreference)), 2)

    def test_delete_background_clears_all_preferences(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            owner = User(username="theme_delete_owner", password_hash=hash_password("password123"))
            other = User(username="theme_delete_other", password_hash=hash_password("password123"))
            db.add_all((owner, other))
            db.flush()
            with patch(
                "web_client.theme_service._validate_image_bytes",
                return_value=("image/png", 32, 18),
            ):
                asset = theme_service.store_theme_asset(db, owner, self._png_bytes())
            theme_service.set_asset_sharing(db, owner, asset.id, True)
            theme_service.save_theme_preference(db, owner, "day", asset.id, asset.id)
            theme_service.save_theme_preference(db, other, "night", asset.id, asset.id)

            deleted_id = asset.id
            theme_service.delete_theme_asset(db, owner, deleted_id)
            db.commit()

            self.assertIsNone(db.get(PortalThemeAsset, deleted_id))
            preferences = list(db.scalars(select(PortalThemePreference)))
            self.assertTrue(all(item.day_asset_id is None for item in preferences))
            self.assertTrue(all(item.night_asset_id is None for item in preferences))
            with self.assertRaisesRegex(ValueError, "背景图片不存在"):
                theme_service.delete_theme_asset(db, owner, deleted_id)

    def test_background_delete_rejects_other_users_asset(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            owner = User(username="theme_owner_only", password_hash=hash_password("password123"))
            other = User(username="theme_other_only", password_hash=hash_password("password123"))
            db.add_all((owner, other))
            db.flush()
            with patch(
                "web_client.theme_service._validate_image_bytes",
                return_value=("image/png", 32, 18),
            ):
                asset = theme_service.store_theme_asset(db, owner, self._png_bytes())
            with self.assertRaisesRegex(ValueError, "只能删除自己上传"):
                theme_service.delete_theme_asset(db, other, asset.id)

    def test_theme_rejects_non_raster_without_a_legacy_source_size_gate(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            user = User(username="theme_user", password_hash=hash_password("password123"))
            db.add(user)
            db.flush()
            with self.assertRaises(ValueError):
                theme_service.store_theme_asset(
                    db,
                    user,
                    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                )

        # The old five-megabyte gate used to reject the bytes before Pillow
        # could inspect their real image metadata. A source larger than that
        # threshold now reaches the stream validator instead.
        source = b"x" * (5 * 1024 * 1024 + 1)
        with patch(
            "web_client.theme_service._validate_theme_image_stream",
            return_value=("image/png", 32, 18),
        ) as validate_stream:
            self.assertEqual(
                ("image/png", 32, 18), theme_service._validate_image_bytes(source)
            )
        validate_stream.assert_called_once()

    def test_theme_stream_preparation_returns_a_compact_webp(self):
        prepared = theme_service.prepare_theme_asset_stream(BytesIO(self._png_bytes()))

        self.assertEqual("image/webp", prepared.content_type)
        self.assertEqual((1, 1), (prepared.width, prepared.height))
        self.assertLessEqual(len(prepared.image_data), theme_service.MAX_THEME_STORED_BYTES)

    def test_theme_rejects_oversized_pixel_headers_before_decode(self):
        image = MagicMock()
        image.__enter__.return_value = image
        image.format = "PNG"
        image.size = (theme_service.MAX_THEME_DIMENSION + 1, 1)
        with patch("PIL.Image.open", return_value=image):
            with self.assertRaisesRegex(ValueError, "尺寸过大"):
                theme_service._validate_theme_image_stream(BytesIO(b"not-decoded"))

        image.verify.assert_called_once()
        image.load.assert_not_called()

    def test_asset_metadata_lookup_defers_the_image_payload(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            user = User(username="theme_metadata", password_hash=hash_password("password123"))
            db.add(user)
            db.flush()
            with patch(
                "web_client.theme_service._validate_image_bytes",
                return_value=("image/png", 1, 1),
            ):
                stored = theme_service.store_theme_asset(db, user, self._png_bytes())
            asset_id = stored.id
            db.expire_all()

            metadata = theme_service.get_accessible_asset(
                db, user, asset_id, load_data=False
            )

            self.assertIsNotNone(metadata)
            self.assertNotIn("image_data", metadata.__dict__)


class DatabaseFlowTests(unittest.TestCase):
    def test_game_authorization_heartbeat_is_independent_from_qq_auth(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        group_id = "1102579807"
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "windows-game",
                [{
                    "group_id": group_id,
                    "game_auth": {
                        "state": "active",
                        "expire": -1,
                        "card_fingerprint": "c" * 64,
                        "card_mask": "JG****CC",
                    },
                }],
                [],
                config_source_complete=False,
                auth_source_complete=False,
                game_auth_source_complete=True,
            )
            row = db.scalar(
                select(GameAuthorization).where(
                    GameAuthorization.group_id == group_id
                )
            )
            self.assertIsNotNone(row)
            self.assertEqual(row.client_id, "windows-game")
            self.assertEqual(
                remote_store.get_game_authorization_for_client(
                    db, "windows-game", group_id
                )["state"],
                "active",
            )
            row.expire = 0
            db.flush()
            self.assertEqual(
                remote_store.get_game_authorization_for_client(
                    db, "windows-game", group_id
                )["state"],
                "missing",
            )

    def test_user_websocket_card_is_encrypted_and_scoped_to_active_client(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="websocket-session-secret-32-characters-min",
            card_pepper="websocket-card-pepper-32-characters-min",
            secure_cookies=False,
            portal_name="Test",
            session_max_age=3600,
        )
        group_id = "1102579807"
        card = "068dbf376ab2ca16fb2a18ec3068b731"
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [{
                    "group_id": group_id,
                    "game_auth": {
                        "state": "active",
                        "expire": -1,
                        "card_fingerprint": "d" * 64,
                        "card_mask": "JG****DD",
                    },
                }],
                [],
                config_source_complete=False,
                auth_source_complete=False,
                game_auth_source_complete=True,
            )
            saved = remote_store.save_group_websocket_access_key(
                db, group_id, card, settings
            )
            db.flush()
            row = db.scalar(
                select(GameAuthorization).where(
                    GameAuthorization.group_id == group_id
                )
            )
            self.assertEqual(saved["state"], "verified")
            self.assertNotIn(card, row.websocket_key_ciphertext)
            im3_row = db.scalar(
                select(Im3Authorization).where(
                    Im3Authorization.group_id == group_id
                )
            )
            self.assertIsNotNone(im3_row)
            self.assertEqual(im3_row.client_id, "")
            self.assertEqual(im3_row.state, "verified")
            self.assertEqual(
                remote_store.get_group_websocket_access_keys(
                    db, [group_id], settings, client_id="windows-a"
                ),
                {group_id: card},
            )
            self.assertEqual(
                remote_store.get_group_websocket_access_keys(
                    db, [group_id], settings, client_id="windows-b"
                ),
                {},
            )
            remote_store.mark_group_websocket_access_failed(db, group_id)
            self.assertEqual(
                remote_store.get_group_websocket_access_keys(
                    db, [group_id], settings, client_id="windows-a"
                ),
                {},
            )

    def test_im3_card_alone_does_not_create_or_activate_game_license(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="im3-only-session-secret-32-characters",
            card_pepper="im3-only-card-pepper-32-characters",
            secure_cookies=False,
            portal_name="Test",
            session_max_age=3600,
        )
        group_id = "1102579807"
        with Session(engine) as db:
            remote_store.save_group_websocket_access_key(
                db,
                group_id,
                "068dbf376ab2ca16fb2a18ec3068b731",
                settings,
            )
            db.flush()
            self.assertIsNone(
                db.scalar(
                    select(GameAuthorization).where(
                        GameAuthorization.group_id == group_id
                    )
                )
            )
            self.assertEqual(
                remote_store.get_game_authorization_status(db, group_id)["state"],
                "missing",
            )
            self.assertEqual(
                remote_store.get_group_websocket_access_status(db, group_id)[
                    "credential_type"
                ],
                "im3_external",
            )

    def test_legacy_merged_im3_card_is_migrated_without_game_client_scope(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        group_id = "1102579807"
        with Session(engine) as db:
            db.add(
                GameAuthorization(
                    group_id=group_id,
                    client_id="old-windows-client",
                    state="active",
                    expire=-1,
                    card_fingerprint="e" * 64,
                    websocket_key_ciphertext="legacy-encrypted-im3-card",
                    websocket_key_mask="068dbf****731",
                    websocket_state="verified",
                    websocket_verified_at=123,
                    websocket_validation_version=remote_store.IM3_CARD_VALIDATION_VERSION,
                )
            )
            db.commit()

        _migrate_database(engine)

        with Session(engine) as db:
            im3_row = db.scalar(
                select(Im3Authorization).where(
                    Im3Authorization.group_id == group_id
                )
            )
            self.assertIsNotNone(im3_row)
            self.assertEqual(im3_row.key_ciphertext, "legacy-encrypted-im3-card")
            self.assertEqual(im3_row.client_id, "")
            self.assertEqual(
                remote_store.get_game_authorization_status(db, group_id)[
                    "credential_type"
                ],
                "game_owner",
            )
    def test_remote_sync_scope_uses_web_account_binding(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        bound = "1102579807"
        unbound = "1102579808"
        other_client = "1102579809"
        with Session(engine) as db:
            user = User(
                username="scope-user",
                password_hash="hash",
                role="customer",
                is_active=True,
            )
            db.add(user)
            db.flush()
            db.add_all(
                [
                    GroupAccess(
                        user_id=user.id,
                        group_id=bound,
                        role="owner",
                        card_fingerprint="a" * 64,
                        card_mask="JK****AA",
                    ),
                    GroupAccess(
                        user_id=user.id,
                        group_id=other_client,
                        role="owner",
                        card_fingerprint="b" * 64,
                        card_mask="JK****BB",
                    ),
                    GroupConfigSnapshot(
                        group_id=bound,
                        revision=1,
                        applied_revision=1,
                        config_json={"group_id": bound, "features": {}},
                        client_id="windows-a",
                        auth_state="active",
                        auth_card_fingerprint="a" * 64,
                    ),
                    GroupConfigSnapshot(
                        group_id=unbound,
                        revision=1,
                        applied_revision=1,
                        config_json={"group_id": unbound, "features": {}},
                        client_id="windows-a",
                        auth_state="active",
                        auth_card_fingerprint="c" * 64,
                    ),
                    GroupConfigSnapshot(
                        group_id=other_client,
                        revision=1,
                        applied_revision=1,
                        config_json={"group_id": other_client, "features": {}},
                        client_id="windows-b",
                        auth_state="active",
                        auth_card_fingerprint="b" * 64,
                    ),
                ]
            )
            db.flush()

            self.assertEqual(remote_store.bound_group_ids(db, "windows-a"), {bound})
            self.assertEqual(remote_store.claimable_group_ids(db, "windows-a"), {unbound})

    def test_scoped_exchange_does_not_tombstone_unlisted_local_snapshot(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        included = "1102579807"
        unbound = "1102579808"
        with Session(engine) as db:
            for group_id in (included, unbound):
                db.add(
                    GroupConfigSnapshot(
                        group_id=group_id,
                        revision=1,
                        applied_revision=1,
                        config_json={"group_id": group_id, "features": {}},
                        client_id="windows-a",
                    )
                )
            db.commit()
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [],
                [included],
                authoritative_group_ids=True,
                sync_scope_complete=True,
            )
            db.flush()
            retained = remote_store.get_snapshot(db, unbound)
            self.assertIsNotNone(retained)
            self.assertNotEqual(retained.sync_status, remote_store.LOCAL_GROUP_MISSING_STATUS)

    def test_mutate_web_config_preserves_changes_from_another_web_request(self):
        """A locked mutator must not overwrite a newer unrelated web edit."""

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        group_id = "1102579807"
        with Session(engine) as seed:
            remote_store.save_web_config(
                seed,
                group_id,
                {
                    "group_id": group_id,
                    "features": {"welcome": {"enabled": False}},
                    "timed_messages": [],
                },
                1,
            )
            seed.commit()

        # Keep an old page projection alive while another request commits a
        # change.  mutate_web_config must lock and reread the current row,
        # rather than applying the stale page copy.
        with Session(engine) as stale_request:
            stale_projection = remote_store.get_config(stale_request, group_id)
            self.assertFalse(stale_projection["features"]["welcome"]["enabled"])
            with Session(engine) as other_request:
                remote_store.mutate_web_config(
                    other_request,
                    group_id,
                    2,
                    lambda config: config["features"].update(
                        {"blacklist": {"enabled": True}}
                    )
                    or config,
                )
                other_request.commit()

            remote_store.mutate_web_config(
                stale_request,
                group_id,
                3,
                lambda config: config["features"]["welcome"].update(
                    {"enabled": True}
                )
                or config,
            )
            stale_request.commit()

        with Session(engine) as db:
            config = remote_store.get_config(db, group_id)
            self.assertTrue(config["features"]["welcome"]["enabled"])
            self.assertTrue(config["features"]["blacklist"]["enabled"])

    def test_console_query_never_leaks_another_group(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add_all(
                (
                    PortalConsoleEvent(
                        event_id="group-a-event",
                        client_id="windows-test",
                        group_id="1102579807",
                        action="发送",
                        detail="仅 A 群",
                    ),
                    PortalConsoleEvent(
                        event_id="group-b-event",
                        client_id="windows-test",
                        group_id="568586484",
                        action="禁言",
                        detail="仅 B 群",
                    ),
                )
            )
            db.flush()

            events = remote_store.list_console_events(db, "1102579807")
            self.assertEqual(["group-a-event"], [event.event_id for event in events])
            self.assertEqual([], remote_store.list_console_events(db, "not-a-group"))

    def test_nginx_template_is_rendered_for_the_selected_domain(self):
        root = Path(__file__).parent
        renderer = runpy.run_path(str(root / "deploy" / "linux" / "render_nginx_config.py"))
        template = root / "deploy" / "linux" / "nginx-web.conf.example"
        with tempfile.TemporaryDirectory() as temporary_dir:
            output = Path(temporary_dir) / "portal.conf"
            mode = renderer["render_config"](
                template,
                output,
                "Portal.Example.COM",
                "/www/wwwroot/portal.example.com",
                "/www/wwwroot/joker-web",
                "/www/server/panel/vhost/cert/portal.example.com/fullchain.pem",
                "/www/server/panel/vhost/cert/portal.example.com/privkey.pem",
            )
            rendered = output.read_text(encoding="utf-8")
            self.assertEqual(mode, "tls")
            self.assertIn("server_name portal.example.com;", rendered)
            self.assertIn("https://portal.example.com$request_uri", rendered)
            self.assertIn("alias /www/wwwroot/joker-web/web_client/static/;", rendered)
            self.assertIn("location = /admin/update {", rendered)
            self.assertIn("client_body_timeout 600s;", rendered)
            self.assertIn("proxy_send_timeout 600s;", rendered)
            self.assertIn("proxy_read_timeout 600s;", rendered)
            self.assertIn("proxy_request_buffering off;", rendered)
            self.assertNotIn("__JOKER_", rendered)
            self.assertNotIn("web.yooyun.top", rendered)

            bootstrap_output = Path(temporary_dir) / "bootstrap.conf"
            self.assertEqual(
                renderer["render_config"](
                    template,
                    bootstrap_output,
                    "portal.example.com",
                    "/www/wwwroot/portal.example.com",
                    "/www/wwwroot/joker-web",
                ),
                "bootstrap",
            )
            bootstrap = bootstrap_output.read_text(encoding="utf-8")
            self.assertNotIn("listen 443", bootstrap)
            self.assertIn("location / {\n        return 404;", bootstrap)
            with self.assertRaises(ValueError):
                renderer["render_config"](
                    template,
                    output,
                    "https://unsafe.example",
                    "/www/wwwroot/portal.example.com",
                    "/www/wwwroot/joker-web",
                    "/www/server/panel/vhost/cert/portal.example.com/fullchain.pem",
                    "/www/server/panel/vhost/cert/portal.example.com/privkey.pem",
                )
            with self.assertRaises(ValueError):
                renderer["render_config"](
                    template,
                    output,
                    "183.136.132.24",
                    "/www/wwwroot/portal.example.com",
                    "/www/wwwroot/joker-web",
                )

    def test_root_update_handoff_rejects_path_escape(self):
        root_helper_path = (
            Path(__file__).parent
            / "deploy"
            / "linux"
            / "joker-web-secure-stage.py"
        )
        # ``pwd`` is Linux-only. The helper does not access it while it is
        # loaded, so a stub makes this security test cross-platform.
        with patch.dict(sys.modules, {"pwd": types.ModuleType("pwd")}):
            root_helper = runpy.run_path(str(root_helper_path))
        required_entries = root_helper["REQUIRED_ARCHIVE_ENTRIES"]
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = (
                Path(temporary_dir) / "Joker_Baota_Remote_v1.10.26-all-in-one.zip"
            )
            fixture_payload = {
                entry: b"# validated package fixture\n"
                for entry in required_entries
                if entry != "update-manifest.json"
            }
            fixture_manifest = {
                "format_version": 1,
                "release_version": "1.10.26",
                "archive_filename": archive_path.name,
                "files": {
                    entry: {
                        "sha256": hashlib.sha256(contents).hexdigest(),
                        "size": len(contents),
                    }
                    for entry, contents in fixture_payload.items()
                },
            }
            with zipfile.ZipFile(archive_path, "w") as archive:
                for entry, contents in fixture_payload.items():
                    archive.writestr(entry, contents)
                archive.writestr(
                    "update-manifest.json",
                    json.dumps(fixture_manifest, ensure_ascii=False, sort_keys=True),
                )
            extracted = Path(temporary_dir) / "extracted"
            root_helper["_validate_archive_and_extract"](archive_path, extracted)
            manifest_verifier = runpy.run_path(
                str(Path(__file__).parent / "deploy" / "linux" / "verify_update_manifest.py")
            )
            manifest_verifier["verify_tree"](extracted)
            self.assertEqual(MAX_UPDATE_REQUEST_BYTES, MAX_ARCHIVE_BYTES + 2 * 1024 * 1024)
            helper_path = (
                "01_plugin_source/astrbot_plugin_joker/deploy/linux/"
                "joker-web-secure-stage.py"
            )
            package_builder = (Path(__file__).parent / "deploy" / "build_baota_package.ps1").read_text(
                encoding="utf-8"
            )
            nginx_example = (
                Path(__file__).parent / "deploy" / "linux" / "nginx-web.conf.example"
            ).read_text(encoding="utf-8")
            installer = (
                Path(__file__).parent / "deploy" / "linux" / "quick_install.sh"
            ).read_text(encoding="utf-8")
            update_worker = (
                Path(__file__).parent / "deploy" / "linux" / "joker-web-apply-update.sh"
            ).read_text(encoding="utf-8")
            update_watcher = (
                Path(__file__).parent / "deploy" / "linux" / "joker-web-scan-updates.sh"
            ).read_text(encoding="utf-8")
            portal_app = (
                Path(__file__).parent / "web_client" / "app.py"
            ).read_text(encoding="utf-8")
            self.assertIn(helper_path, package_builder)
            for required_entry in required_entries:
                self.assertIn(required_entry, package_builder)
            self.assertIn("client_max_body_size 82m;", nginx_example)
            self.assertIn("render_nginx_config.py", package_builder)
            self.assertIn(
                "01_plugin_source/astrbot_plugin_joker/deploy/linux/render_nginx_config.py",
                required_entries,
            )
            self.assertNotIn("web.yooyun.top", installer)
            self.assertIn(
                'render_nginx_config "$TARGET_DIR" "$DOMAIN" "$NGINX_SITE_ROOT"',
                installer,
            )
            self.assertIn("joker-webctl --render-nginx", installer)
            self.assertIn("--apply-nginx", installer)
            self.assertIn("is_baota_vhost_file", installer)
            self.assertIn("restore_nginx_vhost", installer)
            self.assertIn("nginx -t 2>&1", installer)
            self.assertIn("has_completed_install", installer)
            self.assertIn(".joker-web-install-complete", installer)
            self.assertIn("JOKER_WEB_DATABASE_URL=\"\"", installer)
            self.assertIn("数据库参数尚未写入网页配置", installer)
            self.assertIn("/health/ready", installer)
            self.assertNotIn("web.yooyun.top", update_worker)
            self.assertIn('write_status "failed"', update_worker)
            self.assertIn(
                'MANIFEST_VERIFIER="$TEMP_DIR/01_plugin_source/astrbot_plugin_joker/'
                'deploy/linux/verify_update_manifest.py"',
                update_worker,
            )
            self.assertIn('python3 "$MANIFEST_VERIFIER" --tree "$TEMP_DIR"', update_worker)
            self.assertIn("JOKER_UPDATE_RESPONSE_GRACE_SECONDS", installer)
            self.assertIn("TimeoutStartSec=30min", installer)
            self.assertIn('sleep "$RESPONSE_GRACE_SECONDS"', update_watcher)
            self.assertIn("status_code=202", portal_app)
            self.assertIn("update_queued=True", portal_app)

            update_staging = (
                Path(__file__).parent / "web_client" / "self_update.py"
            ).read_text(encoding="utf-8")
            self.assertIn("await asyncio.to_thread(destination.write, chunk)", update_staging)
            self.assertIn("await asyncio.to_thread(os.fsync, destination.fileno())", update_staging)

            update_template = (
                Path(__file__).parent / "web_client" / "templates" / "admin_update.html"
            ).read_text(encoding="utf-8")
            self.assertIn("Joker_Baota_Remote_vX.Y.Z-all-in-one.zip", update_template)
            self.assertNotIn("Joker_Baota_Remote_v*.zip", update_template)
            self.assertIn("更新任务已排队", update_template)

            missing_handoff_helper = Path(temporary_dir) / "missing-handoff-helper.zip"
            with zipfile.ZipFile(missing_handoff_helper, "w") as archive:
                archive.writestr("install.sh", "#!/usr/bin/env bash\n")
                archive.writestr(
                    "01_plugin_source/astrbot_plugin_joker/web_client/app.py",
                    "print('portal')\n",
                )
                archive.writestr(
                    "01_plugin_source/astrbot_plugin_joker/deploy/linux/quick_install.sh",
                    "#!/usr/bin/env bash\n",
                )
            with self.assertRaises(root_helper["StageError"]):
                root_helper["_validate_archive_and_extract"](
                    missing_handoff_helper, Path(temporary_dir) / "missing-extracted"
                )

            unsafe_path = Path(temporary_dir) / "unsafe.zip"
            with zipfile.ZipFile(unsafe_path, "w") as archive:
                archive.writestr("../escape", "bad")
            with self.assertRaises(root_helper["StageError"]):
                root_helper["_validate_archive_and_extract"](
                    unsafe_path, Path(temporary_dir) / "unsafe-extracted"
                )

    def test_discard_staged_update_removes_only_failed_task_files(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            incoming = Path(temporary_dir) / "incoming"
            incoming.mkdir()
            failed_id = "a" * 32
            unrelated_id = "b" * 32
            for suffix in (".uploading", ".requesting", ".zip", ".auth"):
                (incoming / f"{failed_id}{suffix}").write_text("temporary", encoding="utf-8")
            unrelated = incoming / f"{unrelated_id}.zip"
            unrelated.write_text("keep", encoding="utf-8")

            with patch("web_client.self_update.INCOMING_DIR", incoming):
                discard_staged_update(failed_id)
                discard_staged_update("not-an-update-id")

            for suffix in (".uploading", ".requesting", ".zip", ".auth"):
                self.assertFalse((incoming / f"{failed_id}{suffix}").exists())
            self.assertTrue(unrelated.exists())

    @unittest.skipUnless(os.name == "posix", "update staging is Linux-only")
    def test_update_staging_defers_zip_validation_until_root_handoff(self):
        class Upload:
            filename = r"C:\\downloads\\not-a-valid-update.zip"

            def __init__(self, payload: bytes):
                self.payload = payload
                self.offset = 0
                self.closed = False

            async def read(self, size: int) -> bytes:
                chunk = self.payload[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

            async def close(self) -> None:
                self.closed = True

        payload = b"this is deliberately not a zip archive"
        with tempfile.TemporaryDirectory() as temporary_dir:
            incoming = Path(temporary_dir) / "incoming"
            incoming.mkdir()
            upload = Upload(payload)
            with patch("web_client.self_update.INCOMING_DIR", incoming):
                staged = asyncio.run(stage_update_package(upload, "a" * 24))
                queue_staged_update(staged["update_id"])

            update_id = staged["update_id"]
            self.assertEqual(staged["release_version"], PENDING_RELEASE_VERSION)
            self.assertEqual(staged["filename"], "not-a-valid-update.zip")
            self.assertTrue(upload.closed)
            self.assertEqual(payload, (incoming / f"{update_id}.zip").read_bytes())
            self.assertEqual("a" * 24 + "\n", (incoming / f"{update_id}.auth").read_text())
            self.assertEqual(update_id + "\n", (incoming / f"{update_id}.request").read_text())
            self.assertFalse((incoming / f".{update_id}.requesting").exists())

    def test_linux_runner_switch_only_prepares_future_migration(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            initial = get_runner_deployment(db)
            self.assertEqual(initial["active_runner"], "windows")
            self.assertFalse(initial["linux_migration_prepared"])
            self.assertFalse(initial["linux_execution_enabled"])

            updated = set_linux_migration_prepared(db, True, operator_id=7)
            db.commit()
            self.assertEqual(updated["active_runner"], "windows")
            self.assertTrue(updated["linux_migration_prepared"])
            self.assertFalse(updated["linux_runner_available"])
            self.assertFalse(updated["linux_execution_enabled"])

    def test_models_compile_for_mysql(self):
        statements = [
            str(CreateTable(table).compile(dialect=mysql.dialect()))
            for table in Base.metadata.sorted_tables
        ]
        # Keep this guard aligned with the portal-only audit and asset tables.
        self.assertEqual(len(statements), 15)
        self.assertTrue(all("CREATE TABLE" in statement for statement in statements))
        self.assertTrue(any("portal_theme_assets" in statement for statement in statements))
        self.assertTrue(any("portal_theme_preferences" in statement for statement in statements))
        self.assertTrue(any("portal_feedback" in statement for statement in statements))
        self.assertTrue(any("portal_reply_assets" in statement for statement in statements))

    def test_feedback_record_and_configured_recipient_are_portal_only(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="session-secret-for-feedback-tests",
            card_pepper="card-pepper",
            secure_cookies=False,
            portal_name="Test",
            session_max_age=3600,
        )
        form = {
            "enabled": "on",
            "host": "smtp.example.com",
            "port": "465",
            "transport": "ssl",
            "username": "mailer@example.com",
            "password": "smtp-app-password",
            "sender_email": "mailer@example.com",
            "sender_name": "Test sender",
            "feedback_recipient": "owner@example.com",
            "code_ttl_minutes": "10",
        }
        with Session(engine) as db:
            user = User(
                username="feedback-user",
                password_hash=hash_password("password"),
            )
            db.add(user)
            db.flush()
            db.add(
                PortalFeedback(
                    user_id=user.id,
                    group_id="1102579807",
                    category="sync",
                    subject="Save timeout",
                    content="A background task must not block this record.",
                )
            )
            save_email_config(db, settings, form, None)
            db.commit()
            self.assertEqual(get_feedback_recipient(db), "owner@example.com")
            feedback = db.scalar(select(PortalFeedback))
            self.assertEqual(feedback.status, "new")
            self.assertEqual(feedback.email_state, "pending")

    def test_additive_role_migration_for_existing_database(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE portal_users ("
                    "id INTEGER PRIMARY KEY, username VARCHAR(64) NOT NULL, "
                    "password_hash VARCHAR(255) NOT NULL, is_active BOOLEAN NOT NULL, "
                    "created_at DATETIME NOT NULL, last_login_at DATETIME)"
                )
            )
        _migrate_database(engine)
        columns = {item["name"] for item in inspect(engine).get_columns("portal_users")}
        self.assertIn("role", columns)
        self.assertIn("email", columns)

    def test_email_registration_uses_a_code_without_storing_plaintext(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="session-secret-for-email-tests",
            card_pepper="card-pepper",
            secure_cookies=False,
            portal_name="Test",
            session_max_age=3600,
        )
        form = {
            "enabled": "on",
            "host": "smtp.example.com",
            "port": "465",
            "transport": "ssl",
            "username": "mailer@example.com",
            "password": "smtp-app-password",
            "sender_email": "mailer@example.com",
            "sender_name": "Test sender",
            "code_ttl_minutes": "10",
        }
        with Session(engine) as db:
            safe_view = save_email_config(db, settings, form, None)
            db.commit()
            self.assertTrue(safe_view["password_configured"])
            self.assertNotIn(
                "smtp-app-password",
                db.get(PortalSetting, "email_delivery").value_json["password_ciphertext"],
            )
            with (
                patch("web_client.email_service._send_mail"),
                patch("web_client.email_service.secrets.randbelow", return_value=123456),
            ):
                email = request_registration_code(db, settings, "customer@example.com", "127.0.0.1")
            db.commit()
            self.assertEqual(email, "customer@example.com")
            self.assertEqual(db.scalar(select(EmailVerification)).attempts, 0)
            self.assertEqual(
                consume_registration_code(db, settings, email, "123456"),
                email,
            )
            db.commit()
            self.assertIsNotNone(db.scalar(select(EmailVerification)).consumed_at)

    def test_bad_email_code_consumes_an_attempt(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="session-secret-for-email-tests",
            card_pepper="card-pepper",
            secure_cookies=False,
            portal_name="Test",
            session_max_age=3600,
        )
        with Session(engine) as db:
            db.add(
                EmailVerification(
                    email="customer@example.com",
                    purpose="register",
                    code_digest="not-the-right-code",
                    expires_at=utcnow().replace(year=2099),
                )
            )
            db.commit()
            with self.assertRaises(VerificationCodeError):
                consume_registration_code(db, settings, "customer@example.com", "000000")
            db.commit()
            self.assertEqual(db.scalar(select(EmailVerification)).attempts, 1)

    def test_password_reset_code_is_bound_to_the_reset_purpose(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="session-secret-for-email-tests",
            card_pepper="card-pepper",
            secure_cookies=False,
            portal_name="Test",
            session_max_age=3600,
        )
        form = {
            "enabled": "on",
            "host": "smtp.example.com",
            "port": "465",
            "transport": "ssl",
            "username": "mailer@example.com",
            "password": "smtp-app-password",
            "sender_email": "mailer@example.com",
            "sender_name": "Test sender",
            "code_ttl_minutes": "10",
        }
        with Session(engine) as db:
            save_email_config(db, settings, form, None)
            db.add(
                User(
                    username="recoverable",
                    email="recover@example.com",
                    password_hash=hash_password("old-password"),
                )
            )
            db.commit()
            with (
                patch("web_client.email_service._send_mail"),
                patch("web_client.email_service.secrets.randbelow", return_value=654321),
            ):
                request_password_reset_code(db, settings, "recover@example.com", "127.0.0.1")
            db.commit()
            record = db.scalar(select(EmailVerification))
            self.assertEqual(record.purpose, PASSWORD_RESET_PURPOSE)
            self.assertEqual(
                consume_password_reset_code(db, settings, "recover@example.com", "654321"),
                "recover@example.com",
            )

    def test_remote_snapshot_migration_adds_fingerprint_index(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE portal_users ("
                    "id INTEGER PRIMARY KEY, username VARCHAR(64) NOT NULL, "
                    "password_hash VARCHAR(255) NOT NULL, role VARCHAR(16) NOT NULL, "
                    "is_active BOOLEAN NOT NULL, created_at DATETIME NOT NULL, "
                    "last_login_at DATETIME)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE portal_group_config_snapshots ("
                    "id INTEGER PRIMARY KEY, group_id VARCHAR(32) NOT NULL, "
                    "revision INTEGER NOT NULL, config_json JSON NOT NULL, "
                    "updated_by INTEGER, updated_at DATETIME NOT NULL)"
                )
            )
        _migrate_database(engine)
        inspector = inspect(engine)
        columns = {
            item["name"]
            for item in inspector.get_columns("portal_group_config_snapshots")
        }
        indexes = {
            item["name"]
            for item in inspector.get_indexes("portal_group_config_snapshots")
        }
        self.assertIn("auth_card_fingerprint", columns)
        self.assertIn(
            "ix_portal_group_config_snapshots_auth_card_fingerprint", indexes
        )

    def test_activated_card_registration_derives_group_and_does_not_reactivate(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        card = "JK30-1234567890123ABCDEF123456"
        fake_auth = FakeAuthManager(card)
        fake_config = FakeGroupConfig()
        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="session-secret",
            card_pepper="card-pepper",
            secure_cookies=False,
            portal_name="Test",
            session_max_age=3600,
        )

        with Session(engine) as db:
            user = User(username="tester", password_hash=hash_password("password123"))
            db.add(user)
            db.flush()
            with (
                patch("web_client.services.auth_manager", return_value=fake_auth),
                patch("web_client.services.group_config_module", return_value=fake_config),
            ):
                access = claim_activated_card(db, user, card, settings)
                db.commit()

            self.assertEqual(access.group_id, "1102579807")
            self.assertEqual(db.scalar(select(GroupAccess)).user_id, user.id)
            self.assertEqual(db.scalar(select(GroupConfigSnapshot)).revision, 1)
            self.assertIn(card, fake_auth.used)
            self.assertEqual(
                fake_auth.get_group_auth_info("1102579807")["user_id"],
                "2650362143",
            )

    def test_unused_or_expired_card_cannot_register_web_account(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        card = "JK30-1234567890123ABCDEF123456"
        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="session-secret",
            card_pepper="card-pepper",
            secure_cookies=False,
            portal_name="Test",
            session_max_age=3600,
        )

        with Session(engine) as db:
            user = User(username="tester", password_hash=hash_password("password123"))
            db.add(user)
            db.flush()

            unused = FakeAuthManager(card)
            unused.used = {}
            with patch("web_client.services.auth_manager", return_value=unused):
                with self.assertRaisesRegex(PortalError, "尚未在群聊激活"):
                    claim_activated_card(db, user, card, settings)

            expired = FakeAuthManager(card, active=False)
            with patch("web_client.services.auth_manager", return_value=expired):
                with self.assertRaisesRegex(PortalError, "授权已过期"):
                    claim_activated_card(db, user, card, settings)

            replaced = FakeAuthManager(card)
            replaced.groups["1102579807"]["key"] = "JK30-NEW-CURRENT-CARD"
            with patch("web_client.services.auth_manager", return_value=replaced):
                with self.assertRaisesRegex(PortalError, "不是本群当前授权卡密"):
                    claim_activated_card(db, user, card, settings)

    def test_multiple_users_have_independent_group_access(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        card_a = "JK30-1234567890123ABCDEF123456"
        card_b = "JK30-1234567890123ABCDEF654321"
        auth_a = FakeAuthManager(card_a, "1102579807")
        auth_b = FakeAuthManager(card_b, "568586484")
        fake_config = FakeGroupConfig()
        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="session-secret",
            card_pepper="card-pepper",
            secure_cookies=False,
            portal_name="Test",
            session_max_age=3600,
        )

        with Session(engine) as db:
            user_a = User(username="customer_a", password_hash=hash_password("password-a"))
            user_b = User(username="customer_b", password_hash=hash_password("password-b"))
            db.add_all((user_a, user_b))
            db.flush()

            with (
                patch("web_client.services.auth_manager", return_value=auth_a),
                patch("web_client.services.group_config_module", return_value=fake_config),
            ):
                claim_activated_card(db, user_a, card_a, settings)
            with (
                patch("web_client.services.auth_manager", return_value=auth_b),
                patch("web_client.services.group_config_module", return_value=fake_config),
            ):
                claim_activated_card(db, user_b, card_b, settings)
            db.commit()

            self.assertIsNotNone(get_user_group(db, user_a.id, "1102579807"))
            self.assertIsNone(get_user_group(db, user_a.id, "568586484"))
            self.assertIsNotNone(get_user_group(db, user_b.id, "568586484"))
            self.assertIsNone(get_user_group(db, user_b.id, "1102579807"))

            with patch("web_client.services.auth_manager", return_value=auth_a):
                with self.assertRaisesRegex(PortalError, "绑定其他客户账号"):
                    claim_activated_card(db, user_b, card_a, settings)

    def test_one_customer_can_claim_multiple_group_cards(self):
        """A username/password owns several independent group authorizations."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        cards = (
            ("JK30-1234567890123ABCDEF123456", "1102579807"),
            ("JK30-1234567890123ABCDEF654321", "568586484"),
        )
        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="session-secret",
            card_pepper="card-pepper",
            secure_cookies=False,
            portal_name="Test",
            session_max_age=3600,
        )

        with Session(engine) as db:
            customer = User(
                username="multi_group_customer",
                password_hash=hash_password("customer-password"),
            )
            db.add(customer)
            db.flush()
            for card, group_id in cards:
                auth = FakeAuthManager(card, group_id)
                with (
                    patch("web_client.services.auth_manager", return_value=auth),
                    patch(
                        "web_client.services.group_config_module",
                        return_value=FakeGroupConfig(),
                    ),
                ):
                    access = claim_activated_card(db, customer, card, settings)
                self.assertEqual(access.group_id, group_id)
            db.commit()

            owned = list(
                db.scalars(
                    select(GroupAccess)
                    .where(GroupAccess.user_id == customer.id)
                    .order_by(GroupAccess.group_id)
                )
            )
            self.assertEqual(
                [item.group_id for item in owned], ["1102579807", "568586484"]
            )

    def test_admin_creation_and_last_admin_protection(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            admin = create_admin_account(db, "portal_admin", "admin-password")
            customer = User(
                username="customer",
                password_hash=hash_password("customer-password"),
            )
            db.add(customer)
            db.commit()

            self.assertTrue(admin.is_admin)
            self.assertTrue(admin.is_active)
            self.assertFalse(can_remove_admin(db, admin, admin))

            second_admin = create_admin_account(db, "second_admin", "admin-password-2")
            self.assertTrue(can_remove_admin(db, second_admin, admin))
            self.assertTrue(can_remove_admin(db, customer, admin))

    def test_remote_exchange_queues_and_acknowledges_web_revision(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            changes = remote_store.apply_bot_exchange(
                db,
                "desktop",
                [{
                    "group_id": "1102579807",
                    "config": {"group_id": "1102579807", "features": {"power_status": "on"}},
                    "auth": {"state": "active", "expire": -1, "expire_text": "永久授权"},
                }],
            )
            self.assertEqual(changes, [])
            snapshot = db.scalar(select(GroupConfigSnapshot))
            edited = dict(snapshot.config_json)
            edited["features"] = dict(edited["features"])
            edited["features"]["power_status"] = "off"
            remote_store.save_web_config(db, snapshot.group_id, edited, 1)
            changes = remote_store.apply_bot_exchange(
                db,
                "desktop",
                [{
                    "group_id": "1102579807",
                    "config": {"group_id": "1102579807", "features": {"power_status": "on"}},
                    "auth": {"state": "active", "expire": -1},
                }],
            )
            self.assertEqual(len(changes), 1)
            revision = changes[0]["revision"]
            self.assertEqual(changes[0]["source"], "web")
            self.assertEqual(changes[0]["updated_by"], 1)
            self.assertTrue(changes[0]["updated_at"])
            applied = remote_store.acknowledge_bot_changes(
                db,
                "desktop",
                [{"group_id": "1102579807", "revision": revision, "config": changes[0]["config"]}],
            )
            self.assertEqual(applied, 1)
            self.assertEqual(snapshot.applied_revision, snapshot.revision)
            self.assertEqual(snapshot.sync_status, "synced")

    def test_remote_exchange_serializes_pending_web_groups(self):
        """One Windows poll may apply only the most recent pending group."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        first_group = "1102579807"
        latest_group = "568586484"
        local_payloads = [
            {
                "group_id": first_group,
                "config": {
                    "group_id": first_group,
                    "features": {"power_status": "on"},
                },
                "auth": {"state": "active", "expire": -1},
            },
            {
                "group_id": latest_group,
                "config": {
                    "group_id": latest_group,
                    "features": {"power_status": "on"},
                },
                "auth": {"state": "active", "expire": -1},
            },
        ]
        with Session(engine) as db:
            self.assertEqual(
                remote_store.apply_bot_exchange(
                    db, "desktop", local_payloads, [first_group, latest_group]
                ),
                [],
            )
            first = remote_store.get_snapshot(db, first_group)
            latest = remote_store.get_snapshot(db, latest_group)
            first_config = dict(first.config_json)
            first_config["features"] = {"power_status": "off"}
            remote_store.save_web_config(db, first_group, first_config, 1)
            latest_config = dict(latest.config_json)
            latest_config["features"] = {"power_status": "off"}
            remote_store.save_web_config(db, latest_group, latest_config, 1)

            changes = remote_store.apply_bot_exchange(
                db, "desktop", local_payloads, [first_group, latest_group]
            )
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["group_id"], latest_group)
            # Pending browser data remains canonical: stale Windows
            # projections cannot overwrite either saved group.
            self.assertEqual(
                remote_store.get_config(db, first_group)["features"]["power_status"],
                "off",
            )
            self.assertEqual(
                remote_store.get_config(db, latest_group)["features"]["power_status"],
                "off",
            )
            self.assertEqual(
                remote_store.acknowledge_bot_changes(
                    db,
                    "desktop",
                    [
                        {
                            "group_id": latest_group,
                            "revision": changes[0]["revision"],
                            "config": changes[0]["config"],
                        }
                    ],
                ),
                1,
            )
            next_changes = remote_store.apply_bot_exchange(
                db, "desktop", [], [first_group, latest_group]
            )
            self.assertEqual([item["group_id"] for item in next_changes], [first_group])

    def test_never_seen_sync_state_explains_transport_boundary(self):
        state = remote_store.sync_state(None, 180)
        self.assertEqual(state["status"], "never")
        self.assertEqual(state["badge"], "尚未连通")
        self.assertIn("服务器尚未收到", state["text"])
        self.assertIn("http_status_525", state["detail"])

    def test_never_seen_snapshot_keeps_durable_refresh_request_visible(self):
        """A queued pull must not disappear from a first-sync group page."""
        snapshot = GroupConfigSnapshot(
            group_id="1102579807",
            revision=1,
            applied_revision=1,
            config_json={"group_id": "1102579807", "features": {}},
            refresh_requested=True,
        )

        state = remote_store.sync_state(snapshot, 180)

        self.assertEqual(state["status"], "never")
        self.assertFalse(state["online"])
        self.assertTrue(state["refresh_requested"])

    def test_stale_windows_client_cannot_ack_after_group_ownership_changes(self):
        """A previous Windows instance must not complete the current ACK."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "windows-current",
                [{
                    "group_id": "1102579807",
                    "config": {
                        "group_id": "1102579807",
                        "features": {"power_status": "on"},
                    },
                    "auth": {"state": "active", "expire": -1},
                }],
            )
            snapshot = db.scalar(select(GroupConfigSnapshot))
            edited = dict(snapshot.config_json)
            edited["features"] = dict(edited["features"])
            edited["features"]["power_status"] = "off"
            remote_store.save_web_config(db, snapshot.group_id, edited, 1)
            pending_revision = snapshot.revision

            # Another instance has since uploaded a heartbeat and now owns
            # the group.  The stale worker's ACK must remain pending.
            snapshot.client_id = "windows-current"
            self.assertEqual(
                remote_store.acknowledge_bot_changes(
                    db,
                    "windows-stale",
                    [{
                        "group_id": snapshot.group_id,
                        "revision": pending_revision,
                        "config": edited,
                    }],
                ),
                0,
            )
            self.assertEqual(snapshot.applied_revision, pending_revision - 1)
            self.assertEqual(snapshot.sync_status, "pending")

    def test_stale_windows_client_cannot_report_failure_for_current_revision(self):
        """Failure callbacks are scoped to the snapshot's current owner."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "windows-current",
                [{
                    "group_id": "1102579807",
                    "config": {
                        "group_id": "1102579807",
                        "features": {"power_status": "on"},
                    },
                    "auth": {"state": "active", "expire": -1},
                }],
                ["1102579807"],
            )
            snapshot = db.scalar(select(GroupConfigSnapshot))
            edited = dict(snapshot.config_json)
            edited["features"] = dict(edited["features"])
            edited["features"]["power_status"] = "off"
            remote_store.save_web_config(db, snapshot.group_id, edited, 1)
            pending_revision = snapshot.revision

            self.assertEqual(
                remote_store.record_bot_sync_failures(
                    db,
                    "windows-stale",
                    [{
                        "group_id": snapshot.group_id,
                        "revision": pending_revision,
                        "error": "local_config_save_failed",
                    }],
                ),
                0,
            )
            self.assertEqual(snapshot.sync_status, "pending")
            self.assertEqual(
                remote_store.record_bot_sync_failures(
                    db,
                    "windows-current",
                    [{
                        "group_id": snapshot.group_id,
                        "revision": pending_revision,
                        "error": "local_config_save_failed",
                    }],
                ),
                1,
            )
            self.assertEqual(snapshot.sync_status, "error")

    def test_console_events_are_limited_to_current_client_groups(self):
        """A sync client cannot inject CMD records into another client's group."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "windows-current",
                [{
                    "group_id": "1102579807",
                    "config": {"group_id": "1102579807", "features": {}},
                    "auth": {"state": "active", "expire": -1},
                }],
                ["1102579807"],
            )
            remote_store.apply_bot_exchange(
                db,
                "windows-other",
                [{
                    "group_id": "568586484",
                    "config": {"group_id": "568586484", "features": {}},
                    "auth": {"state": "active", "expire": -1},
                }],
                ["568586484"],
            )
            accepted = remote_store.ingest_console_events(
                db,
                "windows-current",
                [
                    {
                        "event_id": "event-current-1",
                        "group_id": "1102579807",
                        "action": "message",
                    },
                    {
                        "event_id": "event-other-1",
                        "group_id": "568586484",
                        "action": "message",
                    },
                ],
            )
            self.assertEqual(accepted, 1)
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(PortalConsoleEvent)
                ),
                1,
            )
            self.assertEqual(
                db.scalar(select(PortalConsoleEvent.group_id)), "1102579807"
            )

    def test_claimed_remote_group_card_grants_editable_configuration_access(self):
        """A synced group-bound card must unlock a customer's web save path.

        This covers the workflow used by ``/添加授权 [天数]``: Windows creates
        a current group card, sends only its fingerprint to Linux, then the
        customer claims that card with their own portal account/password.
        """
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        group_id = "1102579807"
        card = "JK30-1234567890123ABCDEF123456"
        pepper = "p" * 32
        with patch.dict(
            os.environ,
            {
                "JOKER_WEB_DATA_MODE": "remote",
                "JOKER_WEB_SESSION_SECRET": "s" * 32,
                "JOKER_WEB_CARD_PEPPER": pepper,
                "JOKER_WEB_SYNC_TOKEN": "t" * 32,
            },
        ):
            settings = Settings.from_env()
            with Session(engine) as db:
                customer = User(
                    username="customer",
                    password_hash=hash_password("customer-password"),
                )
                db.add(customer)
                db.flush()
                remote_store.apply_bot_exchange(
                    db,
                    "windows-test",
                    [
                        {
                            "group_id": group_id,
                            "config": {
                                "group_id": group_id,
                                "features": {"power_status": "off"},
                            },
                            "auth": {
                                "state": "active",
                                "expire": 1_900_000_000,
                                "expire_text": "2030-03-17 17:46",
                                "card_fingerprint": card_fingerprint(card, pepper),
                                "card_mask": mask_card(card),
                                "activated_at": 1_800_000_000,
                            },
                        }
                    ],
                    [group_id],
                    authoritative_group_ids=True,
                )

                access = claim_activated_card(db, customer, card, settings)
                values = {
                    field.key: field.default
                    for field in FEATURE_BY_KEY["reply"].fields
                }
                values["enabled"] = True
                apply_feature_values(group_id, "reply", values, db, customer.id)

                view = get_group_view(group_id, db)
                snapshot = db.scalar(select(GroupConfigSnapshot))
                self.assertEqual(access.group_id, group_id)
                self.assertIsNotNone(get_user_group(db, customer.id, group_id))
                self.assertTrue(view["auth"]["active"])
                self.assertTrue(
                    view["config"]["features"]["reply"]["enabled"]
                )
                self.assertGreater(snapshot.revision, snapshot.applied_revision)
                self.assertEqual(snapshot.sync_status, "pending")

    def test_remote_clone_copies_group_features_without_target_auth_or_power(self):
        """A customer may clone one owned group into another safely."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        source_group_id = "1102579807"
        target_group_id = "568586484"
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "windows-test",
                [
                    {
                        "group_id": source_group_id,
                        "config": {
                            "group_id": source_group_id,
                            "features": {
                                "power_status": "on",
                                "welcome": {"enabled": True, "message": "源群欢迎"},
                                "stats": {
                                    "enabled": True,
                                    "database_profile": "main",
                                    "database_path": "data/source.db",
                                    "database_profiles": [
                                        {
                                            "id": "main",
                                            "name": "主服",
                                            "path": "data/source.db",
                                        }
                                    ],
                                },
                            },
                            "timed_messages": [
                                {"id": "morning", "message": "早上好", "enabled": True}
                            ],
                            "replies": {"exact": {"你好": {"reply": "你好呀"}}},
                            "sub_admins": [{"user_id": "2650362143"}],
                            "player_replies": {
                                "10001": {"messages": ["专属回复"], "at_user": True}
                            },
                        },
                        "auth": {
                            "state": "active",
                            "expire": -1,
                            "card_fingerprint": "a" * 64,
                        },
                    },
                    {
                        "group_id": target_group_id,
                        "config": {
                            "group_id": target_group_id,
                            "features": {
                                "power_status": "off",
                                "welcome": {"enabled": False},
                            },
                        },
                        "auth": {
                            "state": "active",
                            "expire": 1_900_000_000,
                            "card_fingerprint": "b" * 64,
                            "card_mask": "JK30-****99999",
                        },
                    },
                ],
                [source_group_id, target_group_id],
            )

            with patch("web_client.services.remote_data_mode", return_value=True):
                cloned = clone_group_config(
                    source_group_id, target_group_id, db, updated_by=7
                )

            target = remote_store.get_snapshot(db, target_group_id)
            self.assertEqual(cloned["group_id"], target_group_id)
            self.assertEqual(cloned["features"]["power_status"], "off")
            self.assertTrue(cloned["features"]["welcome"]["enabled"])
            self.assertEqual(cloned["timed_messages"][0]["id"], "morning")
            self.assertEqual(cloned["replies"]["exact"]["你好"]["reply"], "你好呀")
            self.assertEqual(cloned["sub_admins"][0]["user_id"], "2650362143")
            self.assertIn("10001", cloned["player_replies"])
            self.assertTrue(cloned["features"]["stats"]["enabled"])
            self.assertEqual("", cloned["features"]["stats"]["database_profile"])
            self.assertEqual([], cloned["features"]["stats"]["database_profiles"])
            self.assertEqual("", cloned["features"]["stats"]["database_path"])
            self.assertEqual(target.auth_state, "active")
            self.assertEqual(target.auth_card_fingerprint, "b" * 64)
            self.assertEqual(target.config_json["features"]["power_status"], "off")
            self.assertGreater(target.revision, target.applied_revision)

    def test_remote_missing_snapshot_hint_excludes_groups_just_uploaded(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            local_group_id = "1102579807"
            absent_group_id = "568586484"
            remote_store.apply_bot_exchange(
                db,
                "desktop",
                [{
                    "group_id": local_group_id,
                    "config": {"group_id": local_group_id, "features": {}},
                    "auth": {"state": "active", "expire": -1},
                }],
                [local_group_id, absent_group_id],
            )

            self.assertEqual(
                remote_store.missing_bot_group_ids(
                    db, [local_group_id, absent_group_id, "not-a-group"]
                ),
                [absent_group_id],
            )

    def test_protected_heartbeat_touches_liveness_without_mutating_snapshot(self):
        """A source-protected exchange is online but cannot alter group state."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        group_id = "1102579807"
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [
                    {
                        "group_id": group_id,
                        "config": {
                            "group_id": group_id,
                            "features": {"power_status": "on"},
                        },
                        "auth": {"state": "active", "expire": -1},
                    }
                ],
                [group_id],
                authoritative_group_ids=True,
            )
            snapshot = remote_store.get_snapshot(db, group_id, for_update=True)
            assert snapshot is not None
            snapshot.refresh_requested = True
            before_seen = snapshot.last_seen_at
            before_config = dict(snapshot.config_json)
            before_auth_state = snapshot.auth_state
            before_revision = snapshot.revision

            self.assertEqual(remote_store.touch_client_heartbeat(db, "windows-a"), 1)
            self.assertGreaterEqual(snapshot.last_seen_at, before_seen)
            self.assertEqual(snapshot.config_json, before_config)
            self.assertEqual(snapshot.auth_state, before_auth_state)
            self.assertEqual(snapshot.revision, before_revision)
            self.assertTrue(snapshot.refresh_requested)
            self.assertEqual(
                snapshot.sync_status, remote_store.LOCAL_SOURCE_PROTECTED_STATUS
            )
            state = remote_store.sync_state(snapshot, 180)
            self.assertEqual(state["status"], "protected")
            self.assertTrue(state["online"])
            self.assertEqual(state["badge"], "Windows 数据读取保护")

            # The next complete source read clears only the temporary
            # protection marker and still fulfills the queued browser refresh.
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [
                    {
                        "group_id": group_id,
                        "config": {
                            "group_id": group_id,
                            "features": {"power_status": "on"},
                        },
                        "auth": {"state": "active", "expire": -1},
                    }
                ],
                [group_id],
                authoritative_group_ids=True,
            )
            self.assertEqual(snapshot.sync_status, "synced")
            self.assertEqual(snapshot.sync_error, "")
            self.assertFalse(snapshot.refresh_requested)

    def test_windows_full_snapshot_deletion_removes_stale_reply_from_server(self):
        """A later Windows empty collection is authoritative for that snapshot."""

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        group_id = "1102579807"
        first_config = {
            "group_id": group_id,
            "features": {},
            "replies": {
                "exact": {"问题1": {"reply": "答案1"}},
                "fuzzy": [],
                "regex": [],
            },
        }
        deleted_config = {
            "group_id": group_id,
            "features": {},
            "replies": {"exact": {}, "fuzzy": [], "regex": []},
        }

        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [{
                    "group_id": group_id,
                    "config": first_config,
                    "auth": {"state": "active", "expire": -1},
                }],
                [group_id],
                sync_scope_complete=True,
            )
            snapshot = remote_store.get_snapshot(db, group_id)
            self.assertIsNotNone(snapshot)
            self.assertIn("问题1", snapshot.config_json["replies"]["exact"])

            # This models the next complete Windows upload after /删除.  No
            # web revision is pending, so the local projection must replace
            # the prior collection rather than merge it with stale data.
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [{
                    "group_id": group_id,
                    "config": deleted_config,
                    "auth": {"state": "active", "expire": -1},
                }],
                [group_id],
                sync_scope_complete=True,
            )
            self.assertEqual(
                snapshot.config_json["replies"],
                {"exact": {}, "fuzzy": [], "regex": []},
            )

    def test_partial_auth_source_syncs_config_without_trusting_stale_card(self):
        """A readable config file must not get stuck behind a broken auth.json."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        group_id = "1102579807"
        card_fingerprint = "b" * 64
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [{
                    "group_id": group_id,
                    "config": {
                        "group_id": group_id,
                        "features": {"power_status": "on"},
                    },
                    "auth": {
                        "state": "active",
                        "expire": -1,
                        "card_fingerprint": card_fingerprint,
                    },
                }],
                [group_id],
                authoritative_group_ids=True,
            )
            snapshot = remote_store.get_snapshot(db, group_id)
            assert snapshot is not None
            snapshot.refresh_requested = True
            db.flush()

            changes = remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [{
                    "group_id": group_id,
                    "config": {
                        "group_id": group_id,
                        "features": {"power_status": "off"},
                    },
                    # Deliberately omit auth: auth.json could not be read.
                }],
                [group_id],
                authoritative_group_ids=False,
                config_source_complete=True,
                auth_source_complete=False,
            )
            self.assertEqual(snapshot.auth_card_fingerprint, card_fingerprint)
            self.assertEqual(snapshot.auth_state, "unverified")
            self.assertEqual(snapshot.sync_status, remote_store.LOCAL_SOURCE_PARTIAL_STATUS)
            self.assertFalse(snapshot.refresh_requested)
            self.assertEqual(
                remote_store.get_auth_state(db, group_id)["active"], False
            )
            self.assertEqual(
                remote_store.get_config(db, group_id)["features"]["power_status"],
                "off",
            )
            self.assertEqual(changes, [])

            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [{
                    "group_id": group_id,
                    "config": {
                        "group_id": group_id,
                        "features": {"power_status": "off"},
                    },
                    "auth": {
                        "state": "active",
                        "expire": -1,
                        "card_fingerprint": card_fingerprint,
                    },
                }],
                [group_id],
                authoritative_group_ids=True,
            )
            self.assertEqual(snapshot.auth_state, "active")
            self.assertEqual(snapshot.sync_status, "synced")

    def test_unverified_card_blocks_claim_and_group_writes(self):
        """A partial auth source is diagnostic-only until Windows recovers."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        group_id = "1102579807"
        card = "JK30-1234567890123ABCDEF123456"
        pepper = "p" * 32
        with patch.dict(
            os.environ,
            {
                "JOKER_WEB_DATA_MODE": "remote",
                "JOKER_WEB_CARD_PEPPER": pepper,
                "JOKER_WEB_SESSION_SECRET": "s" * 32,
                "JOKER_WEB_SYNC_TOKEN": "t" * 32,
            },
            clear=False,
        ):
            settings = Settings.from_env()
            with Session(engine) as db:
                user = User(
                    username="partial_customer",
                    password_hash=hash_password("password123"),
                )
                db.add(user)
                db.flush()
                remote_store.apply_bot_exchange(
                    db,
                    "windows-partial",
                    [
                        {
                            "group_id": group_id,
                            "config": {"group_id": group_id, "features": {}},
                            "auth": {
                                "state": "active",
                                "expire": -1,
                                "card_fingerprint": card_fingerprint(card, pepper),
                                "card_mask": mask_card(card),
                            },
                        }
                    ],
                    [group_id],
                    authoritative_group_ids=True,
                )
                remote_store.apply_bot_exchange(
                    db,
                    "windows-partial",
                    [
                        {
                            "group_id": group_id,
                            "config": {"group_id": group_id, "features": {}},
                        }
                    ],
                    [group_id],
                    config_source_complete=True,
                    auth_source_complete=False,
                )
                with self.assertRaisesRegex(PortalError, "当前不能验证"):
                    claim_activated_card(db, user, card, settings)
                editable, reason = _group_edit_status(
                    group_id,
                    db,
                    auth={"active": False, "state": "unverified"},
                    sync={"online": True, "status": "partial"},
                )
                self.assertFalse(editable)
                self.assertIn("读取不完整", reason)

    def test_login_refresh_is_client_scoped_and_clears_after_full_upload(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            group_id = "1102579807"
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [{
                    "group_id": group_id,
                    "config": {"group_id": group_id, "features": {}},
                    "auth": {"state": "active", "expire": -1},
                }],
                [group_id],
            )
            snapshot = db.scalar(select(GroupConfigSnapshot))
            self.assertFalse(snapshot.refresh_requested)

            self.assertEqual(
                remote_store.request_group_refresh(db, [group_id, "bad-group"]), 1
            )
            self.assertTrue(snapshot.refresh_requested)
            self.assertEqual(
                remote_store.requested_group_refresh_ids(db, "windows-a", [group_id]),
                [group_id],
            )
            self.assertEqual(
                remote_store.requested_group_refresh_ids(db, "windows-b", [group_id]),
                [],
            )

            # Only a complete group payload from the same Windows client
            # fulfills the durable login-triggered refresh request.
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [{
                    "group_id": group_id,
                    "config": {"group_id": group_id, "features": {"power_status": "on"}},
                    "auth": {"state": "active", "expire": -1},
                }],
                [group_id],
            )
            self.assertFalse(snapshot.refresh_requested)

    def test_first_sync_refresh_request_creates_a_safe_placeholder(self):
        """A bound group can request its first Windows upload without a row yet.

        The placeholder is only a durable pull hint.  It must not look like
        an authorization or a completed configuration until Windows has sent
        the full local projection back.
        """

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        group_id = "1102579807"
        with Session(engine) as db:
            self.assertEqual(remote_store.request_group_refresh(db, [group_id]), 1)
            db.flush()
            snapshot = remote_store.get_snapshot(db, group_id)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.revision, 0)
            self.assertEqual(snapshot.applied_revision, 0)
            self.assertEqual(
                snapshot.config_json, {"group_id": group_id, "features": {}}
            )
            self.assertEqual(snapshot.auth_state, "missing")
            self.assertTrue(snapshot.refresh_requested)
            self.assertEqual(snapshot.client_id, "")

            state = remote_store.sync_state(snapshot, 180)
            self.assertFalse(state["online"])
            self.assertTrue(state["refresh_requested"])
            self.assertEqual(state["badge"], "等待首次补传")

            # The first small heartbeat associates the local client and
            # delivers the pull hint.  It intentionally does not turn an
            # empty placeholder into a valid authorization.
            remote_store.apply_bot_exchange(db, "windows-a", [], [group_id])
            self.assertEqual(snapshot.client_id, "windows-a")
            self.assertIsNotNone(snapshot.last_seen_at)
            self.assertEqual(snapshot.auth_state, "missing")
            self.assertEqual(
                remote_store.requested_group_refresh_ids(db, "windows-a", [group_id]),
                [group_id],
            )

            # The Windows client then invalidates only this local cache entry
            # and sends its normal full projection.  That replaces the
            # placeholder and clears the queued request.
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [
                    {
                        "group_id": group_id,
                        "config": {
                            "group_id": group_id,
                            "features": {"power_status": "on"},
                        },
                        "auth": {"state": "active", "expire": -1},
                    }
                ],
                [group_id],
            )
            self.assertFalse(snapshot.refresh_requested)
            self.assertEqual(snapshot.auth_state, "active")
            self.assertEqual(
                snapshot.config_json["features"]["power_status"], "on"
            )

    def test_auth_only_group_fulfills_refresh_without_creating_fake_config(self):
        """A complete auth-only projection must not leave refresh queued forever."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        group_id = "1102579807"
        with Session(engine) as db:
            self.assertEqual(remote_store.request_group_refresh(db, [group_id]), 1)
            snapshot = remote_store.get_snapshot(db, group_id)
            assert snapshot is not None
            self.assertTrue(snapshot.refresh_requested)

            # The local config source is complete, and its known inventory
            # confirms that this manually-authorized group has no JSON file.
            # Windows therefore sends auth only; this is not an unreadable
            # source and must complete the durable pull request.
            remote_store.apply_bot_exchange(
                db,
                "windows-auth-only",
                [
                    {
                        "group_id": group_id,
                        "auth": {"state": "active", "expire": -1},
                    }
                ],
                [group_id],
                authoritative_group_ids=True,
                config_source_complete=True,
                auth_source_complete=True,
            )
            self.assertFalse(snapshot.refresh_requested)
            self.assertEqual(snapshot.auth_state, "active")
            self.assertEqual(
                snapshot.config_json, {"group_id": group_id, "features": {}}
            )

            # An unreadable auth.json fails closed without inventing a missing
            # card.  Once Windows sends the same auth-only projection again,
            # the active state must be restored rather than remaining stuck at
            # ``unverified`` because there is no config payload to change.
            remote_store.apply_bot_exchange(
                db,
                "windows-auth-only",
                [],
                [group_id],
                config_source_complete=True,
                auth_source_complete=False,
            )
            self.assertEqual(snapshot.auth_state, "unverified")
            self.assertEqual(snapshot.sync_status, remote_store.LOCAL_SOURCE_PARTIAL_STATUS)
            remote_store.apply_bot_exchange(
                db,
                "windows-auth-only",
                [
                    {
                        "group_id": group_id,
                        "auth": {"state": "active", "expire": -1},
                    }
                ],
                [group_id],
                authoritative_group_ids=True,
                config_source_complete=True,
                auth_source_complete=True,
            )
            self.assertEqual(snapshot.auth_state, "active")
            self.assertEqual(snapshot.sync_status, "synced")

    def test_removed_local_group_keeps_refresh_diagnostic_tombstone(self):
        """A deleted Windows group must not turn a portal refresh into silence.

        The retained row deliberately contains no old config or card state.
        It belongs only to the former client long enough to deliver the
        per-group diagnostic that the Windows-side client writes to CMD.
        """

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        group_id = "1102579807"
        client_id = "windows-a"
        local_payload = {
            "group_id": group_id,
            "config": {
                "group_id": group_id,
                "features": {"power_status": "on"},
                "replies": {"exact": {"old": {"reply": "old value"}}},
            },
            "auth": {
                "state": "active",
                "expire": -1,
                "card_fingerprint": "a" * 64,
            },
        }
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db, client_id, [local_payload], [group_id], authoritative_group_ids=True
            )
            self.assertEqual(remote_store.request_group_refresh(db, [group_id]), 1)

            # Windows now reports a complete authoritative group list with
            # this group absent.  The former sensitive projection must go,
            # while the pending pull hint stays routable to the same client.
            remote_store.apply_bot_exchange(
                db, client_id, [], [], authoritative_group_ids=True
            )
            snapshot = remote_store.get_snapshot(db, group_id)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.revision, 0)
            self.assertEqual(snapshot.applied_revision, 0)
            self.assertEqual(snapshot.config_json, {"group_id": group_id, "features": {}})
            self.assertEqual(snapshot.auth_state, "missing")
            self.assertEqual(snapshot.auth_card_fingerprint, "")
            self.assertEqual(snapshot.sync_status, "unavailable")
            self.assertTrue(snapshot.refresh_requested)
            self.assertEqual(
                remote_store.requested_group_refresh_ids(db, client_id, []), [group_id]
            )
            state = remote_store.sync_state(snapshot, 180)
            self.assertEqual(state["status"], "unavailable")
            self.assertIn("本地不存在", state["display_text"])

            # Restoring the group locally replaces the tombstone and fulfills
            # the stored refresh request normally.
            remote_store.apply_bot_exchange(
                db, client_id, [local_payload], [group_id], authoritative_group_ids=True
            )
            self.assertEqual(snapshot.sync_status, "synced")
            self.assertFalse(snapshot.refresh_requested)
            self.assertEqual(snapshot.auth_state, "active")
            self.assertEqual(snapshot.config_json["replies"]["exact"]["old"]["reply"], "old value")

    def test_refresh_queue_prioritizes_live_groups_and_consumes_tombstone_notice(self):
        """A removed group must not permanently occupy the one refresh slot."""

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            removed_group_id = "10000"
            live_group_id = "99999"
            for group_id in (removed_group_id, live_group_id):
                remote_store.apply_bot_exchange(
                    db,
                    "windows-a",
                    [{
                        "group_id": group_id,
                        "config": {"group_id": group_id, "features": {}},
                        "auth": {"state": "active", "expire": -1},
                    }],
                    [group_id],
                )
            removed = remote_store.get_snapshot(db, removed_group_id, for_update=True)
            live = remote_store.get_snapshot(db, live_group_id, for_update=True)
            assert removed is not None and live is not None
            removed.sync_status = remote_store.LOCAL_GROUP_MISSING_STATUS
            removed.refresh_requested = True
            live.refresh_requested = True
            db.flush()

            # The numerically earlier, unavailable ID used to win every poll.
            self.assertEqual(
                remote_store.requested_group_refresh_ids(
                    db, "windows-a", [live_group_id]
                ),
                [live_group_id],
            )
            self.assertTrue(removed.refresh_requested)
            self.assertTrue(live.refresh_requested)

            # The Windows client fulfills the live request in its immediate
            # follow-up exchange.  Only then may the stale diagnostic consume
            # the queue slot once.
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [{
                    "group_id": live_group_id,
                    "config": {"group_id": live_group_id, "features": {}},
                    "auth": {"state": "active", "expire": -1},
                }],
                [live_group_id],
            )
            self.assertFalse(live.refresh_requested)

            # The stale group still reaches CMD once, then its no-op request is
            # consumed so it cannot starve future customer requests.
            self.assertEqual(
                remote_store.requested_group_refresh_ids(db, "windows-a", []),
                [removed_group_id],
            )
            self.assertFalse(removed.refresh_requested)
            self.assertEqual(
                remote_store.requested_group_refresh_ids(db, "windows-a", []), []
            )

    def test_autoflush_disabled_exchange_preserves_full_upload_before_runtime_queries(self):
        """Production sessions disable autoflush; lock rereads must not undo a sync."""

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        group_id = "1102579807"
        with Session(engine, autoflush=False) as db:
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [{
                    "group_id": group_id,
                    "config": {"group_id": group_id, "features": {"power_status": "off"}},
                    "auth": {"state": "missing", "expire": 0},
                }],
                [group_id],
                authoritative_group_ids=True,
            )
            db.commit()

            snapshot = remote_store.get_snapshot(db, group_id, for_update=True)
            assert snapshot is not None
            snapshot.refresh_requested = True
            db.commit()

            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [{
                    "group_id": group_id,
                    "config": {"group_id": group_id, "features": {"power_status": "on"}},
                    "auth": {"state": "active", "expire": -1},
                }],
                [group_id],
                authoritative_group_ids=True,
                runtime_summaries={
                    group_id: {"stats": {"group": {"total_users": 3}}}
                },
            )
            # This query mirrors the post-exchange failure processing in the
            # real request path and used to reload stale values when autoflush
            # was disabled.
            self.assertEqual(
                remote_store.record_bot_sync_failures(
                    db,
                    "windows-a",
                    [{
                        "group_id": group_id,
                        "revision": 1,
                        "error": "ack_failed",
                    }],
                ),
                0,
            )
            db.commit()

        with Session(engine) as verify:
            snapshot = remote_store.get_snapshot(verify, group_id)
            assert snapshot is not None
            self.assertFalse(snapshot.refresh_requested)
            self.assertEqual(snapshot.auth_state, "active")
            self.assertEqual(snapshot.config_json["features"]["power_status"], "on")
            self.assertEqual(snapshot.runtime_json["stats"]["group"]["total_users"], 3)

    def test_authoritative_payload_never_revives_group_missing_from_known_ids(self):
        """Known IDs are the authority; stale payload entries cannot override them."""

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        group_id = "1102579807"
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [{
                    "group_id": group_id,
                    "config": {"group_id": group_id, "features": {"power_status": "on"}},
                    "auth": {"state": "active", "expire": -1},
                }],
                [group_id],
                authoritative_group_ids=True,
            )
            remote_store.request_group_refresh(db, [group_id])
            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [{
                    "group_id": group_id,
                    "config": {"group_id": group_id, "features": {"power_status": "off"}},
                    "auth": {"state": "active", "expire": -1},
                }],
                [],
                authoritative_group_ids=True,
            )
            snapshot = remote_store.get_snapshot(db, group_id)
            assert snapshot is not None
            self.assertEqual(snapshot.sync_status, remote_store.LOCAL_GROUP_MISSING_STATUS)
            self.assertTrue(snapshot.refresh_requested)
            self.assertEqual(snapshot.config_json, {"group_id": group_id, "features": {}})

    def test_runtime_summary_requires_the_reporting_group_and_client(self):
        """A heartbeat cannot overwrite another Windows client's runtime panel."""

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        first_group_id = "1102579807"
        second_group_id = "568586484"
        with Session(engine) as db:
            for client_id, group_id in (("windows-a", first_group_id), ("windows-b", second_group_id)):
                remote_store.apply_bot_exchange(
                    db,
                    client_id,
                    [{
                        "group_id": group_id,
                        "config": {"group_id": group_id, "features": {}},
                        "auth": {"state": "active", "expire": -1},
                    }],
                    [group_id],
                    authoritative_group_ids=True,
                )

            remote_store.apply_bot_exchange(
                db,
                "windows-a",
                [],
                [first_group_id],
                authoritative_group_ids=False,
                runtime_summaries={
                    second_group_id: {"stats": {"group": {"total_users": 99}}}
                },
            )
            snapshot = remote_store.get_snapshot(db, second_group_id)
            assert snapshot is not None
            self.assertEqual(snapshot.runtime_json, {})

    def test_remote_failure_report_requires_matching_pending_revision_and_ack_clears(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "desktop",
                [{
                    "group_id": "1102579807",
                    "config": {"group_id": "1102579807", "features": {"power_status": "on"}},
                    "auth": {"state": "active", "expire": -1},
                }],
                ["1102579807"],
            )
            snapshot = db.scalar(select(GroupConfigSnapshot))
            edited = dict(snapshot.config_json)
            edited["features"] = dict(edited["features"])
            edited["features"]["power_status"] = "off"
            remote_store.save_web_config(db, snapshot.group_id, edited, 1)
            pending_revision = snapshot.revision

            accepted = remote_store.record_bot_sync_failures(
                db,
                "desktop",
                [{
                    "group_id": "1102579807",
                    "revision": pending_revision,
                    "error": "local_config_save_failed",
                }],
            )
            self.assertEqual(accepted, 1)
            self.assertEqual(snapshot.sync_status, "error")
            self.assertEqual(snapshot.sync_error, "local_config_save_failed")
            state = remote_store.sync_state(snapshot, 180)
            self.assertTrue(state["pending"])
            self.assertEqual(state["status"], "error")
            self.assertEqual(state["text"], "local_config_save_failed")
            self.assertEqual(state["display_text"], "Windows 未能写入本地分群配置")
            self.assertIn("版本", state["detail"])

            # A stale revision and unsafe text must not change the current
            # pending state, even when the client has the sync token.
            self.assertEqual(
                remote_store.record_bot_sync_failures(
                    db,
                    "desktop",
                    [{
                        "group_id": "1102579807",
                        "revision": pending_revision - 1,
                        "error": "ack_failed:HTTPStatusError",
                    }],
                ),
                0,
            )
            self.assertEqual(
                remote_store.record_bot_sync_failures(
                    db,
                    "desktop",
                    [{
                        "group_id": "1102579807",
                        "revision": pending_revision,
                        "error": "ack_failed:https://secret.example/?token=leak",
                    }],
                ),
                0,
            )
            self.assertEqual(snapshot.sync_error, "local_config_save_failed")

            applied = remote_store.acknowledge_bot_changes(
                db,
                "desktop",
                [{
                    "group_id": "1102579807",
                    "revision": pending_revision,
                    "config": snapshot.config_json,
                }],
            )
            self.assertEqual(applied, 1)
            self.assertEqual(snapshot.sync_status, "synced")
            self.assertEqual(snapshot.sync_error, "")
            self.assertEqual(
                remote_store.record_bot_sync_failures(
                    db,
                    "desktop",
                    [{
                        "group_id": "1102579807",
                        "revision": pending_revision,
                        "error": "ack_failed:HTTPStatusError",
                    }],
                ),
                0,
            )

    def test_remote_same_config_retry_does_not_create_another_sync_revision(self):
        """A lost save redirect can be retried without duplicating Windows work."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "desktop",
                [{
                    "group_id": "1102579807",
                    "config": {"group_id": "1102579807", "features": {"power_status": "on"}},
                    "auth": {"state": "active", "expire": -1},
                }],
                ["1102579807"],
            )
            snapshot = db.scalar(select(GroupConfigSnapshot))
            updated = dict(snapshot.config_json)
            updated["features"] = dict(updated["features"])
            updated["features"]["power_status"] = "off"

            remote_store.save_web_config(db, snapshot.group_id, updated, 1)
            first_revision = snapshot.revision
            self.assertGreater(first_revision, snapshot.applied_revision)
            self.assertEqual(snapshot.sync_status, "pending")

            # This represents resubmitting the exact same browser form after
            # its original response timed out at the proxy/client boundary.
            remote_store.save_web_config(db, snapshot.group_id, updated, 1)
            self.assertEqual(snapshot.revision, first_revision)
            self.assertEqual(snapshot.sync_status, "pending")

    def test_remote_exchange_ignores_malformed_auth_metadata(self):
        """One bad local auth record must not block every portal synchronization."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "desktop",
                [{
                    "group_id": "1102579807",
                    "config": {"group_id": "1102579807", "features": {}},
                    "auth": {
                        "state": "not-a-real-state",
                        "expire": "not-a-number",
                        "activated_at": str(2**80),
                        "card_fingerprint": "not-a-fingerprint",
                    },
                }],
            )
            snapshot = db.scalar(select(GroupConfigSnapshot))
            self.assertEqual(snapshot.auth_state, "missing")
            self.assertEqual(snapshot.auth_expire, 0)
            self.assertEqual(snapshot.auth_activated_at, 0)
            self.assertEqual(snapshot.auth_card_fingerprint, "")

    def test_authoritative_exchange_redacts_locally_deleted_projection(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "desktop",
                [{
                    "group_id": "1102579807",
                    "config": {
                        "group_id": "1102579807",
                        "features": {"power_status": "on"},
                    },
                    "auth": {
                        "state": "active",
                        "expire": -1,
                        "card_fingerprint": "f" * 64,
                    },
                }],
                ["1102579807"],
                authoritative_group_ids=True,
            )
            self.assertIsNotNone(db.scalar(select(GroupConfigSnapshot)))

            remote_store.apply_bot_exchange(
                db,
                "desktop",
                [],
                [],
                authoritative_group_ids=True,
            )
            db.flush()
            snapshot = db.scalar(select(GroupConfigSnapshot))
            # Keep an ownership-only tombstone for an actionable portal/CMD
            # status, but never leave the former configuration, card, or
            # authorization visible after Windows has authoritatively deleted
            # the local group.
            self.assertIsNotNone(snapshot)
            self.assertEqual(
                snapshot.config_json,
                {"group_id": "1102579807", "features": {}},
            )
            self.assertEqual(snapshot.auth_state, "missing")
            self.assertEqual(snapshot.auth_card_fingerprint, "")
            self.assertEqual(snapshot.sync_status, "unavailable")

    def test_revoked_card_keeps_fingerprint_but_blocks_customer_login(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with patch.dict(os.environ, {"JOKER_WEB_DATA_MODE": "remote"}):
            with Session(engine) as db:
                user = User(username="customer", password_hash=hash_password("password123"))
                db.add(user)
                db.flush()
                access = GroupAccess(
                    user_id=user.id,
                    group_id="1102579807",
                    card_fingerprint="f" * 64,
                    card_mask="JK30-****12345",
                )
                db.add(access)
                remote_store.apply_bot_exchange(
                    db,
                    "desktop",
                    [{
                        "group_id": "1102579807",
                        "config": {"group_id": "1102579807", "features": {}},
                        "auth": {
                            "state": "active",
                            "expire": -1,
                            "card_fingerprint": "f" * 64,
                            "card_mask": "JK30-****12345",
                        },
                    }],
                )
                self.assertTrue(user_has_login_entitlement(db, user))

                remote_store.apply_bot_exchange(
                    db,
                    "desktop",
                    [{
                        "group_id": "1102579807",
                        "config": {"group_id": "1102579807", "features": {}},
                        "auth": {"state": "missing", "expire": 0},
                    }],
                )
                snapshot = db.scalar(select(GroupConfigSnapshot))
                self.assertEqual(snapshot.auth_state, "missing")
                self.assertEqual(snapshot.auth_card_fingerprint, "f" * 64)
                self.assertFalse(user_has_login_entitlement(db, user))

    def test_local_expired_or_missing_card_blocks_customer_login(self):
        """Local authorization history must not keep a customer signed in."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        now = __import__("time").time()
        with patch.dict(os.environ, {"JOKER_WEB_DATA_MODE": "local"}, clear=False):
            with Session(engine) as db:
                user = User(
                    username="local_customer",
                    password_hash=hash_password("password123"),
                )
                db.add(user)
                db.flush()
                db.add(
                    GroupAccess(
                        user_id=user.id,
                        group_id="1102579807",
                        card_fingerprint="a" * 64,
                        card_mask="JK30-****12345",
                    )
                )
                db.flush()
                manager = MagicMock()
                manager.list_all_authorizations.return_value = {
                    "1102579807": {"expire": int(now) - 1}
                }
                with patch("web_client.services.auth_manager", return_value=manager):
                    self.assertFalse(user_has_login_entitlement(db, user))

                manager.list_all_authorizations.return_value = {}
                with patch("web_client.services.auth_manager", return_value=manager):
                    self.assertFalse(user_has_login_entitlement(db, user))

                manager.list_all_authorizations.return_value = {
                    "1102579807": {"expire": -1}
                }
                with patch("web_client.services.auth_manager", return_value=manager):
                    self.assertTrue(user_has_login_entitlement(db, user))

    def test_customer_without_claimed_group_can_sign_in_to_claim_first_card(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            user = User(username="new_customer", password_hash=hash_password("password123"))
            db.add(user)
            db.flush()
            self.assertTrue(user_has_login_entitlement(db, user))

    def test_console_events_are_strictly_scoped_to_the_selected_group(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            # Event ingestion is owned by the active Windows client.  Seed
            # both groups for this client so the test exercises query scoping
            # rather than the ownership rejection path.
            remote_store.apply_bot_exchange(
                db,
                "desktop",
                [
                    {
                        "group_id": "1102579807",
                        "config": {"group_id": "1102579807", "features": {}},
                        "auth": {"state": "active", "expire": -1},
                    },
                    {
                        "group_id": "568586484",
                        "config": {"group_id": "568586484", "features": {}},
                        "auth": {"state": "active", "expire": -1},
                    },
                ],
                ["1102579807", "568586484"],
            )
            accepted = remote_store.ingest_console_events(
                db,
                "desktop",
                [
                    {
                        "event_id": "console-group-a-0001",
                        "group_id": "1102579807",
                        "action": "send",
                        "timestamp": 1_800_000_000,
                    },
                    {
                        "event_id": "console-group-b-0001",
                        "group_id": "568586484",
                        "action": "send",
                        "timestamp": 1_800_000_001,
                    },
                ],
            )
            self.assertEqual(accepted, 2)

            group_a_events = remote_store.list_console_events(db, "1102579807")
            self.assertEqual(
                [event.group_id for event in group_a_events], ["1102579807"]
            )
            self.assertEqual(remote_store.list_console_events(db, "not-a-group"), [])

    def test_console_message_content_requires_explicit_tracking_opt_in(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            remote_store.apply_bot_exchange(
                db,
                "desktop",
                [{
                    "group_id": "1102579807",
                    "config": {"group_id": "1102579807", "features": {}},
                    "auth": {"state": "active", "expire": -1},
                }],
                ["1102579807"],
            )
            raw = {
                "event_id": "console-message-default-0001",
                "group_id": "1102579807",
                "action": "接收",
                "direction": "inbound",
                "message_content": "只应在开启后保存",
            }
            self.assertEqual(
                remote_store.ingest_console_events(db, "desktop", [raw]), 1
            )
            event = db.scalar(select(PortalConsoleEvent))
            self.assertEqual(event.message_content, "")
            self.assertEqual(event.direction, "inbound")

            enabled = dict(raw, event_id="console-message-enabled-0002")
            self.assertEqual(
                remote_store.ingest_console_events(
                    db, "desktop", [enabled], message_content_enabled=True
                ),
                1,
            )
            events = remote_store.list_console_events(db, "1102579807", "开启后")
            self.assertEqual([item.event_id for item in events], [enabled["event_id"]])
            self.assertEqual(events[0].message_content, "只应在开启后保存")


class GlobalConfigTests(unittest.TestCase):
    def test_global_updates_preserve_sensitive_and_unrelated_values(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "astrbot_plugin_joker_config.json"
            original = {
                "auth": {"auth_key": "must-stay-secret"},
                "basic": {"owner_ids": ["2650362143"]},
                "qqadmin": {
                    "enable_group_manage": True,
                    "audit": {
                        "enable_audit": False,
                        "modify_permission": "owner",
                    },
                },
            }
            config_path.write_text(
                json.dumps(original, ensure_ascii=False), encoding="utf-8"
            )

            with patch(
                "web_client.global_config.get_global_config_path",
                return_value=config_path,
            ):
                audit_values = get_global_feature_values("audit")
                audit_values["enabled"] = True
                audit_values["max_attempts"] = 8
                update_global_feature_values("audit", audit_values)

                general = get_general_values()
                general["enable_group_manage"] = False
                update_general_values(general)
                updated = load_global_config()

            self.assertEqual(updated["auth"]["auth_key"], "must-stay-secret")
            self.assertEqual(updated["basic"]["owner_ids"], ["2650362143"])
            self.assertEqual(
                updated["qqadmin"]["audit"]["modify_permission"], "owner"
            )
            self.assertTrue(updated["qqadmin"]["audit"]["enable_audit"])
            self.assertEqual(updated["qqadmin"]["audit"]["max_verify_attempts"], 8)
            self.assertFalse(updated["qqadmin"]["enable_group_manage"])

    def test_admin_can_generate_list_and_delete_unused_license(self):
        fake_auth = FakeAuthManager("JK30-1234567890123ABCDEF123456")
        with (
            patch("web_client.services.auth_manager", return_value=fake_auth),
            patch(
                "web_client.global_config.load_global_config",
                return_value={"auth": {"auth_key": "server-secret"}},
            ),
        ):
            generated = generate_admin_license_keys(30, 2)
            overview = get_admin_license_overview()
            delete_unused_license_key(generated[0])

        self.assertEqual(len(generated), 2)
        self.assertEqual(overview["unused"], generated)
        self.assertNotIn(generated[0], fake_auth.unused)
        self.assertIn(generated[1], fake_auth.unused)


class IntegrationSettingsTests(unittest.TestCase):
    @staticmethod
    def _settings():
        return Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="integration-session-secret-32-characters-minimum",
            card_pepper="integration-card-pepper-32-characters-minimum",
            secure_cookies=False,
            portal_name="Test",
            session_max_age=3600,
        )

    @staticmethod
    def _form(**overrides):
        form = {
            "ftp_enabled": "on",
            "ftp_host": "ftp.example.com",
            "ftp_port": "21",
            "ftp_username": "ftp-user",
            "ftp_password": "ftp-secret",
            "ftp_remote_path": "/incoming",
            "ftp_tls": "off",
            "ftp_passive": "on",
            "database_enabled": "on",
            "database_engine": "mysql",
            "database_host": "db.example.com",
            "database_port": "3306",
            "database_name": "joker_business",
            "database_username": "db-user",
            "database_password": "db-secret",
            "database_charset": "utf8mb4",
        }
        form.update(overrides)
        return form

    def test_external_passwords_are_encrypted_and_blank_updates_preserve_them(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = self._settings()
        with Session(engine) as db:
            view = save_integration_config(db, settings, self._form(), operator_id=7)
            db.commit()
            payload = db.get(PortalSetting, "external_integrations").value_json
            ftp_ciphertext = payload["ftp"]["password_ciphertext"]
            database_ciphertext = payload["database"]["password_ciphertext"]
            self.assertTrue(view["ftp"]["password_configured"])
            self.assertTrue(view["database"]["password_configured"])
            self.assertNotIn("ftp-secret", ftp_ciphertext)
            self.assertNotIn("db-secret", database_ciphertext)
            self.assertNotIn("ftp-secret", repr(view))
            self.assertNotIn("db-secret", repr(view))

            save_integration_config(
                db,
                settings,
                self._form(ftp_password="", database_password=""),
                operator_id=7,
            )
            db.commit()
            saved_again = db.get(PortalSetting, "external_integrations").value_json
            self.assertEqual(saved_again["ftp"]["password_ciphertext"], ftp_ciphertext)
            self.assertEqual(
                saved_again["database"]["password_ciphertext"], database_ciphertext
            )

    def test_user_owned_integrations_are_isolated(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = self._settings()
        with Session(engine) as db:
            save_integration_config(
                db,
                settings,
                self._form(ftp_host="customer-one.example.com"),
                operator_id=7,
                owner_id=7,
            )
            save_integration_config(
                db,
                settings,
                self._form(ftp_host="customer-two.example.com"),
                operator_id=8,
                owner_id=8,
            )
            db.commit()

            self.assertEqual(
                get_integration_config_view(db, 7)["ftp"]["host"],
                "customer-one.example.com",
            )
            self.assertEqual(
                get_integration_config_view(db, 8)["ftp"]["host"],
                "customer-two.example.com",
            )
            self.assertIsNotNone(
                db.get(PortalSetting, "external_integrations:user:7")
            )
            self.assertIsNotNone(
                db.get(PortalSetting, "external_integrations:user:8")
            )
            self.assertIsNone(db.get(PortalSetting, "external_integrations"))

    def test_ftp_only_save_preserves_selected_database(self):
        """Editing FTP must not disable or reinterpret the database selection."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = self._settings()
        with Session(engine) as db:
            save_integration_config(
                db,
                settings,
                self._form(
                    ftp_remote_path="/data",
                    database_engine="sqlite",
                    database_sqlite_path="/data/beijixiong.db",
                    database_host="",
                    database_username="",
                    database_password="",
                ),
                operator_id=7,
                owner_id=7,
            )
            db.commit()
            save_integration_config(
                db,
                settings,
                {
                    "ftp_enabled": "on",
                    "ftp_host": "new-ftp.example.com",
                    "ftp_port": "21",
                    "ftp_username": "ftp-user",
                    "ftp_password": "",
                    "ftp_remote_path": "/data",
                    "ftp_tls": "off",
                    "ftp_passive": "on",
                },
                operator_id=7,
                owner_id=7,
            )
            db.commit()
            view = get_integration_config_view(db, 7)
            self.assertEqual(view["ftp"]["host"], "new-ftp.example.com")
            self.assertEqual(view["database"]["engine"], "sqlite")
            self.assertEqual(view["database"]["sqlite_path"], "/data/beijixiong.db")
            self.assertTrue(view["database"]["enabled"])

    def test_partial_ftp_save_preserves_existing_root_path(self):
        """An omitted root field must not change an account's FTP scope."""

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = self._settings()
        with Session(engine) as db:
            save_integration_config(
                db,
                settings,
                self._form(ftp_remote_path="/SCUM", database_enabled="off"),
                operator_id=7,
                owner_id=7,
            )
            save_integration_config(
                db,
                settings,
                {
                    "ftp_enabled": "on",
                    "ftp_host": "ftp.example.com",
                    "ftp_port": "21",
                    "ftp_username": "ftp-user",
                    "ftp_password": "",
                    "ftp_tls": "off",
                    "ftp_passive": "on",
                },
                operator_id=7,
                owner_id=7,
            )
            self.assertEqual(
                get_integration_config_view(db, 7)["ftp"]["remote_path"], "/SCUM"
            )

    def test_unselected_database_has_no_selection_source(self):
        """A new FTP profile must not show a phantom manual database choice."""

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = self._settings()
        with Session(engine) as db:
            view = save_integration_config(
                db,
                settings,
                {
                    "ftp_enabled": "on",
                    "ftp_host": "ftp.example.com",
                    "ftp_port": "21",
                    "ftp_username": "ftp-user",
                    "ftp_password": "ftp-secret",
                    "ftp_remote_path": "/incoming",
                    "ftp_tls": "off",
                    "ftp_passive": "on",
                },
                operator_id=7,
                owner_id=7,
            )
            self.assertFalse(view["database"]["enabled"])
            self.assertEqual(view["database"]["sqlite_path"], "")
            self.assertEqual(view["database"]["selection_source"], "")

    def test_external_connection_fields_reject_bad_hosts_ports_and_database_names(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = self._settings()
        with Session(engine) as db:
            with self.assertRaises(PortalError):
                save_integration_config(
                    db, settings, self._form(ftp_host="bad host"), operator_id=7
                )
            with self.assertRaises(PortalError):
                save_integration_config(
                    db, settings, self._form(database_port="70000"), operator_id=7
                )
            with self.assertRaises(PortalError):
                save_integration_config(
                    db, settings, self._form(database_name="business;drop"), operator_id=7
                )

    def test_disabled_connection_test_fails_without_network_access(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = self._settings()
        with Session(engine) as db:
            save_integration_config(
                db,
                settings,
                self._form(ftp_enabled="off", database_enabled="off"),
                operator_id=7,
            )
            db.commit()
            with self.assertRaises(PortalError):
                test_integration_connection(db, settings, "ftp")

    def test_database_engine_uses_null_pool_and_safe_view(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = self._settings()
        with Session(engine) as db:
            save_integration_config(db, settings, self._form(), operator_id=7)
            db.commit()
            view = get_integration_config_view(db)
            self.assertNotIn("password", view["ftp"])
            self.assertNotIn("password", view["database"])
            with patch("web_client.integration_service.create_engine") as factory:
                create_business_database_engine(db, settings)
                self.assertIs(factory.call_args.kwargs["poolclass"], NullPool)

    def test_sqlitestudio_selection_clears_legacy_mysql_credentials(self):
        """Selecting the FTP SQLite file must remove the old direct DB secret."""

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = self._settings()
        with Session(engine) as db:
            save_integration_config(db, settings, self._form(), operator_id=7)
            db.commit()

            view = save_integration_config(
                db,
                settings,
                self._form(
                    ftp_remote_path="/data",
                    database_engine="SQLiteStudio",
                    database_sqlite_path="/data/beijixiong.db",
                    database_host="",
                    database_name="",
                    database_username="",
                    database_password="",
                ),
                operator_id=7,
            )
            db.commit()
            payload = db.get(PortalSetting, "external_integrations").value_json

            self.assertEqual(view["database"]["engine"], "sqlite")
            self.assertEqual(view["database"]["sqlite_path"], "/data/beijixiong.db")
            self.assertFalse(view["database"]["password_configured"])
            self.assertEqual(payload["database"]["password_ciphertext"], "")
            self.assertEqual(payload["database"]["host"], "")
            self.assertEqual(payload["database"]["username"], "")

    def test_ftp_database_selection_is_persisted_per_user(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = self._settings()
        with Session(engine) as db:
            save_integration_config(
                db,
                settings,
                self._form(database_enabled="off"),
                operator_id=7,
                owner_id=7,
            )
            save_integration_config(
                db,
                settings,
                self._form(database_enabled="off"),
                operator_id=8,
                owner_id=8,
            )
            save_ftp_database_selection(
                db, 7, "/incoming/beijixiong.db", operator_id=7, source="manual"
            )
            db.commit()

            owner_one = get_integration_config_view(db, 7)
            owner_two = get_integration_config_view(db, 8)
            self.assertEqual(owner_one["database"]["sqlite_path"], "/incoming/beijixiong.db")
            self.assertTrue(owner_one["database"]["enabled"])
            self.assertEqual(owner_one["database"]["selection_source"], "ftp")
            self.assertEqual(owner_two["database"]["sqlite_path"], "")
            self.assertFalse(owner_two["database"]["enabled"])

    def test_virtual_ftp_root_is_preserved_for_manual_database_selection(self):
        """`/BjxRobot/beijixiong.db` must not rewrite a chrooted FTP root."""

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = self._settings()
        with Session(engine) as db:
            save_integration_config(
                db,
                settings,
                self._form(ftp_remote_path="/", database_enabled="off"),
                operator_id=7,
                owner_id=7,
            )
            selected = save_ftp_database_selection(
                db,
                7,
                "/BjxRobot/beijixiong.db",
                operator_id=7,
                source="manual",
            )
            payload = db.get(
                PortalSetting, "external_integrations:user:7"
            ).value_json

            self.assertEqual(selected["ftp"]["remote_path"], "/")
            self.assertEqual(payload["ftp"]["remote_path"], "/")
            self.assertEqual(
                selected["database"]["sqlite_path"], "/BjxRobot/beijixiong.db"
            )

    def test_blank_legacy_ftp_root_is_canonicalized_to_virtual_root(self):
        """Legacy blank roots retain the FTP login root during selection."""

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(
                PortalSetting(
                    key="external_integrations:user:7",
                    value_json={
                        "ftp": {"enabled": True, "remote_path": ""},
                        "database": {},
                    },
                )
            )
            db.flush()

            self.assertEqual(
                get_integration_config_view(db, 7)["ftp"]["remote_path"], "/"
            )
            save_ftp_database_selection(
                db,
                7,
                "/BjxRobot/beijixiong.db",
                operator_id=7,
                source="manual",
            )
            self.assertEqual(
                db.get(
                    PortalSetting, "external_integrations:user:7"
                ).value_json["ftp"]["remote_path"],
                "/",
            )

    def test_ftp_database_paths_reject_traversal_and_non_sqlite_files(self):
        self.assertEqual(_normalize_remote_path("/data/./db"), "/data/db")
        with self.assertRaises(PortalError):
            _normalize_remote_path("/data/../secrets")
        with self.assertRaises(PortalError):
            _path_within_ftp_root("/data", "/secrets/beijixiong.db")
        self.assertEqual(
            _path_within_ftp_root("/data", "/data/sub/beijixiong.db"),
            "/data/sub/beijixiong.db",
        )
        with self.assertRaises(PortalError):
            _normalize_database_file_path("/data/beijixiong.sql")
        self.assertEqual(
            _normalize_database_file_path("data/BEIJIXIONG.DB"),
            "/data/BEIJIXIONG.DB",
        )

    def test_parent_directory_scope_allows_a_sibling_sqlite_file_only_when_enabled(self):
        root = "/SCUM"
        database_path = "/BjxRobot/beijixiong.db"

        with self.assertRaises(PortalError):
            _path_within_ftp_root(root, database_path, "数据库文件路径")
        self.assertEqual(
            _path_within_ftp_root(
                root,
                "/BjxRobot",
                "FTP 目录",
                allow_parent_directory=True,
                parent_path_depth=1,
            ),
            "/BjxRobot",
        )
        self.assertEqual(
            _path_within_ftp_root(
                root,
                database_path,
                "数据库文件路径",
                allow_parent_directory=True,
                parent_path_depth=2,
            ),
            database_path,
        )
        with self.assertRaises(PortalError):
            _path_within_ftp_root(
                root,
                "/BjxRobot/nested/beijixiong.db",
                "数据库文件路径",
                allow_parent_directory=True,
                parent_path_depth=2,
            )
        with self.assertRaises(PortalError):
            _path_within_ftp_root(
                root,
                "/BjxRobot/nested",
                "FTP 目录",
                allow_parent_directory=True,
                parent_path_depth=1,
            )

    def test_parent_directory_switch_is_saved_per_user(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = self._settings()
        with Session(engine) as db:
            view = save_integration_config(
                db,
                settings,
                self._form(
                    ftp_remote_path="/SCUM",
                    ftp_allow_parent="on",
                    database_enabled="off",
                ),
                operator_id=7,
                owner_id=7,
            )
            db.commit()
            payload = db.get(
                PortalSetting, "external_integrations:user:7"
            ).value_json
            self.assertTrue(view["ftp"]["allow_parent_directory"])
            self.assertTrue(payload["ftp"]["allow_parent_directory"])


class TemplateTests(unittest.TestCase):
    def test_installed_release_marker_is_bounded_and_reported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker = Path(temporary_directory) / ".joker-web-release.json"
            marker.write_text(
                json.dumps(
                    {
                        "release_version": "1.10.64",
                        "archive_filename": "Joker_Baota_Remote_v1.10.64-all-in-one.zip",
                        "installed_at": "2026-08-18 12:31:38 UTC",
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"JOKER_WEB_RELEASE_INFO_FILE": str(marker)},
                clear=False,
            ):
                info = get_installed_release_info()
            self.assertTrue(info["known"])
            self.assertEqual(info["version"], "1.10.64")
            self.assertEqual(
                info["package"], "Joker_Baota_Remote_v1.10.64-all-in-one.zip"
            )
            self.assertEqual(info["installed_at"], "2026-08-18 20:31:38 CST")

    def test_missing_installed_release_marker_does_not_guess_a_version(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker = Path(temporary_directory) / ".missing-release.json"
            with patch.dict(
                os.environ,
                {"JOKER_WEB_RELEASE_INFO_FILE": str(marker)},
                clear=False,
            ):
                info = get_installed_release_info()
            self.assertFalse(info["known"])
            self.assertEqual(info["version"], "未记录")

    def test_static_assets_use_a_content_version(self):
        template_dir = Path(__file__).parent / "web_client" / "templates"
        for name in ("base.html", "user_base.html", "admin_base.html"):
            with self.subTest(template=name):
                content = (template_dir / name).read_text(encoding="utf-8")
                self.assertIn("static_asset_version", content)
                self.assertNotIn("20260817-", content)

        application = (Path(__file__).parent / "web_client" / "app.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def _static_asset_version()", application)
        self.assertIn('templates.env.globals["static_asset_version"]', application)

    def test_all_templates_parse(self):
        template_dir = Path(__file__).parent / "web_client" / "templates"
        environment = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(("html",)),
        )
        names = sorted(path.name for path in template_dir.glob("*.html"))
        self.assertGreaterEqual(len(names), 6)
        for name in names:
            with self.subTest(template=name):
                environment.get_template(name)


if __name__ == "__main__":
    unittest.main()
