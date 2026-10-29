"""Central control for Windows execution clients.

The Linux portal is the authority for who owns the plugin and which
installations may execute it.  This state is deliberately kept in
``PortalSetting`` so existing portal databases need no schema migration.
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import GroupClientSync, GroupConfigSnapshot, PortalSetting


EXECUTION_CONTROL_SETTING_KEY = "execution_control"
EXECUTION_CLIENT_REGISTRY_SETTING_KEY = "execution_client_registry"
_OWNER_ID_RE = re.compile(r"^\d{5,20}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def _default_control() -> dict:
    return {
        "enforce_owner_ids": True,
        "owner_ids": [],
        "allow_new_clients": True,
        "allowed_client_ids": [],
        "blocked_client_ids": [],
    }


def _normalized_ids(values: object, pattern: re.Pattern[str]) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[\s,，]+", values)
    if not isinstance(values, (list, tuple, set)):
        return []
    result = {str(value or "").strip() for value in values}
    return sorted(value for value in result if pattern.fullmatch(value))


def normalize_execution_control(value: object) -> dict:
    data = _default_control()
    if isinstance(value, dict):
        data["enforce_owner_ids"] = bool(value.get("enforce_owner_ids", True))
        data["allow_new_clients"] = bool(value.get("allow_new_clients", True))
        data["owner_ids"] = _normalized_ids(value.get("owner_ids"), _OWNER_ID_RE)
        data["allowed_client_ids"] = _normalized_ids(
            value.get("allowed_client_ids"), _CLIENT_ID_RE
        )
        data["blocked_client_ids"] = _normalized_ids(
            value.get("blocked_client_ids"), _CLIENT_ID_RE
        )
    return data


def get_execution_control(db: Session) -> dict:
    setting = db.get(PortalSetting, EXECUTION_CONTROL_SETTING_KEY)
    if setting is None:
        return _default_control()
    return normalize_execution_control(setting.value_json)


def save_execution_control(
    db: Session,
    *,
    owner_ids: object,
    enforce_owner_ids: bool,
    allow_new_clients: bool,
    allowed_client_ids: object,
    blocked_client_ids: object,
    operator_id: int | None = None,
) -> dict:
    normalized = normalize_execution_control(
        {
            "owner_ids": owner_ids,
            "enforce_owner_ids": enforce_owner_ids,
            "allow_new_clients": allow_new_clients,
            "allowed_client_ids": allowed_client_ids,
            "blocked_client_ids": blocked_client_ids,
        }
    )
    if normalized["enforce_owner_ids"] and not normalized["owner_ids"]:
        raise ValueError("启用中心主人前，至少填写一个主人 QQ 号")

    setting = db.get(PortalSetting, EXECUTION_CONTROL_SETTING_KEY)
    if setting is None:
        setting = PortalSetting(key=EXECUTION_CONTROL_SETTING_KEY, value_json=normalized)
        db.add(setting)
    else:
        setting.value_json = normalized
    setting.updated_by = operator_id
    return normalized


def execution_control_for_client(db: Session, client_id: object) -> dict:
    normalized_client_id = str(client_id or "").strip()[:64]
    control = get_execution_control(db)
    blocked = normalized_client_id in set(control["blocked_client_ids"])
    allowlisted = normalized_client_id in set(control["allowed_client_ids"])
    client_allowed = not blocked and (
        bool(control["allow_new_clients"]) or allowlisted
    )
    return {
        **control,
        "client_id": normalized_client_id,
        "client_allowed": client_allowed,
        "server_time": int(datetime.now().timestamp()),
    }


def record_execution_client(db: Session, client_id: object) -> None:
    """Record a client before applying its allow/deny decision."""
    normalized = str(client_id or "").strip()[:64]
    if not _CLIENT_ID_RE.fullmatch(normalized):
        return
    setting = db.get(PortalSetting, EXECUTION_CLIENT_REGISTRY_SETTING_KEY)
    registry = setting.value_json if setting and isinstance(setting.value_json, dict) else {}
    registry = dict(registry)
    registry[normalized] = {"last_seen_at": datetime.now().isoformat(timespec="seconds")}
    # Keep the registry bounded if a token is accidentally used by many
    # short-lived installations.
    if len(registry) > 500:
        oldest = sorted(
            registry,
            key=lambda key: str(registry[key].get("last_seen_at", "")),
        )[: len(registry) - 500]
        for key in oldest:
            registry.pop(key, None)
    if setting is None:
        db.add(
            PortalSetting(
                key=EXECUTION_CLIENT_REGISTRY_SETTING_KEY,
                value_json=registry,
            )
        )
    else:
        setting.value_json = registry


def list_execution_clients(db: Session) -> list[dict]:
    """Return known installation IDs and their group/heartbeat summary."""
    clients: dict[str, dict] = {}
    rows = db.execute(
        select(
            GroupClientSync.client_id,
            func.max(GroupClientSync.last_seen_at),
            func.count(GroupClientSync.group_id),
        )
        .where(GroupClientSync.client_id != "")
        .group_by(GroupClientSync.client_id)
    ).all()
    for client_id, last_seen_at, group_count in rows:
        normalized = str(client_id or "").strip()
        if not normalized:
            continue
        clients[normalized] = {
            "client_id": normalized,
            "last_seen_at": last_seen_at,
            "group_count": int(group_count or 0),
        }

    for client_id, last_seen_at in db.execute(
        select(GroupConfigSnapshot.client_id, func.max(GroupConfigSnapshot.last_seen_at))
        .where(GroupConfigSnapshot.client_id != "")
        .group_by(GroupConfigSnapshot.client_id)
    ).all():
        normalized = str(client_id or "").strip()
        if not normalized:
            continue
        item = clients.setdefault(
            normalized,
            {"client_id": normalized, "last_seen_at": None, "group_count": 0},
        )
        if item["last_seen_at"] is None or (
            last_seen_at is not None and last_seen_at > item["last_seen_at"]
        ):
            item["last_seen_at"] = last_seen_at

    registry_setting = db.get(PortalSetting, EXECUTION_CLIENT_REGISTRY_SETTING_KEY)
    registry = registry_setting.value_json if registry_setting else {}
    if isinstance(registry, dict):
        for client_id, value in registry.items():
            normalized = str(client_id or "").strip()
            if not _CLIENT_ID_RE.fullmatch(normalized):
                continue
            item = clients.setdefault(
                normalized,
                {"client_id": normalized, "last_seen_at": None, "group_count": 0},
            )
            if item["last_seen_at"] is None:
                item["last_seen_at"] = str(
                    value.get("last_seen_at", "") if isinstance(value, dict) else ""
                ) or None

    return sorted(
        clients.values(),
        key=lambda item: (
            item["last_seen_at"] is not None,
            str(item["last_seen_at"] or ""),
            item["client_id"],
        ),
        reverse=True,
    )
