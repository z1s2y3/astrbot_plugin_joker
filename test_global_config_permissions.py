import unittest

from web_client.feature_schema import FEATURE_BY_KEY
from web_client.global_config import GENERAL_FIELDS


class GlobalConfigPermissionTests(unittest.TestCase):
    def test_general_fields_include_permission_modes(self):
        keys = {field.spec.key for field in GENERAL_FIELDS}
        self.assertTrue(
            {"auth_permission_mode", "stats_use_permission", "stats_modify_permission", "scum_permission_mode"}.issubset(keys)
        )

    def test_scum_feature_includes_permission_mode(self):
        scum_fields = {field.key for field in FEATURE_BY_KEY["scum"].fields}
        self.assertIn("permission_mode", scum_fields)

    def test_audit_feature_includes_failure_kick_switch(self):
        audit_fields = {field.key: field for field in FEATURE_BY_KEY["audit"].fields}
        self.assertIn("kick_on_verification_failure", audit_fields)
        self.assertFalse(audit_fields["kick_on_verification_failure"].default)


if __name__ == "__main__":
    unittest.main()
