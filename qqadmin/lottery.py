"""Per-group lottery configuration and Windows-local draw state.

The Linux portal may edit ``lottery`` as part of a group configuration, but
draw records, per-user limits and remaining stock stay on the Windows bot.
That keeps the bot authoritative and avoids a portal sync for every draw.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import random
import re
import time
from typing import Any
from uuid import uuid4

from .group_config import (
    get_group_data,
    set_group_data,
    update_group_data,
    update_group_feature_config,
)
from ..feature_catalog import split_command_aliases


LOTTERY_CONFIG_KEY = "lottery"
LOTTERY_RUNTIME_KEY = "lottery_runtime"
MAX_LOTTERY_AWARDS = 100
MAX_LOTTERY_RECORDS = 1000
MAX_LOTTERY_USERS = 10000
MAX_LOTTERY_STOCK = 1_000_000
MAX_LOTTERY_DRAWS_PER_USER = 1_000_000
MAX_LOTTERY_COOLDOWN_SECONDS = 31_536_000
MAX_LOTTERY_CAMPAIGNS = 50
_AWARD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TEMPLATE_TOKEN_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")


DEFAULT_LOTTERY_CONFIG: dict[str, Any] = {
    "enabled": False,
    "keyword": "抽奖",
    "max_draws_per_user": 0,
    "cooldown": 0,
    "success_message": "🎉 {name} 抽中了「{prize}」！剩余 {remaining}",
    "empty_message": "{name} 很遗憾没有抽中，再试一次吧。",
    "awards": [],
    "campaigns": [],
}


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    """Convert a persisted integer without accepting malformed extremes."""

    if isinstance(value, bool):
        return default
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _bounded_float(value: object, default: float, minimum: float, maximum: float) -> float:
    """Convert a persisted probability without allowing NaN or infinity."""

    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return max(minimum, min(maximum, number))


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _clean_text(value: object, default: str, maximum: int) -> str:
    text = str(value or "").strip()
    return text[:maximum] if text else default


def _new_award_id(stable_hint: object | None = None) -> str:
    """Create an award ID, deterministically when normalising old JSON.

    Portal-created awards already carry an ID.  Hand-edited campaign JSON
    often omits one; using a UUID on every read would then reset local stock
    and history because the runtime key changes on each heartbeat.  A short
    SHA-256-derived suffix keeps those legacy rows stable across reads while
    retaining UUIDs for genuinely new rows.
    """

    if stable_hint is not None:
        digest = hashlib.sha256(str(stable_hint).encode("utf-8")).hexdigest()[:16]
        return "award-" + digest
    return "award-" + uuid4().hex[:16]


def _normalise_award(
    value: object,
    seen_ids: set[str],
    seen_names: set[str],
    stable_hint: object | None = None,
) -> dict[str, Any] | None:
    """Return one safe award record, or ``None`` for an unusable record."""

    if not isinstance(value, dict):
        return None
    name = _clean_text(value.get("name"), "", 100)
    if not name:
        return None
    name_key = name.casefold()
    if name_key in seen_names:
        return None

    award_id = str(value.get("id", "")).strip()
    if not _AWARD_ID_RE.fullmatch(award_id) or award_id in seen_ids:
        award_id = _new_award_id(stable_hint if stable_hint is not None else name.casefold())
        while award_id in seen_ids:
            award_id = _new_award_id(
                f"{stable_hint if stable_hint is not None else name.casefold()}:{len(seen_ids)}"
            )

    seen_ids.add(award_id)
    seen_names.add(name_key)
    probability = _bounded_float(value.get("probability"), 0.0, 0.0, 100.0)
    stock = _bounded_int(value.get("stock"), 0, 0, MAX_LOTTERY_STOCK)
    return {
        "id": award_id,
        "name": name,
        "probability": probability,
        # Zero stock is deliberately unlimited; this keeps a common "thanks
        # for participating" award from being depleted by normal use.
        "stock": stock,
        "enabled": _as_bool(value.get("enabled"), True),
    }


def _normalise_campaign(
    value: object,
    seen_ids: set[str],
    stable_hint: object | None = None,
) -> dict[str, Any] | None:
    """Normalize one independent lottery campaign definition.

    Campaigns deliberately reuse the same award shape as the legacy root,
    but keep their own command and messages. Runtime stock/history is keyed
    by campaign ID, so several activities can run in one group without
    consuming each other's limits.
    """
    if not isinstance(value, dict):
        return None
    campaign_id = str(value.get("id", "") or "").strip()
    if not _AWARD_ID_RE.fullmatch(campaign_id) or campaign_id in seen_ids:
        seed = stable_hint if stable_hint is not None else (
            f"{value.get('name', '')}|{value.get('keyword', value.get('command', ''))}"
        )
        campaign_id = "campaign-" + hashlib.sha256(str(seed).encode("utf-8")).hexdigest()[:16]
        while campaign_id in seen_ids:
            campaign_id = "campaign-" + hashlib.sha256(
                f"{seed}:{len(seen_ids)}".encode("utf-8")
            ).hexdigest()[:16]
    name = _clean_text(value.get("name"), campaign_id, 100)
    keyword = _clean_text(value.get("keyword", value.get("command")), name, 256)
    aliases = split_command_aliases(keyword, name.replace(" ", "")[:48] or "抽奖")
    keyword = "|".join(aliases)
    enabled = _as_bool(value.get("enabled"), True)
    seen_ids.add(campaign_id)
    raw_awards = value.get("awards", [])
    raw_awards = raw_awards if isinstance(raw_awards, list) else []
    award_ids: set[str] = set()
    award_names: set[str] = set()
    awards: list[dict[str, Any]] = []
    for award_index, raw_award in enumerate(raw_awards[:MAX_LOTTERY_AWARDS]):
        award_name_hint = (
            str(raw_award.get("name", "") or "").strip().casefold()
            if isinstance(raw_award, dict)
            else ""
        )
        award = _normalise_award(
            raw_award,
            award_ids,
            award_names,
            stable_hint=(
                f"campaign:{campaign_id}:award:name:{award_name_hint}"
                if award_name_hint
                else f"campaign:{campaign_id}:award:index:{award_index}"
            ),
        )
        if award is not None:
            awards.append(award)
    return {
        "id": campaign_id,
        "name": name,
        "keyword": keyword,
        "enabled": enabled,
        "max_draws_per_user": _bounded_int(value.get("max_draws_per_user"), 0, 0, MAX_LOTTERY_DRAWS_PER_USER),
        "cooldown": _bounded_int(value.get("cooldown"), 0, 0, MAX_LOTTERY_COOLDOWN_SECONDS),
        "success_message": _clean_text(value.get("success_message"), DEFAULT_LOTTERY_CONFIG["success_message"], 1000),
        "empty_message": _clean_text(value.get("empty_message"), DEFAULT_LOTTERY_CONFIG["empty_message"], 1000),
        "awards": awards,
    }


def normalize_lottery_campaigns(value: object) -> list[dict[str, Any]]:
    """Normalize campaigns supplied as dictionaries or one-JSON-per-line."""
    if isinstance(value, str):
        raw_values: list[Any] = []
        for line in value.splitlines():
            try:
                if line.strip():
                    raw_values.append(json.loads(line))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    elif isinstance(value, (list, tuple)):
        raw_values = []
        for item in value:
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            raw_values.append(item)
    else:
        raw_values = []
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    seen_keywords: set[str] = set()
    for index, item in enumerate(raw_values[:MAX_LOTTERY_CAMPAIGNS]):
        if isinstance(item, dict):
            campaign_name_hint = str(item.get("name", "") or "").strip().casefold()
            campaign_command_hint = str(
                item.get("keyword", item.get("command", "")) or ""
            ).strip().lstrip("/").casefold()
        else:
            campaign_name_hint = ""
            campaign_command_hint = ""
        stable_hint = (
            f"campaign:name:{campaign_name_hint}:command:{campaign_command_hint}"
            if campaign_name_hint or campaign_command_hint
            else f"campaign:index:{index}"
        )
        campaign = _normalise_campaign(item, seen, stable_hint=stable_hint)
        if campaign is not None:
            keyword = str(campaign.get("keyword", ""))
            campaign_aliases = split_command_aliases(keyword, "抽奖")
            # Ambiguous commands make one activity silently shadow another.
            # Keep the first declaration deterministic and ignore later
            # duplicates; the web validator also rejects these on save.
            if any(alias.casefold() in seen_keywords for alias in campaign_aliases):
                continue
            seen_keywords.update(alias.casefold() for alias in campaign_aliases)
            result.append(campaign)
    return result


def normalize_lottery_config(value: object) -> dict[str, Any]:
    """Normalize an editable group lottery configuration.

    This function has no persistence side effect.  It is safe to use on a
    portal payload before applying it to a Windows group configuration.
    """

    source = value if isinstance(value, dict) else {}
    result = deepcopy(DEFAULT_LOTTERY_CONFIG)
    result["enabled"] = _as_bool(source.get("enabled"), result["enabled"])
    keyword = _clean_text(source.get("keyword"), result["keyword"], 256)
    result["keyword"] = "|".join(split_command_aliases(keyword, DEFAULT_LOTTERY_CONFIG["keyword"]))
    result["max_draws_per_user"] = _bounded_int(
        source.get("max_draws_per_user"),
        result["max_draws_per_user"],
        0,
        MAX_LOTTERY_DRAWS_PER_USER,
    )
    result["cooldown"] = _bounded_int(
        source.get("cooldown"),
        result["cooldown"],
        0,
        MAX_LOTTERY_COOLDOWN_SECONDS,
    )
    result["success_message"] = _clean_text(
        source.get("success_message"), result["success_message"], 1000
    )
    result["empty_message"] = _clean_text(
        source.get("empty_message"), result["empty_message"], 1000
    )

    raw_awards = source.get("awards", [])
    raw_awards = raw_awards if isinstance(raw_awards, list) else []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    awards: list[dict[str, Any]] = []
    for award_index, raw_award in enumerate(raw_awards[:MAX_LOTTERY_AWARDS]):
        award_name_hint = (
            str(raw_award.get("name", "") or "").strip().casefold()
            if isinstance(raw_award, dict)
            else ""
        )
        award = _normalise_award(
            raw_award,
            seen_ids,
            seen_names,
            stable_hint=(
                f"root:award:name:{award_name_hint}"
                if award_name_hint
                else f"root:award:index:{award_index}"
            ),
        )
        if award is not None:
            awards.append(award)
    result["awards"] = awards
    campaigns = normalize_lottery_campaigns(source.get("campaigns", []))
    # The legacy root command has priority.  Do not let a malformed campaign
    # silently hijack it after a round-trip through an older web client.
    root_keywords = {
        alias.casefold()
        for alias in split_command_aliases(result["keyword"], DEFAULT_LOTTERY_CONFIG["keyword"])
    }
    result["campaigns"] = [
        campaign
        for campaign in campaigns
        if not root_keywords.intersection(
            alias.casefold()
            for alias in split_command_aliases(campaign.get("keyword"), "抽奖")
        )
    ]
    return result


def validate_lottery_config(value: object) -> dict[str, Any]:
    """Normalize and validate a config with percentage-based prize chances."""

    result = normalize_lottery_config(value)
    source = value if isinstance(value, dict) else {}
    raw_campaigns = source.get("campaigns", [])
    if isinstance(raw_campaigns, (list, tuple)):
        used_keywords = {
            alias.casefold()
            for alias in split_command_aliases(result.get("keyword"), "抽奖")
        }
        for raw_campaign in raw_campaigns[:MAX_LOTTERY_CAMPAIGNS]:
            if isinstance(raw_campaign, str):
                try:
                    raw_campaign = json.loads(raw_campaign)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            if not isinstance(raw_campaign, dict):
                continue
            campaign_aliases = split_command_aliases(
                raw_campaign.get("keyword", raw_campaign.get("command", "")),
                "抽奖",
            )
            if not campaign_aliases:
                continue
            if any(alias.casefold() in used_keywords for alias in campaign_aliases):
                raise ValueError("默认抽奖和多个抽奖活动的指令不能重复")
            used_keywords.update(alias.casefold() for alias in campaign_aliases)
    total_probability = sum(
        award["probability"] for award in result["awards"] if award["enabled"]
    )
    if total_probability > 100.000001:
        raise ValueError("启用奖项的概率总和不能超过 100%")
    for campaign in result.get("campaigns", []):
        campaign_total = sum(
            award["probability"]
            for award in campaign.get("awards", [])
            if award.get("enabled")
        )
        if campaign_total > 100.000001:
            raise ValueError(f"抽奖活动“{campaign.get('name', '')}”的奖项概率总和不能超过 100%")
    return result


def get_lottery_config(group_id: str) -> dict[str, Any]:
    """Load one group's editable lottery settings without mutating storage."""

    raw_config = get_group_data(group_id, LOTTERY_CONFIG_KEY, {})
    raw_config = dict(raw_config) if isinstance(raw_config, dict) else {}
    # The portal's generic feature panel persists its switch under
    # ``features.lottery``.  Keep those portal-managed values authoritative
    # while retaining the root record for award definitions and command-only
    # deployments created before the web feature panel existed.
    features = get_group_data(group_id, "features", {})
    feature = features.get("lottery") if isinstance(features, dict) else None
    if isinstance(feature, dict):
        for key in (
            "enabled",
            "keyword",
            "max_draws_per_user",
            "cooldown",
            "success_message",
            "empty_message",
            "campaigns",
        ):
            if key in feature:
                if key == "campaigns" and not feature.get(key) and raw_config.get(key):
                    # A legacy Windows file may have campaigns only at the
                    # root while an older feature mirror still contains the
                    # default empty list.
                    continue
                raw_config[key] = feature[key]
    return normalize_lottery_config(raw_config)


