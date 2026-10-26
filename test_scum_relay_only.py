"""Guard the SCUM relay-only query path against local-source regressions."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class ScumRelayOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.main_tree = ast.parse(cls.main_source)
        cls.schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    def test_query_commands_do_not_call_a_windows_local_provider(self) -> None:
        self.assertNotIn("ServerQuery", self.main_source)
        self.assertNotIn("self.server_query", self.main_source)
        self.assertNotIn("query_scum_service", self.main_source)

        functions = {
            node.name: node
            for node in ast.walk(self.main_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in (
            "_query_online",
            "_query_keyword",
            "_query_scum_via_linux",
        ):
            body = ast.get_source_segment(self.main_source, functions[name]) or ""
            self.assertNotIn("server_query", body)
        for legacy_name in (
            "query_server_ip",
            "_query_server_by_ip",
            "_get_scum_news",
            "show_scum_news",
            "stop_query",
        ):
            self.assertNotIn(legacy_name, functions)
        self.assertNotIn('command("查询服务器"', self.main_source)
        self.assertNotIn('command("scum更新"', self.main_source)

    def test_legacy_sources_and_configuration_are_removed(self) -> None:
        self.assertFalse((ROOT / "scum" / "query.py").exists())
        self.assertFalse((ROOT / "scum" / "remote_client.py").exists())
        self.assertFalse(any((ROOT / "scum_service").glob("*.py")))
        scum_settings = self.schema["scum"]["items"]
        self.assertIn("query_relay_url", scum_settings)
        self.assertNotIn("query_relay_key", scum_settings)
        self.assertIn("SCUM_RELAY_KEY=", (ROOT / "web_client" / ".env.example").read_text(encoding="utf-8"))
        for legacy_key in (
            "query_mode",
            "battlemetrics_proxy_url",
            "standalone_service_url",
            "standalone_service_token",
            "query_local_url",
        ):
            self.assertNotIn(legacy_key, scum_settings)

    def test_old_query_mode_command_is_not_registered(self) -> None:
        self.assertNotIn('command("查询方式"', self.main_source)
        self.assertNotIn("/查询方式", self.main_source)


if __name__ == "__main__":
    unittest.main()
