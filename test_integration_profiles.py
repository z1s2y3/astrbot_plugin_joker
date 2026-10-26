import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from web_client.database import Base
from web_client.integration_service import (
    DEFAULT_PROFILE_ID,
    delete_ftp_connection,
    delete_integration_profile,
    get_active_integration_profile,
    get_ftp_connection_view,
    get_integration_config_view,
    get_saved_ftp_connection_config,
    get_windows_database_profile_projection,
    list_ftp_connections,
    list_integration_profiles,
    migrate_legacy_group_ftp_connections,
    normalize_integration_profile_id,
    save_ftp_connection,
    save_ftp_database_selection,
    save_integration_config,
    save_server_database_binding,
    set_integration_profile_enabled,
    set_active_integration_profile,
    test_integration_connection,
    upsert_integration_profile,
)
from web_client.models import PortalSetting
from web_client.services import PortalError
from web_client.settings import Settings


class IntegrationProfileTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="profile-session-secret-32-characters-minimum",
            card_pepper="profile-card-pepper-32-characters-minimum",
            secure_cookies=False,
            portal_name="Test",
            session_max_age=3600,
        )

    def tearDown(self):
        self.engine.dispose()

    @staticmethod
    def _form(name: str) -> dict[str, str]:
        return {
            "ftp_enabled": "on",
            "ftp_host": f"ftp-{name}.example.com",
            "ftp_port": "21",
            "ftp_username": f"ftp-{name}",
            "ftp_password": f"ftp-secret-{name}",
            "ftp_remote_path": "/incoming",
            "ftp_tls": "off",
            "ftp_passive": "on",
            "database_enabled": "on",
            "database_engine": "mysql",
            "database_host": f"db-{name}.example.com",
            "database_port": "3306",
            "database_name": f"business_{name}",
            "database_username": f"db-{name}",
            "database_password": f"db-secret-{name}",
            "database_charset": "utf8mb4",
        }

    def test_legacy_default_survives_scoped_profile_writes(self):
        with Session(self.engine) as db:
            save_integration_config(
                db, self.settings, self._form("legacy"), operator_id=7, owner_id=7
            )
            upsert_integration_profile(
                db,
                self.settings,
                self._form("group"),
                operator_id=7,
                owner_id=7,
                group_id="10001",
                profile_id="main",
            )
            db.commit()

            payload = db.get(
                PortalSetting, "external_integrations:user:7"
            ).value_json
            self.assertEqual(payload["ftp"]["host"], "ftp-legacy.example.com")
            self.assertEqual(
                payload["profiles"]["10001"]["main"]["ftp"]["host"],
                "ftp-group.example.com",
            )
            self.assertEqual(
                get_integration_config_view(db, 7)["ftp"]["host"],
                "ftp-legacy.example.com",
            )
            self.assertEqual(
                get_integration_config_view(
                    db, 7, group_id="10001", profile_id="main"
                )["ftp"]["host"],
                "ftp-group.example.com",
            )

    def test_legacy_top_level_connection_never_leaks_into_a_group(self):
        with Session(self.engine) as db:
            save_integration_config(
                db, self.settings, self._form("legacy"), operator_id=7, owner_id=7
            )
            db.commit()

            for group_id in ("10001", "20002"):
                with self.subTest(group_id=group_id):
                    view = get_integration_config_view(
                        db, 7, group_id=group_id, profile_id="default"
                    )
                    self.assertEqual(view["ftp"]["host"], "")
                    self.assertFalse(view["ftp"]["enabled"])
                    self.assertEqual(view["profiles"], [])
                    self.assertFalse(
                        get_saved_ftp_connection_config(
                            db, self.settings, 7, group_id=group_id
                        ).enabled
                    )
                    with self.assertRaises(PortalError):
                        set_active_integration_profile(
                            db, 7, group_id, "default", 7
                        )

    def test_profiles_are_encrypted_isolated_and_active_selection_is_used(self):
        with Session(self.engine) as db:
            for profile in ("alpha", "beta"):
                save_integration_config(
                    db,
                    self.settings,
                    self._form(profile),
                    operator_id=7,
                    owner_id=7,
                    group_id="10001",
                    profile_id=profile,
                )
            save_integration_config(
                db,
                self.settings,
                self._form("other"),
                operator_id=7,
                owner_id=7,
                group_id="20002",
                profile_id="alpha",
            )
            db.commit()

            payload = db.get(
                PortalSetting, "external_integrations:user:7"
            ).value_json
            alpha = payload["profiles"]["10001"]["alpha"]
            beta = payload["profiles"]["10001"]["beta"]
            self.assertNotIn("ftp-secret-alpha", repr(alpha))
            self.assertNotIn("db-secret-alpha", repr(alpha))
            self.assertNotEqual(
                alpha["ftp"]["password_ciphertext"],
                beta["ftp"]["password_ciphertext"],
            )
            self.assertEqual(
                get_active_integration_profile(db, 7, "10001"), "alpha"
            )
            self.assertEqual(
                get_saved_ftp_connection_config(
                    db, self.settings, 7, group_id="10001"
                ).password,
                "ftp-secret-alpha",
            )

            set_active_integration_profile(db, 7, "10001", "beta", 7)
            self.assertEqual(
                get_saved_ftp_connection_config(
                    db, self.settings, 7, group_id="10001"
                ).password,
                "ftp-secret-beta",
            )
            self.assertEqual(
                get_saved_ftp_connection_config(
                    db,
                    self.settings,
                    7,
                    group_id="20002",
                    profile_id="alpha",
                ).password,
                "ftp-secret-other",
            )

            profiles = list_integration_profiles(db, 7, group_id="10001")
            safe_output = repr(profiles)
            self.assertEqual(
                [item["profile_id"] for item in profiles], ["alpha", "beta"]
            )
            self.assertNotIn("password_ciphertext", safe_output)
            self.assertNotIn("ftp-secret", safe_output)
            self.assertNotIn("db-secret", safe_output)

    def test_scoped_database_selection_only_updates_selected_profile(self):
        with Session(self.engine) as db:
            for profile in ("alpha", "beta"):
                save_integration_config(
                    db,
                    self.settings,
                    self._form(profile),
                    operator_id=7,
                    owner_id=7,
                    group_id="10001",
                    profile_id=profile,
                )
            save_ftp_database_selection(
                db,
                7,
                "/incoming/beijixiong.db",
                operator_id=7,
                group_id="10001",
                profile_id="beta",
            )

            alpha = get_integration_config_view(
                db, 7, group_id="10001", profile_id="alpha"
            )
            beta = get_integration_config_view(
                db, 7, group_id="10001", profile_id="beta"
            )
            self.assertEqual(alpha["database"]["engine"], "mysql")
            self.assertEqual(beta["database"]["engine"], "sqlite")
            self.assertEqual(
                beta["database"]["sqlite_path"], "/incoming/beijixiong.db"
            )

    def test_deleting_active_profile_falls_back_without_touching_other_groups(self):
        with Session(self.engine) as db:
            for profile in ("alpha", "beta"):
                save_integration_config(
                    db,
                    self.settings,
                    self._form(profile),
                    operator_id=7,
                    owner_id=7,
                    group_id="10001",
                    profile_id=profile,
                )
            save_integration_config(
                db,
                self.settings,
                self._form("other"),
                operator_id=7,
                owner_id=7,
                group_id="20002",
                profile_id="main",
            )
            set_active_integration_profile(db, 7, "10001", "beta", 7)

            deleted = delete_integration_profile(db, 7, "10001", "beta", 7)
            self.assertEqual(deleted["deleted_profile_id"], "beta")
            self.assertEqual(
                get_active_integration_profile(db, 7, "10001"), "alpha"
            )
            self.assertEqual(
                get_integration_config_view(db, 7, group_id="10001")["ftp"]["host"],
                "ftp-alpha.example.com",
            )
            self.assertEqual(
                get_integration_config_view(db, 7, group_id="20002")["ftp"]["host"],
                "ftp-other.example.com",
            )

            delete_integration_profile(db, 7, "10001", "alpha", 7)
            self.assertEqual(
                get_active_integration_profile(db, 7, "10001"),
                DEFAULT_PROFILE_ID,
            )
            self.assertEqual(
                list_integration_profiles(db, 7, group_id="10001"), []
            )

    def test_profile_ids_are_strictly_validated(self):
        self.assertEqual(normalize_integration_profile_id("server_01"), "server_01")
        for invalid in ("", " bad", "bad ", "bad/id", "a" * 49):
            with self.subTest(invalid=invalid):
                with self.assertRaises(PortalError):
                    normalize_integration_profile_id(invalid)

        with Session(self.engine) as db:
            with self.assertRaises(PortalError):
                save_integration_config(
                    db,
                    self.settings,
                    self._form("bad"),
                    operator_id=7,
                    owner_id=7,
                    group_id="10001",
                    profile_id="bad/id",
                )
            self.assertIsNone(
                db.get(PortalSetting, "external_integrations:user:7")
            )

    def test_profile_metadata_survives_partial_ftp_updates(self):
        with Session(self.engine) as db:
            form = self._form("metadata")
            form.update(
                {
                    "profile_name": "主服",
                    "server_id": "12345678901234567",
                    "profile_enabled": "on",
                }
            )
            save_integration_config(
                db,
                self.settings,
                form,
                operator_id=7,
                owner_id=7,
                group_id="10001",
                profile_id="main",
            )
            save_integration_config(
                db,
                self.settings,
                {
                    "ftp_enabled": "on",
                    "ftp_host": "ftp-updated.example.com",
                    "ftp_port": "21",
                    "ftp_username": "ftp-metadata",
                    "ftp_password": "",
                    "ftp_remote_path": "/incoming",
                    "ftp_tls": "off",
                    "ftp_passive": "on",
                },
                operator_id=7,
                owner_id=7,
                group_id="10001",
                profile_id="main",
            )
            profile = list_integration_profiles(db, 7, group_id="10001")[0]
            self.assertEqual(profile["name"], "主服")
            self.assertEqual(profile["server_id"], "12345678901234567")
            self.assertTrue(profile["profile_enabled"])

    def test_connection_test_reports_profile_and_selected_sqlite_path(self):
        from unittest.mock import patch

        with Session(self.engine) as db:
            form = self._form("diagnostic")
            form.update(
                {
                    "database_engine": "sqlite",
                    "database_sqlite_path": "/incoming/beijixiong.db",
                }
            )
            save_integration_config(
                db,
                self.settings,
                form,
                operator_id=7,
                owner_id=7,
                group_id="10001",
                profile_id="main",
            )
            with patch("web_client.integration_service._test_ftp") as test_ftp, patch(
                "web_client.integration_service._test_sqlite_database",
                return_value="/incoming/beijixiong.db",
            ):
                result = test_integration_connection(
                    db,
                    self.settings,
                    "both",
                    7,
                    group_id="10001",
                    profile_id="main",
                )
            test_ftp.assert_called_once()
            self.assertIn("/incoming/beijixiong.db", result)

    def test_disabled_profiles_do_not_become_active(self):
        with Session(self.engine) as db:
            disabled = self._form("disabled")
            disabled.update({"profile_enabled": "off"})
            save_integration_config(
                db,
                self.settings,
                disabled,
                operator_id=7,
                owner_id=7,
                group_id="10001",
                profile_id="disabled",
            )
            save_integration_config(
                db,
                self.settings,
                self._form("enabled"),
                operator_id=7,
                owner_id=7,
                group_id="10001",
                profile_id="enabled",
            )
            self.assertEqual(
                get_active_integration_profile(db, 7, "10001"), "enabled"
            )
            with self.assertRaises(PortalError):
                set_active_integration_profile(db, 7, "10001", "disabled")

    def test_server_profiles_can_be_paused_and_resumed(self):
        with Session(self.engine) as db:
            for profile_id in ("alpha", "beta"):
                form = self._form(profile_id)
                form.update({"server_id": "41638507", "profile_enabled": "on"})
                save_integration_config(
                    db,
                    self.settings,
                    form,
                    operator_id=7,
                    owner_id=7,
                    group_id="10001",
                    profile_id=profile_id,
                )
            set_active_integration_profile(db, 7, "10001", "beta", 7)

            set_integration_profile_enabled(db, 7, "10001", "beta", False, 7)

            self.assertEqual(
                get_active_integration_profile(db, 7, "10001"), "alpha"
            )
            profiles = {
                item["profile_id"]: item
                for item in list_integration_profiles(db, 7, group_id="10001")
            }
            self.assertFalse(profiles["beta"]["profile_enabled"])
            self.assertTrue(profiles["alpha"]["profile_enabled"])

            set_integration_profile_enabled(db, 7, "10001", "beta", True, 7)
            profiles = {
                item["profile_id"]: item
                for item in list_integration_profiles(db, 7, group_id="10001")
            }
            self.assertTrue(profiles["beta"]["profile_enabled"])
            set_active_integration_profile(db, 7, "10001", "beta", 7)
            self.assertEqual(
                get_active_integration_profile(db, 7, "10001"), "beta"
            )

    def test_windows_projection_carries_active_server_metadata_without_secrets(self):
        with Session(self.engine) as db:
            form = self._form("server")
            form.update(
                {
                    "profile_name": "主服",
                    "server_id": "38615532",
                    "profile_enabled": "on",
                }
            )
            save_integration_config(
                db,
                self.settings,
                form,
                operator_id=7,
                owner_id=7,
                group_id="10001",
                profile_id="main",
            )
            projection = get_windows_database_profile_projection(db, 7, "10001")
            self.assertEqual(projection["server_id"], "38615532")
            self.assertEqual(projection["server_profiles"][0]["id"], "main")
            self.assertTrue(projection["server_profiles"][0]["enabled"])
            self.assertNotIn("ftp-secret-server", repr(projection))
            self.assertNotIn("db-secret-server", repr(projection))

    def test_multiple_profiles_receive_isolated_windows_database_paths(self):
        with Session(self.engine) as db:
            for profile in ("alpha", "beta"):
                form = self._form(profile)
                form.update({"profile_name": profile, "profile_enabled": "on"})
                save_integration_config(
                    db,
                    self.settings,
                    form,
                    operator_id=7,
                    owner_id=7,
                    group_id="10001",
                    profile_id=profile,
                )
            projection = get_windows_database_profile_projection(db, 7, "10001")
            paths = [item["path"] for item in projection["database_profiles"]]
            self.assertEqual(len(paths), len(set(paths)))
            self.assertEqual(
                {"sql/servers/10001/alpha.db", "sql/servers/10001/beta.db"},
                set(paths),
            )

    def test_same_profile_id_is_fully_isolated_between_groups(self):
        with Session(self.engine) as db:
            save_integration_config(
                db,
                self.settings,
                self._form("first"),
                operator_id=7,
                owner_id=7,
                group_id="10001",
                profile_id="main",
            )
            save_integration_config(
                db,
                self.settings,
                self._form("second"),
                operator_id=7,
                owner_id=7,
                group_id="20002",
                profile_id="main",
            )

            first = get_integration_config_view(
                db, 7, group_id="10001", profile_id="main"
            )
            second = get_integration_config_view(
                db, 7, group_id="20002", profile_id="main"
            )
            first_projection = get_windows_database_profile_projection(db, 7, "10001")
            second_projection = get_windows_database_profile_projection(db, 7, "20002")
            self.assertEqual(first["ftp"]["host"], "ftp-first.example.com")
            self.assertEqual(second["ftp"]["host"], "ftp-second.example.com")
            self.assertEqual(
                first_projection["database_profiles"][0]["path"],
                "sql/servers/10001/main.db",
            )
            self.assertEqual(
                second_projection["database_profiles"][0]["path"],
                "sql/servers/20002/main.db",
            )

    def test_ftp_connections_are_independent_from_server_bindings(self):
        with Session(self.engine) as db:
            # Start from the previous per-server layout and migrate it without
            # touching another group or exposing its encrypted password.
            save_integration_config(
                db,
                self.settings,
                self._form("legacy"),
                operator_id=7,
                owner_id=7,
                group_id="10001",
                profile_id="legacy-server",
            )
            self.assertTrue(
                migrate_legacy_group_ftp_connections(db, 7, "10001", 7)
            )
            legacy_profile = get_integration_config_view(
                db, 7, group_id="10001", profile_id="legacy-server"
            )
            self.assertEqual(legacy_profile["ftp_connection_id"], "ftp-legacy-server")
            self.assertEqual(legacy_profile["ftp"]["host"], "ftp-legacy.example.com")

            first = self._form("first")
            second = self._form("second")
            save_ftp_connection(
                db,
                self.settings,
                first,
                7,
                owner_id=7,
                group_id="10001",
                ftp_connection_id="ftp-first",
            )
            save_ftp_connection(
                db,
                self.settings,
                second,
                7,
                owner_id=7,
                group_id="10001",
                ftp_connection_id="ftp-second",
            )
            for profile_id, connection_id in (
                ("server-a", "ftp-first"),
                ("server-b", "ftp-second"),
            ):
                save_server_database_binding(
                    db,
                    {
                        "profile_name": profile_id,
                        "server_id": "",
                        "profile_enabled": "on",
                        "ftp_connection_id": connection_id,
                    },
                    7,
                    owner_id=7,
                    group_id="10001",
                    profile_id=profile_id,
                )

            server_a = get_integration_config_view(
                db, 7, group_id="10001", profile_id="server-a"
            )
            server_b = get_integration_config_view(
                db, 7, group_id="10001", profile_id="server-b"
            )
            self.assertEqual(server_a["ftp"]["host"], "ftp-first.example.com")
            self.assertEqual(server_b["ftp"]["host"], "ftp-second.example.com")
            self.assertEqual(server_a["ftp_connection_id"], "ftp-first")
            self.assertEqual(server_b["ftp_connection_id"], "ftp-second")
            self.assertEqual(
                [item["ftp_connection_id"] for item in list_ftp_connections(db, 7, "10001")],
                ["ftp-first", "ftp-legacy-server", "ftp-second"],
            )
            self.assertEqual(
                get_ftp_connection_view(db, 7, "10001", "ftp-first")["bound_profile_ids"],
                ["server-a"],
            )
            with self.assertRaises(PortalError):
                delete_ftp_connection(db, 7, "10001", "ftp-first", 7)

            # Changing one connection cannot alter the other server's source.
            changed = dict(first)
            changed["ftp_host"] = "ftp-first-new.example.com"
            changed["ftp_password"] = ""
            save_ftp_connection(
                db,
                self.settings,
                changed,
                7,
                owner_id=7,
                group_id="10001",
                ftp_connection_id="ftp-first",
            )
            self.assertEqual(
                get_integration_config_view(
                    db, 7, group_id="10001", profile_id="server-b"
                )["ftp"]["host"],
                "ftp-second.example.com",
            )

    def test_explicit_ftp_cleanup_detaches_bound_profile_and_database(self):
        with Session(self.engine) as db:
            save_ftp_connection(
                db,
                self.settings,
                self._form("cleanup"),
                7,
                owner_id=7,
                group_id="10001",
                ftp_connection_id="ftp-cleanup",
            )
            save_server_database_binding(
                db,
                {
                    "profile_name": "server-2962f067f1",
                    "server_id": "",
                    "profile_enabled": "on",
                    "ftp_connection_id": "ftp-cleanup",
                    "database_enabled": "on",
                    "database_engine": "sqlite",
                    "database_path": "/BjxRobot/beijixiong.db",
                },
                7,
                owner_id=7,
                group_id="10001",
                profile_id="server-2962f067f1",
            )

            detached = delete_ftp_connection(
                db,
                7,
                "10001",
                "ftp-cleanup",
                7,
                detach_bound_profiles=True,
            )

            self.assertEqual(["server-2962f067f1"], detached)
            self.assertEqual([], list_ftp_connections(db, 7, "10001"))
            view = get_integration_config_view(
                db, 7, group_id="10001", profile_id="server-2962f067f1"
            )
            self.assertEqual("", view["ftp_connection_id"])
            self.assertFalse(view["database"]["enabled"])
            self.assertEqual("", view["database"]["sqlite_path"])

    def test_server_binding_saves_sqlite_path_inline_without_changing_source_server(self):
        with Session(self.engine) as db:
            save_ftp_connection(
                db,
                self.settings,
                self._form("inline"),
                7,
                owner_id=7,
                group_id="10001",
                ftp_connection_id="ftp-inline",
            )
            primary = save_server_database_binding(
                db,
                {
                    "profile_name": "primary",
                    "server_id": "101",
                    "profile_enabled": "on",
                    "ftp_connection_id": "ftp-inline",
                    "database_path": "/incoming/scum/primary.sqlite",
                },
                7,
                owner_id=7,
                group_id="10001",
                profile_id="primary",
            )
            self.assertTrue(primary["database"]["enabled"])
            self.assertEqual("/incoming/scum/primary.sqlite", primary["database"]["sqlite_path"])

            replica = save_server_database_binding(
                db,
                {
                    "profile_name": "replica",
                    "server_id": "202",
                    "profile_enabled": "on",
                    "ftp_connection_id": "ftp-inline",
                    "database_profile_id": "primary",
                    # A posted value from the current server must be ignored
                    # when the selected database belongs to another server.
                    "database_path": "/incoming/scum/should-not-overwrite.db",
                },
                7,
                owner_id=7,
                group_id="10001",
                profile_id="replica",
            )
            self.assertEqual("primary", replica["database_profile_id"])
            source = get_integration_config_view(
                db, 7, group_id="10001", profile_id="primary"
            )
            self.assertEqual("/incoming/scum/primary.sqlite", source["database"]["sqlite_path"])


if __name__ == "__main__":
    unittest.main()
