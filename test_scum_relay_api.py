"""Verify SCUM search and detail queries use only the authenticated relay."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import httpx

from web_client.scum_provider import LinuxScumProvider


class ScumRelayApiTests(unittest.TestCase):
    def test_search_and_detail_use_relay_and_refresh_each_request(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/api/search":
                return httpx.Response(
                    200,
                    json={
                        "fetchedAt": "2026-10-19T12:00:00.000Z",
                        "servers": [
                            {
                                "id": "39243225",
                                "name": "建家 SCUM",
                                "players": 5,
                                "maxPlayers": 64,
                                "ip": "192.0.2.1",
                                "port": 9222,
                                "country": "China",
                                "status": None,
                            }
                        ]
                    },
                )
            server_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "server": {
                        "id": server_id,
                        "name": "建家 SCUM",
                        "players": 7,
                        "maxPlayers": 64,
                        "ip": "192.0.2.1",
                        "port": 9222,
                        "country": "China",
                        "status": "online",
                        "rank": 17,
                        "version": "1.3.4.2.161892",
                        "updatedAt": "12:20",
                    },
                    "fetchedAt": "2026-10-19T12:01:00.000Z",
                },
            )

        transport = httpx.MockTransport(handle)
        real_async_client = httpx.AsyncClient

        def client_factory(**kwargs: object) -> httpx.AsyncClient:
            return real_async_client(transport=transport, **kwargs)

        provider = LinuxScumProvider(
            relay_url="https://relay.example", relay_key="test-relay-key"
        )
        with patch("web_client.scum_provider.httpx.AsyncClient", side_effect=client_factory):
            search_result = asyncio.run(provider.search_servers("建家"))
            asyncio.run(provider.search_servers("建家"))
            detail_result = asyncio.run(provider.get_server_detail("39243225"))
            numeric_result = asyncio.run(provider.search_servers("41568549"))

        self.assertEqual(len(requests), 4)
        self.assertEqual([request.url.path for request in requests], [
            "/api/search",
            "/api/search",
            "/api/servers/39243225",
            "/api/servers/41568549",
        ])
        self.assertEqual(requests[0].url.params["q"], "建家")
        self.assertTrue(
            all(request.headers["Authorization"] == "Bearer test-relay-key" for request in requests)
        )
        self.assertIn("在线人数: 5/64", search_result)
        self.assertIn("状态: 未知", search_result)
        self.assertNotIn("中转查询时间:", search_result)
        self.assertIn("在线人数: 7/64", detail_result)
        self.assertIn("状态: online", detail_result)
        self.assertIn("排名: #17", detail_result)
        self.assertIn("版本: 1.3.4.2.161892", detail_result)
        self.assertIn("游戏时间: 12:20", detail_result)
        self.assertNotIn("更新时间:", detail_result)
        self.assertIn("是否开机: 是", detail_result)
        self.assertIn("ID: 41568549", numeric_result)
        self.assertIn("是否开机: 是", numeric_result)

    def test_keyword_search_returns_all_records_without_detail_requests(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/api/search":
                return httpx.Response(
                    200,
                    json={
                        "fetchedAt": "2026-10-19T12:00:00.000Z",
                        "servers": [
                            {
                                "id": "39243225",
                                "name": "建家 SCUM",
                                "players": 5,
                                "maxPlayers": 64,
                                "status": None,
                            },
                            {"id": "41568549", "name": "另一台 SCUM", "players": 8},
                        ]
                    },
                )
            return httpx.Response(429, json={"error": "中转站正在处理其他查询，请稍后重试。"})

        transport = httpx.MockTransport(handle)
        real_async_client = httpx.AsyncClient

        def client_factory(**kwargs: object) -> httpx.AsyncClient:
            return real_async_client(transport=transport, **kwargs)

        provider = LinuxScumProvider(
            relay_url="https://relay.example", relay_key="test-relay-key"
        )
        with patch("web_client.scum_provider.httpx.AsyncClient", side_effect=client_factory):
            result = asyncio.run(provider.search_servers("建家"))

        self.assertEqual(
            [request.url.path for request in requests],
            ["/api/search"],
        )
        self.assertIn("39243225", result)
        self.assertIn("41568549", result)
        self.assertIn("在线人数: 5/64", result)
        self.assertNotIn("详情查询失败", result)
        self.assertNotIn("正在处理其他查询", result)
        self.assertNotIn("中转查询时间:", result)

    def test_eight_digit_query_requests_only_that_server_detail(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            server_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "server": {
                        "id": server_id,
                        "name": f"服务器 {server_id}",
                        "players": 8,
                        "maxPlayers": 64,
                        "status": "online",
                    }
                },
            )

        transport = httpx.MockTransport(handle)
        real_async_client = httpx.AsyncClient

        def client_factory(**kwargs: object) -> httpx.AsyncClient:
            return real_async_client(transport=transport, **kwargs)

        provider = LinuxScumProvider(
            relay_url="https://relay.example", relay_key="test-relay-key"
        )
        with patch("web_client.scum_provider.httpx.AsyncClient", side_effect=client_factory):
            result = asyncio.run(provider.search_servers("41568549"))

        self.assertEqual(
            [request.url.path for request in requests],
            ["/api/servers/41568549"],
        )
        self.assertIn("服务器 41568549", result)
        self.assertEqual(result.count("服务器详细信息："), 1)
        self.assertEqual(result.count("是否开机: 是"), 1)

    def test_relay_errors_are_returned_without_direct_fallback(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(503, json={"error": "中转浏览器需要验证。"})

        transport = httpx.MockTransport(handle)
        real_async_client = httpx.AsyncClient

        def client_factory(**kwargs: object) -> httpx.AsyncClient:
            return real_async_client(transport=transport, **kwargs)

        provider = LinuxScumProvider(relay_key="test-relay-key")
        with patch("web_client.scum_provider.httpx.AsyncClient", side_effect=client_factory):
            result = asyncio.run(provider.search_servers("8084"))

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/api/search")
        self.assertIn("中转浏览器需要验证", result)

    def test_missing_key_never_makes_a_request(self) -> None:
        provider = LinuxScumProvider()
        with patch("web_client.scum_provider.httpx.AsyncClient") as client:
            missing_key_result = asyncio.run(provider.search_servers("建家"))

        client.assert_not_called()
        self.assertIn("SCUM_RELAY_KEY", missing_key_result)

    def test_relay_key_is_never_sent_over_plain_http(self) -> None:
        provider = LinuxScumProvider(
            relay_url="http://relay.example", relay_key="test-relay-key"
        )
        with patch("web_client.scum_provider.httpx.AsyncClient") as client:
            result = asyncio.run(provider.search_servers("8084"))

        client.assert_not_called()
        self.assertIn("中转地址无效", result)


if __name__ == "__main__":
    unittest.main()
