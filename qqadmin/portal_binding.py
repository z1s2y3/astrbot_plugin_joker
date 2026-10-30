"""Safe Windows-to-portal binding helpers.

The Linux host is a *web configuration portal*, not a remote controller for
AstrBot.  Consequently this module deliberately models only the three values
needed by the outbound sync protocol:

* the public HTTPS portal URL;
* the portal sync token; and
* the Card Pepper used for irreversible card fingerprints.

In particular, there are no SSH, root, Linux username, password, or port
fields here.  The Windows plugin never needs to log into the Linux host.  The
portal's reverse proxy should route a user-selected domain to the portal
process, while the plugin connects to that domain over HTTPS.

``probe_portal_binding`` is safe to call from a future settings-page button or
an owner-only command.  It first checks ``/health/ready`` and then sends the
smallest valid ``/api/sync/v1/exchange`` request.  The exchange contains no
group IDs, configurations, cards, or console records and explicitly sets
``authoritative_group_ids`` to ``False``.  It therefore validates the URL,
sync token, remote-mode setting, and Card Pepper proof without changing local
or remote group configuration.
"""

from __future__ import annotations

import hmac
import hashlib
import ipaddress
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx


MIN_SECRET_LENGTH = 32
"""Minimum token and Card Pepper length, matching ``PortalSyncClient``."""

CARD_PEPPER_PROOF_CONTEXT = b"joker-portal-card-pepper-proof-v1"
"""Must remain byte-for-byte compatible with the Linux portal."""

BINDING_PROBE_CLIENT_ID = "windows-00000000000000000000000000000000"
"""Non-persistent client marker used only by the zero-data probe."""

# These names belong to abandoned "Windows connects to Linux through SSH"
# designs.  They are intentionally not represented by ``PortalBinding``.  A
# caller can use ``legacy_server_credential_fields`` to warn an administrator
# during a configuration migration without ever reading or displaying values.
_LEGACY_SERVER_CREDENTIAL_FIELDS = frozenset(
    {
        "server_ip",
        "server_host",
        "server_port",
        "server_username",
        "server_user",
        "server_password",
        "root_password",
        "ssh_host",
        "ssh_port",
        "ssh_username",
        "ssh_password",
    }
)

_SAFE_SYNC_ERROR_CODES = frozenset(
    {
        "cdn_origin_tls_handshake_failed",
        "cdn_origin_unavailable",
        "card_pepper_mismatch",
        "connect_timeout",
        "group_apply_or_ack_failed",
        "invalid_json_response",
        "member_directory_storage_busy",
        "read_error",
        "response_timeout",
        "request_retry_exhausted",
        "remote_mode_disabled",
        "sync_error",
        "sync_storage_busy",
        "sync_timeout",
        "sync_token_invalid",
        "tls_handshake_failed",
        "timeout",
        "transport_error",
    }
)


