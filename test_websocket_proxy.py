"""Nginx upgrade forwarding needed by the game WebSocket endpoint."""

from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path


class WebSocketProxyTests(unittest.TestCase):
    def test_rendered_proxy_preserves_upgrade_headers_on_exact_paths(self):
        root = Path(__file__).parent
        renderer = runpy.run_path(str(root / "deploy" / "linux" / "render_nginx_config.py"))
        with tempfile.TemporaryDirectory() as temporary_dir:
            output = Path(temporary_dir) / "portal.conf"
            renderer["render_config"](
                root / "deploy" / "linux" / "nginx-web.conf.example",
                output,
                "portal.example.com",
                "/www/wwwroot/portal.example.com",
                "/www/wwwroot/joker-web",
                "/cert/fullchain.pem",
                "/cert/privkey.pem",
            )
            rendered = output.read_text(encoding="utf-8")
        for path in ("/api/sync/v1/ws", "/api/game/v1/ws"):
            self.assertIn(f"location = {path} {{", rendered)
        self.assertEqual(2, rendered.count("proxy_set_header Upgrade $http_upgrade;"))
        self.assertEqual(2, rendered.count('proxy_set_header Connection "upgrade";'))
        self.assertEqual(2, rendered.count("proxy_read_timeout 3600s;"))


if __name__ == "__main__":
    unittest.main()
