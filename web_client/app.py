"""FastAPI application for the Joker customer configuration portal."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import json
import hashlib
import hmac
import logging
import math
import re
from pathlib import Path
from time import perf_counter, sleep, time as unix_time
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .database import get_db, get_session_factory, init_database
from .feature_schema import FEATURE_BY_KEY, display_value, parse_field_value
from .global_config import (
    GENERAL_FIELDS,
    get_general_values,
    get_global_feature_values,
    load_global_config,
    sync_global_to_groups,
    update_general_values,
    update_global_feature_values,
)
from .models import (
    AuditLog,
    GameAuthorization,
    GroupAccess,
    GroupClientSync,
    GroupConfigSnapshot,
    LoginAttempt,
    PortalDatabaseSnapshot,
    PortalFeedback,
    User,
    utcnow,
)
from . import remote_store, reply_asset_service, theme_service
from .cookie_service import (
    format_session_max_age,
    get_session_max_age,
    save_session_max_age,
)
from .console_service import (
    DEFAULT_CONSOLE_KEYWORDS,
    get_console_settings,
    normalize_console_keywords,
    save_console_settings,
    user_can_view_console,
)
from .security import (
    card_pepper_proof,
    game_card_fingerprint,
    ensure_csrf_token,
    hash_password,
    normalize_username,
    normalize_game_card,
    validate_root_relative_redirect,
    valid_csrf,
    validate_email,
    validate_group_id,
    validate_password,
    validate_username,
    verify_password,
)
from .services import (
    PortalError,
    add_audit_log,
    add_auto_reply,
    add_group_sub_admin,
    add_timed_task,
    add_user_reply,
    apply_lottery_award_action,
    apply_lottery_campaign_action,
    apply_signin_profile_action,
    apply_game_server_profile_action,
    apply_feature_values,
    reset_game_configuration,
    authorization_state,
    can_remove_admin,
    claim_activated_card,
    clone_group_config,
    delete_auto_reply,
    delete_group_sub_admin,
    delete_unused_license_key,
    delete_timed_task,
    delete_user_reply,
    get_group_view,
    get_group_member_directory,
    get_group_display_name,
    get_feature_values,
    get_admin_license_overview,
    get_group_game_status,
    get_user_group,
    group_config_module,
    generate_admin_license_keys,
    known_group_ids,
    list_admin_groups,
    list_user_groups,
    set_power,
    toggle_timed_task,
    update_timed_task,
    upsert_snapshot,
    remote_data_mode,
    _safe_sync_state,
    _normalize_runtime_panels,
    user_has_login_entitlement,
)
from .email_service import (
    VerificationCodeError,
    consume_password_reset_code,
    consume_registration_code,
    get_email_config_view,
    get_feedback_recipient,
    request_password_reset_code,
    request_registration_code,
    save_email_config,
    send_test_email,
    send_feedback_notification,
)
from .integration_service import (
    browse_ftp_directory,
    delete_ftp_connection,
    delete_integration_profile,
    fetch_validated_ftp_database,
    validate_ftp_database_steam_id,
    rename_ftp_database_member,
    find_default_ftp_database,
    get_active_integration_profile,
    get_ftp_connection_view,
    get_integration_config_view,
    get_saved_ftp_connection_config,
    get_windows_database_profile_projection,
    list_ftp_connections,
    list_integration_profiles,
    migrate_legacy_group_ftp_connections,
    normalize_integration_profile_id,
    upload_validated_ftp_database,
    save_ftp_connection,
    save_ftp_database_selection,
    save_database_snapshot,
    mutate_ftp_database_signin,
    reset_ftp_database_signin,
    save_server_database_binding,
    rename_integration_profile,
    set_active_integration_profile,
    set_integration_profile_enabled,
    test_integration_connection,
    validate_ftp_database_file,
)
from .runner_interface import get_runner_deployment, set_linux_migration_prepared
from .execution_control import (
    execution_control_for_client,
    get_execution_control,
    list_execution_clients,
    record_execution_client,
    save_execution_control,
)
from .scum_query_service import LinuxScumQueryService
from .websocket_api import (
    GAME_WS_CLOSE_FORBIDDEN,
    GameWebSocketError,
    GameWebSocketHub,
    serve_game_websocket,
)
from .websocket_settings import (
    game_websocket_user_view,
    game_websocket_runtime_enabled,
    get_game_websocket_settings,
    probe_game_websocket_connection,
    save_game_websocket_settings,
)
from .im3_socketio import (
    get_im3_socketio_settings,
    save_im3_socketio_settings,
    test_im3_socketio_connection,
)
from .music_source_settings import (
    MUSIC_CARD_DEFAULT_SIGN_URL,
    get_music_source_services,
    save_music_source_services,
    test_music_card_signer,
    test_soda_music_source,
)
from .release_service import get_installed_release_info
from .update_log import get_update_log
from .self_update import (
    MAX_UPDATE_REQUEST_BYTES,
    UpdateError,
    discard_staged_update,
    get_update_history,
    get_update_root,
    get_update_status,
    queue_staged_update,
    stage_update_package,
)
from .pagination import DEFAULT_PAGE_SIZE, pagination_metadata, parse_page
from .settings import Settings
from .session_middleware import PortalSessionMiddleware


BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)
SLOW_REQUEST_LOG_MS = 1500

# A Windows heartbeat and a browser save can legitimately touch the same
# snapshot row at the same time. Retry only database errors known to be
# transient; permanent schema/serialization errors remain visible in the
# portal log instead of being hidden behind an endless retry loop.
_SYNC_DB_RETRY_MARKERS = (
    "database is locked",
    "lock wait timeout",
    "deadlock",
    "try restarting transaction",
    "error 1205",
    "error 1213",
    "1205",
    "1213",
)


def _is_retryable_sync_storage_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        if any(marker in text for marker in _SYNC_DB_RETRY_MARKERS):
            return True
        current = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )
    return False


def _sync_storage_failure_code(prefix: str, exc: BaseException | None) -> str:
    """Return a short safe storage category for the Windows terminal."""

    raw = type(exc).__name__ if isinstance(exc, BaseException) else "unknown"
    suffix = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_") or "unknown"
    return f"{prefix}_{suffix}"[:63]


def _public_request_origin(request: Request) -> str:
    """Return the public origin when the portal is behind a trusted proxy."""
    fallback = str(request.base_url).rstrip("/")
    forwarded_scheme = str(request.headers.get("x-forwarded-proto", "") or "")
    forwarded_host = str(request.headers.get("x-forwarded-host", "") or "")
    scheme = forwarded_scheme.split(",", 1)[0].strip().lower()
    host = forwarded_host.split(",", 1)[0].strip()
    if scheme in {"http", "https"} and host and not any(
        char.isspace() or char in "/\\@?#" for char in host
    ):
        parsed = urlsplit(f"{scheme}://{host}")
        if (
            parsed.netloc == host
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        ):
            return f"{scheme}://{host}"
    # The official Linux installer writes this from its validated domain. It
    # is only a fallback for CDN/proxy configurations that drop forwarded
    # headers, so the customer-facing page never needs an address input.
    try:
        configured = str(
            getattr(getattr(request.app.state, "settings", None), "public_origin", "")
            or ""
        ).strip().rstrip("/")
    except Exception:
        configured = ""
    return configured or fallback


def _websocket_endpoint_url(request: Request, websocket_settings: dict) -> str:
    """Build the public WS address shown in the admin form."""
    configured = str(websocket_settings.get("portal_url", "") or "").strip().rstrip("/")
    if not configured:
        configured = _public_request_origin(request)
    parsed = urlsplit(configured)
    scheme = {"https": "wss", "http": "ws"}.get(parsed.scheme.lower(), "ws")
    path = str(websocket_settings.get("endpoint_path", "/api/sync/v1/ws"))
    return f"{scheme}://{parsed.netloc}{path}"


async def _test_admin_game_executor(app: FastAPI, websocket_settings: dict) -> dict:
    """Test the configured SCUM plugin through an online Windows worker."""

    executor = websocket_settings.get("game_executor", {})
    if not isinstance(executor, dict) or not executor.get("enabled"):
        return {
            "success": False,
            "code": "executor_unavailable",
            "message": "默认游戏插件连接未启用。请先打开该开关。",
        }
    if not executor.get("api_key"):
        return {
            "success": False,
            "code": "plugin_websocket_key_missing",
            "message": (
                "默认游戏插件链接未配置。请填写开发文档格式 "
                "ws(s)://主机:端口/ws?apiKey=..."
            ),
        }
    connections = await app.state.game_ws.status()
    if not connections:
        return {
            "success": False,
            "code": "windows_executor_offline",
            "message": (
                "没有在线 Windows 执行端。请先确认网页同步令牌与 "
                "JOKER_WEB_SYNC_TOKEN 配对，并等待 Windows 完成同步。"
            ),
        }
    connection = next(
        (
            item
            for item in connections
            if "test_game_connection" in item.get("capabilities", [])
            or "game.test_game_connection" in item.get("capabilities", [])
        ),
        connections[0],
    )
    try:
        result = await app.state.game_ws.call(
            connection["client_id"],
            "test_game_connection",
            {"group_id": "", "profile_id": "global"},
            timeout=websocket_settings.get("call_timeout", 20.0),
        )
    except GameWebSocketError as exc:
        return {"success": False, "code": exc.code, "message": exc.detail}
    if not isinstance(result, dict):
        return {
            "success": False,
            "code": "invalid_executor_response",
            "message": "Windows 未返回有效的 /ping 检测结果。",
        }
    if result.get("success"):
        result.setdefault(
            "message",
            "Windows 已通过开发文档规定的 GET /ping，实际游戏插件连接正常。",
        )
    return result


def _update_status_with_installed_release() -> dict:
    """Combine root-worker progress with the code version serving this request."""

    status = dict(get_update_status())
    status["installed_release"] = get_installed_release_info()
    return status


def _run_saved_integration_test(
    target: str,
    owner_id: int | None,
    portal_settings: Settings,
    group_id: str | None = None,
    profile_id: str | None = None,
) -> str:
    """Run an external connection test with a thread-local DB session.

    FTP/MySQL handshakes can take several seconds.  A session created for the
    browser request must not be used from the worker thread, otherwise a slow
    test can corrupt the request transaction or make the next save appear to
    time out.
    """

    session = get_session_factory()()
    try:
        try:
            return test_integration_connection(
                session,
                portal_settings,
                target,
                owner_id,
                group_id=group_id,
                profile_id=profile_id,
            )
        except SQLAlchemyError as exc:
            # The external connection test must not turn a temporary portal
            # database pool failure into a blank 500 page.
            raise PortalError("网页数据库暂时不可用，请稍后重试") from exc
    finally:
        session.close()


def _run_saved_ftp_browse(
    path: str | None,
    owner_id: int,
    portal_settings: Settings,
    group_id: str | None = None,
    profile_id: str | None = None,
):
    """Browse FTP in a worker using a thread-local portal session."""

    session = get_session_factory()()
    try:
        config = get_saved_ftp_connection_config(
            session,
            portal_settings,
            owner_id,
            group_id=group_id,
            profile_id=profile_id,
        )
        return browse_ftp_directory(
            config, path, group_id=group_id, profile_id=profile_id
        )
    except SQLAlchemyError as exc:
        raise PortalError("网页数据库暂时不可用，请稍后重试") from exc
    finally:
        session.close()


def _run_auto_select_ftp_database(
    owner_id: int,
    portal_settings: Settings,
    group_id: str | None = None,
    profile_id: str | None = None,
) -> str | None:
    """Search for the conventional SQLite file without blocking the request."""

    session = get_session_factory()()
    try:
        config = get_saved_ftp_connection_config(
            session,
            portal_settings,
            owner_id,
            group_id=group_id,
            profile_id=profile_id,
        )
        return find_default_ftp_database(config)
    except SQLAlchemyError as exc:
        raise PortalError("网页数据库暂时不可用，请稍后重试") from exc
    finally:
        session.close()


def _run_validate_saved_ftp_database(
    owner_id: int,
    path: str,
    portal_settings: Settings,
    group_id: str | None = None,
    profile_id: str | None = None,
    *,
    include_snapshot: bool = False,
) -> str | tuple[str, bytes]:
    """Validate a selected FTP file in a worker-owned database session."""

    session = get_session_factory()()
    try:
        config = get_saved_ftp_connection_config(
            session,
            portal_settings,
            owner_id,
            group_id=group_id,
            profile_id=profile_id,
        )
        if include_snapshot:
            return fetch_validated_ftp_database(
                config,
                path,
                group_id=group_id,
                profile_id=profile_id,
            )
        return validate_ftp_database_file(
            config, path, group_id=group_id, profile_id=profile_id
        )
    except SQLAlchemyError as exc:
        raise PortalError("网页数据库暂时不可用，请稍后重试") from exc
    finally:
        session.close()


def _sync_group_integration_profile(
    db: Session,
    group_id: str,
    owner_id: int,
    updated_by: int | None = None,
) -> dict:
    """Project the selected group database profiles to Windows safely.

    The portal keeps FTP/database credentials in ``PortalSetting``.  Windows
    receives only the allow-listed ``features.stats`` descriptors and a local
    relative path, allowing the existing SteamID validator to read the local
    SQLite file without another portal/FTP request.
    """

    projection = get_windows_database_profile_projection(db, owner_id, group_id)

    def update_features(existing):
        features = deepcopy(existing) if isinstance(existing, dict) else {}
        stats = features.get("stats")
        stats = deepcopy(stats) if isinstance(stats, dict) else {}
        stats.update(
            {
                "database_profile": str(projection.get("database_profile", "") or ""),
                "database_path": str(projection.get("database_path", "") or ""),
                "database_profiles": deepcopy(
                    projection.get("database_profiles", [])
                ),
            }
        )
        features["stats"] = stats
        # Keep the active SCUM server metadata in the Windows projection too.
        # Only identifiers and enabled flags are copied; FTP credentials and
        # remote paths remain in the portal-side integration store.  Clear a
        # previous projection when the last profile is removed so a deleted
        # server cannot remain queryable on Windows.
        scum = features.get("scum")
        scum = deepcopy(scum) if isinstance(scum, dict) else {}
        managed = bool(projection.get("integration_managed"))
        had_projection = any(
            key in scum
            for key in (
                "integration_managed",
                "integration_profile",
                "server_id",
                "server_profiles",
            )
        )
        if managed:
            scum.update(
                {
                    "integration_managed": True,
                    "server_id": str(projection.get("server_id", "") or ""),
                    "server_profiles": deepcopy(
                        projection.get("server_profiles", [])
                    ),
                    "integration_profile": str(
                        projection.get("database_profile", "") or ""
                    ),
                }
            )
            features["scum"] = scum
        elif had_projection:
            for key in (
                "integration_managed",
                "integration_profile",
                "server_id",
                "server_profiles",
            ):
                scum.pop(key, None)
            features["scum"] = scum
        return features

    if remote_data_mode():
        if db is None:
            raise PortalError("网页数据库暂时不可用，请稍后重试")

        def mutate(config):
            config["features"] = update_features(config.get("features"))
            return config

        return remote_store.mutate_web_config(db, group_id, updated_by, mutate)

    # Local mode is used by the Windows-side embedded portal and tests.  The
    # group-config module performs one atomic JSON write and wakes the normal
    # outbound sync loop exactly once.
    return group_config_module().batch_update_group_data(
        group_id, [("features", update_features)]
    )


def _mutate_database_signin_in_worker(
    settings: Settings,
    owner_id: int,
    group_id: str,
    profile_id: str,
    *,
    steam_id: str,
    reward: int,
    player_name: str,
    sign_date: str,
    currency_column: str,
    period_mode: str,
    reward_config: dict,
) -> dict:
    """Run a sign-in transaction with a portal Session owned by this worker."""

    worker_db = get_session_factory()()
    try:
        result = mutate_ftp_database_signin(
            worker_db, settings, owner_id, group_id, profile_id,
            steam_id=steam_id, reward=reward, player_name=player_name,
            sign_date=sign_date, currency_column=currency_column,
            period_mode=period_mode, reward_config=reward_config,
        )
        if isinstance(result, dict) and result.get("success") is True:
            # Commit the authoritative FTP mutation and snapshot first. The
            # Windows projection is ancillary; a projection failure must not
            # turn an already completed sign-in into a reported database
            # failure (or cause a second reward attempt to conflict).
            worker_db.commit()
            try:
                _sync_group_integration_profile(worker_db, group_id, owner_id, owner_id)
                worker_db.commit()
            except Exception as projection_exc:
                worker_db.rollback()
                logger.warning(
                    "Database mutation projection refresh failed group=%s profile=%s error=%s",
                    group_id,
                    profile_id,
                    type(projection_exc).__name__,
                )
        else:
            worker_db.rollback()
        return result if isinstance(result, dict) else {
            "success": False,
            "code": "database_mutation_failed",
            "message": "服务器数据库事务未返回有效结果。",
        }
    except Exception as exc:
        worker_db.rollback()
        if isinstance(exc, PortalError):
            message = str(exc) or "服务器数据库事务失败。"
        elif isinstance(exc, SQLAlchemyError):
            message = "网页数据库事务失败，请检查服务器数据库配置。"
        else:
            message = "服务器数据库事务失败，请检查 FTP 数据库权限、路径和 SQLite 文件。"
        logger.exception(
            "Database mutation worker failed group=%s profile=%s",
            group_id,
            profile_id,
        )
        return {
            "success": False,
            "code": "database_mutation_failed",
            "message": message,
            "detail": type(exc).__name__,
        }
    finally:
        worker_db.close()


def _reset_database_signin_in_worker(
    settings: Settings,
    owner_id: int,
    group_id: str,
    profile_id: str,
    *,
    steam_id: str,
    sign_date: str,
    reward: int | None,
    remove_reward: bool,
    currency_column: str,
) -> dict:
    """Run a sign-in reset with a portal Session owned by this worker."""

    worker_db = get_session_factory()()
    try:
        result = reset_ftp_database_signin(
            worker_db, settings, owner_id, group_id, profile_id,
            steam_id=steam_id, sign_date=sign_date, reward=reward,
            remove_reward=remove_reward, currency_column=currency_column,
        )
        if isinstance(result, dict) and result.get("success") is True:
            worker_db.commit()
            try:
                _sync_group_integration_profile(worker_db, group_id, owner_id, owner_id)
                worker_db.commit()
            except Exception as projection_exc:
                worker_db.rollback()
                logger.warning(
                    "Database reset projection refresh failed group=%s profile=%s error=%s",
                    group_id,
                    profile_id,
                    type(projection_exc).__name__,
                )
        else:
            worker_db.rollback()
        return result if isinstance(result, dict) else {
            "success": False,
            "code": "database_mutation_failed",
            "message": "服务器数据库重置未返回有效结果。",
        }
    except Exception as exc:
        worker_db.rollback()
        if isinstance(exc, PortalError):
            message = str(exc) or "服务器数据库重置失败。"
        elif isinstance(exc, SQLAlchemyError):
            message = "网页数据库事务失败，请检查服务器数据库配置。"
        else:
            message = "服务器数据库重置失败，请检查 FTP 数据库权限、路径和 SQLite 文件。"
        logger.exception(
            "Database reset worker failed group=%s profile=%s",
            group_id,
            profile_id,
        )
        return {
            "success": False,
            "code": "database_mutation_failed",
            "message": message,
            "detail": type(exc).__name__,
        }
    finally:
        worker_db.close()

# Keep administrator and customer sessions in separate browser cookies.  The
# signed payload format and expiry policy remain shared, but logging into one
# portal area can no longer overwrite or authenticate the other area.
ADMIN_SESSION_COOKIE = "joker_admin_session"
USER_SESSION_COOKIE = "joker_user_session"
LEGACY_SESSION_COOKIE = "joker_portal_session"


def _session_cookie_for_scope(scope) -> str:
    """Select the session namespace from the route (and safe referrer hints)."""

    path = str(scope.get("path", ""))
    if path == "/admin" or path.startswith("/admin/"):
        return ADMIN_SESSION_COOKIE
    if path == "/user" or path.startswith("/user/"):
        return USER_SESSION_COOKIE

    # Asset URLs are shared by both templates and the root compatibility page
    # has no role prefix.  A same-origin referrer preserves the active area;
    # otherwise inspect only cookie names, never a host supplied by the user.
    headers = {
        bytes(key).lower(): bytes(value)
        for key, value in scope.get("headers", [])
    }
    referer = headers.get(b"referer", b"").decode("latin-1", "ignore")
    referrer_path = urlsplit(referer).path
    if referrer_path == "/admin" or referrer_path.startswith("/admin/"):
        return ADMIN_SESSION_COOKIE
    if referrer_path == "/user" or referrer_path.startswith("/user/"):
        return USER_SESSION_COOKIE

    cookie_header = headers.get(b"cookie", b"").decode("latin-1", "ignore")
    names = {
        item.split("=", 1)[0].strip()
        for item in cookie_header.split(";")
        if "=" in item
    }
    if ADMIN_SESSION_COOKIE in names and USER_SESSION_COOKIE not in names:
        return ADMIN_SESSION_COOKIE
    return USER_SESSION_COOKIE


def _process_sync_exchange(
    client_id: str,
    groups: list,
    known_group_ids: list,
    console_events: list,
    apply_failures: list,
    authoritative_group_ids: bool,
    runtime_summaries: dict | None = None,
    config_source_complete: bool | None = None,
    auth_source_complete: bool | None = None,
    sync_scope_complete: bool = False,
    portal_settings: Settings | None = None,
    game_auth_source_complete: bool | None = None,
    public_origin: str = "",
    local_group_ids: list | None = None,
) -> dict:
    """Run one Windows exchange away from Uvicorn's event loop.

    SQLAlchemy's regular Session and the remote-store row locks are
    synchronous.  Keeping the complete transaction in one worker thread
    prevents a slow MySQL lock or a large group projection from delaying
    unrelated customer form requests while preserving transaction ordering.
    """

    # The request dependency session belongs to Uvicorn's event-loop thread.
    # Never pass it to this worker: SQLAlchemy sessions are intentionally not
    # thread-safe and a slow sync can otherwise corrupt a concurrent request.
    # New Windows clients report the health of the group-config and auth
    # sources independently.  A readable config source can safely fulfill a
    # group pull while auth.json is recovering, provided the server keeps the
    # previous card fields unchanged.  The HTTP boundary supplies a safe
    # compatibility inference for older clients that omit these fields.
    if config_source_complete is None:
        config_source_complete = bool(authoritative_group_ids)
    if auth_source_complete is None:
        auth_source_complete = bool(authoritative_group_ids)
    config_source_complete = bool(config_source_complete)
    auth_source_complete = bool(auth_source_complete)
    if game_auth_source_complete is None:
        game_auth_source_complete = bool(authoritative_group_ids or groups or known_group_ids)
    game_auth_source_complete = bool(game_auth_source_complete)
    # A missing/corrupt config source cannot fulfill a web configuration
    # request.  Keep that exchange heartbeat-only.  An auth-only degradation
    # is handled by ``remote_store.apply_bot_exchange`` as a config-only
    # projection that preserves existing authorization/card values.
    protected_heartbeat = not config_source_complete
    if protected_heartbeat:
        # Preserve only the independent game heartbeat.  QQ configuration and
        # runtime data remain blocked while the local config source is unreadable.
        groups = [
            item
            for item in groups
            if isinstance(item, dict) and isinstance(item.get("game_auth"), dict)
        ]
        known_group_ids = []
        # Console events are group-scoped runtime data.  Do not accept or
        # acknowledge them while the Windows side cannot prove that its local
        # configuration/auth sources were read completely.
        console_events = []
        runtime_summaries = {}
        apply_failures = []
        local_group_ids = []

    db = get_session_factory()()
    try:
        record_execution_client(db, client_id)
        execution_control = execution_control_for_client(db, client_id)
        if not execution_control["client_allowed"]:
            # Keep the response successful so older Windows clients do not
            # enter an HTTP retry loop. New clients persist this decision and
            # stop processing local plugin events immediately.
            db.commit()
            return {
                "status": "ok",
                "blocked": True,
                "client_id": client_id,
                "execution_control": execution_control,
                "changes": [],
                "console_events": [],
                "accepted_console_event_ids": [],
                "reported_failures": [],
                "missing_group_ids": [],
                "refresh_group_ids": [],
                "bound_group_ids": [],
                "managed_group_ids": [],
                "runtime_accepted_group_ids": [],
                "message_cache_replay_requests": [],
                "claimable_group_ids": [],
                "console": {"enabled": False, "messages_enabled": False},
                "config_source_complete": config_source_complete,
                "auth_source_complete": auth_source_complete,
                "game_auth_source_complete": game_auth_source_complete,
                "sync_scope_complete": bool(sync_scope_complete),
            }
        changes = remote_store.apply_bot_exchange(
            db,
            client_id,
            groups,
            known_group_ids,
            authoritative_group_ids=authoritative_group_ids,
            runtime_summaries=runtime_summaries,
            config_source_complete=config_source_complete,
            auth_source_complete=auth_source_complete,
            sync_scope_complete=sync_scope_complete,
            game_auth_source_complete=game_auth_source_complete,
            local_group_ids=local_group_ids,
        )
        # Resolve the web-account scope before accepting failure reports too;
        # an older Windows process must not mark an unbound snapshot as a web
        # configuration failure after scoped sync has been enabled.
        bound_group_id_set = set(
            remote_store.bound_group_ids(
                db, client_id, known_group_ids, local_group_ids
            )
        )
        apply_failures = [
            item
            for item in apply_failures
            if isinstance(item, dict)
            and str(item.get("group_id", "")).strip() in bound_group_id_set
        ]
        if protected_heartbeat:
            # Keep existing group rows visibly online without accepting any
            # partial local projection.  This touches only liveness; queued
            # web revisions and refresh hints remain unchanged.
            if auth_source_complete:
                remote_store.touch_client_heartbeat(db, client_id)
            else:
                remote_store.touch_client_heartbeat(
                    db, client_id, mark_auth_unverified=True
                )
        reported_failures = remote_store.record_bot_sync_failures(
            db, client_id, apply_failures
        )
        console_settings = get_console_settings(db)
        accepted_console_events = 0
        accepted_console_event_ids: list[str] = []
        if console_settings["enabled"]:
            # ``return_event_ids`` was added after the first Windows client
            # implementation.  Keep the exchange compatible with an older
            # store (and with integrations/mocks) that still returns only an
            # integer accepted-count instead of the newer ID list.
            ingested_console_events = remote_store.ingest_console_events(
                db,
                client_id,
                console_events,
                retention=console_settings["retention"],
                message_content_enabled=console_settings["messages_enabled"],
                return_event_ids=True,
            )
            if isinstance(ingested_console_events, int) and not isinstance(
                ingested_console_events, bool
            ):
                accepted_console_events = max(0, ingested_console_events)
            elif isinstance(ingested_console_events, (list, tuple, set)):
                accepted_console_event_ids = [
                    str(event_id)
                    for event_id in ingested_console_events
                    if str(event_id).strip()
                ]
                accepted_console_events = len(accepted_console_event_ids)
        # A protected heartbeat has no trustworthy local inventory, therefore
        # it must not consume a missing/refresh hint.  The durable request stays
        # in MySQL for the first complete exchange after the local read is
        # repaired.
        if protected_heartbeat:
            missing_group_ids = []
            refresh_group_ids = []
        else:
            missing_group_ids = remote_store.missing_bot_group_ids(
                db, known_group_ids
            )
            refresh_group_ids = remote_store.requested_group_refresh_ids(
                db, client_id, known_group_ids
            )
        # Return the durable web-account scope so Windows can stop sending
        # configuration/runtime/message data for local groups that nobody has
        # claimed through the portal.  IDs only are returned; card values and
        # configuration remain on their existing authenticated paths.
        bound_group_ids = sorted(bound_group_id_set)
        claimable_group_ids = sorted(remote_store.claimable_group_ids(db, client_id))
        # Echo the runtime groups accepted for this client and web scope. The
        # member directory is read-only; this acknowledgement only confirms
        # routing and never sends member rows back to Windows.
        runtime_accepted_group_ids: list[str] = []
        for raw_group_id in (runtime_summaries or {}):
            group_id = str(raw_group_id).strip()
            if group_id not in bound_group_id_set:
                continue
            snapshot = remote_store.get_snapshot(db, group_id)
            if snapshot is not None and str(snapshot.client_id or "") == client_id:
                runtime_accepted_group_ids.append(group_id)
        runtime_accepted_group_ids.sort()
        bound_group_id_set = set(bound_group_ids)
        # A member-detail view may discover that Linux retained fewer message
        # rows than Windows still has in its local cache.  Return those small
        # durable replay requests only to the owning Windows client; no member
        # content travels in this response.
        message_cache_replay_requests = remote_store.pending_group_message_history_replays(
            db, client_id, bound_group_ids
        )
        # Even if an administrator left an old pending revision or refresh flag
        # on an unbound snapshot, never route that work to Windows.  It remains
        # durable and becomes eligible automatically if the group is claimed
        # by a web account later.
        changes = [
            item
            for item in changes
            if isinstance(item, dict)
            and str(item.get("group_id", "")).strip() in bound_group_id_set
        ]
        missing_group_ids = [
            group_id for group_id in missing_group_ids if group_id in bound_group_id_set
        ]
        refresh_group_ids = [
            group_id for group_id in refresh_group_ids if group_id in bound_group_id_set
        ]
        portal_game_settings = get_game_websocket_settings(db, portal_settings)
        response = {
            "status": "ok",
            # The Windows execution client has no separate portal URL field
            # in the web UI.  Carry the public origin from this authenticated
            # exchange so a reverse-proxied deployment can update it without
            # manual Windows editing.
            "portal_url": str(public_origin or "").strip().rstrip("/"),
            "changes": changes,
            "console_events": accepted_console_events,
            # Explicit IDs make client retries safe when the HTTP response
            # was lost after the database transaction already committed.
            "accepted_console_event_ids": accepted_console_event_ids,
            "reported_failures": reported_failures,
            "missing_group_ids": missing_group_ids,
            "refresh_group_ids": refresh_group_ids,
            "bound_group_ids": bound_group_ids,
            # Alias used by the newer Windows client wording.
            "managed_group_ids": bound_group_ids,
            "runtime_accepted_group_ids": runtime_accepted_group_ids,
            "message_cache_replay_requests": message_cache_replay_requests,
            "claimable_group_ids": claimable_group_ids,
            "console": {
                "enabled": console_settings["enabled"],
                "messages_enabled": console_settings["messages_enabled"],
            },
            "runner": get_runner_deployment(db),
            # Linux owns the optional global game fallback.  It is returned
            # only through this authenticated outbound exchange; Windows
            # persists it locally and remains the execution-only endpoint.
            "game_websocket_enabled": bool(
                portal_game_settings.get("game_websocket_enabled", False)
            ),
            "game_websocket_endpoint_path": str(
                # The legacy /api/game/v1/ws alias remains served for old
                # workers, but every successful HTTPS sync moves Windows to
                # this single canonical route.
                "/api/sync/v1/ws"
            ),
            # This token is deliberately distinct from JOKER_WEB_SYNC_TOKEN.
            # It is encrypted at rest and returned only through this already
            # authenticated HTTPS exchange to the Windows execution client.
            "game_websocket_token": str(
                portal_game_settings.get("game_websocket_token", "") or ""
            ),
            "game_executor": dict(portal_game_settings.get("game_executor", {})),
            # This is the documented im3 Socket.IO bus.  It is returned only
            # to the authenticated Windows sync client; the group page gets a
            # redacted status view and never sees the room key.
            "im3_socketio": {
                key: value
                for key, value in get_im3_socketio_settings(
                    db, portal_settings, include_secrets=True
                ).items()
                if key in {"enabled", "server_url", "socketio_path", "bot_type", "key"}
            },
            # These are user-entered mini-program/WebSocket cards.  They are
            # returned only to the authenticated Windows worker so the plugin
            # can connect with the same credential that the user just proved;
            # browsers and audit logs receive only the masked status.
            "user_websocket_keys": remote_store.get_group_websocket_access_keys(
                db, bound_group_ids, portal_settings, client_id=client_id
            ),
            # Source-service credentials remain Linux-admin only. This
            # authenticated Windows heartbeat is the sole route that carries
            # music source and card-signer settings to the execution client.
            "music_source_services": get_music_source_services(
                db, portal_settings, include_secrets=True
            ),
            "source_protected": protected_heartbeat,
            "config_source_complete": config_source_complete,
            "auth_source_complete": auth_source_complete,
            "game_auth_source_complete": game_auth_source_complete,
            "sync_scope_complete": bool(sync_scope_complete),
            "execution_control": execution_control,
        }
        db.commit()
        return response
    except Exception:
        # Do not return a failed worker connection to the pool with an open
        # transaction.  This also makes the next bot heartbeat retry cleanly.
        db.rollback()
        raise
    finally:
        db.close()


def _process_sync_ack(client_id: str, groups: list) -> dict:
    """Apply a Windows ACK in the same worker-thread transaction boundary."""

    db = get_session_factory()()
    try:
        applied = remote_store.acknowledge_bot_changes(
            db, client_id, groups, return_details=True
        )
        db.commit()
        if isinstance(applied, dict):
            return {"status": "ok", **applied}
        return {"status": "ok", "applied": applied}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _process_member_directory_upload(
    client_id: str,
    group_id: str,
    members: list[dict],
    levels_pending: bool = False,
    snapshot_complete: bool = True,
    expected_count: int = 0,
) -> dict:
    """Persist a Windows member refresh through the dedicated fast path."""

    last_error: Exception | None = None
    for attempt in range(3):
        db = get_session_factory()()
        try:
            stored = remote_store.store_runtime_members(
                db,
                client_id,
                group_id,
                members,
                levels_pending=levels_pending,
                snapshot_complete=snapshot_complete,
                expected_count=expected_count,
            )
            if stored <= 0:
                db.rollback()
                return {
                    "status": "rejected",
                    "code": "group_client_not_bound",
                    "stored_count": 0,
                }
            db.commit()
            return {"status": "ok", "stored_count": stored}
        except Exception as exc:
            db.rollback()
            last_error = exc
            if not _is_retryable_sync_storage_error(exc) or attempt >= 2:
                logger.exception(
                    "成员目录写入失败 client=%s group=%s attempt=%s",
                    str(client_id)[:64],
                    str(group_id)[:32],
                    attempt + 1,
                )
                break
            sleep(0.20 * (attempt + 1))
        finally:
            db.close()
    return {
        "status": "error",
        "code": "member_directory_storage_busy"
        if _is_retryable_sync_storage_error(last_error or Exception())
        else "member_directory_storage_failed",
        "stored_count": 0,
    }


def _process_message_cache_upload(
    client_id: str,
    group_id: str,
    messages: list[dict],
    scope_type: str = "group",
    scope_id: str = "",
    complete_user_id: str = "",
    complete_count: int = 0,
) -> dict:
    """Merge a small Windows message batch without waiting for exchange."""

    last_error: Exception | None = None
    for attempt in range(3):
        db = get_session_factory()()
        try:
            if scope_type == "private":
                stored = remote_store.store_private_message_events(
                    db, client_id, scope_id, messages
                )
            else:
                stored = remote_store.store_runtime_messages(
                    db,
                    client_id,
                    group_id,
                    messages,
                    complete_user_id=complete_user_id,
                    complete_count=complete_count,
                )
            if stored <= 0:
                db.rollback()
                return {
                    "status": "rejected",
                    "code": "group_client_not_bound",
                    "stored_count": 0,
                }
            db.commit()
            return {"status": "ok", "stored_count": stored}
        except Exception as exc:
            db.rollback()
            last_error = exc
            if not _is_retryable_sync_storage_error(exc) or attempt >= 2:
                logger.exception(
                    "消息缓存写入失败 client=%s group=%s attempt=%s",
                    str(client_id)[:64],
                    str(group_id)[:32],
                    attempt + 1,
                )
                break
            sleep(0.20 * (attempt + 1))
        finally:
            db.close()
    return {
        "status": "error",
        "code": "message_cache_storage_busy"
        if _is_retryable_sync_storage_error(last_error or Exception())
        else _sync_storage_failure_code("message_cache_storage_failed", last_error),
        "stored_count": 0,
    }


def _process_group_refresh_request(
    group_id: str,
    user_id: int,
    ip_address: str = "",
    user_agent: str = "",
) -> dict[str, int | bool]:
    """Persist one pull request in a short, isolated database transaction.

    The browser route is asynchronous because it has to parse a CSRF form, but
    the remote-store adapter is synchronous and may wait on a MySQL row lock
    while a Windows heartbeat is applying/ACKing a revision.  Running this
    tiny transaction in the thread pool keeps the event loop responsive and
    makes the button return a flash message instead of appearing to hang at a
    reverse-proxy timeout.  The locked read also makes two quick clicks
    idempotent: only the first one creates an audit row and flips the durable
    refresh flag.
    """

    db = get_session_factory()()
    try:
        snapshot = remote_store.get_snapshot(db, group_id, for_update=True)
        if snapshot is not None and snapshot.refresh_requested:
            db.rollback()
            return {"requested": 0, "already_pending": True}

        requested = remote_store.request_group_refresh(db, [group_id])
        if requested:
            db.add(
                AuditLog(
                    user_id=user_id,
                    group_id=group_id,
                    action="portal_sync_refresh_requested",
                    summary="请求 Windows 重新同步当前分群配置",
                    changes={"refresh_requested": True},
                    ip_address=str(ip_address or "")[:64],
                    user_agent=str(user_agent or "")[:1000],
                )
            )
        db.commit()
        return {"requested": int(requested), "already_pending": False}
    except IntegrityError:
        # Two browser tabs can request the first snapshot at the same time.
        # The unique group key lets one transaction win; turn the losing
        # insert into the same idempotent "already queued" result instead of
        # returning a generic 500/timeout page.
        db.rollback()
        existing = remote_store.get_snapshot(db, group_id)
        if existing is not None and bool(existing.refresh_requested):
            return {"requested": 0, "already_pending": True}
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _process_group_config_pull_request(
    group_id: str, client_id: str
) -> dict[str, Any]:
    db = get_session_factory()()
    try:
        result = remote_store.request_group_config_pull(db, group_id, client_id)
        if result.get("status") == "ok":
            db.commit()
        else:
            db.rollback()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _static_asset_version() -> str:
    """Return a cache key that changes whenever shipped browser assets change.

    Nginx deliberately caches ``/static`` responses for a week.  A fixed
    query string here meant that an upgraded portal could still render using
    an old stylesheet, which in turn made the desktop sidebar look like a
    vertical page.  Hashing the small, local assets at process startup keeps
    immutable caching fast while making every changed release unambiguous.
    """

    digest = hashlib.sha256()
    for asset_name in ("app.css", "app.js"):
        asset_path = BASE_DIR / "static" / asset_name
        digest.update(asset_name.encode("ascii"))
        try:
            digest.update(asset_path.read_bytes())
        except OSError:
            # StaticFiles will provide the normal 404 if an operator deploys
            # an incomplete tree.  Do not hide that cause behind app startup.
            digest.update(b"missing")
    return digest.hexdigest()[:16]


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["display_value"] = display_value


_CONSOLE_DISPLAY_TIMEZONE = timezone(timedelta(hours=8))


def _console_event_time(value: object) -> str:
    """Render legacy/null UTC CMD timestamps in the portal's local timezone."""
    formatter = getattr(value, "strftime", None)
    if not callable(formatter):
        return "-"
    try:
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        return str(
            value.astimezone(_CONSOLE_DISPLAY_TIMEZONE).strftime("%m-%d %H:%M:%S")
        )
    except (AttributeError, TypeError, ValueError, OverflowError):
        return "-"


