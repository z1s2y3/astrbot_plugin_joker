#!/usr/bin/env bash
set -Eeuo pipefail

# The web portal deliberately lives outside AstrBot.  This server only stores
# web configuration; the Windows PC pulls it and runs the actual QQ bot.
TARGET_DIR="${JOKER_INSTALL_DIR:-/www/wwwroot/joker-web}"
LEGACY_TARGET_DIR="/www/wwwroot/AstrBot/data/plugins/astrbot_plugin_joker"
SERVICE_NAME="joker-web"
NON_INTERACTIVE=false
ACTION="install"
INSTALL_DIR="$TARGET_DIR"
USE_RELEASE_STAGE=false
RELEASE_STAGE_DIR=""
PREVIOUS_RELEASE_DIR=""
PREVIOUS_RELEASE_VERSION="unknown"
RELEASE_SWITCHED=false
FAILURE_RECOVERY_DONE=false
ERROR_REPORT_SHOWN=false
PACKAGE_VERSION="unknown"
# Keep a small, bounded rollback window for manual recovery.  Old release
# trees are never deleted blindly: only installer-created names are pruned.
RELEASE_BACKUPS_KEEP="${JOKER_RELEASE_BACKUPS_KEEP:-2}"
if ! [[ "$RELEASE_BACKUPS_KEEP" =~ ^[0-9]+$ ]] || (( 10#$RELEASE_BACKUPS_KEEP > 20 )); then
  RELEASE_BACKUPS_KEEP=2
fi
# Keep host logs and installer leftovers bounded.  These limits only apply to
# files owned by the system logger or this deployment, never to the portal
# database, .env, or the live release directory.
JOKER_HOST_LOG_MAX_BYTES="${JOKER_HOST_LOG_MAX_BYTES:-1073741824}"
JOKER_HOST_CACHE_DAYS="${JOKER_HOST_CACHE_DAYS:-14}"
if ! [[ "$JOKER_HOST_LOG_MAX_BYTES" =~ ^[0-9]+$ ]]; then
  JOKER_HOST_LOG_MAX_BYTES=1073741824
fi
if ! [[ "$JOKER_HOST_CACHE_DAYS" =~ ^[0-9]+$ ]]; then
  JOKER_HOST_CACHE_DAYS=14
fi

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --non-interactive) NON_INTERACTIVE=true ;;
    --check) ACTION="check" ;;
    --show-sync) ACTION="show-sync" ;;
    --show-version) ACTION="show-version" ;;
    --render-nginx) ACTION="render-nginx" ;;
    --apply-nginx) ACTION="apply-nginx" ;;
    --help|-h)
      cat <<'EOF'
用法：bash install.sh [--non-interactive] [--check] [--show-sync] [--show-version] [--render-nginx]

默认：安装或升级网页服务。已有配置自动升级并保留数据。
--non-interactive：首次安装时从 JOKER_* 环境变量读取参数。
--check：检查已安装网页服务、数据库与 /health。
--show-sync：显示需要填入 Windows AstrBot 的同步参数。
  --show-version：显示当前已成功启用的网页发布版本，不读取或显示任何密钥。
  joker-webctl --update <ZIP>：在宝塔终端校验并安装指定 Linux 网页部署包。
  --render-nginx：按 JOKER_PORTAL_DOMAIN 和宝塔现有证书路径重新生成 Nginx 配置。
EOF
      echo "--apply-nginx: back up, validate, and apply the rendered Baota vhost configuration"
      exit 0
      ;;
    *)
      echo "错误：未知参数 $1" >&2
      exit 2
      ;;
  esac
  shift
done

cleanup_release_stage() {
  if [[ -n "$RELEASE_STAGE_DIR" && -d "$RELEASE_STAGE_DIR" && "$RELEASE_SWITCHED" != true ]]; then
    rm -rf -- "$RELEASE_STAGE_DIR"
  fi
}

recover_failed_release() {
  # ERR does not run for an explicit `exit`, and an update worker can also be
  # terminated while a package is being prepared. Keep one idempotent recovery
  # path for both cases so a failed run cannot leave a staging directory or a
  # stopped service behind.
  [[ "$FAILURE_RECOVERY_DONE" == true ]] && return 0
  FAILURE_RECOVERY_DONE=true
  trap - ERR
  rollback_release || true
  cleanup_release_stage || true
  trap on_error ERR
}

rollback_release() {
  # A staged upgrade keeps the old directory intact until the replacement has
  # passed the normal service health check.  This is deliberately limited to
  # directories created by this installer under TARGET_DIR's parent.
  [[ "$RELEASE_SWITCHED" == true ]] || return 0

  local target_parent failed_release failed_version
  target_parent="$(dirname -- "$TARGET_DIR")"
  failed_version="$(release_version_label "${PACKAGE_VERSION:-unknown}")"
  failed_release="$target_parent/.joker-web-failed-v${failed_version}-$(date +%Y%m%d-%H%M%S)-$$"
  echo "更新未通过健康检查，正在恢复上一版网页文件……" >&2
  systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  if [[ -d "$TARGET_DIR" && ! -L "$TARGET_DIR" ]]; then
    mv -- "$TARGET_DIR" "$failed_release" || {
      echo "错误：无法保留失败的新版本目录：$TARGET_DIR" >&2
      return 1
    }
  fi
  if [[ -n "$PREVIOUS_RELEASE_DIR" && -d "$PREVIOUS_RELEASE_DIR" ]]; then
    mv -- "$PREVIOUS_RELEASE_DIR" "$TARGET_DIR" || {
      echo "错误：无法恢复上一版目录：$PREVIOUS_RELEASE_DIR" >&2
      return 1
    }
    PREVIOUS_RELEASE_DIR=""
  fi
  RELEASE_SWITCHED=false
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl start "$SERVICE_NAME" >/dev/null 2>&1 || true
  echo "已恢复上一版；失败的新版本保留在：$failed_release" >&2
}

on_error() {
  local exit_code=$?
  recover_failed_release
  echo
  echo "安装没有完成（第 ${BASH_LINENO[0]} 行，退出码 ${exit_code}）。"
  ERROR_REPORT_SHOWN=true
  if [[ -f "/etc/systemd/system/$SERVICE_NAME.service" ]]; then
    echo "---------------- 服务状态 ----------------"
    systemctl status "$SERVICE_NAME" --no-pager --full 2>/dev/null || true
    echo "---------------- 最近日志 ----------------"
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager 2>/dev/null || true
  fi
  echo "请把这一段错误和它上面的 20 行内容发给管理员。"
  exit "$exit_code"
}

on_exit() {
  local exit_code=$?
  if (( exit_code != 0 )); then
    recover_failed_release
    if [[ "$ERROR_REPORT_SHOWN" != true ]]; then
      echo
      echo "安装没有完成（退出码 ${exit_code}）；已清理临时发布目录并尝试恢复网页服务。" >&2
      if [[ -f "/etc/systemd/system/$SERVICE_NAME.service" ]]; then
        systemctl status "$SERVICE_NAME" --no-pager --full 2>/dev/null || true
        journalctl -u "$SERVICE_NAME" -n 30 --no-pager 2>/dev/null || true
      fi
    fi
    trap - EXIT
    exit "$exit_code"
  fi
}

on_signal() {
  local signal_code="$1"
  echo "收到中断信号，正在清理更新临时目录并恢复网页服务……" >&2
  exit "$signal_code"
}

trap on_error ERR
trap on_exit EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

if [[ "${EUID}" -ne 0 ]]; then
  echo "错误：请先切换 root 用户，再执行 bash install.sh"
  exit 1
fi

default_update_root() {
  # Prefer the separately mounted data volume when Baota exposes /www1.
  # Keep the old path as a fallback for existing single-volume hosts.
  if [[ -d /www1 && ! -L /www1 ]]; then
    printf '%s' "/www1/joker-install"
  else
    printf '%s' "/www/wwwroot/joker-install"
  fi
}

migrate_update_root() {
  local destination="$1"
  local legacy_root="/www/wwwroot/joker-install"
  local subdir source_item item_name target_item
  [[ "$destination" != "$legacy_root" ]] || return 0
  [[ -d "$legacy_root" && ! -L "$legacy_root" ]] || return 0
  mkdir -p -- "$destination"
  # Move only updater-owned state. Never move the live portal or unrelated
  # files from the old Baota directory.
  for subdir in incoming processing; do
    [[ -d "$legacy_root/$subdir" && ! -L "$legacy_root/$subdir" ]] || continue
    mkdir -p -- "$destination/$subdir"
    while IFS= read -r -d '' source_item; do
      item_name="$(basename -- "$source_item")"
      target_item="$destination/$subdir/$item_name"
      [[ -e "$target_item" || -L "$target_item" ]] && continue
      mv -- "$source_item" "$target_item" || true
    done < <(find "$legacy_root/$subdir" -mindepth 1 -maxdepth 1 -print0 2>/dev/null || true)
  done
  for source_item in "$legacy_root/last-status.json" "$legacy_root/update-history.json"; do
    [[ -e "$source_item" && ! -L "$source_item" ]] || continue
    target_item="$destination/$(basename -- "$source_item")"
    [[ -e "$target_item" || -L "$target_item" ]] || mv -- "$source_item" "$target_item" || true
  done
}

cleanup_host_caches() {
  # A full root filesystem can prevent the panel and the web service from
  # starting.  Keep this cleanup conservative: only rotated system logs,
  # oversized active syslog files, old Joker update logs, and stale update
  # staging directories are touched.  Application data and rollback releases
  # remain protected by the normal retention functions below.
  local log_file log_size update_root
  if command -v journalctl >/dev/null 2>&1; then
    journalctl --vacuum-size=200M >/dev/null 2>&1 || true
  fi
  for log_file in /var/log/syslog /var/log/messages /var/log/daemon.log; do
    [[ -f "$log_file" ]] || continue
    log_size="$(stat -c '%s' -- "$log_file" 2>/dev/null || printf '0')"
    if [[ "$log_size" =~ ^[0-9]+$ ]] && (( log_size > JOKER_HOST_LOG_MAX_BYTES )); then
      echo "清理过大的系统日志：$log_file（${log_size} 字节）"
      truncate -s 0 -- "$log_file" || echo "警告：无法截断系统日志：$log_file" >&2
    fi
  done
  for log_file in /var/log/syslog.1 /var/log/messages.1 /var/log/daemon.log.1; do
    [[ -f "$log_file" ]] || continue
    log_size="$(stat -c '%s' -- "$log_file" 2>/dev/null || printf '0')"
    if [[ "$log_size" =~ ^[0-9]+$ ]] && (( log_size > 104857600 )); then
      echo "删除过大的轮转日志：$log_file（${log_size} 字节）"
      rm -f -- "$log_file" || echo "警告：无法删除轮转日志：$log_file" >&2
    fi
  done
  if [[ -d /var/log/joker-web-updates && ! -L /var/log/joker-web-updates ]]; then
    find /var/log/joker-web-updates -xdev -type f -mtime +"$JOKER_HOST_CACHE_DAYS" \
      -delete 2>/dev/null || true
  fi
  local legacy_update_root="/www/wwwroot/joker-install"
  local preferred_update_root
  preferred_update_root="$(default_update_root)"
  for update_root in "${JOKER_UPDATE_ROOT:-}" "$preferred_update_root" "$legacy_update_root"; do
    [[ -n "$update_root" ]] || continue
    [[ -d "$update_root" && ! -L "$update_root" ]] || continue
    if [[ -d "$update_root/processing" && ! -L "$update_root/processing" ]]; then
      find "$update_root/processing" -mindepth 1 -maxdepth 1 -type d \
        -mtime +2 -exec rm -rf -- {} + 2>/dev/null || true
    fi
    if [[ -d "$update_root/incoming" && ! -L "$update_root/incoming" ]]; then
      find "$update_root/incoming" -maxdepth 1 -type f \
        \( -name '*.uploading' -o -name '*.requesting' -o -name '*.zip' -o -name '*.auth' \) \
        -mtime +2 -delete 2>/dev/null || true
    fi
  done
  find /tmp -xdev -mindepth 1 -maxdepth 1 -type d -name 'joker-web-*' \
    -mtime +2 -exec rm -rf -- {} + 2>/dev/null || true
}