def _sync_error_message(code: str) -> str:
    """Return a fixed, actionable Chinese description for a safe error code.

    Only stable codes reach this function.  In particular, raw HTTP-client
    exception text is deliberately never shown to QQ users: it can contain a
    URL, proxy address, or an Authorization header fragment.
    """

    messages = {
        "cdn_origin_tls_handshake_failed": (
            "CDN 回源 TLS 握手失败（HTTP 525），这不是卡密或关联问题，也不是令牌问题："
            "请将 CDN/EdgeOne 回源设为 "
            "HTTPS:443，回源 Host 和 SNI 都填写网页域名，并检查源站证书链与 443 放行"
        ),
        "cdn_origin_unavailable": (
            "CDN 回源异常：请检查源站 443、Nginx 反代、源站防火墙及 CDN 回源地址"
        ),
        "tls_handshake_failed": (
            "HTTPS/TLS 握手或证书验证失败：请检查网页域名证书、完整证书链、"
            "电脑系统时间和 CDN 回源 SNI"
        ),
        "read_error": (
            "已连接网页但读取响应中断：请检查 CDN、Nginx 反代和源站网络，"
            "系统会按同步间隔重试"
        ),
        "timeout": "网页同步请求超时：系统会按同步间隔重试",
        "connect_timeout": "无法连接网页配置中心：请检查网络、域名解析和 HTTPS 反代，系统会按同步间隔重试",
        "response_timeout": "已连接网页配置中心，但等待同步响应超时，系统会按同步间隔重试",
        "transport_error": "无法连接网页配置中心：请检查网络、域名解析和 HTTPS 反代，系统会按同步间隔重试",
        "request_retry_exhausted": "网页同步请求重试后仍失败：请检查网页服务和网络链路",
        "remote_mode_disabled": "网页服务器未启用远程同步模式：请检查 Linux 网页配置",
        "card_pepper_mismatch": "Card Pepper 与网页服务器不一致",
        "sync_token_invalid": "同步令牌无效：请核对 Windows 插件与网页服务器的同步令牌",
        "group_apply_or_ack_failed": "部分网页配置未能写入本地或确认，下一次同步会继续处理",
        "invalid_json_response": "网页同步接口返回无效数据，请检查网页服务版本",
        "sync_error": "网页同步失败，系统会按同步间隔重试",
        "sync_storage_busy": "网页数据库正被其他同步占用，Windows 会自动重试",
        "sync_timeout": "网页已收到同步请求，但处理时间较长，Windows 会自动重试",
        "member_directory_storage_busy": "网页成员目录正在写入，Windows 会自动重试",
    }
    if re.fullmatch(r"http_status_525", code):
        return messages["cdn_origin_tls_handshake_failed"]
    if re.fullmatch(r"http_status_52[0-4]", code):
        return messages["cdn_origin_unavailable"]
    if re.fullmatch(r"http_status_[1-5]\d\d", code):
        return f"网页同步接口返回 HTTP {code.rsplit('_', 1)[-1]}，系统会按同步间隔重试"
    return messages.get(code, messages["sync_error"])


def _exception_has_tls_signal(exc: Exception) -> bool:
    """Identify TLS failures without retaining or displaying exception text."""

    current: BaseException | None = exc
    seen: set[int] = set()
    for _ in range(4):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        class_name = type(current).__name__.lower()
        if any(marker in class_name for marker in ("ssl", "tls", "certificate")):
            return True
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


def _classify_request_error(exc: Exception) -> tuple[str, str]:
    """Map transport exceptions to fixed user-safe diagnostic results."""

    class_name = type(exc).__name__.lower()
    if class_name in {"connecttimeout", "connect_timeout"}:
        code = "connect_timeout"
    elif "timeout" in class_name:
        code = "response_timeout"
    elif _exception_has_tls_signal(exc):
        code = "tls_handshake_failed"
    elif class_name in {
        "readerror",
        "read_error",
        "remoteprotocolerror",
        "remote_protocol_error",
    }:
        code = "read_error"
    else:
        code = "transport_error"
    return code, _sync_error_message(code)


def _classify_http_status(status_code: object, fallback_code: str) -> tuple[str, str]:
    """Classify CDN-specific HTTP responses without exposing response bodies."""

    if status_code == 525:
        code = "cdn_origin_tls_handshake_failed"
    elif isinstance(status_code, int) and 520 <= status_code <= 524:
        code = "cdn_origin_unavailable"
    else:
        code = fallback_code
    return code, _sync_error_message(code)


def _unwrap_config_value(value: object) -> object:
    """Read either AstrBot's stored values or a schema-shaped default value.

    AstrBot normally stores ``web_sync`` as a simple mapping.  Some migration
    and focused-test callers use the schema's ``items``/``default`` envelopes;
    accepting both keeps the binding helper independent of the settings UI.
    """

    current = value
    for _ in range(4):
        if not isinstance(current, Mapping):
            break
        if "items" in current and isinstance(current["items"], Mapping):
            current = current["items"]
            continue
        if "default" in current:
            current = current["default"]
            continue
        break
    return current


