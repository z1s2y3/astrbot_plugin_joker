"""Focused standard-library tests for deployment manifest verification."""

import hashlib
import importlib.util
import json
from pathlib import Path
import runpy
import sys
import tempfile
import types
import unittest
import zipfile


MODULE_PATH = (
    Path(__file__).parent / "deploy" / "linux" / "verify_update_manifest.py"
)
SPEC = importlib.util.spec_from_file_location("verify_update_manifest", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


class VerifyTreeTransportArtifactTests(unittest.TestCase):
    release_version = "1.10.31"
    archive_filename = "Joker_Baota_Remote_v1.10.31-all-in-one.zip"

    def _write_tree(self, tree: Path) -> None:
        payload = b"deployment payload\n"
        (tree / "payload.txt").write_bytes(payload)
        manifest = {
            "format_version": 1,
            "release_version": self.release_version,
            "archive_filename": self.archive_filename,
            "files": {
                "payload.txt": {
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            },
        }
        (tree / "update-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def test_root_archive_and_sidecar_do_not_fail_tree_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tree = Path(temporary_directory)
            self._write_tree(tree)
            (tree / self.archive_filename).write_bytes(b"downloaded archive")
            (tree / f"{self.archive_filename}.sha256").write_text(
                "transport checksum\n", encoding="utf-8"
            )

            manifest = VERIFIER.verify_tree(tree)

        self.assertEqual(manifest["release_version"], self.release_version)

    def test_unexpected_root_non_transport_file_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tree = Path(temporary_directory)
            self._write_tree(tree)
            (tree / "unexpected.conf").write_text("not payload\n", encoding="utf-8")

            with self.assertRaisesRegex(
                VERIFIER.VerificationError, r"not listed: unexpected\.conf"
            ):
                VERIFIER.verify_tree(tree)

    def test_stale_deployment_guides_and_archives_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tree = Path(temporary_directory)
            self._write_tree(tree)
            (tree / "最新部署包.txt").write_text("old instructions\n", encoding="utf-8")
            (tree / "Joker_Baota_Remote_v1.10.30-all-in-one.zip").write_bytes(
                b"old archive"
            )
            (tree / "Joker_Baota_Remote_v1.10.30-all-in-one.zip.sha256").write_text(
                "old checksum\n", encoding="utf-8"
            )

            manifest = VERIFIER.verify_tree(tree)

        self.assertEqual(manifest["release_version"], self.release_version)

    def test_nested_archive_named_file_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tree = Path(temporary_directory)
            self._write_tree(tree)
            nested = tree / "nested"
            nested.mkdir()
            (nested / self.archive_filename).write_bytes(b"not a transport artifact")

            with self.assertRaisesRegex(
                VERIFIER.VerificationError,
                r"not listed: nested/Joker_Baota_Remote_v1\.10\.31-all-in-one\.zip",
            ):
                VERIFIER.verify_tree(tree)

    def test_crlf_shell_payload_is_rejected_before_install(self) -> None:
        """Prevent the historical ``set: pipefail\\r`` update failure."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            tree = Path(temporary_directory)
            payload = b"#!/usr/bin/env bash\r\nset -Eeuo pipefail\r\n"
            (tree / "install.sh").write_bytes(payload)
            manifest = {
                "format_version": 1,
                "release_version": self.release_version,
                "archive_filename": self.archive_filename,
                "files": {
                    "install.sh": {
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                },
            }
            (tree / "update-manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                VERIFIER.VerificationError,
                r"Shell script uses CRLF line endings: install\.sh",
            ):
                VERIFIER.verify_tree(tree)

    def test_baota_builder_uses_ascii_guide_name_and_rejects_unicode_entries(self) -> None:
        builder = (
            Path(__file__).parent / "deploy" / "build_baota_package.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('"DEPLOYMENT.txt"', builder)
        self.assertIn("$nonAsciiEntries", builder)
        self.assertIn("function Write-PosixZipArchive", builder)
        self.assertIn("$archive.CreateEntry(", builder)

    def test_critical_portal_files_are_guarded_at_build_and_staging(self) -> None:
        """A partial update must not drop release notes or integration UI."""

        root = Path(__file__).parent
        builder = (root / "deploy" / "build_baota_package.ps1").read_text(
            encoding="utf-8"
        )
        stage = (root / "deploy" / "linux" / "joker-web-secure-stage.py").read_text(
            encoding="utf-8"
        )
        expected = (
            "01_plugin_source/astrbot_plugin_joker/web_client/reply_asset_service.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/update_log.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/templates/admin_update_log.html",
            "01_plugin_source/astrbot_plugin_joker/web_client/integration_service.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/templates/admin_integrations.html",
            "01_plugin_source/astrbot_plugin_joker/web_client/templates/group_integrations.html",
            "01_plugin_source/astrbot_plugin_joker/runtime_compat.py",
            "01_plugin_source/astrbot_plugin_joker/scum/database.py",
        )
        for entry in expected:
            with self.subTest(entry=entry):
                self.assertIn(entry, builder)
                self.assertIn(entry, stage)

    def test_root_stage_reports_the_missing_archive_paths(self) -> None:
        """The portal must expose actionable errors for a rejected update."""

        root_stage_path = (
            Path(__file__).parent
            / "deploy"
            / "linux"
            / "joker-web-secure-stage.py"
        )
        previous_pwd = sys.modules.get("pwd")
        sys.modules["pwd"] = types.ModuleType("pwd")
        try:
            root_stage = runpy.run_path(str(root_stage_path))
        finally:
            if previous_pwd is None:
                del sys.modules["pwd"]
            else:
                sys.modules["pwd"] = previous_pwd

        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            archive_path = workspace / "partial-update.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("install.sh", "#!/usr/bin/env bash\n")
                archive.writestr("update-manifest.json", "{}\n")

            with self.assertRaisesRegex(
                root_stage["StageError"],
                r"Update archive is missing required deployment files: .*health_check\.sh",
            ):
                root_stage["_validate_archive_and_extract"](
                    archive_path,
                    workspace / "payload",
                )


if __name__ == "__main__":
    unittest.main()
