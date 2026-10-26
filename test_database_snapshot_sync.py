from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from web_client.database import Base
from web_client.integration_service import save_database_snapshot
from web_client.integration_service import save_integration_config
from web_client.models import PortalDatabaseSnapshot, PortalSetting, User
from web_client.feature_schema import normalize_database_profile_descriptor
from web_client.settings import Settings


def _load_web_sync_module():
    """Load web_sync without requiring a running AstrBot installation."""

    root_name = "_joker_database_snapshot_test_root"
    package_name = f"{root_name}.qqadmin"
    module_name = f"{package_name}.web_sync"
    root_package = types.ModuleType(root_name)
    root_package.__path__ = []
    package = types.ModuleType(package_name)
    package.__path__ = []
    group_config = types.ModuleType(f"{package_name}.group_config")
    group_config.load_group_config = lambda _group_id: {}
    group_config.load_group_configs = lambda: {}
    group_config.load_group_configs_with_health = lambda: ({}, True)
    group_config.save_group_config = lambda _group_id, _config: True
    console_log = types.ModuleType(f"{package_name}.console_log")
    console_log.get_console_events = lambda limit=100: []
    auth_package = types.ModuleType(f"{root_name}.auth")
    auth_package.__path__ = []
    auth_manager = types.ModuleType(f"{root_name}.auth.manager")
    auth_manager.list_all_authorizations = lambda: {}
    auth_manager.list_all_authorizations_with_health = lambda: ({}, True)
    auth_manager.get_group_auth_info = lambda _group_id: {}
    auth_manager.get_group_auth_state = lambda _group_id: "active"
    root_package.qqadmin = package
    root_package.auth = auth_package
    package.group_config = group_config
    package.console_log = console_log
    auth_package.manager = auth_manager
    astrbot = types.ModuleType("astrbot")
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.logger = logging.getLogger("test_database_snapshot_sync")
    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = object
    httpx.Timeout = object
    httpx.TransportError = type("TransportError", (Exception,), {})
    modules = {
        root_name: root_package,
        package_name: package,
        f"{package_name}.group_config": group_config,
        f"{package_name}.console_log": console_log,
        f"{root_name}.auth": auth_package,
        f"{root_name}.auth.manager": auth_manager,
        "astrbot": astrbot,
        "astrbot.api": astrbot_api,
        "httpx": httpx,
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(module_name, Path(__file__).parent / "qqadmin" / "web_sync.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module


class DatabaseSnapshotSyncTests(unittest.TestCase):
    def test_snapshot_http_status_is_reported_as_a_safe_actionable_code(self):
        module = _load_web_sync_module()

        class FakeResponse:
            status_code = 404

        class FakeHTTPStatusError(Exception):
            response = FakeResponse()

        summary = module.PortalSyncClient._failure_summary(
            "database_snapshot_apply_failed", FakeHTTPStatusError()
        )
        self.assertEqual(
            "database_snapshot_apply_failed:database_snapshot_endpoint_not_found",
            summary,
        )
        self.assertIn(
            "joker-webctl --apply-nginx",
            module.PortalSyncClient.sync_error_message(
                "database_snapshot_endpoint_not_found"
            ),
        )

    def test_fetch_reports_safe_snapshot_access_state(self):
        module = _load_web_sync_module()

        class FakeResponse:
            status_code = 409

            def json(self):
                return {"code": "database_snapshot_missing"}

            def raise_for_status(self):
                raise AssertionError("known portal state must be parsed first")

        class FakeStream:
            async def __aenter__(self):
                return FakeResponse()

            async def __aexit__(self, *_args):
                return False

        class FakeClient:
            def stream(self, *_args, **_kwargs):
                return FakeStream()

        client = module.PortalSyncClient(
            "https://portal.example.com",
            "t" * 32,
            card_pepper="p" * 32,
            client_id="test-client",
        )
        client._get_http_client = lambda *_args, **_kwargs: FakeClient()
        module.httpx.Timeout = lambda *_args, **_kwargs: object()

        with self.assertRaises(module._PortalSyncProtocolError) as captured:
            asyncio.run(client.fetch_database_snapshot("1102579807", "main"))

        self.assertEqual("database_snapshot_missing", captured.exception.code)

    def test_fetch_reads_streaming_409_body_before_parsing_code(self):
        module = _load_web_sync_module()

        class FakeResponse:
            status_code = 409
            _read = False

            async def aread(self):
                self._read = True
                return b'{"code":"game_card_heartbeat_inactive"}'

            def json(self):
                if not self._read:
                    raise RuntimeError("ResponseNotRead")
                return {"code": "game_card_heartbeat_inactive"}

            def raise_for_status(self):
                raise AssertionError("the streamed error code must be parsed first")

        class FakeStream:
            async def __aenter__(self):
                return FakeResponse()

            async def __aexit__(self, *_args):
                return False

        class FakeClient:
            def stream(self, *_args, **_kwargs):
                return FakeStream()

        client = module.PortalSyncClient(
            "https://portal.example.com",
            "t" * 32,
            card_pepper="p" * 32,
            client_id="test-client",
        )
        client._get_http_client = lambda *_args, **_kwargs: FakeClient()
        module.httpx.Timeout = lambda *_args, **_kwargs: object()

        with self.assertRaises(module._PortalSyncProtocolError) as captured:
            asyncio.run(client.fetch_database_snapshot("1102579807", "main"))

        self.assertEqual("game_card_heartbeat_inactive", captured.exception.code)

    def test_fetch_uses_explicit_code_when_portal_returns_unknown_safe_409(self):
        module = _load_web_sync_module()

        class FakeResponse:
            status_code = 409

            async def aread(self):
                return b'{"code":"database_ftp_not_configured"}'

        class FakeStream:
            async def __aenter__(self):
                return FakeResponse()

            async def __aexit__(self, *_args):
                return False

        class FakeClient:
            def stream(self, *_args, **_kwargs):
                return FakeStream()

        client = module.PortalSyncClient(
            "https://portal.example.com",
            "t" * 32,
            card_pepper="p" * 32,
            client_id="test-client",
        )
        client._get_http_client = lambda *_args, **_kwargs: FakeClient()
        module.httpx.Timeout = lambda *_args, **_kwargs: object()

        with self.assertRaises(module._PortalSyncProtocolError) as captured:
            asyncio.run(client.fetch_database_snapshot("1102579807", "main"))

        self.assertEqual("database_ftp_not_configured", captured.exception.code)

    def test_upload_sends_new_body_hash_and_old_base_hash(self):
        module = _load_web_sync_module()
        old_hash = "a" * 64
        data = b"SQLite format 3\x00snapshot-after-reward"
        new_hash = hashlib.sha256(data).hexdigest()

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "success": True,
                    "revision": 2,
                    "sha256": new_hash,
                }

        class FakeClient:
            def __init__(self):
                self.request = None

            async def post(self, url, *, headers, content):
                self.request = (url, headers, content)
                return FakeResponse()

        root = Path(__file__).resolve().parent
        with tempfile.NamedTemporaryFile(
            prefix="database-upload-", suffix=".db", dir=root, delete=False
        ) as handle:
            local_path = Path(handle.name)
            handle.write(data)
        try:
            relative_path = str(local_path.relative_to(root)).replace("\\", "/")
            client = module.PortalSyncClient(
                "https://portal.example.com",
                "t" * 32,
                card_pepper="p" * 32,
                client_id="test-client",
            )
            fake_client = FakeClient()
            client._get_http_client = lambda *_args, **_kwargs: fake_client
            module.httpx.Timeout = lambda *_args, **_kwargs: object()

            result = asyncio.run(
                client.upload_database_snapshot(
                    "1102579807",
                    "main",
                    relative_path,
                    1,
                    old_hash,
                )
            )

            self.assertTrue(result["success"])
            self.assertEqual(fake_client.request[2], data)
            headers = fake_client.request[1]
            self.assertEqual(headers["X-Database-Snapshot-Revision"], "1")
            self.assertEqual(headers["X-Database-Snapshot-SHA256"], new_hash)
            self.assertEqual(headers["X-Database-Snapshot-Base-SHA256"], old_hash)
            self.assertNotIn("1102579807:main", client._database_snapshot_upload_pending)
        finally:
            local_path.unlink(missing_ok=True)

    def test_descriptor_keeps_valid_snapshot_metadata_and_rejects_invalid(self):
        digest = "a" * 64
        descriptor = normalize_database_profile_descriptor(
            {
                "id": "main",
                "path": "sql/main.db",
                "snapshot_revision": 3,
                "snapshot_sha256": digest,
            }
        )
        self.assertEqual(3, descriptor["snapshot_revision"])
        self.assertEqual(digest, descriptor["snapshot_sha256"])
        with self.assertRaises(ValueError):
            normalize_database_profile_descriptor(
                {
                    "id": "main",
                    "path": "sql/main.db",
                    "snapshot_revision": -1,
                }
            )

    def test_snapshot_revision_is_idempotent_and_published(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        data = b"SQLite format 3\x00" + b"snapshot-v1"
        with Session(engine) as db:
            user = User(username="snapshot-user", password_hash="hash")
            db.add(user)
            db.flush()
            db.add(
                PortalSetting(
                    key=f"external_integrations:user:{user.id}",
                    value_json={
                        "profiles": {
                            "1102579807": {
                                "main": {
                                    "ftp": {},
                                    "database": {"enabled": True, "engine": "sqlite"},
                                    "profile_enabled": True,
                                    "windows_database_path": "sql/main.db",
                                }
                            }
                        },
                        "active_profiles": {"1102579807": "main"},
                    },
                )
            )
            db.flush()
            first = save_database_snapshot(db, user.id, "1102579807", "main", data)
            db.commit()
            self.assertEqual(1, first["revision"])
            second = save_database_snapshot(db, user.id, "1102579807", "main", data)
            self.assertEqual(1, second["revision"])
            changed = save_database_snapshot(
                db, user.id, "1102579807", "main", data + b"-changed"
            )
            self.assertEqual(2, changed["revision"])
            row = db.scalar(
                select(PortalDatabaseSnapshot).where(
                    PortalDatabaseSnapshot.owner_user_id == user.id,
                    PortalDatabaseSnapshot.group_id == "1102579807",
                    PortalDatabaseSnapshot.profile_id == "main",
                )
            )
            self.assertIsNotNone(row)
            self.assertEqual(2, row.revision)
            payload = db.get(PortalSetting, f"external_integrations:user:{user.id}")
            profile = payload.value_json["profiles"]["1102579807"]["main"]
            self.assertEqual(2, profile["snapshot_revision"])
            self.assertEqual(changed["sha256"], profile["snapshot_sha256"])

    def test_materialization_is_atomic_and_rejects_bad_scope(self):
        module = _load_web_sync_module()
        data = b"SQLite format 3\x00" + b"valid"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = module.materialize_database_snapshot(
                "sql/beijixiong.db", data, digest, root=root
            )
            self.assertEqual(data, target.read_bytes())
            with self.assertRaises(module._PortalSyncProtocolError):
                module.materialize_database_snapshot(
                    "sql/beijixiong.db", b"SQLite format 3\x00bad", "0" * 64, root=root
                )
            self.assertEqual(data, target.read_bytes())
            with self.assertRaises(module._PortalSyncProtocolError):
                module.materialize_database_snapshot(
                    "../outside.db", data, digest, root=root
                )

    def test_database_source_change_invalidates_old_snapshot(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="snapshot-session-secret-32-characters-minimum",
            card_pepper="snapshot-card-pepper-32-characters-minimum",
            secure_cookies=False,
            portal_name="Test",
            session_max_age=3600,
        )
        form = {
            "ftp_enabled": "on",
            "ftp_host": "ftp.example.com",
            "ftp_port": "21",
            "ftp_username": "ftp-user",
            "ftp_password": "ftp-pass",
            "ftp_remote_path": "/",
            "ftp_tls": "off",
            "ftp_passive": "on",
            "database_enabled": "on",
            "database_engine": "sqlite",
            "database_sqlite_path": "/main.db",
            "windows_database_path": "sql/main.db",
        }
        with Session(engine) as db:
            save_integration_config(
                db,
                settings,
                form,
                operator_id=1,
                owner_id=1,
                group_id="1102579807",
                profile_id="main",
            )
            db.flush()
            data = b"SQLite format 3\x00snapshot"
            save_database_snapshot(db, 1, "1102579807", "main", data)
            db.commit()
            changed = dict(form)
            changed["database_sqlite_path"] = "/other.db"
            changed["ftp_password"] = ""
            save_integration_config(
                db,
                settings,
                changed,
                operator_id=1,
                owner_id=1,
                group_id="1102579807",
                profile_id="main",
            )
            self.assertIsNone(
                db.scalar(
                    select(PortalDatabaseSnapshot).where(
                        PortalDatabaseSnapshot.owner_user_id == 1,
                        PortalDatabaseSnapshot.group_id == "1102579807",
                        PortalDatabaseSnapshot.profile_id == "main",
                    )
                )
            )


if __name__ == "__main__":
    unittest.main()
