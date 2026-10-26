"""Portal appearance preferences and safe database-backed background assets.

This module deliberately owns only portal UI data.  It is not imported by the
Windows sync projection, so a customer's background image can never become a
group configuration payload or be sent to the Windows robot.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Any, BinaryIO
import warnings

from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer

from .models import PortalThemeAsset, PortalThemePreference, User


# There is intentionally no per-image source-file size limit.  The reverse
# proxy and the appearance form retain one total request budget so an
# unauthenticated client cannot exhaust the HTTP worker.  Each accepted source
# is re-encoded to a compact WebP rendition before it enters the MEDIUMBLOB
# column or is served as a CSS background.
MAX_THEME_REQUEST_BYTES = 82 * 1024 * 1024
MAX_THEME_STORED_BYTES = 15 * 1024 * 1024
MAX_THEME_RENDER_DIMENSION = 3_840
MAX_THEME_ASSETS_PER_USER = 20
MAX_THEME_IMAGE_PIXELS = 20_000_000
MAX_THEME_DIMENSION = 8_192
THEME_MODES = {"auto", "day", "night"}
_IMAGE_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


def _asset_url(asset_id: int | None) -> str:
    return f"/theme/assets/{asset_id}" if isinstance(asset_id, int) and asset_id > 0 else ""


def _asset_id(value: object) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        asset_id = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("背景图片编号无效") from exc
    if asset_id <= 0:
        raise ValueError("背景图片编号无效")
    return asset_id


def _can_view_asset(asset: PortalThemeAsset | None, user: User) -> bool:
    return bool(
        asset
        and (
            user.is_admin
            or asset.owner_user_id == user.id
            or asset.is_shared
        )
    )


def get_accessible_asset(
    db: Session, user: User, asset_id: object, *, load_data: bool = True
) -> PortalThemeAsset | None:
    """Return an asset only when the requester is allowed to view its bytes."""

    try:
        normalized_id = _asset_id(asset_id)
    except ValueError:
        return None
    if normalized_id is None:
        return None
    statement = select(PortalThemeAsset).where(PortalThemeAsset.id == normalized_id)
    if not load_data:
        # Listing an appearance page must not pull every multi-megabyte image
        # into memory just to render dimensions and sharing state.
        statement = statement.options(defer(PortalThemeAsset.image_data))
    asset = db.scalar(statement)
    return asset if _can_view_asset(asset, user) else None


def get_theme_context(db: Session, user: User | None) -> dict[str, str]:
    """Build template-safe URLs for a user's currently selectable assets."""

    default = {"mode": "auto", "day_url": "", "night_url": ""}
    if user is None:
        return default
    preference = db.get(PortalThemePreference, user.id)
    if preference is None:
        return default
    mode = preference.mode if preference.mode in THEME_MODES else "auto"
    day_asset = get_accessible_asset(
        db, user, preference.day_asset_id, load_data=False
    )
    night_asset = get_accessible_asset(
        db, user, preference.night_asset_id, load_data=False
    )
    return {
        "mode": mode,
        "day_url": _asset_url(day_asset.id if day_asset is not None else None),
        "night_url": _asset_url(night_asset.id if night_asset is not None else None),
    }


def list_selectable_assets(db: Session, user: User) -> list[PortalThemeAsset]:
    """List own assets plus assets their owners chose to share.

    Administrators can inspect and choose all centrally stored assets.  Other
    users never receive IDs for another user's private image.
    """

    statement = (
        select(PortalThemeAsset)
        .options(defer(PortalThemeAsset.image_data))
        .order_by(PortalThemeAsset.updated_at.desc())
    )
    if not user.is_admin:
        statement = statement.where(
            (PortalThemeAsset.owner_user_id == user.id)
            | (PortalThemeAsset.is_shared.is_(True))
        )
    return list(db.scalars(statement))


def list_owned_assets(db: Session, user: User) -> list[PortalThemeAsset]:
    return list(
        db.scalars(
            select(PortalThemeAsset)
            .options(defer(PortalThemeAsset.image_data))
            .where(PortalThemeAsset.owner_user_id == user.id)
            .order_by(PortalThemeAsset.updated_at.desc())
        )
    )


def list_central_assets(db: Session) -> list[PortalThemeAsset]:
    """Return the administrator's centralized asset inventory."""

    return list(
        db.scalars(
            select(PortalThemeAsset)
            .options(defer(PortalThemeAsset.image_data))
            .order_by(PortalThemeAsset.updated_at.desc())
        )
    )


