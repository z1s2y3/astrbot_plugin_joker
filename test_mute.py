"""Focused tests for the Windows mute state and API result handling."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
PACKAGE = "_joker_mute_test"


def _load_module():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / "qqadmin")]
    sys.modules[PACKAGE] = package
    storage: dict[tuple[str, str], object] = {}
    group_config = types.ModuleType(f"{PACKAGE}.group_config")

    def get(group, key, default=None):
        value = storage.get((str(group), key), default)
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, list):
            return list(value)
        return value

    def set_value(group, key, value):
        storage[(str(group), key)] = value

    def update(group, key, updater):
        value = updater(get(group, key))
        set_value(group, key, value)
        return value

    group_config.get_group_data = get
    group_config.set_group_data = set_value
    group_config.update_group_data = update
    api_module = types.ModuleType(f"{PACKAGE}.api_client")

    class FakeAPI:
        async def set_group_ban(self, *_args, **_kwargs):
            return {"success": True}

        async def set_group_whole_ban(self, *_args, **_kwargs):
            return {"status": "ok"}

    api_module.api_client = FakeAPI()
    sys.modules[group_config.__name__] = group_config
    sys.modules[api_module.__name__] = api_module
    path = ROOT / "qqadmin" / "mute.py"
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.mute", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, storage, api_module


class MuteTests(unittest.TestCase):
    def setUp(self):
        self.mute, self.storage, self.api_module = _load_module()

    def test_unmute_reports_missing_state_and_normalizes_ids(self):
        self.assertFalse(self.mute.unmute_user("100", "  "))
        self.mute.mute_user("100", 123, 60)
        self.assertTrue(self.mute.is_muted("100", "123"))
        self.assertTrue(self.mute.unmute_user("100", "123"))
        self.assertFalse(self.mute.unmute_user("100", "123"))

    def test_auto_mute_uses_escalated_level_duration(self):
        self.mute.set_mute_settings("100", "level_durations", [10, 20])
        self.mute.set_mute_settings("100", "repeat_threshold", 1)
        self.mute.mute_user_auto("100", "u1")
        first = self.mute.get_mute_info("100", "u1")
        self.assertEqual(first["duration"], 10)
        self.mute.unmute_user("100", "u1")
        self.mute.mute_user_auto("100", "u1")
        second = self.mute.get_mute_info("100", "u1")
        self.assertEqual(second["duration"], 20)
        self.assertEqual(second["mute_level"], 2)

    def test_api_failures_do_not_write_local_state(self):
        class BrokenAPI:
            async def set_group_ban(self, *_args, **_kwargs):
                raise RuntimeError("offline")

            async def set_group_whole_ban(self, *_args, **_kwargs):
                return {"success": False}

        with patch.object(self.mute, "api_client", BrokenAPI()):
            self.assertFalse(asyncio.run(self.mute.mute_user_api("100", "u1", 30)))
            self.assertFalse(asyncio.run(self.mute.unmute_user_api("100", "u1")))
            self.assertFalse(asyncio.run(self.mute.set_group_whole_ban_api("100", True)))
        self.assertEqual({}, self.storage.get(("100", "mute_list"), {}))


if __name__ == "__main__":
    unittest.main()
