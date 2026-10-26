"""Session middleware with a database-backed, runtime cookie lifetime."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

from itsdangerous import BadSignature
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import Session, SessionMiddleware
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send


CookieResolver = Callable[[Scope], str]
_COOKIE_NAME_CHARS = frozenset(
    "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)


class PortalSessionMiddleware(SessionMiddleware):
    """Starlette's signed session cookie with a per-request max-age provider.

    The provider is intentionally a cheap in-memory callback.  The database
    value is loaded once at startup and refreshed immediately after an admin
    saves the setting, so concurrent browser requests do not contend on MySQL.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        secret_key: str | bytes,
        session_cookie: str = "session",
        max_age: int = 14 * 24 * 60 * 60,
        max_age_provider: Callable[[], int] | None = None,
        cookie_resolver: CookieResolver | None = None,
        legacy_session_cookie: str | None = None,
        path: str = "/",
        same_site: str = "lax",
        https_only: bool = False,
    ) -> None:
        super().__init__(
            app,
            secret_key=secret_key,
            session_cookie=session_cookie,
            max_age=max_age,
            path=path,
            same_site=same_site,
            https_only=https_only,
        )
        self._max_age_provider = max_age_provider
        self._cookie_resolver = cookie_resolver
        self.legacy_session_cookie = legacy_session_cookie

    def _session_cookie_for_scope(self, scope: Scope) -> str:
        """Resolve the cookie namespace for an HTTP route.

        A portal can expose both customer and administrator pages on one
        hostname.  Keeping the resolver here means the signed session format
        and runtime expiry remain shared, while the browser credentials are
        still isolated by cookie name and route namespace.
        """
        if self._cookie_resolver is None:
            return self.session_cookie
        try:
            value = str(self._cookie_resolver(scope) or "").strip()
        except Exception:
            value = ""
        # Cookie names are deliberately constrained to the characters used by
        # the portal constants.  A bad resolver must never create a header
        # injection or silently disable session handling.
        if not value or any(character not in _COOKIE_NAME_CHARS for character in value):
            return self.session_cookie
        return value

    def _current_max_age(self) -> int:
        try:
            value = int(self._max_age_provider()) if self._max_age_provider else self.max_age
        except (TypeError, ValueError, OverflowError):
            value = self.max_age
        return max(300, min(2_592_000, value))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        session_cookie = self._session_cookie_for_scope(scope)
        max_age = self._current_max_age()
        initial_session_was_empty = True

        if session_cookie in connection.cookies:
            data = connection.cookies[session_cookie].encode("utf-8")
            try:
                data = self.signer.unsign(data, max_age=max_age)
                data = base64.b64decode(data)
                session = Session(json.loads(data))
                initial_session_was_empty = False
            except (BadSignature, json.JSONDecodeError, ValueError, TypeError):
                session = Session()
        else:
            session = Session()

        scope["session"] = session

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                session: Session = scope["session"]
                headers = MutableHeaders(scope=message)
                if session.accessed:
                    headers.add_vary_header("Cookie")
                if session.modified and session:
                    data = base64.b64encode(json.dumps(session).encode("utf-8"))
                    data = self.signer.sign(data)
                    header = (
                        f"{session_cookie}={data.decode('utf-8')}; "
                        f"path={self.path}; Max-Age={max_age}; {self.security_flags}"
                    )
                    headers.append("Set-Cookie", header)
                elif session.modified and not initial_session_was_empty:
                    header = (
                        f"{session_cookie}=null; path={self.path}; "
                        f"expires=Thu, 01 Jan 1970 00:00:00 GMT; {self.security_flags}"
                    )
                    headers.append("Set-Cookie", header)
                # Releases before route-level separation used one shared
                # cookie.  Expire it once the browser sends it, so an old
                # signed session cannot linger beside the isolated cookies.
                legacy_cookie = self.legacy_session_cookie
                if (
                    legacy_cookie
                    and legacy_cookie != session_cookie
                    and legacy_cookie in connection.cookies
                ):
                    headers.append(
                        "Set-Cookie",
                        f"{legacy_cookie}=null; path={self.path}; "
                        f"expires=Thu, 01 Jan 1970 00:00:00 GMT; {self.security_flags}",
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)
