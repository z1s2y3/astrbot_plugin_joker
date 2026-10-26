#!/usr/bin/env python3
"""Offline tests for invitation statistics in qqadmin.audit."""

from __future__ import annotations

import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PACKAGE_NAME = "_invite_audit_test_package"


def _local_timestamp(year: int, month: int, day: int) -> int:
    return int(time.mktime((year, month, day, 12, 0, 0, 0, 0, -1)))


class InviteAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage: dict[str, dict] = {}
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(ROOT / "qqadmin")]
        sys.modules[PACKAGE_NAME] = package

        group_config = types.ModuleType(f"{PACKAGE_NAME}.group_config")

        def get_group_data(group_id, data_key, default=None):
            return self.storage.get(str(group_id), {}).get(data_key, default)

        def update_group_data(group_id, data_key, update_func):
            data = self.storage.setdefault(str(group_id), {})
            data[data_key] = update_func(data.get(data_key))
            return data[data_key]

        def set_group_data(group_id, data_key, value):
            update_group_data(group_id, data_key, lambda _old: value)

        group_config.get_group_data = get_group_data
        group_config.update_group_data = update_group_data
        group_config.set_group_data = set_group_data
        group_config.get_group_feature_setting = lambda *_args, **_kwargs: None
        group_config.get_group_feature_detail = lambda group_id, feature: self.storage.get(
            str(group_id), {}).get("features", {}).get(feature, {})
        group_config.list_all_groups = lambda: list(self.storage)
        sys.modules[group_config.__name__] = group_config

        spec = importlib.util.spec_from_file_location(
            f"{PACKAGE_NAME}.audit", ROOT / "qqadmin" / "audit.py"
        )
        self.assertIsNotNone(spec)
        self.audit = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.audit
        spec.loader.exec_module(self.audit)

    def tearDown(self) -> None:
        for module_name in list(sys.modules):
            if module_name == PACKAGE_NAME or module_name.startswith(
                f"{PACKAGE_NAME}."
            ):
                sys.modules.pop(module_name, None)

    def test_deduplicates_events_and_builds_local_daily_counts(self) -> None:
        occurred_at = _local_timestamp(2026, 8, 15)
        local_date = time.strftime("%Y-%m-%d", time.localtime(occurred_at))

        self.audit.record_invitation(
            "group", "100", "200", "first", event_id="evt-1", occurred_at=occurred_at
        )
        self.audit.record_invitation(
            "group", "100", "200", "first", event_id="evt-1", occurred_at=occurred_at + 60
        )
        self.audit.record_invitation(
            "group", "100", "200", "first", occurred_at=occurred_at + 90
        )
        self.audit.record_invitation(
            "group", "100", "201", "second", occurred_at=occurred_at + 120
        )

        self.assertEqual(2, self.audit.get_inviter_count("group", "100"))
        daily = self.audit.get_daily_invite_statistics("group", local_date)
        self.assertEqual(2, daily["total"])
        self.assertEqual({"100": 2}, daily["inviter_counts"])
        self.assertEqual(2, daily["status_counts"]["pending"])

    def test_status_summary_and_successful_join_ranking(self) -> None:
        occurred_at = _local_timestamp(2026, 8, 15)
        for invitee_id in ("a1", "a2", "a3"):
            self.audit.record_invitation(
                "group", "pending-inviter", invitee_id, occurred_at=occurred_at
            )
        for invitee_id in ("b1", "b2"):
            self.audit.record_invitation(
                "group", "successful-inviter", invitee_id, occurred_at=occurred_at
            )
        self.audit.record_invitation("group", "other", "c1", occurred_at=occurred_at)

        self.audit.update_invite_status("group", "b1", "joined")
        self.audit.update_invite_status("group", "b2", "joined")
        self.audit.update_invite_status("group", "b2", "left")
        self.audit.update_invite_status("group", "c1", "rejected")

        summary = self.audit.get_invite_status_summary("group")
        self.assertEqual(
            {"pending": 3, "joined": 1, "rejected": 1, "left": 1}, summary
        )
        ranking = self.audit.get_top_inviter("group")
        self.assertEqual("successful-inviter", ranking[0]["user_id"])
        self.assertEqual(2, ranking[0]["successful_count"])
        self.assertEqual(1, ranking[0]["joined_count"])

    def test_excessive_invites_use_today_only(self) -> None:
        today = _local_timestamp(2026, 8, 15)
        yesterday = _local_timestamp(2026, 8, 14)
        for index in range(6):
            self.audit.record_invitation(
                "group", "daily-inviter", f"today-{index}", occurred_at=today
            )
        for index in range(7):
            self.audit.record_invitation(
                "group", "daily-inviter", f"yesterday-{index}", occurred_at=yesterday
            )

        self.assertTrue(
            self.audit.check_excessive_invites("group", "daily-inviter", 5, now=today)
        )
        self.assertFalse(
            self.audit.check_excessive_invites("group", "daily-inviter", 6, now=today)
        )
        abnormal = self.audit.get_all_excessive_inviter_stats("group", 5, now=today)
        self.assertEqual(1, len(abnormal))
        self.assertEqual("daily-inviter", abnormal[0]["user_id"])
        self.assertEqual(6, abnormal[0]["count"])
        self.assertTrue(
            all(invite["invite_date"] == abnormal[0]["date"] for invite in abnormal[0]["invitees"])
        )

    def test_legacy_records_are_summarized_without_schema_migration(self) -> None:
        occurred_at = _local_timestamp(2026, 8, 15)
        self.storage["group"] = {
            "invite_stats": {
                "invites": [
                    {
                        "inviter_id": "legacy",
                        "invitee_id": "member",
                        "invited_at": occurred_at,
                        "status": "timeout_rejected",
                    }
                ],
                "inviter_counts": {"legacy": 1},
            }
        }

        self.assertEqual(1, self.audit.get_inviter_count("group", "legacy"))
        self.assertEqual(
            {"pending": 0, "joined": 0, "rejected": 1, "left": 0},
            self.audit.get_invite_status_summary("group"),
        )

    def test_id_verification_mode_keeps_its_challenge_and_mode(self) -> None:
        self.audit.set_audit_approval_mode("group", "id")

        result = self.audit.add_audit_request("group", "200", "member")

        self.assertTrue(result["success"])
        self.assertEqual("id", result["approval_mode"])
        self.assertTrue(result["challenge_info"]["verify_id"].isdigit())
        pending = self.audit.get_pending_verification("group", "200")
        self.assertEqual("id", pending["approval_mode"])
        self.assertEqual(result["challenge_info"]["verify_id"], pending["verify_id"])

    def test_feature_switch_overrides_stale_root_audit_setting(self) -> None:
        self.storage["group"] = {
            "kick_on_verification_failure": False,
            "features": {"audit": {"kick_on_verification_failure": True}},
        }

        self.assertTrue(self.audit.should_kick_on_verification_failure("group"))

        self.storage["group"]["features"]["audit"]["kick_on_verification_failure"] = False
        self.assertFalse(self.audit.should_kick_on_verification_failure("group"))

    def test_verification_answer_normalization_uses_the_actual_answer_token(self) -> None:
        normalize = self.audit.normalize_verification_answer

        self.assertEqual("3", normalize("1 + 2 = 3", "math"))
        self.assertEqual("4.0", normalize("答案是：4.0", "math"))
        self.assertEqual("44798", normalize("验证 ID：４４７９８", "id"))
        self.assertEqual("1234", normalize("审批码: 1234", "code"))

    def test_legacy_numeric_approval_code_matches_string_reply(self) -> None:
        self.storage["group"] = {
            "audit_pending": [
                {
                    "user_id": "210",
                    "status": "pending",
                    "approval_mode": "code",
                    "approval_code": 1234,
                }
            ]
        }
        self.assertEqual(
            "1234",
            self.audit.normalize_verification_answer(
                self.audit.get_pending_verification("group", "210")["approval_code"],
                "code",
            ),
        )

    def test_missing_challenge_is_restored_from_pending_prompt(self) -> None:
        self.storage["group"] = {
            "audit_pending": [
                {
                    "user_id": "211",
                    "status": "pending",
                    "approval_mode": "id",
                    "challenge_id": "missing-211",
                    "verify_id": "44798",
                    "attempts": 1,
                }
            ],
            "audit_challenges": {},
        }
        pending = self.audit.get_pending_verification("group", "211")
        self.assertTrue(self.audit.restore_pending_challenge("group", pending))
        self.assertEqual(
            (True, False),
            self.audit.verify_answer("group", "missing-211", "验证 ID：44798"),
        )

    def test_id_challenge_honors_configured_attempt_limit(self) -> None:
        challenge = self.audit.create_id_challenge("group", "203")
        verify_id = challenge["verify_id"]

        self.assertEqual((False, False), self.audit.verify_challenge_answer(
            "group", challenge["challenge_id"], "00000", max_attempts=4
        ))
        self.assertEqual((False, False), self.audit.verify_challenge_answer(
            "group", challenge["challenge_id"], "00000", max_attempts=4
        ))
        self.assertEqual((False, False), self.audit.verify_challenge_answer(
            "group", challenge["challenge_id"], "00000", max_attempts=4
        ))
        self.assertEqual((True, False), self.audit.verify_challenge_answer(
            "group", challenge["challenge_id"], verify_id, max_attempts=4
        ))

    def test_verify_answer_compatibility_wrapper_accepts_attempt_limit(self) -> None:
        challenge = self.audit.create_id_challenge("group", "204")
        self.assertEqual(
            (False, False),
            self.audit.verify_answer(
                "group", challenge["challenge_id"], "00000", max_attempts=4
            ),
        )

    def test_math_answer_accepts_integral_calculator_format_and_persists_attempts(self) -> None:
        challenge = self.audit.create_math_challenge("group", "205")
        challenge_id = challenge["challenge_id"]

        self.assertEqual(
            (False, False),
            self.audit.verify_answer("group", challenge_id, "答案是 999", max_attempts=3),
        )
        saved = self.storage["group"]["audit_challenges"][challenge_id]
        self.assertEqual(1, saved["attempts"])

        self.assertEqual(
            (True, False),
            self.audit.verify_answer(
                "group", challenge_id, f"{challenge['answer']}.0", max_attempts=3
            ),
        )
        self.assertNotIn(challenge_id, self.storage["group"]["audit_challenges"])

    def test_attempt_limit_removes_challenge_on_final_wrong_answer(self) -> None:
        challenge = self.audit.create_id_challenge("group", "206")
        challenge_id = challenge["challenge_id"]

        self.assertEqual(
            (False, False),
            self.audit.verify_answer("group", challenge_id, "00000", max_attempts=2),
        )
        self.assertEqual(
            (False, True),
            self.audit.verify_answer("group", challenge_id, "00000", max_attempts=2),
        )
        self.assertNotIn(challenge_id, self.storage["group"]["audit_challenges"])

    def test_duplicate_challenge_creation_never_reuses_an_active_id(self) -> None:
        first = self.audit.create_math_challenge("group", "207")
        second = self.audit.create_math_challenge("group", "207")

        self.assertNotEqual(first["challenge_id"], second["challenge_id"])
        challenges = self.storage["group"]["audit_challenges"]
        self.assertIn(first["challenge_id"], challenges)
        self.assertIn(second["challenge_id"], challenges)

    def test_rearm_clears_the_math_prompt_sent_marker(self) -> None:
        challenge = self.audit.create_math_challenge("group", "208")
        self.audit.add_audit_request(
            "group", "208", "member", challenge_info=challenge, approval_mode="math"
        )
        self.storage["group"]["audit_pending"][0].update(
            {"requested_at": 0, "verification_prompt_sent": True, "attempts": 2}
        )

        pending = self.audit.rearm_pending_request("group", "208")

        self.assertFalse(pending["verification_prompt_sent"])
        self.assertEqual(0, pending["attempts"])

    def test_pending_verification_poll_preserves_prompt_delivery_markers(self) -> None:
        self.storage["group"] = {
            "audit_pending": [
                {
                    "user_id": "209",
                    "status": "pending",
                    "requested_at": 1,
                    "verification_prompt_sent": True,
                    "steamid_prompt_sent": False,
                },
                {
                    "user_id": "210",
                    "status": "pending",
                    "requested_at": 2,
                    "verification_prompt_sent": False,
                    "steamid_prompt_sent": True,
                },
                {
                    "user_id": "211",
                    "status": "pending",
                    "requested_at": 3,
                },
            ]
        }

        pending = {
            row["user_id"]: row for row in self.audit.get_pending_verifications()
        }

        self.assertTrue(pending["209"]["verification_prompt_sent"])
        self.assertFalse(pending["209"]["steamid_prompt_sent"])
        self.assertTrue(pending["210"]["steamid_prompt_sent"])
        self.assertFalse(pending["210"]["verification_prompt_sent"])
        self.assertFalse(pending["211"]["verification_prompt_sent"])
        self.assertFalse(pending["211"]["steamid_prompt_sent"])

    def test_verification_expiry_uses_prompt_delivery_time(self) -> None:
        now = int(time.time())
        self.storage["group"] = {
            "verification_timeout": 60,
            "audit_pending": [
                {
                    "user_id": "212",
                    "status": "pending",
                    "requested_at": now - 600,
                    "prompt_sent_at": now - 10,
                    "verification_prompt_sent": True,
                },
                {
                    "user_id": "213",
                    "status": "pending",
                    "requested_at": now - 600,
                    "prompt_sent_at": now - 70,
                    "verification_prompt_sent": True,
                },
            ]
        }

        self.assertFalse(self.audit.is_verification_expired("group", "212"))
        self.assertTrue(self.audit.is_verification_expired("group", "213"))

    def test_timeout_cleanup_does_not_kick_without_delivered_prompt(self) -> None:
        now = int(time.time())
        self.storage["group"] = {
            "verification_timeout": 60,
            "audit_pending": [
                {
                    "user_id": "214",
                    "status": "pending",
                    "requested_at": now - 600,
                    "verification_prompt_sent": False,
                    "steamid_prompt_sent": False,
                }
            ]
        }

        self.assertEqual([], self.audit.clean_expired_challenges_and_kick("group"))
        self.assertEqual([], self.storage["group"]["audit_pending"])

    def test_timeout_cleanup_cancels_unsent_challenge_without_kicking(self) -> None:
        now = int(time.time())
        self.storage["group"] = {
            "verification_timeout": 60,
            "audit_pending": [
                {
                    "user_id": "215",
                    "status": "pending",
                    "challenge_id": "challenge-215",
                    "requested_at": now - 600,
                    "verification_prompt_sent": False,
                    "steamid_prompt_sent": False,
                }
            ],
            "audit_challenges": {
                "challenge-215": {
                    "user_id": "215",
                    "created_at": now - 600,
                    "answer": "8",
                }
            },
        }

        self.assertEqual([], self.audit.clean_expired_challenges_and_kick("group"))
        self.assertNotIn("challenge-215", self.storage["group"]["audit_challenges"])

    def test_direct_mode_does_not_create_a_challenge(self) -> None:
        result = self.audit.add_audit_request(
            "group", "200", "member", approval_mode="direct"
        )

        self.assertTrue(result["success"])
        self.assertEqual("direct", result["approval_mode"])
        self.assertEqual({}, result["challenge_info"])
        pending = self.audit.get_pending_verification("group", "200")
        self.assertEqual("direct", pending["approval_mode"])
        self.assertIsNone(pending["challenge_id"])

    def test_steamid_fallback_replaces_pending_mode_and_challenge(self) -> None:
        result = self.audit.add_audit_request(
            "group", "300", "member", approval_mode="steamid"
        )
        self.assertTrue(result["success"])
        fallback_challenge = self.audit.create_id_challenge("group", "300")

        switched = self.audit.switch_pending_approval_mode(
            "group", "300", "id", fallback_challenge
        )

        self.assertTrue(switched["success"])
        pending = self.audit.get_pending_verification("group", "300")
        self.assertEqual("id", pending["approval_mode"])
        self.assertTrue(pending["steamid_fallback"])
        self.assertEqual(fallback_challenge["challenge_id"], pending["challenge_id"])
        self.assertEqual(fallback_challenge["verify_id"], pending["verify_id"])

    def test_steamid_fallback_code_generates_new_approval_code(self) -> None:
        self.audit.add_audit_request(
            "group", "301", "member", approval_mode="steamid"
        )
        switched = self.audit.switch_pending_approval_mode(
            "group", "301", "code"
        )

        self.assertTrue(switched["success"])
        self.assertTrue(switched["approval_code"].isdigit())
        self.assertEqual(
            switched["approval_code"],
            self.audit.get_pending_verification("group", "301")["approval_code"],
        )

    def test_clears_legacy_completed_math_stage_without_kicking(self) -> None:
        self.storage["group"] = {
            "audit_pending": [
                {
                    "user_id": "400",
                    "user_name": "member",
                    "status": "pending",
                    "approval_mode": "math",
                    "steamid_stage": True,
                    "challenge_id": "old-challenge",
                    "requested_at": int(time.time()) - 600,
                },
                {
                    "user_id": "401",
                    "status": "pending",
                    "approval_mode": "math",
                    "steamid_stage": True,
                    "steamid_fallback": True,
                    "challenge_id": "real-fallback",
                },
                {
                    "user_id": "402",
                    "status": "pending",
                    "approval_mode": "steamid",
                    "steamid_stage": True,
                    "challenge_id": "explicit-steamid",
                },
            ],
            "audit_challenges": {
                "old-challenge": {"answer": "19"},
                "real-fallback": {"answer": "20"},
                "explicit-steamid": {},
            },
        }

        recovered = self.audit.clear_legacy_completed_math_verifications()

        self.assertEqual([{"group_id": "group", "user_id": "400"}], recovered)
        remaining = self.audit.get_pending_requests("group")
        self.assertEqual({"401", "402"}, {row["user_id"] for row in remaining})
        self.assertEqual(
            {"real-fallback", "explicit-steamid"},
            set(self.storage["group"]["audit_challenges"]),
        )
        self.assertEqual("approved", self.audit.get_audit_history("group")[-1]["action"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
