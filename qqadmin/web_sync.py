"""Outbound-only synchronization between local AstrBot data and the portal."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import ipaddress
import json
import os
import re
import socket
import tempfile
import time
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from astrbot.api import logger

from .group_config import get_group_data, load_group_config, load_group_configs, save_group_config
from .console_log import get_console_events


def _load_group_configs_for_sync() -> tuple[dict[str, dict], bool, bool]:
    """Load local configs with a completeness bit for authoritative sync.

    Older hot-reloaded plugin modules may not expose the health-aware helper;
    keep a compatibility fallback while ensuring a real read exception never
    becomes an authoritative empty group list.
    """
    try:
        from .group_config import load_group_configs_with_health
    except (ImportError, AttributeError):
        try:
            value = load_group_configs()
        except Exception:
            return {}, False, False
        # The legacy loader intentionally collapses read errors to an empty
        # mapping and exposes no completeness bit.  Treat its result as
        # untrusted so an in-place old module can never tombstone the portal
        # inventory during a hot reload or transient file failure.
        return (value if isinstance(value, dict) else {}), False, False
    try:
        value = load_group_configs_with_health()
    except Exception:
        return {}, False, False
    if not isinstance(value, tuple) or len(value) != 2:
        return (value if isinstance(value, dict) else {}), False, False
    configs, healthy = value
    # ``healthy`` means the directory is safe for authoritative deletion only
    # after its initialization marker exists.  A readable empty directory is
    # still useful for an auth-only group projection, so consume the optional
    # diagnostic published by the current loader.  Legacy hot-reloaded
    # functions do not expose it and remain conservative.
    readable = getattr(load_group_configs_with_health, "last_readable", healthy)
    return (
        (configs if isinstance(configs, dict) else {}),
        bool(healthy),
        bool(readable),
    )


def _load_authorizations_for_sync() -> tuple[dict[str, Any], bool]:
    """Load local authorization groups without hiding a corrupt auth file."""
    try:
        from ..auth.manager import list_all_authorizations_with_health
    except (ImportError, AttributeError):
        try:
            from ..auth.manager import list_all_authorizations

            value = list_all_authorizations()
        except Exception:
            return {}, False
        # As above, the legacy API has no way to distinguish an intentional
        # empty authorization set from a missing/corrupt source file.
        return (value if isinstance(value, dict) else {}), False
    try:
        value = list_all_authorizations_with_health()
    except Exception:
        return {}, False
    if not isinstance(value, tuple) or len(value) != 2:
        return (value if isinstance(value, dict) else {}), False
    groups, healthy = value
    return (groups if isinstance(groups, dict) else {}), bool(healthy)


def _load_game_authorizations_for_sync() -> tuple[dict[str, Any], bool]:
    """Read the independent game-license source once per heartbeat."""
    try:
        from ..auth.game_manager import (
            cleanup_expired_game_authorizations,
            load_game_auth_data_with_health,
        )
    except (ImportError, AttributeError):
        return {}, False
    try:
        cleanup_expired_game_authorizations()
        data, healthy = load_game_auth_data_with_health()
    except Exception:
        return {}, False
    groups = data.get("groups", {}) if isinstance(data, dict) else {}
    return (groups if isinstance(groups, dict) else {}), bool(healthy)


def _print_sync_group_action(action: str, group_id: object, *, detail: str = "") -> None:
    """Write a configuration-sync action to the Windows CMD/event journal.

    The outbound sync client is also imported by small standalone tests and by
    older hot-reloaded plugin processes.  Resolve ``print_group_action`` lazily
    so a legacy ``console_log`` module without that helper does not prevent
    synchronization from starting.  Sync diagnostics are deliberately
    best-effort: a broken local event journal must never block applying a web
    configuration.
    """

    try:
        from .console_log import print_group_action
    except (ImportError, AttributeError):
        return
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        text = str(detail or "").strip()
        if not text.startswith("时间 "):
            text = f"时间 {timestamp}" + (f"；{text}" if text else "")
        print_group_action(action, group_id, detail=text[:160])
    except Exception:
        return


def _sync_config_detail(
    before: object,
    after: object,
    revision: int,
    metadata: object = None,
) -> str:
    """Build a bounded, value-free description of a web configuration pull.

    Configuration values can contain private replies, paths, or credentials.
    Only changed top-level sections are emitted to CMD and the portal event
    journal; this is enough to identify what was saved without mirroring data.
    """

    before_dict = before if isinstance(before, dict) else {}
    after_dict = after if isinstance(after, dict) else {}
    sections = {
        str(key)
        for key in set(before_dict).union(after_dict)
        if before_dict.get(key) != after_dict.get(key)
    }
    sections.discard("group_id")
    # ``features`` is the canonical container for most web forms.  Expand its
    # changed child keys so CMD says which management module was saved instead
    # of reporting only the generic word "features".
    if "features" in sections:
        before_features = before_dict.get("features")
        after_features = after_dict.get("features")
        before_features = before_features if isinstance(before_features, dict) else {}
        after_features = after_features if isinstance(after_features, dict) else {}
        feature_keys = {
            str(key)
            for key in set(before_features).union(after_features)
            if before_features.get(key) != after_features.get(key)
        }
        sections.remove("features")
        sections.update(f"功能:{key}" for key in sorted(feature_keys)[:8])
        if not feature_keys:
            sections.add("功能设置")
    section_labels = {
        "replies": "自动回复",
        "player_replies": "用户专属回复",
        "timed_messages": "定时消息",
        "sub_admins": "分群管理员",
        "signin": "签到数据",
        "lottery": "抽奖",
    }
    sections = {section_labels.get(section, section) for section in sections}
    # Keep output stable and bounded even when a future plugin adds many roots.
    section_text = ", ".join(sorted(sections)[:12]) or "配置"
    suffix = f"版本 {max(0, int(revision or 0))}；内容：{section_text}"
    if isinstance(metadata, dict):
        updated_at = str(metadata.get("updated_at", "") or "").strip()
        updated_by = str(metadata.get("updated_by", "") or "").strip()
        if updated_at:
            suffix += f"；网页保存时间：{updated_at[:32]}"
        if updated_by and updated_by not in {"0", "None"}:
            suffix += f"；操作账号：{updated_by[:32]}"
    return suffix[:160]


def _set_portal_message_content_enabled(enabled: bool) -> None:
    """Apply the portal privacy switch without breaking legacy hot reloads.

    Older plugin processes can keep an already-imported ``console_log`` test
    or module without the setter during an in-place update.  The sync loop
    must still work in that state; the next normal import will pick up the
    setter and apply the switch.
    """

    try:
        from .console_log import set_portal_message_content_enabled
    except (ImportError, AttributeError):
        return
    try:
        set_portal_message_content_enabled(bool(enabled))
    except Exception:
        # A privacy-toggle update is best effort and must never make a
        # successful exchange look failed or block the following ACK.
        return


# Card data is deliberately not sent to the portal.  The portal receives an
# HMAC fingerprint only after a real, group-activated card is present.  In
# particular, legacy ``MANUAL_<group>_<timestamp>`` markers are authorization
# metadata rather than cards and must never be exposed as claimable cards.
#
# ``auth.manager.verify_license_key`` still accepts the pre-dash format
# ``JK<days><timestamp><signature>``.  It is important that the sync client
# recognizes that same historical format: otherwise a successfully activated
# legacy card is synced as an authorization without a fingerprint, and the
# website can only report it as "not yet synchronized".  Keep the historical
# day-count portion open-ended here, as the verifier accepts legacy cards made
# by earlier releases that did not impose the newer UI limit.
_ACTIVATED_CARD_RE = re.compile(
    r"^JK(?:"
    r"-1-\d{13}[0-9A-F]{12}"  # current permanent card
    r"|[1-9]\d*-\d{13}[0-9A-F]{12}"  # current timed card
    r"|[1-9]\d*\d{13}[0-9A-F]{12}"  # legacy timed card, no dash
    r")$"
)
_GAME_LICENSE_RE = re.compile(
    r"^JG(?:-1-\d{13}[0-9A-F]{12}|[1-9]\d*-\d{13}[0-9A-F]{12})$"
)

# This must stay byte-for-byte compatible with
# ``web_client.security.CARD_PEPPER_PROOF_CONTEXT``.  Keep it local instead
# of importing the portal package because the Windows plugin is independently
# importable by AstrBot and by the focused unit tests.
_CARD_PEPPER_PROOF_CONTEXT = b"joker-portal-card-pepper-proof-v1"
_CLIENT_ID_RE = re.compile(r"^windows-[a-f0-9]{32}$")
_MAX_PORTAL_REPLY_ASSET_ID = 2**63 - 1
_DEFAULT_REPLY_IMAGE_MAX_BYTES = 20 * 1024 * 1024
_MAX_PORTAL_DATABASE_SNAPSHOT_BYTES = 256 * 1024 * 1024
_DATABASE_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
_DATABASE_SNAPSHOT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# A failed pull should recover shortly after the Windows heartbeat or portal
# service comes back; five minutes made a valid save look stale to users.
_DEFAULT_DATABASE_SNAPSHOT_RETRY_SECONDS = 30
# A missing/expired game card is a stable authorization state, not a transport
# failure. Retrying it every 30 seconds only floods logs until activation.
_INACTIVE_GAME_CARD_RETRY_SECONDS = 300
_MAX_PORTAL_ERROR_BODY_BYTES = 64 * 1024
_DEFAULT_REPLY_IMAGE_DOWNLOAD_CONCURRENCY = 4
_DEFAULT_REPLY_IMAGE_RETRY_SECONDS = 300
_DATABASE_SNAPSHOT_PENDING_FILE = "database_snapshot_upload_pending.json"
# Runtime panels contain several independent local database reads (blacklist,
# statistics, audit, kick, mute, recall and message cache).  They are
# read-only and must never delay the configuration pull/ACK path for every
# group on a busy bot.  Rotate a small bounded subset on normal heartbeats;
# a group whose editable configuration changed is always prioritised.
_MAX_RUNTIME_SUMMARY_GROUPS_PER_EXCHANGE = 2
# Console records are useful for the Linux panel but do not belong on the
# critical configuration heartbeat.  Sending a small ordered page lets a busy
# portal catch up without making one exchange wait on hundreds of old rows.
_MAX_CONSOLE_EVENTS_PER_EXCHANGE = 80
# Linux gives its exchange transaction up to 40 seconds by default.  The
# Windows client must wait slightly longer than that, otherwise it reports a
# local timeout even when Linux is about to return its structured response.
_EXCHANGE_TIMEOUT_SECONDS = 50.0

# These are deliberately fixed local messages.  Never render an exception
# string here: TLS/proxy exceptions can contain a full URL or credentials from
# a misconfigured proxy.  In particular, 525 is emitted by a CDN/edge proxy
# before the FastAPI application sees the request, so it is not a card or
# portal-binding association error.
_SYNC_ERROR_MESSAGES = {
    "http_status_525": (
        "CDN/代理无法与 Linux 源站完成 TLS 握手；不是卡密、令牌或群关联问题。"
        "请检查源站 443 证书、SNI/Host、反向代理和 CDN 源站 HTTPS 设置。"
    ),
    "tls_handshake_failed": (
        "HTTPS/TLS 握手或证书验证失败，请检查网页域名证书、完整证书链、"
        "电脑系统时间和 CDN 回源 SNI。"
    ),
    "read_error": (
        "读取网页响应时连接中断，通常是 CDN、代理或源站网络不稳定；"
        "正在按同步间隔重试。"
    ),
    "timeout": "连接网页配置中心超时，正在按同步间隔重试。",
    "transport_error": "无法连接网页配置中心，正在按同步间隔重试。",
    "sync_token_invalid": "同步令牌被网页服务器拒绝，请核对 Windows 插件与服务器的同步令牌。",
    "remote_mode_disabled": "网页服务器未启用远程同步模式，请检查 Linux 网页配置。",
    "card_pepper_mismatch": "Card Pepper 与网页服务器不一致，请核对两端同步配置。",
    "config_pull_endpoint_missing": (
        "Linux 网页端缺少手动拉取接口（HTTP 404），网页项目版本过旧；"
        "请更新 Linux 网页项目并重启网页服务，Windows 不需要重新授权。"
    ),
    "http_status_404": "网页同步接口不存在（HTTP 404），请更新 Linux 网页项目并重启网页服务。",
    "request_compatibility_error": (
        "Windows 当前 HTTP 请求库与网页同步接口不兼容，请更新插件后重启 AstrBot。"
    ),
    "invalid_json_response": (
        "网页同步接口返回无效数据，请检查 Linux 网页服务版本、反向代理和响应内容。"
    ),
    "sync_storage_busy": "网页数据库正被其他同步占用，Windows 已保留本地数据并自动重试。",
    "execution_client_blocked": "该 Windows 执行端已被 Linux 网页停用，请联系主人恢复。",
    "sync_transaction_failed": "网页同步事务失败，请查看 Linux 网页服务日志；Windows 将继续保留本地配置。",
    "member_directory_storage_busy": "网页成员目录正在写入，Windows 已保留本地成员列表并自动补传。",
    "member_directory_storage_failed": "网页成员目录写入失败，Windows 已保留本地成员列表，请检查网页服务日志。",
    "unexpected_redirect": (
        "网页同步接口被重定向；请检查域名反代/CDN 规则，网页地址不能跳转到服务器 IP 或登录页。"
    ),
    "database_snapshot_hash_mismatch": (
        "网页数据库快照校验失败，旧数据库已保留；请检查网页源站或代理缓存。"
    ),
    "database_snapshot_revision_mismatch": (
        "网页数据库快照版本发生变化，旧数据库已保留，正在重新同步。"
    ),
    "database_snapshot_size_mismatch": (
        "网页数据库快照大小校验失败，旧数据库已保留，正在重试。"
    ),
    "database_snapshot_write_failed": (
        "网页数据库快照无法写入本地，请检查插件目录权限；旧数据库已保留。"
    ),
    "database_snapshot_target_locked": (
        "网页数据库快照已下载并校验，但本地数据库文件正在被其他进程占用；"
        "旧数据库已保留，关闭占用该 .db 文件的程序后会自动重试。"
    ),
    "database_snapshot_permission_denied": (
        "网页数据库快照已下载并校验，但 Windows 没有权限替换本地 .db 文件；"
        "请检查插件目录权限，旧数据库已保留。"
    ),
    "invalid_database_snapshot_path": "网页数据库路径无效，已拒绝写入插件目录外的文件。",
    "game_card_heartbeat_inactive": (
        "本群游戏卡密心跳未激活，游戏 WebSocket 功能会暂缓使用；"
        "数据库快照同步不再依赖此心跳。通常是 Windows 尚未完成本群同步、"
        "卡密已过期，或当前客户端身份与授权不一致。请启动并登录 Windows 端后等待下一次心跳。"
    ),
    "group_authorization_expired": (
        "本群群管授权已过期；普通群管功能会停止，但已配置的服务器数据库仍按客户端绑定、"
        "快照版本和哈希校验同步。请同时更新 Linux 网页包和 Windows 插件。"
    ),
    "database_snapshot_missing": (
        "网页未保存当前数据库配置的快照；请在分群的服务器与数据库页面下载并保存对应 FTP 数据库。"
    ),
    "database_snapshot_invalid": (
        "网页保存的数据库快照不完整或校验失败；请重新从对应 FTP 下载并保存数据库。"
    ),
    "database_ftp_not_configured": (
        "当前数据库配置没有启用可用的 FTP/SQLite 数据库；请在网页中为该群和数据库来源分别配置并保存。"
    ),
    "database_snapshot_conflict": (
        "FTP 数据库已有更新版本，当前 Windows 缓存未覆盖；请等待下一次同步或重新拉取该数据库配置。"
    ),
    "database_snapshot_endpoint_not_found": (
        "网页数据库快照接口返回 404；请更新 Linux 网页包，并执行 joker-webctl --apply-nginx 后重载网页服务。"
    ),
    "database_snapshot_http_401": (
        "网页数据库快照接口拒绝同步令牌；请核对 Windows 与 Linux 的同步令牌。"
    ),
    "database_snapshot_http_403": (
        "网页数据库快照接口拒绝当前 Windows 客户端；请检查远程模式、群授权和数据库配置权限。"
    ),
    "database_snapshot_http_409": (
        "网页数据库快照接口返回 409，但没有返回具体原因代码；"
        "请检查客户端绑定、服务器数据库、快照版本和已保存的 FTP 数据库快照，"
        "并确认反向代理没有替换接口响应。"
    ),
}
_SYNC_ERROR_REPEAT_LOG_SECONDS = 300
# A complete exchange/ACK retry is safe because both endpoints are idempotent.
# Keep the pause short enough that a browser-triggered one-group save does not
# look stalled, while still giving a CDN/TLS socket a chance to be recreated.
_SYNC_RETRY_DELAY_SECONDS = 0.25
_HTTP_KEEPALIVE_SECONDS = 60.0


def _persistent_client_id() -> str:
    """Return a local, stable ID for the Windows execution endpoint.

    Hostnames are editable and can be duplicated after a Windows image restore.
    Using a persistent random identifier prevents a renamed PC from leaving
    stale portal snapshots behind, while still keeping the server unaware of
    machine names or any QQ credentials.  If the local plugin data directory
    is unavailable during startup/tests, retain the historical hostname
    fallback rather than preventing the bot from starting.
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        data_dir = (
            Path(get_astrbot_data_path()) / "plugin_data" / "qqadmin"
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        client_id_file = data_dir / "web_sync_client_id.txt"
        try:
            current = client_id_file.read_text(encoding="ascii").strip().lower()
        except FileNotFoundError:
            current = ""
        if _CLIENT_ID_RE.fullmatch(current):
            return current

        generated = f"windows-{uuid4().hex}"
        temporary = data_dir / f".{generated}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="ascii") as destination:
                destination.write(generated + "\n")
                destination.flush()
                os.fsync(destination.fileno())
            # The plugin normally has one process.  os.replace makes the
            # result atomic should the process restart while creating it.
            os.replace(temporary, client_id_file)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return generated
    except Exception as exc:
        logger.warning("Unable to persist portal client ID: %s", type(exc).__name__)
        return socket.gethostname()[:64]


class _PortalSyncProtocolError(RuntimeError):
    """A known, safe-to-display portal protocol failure."""

    def __init__(self, code: str, detail: object = "") -> None:
        self.code = re.sub(r"[^a-z0-9_-]", "_", str(code or "protocol_error").lower())
        self.detail = str(detail or "").strip()[:300]
        super().__init__(self.code)


def materialize_database_snapshot(
    path: object,
    database_data: bytes,
    expected_sha256: object,
    *,
    root: str | os.PathLike[str] | None = None,
    max_bytes: int = _MAX_PORTAL_DATABASE_SNAPSHOT_BYTES,
) -> Path:
    """Atomically write a verified SQLite snapshot beneath the plugin root.

    Validation happens before opening the destination.  A bad response or a
    failed write therefore leaves the previous database untouched.  The
    sibling temporary file and ``os.replace`` keep readers from observing a
    partially written SQLite file.
    """

    raw_path = str(path or "").strip().replace("\\", "/")
    parts = [part for part in raw_path.split("/") if part not in {"", "."}]
    if (
        not raw_path
        or raw_path.startswith("/")
        or re.match(r"^[A-Za-z]:", raw_path)
        or any(part == ".." for part in parts)
        or any(":" in part or any(ord(char) < 32 for char in part) for part in parts)
        or not parts
        or not parts[-1].casefold().endswith((".db", ".sqlite", ".sqlite3", ".s3db"))
    ):
        raise _PortalSyncProtocolError("invalid_database_snapshot_path")
    maximum = _bounded_integer(
        max_bytes,
        _MAX_PORTAL_DATABASE_SNAPSHOT_BYTES,
        1,
        _MAX_PORTAL_DATABASE_SNAPSHOT_BYTES,
    )
    if not isinstance(database_data, bytes):
        raise _PortalSyncProtocolError("invalid_database_snapshot_body")
    if len(database_data) > maximum:
        raise _PortalSyncProtocolError("database_snapshot_too_large")
    expected = str(expected_sha256 or "").strip().lower()
    if not _DATABASE_SNAPSHOT_SHA256_RE.fullmatch(expected):
        raise _PortalSyncProtocolError("invalid_database_snapshot_hash")
    if hashlib.sha256(database_data).hexdigest() != expected:
        raise _PortalSyncProtocolError("database_snapshot_hash_mismatch")
    if database_data[:16] != b"SQLite format 3\x00":
        raise _PortalSyncProtocolError("invalid_database_snapshot_body")

    plugin_root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    try:
        root_resolved = plugin_root.resolve()
        target = (root_resolved / "/".join(parts)).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _PortalSyncProtocolError("invalid_database_snapshot_path") from exc
    if target != root_resolved and root_resolved not in target.parents:
        raise _PortalSyncProtocolError("invalid_database_snapshot_path")
    parent = target.parent
    temporary_path: Path | None = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(parent)
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(database_data)
            destination.flush()
            os.fsync(destination.fileno())
        replace_error: OSError | None = None
        for attempt in range(3):
            try:
                os.replace(str(temporary_path), str(target))
                replace_error = None
                break
            except OSError as exc:
                replace_error = exc
                if attempt < 2:
                    time.sleep(0.15 * (attempt + 1))
        if replace_error is not None:
            winerror = getattr(replace_error, "winerror", None)
            if (
                winerror in {5, 32}
                or getattr(replace_error, "errno", None) in {13, 16}
            ):
                # Windows can report an SQLite handle opened without delete
                # sharing as either WinError 32 or WinError 5. Keep the live
                # database untouched and publish the verified body beside it;
                # the database resolver prefers this newer sidecar.
                sidecar = target.with_name(target.name + ".joker-sync")
                sidecar_temp: Path | None = None
                try:
                    sidecar_fd, sidecar_name = tempfile.mkstemp(
                        prefix=f".{sidecar.name}.", suffix=".tmp", dir=str(parent)
                    )
                    sidecar_temp = Path(sidecar_name)
                    with os.fdopen(sidecar_fd, "wb") as sidecar_file:
                        sidecar_file.write(database_data)
                        sidecar_file.flush()
                        os.fsync(sidecar_file.fileno())
                    os.replace(str(sidecar_temp), str(sidecar))
                    sidecar_temp = None
                    logger.warning("数据库文件被占用，已写入旁路快照: %s", sidecar.name)
                    return sidecar
                except OSError:
                    if sidecar_temp is not None:
                        try:
                            sidecar_temp.unlink()
                        except OSError:
                            pass
                failure_code = (
                    "database_snapshot_permission_denied"
                    if winerror == 5 or getattr(replace_error, "errno", None) == 13
                    else "database_snapshot_target_locked"
                )
                raise _PortalSyncProtocolError(failure_code, replace_error) from replace_error
            raise replace_error
        temporary_path = None
        # Once the canonical file is writable again, an older sidecar is no
        # longer needed. It is safe to leave it only when another process
        # still has it open and Windows refuses the cleanup.
        try:
            target.with_name(target.name + ".joker-sync").unlink()
        except (FileNotFoundError, OSError):
            pass
        # Directory fsync is supported on POSIX and harmlessly unavailable on
        # Windows; the file fsync above still provides the important guarantee
        # for the plugin's normal platform.
        try:
            directory_fd = os.open(str(parent), os.O_RDONLY)
        except (OSError, AttributeError):
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
        return target
    except _PortalSyncProtocolError:
        raise
    except (OSError, ValueError) as exc:
        raise _PortalSyncProtocolError(
            "database_snapshot_write_failed", str(exc)
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _sync_projection_module():
    from ..web_client import sync_projection

    return sync_projection


def _fingerprint_card(card: str, pepper: str) -> str:
    normalized = "".join(str(card or "").split()).upper()
    if not normalized or not pepper:
        return ""
    return hmac.new(
        pepper.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _card_pepper_proof(pepper: str) -> str:
    """Prove a matching Card Pepper without sending the pepper or a card."""
    normalized = str(pepper or "").strip()
    return hmac.new(
        normalized.encode("utf-8"), _CARD_PEPPER_PROOF_CONTEXT, hashlib.sha256
    ).hexdigest()


def _is_activated_card(card: object) -> bool:
    """Return whether a stored authorization key is a real card identifier."""
    normalized = "".join(str(card or "").split()).upper()
    return bool(_ACTIVATED_CARD_RE.fullmatch(normalized))


def _authorization_payload_from_info(auth_info: object, card_pepper: str = "") -> dict:
    """Return the non-secret portal representation of one local authorization.

    This intentionally accepts an already-read record.  The sync loop uses it
    after checking ``auth.json`` health, so it never performs a second loader
    call that could silently turn a temporary file error into a fake
    ``missing`` authorization.
    """

    info = auth_info if isinstance(auth_info, dict) else {}
    try:
        expire = int(info.get("expire", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        expire = 0
    card = str(info.get("key", ""))
    activated_card = card if _is_activated_card(card) else ""
    if expire == -1:
        expire_text = "永久授权"
        state = "active"
    elif expire > int(time.time()):
        expire_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(expire))
        state = "active"
    elif expire > 0:
        expire_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(expire))
        state = "expired"
    else:
        expire_text = "无授权记录"
        state = "missing"
    card_mask = ""
    if activated_card:
        card_mask = (
            activated_card[:4] + "****" + activated_card[-2:]
            if len(activated_card) <= 12
            else activated_card[:7] + "****" + activated_card[-5:]
        )
    stored_fingerprint = str(info.get("card_fingerprint", "") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", stored_fingerprint):
        stored_fingerprint = ""
    if stored_fingerprint and not card_mask:
        card_mask = str(info.get("card_mask", "") or "").strip()[:64]
    try:
        activated_at = int(info.get("activated_at", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        activated_at = 0
    return {
        "state": state,
        "expire": expire,
        "expire_text": expire_text,
        "card_fingerprint": stored_fingerprint or _fingerprint_card(activated_card, card_pepper),
        "card_mask": card_mask,
        "activated_at": activated_at,
        "user_id": str(info.get("user_id", "")),
    }


def _game_authorization_payload_from_info(
    auth_info: object, card_pepper: str = ""
) -> dict[str, Any]:
    """Return the non-secret game-license heartbeat projection."""
    info = auth_info if isinstance(auth_info, dict) else {}
    card = str(info.get("key", "") or "").strip().upper()
    try:
        expire = int(info.get("expire", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        expire = 0
    try:
        activated_at = int(info.get("activated_at", info.get("issued_at", 0)) or 0)
    except (TypeError, ValueError, OverflowError):
        activated_at = 0
    if expire == -1 or expire > int(time.time()):
        state = "active"
    elif expire > 0:
        state = "expired"
    else:
        state = "missing"
    card_mask = ""
    if _GAME_LICENSE_RE.fullmatch(card):
        card_mask = card[:7] + "****" + card[-5:]
    return {
        "state": state,
        "expire": expire,
        "card_fingerprint": _fingerprint_card(card, card_pepper)
        if _GAME_LICENSE_RE.fullmatch(card)
        else "",
        "card_mask": card_mask,
        "activated_at": activated_at,
        "user_id": str(info.get("user_id", "") or "")[:32],
    }


def build_group_payload(
    group_id: str,
    config: dict,
    card_pepper: str = "",
    *,
    include_config: bool = True,
    include_auth: bool = True,
    auth_info: dict | None = None,
    include_game_auth: bool = True,
    game_auth_info: dict | None = None,
) -> dict:
    """Build a privacy-limited local projection for one portal group.

    Configuration and authorization files are independently persisted on
    Windows.  A readable group config must still be allowed to fulfill a
    browser's pull request when ``auth.json`` is temporarily missing, while
    the portal must retain its previous card state.  The optional source
    switches make that distinction explicit on the wire instead of publishing
    a fabricated empty authorization.

    Defaults preserve the legacy full-payload behaviour for direct callers.
    """

    payload: dict[str, Any] = {"group_id": str(group_id)}
    if include_config:
        payload["config"] = _sync_projection_module().project_group_config(config)
    if include_auth:
        if auth_info is None:
            # Compatibility path for existing callers outside ``sync_once``.
            # The production loop passes the already health-checked record.
            from ..auth.manager import get_group_auth_info

            auth_info = get_group_auth_info(group_id) or {}
        payload["auth"] = _authorization_payload_from_info(auth_info, card_pepper)
    if include_game_auth:
        if game_auth_info is None:
            try:
                from ..auth.game_manager import get_game_authorization_info

                game_auth_info = get_game_authorization_info(group_id)
            except (ImportError, AttributeError, OSError):
                game_auth_info = {}
        payload["game_auth"] = _game_authorization_payload_from_info(
            game_auth_info, card_pepper
        )
    return payload


def _message_cache_projection(raw: object) -> dict[str, Any] | None:
    """Convert one local message record into a bounded portal-safe summary."""
    if not isinstance(raw, dict):
        return None
    # A reconcile sentinel only wakes the uploader so it can read real
    # retained records afterwards.  It must never become a blank audit row in
    # the Linux member history.
    if bool(raw.get("_message_cache_reconcile_only", False)):
        return None
    from .message_cache import format_cached_message

    sender = raw.get("sender", {})
    sender = sender if isinstance(sender, dict) else {}
    segments = raw.get("segments", [])
    segment_types = [
        str(segment.get("type", "")).strip().lower()
        for segment in (segments if isinstance(segments, list) else [])
        if isinstance(segment, dict) and str(segment.get("type", "")).strip()
    ]
    media_urls: list[str] = []
    media_items: list[dict[str, str]] = []
    media_identity: set[str] = set()
    face_labels: list[str] = []
    for segment in (segments if isinstance(segments, list) else []):
        if not isinstance(segment, dict):
            continue
        segment_type = str(segment.get("type", "")).strip().lower()
        if segment_type not in {
            "image", "face", "mface", "marketface", "record", "audio", "video", "file"
        }:
            continue
        data = segment.get("data", {})
        if not isinstance(data, dict):
            continue
        if segment_type in {"face", "mface", "marketface"}:
            raw_face = data.get("raw", {})
            raw_face = raw_face if isinstance(raw_face, dict) else {}
            face_id = str(
                raw_face.get("id")
                or raw_face.get("face_id")
                or data.get("id")
                or data.get("face_id")
                or ""
            ).strip()
            face_label = str(
                raw_face.get("faceText")
                or data.get("faceText")
                or data.get("summary")
                or data.get("name")
                or data.get("id")
                or ""
            ).strip()
            if face_label and face_label not in face_labels:
                face_labels.append(face_label[:80])
            if segment_type == "face" and face_id.isdigit() and len(face_id) <= 4:
                face_url = (
                    "https://koishi.js.org/QFace/assets/qq_emoji/"
                    f"{face_id}/png/{face_id}.png"
                )
                face_key = f"face:{face_id}"
                if face_key not in media_identity:
                    media_identity.add(face_key)
                    media_items.append(
                        {
                            "kind": "image",
                            "url": face_url,
                            "name": face_label or f"QQ表情 {face_id}",
                        }
                    )
        for key in (
            "url", "image_url", "record_url", "audio_url", "video_url",
            "file", "file_", "path",
        ):
            candidate = str(data.get(key, "") or "").strip()
            if not candidate.startswith("https://"):
                continue
            try:
                parsed = urlsplit(candidate)
                hostname = (parsed.hostname or "").lower().rstrip(".")
            except ValueError:
                continue
            if parsed.scheme != "https" or not any(
                hostname == suffix or hostname.endswith("." + suffix)
                for suffix in ("qpic.cn", "gtimg.cn", "qq.com", "nt.qq.com.cn")
            ):
                continue
            normalized_url = candidate[:2048]
            if segment_type in {"image", "mface", "marketface"} and normalized_url not in media_urls:
                media_urls.append(normalized_url)
            if segment_type in {"image", "mface", "marketface"}:
                identity = (
                    data.get("file_id")
                    or data.get("file")
                    or data.get("file_")
                    or data.get("image_id")
                    or data.get("id")
                    or normalized_url.split("?", 1)[0].split("#", 1)[0]
                )
                media_key = f"image:{str(identity)[:256]}"
            else:
                media_key = f"{segment_type}:{normalized_url}"
            if media_key not in media_identity:
                media_identity.add(media_key)
                media_items.append(
                    {
                        "kind": (
                            "image"
                            if segment_type in {"image", "mface", "marketface"}
                            else ("audio" if segment_type in {"record", "audio"} else segment_type)
                        ),
                        "url": normalized_url,
                        "name": str(
                            data.get("file_name")
                            or data.get("name")
                            or data.get("file")
                            or ""
                        )[:160],
                    }
                )
            break
        if len(media_items) >= 8:
            break
    # Use Windows' local archive only as a fallback. A live QQ URL and its
    # archived data URL describe the same image; returning both makes the web
    # page render the sender's one image twice. If no live image was
    # projected, the archive keeps historical messages viewable after a QQ
    # CDN URL has expired.
    if not any(
        str(item.get("kind", "")).lower() == "image"
        and str(item.get("url", "")).strip()
        for item in media_items
    ):
        try:
            from .message_cache import get_archived_message_images

            archived = get_archived_message_images(
                str(raw.get("group_id", "") or ""),
                str(raw.get("user_id", "") or ""),
                raw.get("timestamp", 0),
                raw.get("message_id", ""),
            )
            for item in archived:
                if not any(existing.get("url") == item.get("url") for existing in media_items):
                    media_items.append(item)
                if len(media_items) >= 8:
                    break
        except Exception:
            pass
    return {
        "scope_type": str(raw.get("scope_type", "group") or "group")[:16],
        "scope_id": str(raw.get("private_user_id", raw.get("user_id", "")) or "")[:32],
        "timestamp": _bounded_integer(raw.get("timestamp", 0), 0, 0, 2**31 - 1),
        "user_id": str(raw.get("user_id", ""))[:32],
        "nickname": str(raw.get("nickname") or sender.get("nickname") or "")[:80],
        "user_name": str(
            raw.get("nickname")
            or sender.get("nickname")
            or raw.get("user_name")
            or sender.get("card")
            or ""
        )[:80],
        "message_id": str(raw.get("message_id", ""))[:64],
        "dedupe_key": str(raw.get("dedupe_key", ""))[:128],
        "source_account_id": str(raw.get("source_account_id", ""))[:32],
        "mirror_key": str(raw.get("mirror_key", ""))[:128],
        "recalled": bool(raw.get("recalled", False)),
        "recalled_at": _bounded_integer(raw.get("recalled_at", 0), 0, 0, 2**31 - 1),
        "recall_reason": str(raw.get("recall_reason", ""))[:160],
        "content": str(format_cached_message(raw, 1200))[:1200],
        "media_urls": media_urls,
        "media_items": media_items,
        "face_labels": face_labels[:8],
        "direction": str(raw.get("direction", "inbound") or "inbound")[:16],
        "has_image": bool(
            "image" in segment_types
            or raw.get("has_image")
            or raw.get("image_paths")
            or raw.get("images")
        ),
        "image_count": sum(1 for kind in segment_types if kind == "image"),
        "video_count": sum(1 for kind in segment_types if kind == "video"),
        "file_count": sum(1 for kind in segment_types if kind == "file"),
        "record_count": sum(1 for kind in segment_types if kind in {"record", "audio"}),
        "forward_count": sum(1 for kind in segment_types if kind in {"forward", "node"}),
        "message_types": ",".join(sorted(set(segment_types)))[:120],
    }


def build_runtime_summary(group_id: str) -> dict[str, Any]:
    """Build a small read-only view of Windows runtime records.

    The portal uses this to render read-only runtime panels.  It is intentionally
    separate from the editable group projection and bounded so a large local
    database can never make a heartbeat or browser page slow.  None of these
    records are accepted back from the web client, so saving a form cannot
    reset local moderation history.
    """
    # The portal records its receipt time separately.  Do not include a
    # heartbeat timestamp here: it would make an otherwise identical runtime
    # summary appear changed every interval and force a full upload.
    summary: dict[str, Any] = {}
    # Reuse the latest member directory to turn QQ IDs in runtime records into
    # recognisable group names.  It is a display aid only; moderation state is
    # still read from its dedicated local stores.
    member_name_by_id: dict[str, str] = {}
    try:
        # The member list is refreshed on demand by the portal action queue and
        # stored in the existing Windows cache.  Keep only fields needed by the
        # web selector; QQ IDs and display names are not sent elsewhere.
        from .welcome import get_group_members
        from .group_config import get_group_data

        members: list[dict[str, Any]] = []
        raw_members = get_group_members(str(group_id))
        # Hot reloads can leave ``welcome`` and ``group_config`` with separate
        # module caches for one heartbeat. Read the persisted cache directly as
        # a fallback so a successful refresh is not reported as zero members.
        if not isinstance(raw_members, list) or not raw_members:
            cached_fallback = get_group_data(str(group_id), "cached_members", {})
            if isinstance(cached_fallback, dict):
                raw_members = cached_fallback.get("members", [])
        if not isinstance(raw_members, list):
            raw_members = []
        try:
            from .message_cache import get_group_member_activity
            activity_by_user = get_group_member_activity(str(group_id), days=30)
        except Exception:
            activity_by_user = {}
        # This is a group-level activity summary, distinct from the member
        # directory.  It is calculated from Windows' local message cache only;
        # it never touches the SCUM/FTP database and therefore remains fast
        # while a game database file is in use.
        if isinstance(activity_by_user, dict):
            group_message_count = 0
            group_active_members = 0
            group_last_message_at = 0
            for item in activity_by_user.values():
                if not isinstance(item, dict):
                    continue
                try:
                    message_count = max(0, min(1_000_000, int(item.get("message_count", 0) or 0)))
                    last_message_at = max(0, min(2**31 - 1, int(item.get("last_message_at", 0) or 0)))
                except (TypeError, ValueError, OverflowError):
                    continue
                group_message_count += message_count
                if message_count:
                    group_active_members += 1
                group_last_message_at = max(group_last_message_at, last_message_at)
            summary["group_activity"] = {
                "available": True,
                "window_days": 30,
                "message_count": min(10_000_000, group_message_count),
                "active_members": min(100_000, group_active_members),
                "last_message_at": group_last_message_at,
            }
        # Preserve complete member directories for groups larger than 1000.
        for raw in raw_members[:10000]:
            if not isinstance(raw, dict):
                continue
            # NapCat and older OneBot adapters use ``uin``/``userId`` for the
            # same field. Normalize all three forms before filtering, or a
            # successfully refreshed member list is rendered as empty.
            user_id = str(
                raw.get(
                    "user_id",
                    raw.get(
                        "userId",
                        raw.get("uin", raw.get("qq", raw.get("uid", ""))),
                    ),
                )
            ).strip()
            if not user_id.isdigit() or not 5 <= len(user_id) <= 20:
                continue
            card = str(raw.get("card") or raw.get("group_card") or "").strip()
            nickname = str(raw.get("nickname") or raw.get("nick") or "").strip()
            name = card or nickname or str(raw.get("user_name") or "").strip()
            member_name_by_id[user_id] = name[:80]
            try:
                level = max(0, min(999, int(raw.get("level", 0) or 0)))
            except (TypeError, ValueError, OverflowError):
                level = 0
            qq_level = raw.get("qq_level", raw.get("qqLevel", raw.get("q_level", raw.get("qLevel", ""))))
            if qq_level in (None, "", "未返回"):
                qq_level = raw.get("account_level", raw.get("accountLevel", ""))
            if qq_level in (None, "", "未返回"):
                qq_level = raw.get("qq_level_text", raw.get("level_text", ""))
            try:
                mute_until = max(0, int(raw.get("shut_up_timestamp", raw.get("mute_until", 0)) or 0))
            except (TypeError, ValueError, OverflowError):
                mute_until = 0
            activity = activity_by_user.get(user_id, {}) if isinstance(activity_by_user, dict) else {}
            try:
                activity_count = max(0, min(1000000, int(activity.get("message_count", raw.get("activity_count", 0)) or 0)))
                last_message_at = max(0, min(2**31 - 1, int(activity.get("last_message_at", raw.get("last_message_at", 0)) or 0)))
            except (TypeError, ValueError, OverflowError):
                activity_count, last_message_at = 0, 0
            activity_counts: dict[str, int] = {}
            for field in (
                "text_count", "image_count", "video_count", "file_count",
                "record_count", "forward_count", "other_count",
            ):
                try:
                    activity_counts[field] = max(
                        0,
                        min(1000000, int(activity.get(field, raw.get(field, 0)) or 0)),
                    )
                except (TypeError, ValueError, OverflowError):
                    activity_counts[field] = 0
            members.append(
                {
                    "user_id": user_id,
                    "user_name": (name or "未获取姓名")[:80],
                    "card": card[:80],
                    "nickname": nickname[:80],
                    "role": str(
                        raw.get("role", raw.get("permission", "member"))
                    )[:16],
                    "level": level,
                    "group_level": level,
                    "qq_level": qq_level,
                    "level_text": (
                        str(qq_level)[:32]
                        if qq_level not in (None, "", "未返回")
                        else "未返回"
                    ),
                    "title": str(raw.get("title", "") or "")[:80],
                    "join_time": _bounded_integer(raw.get("join_time", 0), 0, 0, 2**31 - 1),
                    "last_sent_time": _bounded_integer(raw.get("last_sent_time", 0), 0, 0, 2**31 - 1),
                    "mute_until": mute_until,
                    "mute_until_text": (
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mute_until))
                        if mute_until > int(time.time())
                        else "未禁言"
                    ),
                    "activity_count": activity_count,
                    "last_message_at": last_message_at,
                    **activity_counts,
                    "last_message_text": str(
                        activity.get("last_message_text", raw.get("last_message_text", "")) or ""
                    )[:160],
                    "last_message_types": str(
                        activity.get("last_message_types", raw.get("last_message_types", "")) or ""
                    )[:120],
                }
            )
        cached = get_group_data(str(group_id), "cached_members", {})
        cached = cached if isinstance(cached, dict) else {}
        cached_updated_at = _bounded_integer(cached.get("updated_at", 0), 0, 0, 2**31 - 1)
        cached_message = str(cached.get("message", "") or "")[:500]
        cached_levels_pending = bool(cached.get("levels_pending", False))
        cached_upload_pending = bool(cached.get("upload_pending", False))
        cached_snapshot_complete = bool(cached.get("snapshot_complete", True))
        cached_expected_count = _bounded_integer(
            cached.get("expected_count", 0), 0, 0, 10000
        )
        # Do not let an uninitialized empty cache replace a usable directory
        # already stored on the portal. A successful refresh (or an explicit
        # refresh error) carries updated_at/message and is still uploaded.
        if members or cached_updated_at or cached_message:
            summary["members"] = {
                "count": len(members),
                "updated_at": cached_updated_at,
                "message": cached_message,
                "levels_pending": cached_levels_pending,
                "upload_pending": cached_upload_pending,
                "partial": not cached_snapshot_complete,
                "expected_count": cached_expected_count,
                "members": members,
            }
    except Exception:
        logger.debug("Unable to build Windows group-member summary", exc_info=True)
    try:
        from .feature_diagnostics import list_feature_errors

        errors = list_feature_errors(str(group_id), limit=30)
        summary["errors"] = {"count": len(errors), "recent": errors}
    except Exception:
        # Diagnostics must never block the normal configuration heartbeat.
        logger.debug("Unable to build Windows feature-error summary", exc_info=True)
    try:
        from .blacklist import list_all_blacklist_records

        records = list_all_blacklist_records(str(group_id))
        entries: list[dict[str, Any]] = []
        if isinstance(records, list):
            for raw in records[:200]:
                if not isinstance(raw, dict):
                    continue
                user_id = str(raw.get("user_id", "")).strip()[:32]
                if not user_id:
                    continue
                try:
                    expire = max(0, int(raw.get("expire_time", 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    expire = 0
                active = bool(raw.get("is_active", raw.get("active", True)))
                if expire and expire <= int(time.time()):
                    active = False
                entries.append(
                    {
                        "user_id": user_id,
                        "user_name": member_name_by_id.get(user_id, ""),
                        "active": active,
                        "ban_type": str(raw.get("ban_type", ""))[:24],
                        "reason": str(raw.get("reason", ""))[:240],
                        "expire_time": expire,
                    }
                )
            active_count = sum(1 for item in entries if item["active"])
            summary["blacklist"] = {
                "total": len(records),
                "active": active_count,
                "entries": entries,
            }
    except Exception:
        logger.debug("Unable to build Windows blacklist runtime summary", exc_info=True)

    try:
        from .blacklist import load_global_blacklist

        global_records = load_global_blacklist()
        global_records = global_records if isinstance(global_records, dict) else {}
        global_entries: list[dict[str, Any]] = []
        global_admin_entries: list[dict[str, Any]] = []
        now = int(time.time())
        for raw_user_id, raw in global_records.items():
            if not isinstance(raw, dict):
                continue
            user_id = str(raw_user_id or raw.get("user_id", "")).strip()[:32]
            if not user_id:
                continue
            expire_time = _bounded_integer(raw.get("expire_time", 0), 0, 0, 2**31 - 1)
            active = bool(raw.get("is_active", raw.get("active", True)))
            if expire_time and expire_time <= now:
                active = False
            allowed_groups = raw.get("allowed_groups", [])
            allowed_groups = (
                [str(item).strip()[:32] for item in allowed_groups[:500] if str(item).strip()]
                if isinstance(allowed_groups, list)
                else []
            )
            allowed_in_group = str(group_id).strip() in set(allowed_groups)
            entry = {
                "user_id": user_id,
                "user_name": member_name_by_id.get(user_id, ""),
                "active": active,
                "ban_type": str(raw.get("ban_type", ""))[:24],
                "reason": str(raw.get("reason", ""))[:240],
                "operator_id": str(raw.get("added_by", raw.get("operator_id", "")))[:32],
                "added_at": _bounded_integer(raw.get("added_at", raw.get("created_at", 0)), 0, 0, 2**31 - 1),
                "expire_time": expire_time,
                "allowed_groups": allowed_groups,
                "allowed_in_group": allowed_in_group,
            }
            # Administrators need the canonical global view, including records
            # that have a per-group exception. Keep it separate from the
            # customer-facing group panel below, which remains filtered.
            global_admin_entries.append(entry)
            # A group-level exception means this user is no longer blocked in
            # the current group. Do not leak the global record into this
            # group's blacklist panel or counts.
            if allowed_in_group:
                continue
            entry = dict(entry)
            entry["allowed_in_group"] = False
            global_entries.append(entry)
        global_entries.sort(
            key=lambda item: (not bool(item.get("active")), -int(item.get("added_at", 0) or 0))
        )
        global_admin_entries.sort(
            key=lambda item: (not bool(item.get("active")), -int(item.get("added_at", 0) or 0))
        )
        active_entries = [item for item in global_entries if item.get("active")]
        permanent_count = sum(
            1 for item in active_entries if not int(item.get("expire_time", 0) or 0)
        )
        temporary_count = max(0, len(active_entries) - permanent_count)
        summary["global_blacklist"] = {
            "total": len(global_entries),
            "active": len(active_entries),
            "permanent": permanent_count,
            "temporary": temporary_count,
            "inactive": max(0, len(global_entries) - len(active_entries)),
            "entries": global_entries[:200],
        }
        admin_active_entries = [item for item in global_admin_entries if item.get("active")]
        admin_permanent_count = sum(
            1 for item in admin_active_entries if not int(item.get("expire_time", 0) or 0)
        )
        summary["global_blacklist_admin"] = {
            "total": len(global_admin_entries),
            "active": len(admin_active_entries),
            "permanent": admin_permanent_count,
            "temporary": max(0, len(admin_active_entries) - admin_permanent_count),
            "inactive": max(0, len(global_admin_entries) - len(admin_active_entries)),
            "entries": global_admin_entries[:500],
        }
    except Exception:
        logger.debug("Unable to build Windows global-blacklist runtime summary", exc_info=True)

    try:
        from .stats import get_group_stats, get_ranking
        from .steam_signin import list_steam_bindings

        raw_group = get_group_stats(str(group_id))
        raw_group = raw_group if isinstance(raw_group, dict) else {}
        group = {}
        for key in ("total_users", "today_signins", "total_points", "avg_points"):
            try:
                group[key] = max(0, int(raw_group.get(key, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                group[key] = 0
        ranking: list[dict[str, Any]] = []
        # Twenty rows were too little to inspect concrete member data. Keep
        # the read-only runtime payload bounded while exposing a useful view.
        raw_ranking = get_ranking(str(group_id), 100)
        if isinstance(raw_ranking, list):
            for index, raw in enumerate(raw_ranking[:100], 1):
                if not isinstance(raw, dict):
                    continue
                user_id = str(raw.get("user_id", "")).strip()[:32]
                if not user_id:
                    continue
                def _count(name: str) -> int:
                    try:
                        return max(0, int(raw.get(name, 0) or 0))
                    except (TypeError, ValueError, OverflowError):
                        return 0
                ranking.append(
                    {
                        "rank": index,
                        "user_id": user_id,
                        "user_name": str(raw.get("user_name", ""))[:80],
                        "points": _count("points"),
                        "signin_count": _count("signin_count"),
                    }
                )
        steam_bindings: list[dict[str, Any]] = []
        for raw in list_steam_bindings(str(group_id), limit=100):
            if not isinstance(raw, dict):
                continue
            steam_id = str(raw.get("steam_id", "")).strip()
            user_id = str(raw.get("user_id", "")).strip()
            if not steam_id or not user_id:
                continue
            def _binding_count(name: str) -> int:
                try:
                    return max(0, int(raw.get(name, 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    return 0
            steam_bindings.append(
                {
                    "steam_id": steam_id,
                    "user_id": user_id[:32],
                    "user_name": str(raw.get("user_name", ""))[:80],
                    "bound_at": _binding_count("bound_at"),
                    "last_signin": str(raw.get("last_signin", ""))[:16],
                    "signin_count": _binding_count("signin_count"),
                    "monthly_count": _binding_count("monthly_count"),
                    "weekly_count": _binding_count("weekly_count"),
                }
            )
        summary["stats"] = {
            "group": group,
            "ranking": ranking,
            "steam_bindings": steam_bindings,
        }
    except Exception:
        logger.debug("Unable to build Windows statistics runtime summary", exc_info=True)

    # Moderation/audit summaries are deliberately small and value-limited.
    # Import inside the heartbeat so an optional legacy module cannot prevent
    # the normal configuration exchange from running.
    try:
        from .audit import get_audit_statistics, get_verification_stats, get_audit_history

        statistics = get_audit_statistics(str(group_id))
        verification = get_verification_stats(str(group_id))
        statistics = statistics if isinstance(statistics, dict) else {}
        verification = verification if isinstance(verification, dict) else {}
        recent: list[dict[str, Any]] = []
        history = get_audit_history(str(group_id), limit=20)
        if isinstance(history, list):
            for raw in history[-20:]:
                if not isinstance(raw, dict):
                    continue
                user_id = str(raw.get("user_id", ""))[:32]
                recent.append(
                    {
                        "user_id": user_id,
                        "user_name": str(raw.get("user_name", "") or member_name_by_id.get(user_id, ""))[:80],
                        "action": str(raw.get("action", ""))[:32],
                        "reason": str(raw.get("reason", ""))[:160],
                        "timestamp": _bounded_integer(raw.get("timestamp", 0), 0, 0, 2**31 - 1),
                    }
                )
        summary["audit"] = {
            "pending_count": _bounded_integer(statistics.get("pending_count", 0), 0, 0, 100000),
            "total_processed": _bounded_integer(statistics.get("total_processed", 0), 0, 0, 1000000),
            "approved": _bounded_integer(statistics.get("approved", 0), 0, 0, 1000000),
            "rejected": _bounded_integer(statistics.get("rejected", 0), 0, 0, 1000000),
            "active_challenges": _bounded_integer(verification.get("active_challenges", 0), 0, 0, 100000),
            "recent": recent,
        }
    except Exception:
        logger.debug("Unable to build Windows audit runtime summary", exc_info=True)

    try:
        from .kick import get_kick_statistics, get_kick_records

        statistics = get_kick_statistics(str(group_id))
        statistics = statistics if isinstance(statistics, dict) else {}
        records: list[dict[str, Any]] = []
        raw_records = get_kick_records(str(group_id), limit=30)
        if isinstance(raw_records, list):
            for raw in raw_records[-30:]:
                if not isinstance(raw, dict):
                    continue
                user_id = str(raw.get("user_id", ""))[:32]
                records.append(
                    {
                        "user_id": user_id,
                        "user_name": member_name_by_id.get(user_id, ""),
                        "count": _bounded_integer(raw.get("踢出次数", raw.get("count", 1)), 1, 1, 1000000),
                        "reason": str(raw.get("last_reason", raw.get("reason", "")))[:160],
                        "last_kicked_at": _bounded_integer(raw.get("last_kicked_at", raw.get("kicked_at", 0)), 0, 0, 2**31 - 1),
                    }
                )
        summary["kick"] = {
            "total_kicks": _bounded_integer(statistics.get("total_kicks", 0), 0, 0, 1000000),
            "unique_users": _bounded_integer(statistics.get("unique_users", 0), 0, 0, 1000000),
            "today_kicks": _bounded_integer(statistics.get("today_kicks", 0), 0, 0, 1000000),
            "week_kicks": _bounded_integer(statistics.get("week_kicks", 0), 0, 0, 1000000),
            "records": records,
        }
    except Exception:
        logger.debug("Unable to build Windows kick runtime summary", exc_info=True)

    try:
        from .mute import get_mute_statistics, list_muted_users

        statistics = get_mute_statistics(str(group_id))
        statistics = statistics if isinstance(statistics, dict) else {}
        active: list[dict[str, Any]] = []
        raw_active = list_muted_users(str(group_id))
        if isinstance(raw_active, list):
            for raw in raw_active[:100]:
                if not isinstance(raw, dict):
                    continue
                user_id = str(raw.get("user_id", ""))[:32]
                active.append(
                    {
                        "user_id": user_id,
                        "user_name": member_name_by_id.get(user_id, ""),
                        "duration": _bounded_integer(raw.get("duration", 0), 0, 0, 2_592_000),
                        "expire_time": _bounded_integer(raw.get("expire_time", 0), 0, 0, 2**31 - 1),
                        "reason": str(raw.get("reason", ""))[:160],
                        "mute_level": _bounded_integer(raw.get("mute_level", 1), 1, 1, 100),
                    }
                )
        summary["mute"] = {
            "total_muted": _bounded_integer(statistics.get("total_muted", 0), 0, 0, 1000000),
            "currently_muted": _bounded_integer(statistics.get("currently_muted", 0), 0, 0, 1000000),
            "total_mute_times": _bounded_integer(statistics.get("total_mute_times", 0), 0, 0, 1000000),
            "active": active,
        }
    except Exception:
        logger.debug("Unable to build Windows mute runtime summary", exc_info=True)

    try:
        from .recall import get_recall_statistics, get_recall_log

        statistics = get_recall_statistics(str(group_id))
        statistics = statistics if isinstance(statistics, dict) else {}
        recent: list[dict[str, Any]] = []
        raw_logs = get_recall_log(str(group_id), limit=30)
        if isinstance(raw_logs, list):
            for raw in raw_logs[-30:]:
                if not isinstance(raw, dict):
                    continue
                user_id = str(raw.get("user_id", ""))[:32]
                recent.append(
                    {
                        "user_id": user_id,
                        "user_name": member_name_by_id.get(user_id, ""),
                        "message_id": str(raw.get("message_id", ""))[:64],
                        "reason": str(raw.get("reason", ""))[:160],
                        "content": str(raw.get("content", ""))[:240],
                        "recalled_at": _bounded_integer(raw.get("recalled_at", 0), 0, 0, 2**31 - 1),
                    }
                )
        summary["recall"] = {
            "total_recalls": _bounded_integer(statistics.get("total_recalls", 0), 0, 0, 1000000),
            "keywords_count": _bounded_integer(statistics.get("keywords_count", 0), 0, 0, 100000),
            "regex_count": _bounded_integer(statistics.get("regex_count", 0), 0, 0, 100000),
            "whitelist_count": _bounded_integer(statistics.get("whitelist_count", 0), 0, 0, 100000),
            "self_recall_enabled": bool(statistics.get("self_recall_enabled", False)),
            "recent": recent,
        }
    except Exception:
        logger.debug("Unable to build Windows recall runtime summary", exc_info=True)

    try:
        from .message_cache import get_cached_messages

        records: list[dict[str, Any]] = []
        # Keep a bounded, useful history for the portal.  The old 50-record
        # projection meant a busy group could lose a member's messages before
        # an administrator had time to inspect them.  Records remain compact
        # summaries; media bytes and Windows-local paths are never uploaded.
        raw_records = get_cached_messages(str(group_id), limit=None)
        if isinstance(raw_records, list):
            for raw in raw_records:
                projected = _message_cache_projection(raw)
                if projected is not None:
                    records.append(projected)
        summary["message_cache"] = {
            "available": True,
            "source": "windows_local_cache",
            "count": len(records),
            "records": records,
        }
    except Exception:
        logger.debug("Unable to build Windows message-cache runtime summary", exc_info=True)
    try:
        from .audit import get_operation_logs

        recent: list[dict[str, Any]] = []
        categories: dict[str, list[dict[str, Any]]] = {
            "入群审核": [],
            "成员处理": [],
            "黑名单": [],
            "撤回": [],
            "定时任务": [],
            "其他": [],
        }

        def operation_category(action: str) -> str:
            normalized = str(action or "")
            if any(token in normalized for token in ("审核", "验证", "审批", "入群", "timeout_reject")):
                return "入群审核"
            if any(token in normalized for token in ("黑名单", "拉黑", "解除黑")):
                return "黑名单"
            if any(token in normalized for token in ("撤回", "删除消息")):
                return "撤回"
            if any(token in normalized for token in ("定时", "整点", "播报", "音乐")):
                return "定时任务"
            if any(token in normalized for token in ("禁言", "解禁", "踢出", "移出", "退群", "名片", "成员")):
                return "成员处理"
            return "其他"

        raw_logs = get_operation_logs(str(group_id), limit=50)
        if isinstance(raw_logs, list):
            # The portal table is a live operation feed. Sort by the stored
            # timestamp instead of relying on append order: older snapshots
            # and migrated data may not be physically ordered.
            sortable_logs = [
                (index, raw)
                for index, raw in enumerate(raw_logs[-50:])
                if isinstance(raw, dict)
            ]
            sortable_logs.sort(
                key=lambda item: (
                    _bounded_integer(item[1].get("timestamp", 0), 0, 0, 2**31 - 1),
                    item[0],
                ),
                reverse=True,
            )
            for _, raw in sortable_logs:
                operator_id = str(raw.get("operator_id", ""))[:32]
                target_id = str(raw.get("target_id", ""))[:32]
                entry = {
                        "operator_id": operator_id,
                        "operator_name": str(raw.get("operator_name", "") or member_name_by_id.get(operator_id, ""))[:80],
                        "action": str(raw.get("action", ""))[:80],
                        "target_id": target_id,
                        "target_name": str(raw.get("target_name", "") or member_name_by_id.get(target_id, ""))[:80],
                        "reason": str(raw.get("reason", ""))[:200],
                        "timestamp": _bounded_integer(raw.get("timestamp", 0), 0, 0, 2**31 - 1),
                    }
                entry["category"] = operation_category(entry["action"])
                recent.append(entry)
                categories[entry["category"]].append(entry)
        summary["operations"] = {
            "count": len(recent),
            "recent": recent,
            "categories": categories,
        }
    except Exception:
        logger.debug("Unable to build Windows operation-log runtime summary", exc_info=True)
    return summary


def _bounded_integer(
    value: object, default: int, minimum: int, maximum: int
) -> int:
    """Return a configuration integer without allowing untrusted extremes."""
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _portal_reply_cache_options(config: object) -> tuple[int, int, int]:
    """Read per-group image cache limits from a merged local configuration.

    The image bytes themselves are not configuration data.  These values only
    control the Windows-side cache work after the portal has sent an opaque
    asset ID and checksum.
    """
    features = config.get("features", {}) if isinstance(config, dict) else {}
    reply = features.get("reply", {}) if isinstance(features, dict) else {}
    reply = reply if isinstance(reply, dict) else {}
    max_megabytes = _bounded_integer(
        reply.get("reply_image_max_mb", 20), 20, 1, 100
    )
    concurrency = _bounded_integer(
        reply.get("reply_image_download_concurrency", 4), 4, 1, 8
    )
    retry_seconds = _bounded_integer(
        reply.get("reply_image_retry_interval", _DEFAULT_REPLY_IMAGE_RETRY_SECONDS),
        _DEFAULT_REPLY_IMAGE_RETRY_SECONDS,
        30,
        86400,
    )
    return max_megabytes * 1024 * 1024, concurrency, retry_seconds


def _config_has_portal_reply_assets(config: object) -> bool:
    """Return whether a configuration contains Linux-owned reply images.

    This deliberately performs only a cheap structural check.  The stricter
    asset-id/checksum validation remains in ``qqadmin.reply`` immediately
    before a download is made.  Keeping this helper local avoids importing
    the full reply module for every normal synchronization heartbeat.
    """
    if not isinstance(config, dict):
        return False
    replies = config.get("replies")
    if not isinstance(replies, dict):
        return False

    entries: list[object] = []
    exact = replies.get("exact")
    if isinstance(exact, dict):
        entries.extend(exact.values())
    fuzzy = replies.get("fuzzy_keywords")
    if isinstance(fuzzy, dict):
        entries.extend(fuzzy.values())
    regex = replies.get("regex")
    if isinstance(regex, list):
        entries.extend(
            item.get("reply")
            for item in regex
            if isinstance(item, dict)
        )
    return any(
        isinstance(entry, dict) and isinstance(entry.get("image_portal_assets"), list)
        and bool(entry.get("image_portal_assets"))
        for entry in entries
    )


class PortalSyncClient:
    """Local source-of-truth client; the server never connects back to AstrBot."""

    def __init__(
        self,
        base_url: str,
        token: str,
        interval_seconds: int = 10,
        card_pepper: str = "",
        client_id: str = "",
        *,
        scum_query_url: str = "",
        show_connection_events: bool = True,
        runtime_settings_callback: Callable[[dict[str, Any]], Any] | None = None,
        portal_action_callback: Callable[[str, list[dict[str, Any]]], Any] | None = None,
        local_group_ids_callback: Callable[[], Any] | None = None,
        execution_control_callback: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        # SCUM is an isolated read-only path. It may use a dedicated relay
        # origin while configuration and group-management traffic continues
        # using the normal portal origin. Empty keeps backward compatibility.
        self.scum_query_url = str(scum_query_url or "").strip().rstrip("/")
        self.token = str(token or "").strip()
        # Keep web saves responsive without creating a tight polling loop.
        self.interval_seconds = max(2, min(3600, int(interval_seconds or 5)))
        # The server trims its environment value too.  Normalizing copied
        # values here avoids an invisible trailing newline producing a wholly
        # different HMAC fingerprint for every activated card.
        self.card_pepper = str(card_pepper or "").strip()
        self.client_id = str(client_id or "").strip()[:64] or _persistent_client_id()
        # Background heartbeats already have a rate-limited logger warning
        # and the web status page.  Keep the older constructor behaviour for
        # standalone callers, while allowing the Windows plugin to hide the
        # duplicate group-CMD "connection failed/recovered" lines by default.
        self.show_connection_events = bool(show_connection_events)
        # Linux-managed runtime settings are applied locally only after the
        # authenticated exchange succeeds.  The callback must never receive
        # a log-rendered copy, so callers can persist encrypted credentials
        # without exposing them to the console.
        self.runtime_settings_callback = runtime_settings_callback
        self.portal_action_callback = portal_action_callback
        self.local_group_ids_callback = local_group_ids_callback
        self.execution_control_callback = execution_control_callback
        self.last_success_at = 0
        self.last_error = ""
        self._last_error_log_code = ""
        self._last_error_log_at = 0.0
        self._suppressed_error_count = 0
        # When enabled, CMD connectivity notices are emitted only on state
        # transitions; a heartbeat must never flood the local console or
        # portal event journal.
        self._connection_state = ""
        self._last_sync_group_ids: tuple[str, ...] = ()
        # A transient JSON read failure must never be sent as an authoritative
        # empty group list.  Track the state only to emit one useful recovery
        # log instead of a warning every heartbeat.
        self._local_sync_sources_complete = True
        # A locally unreadable auth.json cannot be represented as an empty
        # authorization.  When that source recovers, force one full upload for
        # every currently known group even if its serialized payload is equal
        # to the pre-failure cache; the Linux side may have marked the previous
        # card ``unverified`` and needs a fresh auth projection to restore it.
        self._last_auth_source_complete: bool | None = None
        # The portal may retain a pull hint for a group that was removed from
        # this Windows installation (for example after a local config or
        # authorization cleanup).  Keep a small de-duplicated diagnostic set
        # so that such a request is visible in CMD without flooding it every
        # heartbeat.  Once the group appears locally, the set is cleared and
        # a later hint can be reported again if needed.
        self._reported_unavailable_resync_group_ids: set[str] = set()
        self._last_uploaded: dict[str, dict[str, Any]] = {}
        # A pending web revision can legitimately make the same local
        # projection appear in two exchanges.  Keep the upload itself intact,
        # but rate-limit the human-readable CMD line so a retry cannot flood
        # the Windows terminal.
        self._config_upload_log_state: dict[str, tuple[str, float]] = {}
        # Local commands can change a projection between heartbeats. Keep an
        # explicit dirty set so the Windows change is sent immediately.
        self._forced_config_group_ids: set[str] = set()
        self._manual_web_pull_group_ids: set[str] = set()
        # ``None`` means the server scope has not been negotiated yet.  The
        # first request is deliberately auth-only and contains no group config,
        # runtime summary or message log.  A new portal returns the bound IDs;
        # an older portal omits them and the following request falls back to
        # the legacy full-sync protocol.
        self._server_managed_group_ids: set[str] | None = None
        self._server_scope_supported: bool | None = None
        self._server_claimable_group_ids: set[str] = set()
        # Runtime panels are read-only.  Cache their last acknowledged value
        # independently from editable configuration so a normal heartbeat
        # only transfers groups whose live summary actually changed.
        self._last_runtime_summaries: dict[str, dict[str, Any]] = {}
        # Runtime writes (for example deleting a blacklist record) are kept
        # out of the editable projection.  Mark the affected group so the
        # next bounded summary batch does not have to wait for round-robin
        # rotation through every local group.
        self._runtime_dirty_group_ids: set[str] = set()
        # Keep a member directory pending until the portal explicitly
        # acknowledges the runtime group.  This matters when a refresh races
        # a heartbeat or when an older portal accepts the exchange but drops
        # the read-only runtime field; the next heartbeat must resend it.
        self._runtime_pending_ack_group_ids: set[str] = set()
        # Avoid sending and rewriting an identical member directory twice in
        # the same Windows process. The portal remains authoritative after a
        # restart because the first upload after startup is always sent.
        self._member_directory_hashes: dict[str, str] = {}
        # Round-robin cursor for bounded runtime-summary construction.  This
        # prevents a large installation from spending its entire heartbeat in
        # local runtime queries before it can pull one requested web config.
        self._runtime_summary_cursor = 0
        self._uploaded_console_event_ids: set[str] = set()
        # A successful HTTP exchange is not by itself a console-event ACK:
        # the portal deliberately discards events while its console switch is
        # off.  Keep this state separately so those local journal entries can
        # be retried after an administrator enables the console.
        self._portal_console_enabled = False
        # The portal controls whether readable message previews are retained.
        # Start closed until the first authenticated exchange returns the
        # current server setting, so a newly installed client never uploads
        # chat content by default.
        self._portal_message_content_enabled = False
        # A failed local apply must be visible to the remote portal, but the
        # portal is only reachable from this outbound client.  Keep a compact,
        # revision-scoped report until the next exchange can deliver it.
        self._pending_failure_reports: dict[tuple[str, int], str] = {}
        # Card activation and revocation are user-facing state changes.  Let
        # their callers wake the periodic loop instead of making a second,
        # concurrent HTTP request from the command handler.
        self._sync_requested = asyncio.Event()
        # Reply-image transfers are deliberately decoupled from the exchange
        # and ACK requests.  A large image or a temporarily slow portal must
        # never turn a normal configuration save into a reverse-proxy timeout.
        self._reply_asset_tasks: dict[str, asyncio.Task] = {}
        # This is also the next inexpensive integrity check for a ready cache;
        # it avoids re-statting every reply image on every 30-second heartbeat.
        self._reply_asset_retry_at: dict[str, int] = {}
        # FTP SQLite snapshots are fetched by the same authenticated pull
        # channel, but remain decoupled from exchange/ACK so a large database
        # cannot hold up ordinary configuration synchronization.
        self._database_snapshot_tasks: dict[str, asyncio.Task] = {}
        self._database_snapshot_retry_at: dict[str, int] = {}
        self._database_snapshot_local_cache: dict[str, tuple[int, int, int, str]] = {}
        # Keep the last bounded reason for a missing/failed database pull. The
        # sign-in command can then explain why its authoritative reward store
        # is unavailable instead of reducing every failure to a generic DB
        # error.
        self._database_snapshot_last_errors: dict[str, str] = {}
        # A successful local reward must not be overwritten while FTP upload
        # is temporarily unavailable.  Keep one bounded retry descriptor per
        # group/profile; the next heartbeat retries it before snapshot pulls.
        # Restore this small queue from disk so a plugin restart during an
        # update cannot strand a committed local reward.
        self._database_snapshot_upload_pending: dict[str, dict[str, Any]] = {}
        self._restore_pending_database_snapshot_uploads()
        self._database_snapshot_recovery_checked = False
        # Reuse one keep-alive pool per request class.  Creating an
        # AsyncClient for every heartbeat/asset fetch repeats DNS, TCP and
        # TLS setup and was the largest avoidable delay in remote mode.
        self._http_clients: dict[str, httpx.AsyncClient] = {}

    def _get_http_client(self, kind: str, timeout: Any) -> httpx.AsyncClient:
        """Return a reusable HTTP client for one bounded request class."""
        key = str(kind or "exchange")
        client = self._http_clients.get(key)
        if client is not None and not bool(getattr(client, "is_closed", False)):
            return client
        client_options = {
            "timeout": timeout,
            "follow_redirects": False,
            # Sync must use the configured HTTPS domain directly.  Stale
            # proxy variables on Windows can otherwise add TLS failures.
            "trust_env": False,
        }
        limits_type = getattr(httpx, "Limits", None)
        if callable(limits_type):
            client_options["limits"] = limits_type(
                max_connections=8,
                max_keepalive_connections=4,
                keepalive_expiry=_HTTP_KEEPALIVE_SECONDS,
            )
        client = httpx.AsyncClient(**client_options)
        self._http_clients[key] = client
        return client

    async def _discard_http_client(
        self, kind: str, client: object | None = None
    ) -> None:
        """Drop a failed keep-alive connection before one safe retry.

        A reverse proxy can leave an idle TLS socket half-open after a reload.
        Retrying through the same pool then repeats the same timeout.  Exchange
        and ACK calls are idempotent, so recreating this one client is safer
        than retaining a connection that has already timed out.
        """

        key = str(kind or "exchange")
        current = self._http_clients.get(key)
        target = client if client is not None else current
        if current is target:
            self._http_clients.pop(key, None)
        close = getattr(target, "aclose", None)
        if not callable(close):
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("Unable to discard failed portal HTTP client", exc_info=True)

    @property
    def configuration_error(self) -> str:
        """Return an actionable, non-sensitive configuration problem."""
        try:
            parsed_url = urlsplit(self.base_url)
            # The portal is reached through its HTTPS reverse-proxy domain.
            # Refuse HTTP and literal IPs here as well as in the settings
            # helper so a manually edited config cannot send credentials to a
            # deployment that will redirect to an IP address.
            parsed_port = parsed_url.port
            hostname = (parsed_url.hostname or "").strip().lower()
            valid_url = (
                parsed_url.scheme.lower() == "https"
                and bool(parsed_url.netloc)
                and bool(hostname)
                and parsed_url.username is None
                and parsed_url.password is None
                and not parsed_url.query
                and not parsed_url.fragment
                and (parsed_port is None or 1 <= parsed_port <= 65535)
            )
        except ValueError:
            valid_url = False
            hostname = ""
        if valid_url:
            try:
                valid_url = not bool(ipaddress.ip_address(hostname))
            except ValueError:
                valid_url = bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", hostname))
                valid_url = valid_url and "." in hostname and ".." not in hostname
        if not valid_url:
            return "网页同步地址必须是有效的 HTTPS 域名，不能使用 HTTP 或服务器 IP"
        if len(self.token) < 32 or self.token.lower().startswith("replace_with_"):
            return "同步令牌必须至少 32 位，且不能使用示例值"
        if len(self.card_pepper) < 32 or self.card_pepper.lower().startswith("replace_with_"):
            return "Card Pepper 必须至少 32 位，且须与网页服务器完全一致"
        return ""

    @property
    def configured(self) -> bool:
        return not self.configuration_error

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Joker-Client": self.client_id,
            "X-Joker-Card-Pepper-Proof": _card_pepper_proof(self.card_pepper),
        }

    def request_sync(self) -> None:
        """Request a coalesced synchronization as soon as the current one ends."""
        self._sync_requested.set()

    async def request_web_group_config_pull(self, group_id: object) -> dict[str, Any]:
        normalized_group_id = self._valid_failure_group_id(group_id)
        if not normalized_group_id:
            raise _PortalSyncProtocolError("invalid_pull_target")
        result = await self._post_json(
            "/api/sync/v1/config-pull",
            {"client_id": self.client_id, "group_id": normalized_group_id},
            20.0,
        )
        if result.get("status") != "ok":
            code = str(
                result.get("code", "config_pull_rejected") or "config_pull_rejected"
            )
            raise _PortalSyncProtocolError(code)
        self._manual_web_pull_group_ids.add(normalized_group_id)
        self.request_sync()
        return result

    def mark_runtime_dirty(self, group_id: object) -> None:
        """Prioritize one group in the next read-only runtime upload."""
        normalized = self._valid_failure_group_id(group_id)
        if normalized:
            self._runtime_dirty_group_ids.add(normalized)
            self._runtime_pending_ack_group_ids.add(normalized)
            # A portal refresh can repair a previously rejected/empty
            # projection without changing the normalized member rows. Drop
            # the delta baseline so the next exchange uploads the complete
            # summary again instead of incorrectly treating it as unchanged.
            self._last_runtime_summaries.pop(normalized, None)
            self._member_directory_hashes.pop(normalized, None)

    def mark_config_dirty(self, group_id: object) -> None:
        """Force the next exchange to carry this Windows group projection."""
        normalized = self._valid_failure_group_id(group_id)
        if normalized:
            self._forced_config_group_ids.add(normalized)
            self.request_sync()

    def database_snapshot_error(
        self, group_id: object, profile_id: object
    ) -> str:
        """Return the last safe pull error for one group/database pair."""

        group = self._valid_failure_group_id(group_id)
        profile = str(profile_id or "").strip()
        if not group or not profile:
            return ""
        return str(
            self._database_snapshot_last_errors.get(f"{group}:{profile}", "")
            or ""
        )

    def _runtime_summary_group_ids(
        self,
        config_group_ids: set[str],
        changed_groups: list[dict[str, Any]],
        dirty_group_ids: set[str] | None = None,
    ) -> list[str]:
        """Select a small, fair runtime-summary batch for this exchange.

        Editable configuration is the latency-sensitive part of the protocol:
        a user pressing "保存" or "请求重新同步" must not wait for every
        group to query seven local runtime stores first.  The selected group
        list is deterministic and rotates across normal heartbeats.  Groups
        with a newly changed local configuration go first, so their read-only
        panels are refreshed together with that configuration upload.
        """

        available = sorted(
            group_id
            for group_id in config_group_ids
            if self._valid_failure_group_id(group_id)
        )
        if not available:
            self._runtime_summary_cursor = 0
            return []

        limit = min(_MAX_RUNTIME_SUMMARY_GROUPS_PER_EXCHANGE, len(available))
        selected: list[str] = []
        dirty = sorted(
            group_id
            for group_id in (dirty_group_ids or set())
            if group_id in config_group_ids and group_id in available
        )
        priority_groups = dirty + [
            self._valid_failure_group_id(item.get("group_id"))
            for item in changed_groups
            if isinstance(item, dict)
        ]
        for group_id in priority_groups:
            if group_id and group_id in config_group_ids and group_id not in selected:
                selected.append(group_id)
                if len(selected) >= limit:
                    # Advance after the last selected ID so a dirty/config
                    # priority batch does not starve the fair rotation.
                    self._runtime_summary_cursor = (
                        available.index(group_id) + 1
                    ) % len(available)
                    return selected
        remaining = [group_id for group_id in available if group_id not in selected]
        if not remaining:
            self._runtime_summary_cursor = 0
            return selected

        start = self._runtime_summary_cursor % len(remaining)
        rotated = remaining[start:] + remaining[:start]
        slots = limit - len(selected)
        selected.extend(rotated[:slots])
        self._runtime_summary_cursor = (start + slots) % len(remaining)
        return selected

    @staticmethod
    def _console_event_ids(events: list[dict[str, Any]]) -> set[str]:
        """Return the nonempty locally generated IDs represented by a batch."""

        return {
            event_id
            for event in events
            if isinstance(event, dict)
            for event_id in (str(event.get("event_id", "")).strip(),)
            if event_id
        }

    @classmethod
    def _confirmed_console_event_ids(
        cls,
        result: object,
        events: list[dict[str, Any]],
        *,
        console_enabled: bool,
    ) -> set[str]:
        """Return only events the portal explicitly confirmed receiving.

        ``exchange`` is intentionally allowed to succeed while the portal
        console is disabled.  In that case the server stores no event rows,
        so marking the local IDs uploaded would permanently lose CMD records.
        Current portal releases return the number of newly ingested events;
        accept that acknowledgement only when it covers the exact whole
        batch.  A future portal may return explicit IDs, which is stronger
        and also handles an ambiguous retry after a response was lost.
        """

        if not console_enabled or not isinstance(result, dict) or not events:
            return set()

        sent_ids = cls._console_event_ids(events)
        if len(sent_ids) != len(events):
            # A malformed local event must not let a count ACK acknowledge a
            # different event in the same batch.
            return set()

        raw_ids = result.get("accepted_console_event_ids")
        if isinstance(raw_ids, list):
            return {
                event_id
                for raw_event_id in raw_ids
                for event_id in (str(raw_event_id or "").strip(),)
                if event_id in sent_ids
            }

        accepted = result.get("console_events")
        # bool is an int subclass, but it is never a valid ACK count.
        if isinstance(accepted, bool):
            return set()
        try:
            accepted_count = int(accepted)
        except (TypeError, ValueError, OverflowError):
            return set()
        return sent_ids if accepted_count == len(events) else set()

    @staticmethod
    def _is_retryable_transport_error(exc: Exception) -> bool:
        """Return whether a request can safely be retried once.

        ``httpx.TimeoutException`` is a ``TransportError`` in supported
        httpx releases.  ``getattr`` also keeps the focused unit tests free to
        provide a very small fake httpx module.
        """
        for error_name in ("TimeoutException", "TransportError"):
            transport_error = getattr(httpx, error_name, None)
            if isinstance(transport_error, type) and isinstance(exc, transport_error):
                return True
        return False

    @staticmethod
    def _exception_has_tls_signal(exc: Exception) -> bool:
        """Identify a TLS failure without retaining its unsafe detail text."""

        current: BaseException | None = exc
        seen: set[int] = set()
        for _ in range(4):
            if current is None or id(current) in seen:
                break
            seen.add(id(current))
            class_name = type(current).__name__.lower()
            if any(marker in class_name for marker in ("ssl", "tls", "certificate")):
                return True
            # HTTP client exception text can include the URL, proxy address or
            # credentials.  It is inspected only to derive a fixed code and is
            # never stored, logged, or sent to the portal.
            try:
                detail = str(current).lower()
            except Exception:
                detail = ""
            if any(
                marker in detail
                for marker in (
                    "ssl",
                    "tls",
                    "certificate verify",
                    "cert verify",
                    "wrong version number",
                )
            ):
                return True
            cause = getattr(current, "__cause__", None)
            context = getattr(current, "__context__", None)
            current = cause if isinstance(cause, BaseException) else context
            if not isinstance(current, BaseException):
                current = None
        return False

    @staticmethod
    def _sync_error_summary(exc: Exception) -> str:
        """Return a log-safe error code without URLs, tokens, or response text."""
        if isinstance(exc, _PortalSyncProtocolError):
            return exc.code
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and 100 <= status_code <= 599:
            return f"http_status_{status_code}"
        if PortalSyncClient._exception_has_tls_signal(exc):
            return "tls_handshake_failed"
        error_type = re.sub(r"[^a-z0-9_]", "_", type(exc).__name__.lower()).strip("_")
        if "timeout" in error_type:
            return "timeout"
        if error_type in {
            "readerror",
            "read_error",
            "remoteprotocolerror",
            "remote_protocol_error",
        }:
            return "read_error"
        if PortalSyncClient._is_retryable_transport_error(exc):
            return "transport_error"
        return error_type or "sync_error"

    @staticmethod
    def sync_error_message(error_code: object) -> str:
        """Return a compact, action-oriented explanation for a safe error code."""

        code = str(error_code or "").strip().lower()
        # Older in-memory clients used the HTTPX class name.  Keep rendering
        # it correctly during an in-place plugin update, while new failures
        # always use the canonical ``read_error`` protocol code.
        code = {
            "readerror": "read_error",
            "remoteprotocolerror": "read_error",
        }.get(code, code)
        if code in _SYNC_ERROR_MESSAGES:
            return _SYNC_ERROR_MESSAGES[code]
        if re.fullmatch(r"http_status_5\d\d", code):
            return "网页服务、CDN 或源站暂时不可用，正在按同步间隔重试。"
        if re.fullmatch(r"http_status_4\d\d", code):
            return "网页同步请求被拒绝，请检查同步令牌、远程模式和反向代理规则。"
        return "网页同步暂时失败，正在按同步间隔重试。"

    def _log_sync_failure(self, error_code: str) -> None:
        """Log a failure once, then aggregate identical repeats.

        A persistent CDN 525 used to emit an indistinguishable warning every
        interval.  The client still retries at the configured interval, but
        the console now stays usable and provides the concrete repair area.
        """

        now = time.monotonic()
        changed = error_code != self._last_error_log_code
        due = now - self._last_error_log_at >= _SYNC_ERROR_REPEAT_LOG_SECONDS
        if changed or due:
            repeated = self._suppressed_error_count
            suffix = f"；期间重复 {repeated} 次" if repeated else ""
            logger.warning(
                "网页数据同步失败：%s（%s%s）",
                error_code,
                self.sync_error_message(error_code),
                suffix,
            )
            self._last_error_log_code = error_code
            self._last_error_log_at = now
            self._suppressed_error_count = 0
        else:
            self._suppressed_error_count += 1

    def _note_sync_recovery(self) -> None:
        """Make a later successful retry visible without repeating failures."""

        if self._last_error_log_code:
            logger.debug("网页数据同步已恢复：%s", self._last_error_log_code)
        self._last_error_log_code = ""
        self._last_error_log_at = 0.0
        self._suppressed_error_count = 0

    async def _post_json(
        self,
        endpoint: str,
        payload: dict[str, Any],
        timeout: Any,
        *,
        client_kind: str = "exchange",
    ) -> dict[str, Any]:
        """POST one idempotent sync operation with a bounded retry.

        A timeout can happen after the portal has committed a request.  Both
        exchange and ACK payloads are idempotent, so one retry substantially
        improves flaky residential-network and reverse-proxy behaviour while
        avoiding an unbounded request storm.
        """
        for attempt in range(2):
            client = self._get_http_client(client_kind, timeout)
            try:
                response = await client.post(
                    f"{self.base_url}{endpoint}",
                    headers=self.headers,
                    json=payload,
                )
                status_code = getattr(response, "status_code", None)
                if isinstance(status_code, int) and 300 <= status_code < 400:
                    # The binding deliberately refuses redirects.  A
                    # redirect can indicate a domain/CDN misconfiguration;
                    # following it could send the bearer token elsewhere.
                    raise _PortalSyncProtocolError("unexpected_redirect")
                if isinstance(status_code, int) and status_code >= 400:
                    try:
                        error_payload = response.json()
                    except Exception:
                        error_payload = None
                    error_code = ""
                    if isinstance(error_payload, dict):
                        error_code = str(error_payload.get("code", "")).strip().lower()
                    if status_code >= 500 and attempt == 0:
                        await self._discard_http_client(client_kind, client)
                        await asyncio.sleep(_SYNC_RETRY_DELAY_SECONDS)
                        continue
                    if re.fullmatch(r"[a-z][a-z0-9_]{2,63}", error_code):
                        raise _PortalSyncProtocolError(error_code)
                    if (
                        endpoint == "/api/sync/v1/config-pull"
                        and status_code == 404
                    ):
                        raise _PortalSyncProtocolError("config_pull_endpoint_missing")
                    # Convert HTTP failures to stable protocol codes instead
                    # of calling raise_for_status(). Older HTTPX/adapter
                    # combinations can raise a secondary TypeError when a
                    # response has no request metadata, hiding the real
                    # Linux endpoint/version problem.
                    raise _PortalSyncProtocolError(
                        f"http_status_{status_code}"
                    )
                try:
                    result = response.json()
                except (TypeError, ValueError, UnicodeDecodeError) as exc:
                    # A reverse proxy returning HTML is a stable protocol
                    # failure, not a raw response body to expose.
                    raise _PortalSyncProtocolError("invalid_json_response") from exc
                if not isinstance(result, dict):
                    raise _PortalSyncProtocolError("invalid_json_response")
                return result
            except _PortalSyncProtocolError:
                raise
            except TypeError as exc:
                # Keep the user-facing error actionable when an older HTTPX
                # or adapter raises a raw TypeError before a response is
                # decoded. The traceback remains available in debug logs.
                logger.debug(
                    "网页同步请求出现 HTTP 客户端兼容性异常 endpoint=%s",
                    endpoint,
                    exc_info=True,
                )
                raise _PortalSyncProtocolError(
                    "request_compatibility_error"
                ) from exc
            except Exception as exc:
                error_response = getattr(exc, "response", None)
                error_status = getattr(error_response, "status_code", None)
                retryable_status = isinstance(error_status, int) and error_status >= 500
                if attempt == 0 and (
                    retryable_status
                    or self._is_retryable_transport_error(exc)
                    # HTTPX normally wraps TLS failures in TransportError;
                    # one retry is still useful for a transient handshake.
                    or self._exception_has_tls_signal(exc)
                ):
                    await self._discard_http_client(client_kind, client)
                    await asyncio.sleep(_SYNC_RETRY_DELAY_SECONDS)
                    continue
                raise
        raise _PortalSyncProtocolError("request_retry_exhausted")

    async def upload_member_directory(
        self,
        group_id: object,
        members: list[dict[str, Any]],
        *,
        levels_pending: bool = False,
        snapshot_complete: bool = True,
        expected_count: int = 0,
    ) -> dict[str, Any]:
        """Upload a freshly fetched QQ directory through the fast path."""

        normalized_group = self._valid_failure_group_id(group_id)
        if not normalized_group:
            raise _PortalSyncProtocolError("invalid_member_directory_scope")
        rows = [item for item in list(members or [])[:10000] if isinstance(item, dict)]
        canonical_rows = sorted(
            rows,
            key=lambda item: str(item.get("user_id", item.get("userId", ""))),
        )
        fingerprint_payload = json.dumps(
            {
                "members": canonical_rows,
                "levels_pending": bool(levels_pending),
                "snapshot_complete": bool(snapshot_complete),
                "expected_count": max(0, min(10000, int(expected_count or 0))),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        fingerprint = hashlib.sha256(fingerprint_payload).hexdigest()
        if self._member_directory_hashes.get(normalized_group) == fingerprint:
            return {
                "status": "ok",
                "stored_count": len(rows),
                "unchanged": True,
            }
        result = await self._post_json(
            "/api/sync/v1/member-directory",
            {
                "client_id": self.client_id,
                "group_id": normalized_group,
                "members": rows,
                "levels_pending": bool(levels_pending),
                "snapshot_complete": bool(snapshot_complete),
                "expected_count": max(0, min(10000, int(expected_count or 0))),
            },
            # Member refresh is a fast-path UI operation.  Do not let a slow
            # reverse proxy hold the Windows action queue for the full
            # heartbeat timeout; the normal exchange still retries this
            # directory on the next sync cycle.
            # A large group directory can contain hundreds of JSON rows. Keep
            # the fast path bounded, but allow a slow reverse proxy enough
            # time to receive and commit the snapshot before marking it for
            # background retry.
            httpx.Timeout(20.0, connect=5.0),
            client_kind="member_directory",
        )
        if str(result.get("status", "")).strip().lower() != "ok":
            raise _PortalSyncProtocolError(
                str(result.get("code", "member_directory_rejected"))
            )
        self._member_directory_hashes[normalized_group] = fingerprint
        # The dedicated endpoint is already the durable acknowledgement for
        # this read-only panel. Leaving the heartbeat pending marker set makes
        # every 30-second exchange rebuild and upload the same 846 rows.
        self._runtime_pending_ack_group_ids.discard(normalized_group)
        self._runtime_dirty_group_ids.discard(normalized_group)
        try:
            self._last_runtime_summaries[normalized_group] = build_runtime_summary(
                normalized_group
            )
        except Exception:
            # The fast path has succeeded; a summary cache miss must not cause
            # the member directory to be resent indefinitely.
            self._last_runtime_summaries.pop(normalized_group, None)
        return result

    async def upload_message_cache(
        self,
        group_id: object,
        records: list[dict[str, Any]],
        *,
        complete_user_id: str = "",
        complete_count: int | None = None,
    ) -> dict[str, Any]:
        """Push newly written message summaries without waiting for heartbeat."""
        normalized_group = self._valid_failure_group_id(group_id)
        private_rows = [
            raw for raw in list(records or [])
            if isinstance(raw, dict) and str(raw.get("scope_type", "")).lower() == "private"
        ]
        is_private = bool(private_rows) and not normalized_group
        if not normalized_group and not is_private:
            raise _PortalSyncProtocolError("invalid_message_cache_scope")
        rows: list[dict[str, Any]] = []
        for raw in list(records or []):
            projected = _message_cache_projection(raw)
            if projected is not None:
                rows.append(projected)
        normalized_complete_user = str(complete_user_id or "").strip()
        has_completed_member = bool(
            normalized_complete_user.isdigit()
            and 5 <= len(normalized_complete_user) <= 20
        )
        # A member can legitimately have no retained local records.  Still
        # send this small completion envelope so Linux can silently correct a
        # stale count instead of leaving the page stuck at an old number.
        if not rows and not has_completed_member:
            return {"status": "ok", "stored_count": 0, "unchanged": True}
        payload = {
            "client_id": self.client_id,
            "group_id": normalized_group,
            "messages": rows,
        }
        if has_completed_member:
            payload["complete_user_id"] = normalized_complete_user
            try:
                payload["complete_count"] = max(0, min(5000, int(complete_count or 0)))
            except (TypeError, ValueError, OverflowError):
                payload["complete_count"] = 0
        if is_private:
            payload["scope_type"] = "private"
            payload["scope_id"] = str(
                private_rows[0].get("private_user_id")
                or private_rows[0].get("user_id")
                or ""
            )[:32]
        result = await self._post_json(
            "/api/sync/v1/message-cache",
            payload,
            httpx.Timeout(8.0, connect=3.0),
            client_kind="message_cache",
        )
        if str(result.get("status", "")).strip().lower() != "ok":
            raise _PortalSyncProtocolError(
                str(result.get("code", "message_cache_rejected"))
            )
        return result

    async def fetch_reply_asset(
        self, asset_id: object, *, max_image_bytes: int = _DEFAULT_REPLY_IMAGE_MAX_BYTES
    ) -> tuple[bytes, str]:
        """Fetch one portal-owned reply image using the normal sync identity.

        The configuration contains neither a URL nor image bytes, only an
        opaque numeric asset id and a SHA-256 checksum.  This request is
        therefore authenticated with the same headers as ``exchange`` and
        follows no redirects.  Streaming with a bounded accumulator prevents
        a malformed origin response from expanding a Windows process's memory
        before ``qqadmin.reply`` can validate the image.
        """
        try:
            normalized_asset_id = int(asset_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _PortalSyncProtocolError("invalid_reply_asset_id") from exc
        if not 0 < normalized_asset_id <= _MAX_PORTAL_REPLY_ASSET_ID:
            raise _PortalSyncProtocolError("invalid_reply_asset_id")
        maximum = _bounded_integer(
            max_image_bytes,
            _DEFAULT_REPLY_IMAGE_MAX_BYTES,
            1024 * 1024,
            100 * 1024 * 1024,
        )
        endpoint = f"/api/sync/v1/reply-assets/{normalized_asset_id}"
        timeout = httpx.Timeout(30.0, connect=8.0)
        client = self._get_http_client("reply_asset", timeout)

        for attempt in range(2):
            try:
                # ``stream`` avoids accepting an unbounded response body into
                # memory. The endpoint must never redirect to a user host.
                async with client.stream(
                    "GET", f"{self.base_url}{endpoint}", headers=self.headers
                ) as response:
                    status_code = getattr(response, "status_code", None)
                    if status_code in {401, 409}:
                        try:
                            error_payload = response.json()
                        except Exception:
                            error_payload = None
                        if isinstance(error_payload, dict):
                            code = str(error_payload.get("code", "")).strip().lower()
                            if code in {
                                "sync_token_invalid",
                                "card_pepper_mismatch",
                                "remote_mode_disabled",
                            }:
                                raise _PortalSyncProtocolError(code)
                    if isinstance(status_code, int) and status_code >= 500:
                        if attempt == 0:
                            await asyncio.sleep(_SYNC_RETRY_DELAY_SECONDS)
                            continue
                    response.raise_for_status()
                    headers = getattr(response, "headers", {})
                    try:
                        declared_size = int(headers.get("content-length", 0) or 0)
                    except (TypeError, ValueError, OverflowError):
                        declared_size = 0
                    if declared_size > maximum:
                        raise _PortalSyncProtocolError("reply_asset_too_large")
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in response.aiter_bytes():
                        if not isinstance(chunk, bytes):
                            raise _PortalSyncProtocolError("invalid_reply_asset_body")
                        received += len(chunk)
                        if received > maximum:
                            raise _PortalSyncProtocolError("reply_asset_too_large")
                        chunks.append(chunk)
                    content_type = str(headers.get("content-type", "") or "")
                return b"".join(chunks), content_type.split(";", 1)[0].strip().lower()
            except _PortalSyncProtocolError:
                raise
            except Exception as exc:
                error_response = getattr(exc, "response", None)
                error_status = getattr(error_response, "status_code", None)
                retryable_status = isinstance(error_status, int) and error_status >= 500
                if attempt == 0 and (
                    retryable_status
                    or self._is_retryable_transport_error(exc)
                    or self._exception_has_tls_signal(exc)
                ):
                    await asyncio.sleep(_SYNC_RETRY_DELAY_SECONDS)
                    continue
                raise
        raise _PortalSyncProtocolError("reply_asset_retry_exhausted")

    async def fetch_database_snapshot(
        self,
        group_id: object,
        profile_id: object,
        expected_revision: int = 0,
        expected_sha256: str = "",
        *,
        max_bytes: int = _MAX_PORTAL_DATABASE_SNAPSHOT_BYTES,
    ) -> tuple[bytes, int, str]:
        """Fetch and integrity-check one portal-retained SQLite snapshot."""

        normalized_group = self._valid_failure_group_id(group_id)
        normalized_profile = str(profile_id or "").strip()
        if not normalized_group:
            raise _PortalSyncProtocolError("invalid_database_snapshot_scope")
        if not _DATABASE_PROFILE_ID_RE.fullmatch(normalized_profile):
            raise _PortalSyncProtocolError("invalid_database_snapshot_scope")
        try:
            revision = int(expected_revision or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _PortalSyncProtocolError("invalid_database_snapshot_revision") from exc
        if revision < 0 or revision > 2**31 - 1:
            raise _PortalSyncProtocolError("invalid_database_snapshot_revision")
        expected_hash = str(expected_sha256 or "").strip().lower()
        if expected_hash and not _DATABASE_SNAPSHOT_SHA256_RE.fullmatch(expected_hash):
            raise _PortalSyncProtocolError("invalid_database_snapshot_hash")
        maximum = _bounded_integer(
            max_bytes,
            _MAX_PORTAL_DATABASE_SNAPSHOT_BYTES,
            1,
            _MAX_PORTAL_DATABASE_SNAPSHOT_BYTES,
        )
        endpoint = f"/api/sync/v1/database-snapshots/{normalized_group}/{normalized_profile}"
        timeout = httpx.Timeout(60.0, connect=8.0)
        client = self._get_http_client("database_snapshot", timeout)

        for attempt in range(2):
            try:
                async with client.stream(
                    "GET", f"{self.base_url}{endpoint}", headers=self.headers
                ) as response:
                    status_code = getattr(response, "status_code", None)
                    if status_code in {401, 403, 404, 409}:
                        # ``client.stream`` has not read the body yet.  Calling
                        # ``response.json()`` at this point raises
                        # httpx.ResponseNotRead in production, which used to
                        # hide the portal's useful 409 ``code`` and leave only
                        # the misleading generic ``http_status_409`` in CMD.
                        error_payload = await self._read_stream_error_payload(response)
                        if isinstance(error_payload, dict):
                            code = str(error_payload.get("code", "")).strip().lower()
                            if re.fullmatch(r"[a-z0-9_]{1,64}", code):
                                raise _PortalSyncProtocolError(code)
                        if status_code == 401:
                            raise _PortalSyncProtocolError("database_snapshot_http_401")
                        if status_code == 403:
                            raise _PortalSyncProtocolError("database_snapshot_http_403")
                        if status_code == 409:
                            raise _PortalSyncProtocolError("database_snapshot_http_409")
                    if isinstance(status_code, int) and status_code >= 500:
                        if attempt == 0:
                            await asyncio.sleep(_SYNC_RETRY_DELAY_SECONDS)
                            continue
                    response.raise_for_status()
                    headers = getattr(response, "headers", {})
                    try:
                        declared_size = int(headers.get("content-length", 0) or 0)
                    except (TypeError, ValueError, OverflowError):
                        declared_size = 0
                    if declared_size > maximum:
                        raise _PortalSyncProtocolError("database_snapshot_too_large")
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in response.aiter_bytes():
                        if not isinstance(chunk, bytes):
                            raise _PortalSyncProtocolError(
                                "invalid_database_snapshot_body"
                            )
                        received += len(chunk)
                        if received > maximum:
                            raise _PortalSyncProtocolError("database_snapshot_too_large")
                        chunks.append(chunk)
                    if declared_size and declared_size != received:
                        raise _PortalSyncProtocolError(
                            "database_snapshot_size_mismatch"
                        )
                    try:
                        response_revision = int(
                            headers.get(
                                "x-database-snapshot-revision",
                                headers.get("x-snapshot-revision", 0),
                            )
                            or 0
                        )
                    except (TypeError, ValueError, OverflowError):
                        response_revision = 0
                    response_etag = str(headers.get("etag", "") or "").strip()
                    response_etag = response_etag.strip('"')
                data = b"".join(chunks)
                if response_revision <= 0:
                    raise _PortalSyncProtocolError("invalid_database_snapshot_revision")
                if revision and response_revision != revision:
                    raise _PortalSyncProtocolError("database_snapshot_revision_mismatch")
                digest = hashlib.sha256(data).hexdigest()
                if expected_hash and not hmac.compare_digest(digest, expected_hash):
                    raise _PortalSyncProtocolError("database_snapshot_hash_mismatch")
                if response_etag and _DATABASE_SNAPSHOT_SHA256_RE.fullmatch(response_etag):
                    if not hmac.compare_digest(response_etag.lower(), digest):
                        raise _PortalSyncProtocolError("database_snapshot_hash_mismatch")
                if data[:16] != b"SQLite format 3\x00":
                    raise _PortalSyncProtocolError("invalid_database_snapshot_body")
                return data, response_revision, digest
            except _PortalSyncProtocolError:
                raise
            except Exception as exc:
                error_response = getattr(exc, "response", None)
                error_status = getattr(error_response, "status_code", None)
                retryable_status = isinstance(error_status, int) and error_status >= 500
                if attempt == 0 and (
                    retryable_status
                    or self._is_retryable_transport_error(exc)
                    or self._exception_has_tls_signal(exc)
                ):
                    await asyncio.sleep(_SYNC_RETRY_DELAY_SECONDS)
                    continue
                raise
        raise _PortalSyncProtocolError("database_snapshot_retry_exhausted")

    @staticmethod
    async def _read_stream_error_payload(response: object) -> dict[str, Any] | None:
        """Read a bounded JSON error body from an ``httpx.stream`` response.

        The helper deliberately accepts the older test/compatibility response
        shape where ``json()`` is already available.  Real httpx responses are
        read first so protocol codes such as ``database_snapshot_missing`` are
        not lost behind ``ResponseNotRead``.
        """

        aread = getattr(response, "aread", None)
        if callable(aread):
            try:
                body = await aread()
                if isinstance(body, bytes) and len(body) <= _MAX_PORTAL_ERROR_BODY_BYTES:
                    try:
                        payload = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError, TypeError):
                        payload = None
                    return payload if isinstance(payload, dict) else None
                return None
            except Exception:
                # Fall through for lightweight legacy response doubles.  The
                # HTTP status remains actionable even if the body is unreadable.
                pass
        try:
            payload = response.json()  # type: ignore[attr-defined]
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _local_database_snapshot_path(path: object) -> Path | None:
        """Resolve one Windows snapshot path beneath the plugin root.

        The sign-in adapter keeps the already-resolved local ``Path`` on its
        result object, while web configuration descriptors normally contain a
        relative path. Accept both representations, but never allow an
        absolute path outside the plugin root to become an upload source.
        """

        raw_path = str(path or "").strip().replace("\\", "/")
        parts = [part for part in raw_path.split("/") if part not in {"", "."}]
        if not parts or any(ord(char) < 32 for char in raw_path):
            return None
        try:
            root = Path(__file__).resolve().parent.parent
            candidate = Path(raw_path)
            if candidate.is_absolute():
                # Absolute paths are only accepted when they resolve beneath
                # this plugin root. This supports the adapter's resolved
                # Path without weakening the upload boundary.
                target = candidate.resolve()
            else:
                if any(
                    part == ".." or ":" in part
                    for part in parts
                ):
                    return None
                target = (root / "/".join(parts)).resolve()
            target.relative_to(root)
            sidecar = (
                target
                if target.name.endswith(".joker-sync")
                else target.with_name(target.name + ".joker-sync")
            )
            try:
                if sidecar.is_file() and (
                    not target.is_file()
                    or sidecar.stat().st_mtime_ns >= target.stat().st_mtime_ns
                ):
                    return sidecar
            except OSError:
                pass
            return target
        except (OSError, RuntimeError, ValueError):
            return None

    @staticmethod
    def _database_snapshot_pending_path() -> Path:
        """Return the private local queue used to survive plugin restarts."""

        root = Path(__file__).resolve().parent.parent
        return root / "data" / "plugin_data" / "qqadmin" / _DATABASE_SNAPSHOT_PENDING_FILE

    def _restore_pending_database_snapshot_uploads(self) -> None:
        """Restore bounded failed-upload descriptors from the local queue."""

        try:
            payload = json.loads(
                self._database_snapshot_pending_path().read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        for key, item in list(payload.items())[:100]:
            if not isinstance(key, str) or not isinstance(item, dict):
                continue
            group_id = self._valid_failure_group_id(item.get("group_id"))
            profile_id = str(item.get("profile_id", "") or "").strip()
            path = str(item.get("path", "") or "").strip()
            try:
                revision = int(item.get("revision", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            digest = str(item.get("sha256", "") or "").strip().lower()
            if (
                not group_id
                or not _DATABASE_PROFILE_ID_RE.fullmatch(profile_id)
                or not path
                or revision <= 0
                or not _DATABASE_SNAPSHOT_SHA256_RE.fullmatch(digest)
            ):
                continue
            self._database_snapshot_upload_pending[f"{group_id}:{profile_id}"] = {
                "group_id": group_id,
                "profile_id": profile_id,
                "path": path,
                "revision": revision,
                "sha256": digest,
            }

    def _persist_pending_database_snapshot_uploads(self) -> None:
        """Atomically persist the small failed-upload queue without secrets."""

        target = self._database_snapshot_pending_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = {
                key: value
                for key, value in list(self._database_snapshot_upload_pending.items())[:100]
                if isinstance(value, dict)
            }
            if not descriptor:
                target.unlink(missing_ok=True)
                return
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(descriptor, stream, ensure_ascii=False, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, target)
            finally:
                temporary_path.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError):
            logger.warning("网页数据库快照补传队列保存失败")

    def _remember_pending_database_snapshot_upload(
        self, pending_key: str, descriptor: dict[str, Any]
    ) -> None:
        self._database_snapshot_upload_pending[pending_key] = descriptor
        self._persist_pending_database_snapshot_uploads()

    def _forget_pending_database_snapshot_upload(self, pending_key: str) -> None:
        if pending_key in self._database_snapshot_upload_pending:
            self._database_snapshot_upload_pending.pop(pending_key, None)
            self._persist_pending_database_snapshot_uploads()

    async def validate_database_steam_id(
        self,
        group_id: object,
        profile_id: object,
        steam_id: str,
    ) -> dict[str, Any]:
        """Validate one SteamID against the latest FTP database on Linux."""

        normalized_group = self._valid_failure_group_id(group_id)
        normalized_profile = str(profile_id or "").strip()
        normalized_steam = str(steam_id or "").strip()
        if (
            not normalized_group
            or not _DATABASE_PROFILE_ID_RE.fullmatch(normalized_profile)
            or not re.fullmatch(r"[0-9]{17}", normalized_steam)
        ):
            raise _PortalSyncProtocolError("invalid_steam_id")
        endpoint = f"/api/sync/v1/database-members/{normalized_group}/{normalized_profile}/{normalized_steam}"
        client = self._get_http_client(
            "database_member_lookup", httpx.Timeout(60.0, connect=8.0)
        )
        try:
            response = await client.get(f"{self.base_url}{endpoint}", headers=self.headers)
            if getattr(response, "status_code", None) in {400, 401, 403, 404, 409, 502, 503}:
                try:
                    payload = response.json()
                except Exception:
                    payload = {}
                code = str(payload.get("code", "") if isinstance(payload, dict) else "").strip().lower()
                if code:
                    raise _PortalSyncProtocolError(
                        code,
                        payload.get("message", payload.get("detail", "")) if isinstance(payload, dict) else "",
                    )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise _PortalSyncProtocolError("query_error")
            return result
        except _PortalSyncProtocolError:
            raise
        except Exception as exc:
            raise _PortalSyncProtocolError("db_unavailable") from exc

    async def query_scum(
        self,
        group_id: object,
        operation: str,
        argument: object = "",
    ) -> dict[str, Any]:
        """Run one SCUM query on Linux instead of the Windows host."""

        normalized_group = self._valid_failure_group_id(group_id)
        normalized_operation = str(operation or "").strip().lower()
        if not normalized_group or normalized_operation not in {"search", "detail"}:
            raise _PortalSyncProtocolError("invalid_scum_query")
        value = str(argument or "").strip()
        query_base_url = self.scum_query_url or self.base_url
        if not query_base_url:
            raise _PortalSyncProtocolError("scum_query_failed", "SCUM 中转地址未配置")
        try:
            parsed_query_url = urlsplit(query_base_url)
            query_hostname = (parsed_query_url.hostname or "").strip().lower()
            query_port = parsed_query_url.port
            valid_query_url = (
                parsed_query_url.scheme.lower() == "https"
                and bool(parsed_query_url.netloc)
                and bool(query_hostname)
                and parsed_query_url.username is None
                and parsed_query_url.password is None
                and not parsed_query_url.query
                and not parsed_query_url.fragment
                and (query_port is None or 1 <= query_port <= 65535)
            )
        except ValueError:
            valid_query_url = False
            query_hostname = ""
        if valid_query_url:
            try:
                ipaddress.ip_address(query_hostname)
                valid_query_url = False
            except ValueError:
                valid_query_url = bool(
                    re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", query_hostname)
                ) and "." in query_hostname and ".." not in query_hostname
        if not valid_query_url:
            raise _PortalSyncProtocolError(
                "invalid_scum_relay_url",
                "SCUM 中转地址必须是 HTTPS 域名，不能包含账号、密码、查询参数或 IP 地址",
            )
        endpoint = f"/api/sync/v1/scum/query/{normalized_group}"
        client = self._get_http_client(
            "scum_query", httpx.Timeout(75.0, connect=8.0)
        )
        headers = dict(self.headers)
        headers["Content-Type"] = "application/json"
        try:
            response = await client.post(
                f"{query_base_url}{endpoint}",
                headers=headers,
                json={
                    "client_id": self.client_id,
                    "operation": normalized_operation,
                    "argument": value[:128],
                },
            )
            if getattr(response, "status_code", None) in {
                400,
                401,
                403,
                404,
                409,
                502,
                503,
            }:
                try:
                    payload = response.json()
                except Exception:
                    payload = {}
                code = str(
                    payload.get("code", "") if isinstance(payload, dict) else ""
                ).strip().lower()
                if code:
                    detail = (
                        payload.get("detail", payload.get("message", ""))
                        if isinstance(payload, dict)
                        else ""
                    )
                    raise _PortalSyncProtocolError(code, detail)
                status_code = getattr(response, "status_code", None)
                if status_code == 404:
                    raise _PortalSyncProtocolError(
                        "group_not_synced",
                        "网页尚未收到本群最新配置，请等待 Windows 同步完成后重试",
                    )
                if status_code in {401, 403}:
                    raise _PortalSyncProtocolError(
                        "sync_authentication",
                        "网页同步令牌或 Card Pepper 校验失败",
                    )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not result.get("success", True):
                raise _PortalSyncProtocolError("scum_query_failed")
            return result
        except _PortalSyncProtocolError:
            raise
        except Exception as exc:
            raise _PortalSyncProtocolError("scum_query_failed") from exc

    async def rename_database_member(
        self,
        group_id: object,
        profile_id: object,
        steam_id: str,
        player_name: str,
    ) -> dict[str, Any]:
        """Rename a validated player on Linux without downloading the DB."""

        normalized_group = self._valid_failure_group_id(group_id)
        normalized_profile = str(profile_id or "").strip()
        normalized_steam = str(steam_id or "").strip()
        if (
            not normalized_group
            or not _DATABASE_PROFILE_ID_RE.fullmatch(normalized_profile)
            or not re.fullmatch(r"[0-9]{17}", normalized_steam)
        ):
            raise _PortalSyncProtocolError("invalid_rename")
        endpoint = f"/api/sync/v1/database-members/{normalized_group}/{normalized_profile}/{normalized_steam}/rename"
        client = self._get_http_client("database_member_rename", httpx.Timeout(120.0, connect=8.0))
        headers = dict(self.headers)
        headers["Content-Type"] = "application/json"
        try:
            response = await client.post(
                f"{self.base_url}{endpoint}",
                headers=headers,
                json={"player_name": str(player_name or "")[:255]},
            )
            if getattr(response, "status_code", None) in {400, 401, 403, 404, 409, 502, 503}:
                try:
                    payload = response.json()
                except Exception:
                    payload = {}
                code = str(payload.get("code", "") if isinstance(payload, dict) else "").strip().lower()
                if code:
                    raise _PortalSyncProtocolError(
                        code,
                        payload.get("message", payload.get("detail", "")) if isinstance(payload, dict) else "",
                    )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise _PortalSyncProtocolError("database_mutation_failed")
            return result
        except _PortalSyncProtocolError:
            raise
        except Exception as exc:
            raise _PortalSyncProtocolError("database_mutation_failed") from exc

    async def mutate_database_signin(
        self,
        group_id: object,
        profile_id: object,
        *,
        steam_id: str,
        reward: int,
        player_name: str = "",
        sign_date: str = "",
        currency_column: str = "amount",
        period_mode: str = "daily",
        reward_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply one sign-in transaction on the Linux portal database.

        Only the mutation intent crosses the Windows boundary. The portal
        reads the latest FTP SQLite file and performs a compare-and-swap write,
        so another user's change is never replaced by a stale Windows file.
        """

        normalized_group = self._valid_failure_group_id(group_id)
        normalized_profile = str(profile_id or "").strip()
        if not normalized_group or not _DATABASE_PROFILE_ID_RE.fullmatch(normalized_profile):
            raise _PortalSyncProtocolError("invalid_database_snapshot_scope")
        body = {
            "steam_id": str(steam_id or "").strip(),
            "reward": int(reward),
            "player_name": str(player_name or "")[:128],
            "sign_date": str(sign_date or "")[:32],
            "currency_column": str(currency_column or "amount")[:32],
            "period_mode": str(period_mode or "daily")[:16],
            "reward_config": dict(reward_config) if isinstance(reward_config, Mapping) else {},
        }
        endpoint = f"/api/sync/v1/database-mutations/{normalized_group}/{normalized_profile}"
        client = self._get_http_client(
            "database_mutation", httpx.Timeout(120.0, connect=8.0)
        )
        headers = dict(self.headers)
        headers["Content-Type"] = "application/json"
        try:
            response = await client.post(
                f"{self.base_url}{endpoint}", headers=headers, json=body
            )
            status_code = getattr(response, "status_code", None)
            if status_code in {400, 401, 403, 404, 409, 502, 503}:
                try:
                    payload = response.json()
                except Exception:
                    payload = {}
                code = str(payload.get("code", "") if isinstance(payload, dict) else "").strip().lower()
                if code:
                    raise _PortalSyncProtocolError(
                        code,
                        payload.get("message", payload.get("detail", "")) if isinstance(payload, dict) else "",
                    )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise _PortalSyncProtocolError("database_mutation_failed")
            return result
        except _PortalSyncProtocolError:
            raise
        except Exception as exc:
            raise _PortalSyncProtocolError("database_mutation_failed") from exc

    async def reset_database_signin(
        self,
        group_id: object,
        profile_id: object,
        *,
        steam_id: str,
        sign_date: str,
        reward: int | None = None,
        remove_reward: bool = True,
        currency_column: str = "amount",
    ) -> dict[str, Any]:
        """Remove one authoritative server sign-in row without a Windows snapshot upload."""

        normalized_group = self._valid_failure_group_id(group_id)
        normalized_profile = str(profile_id or "").strip()
        if not normalized_group or not _DATABASE_PROFILE_ID_RE.fullmatch(normalized_profile):
            raise _PortalSyncProtocolError("invalid_database_snapshot_scope")
        body = {
            "steam_id": str(steam_id or "").strip(),
            "sign_date": str(sign_date or "")[:32],
            "reward": reward,
            "remove_reward": bool(remove_reward),
            "currency_column": str(currency_column or "amount")[:32],
        }
        endpoint = f"/api/sync/v1/database-mutations/{normalized_group}/{normalized_profile}/reset"
        client = self._get_http_client(
            "database_mutation", httpx.Timeout(120.0, connect=8.0)
        )
        headers = dict(self.headers)
        headers["Content-Type"] = "application/json"
        try:
            response = await client.post(f"{self.base_url}{endpoint}", headers=headers, json=body)
            if getattr(response, "status_code", None) in {400, 401, 403, 404, 409, 502, 503}:
                try:
                    payload = response.json()
                except Exception:
                    payload = {}
                code = str(payload.get("code", "") if isinstance(payload, dict) else "").strip().lower()
                if code:
                    raise _PortalSyncProtocolError(
                        code,
                        payload.get("message", payload.get("detail", "")) if isinstance(payload, dict) else "",
                    )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise _PortalSyncProtocolError("database_mutation_failed")
            return result
        except _PortalSyncProtocolError:
            raise
        except Exception as exc:
            raise _PortalSyncProtocolError("database_mutation_failed") from exc

    async def _recover_unacknowledged_local_database_snapshots(
        self, groups: object
    ) -> None:
        """Recover a committed local reward left by an older client build.

        Before 1.11.112 failed uploads lived only in memory. On the first
        heartbeat after an upgrade, compare each configured local SQLite file
        with its last portal hash and enqueue one safe, base-hash-protected
        upload when the local file is newer. A remote revision conflict is
        discarded so the normal pull path can replace the stale local copy.
        """

        if self._database_snapshot_recovery_checked:
            return
        self._database_snapshot_recovery_checked = True
        if not isinstance(groups, dict):
            return
        for group_id, config in groups.items():
            safe_group_id = self._valid_failure_group_id(group_id)
            if not safe_group_id:
                continue
            for item in self._database_snapshot_requests(config):
                pending_key = f"{safe_group_id}:{item['profile_id']}"
                if pending_key in self._database_snapshot_upload_pending:
                    continue
                local_path = self._local_database_snapshot_path(item["path"])
                if local_path is None or not local_path.is_file():
                    continue
                try:
                    data = local_path.read_bytes()
                except OSError:
                    continue
                if (
                    not data
                    or len(data) > _MAX_PORTAL_DATABASE_SNAPSHOT_BYTES
                    or data[:16] != b"SQLite format 3\x00"
                ):
                    continue
                digest = hashlib.sha256(data).hexdigest()
                if hmac.compare_digest(digest, item["sha256"]):
                    continue
                self._remember_pending_database_snapshot_upload(
                    pending_key,
                    {
                        "group_id": safe_group_id,
                        "profile_id": item["profile_id"],
                        "path": item["path"],
                        "revision": item["revision"],
                        "sha256": item["sha256"],
                    },
                )
                _print_sync_group_action(
                    "网页数据库快照补传",
                    safe_group_id,
                    detail=(
                        f"数据库 {item['profile_name']}；检测到 Windows 本地 SQLite 已更新，"
                        "正在按网页版本和基础哈希安全回传"
                    ),
                )

    async def upload_database_snapshot(
        self,
        group_id: object,
        profile_id: object,
        path: object,
        expected_revision: object,
        expected_sha256: object,
        *,
        _record_pending: bool = True,
    ) -> dict[str, Any]:
        """Upload one changed local SQLite snapshot through the portal."""

        normalized_group = self._valid_failure_group_id(group_id)
        normalized_profile = str(profile_id or "").strip()
        try:
            revision = int(expected_revision or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _PortalSyncProtocolError("invalid_database_snapshot_revision") from exc
        expected = str(expected_sha256 or "").strip().lower()
        local_path = self._local_database_snapshot_path(path)
        if (
            not normalized_group
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", normalized_profile)
            or revision <= 0
            or not _DATABASE_SNAPSHOT_SHA256_RE.fullmatch(expected)
            or local_path is None
            or not local_path.is_file()
        ):
            raise _PortalSyncProtocolError("invalid_database_snapshot")
        try:
            data = local_path.read_bytes()
        except OSError as exc:
            raise _PortalSyncProtocolError("database_snapshot_read_failed") from exc
        if (
            len(data) > _MAX_PORTAL_DATABASE_SNAPSHOT_BYTES
            or data[:16] != b"SQLite format 3\x00"
        ):
            raise _PortalSyncProtocolError("invalid_database_snapshot_body")
        digest = hashlib.sha256(data).hexdigest()
        # ``expected`` is the hash of the revision used to download the
        # local snapshot.  A successful reward necessarily changes the file,
        # so the current body hash must be sent separately.
        pending_key = f"{normalized_group}:{normalized_profile}"
        endpoint = f"/api/sync/v1/database-snapshots/{normalized_group}/{normalized_profile}"
        headers = dict(self.headers)
        headers.update(
            {
                "Content-Type": "application/vnd.sqlite3",
                "X-Database-Snapshot-Revision": str(revision),
                "X-Database-Snapshot-SHA256": digest,
                "X-Database-Snapshot-Base-SHA256": expected,
            }
        )
        client = self._get_http_client(
            "database_snapshot_upload", httpx.Timeout(120.0, connect=8.0)
        )
        try:
            response = await client.post(
                f"{self.base_url}{endpoint}", headers=headers, content=data
            )
            status_code = getattr(response, "status_code", None)
            if status_code in {401, 403, 404, 409, 413, 502}:
                try:
                    payload = response.json()
                except Exception:
                    payload = {}
                code = str(payload.get("code", "") if isinstance(payload, dict) else "").strip()
                if code:
                    raise _PortalSyncProtocolError(code)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or result.get("success") is not True:
                raise _PortalSyncProtocolError("database_snapshot_upload_failed")
            new_revision = int(result.get("revision", 0) or 0)
            new_sha256 = str(result.get("sha256", "") or "").strip().lower()
            if new_revision <= revision or not hmac.compare_digest(new_sha256, digest):
                raise _PortalSyncProtocolError("database_snapshot_upload_unconfirmed")
            self._forget_pending_database_snapshot_upload(pending_key)
            self._database_snapshot_local_cache.pop(str(local_path), None)
            self._update_local_database_snapshot_metadata(
                normalized_group,
                normalized_profile,
                str(path),
                new_revision,
                new_sha256,
            )
            return result
        except _PortalSyncProtocolError:
            if _record_pending:
                self._remember_pending_database_snapshot_upload(pending_key, {
                    "group_id": normalized_group,
                    "profile_id": normalized_profile,
                    "path": str(path),
                    "revision": revision,
                    "sha256": expected,
                })
            raise
        except Exception as exc:
            if _record_pending:
                self._remember_pending_database_snapshot_upload(pending_key, {
                    "group_id": normalized_group,
                    "profile_id": normalized_profile,
                    "path": str(path),
                    "revision": revision,
                    "sha256": expected,
                })
            raise _PortalSyncProtocolError("database_snapshot_upload_failed") from exc

    @staticmethod
    def _update_local_database_snapshot_metadata(
        group_id: str,
        profile_id: str,
        path: str,
        revision: int,
        sha256: str,
    ) -> None:
        """Advance the local descriptor after the portal confirms FTP write."""

        try:
            config = load_group_config(group_id)
            features = config.get("features") if isinstance(config, dict) else None
            stats = features.get("stats") if isinstance(features, dict) else None
            profiles = stats.get("database_profiles") if isinstance(stats, dict) else None
            if not isinstance(profiles, list):
                return
            changed = False
            for item in profiles:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id", item.get("profile_id", "")) or "").strip() != profile_id:
                    continue
                item["snapshot_revision"] = revision
                item["snapshot_sha256"] = sha256
                changed = True
                break
            if changed:
                save_group_config(group_id, config)
        except Exception:
            logger.warning(
                "网页数据库快照本地版本标记失败：群=%s 数据库=%s",
                group_id,
                profile_id,
            )

    async def _retry_pending_database_snapshot_uploads(self) -> None:
        """Retry at most one pending database write per heartbeat."""

        pending = next(iter(self._database_snapshot_upload_pending.values()), None)
        if not isinstance(pending, dict):
            return
        try:
            await self.upload_database_snapshot(
                pending.get("group_id", ""),
                pending.get("profile_id", ""),
                pending.get("path", ""),
                pending.get("revision", 0),
                pending.get("sha256", ""),
                _record_pending=False,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "").strip().lower()
            if code in {
                "database_snapshot_conflict",
                "database_snapshot_missing",
                "database_ftp_not_configured",
            }:
                # The portal has a newer source state or no writable FTP
                # binding. Do not retry the old local body forever; the normal
                # snapshot pull path will reconcile a newer remote revision.
                pending_key = f"{pending.get('group_id', '')}:{pending.get('profile_id', '')}"
                self._forget_pending_database_snapshot_upload(pending_key)
            logger.warning(
                "网页数据库快照补传失败：群=%s 数据库=%s 错误=%s",
                pending.get("group_id", ""),
                pending.get("profile_id", ""),
                self._sync_error_summary(exc),
            )

    @staticmethod
    def _database_profile_label(item: object) -> str:
        """Return a stable, human-readable database source label."""
        if not isinstance(item, dict):
            return "默认数据库（ID: default）"
        profile_id = str(item.get("profile_id", item.get("id", "")) or "").strip()
        profile_name = str(
            item.get("profile_name", item.get("name", item.get("label", ""))) or ""
        ).strip()
        if not profile_name:
            profile_name = "默认数据库" if profile_id == "default" else (profile_id or "数据库")
        if profile_id and profile_name != profile_id:
            return f"{profile_name}（ID: {profile_id}）"
        return profile_name

    @staticmethod
    def _database_snapshot_requests(config: object) -> list[dict[str, Any]]:
        """Extract validated portal snapshot descriptors from one config."""

        if not isinstance(config, dict):
            return []
        features = config.get("features")
        stats = features.get("stats") if isinstance(features, dict) else None
        raw_profiles = stats.get("database_profiles") if isinstance(stats, dict) else None
        if not isinstance(raw_profiles, list):
            return []
        requests: list[dict[str, Any]] = []
        for raw in raw_profiles:
            item = raw
            if isinstance(raw, str):
                try:
                    item = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            if not isinstance(item, dict):
                continue
            profile_id = str(item.get("id", item.get("profile_id", "")) or "").strip()
            path = str(item.get("path", item.get("database_path", "")) or "").strip()
            enabled = item.get("enabled", True)
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() not in {"", "0", "false", "off", "no"}
            if (
                not _DATABASE_PROFILE_ID_RE.fullmatch(profile_id)
                or not path
                or not bool(enabled)
            ):
                continue
            try:
                revision = int(item.get("snapshot_revision", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            digest = str(item.get("snapshot_sha256", "") or "").strip().lower()
            if revision <= 0 or not _DATABASE_SNAPSHOT_SHA256_RE.fullmatch(digest):
                continue
            profile_name = str(item.get("name", item.get("label", "")) or "").strip()
            if not profile_name:
                profile_name = "默认数据库" if profile_id == "default" else profile_id
            requests.append(
                {
                    "profile_id": profile_id,
                    "profile_name": profile_name[:80],
                    "path": path,
                    "revision": revision,
                    "sha256": digest,
                }
            )
        return requests

    def _database_snapshot_local_matches(self, path: str, digest: str) -> bool:
        try:
            raw_path = str(path or "").strip().replace("\\", "/")
            parts = [part for part in raw_path.split("/") if part not in {"", "."}]
            if (
                not parts
                or raw_path.startswith("/")
                or re.match(r"^[A-Za-z]:", raw_path)
                or any(part == ".." for part in parts)
                or any(":" in part or any(ord(char) < 32 for char in part) for part in parts)
            ):
                return False
            root = Path(__file__).resolve().parent.parent
            target = (root / "/".join(parts)).resolve()
            if target != root and root not in target.parents:
                return False
            # A live SQLite connection can deny replacing the canonical file.
            # In that case materialize_database_snapshot publishes a verified
            # ``.joker-sync`` sidecar; validate that newer sidecar here so the
            # snapshot is usable immediately without interrupting the game.
            candidate = target
            sidecar = target.with_name(target.name + ".joker-sync")
            if sidecar.is_file() and (
                not target.is_file()
                or sidecar.stat().st_mtime_ns >= target.stat().st_mtime_ns
            ):
                candidate = sidecar
            cache_key = str(candidate)
            if not candidate.is_file():
                self._database_snapshot_local_cache.pop(cache_key, None)
                return False
            stat = candidate.stat()
            if stat.st_size > _MAX_PORTAL_DATABASE_SNAPSHOT_BYTES:
                return False
            cached = self._database_snapshot_local_cache.get(cache_key)
            stat_key = (
                int(stat.st_mtime_ns),
                int(getattr(stat, "st_ctime_ns", 0)),
                int(stat.st_size),
            )
            if cached is not None and cached[:3] == stat_key:
                return hmac.compare_digest(cached[3], str(digest).lower())
            hasher = hashlib.sha256()
            with candidate.open("rb") as source:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    hasher.update(block)
            calculated = hasher.hexdigest()
            self._database_snapshot_local_cache[cache_key] = (
                stat_key[0],
                stat_key[1],
                stat_key[2],
                calculated,
            )
            return hmac.compare_digest(calculated, str(digest).lower())
        except (OSError, RuntimeError, ValueError):
            return False

    def _queue_database_snapshot_cache(
        self,
        group_id: object,
        config: object,
        *,
        force: bool = False,
    ) -> bool:
        """Schedule one non-blocking database snapshot cache pass."""

        safe_group_id = self._valid_failure_group_id(group_id)
        requests = self._database_snapshot_requests(config)
        if not safe_group_id or not requests:
            if safe_group_id:
                self._database_snapshot_retry_at.pop(safe_group_id, None)
            return False
        pending_upload_keys = set(self._database_snapshot_upload_pending)
        pending = [
            item
            for item in requests
            if f"{safe_group_id}:{item['profile_id']}" not in pending_upload_keys
            and (force
            or not self._database_snapshot_local_matches(item["path"], item["sha256"])
            )
        ]
        if not pending:
            self._database_snapshot_retry_at.pop(safe_group_id, None)
            return False
        active = self._database_snapshot_tasks.get(safe_group_id)
        if active is not None and not active.done():
            return False
        now = int(time.time())
        if not force and now < self._database_snapshot_retry_at.get(safe_group_id, 0):
            return False
        try:
            task = asyncio.create_task(
                self._cache_database_snapshots_for_group(safe_group_id, pending),
                name=f"joker-portal-database-snapshots-{safe_group_id}",
            )
        except RuntimeError:
            return False
        self._database_snapshot_tasks[safe_group_id] = task
        task.add_done_callback(
            lambda finished, current_group_id=safe_group_id: self._finish_database_snapshot_cache(
                current_group_id, finished
            )
        )
        return True

    async def _cache_database_snapshots_for_group(
        self, group_id: str, requests: list[dict[str, Any]]
    ) -> None:
        failed = 0
        try:
            for item in requests:
                profile_id = str(item.get("profile_id", "") or "")
                profile_label = self._database_profile_label(item)
                _print_sync_group_action(
                    "网页数据库快照拉取",
                    group_id,
                    detail=(
                        f"数据库 {profile_label}；版本 {item.get('revision', 0)}；"
                        "配置已应用，正在从网页关联的远程 FTP 获取数据库"
                    ),
                )
                try:
                    data, revision, digest = await self.fetch_database_snapshot(
                        group_id,
                        item["profile_id"],
                        expected_revision=item["revision"],
                        expected_sha256=item["sha256"],
                    )
                    if revision != item["revision"] or digest != item["sha256"]:
                        raise _PortalSyncProtocolError("database_snapshot_metadata_mismatch")
                    materialize_database_snapshot(item["path"], data, digest)
                    self._database_snapshot_last_errors.pop(
                        f"{group_id}:{profile_id}", None
                    )
                    self._database_snapshot_local_cache.pop(
                        str((Path(__file__).resolve().parent.parent / item["path"]).resolve()),
                        None,
                    )
                    _print_sync_group_action(
                        "网页数据库快照已写入本地",
                        group_id,
                        detail=(
                            f"数据库 {profile_label}；版本 {revision}；"
                            "Windows 本地缓存已更新，签到将使用该远程 FTP 数据库"
                        ),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed += 1
                    summary = self._sync_error_summary(exc)
                    self._database_snapshot_last_errors[
                        f"{group_id}:{profile_id}"
                    ] = summary
                    logger.warning(
                        "网页数据库快照缓存失败：群=%s 数据库=%s 错误=%s 详细=%s",
                        group_id,
                        profile_id,
                        summary,
                        str(getattr(exc, "detail", "") or "")[:240] or "-",
                    )
                    _print_sync_group_action(
                        "网页数据库快照拉取失败",
                        group_id,
                        detail=f"数据库 {profile_label}；原因：{summary}",
                    )
            if failed:
                retry_seconds = _DEFAULT_DATABASE_SNAPSHOT_RETRY_SECONDS
                if any(
                    str(value) == "game_card_heartbeat_inactive"
                    for key, value in self._database_snapshot_last_errors.items()
                    if key.startswith(f"{group_id}:")
                ):
                    retry_seconds = _INACTIVE_GAME_CARD_RETRY_SECONDS
                self._database_snapshot_retry_at[group_id] = int(time.time()) + retry_seconds
            else:
                self._database_snapshot_retry_at[group_id] = int(time.time()) + _DEFAULT_DATABASE_SNAPSHOT_RETRY_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._database_snapshot_retry_at[group_id] = int(time.time()) + _DEFAULT_DATABASE_SNAPSHOT_RETRY_SECONDS
            logger.warning(
                "网页数据库快照缓存任务异常：群=%s 错误=%s",
                group_id,
                self._sync_error_summary(exc),
            )

    def _finish_database_snapshot_cache(self, group_id: str, task: asyncio.Task) -> None:
        if self._database_snapshot_tasks.get(group_id) is task:
            self._database_snapshot_tasks.pop(group_id, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            logger.warning(
                "网页数据库快照后台任务异常：群=%s 错误=%s",
                group_id,
                self._sync_error_summary(exc),
            )

    def _queue_reply_asset_cache(
        self,
        group_id: object,
        config: object,
        *,
        force: bool = False,
    ) -> bool:
        """Schedule one non-blocking cache pass for a group's portal assets."""
        safe_group_id = self._valid_failure_group_id(group_id)
        if not safe_group_id or not _config_has_portal_reply_assets(config):
            if safe_group_id:
                self._reply_asset_retry_at.pop(safe_group_id, None)
            return False
        active = self._reply_asset_tasks.get(safe_group_id)
        if active is not None and not active.done():
            return False
        now = int(time.time())
        if not force and now < self._reply_asset_retry_at.get(safe_group_id, 0):
            return False
        try:
            task = asyncio.create_task(
                self._cache_reply_assets_for_group(safe_group_id),
                name=f"joker-portal-reply-assets-{safe_group_id}",
            )
        except RuntimeError:
            # This method is reached only from the async sync loop in normal
            # use.  Keeping a safe no-op protects lightweight import tests and
            # a late interpreter shutdown from producing an unhandled error.
            return False
        self._reply_asset_tasks[safe_group_id] = task
        task.add_done_callback(
            lambda finished, current_group_id=safe_group_id: self._finish_reply_asset_cache(
                current_group_id, finished
            )
        )
        return True

    async def _cache_reply_assets_for_group(self, group_id: str) -> None:
        """Download opaque image assets after the corresponding config is saved."""
        try:
            config = load_group_config(group_id)
            if not _config_has_portal_reply_assets(config):
                self._reply_asset_retry_at.pop(group_id, None)
                return
            max_image_bytes, concurrency, retry_seconds = _portal_reply_cache_options(
                config
            )
            from .reply import cache_portal_reply_assets

            async def fetch_asset(asset_id: int) -> tuple[bytes, str]:
                return await self.fetch_reply_asset(
                    asset_id, max_image_bytes=max_image_bytes
                )

            result = await cache_portal_reply_assets(
                group_id,
                fetch_asset,
                max_image_bytes=max_image_bytes,
                download_concurrency=concurrency,
            )
            failed = _bounded_integer(result.get("failed", 0), 0, 0, 100000)
            if failed:
                self._reply_asset_retry_at[group_id] = int(time.time()) + retry_seconds
                logger.warning(
                    "网页自动回复图片尚未全部缓存：群=%s 失败=%s，将在 %s 秒后重试",
                    group_id,
                    failed,
                    retry_seconds,
                )
            else:
                # A file can later be removed manually or by an older plugin
                # cleanup.  Recheck periodically, but do not turn every
                # normal web-sync heartbeat into image-directory work.
                self._reply_asset_retry_at[group_id] = int(time.time()) + retry_seconds
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep the Windows log actionable but never leak a portal URL,
            # bearer token, local filename, or server response body.
            self._reply_asset_retry_at[group_id] = int(time.time()) + _DEFAULT_REPLY_IMAGE_RETRY_SECONDS
            logger.warning(
                "网页自动回复图片缓存任务失败：群=%s 错误=%s",
                group_id,
                type(exc).__name__,
            )

    def _finish_reply_asset_cache(self, group_id: str, task: asyncio.Task) -> None:
        """Discard a completed cache task and surface unexpected task errors."""
        if self._reply_asset_tasks.get(group_id) is task:
            self._reply_asset_tasks.pop(group_id, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            # ``_cache_reply_assets_for_group`` catches expected transport and
            # validation failures itself; this branch protects future edits.
            logger.warning(
                "网页自动回复图片后台任务异常：群=%s 错误=%s",
                group_id,
                type(exc).__name__,
            )

    async def aclose(self) -> None:
        """Cancel portal asset/database work when sync is reconfigured."""
        tasks = tuple(self._reply_asset_tasks.values()) + tuple(
            self._database_snapshot_tasks.values()
        )
        self._reply_asset_tasks.clear()
        self._database_snapshot_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        clients = tuple(self._http_clients.values())
        self._http_clients.clear()
        for client in clients:
            close = getattr(client, "aclose", None)
            if not callable(close):
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("Unable to close portal HTTP client", exc_info=True)

    @staticmethod
    def _valid_failure_group_id(group_id: object) -> str:
        """Return a safe portal group id, or an empty value for bad input."""
        normalized = str(group_id or "").strip()
        return normalized if re.fullmatch(r"\d{5,20}", normalized) else ""

    @classmethod
    def _valid_group_id_set(cls, values: object) -> set[str]:
        """Normalize a server-provided group scope without trusting input."""
        if not isinstance(values, (list, tuple, set, frozenset)):
            return set()
        return {
            group_id
            for raw_group_id in values
            if (group_id := cls._valid_failure_group_id(raw_group_id))
        }

    @classmethod
    def _parse_group_id_set(cls, values: object) -> set[str] | None:
        """Parse a scope list, returning ``None`` for a malformed response."""
        if not isinstance(values, (list, tuple, set, frozenset)):
            return None
        return cls._valid_group_id_set(values)

    @staticmethod
    def _failure_summary(stage: str, exc: Exception | None = None) -> str:
        """Build a deliberately non-sensitive error summary for the portal.

        Never include exception text here: transport exceptions can contain a
        URL, filesystem path, response body, or other deployment details.
        The local AstrBot log retains the full traceback where appropriate.
        """
        safe_stage = re.sub(r"[^a-z0-9_]", "_", str(stage or "apply_failed").lower())
        safe_stage = safe_stage.strip("_") or "apply_failed"
        if exc is None:
            return safe_stage[:80]
        if isinstance(exc, _PortalSyncProtocolError):
            return f"{safe_stage}:{exc.code}"[:160]
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and 100 <= status_code <= 599:
            if safe_stage.startswith("database_snapshot"):
                if status_code == 404:
                    return f"{safe_stage}:database_snapshot_endpoint_not_found"[:160]
                if status_code in {401, 403}:
                    return f"{safe_stage}:database_snapshot_http_{status_code}"[:160]
            return f"{safe_stage}:http_status_{status_code}"[:160]
        error_type = re.sub(r"[^A-Za-z0-9_]", "_", type(exc).__name__).strip("_")
        return f"{safe_stage}:{error_type or 'Error'}"[:160]

    def _remember_failure(self, group_id: object, revision: object, summary: str) -> None:
        """Queue one sanitized failure report for a pending portal revision."""
        safe_group_id = self._valid_failure_group_id(group_id)
        try:
            safe_revision = int(revision)
        except (TypeError, ValueError, OverflowError):
            return
        if not safe_group_id or safe_revision <= 0:
            return
        key = (safe_group_id, safe_revision)
        # Callers pass either a fixed stage or ``stage:ExceptionType`` built
        # above.  Preserve only that tiny allow-list alphabet rather than any
        # exception message or deployment detail.
        safe_summary = re.sub(r"[^A-Za-z0-9_:-]", "_", str(summary or ""))
        safe_summary = safe_summary.strip("_:") or "apply_failed"
        self._pending_failure_reports[key] = safe_summary[:160]
        # The server caps a single exchange payload.  Keep the newest reports
        # if a malformed response ever contains an unexpectedly large batch.
        while len(self._pending_failure_reports) > 5000:
            self._pending_failure_reports.pop(next(iter(self._pending_failure_reports)))

    def _failure_reports_payload(self) -> list[dict[str, Any]]:
        return [
            {"group_id": group_id, "revision": revision, "error": error}
            for (group_id, revision), error in self._pending_failure_reports.items()
        ]

    def _clear_failure(self, group_id: object, revision: object) -> None:
        safe_group_id = self._valid_failure_group_id(group_id)
        try:
            safe_revision = int(revision)
        except (TypeError, ValueError, OverflowError):
            return
        self._pending_failure_reports.pop((safe_group_id, safe_revision), None)

    def _discard_resolved_failure_reports(
        self, reported: list[dict[str, Any]], changes: object
    ) -> None:
        """Forget reports that the portal no longer considers pending.

        This also handles the ambiguous case where the server committed an ACK
        but its response was lost: the next exchange no longer returns that
        revision, so repeatedly reporting it has no value.
        """
        pending: set[tuple[str, int]] = set()
        if isinstance(changes, list):
            for change in changes:
                if not isinstance(change, dict):
                    continue
                group_id = self._valid_failure_group_id(change.get("group_id"))
                try:
                    revision = int(change.get("revision", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if group_id and revision > 0:
                    pending.add((group_id, revision))

        for item in reported:
            group_id = self._valid_failure_group_id(item.get("group_id"))
            try:
                revision = int(item.get("revision", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            key = (group_id, revision)
            if group_id and revision > 0 and key not in pending:
                self._pending_failure_reports.pop(key, None)

    def _should_log_config_upload(self, group_id: str, payload: dict[str, Any]) -> bool:
        """Return whether this configuration upload deserves a CMD line."""
        try:
            serialized = json.dumps(
                payload.get("config", payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            serialized = repr(payload.get("config", payload))
        signature = hashlib.sha1(serialized.encode("utf-8", "replace")).hexdigest()
        now = time.monotonic()
        previous = self._config_upload_log_state.get(str(group_id))
        # A pending web revision can cause the same group to be included in
        # every retry, and a hot reload can also rebuild the payload object.
        # The upload must continue, but an unchanged payload must not flood
        # the Windows terminal. Emit one line only when its content changes.
        if previous is not None and previous[0] == signature:
            return False
        self._config_upload_log_state[str(group_id)] = (signature, now)
        # Do not retain stale entries forever when groups are frequently added.
        if len(self._config_upload_log_state) > 2000:
            cutoff = now - 3600.0
            self._config_upload_log_state = {
                key: value
                for key, value in self._config_upload_log_state.items()
                if value[1] >= cutoff
            }
        return True

    async def run_forever(self) -> None:
        if not self.configured:
            logger.warning("网页同步未启动：%s", self.configuration_error)
            return
        logger.debug("网页数据同步已启动：%s（间隔 %ss）", self.base_url, self.interval_seconds)
        try:
            while True:
                # Clear before synchronizing.  A request raised while the exchange
                # is running remains set and schedules one follow-up exchange.
                self._sync_requested.clear()
                try:
                    await self.sync_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = self._sync_error_summary(exc)
                    self._log_sync_failure(self.last_error)
                    if self._connection_state != "error":
                        self._connection_state = "error"
                        if self.show_connection_events and self._last_sync_group_ids:
                            _print_sync_group_action(
                                "网页通联失败",
                                self._last_sync_group_ids[0],
                                detail=(
                                    f"时间 {time.strftime('%Y-%m-%d %H:%M:%S')}；"
                                    f"原因：{self.last_error}"
                                ),
                            )
                try:
                    await asyncio.wait_for(
                        self._sync_requested.wait(), timeout=self.interval_seconds
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.aclose()

    async def sync_once(self) -> dict[str, Any]:
        groups, configs_complete, configs_readable = _load_group_configs_for_sync()
        if configs_readable:
            await self._recover_unacknowledged_local_database_snapshots(groups)
        # FTP snapshot writes are maintenance work, not the Windows heartbeat.
        # Keep them bounded so a slow/locked SQLite file cannot make the portal
        # mark this client offline before the normal exchange updates last_seen.
        try:
            await asyncio.wait_for(
                self._retry_pending_database_snapshot_uploads(), timeout=1.0
            )
        except asyncio.TimeoutError:
            logger.debug("网页数据库快照补传仍在后台重试，本轮优先保持网页心跳")
        authorizations, auth_complete = _load_authorizations_for_sync()
        game_authorizations, game_auth_complete = _load_game_authorizations_for_sync()
        # Configuration and authorization are two independent local sources.
        # A damaged auth.json must not block a readable group configuration
        # (otherwise every web pull remains stuck at "等待连接"), but it must
        # also never be represented as an empty authorization.  The two source
        # health bits are therefore sent separately; only when both are true
        # is the complete group inventory authoritative for deletion.
        # A directory can be readable but intentionally empty before the
        # first local JSON is created.  Treat that as a usable config source
        # for auth-only projections while keeping ``authoritative_group_ids``
        # false until the initialization marker proves the empty set is an
        # intentional deletion.  A real read failure remains protected.
        config_source_complete = bool(configs_readable)
        auth_source_complete = bool(auth_complete)
        local_sources_complete = bool(configs_complete and auth_complete)
        config_group_ids = {
            str(group_id) for group_id in groups if self._valid_failure_group_id(group_id)
        }
        auth_group_ids = {
            str(group_id)
            for group_id in authorizations
            if self._valid_failure_group_id(group_id)
        }
        group_ids = set(config_group_ids)
        group_ids.update(auth_group_ids)
        game_auth_group_ids = {
            str(group_id)
            for group_id in game_authorizations
            if self._valid_failure_group_id(group_id)
        }
        if game_auth_complete:
            group_ids.update(game_auth_group_ids)
        local_group_ids = set(group_ids)
        if config_source_complete and callable(self.local_group_ids_callback):
            try:
                listed_group_ids = self.local_group_ids_callback()
                if inspect.isawaitable(listed_group_ids):
                    listed_group_ids = await listed_group_ids
                if isinstance(listed_group_ids, (list, tuple, set)):
                    local_group_ids.update(
                        normalized
                        for value in listed_group_ids
                        if (normalized := self._valid_failure_group_id(value))
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("读取 Windows 本机已加入群列表失败", exc_info=True)
        local_group_ids.update(self._manual_web_pull_group_ids)
        auth_source_recovered = (
            auth_source_complete is True
            and self._last_auth_source_complete is False
        )
        if auth_source_recovered:
            # Do this before building the delta list.  It covers both ordinary
            # config groups and auth-only/manual-authorized groups whose last
            # payload was cached immediately before auth.json became unreadable.
            for group_id in group_ids:
                self._last_uploaded.pop(group_id, None)
        self._last_auth_source_complete = auth_source_complete
        if not local_sources_complete:
            if self._local_sync_sources_complete:
                if config_source_complete and not auth_source_complete:
                    logger.warning(
                        "本地授权读取不完整：继续上传可读群配置，"
                        "不会修改网页已有授权/卡密，也不会进行权威删除"
                    )
                elif auth_source_complete and not config_source_complete:
                    logger.warning(
                        "本地群配置读取不完整：仅保留可读授权，"
                        "不会覆盖网页已有群配置，也不会进行权威删除"
                    )
                else:
                    logger.warning(
                        "本地群配置或授权读取不完整：本轮仅保留网页通联，"
                        "不会上传部分配置/授权/运行数据，也不会消费网页补传请求"
                    )
                # Keep one human-readable CMD trace for the protection state.
                # The web request itself remains queued on Linux, so an
                # operator can distinguish a local JSON read problem from a
                # domain/TLS transport failure without being flooded every
                # heartbeat.
                diagnostic_group_id = next(
                    (
                        candidate
                        for candidate in (
                            *sorted(group_ids),
                            *self._last_sync_group_ids,
                        )
                        if self._valid_failure_group_id(candidate)
                    ),
                    "",
                )
                if diagnostic_group_id:
                    _print_sync_group_action(
                        (
                            "本地授权读取保护"
                            if config_source_complete and not auth_source_complete
                            else "本地数据读取保护"
                        ),
                        diagnostic_group_id,
                        detail=(
                            (
                                "本地授权未完整读取；本轮继续上传群配置但保留网页授权，"
                                "授权数据恢复后将自动补传完整状态"
                            )
                            if config_source_complete and not auth_source_complete
                            else (
                                "本地群配置或授权未完整读取；本轮只保持网页通联，"
                                "不上传配置且不处理网页补传，数据恢复后将自动继续同步"
                            )
                        ),
                    )
            self._local_sync_sources_complete = False
        else:
            if not self._local_sync_sources_complete:
                logger.debug("本地群配置与授权读取已恢复：下一轮同步恢复权威分群校验")
            self._local_sync_sources_complete = True
        # Build only the projections backed by a healthy source.  In the
        # common recovery case (config readable, auth.json unavailable), the
        # payload deliberately omits ``auth``; the Linux server keeps the last
        # verified card instead of replacing it with a fabricated ``missing``.
        # Negotiate the portal capability before sending full group data.  A
        # scope probe is auth-only, so even the first heartbeat after a Windows
        # restart does not upload unbound configuration/runtime/message data.
        scope_probe = self._server_scope_supported is None
        scoped_sync = self._server_scope_supported is True
        managed_group_ids = (
            set(self._server_managed_group_ids or ()) if scoped_sync else set(group_ids)
        )
        scoped_config_group_ids = (
            set()
            if scope_probe
            else (
                config_group_ids & managed_group_ids
                if scoped_sync
                else set(config_group_ids)
            )
        )
        scoped_auth_group_ids = (
            set()
            if scope_probe
            else (
                auth_group_ids & managed_group_ids
                if scoped_sync
                else set(auth_group_ids)
            )
        )
        # A card must be discoverable before a customer can claim it.  Send
        # only the compact auth projection for an unbound, activated card, and
        # only while its payload is new (the delta cache prevents heartbeat
        # repetition).  Config/runtime/message data is never included for
        # these bootstrap-only groups.
        bootstrap_auth_group_ids: set[str] = set()
        if (scope_probe or scoped_sync) and auth_source_complete:
            # Keep the bootstrap channel deliberately narrow: only a real,
            # activated card is eligible.  The normal payload delta cache
            # ensures an unchanged card is not retransmitted on every pulse.
            previous_bootstrap_ids = {
                group_id
                for group_id, previous in self._last_uploaded.items()
                if isinstance(previous, dict)
                and "config" not in previous
                and str((previous.get("auth") or {}).get("state", "")) != "missing"
                and self._valid_failure_group_id(group_id)
            }
            bootstrap_candidates = auth_group_ids | previous_bootstrap_ids
            if scoped_sync:
                bootstrap_candidates -= managed_group_ids
            bootstrap_auth_group_ids = {
                candidate
                for candidate in bootstrap_candidates
                if (
                    isinstance(authorizations.get(candidate), dict)
                    and _is_activated_card(authorizations[candidate].get("key"))
                )
                or candidate in previous_bootstrap_ids
            }

        current_groups: dict[str, dict[str, Any]] = {}
        if config_source_complete:
            for group_id in sorted(scoped_config_group_ids):
                current_groups[group_id] = build_group_payload(
                    group_id,
                    groups.get(group_id, {"group_id": group_id}),
                    self.card_pepper,
                    include_config=True,
                    include_auth=auth_source_complete,
                    auth_info=authorizations.get(group_id) if auth_source_complete else None,
                    include_game_auth=game_auth_complete,
                    game_auth_info=(
                        game_authorizations.get(group_id) if game_auth_complete else None
                    ),
                )
        if config_source_complete and auth_source_complete:
            # Keep manually authorized groups visible even when their config
            # file has not been created yet.  This is the old supported
            # behaviour, now expressed without pretending a missing config is
            # an empty authoritative directory.
            for group_id in sorted(scoped_auth_group_ids - scoped_config_group_ids):
                current_groups[group_id] = build_group_payload(
                    group_id,
                    {"group_id": group_id},
                    self.card_pepper,
                    include_config=False,
                    include_auth=True,
                    auth_info=authorizations.get(group_id),
                    include_game_auth=game_auth_complete,
                    game_auth_info=game_authorizations.get(group_id),
                )
        if game_auth_complete:
            # Game authorization is independent from the QQ management card.
            # Keep a game-only group heartbeat possible even when the group has
            # no ordinary portal authorization or local config file.
            for group_id in sorted(game_auth_group_ids - set(current_groups)):
                current_groups[group_id] = build_group_payload(
                    group_id,
                    {"group_id": group_id},
                    self.card_pepper,
                    include_config=False,
                    include_auth=False,
                    include_game_auth=True,
                    game_auth_info=game_authorizations.get(group_id),
                )
        if config_source_complete and auth_source_complete:
            for group_id in sorted(bootstrap_auth_group_ids):
                auth_info = authorizations.get(group_id)
                # A previously advertised card that disappeared locally is
                # sent once as an explicit missing authorization.  The portal
                # then blocks future login/claim attempts instead of retaining
                # a stale active card forever.
                if not isinstance(auth_info, dict):
                    auth_info = {}
                current_groups[group_id] = build_group_payload(
                    group_id,
                    {"group_id": group_id},
                    self.card_pepper,
                    include_config=False,
                    include_auth=True,
                    auth_info=auth_info,
                    include_game_auth=game_auth_complete,
                    game_auth_info=game_authorizations.get(group_id),
                )
        if current_groups:
            self._last_sync_group_ids = tuple(sorted(current_groups))
        # If an operator restores this group locally, permit a future server
        # request to be reported again instead of suppressing its diagnostic.
        self._reported_unavailable_resync_group_ids.difference_update(current_groups)
        changed_groups = [
            payload
            for group_id, payload in current_groups.items()
            if (
                self._last_uploaded.get(group_id) != payload
                or group_id in self._forced_config_group_ids
            )
        ]
        # A web save and a Windows-side command can cross in transit.  The
        # portal deliberately returns a pending web revision before accepting
        # the local payload in that exchange, which used to let an old web
        # snapshot restore a reply/rule that Windows had just deleted.  Only
        # treat a group as locally newer after this client has already
        # acknowledged a prior full config snapshot.  Initial bootstraps must
        # still be allowed to receive a legitimate existing web revision.
        locally_changed_config_group_ids: set[str] = set()
        for item in changed_groups:
            group_id = self._valid_failure_group_id(item.get("group_id"))
            config = item.get("config") if isinstance(item, dict) else None
            previous = self._last_uploaded.get(group_id) if group_id else None
            previous_config = (
                previous.get("config") if isinstance(previous, dict) else None
            )
            if (
                group_id
                and isinstance(config, dict)
                and isinstance(previous_config, dict)
                and previous_config != config
            ):
                locally_changed_config_group_ids.add(group_id)
        # An unchanged bootstrap card is intentionally not part of the wire
        # inventory.  Including every unbound local ID here would make the
        # portal issue a missing-snapshot hint forever and defeat the reduced
        # communication scope.
        if scope_probe or scoped_sync:
            changed_bootstrap_ids = {
                str(item.get("group_id"))
                for item in changed_groups
                if isinstance(item, dict)
                and "config" not in item
                and isinstance(item.get("auth"), dict)
            }
        else:
            changed_bootstrap_ids = set()
        # Runtime data changes independently of the editable projection.  A
        # complete summary reads several local stores, so rotate a bounded
        # batch and always put groups with a config upload first.  This keeps
        # a page save/request responsive on Windows installations with many
        # configured groups.
        runtime_group_ids = (
            self._runtime_summary_group_ids(
                scoped_config_group_ids,
                changed_groups,
                self._runtime_dirty_group_ids | self._runtime_pending_ack_group_ids,
            )
            if config_source_complete and not scope_probe
            else []
        )
        current_runtime_summaries = {
            group_id: build_runtime_summary(group_id)
            for group_id in runtime_group_ids
        }
        runtime_summaries = {
            group_id: summary
            for group_id, summary in current_runtime_summaries.items()
            if (
                group_id in self._runtime_dirty_group_ids
                or group_id in self._runtime_pending_ack_group_ids
                or self._last_runtime_summaries.get(group_id) != summary
            )
        }
        # Member directories use the dedicated fast path. The explicit
        # refresh result is the single user-facing report; do not print a
        # second upload line for every heartbeat projection.
        # This is the Windows-side half of the pull protocol.  Showing the
        # group number before the request leaves a useful CMD trail even when
        # the portal is temporarily unreachable; the local event is retried
        # by the normal console-event journal after connectivity returns.
        for item in changed_groups:
            group_id = self._valid_failure_group_id(item.get("group_id"))
            if group_id and isinstance(item.get("config"), dict):
                previous = self._last_uploaded.get(group_id) or {}
                previous_config = (
                    previous.get("config") if isinstance(previous, dict) else {}
                )
                if self._should_log_config_upload(group_id, item):
                    _print_sync_group_action(
                        "分群配置上传",
                        group_id,
                        detail=(
                            "Windows 本地配置上传到网页，等待网页确认；"
                            + _sync_config_detail(
                                previous_config, item.get("config"), 0
                            )
                        ),
                    )
        # Send a heartbeat even when nothing changed so the web page can show
        # the local bot as online. Full group payloads are sent only on change.
        # Runtime events are intentionally independent of configuration state:
        # a customer can see the bot's current behaviour even when no group
        # config changed since the last heartbeat.  The server deduplicates by
        # event_id, so retrying a request is safe.
        all_console_events = get_console_events(limit=500)
        console_events = [
            event
            for event in all_console_events
            if str(event.get("event_id", "")) not in self._uploaded_console_event_ids
            and (
                not (scope_probe or scoped_sync)
                or self._valid_failure_group_id(event.get("group_id"))
                in managed_group_ids
            )
        ]
        if scope_probe:
            console_events = []
        if not self._portal_message_content_enabled:
            # Do not send old locally cached previews while the portal switch
            # is off.  Copy only the small event dictionaries; the local
            # journal remains untouched and can be displayed after an
            # explicit opt-in on a later exchange if it is still relevant.
            console_events = [
                {**event, "message_content": ""}
                for event in console_events
            ]
        if not config_source_complete:
            # A protected heartbeat must not upload group-scoped runtime
            # events either.  The portal cannot prove their local group
            # ownership while a source file is unreadable, and marking these
            # events as accepted would discard them from the retry journal
            # before the configuration/auth source has recovered.
            console_events = []
        # Keep the batch in journal order.  After Linux confirms this page the
        # next exchange sends the next unsent page, so a burst of chat events
        # cannot starve older records or turn a normal configuration pull into
        # a large database transaction.
        console_events = console_events[:_MAX_CONSOLE_EVENTS_PER_EXCHANGE]
        # Apply-failure reports are tied to a prior web projection.  Defer them
        # while local sources are incomplete; the reports remain queued locally
        # and are sent after a complete source read succeeds.
        reported_failures = self._failure_reports_payload() if config_source_complete else []
        if scope_probe:
            reported_failures = []
        elif scoped_sync:
            reported_failures = [
                item
                for item in reported_failures
                if self._valid_failure_group_id(item.get("group_id"))
                in managed_group_ids
            ]
        payload = {
            "client_id": self.client_id,
            # Group IDs alone let Linux discover this installation for a
            # group that already has a canonical web configuration. The
            # server never accepts a config from this discovery field.
            "local_group_ids": sorted(local_group_ids)
            if config_source_complete
            else [],
            # Only a complete config directory can be used to reconcile local
            # group deletion.  The server still uses these IDs to route a
            # one-group refresh when the auth source is temporarily absent.
            "known_group_ids": (
                sorted(
                    changed_bootstrap_ids
                    if scope_probe
                    else (managed_group_ids | changed_bootstrap_ids)
                    if scoped_sync
                    else group_ids
                )
                if local_sources_complete
                # A readable-but-uninitialized config directory can still
                # report manually authorized groups.  Include those IDs for
                # routing/refresh, while ``authoritative_group_ids`` remains
                # false so the server cannot delete older snapshots.
                else (sorted(group_ids) if config_source_complete else [])
            ),
            # A scoped inventory is intentionally partial: absent unbound
            # groups must not be converted into server tombstones.
            "authoritative_group_ids": (
                local_sources_complete and not scope_probe and not scoped_sync
            ),
            "sync_scope_complete": scoped_sync,
            "scope_probe": scope_probe,
            "config_source_complete": config_source_complete,
            "auth_source_complete": auth_source_complete,
            "game_auth_source_complete": game_auth_complete,
            "groups": changed_groups,
            "runtime_summaries": runtime_summaries,
            "console_events": console_events,
            # Reports are only ever sent on a later exchange, after the
            # original exchange succeeded but a local apply or ACK failed.
            "apply_failures": reported_failures,
        }
        timeout = httpx.Timeout(_EXCHANGE_TIMEOUT_SECONDS, connect=8.0)
        result = await self._post_json("/api/sync/v1/exchange", payload, timeout)
        execution_control = (
            result.get("execution_control") if isinstance(result, dict) else None
        )
        if isinstance(execution_control, dict) and callable(
            self.execution_control_callback
        ):
            try:
                callback_result = self.execution_control_callback(execution_control)
                if inspect.isawaitable(callback_result):
                    await callback_result
            except Exception as exc:
                logger.warning("Linux 执行端控制策略应用失败：%s", exc)
        if isinstance(execution_control, dict) and not bool(
            execution_control.get("client_allowed", True)
        ):
            self.last_error = "execution_client_blocked"
            self._last_sync_group_ids = ()
            logger.warning(
                "该 Windows 执行端已被 Linux 网页停用 client_id=%s",
                self.client_id,
            )
            return {
                "blocked": True,
                "client_id": self.client_id,
                "execution_control": execution_control,
            }
        if runtime_summaries:
            accepted = 0
            accepted_known = False
            accepted_group_ids: set[str] = set()
            if isinstance(result, dict):
                raw_accepted = result.get("runtime_accepted_group_ids", [])
                accepted_known = "runtime_accepted_group_ids" in result
                if isinstance(raw_accepted, list):
                    accepted_group_ids = {
                        self._valid_failure_group_id(value)
                        for value in raw_accepted
                        if self._valid_failure_group_id(value)
                    }
                    accepted = len(accepted_group_ids)
            if accepted_known:
                # Do not clear a pending member refresh until the server has
                # confirmed the owning group. A rejected/unbound group is
                # retried on the next heartbeat instead of remaining stuck at
                # “等待 Windows 同步成员”.
                self._runtime_pending_ack_group_ids.difference_update(
                    accepted_group_ids
                )
            else:
                # Compatibility with portals predating the acknowledgement
                # field: the successful HTTP exchange is the only signal
                # available, so treat the submitted groups as accepted.
                self._runtime_pending_ack_group_ids.difference_update(
                    runtime_summaries
                )
            logger.debug(
                "网页运行数据上传完成 groups=%s accepted=%s",
                len(runtime_summaries),
                accepted if accepted_known else "兼容确认",
            )
        if isinstance(result, dict) and callable(self.runtime_settings_callback):
            try:
                callback_result = self.runtime_settings_callback(result)
                if inspect.isawaitable(callback_result):
                    await callback_result
            except Exception as exc:
                # A runtime-setting write must not turn a successful group
                # exchange into a retry storm; the next heartbeat retries it.
                logger.warning("Linux 游戏执行器配置应用失败：%s", exc)
        scope_cache_invalidations: set[str] = set()
        # New portals return the durable scope on every response.  Validate it
        # before caching; malformed/omitted fields leave the compatibility
        # state untouched rather than unexpectedly disabling all sync traffic.
        if isinstance(result, dict) and (
            "bound_group_ids" in result or "managed_group_ids" in result
        ):
            raw_scope = result.get("managed_group_ids", result.get("bound_group_ids"))
            new_scope = self._parse_group_id_set(raw_scope)
            claimable_scope = self._parse_group_id_set(
                result.get("claimable_group_ids", [])
            )
            if new_scope is None or claimable_scope is None:
                logger.warning("网页同步范围响应格式无效，本轮保留原同步范围")
            else:
                previous_scope = self._server_managed_group_ids
                self._server_managed_group_ids = new_scope
                self._server_scope_supported = True
                self._server_claimable_group_ids = claimable_scope
            # Scope changes are a synchronization edge, not a reason to wait
            # for the normal timer.  Invalidate only affected local deltas so
            # a newly claimed group gets a complete projection while a
            # released group stops producing runtime traffic immediately.
                affected_scope = (
                    set(new_scope)
                    if previous_scope is None
                    else (previous_scope ^ new_scope)
                )
                scope_cache_invalidations.update(affected_scope)
                if affected_scope:
                    self.request_sync()
        elif scope_probe:
            # Compatibility with a Linux portal installed before scoped sync.
            # The probe contained no full configuration, so schedule exactly
            # one legacy follow-up rather than silently disabling web saves.
            self._server_scope_supported = False
            self.request_sync()
        # Update the delta cache only after the exchange succeeds.  If a
        # timeout occurs after the request is constructed, the next retry must
        # carry the same summaries instead of silently dropping them.
        if config_source_complete:
            self._last_runtime_summaries.update(runtime_summaries)
            # A dirty marker means the summary was requested, not that its
            # value necessarily changed.  Clear it after a successful
            # exchange so a deleted record is not rebuilt on every heartbeat.
            self._runtime_dirty_group_ids.difference_update(runtime_group_ids)
            for group_id in tuple(self._last_runtime_summaries):
                if group_id not in scoped_config_group_ids:
                    self._last_runtime_summaries.pop(group_id, None)
        if self._connection_state != "online":
            self._connection_state = "online"
            if self.show_connection_events and self._last_sync_group_ids:
                _print_sync_group_action(
                    "网页通联恢复",
                    self._last_sync_group_ids[0],
                    detail=f"时间 {time.strftime('%Y-%m-%d %H:%M:%S')}；exchange 成功",
                )
        server_console = result.get("console") if isinstance(result, dict) else None
        previous_console_enabled = self._portal_console_enabled
        previous_messages_enabled = self._portal_message_content_enabled
        server_console_enabled = bool(
            server_console.get("enabled", False)
            if isinstance(server_console, dict)
            else False
        )
        server_messages_enabled = bool(
            server_console_enabled
            and isinstance(server_console, dict)
            and server_console.get("messages_enabled", False)
        )
        self._portal_console_enabled = server_console_enabled
        self._portal_message_content_enabled = server_messages_enabled
        _set_portal_message_content_enabled(server_messages_enabled)
        if server_console_enabled and (
            not previous_console_enabled
            or server_messages_enabled != previous_messages_enabled
        ):
            # The exchange that carries the opt-in was built before Windows
            # knew about it.  Wake one immediate follow-up exchange so events
            # written after the switch include their inbound/outbound preview
            # instead of waiting for the regular heartbeat.
            self.request_sync()
        if config_source_complete or auth_source_complete:
            self._discard_resolved_failure_reports(
                reported_failures if config_source_complete else [], result.get("changes", [])
            )

            # A pending web revision means the portal has not accepted this
            # exchange's local projection as canonical yet.  Do not advance
            # the Windows delta cache for that group before the ACK: if the
            # ACK times out, the next heartbeat must resend the local change
            # and win the same Windows-authoritative conflict check above.
            pending_change_group_ids = {
                group_id
                for change in (result.get("changes", []) if isinstance(result, dict) else [])
                if isinstance(change, dict)
                for group_id in (self._valid_failure_group_id(change.get("group_id")),)
                if group_id and isinstance(change.get("config"), dict)
            }
            for item in changed_groups:
                group_id = self._valid_failure_group_id(item.get("group_id"))
                if group_id and group_id not in pending_change_group_ids:
                    self._last_uploaded[group_id] = item
                    self._forced_config_group_ids.discard(group_id)
            if local_sources_complete:
                for group_id in tuple(self._last_uploaded):
                    # In scoped mode retain a filtered group's last auth/config
                    # payload for future binding changes, but discard entries
                    # that no longer exist in either local source.
                    if group_id not in group_ids:
                        self._last_uploaded.pop(group_id, None)
            # Apply a newly advertised scope *after* recording this exchange's
            # successful upload.  Otherwise the just-sent bootstrap payload
            # would immediately repopulate the delta cache and the first
            # scoped follow-up would skip the bound group's full projection.
            for group_id in scope_cache_invalidations:
                self._last_uploaded.pop(group_id, None)
                self._last_runtime_summaries.pop(group_id, None)
        missing_group_ids: set[str] = set()
        unavailable_resync_group_ids: set[str] = set()
        # A protected heartbeat deliberately does not consume reconciliation
        # hints.  It has no trustworthy local group inventory to fulfill them
        # with, so acting on a response here used to make a browser refresh look
        # permanently ignored until another manual request was made.
        raw_missing_group_ids = (
            result.get("missing_group_ids", []) if config_source_complete else []
        )
        eligible_resync_group_ids = (
            group_ids if config_source_complete else config_group_ids
        )
        if isinstance(raw_missing_group_ids, list):
            for raw_group_id in raw_missing_group_ids:
                group_id = self._valid_failure_group_id(raw_group_id)
                if not group_id:
                    continue
                if group_id in eligible_resync_group_ids:
                    missing_group_ids.add(group_id)
                else:
                    unavailable_resync_group_ids.add(group_id)
        if missing_group_ids:
            # The portal may have been restored or had its snapshot table
            # cleared while AstrBot stayed alive.  Forget only those local
            # cache entries and schedule a full re-upload on the next loop.
            for group_id in missing_group_ids:
                self._last_uploaded.pop(group_id, None)
            self.request_sync()
            logger.debug(
                "网页服务器缺少 %s 个本地群快照，已安排自动补传",
                len(missing_group_ids),
            )
            for group_id in sorted(missing_group_ids):
                _print_sync_group_action(
                    "网页补传请求",
                    group_id,
                    detail="网页未找到本群快照，已请求 Windows 补传",
                )
        refresh_group_ids: set[str] = set()
        raw_refresh_group_ids = (
            result.get("refresh_group_ids", []) if config_source_complete else []
        )
        if isinstance(raw_refresh_group_ids, list):
            for raw_group_id in raw_refresh_group_ids:
                group_id = self._valid_failure_group_id(raw_group_id)
                if not group_id:
                    continue
                if group_id in eligible_resync_group_ids:
                    refresh_group_ids.add(group_id)
                else:
                    unavailable_resync_group_ids.add(group_id)
        if refresh_group_ids:
            # A customer login (or an administrator's refresh action) asks
            # for a fresh projection.  The portal cannot call Windows back;
            # invalidate only the requested groups and wake one follow-up
            # exchange instead.
            for group_id in refresh_group_ids:
                self._last_uploaded.pop(group_id, None)
            self.request_sync()
            logger.debug(
                "网页登录请求补传 %s 个本地群快照，已安排立即同步",
                len(refresh_group_ids),
            )
            for group_id in sorted(refresh_group_ids):
                _print_sync_group_action(
                    "网页拉取请求",
                    group_id,
                    detail="网页请求拉取 Windows 当前分群配置",
                )
        # Never silently discard a server request for a group absent from
        # this Windows installation.  The Linux page can otherwise remain in
        # a permanent "waiting for connection" state while the client keeps
        # reporting healthy heartbeats.  De-duplicate the warning so a stale
        # server hint does not flood CMD every interval.
        newly_unavailable = (
            unavailable_resync_group_ids
            - self._reported_unavailable_resync_group_ids
        )
        for group_id in sorted(newly_unavailable):
            _print_sync_group_action(
                "网页补传未完成",
                group_id,
                detail=(
                    "网页请求的群不在 Windows 本地配置或授权中，无法上传；"
                    "请先在本地加入或激活该群后再请求同步"
                ),
            )
        self._reported_unavailable_resync_group_ids.update(
            unavailable_resync_group_ids
        )
        self._reported_unavailable_resync_group_ids.intersection_update(
            unavailable_resync_group_ids
        )

        self._uploaded_console_event_ids.update(
            self._confirmed_console_event_ids(
                result,
                console_events,
                console_enabled=server_console_enabled,
            )
        )
        known_event_ids = self._console_event_ids(all_console_events)
        self._uploaded_console_event_ids.intersection_update(known_event_ids)

        applied: list[dict[str, Any]] = []
        # ``applied`` is also the ACK payload.  Keep the projection actually
        # confirmed by the portal so the local delta cache reflects a web
        # merge or a Windows-authoritative conflict resolution, rather than
        # an older payload captured just before the exchange started.
        acknowledged_config_cache: dict[str, dict[str, Any]] = {}
        local_conflicts = 0
        reply_asset_configs: dict[str, dict[str, Any]] = {}
        database_snapshot_configs: dict[str, dict[str, Any]] = {}
        failed_groups = 0
        changes = result.get("changes", [])
        if not isinstance(changes, list):
            changes = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            group_id = self._valid_failure_group_id(change.get("group_id"))
            try:
                revision = int(change.get("revision", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            projected = change.get("config")
            if not group_id or revision <= 0 or not isinstance(projected, dict):
                continue
            try:
                current = load_group_config(group_id)
                portal_auth = change.get("auth")
                if isinstance(portal_auth, dict):
                    from ..auth.manager import apply_portal_group_authorization

                    if not apply_portal_group_authorization(group_id, portal_auth):
                        raise RuntimeError("portal_auth_apply_failed")
                    auth_state = str(portal_auth.get("state", "missing") or "missing")
                    auth_expire = str(portal_auth.get("expire_text", "无授权记录") or "无授权记录")
                    _print_sync_group_action(
                        "网页群授权已恢复",
                        group_id,
                        detail=f"状态 {auth_state}；到期 {auth_expire}",
                    )
                projection_module = _sync_projection_module()
                is_current_web_revision = (
                    str(change.get("source", "")).strip().lower() == "web"
                )
                if (
                    is_current_web_revision
                    and group_id in locally_changed_config_group_ids
                    and group_id not in (self._server_managed_group_ids or set())
                    and group_id not in self._manual_web_pull_group_ids
                    and change.get("manual_pull") is not True
                ):
                    # Windows is the execution-side source of truth.  Do not
                    # write an older pending portal projection over a local
                    # command deletion/change that is already being uploaded
                    # in this same exchange.  ACK the exact pending revision
                    # with the current Windows projection: the server then
                    # resolves the stale revision without another full poll.
                    local_projection = projection_module.project_group_config(current)
                    if isinstance(local_projection, dict):
                        local_projection = dict(local_projection)
                        local_projection["group_id"] = group_id
                        remote_projection = dict(projected)
                        remote_projection["group_id"] = group_id
                        if local_projection != remote_projection:
                            # Linux owns command names.  A Windows-side
                            # runtime write can make the full projections
                            # differ at the same moment a portal command is
                            # saved; apply only the portal-owned command
                            # fields instead of discarding that web revision.
                            merge_commands = getattr(
                                projection_module,
                                "merge_portal_command_projection",
                                None,
                            )
                            command_projection = (
                                merge_commands(local_projection, remote_projection)
                                if callable(merge_commands)
                                else local_projection
                            )
                            if command_projection != local_projection:
                                projected = command_projection
                                _print_sync_group_action(
                                    "网页指令覆盖本地旧配置",
                                    group_id,
                                    detail=(
                                        "Linux 网页指令为准，已优先应用命令名称；"
                                        "其他 Windows 运行配置保持不变"
                                    ),
                                )
                            else:
                                change_detail = _sync_config_detail(
                                    remote_projection,
                                    local_projection,
                                    revision,
                                    change,
                                )
                                _print_sync_group_action(
                                    "网页旧配置已跳过",
                                    group_id,
                                    detail=(
                                        "本次已检测到 Windows 本地配置变更；"
                                        "按 Windows 为准确认网页旧修订；"
                                        + change_detail
                                    ),
                                )
                                applied.append(
                                    {
                                        "group_id": group_id,
                                        "revision": revision,
                                        "config": local_projection,
                                    }
                                )
                                acknowledged_config_cache[group_id] = local_projection
                                local_conflicts += 1
                                continue
                if is_current_web_revision:
                    try:
                        merged = projection_module.merge_group_projection(
                            current,
                            projected,
                            explicit_empty_deletes=True,
                        )
                    except TypeError as exc:
                        # Keep an in-place upgrade compatible with an older
                        # projection module that does not know the optional
                        # deletion flag.  Do not hide unrelated TypeErrors
                        # from the merge implementation itself.
                        if "explicit_empty_deletes" not in str(exc):
                            raise
                        merged = projection_module.merge_group_projection(
                            current, projected
                        )
                else:
                    merged = projection_module.merge_group_projection(
                        current, projected
                    )
                # Portal member operations are one-shot commands carried in
                # the same revision channel as configuration.  Execute them
                # on Windows, then remove them before the local save/ACK so a
                # retry cannot mute or blacklist the same member twice.
                portal_actions = merged.pop("portal_actions", None)
                if isinstance(portal_actions, list) and portal_actions and callable(self.portal_action_callback):
                    try:
                        callback_result = self.portal_action_callback(group_id, portal_actions[:32])
                        if inspect.isawaitable(callback_result):
                            callback_result = await callback_result
                        if isinstance(callback_result, list) and callback_result:
                            # Member actions are one-shot commands.  Failed
                            # actions are already recorded in the Windows
                            # feature diagnostics; putting them back into the
                            # synced config replays the same mute/unmute on
                            # every heartbeat and can flood the group.
                            logger.warning(
                                "网页群成员操作未成功但已消费，避免重复执行 group=%s failed=%s",
                                group_id,
                                len(callback_result),
                            )
                    except Exception:
                        logger.warning("网页群成员操作执行失败：group=%s", group_id, exc_info=True)
                    # The callback writes runtime state directly (for example
                    # deleting a blacklist row).  Rebuild the merged config
                    # from the post-action local snapshot so the subsequent
                    # save cannot overwrite that runtime change with the
                    # pre-action copy captured above.
                    try:
                        post_action_current = load_group_config(group_id)
                        try:
                            merged = projection_module.merge_group_projection(
                                post_action_current,
                                projected,
                                explicit_empty_deletes=True,
                            )
                        except TypeError as exc:
                            if "explicit_empty_deletes" not in str(exc):
                                raise
                            merged = projection_module.merge_group_projection(
                                post_action_current, projected
                            )
                        merged.pop("portal_actions", None)
                    except Exception:
                        logger.warning(
                            "网页群成员操作后重建本地配置失败 group=%s",
                            group_id,
                            exc_info=True,
                        )
                change_detail = _sync_config_detail(
                    current,
                    merged,
                    revision,
                    change,
                )
                _print_sync_group_action("网页配置拉取", group_id, detail=change_detail)
                if not save_group_config(group_id, merged):
                    summary = self._failure_summary("local_config_save_failed")
                    self._remember_failure(group_id, revision, summary)
                    _print_sync_group_action(
                        "网页配置保存失败",
                        group_id,
                        detail=f"{change_detail}；原因：{summary}",
                    )
                    logger.warning(
                        "网页配置未能写入本地：group=%s revision=%s error=%s",
                        group_id,
                        revision,
                        summary,
                    )
                    failed_groups += 1
                    continue
                _print_sync_group_action("网页配置已保存", group_id, detail=change_detail)
                applied.append(
                    {
                        "group_id": group_id,
                        "revision": revision,
                        "config": _sync_projection_module().project_group_config(merged),
                    }
                )
                acknowledged_config_cache[group_id] = applied[-1]["config"]
                if _config_has_portal_reply_assets(merged):
                    reply_asset_configs[group_id] = merged
                if self._database_snapshot_requests(merged):
                    database_snapshot_configs[group_id] = merged
            except Exception as exc:
                summary = self._failure_summary("local_apply_failed", exc)
                self._remember_failure(group_id, revision, summary)
                logger.warning(
                    "网页配置应用失败：group=%s revision=%s error=%s",
                    group_id,
                    revision,
                    summary,
                    exc_info=True,
                )
                _print_sync_group_action(
                    "网页配置保存失败",
                    group_id,
                    detail=f"版本 {revision}；原因：{summary}",
                )
                failed_groups += 1

        acknowledged_group_ids: set[str] = set()
        if applied:
            try:
                ack_result = await self._post_json(
                    "/api/sync/v1/ack",
                    {"client_id": self.client_id, "groups": applied},
                    timeout,
                )
                acknowledged = int(
                    ack_result.get("applied", -1)
                    if isinstance(ack_result, dict)
                    else -1
                )
                acknowledged_ids = {
                    self._valid_failure_group_id(value)
                    for value in (
                        ack_result.get("acknowledged_group_ids", [])
                        if isinstance(ack_result, dict)
                        else []
                    )
                    if self._valid_failure_group_id(value)
                }
                if acknowledged != len(applied):
                    self._manual_web_pull_group_ids.difference_update(acknowledged_ids)
                    if acknowledged_ids:
                        for item in applied:
                            if str(item["group_id"]) not in acknowledged_ids:
                                self._remember_failure(
                                    item["group_id"], item["revision"], "ack_not_confirmed"
                                )
                    else:
                        for item in applied:
                            self._remember_failure(
                                item["group_id"], item["revision"], "ack_not_confirmed"
                            )
                    logger.warning(
                        "网页配置 ACK 未完整确认：expected=%s acknowledged=%s confirmed_groups=%s",
                        len(applied),
                        acknowledged,
                        ",".join(sorted(acknowledged_ids)) or "-",
                    )
                    failed_groups += max(0, len(applied) - len(acknowledged_ids))
                    # Do not wait for the normal interval after a revision
                    # race or a partial ACK. Pull the newest server revision
                    # immediately; the next exchange is idempotent.
                    self.request_sync()
                else:
                    for item in applied:
                        self._clear_failure(item["group_id"], item["revision"])
                        acknowledged_group_ids.add(str(item["group_id"]))
                        _print_sync_group_action(
                            "网页配置同步完成",
                            item["group_id"],
                            detail=f"版本 {item['revision']} 已由网页确认",
                        )
                    self._manual_web_pull_group_ids.difference_update(
                        acknowledged_group_ids
                    )
            except Exception as exc:
                summary = self._failure_summary("ack_failed", exc)
                for item in applied:
                    self._remember_failure(item["group_id"], item["revision"], summary)
                logger.warning(
                    "网页配置 ACK 失败：groups=%s error=%s",
                    len(applied),
                    summary,
                )
                for item in applied:
                    _print_sync_group_action(
                        "网页配置同步失败",
                        item["group_id"],
                        detail=f"版本 {item['revision']}；原因：{summary}",
                    )
                failed_groups += len(applied)

        # The outbound cache is normally updated just after a successful
        # exchange.  A subsequently ACKed web merge (or the conflict path
        # above) is more recent than that captured payload.  Replace only the
        # config portion while retaining its healthy authorization projection.
        # Do not revive a cache entry the scope negotiation intentionally
        # invalidated for a newly bound/released group.
        for group_id in acknowledged_group_ids:
            if group_id in scope_cache_invalidations:
                continue
            cached_payload = current_groups.get(group_id)
            acknowledged_config = acknowledged_config_cache.get(group_id)
            if not isinstance(cached_payload, dict) or not isinstance(
                acknowledged_config, dict
            ):
                continue
            replacement = dict(cached_payload)
            replacement["config"] = acknowledged_config
            self._last_uploaded[group_id] = replacement

        # Asset bytes are not part of exchange/ACK.  Queue their download only
        # after the configuration was locally persisted, and never await it in
        # this request path: a slow photo must not delay ordinary page saves.
        queued_reply_asset_caches = 0
        queued_database_snapshot_caches = 0
        for group_id, merged in reply_asset_configs.items():
            queued_reply_asset_caches += int(
                self._queue_reply_asset_cache(group_id, merged, force=True)
            )
        # Database snapshots follow the same post-ACK, non-blocking pattern as
        # reply assets.  The descriptor carries only a local relative path,
        # revision and checksum; FTP credentials remain portal-side.
        for group_id, merged in database_snapshot_configs.items():
            queued_database_snapshot_caches += int(
                # A portal save is an authoritative revision change. Force one
                # fresh fetch now so a stale local SQLite file cannot be used
                # between ACK and the next periodic retry.
                self._queue_database_snapshot_cache(group_id, merged, force=True)
            )
        # A Windows restart can occur after the portal configuration was ACKed
        # but before a previous image cache finished.  Reconsider local rules
        # on each heartbeat; the per-group next-at map above makes this a
        # cheap structural check until the configured retry period elapses.
        if local_sources_complete:
            for group_id, config in groups.items():
                queued_reply_asset_caches += int(
                    self._queue_reply_asset_cache(group_id, config)
                )
                queued_database_snapshot_caches += int(
                    self._queue_database_snapshot_cache(group_id, config)
                )

        self.last_success_at = int(time.time())
        self.last_error = "" if not failed_groups else "group_apply_or_ack_failed"
        if not self.last_error:
            self._note_sync_recovery()
        return {
            "uploaded": len(changed_groups),
            "console_events": len(console_events),
            "applied": len(applied),
            "local_conflicts": local_conflicts,
            "failed": failed_groups,
            "reported_failures": len(reported_failures),
            "resync_requested": len(missing_group_ids | refresh_group_ids),
            "unavailable_resync_groups": len(unavailable_resync_group_ids),
            "reply_asset_cache_queued": queued_reply_asset_caches,
            "database_snapshot_cache_queued": queued_database_snapshot_caches,
            "local_sources_complete": local_sources_complete,
            "config_source_complete": config_source_complete,
            "auth_source_complete": auth_source_complete,
        }
