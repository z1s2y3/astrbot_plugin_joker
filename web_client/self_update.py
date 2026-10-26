"""Fast staging helpers for administrator-triggered Linux portal updates.

The web process never executes or fully validates an uploaded archive. It
only applies transport limits, stores private task inputs, and atomically
publishes a request for a root-owned systemd worker. That worker is the
authoritative security boundary: it copies the untrusted ZIP into a root-only
directory, validates its structure, extracts it safely, verifies every
manifest hash, and only then runs the installer.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any
from uuid import uuid4

from .release_service import format_china_time


LEGACY_UPDATE_ROOT = Path("/www/wwwroot/joker-install")
WWW1_UPDATE_ROOT = Path("/www1/joker-install")


def _default_update_root() -> Path:
    """Prefer the separate /www1 volume so uploads do not fill the root disk."""
    configured = os.getenv("JOKER_WEB_UPDATE_STAGING_DIR", "").strip()
    # An older systemd unit may still export the legacy path.  Treat that
    # value as a fallback once /www1 is available so browser uploads move to
    # the separate data volume without requiring a manual config edit.
    if configured and not (
        configured == str(LEGACY_UPDATE_ROOT)
        and WWW1_UPDATE_ROOT.parent.is_dir()
        and not WWW1_UPDATE_ROOT.parent.is_symlink()
    ):
        return Path(configured)
    if WWW1_UPDATE_ROOT.parent.is_dir() and not WWW1_UPDATE_ROOT.parent.is_symlink():
        return WWW1_UPDATE_ROOT
    return LEGACY_UPDATE_ROOT


UPDATE_ROOT = _default_update_root()
INCOMING_DIR = UPDATE_ROOT / "incoming"
STATUS_FILE = UPDATE_ROOT / "last-status.json"
HISTORY_FILE = UPDATE_ROOT / "update-history.json"
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
# Multipart boundaries and the small authorization form field add a little
# overhead to the archive. Keep this in sync with the Nginx request limit.
MAX_UPDATE_REQUEST_BYTES = MAX_ARCHIVE_BYTES + 2 * 1024 * 1024
PENDING_RELEASE_VERSION = "pending-validation"


class UpdateError(ValueError):
    """A staged portal update cannot be accepted or queued."""


def get_update_root() -> str:
    """Return the private server directory used for update uploads."""

    return str(UPDATE_ROOT)


def get_update_status() -> dict[str, Any]:
    """Return the root helper's public status without exposing update secrets."""

    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"state": "idle", "message": "暂无更新任务"}
    if not isinstance(data, dict):
        return {"state": "unknown", "message": "更新状态文件格式无效"}
    return {
        "state": str(data.get("state", "unknown"))[:32],
        "message": str(data.get("message", ""))[:300],
        "updated_at": format_china_time(data.get("updated_at", "")),
        "package": str(data.get("package", ""))[:160],
        "release_version": str(data.get("release_version", ""))[:64],
    }


def get_update_history(limit: int = 20) -> list[dict[str, str]]:
    """Read root-worker update results for the administrator release log."""

    try:
        info = HISTORY_FILE.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size > 256 * 1024
        ):
            return []
        payload = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return []
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, str]] = []
    for item in payload[: max(1, min(int(limit), 50))]:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "state": str(item.get("state", "unknown"))[:32],
                "message": str(item.get("message", ""))[:300],
                "updated_at": format_china_time(item.get("updated_at", "")),
                "package": str(item.get("package", ""))[:160],
                "release_version": str(item.get("release_version", ""))[:64],
                "update_id": str(item.get("update_id", ""))[:32],
            }
        )
    return rows


def _display_filename(upload: Any) -> str:
    """Return a bounded basename safe to show in the administrator audit log."""

    raw_name = str(getattr(upload, "filename", "") or "")
    return raw_name.replace("\\", "/").rsplit("/", 1)[-1].strip()[:160]


