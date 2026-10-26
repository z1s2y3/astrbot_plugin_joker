"""Verify the Linux SCUM service dispatches only relay-backed operations."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from web_client.scum_provider import LinuxScumProvider
from web_client.scum_query_service import LinuxScumQueryService


class LinuxScumQueryServiceTests(unittest.TestCase):
    def test_relay_game_time_is_not_labeled_as_update_time(self) -> None:
        rendered = LinuxScumProvider._format_server(
            {
                "id": "41566637",
                "name": "测试服务器",
                "players": 16,
                "maxPlayers": 128,
                "ip": "127.0.0.1",
                "port": 23779,
                "status": "online",
                "country": "CN",
                "time": "12:36",
                "updatedAt": "2026-10-19T04:06:09Z",
            },
            detail=True,
        )
        self.assertIn("游戏时间: 12:36", rendered)
        self.assertNotIn("更新时间", rendered)
        self.assertNotIn("2026-10-19T04:06:09Z", rendered)

    def test_relay_configuration_uses_private_environment_values(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SCUM_RELAY_URL": "https://relay.example",
                "SCUM_RELAY_KEY": "private-key",
                "JOKER_SCUM_BATTLEMETRICS_PROXY": "http://unused.example:8080",
                "JOKER_SCUM_OPENGSQ_URL": "https://unused.example",
            },
        ):
            self.assertEqual(
                LinuxScumQueryService._config_values(),
                ("https://relay.example", "private-key"),
            )

    def test_legacy_operations_are_rejected_before_network_client_initialization(self) -> None:
        service = LinuxScumQueryService()
        service._get_client = AsyncMock(side_effect=AssertionError("unexpected request"))

        async def check_results() -> None:
            for operation in ("ip", "news", "ranking"):
                with self.assertRaisesRegex(ValueError, "unsupported_operation"):
                    await service.query(operation, "legacy")

        asyncio.run(check_results())
        service._get_client.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
