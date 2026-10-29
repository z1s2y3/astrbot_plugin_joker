"""Per-account FTP and business database integrations.

These settings are separate from the portal database configured by
``JOKER_WEB_DATABASE_URL``.  They are intended for later features such as
check-in or points synchronization.  Passwords are encrypted before they are
stored in ``portal_settings`` and are never returned by the view helper,
written to audit logs, or included in the Windows synchronization payload.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from ftplib import Error as FtpProtocolError, FTP, FTP_TLS, error_perm
import hashlib
from io import BytesIO
import ipaddress
import os
import posixpath
import re
import socket
import ssl
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote_plus

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import create_engine, delete, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from .feature_schema import normalize_database_profile_descriptor
from .models import PortalDatabaseSnapshot, PortalSetting, utcnow
from .services import PortalError
from .settings import Settings


INTEGRATION_SETTING_KEY = "external_integrations"
SUPPORTED_DATABASE_ENGINES = {"mysql", "sqlite"}
DATABASE_ENGINE_ALIASES = {
    "sqlite3": "sqlite",
    "sqlitestudio": "sqlite",
    "sqlite-studio": "sqlite",
}
DEFAULT_FTP_PORT = 21
DEFAULT_DATABASE_PORT = 3306
MAX_SECRET_LENGTH = 512
MAX_FTP_LIST_ENTRIES = 500
# Automatic selection must remain responsive even when the configured game
# directory contains many folders.  The conventional sibling is checked
# separately; this budget applies only to the configured tree.
MAX_AUTO_DATABASE_DIRECTORIES = 32
MAX_SQLITE_DOWNLOAD_BYTES = 256 * 1024 * 1024
SQLITE_FILE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".s3db")
DEFAULT_SQLITE_FILENAME = "beijixiong.db"
DEFAULT_SIBLING_DATABASE_DIRECTORY = "BjxRobot"
# A few existing panel deployments used this spelling before the plugin
# directory was standardised as ``BjxRobot``.  It is intentionally a narrow
# compatibility alias, resolved only against a directory actually returned by
# the user's FTP account.
DATABASE_DIRECTORY_ALIASES = (
    DEFAULT_SIBLING_DATABASE_DIRECTORY,
    "BjRobot",
)
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
DATABASE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
CHARSET_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
DEFAULT_PROFILE_ID = "default"
DEFAULT_WINDOWS_DATABASE_PATH = "sql/servers/unassigned/default.db"
MAX_PROFILE_ID_LENGTH = 48
MAX_GROUP_SCOPE_LENGTH = 64
MAX_GROUP_PROFILES = 32
MAX_GROUP_FTP_CONNECTIONS = 32
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
GROUP_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PROFILE_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

_FTP_DATABASE_LOCK_GUARD = threading.Lock()
_FTP_DATABASE_LOCKS: dict[str, threading.RLock] = {}


def _ftp_database_lock(config: "FtpConnectionConfig", sqlite_path: str) -> threading.RLock:
    """Return one process-local lock for a single remote database target."""

    key = "|".join(
        (
            str(config.host).casefold(),
            str(config.port),
            str(config.username),
            str(config.remote_path),
            str(sqlite_path),
        )
    )
    with _FTP_DATABASE_LOCK_GUARD:
        lock = _FTP_DATABASE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FTP_DATABASE_LOCKS[key] = lock
        return lock


def integration_setting_key(owner_id: int | None = None) -> str:
    """Return the isolated settings key for an account.

    ``None`` keeps the legacy administrator-owned record readable for existing
    installations. Customer routes always pass their user ID, so one account
    can never read or overwrite another account's FTP/database credentials.
    """

    if owner_id is None:
        return INTEGRATION_SETTING_KEY
    try:
        normalized = int(owner_id)
    except (TypeError, ValueError):
        raise PortalError("用户身份无效") from None
    if normalized <= 0:
        raise PortalError("用户身份无效")
    return f"{INTEGRATION_SETTING_KEY}:user:{normalized}"


@dataclass(frozen=True)
class FtpConnectionConfig:
    enabled: bool
    host: str
    port: int
    username: str
    password: str
    remote_path: str
    tls: bool
    passive: bool
    # When enabled, operations may read one logical directory above the
    # configured root.  This is useful for FTP accounts whose database lives
    # beside (rather than inside) the configured application directory.
    allow_parent_directory: bool = False


@dataclass(frozen=True)
class BusinessDatabaseConfig:
    enabled: bool
    engine: str
    host: str
    port: int
    database: str
    username: str
    password: str
    charset: str
    sqlite_path: str = ""


@dataclass(frozen=True)
class FtpDirectoryEntry:
    """A safe, template-ready projection of one FTP directory entry."""

    name: str
    path: str
    kind: str
    size: int | None = None

    @property
    def is_sqlite_database(self) -> bool:
        return self.kind == "file" and self.name.casefold().endswith(SQLITE_FILE_SUFFIXES)


@dataclass(frozen=True)
class FtpDirectoryListing:
    path: str
    parent_path: str
    entries: tuple[FtpDirectoryEntry, ...]
    truncated: bool = False


def _fernet(settings: Settings) -> Fernet:
    """Derive a stable encryption key from the installation session secret."""

    key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.session_secret.encode("utf-8")).digest()
    )
    return Fernet(key)


def _encrypt(value: str, settings: Settings) -> str:
    return _fernet(settings).encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str, settings: Settings, label: str) -> str:
    try:
        return _fernet(settings).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise PortalError(
            f"已保存的{label}密码无法读取，请重新填写并保存"
        ) from exc


def _payload(
    db: Session,
    owner_id: int | None = None,
    *,
    for_update: bool = False,
) -> dict[str, Any]:
    """Load one account payload, optionally serializing a read-modify-write.

    Integration profiles are stored as one JSON document for backwards
    compatibility.  Mutating callers must lock that document before taking a
    copy; otherwise two simultaneous profile edits can overwrite each other
    even though each edit touched a different group/profile.
    """
    setting_key = integration_setting_key(owner_id)
    statement = select(PortalSetting).where(PortalSetting.key == setting_key)
    if for_update:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    setting = db.scalar(statement)
    if setting is None or not isinstance(setting.value_json, dict):
        return {}
    return deepcopy(setting.value_json)


def _clean_text(value: Any, limit: int, label: str) -> str:
    result = str(value or "").strip()
    if len(result) > limit or "\r" in result or "\n" in result or "\x00" in result:
        raise PortalError(f"{label}格式不正确")
    return result


def _normalize_host(value: Any, label: str) -> str:
    host = _clean_text(value, 253, label).strip("[]")
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not HOST_RE.fullmatch(host) or ".." in host:
        raise PortalError(f"{label}格式不正确，请填写 IP 或域名")
    return host.lower()


def _port(value: Any, default: int, label: str) -> int:
    try:
        result = int(str(value or default).strip())
    except (TypeError, ValueError):
        raise PortalError(f"{label}端口必须是数字") from None
    if not 1 <= result <= 65535:
        raise PortalError(f"{label}端口必须在 1-65535 之间")
    return result


def _secret(value: Any, previous: str, label: str, settings: Settings) -> str:
    supplied = str(value or "")
    if not supplied:
        # An empty password field means "keep the existing encrypted value".
        return previous
    if len(supplied) > MAX_SECRET_LENGTH or "\r" in supplied or "\n" in supplied:
        raise PortalError(f"{label}密码格式不正确")
    return _encrypt(supplied, settings)


def _as_bool(value: Any, default: bool = False) -> bool:
    """Parse a checkbox value, including repeated hidden+checkbox fields.

    ``default`` matters for legacy records which predate a boolean field.  It
    also keeps reads consistent with the form parser: an omitted ``passive``
    setting, for example, remains enabled instead of being silently reset.
    """

    if isinstance(value, (list, tuple, set)):
        if not value:
            return default
        return any(_as_bool(item) for item in value)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def _form_bool(form: Mapping[str, Any], name: str, default: bool = False) -> bool:
    """Read all values for a checkbox when Starlette FormData is supplied."""

    getlist = getattr(form, "getlist", None)
    if callable(getlist):
        values = getlist(name)
        if values:
            return _as_bool(values)
    value = form.get(name)
    return default if value is None else _as_bool(value)


def _section(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = payload.get(name)
    return value if isinstance(value, Mapping) else {}


def normalize_integration_profile_id(value: Any) -> str:
    """Validate a stable profile key used inside one group's settings."""

    raw = "" if value is None else str(value)
    if raw != raw.strip() or not PROFILE_ID_RE.fullmatch(raw):
        raise PortalError(
            "服务器配置编号只能包含字母、数字、下划线或短横线，且长度不能超过 48 个字符"
        )
    return raw


def _normalize_profile_id(value: Any) -> str:
    """Private compatibility alias used by tests and internal callers."""

    return normalize_integration_profile_id(value)


def _normalize_group_scope(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value)
    if raw != raw.strip() or not GROUP_SCOPE_RE.fullmatch(raw):
        raise PortalError("群配置 ID 格式不正确")
    return raw


def _normalize_scope(
    group_id: Any = None, profile_id: Any = None
) -> tuple[str | None, str | None]:
    group = _normalize_group_scope(group_id)
    if group is None:
        if profile_id is not None:
            raise PortalError("选择服务器时必须同时指定群配置 ID")
        return None, None
    profile = (
        normalize_integration_profile_id(profile_id)
        if profile_id is not None
        else None
    )
    return group, profile


def _profiles_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("profiles")
    return value if isinstance(value, Mapping) else {}


def _group_profiles(
    payload: Mapping[str, Any], group_id: str
) -> Mapping[str, Any]:
    value = _profiles_payload(payload).get(group_id)
    return value if isinstance(value, Mapping) else {}


def _ftp_connections_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return independently stored FTP connections keyed by group then ID."""

    value = payload.get("ftp_connections")
    return value if isinstance(value, Mapping) else {}


def _group_ftp_connections(
    payload: Mapping[str, Any], group_id: str
) -> Mapping[str, Any]:
    value = _ftp_connections_payload(payload).get(group_id)
    return value if isinstance(value, Mapping) else {}


def _profile_ftp_connection_id(profile: Mapping[str, Any]) -> str:
    raw = str(profile.get("ftp_connection_id", "") or "").strip()
    try:
        return normalize_integration_profile_id(raw) if raw else ""
    except PortalError:
        return ""


def _profile_ftp_section(
    payload: Mapping[str, Any],
    group_id: str | None,
    profile: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Resolve a server archive's FTP source without crossing group scope."""

    if group_id is not None:
        connection_id = _profile_ftp_connection_id(profile)
        if connection_id:
            connection = _group_ftp_connections(payload, group_id).get(connection_id)
            if isinstance(connection, Mapping):
                section = _section(connection, "ftp")
                if section:
                    return section
    # Old records stored the FTP section inside the server archive.  Keep this
    # read-only fallback until the group is lazily migrated to ftp_connections.
    return _section(profile, "ftp")


def _connection_name(record: Mapping[str, Any], connection_id: str) -> str:
    value = str(record.get("name", connection_id) or connection_id)
    value = "".join(
        " " if character in "\r\n" or ord(character) < 0x20 else character
        for character in value
    ).strip()
    return value[:80] or connection_id


def _active_profiles_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("active_profiles")
    return value if isinstance(value, Mapping) else {}


def _resolve_profile_id(
    payload: Mapping[str, Any],
    group_id: str | None,
    profile_id: str | None,
) -> str:
    if group_id is None:
        return DEFAULT_PROFILE_ID
    if profile_id is not None:
        return profile_id
    group_profiles = _group_profiles(payload, group_id)
    stored = _active_profiles_payload(payload).get(group_id)
    if stored is not None:
        try:
            candidate = normalize_integration_profile_id(stored)
        except PortalError:
            candidate = DEFAULT_PROFILE_ID
        candidate_record = group_profiles.get(candidate)
        if isinstance(candidate_record, Mapping) and _as_bool(
            candidate_record.get("profile_enabled", True), True
        ):
            return candidate
    # A stale active pointer (for example after an interrupted delete) should
    # resolve to the first enabled stored profile rather than an empty config.
    for stored_id in sorted(group_profiles, key=lambda value: str(value).casefold()):
        try:
            normalized_id = normalize_integration_profile_id(stored_id)
        except PortalError:
            continue
        record = group_profiles.get(stored_id)
        if isinstance(record, Mapping) and _as_bool(
            record.get("profile_enabled", True), True
        ):
            return normalized_id
    return DEFAULT_PROFILE_ID


def _scoped_sections(
    payload: Mapping[str, Any],
    group_id: Any = None,
    profile_id: Any = None,
) -> tuple[str | None, str, Mapping[str, Any], Mapping[str, Any]]:
    group, requested_profile = _normalize_scope(group_id, profile_id)
    selected_profile = _resolve_profile_id(payload, group, requested_profile)
    if group is None:
        container: Mapping[str, Any] = payload
    else:
        stored_profile = _group_profiles(payload, group).get(selected_profile)
        # Group pages must never inherit a historical top-level FTP record.
        # That fallback made a newly opened group appear to share another
        # group's server.  A group without a stored archive intentionally
        # receives an empty editor until its own archive is saved.
        container = stored_profile if isinstance(stored_profile, Mapping) else {}
    return (
        group,
        selected_profile,
        _profile_ftp_section(payload, group, container),
        _section(container, "database"),
    )


def _set_scoped_sections(
    payload: Mapping[str, Any],
    *,
    group_id: Any,
    profile_id: Any,
    ftp: Mapping[str, Any] | None,
    database: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
    activate: bool = False,
) -> tuple[dict[str, Any], str | None, str]:
    group, requested_profile = _normalize_scope(group_id, profile_id)
    selected_profile = _resolve_profile_id(payload, group, requested_profile)
    updated = deepcopy(dict(payload))
    profile_value = {"database": deepcopy(dict(database))}
    if ftp is not None:
        profile_value["ftp"] = deepcopy(dict(ftp))
    if group is None:
        if ftp is None:
            raise PortalError("未指定 FTP 连接时不能保存旧版全局配置")
        updated.update(profile_value)
        return updated, None, DEFAULT_PROFILE_ID

    profiles = deepcopy(dict(_profiles_payload(updated)))
    group_profiles = deepcopy(dict(_group_profiles(updated, group)))
    if selected_profile not in group_profiles and len(group_profiles) >= MAX_GROUP_PROFILES:
        raise PortalError(f"每个群最多配置 {MAX_GROUP_PROFILES} 台服务器")
    previous_profile = group_profiles.get(selected_profile)
    if isinstance(previous_profile, Mapping):
        for key in (
            "name",
            "server_id",
            "profile_enabled",
            "database_profile_id",
            "windows_database_path",
            "snapshot_revision",
            "snapshot_sha256",
            "ftp_connection_id",
        ):
            if key in previous_profile:
                profile_value[key] = deepcopy(previous_profile[key])
    if isinstance(metadata, Mapping):
        for key in (
            "name",
            "server_id",
            "profile_enabled",
            "database_profile_id",
            "windows_database_path",
            "snapshot_revision",
            "snapshot_sha256",
            "ftp_connection_id",
        ):
            if key in metadata:
                profile_value[key] = deepcopy(metadata[key])
    group_profiles[selected_profile] = profile_value
    profiles[group] = group_profiles
    updated["profiles"] = profiles

    active_profiles = deepcopy(dict(_active_profiles_payload(updated)))
    current_active = active_profiles.get(group)
    try:
        current_active_id = normalize_integration_profile_id(current_active)
    except PortalError:
        current_active_id = ""
    current_active_record = group_profiles.get(current_active_id)
    active_is_valid = isinstance(current_active_record, Mapping) and _as_bool(
        current_active_record.get("profile_enabled", True), True
    )
    selected_enabled = _as_bool(profile_value.get("profile_enabled", True), True)
    if activate and not selected_enabled:
        raise PortalError("停用的服务器不能设为当前使用")
    if activate or (not active_is_valid and selected_enabled):
        active_profiles[group] = selected_profile
    updated["active_profiles"] = active_profiles
    return updated, group, selected_profile


def _safe_config_view(
    ftp: Mapping[str, Any], database: Mapping[str, Any]
) -> dict[str, Any]:
    """Project stored sections without returning either encrypted secret."""

    try:
        safe_ftp_root = _normalize_ftp_root(ftp.get("remote_path", "/"))
    except PortalError:
        # A hand-edited legacy JSON record must not make the settings page
        # fail closed with a 500. Reads still reject the invalid path before
        # any network operation; the safe view only displays a neutral root.
        safe_ftp_root = "/"
    try:
        ftp_port = int(ftp.get("port", DEFAULT_FTP_PORT) or DEFAULT_FTP_PORT)
    except (TypeError, ValueError):
        ftp_port = DEFAULT_FTP_PORT
    try:
        database_port = int(
            database.get("port", DEFAULT_DATABASE_PORT) or DEFAULT_DATABASE_PORT
        )
    except (TypeError, ValueError):
        database_port = DEFAULT_DATABASE_PORT
    sqlite_path = str(database.get("sqlite_path", database.get("remote_path", "")))
    return {
        "ftp": {
            "enabled": _as_bool(ftp.get("enabled", False)),
            "host": str(ftp.get("host", "")),
            "port": ftp_port,
            "username": str(ftp.get("username", "")),
            "remote_path": safe_ftp_root,
            "tls": _as_bool(ftp.get("tls", False)),
            "passive": _as_bool(ftp.get("passive", True), True),
            "allow_parent_directory": _as_bool(
                ftp.get("allow_parent_directory", ftp.get("allow_parent", False))
            ),
            "password_configured": bool(ftp.get("password_ciphertext", "")),
        },
        "database": {
            "enabled": _as_bool(database.get("enabled", False)),
            "engine": _normalize_database_engine(database.get("engine", "sqlite")),
            "host": str(database.get("host", "")),
            "port": database_port,
            "database": str(database.get("database", "")),
            # Keep the physical SQLite filename stable for existing snapshot
            # consumers while exposing a separate operator-facing database
            # name in the combined server data-source form.
            "display_name": str(
                database.get("display_name", database.get("database", ""))
                or ""
            ),
            "username": str(database.get("username", "")),
            "charset": str(database.get("charset", "utf8mb4")),
            "sqlite_path": sqlite_path,
            "selection_source": (
                str(database.get("selection_source", "")).strip()
                if sqlite_path.strip()
                else ""
            ),
            "password_configured": bool(database.get("password_ciphertext", "")),
        },
    }


def _normalize_database_engine(value: Any) -> str:
    engine = str(value or "sqlite").strip().lower()
    return DATABASE_ENGINE_ALIASES.get(engine, engine)


def _normalize_remote_path(value: Any, label: str = "FTP 目录") -> str:
    """Normalize a user supplied FTP path without allowing traversal."""

    raw = _clean_text(value or "/", 1024, label).replace("\\", "/")
    if "\x00" in raw or any(ord(char) < 32 for char in raw):
        raise PortalError(f"{label}格式不正确")
    if not raw.startswith("/"):
        raw = "/" + raw
    # FTP is a POSIX path even when the remote host runs Windows.  Refuse
    # traversal components rather than relying on an FTP server's cwd rules.
    components = [part for part in raw.split("/") if part not in {"", "."}]
    if any(part == ".." for part in components):
        raise PortalError(f"{label}不能包含 ..")
    normalized = "/" + "/".join(components)
    return normalized if normalized != "" else "/"


def _normalize_ftp_root(value: Any) -> str:
    """Return the canonical saved FTP root.

    A panel-managed FTP account commonly exposes a chroot whose visible
    root is ``/``.  Blank legacy values therefore mean the virtual root too;
    database selection must never rewrite that root to the database folder.
    """

    return _normalize_remote_path(value or "/", "FTP 根目录")


def _normalize_database_file_path(value: Any) -> str:
    path = _normalize_remote_path(value, "数据库文件路径")
    if path == "/":
        raise PortalError("请选择或填写数据库文件路径")
    filename = posixpath.basename(path).lower()
    if not filename.endswith(SQLITE_FILE_SUFFIXES):
        raise PortalError("SQLite 数据库文件必须以 .db、.sqlite、.sqlite3 或 .s3db 结尾")
    return path


def _normalize_windows_database_path(value: Any) -> str:
    """Validate a local path that may cross the Windows sync boundary.

    The FTP-selected path is a remote POSIX path and must never be reused as
    a Windows filesystem path.  This separate validator accepts only a
    relative path under the plugin directory and keeps the descriptor safe to
    pass through the existing ``database_profiles`` projection.
    """

    raw = _clean_text(value or DEFAULT_WINDOWS_DATABASE_PATH, 512, "Windows 数据库路径")
    normalized = raw.replace("\\", "/").strip()
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or "\x00" in normalized
        or any(":" in part or any(ord(char) < 32 for char in part) for part in parts)
        or any(part == ".." for part in parts)
        or not parts
    ):
        raise PortalError("Windows 数据库路径必须是插件目录内的相对路径")
    filename = parts[-1].casefold()
    if not filename.endswith(SQLITE_FILE_SUFFIXES):
        raise PortalError("Windows 数据库路径必须以 .db、.sqlite、.sqlite3 或 .s3db 结尾")
    return "/".join(parts)