def get_lottery_campaigns(group_id: str) -> list[dict[str, Any]]:
    """Return independent campaign definitions for a group."""
    return get_lottery_config(group_id).get("campaigns", [])


def save_lottery_campaigns(group_id: str, campaigns: object) -> list[dict[str, Any]]:
    """Replace campaign definitions while retaining legacy awards/settings."""
    current = get_lottery_config(group_id)
    current["campaigns"] = normalize_lottery_campaigns(campaigns)
    return save_lottery_config(group_id, current).get("campaigns", [])


def select_lottery_campaign(group_id: str, selector: object = "") -> dict[str, Any]:
    """Resolve a campaign by ID, name, or command; fallback to legacy root."""
    current = get_lottery_config(group_id)
    wanted = str(selector or "").strip().lstrip("/")
    if wanted:
        for campaign in current.get("campaigns", []):
            if wanted in {
                str(campaign.get("id", "")),
                str(campaign.get("name", "")),
            } or wanted in split_command_aliases(campaign.get("keyword"), "抽奖"):
                merged = dict(current)
                merged.update(campaign)
                # Validate the selected definition as the active root without
                # also re-validating the campaign collection against its own
                # command (which would look like a duplicate by construction).
                merged["campaigns"] = []
                selected = validate_lottery_config(merged)
                selected["campaigns"] = current.get("campaigns", [])
                return selected
    return current


