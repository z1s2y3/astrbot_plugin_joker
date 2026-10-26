"""Linux-side SCUM query service.

SCUM provider requests are intentionally executed by the Linux portal. The
Windows plugin only sends the command scope and displays the returned text;
this keeps provider networking off the machine that commonly cannot reach the
SCUM endpoints.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .scum_provider import LinuxScumProvider


class LinuxScumQueryService:
    """Reuse an isolated Linux provider client following portal settings."""

    def __init__(self) -> None:
        self._client: LinuxScumProvider | None = None
        self._signature: tuple[str, str] | None = None

    @staticmethod
    def _config_values() -> tuple[str, str]:
        # Keep the relay key in the Linux portal environment, never in the
        # Windows plugin configuration or public client settings.
        # This service can be imported directly by uvicorn workers and by the
        # standalone query process, before the portal Settings object loads
        # its environment. Load the local portal .env defensively so relay
        # credentials are available in those launch modes.
        try:
            from dotenv import load_dotenv

            load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
        except Exception:
            pass
        relay_url = str(os.getenv("SCUM_RELAY_URL", "") or "").strip()
        relay_key = str(os.getenv("SCUM_RELAY_KEY", "") or "").strip()
        return relay_url, relay_key

    async def _get_client(self) -> LinuxScumProvider:
        relay_url, relay_key = self._config_values()
        signature = (relay_url, relay_key)
        if self._client is None or self._signature != signature:
            self._client = LinuxScumProvider(relay_url=relay_url, relay_key=relay_key)
            self._signature = signature
        return self._client

    async def query(self, operation: str, argument: Any = "") -> str:
        """Execute one bounded read-only SCUM operation."""

        op = str(operation or "").strip().lower()
        value = str(argument or "").strip()
        if op not in {"search", "detail"}:
            raise ValueError("unsupported_operation")
        client = await self._get_client()
        if op == "search":
            return await client.search_servers(value)
        if op == "detail":
            return await client.get_server_detail(value)
        raise ValueError("unsupported_operation")
