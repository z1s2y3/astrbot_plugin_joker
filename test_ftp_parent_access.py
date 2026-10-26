"""Regression tests for selecting a SQLite database beside the FTP root."""

from __future__ import annotations

from contextlib import contextmanager
from ftplib import error_perm
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from web_client.integration_service import (
    FtpConnectionConfig,
    _test_ftp,
    _path_within_ftp_root,
    browse_ftp_directory,
    find_default_ftp_database,
    validate_ftp_database_file,
)
from web_client.services import PortalError


class _CaseSensitiveFtp:
    """Small FTP fixture whose visible names match the user's layout."""

    _LISTINGS = {
        "/": (
            ("SCUM", {"type": "dir"}),
            ("BjxRobot", {"type": "dir"}),
            ("Engine", {"type": "dir"}),
        ),
        "/SCUM": (("plugin.json", {"type": "file"}),),
        "/BjxRobot": (("beijixiong.db", {"type": "file", "size": "16"}),),
        # A same-named database in another sibling must not be auto-selected.
        "/Engine": (("beijixiong.db", {"type": "file", "size": "16"}),),
    }
    database_bytes = b""
    listed_paths: list[str] = []

    def mlsd(self, path, facts=None):
        self.listed_paths.append(path)
        if path not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        return iter(self._LISTINGS[path])

    def cwd(self, path):
        if path not in self._LISTINGS:
            raise error_perm("550 directory unavailable")

    def size(self, path):
        if path == "/BjxRobot/beijixiong.db":
            return len(self.database_bytes)
        raise error_perm("550 file unavailable")

    def retrbinary(self, command, callback, blocksize=8192):
        if command != "RETR /BjxRobot/beijixiong.db":
            raise error_perm("550 file unavailable")
        for start in range(0, len(self.database_bytes), blocksize):
            callback(self.database_bytes[start : start + blocksize])

    def voidcmd(self, command):
        if command != "TYPE I":
            raise error_perm("500 unsupported command")

    def quit(self):
        pass

    def close(self):
        pass


class _VirtualRootFtp(_CaseSensitiveFtp):
    """FTP fixture that rejects absolute paths and exposes only a chroot.

    This is common for panel-managed FTP accounts.  The login directory is
    represented by ``.``; `/BjxRobot/...` is a portal canonical path only.
    """

    def _wire(self, path):
        value = str(path or "")
        if value in {".", "./"}:
            return "/"
        if value.startswith("./"):
            return "/" + value[2:]
        if value.startswith("/"):
            raise error_perm("550 absolute path unavailable")
        return "/" + value

    def mlsd(self, path, facts=None):
        self.listed_paths.append(path)
        return iter(self._LISTINGS[self._wire(path)])

    def cwd(self, path):
        if self._wire(path) not in self._LISTINGS:
            raise error_perm("550 directory unavailable")

    def size(self, path):
        if self._wire(path) == "/BjxRobot/beijixiong.db":
            return len(self.database_bytes)
        raise error_perm("550 file unavailable")

    def retrbinary(self, command, callback, blocksize=8192):
        prefix, path = command.split(" ", 1)
        if prefix != "RETR" or self._wire(path) != "/BjxRobot/beijixiong.db":
            raise error_perm("550 file unavailable")
        for start in range(0, len(self.database_bytes), blocksize):
            callback(self.database_bytes[start : start + blocksize])


class _StrictVirtualRootFtp(_CaseSensitiveFtp):
    """A virtual-root server where absolute paths target another namespace.

    Some panel FTP daemons expose the account root after login but reserve a
    leading slash for the physical server namespace.  Relative paths are then
    only correct while the current directory remains the login root.
    """

    def __init__(self):
        self.current_path = "/"
        self.retr_paths: list[str] = []

    def _wire(self, path):
        value = str(path or "")
        if value.startswith("/"):
            raise error_perm("550 physical root unavailable")
        if value in {".", "./"}:
            return self.current_path
        if value.startswith("./"):
            value = value[2:]
        return "/" + "/".join(
            part
            for part in (self.current_path.strip("/") + "/" + value).split("/")
            if part
        )

    def mlsd(self, path, facts=None):
        self.listed_paths.append(path)
        resolved = self._wire(path)
        if resolved not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        return iter(self._LISTINGS[resolved])

    def cwd(self, path):
        resolved = self._wire(path)
        if resolved not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        self.current_path = resolved

    def size(self, path):
        if self._wire(path) == "/BjxRobot/beijixiong.db":
            return len(self.database_bytes)
        raise error_perm("550 file unavailable")

    def retrbinary(self, command, callback, blocksize=8192):
        prefix, path = command.split(" ", 1)
        self.retr_paths.append(path)
        if prefix != "RETR" or self._wire(path) != "/BjxRobot/beijixiong.db":
            raise error_perm("550 file unavailable")
        for start in range(0, len(self.database_bytes), blocksize):
            callback(self.database_bytes[start : start + blocksize])


