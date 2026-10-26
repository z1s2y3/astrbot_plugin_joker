#!/usr/bin/env bash
# Executed by a separate root-owned systemd unit, never by the web process.
set -Eeuo pipefail

CONFIG_FILE="/etc/joker-web-updater.conf"
[[ -r "$CONFIG_FILE" ]] || exit 2
# shellcheck disable=SC1090
source "$CONFIG_FILE"
# Keep the transport directory beside the deployment package.  The web
# process stages uploads here and the root worker consumes the same queue.
# Older installations may still override this in /etc/joker-web-updater.conf;
# the installer migrates that legacy value on the next manual upgrade.
if [[ -n "${JOKER_UPDATE_ROOT:-}" ]] \
  && ! [[ "$JOKER_UPDATE_ROOT" == "/www/wwwroot/joker-install" && -d /www1 && ! -L /www1 ]]; then
  UPDATE_ROOT="$JOKER_UPDATE_ROOT"
elif [[ -d /www1 && ! -L /www1 ]]; then
  UPDATE_ROOT="/www1/joker-install"
else
  UPDATE_ROOT="/www/wwwroot/joker-install"
fi
STATUS_FILE="$UPDATE_ROOT/last-status.json"
HISTORY_FILE="$UPDATE_ROOT/update-history.json"
UPDATE_ID="${1:-}"
UPDATE_WEB_USER="${JOKER_UPDATE_WEB_USER:-joker-web}"
KEY_FILE="/root/joker-web-update-key.txt"
STAGE_HELPER="/usr/local/libexec/joker-web-secure-stage"
WORK_DIR="$UPDATE_ROOT/processing/$UPDATE_ID"
ARCHIVE="$WORK_DIR/package.zip"
TEMP_DIR="$WORK_DIR/payload"
PACKAGE_LABEL="$UPDATE_ID.zip"
RELEASE_VERSION="未验证"
PACKAGE_PATH=""
PACKAGE_PUBLISHED=false
LOG_DIR="/var/log/joker-web-updates"
LOG_FILE="$LOG_DIR/$UPDATE_ID.log"
LAST_STATE="unknown"
LAST_MESSAGE=""

write_status() {
  local state="$1"
  local message="$2"
  LAST_STATE="$state"
  LAST_MESSAGE="$message"
  python3 - "$STATUS_FILE" "$state" "$message" "$PACKAGE_LABEL" "$RELEASE_VERSION" <<'PY'
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

target = Path(sys.argv[1])
content = json.dumps({
    "state": sys.argv[2],
    "message": sys.argv[3],
    "package": sys.argv[4],
    "release_version": sys.argv[5],
    "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S CST"),
}, ensure_ascii=False)
temporary = target.with_name(f".{target.name}.writing-{os.getpid()}")
temporary.write_text(content, encoding="utf-8")
temporary.chmod(0o644)
temporary.replace(target)
PY
}

append_update_history() {
  [[ -d "$UPDATE_ROOT" && ! -L "$UPDATE_ROOT" ]] || return 0
  [[ "$LAST_STATE" == "success" || "$LAST_STATE" == "failed" ]] || return 0
  python3 - "$HISTORY_FILE" "$UPDATE_ID" "$LAST_STATE" "$LAST_MESSAGE" "$PACKAGE_LABEL" "$RELEASE_VERSION" <<'PY'
import json
import os
import stat
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

target = Path(sys.argv[1])
try:
    current = target.lstat()
except FileNotFoundError:
    current = None
if current is not None and (stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)):
    raise SystemExit(0)
try:
    rows = json.loads(target.read_text(encoding="utf-8")) if current else []
except (OSError, UnicodeDecodeError, ValueError, TypeError):
    rows = []
if not isinstance(rows, list):
    rows = []
rows.insert(0, {
    "update_id": sys.argv[2],
    "state": sys.argv[3],
    "message": sys.argv[4][:300],
    "package": sys.argv[5][:160],
    "release_version": sys.argv[6][:64],
    "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S CST"),
})
rows = [row for row in rows if isinstance(row, dict)][:30]
temporary = target.with_name(f".{target.name}.writing-{os.getpid()}")
temporary.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
temporary.chmod(0o644)
temporary.replace(target)
PY
}

if [[ ! "$UPDATE_ID" =~ ^[a-f0-9]{32}$ ]]; then
  write_status "failed" "更新任务标识无效"
  exit 3
fi
if [[ ! -f "$STAGE_HELPER" || -L "$STAGE_HELPER" || ! -O "$STAGE_HELPER" || ! -x "$STAGE_HELPER" ]]; then
  write_status "failed" "服务器缺少安全更新组件，请先手动升级一次"
  exit 4
fi

