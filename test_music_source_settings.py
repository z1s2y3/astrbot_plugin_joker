"""Tests for Linux-only music source service configuration."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from web_client.music_source_settings import (
    MUSIC_CARD_DEFAULT_SIGN_URL,
    MUSIC_PLATFORM_KEYS,
    MUSIC_SOURCE_SERVICES_SETTING_KEY,
    get_music_source_services,
    save_music_source_services,
    test_music_card_signer,
)
from web_client.settings import Settings


TEST_SETTINGS = Settings(
    database_url="sqlite://",
    session_secret="session-secret-012345678901234567890123456789",
    card_pepper="card-pepper-012345678901234567890123456789",
    secure_cookies=False,
    portal_name="test",
    session_max_age=3600,
)


class _FakeDb:
    def __init__(self):
        self.settings = {}

    def get(self, _model, key):
        return self.settings.get(key)

    def add(self, item):
        self.settings[item.key] = item


class MusicSourceSettingsTests(unittest.TestCase):
    def test_defaults_do_not_expose_a_service_or_key(self):
        soda = get_music_source_services(_FakeDb(), TEST_SETTINGS, include_secrets=False)["soda"]
        self.assertFalse(soda["enabled"])
        self.assertEqual("", soda["base_url"])
        self.assertFalse(soda["api_key_configured"])
        self.assertNotIn("api_key", soda)
        signer = get_music_source_services(
            _FakeDb(), TEST_SETTINGS, include_secrets=False
        )["music_card_signer"]
        self.assertTrue(signer["enabled"])
        self.assertEqual(MUSIC_CARD_DEFAULT_SIGN_URL, signer["sign_url"])
        self.assertFalse(signer["api_key_configured"])
        self.assertFalse(signer["cookie_configured"])
        self.assertNotIn("api_key", signer)
        self.assertNotIn("cookie", signer)
        public = get_music_source_services(
            _FakeDb(), TEST_SETTINGS, include_secrets=False
        )
        self.assertEqual(MUSIC_PLATFORM_KEYS, tuple(public["platform_cookies"]))
        self.assertTrue(
            all(
                not item["cookie_configured"]
                and "cookie" not in item
                for item in public["platform_cookies"].values()
            )
        )

    def test_soda_key_is_encrypted_and_blank_update_keeps_it(self):
        db = _FakeDb()
        saved = save_music_source_services(
            db,
            soda_enabled=True,
            soda_base_url="https://music-api.example.com/api",
            soda_api_key="bridge-secret",
            settings=TEST_SETTINGS,
            operator_id=7,
        )
        self.assertTrue(saved["soda"]["enabled"])
        self.assertEqual("bridge-secret", saved["soda"]["api_key"])
        raw = db.settings[MUSIC_SOURCE_SERVICES_SETTING_KEY].value_json
        self.assertNotIn("bridge-secret", str(raw))
        kept = save_music_source_services(
            db,
            soda_enabled=True,
            soda_base_url="https://music-api.example.com/api/",
            soda_api_key="",
            settings=TEST_SETTINGS,
            operator_id=7,
        )
        self.assertEqual("https://music-api.example.com/api", kept["soda"]["base_url"])
        self.assertEqual("bridge-secret", kept["soda"]["api_key"])

    def test_signer_url_and_key_are_saved_for_windows_heartbeat_only(self):
        db = _FakeDb()
        saved = save_music_source_services(
            db,
            soda_enabled=False,
            soda_base_url="",
            signer_enabled=True,
            signer_url="https://sign.example.com/music_card/card/",
            signer_api_key="signer-secret",
            platform_cookies={
                "qq": "uin=o123; qm_keyst=qq-secret",
                "163": "MUSIC_U=netease-secret",
            },
            signer_cookie="signer-session=secret",
            settings=TEST_SETTINGS,
            operator_id=7,
        )
        signer = saved["music_card_signer"]
        self.assertTrue(signer["enabled"])
        self.assertEqual(
            "https://sign.example.com/music_card/card", signer["sign_url"]
        )
        self.assertEqual("signer-secret", signer["api_key"])
        raw = db.settings[MUSIC_SOURCE_SERVICES_SETTING_KEY].value_json
        self.assertNotIn("signer-secret", str(raw))
        self.assertNotIn("qq-secret", str(raw))
        self.assertNotIn("netease-secret", str(raw))
        self.assertNotIn("signer-session", str(raw))
        public = get_music_source_services(db, TEST_SETTINGS, include_secrets=False)
        self.assertNotIn("api_key", public["music_card_signer"])
        self.assertNotIn("cookie", public["music_card_signer"])
        self.assertTrue(public["platform_cookies"]["qq"]["cookie_configured"])
        self.assertTrue(public["platform_cookies"]["163"]["cookie_configured"])
        self.assertEqual(
            "uin=o123; qm_keyst=qq-secret",
            saved["platform_cookies"]["qq"]["cookie"],
        )
        self.assertEqual("signer-session=secret", saved["music_card_signer"]["cookie"])

    def test_music_cookies_are_kept_when_admin_leaves_them_blank(self):
        db = _FakeDb()
        save_music_source_services(
            db,
            soda_enabled=False,
            soda_base_url="",
            signer_enabled=True,
            signer_url=MUSIC_CARD_DEFAULT_SIGN_URL,
            platform_cookies={"kugou": "kg-session=secret"},
            signer_cookie="sign-session=secret",
            settings=TEST_SETTINGS,
        )
        kept = save_music_source_services(
            db,
            soda_enabled=False,
            soda_base_url="",
            signer_enabled=True,
            signer_url=MUSIC_CARD_DEFAULT_SIGN_URL,
            platform_cookies={},
            signer_cookie="",
            settings=TEST_SETTINGS,
        )
        self.assertEqual("kg-session=secret", kept["platform_cookies"]["kugou"]["cookie"])
        self.assertEqual("sign-session=secret", kept["music_card_signer"]["cookie"])

    def test_enabled_signer_requires_a_valid_url(self):
        with self.assertRaisesRegex(ValueError, "启用音乐名片签名服务"):
            save_music_source_services(
                _FakeDb(),
                soda_enabled=False,
                soda_base_url="",
                signer_enabled=True,
                signer_url="",
                settings=TEST_SETTINGS,
            )

    def test_signer_error_text_is_not_reported_as_connected(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return "关闭id解析功能"

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def post(self, *_args, **_kwargs):
                return FakeResponse()

        with patch(
            "web_client.music_source_settings.httpx.Client",
            FakeClient,
        ):
            result = test_music_card_signer(
                {
                    "enabled": True,
                    "sign_url": "https://sign.example.com/card",
                }
            )
        self.assertFalse(result["success"])
        self.assertEqual("signer_connection_failed", result["code"])

    def test_enabled_soda_rejects_missing_or_unsafe_url(self):
        db = _FakeDb()
        with self.assertRaisesRegex(ValueError, "填写服务地址"):
            save_music_source_services(
                db,
                soda_enabled=True,
                soda_base_url="",
                settings=TEST_SETTINGS,
            )
        with self.assertRaisesRegex(ValueError, "不能包含账号"):
            save_music_source_services(
                db,
                soda_enabled=False,
                soda_base_url="https://user:pass@example.com",
                settings=TEST_SETTINGS,
            )


if __name__ == "__main__":
    unittest.main()
