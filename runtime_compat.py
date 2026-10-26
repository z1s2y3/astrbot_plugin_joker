"""Small runtime fallbacks for standalone module tests.

The plugin normally runs inside AstrBot, where ``logger`` and
``get_astrbot_data_path`` are provided by the host.  A number of persistence
helpers are also useful in migration scripts and unit tests that intentionally
run without AstrBot installed.  Keeping the fallback here avoids duplicating
host-detection logic in every helper module while leaving the production path
unchanged.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path


try:  # pragma: no cover - exercised only inside a real AstrBot process
    from astrbot.api import logger as logger  # type: ignore
except Exception:  # pragma: no cover - covered by standalone tests
    logger = logging.getLogger("astrbot_plugin_joker")
    if not logger.handlers:
        logging.basicConfig(level=logging.WARNING)


try:  # pragma: no cover - exercised only inside a real AstrBot process
    from astrbot.core.utils.astrbot_path import (  # type: ignore
        get_astrbot_data_path as get_astrbot_data_path,
    )
except Exception:  # pragma: no cover - covered by standalone tests
    def get_astrbot_data_path() -> str:
        """Return an isolated writable data directory outside the package."""

        configured = os.environ.get("ASTRBOT_DATA_PATH", "").strip()
        if configured:
            path = Path(configured)
        else:
            path = Path.cwd() / ".astrbot-data"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


__all__ = ["logger", "get_astrbot_data_path"]