def _scoped_windows_database_path(group_id: str, profile_id: str) -> str:
    """Return a deterministic local path for a colliding database profile."""

    return f"sql/servers/{group_id}/{profile_id}.db"


def _ftp_parent_path(root: str) -> str:
    """Return the one-level parent boundary for an already normalized path."""

    if root == "/":
        return "/"
    return posixpath.dirname(root.rstrip("/")) or "/"


def _path_within_ftp_root(
    root: Any,
    candidate: Any,
    label: str = "FTP 文件路径",
    allow_parent_directory: bool = False,
    parent_path_depth: int | None = None,
    case_insensitive: bool = False,
) -> str:
    """Normalize a path and keep it inside the configured FTP scope.

    The normal scope is the saved root.  An explicit parent-directory switch
    adds a tightly bounded path under the parent.  It never accepts ``..``
    syntax and cannot walk beyond the calculated boundary.  Callers choose the
    parent-relative depth needed for their operation: directory browsing needs
    at most one direct child, while SQLite selection may use a direct file in a
    sibling directory.
    """

    normalized_root = _normalize_remote_path(root or "/", "FTP 根目录")
    normalized_candidate = _normalize_remote_path(candidate, label)
    if normalized_root == "/":
        return normalized_candidate
    root_prefix = normalized_root.rstrip("/") + "/"
    comparable_root = normalized_root.casefold() if case_insensitive else normalized_root
    comparable_candidate = (
        normalized_candidate.casefold() if case_insensitive else normalized_candidate
    )
    comparable_root_prefix = root_prefix.casefold() if case_insensitive else root_prefix
    if (
        comparable_candidate == comparable_root
        or comparable_candidate.startswith(comparable_root_prefix)
    ):
        return normalized_candidate
    if allow_parent_directory:
        parent_root = _ftp_parent_path(normalized_root)
        comparable_parent = parent_root.casefold() if case_insensitive else parent_root
        if comparable_candidate == comparable_parent:
            return normalized_candidate
        parent_prefix = parent_root.rstrip("/") + "/"
        comparable_parent_prefix = (
            parent_prefix.casefold() if case_insensitive else parent_prefix
        )
        if not comparable_candidate.startswith(comparable_parent_prefix):
            raise PortalError(f"{label}必须位于 FTP 根目录内")
        relative = normalized_candidate[len(parent_prefix) :]
        depth = len([part for part in relative.split("/") if part])
        # Database selection may enter one direct sibling directory and select
        # one file, such as `/BjxRobot/beijixiong.db` or
        # `/data/BjxRobot/beijixiong.db`.  Browsing passes an explicit depth
        # of one and therefore cannot move below the sibling directory.
        allowed_depth = (
            2
            if parent_path_depth is None
            else max(0, int(parent_path_depth))
        )
        if 0 < depth <= allowed_depth:
            return normalized_candidate
    if label == "数据库文件路径":
        raise PortalError(
            "数据库文件路径必须位于 FTP 根目录内；如果数据库在同级目录，请先开启“允许访问上一级目录”"
        )
    raise PortalError(f"{label}必须位于 FTP 根目录内")


