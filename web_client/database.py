"""SQLAlchemy database setup for MySQL.

The engine is created lazily so importing utility modules does not attempt a
database connection.  This also keeps unit testing straightforward.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Generator

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import Settings


class Base(DeclarativeBase):
    """Base class for all portal tables."""


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = Settings.from_env()
    options = {
        "pool_pre_ping": True,
        "future": True,
    }
    if settings.database_url.startswith("mysql"):
        # Fail fast when MySQL is unavailable instead of holding a browser
        # request for SQLAlchemy's long default pool timeout.  This keeps the
        # portal responsive during a database restart and lets the health
        # endpoint report the real dependency failure quickly.
        options.update(
            {
                "pool_recycle": 1800,
                "pool_size": settings.database_pool_size,
                "max_overflow": settings.database_max_overflow,
                "pool_timeout": settings.database_pool_timeout,
                # Do not let a row lock turn a normal form save into a proxy
                # timeout.  A short, explicit failure can be retried safely.
                "connect_args": {
                    "init_command": "SET SESSION innodb_lock_wait_timeout=5"
                },
            }
        )
    elif settings.database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    return create_engine(settings.database_url, **options)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autocommit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def init_database() -> None:
    """Create portal tables; safe to run repeatedly during deployment."""
    # Import for model registration before metadata is materialized.
    from . import models  # noqa: F401

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _migrate_database(engine)


def _migrate_database(engine: Engine) -> None:
    """Apply small additive migrations for portals created by older builds."""
    inspector = inspect(engine)
    if "portal_users" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("portal_users")}
    if "role" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE portal_users ADD COLUMN role "
                    "VARCHAR(16) NOT NULL DEFAULT 'customer'"
                )
            )
    if "email" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE portal_users ADD COLUMN email VARCHAR(320) NULL")
            )

    if "can_view_console" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE portal_users ADD COLUMN can_view_console "
                    "BOOLEAN NOT NULL DEFAULT 0"
                )
            )

    if "portal_group_client_sync" in inspector.get_table_names():
        client_sync_columns = {
            column["name"]
            for column in inspect(engine).get_columns("portal_group_client_sync")
        }
        if "pull_requested" not in client_sync_columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE portal_group_client_sync "
                        "ADD COLUMN pull_requested BOOLEAN NOT NULL DEFAULT 0"
                    )
                )

    # ``create_all`` cannot add the user-supplied mini-program/WebSocket card
    # fields to an existing portal.  Keep them separate from the JG heartbeat
    # card so either credential can be rotated without changing the other.
    if "portal_game_authorizations" in inspector.get_table_names():
        game_auth_columns = {
            column["name"]
            for column in inspect(engine).get_columns("portal_game_authorizations")
        }
        game_auth_additions = {
            "websocket_key_ciphertext": "VARCHAR(2048) NOT NULL DEFAULT ''",
            "websocket_key_mask": "VARCHAR(80) NOT NULL DEFAULT ''",
            "websocket_state": "VARCHAR(16) NOT NULL DEFAULT 'missing'",
            "websocket_verified_at": "BIGINT NOT NULL DEFAULT 0",
            "websocket_validation_version": "VARCHAR(16) NOT NULL DEFAULT ''",
        }
        with engine.begin() as connection:
            for name, definition in game_auth_additions.items():
                if name not in game_auth_columns:
                    connection.execute(
                        text(
                            "ALTER TABLE portal_game_authorizations "
                            f"ADD COLUMN {name} {definition}"
                        )
                    )

        # Releases before the credential split stored the externally issued
        # IM3 card beside the owner-generated game heartbeat.  Copy it once
        # into the dedicated table.  The legacy columns are intentionally not
        # deleted here so an interrupted upgrade can be retried safely.
        if "portal_im3_authorizations" in inspector.get_table_names():
            from .models import GameAuthorization, Im3Authorization

            with Session(engine) as session:
                legacy_rows = session.scalars(
                    select(GameAuthorization).where(
                        GameAuthorization.websocket_key_ciphertext != ""
                    )
                ).all()
                changed = False
                for legacy in legacy_rows:
                    group_id = str(legacy.group_id or "").strip()
                    if not group_id:
                        continue
                    current = session.scalar(
                        select(Im3Authorization).where(
                            Im3Authorization.group_id == group_id
                        )
                    )
                    if current is None:
                        current = Im3Authorization(group_id=group_id)
                        session.add(current)
                    if not current.key_ciphertext:
                        # The old field reused the game heartbeat client ID;
                        # it is not an IM3 credential scope.
                        current.client_id = ""
                        current.key_ciphertext = str(
                            legacy.websocket_key_ciphertext or ""
                        )[:2048]
                        current.key_mask = str(legacy.websocket_key_mask or "")[:80]
                        current.state = str(legacy.websocket_state or "missing")[:16]
                        current.verified_at = max(
                            0, int(legacy.websocket_verified_at or 0)
                        )
                        current.validation_version = str(
                            legacy.websocket_validation_version or ""
                        )[:16]
                        changed = True
                if changed:
                    session.commit()

    user_indexes = {
        index["name"] for index in inspect(engine).get_indexes("portal_users")
    }
    email_index = "ix_portal_users_email"
    if email_index not in user_indexes:
        with engine.begin() as connection:
            connection.execute(
                text(f"CREATE UNIQUE INDEX {email_index} ON portal_users (email)")
            )

    # ``create_all`` cannot add fields to a runtime-event table that already
    # exists.  These additive columns preserve all older action records while
    # allowing newly opted-in message tracking to show direction, a bounded
    # preview, and the sender name captured by the Windows plugin.
    if "portal_console_events" in inspector.get_table_names():
        console_columns = {
            column["name"]
            for column in inspect(engine).get_columns("portal_console_events")
        }
        console_additions = {
            "direction": "VARCHAR(16) NOT NULL DEFAULT ''",
            "message_content": "VARCHAR(2048) NOT NULL DEFAULT ''",
            "user_name": "VARCHAR(128) NOT NULL DEFAULT ''",
            "message_payload": "JSON NULL",
        }
        with engine.begin() as connection:
            for name, definition in console_additions.items():
                if name not in console_columns:
                    connection.execute(
                        text(
                            "ALTER TABLE portal_console_events "
                            f"ADD COLUMN {name} {definition}"
                        )
                    )

    if "portal_group_config_snapshots" not in inspector.get_table_names():
        return
    snapshot_columns = {
        column["name"]
        for column in inspect(engine).get_columns("portal_group_config_snapshots")
    }
    additions = {
        "applied_revision": "INTEGER NOT NULL DEFAULT 1",
        "sync_status": "VARCHAR(16) NOT NULL DEFAULT 'synced'",
        "sync_error": "VARCHAR(255) NOT NULL DEFAULT ''",
        "last_seen_at": "DATETIME NULL",
        "client_id": "VARCHAR(64) NOT NULL DEFAULT ''",
        "auth_state": "VARCHAR(16) NOT NULL DEFAULT 'missing'",
        "auth_expire": "BIGINT NOT NULL DEFAULT 0",
        "auth_expire_text": "VARCHAR(64) NOT NULL DEFAULT '无授权记录'",
        "auth_card_fingerprint": "VARCHAR(64) NOT NULL DEFAULT ''",
        "auth_card_mask": "VARCHAR(64) NOT NULL DEFAULT ''",
        "auth_activated_at": "BIGINT NOT NULL DEFAULT 0",
        "auth_user_id": "VARCHAR(32) NOT NULL DEFAULT ''",
        "refresh_requested": "BOOLEAN NOT NULL DEFAULT 0",
        "runtime_json": "JSON NULL",
        "runtime_updated_at": "DATETIME NULL",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in snapshot_columns:
                connection.execute(
                    text(
                        f"ALTER TABLE portal_group_config_snapshots "
                        f"ADD COLUMN {name} {definition}"
                    )
                )

    # ``create_all`` creates model indexes only for new tables. Older portal
    # databases receive the fingerprint column above, so add its lookup index
    # explicitly as an idempotent migration. Remote card registration queries
    # this field on every claim attempt.
    snapshot_indexes = {
        index["name"]
        for index in inspect(engine).get_indexes("portal_group_config_snapshots")
    }
    fingerprint_index = "ix_portal_group_config_snapshots_auth_card_fingerprint"
    if fingerprint_index not in snapshot_indexes:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE INDEX {fingerprint_index} "
                    "ON portal_group_config_snapshots (auth_card_fingerprint)"
                )
            )