class _VirtualRootPhysicalSiblingFtp(_CaseSensitiveFtp):
    """A `/` login root backed by SCUM with a permitted physical sibling."""

    _LISTINGS = {
        "/SCUM": (("plugin.json", {"type": "file"}),),
        "/BjxRobot": (("beijixiong.db", {"type": "file", "size": "16"}),),
        "/BjRobot": (("beijixiong.db", {"type": "file", "size": "16"}),),
    }
    database_bytes = b""

    def pwd(self):
        # The server reports a virtual root even though the physical login
        # directory is SCUM.  This makes the portal retain logical `/`.
        return "/"

    @staticmethod
    def _wire(path):
        value = str(path or "")
        if value in {".", "./"}:
            return "/SCUM"
        if value.startswith("../"):
            return "/" + value[3:]
        if value.startswith("/"):
            raise error_perm("550 physical root unavailable")
        return "/SCUM/" + value

    def mlsd(self, path, facts=None):
        self.listed_paths.append(path)
        resolved = self._wire(path)
        if resolved not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        return iter(self._LISTINGS[resolved])

    def nlst(self, path):
        # The fixture intentionally has no legacy listing support; returning
        # a bounded FTP error lets the production fallback surface PortalError.
        raise error_perm("550 listing unavailable")

    def size(self, path):
        if self._wire(path) == "/BjxRobot/beijixiong.db":
            return len(self.database_bytes)
        if self._wire(path) == "/BjRobot/beijixiong.db":
            return len(self.database_bytes)
        raise error_perm("550 file unavailable")

    def retrbinary(self, command, callback, blocksize=8192):
        prefix, path = command.split(" ", 1)
        if prefix != "RETR" or self._wire(path) not in {
            "/BjxRobot/beijixiong.db",
            "/BjRobot/beijixiong.db",
        }:
            raise error_perm("550 file unavailable")
        for start in range(0, len(self.database_bytes), blocksize):
            callback(self.database_bytes[start : start + blocksize])


class _DirectRetrOnlyFtp(_StrictVirtualRootFtp):
    """Fixture for servers that deny MLSD/NLST but permit direct RETR."""

    def mlsd(self, path, facts=None):
        raise error_perm("550 listing disabled")

    def nlst(self, path):
        raise error_perm("550 listing disabled")


class _NonChrootLoginFtp(_CaseSensitiveFtp):
    """A non-chroot FTP login whose PWD starts at the real `/SCUM`."""

    def __init__(self):
        self.current_path = "/SCUM"

    def pwd(self):
        return self.current_path

    def _wire(self, path):
        value = str(path or "")
        if value.startswith("/"):
            return value.rstrip("/") or "/"
        if value in {".", "./"}:
            return self.current_path
        if value.startswith("./"):
            value = value[2:]
        return "/" + "/".join(
            part
            for part in (self.current_path.strip("/") + "/" + value).split("/")
            if part
        )

    def mlsd(self, path, facts=None):
        self.listed_paths.append(path)
        resolved = self._wire(path)
        if resolved not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        return iter(self._LISTINGS[resolved])

    def cwd(self, path):
        resolved = self._wire(path)
        if resolved not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        self.current_path = resolved

    def size(self, path):
        if self._wire(path) == "/BjxRobot/beijixiong.db":
            return len(self.database_bytes)
        raise error_perm("550 file unavailable")

    def retrbinary(self, command, callback, blocksize=8192):
        prefix, path = command.split(" ", 1)
        if prefix != "RETR" or self._wire(path) != "/BjxRobot/beijixiong.db":
            raise error_perm("550 file unavailable")
        for start in range(0, len(self.database_bytes), blocksize):
            callback(self.database_bytes[start : start + blocksize])


