"""Focused tests for daily broadcast scheduling helpers."""

import copy
import importlib.util
import sys
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


def _load_timed_message_module():
    package_name = "_timed_message_test_package"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).parent / "qqadmin")]
    sys.modules[package_name] = package

    storage = {}
    group_config = ModuleType(f"{package_name}.group_config")

    def get_group_data(group_id, key, default):
        return copy.deepcopy(storage.get((str(group_id), key), default))

    def set_group_data(group_id, key, value):
        storage[(str(group_id), key)] = copy.deepcopy(value)

    def update_group_data(group_id, key, callback):
        current = get_group_data(group_id, key, [])
        updated = callback(current)
        set_group_data(group_id, key, updated)
        return updated

    group_config.get_group_data = get_group_data
    group_config.set_group_data = set_group_data
    group_config.update_group_data = update_group_data
    sys.modules[group_config.__name__] = group_config

    module_path = Path(__file__).parent / "qqadmin" / "timed_message.py"
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.timed_message", module_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


timed_message = _load_timed_message_module()


class TestDailyBroadcastSchedule(unittest.TestCase):
    def test_daily_time_is_converted_to_the_compatible_cron_expression(self):
        self.assertEqual("30 9 * * *", timed_message.cron_from_daily_time("09:30"))
        self.assertEqual("0 0 * * *", timed_message.cron_from_daily_time("00:00"))
        self.assertEqual("59 23 * * *", timed_message.cron_from_daily_time("23:59"))
        self.assertIsNone(timed_message.cron_from_daily_time("9:30"))
        self.assertIsNone(timed_message.cron_from_daily_time("24:00"))
        self.assertIsNone(timed_message.cron_from_daily_time("09:60"))

    def test_broadcast_arguments_allow_at_all_before_or_after_time(self):
        self.assertEqual(
            ("09:00", False, "daily news"),
            timed_message.parse_daily_broadcast_arguments("09:00 daily news"),
        )
        self.assertEqual(
            ("09:00", True, "daily news"),
            timed_message.parse_daily_broadcast_arguments("@全体 09:00 daily news"),
        )
        self.assertEqual(
            ("09:00", True, "daily news"),
            timed_message.parse_daily_broadcast_arguments("09:00 @全体 daily news"),
        )
        self.assertIsNone(timed_message.parse_daily_broadcast_arguments("@全体 09:00"))

    def test_one_time_broadcast_arguments_and_schedule(self):
        self.assertEqual(
            ("2026-12-31 20:00", False, "year end"),
            timed_message.parse_one_time_broadcast_arguments(
                "2026-12-31 20:00 year end"
            ),
        )
        self.assertEqual(
            ("2026-12-31 20:00", True, "year end"),
            timed_message.parse_one_time_broadcast_arguments(
                "@全体 2026-12-31 20:00 year end"
            ),
        )
        self.assertEqual(
            ("2026-12-31 20:00", True, "year end"),
            timed_message.parse_one_time_broadcast_arguments(
                "2026-12-31 20:00 @全体 year end"
            ),
        )
        self.assertIsNotNone(timed_message.parse_one_time_datetime("2099-12-31 20:00"))

    def test_one_time_task_is_due_once_and_marking_it_disables_it(self):
        now = 1_700_000_000
        with patch.object(timed_message.time, "time", return_value=now):
            result = timed_message.add_timed_message(
                "one-time-group",
                "one time",
                "",
                run_at=now + 1,
            )
        self.assertTrue(result["success"])
        with patch.object(timed_message.time, "time", return_value=now + 2):
            due = timed_message.get_due_messages("one-time-group")
        self.assertEqual(1, len(due))
        timed_message.mark_sent("one-time-group", due[0]["id"])
        stored = timed_message.get_timed_messages("one-time-group")
        self.assertFalse(stored[0]["enabled"])
        with patch.object(timed_message.time, "time", return_value=now + 3):
            self.assertEqual([], timed_message.get_due_messages("one-time-group"))

    def test_due_messages_skip_recent_or_invalid_entries(self):
        now = 1_700_000_000
        current_time = time.struct_time((2023, 11, 14, 9, 30, 1, 318, -1, -1))
        messages = [
            {
                "id": "due",
                "enabled": True,
                "cron_expr": "30 9 * * *",
                "message": "daily update",
                "last_sent_at": now - 61,
            },
            {
                "id": "recent",
                "enabled": True,
                "cron_expr": "30 9 * * *",
                "message": "too soon",
                "last_sent_at": now - 10,
            },
            {
                "id": "bad-cron",
                "enabled": True,
                "cron_expr": None,
                "message": "ignored",
                "last_sent_at": 0,
            },
        ]

        with (
            patch.object(timed_message, "load_timed_messages", return_value=messages),
            patch.object(timed_message.time, "time", return_value=now),
            patch.object(timed_message.time, "localtime", return_value=current_time),
        ):
            due = timed_message.get_due_messages("group")

        self.assertEqual(["due"], [item["id"] for item in due])

    def test_cron_lists_and_ranges_use_the_same_matching_rules_as_validation(self):
        current_time = time.struct_time((2023, 11, 14, 9, 30, 1, 318, -1, -1))

        self.assertTrue(
            timed_message.is_cron_match("25-35,40 9 * * *", current_time)
        )
        self.assertTrue(
            timed_message.is_cron_match("0,15,30,45 9 * * *", current_time)
        )
        self.assertFalse(
            timed_message.is_cron_match("25-29,40 9 * * *", current_time)
        )

    def test_cron_rejects_descending_ranges_and_accepts_steps_in_lists(self):
        self.assertIn("error", timed_message.validate_cron_expr("10-5 9 * * *"))
        self.assertIn("error", timed_message.validate_cron_expr("*/0 9 * * *"))
        self.assertTrue(
            timed_message.validate_cron_expr("5-25/5,30 9 * * *").get("success")
        )
        current_time = time.struct_time((2023, 11, 14, 9, 20, 1, 318, -1, -1))
        self.assertTrue(
            timed_message.is_cron_match("5-25/5,30 9 * * *", current_time)
        )

    def test_daily_broadcast_keeps_at_all_flag_in_persisted_task(self):
        result = timed_message.add_timed_message(
            "group",
            "hello everyone",
            "0 9 * * *",
            at_all=True,
            name="daily broadcast 09:00",
        )

        self.assertTrue(result["success"])
        stored = timed_message.get_timed_messages("group")
        self.assertEqual(1, len(stored))
        self.assertTrue(stored[0]["at_all"])
        self.assertEqual("daily broadcast 09:00", stored[0]["name"])

    def test_random_music_task_uses_automatic_interfaces_and_is_due(self):
        with patch.object(timed_message.time, "time", return_value=1_699_999_000):
            result = timed_message.add_timed_message(
                "music-group",
                "晴天|杨幂|稻香",
                "30 9 * * *",
                action="random_music",
                name="午间随机音乐",
            )
        self.assertTrue(result["success"])
        stored = timed_message.get_timed_messages("music-group")
        self.assertEqual("random_music", stored[0]["action"])
        self.assertNotIn("music_provider", stored[0])

        current_time = time.struct_time((2023, 11, 14, 9, 30, 1, 318, -1, -1))
        with patch.object(timed_message.time, "time", return_value=1_700_000_000), patch.object(
            timed_message.time, "localtime", return_value=current_time
        ):
            due = timed_message.get_due_messages("music-group")
        self.assertEqual(1, len(due))

    def test_hourly_time_task_is_created_without_message_and_runs_on_the_hour(self):
        with patch.object(timed_message.time, "time", return_value=1_699_999_000):
            result = timed_message.add_timed_message(
                "hourly-group",
                "",
                "0 * * * *",
                action="hourly_time",
                name="整点报时",
                at_all=True,
            )
        self.assertTrue(result["success"])
        stored = timed_message.get_timed_messages("hourly-group")
        self.assertEqual("hourly_time", stored[0]["action"])
        self.assertEqual("0 * * * *", stored[0]["cron_expr"])
        self.assertTrue(stored[0]["at_all"])
        current_time = time.struct_time((2023, 11, 14, 10, 0, 1, 318, -1, -1))
        with patch.object(timed_message.time, "time", return_value=1_700_000_000), patch.object(
            timed_message.time, "localtime", return_value=current_time
        ):
            due = timed_message.get_due_messages("hourly-group")
        self.assertEqual(1, len(due))

    def test_hourly_time_runtime_honors_at_all(self):
        """The Windows executor must retain the portal's hourly @all setting."""
        source = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        start = source.index('if action_type == "hourly_time":')
        end = source.index('elif action_type == "whole_ban":', start)
        hourly_branch = source[start:end]

        self.assertIn('if msg.get("at_all", False):', hourly_branch)
        self.assertIn('At(qq="all")', hourly_branch)

    def test_slow_hourly_send_does_not_consume_the_next_hour(self):
        group_id = "hourly-cross-boundary"
        scheduled_at = int(time.mktime((2026, 10, 13, 10, 0, 0, 0, 0, -1)))
        finished_at = scheduled_at + 3602
        timed_message.save_timed_messages(group_id, [{
            "id": "hourly", "enabled": True, "action": "hourly_time",
            "message": "", "last_sent_at": 0, "sent_count": 0,
        }])
        with patch.object(timed_message.time, "time", return_value=finished_at):
            timed_message.mark_sent(group_id, "hourly", scheduled_at=scheduled_at)
            due = timed_message.get_due_messages(group_id)
            self.assertEqual(1, len(due))
            self.assertEqual(scheduled_at + 3600, due[0]["_scheduled_at"])
            self.assertTrue(timed_message.claim_timed_message(group_id, "hourly", scheduled_at=due[0]["_scheduled_at"]))
            timed_message.mark_sent(group_id, "hourly")
            self.assertEqual([], timed_message.get_due_messages(group_id))
        stored = timed_message.get_timed_messages(group_id)[0]
        self.assertEqual(finished_at, stored["last_sent_at"])
        self.assertEqual(scheduled_at + 3600, stored["last_scheduled_at"])

    def test_hourly_time_has_one_due_slot_at_each_hour_boundary(self):
        """An hourly report produces one planned run for every HH:00."""
        group_id = "hourly-boundary-group"
        timed_message.save_timed_messages(
            group_id,
            [{
                "id": "hourly",
                "enabled": True,
                # A stale Cron from an old portal configuration is ignored.
                "cron_expr": "15 9 * * *",
                "action": "hourly_time",
                "message": "",
                "created_at": 0,
                "last_sent_at": 0,
                "sent_count": 0,
            }],
        )
        base = 1_700_000_000
        for hour in range(24):
            now = base + hour * 3600
            current_time = time.struct_time((2023, 11, 14, hour, 0, 0, 318, -1, -1))
            with (
                patch.object(timed_message.time, "time", return_value=now),
                patch.object(timed_message.time, "localtime", return_value=current_time),
            ):
                due = timed_message.get_due_messages(group_id)
                self.assertEqual(1, len(due), f"hour={hour}")
                self.assertEqual(now - (now % 60), due[0]["_scheduled_at"])
                timed_message.mark_sent(group_id, due[0]["id"])
        stored = timed_message.get_timed_messages(group_id)[0]
        self.assertEqual(24, stored["sent_count"])

    def test_cron_task_recovers_a_recently_missed_hour_once(self):
        # The polling worker may resume after 10:00 because another task was
        # waiting on QQ.  The 10:00 report must still be sent once.
        now = 1_700_000_125
        messages = [{
            "id": "hourly-catch-up",
            "enabled": True,
            "cron_expr": "0 * * * *",
            "action": "hourly_time",
            "message": "",
            "last_sent_at": now - 3_700,
        }]
        ten_am = time.struct_time((2023, 11, 14, 10, 0, 0, 318, -1, -1))
        with (
            patch.object(timed_message, "load_timed_messages", return_value=messages),
            patch.object(timed_message.time, "time", return_value=now),
            patch.object(timed_message.time, "localtime", return_value=ten_am),
        ):
            due = timed_message.get_due_messages("group")
        self.assertEqual(["hourly-catch-up"], [item["id"] for item in due])
        self.assertEqual(now - (now % 60), due[0]["_scheduled_at"])

    def test_hourly_report_can_recover_the_latest_hour_after_reload(self):
        """A reload at :45 may still deliver the current hour once."""
        now = (10 * 3600) + (45 * 60)
        with (
            patch.object(
                timed_message,
                "is_cron_match",
                side_effect=lambda _expr, slot: slot.tm_min == 0,
            ),
            patch.object(timed_message.time, "localtime", side_effect=time.gmtime),
        ):
            scheduled_at = timed_message._latest_due_cron_slot(
                "0 * * * *",
                now,
                last_sent_at=(9 * 3600),
                catchup_seconds=65 * 60,
            )

        self.assertEqual(10 * 3600, scheduled_at)

    def test_cron_task_does_not_replay_an_old_or_newly_created_hour(self):
        now = 1_700_000_660
        messages = [{
            "id": "stale-hourly",
            "enabled": True,
            "cron_expr": "0 * * * *",
            "action": "hourly_time",
            "message": "",
            "last_sent_at": 0,
            "created_at": now,
        }]
        ten_am = time.struct_time((2023, 11, 14, 10, 0, 0, 318, -1, -1))
        with (
            patch.object(timed_message, "load_timed_messages", return_value=messages),
            patch.object(timed_message.time, "time", return_value=now),
            patch.object(timed_message.time, "localtime", return_value=ten_am),
        ):
            self.assertEqual([], timed_message.get_due_messages("group"))

    def test_claim_is_single_flight_until_send_is_marked(self):
        timed_message.save_timed_messages(
            "claim-group",
            [{
                "id": "task-1",
                "enabled": True,
                "cron_expr": "* * * * *",
                "message": "hello",
                "last_sent_at": 0,
            }],
        )
        with patch.object(timed_message.time, "time", return_value=1_700_000_000):
            self.assertTrue(timed_message.claim_timed_message("claim-group", "task-1"))
            self.assertFalse(timed_message.claim_timed_message("claim-group", "task-1"))

    def test_active_claim_cannot_be_preempted_by_another_account(self):
        timed_message.save_timed_messages(
            "priority-group",
            [{
                "id": "task-1",
                "enabled": True,
                "cron_expr": "* * * * *",
                "message": "hello",
                "last_sent_at": 0,
            }],
        )
        with patch.object(timed_message.time, "time", return_value=1_700_000_000):
            self.assertTrue(
                timed_message.claim_timed_message(
                    "priority-group", "task-1", owner_id="low", priority=10
                )
            )
            self.assertFalse(
                timed_message.claim_timed_message(
                    "priority-group", "task-1", owner_id="same", priority=10
                )
            )
            self.assertFalse(
                timed_message.claim_timed_message(
                    "priority-group", "task-1", owner_id="high", priority=90
                )
            )
            # Priority is applied before claiming.  Once an account starts
            # the QQ request, a later account must not duplicate the send.
            timed_message.mark_sent("priority-group", "task-1")
            self.assertFalse(timed_message.claim_timed_message("priority-group", "task-1"))

    def test_failed_task_is_backed_off_instead_of_retried_every_scheduler_pass(self):
        now = 1_700_000_000
        timed_message.save_timed_messages(
            "retry-group",
            [{
                "id": "task-1",
                "enabled": True,
                "cron_expr": "30 9 * * *",
                "message": "hello",
                "last_sent_at": 0,
            }],
        )
        current_time = time.struct_time((2023, 11, 14, 9, 30, 1, 318, -1, -1))
        with patch.object(timed_message.time, "time", return_value=now):
            timed_message.mark_timed_message_retry(
                "retry-group", "task-1", retry_after_seconds=300, reason="no ack"
            )
        with (
            patch.object(timed_message.time, "time", return_value=now + 60),
            patch.object(timed_message.time, "localtime", return_value=current_time),
        ):
            self.assertEqual([], timed_message.get_due_messages("retry-group"))
        stored = timed_message.get_timed_messages("retry-group")[0]
        self.assertEqual("no ack", stored["last_error"])

    def test_music_metadata_is_retained_after_success_and_clears_retry_state(self):
        timed_message.save_timed_messages(
            "music-state-group",
            [{
                "id": "task-1",
                "enabled": True,
                "cron_expr": "0 9 * * *",
                "message": "晴天|稻香",
                "next_retry_at": 1_700_000_300,
                "last_error": "old error",
            }],
        )
        timed_message.mark_sent(
            "music-state-group",
            "task-1",
            music_keyword="稻香",
            music_track="qq:123",
        )
        stored = timed_message.get_timed_messages("music-state-group")[0]
        self.assertEqual("稻香", stored["last_music_keyword"])
        self.assertEqual("qq:123", stored["last_music_track"])
        self.assertNotIn("next_retry_at", stored)
        self.assertNotIn("last_error", stored)


if __name__ == "__main__":
    unittest.main()
