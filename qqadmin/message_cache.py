"""Persistent, per-group message history used by recall administration.

Message history is intentionally kept outside ``group_configs``.  Group
configuration files are read on most messages, while message history can grow
quickly and contains user-generated content.
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta
from enum import Enum
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:  # AstrBot supplies the plugin logger in production.
    from astrbot.api import logger
except ImportError:
    try:
        from ..runtime_compat import logger
    except ImportError:
        from runtime_compat import logger

from .group_config import get_group_config_file, get_plugin_data_path


DEFAULT_MESSAGE_CACHE_SETTINGS = {
    "enabled": True,
    "cache_raw_message": True,
    "retention_days": 0,
    "max_messages_per_user": 0,
    # Image files are stored separately from the JSON history.  Keeping this
    # enabled by default makes the history useful after QQ temporary URLs
    # expire, while the live message handler only ever queues this work.
    "archive_images": True,
    "archive_image_max_mb": 20,
}

_CACHE_LOCK = threading.RLock()
_LAST_CLEANUP_AT: dict[str, float] = {}
_PENDING_RECALL_MARKS: dict[tuple[str, str], dict[str, Any]] = {}
# Incremented whenever a group's configuration/data is explicitly removed.
# Queued records carry the generation they observed, so stale async writes do
# not recreate a cache after a reset or deletion.
_CACHE_GENERATIONS: dict[str, int] = {}
_ACTIVITY_CACHE: dict[tuple[str, int], tuple[int, dict[str, dict[str, Any]]]] = {}
_CLEANUP_INTERVAL_SECONDS = 600
_ACTIVITY_CACHE_TTL_SECONDS = 20
_RECALL_MARK_TTL_SECONDS = 600
_MAX_PENDING_RECALL_MARKS = 2000
_SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
_CQ_PATTERN = re.compile(r"\[CQ:([A-Za-z0-9_]+)(?:,([^\]]*))?\]")
_CQ_FIELD_PATTERN = re.compile(r"([^=,]+)=((?:&#44;|[^,])*)")
_MAX_TEXT_LENGTH = 4096
_MAX_RAW_MESSAGE_LENGTH = 8192
_MAX_SEGMENT_VALUE_LENGTH = 1024
_MAX_SEGMENTS = 100
_MAX_RETENTION_DAYS = 3650
_MAX_MESSAGES_PER_USER = 5000
_MAX_ARCHIVE_IMAGE_MB = 100
_MAX_ARCHIVE_IMAGE_BYTES = _MAX_ARCHIVE_IMAGE_MB * 1024 * 1024
_IMAGE_ARCHIVE_DIRNAME = "message_images"
_PENDING_CACHE_DIRNAME = ".pending"
_PENDING_CACHE_MAX_RECORDS = 20000
_IMAGE_SOURCE_KEYS = ("url", "image_url", "file", "file_", "path")

# AstrBot's built-in components cover most of these fields, while adapters
# commonly add a few more (rich emoji IDs, dimensions, durations, etc.).
# Keeping the list here makes the persisted representation forward-compatible
# without ever touching computed properties such as ``File.file``.
_COMPONENT_FIELD_NAMES = (
    "text",
    "url",
    "file",
    "file_",
    "file_id",
    "file_name",
    "path",
    "qq",
    "id",
    "message_id",
    "name",
    "title",
    "summary",
    "data",
    "content",
    "cover",
    "image",
    "image_url",
    "sender_id",
    "sender_name",
    "sender_nickname",
    "nickname",
    "user_id",
    "uin",
    "message_str",
    "time",
    "address",
    "latitude",
    "longitude",
    "lat",
    "lon",
    "type",
    "sub_type",
    "chain",
    "nodes",
    "message",
    "messages",
    "_type",
    # Common OneBot/QQ rich-media fields.
    "emoji_id",
    "emoji_package_id",
    "face_id",
    "key",
    "business_id",
    "file_size",
    "duration",
    "width",
    "height",
    "alt",
    "display",
    "source",
    "target",
)


def _safe_identifier(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or not _SAFE_ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} contains unsafe characters")
    return normalized


def _limited_text(value: Any, limit: int = _MAX_TEXT_LENGTH) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = str(value)
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _safe_json_value(value: Any, limit: int = _MAX_SEGMENT_VALUE_LENGTH) -> Any:
    """Convert arbitrary adapter data into small JSON-compatible values."""
    if isinstance(value, Enum):
        value = value.value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _limited_text(value, limit)
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(item, limit) for item in value[:20]]
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:30]:
            result[_limited_text(key, 64)] = _safe_json_value(item, limit)
        return result
    return _limited_text(value, limit)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_message_cache_settings(settings: dict | None = None) -> dict:
    """Return bounded, storage-safe cache settings with defaults applied."""
    source = settings if isinstance(settings, dict) else {}
    normalized = dict(DEFAULT_MESSAGE_CACHE_SETTINGS)
    normalized["enabled"] = _as_bool(source.get("enabled", normalized["enabled"]))
    normalized["cache_raw_message"] = _as_bool(
        source.get("cache_raw_message", normalized["cache_raw_message"])
    )
    normalized["archive_images"] = _as_bool(
        source.get("archive_images", normalized["archive_images"])
    )

    for key, minimum, maximum in (
        ("retention_days", 0, _MAX_RETENTION_DAYS),
        ("max_messages_per_user", 0, _MAX_MESSAGES_PER_USER),
        ("archive_image_max_mb", 1, _MAX_ARCHIVE_IMAGE_MB),
    ):
        try:
            value = int(source.get(key, normalized[key]))
        except (TypeError, ValueError):
            value = normalized[key]
        normalized[key] = max(minimum, min(maximum, value))
    return normalized


def get_message_cache_dir(create: bool = True) -> Path:
    cache_dir = Path(get_plugin_data_path()) / "message_cache"
    if create:
        cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _get_pending_cache_dir(create: bool = True) -> Path:
    """Return the durable spool used when a live cache write cannot finish.

    The spool is deliberately kept beside the normal cache, but in a hidden
    namespace so it cannot be mistaken for a retained message day.  It is
    JSONL because appending one bounded record is safer than rewriting a large
    group history while the event loop is busy.
    """
    pending_dir = get_message_cache_dir(create=create) / _PENDING_CACHE_DIRNAME
    if create:
        pending_dir.mkdir(parents=True, exist_ok=True)
    return pending_dir


def _pending_cache_path(scope_id: str, create: bool = True) -> Path:
    safe_scope = _safe_identifier(scope_id, "scope_id")
    return _get_pending_cache_dir(create=create) / f"{safe_scope}.jsonl"


def queue_pending_message_cache_record(record: dict) -> bool:
    """Persist one cache record for retry after a transient write failure."""
    if not isinstance(record, dict):
        return False
    raw_scope = str(record.get("group_id", "") or "").strip()
    if not raw_scope:
        return False
    try:
        path = _pending_cache_path(raw_scope, create=True)
        # Keep only normalized JSON-safe values; private queue metadata is safe
        # to retain because the normal writer removes it before final storage.
        payload = _safe_json_value(dict(record), _MAX_RAW_MESSAGE_LENGTH)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(line.encode("utf-8", "ignore")) > 2 * 1024 * 1024:
            logger.warning("Skipping oversized pending message cache record scope=%s", raw_scope)
            return False
        with _CACHE_LOCK:
            existing = []
            if path.exists():
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        existing = [item for item in handle if item.strip()][-_PENDING_CACHE_MAX_RECORDS + 1 :]
                except (OSError, UnicodeError):
                    existing = []
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            # Bound a repeatedly failing spool without touching the durable
            # message history.  The newest record is always retained.
            if len(existing) >= _PENDING_CACHE_MAX_RECORDS:
                compact = existing[-(_PENDING_CACHE_MAX_RECORDS - 1) :] + [line + "\n"]
                tmp = path.with_suffix(".jsonl.tmp")
                tmp.write_text("".join(compact), encoding="utf-8")
                os.replace(tmp, path)
        return True
    except Exception as exc:
        logger.warning("Unable to persist pending message cache record: %s", exc)
        return False


def load_pending_message_cache_records(scope_id: str) -> list[dict]:
    """Read the durable retry spool for one group/private scope."""
    try:
        path = _pending_cache_path(scope_id, create=False)
    except ValueError:
        return []
    if not path.is_file():
        return []
    records: list[dict] = []
    try:
        with _CACHE_LOCK, path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if len(records) >= _PENDING_CACHE_MAX_RECORDS:
                    break
                try:
                    value = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except (OSError, UnicodeError):
        return []
    return records


def remove_pending_message_cache_records(scope_id: str, records: list[dict] | None = None) -> bool:
    """Remove successfully replayed records from the durable retry spool."""
    try:
        path = _pending_cache_path(scope_id, create=False)
    except ValueError:
        return False
    if not path.is_file():
        return True
    if not records:
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            return False
    successful = {
        str(item.get("message_id", "") or "").strip()
        or str(item.get("dedupe_key", "") or "").strip()
        for item in records
        if isinstance(item, dict)
    }
    successful.discard("")
    remaining: list[dict] = []
    for item in load_pending_message_cache_records(scope_id):
        key = (
            str(item.get("message_id", "") or "").strip()
            or str(item.get("dedupe_key", "") or "").strip()
        )
        if not key or key not in successful:
            remaining.append(item)
    try:
        with _CACHE_LOCK:
            if not remaining:
                path.unlink(missing_ok=True)
            else:
                tmp = path.with_suffix(".jsonl.tmp")
                tmp.write_text(
                    "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str) + "\n" for item in remaining),
                    encoding="utf-8",
                )
                os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        return False


def get_message_media_dir(create: bool = True) -> Path:
    """Return the root used for locally archived group-image files."""
    media_dir = Path(get_plugin_data_path()) / _IMAGE_ARCHIVE_DIRNAME
    if create:
        media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir


def _get_group_cache_dir(group_id: str, create: bool = True) -> Path:
    safe_group_id = _safe_identifier(group_id, "group_id")
    group_dir = get_message_cache_dir(create=create) / safe_group_id
    if create:
        group_dir.mkdir(parents=True, exist_ok=True)
    return group_dir


def _get_group_media_dir(group_id: str, create: bool = True) -> Path:
    safe_group_id = _safe_identifier(group_id, "group_id")
    group_dir = get_message_media_dir(create=create) / safe_group_id
    if create:
        group_dir.mkdir(parents=True, exist_ok=True)
    return group_dir


def _get_private_cache_dir(user_id: str, create: bool = True) -> Path:
    """Return the local cache directory for one private-chat peer."""
    safe_user_id = _safe_identifier(user_id, "user_id")
    private_dir = get_message_cache_dir(create=create) / "private" / safe_user_id
    if create:
        private_dir.mkdir(parents=True, exist_ok=True)
    return private_dir


def _get_private_media_dir(user_id: str, create: bool = True) -> Path:
    safe_user_id = _safe_identifier(user_id, "user_id")
    private_dir = get_message_media_dir(create=create) / "private" / safe_user_id
    if create:
        private_dir.mkdir(parents=True, exist_ok=True)
    return private_dir


def extract_image_sources(record: dict | None) -> list[str]:
    """Extract safe, downloadable image sources from a normalized record.

    Only HTTP(S) URLs and OneBot ``base64://`` payloads are accepted.  A group
    message must never be allowed to make the plugin read an arbitrary local
    filesystem path.
    """
    if not isinstance(record, dict):
        return []
    segments = record.get("segments", [])
    if not isinstance(segments, list):
        return []

    sources: list[str] = []
    seen: set[str] = set()
    for segment in segments:
        if not isinstance(segment, dict) or str(segment.get("type", "")).lower() != "image":
            continue
        data = segment.get("data", {})
        if not isinstance(data, dict):
            continue
        for key in _IMAGE_SOURCE_KEYS:
            source = str(data.get(key, "") or "").strip()
            if not source.startswith(("https://", "http://", "base64://")):
                continue
            if source not in seen:
                seen.add(source)
                sources.append(source)
    return sources


def _looks_like_image_bytes(content_type: str, data: bytes) -> bool:
    normalized_type = str(content_type or "").split(";", 1)[0].strip().lower()
    return (
        data.startswith((b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"\xff\xd8\xff", b"BM"))
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        or (normalized_type.startswith("image/") and bool(data))
    )


def _looks_like_complete_image_bytes(content_type: str, data: bytes) -> bool:
    """Reject CDN responses that contain only an image header or first chunk.

    QQ image endpoints occasionally return a short 200/206 response while the
    transfer is still incomplete. A signature check alone accepts those files
    and the web UI can only render their top strip. The terminal markers below
    are dependency-free so the Windows plugin does not need PIL.
    Very small signature-only payloads remain accepted for compatibility with
    OneBot base64 payloads and tiny adapter-generated images.
    """
    if not _looks_like_image_bytes(content_type, data):
        return False
    if len(data) <= 64:
        return True
    normalized_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if data.startswith(b"\x89PNG\r\n\x1a\n") or normalized_type == "image/png":
        # A PNG IEND chunk is 12 bytes: length, type, CRC. Allow harmless
        # trailing bytes from a proxy, but require the complete chunk.
        marker = data.rfind(b"IEND")
        return marker >= 4 and marker + 8 <= len(data)
    if data.startswith((b"GIF87a", b"GIF89a")) or normalized_type == "image/gif":
        return data.rfind(b"\x3b") >= max(0, len(data) - 64)
    if data.startswith(b"\xff\xd8\xff") or normalized_type in {"image/jpeg", "image/jpg"}:
        return data.rfind(b"\xff\xd9") >= max(0, len(data) - 64)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        if len(data) < 12:
            return False
        declared_size = int.from_bytes(data[4:8], "little", signed=False)
        return declared_size + 8 <= len(data)
    if data.startswith(b"BM") or normalized_type == "image/bmp":
        if len(data) < 6:
            return False
        declared_size = int.from_bytes(data[2:6], "little", signed=False)
        return declared_size == 0 or declared_size <= len(data)
    return False


def _image_suffix(source: str, content_type: str, data: bytes) -> str:
    normalized_type = str(content_type or "").split(";", 1)[0].strip().lower()
    by_type = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }
    if normalized_type in by_type:
        return by_type[normalized_type]
    for signature, suffix in (
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"GIF87a", ".gif"),
        (b"GIF89a", ".gif"),
        (b"\xff\xd8\xff", ".jpg"),
        (b"BM", ".bmp"),
    ):
        if data.startswith(signature):
            return suffix
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    suffix = Path(source.split("?", 1)[0]).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"} else ".img"


async def _read_archive_image_source(source: str, session, max_bytes: int) -> tuple[bytes, str]:
    """Read one permitted image source and retry incomplete CDN responses."""
    try:
        if source.startswith("base64://"):
            data = await asyncio.to_thread(
                base64.b64decode, source[len("base64://"):], validate=True
            )
            return (data, "") if len(data) <= max_bytes else (b"", "")
        if source.startswith(("http://", "https://")) and session is not None:
            # The first request keeps the normal fast path. Subsequent
            # requests bypass an intermediary's cached short response and ask
            # for the complete byte range explicitly.
            for attempt in range(3):
                assembled = b""
                content_type = ""
                # Some QQ/CDN proxies cap a response at 32 KiB but advertise
                # it as a successful response. Follow Content-Range (and, if
                # present, the total size) so the archive receives the rest.
                for _part in range(16):
                    headers = {
                        "Accept": "image/*,*/*;q=0.8",
                        "Cache-Control": "no-cache",
                    }
                    if assembled or attempt:
                        headers["Range"] = f"bytes={len(assembled)}-"
                    async with session.get(source, headers=headers, allow_redirects=True) as response:
                        if response.status not in {200, 206}:
                            break
                        try:
                            content_length = int(response.headers.get("Content-Length", 0) or 0)
                        except (TypeError, ValueError):
                            content_length = 0
                        if content_length and len(assembled) + content_length > max_bytes:
                            return b"", ""
                        body = await response.content.read(max_bytes - len(assembled) + 1)
                        if len(assembled) + len(body) > max_bytes:
                            return b"", ""
                        content_type = str(response.headers.get("Content-Type", "")) or content_type
                        if response.status == 200:
                            if assembled and body.startswith(assembled):
                                assembled = body
                            elif assembled:
                                # The endpoint ignored the range request and
                                # returned a different full document. Do not
                                # concatenate duplicate/corrupt bytes.
                                break
                            else:
                                assembled = body
                            break
                        content_range = str(response.headers.get("Content-Range", ""))
                        match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range)
                        if match and int(match.group(1)) != len(assembled):
                            break
                        assembled += body
                        if _looks_like_complete_image_bytes(content_type, assembled):
                            return assembled, content_type
                        if not body:
                            break
                        if match and match.group(3) != "*" and len(assembled) >= int(match.group(3)):
                            break
                        if not match:
                            # Without a range declaration a second request
                            # cannot be safely joined to this response.
                            break
                if _looks_like_complete_image_bytes(content_type, assembled):
                    return assembled, content_type
                await asyncio.sleep(0.15 * (attempt + 1))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("Group image archive download skipped: %s", type(exc).__name__)
    return b"", ""


def _write_archived_image(destination: Path, data: bytes) -> bool:
    """Atomically persist a downloaded image without exposing partial files."""
    if destination.is_file():
        try:
            existing = destination.read_bytes()
            existing_type = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
            }.get(destination.suffix.lower(), "")
            if _looks_like_complete_image_bytes(existing_type, existing):
                return True
            destination.unlink()
        except OSError:
            return False
    temporary_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
        return True
    except OSError as exc:
        logger.warning("Unable to archive group image %s: %s", destination, exc)
        return False
    finally:
        if temporary_path and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _write_archived_image_once(
    destination: Path,
    data: bytes,
    digest: str,
) -> bool:
    """Write an image only once per content digest in a user/day folder."""

    try:
        with _CACHE_LOCK:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Different QQ accounts can expose different message IDs for the
            # same image.  The message cache dedupe happens asynchronously,
            # so both image workers may still reach this point; serialize the
            # digest check with the write to avoid two physical copies.
            for existing in destination.parent.glob(f"*_{digest}.*"):
                if existing.is_file():
                    return False
            return _write_archived_image(destination, data)
    except OSError as exc:
        logger.warning("Unable to check duplicate group image %s: %s", destination, exc)
        return False


async def archive_group_message_images(
    group_id: str,
    user_id: str,
    timestamp: int,
    message_id: object,
    sources: list[str],
    *,
    max_image_bytes: int = 20 * 1024 * 1024,
) -> int:
    """Archive received images as ``group/date/user/image`` files.

    This coroutine is only called from background workers, so image transfer
    cannot delay a command, automatic reply, or moderation action.
    """
    try:
        safe_group_id = _safe_identifier(group_id, "group_id")
        safe_user_id = _safe_identifier(user_id, "user_id")
    except ValueError:
        return 0
    try:
        event_timestamp = int(timestamp)
    except (TypeError, ValueError):
        event_timestamp = int(time.time())
    if event_timestamp <= 0:
        event_timestamp = int(time.time())
    try:
        max_bytes = max(1024 * 1024, min(int(max_image_bytes), _MAX_ARCHIVE_IMAGE_BYTES))
    except (TypeError, ValueError):
        max_bytes = 20 * 1024 * 1024
    valid_sources = [
        str(source).strip()
        for source in sources
        if str(source or "").strip().startswith(("http://", "https://", "base64://"))
    ]
    if not valid_sources:
        return 0

    is_private = safe_group_id.startswith("private_")
    target_dir = (
        _get_private_media_dir(safe_user_id)
        if is_private
        else _get_group_media_dir(safe_group_id)
    ) / _date_key(event_timestamp)
    if not is_private:
        target_dir = target_dir / safe_user_id
    safe_message_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(message_id or "")).strip("_")
    file_prefix = safe_message_id or str(event_timestamp)
    session = None
    if any(source.startswith(("http://", "https://")) for source in valid_sources):
        try:
            import aiohttp

            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20, connect=5, sock_read=15),
                connector=aiohttp.TCPConnector(limit=1),
            )
        except Exception as exc:
            logger.warning("Unable to start group image archive session: %s", exc)
            return 0

    saved = 0
    try:
        for index, source in enumerate(valid_sources, start=1):
            data, content_type = await _read_archive_image_source(source, session, max_bytes)
            if not data or not _looks_like_complete_image_bytes(content_type, data):
                continue
            digest = hashlib.sha256(data).hexdigest()[:12]
            suffix = _image_suffix(source, content_type, data)
            destination = target_dir / f"{file_prefix}_{index:02d}_{digest}{suffix}"
            if await asyncio.to_thread(
                _write_archived_image_once, destination, data, digest
            ):
                saved += 1
    finally:
        if session is not None:
            await session.close()
    return saved


def get_archived_message_images(
    group_id: str,
    user_id: str,
    timestamp: object,
    message_id: object,
    *,
    max_items: int = 1,
    max_bytes: int = 256 * 1024,
) -> list[dict[str, str]]:
    """Read only this message's bounded local image archive for web replay.

    QQ media URLs expire.  The Windows archive is the durable source, so a
    later incremental upload can carry a small data URL and the Linux page
    remains viewable after the original CDN link has gone stale.
    """
    try:
        safe_group = _safe_identifier(group_id, "group_id")
        safe_user = _safe_identifier(user_id, "user_id")
        event_timestamp = int(timestamp or 0)
    except (TypeError, ValueError, OverflowError):
        return []
    if event_timestamp <= 0 or not str(message_id or "").strip():
        return []
    try:
        limit = max(1, min(8, int(max_items)))
        byte_limit = max(16 * 1024, min(1024 * 1024, int(max_bytes)))
    except (TypeError, ValueError, OverflowError):
        limit, byte_limit = 1, 256 * 1024
    day_dir = _get_group_media_dir(safe_group, create=False) / _date_key(event_timestamp) / safe_user
    if not day_dir.is_dir():
        return []
    safe_message_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(message_id)).strip("_")
    if not safe_message_id:
        return []
    result: list[dict[str, str]] = []
    for path in sorted(day_dir.glob(f"{safe_message_id}_*"))[:limit]:
        try:
            if not path.is_file() or path.stat().st_size > byte_limit:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if not raw or len(raw) > byte_limit:
            continue
        suffix = path.suffix.lower()
        mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".gif": "image/gif", ".bmp": "image/bmp", ".png": "image/png",
        }.get(suffix, "image/png")
        if not _looks_like_complete_image_bytes(mime, raw):
            continue
        result.append(
            {
                "kind": "image",
                "url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
                "name": path.name[:160],
            }
        )
    return result


def cleanup_expired_message_media(group_id: str, retention_days: int) -> int:
    """Delete image archive day folders using the message-history retention."""
    if retention_days <= 0:
        return 0
    try:
        group_dir = _get_group_media_dir(group_id, create=False)
    except ValueError:
        return 0
    if not group_dir.exists():
        return 0
    cutoff = datetime.now().date() - timedelta(days=max(0, retention_days - 1))
    deleted_days = 0
    for day_dir in list(group_dir.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                shutil.rmtree(day_dir)
                deleted_days += 1
            except OSError as exc:
                logger.warning("Unable to remove expired group image archive %s: %s", day_dir, exc)
    try:
        group_dir.rmdir()
    except OSError:
        pass
    return deleted_days


def remove_group_message_media(group_id: str) -> bool:
    """Remove archived group images during explicit group-data deletion."""
    try:
        group_dir = _get_group_media_dir(group_id, create=False)
    except ValueError:
        return False
    try:
        if group_dir.exists():
            shutil.rmtree(group_dir)
        return True
    except OSError as exc:
        logger.warning("Unable to remove group image archive %s: %s", group_id, exc)
        return False


def get_group_message_cache_generation(group_id: str) -> int:
    """Return the current in-process generation for a group's message cache."""
    try:
        safe_group_id = _safe_identifier(group_id, "group_id")
    except ValueError:
        return -1
    with _CACHE_LOCK:
        return int(_CACHE_GENERATIONS.get(safe_group_id, 0))


