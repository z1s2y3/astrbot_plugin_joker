"""Read-only validation helpers for SCUM player SQLite databases.

The Windows plugin keeps the QQ/Steam binding records locally, while the
game database is only consulted to verify that a submitted SteamID belongs to
the selected server database.  This module deliberately performs no writes
and never returns a database path or provider error to callers.
"""

from __future__ import annotations

import re
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping


# Keep validation byte-compatible with the Windows binding store.  ``\d`` is
# Unicode-aware in Python and would accept numerals that are not SteamID text.
STEAM_ID_PATTERN = re.compile(r"^[0-9]{17}$")
_VALIDATION_MESSAGES = {
    "found": "",
    "invalid_steam_id": "SteamID 必须是 17 位数字。",
    "db_unavailable": "服务器数据库暂不可用，暂时无法验证 SteamID，请稍后重试。",
    "not_found": "该 SteamID 不在当前服务器数据库中，无法绑定。",
    "ambiguous": "服务器数据库中的 SteamID 记录不唯一，请联系管理员。",
    "schema_error": "服务器数据库缺少 user_list.steam_id，无法验证 SteamID，请联系管理员。",
    "query_error": "服务器数据库查询失败，暂时无法验证 SteamID，请稍后重试。",
}


def normalize_database_steam_id(value: object) -> str:
    """Return a canonical 17-digit SteamID or an empty string."""

    candidate = str(value or "").strip()
    return candidate if STEAM_ID_PATTERN.fullmatch(candidate) else ""


def _result(
    code: str,
    steam_id: str = "",
    *,
    player: Mapping[str, Any] | None = None,
    detail: str = "",
) -> dict[str, Any]:
    """Build the small, stable result contract consumed by the binder."""

    normalized_player = dict(player) if isinstance(player, Mapping) else None
    return {
        "success": code == "found",
        "code": code,
        "steam_id": steam_id,
        "player": normalized_player,
        # Keep diagnostics internal; callers may log the code but should use
        # this sanitized message for a group reply.
        "message": _VALIDATION_MESSAGES.get(code, _VALIDATION_MESSAGES["query_error"]),
        "detail": detail,
    }


def _row_to_player(row: object) -> dict[str, str]:
    """Convert sqlite row tuples or mapping-like rows to bounded text.

    The game database versions in the wild do not all expose the optional
    display columns (``user_name``/``nick_name``).  The lookup layer therefore
    passes a mapping when it can, while this helper keeps the tuple fallback
    for callers/tests that use a hand-written DB-API connection.
    """

    if isinstance(row, Mapping):
        get_value = row.get
    else:
        values = tuple(row) if isinstance(row, (tuple, list)) else ()

        def get_value(key: str, default: object = "") -> object:
            index = {"user_id": 0, "user_name": 1, "nick_name": 2, "steam_id": 3}.get(key)
            return values[index] if index is not None and index < len(values) else default

    return {
        "user_id": str(get_value("user_id", "") or "").strip()[:64],
        "user_name": str(get_value("user_name", "") or "").strip()[:255],
        "nick_name": str(get_value("nick_name", "") or "").strip()[:255],
        "steam_id": str(get_value("steam_id", "") or "").strip()[:40],
    }


