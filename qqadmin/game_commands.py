"""Per-group SCUM game-command templates and SteamID substitution."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any


MAX_GAME_COMMANDS = 50
MAX_TEMPLATE_LENGTH = 512
MAX_TRIGGER_LENGTH = 64
GAME_COMMANDS_DATA_KEY = "game_commands"
# Kept as a public compatibility constant for callers that want to highlight
# the two built-in examples. Custom SCUM verbs are valid as well.
ALLOWED_GAME_COMMANDS = frozenset({"changecurrencybalance", "spawnzombie"})

# Safe starter templates. Plugin WebSocket credentials stay in the server
# settings and are never fabricated by this default set.
DEFAULT_GAME_COMMANDS = (
    {
        "id": "currency",
        "name": "设置金币",
        "trigger": "ChangeCurrencyBalance",
        "template": "#ChangeCurrencyBalance Normal 10000000",
        "operation": "execute_game_command",
        "permission": "member",
        "enabled": True,
    },
    {
        "id": "spawnzombie",
        "name": "生成僵尸",
        "trigger": "spawnzombie",
        "template": "#spawnzombie BP_Zombie_Civilian_Muscular_Male 1",
        "operation": "execute_game_command",
        "permission": "admin",
        "enabled": True,
    },
    {
        "id": "addxiongb",
        "name": "数据库增加熊币",
        "trigger": "addxiongb",
        "template": "#addxiongb 10",
        "operation": "database_credit",
        "currency_column": "amount",
        "permission": "member",
        "enabled": True,
    },
    {
        "id": "redxiongb",
        "name": "数据库扣除熊币",
        "trigger": "redxiongb",
        "template": "#redxiongb 10",
        "operation": "database_debit",
        "currency_column": "amount",
        "permission": "admin",
        "enabled": True,
    },
)


def get_default_game_commands() -> list[dict[str, Any]]:
    """Return independent copies of the built-in starter templates."""
    return deepcopy(list(DEFAULT_GAME_COMMANDS))
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_TRIGGER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_STEAM_ID_RE = re.compile(r"^[0-9]{17}$")
_MULTI_COMMAND_SEPARATOR_RE = re.compile(r"\s*(?:,|，|\|)\s*(?=#)")


def _as_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"", "0", "false", "off", "no"}:
            return False
        if text in {"1", "true", "on", "yes"}:
            return True
    return default if value is None else bool(value)


def _row_from_text(value: object) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def split_game_commands(value: object) -> list[str]:
    """Split one configured value into complete ``#`` command segments.

    The separator is recognized only before another ``#`` command, so a pipe
    or comma that belongs to a future SCUM argument is not split accidentally.
    Newlines remain supported for the existing JSON-line editor.
    """
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    segments: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        segments.extend(
            item.strip()
            for item in _MULTI_COMMAND_SEPARATOR_RE.split(line)
            if item.strip()
        )
    return segments


def _plain_command_row(value: object, index: int) -> dict[str, Any] | None:
    """Turn a directly entered full command into the JSON-line shape."""
    text = str(value or "").strip()
    if not text.startswith("#"):
        return None
    segments = split_game_commands(text)
    first = segments[0] if segments else text
    trigger = first[1:].split(maxsplit=1)[0].strip()
    if not _TRIGGER_RE.fullmatch(trigger):
        return None
    return {
        "id": f"auto-{index + 1}",
        "name": trigger,
        "trigger": trigger,
        "template": text,
        "operation": "execute_game_command",
        "permission": "member",
        "enabled": True,
    }


def normalize_game_commands(value: object) -> list[dict[str, Any]]:
    """Normalize JSON-line/list templates for a single QQ group."""
    if isinstance(value, str):
        rows: list[Any] = [line for line in value.splitlines() if line.strip()]
    elif isinstance(value, (list, tuple)):
        rows = list(value)
    else:
        rows = []

    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_triggers: set[str] = set()
    for index, raw in enumerate(rows[:MAX_GAME_COMMANDS]):
        row = _row_from_text(raw)
        if row is None:
            row = _plain_command_row(raw, index)
        if not row:
            continue
        command_id = str(row.get("id", "") or "").strip()[:48] or f"auto-{index + 1}"
        trigger = str(row.get("trigger", "") or "").strip()
        template = str(row.get("template", row.get("command", "")) or "").strip()
        if not _ID_RE.fullmatch(command_id) or command_id in seen_ids:
            continue
        segments = split_game_commands(template)
        if (
            not segments
            or any(
                len(segment) > MAX_TEMPLATE_LENGTH
                or
                not segment.startswith("#")
                or not _TRIGGER_RE.fullmatch(segment[1:].split(maxsplit=1)[0])
                or len(segment.split()) < 2
                or any(ord(character) < 0x20 and character not in {"\t"} for character in segment)
                for segment in segments
            )
        ):
            continue
        if not trigger:
            trigger = segments[0][1:].split(maxsplit=1)[0].strip()
        if trigger.startswith("#"):
            trigger = trigger[1:]
        if not _TRIGGER_RE.fullmatch(trigger) or trigger.casefold() in seen_triggers:
            continue
        operation = str(row.get("operation", "execute_game_command") or "").strip().lower()
        if operation not in {
            "execute_game_command",
            "game.execute_game_command",
            "database_credit",
            "database_debit",
            "db_credit",
            "db_debit",
        }:
            continue
        permission = str(row.get("permission", "member") or "member").strip().lower()
        if permission not in {"member", "admin", "owner"}:
            permission = "member"
        normalized = {
            "id": command_id,
            "name": str(row.get("name", trigger) or trigger).strip()[:80] or trigger,
            "trigger": trigger,
            "template": template,
            "operation": operation,
            "currency_column": str(
                row.get("currency_column", row.get("currency", "amount"))
                or "amount"
            ).strip().casefold()[:32],
            "permission": permission,
            "enabled": _as_bool(row.get("enabled"), True),
        }
        result.append(normalized)
        seen_ids.add(command_id)
        seen_triggers.add(trigger.casefold())
    return result


def match_game_command(message: object, commands: object) -> dict[str, Any] | None:
    """Match only the first command token; configured arguments stay fixed."""
    text = str(message or "").strip()
    if not text.startswith("#"):
        return None
    token = text.split(maxsplit=1)[0][1:].strip().casefold()
    if not token:
        return None
    for command in normalize_game_commands(commands):
        if command["enabled"] and command["trigger"].casefold() == token:
            return command
    return None


def _resolve_one_game_command(
    template: object,
    steam_id: object,
    *,
    user_id: object = "",
    user_name: object = "",
    group_id: object = "",
    reward: object = "",
) -> str:
    """Render one arbitrary configured SCUM command for one bound player."""
    normalized_steam = str(steam_id or "").strip()
    if not _STEAM_ID_RE.fullmatch(normalized_steam):
        raise ValueError("当前发言人没有有效的本群 SteamID 绑定")
    command = str(template or "").strip()
    if not command.startswith("#") or len(command) > MAX_TEMPLATE_LENGTH:
        raise ValueError("游戏指令模板无效")
    verb = command[1:].split(maxsplit=1)[0].strip()
    if not _TRIGGER_RE.fullmatch(verb):
        raise ValueError("游戏指令名称无效")
    if len(command.split()) < 2:
        raise ValueError("游戏指令必须包含完整参数")
    values = {
        "steam_id": normalized_steam,
        "location": normalized_steam,
        "qq": str(user_id or "").strip(),
        "user_id": str(user_id or "").strip(),
        "name": str(user_name or "").strip(),
        "user_name": str(user_name or "").strip(),
        "group_id": str(group_id or "").strip(),
        "reward": str(reward or "").strip(),
    }
    for key, value in values.items():
        command = command.replace("{" + key + "}", value)

    if "{steam_id}" in command or "{location}" in command:
        raise ValueError("游戏指令包含未替换的 SteamID 占位符")

    parts = command.split()
    steam_token_indexes = [
        index for index, part in enumerate(parts) if _STEAM_ID_RE.fullmatch(part)
    ]
    for index in steam_token_indexes:
        parts[index] = normalized_steam

    if not steam_token_indexes:
        location_index = next(
            (index for index, part in enumerate(parts) if part.casefold() == "location"),
            None,
        )
        if location_index is None:
            if verb.casefold() == "spawnzombie":
                parts.extend(("location", normalized_steam))
            else:
                parts.append(normalized_steam)
        else:
            # ``location`` is the SCUM player-location marker.  Put the
            # bound ID immediately after it, including when the operator
            # configured the marker as the last token.
            if location_index + 1 < len(parts) and _STEAM_ID_RE.fullmatch(parts[location_index + 1]):
                parts[location_index + 1] = normalized_steam
            else:
                parts.insert(location_index + 1, normalized_steam)
    return " ".join(parts)


def resolve_game_commands(
    template: object,
    steam_id: object,
    *,
    user_id: object = "",
    user_name: object = "",
    group_id: object = "",
    reward: object = "",
) -> list[str]:
    """Render one or more comma/pipe-separated commands for one player."""
    segments = split_game_commands(template)
    if not segments:
        raise ValueError("游戏指令模板不能为空")
    return [
        _resolve_one_game_command(
            segment,
            steam_id,
            user_id=user_id,
            user_name=user_name,
            group_id=group_id,
            reward=reward,
        )
        for segment in segments
    ]


def resolve_game_command(
    template: object,
    steam_id: object,
    *,
    user_id: object = "",
    user_name: object = "",
    group_id: object = "",
    reward: object = "",
) -> str:
    """Backward-compatible single-string wrapper around multi-command rendering."""
    return ",".join(
        resolve_game_commands(
            template,
            steam_id,
            user_id=user_id,
            user_name=user_name,
            group_id=group_id,
            reward=reward,
        )
    )


__all__ = [
    "ALLOWED_GAME_COMMANDS",
    "DEFAULT_GAME_COMMANDS",
    "GAME_COMMANDS_DATA_KEY",
    "get_default_game_commands",
    "match_game_command",
    "normalize_game_commands",
    "resolve_game_commands",
    "resolve_game_command",
    "split_game_commands",
]