# The path watcher and a manual terminal command must never replace the same
# release concurrently. The descriptor stays open for the whole installer.
if command -v flock >/dev/null 2>&1; then
  exec 8>/var/lock/joker-web-install.lock
  if ! flock -n 8; then
    echo "错误：已有 Joker 网页安装或更新任务正在运行，请稍后再试。" >&2
    exit 1
  fi
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR/01_plugin_source/astrbot_plugin_joker"

prompt_default() {
  local prompt="$1"
  local default_value="$2"
  local result
  read -r -p "$prompt [$default_value]: " result
  printf '%s' "${result:-$default_value}"
}

# This file is created by a previous successful installation.  Its values are
# written with ``printf %q`` below and are root-only, so plain extraction is
# enough here.  Do not source it: an installer must never execute a mutable
# configuration file merely to discover the previous public domain.
updater_setting() {
  local name="$1"
  local config_file="/etc/joker-web-updater.conf"
  local matched
  [[ -r "$config_file" ]] || return 0
  matched="$(grep -E "^${name}=" "$config_file" 2>/dev/null | tail -n 1 || true)"
  printf '%s' "${matched#*=}"
}

is_valid_release_version() {
  local candidate="${1:-}"
  [[ "$candidate" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-][0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?$ ]]
}

release_version_label() {
  local candidate="${1:-}"
  if is_valid_release_version "$candidate"; then
    printf '%s' "$candidate"
  else
    printf '%s' "unknown"
  fi
}

release_version_from_directory() {
  # Read only public version markers. A pre-marker installation remains
  # recoverable and is labelled unknown instead of blocking its backup.
  local directory="$1"
  local metadata_file completion_file discovered=""
  if [[ ! -d "$directory" || -L "$directory" ]]; then
    printf '%s' "unknown"
    return 0
  fi

  metadata_file="$directory/.joker-web-release.json"
  if [[ -r "$metadata_file" && ! -L "$metadata_file" ]]; then
    discovered="$(python3 - "$metadata_file" 2>/dev/null <<'PY'
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, ValueError):
    raise SystemExit(1)
version = payload.get("release_version") if isinstance(payload, dict) else None
if not isinstance(version, str) or not re.fullmatch(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
    version,
):
    raise SystemExit(1)
print(version)
PY
    )" || true
  fi
  if is_valid_release_version "$discovered"; then
    printf '%s' "$discovered"
    return 0
  fi

  completion_file="$directory/.joker-web-install-complete"
  if [[ -r "$completion_file" && ! -L "$completion_file" ]]; then
    discovered="$(
      grep -E '^release_version=' "$completion_file" 2>/dev/null \
        | tail -n 1 | cut -d= -f2- || true
    )"
  fi
  release_version_label "$discovered"
}

backup_release_basename() {
  local version
  version="$(release_version_label "${1:-}")"
  printf 'joker-web-v%s-%s' "$version" "${2:-unknown-time}"
}

is_rollback_release_dir() {
  local candidate="$1"
  local name
  name="$(basename -- "$candidate")"
  # Accept the timestamp-only layout from older packages for recovery.
  [[ "$name" =~ ^\.joker-web-rollback-[0-9]{8}-[0-9]{6}-[0-9]+$ ]] \
    || [[ "$name" =~ ^\.joker-web-rollback-v(unknown|[0-9]+\.[0-9]+\.[0-9]+[-+A-Za-z0-9.]*)-[0-9]{8}-[0-9]{6}-[0-9]+$ ]]
}

is_managed_backup_release_dir() {
  local candidate="$1"
  local parent name
  parent="$(dirname -- "$candidate")"
  name="$(basename -- "$candidate")"
  [[ "$parent" == "$(dirname -- "$TARGET_DIR")/.joker-web-backups" ]] \
    && [[ "$name" =~ ^joker-web-v(unknown|[0-9]+\.[0-9]+\.[0-9]+[-+A-Za-z0-9.]*)-[0-9]{8}-[0-9]{6}-[0-9]+([-.][0-9]+)?$ ]]
}

prune_release_backups() {
  # Keep only the newest installer-created release directories.  The NUL
  # pipeline handles spaces safely and the basename guard prevents a typo or
  # an operator-created directory from being removed.
  local target_parent backup_dir entry candidate candidate_name kept=0
  target_parent="$(dirname -- "$TARGET_DIR")"
  backup_dir="$target_parent/.joker-web-backups"
  [[ -d "$backup_dir" && ! -L "$backup_dir" ]] || return 0
  while IFS= read -r -d '' entry; do
    candidate="${entry#*:}"
    candidate_name="$(basename -- "$candidate")"
    if ! is_managed_backup_release_dir "$candidate"; then
      continue
    fi
    if (( kept < 10#$RELEASE_BACKUPS_KEEP )); then
      ((kept += 1))
      continue
    fi
    echo "清理旧网页发布备份：$candidate"
    rm -rf -- "$candidate" || echo "警告：无法清理旧发布备份：$candidate" >&2
  done < <(
    find "$backup_dir" -mindepth 1 -maxdepth 1 -type d -name 'joker-web-v*' \
      -printf '%T@:%p\0' 2>/dev/null | sort -z -rn || true
  )
}

prune_env_backups() {
  # Upgrade attempts also create root-owned .env snapshots.  Retain a few for
  # diagnostics, but prevent every background update from consuming /root.
  local entry candidate_name kept=0 keep_count=3
  while IFS= read -r -d '' entry; do
    candidate_name="$(basename -- "$entry")"
    if (( kept < keep_count )); then
      ((kept += 1))
      continue
    fi
    echo "清理旧网页配置备份：$entry"
    rm -f -- "$entry" || echo "警告：无法清理旧配置备份：$entry" >&2
  done < <(
    find /root -mindepth 1 -maxdepth 1 -type f \
      \( -name 'joker-web.env.backup.*' -o -name 'joker-web.env.failed.*' \) \
      -printf '%T@:%p\0' 2>/dev/null | sort -z -rn || true
  )
}

archive_orphaned_rollbacks() {
  # Once the new service is healthy, any sibling rollback directories are
  # stale artifacts from interrupted updates. Move them into the managed
  # backup directory before applying the retention policy.
  local target_parent backup_dir candidate version backup_name
  portal_health_is_ready || return 0
  target_parent="$(dirname -- "$TARGET_DIR")"
  [[ "$target_parent" != "/" && -d "$target_parent" && ! -L "$target_parent" ]] || return 0
  backup_dir="$target_parent/.joker-web-backups"
  if [[ -e "$backup_dir" && ( ! -d "$backup_dir" || -L "$backup_dir" ) ]]; then
    echo "警告：无法归档孤立回滚目录，备份目录不可用：$backup_dir" >&2
    return 0
  fi
  mkdir -p -- "$backup_dir"
  chmod 700 "$backup_dir"
  while IFS= read -r -d '' candidate; do
    is_rollback_release_dir "$candidate" || continue
    version="$(release_version_from_directory "$candidate")"
    backup_name="$backup_dir/$(backup_release_basename "$version" "$(date +%Y%m%d-%H%M%S)-$$")"
    [[ -e "$backup_name" || -L "$backup_name" ]] && backup_name="$backup_name-$$"
    if mv -- "$candidate" "$backup_name"; then
      echo "已归档中断更新留下的旧版本：$backup_name"
    else
      echo "警告：无法归档孤立回滚目录：$candidate" >&2
    fi
  done < <(
    find "$target_parent" -mindepth 1 -maxdepth 1 -type d \
      -name '.joker-web-rollback-*' -print0 2>/dev/null || true
  )
}

show_release_version() {
  local metadata_file fallback_file fallback_version
  metadata_file="$TARGET_DIR/.joker-web-release.json"
  if [[ -r "$metadata_file" && ! -L "$metadata_file" ]]; then
    if python3 - "$metadata_file" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    value = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, ValueError):
    raise SystemExit(1)
if not isinstance(value, dict):
    raise SystemExit(1)

version = value.get("release_version")
if not isinstance(version, str) or not re.fullmatch(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
    version,
):
    raise SystemExit(1)

archive = value.get("archive_filename")
installed_at = value.get("installed_at")
print(f"当前网页发布版本：{version}")
if isinstance(archive, str) and archive:
    print(f"部署包：{archive}")
if isinstance(installed_at, str) and installed_at:
    display_time = installed_at.strip()
    try:
        legacy_time = display_time.replace(" UTC", "+00:00")
        parsed_time = datetime.fromisoformat(legacy_time)
        if parsed_time.tzinfo is None:
            parsed_time = parsed_time.replace(tzinfo=timezone.utc)
        display_time = parsed_time.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S CST")
    except (TypeError, ValueError):
        pass
    print(f"安装完成时间（中国标准时间）：{display_time}")
PY
    then
      return 0
    fi
  fi

  # Older installations have no public release marker.  Their completion
  # marker is a best-effort compatibility fallback; the next successful
  # update writes the public marker above.
  fallback_file="$TARGET_DIR/.joker-web-install-complete"
  fallback_version="$(
    grep -E '^release_version=[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$' \
      "$fallback_file" 2>/dev/null | tail -n 1 | cut -d= -f2- || true
  )"
  if [[ -n "$fallback_version" ]]; then
    echo "当前网页发布版本：$fallback_version（来自旧安装完成标记）"
    echo "下一次成功更新后会写入后台可读取的版本记录。"
    return 0
  fi

  echo "尚未找到已成功启用的网页发布版本记录。" >&2
  return 1
}

write_release_metadata() {
  local metadata_file="$1"
  local manifest_file="$2"

  # The package manifest was verified before this function is called.  Keep
  # the record intentionally secret-free so the web process may report its
  # version without exposing the .env values that are preserved on upgrades.
  if [[ -L "$metadata_file" ]]; then
    echo "错误：发布版本元数据路径不能是符号链接：$metadata_file" >&2
    return 1
  fi
  python3 - "$metadata_file" "$manifest_file" <<'PY'
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

target = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
try:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
except (OSError, UnicodeDecodeError, ValueError) as error:
    raise SystemExit(f"cannot read verified update manifest: {error}")

if not isinstance(manifest, dict):
    raise SystemExit("verified update manifest is not an object")
version = manifest.get("release_version")
archive = manifest.get("archive_filename")
if not isinstance(version, str) or not re.fullmatch(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
    version,
):
    raise SystemExit("verified update manifest has an invalid release version")
if not isinstance(archive, str) or not archive or "/" in archive or "\\" in archive:
    raise SystemExit("verified update manifest has an invalid archive filename")
if len(archive) > 160:
    raise SystemExit("verified update manifest archive filename is too long")
if not target.parent.is_dir():
    raise SystemExit("release metadata parent directory does not exist")

try:
    existing = target.lstat()
except FileNotFoundError:
    pass
else:
    if stat.S_ISLNK(existing.st_mode):
        raise SystemExit("release metadata path must not be a symbolic link")
    if not stat.S_ISREG(existing.st_mode):
        raise SystemExit("release metadata path must be a regular file")

metadata = {
    "format_version": 1,
    "release_version": version,
    "archive_filename": archive,
    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    "installed_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S CST"),
}
payload = (json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
temporary = target.with_name(
    f".{target.name}.new-{os.getpid()}-{secrets.token_hex(8)}"
)
descriptor = None
try:
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o644,
    )
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short write while publishing release metadata")
        offset += written
    os.fchmod(descriptor, 0o644)
    os.fsync(descriptor)
    os.close(descriptor)
    descriptor = None
    os.replace(temporary, target)
    directory_descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
