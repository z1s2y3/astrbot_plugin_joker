#!/usr/bin/env python3
"""Safely transfer a portal update from ``joker-web`` to root.

This program is only called by the root-owned update worker.  The web
process may write files to the incoming directory, but it must never retain
control of the exact bytes that a root process validates or executes.  The
handoff therefore moves the request into a root-only work directory, copies
each untrusted file into a new root-owned file, verifies that its source did
not change while it was copied, and validates/extracts the ZIP there.
"""

from __future__ import annotations

import argparse
import hmac
import os
from pathlib import Path, PurePosixPath
import pwd
import shutil
import stat
import sys
import zipfile


MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 180 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 400
MAX_AUTHORIZATION_BYTES = 512
REQUIRED_ARCHIVE_ENTRIES = {
    "install.sh",
    "update-manifest.json",
    # Match the package builder's critical-file guard. The root worker must
    # independently reject a partially built package before it stops the
    # existing service.
    "01_plugin_source/astrbot_plugin_joker/feature_catalog.py",
    "01_plugin_source/astrbot_plugin_joker/runtime_compat.py",
    "01_plugin_source/astrbot_plugin_joker/scum/__init__.py",
    "01_plugin_source/astrbot_plugin_joker/scum/database.py",
    "01_plugin_source/astrbot_plugin_joker/web_client/__main__.py",
    "01_plugin_source/astrbot_plugin_joker/web_client/app.py",
    "01_plugin_source/astrbot_plugin_joker/web_client/release_service.py",
    "01_plugin_source/astrbot_plugin_joker/web_client/models.py",
    "01_plugin_source/astrbot_plugin_joker/web_client/reply_asset_service.py",
    "01_plugin_source/astrbot_plugin_joker/web_client/theme_service.py",
    "01_plugin_source/astrbot_plugin_joker/web_client/cookie_service.py",
    "01_plugin_source/astrbot_plugin_joker/web_client/session_middleware.py",
    "01_plugin_source/astrbot_plugin_joker/web_client/integration_service.py",
    "01_plugin_source/astrbot_plugin_joker/web_client/requirements.txt",
    "01_plugin_source/astrbot_plugin_joker/web_client/self_update.py",
    "01_plugin_source/astrbot_plugin_joker/web_client/update_log.py",
    "01_plugin_source/astrbot_plugin_joker/web_client/templates/admin_update.html",
    "01_plugin_source/astrbot_plugin_joker/web_client/templates/admin_update_log.html",
    # Keep the retired account-wide page in release archives. Older root
    # update receivers still require this filename before they can install a
    # newer portal that redirects the route to group-scoped integrations.
    "01_plugin_source/astrbot_plugin_joker/web_client/templates/admin_integrations.html",
    "01_plugin_source/astrbot_plugin_joker/web_client/templates/group_integrations.html",
    "01_plugin_source/astrbot_plugin_joker/web_client/templates/admin_security.html",
    "01_plugin_source/astrbot_plugin_joker/web_client/templates/appearance.html",
    "01_plugin_source/astrbot_plugin_joker/deploy/linux/quick_install.sh",
    "01_plugin_source/astrbot_plugin_joker/deploy/linux/install_web.sh",
    "01_plugin_source/astrbot_plugin_joker/deploy/linux/health_check.sh",
    "01_plugin_source/astrbot_plugin_joker/deploy/linux/joker-web-scan-updates.sh",
    "01_plugin_source/astrbot_plugin_joker/deploy/linux/joker-web-apply-update.sh",
    "01_plugin_source/astrbot_plugin_joker/deploy/linux/joker-web-manual-update.sh",
    "01_plugin_source/astrbot_plugin_joker/deploy/linux/joker-web-secure-stage.py",
    "01_plugin_source/astrbot_plugin_joker/deploy/linux/joker-web.service.example",
    "01_plugin_source/astrbot_plugin_joker/deploy/linux/nginx-web.conf.example",
    "01_plugin_source/astrbot_plugin_joker/deploy/linux/render_nginx_config.py",
    "01_plugin_source/astrbot_plugin_joker/deploy/linux/verify_update_manifest.py",
}


class StageError(RuntimeError):
    """The staged update cannot be trusted or is not a valid package."""


def _raise(message: str) -> None:
    raise StageError(message)


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _raise(f"Cannot open update directory safely: {exc.strerror or exc}")
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        _raise("Update directory is not a directory")
    return descriptor


def _ensure_root_directory(parent_fd: int, name: str, mode: int) -> int:
    try:
        os.mkdir(name, mode=mode, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        _raise(f"Cannot create protected update directory: {exc.strerror or exc}")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        _raise(f"Cannot open protected update directory: {exc.strerror or exc}")
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & 0o077
    ):
        os.close(descriptor)
        _raise("Protected update directory has unsafe ownership or permissions")
    try:
        os.fchmod(descriptor, mode)
    except OSError as exc:
        os.close(descriptor)
        _raise(f"Cannot protect update directory: {exc.strerror or exc}")
    return descriptor


