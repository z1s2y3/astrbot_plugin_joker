"""One execution boundary for game commands and Steam rewards.

The Windows plugin can execute locally through the documented game plugin
WebSocket, or ask the portal to route a call through the authenticated control
WebSocket. Keeping authorization, transport selection, exception mapping, and
result normalization here makes both paths return the same contract to the QQ
command layer.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


MAX_RESULT_TEXT = 512
SUCCESS_CODES = frozenset({"ok", "success", "executed", "plugin_websocket_connected"})
@dataclass(frozen=True)
class GameExecutionRequest:
    """Bound execution identity; callers cannot replace the SteamID later."""

    group_id: str
    profile_id: str = ""
    user_id: str = ""
    steam_id: str = ""
    operation: str = "execute_game_command"

    @classmethod
    def from_values(
        cls,
        *,
        group_id: object,
        profile_id: object = "",
        user_id: object = "",
        steam_id: object = "",
        operation: object = "execute_game_command",
    ) -> "GameExecutionRequest":
        return cls(
            group_id=str(group_id or "").strip()[:32],
            profile_id=str(profile_id or "").strip()[:48],
            user_id=str(user_id or "").strip()[:32],
            steam_id=str(steam_id or "").strip()[:32],
            operation=str(operation or "execute_game_command").strip().lower()[:64],
        )

    def payload(self, command: str) -> dict[str, str]:
        return {
            "command": str(command or "").strip()[:512],
            "group_id": self.group_id,
            "profile_id": self.profile_id,
            "user_id": self.user_id,
            "steam_id": self.steam_id,
        }


def _bounded_text(value: object, limit: int = MAX_RESULT_TEXT) -> str:
    return str(value or "").strip()[:limit]


def normalize_game_result(
    raw: object,
    *,
    request: GameExecutionRequest,
    transport: str,
) -> dict[str, Any]:
    """Return one bounded result contract for local and remote execution."""
    if isinstance(raw, Mapping):
        result = dict(raw)
        if "success" in result:
            # Only a real boolean ``True`` confirms that the external game
            # operation completed.  Truthy strings such as "false" are not
            # valid acknowledgements.
            success = result.get("success") is True
        elif "ok" in result:
            # The WebSocket protocol may use ``ok`` for its transport-level
            # acknowledgement, but it must still be an actual boolean.
            success = result.get("ok") is True
        else:
            success = False
            code = str(result.get("code", "") or "").strip().lower()
            if not code or code in SUCCESS_CODES:
                result["code"] = "invalid_executor_response"
                result.setdefault("message", "游戏执行端未返回可确认的执行结果。")
    else:
        result = {
            "result": raw,
            "success": False,
            "code": "invalid_executor_response",
            "message": "游戏执行端未返回可确认的执行结果。",
        }
        success = False

    result["success"] = bool(success)
    result["code"] = _bounded_text(
        result.get("code", "executed" if success else "game_execution_failed"),
        64,
    ) or ("executed" if success else "game_execution_failed")
    if "message" in result:
        result["message"] = _bounded_text(result.get("message"))
    if "detail" in result:
        result["detail"] = _bounded_text(result.get("detail"))
    result["transport"] = _bounded_text(result.get("transport", transport), 24)
    result["operation"] = request.operation
    result["group_id"] = request.group_id
    if request.profile_id:
        result["profile_id"] = request.profile_id
    result["user_id"] = request.user_id
    if request.steam_id:
        result["steam_id"] = request.steam_id
    return result


def authorization_failure(state: object) -> dict[str, Any] | None:
    normalized = str(state or "missing").strip().lower()
    if normalized == "active":
        return None
    if normalized == "expired":
        return {
            "success": False,
            "code": "game_license_expired",
            "message": "游戏授权已过期，请在保留期内续期。",
        }
    return {
        "success": False,
        "code": "game_license_required",
        "message": "该群未生成有效游戏授权，无法执行游戏功能。",
    }


class GameExecutionService:
    """Select one transport and normalize its outcome."""

    def __init__(
        self,
        *,
        authorization_state: Callable[[str], object],
        websocket_client: Any = None,
        local_executor: Callable[..., Any] | None = None,
        local_settings: object = None,
        profiles: object = None,
    ) -> None:
        self.authorization_state = authorization_state
        self.websocket_client = websocket_client
        self.local_executor = local_executor
        self.local_settings = local_settings
        self.profiles = profiles

    async def execute(
        self,
        command: str,
        request: GameExecutionRequest,
        *,
        prefer_websocket: bool = False,
    ) -> dict[str, Any]:
        try:
            failure = authorization_failure(self.authorization_state(request.group_id))
        except Exception:
            return normalize_game_result(
                {"success": False, "code": "game_authorization_unavailable"},
                request=request,
                transport="authorization",
            )
        if failure is not None:
            return normalize_game_result(
                failure, request=request, transport="authorization"
            )

        if prefer_websocket:
            return await self._execute_websocket(command, request)
        return await self._execute_local(command, request)

    async def _execute_websocket(
        self, command: str, request: GameExecutionRequest
    ) -> dict[str, Any]:
        client = self.websocket_client
        if client is None or not bool(getattr(client, "enabled", False)):
            return normalize_game_result(
                {"success": False, "code": "websocket_unavailable"},
                request=request,
                transport="websocket",
            )
        try:
            result = await client.call_via_portal(
                request.operation,
                request.payload(command),
                timeout=getattr(client, "call_timeout", 20.0),
            )
        except TimeoutError:
            result = {
                "success": False,
                "code": "websocket_timeout",
                "message": "游戏 WebSocket 响应超时，无法确认指令是否完成。",
            }
        except Exception as exc:
            result = {
                "success": False,
                "code": "websocket_call_failed",
                "message": (
                    "游戏 WebSocket 调用失败（"
                    f"{type(exc).__name__}），指令未执行。"
                ),
            }
        return normalize_game_result(result, request=request, transport="websocket")

    async def _execute_local(
        self, command: str, request: GameExecutionRequest
    ) -> dict[str, Any]:
        try:
            if callable(self.local_executor):
                executor_kwargs = {
                    "group_id": request.group_id,
                    "user_id": request.user_id,
                    "steam_id": request.steam_id,
                    "operation": request.operation,
                }
                # Keep old third-party adapters compatible while providing
                # the route identity to the built-in multi-server adapter.
                if request.profile_id:
                    executor_kwargs["profile_id"] = request.profile_id
                result = self.local_executor(command, **executor_kwargs)
                if inspect.isawaitable(result):
                    result = await result
            else:
                from .game_executor import execute_configured_game_command

                result = await execute_configured_game_command(
                    self.local_settings,
                    command,
                    profiles=self.profiles,
                    group_id=request.group_id,
                    profile_id=request.profile_id,
                    user_id=request.user_id,
                    steam_id=request.steam_id,
                    operation=request.operation,
                )
        except TimeoutError:
            result = {
                "success": False,
                "code": "executor_timeout",
                "message": "游戏执行器响应超时，无法确认指令是否完成。",
            }
        except Exception as exc:
            result = {
                "success": False,
                "code": "executor_failed",
                "message": (
                    "游戏执行器执行失败（"
                    f"{type(exc).__name__}），指令未执行。"
                ),
            }
        return normalize_game_result(result, request=request, transport="local")


def format_game_execution_reply(result: object) -> str:
    """Render a short user-facing result without exposing transport details."""
    payload = result if isinstance(result, Mapping) else {}
    if payload.get("success") is True:
        response = _bounded_text(payload.get("message"), 300)
        if response:
            return f"游戏指令执行成功：{response}"
        output = _bounded_text(payload.get("result"), 300)
        if output:
            return f"游戏指令执行成功：{output}"
        return "游戏指令执行成功。"
    code = _bounded_text(payload.get("code", "game_execution_failed"), 64)
    defaults = {
        "game_license_required": "请先完成游戏授权。",
        "game_license_expired": "游戏授权已过期。",
        "game_authorization_unavailable": "暂时无法读取游戏授权。",
        "websocket_unavailable": "游戏执行端未连接。",
        "websocket_timeout": "游戏执行超时。",
        "websocket_transport_failed": "游戏连接失败。",
        "invalid_timeout": "游戏操作超时设置无效。",
        "websocket_call_failed": "游戏指令执行失败。",
        "client_offline": "游戏执行端未连接。",
        "executor_unavailable": "游戏执行端未启用。",
        "executor_timeout": "游戏执行超时。",
        "executor_failed": "游戏指令执行失败。",
        "plugin_websocket_config_invalid": "游戏 WebSocket 配置无效。",
        "plugin_websocket_key_missing": "游戏 WebSocket 未配置。",
        "plugin_websocket_connect_failed": "游戏 WebSocket 连接失败。",
        "plugin_websocket_timeout": "游戏执行超时。",
        "plugin_websocket_request_failed": "游戏指令执行失败。",
        "plugin_websocket_closed": "游戏连接已断开。",
        "game_command_invalid": "游戏指令格式错误。",
        "request_timeout": "游戏执行超时。",
        "invalid_executor_response": "游戏指令执行失败。",
        "steam_operation_unconfirmed": "游戏奖励未确认。",
    }
    return defaults.get(code, "游戏指令执行失败，未执行。")


__all__ = [
    "GameExecutionRequest",
    "GameExecutionService",
    "authorization_failure",
    "format_game_execution_reply",
    "normalize_game_result",
]