def _lookup_connection(
    connection: Any,
    normalized_steam_id: str,
    *,
    max_matches: int = 2,
) -> dict[str, Any]:
    """Look up one ID through a DB-API sqlite connection.

    Named SQLite parameters also work with SQLAlchemy's SQLite driver when a
    raw connection is supplied.  The query intentionally caps rows at two so
    duplicate game records are detected without scanning a large table.
    """
    try:
        max_matches = max(2, min(int(max_matches), 100))
    except (TypeError, ValueError, OverflowError):
        max_matches = 2

    try:
        # ``steam_id`` is the only required field.  Older/private server
        # snapshots often omit one or more display columns, and selecting
        # those columns unconditionally made a valid player database look
        # malformed.  Read the schema first and select only columns that are
        # actually present.
        schema_cursor = connection.execute("PRAGMA table_info(user_list)")
        schema_rows = schema_cursor.fetchall()
        if not schema_rows:
            return _result(
                "schema_error",
                normalized_steam_id,
                detail="user_list table is missing",
            )

        available: dict[str, str] = {}
        for schema_row in schema_rows:
            if isinstance(schema_row, Mapping):
                raw_name = schema_row.get("name", "")
            else:
                try:
                    raw_name = schema_row[1]
                except (IndexError, KeyError, TypeError):
                    raw_name = ""
            name = str(raw_name or "").strip()
            if name:
                available.setdefault(name.casefold(), name)

        steam_column = available.get("steam_id")
        if not steam_column:
            return _result(
                "schema_error",
                normalized_steam_id,
                detail="user_list.steam_id column is missing",
            )

        selected_columns = [
            available[key]
            for key in ("user_id", "user_name", "nick_name", "steam_id")
            if key in available
        ]

        def quote_identifier(identifier: str) -> str:
            # Names come from PRAGMA, but quote them anyway so a legacy column
            # name containing punctuation cannot alter the statement.
            return '"' + identifier.replace('"', '""') + '"'

        select_sql = ", ".join(
            f"{quote_identifier(column)} AS {quote_identifier(column.casefold())}"
            for column in selected_columns
        )
        cursor = connection.execute(
            f"""
            SELECT {select_sql}
              FROM user_list
             WHERE TRIM(CAST({quote_identifier(steam_column)} AS TEXT)) = :steam_id
             LIMIT :max_matches
            """,
            {"steam_id": normalized_steam_id, "max_matches": max_matches},
        )
        rows = cursor.fetchall()
        # Map by the cursor's canonical aliases so optional-column layouts
        # still produce a stable player object.
        description = getattr(cursor, "description", None) or ()
        column_names = [str(item[0]).casefold() for item in description]
        mapped_rows = []
        for row in rows:
            if not column_names:
                mapped_rows.append(row)
                continue
            mapped: dict[str, Any] = {}
            for index, name in enumerate(column_names):
                try:
                    mapped[name] = row[index]
                except (IndexError, KeyError, TypeError):
                    continue
            mapped_rows.append(mapped or row)
        rows = mapped_rows
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "no such table" in message or "no such column" in message:
            return _result("schema_error", normalized_steam_id, detail=str(exc))
        return _result("query_error", normalized_steam_id, detail=str(exc))
    except (sqlite3.DatabaseError, OSError, RuntimeError) as exc:
        return _result("query_error", normalized_steam_id, detail=str(exc))
    except Exception as exc:
        # A malformed/closed connection should not leak implementation details
        # into a QQ reply, but it is still distinguishable from a missing row.
        return _result("query_error", normalized_steam_id, detail=type(exc).__name__)

    if not rows:
        return _result("not_found", normalized_steam_id)
    if len(rows) > 1:
        return _result("ambiguous", normalized_steam_id)
    return _result("found", normalized_steam_id, player=_row_to_player(rows[0]))


def lookup_user_list_steam_id(
    database: str | Path | Any,
    steam_id: object,
    *,
    timeout: float = 3.0,
) -> dict[str, Any]:
    """Validate ``steam_id`` against ``user_list.steam_id``.

    ``database`` may be a filesystem path or an already-open sqlite
    connection.  Path access uses SQLite's read-only URI mode, so binding
    validation cannot mutate the uploaded game database.  The returned dict
    always contains ``success``, ``code``, ``message`` and ``player`` keys.
    """

    normalized = normalize_database_steam_id(steam_id)
    if not normalized:
        return _result("invalid_steam_id")

    if isinstance(database, (str, Path)):
        try:
            path = Path(database).expanduser().resolve()
        except (OSError, RuntimeError, TypeError):
            return _result("db_unavailable", normalized)
        if not path.is_file():
            return _result("db_unavailable", normalized)
        connection: sqlite3.Connection | None = None
        try:
            uri = f"file:{path.as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=max(0.1, float(timeout)))
            return _lookup_connection(connection, normalized)
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            return _result("db_unavailable", normalized, detail=type(exc).__name__)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

    # This branch is useful for tests and for callers that already own the
    # short-lived FTP-backed SQLite connection.  The caller remains
    # responsible for closing it.
    if hasattr(database, "execute"):
        return _lookup_connection(database, normalized)
    return _result("db_unavailable", normalized)


