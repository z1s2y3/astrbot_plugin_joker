"""Focused tests for the portal runtime-log cleanup helper."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from web_client.database import Base
from web_client.models import PortalConsoleEvent
from web_client.remote_store import purge_console_events


class ConsoleLogCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.now = datetime(2026, 1, 10, 12, 0, 0)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _seed(self) -> None:
        with Session(self.engine) as db:
            db.add_all(
                [
                    PortalConsoleEvent(
                        event_id="event-old-send-1",
                        client_id="windows-client",
                        group_id="12345",
                        action="发送",
                        message_type="text",
                        occurred_at=self.now - timedelta(days=3),
                    ),
                    PortalConsoleEvent(
                        event_id="event-old-recall-1",
                        client_id="windows-client",
                        group_id="12345",
                        action="撤回",
                        message_type="text",
                        occurred_at=self.now - timedelta(days=3),
                    ),
                    PortalConsoleEvent(
                        event_id="event-old-send-2",
                        client_id="windows-client",
                        group_id="67890",
                        action="发送",
                        message_type="text",
                        occurred_at=self.now - timedelta(days=3),
                    ),
                    PortalConsoleEvent(
                        event_id="event-recent-send",
                        client_id="windows-client",
                        group_id="12345",
                        action="发送",
                        message_type="text",
                        occurred_at=self.now - timedelta(days=1),
                    ),
                ]
            )
            db.commit()

    def test_purge_is_scoped_by_days_action_and_group(self) -> None:
        self._seed()
        with Session(self.engine) as db:
            removed = purge_console_events(
                db,
                2,
                action="发送",
                group_id="12345",
                now=self.now,
            )
            self.assertEqual(removed, 1)
            db.commit()

        with Session(self.engine) as db:
            remaining = list(
                db.scalars(
                    select(PortalConsoleEvent).order_by(PortalConsoleEvent.event_id)
                )
            )
            self.assertEqual(
                [event.event_id for event in remaining],
                [
                    "event-old-recall-1",
                    "event-old-send-2",
                    "event-recent-send",
                ],
            )

    def test_purge_supports_message_type_and_aware_reference_time(self) -> None:
        self._seed()
        aware_now = self.now.replace(tzinfo=timezone.utc)
        with Session(self.engine) as db:
            removed = purge_console_events(
                db,
                "2",
                message_type="text",
                group_id="67890",
                now=aware_now,
            )
            self.assertEqual(removed, 1)
            db.rollback()
            # The helper leaves transaction ownership to its caller.
            self.assertEqual(
                db.scalar(
                    select(PortalConsoleEvent.event_id).where(
                        PortalConsoleEvent.event_id == "event-old-send-2"
                    )
                ),
                "event-old-send-2",
            )

    def test_purge_rejects_unsafe_parameters(self) -> None:
        self._seed()
        invalid_values = (0, -1, True, "1.5", "999999")
        for value in invalid_values:
            with self.subTest(value=value):
                with Session(self.engine) as db:
                    with self.assertRaises(ValueError):
                        purge_console_events(db, value, now=self.now)

        with Session(self.engine) as db:
            with self.assertRaises(ValueError):
                purge_console_events(db, 2, action="发送\n撤回", now=self.now)
            with self.assertRaises(ValueError):
                purge_console_events(db, 2, group_id="not-a-group", now=self.now)


if __name__ == "__main__":
    unittest.main()