class _LegacyBjRobotFtp(_CaseSensitiveFtp):
    _LISTINGS = {
        "/": (("BjRobot", {"type": "dir"}),),
        "/BjRobot": (("beijixiong.db", {"type": "file", "size": "16"}),),
    }

    def size(self, path):
        if path == "/BjRobot/beijixiong.db":
            return len(self.database_bytes)
        raise error_perm("550 file unavailable")

    def retrbinary(self, command, callback, blocksize=8192):
        if command != "RETR /BjRobot/beijixiong.db":
            raise error_perm("550 file unavailable")
        for start in range(0, len(self.database_bytes), blocksize):
            callback(self.database_bytes[start : start + blocksize])


class _CwdParentOnlyFtp(_VirtualRootPhysicalSiblingFtp):
    """Panel FTP that rejects ``../`` wire paths but accepts CWD traversal."""

    def __init__(self):
        self.current_path = "/SCUM"

    def pwd(self):
        return "/"

    def _resolve(self, path):
        value = str(path or "")
        if value in {".", "./"}:
            return self.current_path
        if value == "..":
            return "/"
        if value.startswith("../") or value.startswith("/"):
            raise error_perm("550 direct parent path unavailable")
        return "/" + "/".join(
            part for part in (self.current_path.strip("/") + "/" + value).split("/") if part
        )

    def cwd(self, path):
        resolved = self._resolve(path)
        if resolved not in self._LISTINGS and resolved != "/":
            raise error_perm("550 directory unavailable")
        self.current_path = resolved

    def mlsd(self, path, facts=None):
        resolved = self._resolve(path)
        if resolved not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        return iter(self._LISTINGS[resolved])

    def size(self, path):
        if self._resolve(path) == "/BjxRobot/beijixiong.db":
            return len(self.database_bytes)
        raise error_perm("550 file unavailable")

    def retrbinary(self, command, callback, blocksize=8192):
        prefix, path = command.split(" ", 1)
        if prefix != "RETR" or self._resolve(path) != "/BjxRobot/beijixiong.db":
            raise error_perm("550 file unavailable")
        for start in range(0, len(self.database_bytes), blocksize):
            callback(self.database_bytes[start : start + blocksize])


class _RootScumCwdParentOnlyFtp(_CwdParentOnlyFtp):
    """Login reports physical ``/SCUM`` while the saved portal root is ``/``."""

    def pwd(self):
        return self.current_path


class _NestedCwdParentOnlyFtp(_CwdParentOnlyFtp):
    """CWD-only panel layout for ``/data/SCUM`` and ``/data/BJXROBOT``."""

    _LISTINGS = {
        "/data": (("BJXROBOT", {"type": "dir"}),),
        "/data/SCUM": (("plugin.json", {"type": "file"}),),
        "/data/BJXROBOT": (("beijixiong.db", {"type": "file", "size": "16"}),),
    }

    def __init__(self):
        self.current_path = "/data/SCUM"

    def pwd(self):
        return self.current_path

    def _resolve(self, path):
        value = str(path or "")
        if value in {".", "./"}:
            return self.current_path
        if value == "..":
            return "/data"
        if value.startswith("../") or value.startswith("/"):
            raise error_perm("550 direct parent path unavailable")
        return "/" + "/".join(
            part
            for part in (self.current_path.strip("/") + "/" + value).split("/")
            if part
        )

    def cwd(self, path):
        resolved = self._resolve(path)
        if resolved not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        self.current_path = resolved

    def mlsd(self, path, facts=None):
        resolved = self._resolve(path)
        if resolved not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        return iter(self._LISTINGS[resolved])

    def size(self, path):
        if self._resolve(path) == "/data/BJXROBOT/beijixiong.db":
            return len(self.database_bytes)
        raise error_perm("550 file unavailable")

    def retrbinary(self, command, callback, blocksize=8192):
        prefix, path = command.split(" ", 1)
        if prefix != "RETR" or self._resolve(path) != "/data/BJXROBOT/beijixiong.db":
            raise error_perm("550 file unavailable")
        for start in range(0, len(self.database_bytes), blocksize):
            callback(self.database_bytes[start : start + blocksize])