def _save_payload(
    db: Session,
    owner_id: int | None,
    payload: Mapping[str, Any],
    operator_id: int | None,
) -> None:
    """Persist one complete per-account integration payload."""

    setting_key = integration_setting_key(owner_id)
    # Keep the complete JSON document update under the same row lock used by
    # mutating readers.  SQLite ignores FOR UPDATE, while MySQL serializes
    # concurrent profile saves as intended.
    statement = (
        select(PortalSetting)
        .where(PortalSetting.key == setting_key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    setting = db.scalar(statement)
    value = deepcopy(dict(payload))
    if setting is None:
        db.add(
            PortalSetting(
                key=setting_key,
                value_json=value,
                updated_by=operator_id,
            )
        )
    else:
        setting.value_json = value
        setting.updated_by = operator_id


def _profile_summaries(
    payload: Mapping[str, Any], group_id: str | None
) -> list[dict[str, Any]]:
    if group_id is None:
        ftp = _section(payload, "ftp")
        database = _section(payload, "database")
        if not ftp and not database:
            return []
        summary = _safe_config_view(ftp, database)
        summary.update(
            {
                "id": DEFAULT_PROFILE_ID,
                "profile_id": DEFAULT_PROFILE_ID,
                "group_id": None,
                "active": True,
                "name": DEFAULT_PROFILE_ID,
                "server_id": "",
                "profile_enabled": True,
                "snapshot_revision": 0,
                "snapshot_sha256": "",
            }
        )
        return [summary]

    active_profile = _resolve_profile_id(payload, group_id, None)
    summaries: list[dict[str, Any]] = []
    for stored_id, stored_profile in _group_profiles(payload, group_id).items():
        try:
            profile = normalize_integration_profile_id(stored_id)
        except PortalError:
            continue
        if not isinstance(stored_profile, Mapping):
            continue
        ftp_connection_id = _profile_ftp_connection_id(stored_profile)
        ftp_connection = _group_ftp_connections(payload, group_id).get(
            ftp_connection_id
        )
        summary = _safe_config_view(
            _profile_ftp_section(payload, group_id, stored_profile),
            _section(stored_profile, "database"),
        )
        try:
            snapshot_revision = int(stored_profile.get("snapshot_revision", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            snapshot_revision = 0
        safe_name = str(stored_profile.get("name", profile) or profile)
        safe_name = "".join(
            " " if character in "\r\n" or ord(character) < 0x20 else character
            for character in safe_name
        ).strip()[:80] or profile
        safe_server_id = str(stored_profile.get("server_id", "") or "").strip()[:128]
        if safe_server_id and not PROFILE_SERVER_ID_RE.fullmatch(safe_server_id):
            safe_server_id = ""
        snapshot_sha256 = str(stored_profile.get("snapshot_sha256", "") or "").strip().lower()
        if len(snapshot_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in snapshot_sha256
        ):
            snapshot_sha256 = ""
        # FTP is the database source. Windows receives an isolated temporary
        # snapshot path, derived from the group and archive ID, rather than a
        # user-selected local database path. This avoids cross-group writes
        # when several remote servers use the same SQLite filename.
        windows_database_path = _scoped_windows_database_path(group_id, profile)
        summary.update(
            {
                "id": profile,
                "profile_id": profile,
                "group_id": group_id,
                "active": profile == active_profile,
                "name": safe_name,
                "server_id": safe_server_id,
                "database_profile_id": str(
                    stored_profile.get("database_profile_id", profile) or profile
                ).strip() or profile,
                "profile_enabled": _as_bool(stored_profile.get("profile_enabled", True), True),
                "ftp_connection_id": ftp_connection_id,
                "ftp_connection_name": (
                    _connection_name(ftp_connection, ftp_connection_id)
                    if isinstance(ftp_connection, Mapping) and ftp_connection_id
                    else ""
                ),
                "windows_database_path": windows_database_path,
                "snapshot_revision": snapshot_revision,
                "snapshot_sha256": snapshot_sha256,
            }
        )
        summaries.append(summary)
    summaries.sort(key=lambda item: str(item["profile_id"]).casefold())
    return summaries


def list_integration_profiles(
    db: Session,
    owner_id: int | None = None,
    *,
    group_id: Any = None,
) -> list[dict[str, Any]]:
    """List stored profiles as safe views without either secret ciphertext."""

    group = _normalize_group_scope(group_id)
    return _profile_summaries(_payload(db, owner_id), group)


def rename_integration_profile(
    db: Session,
    owner_id: int | None,
    group_id: Any,
    old_profile_id: Any,
    new_profile_id: Any,
    operator_id: int | None = None,
) -> str:
    """Rename a group server archive without losing its database snapshot.

    ``profile_id`` is used as the snapshot scope on the sync wire, so a rename
    must move the stored archive, active pointer and snapshot row together.
    The operation is intentionally separate from the normal form save: callers
    can validate the new identifier before writing FTP or game settings.
    """

    group = _normalize_group_scope(group_id)
    if group is None:
        raise PortalError("必须指定群配置 ID")
    old_id = normalize_integration_profile_id(old_profile_id)
    new_id = normalize_integration_profile_id(new_profile_id)
    if old_id == new_id:
        return new_id
    payload = _payload(db, owner_id, for_update=True)
    profiles = deepcopy(dict(_group_profiles(payload, group)))
    record = profiles.get(old_id)
    if not isinstance(record, Mapping):
        raise PortalError("原服务器不存在")
    if new_id in profiles:
        raise PortalError("新的服务器编号已存在，请使用其他编号")
    moved = deepcopy(dict(record))
    moved["windows_database_path"] = _scoped_windows_database_path(group, new_id)
    profiles.pop(old_id, None)
    profiles[new_id] = moved
    for sibling in profiles.values():
        if not isinstance(sibling, dict):
            continue
        if str(sibling.get("database_profile_id", "") or "").strip() == old_id:
            sibling["database_profile_id"] = new_id
    updated = deepcopy(dict(payload))
    all_profiles = deepcopy(dict(_profiles_payload(updated)))
    all_profiles[group] = profiles
    updated["profiles"] = all_profiles
    active_profiles = deepcopy(dict(_active_profiles_payload(updated)))
    if str(active_profiles.get(group, "")) == old_id:
        active_profiles[group] = new_id
    updated["active_profiles"] = active_profiles
    _save_payload(db, owner_id, updated, operator_id)
    try:
        normalized_owner = int(owner_id) if owner_id is not None else 0
    except (TypeError, ValueError, OverflowError):
        normalized_owner = 0
    if normalized_owner > 0:
        db.execute(
            update(PortalDatabaseSnapshot)
            .where(
                PortalDatabaseSnapshot.owner_user_id == normalized_owner,
                PortalDatabaseSnapshot.group_id == group,
                PortalDatabaseSnapshot.profile_id == old_id,
            )
            .values(profile_id=new_id)
        )
    return new_id


def _legacy_ftp_connection_id(profile_id: str, existing: Mapping[str, Any]) -> str:
    """Create a deterministic, valid FTP ID while avoiding existing records."""

    base = f"ftp-{profile_id}"
    if len(base) > MAX_PROFILE_ID_LENGTH:
        digest = hashlib.sha256(profile_id.encode("utf-8")).hexdigest()[:8]
        base = f"ftp-{profile_id[:35]}-{digest}"
    candidate = base
    suffix = 2
    while candidate in existing:
        trailer = f"-{suffix}"
        candidate = base[: MAX_PROFILE_ID_LENGTH - len(trailer)] + trailer
        suffix += 1
    return candidate


def migrate_legacy_group_ftp_connections(
    db: Session,
    owner_id: int | None,
    group_id: Any,
    operator_id: int | None = None,
) -> bool:
    """Extract old per-server FTP sections into independent connection records.

    Earlier releases stored each server's FTP credentials next to its database
    choice.  This migration is intentionally group-local and only copies the
    existing encrypted value; it never reads or rewrites another group's data.
    """

    group = _normalize_group_scope(group_id)
    if group is None:
        raise PortalError("必须指定群配置 ID")
    payload = _payload(db, owner_id, for_update=True)
    source_profiles = _group_profiles(payload, group)
    if not source_profiles:
        return False
    profiles = deepcopy(dict(_profiles_payload(payload)))
    group_profiles = deepcopy(dict(source_profiles))
    connections = deepcopy(dict(_ftp_connections_payload(payload)))
    group_connections = deepcopy(dict(_group_ftp_connections(payload, group)))
    changed = False
    for raw_profile_id, raw_profile in list(group_profiles.items()):
        try:
            profile_id = normalize_integration_profile_id(raw_profile_id)
        except PortalError:
            continue
        if not isinstance(raw_profile, Mapping):
            continue
        profile = deepcopy(dict(raw_profile))
        if _profile_ftp_connection_id(profile):
            continue
        legacy_ftp = _section(profile, "ftp")
        if not legacy_ftp:
            continue
        if len(group_connections) >= MAX_GROUP_FTP_CONNECTIONS:
            raise PortalError(f"每个群最多配置 {MAX_GROUP_FTP_CONNECTIONS} 个 FTP 连接")
        connection_id = _legacy_ftp_connection_id(profile_id, group_connections)
        profile_name = _connection_name(profile, profile_id)
        group_connections[connection_id] = {
            "name": f"{profile_name} FTP"[:80],
            "ftp": deepcopy(dict(legacy_ftp)),
        }
        profile.pop("ftp", None)
        profile["ftp_connection_id"] = connection_id
        group_profiles[profile_id] = profile
        changed = True
    if not changed:
        return False
    profiles[group] = group_profiles
    connections[group] = group_connections
    updated = deepcopy(dict(payload))
    updated["profiles"] = profiles
    updated["ftp_connections"] = connections
    _save_payload(db, owner_id, updated, operator_id)
    return True


def _ftp_connection_view(
    connection_id: str,
    record: Mapping[str, Any],
    bound_profiles: list[str],
) -> dict[str, Any]:
    safe = _safe_config_view(_section(record, "ftp"), {})["ftp"]
    return {
        "id": connection_id,
        "ftp_connection_id": connection_id,
        "name": _connection_name(record, connection_id),
        "ftp": safe,
        "bound_profile_ids": sorted(bound_profiles, key=str.casefold),
        "bound_count": len(bound_profiles),
    }


def list_ftp_connections(
    db: Session,
    owner_id: int | None,
    group_id: Any,
) -> list[dict[str, Any]]:
    """List only the FTP connections stored for one group."""

    group = _normalize_group_scope(group_id)
    if group is None:
        raise PortalError("必须指定群配置 ID")
    payload = _payload(db, owner_id)
    bindings: dict[str, list[str]] = {}
    for raw_profile_id, raw_profile in _group_profiles(payload, group).items():
        if not isinstance(raw_profile, Mapping):
            continue
        connection_id = _profile_ftp_connection_id(raw_profile)
        if connection_id:
            bindings.setdefault(connection_id, []).append(str(raw_profile_id))
    views: list[dict[str, Any]] = []
    for raw_connection_id, raw_connection in _group_ftp_connections(payload, group).items():
        try:
            connection_id = normalize_integration_profile_id(raw_connection_id)
        except PortalError:
            continue
        if not isinstance(raw_connection, Mapping):
            continue
        views.append(
            _ftp_connection_view(
                connection_id,
                raw_connection,
                bindings.get(connection_id, []),
            )
        )
    views.sort(key=lambda item: str(item["ftp_connection_id"]).casefold())
    return views


def get_ftp_connection_view(
    db: Session,
    owner_id: int | None,
    group_id: Any,
    ftp_connection_id: Any,
) -> dict[str, Any]:
    """Return one template-safe FTP connection, or an empty editor view."""

    group = _normalize_group_scope(group_id)
    if group is None:
        raise PortalError("必须指定群配置 ID")
    connection_id = normalize_integration_profile_id(ftp_connection_id)
    payload = _payload(db, owner_id)
    record = _group_ftp_connections(payload, group).get(connection_id)
    if not isinstance(record, Mapping):
        return _ftp_connection_view(connection_id, {}, [])
    bound = [
        str(profile_id)
        for profile_id, profile in _group_profiles(payload, group).items()
        if isinstance(profile, Mapping)
        and _profile_ftp_connection_id(profile) == connection_id
    ]
    return _ftp_connection_view(connection_id, record, bound)


def save_ftp_connection(
    db: Session,
    settings: Settings,
    form: Mapping[str, Any],
    operator_id: int | None,
    *,
    owner_id: int | None,
    group_id: Any,
    ftp_connection_id: Any,
) -> dict[str, Any]:
    """Save one independent FTP connection and invalidate only its consumers."""

    group = _normalize_group_scope(group_id)
    if group is None:
        raise PortalError("必须指定群配置 ID")
    connection_id = normalize_integration_profile_id(ftp_connection_id)
    previous = _payload(db, owner_id, for_update=True)
    previous_connections = _group_ftp_connections(previous, group)
    previous_record = previous_connections.get(connection_id)
    previous_record = previous_record if isinstance(previous_record, Mapping) else {}
    previous_ftp = _section(previous_record, "ftp")
    name = str(form.get("ftp_connection_name", form.get("profile_name", "")) or "")
    name = name.replace("\r", " ").replace("\n", " ").strip() or connection_id
    if len(name) > 80 or any(ord(character) < 0x20 for character in name):
        raise PortalError("FTP 连接名称长度应为 1-80 个字符")
    enabled = _form_bool(form, "ftp_enabled")
    host = _normalize_host(form.get("ftp_host"), "FTP 服务器")
    port = _port(form.get("ftp_port"), DEFAULT_FTP_PORT, "FTP ")
    username = _clean_text(form.get("ftp_username"), 320, "FTP 用户名")
    raw_path = form.get("ftp_remote_path")
    if not str(raw_path or "").strip():
        raw_path = previous_ftp.get("remote_path", "/") or "/"
    remote_path = _normalize_ftp_root(raw_path)
    password = _secret(
        form.get("ftp_password"),
        str(previous_ftp.get("password_ciphertext", "")),
        "FTP ",
        settings,
    )
    if enabled and (not host or not username or not password):
        raise PortalError("启用 FTP 时必须填写服务器、账号和密码")
    allow_parent = (
        _form_bool(form, "ftp_allow_parent")
        if "ftp_allow_parent" in form
        else _as_bool(previous_ftp.get("allow_parent_directory", False))
    )
    ftp = {
        "enabled": enabled,
        "host": host,
        "port": port,
        "username": username,
        "remote_path": remote_path,
        "tls": _form_bool(form, "ftp_tls"),
        "passive": _form_bool(form, "ftp_passive")
        if "ftp_passive" in form
        else _as_bool(previous_ftp.get("passive", True), True),
        "allow_parent_directory": allow_parent,
        "password_ciphertext": password,
    }
    identity_changed = any(
        str(previous_ftp.get(key, "") or "").strip().casefold()
        != str(ftp.get(key, "") or "").strip().casefold()
        for key in (
            "enabled", "host", "port", "username", "remote_path", "tls",
            "passive", "allow_parent_directory", "password_ciphertext",
        )
    )
    connections = deepcopy(dict(_ftp_connections_payload(previous)))
    group_connections = deepcopy(dict(previous_connections))
    if connection_id not in group_connections and len(group_connections) >= MAX_GROUP_FTP_CONNECTIONS:
        raise PortalError(f"每个群最多配置 {MAX_GROUP_FTP_CONNECTIONS} 个 FTP 连接")
    group_connections[connection_id] = {"name": name, "ftp": ftp}
    connections[group] = group_connections
    updated = deepcopy(dict(previous))
    updated["ftp_connections"] = connections
    affected_profiles: list[str] = []
    if identity_changed:
        profiles = deepcopy(dict(_profiles_payload(updated)))
        group_profiles = deepcopy(dict(_group_profiles(updated, group)))
        for profile_id, raw_profile in list(group_profiles.items()):
            if not isinstance(raw_profile, Mapping):
                continue
            profile = deepcopy(dict(raw_profile))
            if _profile_ftp_connection_id(profile) != connection_id:
                continue
            profile["snapshot_revision"] = 0
            profile["snapshot_sha256"] = ""
            group_profiles[profile_id] = profile
            affected_profiles.append(str(profile_id))
        profiles[group] = group_profiles
        updated["profiles"] = profiles
    _save_payload(db, owner_id, updated, operator_id)
    if identity_changed:
        try:
            normalized_owner = int(owner_id) if owner_id is not None else 0
        except (TypeError, ValueError, OverflowError):
            normalized_owner = 0
        if normalized_owner > 0 and affected_profiles:
            db.execute(
                delete(PortalDatabaseSnapshot).where(
                    PortalDatabaseSnapshot.owner_user_id == normalized_owner,
                    PortalDatabaseSnapshot.group_id == group,
                    PortalDatabaseSnapshot.profile_id.in_(affected_profiles),
                )
            )
    view = get_ftp_connection_view(db, owner_id, group, connection_id)
    view["invalidated_profile_ids"] = affected_profiles
    return view


def delete_ftp_connection(
    db: Session,
    owner_id: int | None,
    group_id: Any,
    ftp_connection_id: Any,
    operator_id: int | None = None,
    *,
    detach_bound_profiles: bool = False,
) -> list[str]:
    """Delete an FTP connection, optionally detaching its server profiles.

    A normal delete remains strict so an accidental click cannot break a live
    server.  The explicit detach path is used by the cleanup UI: it clears the
    affected profile's database selection and snapshot before removing the
    source, while leaving sibling profiles and FTP connections untouched.
    """

    group = _normalize_group_scope(group_id)
    if group is None:
        raise PortalError("必须指定群配置 ID")
    connection_id = normalize_integration_profile_id(ftp_connection_id)
    payload = _payload(db, owner_id, for_update=True)
    connections = deepcopy(dict(_ftp_connections_payload(payload)))
    group_connections = deepcopy(dict(_group_ftp_connections(payload, group)))
    if connection_id not in group_connections:
        raise PortalError("FTP 连接不存在")
    bound = [
        str(profile_id)
        for profile_id, profile in _group_profiles(payload, group).items()
        if isinstance(profile, Mapping)
        and _profile_ftp_connection_id(profile) == connection_id
    ]
    if bound:
        if not detach_bound_profiles:
            raise PortalError("该 FTP 连接仍被服务器引用：" + "、".join(bound[:5]))
        profiles = deepcopy(dict(_profiles_payload(payload)))
        group_profiles = deepcopy(dict(_group_profiles(payload, group)))
        for profile_id in bound:
            raw_profile = group_profiles.get(profile_id)
            if not isinstance(raw_profile, Mapping):
                continue
            profile = deepcopy(dict(raw_profile))
            profile["ftp_connection_id"] = ""
            profile.pop("ftp", None)
            database = deepcopy(dict(_section(profile, "database")))
            database.update(
                {
                    "enabled": False,
                    "sqlite_path": "",
                    "selection_source": "",
                }
            )
            profile["database"] = database
            profile["snapshot_revision"] = 0
            profile["snapshot_sha256"] = ""
            group_profiles[profile_id] = profile
        profiles[group] = group_profiles
        payload["profiles"] = profiles
        try:
            normalized_owner = int(owner_id) if owner_id is not None else 0
        except (TypeError, ValueError, OverflowError):
            normalized_owner = 0
        if normalized_owner > 0:
            db.execute(
                delete(PortalDatabaseSnapshot).where(
                    PortalDatabaseSnapshot.owner_user_id == normalized_owner,
                    PortalDatabaseSnapshot.group_id == group,
                    PortalDatabaseSnapshot.profile_id.in_(bound),
                )
            )
    del group_connections[connection_id]
    if group_connections:
        connections[group] = group_connections
    else:
        connections.pop(group, None)
    updated = deepcopy(dict(payload))
    if connections:
        updated["ftp_connections"] = connections
    else:
        updated.pop("ftp_connections", None)
    _save_payload(db, owner_id, updated, operator_id)
    return bound


def get_windows_database_profile_projection(
    db: Session,
    owner_id: int | None,
    group_id: Any,
) -> dict[str, Any]:
    """Return the non-sensitive database profile payload for Windows.

    FTP hosts, usernames, remote paths, DSNs and both encrypted/plaintext
    passwords intentionally never participate in this projection.  The only
    paths emitted are generated, group-scoped Windows cache paths, so the
    Windows validator cannot mistake a remote FTP path for a local file or
    collide with another group's snapshot. The returned ``server_id`` /
    ``server_profiles`` fields contain only the public SCUM identifiers needed
    by the Windows query command.
    """

    group = _normalize_group_scope(group_id)
    if group is None:
        raise PortalError("必须指定群配置 ID")
    payload = _payload(db, owner_id)
    active = _resolve_profile_id(payload, group, None)
    descriptors: list[dict[str, Any]] = []
    # The active integration profile also identifies the SCUM server that the
    # Windows command layer should query.  Keep this as a separate, strictly
    # allow-listed projection: FTP hosts, remote paths and credentials remain
    # Linux-owned and never cross the sync boundary.
    server_descriptors: list[dict[str, Any]] = []
    active_server_id = ""
    for item in _profile_summaries(payload, group):
        database = item.get("database") if isinstance(item, Mapping) else None
        database = database if isinstance(database, Mapping) else {}
        # Only a selected SQLite file can be validated by the Windows
        # ``user_list`` checker.  Keep metadata-only/MySQL profiles as
        # disabled descriptors so an explicit group profile cannot silently
        # fall back to another database.
        engine = _normalize_database_engine(database.get("engine", "sqlite"))
        sqlite_selected = bool(
            engine == "sqlite"
            and _as_bool(database.get("enabled", False))
            and str(database.get("sqlite_path", "") or "").strip()
        )
        try:
            local_path = _normalize_windows_database_path(
                item.get("windows_database_path", _scoped_windows_database_path(group, str(item.get("profile_id", "") or "")))
            )
            try:
                snapshot_revision = int(item.get("snapshot_revision", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                snapshot_revision = 0
            snapshot_sha256 = str(item.get("snapshot_sha256", "") or "").strip().lower()
            snapshot_ready = (
                snapshot_revision > 0
                and len(snapshot_sha256) == 64
                and all(character in "0123456789abcdef" for character in snapshot_sha256)
            )
            descriptor_input = {
                "id": item.get("profile_id", item.get("id", "")),
                "name": item.get("name", item.get("profile_id", "")),
                "server_id": item.get("server_id", ""),
                "enabled": bool(
                    item.get("profile_enabled", True)
                    and sqlite_selected
                    and snapshot_ready
                ),
                "path": local_path,
            }
            if snapshot_revision > 0 or snapshot_sha256:
                descriptor_input.update(
                    {
                        "snapshot_revision": snapshot_revision,
                        "snapshot_sha256": snapshot_sha256,
                    }
                )
            descriptor = normalize_database_profile_descriptor(descriptor_input)
        except (PortalError, ValueError):
            continue
        if any(existing.get("id") == descriptor["id"] for existing in descriptors):
            continue
        descriptors.append(descriptor)

        raw_server_id = str(item.get("server_id", "") or "").strip()
        if raw_server_id and PROFILE_SERVER_ID_RE.fullmatch(raw_server_id):
            server_descriptors.append(
                {
                    "id": descriptor["id"],
                    "name": descriptor["name"],
                    "server_id": raw_server_id,
                    # A disabled profile must never become a query target even
                    # when it remains visible in the portal for editing.
                    "enabled": bool(item.get("profile_enabled", True)),
                }
            )
    # Once a group has an explicit integration profile, do not let an empty or
    # MySQL-only selection silently fall back to the bundled database on
    # Windows.  Keeping the active ID without a descriptor makes the local
    # resolver return ``db_unavailable`` until the operator selects a valid
    # SQLite snapshot.
    scoped_ids: list[str] = []
    for stored_id in _group_profiles(payload, group):
        try:
            normalized_id = normalize_integration_profile_id(stored_id)
        except PortalError:
            continue
        if normalized_id not in scoped_ids:
            scoped_ids.append(normalized_id)
    selected = active if active in scoped_ids else (scoped_ids[0] if scoped_ids else "")
    selected_path = next(
        (
            item["path"]
            for item in descriptors
            if item["id"] == selected and item["enabled"]
        ),
        "",
    )
    for item in server_descriptors:
        if item["id"] == selected and item.get("enabled"):
            active_server_id = str(item.get("server_id", "") or "")
            break
    return {
        "database_profile": selected,
        "database_path": selected_path,
        "database_profiles": descriptors,
        "server_id": active_server_id,
        "server_profiles": server_descriptors,
        "integration_managed": bool(scoped_ids),
    }


def get_active_integration_profile(
    db: Session,
    owner_id: int | None,
    group_id: Any,
) -> str:
    """Return the effective active profile ID for one group."""

    group = _normalize_group_scope(group_id)
    if group is None:
        raise PortalError("必须指定群配置 ID")
    return _resolve_profile_id(_payload(db, owner_id), group, None)


def get_integration_config_view(
    db: Session,
    owner_id: int | None = None,
    *,
    group_id: Any = None,
    profile_id: Any = None,
) -> dict[str, Any]:
    """Return a template-safe view; plaintext and ciphertext stay internal."""

    payload = _payload(db, owner_id)
    group, selected_profile, ftp, database = _scoped_sections(
        payload, group_id, profile_id
    )
    view = _safe_config_view(ftp, database)
    # The ID stays internal; operator-facing messages use the saved server name.
    view["profile_name"] = str(selected_profile or DEFAULT_PROFILE_ID)
    view.update(
        {
            "group_id": group,
            "profile_id": selected_profile,
            "active_profile_id": _resolve_profile_id(payload, group, None),
            "profiles": _profile_summaries(payload, group),
        }
    )
    if group is not None:
        profile = _group_profiles(payload, group).get(selected_profile)
        if isinstance(profile, Mapping):
            configured_name = str(
                profile.get("name", selected_profile) or selected_profile
            )
            configured_name = "".join(
                " " if character in "\r\n" or ord(character) < 0x20 else character
                for character in configured_name
            ).strip()[:80]
            view["profile_name"] = configured_name or str(selected_profile)
            database_profile_id = str(
                profile.get("database_profile_id", selected_profile) or selected_profile
            ).strip()
            if database_profile_id not in _group_profiles(payload, group):
                database_profile_id = selected_profile
            view["database_profile_id"] = database_profile_id
            connection_id = _profile_ftp_connection_id(profile)
            connection = _group_ftp_connections(payload, group).get(connection_id)
            view["ftp_connection_id"] = connection_id
            view["ftp_connection_name"] = (
                _connection_name(connection, connection_id)
                if isinstance(connection, Mapping) and connection_id
                else ""
            )
        else:
            view["ftp_connection_id"] = ""
            view["ftp_connection_name"] = ""
    return view


def save_server_database_binding(
    db: Session,
    form: Mapping[str, Any],
    operator_id: int | None,
    *,
    owner_id: int | None,
    group_id: Any,
    profile_id: Any,
    activate: bool = False,
) -> dict[str, Any]:
    """Save server metadata, FTP reference, and an optional SQLite selection."""

    group = _normalize_group_scope(group_id)
    if group is None:
        raise PortalError("必须指定群配置 ID")
    profile = normalize_integration_profile_id(profile_id)
    # A caller may have created or edited this server's FTP connection in the
    # same database transaction. Make that write visible before validating the
    # connection ID against the scoped payload.
    db.flush()
    previous = _payload(db, owner_id, for_update=True)
    previous_profile = _group_profiles(previous, group).get(profile)
    previous_profile = previous_profile if isinstance(previous_profile, Mapping) else {}
    name = str(form.get("profile_name", "") or "").replace("\r", " ").replace("\n", " ").strip() or profile
    if len(name) > 80 or any(ord(character) < 0x20 for character in name):
        raise PortalError("服务器名称长度应为 1-80 个字符")
    database_display_name = str(
        form.get("database_display_name", "") or ""
    ).replace("\r", " ").replace("\n", " ").strip()
    if len(database_display_name) > 80 or any(
        ord(character) < 0x20 for character in database_display_name
    ):
        raise PortalError("数据库名称长度应为 1-80 个字符")
    # The SCUM online-query ID is bound by the group command, not entered in
    # the server/FTP/database editor. Preserve any legacy stored value when
    # this form intentionally omits that field.
    server_id_source = (
        form.get("server_id", "")
        if "server_id" in form
        else previous_profile.get("server_id", "")
    )
    server_id = str(server_id_source or "").strip()
    if server_id and not PROFILE_SERVER_ID_RE.fullmatch(server_id):
        raise PortalError("服务器 ID 格式不正确")
    enabled = _form_bool(form, "profile_enabled", True)
    raw_connection = str(form.get("ftp_connection_id", "") or "").strip()
    connection_id = normalize_integration_profile_id(raw_connection) if raw_connection else ""
    if connection_id and not isinstance(
        _group_ftp_connections(previous, group).get(connection_id), Mapping
    ):
        raise PortalError("请选择当前群已保存的 FTP 连接")
    previous_connection = _profile_ftp_connection_id(previous_profile)
    previous_database = _section(previous_profile, "database")
    reused_database_profile = False
    database_profile_id = str(
        form.get("database_profile_id", profile) or profile
    ).strip() or profile
    try:
        database_profile_id = normalize_integration_profile_id(database_profile_id)
    except PortalError:
        raise PortalError("服务器数据库编号格式不正确") from None
    if database_profile_id != profile:
        source_profile = _group_profiles(previous, group).get(database_profile_id)
        if not isinstance(source_profile, Mapping):
            raise PortalError("选择的服务器数据库不存在")
        if not _as_bool(source_profile.get("profile_enabled", True), True):
            raise PortalError("不能复用已停用服务器的数据库")
        source_database = _section(source_profile, "database")
        if not _as_bool(source_database.get("enabled", False)) or not str(
            source_database.get("sqlite_path", "") or ""
        ).strip():
            raise PortalError("选择的服务器尚未绑定 SQLite 数据库文件")
        # A server may reuse another saved database definition.  The source
        # connection is selected by the route when it is available; keeping
        # the database section here also makes legacy operations immediate.
        previous_database = source_database
        reused_database_profile = True
    source_changed = previous_connection != connection_id and not reused_database_profile
    database = deepcopy(dict(previous_database))
    database_path_changed = False
    if source_changed:
        # A path selected through one FTP endpoint is not meaningful after
        # the server archive points at another endpoint.
        database = {
            "enabled": False,
            "engine": "sqlite",
            "sqlite_path": "",
            "selection_source": "",
            "display_name": "",
        }
    raw_database_path = str(form.get("database_path", "") or "").strip()
    if raw_database_path and not reused_database_profile:
        connection = _group_ftp_connections(previous, group).get(connection_id)
        ftp = _section(connection, "ftp") if isinstance(connection, Mapping) else {}
        if not _as_bool(ftp.get("enabled", False)):
            raise PortalError("请先启用 FTP，再绑定服务器数据库文件")
        ftp_root = _normalize_ftp_root(ftp.get("remote_path", "/"))
        database_path = _path_within_ftp_root(
            ftp_root,
            _normalize_database_file_path(raw_database_path),
            "数据库文件路径",
            bool(ftp.get("allow_parent_directory", ftp.get("allow_parent", False))),
            case_insensitive=True,
        )
        database_path_changed = (
            not _as_bool(database.get("enabled", False))
            or str(database.get("sqlite_path", "") or "").strip() != database_path
        )
        database.update(
            {
                "enabled": True,
                "engine": "sqlite",
                "sqlite_path": database_path,
                "database": posixpath.basename(database_path),
                "display_name": (
                    database_display_name
                    or str(database.get("display_name", "") or "").strip()
                    or posixpath.basename(database_path)
                ),
                "host": "",
                "port": DEFAULT_DATABASE_PORT,
                "username": "",
                "password_ciphertext": "",
                "selection_source": "manual",
            }
        )
    elif database_display_name and not reused_database_profile:
        # Rename an existing selected file in the same unified form without
        # touching its remote path or database contents.
        database["display_name"] = database_display_name
    metadata = {
        "name": name,
        "server_id": server_id,
        "profile_enabled": enabled,
        "database_profile_id": database_profile_id,
        "windows_database_path": _scoped_windows_database_path(group, profile),
        "ftp_connection_id": connection_id,
    }
    if source_changed or database_path_changed:
        metadata.update({"snapshot_revision": 0, "snapshot_sha256": ""})
    elif reused_database_profile:
        metadata.update({"snapshot_revision": 0, "snapshot_sha256": ""})
    updated, _, selected_profile = _set_scoped_sections(
        previous,
        group_id=group,
        profile_id=profile,
        ftp=None,
        database=database,
        metadata=metadata,
        activate=activate,
    )
    _save_payload(db, owner_id, updated, operator_id)
    if source_changed or database_path_changed:
        try:
            normalized_owner = int(owner_id) if owner_id is not None else 0
        except (TypeError, ValueError, OverflowError):
            normalized_owner = 0
        if normalized_owner > 0:
            db.execute(
                delete(PortalDatabaseSnapshot).where(
                    PortalDatabaseSnapshot.owner_user_id == normalized_owner,
                    PortalDatabaseSnapshot.group_id == group,
                    PortalDatabaseSnapshot.profile_id == selected_profile,
                )
            )
    return get_integration_config_view(
        db, owner_id, group_id=group, profile_id=selected_profile
    )


def save_integration_config(
    db: Session,
    settings: Settings,
    form: Mapping[str, Any],
    operator_id: int | None,
    owner_id: int | None = None,
    *,
    group_id: Any = None,
    profile_id: Any = None,
    activate: bool = False,
) -> dict[str, Any]:
    """Validate and persist both integration sections."""

    previous = _payload(db, owner_id, for_update=True)
    group_scope, selected_scope_profile, previous_ftp, previous_database = _scoped_sections(
        previous, group_id, profile_id
    )
    uses_independent_ftp = False
    profile_name = str(form.get("profile_name", "") or "").replace("\r", " ").replace("\n", " ").strip()
    server_id = str(form.get("server_id", "") or "").strip()
    profile_enabled = _form_bool(form, "profile_enabled", True)
    if group_scope is not None:
        selected_for_name = selected_scope_profile or normalize_integration_profile_id(profile_id)
        previous_profile = _group_profiles(previous, group_scope).get(selected_for_name)
        previous_profile = previous_profile if isinstance(previous_profile, Mapping) else {}
        uses_independent_ftp = bool(_profile_ftp_connection_id(previous_profile))
        if "profile_name" not in form:
            profile_name = str(previous_profile.get("name", "") or "").strip()
        profile_name = profile_name or selected_for_name
        if "server_id" not in form:
            server_id = str(previous_profile.get("server_id", "") or "").strip()
        if "profile_enabled" not in form:
            profile_enabled = _as_bool(previous_profile.get("profile_enabled", True), True)
        if len(profile_name) > 80 or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in profile_name):
            raise PortalError("服务器名称长度应为 1-80 个字符")
    if server_id and not PROFILE_SERVER_ID_RE.fullmatch(server_id):
        raise PortalError("服务器 ID 格式不正确")

    windows_database_path = DEFAULT_WINDOWS_DATABASE_PATH
    if group_scope is not None:
        windows_database_path = _scoped_windows_database_path(
            group_scope,
            selected_scope_profile or normalize_integration_profile_id(profile_id),
        )

    ftp_enabled = _form_bool(form, "ftp_enabled")
    ftp_host = _normalize_host(form.get("ftp_host"), "FTP 服务器")
    ftp_port = _port(form.get("ftp_port"), DEFAULT_FTP_PORT, "FTP ")
    ftp_username = _clean_text(form.get("ftp_username"), 320, "FTP 用户名")
    # A partial form submission must not silently rewrite the account's FTP
    # root.  This matters for users whose server exposes its virtual account
    # root as `/`: their database is selected by an absolute path below that
    # root (for example `/BjxRobot/beijixiong.db`), not by changing the FTP
    # root to the database directory.
    raw_ftp_path = form.get("ftp_remote_path")
    if not str(raw_ftp_path or "").strip():
        raw_ftp_path = previous_ftp.get("remote_path", "/") or "/"
    ftp_path = _normalize_ftp_root(raw_ftp_path)
    ftp_password = _secret(
        form.get("ftp_password"),
        str(previous_ftp.get("password_ciphertext", "")),
        "FTP ",
        settings,
    )
    if ftp_enabled and (not ftp_host or not ftp_username or not ftp_password):
        raise PortalError("启用 FTP 时必须填写服务器、账号和密码")
    ftp_allow_parent = (
        _form_bool(form, "ftp_allow_parent")
        if "ftp_allow_parent" in form
        else (
            _form_bool(form, "ftp_allow_parent_directory")
            if "ftp_allow_parent_directory" in form
            else bool(
                previous_ftp.get(
                    "allow_parent_directory", previous_ftp.get("allow_parent", False)
                )
            )
        )
    )

    # The FTP panel is saved independently from database selection.  A form
    # that contains no database fields must not disable or reinterpret an
    # existing MySQL/SQLite configuration just because the user edited FTP.
    database_form_fields = {
        "database_enabled",
        "database_engine",
        "database_sqlite_path",
        "database_path",
        "database_host",
        "database_port",
        "database_name",
        "database_username",
        "database_password",
        "database_charset",
        "database_selection_source",
    }
    if not any(field in form for field in database_form_fields):
        ftp_section = {
            "enabled": ftp_enabled,
            "host": ftp_host,
            "port": ftp_port,
            "username": ftp_username,
            "remote_path": ftp_path,
            "tls": _form_bool(form, "ftp_tls"),
            "passive": _form_bool(form, "ftp_passive")
            if "ftp_passive" in form
            else bool(previous_ftp.get("passive", True)),
            "allow_parent_directory": ftp_allow_parent,
            "password_ciphertext": ftp_password,
        }
        ftp_snapshot_metadata: dict[str, Any] = {}
        if group_scope is not None:
            ftp_identity_changed = any(
                str(previous_ftp.get(key, "") or "").strip().casefold()
                != str(ftp_section.get(key, "") or "").strip().casefold()
                for key in (
                    "host",
                    "port",
                    "username",
                    "remote_path",
                    "tls",
                    "passive",
                    "allow_parent_directory",
                    "password_ciphertext",
                )
            )
            if ftp_identity_changed:
                ftp_snapshot_metadata = {
                    "snapshot_revision": 0,
                    "snapshot_sha256": "",
                }
        payload, _, selected_profile = _set_scoped_sections(
            previous,
            group_id=group_id,
            profile_id=profile_id,
            ftp=None if uses_independent_ftp else ftp_section,
            database=previous_database,
            metadata=(
                {
                    "name": profile_name,
                    "server_id": server_id,
                    "profile_enabled": profile_enabled,
                    "windows_database_path": windows_database_path,
                    **ftp_snapshot_metadata,
                }
                if group_scope is not None
                else None
            ),
            activate=activate,
        )
        _save_payload(db, owner_id, payload, operator_id)
        if ftp_snapshot_metadata and group_scope is not None:
            try:
                normalized_owner = int(owner_id) if owner_id is not None else 0
            except (TypeError, ValueError, OverflowError):
                normalized_owner = 0
            if normalized_owner > 0:
                db.execute(
                    delete(PortalDatabaseSnapshot).where(
                        PortalDatabaseSnapshot.owner_user_id == normalized_owner,
                        PortalDatabaseSnapshot.group_id == group_scope,
                        PortalDatabaseSnapshot.profile_id == selected_profile,
                    )
                )
        return get_integration_config_view(
            db,
            owner_id,
            group_id=group_id,
            profile_id=selected_profile if group_id is not None else None,
        )

    database_enabled = _form_bool(form, "database_enabled")
    requested_engine = form.get("database_engine")
    database_engine = _normalize_database_engine(
        requested_engine
        if requested_engine is not None
        else previous_database.get("engine", "sqlite")
    )
    if database_engine not in SUPPORTED_DATABASE_ENGINES:
        raise PortalError("数据库类型仅支持 MySQL 或 SQLite 文件")
    sqlite_path_value = form.get(
        "database_sqlite_path",
        form.get("database_path", previous_database.get("sqlite_path", "")),
    )
    sqlite_path = ""
    if str(sqlite_path_value or "").strip():
        sqlite_path = _normalize_database_file_path(sqlite_path_value)
        sqlite_path = _path_within_ftp_root(
            ftp_path,
            sqlite_path,
            "数据库文件路径",
            ftp_allow_parent,
            case_insensitive=True,
        )
    database_host = _normalize_host(form.get("database_host"), "数据库服务器")
    database_port = _port(form.get("database_port"), DEFAULT_DATABASE_PORT, "数据库 ")
    database_name = _clean_text(form.get("database_name"), 128, "数据库名称")
    if database_name and not DATABASE_NAME_RE.fullmatch(database_name):
        raise PortalError("数据库名称只能包含字母、数字、下划线、点或短横线")
    database_username = _clean_text(form.get("database_username"), 320, "数据库用户名")
    database_charset = _clean_text(
        form.get("database_charset") or "utf8mb4", 32, "数据库字符集"
    )
    if not CHARSET_RE.fullmatch(database_charset):
        raise PortalError("数据库字符集格式不正确")
    if database_engine == "sqlite":
        if database_enabled and (not ftp_enabled or not ftp_password):
            raise PortalError("启用 SQLite 文件时必须先启用并填写 FTP 连接")
        if database_enabled and not sqlite_path:
            raise PortalError("启用 SQLite 文件时请选择或填写数据库文件路径")
        # A SQLite file is selected from FTP; stale MySQL credentials must not
        # remain active after switching the database type.
        database_password = ""
        database_name = posixpath.basename(sqlite_path) if sqlite_path else ""
    else:
        database_password = _secret(
            form.get("database_password"),
            str(previous_database.get("password_ciphertext", "")),
            "数据库 ",
            settings,
        )
        if database_enabled and (
            not database_host
            or not database_name
            or not database_username
            or not database_password
        ):
            raise PortalError("启用 MySQL 时必须填写服务器、库名、账号和密码")

    ftp_section = {
        "enabled": ftp_enabled,
        "host": ftp_host,
        "port": ftp_port,
        "username": ftp_username,
        "remote_path": ftp_path,
        "tls": _form_bool(form, "ftp_tls"),
        # Unchecked HTML checkboxes are absent from FormData.  The default
        # is true for old records and for forms that predate this switch.
        "passive": _form_bool(form, "ftp_passive")
        if "ftp_passive" in form
        else bool(previous_ftp.get("passive", True)),
        "allow_parent_directory": ftp_allow_parent,
        "password_ciphertext": ftp_password,
    }
    database_section = {
        "enabled": database_enabled,
        "engine": database_engine,
        "host": database_host,
        "port": database_port,
        "database": database_name,
        "username": database_username,
        "charset": database_charset,
        "sqlite_path": sqlite_path,
        "selection_source": str(
            form.get(
                "database_selection_source",
                previous_database.get("selection_source", "manual"),
            )
        )[:16]
        if database_engine == "sqlite"
        else "manual",
        "password_ciphertext": database_password,
    }
    snapshot_metadata: dict[str, Any] = {}
    if group_scope is not None:
        previous_engine = _normalize_database_engine(
            previous_database.get("engine", "sqlite")
        )
        previous_enabled = _as_bool(previous_database.get("enabled", False))
        previous_sqlite_path = str(
            previous_database.get("sqlite_path", "") or ""
        ).strip()
        ftp_identity_changed = any(
            str(previous_ftp.get(key, "") or "").strip().casefold()
            != str(ftp_section.get(key, "") or "").strip().casefold()
            for key in (
                "enabled",
                "host",
                "port",
                "username",
                "remote_path",
                "tls",
                "passive",
                "allow_parent_directory",
                "password_ciphertext",
            )
        )
        database_identity_changed = (
            previous_engine != database_engine
            or previous_enabled != database_enabled
            or previous_sqlite_path.casefold() != sqlite_path.casefold()
            or ftp_identity_changed
        )
        if database_identity_changed:
            # The old BLOB belongs to a different remote file (or to a
            # disabled/MySQL profile).  Clear its wire metadata until the
            # operator selects and validates a fresh SQLite snapshot.
            snapshot_metadata = {"snapshot_revision": 0, "snapshot_sha256": ""}
    payload, _, selected_profile = _set_scoped_sections(
        previous,
        group_id=group_id,
        profile_id=profile_id,
        ftp=None if uses_independent_ftp else ftp_section,
        database=database_section,
        metadata=(
            {
                "name": profile_name,
                "server_id": server_id,
                "profile_enabled": profile_enabled,
                "windows_database_path": windows_database_path,
                **snapshot_metadata,
            }
            if group_scope is not None
            else None
        ),
        activate=activate,
    )
    _save_payload(db, owner_id, payload, operator_id)
    if snapshot_metadata and group_scope is not None:
        try:
            normalized_owner = int(owner_id) if owner_id is not None else 0
        except (TypeError, ValueError, OverflowError):
            normalized_owner = 0
        if normalized_owner > 0:
            db.execute(
                delete(PortalDatabaseSnapshot).where(
                    PortalDatabaseSnapshot.owner_user_id == normalized_owner,
                    PortalDatabaseSnapshot.group_id == group_scope,
                    PortalDatabaseSnapshot.profile_id == selected_profile,
                )
            )
    return get_integration_config_view(
        db,
        owner_id,
        group_id=group_id,
        profile_id=selected_profile if group_id is not None else None,
    )


def upsert_integration_profile(
    db: Session,
    settings: Settings,
    form: Mapping[str, Any],
    operator_id: int | None,
    owner_id: int | None = None,
    *,
    group_id: Any,
    profile_id: Any,
    activate: bool = False,
) -> dict[str, Any]:
    """Create or update one validated group/server integration profile."""

    group = _normalize_group_scope(group_id)
    if group is None:
        raise PortalError("必须指定群配置 ID")
    profile = normalize_integration_profile_id(profile_id)
    return save_integration_config(
        db,
        settings,
        form,
        operator_id,
        owner_id,
        group_id=group,
        profile_id=profile,
        activate=activate,
    )


def set_active_integration_profile(
    db: Session,
    owner_id: int | None,
    group_id: Any,
    profile_id: Any,
    operator_id: int | None = None,
) -> dict[str, Any]:
    """Select an existing profile for subsequent scoped reads and tests."""

    group = _normalize_group_scope(group_id)
    if group is None:
        raise PortalError("必须指定群配置 ID")
    profile = normalize_integration_profile_id(profile_id)
    payload = _payload(db, owner_id, for_update=True)
    profiles = _group_profiles(payload, group)
    stored_profile = profiles.get(profile)
    if not isinstance(stored_profile, Mapping):
        raise PortalError("服务器不存在")
    if isinstance(stored_profile, Mapping) and not _as_bool(
        stored_profile.get("profile_enabled", True), True
    ):
        raise PortalError("停用的服务器不能设为当前使用")
    active_profiles = deepcopy(dict(_active_profiles_payload(payload)))
    active_profiles[group] = profile
    payload["active_profiles"] = active_profiles
    _save_payload(db, owner_id, payload, operator_id)
    return get_integration_config_view(db, owner_id, group_id=group)


def set_integration_profile_enabled(
    db: Session,
    owner_id: int | None,
    group_id: Any,
    profile_id: Any,
    enabled: bool,
    operator_id: int | None = None,
) -> dict[str, Any]:
    """Pause or resume one server profile without editing its data source."""

    group = _normalize_group_scope(group_id)
    if group is None:
        raise PortalError("必须指定群配置 ID")
    profile = normalize_integration_profile_id(profile_id)
    payload = _payload(db, owner_id, for_update=True)
    profiles = deepcopy(dict(_profiles_payload(payload)))
    group_profiles = deepcopy(dict(_group_profiles(payload, group)))
    record = group_profiles.get(profile)
    if not isinstance(record, Mapping):
        raise PortalError("服务器不存在")

    record = deepcopy(dict(record))
    record["profile_enabled"] = bool(enabled)
    group_profiles[profile] = record
    profiles[group] = group_profiles
    payload["profiles"] = profiles

    active_profiles = deepcopy(dict(_active_profiles_payload(payload)))
    active_id = str(active_profiles.get(group, "") or "").strip()
    active_record = group_profiles.get(active_id)
    active_is_enabled = isinstance(active_record, Mapping) and _as_bool(
        active_record.get("profile_enabled", True), True
    )
    if not enabled and active_id == profile:
        active_id = ""
        for candidate in sorted(group_profiles, key=lambda value: str(value).casefold()):
            candidate_record = group_profiles.get(candidate)
            if isinstance(candidate_record, Mapping) and _as_bool(
                candidate_record.get("profile_enabled", True), True
            ):
                active_id = str(candidate)
                break
        if active_id:
            active_profiles[group] = active_id
        else:
            active_profiles.pop(group, None)
    elif enabled and not active_is_enabled:
        active_profiles[group] = profile

    if active_profiles:
        payload["active_profiles"] = active_profiles
    else:
        payload.pop("active_profiles", None)
    _save_payload(db, owner_id, payload, operator_id)
    return get_integration_config_view(db, owner_id, group_id=group)


def delete_integration_profile(
    db: Session,
    owner_id: int | None,
    group_id: Any,
    profile_id: Any,
    operator_id: int | None = None,
) -> dict[str, Any]:
    """Delete one scoped profile without changing legacy or sibling records."""

    group = _normalize_group_scope(group_id)
    if group is None:
        raise PortalError("必须指定群配置 ID")
    profile = normalize_integration_profile_id(profile_id)
    payload = _payload(db, owner_id, for_update=True)
    profiles = deepcopy(dict(_profiles_payload(payload)))
    group_profiles = deepcopy(dict(_group_profiles(payload, group)))
    if profile not in group_profiles:
        raise PortalError("服务器不存在")

    was_active = _resolve_profile_id(payload, group, None) == profile
    del group_profiles[profile]
    if group_profiles:
        profiles[group] = group_profiles
    else:
        profiles.pop(group, None)
    if profiles:
        payload["profiles"] = profiles
    else:
        payload.pop("profiles", None)

    active_profiles = deepcopy(dict(_active_profiles_payload(payload)))
    if was_active:
        default_record = group_profiles.get(DEFAULT_PROFILE_ID)
        if isinstance(default_record, Mapping) and _as_bool(
            default_record.get("profile_enabled", True), True
        ):
            active_profiles[group] = DEFAULT_PROFILE_ID
        else:
            fallback_profile = None
            for candidate in sorted(
                group_profiles, key=lambda value: str(value).casefold()
            ):
                record = group_profiles.get(candidate)
                if isinstance(record, Mapping) and _as_bool(
                    record.get("profile_enabled", True), True
                ):
                    try:
                        fallback_profile = normalize_integration_profile_id(candidate)
                    except PortalError:
                        continue
                    break
            if fallback_profile:
                active_profiles[group] = fallback_profile
            else:
                active_profiles.pop(group, None)
    if active_profiles:
        payload["active_profiles"] = active_profiles
    else:
        payload.pop("active_profiles", None)

    _save_payload(db, owner_id, payload, operator_id)
    # The BLOB is no longer reachable once its profile is deleted.  Remove it
    # in the same transaction so a later Windows request cannot retain stale
    # database bytes indefinitely.
    try:
        normalized_owner = int(owner_id)
    except (TypeError, ValueError, OverflowError):
        normalized_owner = 0
    if normalized_owner > 0:
        db.execute(
            delete(PortalDatabaseSnapshot).where(
                PortalDatabaseSnapshot.owner_user_id == normalized_owner,
                PortalDatabaseSnapshot.group_id == group,
                PortalDatabaseSnapshot.profile_id == profile,
            )
        )
    view = get_integration_config_view(db, owner_id, group_id=group)
    view["deleted_profile_id"] = profile
    return view


# Concise aliases keep the profile API convenient for callers outside the web
# package while the longer names remain self-documenting at route boundaries.
list_profiles = list_integration_profiles
upsert_profile = upsert_integration_profile
save_integration_profile = upsert_integration_profile
set_active_profile = set_active_integration_profile
activate_integration_profile = set_active_integration_profile
delete_profile = delete_integration_profile


def _read_ftp_config(
    db: Session,
    settings: Settings,
    owner_id: int | None = None,
    *,
    group_id: Any = None,
    profile_id: Any = None,
) -> FtpConnectionConfig:
    _, _, section, _ = _scoped_sections(
        _payload(db, owner_id), group_id, profile_id
    )
    ciphertext = str(section.get("password_ciphertext", ""))
    return FtpConnectionConfig(
        enabled=_as_bool(section.get("enabled", False)),
        host=str(section.get("host", "")),
        port=int(section.get("port", DEFAULT_FTP_PORT) or DEFAULT_FTP_PORT),
        username=str(section.get("username", "")),
        password=_decrypt(ciphertext, settings, "FTP") if ciphertext else "",
        remote_path=_normalize_ftp_root(section.get("remote_path", "/")),
        tls=_as_bool(section.get("tls", False)),
        passive=_as_bool(section.get("passive", True), True),
        allow_parent_directory=_as_bool(
            section.get("allow_parent_directory", section.get("allow_parent", False))
        ),
    )


def _read_database_config(
    db: Session,
    settings: Settings,
    owner_id: int | None = None,
    *,
    group_id: Any = None,
    profile_id: Any = None,
) -> BusinessDatabaseConfig:
    _, _, _, section = _scoped_sections(
        _payload(db, owner_id), group_id, profile_id
    )
    ciphertext = str(section.get("password_ciphertext", ""))
    return BusinessDatabaseConfig(
        enabled=_as_bool(section.get("enabled", False)),
        engine=_normalize_database_engine(section.get("engine", "sqlite")),
        host=str(section.get("host", "")),
        port=int(section.get("port", DEFAULT_DATABASE_PORT) or DEFAULT_DATABASE_PORT),
        database=str(section.get("database", "")),
        username=str(section.get("username", "")),
        password=_decrypt(ciphertext, settings, "数据库") if ciphertext else "",
        charset=str(section.get("charset", "utf8mb4")),
        sqlite_path=str(
            section.get("sqlite_path", section.get("remote_path", ""))
        ),
    )


def _ftp_permission_error(
    exc: FtpProtocolError, *, directory: bool = False
) -> PortalError:
    """Convert FTP responses to bounded messages without echoing server text."""

    code = str(exc).split(" ", 1)[0].strip()
    if code == "530":
        return PortalError("FTP 登录失败，请检查账号和密码")
    if code == "550" and directory:
        return PortalError("FTP 目录不可访问，请检查路径和账号权限")
    if code == "550":
        return PortalError("FTP 文件不可访问，请检查路径和账号权限")
    return PortalError("FTP 登录或目录权限验证失败")


@contextmanager
def _open_ftp(config: FtpConnectionConfig):
    """Open a short-lived authenticated FTP session."""

    if not config.enabled:
        raise PortalError("FTP 功能尚未启用")
    client: FTP | FTP_TLS = FTP_TLS() if config.tls else FTP()
    try:
        client.connect(config.host, config.port, timeout=10)
        if config.tls:
            client.auth()
            client.prot_p()
        client.login(config.username, config.password)
        client.set_pasv(config.passive)
        client.voidcmd("TYPE I")
        yield client
    except socket.gaierror as exc:
        raise PortalError("FTP 域名解析失败，请检查服务器 IP 或域名") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise PortalError("FTP 连接超时，请检查端口、防火墙和被动模式") from exc
    except ConnectionRefusedError as exc:
        raise PortalError("FTP 端口拒绝连接，请确认 FTP 服务和端口已开放") from exc
    except ssl.SSLError as exc:
        raise PortalError("FTPS TLS 握手失败，请检查证书和 FTPS 设置") from exc
    except error_perm as exc:
        raise _ftp_permission_error(exc, directory=True) from exc
    except (OSError, EOFError) as exc:
        raise PortalError("FTP 连接失败，请检查地址、端口、防火墙和账号密码") from exc
    except PortalError:
        raise
    except Exception as exc:
        raise PortalError("FTP 登录或目录访问失败，请检查配置") from exc
    finally:
        try:
            client.quit()
        except Exception:
            try:
                client.close()
            except Exception:
                pass


def _safe_ftp_entry_name(value: Any) -> str:
    name = str(value or "").strip()
    # MLSD implementations differ: some return a basename, others return a
    # path.  Keep only the final component and reject control/path separators.
    name = posixpath.basename(name.rstrip("/"))
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 for char in name)
    ):
        return ""
    return name


def _ftp_child_path(directory: str, name: str) -> str:
    return _normalize_remote_path(
        posixpath.join(directory, name), "FTP 文件路径"
    )


def _ftp_path_variants(
    path: str,
    *,
    prefer_relative: bool = False,
    allow_virtual_root_parent: bool = False,
) -> tuple[str, ...]:
    """Return absolute and chroot-relative spellings for one safe path.

    FTP daemons differ here: MLSD often accepts ``/BjxRobot`` while a chroot
    server accepts only ``BjxRobot`` after login.  The UI stores the canonical
    absolute path; this helper only broadens the wire operation and never
    accepts traversal components.  For an FTP account whose configured root is
    ``/``, the leading slash can refer to the *physical* server root rather
    than the login (virtual) root.  Callers can therefore prefer the relative
    spelling while retaining absolute fallbacks.  The optional parent bridge
    is supplied only for the known database-directory aliases after an
    explicit user opt-in.
    """
    normalized = _normalize_remote_path(path, "FTP 文件路径")
    relative = normalized.lstrip("/")
    if not relative:
        # Chrooted FTP servers often reject `/` but accept the virtual-root
        # spellings `.` or `./` after login.  A few panel daemons only accept
        # the omitted-path form (`MLSD`/`NLST` with an empty argument).
        if prefer_relative:
            return (".", "./", "", normalized)
        return (normalized, ".", "./", "")
    # Keep the relative spellings as separate wire attempts.  They are all
    # derived from the validated absolute path and do not widen the scope.
    if prefer_relative:
        variants = [relative, "./" + relative, normalized]
    else:
        variants = [normalized, relative, "./" + relative]
    # An explicitly enabled parent bridge supports FTP accounts whose `/`
    # login root is backed by a physical application directory such as SCUM.
    if allow_virtual_root_parent:
        variants.append("../" + relative)
    return tuple(dict.fromkeys(variants))


def _ftp_prefers_relative_root(client: FTP | FTP_TLS) -> bool:
    """Choose wire-path order from the FTP login directory.

    A chroot/virtual account normally reports ``PWD /`` and only its
    login-relative names are valid.  A non-chroot account may report a real
    directory such as ``/SCUM`` while the user intentionally stores the
    portal root as ``/``; in that case an absolute `/BjxRobot/...` path must
    be attempted first.  Servers without a usable PWD command retain the
    virtual-root default for compatibility.
    """

    try:
        current = _normalize_remote_path(client.pwd(), "FTP 当前目录")
    except Exception:
        return True
    return current == "/"


def _is_database_directory_alias(value: Any) -> bool:
    return str(value or "").casefold() in {
        item.casefold() for item in DATABASE_DIRECTORY_ALIASES
    }


def _database_directory_alias_variants(path: Any) -> tuple[str, ...]:
    """Return safe BjxRobot/BjRobot spellings for one already-scoped path."""

    normalized = _normalize_remote_path(path, "数据库文件路径")
    components = normalized.strip("/").split("/")
    if not components:
        return (normalized,)
    # The approved database directory may be below a physical prefix, such
    # as `/data/BjxRobot/beijixiong.db`, rather than component zero.
    if _is_database_directory_alias(components[-1]):
        alias_index = len(components) - 1
    elif len(components) >= 2 and _is_database_directory_alias(components[-2]):
        alias_index = len(components) - 2
    else:
        return (normalized,)
    suffix = (
        "/" + "/".join(components[alias_index + 1 :])
        if alias_index + 1 < len(components)
        else ""
    )
    variants: list[str] = []
    for directory_name in (components[alias_index], *DATABASE_DIRECTORY_ALIASES):
        candidate_components = list(components)
        candidate_components[alias_index] = directory_name
        directory_name = "/".join(candidate_components[: alias_index + 1])
        candidate = _normalize_remote_path(
            "/" + directory_name + suffix, "数据库文件路径"
        )
        if candidate not in variants:
            variants.append(candidate)
    return tuple(variants)


def _physical_database_path_variants(
    client: FTP | FTP_TLS,
    remote_path: Any,
    *,
    enabled: bool = False,
    directory_path: bool = False,
) -> tuple[str, ...]:
    """Derive a bounded physical sibling path from the FTP login PWD.

    Some panel FTP accounts report a real login directory such as
    ``/home/wwwroot/SCUM`` but reject both ``CWD ..`` and wire paths containing
    ``../``.  If the account still permits absolute paths, the sibling database
    can be reached at ``/home/wwwroot/BjxRobot/beijixiong.db``.  This helper is
    intentionally *not* part of ordinary path browsing: callers must opt in,
    and only the two known database-directory aliases are accepted.

    The returned paths are transport-only candidates.  The portal continues to
    persist and display the logical path supplied by the user.
    """

    if not enabled:
        return ()
    try:
        normalized = _normalize_remote_path(
            remote_path, "FTP 鏁版嵁搴撹矾寰?"
        )
        parts = normalized.strip("/").split("/")
        if directory_path:
            if not parts or not _is_database_directory_alias(parts[-1]):
                return ()
            suffix = ""
        else:
            if len(parts) < 2 or not _is_database_directory_alias(parts[-2]):
                return ()
            suffix = "/" + parts[-1]
        current = _normalize_remote_path(
            client.pwd(), "FTP 褰撳墠鐩綍"
        )
    except (PortalError, AttributeError, FtpProtocolError, OSError, EOFError):
        return ()
    # A virtual/chroot root has no physical prefix to derive.  In that case
    # the regular relative or CWD bridge remains responsible for resolution.
    if current == "/":
        return ()
    parent = posixpath.dirname(current.rstrip("/")) or "/"
    requested_alias = parts[-1] if directory_path else parts[-2]
    candidates: list[str] = []
    for alias in (requested_alias, *DATABASE_DIRECTORY_ALIASES):
        if not _is_database_directory_alias(alias):
            continue
        candidate = _normalize_remote_path(
            posixpath.join(parent, alias) + suffix,
            "FTP 鏁版嵁搴撹矾寰?",
        )
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def _is_known_database_directory_path(path: Any) -> bool:
    """Whether a path starts with the only approved parent-directory alias."""

    normalized = _normalize_remote_path(path, "数据库文件路径")
    components = normalized.strip("/").split("/")
    return bool(
        components
        and (
            _is_database_directory_alias(components[-1])
            or (
                len(components) >= 2
                and _is_database_directory_alias(components[-2])
            )
        )
    )


def _find_database_directory_entry(
    entries: tuple[FtpDirectoryEntry, ...],
    requested_name: Any = DEFAULT_SIBLING_DATABASE_DIRECTORY,
) -> FtpDirectoryEntry | None:
    """Find the conventional database directory, including its old alias."""

    requested = str(requested_name or "").casefold()
    exact = next(
        (
            entry
            for entry in entries
            if entry.kind == "dir" and entry.name.casefold() == requested
        ),
        None,
    )
    if exact is not None or not _is_database_directory_alias(requested_name):
        return exact
    return next(
        (
            entry
            for entry in entries
            if entry.kind == "dir" and _is_database_directory_alias(entry.name)
        ),
        None,
    )


def _resolve_ftp_case_path(
    client: FTP | FTP_TLS,
    path: Any,
    *,
    prefer_relative_root: bool = False,
) -> str:
    """Resolve path components using the FTP server's spelling.

    Windows FTP servers commonly expose ``SCUM`` while users enter ``scum``.
    Resolving each component from a bounded directory listing keeps the UI
    case-friendly without treating arbitrary user input as a traversal path.
    If a server cannot list a component, the normalized spelling is returned
    and the subsequent FTP operation reports the actual permission/path error.
    """

    normalized = _normalize_remote_path(path)
    if normalized == "/":
        return normalized
    current = "/"
    for component in normalized.strip("/").split("/"):
        try:
            listing = _list_ftp_directory_with_client(
                client,
                current,
                prefer_relative=prefer_relative_root,
            )
        except PortalError:
            return normalized
        match = next(
            (
                entry
                for entry in listing.entries
                if entry.name.casefold() == component.casefold()
            ),
            None,
        )
        if match is None and _is_database_directory_alias(component):
            # Accept `/BjRobot/...` and `/BjxRobot/...` as a narrow legacy
            # spelling pair, but only when the FTP listing proves that one of
            # those names exists in the current account scope.
            match = _find_database_directory_entry(listing.entries, component)
        if match is None:
            return normalized
        current = _normalize_remote_path(
            posixpath.join(current, match.name), "FTP 文件路径"
        )
    return current


def _resolve_scoped_ftp_path(
    client: FTP | FTP_TLS,
    root: Any,
    candidate: Any,
    label: str,
    allow_parent_directory: bool = False,
    parent_path_depth: int | None = None,
    prefer_relative_root: bool | None = None,
) -> str:
    """Resolve an FTP path's spelling, then enforce the configured scope.

    FTP paths are commonly case-sensitive on Linux and case-insensitive on
    Windows.  The web form may therefore contain ``/scum`` while the server
    returns ``/SCUM``.  Validate the user spelling first (so a path cannot use
    case resolution to escape the scope), resolve each component using the
    server listing, and validate the resolved spelling again before returning
    it to callers.  The returned value is always the path that should be used
    for subsequent CWD/RETR operations and persistence.
    """

    requested_root = _normalize_remote_path(root or "/", "FTP 根目录")
    requested_candidate = _normalize_remote_path(candidate, label)
    _path_within_ftp_root(
        requested_root,
        requested_candidate,
        label,
        allow_parent_directory,
        parent_path_depth=parent_path_depth,
        case_insensitive=True,
    )
    if prefer_relative_root is None:
        prefer_relative_root = (
            requested_root == "/" and _ftp_prefers_relative_root(client)
        )
    resolved_root = _resolve_ftp_case_path(
        client, requested_root, prefer_relative_root=prefer_relative_root
    )
    resolved_candidate = _resolve_ftp_case_path(
        client, requested_candidate, prefer_relative_root=prefer_relative_root
    )
    return _path_within_ftp_root(
        resolved_root,
        resolved_candidate,
        label,
        allow_parent_directory,
        parent_path_depth=parent_path_depth,
        case_insensitive=True,
    )


def _list_ftp_directory_with_client(
    client: FTP | FTP_TLS,
    directory: str,
    *,
    prefer_relative: bool = False,
    allow_virtual_root_parent: bool = False,
) -> FtpDirectoryListing:
    """List one directory using MLSD, with a conservative NLST fallback."""

    normalized = _normalize_remote_path(directory)
    entries: list[FtpDirectoryEntry] = []
    truncated = False
    try:
        rows = None
        last_error: Exception | None = None
        for wire_path in _ftp_path_variants(
            normalized,
            prefer_relative=prefer_relative,
            allow_virtual_root_parent=allow_virtual_root_parent,
        ):
            try:
                rows = client.mlsd(wire_path, facts=["type", "size"])
                break
            except (FtpProtocolError, OSError, EOFError) as exc:
                last_error = exc
        if rows is None:
            if isinstance(last_error, FtpProtocolError):
                raise last_error
            raise error_perm("550 directory unavailable")
        for raw_name, facts in rows:
            name = _safe_ftp_entry_name(raw_name)
            if not name:
                continue
            facts = facts if isinstance(facts, Mapping) else {}
            kind = str(facts.get("type", "")).lower()
            if kind in {"cdir", "pdir"}:
                continue
            if kind not in {"dir", "file"}:
                # Unknown MLSD entries are shown as files only when their
                # suffix can be selected as a SQLite database.
                kind = "file"
            raw_size = facts.get("size")
            try:
                size = int(raw_size) if raw_size is not None else None
            except (TypeError, ValueError):
                size = None
            entries.append(
                FtpDirectoryEntry(name, _ftp_child_path(normalized, name), kind, size)
            )
            if len(entries) >= MAX_FTP_LIST_ENTRIES:
                truncated = True
                break
    except (AttributeError, FtpProtocolError):
        # Old FTP daemons do not implement MLSD.  NLST has no type facts, so a
        # size probe is used to distinguish regular files from directories.
        try:
            names = None
            last_error: Exception | None = None
            for wire_path in _ftp_path_variants(
                normalized,
                prefer_relative=prefer_relative,
                allow_virtual_root_parent=allow_virtual_root_parent,
            ):
                try:
                    names = client.nlst(wire_path)
                    break
                except (FtpProtocolError, OSError, EOFError) as exc:
                    last_error = exc
            if names is None:
                if isinstance(last_error, FtpProtocolError):
                    raise last_error
                raise error_perm("550 directory unavailable")
        except FtpProtocolError as exc:
            raise _ftp_permission_error(exc, directory=True) from exc
        for raw_name in names:
            name = _safe_ftp_entry_name(raw_name)
            if not name:
                continue
            path = _ftp_child_path(normalized, name)
            kind = "dir"
            size: int | None = None
            for wire_path in _ftp_path_variants(
                path,
                prefer_relative=prefer_relative,
                allow_virtual_root_parent=allow_virtual_root_parent,
            ):
                try:
                    raw_size = client.size(wire_path)
                    size = int(raw_size) if raw_size is not None else None
                    kind = "file"
                    break
                except Exception:
                    # A directory (or a server that disallows SIZE) remains
                    # navigable; the user can still select files manually.
                    continue
            entries.append(FtpDirectoryEntry(name, path, kind, size))
            if len(entries) >= MAX_FTP_LIST_ENTRIES:
                truncated = True
                break

    entries.sort(key=lambda item: (item.kind != "dir", item.name.casefold()))
    parent = posixpath.dirname(normalized.rstrip("/")) or "/"
    return FtpDirectoryListing(normalized, parent, tuple(entries), truncated)


def _list_configured_ftp_root_with_client(
    client: FTP | FTP_TLS,
    configured_root: str,
    *,
    prefer_relative: bool = False,
) -> FtpDirectoryListing:
    """List a saved FTP root, including panel accounts rooted at that folder.

    Some BaoTa FTP accounts report ``PWD /`` even though login lands directly
    inside the configured ``SCUM`` folder.  In that layout neither
    ``/SCUM`` nor ``SCUM`` is a valid wire path, but ``.`` is.  Try ordinary
    canonical paths first; only after they fail and the server explicitly
    reports a virtual root, list ``.`` and rebase returned entries onto the
    saved logical root.
    """

    normalized_root = _normalize_ftp_root(configured_root)
    try:
        return _list_ftp_directory_with_client(
            client,
            normalized_root,
            prefer_relative=prefer_relative,
        )
    except PortalError:
        # A panel account can report a physical login path (`/SCUM` or
        # `/data/SCUM`) but reject every absolute-path listing.  The saved
        # portal root is often `/` in that layout; if the saved root is the
        # login directory (or the logical virtual root), a relative `.` is
        # still an exact and safe way to list it.
        try:
            login_root = _normalize_remote_path(client.pwd(), "FTP current directory")
        except Exception:
            login_root = ""
        can_rebase_login_root = (
            normalized_root == "/"
            or (
                bool(login_root)
                and normalized_root.casefold() == login_root.casefold()
            )
        )
        if not (can_rebase_login_root or _ftp_prefers_relative_root(client)):
            raise
    login_listing = _list_ftp_directory_with_client(
        client,
        ".",
        prefer_relative=True,
    )
    return FtpDirectoryListing(
        path=normalized_root,
        parent_path=_ftp_parent_path(normalized_root),
        entries=tuple(
            FtpDirectoryEntry(
                entry.name,
                _ftp_child_path(normalized_root, entry.name),
                entry.kind,
                entry.size,
            )
            for entry in login_listing.entries
        ),
        truncated=login_listing.truncated,
    )


def _list_database_directory_with_client(
    client: FTP | FTP_TLS,
    directory: str,
    *,
    prefer_relative: bool = False,
    allow_virtual_root_parent: bool = False,
    allow_physical_parent: bool = False,
) -> FtpDirectoryListing:
    """List a database directory with the narrow legacy-name fallback."""

    last_error: PortalError | None = None
    for candidate in _database_directory_alias_variants(directory):
        try:
            return _list_ftp_directory_with_client(
                client,
                candidate,
                prefer_relative=prefer_relative,
                allow_virtual_root_parent=(
                    allow_virtual_root_parent
                    and _is_known_database_directory_path(candidate)
                ),
            )
        except PortalError as exc:
            last_error = exc
    # Some BaoTa FTP accounts expose a physical PWD (for example
    # ``/home/wwwroot/SCUM``), reject both ``CWD ..`` and ``../...`` paths,
    # but still allow an absolute physical sibling path.  Keep this fallback
    # opt-in and limited to the approved database aliases.
    if allow_virtual_root_parent and allow_physical_parent:
        for candidate in _physical_database_path_variants(
            client,
            directory,
            enabled=True,
            directory_path=True,
        ):
            try:
                physical_listing = _list_ftp_directory_with_client(
                    client,
                    candidate,
                    prefer_relative=False,
                )
                # Physical transport paths must never leak into the portal
                # state.  Rebase the entries onto the logical path the user
                # selected (normally `/BjxRobot`).
                logical_directory = _normalize_remote_path(
                    directory, "FTP 鐩綍"
                )
                logical_entries = tuple(
                    FtpDirectoryEntry(
                        entry.name,
                        _ftp_child_path(logical_directory, entry.name),
                        entry.kind,
                        entry.size,
                    )
                    for entry in physical_listing.entries
                )
                return FtpDirectoryListing(
                    path=logical_directory,
                    parent_path=posixpath.dirname(
                        logical_directory.rstrip("/")
                    )
                    or "/",
                    entries=logical_entries,
                    truncated=physical_listing.truncated,
                )
            except PortalError as exc:
                last_error = exc
    if allow_virtual_root_parent:
        for candidate in _database_directory_alias_variants(directory):
            try:
                listing = _list_database_directory_via_parent_cwd(client, candidate)
            except PortalError as exc:
                last_error = exc
                continue
            if listing is not None:
                return listing
    if last_error is not None:
        raise last_error
    raise PortalError("FTP 目录不可访问，请检查路径和账号权限")


def _ftp_parent_cwd_target(
    client: FTP | FTP_TLS,
    directory: str,
) -> tuple[str, str] | None:
    """Enter one approved sibling directory through ``CWD ..``.

    BaoTa/Serv-U accounts may expose the login directory as ``/`` (or its
    physical spelling, such as ``/SCUM``), while rejecting absolute paths and
    wire paths containing ``../``.  Keep this bridge limited to one direct
    parent and the known database directory aliases.  The returned tuple is
    the original PWD and the server spelling used after entering the sibling.
    """

    normalized = _normalize_remote_path(directory, "FTP directory")
    components = normalized.strip("/").split("/")
    if not components or not _is_database_directory_alias(components[-1]):
        return None
    try:
        original = str(client.pwd() or "")
    except Exception:
        return None
    if not original:
        return None
    try:
        normalized_original = _normalize_remote_path(
            original, "FTP current directory"
        )
    except PortalError:
        return None
    requested_parent = posixpath.dirname(normalized.rstrip("/")) or "/"
    current_parent = _ftp_parent_path(normalized_original)
    # A saved logical root `/` is intentionally independent of the physical
    # directory reported by a panel account (for example PWD
    # `/home/wwwroot/SCUM`).  In that one-component case, CWD `..` followed by
    # the approved alias is still a bounded sibling lookup.  Non-root saved
    # paths retain the strict same-parent check.
    logical_root_sibling = requested_parent == "/" and len(components) == 1
    # ``PWD /`` normally denotes a chroot/virtual root.  Issuing ``CWD /``
    # there is a no-op on most daemons and, on a few panel implementations,
    # can unexpectedly switch the session namespace without a reliable way to
    # restore it.  The slash fallback is therefore reserved for a physical
    # absolute PWD such as `/home/wwwroot/SCUM`.
    physical_root_sibling = logical_root_sibling and normalized_original != "/"
    if (
        requested_parent.casefold() != current_parent.casefold()
        and not logical_root_sibling
    ):
        # Do not turn the opt-in bridge into a generic path walker.
        return None
    entered = False
    try:
        # Most panel FTP daemons expose the parent through ``CWD ..``.  A
        # smaller set rejects the literal parent component but accepts
        # ``CWD /`` (while still keeping the account inside its virtual
        # namespace).  This fallback is deliberately limited to the logical
        # root bridge above; non-root configured paths must never use it as a
        # generic absolute-path walker.
        try:
            client.cwd("..")
        except (FtpProtocolError, OSError, EOFError):
            if not physical_root_sibling:
                return None
            client.cwd("/")
        target_names = [components[-1]]
        target_names.extend(
            alias
            for alias in DATABASE_DIRECTORY_ALIASES
            if alias.casefold() != components[-1].casefold()
        )
        last_error: FtpProtocolError | None = None
        for target_name in target_names:
            try:
                client.cwd(target_name)
                entered = True
                return original, target_name
            except FtpProtocolError as exc:
                last_error = exc

        # Resolve a server-specific case spelling, but only for the approved
        # database aliases.  This is a bounded listing of the parent, not a
        # recursive scan.
        try:
            rows = list(client.mlsd(".", facts=["type"]))
        except (AttributeError, FtpProtocolError, OSError, EOFError):
            try:
                rows = [(name, {}) for name in client.nlst(".")]
            except (AttributeError, FtpProtocolError, OSError, EOFError):
                rows = []
        for raw_name, facts in rows:
            name = _safe_ftp_entry_name(raw_name)
            if not name or not _is_database_directory_alias(name):
                continue
            facts = facts if isinstance(facts, Mapping) else {}
            if str(facts.get("type", "")).lower() not in {"", "dir", "cdir", "pdir"}:
                continue
            try:
                client.cwd(name)
                entered = True
                return original, name
            except FtpProtocolError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
    except (FtpProtocolError, OSError, EOFError):
        return None
    finally:
        if not entered:
            _restore_ftp_pwd(client, original)
    return None


def _restore_ftp_pwd(client: FTP | FTP_TLS, original: str | None) -> None:
    """Best-effort restoration after a bounded parent CWD bridge."""

    if not original:
        return
    try:
        client.cwd(original)
        try:
            restored = _normalize_remote_path(
                client.pwd(), "FTP current directory"
            )
            expected = _normalize_remote_path(
                original, "FTP current directory"
            )
            if restored.casefold() == expected.casefold():
                return
        except Exception:
            # If PWD is unavailable, retaining the historical best-effort
            # behavior is safer than issuing an unbounded traversal.
            return
    except Exception:
        pass
    # Some panel daemons reject absolute CWD even though PWD reports an
    # absolute physical path.  Reconstruct the route from the current
    # directory using individual CWD commands; e.g. `/data/BJXROBOT` back to
    # `/data/SCUM` becomes `CWD ..` then `CWD SCUM` rather than a rejected
    # `CWD /data/SCUM`.
    try:
        current = _normalize_remote_path(client.pwd(), "FTP current directory")
        target = _normalize_remote_path(original, "FTP current directory")
        if current.casefold() == target.casefold():
            return
        current_parts = current.strip("/").split("/") if current != "/" else []
        target_parts = target.strip("/").split("/") if target != "/" else []
        shared = 0
        for current_part, target_part in zip(current_parts, target_parts):
            if current_part.casefold() != target_part.casefold():
                break
            shared += 1
        for _ in current_parts[shared:]:
            client.cwd("..")
        for part in target_parts[shared:]:
            client.cwd(part)
        return
    except Exception:
        pass
    # Virtual-root daemons may reject the absolute PWD returned as ``/``.
    try:
        client.cwd(".")
    except Exception:
        pass


def _list_database_directory_via_parent_cwd(
    client: FTP | FTP_TLS, directory: str
) -> FtpDirectoryListing | None:
    """List a known sibling after stepping to the FTP parent with CWD.

    Some BaoTa/Serv-U accounts permit ``CWD ..`` followed by ``CWD BjxRobot``
    but reject a wire path containing ``..``.  This fallback is restricted to
    the known database directory aliases and is called only after the user
    explicitly enables parent-directory access.
    """

    original: str | None = None
    try:
        entered = _ftp_parent_cwd_target(client, directory)
        if entered is None:
            return None
        original, _ = entered
        normalized = _normalize_remote_path(directory, "FTP directory")
        try:
            rows = list(client.mlsd(".", facts=["type", "size"]))
        except (AttributeError, FtpProtocolError, OSError, EOFError):
            try:
                rows = [(name, {}) for name in client.nlst(".")]
            except (AttributeError, FtpProtocolError, OSError, EOFError) as exc:
                if isinstance(exc, FtpProtocolError):
                    raise _ftp_permission_error(exc, directory=True) from exc
                raise PortalError("FTP directory listing failed") from exc

        entries: list[FtpDirectoryEntry] = []
        for raw_name, facts in rows:
            name = _safe_ftp_entry_name(raw_name)
            if not name:
                continue
            facts = facts if isinstance(facts, Mapping) else {}
            kind = str(facts.get("type", "")).lower()
            if kind in {"cdir", "pdir"}:
                continue
            if kind not in {"dir", "file"}:
                kind = "file"
            raw_size = facts.get("size")
            try:
                size = int(raw_size) if raw_size is not None else None
            except (TypeError, ValueError):
                size = None
            if size is None and kind == "file":
                try:
                    size = int(client.size(name))
                except Exception:
                    pass
            entries.append(
                FtpDirectoryEntry(name, _ftp_child_path(normalized, name), kind, size)
            )
            if len(entries) >= MAX_FTP_LIST_ENTRIES:
                break
        entries.sort(key=lambda item: (item.kind != "dir", item.name.casefold()))
        return FtpDirectoryListing(
            normalized,
            posixpath.dirname(normalized.rstrip("/")) or "/",
            tuple(entries),
            len(entries) >= MAX_FTP_LIST_ENTRIES,
        )
    except FtpProtocolError as exc:
        raise _ftp_permission_error(exc, directory=True) from exc
    finally:
        _restore_ftp_pwd(client, original)


def _list_parent_directory_via_cwd(
    client: FTP | FTP_TLS,
    configured_root: str,
) -> FtpDirectoryListing | None:
    """List the one-level parent of ``configured_root`` using ``CWD ..``.

    A number of panel FTP accounts expose ``/`` as the configured application
    directory (for example ``SCUM``), while the sibling ``BjxRobot`` directory
    is reachable only after a real ``CWD ..``.  Such servers commonly reject
    both ``../BjxRobot`` and an absolute physical path.  Keep this bridge
    deliberately narrow: callers pass the already-normalized configured root,
    we move up exactly once, list the resulting directory, and restore the
    original working directory before returning.

    Returned paths remain logical portal paths; the current FTP working
    directory is never exposed to the caller or persisted.
    """

    normalized_root = _normalize_ftp_root(configured_root)
    if normalized_root == "/":
        # The configured root is already the logical root.  A parent bridge is
        # still useful for virtual-root accounts whose physical login folder
        # is SCUM, but it must not be used when the server cannot report/restore
        # its current directory safely.
        parent_path = "/"
    else:
        parent_path = _ftp_parent_path(normalized_root)

    original: str | None = None
    try:
        try:
            original = client.pwd()
        except Exception:
            # Without PWD there is no safe way to restore the session after a
            # CWD traversal.  The regular absolute/relative path attempts can
            # still be used by the caller.
            return None
        try:
            original_normalized = _normalize_remote_path(
                original, "FTP 褰撳墠鐩綍"
            )
        except PortalError:
            return None
        try:
            client.cwd("..")
        except (FtpProtocolError, OSError, EOFError):
            # Some panel FTP servers reject ``CWD ..`` but permit ``CWD /``
            # when the saved logical root is `/`.  Keep this fallback scoped
            # to that root bridge; a non-root scope must not become an
            # unrestricted absolute-path browser.
            if normalized_root != "/" or original_normalized == "/":
                return None
            client.cwd("/")
        listing = _list_ftp_directory_with_client(
            client,
            ".",
            # After CWD .., a relative dot is the only spelling that is
            # portable across chrooted and panel-managed FTP daemons.
            prefer_relative=True,
        )
        entries = tuple(
            FtpDirectoryEntry(
                entry.name,
                _ftp_child_path(parent_path, entry.name),
                entry.kind,
                entry.size,
            )
            for entry in listing.entries
        )
        return FtpDirectoryListing(
            path=parent_path,
            parent_path=_ftp_parent_path(parent_path),
            entries=entries,
            truncated=listing.truncated,
        )
    except (FtpProtocolError, OSError, EOFError, PortalError):
        return None
    finally:
        _restore_ftp_pwd(client, original)


def _database_ftp_wire_path_variants(
    remote_path: str,
    *,
    client: FTP | FTP_TLS | None = None,
    prefer_relative: bool = False,
    allow_virtual_root_parent: bool = False,
    allow_physical_parent: bool = False,
) -> tuple[str, ...]:
    """Build safe wire paths for a selected database file."""

    variants: list[str] = []
    if client is not None and allow_virtual_root_parent and allow_physical_parent:
        # Try the physical sibling only after the normal logical spellings.
        # This keeps the canonical `/BjxRobot/...` path stable while covering
        # panel accounts that reject CWD traversal but permit absolute paths.
        physical_candidates = _physical_database_path_variants(
            client,
            remote_path,
            enabled=True,
            directory_path=False,
        )
    else:
        physical_candidates = ()
    for candidate in _database_directory_alias_variants(remote_path):
        for wire_path in _ftp_path_variants(
            candidate,
            prefer_relative=prefer_relative,
            allow_virtual_root_parent=(
                allow_virtual_root_parent
                and _is_known_database_directory_path(candidate)
            ),
        ):
            if wire_path not in variants:
                variants.append(wire_path)
    for candidate in physical_candidates:
        if candidate not in variants:
            variants.append(candidate)
    return tuple(variants)


def get_saved_ftp_connection_config(
    db: Session,
    settings: Settings,
    owner_id: int | None = None,
    *,
    group_id: Any = None,
    profile_id: Any = None,
) -> FtpConnectionConfig:
    """Read a user's FTP credentials for a short-lived worker operation."""

    return _read_ftp_config(
        db,
        settings,
        owner_id,
        group_id=group_id,
        profile_id=profile_id,
    )


def browse_ftp_directory(
    config: FtpConnectionConfig,
    path: str | None = None,
    *,
    group_id: Any = None,
    profile_id: Any = None,
) -> FtpDirectoryListing:
    # The connection is already resolved, but accepting and validating the
    # scope lets route/service layers forward one uniform set of arguments.
    _normalize_scope(group_id, profile_id)
    with _open_ftp(config) as client:
        configured_root = _normalize_ftp_root(config.remote_path)
        # A saved `/` is the account's logical root.  Prefer relative wire
        # paths so FTP daemons that expose a physical root at `/` still see
        # directories visible after login (for example `BjxRobot`).
        prefer_relative = (
            configured_root == "/" and _ftp_prefers_relative_root(client)
        )
        requested_directory = _normalize_remote_path(
            path or configured_root, "FTP 目录"
        )
        # Resolve both paths before listing so returned paths use the spelling
        # exposed by the FTP server (for example ``/SCUM`` rather than the
        # user's ``/scum`` input).
        directory = _resolve_scoped_ftp_path(
            client,
            configured_root,
            requested_directory,
            "FTP 目录",
            config.allow_parent_directory,
            # A parent bridge plus a direct sibling file has two components,
            # e.g. `BjxRobot/beijixiong.db`.
            parent_path_depth=2,
            prefer_relative_root=prefer_relative,
        )
        configured_root = _resolve_ftp_case_path(
            client,
            configured_root,
            prefer_relative_root=prefer_relative,
        )
        parent_root = _ftp_parent_path(configured_root)
        is_parent_bridge_view = (
            config.allow_parent_directory
            and configured_root != "/"
            and directory == parent_root
        )
        # MLSD/NLST already verifies that the directory can be read.  Avoid a
        # separate CWD here: changing the connection's current directory makes
        # later relative fallbacks resolve against that directory instead of
        # the FTP login root on chrooted/panel-managed servers.
        try:
            if directory.casefold() == configured_root.casefold():
                listing = _list_configured_ftp_root_with_client(
                    client,
                    configured_root,
                    prefer_relative=prefer_relative,
                )
            else:
                listing = _list_database_directory_with_client(
                    client,
                    directory,
                    prefer_relative=prefer_relative,
                    allow_virtual_root_parent=(
                        config.allow_parent_directory
                        and _is_known_database_directory_path(directory)
                    ),
                    allow_physical_parent=(
                        config.allow_parent_directory and configured_root == "/"
                    ),
                )
        except PortalError as exc:
            if is_parent_bridge_view:
                # A panel may reject every direct parent spelling while still
                # permitting CWD .. followed by a listing from the parent.
                listing = _list_parent_directory_via_cwd(client, configured_root)
                if listing is None:
                    raise PortalError(
                    "FTP 账号无法访问根目录上一级；请确认登录后根目录同时可见 SCUM 和 BjxRobot"
                    ) from exc
            else:
                raise
        entries = listing.entries
        parent_path = listing.parent_path
        # A virtual FTP root can itself be backed by SCUM.  When opted in,
        # expose only the known sibling database directory from its physical
        # parent so users can select `BjxRobot/beijixiong.db` without changing
        # the saved root to an invalid absolute server path.
        if (
            config.allow_parent_directory
            and directory == parent_root
            and _find_database_directory_entry(entries) is None
        ):
            # Do not rely solely on a parent listing: a BaoTa FTP account may
            # allow `CWD ..; CWD BjxRobot` yet deny a listing of the parent
            # itself.  Probe only the documented sibling aliases and add a
            # navigable directory entry when that narrow probe succeeds.
            sibling_path = _ftp_child_path(
                parent_root, DEFAULT_SIBLING_DATABASE_DIRECTORY
            )
            try:
                sibling_listing = _list_database_directory_with_client(
                    client,
                    sibling_path,
                    prefer_relative=prefer_relative,
                    allow_virtual_root_parent=(parent_root == "/"),
                    allow_physical_parent=(
                        config.allow_parent_directory and configured_root == "/"
                    ),
                )
            except PortalError:
                sibling_listing = None
            if sibling_listing is not None:
                sibling_name = posixpath.basename(sibling_listing.path)
                known_entry = FtpDirectoryEntry(
                    sibling_name,
                    sibling_listing.path,
                    "dir",
                )
                existing_names = {entry.name.casefold() for entry in entries}
                if known_entry.name.casefold() not in existing_names:
                    entries = tuple(
                        sorted(
                            (*entries, known_entry),
                            key=lambda item: (item.kind != "dir", item.name.casefold()),
                        )
                    )
        root_prefix = configured_root.rstrip("/") + "/"
        is_configured_tree = (
            directory == configured_root or directory.startswith(root_prefix)
        )
        if not config.allow_parent_directory and directory == configured_root:
            parent_path = directory
        elif (
            config.allow_parent_directory
            and configured_root != "/"
            and directory == parent_root
        ):
            # The parent is only a bridge to select a database.  Show the
            # configured root and direct SQLite files, but do not disclose
            # unrelated regular files.  Direct sibling folders remain
            # navigable only one level so a file such as
            # /BjxRobot/beijixiong.db can be selected.
            entries = tuple(
                entry
                for entry in entries
                if entry.path == configured_root
                or (entry.kind == "dir")
                or entry.is_sqlite_database
            )
            parent_path = configured_root
        elif config.allow_parent_directory and not is_configured_tree:
            # A direct sibling directory may only expose SQLite files.  The
            # selected file itself is validated and downloaded separately.
            entries = tuple(entry for entry in entries if entry.is_sqlite_database)
        return FtpDirectoryListing(
            path=listing.path,
            parent_path=parent_path,
            entries=entries,
            truncated=listing.truncated,
        )


def find_default_ftp_database(
    config: FtpConnectionConfig,
    filename: str = DEFAULT_SQLITE_FILENAME,
    max_depth: int = 4,
    *,
    group_id: Any = None,
    profile_id: Any = None,
) -> str | None:
    """Find a named SQLite file in the configured tree and optional sibling.

    With parent access enabled, the parent itself and the conventional
    ``BjxRobot`` sibling directory are checked.  The search intentionally only
    inspects the configured directory and that one named sibling.  Walking all
    children (or every sibling) makes a slow FTP server hold the HTTP request
    open and can select a same-named database from an unrelated application.

    ``max_depth`` is retained for API compatibility with older callers.  The
    configured tree keeps a small depth-limited search, while the parent is
    never recursively scanned.  Users who keep their database in another
    sibling directory can browse to it and select it explicitly.
    """

    _normalize_scope(group_id, profile_id)
    wanted = _safe_ftp_entry_name(filename).casefold()
    if not wanted:
        raise PortalError("默认数据库文件名无效")
    # Keep the public argument while making the bounded behaviour explicit.
    # The recursive portion below is limited to the configured tree and a
    # small directory budget; parent siblings are never queued.
    root = _normalize_ftp_root(config.remote_path)
    parent_listing_failed = False
    with _open_ftp(config) as client:
        prefer_relative = root == "/" and _ftp_prefers_relative_root(client)
        root = _resolve_ftp_case_path(
            client, root, prefer_relative_root=prefer_relative
        )
        # Search the configured directory first.  This also lets automatic
        # selection work when the account can read its configured directory
        # but cannot read the optional parent directory.
        root_listing = _list_configured_ftp_root_with_client(
            client, root, prefer_relative=prefer_relative
        )
        for entry in root_listing.entries:
            if entry.kind == "file" and entry.name.casefold() == wanted:
                return entry.path
        # When the configured root is the FTP account's virtual `/`,
        # ``BjxRobot`` is a direct child and remains inside the normal scope.
        # Check that one conventional directory without recursively walking
        # every top-level folder.
        sibling = _find_database_directory_entry(root_listing.entries)
        if sibling is not None and root == "/":
            try:
                sibling_listing = _list_database_directory_with_client(
                    client,
                    sibling.path,
                    prefer_relative=prefer_relative,
                    allow_virtual_root_parent=config.allow_parent_directory,
                    allow_physical_parent=(
                        config.allow_parent_directory and root == "/"
                    ),
                )
            except PortalError:
                sibling_listing = None
            if sibling_listing is not None:
                for entry in sibling_listing.entries:
                    if entry.kind == "file" and entry.name.casefold() == wanted:
                        return entry.path

        # Some panel FTP accounts expose `/` as the SCUM login directory and
        # hide its sibling `BjxRobot` from the top listing, while still
        # permitting an explicitly enabled one-level parent lookup.  Try the
        # two known database directory spellings without recursively scanning
        # or accepting user-supplied `..` components.
        if root == "/" and config.allow_parent_directory and sibling is None:
            try:
                sibling_listing = _list_database_directory_with_client(
                    client,
                    "/BjxRobot",
                    prefer_relative=prefer_relative,
                    allow_virtual_root_parent=True,
                    allow_physical_parent=True,
                )
            except PortalError:
                sibling_listing = None
            if sibling_listing is not None:
                for entry in sibling_listing.entries:
                    if entry.kind == "file" and entry.name.casefold() == wanted:
                        return entry.path

        if config.allow_parent_directory and root != "/":
            parent_root = _ftp_parent_path(root)
            try:
                parent_listing = _list_ftp_directory_with_client(
                    client, parent_root, prefer_relative=prefer_relative
                )
            except PortalError:
                parent_listing = None
                parent_listing_failed = True
            if parent_listing is not None:
                for entry in parent_listing.entries:
                    if entry.kind == "file" and entry.name.casefold() == wanted:
                        return entry.path
                sibling = _find_database_directory_entry(parent_listing.entries)
                if sibling is not None:
                    try:
                        sibling_listing = _list_ftp_directory_with_client(
                            client, sibling.path, prefer_relative=prefer_relative
                        )
                    except PortalError:
                        sibling_listing = None
                    if sibling_listing is not None:
                        for entry in sibling_listing.entries:
                            if (
                                entry.kind == "file"
                                and entry.name.casefold() == wanted
                            ):
                                return entry.path
            # The normal parent listing can actually be the configured SCUM
            # directory again on panel FTP servers.  Retry the same one-level
            # scope through CWD .., then inspect only the approved database
            # directory aliases.  This is intentionally not a generic parent
            # walker and therefore cannot scan unrelated siblings.
            bridged_parent = _list_parent_directory_via_cwd(client, root)
            if bridged_parent is not None:
                parent_listing_failed = False
                for entry in bridged_parent.entries:
                    if entry.kind == "file" and entry.name.casefold() == wanted:
                        return entry.path
                sibling = _find_database_directory_entry(bridged_parent.entries)
                if sibling is not None:
                    try:
                        sibling_listing = _list_database_directory_with_client(
                            client,
                            sibling.path,
                            prefer_relative=prefer_relative,
                            allow_virtual_root_parent=True,
                        )
                    except PortalError:
                        sibling_listing = None
                    if sibling_listing is not None:
                        for entry in sibling_listing.entries:
                            if entry.kind == "file" and entry.name.casefold() == wanted:
                                return entry.path
            elif parent_listing is None:
                parent_listing_failed = True
            # Listing a physical parent can itself be forbidden even where a
            # direct `CWD ..` followed by the conventional database directory
            # is allowed.  Test that exact, opt-in path before reporting that
            # the sibling database cannot be found.
            try:
                sibling_listing = _list_database_directory_with_client(
                    client,
                    _ftp_child_path(
                        parent_root, DEFAULT_SIBLING_DATABASE_DIRECTORY
                    ),
                    prefer_relative=prefer_relative,
                    allow_virtual_root_parent=(parent_root == "/"),
                )
            except PortalError:
                sibling_listing = None
            if sibling_listing is not None:
                parent_listing_failed = False
                for entry in sibling_listing.entries:
                    if entry.kind == "file" and entry.name.casefold() == wanted:
                        return entry.path
        # Preserve support for a database nested below the configured game
        # directory, but bound the number of directory listings.  The old
        # implementation also queued every parent sibling (up to 64); that is
        # intentionally excluded above because it caused long HTTP waits.
        if root != "/" and max_depth > 0:
            queue: list[tuple[str, int]] = [
                (entry.path, 1)
                for entry in root_listing.entries
                if entry.kind == "dir"
            ]
            seen: set[str] = {root}
            searched_directories = 0
            while queue and searched_directories < MAX_AUTO_DATABASE_DIRECTORIES:
                directory, depth = queue.pop(0)
                if directory in seen:
                    continue
                seen.add(directory)
                searched_directories += 1
                try:
                    listing = _list_ftp_directory_with_client(
                        client, directory, prefer_relative=prefer_relative
                    )
                except PortalError:
                    continue
                for entry in listing.entries:
                    if entry.kind == "file" and entry.name.casefold() == wanted:
                        return entry.path
                    if entry.kind == "dir" and depth < max_depth:
                        queue.append((entry.path, depth + 1))
    if parent_listing_failed:
        raise PortalError(
            "FTP 账号无法访问根目录上一级；请确认登录后根目录同时可见 SCUM 和 BjxRobot"
        )
    return None


def _download_ftp_file(
    client: FTP | FTP_TLS,
    remote_path: str,
    destination: str,
    *,
    prefer_relative: bool = False,
    allow_virtual_root_parent: bool = False,
    allow_physical_parent: bool = False,
) -> int:
    """Download one bounded file and return its byte count."""

    normalized = _normalize_database_file_path(remote_path)
    expected = None
    for wire_path in _database_ftp_wire_path_variants(
        normalized,
        client=client,
        prefer_relative=prefer_relative,
        allow_virtual_root_parent=allow_virtual_root_parent,
        allow_physical_parent=allow_physical_parent,
    ):
        try:
            expected = client.size(wire_path)
            break
        except Exception:
            continue
    if expected is not None and int(expected) > MAX_SQLITE_DOWNLOAD_BYTES:
        raise PortalError("SQLite 数据库文件超过 256 MB，拒绝下载")
    written = 0
    with open(destination, "wb") as output:
        def write_block(block: bytes) -> None:
            nonlocal written
            written += len(block)
            if written > MAX_SQLITE_DOWNLOAD_BYTES:
                raise PortalError("SQLite 数据库文件超过 256 MB，拒绝下载")
            output.write(block)

        last_error: FtpProtocolError | None = None
        for wire_path in _database_ftp_wire_path_variants(
            normalized,
            client=client,
            prefer_relative=prefer_relative,
            allow_virtual_root_parent=allow_virtual_root_parent,
            allow_physical_parent=allow_physical_parent,
        ):
            try:
                client.retrbinary(
                    f"RETR {wire_path}", write_block, blocksize=1024 * 128
                )
                break
            except FtpProtocolError as exc:
                last_error = exc
                # A failed absolute RETR can leave partial bytes in the
                # destination before the daemon rejects the command.
                output.seek(0)
                output.truncate(0)
                written = 0
        else:
            # A few FTP servers reject ``RETR ../BjxRobot/file`` while
            # permitting a safe ``CWD ..`` + ``CWD BjxRobot`` sequence.  Try
            # that sequence only for the approved sibling aliases.
            if allow_virtual_root_parent:
                for candidate in _database_directory_alias_variants(normalized):
                    if not _is_known_database_directory_path(candidate):
                        continue
                    try:
                        output.seek(0)
                        output.truncate(0)
                        written = 0
                        if _download_database_via_parent_cwd(
                            client, candidate, destination, write_block
                        ):
                            return written
                    except PortalError:
                        output.seek(0)
                        output.truncate(0)
                        written = 0
                        continue
            if last_error is not None:
                raise _ftp_permission_error(last_error) from last_error
    return written


def _download_database_via_parent_cwd(
    client: FTP | FTP_TLS,
    remote_path: str,
    destination: str,
    write_block,
) -> bool:
    """Download a known sibling after changing directory with CWD."""

    normalized = _normalize_database_file_path(remote_path)
    components = normalized.strip("/").split("/")
    if len(components) < 2 or not _is_database_directory_alias(components[-2]):
        return False
    original: str | None = None
    try:
        directory = "/" + "/".join(components[:-1])
        entered = _ftp_parent_cwd_target(client, directory)
        if entered is None:
            return False
        original, _ = entered
        # The helper entered the directory itself, so RETR only needs the
        # basename.  This avoids servers that reject both absolute paths and
        # `../` in a RETR command.
        client.retrbinary(
            f"RETR {components[-1]}", write_block, blocksize=1024 * 128
        )
        return True
    except (FtpProtocolError, OSError, EOFError) as exc:
        if isinstance(exc, FtpProtocolError):
            raise _ftp_permission_error(exc) from exc
        raise PortalError("FTP database download failed") from exc
    finally:
        _restore_ftp_pwd(client, original)


def _download_validated_sqlite_database(
    config: FtpConnectionConfig, sqlite_path: str
) -> tuple[str, bytes]:
    """Download and validate one FTP SQLite file, returning its bounded bytes.

    The existing selection flow only needed the resolved remote path and
    discarded the temporary download.  Snapshot synchronization needs the
    exact validated bytes as well, so keep the validation in one place and
    expose a small internal helper.  The FTP downloader enforces the global
    256 MiB limit before this function reads the file into memory.
    """

    requested_path = _normalize_database_file_path(sqlite_path)
    requested_root = _normalize_ftp_root(config.remote_path)
    temporary_path = ""
    resolved_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix="joker-ftp-db-", suffix=".db", delete=False
        ) as temporary:
            temporary_path = temporary.name
        with _open_ftp(config) as client:
            prefer_relative = (
                requested_root == "/" and _ftp_prefers_relative_root(client)
            )
            resolved_path = _resolve_scoped_ftp_path(
                client,
                requested_root,
                requested_path,
                "数据库文件路径",
                config.allow_parent_directory,
                prefer_relative_root=prefer_relative,
            )
            _download_ftp_file(
                client,
                resolved_path,
                temporary_path,
                prefer_relative=prefer_relative,
                allow_virtual_root_parent=config.allow_parent_directory,
                allow_physical_parent=(
                    config.allow_parent_directory and requested_root == "/"
                ),
            )
        with open(temporary_path, "rb") as source:
            # Read one byte past the hard limit as a defence in depth check in
            # case a custom FTP client bypasses _download_ftp_file.
            data = source.read(MAX_SQLITE_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_SQLITE_DOWNLOAD_BYTES:
            raise PortalError("SQLite 数据库文件超过 256 MB，拒绝下载")
        if data[:16] != b"SQLite format 3\x00":
            raise PortalError("所选文件不是有效的 SQLite 数据库")
        # The downloaded copy is disposable and must be opened read-only.  A
        # query-only pragma is kept as a second guard for SQLite clients that
        # ignore the URI mode flag.
        connection = sqlite3.connect(f"file:{temporary_path}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        finally:
            connection.close()
        return resolved_path, data
    except sqlite3.DatabaseError as exc:
        raise PortalError("SQLite 数据库文件无法打开或已损坏") from exc
    except OSError as exc:
        raise PortalError("SQLite 数据库临时文件无法读取") from exc
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def fetch_validated_ftp_database(
    config: FtpConnectionConfig,
    sqlite_path: str,
    *,
    group_id: Any = None,
    profile_id: Any = None,
) -> tuple[str, bytes]:
    """Return a validated FTP SQLite snapshot for portal persistence."""

    _normalize_scope(group_id, profile_id)
    return _download_validated_sqlite_database(config, sqlite_path)


def validate_ftp_database_steam_id(
    db: Session,
    settings: Settings,
    owner_id: int,
    group_id: Any,
    profile_id: Any,
    steam_id: str,
) -> dict[str, Any]:
    """Check one SteamID against the latest FTP SQLite source on Linux."""

    profile = normalize_integration_profile_id(profile_id)
    config = _read_ftp_config(db, settings, owner_id, group_id=group_id, profile_id=profile)
    database = _read_database_config(db, settings, owner_id, group_id=group_id, profile_id=profile)
    if not config.enabled or database.engine != "sqlite" or not database.enabled or not database.sqlite_path:
        return {
            "success": False,
            "code": "db_unavailable",
            "message": "当前服务器尚未配置可用的 SQLite 数据库。",
            "validation_source": "ftp_latest",
        }
    try:
        try:
            from ..scum.database import lookup_user_list_steam_id
        except ImportError:
            from scum.database import lookup_user_list_steam_id
    except ImportError as exc:
        return {
            "success": False,
            "code": "query_error",
            "message": "Linux 服务器缺少 SteamID 数据库查询组件，请更新网页后台安装包。",
            "detail": type(exc).__name__,
            "validation_source": "ftp_latest",
        }
    normalized_steam = str(steam_id or "").strip()
    temporary_path = ""
    try:
        resolved_path, original = _download_validated_sqlite_database(config, database.sqlite_path)
        del resolved_path
        with tempfile.NamedTemporaryFile(prefix="joker-steam-lookup-", suffix=".db", delete=False) as temporary:
            temporary_path = temporary.name
            temporary.write(original)
        result = lookup_user_list_steam_id(temporary_path, normalized_steam)
        if not isinstance(result, dict):
            return {
                "success": False,
                "code": "query_error",
                "message": "服务器数据库返回格式无效。",
                "validation_source": "ftp_latest",
            }
        normalized_result = dict(result)
        normalized_result.setdefault("validation_source", "ftp_latest")
        return normalized_result
    except PortalError as exc:
        return {
            "success": False,
            "code": "db_unavailable",
            "message": str(exc)[:300] or "服务器 FTP 数据库暂不可用。",
            "detail": type(exc).__name__,
            "validation_source": "ftp_latest",
        }
    except (FtpProtocolError, socket.timeout, TimeoutError, OSError, sqlite3.DatabaseError) as exc:
        return {
            "success": False,
            "code": "query_error",
            "message": "服务器数据库下载或读取失败，请检查 FTP 路径与数据库文件。",
            "detail": type(exc).__name__,
            "validation_source": "ftp_latest",
        }
    except Exception as exc:
        return {
            "success": False,
            "code": "query_error",
            "message": "服务器数据库查询组件执行失败，请更新网页后台并重试。",
            "detail": type(exc).__name__,
            "validation_source": "ftp_latest",
        }
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def _rename_ftp_database_member_unlocked(
    db: Session,
    settings: Settings,
    owner_id: int,
    group_id: Any,
    profile_id: Any,
    *,
    steam_id: str,
    player_name: str,
) -> dict[str, Any]:
    """Rename one existing player in the live FTP database with CAS upload."""

    profile = normalize_integration_profile_id(profile_id)
    config = _read_ftp_config(db, settings, owner_id, group_id=group_id, profile_id=profile)
    database = _read_database_config(db, settings, owner_id, group_id=group_id, profile_id=profile)
    if not config.enabled or database.engine != "sqlite" or not database.enabled or not database.sqlite_path:
        return {"success": False, "code": "db_unavailable", "message": "当前服务器尚未配置可写入的 SQLite 数据库。"}
    normalized_steam = str(steam_id or "").strip()
    normalized_name = str(player_name or "").strip()[:255]
    if not re.fullmatch(r"[0-9]{17}", normalized_steam) or not normalized_name:
        return {"success": False, "code": "invalid_rename", "message": "SteamID 或玩家名称无效。"}
    try:
        from ..scum.player_database import update_player_name
    except ImportError:
        from scum.player_database import update_player_name

    for attempt in range(2):
        resolved_path, original = _download_validated_sqlite_database(config, database.sqlite_path)
        original_hash = hashlib.sha256(original).hexdigest()
        temporary_path = ""
        try:
            with tempfile.NamedTemporaryFile(prefix="joker-database-rename-", suffix=".db", delete=False) as temporary:
                temporary_path = temporary.name
                temporary.write(original)
            result = update_player_name(temporary_path, normalized_steam, normalized_name)
            if not isinstance(result, dict) or result.get("success") is not True:
                return result if isinstance(result, dict) else {
                    "success": False, "code": "database_mutation_failed", "message": "服务器数据库改名未完成。"
                }
            changed = Path(temporary_path).read_bytes()
            upload_validated_ftp_database(config, resolved_path, changed, expected_sha256=original_hash)
            saved = save_database_snapshot(db, owner_id, group_id, profile, changed)
            result = dict(result)
            result.update({"database_mode": "portal_transaction", "snapshot_revision": saved.get("revision", 0), "snapshot_sha256": saved.get("sha256", "")})
            return result
        except PortalError as exc:
            if "已被其他来源更新" in str(exc) and attempt == 0:
                continue
            return {"success": False, "code": "database_mutation_conflict" if "已被其他来源更新" in str(exc) else "database_mutation_failed", "message": str(exc)}
        except (OSError, sqlite3.DatabaseError) as exc:
            return {"success": False, "code": "database_mutation_failed", "message": "服务器数据库改名失败，请检查 FTP 数据库权限。", "detail": type(exc).__name__}
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
    return {"success": False, "code": "database_mutation_conflict", "message": "服务器数据库正在被其他人修改，请稍后重试。"}


def rename_ftp_database_member(*args, **kwargs) -> dict[str, Any]:
    """Serialize one authoritative player-name update per FTP database."""

    if len(args) < 5:
        raise TypeError("database rename requires db, settings, owner, group and profile")
    profile = normalize_integration_profile_id(args[4])
    config = _read_ftp_config(args[0], args[1], args[2], group_id=args[3], profile_id=profile)
    database = _read_database_config(args[0], args[1], args[2], group_id=args[3], profile_id=profile)
    with _ftp_database_lock(config, database.sqlite_path):
        return _rename_ftp_database_member_unlocked(*args, **kwargs)


def _upload_validated_ftp_database_unlocked(
    config: FtpConnectionConfig,
    sqlite_path: str,
    database_data: bytes,
    *,
    expected_sha256: str = "",
) -> str:
    """Replace one validated SQLite file in the configured FTP scope.

    Windows owns the transaction that changes the local snapshot.  The portal
    calls this helper afterwards so FTP credentials never cross the sync
    boundary.  ``expected_sha256`` is checked against the remote file before
    STOR to avoid overwriting a newer server database with an old snapshot.
    """

    if not isinstance(database_data, bytes):
        raise PortalError("SQLite 数据库快照格式无效")
    if len(database_data) > MAX_SQLITE_DOWNLOAD_BYTES:
        raise PortalError("SQLite 数据库文件超过 256 MB，拒绝上传")
    if database_data[:16] != b"SQLite format 3\x00":
        raise PortalError("SQLite 数据库快照格式无效")
    expected = str(expected_sha256 or "").strip().lower()
    if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise PortalError("数据库快照校验值无效")

    requested_path = _normalize_database_file_path(sqlite_path)
    requested_root = _normalize_ftp_root(config.remote_path)
    with _open_ftp(config) as client:
        prefer_relative = (
            requested_root == "/" and _ftp_prefers_relative_root(client)
        )
        resolved_path = _resolve_scoped_ftp_path(
            client,
            requested_root,
            requested_path,
            "数据库文件路径",
            config.allow_parent_directory,
            parent_path_depth=2,
            prefer_relative_root=prefer_relative,
        )

        # Read the current remote bytes first.  This is intentionally bounded
        # and uses the same path variants as the existing download flow.
        remote_digest = ""
        temporary_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                prefix="joker-ftp-current-", suffix=".db", delete=False
            ) as temporary:
                temporary_path = temporary.name
            _download_ftp_file(
                client,
                resolved_path,
                temporary_path,
                prefer_relative=prefer_relative,
                allow_virtual_root_parent=(
                    config.allow_parent_directory
                    and _is_known_database_directory_path(resolved_path)
                ),
                allow_physical_parent=(
                    config.allow_parent_directory and requested_root == "/"
                ),
            )
            hasher = hashlib.sha256()
            with open(temporary_path, "rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(block)
            remote_digest = hasher.hexdigest()
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
        if expected and remote_digest != expected:
            raise PortalError(
                "FTP 数据库已被其他来源更新，请先重新下载数据库后再签到"
            )

        variants = _database_ftp_wire_path_variants(
            resolved_path,
            client=client,
            prefer_relative=prefer_relative,
            allow_virtual_root_parent=(
                config.allow_parent_directory
                and _is_known_database_directory_path(resolved_path)
            ),
            allow_physical_parent=(
                config.allow_parent_directory and requested_root == "/"
            ),
        )
        last_error: FtpProtocolError | None = None
        for wire_path in variants:
            try:
                client.storbinary(
                    f"STOR {wire_path}",
                    BytesIO(database_data),
                    blocksize=1024 * 128,
                )
                return resolved_path
            except FtpProtocolError as exc:
                last_error = exc

        # Match the download fallback for panel FTP accounts that allow CWD
        # into the known database directory but reject a path containing ..
        # or an absolute physical path.
        if config.allow_parent_directory and _is_known_database_directory_path(
            resolved_path
        ):
            components = resolved_path.strip("/").split("/")
            if len(components) >= 2:
                entered = _ftp_parent_cwd_target(
                    client, "/" + "/".join(components[:-1])
                )
                if entered is not None:
                    original, _ = entered
                    try:
                        client.storbinary(
                            f"STOR {components[-1]}",
                            BytesIO(database_data),
                            blocksize=1024 * 128,
                        )
                        return resolved_path
                    except FtpProtocolError as exc:
                        last_error = exc
                    finally:
                        _restore_ftp_pwd(client, original)
        if last_error is not None:
            raise _ftp_permission_error(last_error) from last_error
    raise PortalError("FTP 数据库上传失败")


def upload_validated_ftp_database(
    config: FtpConnectionConfig,
    sqlite_path: str,
    database_data: bytes,
    *,
    expected_sha256: str = "",
) -> str:
    """Upload one SQLite file while serializing writes per FTP database target."""

    with _ftp_database_lock(config, sqlite_path):
        return _upload_validated_ftp_database_unlocked(
            config,
            sqlite_path,
            database_data,
            expected_sha256=expected_sha256,
        )


def _test_sqlite_database(config: FtpConnectionConfig, sqlite_path: str) -> str:
    """Validate a selected remote file and return its resolved path."""

    resolved_path, _ = _download_validated_sqlite_database(config, sqlite_path)
    return resolved_path


def validate_ftp_database_file(
    config: FtpConnectionConfig,
    sqlite_path: str,
    *,
    group_id: Any = None,
    profile_id: Any = None,
) -> str:
    """Validate a selected remote file before its path is persisted."""

    _normalize_scope(group_id, profile_id)
    return _test_sqlite_database(config, sqlite_path)


def save_ftp_database_selection(
    db: Session,
    owner_id: int,
    remote_path: str,
    operator_id: int | None,
    source: str = "manual",
    *,
    group_id: Any = None,
    profile_id: Any = None,
) -> dict[str, Any]:
    """Persist a SQLite file selected from the user's FTP directory."""

    previous = _payload(db, owner_id, for_update=True)
    _, _, previous_ftp, previous_database = _scoped_sections(
        previous, group_id, profile_id
    )
    group_scope, requested_scope_profile = _normalize_scope(group_id, profile_id)
    selected_scope_profile = _resolve_profile_id(
        previous, group_scope, requested_scope_profile
    )
    previous_profile = (
        _group_profiles(previous, group_scope).get(selected_scope_profile)
        if group_scope is not None
        else {}
    )
    uses_independent_ftp = isinstance(previous_profile, Mapping) and bool(
        _profile_ftp_connection_id(previous_profile)
    )
    ftp = dict(previous_ftp)
    if not bool(ftp.get("enabled", False)):
        raise PortalError("请先保存并启用 FTP 连接")
    # Keep the account's configured scope stable when selecting a file.
    # Older records may have omitted/blank `remote_path`; those mean the
    # virtual FTP root and must not be changed to the database directory.
    ftp_root = _normalize_ftp_root(ftp.get("remote_path", "/"))
    ftp["remote_path"] = ftp_root
    path = _path_within_ftp_root(
        ftp_root,
        _normalize_database_file_path(remote_path),
        "数据库文件路径",
        bool(ftp.get("allow_parent_directory", ftp.get("allow_parent", False))),
        case_insensitive=True,
    )
    normalized_source = str(source or "manual").strip().lower()
    if normalized_source not in {"manual", "auto", "ftp"}:
        raise PortalError("数据库选择方式无效")
    database = dict(previous_database)
    database.update(
        {
            "enabled": True,
            "engine": "sqlite",
            "sqlite_path": path,
            "database": posixpath.basename(path),
            "display_name": (
                str(database.get("display_name", "") or "").strip()
                or posixpath.basename(path)
            ),
            "host": "",
            "port": DEFAULT_DATABASE_PORT,
            "username": "",
            "password_ciphertext": "",
            "selection_source": "auto" if normalized_source == "auto" else "ftp",
        }
    )
    payload, _, selected_profile = _set_scoped_sections(
        previous,
        group_id=group_id,
        profile_id=profile_id,
        ftp=None if uses_independent_ftp else ftp,
        database=database,
    )
    _save_payload(db, owner_id, payload, operator_id)
    return get_integration_config_view(
        db,
        owner_id,
        group_id=group_id,
        profile_id=selected_profile if group_id is not None else None,
    )


def save_database_snapshot(
    db: Session,
    owner_id: int,
    group_id: Any,
    profile_id: Any,
    database_data: bytes,
) -> dict[str, Any]:
    """Persist a validated SQLite snapshot and publish its revision metadata.

    Snapshot bytes are deliberately keyed by the same owner/group/profile
    scope as the encrypted FTP settings.  A checksum-identical selection is
    idempotent and keeps its revision, while changed bytes advance the
    revision exactly once.  The profile metadata is updated in the same
    transaction so a Windows client can never receive a new checksum without
    a corresponding row to download.
    """

    try:
        normalized_owner = int(owner_id)
    except (TypeError, ValueError, OverflowError):
        raise PortalError("用户身份无效") from None
    if normalized_owner <= 0:
        raise PortalError("用户身份无效")
    group, requested_profile = _normalize_scope(group_id, profile_id)
    if group is None:
        raise PortalError("数据库快照必须绑定群配置")
    if not isinstance(database_data, (bytes, bytearray, memoryview)):
        raise PortalError("SQLite 数据库快照格式无效")
    data = bytes(database_data)
    if len(data) > MAX_SQLITE_DOWNLOAD_BYTES:
        raise PortalError("SQLite 数据库文件超过 256 MB，拒绝保存")
    if data[:16] != b"SQLite format 3\x00":
        raise PortalError("所选文件不是有效的 SQLite 数据库")
    digest = hashlib.sha256(data).hexdigest()

    # Lock the settings row before checking the profile and update its public
    # metadata together with the snapshot row.  This mirrors the existing
    # profile read-modify-write discipline in ``save_integration_config``.
    payload = _payload(db, normalized_owner, for_update=True)
    profile = (
        normalize_integration_profile_id(requested_profile)
        if requested_profile is not None
        else _resolve_profile_id(payload, group, None)
    )
    profile_record = _group_profiles(payload, group).get(profile)
    if not isinstance(profile_record, Mapping):
        raise PortalError("服务器不存在")

    statement = (
        select(PortalDatabaseSnapshot)
        .where(
            PortalDatabaseSnapshot.owner_user_id == normalized_owner,
            PortalDatabaseSnapshot.group_id == group,
            PortalDatabaseSnapshot.profile_id == profile,
        )
        .with_for_update()
    )
    snapshot = db.scalar(statement)
    previous_revision = 0
    previous_sha256 = ""
    if snapshot is not None:
        try:
            previous_revision = max(0, int(snapshot.revision or 0))
        except (TypeError, ValueError, OverflowError):
            previous_revision = 0
        previous_sha256 = str(snapshot.sha256 or "").strip().lower()
    changed = snapshot is None or str(snapshot.sha256 or "").lower() != digest
    if snapshot is None:
        snapshot = PortalDatabaseSnapshot(
            owner_user_id=normalized_owner,
            group_id=group,
            profile_id=profile,
            revision=1,
            sha256=digest,
            size_bytes=len(data),
            database_data=data,
            updated_at=utcnow(),
        )
        db.add(snapshot)
        revision = 1
    else:
        try:
            current_revision = max(0, int(snapshot.revision or 0))
        except (TypeError, ValueError, OverflowError):
            current_revision = 0
        if changed and current_revision >= 2**31 - 1:
            raise PortalError("数据库快照版本已达到上限，请清理后重新选择数据库")
        revision = current_revision + 1 if changed else max(1, current_revision)
        if changed:
            snapshot.revision = revision
            snapshot.sha256 = digest
            snapshot.size_bytes = len(data)
            snapshot.database_data = data
            snapshot.updated_at = utcnow()
        else:
            # Repair metadata created by an older migration without changing
            # the observable revision or rewriting a large BLOB.
            if int(snapshot.size_bytes or 0) != len(data):
                snapshot.size_bytes = len(data)
    updated_payload = deepcopy(dict(payload))
    profiles = deepcopy(dict(_profiles_payload(updated_payload)))
    group_profiles = deepcopy(dict(_group_profiles(updated_payload, group)))
    current_profile = deepcopy(dict(group_profiles.get(profile) or {}))
    current_profile["snapshot_revision"] = revision
    current_profile["snapshot_sha256"] = digest
    group_profiles[profile] = current_profile
    profiles[group] = group_profiles
    updated_payload["profiles"] = profiles
    _save_payload(db, normalized_owner, updated_payload, normalized_owner)
    db.flush()
    return {
        "group_id": group,
        "profile_id": profile,
        "revision": revision,
        "sha256": digest,
        "size_bytes": len(data),
        "changed": changed,
        "previous_revision": previous_revision,
        "previous_sha256": previous_sha256,
    }


def _mutate_ftp_database_signin_unlocked(
    db: Session,
    settings: Settings,
    owner_id: int,
    group_id: Any,
    profile_id: Any,
    *,
    steam_id: str,
    reward: int,
    player_name: str = "",
    sign_date: str = "",
    currency_column: str = "amount",
    period_mode: str = "daily",
    reward_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Mutate the current FTP SQLite source without receiving a full file.

    The portal serializes its own writers, downloads the latest source into a
    private temporary file, commits the reward transaction there, then uploads
    it only when the FTP source hash is unchanged. A newer portal request or
    external update is therefore rejected rather than silently accepted as a
    successful stale write.
    """

    profile = normalize_integration_profile_id(profile_id)
    config = _read_ftp_config(
        db, settings, owner_id, group_id=group_id, profile_id=profile
    )
    database = _read_database_config(
        db, settings, owner_id, group_id=group_id, profile_id=profile
    )
    if not config.enabled or database.engine != "sqlite" or not database.enabled:
        return {"success": False, "code": "database_ftp_not_configured", "message": "当前服务器尚未启用 SQLite FTP 数据库。"}
    if not database.sqlite_path:
        return {"success": False, "code": "database_ftp_not_configured", "message": "当前服务器尚未选择 SQLite 数据库文件。"}
    try:
        from ..scum.player_database import apply_signin_reward
    except ImportError:
        from scum.player_database import apply_signin_reward

    for attempt in range(2):
        resolved_path, original = _download_validated_sqlite_database(
            config, database.sqlite_path
        )
        original_hash = hashlib.sha256(original).hexdigest()
        temporary_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                prefix="joker-database-mutation-", suffix=".db", delete=False
            ) as temporary:
                temporary_path = temporary.name
                temporary.write(original)
            result = apply_signin_reward(
                temporary_path,
                steam_id,
                reward,
                player_name=player_name,
                sign_date=sign_date,
                currency_column=currency_column,
                period_mode=period_mode,
                reward_config=reward_config,
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                return result if isinstance(result, dict) else {
                    "success": False,
                    "code": "database_mutation_failed",
                    "message": "服务器数据库事务未完成。",
                }
            changed = Path(temporary_path).read_bytes()
            upload_validated_ftp_database(
                config,
                resolved_path,
                changed,
                expected_sha256=original_hash,
            )
            saved = save_database_snapshot(
                db, owner_id, group_id, profile, changed
            )
            result = dict(result)
            result.update(
                {
                    "database_mode": "portal_transaction",
                    "snapshot_revision": saved.get("revision", 0),
                    "snapshot_sha256": saved.get("sha256", ""),
                }
            )
            return result
        except PortalError as exc:
            if "已被其他来源更新" in str(exc) and attempt == 0:
                continue
            return {
                "success": False,
                "code": "database_mutation_conflict" if "已被其他来源更新" in str(exc) else "database_mutation_failed",
                "message": str(exc),
            }
        except (OSError, sqlite3.DatabaseError) as exc:
            return {
                "success": False,
                "code": "database_mutation_failed",
                "message": "服务器数据库事务失败，请检查 FTP 数据库权限。",
                "detail": type(exc).__name__,
            }
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
    return {"success": False, "code": "database_mutation_conflict", "message": "服务器数据库正在被其他人修改，请稍后重试。"}


def mutate_ftp_database_signin(*args, **kwargs) -> dict[str, Any]:
    """Serialize one server-side sign-in transaction per FTP database."""

    if len(args) < 5:
        raise TypeError("database mutation requires db, settings, owner, group and profile")
    database_profile = args[4]
    db = args[0]
    settings = args[1]
    owner_id = args[2]
    group_id = args[3]
    profile = normalize_integration_profile_id(database_profile)
    config = _read_ftp_config(db, settings, owner_id, group_id=group_id, profile_id=profile)
    database = _read_database_config(db, settings, owner_id, group_id=group_id, profile_id=profile)
    with _ftp_database_lock(config, database.sqlite_path):
        return _mutate_ftp_database_signin_unlocked(*args, **kwargs)


def _reset_ftp_database_signin_unlocked(
    db: Session,
    settings: Settings,
    owner_id: int,
    group_id: Any,
    profile_id: Any,
    *,
    steam_id: str,
    sign_date: str,
    reward: int | None = None,
    remove_reward: bool = True,
    currency_column: str = "amount",
) -> dict[str, Any]:
    """Delete one sign-in row in the latest FTP database with CAS protection."""

    profile = normalize_integration_profile_id(profile_id)
    config = _read_ftp_config(db, settings, owner_id, group_id=group_id, profile_id=profile)
    database = _read_database_config(db, settings, owner_id, group_id=group_id, profile_id=profile)
    if not config.enabled or database.engine != "sqlite" or not database.enabled or not database.sqlite_path:
        return {"success": False, "code": "database_ftp_not_configured", "message": "当前服务器尚未配置可写入的 FTP SQLite 数据库。"}
    try:
        from ..scum.player_database import reset_signin_record
    except ImportError:
        from scum.player_database import reset_signin_record

    for attempt in range(2):
        resolved_path, original = _download_validated_sqlite_database(config, database.sqlite_path)
        original_hash = hashlib.sha256(original).hexdigest()
        temporary_path = ""
        try:
            with tempfile.NamedTemporaryFile(prefix="joker-database-reset-", suffix=".db", delete=False) as temporary:
                temporary_path = temporary.name
                temporary.write(original)
            result = reset_signin_record(
                temporary_path,
                steam_id,
                sign_date,
                reward=reward,
                remove_reward=remove_reward,
                currency_column=currency_column,
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                return result if isinstance(result, dict) else {
                    "success": False,
                    "code": "database_mutation_failed",
                    "message": "服务器签到记录未完成删除。",
                }
            changed = Path(temporary_path).read_bytes()
            upload_validated_ftp_database(config, resolved_path, changed, expected_sha256=original_hash)
            saved = save_database_snapshot(db, owner_id, group_id, profile, changed)
            result = dict(result)
            result.update({
                "database_mode": "portal_transaction",
                "snapshot_revision": saved.get("revision", 0),
                "snapshot_sha256": saved.get("sha256", ""),
            })
            return result
        except PortalError as exc:
            if "已被其他来源更新" in str(exc) and attempt == 0:
                continue
            return {
                "success": False,
                "code": "database_mutation_conflict" if "已被其他来源更新" in str(exc) else "database_mutation_failed",
                "message": str(exc),
            }
        except (OSError, sqlite3.DatabaseError) as exc:
            return {
                "success": False,
                "code": "database_mutation_failed",
                "message": "服务器数据库事务失败，请检查 FTP 数据库权限。",
                "detail": type(exc).__name__,
            }
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
    return {"success": False, "code": "database_mutation_conflict", "message": "服务器数据库正在被其他人修改，请稍后重试。"}


def reset_ftp_database_signin(*args, **kwargs) -> dict[str, Any]:
    """Serialize one server-side sign-in reset per FTP database."""

    if len(args) < 5:
        raise TypeError("database reset requires db, settings, owner, group and profile")
    database_profile = args[4]
    db = args[0]
    settings = args[1]
    owner_id = args[2]
    group_id = args[3]
    profile = normalize_integration_profile_id(database_profile)
    config = _read_ftp_config(db, settings, owner_id, group_id=group_id, profile_id=profile)
    database = _read_database_config(db, settings, owner_id, group_id=group_id, profile_id=profile)
    with _ftp_database_lock(config, database.sqlite_path):
        return _reset_ftp_database_signin_unlocked(*args, **kwargs)


def _test_ftp(config: FtpConnectionConfig) -> None:
    with _open_ftp(config) as client:
        try:
            requested_root = _normalize_ftp_root(config.remote_path)
            prefer_relative = (
                requested_root == "/" and _ftp_prefers_relative_root(client)
            )
            root = _resolve_ftp_case_path(
                client,
                requested_root,
                prefer_relative_root=prefer_relative,
            )
            last_error: FtpProtocolError | None = None
            for wire_path in _ftp_path_variants(
                root, prefer_relative=prefer_relative
            ):
                try:
                    client.cwd(wire_path)
                    last_error = None
                    break
                except FtpProtocolError as exc:
                    last_error = exc
            if (
                last_error is not None
                and requested_root != "/"
                and _ftp_prefers_relative_root(client)
            ):
                # A panel may treat its login directory as virtual `/` while
                # the saved logical root is `/SCUM`.  The authenticated
                # current directory is then the only valid root spelling.
                try:
                    client.cwd(".")
                    last_error = None
                except FtpProtocolError as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
            client.voidcmd("TYPE I")
        except FtpProtocolError as exc:
            raise _ftp_permission_error(exc, directory=True) from exc


def _database_url(config: BusinessDatabaseConfig) -> str:
    if config.engine != "mysql":
        raise PortalError("当前只支持 MySQL 业务数据库连接")
    host = config.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return (
        f"mysql+pymysql://{quote_plus(config.username)}:{quote_plus(config.password)}"
        f"@{host}:{config.port}/{quote_plus(config.database)}"
        f"?charset={quote_plus(config.charset)}&connect_timeout=10"
        "&read_timeout=10&write_timeout=10"
    )


def _database_failure_message(error: BaseException) -> str:
    """Classify a SQLAlchemy/PyMySQL failure without exposing its text."""

    current: BaseException | None = error
    seen: set[int] = set()
    code = ""
    for _ in range(6):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return "业务数据库域名解析失败，请检查服务器地址"
        if isinstance(current, (socket.timeout, TimeoutError)):
            return "业务数据库连接超时，请检查端口、防火墙和白名单"
        if isinstance(current, ConnectionRefusedError):
            return "业务数据库端口拒绝连接，请确认 MySQL 服务已开放"
        if isinstance(current, ssl.SSLError):
            return "业务数据库 TLS 握手失败，请检查加密连接设置"
        if isinstance(current, ModuleNotFoundError):
            return "网页环境缺少 PyMySQL 驱动，请重新安装部署依赖"
        args = getattr(current, "args", ())
        if isinstance(args, tuple) and args:
            candidate = args[0]
            if isinstance(candidate, int):
                code = str(candidate)
        original = getattr(current, "orig", None)
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        if isinstance(original, BaseException):
            current = original
        elif isinstance(cause, BaseException):
            current = cause
        elif isinstance(context, BaseException):
            current = context
        else:
            current = None
    if code in {"1045", "1698"}:
        return "业务数据库账号或密码不正确"
    if code == "1049":
        return "业务数据库名称不存在"
    if code in {"1044", "1142"}:
        return "业务数据库账号没有访问该库的权限"
    return "业务数据库连接失败，请检查 MySQL 地址、端口、库名和账号"


def create_business_database_engine(
    db: Session,
    settings: Settings,
    owner_id: int | None = None,
    *,
    group_id: Any = None,
    profile_id: Any = None,
) -> Engine:
    """Create a short-lived engine for future business features.

    Callers must dispose the returned engine after their operation.  ``NullPool``
    prevents external credentials or connections from being retained by every
    web worker.
    """

    config = _read_database_config(
        db,
        settings,
        owner_id,
        group_id=group_id,
        profile_id=profile_id,
    )
    if not config.enabled:
        raise PortalError("业务数据库功能尚未启用")
    if config.engine == "sqlite":
        raise PortalError(
            "SQLite 数据库位于 FTP，请使用 open_business_database_engine 读取"
        )
    if not config.password or not config.host or not config.database:
        raise PortalError("MySQL 业务数据库配置不完整")
    return create_engine(
        _database_url(config),
        poolclass=NullPool,
        pool_pre_ping=True,
        future=True,
    )


@contextmanager
def open_business_database_engine(
    db: Session,
    settings: Settings,
    owner_id: int | None = None,
    *,
    group_id: Any = None,
    profile_id: Any = None,
):
    """Yield a disposable MySQL engine or a temporary FTP-backed SQLite one.

    SQLite files selected through FTP cannot be represented by a permanent
    local SQLAlchemy URL.  This context manager downloads a bounded copy and
    removes it after the caller disposes the engine.
    """

    config = _read_database_config(
        db,
        settings,
        owner_id,
        group_id=group_id,
        profile_id=profile_id,
    )
    if not config.enabled:
        raise PortalError("业务数据库功能尚未启用")
    if config.engine == "mysql":
        engine = create_business_database_engine(
            db,
            settings,
            owner_id,
            group_id=group_id,
            profile_id=profile_id,
        )
        try:
            yield engine
        finally:
            engine.dispose()
        return
    if config.engine != "sqlite":
        raise PortalError("数据库类型仅支持 MySQL 或 SQLite 文件")
    if not config.sqlite_path:
        raise PortalError("尚未选择 SQLite 数据库文件")
    ftp_config = _read_ftp_config(
        db,
        settings,
        owner_id,
        group_id=group_id,
        profile_id=profile_id,
    )
    requested_root = _normalize_ftp_root(ftp_config.remote_path)
    requested_sqlite_path = _normalize_database_file_path(config.sqlite_path)
    _path_within_ftp_root(
        requested_root,
        requested_sqlite_path,
        "数据库文件路径",
        ftp_config.allow_parent_directory,
        case_insensitive=True,
    )
    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix="joker-ftp-db-", suffix=".db", delete=False
        ) as temporary:
            temporary_path = temporary.name
        with _open_ftp(ftp_config) as client:
            prefer_relative = (
                requested_root == "/" and _ftp_prefers_relative_root(client)
            )
            resolved_root = _resolve_ftp_case_path(
                client,
                requested_root,
                prefer_relative_root=prefer_relative,
            )
            sqlite_path = _resolve_ftp_case_path(
                client,
                requested_sqlite_path,
                prefer_relative_root=prefer_relative,
            )
            sqlite_path = _path_within_ftp_root(
                resolved_root,
                sqlite_path,
                "数据库文件路径",
                ftp_config.allow_parent_directory,
                case_insensitive=True,
            )
            _download_ftp_file(
                client,
                sqlite_path,
                temporary_path,
                prefer_relative=prefer_relative,
                allow_virtual_root_parent=(
                    ftp_config.allow_parent_directory
                ),
                allow_physical_parent=(
                    ftp_config.allow_parent_directory and requested_root == "/"
                ),
            )
        engine = create_engine(
            f"sqlite+pysqlite:///file:{temporary_path}?mode=ro",
            connect_args={"uri": True},
            poolclass=NullPool,
            future=True,
        )
        try:
            yield engine
        finally:
            engine.dispose()
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def test_integration_connection(
    db: Session,
    settings: Settings,
    target: str,
    owner_id: int | None = None,
    *,
    group_id: Any = None,
    profile_id: Any = None,
) -> str:
    """Test one saved integration without exposing provider error details."""

    normalized_target = str(target or "both").strip().lower()
    if normalized_target not in {"ftp", "database", "both"}:
        raise PortalError("连接测试目标无效")
    view = get_integration_config_view(
        db,
        owner_id,
        group_id=group_id,
        profile_id=profile_id,
    )
    server_name = str(
        view.get("profile_name") or view.get("profile_id") or DEFAULT_PROFILE_ID
    ).strip() or DEFAULT_PROFILE_ID
    database_view = view.get("database", {})
    if not isinstance(database_view, Mapping):
        database_view = {}
    database_name = str(
        database_view.get("display_name")
        or database_view.get("database")
        or database_view.get("sqlite_path")
        or "未命名数据库"
    ).strip()
    if normalized_target in {"ftp", "both"}:
        _test_ftp(
            _read_ftp_config(
                db,
                settings,
                owner_id,
                group_id=group_id,
                profile_id=profile_id,
            )
        )
    database_path = ""
    if normalized_target in {"database", "both"}:
        database_config = _read_database_config(
            db,
            settings,
            owner_id,
            group_id=group_id,
            profile_id=profile_id,
        )
        if database_config.engine == "sqlite":
            if not database_config.enabled:
                raise PortalError("业务数据库功能尚未启用")
            if not database_config.sqlite_path:
                raise PortalError("尚未选择 SQLite 数据库文件")
            database_path = _test_sqlite_database(
                _read_ftp_config(
                    db,
                    settings,
                    owner_id,
                    group_id=group_id,
                    profile_id=profile_id,
                ),
                database_config.sqlite_path,
            )
            if normalized_target == "database":
                return (
                    f"服务器「{server_name}」的数据库「{database_name}」"
                    f"连接成功，测试成功（{database_path}）"
                )
        else:
            try:
                engine = create_business_database_engine(
                    db,
                    settings,
                    owner_id,
                    group_id=group_id,
                    profile_id=profile_id,
                )
            except PortalError:
                raise
            except Exception as exc:
                raise PortalError("业务数据库驱动不可用，请确认已安装 MySQL 连接依赖") from exc
            try:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
            except Exception as exc:
                raise PortalError(_database_failure_message(exc)) from exc
            finally:
                engine.dispose()
    if normalized_target == "ftp":
        return f"服务器「{server_name}」的 FTP 连接成功，测试成功"
    if normalized_target == "database":
        return f"服务器「{server_name}」的数据库「{database_name}」连接成功，测试成功"
    if database_path:
        return (
            f"服务器「{server_name}」的 FTP 与数据库「{database_name}」"
            f"连接成功，测试成功（{database_path}）"
        )
    return f"服务器「{server_name}」的 FTP 与数据库「{database_name}」连接成功，测试成功"