def save_lottery_config(group_id: str, config: object) -> dict[str, Any]:
    """Persist a complete editable lottery config and its feature mirror.

    ``lottery`` is the canonical root definition because it also contains the
    award rows.  The six scalar values are mirrored under
    ``features.lottery`` for the generic feature gate and older Windows
    builds.  Keeping both copies in sync here is important: callers such as
    an admin command or an import routine may call this function directly,
    bypassing the individual ``set_lottery_*`` helpers.
    """

    normalized = validate_lottery_config(config)

    scalar_keys = (
        "enabled",
        "keyword",
        "max_draws_per_user",
        "cooldown",
        "success_message",
        "empty_message",
        "campaigns",
    )

    def update_features(existing: object) -> dict[str, Any]:
        features = deepcopy(existing) if isinstance(existing, dict) else {}
        lottery_feature = features.get("lottery")
        lottery_feature = (
            deepcopy(lottery_feature) if isinstance(lottery_feature, dict) else {}
        )
        for key in scalar_keys:
            lottery_feature[key] = deepcopy(normalized[key])
        features["lottery"] = lottery_feature
        return features

    # Use one atomic group-file write when the current group-config module
    # exposes the batch API.  The fallback keeps compatibility with older
    # hot-reloaded test/Windows modules that only provide the legacy helpers.
    try:
        from .group_config import batch_update_group_data
    except (ImportError, AttributeError):
        set_group_data(group_id, LOTTERY_CONFIG_KEY, normalized)
        update_group_feature_config(
            group_id,
            "lottery",
            "enabled",
            deepcopy(normalized["enabled"]),
        )
        for key in scalar_keys[1:]:
            update_group_feature_config(
                group_id, "lottery", key, deepcopy(normalized[key])
            )
    else:
        batch_update_group_data(
            group_id,
            [
                (
                    LOTTERY_CONFIG_KEY,
                    lambda _old, value=deepcopy(normalized): deepcopy(value),
                ),
                ("features", update_features),
            ],
        )
    return normalized


