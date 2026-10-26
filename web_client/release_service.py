"""Read the safe, installer-written portal release marker.

The marker lives beside ``web_client`` rather than in the database so the
reported version always describes the code currently serving this request.
It contains no credentials and is published only after the installer health
check succeeds.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


RELEASE_INFO_FILENAME = ".joker-web-release.json"
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def format_china_time(value: object) -> str:
    """Show legacy UTC installer timestamps in China Standard Time."""
    text = str(value or "").strip()[:64]
    if not text:
        return ""
    try:
        if text.endswith(" UTC"):
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S UTC").replace(
                tzinfo=timezone.utc
            )
        else:
            candidate = text.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                return text
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S CST"
        )
    except (TypeError, ValueError, OverflowError):
        return text


def _unknown_release(message: str) -> dict[str, str | bool]:
    return {
        "known": False,
        "version": "未记录",
        "package": "",
        "installed_at": "",
        "message": message,
    }


def release_info_path() -> Path:
    """Return the installer marker path, with a test-only environment override."""

    configured = os.getenv("JOKER_WEB_RELEASE_INFO_FILE", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / RELEASE_INFO_FILENAME


def get_installed_release_info() -> dict[str, str | bool]:
    """Return a bounded presentation model for the current running portal.

    Never follow a symlink here. The installer writes a root-owned regular
    file after its readiness probe; a malformed or old installation simply
    reports an unknown version rather than breaking the administrator page.
    """

    marker = release_info_path()
    try:
        info = marker.lstat()
    except OSError:
        return _unknown_release("当前安装尚未写入版本记录；升级一次即可显示准确版本。")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return _unknown_release("版本记录不可用，请使用完整部署包重新升级。")
    if info.st_size > 4096:
        return _unknown_release("版本记录格式无效，请使用完整部署包重新升级。")
    try:
        payload: Any = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return _unknown_release("版本记录格式无效，请使用完整部署包重新升级。")
    if not isinstance(payload, dict):
        return _unknown_release("版本记录格式无效，请使用完整部署包重新升级。")
    version = str(payload.get("release_version", "")).strip()
    if not SEMVER_RE.fullmatch(version):
        return _unknown_release("版本记录格式无效，请使用完整部署包重新升级。")
    package = str(payload.get("archive_filename", "")).strip()[:160]
    if package and ("/" in package or "\\" in package):
        return _unknown_release("版本记录格式无效，请使用完整部署包重新升级。")
    installed_at = format_china_time(payload.get("installed_at", ""))
    return {
        "known": True,
        "version": version,
        "package": package,
        "installed_at": installed_at,
        "message": "安装器健康检查通过后记录",
    }