def _validate_untrusted_file(info: os.stat_result, web_uid: int, maximum_size: int) -> None:
    if not stat.S_ISREG(info.st_mode):
        _raise("Update task contains a non-regular file")
    if info.st_uid != web_uid:
        _raise("Update task file was not created by the web service account")
    if info.st_mode & 0o077:
        _raise("Update task file permissions are too broad")
    if info.st_nlink != 1:
        _raise("Update task file has unexpected hard links")
    if info.st_size < 0 or info.st_size > maximum_size:
        _raise("Update task file exceeds the allowed size")


def _move_untrusted_file(
    incoming_fd: int,
    work_fd: int,
    source_name: str,
    destination_name: str,
    web_uid: int,
    maximum_size: int,
) -> None:
    try:
        before = os.stat(source_name, dir_fd=incoming_fd, follow_symlinks=False)
    except FileNotFoundError:
        _raise("Update task is incomplete")
    except OSError as exc:
        _raise(f"Cannot inspect update task file: {exc.strerror or exc}")
    _validate_untrusted_file(before, web_uid, maximum_size)
    try:
        os.rename(
            source_name,
            destination_name,
            src_dir_fd=incoming_fd,
            dst_dir_fd=work_fd,
        )
        after = os.stat(destination_name, dir_fd=work_fd, follow_symlinks=False)
    except OSError as exc:
        _raise(f"Cannot secure update task file: {exc.strerror or exc}")
    _validate_untrusted_file(after, web_uid, maximum_size)


def _stable_copy(
    work_fd: int,
    source_name: str,
    destination_name: str,
    maximum_size: int,
) -> None:
    """Copy a moved web-owned file into a root-owned immutable snapshot."""
    read_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        source_fd = os.open(source_name, read_flags, dir_fd=work_fd)
    except OSError as exc:
        _raise(f"Cannot open secured update task file: {exc.strerror or exc}")

    temporary_name = f".{destination_name}.copying"
    destination_fd = -1
    try:
        # No process can open this file by pathname once it lives in the
        # root-only work directory.  Existing write descriptors are still
        # possible, so the before/after metadata comparison below rejects
        # a source changed while it was copied.
        os.fchown(source_fd, 0, 0)
        os.fchmod(source_fd, 0o400)
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_size:
            _raise("Secured update task file is invalid")

        destination_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=work_fd,
        )
        copied = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > maximum_size:
                _raise("Update task file exceeds the allowed size")
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_nlink",
        )
        if copied != before.st_size or any(
            getattr(before, field) != getattr(after, field) for field in stable_fields
        ):
            _raise("Update task changed during secure handoff")

        os.fchown(destination_fd, 0, 0)
        os.fchmod(destination_fd, 0o600)
        os.close(destination_fd)
        destination_fd = -1
        os.rename(
            temporary_name,
            destination_name,
            src_dir_fd=work_fd,
            dst_dir_fd=work_fd,
        )
    except OSError as exc:
        _raise(f"Cannot make protected update copy: {exc.strerror or exc}")
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)
        try:
            os.unlink(temporary_name, dir_fd=work_fd)
        except FileNotFoundError:
            pass


