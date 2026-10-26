"""Focused regression tests for per-group sub-admin permission handling."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _load_sub_admin_module():
    """Load the persistence helper with a tiny in-memory group-config stub."""
    package_name = "_sub_admin_permission_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT / "qqadmin")]
    sys.modules[package_name] = package

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
    sys.modules[group_config.__name__] = group_config

    module_path = ROOT / "qqadmin" / "sub_admin.py"
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.sub_admin", module_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, storage


class TestSubAdminPermissions(unittest.TestCase):
    def setUp(self):
        self.sub_admin, self.storage = _load_sub_admin_module()

    def test_legacy_and_current_records_have_scoped_permissions(self):
        self.storage["100"] = {
            "sub_admins": [
                "10001",  # legacy config: all standard permissions
                {"user_id": 10002, "permissions": ["kick"]},
            ]
        }

        # Legacy plain-ID entries are upgraded to the least-privilege
        # reply-management role.  A normal member may maintain replies, but
        # must not gain moderation actions such as mute/kick/blacklist.
        self.assertTrue(self.sub_admin.has_permission("100", "10001", "reply"))
        self.assertFalse(self.sub_admin.has_permission("100", "10001", "mute"))
        self.assertTrue(self.sub_admin.has_permission("100", "10002", "kick"))
        self.assertFalse(self.sub_admin.has_permission("100", "10002", "mute"))
        self.assertFalse(self.sub_admin.has_permission("100", "99999", "kick"))
        self.assertEqual(
            "10001", self.sub_admin.get_sub_admins("100")[0]["user_id"]
        )

    def test_permission_update_migrates_legacy_record_without_duplication(self):
        self.storage["100"] = {"sub_admins": ["10001"]}

        self.assertTrue(
            self.sub_admin.set_sub_admin_permissions("100", "10001", ["recall"])
        )
        stored = self.storage["100"]["sub_admins"]
        self.assertEqual("10001", stored[0]["user_id"])
        self.assertEqual(["recall"], stored[0]["permissions"])
        self.assertTrue(self.sub_admin.has_permission("100", "10001", "recall"))
        self.assertFalse(self.sub_admin.has_permission("100", "10001", "mute"))

    def test_runtime_wiring_is_scoped_and_requires_feature_toggle(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")

        self.assertNotIn("sender_id_str in [str(a) for a in sub_admins]", source)
        self.assertIn('is_feature_enabled(group_id, "sub_admin", self.config)', source)
        self.assertIn('set_feature_enabled(group_id, "sub_admin", True, self.config)', source)
        self.assertIn('set_feature_enabled(group_id, "sub_admin", False, self.config)', source)
        self.assertIn('required_sub_admin_permission in {"reply", "mute", "kick", "recall", "blacklist"}', source)
        for permission in ("reply", "mute", "kick", "recall", "blacklist"):
            self.assertIn(
                f'required_sub_admin_permission="{permission}"', source,
                permission,
            )


if __name__ == "__main__":
    unittest.main()
