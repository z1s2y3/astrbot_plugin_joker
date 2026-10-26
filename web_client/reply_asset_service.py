"""Private image assets for Linux-configured automatic replies.

The Linux portal stores a compact, validated image rendition.  A group
configuration contains only an asset ID and SHA-256 digest; the Windows bot
downloads bytes later through the authenticated sync API and writes them into
its normal local image cache.  This keeps URLs, bearer tokens and image bytes
out of group configuration JSON.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import re
from typing import Any, BinaryIO, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import theme_service
from .models import PortalReplyAsset


# Reply-image uploads use the same streaming/re-encode path as portal
# backgrounds.  Do not impose the former 4 MiB-per-image / 12 MiB-per-rule
# product limits: large originals are accepted and compacted before storage.
# ``MEDIUMBLOB`` still requires one stored rendition to fit below its physical
# limit, while the request ceiling protects the web worker from unbounded
# multipart bodies.
MAX_REPLY_IMAGES_PER_RULE = 20
MAX_REPLY_ASSET_BYTES = theme_service.MAX_THEME_STORED_BYTES
MAX_REPLY_REQUEST_BYTES = theme_service.MAX_THEME_REQUEST_BYTES
MAX_REPLY_ASSET_TOTAL_BYTES = MAX_REPLY_REQUEST_BYTES
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def prepare_reply_asset_stream(source: BinaryIO) -> theme_service.PreparedThemeAsset:
    """Validate one uploaded reply image and produce a safe WebP rendition."""

    prepared = theme_service.prepare_theme_asset_stream(source)
    if len(prepared.image_data) > MAX_REPLY_ASSET_BYTES:
        raise ValueError("自动回复图片压缩后仍超过数据库可保存的单张上限")
    return prepared


def store_prepared_reply_asset(
    db: Session,
    group_id: str,
    owner_user_id: int | None,
    prepared: theme_service.PreparedThemeAsset,
) -> PortalReplyAsset:
    """Store or reuse a content-addressed reply image within one group."""

    payload = bytes(prepared.image_data)
    digest = sha256(payload).hexdigest()
    existing = db.scalar(
        select(PortalReplyAsset).where(
            PortalReplyAsset.group_id == str(group_id),
            PortalReplyAsset.sha256 == digest,
        )
    )
    if existing is not None:
        return existing
    asset = PortalReplyAsset(
        group_id=str(group_id),
        owner_user_id=owner_user_id,
        content_type=prepared.content_type,
        image_data=payload,
        sha256=digest,
        size_bytes=len(payload),
        width=int(prepared.width),
        height=int(prepared.height),
    )
    db.add(asset)
    # Allocate an ID before it is referenced inside the JSON group config.
    db.flush()
    return asset


def asset_reference(asset: PortalReplyAsset) -> dict[str, object]:
    return {"id": int(asset.id), "sha256": str(asset.sha256).lower()}


def normalize_asset_references(
    value: object, *, maximum: int = MAX_REPLY_IMAGES_PER_RULE
) -> list[dict[str, object]]:
    """Return a strict, de-duplicated list of opaque asset references."""

    if not isinstance(value, (list, tuple)):
        return []
    try:
        safe_maximum = max(0, min(MAX_REPLY_IMAGES_PER_RULE, int(maximum)))
    except (TypeError, ValueError):
        safe_maximum = MAX_REPLY_IMAGES_PER_RULE
    result: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            asset_id = int(item.get("id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        digest = str(item.get("sha256", "") or "").strip().lower()
        if asset_id <= 0 or asset_id > 2**63 - 1 or not _SHA256_RE.fullmatch(digest):
            continue
        key = (asset_id, digest)
        if key in seen:
            continue
        seen.add(key)
        result.append({"id": asset_id, "sha256": digest})
        if len(result) >= safe_maximum:
            break
    return result


def clone_group_reply_assets(
    db: Session,
    source_group_id: str,
    target_group_id: str,
    config: object,
    owner_user_id: int | None = None,
) -> dict[str, Any]:
    """Copy reply image assets while remapping references to the target group.

    Reply assets are deliberately scoped by ``group_id``.  A raw config copy
    would leave the target pointing at source-group asset IDs, which the sync
    endpoint correctly rejects.  Duplicate image bytes are reused per target;
    missing or stale source references are dropped while the text reply stays
    usable.
    """
    result = deepcopy(config) if isinstance(config, dict) else {}
    replies = result.get("replies")
    if not isinstance(replies, dict):
        return result

    asset_map: dict[tuple[int, str], dict[str, object] | None] = {}

    def clone_record(record: object) -> None:
        if not isinstance(record, dict):
            return
        references = normalize_asset_references(record.get("image_portal_assets"))
        if not references:
            return
        new_references: list[dict[str, object]] = []
        for reference in references:
            try:
                source_id = int(reference["id"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            digest = str(reference.get("sha256", "")).lower()
            cache_key = (source_id, digest)
            if cache_key not in asset_map:
                source_asset = db.scalar(
                    select(PortalReplyAsset).where(
                        PortalReplyAsset.id == source_id,
                        PortalReplyAsset.group_id == str(source_group_id),
                    )
                )
                if source_asset is None or str(source_asset.sha256).lower() != digest:
                    asset_map[cache_key] = None
                else:
                    target_asset = db.scalar(
                        select(PortalReplyAsset).where(
                            PortalReplyAsset.group_id == str(target_group_id),
                            PortalReplyAsset.sha256 == digest,
                        )
                    )
                    if target_asset is None:
                        target_asset = PortalReplyAsset(
                            group_id=str(target_group_id),
                            owner_user_id=owner_user_id,
                            content_type=source_asset.content_type,
                            image_data=bytes(source_asset.image_data),
                            sha256=digest,
                            size_bytes=int(source_asset.size_bytes),
                            width=int(source_asset.width),
                            height=int(source_asset.height),
                        )
                        db.add(target_asset)
                        db.flush()
                    asset_map[cache_key] = asset_reference(target_asset)
            target_reference = asset_map[cache_key]
            if target_reference is not None:
                new_references.append(deepcopy(target_reference))

        record["image_portal_assets"] = new_references
        record["has_images"] = bool(new_references)
        record["image_cache_refs"] = [""] * len(new_references)
        record["image_cache_status"] = "pending" if new_references else ""
        record["image_urls"] = []
        record["image_source_urls"] = []

    exact = replies.get("exact")
    if isinstance(exact, dict):
        for record in exact.values():
            clone_record(record)
    fuzzy_data = replies.get("fuzzy_keywords")
    if isinstance(fuzzy_data, dict):
        for record in fuzzy_data.values():
            clone_record(record)
    regex = replies.get("regex")
    if isinstance(regex, list):
        for entry in regex:
            if isinstance(entry, dict):
                clone_record(entry.get("reply"))
    return result


def verify_asset_references_for_group(
    db: Session, group_id: str, value: object
) -> list[dict[str, object]]:
    """Ensure every submitted reference names a stored asset for this group."""

    references = normalize_asset_references(value)
    if not references:
        return []
    by_id = {
        int(asset.id): asset
        for asset in db.scalars(
            select(PortalReplyAsset).where(
                PortalReplyAsset.group_id == str(group_id),
                PortalReplyAsset.id.in_([int(item["id"]) for item in references]),
            )
        )
    }
    for reference in references:
        asset = by_id.get(int(reference["id"]))
        if asset is None or str(asset.sha256).lower() != reference["sha256"]:
            raise ValueError("自动回复图片资源已失效，请重新选择图片")
    return references


def _iter_reply_records(config: object) -> Iterable[dict[str, Any]]:
    if not isinstance(config, dict):
        return
    replies = config.get("replies", {})
    if not isinstance(replies, dict):
        return
    exact = replies.get("exact", {})
    if isinstance(exact, dict):
        for record in exact.values():
            if isinstance(record, dict):
                yield record
    fuzzy_data = replies.get("fuzzy_keywords", {})
    if isinstance(fuzzy_data, dict):
        for record in fuzzy_data.values():
            if isinstance(record, dict):
                yield record
    regex = replies.get("regex", [])
    if isinstance(regex, list):
        for entry in regex:
            record = entry.get("reply") if isinstance(entry, dict) else None
            if isinstance(record, dict):
                yield record


def referenced_asset_pairs(config: object) -> set[tuple[int, str]]:
    """Return every valid reply-asset reference still present in a config."""

    result: set[tuple[int, str]] = set()
    for record in _iter_reply_records(config):
        for reference in normalize_asset_references(record.get("image_portal_assets")):
            result.add((int(reference["id"]), str(reference["sha256"])))
    return result


def reply_asset_is_referenced(
    config: object, asset_id: object, asset_sha256: object
) -> bool:
    try:
        normalized_id = int(asset_id)
    except (TypeError, ValueError, OverflowError):
        return False
    digest = str(asset_sha256 or "").strip().lower()
    return (normalized_id, digest) in referenced_asset_pairs(config)


def cleanup_unreferenced_reply_assets(
    db: Session, group_id: str, config: object
) -> int:
    """Remove only images no longer referenced by any rule in this group."""

    # Remote mutation helpers are deliberately idempotent and are also used by
    # lightweight adapters during retry/replay handling.  Those adapters only
    # persist the projected config and do not expose SQLAlchemy's ``scalars``
    # API; there is no asset table to clean in that context.  Keep the actual
    # database cleanup below unchanged while making the projection path safe.
    if not hasattr(db, "scalars"):
        return 0
    referenced = referenced_asset_pairs(config)
    stale = []
    for asset in db.scalars(
        select(PortalReplyAsset).where(PortalReplyAsset.group_id == str(group_id))
    ):
        if (int(asset.id), str(asset.sha256).lower()) not in referenced:
            stale.append(asset)
    for asset in stale:
        db.delete(asset)
    return len(stale)


def get_referenced_reply_asset(
    db: Session,
    group_id: str,
    asset_id: object,
    config: object,
) -> PortalReplyAsset | None:
    """Find one asset only when its exact ID/hash remains referenced."""

    try:
        normalized_id = int(asset_id)
    except (TypeError, ValueError, OverflowError):
        return None
    if normalized_id <= 0:
        return None
    asset = db.scalar(
        select(PortalReplyAsset).where(
            PortalReplyAsset.id == normalized_id,
            PortalReplyAsset.group_id == str(group_id),
        )
    )
    if asset is None:
        return None
    return (
        asset
        if reply_asset_is_referenced(config, asset.id, asset.sha256)
        else None
    )


def find_reply_asset(db: Session, asset_id: object) -> PortalReplyAsset | None:
    """Look up an asset by ID; caller must still enforce group ownership."""

    try:
        normalized_id = int(asset_id)
    except (TypeError, ValueError, OverflowError):
        return None
    if normalized_id <= 0:
        return None
    return db.scalar(
        select(PortalReplyAsset).where(PortalReplyAsset.id == normalized_id)
    )
