"""Shared authorization and power-state gate for outbound group messages."""

import json
from pathlib import Path
from typing import Optional

try:  # AstrBot supplies these in production.
    from astrbot.api import logger
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:  # Standalone command/API tests.
    try:
        from ..runtime_compat import logger, get_astrbot_data_path
    except ImportError:
        from runtime_compat import logger, get_astrbot_data_path

try:
    from ..auth.manager import is_group_authorized
except ImportError:  # Support legacy top-level ``qqadmin`` imports.
    from auth.manager import is_group_authorized
from .group_config import get_group_data


def _unwrap_config_value(value):
    if isinstance(value, dict) and "items" in value:
        value = value["items"]
    if isinstance(value, dict) and "default" in value:
        value = value["default"]
    return value


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_group_ids(value) -> frozenset[str]:
    """Normalize a configured group-ID list without accepting empty IDs."""
    value = _unwrap_config_value(value)
    if isinstance(value, (str, int)):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(
        normalized
        for item in value
        if (normalized := str(item).strip()) and normalized != "None"
    )


def _load_auth_settings() -> Optional[tuple[bool, frozenset[str]]]:
    """Load the settings needed by standalone OneBot send helpers.

    Standalone callers do not have a MyPlugin instance, so a failed config read
    must fail closed instead of allowing a potentially unauthorized send.
    """
    try:
        config_path = Path(get_astrbot_data_path()) / "config" / "astrbot_plugin_joker_config.json"
        with config_path.open("r", encoding="utf-8-sig") as config_file:
            config = json.load(config_file)
    except Exception as exc:
        logger.warning(f"Unable to load plugin message gate settings: {exc}")
        return None

    if not isinstance(config, dict):
        logger.warning("Plugin message gate settings are not a JSON object")
        return None

    auth = _unwrap_config_value(config.get("auth", {}))
    auth = auth if isinstance(auth, dict) else {}
    basic = _unwrap_config_value(config.get("basic", {}))
    basic = basic if isinstance(basic, dict) else {}

    enable_auth = _as_bool(_unwrap_config_value(auth.get("enable_auth", False)))
    whitelist_groups = _normalize_group_ids(basic.get("whitelist_groups", []))
    return enable_auth, whitelist_groups


def auth_allows_group_message(
    group_id,
    *,
    enable_auth: Optional[bool] = None,
) -> bool:
    """Check the current authorization policy without checking power state.

    Controlled diagnostics such as the ``/开机`` prompt use this helper.  The
    policy is read from persistent configuration when no override is supplied,
    so hot-reloaded authorization settings cannot leave a stale bypass behind.
    """
    normalized_group_id = str(group_id or "").strip()
    if not normalized_group_id or normalized_group_id == "None":
        return False

    if enable_auth is None:
        settings = _load_auth_settings()
        if settings is None:
            return False
        enable_auth, whitelist_groups = settings
    else:
        whitelist_groups = frozenset()

    if normalized_group_id in whitelist_groups:
        return True
    if not _as_bool(enable_auth):
        return True

    try:
        return bool(is_group_authorized(normalized_group_id))
    except Exception as exc:
        logger.warning(
            f"Unable to check authorization for group {normalized_group_id}: {exc}"
        )
        return False


def can_send_group_message(
    group_id,
    *,
    enable_auth: Optional[bool] = None,
) -> bool:
    """Return whether the target group may receive a plugin-originated message."""
    normalized_group_id = str(group_id or "").strip()
    if not normalized_group_id or normalized_group_id == "None":
        return False

    # A whitelist is an explicit operational bypass.  Check it before the
    # persisted power state so listed groups remain usable even without an
    # individual group config or an ``on`` value.
    settings = _load_auth_settings()
    if settings is None:
        if enable_auth is None:
            return False
        configured_enable_auth = enable_auth
        whitelist_groups = frozenset()
    else:
        configured_enable_auth, whitelist_groups = settings
        if enable_auth is not None:
            configured_enable_auth = enable_auth

    if normalized_group_id in whitelist_groups:
        return True

    try:
        features = get_group_data(normalized_group_id, "features", {})
    except Exception as exc:
        logger.warning(f"Unable to read power state for group {normalized_group_id}: {exc}")
        return False

    if not isinstance(features, dict) or features.get("power_status") != "on":
        logger.debug(f"Blocked group message because group is not powered on: {normalized_group_id}")
        return False

    if not auth_allows_group_message(
        normalized_group_id,
        enable_auth=configured_enable_auth,
    ):
        logger.debug(f"Blocked group message because group is unauthorized: {normalized_group_id}")
        return False
    return True