cleanup() {
  # The identifier is checked above and processing is root-owned.  The ZIP is
  # copied to the fixed joker-install directory only for the duration of this
  # update. The task identifier makes an interrupted same-version update unable
  # to block a later retry. Always remove this task's copy, while retaining the
  # separate update log and status file for diagnostics.
  if [[ "$PACKAGE_PUBLISHED" == true && -n "$PACKAGE_PATH" ]]; then
    rm -f -- "$PACKAGE_PATH" || true
  fi
  rm -rf -- "$WORK_DIR"
  # Keep interrupted worker runs visible in the deployment history too.  A
  # normal success/failure status is already written by write_status(); this
  # fallback covers service restarts, SIGTERM and unexpected shell exits.
  if [[ "$LAST_STATE" != "success" && "$LAST_STATE" != "failed" ]]; then
    LAST_STATE="failed"
    LAST_MESSAGE="更新任务异常中断，请查看对应的服务器更新日志"
  fi
  append_update_history || true
}
trap cleanup EXIT

is_safe_update_root() {
  local candidate="${1:-}"
  [[ "$candidate" == /* \
    && "$candidate" != "/" \
    && "$candidate" != *$'\n'* \
    && "$candidate" != *$'\r'* \
    && ! -L "$candidate" \
    && -d "$candidate" ]]
}

STAGE_MESSAGE=""
if ! STAGE_MESSAGE="$(python3 "$STAGE_HELPER" \
  --update-root "$UPDATE_ROOT" \
  --update-id "$UPDATE_ID" \
  --web-user "$UPDATE_WEB_USER" \
  --key-file "$KEY_FILE" 2>&1)"; then
  STAGE_MESSAGE="${STAGE_MESSAGE//$'\n'/ }"
  write_status "failed" "安全接收更新包失败：${STAGE_MESSAGE:0:220}"
  exit 5
fi

if [[ ! -f "$ARCHIVE" || ! -f "$TEMP_DIR/install.sh" ]]; then
  write_status "failed" "安全更新组件返回了无效的任务目录"
  exit 6
fi

# The secure staging helper verifies archive structure before extraction.  Run
# the package-owned manifest verifier over the root-owned extracted tree as a
# second, content-level gate before the installer can stop or replace the
# live release.  Do not use --zip here: the helper deliberately renames the
# untrusted upload to package.zip, while the signed manifest retains the
# original release filename.
MANIFEST_VERIFIER="$TEMP_DIR/01_plugin_source/astrbot_plugin_joker/deploy/linux/verify_update_manifest.py"
# Run one diagnostic pass before the release-version parsing below.  The bounded
# reason is exposed through last-status.json so the administrator can see a
# missing file, CRLF shell script, or hash mismatch without opening a root log.
MANIFEST_MESSAGE=""
if [[ ! -f "$MANIFEST_VERIFIER" ]]; then
  MANIFEST_MESSAGE="server update package is missing the manifest verifier"
  write_status "failed" "$MANIFEST_MESSAGE"
  exit 6
elif ! MANIFEST_MESSAGE="$(python3 "$MANIFEST_VERIFIER" --tree "$TEMP_DIR" 2>&1)"; then
  MANIFEST_MESSAGE="${MANIFEST_MESSAGE//$'\n'/ }"
  MANIFEST_MESSAGE="${MANIFEST_MESSAGE//$'\r'/ }"
  MANIFEST_MESSAGE="${MANIFEST_MESSAGE:0:220}"
  write_status "failed" "update package verification failed: ${MANIFEST_MESSAGE}"
  exit 6
fi

# The manifest has passed the root-owned content verifier, so its release
# version is now safe to show in the administrator status page. Do not parse
# this untrusted upload before the verification above.
if ! RELEASE_VERSION="$(python3 - "$TEMP_DIR/update-manifest.json" <<'PY'
import json
import re
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
version = str(payload.get("release_version", "")).strip()
pattern = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
if not pattern.fullmatch(version):
    raise SystemExit(1)
print(version)
PY
)"; then
  write_status "failed" "更新包版本记录无效，未执行安装"
  exit 6
fi

# The manifest verifier above has already checked that this is a safe basename
# matching the signed release. Use it both in the administrator status and as
# the preserved archive name in the fixed joker-install package directory.
if ! PACKAGE_LABEL="$(python3 - "$TEMP_DIR/update-manifest.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
name = payload.get("archive_filename")
if not isinstance(name, str) or not name or "/" in name or "\\" in name:
    raise SystemExit(1)
print(name)
PY
)"; then
  write_status "failed" "更新包文件名记录无效，未执行安装"
  exit 6
fi

mkdir -p "$LOG_DIR"
chmod 750 "$LOG_DIR"
write_status "installing" "正在验证并安装网页更新"

is_valid_install_dir() {
  local candidate="${1:-}"
  [[ "$candidate" == /* \
    && "$candidate" != "/" \
    && "$candidate" != *$'\n'* \
    && "$candidate" != *$'\r'* \
    && -d "$candidate" \
    && ! -L "$candidate" \
    && -f "$candidate/web_client/.env" ]]
}

# Older installations may have been created before JOKER_INSTALL_DIR was
# persisted in /etc/joker-web-updater.conf. The systemd unit is authoritative:
# prefer its live working directory so a stale setting cannot create a second
# joker-web directory after a failed update.
SAVED_INSTALL_DIR="${JOKER_INSTALL_DIR:-}"
SYSTEMD_INSTALL_DIR="$(systemctl show -p WorkingDirectory --value joker-web 2>/dev/null || true)"
SYSTEMD_INSTALL_DIR="${SYSTEMD_INSTALL_DIR%$'\r'}"
if is_valid_install_dir "$SYSTEMD_INSTALL_DIR"; then
  JOKER_INSTALL_DIR="$SYSTEMD_INSTALL_DIR"
elif is_valid_install_dir "$SAVED_INSTALL_DIR"; then
  JOKER_INSTALL_DIR="$SAVED_INSTALL_DIR"
elif is_valid_install_dir "/www/wwwroot/joker-web"; then
  JOKER_INSTALL_DIR="/www/wwwroot/joker-web"
else
  JOKER_INSTALL_DIR=""
fi
if [[ -z "$JOKER_INSTALL_DIR" ]]; then
  write_status "failed" "服务器未保存网页安装目录；未执行更新，避免创建错误的新目录"
  exit 7
fi
if ! is_valid_install_dir "$JOKER_INSTALL_DIR"; then
  write_status "failed" "服务器未保存有效网页安装目录；请手动运行部署包 install.sh 修复配置"
  exit 7
fi
export JOKER_INSTALL_DIR
# Keep the transport directory stable.  Never replace it with the extracted
# payload: it contains the incoming/processing queues used by the watcher.
# The verified ZIP is copied beside those queues for the duration of this run,
# then removed by cleanup() after install.sh exits.
if ! is_safe_update_root "$UPDATE_ROOT"; then
  write_status "failed" "服务器更新目录无效：必须使用现有的独立 joker-install 文件夹"
  exit 7
fi
PACKAGE_PATH="$UPDATE_ROOT/.joker-web-update-${UPDATE_ID}-${PACKAGE_LABEL}"
if [[ -e "$PACKAGE_PATH" || -L "$PACKAGE_PATH" ]]; then
  write_status "failed" "更新任务临时部署包路径冲突，请重新上传后重试：$PACKAGE_LABEL"
  exit 7
fi
if ! install -o root -g root -m 600 "$ARCHIVE" "$PACKAGE_PATH"; then
  write_status "failed" "无法把部署包写入 $UPDATE_ROOT"
  exit 7
fi
PACKAGE_PUBLISHED=true
# The updater inherits the domain recorded by the installer.  Never fall back
# to a built-in public domain: that would silently replace a customer's
# reverse-proxy host during a background update.
JOKER_PORTAL_DOMAIN="${JOKER_PORTAL_DOMAIN:-}"
JOKER_PORTAL_DOMAIN="${JOKER_PORTAL_DOMAIN,,}"
if [[ -z "$JOKER_PORTAL_DOMAIN" || ! "$JOKER_PORTAL_DOMAIN" =~ ^[A-Za-z0-9.-]+$ || "$JOKER_PORTAL_DOMAIN" == .* || "$JOKER_PORTAL_DOMAIN" == *. || "$JOKER_PORTAL_DOMAIN" == *..* || "$JOKER_PORTAL_DOMAIN" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  write_status "failed" "服务器未保存有效网页域名；请手动运行部署包 install.sh 并设置 JOKER_PORTAL_DOMAIN"
  exit 7
fi
export JOKER_PORTAL_DOMAIN
set +e
(
  # Run the verified installer from its extracted package root.  Its
  # SCRIPT_DIR therefore points at this task's payload, while JOKER_INSTALL_DIR
  # still points at the existing live joker-web project.  No second project
  # directory is created and the fixed update directory is never replaced.
  cd "$TEMP_DIR"
  bash ./install.sh --non-interactive
) >"$LOG_FILE" 2>&1
EXIT_CODE=$?
set -e
if [[ "$EXIT_CODE" -ne 0 ]]; then
  write_status "failed" "更新失败（代码 $EXIT_CODE），请在服务器查看 $LOG_FILE"
  exit "$EXIT_CODE"
fi
if ! rm -f -- "$PACKAGE_PATH"; then
  write_status "failed" "网页已完成更新，但无法删除本次部署包：$PACKAGE_LABEL"
  exit 8
fi
PACKAGE_PUBLISHED=false
PACKAGE_PATH=""
if ! rm -rf -- "$WORK_DIR"; then
  write_status "failed" "网页已完成更新，但无法清理本次临时任务目录"
  exit 8
fi
write_status "success" "网页更新完成，服务已重启；本次部署包已清理"