def _read_mapping_value(mapping: Mapping[str, object], key: str, default: object) -> object:
    return _unwrap_config_value(mapping.get(key, default))


def _as_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _safe_interval(value: object) -> int:
    try:
        interval = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        interval = 5
    return max(3, min(3600, interval))


def _normalized_portal_url(value: object) -> tuple[str, str]:
    """Return ``(canonical_url, error)`` without preserving unsafe URL parts."""

    raw = str(value or "").strip()
    if not raw:
        return "", "网页同步地址不能为空"
    if len(raw) > 2048:
        return "", "网页同步地址过长"
    try:
        parsed = urlsplit(raw)
        # Accessing ``port`` makes urllib reject malformed values such as
        # ``:abc`` rather than carrying them through into an HTTP request.
        port = parsed.port
    except ValueError:
        return "", "网页同步地址格式无效"

    if parsed.scheme.lower() != "https":
        return "", "网页同步地址必须使用 HTTPS 域名，不能使用 HTTP 或服务器 IP"
    if not parsed.netloc or not parsed.hostname:
        return "", "网页同步地址必须包含域名"
    if parsed.username is not None or parsed.password is not None:
        return "", "网页同步地址不能包含账号或密码"
    if parsed.query or parsed.fragment:
        return "", "网页同步地址不能包含查询参数或锚点"
    if port is not None and not 1 <= port <= 65535:
        return "", "网页同步地址端口无效"

    hostname = parsed.hostname.strip().lower()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        # The portal certificate is issued to a hostname.  Rejecting an IP
        # here also prevents the common accidental "domain redirects to IP"
        # deployment mistake from becoming a saved Windows binding.
        return "", "网页同步地址应填写已配置 HTTPS 证书的域名，不应填写服务器 IP"

    # URL parsing accepts a few host forms which cannot be a public HTTPS
    # hostname.  Keep the check intentionally ASCII/IDNA-friendly; a caller
    # with an internationalized name can enter its punycode representation.
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", hostname):
        return "", "网页同步地址域名无效"
    if "." not in hostname or ".." in hostname:
        return "", "网页同步地址请填写完整域名，例如 https://portal.example.com"

    path = parsed.path.rstrip("/")
    canonical = urlunsplit(("https", parsed.netloc, path, "", "")).rstrip("/")
    return canonical, ""


def _card_pepper_proof(card_pepper: str) -> str:
    """Return the proof understood by the portal without sending the pepper."""

    return hmac.new(
        card_pepper.encode("utf-8"), CARD_PEPPER_PROOF_CONTEXT, hashlib.sha256
    ).hexdigest()


def _safe_error_code(value: object) -> str:
    """Map a sync error to a fixed safe code, never returning exception text."""

    candidate = str(value or "").strip().lower()
    # Accept the compact class-name codes emitted by older Windows plugin
    # builds, but expose only the canonical protocol codes from this point on.
    candidate = {
        "connecttimeout": "connect_timeout",
        "readerror": "read_error",
        "remoteprotocolerror": "read_error",
        "readtimeout": "response_timeout",
        "writetimeout": "response_timeout",
        "pooltimeout": "response_timeout",
    }.get(candidate, candidate)
    if candidate in _SAFE_SYNC_ERROR_CODES:
        return candidate
    if re.fullmatch(r"http_status_[1-5]\d\d", candidate):
        return candidate
    return "sync_error" if candidate else ""


def _friendly_sync_error(error_code: object) -> str:
    """Translate a safe sync code without ever exposing transport details."""

    code = _safe_error_code(error_code)
    return _sync_error_message(code) if code else "同步失败"


def _http_failure_message(status_code: int) -> tuple[str, str] | None:
    """Return a special probe code/message for infrastructure-level HTTP errors."""

    if 520 <= status_code <= 525:
        return _classify_http_status(status_code, "portal_unhealthy")
    return None


