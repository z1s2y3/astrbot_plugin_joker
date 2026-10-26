"""Regression coverage for card fingerprints in the outbound web sync."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import hmac
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parent / "qqadmin" / "web_sync.py"


def _load_web_sync_module():
    """Load the small card-format unit without importing the whole plugin."""
    root_name = "_joker_web_sync_test_root"
    package_name = f"{root_name}.qqadmin"
    module_name = f"{package_name}.web_sync"

    root_package = types.ModuleType(root_name)
    root_package.__path__ = []
    package = types.ModuleType(package_name)
    package.__path__ = []
    group_config = types.ModuleType(f"{package_name}.group_config")
    group_config.get_group_data = lambda *_args, **_kwargs: None
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
    astrbot_api.logger = logging.getLogger("test_web_sync_cards")
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
        spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        modules[module_name] = module
        module._test_modules = modules
        return module


class _FakeResponse:
    def __init__(
        self,
        payload=None,
        error: Exception | None = None,
        status_code: int | None = None,
    ):
        self._payload = {} if payload is None else payload
        self._error = error
        self.status_code = status_code

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def json(self):
        return deepcopy(self._payload)


class _InvalidJsonResponse(_FakeResponse):
    def json(self):
        raise ValueError("not json")


class _FakeAsyncClient:
    responses: list[_FakeResponse | Exception] = []
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, *, headers, json):
        type(self).calls.append({"url": url, "headers": dict(headers), "json": deepcopy(json)})
        response = type(self).responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeTimeout:
    def __init__(self, *args, **kwargs):
        pass


class _FakeStreamResponse:
    def __init__(self, data=b"", *, headers=None, status_code=200, payload=None):
        self._data = data
        self.headers = dict(headers or {})
        self.status_code = status_code
        self._payload = {} if payload is None else deepcopy(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            error = RuntimeError(f"status={self.status_code}")
            error.response = self
            raise error

    def json(self):
        return deepcopy(self._payload)

    async def aiter_bytes(self):
        for start in range(0, len(self._data), 3):
            yield self._data[start:start + 3]


class _FakeStreamClient:
    responses: list[_FakeStreamResponse] = []
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, method, url, *, headers):
        type(self).calls.append(
            {"method": method, "url": url, "headers": dict(headers)}
        )
        return type(self).responses.pop(0)


class _Projection:
    @staticmethod
    def project_group_config(config):
        return deepcopy(config) if isinstance(config, dict) else {}

    @staticmethod
    def merge_group_projection(_current, projected):
        return deepcopy(projected)


class _AuthoritativeProjection:
    calls: list[bool] = []

    @staticmethod
    def project_group_config(config):
        return deepcopy(config) if isinstance(config, dict) else {}

    @classmethod
    def merge_group_projection(cls, _current, projected, *, explicit_empty_deletes=False):
        cls.calls.append(bool(explicit_empty_deletes))
        return deepcopy(projected)


class WebSyncCardTests(unittest.TestCase):
    def test_cmd_sync_action_adds_time_group_and_bounded_detail(self):
        web_sync = _load_web_sync_module()
        console_module = next(
            module
            for name, module in web_sync._test_modules.items()
            if name.endswith(".qqadmin.console_log")
        )
        captured = []
        console_module.print_group_action = (
            lambda action, group_id, *, detail="": captured.append(
                (action, str(group_id), detail)
            )
        )
        with patch.dict(sys.modules, web_sync._test_modules):
            web_sync._print_sync_group_action(
                "网页配置拉取", "1102579807", detail="版本 4；内容：功能:recall"
            )

        self.assertEqual(captured[0][0], "网页配置拉取")
        self.assertEqual(captured[0][1], "1102579807")
        self.assertTrue(captured[0][2].startswith("时间 "))
        self.assertIn("版本 4", captured[0][2])

    def test_group_bound_manual_grant_card_is_sent_as_a_claimable_fingerprint(self):
        """A direct owner grant must reach Linux as a real group card, not MANUAL metadata."""
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        card = "JK30-1234567890123ABCDEF123456"
        auth_module = next(
            module
            for name, module in web_sync._test_modules.items()
            if name.endswith(".auth.manager")
        )
        auth_module.get_group_auth_info = lambda _group_id: {
            "key": card,
            "expire": 1_900_000_000,
            "activated_at": 1_800_000_000,
            "user_id": "2650362143",
        }
        auth_module.get_group_auth_state = lambda _group_id: "active"

        with (
            patch.dict(sys.modules, web_sync._test_modules),
            patch.object(web_sync, "_sync_projection_module", return_value=_Projection),
        ):
            payload = web_sync.build_group_payload(
                group_id,
                {"group_id": group_id, "features": {}},
                "p" * 32,
            )

        self.assertEqual(payload["auth"]["state"], "active")
        self.assertEqual(
            payload["auth"]["card_fingerprint"],
            web_sync._fingerprint_card(card, "p" * 32),
        )
        self.assertNotEqual(payload["auth"]["card_fingerprint"], "")
        self.assertIn("****", payload["auth"]["card_mask"])

    def test_legacy_activated_card_is_fingerprint_eligible(self):
        web_sync = _load_web_sync_module()
        timestamp = "1234567890123"
        signature = "ABCDEF123456"

        self.assertTrue(web_sync._is_activated_card(f"JK30-{timestamp}{signature}"))
        self.assertTrue(web_sync._is_activated_card(f"JK-1-{timestamp}{signature}"))
        # Older local data used the same verified card without the day/timestamp
        # separator.  It must be sent as a fingerprint rather than being treated
        # as an unsynchronized card by the portal.
        self.assertTrue(web_sync._is_activated_card(f"JK30{timestamp}{signature}"))

        self.assertFalse(web_sync._is_activated_card("MANUAL_1102579807_1"))
        self.assertFalse(web_sync._is_activated_card(f"JK30{timestamp}BAD"))

    def test_sync_requires_card_pepper_and_proves_its_value(self):
        web_sync = _load_web_sync_module()
        missing_pepper = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="", client_id="desktop"
        )
        self.assertFalse(missing_pepper.configured)
        self.assertIn("Card Pepper", missing_pepper.configuration_error)

        pepper = "p" * 32
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper=f"  {pepper}  ", client_id="desktop"
        )
        self.assertTrue(client.configured)
        expected = hmac.new(
            pepper.encode("utf-8"),
            b"joker-portal-card-pepper-proof-v1",
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(client.headers["X-Joker-Card-Pepper-Proof"], expected)

    def test_scum_query_uses_isolated_relay_origin(self):
        web_sync = _load_web_sync_module()
        web_sync.httpx.Timeout = _FakeTimeout
        web_sync.httpx.AsyncClient = _FakeAsyncClient
        _FakeAsyncClient.calls = []
        _FakeAsyncClient.responses = [
            _FakeResponse({"success": True, "result": "relay result"})
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            client_id="desktop",
            scum_query_url="https://ba.yooyun.top",
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            result = asyncio.run(client.query_scum("1102579807", "search", "test"))

        self.assertEqual(result["result"], "relay result")
        self.assertEqual(client.base_url, "https://portal.example")
        self.assertEqual(
            _FakeAsyncClient.calls[0]["url"],
            "https://ba.yooyun.top/api/sync/v1/scum/query/1102579807",
        )
        self.assertEqual(
            _FakeAsyncClient.calls[0]["headers"]["Authorization"],
            "Bearer " + "x" * 32,
        )

    def test_scum_query_rejects_unsafe_relay_origin(self):
        web_sync = _load_web_sync_module()
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            scum_query_url="http://127.0.0.1:8766",
        )

        with self.assertRaisesRegex(RuntimeError, "invalid_scum_relay_url"):
            asyncio.run(client.query_scum("1102579807", "search", "test"))

    def test_scum_query_rejects_removed_operations_before_network(self):
        web_sync = _load_web_sync_module()
        _FakeAsyncClient.calls = []
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            client_id="desktop",
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            with self.assertRaisesRegex(RuntimeError, "invalid_scum_query"):
                asyncio.run(client.query_scum("1102579807", "ranking", ""))

        self.assertEqual([], _FakeAsyncClient.calls)

    def test_empty_client_id_uses_the_persistent_windows_identity(self):
        web_sync = _load_web_sync_module()
        persisted_id = "windows-" + "a" * 32
        with patch.object(web_sync, "_persistent_client_id", return_value=persisted_id):
            client = web_sync.PortalSyncClient(
                "https://portal.example", "x" * 32, card_pepper="p" * 32
            )
        self.assertEqual(client.client_id, persisted_id)

    def test_525_and_read_errors_have_actionable_safe_messages(self):
        web_sync = _load_web_sync_module()

        tls_message = web_sync.PortalSyncClient.sync_error_message("http_status_525")
        read_message = web_sync.PortalSyncClient.sync_error_message("read_error")

        self.assertIn("TLS 握手", tls_message)
        self.assertIn("不是卡密", tls_message)
        self.assertIn("读取网页响应", read_message)
        self.assertEqual(
            web_sync.PortalSyncClient.sync_error_message("readerror"), read_message
        )
        self.assertNotIn("https://", tls_message)

    def test_transport_errors_use_canonical_safe_codes(self):
        web_sync = _load_web_sync_module()

        class ReadError(web_sync.httpx.TransportError):
            pass

        class ReadTimeout(web_sync.httpx.TransportError):
            pass

        class ConnectError(web_sync.httpx.TransportError):
            pass

        self.assertEqual(
            web_sync.PortalSyncClient._sync_error_summary(
                ReadError("response interrupted at https://portal.example/?token=must-not-leak")
            ),
            "read_error",
        )
        self.assertEqual(
            web_sync.PortalSyncClient._sync_error_summary(ReadTimeout("slow response")),
            "timeout",
        )
        self.assertEqual(
            web_sync.PortalSyncClient._sync_error_summary(
                ConnectError(
                    "certificate verify failed for https://portal.example/?token=must-not-leak"
                )
            ),
            "tls_handshake_failed",
        )

    def test_unwrapped_tls_handshake_failure_rebuilds_failed_keepalive_client(self):
        """A transient TLS error retries through a fresh client connection."""

        web_sync = _load_web_sync_module()

        class TlsHandshakeFailure(Exception):
            pass

        class CountedAsyncClient(_FakeAsyncClient):
            responses: list[_FakeResponse | Exception] = []
            calls: list[dict] = []
            instances = 0

            def __init__(self, *args, **kwargs):
                type(self).instances += 1

        CountedAsyncClient.responses = [
            TlsHandshakeFailure("TLS handshake connection reset"),
            _FakeResponse({"status": "ok"}),
        ]
        web_sync.httpx.AsyncClient = CountedAsyncClient
        web_sync.httpx.Timeout = _FakeTimeout
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32
        )

        result = asyncio.run(client._post_json("/api/test", {}, _FakeTimeout()))

        self.assertEqual({"status": "ok"}, result)
        self.assertEqual(2, CountedAsyncClient.instances)
        self.assertEqual(2, len(CountedAsyncClient.calls))

    def test_sync_failure_log_is_debounced_and_recovery_is_emitted_once(self):
        web_sync = _load_web_sync_module()
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32
        )

        with (
            patch.object(web_sync.time, "monotonic", side_effect=[100.0, 101.0, 102.0]),
            patch.object(web_sync.logger, "warning") as warning,
            patch.object(web_sync.logger, "debug") as debug,
        ):
            client._log_sync_failure("tls_handshake_failed")
            client._log_sync_failure("tls_handshake_failed")
            client._log_sync_failure("tls_handshake_failed")
            client._note_sync_recovery()
            client._note_sync_recovery()

        self.assertEqual(1, warning.call_count)
        self.assertEqual(1, debug.call_count)
        self.assertEqual(0, client._suppressed_error_count)

    def test_reply_asset_fetch_is_token_authenticated_bounded_and_not_redirected(self):
        web_sync = _load_web_sync_module()
        image = b"\x89PNG\r\n\x1a\nportal-image"
        _FakeStreamClient.responses = [
            _FakeStreamResponse(
                image,
                headers={
                    "content-type": "image/png; charset=binary",
                    "content-length": str(len(image)),
                },
            )
        ]
        _FakeStreamClient.calls = []
        web_sync.httpx.Timeout = _FakeTimeout
        web_sync.httpx.AsyncClient = _FakeStreamClient
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            data, content_type = asyncio.run(
                client.fetch_reply_asset(55, max_image_bytes=1024 * 1024)
            )

        self.assertEqual(data, image)
        self.assertEqual(content_type, "image/png")
        self.assertEqual(len(_FakeStreamClient.calls), 1)
        call = _FakeStreamClient.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(
            call["url"], "https://portal.example/api/sync/v1/reply-assets/55"
        )
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + "x" * 32)
        self.assertIn("X-Joker-Card-Pepper-Proof", call["headers"])

    def test_reply_asset_fetch_rejects_declared_oversize_before_reading_body(self):
        web_sync = _load_web_sync_module()
        _FakeStreamClient.responses = [
            _FakeStreamResponse(
                b"ignored",
                headers={"content-length": str(2 * 1024 * 1024)},
            )
        ]
        _FakeStreamClient.calls = []
        web_sync.httpx.Timeout = _FakeTimeout
        web_sync.httpx.AsyncClient = _FakeStreamClient
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            with self.assertRaisesRegex(RuntimeError, "reply_asset_too_large"):
                asyncio.run(client.fetch_reply_asset(56, max_image_bytes=1024 * 1024))

    def test_sync_queues_portal_reply_cache_after_saving_a_remote_rule(self):
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        image = b"\x89PNG\r\n\x1a\nportal-image"
        digest = hashlib.sha256(image).hexdigest()
        configs = {group_id: {"group_id": group_id, "features": {}}}

        def save_group_config(saved_group_id, config):
            configs[saved_group_id] = deepcopy(config)
            return True

        self._configure_sync_fakes(web_sync, configs, save_group_config)
        remote_rule = {
            "group_id": group_id,
            "features": {"reply": {"reply_image_retry_interval": 30}},
            "replies": {
                "exact": {
                    "图片": {
                        "reply": "ok",
                        "image_portal_assets": [{"id": 57, "sha256": digest}],
                    }
                },
                "fuzzy": [],
                "fuzzy_keywords": {},
                "regex": [],
            },
        }
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": [{"group_id": group_id, "revision": 2, "config": remote_rule}]}),
            _FakeResponse({"applied": 1}),
        ]
        cache_calls = []
        package_name = next(
            name for name in web_sync._test_modules if name.endswith(".qqadmin")
        )
        fake_reply = types.ModuleType(f"{package_name}.reply")

        async def cache_portal_reply_assets(cache_group_id, fetch_asset, **kwargs):
            cache_calls.append((cache_group_id, kwargs))
            data, content_type = await fetch_asset(57)
            self.assertEqual((data, content_type), (image, "image/png"))
            return {"rules": 1, "cached": 1, "failed": 0}

        fake_reply.cache_portal_reply_assets = cache_portal_reply_assets
        modules = dict(web_sync._test_modules)
        modules[fake_reply.__name__] = fake_reply
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )

        async def fetch_asset(asset_id, *, max_image_bytes):
            self.assertEqual((asset_id, max_image_bytes), (57, 20 * 1024 * 1024))
            return image, "image/png"

        client.fetch_reply_asset = fetch_asset

        async def run_sync_and_background_cache():
            result = await client.sync_once()
            await asyncio.sleep(0)
            return result

        with patch.dict(sys.modules, modules):
            result = asyncio.run(run_sync_and_background_cache())

        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["reply_asset_cache_queued"], 1)
        self.assertEqual(len(cache_calls), 1)
        self.assertEqual(cache_calls[0][0], group_id)
        self.assertEqual(cache_calls[0][1]["download_concurrency"], 4)

    def test_server_missing_snapshot_forces_a_full_reupload(self):
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}

        def save_group_config(saved_group_id, config):
            configs[saved_group_id] = deepcopy(config)
            return True

        self._configure_sync_fakes(web_sync, configs, save_group_config)
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": []}),
            # Simulate the portal snapshot table being cleared after the
            # first successful upload.  The second request is a heartbeat,
            # so only the reconciliation hint can recover this group.
            _FakeResponse({"changes": [], "missing_group_ids": [group_id]}),
            _FakeResponse({"changes": []}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            first = asyncio.run(client.sync_once())
            second = asyncio.run(client.sync_once())
            third = asyncio.run(client.sync_once())

        self.assertEqual(first["uploaded"], 1)
        self.assertEqual(second["uploaded"], 0)
        self.assertEqual(second["resync_requested"], 1)
        self.assertTrue(client._sync_requested.is_set())
        self.assertEqual(third["uploaded"], 1)
        self.assertEqual(
            [call["json"]["groups"] for call in _FakeAsyncClient.calls],
            [
                [{"group_id": group_id, "config": configs[group_id], "auth": {}}],
                [],
                [{"group_id": group_id, "config": configs[group_id], "auth": {}}],
            ],
        )

    def test_login_refresh_hint_forces_a_full_reupload(self):
        """A Linux login request must survive until Windows uploads again."""

        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}

        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": []}),
            _FakeResponse({"changes": [], "refresh_group_ids": [group_id]}),
            _FakeResponse({"changes": []}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            first = asyncio.run(client.sync_once())
            second = asyncio.run(client.sync_once())
            third = asyncio.run(client.sync_once())

        self.assertEqual(first["uploaded"], 1)
        self.assertEqual(second["uploaded"], 0)
        self.assertEqual(second["resync_requested"], 1)
        self.assertTrue(client._sync_requested.is_set())
        self.assertEqual(third["uploaded"], 1)
        self.assertEqual(
            [call["json"]["groups"] for call in _FakeAsyncClient.calls],
            [
                [{"group_id": group_id, "config": configs[group_id], "auth": {}}],
                [],
                [{"group_id": group_id, "config": configs[group_id], "auth": {}}],
            ],
        )

    def test_unknown_server_resync_hint_is_visible_once_in_cmd(self):
        """A stale server group must not be silently ignored by Windows."""
        web_sync = _load_web_sync_module()
        local_group_id = "1102579807"
        removed_group_id = "568586484"
        configs = {local_group_id: {"group_id": local_group_id, "features": {}}}
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": [], "refresh_group_ids": [removed_group_id]}),
            _FakeResponse({"changes": [], "refresh_group_ids": [removed_group_id]}),
        ]
        actions = []
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )

        with patch.dict(sys.modules, web_sync._test_modules), patch.object(
            web_sync,
            "_print_sync_group_action",
            side_effect=lambda action, group_id, *, detail="": actions.append(
                (action, str(group_id), detail)
            ),
        ):
            first = asyncio.run(client.sync_once())
            second = asyncio.run(client.sync_once())

        self.assertEqual(first["unavailable_resync_groups"], 1)
        self.assertEqual(second["unavailable_resync_groups"], 1)
        unavailable = [item for item in actions if item[0] == "网页补传未完成"]
        self.assertEqual(len(unavailable), 1)
        self.assertEqual(unavailable[0][1], removed_group_id)
        self.assertIn("不在 Windows 本地配置或授权中", unavailable[0][2])

    def test_incomplete_local_sources_keep_a_protected_heartbeat(self):
        """A damaged local source must not publish or consume group sync work."""

        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        group_module = next(
            module
            for name, module in web_sync._test_modules.items()
            if name.endswith(".qqadmin.group_config")
        )
        auth_module = next(
            module
            for name, module in web_sync._test_modules.items()
            if name.endswith(".auth.manager")
        )
        group_module.load_group_configs_with_health = lambda: (deepcopy(configs), False)
        auth_module.list_all_authorizations_with_health = lambda: ({}, True)
        # A stale response must not make the Windows client consume either
        # reconciliation hint while its local source read is incomplete.
        _FakeAsyncClient.responses = [
            _FakeResponse(
                {
                    "changes": [],
                    "missing_group_ids": [group_id],
                    "refresh_group_ids": [group_id],
                }
            ),
            _FakeResponse(
                {
                    "changes": [],
                    "missing_group_ids": [group_id],
                    "refresh_group_ids": [group_id],
                }
            ),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )
        client._last_uploaded[group_id] = {
            "group_id": group_id,
            "config": {"group_id": group_id, "features": {"power_status": "on"}},
            "auth": {"state": "active"},
        }
        client._last_runtime_summaries[group_id] = {"stats": {"group": {"total_users": 1}}}
        actions = []

        with patch.dict(sys.modules, web_sync._test_modules), patch.object(
            web_sync,
            "_print_sync_group_action",
            side_effect=lambda action, action_group_id, *, detail="": actions.append(
                (action, str(action_group_id), detail)
            ),
        ):
            result = asyncio.run(client.sync_once())
            second = asyncio.run(client.sync_once())

        self.assertFalse(result["local_sources_complete"])
        self.assertFalse(_FakeAsyncClient.calls[0]["json"]["authoritative_group_ids"])
        self.assertEqual(_FakeAsyncClient.calls[0]["json"]["known_group_ids"], [])
        self.assertEqual(_FakeAsyncClient.calls[0]["json"]["groups"], [])
        self.assertEqual(_FakeAsyncClient.calls[0]["json"]["runtime_summaries"], {})
        self.assertEqual(_FakeAsyncClient.calls[0]["json"]["apply_failures"], [])
        self.assertEqual(result["uploaded"], 0)
        self.assertEqual(result["resync_requested"], 0)
        self.assertEqual(second["resync_requested"], 0)
        self.assertFalse(client._sync_requested.is_set())
        # Delta caches must survive protection mode, otherwise recovery would
        # accidentally treat the partial inventory as a local deletion.
        self.assertIn(group_id, client._last_uploaded)
        self.assertIn(group_id, client._last_runtime_summaries)
        protected = [item for item in actions if item[0] == "本地数据读取保护"]
        self.assertEqual(len(protected), 1)
        self.assertEqual(protected[0][1], group_id)

    def test_transport_timeout_is_retried_once(self):
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}

        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        _FakeAsyncClient.responses = [
            web_sync.httpx.TransportError("temporary network failure"),
            _FakeResponse({"changes": []}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            result = asyncio.run(client.sync_once())
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(len(_FakeAsyncClient.calls), 2)

    def test_server_error_is_retried_once(self):
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}

        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        _FakeAsyncClient.responses = [
            _FakeResponse({"detail": "temporary upstream error"}, status_code=503),
            _FakeResponse({"changes": []}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            result = asyncio.run(client.sync_once())
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(len(_FakeAsyncClient.calls), 2)

    def test_invalid_json_response_uses_safe_protocol_error(self):
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        _FakeAsyncClient.responses = [_InvalidJsonResponse(status_code=200)]
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            client_id="desktop",
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            with self.assertRaisesRegex(RuntimeError, "invalid_json_response"):
                asyncio.run(client.sync_once())
        self.assertEqual(len(_FakeAsyncClient.calls), 1)
        self.assertIn(
            "无效数据",
            web_sync.PortalSyncClient.sync_error_message("invalid_json_response"),
        )

    def test_redirect_response_is_not_followed_or_retried(self):
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        _FakeAsyncClient.responses = [_FakeResponse({}, status_code=301)]
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            client_id="desktop",
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            with self.assertRaisesRegex(RuntimeError, "unexpected_redirect"):
                asyncio.run(client.sync_once())
        self.assertEqual(len(_FakeAsyncClient.calls), 1)
        self.assertIn(
            "重定向",
            web_sync.PortalSyncClient.sync_error_message("unexpected_redirect"),
        )

    def test_card_pepper_mismatch_is_not_retried(self):
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}

        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        _FakeAsyncClient.responses = [
            _FakeResponse(
                {"code": "card_pepper_mismatch"},
                status_code=409,
            ),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            with self.assertRaisesRegex(RuntimeError, "card_pepper_mismatch"):
                asyncio.run(client.sync_once())
        self.assertEqual(len(_FakeAsyncClient.calls), 1)

    def test_invalid_sync_token_is_not_reported_as_a_timeout_or_retried(self):
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}

        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        _FakeAsyncClient.responses = [
            _FakeResponse({"code": "sync_token_invalid"}, status_code=401),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            with self.assertRaisesRegex(RuntimeError, "sync_token_invalid"):
                asyncio.run(client.sync_once())
        self.assertEqual(len(_FakeAsyncClient.calls), 1)
        self.assertIn(
            "同步令牌",
            web_sync.PortalSyncClient.sync_error_message("sync_token_invalid"),
        )

    def _configure_sync_fakes(self, web_sync, configs, save_group_config):
        web_sync.load_group_configs = lambda: deepcopy(configs)
        group_module = next(
            module
            for name, module in web_sync._test_modules.items()
            if name.endswith(".qqadmin.group_config")
        )
        auth_module = next(
            module
            for name, module in web_sync._test_modules.items()
            if name.endswith(".auth.manager")
        )
        group_module.load_group_configs_with_health = lambda: (deepcopy(configs), True)
        auth_module.list_all_authorizations_with_health = lambda: ({}, True)
        web_sync.load_group_config = lambda group_id: deepcopy(configs.get(group_id, {}))
        web_sync.save_group_config = save_group_config
        web_sync.get_console_events = lambda limit=500: []
        web_sync._sync_projection_module = lambda: _Projection
        def build_payload_stub(group_id, config, _pepper, **options):
            payload = {"group_id": str(group_id)}
            if options.get("include_config", True):
                payload["config"] = deepcopy(config)
            if options.get("include_auth", True):
                payload["auth"] = {}
            return payload

        web_sync.build_group_payload = build_payload_stub
        web_sync.httpx.Timeout = _FakeTimeout
        web_sync.httpx.AsyncClient = _FakeAsyncClient
        _FakeAsyncClient.calls = []
        # Most historic tests exercise the established full-sync behaviour in
        # isolation.  Mark their fake portal as legacy; dedicated scope tests
        # below reset this state to ``None``/``True`` explicitly.
        original_init = web_sync.PortalSyncClient.__init__
        web_sync.PortalSyncClient._original_init_for_tests = original_init

        def legacy_init(client, *args, **kwargs):
            original_init(client, *args, **kwargs)
            client._server_scope_supported = False

        web_sync.PortalSyncClient.__init__ = legacy_init

    def test_runtime_summaries_are_sent_only_when_their_content_changes(self):
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}
        runtime = {group_id: {"stats": {"group": {"total_users": 1}}}}

        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        web_sync.build_runtime_summary = lambda current_group: deepcopy(
            runtime[str(current_group)]
        )
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": []}),
            _FakeResponse({"changes": []}),
            _FakeResponse({"changes": []}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            client_id="desktop",
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            asyncio.run(client.sync_once())
            asyncio.run(client.sync_once())
            runtime[group_id] = {"stats": {"group": {"total_users": 2}}}
            asyncio.run(client.sync_once())

        payloads = [call["json"]["runtime_summaries"] for call in _FakeAsyncClient.calls]
        self.assertEqual(
            payloads[0],
            {group_id: {"stats": {"group": {"total_users": 1}}}},
        )
        self.assertEqual(payloads[1], {})
        self.assertEqual(
            payloads[2],
            {group_id: {"stats": {"group": {"total_users": 2}}}},
        )

    def test_local_reply_deletion_uploads_an_explicit_empty_collection(self):
        """A Windows command deletion must not be hidden by the delta cache."""

        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {
            group_id: {
                "group_id": group_id,
                "features": {},
                "replies": {
                    "exact": {"问题1": {"reply": "答案1"}},
                    "fuzzy": [],
                    "regex": [],
                },
            }
        }

        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": []}),
            _FakeResponse({"changes": []}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            client_id="desktop",
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            asyncio.run(client.sync_once())
            configs[group_id]["replies"]["exact"].pop("问题1")
            asyncio.run(client.sync_once())

        first_payload = _FakeAsyncClient.calls[0]["json"]["groups"][0]["config"]
        second_payload = _FakeAsyncClient.calls[1]["json"]["groups"][0]["config"]
        self.assertIn("问题1", first_payload["replies"]["exact"])
        self.assertEqual(second_payload["replies"]["exact"], {})

    def test_windows_change_wins_over_a_stale_pending_web_revision(self):
        """An old web snapshot must not recreate a locally deleted reply."""

        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {
            group_id: {
                "group_id": group_id,
                "features": {},
                "replies": {
                    "exact": {"问题1": {"reply": "答案1"}},
                    "fuzzy": [],
                    "regex": [],
                },
            }
        }
        saved: list[dict] = []

        def save_group_config(_group_id, config):
            saved.append(deepcopy(config))
            configs[group_id] = deepcopy(config)
            return True

        self._configure_sync_fakes(web_sync, configs, save_group_config)
        stale_web_projection = deepcopy(configs[group_id])
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": []}),
            _FakeResponse(
                {
                    "changes": [
                        {
                            "group_id": group_id,
                            "revision": 7,
                            "source": "web",
                            "config": stale_web_projection,
                        }
                    ]
                }
            ),
            _FakeResponse({"applied": 1}),
            _FakeResponse({"changes": []}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32
        )
        actions: list[tuple[str, str]] = []

        with patch.dict(sys.modules, web_sync._test_modules), patch.object(
            web_sync,
            "_print_sync_group_action",
            side_effect=lambda action, current_group, **_kwargs: actions.append(
                (action, str(current_group))
            ),
        ):
            asyncio.run(client.sync_once())
            configs[group_id]["replies"]["exact"].clear()
            second = asyncio.run(client.sync_once())
            third = asyncio.run(client.sync_once())

        self.assertEqual(1, second["local_conflicts"])
        self.assertEqual([], saved)
        self.assertEqual({}, configs[group_id]["replies"]["exact"])
        self.assertEqual(
            {}, _FakeAsyncClient.calls[1]["json"]["groups"][0]["config"]["replies"]["exact"]
        )
        self.assertEqual(
            {}, _FakeAsyncClient.calls[2]["json"]["groups"][0]["config"]["replies"]["exact"]
        )
        self.assertEqual(0, third["uploaded"])
        self.assertIn(("网页旧配置已跳过", group_id), actions)

    def test_local_delta_is_retried_when_pending_web_ack_times_out(self):
        """An ACK timeout must not hide the local deletion on next heartbeat."""

        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {
            group_id: {
                "group_id": group_id,
                "features": {},
                "replies": {
                    "exact": {"问题1": {"reply": "答案1"}},
                    "fuzzy": [],
                    "regex": [],
                },
            }
        }
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        stale = deepcopy(configs[group_id])
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": []}),
            _FakeResponse(
                {
                    "changes": [
                        {
                            "group_id": group_id,
                            "revision": 8,
                            "source": "web",
                            "config": stale,
                        }
                    ]
                }
            ),
            _FakeResponse(error=RuntimeError("ACK timeout")),
            _FakeResponse(
                {
                    "changes": [
                        {
                            "group_id": group_id,
                            "revision": 8,
                            "source": "web",
                            "config": stale,
                        }
                    ]
                }
            ),
            _FakeResponse({"applied": 1}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            asyncio.run(client.sync_once())
            configs[group_id]["replies"]["exact"].clear()
            failed = asyncio.run(client.sync_once())
            retried = asyncio.run(client.sync_once())

        self.assertEqual(1, failed["failed"])
        self.assertEqual(1, retried["local_conflicts"])
        # Exchange #3 must carry the deletion again; the ACK call is #4.
        self.assertEqual(
            {}, _FakeAsyncClient.calls[3]["json"]["groups"][0]["config"]["replies"]["exact"]
        )

    def test_scoped_windows_reply_deletion_is_not_lost_after_scope_probe(self):
        """The production scoped handshake still carries the empty snapshot."""

        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {
            group_id: {
                "group_id": group_id,
                "features": {},
                "replies": {
                    "exact": {"问题1": {"reply": "答案1"}},
                    "fuzzy": [],
                    "regex": [],
                },
            }
        }
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        # Undo the legacy override installed by _configure_sync_fakes.
        web_sync.PortalSyncClient.__init__ = (
            web_sync.PortalSyncClient._original_init_for_tests
        )
        _FakeAsyncClient.responses = [
            _FakeResponse(
                {
                    "changes": [],
                    "managed_group_ids": [group_id],
                    "claimable_group_ids": [],
                }
            ),
            _FakeResponse({"changes": []}),
            _FakeResponse({"changes": []}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            client_id="desktop",
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            asyncio.run(client.sync_once())
            asyncio.run(client.sync_once())
            configs[group_id]["replies"]["exact"].clear()
            asyncio.run(client.sync_once())

        payloads = [call["json"]["groups"] for call in _FakeAsyncClient.calls]
        self.assertEqual(payloads[0], [])
        self.assertEqual(
            payloads[1][0]["config"]["replies"]["exact"],
            {"问题1": {"reply": "答案1"}},
        )
        self.assertEqual(
            payloads[2][0]["config"]["replies"]["exact"], {}
        )

    def test_windows_blacklist_runtime_deletion_uploads_empty_runtime_summary(self):
        """Removing a blacklist record replaces the read-only portal panel."""

        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}
        runtime = {
            group_id: {
                "blacklist": {
                    "total": 1,
                    "active": 1,
                    "entries": [{"user_id": "12345", "active": True}],
                }
            }
        }
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        web_sync.build_runtime_summary = lambda current_group: deepcopy(
            runtime[str(current_group)]
        )
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": []}),
            _FakeResponse({"changes": []}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, client_id="desktop"
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            asyncio.run(client.sync_once())
            runtime[group_id] = {
                "blacklist": {"total": 0, "active": 0, "entries": []}
            }
            asyncio.run(client.sync_once())

        summaries = [call["json"]["runtime_summaries"] for call in _FakeAsyncClient.calls]
        self.assertEqual(summaries[0][group_id]["blacklist"]["total"], 1)
        self.assertEqual(summaries[1][group_id]["blacklist"]["entries"], [])

    def test_runtime_summary_batch_is_bounded_and_rotates_after_config_upload(self):
        """Many runtime stores must not delay a one-group web sync exchange."""

        web_sync = _load_web_sync_module()
        group_ids = ["1102579807", "1102579808", "1102579809"]
        configs = {
            group_id: {"group_id": group_id, "features": {}}
            for group_id in group_ids
        }
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        runtime_calls: list[str] = []

        def runtime_summary(group_id):
            normalized = str(group_id)
            runtime_calls.append(normalized)
            return {"stats": {"group": {"total_users": int(normalized[-1])}}}

        web_sync.build_runtime_summary = runtime_summary
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": []}),
            _FakeResponse({"changes": []}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            client_id="desktop",
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            asyncio.run(client.sync_once())
            asyncio.run(client.sync_once())

        payloads = [call["json"]["runtime_summaries"] for call in _FakeAsyncClient.calls]
        self.assertEqual(set(payloads[0]), set(group_ids[:2]))
        # The second heartbeat starts after the first priority batch, so the
        # third group is refreshed without a full all-group runtime scan.
        self.assertEqual(set(payloads[1]), {group_ids[2]})
        self.assertEqual(runtime_calls, [group_ids[0], group_ids[1], group_ids[2], group_ids[0]])

    def test_dirty_runtime_group_is_prioritized_before_round_robin(self):
        web_sync = _load_web_sync_module()
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, client_id="desktop"
        )
        group_ids = {"1102579807", "1102579808", "1102579809"}
        client.mark_runtime_dirty("1102579809")
        selected = client._runtime_summary_group_ids(
            group_ids, [], client._runtime_dirty_group_ids
        )
        self.assertEqual(selected[0], "1102579809")

    def test_readable_config_source_fulfills_pull_when_auth_source_is_unavailable(self):
        """A broken auth.json must not leave a readable group in wait state."""
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {
            group_id: {
                "group_id": group_id,
                "features": {"power_status": "on"},
            }
        }
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        auth_module = next(
            module
            for name, module in web_sync._test_modules.items()
            if name.endswith(".auth.manager")
        )
        auth_module.list_all_authorizations_with_health = lambda: ({}, False)
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": [], "refresh_group_ids": [group_id]})
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            client_id="desktop",
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            result = asyncio.run(client.sync_once())

        payload = _FakeAsyncClient.calls[0]["json"]
        self.assertFalse(payload["authoritative_group_ids"])
        self.assertTrue(payload["config_source_complete"])
        self.assertFalse(payload["auth_source_complete"])
        self.assertEqual(payload["known_group_ids"], [group_id])
        self.assertEqual([item["group_id"] for item in payload["groups"]], [group_id])
        self.assertNotIn("auth", payload["groups"][0])
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(result["resync_requested"], 1)

    def test_auth_only_group_is_uploaded_without_a_fake_config_projection(self):
        """Manual authorization remains visible even before a group JSON exists."""
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {}
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        auth_module = next(
            module
            for name, module in web_sync._test_modules.items()
            if name.endswith(".auth.manager")
        )
        auth_module.list_all_authorizations_with_health = lambda: (
            {
                group_id: {
                    "key": "MANUAL_1102579807_1",
                    "expire": -1,
                    "activated_at": 1_800_000_000,
                    "user_id": "owner",
                }
            },
            True,
        )
        _FakeAsyncClient.responses = [_FakeResponse({"changes": []})]
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            client_id="desktop",
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            result = asyncio.run(client.sync_once())

        payload = _FakeAsyncClient.calls[0]["json"]
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(payload["known_group_ids"], [group_id])
        self.assertTrue(payload["authoritative_group_ids"])
        self.assertEqual(len(payload["groups"]), 1)
        self.assertEqual(payload["groups"][0]["group_id"], group_id)
        self.assertNotIn("config", payload["groups"][0])
        self.assertIn("auth", payload["groups"][0])

    def test_readable_uninitialized_empty_config_dir_still_uploads_auth_only_group(self):
        """An empty, readable first-run directory must not strand an auth-only group."""
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {}
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        group_module = next(
            module
            for name, module in web_sync._test_modules.items()
            if name.endswith(".qqadmin.group_config")
        )
        # The source is readable but has no initialization marker yet.  The
        # two-value health result remains non-authoritative; the optional
        # function attribute lets the new client use it for an auth-only
        # projection without allowing deletion of older portal rows.
        group_module.load_group_configs_with_health = lambda: ({}, False)
        group_module.load_group_configs_with_health.last_readable = True
        auth_module = next(
            module
            for name, module in web_sync._test_modules.items()
            if name.endswith(".auth.manager")
        )
        auth_module.list_all_authorizations_with_health = lambda: (
            {group_id: {"key": "MANUAL_1102579807_1", "expire": -1}},
            True,
        )
        _FakeAsyncClient.responses = [_FakeResponse({"changes": []})]
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            client_id="desktop",
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            result = asyncio.run(client.sync_once())

        payload = _FakeAsyncClient.calls[0]["json"]
        self.assertEqual(result["uploaded"], 1)
        self.assertFalse(payload["authoritative_group_ids"])
        self.assertTrue(payload["config_source_complete"])
        self.assertEqual(payload["known_group_ids"], [group_id])
        self.assertNotIn("config", payload["groups"][0])
        self.assertIn("auth", payload["groups"][0])

    def test_auth_only_group_reuploads_after_auth_source_recovers(self):
        """A recovered auth.json must clear the server-side unverified state."""
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        self._configure_sync_fakes(web_sync, {}, lambda *_args: True)
        auth_module = next(
            module
            for name, module in web_sync._test_modules.items()
            if name.endswith(".auth.manager")
        )
        source = {"healthy": True}
        auth_record = {"key": "MANUAL_1102579807_1", "expire": -1}
        auth_module.list_all_authorizations_with_health = lambda: (
            ({group_id: auth_record} if source["healthy"] else {}),
            source["healthy"],
        )
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": []}),
            _FakeResponse({"changes": []}),
            _FakeResponse({"changes": []}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            client_id="desktop",
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            first = asyncio.run(client.sync_once())
            source["healthy"] = False
            partial = asyncio.run(client.sync_once())
            source["healthy"] = True
            recovered = asyncio.run(client.sync_once())

        self.assertEqual(first["uploaded"], 1)
        self.assertEqual(partial["uploaded"], 0)
        self.assertEqual(recovered["uploaded"], 1)
        self.assertEqual(
            [call["json"]["groups"] for call in _FakeAsyncClient.calls],
            [
                [{"group_id": group_id, "auth": {}}],
                [],
                [{"group_id": group_id, "auth": {}}],
            ],
        )

    def test_one_local_group_failure_does_not_block_another_group_ack(self):
        web_sync = _load_web_sync_module()
        failed_group = "1102579807"
        successful_group = "568586484"
        configs = {
            failed_group: {"group_id": failed_group, "features": {}},
            successful_group: {"group_id": successful_group, "features": {}},
        }
        saved = {}

        def save_group_config(group_id, config):
            if group_id == failed_group:
                return False
            saved[group_id] = deepcopy(config)
            configs[group_id] = deepcopy(config)
            return True

        self._configure_sync_fakes(web_sync, configs, save_group_config)
        _FakeAsyncClient.responses = [
            _FakeResponse(
                {
                    "changes": [
                        {"group_id": failed_group, "revision": 7, "config": {"group_id": failed_group, "features": {"power_status": "on"}}},
                        {"group_id": successful_group, "revision": 8, "config": {"group_id": successful_group, "features": {"power_status": "on"}}},
                    ]
                }
            ),
            _FakeResponse({"applied": 1}),
            # The server still has the failed revision pending, so the next
            # exchange returns it and accepts the attached failure report.
            _FakeResponse(
                {"changes": [{"group_id": failed_group, "revision": 7, "config": {"group_id": failed_group, "features": {"power_status": "on"}}}]}
            ),
        ]
        client = web_sync.PortalSyncClient("https://portal.example", "x" * 32, client_id="desktop")

        with patch.dict(sys.modules, web_sync._test_modules):
            first = asyncio.run(client.sync_once())
            self.assertEqual(first["applied"], 1)
            self.assertEqual(first["failed"], 1)
            self.assertEqual(saved[successful_group]["features"]["power_status"], "on")
            self.assertNotIn(failed_group, saved)
            self.assertEqual(len(_FakeAsyncClient.calls), 2)
            self.assertEqual(_FakeAsyncClient.calls[1]["url"], "https://portal.example/api/sync/v1/ack")
            self.assertEqual(
                [item["group_id"] for item in _FakeAsyncClient.calls[1]["json"]["groups"]],
                [successful_group],
            )
            self.assertEqual(
                client._pending_failure_reports[(failed_group, 7)],
                "local_config_save_failed",
            )

            second = asyncio.run(client.sync_once())
            self.assertEqual(second["reported_failures"], 1)
            report = _FakeAsyncClient.calls[2]["json"]["apply_failures"]
            self.assertEqual(
                report,
                [{"group_id": failed_group, "revision": 7, "error": "local_config_save_failed"}],
            )

    def test_ack_failure_is_sanitized_and_reported_on_next_exchange(self):
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}

        def save_group_config(saved_group_id, config):
            configs[saved_group_id] = deepcopy(config)
            return True

        self._configure_sync_fakes(web_sync, configs, save_group_config)
        _FakeAsyncClient.responses = [
            _FakeResponse(
                {"changes": [{"group_id": group_id, "revision": 9, "config": {"group_id": group_id, "features": {"power_status": "on"}}}]}
            ),
            _FakeResponse(error=RuntimeError("https://portal.example/?token=must-not-leak")),
            _FakeResponse(
                {"changes": [{"group_id": group_id, "revision": 9, "config": {"group_id": group_id, "features": {"power_status": "on"}}}]}
            ),
            _FakeResponse({"applied": 1}),
        ]
        client = web_sync.PortalSyncClient("https://portal.example", "x" * 32, client_id="desktop")

        with patch.dict(sys.modules, web_sync._test_modules):
            first = asyncio.run(client.sync_once())
            self.assertEqual(first["failed"], 1)
            self.assertEqual(client._pending_failure_reports[(group_id, 9)], "ack_failed:RuntimeError")

            asyncio.run(client.sync_once())
            report = _FakeAsyncClient.calls[2]["json"]["apply_failures"]
            self.assertEqual(report[0]["group_id"], group_id)
            self.assertEqual(report[0]["revision"], 9)
            self.assertEqual(report[0]["error"], "ack_failed:RuntimeError")
            self.assertNotIn("portal.example", report[0]["error"])
            self.assertNotIn("must-not-leak", report[0]["error"])

    def test_web_pull_save_and_ack_are_visible_per_group_in_cmd(self):
        """A web revision produces a readable group-scoped CMD trail."""

        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}

        def save_group_config(saved_group_id, config):
            configs[saved_group_id] = deepcopy(config)
            return True

        self._configure_sync_fakes(web_sync, configs, save_group_config)
        _FakeAsyncClient.responses = [
            _FakeResponse(
                {
                    "changes": [
                        {
                            "group_id": group_id,
                            "revision": 4,
                            "updated_at": "2026-08-19T12:34:56",
                            "updated_by": 2,
                            "config": {
                                "group_id": group_id,
                                "features": {"recall": {"enabled": False}},
                            },
                        }
                    ]
                }
            ),
            _FakeResponse({"applied": 1}),
        ]
        actions = []

        def capture(action, captured_group, *, detail=""):
            actions.append((action, str(captured_group), detail))

        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )
        with patch.dict(sys.modules, web_sync._test_modules), patch.object(
            web_sync, "_print_sync_group_action", side_effect=capture
        ):
            result = asyncio.run(client.sync_once())

        self.assertEqual(result["applied"], 1)
        self.assertEqual(
            [action[0] for action in actions],
            [
                "分群配置上传",
                "网页通联恢复",
                "网页配置拉取",
                "网页配置已保存",
                "网页配置同步完成",
            ],
        )
        self.assertTrue(all(action[1] == group_id for action in actions))
        self.assertIn("版本 4", actions[2][2])
        self.assertIn("网页保存时间", actions[2][2])
        self.assertIn("recall", actions[2][2])

    def test_current_web_revision_marks_explicit_empty_deletes(self):
        """Server metadata enables safe clearing of empty policy mirrors."""

        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}

        def save_group_config(saved_group_id, config):
            configs[saved_group_id] = deepcopy(config)
            return True

        self._configure_sync_fakes(web_sync, configs, save_group_config)
        web_sync._sync_projection_module = lambda: _AuthoritativeProjection
        _AuthoritativeProjection.calls = []
        _FakeAsyncClient.responses = [
            _FakeResponse(
                {
                    "changes": [
                        {
                            "group_id": group_id,
                            "revision": 1,
                            "source": "web",
                            "config": {
                                "group_id": group_id,
                                "features": {},
                                "blacklist_settings": {},
                            },
                        }
                    ]
                }
            ),
            _FakeResponse({"applied": 1}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, client_id="desktop"
        )

        with patch.dict(sys.modules, web_sync._test_modules):
            result = asyncio.run(client.sync_once())

        self.assertEqual(result["applied"], 1)
        self.assertEqual(_AuthoritativeProjection.calls, [True])

    def test_local_upload_and_refresh_request_are_visible_per_group(self):
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": [], "refresh_group_ids": [group_id]}),
            _FakeResponse({"changes": []}),
        ]
        actions = []
        with patch.dict(sys.modules, web_sync._test_modules), patch.object(
            web_sync,
            "_print_sync_group_action",
            side_effect=lambda action, captured_group, *, detail="": actions.append(
                (action, str(captured_group), detail)
            ),
        ):
            first = asyncio.run(
                web_sync.PortalSyncClient(
                    "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
                ).sync_once()
            )

        self.assertEqual(first["uploaded"], 1)
        self.assertIn(("分群配置上传", group_id), [(a[0], a[1]) for a in actions])
        self.assertIn(("网页拉取请求", group_id), [(a[0], a[1]) for a in actions])


    def test_connection_events_can_be_hidden_from_group_cmd(self):
        """Normal background heartbeats need not duplicate logger status lines."""

        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        _FakeAsyncClient.responses = [_FakeResponse({"changes": []})]
        actions = []
        client = web_sync.PortalSyncClient(
            "https://portal.example",
            "x" * 32,
            card_pepper="p" * 32,
            client_id="desktop",
            show_connection_events=False,
        )
        with patch.dict(sys.modules, web_sync._test_modules), patch.object(
            web_sync,
            "_print_sync_group_action",
            side_effect=lambda action, captured_group, *, detail="": actions.append(
                (action, str(captured_group), detail)
            ),
        ):
            result = asyncio.run(client.sync_once())

        self.assertEqual(result["uploaded"], 1)
        self.assertFalse(any("恢复" in item[0] for item in actions))

    def test_unbound_groups_stop_after_server_scope_is_advertised(self):
        """Only web-bound groups keep full config/runtime/event traffic."""

        web_sync = _load_web_sync_module()
        bound = "1102579807"
        unbound = "1102579808"
        configs = {
            bound: {"group_id": bound, "features": {}},
            unbound: {"group_id": unbound, "features": {}},
        }
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        web_sync.build_runtime_summary = lambda group_id: {
            "stats": {"group": {"total_users": int(str(group_id)[-1])}}
        }
        web_sync.get_console_events = lambda limit=500: [
            {"event_id": "bound-event", "group_id": bound, "action": "x"},
            {"event_id": "unbound-event", "group_id": unbound, "action": "x"},
        ]
        _FakeAsyncClient.responses = [
            _FakeResponse({
                "changes": [],
                "managed_group_ids": [bound],
                "claimable_group_ids": [],
            }),
            _FakeResponse({
                "changes": [],
                "managed_group_ids": [bound],
                "claimable_group_ids": [],
            }),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )
        client._server_scope_supported = None
        with patch.dict(sys.modules, web_sync._test_modules):
            asyncio.run(client.sync_once())
            asyncio.run(client.sync_once())

        first, second = [call["json"] for call in _FakeAsyncClient.calls]
        self.assertEqual(first["groups"], [])
        self.assertEqual(first["runtime_summaries"], {})
        self.assertEqual(first["console_events"], [])
        self.assertFalse(any(item["group_id"] == unbound for item in second["groups"]))
        self.assertEqual({item["group_id"] for item in second["groups"]}, {bound})
        self.assertEqual(set(second["runtime_summaries"]), {bound})
        self.assertEqual({item["group_id"] for item in second["console_events"]}, {bound})
        self.assertTrue(second["sync_scope_complete"])

    def test_unbound_activated_card_bootstrap_has_auth_only_projection(self):
        """First card claim remains possible without full unbound-group sync."""

        web_sync = _load_web_sync_module()
        bound = "1102579807"
        pending = "1102579808"
        configs = {
            bound: {"group_id": bound, "features": {}},
            pending: {"group_id": pending, "features": {}},
        }
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        auth_module = next(
            module for name, module in web_sync._test_modules.items()
            if name.endswith(".auth.manager")
        )
        auth_module.list_all_authorizations_with_health = lambda: (
            {
                pending: {"key": "JK-1-1800000000000" + "A" * 12, "expire": -1},
            },
            True,
        )
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": [], "managed_group_ids": [bound], "claimable_group_ids": [pending]})
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )
        # Simulate a client that has already received the portal scope on a
        # previous heartbeat; this exchange is the card-bootstrap attempt.
        client._server_managed_group_ids = {bound}
        client._server_scope_supported = True
        client._server_claimable_group_ids = {pending}
        with patch.dict(sys.modules, web_sync._test_modules):
            asyncio.run(client.sync_once())
        payload = _FakeAsyncClient.calls[0]["json"]
        pending_item = next(item for item in payload["groups"] if item["group_id"] == pending)
        self.assertNotIn("config", pending_item)
        self.assertIn("auth", pending_item)

    def test_initial_scope_probe_sends_card_auth_but_no_full_group_data(self):
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {"power_status": "on"}}}
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        auth_module = next(
            module for name, module in web_sync._test_modules.items()
            if name.endswith(".auth.manager")
        )
        auth_module.list_all_authorizations_with_health = lambda: (
            {group_id: {"key": "JK-1-1800000000000" + "B" * 12, "expire": -1}},
            True,
        )
        web_sync.build_runtime_summary = lambda _group_id: {"must_not": "run"}
        web_sync.get_console_events = lambda limit=500: [
            {"event_id": "probe-event", "group_id": group_id, "action": "x"}
        ]
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": [], "managed_group_ids": [], "claimable_group_ids": [group_id]})
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )
        client._server_scope_supported = None
        with patch.dict(sys.modules, web_sync._test_modules):
            asyncio.run(client.sync_once())

        payload = _FakeAsyncClient.calls[0]["json"]
        self.assertTrue(payload["scope_probe"])
        self.assertEqual(payload["runtime_summaries"], {})
        self.assertEqual(payload["console_events"], [])
        self.assertEqual(len(payload["groups"]), 1)
        self.assertNotIn("config", payload["groups"][0])
        self.assertIn("auth", payload["groups"][0])

    def test_scope_probe_falls_back_for_older_portal(self):
        web_sync = _load_web_sync_module()
        group_id = "1102579807"
        configs = {group_id: {"group_id": group_id, "features": {}}}
        self._configure_sync_fakes(web_sync, configs, lambda *_args: True)
        _FakeAsyncClient.responses = [
            _FakeResponse({"changes": []}),
            _FakeResponse({"changes": []}),
        ]
        client = web_sync.PortalSyncClient(
            "https://portal.example", "x" * 32, card_pepper="p" * 32, client_id="desktop"
        )
        client._server_scope_supported = None
        with patch.dict(sys.modules, web_sync._test_modules):
            first = asyncio.run(client.sync_once())
            second = asyncio.run(client.sync_once())

        self.assertEqual(first["uploaded"], 0)
        self.assertEqual(second["uploaded"], 1)
        self.assertTrue(_FakeAsyncClient.calls[0]["json"]["scope_probe"])
        self.assertFalse(_FakeAsyncClient.calls[1]["json"]["scope_probe"])
        self.assertFalse(_FakeAsyncClient.calls[1]["json"]["sync_scope_complete"])


if __name__ == "__main__":
    unittest.main()
