"""Regression coverage for the per-group automatic blacklist-kick quota."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent


def _load_blacklist_module():
    package_name = "_blacklist_daily_limit_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT / "qqadmin")]
    storage: dict[str, dict] = {}
    group_config = types.ModuleType(f"{package_name}.group_config")

    def get_group_data(group_id, key, default=None):
        return storage.get(str(group_id), {}).get(key, default)

    def set_group_data(group_id, key, value):
        storage.setdefault(str(group_id), {})[key] = value

    def update_group_data(group_id, key, updater):
        group = storage.setdefault(str(group_id), {})
        group[key] = updater(group.get(key))
        return group[key]

    group_config.get_group_data = get_group_data
    group_config.set_group_data = set_group_data
    group_config.update_group_data = update_group_data
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(error=lambda *a, **k: None)
    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    utils = types.ModuleType("astrbot.core.utils")
    utils.__path__ = []
    path_module = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_module.get_astrbot_data_path = lambda: str(ROOT)
    path = ROOT / "qqadmin" / "blacklist.py"
    spec = importlib.util.spec_from_file_location(f"{package_name}.blacklist", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    modules = {
        package_name: package,
        group_config.__name__: group_config,
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.core": core,
        "astrbot.core.utils": utils,
        "astrbot.core.utils.astrbot_path": path_module,
        spec.name: module,
    }
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module, storage


class BlacklistDailyLimitTests(unittest.TestCase):
    def test_zero_disables_and_limit_rolls_over_by_day(self):
        module, storage = _load_blacklist_module()
        group_id = "1102579807"
        storage[group_id] = {
            "blacklist_settings": {"max_auto_kick_per_day": 2}
        }
        day = 1_700_000_000
        self.assertTrue(module.can_auto_kick_today(group_id, day))
        self.assertEqual(1, module.record_auto_kick(group_id, "1", day))
        self.assertTrue(module.can_auto_kick_today(group_id, day))
        self.assertEqual(2, module.record_auto_kick(group_id, "2", day))
        self.assertFalse(module.can_auto_kick_today(group_id, day))
        self.assertTrue(module.can_auto_kick_today(group_id, day + 86_400))

        storage[group_id]["blacklist_settings"]["max_auto_kick_per_day"] = 0
        self.assertFalse(module.can_auto_kick_today(group_id, day + 86_400))

    def test_delete_current_group_record_is_physical(self):
        module, storage = _load_blacklist_module()
        group_id = "1102579807"
        storage[group_id] = {
            "blacklist": {
                "1668363852": {
                    "is_active": False,
                    "reason": "test",
                }
            }
        }
        self.assertTrue(module.delete_blacklist_record(group_id, "1668363852"))
        self.assertEqual({}, storage[group_id]["blacklist"])
        self.assertFalse(module.delete_blacklist_record(group_id, "1668363852"))

    def test_global_allow_is_scoped_to_current_group(self):
        module, _storage = _load_blacklist_module()
        with patch.object(module, "load_global_blacklist", return_value={
            "1668363852": {
                "is_active": True,
                "allowed_groups": [],
            }
        }) as load, patch.object(module, "save_global_blacklist") as save:
            self.assertTrue(
                module.allow_global_blacklist_in_group(
                    "1668363852", "1102579807", "web", "operator"
                )
            )
            saved = save.call_args.args[0]["1668363852"]
            self.assertEqual(["1102579807"], saved["allowed_groups"])
            with patch.object(module, "load_global_blacklist", return_value=save.call_args.args[0]):
                self.assertFalse(module.is_in_global_blacklist("1668363852", group_id="1102579807"))
                self.assertTrue(module.is_in_global_blacklist("1668363852", group_id="513920261"))


if __name__ == "__main__":
    unittest.main()