def _safe_timestamp(value: object) -> int:
    try:
        timestamp = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, timestamp)


@dataclass(frozen=True)
class PortalBinding:
    """The only persisted values required to bind Windows to the web portal.

    Values are not masked inside this object because the sync client needs to
    use them.  Never convert this dataclass to a log line or an error message;
    use :meth:`safe_status` instead.
    """

    portal_url: str = ""
    sync_token: str = ""
    card_pepper: str = ""
    enabled: bool = False
    interval_seconds: int = 5

    @classmethod
    def from_plugin_config(cls, plugin_config: Mapping[str, object] | None) -> "PortalBinding":
        """Read the established ``web_sync`` configuration without migration writes."""

        root = plugin_config if isinstance(plugin_config, Mapping) else {}
        section = _unwrap_config_value(root.get("web_sync", {}))
        web_sync = section if isinstance(section, Mapping) else {}
        return cls(
            portal_url=str(_read_mapping_value(web_sync, "portal_url", "") or "").strip(),
            sync_token=str(_read_mapping_value(web_sync, "sync_token", "") or "").strip(),
            card_pepper=str(_read_mapping_value(web_sync, "card_pepper", "") or "").strip(),
            enabled=_as_bool(_read_mapping_value(web_sync, "enabled", False)),
            interval_seconds=_safe_interval(
                _read_mapping_value(web_sync, "interval_seconds", 5)
            ),
        )

    @property
    def normalized_portal_url(self) -> str:
        """Return the safe canonical URL, or an empty string if invalid."""

        return _normalized_portal_url(self.portal_url)[0]

    @property
    def configuration_error(self) -> str:
        """Return a concise, safe-to-display validation error, if any."""

        _url, url_error = _normalized_portal_url(self.portal_url)
        if url_error:
            return url_error
        token = self.sync_token.strip()
        if len(token) < MIN_SECRET_LENGTH or token.lower().startswith("replace_with_"):
            return "同步令牌必须至少 32 位，且不能使用示例值"
        pepper = self.card_pepper.strip()
        if len(pepper) < MIN_SECRET_LENGTH or pepper.lower().startswith("replace_with_"):
            return "Card Pepper 必须至少 32 位，且必须与网页服务器完全一致"
        return ""

    def validate(self) -> str:
        """Compatibility-friendly method form of :attr:`configuration_error`."""

        return self.configuration_error

    @property
    def configured(self) -> bool:
        return not self.configuration_error

    def endpoint(self, suffix: str) -> str:
        """Build an API URL only after strict validation has succeeded."""

        base = self.normalized_portal_url
        if not base:
            raise ValueError("portal binding is not configured")
        if not suffix.startswith("/"):
            raise ValueError("portal endpoint must begin with '/'")
        return f"{base}{suffix}"

    def safe_status(self) -> dict[str, object]:
        """Return displayable metadata without tokens, peppers, or SSH fields."""

        error = self.configuration_error
        return {
            "enabled": self.enabled,
            "configured": not bool(error),
            "portal_url": self.normalized_portal_url if not error else "",
            "interval_seconds": self.interval_seconds,
            "configuration_error": error,
        }


def legacy_server_credential_fields(
    plugin_config: Mapping[str, object] | None,
) -> tuple[str, ...]:
    """Return legacy SSH-field *names* present in ``web_sync``, never values.

    This gives a settings migration a way to say "remove these stale fields"
    without accidentally copying a root password into a new configuration or a
    log.  The returned names are sorted for stable tests and UI rendering.
    """

    root = plugin_config if isinstance(plugin_config, Mapping) else {}
    section = _unwrap_config_value(root.get("web_sync", {}))
    if not isinstance(section, Mapping):
        return ()
    return tuple(
        sorted(
            str(key)
            for key in section
            if str(key).strip().lower() in _LEGACY_SERVER_CREDENTIAL_FIELDS
        )
    )