def _atomic_write_json(path: Path, data: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(data, temp_file, ensure_ascii=False, separators=(",", ":"))
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    except Exception:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def _load_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as file:
            records = json.load(file)
        if isinstance(records, list):
            return [record for record in records if isinstance(record, dict)]
    except Exception as exc:
        logger.warning("Unable to read message cache file %s: %s", path, exc)
    return []


def _event_value(event: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(event, name, default)
    except Exception:
        return default


def _object_value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    # AstrBot components are Pydantic models, so their declared fields are
    # normally present in ``__dict__``.  Prefer those values so auditing a
    # message never invokes a computed property such as ``File.file`` (which
    # can attempt to resolve or download an attachment).
    try:
        instance_values = vars(obj)
    except TypeError:
        instance_values = {}
    if isinstance(instance_values, dict) and name in instance_values:
        return instance_values[name]
    try:
        descriptor = getattr(type(obj), name, None)
    except Exception:
        descriptor = None
    if isinstance(descriptor, property):
        return default
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _segment_reference(data: dict, *field_names: str) -> str:
    """Return a short useful identifier without exposing an unbounded URL."""
    for field_name in field_names:
        value = data.get(field_name)
        if value in (None, "", 0, "0"):
            continue
        reference = _limited_text(value, 160).strip()
        if reference.startswith(("http://", "https://")):
            reference = reference.rsplit("/", 1)[-1].split("?", 1)[0] or "链接"
        return reference
    return ""


def _onebot_segment_summary(segment_type: str, data: dict) -> str:
    if segment_type == "text":
        return _limited_text(data.get("text", ""))
    if segment_type in {"image", "flashimage", "flash_image"}:
        reference = _segment_reference(data, "file_id", "file", "file_", "image_id", "url")
        return f"[图片: {reference}]" if reference else "[图片]"
    if segment_type in {"face", "mface", "sface", "marketface", "emoji", "emotion"}:
        face_id = (
            data.get("faceText")
            or data.get("face_text")
            or data.get("name")
            or data.get("summary")
            or data.get("id")
            or data.get("face_id")
            or data.get("emoji_id")
        )
        return f"[表情: {face_id}]" if face_id else "[表情]"
    if segment_type in {"at", "at_all", "atall"}:
        target = data.get("qq") or data.get("user_id") or "all"
        return f"[@{target}]"
    if segment_type == "reply":
        reply_id = data.get("id") or data.get("message_id")
        reply_sender = data.get("qq") or data.get("user_id") or data.get("sender_id")
        reply_text = _limited_text(data.get("text") or data.get("message_str") or "", 80).strip()
        details = " ".join(str(item) for item in (reply_sender, reply_text) if item)
        if reply_id and details:
            return f"[回复 {reply_id}: {details}]"
        if reply_id:
            return f"[回复: {reply_id}]"
        return f"[回复: {details}]" if details else "[回复]"
    if segment_type in {"json", "xml"}:
        return "[卡片]"
    if segment_type == "markdown":
        return "[Markdown]"
    if segment_type == "record":
        reference = _segment_reference(data, "file", "file_id", "url")
        return f"[语音: {reference}]" if reference else "[语音]"
    if segment_type == "video":
        reference = _segment_reference(data, "file", "file_id", "cover", "url")
        return f"[视频: {reference}]" if reference else "[视频]"
    if segment_type == "file":
        name = (
            data.get("file_name")
            or data.get("name")
            or data.get("file")
            or data.get("file_")
            or data.get("file_id")
            or data.get("url")
        )
        return f"[文件: {name}]" if name else "[文件]"
    if segment_type == "node":
        sender_name = _limited_text(
            data.get("nickname") or data.get("name") or data.get("sender_name") or "",
            80,
        ).strip()
        sender_id = _limited_text(data.get("user_id") or data.get("uin") or "", 64).strip()
        if sender_id == "0":
            sender_id = ""
        sender = " ".join(item for item in (sender_name, sender_id) if item)
        if sender:
            return f"[转发消息: {sender}]"
    if segment_type in {"forward", "node"}:
        reference = _segment_reference(data, "id", "resid", "file")
        return f"[合并转发: {reference}]" if reference else "[合并转发]"
    if segment_type == "contact":
        return "[名片]"
    if segment_type == "share":
        title = _segment_reference(data, "title", "content", "url")
        return f"[分享: {title}]" if title else "[分享]"
    if segment_type == "music":
        title = _segment_reference(data, "title", "content", "id")
        return f"[音乐: {title}]" if title else "[音乐]"
    if segment_type == "location":
        location = _segment_reference(data, "title", "content", "address")
        if location:
            return f"[位置: {location}]"
        coordinates = [
            _limited_text(value, 64).strip()
            for value in (data.get("latitude"), data.get("lat"), data.get("longitude"), data.get("lon"))
            if value not in (None, "")
        ]
        return f"[位置: {', '.join(coordinates)}]" if coordinates else "[位置]"
    if segment_type in {"dice", "rps", "poke", "shake"}:
        return f"[{segment_type}]"
    return f"[{segment_type or '未知消息'}]"


def _normalize_onebot_segment(segment: Any, depth: int = 0) -> dict:
    if not isinstance(segment, dict):
        text = _limited_text(segment)
        return {"type": "unknown", "data": {"value": text}, "summary": "[未知消息]"}

    segment_type = _limited_text(segment.get("type", "unknown"), 64).lower() or "unknown"
    raw_data = segment.get("data", {})
    data = raw_data if isinstance(raw_data, dict) else {"value": raw_data}
    nested_field_names = ("chain", "nodes", "message", "messages", "content")
    nested_values = {
        field_name: data[field_name]
        for field_name in nested_field_names
        if field_name in data
    }
    normalized_data = _safe_json_value(
        {
            field_name: value
            for field_name, value in data.items()
            if field_name not in nested_values
        }
    )
    if not isinstance(normalized_data, dict):
        normalized_data = {"value": normalized_data}
    # Forwarded nodes and reply chains carry nested OneBot segments.  Treat
    # them as actual segments so rich content is retained and truncation is
    # explicit instead of silently falling under the generic list limit.
    for field_name, nested_value in nested_values.items():
        if isinstance(nested_value, (list, tuple)):
            normalized_data[field_name] = _normalize_component_sequence(
                nested_value,
                depth,
            )
        else:
            normalized_data[field_name] = _safe_json_value(nested_value)
    return {
        "type": segment_type,
        "data": normalized_data,
        "summary": _onebot_segment_summary(segment_type, normalized_data),
    }


def _parse_cq_message(raw_message: str) -> list[dict]:
    segments: list[dict] = []
    last_index = 0
    for match in _CQ_PATTERN.finditer(raw_message):
        plain_text = raw_message[last_index:match.start()]
        if plain_text:
            segments.append(
                {
                    "type": "text",
                    "data": {"text": _limited_text(unescape(plain_text))},
                    "summary": _limited_text(unescape(plain_text)),
                }
            )
        data: dict[str, Any] = {}
        fields = match.group(2) or ""
        for field_match in _CQ_FIELD_PATTERN.finditer(fields):
            key = unescape(field_match.group(1)).strip()
            if key:
                data[key] = _limited_text(unescape(field_match.group(2)))
        segment_type = match.group(1).lower()
        segments.append(
            {
                "type": segment_type,
                "data": data,
                "summary": _onebot_segment_summary(segment_type, data),
            }
        )
        last_index = match.end()
    trailing_text = raw_message[last_index:]
    if trailing_text:
        segments.append(
            {
                "type": "text",
                "data": {"text": _limited_text(unescape(trailing_text))},
                "summary": _limited_text(unescape(trailing_text)),
            }
        )
    return segments


def _limit_segments(segments: list[dict], omitted_count: int = 0) -> list[dict]:
    """Bound a segment list while making omitted content visible to auditors."""
    if omitted_count <= 0 and len(segments) <= _MAX_SEGMENTS:
        return segments
    if omitted_count <= 0:
        omitted_count = len(segments) - _MAX_SEGMENTS + 1
    marker = {
        "type": "truncated",
        "data": {"omitted_count": omitted_count},
        "summary": f"[其余 {omitted_count} 个消息段已省略]",
        "truncated": True,
    }
    if _MAX_SEGMENTS <= 1:
        return [marker]
    return segments[: _MAX_SEGMENTS - 1] + [marker]


def _normalize_component_sequence(value: Any, depth: int) -> Any:
    """Preserve Reply/Node message chains without recursively storing objects."""
    if not isinstance(value, (list, tuple)):
        return _safe_json_value(value)
    if depth >= 3:
        return _limit_segments([], omitted_count=len(value))

    segments: list[dict] = []
    item_limit = _MAX_SEGMENTS if len(value) <= _MAX_SEGMENTS else _MAX_SEGMENTS - 1
    for item in value[:item_limit]:
        if isinstance(item, dict) and "type" in item:
            segments.append(_normalize_onebot_segment(item, depth + 1))
        elif isinstance(item, (str, int, float, bool)) or item is None:
            segments.append(
                {
                    "type": "text",
                    "data": {"text": _limited_text(item)},
                    "summary": _limited_text(item),
                }
            )
        else:
            segments.append(_normalize_component(item, depth + 1))
    return _limit_segments(segments, omitted_count=max(0, len(value) - len(segments)))


def _normalize_component(component: Any, depth: int = 0) -> dict:
    """Normalize an AstrBot component when OneBot raw segments are unavailable."""
    component_type = type(component).__name__.lower() or "unknown"
    declared_type = _object_value(component, "type")
    if hasattr(declared_type, "value"):
        declared_type = declared_type.value
    if isinstance(declared_type, str) and declared_type.strip():
        component_type = declared_type.strip().lower()
    data: dict[str, Any] = {}
    for field_name in _COMPONENT_FIELD_NAMES:
        value = _object_value(component, field_name)
        if value in (None, ""):
            continue
        if field_name in {"chain", "nodes", "message", "messages", "content"}:
            data[field_name] = _normalize_component_sequence(value, depth)
        else:
            data[field_name] = _safe_json_value(value)

    # Preserve adapter-specific scalar fields as well.  ``vars`` is used on
    # purpose: reading arbitrary attributes with ``getattr`` may invoke a
    # computed downloader (notably ``File.file``).  Limit the number and size
    # of extras so one malformed component cannot inflate a history record.
    try:
        instance_values = vars(component)
    except TypeError:
        instance_values = {}
    if isinstance(instance_values, dict):
        known_fields = set(_COMPONENT_FIELD_NAMES)
        extra_count = 0
        for field_name, value in instance_values.items():
            if (
                extra_count >= 24
                or not isinstance(field_name, str)
                or field_name in known_fields
                or field_name.startswith("_")
                or value in (None, "")
            ):
                continue
            if field_name in {"chain", "nodes", "message", "messages", "content"}:
                data[field_name] = _normalize_component_sequence(value, depth)
            else:
                data[field_name] = _safe_json_value(value)
            extra_count += 1

    aliases = {
        "plain": "text",
        "plaintext": "text",
        "image": "image",
        "face": "face",
        "at": "at",
        "reply": "reply",
        "record": "record",
        "video": "video",
        "file": "file",
        "forward": "forward",
        "node": "node",
        "nodes": "forward",
        "music": "music",
        "share": "share",
        "location": "location",
        "poke": "poke",
        "markdown": "markdown",
        "json": "json",
        "xml": "xml",
        # Subclasses such as AtAll still declare their wire type as ``At``.
        # These aliases also cover common adapter-specific rich emoji/image
        # component names when a OneBot raw segment is unavailable.
        "atall": "at",
        "at_all": "at",
        "mface": "face",
        "sface": "face",
        "marketface": "face",
        "emoji": "face",
        "emotion": "face",
        "flashimage": "image",
        "flash_image": "image",
    }
    segment_type = aliases.get(component_type, component_type)
    return {
        "type": segment_type,
        "data": data,
        "summary": _onebot_segment_summary(segment_type, data),
    }


def _enrich_raw_reply_segments(event: Any, segments: list[dict]) -> list[dict]:
    """Merge AstrBot Reply chains into sparse OneBot reply segments when present."""
    try:
        components = event.get_messages()
    except Exception:
        return segments
    if not isinstance(components, (list, tuple)):
        return segments

    reply_components = [
        component
        for component in components
        if type(component).__name__.lower() == "reply"
    ]
    if not reply_components:
        return segments

    remaining_components = list(reply_components)
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("type") != "reply":
            continue
        data = segment.get("data", {})
        data = dict(data) if isinstance(data, dict) else {}
        reply_id = str(data.get("id") or data.get("message_id") or "")
        component = None
        for candidate in remaining_components:
            candidate_id = str(
                _object_value(candidate, "id") or _object_value(candidate, "message_id") or ""
            )
            if reply_id and candidate_id == reply_id:
                component = candidate
                break
        if component is None and len(remaining_components) == 1:
            component = remaining_components[0]
        if component is None:
            continue

        remaining_components.remove(component)
        normalized_reply = _normalize_component(component)
        normalized_data = normalized_reply.get("data", {})
        if isinstance(normalized_data, dict):
            data.update(normalized_data)
        if reply_id:
            data.setdefault("id", reply_id)
        segment["data"] = data
        segment["summary"] = _onebot_segment_summary("reply", data)
    return segments


def _event_segments(event: Any, raw_event: Any, plain_text: str) -> list[dict]:
    if isinstance(raw_event, dict):
        raw_segments = raw_event.get("message")
        if isinstance(raw_segments, list) and raw_segments:
            segment_limit = _MAX_SEGMENTS if len(raw_segments) <= _MAX_SEGMENTS else _MAX_SEGMENTS - 1
            normalized_segments = [
                _normalize_onebot_segment(segment)
                for segment in raw_segments[:segment_limit]
            ]
            return _enrich_raw_reply_segments(
                event,
                _limit_segments(
                    normalized_segments,
                    omitted_count=max(0, len(raw_segments) - len(normalized_segments)),
                ),
            )
        raw_message = raw_event.get("raw_message") or raw_segments
        if isinstance(raw_message, str) and raw_message:
            parsed = _parse_cq_message(_limited_text(raw_message, _MAX_RAW_MESSAGE_LENGTH))
            if parsed:
                return _limit_segments(parsed)

    try:
        components = event.get_messages()
    except Exception:
        components = []
    if isinstance(components, (list, tuple)) and components:
        component_limit = _MAX_SEGMENTS if len(components) <= _MAX_SEGMENTS else _MAX_SEGMENTS - 1
        normalized_components = [
            _normalize_component(component)
            for component in components[:component_limit]
        ]
        return _limit_segments(
            normalized_components,
            omitted_count=max(0, len(components) - len(normalized_components)),
        )
    if plain_text:
        return [{"type": "text", "data": {"text": plain_text}, "summary": plain_text}]
    return []


def _mirror_segment_fingerprint(segments: list[dict]) -> list[dict[str, str]]:
    """Normalize message segments for cross-account mirror detection.

    QQ image URLs often contain adapter-specific hosts, signatures, or query
    parameters.  Those values identify a download location, not a different
    user message, so they must not split the mirror fingerprint.
    """

    fingerprint: list[dict[str, str]] = []
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            continue
        segment_type = str(segment.get("type", "") or "").strip().lower()
        data = segment.get("data", {})
        data = data if isinstance(data, dict) else {}
        if segment_type in {"image", "flashimage", "flash_image"}:
            identity = (
                data.get("file_id")
                or data.get("file")
                or data.get("file_")
                or data.get("image_id")
                or data.get("id")
                or data.get("url")
                or data.get("image_url")
                or "image"
            )
            identity = str(identity or "").split("?", 1)[0].split("#", 1)[0]
            fingerprint.append({"type": "image", "identity": identity[:256]})
            continue
        if segment_type == "text":
            fingerprint.append(
                {"type": "text", "identity": str(data.get("text", "") or "")[:4096]}
            )
            continue
        stable_fields = []
        for key in ("id", "qq", "user_id", "file_id", "file", "file_", "name", "summary"):
            value = data.get(key)
            if value not in (None, ""):
                stable_fields.append(f"{key}={str(value)[:256]}")
        fingerprint.append(
            {
                "type": segment_type,
                "identity": "|".join(stable_fields)[:512],
            }
        )
    return fingerprint


def _event_timestamp(raw_event: Any, message_obj: Any) -> int:
    candidates = []
    if isinstance(raw_event, dict):
        candidates.append(raw_event.get("time"))
    candidates.extend((_object_value(message_obj, "time"), _object_value(message_obj, "timestamp")))
    for value in candidates:
        try:
            timestamp = int(value)
            if timestamp > 0:
                return timestamp
        except (TypeError, ValueError):
            continue
    return int(time.time())


def _event_source_account_id(event: Any, raw_event: dict[str, Any]) -> str:
    """Return the QQ account that received an event, when the adapter exposes it."""

    candidates: list[Any] = []
    if isinstance(raw_event, dict):
        candidates.extend(
            (
                raw_event.get("self_id"),
                raw_event.get("selfId"),
                raw_event.get("bot_id"),
                raw_event.get("botId"),
            )
        )
    getter = getattr(event, "get_self_id", None)
    if callable(getter):
        try:
            candidates.append(getter())
        except Exception:
            pass
    candidates.extend(
        (
            _event_value(event, "self_id", ""),
            _event_value(event, "selfId", ""),
        )
    )
    for value in candidates:
        text = str(value or "").strip()
        if re.fullmatch(r"\d{5,20}", text):
            return text
    return ""


def build_message_record(group_id: str, user_id: str, event: Any, settings: dict | None = None) -> dict:
    """Create a JSON-safe message record from AstrBot and OneBot event data."""
    normalized_settings = normalize_message_cache_settings(settings)
    message_obj = _event_value(event, "message_obj")
    raw_event = _object_value(message_obj, "raw_message")
    raw_event = raw_event if isinstance(raw_event, dict) else {}
    sender = _object_value(message_obj, "sender")
    raw_sender = raw_event.get("sender", {})
    raw_sender = raw_sender if isinstance(raw_sender, dict) else {}

    plain_text = _limited_text(
        _event_value(event, "message_str", "") or _object_value(message_obj, "message_str", "")
    )
    timestamp = _event_timestamp(raw_event, message_obj)
    message_id = raw_event.get("message_id") or _object_value(message_obj, "message_id") or ""
    nickname = (
        raw_sender.get("nickname")
        or _object_value(sender, "nickname")
        or _object_value(message_obj, "sender_nickname")
        or _object_value(message_obj, "sender_name")
        or ""
    )
    card = raw_sender.get("card") or _object_value(sender, "card") or ""
    raw_message = raw_event.get("raw_message") or raw_event.get("message", "")
    if not isinstance(raw_message, str):
        raw_message = ""

    sender_user_id = (
        raw_sender.get("user_id")
        or _object_value(sender, "user_id")
        or _object_value(message_obj, "sender_id")
        or user_id
    )
    sender_record = {
        "user_id": _limited_text(sender_user_id, 128),
        "nickname": _limited_text(nickname, 256),
        "card": _limited_text(card, 256),
        "role": _limited_text(raw_sender.get("role") or _object_value(sender, "role"), 64),
    }
    # Adapters differ in which member metadata they expose.  Keep the common
    # bounded fields so an audit can identify the sender without retaining an
    # unbounded adapter object.
    for sender_field in (
        "sex",
        "age",
        "area",
        "level",
        "title",
        "permission",
        "join_time",
        "last_sent_time",
        "unfriendly",
    ):
        value = raw_sender.get(sender_field, _object_value(sender, sender_field))
        if value not in (None, ""):
            sender_record[sender_field] = _safe_json_value(value, 256)

    record = {
        "message_id": _limited_text(message_id, 128),
        "timestamp": timestamp,
        "datetime": datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S"),
        "group_id": str(group_id),
        "user_id": str(user_id),
        "nickname": _limited_text(nickname, 256),
        "sender": sender_record,
        "text": plain_text,
        "segments": _event_segments(event, raw_event, plain_text),
    }
    source_account_id = _event_source_account_id(event, raw_event)
    mirror_payload = {
        "scope": "group",
        "group_id": str(group_id),
        "user_id": str(user_id),
        "timestamp": int(timestamp),
        "text": plain_text,
        "segments": _mirror_segment_fingerprint(record["segments"]),
    }
    record["source_account_id"] = source_account_id
    record["mirror_key"] = hashlib.sha256(
        json.dumps(
            mirror_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    # Multiple connected QQ accounts can receive the same message through
    # different OneBot adapters, each with its own adapter-local message ID.
    # Merge that mirrored traffic locally before it is uploaded to Linux.
    upstream_sequence = (
        raw_event.get("message_seq")
        or raw_event.get("message_seq_id")
        or raw_event.get("real_id")
        or raw_event.get("msg_seq")
        or ""
    )
    dedupe_payload = {
        "scope": "group",
        "group_id": str(group_id),
        "user_id": str(user_id),
        "timestamp": int(timestamp),
        "sequence": str(upstream_sequence)[:128],
        # When an adapter does not expose a message sequence, retain the
        # adapter message id so two distinct same-second messages are not
        # collapsed into one activity record. When a shared sequence exists,
        # leave the local id out so mirrored multi-bot deliveries still merge.
        "message_id": "" if upstream_sequence else str(message_id)[:128],
        "text": plain_text,
        "segments": record["segments"],
    }
    record["dedupe_key"] = hashlib.sha256(
        json.dumps(
            dedupe_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    if normalized_settings["cache_raw_message"] and raw_message:
        record["raw_message"] = _limited_text(raw_message, _MAX_RAW_MESSAGE_LENGTH)
    elif normalized_settings["cache_raw_message"] and isinstance(raw_event.get("message"), list):
        # Some adapters provide only an array of OneBot segments.  Keep a
        # bounded raw copy in addition to the normalized, display-safe list.
        record["raw_segments"] = _safe_json_value(raw_event["message"])
    return record


def build_private_message_record(user_id: str, event: Any, settings: dict | None = None) -> dict:
    """Build a private-chat record in the same normalized format as groups."""
    safe_user_id = _safe_identifier(user_id, "user_id")
    record = build_message_record(f"private_{safe_user_id}", safe_user_id, event, settings)
    record["scope_type"] = "private"
    record["private_user_id"] = safe_user_id
    return record


def build_outbound_group_message_record(
    group_id: str,
    message_id: str | int,
    message: Any,
    *,
    sender_id: str = "bot",
    sender_name: str = "机器人",
    timestamp: int | None = None,
) -> dict:
    """Build a locally-stored record for a message sent by the bot.

    Framework-managed replies do not always return the OneBot message id to a
    plugin callback.  A stable local fallback id still lets the portal render
    the reply, while direct OneBot sends retain their real id and can later be
    marked as recalled.  This function deliberately shares the same segment
    normalization as inbound messages so text, images, QQ faces and rich cards
    have one display path in the web console.
    """
    safe_group_id = _safe_identifier(group_id, "group_id")
    safe_sender_id = _safe_identifier(sender_id or "bot", "sender_id")
    current_timestamp = int(timestamp or time.time())
    raw_id = _limited_text(message_id, 128).strip()
    if not raw_id:
        raw_id = f"bot-{current_timestamp}-{time.time_ns() % 1_000_000_000}"

    source = message.chain if hasattr(message, "chain") else message
    segments: list[dict] = []
    if isinstance(source, str):
        segments = _parse_cq_message(_limited_text(source, _MAX_RAW_MESSAGE_LENGTH))
        if not segments and source.strip():
            segments = [{"type": "text", "data": {"text": _limited_text(source)}}]
    elif isinstance(source, (list, tuple)):
        for item in list(source)[:_MAX_SEGMENTS]:
            if isinstance(item, dict):
                segments.append(_normalize_onebot_segment(item))
            else:
                segments.append(_normalize_component(item))
        segments = _limit_segments(segments)
    elif source is not None:
        segments = _limit_segments([_normalize_component(source)])

    text = "".join(
        _limited_text((segment.get("data") or {}).get("text", ""))
        for segment in segments
        if isinstance(segment, dict)
        and str(segment.get("type", "")).lower() == "text"
        and isinstance(segment.get("data"), dict)
    )
    return {
        "message_id": raw_id,
        "timestamp": current_timestamp,
        "datetime": datetime.fromtimestamp(current_timestamp).strftime("%Y-%m-%d %H:%M:%S"),
        "group_id": safe_group_id,
        "user_id": safe_sender_id,
        "user_name": _limited_text(sender_name or "机器人", 80),
        "sender": {
            "user_id": safe_sender_id,
            "nickname": _limited_text(sender_name or "机器人", 80),
            "card": _limited_text(sender_name or "机器人", 80),
            "role": "bot",
        },
        "direction": "outbound",
        "text": text,
        "segments": segments,
    }


def _date_key(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def _iter_user_cache_files(group_dir: Path, user_id: str) -> list[Path]:
    file_name = f"{user_id}.json"
    if not group_dir.exists():
        return []
    files = [
        day_dir / file_name
        for day_dir in group_dir.iterdir()
        if day_dir.is_dir()
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_dir.name)
        and (day_dir / file_name).is_file()
    ]
    return sorted(files, reverse=True)


def _trim_user_records(group_dir: Path, user_id: str, maximum: int) -> None:
    """Keep the newest records for a user across all daily cache files."""
    if maximum <= 0:
        return
    files = _iter_user_cache_files(group_dir, user_id)
    entries: list[tuple[int, int, int, Path, dict]] = []
    records_by_path: dict[Path, list[dict]] = {}
    for path in files:
        records = _load_records(path)
        records_by_path[path] = records
        for index, record in enumerate(records):
            try:
                timestamp = int(record.get("timestamp", 0))
            except (TypeError, ValueError):
                timestamp = 0
            try:
                cached_at = int(record.get("cached_at", 0))
            except (TypeError, ValueError):
                cached_at = 0
            entries.append((timestamp, cached_at, index, path, record))
    if len(entries) <= maximum:
        return

    keep_entries = sorted(entries, key=lambda item: (item[0], item[1], item[2]), reverse=True)[:maximum]
    keep_by_path: dict[Path, set[int]] = {}
    for _, _, index, path, _ in keep_entries:
        keep_by_path.setdefault(path, set()).add(index)

    for path in files:
        records = records_by_path[path]
        kept = [record for index, record in enumerate(records) if index in keep_by_path.get(path, set())]
        if len(kept) == len(records):
            continue
        if kept:
            _atomic_write_json(path, kept)
        else:
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass


def cleanup_expired_message_cache(group_id: str, retention_days: int) -> int:
    """Remove daily cache directories older than the configured retention."""
    if retention_days <= 0:
        return 0
    group_dir = _get_group_cache_dir(group_id, create=False)
    if not group_dir.exists():
        return 0
    cutoff = datetime.now().date() - timedelta(days=max(0, retention_days - 1))
    deleted_days = 0
    with _CACHE_LOCK:
        for day_dir in list(group_dir.iterdir()):
            if not day_dir.is_dir():
                continue
            try:
                day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                shutil.rmtree(day_dir)
                deleted_days += 1
        try:
            group_dir.rmdir()
        except OSError:
            pass
    return deleted_days


def _maybe_cleanup(group_id: str, retention_days: int) -> None:
    if retention_days <= 0:
        return
    now = time.monotonic()
    last_cleanup = _LAST_CLEANUP_AT.get(group_id, 0.0)
    if now - last_cleanup < _CLEANUP_INTERVAL_SECONDS:
        return
    _LAST_CLEANUP_AT[group_id] = now
    try:
        cleanup_expired_message_cache(group_id, retention_days)
    except Exception as exc:
        logger.warning("Unable to clean message cache for group %s: %s", group_id, exc)


def _prune_pending_recall_marks_locked(now: int | None = None) -> None:
    """Bound markers created when a recall wins the async write race."""
    current_time = int(time.time()) if now is None else now
    expired_before = current_time - _RECALL_MARK_TTL_SECONDS
    for key, mark in list(_PENDING_RECALL_MARKS.items()):
        if int(mark.get("pending_at", 0) or 0) < expired_before:
            _PENDING_RECALL_MARKS.pop(key, None)

    overflow = len(_PENDING_RECALL_MARKS) - _MAX_PENDING_RECALL_MARKS
    if overflow > 0:
        oldest = sorted(
            _PENDING_RECALL_MARKS,
            key=lambda key: int(_PENDING_RECALL_MARKS[key].get("pending_at", 0) or 0),
        )[:overflow]
        for key in oldest:
            _PENDING_RECALL_MARKS.pop(key, None)


def _apply_recall_mark(record: dict, mark: dict) -> None:
    """Apply one normalized recall marker to an in-memory cache record."""
    record["recalled"] = True
    record["recalled_at"] = int(mark.get("recalled_at", int(time.time())) or int(time.time()))
    if mark.get("recall_reason"):
        record["recall_reason"] = mark["recall_reason"]
    if mark.get("recall_operator_id"):
        record["recall_operator_id"] = mark["recall_operator_id"]


def cache_group_message_record(record: dict, settings: dict | None = None) -> bool:
    """Persist a prepared message record and enforce its storage limits.

    The receiver builds records on the event loop, while this function is safe
    to run in a worker thread.  Keeping disk IO here avoids blocking message
    dispatch under an active group chat.
    """
    normalized_settings = normalize_message_cache_settings(settings)
    if not normalized_settings["enabled"] or not isinstance(record, dict):
        return False
    expected_generation = record.get("_cache_generation") if isinstance(record, dict) else None
    try:
        safe_group_id = _safe_identifier(record.get("group_id"), "group_id")
        safe_user_id = _safe_identifier(record.get("user_id"), "user_id")
    except ValueError as exc:
        logger.warning("Skipping message cache write: %s", exc)
        return False

    try:
        stored_record = dict(record)
        # Private queue metadata is a write guard, never persisted user data.
        stored_record.pop("_cache_generation", None)
        stored_record["group_id"] = safe_group_id
        stored_record["user_id"] = safe_user_id
        try:
            timestamp = int(stored_record.get("timestamp", 0))
        except (TypeError, ValueError):
            timestamp = 0
        if timestamp <= 0:
            timestamp = int(time.time())
        stored_record["timestamp"] = timestamp
        stored_record.setdefault(
            "datetime", datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        )
        if not normalized_settings["cache_raw_message"]:
            stored_record.pop("raw_message", None)
            stored_record.pop("raw_segments", None)
        stored_record["cached_at"] = time.time_ns()
        message_id = _limited_text(stored_record.get("message_id", ""), 128)
        stored_record["message_id"] = message_id
        dedupe_key = _limited_text(stored_record.get("dedupe_key", ""), 128)
        stored_record["dedupe_key"] = dedupe_key
        stored_record["source_account_id"] = _limited_text(
            stored_record.get("source_account_id", ""), 32
        )
        stored_record["mirror_key"] = _limited_text(
            stored_record.get("mirror_key", ""), 128
        )
        is_private = str(stored_record.get("scope_type", "")).lower() == "private"
        group_dir = (
            _get_private_cache_dir(safe_user_id, create=False)
            if is_private
            else _get_group_cache_dir(safe_group_id, create=False)
        )
        with _CACHE_LOCK:
            if expected_generation is not None:
                try:
                    expected = int(expected_generation)
                except (TypeError, ValueError):
                    return False
                if expected != int(_CACHE_GENERATIONS.get(safe_group_id, 0)):
                    return False
            # A config deletion is also a data deletion for group history. A
            # private chat has no group config and is kept in its own local
            # namespace instead.
            if not is_private and not get_group_config_file(safe_group_id).is_file():
                return False
            group_dir.mkdir(parents=True, exist_ok=True)
            cache_file = group_dir / _date_key(timestamp) / f"{safe_user_id}.json"
            records = _load_records(cache_file)
            _prune_pending_recall_marks_locked()
            existing_record = next(
                (
                    item
                    for item in records
                    if (
                        message_id and str(item.get("message_id", "")) == message_id
                    ) or (
                        dedupe_key and str(item.get("dedupe_key", "")) == dedupe_key
                    )
                ),
                None,
            )
            if existing_record is None and stored_record["mirror_key"] and stored_record["source_account_id"]:
                mirror_key = stored_record["mirror_key"]
                source_account = stored_record["source_account_id"]
                existing_record = next(
                    (
                        item
                        for item in records
                        if str(item.get("mirror_key", "") or "") == mirror_key
                        and str(item.get("source_account_id", "") or "")
                        and str(item.get("source_account_id", "") or "") != source_account
                    ),
                    None,
                )
            if existing_record is not None:
                pending_mark = _PENDING_RECALL_MARKS.pop((safe_group_id, message_id), None)
                if pending_mark:
                    _apply_recall_mark(existing_record, pending_mark)
                    _atomic_write_json(cache_file, records)
                return True
            pending_mark = _PENDING_RECALL_MARKS.pop((safe_group_id, message_id), None) if message_id else None
            if pending_mark:
                _apply_recall_mark(stored_record, pending_mark)
            records.append(stored_record)
            _atomic_write_json(cache_file, records)
            _trim_user_records(group_dir, safe_user_id, normalized_settings["max_messages_per_user"])
            for cache_key in [key for key in _ACTIVITY_CACHE if key[0] == safe_group_id]:
                _ACTIVITY_CACHE.pop(cache_key, None)
        _maybe_cleanup(safe_group_id, normalized_settings["retention_days"])
        return True
    except Exception as exc:
        logger.warning("Unable to cache group message for %s: %s", safe_group_id, exc)
        return False


def cache_group_message(group_id: str, user_id: str, event: Any, settings: dict | None = None) -> bool:
    """Build and persist one received group message.

    Kept as a synchronous compatibility wrapper for manual OneBot-history
    imports.  Live group traffic should use ``cache_group_message_record`` via
    the plugin's queued writer.
    """
    normalized_settings = normalize_message_cache_settings(settings)
    if not normalized_settings["enabled"]:
        return False
    try:
        safe_group_id = _safe_identifier(group_id, "group_id")
        safe_user_id = _safe_identifier(user_id, "user_id")
    except ValueError as exc:
        logger.warning("Skipping message cache write: %s", exc)
        return False
    record = build_message_record(safe_group_id, safe_user_id, event, normalized_settings)
    return cache_group_message_record(record, normalized_settings)


def cache_private_message_record(record: dict, settings: dict | None = None) -> bool:
    """Persist a private message locally without requiring a group config."""
    if not isinstance(record, dict):
        return False
    private_record = dict(record)
    private_record["scope_type"] = "private"
    return cache_group_message_record(private_record, settings)


def cleanup_configured_message_caches(group_settings: dict[str, dict] | None = None) -> dict[str, int]:
    """Clean expired history for every configured group.

    ``group_settings`` deliberately comes from the caller instead of reading
    group configuration here.  That keeps this storage module independent of
    configuration lifecycle decisions and makes the janitor safe for disabled
    groups as well.
    """
    cleaned_groups = 0
    deleted_days = 0
    failed_groups = 0
    for group_id, settings in (group_settings or {}).items():
        normalized_settings = normalize_message_cache_settings(settings)
        retention_days = normalized_settings["retention_days"]
        if retention_days <= 0:
            continue
        try:
            removed = cleanup_expired_message_cache(str(group_id), retention_days)
            removed += cleanup_expired_message_media(str(group_id), retention_days)
            cleaned_groups += 1
            deleted_days += removed
        except Exception as exc:
            failed_groups += 1
            logger.warning("Unable to clean message cache for group %s: %s", group_id, exc)
    return {
        "cleaned_groups": cleaned_groups,
        "deleted_days": deleted_days,
        "failed_groups": failed_groups,
    }


def cache_onebot_message(
    group_id: str,
    user_id: str,
    onebot_message: dict,
    settings: dict | None = None,
) -> bool:
    """Cache a OneBot history record that was fetched for manual recall."""
    if not isinstance(onebot_message, dict):
        return False
    raw_event = dict(onebot_message)
    sender_data = raw_event.get("sender", {})
    sender_data = sender_data if isinstance(sender_data, dict) else {}
    raw_segments = raw_event.get("message", [])
    plain_text = ""
    if isinstance(raw_segments, list):
        plain_text = "".join(
            _limited_text(segment.get("data", {}).get("text", ""))
            for segment in raw_segments
            if isinstance(segment, dict)
            and segment.get("type") == "text"
            and isinstance(segment.get("data"), dict)
        )
    message_obj = SimpleNamespace(
        raw_message=raw_event,
        message_id=raw_event.get("message_id", ""),
        time=raw_event.get("time", 0),
        sender=SimpleNamespace(
            user_id=sender_data.get("user_id", user_id),
            nickname=sender_data.get("nickname", ""),
            card=sender_data.get("card", ""),
            role=sender_data.get("role", ""),
        ),
    )
    event = SimpleNamespace(
        message_obj=message_obj,
        message_str=plain_text,
        get_messages=lambda: [],
    )
    return cache_group_message(group_id, user_id, event, settings)


def get_cached_messages(
    group_id: str,
    user_id: str | None = None,
    limit: int | None = 20,
    recalled_only: bool = False,
) -> list[dict]:
    """Read cached messages, newest first.

    ``limit=None`` is used by the portal export path to read every record that
    is still retained by the local cache policy instead of applying a second
    hidden web-sync limit.
    """
    try:
        safe_group_id = _safe_identifier(group_id, "group_id")
        safe_user_id = _safe_identifier(user_id, "user_id") if user_id else None
        maximum = None if limit is None else max(1, min(int(limit), _MAX_MESSAGES_PER_USER))
    except (ValueError, TypeError) as exc:
        logger.warning("Unable to read message cache: %s", exc)
        return []

    group_dir = _get_group_cache_dir(safe_group_id, create=False)
    if not group_dir.exists():
        return []
    records: list[dict] = []
    with _CACHE_LOCK:
        # Queries are also a maintenance opportunity when no new writes arrive.
        # This prevents recall markers for messages that never reached the
        # writer from accumulating indefinitely in a long-lived process.
        _prune_pending_recall_marks_locked()
        if safe_user_id:
            files = _iter_user_cache_files(group_dir, safe_user_id)
        else:
            files = sorted(group_dir.glob("*/*.json"), reverse=True)
        for index, path in enumerate(files):
            loaded_records = _load_records(path)
            if recalled_only:
                loaded_records = [
                    record for record in loaded_records if record.get("recalled") is True
                ]
            records.extend(loaded_records)

            # Files are sorted newest-date first.  For a whole-group query,
            # finish every user's file for the current date before stopping;
            # otherwise a newer message in the same day could be skipped.
            next_is_same_day = (
                index + 1 < len(files) and files[index + 1].parent == path.parent
            )
            if maximum is not None and len(records) >= maximum and not next_is_same_day:
                break

    def _record_sort_key(record: dict) -> tuple[int, int]:
        try:
            timestamp = int(record.get("timestamp", 0) or 0)
        except (TypeError, ValueError):
            timestamp = 0
        try:
            cached_at = int(record.get("cached_at", 0) or 0)
        except (TypeError, ValueError):
            cached_at = 0
        return timestamp, cached_at

    records.sort(key=_record_sort_key, reverse=True)
    return records if maximum is None else records[:maximum]


def get_group_member_activity(
    group_id: str,
    user_ids: object = None,
    *,
    days: int = 30,
    now: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Calculate recent member activity from local message records only."""
    try:
        safe_group_id = _safe_identifier(group_id, "group_id")
        window_days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        return {}
    current = int(now or time.time())
    cutoff = current - window_days * 86400
    selected = {
        str(item).strip()
        for item in (user_ids if isinstance(user_ids, (list, tuple, set, frozenset)) else [])
        if str(item).strip().isdigit()
    }
    cache_key = (safe_group_id, window_days)
    with _CACHE_LOCK:
        cached = _ACTIVITY_CACHE.get(cache_key)
        if cached and current - cached[0] < _ACTIVITY_CACHE_TTL_SECONDS:
            return {
                user_id: dict(values)
                for user_id, values in cached[1].items()
                if not selected or user_id in selected
            }
    group_dir = _get_group_cache_dir(safe_group_id, create=False)
    if not group_dir.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for day_dir in sorted(group_dir.iterdir(), reverse=True):
        if not day_dir.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_dir.name):
            continue
        for path in day_dir.glob("*.json"):
            user_id = path.stem
            if not user_id.isdigit() or (selected and user_id not in selected):
                continue
            for record in _load_records(path):
                try:
                    timestamp = int(record.get("timestamp", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if timestamp < cutoff or timestamp > current + 60:
                    continue
                item = result.setdefault(
                    user_id,
                    {
                        "message_count": 0,
                        "last_message_at": 0,
                        "text_count": 0,
                        "image_count": 0,
                        "video_count": 0,
                        "file_count": 0,
                        "record_count": 0,
                        "forward_count": 0,
                        "other_count": 0,
                        "last_message_text": "",
                        "last_message_types": "",
                    },
                )
                item["message_count"] += 1
                segments = record.get("segments", [])
                segment_types = {
                    str(segment.get("type", "")).strip().lower()
                    for segment in (segments if isinstance(segments, list) else [])
                    if isinstance(segment, dict) and str(segment.get("type", "")).strip()
                }
                if "text" in segment_types or str(record.get("text", "")).strip():
                    item["text_count"] += 1
                if "image" in segment_types:
                    item["image_count"] += 1
                if "video" in segment_types:
                    item["video_count"] += 1
                if "file" in segment_types:
                    item["file_count"] += 1
                if "record" in segment_types:
                    item["record_count"] += 1
                if "forward" in segment_types or "node" in segment_types:
                    item["forward_count"] += 1
                known_types = {"text", "image", "video", "file", "record", "forward", "node"}
                item["other_count"] += len(segment_types - known_types)
                if timestamp >= int(item.get("last_message_at", 0) or 0):
                    item["last_message_at"] = timestamp
                    text = str(record.get("text", "") or "").replace("\r", " ").replace("\n", " ").strip()
                    if text:
                        item["last_message_text"] = text[:160]
                    elif segment_types:
                        labels = {
                            "image": "图片",
                            "video": "视频",
                            "file": "文件",
                            "record": "语音",
                            "forward": "转发",
                            "node": "转发",
                            "text": "文字",
                        }
                        item["last_message_text"] = "、".join(
                            labels.get(kind, kind) for kind in sorted(segment_types)
                        )[:160]
                    item["last_message_types"] = ",".join(sorted(segment_types))[:120]
    for item in result.values():
        last = item.get("last_message_at", 0)
        item["inactive_seconds"] = max(0, current - last) if last else 0
    with _CACHE_LOCK:
        _ACTIVITY_CACHE[cache_key] = (
            current,
            {key: dict(value) for key, value in result.items()},
        )
    return {
        user_id: dict(values)
        for user_id, values in result.items()
        if not selected or user_id in selected
    }


def get_cached_message_ids(
    group_id: str,
    user_id: str,
    limit: int = 500,
    max_age_seconds: int | None = None,
) -> list[str]:
    """Return unique cached OneBot message IDs for a user's removable messages.

    ``max_age_seconds`` filters records before returning IDs.  QQ/OneBot only
    allows a short recall window, so callers handling automatic moderation can
    avoid slow, guaranteed-to-fail requests for historical records.
    """
    try:
        max_age = None if max_age_seconds is None else max(0, int(max_age_seconds))
    except (TypeError, ValueError):
        max_age = None
    now = int(time.time())
    message_ids: list[str] = []
    seen: set[str] = set()
    for record in get_cached_messages(group_id, user_id, limit):
        if record.get("recalled") is True:
            continue
        if max_age is not None:
            try:
                timestamp = int(record.get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                timestamp = 0
            if timestamp <= 0 or now - timestamp > max_age:
                continue
        message_id = _limited_text(record.get("message_id", ""), 128)
        if message_id and message_id not in seen:
            seen.add(message_id)
            message_ids.append(message_id)
    return message_ids


def mark_message_recalled(
    group_id: str,
    message_id: str | int,
    reason: str = "",
    operator_id: str = "",
) -> bool:
    """Mark a recalled message, including one still waiting in the write queue."""
    try:
        safe_group_id = _safe_identifier(group_id, "group_id")
    except ValueError as exc:
        logger.warning("Unable to mark recalled message: %s", exc)
        return False
    target_message_id = _limited_text(message_id, 128)
    if not target_message_id:
        return False

    updated = False
    recalled_at = int(time.time())
    recall_mark = {
        "recalled_at": recalled_at,
        "pending_at": recalled_at,
    }
    if reason:
        recall_mark["recall_reason"] = _limited_text(reason, 256)
    if operator_id:
        recall_mark["recall_operator_id"] = _limited_text(operator_id, 128)

    group_dir = _get_group_cache_dir(safe_group_id, create=False)
    with _CACHE_LOCK:
        # A queued message can be successfully recalled before the writer has
        # created its daily JSON file.  Retain this short-lived marker so the
        # writer applies it atomically with the first persistence.
        if not get_group_config_file(safe_group_id).is_file():
            return False
        _prune_pending_recall_marks_locked(recalled_at)
        marker_key = (safe_group_id, target_message_id)
        _PENDING_RECALL_MARKS[marker_key] = recall_mark

        cache_files = group_dir.glob("*/*.json") if group_dir.exists() else ()
        for path in cache_files:
            records = _load_records(path)
            changed = False
            for record in records:
                if str(record.get("message_id", "")) != target_message_id:
                    continue
                _apply_recall_mark(record, recall_mark)
                changed = True
                updated = True
            if changed:
                _atomic_write_json(path, records)
        if updated:
            _PENDING_RECALL_MARKS.pop(marker_key, None)
    return True


def _format_segment_summary(segment: dict, limit: int = 160, depth: int = 0) -> str:
    """Render one normalized segment without changing the original order."""
    if not isinstance(segment, dict):
        return ""
    segment_type = _limited_text(segment.get("type", ""), 64)
    data = segment.get("data", {})
    data = data if isinstance(data, dict) else {}
    summary = _limited_text(
        segment.get("summary") or _onebot_segment_summary(segment_type, data),
        limit,
    ).strip()
    if depth >= 2:
        return summary

    nested_parts: list[str] = []
    for field_name in ("chain", "nodes", "message", "messages", "content"):
        nested = data.get(field_name)
        if not isinstance(nested, list):
            continue
        for item in nested:
            if not isinstance(item, dict) or ("type" not in item and "summary" not in item):
                continue
            nested_summary = _format_segment_summary(item, 80, depth + 1)
            if nested_summary:
                nested_parts.append(nested_summary)
    if nested_parts:
        nested_text = _limited_text(" ".join(nested_parts), 120)
        summary = f"{summary} {{{nested_text}}}" if summary else f"{{{nested_text}}}"
    return _limited_text(summary, limit)


def format_cached_message(record: dict, content_limit: int = 280) -> str:
    """Create a bounded, ordered representation for an administrator reply."""
    segments = record.get("segments", []) if isinstance(record, dict) else []
    summaries = [
        _format_segment_summary(segment)
        for segment in segments
        if isinstance(segment, dict)
    ]
    summaries = [summary for summary in summaries if summary]
    if summaries:
        # Do not deduplicate or prepend aggregate text: both would lose the
        # position and count of repeated images, emoji, and other segments.
        content = " ".join(summaries)
    else:
        fallback_text = record.get("text", "") if isinstance(record, dict) else ""
        content = _limited_text(fallback_text, content_limit).strip()
    return _limited_text(content or "[空消息]", content_limit)


def remove_group_message_cache(group_id: str) -> bool:
    """Delete a group's message history as part of explicit group-data cleanup."""
    try:
        safe_group_id = _safe_identifier(group_id, "group_id")
        group_dir = _get_group_cache_dir(safe_group_id, create=False)
        with _CACHE_LOCK:
            if group_dir.exists():
                shutil.rmtree(group_dir)
            _LAST_CLEANUP_AT.pop(safe_group_id, None)
            _CACHE_GENERATIONS[safe_group_id] = int(
                _CACHE_GENERATIONS.get(safe_group_id, 0)
            ) + 1
            for cache_key in [key for key in _ACTIVITY_CACHE if key[0] == safe_group_id]:
                _ACTIVITY_CACHE.pop(cache_key, None)
            for key in [key for key in _PENDING_RECALL_MARKS if key[0] == safe_group_id]:
                _PENDING_RECALL_MARKS.pop(key, None)
        if not remove_group_message_media(safe_group_id):
            return False
        return True
    except Exception as exc:
        logger.error("Unable to remove message cache for group %s: %s", group_id, exc)
        return False