templates.env.globals["console_event_time"] = _console_event_time
templates.env.globals["static_asset_version"] = _static_asset_version()


def _parse_console_datetime_filter(value: object) -> datetime | None:
    """Interpret a browser ``datetime-local`` query as China local time.

    Runtime events are stored as naive UTC in MySQL/SQLite.  Keeping this
    conversion at the HTTP boundary means ``remote_store`` remains usable by
    API callers that already provide UTC datetimes.
    """

    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CONSOLE_DISPLAY_TIMEZONE)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _pagination_context(
    request: Request,
    total: int,
    *,
    page_param: str = "page",
    page_size: int = DEFAULT_PAGE_SIZE,
    anchor: str = "",
) -> dict:
    """Build one safe, filter-preserving pager for a rendered portal page.

    Keeping the page number separate for each table avoids a console-log pager
    changing a customer's feedback or group-list page.  Query parameters are
    reconstructed from the current request instead of trusting a browser
    supplied URL, and only the current route path is used in generated links.
    """

    current = parse_page(request.query_params.get(page_param))
    pager = pagination_metadata(total, current, page_size)
    base_params = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key != page_param
    ]

    def page_url(page: int) -> str:
        query = urlencode([*base_params, (page_param, str(page))], doseq=True)
        location = request.url.path
        if query:
            location = f"{location}?{query}"
        return f"{location}{anchor}" if anchor else location

    pager["links"] = [
        {
            "number": number,
            "url": page_url(number) if number else "",
            "current": number == pager["page"],
        }
        for number in pager["page_numbers"]
    ]
    pager["previous_url"] = (
        page_url(pager["previous_page"]) if pager["previous_page"] else ""
    )
    pager["next_url"] = page_url(pager["next_page"]) if pager["next_page"] else ""
    return pager


class SyncAuthenticationError(PermissionError):
    """Raised when a machine-to-machine portal request has no valid token."""


class GameWebSocketAuthenticationError(PermissionError):
    """Raised when a game-operation request has no valid entry token."""


# The identity is persisted by the Windows plugin and is used to scope
# pending revisions and reply assets.  Keep the grammar intentionally small:
# it is never a user-facing name and accepting arbitrary whitespace or URL
# punctuation makes mismatched clients look like intermittent sync failures.
_SYNC_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _sync_client_id_error(code: str, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code}, status_code=400)


def _extract_sync_client_id(
    request: Request, payload: dict | None = None
) -> tuple[str | None, JSONResponse | None]:
    """Validate the stable Windows client identity used by sync endpoints.

    Older clients may send the identity in only one location, so a valid
    header *or* payload value is sufficient.  When both are present they must
    match exactly; silently preferring one lets a bad proxy/header rewrite
    attach an ACK to another Windows installation.
    """

    header_value = request.headers.get("x-joker-client")
    payload_value = payload.get("client_id") if isinstance(payload, dict) else None
    values: list[tuple[str, str]] = []
    for source, raw_value in (("header", header_value), ("payload", payload_value)):
        if raw_value is None:
            continue
        if not isinstance(raw_value, str):
            return None, _sync_client_id_error(
                "sync_client_invalid", "同步客户端标识格式无效"
            )
        value = raw_value.strip()
        # Treat an omitted/blank optional source as absent.  A blank value in
        # both locations still fails below, while a valid value in the other
        # source preserves compatibility with older plugin builds.
        if not value:
            continue
        if not _SYNC_CLIENT_ID_RE.fullmatch(value):
            return None, _sync_client_id_error(
                "sync_client_invalid", "同步客户端标识格式无效"
            )
        values.append((source, value))

    if not values:
        return None, _sync_client_id_error(
            "sync_client_missing", "缺少同步客户端标识"
        )
    if len(values) == 2 and values[0][1] != values[1][1]:
        return None, _sync_client_id_error(
            "sync_client_mismatch", "同步客户端标识不一致"
        )
    return values[0][1], None


FEEDBACK_CATEGORIES = {
    "portal": "网页使用",
    "sync": "Windows 同步",
    "group": "群管功能",
    "license": "授权/卡密",
    "other": "其他建议",
}
FEEDBACK_STATUSES = {
    "new": "待处理",
    "processing": "处理中",
    "resolved": "已解决",
    "rejected": "不处理",
}


def _redirect(path: str) -> RedirectResponse:
    """Create a same-origin redirect without trusting a public host header.

    The portal is normally behind Nginx/CDN.  Keep every application redirect
    root-relative so a proxy's origin IP, a stale ``Host`` header, or an
    accidental future call site cannot turn a normal form redirect into a
    public domain-to-IP jump.
    """
    return RedirectResponse(validate_root_relative_redirect(path), status_code=303)


def _flash(request: Request, message: str, category: str = "info") -> None:
    messages = list(request.session.get("flashes", []))
    messages.append({"message": message, "category": category})
    request.session["flashes"] = messages[-5:]


def _template_context(request: Request, user: User | None = None, **kwargs):
    context = {
        "request": request,
        "portal_name": request.app.state.settings.portal_name,
        "csrf_token": ensure_csrf_token(request.session),
        "flashes": request.session.pop("flashes", []),
        "current_user": user,
        "current_year": datetime.now().year,
        # This opaque ID lets an administrator locate the matching traceback
        # without exposing a database error, URL, sync token, or group config
        # in the browser.  It is also returned as an HTTP response header.
        "request_id": getattr(request.state, "request_id", ""),
        # _current_user resolves a database-backed preference once per
        # request.  Anonymous pages intentionally keep the built-in theme.
        "theme": getattr(
            request.state,
            "portal_theme",
            {"mode": "auto", "day_url": "", "night_url": ""},
        ),
    }
    context.update(kwargs)
    return context


def _fallback_group_view(group_id: str) -> dict:
    """Build a renderable read-only view when one legacy row is malformed."""

    return {
        "config": {"group_id": str(group_id), "features": {}},
        "powered_on": False,
        "features": [
            {
                "spec": spec,
                "values": {field.key: field.default for field in spec.fields},
            }
            for spec in FEATURE_BY_KEY.values()
        ],
        "auth": {
            "state": "missing",
            "active": False,
            "expire": 0,
            "expire_text": "authorization unavailable",
        },
        "timed_messages": [],
        "replies": [],
        "sub_admins": [],
        "player_replies": [],
        "blacklist_summary": {
            "available": False,
            "message": "等待 Windows 同步黑名单数据；运行时记录不会上传到 Linux。",
            "total": 0,
            "active": 0,
            "entries": [],
        },
        "stats_summary": {
            "available": False,
            "message": "等待 Windows 同步统计数据；运行时用户数据不会上传到 Linux。",
            "group": {"total_users": 0, "today_signins": 0, "total_points": 0, "avg_points": 0},
            "ranking": [],
        },
        "sync": {
            "online": False,
            "pending": False,
            "status": "unknown",
            "connection_state": "offline",
            "badge": "sync state unavailable",
            "text": "sync state unavailable",
            "display_text": "sync state unavailable",
            "detail": "The group view was loaded with safe defaults; check joker-web logs.",
            "revision": 0,
            "applied_revision": 0,
            "last_seen_at": None,
            "last_seen_text": "",
            "refresh_requested": False,
        },
    }


def _current_user(request: Request, db: Session) -> User | None:
    raw_id = request.session.get("user_id")
    try:
        user_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    if not user.is_admin and not user_has_login_entitlement(db, user):
        # A deleted current group card revokes portal access without deleting
        # the independently-owned username, password, or audit history.
        request.session.clear()
        _flash(
            request,
            "账号绑定的群聊卡密已删除或授权已撤销，暂时不能登录。请联系管理员重新授权。",
            "warning",
        )
        return None
    if getattr(request.state, "portal_theme_user_id", None) != user.id:
        request.state.portal_theme = theme_service.get_theme_context(db, user)
        request.state.portal_theme_user_id = user.id
    return user


def _role_home(user: User | None) -> str:
    """Return the canonical landing page for a signed-in account."""
    if user is None:
        return "/user/login"
    return "/admin" if user.is_admin else "/user"


def _auth_prefix(request: Request) -> str:
    """Canonical auth namespace used by the current request."""
    return "/admin" if request.url.path == "/admin" or request.url.path.startswith("/admin/") else "/user"


def _group_path(user: User, group_id: str, fragment: str = "") -> str:
    prefix = "/admin/groups" if user.is_admin else "/user/groups"
    return f"{prefix}/{quote(group_id)}{fragment}"


def _require_user(request: Request, db: Session) -> User | RedirectResponse:
    user = _current_user(request, db)
    admin_area = request.url.path == "/admin" or request.url.path.startswith("/admin/")
    user_area = request.url.path == "/user" or request.url.path.startswith("/user/")
    if user is not None and admin_area and not user.is_admin:
        _flash(request, "管理员页面仅限管理员账号访问", "error")
        return _redirect("/user")
    if user is not None and user_area and user.is_admin:
        _flash(request, "管理员账号请使用管理后台", "info")
        return _redirect("/admin")
    if user is None and admin_area:
        _flash(request, "请先登录管理员账号", "warning")
        return _redirect("/admin/login")
    if user is None:
        _flash(request, "请先登录", "warning")
        return _redirect("/user/login")
    return user


def _require_customer(request: Request, db: Session) -> User | RedirectResponse:
    """Authenticate the customer portal without allowing an admin session in.

    ``_require_user`` is intentionally shared by the two group namespaces,
    where an administrator may inspect a group through ``/admin/groups``.
    Customer-only actions use this narrower guard so a future route cannot
    accidentally expose customer registration, card claiming, feedback, or
    customer sync controls to an administrator session.
    """

    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if user.is_admin:
        _flash(request, "管理员账号请使用管理后台", "info")
        return _redirect("/admin")
    return user


def _require_admin(request: Request, db: Session) -> User | RedirectResponse:
    user = _current_user(request, db)
    if user is None:
        _flash(request, "请先登录管理员账号", "warning")
        return _redirect("/admin/login")
    if not user.is_admin:
        _flash(request, "仅后台管理员可以访问该页面", "error")
        return _redirect("/user")
    return user


async def _validated_form(request: Request):
    form = await request.form()
    if not valid_csrf(request.session, form.get("csrf_token")):
        raise PortalError("页面已过期，请刷新后重新提交")
    return form


def _form_checkbox(form: object, name: str, default: bool = False) -> bool:
    """Read a checkbox with the common hidden-off + checked-on form shape."""

    getlist = getattr(form, "getlist", None)
    values = getlist(name) if callable(getlist) else []
    if not values:
        getter = getattr(form, "get", None)
        value = getter(name, None) if callable(getter) else None
        values = [] if value is None else [value]
    if not values:
        return default
    return any(
        str(value or "").strip().lower() in {"on", "1", "true", "yes"}
        for value in values
    )


def _group_edit_status(
    group_id: str,
    db: Session,
    *,
    auth: dict | None = None,
    sync: dict | None = None,
) -> tuple[bool, str]:
    """Return whether a group may be changed from the portal.

    Remote mode stores a durable web revision in the portal database.  A
    disconnected Windows client can apply that revision on its next
    heartbeat, so transport liveness must not make the web form read-only.
    Only an authoritative local-source protection state blocks writes; those
    states mean Windows explicitly withheld or removed the group projection
    and accepting edits could target the wrong local group.
    """

    auth_state = auth if isinstance(auth, dict) else authorization_state(group_id, db)
    if not remote_data_mode():
        if not bool(auth_state.get("active")):
            return False, "该群授权无效，不能执行配置变更或开关机。"
        return True, ""

    # In remote mode the sync/source diagnostic must take precedence over the
    # projected authorization flag.  During a partial Windows read the portal
    # deliberately marks the card as ``unverified`` (inactive) to fail closed;
    # checking ``active`` first used to hide the real communication problem and
    # made the group page say only “授权无效” while the bot was online.
    sync_state = sync if isinstance(sync, dict) else None
    if sync_state is None:
        snapshot = remote_store.get_snapshot(db, group_id)
        try:
            offline_after = Settings.from_env().offline_after_seconds
        except Exception:
            offline_after = 180
        sync_state = remote_store.sync_state(snapshot, offline_after)
    if str(sync_state.get("status", "") or "") == "unavailable":
        return (
            False,
            "Windows 已报告本地不存在此分群配置或授权，当前只能查看；"
            "恢复 Windows 本地配置/授权并完成同步后再保存。",
        )
    if str(sync_state.get("status", "") or "") in {"protected", "partial"}:
        return (
            False,
            (
                "Windows 已连接网页，但本地授权正在重新验证；为避免旧卡密继续生效，"
                "当前只能查看。请先修复 Windows 授权数据读取不完整的问题后再保存。"
                if str(sync_state.get("status", "") or "") == "partial"
                else "Windows 已连接网页，但本地群配置或授权读取不完整；为避免双端覆盖，"
                "当前只能查看。请先修复 Windows 本地数据读取后再保存。"
            ),
        )
    if not bool(auth_state.get("active")):
        return False, "该群授权无效，不能执行配置变更或开关机。"
    return True, ""


def _owned_group_or_redirect(
    request: Request,
    db: Session,
    user: User,
    group_id: str,
    require_active: bool = False,
):
    route_path = request.url.path
    if route_path.startswith("/admin/") and not user.is_admin:
        _flash(request, "管理员页面仅限管理员账号访问", "error")
        return _redirect("/user")
    if route_path.startswith("/user/") and user.is_admin:
        _flash(request, "管理员账号请使用管理后台", "info")
        return _redirect("/admin")
    access = get_user_group(db, user.id, group_id)
    if user.is_admin:
        if group_id not in known_group_ids(db):
            _flash(request, "该群没有授权或分群配置记录", "error")
            return _redirect("/admin")
        if require_active:
            editable, reason = _group_edit_status(group_id, db)
            if not editable:
                _flash(request, reason, "warning")
                return _redirect(f"/admin/groups/{quote(group_id)}")
        return access or SimpleNamespace(group_id=group_id, role="admin")
    if access is None:
        _flash(request, "你没有该群的网页管理权限", "error")
        return _redirect("/user")
    if require_active:
        editable, reason = _group_edit_status(group_id, db)
        if not editable:
            # Keep the card-specific wording for expired customers while
            # making an offline Windows connection distinguishable.
            _flash(request, reason, "warning")
            return _redirect(f"/user/groups/{quote(group_id)}")
    return access