@dataclass(frozen=True)
class PortalProbeResult:
    """Non-sensitive result of a portal binding probe."""

    code: str
    message: str
    portal_url: str = ""
    configured: bool = False
    reachable: bool = False
    authenticated: bool = False
    card_pepper_verified: bool = False
    remote_mode: bool = False
    http_status: int = 0
    checked_at: int = 0

    @property
    def ok(self) -> bool:
        return (
            self.code == "connected"
            and self.configured
            and self.reachable
            and self.authenticated
            and self.card_pepper_verified
            and self.remote_mode
        )

    def safe_status(self) -> dict[str, object]:
        """A JSON/UI-friendly representation that cannot disclose secrets."""

        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "portal_url": self.portal_url,
            "configured": self.configured,
            "reachable": self.reachable,
            "authenticated": self.authenticated,
            "card_pepper_verified": self.card_pepper_verified,
            "remote_mode": self.remote_mode,
            "http_status": self.http_status or None,
            "checked_at": self.checked_at,
        }

    def format_text(self) -> str:
        """Render a short status message suitable for an owner-only command."""

        title = "网页配置中心连接测试：成功" if self.ok else "网页配置中心连接测试：失败"
        lines = [title, f"状态：{self.message}"]
        if self.portal_url:
            lines.append(f"网页地址：{self.portal_url}")
        if self.http_status:
            lines.append(f"HTTP：{self.http_status}")
        return "\n".join(lines)


def _probe_result(
    code: str,
    message: str,
    binding: PortalBinding,
    *,
    reachable: bool = False,
    authenticated: bool = False,
    card_pepper_verified: bool = False,
    remote_mode: bool = False,
    http_status: int = 0,
) -> PortalProbeResult:
    return PortalProbeResult(
        code=code,
        message=message,
        portal_url=binding.normalized_portal_url if binding.configured else "",
        configured=binding.configured,
        reachable=reachable,
        authenticated=authenticated,
        card_pepper_verified=card_pepper_verified,
        remote_mode=remote_mode,
        http_status=http_status,
        checked_at=int(time.time()),
    )


def _response_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


