"""Independent game/WebSocket authorization for the local AstrBot client.

Game access is intentionally separate from QQ group-management cards.  One
group may have one generated game card; the card is never regenerated while
its record is retained, and only its expiry may be extended.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
    from astrbot.api import logger
except ImportError:
    try:
        from ..runtime_compat import get_astrbot_data_path, logger
    except ImportError:
        from runtime_compat import get_astrbot_data_path, logger


GAME_AUTH_RETENTION_DAYS = 7
GAME_AUTH_RETENTION_SECONDS = GAME_AUTH_RETENTION_DAYS * 86400
GAME_AUTH_FILE_NAME = "game_auth.json"
GAME_LICENSE_RE = re.compile(
    r"^JG(?:-1-\d{13}[0-9A-F]{12}|[1-9]\d*-\d{13}[0-9A-F]{12})$"
)
_GROUP_ID_RE = re.compile(r"^\d{5,20}$")
_AUTH_LOCK = threading.RLock()


def get_game_auth_path() -> Path:
    path = Path(get_astrbot_data_path()) / "plugin_data" / "auth"
    path.mkdir(parents=True, exist_ok=True)
    return path / GAME_AUTH_FILE_NAME


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _empty_data() -> dict[str, Any]:
    return {"version": 1, "groups": {}}


def _normalize_group_id(group_id: object) -> str:
    value = str(group_id or "").strip()
    return value if _GROUP_ID_RE.fullmatch(value) else ""


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return default


def _normalize_info(info: object) -> dict[str, Any]:
    value = info if isinstance(info, dict) else {}
    expire = _safe_int(value.get("expire"), 0)
    if expire < -1:
        expire = 0
    activated_at = max(0, _safe_int(value.get("activated_at"), 0))
    issued_at = max(0, _safe_int(value.get("issued_at"), activated_at))
    last_extended_at = max(0, _safe_int(value.get("last_extended_at"), 0))
    key = str(value.get("key", "") or "").strip().upper()
    state = str(value.get("state", "") or "").strip().lower()
    if state not in {"active", "expired"}:
        if expire == -1 or expire > int(time.time()):
            state = "active"
        elif expire > 0:
            state = "expired"
        else:
            state = "missing"
    return {
        "key": key if GAME_LICENSE_RE.fullmatch(key) else "",
        "days": _safe_int(value.get("days"), 0),
        "expire": expire,
        "activated_at": activated_at,
        "issued_at": issued_at,
        "last_extended_at": last_extended_at,
        "user_id": str(value.get("user_id", "") or "")[:32],
        "state": state,
    }


def load_game_auth_data_with_health() -> tuple[dict[str, Any], bool]:
    path = get_game_auth_path()
    with _AUTH_LOCK:
        try:
            if not path.exists():
                return _empty_data(), True
            with path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            groups = raw.get("groups") if isinstance(raw, dict) else None
            if not isinstance(groups, dict):
                return _empty_data(), False
            normalized = {
                group_id: _normalize_info(info)
                for group_id, info in groups.items()
                if _normalize_group_id(group_id)
                for group_id in [_normalize_group_id(group_id)]
            }
            return {"version": 1, "groups": normalized}, True
        except (OSError, ValueError, TypeError) as exc:
            logger.error("加载游戏授权失败：%s", type(exc).__name__)
            return _empty_data(), False


def load_game_auth_data() -> dict[str, Any]:
    return load_game_auth_data_with_health()[0]


def save_game_auth_data(data: dict[str, Any]) -> bool:
    groups = data.get("groups") if isinstance(data, dict) else {}
    if not isinstance(groups, dict):
        return False
    normalized = {
        group_id: _normalize_info(info)
        for raw_group_id, info in groups.items()
        for group_id in [_normalize_group_id(raw_group_id)]
        if group_id and _normalize_info(info).get("key")
    }
    try:
        _atomic_write_json(get_game_auth_path(), {"version": 1, "groups": normalized})
        return True
    except OSError as exc:
        logger.error("保存游戏授权失败：%s", type(exc).__name__)
        return False


def _signature(auth_key: str, timestamp_ms: int, group_id: str, index: int = 0) -> str:
    payload = f"{timestamp_ms}:{group_id}:{index}".encode("utf-8")
    return hmac.new(
        str(auth_key or "").encode("utf-8"),
        b"joker-game-license-v1:" + payload,
        hashlib.sha256,
    ).hexdigest()[:12].upper()


def generate_game_license_key(auth_key: str, days: int, group_id: str) -> str:
    timestamp = int(time.time() * 1000)
    prefix = "JG-1-" if int(days) == -1 else f"JG{int(days)}-"
    return f"{prefix}{timestamp}{_signature(auth_key, timestamp, _normalize_group_id(group_id))}"


def is_supported_game_license_key(key: object) -> bool:
    return bool(GAME_LICENSE_RE.fullmatch(str(key or "").strip().upper()))


def cleanup_expired_game_authorizations(
    retention_days: int = GAME_AUTH_RETENTION_DAYS, now: int | None = None
) -> int:
    del retention_days  # Retention is fixed by protocol and cannot be weakened by a form.
    current = int(time.time()) if now is None else int(now)
    cutoff = current - GAME_AUTH_RETENTION_SECONDS
    with _AUTH_LOCK:
        data, healthy = load_game_auth_data_with_health()
        if not healthy:
            return 0
        groups = data["groups"]
        removed = 0
        for group_id, info in list(groups.items()):
            expire = _safe_int(info.get("expire"), 0)
            if expire > 0 and expire <= cutoff:
                del groups[group_id]
                removed += 1
        if removed:
            save_game_auth_data(data)
        return removed


def get_game_authorization_info(group_id: object) -> dict[str, Any]:
    normalized = _normalize_group_id(group_id)
    if not normalized:
        return {"state": "missing", "status": "missing", "expire": 0}
    data = load_game_auth_data()
    info = _normalize_info(data.get("groups", {}).get(normalized, {}))
    if not info.get("key"):
        return {"state": "missing", "status": "missing", "expire": 0}
    now = int(time.time())
    state = "active" if info["expire"] == -1 or info["expire"] > now else "expired"
    info["state"] = state
    info["status"] = state
    return info


def list_game_authorizations() -> dict[str, dict[str, Any]]:
    cleanup_expired_game_authorizations()
    data = load_game_auth_data()
    return {
        group_id: get_game_authorization_info(group_id)
        for group_id in data.get("groups", {})
    }


def game_authorization_state(group_id: object) -> str:
    return str(get_game_authorization_info(group_id).get("state", "missing"))


def issue_game_authorization(
    group_id: object, days: int, auth_key: str, user_id: object = ""
) -> dict[str, Any]:
    normalized = _normalize_group_id(group_id)
    if not normalized:
        return {"status": "invalid_group"}
    if not str(auth_key or "").strip():
        return {"status": "missing_auth_key"}
    days = int(days)
    if days == 0 or days < -1:
        return {"status": "invalid_days"}
    with _AUTH_LOCK:
        cleanup_expired_game_authorizations()
        data = load_game_auth_data()
        existing = data.get("groups", {}).get(normalized)
        if existing:
            return {**get_game_authorization_info(normalized), "status": "existing"}
        now = int(time.time())
        expire = -1 if days == -1 else now + days * 86400
        info = {
            "key": generate_game_license_key(auth_key, days, normalized),
            "days": days,
            "expire": expire,
            "activated_at": now,
            "issued_at": now,
            "last_extended_at": 0,
            "user_id": str(user_id or "")[:32],
            "state": "active",
        }
        data.setdefault("groups", {})[normalized] = info
        if not save_game_auth_data(data):
            return {"status": "save_failed"}
        return {"status": "issued", **info}


def add_game_authorization_time(group_id: object, days: int) -> dict[str, Any]:
    normalized = _normalize_group_id(group_id)
    days = int(days)
    if not normalized or days <= 0:
        return {"status": "invalid_request"}
    with _AUTH_LOCK:
        cleanup_expired_game_authorizations()
        data = load_game_auth_data()
        raw = data.get("groups", {}).get(normalized)
        if not raw:
            return {"status": "missing"}
        info = _normalize_info(raw)
        if info["expire"] == -1:
            return {**info, "status": "already_permanent"}
        base = max(int(time.time()), info["expire"])
        info["expire"] = base + days * 86400
        info["last_extended_at"] = int(time.time())
        info["state"] = "active"
        data["groups"][normalized] = info
        if not save_game_auth_data(data):
            return {"status": "save_failed"}
        return {**info, "status": "extended"}


def is_game_authorized(group_id: object) -> bool:
    return game_authorization_state(group_id) == "active"


__all__ = [
    "GAME_AUTH_RETENTION_DAYS",
    "add_game_authorization_time",
    "cleanup_expired_game_authorizations",
    "game_authorization_state",
    "generate_game_license_key",
    "get_game_auth_path",
    "get_game_authorization_info",
    "is_game_authorized",
    "is_supported_game_license_key",
    "issue_game_authorization",
    "list_game_authorizations",
    "load_game_auth_data",
    "load_game_auth_data_with_health",
    "save_game_auth_data",
]