def _read_stable_request(work_fd: int, update_id: str) -> None:
    request_name = "request"
    try:
        request_fd = os.open(
            request_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=work_fd,
        )
    except OSError as exc:
        _raise(f"Cannot read secured update request: {exc.strerror or exc}")
    try:
        os.fchown(request_fd, 0, 0)
        os.fchmod(request_fd, 0o400)
        before = os.fstat(request_fd)
        content = os.read(request_fd, 128)
        if os.read(request_fd, 1):
            _raise("Update request is too large")
        after = os.fstat(request_fd)
        if (
            content != f"{update_id}\n".encode("ascii")
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            _raise("Update request changed during secure handoff")
    finally:
        os.close(request_fd)


def _validate_archive_and_extract(archive_path: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                _raise("Update archive contains too many files")

            names: set[str] = set()
            total_uncompressed = 0
            for info in infos:
                name = info.filename.replace("\\", "/")
                candidate = PurePosixPath(name)
                if (
                    not name
                    or candidate.is_absolute()
                    or ".." in candidate.parts
                    or name.startswith("/")
                ):
                    _raise("Update archive contains an unsafe path")
                if stat.S_ISLNK(info.external_attr >> 16):
                    _raise("Update archive contains a symbolic link")
                if name in names:
                    _raise("Update archive contains duplicate paths")
                names.add(name)
                total_uncompressed += max(0, int(info.file_size))
                if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                    _raise("Update archive expands beyond the allowed size")

            missing = sorted(REQUIRED_ARCHIVE_ENTRIES - names)
            if missing:
                preview = ", ".join(missing[:8])
                suffix = "" if len(missing) <= 8 else ", ..."
                _raise(
                    "Update archive is missing required deployment files: "
                    + preview
                    + suffix
                )

            destination.mkdir(mode=0o700)
            for info in infos:
                relative = PurePosixPath(info.filename.replace("\\", "/"))
                target = destination.joinpath(*relative.parts)
                if info.is_dir() or info.filename.endswith(("/", "\\")):
                    target.mkdir(mode=0o700, parents=True, exist_ok=False)
                    continue
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.open(info, "r") as source, target.open("xb") as output:
                    copied = 0
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > info.file_size:
                            _raise("Update archive entry has an invalid size")
                        output.write(chunk)
                if copied != info.file_size:
                    _raise("Update archive entry was truncated")
                os.chmod(target, 0o600)
    except StageError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        _raise(f"Cannot validate update archive: {exc}")


def _read_authorization(path: Path, key_file: Path) -> None:
    try:
        supplied = path.read_text(encoding="utf-8").strip("\r\n")
        key_info = key_file.lstat()
        if (
            not stat.S_ISREG(key_info.st_mode)
            or key_info.st_uid != 0
            or key_info.st_mode & 0o077
        ):
            _raise("Server update authorization key is invalid")
        expected = key_file.read_text(encoding="utf-8").strip("\r\n")
    except UnicodeDecodeError:
        _raise("Update authorization code is invalid")
    except OSError as exc:
        _raise(f"Cannot read server update authorization key: {exc.strerror or exc}")
    if not expected or not hmac.compare_digest(expected, supplied):
        _raise("Update authorization code is incorrect")


def _remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def stage_update(update_root: Path, update_id: str, web_user: str, key_file: Path) -> Path:
    if len(update_id) != 32 or any(character not in "0123456789abcdef" for character in update_id):
        _raise("Update task identifier is invalid")
    try:
        web_uid = pwd.getpwnam(web_user).pw_uid
    except KeyError:
        _raise("Configured web service account does not exist")

    root_fd = _open_directory(update_root)
    incoming_fd = processing_fd = work_fd = -1
    work_path = update_root / "processing" / update_id
    try:
        root_info = os.fstat(root_fd)
        if root_info.st_uid != 0 or root_info.st_mode & 0o022:
            _raise("Update root directory has unsafe ownership or permissions")
        incoming_fd = _open_directory(update_root / "incoming")
        incoming_info = os.fstat(incoming_fd)
        if incoming_info.st_mode & 0o002:
            _raise("Update incoming directory is world writable")
        processing_fd = _ensure_root_directory(root_fd, "processing", 0o700)
        try:
            os.mkdir(update_id, mode=0o700, dir_fd=processing_fd)
        except FileExistsError:
            _raise("Update task is already being processed")
        work_fd = _open_directory(work_path)

        _move_untrusted_file(
            incoming_fd, work_fd, f"{update_id}.request", "request", web_uid, 128
        )
        _read_stable_request(work_fd, update_id)
        _move_untrusted_file(
            incoming_fd,
            work_fd,
            f"{update_id}.zip",
            "archive.source",
            web_uid,
            MAX_ARCHIVE_BYTES,
        )
        _move_untrusted_file(
            incoming_fd,
            work_fd,
            f"{update_id}.auth",
            "authorization.source",
            web_uid,
            MAX_AUTHORIZATION_BYTES,
        )
        _stable_copy(work_fd, "archive.source", "package.zip", MAX_ARCHIVE_BYTES)
        _stable_copy(
            work_fd,
            "authorization.source",
            "authorization.txt",
            MAX_AUTHORIZATION_BYTES,
        )
        os.unlink("request", dir_fd=work_fd)
        os.unlink("archive.source", dir_fd=work_fd)
        os.unlink("authorization.source", dir_fd=work_fd)

        _read_authorization(work_path / "authorization.txt", key_file)
        _validate_archive_and_extract(work_path / "package.zip", work_path / "payload")
        return work_path
    except Exception:
        _remove_tree(work_path)
        # A malformed or incomplete request must not wake the root worker
        # forever.  Removing this name is safe: it is confined by dir_fd to
        # the inbox and a fresh browser submission creates a new random ID.
        if incoming_fd >= 0:
            # Remove all three inbox artifacts.  A failure can occur before
            # they have all been moved, and retaining a ZIP here needlessly
            # consumes the web-user quota and leaves a stale task behind.
            for suffix in ("request", "zip", "auth"):
                try:
                    os.unlink(f"{update_id}.{suffix}", dir_fd=incoming_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
        raise
    finally:
        for descriptor in (work_fd, processing_fd, incoming_fd, root_fd):
            if descriptor >= 0:
                os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description="Securely stage a Joker portal update")
    parser.add_argument("--update-root", required=True)
    parser.add_argument("--update-id", required=True)
    parser.add_argument("--web-user", required=True)
    parser.add_argument("--key-file", required=True)
    arguments = parser.parse_args()
    try:
        work_path = stage_update(
            Path(arguments.update_root),
            arguments.update_id,
            arguments.web_user,
            Path(arguments.key_file),
        )
    except StageError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive root helper guard
        print(f"Unexpected secure staging failure: {exc}", file=sys.stderr)
        return 1
    print(work_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
