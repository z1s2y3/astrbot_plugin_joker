"""Environment-backed settings for the customer portal.

The portal intentionally keeps its configuration separate from AstrBot's
plugin configuration.  It can therefore be deployed on another machine with
its own MySQL database and reverse proxy.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

try:
    from dotenv import load_dotenv
except ImportError:  # Helpful error is raised later when dependencies are installed.
    load_dotenv = None


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_admin_path(value: str | None) -> str:
    """Validate the public administrator URL prefix."""
    path = str(value or "/admin").strip()
    if not path:
        path = "/admin"
    if (
        len(path) > 64
        or not path.startswith("/")
        or path.endswith("/")
        or not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", path)
        or path in {"/", "/user", "/api", "/static", "/health"}
    ):
        raise RuntimeError(
            "JOKER_WEB_ADMIN_PATH must be a path such as /control-panel; "
            "it cannot be /user, /api, /static, or /health"
        )
    return path


def normalize_public_origin(value: str | None) -> str:
    """Validate the Linux-owned public portal origin used by Windows.

    This is a deployment fallback, not a browser-editable setting.  It is
    used only when a CDN or reverse proxy strips the forwarded origin headers
    before a successful Windows HTTPS sync exchange.
    """
    from urllib.parse import urlsplit

    origin = str(value or "").strip().rstrip("/")
    if not origin:
        return ""
    parsed = urlsplit(origin)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() or char in "\\@?#" for char in parsed.netloc)
    ):
        raise RuntimeError(
            "JOKER_WEB_PUBLIC_ORIGIN must be an http(s) origin such as https://panel.example.com"
        )
    return f"{parsed.scheme.lower()}://{parsed.netloc}"


@dataclass(frozen=True)
class Settings:
    database_url: str
    session_secret: str
    card_pepper: str
    secure_cookies: bool
    portal_name: str
    session_max_age: int
    data_mode: str = "local"
    sync_token: str = ""
    admin_path: str = "/admin"
    public_origin: str = ""
    offline_after_seconds: int = 180
    # MySQL pool bounds are configurable for a busy multi-customer portal.
    # They remain deliberately capped so one web worker cannot exhaust the
    # database server when several browser saves arrive together.
    database_pool_size: int = 10
    database_max_overflow: int = 10
    database_pool_timeout: int = 5
    # Keep a slow sync transaction inside the app boundary so the reverse
    # proxy does not turn it into an opaque 502 for the Windows client.
    sync_exchange_timeout_seconds: int = 40

    @classmethod
    def from_env(cls) -> "Settings":
        if load_dotenv is not None:
            load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
        database_url = os.getenv("JOKER_WEB_DATABASE_URL", "").strip()
        if not database_url:
            mysql_user = quote_plus(os.getenv("JOKER_WEB_MYSQL_USER", "joker_web"))
            mysql_password = quote_plus(os.getenv("JOKER_WEB_MYSQL_PASSWORD", ""))
            mysql_host = os.getenv("JOKER_WEB_MYSQL_HOST", "127.0.0.1")
            mysql_port = os.getenv("JOKER_WEB_MYSQL_PORT", "3306")
            mysql_database = os.getenv("JOKER_WEB_MYSQL_DATABASE", "joker_portal")
            database_url = (
                f"mysql+pymysql://{mysql_user}:{mysql_password}@"
                f"{mysql_host}:{mysql_port}/{mysql_database}"
                "?charset=utf8mb4&connect_timeout=5&read_timeout=20&write_timeout=20"
            )

        session_secret = os.getenv("JOKER_WEB_SESSION_SECRET", "").strip()
        card_pepper = os.getenv("JOKER_WEB_CARD_PEPPER", "").strip()
        weak_secret = (
            len(session_secret) < 32
            or len(card_pepper) < 32
            or session_secret.startswith("replace_with_")
            or card_pepper.startswith("replace_with_")
        )
        if weak_secret:
            raise RuntimeError(
                "JOKER_WEB_SESSION_SECRET and JOKER_WEB_CARD_PEPPER must be "
                "different random values of at least 32 characters. Copy and "
                "edit web_client/.env.example before starting the portal."
            )
        if hmac_compare_equal(session_secret, card_pepper):
            raise RuntimeError(
                "JOKER_WEB_SESSION_SECRET and JOKER_WEB_CARD_PEPPER must be different."
            )

        try:
            session_max_age = int(os.getenv("JOKER_WEB_SESSION_MAX_AGE", "604800"))
        except ValueError:
            session_max_age = 604800

        data_mode = os.getenv("JOKER_WEB_DATA_MODE", "local").strip().lower()
        if data_mode not in {"local", "remote"}:
            raise RuntimeError("JOKER_WEB_DATA_MODE must be local or remote")
        sync_token = os.getenv("JOKER_WEB_SYNC_TOKEN", "").strip()
        if data_mode == "remote" and (
            len(sync_token) < 32 or sync_token.startswith("replace_with_")
        ):
            raise RuntimeError(
                "JOKER_WEB_SYNC_TOKEN must be a random value of at least 32 "
                "characters when JOKER_WEB_DATA_MODE=remote."
            )
        admin_path = normalize_admin_path(os.getenv("JOKER_WEB_ADMIN_PATH", "/admin"))
        public_origin = normalize_public_origin(os.getenv("JOKER_WEB_PUBLIC_ORIGIN", ""))
        try:
            offline_after_seconds = int(
                os.getenv("JOKER_WEB_OFFLINE_AFTER_SECONDS", "180")
            )
        except ValueError:
            offline_after_seconds = 180

        def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(os.getenv(name, str(default)))
            except ValueError:
                value = default
            return max(minimum, min(maximum, value))

        return cls(
            database_url=database_url,
            session_secret=session_secret,
            card_pepper=card_pepper,
            secure_cookies=_as_bool(os.getenv("JOKER_WEB_SECURE_COOKIES"), True),
            portal_name=os.getenv("JOKER_WEB_PORTAL_NAME", "Joker 群管客户中心"),
            session_max_age=max(300, min(2_592_000, session_max_age)),
            data_mode=data_mode,
            sync_token=sync_token,
            admin_path=admin_path,
            public_origin=public_origin,
            offline_after_seconds=max(30, min(86_400, offline_after_seconds)),
            database_pool_size=bounded_int(
                "JOKER_WEB_DATABASE_POOL_SIZE", 10, 2, 50
            ),
            database_max_overflow=bounded_int(
                "JOKER_WEB_DATABASE_MAX_OVERFLOW", 10, 0, 100
            ),
            database_pool_timeout=bounded_int(
                "JOKER_WEB_DATABASE_POOL_TIMEOUT", 5, 1, 30
            ),
            sync_exchange_timeout_seconds=bounded_int(
                "JOKER_WEB_SYNC_EXCHANGE_TIMEOUT", 40, 8, 90
            ),
        )


def hmac_compare_equal(left: str, right: str) -> bool:
    # Importing hmac locally keeps this settings module dependency-free.
    import hmac

    return hmac.compare_digest(left, right)
