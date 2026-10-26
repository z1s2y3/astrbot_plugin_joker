"""Read/write adapter for the SCUM player SQLite database.

The original SCUM database is the source of truth for player balances and
sign-in history.  The QQ plugin keeps its group policy and QQ-to-Steam
association locally, but every Steam-backed reward must be applied to the
player row identified by ``user_list.steam_id``.  This module deliberately
contains no AstrBot or FTP code so it can be used by both command handlers
and tests.

Only the existing tables are touched:

* ``user_list`` -- player row and the configured currency column (``amount``
  by default);
* ``sign_log`` -- one row per successful calendar-day sign-in;
* ``user_amount_change_log`` -- optional audit trail when that legacy table is
  present.

All mutating operations run in one SQLite transaction and use a schema-derived
identifier whitelist.  Missing optional audit tables do not prevent a reward
from being applied; missing ``user_list.steam_id`` or (for sign-in) the
required ``sign_log`` columns do.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Mapping


STEAM_ID_PATTERN = re.compile(r"^[0-9]{17}$")
DEFAULT_CURRENCY_COLUMN = "amount"
MAX_TRANSACTION_AMOUNT = 1_000_000_000

# These are the currency-like fields shipped by the supplied database.  The
# final SQL identifier is still checked against PRAGMA table_info, so a custom
# value can never turn into an arbitrary SQL fragment.
ALLOWED_CURRENCY_COLUMNS = frozenset(
    {"amount", "integral", "normal_integral", "coins", "fragments", "dlb_num"}
)

_VALIDATION_MESSAGES = {
    "found": "",
    "credited": "",
    "debited": "",
    "signed": "",
    "invalid_steam_id": "SteamID 必须是 17 位数字。",
    "db_unavailable": "服务器数据库暂不可用。",
    "not_found": "该 SteamID 不在当前服务器数据库中。",
    "ambiguous": "服务器数据库中的 SteamID 记录不唯一。",
    "schema_error": "服务器数据库结构不完整，无法修改玩家记录。",
    "query_error": "服务器数据库操作失败，请稍后重试。",
    "already_signed": "今日已经签到过了。",
    "insufficient_balance": "余额不足。",
    "invalid_amount": "奖励数值必须是非负整数。",
    "invalid_sign_date": "签到日期无效。",
    "sign_not_found": "未找到该日期的签到记录。",
    "sign_ambiguous": "该日期存在多条签到记录，无法安全重置。",
    "audit_not_found": "未找到与签到对应的奖励变动记录，无法安全重置。",
    "audit_ambiguous": "签到对应的奖励变动记录不唯一，无法安全重置。",
    "rollback_insufficient": "当前余额不足以安全退回该次签到奖励。",
    "balance_changed": "签到后的余额已发生变化，未执行重置。",
    "audit_unavailable": "缺少奖励审计表，未执行重置。",
    "reset": "签到记录和对应奖励已重置。",
    "reset_record_only": "签到记录已删除，原奖励未退回。",
    "renamed": "玩家名称已更新。",
}


def normalize_steam_id(value: object) -> str:
    """Return a canonical 17-digit ASCII SteamID, or ``""``."""

    candidate = str(value or "").strip()
    return candidate if STEAM_ID_PATTERN.fullmatch(candidate) else ""


def _message(code: str) -> str:
    return _VALIDATION_MESSAGES.get(code, _VALIDATION_MESSAGES["query_error"])


def _result(code: str, steam_id: str = "", **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "success": code in {"found", "credited", "debited", "signed", "reset", "reset_record_only", "renamed"},
        "code": code,
        "message": _message(code),
        "steam_id": steam_id,
    }
    value.update(extra)
    return value


def _quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _as_int(value: object, default: int = 0) -> int:
    try:
        # SQLite occasionally returns a float-like string from a legacy table;
        # reject fractional values rather than silently rounding a balance.
        text = str(value).strip() if isinstance(value, str) else value
        return int(text)
    except (TypeError, ValueError, OverflowError):
        return default


def _parse_balance(value: object) -> int | None:
    """Parse a legacy balance, preserving empty fields as zero.

    A non-empty malformed value is rejected instead of being interpreted as
    zero; silently replacing a corrupted SCUM balance would be destructive.
    """

    if value is None or (isinstance(value, str) and not value.strip()):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        if not isinstance(value, (str, bytes, bytearray)) and value != parsed:
            return None
    except (TypeError, ValueError):
        return None
    return parsed


def _as_non_negative_amount(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        amount = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    # ``int(1.5)`` silently truncates.  A reward or balance mutation must be
    # an integral unit, otherwise the value written to SQLite can differ from
    # the amount shown to the caller.
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        if not isinstance(value, (str, bytes, bytearray)) and value != amount:
            # Covers Decimal/Fraction-like values without importing a second
            # numeric abstraction; ordinary integer-like objects compare
            # equal and remain accepted.
            return None
    except (TypeError, ValueError):
        return None
    return amount if 0 <= amount <= MAX_TRANSACTION_AMOUNT else None


def _reward_for_streak(
    fallback: int,
    streak: int,
    reward_config: Mapping[str, Any] | None,
) -> int:
    if not isinstance(reward_config, Mapping):
        return fallback
    normalized: dict[str, int] = {}
    for key, default in (
        ("base_reward", fallback),
        ("reward_3_days", fallback),
        ("reward_7_days", fallback),
        ("reward_14_days", fallback),
        ("reward_30_days", fallback),
    ):
        value = _as_non_negative_amount(reward_config.get(key, default))
        normalized[key] = default if value is None else value
    for minimum, key in (
        (30, "reward_30_days"),
        (14, "reward_14_days"),
        (7, "reward_7_days"),
        (3, "reward_3_days"),
    ):
        if streak >= minimum:
            return normalized[key]
    return normalized["base_reward"]


def _table_columns(connection: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    """Return case-folded column metadata for an existing table."""

    try:
        rows = connection.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    except sqlite3.DatabaseError:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            name = str(row[1] or "").strip()
        except (IndexError, TypeError):
            continue
        if name:
            result.setdefault(name.casefold(), {"name": name, "type": str(row[2] or "")})
    return result


def _mapping_row(cursor: sqlite3.Cursor, row: object) -> dict[str, Any]:
    description = cursor.description or ()
    names = [str(item[0]) for item in description]
    if isinstance(row, Mapping):
        return {str(key): value for key, value in row.items()}
    # ``sqlite3.Row`` is not registered as ``Mapping`` on all supported
    # Python versions, but it exposes a reliable ``keys`` method.  Handling it
    # before the tuple fallback avoids returning the Row object as a value.
    if hasattr(row, "keys"):
        try:
            return {str(key): row[key] for key in row.keys()}  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            pass
    values = tuple(row) if isinstance(row, (tuple, list)) else (row,)
    return {name: values[index] for index, name in enumerate(names) if index < len(values)}


def _open_rw(database: str | Path | sqlite3.Connection, timeout: float) -> tuple[sqlite3.Connection, bool] | None:
    if hasattr(database, "execute"):
        return database, False  # type: ignore[return-value]
    try:
        path = Path(database).expanduser().resolve()
    except (OSError, RuntimeError, TypeError):
        return None
    try:
        if not path.is_file():
            return None
        connection = sqlite3.connect(str(path), timeout=max(0.1, float(timeout)))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection, True
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None


@contextmanager
def _database_connection(database: str | Path | sqlite3.Connection, timeout: float = 5.0) -> Iterator[sqlite3.Connection]:
    opened = _open_rw(database, timeout)
    if opened is None:
        raise OSError("database unavailable")
    connection, owned = opened
    try:
        yield connection
    except BaseException:
        # Path-owned connections roll back on close, but callers may pass a
        # shared connection.  Always clear an interrupted write transaction so
        # the caller can continue using it safely.
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        if owned:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def _find_player(connection: sqlite3.Connection, steam_id: str) -> tuple[str, dict[str, Any]] | tuple[None, dict[str, Any]]:
    columns = _table_columns(connection, "user_list")
    steam_column = columns.get("steam_id")
    if not steam_column:
        return None, _result("schema_error", steam_id, detail="user_list.steam_id missing")

    selected = [steam_column["name"]]
    for key in ("user_id", "user_name", "nick_name"):
        if key in columns and columns[key]["name"] not in selected:
            selected.append(columns[key]["name"])
    # Keep the balance fields available to callers without assuming all are
    # present in older/private-server snapshots.
    for key in ALLOWED_CURRENCY_COLUMNS:
        if key in columns and columns[key]["name"] not in selected:
            selected.append(columns[key]["name"])

    select_sql = ", ".join(_quote_identifier(name) for name in selected)
    try:
        cursor = connection.execute(
            f"SELECT {select_sql} FROM {_quote_identifier('user_list')} "
            f"WHERE TRIM(CAST({_quote_identifier(steam_column['name'])} AS TEXT)) = ? LIMIT 2",
            (steam_id,),
        )
        rows = cursor.fetchall()
    except sqlite3.OperationalError as exc:
        return None, _result("schema_error" if "no such" in str(exc).lower() else "query_error", steam_id, detail=str(exc))
    except sqlite3.DatabaseError as exc:
        return None, _result("query_error", steam_id, detail=str(exc))

    if not rows:
        return None, _result("not_found", steam_id)
    if len(rows) > 1:
        return None, _result("ambiguous", steam_id)
    player = _mapping_row(cursor, rows[0])
    return steam_column["name"], _result("found", steam_id, player=player)


def _player_result(steam_id: str, player: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {str(key): value for key, value in player.items()}
    return _result("found", steam_id, player=normalized)


def read_player(database: str | Path | sqlite3.Connection, steam_id: object, *, timeout: float = 3.0) -> dict[str, Any]:
    """Read a unique ``user_list`` row by SteamID (read-only operation)."""

    normalized = normalize_steam_id(steam_id)
    if not normalized:
        return _result("invalid_steam_id")
    # A path is opened read-only here so status/reply rendering can never
    # create or mutate a game database accidentally.
    if hasattr(database, "execute"):
        try:
            _, result = _find_player(database, normalized)
            if result.get("code") != "found":
                return result
            return _player_result(normalized, result.get("player", {}))
        except Exception as exc:
            return _result("query_error", normalized, detail=type(exc).__name__)
    try:
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            return _result("db_unavailable", normalized)
        uri = f"file:{path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=max(0.1, float(timeout)))
        try:
            connection.row_factory = sqlite3.Row
            _, result = _find_player(connection, normalized)
            if result.get("code") != "found":
                return result
            return _player_result(normalized, result.get("player", {}))
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        return _result("db_unavailable", normalized, detail=type(exc).__name__)


def update_player_name(
    database: str | Path | sqlite3.Connection,
    steam_id: object,
    player_name: object,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Atomically update the authoritative ``user_list`` display name.

    SCUM snapshots use ``user_name`` while a few private/legacy schemas only
    expose ``nick_name``. The schema-derived column selection keeps both
    layouts safe and never creates a second player row.
    """

    normalized = normalize_steam_id(steam_id)
    if not normalized:
        return _result("invalid_steam_id")
    name = str(player_name or "").strip()[:255]
    if not name:
        return _result("query_error", normalized, detail="player_name_empty")
    try:
        with _database_connection(database, timeout) as connection:
            connection.execute("BEGIN IMMEDIATE")
            steam_column, found = _find_player(connection, normalized)
            if found.get("code") != "found" or not steam_column:
                connection.rollback()
                return found
            columns = _table_columns(connection, "user_list")
            name_meta = columns.get("user_name") or columns.get("nick_name")
            if not name_meta:
                connection.rollback()
                return _result("schema_error", normalized, detail="player name column missing")
            player = found.get("player", {})
            before = str(player.get(name_meta["name"], "") or "")
            where_sql, where_args = _player_where(columns, player, steam_column)
            updated = connection.execute(
                f"UPDATE {_quote_identifier('user_list')} SET "
                f"{_quote_identifier(name_meta['name'])} = ? {where_sql}",
                (name, *where_args),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return _result("query_error", normalized, detail="player update affected unexpected rows")
            connection.commit()
            return _result(
                "renamed",
                normalized,
                previous_name=before,
                player_name=name,
                name_column=name_meta["name"],
            )
    except OSError:
        return _result("db_unavailable", normalized)
    except sqlite3.DatabaseError as exc:
        return _result("query_error", normalized, detail=str(exc))
    except Exception as exc:
        return _result("query_error", normalized, detail=type(exc).__name__)


def _signin_calendar_summary(
    connection: sqlite3.Connection,
    sign_columns: Mapping[str, Mapping[str, Any]],
    steam_id: str,
    current_date: date,
) -> dict[str, Any]:
    steam_name = str(sign_columns["player_steamid"]["name"])
    date_name = str(sign_columns["sign_date"]["name"])
    try:
        rows = connection.execute(
            f"SELECT TRIM(CAST({_quote_identifier(date_name)} AS TEXT)) "
            f"FROM {_quote_identifier('sign_log')} "
            f"WHERE TRIM(CAST({_quote_identifier(steam_name)} AS TEXT)) = ? "
            f"ORDER BY rowid DESC LIMIT 2000",
            (steam_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        # WITHOUT ROWID legacy tables have no rowid.  ISO dates still provide
        # a stable order for all records created by this adapter.
        rows = connection.execute(
            f"SELECT TRIM(CAST({_quote_identifier(date_name)} AS TEXT)) "
            f"FROM {_quote_identifier('sign_log')} "
            f"WHERE TRIM(CAST({_quote_identifier(steam_name)} AS TEXT)) = ? "
            f"ORDER BY {_quote_identifier(date_name)} DESC LIMIT 2000",
            (steam_id,),
        ).fetchall()
    raw_dates = []
    parsed_days: set[date] = set()
    for row in rows:
        value = str(row[0] or "").strip()[:32]
        if not value:
            continue
        raw_dates.append(value)
        try:
            parsed_days.add(date.fromisoformat(value[:10]))
        except ValueError:
            continue
    ordered_days = sorted(parsed_days, reverse=True)
    cursor_day = current_date
    if cursor_day not in parsed_days:
        cursor_day = date.fromordinal(cursor_day.toordinal() - 1)
    streak = 0
    while cursor_day in parsed_days:
        streak += 1
        cursor_day = date.fromordinal(cursor_day.toordinal() - 1)
    current_week = current_date.isocalendar()[:2]
    return {
        "sign_dates": [value.isoformat() for value in ordered_days],
        "signed_today": current_date in parsed_days,
        "total_signins": len(parsed_days),
        "month_signins": sum(
            value.year == current_date.year and value.month == current_date.month
            for value in parsed_days
        ),
        "week_signins": sum(
            value.isocalendar()[:2] == current_week for value in parsed_days
        ),
        "last_signin": ordered_days[0].isoformat() if ordered_days else "",
        "consecutive_signins": streak,
        "sign_log_rows": len(raw_dates),
    }


def read_player_status(
    database: str | Path | sqlite3.Connection,
    steam_id: object,
    *,
    today: object = None,
    currency_column: object = DEFAULT_CURRENCY_COLUMN,
    timeout: float = 3.0,
) -> dict[str, Any]:
    """Read a player's balance and database sign-in history.

    ``user_list`` remains the required source for the player.  ``sign_log``
    is optional for status rendering so older snapshots can still show a
    balance; when it exists, the returned calendar fields are derived from it
    instead of the QQ JSON mirror.
    """
    normalized = normalize_steam_id(steam_id)
    if not normalized:
        return _result("invalid_steam_id")
    current_date = str(today or date.today().isoformat()).strip()[:32]
    try:
        date.fromisoformat(current_date)
    except ValueError:
        current_date = date.today().isoformat()

    def _read(connection: sqlite3.Connection) -> dict[str, Any]:
        steam_column, player_result = _find_player(connection, normalized)
        if player_result.get("code") != "found" or steam_column is None:
            return player_result
        player = dict(player_result.get("player") or {})
        user_columns = _table_columns(connection, "user_list")
        currency = _currency_column(user_columns, currency_column)
        if currency is None:
            return _result("schema_error", normalized, detail="currency column missing")
        balance = _parse_balance(player.get(currency))
        if balance is None:
            return _result("schema_error", normalized, detail="currency value is not numeric")
        response = _result(
            "found",
            normalized,
            player=player,
            currency=currency,
            balance=balance,
            signed_today=False,
            total_signins=0,
            month_signins=0,
            week_signins=0,
            consecutive_signins=0,
            sign_dates=[],
            sign_log_available=False,
        )
        sign_columns = _table_columns(connection, "sign_log")
        if not sign_columns or "player_steamid" not in sign_columns or "sign_date" not in sign_columns:
            return response
        response["sign_log_available"] = True
        response.update(
            _signin_calendar_summary(
                connection,
                sign_columns,
                normalized,
                date.fromisoformat(current_date),
            )
        )
        return response

    if hasattr(database, "execute"):
        try:
            return _read(database)  # type: ignore[arg-type]
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            return _result("query_error", normalized, detail=type(exc).__name__)
    try:
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            return _result("db_unavailable", normalized)
        uri = f"file:{path.as_posix()}?mode=ro"
        # ``sqlite3.Connection``'s context manager only commits/rolls back;
        # it does not close the handle.  Close explicitly so Windows can
        # replace or clean up the database immediately after a status read.
        connection = sqlite3.connect(uri, uri=True, timeout=max(0.1, float(timeout)))
        try:
            connection.row_factory = sqlite3.Row
            return _read(connection)
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        return _result("db_unavailable", normalized, detail=type(exc).__name__)


def _currency_column(columns: Mapping[str, Mapping[str, Any]], requested: object) -> str | None:
    name = str(requested or DEFAULT_CURRENCY_COLUMN).strip().casefold()
    if name not in ALLOWED_CURRENCY_COLUMNS:
        name = DEFAULT_CURRENCY_COLUMN
    metadata = columns.get(name)
    return str(metadata["name"]) if metadata else None


def _player_where(columns: Mapping[str, Mapping[str, Any]], player: Mapping[str, Any], steam_column: str) -> tuple[str, tuple[Any, ...]]:
    user_id_column = columns.get("user_id")
    if user_id_column and player.get(user_id_column["name"]) is not None:
        return f"WHERE {_quote_identifier(user_id_column['name'])} = ?", (player.get(user_id_column["name"]),)
    return f"WHERE TRIM(CAST({_quote_identifier(steam_column)} AS TEXT)) = ?", (str(player.get(steam_column, "")).strip(),)


def _store_numeric(connection: sqlite3.Connection, table_columns: Mapping[str, Mapping[str, Any]], column: str, value: int) -> int | str:
    declared = str(table_columns.get(column.casefold(), {}).get("type", "")).upper()
    return value if any(token in declared for token in ("INT", "REAL", "NUM", "DEC")) else str(value)


def _insert_change_log(
    connection: sqlite3.Connection,
    steam_id: str,
    *,
    change_type: str,
    direction: str,
    amount: int,
    before_value: int,
    after_value: int,
    method_name: str,
    created_at: str,
) -> bool:
    columns = _table_columns(connection, "user_amount_change_log")
    required = {
        "created_at": created_at,
        "steamid": steam_id,
        "change_type": str(change_type or "xiongB")[:80],
        "change_action": str(direction or ("add" if amount >= 0 else "reduce"))[:80],
        "before_value": before_value,
        "change_value": amount,
        "after_value": after_value,
        "method_name": str(method_name or "joker")[:120],
    }
    if not columns or any(key not in columns for key in required):
        return False
    names = [columns[key]["name"] for key in required]
    values = [required[key] for key in required]
    connection.execute(
        f"INSERT INTO {_quote_identifier('user_amount_change_log')} "
        f"({', '.join(_quote_identifier(name) for name in names)}) VALUES ({', '.join('?' for _ in names)})",
        values,
    )
    return True


def _credit_or_debit(
    database: str | Path | sqlite3.Connection,
    steam_id: object,
    amount: object,
    *,
    direction: str,
    currency_column: object = DEFAULT_CURRENCY_COLUMN,
    change_type: str = "xiongB",
    method_name: str = "joker",
    timeout: float = 5.0,
) -> dict[str, Any]:
    normalized = normalize_steam_id(steam_id)
    if not normalized:
        return _result("invalid_steam_id")
    normalized_amount = _as_non_negative_amount(amount)
    if normalized_amount is None:
        return _result("invalid_amount", normalized)
    signed_amount = normalized_amount if direction == "add" else -normalized_amount
    try:
        with _database_connection(database, timeout) as connection:
            connection.execute("BEGIN IMMEDIATE")
            steam_column, player_result = _find_player(connection, normalized)
            if player_result.get("code") != "found" or steam_column is None:
                connection.rollback()
                return player_result
            columns = _table_columns(connection, "user_list")
            currency = _currency_column(columns, currency_column)
            if currency is None:
                connection.rollback()
                return _result("schema_error", normalized, detail="currency column missing")
            before = _parse_balance(player_result["player"].get(currency))
            if before is None:
                connection.rollback()
                return _result("schema_error", normalized, detail="currency value is not numeric")
            after = before + signed_amount
            if after < 0:
                connection.rollback()
                return _result("insufficient_balance", normalized, before=before, change=signed_amount, after=before)
            where_sql, where_args = _player_where(columns, player_result["player"], steam_column)
            stored_after = _store_numeric(connection, columns, currency, after)
            cursor = connection.execute(
                f"UPDATE {_quote_identifier('user_list')} SET {_quote_identifier(currency)} = ? {where_sql}",
                (stored_after, *where_args),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return _result("query_error", normalized, detail="player update affected unexpected rows")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            audit_logged = _insert_change_log(
                connection,
                normalized,
                change_type=change_type,
                direction=direction,
                amount=signed_amount,
                before_value=before,
                after_value=after,
                method_name=method_name,
                created_at=now,
            )
            connection.commit()
            player = dict(player_result["player"])
            player[currency] = stored_after
            return _result(
                "credited" if direction == "add" else "debited",
                normalized,
                player=player,
                currency=currency,
                before=before,
                change=signed_amount,
                after=after,
                balance=after,
                audit_logged=audit_logged,
            )
    except OSError:
        return _result("db_unavailable", normalized)
    except sqlite3.OperationalError as exc:
        return _result("query_error", normalized, detail=str(exc))
    except sqlite3.DatabaseError as exc:
        return _result("query_error", normalized, detail=str(exc))
    except Exception as exc:
        return _result("query_error", normalized, detail=type(exc).__name__)


def credit_player(
    database: str | Path | sqlite3.Connection,
    steam_id: object,
    amount: object,
    *,
    currency_column: object = DEFAULT_CURRENCY_COLUMN,
    change_type: str = "xiongB",
    method_name: str = "joker",
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Atomically add a non-negative reward to the player's balance."""

    return _credit_or_debit(
        database,
        steam_id,
        amount,
        direction="add",
        currency_column=currency_column,
        change_type=change_type,
        method_name=method_name,
        timeout=timeout,
    )


def debit_player(
    database: str | Path | sqlite3.Connection,
    steam_id: object,
    amount: object,
    *,
    currency_column: object = DEFAULT_CURRENCY_COLUMN,
    change_type: str = "xiongB",
    method_name: str = "joker",
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Atomically deduct a non-negative amount without allowing negatives."""

    return _credit_or_debit(
        database,
        steam_id,
        amount,
        direction="reduce",
        currency_column=currency_column,
        change_type=change_type,
        method_name=method_name,
        timeout=timeout,
    )


def _insert_sign_log(
    connection: sqlite3.Connection,
    steam_id: str,
    player_name: str,
    sign_date: str,
    sign_time: str,
) -> bool:
    columns = _table_columns(connection, "sign_log")
    required_keys = ("player_steamid", "sign_date")
    if not columns or any(key not in columns for key in required_keys):
        return False
    values_by_key: dict[str, Any] = {
        "player_name": str(player_name or "")[:255],
        "player_steamid": steam_id,
        "sign_date": sign_date,
        "sign_time": sign_time,
    }
    keys = [key for key in values_by_key if key in columns]
    names = [columns[key]["name"] for key in keys]
    connection.execute(
        f"INSERT INTO {_quote_identifier('sign_log')} "
        f"({', '.join(_quote_identifier(name) for name in names)}) VALUES ({', '.join('?' for _ in names)})",
        [values_by_key[key] for key in keys],
    )
    return True


def apply_signin_reward(
    database: str | Path | sqlite3.Connection,
    steam_id: object,
    reward: object,
    *,
    player_name: object = "",
    sign_date: object = None,
    sign_time: object = None,
    currency_column: object = DEFAULT_CURRENCY_COLUMN,
    change_type: str = "xiongB",
    method_name: str = "playerSignInDo",
    period_mode: object = "daily",
    reward_config: Mapping[str, Any] | None = None,
    timeout: float = 5.0,
    allow_duplicate: bool = False,
) -> dict[str, Any]:
    """Record a calendar-day sign-in and credit its reward atomically.

    ``sign_log`` is treated as the database-level idempotency record.  A
    duplicate check and the balance update occur under ``BEGIN IMMEDIATE`` so
    two concurrent QQ events cannot both receive the same daily reward.
    """

    normalized = normalize_steam_id(steam_id)
    if not normalized:
        return _result("invalid_steam_id")
    normalized_reward = _as_non_negative_amount(reward)
    if normalized_reward is None:
        return _result("invalid_amount", normalized)
    current = date.today()
    date_text = str(sign_date or current.isoformat()).strip()[:32]
    try:
        # Validate date text where possible; retain explicit legacy strings for
        # compatibility but never emit an empty date.
        date.fromisoformat(date_text)
    except ValueError:
        date_text = current.isoformat()
    time_text = str(sign_time or datetime.now().strftime("%H:%M:%S")).strip()[:32]
    normalized_period = str(period_mode or "daily").strip().lower()
    if normalized_period not in {"daily", "weekly", "monthly"}:
        normalized_period = "daily"

    # Perform the duplicate check, balance update, sign-log insert and audit
    # insert in one transaction.  Do not call ``credit_player`` here: that
    # public helper owns and commits its transaction independently.
    try:
        with _database_connection(database, timeout) as connection:
            connection.execute("BEGIN IMMEDIATE")
            columns = _table_columns(connection, "sign_log")
            if not columns or "player_steamid" not in columns or "sign_date" not in columns:
                connection.rollback()
                return _result("schema_error", normalized, detail="sign_log columns missing")
            if not allow_duplicate:
                selected_date = date.fromisoformat(date_text)
                if normalized_period == "weekly":
                    period_start = date.fromordinal(
                        selected_date.toordinal() - selected_date.weekday()
                    )
                    period_end = date.fromordinal(period_start.toordinal() + 6)
                elif normalized_period == "monthly":
                    period_start = selected_date.replace(day=1)
                    if selected_date.month == 12:
                        next_month = selected_date.replace(
                            year=selected_date.year + 1, month=1, day=1
                        )
                    else:
                        next_month = selected_date.replace(
                            month=selected_date.month + 1, day=1
                        )
                    period_end = date.fromordinal(next_month.toordinal() - 1)
                else:
                    period_start = selected_date
                    period_end = selected_date
                duplicate = connection.execute(
                    f"SELECT 1 FROM {_quote_identifier('sign_log')} WHERE "
                    f"TRIM(CAST({_quote_identifier(columns['player_steamid']['name'])} AS TEXT)) = ? "
                    f"AND SUBSTR(TRIM(CAST({_quote_identifier(columns['sign_date']['name'])} AS TEXT)), 1, 10) "
                    f"BETWEEN ? AND ? LIMIT 1",
                    (normalized, period_start.isoformat(), period_end.isoformat()),
                ).fetchone()
                if duplicate is not None:
                    connection.rollback()
                    period_label = {
                        "weekly": "本周已经签到过了。",
                        "monthly": "本月已经签到过了。",
                    }.get(normalized_period, _message("already_signed"))
                    return _result(
                        "already_signed",
                        normalized,
                        message=period_label,
                        sign_date=date_text,
                        period_mode=normalized_period,
                    )
            steam_column, player_result = _find_player(connection, normalized)
            if player_result.get("code") != "found" or steam_column is None:
                connection.rollback()
                return player_result
            player = player_result["player"]
            previous_calendar = _signin_calendar_summary(
                connection,
                columns,
                normalized,
                date.fromisoformat(date_text),
            )
            reward_streak = max(
                1, int(previous_calendar.get("consecutive_signins", 0)) + 1
            )
            normalized_reward = _reward_for_streak(
                normalized_reward,
                reward_streak,
                reward_config,
            )
            user_columns = _table_columns(connection, "user_list")
            currency = _currency_column(user_columns, currency_column)
            if currency is None:
                connection.rollback()
                return _result("schema_error", normalized, detail="currency column missing")
            before = _parse_balance(player.get(currency))
            if before is None:
                connection.rollback()
                return _result("schema_error", normalized, detail="currency value is not numeric")
            after = before + normalized_reward
            where_sql, where_args = _player_where(user_columns, player, steam_column)
            stored_after = _store_numeric(connection, user_columns, currency, after)
            updated = connection.execute(
                f"UPDATE {_quote_identifier('user_list')} SET {_quote_identifier(currency)} = ? {where_sql}",
                (stored_after, *where_args),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return _result("query_error", normalized, detail="player update affected unexpected rows")
            signed_name = str(player_name or player.get("user_name", "") or "").strip()[:255]
            sign_logged = _insert_sign_log(connection, normalized, signed_name, date_text, time_text)
            audit_logged = _insert_change_log(
                connection,
                normalized,
                change_type=change_type,
                direction="add",
                amount=normalized_reward,
                before_value=before,
                after_value=after,
                method_name=method_name,
                created_at=f"{date_text} {time_text}"[:64],
            )
            updated_player = dict(player)
            updated_player[currency] = stored_after
            calendar_summary = _signin_calendar_summary(
                connection,
                columns,
                normalized,
                date.fromisoformat(date_text),
            )
            connection.commit()
            return _result(
                "signed",
                normalized,
                player=updated_player,
                currency=currency,
                before=before,
                change=normalized_reward,
                after=after,
                balance=after,
                sign_date=date_text,
                sign_time=time_text,
                period_mode=normalized_period,
                reward_streak=reward_streak,
                sign_logged=sign_logged,
                audit_logged=audit_logged,
                **calendar_summary,
            )
    except OSError:
        return _result("db_unavailable", normalized)
    except sqlite3.OperationalError as exc:
        return _result("query_error", normalized, detail=str(exc))
    except sqlite3.DatabaseError as exc:
        return _result("query_error", normalized, detail=str(exc))
    except Exception as exc:
        return _result("query_error", normalized, detail=type(exc).__name__)


def reset_signin_record(
    database: str | Path | sqlite3.Connection,
    steam_id: object,
    sign_date: object,
    *,
    currency_column: object = DEFAULT_CURRENCY_COLUMN,
    method_name: str = "playerSignInDo",
    reward: object = None,
    remove_reward: bool = True,
    allow_balance_drift: bool = False,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Safely remove one database sign-in record and optionally reverse it.

    A sign-in row has no foreign key to the legacy amount-audit table.  To
    avoid deleting an economically meaningful record by guesswork this
    function therefore requires exactly one matching positive audit row and,
    by default, requires the player's current balance to equal that audit's
    ``after_value``.  Callers handling a failed/partial reward can pass
    ``remove_reward=False`` to remove only the sign row.  No mutation is
    committed when any identity or audit check is ambiguous.
    """

    normalized = normalize_steam_id(steam_id)
    if not normalized:
        return _result("invalid_steam_id")
    date_text = str(sign_date or "").strip()[:32]
    try:
        selected_date = date.fromisoformat(date_text[:10])
    except (TypeError, ValueError):
        return _result("invalid_sign_date", normalized)
    date_prefix = selected_date.isoformat()
    requested_reward: int | None = None
    if reward is not None:
        requested_reward = _as_non_negative_amount(reward)
        if requested_reward is None:
            return _result("invalid_amount", normalized)

    def _rollback_result(code: str, **extra: Any) -> dict[str, Any]:
        return _result(code, normalized, sign_date=date_prefix, **extra)

    try:
        with _database_connection(database, timeout) as connection:
            connection.execute("BEGIN IMMEDIATE")
            steam_column, player_result = _find_player(connection, normalized)
            if player_result.get("code") != "found" or steam_column is None:
                connection.rollback()
                return player_result

            user_columns = _table_columns(connection, "user_list")
            currency = _currency_column(user_columns, currency_column)
            if currency is None:
                connection.rollback()
                return _rollback_result("schema_error", detail="currency column missing")
            before_balance = _parse_balance(player_result["player"].get(currency))
            if before_balance is None:
                connection.rollback()
                return _rollback_result("schema_error", detail="currency value is not numeric")

            sign_columns = _table_columns(connection, "sign_log")
            if not sign_columns or "player_steamid" not in sign_columns or "sign_date" not in sign_columns:
                connection.rollback()
                return _rollback_result("schema_error", detail="sign_log columns missing")
            sign_steam = sign_columns["player_steamid"]["name"]
            sign_date_name = sign_columns["sign_date"]["name"]
            selected_sign_id = sign_columns.get("sign_id", {}).get("name")
            selected = []
            if selected_sign_id:
                selected.append(_quote_identifier(selected_sign_id))
            # rowid is available for the supplied legacy table, while a
            # WITHOUT ROWID table may require the date/steam predicate below.
            selected.append("rowid AS __joker_rowid__")
            selected.extend(
                _quote_identifier(meta["name"])
                for key, meta in sign_columns.items()
                if meta["name"] not in {selected_sign_id, sign_date_name, sign_steam}
                and meta["name"] != "rowid"
            )
            # Fetch only the identity columns needed for a deterministic
            # delete; selecting rowid can fail on WITHOUT ROWID tables, so
            # retry with the explicit primary key/date fields.
            try:
                sign_cursor = connection.execute(
                    f"SELECT {', '.join(selected)} FROM {_quote_identifier('sign_log')} "
                    f"WHERE TRIM(CAST({_quote_identifier(sign_steam)} AS TEXT)) = ? "
                    f"AND SUBSTR(TRIM(CAST({_quote_identifier(sign_date_name)} AS TEXT)), 1, 10) = ?",
                    (normalized, date_prefix),
                )
                sign_rows = sign_cursor.fetchall()
            except sqlite3.OperationalError:
                fallback_cols = []
                if selected_sign_id:
                    fallback_cols.append(_quote_identifier(selected_sign_id))
                fallback_cols.extend([
                    _quote_identifier(sign_steam),
                    _quote_identifier(sign_date_name),
                ])
                sign_cursor = connection.execute(
                    f"SELECT {', '.join(fallback_cols)} FROM {_quote_identifier('sign_log')} "
                    f"WHERE TRIM(CAST({_quote_identifier(sign_steam)} AS TEXT)) = ? "
                    f"AND SUBSTR(TRIM(CAST({_quote_identifier(sign_date_name)} AS TEXT)), 1, 10) = ?",
                    (normalized, date_prefix),
                )
                sign_rows = sign_cursor.fetchall()
            if not sign_rows:
                connection.rollback()
                return _rollback_result("sign_not_found")
            if len(sign_rows) != 1:
                connection.rollback()
                return _rollback_result("sign_ambiguous")
            sign_row = sign_rows[0]
            sign_mapping: dict[str, Any] = {}
            # Build a reliable mapping from the cursor description without
            # relying on sqlite3.Row being registered as Mapping.
            # The query is repeated only for metadata and stays in the same
            # transaction; no external state is touched.
            # (Using the row's keys is faster when row_factory is enabled.)
            if hasattr(sign_row, "keys"):
                try:
                    sign_mapping = {str(key): sign_row[key] for key in sign_row.keys()}
                except (KeyError, IndexError, TypeError):
                    sign_mapping = {}
            if not sign_mapping:
                # The cursor used for the successful query retains the exact
                # fallback column list (important for WITHOUT ROWID tables).
                sign_mapping = _mapping_row(sign_cursor, sign_row)

            # Resolve the row identity before looking at the audit trail.
            sign_identity: tuple[str, Any] | None = None
            if selected_sign_id and sign_mapping.get(selected_sign_id) is not None:
                sign_identity = (selected_sign_id, sign_mapping.get(selected_sign_id))
            elif sign_mapping.get("__joker_rowid__") is not None:
                sign_identity = ("__joker_rowid__", sign_mapping.get("__joker_rowid__"))
            if sign_identity is None:
                # A table without a key can still be deleted safely because
                # the uniqueness check above guarantees one matching row.
                sign_identity = ("predicate", None)

            audit_columns = _table_columns(connection, "user_amount_change_log")
            audit_row: dict[str, Any] | None = None
            if remove_reward:
                required = {"steamid", "change_value", "before_value", "after_value"}
                if not audit_columns or not required.issubset(audit_columns):
                    connection.rollback()
                    return _rollback_result("audit_unavailable")
                audit_steam = audit_columns["steamid"]["name"]
                audit_change = audit_columns["change_value"]["name"]
                audit_before = audit_columns["before_value"]["name"]
                audit_after = audit_columns["after_value"]["name"]
                audit_select = [
                    _quote_identifier(name["name"])
                    for name in audit_columns.values()
                ]
                audit_where = (
                    f"TRIM(CAST({_quote_identifier(audit_steam)} AS TEXT)) = ? "
                    f"AND CAST({_quote_identifier(audit_change)} AS INTEGER) > 0"
                )
                args: list[Any] = [normalized]
                created_name = audit_columns.get("created_at", {}).get("name")
                if created_name:
                    audit_where += (
                        f" AND SUBSTR(TRIM(CAST({_quote_identifier(created_name)} AS TEXT)), 1, 10) = ?"
                    )
                    args.append(date_prefix)
                method_column = audit_columns.get("method_name", {}).get("name")
                if method_column and str(method_name or "").strip():
                    audit_where += f" AND {_quote_identifier(method_column)} = ?"
                    args.append(str(method_name).strip()[:120])
                audit_sql = (
                    f"SELECT {', '.join(audit_select)} FROM {_quote_identifier('user_amount_change_log')} "
                    f"WHERE {audit_where} ORDER BY rowid DESC LIMIT 20"
                )
                try:
                    audit_cursor = connection.execute(audit_sql, args)
                except sqlite3.OperationalError as exc:
                    # Some private-server snapshots declare the audit table
                    # WITHOUT ROWID.  The audit identity is not needed for a
                    # rollback, so use a deterministic timestamp/key order
                    # when the implicit rowid is unavailable.
                    if "rowid" not in str(exc).lower():
                        raise
                    order_column = created_name or audit_columns.get("id", {}).get("name")
                    if order_column:
                        audit_sql = (
                            f"SELECT {', '.join(audit_select)} FROM {_quote_identifier('user_amount_change_log')} "
                            f"WHERE {audit_where} ORDER BY {_quote_identifier(order_column)} DESC LIMIT 20"
                        )
                    else:
                        audit_sql = (
                            f"SELECT {', '.join(audit_select)} FROM {_quote_identifier('user_amount_change_log')} "
                            f"WHERE {audit_where} LIMIT 20"
                        )
                    audit_cursor = connection.execute(audit_sql, args)
                audit_rows = audit_cursor.fetchall()
                for raw_audit in audit_rows:
                    candidate = _mapping_row(audit_cursor, raw_audit)
                    amount_value = _as_non_negative_amount(candidate.get(audit_change))
                    before_value = _parse_balance(candidate.get(audit_before))
                    after_value = _parse_balance(candidate.get(audit_after))
                    if amount_value is None or before_value is None or after_value is None:
                        continue
                    if after_value - before_value != amount_value:
                        continue
                    if requested_reward is not None and amount_value != requested_reward:
                        continue
                    if audit_row is not None:
                        connection.rollback()
                        return _rollback_result("audit_ambiguous")
                    audit_row = {
                        "row": candidate,
                        "amount": amount_value,
                        "before": before_value,
                        "after": after_value,
                    }
                if audit_row is None:
                    connection.rollback()
                    return _rollback_result("audit_not_found")
                reward_amount = int(audit_row["amount"])
                if not allow_balance_drift and before_balance != int(audit_row["after"]):
                    connection.rollback()
                    return _rollback_result(
                        "balance_changed",
                        before=before_balance,
                        expected_after=int(audit_row["after"]),
                        reward=reward_amount,
                    )
                target_balance = int(audit_row["before"])
                if target_balance < 0 or before_balance < reward_amount:
                    connection.rollback()
                    return _rollback_result("rollback_insufficient")
            else:
                reward_amount = 0
                target_balance = before_balance

            if remove_reward:
                where_sql, where_args = _player_where(user_columns, player_result["player"], steam_column)
                updated = connection.execute(
                    f"UPDATE {_quote_identifier('user_list')} SET {_quote_identifier(currency)} = ? {where_sql}",
                    (_store_numeric(connection, user_columns, currency, target_balance), *where_args),
                )
                if updated.rowcount != 1:
                    connection.rollback()
                    return _rollback_result("query_error", detail="player rollback affected unexpected rows")

            if sign_identity[0] == "predicate":
                deleted = connection.execute(
                    f"DELETE FROM {_quote_identifier('sign_log')} WHERE "
                    f"TRIM(CAST({_quote_identifier(sign_steam)} AS TEXT)) = ? "
                    f"AND SUBSTR(TRIM(CAST({_quote_identifier(sign_date_name)} AS TEXT)), 1, 10) = ?",
                    (normalized, date_prefix),
                )
            else:
                identity_name, identity_value = sign_identity
                identity_sql = "rowid" if identity_name == "__joker_rowid__" else _quote_identifier(identity_name)
                deleted = connection.execute(
                    f"DELETE FROM {_quote_identifier('sign_log')} WHERE {identity_sql} = ?",
                    (identity_value,),
                )
            if deleted.rowcount != 1:
                connection.rollback()
                return _rollback_result("query_error", detail="sign-in delete affected unexpected rows")

            reverse_audit_logged = False
            if remove_reward:
                reverse_audit_logged = _insert_change_log(
                    connection,
                    normalized,
                    change_type="signin_reset",
                    direction="reduce",
                    amount=-reward_amount,
                    before_value=before_balance,
                    after_value=target_balance,
                    method_name="reset_signin_record",
                    created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
            connection.commit()
            return _result(
                "reset" if remove_reward else "reset_record_only",
                normalized,
                sign_date=date_prefix,
                before=before_balance,
                change=-reward_amount if remove_reward else 0,
                after=target_balance,
                balance=target_balance,
                reward=reward_amount,
                removed_sign_log=1,
                reverse_audit_logged=reverse_audit_logged,
                reward_reverted=bool(remove_reward),
            )
    except OSError:
        return _result("db_unavailable", normalized, sign_date=date_prefix)
    except sqlite3.OperationalError as exc:
        return _result("query_error", normalized, sign_date=date_prefix, detail=str(exc))
    except sqlite3.DatabaseError as exc:
        return _result("query_error", normalized, sign_date=date_prefix, detail=str(exc))
    except Exception as exc:
        return _result("query_error", normalized, sign_date=date_prefix, detail=type(exc).__name__)


__all__ = [
    "ALLOWED_CURRENCY_COLUMNS",
    "DEFAULT_CURRENCY_COLUMN",
    "MAX_TRANSACTION_AMOUNT",
    "STEAM_ID_PATTERN",
    "apply_signin_reward",
    "reset_signin_record",
    "credit_player",
    "debit_player",
    "normalize_steam_id",
    "read_player",
    "read_player_status",
]
