#!/usr/bin/env python3
"""Safely render the per-domain Nginx configuration for the Joker portal."""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import re
import tempfile


DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
PATH_RE = re.compile(r"^/[A-Za-z0-9._/@+=,:~%\-]+$")
TOKENS = {
    "__JOKER_PORTAL_DOMAIN__",
    "__JOKER_NGINX_SITE_ROOT__",
    "__JOKER_HTTP_ROUTE__",
    "__JOKER_TLS_SERVER__",
}


def _safe_domain(value: str) -> str:
    value = value.strip().lower()
    if not DOMAIN_RE.fullmatch(value):
        raise ValueError("portal address must be a DNS domain without scheme, port, or path")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("portal address must use a DNS domain, not a server IP")
    return value


def _safe_path(value: str, label: str) -> str:
    value = value.strip()
    if not PATH_RE.fullmatch(value):
        raise ValueError(f"{label} is not a safe absolute path")
    return value.rstrip("/") or "/"


def _proxy_location(domain: str, protocol: str, port: int) -> str:
    return f'''    # A portal update can contain an 80 MiB archive. Keep this
    # exception narrow so normal browser requests still fail quickly.
    location = /admin/update {{
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host {domain};
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host {domain};
        proxy_set_header X-Forwarded-Port {port};
        proxy_set_header X-Forwarded-Proto {protocol};
        proxy_set_header Connection "";
        proxy_redirect off;
        client_body_timeout 600s;
        proxy_connect_timeout 10s;
        proxy_send_timeout 600s;
        proxy_read_timeout 600s;
        # Starlette spools the multipart body to a private temporary file and
        # enforces the same size limit. Avoid a second Nginx disk buffer.
        proxy_request_buffering off;
        proxy_buffering off;
    }}

    # Keep the machine-to-machine game channel on dedicated exact paths.
    # The normal location below intentionally clears Connection; without this
    # exception Nginx would terminate the WebSocket upgrade before it reaches
    # FastAPI. Exact locations keep ordinary API requests on normal timeouts.
    location = /api/sync/v1/ws {{
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host {domain};
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host {domain};
        proxy_set_header X-Forwarded-Port {port};
        proxy_set_header X-Forwarded-Proto {protocol};
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 120s;
        proxy_buffering off;
        proxy_redirect off;
    }}

    location = /api/game/v1/ws {{
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host {domain};
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host {domain};
        proxy_set_header X-Forwarded-Port {port};
        proxy_set_header X-Forwarded-Proto {protocol};
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 120s;
        proxy_buffering off;
        proxy_redirect off;
    }}

    location / {{
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host {domain};
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host {domain};
        proxy_set_header X-Forwarded-Port {port};
        proxy_set_header X-Forwarded-Proto {protocol};
        proxy_set_header Connection "";
        proxy_redirect off;
        proxy_connect_timeout 5s;
        proxy_send_timeout 180s;
        proxy_read_timeout 180s;
        # Retry a transient local service restart before exposing a 502/504
        # to the browser. Never retry an already-upgraded WebSocket location.
        proxy_next_upstream error timeout invalid_header http_500 http_502 http_503 http_504;
        proxy_next_upstream_tries 2;
        proxy_next_upstream_timeout 15s;
        proxy_request_buffering on;
        proxy_buffering on;
        proxy_buffers 16 16k;
        proxy_busy_buffers_size 32k;
    }}'''


def _tls_server(domain: str, static_root: str, fullchain: str, private_key: str) -> str:
    return f'''server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {domain};
    server_tokens off;
    client_max_body_size 82m;

    # An origin IP Host must never become a public redirect target.
    if ($host != {domain}) {{
        return 444;
    }}

    ssl_certificate     {fullchain};
    ssl_certificate_key {private_key};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:joker_web_ssl:10m;
    ssl_session_timeout 10m;
    absolute_redirect off;
    server_name_in_redirect off;
    port_in_redirect off;

    location ^~ /static/ {{
        alias {static_root}/;
        access_log off;
        expires 7d;
        add_header Cache-Control "public, max-age=604800, immutable" always;
        try_files $uri =404;
    }}

{_proxy_location(domain, "https", 443)}
}}'''


def render_config(
    template: Path,
    output: Path,
    domain: str,
    site_root: str,
    install_dir: str,
    fullchain: str = "",
    private_key: str = "",
) -> str:
    """Render a TLS vhost when a certificate pair is available, else ACME bootstrap."""
    domain = _safe_domain(domain)
    site_root = _safe_path(site_root, "site root")
    install_dir = _safe_path(install_dir, "web install directory")
    if bool(fullchain) != bool(private_key):
        raise ValueError("certificate chain and private key must be provided together")
    mode = "bootstrap"
    if fullchain:
        fullchain = _safe_path(fullchain, "certificate chain path")
        private_key = _safe_path(private_key, "private key path")
        mode = "tls"

    source = template.read_text(encoding="utf-8")
    missing = sorted(token for token in TOKENS if token not in source)
    if missing:
        raise ValueError("Nginx template is missing placeholders: " + ", ".join(missing))

    if mode == "tls":
        http_route = f'''    location / {{
        return 308 https://{domain}$request_uri;
    }}'''
        tls_server = _tls_server(
            domain,
            _safe_path(f"{install_dir}/web_client/static", "static files directory"),
            fullchain,
            private_key,
        )
    else:
        # Before a certificate exists, expose only the ACME location.  Serving
        # the portal over plaintext would undermine secure session cookies.
        http_route = '''    location / {
        return 404;
    }'''
        tls_server = "# TLS certificate not found yet; rerun joker-webctl --render-nginx after SSL is issued."

    rendered = source
    replacements = {
        "__JOKER_PORTAL_DOMAIN__": domain,
        "__JOKER_NGINX_SITE_ROOT__": site_root,
        "__JOKER_HTTP_ROUTE__": http_route,
        "__JOKER_TLS_SERVER__": tls_server,
    }
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    if any(token in rendered for token in TOKENS):
        raise ValueError("Nginx template rendering left unresolved placeholders")

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as destination:
            destination.write(rendered)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, output)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return mode


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a Joker Nginx reverse-proxy configuration")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--site-root", required=True)
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--cert-fullchain", default="")
    parser.add_argument("--cert-privkey", default="")
    arguments = parser.parse_args()
    mode = render_config(
        arguments.template,
        arguments.output,
        arguments.domain,
        arguments.site_root,
        arguments.install_dir,
        arguments.cert_fullchain,
        arguments.cert_privkey,
    )
    print(f"{mode}:{arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
