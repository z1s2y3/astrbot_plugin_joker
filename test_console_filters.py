"""Focused regression coverage for the group CMD runtime view."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from web_client import remote_store
from web_client.database import Base, _migrate_database
from web_client.models import GroupConfigSnapshot, PortalConsoleEvent


class ConsoleFilterTests(unittest.TestCase):
    def _engine_with_events(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add_all(
                (
                    PortalConsoleEvent(
                        event_id="console-filter-actor-a",
                        client_id="desktop",
                        group_id="1102579807",
                        action="receive",
                        user_id="100001",
                        user_name="Alice",
                        detail="daily check-in",
                        message_content="welcome Alice",
                        occurred_at=datetime(2026, 8, 19, 1, 0, 0),
                    ),
                    PortalConsoleEvent(
                        event_id="console-filter-actor-b",
                        client_id="desktop",
                        group_id="1102579807",
                        action="auto_reply",
                        user_id="200002",
                        user_name="Bob",
                        detail="reply matched",
                        message_content="hello from bot",
                        occurred_at=datetime(2026, 8, 19, 2, 0, 0),
                    ),
                    PortalConsoleEvent(
                        event_id="console-filter-other-group",
                        client_id="desktop",
                        group_id="568586484",
                        action="receive",
                        user_id="100001",
                        user_name="Alice",
                        occurred_at=datetime(2026, 8, 19, 3, 0, 0),
                    ),
                )
            )
            db.commit()
        return engine

    def test_filters_are_group_scoped_and_composable(self):
        engine = self._engine_with_events()
        try:
            with Session(engine) as db:
                self.assertEqual(
                    ["console-filter-actor-a"],
                    [
                        event.event_id
                        for event in remote_store.list_console_events(
                            db,
                            "1102579807",
                            "check-in",
                            user_id="100001",
                            user_name="Ali",
                            start_at=datetime(2026, 8, 19, 0, 30, 0),
                            end_at=datetime(2026, 8, 19, 1, 30, 0),
                        )
                    ],
                )
                self.assertEqual(
                    ["console-filter-actor-b"],
                    [
                        event.event_id
                        for event in remote_store.list_console_events(
                            db, "1102579807", user_name="Bob"
                        )
                    ],
                )
                self.assertEqual(
                    [],
                    remote_store.list_console_events(
                        db, "1102579807", user_id="not-a-qq"
                    ),
                )
                self.assertEqual(
                    [],
                    remote_store.list_console_events(
                        db, "1102579807", start_at="not-a-time"
                    ),
                )
        finally:
            engine.dispose()

    def test_ingest_preserves_the_windows_sender_name(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        try:
            with Session(engine) as db:
                db.add(
                    GroupConfigSnapshot(
                        group_id="1102579807",
                        client_id="desktop",
                        config_json={"group_id": "1102579807", "features": {}},
                    )
                )
                db.flush()
                accepted = remote_store.ingest_console_events(
                    db,
                    "desktop",
                    [
                        {
                            "event_id": "console-name-ingest-001",
                            "group_id": "1102579807",
                            "action": "receive",
                            "user_id": "100001",
                            "user_name": "Alice",
                            "timestamp": 1_787_000_000,
                        }
                    ],
                )
                self.assertEqual(1, accepted)
                event = db.query(PortalConsoleEvent).one()
                self.assertEqual("Alice", event.user_name)
        finally:
            engine.dispose()

    def test_legacy_console_table_receives_sender_name_column(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE portal_users ("
                    "id INTEGER PRIMARY KEY, username VARCHAR(64), "
                    "password_hash VARCHAR(255), is_active BOOLEAN)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE portal_console_events ("
                    "id INTEGER PRIMARY KEY, event_id VARCHAR(80), "
                    "client_id VARCHAR(64), group_id VARCHAR(32), "
                    "action VARCHAR(64), user_id VARCHAR(32), "
                    "message_id VARCHAR(64), message_type VARCHAR(128), "
                    "detail VARCHAR(160), occurred_at DATETIME, created_at DATETIME)"
                )
            )

        try:
            _migrate_database(engine)
            columns = {
                column["name"]
                for column in inspect(engine).get_columns("portal_console_events")
            }
            self.assertIn("user_name", columns)
        finally:
            engine.dispose()

    def test_timezone_aware_api_filters_normalize_to_utc(self):
        engine = self._engine_with_events()
        try:
            with Session(engine) as db:
                events = remote_store.list_console_events(
                    db,
                    "1102579807",
                    start_at=datetime(2026, 8, 19, 9, 0, 0, tzinfo=timezone.utc),
                )
                self.assertEqual([], events)
        finally:
            engine.dispose()

    def test_count_and_offset_share_the_same_group_filters(self):
        engine = self._engine_with_events()
        try:
            with Session(engine) as db:
                self.assertEqual(
                    2,
                    remote_store.count_console_events(db, "1102579807"),
                )
                self.assertEqual(
                    ["console-filter-actor-a"],
                    [
                        event.event_id
                        for event in remote_store.list_console_events(
                            db, "1102579807", limit=1, offset=1
                        )
                    ],
                )
                self.assertEqual(
                    1,
                    remote_store.count_console_events(
                        db, "1102579807", user_name="Alice"
                    ),
                )
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