class _PhysicalDeepRootCwdParentOnlyFtp(_NestedCwdParentOnlyFtp):
    """Physical PWD is deep while the portal intentionally stores root `/`."""

    _LISTINGS = {
        "/home/wwwroot": (("BJXROBOT", {"type": "dir"}),),
        "/home/wwwroot/SCUM": (("plugin.json", {"type": "file"}),),
        "/home/wwwroot/BJXROBOT": (
            ("beijixiong.db", {"type": "file", "size": "16"}),
        ),
    }

    def __init__(self):
        self.current_path = "/home/wwwroot/SCUM"

    def _resolve(self, path):
        value = str(path or "")
        if value in {".", "./"}:
            return self.current_path
        if value == "..":
            return "/home/wwwroot"
        if value.startswith("../") or value.startswith("/"):
            raise error_perm("550 direct parent path unavailable")
        return "/" + "/".join(
            part
            for part in (self.current_path.strip("/") + "/" + value).split("/")
            if part
        )

    def cwd(self, path):
        resolved = self._resolve(path)
        if resolved not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        self.current_path = resolved

    def mlsd(self, path, facts=None):
        resolved = self._resolve(path)
        if resolved not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        return iter(self._LISTINGS[resolved])

    def size(self, path):
        if self._resolve(path) == "/home/wwwroot/BJXROBOT/beijixiong.db":
            return len(self.database_bytes)
        raise error_perm("550 file unavailable")

    def retrbinary(self, command, callback, blocksize=8192):
        prefix, path = command.split(" ", 1)
        if prefix != "RETR" or self._resolve(path) != "/home/wwwroot/BJXROBOT/beijixiong.db":
            raise error_perm("550 file unavailable")
        for start in range(0, len(self.database_bytes), blocksize):
            callback(self.database_bytes[start : start + blocksize])


class _RootCwdOnlyFtp(_PhysicalDeepRootCwdParentOnlyFtp):
    """Panel FTP that allows ``CWD /`` but rejects ``CWD ..`` and paths.

    A few BaoTa configurations expose a physical login directory while the
    data channel rejects absolute MLSD/RETR paths and the literal ``..``
    component.  The only supported bridge is ``CWD /`` followed by the known
    database directory.
    """

    def _resolve(self, path):
        value = str(path or "")
        if value == "/":
            raise error_perm("550 absolute listing path unavailable")
        if value == ".." or value.startswith("../"):
            raise error_perm("550 parent component unavailable")
        if value.startswith("/"):
            raise error_perm("550 absolute path unavailable")
        if value in {".", "./"}:
            return self.current_path
        if value.startswith("./"):
            value = value[2:]
        return "/" + "/".join(
            part
            for part in (self.current_path.strip("/") + "/" + value).split("/")
            if part
        )

    def cwd(self, path):
        value = str(path or "")
        if value == "/":
            resolved = "/home/wwwroot"
        else:
            resolved = self._resolve(value)
        if resolved not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        self.current_path = resolved


class _PhysicalAbsoluteSiblingFtp(_PhysicalDeepRootCwdParentOnlyFtp):
    """FTP that permits only the physical absolute sibling path.

    This models a panel account whose login PWD is ``/home/wwwroot/SCUM``;
    ``CWD ..`` and logical ``/BjxRobot`` are denied, while
    ``/home/wwwroot/BjxRobot`` is readable.
    """

    _LISTINGS = {
        "/home/wwwroot/SCUM": (("plugin.json", {"type": "file"}),),
        "/home/wwwroot/BjxRobot": (
            ("beijixiong.db", {"type": "file", "size": "16"}),
        ),
    }

    def _resolve(self, path):
        value = str(path or "")
        if value in {".", "./"}:
            return self.current_path
        if value == ".." or value.startswith("../"):
            raise error_perm("550 parent component unavailable")
        if value.startswith("/"):
            return value.rstrip("/") or "/"
        # Relative sibling paths are intentionally denied; callers must use
        # the physical absolute fallback derived from PWD.
        raise error_perm("550 relative sibling unavailable")

    def cwd(self, path):
        resolved = self._resolve(path)
        if resolved not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        self.current_path = resolved

    def mlsd(self, path, facts=None):
        resolved = self._resolve(path)
        if resolved not in self._LISTINGS:
            raise error_perm("550 directory unavailable")
        return iter(self._LISTINGS[resolved])

    def size(self, path):
        if self._resolve(path) == "/home/wwwroot/BjxRobot/beijixiong.db":
            return len(self.database_bytes)
        raise error_perm("550 file unavailable")

    def retrbinary(self, command, callback, blocksize=8192):
        prefix, path = command.split(" ", 1)
        if (
            prefix != "RETR"
            or self._resolve(path) != "/home/wwwroot/BjxRobot/beijixiong.db"
        ):
            raise error_perm("550 file unavailable")
        for start in range(0, len(self.database_bytes), blocksize):
            callback(self.database_bytes[start : start + blocksize])