except Exception:
    if descriptor is not None:
        os.close(descriptor)
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    raise
PY
}

# ``joker-webctl`` is the installed copy of this script.  It must find a
# non-default portal directory without requiring callers to repeat an
# environment variable, while an explicit JOKER_INSTALL_DIR still wins.
if [[ -z "${JOKER_INSTALL_DIR:-}" ]]; then
  SAVED_INSTALL_DIR="$(updater_setting 'JOKER_INSTALL_DIR')"
  if [[ -n "$SAVED_INSTALL_DIR" ]]; then
    TARGET_DIR="$SAVED_INSTALL_DIR"
  fi
fi
INSTALL_DIR="$TARGET_DIR"

# Never unpack a release ZIP directly into the live joker-web directory. The
# installer needs the package tree as an independent source so an update can
# verify a clean payload and then replace the stable target atomically. This
# guard also prevents an accidental `01_plugin_source` directory from being
# left inside the running site.
SCRIPT_REAL="$(realpath -m -- "$SCRIPT_DIR")"
TARGET_REAL="$(realpath -m -- "$TARGET_DIR")"
if [[ "$SCRIPT_REAL" == "$TARGET_REAL" || "$SCRIPT_REAL" == "$TARGET_REAL/"* || "$TARGET_REAL" == "$SCRIPT_REAL/"* ]]; then
  echo "错误：部署包目录不能与运行目录重叠：" >&2
  echo "  部署包：$SCRIPT_REAL" >&2
  echo "  运行目录：$TARGET_REAL" >&2
  echo "请把 ZIP 解压到 /tmp/joker-web-update-XXXX 或独立的 joker-update 目录，再执行其中的 install.sh。" >&2
  exit 1
fi

cleanup_stale_release_stages() {
  # A worker killed by SIGKILL cannot run the shell traps above. On the next
  # locked installer run, remove only old directories made by this installer;
  # never use a broad wildcard or touch the live target itself.
  local target_parent candidate candidate_name
  target_parent="$(dirname -- "$TARGET_DIR")"
  [[ "$target_parent" != "/" && "$target_parent" != "." ]] || return 0
  [[ -d "$target_parent" && ! -L "$target_parent" ]] || return 0
  while IFS= read -r -d '' candidate; do
    candidate_name="$(basename -- "$candidate")"
    if [[ "$candidate_name" =~ ^\.joker-web-stage\.[A-Za-z0-9]+$ ]]; then
      echo "清理上次中断留下的临时发布目录：$candidate"
      rm -rf -- "$candidate" || echo "警告：无法清理临时目录：$candidate" >&2
    fi
  done < <(
    find "$target_parent" -mindepth 1 -maxdepth 1 -type d \
      -name '.joker-web-stage.????????' -mmin +10 -print0 2>/dev/null || true
  )
}

portal_health_is_ready() {
  command -v curl >/dev/null 2>&1 || return 1
  curl --max-time 4 --fail --silent \
    http://127.0.0.1:8765/health/ready >/dev/null 2>&1
}

recover_interrupted_release_switch() {
  # A power loss or SIGKILL can land between the two atomic renames in
  # activate_release_stage. The old release is then in a sibling rollback
  # directory while Joker either has no target directory or serves 502. Before
  # accepting another update, prefer the known previous release and bring it
  # back to the stable path. If the current release is already healthy, the
  # leftover rollback is only stale cleanup material.
  local target_parent candidate candidate_name rollback_dir="" rollback_count=0
  local stage_found=false failed_release backup_dir backup_name rollback_version failed_version
  target_parent="$(dirname -- "$TARGET_DIR")"
  [[ "$target_parent" != "/" && "$target_parent" != "." ]] || return 0
  [[ -d "$target_parent" && ! -L "$target_parent" ]] || return 0

  while IFS= read -r -d '' candidate; do
    candidate_name="$(basename -- "$candidate")"
    if is_rollback_release_dir "$candidate"; then
      rollback_dir="$candidate"
      ((rollback_count += 1))
    fi
  done < <(
    find "$target_parent" -mindepth 1 -maxdepth 1 -type d \
      -name '.joker-web-rollback-*' -print0 2>/dev/null || true
  )

  if (( rollback_count == 1 )); then
    if portal_health_is_ready; then
      backup_dir="$target_parent/.joker-web-backups"
      if [[ ! -e "$backup_dir" ]]; then
        mkdir -p -- "$backup_dir"
        chmod 700 "$backup_dir"
      fi
      if [[ -d "$backup_dir" && ! -L "$backup_dir" ]]; then
        rollback_version="$(release_version_from_directory "$rollback_dir")"
        backup_name="$backup_dir/$(backup_release_basename "$rollback_version" "$(date +%Y%m%d-%H%M%S)-$$")"
        if [[ -e "$backup_name" || -L "$backup_name" ]]; then
          backup_name="$backup_name-$$"
        fi
        mv -- "$rollback_dir" "$backup_name" || echo "警告：无法保留回滚备份：$rollback_dir" >&2
        if [[ ! -e "$rollback_dir" ]]; then
          echo "已保留上一版回滚备份：$backup_name"
        fi
      else
        echo "警告：回滚备份目录不可用，保留回滚目录：$rollback_dir" >&2
      fi
    elif [[ ! -e "$TARGET_DIR" || ( -d "$TARGET_DIR" && ! -L "$TARGET_DIR" ) ]]; then
      echo "检测到上次更新在切换时中断，正在恢复上一版网页文件……" >&2
      systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
      if [[ -d "$TARGET_DIR" && ! -L "$TARGET_DIR" ]]; then
        failed_version="$(release_version_from_directory "$TARGET_DIR")"
        failed_release="$target_parent/.joker-web-failed-v${failed_version}-$(date +%Y%m%d-%H%M%S)-recovery-$$"
        mv -- "$TARGET_DIR" "$failed_release" || {
          echo "警告：无法保留中断的新版本目录：$TARGET_DIR" >&2
          return 0
        }
        echo "中断的新版本已保留在：$failed_release" >&2
      fi
      if mv -- "$rollback_dir" "$TARGET_DIR"; then
        systemctl daemon-reload >/dev/null 2>&1 || true
        systemctl start "$SERVICE_NAME" >/dev/null 2>&1 || true
        echo "已恢复上一版，继续安装前会重新检查服务健康状态。" >&2
      else
        echo "警告：无法恢复上一版目录：$rollback_dir" >&2
      fi
    else
      echo "警告：运行目录不是可恢复的普通目录，保留回滚目录供人工检查：$rollback_dir" >&2
    fi
  elif (( rollback_count > 1 )); then
    echo "警告：发现多个未清理的网页回滚目录，未自动删除；请先检查：$target_parent/.joker-web-rollback-*" >&2
  fi

  # With the installer lock held, every stage directory that existed before
  # this invocation is orphaned. Remove it even when it is younger than the
  # periodic stale-cleanup threshold so a failed web update does not accumulate
  # folders in /www/wwwroot.
  while IFS= read -r -d '' candidate; do
    candidate_name="$(basename -- "$candidate")"
    if [[ "$candidate_name" =~ ^\.joker-web-stage\.[A-Za-z0-9]+$ ]]; then
      stage_found=true
      echo "清理上次中断留下的临时发布目录：$candidate"
      rm -rf -- "$candidate" || echo "警告：无法清理临时目录：$candidate" >&2
    fi
  done < <(
    find "$target_parent" -mindepth 1 -maxdepth 1 -type d \
      -name '.joker-web-stage.????????' -print0 2>/dev/null || true
  )

  if [[ "$stage_found" == true ]] && ! portal_health_is_ready; then
    # A pre-switch failure should never stop the old service, but older update
    # packages did so. Starting the stable service here repairs that case
    # before this new installer starts its own staged update.
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl start "$SERVICE_NAME" >/dev/null 2>&1 || true
  fi
}

if [[ "$ACTION" == "install" ]]; then
  cleanup_stale_release_stages
  recover_interrupted_release_switch
  prune_env_backups
  # Run after the installer lock is acquired so a watcher and a manual
  # upgrade cannot clean the same host logs concurrently.
  cleanup_host_caches
fi

has_valid_env() {
  local candidate="$1/web_client/.env"
  [[ -f "$candidate" ]] && ! grep -Eq 'replace_with_|替换为' "$candidate"
}

# A valid-looking .env alone does not prove that a first install finished.
# The configuration is written before MySQL is contacted, so a bad password
# used to make the next run look like an upgrade and retain the same bad
# password indefinitely. New installations write the completion marker only
# after the service and its health check both succeed. The systemd branch
# keeps installations created by older packages compatible.
has_completed_install() {
  local candidate="$1"
  local completion_file="$candidate/.joker-web-install-complete"
  local service_file="/etc/systemd/system/$SERVICE_NAME.service"

  has_valid_env "$candidate" || return 1
  if [[ -f "$completion_file" ]]; then
    return 0
  fi

  [[ -x "$candidate/.web-venv/bin/python" ]] \
    && [[ -f "$service_file" ]] \
    && grep -Fqx "WorkingDirectory=$candidate" "$service_file" \
    && grep -Fqx "ExecStart=$candidate/.web-venv/bin/python -m web_client" "$service_file"
}