def resolve_group_database_path(
    group_config: object,
    *,
    plugin_root: str | Path | None = None,
) -> Path | None:
    """Resolve a group SQLite path from supported config keys.

    Per-group values take precedence. Supported
    keys are ``features.stats.database_path``, ``features.stats.sqlite_path``,
    root-level ``database_path`` and root-level ``sqlite_path``.  Relative
    values are interpreted relative to the plugin root; an explicitly set but
    missing path returns ``None`` rather than silently selecting another
    group's database.
    """

    raw = group_config if isinstance(group_config, Mapping) else {}
    features = raw.get("features")
    stats = features.get("stats") if isinstance(features, Mapping) else None
    scum = features.get("scum") if isinstance(features, Mapping) else None
    game_servers = features.get("game_servers") if isinstance(features, Mapping) else None
    linked_database_profile = ""
    if isinstance(game_servers, Mapping):
        active_game_profile_id = str(
            game_servers.get("active_profile_id", "") or ""
        ).strip()
        raw_game_profiles = game_servers.get("profiles", [])
        if isinstance(raw_game_profiles, str):
            raw_game_profiles = raw_game_profiles.splitlines()
        if not isinstance(raw_game_profiles, (list, tuple)):
            raw_game_profiles = []
        selected_game_profile: Mapping[str, Any] | None = None
        fallback_game_profile: Mapping[str, Any] | None = None
        for raw_item in list(raw_game_profiles)[:100]:
            item = raw_item
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            if not isinstance(item, Mapping):
                continue
            enabled = item.get("enabled", True)
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() not in {"", "0", "false", "off", "no"}
            if not enabled:
                continue
            if fallback_game_profile is None:
                fallback_game_profile = item
            item_id = str(item.get("id", item.get("profile_id", "")) or "").strip()
            if active_game_profile_id and item_id == active_game_profile_id:
                selected_game_profile = item
                break
        selected_game_profile = selected_game_profile or fallback_game_profile
        if selected_game_profile is not None:
            linked_database_profile = str(
                selected_game_profile.get(
                    "integration_profile_id",
                    selected_game_profile.get("database_profile", ""),
                )
                or ""
            ).strip()
    configured: object = None
    profile_sources: list[Mapping[str, Any]] = []
    for section in (stats, scum, raw):
        if isinstance(section, Mapping):
            profile_sources.append(section)
    for section in profile_sources:
        selected_raw = section.get(
            "database_profile",
            section.get(
                "active_database_profile",
                section.get("integration_profile", ""),
            ),
        )
        if section is stats and linked_database_profile:
            selected_raw = linked_database_profile
        if isinstance(selected_raw, Mapping) and "default" in selected_raw:
            selected_raw = selected_raw.get("default")
        selected_profile = str(selected_raw or "").strip()
        profile_requested = bool(selected_profile)
        # The web integration projection calls these ``server_profiles`` on
        # the SCUM feature, while older configs used ``database_profiles`` or
        # ``databases``.  Treat all four names as the same *authoritative*
        # profile collection.  In particular, an empty/disabled collection
        # must fail closed instead of falling back to another server's DB.
        raw_profiles = section.get(
            "database_profiles",
            section.get(
                "server_profiles",
                section.get("databases", section.get("servers", [])),
            ),
        )
        if isinstance(raw_profiles, str):
            profile_items: list[Any] = []
            for line in raw_profiles.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    profile_items.append(json.loads(line))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
        elif isinstance(raw_profiles, (list, tuple)):
            profile_items = list(raw_profiles)
        else:
            profile_items = []
        selected_item: Mapping[str, Any] | None = None
        normalized_profile_items: list[Mapping[str, Any]] = []
        for raw_item in profile_items[:50]:
            item = raw_item
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            if not isinstance(item, Mapping):
                continue
            normalized_profile_items.append(item)
            profile_id = str(
                item.get("id", item.get("profile_id", item.get("server_id", ""))) or ""
            ).strip()
            enabled = item.get("enabled", True)
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() not in {"", "0", "false", "off", "no"}
            if not enabled:
                continue
            if selected_profile and profile_id == selected_profile:
                selected_item = item
                break
            if not selected_profile and selected_item is None:
                selected_item = item
        # A profile collection is authoritative even when every profile is
        # disabled or malformed.  Falling through to the bundled database in
        # that state could validate a SteamID against a different server.
        if selected_item is None and (profile_requested or normalized_profile_items):
            return None
        if selected_item is not None:
            for key in ("database_path", "sqlite_path", "path", "file"):
                value = selected_item.get(key)
                if str(value or "").strip():
                    configured = value
                    break
            # A declared profile without a local path is an explicit
            # "database unavailable" state.  Falling through to the bundled
            # DB here can validate a SteamID against the wrong server.
            if configured is None and (profile_requested or normalized_profile_items):
                return None
        for key in ("database_path", "sqlite_path", "db_path", "user_database"):
            if configured is not None:
                break
            value = section.get(key)
            if str(value or "").strip():
                configured = value
                break
        if configured is not None:
            break
    if configured is None:
        for key in ("database_path", "sqlite_path", "db_path", "user_database"):
            value = raw.get(key)
            if str(value or "").strip():
                configured = value
                break

    # A configured FTP/server profile must provide an actual Windows snapshot
    # before any binding lookup can happen. There is intentionally no bundled
    # database fallback: using a local example file could validate or reward
    # a player against a different remote server.
    if configured is None:
        return None

    try:
        root = Path(plugin_root).resolve() if plugin_root else Path(__file__).resolve().parent.parent
    except (OSError, RuntimeError, TypeError):
        root = Path.cwd()

    text = str(configured).strip()
    if "\x00" in text or len(text) > 1024:
        return None
    # Configured relative paths are FTP-originated Windows snapshots. Reject
    # parent components before resolving so a stale/hand-edited group config
    # cannot make the binder read a file outside this plugin's directory.
    normalized_text = text.replace("\\", "/")
    relative_parts = [part for part in normalized_text.split("/") if part not in {"", "."}]
    candidate = Path(normalized_text).expanduser()
    was_relative = not candidate.is_absolute()
    if was_relative:
        if ".." in relative_parts:
            return None
        candidate = root / candidate
    try:
        candidate = candidate.resolve()
    except (OSError, RuntimeError):
        return None

    if was_relative:
        # Resolve symlinks as well as textual ``..`` components. This keeps
        # remote snapshots confined to the plugin root even if an attacker
        # places a symlink inside it.
        try:
            candidate.relative_to(root)
        except ValueError:
            return None

    try:
        sidecar = (
            candidate
            if candidate.name.endswith(".joker-sync")
            else candidate.with_name(candidate.name + ".joker-sync")
        )
        if sidecar.is_file() and (
            not candidate.is_file()
            or sidecar.stat().st_mtime_ns >= candidate.stat().st_mtime_ns
        ):
            return sidecar
        return candidate if candidate.is_file() else None
    except OSError:
        return None


__all__ = [
    "STEAM_ID_PATTERN",
    "lookup_user_list_steam_id",
    "normalize_database_steam_id",
    "resolve_group_database_path",
]
