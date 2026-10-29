"""Local cache of the Linux portal execution policy."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


_OWNER_ID_RE = re.compile(r"^\d{5,20}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_CONTROL_FILE_NAME = "execution_control.json"


def _default_state() -> dict[str, Any]:
    return {
        "enforce_owner_ids": True,
        "owner_ids": [],
        "allow_new_clients": True,
        "allowed_client_ids": [],
        "blocked_client_ids": [],
        "policy_received": False,
        "client_allowed": True,
    }


def _normalize_ids(value: object, pattern: re.Pattern[str]) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[\s,，]+", value)
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted(
        {
            str(item or "").strip()
            for item in value
            if pattern.fullmatch(str(item or "").strip())
        }
    )


def normalize_execution_control(value: object) -> dict[str, Any]:
    state = _default_state()
    if not isinstance(value, dict):
        return state
    state["enforce_owner_ids"] = bool(value.get("enforce_owner_ids", True))
    state["allow_new_clients"] = bool(value.get("allow_new_clients", True))
    state["owner_ids"] = _normalize_ids(value.get("owner_ids"), _OWNER_ID_RE)
    state["allowed_client_ids"] = _normalize_ids(
        value.get("allowed_client_ids"), _CLIENT_ID_RE
    )
    state["blocked_client_ids"] = _normalize_ids(
        value.get("blocked_client_ids"), _CLIENT_ID_RE
    )
    state["client_allowed"] = bool(value.get("client_allowed", True))
    state["policy_received"] = bool(
        value.get(
            "policy_received",
            "client_id" in value or "server_time" in value,
        )
    )
    if "client_id" in value:
        state["client_id"] = str(value.get("client_id") or "").strip()[:64]
    if "server_time" in value:
        try:
            state["server_time"] = int(value["server_time"])
        except (TypeError, ValueError):
            pass
    return state


def _state_path() -> Path | None:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        directory = Path(get_astrbot_data_path()) / "plugin_data" / "qqadmin"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / _CONTROL_FILE_NAME
    except Exception:
        return None


def load_execution_control() -> dict[str, Any]:
    path = _state_path()
    if path is None:
        return _default_state()
    try:
        with path.open("r", encoding="utf-8") as source:
            return normalize_execution_control(json.load(source))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _default_state()


def save_execution_control(value: object) -> dict[str, Any]:
    state = normalize_execution_control(value)
    path = _state_path()
    if path is None:
        return state
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as destination:
            json.dump(state, destination, ensure_ascii=False, indent=2)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return state