def _snapshot_and_log(
    request: Request,
    db: Session,
    user: User,
    group_id: str,
    action: str,
    summary: str,
    changes: dict | None = None,
) -> None:
    logged_changes = dict(changes) if isinstance(changes, dict) else {}
    if not remote_data_mode():
        config = group_config_module().get_group_config(group_id)
        upsert_snapshot(db, group_id, config, user.id)
    else:
        # Keep the ordinary operation audit entry tied to the exact remote
        # revision it created.  This makes a web save visible as a sync event
        # in the admin log without adding a second request or a new table.
        snapshot = remote_store.get_snapshot(db, group_id)
        if snapshot is not None:
            updated_at = getattr(snapshot, "updated_at", None)
            if isinstance(updated_at, datetime):
                updated_at = updated_at.isoformat(timespec="seconds")
            try:
                revision = max(0, int(getattr(snapshot, "revision", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                revision = 0
            try:
                applied_revision = max(
                    0, int(getattr(snapshot, "applied_revision", 0) or 0)
                )
            except (TypeError, ValueError, OverflowError):
                applied_revision = 0
            logged_changes["_sync"] = {
                "status": str(getattr(snapshot, "sync_status", "pending") or "pending"),
                "revision": revision,
                "applied_revision": applied_revision,
                "saved_at": str(updated_at or ""),
                "state": "pending",
            }
    add_audit_log(db, user, group_id, action, summary, request, logged_changes)
    db.commit()


def _request_group_page_refresh_once(
    request: Request,
    db: Session,
    user: User,
    group_id: str,
) -> bool:
    """Compatibility no-op for pre-1.10.82 callers.

    Opening a page must be read-only.  Older releases queued a Windows
    refresh here, which made ordinary browser reloads look like repeated
    configuration pulls in the local CMD.  A refresh is now queued only by
    the explicit ``/sync-refresh`` action for the selected group.
    """
    del request, db, user, group_id
    return False


def _clean_feedback_text(
    value: object,
    *,
    label: str,
    maximum: int,
    required: bool = True,
    single_line: bool = False,
) -> str:
    """Validate user-provided support text without leaking it into logs."""

    result = str(value or "").strip()
    if required and not result:
        raise PortalError(f"请填写{label}")
    if len(result) > maximum:
        raise PortalError(f"{label}不能超过 {maximum} 个字符")
    if "\x00" in result or (single_line and ("\r" in result or "\n" in result)):
        raise PortalError(f"{label}格式不正确")
    return result


def _global_blacklist_admin_view(db: Session) -> dict:
    """Build the administrator's canonical global-blacklist view."""

    groups = list_admin_groups(db)
    group_ids = [str(item.get("group_id", "")).strip() for item in groups]
    entries_by_id: dict[str, dict] = {}
    observed_runtime = False

    def add_entry(raw: object, source_group: str = "") -> None:
        if not isinstance(raw, dict):
            return
        user_id = str(raw.get("user_id", "") or "").strip()
        if not user_id.isdigit() or not 5 <= len(user_id) <= 20:
            return
        try:
            added_at = max(0, int(raw.get("added_at", raw.get("created_at", 0)) or 0))
        except (TypeError, ValueError, OverflowError):
            added_at = 0
        try:
            expire_time = max(0, int(raw.get("expire_time", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            expire_time = 0
        active = bool(raw.get("active", raw.get("is_active", True)))
        if expire_time and expire_time <= int(unix_time()):
            active = False
        allowed_groups = raw.get("allowed_groups", [])
        allowed_groups = sorted(
            {
                str(item).strip()[:32]
                for item in allowed_groups
                if str(item).strip()
            }
        ) if isinstance(allowed_groups, list) else []
        current = entries_by_id.get(user_id)
        if current is None:
            current = {
                "user_id": user_id,
                "user_name": str(raw.get("user_name", "") or "")[:80],
                "active": active,
                "ban_type": str(raw.get("ban_type", "") or "")[:24],
                "reason": str(raw.get("reason", "") or "")[:240],
                "operator_id": str(raw.get("operator_id", raw.get("added_by", "")) or "")[:32],
                "added_at": added_at,
                "expire_time": expire_time,
                "allowed_groups": allowed_groups,
                "source_groups": [],
            }
            entries_by_id[user_id] = current
        else:
            current["active"] = bool(current.get("active")) or active
            current["allowed_groups"] = sorted(
                set(current.get("allowed_groups", [])) | set(allowed_groups)
            )[:500]
            if added_at > int(current.get("added_at", 0) or 0):
                for key in ("user_name", "ban_type", "reason", "operator_id", "added_at", "expire_time"):
                    if key in raw:
                        current[key] = (
                            str(raw.get(key, "") or "")[:240]
                            if key in {"reason", "ban_type", "operator_id", "user_name"}
                            else added_at if key == "added_at" else expire_time if key == "expire_time" else current[key]
                        )
        if source_group and source_group not in current["source_groups"]:
            current["source_groups"].append(source_group)

    if remote_data_mode():
        snapshots = remote_store.list_snapshots(db, group_ids)
        for group_id, snapshot in snapshots.items():
            raw_runtime = getattr(snapshot, "runtime_json", None)
            if isinstance(raw_runtime, (str, bytes, bytearray)):
                try:
                    raw_runtime = json.loads(raw_runtime)
                except (TypeError, ValueError, json.JSONDecodeError):
                    raw_runtime = {}
            if not isinstance(raw_runtime, dict):
                continue
            panel = raw_runtime.get("global_blacklist_admin")
            if not isinstance(panel, dict):
                panel = raw_runtime.get("global_blacklist")
            if isinstance(panel, dict):
                observed_runtime = True
            rows = panel.get("entries", []) if isinstance(panel, dict) else []
            for row in rows if isinstance(rows, list) else []:
                add_entry(row, str(group_id))
    else:
        try:
            from ..qqadmin.blacklist import load_global_blacklist

            records = load_global_blacklist()
            for user_id, record in (records.items() if isinstance(records, dict) else []):
                add_entry({"user_id": user_id, **record} if isinstance(record, dict) else {}, "")
        except Exception:
            logger.exception("Unable to load local global blacklist for admin page")

    entries = list(entries_by_id.values())
    for entry in entries:
        entry["added_at_text"] = (
            datetime.fromtimestamp(entry["added_at"]).strftime("%Y-%m-%d %H:%M")
            if entry.get("added_at") else "未知"
        )
        entry["expire_time_text"] = (
            datetime.fromtimestamp(entry["expire_time"]).strftime("%Y-%m-%d %H:%M")
            if entry.get("expire_time") else "长期"
        )
    entries.sort(key=lambda item: (not bool(item.get("active")), -int(item.get("added_at", 0) or 0)))
    active_entries = [item for item in entries if item.get("active")]
    permanent = sum(1 for item in active_entries if not item.get("expire_time"))
    return {
        "available": observed_runtime or not remote_data_mode(),
        "entries": entries[:500],
        "total": len(entries),
        "active": len(active_entries),
        "permanent": permanent,
        "temporary": max(0, len(active_entries) - permanent),
        "inactive": max(0, len(entries) - len(active_entries)),
        "groups": [
            {
                "group_id": item.get("group_id", ""),
                "label": item.get("group_display_name") or item.get("group_id", ""),
            }
            for item in groups
        ],
    }


def _queue_staged_update_after_response(update_id: str, operator_id: int) -> None:
    """Publish a staged portal update after its browser response was sent.

    Publishing ``*.request`` wakes a root-owned worker that can eventually
    restart this service. Scheduling it as a FastAPI background task gives
    Nginx the completed response first. The root watcher adds its own short
    grace period before it starts the installer, so a fast local update cannot
    tear down the upstream connection while the browser is still receiving
    that response.
    """

    try:
        queue_staged_update(update_id)
    except UpdateError:
        discard_staged_update(update_id)
        logger.exception("Unable to queue staged portal update update_id=%s", update_id)
        # A background task cannot change the already-sent flash message.
        # Record a bounded, secret-free failure in the same administrator audit
        # trail so the next page visit has an actionable server-side record.
        db = get_session_factory()()
        try:
            add_audit_log(
                db,
                db.get(User, operator_id),
                None,
                "portal_update_queue_failed",
                "网页更新包未能进入服务器更新队列，请检查网页服务日志和更新目录权限",
                changes={"update_id": update_id},
            )
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            logger.exception(
                "Unable to record portal update queue failure update_id=%s", update_id
            )
        finally:
            db.close()


def _deliver_feedback_notification(
    feedback_id: int, recipient: str, settings: Settings
) -> None:
    """Send a queued support email outside the browser request lifecycle.

    The submitted feedback row is committed before this task is scheduled.
    A separate session therefore makes SMTP latency and failures invisible to
    the customer form, while the resulting delivery state remains visible to
    administrators.
    """

    db = get_session_factory()()
    try:
        feedback = db.get(PortalFeedback, feedback_id)
        if feedback is None or feedback.email_state != "queued":
            return
        reporter = db.get(User, feedback.user_id)
        try:
            send_feedback_notification(
                db,
                settings,
                recipient,
                feedback_id=feedback.id,
                username=reporter.username if reporter is not None else f"用户 #{feedback.user_id}",
                group_id=feedback.group_id or "",
                category=FEEDBACK_CATEGORIES.get(feedback.category, feedback.category),
                subject=feedback.subject,
                content=feedback.content,
            )
        except (ValueError, PortalError) as exc:
            feedback.email_state = "failed"
            feedback.email_error = str(exc)[:255]
            logger.warning("Feedback email delivery failed feedback_id=%s", feedback_id)
        except Exception:
            feedback.email_state = "failed"
            feedback.email_error = "邮件发送发生未知错误，请检查服务日志"[:255]
            logger.exception("Feedback email delivery crashed feedback_id=%s", feedback_id)
        else:
            feedback.email_state = "sent"
            feedback.email_error = ""
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Unable to record feedback email state feedback_id=%s", feedback_id)
    finally:
        db.close()


def create_app() -> FastAPI:
    settings = Settings.from_env()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_database()
        # Load the optional database override once.  The admin save route
        # updates this in-memory value immediately; a restart reloads it from
        # the same portal-only setting without touching the Windows client.
        session_db = get_session_factory()()
        try:
            app.state.session_max_age = get_session_max_age(
                session_db, settings.session_max_age
            )
        finally:
            session_db.close()
        websocket_db = get_session_factory()()
        try:
            websocket_settings = get_game_websocket_settings(websocket_db, settings)
            im3_socketio_settings = get_im3_socketio_settings(
                websocket_db, settings, include_secrets=False
            )
        finally:
            websocket_db.close()
        app.state.game_websocket_settings = websocket_settings
        app.state.im3_socketio_settings = im3_socketio_settings
        app.state.game_ws.max_clients = websocket_settings["max_clients"]
        app.state.game_ws.call_timeout = websocket_settings["call_timeout"]
        try:
            yield
        finally:
            # Do not leave pending game calls/futures attached to a worker
            # when Uvicorn is reloaded or the service is stopped gracefully.
            await app.state.game_ws.close()

    app = FastAPI(
        title=settings.portal_name,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    # Optional low-latency command channel for Windows game operations.  The
    # existing HTTPS sync loop remains authoritative for configuration; this
    # in-memory hub only tracks currently connected workers and is recreated
    # on process restart.
    app.state.game_ws = GameWebSocketHub()
    # SCUM provider traffic is executed from Linux. Windows only uses the
    # authenticated sync endpoint below, which avoids its unreliable route to
    # the external server APIs and keeps one short-lived query cache here.
    app.state.scum_query = LinuxScumQueryService()
    # Descriptive alias for integrations that do not use the short state key.
    app.state.game_websocket_hub = app.state.game_ws
    app.state.game_websocket_settings = {
        "enabled": True,
        "game_websocket_enabled": False,
        "max_clients": app.state.game_ws.max_clients,
        "call_timeout": app.state.game_ws.call_timeout,
        "portal_url": "",
        "endpoint_path": "/api/sync/v1/ws",
        "game_websocket_token": "",
        "game_websocket_token_configured": False,
        "portal_sync_token_configured": bool(settings.sync_token),
        # The portal sync token is never a fallback game credential.
        "sync_token": "",
        "token_configured": False,
        "game_executor": {
            "enabled": False,
            "type": "plugin_websocket",
            "host": "127.0.0.1",
            "port": 28016,
            "api_key": "",
            "websocket_scheme": "ws",
            "timeout_seconds": 10.0,
        },
        "game_executor_api_key_configured": False,
        "game_executor_display_url": "",
    }
    app.state.im3_socketio_settings = {
        "enabled": False,
        "server_url": "https://im3.vipscum.cn",
        "socketio_path": "/socket.io",
        "bot_type": "bot_main",
    }
    app.state.session_max_age = settings.session_max_age
    # Keep one lock per group instead of one global queue. Ten unrelated groups
    # can sync in parallel, while writes for the same group remain ordered.
    app.state.sync_group_locks = {}
    app.state.sync_group_locks_guard = asyncio.Lock()
    app.state.dummy_password_hash = hash_password("dummy-password-for-timing-check")
    app.add_middleware(
        PortalSessionMiddleware,
        secret_key=settings.session_secret,
        # ``session_cookie`` is the safe fallback for routes without a role
        # prefix; the resolver selects the isolated admin/customer cookie for
        # normal requests.
        session_cookie=USER_SESSION_COOKIE,
        cookie_resolver=_session_cookie_for_scope,
        legacy_session_cookie=LEGACY_SESSION_COOKIE,
        max_age=settings.session_max_age,
        max_age_provider=lambda: getattr(
            app.state, "session_max_age", settings.session_max_age
        ),
        same_site="lax",
        https_only=settings.secure_cookies,
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        started_at = perf_counter()
        request.state.request_id = uuid4().hex[:16]
        admin_path = settings.admin_path
        original_path = str(request.scope.get("path", "") or "")
        admin_redirect: RedirectResponse | None = None
        if admin_path != "/admin":
            if original_path == admin_path or original_path.startswith(admin_path + "/"):
                suffix = original_path[len(admin_path):]
                normalized_path = "/admin" + suffix
                request.scope["path"] = normalized_path
                request.scope["raw_path"] = normalized_path.encode("utf-8")
            elif original_path == "/admin" or original_path.startswith("/admin/"):
                suffix = original_path[len("/admin"):]
                admin_redirect = RedirectResponse(
                    f"{admin_path}{suffix}", status_code=307
                )
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            content_length = 0
        if request.url.path == "/admin/update" and request.method == "POST":
            # The archive itself is capped at 80 MiB; allow only a small,
            # fixed multipart envelope around it.  Nginx uses the same 82 MiB
            # request ceiling in the shipped reverse-proxy example.
            size_limit = MAX_UPDATE_REQUEST_BYTES
        elif (
            request.method == "POST"
            and request.url.path in {"/user/appearance", "/admin/appearance"}
        ):
            # Appearance uploads are deliberately the sole non-update form
            # route that may exceed 1 MiB. The total transport budget protects
            # the HTTP worker; each source image is then format/pixel checked
            # and re-encoded to a compact WebP before entering the database.
            size_limit = theme_service.MAX_THEME_REQUEST_BYTES
        elif (
            request.method == "POST"
            and request.url.path.endswith("/replies")
            and request.url.path.startswith(("/user/groups/", "/admin/groups/"))
        ):
            # Reply images are independently bounded and never enter sync
            # JSON.  Keep this larger multipart budget separate from normal
            # text forms so several selected images are accepted without
            # weakening the rest of the portal's request limit.
            size_limit = reply_asset_service.MAX_REPLY_REQUEST_BYTES
        else:
            size_limit = (
                10_000_000 if request.url.path.startswith("/api/sync/") else 1_000_000
            )
        if admin_redirect is not None:
            response = admin_redirect
        elif content_length > size_limit:
            response = JSONResponse({"detail": "请求内容过大"}, status_code=413)
        else:
            response = await call_next(request)
        location = response.headers.get("location", "")
        if (
            admin_path != "/admin"
            and (location == "/admin" or location.startswith("/admin/"))
        ):
            response.headers["location"] = f"{admin_path}{location[len('/admin'):]}"
        content_type = response.headers.get("content-type", "").lower()
        if admin_path != "/admin" and content_type.startswith("text/html"):
            body = b"".join([chunk async for chunk in response.body_iterator])
            body = body.replace(b"/admin", admin_path.encode("utf-8"))
            rewritten = Response(content=body, status_code=response.status_code)
            rewritten.raw_headers = [
                (key, value)
                for key, value in response.raw_headers
                if key.lower() != b"content-length"
            ]
            rewritten.raw_headers.append(
                (b"content-length", str(len(body)).encode("ascii"))
            )
            rewritten.background = response.background
            response = rewritten
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            # QQ supplies signed short-lived media URLs through its own CDN.
            # Keep the allow-list narrow while permitting the browser to show
            # the already-authorized image and play the recorded voice/video.
            "img-src 'self' data: blob: https://*.qq.com https://*.qpic.cn "
            "https://*.gtimg.cn https://*.nt.qq.com.cn https://koishi.js.org; "
            "media-src 'self' https://*.qq.com https://*.qpic.cn "
            "https://*.gtimg.cn https://*.nt.qq.com.cn https://koishi.js.org; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        elapsed_ms = max(0, round((perf_counter() - started_at) * 1000))
        response.headers["X-Response-Time-Ms"] = str(elapsed_ms)
        if elapsed_ms >= SLOW_REQUEST_LOG_MS:
            # Keep slow-refresh diagnosis on the server side; query strings
            # may contain user-entered names or filters and are not needed to
            # identify a route bottleneck.
            logger.warning(
                "Slow web request request_id=%s method=%s path=%s status=%s elapsed_ms=%s",
                getattr(request.state, "request_id", ""),
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
            )
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path.startswith("/static/"):
            # CSS and tiny client-side interaction code are versioned in the
            # template URL.  Let the browser/EO cache them so page switches do
            # not repeatedly wait for the Linux origin.
            response.headers["Cache-Control"] = "public, max-age=604800, immutable"
        elif request.url.path.startswith(("/user", "/admin")) and not response.headers.get(
            "Cache-Control"
        ):
            # Group sync state is heartbeat-driven and changes independently
            # of the browser request. Authenticated HTML must not be served
            # from a browser/CDN cache after Windows has acknowledged a
            # refresh; otherwise the page can continue showing "等待连接"
            # even though the database is already current. Leave explicit
            # asset/update cache policies above untouched.
            response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.exception_handler(PortalError)
    async def portal_error_handler(request: Request, exc: PortalError):
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"detail": str(exc)}, status_code=400)
        user = None
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context=_template_context(request, user, error=str(exc)),
            status_code=400,
        )

    @app.exception_handler(SyncAuthenticationError)
    async def sync_authentication_error_handler(
        _request: Request, _exc: SyncAuthenticationError
    ):
        # This endpoint is consumed by the Windows plugin, never by a browser
        # form.  A structured 401 makes a copied/mismatched token distinct from
        # malformed JSON, a 5xx response, or a transport timeout.
        return JSONResponse(
            {
                "detail": "同步令牌无效，请核对 Windows 插件与网页服务器的同步令牌。",
                "code": "sync_token_invalid",
            },
            status_code=401,
        )

    @app.exception_handler(GameWebSocketAuthenticationError)
    async def game_websocket_authentication_error_handler(
        _request: Request, _exc: GameWebSocketAuthenticationError
    ):
        return JSONResponse(
            {
                "detail": "游戏 WebSocket 入口令牌无效，请核对后台生成的入口令牌。",
                "code": "game_websocket_token_invalid",
            },
            status_code=401,
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(request: Request, _exc: SQLAlchemyError):
        # Database pool exhaustion and transient row locks should fail quickly
        # with an actionable response instead of leaving a browser waiting for
        # its proxy timeout.  Detailed exceptions stay in the server log.
        logger.warning(
            "Database request failed request_id=%s path=%s error=%s",
            getattr(request.state, "request_id", ""),
            request.url.path,
            type(_exc).__name__,
        )
        if request.url.path.startswith("/api/") or request.headers.get(
            "accept", ""
        ).startswith("application/json"):
            return JSONResponse(
                {"detail": "数据库暂时繁忙，请稍后重试同步。"}, status_code=503
            )
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context=_template_context(
                request,
                None,
                error="保存暂未完成：服务器数据库暂时繁忙，请稍后重试。已保存的数据不会被覆盖。",
            ),
            status_code=503,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        # Do not expose tracebacks or internal configuration to customers.  The
        # server log retains the traceback and the browser receives a usable
        # retry page instead of Starlette's blank "Internal Server Error".
        logger.exception(
            "Unhandled portal request failed request_id=%s path=%s",
            getattr(request.state, "request_id", ""),
            request.url.path,
        )
        if request.url.path.startswith("/api/") or request.headers.get(
            "accept", ""
        ).startswith("application/json"):
            return JSONResponse(
                {
                    "detail": "网页服务暂时无法处理请求，请稍后重试。",
                    "code": "internal_error",
                },
                status_code=500,
            )
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context=_template_context(
                request,
                None,
                error="群配置暂时无法加载。请刷新后重试；若问题持续，请联系管理员查看 joker-web 服务日志。",
            ),
            status_code=500,
        )

    @app.get("/health/live")
    def health_live():
        """Process-only check: use it to distinguish portal and MySQL faults."""
        return {"status": "ok"}

    @app.get("/health")
    @app.get("/health/ready")
    def health(db: Session = Depends(get_db)):
        # Do not use COUNT(*) here: this endpoint is polled by deployment and
        # monitoring tools and should be independent of table size.
        db.execute(select(1)).scalar_one()
        return {"status": "ok"}

    async def _run_serialized_sync_write(
        operation,
        *args,
        timeout: float | None = None,
        lock_keys: list[object] | tuple[object, ...] | set[object] | None = None,
    ):
        """Run one sync write while serializing only overlapping group scopes."""

        normalized_keys = sorted(
            {
                str(value or "").strip()[:80]
                for value in (lock_keys or [])
                if str(value or "").strip()
            }
        )
        if not normalized_keys:
            normalized_keys = ["__sync_global__"]
        async with app.state.sync_group_locks_guard:
            locks = [
                app.state.sync_group_locks.setdefault(key, asyncio.Lock())
                for key in normalized_keys
            ]

        acquired: list[asyncio.Lock] = []
        queue_deadline = asyncio.get_running_loop().time() + 30.0

        def defer_lock_release(task: asyncio.Task) -> None:
            held_locks = tuple(acquired)
            acquired.clear()

            async def release_after_completion() -> None:
                try:
                    await task
                except Exception:
                    logger.debug("后台网页同步事务已结束", exc_info=True)
                finally:
                    for lock in reversed(held_locks):
                        if lock.locked():
                            lock.release()

            asyncio.create_task(release_after_completion())

        try:
            # All keys are sorted before acquisition, preventing deadlocks when
            # two multi-group exchanges overlap on only part of their scopes.
            for lock in locks:
                remaining = queue_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await asyncio.wait_for(lock.acquire(), timeout=remaining)
                acquired.append(lock)
            task = asyncio.create_task(run_in_threadpool(operation, *args))
            if timeout is None:
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    defer_lock_release(task)
                    raise
            try:
                # A worker thread cannot be interrupted halfway through a
                # transaction. Keep the group locks until it commits/rolls back.
                return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                defer_lock_release(task)
                raise
            except asyncio.CancelledError:
                defer_lock_release(task)
                raise
        finally:
            for lock in reversed(acquired):
                if lock.locked():
                    lock.release()

    def _require_sync_token(request: Request) -> None:
        supplied = request.headers.get("authorization", "")
        configured_token = str(settings.sync_token or "").strip()
        expected = f"Bearer {configured_token}"
        if not configured_token or not hmac.compare_digest(supplied, expected):
            raise SyncAuthenticationError("sync token invalid")

    def _require_game_websocket_token(request: Request) -> None:
        supplied = request.headers.get("authorization", "")
        configured_token = str(
            getattr(request.app.state, "game_websocket_settings", {}).get(
                "game_websocket_token", ""
            )
            or ""
        ).strip()
        expected = f"Bearer {configured_token}"
        if not configured_token or not hmac.compare_digest(supplied, expected):
            raise GameWebSocketAuthenticationError("game websocket token invalid")

    @app.websocket("/api/game/v1/ws")
    @app.websocket("/api/sync/v1/ws")
    async def game_websocket(websocket: WebSocket):
        """Serve the optional authenticated Windows game-operation channel.

        The route intentionally has no cookie/session fallback: a browser
        session must never be able to impersonate a Windows execution client.
        Clients authenticate with the dedicated game entry token and identify
        themselves in the bounded ``hello`` frame. HTTP configuration sync
        continues to use JOKER_WEB_SYNC_TOKEN.
        """

        # Local mode has no remote Windows scope or game entry token and
        # must not accidentally expose a command socket just because an
        # operator left a portal token in the environment.
        if settings.data_mode != "remote" or not game_websocket_runtime_enabled(
            app.state.game_websocket_settings
        ):
            await websocket.close(code=GAME_WS_CLOSE_FORBIDDEN)
            return

        game_token = str(
            app.state.game_websocket_settings.get("game_websocket_token", "")
            or ""
        ).strip()
        if not game_token:
            await websocket.close(code=GAME_WS_CLOSE_FORBIDDEN)
            return

        await serve_game_websocket(
            websocket,
            app.state.game_ws,
            expected_token=game_token,
        )

    @app.get("/api/sync/v1/ws/status")
    async def game_websocket_status(request: Request):
        """Return non-sensitive connected-worker state for sync diagnostics."""

        _require_game_websocket_token(request)
        if settings.data_mode != "remote":
            return JSONResponse(
                {"detail": "网页未启用远程同步模式", "code": "remote_mode_disabled"},
                status_code=409,
            )
        if not game_websocket_runtime_enabled(app.state.game_websocket_settings):
            return JSONResponse(
                {"detail": "游戏 WebSocket 已由后台停用", "code": "websocket_disabled"},
                status_code=409,
            )
        return {
            "status": "ok",
            "protocol": "joker-game-v1",
            "settings": {
                "enabled": True,
                "max_clients": app.state.game_websocket_settings["max_clients"],
                "call_timeout": app.state.game_websocket_settings["call_timeout"],
                "endpoint_path": app.state.game_websocket_settings.get(
                    "endpoint_path", "/api/sync/v1/ws"
                ),
            },
            "connections": await app.state.game_ws.status(),
        }

    @app.post("/api/sync/v1/game/call")
    async def game_call(request: Request, db: Session = Depends(get_db)):
        """Route one authenticated game operation through the WebSocket.

        This endpoint is intended for trusted backend integrations, not the
        customer browser.  The Windows worker remains the only process that
        touches QQ/game APIs or the external SCUM database.
        """

        _require_game_websocket_token(request)
        if settings.data_mode != "remote":
            return JSONResponse(
                {"detail": "网页未启用远程同步模式", "code": "remote_mode_disabled"},
                status_code=409,
            )
        if not game_websocket_runtime_enabled(app.state.game_websocket_settings):
            return JSONResponse(
                {"detail": "游戏 WebSocket 已由后台停用", "code": "websocket_disabled"},
                status_code=409,
            )
        if not str(
            app.state.game_websocket_settings.get("game_websocket_token", "")
            or ""
        ).strip():
            return JSONResponse(
                {"detail": "网页未配置游戏 WebSocket 入口令牌", "code": "game_websocket_token_missing"},
                status_code=409,
            )
        try:
            payload = await request.json()
        except (UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "游戏操作格式无效", "code": "invalid_json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "游戏操作格式无效", "code": "invalid_payload"}, status_code=400)
        client_id = payload.get("client_id")
        # ``action`` was used by an early integration draft; accepting it as
        # a read-only alias keeps backend callers upgradeable without making
        # the wire frame ambiguous.
        operation = payload.get("operation", payload.get("action"))
        timeout = payload.get("timeout")
        if not isinstance(client_id, str) or not isinstance(operation, str):
            return JSONResponse({"detail": "客户端和操作不能为空", "code": "invalid_request"}, status_code=400)
        call_payload = payload.get("payload")
        if not isinstance(call_payload, dict):
            return JSONResponse(
                {"detail": "游戏操作必须包含群号", "code": "invalid_payload"},
                status_code=400,
            )
        try:
            group_id = validate_group_id(str(call_payload.get("group_id", "")))
        except ValueError:
            return JSONResponse(
                {"detail": "游戏操作必须包含有效群号", "code": "invalid_scope"},
                status_code=400,
            )
        license_info = remote_store.get_game_authorization_for_client(
            db, client_id, group_id
        )
        if license_info.get("state") != "active":
            code = str(license_info.get("code", "game_license_required"))
            detail = (
                "该群尚未完成游戏/WebSocket授权心跳"
                if code == "game_license_required"
                else "该群游戏/WebSocket授权已过期，请联系主人续期"
            )
            return JSONResponse({"detail": detail, "code": code}, status_code=403)
        if isinstance(timeout, bool):
            return JSONResponse({"detail": "超时时间格式无效", "code": "invalid_timeout"}, status_code=400)
        try:
            timeout_value = None if timeout is None else float(timeout)
        except (TypeError, ValueError, OverflowError):
            return JSONResponse({"detail": "超时时间格式无效", "code": "invalid_timeout"}, status_code=400)
        if timeout_value is not None and (
            not math.isfinite(timeout_value) or timeout_value < 0.1 or timeout_value > 120
        ):
            return JSONResponse({"detail": "超时时间范围无效", "code": "invalid_timeout"}, status_code=400)
        try:
            result = await app.state.game_ws.call(
                client_id,
                operation,
                call_payload,
                timeout=timeout_value,
            )
        except GameWebSocketError as exc:
            if exc.code in {"invalid_client", "invalid_operation", "invalid_payload", "invalid_timeout", "payload_too_large"}:
                status_code = 400
            elif exc.code in {"unsupported_operation", "busy"}:
                status_code = 409
            elif exc.code == "request_timeout":
                status_code = 504
            elif exc.code == "client_offline":
                status_code = 503
            else:
                status_code = 502
            return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=status_code)
        return {"status": "ok", "result": result}

    def _card_pepper_mismatch_response(request: Request) -> JSONResponse | None:
        """Fail closed when Windows and portal use different card salts.

        The sync bearer token authenticates the request, but it cannot tell us
        whether both sides create the same card fingerprint.  Without this
        handshake, normal-looking configuration sync can silently leave every
        card claim in the misleading "not yet synchronized" state.
        """
        supplied = request.headers.get("x-joker-card-pepper-proof", "")
        expected = card_pepper_proof(settings.card_pepper)
        if supplied and hmac.compare_digest(supplied, expected):
            return None
        return JSONResponse(
            {
                "detail": "Windows 插件的 Card Pepper 与网页服务器不一致，请核对同步配置。",
                "code": "card_pepper_mismatch",
            },
            status_code=409,
        )

    @app.post("/api/sync/v1/scum/query/{group_id}")
    async def sync_scum_query(
        group_id: str,
        request: Request,
    ):
        """Execute a read-only SCUM query for an authenticated Windows bot.

        SCUM search/detail is read-only and does not expose any group-scoped
        portal data.  A newly installed Windows client may legitimately query
        before its first configuration heartbeat has created a group snapshot,
        so requiring ``GroupAccess`` or a snapshot here incorrectly blocks a
        valid card-authorized group.  The bearer token, Card Pepper proof and
        stable client ID remain mandatory.
        """

        _require_sync_token(request)
        if settings.data_mode != "remote":
            return Response(status_code=404)
        pepper_mismatch = _card_pepper_mismatch_response(request)
        if pepper_mismatch is not None:
            return pepper_mismatch
        try:
            payload = await request.json()
        except (UnicodeDecodeError, ValueError):
            return JSONResponse(
                {"detail": "SCUM 查询请求格式无效", "code": "invalid_json"},
                status_code=400,
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"detail": "SCUM 查询请求格式无效", "code": "invalid_payload"},
                status_code=400,
            )
        client_id, client_id_error = _extract_sync_client_id(request, payload)
        if client_id_error is not None:
            return Response(status_code=404)
        try:
            normalized_group = validate_group_id(str(group_id))
        except (TypeError, ValueError):
            return Response(status_code=404)
        if not client_id:
            return Response(status_code=404)

        operation = str(payload.get("operation", "") or "").strip().lower()
        if operation not in {"search", "detail"}:
            return JSONResponse(
                {"detail": "不支持的 SCUM 查询类型", "code": "unsupported_operation"},
                status_code=400,
            )
        argument = payload.get("argument", "")
        if isinstance(argument, (dict, list, tuple, set)):
            return JSONResponse(
                {"detail": "SCUM 查询参数格式无效", "code": "invalid_argument"},
                status_code=400,
            )
        argument = str(argument or "").strip()
        limits = {"search": 128, "detail": 64}
        if operation in limits and not argument:
            return JSONResponse(
                {"detail": "SCUM 查询参数不能为空", "code": "missing_argument"},
                status_code=400,
            )
        if len(argument) > limits.get(operation, 16):
            return JSONResponse(
                {"detail": "SCUM 查询参数过长", "code": "invalid_argument"},
                status_code=400,
            )
        try:
            result = await app.state.scum_query.query(operation, argument)
        except ValueError as exc:
            return JSONResponse(
                {"detail": "不支持的 SCUM 查询类型", "code": str(exc)},
                status_code=400,
            )
        except Exception as exc:
            logger.exception("Linux SCUM query failed group=%s operation=%s", normalized_group, operation)
            detail = str(exc).replace("\r", " ").replace("\n", " ").strip()[:240]
            return JSONResponse(
                {
                    "detail": (
                        f"Linux 查询 SCUM 服务失败：{detail}"
                        if detail
                        else "Linux 查询 SCUM 服务失败，请检查网页服务器网络和依赖"
                    ),
                    "code": "scum_query_failed",
                },
                status_code=502,
            )
        return {
            "success": True,
            "backend": "linux",
            "operation": operation,
            "result": str(result or ""),
        }

    @app.get("/api/sync/v1/reply-assets/{asset_id}")
    def sync_reply_asset(asset_id: int, request: Request, db: Session = Depends(get_db)):
        """Return one reply image to its linked Windows sync client only.

        The browser never receives this endpoint from a group configuration.
        It uses the same bearer token, Card Pepper proof and stable Windows
        client identity as a normal exchange, then verifies that the asset is
        still referenced by a group assigned to that client.
        """

        _require_sync_token(request)
        if settings.data_mode != "remote":
            return Response(status_code=404)
        pepper_mismatch = _card_pepper_mismatch_response(request)
        if pepper_mismatch is not None:
            return pepper_mismatch
        client_id, client_id_error = _extract_sync_client_id(request)
        if client_id_error is not None:
            # Keep malformed asset requests indistinguishable from a missing
            # asset to avoid turning this authenticated endpoint into an
            # asset-id oracle.  Exchange/ACK return the structured 400 below.
            return Response(status_code=404)
        asset = reply_asset_service.find_reply_asset(db, asset_id)
        if not client_id or asset is None:
            return Response(status_code=404)
        snapshot = remote_store.get_snapshot(db, asset.group_id)
        if (
            snapshot is None
            or not snapshot.client_id
            or not hmac.compare_digest(str(snapshot.client_id), client_id)
        ):
            return Response(status_code=404)
        referenced = reply_asset_service.get_referenced_reply_asset(
            db, asset.group_id, asset.id, snapshot.config_json
        )
        if referenced is None:
            return Response(status_code=404)
        return Response(
            content=referenced.image_data,
            media_type=referenced.content_type,
            headers={
                "Cache-Control": "private, no-store",
                "ETag": f'"{referenced.sha256}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/sync/v1/database-snapshots/{group_id}/{profile_id}")
    def sync_database_snapshot(
        group_id: str,
        profile_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Return one portal-retained FTP SQLite snapshot to its Windows bot.

        FTP credentials never cross this boundary.  The request is scoped by
        the stable Windows client identity bound to the group's config
        snapshot and by the account that owns the group/profile row.  Unknown
        or malformed scopes intentionally look like a missing resource.
        """

        _require_sync_token(request)
        if settings.data_mode != "remote":
            return Response(status_code=404)
        pepper_mismatch = _card_pepper_mismatch_response(request)
        if pepper_mismatch is not None:
            return pepper_mismatch
        client_id, client_id_error = _extract_sync_client_id(request)
        if client_id_error is not None:
            return Response(status_code=404)
        try:
            normalized_group = validate_group_id(str(group_id))
            normalized_profile = normalize_integration_profile_id(profile_id)
        except (TypeError, ValueError, PortalError):
            return Response(status_code=404)
        if not client_id:
            return Response(status_code=404)

        # A group is tenant-owned by exactly one GroupAccess row.  Requiring
        # the snapshot owner to match that row prevents a client that knows a
        # valid group/client pair from reading another account's BLOB.
        access = db.scalar(
            select(GroupAccess).where(GroupAccess.group_id == normalized_group)
        )
        config_snapshot = remote_store.get_snapshot(db, normalized_group)
        if (
            access is None
            or config_snapshot is None
            or not config_snapshot.client_id
            or not hmac.compare_digest(str(config_snapshot.client_id), client_id)
        ):
            return Response(status_code=404)
        # Database access is intentionally independent from the expiry of the
        # group-management license. The durable GroupAccess row plus the
        # bound Windows client still scope this private resource, while the
        # expiry only controls ordinary group-management commands. This lets
        # a configured FTP/SQLite sign-in database continue serving rewards
        # during the renewal window instead of failing before the DB query.
        database_snapshot = db.scalar(
            select(PortalDatabaseSnapshot).where(
                PortalDatabaseSnapshot.owner_user_id == access.user_id,
                PortalDatabaseSnapshot.group_id == normalized_group,
                PortalDatabaseSnapshot.profile_id == normalized_profile,
            )
        )
        if database_snapshot is None:
            # A profile can be projected to Windows before its first snapshot
            # is persisted. Bootstrap it from the already-bound FTP source so
            # the client does not enter an endless missing-snapshot retry loop.
            try:
                integration_view = get_integration_config_view(
                    db,
                    access.user_id,
                    group_id=normalized_group,
                    profile_id=normalized_profile,
                )
                database_view = integration_view.get("database", {})
                sqlite_path = (
                    str(database_view.get("sqlite_path", "") or "").strip()
                    if isinstance(database_view, dict)
                    else ""
                )
                if (
                    not isinstance(database_view, dict)
                    or not database_view.get("enabled")
                    or str(database_view.get("engine", "")).strip().lower() != "sqlite"
                    or not sqlite_path
                ):
                    raise PortalError("当前服务器尚未绑定 SQLite 数据库文件")
                ftp_config = get_saved_ftp_connection_config(
                    db,
                    settings,
                    access.user_id,
                    group_id=normalized_group,
                    profile_id=normalized_profile,
                )
                _, snapshot_bytes = fetch_validated_ftp_database(
                    ftp_config,
                    sqlite_path,
                    group_id=normalized_group,
                    profile_id=normalized_profile,
                )
                save_database_snapshot(
                    db,
                    access.user_id,
                    normalized_group,
                    normalized_profile,
                    snapshot_bytes,
                )
                db.commit()
                database_snapshot = db.scalar(
                    select(PortalDatabaseSnapshot).where(
                        PortalDatabaseSnapshot.owner_user_id == access.user_id,
                        PortalDatabaseSnapshot.group_id == normalized_group,
                        PortalDatabaseSnapshot.profile_id == normalized_profile,
                    )
                )
            except PortalError as exc:
                db.rollback()
                logger.warning(
                    "Database snapshot bootstrap failed group=%s profile=%s reason=%s",
                    normalized_group,
                    normalized_profile,
                    str(exc)[:160],
                )
                return JSONResponse(
                    {"detail": f"数据库快照尚未建立：{exc}", "code": "database_snapshot_missing"},
                    status_code=409,
                )
            except Exception:
                db.rollback()
                logger.exception(
                    "Database snapshot bootstrap crashed group=%s profile=%s",
                    normalized_group,
                    normalized_profile,
                )
                return JSONResponse(
                    {"detail": "数据库快照初始化失败，请检查 FTP 数据库配置", "code": "database_snapshot_missing"},
                    status_code=409,
                )
            if database_snapshot is None:
                return JSONResponse(
                    {"detail": "数据库快照初始化后仍不可用", "code": "database_snapshot_missing"},
                    status_code=409,
                )
        try:
            body = bytes(database_snapshot.database_data)
            size_bytes = int(database_snapshot.size_bytes or 0)
            revision = int(database_snapshot.revision or 0)
        except (TypeError, ValueError, OverflowError):
            return JSONResponse(
                {
                    "detail": "服务器数据库快照记录无效，请重新下载并保存数据库",
                    "code": "database_snapshot_invalid",
                },
                status_code=409,
            )
        if (
            revision <= 0
            or revision > 2**31 - 1
            or size_bytes < 0
            or size_bytes != len(body)
            or size_bytes > 256 * 1024 * 1024
            or body[:16] != b"SQLite format 3\x00"
            or not re.fullmatch(r"[0-9a-f]{64}", str(database_snapshot.sha256 or ""))
            or hashlib.sha256(body).hexdigest() != str(database_snapshot.sha256).lower()
        ):
            # A partial/corrupt BLOB must never be handed to Windows.  Treat
            # it as unavailable so the client retains its previous file.
            logger.error(
                "Invalid database snapshot row group_id=%s profile_id=%s",
                normalized_group,
                normalized_profile,
            )
            return JSONResponse(
                {
                    "detail": "服务器数据库快照校验失败，请重新下载并保存数据库",
                    "code": "database_snapshot_invalid",
                },
                status_code=409,
            )
        etag = f'"{str(database_snapshot.sha256).lower()}"'
        if request.headers.get("if-none-match", "").strip() in {
            etag,
            str(database_snapshot.sha256).lower(),
        }:
            return Response(
                status_code=304,
                headers={
                    "Cache-Control": "private, no-store",
                    "ETag": etag,
                    "X-Database-Snapshot-Revision": str(revision),
                    "X-Snapshot-Revision": str(revision),
                },
            )
        return Response(
            content=body,
            media_type="application/vnd.sqlite3",
            headers={
                "Cache-Control": "private, no-store",
                "ETag": etag,
                "X-Database-Snapshot-Revision": str(revision),
                "X-Snapshot-Revision": str(revision),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/sync/v1/database-members/{group_id}/{profile_id}/{steam_id}")
    async def validate_database_steam_id_from_windows(
        group_id: str,
        profile_id: str,
        steam_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Validate one SteamID against the latest Linux FTP database."""

        _require_sync_token(request)
        if settings.data_mode != "remote":
            return JSONResponse({"detail": "网页未启用远程同步模式", "code": "remote_mode_disabled"}, status_code=409)
        pepper_mismatch = _card_pepper_mismatch_response(request)
        if pepper_mismatch is not None:
            return pepper_mismatch
        client_id, client_id_error = _extract_sync_client_id(request)
        if client_id_error is not None:
            return Response(status_code=404)
        try:
            normalized_group = validate_group_id(str(group_id))
            normalized_profile = normalize_integration_profile_id(profile_id)
        except (TypeError, ValueError, PortalError):
            return Response(status_code=404)
        if not re.fullmatch(r"[0-9]{17}", str(steam_id or "").strip()) or not client_id:
            return JSONResponse({"detail": "SteamID 无效", "code": "invalid_steam_id"}, status_code=400)
        access = db.scalar(select(GroupAccess).where(GroupAccess.group_id == normalized_group))
        if access is None:
            return JSONResponse({"detail": "当前群未绑定网页账号", "code": "group_not_bound"}, status_code=404)
        config_snapshot = remote_store.get_snapshot(db, normalized_group)
        # SteamID 入群验证 is read-only.  A just-saved server/database
        # binding may be valid before the asynchronous Windows config snapshot
        # exists, so requiring that snapshot here made the endpoint return 404
        # without ever querying the FTP database.  Preserve the strict binding
        # check whenever a client identity is available in a snapshot; only the
        # brief bootstrap window is allowed through for this read-only lookup.
        if (
            config_snapshot is not None
            and config_snapshot.client_id
            and not hmac.compare_digest(str(config_snapshot.client_id), client_id)
        ):
            return JSONResponse({"detail": "同步客户端与当前群不匹配", "code": "sync_client_mismatch"}, status_code=404)
        try:
            result = await run_in_threadpool(
                validate_ftp_database_steam_id,
                db,
                settings,
                access.user_id,
                normalized_group,
                normalized_profile,
                str(steam_id).strip(),
            )
            if not isinstance(result, dict):
                return JSONResponse({"detail": "服务器数据库校验失败", "code": "query_error"}, status_code=502)
            # A valid lookup with no matching user is still a successful
            # protocol response; keep ``not_found`` in the JSON so Windows
            # can show the correct binding failure instead of treating it as
            # a transport/database outage.
            return JSONResponse(result)
        except PortalError as exc:
            return JSONResponse({"detail": str(exc), "code": "db_unavailable"}, status_code=502)
        except Exception:
            logger.exception("SteamID database validation failed group=%s profile=%s", normalized_group, normalized_profile)
            return JSONResponse({"detail": "服务器数据库校验失败，请稍后重试", "code": "query_error"}, status_code=502)

    @app.post("/api/sync/v1/database-members/{group_id}/{profile_id}/{steam_id}/rename")
    async def rename_database_member_from_windows(
        group_id: str,
        profile_id: str,
        steam_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Rename an already validated player in the live Linux database."""

        _require_sync_token(request)
        if settings.data_mode != "remote":
            return JSONResponse({"detail": "网页未启用远程同步模式", "code": "remote_mode_disabled"}, status_code=409)
        pepper_mismatch = _card_pepper_mismatch_response(request)
        if pepper_mismatch is not None:
            return pepper_mismatch
        client_id, client_id_error = _extract_sync_client_id(request)
        if client_id_error is not None:
            return client_id_error
        try:
            normalized_group = validate_group_id(str(group_id))
            normalized_profile = normalize_integration_profile_id(profile_id)
        except (TypeError, ValueError, PortalError):
            return JSONResponse({"detail": "数据库操作范围无效", "code": "invalid_database_snapshot_scope"}, status_code=400)
        if not re.fullmatch(r"[0-9]{17}", str(steam_id or "").strip()) or not client_id:
            return JSONResponse({"detail": "SteamID 无效", "code": "invalid_steam_id"}, status_code=400)
        try:
            payload = await request.json()
        except (UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "改名参数无效", "code": "invalid_rename"}, status_code=400)
        player_name = str(payload.get("player_name", "") if isinstance(payload, dict) else "").strip()[:255]
        if not player_name:
            return JSONResponse({"detail": "玩家名称不能为空", "code": "invalid_rename"}, status_code=400)
        access = db.scalar(select(GroupAccess).where(GroupAccess.group_id == normalized_group))
        config_snapshot = remote_store.get_snapshot(db, normalized_group)
        if access is None or config_snapshot is None or not config_snapshot.client_id or not hmac.compare_digest(str(config_snapshot.client_id), client_id):
            return Response(status_code=404)
        try:
            result = await run_in_threadpool(
                rename_ftp_database_member,
                db, settings, access.user_id, normalized_group, normalized_profile,
                steam_id=str(steam_id).strip(), player_name=player_name,
            )
            if not isinstance(result, dict):
                return JSONResponse({"detail": "服务器数据库改名失败", "code": "database_mutation_failed"}, status_code=502)
            if result.get("success") is True:
                db.commit()
                return JSONResponse(result)
            db.rollback()
            return JSONResponse(result, status_code=409 if result.get("code") == "database_mutation_conflict" else 502)
        except PortalError as exc:
            db.rollback()
            return JSONResponse({"detail": str(exc), "code": "database_mutation_failed"}, status_code=502)
        except Exception:
            db.rollback()
            logger.exception("Database member rename failed group=%s profile=%s", normalized_group, normalized_profile)
            return JSONResponse({"detail": "服务器数据库改名失败，请稍后重试", "code": "database_mutation_failed"}, status_code=502)

    @app.post("/api/sync/v1/database-mutations/{group_id}/{profile_id}")
    async def mutate_database_from_windows(
        group_id: str,
        profile_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Apply one database reward on Linux without receiving a full DB file."""

        _require_sync_token(request)
        if settings.data_mode != "remote":
            return JSONResponse(
                {"detail": "网页未启用远程同步模式", "code": "remote_mode_disabled"},
                status_code=409,
            )
        pepper_mismatch = _card_pepper_mismatch_response(request)
        if pepper_mismatch is not None:
            return pepper_mismatch
        client_id, client_id_error = _extract_sync_client_id(request)
        if client_id_error is not None:
            return client_id_error
        try:
            normalized_group = validate_group_id(str(group_id))
            normalized_profile = normalize_integration_profile_id(profile_id)
        except (TypeError, ValueError, PortalError):
            return JSONResponse(
                {"detail": "数据库操作范围无效", "code": "invalid_database_snapshot_scope"},
                status_code=400,
            )
        try:
            payload = await request.json()
        except (UnicodeDecodeError, ValueError):
            return JSONResponse(
                {"detail": "数据库操作参数无效", "code": "invalid_database_mutation"},
                status_code=400,
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"detail": "数据库操作参数无效", "code": "invalid_database_mutation"},
                status_code=400,
            )
        steam_id = str(payload.get("steam_id", "") or "").strip()
        if not re.fullmatch(r"[0-9]{17}", steam_id):
            return JSONResponse(
                {"detail": "SteamID 必须是 17 位数字", "code": "invalid_steam_id"},
                status_code=400,
            )
        try:
            reward = int(payload.get("reward", 0))
        except (TypeError, ValueError, OverflowError):
            reward = -1
        if reward < 0 or reward > 1_000_000_000:
            return JSONResponse(
                {"detail": "奖励数值无效", "code": "invalid_amount"},
                status_code=400,
            )
        access = db.scalar(
            select(GroupAccess).where(GroupAccess.group_id == normalized_group)
        )
        if access is None:
            return JSONResponse(
                {"detail": "当前群未绑定网页账号", "code": "group_not_bound"},
                status_code=404,
            )
        # Do not make a live sign-in write depend on a separate, asynchronous
        # Windows configuration snapshot.  Immediately after saving an FTP
        # database that snapshot may be absent or stale even though the group,
        # profile and authenticated sync request are all valid.  The mutation
        # remains protected by the sync token, validated client identifier,
        # profile scope and the FTP SHA-256 compare-and-swap in the service.
        config_snapshot = remote_store.get_snapshot(db, normalized_group)
        if (
            config_snapshot is not None
            and config_snapshot.client_id
            and not hmac.compare_digest(str(config_snapshot.client_id), client_id)
        ):
            logger.warning(
                "Allowing database mutation during stale Windows snapshot group=%s profile=%s",
                normalized_group,
                normalized_profile,
            )
        try:
            result = await run_in_threadpool(
                _mutate_database_signin_in_worker,
                settings,
                access.user_id,
                normalized_group,
                normalized_profile,
                steam_id=steam_id,
                reward=reward,
                player_name=str(payload.get("player_name", "") or "")[:128],
                sign_date=str(payload.get("sign_date", "") or "")[:32],
                currency_column=str(payload.get("currency_column", "amount") or "amount")[:32],
                period_mode=str(payload.get("period_mode", "daily") or "daily")[:16],
                reward_config=payload.get("reward_config") if isinstance(payload.get("reward_config"), dict) else {},
            )
            if result.get("success") is True:
                return JSONResponse(result)
            status_code = 409 if result.get("code") == "database_mutation_conflict" else 502
            return JSONResponse(result, status_code=status_code)
        except PortalError as exc:
            return JSONResponse(
                {"detail": str(exc), "code": "database_mutation_failed"},
                status_code=502,
            )
        except Exception:
            logger.exception(
                "Database mutation failed group=%s profile=%s",
                normalized_group,
                normalized_profile,
            )
            return JSONResponse(
                {"detail": "服务器数据库事务失败，请稍后重试", "code": "database_mutation_failed"},
                status_code=502,
            )

    @app.post("/api/sync/v1/database-mutations/{group_id}/{profile_id}/reset")
    async def reset_database_signin_from_windows(
        group_id: str,
        profile_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Remove one authoritative server sign-in row without a Windows upload."""

        _require_sync_token(request)
        if settings.data_mode != "remote":
            return JSONResponse({"detail": "网页未启用远程同步模式", "code": "remote_mode_disabled"}, status_code=409)
        pepper_mismatch = _card_pepper_mismatch_response(request)
        if pepper_mismatch is not None:
            return pepper_mismatch
        client_id, client_id_error = _extract_sync_client_id(request)
        if client_id_error is not None:
            return client_id_error
        try:
            normalized_group = validate_group_id(str(group_id))
            normalized_profile = normalize_integration_profile_id(profile_id)
        except (TypeError, ValueError, PortalError):
            return JSONResponse({"detail": "数据库操作范围无效", "code": "invalid_database_snapshot_scope"}, status_code=400)
        try:
            payload = await request.json()
        except (UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "数据库操作参数无效", "code": "invalid_database_mutation"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "数据库操作参数无效", "code": "invalid_database_mutation"}, status_code=400)
        steam_id = str(payload.get("steam_id", "") or "").strip()
        sign_date = str(payload.get("sign_date", "") or "").strip()
        if not re.fullmatch(r"[0-9]{17}", steam_id) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", sign_date[:10]):
            return JSONResponse({"detail": "SteamID 或签到日期无效", "code": "invalid_database_mutation"}, status_code=400)
        reward = payload.get("reward")
        if reward is not None:
            try:
                reward = int(reward)
            except (TypeError, ValueError, OverflowError):
                return JSONResponse({"detail": "奖励数值无效", "code": "invalid_amount"}, status_code=400)
            if reward < 0 or reward > 1_000_000_000:
                return JSONResponse({"detail": "奖励数值无效", "code": "invalid_amount"}, status_code=400)
        access = db.scalar(select(GroupAccess).where(GroupAccess.group_id == normalized_group))
        if access is None:
            return JSONResponse({"detail": "当前群未绑定网页账号", "code": "group_not_bound"}, status_code=404)
        config_snapshot = remote_store.get_snapshot(db, normalized_group)
        if (
            config_snapshot is not None
            and config_snapshot.client_id
            and not hmac.compare_digest(str(config_snapshot.client_id), client_id)
        ):
            logger.warning(
                "Allowing database reset during stale Windows snapshot group=%s profile=%s",
                normalized_group,
                normalized_profile,
            )
        try:
            result = await run_in_threadpool(
                _reset_database_signin_in_worker,
                settings,
                access.user_id,
                normalized_group,
                normalized_profile,
                steam_id=steam_id,
                sign_date=sign_date[:10],
                reward=reward,
                remove_reward=bool(payload.get("remove_reward", True)),
                currency_column=str(payload.get("currency_column", "amount") or "amount")[:32],
            )
            if result.get("success") is True:
                return JSONResponse(result)
            return JSONResponse(result, status_code=409 if result.get("code") == "database_mutation_conflict" else 502)
        except Exception:
            logger.exception("Database sign-in reset failed group=%s profile=%s", normalized_group, normalized_profile)
            return JSONResponse({"detail": "服务器数据库重置失败，请稍后重试", "code": "database_mutation_failed"}, status_code=502)

    @app.post("/api/sync/v1/database-snapshots/{group_id}/{profile_id}")
    async def upload_database_snapshot_from_windows(
        group_id: str,
        profile_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Persist a changed Windows snapshot and write it back to FTP.

        The Windows worker never receives FTP credentials.  It sends the
        already-committed local SQLite bytes with the revision/hash it used;
        the portal checks that version, updates the configured FTP file, then
        publishes the new snapshot metadata for future Windows pulls.
        """

        _require_sync_token(request)
        if settings.data_mode != "remote":
            return JSONResponse(
                {"detail": "网页未启用远程同步模式", "code": "remote_mode_disabled"},
                status_code=409,
            )
        pepper_mismatch = _card_pepper_mismatch_response(request)
        if pepper_mismatch is not None:
            return pepper_mismatch
        client_id, client_id_error = _extract_sync_client_id(request)
        if client_id_error is not None:
            return client_id_error
        try:
            normalized_group = validate_group_id(str(group_id))
            normalized_profile = normalize_integration_profile_id(profile_id)
            expected_revision = int(
                request.headers.get("x-database-snapshot-revision", "0") or "0"
            )
        except (TypeError, ValueError, OverflowError, PortalError):
            return JSONResponse(
                {"detail": "数据库快照版本或范围无效", "code": "invalid_database_snapshot"},
                status_code=400,
            )
        expected_sha256 = str(
            request.headers.get("x-database-snapshot-sha256", "") or ""
        ).strip().lower()
        base_sha256 = str(
            request.headers.get("x-database-snapshot-base-sha256", "") or ""
        ).strip().lower()
        if (
            not client_id
            or expected_revision <= 0
            or expected_revision > 2**31 - 1
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", base_sha256)
        ):
            return JSONResponse(
                {"detail": "数据库快照版本信息不完整", "code": "invalid_database_snapshot"},
                status_code=400,
            )
        try:
            declared_size = int(request.headers.get("content-length", "0") or "0")
        except (TypeError, ValueError, OverflowError):
            declared_size = 0
        if declared_size > 256 * 1024 * 1024:
            return JSONResponse(
                {"detail": "SQLite 数据库文件超过 256 MB", "code": "database_snapshot_too_large"},
                status_code=413,
            )
        body = await request.body()
        if (
            not body
            or len(body) > 256 * 1024 * 1024
            or body[:16] != b"SQLite format 3\x00"
        ):
            return JSONResponse(
                {"detail": "SQLite 数据库快照格式无效", "code": "invalid_database_snapshot_body"},
                status_code=400,
            )
        body_sha256 = hashlib.sha256(body).hexdigest()
        if body_sha256 != expected_sha256:
            return JSONResponse(
                {"detail": "数据库快照校验失败", "code": "database_snapshot_hash_mismatch"},
                status_code=400,
            )

        access = db.scalar(
            select(GroupAccess).where(GroupAccess.group_id == normalized_group)
        )
        config_snapshot = remote_store.get_snapshot(
            db, normalized_group, for_update=True
        )
        if (
            access is None
            or config_snapshot is None
            or config_snapshot.client_id != client_id
        ):
            return Response(status_code=404)
        # Keep snapshot writes available for an already-bound database even
        # when the separate group-management license has expired. Revision,
        # base-hash, client binding and FTP validation below remain the write
        # authorization and concurrency guards.
        database_snapshot = db.scalar(
            select(PortalDatabaseSnapshot).where(
                PortalDatabaseSnapshot.owner_user_id == access.user_id,
                PortalDatabaseSnapshot.group_id == normalized_group,
                PortalDatabaseSnapshot.profile_id == normalized_profile,
            ).with_for_update()
        )
        if database_snapshot is None:
            return JSONResponse(
                {"detail": "数据库快照不存在，请先重新下载服务器数据库", "code": "database_snapshot_missing"},
                status_code=404,
            )
        current_hash = str(database_snapshot.sha256 or "").strip().lower()
        try:
            current_revision = int(database_snapshot.revision or 0)
        except (TypeError, ValueError, OverflowError):
            current_revision = 0
        if current_revision != expected_revision or current_hash != base_sha256:
            return JSONResponse(
                {"detail": "FTP 数据库已有新版本，请先重新下载服务器数据库", "code": "database_snapshot_conflict"},
                status_code=409,
            )

        try:
            integration_view = get_integration_config_view(
                db,
                access.user_id,
                group_id=normalized_group,
                profile_id=normalized_profile,
            )
            database_view = integration_view.get("database", {})
            if (
                not isinstance(database_view, dict)
                or not database_view.get("enabled")
                or str(database_view.get("engine", "")).strip().lower() != "sqlite"
                or not str(database_view.get("sqlite_path", "") or "").strip()
            ):
                return JSONResponse(
                    {"detail": "当前数据库配置没有启用 SQLite 数据库", "code": "database_ftp_not_configured"},
                    status_code=409,
                )
            ftp_config = get_saved_ftp_connection_config(
                db,
                settings,
                access.user_id,
                group_id=normalized_group,
                profile_id=normalized_profile,
            )
            await run_in_threadpool(
                upload_validated_ftp_database,
                ftp_config,
                str(database_view.get("sqlite_path", "")),
                body,
                expected_sha256=base_sha256,
            )
            saved = save_database_snapshot(
                db,
                access.user_id,
                normalized_group,
                normalized_profile,
                body,
            )
            # Keep the portal's current Windows projection aligned with the
            # new BLOB without creating a new editable configuration revision.
            projection = get_windows_database_profile_projection(
                db, access.user_id, normalized_group
            )
            config_value = deepcopy(config_snapshot.config_json)
            features = config_value.get("features")
            features = deepcopy(features) if isinstance(features, dict) else {}
            stats = features.get("stats")
            stats = deepcopy(stats) if isinstance(stats, dict) else {}
            stats.update(
                {
                    "database_profile": str(projection.get("database_profile", "") or ""),
                    "database_path": str(projection.get("database_path", "") or ""),
                    "database_profiles": deepcopy(projection.get("database_profiles", [])),
                }
            )
            features["stats"] = stats
            config_value["features"] = features
            config_snapshot.config_json = remote_store._secure_projection(
                config_value, config_snapshot.config_json
            )
            db.commit()
            logger.debug(
                "Windows database reward uploaded group=%s profile=%s revision=%s",
                normalized_group,
                normalized_profile,
                saved.get("revision", 0),
            )
            return JSONResponse(
                {
                    "success": True,
                    "code": "database_snapshot_uploaded",
                    "revision": int(saved.get("revision", 0) or 0),
                    "sha256": str(saved.get("sha256", "") or ""),
                    "size_bytes": len(body),
                }
            )
        except PortalError as exc:
            db.rollback()
            logger.warning(
                "Windows database reward upload failed group=%s profile=%s reason=%s",
                normalized_group,
                normalized_profile,
                str(exc)[:120],
            )
            return JSONResponse(
                {"detail": str(exc), "code": "database_snapshot_upload_failed"},
                status_code=502,
            )
        except Exception:
            db.rollback()
            logger.exception(
                "Windows database reward upload crashed group=%s profile=%s",
                normalized_group,
                normalized_profile,
            )
            return JSONResponse(
                {"detail": "数据库变更已在 Windows 本地执行，但上传服务器数据库失败，请检查 FTP 权限", "code": "database_snapshot_upload_failed"},
                status_code=502,
            )

    @app.get("/user/groups/{group_id}/reply-assets/{asset_id}")
    @app.get("/admin/groups/{group_id}/reply-assets/{asset_id}")
    def portal_reply_asset(
        group_id: str,
        asset_id: int,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Serve a saved automatic-reply image to an authorized group user.

        Reply assets are private database records, unlike shared appearance
        images.  Require the normal group access check and verify that the
        exact asset is still referenced by the current projection before
        returning bytes.  This gives the Linux UI a useful preview without
        exposing an unrestricted asset-ID endpoint.
        """

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id)
        if isinstance(access, RedirectResponse):
            return access
        if settings.data_mode != "remote":
            return Response(status_code=404)
        try:
            config = remote_store.get_config(db, group_id)
            asset = reply_asset_service.get_referenced_reply_asset(
                db, group_id, asset_id, config
            )
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to load portal reply asset group_id=%s asset_id=%s request_id=%s",
                group_id,
                asset_id,
                getattr(request.state, "request_id", ""),
            )
            return Response(status_code=404)
        if asset is None:
            return Response(status_code=404)
        etag = f'"{asset.sha256}"'
        if request.headers.get("if-none-match", "").strip() == etag:
            return Response(
                status_code=304,
                headers={
                    "Cache-Control": "private, no-cache",
                    "ETag": etag,
                    "Vary": "Cookie",
                },
            )
        return Response(
            content=bytes(asset.image_data),
            media_type=asset.content_type,
            headers={
                "Cache-Control": "private, no-cache",
                "ETag": etag,
                "Vary": "Cookie",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/sync/v1/config-pull")
    async def sync_config_pull(request: Request):
        _require_sync_token(request)
        if settings.data_mode != "remote":
            return JSONResponse(
                {"status": "error", "code": "remote_mode_disabled"},
                status_code=409,
            )
        pepper_mismatch = _card_pepper_mismatch_response(request)
        if pepper_mismatch is not None:
            return pepper_mismatch
        try:
            payload = await request.json()
        except (UnicodeDecodeError, ValueError):
            return JSONResponse(
                {"status": "error", "code": "invalid_pull_target"},
                status_code=400,
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"status": "error", "code": "invalid_pull_target"},
                status_code=400,
            )
        client_id, client_id_error = _extract_sync_client_id(request, payload)
        if client_id_error is not None:
            return client_id_error
        group_id = str(payload.get("group_id", "") or "").strip()
        if not remote_store.GROUP_ID_RE.fullmatch(group_id):
            return JSONResponse(
                {"status": "error", "code": "invalid_pull_target"},
                status_code=400,
            )
        try:
            return await _run_serialized_sync_write(
                _process_group_config_pull_request,
                group_id,
                client_id,
                lock_keys=[group_id],
            )
        except Exception:
            logger.exception(
                "Windows 手动拉取群配置请求入队失败 client=%s group=%s",
                client_id,
                group_id,
            )
            return JSONResponse(
                {"status": "error", "code": "sync_storage_busy"},
                status_code=503,
                headers={"Retry-After": "5"},
            )

    @app.post("/api/sync/v1/exchange")
    async def sync_exchange(request: Request):
        _require_sync_token(request)
        if settings.data_mode != "remote":
            return JSONResponse(
                {
                    "detail": "网页未启用远程同步模式",
                    "code": "remote_mode_disabled",
                },
                status_code=409,
            )
        pepper_mismatch = _card_pepper_mismatch_response(request)
        if pepper_mismatch is not None:
            return pepper_mismatch
        try:
            payload = await request.json()
        except (UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "同步数据格式无效"}, status_code=400)
        # ``Request.json`` accepts any JSON value.  Reject arrays/scalars
        # before accessing optional fields below so a malformed but otherwise
        # authenticated request returns a useful 400 instead of a server 500.
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "同步数据格式无效"}, status_code=400)
        groups = payload.get("groups", [])
        if not isinstance(groups, list) or len(groups) > 5000:
            return JSONResponse({"detail": "同步数据格式无效"}, status_code=400)
        console_events = payload.get("console_events", [])
        if not isinstance(console_events, list) or len(console_events) > 500:
            return JSONResponse({"detail": "运行日志格式无效"}, status_code=400)
        apply_failures = payload.get("apply_failures", [])
        if not isinstance(apply_failures, list) or len(apply_failures) > 5000:
            return JSONResponse({"detail": "同步失败回执格式无效"}, status_code=400)
        runtime_summaries = payload.get("runtime_summaries", {})
        if not isinstance(runtime_summaries, dict) or len(runtime_summaries) > 5000:
            return JSONResponse({"detail": "运行摘要格式无效"}, status_code=400)
        client_id, client_id_error = _extract_sync_client_id(request, payload)
        if client_id_error is not None:
            return client_id_error
        known_group_ids = payload.get("known_group_ids", [])
        if not isinstance(known_group_ids, list) or len(known_group_ids) > 5000:
            return JSONResponse({"detail": "群列表格式无效"}, status_code=400)
        local_group_ids = payload.get("local_group_ids", [])
        if not isinstance(local_group_ids, list) or len(local_group_ids) > 5000:
            return JSONResponse({"detail": "本机群列表格式无效"}, status_code=400)
        authoritative_group_ids = payload.get("authoritative_group_ids") is True
        has_config_source_flag = "config_source_complete" in payload
        has_auth_source_flag = "auth_source_complete" in payload
        if has_config_source_flag != has_auth_source_flag:
            return JSONResponse({"detail": "同步来源状态格式无效"}, status_code=400)
        if has_config_source_flag:
            config_source_complete = payload.get("config_source_complete")
            auth_source_complete = payload.get("auth_source_complete")
            if not isinstance(config_source_complete, bool) or not isinstance(
                auth_source_complete, bool
            ):
                return JSONResponse({"detail": "同步来源状态格式无效"}, status_code=400)
        else:
            # Compatibility with older Windows plugin releases that did not
            # send source-health fields.  A non-empty known inventory is
            # enough to accept that client's ordinary projection, but deletion
            # remains disabled because the old client cannot prove that its
            # local files were read completely.  An empty legacy envelope is
            # still treated as a protected heartbeat.
            legacy_has_inventory = bool(groups or known_group_ids)
            config_source_complete = bool(authoritative_group_ids or legacy_has_inventory)
            auth_source_complete = bool(authoritative_group_ids or legacy_has_inventory)
        game_auth_source_complete = payload.get("game_auth_source_complete")
        if game_auth_source_complete is None:
            game_auth_source_complete = bool(groups or known_group_ids or authoritative_group_ids)
        if not isinstance(game_auth_source_complete, bool):
            return JSONResponse({"detail": "游戏授权来源状态格式无效"}, status_code=400)
        sync_scope_complete = payload.get("sync_scope_complete", False)
        if not isinstance(sync_scope_complete, bool):
            return JSONResponse({"detail": "同步范围状态格式无效"}, status_code=400)
        # The Windows process reports only errors that happened *after* a
        # previous exchange succeeded (local JSON write or ACK).  Keep the
        # exact-revision guard in remote_store so an old report cannot turn a
        # newly saved configuration into an error state.  The whole
        # synchronous transaction runs in a worker thread so MySQL locks and
        # large projections do not block unrelated browser requests.
        exchange_lock_keys = {
            str(value or "").strip()
            for value in [
                *known_group_ids,
                *local_group_ids,
                *runtime_summaries.keys(),
                *(
                    item.get("group_id")
                    for item in groups
                    if isinstance(item, dict)
                ),
                *(
                    item.get("group_id")
                    for item in apply_failures
                    if isinstance(item, dict)
                ),
            ]
            if str(value or "").strip()
        }
        try:
            return await _run_serialized_sync_write(
                _process_sync_exchange,
                client_id,
                groups,
                known_group_ids,
                console_events,
                apply_failures,
                authoritative_group_ids,
                runtime_summaries,
                config_source_complete,
                auth_source_complete,
                sync_scope_complete,
                settings,
                game_auth_source_complete,
                _public_request_origin(request),
                local_group_ids,
                timeout=settings.sync_exchange_timeout_seconds,
                lock_keys=exchange_lock_keys,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Windows 同步事务超时 client=%s timeout=%ss",
                client_id,
                settings.sync_exchange_timeout_seconds,
            )
            return JSONResponse(
                {
                    "detail": "网页同步繁忙，本次未完成；Windows 将保留本地数据并自动重试。",
                    "code": "sync_timeout",
                },
                status_code=503,
                headers={"Retry-After": "5"},
            )
        except Exception as exc:
            # A large/legacy runtime projection must never prevent the
            # configuration and heartbeat exchange from completing. Retry the
            # same transaction without read-only runtime panels; the member
            # directory is uploaded through its dedicated endpoint and will
            # be retried independently.
            if runtime_summaries:
                try:
                    fallback = await _run_serialized_sync_write(
                        _process_sync_exchange,
                        client_id,
                        groups,
                        known_group_ids,
                        console_events,
                        apply_failures,
                        authoritative_group_ids,
                        {},
                        config_source_complete,
                        auth_source_complete,
                        sync_scope_complete,
                        settings,
                        game_auth_source_complete,
                        _public_request_origin(request),
                        local_group_ids,
                        timeout=settings.sync_exchange_timeout_seconds,
                        lock_keys=exchange_lock_keys,
                    )
                    if isinstance(fallback, dict):
                        fallback["runtime_deferred"] = True
                        return fallback
                except Exception:
                    logger.exception(
                        "Windows 同步事务在跳过运行数据后仍失败 client=%s",
                        client_id,
                    )
            # Never turn a transient portal database lock or a malformed
            # runtime projection into an opaque reverse-proxy 500. Windows
            # can then retain its local cache and retry the exact envelope.
            retryable = _is_retryable_sync_storage_error(exc)
            logger.exception(
                "Windows 同步事务失败 client=%s retryable=%s",
                client_id,
                retryable,
            )
            return JSONResponse(
                {
                    "detail": (
                        "网页数据库正在处理其他同步，请稍后自动重试。"
                        if retryable
                        else "网页同步事务失败，请查看网页服务日志。"
                    ),
                    "code": "sync_storage_busy" if retryable else "sync_transaction_failed",
                },
                status_code=503,
            )

    @app.post("/api/sync/v1/ack")
    async def sync_ack(request: Request):
        _require_sync_token(request)
        if settings.data_mode != "remote":
            return JSONResponse(
                {
                    "detail": "网页未启用远程同步模式",
                    "code": "remote_mode_disabled",
                },
                status_code=409,
            )
        pepper_mismatch = _card_pepper_mismatch_response(request)
        if pepper_mismatch is not None:
            return pepper_mismatch
        try:
            payload = await request.json()
        except (UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "同步数据格式无效"}, status_code=400)
        # See exchange above: JSON arrays and scalar values are not valid sync
        # envelopes and must not reach ``payload.get``.
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "同步数据格式无效"}, status_code=400)
        groups = payload.get("groups", [])
        if not isinstance(groups, list) or len(groups) > 5000:
            return JSONResponse({"detail": "同步数据格式无效"}, status_code=400)
        client_id, client_id_error = _extract_sync_client_id(request, payload)
        if client_id_error is not None:
            return client_id_error
        return await _run_serialized_sync_write(
            _process_sync_ack,
            client_id,
            groups,
            lock_keys=[
                item.get("group_id")
                for item in groups
                if isinstance(item, dict)
            ],
        )

    @app.post("/api/sync/v1/member-directory")
    async def sync_member_directory(request: Request):
        """Receive a refreshed QQ member directory independently of heartbeat."""

        _require_sync_token(request)
        if settings.data_mode != "remote":
            return JSONResponse(
                {"detail": "网页未启用远程同步模式", "code": "remote_mode_disabled"},
                status_code=409,
            )
        pepper_mismatch = _card_pepper_mismatch_response(request)
        if pepper_mismatch is not None:
            return pepper_mismatch
        try:
            payload = await request.json()
        except (UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "成员目录格式无效"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "成员目录格式无效"}, status_code=400)
        scope_type = str(payload.get("scope_type", "group") or "group").strip().lower()
        group_id = str(payload.get("group_id", "")).strip()
        scope_id = str(payload.get("scope_id", "")).strip()
        if scope_type == "private":
            if not re.fullmatch(r"\d{5,20}", scope_id):
                return JSONResponse({"detail": "私聊对象格式无效"}, status_code=400)
            group_id = ""
        else:
            try:
                group_id = validate_group_id(group_id)
            except ValueError:
                return JSONResponse({"detail": "群号格式无效"}, status_code=400)
        members = payload.get("members", [])
        if not isinstance(members, list) or len(members) > 10000:
            return JSONResponse({"detail": "成员目录格式无效"}, status_code=400)
        members = [item for item in members if isinstance(item, dict)]
        client_id, client_id_error = _extract_sync_client_id(request, payload)
        if client_id_error is not None:
            return client_id_error
        levels_pending = bool(payload.get("levels_pending", False))
        snapshot_complete = bool(payload.get("snapshot_complete", True))
        try:
            expected_count = max(0, min(10000, int(payload.get("expected_count", 0) or 0)))
        except (TypeError, ValueError, OverflowError):
            expected_count = 0
        result = await _run_serialized_sync_write(
            _process_member_directory_upload,
            client_id,
            group_id,
            members,
            levels_pending,
            snapshot_complete,
            expected_count,
            lock_keys=[group_id or f"private:{scope_id}"],
        )
        stored_count = (
            int(result.get("stored_count", 0) or 0)
            if isinstance(result, dict)
            else 0
        )
        if isinstance(result, dict) and result.get("status") == "ok":
            logger.info(
                "Linux 已成功接收 Windows 群成员数据 group=%s received=%s stored=%s complete=%s expected=%s",
                group_id or scope_id,
                len(members),
                stored_count,
                snapshot_complete,
                expected_count,
            )
        elif isinstance(result, dict) and result.get("status") == "rejected":
            logger.warning(
                "Linux 未保存 Windows 群成员数据 group=%s code=%s received=%s",
                group_id or scope_id,
                result.get("code", "group_client_not_bound"),
                len(members),
            )
        else:
            logger.warning(
                "Linux 保存 Windows 群成员数据失败 group=%s code=%s received=%s",
                group_id or scope_id,
                result.get("code", "member_directory_storage_failed")
                if isinstance(result, dict)
                else "invalid_result",
                len(members),
            )
        if result.get("status") == "error":
            return JSONResponse(
                {
                    "detail": "网页数据库正在处理其他同步，成员列表已保留在 Windows，稍后自动重试。",
                    **result,
                },
                status_code=503,
            )
        if result.get("status") != "ok":
            return JSONResponse(
                {"detail": "该群尚未绑定当前 Windows 执行端", **result},
                status_code=409,
            )
        return result

    @app.post("/api/sync/v1/message-cache")
    async def sync_message_cache(request: Request):
        """Receive newly written Windows group-message summaries immediately."""

        _require_sync_token(request)
        if settings.data_mode != "remote":
            return JSONResponse(
                {"detail": "网页未启用远程同步模式", "code": "remote_mode_disabled"},
                status_code=409,
            )
        pepper_mismatch = _card_pepper_mismatch_response(request)
        if pepper_mismatch is not None:
            return pepper_mismatch
        try:
            payload = await request.json()
        except (UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "消息缓存格式无效"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "消息缓存格式无效"}, status_code=400)
        scope_type = str(payload.get("scope_type", "group") or "group").strip().lower()
        group_id = str(payload.get("group_id", "")).strip()
        scope_id = str(payload.get("scope_id", "")).strip()
        if scope_type == "private":
            if not re.fullmatch(r"\d{5,20}", scope_id):
                return JSONResponse({"detail": "私聊对象格式无效"}, status_code=400)
            group_id = ""
        else:
            try:
                group_id = validate_group_id(group_id)
            except ValueError:
                return JSONResponse({"detail": "群号格式无效"}, status_code=400)
        messages = payload.get("messages", [])
        if not isinstance(messages, list) or len(messages) > 100:
            return JSONResponse({"detail": "消息缓存格式无效"}, status_code=400)
        messages = [item for item in messages if isinstance(item, dict)]
        complete_user_id = str(payload.get("complete_user_id", "") or "").strip()
        if complete_user_id and not re.fullmatch(r"\d{5,20}", complete_user_id):
            return JSONResponse({"detail": "成员 QQ 号格式无效"}, status_code=400)
        try:
            complete_count = max(0, min(5000, int(payload.get("complete_count", 0) or 0)))
        except (TypeError, ValueError, OverflowError):
            complete_count = 0
        client_id, client_id_error = _extract_sync_client_id(request, payload)
        if client_id_error is not None:
            return client_id_error
        result = await _run_serialized_sync_write(
            _process_message_cache_upload,
            client_id,
            group_id,
            messages,
            scope_type,
            scope_id,
            complete_user_id,
            complete_count,
            lock_keys=[group_id or f"private:{scope_id}"],
        )
        if result.get("status") == "error":
            return JSONResponse(
                {
                    "detail": "网页消息缓存正在处理其他同步，Windows 会在下一次心跳重试。",
                    **result,
                },
                status_code=503,
            )
        if result.get("status") != "ok":
            return JSONResponse(
                {"detail": "该群尚未绑定当前 Windows 执行端", **result},
                status_code=409,
            )
        return result

    @app.get("/api/runner/v1/capabilities")
    def runner_capabilities(request: Request, db: Session = Depends(get_db)):
        """Authenticated discovery endpoint reserved for a future Linux runner."""
        _require_sync_token(request)
        if settings.data_mode != "remote":
            return JSONResponse(
                {
                    "detail": "网页未启用远程同步模式",
                    "code": "remote_mode_disabled",
                },
                status_code=409,
            )
        return get_runner_deployment(db)

    @app.get("/", include_in_schema=False)
    def index(request: Request, db: Session = Depends(get_db)):
        return _redirect(_role_home(_current_user(request, db)))

    @app.get("/login", include_in_schema=False)
    def legacy_login(request: Request, db: Session = Depends(get_db)):
        """Compatibility bookmark: the actual login pages are role-specific."""
        return _redirect(_role_home(_current_user(request, db)))

    @app.get("/admin/login", response_class=HTMLResponse)
    @app.get("/user/login", response_class=HTMLResponse)
    def login_page(request: Request, db: Session = Depends(get_db)):
        if request.url.path == "/login":
            return _redirect("/user/login")
        current = _current_user(request, db)
        if current:
            return _redirect(_role_home(current))
        prefix = _auth_prefix(request)
        return templates.TemplateResponse(
            request=request,
            name="admin_login.html" if prefix == "/admin" else "login.html",
            context=_template_context(request, auth_prefix=prefix),
        )

    @app.post("/admin/login")
    @app.post("/user/login")
    async def login(request: Request, db: Session = Depends(get_db)):
        if request.url.path == "/login":
            return _redirect("/user/login")
        prefix = _auth_prefix(request)
        form = await _validated_form(request)
        username = normalize_username(str(form.get("username", "")))
        password = str(form.get("password", ""))
        login_card = str(form.get("license_key", "") or "").strip()
        ip_address = request.client.host if request.client else ""
        cutoff = utcnow() - timedelta(minutes=15)
        db.execute(
            delete(LoginAttempt).where(
                LoginAttempt.created_at < utcnow() - timedelta(days=30)
            )
        )
        identity_failed_count = db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.ip_address == ip_address,
                LoginAttempt.identity == username[:128],
                LoginAttempt.successful.is_(False),
                LoginAttempt.created_at >= cutoff,
            )
        ) or 0
        ip_failed_count = db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.ip_address == ip_address,
                LoginAttempt.successful.is_(False),
                LoginAttempt.created_at >= cutoff,
            )
        ) or 0
        if identity_failed_count >= 10 or ip_failed_count >= 50:
            db.rollback()
            _flash(request, "登录失败次数过多，请 15 分钟后重试", "error")
            return _redirect(f"{prefix}/login")

        user = db.scalar(select(User).where(User.username == username))
        candidate_hash = (
            user.password_hash if user is not None else request.app.state.dummy_password_hash
        )
        password_matches = verify_password(password, candidate_hash)
        credentials_valid = bool(user and user.is_active and password_matches)
        requested_admin = prefix == "/admin"
        claimed_access = None
        if credentials_valid and user and not requested_admin and not user.is_admin and login_card:
            try:
                claimed_access = claim_activated_card(db, user, login_card, settings)
                add_audit_log(
                    db,
                    user,
                    claimed_access.group_id,
                    "license_claim",
                    "登录时登记群聊已激活卡密",
                    request,
                )
                db.flush()
            except (ValueError, PortalError, IntegrityError) as exc:
                db.rollback()
                _flash(request, str(exc) or "卡密登记失败，请检查卡密是否有效", "error")
                return _redirect(f"{prefix}/login")
        entitlement_valid = bool(
            credentials_valid
            and user
            and (user.is_admin or user_has_login_entitlement(db, user))
        )
        role_matches = bool(
            credentials_valid and user and bool(user.is_admin) == requested_admin
        )
        if credentials_valid and entitlement_valid and not role_matches:
            # Do not create a login session, audit entry, refresh request, or
            # successful LoginAttempt for a credential entered in the wrong
            # portal.  The two areas deliberately authenticate independently.
            db.rollback()
            target = "/admin/login" if user and user.is_admin else "/user/login"
            _flash(
                request,
                "该账号不属于当前入口，请使用对应的登录入口",
                "error",
            )
            return _redirect(target)
        success = credentials_valid and entitlement_valid and role_matches
        db.add(
            LoginAttempt(
                identity=username[:128], ip_address=ip_address[:64], successful=success
            )
        )
        if not success:
            db.commit()
            if credentials_valid and not entitlement_valid:
                _flash(
                    request,
                    "账号绑定的群聊卡密已删除或授权已撤销，暂时不能登录。请联系管理员重新授权。",
                    "error",
                )
            else:
                _flash(request, "账号或密码错误", "error")
            return _redirect(f"{prefix}/login")

        user.last_login_at = utcnow()
        add_audit_log(db, user, None, "login", "账号登录", request)
        refresh_requested = 0
        if settings.data_mode == "remote" and not user.is_admin:
            # The Windows bot is the execution endpoint and keeps the source
            # projection.  A browser login must not try to connect to it;
            # instead persist a pull-only refresh request for its next normal
            # outbound heartbeat.
            group_ids = list(
                db.scalars(
                    select(GroupAccess.group_id).where(GroupAccess.user_id == user.id)
                )
            )
            refresh_requested = remote_store.request_group_refresh(db, group_ids)
            if refresh_requested:
                add_audit_log(
                    db,
                    user,
                    None,
                    "portal_refresh_requested",
                    f"登录后请求 Windows 补传 {refresh_requested} 个分群配置",
                    request,
                )
        db.commit()
        request.session.clear()
        request.session["user_id"] = user.id
        ensure_csrf_token(request.session)
        if prefix == "/admin" and not user.is_admin:
            request.session.clear()
            _flash(request, "管理员入口仅限管理员账号使用", "error")
            return _redirect("/admin/login")
        if refresh_requested:
            _flash(
                request,
                "已请求 Windows 机器人补传当前分群配置；机器人联网后会在下一轮同步自动完成。",
                "info",
            )
        if claimed_access is not None:
            _flash(
                request,
                f"卡密已绑定群 {claimed_access.group_id}，已初始化默认群聊功能。",
                "success",
            )
            return _redirect(_group_path(user, claimed_access.group_id))
        return _redirect(_role_home(user))

    @app.get("/forgot-password", include_in_schema=False)
    def legacy_forgot_password():
        return _redirect("/user/forgot-password")

    @app.get("/user/forgot-password", response_class=HTMLResponse)
    @app.get("/admin/forgot-password", response_class=HTMLResponse)
    def forgot_password_page(request: Request, db: Session = Depends(get_db)):
        current = _current_user(request, db)
        if current:
            return _redirect(_role_home(current))
        prefix = _auth_prefix(request)
        return templates.TemplateResponse(
            request=request,
            name="admin_forgot_password.html" if prefix == "/admin" else "forgot_password.html",
            context=_template_context(
                request,
                reset_email=str(request.session.get("password_reset_email", "")),
                auth_prefix=prefix,
            ),
        )

    @app.post("/user/forgot-password/code")
    @app.post("/admin/forgot-password/code")
    async def request_password_reset(request: Request, db: Session = Depends(get_db)):
        prefix = _auth_prefix(request)
        form = await _validated_form(request)
        try:
            reset_email = validate_email(str(form.get("email", "")))
            target = db.scalar(
                select(User).where(User.email == reset_email, User.is_active.is_(True))
            )
            if target is not None and bool(target.is_admin) != (prefix == "/admin"):
                raise PortalError("请使用该账号所属区域的密码找回入口")
            email = request_password_reset_code(
                db,
                settings,
                reset_email,
                request.client.host if request.client else "",
            )
            db.commit()
            request.session["password_reset_email"] = email
            request.session["password_reset_scope"] = prefix
            _flash(request, "找回验证码已发送，请查看邮箱", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(f"{prefix}/forgot-password")

    @app.post("/user/forgot-password")
    @app.post("/admin/forgot-password")
    async def reset_password_by_email(request: Request, db: Session = Depends(get_db)):
        prefix = _auth_prefix(request)
        form = await _validated_form(request)
        try:
            if request.session.get("password_reset_scope") != prefix:
                raise PortalError("请从当前入口重新获取邮箱验证码")
            email = validate_email(str(form.get("email", "")))
            password = validate_password(str(form.get("password", "")))
            if password != str(form.get("password_confirm", "")):
                raise PortalError("两次输入的密码不一致")
            user = db.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
            if user is None:
                raise PortalError("该邮箱没有可找回的有效账号")
            if bool(user.is_admin) != (prefix == "/admin"):
                raise PortalError("请使用该账号所属区域的密码找回入口")
            consume_password_reset_code(
                db,
                settings,
                email,
                str(form.get("email_code", "")),
            )
            user.password_hash = hash_password(password)
            add_audit_log(
                db,
                user,
                None,
                "password_reset_email",
                "通过邮箱验证码重置密码",
                request,
            )
            db.commit()
        except VerificationCodeError as exc:
            db.commit()
            _flash(request, str(exc), "error")
            return _redirect(f"{prefix}/forgot-password")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
            return _redirect(f"{prefix}/forgot-password")

        request.session.clear()
        _flash(request, "密码已重置，请使用新密码登录", "success")
        return _redirect(f"{prefix}/login")

    @app.get("/register", include_in_schema=False)
    def legacy_register():
        return _redirect("/user/register")

    @app.get("/user/register", response_class=HTMLResponse)
    def register_page(request: Request, db: Session = Depends(get_db)):
        current = _current_user(request, db)
        if current:
            return _redirect(_role_home(current))
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context=_template_context(
                request,
                registration_email=str(request.session.get("registration_email", "")),
                auth_prefix="/user",
            ),
        )

    @app.post("/user/register/code")
    async def request_register_code(request: Request, db: Session = Depends(get_db)):
        current = _current_user(request, db)
        if current is not None:
            _flash(request, "已登录账号不能创建新的客户账号", "warning")
            return _redirect(_role_home(current))
        form = await _validated_form(request)
        try:
            email = request_registration_code(
                db,
                settings,
                str(form.get("email", "")),
                request.client.host if request.client else "",
            )
            db.commit()
            request.session["registration_email"] = email
            _flash(request, "验证码已发送，请查看邮箱（有效期以邮件提示为准）", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect("/user/register")

    @app.post("/user/register")
    async def register(request: Request, db: Session = Depends(get_db)):
        current = _current_user(request, db)
        if current is not None:
            _flash(request, "已登录账号不能创建新的客户账号", "warning")
            return _redirect(_role_home(current))
        form = await _validated_form(request)
        try:
            username = validate_username(str(form.get("username", "")))
            email = validate_email(str(form.get("email", "")))
            password = validate_password(str(form.get("password", "")))
            if password != str(form.get("password_confirm", "")):
                raise PortalError("两次输入的密码不一致")
            if db.scalar(select(User).where(User.username == username)):
                raise PortalError("该账号已经注册")
            if db.scalar(select(User).where(User.email == email)):
                raise PortalError("该邮箱已经注册，请直接登录")
            consume_registration_code(
                db,
                settings,
                email,
                str(form.get("email_code", "")),
            )

            user = User(
                username=username,
                email=email,
                password_hash=hash_password(password),
            )
            db.add(user)
            db.flush()
            add_audit_log(
                db,
                user,
                None,
                "email_register",
                "通过邮箱验证码注册网页账号",
                request,
            )
            db.commit()
        except VerificationCodeError as exc:
            # Expiry and incorrect-attempt counters must be retained; otherwise
            # a caller could retry the same code without a limit.
            db.commit()
            _flash(request, str(exc), "error")
            return _redirect("/user/register")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
            return _redirect("/user/register")
        except IntegrityError:
            db.rollback()
            _flash(request, "账号或邮箱已经注册", "error")
            return _redirect("/user/register")

        request.session.clear()
        request.session["user_id"] = user.id
        ensure_csrf_token(request.session)
        _flash(request, "注册成功，请先登记群内已激活的授权卡密", "success")
        return _redirect("/user")

    @app.post("/user/logout")
    @app.post("/admin/logout")
    async def logout(request: Request, db: Session = Depends(get_db)):
        scoped_user = _require_user(request, db)
        if isinstance(scoped_user, RedirectResponse):
            return scoped_user
        await _validated_form(request)
        user = _current_user(request, db)
        if user:
            add_audit_log(db, user, None, "logout", "账号退出", request)
            db.commit()
        request.session.clear()
        return _redirect("/admin/login" if scoped_user.is_admin else "/user/login")

    @app.get("/dashboard", include_in_schema=False)
    def legacy_dashboard(request: Request, db: Session = Depends(get_db)):
        return _redirect(_role_home(_current_user(request, db)))

    @app.get("/user", response_class=HTMLResponse)
    @app.get("/user/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, db: Session = Depends(get_db)):
        user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        if user.is_admin:
            return _redirect("/admin")
        groups = list_user_groups(db, user.id)
        group_search = (
            str(request.query_params.get("group_search", ""))
            .replace("\r", " ")
            .replace("\n", " ")
            .strip()[:64]
        )
        filtered_groups = groups
        if group_search:
            needle = group_search.casefold()
            filtered_groups = [
                item
                for item in groups
                if needle in str(getattr(item.get("access"), "group_id", "")).casefold()
                or needle in str(item.get("group_display_name", "")).casefold()
            ]
        group_pagination = _pagination_context(
            request,
            len(filtered_groups),
            page_param="group_page",
            page_size=25,
        )
        start = (group_pagination["page"] - 1) * group_pagination["page_size"]
        display_groups = filtered_groups[
            start : start + group_pagination["page_size"]
        ]
        logs = list(
            db.scalars(
                select(AuditLog)
                .where(AuditLog.user_id == user.id)
                .order_by(AuditLog.created_at.desc())
                .limit(8)
            )
        )
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=_template_context(
                request,
                user,
                groups=groups,
                display_groups=display_groups,
                group_search=group_search,
                group_pagination=group_pagination,
                logs=logs,
            ),
        )

    @app.post("/user/sync-refresh")
    async def request_all_user_group_sync_refresh(
        request: Request, db: Session = Depends(get_db)
    ):
        """Queue a pull-only Windows refresh for every group owned by a customer.

        The portal never tries to connect back to a home Windows machine.  It
        stores an idempotent hint here; the authenticated AstrBot heartbeat
        picks it up, performs one full local upload, and clears the hint.
        Groups that have not reached the portal at all are covered by the
        existing ``missing_group_ids`` reconciliation response on that same
        heartbeat.
        """

        user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        await _validated_form(request)
        if settings.data_mode != "remote":
            _flash(request, "当前网页与本地插件直连，无需请求远程同步。", "info")
            return _redirect("/user#sync-summary")

        group_ids = list(
            db.scalars(
                select(GroupAccess.group_id)
                .where(GroupAccess.user_id == user.id)
                .order_by(GroupAccess.group_id)
            )
        )
        if not group_ids:
            _flash(request, "当前账号还没有已登记的分群，暂时不能请求同步。", "info")
            return _redirect("/user#sync-summary")

        try:
            snapshot_count = int(
                db.scalar(
                    select(func.count())
                    .select_from(GroupConfigSnapshot)
                    .where(GroupConfigSnapshot.group_id.in_(group_ids))
                )
                or 0
            )
            requested = remote_store.request_group_refresh(db, group_ids)
            add_audit_log(
                db,
                user,
                None,
                "portal_bulk_sync_refresh_requested",
                f"请求 Windows 补传 {len(group_ids)} 个已绑定分群配置",
                request,
                {
                    "group_count": len(group_ids),
                    "refresh_requested": requested,
                    "snapshot_count": snapshot_count,
                },
            )
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            logger.exception("Unable to queue customer sync refresh user_id=%s", user.id)
            _flash(request, "同步请求暂时无法保存，请稍后重试。", "error")
            return _redirect("/user#sync-summary")

        missing_count = max(0, len(group_ids) - snapshot_count)
        if requested:
            message = f"已请求 Windows 补传 {requested} 个分群配置。"
        else:
            message = "已登记的分群配置已在等待 Windows 同步，无需重复请求。"
        if missing_count:
            message += f"另有 {missing_count} 个分群尚未首次上传，Windows 下次心跳会自动发现并补传。"
        else:
            message += "Windows 电脑联网后会在下一轮同步中自动完成。"
        _flash(request, message, "success" if requested else "info")
        return _redirect("/user#sync-summary")

    @app.get("/user/feedback", response_class=HTMLResponse)
    def user_feedback(request: Request, db: Session = Depends(get_db)):
        """Show a customer's own reports and the administrator's response."""

        user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        feedback_total = int(
            db.scalar(
                select(func.count())
                .select_from(PortalFeedback)
                .where(PortalFeedback.user_id == user.id)
            )
            or 0
        )
        feedback_pagination = _pagination_context(
            request, feedback_total, page_size=20
        )
        feedback_offset = (
            feedback_pagination["page"] - 1
        ) * feedback_pagination["page_size"]
        feedback_items = list(
            db.scalars(
                select(PortalFeedback)
                .where(PortalFeedback.user_id == user.id)
                .order_by(PortalFeedback.created_at.desc(), PortalFeedback.id.desc())
                .offset(feedback_offset)
                .limit(feedback_pagination["page_size"])
            )
        )
        groups = list(
            db.scalars(
                select(GroupAccess)
                .where(GroupAccess.user_id == user.id)
                .order_by(GroupAccess.group_id)
            )
        )
        return templates.TemplateResponse(
            request=request,
            name="user_feedback.html",
            context=_template_context(
                request,
                user,
                feedback_items=feedback_items,
                feedback_pagination=feedback_pagination,
                feedback_total=feedback_total,
                groups=groups,
                feedback_categories=FEEDBACK_CATEGORIES,
                feedback_statuses=FEEDBACK_STATUSES,
            ),
        )

    @app.get("/user/feedback/{feedback_id}", response_class=HTMLResponse)
    def user_feedback_detail(
        feedback_id: int, request: Request, db: Session = Depends(get_db)
    ):
        """Render one customer's report without exposing other accounts."""

        user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        feedback = db.get(PortalFeedback, feedback_id)
        if feedback is None or feedback.user_id != user.id:
            _flash(request, "反馈记录不存在或无权查看", "error")
            return _redirect("/user/feedback")
        return templates.TemplateResponse(
            request=request,
            name="user_feedback_detail.html",
            context=_template_context(
                request,
                user,
                feedback=feedback,
                feedback_categories=FEEDBACK_CATEGORIES,
                feedback_statuses=FEEDBACK_STATUSES,
            ),
        )

    @app.post("/user/feedback")
    async def submit_user_feedback(
        request: Request,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
    ):
        """Persist feedback first, then send its notification asynchronously."""

        user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        try:
            category = str(form.get("category", "other")).strip().lower()
            if category not in FEEDBACK_CATEGORIES:
                raise PortalError("反馈分类无效")
            raw_group_id = str(form.get("group_id", "")).strip()
            group_id = validate_group_id(raw_group_id) if raw_group_id else None
            if group_id is not None and get_user_group(db, user.id, group_id) is None:
                raise PortalError("只能选择当前账号已登记的分群")
            subject = _clean_feedback_text(
                form.get("subject"), label="反馈主题", maximum=160, single_line=True
            )
            content = _clean_feedback_text(
                form.get("content"), label="反馈内容", maximum=8000
            )
            feedback = PortalFeedback(
                user_id=user.id,
                group_id=group_id,
                category=category,
                subject=subject,
                content=content,
            )
            db.add(feedback)
            db.flush()
            recipient = get_feedback_recipient(db)
            feedback.email_state = "queued" if recipient else "not_configured"
            add_audit_log(
                db,
                user,
                group_id,
                "feedback_submit",
                f"提交用户反馈 #{feedback.id}",
                request,
                {"feedback_id": feedback.id, "category": category},
            )
            db.commit()
            if recipient:
                background_tasks.add_task(
                    _deliver_feedback_notification, feedback.id, recipient, settings
                )
                _flash(request, "反馈已提交，管理员会收到邮件通知。", "success")
            else:
                _flash(
                    request,
                    "反馈已提交，管理员尚未配置通知邮箱；记录已保存在后台。",
                    "success",
                )
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect("/user/feedback")

    @app.post("/user/licenses/claim")
    async def claim_license(request: Request, db: Session = Depends(get_db)):
        user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        try:
            access = claim_activated_card(
                db,
                user,
                str(form.get("license_key", "")),
                settings,
            )
            group_id = access.group_id
            add_audit_log(
                db,
                user,
                group_id,
                "license_claim",
                "登记群聊已激活卡密",
                request,
            )
            db.commit()
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except IntegrityError:
            db.rollback()
            _flash(request, "该授权卡密或对应群已经绑定其他账号", "error")
        else:
            _flash(request, f"已登记群 {group_id} 的授权卡密", "success")
            # A successful claim is immediately actionable.  Taking the
            # customer straight to the group page makes the account/password
            # and group-card roles clear, and avoids looking like the card was
            # accepted but no editable configuration was opened.
            return _redirect(_group_path(user, group_id))
        return _redirect("/user")

    @app.post("/user/groups/{group_id}/game-license")
    async def verify_user_game_license(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        """Verify a user game card without exposing backend WebSocket secrets."""

        user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, False)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        try:
            normalized_group = validate_group_id(str(group_id))
            raw_game_card = re.sub(r"\s+", "", str(form.get("game_card", "")))
            try:
                card = normalize_game_card(raw_game_card)
            except ValueError:
                if re.fullmatch(r"[0-9a-fA-F]{32,128}", raw_game_card):
                    raise PortalError(
                        "这是小程序网站 WebSocket 卡密，不是 JG 游戏心跳卡。请在下方第二步验证网站卡密。"
                    ) from None
                raise
            fingerprint = game_card_fingerprint(card, settings.card_pepper)
            status = remote_store.get_game_authorization_status(db, normalized_group)
            if settings.data_mode == "remote":
                row = db.scalar(
                    select(GameAuthorization).where(
                        GameAuthorization.group_id == normalized_group
                    )
                )
                if row is None or not hmac.compare_digest(
                    str(row.card_fingerprint or ""), fingerprint
                ):
                    raise PortalError("游戏卡密与当前群不匹配，请联系主人核对群号。")
                if status.get("state") != "active":
                    raise PortalError("游戏卡密已过期，或 Windows 尚未上传有效心跳。")
                expire = int(status.get("expire", 0) or 0)
            else:
                from ..auth.game_manager import get_game_authorization_info

                info = get_game_authorization_info(normalized_group)
                stored_card = str(info.get("key", "") or "").strip().upper()
                if not stored_card or not hmac.compare_digest(
                    game_card_fingerprint(stored_card, settings.card_pepper),
                    fingerprint,
                ):
                    raise PortalError("游戏卡密与当前群不匹配，请联系主人核对群号。")
                if str(info.get("state", "missing")) != "active":
                    raise PortalError("游戏卡密已过期，请联系主人续期。")
                expire = int(info.get("expire", 0) or 0)

            verified = request.session.get("game_license_verified", {})
            if not isinstance(verified, dict):
                verified = {}
            verified[normalized_group] = {
                "fingerprint": fingerprint,
                "expire": expire,
                "verified_at": int(datetime.now(timezone.utc).timestamp()),
            }
            request.session["game_license_verified"] = verified
            add_audit_log(
                db,
                user,
                normalized_group,
                "game_license_verify",
                "验证用户游戏卡密",
                request,
                {"card_fingerprint": fingerprint, "state": "active"},
            )
            db.commit()
            link_message = ""
            if settings.data_mode == "remote":
                try:
                    executor_settings = get_game_websocket_settings(db, settings)
                    client_id = str(status.get("client_id", "") or "").strip()
                    if (
                        executor_settings.get("game_websocket_enabled")
                        and isinstance(executor_settings.get("game_executor"), dict)
                        and executor_settings["game_executor"].get("enabled")
                        and client_id
                    ):
                        result = await app.state.game_ws.call(
                            client_id,
                            "test_game_connection",
                            {"group_id": normalized_group},
                            timeout=app.state.game_ws.call_timeout,
                        )
                        if isinstance(result, dict) and result.get("success"):
                            link_message = " Windows 已确认插件 WebSocket /ping 连通。"
                        else:
                            detail = (
                                str(result.get("message", "未收到 Windows 确认"))[:120]
                                if isinstance(result, dict)
                                else "未收到 Windows 确认"
                            )
                            link_message = f" 但插件 WebSocket 尚未确认：{detail}。"
                    else:
                        link_message = " 等待 Windows 拉取后台链接后即可检测通联。"
                except GameWebSocketError as exc:
                    link_message = f" 但插件 WebSocket 尚未确认：{exc.detail[:120]}。"
                except Exception:
                    logger.warning(
                        "Unable to test verified game websocket group_id=%s",
                        normalized_group,
                        exc_info=True,
                    )
                    link_message = " 但插件 WebSocket 检测暂时不可用，请稍后重试。"
            _flash(
                request,
                "游戏卡密验证成功。后台 WebSocket 连接参数由管理员维护，Windows 端执行实际游戏操作。"
                + link_message,
                "success",
            )
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(_group_path(user, str(group_id), "#integration-status"))

    @app.post("/user/groups/{group_id}/game-websocket-test")
    async def test_user_game_websocket(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        """Let a verified customer recheck the backend-selected game WebSocket."""

        user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, False)
        if isinstance(access, RedirectResponse):
            return access
        try:
            normalized_group = validate_group_id(str(group_id))
            verified = request.session.get("game_license_verified", {})
            verified_item = verified.get(normalized_group) if isinstance(verified, dict) else None
            if not isinstance(verified_item, dict):
                raise PortalError("请先输入主人生成的 JG... 游戏卡密，验证成功后再检测 WebSocket。")
            if settings.data_mode != "remote":
                _flash(request, "当前网页为本地模式，游戏插件 WebSocket 由 Windows 本地直接检测。", "warning")
                return _redirect(_group_path(user, normalized_group, "#integration-status"))
            status = remote_store.get_game_authorization_status(db, normalized_group)
            if status.get("state") != "active":
                raise PortalError("游戏心跳已失效，请重新验证 JG... 游戏卡密或联系主人续期。")
            client_id = str(status.get("client_id", "") or "").strip()
            if not client_id:
                raise PortalError("Windows 尚未上传有效游戏心跳，请先启动 Windows 插件并等待同步。")
            result = await app.state.game_ws.call(
                client_id,
                "test_game_connection",
                {"group_id": normalized_group},
                timeout=app.state.game_ws.call_timeout,
            )
            if isinstance(result, dict) and result.get("success"):
                _flash(request, "用户端检测成功：Windows 已确认游戏插件 WebSocket /ping 连通。", "success")
            else:
                code = str(result.get("code", "game_connection_test_failed")) if isinstance(result, dict) else "game_connection_test_failed"
                message = str(result.get("message", "未收到 Windows 确认")) if isinstance(result, dict) else "未收到 Windows 确认"
                _flash(request, f"用户端检测失败（{code}）：{message[:180]}", "error")
        except GameWebSocketError as exc:
            _flash(request, f"用户端检测未完成：{exc.detail[:180]}", "error")
        except (ValueError, PortalError) as exc:
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to test user game websocket group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "用户端检测失败，请查看 Windows CMD 和网页同步状态。", "error")
        return _redirect(_group_path(user, str(group_id), "#integration-status"))

    @app.post("/user/groups/{group_id}/websocket-license")
    async def verify_user_websocket_license(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        """Verify the user's mini-program/WebSocket card against the linked site."""

        user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, False)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        try:
            normalized_group = validate_group_id(str(group_id))
            if settings.data_mode != "remote":
                raise PortalError("当前网页未启用远程同步，用户网站卡无法下发到 Windows。请先启用远程同步。")
            site = get_im3_socketio_settings(db, settings, include_secrets=False)
            if not bool(site.get("enabled")):
                raise PortalError("后台尚未启用小程序 WebSocket，请联系管理员。")
            websocket_key = re.sub(r"\s+", "", str(form.get("websocket_key", "")))
            if not websocket_key or len(websocket_key) > 512:
                raise PortalError("小程序 WebSocket 卡密不能为空且不能超过 512 个字符。")
            if any(ord(char) < 0x20 or ord(char) == 0x7F for char in websocket_key):
                raise PortalError("小程序 WebSocket 卡密包含无效字符。")
            result = await run_in_threadpool(
                test_im3_socketio_connection,
                {
                    **site,
                    "key": websocket_key,
                    "require_card_authorization": True,
                },
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                code = str(result.get("code", "websocket_card_rejected")) if isinstance(result, dict) else "websocket_card_rejected"
                message = str(result.get("message", "网站未确认 join_room")) if isinstance(result, dict) else "网站未确认 join_room"
                raise PortalError(f"小程序 WebSocket 卡密验证失败（{code}）：{message[:180]}")
            websocket_status = remote_store.save_group_websocket_access_key(
                db,
                normalized_group,
                websocket_key,
                settings,
            )
            verified = request.session.get("websocket_license_verified", {})
            if not isinstance(verified, dict):
                verified = {}
            verified[normalized_group] = {
                "verified_at": int(datetime.now(timezone.utc).timestamp()),
                "card_mask": websocket_status.get("card_mask", ""),
            }
            request.session["websocket_license_verified"] = verified
            add_audit_log(
                db,
                user,
                normalized_group,
                "websocket_license_verify",
                "连接用户小程序 WebSocket 卡密",
                request,
                {
                    "card_mask": websocket_status.get("card_mask", ""),
                    "site": str(site.get("server_url", ""))[:128],
                    "bot_type": str(site.get("bot_type", ""))[:64],
                },
            )
            db.commit()
            _flash(
                request,
                "小程序 WebSocket 卡密已由 im3 网站明确授权，已加密保存；Windows 下一次同步会使用该卡密连接网站。",
                "success",
            )
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to verify user websocket license group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "小程序 WebSocket 卡密验证失败，请检查后台网站配置和连接日志。", "error")
        return _redirect(_group_path(user, str(group_id), "#integration-status"))

    @app.post("/user/groups/{group_id}/websocket-license-test")
    async def test_user_websocket_license(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        """Recheck the encrypted user website card without asking for it again."""

        user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, False)
        if isinstance(access, RedirectResponse):
            return access
        try:
            normalized_group = validate_group_id(str(group_id))
            websocket_key = remote_store.get_group_websocket_access_key(
                db, normalized_group, settings
            )
            if not websocket_key:
                raise PortalError("本群还没有保存小程序 WebSocket 卡密，请先输入并验证。")
            if settings.data_mode != "remote":
                raise PortalError("当前网页未启用远程同步，用户网站卡无法下发到 Windows。请先启用远程同步。")
            site = get_im3_socketio_settings(db, settings, include_secrets=False)
            if not bool(site.get("enabled")):
                raise PortalError("后台尚未启用小程序 WebSocket，请联系管理员。")
            result = await run_in_threadpool(
                test_im3_socketio_connection,
                {
                    **site,
                    "key": websocket_key,
                    "require_card_authorization": True,
                },
            )
            if isinstance(result, dict) and result.get("success") is True:
                remote_store.mark_group_websocket_access_verified(
                    db, normalized_group
                )
                db.commit()
                _flash(request, "小程序 WebSocket 卡密重新检测成功，网站已明确返回授权回执。", "success")
            else:
                code = str(result.get("code", "websocket_card_rejected")) if isinstance(result, dict) else "websocket_card_rejected"
                message = str(result.get("message", "网站未确认 join_room")) if isinstance(result, dict) else "网站未确认 join_room"
                remote_store.mark_group_websocket_access_failed(db, normalized_group)
                db.commit()
                raise PortalError(f"小程序 WebSocket 重新检测失败（{code}）：{message[:180]}")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to retest user websocket license group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "小程序 WebSocket 重新检测失败，请查看后台和 Windows 连接日志。", "error")
        return _redirect(_group_path(user, str(group_id), "#integration-status"))

    @app.post("/user/account/password")
    @app.post("/admin/account/password")
    async def change_password(request: Request, db: Session = Depends(get_db)):
        # Keep the credential-management endpoints role-scoped just like the
        # surrounding page namespaces.  A customer session must not invoke an
        # admin action by changing the URL, and an administrator should use
        # the dedicated admin form rather than crossing into /user.
        user = (
            _require_admin(request, db)
            if request.url.path.startswith("/admin/")
            else _require_customer(request, db)
        )
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        current = str(form.get("current_password", ""))
        new_password = str(form.get("new_password", ""))
        try:
            if not verify_password(current, user.password_hash):
                raise PortalError("当前密码错误")
            validate_password(new_password)
            if new_password != str(form.get("new_password_confirm", "")):
                raise PortalError("两次输入的新密码不一致")
            user.password_hash = hash_password(new_password)
            add_audit_log(db, user, None, "password_change", "修改账号密码", request)
            db.commit()
            # A session authenticated with the old password must not remain
            # usable after a credential change.  Clear it only after the
            # database transaction succeeds so a failed save never signs the
            # user out unnecessarily.  ``_flash`` then starts a fresh session
            # solely to carry the one-time login notice.
            request.session.clear()
            _flash(request, "密码修改成功，请使用新密码重新登录", "success")
            return _redirect("/admin/login" if user.is_admin else "/user/login")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect("/admin" if user.is_admin else "/user")

    def _appearance_prefix(user: User) -> str:
        return "/admin/appearance" if user.is_admin else "/user/appearance"

    async def _store_theme_upload(form, field: str, user: User, db: Session):
        """Store one uploaded image without copying the original into RAM."""

        upload = form.get(field)
        if upload is None or not str(getattr(upload, "filename", "")).strip():
            return None
        source = getattr(upload, "file", None)
        if not callable(getattr(source, "read", None)) or not callable(
            getattr(source, "seek", None)
        ):
            raise PortalError("背景图片上传格式无效")
        try:
            # Starlette's UploadFile is a spooled temporary file. Passing its
            # underlying seekable stream into Pillow avoids the former
            # ``await upload.read()`` whole-file memory copy.
            prepared = await run_in_threadpool(
                theme_service.prepare_theme_asset_stream, source
            )
        finally:
            closer = getattr(upload, "close", None)
            if callable(closer):
                await closer()
        return theme_service.store_prepared_theme_asset(
            db,
            user,
            prepared,
            is_shared=str(form.get(f"{field}_shared", "")) == "on",
        )

    async def _store_reply_uploads(form, group_id: str, user: User, db: Session):
        """Validate and store selected automatic-reply images atomically.

        The returned entries contain only a database ID and a checksum.  The
        Windows bot later downloads the bytes through its authenticated sync
        channel; a temporary browser file path, image URL or base64 value is
        never written into the group configuration.
        """

        raw_uploads = list(form.getlist("images")) if hasattr(form, "getlist") else []
        uploads = [
            upload
            for upload in raw_uploads
            if str(getattr(upload, "filename", "") or "").strip()
        ]
        if len(uploads) > reply_asset_service.MAX_REPLY_IMAGES_PER_RULE:
            raise PortalError(
                f"每条自动回复最多上传 {reply_asset_service.MAX_REPLY_IMAGES_PER_RULE} 张图片"
            )
        if not uploads:
            return None
        prepared_assets = []
        total_size = 0
        for upload in uploads:
            source = getattr(upload, "file", None)
            if not callable(getattr(source, "read", None)) or not callable(
                getattr(source, "seek", None)
            ):
                raise PortalError("自动回复图片上传格式无效")
            try:
                prepared = await run_in_threadpool(
                    reply_asset_service.prepare_reply_asset_stream, source
                )
            except ValueError as exc:
                raise PortalError(str(exc)) from None
            finally:
                closer = getattr(upload, "close", None)
                if callable(closer):
                    await closer()
            total_size += len(prepared.image_data)
            if total_size > reply_asset_service.MAX_REPLY_ASSET_TOTAL_BYTES:
                raise PortalError("本条自动回复图片总请求超过服务器允许的大小")
            prepared_assets.append(prepared)

        return [
            reply_asset_service.asset_reference(
                reply_asset_service.store_prepared_reply_asset(
                    db, group_id, user.id, prepared
                )
            )
            for prepared in prepared_assets
        ]

    @app.get("/user/appearance", response_class=HTMLResponse)
    @app.get("/admin/appearance", response_class=HTMLResponse)
    def appearance_settings(request: Request, db: Session = Depends(get_db)):
        if request.url.path.startswith("/admin/"):
            user = _require_admin(request, db)
        else:
            user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        if user.is_admin and request.url.path.startswith("/user/"):
            return _redirect("/admin/appearance")
        return templates.TemplateResponse(
            request=request,
            name="appearance.html",
            context=_template_context(
                request,
                user,
                appearance_prefix=_appearance_prefix(user),
                preference=theme_service.preference_view(db, user),
                selectable_assets=theme_service.list_selectable_assets(db, user),
                owned_assets=theme_service.list_owned_assets(db, user),
                central_assets=(theme_service.list_central_assets(db) if user.is_admin else []),
            ),
        )

    @app.post("/user/appearance")
    @app.post("/admin/appearance")
    async def save_appearance_settings(request: Request, db: Session = Depends(get_db)):
        if request.url.path.startswith("/admin/"):
            user = _require_admin(request, db)
        else:
            user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        if user.is_admin and request.url.path.startswith("/user/"):
            return _redirect("/admin/appearance")
        form = await _validated_form(request)
        try:
            day_asset = await _store_theme_upload(form, "day_image", user, db)
            night_asset = await _store_theme_upload(form, "night_image", user, db)
            preference = theme_service.save_theme_preference(
                db,
                user,
                form.get("mode", "auto"),
                day_asset.id if day_asset is not None else form.get("day_asset_id"),
                night_asset.id if night_asset is not None else form.get("night_asset_id"),
            )
            add_audit_log(
                db,
                user,
                None,
                "portal_appearance_save",
                "更新网页昼夜背景与切换模式",
                request,
                {
                    "mode": preference.mode,
                    "day_asset_id": preference.day_asset_id,
                    "night_asset_id": preference.night_asset_id,
                    "uploaded_day": day_asset is not None,
                    "uploaded_night": night_asset is not None,
                },
            )
            db.commit()
            _flash(request, "网页外观已保存；背景仅保存在服务器数据库，不会同步给机器人", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(_appearance_prefix(user))

    @app.post("/user/appearance/assets/{asset_id}/sharing")
    @app.post("/admin/appearance/assets/{asset_id}/sharing")
    async def update_appearance_asset_sharing(
        asset_id: int, request: Request, db: Session = Depends(get_db)
    ):
        if request.url.path.startswith("/admin/"):
            user = _require_admin(request, db)
        else:
            user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        if user.is_admin and request.url.path.startswith("/user/"):
            return _redirect("/admin/appearance")
        form = await _validated_form(request)
        try:
            shared = str(form.get("is_shared", "")) == "on"
            asset = theme_service.set_asset_sharing(db, user, asset_id, shared)
            add_audit_log(
                db,
                user,
                None,
                "portal_appearance_share",
                f"{'允许' if shared else '停止允许'}其他用户使用背景图片",
                request,
                {"asset_id": asset.id, "is_shared": asset.is_shared},
            )
            db.commit()
            _flash(request, "背景图片共享设置已保存", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(_appearance_prefix(user))

    @app.post("/user/appearance/assets/{asset_id}/delete")
    @app.post("/admin/appearance/assets/{asset_id}/delete")
    async def delete_appearance_asset(
        asset_id: int, request: Request, db: Session = Depends(get_db)
    ):
        if request.url.path.startswith("/admin/"):
            user = _require_admin(request, db)
        else:
            user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        if user.is_admin and request.url.path.startswith("/user/"):
            return _redirect("/admin/appearance")
        await _validated_form(request)
        try:
            asset = theme_service.delete_theme_asset(db, user, asset_id)
            add_audit_log(
                db,
                user,
                None,
                "portal_appearance_delete",
                "删除网页背景图片",
                request,
                {"asset_id": asset.id, "owner_user_id": asset.owner_user_id},
            )
            db.commit()
            _flash(request, "背景图片已删除，引用它的昼夜背景已清空", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(_appearance_prefix(user))

    @app.get("/theme/assets/{asset_id}")
    def serve_theme_asset(
        asset_id: int, request: Request, db: Session = Depends(get_db)
    ):
        """Serve one private/shared raster only after authorization checks.

        Background assets are immutable after creation, so an ETag lets the
        browser keep the compact WebP locally and receive a zero-body 304 on a
        revisit. ``no-cache`` means authorization is still rechecked before a
        cached image can be reused; ``Vary: Cookie`` prevents a browser cache
        entry from being reused across portal sessions.
        """

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        # Keep the large blob deferred unless the browser actually needs a
        # fresh body. A conditional request normally needs only metadata.
        asset = theme_service.get_accessible_asset(db, user, asset_id, load_data=False)
        if asset is None:
            return Response(status_code=404)
        etag = f'"{asset.sha256}"'
        headers = {
            "Cache-Control": "private, no-cache",
            "ETag": etag,
            "Vary": "Cookie",
        }
        requested_etags = {
            item.strip().removeprefix("W/")
            for item in request.headers.get("if-none-match", "").split(",")
            if item.strip()
        }
        if "*" in requested_etags or etag in requested_etags:
            return Response(status_code=304, headers=headers)
        return Response(
            content=asset.image_data,
            media_type=asset.content_type,
            headers=headers,
        )

    @app.get("/admin", response_class=HTMLResponse)
    def admin_dashboard(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        users = list(db.scalars(select(User).order_by(User.created_at.desc())))
        groups = list_admin_groups(db)
        console_settings = get_console_settings(db)
        stats = {
            "users": len(users),
            "active_users": sum(1 for item in users if item.is_active),
            "admins": sum(1 for item in users if item.is_admin),
            "groups": len(groups),
            "active_auth": sum(1 for item in groups if item["auth"]["active"]),
        }
        return templates.TemplateResponse(
            request=request,
            name="admin.html",
            context=_template_context(
                request,
                user,
                users=users,
                groups=groups,
                stats=stats,
                console_settings=console_settings,
                default_console_keywords=DEFAULT_CONSOLE_KEYWORDS,
                runner=get_runner_deployment(db),
            ),
        )

    @app.get("/admin/global-blacklist", response_class=HTMLResponse)
    def admin_global_blacklist(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        return templates.TemplateResponse(
            request=request,
            name="admin_global_blacklist.html",
            context=_template_context(
                request,
                user,
                blacklist=_global_blacklist_admin_view(db),
            ),
        )

    @app.post("/admin/global-blacklist")
    async def admin_global_blacklist_action(
        request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        action = str(form.get("action", "")).strip().lower()
        target_group_id = str(form.get("target_group_id", "")).strip()
        try:
            user_id = str(form.get("user_id", "")).strip()
            if not user_id.isdigit() or not 5 <= len(user_id) <= 20:
                raise PortalError("QQ号格式不正确，请填写 5-20 位数字")
            if action not in {"add_global_blacklist", "remove_global_blacklist"}:
                raise PortalError("未知全局黑名单操作")
            if settings.data_mode == "remote":
                target_group_id = validate_group_id(target_group_id)
                if target_group_id not in known_group_ids(db):
                    raise PortalError("请选择已同步的执行群")
                access = _owned_group_or_redirect(request, db, user, target_group_id, True)
                if isinstance(access, RedirectResponse):
                    return access

            if action == "add_global_blacklist":
                reason = _clean_feedback_text(
                    form.get("reason", ""),
                    label="拉黑原因",
                    maximum=240,
                    required=False,
                    single_line=True,
                )
                ban_type = str(form.get("ban_type", "permanent") or "permanent").strip().lower()
                if ban_type not in {"permanent", "temporary"}:
                    raise PortalError("全局黑名单类型无效")
                try:
                    duration = max(0, min(2592000, int(form.get("duration", 0) or 0)))
                except (TypeError, ValueError, OverflowError):
                    raise PortalError("临时黑名单时长无效")
                if ban_type == "temporary" and duration <= 0:
                    raise PortalError("临时全局黑名单必须设置有效时长")
            else:
                reason = ""
                ban_type = "permanent"
                duration = 0

            if settings.data_mode == "remote":
                request_id = uuid4().hex
                item = {
                    "action": action,
                    "request_id": request_id,
                    "created_at": int(unix_time()),
                    "user_id": user_id,
                }
                if action == "add_global_blacklist":
                    item.update(
                        {
                            "reason": reason,
                            "ban_type": ban_type,
                            "duration": duration if ban_type == "temporary" else 0,
                        }
                    )

                def mutate(config):
                    actions = config.get("portal_actions", [])
                    actions = list(actions) if isinstance(actions, list) else []
                    actions = [
                        entry
                        for entry in actions
                        if isinstance(entry, dict)
                        and str(entry.get("request_id", "")) != request_id
                    ]
                    actions.append(item)
                    config["portal_actions"] = actions[-32:]
                    return config

                remote_store.mutate_web_config(db, target_group_id, user.id, mutate)
                _snapshot_and_log(
                    request,
                    db,
                    user,
                    target_group_id,
                    "global_blacklist_action",
                    f"提交全局黑名单操作 {action}",
                    {
                        "action": action,
                        "user_id": user_id,
                        "ban_type": ban_type,
                        "duration": duration,
                        "request_id": request_id,
                    },
                )
                _flash(request, "操作已提交，Windows 在线后会执行并刷新全局黑名单。", "success")
            else:
                from ..qqadmin.blacklist import (
                    add_to_global_blacklist,
                    remove_from_global_blacklist,
                )

                if action == "add_global_blacklist":
                    if not add_to_global_blacklist(
                        user_id,
                        reason=reason,
                        operator_id=f"web:{user.id}",
                        ban_type=ban_type,
                        duration=duration if ban_type == "temporary" else 0,
                    ):
                        raise PortalError("全局黑名单保存失败")
                elif not remove_from_global_blacklist(
                    user_id,
                    unban_reason="网页解除全局黑名单",
                    unbanned_by=f"web:{user.id}",
                ):
                    raise PortalError("该用户不在全局黑名单中")
                add_audit_log(
                    db,
                    user,
                    target_group_id or None,
                    "global_blacklist_action",
                    f"网页执行全局黑名单操作 {action}",
                    request,
                    {"action": action, "user_id": user_id, "ban_type": ban_type, "duration": duration},
                )
                db.commit()
                _flash(request, "全局黑名单已更新。", "success")
        except (PortalError, ValueError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to update global blacklist from admin page request_id=%s",
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "全局黑名单操作失败，请检查同步状态和 Windows 日志。", "error")
        return _redirect("/admin/global-blacklist")

    @app.get("/admin/users", response_class=HTMLResponse)
    def admin_users(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        users = list(db.scalars(select(User).order_by(User.created_at.desc())))
        return templates.TemplateResponse(
            request=request,
            name="admin_users.html",
            context=_template_context(request, user, users=users),
        )

    @app.get("/admin/groups", response_class=HTMLResponse)
    def admin_groups(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        group_search = (
            str(request.query_params.get("group_search", ""))
            .replace("\r", " ")
            .replace("\n", " ")
            .strip()[:64]
        )
        all_groups = list_admin_groups(db)
        filtered_groups = all_groups
        if group_search:
            needle = group_search.casefold()

            def matches(item):
                owner = item.get("owner")
                access = item.get("access")
                haystack = " ".join(
                    (
                        str(item.get("group_id", "")),
                        str(getattr(owner, "username", "")),
                        str(getattr(access, "card_mask", "")),
                    )
                )
                return needle in haystack.casefold()

            filtered_groups = [item for item in all_groups if matches(item)]
        group_pagination = _pagination_context(
            request,
            len(filtered_groups),
            page_param="group_page",
            page_size=30,
        )
        group_offset = (
            group_pagination["page"] - 1
        ) * group_pagination["page_size"]
        return templates.TemplateResponse(
            request=request,
            name="admin_groups.html",
            context=_template_context(
                request,
                user,
                groups=filtered_groups[
                    group_offset : group_offset + group_pagination["page_size"]
                ],
                group_search=group_search,
                group_total=len(filtered_groups),
                group_pagination=group_pagination,
            ),
        )

    @app.get("/admin/logs", response_class=HTMLResponse)
    def admin_logs(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        log_total = int(db.scalar(select(func.count()).select_from(AuditLog)) or 0)
        log_pagination = _pagination_context(
            request, log_total, page_param="log_page", page_size=50
        )
        log_offset = (log_pagination["page"] - 1) * log_pagination["page_size"]
        logs = list(
            db.scalars(
                select(AuditLog)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .offset(log_offset)
                .limit(log_pagination["page_size"])
            )
        )
        return templates.TemplateResponse(
            request=request,
            name="admin_logs.html",
            context=_template_context(
                request,
                user,
                logs=logs,
                log_total=log_total,
                log_pagination=log_pagination,
            ),
        )

    @app.get("/admin/feedback", response_class=HTMLResponse)
    def admin_feedback(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        requested_status = str(request.query_params.get("status", "")).strip().lower()
        query = select(PortalFeedback).order_by(
            PortalFeedback.created_at.desc(), PortalFeedback.id.desc()
        )
        count_query = select(func.count()).select_from(PortalFeedback)
        if requested_status in FEEDBACK_STATUSES:
            query = query.where(PortalFeedback.status == requested_status)
            count_query = count_query.where(PortalFeedback.status == requested_status)
        feedback_total = int(db.scalar(count_query) or 0)
        feedback_pagination = _pagination_context(request, feedback_total, page_size=25)
        feedback_offset = (
            feedback_pagination["page"] - 1
        ) * feedback_pagination["page_size"]
        feedback_items = list(
            db.scalars(
                query.offset(feedback_offset).limit(feedback_pagination["page_size"])
            )
        )
        user_ids = {
            item.user_id for item in feedback_items if item.user_id is not None
        } | {
            item.handled_by for item in feedback_items if item.handled_by is not None
        }
        users_by_id = {}
        if user_ids:
            users_by_id = {
                item.id: item
                for item in db.scalars(select(User).where(User.id.in_(user_ids)))
            }
        return templates.TemplateResponse(
            request=request,
            name="admin_feedback.html",
            context=_template_context(
                request,
                user,
                feedback_items=feedback_items,
                users_by_id=users_by_id,
                feedback_categories=FEEDBACK_CATEGORIES,
                feedback_statuses=FEEDBACK_STATUSES,
                selected_status=requested_status,
                feedback_total=feedback_total,
                feedback_pagination=feedback_pagination,
            ),
        )

    @app.get("/admin/feedback/{feedback_id}", response_class=HTMLResponse)
    def admin_feedback_detail(
        feedback_id: int, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        feedback = db.get(PortalFeedback, feedback_id)
        if feedback is None:
            _flash(request, "反馈记录不存在或已删除", "error")
            return _redirect("/admin/feedback")
        reporter = db.get(User, feedback.user_id)
        handler = db.get(User, feedback.handled_by) if feedback.handled_by else None
        return templates.TemplateResponse(
            request=request,
            name="admin_feedback_detail.html",
            context=_template_context(
                request,
                user,
                feedback=feedback,
                reporter=reporter,
                handler=handler,
                feedback_categories=FEEDBACK_CATEGORIES,
                feedback_statuses=FEEDBACK_STATUSES,
            ),
        )

    @app.post("/admin/feedback/{feedback_id}")
    async def admin_update_feedback(
        feedback_id: int, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        feedback = db.get(PortalFeedback, feedback_id)
        if feedback is None:
            _flash(request, "反馈记录不存在或已删除", "error")
            return _redirect("/admin/feedback")
        try:
            status = str(form.get("status", "new")).strip().lower()
            if status not in FEEDBACK_STATUSES:
                raise PortalError("反馈状态无效")
            admin_reply = _clean_feedback_text(
                form.get("admin_reply"),
                label="处理意见",
                maximum=8000,
                required=False,
            )
            feedback.status = status
            feedback.admin_reply = admin_reply
            if status == "new":
                feedback.handled_by = None
                feedback.handled_at = None
            else:
                feedback.handled_by = user.id
                feedback.handled_at = utcnow()
            add_audit_log(
                db,
                user,
                feedback.group_id,
                "feedback_update",
                f"更新用户反馈 #{feedback.id} 状态",
                request,
                {"feedback_id": feedback.id, "status": status},
            )
            db.commit()
            _flash(request, f"反馈 #{feedback.id} 已更新", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect("/admin/feedback")

    @app.get("/admin/deployment", response_class=HTMLResponse)
    def admin_deployment(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        return templates.TemplateResponse(
            request=request,
            name="admin_deployment.html",
            context=_template_context(
                request,
                user,
                runner=get_runner_deployment(db),
                execution_control=get_execution_control(db),
                execution_clients=list_execution_clients(db),
            ),
        )

    @app.post("/admin/deployment")
    async def admin_save_deployment(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        action = str(form.get("action", "deployment")).strip()
        if action == "save_execution_control":
            try:
                control = save_execution_control(
                    db,
                    owner_ids=form.get("owner_ids", ""),
                    enforce_owner_ids=str(form.get("enforce_owner_ids", "")) == "on",
                    allow_new_clients=str(form.get("allow_new_clients", "")) == "on",
                    allowed_client_ids=form.get("allowed_client_ids", ""),
                    blocked_client_ids=form.get("blocked_client_ids", ""),
                    operator_id=user.id,
                )
                add_audit_log(
                    db,
                    user,
                    None,
                    "execution_control_update",
                    "更新中心主人与执行端控制策略",
                    request,
                    {"enforce_owner_ids": control["enforce_owner_ids"]},
                )
                db.commit()
                _flash(request, "中心控制策略已保存；执行端将在下一次同步时生效。", "success")
            except (ValueError, PortalError) as exc:
                db.rollback()
                _flash(request, str(exc), "error")
            return _redirect("/admin/deployment")

        if action == "set_client_access":
            client_id = str(form.get("client_id", "")).strip()[:64]
            if not client_id:
                _flash(request, "执行端编号不能为空", "error")
                return _redirect("/admin/deployment")
            control = get_execution_control(db)
            blocked = set(control["blocked_client_ids"])
            if str(form.get("client_action", "")) == "block":
                blocked.add(client_id)
                message = "已停用该执行端"
            else:
                blocked.discard(client_id)
                message = "已恢复该执行端"
            try:
                save_execution_control(
                    db,
                    owner_ids=control["owner_ids"],
                    enforce_owner_ids=control["enforce_owner_ids"],
                    allow_new_clients=control["allow_new_clients"],
                    allowed_client_ids=control["allowed_client_ids"],
                    blocked_client_ids=sorted(blocked),
                    operator_id=user.id,
                )
                add_audit_log(
                    db,
                    user,
                    None,
                    "execution_client_access_update",
                    message,
                    request,
                    {"client_id": client_id},
                )
                db.commit()
                _flash(request, f"{message}：{client_id}", "success")
            except (ValueError, PortalError) as exc:
                db.rollback()
                _flash(request, str(exc), "error")
            return _redirect("/admin/deployment")

        prepared = str(form.get("linux_migration_prepared", "")) == "on"
        runner = set_linux_migration_prepared(db, prepared, user.id)
        add_audit_log(
            db,
            user,
            None,
            "runner_migration_prepared",
            "已预留 Linux 执行端迁移" if prepared else "已取消 Linux 执行端迁移预留",
            request,
            {"linux_migration_prepared": prepared, "active_runner": runner["active_runner"]},
        )
        db.commit()
        _flash(
            request,
            "Linux 迁移接口已预留，当前机器人仍只在 Windows 上执行。"
            if prepared
            else "已取消 Linux 迁移预留；当前仍只在 Windows 上执行。",
            "success",
        )
        return _redirect("/admin/deployment")

    @app.get("/admin/websocket", response_class=HTMLResponse)
    async def admin_websocket_settings(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        # The lifecycle loader stores the latest portal-only configuration on
        # app.state.  The old route referenced the lifecycle function's local
        # variable, so opening the page raised NameError and rendered no form.
        current_websocket_settings = getattr(
            app.state,
            "game_websocket_settings",
            {},
        )
        im3_settings = get_im3_socketio_settings(db, settings, include_secrets=False)
        return templates.TemplateResponse(
            request=request,
            name="admin_websocket.html",
            context=_template_context(
                request,
                user,
                websocket_settings=current_websocket_settings,
                websocket_endpoint=_websocket_endpoint_url(
                    request,
                    current_websocket_settings,
                ),
                status={"connections": await app.state.game_ws.status()},
                im3_settings=im3_settings,
            ),
        )

    @app.post("/admin/websocket")
    async def admin_save_websocket_settings(
        request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        operation = str(form.get("operation", "") or "").strip().lower()
        current_websocket_settings = getattr(
            app.state, "game_websocket_settings", {}
        )
        # The public origin belongs to the reverse proxy request, not an old
        # form field. Re-derive it on every save so a stale internal address
        # cannot keep producing Windows handshake failures after a domain or
        # CDN change.
        automatic_portal_url = _public_request_origin(request)
        # The internal route is intentionally not user-configurable. Keeping
        # one path removes a class of Windows/web mismatches after upgrades.
        automatic_endpoint_path = "/api/sync/v1/ws"
        if operation == "test_im3":
            try:
                # The test button is part of the same form. Persist the values
                # first so a freshly entered website/path/type is what gets
                # tested instead of the previous database row.
                im3_settings = save_im3_socketio_settings(
                    db,
                    enabled=_form_checkbox(form, "im3_enabled"),
                    server_url=form.get("im3_server_url", "https://im3.vipscum.cn"),
                    socketio_path=form.get("im3_socketio_path", "/socket.io"),
                    bot_type=form.get("im3_bot_type", "bot_main"),
                    settings=settings,
                    operator_id=user.id,
                )
                db.commit()
                app.state.im3_socketio_settings = im3_settings
                result = await run_in_threadpool(
                    test_im3_socketio_connection,
                    {**im3_settings, "probe_only": True},
                )
                prefix = "im3 配置已保存；"
                _flash(request, prefix + str(result.get("message", "im3 检测完成")), "success" if result.get("success") else "error")
            except (ValueError, PortalError) as exc:
                db.rollback()
                _flash(request, str(exc), "error")
            return _redirect("/admin/websocket")
        if operation in {"test_portal", "test_executor"}:
            try:
                websocket_settings = save_game_websocket_settings(
                    db,
                    enabled=_form_checkbox(form, "enabled"),
                    game_websocket_enabled=_form_checkbox(form, "game_websocket_enabled"),
                    max_clients=form.get("max_clients"),
                    call_timeout=form.get("call_timeout"),
                    portal_url=automatic_portal_url,
                    endpoint_path=automatic_endpoint_path,
                    game_websocket_token=None,
                    game_executor_enabled=_form_checkbox(form, "game_executor_enabled"),
                    game_executor_timeout_seconds=form.get("game_executor_timeout_seconds"),
                    game_executor_websocket_url=form.get("game_executor_websocket_url"),
                    settings=settings,
                    operator_id=user.id,
                    auto_generate_game_websocket_token=True,
                )
                db.commit()
                app.state.game_websocket_settings = websocket_settings
                if operation == "test_portal":
                    websocket_result = await probe_game_websocket_connection(
                        websocket_settings,
                        timeout_seconds=websocket_settings.get("call_timeout", 20.0),
                    )
                else:
                    websocket_result = await _test_admin_game_executor(
                        app, websocket_settings
                    )
                _flash(
                    request,
                    (
                        "网页游戏入口已保存；"
                        if operation == "test_portal"
                        else "实际游戏插件检测："
                    )
                    + str(websocket_result.get("message", "检测完成")),
                    "success" if websocket_result.get("success") else "error",
                )
            except (ValueError, PortalError) as exc:
                db.rollback()
                _flash(request, str(exc), "error")
            except Exception:
                db.rollback()
                logger.exception("后台 WebSocket 检测失败 operation=%s", operation)
                _flash(request, "WebSocket 检测失败，请查看网页服务日志。", "error")
            return _redirect("/admin/websocket")
        try:
            websocket_settings = save_game_websocket_settings(
                db,
                enabled=_form_checkbox(form, "enabled"),
                game_websocket_enabled=_form_checkbox(form, "game_websocket_enabled"),
                max_clients=form.get("max_clients"),
                call_timeout=form.get("call_timeout"),
                portal_url=automatic_portal_url,
                endpoint_path=automatic_endpoint_path,
                game_websocket_token=None,
                game_executor_enabled=_form_checkbox(form, "game_executor_enabled"),
                game_executor_timeout_seconds=(
                    form.get("game_executor_timeout_seconds")
                    if "game_executor_timeout_seconds" in form
                    else None
                ),
                game_executor_websocket_url=(
                    form.get("game_executor_websocket_url")
                    if "game_executor_websocket_url" in form
                    else None
                ),
                settings=settings,
                operator_id=user.id,
                auto_generate_game_websocket_token=True,
            )
            app.state.game_websocket_settings = websocket_settings
            app.state.game_ws.max_clients = websocket_settings["max_clients"]
            app.state.game_ws.call_timeout = websocket_settings["call_timeout"]
            im3_settings = save_im3_socketio_settings(
                db,
                enabled=_form_checkbox(form, "im3_enabled"),
                server_url=form.get("im3_server_url", "https://im3.vipscum.cn"),
                socketio_path=form.get("im3_socketio_path", "/socket.io"),
                bot_type=form.get("im3_bot_type", "bot_main"),
                settings=settings,
                operator_id=user.id,
            )
            app.state.im3_socketio_settings = im3_settings
            audit_executor = dict(websocket_settings.get("game_executor", {}))
            audit_executor.pop("api_key", None)
            add_audit_log(
                db,
                user,
                None,
                "admin_websocket_settings",
                "更新游戏 WebSocket 设置",
                request,
                {
                    **{
                        key: value
                        for key, value in websocket_settings.items()
                        if key
                        not in {
                            "sync_token",
                            "token_configured",
                            "game_websocket_token",
                            "game_websocket_token_configured",
                            "portal_sync_token_configured",
                            "game_executor",
                            "im3_socketio",
                        }
                    },
                    "game_executor": audit_executor,
                    "im3_socketio": {
                        "enabled": im3_settings.get("enabled", False),
                        "server_url": im3_settings.get("server_url", ""),
                        "bot_type": im3_settings.get("bot_type", "bot_main"),
                    },
                },
            )
            db.commit()
            _flash(request, "游戏 WebSocket 设置已保存", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect("/admin/websocket")

    @app.get("/admin/music-sources", response_class=HTMLResponse)
    def admin_music_sources(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        return templates.TemplateResponse(
            request=request,
            name="admin_music_sources.html",
            context=_template_context(
                request,
                user,
                music_sources=get_music_source_services(
                    db, settings, include_secrets=False
                ),
            ),
        )

    @app.post("/admin/music-sources")
    async def admin_save_music_sources(
        request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        operation = str(form.get("operation", "") or "").strip().lower()
        if operation == "apply_defaults":
            try:
                save_music_source_services(
                    db,
                    soda_enabled=False,
                    soda_base_url="",
                    signer_enabled=True,
                    signer_url=MUSIC_CARD_DEFAULT_SIGN_URL,
                    platform_cookies={},
                    signer_cookie="",
                    settings=settings,
                    operator_id=user.id,
                )
                add_audit_log(
                    db,
                    user,
                    None,
                    "admin_music_sources",
                    "应用音乐源默认配置",
                    request,
                    {"signer_default_url": MUSIC_CARD_DEFAULT_SIGN_URL},
                )
                db.commit()
                _flash(
                    request,
                    "已应用默认音乐名片配置，Cookie 保持不变；Windows 将在下一次心跳获取。",
                    "success",
                )
            except (ValueError, PortalError) as exc:
                db.rollback()
                _flash(request, str(exc), "error")
            return _redirect("/admin/music-sources")
        if operation == "test_soda":
            try:
                current = get_music_source_services(db, settings, include_secrets=True)
                result = await run_in_threadpool(test_soda_music_source, current)
                _flash(
                    request,
                    str(result.get("message", "汽水音乐服务检测完成")),
                    "success" if result.get("success") else "error",
                )
            except ValueError as exc:
                _flash(request, str(exc), "error")
            return _redirect("/admin/music-sources")
        if operation == "test_signer":
            try:
                current = get_music_source_services(db, settings, include_secrets=True)
                result = await run_in_threadpool(test_music_card_signer, current)
                _flash(
                    request,
                    str(result.get("message", "音乐名片签名服务检测完成")),
                    "success" if result.get("success") else "error",
                )
            except ValueError as exc:
                _flash(request, str(exc), "error")
            return _redirect("/admin/music-sources")
        try:
            sources = save_music_source_services(
                db,
                soda_enabled=_form_checkbox(form, "soda_enabled"),
                soda_base_url=form.get("soda_base_url", ""),
                soda_api_key=form.get("soda_api_key", ""),
                signer_enabled=_form_checkbox(form, "signer_enabled"),
                signer_url=form.get("signer_url", ""),
                signer_api_key=form.get("signer_api_key", ""),
                platform_cookies={
                    provider: form.get(f"cookie_{provider}", "")
                    for provider in ("qq", "163", "kugou", "soda")
                },
                signer_cookie=form.get("signer_cookie", ""),
                settings=settings,
                operator_id=user.id,
            )
            audit_sources = get_music_source_services(
                db, settings, include_secrets=False
            )
            add_audit_log(
                db,
                user,
                None,
                "admin_music_sources",
                "更新音乐源和音乐名片签名设置",
                request,
                audit_sources,
            )
            db.commit()
            _flash(request, "音乐源服务已保存，Windows 将在下一次心跳获取配置。", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect("/admin/music-sources")

    @app.get("/admin/security", response_class=HTMLResponse)
    def admin_security(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        max_age = int(
            getattr(request.app.state, "session_max_age", settings.session_max_age)
        )
        return templates.TemplateResponse(
            request=request,
            name="admin_security.html",
            context=_template_context(
                request,
                user,
                session_max_age=max_age,
                session_max_age_text=format_session_max_age(max_age),
            ),
        )

    @app.post("/admin/security")
    async def admin_save_security(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        try:
            raw_max_age = str(form.get("session_max_age", "")).strip()
            if not raw_max_age.isdigit():
                raise PortalError("Cookie 有效期必须是整数秒数")
            value = save_session_max_age(db, raw_max_age, user.id)
            add_audit_log(
                db,
                user,
                None,
                "admin_security_settings",
                "更新浏览器 Cookie 有效期",
                request,
                {"session_max_age": value},
            )
            db.commit()
            request.app.state.session_max_age = value
            _flash(
                request,
                f"Cookie 有效期已保存为 {format_session_max_age(value)}",
                "success",
            )
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect("/admin/security")

    @app.get("/admin/update", response_class=HTMLResponse)
    def admin_update(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        return templates.TemplateResponse(
            request=request,
            name="admin_update.html",
            context=_template_context(
                request,
                user,
                update_status=_update_status_with_installed_release(),
                update_root=get_update_root(),
                deployment_history=get_update_history(),
            ),
        )

    @app.get("/admin/update-log", response_class=HTMLResponse)
    def admin_update_log(request: Request, db: Session = Depends(get_db)):
        """Render shipped release notes for authenticated administrators."""

        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        return templates.TemplateResponse(
            request=request,
            name="admin_update_log.html",
            context=_template_context(
                request,
                user,
                releases=get_update_log(),
                deployment_history=get_update_history(),
                installed_release=get_installed_release_info(),
            ),
        )

    @app.get("/admin/update/status")
    def admin_update_status(request: Request, db: Session = Depends(get_db)):
        """Return root-worker update state to the administrator only.

        The update worker can restart this application while an administrator
        is viewing the page.  A small no-store JSON endpoint lets the browser
        poll and recover after that restart without exposing task state to
        customers or requiring a full HTML navigation during the handoff.
        """

        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        response = JSONResponse(_update_status_with_installed_release())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/admin/update")
    async def admin_upload_update(
        request: Request,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        staged: dict[str, object] | None = None
        queue_scheduled = False
        queued_status: dict[str, str] | None = None
        try:
            upload = form.get("update_package")
            if upload is None or not hasattr(upload, "read"):
                raise UpdateError("请选择要上传的 Joker 网页部署 ZIP 包")
            staged = await stage_update_package(
                upload,
                str(form.get("update_authorization", "")),
            )
            add_audit_log(
                db,
                user,
                None,
                "portal_update_staged",
                "通过后台上传网页更新包，等待服务器验证和排队",
                request,
                {
                    "filename": staged["filename"],
                    "size": staged["size"],
                    "release_version": staged["release_version"],
                },
            )
            db.commit()
            # The root watcher may restart this service after it receives the
            # request file. Add that final publication only after FastAPI has
            # sent the accepted response to Nginx.
            background_tasks.add_task(
                _queue_staged_update_after_response, str(staged["update_id"]), user.id
            )
            queue_scheduled = True
            queued_status = {
                "state": "queued",
                "message": "更新包已接收，正在等待独立服务器任务校验和安装。",
                "updated_at": "",
                "package": str(staged["filename"]),
            }
            _flash(
                request,
                "更新包已接收，服务器会在本次页面响应完成后验证、安装并重启网页服务。",
                "success",
            )
        except UpdateError as exc:
            db.rollback()
            if staged is not None and not queue_scheduled:
                discard_staged_update(str(staged["update_id"]))
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            if staged is not None and not queue_scheduled:
                discard_staged_update(str(staged["update_id"]))
            raise
        if queue_scheduled and queued_status is not None:
            # Do not issue a 303 here. A browser immediately follows a 303
            # with GET /admin/update, which can race the updater's service
            # restart and turn a successful upload into a misleading timeout.
            # The 202 page is fully sent before the watcher may begin work.
            return templates.TemplateResponse(
                request=request,
                name="admin_update.html",
                context=_template_context(
                    request,
                    user,
                    update_status={
                        **queued_status,
                        "installed_release": get_installed_release_info(),
                    },
                    update_root=get_update_root(),
                    update_queued=True,
                ),
                status_code=202,
                headers={"Cache-Control": "no-store"},
            )
        return _redirect("/admin/update")

    @app.get("/admin/users/{target_user_id}", response_class=HTMLResponse)
    def admin_user_detail(
        target_user_id: int, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        target = db.get(User, target_user_id)
        if target is None:
            _flash(request, "用户不存在", "error")
            return _redirect("/admin/users")
        groups = list_user_groups(db, target.id)
        logs = list(
            db.scalars(
                select(AuditLog)
                .where(AuditLog.user_id == target.id)
                .order_by(AuditLog.created_at.desc())
                .limit(100)
            )
        )
        return templates.TemplateResponse(
            request=request,
            name="admin_user.html",
            context=_template_context(
                request,
                user,
                target_user=target,
                groups=groups,
                logs=logs,
            ),
        )

    @app.post("/admin/users/{target_user_id}/console-access")
    async def admin_update_console_access(
        target_user_id: int, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        target = db.get(User, target_user_id)
        if target is None:
            _flash(request, "用户不存在", "error")
            return _redirect("/admin/users")
        enabled = str(form.get("enabled", "")) == "true"
        target.can_view_console = enabled
        add_audit_log(
            db,
            user,
            None,
            "admin_console_access",
            f"{'允许' if enabled else '禁止'}用户 {target.username} 查看运行日志",
            request,
            {"target_user_id": target.id, "enabled": enabled},
        )
        db.commit()
        _flash(request, "用户运行日志权限已更新", "success")
        return _redirect(f"/admin/users/{target_user_id}")

    @app.post("/admin/console-settings")
    async def admin_save_console_settings(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        try:
            console_settings = save_console_settings(
                db,
                enabled=str(form.get("enabled", "")) == "on",
                messages_enabled=str(form.get("messages_enabled", "")) == "on",
                retention=form.get("retention"),
                keywords=form.get("keywords"),
                operator_id=user.id,
            )
            add_audit_log(
                db,
                user,
                None,
                "admin_console_settings",
                "更新运行日志显示设置",
                request,
                {
                    "enabled": console_settings["enabled"],
                    "messages_enabled": console_settings["messages_enabled"],
                    "retention": console_settings["retention"],
                    "keyword_count": len(console_settings["keywords"]),
                },
            )
            db.commit()
            _flash(request, "运行日志显示设置已保存", "success")
        except ValueError as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect("/admin#console")

    @app.get("/admin/licenses", response_class=HTMLResponse)
    def admin_licenses(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        overview = get_admin_license_overview(db)
        return templates.TemplateResponse(
            request=request,
            name="admin_licenses.html",
            context=_template_context(request, user, **overview),
        )

    @app.get("/admin/email", response_class=HTMLResponse)
    def admin_email_settings(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        return templates.TemplateResponse(
            request=request,
            name="admin_email.html",
            context=_template_context(
                request,
                user,
                email_config=get_email_config_view(db),
            ),
        )

    @app.post("/admin/email")
    async def admin_save_email_settings(
        request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        try:
            config = save_email_config(db, settings, form, user.id)
            add_audit_log(
                db,
                user,
                None,
                "admin_email_config",
                "更新 SMTP 邮箱验证配置",
                request,
                {
                    "enabled": config["enabled"],
                    "host": config["host"],
                    "port": config["port"],
                    "transport": config["transport"],
                    "sender_email": config["sender_email"],
                },
            )
            db.commit()
            _flash(request, "邮箱配置已保存；SMTP 密码不会显示或写入操作日志", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect("/admin/email")

    @app.post("/admin/email/test")
    async def admin_test_email(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        try:
            recipient = str(form.get("test_email", ""))
            send_test_email(db, settings, recipient)
            add_audit_log(
                db,
                user,
                None,
                "admin_email_test",
                "发送 SMTP 测试邮件",
                request,
                {"recipient": recipient[:320]},
            )
            db.commit()
            _flash(request, "测试邮件已发送，请检查收件箱和垃圾邮箱", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect("/admin/email")

    @app.get("/admin/integrations", response_class=HTMLResponse)
    def admin_integrations(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        # FTP and the business database are customer-owned integrations.  The
        # old public setting remains readable by migration code, but must not
        # be edited or tested from the administrator area because it is not
        # used by any customer's business workflow.
        _flash(request, "FTP 与业务数据库由每个分群在“服务器与数据库”中单独配置。", "info")
        return _redirect("/admin/users")

    @app.post("/admin/integrations")
    async def admin_save_integrations(
        request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        _flash(request, "请进入对应分群的“服务器与数据库”配置 FTP 与业务数据库，管理员不保存公共连接。", "info")
        return _redirect("/admin/users")

    @app.post("/admin/integrations/test")
    async def admin_test_integrations(
        request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        _flash(request, "请进入对应用户账号测试其 FTP 或业务数据库连接。", "info")
        return _redirect("/admin/users")

    def _legacy_integrations_redirect(request: Request) -> RedirectResponse:
        """Retire the account-wide FTP editor without breaking old bookmarks."""

        _flash(
            request,
            "FTP 与数据库必须在对应的分群中设置。请进入目标群后打开“服务器与数据库”。",
            "info",
        )
        return _redirect("/user")

    @app.get("/user/integrations", response_class=HTMLResponse)
    async def user_integrations(request: Request, db: Session = Depends(get_db)):
        user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        return _legacy_integrations_redirect(request)

    @app.post("/user/integrations")
    @app.post("/user/integrations/browse")
    @app.post("/user/integrations/select-database")
    @app.post("/user/integrations/auto-select-database")
    @app.post("/user/integrations/test")
    async def user_legacy_integrations_action(
        request: Request, db: Session = Depends(get_db)
    ):
        user = _require_customer(request, db)
        if isinstance(user, RedirectResponse):
            return user
        await _validated_form(request)
        return _legacy_integrations_redirect(request)

    # Group-scoped external integrations.  Credentials remain in the
    # account-isolated portal setting; only the selected profile's public
    # summary is ever rendered or written to the group projection.  Admins
    # may maintain the owner account's scoped profile from the admin group
    # page, which keeps the feature reachable without moving secrets into a
    # shared administrator setting.
    def _integration_owner_id(user: User, access: object, db: Session, group_id: str) -> int:
        if not user.is_admin:
            return int(user.id)
        owner_id = getattr(access, "user_id", None)
        try:
            normalized = int(owner_id)
        except (TypeError, ValueError, OverflowError):
            normalized = 0
        if normalized > 0:
            return normalized
        owner_access = db.scalar(
            select(GroupAccess).where(GroupAccess.group_id == str(group_id))
        )
        try:
            normalized = int(getattr(owner_access, "user_id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            normalized = 0
        if normalized <= 0:
            raise PortalError("该群没有可用的配置所属账号")
        return normalized

    def _integration_group_prefix(user: User, group_id: str) -> str:
        prefix = "/admin/groups" if user.is_admin else "/user/groups"
        return f"{prefix}/{quote(group_id, safe='')}"

    @app.get("/user/groups/{group_id}/integrations", response_class=HTMLResponse)
    @app.get("/admin/groups/{group_id}/integrations", response_class=HTMLResponse)
    async def group_integrations(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id)
        if isinstance(access, RedirectResponse):
            return access
        group_id = validate_group_id(str(group_id))
        owner_id = _integration_owner_id(user, access, db, group_id)
        try:
            if migrate_legacy_group_ftp_connections(
                db, owner_id, group_id, user.id
            ):
                db.commit()
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        profiles = list_integration_profiles(db, owner_id, group_id=group_id)
        ftp_connections = list_ftp_connections(db, owner_id, group_id)
        requested_profile_raw = str(
            request.query_params.get("profile_id", "") or ""
        ).strip()
        creating_profile = str(request.query_params.get("new", "")) == "1"
        if creating_profile and not requested_profile_raw:
            requested_profile_raw = "server-" + uuid4().hex[:10]
        try:
            requested_profile = (
                normalize_integration_profile_id(requested_profile_raw)
                if requested_profile_raw
                else ""
            )
        except PortalError:
            _flash(request, "服务器配置编号格式不正确，已切换到当前使用服务器。", "warning")
            requested_profile = ""
        active_profile = get_active_integration_profile(db, owner_id, group_id)
        profile_id = requested_profile or active_profile or "default"
        # A valid, explicitly requested ID may be a brand-new profile.  Keep
        # it so the form renders an empty editor and the subsequent POST can
        # create it; only an omitted/invalid request should fall back to the
        # currently active stored profile.
        available_profile_ids = {
            str(item.get("profile_id", "") or "").strip()
            for item in profiles
            if isinstance(item, dict) and str(item.get("profile_id", "") or "").strip()
        }
        if not requested_profile and available_profile_ids and profile_id not in available_profile_ids:
            _flash(request, "服务器不存在，已切换到当前使用服务器。", "warning")
            profile_id = (
                "default"
                if "default" in available_profile_ids
                else sorted(available_profile_ids)[0]
            )
        requested_ftp_raw = str(request.query_params.get("ftp_id", "") or "").strip()
        creating_ftp = str(request.query_params.get("new_ftp", "")) == "1"
        if creating_ftp and not requested_ftp_raw:
            requested_ftp_raw = "ftp-" + uuid4().hex[:10]
        try:
            requested_ftp = (
                normalize_integration_profile_id(requested_ftp_raw)
                if requested_ftp_raw
                else ""
            )
        except PortalError:
            _flash(request, "FTP 连接 ID 格式不正确，已切换到可用连接。", "warning")
            requested_ftp = ""
        ftp_connection_id = requested_ftp or next(
            (
                str(item.get("ftp_connection_id", "") or "")
                for item in ftp_connections
                if str(item.get("ftp_connection_id", "") or "")
            ),
            "ftp-" + uuid4().hex[:10],
        )
        browse_requested = str(request.query_params.get("browse", "")) == "1"
        browse_path = str(request.query_params.get("ftp_path", "")).strip()[:1024]
        ftp_browser = None
        if browse_requested:
            try:
                ftp_browser = await run_in_threadpool(
                    _run_saved_ftp_browse,
                    browse_path or None,
                    owner_id,
                    settings,
                    group_id,
                    profile_id,
                )
            except (ValueError, PortalError) as exc:
                _flash(request, str(exc), "error")
        try:
            game_server_values = get_feature_values(
                group_id, "game_servers", db
            )
        except (PortalError, ValueError):
            # The server/database page must remain available when an older
            # group has a malformed optional game connection row.  The editor
            # will replace it on the next successful save.
            game_server_values = {}
        raw_game_profiles = game_server_values.get("profiles", [])
        raw_game_profiles = (
            raw_game_profiles if isinstance(raw_game_profiles, list) else []
        )
        game_by_integration: dict[str, dict] = {}
        for raw_game_profile in raw_game_profiles:
            if not isinstance(raw_game_profile, dict):
                continue
            game_profile = deepcopy(raw_game_profile)
            game_profile_id = str(game_profile.get("id", "") or "").strip()
            integration_id = str(
                game_profile.get("integration_profile_id", "") or game_profile_id
            ).strip()
            if integration_id and integration_id not in game_by_integration:
                game_by_integration[integration_id] = game_profile
        decorated_profiles: list[dict] = []
        for raw_profile in profiles:
            profile = deepcopy(raw_profile)
            profile_id_value = str(profile.get("profile_id", "") or "").strip()
            profile["game"] = deepcopy(game_by_integration.get(profile_id_value, {}))
            decorated_profiles.append(profile)
        current_game_profile = deepcopy(game_by_integration.get(profile_id, {}))
        current_ftp_connection_id = str(
            get_integration_config_view(
                db, owner_id, group_id=group_id, profile_id=profile_id
            ).get("ftp_connection_id", "")
            or ""
        ).strip()
        if not current_ftp_connection_id:
            # The connection identifier remains an internal implementation
            # detail.  A fresh random value avoids collisions for two long
            # server profile IDs while each later edit reuses this same row.
            current_ftp_connection_id = "ftp-" + uuid4().hex[:12]
        return templates.TemplateResponse(
            request=request,
                name="group_integrations.html",
            context=_template_context(
                request,
                user,
                group_id=group_id,
                integration_group_mode=True,
                integration_group_id=group_id,
                integration_profiles=decorated_profiles,
                integration_profile_id=profile_id,
                integration_creating_profile=creating_profile,
                ftp_connections=ftp_connections,
                ftp_connection_id=ftp_connection_id,
                ftp_connection_creating=creating_ftp,
                ftp_connection=get_ftp_connection_view(
                    db, owner_id, group_id, ftp_connection_id
                ),
        integration_config=get_integration_config_view(
                    db,
                    owner_id,
                    group_id=group_id,
                    profile_id=profile_id,
                ),
                unified_ftp_connection_id=current_ftp_connection_id,
                game_server_profile=current_game_profile,
                game_server_enabled=bool(game_server_values.get("enabled", False)),
                integration_prefix=_integration_group_prefix(user, group_id),
                integration_title=f"群 {group_id} · 服务器与数据库",
                integration_description="当前群可配置多台 SCUM 服务器；每台服务器独立保存 FTP 与数据库选择，SteamID 验证只读查询，签到只写入当前使用服务器的数据库。",
                integration_scope_label=(
                    f"管理员维护群 {group_id} · 所属账号 {owner_id}"
                    if user.is_admin
                    else f"仅群 {group_id} · 当前账号"
                ),
                integration_back_url=_group_path(user, group_id),
                integration_back_label="返回群配置",
                ftp_browser=ftp_browser,
                ftp_browser_requested=browse_requested,
                ftp_browser_path=browse_path,
            ),
        )

    @app.post("/user/groups/{group_id}/integrations/server")
    @app.post("/admin/groups/{group_id}/integrations/server")
    async def save_unified_group_server(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        """Save one game server together with its private FTP binding.

        The storage remains backward compatible: legacy shared FTP records
        stay readable, while new edits bind the selected server to exactly one
        connection.  Windows still receives only the database snapshot and
        game connection descriptor, never the FTP credential.
        """

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        group_id = validate_group_id(str(group_id))
        owner_id = _integration_owner_id(user, access, db, group_id)
        prefix = _integration_group_prefix(user, group_id)
        form = await _validated_form(request)
        profile_id = str(form.get("profile_id", "") or "").strip()
        original_profile_id = str(
            form.get("original_profile_id", profile_id) or profile_id
        ).strip()
        requested_connection_id = str(
            form.get("ftp_connection_id", "") or ""
        ).strip()
        requested_game_profile_id = str(
            form.get("game_profile_id", "") or ""
        ).strip()
        activate = str(form.get("activate_profile", "")).lower() in {
            "on", "1", "true"
        }
        game_enabled = _form_checkbox(form, "game_enabled")
        redirect_browse = False
        try:
            profile_id = normalize_integration_profile_id(profile_id)
            original_profile_id = (
                normalize_integration_profile_id(original_profile_id)
                if original_profile_id
                else ""
            )
            if original_profile_id and original_profile_id != profile_id:
                rename_integration_profile(
                    db,
                    owner_id,
                    group_id,
                    original_profile_id,
                    profile_id,
                    user.id,
                )
                # Keep an existing game row attached to the renamed
                # integration archive.  The WebSocket secret stays encrypted
                # inside the existing row; the blank URL tells the service to
                # retain it while only changing the public association.
                renamed_game_values = get_feature_values(group_id, "game_servers", db)
                renamed_game_profiles = renamed_game_values.get("profiles", [])
                renamed_game = next(
                    (
                        item
                        for item in renamed_game_profiles
                        if isinstance(item, dict)
                        and str(item.get("integration_profile_id", "") or "").strip()
                        == original_profile_id
                    ),
                    None,
                )
                if renamed_game is not None:
                    apply_game_server_profile_action(
                        group_id,
                        "save",
                        profile_id=str(renamed_game.get("id", "") or original_profile_id),
                        name=str(renamed_game.get("name", "") or profile_id),
                        host=str(renamed_game.get("host", "") or ""),
                        port=renamed_game.get("port", 28016),
                        executor_type=str(renamed_game.get("type", "plugin_websocket") or "plugin_websocket"),
                        timeout_seconds=renamed_game.get("timeout_seconds", 10),
                        profile_enabled=renamed_game.get("enabled", True),
                        integration_profile_id=profile_id,
                        db=db,
                        updated_by=user.id,
                    )
            current = get_integration_config_view(
                db, owner_id, group_id=group_id, profile_id=profile_id
            )
            selected_database_profile = str(
                form.get("database_profile_id", profile_id) or profile_id
            ).strip()
            if (
                original_profile_id
                and original_profile_id != profile_id
                and selected_database_profile == original_profile_id
            ):
                selected_database_profile = profile_id
            selected_database_profile = normalize_integration_profile_id(
                selected_database_profile
            )
            # Reusing another server's database also reuses its saved FTP
            # connection.  Do not overwrite that connection with the current
            # form's credentials; the current server still keeps its own
            # editable metadata while the database source is explicit.
            selected_database_view = None
            if selected_database_profile != profile_id:
                selected_database_view = get_integration_config_view(
                    db,
                    owner_id,
                    group_id=group_id,
                    profile_id=selected_database_profile,
                )
                selected_connection_id = str(
                    selected_database_view.get("ftp_connection_id", "") or ""
                ).strip()
                if not selected_connection_id:
                    raise PortalError("选择的服务器数据库没有可用 FTP 连接")
                requested_connection_id = selected_connection_id
            connection_id = str(
                (selected_database_view or {}).get("ftp_connection_id", "")
                if selected_database_view is not None
                else current.get("ftp_connection_id", "") or requested_connection_id
            ).strip()
            if not connection_id:
                connection_id = "ftp-" + uuid4().hex[:12]
            connection_id = normalize_integration_profile_id(connection_id)

            ftp_form = dict(form)
            ftp_form["ftp_connection_name"] = (
                str(form.get("profile_name", "") or "").strip() or profile_id
            )[:72] + " FTP"
            if selected_database_view is None or connection_id != str(
                selected_database_view.get("ftp_connection_id", "") or ""
            ).strip():
                save_ftp_connection(
                    db,
                    settings,
                    ftp_form,
                    user.id,
                    owner_id=owner_id,
                    group_id=group_id,
                    ftp_connection_id=connection_id,
                )
                # The server/database binding below validates against the
                # persisted group FTP map. Flush the new connection first so
                # a first-time "server + FTP + database" save never reads
                # the pre-save payload and reports a false missing-FTP error.
                db.flush()

            binding_form = dict(form)
            binding_form["ftp_connection_id"] = connection_id
            binding_form["database_profile_id"] = selected_database_profile
            # A shared database belongs to its source server.  Ignore a local
            # path posted by this form so saving metadata on the current server
            # can never overwrite or retarget the shared database.
            if selected_database_profile != profile_id:
                binding_form.pop("database_path", None)
            config = save_server_database_binding(
                db,
                binding_form,
                user.id,
                owner_id=owner_id,
                group_id=group_id,
                profile_id=profile_id,
                activate=activate,
            )
            db.flush()

            current_game_values = get_feature_values(group_id, "game_servers", db)
            current_game_profiles = current_game_values.get("profiles", [])
            current_game_profiles = (
                current_game_profiles
                if isinstance(current_game_profiles, list)
                else []
            )
            game_profile_id = requested_game_profile_id or profile_id
            existing_game = next(
                (
                    item
                    for item in current_game_profiles
                    if isinstance(item, dict)
                    and str(item.get("id", "") or "").strip() == game_profile_id
                ),
                None,
            )
            if existing_game is not None:
                existing_game_integration_id = str(
                    existing_game.get("integration_profile_id", "") or ""
                ).strip()
                if existing_game_integration_id:
                    if existing_game_integration_id != profile_id:
                        existing_game = None
                elif game_profile_id != profile_id:
                    # An unlinked legacy row is safe to reuse only when its
                    # game ID is the same as this server bundle ID.
                    existing_game = None
            # Reject a stale hidden game ID: FTP/database and game settings
            # must stay attached to the same server bundle in this group.
            if existing_game is None:
                existing_game = next(
                    (
                        item
                        for item in current_game_profiles
                        if isinstance(item, dict)
                        and str(item.get("integration_profile_id", "") or "").strip()
                        == profile_id
                    ),
                    None,
                )
                game_profile_id = (
                    str(existing_game.get("id", "") or "").strip()
                    if existing_game is not None
                    else profile_id
                )
            if not user.is_admin and existing_game is not None:
                stored_enabled = existing_game.get("enabled", True)
                if isinstance(stored_enabled, str):
                    stored_enabled = stored_enabled.strip().lower() not in {
                        "", "0", "false", "off", "no"
                    }
                game_enabled = bool(stored_enabled)
            # The actual plugin URL/API key belongs to the administrator's
            # server profile. Customers only verify the corresponding game
            # card and must never overwrite this backend connection.
            websocket_url = (
                str(form.get("websocket_url", "") or "").strip()
                if user.is_admin
                else ""
            )
            has_game_input = bool(websocket_url)
            if not user.is_admin and game_enabled and existing_game is None:
                raise PortalError("请联系管理员先配置本服务器的游戏连接，用户只需验证对应游戏卡密")
            game_timeout = str(
                form.get(
                    "timeout_seconds",
                    existing_game.get("timeout_seconds", 10)
                    if existing_game is not None
                    else 10,
                )
                or 10
            ).strip()
            # A database-only server is valid.  Creating a game connection,
            # however, still requires the complete documented WebSocket URL.
            if game_enabled or existing_game is not None or has_game_input:
                apply_game_server_profile_action(
                    group_id,
                    "save",
                    profile_id=game_profile_id,
                    name=str(form.get("profile_name", "") or "").strip(),
                    websocket_url=websocket_url,
                    timeout_seconds=game_timeout,
                    profile_enabled=game_enabled,
                    integration_profile_id=profile_id,
                    db=db,
                    updated_by=user.id,
                )
                if activate and game_enabled:
                    apply_game_server_profile_action(
                        group_id,
                        "activate",
                        profile_id=game_profile_id,
                        db=db,
                        updated_by=user.id,
                    )

            _sync_group_integration_profile(db, group_id, owner_id, user.id)
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "group_server_save",
                f"保存群 {group_id} 游戏服务器 {profile_id}",
                {
                    "profile_id": profile_id,
                    "ftp_connection_id": connection_id,
                    "game_profile_id": game_profile_id,
                    "game_enabled": game_enabled,
                    "database_selected": bool(
                        config.get("database", {}).get("sqlite_path", "")
                    ),
                },
            )
            test_after_save = str(form.get("test_after_save", "") or "").strip().lower()
            server_name = str(form.get("profile_name", "") or "").strip() or profile_id
            database_name = str(
                config.get("database", {}).get("display_name", "")
                or config.get("database", {}).get("database", "")
                or config.get("database", {}).get("sqlite_path", "")
                or "未命名数据库"
            ).strip()
            if test_after_save in {"ftp", "database"}:
                test_label = "数据库" if test_after_save == "database" else "FTP"
                try:
                    # The validation worker owns a separate session.  Commit
                    # the server binding first so a brand-new FTP/database
                    # configuration is immediately visible to that worker.
                    # The binding stays saved even when the external check
                    # fails, which lets the user correct it without retyping.
                    db.commit()
                    if test_after_save == "database":
                        database_path = str(
                            config.get("database", {}).get("sqlite_path", "")
                            or ""
                        ).strip()
                        if not database_path:
                            raise PortalError("请先填写远程 SQLite 数据库文件路径")
                        validated = await run_in_threadpool(
                            _run_validate_saved_ftp_database,
                            owner_id,
                            database_path,
                            settings,
                            group_id,
                            profile_id,
                            include_snapshot=True,
                        )
                        if not isinstance(validated, tuple) or len(validated) != 2:
                            raise PortalError("FTP 数据库快照下载失败，请稍后重试")
                        resolved_path, snapshot_bytes = validated
                        snapshot_result = save_database_snapshot(
                            db, owner_id, group_id, profile_id, snapshot_bytes
                        )
                        _sync_group_integration_profile(
                            db, group_id, owner_id, user.id
                        )
                        db.commit()
                        test_result = (
                            f"服务器「{server_name}」的数据库「{database_name}」"
                            f"连接成功，测试成功（{resolved_path}；"
                            f"Windows 快照版本 {snapshot_result['revision']}）"
                        )
                    else:
                        test_result = await run_in_threadpool(
                            _run_saved_integration_test,
                            "ftp",
                            owner_id,
                            settings,
                            group_id,
                            profile_id,
                        )
                    _flash(request, test_result, "success")
                    redirect_browse = test_after_save == "ftp"
                except (ValueError, PortalError) as exc:
                    # The save is already committed. Keep that fact visible so
                    # the user can correct the FTP details without losing the
                    # server/profile binding just created.
                    _flash(
                        request,
                        f"服务器「{server_name}」已保存，但 {test_label} 检测失败：{exc}",
                        "error",
                    )
            else:
                _flash(
                    request,
                    f"服务器「{server_name}」与数据库「{database_name}」已保存。"
                    "SteamID 验证会只读查询，签到会实际写入这台服务器的数据库。",
                    "success",
                )
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to save unified game server group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "游戏服务器保存失败，请稍后重试。", "error")
        target = f"{prefix}/integrations?profile_id={quote(profile_id, safe='')}"
        if redirect_browse:
            target += "&browse=1"
        return _redirect(target)

    @app.post("/user/groups/{group_id}/integrations/server/delete")
    @app.post("/admin/groups/{group_id}/integrations/server/delete")
    async def delete_unified_group_server(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        """Remove one server bundle without touching a sibling server."""

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        group_id = validate_group_id(str(group_id))
        owner_id = _integration_owner_id(user, access, db, group_id)
        prefix = _integration_group_prefix(user, group_id)
        form = await _validated_form(request)
        profile_id = str(form.get("profile_id", "") or "").strip()
        try:
            profile_id = normalize_integration_profile_id(profile_id)
            current = get_integration_config_view(
                db, owner_id, group_id=group_id, profile_id=profile_id
            )
            connection_id = str(current.get("ftp_connection_id", "") or "").strip()
            current_game_values = get_feature_values(group_id, "game_servers", db)
            current_game_profiles = current_game_values.get("profiles", [])
            current_game_profiles = (
                current_game_profiles
                if isinstance(current_game_profiles, list)
                else []
            )
            linked_game_profile = next(
                (
                    item
                    for item in current_game_profiles
                    if isinstance(item, dict)
                    and (
                        str(item.get("integration_profile_id", "") or "").strip()
                        == profile_id
                        or (
                            not str(item.get("integration_profile_id", "") or "").strip()
                            and str(item.get("id", "") or "").strip() == profile_id
                        )
                    )
                ),
                None,
            )
            game_profile_id = (
                str(linked_game_profile.get("id", "") or "").strip()
                if linked_game_profile
                else ""
            )
            if game_profile_id:
                apply_game_server_profile_action(
                    group_id,
                    "delete",
                    profile_id=game_profile_id,
                    db=db,
                    updated_by=user.id,
                )
            delete_integration_profile(db, owner_id, group_id, profile_id, user.id)
            if connection_id:
                # A legacy connection may deliberately serve another profile.
                # Delete only when it became unused after this bundle removal.
                connection = get_ftp_connection_view(
                    db, owner_id, group_id, connection_id
                )
                if int(connection.get("bound_count", 0) or 0) == 0:
                    delete_ftp_connection(
                        db, owner_id, group_id, connection_id, user.id
                    )
            db.flush()
            _sync_group_integration_profile(db, group_id, owner_id, user.id)
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "group_server_delete",
                f"删除群 {group_id} 游戏服务器 {profile_id}",
                {"profile_id": profile_id, "game_profile_id": game_profile_id},
            )
            _flash(request, "游戏服务器及其未被其他服务器使用的 FTP 连接已删除。", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to delete unified game server group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "删除游戏服务器失败，请稍后重试。", "error")
        return _redirect(f"{prefix}/integrations")

    @app.post("/user/groups/{group_id}/integrations/server/status")
    @app.post("/admin/groups/{group_id}/integrations/server/status")
    async def set_unified_group_server_status(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        group_id = validate_group_id(str(group_id))
        owner_id = _integration_owner_id(user, access, db, group_id)
        prefix = _integration_group_prefix(user, group_id)
        form = await _validated_form(request)
        profile_id = str(form.get("profile_id", "") or "").strip()
        enabled = _form_checkbox(form, "enabled")
        try:
            profile_id = normalize_integration_profile_id(profile_id)
            set_integration_profile_enabled(
                db, owner_id, group_id, profile_id, enabled, user.id
            )
            db.flush()
            _sync_group_integration_profile(db, group_id, owner_id, user.id)
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "group_server_enable" if enabled else "group_server_pause",
                f"{'启用' if enabled else '暂停'}群 {group_id} 游戏服务器 {profile_id}",
                {"profile_id": profile_id, "enabled": enabled},
            )
            db.commit()
            _flash(request, "服务器已启用。" if enabled else "服务器已暂停。", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to change unified game server status group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "服务器状态保存失败，请稍后重试。", "error")
        return _redirect(f"{prefix}/integrations?profile_id={quote(profile_id, safe='')}")

    @app.post("/user/groups/{group_id}/integrations")
    @app.post("/admin/groups/{group_id}/integrations")
    async def save_group_integrations(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        group_id = validate_group_id(str(group_id))
        owner_id = _integration_owner_id(user, access, db, group_id)
        form = await _validated_form(request)
        profile_id = str(form.get("profile_id", "default") or "default").strip()
        activate = str(form.get("activate_profile", "")) in {"on", "1", "true"}
        try:
            config = save_server_database_binding(
                db,
                form,
                user.id,
                owner_id=owner_id,
                group_id=group_id,
                profile_id=profile_id,
                activate=activate,
            )
            # The profile row and the Windows-safe projection share this
            # request transaction so a failed projection cannot advertise a
            # database that Windows will not be able to validate.
            db.flush()
            _sync_group_integration_profile(db, group_id, owner_id, user.id)
            add_audit_log(
                db,
                user,
                group_id,
                "group_integrations_config",
                f"更新群 {group_id} 服务器 {profile_id}",
                request,
                {"profile_id": profile_id, "config": config},
            )
            db.commit()
            _flash(request, "服务器已保存；FTP 连接与数据库选择保持独立。", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        target = f"{_integration_group_prefix(user, group_id)}/integrations?profile_id={quote(profile_id, safe='')}"
        return _redirect(target)

    @app.post("/user/groups/{group_id}/integrations/ftp-connections")
    @app.post("/admin/groups/{group_id}/integrations/ftp-connections")
    async def save_group_ftp_connection(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        group_id = validate_group_id(str(group_id))
        owner_id = _integration_owner_id(user, access, db, group_id)
        prefix = _integration_group_prefix(user, group_id)
        form = await _validated_form(request)
        connection_id = str(form.get("ftp_connection_id", "") or "").strip()
        profile_id = str(form.get("profile_id", "") or "").strip()
        try:
            connection = save_ftp_connection(
                db,
                settings,
                form,
                user.id,
                owner_id=owner_id,
                group_id=group_id,
                ftp_connection_id=connection_id,
            )
            db.flush()
            _sync_group_integration_profile(db, group_id, owner_id, user.id)
            invalidated = list(connection.get("invalidated_profile_ids", []) or [])
            add_audit_log(
                db,
                user,
                group_id,
                "group_ftp_connection_save",
                f"保存群 {group_id} FTP 连接 {connection_id}",
                request,
                {"ftp_connection_id": connection_id, "invalidated_profiles": invalidated},
            )
            db.commit()
            suffix = (
                "；已使引用它的数据库快照失效"
                if invalidated
                else ""
            )
            _flash(request, f"FTP 连接 {connection_id} 已独立保存{suffix}。", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        target = f"{prefix}/integrations?ftp_id={quote(connection_id, safe='')}"
        if profile_id:
            target += f"&profile_id={quote(profile_id, safe='')}"
        return _redirect(target)

    @app.post("/user/groups/{group_id}/integrations/ftp-connections/delete")
    @app.post("/admin/groups/{group_id}/integrations/ftp-connections/delete")
    async def delete_group_ftp_connection(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        group_id = validate_group_id(str(group_id))
        owner_id = _integration_owner_id(user, access, db, group_id)
        prefix = _integration_group_prefix(user, group_id)
        form = await _validated_form(request)
        connection_id = str(form.get("ftp_connection_id", "") or "").strip()
        profile_id = str(form.get("profile_id", "") or "").strip()
        detach_bound_profiles = _form_checkbox(form, "detach_bound_profiles")
        try:
            detached_profiles = delete_ftp_connection(
                db,
                owner_id,
                group_id,
                connection_id,
                user.id,
                detach_bound_profiles=detach_bound_profiles,
            )
            db.flush()
            _sync_group_integration_profile(db, group_id, owner_id, user.id)
            add_audit_log(
                db,
                user,
                group_id,
                "group_ftp_connection_delete",
                f"删除群 {group_id} FTP 连接 {connection_id}",
                request,
                {"ftp_connection_id": connection_id},
            )
            db.commit()
            if detached_profiles:
                _flash(
                    request,
                    f"FTP 连接 {connection_id} 已删除；已解除服务器 "
                    f"{'、'.join(detached_profiles[:5])} 的数据库绑定。",
                    "success",
                )
            else:
                _flash(request, f"FTP 连接 {connection_id} 已删除。", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        target = f"{prefix}/integrations"
        if profile_id:
            target += f"?profile_id={quote(profile_id, safe='')}"
        return _redirect(target)

    @app.post("/user/groups/{group_id}/integrations/browse")
    @app.post("/admin/groups/{group_id}/integrations/browse")
    async def browse_group_integrations(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id)
        if isinstance(access, RedirectResponse):
            return access
        group_id = validate_group_id(str(group_id))
        prefix = _integration_group_prefix(user, group_id)
        form = await _validated_form(request)
        profile_id = str(form.get("profile_id", "default") or "default").strip()
        path = str(form.get("ftp_path", "")).strip()[:1024]
        target = f"{prefix}/integrations?profile_id={quote(profile_id, safe='')}&browse=1"
        if path:
            target += "&ftp_path=" + quote(path, safe="")
        return _redirect(target)

    @app.post("/user/groups/{group_id}/integrations/select-database")
    @app.post("/admin/groups/{group_id}/integrations/select-database")
    async def select_group_ftp_database(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        group_id = validate_group_id(str(group_id))
        owner_id = _integration_owner_id(user, access, db, group_id)
        prefix = _integration_group_prefix(user, group_id)
        form = await _validated_form(request)
        profile_id = str(form.get("profile_id", "default") or "default").strip()
        path = str(form.get("database_path", "")).strip()[:1024]
        browse_path = str(form.get("ftp_path", "")).strip()[:1024]
        source = str(form.get("selection_source", "manual")).strip().lower()
        try:
            selected = save_ftp_database_selection(
                db,
                owner_id,
                path,
                owner_id,
                source=source,
                group_id=group_id,
                profile_id=profile_id,
            )
            selected_database = selected.get("database") or {}
            selected_name = str(selected.get("profile_name") or profile_id).strip()
            selected_database_name = str(
                selected_database.get("display_name", "")
                or selected_database.get("database", "")
                or path
            ).strip()
            add_audit_log(
                db,
                user,
                group_id,
                "group_database_select",
                f"群 {group_id} 选择服务器数据库",
                request,
                {
                    "profile_id": profile_id,
                    "engine": selected["database"]["engine"],
                    "sqlite_path": selected_database.get("sqlite_path", ""),
                    "selection_source": selected_database.get("selection_source", "ftp"),
                },
            )
            db.commit()
            # The selected database is already committed and usable by the
            # Linux-side SteamID/sign-in paths.  Publishing its safe Windows
            # projection must never turn a successful database choice into a
            # failed web action when the client is temporarily offline.
            try:
                _sync_group_integration_profile(db, group_id, owner_id, user.id)
                db.commit()
            except Exception:
                db.rollback()
                logger.warning(
                    "Database selection saved but Windows projection is pending group_id=%s profile_id=%s",
                    group_id,
                    profile_id,
                    exc_info=True,
                )
            _flash(
                request,
                f"服务器「{selected_name}」已选择数据库「{selected_database_name}」。"
                "SteamID 验证和签到现在使用此数据库；Windows 会在下一次心跳同步当前设置。",
                "success",
            )
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception as exc:
            db.rollback()
            logger.exception(
                "Unable to select FTP database group_id=%s profile_id=%s request_id=%s",
                group_id,
                profile_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(
                request,
                f"数据库选择处理失败（{type(exc).__name__}），本次保存已撤销；"
                "请在网页后台日志中查看 FTP 数据库选择错误。",
                "error",
            )
        target = f"{prefix}/integrations?profile_id={quote(profile_id, safe='')}&browse=1"
        if browse_path:
            target += "&ftp_path=" + quote(browse_path, safe="")
        return _redirect(target)

    @app.post("/user/groups/{group_id}/integrations/auto-select-database")
    @app.post("/admin/groups/{group_id}/integrations/auto-select-database")
    async def auto_select_group_ftp_database(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        group_id = validate_group_id(str(group_id))
        owner_id = _integration_owner_id(user, access, db, group_id)
        prefix = _integration_group_prefix(user, group_id)
        form = await _validated_form(request)
        profile_id = str(form.get("profile_id", "default") or "default").strip()
        try:
            path = await run_in_threadpool(
                _run_auto_select_ftp_database,
                owner_id,
                settings,
                group_id,
                profile_id,
            )
            if not path:
                raise PortalError("FTP 中未找到 beijixiong.db，请浏览目录后手动选择数据库文件")
            selected = save_ftp_database_selection(
                db,
                owner_id,
                path,
                user.id,
                source="auto",
                group_id=group_id,
                profile_id=profile_id,
            )
            selected_database = selected.get("database") or {}
            selected_name = str(selected.get("profile_name") or profile_id).strip()
            selected_database_name = str(
                selected_database.get("display_name", "")
                or selected_database.get("database", "")
                or path
            ).strip()
            add_audit_log(
                db,
                user,
                group_id,
                "group_database_auto_select",
                f"群 {group_id} 自动选择服务器数据库",
                request,
                {
                    "profile_id": profile_id,
                    "sqlite_path": selected_database.get("sqlite_path", ""),
                    "selection_source": selected_database.get("selection_source", "auto"),
                },
            )
            db.commit()
            try:
                _sync_group_integration_profile(db, group_id, owner_id, user.id)
                db.commit()
            except Exception:
                db.rollback()
                logger.warning(
                    "Automatic database selection saved but Windows projection is pending group_id=%s profile_id=%s",
                    group_id,
                    profile_id,
                    exc_info=True,
                )
            _flash(
                request,
                f"服务器「{selected_name}」已选择数据库「{selected_database_name}」。"
                "SteamID 验证和签到现在使用此数据库；Windows 会在下一次心跳同步当前设置。",
                "success",
            )
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception as exc:
            db.rollback()
            logger.exception(
                "Unable to auto-select FTP database group_id=%s profile_id=%s request_id=%s",
                group_id,
                profile_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(
                request,
                f"数据库自动选择处理失败（{type(exc).__name__}），本次保存已撤销；"
                "请在网页后台日志中查看 FTP 数据库选择错误。",
                "error",
            )
        return _redirect(
            f"{prefix}/integrations?profile_id={quote(profile_id, safe='')}&browse=1"
        )

    @app.post("/user/groups/{group_id}/integrations/test")
    @app.post("/admin/groups/{group_id}/integrations/test")
    async def test_group_integrations(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id)
        if isinstance(access, RedirectResponse):
            return access
        group_id = validate_group_id(str(group_id))
        owner_id = _integration_owner_id(user, access, db, group_id)
        prefix = _integration_group_prefix(user, group_id)
        form = await _validated_form(request)
        profile_id = str(form.get("profile_id", "default") or "default").strip()
        target = str(form.get("target", "both")).strip().lower()[:16]
        try:
            result = await run_in_threadpool(
                _run_saved_integration_test,
                target,
                owner_id,
                settings,
                group_id,
                profile_id,
            )
            add_audit_log(
                db,
                user,
                group_id,
                "group_integrations_test",
                f"测试群 {group_id} 服务器 {profile_id} 的 {target} 连接",
                request,
                {"profile_id": profile_id, "target": target, "success": True},
            )
            db.commit()
            _flash(request, result, "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(
            f"{prefix}/integrations?profile_id={quote(profile_id, safe='')}"
        )

    @app.post("/user/groups/{group_id}/integration-profiles/activate")
    @app.post("/admin/groups/{group_id}/integration-profiles/activate")
    async def activate_group_integration_profile(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        group_id = validate_group_id(str(group_id))
        owner_id = _integration_owner_id(user, access, db, group_id)
        prefix = _integration_group_prefix(user, group_id)
        form = await _validated_form(request)
        profile_id = str(form.get("profile_id", "") or "").strip()
        try:
            set_active_integration_profile(db, owner_id, group_id, profile_id, user.id)
            db.flush()
            _sync_group_integration_profile(db, group_id, owner_id, user.id)
            db.commit()
            current_view = get_integration_config_view(
                db, owner_id, group_id=group_id, profile_id=profile_id
            )
            current_name = str(current_view.get("profile_name") or profile_id).strip()
            _flash(request, f"已将服务器「{current_name}」设为当前使用服务器。", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(f"{prefix}/integrations?profile_id={quote(profile_id, safe='')}")

    @app.post("/user/groups/{group_id}/integration-profiles/delete")
    @app.post("/admin/groups/{group_id}/integration-profiles/delete")
    async def delete_group_integration_profile(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        group_id = validate_group_id(str(group_id))
        owner_id = _integration_owner_id(user, access, db, group_id)
        prefix = _integration_group_prefix(user, group_id)
        form = await _validated_form(request)
        profile_id = str(form.get("profile_id", "") or "").strip()
        try:
            deleted = delete_integration_profile(db, owner_id, group_id, profile_id, user.id)
            db.flush()
            _sync_group_integration_profile(db, group_id, owner_id, user.id)
            db.commit()
            _flash(request, "服务器已删除。", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(f"{prefix}/integrations")

    @app.post("/admin/licenses/generate")
    async def admin_generate_licenses(
        request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        try:
            days = int(str(form.get("days", "")))
            count = int(str(form.get("count", "1")))
            generated = generate_admin_license_keys(days, count)
            add_audit_log(
                db,
                user,
                None,
                "admin_license_generate",
                f"生成 {len(generated)} 张 {'永久' if days == -1 else f'{days} 天'}卡密",
                request,
                {"days": days, "count": len(generated)},
            )
            db.commit()
            _flash(request, f"已生成 {len(generated)} 张卡密", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc) if str(exc) else "卡密参数无效", "error")
        return _redirect("/admin/licenses")

    @app.post("/admin/licenses/delete")
    async def admin_delete_unused_license(
        request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        card = str(form.get("license_key", ""))
        try:
            delete_unused_license_key(card)
            add_audit_log(
                db,
                user,
                None,
                "admin_license_delete",
                "删除一张未使用卡密",
                request,
            )
            db.commit()
            _flash(request, "未使用卡密已删除", "success")
        except PortalError as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect("/admin/licenses")

    @app.post("/admin/users/create")
    async def admin_create_user(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        try:
            username = validate_username(str(form.get("username", "")))
            password = validate_password(str(form.get("password", "")))
            role = str(form.get("role", "customer"))
            if role not in {"customer", "admin"}:
                raise PortalError("用户角色无效")
            if db.scalar(select(User).where(User.username == username)):
                raise PortalError("该账号已经存在")
            target = User(
                username=username,
                password_hash=hash_password(password),
                role=role,
                is_active=True,
            )
            db.add(target)
            db.flush()
            add_audit_log(
                db,
                user,
                None,
                "admin_user_create",
                f"创建{role}账号 {username}",
                request,
                {"target_user_id": target.id, "role": role},
            )
            db.commit()
            _flash(request, f"账号 {username} 已创建", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except IntegrityError:
            db.rollback()
            _flash(request, "该账号已经存在", "error")
        return _redirect("/admin/users")

    @app.post("/admin/users/{target_user_id}/role")
    async def admin_update_user_role(
        target_user_id: int, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        target = db.get(User, target_user_id)
        role = str(form.get("role", ""))
        if target is None:
            _flash(request, "用户不存在", "error")
        elif role not in {"customer", "admin"}:
            _flash(request, "用户角色无效", "error")
        elif role == "customer" and not can_remove_admin(db, target, user):
            _flash(request, "不能降低自己的权限或移除最后一个有效管理员", "error")
        else:
            target.role = role
            add_audit_log(
                db,
                user,
                None,
                "admin_user_role",
                f"将用户 {target.username} 设置为 {role}",
                request,
                {"target_user_id": target.id, "role": role},
            )
            db.commit()
            _flash(request, "用户角色已更新", "success")
        return _redirect(f"/admin/users/{target_user_id}")

    @app.post("/admin/users/{target_user_id}/status")
    async def admin_update_user_status(
        target_user_id: int, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        target = db.get(User, target_user_id)
        enabled = str(form.get("enabled", "")) == "true"
        if target is None:
            _flash(request, "用户不存在", "error")
        elif not enabled and not can_remove_admin(db, target, user):
            _flash(request, "不能停用自己的账号或最后一个有效管理员", "error")
        else:
            target.is_active = enabled
            add_audit_log(
                db,
                user,
                None,
                "admin_user_status",
                f"{'启用' if enabled else '停用'}用户 {target.username}",
                request,
                {"target_user_id": target.id, "enabled": enabled},
            )
            db.commit()
            _flash(request, "用户状态已更新", "success")
        return _redirect(f"/admin/users/{target_user_id}")

    @app.post("/admin/users/{target_user_id}/password")
    async def admin_reset_user_password(
        target_user_id: int, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        form = await _validated_form(request)
        target = db.get(User, target_user_id)
        try:
            if target is None:
                raise PortalError("用户不存在")
            new_password = validate_password(str(form.get("new_password", "")))
            if new_password != str(form.get("new_password_confirm", "")):
                raise PortalError("两次输入的新密码不一致")
            target.password_hash = hash_password(new_password)
            add_audit_log(
                db,
                user,
                None,
                "admin_password_reset",
                f"重置用户 {target.username} 的密码",
                request,
                {"target_user_id": target.id},
            )
            db.commit()
            _flash(request, "用户密码已重置", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(f"/admin/users/{target_user_id}")

    @app.post("/admin/groups/{group_id}/unbind")
    async def admin_unbind_group(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        await _validated_form(request)
        access = db.scalar(select(GroupAccess).where(GroupAccess.group_id == group_id))
        if access is None:
            _flash(request, "该群没有网页账号绑定", "warning")
        else:
            owner_name = access.user.username
            db.delete(access)
            add_audit_log(
                db,
                user,
                group_id,
                "admin_group_unbind",
                f"解除群 {group_id} 与用户 {owner_name} 的网页绑定",
                request,
            )
            db.commit()
            _flash(request, "网页群归属已解除，机器人授权和分群配置均已保留", "success")
        return _redirect("/admin/groups")

    @app.post("/user/groups/{group_id}/delete")
    @app.post("/admin/groups/{group_id}/delete")
    async def delete_portal_group(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        """Remove a web registration without deleting Windows group data."""

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        try:
            group_id = validate_group_id(str(group_id))
        except ValueError as exc:
            _flash(request, str(exc), "error")
            return _redirect("/admin/groups" if user.is_admin else "/user")

        access = _owned_group_or_redirect(request, db, user, group_id)
        if isinstance(access, RedirectResponse):
            return access
        await _validated_form(request)
        target = db.scalar(select(GroupAccess).where(GroupAccess.group_id == group_id))
        if target is None:
            _flash(request, "该群没有网页登记记录", "warning")
        else:
            owner_name = target.user.username if target.user is not None else "未知账号"
            db.delete(target)
            add_audit_log(
                db,
                user,
                group_id,
                "portal_group_delete",
                f"删除网页群组登记 {group_id}",
                request,
                {
                    "owner": owner_name,
                    "runtime_data_preserved": True,
                    "meaning": "remove_web_registration_only",
                },
            )
            db.commit()
            _flash(
                request,
                f"群 {group_id} 已从网页账号移除；Windows 授权和本地群配置未删除",
                "success",
            )
        return _redirect("/admin/groups" if user.is_admin else "/user")

    @app.get("/admin/global", response_class=HTMLResponse)
    def admin_global_config(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        if settings.data_mode == "remote":
            _flash(
                request,
                "远程网页不修改本地 AstrBot 全局配置，请在本地插件设置中操作",
                "warning",
            )
            return _redirect("/admin")
        try:
            config = load_global_config()
            general_values = get_general_values(config)
            features = [
                {
                    "spec": spec,
                    "values": get_global_feature_values(spec.key, config),
                }
                for spec in FEATURE_BY_KEY.values()
            ]
        except ValueError as exc:
            _flash(request, str(exc), "error")
            return _redirect("/admin")
        return templates.TemplateResponse(
            request=request,
            name="admin_global.html",
            context=_template_context(
                request,
                user,
                general_fields=GENERAL_FIELDS,
                general_values=general_values,
                features=features,
                updated_at=config.get("_updated_at", 0),
                group_count=len(known_group_ids(db)),
            ),
        )

    @app.post("/admin/global/general")
    async def admin_update_global_general(
        request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        if settings.data_mode == "remote":
            raise PortalError("远程网页不能修改本地 AstrBot 全局配置")
        form = await _validated_form(request)
        try:
            values = {
                item.spec.key: parse_field_value(
                    item.spec,
                    form.get(item.spec.key),
                    item.spec.key in form,
                )
                for item in GENERAL_FIELDS
            }
            update_general_values(values)
            add_audit_log(
                db,
                user,
                None,
                "admin_global_general",
                "更新群管全局基础配置",
                request,
                values,
            )
            db.commit()
            _flash(request, "全局基础配置已保存", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect("/admin/global#global-general")

    @app.post("/admin/global/features/{feature_key}")
    async def admin_update_global_feature(
        feature_key: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        if settings.data_mode == "remote":
            raise PortalError("远程网页不能修改本地 AstrBot 全局配置")
        form = await _validated_form(request)
        spec = FEATURE_BY_KEY.get(feature_key)
        if spec is None:
            raise PortalError("未知全局功能模块")
        try:
            current_values = get_global_feature_values(feature_key)
            values = {
                field.key: (
                    current_values.get(field.key, field.default)
                    if field.group_only
                    else parse_field_value(
                        field,
                        form.getlist(field.key) if field.kind == "multichoice" else form.get(field.key),
                        field.key in form,
                    )
                )
                for field in spec.fields
            }
            update_global_feature_values(feature_key, values)
            add_audit_log(
                db,
                user,
                None,
                "admin_global_feature",
                f"更新全局{spec.label}默认配置",
                request,
                {feature_key: values},
            )
            db.commit()
            _flash(request, f"全局{spec.label}配置已保存", "success")
        except (ValueError, PortalError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(f"/admin/global#global-{quote(feature_key)}")

    @app.post("/admin/global/sync")
    async def admin_sync_global(request: Request, db: Session = Depends(get_db)):
        user = _require_admin(request, db)
        if isinstance(user, RedirectResponse):
            return user
        if settings.data_mode == "remote":
            raise PortalError("远程网页不能执行本地全局配置同步")
        await _validated_form(request)
        try:
            count = sync_global_to_groups()
            add_audit_log(
                db,
                user,
                None,
                "admin_global_sync",
                f"同步全局配置到 {count} 个分群",
                request,
                {"group_count": count},
            )
            db.commit()
            _flash(request, f"全局配置已同步到 {count} 个分群", "success")
        except ValueError as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect("/admin/global")

    @app.get("/groups/{group_id}", include_in_schema=False)
    def legacy_group_detail(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _current_user(request, db)
        if user is None:
            return _redirect("/user/login")
        return _redirect(_group_path(user, group_id))

    @app.get("/user/groups/{group_id}", response_class=HTMLResponse)
    @app.get("/admin/groups/{group_id}", response_class=HTMLResponse)
    def group_detail(group_id: str, request: Request, db: Session = Depends(get_db)):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id)
        if isinstance(access, RedirectResponse):
            return access
        # Rendering is deliberately read-only.  The portal must not queue a
        # Windows pull merely because a user opened or refreshed this page;
        # the explicit "request sync" action below is scoped to this group.
        try:
            view = get_group_view(group_id, db, include_members=False)
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to build group view group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            view = _fallback_group_view(group_id)
            try:
                view["auth"] = authorization_state(group_id, db)
            except Exception:
                db.rollback()
                logger.exception(
                    "Unable to load fallback group authorization group_id=%s request_id=%s",
                    group_id,
                    getattr(request.state, "request_id", ""),
                )
        try:
            console_settings = get_console_settings(db)
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to load console settings group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            console_settings = {
                "enabled": False,
                "messages_enabled": False,
                "retention": 2000,
                "keywords": [],
                "suggested_keywords": list(DEFAULT_CONSOLE_KEYWORDS),
            }
        console_filter = str(request.query_params.get("console_keywords", "")).strip()
        console_filter_keywords = normalize_console_keywords(console_filter)
        console_user_id = str(request.query_params.get("console_user_id", "")).strip()[:20]
        console_user_name = (
            str(request.query_params.get("console_user_name", ""))
            .replace("\r", " ")
            .replace("\n", " ")
            .strip()[:128]
        )
        console_start = str(request.query_params.get("console_start", "")).strip()[:40]
        console_end = str(request.query_params.get("console_end", "")).strip()[:40]
        console_start_at = _parse_console_datetime_filter(console_start)
        console_end_at = _parse_console_datetime_filter(console_end)
        console_filter_error = ""
        if console_start and console_start_at is None:
            console_filter_error = "开始时间格式不正确。"
        elif console_end and console_end_at is None:
            console_filter_error = "结束时间格式不正确。"
        elif (
            console_start_at is not None
            and console_end_at is not None
            and console_start_at > console_end_at
        ):
            console_filter_error = "开始时间不能晚于结束时间。"
        can_view_console = (
            settings.data_mode == "remote" and user_can_view_console(user, console_settings)
        )
        console_events = []
        console_pagination = _pagination_context(
            request,
            0,
            page_param="console_page",
            page_size=50,
            anchor="#console-log",
        )
        if can_view_console:
            try:
                console_total = remote_store.count_console_events(
                    db,
                    group_id,
                    console_filter_keywords,
                    user_id=console_user_id,
                    user_name=console_user_name,
                    start_at=(
                        console_start_at
                        if console_start_at is not None
                        else console_start or None
                    ),
                    end_at=(
                        console_end_at
                        if console_end_at is not None
                        else console_end or None
                    ),
                )
                console_pagination = _pagination_context(
                    request,
                    console_total,
                    page_param="console_page",
                    page_size=50,
                    anchor="#console-log",
                )
                console_offset = (
                    console_pagination["page"] - 1
                ) * console_pagination["page_size"]
                console_events = remote_store.list_console_events(
                    db,
                    group_id,
                    console_filter_keywords,
                    user_id=console_user_id,
                    user_name=console_user_name,
                    # Invalid direct URL values deliberately yield no rows;
                    # never drop a malformed time filter and show unrelated
                    # runtime records from the current group.
                    start_at=(console_start_at if console_start_at is not None else console_start or None),
                    end_at=(console_end_at if console_end_at is not None else console_end or None),
                    limit=console_pagination["page_size"],
                    offset=console_offset,
                )
            except Exception:
                # A legacy/null CMD row must never prevent configuration forms
                # from loading. The next sync or an administrator can repair
                # the optional log independently of the group projection.
                db.rollback()
                logger.warning(
                    "Unable to load console events group_id=%s request_id=%s",
                    group_id,
                    getattr(request.state, "request_id", ""),
                    exc_info=True,
                )
                console_events = []
                console_pagination = _pagination_context(
                    request,
                    0,
                    page_param="console_page",
                    page_size=50,
                    anchor="#console-log",
                )
        try:
            if user.is_admin:
                clone_sources = known_group_ids(db)
            else:
                clone_sources = set(
                    db.scalars(
                        select(GroupAccess.group_id).where(GroupAccess.user_id == user.id)
                    )
                )
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to load clone sources group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            clone_sources = set()
        clone_sources = sorted(
            (str(item) for item in clone_sources if str(item) != str(group_id)),
            key=lambda value: (len(value), value),
        )
        can_edit, edit_block_reason = _group_edit_status(
            group_id,
            db,
            auth=view.get("auth"),
            sync=view.get("sync"),
        )
        try:
            game_status = get_group_game_status(group_id, db)
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to load game heartbeat group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            game_status = {
                "state": "unknown",
                "active": False,
                "expire": 0,
                "expire_text": "暂时无法读取",
                "card_mask": "",
                "client_id": "",
                "last_heartbeat_text": "暂时无法读取 Windows 心跳",
                "message": "游戏心跳状态暂时无法读取，请稍后刷新。",
            }
        try:
            websocket_access_status = remote_store.get_group_websocket_access_status(
                db, group_id
            )
            websocket_site_settings = get_im3_socketio_settings(
                db, settings, include_secrets=False
            )
        except Exception:
            db.rollback()
            logger.warning(
                "Unable to load user websocket status group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
                exc_info=True,
            )
            websocket_access_status = {
                "state": "missing",
                "configured": False,
                "card_mask": "",
            }
            websocket_site_settings = {
                "enabled": False,
                "server_url": "",
                "socketio_path": "/socket.io",
                "bot_type": "bot_main",
            }
        game_license_verified = False
        verified_games = request.session.get("game_license_verified", {})
        if isinstance(verified_games, dict):
            verified_item = verified_games.get(str(group_id))
            if isinstance(verified_item, dict):
                verified_expire = int(verified_item.get("expire", 0) or 0)
                game_license_verified = bool(
                    verified_expire == -1
                    or (
                        verified_expire > int(datetime.now(timezone.utc).timestamp())
                        and game_status.get("active")
                    )
                )
        websocket_license_verified = bool(
            websocket_access_status.get("state") == "verified"
            and websocket_access_status.get("configured")
        )
        try:
            game_websocket_config = get_game_websocket_settings(db, settings)
        except Exception:
            db.rollback()
            logger.warning(
                "Unable to load game websocket settings for group page group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
                exc_info=True,
            )
            game_websocket_config = {
                "enabled": False,
                "game_websocket_enabled": False,
                "game_websocket_token_configured": False,
                "token_configured": False,
                "portal_url": "",
                "endpoint_path": "/api/sync/v1/ws",
            }
        game_websocket_config_ready = bool(
            game_websocket_runtime_enabled(game_websocket_config)
            and game_websocket_config.get("game_websocket_token_configured")
        )
        game_server_feature = next(
            (
                item.get("values", {})
                for item in view.get("features", [])
                if isinstance(item, dict)
                and getattr(item.get("spec"), "key", "") == "game_servers"
            ),
            {},
        )
        game_server_profiles = game_server_feature.get("profiles", [])
        group_executor_available = bool(
            game_server_feature.get("enabled")
            and isinstance(game_server_profiles, list)
            and any(
                isinstance(profile, dict) and profile.get("enabled", True)
                for profile in game_server_profiles
            )
        )
        global_executor = game_websocket_config.get("game_executor", {})
        global_executor_available = bool(
            isinstance(global_executor, dict) and global_executor.get("enabled")
        )
        game_transport = game_websocket_user_view(
            game_websocket_config,
            group_authorized=(
                True
                if game_status.get("state") == "local"
                else bool(game_status.get("active"))
            ),
            executor_available=bool(
                group_executor_available or global_executor_available
            ),
            local_mode=game_status.get("state") == "local",
        )
        game_websocket_ready = bool(game_transport["callable"])
        public_game_websocket_config = {
            "enabled": bool(game_websocket_config.get("enabled")),
            "game_websocket_enabled": bool(
                game_websocket_config.get("game_websocket_enabled")
            ),
            "token_configured": bool(
                game_websocket_config.get("game_websocket_token_configured")
            ),
            "endpoint_path": str(
                game_websocket_config.get("endpoint_path", "/api/sync/v1/ws")
            ),
        }
        # The game-server editor only needs safe profile summaries.  Resolve
        # the tenant owner for admin views so an administrator can maintain a
        # customer's server-to-FTP/database link without receiving secrets.
        integration_profiles = []
        try:
            integration_owner_id = int(user.id)
            if user.is_admin:
                owner_access = db.scalar(
                    select(GroupAccess).where(GroupAccess.group_id == str(group_id))
                )
                if owner_access is not None:
                    integration_owner_id = int(owner_access.user_id)
            integration_profiles = list_integration_profiles(
                db, integration_owner_id, group_id=group_id
            )
        except Exception:
            db.rollback()
            logger.warning(
                "Unable to load integration profile choices group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
                exc_info=True,
            )
        return templates.TemplateResponse(
            request=request,
            name="group.html",
            context=_template_context(
                request,
                user,
                group_id=group_id,
                group_prefix="/admin/groups" if user.is_admin else "/user/groups",
                access=access,
                can_edit=can_edit,
                edit_block_reason=edit_block_reason,
                can_view_console=can_view_console,
                console_enabled=console_settings["enabled"],
                console_events=console_events,
                console_pagination=console_pagination,
                console_filter=console_filter,
                console_user_id=console_user_id,
                console_user_name=console_user_name,
                console_start=console_start,
                console_end=console_end,
                console_filter_error=console_filter_error,
                console_keywords=console_settings["keywords"] or DEFAULT_CONSOLE_KEYWORDS,
                clone_sources=clone_sources,
                game_status=game_status,
                game_license_verified=game_license_verified,
                game_websocket_config=public_game_websocket_config,
                game_websocket_config_ready=game_websocket_config_ready,
                game_websocket_ready=game_websocket_ready,
                game_transport=game_transport,
                websocket_access_status=websocket_access_status,
                websocket_site_settings={
                    "enabled": bool(websocket_site_settings.get("enabled")),
                    "server_url": str(websocket_site_settings.get("server_url", ""))[:128],
                    "socketio_path": str(websocket_site_settings.get("socketio_path", "/socket.io")),
                    "bot_type": str(websocket_site_settings.get("bot_type", "bot_main")),
                },
                websocket_license_verified=websocket_license_verified,
                integration_profiles=integration_profiles,
                **view,
            ),
        )

    @app.post("/user/groups/{group_id}/clone")
    @app.post("/admin/groups/{group_id}/clone")
    async def clone_group(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        target_access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(target_access, RedirectResponse):
            return target_access
        form = await _validated_form(request)
        source_group_id = str(form.get("source_group_id", "")).strip()
        try:
            source_group_id = validate_group_id(source_group_id)
            if source_group_id == str(group_id):
                raise PortalError("源群和目标群不能相同")
            source_access = _owned_group_or_redirect(
                request, db, user, source_group_id, False
            )
            if isinstance(source_access, RedirectResponse):
                return source_access
            clone_group_config(source_group_id, group_id, db, user.id)
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "group_clone",
                f"从群 {source_group_id} 复制群管配置",
                {"source_group_id": source_group_id},
            )
            _flash(
                request,
                f"已从群 {source_group_id} 复制群管配置，等待 Windows 同步",
                "success",
            )
        except (PortalError, ValueError) as exc:
            db.rollback()
            _flash(request, str(exc) or "群管配置复制失败", "error")
        return _redirect(_group_path(user, group_id, "#clone-config"))

    @app.post("/user/groups/{group_id}/sync-refresh")
    @app.post("/admin/groups/{group_id}/sync-refresh")
    async def request_group_sync_refresh(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        """Persist a pull-only refresh request for one Windows-owned group.

        The web portal must never attempt to open a connection back to the
        customer's Windows computer.  Instead, the next authenticated
        Windows heartbeat receives this durable hint and uploads the current
        local projection for this group.
        """

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id)
        if isinstance(access, RedirectResponse):
            return access
        await _validated_form(request)

        if settings.data_mode != "remote":
            _flash(request, "当前网页与本地插件直连，无需请求远程同步。", "info")
            return _redirect(_group_path(user, group_id, "#sync-flow-title"))

        request_meta_ip = request.headers.get("x-forwarded-for", "")
        if request_meta_ip:
            request_meta_ip = request_meta_ip.split(",", 1)[0].strip()
        elif request.client is not None:
            request_meta_ip = request.client.host or ""
        try:
            result = await run_in_threadpool(
                _process_group_refresh_request,
                group_id,
                int(user.id),
                request_meta_ip,
                request.headers.get("user-agent", ""),
            )
        except SQLAlchemyError:
            # A short MySQL lock/pool failure should be visible as a retryable
            # flash on the same group page, not as a blank reverse-proxy 502.
            # The durable request was not committed in this case; the user
            # can press the button again after the current heartbeat releases
            # its lock.
            logger.warning(
                "Unable to queue group sync refresh group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
                exc_info=True,
            )
            _flash(
                request,
                "当前同步请求暂时遇到数据库锁或连接繁忙，请稍后点击刷新状态后重试。",
                "warning",
            )
            return _redirect(_group_path(user, group_id, "#sync-flow-title"))
        if bool(result.get("already_pending")):
            _flash(
                request,
                "本群已经在等待 Windows 补传配置，无需重复请求。",
                "info",
            )
        else:
            requested = int(result.get("requested", 0) or 0)
            _flash(
                request,
                (
                    "已请求 Windows 机器人补传本群配置；电脑联网后会自动完成。"
                    if requested
                    else "本群补传请求已登记，等待 Windows 电脑联网。"
                ),
                "success",
            )
        return _redirect(_group_path(user, group_id, "#sync-flow-title"))

    @app.get("/user/groups/{group_id}/sync-status")
    @app.get("/admin/groups/{group_id}/sync-status")
    def group_sync_status(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        """Return a small no-store status snapshot for the group page.

        This endpoint is read-only.  It lets the browser notice the next
        Windows heartbeat after a durable refresh request without reloading a
        page that may contain an unsaved feature draft.  It never asks Windows
        to connect and never changes the queued refresh flag.
        """

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id)
        if isinstance(access, RedirectResponse):
            return access

        if settings.data_mode == "remote":
            snapshot = remote_store.get_snapshot(db, group_id)
            try:
                offline_after_seconds = Settings.from_env().offline_after_seconds
            except Exception:
                offline_after_seconds = 180
            sync = _safe_sync_state(snapshot, offline_after_seconds)
        else:
            sync = {
                "online": True,
                "pending": False,
                "status": "local",
                "connection_state": "local",
                "badge": "本地模式",
                "text": "网页直接读取本地配置",
                "display_text": "网页直接读取本地配置",
                "detail": "当前不需要 Windows 远程心跳。",
                "revision": 0,
                "applied_revision": 0,
                "last_seen_text": "本地配置可用",
                "saved_at": "",
                "saved_by": 0,
                "refresh_requested": False,
            }

        # ``sync_state`` also contains a datetime object for internal callers;
        # expose only the stable template fields so this endpoint remains JSON
        # serializable across old and new portal schemas.
        public_keys = (
            "online",
            "pending",
            "status",
            "connection_state",
            "badge",
            "text",
            "display_text",
            "detail",
            "revision",
            "applied_revision",
            "last_seen_text",
            "saved_at",
            "saved_by",
            "refresh_requested",
        )
        public_sync = {key: sync.get(key) for key in public_keys}
        return JSONResponse(
            {
                "group_id": str(group_id),
                "sync": public_sync,
                "server_time": utcnow().isoformat(timespec="seconds"),
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/user/groups/{group_id}/runtime-status")
    @app.get("/admin/groups/{group_id}/runtime-status")
    def group_runtime_status(
        group_id: str,
        request: Request,
        version: str = "",
        db: Session = Depends(get_db),
    ):
        """Return Windows runtime data for in-place browser updates.

        ``version`` is the last ``runtime_updated_at`` seen by the browser.
        Returning a small unchanged response avoids reloading the page or
        transferring a large member directory when nothing changed.
        """

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id)
        if isinstance(access, RedirectResponse):
            return access

        snapshot = None
        if settings.data_mode == "remote":
            snapshot = remote_store.get_snapshot(db, group_id)
            raw_runtime = getattr(snapshot, "runtime_json", None) if snapshot is not None else None
            updated_at = getattr(snapshot, "runtime_updated_at", None) if snapshot is not None else None
        else:
            try:
                view = get_group_view(group_id, db, include_members=False)
                runtime = view.get("runtime_panels", {}) if isinstance(view, dict) else {}
            except Exception:
                db.rollback()
                logger.exception("Unable to load local runtime status group_id=%s", group_id)
                runtime = {}
            updated_at = None
            raw_runtime = runtime

        runtime_version = ""
        if updated_at is not None:
            try:
                # Several group messages may arrive during the same second.
                # A seconds-only marker made the browser miss the later writes
                # and left the detail panel behind the visible message count.
                # This is an opaque polling token, so microseconds are safe.
                runtime_version = updated_at.isoformat(timespec="microseconds")
            except AttributeError:
                runtime_version = str(updated_at)[:64]
        if version and runtime_version and version == runtime_version:
            return JSONResponse(
                {"group_id": str(group_id), "unchanged": True, "version": runtime_version},
                headers={"Cache-Control": "private, no-store"},
            )
        runtime = _normalize_runtime_panels(raw_runtime, include_members=False)
        return JSONResponse(
            {
                "group_id": str(group_id),
                "unchanged": False,
                "version": runtime_version,
                "runtime": runtime,
                "server_time": utcnow().isoformat(timespec="seconds"),
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/user/groups/{group_id}/members")
    @app.get("/admin/groups/{group_id}/members")
    def group_member_directory(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Return the member directory only when the members panel is opened."""

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id)
        if isinstance(access, RedirectResponse):
            return access
        try:
            panel = get_group_member_directory(group_id, db)
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to load member directory group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            panel = {"available": False, "count": 0, "valid_count": 0, "members": []}
        return JSONResponse(
            {"group_id": str(group_id), "members": panel},
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/user/groups/{group_id}/messages")
    @app.get("/admin/groups/{group_id}/messages")
    def group_message_timeline(
        group_id: str,
        request: Request,
        limit: int = 5000,
        db: Session = Depends(get_db),
    ):
        """Return the retained QQ-like group timeline without a page reload."""

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id)
        if isinstance(access, RedirectResponse):
            return access
        try:
            row_limit = max(1, min(10_000, int(limit)))
        except (TypeError, ValueError, OverflowError):
            row_limit = 5_000
        if settings.data_mode == "remote":
            records = remote_store.list_group_message_history(
                db, str(group_id), None, limit=row_limit
            )
        else:
            try:
                view = get_group_view(group_id, db, include_members=False)
                raw_runtime = view.get("runtime_panels", {}) if isinstance(view, dict) else {}
                raw_cache = raw_runtime.get("message_cache", {}) if isinstance(raw_runtime, dict) else {}
                raw_rows = raw_cache.get("records", []) if isinstance(raw_cache, dict) else []
                records = [dict(row) for row in raw_rows if isinstance(row, dict)]
            except Exception:
                db.rollback()
                logger.warning("Unable to load group message timeline group_id=%s", group_id, exc_info=True)
                records = []
        normalized = _normalize_runtime_panels(
            {"message_cache": {"available": True, "records": records}}
        )
        rows = normalized.get("message_cache", {}).get("records", [])
        return JSONResponse(
            {
                "group_id": str(group_id),
                "count": len(rows),
                "records": rows,
                "retained_only": True,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/user/groups/{group_id}/members/{member_id}/messages")
    @app.get("/admin/groups/{group_id}/members/{member_id}/messages")
    def group_member_message_history(
        group_id: str,
        member_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Return complete retained history for the selected QQ member."""

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id)
        if isinstance(access, RedirectResponse):
            return access
        normalized_member = str(member_id or "").strip()
        if not re.fullmatch(r"\d{5,20}", normalized_member):
            return JSONResponse({"detail": "成员 QQ 号格式无效"}, status_code=400)
        replay: dict[str, object] = {}
        if settings.data_mode == "remote":
            records = remote_store.list_group_message_history(
                db, str(group_id), normalized_member
            )
            # Keep the same request alive while the detail drawer polls.  A
            # Windows heartbeat can arrive after the first browser request;
            # dropping the flag on the next poll made the UI stop waiting
            # before the local cache replay had a chance to run.
            replay = remote_store.request_group_message_history_replay(
                db,
                str(group_id),
                normalized_member,
                available_count=len(records),
            )
            if bool(replay.get("requested")):
                db.commit()
        else:
            try:
                view = get_group_view(group_id, db, include_members=False)
                raw_runtime = view.get("runtime_panels", {}) if isinstance(view, dict) else {}
                raw_cache = raw_runtime.get("message_cache", {}) if isinstance(raw_runtime, dict) else {}
                raw_rows = raw_cache.get("records", []) if isinstance(raw_cache, dict) else []
                records = [
                    row for row in raw_rows
                    if isinstance(row, dict) and str(row.get("user_id", "")).strip() == normalized_member
                ]
            except Exception:
                db.rollback()
                logger.warning(
                    "Unable to load local member message history group_id=%s user_id=%s",
                    group_id,
                    normalized_member,
                    exc_info=True,
                )
                records = []
        normalized = _normalize_runtime_panels(
            {"message_cache": {"available": True, "records": records}}
        )
        rows = normalized.get("message_cache", {}).get("records", [])
        return JSONResponse(
            {
                "group_id": str(group_id),
                "user_id": normalized_member,
                "count": len(rows),
                "records": rows,
                "sync_pending": bool(replay.get("requested", False)),
                "expected_count": int(replay.get("expected_count", len(rows)) or len(rows)),
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @app.post("/user/groups/{group_id}/power")
    @app.post("/admin/groups/{group_id}/power")
    async def group_power(group_id: str, request: Request, db: Session = Depends(get_db)):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        powered_on = str(form.get("power", "")) == "on"
        set_power(group_id, powered_on, db, user.id)
        _snapshot_and_log(
            request,
            db,
            user,
            group_id,
            "power",
            "网页开机" if powered_on else "网页关机",
            {"power_status": "on" if powered_on else "off"},
        )
        _flash(request, "已开机" if powered_on else "已关机", "success")
        return _redirect(_group_path(user, group_id))

    @app.post("/user/groups/{group_id}/features/scum/reset")
    @app.post("/admin/groups/{group_id}/features/scum/reset")
    async def reset_scum_commands(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Clear this group's SCUM command overrides and restore defaults."""

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        await _validated_form(request)
        try:
            values = get_feature_values(group_id, "scum", db)
            values.update(
                {
                    "keyword_command": "查询",
                    "online_command": "查询在线",
                }
            )
            apply_feature_values(group_id, "scum", values, db, user.id)
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "feature_reset",
                "清除 SCUM 自定义覆盖并恢复默认",
                {
                    "feature": "scum",
                    "deleted_fields": [
                        "keyword_command",
                        "online_command",
                    ],
                },
            )
            _flash(request, "已清除本群 SCUM 自定义覆盖，恢复为默认指令", "success")
        except (PortalError, ValueError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to reset SCUM commands group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "删除 SCUM 自定义指令失败，请稍后重试", "error")
        return _redirect(_group_path(user, group_id, "#feature-scum"))

    @app.post("/user/groups/{group_id}/features/game/reset")
    @app.post("/admin/groups/{group_id}/features/game/reset")
    async def reset_game_defaults(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Restore starter game commands without deleting bindings or servers."""

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        await _validated_form(request)
        try:
            reset_game_configuration(group_id, db, user.id)
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "feature_reset",
                "恢复默认游戏配置",
                {
                    "feature": "game",
                    "reset": ["game_commands", "stats_game_policy"],
                    "preserved": ["steam_bindings", "signin_records", "game_servers"],
                },
            )
            _flash(request, "已恢复默认游戏配置，Windows 下次同步后生效", "success")
        except (PortalError, ValueError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to reset game configuration group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "恢复默认游戏配置失败，请稍后重试", "error")
        return _redirect(_group_path(user, group_id, "#feature-game_commands"))

    @app.post("/user/groups/{group_id}/game-server-profile")
    @app.post("/admin/groups/{group_id}/game-server-profile")
    async def update_game_server_profile(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Add, edit, remove or select one group's game server connection."""

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        operation = str(form.get("operation", "save") or "save").strip().lower()
        try:
            apply_game_server_profile_action(
                group_id,
                operation,
                profile_id=str(form.get("profile_id", "") or "").strip(),
                name=str(form.get("name", "") or "").strip(),
                host=str(form.get("host", "") or "").strip(),
                port=str(form.get("port", "") or "").strip(),
                executor_type=str(form.get("type", "plugin_websocket") or "plugin_websocket").strip(),
                websocket_url=str(form.get("websocket_url", "") or "").strip(),
                api_key=str(form.get("api_key", "") or ""),
                timeout_seconds=str(form.get("timeout_seconds", "") or "").strip(),
                profile_enabled=_form_checkbox(form, "profile_enabled", True),
                integration_profile_id=str(
                    form.get("integration_profile_id", "") or ""
                ).strip(),
                enabled=_form_checkbox(form, "enabled"),
                active_profile_id=str(form.get("active_profile_id", "") or "").strip(),
                db=db,
                updated_by=user.id,
            )
            labels = {
                "save": "游戏服务器已保存",
                "delete": "游戏服务器已删除",
                "activate": "当前游戏服务器已切换",
                "settings": "游戏服务器设置已保存",
            }
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "feature_update",
                labels.get(operation, "游戏服务器设置已保存"),
                {
                    "feature": "game_servers",
                    "operation": operation,
                    "profile_id": str(form.get("profile_id", "") or "")[:48],
                },
            )
            _flash(request, labels.get(operation, "游戏服务器设置已保存"), "success")
        except (PortalError, ValueError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to update game server profile group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "游戏服务器设置保存失败，请稍后重试。", "error")
        return _redirect(_group_path(user, group_id, "#feature-game_servers"))

    @app.post("/user/groups/{group_id}/commands")
    @app.post("/admin/groups/{group_id}/commands")
    async def update_group_command(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Save one command name without exposing or rewriting feature settings."""
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        feature_key = str(form.get("feature_key", "") or "").strip()
        command_key = str(form.get("command_key", "") or "").strip()
        command_kind = str(form.get("command_kind", "field") or "field").strip()
        try:
            spec = FEATURE_BY_KEY.get(feature_key)
            if spec is None:
                raise PortalError("未知功能模块")
            current = get_feature_values(group_id, feature_key, db=db)
            values = {
                field.key: current.get(field.key, field.default)
                for field in spec.fields
            }
            if command_kind == "game":
                try:
                    command_index = int(str(form.get("command_index", "-1")))
                except (TypeError, ValueError):
                    raise PortalError("游戏指令编号无效") from None
                commands = deepcopy(values.get("commands", []))
                if not isinstance(commands, list) or not 0 <= command_index < len(commands):
                    raise PortalError("未找到该游戏指令，可能已被其他页面修改")
                trigger = str(form.get("value", "") or "").strip().lstrip("#")
                if not trigger or any(character.isspace() for character in trigger):
                    raise PortalError("游戏指令名称不能为空且不能包含空格")
                command = commands[command_index]
                if not isinstance(command, dict):
                    raise PortalError("游戏指令格式无效")
                command["trigger"] = trigger
                values["commands"] = commands
            elif command_kind in {"signin_profile", "lottery_campaign"}:
                collection_key = (
                    "signin_profiles"
                    if command_kind == "signin_profile"
                    else "campaigns"
                )
                target_id = str(form.get("target_id", "") or "").strip()
                collection = deepcopy(values.get(collection_key, []))
                if not isinstance(collection, list):
                    raise PortalError("指令列表格式无效")
                target = next(
                    (
                        item for item in collection
                        if isinstance(item, dict)
                        and str(item.get("id", "") or "").strip() == target_id
                    ),
                    None,
                )
                if target is None:
                    raise PortalError("未找到该指令方案，可能已被其他页面修改")
                command = str(form.get("value", "") or "").strip().lstrip("/")
                if not command or any(character.isspace() for character in command):
                    raise PortalError("指令名称不能为空且不能包含空格")
                target["command" if command_kind == "signin_profile" else "keyword"] = command
                values[collection_key] = collection
            else:
                allowed_command_keys = {
                    "signin_command", "bind_command",
                    "keyword_command", "online_command",
                    "time_command", "like_command", "music_command", "video_command",
                    "dice_command", "rps_command", "random_command", "coin_command",
                    "fortune_command", "luck_command", "answer_command", "keyword",
                    "help_command",
                }
                field = next(
                    (candidate for candidate in spec.fields if candidate.key == command_key),
                    None,
                )
                if field is None or command_key not in allowed_command_keys:
                    raise PortalError("该功能没有可编辑的指令名称")
                values[command_key] = parse_field_value(
                    field, form.get("value"), True
                )
            apply_feature_values(group_id, feature_key, values, db, user.id)
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "feature_update",
                f"更新{spec.label}指令名称",
                {
                    "feature": feature_key,
                    "command": command_key or command_kind,
                },
            )
            _flash(request, "指令名称已保存，等待 Windows 同步", "success")
        except (PortalError, ValueError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to update group command group_id=%s feature_key=%s request_id=%s",
                group_id,
                feature_key,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "指令名称保存失败，请稍后重试。", "error")
        return _redirect(_group_path(user, group_id, f"#command-editor-{quote(feature_key)}"))

    @app.post("/user/groups/{group_id}/features/{feature_key}")
    @app.post("/admin/groups/{group_id}/features/{feature_key}")
    async def update_feature(
        group_id: str,
        feature_key: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        spec = FEATURE_BY_KEY.get(feature_key)
        if spec is None:
            raise PortalError("未知功能模块")
        try:
            # Entertainment is rendered as several independent panels.  Each
            # panel submits only its own controls; merge those controls with
            # the current feature before validation so saving dice settings
            # cannot reset music, likes, or another panel.
            section = str(form.get("entertainment_section", "") or "").strip()
            entertainment_groups = {
                "overview": {"enabled", "cooldown"},
                "time": {"time_enabled", "time_command"},
                "dice": {"dice_enabled", "dice_command", "max_sides"},
                "rps": {"rps_enabled", "rps_command"},
                "random": {"random_enabled", "random_command", "max_options"},
                "coin": {"coin_enabled", "coin_command"},
                "fortune": {"fortune_enabled", "fortune_command"},
                "luck": {"luck_enabled", "luck_command"},
                "answer": {"answer_enabled", "answer_command"},
                "like": {"like_enabled", "like_command", "like_daily_limit"},
                "music": {"music_enabled", "music_sources", "music_default_source", "music_command", "music_selection_timeout"},
                "video": {"video_enabled", "video_command"},
            }
            partial_entertainment = feature_key == "entertainment" and section in entertainment_groups
            if partial_entertainment:
                current = get_feature_values(group_id, feature_key, db=db)
                submitted = entertainment_groups[section]
                values = {}
                for field in spec.fields:
                    present = field.key in submitted
                    raw = form.getlist(field.key) if field.kind == "multichoice" else form.get(field.key)
                    if present:
                        values[field.key] = parse_field_value(field, raw, field.key in form)
                    else:
                        values[field.key] = current.get(field.key, field.default)
            else:
                values = {
                    field.key: parse_field_value(
                        field,
                        form.getlist(field.key) if field.kind == "multichoice" else form.get(field.key),
                        field.key in form,
                    )
                    for field in spec.fields
                }
            apply_feature_values(group_id, feature_key, values, db, user.id)
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "feature_update",
                f"更新{spec.label}配置",
                {feature_key: values},
            )
            _flash(request, f"{spec.label}配置已保存", "success")
        except PortalError as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except ValueError as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to save group feature group_id=%s feature_key=%s request_id=%s",
                group_id,
                feature_key,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "分群配置保存失败，请稍后重试；Windows 会在下一次同步时继续处理。", "error")
        redirect_section = (
            f"#entertainment-{quote(section)}"
            if partial_entertainment and section != "overview"
            else f"#feature-{quote(feature_key)}"
        )
        return _redirect(_group_path(user, group_id, redirect_section))

    @app.post("/user/groups/{group_id}/member-action")
    @app.post("/admin/groups/{group_id}/member-action")
    async def queue_group_member_action(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        """Queue one bounded member operation for the Windows execution client."""
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        action = str(form.get("action", "")).strip().lower()
        allowed = {
            "refresh_members",
            "mute_member",
            "unmute_member",
            "kick_member",
            "blacklist_member",
            "unblacklist_member",
            "delete_blacklist_record",
            "unblacklist_global",
            "add_global_blacklist",
            "remove_global_blacklist",
            "unbind_steam_id",
            "set_member_content",
        }
        try:
            if action not in allowed:
                raise PortalError("未知群成员操作")
            if action in {"add_global_blacklist", "remove_global_blacklist"} and not user.is_admin:
                raise PortalError("全局黑名单操作仅限后台管理员")
            raw_user_ids = []
            getlist = getattr(form, "getlist", None)
            if callable(getlist):
                raw_user_ids.extend(getlist("user_id"))
                raw_user_ids.extend(getlist("user_ids"))
            if not raw_user_ids:
                raw_user_ids.append(form.get("user_id", ""))
            user_ids: list[str] = []
            for raw_user_id in raw_user_ids:
                normalized_user_id = str(raw_user_id or "").strip()
                if not normalized_user_id or normalized_user_id in user_ids:
                    continue
                if not normalized_user_id.isdigit() or not 5 <= len(normalized_user_id) <= 20:
                    raise PortalError("请选择有效的群成员")
                user_ids.append(normalized_user_id)
            if action != "refresh_members" and not user_ids:
                raise PortalError("请选择至少一名群成员")
            if action == "add_global_blacklist" and len(user_ids) != 1:
                raise PortalError("添加全局黑名单每次只能选择一名用户")
            if len(user_ids) > 200:
                raise PortalError("单次最多选择 200 名成员")
            try:
                duration = max(0, min(2592000, int(form.get("duration", 0) or 0)))
            except (TypeError, ValueError, OverflowError):
                raise PortalError("禁言时长必须是 0-2592000 秒")
            content = str(form.get("content", "")).replace("\r", " ").replace("\n", " ").strip()[:60]
            if action == "set_member_content" and not content:
                raise PortalError("群名片不能为空")
            global_reason = str(form.get("reason", "")).replace("\r", " ").replace("\n", " ").strip()[:240]
            global_ban_type = str(form.get("ban_type", "permanent") or "permanent").strip().lower()
            if global_ban_type not in {"permanent", "temporary"}:
                raise PortalError("全局黑名单类型无效")
            if action == "add_global_blacklist" and global_ban_type == "temporary" and duration <= 0:
                raise PortalError("临时全局黑名单必须设置有效时长")
            request_id = uuid4().hex
            item = {
                "action": action,
                "request_id": request_id,
                "created_at": int(unix_time()),
            }
            if user_ids:
                item["user_id"] = user_ids[0]
                if len(user_ids) > 1:
                    item["user_ids"] = user_ids
            if action in {"mute_member", "unmute_member"}:
                # OneBot uses duration=0 as the unmute operation. Never carry
                # the form's mute-duration default into an unmute request.
                item["duration"] = 0 if action == "unmute_member" else duration
            if action == "add_global_blacklist":
                item["reason"] = global_reason
                item["ban_type"] = global_ban_type
                item["duration"] = duration if global_ban_type == "temporary" else 0
            if content:
                item["content"] = content
            if settings.data_mode != "remote":
                raise PortalError("成员网页操作需要 Linux 远程同步模式")

            def mutate(config):
                actions = config.get("portal_actions", [])
                actions = list(actions) if isinstance(actions, list) else []
                actions = [entry for entry in actions if isinstance(entry, dict) and str(entry.get("request_id", "")) != request_id]
                actions.append(item)
                # One batch action carries all selected IDs, so keep enough
                # room for several requests without splitting one selection
                # across multiple sync revisions.
                config["portal_actions"] = actions[-32:]
                return config

            remote_store.mutate_web_config(db, group_id, user.id, mutate)
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "member_action",
                f"提交群成员操作 {action}",
                {
                    "action": action,
                    "user_id": user_ids[0] if len(user_ids) == 1 else (f"{len(user_ids)}人" if user_ids else "all"),
                    "user_ids": user_ids,
                    "request_id": request_id,
                },
            )
            _flash(request, "操作已提交，Windows 在线后会执行并刷新成员列表。", "success")
        except (PortalError, ValueError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return_section = str(form.get("return_section", "")).strip().lower()
        redirect_anchor = "#blacklist-data" if return_section == "blacklist-data" else "#member-management"
        return _redirect(_group_path(user, group_id, redirect_anchor))

    @app.post("/user/groups/{group_id}/game-server-test")
    @app.post("/admin/groups/{group_id}/game-server-test")
    async def test_group_game_server(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        """Ask the Windows worker owning this group to test the selected transport."""
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, False)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        profile_id = str(form.get("profile_id", "") or "").strip()[:48]
        return_to_integrations = str(form.get("return_to", "")).strip() == "integrations"
        result_redirect = (
            f"{_integration_group_prefix(user, group_id)}/integrations?profile_id={quote(profile_id, safe='')}"
            if return_to_integrations
            else _group_path(user, group_id, "#feature-game_servers")
        )
        if settings.data_mode != "remote":
            _flash(request, "当前网页不是远程模式，无法通过 Linux WebSocket 检测 Windows 游戏连接。", "warning")
            return _redirect(result_redirect)
        snapshot = remote_store.get_snapshot(db, group_id)
        client_id = str(getattr(snapshot, "client_id", "") or "").strip()
        if not client_id:
            _flash(request, "该群暂时没有在线 Windows 执行端，请先启动 Windows 插件并等待同步。", "warning")
            return _redirect(result_redirect)
        try:
            result = await app.state.game_ws.call(
                client_id,
                "test_game_connection",
                {"group_id": str(group_id), "profile_id": profile_id},
                timeout=app.state.game_ws.call_timeout,
            )
            if isinstance(result, dict) and result.get("success"):
                _flash(request, f"Windows 已确认插件 WebSocket 连接成功：{result.get('profile_id', profile_id or '全局配置')}", "success")
            else:
                code = str(result.get("code", "game_connection_test_failed")) if isinstance(result, dict) else "game_connection_test_failed"
                message = str(result.get("message", "Windows 游戏连接检测失败")) if isinstance(result, dict) else "Windows 游戏连接检测失败"
                _flash(request, f"游戏连接检测失败（{code}）：{message[:180]}", "error")
        except GameWebSocketError as exc:
            _flash(request, f"Windows 游戏连接检测未完成：{exc.detail[:180]}", "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to test game server group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "Windows 游戏连接检测失败，请查看 Windows CMD 和网页同步状态。", "error")
        return _redirect(result_redirect)

    @app.post("/user/groups/{group_id}/signin-profiles")
    @app.post("/admin/groups/{group_id}/signin-profiles")
    async def update_signin_profile(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        """Maintain one independent sign-in scheme without editing JSON."""

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        operation_values = form.getlist("operation") if hasattr(form, "getlist") else []
        operation = str((operation_values[-1] if operation_values else form.get("operation", "")) or "").strip().lower()
        try:
            apply_signin_profile_action(
                group_id,
                operation,
                profile_id=str(form.get("profile_id", "") or "").strip(),
                name=str(form.get("name", "") or "").strip(),
                command=str(form.get("command", "") or "").strip(),
                mode=str(form.get("mode", "basic") or "basic"),
                schedule=str(form.get("schedule", "daily") or "daily"),
                enabled=(str(form.get("enabled", "") or "").lower() in {"1", "true", "on", "yes"}),
                base_reward=form.get("base_reward", ""),
                reward_3_days=form.get("reward_3_days", ""),
                reward_7_days=form.get("reward_7_days", ""),
                reward_14_days=form.get("reward_14_days", ""),
                reward_30_days=form.get("reward_30_days", ""),
                signin_message=str(form.get("signin_message", "") or ""),
                steam_reward_mode=str(form.get("steam_reward_mode", "database") or "database"),
                steam_reward_operation=str(form.get("steam_reward_operation", "execute_game_command") or "execute_game_command"),
                steam_reward_command=str(form.get("steam_reward_command", "") or ""),
                steam_reward_failure_message=str(form.get("steam_reward_failure_message", "") or ""),
                db=db,
                updated_by=user.id,
            )
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "signin_profile_update",
                {"add": "新增签到方案", "update": "更新签到方案", "delete": "删除签到方案"}.get(
                    operation, "更新签到方案"
                ),
                {"operation": operation, "profile_id": str(form.get("profile_id", ""))[:64]},
            )
            db.commit()
            _flash(request, "签到方案已保存，等待 Windows 同步", "success")
        except (PortalError, ValueError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to save sign-in profile group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "签到方案保存失败，请稍后重试", "error")
        return _redirect(_group_path(user, group_id, "#signin-profiles"))

    @app.post("/user/groups/{group_id}/lottery-campaigns")
    @app.post("/admin/groups/{group_id}/lottery-campaigns")
    async def update_lottery_campaign(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        """Maintain one named lottery activity without editing JSON."""

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        operation_values = form.getlist("operation") if hasattr(form, "getlist") else []
        operation = str((operation_values[-1] if operation_values else form.get("operation", "")) or "").strip().lower()
        submitted_name = str(
            form.get("name", "") or form.get("campaign_name", "") or ""
        ).strip()
        try:
            apply_lottery_campaign_action(
                group_id,
                operation,
                campaign_id=str(form.get("campaign_id", "") or "").strip(),
                name=submitted_name,
                keyword=str(form.get("keyword", "") or "").strip(),
                enabled=(str(form.get("enabled", "") or "").lower() in {"1", "true", "on", "yes"}),
                max_draws_per_user=form.get("max_draws_per_user", 0),
                cooldown=form.get("cooldown", 0),
                success_message=str(form.get("success_message", "") or ""),
                empty_message=str(form.get("empty_message", "") or ""),
                db=db,
                updated_by=user.id,
            )
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "lottery_campaign_update",
                {"add": "新增抽奖活动", "update": "更新抽奖活动", "delete": "删除抽奖活动"}.get(
                    operation, "更新抽奖活动"
                ),
                {"operation": operation, "campaign_id": str(form.get("campaign_id", ""))[:64]},
            )
            db.commit()
            _flash(request, "抽奖活动已保存，等待 Windows 同步", "success")
        except (PortalError, ValueError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to save lottery campaign group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "抽奖活动保存失败，请稍后重试", "error")
        return _redirect(_group_path(user, group_id, "#lottery-campaigns"))

    @app.post("/user/groups/{group_id}/lottery-awards")
    @app.post("/admin/groups/{group_id}/lottery-awards")
    async def update_lottery_award(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        """Maintain one group's lottery award list without a JSON-only UI."""

        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        operation_values = form.getlist("operation") if hasattr(form, "getlist") else []
        operation = str((operation_values[-1] if operation_values else form.get("operation", "")) or "").strip().lower()
        try:
            result = apply_lottery_award_action(
                group_id,
                operation,
                award_id=str(form.get("award_id", "") or "").strip(),
                campaign_id=str(form.get("campaign_id", "") or "").strip(),
                name=str(form.get("name", "") or "").strip(),
                probability=form.get("probability", "0"),
                stock=form.get("stock", "0"),
                enabled=(
                    str(form.get("enabled", "") or "").lower()
                    in {"1", "true", "on", "yes"}
                ),
                db=db,
                updated_by=user.id,
            )
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "lottery_award_update",
                {"add": "新增抽奖奖项", "update": "更新抽奖奖项", "delete": "删除抽奖奖项"}.get(
                    operation, "更新抽奖奖项"
                ),
                {
                    "operation": operation,
                    "award_id": str(form.get("award_id", "") or "")[:64],
                    "award_name": str(form.get("name", "") or "")[:100],
                    "revision_configured": bool(result),
                },
            )
            db.commit()
            _flash(request, "抽奖奖项已保存，等待 Windows 同步", "success")
        except (PortalError, ValueError) as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        except Exception:
            db.rollback()
            logger.exception(
                "Unable to save lottery award group_id=%s request_id=%s",
                group_id,
                getattr(request.state, "request_id", ""),
            )
            _flash(request, "抽奖奖项保存失败，请稍后重试", "error")
        award_anchor = (
            "#lottery-campaigns"
            if str(form.get("campaign_id", "") or "").strip()
            else "#lottery-awards"
        )
        return _redirect(_group_path(user, group_id, award_anchor))

    @app.post("/user/groups/{group_id}/timed-messages")
    @app.post("/admin/groups/{group_id}/timed-messages")
    async def create_timed_message(
        group_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        try:
            hourly = str(form.get("hourly", "") or "").strip().lower() in {"1", "true", "on", "yes"}
            action = str(form.get("action", "message") or "message")
            cron_expr = "0 * * * *" if hourly or action == "hourly_time" else str(form.get("cron_expr", "") or "")
            task_name = str(form.get("name", "") or "").strip() or ("整点报时" if action == "hourly_time" else ("整点播报" if hourly else ""))
            result = add_timed_task(
                group_id,
                {
                    "name": task_name,
                    "cron_expr": cron_expr,
                    "run_at": form.get("run_at", ""),
                    "message": form.get("message", ""),
                    "action": action,
                    "at_all": "at_all" in form,
                },
                str(user.id),
                db,
            )
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "timed_message_add",
                "添加定时任务",
                {"task_id": result["id"]},
            )
            _flash(request, "定时任务已添加", "success")
        except PortalError as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(_group_path(user, group_id, "#timed-messages"))

    @app.post("/user/groups/{group_id}/timed-messages/{task_id}/edit")
    @app.post("/admin/groups/{group_id}/timed-messages/{task_id}/edit")
    async def update_timed_message_route(
        group_id: str, task_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        try:
            hourly = str(form.get("hourly", "") or "").strip().lower() in {
                "1", "true", "on", "yes"
            }
            action = str(form.get("action", "message") or "message")
            cron_expr = "0 * * * *" if hourly or action == "hourly_time" else str(form.get("cron_expr", "") or "")
            result = update_timed_task(
                group_id,
                task_id,
                {
                    "name": str(form.get("name", "") or "").strip(),
                    "cron_expr": cron_expr,
                    "run_at": form.get("run_at", ""),
                    "message": form.get("message", ""),
                    "action": action,
                    "at_all": "at_all" in form,
                },
                str(user.id),
                db,
            )
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "timed_message_update",
                "编辑定时任务",
                {"task_id": str(result.get("id", task_id))},
            )
            _flash(request, "定时任务已保存，等待 Windows 同步", "success")
        except PortalError as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(_group_path(user, group_id, "#timed-messages"))

    @app.post("/user/groups/{group_id}/timed-messages/{task_id}/toggle")
    @app.post("/admin/groups/{group_id}/timed-messages/{task_id}/toggle")
    async def toggle_timed_message(
        group_id: str, task_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        try:
            raw_enabled = form.get("enabled")
            desired_enabled = None
            if raw_enabled is not None:
                desired_enabled = str(raw_enabled).strip().lower() in {
                    "1",
                    "true",
                    "on",
                    "yes",
                }
            enabled = toggle_timed_task(
                group_id, task_id, db, user.id, desired_enabled
            )
            _snapshot_and_log(request, db, user, group_id, "timed_message_toggle", "切换定时任务状态", {"task_id": task_id, "enabled": enabled})
        except PortalError as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(_group_path(user, group_id, "#timed-messages"))

    @app.post("/user/groups/{group_id}/timed-messages/{task_id}/delete")
    @app.post("/admin/groups/{group_id}/timed-messages/{task_id}/delete")
    async def remove_timed_message(
        group_id: str, task_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        await _validated_form(request)
        try:
            delete_timed_task(group_id, task_id, db, user.id)
            _snapshot_and_log(request, db, user, group_id, "timed_message_delete", "删除定时任务", {"task_id": task_id})
            _flash(request, "定时任务已删除", "success")
        except PortalError as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(_group_path(user, group_id, "#timed-messages"))

    @app.post("/user/groups/{group_id}/replies")
    @app.post("/admin/groups/{group_id}/replies")
    async def create_reply(group_id: str, request: Request, db: Session = Depends(get_db)):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        try:
            reply_type = str(form.get("reply_type", "exact"))
            key = str(form.get("key", ""))
            image_assets = None
            if settings.data_mode == "remote":
                image_assets = await _store_reply_uploads(form, group_id, user, db)
            elif any(
                str(getattr(upload, "filename", "") or "").strip()
                for upload in (form.getlist("images") if hasattr(form, "getlist") else [])
            ):
                raise PortalError("图片自动回复需要先关联 Windows 机器人网页")
            add_auto_reply(
                group_id,
                reply_type,
                key,
                str(form.get("reply", "")),
                db,
                user.id,
                image_assets=image_assets,
                clear_images=str(form.get("clear_images", "")) == "on",
                probability=form.get("probability", 100),
                max_uses=form.get("max_uses", 0),
            )
            _snapshot_and_log(
                request,
                db,
                user,
                group_id,
                "reply_add",
                "添加或更新自动回复",
                {
                    "type": reply_type,
                    "key": key,
                    "image_count": len(image_assets or []),
                    "clear_images": str(form.get("clear_images", "")) == "on",
                    "probability": str(form.get("probability", 100))[:16],
                    "max_uses": str(form.get("max_uses", 0))[:16],
                },
            )
            _flash(request, "自动回复已保存，Windows 会在下次同步时缓存图片", "success")
        except PortalError as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(_group_path(user, group_id, "#auto-replies"))

    @app.post("/user/groups/{group_id}/replies/delete")
    @app.post("/admin/groups/{group_id}/replies/delete")
    async def remove_reply(group_id: str, request: Request, db: Session = Depends(get_db)):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        reply_type = str(form.get("reply_type", ""))
        key = str(form.get("key", ""))
        try:
            delete_auto_reply(group_id, reply_type, key, db, user.id)
            _snapshot_and_log(request, db, user, group_id, "reply_delete", "删除自动回复", {"type": reply_type, "key": key})
            _flash(request, "自动回复已删除", "success")
        except PortalError as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(_group_path(user, group_id, "#auto-replies"))

    @app.post("/user/groups/{group_id}/sub-admins")
    @app.post("/admin/groups/{group_id}/sub-admins")
    async def create_sub_admin(group_id: str, request: Request, db: Session = Depends(get_db)):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        target = str(form.get("user_id", "")).strip()
        permissions = [str(item).strip() for item in form.getlist("permissions")]
        if not permissions:
            # A missing checkbox payload must not create an unrestricted
            # sub-admin.  Keep the safe, useful default for keyboard/manual
            # submissions as well as the normal browser form.
            permissions = ["reply"]
        try:
            add_group_sub_admin(
                group_id,
                target,
                str(user.id),
                db,
                permissions=permissions,
            )
            _snapshot_and_log(request, db, user, group_id, "sub_admin_add", "添加分群管理员", {"user_id": target})
            _flash(request, "分群管理员已添加", "success")
        except PortalError as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(_group_path(user, group_id, "#sub-admins"))

    @app.post("/user/groups/{group_id}/sub-admins/{target_id}/delete")
    @app.post("/admin/groups/{group_id}/sub-admins/{target_id}/delete")
    async def remove_sub_admin(group_id: str, target_id: str, request: Request, db: Session = Depends(get_db)):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        await _validated_form(request)
        delete_group_sub_admin(group_id, target_id, db, user.id)
        _snapshot_and_log(request, db, user, group_id, "sub_admin_delete", "删除分群管理员", {"user_id": target_id})
        _flash(request, "分群管理员已删除", "success")
        return _redirect(_group_path(user, group_id, "#sub-admins"))

    @app.post("/user/groups/{group_id}/player-replies")
    @app.post("/admin/groups/{group_id}/player-replies")
    async def create_player_reply(group_id: str, request: Request, db: Session = Depends(get_db)):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        form = await _validated_form(request)
        target = str(form.get("user_id", "")).strip()
        messages = str(form.get("messages", "")).splitlines()
        try:
            add_user_reply(
                group_id,
                target,
                messages,
                "at_user" in form,
                db,
                user.id,
                enabled="enabled" in form,
            )
            _snapshot_and_log(request, db, user, group_id, "player_reply_add", "添加用户专属回复", {"user_id": target})
            _flash(request, "用户专属回复已保存", "success")
        except PortalError as exc:
            db.rollback()
            _flash(request, str(exc), "error")
        return _redirect(_group_path(user, group_id, "#player-replies"))

    @app.post("/user/groups/{group_id}/player-replies/{target_id}/delete")
    @app.post("/admin/groups/{group_id}/player-replies/{target_id}/delete")
    async def remove_player_reply(group_id: str, target_id: str, request: Request, db: Session = Depends(get_db)):
        user = _require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user
        access = _owned_group_or_redirect(request, db, user, group_id, True)
        if isinstance(access, RedirectResponse):
            return access
        await _validated_form(request)
        delete_user_reply(group_id, target_id, db, user.id)
        _snapshot_and_log(request, db, user, group_id, "player_reply_delete", "删除用户专属回复", {"user_id": target_id})
        _flash(request, "用户专属回复已删除", "success")
        return _redirect(_group_path(user, group_id, "#player-replies"))

    return app