service_working_directory() {
  # An earlier installation can still be healthy even when the updater
  # settings or completion marker were lost.  The live systemd unit is the
  # authoritative location in that case, and copying its valid .env avoids
  # silently generating a new Windows sync token during an upgrade.
  local candidate
  candidate="$(systemctl show -p WorkingDirectory --value "$SERVICE_NAME" 2>/dev/null || true)"
  candidate="${candidate%$'\r'}"
  if [[ "$candidate" == /* && "$candidate" != "/" && -d "$candidate" ]]; then
    printf '%s' "$candidate"
  fi
}

prepare_release_stage() {
  # Build an upgrade in a sibling staging directory while the current portal
  # remains live.  The staging directory is temporary; after the health check
  # succeeds the old tree is removed and the stable joker-web path is kept.
  local config_source="$1"
  local target_parent
  target_parent="$(dirname -- "$TARGET_DIR")"
  mkdir -p "$target_parent"
  RELEASE_STAGE_DIR="$(mktemp -d "$target_parent/.joker-web-stage.XXXXXXXX")"
  INSTALL_DIR="$RELEASE_STAGE_DIR"
  cp -a "$SOURCE_DIR/." "$INSTALL_DIR/"
  if [[ -f "$config_source/web_client/.env" ]]; then
    cp -a "$config_source/web_client/.env" "$INSTALL_DIR/web_client/.env"
  fi
}

activate_release_stage() {
  # Keep the old tree as a root-owned rollback directory only until the new
  # release passes its health check. `mv` is atomic because both paths are
  # direct children of the target's parent directory.
  local target_parent rollback_name previous_version
  target_parent="$(dirname -- "$TARGET_DIR")"

  if systemctl list-unit-files "$SERVICE_NAME.service" >/dev/null 2>&1; then
    systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  fi
  if curl --max-time 2 --fail --silent http://127.0.0.1:8765/health >/dev/null 2>&1; then
    echo "错误：8765 端口仍被手动启动的旧网页进程占用。" >&2
    if command -v fuser >/dev/null 2>&1; then
      echo "占用进程：" >&2
      fuser -v 8765/tcp || true
    fi
    return 1
  fi

  previous_version="$(release_version_from_directory "$TARGET_DIR")"
  PREVIOUS_RELEASE_VERSION="$previous_version"
  rollback_name="$target_parent/.joker-web-rollback-v${previous_version}-$(date +%Y%m%d-%H%M%S)-$$"
  if [[ -d "$TARGET_DIR" && ! -L "$TARGET_DIR" ]]; then
    mv -- "$TARGET_DIR" "$rollback_name"
    PREVIOUS_RELEASE_DIR="$rollback_name"
  fi
  if ! mv -- "$RELEASE_STAGE_DIR" "$TARGET_DIR"; then
    if [[ -n "$PREVIOUS_RELEASE_DIR" && -d "$PREVIOUS_RELEASE_DIR" ]]; then
      mv -- "$PREVIOUS_RELEASE_DIR" "$TARGET_DIR" || true
      PREVIOUS_RELEASE_DIR=""
    fi
    return 1
  fi
  RELEASE_STAGE_DIR=""
  INSTALL_DIR="$TARGET_DIR"
  RELEASE_SWITCHED=true
}

cleanup_successful_rollback() {
  # A successful update must not leave a second joker-web directory beside the
  # live one. The path was created above from a fixed prefix; validate both
  # the parent and basename before removing it.
  [[ "$RELEASE_SWITCHED" == true ]] || return 0
  if [[ -z "$PREVIOUS_RELEASE_DIR" || ! -d "$PREVIOUS_RELEASE_DIR" ]]; then
    archive_orphaned_rollbacks
    prune_release_backups
    prune_env_backups
    return 0
  fi
  local target_parent previous_parent backup_dir backup_name previous_version
  target_parent="$(dirname -- "$TARGET_DIR")"
  previous_parent="$(dirname -- "$PREVIOUS_RELEASE_DIR")"
  [[ "$previous_parent" == "$target_parent" ]] || return 1
  is_rollback_release_dir "$PREVIOUS_RELEASE_DIR" || return 1
  backup_dir="$target_parent/.joker-web-backups"
  if [[ -e "$backup_dir" && ( ! -d "$backup_dir" || -L "$backup_dir" ) ]]; then
    echo "警告：回滚备份目录不是普通目录，旧版本仍保留在：$PREVIOUS_RELEASE_DIR" >&2
    return 1
  fi
  mkdir -p -- "$backup_dir"
  chmod 700 "$backup_dir"
  previous_version="$(release_version_from_directory "$PREVIOUS_RELEASE_DIR")"
  if [[ "$previous_version" == "unknown" ]]; then
    previous_version="$(release_version_label "${PREVIOUS_RELEASE_VERSION:-unknown}")"
  fi
  backup_name="$backup_dir/$(backup_release_basename "$previous_version" "$(date +%Y%m%d-%H%M%S)-$$")"
  if [[ -e "$backup_name" || -L "$backup_name" ]]; then
    backup_name="$backup_name-$$"
  fi
  mv -- "$PREVIOUS_RELEASE_DIR" "$backup_name"
  echo "当前项目目录保持不变：$TARGET_DIR"
  echo "上一版已备份，可用于回滚：$backup_name"
  PREVIOUS_RELEASE_DIR=""
  archive_orphaned_rollbacks
  prune_release_backups
  prune_env_backups
}

collect_first_install_inputs() {
  local install_kind="$1"
  if [[ "$NON_INTERACTIVE" == true ]]; then
    MYSQL_HOST="${JOKER_MYSQL_HOST:-127.0.0.1}"
    MYSQL_PORT="${JOKER_MYSQL_PORT:-3306}"
    MYSQL_DATABASE="${JOKER_MYSQL_DATABASE:-joker_portal}"
    MYSQL_USER="${JOKER_MYSQL_USER:-joker_web}"
    MYSQL_PASSWORD="${JOKER_MYSQL_PASSWORD:-}"
    ADMIN_USER="${JOKER_ADMIN_USER:-admin}"
    ADMIN_PASSWORD="${JOKER_ADMIN_PASSWORD:-}"
    local missing=()
    [[ -n "$MYSQL_PASSWORD" ]] || missing+=("JOKER_MYSQL_PASSWORD")
    [[ -n "$ADMIN_PASSWORD" ]] || missing+=("JOKER_ADMIN_PASSWORD")
    if (( ${#missing[@]} > 0 )); then
      echo "错误：${install_kind}无交互安装缺少：${missing[*]}" >&2
      echo "请先在宝塔创建 MySQL 数据库和用户，再设置所需环境变量后重试。" >&2
      return 1
    fi
    echo "${install_kind}无交互安装：将使用提供的 MySQL 与管理员参数。"
    return 0
  fi

  echo "开始前请先在宝塔创建 MySQL 数据库。"
  MYSQL_HOST="$(prompt_default 'MySQL 地址' '127.0.0.1')"
  MYSQL_PORT="$(prompt_default 'MySQL 端口' '3306')"
  MYSQL_DATABASE="$(prompt_default '数据库名' 'joker_portal')"
  MYSQL_USER="$(prompt_default '数据库用户名' 'joker_web')"
  while true; do
    read -r -s -p "数据库密码（输入时不会显示）: " MYSQL_PASSWORD
    echo
    if [[ -n "$MYSQL_PASSWORD" ]]; then
      break
    fi
    echo "数据库密码不能为空，请重新输入。"
  done
  ADMIN_USER="$(prompt_default '网页管理员账号' 'admin')"
}

wait_for_package_manager() {
  # Ubuntu may finish unattended upgrades immediately after a new server is
  # created.  Waiting is safer than deleting dpkg lock files.
  if ! command -v fuser >/dev/null 2>&1; then
    return 0
  fi

  local attempt=0
  while fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if (( attempt > 60 )); then
      echo "系统正在进行软件更新，已等待 5 分钟。请等更新结束后重新执行 bash install.sh。"
      return 1
    fi
    if (( attempt == 1 )); then
      echo "检测到系统正在自动更新，正在等待（不会删除系统锁文件）……"
    fi
    sleep 5
  done
}

is_valid_domain() {
  local candidate="${1:-}"
  [[ "$candidate" =~ ^[A-Za-z0-9.-]+$ ]] \
    && [[ "$candidate" != .* ]] \
    && [[ "$candidate" != *. ]] \
    && [[ "$candidate" != *..* ]] \
    && [[ ! "$candidate" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]
}

detect_nginx_certificates() {
  local domain="$1"
  local saved_vhost_file=""
  local saved_fullchain=""
  local saved_private_key=""
  local saved_cert_dir=""
  if [[ "${DOMAIN_CHANGED:-false}" != true ]]; then
    saved_vhost_file="$(updater_setting 'JOKER_NGINX_VHOST_FILE')"
    saved_fullchain="$(updater_setting 'JOKER_NGINX_CERT_FULLCHAIN')"
    saved_private_key="$(updater_setting 'JOKER_NGINX_CERT_PRIVKEY')"
    saved_cert_dir="$(updater_setting 'JOKER_NGINX_CERT_DIR')"
  fi
  local vhost_file="${JOKER_NGINX_VHOST_FILE:-${saved_vhost_file:-/www/server/panel/vhost/nginx/$domain.conf}}"
  local fullchain="${JOKER_NGINX_CERT_FULLCHAIN:-$saved_fullchain}"
  local private_key="${JOKER_NGINX_CERT_PRIVKEY:-$saved_private_key}"
  local certificate_dir="${JOKER_NGINX_CERT_DIR:-${saved_cert_dir:-/www/server/panel/vhost/cert/$domain}}"

  NGINX_CERT_FULLCHAIN=""
  NGINX_CERT_PRIVKEY=""
  NGINX_VHOST_FILE="$vhost_file"

  if [[ -n "$fullchain" || -n "$private_key" ]]; then
    if [[ -z "$fullchain" || -z "$private_key" ]]; then
      echo "错误：JOKER_NGINX_CERT_FULLCHAIN 与 JOKER_NGINX_CERT_PRIVKEY 必须同时设置。" >&2
      return 1
    fi
  elif [[ -r "$vhost_file" ]]; then
    # Preserve a custom Baota certificate location if the selected site
    # already has one. Values are revalidated by the Python renderer.
    fullchain="$(awk '$1 == "ssl_certificate" { sub(/;$/, "", $2); print $2; exit }' "$vhost_file")"
    private_key="$(awk '$1 == "ssl_certificate_key" { sub(/;$/, "", $2); print $2; exit }' "$vhost_file")"
  fi

  if [[ -z "$fullchain" && -z "$private_key" ]]; then
    fullchain="$certificate_dir/fullchain.pem"
    private_key="$certificate_dir/privkey.pem"
  fi
  if [[ -f "$fullchain" && -f "$private_key" ]]; then
    NGINX_CERT_FULLCHAIN="$fullchain"
    NGINX_CERT_PRIVKEY="$private_key"
  fi
}

render_nginx_config() {
  local install_dir="$1"
  local domain="$2"
  local site_root="$3"
  local template_path="$install_dir/deploy/linux/nginx-web.conf.example"
  local output_path="$install_dir/deploy/linux/nginx-web.$domain.conf"

  if [[ ! -r "$template_path" || ! -r "$install_dir/deploy/linux/render_nginx_config.py" ]]; then
    echo "错误：找不到 Nginx 配置模板或渲染器：$install_dir/deploy/linux" >&2
    return 1
  fi
  detect_nginx_certificates "$domain"
  local renderer_args=(
    --template "$template_path"
    --output "$output_path"
    --domain "$domain"
    --site-root "$site_root"
    --install-dir "$install_dir"
  )
  if [[ -n "$NGINX_CERT_FULLCHAIN" ]]; then
    renderer_args+=(
      --cert-fullchain "$NGINX_CERT_FULLCHAIN"
      --cert-privkey "$NGINX_CERT_PRIVKEY"
    )
  fi
  python3 "$install_dir/deploy/linux/render_nginx_config.py" "${renderer_args[@]}" >/dev/null
  RENDERED_NGINX_CONFIG="$output_path"
  if [[ -n "$NGINX_CERT_FULLCHAIN" ]]; then
    RENDERED_NGINX_MODE="tls"
  else
    RENDERED_NGINX_MODE="bootstrap"
  fi
  RENDERED_NGINX_CERT_FULLCHAIN="$NGINX_CERT_FULLCHAIN"
}

# The generated configuration is intentionally kept outside Baota's live
# vhost directory until an administrator explicitly asks to apply it.  This
# helper makes that final step safe and repeatable: it only permits Baota's
# per-site vhost path, creates a root-only backup, validates the complete
# Nginx configuration, and restores the previous file if validation or reload
# fails.  It must never be called automatically by a package update.
is_baota_vhost_file() {
  local candidate="${1:-}"
  [[ "$candidate" =~ ^/www/server/panel/vhost/nginx/[A-Za-z0-9._-]+\.conf$ ]]
}

restore_nginx_vhost() {
  local target="$1"
  local backup="$2"
  local existed="$3"
  if [[ "$existed" == "true" ]]; then
    install -o root -g root -m 644 "$backup" "$target"
  else
    rm -f -- "$target"
  fi
  nginx -t >/dev/null 2>&1 || true
  systemctl reload nginx >/dev/null 2>&1 || true
}

apply_rendered_nginx_config() {
  local rendered="$1"
  local target="$2"
  local domain="$3"
  local target_dir target_name backup_dir backup temporary validation_output
  local target_existed="false"

  if ! is_baota_vhost_file "$target"; then
    echo "Refusing to overwrite an unsafe Baota vhost path: $target" >&2
    echo "Set JOKER_NGINX_VHOST_FILE to /www/server/panel/vhost/nginx/<domain>.conf, then retry." >&2
    return 1
  fi
  target_dir="$(dirname -- "$target")"
  target_name="$(basename -- "$target")"
  if [[ ! -d "$target_dir" || -L "$target_dir" ]]; then
    echo "Baota vhost directory is missing or unsafe: $target_dir" >&2
    return 1
  fi
  if [[ -e "$target" && ( ! -f "$target" || -L "$target" ) ]]; then
    echo "Baota vhost target is not a regular file: $target" >&2
    return 1
  fi

  backup_dir="/root/joker-web-nginx-backups"
  install -d -o root -g root -m 700 "$backup_dir"
  backup="$backup_dir/${target_name}.$(date +%Y%m%d-%H%M%S).bak"
  if [[ -e "$target" ]]; then
    cp -a -- "$target" "$backup"
    target_existed="true"
  fi

  temporary="$target_dir/.${target_name}.joker-new.$$"
  install -o root -g root -m 644 "$rendered" "$temporary"
  mv -f -- "$temporary" "$target"

  if ! validation_output="$(nginx -t 2>&1)"; then
    echo "$validation_output" >&2
    echo "Nginx validation failed; restoring the previous Baota vhost." >&2
    restore_nginx_vhost "$target" "$backup" "$target_existed"
    return 1
  fi
  if printf '%s\n' "$validation_output" | grep -F "conflicting server name \"$domain\"" >/dev/null; then
    echo "$validation_output" >&2
    echo "A second active vhost still declares $domain; keeping the previous configuration." >&2
    restore_nginx_vhost "$target" "$backup" "$target_existed"
    return 1
  fi
  if ! systemctl reload nginx; then
    echo "Nginx reload failed; restoring the previous Baota vhost." >&2
    restore_nginx_vhost "$target" "$backup" "$target_existed"
    return 1
  fi

  # Verify the active TLS vhost, not only the generated file. A conflicting
  # Baota site or stale include can otherwise leave both WebSocket paths at
  # HTTP 404 after a successful nginx reload.
  local websocket_path websocket_status
  for websocket_path in /api/sync/v1/ws /api/game/v1/ws; do
    websocket_status="$(curl --insecure --silent --show-error --output /dev/null \
      --max-time 10 --write-out '%{http_code}' \
      --resolve "${domain}:443:127.0.0.1" \
      -H 'Connection: Upgrade' \
      -H 'Upgrade: websocket' \
      -H 'Sec-WebSocket-Version: 13' \
      -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
      "https://${domain}${websocket_path}" || true)"
    if [[ "$websocket_status" == "404" || "$websocket_status" == "000" ]]; then
      echo "Active TLS vhost does not expose ${websocket_path} (HTTP ${websocket_status}); restoring the previous vhost." >&2
      restore_nginx_vhost "$target" "$backup" "$target_existed"
      return 1
    fi
  done

  echo "Applied Nginx vhost: $target"
  if [[ "$target_existed" == "true" ]]; then
    echo "Previous vhost backup: $backup"
  fi
}

if [[ "$ACTION" == "check" ]]; then
  exec "$TARGET_DIR/deploy/linux/health_check.sh" "$TARGET_DIR"
fi
if [[ "$ACTION" == "show-sync" ]]; then
  if [[ ! -r /root/joker-web-local-sync.txt ]]; then
    echo "尚未完成网页安装，找不到 /root/joker-web-local-sync.txt" >&2
    exit 1
  fi
  cat /root/joker-web-local-sync.txt
  exit 0
fi
if [[ "$ACTION" == "show-version" ]]; then
  show_release_version
  exit $?
fi
if [[ "$ACTION" == "render-nginx" || "$ACTION" == "apply-nginx" ]]; then
  SAVED_DOMAIN="$(updater_setting 'JOKER_PORTAL_DOMAIN')"
  DOMAIN="${JOKER_PORTAL_DOMAIN:-$SAVED_DOMAIN}"
  DOMAIN="${DOMAIN,,}"
  if ! is_valid_domain "$DOMAIN"; then
    echo "错误：请设置有效的 JOKER_PORTAL_DOMAIN 后再执行 joker-webctl --render-nginx。" >&2
    exit 1
  fi
  DOMAIN_CHANGED=false
  if [[ -n "$SAVED_DOMAIN" && "$DOMAIN" != "${SAVED_DOMAIN,,}" ]]; then
    DOMAIN_CHANGED=true
  fi
  NGINX_SITE_ROOT="${JOKER_NGINX_SITE_ROOT:-}"
  if [[ -z "$NGINX_SITE_ROOT" && "$DOMAIN_CHANGED" != true ]]; then
    NGINX_SITE_ROOT="$(updater_setting 'JOKER_NGINX_SITE_ROOT')"
  fi
  NGINX_SITE_ROOT="${NGINX_SITE_ROOT:-/www/wwwroot/$DOMAIN}"
  render_nginx_config "$TARGET_DIR" "$DOMAIN" "$NGINX_SITE_ROOT"
  if [[ "$ACTION" == "apply-nginx" ]]; then
    if [[ "$RENDERED_NGINX_MODE" != "tls" ]]; then
      echo "Refusing to replace an active site with an HTTP bootstrap vhost. Issue or restore the TLS certificate first." >&2
      exit 1
    fi
    apply_rendered_nginx_config "$RENDERED_NGINX_CONFIG" "$NGINX_VHOST_FILE" "$DOMAIN"

    # Probe the local vhost with the public Host and SNI. This distinguishes
    # an origin redirect from a CDN/EdgeOne redirect without relying on the
    # public network path.
    SOURCE_HEADERS="$(curl --insecure --silent --show-error --max-time 10 \
      --resolve "${DOMAIN}:443:127.0.0.1" -D - -o /dev/null \
      "https://${DOMAIN}/" 2>&1 || true)"
    SOURCE_LOCATION="$(printf '%s\n' "$SOURCE_HEADERS" | awk 'tolower($1) == "location:" { sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); print; exit }')"
    if [[ "$SOURCE_LOCATION" == http://* || "$SOURCE_LOCATION" == https://* ]]; then
      echo "Warning: the local origin still returned an absolute Location: $SOURCE_LOCATION" >&2
      echo "Inspect all active Baota vhosts before changing CDN settings." >&2
    elif [[ -n "$SOURCE_LOCATION" ]]; then
      echo "Local origin redirect verified: Location: $SOURCE_LOCATION"
      echo "If the public domain still redirects to an IP, remove the CDN/EdgeOne URL redirect rule and purge its redirect cache."
    else
      echo "The local origin did not return a redirect header; inspect the response with:"
      echo "curl -skD - -o /dev/null --max-redirs 0 --resolve ${DOMAIN}:443:127.0.0.1 https://${DOMAIN}/"
    fi
    exit 0
  fi
  echo "已生成 Nginx 配置：$RENDERED_NGINX_CONFIG"
  if [[ "$RENDERED_NGINX_MODE" == "tls" ]]; then
    echo "已使用现有宝塔证书：$RENDERED_NGINX_CERT_FULLCHAIN"
  else
    echo "尚未检测到证书，生成的是 HTTP/ACME 引导配置；签发 SSL 后重新执行 joker-webctl --render-nginx。"
  fi
  exit 0
fi

if [[ ! -f "$SOURCE_DIR/web_client/__main__.py" ]]; then
  echo "错误：安装包不完整。install.sh 必须和 01_plugin_source 文件夹放在同一目录。" >&2
  exit 1
fi
echo "============================================================"
echo " Joker 服务器网页配置中心一键安装"
echo "============================================================"
echo "直接按回车可使用方括号中的默认值。"
echo

SAVED_DOMAIN="$(updater_setting 'JOKER_PORTAL_DOMAIN')"
SAVED_NGINX_SITE_ROOT="$(updater_setting 'JOKER_NGINX_SITE_ROOT')"
SAVED_NGINX_CERT_DIR="$(updater_setting 'JOKER_NGINX_CERT_DIR')"
SAVED_NGINX_VHOST_FILE="$(updater_setting 'JOKER_NGINX_VHOST_FILE')"
if [[ "$NON_INTERACTIVE" == true ]]; then
  DOMAIN="${JOKER_PORTAL_DOMAIN:-$SAVED_DOMAIN}"
else
  if [[ -n "$SAVED_DOMAIN" ]]; then
    DOMAIN="$(prompt_default '网页域名' "$SAVED_DOMAIN")"
  else
    read -r -p "网页域名（例如 portal.example.com）: " DOMAIN
  fi
fi
DOMAIN="${DOMAIN,,}"
if ! is_valid_domain "$DOMAIN"; then
  echo "错误：必须提供 DNS 网页域名，不能使用服务器 IP、协议、端口或路径。" >&2
  exit 1
fi
IS_UPGRADE=false
CONFIG_SOURCE_DIR=""
IS_PARTIAL_INSTALL=false
PARTIAL_CONFIG_SOURCE_DIR=""
ACTIVE_SERVICE_DIR="$(service_working_directory)"
if has_completed_install "$TARGET_DIR"; then
  CONFIG_SOURCE_DIR="$TARGET_DIR"
  IS_UPGRADE=true
  echo "检测到已完成的网页安装：本次按升级处理，保留数据库密码和全部密钥。"
elif [[ "$TARGET_DIR" != "$LEGACY_TARGET_DIR" ]] && has_completed_install "$LEGACY_TARGET_DIR"; then
  CONFIG_SOURCE_DIR="$LEGACY_TARGET_DIR"
  IS_UPGRADE=true
  echo "检测到旧版网页目录，正在无损迁移到独立目录：$TARGET_DIR"
  echo "数据库、网页账号、群配置和密钥都会保留；旧目录暂不删除。"
elif has_valid_env "$TARGET_DIR"; then
  IS_PARTIAL_INSTALL=true
  PARTIAL_CONFIG_SOURCE_DIR="$TARGET_DIR"
  echo "检测到未完成的首次安装配置：不会按升级保留旧的 MySQL 密码。"
  echo "将先备份 .env，并重新收集 MySQL 参数；已有会话密钥、卡密指纹盐和同步令牌会保留。"
elif [[ "$TARGET_DIR" != "$LEGACY_TARGET_DIR" ]] && has_valid_env "$LEGACY_TARGET_DIR"; then
  IS_PARTIAL_INSTALL=true
  PARTIAL_CONFIG_SOURCE_DIR="$LEGACY_TARGET_DIR"
  echo "检测到旧目录中未完成的首次安装配置，正在安全恢复到：$TARGET_DIR"
elif [[ -n "$ACTIVE_SERVICE_DIR" && "$ACTIVE_SERVICE_DIR" != "$TARGET_DIR" ]] && has_valid_env "$ACTIVE_SERVICE_DIR"; then
  CONFIG_SOURCE_DIR="$ACTIVE_SERVICE_DIR"
  IS_UPGRADE=true
  echo "检测到正在运行的 Joker 网页目录：$ACTIVE_SERVICE_DIR"
  echo "本次将按升级处理并保留该目录的 .env、同步令牌和 Card Pepper。"
else
  echo "未检测到已完成安装，按首次安装处理。"
fi

if [[ "$IS_UPGRADE" == false ]]; then
  if [[ "$IS_PARTIAL_INSTALL" == true ]]; then
    collect_first_install_inputs "恢复未完成的"
  else
    collect_first_install_inputs "首次"
  fi
fi

DOMAIN_CHANGED=false
if [[ -n "$SAVED_DOMAIN" && "$DOMAIN" != "${SAVED_DOMAIN,,}" ]]; then
  DOMAIN_CHANGED=true
fi
if [[ "$DOMAIN_CHANGED" == true ]]; then
  NGINX_SITE_ROOT="${JOKER_NGINX_SITE_ROOT:-/www/wwwroot/$DOMAIN}"
  NGINX_CERT_DIR="${JOKER_NGINX_CERT_DIR:-/www/server/panel/vhost/cert/$DOMAIN}"
else
  NGINX_SITE_ROOT="${JOKER_NGINX_SITE_ROOT:-${SAVED_NGINX_SITE_ROOT:-/www/wwwroot/$DOMAIN}}"
  NGINX_CERT_DIR="${JOKER_NGINX_CERT_DIR:-${SAVED_NGINX_CERT_DIR:-/www/server/panel/vhost/cert/$DOMAIN}}"
fi
# These values are persisted before the first Nginx render below. Initialize
# them for `set -u`; detect_nginx_certificates will refine certificate paths
# and preserve an existing custom Baota vhost when the render runs later.
NGINX_VHOST_FILE="${JOKER_NGINX_VHOST_FILE:-${SAVED_NGINX_VHOST_FILE:-/www/server/panel/vhost/nginx/$DOMAIN.conf}}"
NGINX_CERT_FULLCHAIN="${NGINX_CERT_FULLCHAIN:-}"
NGINX_CERT_PRIVKEY="${NGINX_CERT_PRIVKEY:-}"
if [[ "$IS_UPGRADE" == false && ! "$MYSQL_PORT" =~ ^[0-9]+$ ]]; then
  echo "错误：MySQL 端口必须是数字。"
  exit 1
fi
if [[ "$IS_UPGRADE" == false && ! "$ADMIN_USER" =~ ^[A-Za-z0-9_]{4,32}$ ]]; then
  echo "错误：管理员账号只能使用 4～32 位字母、数字或下划线。"
  exit 1
fi

echo
echo "[1/8] 检查系统依赖……"
if ! command -v python3 >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1 || ! command -v unzip >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    wait_for_package_manager
    apt-get update
    apt-get install -y python3 python3-venv python3-pip curl unzip
  else
    echo "错误：未找到 python3/curl/unzip，也无法自动使用 apt 安装。"
    exit 1
  fi
fi

MANIFEST_PATH="$SCRIPT_DIR/update-manifest.json"
MANIFEST_VERIFIER="$SOURCE_DIR/deploy/linux/verify_update_manifest.py"
if [[ ! -f "$MANIFEST_PATH" || ! -f "$MANIFEST_VERIFIER" ]]; then
  echo "错误：部署包缺少 update-manifest.json 或清单校验器，请使用最新完整部署包。" >&2
  exit 1
fi
if ! python3 "$MANIFEST_VERIFIER" --tree "$SCRIPT_DIR" >/dev/null; then
  echo "错误：部署包清单或文件 SHA-256 校验失败，已停止安装。" >&2
  exit 1
fi
PACKAGE_VERSION="$(python3 - "$MANIFEST_PATH" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(data["release_version"])
PY
)"

echo "[2/8] 安装网页文件……"
if [[ "$IS_UPGRADE" == true ]]; then
  BACKUP_FILE="/root/joker-web.env.backup.$(date +%Y%m%d-%H%M%S)"
  cp -a "$CONFIG_SOURCE_DIR/web_client/.env" "$BACKUP_FILE"
  chmod 600 "$BACKUP_FILE"
  echo "已备份原配置：$BACKUP_FILE"
elif [[ "$IS_PARTIAL_INSTALL" == true ]]; then
  BACKUP_FILE="/root/joker-web.env.failed.$(date +%Y%m%d-%H%M%S)"
  cp -a "$PARTIAL_CONFIG_SOURCE_DIR/web_client/.env" "$BACKUP_FILE"
  chmod 600 "$BACKUP_FILE"
  echo "已备份未完成安装的配置：$BACKUP_FILE"
fi

# A completed deployment (or a recoverable partial deployment) is assembled
# outside the live directory.  The existing service stays up while Python
# packages are installed and MySQL is checked, and a later directory rename
# prevents stale files from surviving an update.
if [[ "$IS_UPGRADE" == true || "$IS_PARTIAL_INSTALL" == true ]]; then
  USE_RELEASE_STAGE=true
  if [[ "$IS_UPGRADE" == true ]]; then
    prepare_release_stage "$CONFIG_SOURCE_DIR"
  else
    prepare_release_stage "$PARTIAL_CONFIG_SOURCE_DIR"
  fi
else
  if systemctl list-unit-files "$SERVICE_NAME.service" >/dev/null 2>&1; then
    systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  fi
  if curl --max-time 2 --fail --silent http://127.0.0.1:8765/health >/dev/null 2>&1; then
    echo "错误：8765 端口仍被手动启动的旧网页进程占用。"
    if command -v fuser >/dev/null 2>&1; then
      echo "占用进程："
      fuser -v 8765/tcp || true
    fi
    echo "请先停止该手动网页进程后，再重新执行 bash install.sh。"
    exit 1
  fi
  mkdir -p "$(dirname -- "$TARGET_DIR")"
  mkdir -p "$TARGET_DIR"
  cp -a "$SOURCE_DIR/." "$TARGET_DIR/"
fi
# Keep a tiny command entry point after installation.  It supports only safe
# diagnostics/display actions; installation and upgrades still require the
# complete extracted package alongside 01_plugin_source.
if [[ "$IS_UPGRADE" == true && "$CONFIG_SOURCE_DIR" != "$TARGET_DIR" ]]; then
  echo "已将原网页配置复制到新的发布目录。"
elif [[ "$IS_PARTIAL_INSTALL" == true && "$PARTIAL_CONFIG_SOURCE_DIR" != "$TARGET_DIR" ]]; then
  echo "已将未完成安装的配置复制到新的发布目录，等待验证新的 MySQL 参数。"
fi
cd "$INSTALL_DIR"

ENV_FILE="$INSTALL_DIR/web_client/.env"

if [[ "$IS_UPGRADE" == false ]]; then
  export JOKER_SETUP_MYSQL_HOST="$MYSQL_HOST"
  export JOKER_SETUP_MYSQL_PORT="$MYSQL_PORT"
  export JOKER_SETUP_MYSQL_DATABASE="$MYSQL_DATABASE"
  export JOKER_SETUP_MYSQL_USER="$MYSQL_USER"
  export JOKER_SETUP_MYSQL_PASSWORD="$MYSQL_PASSWORD"
  export JOKER_SETUP_PUBLIC_ORIGIN="https://$DOMAIN"
  if [[ "$IS_PARTIAL_INSTALL" == true ]]; then
    echo "[3/8] 验证恢复用的 MySQL 配置……"
    echo "数据库连接成功前不会改写现有 .env 中的 MySQL 参数。"
  else
    echo "[3/8] 自动生成配置和安全密钥……"
    SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    CARD_PEPPER="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    SYNC_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    export JOKER_SETUP_SESSION_SECRET="$SESSION_SECRET"
    export JOKER_SETUP_CARD_PEPPER="$CARD_PEPPER"
    export JOKER_SETUP_SYNC_TOKEN="$SYNC_TOKEN"
    python3 - "$ENV_FILE" <<'PY'
import json
import os
import sys
from pathlib import Path


def quoted(name: str) -> str:
    return json.dumps(os.environ[name], ensure_ascii=False)


content = "\n".join(
    [
        f"JOKER_WEB_MYSQL_HOST={quoted('JOKER_SETUP_MYSQL_HOST')}",
        f"JOKER_WEB_MYSQL_PORT={quoted('JOKER_SETUP_MYSQL_PORT')}",
        f"JOKER_WEB_MYSQL_DATABASE={quoted('JOKER_SETUP_MYSQL_DATABASE')}",
        f"JOKER_WEB_MYSQL_USER={quoted('JOKER_SETUP_MYSQL_USER')}",
        f"JOKER_WEB_MYSQL_PASSWORD={quoted('JOKER_SETUP_MYSQL_PASSWORD')}",
        "",
        f"JOKER_WEB_SESSION_SECRET={quoted('JOKER_SETUP_SESSION_SECRET')}",
        f"JOKER_WEB_CARD_PEPPER={quoted('JOKER_SETUP_CARD_PEPPER')}",
        f"JOKER_WEB_SYNC_TOKEN={quoted('JOKER_SETUP_SYNC_TOKEN')}",
        "JOKER_WEB_DATA_MODE=remote",
        "JOKER_WEB_OFFLINE_AFTER_SECONDS=180",
        "",
        "SCUM_RELAY_URL=https://ba.yooyun.top",
        "SCUM_RELAY_KEY=",
        "",
        "JOKER_WEB_PORTAL_NAME=Joker 群管客户中心",
        f"JOKER_WEB_PUBLIC_ORIGIN={quoted('JOKER_SETUP_PUBLIC_ORIGIN')}",
        "JOKER_WEB_HOST=127.0.0.1",
        "JOKER_WEB_PORT=8765",
        "JOKER_WEB_SESSION_MAX_AGE=604800",
        "JOKER_WEB_SECURE_COOKIES=true",
        "",
    ]
)
target = Path(sys.argv[1])
temporary = target.with_name(f".{target.name}.joker-new-{os.getpid()}")
temporary.write_text(content, encoding="utf-8")
temporary.chmod(0o600)
temporary.replace(target)
PY
  fi
else
  echo "[3/8] 保留现有配置和安全密钥……"
fi
# Keep a non-interactive, deployment-owned public-origin fallback. It only
# fills legacy .env files that predate this setting; explicit operator values
# are never overwritten during an upgrade.
export JOKER_SETUP_PUBLIC_ORIGIN="https://$DOMAIN"
python3 - "$ENV_FILE" <<'PY'
import json
import os
import sys
from pathlib import Path


target = Path(sys.argv[1])
lines = target.read_text(encoding="utf-8").splitlines()
defaults = {
    "JOKER_WEB_PUBLIC_ORIGIN": json.dumps(
        os.environ["JOKER_SETUP_PUBLIC_ORIGIN"], ensure_ascii=False
    ),
    "SCUM_RELAY_URL": "https://ba.yooyun.top",
    "SCUM_RELAY_KEY": "",
}
updated = False
for key, value in defaults.items():
    if not any(line.partition("=")[0] == key for line in lines):
        lines.append(f"{key}={value}")
        updated = True
if updated:
    temporary = target.with_name(f".{target.name}.joker-origin-{os.getpid()}")
    temporary.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target)
PY
chmod 600 "$ENV_FILE"

echo "[4/8] 创建 Python 环境并安装依赖……"
chmod +x deploy/linux/install_web.sh deploy/linux/health_check.sh deploy/linux/joker-web-scan-updates.sh deploy/linux/joker-web-apply-update.sh deploy/linux/joker-web-manual-update.sh deploy/linux/joker-web-secure-stage.py deploy/linux/render_nginx_config.py
if ! ./deploy/linux/install_web.sh "$INSTALL_DIR" python3; then
  if command -v apt-get >/dev/null 2>&1; then
    wait_for_package_manager
    apt-get update
    apt-get install -y python3-venv python3-pip
    ./deploy/linux/install_web.sh "$INSTALL_DIR" python3
  else
    exit 1
  fi
fi

if [[ "$IS_UPGRADE" == false ]]; then
  # Settings.load_dotenv(..., override=False) lets these temporary values
  # validate the newly supplied credentials without first committing them to a
  # failed partial-install .env.  A custom URL must not mask the entered
  # MySQL fields during this recovery check.
  export JOKER_WEB_MYSQL_HOST="$JOKER_SETUP_MYSQL_HOST"
  export JOKER_WEB_MYSQL_PORT="$JOKER_SETUP_MYSQL_PORT"
  export JOKER_WEB_MYSQL_DATABASE="$JOKER_SETUP_MYSQL_DATABASE"
  export JOKER_WEB_MYSQL_USER="$JOKER_SETUP_MYSQL_USER"
  export JOKER_WEB_MYSQL_PASSWORD="$JOKER_SETUP_MYSQL_PASSWORD"
  export JOKER_WEB_DATABASE_URL=""
fi

echo "[5/8] 验证 MySQL 并初始化数据表……"
if ! .web-venv/bin/python - <<'PY'
import sys

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from web_client.database import init_database


try:
    init_database()
except OperationalError as error:
    message = str(getattr(error, "orig", error)).lower()
    if "1045" in message or "access denied" in message:
        print(
            "MySQL 拒绝登录（错误 1045）：请在宝塔“数据库”中核对该用户的密码、"
            "主机匹配项（localhost/127.0.0.1）以及目标数据库权限。",
            file=sys.stderr,
        )
    else:
        print("无法连接 MySQL：请检查 MySQL 服务、地址、端口和数据库名称。", file=sys.stderr)
    raise SystemExit(1)
except SQLAlchemyError:
    print(
        "MySQL 已连接但无法初始化数据表：请授予该用户目标数据库的创建、修改和索引权限。",
        file=sys.stderr,
    )
    raise SystemExit(1)
except Exception:
    print("数据库配置读取或初始化失败；请检查 .env 与安装日志。", file=sys.stderr)
    raise SystemExit(1)
else:
    print("数据库连接与数据表初始化成功")
PY
then
  echo "数据库参数尚未写入网页配置；修正后直接重新执行 bash install.sh 即可。" >&2
  exit 1
fi

if [[ "$IS_PARTIAL_INSTALL" == true ]]; then
  python3 - "$ENV_FILE" <<'PY'
import json
import os
import sys
from pathlib import Path


target = Path(sys.argv[1])
updates = {
    "JOKER_WEB_MYSQL_HOST": os.environ["JOKER_SETUP_MYSQL_HOST"],
    "JOKER_WEB_MYSQL_PORT": os.environ["JOKER_SETUP_MYSQL_PORT"],
    "JOKER_WEB_MYSQL_DATABASE": os.environ["JOKER_SETUP_MYSQL_DATABASE"],
    "JOKER_WEB_MYSQL_USER": os.environ["JOKER_SETUP_MYSQL_USER"],
    "JOKER_WEB_MYSQL_PASSWORD": os.environ["JOKER_SETUP_MYSQL_PASSWORD"],
}
lines = target.read_text(encoding="utf-8").splitlines()
seen: set[str] = set()
rewritten: list[str] = []
for line in lines:
    key, separator, _ = line.partition("=")
    if separator and key in updates:
        rewritten.append(f"{key}={json.dumps(updates[key], ensure_ascii=False)}")
        seen.add(key)
    elif separator and key == "JOKER_WEB_DATABASE_URL":
        # A URL takes precedence over the five MySQL fields. Removing it is
        # intentional here: the operator has explicitly entered replacement
        # connection parameters, and the original remains in /root backup.
        continue
    else:
        rewritten.append(line)
for key, value in updates.items():
    if key not in seen:
        rewritten.append(f"{key}={json.dumps(value, ensure_ascii=False)}")

content = "\n".join(rewritten).rstrip("\n") + "\n"
temporary = target.with_name(f".{target.name}.joker-repair-{os.getpid()}")
temporary.write_text(content, encoding="utf-8")
temporary.chmod(0o600)
temporary.replace(target)
PY
  echo "新的 MySQL 参数已通过验证并安全写入 .env；原配置仍保存在：$BACKUP_FILE"
fi

if [[ "$IS_UPGRADE" == false ]]; then
  unset JOKER_WEB_MYSQL_HOST JOKER_WEB_MYSQL_PORT JOKER_WEB_MYSQL_DATABASE
  unset JOKER_WEB_MYSQL_USER JOKER_WEB_MYSQL_PASSWORD JOKER_WEB_DATABASE_URL
  unset JOKER_SETUP_MYSQL_HOST
  unset JOKER_SETUP_MYSQL_PORT JOKER_SETUP_MYSQL_DATABASE JOKER_SETUP_MYSQL_USER
  unset JOKER_SETUP_MYSQL_PASSWORD JOKER_SETUP_SESSION_SECRET JOKER_SETUP_CARD_PEPPER
  unset JOKER_SETUP_SYNC_TOKEN JOKER_SETUP_PUBLIC_ORIGIN MYSQL_PASSWORD SESSION_SECRET CARD_PEPPER SYNC_TOKEN
fi

echo "[6/8] 创建网页管理员……"
ADMIN_COUNT="$(.web-venv/bin/python -c \
  "from sqlalchemy import func, select; from web_client.database import get_session_factory; from web_client.models import User; s=get_session_factory()(); print(s.scalar(select(func.count()).select_from(User).where(User.role == 'admin')) or 0); s.close()")"
if [[ "$ADMIN_COUNT" == "0" ]]; then
  if [[ "$NON_INTERACTIVE" == true ]]; then
    ADMIN_USER="${ADMIN_USER:-${JOKER_ADMIN_USER:-admin}}"
    ADMIN_PASSWORD="${ADMIN_PASSWORD:-${JOKER_ADMIN_PASSWORD:-}}"
    if [[ -z "$ADMIN_PASSWORD" ]]; then
      echo "错误：当前没有网页管理员，需设置 JOKER_ADMIN_PASSWORD 后重试。" >&2
      exit 1
    fi
  elif [[ -z "${ADMIN_USER:-}" ]]; then
    ADMIN_USER="$(prompt_default '网页管理员账号' 'admin')"
  fi
  if [[ ! "$ADMIN_USER" =~ ^[A-Za-z0-9_]{4,32}$ ]]; then
    echo "错误：管理员账号只能使用 4～32 位字母、数字或下划线。"
    exit 1
  fi
  if [[ "$NON_INTERACTIVE" == true ]]; then
    export JOKER_WEB_BOOTSTRAP_ADMIN_PASSWORD="$ADMIN_PASSWORD"
    .web-venv/bin/python -m web_client create-admin "$ADMIN_USER" --password-env
    unset JOKER_WEB_BOOTSTRAP_ADMIN_PASSWORD ADMIN_PASSWORD
  else
    echo "接下来请输入两次网页管理员密码（输入时不会显示）："
    .web-venv/bin/python -m web_client create-admin "$ADMIN_USER"
  fi
  ADMIN_STATUS="已创建：$ADMIN_USER"
else
  echo "检测到已有管理员，保留现有账号和密码。"
  ADMIN_STATUS="已保留现有管理员"
fi

echo "[7/8] 设置开机自启并启动网页……"
if ! getent passwd joker-web >/dev/null 2>&1; then
  useradd --system --home-dir "$TARGET_DIR" --shell /usr/sbin/nologin joker-web
fi
RUN_USER="joker-web"
RUN_GROUP="$(id -gn "$RUN_USER")"
chmod -R a+rX "$INSTALL_DIR"
chown root:"$RUN_GROUP" "$ENV_FILE"
chmod 640 "$ENV_FILE"

SAVED_UPDATE_ROOT="$(updater_setting 'JOKER_UPDATE_ROOT')"
if [[ -n "${JOKER_UPDATE_ROOT:-}" ]]; then
  UPDATE_ROOT="$JOKER_UPDATE_ROOT"
elif [[ -n "$SAVED_UPDATE_ROOT" && "$SAVED_UPDATE_ROOT" != "/var/lib/joker-web-updates" ]] \
  && ! [[ "$SAVED_UPDATE_ROOT" == "/www/wwwroot/joker-install" && -d /www1 && ! -L /www1 ]]; then
  UPDATE_ROOT="$SAVED_UPDATE_ROOT"
else
  # Keep administrator uploads on the separate /www1 volume when available.
  UPDATE_ROOT="$(default_update_root)"
fi
if [[ "$UPDATE_ROOT" != /* || "$UPDATE_ROOT" == "/" || "$UPDATE_ROOT" == *$'\n'* || "$UPDATE_ROOT" == *$'\r'* || -L "$UPDATE_ROOT" ]]; then
  echo "JOKER_UPDATE_ROOT must be a safe absolute directory path: $UPDATE_ROOT" >&2
  exit 1
fi
if [[ "$UPDATE_ROOT" == "$TARGET_DIR" || "$UPDATE_ROOT" == "$TARGET_DIR/"* || "$TARGET_DIR" == "$UPDATE_ROOT/"* ]]; then
  echo "Update directory must not overlap the live joker-web directory: $UPDATE_ROOT" >&2
  exit 1
fi
migrate_update_root "$UPDATE_ROOT"

cat >"/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Joker remote customer portal
After=network-online.target mysql.service mysqld.service
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$TARGET_DIR
ExecStart=$TARGET_DIR/.web-venv/bin/python -m web_client
Restart=always
RestartSec=2
TimeoutStartSec=90
TimeoutStopSec=15
Environment=PYTHONUNBUFFERED=1
# Nginx terminates HTTPS; keep Uvicorn reachable only from the local proxy.
Environment=JOKER_WEB_HOST=127.0.0.1
Environment=JOKER_WEB_PORT=8765
Environment=JOKER_WEB_UPDATE_STAGING_DIR=$UPDATE_ROOT
UMask=0027
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

echo "[8/8] 配置网页后台更新服务……"
SAVED_UPDATE_ROOT="$(updater_setting 'JOKER_UPDATE_ROOT')"
if [[ -n "${JOKER_UPDATE_ROOT:-}" ]]; then
  UPDATE_ROOT="$JOKER_UPDATE_ROOT"
elif [[ -n "$SAVED_UPDATE_ROOT" && "$SAVED_UPDATE_ROOT" != "/var/lib/joker-web-updates" ]] \
  && ! [[ "$SAVED_UPDATE_ROOT" == "/www/wwwroot/joker-install" && -d /www1 && ! -L /www1 ]]; then
  UPDATE_ROOT="$SAVED_UPDATE_ROOT"
else
  # Keep administrator uploads on the separate /www1 volume when available.
  UPDATE_ROOT="$(default_update_root)"
fi
if [[ "$UPDATE_ROOT" != /* || "$UPDATE_ROOT" == "/" || "$UPDATE_ROOT" == *$'\n'* || "$UPDATE_ROOT" == *$'\r'* || -L "$UPDATE_ROOT" ]]; then
  echo "错误：JOKER_UPDATE_ROOT 必须是安全的绝对目录路径：$UPDATE_ROOT" >&2
  exit 1
fi
TARGET_PARENT="$(dirname -- "$TARGET_DIR")"
UPDATE_PARENT="$(dirname -- "$UPDATE_ROOT")"
if [[ "$UPDATE_ROOT" == "$TARGET_DIR" || "$UPDATE_ROOT" == "$TARGET_DIR/"* || "$TARGET_DIR" == "$UPDATE_ROOT/"* ]]; then
  echo "错误：更新目录不能与运行目录 joker-web 重叠：$UPDATE_ROOT" >&2
  exit 1
fi
UPDATE_KEY_FILE="/root/joker-web-update-key.txt"
install -d -o root -g root -m 711 "$UPDATE_ROOT"
# The portal may create named task files but cannot list the inbox or enter
# root-owned processing directories.  The root worker moves every task out of
# this directory before it validates, copies, or executes it.
install -d -o root -g "$RUN_GROUP" -m 730 "$UPDATE_ROOT/incoming"
install -d -o root -g root -m 700 "$UPDATE_ROOT/processing"
install -d -o root -g root -m 750 /var/log/joker-web-updates
install -o root -g root -m 750 deploy/linux/joker-web-scan-updates.sh /usr/local/sbin/joker-web-scan-updates
install -o root -g root -m 750 deploy/linux/joker-web-apply-update.sh /usr/local/sbin/joker-web-apply-update
install -o root -g root -m 750 deploy/linux/joker-web-manual-update.sh /usr/local/sbin/joker-web-manual-update
install -d -o root -g root -m 755 /usr/local/libexec
install -o root -g root -m 750 deploy/linux/joker-web-secure-stage.py /usr/local/libexec/joker-web-secure-stage
if [[ ! -s "$UPDATE_KEY_FILE" ]]; then
  umask 077
  python3 -c 'import secrets; print(secrets.token_urlsafe(36))' >"$UPDATE_KEY_FILE"
  chmod 600 "$UPDATE_KEY_FILE"
  echo "已创建网页后台更新授权码：$UPDATE_KEY_FILE"
else
  chmod 600 "$UPDATE_KEY_FILE"
fi

{
  printf 'JOKER_INSTALL_DIR=%q\n' "$TARGET_DIR"
  printf 'JOKER_PORTAL_DOMAIN=%q\n' "$DOMAIN"
  printf 'JOKER_NGINX_SITE_ROOT=%q\n' "$NGINX_SITE_ROOT"
  printf 'JOKER_NGINX_CERT_DIR=%q\n' "$NGINX_CERT_DIR"
  printf 'JOKER_NGINX_VHOST_FILE=%q\n' "$NGINX_VHOST_FILE"
  if [[ -n "$NGINX_CERT_FULLCHAIN" ]]; then
    printf 'JOKER_NGINX_CERT_FULLCHAIN=%q\n' "$NGINX_CERT_FULLCHAIN"
    printf 'JOKER_NGINX_CERT_PRIVKEY=%q\n' "$NGINX_CERT_PRIVKEY"
  fi
  printf 'JOKER_UPDATE_ROOT=%q\n' "$UPDATE_ROOT"
  printf 'JOKER_UPDATE_WEB_USER=%q\n' "$RUN_USER"
  # The web updater copies each verified ZIP to this fixed transport directory
  # only while install.sh runs, then removes that exact archive. The live
  # project remains the stable JOKER_INSTALL_DIR sibling.
  printf 'JOKER_INSTALL_PACKAGE_DIR=%q\n' "$UPDATE_ROOT"
  # The root watcher waits briefly after a web upload response before an
  # installer can restart joker-web. This prevents a completed upload from
  # being reported as a browser/proxy timeout.
  printf 'JOKER_UPDATE_RESPONSE_GRACE_SECONDS=%q\n' "5"
} >/etc/joker-web-updater.conf
chmod 600 /etc/joker-web-updater.conf

cat >/etc/systemd/system/joker-web-update-watcher.service <<'EOF'
[Unit]
Description=Joker portal update watcher
After=network-online.target

[Service]
Type=oneshot
# The worker validates, extracts, installs Python dependencies, and restarts
# the portal. systemd's default start timeout is commonly 90 seconds, which
# is too short for a valid update on a busy or slow server.
TimeoutStartSec=30min
ExecStart=/usr/local/sbin/joker-web-scan-updates
EOF
cat >/etc/systemd/system/joker-web-update-watcher.path <<EOF
[Unit]
Description=Watch Joker portal update requests

[Path]
PathChanged=$UPDATE_ROOT/incoming
PathModified=$UPDATE_ROOT/incoming
Unit=joker-web-update-watcher.service

[Install]
WantedBy=multi-user.target
EOF
cat >/etc/systemd/system/joker-web-update-watcher.timer <<'EOF'
[Unit]
Description=Fallback scan for Joker portal update requests

[Timer]
OnBootSec=1min
OnUnitActiveSec=30s
Unit=joker-web-update-watcher.service

[Install]
WantedBy=timers.target
EOF

if [[ "$USE_RELEASE_STAGE" == true ]]; then
  echo "预检完成，正在原子切换到新的网页发布目录……"
  activate_release_stage
  ENV_FILE="$TARGET_DIR/web_client/.env"
  cd "$TARGET_DIR"
fi

# Render after the directory switch so generated static-file aliases point to
# the stable live path rather than the temporary staging directory.
render_nginx_config "$TARGET_DIR" "$DOMAIN" "$NGINX_SITE_ROOT"

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME" joker-web-update-watcher.path joker-web-update-watcher.timer
for _ in {1..15}; do
  if curl --fail --silent --show-error http://127.0.0.1:8765/health/ready >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl --fail --silent --show-error http://127.0.0.1:8765/health/ready >/dev/null

SYNC_INFO_FILE="/root/joker-web-local-sync.txt"
.web-venv/bin/python - "$ENV_FILE" "$SYNC_INFO_FILE" "$DOMAIN" <<'PY'
import sys
from pathlib import Path

from dotenv import dotenv_values

values = dotenv_values(sys.argv[1])
content = "\n".join(
    [
        "Joker 本地插件“远程网页同步”需要填写：",
        f"网页地址：https://{sys.argv[3]}",
        f"同步令牌：{values.get('JOKER_WEB_SYNC_TOKEN', '')}",
        f"卡密指纹盐：{values.get('JOKER_WEB_CARD_PEPPER', '')}",
        "同步间隔：10",
        "",
    ]
)
Path(sys.argv[2]).write_text(content, encoding="utf-8")
PY
chmod 600 "$SYNC_INFO_FILE"

INSTALL_COMPLETE_FILE="$TARGET_DIR/.joker-web-install-complete"
if [[ -L "$INSTALL_COMPLETE_FILE" ]]; then
  echo "错误：安装完成标记路径不能是符号链接：$INSTALL_COMPLETE_FILE" >&2
  exit 1
fi
INSTALL_COMPLETE_TEMP="$TARGET_DIR/.joker-web-install-complete.$$"
(
  umask 077
  printf 'completed_at=%s\ndomain=%s\nrelease_version=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$DOMAIN" "$PACKAGE_VERSION" >"$INSTALL_COMPLETE_TEMP"
)
chown root:root "$INSTALL_COMPLETE_TEMP"
chmod 600 "$INSTALL_COMPLETE_TEMP"
mv -f -- "$INSTALL_COMPLETE_TEMP" "$INSTALL_COMPLETE_FILE"

# This public marker is independent of the root-only completion marker above.
# It is written only after the new service passed /health/ready and completion
# was committed. A failed staged update restores the old directory, including
# its old marker; .env and all Windows sync secrets are never changed here.
write_release_metadata "$TARGET_DIR/.joker-web-release.json" "$MANIFEST_PATH"

# Publish the control command only after the replacement has passed its health
# check and the release marker was committed. The old command remains usable
# throughout an upgrade and therefore still points at the known-good release
# when an installation fails and rolls back. Keep the executable outside the
# live project tree: the package staging directory is removed after a web
# initiated update, so a command copied there would become unusable on the
# next invocation.
CONTROL_SCRIPT="/usr/local/libexec/joker-webctl"
install -d -o root -g root -m 755 /usr/local/libexec
install -o root -g root -m 750 "$SCRIPT_DIR/install.sh" "$CONTROL_SCRIPT"
CONTROL_LAUNCHER="/usr/local/sbin/joker-webctl"
CONTROL_LAUNCHER_TEMP="${CONTROL_LAUNCHER}.joker-new.$$"
cat >"$CONTROL_LAUNCHER_TEMP" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

# The updater settings file is generated by the root-owned installer. It is
# sourced here only to discover a custom stable project path; callers cannot
# replace it because this launcher is root-owned and mode 0750.
if [[ -r /etc/joker-web-updater.conf ]]; then
  # shellcheck disable=SC1091
  source /etc/joker-web-updater.conf
fi
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
SAVED_INSTALL_DIR="${JOKER_INSTALL_DIR:-}"
SYSTEMD_INSTALL_DIR="$(systemctl show -p WorkingDirectory --value joker-web 2>/dev/null || true)"
SYSTEMD_INSTALL_DIR="${SYSTEMD_INSTALL_DIR%$'\r'}"
if is_valid_install_dir "$SYSTEMD_INSTALL_DIR"; then
  JOKER_INSTALL_DIR="$SYSTEMD_INSTALL_DIR"
elif is_valid_install_dir "$SAVED_INSTALL_DIR"; then
  JOKER_INSTALL_DIR="$SAVED_INSTALL_DIR"
fi
export JOKER_INSTALL_DIR="${JOKER_INSTALL_DIR:-/www/wwwroot/joker-web}"
if [[ "${1:-}" == "--update" ]]; then
  shift
  exec /usr/local/sbin/joker-web-manual-update "$@"
fi
exec /usr/local/libexec/joker-webctl "$@"
EOF
chown root:root "$CONTROL_LAUNCHER_TEMP"
chmod 750 "$CONTROL_LAUNCHER_TEMP"
mv -f -- "$CONTROL_LAUNCHER_TEMP" "$CONTROL_LAUNCHER"

if [[ "$USE_RELEASE_STAGE" == true ]]; then
  if cleanup_successful_rollback; then
    echo "更新完成，项目目录保持为：$TARGET_DIR"
  else
    echo "警告：上一版未能移动到备份目录，请保留并检查：$PREVIOUS_RELEASE_DIR" >&2
  fi
fi

echo
echo "============================================================"
echo " 安装成功"
echo "============================================================"
echo "本机健康检查：http://127.0.0.1:8765/health"
echo "管理员：$ADMIN_STATUS"
echo "当前网页发布版本：$PACKAGE_VERSION"
echo "电脑 AstrBot 的同步参数已保存到：$SYNC_INFO_FILE"
echo "网页后台更新授权码保存到：$UPDATE_KEY_FILE"
echo
echo "Linux 只运行网页，不运行 AstrBot 或 NapCat。"
echo "现在只剩两个宝塔界面操作："
echo "1. 添加网站 $DOMAIN，先申请 SSL"
if [[ "$RENDERED_NGINX_MODE" == "tls" ]]; then
  echo "2. 将已生成的 HTTPS 反代配置应用到该站点：$RENDERED_NGINX_CONFIG"
  echo "   已识别证书：$RENDERED_NGINX_CERT_FULLCHAIN"
else
  echo "2. 先将 ACME 引导配置应用到该站点：$RENDERED_NGINX_CONFIG"
  echo "   SSL 签发后执行 joker-webctl --render-nginx，再应用同一路径的新 HTTPS 配置。"
fi
echo "   不要把网站反代到公网 IP，也不要开放 8765。"
echo
echo "查看同步参数：cat $SYNC_INFO_FILE"
echo "查看当前发布版本：joker-webctl --show-version"
echo "通过堡塔终端更新网页包：joker-webctl --update /path/to/Joker_Baota_Remote_vX.Y.Z-all-in-one.zip"
echo "将同步参数填入电脑 AstrBot 的 Joker 插件设置并启用“远程网页同步”。"
echo "网页后台更新：登录管理员后台 -> 更新；授权码可用 cat $UPDATE_KEY_FILE 查看。"
echo "查看服务状态：systemctl status $SERVICE_NAME --no-pager"
