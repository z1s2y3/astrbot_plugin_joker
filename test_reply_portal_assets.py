"""Focused tests for Linux-stored automatic-reply image references."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from web_client.database import Base
from web_client import remote_store, reply_asset_service, theme_service
from web_client.models import GroupConfigSnapshot, PortalReplyAsset, User
from web_client.services import add_auto_reply, delete_auto_reply


class PortalReplyAssetTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    @staticmethod
    def _prepared(payload: bytes = b"portable-webp-bytes"):
        return theme_service.PreparedThemeAsset(
            content_type="image/webp",
            image_data=payload,
            width=32,
            height=24,
        )

    def test_assets_are_group_scoped_reused_and_removed_when_unreferenced(self):
        with Session(self.engine) as db:
            user = User(username="customer", password_hash="hash")
            db.add(user)
            db.flush()
            first = reply_asset_service.store_prepared_reply_asset(
                db, "1102579807", user.id, self._prepared()
            )
            duplicate = reply_asset_service.store_prepared_reply_asset(
                db, "1102579807", user.id, self._prepared()
            )
            self.assertEqual(first.id, duplicate.id)
            reference = reply_asset_service.asset_reference(first)
            config = {
                "replies": {
                    "exact": {
                        "图片": {
                            "has_images": True,
                            "image_portal_assets": [reference],
                        }
                    }
                }
            }
            self.assertTrue(
                reply_asset_service.reply_asset_is_referenced(
                    config, first.id, first.sha256
                )
            )
            self.assertIsNotNone(
                reply_asset_service.get_referenced_reply_asset(
                    db, "1102579807", first.id, config
                )
            )
            self.assertEqual(
                reply_asset_service.cleanup_unreferenced_reply_assets(
                    db, "1102579807", {"replies": {}}
                ),
                1,
            )
            db.flush()
            self.assertIsNone(db.get(PortalReplyAsset, first.id))

    def test_image_only_reply_uses_opaque_reference_and_delete_cleans_asset(self):
        with Session(self.engine) as db, patch(
            "web_client.services.remote_data_mode", return_value=True
        ):
            user = User(username="customer", password_hash="hash")
            db.add(user)
            db.flush()
            asset = reply_asset_service.store_prepared_reply_asset(
                db, "1102579807", user.id, self._prepared(b"different-webp-bytes")
            )
            reference = reply_asset_service.asset_reference(asset)
            add_auto_reply(
                "1102579807",
                "regex",
                r"^图片$",
                "",
                db,
                user.id,
                image_assets=[reference],
            )
            snapshot = db.scalar(select(GroupConfigSnapshot))
            rule = snapshot.config_json["replies"]["regex"][0]["reply"]
            self.assertEqual(rule["reply"], "")
            self.assertTrue(rule["has_images"])
            self.assertEqual(rule["image_portal_assets"], [reference])
            self.assertEqual(rule["image_urls"], [])
            self.assertNotIn("base64", str(snapshot.config_json).lower())
            self.assertNotIn("different-webp-bytes", str(snapshot.config_json))

            delete_auto_reply("1102579807", "regex", r"^图片$", db, user.id)
            db.flush()
            self.assertIsNone(db.get(PortalReplyAsset, asset.id))

    def test_references_cannot_be_reused_from_another_group(self):
        with Session(self.engine) as db:
            user = User(username="customer", password_hash="hash")
            db.add(user)
            db.flush()
            asset = reply_asset_service.store_prepared_reply_asset(
                db, "1102579807", user.id, self._prepared()
            )
            with self.assertRaises(ValueError):
                reply_asset_service.verify_asset_references_for_group(
                    db,
                    "568586484",
                    [reply_asset_service.asset_reference(asset)],
                )


if __name__ == "__main__":
    unittest.main()