@contextmanager
def _open_fake_ftp(_config):
    yield _CaseSensitiveFtp()


@contextmanager
def _open_virtual_root_ftp(_config):
    yield _VirtualRootFtp()


@contextmanager
def _open_strict_virtual_root_ftp(_config):
    yield _StrictVirtualRootFtp()


@contextmanager
def _open_virtual_root_physical_sibling_ftp(_config):
    yield _VirtualRootPhysicalSiblingFtp()


@contextmanager
def _open_cwd_parent_only_ftp(_config):
    yield _CwdParentOnlyFtp()


@contextmanager
def _open_root_scum_cwd_parent_only_ftp(_config):
    yield _RootScumCwdParentOnlyFtp()


@contextmanager
def _open_nested_cwd_parent_only_ftp(_config):
    yield _NestedCwdParentOnlyFtp()


@contextmanager
def _open_physical_deep_root_cwd_parent_only_ftp(_config):
    yield _PhysicalDeepRootCwdParentOnlyFtp()


@contextmanager
def _open_root_cwd_only_ftp(_config):
    yield _RootCwdOnlyFtp()


@contextmanager
def _open_physical_absolute_sibling_ftp(_config):
    yield _PhysicalAbsoluteSiblingFtp()


@contextmanager
def _open_direct_retr_only_ftp(_config):
    yield _DirectRetrOnlyFtp()


@contextmanager
def _open_nonchroot_login_ftp(_config):
    yield _NonChrootLoginFtp()


@contextmanager
def _open_legacy_bjrobot_ftp(_config):
    yield _LegacyBjRobotFtp()