async def probe_portal_binding(
    binding: PortalBinding,
    *,
    client_id: str = BINDING_PROBE_CLIENT_ID,
    timeout_seconds: float = 8.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PortalProbeResult:
    """Check portal reachability and all sync credentials without group data.

    The test deliberately refuses redirects.  A redirect usually indicates a
    reverse-proxy/domain configuration error; following it could hide an
    accidental redirect to a public server IP and may send the bearer token to
    a different origin.
    """

    error = binding.configuration_error
    if error:
        return _probe_result("configuration_error", error, binding)

    safe_client_id = re.sub(r"[^A-Za-z0-9._-]", "_", str(client_id or ""))[:64]
    if not safe_client_id:
        safe_client_id = BINDING_PROBE_CLIENT_ID
    timeout = max(1.0, min(30.0, float(timeout_seconds or 8.0)))
    sync_headers = {
        "Authorization": f"Bearer {binding.sync_token.strip()}",
        "X-Joker-Client": safe_client_id,
        "X-Joker-Card-Pepper-Proof": _card_pepper_proof(binding.card_pepper.strip()),
    }
    client_kwargs: dict[str, object] = {
        "timeout": httpx.Timeout(timeout, connect=min(5.0, timeout)),
        "follow_redirects": False,
        # A binding test must exercise the same direct route as periodic
        # synchronization.  Environment proxy variables are unrelated to the
        # portal binding and frequently contain stale local proxy endpoints.
        "trust_env": False,
        # The unauthenticated health check deliberately gets no bearer token
        # or pepper proof.  Credentials are sent only to the exact HTTPS sync
        # endpoint after the configured origin has passed that first check.
        "headers": {"Accept": "application/json"},
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    probe_stage = "health"
    reachable = False
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            health = await client.get(binding.endpoint("/health/ready"))
            if health.is_redirect:
                return _probe_result(
                    "unexpected_redirect",
                    "网页地址发生重定向，请检查域名反代和 HTTPS 配置",
                    binding,
                    reachable=True,
                    http_status=health.status_code,
                )
            if health.status_code != 200:
                infrastructure_failure = _http_failure_message(health.status_code)
                if infrastructure_failure is not None:
                    return _probe_result(
                        infrastructure_failure[0],
                        infrastructure_failure[1],
                        binding,
                        reachable=True,
                        http_status=health.status_code,
                    )
                return _probe_result(
                    "portal_unhealthy",
                    "网页服务或数据库尚未就绪",
                    binding,
                    reachable=True,
                    http_status=health.status_code,
                )
            health_payload = _response_json(health)
            if not health_payload or health_payload.get("status") != "ok":
                return _probe_result(
                    "invalid_health_response",
                    "网页健康检查返回无效结果",
                    binding,
                    reachable=True,
                    http_status=health.status_code,
                )

            reachable = True
            probe_stage = "exchange"
            # Do not reuse a real periodic exchange here: a connection test
            # must neither upload a local group projection nor ACK a pending
            # webpage edit.  This payload is accepted by the current portal
            # and leaves ``apply_bot_exchange`` with no snapshots to mutate.
            exchange = await client.post(
                binding.endpoint("/api/sync/v1/exchange"),
                headers=sync_headers,
                json={
                    "client_id": safe_client_id,
                    "known_group_ids": [],
                    "authoritative_group_ids": False,
                    "groups": [],
                    "console_events": [],
                    "apply_failures": [],
                },
            )
    except httpx.TimeoutException as exc:
        code, message = _classify_request_error(exc)
        if reachable and probe_stage == "exchange":
            message = "网页健康检查正常，但同步接口超时：请检查网页数据库锁、反代超时和同步接口响应"
        return _probe_result(code, message, binding, reachable=reachable)
    except httpx.RequestError as exc:
        code, message = _classify_request_error(exc)
        if reachable and probe_stage == "exchange":
            message = "网页地址可达，但同步接口连接失败：请检查反代转发和网页同步接口"
        return _probe_result(code, message, binding, reachable=reachable)
    except (TypeError, ValueError, OverflowError):
        # Do not include exception text: proxy and TLS errors can contain a
        # full URL or other deployment-specific information.
        return _probe_result("probe_error", "网页连接测试失败", binding)

    if exchange.is_redirect:
        return _probe_result(
            "unexpected_redirect",
            "同步接口发生重定向，请检查域名反代和 HTTPS 配置",
            binding,
            reachable=True,
            http_status=exchange.status_code,
        )

    payload = _response_json(exchange)
    if exchange.status_code == 409:
        if payload and payload.get("code") == "card_pepper_mismatch":
            return _probe_result(
                "card_pepper_mismatch",
                "Card Pepper 与网页服务器不一致",
                binding,
                reachable=True,
                authenticated=True,
                remote_mode=True,
                http_status=exchange.status_code,
            )
        return _probe_result(
            "remote_mode_unavailable",
            "网页未启用远程同步模式，或同步协议不匹配",
            binding,
            reachable=True,
            authenticated=True,
            http_status=exchange.status_code,
        )
    if exchange.status_code != 200:
        infrastructure_failure = _http_failure_message(exchange.status_code)
        if infrastructure_failure is not None:
            return _probe_result(
                infrastructure_failure[0],
                infrastructure_failure[1],
                binding,
                reachable=True,
                authenticated=True,
                card_pepper_verified=True,
                remote_mode=True,
                http_status=exchange.status_code,
            )
        return _probe_result(
            "sync_authentication_failed",
            "同步令牌无效，或网页同步接口不可用",
            binding,
            reachable=True,
            http_status=exchange.status_code,
        )
    if not payload or payload.get("status") != "ok":
        return _probe_result(
            "invalid_sync_response",
            "网页同步接口返回无效结果",
            binding,
            reachable=True,
            authenticated=True,
            card_pepper_verified=True,
            remote_mode=True,
            http_status=exchange.status_code,
        )
    return _probe_result(
        "connected",
        "已验证网页地址、同步令牌和 Card Pepper",
        binding,
        reachable=True,
        authenticated=True,
        card_pepper_verified=True,
        remote_mode=True,
        http_status=exchange.status_code,
    )


@dataclass(frozen=True)
class PortalRuntimeStatus:
    """Safe summary of the periodic ``PortalSyncClient`` state."""

    code: str
    message: str
    enabled: bool
    configured: bool
    running: bool | None
    portal_url: str
    last_success_at: int = 0
    last_error_code: str = ""

    def safe_status(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "enabled": self.enabled,
            "configured": self.configured,
            "running": self.running,
            "portal_url": self.portal_url,
            "last_success_at": self.last_success_at or None,
            "last_error_code": self.last_error_code or None,
        }

    def format_text(self) -> str:
        lines = [f"网页配置中心状态：{self.message}"]
        if self.portal_url:
            lines.append(f"网页地址：{self.portal_url}")
        if self.last_success_at:
            lines.append(
                "最近成功同步："
                + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_success_at))
            )
        if self.last_error_code:
            lines.append(f"最近错误：{_friendly_sync_error(self.last_error_code)}")
        return "\n".join(lines)