def update_lottery_config(group_id: str, **changes: Any) -> dict[str, Any]:
    """Apply a partial configuration change while retaining existing awards."""

    current = get_lottery_config(group_id)
    current.update(changes)
    return save_lottery_config(group_id, current)


def set_lottery_enabled(group_id: str, enabled: bool) -> dict[str, Any]:
    # Commands and web form payloads may pass textual values (for example
    # ``"false"``).  ``bool("false")`` is truthy, which used to make a
    # seemingly disabled lottery remain enabled until the next round-trip.
    return update_lottery_config(group_id, enabled=_as_bool(enabled, False))


def set_lottery_keyword(group_id: str, keyword: str) -> dict[str, Any]:
    return update_lottery_config(group_id, keyword=keyword)


def set_lottery_limits(
    group_id: str,
    *,
    max_draws_per_user: object | None = None,
    cooldown: object | None = None,
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if max_draws_per_user is not None:
        changes["max_draws_per_user"] = max_draws_per_user
    if cooldown is not None:
        changes["cooldown"] = cooldown
    return update_lottery_config(group_id, **changes)


def set_lottery_messages(
    group_id: str,
    *,
    success_message: str | None = None,
    empty_message: str | None = None,
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if success_message is not None:
        changes["success_message"] = success_message
    if empty_message is not None:
        changes["empty_message"] = empty_message
    return update_lottery_config(group_id, **changes)


def _find_award_index(awards: list[dict[str, Any]], identifier: object) -> int:
    target = str(identifier or "").strip()
    if not target:
        return -1
    target_name = target.casefold()
    for index, award in enumerate(awards):
        if award["id"] == target or award["name"].casefold() == target_name:
            return index
    return -1


def add_lottery_award(
    group_id: str,
    name: str,
    probability: object,
    stock: object = 0,
) -> dict[str, Any]:
    """Add one prize to a group's editable configuration.

    Probability is a percentage in the inclusive range 0-100.  The enabled
    prize total may not exceed 100%, leaving any remainder as "no prize".
    """

    current = get_lottery_config(group_id)
    if len(current["awards"]) >= MAX_LOTTERY_AWARDS:
        raise ValueError(f"最多可设置 {MAX_LOTTERY_AWARDS} 个奖项")
    clean_name = _clean_text(name, "", 100)
    if not clean_name:
        raise ValueError("奖项名称不能为空")
    if _find_award_index(current["awards"], clean_name) >= 0:
        raise ValueError("同名奖项已经存在")
    award = {
        "id": _new_award_id(),
        "name": clean_name,
        "probability": probability,
        "stock": stock,
        "enabled": True,
    }
    current["awards"].append(award)
    saved = save_lottery_config(group_id, current)
    return next(item for item in saved["awards"] if item["id"] == award["id"])


def update_lottery_award(
    group_id: str,
    identifier: object,
    **changes: Any,
) -> dict[str, Any]:
    """Update one prize by ID or current name and return the saved record."""

    current = get_lottery_config(group_id)
    index = _find_award_index(current["awards"], identifier)
    if index < 0:
        raise ValueError("未找到该奖项")
    updated = dict(current["awards"][index])
    for key in ("name", "probability", "stock", "enabled"):
        if key in changes:
            updated[key] = changes[key]
    if "name" in changes:
        clean_name = _clean_text(changes["name"], "", 100)
        if not clean_name:
            raise ValueError("奖项名称不能为空")
        other_index = _find_award_index(current["awards"], clean_name)
        if other_index not in {-1, index}:
            raise ValueError("同名奖项已经存在")
        updated["name"] = clean_name
    current["awards"][index] = updated
    saved = save_lottery_config(group_id, current)
    return saved["awards"][index]


def remove_lottery_award(group_id: str, identifier: object) -> bool:
    """Remove one prize definition without touching unrelated local records."""

    current = get_lottery_config(group_id)
    index = _find_award_index(current["awards"], identifier)
    if index < 0:
        return False
    current["awards"].pop(index)
    save_lottery_config(group_id, current)
    return True


def lottery_keyword_matches(group_id: str, command: object) -> bool:
    """Return whether a slash command token matches this group's lottery key."""

    actual = str(command or "").strip().lstrip("/")
    config = get_lottery_config(group_id)
    if not actual:
        return False
    if actual in split_command_aliases(config.get("keyword"), "抽奖"):
        return True
    return any(
        campaign.get("enabled", True)
        and actual in split_command_aliases(campaign.get("keyword"), "抽奖")
        for campaign in config.get("campaigns", [])
        if isinstance(campaign, dict)
    )


def lottery_campaign_for_keyword(group_id: str, command: object) -> str:
    """Return the matching campaign ID, or an empty string for legacy mode."""
    actual = str(command or "").strip().lstrip("/")
    if not actual:
        return ""
    config = get_lottery_config(group_id)
    for campaign in config.get("campaigns", []):
        if (
            isinstance(campaign, dict)
            and campaign.get("enabled", True)
            and actual in split_command_aliases(campaign.get("keyword"), "抽奖")
        ):
            return str(campaign.get("id", ""))
    return ""


def _normalise_runtime(value: object) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    awards = source.get("awards")
    users = source.get("users")
    records = source.get("records")
    # Runtime state is local JSON and may survive upgrades or manual edits.
    # Keep only the shapes the draw code understands and cap each collection
    # before copying it, so a malformed/oversized file cannot break an admin
    # command or make one draw allocate unbounded memory.
    if isinstance(awards, dict):
        awards = {
            str(key)[:64]: deepcopy(item)
            for key, item in list(awards.items())[:MAX_LOTTERY_AWARDS]
            if isinstance(item, dict)
        }
    else:
        awards = {}
    if isinstance(users, dict):
        users = {
            str(key)[:64]: deepcopy(item)
            for key, item in list(users.items())[:MAX_LOTTERY_USERS]
            if isinstance(item, dict)
        }
    else:
        users = {}
    if isinstance(records, list):
        records = [
            deepcopy(item)
            for item in records[-MAX_LOTTERY_RECORDS:]
            if isinstance(item, dict)
        ]
    else:
        records = []
    return {
        "awards": awards,
        "users": users,
        "records": records,
    }


def _runtime_award_state(runtime_awards: dict[str, Any], award: dict[str, Any]) -> dict[str, Any]:
    """Load/reset local remaining stock when a web config changes its stock."""

    award_id = award["id"]
    configured_stock = award["stock"]
    raw_state = runtime_awards.get(award_id)
    state = dict(raw_state) if isinstance(raw_state, dict) else {}
    if state.get("configured_stock") != configured_stock:
        state["configured_stock"] = configured_stock
        state["remaining"] = configured_stock
    state["remaining"] = _bounded_int(
        state.get("remaining"), configured_stock, 0, MAX_LOTTERY_STOCK
    )
    runtime_awards[award_id] = state
    return state


def _prune_runtime(runtime: dict[str, Any], record_limit: int) -> None:
    records = runtime["records"]
    if len(records) > record_limit:
        runtime["records"] = records[-record_limit:]
    users = runtime["users"]
    if len(users) > MAX_LOTTERY_USERS:
        ordered = sorted(
            users.items(),
            key=lambda item: _bounded_int(
                item[1].get("last_draw_at", 0) if isinstance(item[1], dict) else 0,
                0,
                0,
                2**63 - 1,
            ),
            reverse=True,
        )
        runtime["users"] = dict(ordered[:MAX_LOTTERY_USERS])


def render_lottery_template(
    template: object,
    *,
    user_name: object = "",
    user_id: object = "",
    group_id: object = "",
    prize: object = "",
    remaining: object = "-",
    draw_count: object = 0,
    now: float | None = None,
) -> str:
    """Expand safe lottery placeholders while preserving unknown future ones."""

    timestamp = time.localtime(time.time() if now is None else now)
    display_name = str(user_name or user_id or "")
    count = _bounded_int(draw_count, 0, 0, MAX_LOTTERY_DRAWS_PER_USER)
    values = {
        "name": display_name,
        "user_name": display_name,
        "qq": str(user_id or ""),
        "user_id": str(user_id or ""),
        "group": str(group_id or ""),
        "group_id": str(group_id or ""),
        "prize": str(prize or ""),
        "award": str(prize or ""),
        "remaining": str(remaining),
        "stock": str(remaining),
        "draw_count": str(count),
        "count": str(count),
        "time": time.strftime("%Y-%m-%d %H:%M:%S", timestamp),
        "date": time.strftime("%Y-%m-%d", timestamp),
    }

    def replace(match: re.Match[str]) -> str:
        return values.get(match.group(1), match.group(0))

    return _TEMPLATE_TOKEN_RE.sub(replace, str(template or ""))


def draw_lottery(
    group_id: str,
    user_id: object,
    user_name: object = "",
    *,
    now: int | float | None = None,
    random_value: float | None = None,
    campaign_id: object = "",
) -> dict[str, Any]:
    """Atomically draw one prize and append a Windows-local history record."""

    config = select_lottery_campaign(group_id, campaign_id)
    user_id = str(user_id or "").strip()
    if not user_id:
        return {"success": False, "code": "invalid_user", "message": "无法识别抽奖用户。"}
    if not config["enabled"]:
        return {"success": False, "code": "disabled", "message": "抽奖功能未开启。"}
    if not config["awards"]:
        return {"success": False, "code": "no_awards", "message": "当前没有可抽取的奖项。"}

    try:
        current_time = int(time.time() if now is None else now)
    except (TypeError, ValueError, OverflowError):
        current_time = int(time.time())
    current_time = max(0, current_time)
    result: dict[str, Any] = {}

    def claim(raw_runtime: object) -> dict[str, Any]:
        runtime = _normalise_runtime(raw_runtime)
        raw_user = runtime["users"].get(user_id)
        user = dict(raw_user) if isinstance(raw_user, dict) else {}
        previous_count = _bounded_int(
            user.get("draw_count"), 0, 0, MAX_LOTTERY_DRAWS_PER_USER
        )
        last_draw_at = _bounded_int(user.get("last_draw_at"), 0, 0, 2**63 - 1)

        max_draws = config["max_draws_per_user"]
        if max_draws and previous_count >= max_draws:
            result.update(
                success=False,
                code="draw_limit",
                draw_count=previous_count,
                message=f"每人最多可抽 {max_draws} 次，您已达到上限。",
            )
            return runtime

        cooldown = config["cooldown"]
        elapsed = current_time - last_draw_at
        if cooldown and last_draw_at and elapsed < cooldown:
            remaining_seconds = cooldown - max(0, elapsed)
            result.update(
                success=False,
                code="cooldown",
                draw_count=previous_count,
                remaining_seconds=remaining_seconds,
                message=f"抽奖冷却中，请 {remaining_seconds} 秒后再试。",
            )
            return runtime

        eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for award in config["awards"]:
            if not award["enabled"] or award["probability"] <= 0:
                continue
            state = _runtime_award_state(runtime["awards"], award)
            if award["stock"] > 0 and state["remaining"] <= 0:
                continue
            eligible.append((award, state))

        next_count = previous_count + 1
        user.update(
            {
                "draw_count": next_count,
                "last_draw_at": current_time,
                "user_name": str(user_name or user.get("user_name", "") or user_id)[:100],
            }
        )
        runtime["users"][user_id] = user

        selected_award: dict[str, Any] | None = None
        selected_state: dict[str, Any] | None = None
        total_probability = sum(award["probability"] for award, _state in eligible)
        if eligible and total_probability > 0:
            if random_value is None:
                roll_ratio = random.random()
            else:
                roll_ratio = _bounded_float(random_value, 0.0, 0.0, 0.999999999999)
            # A valid config uses 0..100 as literal chance.  The fallback
            # still behaves deterministically if a hand-edited old config
            # exceeds 100%, rather than failing one user's command.
            roll = roll_ratio * (100.0 if total_probability <= 100.0 else total_probability)
            cumulative = 0.0
            for award, state in eligible:
                cumulative += award["probability"]
                if roll < cumulative:
                    selected_award = award
                    selected_state = state
                    break

        if selected_award is not None and selected_state is not None:
            if selected_award["stock"] > 0:
                selected_state["remaining"] = max(0, selected_state["remaining"] - 1)
            remaining: object = (
                selected_state["remaining"] if selected_award["stock"] > 0 else "不限量"
            )
            message = render_lottery_template(
                config["success_message"],
                user_name=user["user_name"],
                user_id=user_id,
                group_id=group_id,
                prize=selected_award["name"],
                remaining=remaining,
                draw_count=next_count,
                now=current_time,
            )
            outcome = "prize"
        else:
            remaining = "-"
            message = render_lottery_template(
                config["empty_message"],
                user_name=user["user_name"],
                user_id=user_id,
                group_id=group_id,
                prize="未中奖",
                remaining=remaining,
                draw_count=next_count,
                now=current_time,
            )
            outcome = "empty"

        runtime["records"].append(
            {
                "timestamp": current_time,
                "user_id": user_id,
                "user_name": user["user_name"],
                "outcome": outcome,
                "award_id": selected_award["id"] if selected_award else "",
                "award_name": selected_award["name"] if selected_award else "",
                "draw_count": next_count,
            }
        )
        _prune_runtime(runtime, MAX_LOTTERY_RECORDS)
        result.update(
            success=True,
            code=outcome,
            message=message,
            draw_count=next_count,
            award=deepcopy(selected_award) if selected_award else None,
            remaining=remaining,
        )
        return runtime

    runtime_key = LOTTERY_RUNTIME_KEY
    # Resolve aliases (campaign name/keyword) to the persisted campaign ID.
    # Unknown selectors intentionally fall back to the legacy root runtime;
    # otherwise an arbitrary selector could create an isolated runtime and
    # bypass the root user's draw limit/cooldown/history.
    clean_campaign_id = _canonical_campaign_id(config, campaign_id)
    if clean_campaign_id:
        runtime_key = f"{LOTTERY_RUNTIME_KEY}:{clean_campaign_id}"[:128]
    update_group_data(group_id, runtime_key, claim)
    return result


def _lottery_runtime_key(campaign_id: object = "") -> str:
    clean = str(campaign_id or "").strip()
    return f"{LOTTERY_RUNTIME_KEY}:{clean}"[:128] if clean else LOTTERY_RUNTIME_KEY


def _canonical_campaign_id(config: object, selector: object = "") -> str:
    """Return a stored campaign ID for a selector, or ``""`` for root mode.

    The Windows command path normally supplies an ID, while API callers and
    older integrations may use a campaign name or keyword.  Runtime state
    must use one canonical key for all of those forms.  Do not return an
    arbitrary selector: an unknown value must use the root runtime rather
    than silently creating a second quota/history bucket.
    """

    wanted = str(selector or "").strip().lstrip("/")
    if not wanted or not isinstance(config, dict):
        return ""
    campaigns = config.get("campaigns", [])
    if not isinstance(campaigns, list):
        return ""
    for campaign in campaigns:
        if not isinstance(campaign, dict):
            continue
        if wanted in {
            str(campaign.get("id", "")),
            str(campaign.get("name", "")),
        } or wanted in split_command_aliases(campaign.get("keyword"), "抽奖"):
            campaign_id = str(campaign.get("id", "")).strip()
            return campaign_id if campaign_id else ""
    return ""


def get_lottery_records(group_id: str, limit: object = 100, campaign_id: object = "") -> list[dict[str, Any]]:
    """Return newest Windows-local draw records, bounded for CMD/web display."""

    normalized_limit = _bounded_int(limit, 100, 1, MAX_LOTTERY_RECORDS)
    config = get_lottery_config(group_id)
    runtime_id = _canonical_campaign_id(config, campaign_id)
    runtime = _normalise_runtime(get_group_data(group_id, _lottery_runtime_key(runtime_id), {}))
    return [deepcopy(item) for item in reversed(runtime["records"][-normalized_limit:])]


def clear_lottery_records(group_id: str, campaign_id: object = "") -> None:
    """Clear draw history while retaining per-user limits and stock state."""

    def clear(raw_runtime: object) -> dict[str, Any]:
        runtime = _normalise_runtime(raw_runtime)
        runtime["records"] = []
        return runtime

    runtime_id = _canonical_campaign_id(get_lottery_config(group_id), campaign_id)
    update_group_data(group_id, _lottery_runtime_key(runtime_id), clear)


def get_lottery_status(group_id: str, campaign_id: object = "") -> dict[str, Any]:
    """Return editable settings enriched with Windows-local remaining stock."""

    config = select_lottery_campaign(group_id, campaign_id)
    runtime_id = _canonical_campaign_id(config, campaign_id)
    runtime = _normalise_runtime(get_group_data(group_id, _lottery_runtime_key(runtime_id), {}))
    awards: list[dict[str, Any]] = []
    for award in config["awards"]:
        state = _runtime_award_state(runtime["awards"], award)
        item = deepcopy(award)
        item["remaining"] = state["remaining"] if award["stock"] > 0 else None
        awards.append(item)
    result = deepcopy(config)
    result["awards"] = awards
    result["record_count"] = len(runtime["records"])
    result["user_count"] = len(runtime["users"])
    return result


def get_lottery_statistics(group_id: str) -> dict[str, Any]:
    """Return a compact local summary without exposing unrelated group data."""

    runtime = _normalise_runtime(get_group_data(group_id, LOTTERY_RUNTIME_KEY, {}))
    records = runtime["records"]
    prize_count = sum(1 for record in records if record.get("outcome") == "prize")
    return {
        "records": len(records),
        "prizes": prize_count,
        "empty": len(records) - prize_count,
        "users": len(runtime["users"]),
    }
