#!/usr/bin/env python3
"""Verify a Joker deployment ZIP or an extracted deployment tree.

``update-manifest.json`` is stored at the root of every deployment ZIP.  Its
``files`` field is a JSON object whose keys are normalised POSIX paths and
whose values contain the uncompressed byte count and SHA-256 of each payload
file.  The manifest is intentionally excluded from that map: a sidecar
``.sha256`` file covers the complete ZIP (including the manifest) when an
operator wants an additional package-level check.

Examples::

    python3 verify_update_manifest.py --zip Joker_Baota_Remote_v1.10.30-all-in-one.zip
    python3 verify_update_manifest.py --zip Joker_Baota_Remote_v1.10.30-all-in-one.zip \
        --sha256 Joker_Baota_Remote_v1.10.30-all-in-one.zip.sha256
    python3 verify_update_manifest.py --tree extracted/

The checker uses only Python's standard library and can be copied away from a
deployment package for offline verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any, Dict, Iterable, Mapping, Optional, Set, Tuple
import zipfile


MANIFEST_FILENAME = "update-manifest.json"
MANIFEST_FORMAT_VERSION = 1
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_NUMERIC_IDENTIFIER = r"(?:0|[1-9][0-9]*)"
_PRERELEASE_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_PATTERN = re.compile(
    rf"^{_NUMERIC_IDENTIFIER}\.{_NUMERIC_IDENTIFIER}\.{_NUMERIC_IDENTIFIER}"
    rf"(?:-(?:{_PRERELEASE_IDENTIFIER})(?:\.{_PRERELEASE_IDENTIFIER})*)?"
    rf"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class VerificationError(RuntimeError):
    """The archive, extracted tree, manifest, or checksum is invalid."""


def _fail(message: str) -> None:
    raise VerificationError(message)


def _normalise_file_path(value: object, *, label: str) -> str:
    """Return a safe, canonical POSIX path or raise ``VerificationError``."""
    if not isinstance(value, str) or not value:
        _fail(f"{label} must be a non-empty POSIX path")
    if "\\" in value:
        _fail(f"{label} must use '/' separators")

    candidate = PurePosixPath(value)
    normalised = "/".join(candidate.parts)
    if (
        value.startswith("/")
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "." in candidate.parts
        or normalised != value
    ):
        _fail(f"{label} is unsafe or not normalised: {value!r}")
    return value


def _hash_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return _hash_stream(stream)


def _reject_crlf_shell_script(path: str, read_bytes: Any) -> None:
    """Reject shell payloads with DOS line endings before installation.

    Bash treats the carriage return at the end of ``set -Eeuo pipefail`` (and
    other tokens) as part of the command.  A package can otherwise pass its
    SHA-256 manifest and only fail after the updater has already stopped the
    running service, producing a misleading 502.  The builder emits LF files;
    enforcing that contract here gives operators a deterministic preflight
    error instead.
    """

    if not (path.casefold() == "install.sh" or path.casefold().endswith(".sh")):
        return
    payload = read_bytes()
    if b"\r" in payload:
        _fail(
            f"Shell script uses CRLF line endings: {path}; "
            "rebuild the package with LF endings"
        )


def _reject_duplicate_json_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"Manifest contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_manifest(raw: bytes) -> Mapping[str, Any]:
    try:
        parsed = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"Cannot parse {MANIFEST_FILENAME}: {exc}")
    if not isinstance(parsed, dict):
        _fail("Manifest root must be an object")
    return parsed


def _validate_metadata(manifest: Mapping[str, Any]) -> Dict[str, str]:
    format_version = manifest.get("format_version")
    if (
        isinstance(format_version, bool)
        or not isinstance(format_version, int)
        or format_version != MANIFEST_FORMAT_VERSION
    ):
        _fail(f"Unsupported manifest format version: {format_version!r}")

    release_version = manifest.get("release_version")
    if not isinstance(release_version, str) or not SEMVER_PATTERN.fullmatch(
        release_version
    ):
        _fail(f"Manifest release_version is not SemVer: {release_version!r}")

    archive_filename = manifest.get("archive_filename")
    if not isinstance(archive_filename, str) or not archive_filename:
        _fail("Manifest archive_filename must be a non-empty string")
    if (
        Path(archive_filename).name != archive_filename
        or "/" in archive_filename
        or "\\" in archive_filename
    ):
        _fail("Manifest archive_filename must not contain a path")

    expected_prefix = f"Joker_Baota_Remote_v{release_version}-"
    if not (
        archive_filename.startswith(expected_prefix)
        and archive_filename.lower().endswith(".zip")
    ):
        _fail("Manifest archive_filename does not match release_version")
    return {
        "release_version": release_version,
        "archive_filename": archive_filename,
    }


def _manifest_files(manifest: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    _validate_metadata(manifest)
    rows = manifest.get("files")
    if not isinstance(rows, dict) or not rows:
        _fail("Manifest files must be a non-empty object")

    result: Dict[str, Mapping[str, Any]] = {}
    for path_value, row in rows.items():
        path = _normalise_file_path(path_value, label="Manifest file path")
        if path == MANIFEST_FILENAME:
            _fail("Manifest must not hash itself")
        if not isinstance(row, dict):
            _fail(f"Manifest record must be an object: {path}")

        expected_hash = row.get("sha256")
        if not isinstance(expected_hash, str) or not SHA256_PATTERN.fullmatch(
            expected_hash.lower()
        ):
            _fail(f"Manifest file hash is invalid: {path}")
        size = row.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            _fail(f"Manifest file size is invalid: {path}")
        result[path] = row
    return result


def _archive_files(archive: zipfile.ZipFile) -> Iterable[Tuple[str, zipfile.ZipInfo]]:
    seen: Set[str] = set()
    for info in archive.infolist():
        raw_name = info.filename.replace("\\", "/")
        is_directory = info.is_dir() or raw_name.endswith("/")
        name = raw_name.rstrip("/")
        if not name:
            _fail("Archive contains an empty entry name")
        name = _normalise_file_path(name, label="Archive entry")
        if name in seen:
            _fail(f"Archive contains duplicate entry: {name}")
        seen.add(name)
        if stat.S_ISLNK(info.external_attr >> 16):
            _fail(f"Archive contains a symbolic link: {name}")
        if not is_directory and name != MANIFEST_FILENAME:
            yield name, info


def _read_zip_manifest(archive: zipfile.ZipFile) -> Mapping[str, Any]:
    infos = [info for info in archive.infolist() if info.filename == MANIFEST_FILENAME]
    if len(infos) != 1 or infos[0].is_dir():
        _fail(f"Archive must contain exactly one root {MANIFEST_FILENAME}")
    try:
        return _parse_manifest(archive.read(infos[0]))
    except zipfile.BadZipFile as exc:
        _fail(f"Cannot read {MANIFEST_FILENAME}: {exc}")


def _check_file_map(
    expected_files: Mapping[str, Mapping[str, Any]],
    actual_files: Mapping[str, Any],
    read_bytes: Any,
    validate_payload: Any = None,
) -> None:
    missing = sorted(set(expected_files) - set(actual_files))
    unexpected = sorted(set(actual_files) - set(expected_files))
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("not listed: " + ", ".join(unexpected))
        _fail("Manifest file list does not match payload (" + "; ".join(details) + ")")

    for path, source in actual_files.items():
        expected = expected_files[path]
        if validate_payload is not None:
            validate_payload(path, source)
        expected_size = int(expected["size"])
        actual_size, actual_hash = read_bytes(source)
        if expected_size != actual_size:
            _fail(f"Manifest size mismatch for {path}")
        if actual_hash != str(expected["sha256"]).lower():
            _fail(f"Manifest SHA-256 mismatch for {path}")


def verify_archive(archive_path: Path) -> Mapping[str, Any]:
    """Verify the internal manifest in a ZIP and return the parsed manifest."""
    if not archive_path.is_file():
        _fail(f"Archive does not exist: {archive_path}")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            manifest = _read_zip_manifest(archive)
            metadata = _validate_metadata(manifest)
            if metadata["archive_filename"] != archive_path.name:
                _fail(
                    "Manifest archive_filename does not match the ZIP name: "
                    f"{metadata['archive_filename']!r} != {archive_path.name!r}"
                )
            expected_files = _manifest_files(manifest)
            actual_files = dict(_archive_files(archive))

            def read_entry(info: zipfile.ZipInfo) -> Tuple[int, str]:
                with archive.open(info, "r") as stream:
                    return info.file_size, _hash_stream(stream)

            def validate_entry(path: str, info: zipfile.ZipInfo) -> None:
                _reject_crlf_shell_script(path, lambda: archive.read(info))

            _check_file_map(
                expected_files,
                actual_files,
                read_entry,
                validate_entry,
            )
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        _fail(f"Cannot read archive: {exc}")
    return manifest


def _tree_files(
    tree_path: Path,
    *,
    ignored_root_files: Optional[Set[str]] = None,
    expected_files: Optional[Set[str]] = None,
) -> Dict[str, Path]:
    """Return manifest-relevant regular files from an extracted tree.

    A manual install commonly leaves the downloaded deployment ZIP and old
    deployment notes beside ``install.sh``.  Those files are transport input
    rather than extracted payload, so the current archive, its optional
    checksum sidecar, and stale root-level package artifacts are exempted.
    Nested files with the same basename remain part of the payload check.
    """
    ignored_root_files = ignored_root_files or set()
    expected_files = expected_files or set()

    def is_stale_transport_file(relative: str) -> bool:
        """Ignore package artifacts left beside an extracted update.

        Operators commonly unpack a new ZIP into the same directory used for
        the previous release.  Deployment notes and older ZIP/checksum files
        are transport inputs, not installed payload.  They must not make a
        valid new manifest fail before ``install.sh`` can stage the clean
        release directory.  Files that are listed by the current manifest are
        always retained and verified; nested files are never exempted.
        """

        if "/" in relative or relative in expected_files:
            return False
        name = relative.lower()
        if name.endswith(".txt"):
            return True
        if name.startswith("joker_baota_remote_v") and (
            name.endswith(".zip") or name.endswith(".zip.sha256")
        ):
            return True
        return False

    actual: Dict[str, Path] = {}
    for root, directory_names, file_names in os.walk(
        str(tree_path), topdown=True, followlinks=False
    ):
        root_path = Path(root)
        safe_directories = []
        for directory_name in directory_names:
            candidate = root_path / directory_name
            if candidate.is_symlink():
                _fail(f"Extracted tree contains a symbolic link: {candidate}")
            safe_directories.append(directory_name)
        directory_names[:] = sorted(safe_directories)
        for file_name in sorted(file_names):
            candidate = root_path / file_name
            if candidate.is_symlink() or not candidate.is_file():
                _fail(f"Extracted tree contains a non-regular file: {candidate}")
            relative = candidate.relative_to(tree_path).as_posix()
            relative = _normalise_file_path(relative, label="Extracted tree entry")
            if relative in actual:
                _fail(f"Extracted tree contains duplicate path: {relative}")
            if (
                relative != MANIFEST_FILENAME
                and relative not in ignored_root_files
                and not is_stale_transport_file(relative)
            ):
                actual[relative] = candidate
    return actual


def verify_tree(tree_path: Path) -> Mapping[str, Any]:
    """Verify an already extracted deployment tree and return its manifest."""
    if not tree_path.is_dir() or tree_path.is_symlink():
        _fail(f"Extracted tree does not exist or is not a directory: {tree_path}")
    manifest_path = tree_path / MANIFEST_FILENAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        _fail(f"Extracted tree is missing root {MANIFEST_FILENAME}")
    try:
        manifest = _parse_manifest(manifest_path.read_bytes())
    except OSError as exc:
        _fail(f"Cannot read {MANIFEST_FILENAME}: {exc}")
    metadata = _validate_metadata(manifest)
    expected_files = _manifest_files(manifest)
    archive_filename = metadata["archive_filename"]
    actual_files = _tree_files(
        tree_path,
        ignored_root_files={
            archive_filename,
            f"{archive_filename}.sha256",
        },
        expected_files=set(expected_files),
    )

    def read_path(path: Path) -> Tuple[int, str]:
        return path.stat().st_size, _hash_file(path)

    def validate_path(relative: str, path: Path) -> None:
        _reject_crlf_shell_script(relative, path.read_bytes)

    _check_file_map(expected_files, actual_files, read_path, validate_path)
    return manifest


def _read_sidecar(path: Path, archive_name: str) -> str:
    try:
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"Cannot read SHA-256 sidecar: {exc}")
    if len(lines) != 1:
        _fail("SHA-256 sidecar must contain exactly one checksum line")

    # Accept the generated form ("SHA256  <hash>  <filename>") and the
    # conventional sha256sum form ("<hash>  <filename>").
    match = re.fullmatch(r"(?:(?:SHA256)\s+)?([0-9a-fA-F]{64})\s+\*?(.+)", lines[0])
    if not match:
        _fail("SHA-256 sidecar has an invalid format")
    expected_hash, listed_name = match.groups()
    if listed_name != archive_name:
        _fail("SHA-256 sidecar filename does not match the ZIP name")
    return expected_hash.lower()


def verify_sidecar(archive_path: Path, sidecar_path: Path) -> None:
    """Verify the complete ZIP against a generated ``.sha256`` sidecar."""
    expected_hash = _read_sidecar(sidecar_path, archive_path.name)
    if _hash_file(archive_path) != expected_hash:
        _fail("SHA-256 sidecar does not match the complete ZIP")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a Joker deployment ZIP or extracted tree"
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--zip", dest="archive", type=Path, help="deployment ZIP")
    modes.add_argument("--tree", dest="tree", type=Path, help="extracted package root")
    parser.add_argument(
        "--sha256",
        type=Path,
        dest="sidecar",
        help="optional ZIP SHA-256 sidecar (only valid with --zip)",
    )
    arguments = parser.parse_args()
    if arguments.sidecar is not None and arguments.archive is None:
        parser.error("--sha256 can only be used together with --zip")

    try:
        if arguments.archive is not None:
            manifest = verify_archive(arguments.archive)
            if arguments.sidecar is not None:
                verify_sidecar(arguments.archive, arguments.sidecar)
            target_name = arguments.archive.name
        else:
            manifest = verify_tree(arguments.tree)
            target_name = str(arguments.tree)
    except VerificationError as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        return 1

    suffix = " and SHA-256 sidecar" if arguments.sidecar is not None else ""
    print(
        f"Verified {target_name}: release {manifest['release_version']}, "
        f"{len(manifest['files'])} payload files{suffix}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