def portal_runtime_status(
    binding: PortalBinding,
    sync_client: object | None = None,
    sync_task: object | None = None,
) -> PortalRuntimeStatus:
    """Build a safe status view from a ``PortalSyncClient``-like object.

    ``sync_client`` intentionally uses duck typing so importing this module
    does not import AstrBot or create a circular dependency with
    ``qqadmin.web_sync``.  Only its non-sensitive ``last_success_at`` and
    already-sanitized ``last_error`` attributes are read.
    """

    if not binding.enabled:
        return PortalRuntimeStatus(
            code="disabled",
            message="网页同步未启用",
            enabled=False,
            configured=binding.configured,
            running=False,
            portal_url=binding.normalized_portal_url if binding.configured else "",
        )

    configuration_error = binding.configuration_error
    if configuration_error:
        return PortalRuntimeStatus(
            code="configuration_error",
            message=configuration_error,
            enabled=True,
            configured=False,
            running=False,
            portal_url="",
        )

    running: bool | None = None
    if sync_task is not None:
        done = getattr(sync_task, "done", None)
        if callable(done):
            try:
                running = not bool(done())
            except Exception:
                running = None

    last_success_at = _safe_timestamp(getattr(sync_client, "last_success_at", 0))
    last_error_code = _safe_error_code(getattr(sync_client, "last_error", ""))
    if last_error_code:
        return PortalRuntimeStatus(
            code="degraded",
            message="网页同步最近一次失败，正在按同步间隔重试",
            enabled=True,
            configured=True,
            running=running,
            portal_url=binding.normalized_portal_url,
            last_success_at=last_success_at,
            last_error_code=last_error_code,
        )
    if last_success_at:
        return PortalRuntimeStatus(
            code="connected",
            message="网页同步正常",
            enabled=True,
            configured=True,
            running=running,
            portal_url=binding.normalized_portal_url,
            last_success_at=last_success_at,
        )
    return PortalRuntimeStatus(
        code="waiting",
        message="等待首次网页同步",
        enabled=True,
        configured=True,
        running=running,
        portal_url=binding.normalized_portal_url,
    )


__all__ = [
    "BINDING_PROBE_CLIENT_ID",
    "CARD_PEPPER_PROOF_CONTEXT",
    "MIN_SECRET_LENGTH",
    "PortalBinding",
    "PortalProbeResult",
    "PortalRuntimeStatus",
    "legacy_server_credential_fields",
    "portal_runtime_status",
    "probe_portal_binding",
]