class FtpParentAccessTests(unittest.TestCase):
    def setUp(self):
        _CaseSensitiveFtp.listed_paths = []

    def _config(self, allow_parent: bool) -> FtpConnectionConfig:
        return FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            # The user-entered spelling intentionally differs from FTP.
            remote_path="scum",
            tls=False,
            passive=True,
            allow_parent_directory=allow_parent,
        )

    @patch("web_client.integration_service._open_ftp", _open_fake_ftp)
    def test_parent_access_finds_database_in_sibling_directory(self):
        config = self._config(True)

        top = browse_ftp_directory(config, "/")
        self.assertEqual(top.parent_path, "/SCUM")
        self.assertEqual(
            [entry.name for entry in top.entries], ["BjxRobot", "Engine", "SCUM"]
        )

        configured = browse_ftp_directory(config, "/SCUM")
        self.assertEqual(configured.path, "/SCUM")

        database_directory = browse_ftp_directory(config, "/BjxRobot")
        self.assertEqual(database_directory.path, "/BjxRobot")
        self.assertEqual(
            [entry.path for entry in database_directory.entries],
            ["/BjxRobot/beijixiong.db"],
        )
        self.assertEqual(
            find_default_ftp_database(config), "/BjxRobot/beijixiong.db"
        )

    @patch("web_client.integration_service._open_ftp", _open_fake_ftp)
    def test_parent_access_remains_disabled_by_default(self):
        with self.assertRaises(PortalError):
            browse_ftp_directory(self._config(False), "/")

    def test_parent_access_cannot_escape_a_non_root_parent(self):
        self.assertEqual(
            _path_within_ftp_root(
                "/data/SCUM",
                "/data/BjxRobot/beijixiong.db",
                "数据库文件路径",
                allow_parent_directory=True,
            ),
            "/data/BjxRobot/beijixiong.db",
        )
        with self.assertRaises(PortalError):
            _path_within_ftp_root(
                "/data/SCUM",
                "/other/BjxRobot/beijixiong.db",
                "数据库文件路径",
                allow_parent_directory=True,
            )

    def test_scope_accepts_server_case_for_a_lowercase_saved_root(self):
        self.assertEqual(
            _path_within_ftp_root(
                "/scum",
                "/SCUM/config.json",
                "FTP 目录",
                case_insensitive=True,
            ),
            "/SCUM/config.json",
        )

    @patch("web_client.integration_service._open_ftp", _open_fake_ftp)
    def test_validation_uses_server_case_for_the_sibling_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _CaseSensitiveFtp.database_bytes = database_path.read_bytes()
            resolved = validate_ftp_database_file(
                self._config(True), "/bjxrobot/BEIJIXIONG.DB"
            )
            self.assertEqual(resolved, "/BjxRobot/beijixiong.db")

    @patch("web_client.integration_service._open_ftp", _open_fake_ftp)
    def test_auto_select_checks_bjxrobot_when_ftp_root_is_virtual_root(self):
        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
            allow_parent_directory=False,
        )
        self.assertEqual(
            find_default_ftp_database(config), "/BjxRobot/beijixiong.db"
        )

    @patch("web_client.integration_service._open_ftp", _open_fake_ftp)
    def test_auto_select_does_not_scan_unrelated_siblings(self):
        self.assertEqual(
            find_default_ftp_database(self._config(True)),
            "/BjxRobot/beijixiong.db",
        )
        self.assertNotIn("/Engine", _CaseSensitiveFtp.listed_paths)

    @patch("web_client.integration_service._open_ftp", _open_fake_ftp)
    def test_connection_test_resolves_server_case_for_saved_root(self):
        _test_ftp(self._config(True))

    @patch("web_client.integration_service._open_ftp", _open_virtual_root_ftp)
    def test_virtual_root_accepts_absolute_canonical_database_path(self):
        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
        )
        top = browse_ftp_directory(config, "/")
        self.assertEqual(top.path, "/")
        self.assertEqual(find_default_ftp_database(config), "/BjxRobot/beijixiong.db")
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _VirtualRootFtp.database_bytes = database_path.read_bytes()
            self.assertEqual(
                validate_ftp_database_file(config, "/BjxRobot/beijixiong.db"),
                "/BjxRobot/beijixiong.db",
            )

    @patch("web_client.integration_service._open_ftp", _open_strict_virtual_root_ftp)
    def test_root_database_directory_uses_login_relative_path_without_cwd(self):
        """`/` remains the saved logical root on strict virtual-root FTP."""

        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
        )
        listing = browse_ftp_directory(config, "/BjxRobot")
        self.assertEqual(listing.path, "/BjxRobot")
        self.assertEqual(
            [entry.path for entry in listing.entries],
            ["/BjxRobot/beijixiong.db"],
        )

    @patch("web_client.integration_service._open_ftp", _open_strict_virtual_root_ftp)
    def test_root_database_download_prefers_login_relative_path(self):
        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _StrictVirtualRootFtp.database_bytes = database_path.read_bytes()
            self.assertEqual(
                validate_ftp_database_file(config, "/BjxRobot/beijixiong.db"),
                "/BjxRobot/beijixiong.db",
            )

    @patch("web_client.integration_service._open_ftp", _open_strict_virtual_root_ftp)
    def test_connection_test_keeps_root_as_login_virtual_root(self):
        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
        )
        _test_ftp(config)

    @patch(
        "web_client.integration_service._open_ftp",
        _open_virtual_root_physical_sibling_ftp,
    )
    def test_virtual_root_parent_bridge_only_uses_known_database_alias(self):
        """`/` stays canonical while an opted-in panel sibling is reachable."""

        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
            allow_parent_directory=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _VirtualRootPhysicalSiblingFtp.database_bytes = database_path.read_bytes()

            listing = browse_ftp_directory(config, "/BjxRobot")
            self.assertEqual(listing.path, "/BjxRobot")
            self.assertEqual(
                find_default_ftp_database(config), "/BjxRobot/beijixiong.db"
            )
            self.assertEqual(
                validate_ftp_database_file(config, "/BjxRobot/beijixiong.db"),
                "/BjxRobot/beijixiong.db",
            )

    @patch(
        "web_client.integration_service._open_ftp",
        _open_virtual_root_physical_sibling_ftp,
    )
    def test_virtual_root_parent_bridge_does_not_try_arbitrary_paths(self):
        """The parent bridge cannot become a generic `/..` browser."""

        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
            allow_parent_directory=True,
        )
        _VirtualRootPhysicalSiblingFtp.listed_paths = []
        with self.assertRaises(PortalError):
            browse_ftp_directory(config, "/Engine")
        self.assertNotIn("../Engine", _VirtualRootPhysicalSiblingFtp.listed_paths)

    @patch("web_client.integration_service._open_ftp", _open_cwd_parent_only_ftp)
    def test_parent_access_supports_cwd_only_ftp_servers(self):
        """A server may permit CWD traversal while rejecting ../ wire paths."""

        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
            allow_parent_directory=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _CwdParentOnlyFtp.database_bytes = database_path.read_bytes()
            self.assertEqual(
                find_default_ftp_database(config), "/BjxRobot/beijixiong.db"
            )
            self.assertEqual(
                validate_ftp_database_file(config, "/BjxRobot/beijixiong.db"),
                "/BjxRobot/beijixiong.db",
            )

    @patch(
        "web_client.integration_service._open_ftp",
        _open_root_scum_cwd_parent_only_ftp,
    )
    def test_root_login_scum_uses_cwd_bridge_for_sibling_database(self):
        """A `/` FTP setting may still land in physical `SCUM` after login."""

        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
            allow_parent_directory=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _RootScumCwdParentOnlyFtp.database_bytes = database_path.read_bytes()
            self.assertEqual(
                find_default_ftp_database(config), "/BjxRobot/beijixiong.db"
            )
            self.assertEqual(
                validate_ftp_database_file(config, "/BjxRobot/beijixiong.db"),
                "/BjxRobot/beijixiong.db",
            )

    @patch(
        "web_client.integration_service._open_ftp",
        _open_nested_cwd_parent_only_ftp,
    )
    def test_nested_scum_root_uses_cwd_bridge_without_absolute_paths(self):
        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/data/SCUM",
            tls=False,
            passive=True,
            allow_parent_directory=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _NestedCwdParentOnlyFtp.database_bytes = database_path.read_bytes()
            self.assertEqual(
                find_default_ftp_database(config), "/data/BJXROBOT/beijixiong.db"
            )
            self.assertEqual(
                validate_ftp_database_file(config, "/data/BJXROBOT/beijixiong.db"),
                "/data/BJXROBOT/beijixiong.db",
            )

    @patch(
        "web_client.integration_service._open_ftp",
        _open_physical_deep_root_cwd_parent_only_ftp,
    )
    def test_logical_root_can_bridge_from_physical_deep_pwd(self):
        """`remote_path=/` must work when PWD is `/home/wwwroot/SCUM`."""

        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
            allow_parent_directory=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _PhysicalDeepRootCwdParentOnlyFtp.database_bytes = database_path.read_bytes()
            self.assertEqual(
                find_default_ftp_database(config), "/BjxRobot/beijixiong.db"
            )
            self.assertEqual(
                validate_ftp_database_file(config, "/BjxRobot/beijixiong.db"),
                "/BjxRobot/beijixiong.db",
            )

    @patch("web_client.integration_service._open_ftp", _open_root_cwd_only_ftp)
    def test_logical_root_falls_back_to_cwd_slash_when_parent_component_is_denied(self):
        """A root bridge may use ``CWD /`` when ``CWD ..`` is unavailable."""

        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
            allow_parent_directory=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _RootCwdOnlyFtp.database_bytes = database_path.read_bytes()
            self.assertEqual(
                find_default_ftp_database(config), "/BjxRobot/beijixiong.db"
            )
            self.assertEqual(
                validate_ftp_database_file(config, "/BjxRobot/beijixiong.db"),
                "/BjxRobot/beijixiong.db",
            )

    @patch(
        "web_client.integration_service._open_ftp",
        _open_physical_absolute_sibling_ftp,
    )
    def test_physical_pwd_absolute_sibling_fallback(self):
        """Use PWD's physical parent when CWD traversal is unavailable."""

        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
            allow_parent_directory=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _PhysicalAbsoluteSiblingFtp.database_bytes = database_path.read_bytes()
            self.assertEqual(
                find_default_ftp_database(config), "/BjxRobot/beijixiong.db"
            )
            self.assertEqual(
                validate_ftp_database_file(config, "/BjxRobot/beijixiong.db"),
                "/BjxRobot/beijixiong.db",
            )

    @patch("web_client.integration_service._open_ftp", _open_cwd_parent_only_ftp)
    def test_non_root_scum_path_supports_cwd_parent_database(self):
        """A saved `scum` root can still select its physical sibling database."""

        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="scum",
            tls=False,
            passive=True,
            allow_parent_directory=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _CwdParentOnlyFtp.database_bytes = database_path.read_bytes()
            _test_ftp(config)
            self.assertEqual(
                find_default_ftp_database(config), "/BjxRobot/beijixiong.db"
            )
            self.assertEqual(
                validate_ftp_database_file(config, "/BjxRobot/beijixiong.db"),
                "/BjxRobot/beijixiong.db",
            )

    @patch("web_client.integration_service._open_ftp", _open_cwd_parent_only_ftp)
    def test_non_root_scum_parent_browse_exposes_only_database_sibling(self):
        """Parent browsing remains narrow when the daemon denies parent MLSD."""

        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="scum",
            tls=False,
            passive=True,
            allow_parent_directory=True,
        )
        listing = browse_ftp_directory(config, "/")
        self.assertEqual(listing.path, "/")
        self.assertEqual(
            [(entry.name, entry.kind) for entry in listing.entries],
            [("BjxRobot", "dir")],
        )

    @patch("web_client.integration_service._open_ftp", _open_direct_retr_only_ftp)
    def test_manual_database_path_can_download_when_listing_is_disabled(self):
        """A valid direct path must not depend on MLSD/NLST support."""

        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _DirectRetrOnlyFtp.database_bytes = database_path.read_bytes()
            self.assertEqual(
                validate_ftp_database_file(config, "/BjxRobot/beijixiong.db"),
                "/BjxRobot/beijixiong.db",
            )

    @patch("web_client.integration_service._open_ftp", _open_nonchroot_login_ftp)
    def test_nonchroot_login_pwd_uses_absolute_root_for_sibling_database(self):
        """PWD `/SCUM` must not make logical root `/` inspect `/SCUM` only."""

        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
        )
        self.assertEqual(
            find_default_ftp_database(config), "/BjxRobot/beijixiong.db"
        )
        listing = browse_ftp_directory(config, "/")
        self.assertIn("/BjxRobot", [entry.path for entry in listing.entries])

    @patch("web_client.integration_service._open_ftp", _open_nonchroot_login_ftp)
    def test_nonchroot_login_pwd_validates_absolute_database_path(self):
        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _NonChrootLoginFtp.database_bytes = database_path.read_bytes()
            self.assertEqual(
                validate_ftp_database_file(config, "/BjxRobot/beijixiong.db"),
                "/BjxRobot/beijixiong.db",
            )

    @patch("web_client.integration_service._open_ftp", _open_legacy_bjrobot_ftp)
    def test_legacy_bjrobot_alias_is_resolved_to_server_spelling(self):
        config = FtpConnectionConfig(
            enabled=True,
            host="ftp.example.invalid",
            port=21,
            username="user",
            password="secret",
            remote_path="/",
            tls=False,
            passive=True,
        )
        self.assertEqual(
            find_default_ftp_database(config), "/BjRobot/beijixiong.db"
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "beijixiong.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            _LegacyBjRobotFtp.database_bytes = database_path.read_bytes()
            self.assertEqual(
                validate_ftp_database_file(config, "/BjxRobot/beijixiong.db"),
                "/BjRobot/beijixiong.db",
            )


if __name__ == "__main__":
    unittest.main()
