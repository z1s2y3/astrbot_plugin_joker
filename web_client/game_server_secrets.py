"""Encrypt Linux-managed plugin WebSocket secrets at portal rest."""

from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .feature_schema import normalize_game_server_profile_descriptor
from .settings import Settings


def _fernet(settings: Settings | None = None) -> Fernet:
    current = settings or Settings.from_env()
    key = base64.urlsafe_b64encode(
        hashlib.sha256(current.session_secret.encode("utf-8")).digest()
    )
    return Fernet(key)


def encrypt_game_server_api_key(value: str, settings: Settings | None = None) -> str:
    return _fernet(settings).encrypt(str(value).encode("utf-8")).decode("ascii")


def decrypt_game_server_api_key(value: str, settings: Settings | None = None) -> str:
    try:
        return _fernet(settings).decrypt(str(value).encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("已保存的插件 WebSocket API Key 无法读取，请重新填写并保存") from exc


def _profile_candidates(value: object) -> list[object]:
    if isinstance(value, str):
        value = value.splitlines()
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    result: list[object] = []
    for item in value:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        if isinstance(item, dict):
            result.append(item)
    return result


def store_game_server_profiles(
    value: object,
    previous: object = None,
    *,
    settings: Settings | None = None,
    encrypt_secrets: bool = True,
) -> list[dict[str, Any]]:
    """Canonicalize profiles and retain the existing API Key on blank input."""
    previous_by_id: dict[str, dict[str, Any]] = {}
    for item in _profile_candidates(previous):
        if isinstance(item, dict) and str(item.get("id", "")).strip():
            previous_by_id[str(item["id"]).strip()] = item
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _profile_candidates(value)[:100]:
        try:
            profile = normalize_game_server_profile_descriptor(raw)
        except ValueError:
            continue
        profile_id = profile["id"]
        if profile_id in seen:
            continue
        old = previous_by_id.get(profile_id, {})
        api_key = str(profile.get("api_key", "") or "")
        old_api_key = str(old.get("api_key", "") or "")
        api_key_ciphertext = str(old.get("api_key_ciphertext", "") or "")
        if api_key and encrypt_secrets:
            api_key_ciphertext = encrypt_game_server_api_key(api_key, settings)
        elif api_key and not encrypt_secrets:
            profile["api_key"] = api_key
        elif old_api_key and not encrypt_secrets:
            profile["api_key"] = old_api_key
        if encrypt_secrets:
            profile.pop("api_key", None)
        if api_key_ciphertext:
            profile["api_key_ciphertext"] = api_key_ciphertext[:2048]
            profile["api_key_configured"] = True
        else:
            profile["api_key_configured"] = False
        result.append(profile)
        seen.add(profile_id)
    return result


def redact_game_server_profiles(value: object) -> list[str]:
    """Return JSON lines suitable for a web form without exposing secrets."""
    lines: list[str] = []
    for profile in redact_game_server_profile_records(value):
        lines.append(json.dumps(profile, ensure_ascii=False, separators=(",", ":")))
    return lines


def redact_game_server_profile_records(value: object) -> list[dict[str, Any]]:
    """Return structured profile rows for the human-friendly web form."""
    records: list[dict[str, Any]] = []
    for raw in _profile_candidates(value)[:100]:
        try:
            profile = normalize_game_server_profile_descriptor(raw)
        except ValueError:
            continue
        profile["api_key"] = ""
        profile["api_key_configured"] = bool(
            raw.get("api_key_ciphertext")
            or raw.get("api_key_configured")
            or raw.get("api_key")
        ) if isinstance(raw, dict) else False
        records.append(profile)
    return records


def materialize_game_server_secrets(config: object, settings: Settings | None = None) -> dict[str, Any]:
    """Create the one outbound Windows projection containing the API Keys."""
    projected = deepcopy(config) if isinstance(config, dict) else {}
    features = projected.get("features")
    if not isinstance(features, dict):
        return projected
    game_servers = features.get("game_servers")
    if not isinstance(game_servers, dict):
        return projected
    profiles: list[dict[str, Any]] = []
    for raw in _profile_candidates(game_servers.get("profiles", []))[:100]:
        try:
            profile = normalize_game_server_profile_descriptor(raw)
        except ValueError:
            continue
        api_key_ciphertext = str(raw.get("api_key_ciphertext", "") or "")
        if api_key_ciphertext:
            profile["api_key"] = decrypt_game_server_api_key(api_key_ciphertext, settings)
        profiles.append(profile)
    game_servers["profiles"] = profiles
    features["game_servers"] = game_servers
    projected["features"] = features
    return projected


__all__ = [
    "decrypt_game_server_api_key",
    "encrypt_game_server_api_key",
    "materialize_game_server_secrets",
    "redact_game_server_profiles",
    "redact_game_server_profile_records",
    "store_game_server_profiles",
]
