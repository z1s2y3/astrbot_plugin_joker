"""MySQL-backed data adapter used when the portal is remote from AstrBot."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from typing import Any
import hashlib
import json
import logging
import re
import time

from sqlalchemy import and_, delete, desc, false, func, or_, select
from sqlalchemy.orm import Session

from .models import (
    GameAuthorization,
    GroupAccess,
    GroupClientSync,
    GroupConfigSnapshot,
    Im3Authorization,
    PortalConsoleEvent,
    utcnow,
)
from .console_service import normalize_console_keywords
from .sync_projection import project_group_config
from .game_server_secrets import materialize_game_server_secrets, store_game_server_profiles
from .im3_socketio import normalize_im3_access_key
from .websocket_settings import _decrypt_token, _encrypt_token, _read_settings_fallback


GROUP_ID_RE = re.compile(r"^\d{5,20}$")
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
# Log cleanup is intentionally bounded even when an administrator submits a
# malformed form value.  A century is long enough for retention policies while
# preventing accidental integer/SQL overflow or an unbounded date calculation.
MAX_CONSOLE_PURGE_DAYS = 36_500
MAX_CONSOLE_PURGE_FILTER_LENGTH = 128
# Card fingerprints are HMAC-SHA256 digests.  Keep the protocol strict so a
# malformed sync payload cannot create a claimable identity; legacy card
# *formats* are normalized and hashed by the Windows client before upload.
AUTH_CARD_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
AUTH_STATES = frozenset({"active", "expired", "missing", "unverified"})
IM3_CARD_VALIDATION_VERSION = "v3"
GAME_AUTH_STATES = frozenset({"active", "expired", "missing"})
GAME_AUTH_RETENTION_SECONDS = 7 * 86400
# The Windows client sends only one of these compact, non-sensitive summaries.
# Keep validation on the server as well: a bearer-token holder must not be able
# to place arbitrary exception text, URLs, or HTML into an administrator view.
SYNC_FAILURE_SUMMARY_RE = re.compile(
    r"^(?:local_config_save_failed|local_apply_failed|ack_failed|ack_not_confirmed)"
    r"(?::[A-Za-z][A-Za-z0-9_]{0,63})?$"
)
_MIN_BIGINT = -(2**63)
_MAX_BIGINT = 2**63 - 1
# Keep each outbound exchange focused.  A browser save is scoped to one
# group, and returning a large pending batch made one Windows poll overwrite
# or lock several unrelated local groups at once.
MAX_PENDING_CHANGES_PER_EXCHANGE = 1
MAX_RECONCILIATION_HINTS_PER_EXCHANGE = 1
LOCAL_GROUP_MISSING_STATUS = "unavailable"
LOCAL_GROUP_MISSING_ERROR = "local_group_missing"
# The Windows process can keep an authenticated HTTP heartbeat alive while a
# local config/auth JSON source is temporarily unreadable.  Record that state
# separately from ``synced`` so the web page does not claim a healthy snapshot
# merely because the transport itself is reachable.
LOCAL_SOURCE_PROTECTED_STATUS = "protected"
LOCAL_SOURCE_PROTECTED_ERROR = "local_source_incomplete"
LOCAL_SOURCE_PARTIAL_STATUS = "partial"
LOCAL_SOURCE_PARTIAL_ERROR = "auth_source_incomplete"
logger = logging.getLogger(__name__)


def _secure_projection(config: object, previous: object = None) -> dict[str, Any]:
    """Keep plugin WebSocket API Keys encrypted in portal snapshots."""
    projected = project_group_config(config if isinstance(config, dict) else {})
    features = projected.get("features")
    if not isinstance(features, dict):
        return projected
    game_servers = features.get("game_servers")
    if not isinstance(game_servers, dict):
        return projected
    previous_features = previous.get("features") if isinstance(previous, dict) else {}
    previous_servers = previous_features.get("game_servers") if isinstance(previous_features, dict) else {}
    game_servers["profiles"] = store_game_server_profiles(
        game_servers.get("profiles", []),
        previous_servers.get("profiles", []) if isinstance(previous_servers, dict) else [],
        encrypt_secrets=True,
    )
    features["game_servers"] = game_servers
    projected["features"] = features
    return projected


def _client_config(config: object) -> dict[str, Any]:
    """Materialize encrypted game credentials only for the authenticated worker."""
    return materialize_game_server_secrets(config)


def cleanup_game_authorizations(db: Session, now: int | None = None) -> int:
    """Remove game licenses expired beyond the fixed seven-day retention."""
    current = int(time.time()) if now is None else int(now)
    threshold = current - GAME_AUTH_RETENTION_SECONDS
    result = db.execute(
        delete(GameAuthorization).where(
            GameAuthorization.expire > 0,
            GameAuthorization.expire <= threshold,
        )
    )
    return int(result.rowcount or 0)


def _websocket_key_mask(value: object) -> str:
    """Return a bounded display mask without exposing the supplied card."""

    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 10:
        return text[:3] + "****" + text[-2:]
    return text[:6] + "****" + text[-4:]


def _get_im3_authorization(
    db: Session, group_id: str, *, for_update: bool = False
) -> Im3Authorization | GameAuthorization | None:
    """Load the external IM3 card, with a read-only legacy fallback.

    The fallback exists only for databases upgraded from releases that put
    the IM3 fields on 'portal_game_authorizations'. All new writes go to
    'portal_im3_authorizations' and therefore cannot create or modify a
    game-license row.
    """

    statement = select(Im3Authorization).where(
        Im3Authorization.group_id == group_id
    )
    if for_update:
        statement = statement.with_for_update()
    row = db.scalar(statement)
    if row is not None:
        return row
    legacy_statement = select(GameAuthorization).where(
        GameAuthorization.group_id == group_id,
        GameAuthorization.websocket_key_ciphertext != "",
    )
    if for_update:
        legacy_statement = legacy_statement.with_for_update()
    return db.scalar(legacy_statement)


def _im3_key_ciphertext(row: Im3Authorization | GameAuthorization | None) -> str:
    if row is None:
        return ""
    return str(
        getattr(row, "key_ciphertext", "")
        or getattr(row, "websocket_key_ciphertext", "")
        or ""
    )


def _im3_key_mask(row: Im3Authorization | GameAuthorization | None) -> str:
    if row is None:
        return ""
    return str(
        getattr(row, "key_mask", "")
        or getattr(row, "websocket_key_mask", "")
        or ""
    )


def _im3_state(row: Im3Authorization | GameAuthorization | None) -> str:
    if row is None:
        return "missing"
    return str(
        getattr(row, "state", "")
        or getattr(row, "websocket_state", "")
        or "missing"
    ).strip().lower()


def _im3_verified_at(row: Im3Authorization | GameAuthorization | None) -> int:
    if row is None:
        return 0
    try:
        return max(
            0,
            int(
                getattr(row, "verified_at", 0)
                or getattr(row, "websocket_verified_at", 0)
                or 0
            ),
        )
    except (TypeError, ValueError, OverflowError):
        return 0


def _im3_validation_version(row: Im3Authorization | GameAuthorization | None) -> str:
    if row is None:
        return ""
    return str(
        getattr(row, "validation_version", "")
        or getattr(row, "websocket_validation_version", "")
        or ""
    ).strip()


def get_group_websocket_access_key(
    db: Session, group_id: object, settings=None
) -> str:
    """Decrypt one group's externally issued IM3 card internally."""

    normalized_group_id = str(group_id or "").strip()
    if not GROUP_ID_RE.fullmatch(normalized_group_id):
        return ""
    row = _get_im3_authorization(db, normalized_group_id)
    ciphertext = _im3_key_ciphertext(row)
    if not ciphertext:
        return ""
    current_settings = settings or _read_settings_fallback()
    return _decrypt_token(ciphertext, current_settings)


def get_group_websocket_access_status(db: Session, group_id: object) -> dict[str, Any]:
    """Return only safe status for the external IM3 website card."""

    normalized_group_id = str(group_id or "").strip()
    if not GROUP_ID_RE.fullmatch(normalized_group_id):
        return {
            "credential_type": "im3_external",
            "state": "missing",
            "configured": False,
            "card_mask": "",
        }
    row = _get_im3_authorization(db, normalized_group_id)
    if row is None:
        return {
            "credential_type": "im3_external",
            "state": "missing",
            "configured": False,
            "card_mask": "",
        }
    state = _im3_state(row)
    if state == "verified" and _im3_validation_version(row) != IM3_CARD_VALIDATION_VERSION:
        state = "unverified"
    if state not in {"verified", "failed", "missing", "unverified"}:
        state = "missing"
    return {
        "credential_type": "im3_external",
        "state": state,
        "configured": bool(_im3_key_ciphertext(row)),
        "card_mask": _im3_key_mask(row)[:80],
        "verified_at": _im3_verified_at(row),
    }


def save_group_websocket_access_key(
    db: Session,
    group_id: object,
    key: object,
    settings=None,
    *,
    client_id: object = "",
) -> dict[str, Any]:
    """Encrypt and persist an external IM3 card after confirmed authorization."""

    normalized_group_id = str(group_id or "").strip()
    if not GROUP_ID_RE.fullmatch(normalized_group_id):
        raise ValueError("群号格式无效")
    normalized_key = normalize_im3_access_key(key)
    current_settings = settings or _read_settings_fallback()
    if current_settings is None:
        raise ValueError("网页加密配置不可用，请检查 session secret")
    row = db.scalar(
        select(Im3Authorization)
        .where(Im3Authorization.group_id == normalized_group_id)
        .with_for_update()
    )
    if row is None:
        row = Im3Authorization(group_id=normalized_group_id)
        db.add(row)
    row.key_ciphertext = _encrypt_token(normalized_key, current_settings)
    row.key_mask = _websocket_key_mask(normalized_key)
    row.state = "verified"
    row.verified_at = int(time.time())
    row.validation_version = IM3_CARD_VALIDATION_VERSION
    # The production session factory disables autoflush.  Flush this new
    # independent row before reading its status so the first save cannot
    # incorrectly report "missing" while the later commit succeeds.
    db.flush()
    return get_group_websocket_access_status(db, normalized_group_id)


def get_group_websocket_access_keys(
    db: Session, group_ids: object, settings=None, *, client_id: object = ""
) -> dict[str, str]:
    """Return verified IM3 external cards for the authenticated Windows worker.

    This is used only on the already authenticated outbound sync response.
    The browser never receives the result and audit records contain only a
    mask, so a web login cannot turn it into a credential disclosure.
    """

    if not isinstance(group_ids, (list, tuple, set, frozenset)):
        return {}
    result: dict[str, str] = {}
    normalized_client_id = str(client_id or "").strip()[:64]
    now = int(time.time())
    for group_id in group_ids:
        normalized_group_id = str(group_id or "").strip()
        if not GROUP_ID_RE.fullmatch(normalized_group_id):
            continue
        game_row = db.scalar(
            select(GameAuthorization).where(
                GameAuthorization.group_id == normalized_group_id
            )
        )
        im3_row = _get_im3_authorization(db, normalized_group_id)
        if (
            game_row is None
            or im3_row is None
            or _im3_state(im3_row) != "verified"
            or _im3_validation_version(im3_row) != IM3_CARD_VALIDATION_VERSION
            or not _im3_key_ciphertext(im3_row)
            or str(game_row.state or "").strip().lower() != "active"
            or (
                normalized_client_id
                and str(game_row.client_id or "") != normalized_client_id
            )
        ):
            continue
        expire = int(game_row.expire or 0)
        if expire != -1 and (expire <= 0 or expire <= now):
            continue
        key = get_group_websocket_access_key(db, normalized_group_id, settings)
        if key:
            result[normalized_group_id] = key
    return result


def mark_group_websocket_access_failed(db: Session, group_id: object) -> None:
    """Stop sync delivery after the linked website rejects a saved card."""

    normalized_group_id = str(group_id or "").strip()
    if not GROUP_ID_RE.fullmatch(normalized_group_id):
        return
    row = db.scalar(
        select(Im3Authorization)
        .where(Im3Authorization.group_id == normalized_group_id)
        .with_for_update()
    )
    if row is not None and row.key_ciphertext:
        row.state = "failed"
        return
    # Compatibility for one upgrade window: old rows are never newly created,
    # but a failed legacy credential must still stop being delivered.
    legacy = db.scalar(
        select(GameAuthorization)
        .where(GameAuthorization.group_id == normalized_group_id)
        .with_for_update()
    )
    if legacy is not None and legacy.websocket_key_ciphertext:
        legacy.websocket_state = "failed"


def mark_group_websocket_access_verified(db: Session, group_id: object) -> None:
    """Mark the independent IM3 card verified after a successful retest."""

    normalized_group_id = str(group_id or "").strip()
    if not GROUP_ID_RE.fullmatch(normalized_group_id):
        return
    row = db.scalar(
        select(Im3Authorization)
        .where(Im3Authorization.group_id == normalized_group_id)
        .with_for_update()
    )
    if row is None or not row.key_ciphertext:
        return
    row.state = "verified"
    row.verified_at = int(time.time())
    row.validation_version = IM3_CARD_VALIDATION_VERSION


