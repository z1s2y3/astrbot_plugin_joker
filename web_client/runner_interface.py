"""Forward-compatible execution-runner contract for the remote portal.

Today only the Windows AstrBot/NapCat machine is allowed to execute QQ bot
work.  The portal still exposes a small, authenticated capability contract so
a future Linux runner can be added without changing customer configuration
storage or the sync API.  Importantly, marking Linux migration as prepared
does *not* start a bot or stop the Windows runner.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .models import PortalSetting


RUNNER_DEPLOYMENT_SETTING_KEY = "runner_deployment"
RUNNER_API_VERSION = "v1"


def _view(prepared: bool) -> dict[str, Any]:
    """Return only non-sensitive runner information safe for a client API."""
    return {
        "api_version": RUNNER_API_VERSION,
        # This is deliberately not editable until a Linux runner exists and
        # has an explicit activation handshake.
        "active_runner": "windows",
        "linux_migration_prepared": prepared,
        "linux_runner_available": False,
        "linux_execution_enabled": False,
        "message": (
            "Linux 执行端已预留，当前仍由 Windows 执行"
            if prepared
            else "当前由 Windows 执行；可在后台预留 Linux 迁移接口"
        ),
    }


def get_runner_deployment(db: Session) -> dict[str, Any]:
    """Read the portal-only migration switch with a safe Windows default."""
    setting = db.get(PortalSetting, RUNNER_DEPLOYMENT_SETTING_KEY)
    payload = setting.value_json if setting and isinstance(setting.value_json, dict) else {}
    return _view(bool(payload.get("linux_migration_prepared", False)))


def set_linux_migration_prepared(
    db: Session, prepared: bool, operator_id: int | None
) -> dict[str, Any]:
    """Persist the future-migration switch without activating any runner."""
    payload = {"linux_migration_prepared": bool(prepared)}
    setting = db.get(PortalSetting, RUNNER_DEPLOYMENT_SETTING_KEY)
    if setting is None:
        setting = PortalSetting(
            key=RUNNER_DEPLOYMENT_SETTING_KEY,
            value_json=payload,
            updated_by=operator_id,
        )
        db.add(setting)
    else:
        setting.value_json = payload
        setting.updated_by = operator_id
    return _view(bool(prepared))
