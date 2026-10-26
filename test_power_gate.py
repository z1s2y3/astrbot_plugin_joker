"""Regression checks for the owner authorization/power gate boundary."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


class PowerGateTests(unittest.TestCase):
    def test_owner_bypass_is_limited_to_authorization_control_plane(self):
        source = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="main.py")
        plugin = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MyPlugin"
        )
        handler = next(
            node
            for node in plugin.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "handle_message"
        )
        handler_source = ast.get_source_segment(source, handler) or ""

        # A previous implementation used ``and not is_owner`` for both gates,
        # allowing the owner to run ordinary group commands after web/command
        # shutdown.  Only the exact owner authorization command predicate may
        # bypass those gates now.
        self.assertIn("owner_auth_command = self._is_owner_auth_command(event, message)", handler_source)
        self.assertIn("not owner_auth_command", handler_source)
        self.assertNotIn("not self._is_auth_command(message) and not is_owner", handler_source)


if __name__ == "__main__":
    unittest.main()