def apply_game_authorization_heartbeat(
    db: Session,
    client_id: object,
    group_id: object,
    raw_auth: object,
) -> None:
    """Persist a bounded game-license heartbeat without accepting the card."""
    normalized_group_id = str(group_id or "").strip()
    normalized_client_id = str(client_id or "").strip()[:64]
    if not GROUP_ID_RE.fullmatch(normalized_group_id) or not normalized_client_id:
        return
    auth = raw_auth if isinstance(raw_auth, dict) else {}
    state = str(auth.get("state", "missing") or "missing").strip().lower()
    if state not in GAME_AUTH_STATES:
        state = "missing"
    try:
        expire = int(auth.get("expire", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        expire = 0
    if expire < -1:
        expire = 0
    if state == "active" and expire == 0:
        state = "missing"
    if expire == -1:
        state = "active"
    fingerprint = str(auth.get("card_fingerprint", "") or "").strip().lower()
    if not AUTH_CARD_FINGERPRINT_RE.fullmatch(fingerprint):
        fingerprint = ""
    if state == "active" and not fingerprint:
        # An active game heartbeat must prove possession of the generated
        # card through the shared Card Pepper; a bare state flag is not enough.
        state = "missing"
        expire = 0
    card_mask = str(auth.get("card_mask", "") or "")[:64]
    try:
        activated_at = max(0, int(auth.get("activated_at", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        activated_at = 0
    row = db.scalar(
        select(GameAuthorization)
        .where(GameAuthorization.group_id == normalized_group_id)
        .with_for_update()
    )
    if row is None:
        row = GameAuthorization(group_id=normalized_group_id)
        db.add(row)
    row.client_id = normalized_client_id
    row.state = state
    row.expire = expire
    row.card_fingerprint = fingerprint
    row.card_mask = card_mask if fingerprint else ""
    row.activated_at = activated_at
    row.user_id = str(auth.get("user_id", "") or "")[:32]
    row.last_heartbeat_at = utcnow()


def get_game_authorization_for_client(
    db: Session, client_id: object, group_id: object
) -> dict[str, Any]:
    """Return an active game license only for its current sync client."""
    normalized_client_id = str(client_id or "").strip()[:64]
    normalized_group_id = str(group_id or "").strip()
    if not normalized_client_id or not GROUP_ID_RE.fullmatch(normalized_group_id):
        return {"state": "missing", "code": "invalid_scope"}
    row = db.scalar(
        select(GameAuthorization).where(
            GameAuthorization.group_id == normalized_group_id,
            GameAuthorization.client_id == normalized_client_id,
        )
    )
    if row is None:
        # Distinguish a missing card from a valid card still owned by another
        # Windows client.  The latter is the common cause of an apparently
        # permanent database snapshot 409 after reinstalling Windows.
        other_client = db.scalar(
            select(GameAuthorization).where(
                GameAuthorization.group_id == normalized_group_id,
            )
        )
        if (
            other_client is not None
            and str(other_client.client_id or "").strip()
            and str(other_client.state or "").strip().lower() == "active"
        ):
            return {"state": "missing", "code": "game_license_client_mismatch"}
        return {"state": "missing", "code": "game_license_required"}
    now = int(time.time())
    expire = int(row.expire or 0)
    if row.state != "active":
        state = "expired" if row.state == "expired" else "missing"
        return {
            "state": state,
            "code": "game_license_expired" if state == "expired" else "game_license_required",
        }
    if expire == 0:
        return {"state": "missing", "code": "game_license_required"}
    if expire != -1 and expire <= now:
        return {"state": "expired", "code": "game_license_expired"}
    return {"state": "active", "expire": expire, "group_id": normalized_group_id}


def get_game_authorization_status(db: Session, group_id: object) -> dict[str, Any]:
    """Return a safe, group-scoped game heartbeat view for the web UI.

    The raw card and fingerprint never leave the local Windows plugin.  This
    projection is intentionally read-only and is separate from ordinary QQ
    authorization so a database-only group can still show its game status.
    """
    normalized_group_id = str(group_id or "").strip()
    if not GROUP_ID_RE.fullmatch(normalized_group_id):
        return {"state": "missing", "active": False, "expire": 0, "code": "invalid_scope"}
    row = db.scalar(
        select(GameAuthorization).where(GameAuthorization.group_id == normalized_group_id)
    )
    if row is None:
        return {
            "credential_type": "game_owner",
            "state": "missing",
            "active": False,
            "expire": 0,
            "code": "game_license_required",
            "card_mask": "",
            "client_id": "",
            "last_heartbeat_at": None,
        }
    now = int(time.time())
    expire = int(row.expire or 0)
    state = str(row.state or "missing").strip().lower()
    if state == "active" and expire != -1 and (expire <= 0 or expire <= now):
        state = "expired" if expire > 0 else "missing"
    if state not in GAME_AUTH_STATES:
        state = "missing"
    return {
        "credential_type": "game_owner",
        "state": state,
        "active": state == "active",
        "expire": expire if state != "missing" else 0,
        "code": "connected" if state == "active" else (
            "game_license_expired" if state == "expired" else "game_license_required"
        ),
        "card_mask": str(row.card_mask or "")[:64],
        "client_id": str(row.client_id or "")[:64],
        "last_heartbeat_at": row.last_heartbeat_at,
    }

# ``sync_error`` is intentionally a short protocol summary, not an exception
# message.  Translate the safe stage into wording that explains what failed
# without exposing a local path, URL, token, or server response body.
_SYNC_FAILURE_TEXT = {
    "local_config_save_failed": "Windows 未能写入本地分群配置",
    "local_apply_failed": "Windows 应用网页配置时失败",
    "ack_failed": "Windows 已处理配置，但同步确认未能返回网页",
    "ack_not_confirmed": "Windows 未收到网页对配置应用的确认",
}


def _sync_failure_display(value: object) -> str:
    summary = str(value or "").strip()
    stage = summary.split(":", 1)[0]
    return _SYNC_FAILURE_TEXT.get(stage, "Windows 本地应用失败，正在等待重试")


def _sync_last_seen_text(value: datetime | None) -> str:
    if value is None:
        return "服务器尚未收到 Windows 同步请求"
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return f"最近收到 Windows 同步：{value.strftime('%Y-%m-%d %H:%M:%S')} UTC"


def _sync_saved_at_text(snapshot: GroupConfigSnapshot | None) -> str:
    """Format the last web-save timestamp for a template-safe status view."""

    value = getattr(snapshot, "updated_at", None) if snapshot is not None else None
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def _safe_bigint(value: Any, default: int = 0) -> int:
    """Return a database-safe integer for untrusted sync payload fields."""
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if _MIN_BIGINT <= parsed <= _MAX_BIGINT else default


def bounded_text(value: object, limit: int = 500) -> str:
    """Normalize user-supplied text before storing it in portal runtime JSON."""
    try:
        maximum = max(0, min(4096, int(limit)))
    except (TypeError, ValueError, OverflowError):
        maximum = 500
    text_value = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text_value[:maximum]


def _normalized_auth_state(value: Any) -> str:
    state = str(value or "missing").strip().lower()
    return state if state in AUTH_STATES else "missing"


def _safe_runtime_summary(value: Any) -> dict[str, Any]:
    """Validate the bounded, read-only runtime data sent by Windows.

    Runtime records are deliberately kept outside ``config_json``.  The
    Windows client is the source of truth, but the portal must still cap every
    string/list before rendering it in a browser or persisting it in MySQL.
    """
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}

    # Define the runtime validators before any panel is decoded.  These
    # helpers used to be declared below the blacklist panels, which made
    # Python treat them as local variables before initialization.  When a
    # Windows snapshot contained a global blacklist, the whole runtime
    # payload then failed validation and Linux rendered stale/empty panels.
    def bounded_timestamp(raw: Any) -> int:
        parsed = _safe_bigint(raw, 0)
        return parsed if 0 <= parsed <= 2**31 - 1 else 0

    def bounded_text(raw: Any, limit: int) -> str:
        return str(raw or "").replace("\r", " ").replace("\n", " ")[:limit]

    raw_blacklist = value.get("blacklist")
    if isinstance(raw_blacklist, dict):
        entries: list[dict[str, Any]] = []
        raw_entries = raw_blacklist.get("entries", [])
        if isinstance(raw_entries, list):
            for raw in raw_entries[:200]:
                if not isinstance(raw, dict):
                    continue
                user_id = str(raw.get("user_id", "")).strip()[:32]
                if not user_id:
                    continue
                expire = raw.get("expire_time", 0)
                try:
                    expire = max(0, int(expire or 0))
                except (TypeError, ValueError, OverflowError):
                    expire = 0
                entries.append(
                    {
                        "user_id": user_id,
                        "user_name": bounded_text(raw.get("user_name"), 80),
                        "active": bool(raw.get("active", raw.get("is_active", True))),
                        "ban_type": str(raw.get("ban_type", ""))[:24],
                        "reason": str(raw.get("reason", ""))[:240],
                        "expire_time": expire,
                    }
                )
        try:
            total = max(0, int(raw_blacklist.get("total", len(entries)) or 0))
        except (TypeError, ValueError, OverflowError):
            total = len(entries)
        try:
            active = max(0, int(raw_blacklist.get("active", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            active = sum(1 for item in entries if item["active"])
        result["blacklist"] = {
            "available": True,
            "message": "",
            "total": min(total, 100000),
            "active": min(active, 100000),
            "entries": entries,
        }
    raw_global_blacklist = value.get("global_blacklist")
    if isinstance(raw_global_blacklist, dict):
        entries: list[dict[str, Any]] = []
        raw_entries = raw_global_blacklist.get("entries", [])
        if isinstance(raw_entries, list):
            for raw in raw_entries[:200]:
                if not isinstance(raw, dict):
                    continue
                user_id = str(raw.get("user_id", "")).strip()[:32]
                if not user_id:
                    continue
                entries.append(
                    {
                        "user_id": user_id,
                        "user_name": bounded_text(raw.get("user_name"), 80),
                        "active": bool(raw.get("active", raw.get("is_active", True))),
                        "ban_type": bounded_text(raw.get("ban_type"), 24),
                        "reason": bounded_text(raw.get("reason"), 240),
                        "operator_id": bounded_text(raw.get("operator_id"), 32),
                        "added_at": bounded_timestamp(raw.get("added_at")),
                        "expire_time": bounded_timestamp(raw.get("expire_time")),
                        "allowed_in_group": bool(raw.get("allowed_in_group", False)),
                    }
                )
        def global_count(name: str, fallback: int = 0) -> int:
            return min(max(0, _safe_bigint(raw_global_blacklist.get(name), fallback)), 100000)
        result["global_blacklist"] = {
            "available": True,
            "total": global_count("total", len(entries)),
            "active": global_count("active"),
            "permanent": global_count("permanent"),
            "temporary": global_count("temporary"),
            "inactive": global_count("inactive"),
            "entries": entries,
        }
    raw_global_blacklist_admin = value.get("global_blacklist_admin")
    if isinstance(raw_global_blacklist_admin, dict):
        entries: list[dict[str, Any]] = []
        raw_entries = raw_global_blacklist_admin.get("entries", [])
        if isinstance(raw_entries, list):
            for raw in raw_entries[:500]:
                if not isinstance(raw, dict):
                    continue
                user_id = str(raw.get("user_id", "")).strip()[:32]
                if not user_id:
                    continue
                allowed_groups = raw.get("allowed_groups", [])
                allowed_groups = (
                    [bounded_text(item, 32) for item in allowed_groups[:500] if str(item).strip()]
                    if isinstance(allowed_groups, list)
                    else []
                )
                entries.append(
                    {
                        "user_id": user_id,
                        "user_name": bounded_text(raw.get("user_name"), 80),
                        "active": bool(raw.get("active", raw.get("is_active", True))),
                        "ban_type": bounded_text(raw.get("ban_type"), 24),
                        "reason": bounded_text(raw.get("reason"), 240),
                        "operator_id": bounded_text(raw.get("operator_id", raw.get("added_by")), 32),
                        "added_at": bounded_timestamp(raw.get("added_at")),
                        "expire_time": bounded_timestamp(raw.get("expire_time")),
                        "allowed_groups": allowed_groups,
                    }
                )
        def admin_global_count(name: str, fallback: int = 0) -> int:
            return min(max(0, _safe_bigint(raw_global_blacklist_admin.get(name), fallback)), 100000)
        result["global_blacklist_admin"] = {
            "available": True,
            "total": admin_global_count("total", len(entries)),
            "active": admin_global_count("active", sum(1 for item in entries if item["active"])),
            "permanent": admin_global_count("permanent"),
            "temporary": admin_global_count("temporary"),
            "inactive": admin_global_count("inactive"),
            "entries": entries,
        }
    raw_stats = value.get("stats")
    if isinstance(raw_stats, dict):
        raw_group = raw_stats.get("group", {})
        raw_group = raw_group if isinstance(raw_group, dict) else {}

        def count(name: str) -> int:
            try:
                return max(0, int(raw_group.get(name, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                return 0

        ranking: list[dict[str, Any]] = []
        raw_ranking = raw_stats.get("ranking", [])
        if isinstance(raw_ranking, list):
            for index, raw in enumerate(raw_ranking[:100], 1):
                if not isinstance(raw, dict):
                    continue
                user_id = str(raw.get("user_id", "")).strip()[:32]
                if not user_id:
                    continue
                def rank_count(name: str) -> int:
                    try:
                        return max(0, int(raw.get(name, 0) or 0))
                    except (TypeError, ValueError, OverflowError):
                        return 0
                ranking.append(
                    {
                        "rank": index,
                        "user_id": user_id,
                        "user_name": str(raw.get("user_name", ""))[:80],
                        "points": rank_count("points"),
                        "signin_count": rank_count("signin_count"),
                    }
                )
        steam_bindings: list[dict[str, Any]] = []
        raw_bindings = raw_stats.get("steam_bindings", [])
        if isinstance(raw_bindings, list):
            for raw in raw_bindings[:100]:
                if not isinstance(raw, dict):
                    continue
                steam_id = str(raw.get("steam_id", "")).strip()
                user_id = str(raw.get("user_id", "")).strip()
                if not re.fullmatch(r"\d{17}", steam_id) or not user_id:
                    continue
                def binding_count(name: str) -> int:
                    try:
                        return max(0, int(raw.get(name, 0) or 0))
                    except (TypeError, ValueError, OverflowError):
                        return 0
                steam_bindings.append(
                    {
                        "steam_id": steam_id,
                        "user_id": user_id[:32],
                        "user_name": str(raw.get("user_name", ""))[:80],
                        "bound_at": binding_count("bound_at"),
                        "last_signin": str(raw.get("last_signin", ""))[:16],
                        "signin_count": binding_count("signin_count"),
                        "monthly_count": binding_count("monthly_count"),
                        "weekly_count": binding_count("weekly_count"),
                    }
                )
        result["stats"] = {
            "available": True,
            "message": "",
            "group": {
                "total_users": count("total_users"),
                "today_signins": count("today_signins"),
                "total_points": count("total_points"),
                "avg_points": count("avg_points"),
            },
            "ranking": ranking,
            "steam_bindings": steam_bindings,
        }
    # The following panels are read-only operational projections.  Keep the
    # same strict limits here as on the Windows side because a compromised or
    # outdated client must not be able to make the portal store unbounded JSON.
    raw_members = value.get("members")
    # Accept the short-lived payload shapes used by older Windows clients.
    # Some builds sent the directory as ``member_list``/``items`` or sent the
    # list itself instead of wrapping it in a panel object. Treating those
    # forms as empty here made the portal report a successful sync while the
    # selector had no options.
    if isinstance(raw_members, list):
        raw_members = {"members": raw_members, "count": len(raw_members)}
    elif isinstance(raw_members, dict):
        raw_members = dict(raw_members)
    elif isinstance(value.get("member_list"), list):
        raw_members = {
            "members": value.get("member_list"),
            "count": len(value.get("member_list") or []),
        }
    elif isinstance(value.get("member_directory"), dict):
        raw_members = value.get("member_directory")
    if isinstance(raw_members, dict):
        members: list[dict[str, Any]] = []
        rows = raw_members.get(
            "members",
            raw_members.get("items", raw_members.get("rows", [])),
        )
        if isinstance(rows, list):
            for raw in rows[:10000]:
                if not isinstance(raw, dict):
                    continue
                user_id = bounded_text(
                    raw.get(
                        "user_id",
                        raw.get(
                            "userId",
                            raw.get("uin", raw.get("qq", raw.get("uid", ""))),
                        ),
                    ),
                    24,
                )
                if not user_id.isdigit() or not 5 <= len(user_id) <= 20:
                    continue
                card = bounded_text(raw.get("card") or raw.get("group_card"), 80)
                nickname = bounded_text(
                    raw.get("nickname") or raw.get("nick"), 80
                )
                resolved_name = bounded_text(
                    nickname or card or raw.get("user_name") or raw.get("display_name"),
                    80,
                )
                user_name = resolved_name or "未获取姓名"
                role = bounded_text(
                    raw.get("role") or raw.get("permission") or "member", 16
                ).lower()
                role = {
                    "group_owner": "owner",
                    "groupowner": "owner",
                    "群主": "owner",
                    "administrator": "admin",
                    "group_admin": "admin",
                    "groupadmin": "admin",
                    "管理员": "admin",
                    "群管理员": "admin",
                }.get(role, role)
                if role not in {"owner", "admin", "member"}:
                    role = "member"
                # ``level`` is the group activity level. QQ account level is
                # carried separately as qq_level/qqLevel by NapCat.
                group_level_raw = raw.get("group_level", raw.get("level"))
                qq_level_raw = raw.get("qq_level")
                if qq_level_raw in (None, "", "未返回"):
                    qq_level_raw = raw.get("qqLevel")
                if qq_level_raw in (None, "", "未返回"):
                    qq_level_raw = raw.get("q_level", raw.get("qLevel"))
                if qq_level_raw in (None, "", "未返回"):
                    qq_level_raw = raw.get("account_level", raw.get("accountLevel"))
                if qq_level_raw in (None, "", "未返回") and isinstance(raw.get("level_info"), dict):
                    level_info = raw.get("level_info")
                    qq_level_raw = level_info.get("qq_level", level_info.get("qqLevel"))
                if qq_level_raw in (None, "", "未返回"):
                    qq_level_raw = raw.get("qq_level_text")

                def _member_level_number(value: object) -> int:
                    if isinstance(value, dict):
                        value = value.get("level", value.get("value", ""))
                    level_match = re.search(r"(?<!\d)(\d{1,3})(?!\d)", str(value or ""))
                    parsed = level_match.group(1) if level_match else value
                    return max(0, min(999, _safe_bigint(parsed, 0)))

                group_level_value = _member_level_number(group_level_raw)
                qq_level_value = _member_level_number(qq_level_raw) if qq_level_raw not in (None, "", "未返回") else "未返回"
                last_message_at = bounded_timestamp(raw.get("last_message_at"))
                inactive_seconds = max(0, int(time.time()) - last_message_at) if last_message_at else 0
                activity_counts: dict[str, int] = {}
                for field in (
                    "text_count", "image_count", "video_count", "file_count",
                    "record_count", "forward_count", "other_count",
                ):
                    activity_counts[field] = max(
                        0,
                        min(1_000_000, _safe_bigint(raw.get(field), 0)),
                    )
                members.append(
                    {
                        "user_id": user_id,
                        "user_name": user_name,
                        "display_name": user_name,
                        "name_available": bool(resolved_name),
                        "card": card,
                        "nickname": nickname,
                        "role": role,
                        "group_level": group_level_value,
                        "level": group_level_value,
                        "qq_level": qq_level_value,
                        "level_text": bounded_text(
                            qq_level_raw if qq_level_raw not in (None, "", "未返回") else "未返回",
                            32,
                        ),
                        "title": bounded_text(raw.get("title"), 80),
                        "join_time": bounded_timestamp(raw.get("join_time")),
                        "last_sent_time": bounded_timestamp(raw.get("last_sent_time")),
                        "mute_until": bounded_timestamp(raw.get("mute_until", raw.get("shut_up_timestamp"))),
                        "mute_until_text": bounded_text(raw.get("mute_until_text"), 32) or "未禁言",
                        "activity_count": max(0, min(1000000, _safe_bigint(raw.get("activity_count"), 0))),
                        "last_message_at": last_message_at,
                        "inactive_seconds": min(3650 * 86400, inactive_seconds),
                        **activity_counts,
                        "last_message_text": bounded_text(raw.get("last_message_text"), 160),
                        "last_message_types": bounded_text(raw.get("last_message_types"), 120),
                    }
                )
        try:
            reported_count = min(
                max(0, _safe_bigint(raw_members.get("count"), len(members))),
                10000,
            )
        except (TypeError, ValueError, OverflowError):
            reported_count = len(members)
        # A non-zero reported count with no valid rows is useful diagnostic
        # information, but never let it make the UI claim that selectable
        # rows exist. The dropdown is driven by the normalized list.
        result["members"] = {
            "available": True,
            "count": len(members) if members else reported_count,
            "valid_count": len(members),
            "updated_at": bounded_timestamp(raw_members.get("updated_at")),
            "message": bounded_text(raw_members.get("message"), 500),
            "levels_pending": bool(raw_members.get("levels_pending", False)),
            "upload_pending": bool(raw_members.get("upload_pending", False)),
            "partial": bool(raw_members.get("partial", False)),
            "expected_count": min(
                10000,
                max(0, _safe_bigint(raw_members.get("expected_count"), 0)),
            ),
            "members": members,
        }

    raw_group_activity = value.get("group_activity")
    if isinstance(raw_group_activity, dict):
        last_message_at = bounded_timestamp(raw_group_activity.get("last_message_at"))
        active_members = max(
            0,
            min(100_000, _safe_bigint(raw_group_activity.get("active_members"), 0)),
        )
        result["group_activity"] = {
            "available": bool(raw_group_activity.get("available", True)),
            "window_days": max(
                1,
                min(365, _safe_bigint(raw_group_activity.get("window_days"), 30)),
            ),
            "message_count": max(
                0,
                min(10_000_000, _safe_bigint(raw_group_activity.get("message_count"), 0)),
            ),
            "active_members": active_members,
            "last_message_at": last_message_at,
            "inactive_seconds": min(
                3650 * 86400,
                max(0, int(time.time()) - last_message_at) if last_message_at else 0,
            ),
        }

    raw_audit = value.get("audit")
    if isinstance(raw_audit, dict):
        recent: list[dict[str, Any]] = []
        rows = raw_audit.get("recent", [])
        if isinstance(rows, list):
            for raw in rows[:50]:
                if not isinstance(raw, dict):
                    continue
                recent.append(
                    {
                        "user_id": bounded_text(raw.get("user_id"), 32),
                        "nickname": bounded_text(raw.get("nickname"), 80),
                        "user_name": bounded_text(raw.get("user_name"), 80),
                        "action": bounded_text(raw.get("action"), 32),
                        "reason": bounded_text(raw.get("reason"), 160),
                        "timestamp": bounded_timestamp(raw.get("timestamp")),
                    }
                )
        result["audit"] = {
            "pending_count": max(0, min(_safe_bigint(raw_audit.get("pending_count")), 100000)),
            "total_processed": max(0, min(_safe_bigint(raw_audit.get("total_processed")), 1000000)),
            "approved": max(0, min(_safe_bigint(raw_audit.get("approved")), 1000000)),
            "rejected": max(0, min(_safe_bigint(raw_audit.get("rejected")), 1000000)),
            "active_challenges": max(0, min(_safe_bigint(raw_audit.get("active_challenges")), 100000)),
            "recent": recent,
        }

    raw_kick = value.get("kick")
    if isinstance(raw_kick, dict):
        records: list[dict[str, Any]] = []
        rows = raw_kick.get("records", [])
        if isinstance(rows, list):
            for raw in rows[:50]:
                if not isinstance(raw, dict):
                    continue
                records.append(
                    {
                        "user_id": bounded_text(raw.get("user_id"), 32),
                        "user_name": bounded_text(raw.get("user_name"), 80),
                        "count": max(1, min(_safe_bigint(raw.get("count"), 1), 1000000)),
                        "reason": bounded_text(raw.get("reason"), 160),
                        "last_kicked_at": bounded_timestamp(raw.get("last_kicked_at")),
                    }
                )
        result["kick"] = {
            "total_kicks": max(0, min(_safe_bigint(raw_kick.get("total_kicks")), 1000000)),
            "unique_users": max(0, min(_safe_bigint(raw_kick.get("unique_users")), 1000000)),
            "today_kicks": max(0, min(_safe_bigint(raw_kick.get("today_kicks")), 1000000)),
            "week_kicks": max(0, min(_safe_bigint(raw_kick.get("week_kicks")), 1000000)),
            "records": records,
        }

    raw_mute = value.get("mute")
    if isinstance(raw_mute, dict):
        active: list[dict[str, Any]] = []
        rows = raw_mute.get("active", [])
        if isinstance(rows, list):
            for raw in rows[:100]:
                if not isinstance(raw, dict):
                    continue
                active.append(
                    {
                        "user_id": bounded_text(raw.get("user_id"), 32),
                        "user_name": bounded_text(raw.get("user_name"), 80),
                        "duration": max(0, min(_safe_bigint(raw.get("duration")), 2592000)),
                        "expire_time": bounded_timestamp(raw.get("expire_time")),
                        "reason": bounded_text(raw.get("reason"), 160),
                        "mute_level": max(1, min(_safe_bigint(raw.get("mute_level"), 1), 100)),
                    }
                )
        result["mute"] = {
            "total_muted": max(0, min(_safe_bigint(raw_mute.get("total_muted")), 1000000)),
            "currently_muted": max(0, min(_safe_bigint(raw_mute.get("currently_muted")), 1000000)),
            "total_mute_times": max(0, min(_safe_bigint(raw_mute.get("total_mute_times")), 1000000)),
            "active": active,
        }

    raw_recall = value.get("recall")
    if isinstance(raw_recall, dict):
        recent: list[dict[str, Any]] = []
        rows = raw_recall.get("recent", [])
        if isinstance(rows, list):
            for raw in rows[:50]:
                if not isinstance(raw, dict):
                    continue
                recent.append(
                    {
                        "user_id": bounded_text(raw.get("user_id"), 32),
                        "user_name": bounded_text(raw.get("user_name"), 80),
                        "message_id": bounded_text(raw.get("message_id"), 64),
                        "dedupe_key": bounded_text(raw.get("dedupe_key"), 128),
                        "reason": bounded_text(raw.get("reason"), 160),
                        "content": bounded_text(raw.get("content"), 1200),
                        "recalled_at": bounded_timestamp(raw.get("recalled_at")),
                    }
                )
        result["recall"] = {
            "total_recalls": max(0, min(_safe_bigint(raw_recall.get("total_recalls")), 1000000)),
            "keywords_count": max(0, min(_safe_bigint(raw_recall.get("keywords_count")), 100000)),
            "regex_count": max(0, min(_safe_bigint(raw_recall.get("regex_count")), 100000)),
            "whitelist_count": max(0, min(_safe_bigint(raw_recall.get("whitelist_count")), 100000)),
            "self_recall_enabled": bool(raw_recall.get("self_recall_enabled", False)),
            "recent": recent,
        }

    raw_cache = value.get("message_cache")
    if isinstance(raw_cache, dict):
        records: list[dict[str, Any]] = []
        rows = raw_cache.get("records", [])
        if isinstance(rows, list):
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                timestamp = bounded_timestamp(raw.get("timestamp"))
                records.append(
                    {
                        "timestamp": timestamp,
                        "timestamp_text": (
                            datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                            if timestamp else ""
                        ),
                        "user_id": bounded_text(raw.get("user_id"), 32),
                        "nickname": bounded_text(raw.get("nickname"), 80),
                        "user_name": bounded_text(
                            raw.get("nickname") or raw.get("user_name"), 80
                        ),
                        "message_id": bounded_text(raw.get("message_id"), 64),
                        # Keep the Windows-side content identity through the
                        # portal normalizer. Different OneBot accounts may
                        # assign different local message IDs to the same QQ
                        # message; the dedupe key is the stable cross-adapter
                        # identity used by runtime merges and event backfill.
                        "dedupe_key": bounded_text(raw.get("dedupe_key"), 128),
                        "recalled": bool(raw.get("recalled", False)),
                        "recalled_at": bounded_timestamp(raw.get("recalled_at")),
                        "recalled_at_text": (
                            datetime.fromtimestamp(
                                bounded_timestamp(raw.get("recalled_at")),
                                tz=timezone.utc,
                            ).strftime("%Y-%m-%d %H:%M:%S UTC")
                            if bounded_timestamp(raw.get("recalled_at"))
                            else ""
                        ),
                        "recall_reason": bounded_text(raw.get("recall_reason"), 160),
                        "content": bounded_text(raw.get("content"), 1200),
                        "direction": bounded_text(raw.get("direction"), 16) or "inbound",
                        "media_urls": [
                            bounded_text(url, 2048)
                            for url in raw.get("media_urls", [])[:4]
                            if isinstance(url, str) and url.startswith("https://")
                        ] if isinstance(raw.get("media_urls"), list) else [],
                        "media_items": [
                            {
                                "kind": bounded_text(item.get("kind"), 16) or "file",
                                "url": bounded_text(item.get("url"), 700000),
                                "name": bounded_text(item.get("name"), 160),
                            }
                            for item in raw.get("media_items", [])[:8]
                            if isinstance(item, dict)
                            and (
                                bounded_text(item.get("url"), 700000).startswith("https://")
                                or (
                                    bounded_text(item.get("url"), 700000).startswith("data:image/")
                                    and ";base64," in bounded_text(item.get("url"), 700000)
                                    and len(bounded_text(item.get("url"), 700000)) <= 700000
                                )
                            )
                        ] if isinstance(raw.get("media_items"), list) else [],
                        "face_labels": [
                            bounded_text(label, 80)
                            for label in raw.get("face_labels", [])[:8]
                            if isinstance(label, str) and bounded_text(label, 80)
                        ] if isinstance(raw.get("face_labels"), list) else [],
                        "has_image": bool(raw.get("has_image", False)),
                        "image_count": max(0, min(_safe_bigint(raw.get("image_count")), 100)),
                        "video_count": max(0, min(_safe_bigint(raw.get("video_count")), 100)),
                        "file_count": max(0, min(_safe_bigint(raw.get("file_count")), 100)),
                        "record_count": max(0, min(_safe_bigint(raw.get("record_count")), 100)),
                        "forward_count": max(0, min(_safe_bigint(raw.get("forward_count")), 100)),
                        "message_types": bounded_text(raw.get("message_types"), 120),
                    }
                )
        result["message_cache"] = {
            "available": bool(raw_cache.get("available", True)),
            "source": bounded_text(raw_cache.get("source"), 64) or "windows_runtime",
            "count": len(records),
            "records": records,
        }
    raw_errors = value.get("errors")
    if isinstance(raw_errors, dict):
        recent_errors: list[dict[str, Any]] = []
        rows = raw_errors.get("recent", [])
        if isinstance(rows, list):
            for raw in rows[:30]:
                if not isinstance(raw, dict):
                    continue
                recent_errors.append(
                    {
                        "id": bounded_text(raw.get("id"), 32),
                        "timestamp": bounded_timestamp(raw.get("timestamp")),
                        "feature": bounded_text(raw.get("feature"), 64),
                        "code": bounded_text(raw.get("code"), 64),
                        "operation": bounded_text(raw.get("operation"), 80),
                        "message": bounded_text(raw.get("message"), 240),
                        "user_id": bounded_text(raw.get("user_id"), 32),
                        "count": max(1, min(_safe_bigint(raw.get("count"), 1), 9999)),
                    }
                )
        result["errors"] = {
            "available": True,
            "count": min(max(0, _safe_bigint(raw_errors.get("count"), len(recent_errors))), 100000),
            "recent": recent_errors,
        }
    raw_operations = value.get("operations")
    if isinstance(raw_operations, dict):
        recent_operations: list[dict[str, Any]] = []
        rows = raw_operations.get("recent", [])
        if isinstance(rows, list):
            for raw in rows[:50]:
                if not isinstance(raw, dict):
                    continue
                recent_operations.append(
                    {
                        "operator_id": bounded_text(raw.get("operator_id"), 32),
                        "operator_name": bounded_text(raw.get("operator_name"), 80),
                        "action": bounded_text(raw.get("action"), 80),
                        "target_id": bounded_text(raw.get("target_id"), 32),
                        "target_name": bounded_text(raw.get("target_name"), 80),
                        "reason": bounded_text(raw.get("reason"), 200),
                        "timestamp": bounded_timestamp(raw.get("timestamp")),
                        "category": bounded_text(raw.get("category"), 32) or "其他",
                    }
                )
        recent_operations.sort(
            key=lambda item: int(item.get("timestamp", 0) or 0),
            reverse=True,
        )
        categories: dict[str, list[dict[str, Any]]] = {}
        for operation in recent_operations:
            category = str(operation.get("category", "其他") or "其他")[:32]
            categories.setdefault(category, []).append(operation)
        result["operations"] = {
            "available": True,
            "count": min(max(0, _safe_bigint(raw_operations.get("count"), len(recent_operations))), 100000),
            "recent": recent_operations,
            "categories": categories,
        }
    # Keep a small timestamp for the page's diagnostic line; it is not used
    # for authorization or expiry decisions.
    try:
        result["updated_at"] = max(0, int(value.get("updated_at", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        result["updated_at"] = 0
    return result


def get_snapshot(
    db: Session, group_id: str, *, for_update: bool = False
) -> GroupConfigSnapshot | None:
    """Load one group projection, optionally serializing concurrent writers.

    A bot exchange and a web save may arrive at almost exactly the same time.
    Mutating callers must use a current, locked read: otherwise an exchange
    that started with an old local projection can overwrite a just-saved web
    revision while leaving the newer revision number in place.
    """
    statement = select(GroupConfigSnapshot).where(
        GroupConfigSnapshot.group_id == str(group_id)
    )
    if for_update:
        # ``populate_existing`` is essential when this Session previously
        # read the same row (for example in the heartbeat loop).  A row lock
        # alone is not enough if SQLAlchemy then retains stale identity-map
        # values when deciding whether a web revision is pending.
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    return db.scalar(statement)


def store_runtime_members(
    db: Session,
    client_id: str,
    group_id: str,
    members: list[dict[str, Any]],
    *,
    levels_pending: bool = False,
    snapshot_complete: bool = True,
    expected_count: int = 0,
) -> int:
    """Store a refreshed QQ directory without waiting for a full exchange."""

    snapshot = get_snapshot(db, str(group_id), for_update=True)
    if snapshot is None or str(snapshot.client_id or "") != str(client_id or ""):
        return 0
    raw_runtime = getattr(snapshot, "runtime_json", None)
    if isinstance(raw_runtime, str):
        try:
            raw_runtime = json.loads(raw_runtime)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_runtime = {}
    runtime = dict(raw_runtime) if isinstance(raw_runtime, dict) else {}
    incoming_rows = [item for item in list(members or [])[:10000] if isinstance(item, dict)]
    if not incoming_rows:
        # Empty is not a valid refresh result. Keep an existing directory and
        # report its size so an older Windows client cannot erase the selector.
        existing_panel = runtime.get("members")
        existing_rows = (
            existing_panel.get("members", [])
            if isinstance(existing_panel, dict)
            else []
        )
        return len(existing_rows) if isinstance(existing_rows, list) else 0
    safe = _safe_runtime_summary(
        {
            "members": {
                "members": incoming_rows,
                "count": len(incoming_rows),
                "updated_at": int(time.time()),
                "levels_pending": bool(levels_pending),
            }
        }
    )
    panel = safe.get("members")
    if not isinstance(panel, dict):
        return 0
    existing_panel = runtime.get("members")
    existing_rows = (
        existing_panel.get("members", [])
        if isinstance(existing_panel, dict)
        else []
    )
    incoming_rows_safe = panel.get("members", [])

    # A previous Windows build may have left the portal's directory marked as
    # partial even though the current response is a complete small-group
    # snapshot. When the fresh row count reaches the reported count, trust the
    # new directory and remove departed members instead of carrying stale rows
    # forward from the old partial projection.
    incoming_count = len(incoming_rows_safe) if isinstance(incoming_rows_safe, list) else 0
    try:
        normalized_expected_count = max(0, min(10000, int(expected_count or 0)))
    except (TypeError, ValueError, OverflowError):
        normalized_expected_count = 0
    authoritative_snapshot = bool(snapshot_complete) or bool(
        normalized_expected_count > 0 and incoming_count >= normalized_expected_count
    )
    if authoritative_snapshot:
        panel["count"] = incoming_count
        panel["valid_count"] = incoming_count
        panel["partial"] = False
        if normalized_expected_count:
            # The group-info count can lag behind the member-list response.
            # Never expose that stale count as the expected directory size.
            panel["expected_count"] = incoming_count

    # Member directories are stored inside one bounded runtime JSON column.
    # Compare the normalized records before touching that column so a refresh
    # with identical data does not change updated_at, create a new row version,
    # or make every browser appear to have new data.
    def _member_key(row: Any) -> str:
        if not isinstance(row, dict):
            return ""
        return str(row.get("user_id", row.get("userId", ""))).strip()

    def _member_fingerprint(rows: Any) -> str:
        normalized = [row for row in (rows if isinstance(rows, list) else []) if isinstance(row, dict)]
        normalized.sort(key=_member_key)
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    # A partial response is still a snapshot of what Windows actually
    # received. Never merge old Linux rows into it: doing that makes departed
    # members look current and makes a real member-count mismatch impossible
    # to diagnose. Keep the partial marker so the UI can tell the operator
    # that another live refresh is required.
    if not authoritative_snapshot:
        live_rows = [
            dict(row)
            for row in (incoming_rows_safe if isinstance(incoming_rows_safe, list) else [])
            if isinstance(row, dict) and _member_key(row)
        ][:10000]
        panel["members"] = live_rows
        panel["count"] = len(live_rows)
        panel["valid_count"] = len(live_rows)
        panel["partial"] = True
        panel["expected_count"] = max(
            len(live_rows), normalized_expected_count
        )
        incoming_rows_safe = live_rows

    same_rows = _member_fingerprint(existing_rows) == _member_fingerprint(incoming_rows_safe)
    same_pending = bool(
        isinstance(existing_panel, dict)
        and existing_panel.get("levels_pending", False) == panel.get("levels_pending", False)
    )
    same_completeness = bool(
        isinstance(existing_panel, dict)
        and existing_panel.get("partial", False) == panel.get("partial", False)
        and _safe_bigint(existing_panel.get("expected_count", 0), 0)
        == _safe_bigint(panel.get("expected_count", 0), 0)
    )
    if same_rows and same_pending and same_completeness:
        # Preserve the old timestamp and ORM state. The caller may still commit
        # its transaction, but SQLAlchemy has no dirty runtime fields to write.
        return len(incoming_rows_safe) if isinstance(incoming_rows_safe, list) else 0
    runtime["members"] = panel
    snapshot.runtime_json = runtime
    snapshot.runtime_updated_at = utcnow()
    db.flush()
    return len(panel.get("members", [])) if isinstance(panel.get("members"), list) else 0


def _message_event_id(client_id: str, group_id: str, raw: dict[str, Any]) -> str:
    """Return a compact, stable ID for a retriable group-message upload."""

    dedupe_key = str(raw.get("dedupe_key", "") or "").strip()
    message_id = str(raw.get("message_id", "") or "").strip()
    identity = dedupe_key or message_id or "|".join(
        (
            str(raw.get("timestamp", "") or ""),
            str(raw.get("user_id", "") or ""),
            str(raw.get("content", "") or ""),
            str(raw.get("message_types", "") or ""),
        )
    )
    digest = hashlib.sha256(
        f"{str(client_id).strip()}|{str(group_id).strip()}|{identity}".encode(
            "utf-8", "ignore"
        )
    ).hexdigest()
    return f"gmsg-{digest[:64]}"


def _message_event_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep browser-visible message metadata without retaining media bytes."""

    payload: dict[str, Any] = {}
    for name in ("has_image", "recalled"):
        if name in raw:
            payload[name] = bool(raw.get(name))
    for name in (
        "image_count",
        "record_count",
        "video_count",
        "file_count",
        "forward_count",
        "recalled_at",
    ):
        if name in raw:
            payload[name] = max(0, _safe_bigint(raw.get(name), 0))
    for name, limit in (("recall_reason", 500), ("nickname", 128)):
        value = str(raw.get(name, "") or "").replace("\r", " ").replace("\n", " ").strip()
        if value:
            payload[name] = value[:limit]
    dedupe_key = bounded_text(raw.get("dedupe_key"), 128)
    if dedupe_key:
        payload["dedupe_key"] = dedupe_key
    source_account_id = bounded_text(raw.get("source_account_id"), 32)
    if source_account_id:
        payload["source_account_id"] = source_account_id
    mirror_key = bounded_text(raw.get("mirror_key"), 128)
    if mirror_key:
        payload["mirror_key"] = mirror_key
    direction = bounded_text(raw.get("direction"), 16).lower()
    if direction in {"inbound", "outbound", "system"}:
        payload["direction"] = direction
    raw_urls = raw.get("media_urls", [])
    if isinstance(raw_urls, list):
        urls: list[str] = []
        for raw_url in raw_urls[:8]:
            value = str(raw_url or "").strip()
            if value:
                urls.append(value[:1024])
        if urls:
            payload["media_urls"] = urls
    raw_media_items = raw.get("media_items", [])
    if isinstance(raw_media_items, list):
        media_items: list[dict[str, str]] = []
        for raw_item in raw_media_items[:8]:
            if not isinstance(raw_item, dict):
                continue
            url = bounded_text(raw_item.get("url"), 700000)
            if not (
                url.startswith("https://")
                or (url.startswith("data:image/") and ";base64," in url and len(url) <= 700000)
            ):
                continue
            kind = bounded_text(raw_item.get("kind"), 16).lower() or "file"
            if kind not in {"image", "audio", "video", "file"}:
                kind = "file"
            media_items.append(
                {
                    "kind": kind,
                    "url": url,
                    "name": bounded_text(raw_item.get("name"), 160),
                }
            )
        if media_items:
            payload["media_items"] = media_items
    raw_face_labels = raw.get("face_labels", [])
    if isinstance(raw_face_labels, list):
        face_labels = [
            bounded_text(label, 80)
            for label in raw_face_labels[:8]
            if isinstance(label, str) and bounded_text(label, 80)
        ]
        if face_labels:
            payload["face_labels"] = face_labels
    return payload


def _persist_group_message_events(
    db: Session,
    client_id: str,
    group_id: str,
    messages: list[dict[str, Any]],
) -> int:
    """Write each group message independently of the mutable runtime snapshot.

    The JSON runtime panel is useful for fast status rendering, but a later
    heartbeat can be delayed or compacted. This durable event stream is the
    source used by the per-member message viewer.
    """

    normalized_client = str(client_id or "").strip()[:64]
    normalized_group = str(group_id or "").strip()
    if not normalized_client or not GROUP_ID_RE.fullmatch(normalized_group):
        return 0
    candidates = [
        raw for raw in list(messages or [])[:100] if isinstance(raw, dict)
    ]
    if not candidates:
        return 0
    event_ids = [
        _message_event_id(normalized_client, normalized_group, raw)
        for raw in candidates
    ]
    existing_events = {
        str(event.event_id): event
        for event in db.scalars(
            select(PortalConsoleEvent).where(
                PortalConsoleEvent.event_id.in_(event_ids)
            )
        )
    }
    stored = 0
    for raw, event_id in zip(candidates, event_ids):
        message_id = str(raw.get("message_id", "") or "").strip()[:64]
        user_id = str(raw.get("user_id", "") or "").strip()[:32]
        user_name = str(
            raw.get("nickname") or raw.get("user_name", "") or ""
        ).replace("\r", " ").replace("\n", " ").strip()[:128]
        content = str(raw.get("content", "") or "").replace("\r", " ").replace("\n", " ").strip()[:2048]
        existing_event = existing_events.get(event_id)
        if existing_event is not None:
            # Replays commonly bring richer metadata than the first fast
            # runtime upload (for example a QQ image URL or face name). Keep
            # the event identity stable while refreshing its display payload.
            existing_event.user_name = user_name
            existing_event.message_type = str(raw.get("message_types", "") or "")[:128]
            existing_event.message_content = content
            direction = bounded_text(raw.get("direction"), 16).lower()
            if direction in {"inbound", "outbound", "system"}:
                existing_event.direction = direction
            payload = (
                dict(existing_event.message_payload)
                if isinstance(existing_event.message_payload, dict)
                else {}
            )
            payload.update(_message_event_payload(raw))
            existing_event.message_payload = payload
            continue
        db.add(
            PortalConsoleEvent(
                event_id=event_id,
                client_id=normalized_client,
                group_id=normalized_group,
                action="群消息缓存",
                user_id=user_id,
                user_name=user_name,
                message_id=message_id,
                message_type=str(raw.get("message_types", "") or "")[:128],
                detail="Windows 本地消息缓存同步",
                direction=(
                    bounded_text(raw.get("direction"), 16).lower()
                    if bounded_text(raw.get("direction"), 16).lower()
                    in {"inbound", "outbound", "system"}
                    else "inbound"
                ),
                message_content=content,
                message_payload=_message_event_payload(raw),
                occurred_at=_event_datetime(raw.get("timestamp")),
            )
        )
        stored += 1
    return stored


def _runtime_message_key(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    dedupe_key = str(row.get("dedupe_key", "") or "").strip()
    if dedupe_key:
        return f"dedupe:{dedupe_key}"
    message_id = str(row.get("message_id", "") or "").strip()
    if message_id:
        return f"id:{message_id}"
    return "fallback:{timestamp}|{user_id}|{content}|{types}".format(
        timestamp=row.get("timestamp", 0),
        user_id=row.get("user_id", ""),
        content=row.get("content", ""),
        types=row.get("message_types", ""),
    )


def _runtime_message_mirror_key(row: Any) -> tuple[str, str]:
    """Return content fingerprint and receiver account for cross-account dedupe."""

    if not isinstance(row, dict):
        return "", ""
    return (
        str(row.get("mirror_key", "") or "").strip(),
        str(row.get("source_account_id", "") or "").strip(),
    )


def _append_runtime_message(
    records: list[dict[str, Any]],
    seen_keys: set[str],
    mirror_sources: dict[str, set[str]],
    row: dict[str, Any],
) -> bool:
    key = _runtime_message_key(row)
    if not key or key in seen_keys:
        return False
    mirror_key, source_account_id = _runtime_message_mirror_key(row)
    if mirror_key and source_account_id:
        known_sources = mirror_sources.setdefault(mirror_key, set())
        if any(source != source_account_id for source in known_sources):
            return False
        known_sources.add(source_account_id)
    seen_keys.add(key)
    records.append(row)
    return True


def store_runtime_messages(
    db: Session,
    client_id: str,
    group_id: str,
    messages: list[dict[str, Any]],
    *,
    complete_user_id: str = "",
    complete_count: int = 0,
) -> int:
    """Merge newly received Windows message summaries into the runtime cache."""

    normalized_client_id = str(client_id or "").strip()[:64]
    normalized_group_id = str(group_id or "").strip()
    snapshot = get_snapshot(db, normalized_group_id, for_update=True)
    if snapshot is None or str(snapshot.client_id or "") != normalized_client_id:
        return 0
    raw_runtime = getattr(snapshot, "runtime_json", None)
    if isinstance(raw_runtime, str):
        try:
            raw_runtime = json.loads(raw_runtime)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_runtime = {}
    runtime = dict(raw_runtime) if isinstance(raw_runtime, dict) else {}
    existing_panel = runtime.get("message_cache")
    existing_rows = (
        existing_panel.get("records", [])
        if isinstance(existing_panel, dict)
        else []
    )
    incoming_candidates = [
        item for item in list(messages or [])[:100]
        if isinstance(item, dict)
    ]
    # A single Windows upload can contain copies received by multiple QQ
    # accounts.  Filter those before writing the durable event stream; doing
    # this only at render time would leave activity counts and audit rows
    # doubled even when the browser hides one copy.
    incoming_rows: list[dict[str, Any]] = []
    incoming_seen: set[str] = set()
    incoming_mirror_sources: dict[str, set[str]] = {}
    for item in incoming_candidates:
        _append_runtime_message(
            incoming_rows,
            incoming_seen,
            incoming_mirror_sources,
            item,
        )
    if not incoming_rows:
        normalized_complete_user = str(complete_user_id or "").strip()
        if re.fullmatch(r"\d{5,20}", normalized_complete_user):
            try:
                verified_count = max(0, min(5000, int(complete_count or 0)))
            except (TypeError, ValueError, OverflowError):
                verified_count = 0
            members_panel = runtime.get("members")
            members = (
                members_panel.get("members", [])
                if isinstance(members_panel, dict)
                else []
            )
            if isinstance(members, list):
                changed = False
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    if str(member.get("user_id", "")).strip() != normalized_complete_user:
                        continue
                    if _safe_bigint(member.get("activity_count"), 0) != verified_count:
                        member["activity_count"] = verified_count
                        changed = True
                    break
                if changed and isinstance(members_panel, dict):
                    members_panel["members"] = members
                    runtime["members"] = members_panel
                    snapshot.runtime_json = runtime
                    snapshot.runtime_updated_at = utcnow()
                    db.flush()
            return 1
        return len(existing_rows) if isinstance(existing_rows, list) else 0
    safe = _safe_runtime_summary(
        # Put the newly received batch first because the safety normalizer
        # caps the list before the final timestamp sort.  Otherwise a full
        # existing cache could discard the very messages this request adds.
        {"message_cache": {"records": incoming_rows + list(existing_rows)}}
    )
    panel = safe.get("message_cache")
    if not isinstance(panel, dict):
        return 0
    rows = panel.get("records", [])

    # Newest first, then de-duplicate retries from a timed-out HTTP request.
    rows = sorted(
        [row for row in rows if isinstance(row, dict)],
        key=lambda row: _safe_bigint(row.get("timestamp"), 0),
        reverse=True,
    )
    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    mirror_sources: dict[str, set[str]] = {}
    for row in rows:
        _append_runtime_message(deduplicated, seen, mirror_sources, row)
    panel["records"] = deduplicated
    panel["count"] = len(deduplicated)
    panel["available"] = True
    panel["source"] = "windows_incremental_sync"

    # Update the member directory from the same delta. A full member refresh
    # can take several seconds in a large group; message rows should still
    # make the selected member's last message and activity counters visible
    # immediately.
    members_panel = runtime.get("members")
    member_rows = (
        members_panel.get("members", [])
        if isinstance(members_panel, dict)
        else []
    )
    if isinstance(member_rows, list):
        existing_keys = {
            _runtime_message_key(row)
            for row in (existing_rows if isinstance(existing_rows, list) else [])
            if isinstance(row, dict)
        }
        applied_keys: set[str] = set()
        for message in incoming_rows:
            user_id = str(message.get("user_id", "")).strip()
            if not user_id:
                continue
            message_key = _runtime_message_key(message)
            is_new = bool(
                message_key
                and message_key not in existing_keys
                and message_key not in applied_keys
            )
            if message_key:
                applied_keys.add(message_key)
            timestamp = max(0, _safe_bigint(message.get("timestamp"), 0))
            message_types = str(message.get("message_types", "") or "")
            type_set = {
                item.strip().lower()
                for item in message_types.split(",")
                if item.strip()
            }
            for member in member_rows:
                if not isinstance(member, dict) or str(member.get("user_id", "")).strip() != user_id:
                    continue
                if is_new:
                    member["activity_count"] = max(
                        0, _safe_bigint(member.get("activity_count"), 0)
                    ) + 1
                    type_fields = {
                        "text": "text_count",
                        "image": "image_count",
                        "video": "video_count",
                        "file": "file_count",
                        "record": "record_count",
                        "audio": "record_count",
                        "forward": "forward_count",
                        "node": "forward_count",
                    }
                    counted: set[str] = set()
                    for kind in type_set:
                        field = type_fields.get(kind)
                        if field and field not in counted:
                            member[field] = max(
                                0, _safe_bigint(member.get(field), 0)
                            ) + 1
                            counted.add(field)
                    unknown = len(type_set - set(type_fields))
                    if unknown:
                        member["other_count"] = max(
                            0, _safe_bigint(member.get("other_count"), 0)
                        ) + unknown
                if timestamp >= max(0, _safe_bigint(member.get("last_message_at"), 0)):
                    member["last_message_at"] = timestamp
                    member["last_message_text"] = bounded_text(
                        message.get("content"), 160
                    )
                    member["last_message_types"] = bounded_text(message_types, 120)
                    member["inactive_seconds"] = (
                        max(0, int(time.time()) - timestamp) if timestamp else 0
                    )
                break
        members_panel["members"] = member_rows
        members_panel["count"] = len(member_rows)
        members_panel["valid_count"] = len(member_rows)
        runtime["members"] = members_panel
    runtime["message_cache"] = panel
    snapshot.runtime_json = runtime
    snapshot.runtime_updated_at = utcnow()
    # Persist the live runtime panel first.  Older Linux databases can be
    # missing a newly-added portal_console_events column until the service is
    # restarted and its additive migration has run.  That legacy event-table
    # issue must not discard the incoming Windows messages or leave the web
    # viewer stuck at 1/4: the snapshot safely retains the complete records,
    # and durable per-message events resume automatically after migration.
    db.flush()
    try:
        with db.begin_nested():
            _persist_group_message_events(
                db, normalized_client_id, normalized_group_id, incoming_rows
            )
            db.flush()
    except Exception as exc:
        logger.warning(
            "群消息明细事件暂未落库，已保留运行缓存 group=%s error_type=%s",
            normalized_group_id,
            type(exc).__name__,
        )
    # Member cards used to increment a cached counter indefinitely, so a
    # deleted/expired local Windows record could leave the UI saying “4 条”
    # while the detail view correctly had only 2.  Recalculate the affected
    # members from the durable Linux timeline after every successful delta.
    # This is bounded to the users in the current upload and is silent: no
    # browser refresh or extra QQ action is required.
    affected_users = {
        str(item.get("user_id", "")).strip()
        for item in incoming_rows
        if isinstance(item, dict)
        and str(item.get("direction", "inbound") or "inbound").lower() == "inbound"
        and re.fullmatch(r"\d{5,20}", str(item.get("user_id", "")).strip())
    }
    if affected_users:
        try:
            aggregate_rows = db.execute(
                select(
                    PortalConsoleEvent.user_id,
                    func.count(PortalConsoleEvent.id),
                    func.max(PortalConsoleEvent.occurred_at),
                )
                .where(
                    PortalConsoleEvent.group_id == normalized_group_id,
                    PortalConsoleEvent.client_id == normalized_client_id,
                    PortalConsoleEvent.action == "群消息缓存",
                    PortalConsoleEvent.user_id.in_(affected_users),
                )
                .group_by(PortalConsoleEvent.user_id)
            ).all()
            activity_by_user = {
                str(user_id): (max(0, int(count or 0)), latest)
                for user_id, count, latest in aggregate_rows
            }
            normalized_complete_user = str(complete_user_id or "").strip()
            if re.fullmatch(r"\d{5,20}", normalized_complete_user):
                try:
                    verified_count = max(0, min(5000, int(complete_count or 0)))
                except (TypeError, ValueError, OverflowError):
                    verified_count = 0
                previous = activity_by_user.get(normalized_complete_user, (0, None))
                activity_by_user[normalized_complete_user] = (verified_count, previous[1])
            members_panel = runtime.get("members")
            members = (
                members_panel.get("members", [])
                if isinstance(members_panel, dict)
                else []
            )
            if isinstance(members, list):
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    user_id = str(member.get("user_id", "")).strip()
                    if user_id not in activity_by_user:
                        continue
                    count, latest = activity_by_user[user_id]
                    member["activity_count"] = count
                    if isinstance(latest, datetime):
                        member["last_message_at"] = int(
                            latest.replace(tzinfo=timezone.utc).timestamp()
                        )
                        member["inactive_seconds"] = max(
                            0, int(time.time()) - int(member["last_message_at"])
                        )
                if isinstance(members_panel, dict):
                    members_panel["members"] = members
                    runtime["members"] = members_panel
                    snapshot.runtime_json = runtime
        except Exception:
            logger.debug(
                "成员消息统计静默回算失败 group=%s", normalized_group_id, exc_info=True
            )
    return len(deduplicated)


def list_group_message_history(
    db: Session,
    group_id: str,
    user_id: str | None = None,
    *,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Return retained messages for one member or an entire group.

    Durable event rows are append-only and are merged with the newest runtime
    snapshot.  This preserves old history while accepting just the newly
    uploaded Windows records on every timestamp-based sync.
    """

    normalized_group = str(group_id or "").strip()
    normalized_user = str(user_id or "").strip()
    if not GROUP_ID_RE.fullmatch(normalized_group):
        return []
    if normalized_user and not re.fullmatch(r"\d{5,20}", normalized_user):
        return []
    try:
        row_limit = max(1, min(10000, int(limit)))
    except (TypeError, ValueError, OverflowError):
        row_limit = 5000
    snapshot = get_snapshot(db, normalized_group)
    if snapshot is None:
        return []
    client_id = str(snapshot.client_id or "").strip()
    raw_runtime = getattr(snapshot, "runtime_json", None)
    if isinstance(raw_runtime, str):
        try:
            raw_runtime = json.loads(raw_runtime)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_runtime = {}
    runtime_rows: list[dict[str, Any]] = []
    if isinstance(raw_runtime, dict):
        safe = _safe_runtime_summary({"message_cache": raw_runtime.get("message_cache", {})})
        panel = safe.get("message_cache", {})
        rows = panel.get("records", []) if isinstance(panel, dict) else []
        runtime_rows = [
            dict(raw)
            for raw in rows
            if isinstance(raw, dict)
            and (not normalized_user or str(raw.get("user_id", "")).strip() == normalized_user)
        ]

    event_filters = [
        PortalConsoleEvent.group_id == normalized_group,
        PortalConsoleEvent.client_id == client_id,
        PortalConsoleEvent.action == "群消息缓存",
    ]
    if normalized_user:
        event_filters.append(PortalConsoleEvent.user_id == normalized_user)
    events = list(
        db.scalars(
            select(PortalConsoleEvent)
            .where(*event_filters)
            .order_by(desc(PortalConsoleEvent.occurred_at), desc(PortalConsoleEvent.id))
            .limit(row_limit)
        )
    )
    durable_rows: list[dict[str, Any]] = []
    for event in events:
        timestamp = 0
        occurred_at = getattr(event, "occurred_at", None)
        if isinstance(occurred_at, datetime):
            timestamp = int(occurred_at.replace(tzinfo=timezone.utc).timestamp())
        record = {
            "timestamp": timestamp,
            "user_id": str(event.user_id or ""),
            "user_name": str(event.user_name or ""),
            "message_id": str(event.message_id or ""),
            "message_types": str(event.message_type or ""),
            "content": str(event.message_content or ""),
            "direction": str(event.direction or "inbound"),
        }
        payload = getattr(event, "message_payload", None)
        if isinstance(payload, dict):
            record.update(payload)
        durable_rows.append(record)

    merged = durable_rows + runtime_rows
    merged.sort(
        key=lambda item: _safe_bigint(item.get("timestamp"), 0),
        reverse=True,
    )
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    mirror_sources: dict[str, set[str]] = {}
    for item in merged:
        _append_runtime_message(records, seen, mirror_sources, item)
        if len(records) >= row_limit:
            break
    return records


def request_group_message_history_replay(
    db: Session,
    group_id: str,
    user_id: str,
    *,
    available_count: int = 0,
) -> dict[str, int | bool]:
    """Ask the owning Windows client to replay local history for one member.

    The portal never reads Windows files directly.  If the member activity
    panel proves that Windows has more messages than the Linux detail view,
    retain a short durable replay request.  The request is carried through the
    normal authenticated exchange and is safe to repeat because QQ message IDs
    are deduplicated on the Linux side.
    """

    normalized_group = str(group_id or "").strip()
    normalized_user = str(user_id or "").strip()
    result: dict[str, int | bool] = {
        "expected_count": 0,
        "available_count": max(0, _safe_bigint(available_count, 0)),
        "requested": False,
    }
    if not GROUP_ID_RE.fullmatch(normalized_group) or not re.fullmatch(r"\d{5,20}", normalized_user):
        return result
    snapshot = get_snapshot(db, normalized_group, for_update=True)
    if snapshot is None or not str(snapshot.client_id or "").strip():
        return result
    raw_runtime = getattr(snapshot, "runtime_json", None)
    if isinstance(raw_runtime, str):
        try:
            raw_runtime = json.loads(raw_runtime)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_runtime = {}
    runtime = dict(raw_runtime) if isinstance(raw_runtime, dict) else {}
    members_panel = runtime.get("members")
    member_rows = members_panel.get("members", []) if isinstance(members_panel, dict) else []
    expected_count = 0
    if isinstance(member_rows, list):
        for member in member_rows:
            if not isinstance(member, dict) or str(member.get("user_id", "")).strip() != normalized_user:
                continue
            expected_count = max(0, _safe_bigint(member.get("activity_count"), 0))
            break
    result["expected_count"] = expected_count
    if expected_count <= int(result["available_count"]):
        # The browser has now observed a complete detail set.  Remove any
        # older request immediately so the next Windows heartbeat does not
        # replay the whole cache again during the request TTL.
        request_id = "msg-replay-" + hashlib.sha256(
            f"{snapshot.client_id}|{normalized_group}|{normalized_user}".encode("utf-8", "ignore")
        ).hexdigest()[:48]
        existing = db.scalar(
            select(PortalConsoleEvent)
            .where(PortalConsoleEvent.event_id == request_id)
            .with_for_update()
        )
        if existing is not None:
            db.delete(existing)
            db.flush()
        return result

    now = int(time.time())
    request_id = "msg-replay-" + hashlib.sha256(
        f"{snapshot.client_id}|{normalized_group}|{normalized_user}".encode("utf-8", "ignore")
    ).hexdigest()[:48]
    existing = db.scalar(
        select(PortalConsoleEvent)
        .where(PortalConsoleEvent.event_id == request_id)
        .with_for_update()
    )
    if existing is None:
        existing = PortalConsoleEvent(
            event_id=request_id,
            client_id=str(snapshot.client_id or "")[:64],
            group_id=normalized_group,
            action="消息明细回补请求",
            user_id=normalized_user,
            user_name="",
            message_id="",
            message_type="system",
            detail="网页消息明细缺口自动回补",
            direction="system",
            message_content="",
        )
        db.add(existing)
    existing.occurred_at = utcnow()
    existing.message_payload = {
        "expected_count": expected_count,
        "available_count": int(result["available_count"]),
        "expires_at": now + 120,
    }
    db.flush()
    result["requested"] = True
    return result


def pending_group_message_history_replays(
    db: Session,
    client_id: str,
    group_ids: object,
    *,
    limit: int = 32,
) -> list[dict[str, Any]]:
    """Return short-lived, member-specific replay requests for one client."""

    normalized_client = str(client_id or "").strip()[:64]
    allowed_groups = {
        str(value).strip()
        for value in (group_ids if isinstance(group_ids, (list, tuple, set, frozenset)) else [])
        if GROUP_ID_RE.fullmatch(str(value).strip())
    }
    if not normalized_client or not allowed_groups:
        return []
    max_rows = max(1, min(100, int(limit or 32)))
    now = int(time.time())
    requests: list[dict[str, Any]] = []
    rows = db.scalars(
        select(PortalConsoleEvent)
        .where(
            PortalConsoleEvent.client_id == normalized_client,
            PortalConsoleEvent.action == "消息明细回补请求",
            PortalConsoleEvent.group_id.in_(sorted(allowed_groups)),
        )
        .order_by(desc(PortalConsoleEvent.occurred_at), desc(PortalConsoleEvent.id))
        .limit(max_rows * 2)
    )
    for row in rows:
        payload = getattr(row, "message_payload", None)
        payload = payload if isinstance(payload, dict) else {}
        if _safe_bigint(payload.get("expires_at"), 0) < now:
            continue
        group_id = str(row.group_id or "").strip()
        user_id = str(row.user_id or "").strip()
        if not GROUP_ID_RE.fullmatch(group_id) or not re.fullmatch(r"\d{5,20}", user_id):
            continue
        requests.append(
            {
                "request_id": str(row.event_id or "")[:80],
                "group_id": group_id,
                "user_id": user_id,
                "expected_count": max(0, _safe_bigint(payload.get("expected_count"), 0)),
            }
        )
        if len(requests) >= max_rows:
            break
    return requests


def store_private_message_events(
    db: Session,
    client_id: str,
    user_id: str,
    messages: list[dict[str, Any]],
) -> int:
    """Persist private-chat summaries in the Linux event store.

    Private messages have no group snapshot to attach to.  They are therefore
    stored as scoped console events after confirming that the Windows client
    owns at least one group snapshot.  Raw media bytes stay on Windows; the
    normalized type/content metadata is enough for audit and diagnostics.
    """
    normalized_client = str(client_id or "").strip()[:64]
    normalized_user = str(user_id or "").strip()
    if not normalized_client or not re.fullmatch(r"\d{5,20}", normalized_user):
        return 0
    bound = db.scalar(
        select(GroupConfigSnapshot.id).where(
            GroupConfigSnapshot.client_id == normalized_client
        )
    )
    if bound is None:
        return 0
    stored = 0
    scope = f"private:{normalized_user}"
    for raw in list(messages or [])[:100]:
        if not isinstance(raw, dict):
            continue
        message_id = str(raw.get("message_id", "") or "").strip()[:64]
        timestamp = _safe_bigint(raw.get("timestamp"), 0)
        event_id = f"private-{normalized_client}-{normalized_user}-{message_id or timestamp}"[:80]
        if db.scalar(select(PortalConsoleEvent.id).where(PortalConsoleEvent.event_id == event_id)):
            stored += 1
            continue
        db.add(
            PortalConsoleEvent(
                event_id=event_id,
                client_id=normalized_client,
                group_id=scope,
                action="私聊消息",
                user_id=str(raw.get("user_id", normalized_user))[:32],
                user_name=str(raw.get("user_name", ""))[:128],
                message_id=message_id,
                message_type=str(raw.get("message_types", ""))[:128],
                detail="Windows 本地缓存静默同步",
                direction="inbound",
                message_content=str(raw.get("content", ""))[:2048],
                occurred_at=_event_datetime(timestamp),
            )
        )
        stored += 1
    return stored


def list_snapshots(db: Session, group_ids: object) -> dict[str, GroupConfigSnapshot]:
    """Load several group projections in one query for portal list pages."""

    normalized_ids = sorted(
        {
            str(group_id).strip()
            for group_id in (group_ids if isinstance(group_ids, (list, tuple, set, frozenset)) else [])
            if str(group_id).strip()
        }
    )
    if not normalized_ids:
        return {}
    rows = db.scalars(
        select(GroupConfigSnapshot).where(
            GroupConfigSnapshot.group_id.in_(normalized_ids)
        )
    )
    return {str(row.group_id): row for row in rows}


def get_config(db: Session, group_id: str) -> dict[str, Any]:
    snapshot = get_snapshot(db, group_id)
    if snapshot is None or not isinstance(snapshot.config_json, dict):
        return {"group_id": str(group_id), "features": {}}
    return deepcopy(snapshot.config_json)


def auth_state_from_snapshot(snapshot: GroupConfigSnapshot | None) -> dict[str, Any]:
    """Derive authorization without issuing another snapshot query."""

    if snapshot is None:
        return {
            "state": "missing",
            "active": False,
            "expire": 0,
            "expire_text": "尚未收到 Windows 首次同步",
        }
    expire = _safe_bigint(snapshot.auth_expire)
    state = _normalized_auth_state(snapshot.auth_state)
    if state == "unverified":
        # A readable local group config may be synchronized while Windows is
        # repairing a separate auth.json.  Preserve the last card fingerprint
        # for diagnostics, but do not let a historical authorization keep a
        # customer logged in or permit a write until the Windows auth source
        # has supplied a complete record again.
        return {
            "state": state,
            "active": False,
            "expire": expire,
            "expire_text": "等待 Windows 授权验证",
        }
    # A heartbeat records the Windows state, but expiry itself is a wall-clock
    # rule.  Downgrade a stale active snapshot locally so a customer cannot
    # keep using an expired group simply because the next heartbeat is late.
    if state == "active" and expire > 0 and expire <= int(
        utcnow().replace(tzinfo=timezone.utc).timestamp()
    ):
        state = "expired"
    return {
        "state": state,
        "active": state == "active",
        "expire": expire,
        "expire_text": snapshot.auth_expire_text or "无授权记录",
    }


def get_auth_state(db: Session, group_id: str) -> dict[str, Any]:
    return auth_state_from_snapshot(get_snapshot(db, group_id))


def missing_card_sync_message(db: Session, offline_after_seconds: int) -> str:
    """Explain a missing card claim without conflating it with an account login.

    The portal intentionally never receives raw cards, only HMAC fingerprints.
    A lookup miss therefore normally means one of three things: the card has
    not yet been activated in its target group, the local bot is offline, or
    the two sides use different Card Pepper values.
    """
    latest_seen = db.scalar(select(func.max(GroupConfigSnapshot.last_seen_at)))
    if latest_seen is None:
        return (
            "本地机器人尚未完成首次同步，暂时无法验证该群聊卡密；这不是卡密格式错误。"
            "请确认 Windows 插件已启用同步，网页地址、同步令牌和 Card Pepper 与服务器完全一致；"
            "再在目标群执行 /激活卡密，并等待本地机器人联网同步后重试。"
        )
    if latest_seen < utcnow() - timedelta(seconds=offline_after_seconds):
        return (
            "本地机器人当前离线，暂时无法确认该群聊卡密。"
            "请启动并联网后等待同步；卡密必须先在目标群激活。"
        )
    return (
        "本地机器人已在线，但尚未收到该卡密的授权记录。"
        "请确认使用的是目标群刚激活的当前卡密；若已激活仍持续出现，"
        "请核对网页与本地插件的 Card Pepper（卡密指纹盐）是否完全一致。"
        "卡密只用于群聊授权登记，不是网页账号密码。"
    )


def sync_state(
    snapshot: GroupConfigSnapshot | None, offline_after_seconds: int
) -> dict[str, Any]:
    # Early portal migrations could leave these counters NULL or as strings.
    # Normalize them before comparing so a legacy row cannot turn the group
    # configuration page into a 500 response.
    revision = _safe_bigint(getattr(snapshot, "revision", 0)) if snapshot is not None else 0
    applied_revision = (
        _safe_bigint(getattr(snapshot, "applied_revision", 0))
        if snapshot is not None
        else 0
    )
    # Keep the durable refresh hint visible even before the first heartbeat.
    # Previously this early branch always returned ``refresh_requested=False``
    # when ``last_seen_at`` was NULL, so a customer could click the request
    # button successfully but the page immediately rendered it as available
    # again and gave no indication that Windows still had work queued.
    refresh_requested = bool(getattr(snapshot, "refresh_requested", False)) if snapshot is not None else False
    if snapshot is None or snapshot.last_seen_at is None:
        pending = revision > applied_revision
        if refresh_requested:
            # A refresh can be queued before the first Windows projection has
            # ever arrived.  Keep that intent explicit instead of showing the
            # generic "尚未连通" message, which made a successful button
            # submission look as if it had been discarded.
            text = "已请求 Windows 补传本群首次配置"
            detail = (
                "补传请求已保存；Windows 下次成功连接网页后会先登记本群，"
                "再上传当前本地配置。"
            )
            badge = "等待首次补传"
        elif pending:
            text = "网页配置已保存，但服务器尚未收到 Windows 同步"
            detail = (
                f"网页版本 {revision} 已保留。请在 Windows 插件检查网页地址为 HTTPS 域名、"
                "同步令牌和 Card Pepper。"
            )
            badge = "等待首次连接"
        else:
            text = "服务器尚未收到此分群的 Windows 同步"
            detail = (
                "请在 Windows 插件检查网页地址为 HTTPS 域名、同步令牌和 Card Pepper。"
                "若 CMD 显示 http_status_525、TLS 或 timeout，问题在域名/CDN/反代通联，"
                "服务器尚未收到请求，和卡密或群关联无关。"
            )
            badge = "尚未连通"
        return {
            "online": False,
            "pending": pending,
            "status": "never",
            "connection_state": "never",
            "badge": badge,
            "text": text,
            "display_text": text,
            "detail": detail,
            "revision": revision,
            "applied_revision": applied_revision,
            "last_seen_at": None,
            "last_seen_text": _sync_last_seen_text(None),
            "saved_at": "",
            "saved_by": 0,
            "refresh_requested": refresh_requested,
        }
    last_seen_at = snapshot.last_seen_at
    if last_seen_at.tzinfo is not None:
        last_seen_at = last_seen_at.astimezone(timezone.utc).replace(tzinfo=None)
    online = last_seen_at >= utcnow() - timedelta(seconds=offline_after_seconds)
    pending = revision > applied_revision
    refresh_requested = bool(snapshot.refresh_requested)
    sync_status = str(getattr(snapshot, "sync_status", "synced") or "synced")
    sync_error = str(getattr(snapshot, "sync_error", "") or "")
    if sync_status == LOCAL_GROUP_MISSING_STATUS:
        # The latest authoritative Windows heartbeat explicitly reported
        # that this group is no longer present locally.  Keep this distinct
        # from a transport outage: the endpoint is reachable, but there is
        # nothing on Windows that can apply a web edit until the group/config
        # is restored or its authorization is activated again.
        text = "Windows 已报告本地不存在此分群配置或授权"
        display_text = text
        detail = (
            "Windows 已在线，但本地配置或授权中没有此群；网页不会继续伪装成等待连接。"
            + (
                "补传请求会保留到本地群配置或授权恢复后自动完成。"
                if refresh_requested
                else "恢复本地群配置/授权后，再点击“请求重新同步”。"
            )
        )
        badge = "Windows 未登记此群"
    elif sync_status == LOCAL_SOURCE_PARTIAL_STATUS:
        text = "Windows 已连接网页，群配置已同步但授权正在重新验证"
        display_text = text
        detail = (
            "Windows 的群配置文件可读，但授权文件暂时未完整读取；网页保留历史卡密仅作诊断，"
            "不会允许登录、保存或开关机，授权文件恢复后会自动更新。"
        )
        badge = "等待授权验证"
    elif sync_status == LOCAL_SOURCE_PROTECTED_STATUS:
        # The Windows endpoint reached the portal, but deliberately withheld
        # its group projection because one of its local source files could
        # not be read completely.  This is distinct from an offline bot and
        # from a pending web revision: the durable revision/request remains
        # untouched and will be retried after the local source recovers.
        text = "Windows 已连接网页，但本地配置或授权读取不完整"
        display_text = text
        detail = (
            "本轮仅保持通联，未上传部分配置、授权或运行数据，也未消费网页补传请求；"
            "请查看 Windows CMD 的“本地数据读取保护”提示，修复本地 JSON 后会自动恢复。"
        )
        badge = "Windows 数据读取保护"
    elif pending and sync_status == "error":
        # A retryable Windows-side write/ACK failure is more useful than the
        # generic pending text; retain ``pending=True`` so the UI still makes
        # clear that the portal revision has not been applied.
        text = sync_error or "local_apply_failed"
        display_text = _sync_failure_display(sync_error)
        detail = (
            f"网页版本 {revision} 已保留。Windows 下次成功连接后会自动重试，"
            "无需再次保存相同配置。"
        )
        badge = "本地应用失败"
    elif pending and online:
        text = "网页配置已保存，等待 Windows 拉取并应用"
        display_text = text
        detail = (
            f"网页版本 {revision} 尚未确认应用；Windows 在线时会在下一轮同步处理。"
        )
        badge = "等待应用"
    elif pending:
        text = "网页配置已保存，但 Windows 当前未连接"
        display_text = text
        detail = (
            f"网页版本 {revision} 已保留。请恢复 Windows 到网页域名的 HTTPS 连通后等待自动应用。"
        )
        badge = "等待连接"
    elif refresh_requested and online:
        text = "已请求 Windows 补传当前分群配置"
        display_text = text
        detail = "Windows 已在线，正在等待其上传当前本地配置。"
        badge = "等待补传"
    elif refresh_requested:
        text = "已请求 Windows 补传，但 Windows 当前未连接"
        display_text = text
        detail = "补传请求已保留；Windows 成功连接网页后会自动上传。"
        badge = "等待连接"
    elif not online:
        text = "Windows 最近同步已过期，正在显示上次数据"
        display_text = text
        detail = (
            "请检查 Windows 电脑是否运行插件并可访问网页 HTTPS 地址。"
            "若 CMD 显示 525、TLS 或 timeout，请先修复域名/CDN/反代通联。"
        )
        badge = "Windows 离线"
    elif sync_status == "error":
        text = sync_error or "local_apply_failed"
        display_text = _sync_failure_display(sync_error)
        detail = "Windows 已连接网页，但上次本地应用未完成；后续同步会自动重试。"
        badge = "需要重试"
    else:
        text = "已与本地机器人同步"
        display_text = text
        detail = "网页配置已同步到已绑定的 Windows 客户端。"
        badge = "已同步"
    return {
        "online": online,
        "pending": pending,
        "status": sync_status,
        "connection_state": "online" if online else "offline",
        "badge": badge,
        "text": text,
        "display_text": display_text,
        "detail": detail,
        "revision": revision,
        "applied_revision": applied_revision,
        "last_seen_at": last_seen_at,
        "last_seen_text": _sync_last_seen_text(last_seen_at),
        "saved_at": _sync_saved_at_text(snapshot),
        "saved_by": _safe_bigint(getattr(snapshot, "updated_by", 0)),
        "refresh_requested": refresh_requested,
    }


def save_web_config(
    db: Session, group_id: str, config: dict[str, Any], updated_by: int | None
) -> GroupConfigSnapshot:
    """Persist one complete web projection under the group row lock.

    Callers that already have a complete projection can continue using this
    compatibility API.  Incremental web operations should prefer
    :func:`mutate_web_config`, which reads and modifies the current row while
    holding the same lock; otherwise two browser requests that started from
    the same page can overwrite each other's unrelated changes.
    """
    # Serialize this save with the Windows exchange/ACK path.  See
    # ``get_snapshot`` for why a normal ORM read is insufficient here.
    snapshot = get_snapshot(db, group_id, for_update=True)
    return _save_web_config_locked(db, group_id, snapshot, config, updated_by)


def _save_web_config_locked(
    db: Session,
    group_id: str,
    snapshot: GroupConfigSnapshot | None,
    config: dict[str, Any],
    updated_by: int | None,
) -> GroupConfigSnapshot:
    """Save a projection when ``snapshot`` is already locked by this session."""
    projected = _secure_projection(config, snapshot.config_json if snapshot is not None else None)
    projected["group_id"] = str(group_id)
    if snapshot is None:
        snapshot = GroupConfigSnapshot(
            group_id=str(group_id),
            revision=1,
            applied_revision=0,
            config_json=projected,
            updated_by=updated_by,
            sync_status="pending",
        )
        db.add(snapshot)
    else:
        snapshot.revision = _safe_bigint(snapshot.revision)
        snapshot.applied_revision = _safe_bigint(snapshot.applied_revision)
        # A browser or reverse proxy can lose the redirect after this method
        # has committed.  Retrying the same form must not create another
        # revision (and another Windows sync) for an unchanged projection.
        # Pending/error revisions remain intact so the Windows client can
        # retry the exact work it already received.
        if snapshot.config_json == projected:
            return snapshot
        snapshot.revision += 1
        snapshot.config_json = projected
        snapshot.updated_by = updated_by
        snapshot.updated_at = utcnow()
        snapshot.sync_status = "pending"
        snapshot.sync_error = ""
    return snapshot


def mutate_web_config(
    db: Session,
    group_id: str,
    updated_by: int | None,
    mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> dict[str, Any]:
    """Atomically read, modify and persist one group's web projection.

    The old service pattern was ``get_config()`` followed later by
    ``save_web_config()``.  Those calls each used a separate read boundary,
    leaving a lost-update window when two users edited the same group at the
    same time.  This helper acquires the row lock *before* invoking ``mutator``
    and therefore serializes all edits against both other web requests and the
    Windows exchange/ACK transaction.

    ``mutator`` may mutate the supplied dictionary in place and return ``None``
    or return a replacement dictionary.  A deep copy is passed in and another
    deep copy is returned so callers cannot mutate SQLAlchemy's identity-mapped
    JSON value after this function returns.

    A tiny compatibility fallback is retained for lightweight legacy tests and
    integrations that pass a non-SQLAlchemy fake ``db`` and monkey-patch the
    old ``get_config``/``save_web_config`` pair.  Real portal requests always
    use a ``Session`` and the locked path above.
    """
    if not isinstance(db, Session):
        current = get_config(db, group_id)
        # Legacy lightweight adapters often return their canonical in-memory
        # mapping from ``get_config`` and expect the compatibility save hook to
        # observe the same object.  Keep that behaviour here; real portal
        # requests always take the locked SQLAlchemy path below.
        working = current if isinstance(current, dict) else {}
        before = deepcopy(working)
        updated = mutator(working)
        if updated is None:
            updated = working
        if not isinstance(updated, dict):
            raise TypeError("web config mutator must return a mapping or None")
        if updated != before:
            save_web_config(db, group_id, updated, updated_by)
        return deepcopy(updated)

    snapshot = get_snapshot(db, group_id, for_update=True)
    if snapshot is None or not isinstance(snapshot.config_json, dict):
        current = {"group_id": str(group_id), "features": {}}
    else:
        current = deepcopy(snapshot.config_json)
    updated = mutator(current)
    if updated is None:
        updated = current
    if not isinstance(updated, dict):
        raise TypeError("web config mutator must return a mapping or None")
    saved = _save_web_config_locked(db, group_id, snapshot, updated, updated_by)
    return deepcopy(saved.config_json if isinstance(saved.config_json, dict) else updated)


def known_group_ids(db: Session) -> set[str]:
    return {str(value) for value in db.scalars(select(GroupConfigSnapshot.group_id))}


def _group_client_sync(
    db: Session, group_id: str, client_id: str, *, for_update: bool = False
) -> GroupClientSync | None:
    statement = select(GroupClientSync).where(
        GroupClientSync.group_id == str(group_id),
        GroupClientSync.client_id == str(client_id),
    )
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def _update_group_sync_summary(db: Session, snapshot: GroupConfigSnapshot) -> None:
    states = db.scalars(
        select(GroupClientSync).where(
            GroupClientSync.group_id == str(snapshot.group_id)
        )
    ).all()
    if not states:
        return
    applied_revision = min(_safe_bigint(item.applied_revision) for item in states)
    snapshot.applied_revision = applied_revision
    if applied_revision >= _safe_bigint(snapshot.revision):
        snapshot.sync_status = "synced"
        snapshot.sync_error = ""
    else:
        failed = next((item.sync_error for item in states if item.sync_error), "")
        snapshot.sync_status = "error" if failed else "pending"
        snapshot.sync_error = failed[:255]


def bound_group_ids(
    db: Session,
    client_id: object,
    known_ids: list[object] | set[object] | None = None,
    local_group_ids: list[object] | set[object] | None = None,
) -> set[str]:
    """Return groups that have a web-account binding for this Windows client.

    A local AstrBot installation can contain many groups which have never been
    claimed by a portal account.  Those groups must not receive the regular
    configuration/runtime/message projection. ``GroupAccess`` is the durable
    web-account binding. The primary snapshot owner and clients that report
    the same locally joined group receive its canonical web projection. The
    group account binding remains the tenant boundary.

    The function deliberately does not inspect ``expires_at``.  An expired or
    revoked card still needs one last Windows exchange so the local side can
    receive the current authorization state and stop allowing the group.
    """

    normalized_client_id = str(client_id or "").strip()[:64]
    if not normalized_client_id:
        return set()
    reported_ids = {
        str(value).strip()
        for value in [*(known_ids or []), *(local_group_ids or [])]
        if GROUP_ID_RE.fullmatch(str(value).strip())
    }
    db.flush()
    criteria = [
        GroupConfigSnapshot.client_id == normalized_client_id,
        select(GroupClientSync.id)
        .where(
            GroupClientSync.group_id == GroupAccess.group_id,
            GroupClientSync.client_id == normalized_client_id,
        )
        .exists(),
    ]
    if reported_ids:
        criteria.extend(
            [
                GroupConfigSnapshot.group_id.is_(None)
                & GroupAccess.group_id.in_(reported_ids),
                (GroupConfigSnapshot.client_id == "")
                & GroupAccess.group_id.in_(reported_ids),
            ]
        )
    rows = db.execute(
        select(GroupAccess.group_id)
        .outerjoin(
            GroupConfigSnapshot,
            GroupConfigSnapshot.group_id == GroupAccess.group_id,
        )
        .where(
            or_(*criteria)
        )
    ).all()
    return {
        str(row[0]).strip()
        for row in rows
        if GROUP_ID_RE.fullmatch(str(row[0]).strip())
    }


def claimable_group_ids(db: Session, client_id: object) -> set[str]:
    """Return unbound groups with a currently claimable card.

    This is a small compatibility/bootstrap hint for the Windows client.  It
    lets a filtered client re-send only card metadata needed by the portal's
    first ``卡密登记`` flow instead of continuously uploading every local
    group.  The card fingerprint itself is never returned by this helper.
    """

    normalized_client_id = str(client_id or "").strip()[:64]
    if not normalized_client_id:
        return set()
    db.flush()
    rows = db.execute(
        select(GroupConfigSnapshot.group_id)
        .outerjoin(
            GroupAccess,
            GroupAccess.group_id == GroupConfigSnapshot.group_id,
        )
        .where(
            GroupConfigSnapshot.client_id == normalized_client_id,
            GroupAccess.group_id.is_(None),
            GroupConfigSnapshot.auth_state == "active",
            GroupConfigSnapshot.auth_card_fingerprint != "",
        )
    ).all()
    return {
        str(row[0]).strip()
        for row in rows
        if GROUP_ID_RE.fullmatch(str(row[0]).strip())
    }


def touch_client_heartbeat(
    db: Session, client_id: object, *, mark_auth_unverified: bool = False
) -> int:
    """Refresh liveness for snapshots owned by a Windows client.

    A protected heartbeat intentionally carries no group inventory because a
    local JSON/auth read was incomplete.  It is still a successful transport
    exchange, so existing snapshots must not age into the misleading
    ``Windows 离线`` state while the local source repairs itself.  This helper
    changes only liveness and a safe source-protection status; configuration,
    authorization, revisions and durable refresh hints remain untouched.
    """

    normalized_client_id = str(client_id or "").strip()[:64]
    if not normalized_client_id:
        return 0
    now = utcnow()
    count = 0
    snapshots = db.scalars(
        select(GroupConfigSnapshot)
        .where(GroupConfigSnapshot.client_id == normalized_client_id)
        .with_for_update()
    )
    for snapshot in snapshots:
        # Do not hide an explicit authoritative "this group is absent"
        # tombstone behind the temporary local-source diagnostic.
        if str(getattr(snapshot, "sync_status", "") or "") != LOCAL_GROUP_MISSING_STATUS:
            snapshot.sync_status = LOCAL_SOURCE_PROTECTED_STATUS
            snapshot.sync_error = LOCAL_SOURCE_PROTECTED_ERROR
            if mark_auth_unverified and snapshot.auth_state != "missing":
                snapshot.auth_state = "unverified"
                snapshot.auth_expire_text = "等待 Windows 授权验证"
        snapshot.last_seen_at = now
        count += 1
    return count


def missing_bot_group_ids(db: Session, group_ids: list[object] | None) -> list[str]:
    """Return valid local group IDs that have no server snapshot yet.

    The Windows process only sends a full group projection when it changes.
    If the portal database is restored or its snapshots are removed while that
    process remains running, its in-memory cache otherwise makes the portal
    look permanently unsynchronized.  This small reconciliation hint lets the
    client invalidate only those cache entries and upload them again.
    """
    normalized_group_ids = {
        str(group_id).strip()
        for group_id in (group_ids or [])
        if GROUP_ID_RE.fullmatch(str(group_id).strip())
    }
    if not normalized_group_ids:
        return []
    existing_group_ids = {
        str(group_id)
        for group_id in db.scalars(
            select(GroupConfigSnapshot.group_id).where(
                GroupConfigSnapshot.group_id.in_(normalized_group_ids)
            )
        )
    }
    # Reconcile one group per heartbeat.  The Windows client immediately
    # schedules the next coalesced poll after consuming this hint, so a
    # restored portal still converges without sending a burst of full
    # projections in one request.
    return sorted(normalized_group_ids - existing_group_ids)[
        :MAX_RECONCILIATION_HINTS_PER_EXCHANGE
    ]


def request_group_refresh(db: Session, group_ids: list[object] | set[object]) -> int:
    """Ask the next Windows heartbeat to upload fresh snapshots for groups.

    The web server deliberately cannot call into a Windows AstrBot process.
    Persisting the request makes a customer login useful even when the bot is
    temporarily offline: the next authenticated outbound exchange returns the
    group IDs, and the client performs one immediate full re-upload.
    """

    normalized_group_ids = {
        str(group_id).strip()
        for group_id in group_ids
        if GROUP_ID_RE.fullmatch(str(group_id).strip())
    }
    if not normalized_group_ids:
        return 0
    snapshots = {
        str(snapshot.group_id): snapshot
        for snapshot in db.scalars(
            select(GroupConfigSnapshot).where(
                GroupConfigSnapshot.group_id.in_(normalized_group_ids)
            )
        )
    }
    changed = 0
    for group_id in sorted(normalized_group_ids):
        snapshot = snapshots.get(group_id)
        if snapshot is None:
            # A customer may already own a group access row while the first
            # Windows heartbeat has not created its projection yet.  Keep a
            # tiny, non-authoritative placeholder so the refresh request is
            # durable and can be picked up by the next outbound exchange.
            # revision/applied_revision stay at zero: this row is not a web
            # edit, does not grant authorization, and is replaced by the
            # complete Windows payload on the first heartbeat.
            snapshot = GroupConfigSnapshot(
                group_id=group_id,
                revision=0,
                applied_revision=0,
                config_json={"group_id": group_id, "features": {}},
                sync_status="synced",
                refresh_requested=True,
            )
            db.add(snapshot)
            changed += 1
            continue
        if not snapshot.refresh_requested:
            snapshot.refresh_requested = True
            changed += 1
    return changed


def request_group_config_pull(
    db: Session, group_id: object, client_id: object
) -> dict[str, Any]:
    """Queue the current web revision for one explicitly requesting client."""

    normalized_group_id = str(group_id or "").strip()
    normalized_client_id = str(client_id or "").strip()[:64]
    if not GROUP_ID_RE.fullmatch(normalized_group_id) or not normalized_client_id:
        return {"status": "error", "code": "invalid_pull_target"}
    if db.scalar(
        select(GroupAccess.id).where(GroupAccess.group_id == normalized_group_id)
    ) is None:
        return {"status": "error", "code": "group_not_bound"}
    snapshot = get_snapshot(db, normalized_group_id, for_update=True)
    if (
        snapshot is None
        or _safe_bigint(snapshot.revision) <= 0
        or not isinstance(snapshot.config_json, dict)
    ):
        return {"status": "error", "code": "web_config_missing"}

    revision = _safe_bigint(snapshot.revision)
    state = _group_client_sync(
        db, normalized_group_id, normalized_client_id, for_update=True
    )
    already_pending = bool(
        state is not None
        and state.pull_requested
        and _safe_bigint(state.applied_revision) < revision
    )
    if state is None:
        state = GroupClientSync(
            group_id=normalized_group_id,
            client_id=normalized_client_id,
            applied_revision=max(0, revision - 1),
        )
        db.add(state)
    elif not already_pending and _safe_bigint(state.applied_revision) >= revision:
        state.applied_revision = max(0, revision - 1)
    state.last_seen_at = utcnow()
    state.sync_error = ""
    state.pull_requested = True
    db.flush()
    _update_group_sync_summary(db, snapshot)
    return {
        "status": "ok",
        "group_id": normalized_group_id,
        "revision": revision,
        "already_pending": already_pending,
    }


def requested_group_refresh_ids(
    db: Session, client_id: str, known_group_ids: list[object] | None
) -> list[str]:
    """Return refresh requests that belong to the polling Windows client.

    A current client normally receives only IDs it explicitly reported in
    ``known_group_ids``.  A group may, however, have been removed locally
    after an earlier successful upload.  Its retained tombstone belongs to
    the same client and must still receive a one-group refresh hint so the
    Windows CMD can explain that the local group/authorization is absent
    rather than leaving the portal in an indefinite waiting state.
    """

    normalized_group_ids = {
        str(group_id).strip()
        for group_id in (known_group_ids or [])
        if GROUP_ID_RE.fullmatch(str(group_id).strip())
    }
    normalized_client_id = str(client_id or "").strip()[:64]
    if not normalized_client_id:
        return []
    # Portal sessions intentionally disable SQLAlchemy autoflush.  Flush the
    # exchange's just-applied ownership/tombstone changes before a locking read
    # with ``populate_existing``; otherwise the query can reload an old
    # ``refresh_requested`` value from MySQL and make a completed pull appear
    # stuck forever.
    db.flush()
    # Lock the rows while selecting/consuming hints.  Without the lock, two
    # overlapping heartbeat workers can both receive the same one-group hint;
    # more importantly, a permanently missing local group could keep winning
    # the single hint slot and starve a live group's refresh request.
    unassigned_visible = (
        and_(
            GroupConfigSnapshot.client_id == "",
            GroupConfigSnapshot.group_id.in_(normalized_group_ids),
        )
        if normalized_group_ids
        else false()
    )
    snapshots = list(
        db.scalars(
            select(GroupConfigSnapshot)
            .where(
                GroupConfigSnapshot.refresh_requested.is_(True),
                or_(
                    GroupConfigSnapshot.client_id == normalized_client_id,
                    unassigned_visible,
                ),
            )
            .with_for_update()
        )
    )
    # A snapshot with no client ID is a first-upload placeholder. It is safe
    # to hand it only to a client that actually reported that group. A row
    # already owned by this client is also deliverable when it has just been
    # removed from ``known_group_ids``; see the tombstone explanation above.
    eligible = [
        snapshot
        for snapshot in snapshots
        if snapshot.client_id == normalized_client_id
        or (
            not snapshot.client_id
            and str(snapshot.group_id) in normalized_group_ids
        )
    ]
    # A tombstone is useful for one CMD diagnostic, but it cannot be uploaded
    # until the operator restores the group locally.  Always deliver live
    # groups first so one stale/deleted group cannot block every other refresh.
    live = [
        snapshot
        for snapshot in eligible
        if str(getattr(snapshot, "sync_status", "") or "")
        != LOCAL_GROUP_MISSING_STATUS
    ]
    unavailable = [
        snapshot
        for snapshot in eligible
        if str(getattr(snapshot, "sync_status", "") or "")
        == LOCAL_GROUP_MISSING_STATUS
    ]
    live.sort(key=lambda snapshot: str(snapshot.group_id))
    unavailable.sort(key=lambda snapshot: str(snapshot.group_id))
    selected = (live + unavailable)[:MAX_RECONCILIATION_HINTS_PER_EXCHANGE]
    selected_ids = [str(snapshot.group_id) for snapshot in selected]
    # The Windows client can only report that a missing group is absent; it
    # cannot upload anything for it.  Consume that diagnostic hint after one
    # delivery.  If the group is restored later, the user can request a fresh
    # sync again and a new durable hint will be created.
    for snapshot in selected:
        if str(getattr(snapshot, "sync_status", "") or "") == LOCAL_GROUP_MISSING_STATUS:
            snapshot.refresh_requested = False
    return selected_ids


def apply_bot_exchange(
    db: Session,
    client_id: str,
    groups: list[dict[str, Any]],
    known_group_ids: list[str] | None = None,
    *,
    authoritative_group_ids: bool = False,
    runtime_summaries: dict[str, Any] | None = None,
    config_source_complete: bool = True,
    auth_source_complete: bool = True,
    sync_scope_complete: bool = False,
    game_auth_source_complete: bool = True,
    local_group_ids: list[object] | None = None,
) -> list[dict[str, Any]]:
    """Ingest local state and return pending full projections for this client.

    Configuration and authorization have separate health bits because they
    are stored in different Windows files.  A config-only projection is safe
    to ingest when auth.json is unavailable, but it must retain/temporarily
    invalidate the portal's card state rather than writing a fabricated empty
    authorization. For groups bound to a portal account, the Linux web
    projection is canonical and local Windows config uploads never replace it.
    Only a complete pair of sources may reconcile deletion.
    """

    def change_metadata(snapshot: GroupConfigSnapshot) -> dict[str, Any]:
        """Expose bounded web-save metadata to the Windows CMD diagnostic.

        The configuration values stay in ``config`` and are never duplicated in
        metadata.  ISO timestamps and the numeric operator ID let the Windows
        side show which web revision it pulled and when it was saved, without
        requiring a second request or leaking account credentials.
        """

        updated_at = getattr(snapshot, "updated_at", None)
        if isinstance(updated_at, datetime):
            updated_at_text = updated_at.isoformat(timespec="seconds")
        else:
            updated_at_text = ""
        updated_by = getattr(snapshot, "updated_by", None)
        try:
            updated_by_value = int(updated_by) if updated_by is not None else 0
        except (TypeError, ValueError, OverflowError):
            updated_by_value = 0
        return {
            "updated_at": updated_at_text,
            "updated_by": updated_by_value,
            "source": "web",
        }

    config_source_complete = bool(config_source_complete)
    auth_source_complete = bool(auth_source_complete)
    # Game/WebSocket authorization has its own source and retention policy.
    # Cleanup is deliberately best-effort within the exchange transaction;
    # a bad game-auth file must never block normal QQ configuration sync.
    cleanup_game_authorizations(db)
    # Never honour a caller's authoritative deletion claim unless both source
    # health bits explicitly support it.  This is defensive even though the
    # HTTP layer already derives the same rule.
    authoritative_group_ids = bool(
        authoritative_group_ids
        and not sync_scope_complete
        and config_source_complete
        and auth_source_complete
    )
    normalized_known_ids = {
        str(group_id)
        for group_id in (known_group_ids or [])
        if GROUP_ID_RE.fullmatch(str(group_id))
    }
    normalized_local_group_ids = {
        str(group_id).strip()
        for group_id in (local_group_ids or [])
        if config_source_complete
        and GROUP_ID_RE.fullmatch(str(group_id).strip())
    }
    portal_group_ids = {
        str(group_id).strip()
        for group_id in db.scalars(select(GroupAccess.group_id))
        if GROUP_ID_RE.fullmatch(str(group_id).strip())
    }
    normalized_client_id = str(client_id or "").strip()[:64]
    now = utcnow()
    known_portal_group_ids = normalized_known_ids.intersection(portal_group_ids)
    discovered_portal_group_ids = normalized_local_group_ids.intersection(
        portal_group_ids
    )
    client_portal_group_ids = set(discovered_portal_group_ids)
    if normalized_client_id and known_portal_group_ids:
        for group_id in known_portal_group_ids:
            snapshot = get_snapshot(db, group_id)
            if snapshot is not None and snapshot.client_id == normalized_client_id:
                client_portal_group_ids.add(group_id)
    if normalized_client_id and client_portal_group_ids:
        snapshots = {
            str(snapshot.group_id): snapshot
            for snapshot in db.scalars(
                select(GroupConfigSnapshot)
                .where(GroupConfigSnapshot.group_id.in_(client_portal_group_ids))
                .with_for_update()
            )
        }
        states = {
            (str(state.group_id), str(state.client_id)): state
            for state in db.scalars(
                select(GroupClientSync)
                .where(GroupClientSync.group_id.in_(client_portal_group_ids))
                .with_for_update()
            )
        }
        for group_id in client_portal_group_ids:
            snapshot = snapshots.get(group_id)
            if snapshot is None:
                snapshot = GroupConfigSnapshot(
                    group_id=group_id,
                    revision=1,
                    applied_revision=0,
                    config_json={"group_id": group_id, "features": {}},
                    sync_status="pending",
                    client_id=normalized_client_id,
                    last_seen_at=now,
                )
                db.add(snapshot)
                snapshots[group_id] = snapshot
            elif not snapshot.client_id:
                snapshot.client_id = normalized_client_id
                snapshot.applied_revision = 0
                snapshot.sync_status = "pending"
            owner_id = str(snapshot.client_id or "").strip()
            if owner_id and (group_id, owner_id) not in states:
                owner_state = GroupClientSync(
                    group_id=group_id,
                    client_id=owner_id,
                    applied_revision=_safe_bigint(snapshot.applied_revision),
                )
                db.add(owner_state)
                states[(group_id, owner_id)] = owner_state
            state = states.get((group_id, normalized_client_id))
            if state is None:
                is_owner = snapshot.client_id == normalized_client_id
                state = GroupClientSync(
                    group_id=group_id,
                    client_id=normalized_client_id,
                    applied_revision=(
                        _safe_bigint(snapshot.applied_revision)
                        if is_owner
                        else _safe_bigint(snapshot.revision)
                    ),
                )
                db.add(state)
                states[(group_id, normalized_client_id)] = state
            state.last_seen_at = now
            snapshot.last_seen_at = now
        db.flush()
        for group_id in discovered_portal_group_ids:
            snapshot = snapshots.get(group_id)
            if snapshot is not None:
                _update_group_sync_summary(db, snapshot)
    # In an authoritative exchange, ``known_group_ids`` is the complete local
    # set.  Never let a stale/malformed payload re-create a group that the same
    # heartbeat says is absent; otherwise a transient empty list could both
    # tombstone the old row and immediately revive it with old data.
    if authoritative_group_ids:
        groups = [
            item
            for item in groups
            if isinstance(item, dict)
            and str(item.get("group_id", "")).strip() in normalized_known_ids
        ]

    changes: list[dict[str, Any]] = []
    if config_source_complete and not auth_source_complete and client_id:
        # A valid-but-empty initialized config directory can accompany a
        # temporarily unreadable auth source.  It contains no per-group item
        # through which to mark historic cards unverified, so conservatively
        # mark this Windows client's existing rows without touching their
        # configurations, revisions, card fingerprints, or refresh requests.
        for snapshot in db.scalars(
            select(GroupConfigSnapshot)
            .where(GroupConfigSnapshot.client_id == client_id[:64])
            .with_for_update()
            .execution_options(populate_existing=True)
        ):
            snapshot.last_seen_at = now
            if snapshot.sync_status != LOCAL_GROUP_MISSING_STATUS:
                if snapshot.auth_state != "missing":
                    snapshot.auth_state = "unverified"
                    snapshot.auth_expire_text = "等待 Windows 授权验证"
                snapshot.sync_status = LOCAL_SOURCE_PARTIAL_STATUS
                snapshot.sync_error = LOCAL_SOURCE_PARTIAL_ERROR
    # A current local client explicitly marks its complete group list as
    # authoritative. If both the authorization record and group config were
    # deleted locally, remove the stale *configuration* from the server while
    # retaining a minimal tombstone.  Deleting the row outright made a later
    # portal refresh impossible to route back to the Windows client and left
    # users seeing only "waiting for connection".  The tombstone contains no
    # former rules, runtime data, authorization, or card fingerprint; it only
    # keeps the stable client association and an actionable state.
    if authoritative_group_ids and client_id:
        owned_snapshots = db.scalars(
            select(GroupConfigSnapshot).where(
                GroupConfigSnapshot.client_id == client_id[:64]
            ).order_by(GroupConfigSnapshot.group_id).with_for_update().execution_options(
                populate_existing=True
            )
        )
        for snapshot in list(owned_snapshots):
            if (
                snapshot.group_id not in normalized_known_ids
                and snapshot.group_id not in portal_group_ids
            ):
                snapshot.revision = 0
                snapshot.applied_revision = 0
                snapshot.config_json = {
                    "group_id": str(snapshot.group_id),
                    "features": {},
                }
                snapshot.sync_status = LOCAL_GROUP_MISSING_STATUS
                snapshot.sync_error = LOCAL_GROUP_MISSING_ERROR
                snapshot.last_seen_at = now
                # Preserve a browser's existing refresh request.  It is
                # intentionally returned to the same client below even
                # though this group is absent from ``known_group_ids`` so
                # Windows can display a clear per-group CMD diagnostic.
                snapshot.refresh_requested = bool(snapshot.refresh_requested)
                snapshot.auth_state = "missing"
                snapshot.auth_expire = 0
                snapshot.auth_expire_text = "无授权记录"
                snapshot.auth_card_fingerprint = ""
                snapshot.auth_card_mask = ""
                snapshot.auth_activated_at = 0
                snapshot.auth_user_id = ""
                snapshot.runtime_json = {}
                snapshot.runtime_updated_at = None

    if config_source_complete and client_portal_group_ids:
        for snapshot in db.scalars(
            select(GroupConfigSnapshot).where(
                GroupConfigSnapshot.group_id.in_(client_portal_group_ids)
            ).order_by(GroupConfigSnapshot.group_id).with_for_update().execution_options(
                populate_existing=True
            )
        ):
            snapshot.revision = _safe_bigint(snapshot.revision)
            snapshot.applied_revision = _safe_bigint(snapshot.applied_revision)
            if snapshot.group_id in portal_group_ids:
                state = _group_client_sync(db, snapshot.group_id, normalized_client_id)
                if state is None:
                    continue
                state.last_seen_at = now
                snapshot.last_seen_at = now
                if auth_source_complete and snapshot.sync_status in {
                    LOCAL_SOURCE_PROTECTED_STATUS,
                    LOCAL_SOURCE_PARTIAL_STATUS,
                }:
                    snapshot.sync_status = (
                        "pending"
                        if snapshot.revision > snapshot.applied_revision
                        else "synced"
                    )
                    snapshot.sync_error = ""
                if (
                    state.pull_requested
                    and _safe_bigint(state.applied_revision) < snapshot.revision
                ):
                    changes.append(
                        {
                            "group_id": snapshot.group_id,
                            "revision": snapshot.revision,
                            "config": _client_config(snapshot.config_json),
                            "manual_pull": True,
                            **change_metadata(snapshot),
                        }
                    )
                continue
            snapshot.client_id = client_id[:64]
            snapshot.last_seen_at = now
            if auth_source_complete and snapshot.sync_status in {
                LOCAL_SOURCE_PROTECTED_STATUS,
                LOCAL_SOURCE_PARTIAL_STATUS,
            }:
                # A complete authoritative heartbeat proves the local source
                # can be read again.  Preserve a pending web revision if one
                # exists; otherwise return the row to its normal synced state.
                snapshot.sync_status = (
                    "pending"
                    if snapshot.revision > snapshot.applied_revision
                    else "synced"
                )
                snapshot.sync_error = ""
            if snapshot.revision > snapshot.applied_revision:
                changes.append(
                    {
                        "group_id": snapshot.group_id,
                        "revision": snapshot.revision,
                        "config": _client_config(snapshot.config_json),
                        **change_metadata(snapshot),
                    }
                )

    change_ids = {item["group_id"] for item in changes}
    for item in groups:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("group_id", "")).strip()
        config = item.get("config")
        auth = item.get("auth") if isinstance(item.get("auth"), dict) else None
        has_config = bool(config_source_complete and isinstance(config, dict))
        has_auth = bool(auth_source_complete and isinstance(auth, dict))
        has_game_auth = bool(
            isinstance(item.get("game_auth"), dict)
            and game_auth_source_complete
        )
        if not GROUP_ID_RE.fullmatch(group_id) or not (
            has_config or has_auth or has_game_auth
        ):
            continue
        is_portal_group = group_id in portal_group_ids
        if is_portal_group:
            state = _group_client_sync(db, group_id, normalized_client_id)
            if state is None:
                continue
            state.last_seen_at = now
            snapshot_owner = get_snapshot(db, group_id)
            if snapshot_owner is not None and snapshot_owner.client_id != normalized_client_id:
                continue
        if has_game_auth and not (has_config or has_auth):
            apply_game_authorization_heartbeat(
                db, client_id, group_id, item.get("game_auth")
            )
            continue
        # Re-read under the same row lock even when the heartbeat query above
        # already saw this group.  It keeps the decision to accept the local
        # projection and the pending-revision check atomic with a web save.
        snapshot = get_snapshot(db, group_id, for_update=True)
        incoming = (
            _secure_projection(
                config,
                snapshot.config_json if snapshot is not None else None,
            )
            if has_config
            else None
        )
        if isinstance(incoming, dict):
            incoming["group_id"] = group_id
        if snapshot is None:
            snapshot = GroupConfigSnapshot(
                group_id=group_id,
                revision=1,
                applied_revision=0 if is_portal_group else 1,
                config_json=(
                    {"group_id": group_id, "features": {}}
                    if is_portal_group
                    else (
                        incoming
                        if isinstance(incoming, dict)
                        else {"group_id": group_id, "features": {}}
                    )
                ),
                sync_status="pending" if is_portal_group else "synced",
            )
            db.add(snapshot)
            db.flush()
        else:
            snapshot.revision = _safe_bigint(snapshot.revision)
            snapshot.applied_revision = _safe_bigint(snapshot.applied_revision)
            if (
                is_portal_group
                and snapshot.client_id
                and snapshot.client_id != client_id[:64]
            ):
                continue
            if is_portal_group and not snapshot.client_id:
                snapshot.applied_revision = 0
                snapshot.sync_status = "pending"
        if (
            has_config
            and not is_portal_group
            and snapshot.revision <= snapshot.applied_revision
        ):
            # No pending web edit: local remains canonical and refreshes MySQL.
            snapshot.config_json = incoming
            snapshot.sync_status = "synced"
            snapshot.sync_error = ""

        snapshot.client_id = client_id[:64]
        snapshot.last_seen_at = now
        if has_config:
            # The client included a current config projection for this group,
            # so its durable browser refresh has been fulfilled.  An auth-only
            # partial exchange is never allowed to consume that config pull.
            snapshot.refresh_requested = False
        elif has_auth and config_source_complete:
            # A complete config-source inventory can intentionally contain a
            # manually-authorized group without a local JSON config yet.  The
            # auth-only projection is still the authoritative answer for the
            # durable first-pull request: Windows has reported this group and
            # confirmed that no config file exists.  Leaving the flag set would
            # make every heartbeat return the same refresh hint forever and
            # keep the web page stuck at “等待补传”.  The default projection
            # remains editable; a later web save creates the local file.
            snapshot.refresh_requested = False
        if has_auth and isinstance(auth, dict):
            snapshot.auth_state = _normalized_auth_state(auth.get("state"))
            snapshot.auth_expire = _safe_bigint(auth.get("expire"))
            snapshot.auth_expire_text = str(auth.get("expire_text", "无授权记录"))[:64]
            incoming_fingerprint = str(auth.get("card_fingerprint", "")).strip().lower()
            incoming_card_mask = str(auth.get("card_mask", "")).strip()[:64]
            if not AUTH_CARD_FINGERPRINT_RE.fullmatch(incoming_fingerprint):
                incoming_fingerprint = ""
            if incoming_fingerprint:
                # Current card activation: replace the prior card identity.
                snapshot.auth_card_fingerprint = incoming_fingerprint
                snapshot.auth_card_mask = incoming_card_mask
            elif snapshot.auth_state != "missing":
                # A manual authorization is valid but has no claimable card.
                # Do not leave a former card attached to that newer authorization.
                snapshot.auth_card_fingerprint = ""
                snapshot.auth_card_mask = ""
            # For a deleted/revoked card retain its non-reversible fingerprint
            # and mask.  This lets the user receive an accurate "revoked"
            # message instead of the misleading "not yet synchronized" result.
            snapshot.auth_activated_at = _safe_bigint(auth.get("activated_at"))
            snapshot.auth_user_id = str(auth.get("user_id", ""))[:32]
        if game_auth_source_complete and "game_auth" in item:
            apply_game_authorization_heartbeat(
                db,
                client_id,
                group_id,
                item.get("game_auth"),
            )
        elif has_config and not auth_source_complete:
            # Do not continue trusting a previously active card while its
            # local source is unreadable.  The fingerprint remains only for a
            # useful diagnostic; ``unverified`` blocks login and all writes
            # until a complete auth source restores the real state.
            snapshot.auth_state = "unverified"
            snapshot.auth_expire_text = "等待 Windows 授权验证"
            snapshot.sync_status = LOCAL_SOURCE_PARTIAL_STATUS
            snapshot.sync_error = LOCAL_SOURCE_PARTIAL_ERROR

        if (
            has_config
            and not is_portal_group
            and snapshot.revision > snapshot.applied_revision
            and group_id not in change_ids
        ):
            changes.append(
                {
                    "group_id": group_id,
                    "revision": snapshot.revision,
                    "config": _client_config(snapshot.config_json),
                    **change_metadata(snapshot),
                }
            )
            change_ids.add(group_id)

    if normalized_client_id:
        for group_id in sorted(portal_group_ids):
            state = _group_client_sync(db, group_id, normalized_client_id)
            if state is None:
                continue
            snapshot = get_snapshot(db, group_id)
            if snapshot is None:
                continue
            state.last_seen_at = now
            if snapshot.revision <= _safe_bigint(state.applied_revision):
                continue
            if group_id in change_ids:
                continue
            changes.append(
                {
                    "group_id": group_id,
                    "revision": _safe_bigint(snapshot.revision),
                    "config": _client_config(snapshot.config_json),
                    **change_metadata(snapshot),
                }
            )
            change_ids.add(group_id)

    # ``runtime_summaries`` below uses a locking ``populate_existing`` read for
    # each group.  Flush the full-config/auth updates first; with autoflush
    # disabled that read could otherwise replace dirty values (including a
    # cleared refresh request) with the previous database row.
    db.flush()

    # Runtime summaries are sent on every heartbeat, independently of config
    # changes.  Apply them after the full group loop so a first heartbeat can
    # store runtime data on the snapshot it just created as well.  This keeps
    # the Windows runtime authoritative while allowing a browser save to
    # update only ``config_json``. Unknown groups are ignored.
    if config_source_complete and isinstance(runtime_summaries, dict):
        for raw_group_id, raw_runtime in list(runtime_summaries.items())[:5000]:
            group_id = str(raw_group_id).strip()
            if not GROUP_ID_RE.fullmatch(group_id):
                continue
            # Runtime data is read-only but still belongs to the Windows
            # client that reported the group.  Do not let a shared bearer token
            # overwrite another client's runtime panel, and in authoritative
            # mode never accept a group outside the complete local set.
            if group_id not in normalized_known_ids:
                continue
            snapshot = get_snapshot(db, group_id, for_update=True)
            if snapshot is None:
                continue
            if snapshot.client_id and snapshot.client_id != client_id[:64]:
                continue
            incoming_runtime = _safe_runtime_summary(raw_runtime)
            # A heartbeat without a member panel is not a member snapshot.
            # Do not carry the previous directory forward: the portal must
            # never present an old member as current when Windows has not
            # supplied a live result for this exchange.
            previous_runtime = getattr(snapshot, "runtime_json", None)
            if isinstance(previous_runtime, str):
                try:
                    previous_runtime = json.loads(previous_runtime)
                except (TypeError, ValueError, json.JSONDecodeError):
                    previous_runtime = {}
            previous_runtime = (
                previous_runtime if isinstance(previous_runtime, dict) else {}
            )
            # A direct message-cache upload can arrive just before a regular
            # heartbeat. Preserve that delta when the heartbeat was built
            # from an older local snapshot instead of replacing it with stale
            # message rows.
            previous_cache = previous_runtime.get("message_cache")
            incoming_cache = incoming_runtime.get("message_cache")
            previous_message_rows = (
                previous_cache.get("records", [])
                if isinstance(previous_cache, dict)
                else []
            )
            incoming_message_rows = (
                incoming_cache.get("records", [])
                if isinstance(incoming_cache, dict)
                else []
            )
            previous_message_keys = {
                _runtime_message_key(row)
                for row in (
                    previous_message_rows
                    if isinstance(previous_message_rows, list)
                    else []
                )
                if isinstance(row, dict)
                and _runtime_message_key(row)
            }
            if isinstance(previous_message_rows, list) and previous_message_rows:
                combined_rows = list(incoming_message_rows) if isinstance(incoming_message_rows, list) else []
                combined_rows.extend(previous_message_rows)
                merged_rows: list[dict[str, Any]] = []
                seen_message_keys: set[str] = set()

                for row in sorted(
                    (item for item in combined_rows if isinstance(item, dict)),
                    key=lambda item: _safe_bigint(item.get("timestamp"), 0),
                    reverse=True,
                ):
                    key = _runtime_message_key(row)
                    if not key or key in seen_message_keys:
                        continue
                    seen_message_keys.add(key)
                    merged_rows.append(row)
                incoming_runtime["message_cache"] = {
                    **(incoming_cache if isinstance(incoming_cache, dict) else {}),
                    "available": True,
                    "count": len(merged_rows),
                    "records": merged_rows,
                }
            persisted_cache = incoming_runtime.get("message_cache")
            persisted_message_rows = (
                persisted_cache.get("records", [])
                if isinstance(persisted_cache, dict)
                else []
            )
            # Older Windows clients can send complete history only through
            # the ordinary heartbeat. Backfill it once, then only persist new
            # rows so normal polls do not rewrite the entire message history.
            backfill_pending = not bool(
                previous_runtime.get("_message_event_backfill_v1", False)
            )
            rows_for_event_store = [
                row
                for row in (persisted_message_rows if isinstance(persisted_message_rows, list) else [])
                if isinstance(row, dict)
                and (
                    backfill_pending
                    or _runtime_message_key(row) not in previous_message_keys
                )
            ]
            for offset in range(0, len(rows_for_event_store), 100):
                _persist_group_message_events(
                    db,
                    client_id[:64],
                    group_id,
                    rows_for_event_store[offset : offset + 100],
                )
            if isinstance(persisted_message_rows, list):
                # This internal marker remains in runtime_json only. The
                # browser runtime view exposes known panels and ignores it.
                incoming_runtime["_message_event_backfill_v1"] = True
            snapshot.runtime_json = incoming_runtime
            snapshot.runtime_updated_at = now
    # ``get_snapshot(..., for_update=True)`` uses ``populate_existing`` and the
    # portal session has autoflush disabled.  Persist all changes before the
    # caller performs failure/missing/refresh queries, otherwise a dirty ORM
    # row can be overwritten by the database's previous value in the same
    # heartbeat transaction.
    db.flush()
    if len(changes) > MAX_PENDING_CHANGES_PER_EXCHANGE:
        # Prefer the most recently saved web revision.  This makes a user's
        # just-saved group visible on the next poll even when another group
        # had an older pending revision.  The remaining revisions stay in the
        # database and are returned one at a time on subsequent polls.
        changes.sort(
            key=lambda item: (
                str(item.get("updated_at", "") or ""),
                _safe_bigint(item.get("revision", 0)),
                str(item.get("group_id", "")),
            ),
            reverse=True,
        )
    return changes[:MAX_PENDING_CHANGES_PER_EXCHANGE]


def record_bot_sync_failures(
    db: Session, client_id: str, failures: list[dict[str, Any]]
) -> int:
    """Record Windows apply/ACK failures for still-pending exact revisions.

    A report always arrives on a *later* exchange.  It must therefore never
    overwrite a newer web save, revive a completed revision, or expose raw
    local exception text.  The pending revision remains available for the
    Windows client to retry on the same response.
    """
    # The failure report is sent on a later exchange, so the bearer token by
    # itself is not enough to identify which Windows installation encountered
    # the error.  Require the stable client identity that currently owns the
    # snapshot; otherwise an old process can mark the current process's
    # pending revision as failed after ownership has moved.
    normalized_client_id = str(client_id or "").strip()
    if not normalized_client_id or len(normalized_client_id) > 64:
        return 0
    accepted = 0
    for item in failures:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("group_id", "")).strip()
        revision = _safe_bigint(item.get("revision"))
        summary = str(item.get("error", "")).strip()
        if (
            not GROUP_ID_RE.fullmatch(group_id)
            or revision <= 0
            or not SYNC_FAILURE_SUMMARY_RE.fullmatch(summary)
        ):
            continue
        snapshot = get_snapshot(db, group_id, for_update=True)
        if snapshot is None:
            continue
        snapshot.revision = _safe_bigint(snapshot.revision)
        snapshot.applied_revision = _safe_bigint(snapshot.applied_revision)
        if db.scalar(
            select(GroupAccess.id).where(GroupAccess.group_id == group_id)
        ) is not None:
            state = _group_client_sync(
                db, group_id, normalized_client_id, for_update=True
            )
            if (
                state is None
                or snapshot.revision != revision
                or _safe_bigint(state.applied_revision) >= revision
            ):
                continue
            state.sync_error = summary[:255]
            state.last_seen_at = utcnow()
            _update_group_sync_summary(db, snapshot)
            accepted += 1
            continue
        if (
            not snapshot.client_id
            or snapshot.client_id != normalized_client_id
            or snapshot.revision != revision
            or snapshot.applied_revision >= revision
        ):
            continue
        snapshot.sync_status = "error"
        snapshot.sync_error = summary[:255]
        accepted += 1
    return accepted


def acknowledge_bot_changes(
    db: Session,
    client_id: str,
    groups: list[dict[str, Any]],
    *,
    return_details: bool = False,
) -> int | dict[str, Any]:
    normalized_client_id = str(client_id or "").strip()[:64]
    if not normalized_client_id:
        # A client identity is part of the Windows-first ownership contract;
        # accepting a blank ACK would let an old/malformed worker complete a
        # pending revision after another Windows instance took over the group.
        return {"applied": 0, "acknowledged_group_ids": []} if return_details else 0
    count = 0
    acknowledged_group_ids: list[str] = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("group_id", "")).strip()
        revision = _safe_bigint(item.get("revision"))
        config = item.get("config")
        # ACKs must not mark a newer browser revision as applied.  Locking
        # here serializes the revision comparison with ``save_web_config``.
        snapshot = get_snapshot(db, group_id, for_update=True)
        if snapshot is None:
            continue
        snapshot.revision = _safe_bigint(snapshot.revision)
        snapshot.applied_revision = _safe_bigint(snapshot.applied_revision)
        if db.scalar(
            select(GroupAccess.id).where(GroupAccess.group_id == group_id)
        ) is not None:
            state = _group_client_sync(
                db, group_id, normalized_client_id, for_update=True
            )
            if state is None or revision > snapshot.revision:
                continue
            state.last_seen_at = utcnow()
            if revision > _safe_bigint(state.applied_revision):
                state.applied_revision = revision
            if revision >= snapshot.revision:
                state.pull_requested = False
            state.sync_error = ""
            _update_group_sync_summary(db, snapshot)
            count += 1
            acknowledged_group_ids.append(group_id)
            continue
        # Verify ownership before accepting either an exact or superseded
        # revision. An old Windows instance must never ACK a group after a new
        # instance has taken over the snapshot.
        if snapshot.client_id and snapshot.client_id != normalized_client_id:
            continue
        if revision != snapshot.revision:
            # A newer browser save superseded this exact revision between the
            # exchange response and the ACK request. The old revision no
            # longer needs an ACK; the next heartbeat will fetch the newer
            # revision. Treat it as consumed instead of reporting a false
            # transport failure and waiting a full polling interval.
            if revision < snapshot.revision:
                acknowledged_group_ids.append(group_id)
                count += 1
            continue
        if isinstance(config, dict) and db.scalar(
            select(GroupAccess.id).where(GroupAccess.group_id == group_id)
        ) is None:
            snapshot.config_json = _secure_projection(config, snapshot.config_json)
            snapshot.config_json["group_id"] = group_id
        snapshot.applied_revision = revision
        snapshot.sync_status = "synced"
        snapshot.sync_error = ""
        snapshot.last_seen_at = utcnow()
        snapshot.client_id = normalized_client_id
        count += 1
        acknowledged_group_ids.append(group_id)
    if return_details:
        return {
            "applied": count,
            "acknowledged_group_ids": acknowledged_group_ids,
        }
    return count


def _event_datetime(value: Any) -> datetime:
    """Convert a client timestamp to naive UTC, falling back safely."""
    try:
        timestamp = float(value)
        if timestamp > 0:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return utcnow()


def ingest_console_events(
    db: Session,
    client_id: str,
    raw_events: list[dict[str, Any]],
    *,
    retention: int = 2000,
    message_content_enabled: bool = False,
    return_event_ids: bool = False,
) -> int | list[str]:
    """Store validated, group-scoped runtime events.

    The local client may retry the same batch; the unique event ID makes that
    operation idempotent.  A message preview is accepted only while the
    portal-wide tracking setting is enabled.
    """
    # Runtime events are part of the same Windows-owned data boundary as
    # configuration snapshots.  A client holding the shared sync token must
    # not be able to write a CMD record for another Windows installation's
    # group, and a stale client must not repopulate a group after ownership has
    # moved.  The exchange caller may have just assigned ownership in this
    # transaction, so flush those updates before querying the rows.
    normalized_client_id = str(client_id or "").strip()
    if not normalized_client_id or len(normalized_client_id) > 64:
        return [] if return_event_ids else 0
    candidate_group_ids = {
        str(raw.get("group_id", "")).strip()
        for raw in raw_events
        if isinstance(raw, dict)
    }
    candidate_group_ids = {
        group_id
        for group_id in candidate_group_ids
        if GROUP_ID_RE.fullmatch(group_id)
    }
    if not candidate_group_ids:
        return [] if return_event_ids else 0
    db.flush()
    owned_group_ids = set(
        db.scalars(
            select(GroupConfigSnapshot.group_id).where(
                GroupConfigSnapshot.group_id.in_(candidate_group_ids),
                GroupConfigSnapshot.client_id == normalized_client_id,
            )
        )
    )
    if not owned_group_ids:
        # Compatibility for the first heartbeat on a freshly-created portal
        # database.  Older clients uploaded console events through this helper
        # before their first group snapshot was persisted.  Once any snapshot
        # exists, keep the strict client/group ownership boundary below so a
        # stale token cannot inject records into another installation's group.
        snapshot_exists = db.scalar(
            select(func.count()).select_from(GroupConfigSnapshot)
        )
        if not snapshot_exists:
            owned_group_ids = set(candidate_group_ids)
    if not owned_group_ids:
        return [] if return_event_ids else 0
    accepted = 0
    accepted_event_ids: list[str] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        event_id = str(raw.get("event_id", "")).strip()[:80]
        group_id = str(raw.get("group_id", "")).strip()
        if not EVENT_ID_RE.fullmatch(event_id) or not GROUP_ID_RE.fullmatch(group_id):
            continue
        if group_id not in owned_group_ids:
            # Ignore rather than reject the whole batch: one stale event can
            # remain in a local journal after a client handoff, while valid
            # events from the current client should still be accepted.
            continue
        existing_event = db.scalar(
            select(PortalConsoleEvent).where(PortalConsoleEvent.event_id == event_id)
        )
        if existing_event is not None:
            # A response can be lost after this transaction commits.  Report
            # a duplicate ID back only when it belongs to this same Windows
            # client and group, letting the local journal safely stop retrying
            # without acknowledging another installation's event.
            if (
                existing_event.client_id == normalized_client_id
                and existing_event.group_id == group_id
            ):
                accepted_event_ids.append(event_id)
            continue
        def clean(value: Any, limit: int) -> str:
            return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:limit]
        direction = clean(raw.get("direction"), 16).lower()
        if direction not in {"inbound", "outbound"}:
            direction = ""
        message_content = (
            clean(raw.get("message_content"), 2048)
            if message_content_enabled
            else ""
        )
        db.add(
            PortalConsoleEvent(
                event_id=event_id,
                client_id=clean(client_id, 64),
                group_id=group_id,
                action=clean(raw.get("action"), 64),
                user_id=clean(raw.get("user_id"), 32),
                user_name=clean(raw.get("user_name"), 128),
                message_id=clean(raw.get("message_id"), 64),
                message_type=clean(raw.get("message_type"), 128),
                detail=clean(raw.get("detail"), 160),
                direction=direction,
                message_content=message_content,
                occurred_at=_event_datetime(raw.get("timestamp")),
            )
        )
        accepted += 1
        accepted_event_ids.append(event_id)

    # Most synchronization exchanges are heartbeats with no new runtime
    # events.  Avoid an unnecessary flush and full retention scan on every
    # heartbeat, which otherwise competes with customer configuration saves.
    if not accepted:
        return accepted_event_ids if return_event_ids else 0

    # Keep the table bounded even when the portal receives events for many
    # groups.  This query is portable across MySQL and SQLite test databases.
    try:
        keep = max(100, min(10000, int(retention)))
    except (TypeError, ValueError):
        keep = 2000
    db.flush()
    stale_ids = list(
        db.scalars(
            select(PortalConsoleEvent.id)
            .order_by(desc(PortalConsoleEvent.occurred_at), desc(PortalConsoleEvent.id))
            .offset(keep)
        )
    )
    if stale_ids:
        db.execute(delete(PortalConsoleEvent).where(PortalConsoleEvent.id.in_(stale_ids)))
    return accepted_event_ids if return_event_ids else accepted


def _console_filter_datetime(value: Any) -> datetime | None:
    """Return a database-comparable UTC datetime for a console filter.

    The persistence column is a naive UTC ``DATETIME``.  API users may pass a
    datetime or an ISO-8601 value; timezone-aware values are normalised to
    UTC and old/invalid input is rejected by the caller instead of silently
    broadening a customer's log query.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _filtered_console_events_query(
    db: Session,
    group_id: str,
    keywords: Any = None,
    *,
    user_id: Any = None,
    user_name: Any = None,
    start_at: Any = None,
    end_at: Any = None,
):
    """Build the strictly-scoped console event query used by list and count.

    The CMD page is scoped by its route's group ID.  An invalid scope must not
    degrade into an unfiltered query, otherwise another subgroup's runtime
    events could appear in the current subgroup view.  New filter arguments
    are keyword-only so existing callers that pass ``keywords`` positionally
    remain compatible.
    """
    scoped_group_id = str(group_id or "").strip()
    if not GROUP_ID_RE.fullmatch(scoped_group_id):
        return None
    query = select(PortalConsoleEvent).where(
        PortalConsoleEvent.group_id == scoped_group_id
    )
    normalized_user_id = str(user_id or "").strip()
    if normalized_user_id:
        # QQ is an identifier rather than arbitrary text.  Reject malformed
        # input rather than accidentally returning the whole group's history.
        if not re.fullmatch(r"\d{1,20}", normalized_user_id):
            return None
        query = query.where(PortalConsoleEvent.user_id == normalized_user_id)
    normalized_user_name = str(user_name or "").replace("\r", " ").replace("\n", " ").strip()
    if normalized_user_name:
        query = query.where(
            PortalConsoleEvent.user_name.contains(
                normalized_user_name[:128], autoescape=True
            )
        )
    start_time = _console_filter_datetime(start_at)
    end_time = _console_filter_datetime(end_at)
    if start_at not in (None, "") and start_time is None:
        return None
    if end_at not in (None, "") and end_time is None:
        return None
    if start_time is not None and end_time is not None and start_time > end_time:
        return None
    if start_time is not None:
        query = query.where(PortalConsoleEvent.occurred_at >= start_time)
    if end_time is not None:
        query = query.where(PortalConsoleEvent.occurred_at <= end_time)
    normalized = normalize_console_keywords(keywords)
    if normalized:
        conditions = []
        for keyword in normalized:
            conditions.extend(
                (
                    PortalConsoleEvent.action.contains(keyword, autoescape=True),
                    PortalConsoleEvent.message_type.contains(keyword, autoescape=True),
                    PortalConsoleEvent.detail.contains(keyword, autoescape=True),
                    PortalConsoleEvent.message_content.contains(keyword, autoescape=True),
                )
            )
        query = query.where(or_(*conditions))
    return query


def count_console_events(
    db: Session,
    group_id: str,
    keywords: Any = None,
    *,
    user_id: Any = None,
    user_name: Any = None,
    start_at: Any = None,
    end_at: Any = None,
) -> int:
    """Return the count for the exact same bounded console filter."""

    query = _filtered_console_events_query(
        db,
        group_id,
        keywords,
        user_id=user_id,
        user_name=user_name,
        start_at=start_at,
        end_at=end_at,
    )
    if query is None:
        return 0
    return int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)


def list_console_events(
    db: Session,
    group_id: str,
    keywords: Any = None,
    *,
    user_id: Any = None,
    user_name: Any = None,
    start_at: Any = None,
    end_at: Any = None,
    limit: int = 100,
    offset: int = 0,
) -> list[PortalConsoleEvent]:
    """Return newest events for exactly one group with bounded filters.

    ``offset`` is intentionally explicit rather than inferred from a browser
    page number.  The Windows side continues to call this with the old
    defaults, while the group page can render a stable server-side pager.
    """

    query = _filtered_console_events_query(
        db,
        group_id,
        keywords,
        user_id=user_id,
        user_name=user_name,
        start_at=start_at,
        end_at=end_at,
    )
    if query is None:
        return []
    try:
        safe_limit = max(1, min(500, int(limit)))
    except (TypeError, ValueError, OverflowError):
        safe_limit = 100
    try:
        safe_offset = max(0, min(1_000_000, int(offset)))
    except (TypeError, ValueError, OverflowError):
        safe_offset = 0
    return list(
        db.scalars(
            query.order_by(desc(PortalConsoleEvent.occurred_at), desc(PortalConsoleEvent.id))
            .offset(safe_offset)
            .limit(safe_limit)
        )
    )


def _normalize_console_purge_text(
    value: Any,
    *,
    label: str,
    maximum: int = MAX_CONSOLE_PURGE_FILTER_LENGTH,
) -> str | None:
    """Validate an optional exact log filter without accepting control data."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label}格式不正确")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        raise ValueError(f"{label}格式不正确")
    return normalized


def _normalize_console_purge_days(value: Any) -> int:
    """Parse a positive, bounded whole-day retention value."""

    if isinstance(value, bool):
        raise ValueError("日志保留天数必须是正整数")
    raw = str(value or "").strip()
    if not raw.isdigit():
        raise ValueError("日志保留天数必须是正整数")
    days = int(raw)
    if not 1 <= days <= MAX_CONSOLE_PURGE_DAYS:
        raise ValueError(
            f"日志保留天数必须在 1-{MAX_CONSOLE_PURGE_DAYS} 天之间"
        )
    return days


def purge_console_events(
    db: Session,
    older_than_days: Any,
    *,
    action: Any = None,
    message_type: Any = None,
    group_id: Any = None,
    now: datetime | None = None,
) -> int:
    """Delete old Windows runtime logs and return the number removed.

    ``older_than_days`` is measured against ``occurred_at`` in naive UTC.  The
    optional filters are exact matches (never SQL ``LIKE`` expressions), which
    makes an administrator's "clear this type" operation predictable.  The
    caller owns the transaction and must commit or roll back the deletion.
    ``now`` exists for deterministic maintenance jobs and tests; aware values
    are normalized to UTC while naive values are treated as UTC.
    """

    days = _normalize_console_purge_days(older_than_days)
    normalized_action = _normalize_console_purge_text(action, label="日志类型")
    normalized_message_type = _normalize_console_purge_text(
        message_type, label="消息类型"
    )
    normalized_group_id = _normalize_console_purge_text(
        group_id, label="群配置 ID", maximum=20
    )
    if normalized_group_id is not None and not GROUP_ID_RE.fullmatch(
        normalized_group_id
    ):
        raise ValueError("群配置 ID 格式不正确")

    if now is None:
        reference_time = utcnow()
    elif isinstance(now, datetime):
        reference_time = now
        if reference_time.tzinfo is not None:
            reference_time = reference_time.astimezone(timezone.utc).replace(
                tzinfo=None
            )
    else:
        raise ValueError("清理时间格式不正确")
    cutoff = reference_time - timedelta(days=days)

    conditions = [PortalConsoleEvent.occurred_at < cutoff]
    if normalized_action is not None:
        conditions.append(PortalConsoleEvent.action == normalized_action)
    if normalized_message_type is not None:
        conditions.append(PortalConsoleEvent.message_type == normalized_message_type)
    if normalized_group_id is not None:
        conditions.append(PortalConsoleEvent.group_id == normalized_group_id)

    # Count before deleting so callers receive a useful result even on database
    # dialects that report ``rowcount=-1`` for a bulk DELETE.  The same
    # predicates are used for both statements and the surrounding transaction
    # keeps the operation atomic for normal maintenance requests.
    matched = int(
        db.scalar(
            select(func.count())
            .select_from(PortalConsoleEvent)
            .where(*conditions)
        )
        or 0
    )
    if not matched:
        return 0
    result = db.execute(
        delete(PortalConsoleEvent)
        .where(*conditions)
        .execution_options(synchronize_session=False)
    )
    rowcount = getattr(result, "rowcount", None)
    try:
        rowcount = int(rowcount)
    except (TypeError, ValueError):
        rowcount = -1
    return rowcount if rowcount >= 0 else matched
