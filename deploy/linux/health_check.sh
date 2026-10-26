#!/usr/bin/env bash
set -euo pipefail

PLUGIN_DIR="${1:-/www/wwwroot/joker-web}"
cd "$PLUGIN_DIR"

if [[ ! -f web_client/.env ]]; then
  echo "失败：web_client/.env 不存在"
  exit 1
fi
if grep -q 'replace_with_' web_client/.env; then
  echo "失败：web_client/.env 仍有未替换的占位值"
  exit 1
fi

.web-venv/bin/python -m web_client --help >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8765/health/live
echo
curl --fail --silent --show-error --max-time 8 http://127.0.0.1:8765/health/ready
echo

# A normal HTTP health check cannot prove that the deployed package still
# exposes the machine-to-machine WebSocket routes. Send a valid upgrade
# request and reject only a missing route; 400/403 are expected when the
# endpoint exists but its token or runtime switch is disabled.
for websocket_path in /api/sync/v1/ws /api/game/v1/ws; do
  websocket_status="$(curl --silent --show-error --output /dev/null --max-time 8 \
    --write-out '%{http_code}' \
    -H 'Connection: Upgrade' \
    -H 'Upgrade: websocket' \
    -H 'Sec-WebSocket-Version: 13' \
    -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
    "http://127.0.0.1:8765${websocket_path}" || true)"
  if [[ "$websocket_status" == "404" || "$websocket_status" == "000" ]]; then
    echo "WebSocket route missing: ${websocket_path} (HTTP ${websocket_status}). Update the web package and restart joker-web." >&2
    exit 1
  fi
  echo "WebSocket route ${websocket_path} is registered (HTTP ${websocket_status})."
done

echo "Web service, database, health endpoints, and WebSocket routes passed"
echo "网页进程、数据库和 /health 检查正常。"