async def stage_update_package(upload: Any, authorization_code: str) -> dict[str, Any]:
    """Store bounded task inputs without validating the ZIP in the HTTP path.

    The browser request intentionally avoids opening, decompressing, or
    hashing the archive. Root repeats all trust-sensitive validation after an
    fd-safe handoff, so a malformed ZIP still cannot reach the installer.
    """

    if os.name != "posix":
        raise UpdateError("网页后台更新仅支持 Linux 服务器部署")
    filename = _display_filename(upload)
    if not filename.lower().endswith(".zip"):
        raise UpdateError("只能上传 Joker 网页部署 ZIP 包")
    authorization_code = authorization_code.strip()
    if not 24 <= len(authorization_code) <= 256:
        raise UpdateError("请填写服务器更新授权码")
    if not INCOMING_DIR.is_dir():
        raise UpdateError("服务器尚未启用网页后台更新，请先使用部署包手动升级一次")

    update_id = uuid4().hex
    temporary = INCOMING_DIR / f".{update_id}.uploading"
    archive_path = INCOMING_DIR / f"{update_id}.zip"
    auth_path = INCOMING_DIR / f"{update_id}.auth"
    size = 0
    try:
        with temporary.open("xb") as destination:
            os.fchmod(destination.fileno(), 0o600)
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise UpdateError("更新包不能超过 80 MB")
                # File writes and fsync can block noticeably on a full or
                # shared disk. Keep them outside Uvicorn's event loop so one
                # administrator's large upload does not turn unrelated saves
                # or Windows sync requests into apparent proxy timeouts.
                await asyncio.to_thread(destination.write, chunk)
            await asyncio.to_thread(destination.flush)
            await asyncio.to_thread(os.fsync, destination.fileno())
        if size == 0:
            raise UpdateError("上传的更新包为空")

        # The root-owned handoff validates paths, links, archive limits, and
        # every manifest hash before it extracts or executes this ZIP. Keeping
        # the expensive scan out of this request lets the browser receive its
        # accepted response before an upstream proxy reaches its timeout.
        os.replace(temporary, archive_path)
        _write_private_text(auth_path, authorization_code + "\n", encoding="utf-8")
        return {
            "update_id": update_id,
            "filename": filename,
            "size": size,
            "release_version": PENDING_RELEASE_VERSION,
        }
    except UpdateError:
        archive_path.unlink(missing_ok=True)
        auth_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        archive_path.unlink(missing_ok=True)
        auth_path.unlink(missing_ok=True)
        raise UpdateError("无法保存更新包，请检查服务器磁盘和目录权限") from exc
    except Exception:
        # Keep a partial upload from consuming disk or leaving an authorization
        # file behind if the multipart implementation raises unexpectedly.
        archive_path.unlink(missing_ok=True)
        auth_path.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        close = getattr(upload, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


def queue_staged_update(update_id: str) -> None:
    """Atomically publish a request watched by the root-owned systemd unit.

    The watcher reacts to ``*.request`` path changes. Writing that final name
    directly lets an eager watcher see a partial task record, so a hidden
    temporary file is flushed first and then atomically renamed into view.
    """

    if len(update_id) != 32 or any(
        character not in "0123456789abcdef" for character in update_id
    ):
        raise UpdateError("更新任务标识无效")
    request_path = INCOMING_DIR / f"{update_id}.request"
    temporary = INCOMING_DIR / f".{update_id}.requesting"
    try:
        if request_path.exists() or request_path.is_symlink():
            raise FileExistsError("update request already exists")
        _write_private_text(temporary, update_id + "\n", encoding="ascii")
        os.replace(temporary, request_path)
    except OSError as exc:
        # Never remove the final filename on failure. It may have been
        # published and moved into the root-only work directory already.
        temporary.unlink(missing_ok=True)
        raise UpdateError("无法排队更新，请使用上传包手动更新") from exc


def discard_staged_update(update_id: str) -> None:
    """Remove an upload when it could not be published to the root worker."""

    if len(update_id) != 32 or any(
        character not in "0123456789abcdef" for character in update_id
    ):
        return
    # Leave ``.request`` untouched: root may have already accepted it. The
    # hidden request temp is safe to remove because the watcher ignores it.
    for suffix in (".uploading", ".requesting", ".zip", ".auth"):
        try:
            (INCOMING_DIR / f"{update_id}{suffix}").unlink(missing_ok=True)
        except OSError:
            # The queue failure is more useful than a best-effort cleanup
            # failure, and the root worker rejects incomplete tasks itself.
            pass


def _write_private_text(path: Path, content: str, *, encoding: str) -> None:
    """Create a private task file without ever following an existing link."""

    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, closefd=False) as destination:
            destination.write(content)
            destination.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
