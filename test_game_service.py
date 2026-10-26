"""Tests for the shared game execution boundary."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType


def _load_module():
    root = Path(__file__).resolve().parent
    package_name = "_game_service_test_package"
    package = ModuleType(package_name)
    package.__path__ = [str(root)]
    sys.modules[package_name] = package
    child = ModuleType(f"{package_name}.qqadmin")
    child.__path__ = [str(root / "qqadmin")]
    sys.modules[child.__name__] = child
    name = f"{package_name}.qqadmin.game_service"
    spec = importlib.util.spec_from_file_location(name, root / "qqadmin" / "game_service.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


game_service = _load_module()


class GameExecutionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorization_gate_runs_before_transport(self):
        calls = []

        async def executor(*args, **kwargs):
            calls.append((args, kwargs))
            return {"success": True, "code": "executed"}

        service = game_service.GameExecutionService(
            authorization_state=lambda _group: "expired",
            local_executor=executor,
        )
        request = game_service.GameExecutionRequest.from_values(
            group_id="766637236", user_id="42", steam_id="76561199616499552"
        )
        result = await service.execute("#test", request)

        self.assertFalse(result["success"])
        self.assertEqual("game_license_expired", result["code"])
        self.assertEqual([], calls)

    async def test_local_and_websocket_paths_share_result_contract(self):
        async def executor(command, **kwargs):
            return {"success": True, "code": "executed", "result": command}

        class Client:
            enabled = True
            call_timeout = 4

            async def call_via_portal(self, operation, payload, *, timeout):
                return {"success": True, "code": "executed", "result": payload["command"]}

        request = game_service.GameExecutionRequest.from_values(
            group_id="766637236",
            user_id="42",
            steam_id="76561199616499552",
            operation="steam_reward",
        )
        local = game_service.GameExecutionService(
            authorization_state=lambda _group: "active",
            local_executor=executor,
        )
        remote = game_service.GameExecutionService(
            authorization_state=lambda _group: "active",
            websocket_client=Client(),
        )

        local_result = await local.execute("#local", request)
        remote_result = await remote.execute("#remote", request, prefer_websocket=True)

        self.assertTrue(local_result["success"])
        self.assertEqual("local", local_result["transport"])
        self.assertEqual("steam_reward", local_result["operation"])
        self.assertTrue(remote_result["success"])
        self.assertEqual("websocket", remote_result["transport"])
        self.assertEqual("#remote", remote_result["result"])

    async def test_websocket_exception_is_safe_failure(self):
        class Client:
            enabled = True
            call_timeout = 4

            async def call_via_portal(self, *_args, **_kwargs):
                raise TimeoutError()

        service = game_service.GameExecutionService(
            authorization_state=lambda _group: "active",
            websocket_client=Client(),
        )
        request = game_service.GameExecutionRequest.from_values(group_id="766637236")
        result = await service.execute("#test", request, prefer_websocket=True)

        self.assertFalse(result["success"])
        self.assertEqual("websocket_timeout", result["code"])
        self.assertIn("无法确认", result["message"])

    async def test_unconfirmed_executor_result_cannot_be_success(self):
        async def executor(_command, **_kwargs):
            return {"code": "executed"}

        service = game_service.GameExecutionService(
            authorization_state=lambda _group: "active",
            local_executor=executor,
        )
        request = game_service.GameExecutionRequest.from_values(
            group_id="766637236", operation="steam_reward"
        )
        result = await service.execute("#reward", request)

        self.assertFalse(result["success"])
        self.assertEqual("invalid_executor_response", result["code"])

    async def test_bare_executor_value_is_rejected(self):
        async def executor(_command, **_kwargs):
            return True

        service = game_service.GameExecutionService(
            authorization_state=lambda _group: "active",
            local_executor=executor,
        )
        request = game_service.GameExecutionRequest.from_values(group_id="766637236")
        result = await service.execute("#reward", request)

        self.assertFalse(result["success"])
        self.assertEqual("invalid_executor_response", result["code"])
        self.assertIn("未返回可确认", result["message"])

    def test_reply_requires_confirmed_success(self):
        self.assertIn("未执行", game_service.format_game_execution_reply({}))
        self.assertIn(
            "成功",
            game_service.format_game_execution_reply(
                {"success": True, "code": "executed"}
            ),
        )


if __name__ == "__main__":
    unittest.main()