def _validate_theme_image_stream(source: BinaryIO) -> tuple[str, int, int]:
    """Validate a seekable raster stream without copying the source into RAM.

    Starlette stores multipart files in a ``SpooledTemporaryFile``. Processing
    that file object directly means a large original does not become a second
    in-memory byte string inside the application. Its decoded dimensions,
    format and malformed/compression-bomb behaviour are still strictly checked
    before any pixels are loaded.
    """

    if not callable(getattr(source, "read", None)) or not callable(
        getattr(source, "seek", None)
    ):
        raise ValueError("背景图片上传格式无效")
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # Deployment installs Pillow from requirements.
        raise ValueError("服务器缺少背景图片校验组件，请先更新网页部署包") from exc

    try:
        source.seek(0)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                # ``verify`` rejects malformed container data without asking
                # Pillow to expand pixels. The preparation pass reopens the
                # stream and performs the actual decode.
                image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("背景图片文件损坏或格式不受支持") from exc
    except Image.DecompressionBombWarning as exc:
        raise ValueError("背景图片像素过大") from exc
    except Exception as exc:
        # Pillow has format-specific parsers that can raise SyntaxError or
        # EOFError for truncated files. Treat every parser failure as a
        # user-facing validation error instead of returning a 500 response.
        raise ValueError("背景图片文件损坏或格式不受支持") from exc

    content_type = _IMAGE_TYPES.get(image_format)
    if content_type is None:
        raise ValueError("仅支持 PNG、JPG 和 WebP 背景图片")
    if (
        width < 1
        or height < 1
        or width > MAX_THEME_DIMENSION
        or height > MAX_THEME_DIMENSION
        or width * height > MAX_THEME_IMAGE_PIXELS
    ):
        raise ValueError("背景图片尺寸过大，请使用不超过 8192 像素边长的图片")
    return content_type, width, height


def _validate_image_bytes(image_data: bytes) -> tuple[str, int, int]:
    """Compatibility wrapper for service callers that already have bytes.

    The former implementation applied an arbitrary five-megabyte check here.
    The size of a selected source is no longer a validity condition; only its
    true image metadata and safe decoded representation matter.
    """

    if not isinstance(image_data, (bytes, bytearray)) or not image_data:
        raise ValueError("请选择 PNG、JPG 或 WebP 背景图片")
    return _validate_theme_image_stream(BytesIO(bytes(image_data)))


@dataclass(frozen=True)
class PreparedThemeAsset:
    """A validated, browser-efficient representation of one uploaded image."""

    content_type: str
    image_data: bytes
    width: int
    height: int


def _prepare_validated_theme_stream(source: BinaryIO) -> PreparedThemeAsset:
    """Decode a previously validated stream and create the stored rendition."""

    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ValueError("服务器缺少背景图片处理组件，请先更新网页部署包") from exc

    base = None
    try:
        source.seek(0)
        with Image.open(source) as source_image:
            # Dimensions have already been bounded before this decode. Keep
            # actual pixel work off the async request loop (the caller invokes
            # this function through ``run_in_threadpool``).
            source_image.load()
            transposed = ImageOps.exif_transpose(source_image)
            # ``exif_transpose`` can return its input. Do not retain a view
            # backed by the file that is about to leave this context manager.
            base = source_image.copy() if transposed is source_image else transposed
    except Exception as exc:
        raise ValueError("背景图片文件损坏或格式不受支持") from exc

    try:
        if base.mode not in {"RGB", "RGBA"}:
            converted = base.convert("RGBA" if "transparency" in base.info else "RGB")
            base.close()
            base = converted

        # Fall back to a smaller rendition for noisy images, ensuring the
        # stored payload remains below MySQL MEDIUMBLOB while keeping normal
        # high-resolution images crisp. This bound applies to the generated
        # cache, not the selected source image.
        for edge, quality in ((MAX_THEME_RENDER_DIMENSION, 84), (2_560, 82), (1_920, 80)):
            longest_edge = max(base.width, base.height)
            if longest_edge <= edge:
                rendered = base
            else:
                scale = edge / longest_edge
                rendered = base.resize(
                    (
                        max(1, round(base.width * scale)),
                        max(1, round(base.height * scale)),
                    ),
                    Image.Resampling.LANCZOS,
                )
            try:
                payload = BytesIO()
                rendered.save(payload, format="WEBP", quality=quality, method=4)
                encoded = payload.getvalue()
                width, height = rendered.width, rendered.height
            finally:
                if rendered is not base:
                    rendered.close()
            if len(encoded) <= MAX_THEME_STORED_BYTES:
                return PreparedThemeAsset(
                    content_type="image/webp",
                    image_data=encoded,
                    width=width,
                    height=height,
                )
        raise ValueError("背景图片无法压缩到可安全保存的大小")
    finally:
        if base is not None:
            base.close()


def prepare_theme_asset_stream(source: BinaryIO) -> PreparedThemeAsset:
    """Validate a seekable image stream and encode one compact WebP asset.

    This is the upload-path entry point. It deliberately works against the
    multipart temporary file instead of first calling ``await upload.read()``,
    so the original source is never duplicated in application memory.
    """

    _validate_theme_image_stream(source)
    return _prepare_validated_theme_stream(source)


def prepare_theme_asset(image_data: bytes) -> PreparedThemeAsset:
    """Bytes-based compatibility entry point for non-HTTP service callers."""

    _validate_image_bytes(image_data)
    return _prepare_validated_theme_stream(BytesIO(bytes(image_data)))


