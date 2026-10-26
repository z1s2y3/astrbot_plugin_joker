"""Regression checks for the Linux portal update handoff contract."""

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent


class UpdateFlowContractTests(unittest.TestCase):
    def test_update_history_reads_regular_file_without_page_error(self):
        from web_client import self_update

        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "update-history.json"
            history.write_text(
                json.dumps(
                    [
                        {
                            "state": "success",
                            "message": "完成",
                            "release_version": "1.11.358",
                            "package": "Joker_Baota_Remote_v1.11.358-all-in-one.zip",
                            "updated_at": "2026-10-17 19:00:00 CST",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with patch.object(self_update, "HISTORY_FILE", history):
                rows = self_update.get_update_history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "success")
        self.assertEqual(rows[0]["release_version"], "1.11.358")

    def test_installer_names_previous_release_backups_with_version(self):
        installer = (ROOT / "deploy" / "linux" / "quick_install.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("release_version_from_directory", installer)
        self.assertIn("PREVIOUS_RELEASE_VERSION", installer)
        self.assertIn(".joker-web-rollback-v${previous_version}", installer)
        self.assertIn("joker-web-v%s-%s", installer)
        self.assertIn(".joker-web-failed-v${failed_version}", installer)

    def test_web_worker_uses_task_specific_package_in_joker_install(self):
        worker = (
            ROOT / "deploy" / "linux" / "joker-web-apply-update.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'PACKAGE_PATH="$UPDATE_ROOT/.joker-web-update-${UPDATE_ID}-${PACKAGE_LABEL}"',
            worker,
        )
        self.assertIn('cd "$TEMP_DIR"', worker)
        self.assertIn('bash ./install.sh --non-interactive', worker)
        self.assertIn('rm -f -- "$PACKAGE_PATH"', worker)
        self.assertIn('rm -rf -- "$WORK_DIR"', worker)

    def test_web_worker_reports_manifest_reason_in_status(self):
        worker = (
            ROOT / "deploy" / "linux" / "joker-web-apply-update.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'MANIFEST_MESSAGE="$(python3 "$MANIFEST_VERIFIER" --tree "$TEMP_DIR" 2>&1)"',
            worker,
        )
        self.assertIn('update package verification failed: ${MANIFEST_MESSAGE}', worker)

    def test_documented_live_target_and_backup_contract(self):
        guide = (ROOT / "deploy" / "linux" / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("/www/wwwroot/joker-install", guide)
        self.assertIn("joker-web-v<previous-version>-<timestamp>-<pid>", guide)
        self.assertIn("/www/wwwroot/joker-web", guide)


if __name__ == "__main__":
    unittest.main()
