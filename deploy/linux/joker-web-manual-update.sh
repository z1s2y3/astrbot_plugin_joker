#!/usr/bin/env bash
# Run a local Linux portal package through the same verified installer used by
# the web upload worker. This command is installed root-only by quick_install.
set -Eeuo pipefail

MAX_ARCHIVE_BYTES=$((80 * 1024 * 1024))
MAX_UNCOMPRESSED_BYTES=$((180 * 1024 * 1024))
PACKAGE_PATH="${1:-}"
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" || -z "$PACKAGE_PATH" || "$#" -ne 1 ]]; then
  echo "用法：joker-webctl --update /www1/joker-install/Joker_Baota_Remote_vX.Y.Z-all-in-one.zip" >&2
  echo "没有 /www1 时可使用 /www/wwwroot/joker-install 下的更新包。" >&2
  exit 2
fi
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "错误：网页更新命令必须由 root 或宝塔终端执行。" >&2
  exit 1
fi
if [[ -L "$PACKAGE_PATH" || ! -f "$PACKAGE_PATH" ]]; then
  echo "错误：更新包不存在或不是普通文件：$PACKAGE_PATH" >&2
  exit 1
fi
PACKAGE_PATH="$(realpath -e -- "$PACKAGE_PATH")"
if [[ "$PACKAGE_PATH" == *$'\n'* || "$PACKAGE_PATH" == *$'\r'* ]]; then
  echo "错误：更新包路径不能包含换行。" >&2
  exit 1
fi
PACKAGE_BYTES="$(stat -c '%s' -- "$PACKAGE_PATH")"
if ! [[ "$PACKAGE_BYTES" =~ ^[0-9]+$ ]] || (( PACKAGE_BYTES == 0 || PACKAGE_BYTES > MAX_ARCHIVE_BYTES )); then
  echo "错误：更新包必须大于 0 且不超过 80 MiB。" >&2
  exit 1
fi

WORK_DIR="$(mktemp -d /tmp/joker-web-manual-update.XXXXXX)"
cleanup() {
  rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT

# Extract only normal files into a fresh directory. The manifest verifier then
# checks every extracted byte before install.sh is allowed to run.
python3 - "$PACKAGE_PATH" "$WORK_DIR" "$MAX_UNCOMPRESSED_BYTES" <<'PY'
from __future__ import annotations

import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

archive_path = Path(sys.argv[1])
destination = Path(sys.argv[2])
max_uncompressed = int(sys.argv[3])
max_entries = 400
total_size = 0
seen: set[str] = set()

def safe_name(raw: str) -> tuple[str, bool]:
    if "\\" in raw:
        raise ValueError(f"ZIP 路径必须使用正斜杠：{raw!r}")
    normalized = raw.replace("\\", "/")
    is_directory = normalized.endswith("/")
    name = normalized.rstrip("/")
    path = PurePosixPath(name)
    canonical = "/".join(path.parts)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or canonical != name
    ):
        raise ValueError(f"不安全的 ZIP 路径：{raw!r}")
    return name, is_directory

try:
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if not members or len(members) > max_entries:
            raise ValueError("ZIP 条目数量无效")
        for info in members:
            name, is_directory = safe_name(info.filename)
            if name in seen:
                raise ValueError(f"ZIP 包含重复条目：{name}")
            seen.add(name)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ValueError(f"ZIP 不允许符号链接：{name}")
            if is_directory:
                (destination / name).mkdir(parents=True, exist_ok=True)
                continue
            if info.file_size < 0:
                raise ValueError(f"ZIP 条目大小无效：{name}")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("xb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    total_size += len(chunk)
                    if total_size > max_uncompressed:
                        raise ValueError("ZIP 实际解压后大小超过 180 MiB")
                    output.write(chunk)
except (OSError, ValueError, zipfile.BadZipFile) as exc:
    raise SystemExit(f"更新包解压校验失败：{exc}") from exc
PY

VERIFIER="$WORK_DIR/01_plugin_source/astrbot_plugin_joker/deploy/linux/verify_update_manifest.py"
if [[ ! -f "$WORK_DIR/install.sh" || ! -f "$VERIFIER" ]]; then
  echo "错误：更新包缺少 install.sh 或清单校验器。" >&2
  exit 1
fi
if ! python3 "$VERIFIER" --tree "$WORK_DIR"; then
  echo "错误：更新包清单校验失败，未执行安装。" >&2
  exit 1
fi

echo "更新包校验通过，开始执行 Linux 网页端升级……"
cd "$WORK_DIR"
bash ./install.sh --non-interactive
