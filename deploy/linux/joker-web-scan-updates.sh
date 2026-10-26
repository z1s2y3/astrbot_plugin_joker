#!/usr/bin/env bash
# Root-owned systemd watcher. It scans only explicit requests created by the
# portal, while the actual worker validates the separate root authorization.
set -Eeuo pipefail

CONFIG_FILE="/etc/joker-web-updater.conf"
[[ -r "$CONFIG_FILE" ]] || exit 0
# shellcheck disable=SC1090
source "$CONFIG_FILE"
# Must match web_client.self_update and the updater service configuration.
if [[ -n "${JOKER_UPDATE_ROOT:-}" ]] \
  && ! [[ "$JOKER_UPDATE_ROOT" == "/www/wwwroot/joker-install" && -d /www1 && ! -L /www1 ]]; then
  UPDATE_ROOT="$JOKER_UPDATE_ROOT"
elif [[ -d /www1 && ! -L /www1 ]]; then
  UPDATE_ROOT="/www1/joker-install"
else
  UPDATE_ROOT="/www/wwwroot/joker-install"
fi
INCOMING_DIR="$UPDATE_ROOT/incoming"
# A request file is published only after FastAPI sends its 202 response. Keep
# a second, root-side grace period before the installer can restart joker-web:
# reverse proxies and a remote browser then have time to finish that response.
RESPONSE_GRACE_SECONDS="${JOKER_UPDATE_RESPONSE_GRACE_SECONDS:-5}"
if ! [[ "$RESPONSE_GRACE_SECONDS" =~ ^[1-9][0-9]?$ ]] || (( 10#$RESPONSE_GRACE_SECONDS > 30 )); then
  RESPONSE_GRACE_SECONDS=5
fi

if [[ ! -d "$INCOMING_DIR" || -L "$INCOMING_DIR" ]]; then
  exit 0
fi
exec 9>"$UPDATE_ROOT/.scan.lock"
flock -n 9 || exit 0
shopt -s nullglob
for request in "$INCOMING_DIR"/*.request; do
  update_id="$(basename "$request" .request)"
  if [[ "$update_id" =~ ^[a-f0-9]{32}$ ]]; then
    # The worker performs an fd-safe, root-only handoff.  Do not inspect the
    # request, archive, or authorization file in this shell process.
    sleep "$RESPONSE_GRACE_SECONDS"
    /usr/local/sbin/joker-web-apply-update "$update_id" || true
  else
    rm -f -- "$request"
  fi
done
