"""Focused regression tests for calendar-based daily sign-in handling."""

import importlib.util
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch


def _load_stats_module():
    """Load stats.py with only its storage dependency stubbed for isolation."""
    package_name = "_stats_test_package"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).parent / "qqadmin")]
    sys.modules[package_name] = package

    group_config = ModuleType(f"{package_name}.group_config")
    group_config.get_group_data = lambda *_args, **_kwargs: {}
    group_config.set_group_data = lambda *_args, **_kwargs: None
    group_config.update_group_data = lambda *_args, **_kwargs: None
    sys.modules[group_config.__name__] = group_config

    stats_path = Path(__file__).parent / "qqadmin" / "stats.py"
    spec = importlib.util.spec_from_file_location(f"{package_name}.stats", stats_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


stats = _load_stats_module()


class TestDailySigninHelpers(unittest.TestCase):
    def test_normalizes_legacy_record_without_losing_extensions(self):
        record = {
            "last_signin": "not-a-date",
            "consecutive_days": "4",
            "total_days": -3,
            "user_name": "Alice",
            "extension": {"source": "legacy"},
        }

        normalized = stats.normalize_daily_signin_record(record)
        status = stats.get_daily_signin_status(normalized, date(2026, 8, 15))

        self.assertEqual("", normalized["last_signin"])
        self.assertEqual(4, normalized["consecutive_days"])
        self.assertEqual(0, normalized["total_days"])
        self.assertEqual({"source": "legacy"}, normalized["extension"])
        self.assertFalse(status["signed_today"])

    def test_streak_uses_previous_calendar_day(self):
        record = {"last_signin": "2024-03-10", "consecutive_days": 6}

        # This date is the day after a common daylight-saving transition.
        self.assertEqual(
            7,
            stats.calculate_next_consecutive_days(record, date(2024, 3, 11)),
        )

    def test_streak_resets_after_a_missed_calendar_day(self):
        record = {"last_signin": "2024-03-10", "consecutive_days": 6}

        self.assertEqual(
            1,
            stats.calculate_next_consecutive_days(record, date(2024, 3, 12)),
        )


class TestSigninPersistence(unittest.TestCase):
    @staticmethod
    def _steam_module(binding: dict | None):
        module = ModuleType(f"{stats.__package__}.steam_signin")
        module.get_steam_binding = lambda *_args, **_kwargs: binding
        module.record_steam_signin = lambda *_args, **_kwargs: binding
        return module

    def test_explicit_websocket_backend_never_calls_database_adapter(self):
        stored = {"signin": {}, "stats": {"users": {}}}
        adapter = Mock(return_value={"success": False, "code": "must_not_run"})

        with (
            patch.object(stats, "load_signin_data", side_effect=lambda _group: stored["signin"]),
            patch.object(stats, "load_stats", side_effect=lambda _group: stored["stats"]),
            patch.object(stats, "save_signin_data", side_effect=lambda _group, value: stored.__setitem__("signin", value)),
            patch.object(stats, "save_stats", side_effect=lambda _group, value: stored.__setitem__("stats", value)),
            patch.object(stats, "_current_local_date", return_value=date(2026, 8, 20)),
        ):
            result = stats.signin(
                "group",
                "42",
                "Alice",
                reward_config={"base_reward": 0},
                signin_policy={"signin_mode": "steam", "steam_reward_mode": "websocket"},
                database_adapter=adapter,
                backend="websocket",
            )

        self.assertTrue(result["success"])
        self.assertEqual("websocket", result["backend"])
        adapter.assert_not_called()

    def test_steam_database_failure_does_not_write_local_json(self):
        steam_module = self._steam_module(
            {"steam_id": "76561198000000000", "user_id": "42"}
        )
        adapter_calls = []

        def reject_adapter(*args, **kwargs):
            adapter_calls.append((args, kwargs))
            return {
                "success": False,
                "code": "query_error",
                "message": "database failed",
            }

        with (
            patch.dict(sys.modules, {steam_module.__name__: steam_module}),
            patch.object(stats, "load_signin_data", return_value={}),
            patch.object(stats, "load_stats", return_value={"users": {}}),
            patch.object(stats, "save_signin_data") as save_signin,
            patch.object(stats, "save_stats") as save_stats,
            patch.object(stats, "_current_local_date", return_value=date(2026, 8, 20)),
        ):
            result = stats.signin(
                "group",
                "42",
                "Alice",
                signin_policy={"signin_mode": "steam"},
                database_adapter=reject_adapter,
            )

        self.assertEqual("query_error", result["code"])
        self.assertEqual(1, len(adapter_calls))
        save_signin.assert_not_called()
        save_stats.assert_not_called()

    def test_steam_database_reward_commits_before_local_mirror(self):
        steam_id = "76561198000000000"
        steam_module = self._steam_module({"steam_id": steam_id, "user_id": "42"})
        stored = {"signin": {}, "stats": {"users": {}}}
        order = []

        def apply_reward(received_steam_id, reward, **kwargs):
            order.append("database")
            self.assertEqual(steam_id, received_steam_id)
            self.assertEqual("2026-08-20", kwargs["sign_date"])
            return {"success": True, "code": "signed", "balance": 115}

        def save_signin(_group, value):
            order.append("signin_json")
            stored["signin"] = value

        def save_group_stats(_group, value):
            order.append("stats_json")
            stored["stats"] = value

        with (
            patch.dict(sys.modules, {steam_module.__name__: steam_module}),
            patch.object(stats, "load_signin_data", side_effect=lambda _group: stored["signin"]),
            patch.object(stats, "load_stats", side_effect=lambda _group: stored["stats"]),
            patch.object(stats, "save_signin_data", side_effect=save_signin),
            patch.object(stats, "save_stats", side_effect=save_group_stats),
            patch.object(stats, "_current_local_date", return_value=date(2026, 8, 20)),
        ):
            result = stats.signin(
                "group",
                "42",
                "Alice",
                signin_policy={"signin_mode": "steam"},
                database_adapter=apply_reward,
            )

        self.assertTrue(result["success"])
        self.assertEqual(115, result["database_balance"])
        self.assertEqual(["database", "signin_json", "stats_json"], order)
        self.assertEqual(115, stored["stats"]["users"]["42"]["points"])

    def test_steam_binding_record_failure_is_not_reported_as_success(self):
        steam_id = "76561198000000000"
        steam_module = self._steam_module(
            {"steam_id": steam_id, "user_id": "42"}
        )
        steam_module.record_steam_signin = lambda *_args, **_kwargs: {
            "success": False,
            "code": "write_failed",
            "message": "disk full",
        }

        with (
            patch.dict(sys.modules, {steam_module.__name__: steam_module}),
            patch.object(stats, "load_signin_data", return_value={}),
            patch.object(stats, "load_stats", return_value={"users": {}}),
            patch.object(stats, "save_signin_data"),
            patch.object(stats, "save_stats"),
            patch.object(
                stats,
                "_current_local_date",
                return_value=date(2026, 8, 20),
            ),
        ):
            result = stats.signin(
                "group",
                "42",
                "Alice",
                signin_policy={"signin_mode": "steam"},
                database_adapter=lambda *_args, **_kwargs: {
                    "success": True,
                    "code": "signed",
                    "balance": 115,
                },
            )

        self.assertFalse(result["success"])
        self.assertEqual("steam_binding_record_failed", result["code"])

    def test_database_adapter_type_error_is_not_retried_after_invocation(self):
        """A post-write adapter error must never trigger a second reward call."""
        steam_id = "76561198000000000"
        steam_module = self._steam_module({"steam_id": steam_id, "user_id": "42"})
        calls = []

        def adapter(received_steam_id, reward, **kwargs):
            calls.append((received_steam_id, reward, kwargs))
            raise TypeError("response construction failed after commit")

        with (
            patch.dict(sys.modules, {steam_module.__name__: steam_module}),
            patch.object(stats, "load_signin_data", return_value={}),
            patch.object(stats, "load_stats", return_value={"users": {}}),
            patch.object(stats, "save_signin_data") as save_signin,
            patch.object(stats, "save_stats") as save_stats,
            patch.object(stats, "_current_local_date", return_value=date(2026, 8, 20)),
        ):
            result = stats.signin(
                "group",
                "42",
                "Alice",
                signin_policy={"signin_mode": "steam"},
                database_adapter=adapter,
            )

        self.assertEqual("steam_database_error", result["code"])
        self.assertEqual(1, len(calls))
        save_signin.assert_not_called()
        save_stats.assert_not_called()

    def test_database_adapter_must_return_explicit_success_mapping(self):
        steam_module = self._steam_module(
            {"steam_id": "76561198000000000", "user_id": "42"}
        )

        def adapter(*_args, **_kwargs):
            return True

        with (
            patch.dict(sys.modules, {steam_module.__name__: steam_module}),
            patch.object(stats, "load_signin_data", return_value={}),
            patch.object(stats, "load_stats", return_value={"users": {}}),
            patch.object(stats, "save_signin_data") as save_signin,
            patch.object(stats, "save_stats") as save_stats,
            patch.object(stats, "_current_local_date", return_value=date(2026, 8, 20)),
        ):
            result = stats.signin(
                "group",
                "42",
                "Alice",
                signin_policy={"signin_mode": "steam"},
                database_adapter=adapter,
            )

        self.assertFalse(result["success"])
        self.assertEqual("invalid_database_response", result["code"])
        self.assertIn("未发放奖励", result["message"])
        save_signin.assert_not_called()
        save_stats.assert_not_called()

    def test_database_adapter_string_success_is_not_accepted(self):
        steam_module = self._steam_module(
            {"steam_id": "76561198000000000", "user_id": "42"}
        )

        def adapter(*_args, **_kwargs):
            return {"success": "true", "code": "signed"}

        with (
            patch.dict(sys.modules, {steam_module.__name__: steam_module}),
            patch.object(stats, "load_signin_data", return_value={}),
            patch.object(stats, "load_stats", return_value={"users": {}}),
            patch.object(stats, "save_signin_data") as save_signin,
            patch.object(stats, "save_stats") as save_stats,
            patch.object(stats, "_current_local_date", return_value=date(2026, 8, 20)),
        ):
            result = stats.signin(
                "group",
                "42",
                "Alice",
                signin_policy={"signin_mode": "steam"},
                database_adapter=adapter,
            )

        self.assertFalse(result["success"])
        self.assertEqual("signed", result["code"])
        save_signin.assert_not_called()
        save_stats.assert_not_called()

    def test_database_adapter_receives_period_mode_when_supported(self):
        steam_id = "76561198000000000"
        steam_module = self._steam_module({"steam_id": steam_id, "user_id": "42"})
        received = {}

        def adapter(received_steam_id, reward, *, player_name, sign_date, period_mode):
            received.update(
                steam_id=received_steam_id,
                reward=reward,
                player_name=player_name,
                sign_date=sign_date,
                period_mode=period_mode,
            )
            return {"success": False, "code": "already_signed", "message": "duplicate"}

        with (
            patch.dict(sys.modules, {steam_module.__name__: steam_module}),
            patch.object(stats, "load_signin_data", return_value={}),
            patch.object(stats, "load_stats", return_value={"users": {}}),
            patch.object(stats, "_current_local_date", return_value=date(2026, 8, 20)),
        ):
            result = stats.signin(
                "group",
                "42",
                "Alice",
                signin_policy={"signin_mode": "steam", "schedule_mode": "weekly"},
                database_adapter=adapter,
            )

        self.assertEqual("already_signed", result["code"])
        self.assertEqual("weekly", received["period_mode"])
        self.assertEqual("2026-08-20", received["sign_date"])

    def test_basic_signin_does_not_touch_injected_database_by_default(self):
        stored = {"signin": {}, "stats": {"users": {}}}
        adapter = Mock()

        with (
            patch.object(stats, "load_signin_data", side_effect=lambda _group: stored["signin"]),
            patch.object(stats, "load_stats", side_effect=lambda _group: stored["stats"]),
            patch.object(stats, "save_signin_data", side_effect=lambda _group, value: stored.__setitem__("signin", value)),
            patch.object(stats, "save_stats", side_effect=lambda _group, value: stored.__setitem__("stats", value)),
            patch.object(stats, "_current_local_date", return_value=date(2026, 8, 20)),
        ):
            result = stats.signin(
                "group",
                "42",
                "Alice",
                signin_policy={"signin_mode": "basic"},
                database_adapter=adapter,
            )

        self.assertTrue(result["success"])
        adapter.assert_not_called()

    def test_reset_steam_failure_does_not_write_local_mirror(self):
        steam_module = self._steam_module(
            {"steam_id": "76561198000000000", "user_id": "42"}
        )
        stored_signin = {
            "42": {
                "last_signin": "2026-08-20",
                "consecutive_days": 1,
                "total_days": 1,
                "signin_history": ["2026-08-20"],
            }
        }
        stored_stats = {"users": {"42": {"points": 10, "signin_count": 1, "total_signins": 1}}}

        def reject_reset(*_args, **_kwargs):
            return {"success": False, "code": "balance_changed", "message": "changed"}

        with (
            patch.dict(sys.modules, {steam_module.__name__: steam_module}),
            patch.object(stats, "load_signin_data", return_value=stored_signin),
            patch.object(stats, "load_stats", return_value=stored_stats),
            patch.object(stats, "save_signin_data") as save_signin,
            patch.object(stats, "save_stats") as save_stats,
        ):
            result = stats.reset_signin(
                "group",
                "42",
                "2026-08-20",
                signin_policy={"signin_mode": "steam"},
                database_adapter=reject_reset,
            )

        self.assertFalse(result["success"])
        self.assertEqual("balance_changed", result["code"])
        save_signin.assert_not_called()
        save_stats.assert_not_called()

    def test_reset_steam_success_updates_local_mirror_after_database(self):
        steam_module = self._steam_module(
            {"steam_id": "76561198000000000", "user_id": "42"}
        )
        stored = {
            "signin": {
                "42": {
                    "last_signin": "2026-08-20",
                    "consecutive_days": 1,
                    "total_days": 1,
                    "signin_history": ["2026-08-20"],
                }
            },
            "stats": {"users": {"42": {"points": 17, "signin_count": 1, "total_signins": 1}}},
        }
        order = []

        def reset_adapter(received_steam_id, received_date, *, remove_reward, currency_column):
            order.append("database")
            self.assertEqual("76561198000000000", received_steam_id)
            self.assertEqual("2026-08-20", received_date)
            self.assertTrue(remove_reward)
            self.assertEqual("amount", currency_column)
            return {"success": True, "code": "reset", "balance": 10, "reward": 7}

        def save_signin(_group, value):
            order.append("signin_json")
            stored["signin"] = value

        def save_stats(_group, value):
            order.append("stats_json")
            stored["stats"] = value

        with (
            patch.dict(sys.modules, {steam_module.__name__: steam_module}),
            patch.object(stats, "load_signin_data", side_effect=lambda _group: stored["signin"]),
            patch.object(stats, "load_stats", side_effect=lambda _group: stored["stats"]),
            patch.object(stats, "save_signin_data", side_effect=save_signin),
            patch.object(stats, "save_stats", side_effect=save_stats),
        ):
            result = stats.reset_signin(
                "group",
                "42",
                "2026-08-20",
                signin_policy={"signin_mode": "steam"},
                database_adapter=reset_adapter,
            )

        self.assertTrue(result["success"])
        self.assertEqual(["database", "signin_json", "stats_json"], order)
        self.assertEqual([], stored["signin"]["42"]["signin_history"])
        self.assertEqual(10, stored["stats"]["users"]["42"]["points"])
        self.assertEqual(0, stored["stats"]["users"]["42"]["signin_count"])

    def test_reset_record_only_does_not_reduce_local_points(self):
        steam_module = self._steam_module(
            {"steam_id": "76561198000000000", "user_id": "42"}
        )
        stored = {
            "signin": {
                "42": {
                    "last_signin": "2026-08-20",
                    "consecutive_days": 1,
                    "total_days": 1,
                    "signin_history": ["2026-08-20"],
                }
            },
            "stats": {"users": {"42": {"points": 17, "signin_count": 1, "total_signins": 1}}},
        }

        def reset_adapter(*_args, **kwargs):
            self.assertFalse(kwargs["remove_reward"])
            return {"success": True, "code": "reset_record_only", "balance": 17}

        with (
            patch.dict(sys.modules, {steam_module.__name__: steam_module}),
            patch.object(stats, "load_signin_data", side_effect=lambda _group: stored["signin"]),
            patch.object(stats, "load_stats", side_effect=lambda _group: stored["stats"]),
            patch.object(stats, "save_signin_data", side_effect=lambda _group, value: stored.__setitem__("signin", value)),
            patch.object(stats, "save_stats", side_effect=lambda _group, value: stored.__setitem__("stats", value)),
        ):
            result = stats.reset_signin(
                "group",
                "42",
                "2026-08-20",
                signin_policy={"signin_mode": "steam"},
                database_adapter=reset_adapter,
                remove_reward=False,
            )

        self.assertTrue(result["success"])
        self.assertEqual(17, stored["stats"]["users"]["42"]["points"])

    def test_websocket_reset_only_removes_local_record(self):
        stored = {
            "signin": {
                "42": {
                    "last_signin": "2026-08-20",
                    "consecutive_days": 1,
                    "total_days": 1,
                    "signin_history": ["2026-08-20"],
                }
            },
            "stats": {
                "users": {
                    "42": {"points": 17, "signin_count": 1, "total_signins": 1}
                }
            },
        }
        adapter = Mock()

        with (
            patch.object(stats, "load_signin_data", side_effect=lambda _group: stored["signin"]),
            patch.object(stats, "load_stats", side_effect=lambda _group: stored["stats"]),
            patch.object(stats, "save_signin_data", side_effect=lambda _group, value: stored.__setitem__("signin", value)),
            patch.object(stats, "save_stats", side_effect=lambda _group, value: stored.__setitem__("stats", value)),
        ):
            result = stats.reset_signin(
                "group",
                "42",
                "2026-08-20",
                signin_policy={"signin_mode": "steam", "steam_reward_mode": "websocket"},
                database_adapter=adapter,
            )

        self.assertTrue(result["success"])
        adapter.assert_not_called()
        self.assertEqual("", stored["signin"]["42"]["last_signin"])
        self.assertEqual(17, stored["stats"]["users"]["42"]["points"])

    def test_configured_profiles_have_independent_claim_windows(self):
        """Two portal sign-in schemes may both be used in one calendar day."""

        stored = {"signin": {}, "stats": {"users": {"42": {"points": 0}}}}

        def load_signin(_group):
            return stored["signin"]

        def load_group_stats(_group):
            return stored["stats"]

        def save_signin(_group, value):
            stored["signin"] = value

        def save_group_stats(_group, value):
            stored["stats"] = value

        policy_a = {"profile_id": "daily", "schedule_mode": "daily"}
        policy_b = {"profile_id": "weekly", "schedule_mode": "weekly"}
        with (
            patch.object(stats, "load_signin_data", side_effect=load_signin),
            patch.object(stats, "load_stats", side_effect=load_group_stats),
            patch.object(stats, "save_signin_data", side_effect=save_signin),
            patch.object(stats, "save_stats", side_effect=save_group_stats),
            patch.object(stats, "_current_local_date", return_value=date(2026, 8, 19)),
        ):
            first = stats.signin("group", "42", "Alice", signin_policy=policy_a)
            second = stats.signin("group", "42", "Alice", signin_policy=policy_b)
            duplicate = stats.signin("group", "42", "Alice", signin_policy=policy_a)

        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertFalse(duplicate["success"])
        self.assertEqual(20, stored["stats"]["users"]["42"]["points"])
        profile_records = stored["signin"]["42"][stats.SIGNIN_PROFILE_RECORDS_KEY]
        self.assertEqual({"daily", "weekly"}, set(profile_records))
        self.assertEqual(1, stored["signin"]["42"]["total_days"])

    def test_preview_signin_reward_reads_profile_without_writing(self):
        signin_data = {
            "42": {
                "profile_records": {
                    "steam": {
                        "last_signin": "2026-08-18",
                        "consecutive_days": 2,
                        "total_days": 2,
                    }
                }
            }
        }
        with (
            patch.object(stats, "load_signin_data", return_value=signin_data),
            patch.object(stats, "_current_local_date", return_value=date(2026, 8, 19)),
        ):
            result = stats.preview_signin_reward(
                "group",
                "42",
                reward_config={"base_reward": 10, "reward_3_days": 55},
                signin_policy={"profile_id": "steam", "schedule_mode": "daily"},
            )

        self.assertTrue(result["success"])
        self.assertEqual(55, result["reward"])
        self.assertEqual(3, result["consecutive_days"])
        self.assertEqual("2026-08-19", result["sign_date"])
        self.assertEqual("2026-08-18", signin_data["42"]["profile_records"]["steam"]["last_signin"])

    def test_signin_preserves_existing_record_fields_and_updates_streak(self):
        signin_data = {
            "42": {
                "last_signin": "2024-03-10",
                "consecutive_days": "2",
                "total_days": "8",
                "user_name": "Old name",
                "legacy_note": "keep",
            }
        }
        group_stats = {
            "users": {
                "42": {
                    "points": "15",
                    "signin_count": "8",
                    "total_signins": "8",
                    "user_name": "Old name",
                    "joined_at": 123,
                    "legacy_metric": 9,
                }
            }
        }
        saved = {}

        with (
            patch.object(stats, "load_signin_data", return_value=signin_data),
            patch.object(stats, "load_stats", return_value=group_stats),
            patch.object(stats, "save_signin_data", side_effect=lambda _, value: saved.setdefault("signin", value)),
            patch.object(stats, "save_stats", side_effect=lambda _, value: saved.setdefault("stats", value)),
            patch.object(stats, "_current_local_date", return_value=date(2024, 3, 11)),
        ):
            result = stats.signin("group", "42", "Alice")

        self.assertTrue(result["success"])
        self.assertEqual(3, result["consecutive_days"])
        self.assertEqual(45, result["points"])
        self.assertEqual("2024-03-11", saved["signin"]["42"]["last_signin"])
        self.assertEqual("keep", saved["signin"]["42"]["legacy_note"])
        self.assertEqual(9, saved["stats"]["users"]["42"]["legacy_metric"])

    def test_duplicate_signin_does_not_write_or_increment(self):
        signin_data = {
            "42": {
                "last_signin": "2024-03-11",
                "consecutive_days": 3,
                "total_days": 9,
            }
        }
        group_stats = {"users": {"42": {"points": 30}}}

        with (
            patch.object(stats, "load_signin_data", return_value=signin_data),
            patch.object(stats, "load_stats", return_value=group_stats),
            patch.object(stats, "save_signin_data") as save_signin,
            patch.object(stats, "save_stats") as save_stats,
            patch.object(stats, "_current_local_date", return_value=date(2024, 3, 11)),
        ):
            result = stats.signin("group", "42")

        self.assertFalse(result["success"])
        self.assertEqual(3, result["consecutive_days"])
        save_signin.assert_not_called()
        save_stats.assert_not_called()


class TestExtendedSigninData(unittest.TestCase):
    def test_signin_keeps_a_bounded_recent_history(self):
        current_date = date(2026, 8, 15)
        oldest_date = current_date - timedelta(days=stats.SIGNIN_HISTORY_LIMIT)
        history = [
            (oldest_date + timedelta(days=offset)).isoformat()
            for offset in range(stats.SIGNIN_HISTORY_LIMIT)
        ]
        signin_data = {
            "42": {
                "last_signin": history[-1],
                "consecutive_days": stats.SIGNIN_HISTORY_LIMIT,
                "total_days": stats.SIGNIN_HISTORY_LIMIT,
                "signin_history": history,
            }
        }
        group_stats = {"users": {"42": {"points": 0}}}
        saved = {}

        with (
            patch.object(stats, "load_signin_data", return_value=signin_data),
            patch.object(stats, "load_stats", return_value=group_stats),
            patch.object(
                stats,
                "save_signin_data",
                side_effect=lambda _, value: saved.setdefault("signin", value),
            ),
            patch.object(
                stats,
                "save_stats",
                side_effect=lambda _, value: saved.setdefault("stats", value),
            ),
            patch.object(stats, "_current_local_date", return_value=current_date),
        ):
            result = stats.signin("group", "42")

        saved_history = saved["signin"]["42"]["signin_history"]
        self.assertTrue(result["success"])
        self.assertEqual(stats.SIGNIN_HISTORY_LIMIT, len(saved_history))
        self.assertEqual((oldest_date + timedelta(days=1)).isoformat(), saved_history[0])
        self.assertEqual(current_date.isoformat(), saved_history[-1])

    def test_monthly_summary_uses_normalized_history_and_current_status(self):
        signin_data = {
            "42": {
                "last_signin": "2026-08-15",
                "consecutive_days": 3,
                "total_days": 11,
                "signin_history": [
                    "2026-08-03",
                    "invalid",
                    "2026-07-31",
                    "2026-08-01",
                    "2026-08-03",
                ],
            }
        }

        with patch.object(stats, "load_signin_data", return_value=signin_data):
            summary = stats.get_monthly_signin_summary(
                "group", "42", date(2026, 8, 15)
            )

        self.assertEqual(
            {
                "month": "2026-08",
                "signin_days": 3,
                "dates": ["2026-08-01", "2026-08-03", "2026-08-15"],
                "signed_today": True,
                "consecutive_days": 3,
                "total_days": 11,
            },
            summary,
        )

    def test_signin_ranking_uses_total_days_with_legacy_stats_fallback(self):
        signin_data = {
            "u1": {
                "total_days": 8,
                "consecutive_days": 4,
                "last_signin": "2026-08-15",
                "user_name": "Alice",
            },
            "u2": {
                "total_days": "8",
                "consecutive_days": "4",
                "last_signin": "2026-08-14",
            },
            "u3": {
                "total_days": 10,
                "consecutive_days": 1,
                "last_signin": "2026-08-10",
                "user_name": "Carol",
            },
        }
        group_stats = {
            "users": {
                "u2": {"user_name": "Bob"},
                "u4": {"user_name": "Legacy", "total_signins": "6"},
            }
        }

        with (
            patch.object(stats, "load_signin_data", return_value=signin_data),
            patch.object(stats, "load_stats", return_value=group_stats),
        ):
            ranking = stats.get_signin_ranking("group")
            no_entries = stats.get_signin_ranking("group", 0)

        self.assertEqual([], no_entries)
        self.assertEqual(
            [
                {
                    "rank": 1,
                    "user_id": "u3",
                    "user_name": "Carol",
                    "total_days": 10,
                    "consecutive_days": 1,
                    "last_signin": "2026-08-10",
                },
                {
                    "rank": 2,
                    "user_id": "u1",
                    "user_name": "Alice",
                    "total_days": 8,
                    "consecutive_days": 4,
                    "last_signin": "2026-08-15",
                },
                {
                    "rank": 3,
                    "user_id": "u2",
                    "user_name": "Bob",
                    "total_days": 8,
                    "consecutive_days": 4,
                    "last_signin": "2026-08-14",
                },
                {
                    "rank": 4,
                    "user_id": "u4",
                    "user_name": "Legacy",
                    "total_days": 6,
                    "consecutive_days": 0,
                    "last_signin": "",
                },
            ],
            ranking,
        )


class TestSigninRewardPolicy(unittest.TestCase):
    def test_default_reward_tiers_remain_unchanged(self):
        self.assertEqual(10, stats.get_signin_reward(1))
        self.assertEqual(30, stats.get_signin_reward(3))
        self.assertEqual(100, stats.get_signin_reward(7))
        self.assertEqual(200, stats.get_signin_reward(14))
        self.assertEqual(500, stats.get_signin_reward(30))

    def test_reward_policy_normalizes_invalid_values_without_negative_rewards(self):
        policy = stats.normalize_signin_reward_config(
            {
                "base_reward": -1,
                "reward_3_days": "invalid",
                "reward_7_days": True,
                "reward_14_days": stats.MAX_SIGNIN_REWARD + 1,
                "reward_30_days": "750",
            }
        )

        self.assertEqual(
            {
                "base_reward": 10,
                "reward_3_days": 30,
                "reward_7_days": 100,
                "reward_14_days": 200,
                "reward_30_days": 750,
            },
            policy,
        )
        self.assertEqual(12, stats.get_signin_reward(1, {"base_reward": "12"}))
        self.assertEqual(0, stats.get_signin_reward(3, {"reward_3_days": 0}))

    def test_signin_applies_custom_reward_policy(self):
        signin_data = {
            "42": {
                "last_signin": "2024-03-10",
                "consecutive_days": 2,
                "total_days": 8,
            }
        }
        group_stats = {"users": {"42": {"points": 15}}}
        saved = {}

        with (
            patch.object(stats, "load_signin_data", return_value=signin_data),
            patch.object(stats, "load_stats", return_value=group_stats),
            patch.object(
                stats,
                "save_signin_data",
                side_effect=lambda _, value: saved.setdefault("signin", value),
            ),
            patch.object(
                stats,
                "save_stats",
                side_effect=lambda _, value: saved.setdefault("stats", value),
            ),
            patch.object(stats, "_current_local_date", return_value=date(2024, 3, 11)),
        ):
            result = stats.signin(
                "group", "42", reward_config={"reward_3_days": "55"}
            )

        self.assertTrue(result["success"])
        self.assertEqual(55, result["reward"])
        self.assertEqual(70, saved["stats"]["users"]["42"]["points"])


class TestMemberDisplayNames(unittest.TestCase):
    def test_set_user_name_initializes_zero_count_records(self):
        stored = {"stats": {}, "signin": {}}

        def update(_group_id, key, updater):
            stored[key] = updater(stored[key])

        with patch.object(stats, "update_group_data", side_effect=update):
            self.assertTrue(stats.set_user_name("group", "42", "Alice"))

        self.assertEqual("Alice", stored["stats"]["users"]["42"]["user_name"])
        self.assertIn("joined_at", stored["stats"]["users"]["42"])
        self.assertEqual(
            {
                "last_signin": "",
                "consecutive_days": 0,
                "total_days": 0,
                "signin_history": [],
                "user_name": "Alice",
            },
            stored["signin"]["42"],
        )

    def test_signin_keeps_initialized_name_when_event_has_no_name(self):
        signin_data = {
            "42": {
                "user_name": "Alice",
                "last_signin": "",
                "consecutive_days": 0,
                "total_days": 0,
            }
        }
        group_stats = {"users": {"42": {"user_name": "Alice"}}}
        saved = {}

        with (
            patch.object(stats, "load_signin_data", return_value=signin_data),
            patch.object(stats, "load_stats", return_value=group_stats),
            patch.object(stats, "save_signin_data", side_effect=lambda _, value: saved.setdefault("signin", value)),
            patch.object(stats, "save_stats", side_effect=lambda _, value: saved.setdefault("stats", value)),
            patch.object(stats, "_current_local_date", return_value=date(2026, 8, 19)),
        ):
            result = stats.signin("group", "42", "")

        self.assertTrue(result["success"])
        self.assertEqual("Alice", saved["signin"]["42"]["user_name"])
        self.assertEqual("Alice", saved["stats"]["users"]["42"]["user_name"])


if __name__ == "__main__":
    unittest.main()