def store_theme_asset(
    db: Session,
    user: User,
    image_data: bytes,
    *,
    is_shared: bool = False,
) -> PortalThemeAsset:
    """Validate and store an owned image, de-duplicating rendered assets."""

    prepared = prepare_theme_asset(image_data)
    return store_prepared_theme_asset(db, user, prepared, is_shared=is_shared)


def store_prepared_theme_asset(
    db: Session,
    user: User,
    prepared: PreparedThemeAsset,
    *,
    is_shared: bool = False,
) -> PortalThemeAsset:
    """Store a prepared rendition without doing image CPU work in the DB path."""

    content_type = prepared.content_type
    width = prepared.width
    height = prepared.height
    image_data = prepared.image_data
    digest = sha256(image_data).hexdigest()
    existing = db.scalar(
        select(PortalThemeAsset).where(
            PortalThemeAsset.owner_user_id == user.id,
            PortalThemeAsset.sha256 == digest,
        )
    )
    if existing is not None:
        existing.is_shared = bool(is_shared)
        return existing

    owned_count = db.scalar(
        select(func.count())
        .select_from(PortalThemeAsset)
        .where(PortalThemeAsset.owner_user_id == user.id)
    ) or 0
    if owned_count >= MAX_THEME_ASSETS_PER_USER:
        raise ValueError(f"每个账号最多保存 {MAX_THEME_ASSETS_PER_USER} 张背景图片")

    asset = PortalThemeAsset(
        owner_user_id=user.id,
        content_type=content_type,
        image_data=bytes(image_data),
        sha256=digest,
        size_bytes=len(image_data),
        width=width,
        height=height,
        is_shared=bool(is_shared),
    )
    db.add(asset)
    db.flush()
    return asset


def save_theme_preference(
    db: Session,
    user: User,
    mode: object,
    day_asset_id: object,
    night_asset_id: object,
) -> PortalThemePreference:
    """Persist a safe day/night selection for a single user."""

    normalized_mode = str(mode or "auto").strip().lower()
    if normalized_mode not in THEME_MODES:
        raise ValueError("外观模式无效")
    day_id = _asset_id(day_asset_id)
    night_id = _asset_id(night_asset_id)
    day_asset = get_accessible_asset(db, user, day_id)
    night_asset = get_accessible_asset(db, user, night_id)
    if day_id is not None and day_asset is None:
        raise ValueError("白天背景图片不存在、已设为私有或无权使用")
    if night_id is not None and night_asset is None:
        raise ValueError("夜间背景图片不存在、已设为私有或无权使用")

    preference = db.get(PortalThemePreference, user.id)
    if preference is None:
        preference = PortalThemePreference(user_id=user.id)
        db.add(preference)
    preference.mode = normalized_mode
    preference.day_asset_id = day_asset.id if day_asset is not None else None
    preference.night_asset_id = night_asset.id if night_asset is not None else None
    return preference


def set_asset_sharing(
    db: Session, actor: User, asset_id: object, shared: bool
) -> PortalThemeAsset:
    """Change one asset's opt-in sharing flag with strict ownership checks."""

    normalized_id = _asset_id(asset_id)
    asset = (
        db.scalar(
            select(PortalThemeAsset)
            .options(defer(PortalThemeAsset.image_data))
            .where(PortalThemeAsset.id == normalized_id)
        )
        if normalized_id
        else None
    )
    if asset is None:
        raise ValueError("背景图片不存在")
    if not actor.is_admin and asset.owner_user_id != actor.id:
        raise ValueError("只能修改自己上传的背景图片")
    asset.is_shared = bool(shared)
    return asset


def delete_theme_asset(
    db: Session, actor: User, asset_id: object
) -> PortalThemeAsset:
    """Delete one background asset after checking ownership or admin access."""

    normalized_id = _asset_id(asset_id)
    asset = (
        db.scalar(
            select(PortalThemeAsset).where(PortalThemeAsset.id == normalized_id)
        )
        if normalized_id
        else None
    )
    if asset is None:
        raise ValueError("背景图片不存在")
    if not actor.is_admin and asset.owner_user_id != actor.id:
        raise ValueError("只能删除自己上传的背景图片")

    # Clear both slots explicitly before deleting.  This keeps the operation
    # portable across SQLite/MySQL and avoids leaving a selected asset ID in
    # databases where foreign-key SET NULL is not enabled.
    preferences = db.scalars(
        select(PortalThemePreference).where(
            (PortalThemePreference.day_asset_id == asset.id)
            | (PortalThemePreference.night_asset_id == asset.id)
        )
    )
    for preference in preferences:
        if preference.day_asset_id == asset.id:
            preference.day_asset_id = None
        if preference.night_asset_id == asset.id:
            preference.night_asset_id = None
    db.delete(asset)
    db.flush()
    return asset


def preference_view(db: Session, user: User) -> dict[str, Any]:
    """Data required by the appearance form, without image bytes."""

    preference = db.get(PortalThemePreference, user.id)
    context = get_theme_context(db, user)
    return {
        **deepcopy(context),
        "day_asset_id": preference.day_asset_id if preference else None,
        "night_asset_id": preference.night_asset_id if preference else None,
    }
