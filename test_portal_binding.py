"""Focused coverage for the standalone Windows portal-binding helper."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path

import httpx


MODULE_PATH = Path(__file__).resolve().parent / "qqadmin" / "portal_binding.py"


def _load_module():
    """Load the helper without importing the rest of AstrBot's plugin package."""

    module_name = "_joker_portal_binding_test"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


portal_binding = _load_module()
BINDING_PROBE_CLIENT_ID = portal_binding.BINDING_PROBE_CLIENT_ID
PortalBinding = portal_binding.PortalBinding
legacy_server_credential_fields = portal_binding.legacy_server_credential_fields
portal_runtime_status = portal_binding.portal_runtime_status
probe_portal_binding = portal_binding.probe_portal_binding


class PortalBindingTests(unittest.TestCase):
    def _binding(self) -> PortalBinding:
        return PortalBinding(
            portal_url="https://portal.example.com/",
            sync_token="t" * 48,
            card_pepper="p" * 48,
            enabled=True,
        )

    def test_reads_existing_web_sync_shape_and_ignores_legacy_ssh_values(self):
        config = {
            "web_sync": {
                "items": {
                    "enabled": {"default": "true"},
                    "portal_url": {"default": "https://portal.example.com/"},
                    "sync_token": {"default": "t" * 32},
                    "card_pepper": {"default": "p" * 32},
                    "interval_seconds": {"default": "5"},
                    "server_ip": "203.0.113.54",
                    "server_username": "root",
                    "server_password": "must-never-be-copied",
                    "server_port": 2222,
                }
            }
        }

        binding = PortalBinding.from_plugin_config(config)

        self.assertEqual(binding.portal_url, "https://portal.example.com/")
        self.assertEqual(binding.interval_seconds, 10)
        self.assertTrue(binding.enabled)
        self.assertTrue(binding.configured)
        self.assertNotIn("server_password", binding.__dataclass_fields__)
        self.assertEqual(
            legacy_server_credential_fields(config),
            ("server_ip", "server_password", "server_port", "server_username"),
        )

    def test_validation_requires_domain_https_and_three_real_sync_values(self):
        self.assertIn(
            "HTTPS",
            PortalBinding(
                portal_url="http://portal.example.com",
                sync_token="t" * 32,
                card_pepper="p" * 32,
            ).configuration_error,
        )
        self.assertIn(
            "服务器 IP",
            PortalBinding(
                portal_url="https://203.0.113.54",
                sync_token="t" * 32,
                card_pepper="p" * 32,
            ).configuration_error,
        )
        self.assertIn(
            "同步令牌",
            PortalBinding(
                portal_url="https://portal.example.com",
                sync_token="short",
                card_pepper="p" * 32,
            ).validate(),
        )
        self.assertIn(
            "Card Pepper",
            PortalBinding(
                portal_url="https://portal.example.com",
                sync_token="t" * 32,
                card_pepper="short",
            ).configuration_error,
        )

    def test_probe_uses_empty_non_authoritative_exchange_and_never_exposes_secrets(self):
        calls: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.url.path == "/health/ready":
                return httpx.Response(200, json={"status": "ok"})
            if request.url.path == "/api/sync/v1/exchange":
                return httpx.Response(200, json={"status": "ok", "changes": []})
            return httpx.Response(404)

        binding = self._binding()
        result = asyncio.run(
            probe_portal_binding(binding, transport=httpx.MockTransport(handler))
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "connected")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("authorization", calls[0].headers)
        self.assertNotIn("x-joker-card-pepper-proof", calls[0].headers)
        exchange = calls[1]
        payload = json.loads(exchange.content.decode("utf-8"))
        self.assertEqual(payload["client_id"], BINDING_PROBE_CLIENT_ID)
        self.assertEqual(payload["known_group_ids"], [])
        self.assertEqual(payload["groups"], [])
        self.assertEqual(payload["console_events"], [])
        self.assertEqual(payload["apply_failures"], [])
        self.assertFalse(payload["authoritative_group_ids"])
        self.assertEqual(exchange.headers["authorization"], "Bearer " + "t" * 48)
        self.assertNotIn("sync_token", result.safe_status())
        self.assertNotIn("card_pepper", result.safe_status())

    def test_probe_reports_pepper_mismatch_without_sending_or_showing_pepper(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health/ready":
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(409, json={"code": "card_pepper_mismatch"})

        result = asyncio.run(
            probe_portal_binding(self._binding(), transport=httpx.MockTransport(handler))
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "card_pepper_mismatch")
        self.assertTrue(result.authenticated)
        self.assertFalse(result.card_pepper_verified)
        self.assertNotIn("p" * 48, result.format_text())

    def test_probe_distinguishes_exchange_timeout_after_healthy_portal(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health/ready":
                return httpx.Response(200, json={"status": "ok"})
            raise httpx.ReadTimeout("sync exchange timed out", request=request)

        result = asyncio.run(
            probe_portal_binding(self._binding(), transport=httpx.MockTransport(handler))
        )

        self.assertEqual(result.code, "timeout")
        self.assertTrue(result.reachable)
        self.assertIn("健康检查正常", result.message)

    def test_probe_refuses_redirect_so_token_is_not_followed_to_an_ip(self):
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://203.0.113.54/"})

        result = asyncio.run(
            probe_portal_binding(self._binding(), transport=httpx.MockTransport(handler))
        )

        self.assertEqual(result.code, "unexpected_redirect")
        self.assertTrue(result.reachable)
        self.assertNotIn("203.0.113.54", result.format_text())

    def test_probe_explains_cdn_tls_handshake_failure(self):
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(525, json={"detail": "edge failure"})

        result = asyncio.run(
            probe_portal_binding(self._binding(), transport=httpx.MockTransport(handler))
        )

        self.assertEqual(result.code, "cdn_origin_tls_handshake_failed")
        self.assertEqual(result.http_status, 525)
        self.assertIn("不是卡密或关联问题", result.format_text())

    def test_probe_classifies_read_error_without_exposing_transport_text(self):
        class ReadError(httpx.RequestError):
            pass

        async def handler(request: httpx.Request) -> httpx.Response:
            raise ReadError(
                "response interrupted at https://portal.example.com/?token=must-not-leak",
                request=request,
            )

        result = asyncio.run(
            probe_portal_binding(self._binding(), transport=httpx.MockTransport(handler))
        )

        self.assertEqual(result.code, "read_error")
        self.assertIn("读取响应中断", result.format_text())
        self.assertNotIn("must-not-leak", result.format_text())

    def test_probe_classifies_tls_error_without_exposing_transport_text(self):
        class ConnectError(httpx.RequestError):
            pass

        async def handler(request: httpx.Request) -> httpx.Response:
            raise ConnectError(
                "certificate verify failed for https://portal.example.com/?token=must-not-leak",
                request=request,
            )

        result = asyncio.run(
            probe_portal_binding(self._binding(), transport=httpx.MockTransport(handler))
        )

        self.assertEqual(result.code, "tls_handshake_failed")
        self.assertIn("TLS 握手", result.format_text())
        self.assertNotIn("must-not-leak", result.format_text())

    def test_runtime_status_drops_untrusted_exception_text(self):
        class SyncClient:
            last_success_at = 123
            last_error = "ConnectError https://portal.example.com/?token=must-not-leak"

        status = portal_runtime_status(self._binding(), SyncClient())

        self.assertEqual(status.code, "degraded")
        self.assertEqual(status.last_error_code, "sync_error")
        self.assertNotIn("must-not-leak", status.format_text())
        self.assertNotIn("must-not-leak", str(status.safe_status()))

    def test_runtime_status_explains_525_without_exposing_transport_details(self):
        class SyncClient:
            last_success_at = 0
            last_error = "http_status_525"

        status = portal_runtime_status(self._binding(), SyncClient())

        self.assertEqual(status.code, "degraded")
        self.assertIn("TLS 握手失败", status.format_text())
        self.assertIn("不是卡密或关联问题", status.format_text())

    def test_runtime_status_keeps_canonical_read_error(self):
        class SyncClient:
            last_success_at = 0
            # ``readerror`` is accepted from a previous plugin build and is
            # normalized before being displayed by the current status command.
            last_error = "readerror"

        status = portal_runtime_status(self._binding(), SyncClient())

        self.assertEqual(status.code, "degraded")
        self.assertEqual(status.last_error_code, "read_error")
        self.assertIn("读取响应中断", status.format_text())


if __name__ == "__main__":
    unittest.main()
