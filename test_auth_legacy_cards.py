"""Regression coverage for the legacy no-dash card format."""

from __future__ import annotations

import importlib.util
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parent / "auth" / "manager.py"


def _load_auth_manager():
    """Load auth.manager with the small AstrBot surface it needs stubbed."""
    root_name = "_joker_auth_legacy_test_root"
    package_name = f"{root_name}.auth"
    module_name = f"{package_name}.manager"

    root_package = types.ModuleType(root_name)
    root_package.__path__ = []
    package = types.ModuleType(package_name)
    package.__path__ = []
    astrbot = types.ModuleType("astrbot")
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.logger = logging.getLogger("test_auth_legacy_cards")
    astrbot_core = types.ModuleType("astrbot.core")
    astrbot_core_utils = types.ModuleType("astrbot.core.utils")
    astrbot_path = types.ModuleType("astrbot.core.utils.astrbot_path")
    astrbot_path.get_astrbot_data_path = lambda: str(MODULE_PATH.parent)

    modules = {
        root_name: root_package,
        package_name: package,
        "astrbot": astrbot,
        "astrbot.api": astrbot_api,
        "astrbot.core": astrbot_core,
        "astrbot.core.utils": astrbot_core_utils,
        "astrbot.core.utils.astrbot_path": astrbot_path,
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module


class LegacyCardTests(unittest.TestCase):
    def test_fresh_or_missing_auth_file_is_not_authoritative_for_sync(self):
        """A first Windows run must never look like every group was revoked."""

        manager = _load_auth_manager()
        with tempfile.TemporaryDirectory() as temporary_dir, patch.object(
            manager, "get_plugin_data_path", return_value=Path(temporary_dir)
        ):
            data, complete = manager.load_auth_data_with_health()

        self.assertEqual({}, data["groups"])
        self.assertFalse(complete)

    def test_previously_populated_auth_source_can_reconcile_an_empty_group_set(self):
        """A deliberate final authorization deletion remains synchronizable."""

        manager = _load_auth_manager()
        with tempfile.TemporaryDirectory() as temporary_dir, patch.object(
            manager, "get_plugin_data_path", return_value=Path(temporary_dir)
        ):
            manager.save_auth_data(
                {
                    "groups": {"1102579807": {"expire": -1}},
                    "unused_keys": [],
                    "used_keys": {},
                }
            )
            manager.save_auth_data({"groups": {}, "unused_keys": [], "used_keys": {}})
            data, complete = manager.load_auth_data_with_health()

        self.assertEqual({}, data["groups"])
        self.assertTrue(complete)

    def test_group_card_refuses_an_invalid_portal_group_id(self):
        """Never issue a card that the portal intentionally cannot claim."""
        manager = _load_auth_manager()
        auth_data = {"groups": {}, "unused_keys": [], "used_keys": {}}

        with patch.object(manager, "load_auth_data", return_value=auth_data):
            result = manager.authorize_group_with_card(
                "not-a-qq-group", 30, "test-authorization-secret", "owner"
            )

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(auth_data, {"groups": {}, "unused_keys": [], "used_keys": {}})

    def test_manual_group_grant_creates_a_group_bound_current_card(self):
        manager = _load_auth_manager()
        auth_key = "test-authorization-secret"
        group_id = "1102579807"
        auth_data = {"groups": {}, "unused_keys": [], "used_keys": {}}
        now = 1_800_000_000

        with (
            patch.object(manager, "load_auth_data", return_value=auth_data),
            patch.object(manager, "_atomic_write_json"),
            patch.object(manager.time, "time", return_value=now),
        ):
            result = manager.authorize_group_with_card(
                group_id, 30, auth_key, "2650362143"
            )

        self.assertEqual(result["status"], "authorized")
        self.assertTrue(result["created"])
        card = result["key"]
        self.assertTrue(manager.verify_license_key(card, auth_key, group_id)["valid"])
        self.assertFalse(
            manager.verify_license_key(card, auth_key, "568586484")["valid"]
        )
        self.assertEqual(auth_data["groups"][group_id]["key"], card)
        self.assertEqual(auth_data["used_keys"][card]["group_id"], group_id)
        self.assertEqual(
            auth_data["groups"][group_id]["expire"], now + 30 * 86400
        )

    def test_manual_grant_preserves_an_existing_current_card_on_extension(self):
        manager = _load_auth_manager()
        group_id = "1102579807"
        auth_key = "secret"
        timestamp = 1_700_000_000_000
        card = (
            f"JK30-{timestamp}"
            f"{manager._license_signature(auth_key, timestamp, group_id, 0)}"
        )
        now = 1_800_000_000
        old_expire = now + 30 * 86400
        auth_data = {
            "groups": {
                group_id: {
                    "expire": old_expire,
                    "activated_at": 1_700_000_000,
                    "key": card,
                    "days": 30,
                    "user_id": "2650362143",
                }
            },
            "unused_keys": [],
            "used_keys": {
                card: {
                    "group_id": group_id,
                    "activated_at": 1_700_000_000,
                    "expire": old_expire,
                    "user_id": "2650362143",
                }
            },
        }

        with (
            patch.object(manager, "load_auth_data", return_value=auth_data),
            patch.object(manager, "_atomic_write_json"),
            patch.object(manager.time, "time", return_value=now),
        ):
            result = manager.authorize_group_with_card(group_id, 7, auth_key, "owner")

        self.assertEqual(result["status"], "extended")
        self.assertFalse(result["created"])
        self.assertEqual(result["key"], card)
        self.assertEqual(len(auth_data["used_keys"]), 1)
        self.assertEqual(
            auth_data["groups"][group_id]["expire"], old_expire + 7 * 86400
        )
        self.assertEqual(
            auth_data["used_keys"][card]["expire"], old_expire + 7 * 86400
        )

    def test_malformed_current_card_is_reissued_without_extending_expiry(self):
        manager = _load_auth_manager()
        group_id = "1102579807"
        auth_key = "test-authorization-secret"
        now = 1_800_000_000
        old_expire = now + 12 * 86400
        # It looks like a current card, but is not signed by ``auth_key``.
        malformed = "JK30-1700000000000ABCDEF123456"
        auth_data = {
            "groups": {
                group_id: {
                    "expire": old_expire,
                    "activated_at": 1_700_000_000,
                    "key": malformed,
                    "days": 30,
                    "user_id": "customer",
                }
            },
            "unused_keys": [],
            "used_keys": {
                malformed: {
                    "group_id": group_id,
                    "activated_at": 1_700_000_000,
                    "expire": old_expire,
                    "user_id": "customer",
                }
            },
        }

        with (
            patch.object(manager, "load_auth_data", return_value=auth_data),
            patch.object(manager, "_atomic_write_json"),
            patch.object(manager.time, "time", return_value=now),
        ):
            result = manager.ensure_group_authorization_card(
                group_id, auth_key, "owner"
            )

        self.assertEqual(result["status"], "issued")
        self.assertTrue(result["created"])
        self.assertEqual(result["expire"], old_expire)
        self.assertNotIn(malformed, auth_data["used_keys"])
        self.assertTrue(
            manager.verify_license_key(result["key"], auth_key, group_id)["valid"]
        )
        self.assertEqual(auth_data["groups"][group_id]["key"], result["key"])

    def test_reissue_for_old_manual_grant_does_not_extend_its_expiry(self):
        manager = _load_auth_manager()
        auth_key = "test-authorization-secret"
        group_id = "1102579807"
        now = 1_800_000_000
        old_expire = now + 12 * 86400
        auth_data = {
            "groups": {
                group_id: {
                    "expire": old_expire,
                    "activated_at": 1_700_000_000,
                    "key": "MANUAL_1102579807_1700000000",
                    "days": 30,
                    "user_id": "owner",
                }
            },
            "unused_keys": [],
            "used_keys": {},
        }

        with (
            patch.object(manager, "load_auth_data", return_value=auth_data),
            patch.object(manager, "_atomic_write_json"),
            patch.object(manager.time, "time", return_value=now),
        ):
            result = manager.ensure_group_authorization_card(
                group_id, auth_key, "2650362143"
            )

        self.assertEqual(result["status"], "issued")
        self.assertTrue(result["created"])
        card = result["key"]
        self.assertTrue(manager.verify_license_key(card, auth_key, group_id)["valid"])
        self.assertEqual(auth_data["groups"][group_id]["expire"], old_expire)
        self.assertEqual(auth_data["used_keys"][card]["expire"], old_expire)

        with (
            patch.object(manager, "load_auth_data", return_value=auth_data),
            patch.object(manager, "_atomic_write_json"),
        ):
            revoked_groups = manager.delete_used_keys()
        self.assertEqual(revoked_groups, [group_id])
        self.assertNotIn(group_id, auth_data["groups"])

    def test_manual_permanent_grant_is_not_downgraded_when_adding_days(self):
        manager = _load_auth_manager()
        group_id = "1102579807"
        auth_data = {
            "groups": {
                group_id: {
                    "expire": -1,
                    "activated_at": 1_700_000_000,
                    "key": "MANUAL_1102579807_1700000000",
                    "days": -1,
                    "user_id": "owner",
                }
            },
            "unused_keys": [],
            "used_keys": {},
        }

        with (
            patch.object(manager, "load_auth_data", return_value=auth_data),
            patch.object(manager, "_atomic_write_json"),
            patch.object(manager.time, "time", return_value=1_800_000_000),
        ):
            result = manager.authorize_group_with_card(group_id, 30, "secret", "owner")

        self.assertEqual(result["status"], "already_permanent")
        self.assertTrue(result["created"])
        self.assertEqual(result["expire"], -1)
        self.assertTrue(result["key"].startswith("JK-1-"))
        self.assertEqual(auth_data["groups"][group_id]["expire"], -1)

    def test_time_adjustment_keeps_current_card_expiry_in_sync(self):
        manager = _load_auth_manager()
        group_id = "1102579807"
        card = "JK30-1700000000000ABCDEF123456"
        now = 1_800_000_000
        old_expire = now + 10 * 86400
        auth_data = {
            "groups": {
                group_id: {
                    "expire": old_expire,
                    "activated_at": now,
                    "key": card,
                    "days": 10,
                    "user_id": "owner",
                }
            },
            "unused_keys": [],
            "used_keys": {
                card: {
                    "group_id": group_id,
                    "activated_at": now,
                    "expire": old_expire,
                    "user_id": "owner",
                }
            },
        }

        with (
            patch.object(manager, "load_auth_data", return_value=auth_data),
            patch.object(manager, "_atomic_write_json"),
            patch.object(manager.time, "time", return_value=now),
        ):
            self.assertTrue(manager.add_auth_time(group_id, 7))
            self.assertEqual(
                auth_data["used_keys"][card]["expire"], old_expire + 7 * 86400
            )
            self.assertTrue(manager.reduce_auth_time(group_id, 3))
        self.assertEqual(
            auth_data["used_keys"][card]["expire"], old_expire + 4 * 86400
        )

    def test_legacy_manual_authorize_helper_does_not_erase_current_card(self):
        manager = _load_auth_manager()
        group_id = "1102579807"
        card = "JK30-1700000000000ABCDEF123456"
        now = 1_800_000_000
        old_expire = now + 10 * 86400
        auth_data = {
            "groups": {
                group_id: {
                    "expire": old_expire,
                    "activated_at": 1_700_000_000,
                    "key": card,
                    "days": 10,
                    "user_id": "customer",
                }
            },
            "unused_keys": [],
            "used_keys": {
                card: {
                    "group_id": group_id,
                    "activated_at": 1_700_000_000,
                    "expire": old_expire,
                    "user_id": "customer",
                }
            },
        }

        with (
            patch.object(manager, "load_auth_data", return_value=auth_data),
            patch.object(manager, "_atomic_write_json"),
            patch.object(manager.time, "time", return_value=now),
        ):
            self.assertTrue(manager.authorize_group(group_id, 7, "owner"))

        self.assertEqual(auth_data["groups"][group_id]["key"], card)
        self.assertEqual(auth_data["groups"][group_id]["user_id"], "customer")
        self.assertEqual(
            auth_data["used_keys"][card]["expire"], old_expire + 7 * 86400
        )

    def test_legacy_card_keeps_its_day_count_when_activated(self):
        manager = _load_auth_manager()
        auth_key = "test-authorization-secret"
        timestamp = 1_700_000_000_000
        days = 30
        card = (
            f"JK{days}{timestamp}"
            f"{manager._license_signature(auth_key, timestamp, '', 0)}"
        )
        self.assertEqual(manager.verify_license_key(card, auth_key)["days"], days)

        auth_data = {"groups": {}, "unused_keys": [card], "used_keys": {}}
        now = 1_800_000_000
        with (
            patch.object(manager, "load_auth_data", return_value=auth_data),
            patch.object(manager, "save_auth_data"),
            patch.object(manager.time, "time", return_value=now),
        ):
            self.assertTrue(manager.mark_key_used(card, "1102579807", "2650362143"))

        group_auth = auth_data["groups"]["1102579807"]
        self.assertEqual(group_auth["days"], days)
        self.assertEqual(group_auth["expire"], now + days * 86400)

    def test_deleted_but_otherwise_valid_card_cannot_be_activated(self):
        manager = _load_auth_manager()
        auth_key = "test-authorization-secret"
        timestamp = 1_700_000_000_000
        card = f"JK30-{timestamp}{manager._license_signature(auth_key, timestamp, '', 0)}"
        auth_data = {"groups": {}, "unused_keys": [], "used_keys": {}}

        with patch.object(manager, "load_auth_data", return_value=auth_data):
            result = manager.activate_unused_license_key(
                card, "1102579807", "2650362143", 30
            )

        self.assertEqual(result["status"], "deleted")
        self.assertEqual(auth_data["groups"], {})
        self.assertEqual(auth_data["used_keys"], {})

    def test_atomic_activation_consumes_card_only_once(self):
        manager = _load_auth_manager()
        auth_key = "test-authorization-secret"
        timestamp = 1_700_000_000_000
        card = f"JK30-{timestamp}{manager._license_signature(auth_key, timestamp, '', 0)}"
        auth_data = {"groups": {}, "unused_keys": [card], "used_keys": {}}
        now = 1_800_000_000

        with (
            patch.object(manager, "load_auth_data", return_value=auth_data),
            patch.object(manager, "_atomic_write_json"),
            patch.object(manager.time, "time", return_value=now),
        ):
            first = manager.activate_unused_license_key(
                card, "1102579807", "2650362143", 30
            )
            second = manager.activate_unused_license_key(
                card, "568586484", "2650362143", 30
            )

        self.assertEqual(first["status"], "activated")
        self.assertEqual(first["expire"], now + 30 * 86400)
        self.assertEqual(second["status"], "used")
        self.assertEqual(auth_data["used_keys"][card]["group_id"], "1102579807")
        self.assertNotIn(card, auth_data["unused_keys"])


if __name__ == "__main__":
    unittest.main()
