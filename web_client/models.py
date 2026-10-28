"""Database models for customer identities, ownership, and audit history."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGBLOB, MEDIUMBLOB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    # MySQL DATETIME is timezone-naive; always store a naive UTC value.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "portal_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Email is the independent identity used for customer registration.  It is
    # deliberately not coupled to a group card because cards can be renewed.
    # Existing administrators created before email registration remain valid.
    email: Mapped[str | None] = mapped_column(
        String(320), unique=True, index=True, nullable=True
    )
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(
        String(16), default="customer", server_default="customer", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Customer-level permission for the remote CMD/runtime view.  Administrators
    # are allowed whenever the portal-wide switch is enabled; this field lets
    # an administrator grant or revoke the same view for each customer.
    can_view_console: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    groups: Mapped[list["GroupAccess"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class GroupAccess(Base):
    __tablename__ = "portal_group_access"
    __table_args__ = (
        UniqueConstraint("user_id", "group_id", name="uq_portal_user_group"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True
    )
    # Tenant boundary: one QQ group belongs to exactly one customer account.
    group_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(16), default="owner", nullable=False)
    card_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    card_mask: Mapped[str] = mapped_column(String(64), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="groups")


class GroupConfigSnapshot(Base):
    __tablename__ = "portal_group_config_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    applied_revision: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("portal_users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    sync_status: Mapped[str] = mapped_column(
        String(16), default="synced", server_default="synced", nullable=False
    )
    sync_error: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    client_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    auth_state: Mapped[str] = mapped_column(
        String(16), default="missing", server_default="missing", nullable=False
    )
    auth_expire: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    auth_expire_text: Mapped[str] = mapped_column(
        String(64), default="无授权记录", server_default="无授权记录", nullable=False
    )
    auth_card_fingerprint: Mapped[str] = mapped_column(
        String(64), default="", index=True, nullable=False
    )
    auth_card_mask: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    auth_activated_at: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    auth_user_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    # A customer login can ask the next Windows heartbeat to re-upload the
    # current projection.  The portal never initiates a connection to the
    # Windows machine; this is a durable, pull-only request consumed by the
    # next outbound exchange.
    refresh_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    # Read-only runtime projection sent by the Windows bot.  It is kept
    # separate from config_json so a web save can never erase blacklist,
    # statistics, kick counters, or other live records.
    runtime_json: Mapped[dict | None] = mapped_column(JSON, default=dict, nullable=True)
    runtime_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )


class GroupClientSync(Base):
    """Per-installation ACK cursor for a group-authorized web projection."""

    __tablename__ = "portal_group_client_sync"
    __table_args__ = (
        UniqueConstraint("group_id", "client_id", name="uq_portal_group_client_sync"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    applied_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_error: Mapped[str] = mapped_column(String(255), default="", nullable=False)


class GameAuthorization(Base):
    """The owner-generated game license and its Windows heartbeat."""

    __tablename__ = "portal_game_authorizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(64), default="", index=True, nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), default="missing", server_default="missing", nullable=False
    )
    expire: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    card_fingerprint: Mapped[str] = mapped_column(
        String(64), default="", index=True, nullable=False
    )
    card_mask: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    activated_at: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    # These columns were used by older releases for the IM3 card.  They remain
    # nullable-in-practice compatibility storage so an upgrade can migrate
    # existing rows without losing a credential.  New code must use
    # ``Im3Authorization`` and must never write these fields.
    websocket_key_ciphertext: Mapped[str] = mapped_column(
        String(2048), default="", nullable=False
    )
    websocket_key_mask: Mapped[str] = mapped_column(
        String(80), default="", nullable=False
    )
    websocket_state: Mapped[str] = mapped_column(
        String(16), default="missing", server_default="missing", nullable=False
    )
    websocket_verified_at: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    websocket_validation_version: Mapped[str] = mapped_column(
        String(16), default="", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class Im3Authorization(Base):
    """One group's externally issued IM3 website card.

    This is intentionally separate from :class:`GameAuthorization`: the IM3
    card belongs to another service and grants no game permission by itself.
    The owner-generated game card and its heartbeat are checked independently
    before game features or IM3-backed game operations are allowed.
    """

    __tablename__ = "portal_im3_authorizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(64), default="", index=True, nullable=False)
    key_ciphertext: Mapped[str] = mapped_column(
        String(2048), default="", nullable=False
    )
    key_mask: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), default="missing", server_default="missing", nullable=False
    )
    verified_at: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    validation_version: Mapped[str] = mapped_column(
        String(16), default="", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class AuditLog(Base):
    __tablename__ = "portal_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    group_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    changes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    user_agent: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PortalFeedback(Base):
    """A customer support item stored only by the Linux portal.

    Feedback is deliberately not included in the Windows configuration
    projection.  A slow or offline Windows AstrBot must never prevent a
    customer from reporting a portal problem or an administrator from
    recording the resolution.
    """

    __tablename__ = "portal_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Optional: a report can concern the portal generally rather than a
    # particular QQ group.  We retain the literal group number after a group
    # access row is removed so the support record stays useful.
    group_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    category: Mapped[str] = mapped_column(
        String(32), default="other", server_default="other", nullable=False
    )
    subject: Mapped[str] = mapped_column(String(160), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="new", server_default="new", index=True, nullable=False
    )
    admin_reply: Mapped[str] = mapped_column(Text, default="", nullable=False)
    handled_by: Mapped[int | None] = mapped_column(
        ForeignKey("portal_users.id", ondelete="SET NULL"), nullable=True
    )
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Delivery is asynchronous.  Keeping its result alongside the report
    # lets the administrator see whether a notification was sent without
    # making the customer wait for SMTP.
    email_state: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", nullable=False
    )
    email_error: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, index=True, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class LoginAttempt(Base):
    __tablename__ = "portal_login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity: Mapped[str] = mapped_column(String(128), index=True)
    ip_address: Mapped[str] = mapped_column(String(64), index=True)
    successful: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PortalSetting(Base):
    """Small portal-only settings that must not be written to AstrBot config."""

    __tablename__ = "portal_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("portal_users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class PortalThemeAsset(Base):
    """A portal-only background image stored centrally in the database.

    The image is never included in the Windows synchronization projection.
    ``MEDIUMBLOB`` is required on MySQL because a normal ``BLOB`` is limited
    to 64 KiB.  The portal accepts a large source upload but stores a
    browser-optimized WebP rendition that stays inside MEDIUMBLOB.
    """

    __tablename__ = "portal_theme_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    image_data: Mapped[bytes] = mapped_column(
        LargeBinary().with_variant(MEDIUMBLOB, "mysql"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    # The owner can opt in to let other signed-in portal users select this
    # image.  It remains visible to the administrator in the central backend.
    is_shared: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class PortalThemePreference(Base):
    """One day/night background selection per portal account."""

    __tablename__ = "portal_theme_preferences"

    # A primary-key foreign key makes the user-to-preference relationship
    # exactly one-to-one without an extra surrogate ID.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), primary_key=True
    )
    # ``auto`` follows local browser time; explicit day/night modes let a
    # customer lock a preferred appearance.
    mode: Mapped[str] = mapped_column(
        String(8), default="auto", server_default="auto", nullable=False
    )
    day_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_theme_assets.id", ondelete="SET NULL"), nullable=True
    )
    night_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_theme_assets.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class PortalConsoleEvent(Base):
    """A scoped runtime event uploaded by the local AstrBot client.

    ``message_content`` remains empty unless the portal administrator has
    explicitly enabled message tracking and the Windows sync client has
    received that setting.  ``event_id`` makes retries idempotent.
    """

    __tablename__ = "portal_console_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(64), default="", index=True, nullable=False)
    group_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    # The QQ number remains the stable actor identifier.  ``user_name`` is a
    # bounded display snapshot supplied by the Windows plugin so that the web
    # console can be searched and read without having to call QQ APIs.
    user_name: Mapped[str] = mapped_column(
        String(128), default="", server_default="", nullable=False
    )
    message_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    message_type: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    detail: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    direction: Mapped[str] = mapped_column(
        String(16), default="", server_default="", nullable=False
    )
    message_content: Mapped[str] = mapped_column(
        String(2048), default="", server_default="", nullable=False
    )
    # Compact non-text message metadata (image links, recall state and media
    # counters). Keeping it separate from message_content lets the
    # member-history API render a full record without storing media bytes.
    message_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)


class PortalReplyAsset(Base):
    """A private reply image that the linked Windows bot can fetch by token.

    The group configuration holds only this asset's ID and checksum; the
    image bytes never become part of the sync JSON or a domain-bound URL.
    """

    __tablename__ = "portal_reply_assets"
    __table_args__ = (
        UniqueConstraint("group_id", "sha256", name="uq_portal_reply_asset_group_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    image_data: Mapped[bytes] = mapped_column(
        LargeBinary().with_variant(MEDIUMBLOB, "mysql"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


class PortalDatabaseSnapshot(Base):
    """A bounded FTP SQLite snapshot retained for the Windows sync client.

    FTP credentials and the remote path remain in ``portal_settings``.  This
    table stores only the validated bytes plus a revision/checksum, allowing a
    pull-only Windows client to materialize the selected database locally
    without receiving any FTP connection details.
    """

    __tablename__ = "portal_database_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "group_id",
            "profile_id",
            name="uq_portal_database_snapshot_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("portal_users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    group_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    profile_id: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    database_data: Mapped[bytes] = mapped_column(
        LargeBinary().with_variant(LONGBLOB, "mysql"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class EmailVerification(Base):
    """One-time email codes.  Only a keyed digest of the code is stored."""

    __tablename__ = "portal_email_verifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_ip: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, index=True, nullable=False
    )
