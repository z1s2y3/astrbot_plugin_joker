import json
import os
import time
import hashlib
import hmac
import threading
import tempfile
import re
from pathlib import Path
try:  # AstrBot supplies these in production.
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
    from astrbot.api import logger
except ImportError:  # Keep persistence helpers usable by standalone tools/tests.
    try:
        from ..runtime_compat import get_astrbot_data_path, logger
    except ImportError:  # Imported as a top-level ``auth`` package.
        from runtime_compat import get_astrbot_data_path, logger

# 授权系统原子操作锁（防止并发激活同一卡密）
_auth_lock = threading.RLock()
_AUTH_GROUP_ID_RE = re.compile(r"^\d{5,20}$")
# Do not interpret an absent/newly-created empty auth file as an intentional
# removal of every portal group.  Once a real group authorization has existed,
# this marker allows a later deliberate empty authorization set to reconcile.
_PORTAL_SYNC_INITIALIZED_MARKER = ".portal_auth_initialized"
# The portal can only fingerprint cards that use one of these generated
# formats.  Keep this grammar explicit instead of treating every ``JK...``
# string as a card: historical/manual records occasionally contain an
# incomplete value with that prefix.
_LICENSE_KEY_RE = re.compile(
    r"^JK(?:-1-\d{13}[0-9A-F]{12}|[1-9]\d*-\d{13}[0-9A-F]{12}|[1-9]\d*\d{13}[0-9A-F]{12})$"
)


def _atomic_write_json(path: str | Path, data: dict) -> None:
    """Write JSON transactionally so a failed write does not corrupt auth data."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False, dir=target.parent,
            prefix=f".{target.name}.", suffix=".tmp"
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(data, temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, target)
    except Exception:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def _license_signature(auth_key: str, timestamp: int, group_id: str, index: int) -> str:
    payload = f"{timestamp}:{group_id}:{index}".encode("utf-8")
    return hmac.new(auth_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()[:12].upper()


def get_plugin_data_path():
    data_path = Path(get_astrbot_data_path()) / "plugin_data" / "auth"
    data_path.mkdir(parents=True, exist_ok=True)
    return data_path

def get_auth_file():
    return os.path.join(get_plugin_data_path(), "auth.json")


def _portal_sync_marker_file() -> Path:
    """Return the marker used to qualify an empty auth source as intentional."""

    return get_plugin_data_path() / _PORTAL_SYNC_INITIALIZED_MARKER


def _portal_sync_source_initialized() -> bool:
    try:
        return _portal_sync_marker_file().is_file()
    except OSError:
        return False


def _mark_portal_sync_source_initialized(data: dict | None = None) -> None:
    """Record that this source has held a real authorization at least once.

    Merely generating unused card keys must not make a brand-new Windows
    install's empty group list authoritative.  A marker is created only after
    a non-empty authorization set is observed; it is never removed when the
    last group is deliberately revoked.
    """

    groups = data.get("groups", {}) if isinstance(data, dict) else {}
    if not isinstance(groups, dict) or not groups:
        return
    try:
        _portal_sync_marker_file().touch(exist_ok=True)
    except OSError:
        # Conservative fallback: the next empty read remains non-authoritative.
        logger.warning("Unable to persist authorization sync initialization marker")

def get_bindings_file():
    return os.path.join(get_plugin_data_path(), "bindings.json")

def get_group_settings_file():
    return os.path.join(get_plugin_data_path(), "group_settings.json")

# ==================== 卡密相关 ====================

def generate_license_key(auth_key: str, days: int, group_id: str = "", index: int = 0) -> str:
    timestamp = int(time.time() * 1000)
    unique = _license_signature(auth_key, timestamp, str(group_id or "").strip(), index)
    return f"JK{days}-{timestamp}{unique}".upper()


def is_supported_license_key_format(key: str) -> bool:
    """Return whether *key* has a generated card shape, without trusting it.

    Signature verification is still required before an existing card is kept
    as a group's portal credential.  This helper only prevents malformed
    historical ``JK`` markers from being treated as a real card.
    """
    normalized = str(key or "").strip().upper()
    return len(normalized) <= 96 and bool(_LICENSE_KEY_RE.fullmatch(normalized))

def verify_license_key(key: str, auth_key: str, group_id: str = "") -> dict:
    """Verify a generated card against its intended group when one is supplied.

    Older/current cards are generated with an empty group component and are
    therefore valid in the group where they are first activated.  Some callers
    also generate a card for a concrete group.  Trying that concrete ID first
    and then the legacy empty ID keeps both forms usable without allowing a
    group-bound card to activate another group.
    """
    key = str(key or "").strip().upper()
    if not is_supported_license_key_format(key):
        return {"valid": False, "error": "无效的卡密格式"}

    try:
        remaining = key[2:]

        if '-'in remaining:
            # 永久卡密格式: JK-1-{timestamp}{unique}
            if remaining.startswith('-1-'):
                days = -1
                rest = remaining[3:]
            else:
                parts = remaining.split('-', 1)
                days_str = parts[0]
                rest = parts[1]

                if not days_str.isdigit():
                    return {"valid": False, "error": "无效的卡密格式"}

                days = int(days_str)
            timestamp_str = rest[:13]
            unique = rest[13:]
            if len(unique) != 12 or not timestamp_str.isdigit():
                return {"valid": False, "error": "无效的卡密格式"}
        else:
            if len(remaining) < 25:
                return {"valid": False, "error": "无效的卡密格式"}

            unique = remaining[-12:]
            timestamp_str = remaining[-25:-12]
            days_str = remaining[:-25]

            if not days_str.isdigit():
                return {"valid": False, "error": "无效的卡密格式"}
            if not timestamp_str.isdigit():
                return {"valid": False, "error": "无效的卡密格式"}

            days = int(days_str)

        timestamp = int(timestamp_str)
        requested_group_id = str(group_id or "").strip()
        candidate_group_ids = [requested_group_id]
        if requested_group_id:
            # Generated cards traditionally carry no group ID.  Preserve that
            # behavior while still accepting cards explicitly generated for
            # the activation group.
            candidate_group_ids.append("")
        for signed_group_id in candidate_group_ids:
            for i in range(100):
                expected_unique = _license_signature(auth_key, timestamp, signed_group_id, i)
                if hmac.compare_digest(unique, expected_unique):
                    return {
                        "valid": True,
                        "days": days,
                        "timestamp": timestamp,
                        "group_bound": bool(signed_group_id),
                    }
        return {"valid": False, "error": "卡密验证失败"}
    except Exception as e:
        logger.error(f"验证卡密失败: {e}")
        return {"valid": False, "error": "卡密验证失败"}

def load_auth_data_with_health() -> tuple[dict, bool]:
    """Load authorization data without mistaking a read failure for no groups.

    The normal public loader retains its historical empty-default behavior for
    all existing callers.  The sync loop uses the health bit so a damaged or
    temporarily locked ``auth.json`` cannot publish an authoritative empty
    group list and erase the portal's current Windows projection.
    """
    auth_file = get_auth_file()
    with _auth_lock:
        try:
            if os.path.exists(auth_file):
                with open(auth_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    logger.error("加载授权数据失败: JSON 格式不正确")
                    return {"groups": {}, "unused_keys": [], "used_keys": {}}, False
                healthy = isinstance(data.get("groups", {}), dict)
                if not healthy:
                    logger.error("加载授权数据失败: groups 字段格式不正确")
                if healthy:
                    _mark_portal_sync_source_initialized(data)
                # An existing, valid empty auth file from a fresh installation
                # is indistinguishable from a recreated file.  It becomes
                # authoritative only after this source previously contained a
                # real group authorization.
                complete = bool(healthy and (
                    isinstance(data.get("groups", {}), dict)
                    and (data.get("groups") or _portal_sync_source_initialized())
                ))
                return data, complete
        except Exception as e:
            logger.error(f"加载授权数据失败: {e}")
            return {"groups": {}, "unused_keys": [], "used_keys": {}}, False
    # Missing auth.json is normal on first run, but must never be sent as an
    # authoritative empty group list to the portal.
    return {"groups": {}, "unused_keys": [], "used_keys": {}}, False


def load_auth_data() -> dict:
    """Load authorization data, preserving the legacy empty-default API."""
    data, _healthy = load_auth_data_with_health()
    return data

def save_auth_data(data: dict):
    auth_file = get_auth_file()
    with _auth_lock:
        try:
            _atomic_write_json(auth_file, data)
            _mark_portal_sync_source_initialized(data)
        except Exception as e:
            logger.error(f"保存授权数据失败: {e}")

def add_unused_key(key: str):
    add_unused_keys([key])


def add_unused_keys(keys: list[str]) -> None:
    """Append keys as one read-modify-write transaction."""
    with _auth_lock:
        data = load_auth_data()
        unused_keys = data.setdefault("unused_keys", [])
        new_keys = [key for key in keys if key not in unused_keys]
        if new_keys:
            unused_keys.extend(new_keys)
            save_auth_data(data)

def remove_unused_key(key: str):
    with _auth_lock:
        data = load_auth_data()
        if key in data["unused_keys"]:
            data["unused_keys"].remove(key)
            save_auth_data(data)

def get_unused_keys() -> list:
    data = load_auth_data()
    return data.get("unused_keys", [])

def get_used_keys() -> dict:
    data = load_auth_data()
    return data.get("used_keys", {})

def mark_key_used(key: str, group_id: str, user_id: str = "") -> bool:
    """激活卡密（原子操作，保证线程安全）"""
    with _auth_lock:  # 获取锁
        data = load_auth_data()
        
        # 再次检查（double-check locking）
        if key not in data["unused_keys"]:
            return False
        
        now = int(time.time())

        # 解析天数（统一处理三种格式）
        try:
            # 永久卡密格式：JK-1-{timestamp}{unique}
            if key.startswith("JK-1-"):
                days = -1
            # 新格式：JK{days}-{timestamp}{unique}
            elif '-' in key[2:]:  # 从 'JK' 之后检查 '-'
                parts = key[2:].split('-', 1)
                days_str = parts[0]
                if not days_str.isdigit():
                    return False
                days = int(days_str)
            # 旧格式（已弃用，但兼容）：JK{days}{timestamp}{unique}
            else:
                # ``days`` 和 13 位毫秒时间戳之间没有分隔符，不能从左
                # 向右读取全部数字，否则会把时间戳误当作授权天数。验证函数
                # 已按尾部的 ``13 位时间戳 + 12 位签名`` 解析，这里必须使用
                # 相同规则，才能让旧卡密激活后的授权记录保持正确的到期时间。
                remaining = key[2:]
                if len(remaining) < 26:
                    return False
                days_str = remaining[:-25]
                timestamp_str = remaining[-25:-12]
                if not days_str.isdigit() or not timestamp_str.isdigit():
                    return False
                days = int(days_str)
        except (ValueError, IndexError):
            return False

        # 计算过期时间
        if days == -1:
            expire = -1
        else:
            expire = now + (days * 86400)
        
        # 原子操作：移除和添加
        data["unused_keys"].remove(key)
        data["used_keys"][key] = {
            "group_id": group_id,
            "activated_at": now,
            "expire": expire,
            "user_id": user_id
        }
        
        # 更新群组授权信息
        if group_id not in data["groups"]:
            data["groups"][group_id] = {}
        data["groups"][group_id] = {
            "expire": expire,
            "activated_at": now,
            "key": key,
            "days": days,
            "user_id": user_id
        }
        
        # 原子保存
        save_auth_data(data)
        return True


def activate_unused_license_key(
    key: str,
    group_id: str,
    user_id: str,
    days: int,
) -> dict:
    """Atomically consume an issued card and update its group authorization.

    Card signatures are deterministic, so signature verification alone cannot
    prove that a card is still issued.  In particular, an administrator may
    remove an unused card before somebody attempts to activate it.  Keeping
    the membership check and the read-modify-write under the same lock makes
    deletion effective immediately and prevents two groups from consuming the
    same card concurrently.

    ``days`` must come from :func:`verify_license_key`; this helper deliberately
    does not accept a raw, unverified card as proof of entitlement.
    """
    normalized_key = str(key or "").strip().upper()
    normalized_group_id = str(group_id or "").strip()
    try:
        normalized_days = int(days)
    except (TypeError, ValueError):
        return {"status": "invalid"}
    if not normalized_key or not normalized_group_id or (
        normalized_days != -1 and normalized_days <= 0
    ):
        return {"status": "invalid"}

    with _auth_lock:
        data = load_auth_data()
        unused_keys = data.get("unused_keys")
        used_keys = data.get("used_keys")
        groups = data.get("groups")
        if not isinstance(unused_keys, list) or not isinstance(used_keys, dict) or not isinstance(groups, dict):
            logger.error("授权数据格式无效，拒绝激活卡密")
            return {"status": "data_invalid"}

        if normalized_key in used_keys:
            return {"status": "used"}
        if normalized_key not in unused_keys:
            # This covers an administrator-deleted, expired-cleanup, or
            # otherwise revoked card.  It is intentionally distinct from a
            # malformed card so the user gets an actionable message.
            return {"status": "deleted"}

        now = int(time.time())
        existing = groups.get(normalized_group_id)
        existing_info = existing if isinstance(existing, dict) else {}
        existing_expire = _get_expire_int(existing_info)
        try:
            old_days = int(existing_info.get("days", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            old_days = 0

        # Do not burn a valid, unused renewal card when the group has already
        # been made permanent.  This retains the historical behaviour, while
        # still requiring that the submitted card exists in the issued list.
        if existing_expire == -1:
            return {"status": "already_permanent"}

        if normalized_days == -1:
            expire = -1
            action = "upgraded" if normalized_group_id in groups else "activated"
        elif existing_expire > now:
            expire = existing_expire + normalized_days * 86400
            action = "extended"
        else:
            expire = now + normalized_days * 86400
            action = "reactivated" if normalized_group_id in groups else "activated"

        display_days = (
            -1
            if normalized_days == -1
            else old_days + normalized_days
            if existing_expire > now and old_days > 0
            else normalized_days
        )

        unused_keys.remove(normalized_key)
        used_keys[normalized_key] = {
            "group_id": normalized_group_id,
            "activated_at": now,
            "expire": expire,
            "user_id": str(user_id or ""),
        }
        groups[normalized_group_id] = {
            "expire": expire,
            "activated_at": now,
            "key": normalized_key,
            "days": display_days,
            "user_id": str(user_id or ""),
        }
        try:
            _atomic_write_json(get_auth_file(), data)
        except Exception as exc:
            logger.error(f"保存卡密激活数据失败: {exc}")
            return {"status": "save_failed"}

        return {
            "status": action,
            "activated_at": now,
            "expire": expire,
            "days": display_days,
        }

def _revoke_groups_for_deleted_cards(data: dict, deleted_cards: set[str]) -> list[str]:
    """Remove only groups whose *current* authorization came from a deleted card.

    Direct owner grants use ``MANUAL_...`` as their source marker and are not
    license cards, so they intentionally survive card-list cleanup.  When a
    group was renewed with multiple cards, only the newest card stored in the
    group record controls the current authorization.
    """
    if not deleted_cards:
        return []
    groups = data.setdefault("groups", {})
    if not isinstance(groups, dict):
        return []
    revoked_groups = []
    for group_id, info in list(groups.items()):
        if not isinstance(info, dict):
            continue
        if str(info.get("key", "")) in deleted_cards:
            del groups[group_id]
            revoked_groups.append(str(group_id))
    return revoked_groups


def delete_all_keys() -> list[str]:
    """Delete every card and revoke groups currently backed by used cards."""
    with _auth_lock:
        data = load_auth_data()
        used_keys = data.get("used_keys", {})
        deleted_cards = set(used_keys) if isinstance(used_keys, dict) else set()
        data["unused_keys"] = []
        data["used_keys"] = {}
        revoked_groups = _revoke_groups_for_deleted_cards(data, deleted_cards)
        save_auth_data(data)
        return revoked_groups

def delete_used_keys() -> list[str]:
    """Delete used cards and revoke the groups whose active card was deleted."""
    with _auth_lock:
        data = load_auth_data()
        used_keys = data.get("used_keys", {})
        deleted_cards = set(used_keys) if isinstance(used_keys, dict) else set()
        data["used_keys"] = {}
        revoked_groups = _revoke_groups_for_deleted_cards(data, deleted_cards)
        save_auth_data(data)
        return revoked_groups

# ==================== 授权管理 ====================

def _get_expire_int(info: dict) -> int:
    """统一将 expire 转换为 int 防御 JSON 中被存为字符串的情况"""
    raw = info.get("expire", 0)
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0


def _normalize_auth_group_id(value: object) -> str:
    """Return a QQ group ID accepted by both the card and portal flows.

    Cards issued by owner commands are already activated for their target
    group.  Generating one for an arbitrary string would create a credential
    that the portal deliberately cannot claim, because the portal only accepts
    real QQ group IDs.  Keep this validation here as the shared, last-line
    guard for both initial issuance and old-grant migration.
    """
    group_id = str(value or "").strip()
    return group_id if _AUTH_GROUP_ID_RE.fullmatch(group_id) else ""


def _generate_unique_group_card(
    auth_key: str,
    days: int,
    group_id: str,
    used_keys: dict,
    unused_keys: list,
) -> str:
    """Return a non-colliding group-bound card while the auth lock is held."""
    timestamp = int(time.time() * 1000)
    for index in range(100):
        candidate = (
            f"JK{days}-{timestamp}"
            f"{_license_signature(auth_key, timestamp, group_id, index)}"
        ).upper()
        if candidate not in used_keys and candidate not in unused_keys:
            return candidate
    return ""


def _sync_current_card_expire(data: dict, group_id: str) -> None:
    """Keep the current card's audit record aligned with group expiry.

    The group record is authoritative for runtime access, but keeping its
    ``used_keys`` entry in step prevents a manually extended card from looking
    expired in owner/card views while the group is still active.
    """
    groups = data.get("groups")
    used_keys = data.get("used_keys")
    if not isinstance(groups, dict) or not isinstance(used_keys, dict):
        return
    info = groups.get(str(group_id))
    if not isinstance(info, dict):
        return
    key = str(info.get("key", "")).strip().upper()
    used_info = used_keys.get(key)
    if isinstance(used_info, dict) and str(used_info.get("group_id", "")) == str(group_id):
        used_info["expire"] = _get_expire_int(info)


def _is_valid_current_group_card(
    key: str, auth_key: str, group_id: str, used_info: object
) -> bool:
    """Check that a retained card is owned by this group and still verifiable.

    Older code used ``key.startswith('JK')`` here.  A malformed or token-signed
    card then remained in the local authorization record, while web sync
    correctly omitted its fingerprint.  The customer consequently saw a
    misleading "not synchronized" result.  Requiring the actual HMAC lets the
    caller safely issue a replacement card instead.
    """
    if (
        not isinstance(used_info, dict)
        or str(used_info.get("group_id", "")) != str(group_id)
        or not is_supported_license_key_format(key)
    ):
        return False
    return bool(verify_license_key(key, auth_key, group_id).get("valid"))

def authorize_group(group_id: str, days: int, operator_id: str = "") -> bool:
    with _auth_lock:
        group_id = str(group_id)
        data = load_auth_data()
        now = int(time.time())
        existing = data["groups"].get(group_id)

        if days == -1:
            # 永不过期
            expire = -1
            display_days = -1
        else:
            # 延长逻辑：已有授权且未过期则追加，否则从现在重新算
            if existing:
                cur = _get_expire_int(existing)
                if cur == -1:
                    expire = -1
                    display_days = -1
                else:
                    base = cur if cur > now else now
                    expire = base + (days * 86400)
                    if cur > now:
                        # 未过期：累加天数
                        old_days = existing.get("days", 0)
                        try:
                            display_days = int(old_days) + days if isinstance(old_days, int) and old_days > 0 else days
                        except (ValueError, TypeError):
                            display_days = days
                    else:
                        # 已过期：从现在重新计算，不累加旧天数
                        display_days = days
            else:
                expire = now + (days * 86400)
                display_days = days

        # Keep a previously activated card as the current group credential when
        # an older caller still uses this legacy helper.  Replacing it with a
        # MANUAL marker would make an already registered portal customer lose
        # its claimable card on an otherwise ordinary renewal.
        existing_key = str(existing.get("key", "")).strip().upper() if isinstance(existing, dict) else ""
        existing_used = data.get("used_keys", {}).get(existing_key) if isinstance(data.get("used_keys"), dict) else None
        has_current_card = (
            existing_key.startswith("JK")
            and isinstance(existing_used, dict)
            and str(existing_used.get("group_id", "")) == group_id
        )
        activated_at = existing.get("activated_at", now) if existing else now
        data["groups"][group_id] = {
            "expire": expire,
            "activated_at": activated_at,
            "key": existing_key if has_current_card else f"MANUAL_{group_id}_{now}",
            "days": display_days if days != -1 else -1,
            "user_id": (
                str(existing.get("user_id", existing_used.get("user_id", "")) or "")
                if has_current_card
                else operator_id
            )
        }
        _sync_current_card_expire(data, group_id)
        save_auth_data(data)
    logger.info(f"authorize_group 写入: group_id={group_id}, days={days}, expire={expire}, display_days={display_days if days != -1 else -1}")
    return True


def authorize_group_with_card(
    group_id: str,
    days: int,
    auth_key: str,
    operator_id: str = "",
) -> dict:
    """Authorize one group and atomically attach a claimable group card.

    ``authorize_group`` predates the web portal and records a
    ``MANUAL_<group>_<timestamp>`` source marker.  That marker intentionally is
    not a card, so the portal cannot safely use it to bind a customer account.
    The owner-facing manual grant commands use this function instead: the group
    becomes authorized immediately *and* receives a real, group-bound card in
    ``used_keys``.  It is already activated for that group, therefore the card
    can be entered directly on the web portal after the next Windows sync.

    The entire read/modify/write is protected by the same lock as normal card
    activation.  This prevents a browser-claimable card from being shown when
    the corresponding authorization record was not saved.
    """
    normalized_group_id = _normalize_auth_group_id(group_id)
    normalized_auth_key = str(auth_key or "").strip()
    try:
        normalized_days = int(days)
    except (TypeError, ValueError):
        return {"status": "invalid"}
    if (
        not normalized_group_id
        or not normalized_auth_key
        or (normalized_days != -1 and normalized_days <= 0)
    ):
        return {"status": "invalid"}

    with _auth_lock:
        data = load_auth_data()
        groups = data.get("groups")
        unused_keys = data.get("unused_keys")
        used_keys = data.get("used_keys")
        if not isinstance(groups, dict) or not isinstance(unused_keys, list) or not isinstance(used_keys, dict):
            logger.error("授权数据格式无效，拒绝创建群专属卡密")
            return {"status": "data_invalid"}

        now = int(time.time())
        existing = groups.get(normalized_group_id)
        existing_info = existing if isinstance(existing, dict) else {}
        existing_expire = _get_expire_int(existing_info)
        existing_key = str(existing_info.get("key", "")).strip().upper()

        # A permanent record must never be downgraded by a later timed manual
        # grant.  Legacy MANUAL records still fall through to issue a permanent
        # claimable card, while card-backed records are preserved below.
        if existing_expire == -1:
            expire = -1
            display_days = -1
            card_days = -1
            status = "already_permanent"
        elif normalized_days == -1:
            expire = -1
            display_days = -1
            card_days = -1
            status = "upgraded" if existing_info else "authorized"
        else:
            card_days = normalized_days
            if existing_info and existing_expire > now:
                expire = existing_expire + normalized_days * 86400
                old_days = existing_info.get("days", 0)
                try:
                    display_days = (
                        int(old_days) + normalized_days
                        if int(old_days) > 0
                        else normalized_days
                    )
                except (TypeError, ValueError):
                    display_days = normalized_days
                status = "extended"
            else:
                expire = now + normalized_days * 86400
                display_days = normalized_days
                status = "reactivated" if existing_info else "authorized"

        # Extending an already card-backed group must not replace the current
        # card identity.  The customer may already be registered on the portal
        # with its fingerprint; keeping that card avoids needless re-claiming
        # and keeps the deletion/revocation rule tied to the same credential.
        # Old manual records have no such key and take the new-card branch
        # below, which is how they become web-manageable for the first time.
        existing_used = used_keys.get(existing_key)
        if _is_valid_current_group_card(
            existing_key, normalized_auth_key, normalized_group_id, existing_used
        ):
            activated_at = int(existing_info.get("activated_at", now) or now)
            user_id = str(existing_info.get("user_id", existing_used.get("user_id", "")) or "")
            existing_used["expire"] = expire
            groups[normalized_group_id] = {
                "expire": expire,
                "activated_at": activated_at,
                "key": existing_key,
                "days": display_days,
                "user_id": user_id,
            }
            try:
                _atomic_write_json(get_auth_file(), data)
            except Exception as exc:
                logger.error("保存已有群卡密授权失败: %s", exc)
                return {"status": "save_failed"}
            return {
                "status": "already_permanent" if expire == -1 and existing_expire == -1 else status,
                "key": existing_key,
                "expire": expire,
                "days": display_days,
                "activated_at": activated_at,
                "created": False,
            }

        # Group-bound cards can only be verified in this group.  The index is
        # part of the HMAC input and removes the (rare) same-millisecond
        # collision when an owner grants the same group more than once quickly.
        key = _generate_unique_group_card(
            normalized_auth_key,
            card_days,
            normalized_group_id,
            used_keys,
            unused_keys,
        )
        if not key:
            logger.error("群专属卡密生成冲突过多: group_id=%s", normalized_group_id)
            return {"status": "key_collision"}

        # A broken historical card must not remain in the used-card list once
        # its replacement becomes authoritative.  Never delete an entry owned
        # by another group, even if a corrupt group record points at it.
        if (
            existing_key.startswith("JK")
            and isinstance(existing_used, dict)
            and str(existing_used.get("group_id", "")) == normalized_group_id
        ):
            used_keys.pop(existing_key, None)
        used_keys[key] = {
            "group_id": normalized_group_id,
            "activated_at": now,
            "expire": expire,
            "user_id": str(operator_id or ""),
        }
        groups[normalized_group_id] = {
            "expire": expire,
            "activated_at": now,
            "key": key,
            "days": display_days,
            "user_id": str(operator_id or ""),
        }
        try:
            _atomic_write_json(get_auth_file(), data)
        except Exception as exc:
            logger.error("保存群专属卡密授权失败: %s", exc)
            return {"status": "save_failed"}

    logger.info(
        "群组授权已绑定卡密: group_id=%s, days=%s, expire=%s, status=%s",
        normalized_group_id,
        normalized_days,
        expire,
        status,
    )
    return {
        "status": status,
        "key": key,
        "expire": expire,
        "days": display_days,
        "activated_at": now,
        "created": True,
    }


def ensure_group_authorization_card(
    group_id: str, auth_key: str, operator_id: str = ""
) -> dict:
    """Return the current group card, issuing one for an old manual grant.

    This is the non-destructive migration path for groups created before the
    portal existed.  It deliberately keeps the original expiry and does not
    grant extra days, unlike :func:`authorize_group_with_card`.  A caller can
    therefore repair an old ``MANUAL_...`` authorization and let the customer
    claim the group on the web without changing the commercial entitlement.
    """
    normalized_group_id = _normalize_auth_group_id(group_id)
    normalized_auth_key = str(auth_key or "").strip()
    if not normalized_group_id or not normalized_auth_key:
        return {"status": "invalid"}

    with _auth_lock:
        data = load_auth_data()
        groups = data.get("groups")
        unused_keys = data.get("unused_keys")
        used_keys = data.get("used_keys")
        if not isinstance(groups, dict) or not isinstance(unused_keys, list) or not isinstance(used_keys, dict):
            logger.error("授权数据格式无效，拒绝补发群专属卡密")
            return {"status": "data_invalid"}
        existing = groups.get(normalized_group_id)
        if not isinstance(existing, dict):
            return {"status": "missing"}

        now = int(time.time())
        expire = _get_expire_int(existing)
        existing_key = str(existing.get("key", "")).strip().upper()
        existing_used = used_keys.get(existing_key)
        if _is_valid_current_group_card(
            existing_key, normalized_auth_key, normalized_group_id, existing_used
        ):
            # Repair a historical inconsistency in the detail record while
            # preserving the card identity a customer has already registered.
            if existing_used.get("expire") != expire:
                existing_used["expire"] = expire
                try:
                    _atomic_write_json(get_auth_file(), data)
                except Exception as exc:
                    logger.error("保存已有群卡密授权失败: %s", exc)
                    return {"status": "save_failed"}
            return {
                "status": "existing",
                "key": existing_key,
                "expire": expire,
                "days": existing.get("days", -1 if expire == -1 else 0),
                "activated_at": int(existing.get("activated_at", now) or now),
                "created": False,
            }

        if expire == -1:
            card_days = -1
            display_days = -1
        else:
            try:
                display_days = int(existing.get("days", 0) or 0)
            except (TypeError, ValueError):
                display_days = 0
            if display_days <= 0:
                # Older manual records can lack a usable day count.  The card
                # is already activated, but its format still needs a positive
                # duration; use a bounded representation of remaining time.
                remaining_seconds = max(0, expire - now)
                display_days = max(1, (remaining_seconds + 86399) // 86400)
            card_days = display_days

        key = _generate_unique_group_card(
            normalized_auth_key,
            card_days,
            normalized_group_id,
            used_keys,
            unused_keys,
        )
        if not key:
            logger.error("补发群专属卡密冲突过多: group_id=%s", normalized_group_id)
            return {"status": "key_collision"}
        if (
            existing_key.startswith("JK")
            and isinstance(existing_used, dict)
            and str(existing_used.get("group_id", "")) == normalized_group_id
        ):
            used_keys.pop(existing_key, None)
        used_keys[key] = {
            "group_id": normalized_group_id,
            "activated_at": now,
            "expire": expire,
            "user_id": str(operator_id or ""),
        }
        groups[normalized_group_id] = {
            "expire": expire,
            "activated_at": now,
            "key": key,
            "days": display_days,
            "user_id": str(operator_id or ""),
        }
        try:
            _atomic_write_json(get_auth_file(), data)
        except Exception as exc:
            logger.error("保存补发群专属卡密失败: %s", exc)
            return {"status": "save_failed"}
    logger.info("已补发群专属卡密: group_id=%s, expire=%s", normalized_group_id, expire)
    return {
        "status": "issued",
        "key": key,
        "expire": expire,
        "days": display_days,
        "activated_at": now,
        "created": True,
    }

def deauthorize_group(group_id: str) -> bool:
    with _auth_lock:
        group_id = str(group_id)
        data = load_auth_data()
        if group_id not in data["groups"]:
            return False
        import traceback
        logger.warning(f"[JOKER] deauthorize_group 被调用: group_id={group_id}, "
                       f"原expire={data['groups'][group_id].get('expire')}, "
                       f"原days={data['groups'][group_id].get('days')}, "
                       f"调用栈:\n{''.join(traceback.format_stack(limit=8))}")
        del data["groups"][group_id]
        save_auth_data(data)
    return True

def is_group_authorized(group_id: str) -> bool:
    group_id = str(group_id)
    data = load_auth_data()
    if group_id not in data["groups"]:
        return False
    expire = _get_expire_int(data["groups"][group_id])
    if expire == -1:
        return True
    return expire > int(time.time())


def get_group_auth_state(group_id: str, now: int | None = None) -> str:
    """Return ``active``, ``expired``, or ``missing`` for a group authorization.

    ``is_group_authorized`` intentionally combines missing and expired records for
    the outbound gate. Callers that need to show an expiry notice must distinguish
    those two cases so groups that were never licensed remain silent.
    """
    data = load_auth_data()
    info = data.get("groups", {}).get(str(group_id))
    if not isinstance(info, dict):
        return "missing"

    expire = _get_expire_int(info)
    current_time = int(time.time()) if now is None else int(now)
    if expire == -1 or expire > current_time:
        return "active"
    return "expired"


def get_group_auth_info(group_id: str) -> dict:
    data = load_auth_data()
    info = data.get("groups", {}).get(str(group_id), {})
    if info:
        info["expire"] = _get_expire_int(info)
    return info

def add_auth_time(group_id: str, days: int) -> bool:
    with _auth_lock:
        group_id = str(group_id)
        data = load_auth_data()
        now = int(time.time())

        # days == -1 且群未授权  直接创建永久授权
        if group_id not in data["groups"]:
            if days == -1:
                data["groups"][group_id] = {
                    "expire": -1,
                    "activated_at": now,
                    "key": f"MANUAL_{group_id}_{now}",
                    "days": -1,
                    "user_id": ""
                }
                save_auth_data(data)
                return True
            return False

        expire = _get_expire_int(data["groups"][group_id])
        if days == -1:
            data["groups"][group_id]["expire"] = -1
            data["groups"][group_id]["days"] = -1
            _sync_current_card_expire(data, group_id)
            save_auth_data(data)
            return True
        if expire == -1:
            return True
        if expire < now:
            expire = now
        expire += days * 86400
        data["groups"][group_id]["expire"] = expire
        old_days = data["groups"][group_id].get("days", 0)
        try:
            data["groups"][group_id]["days"] = int(old_days) + days
        except (ValueError, TypeError):
            data["groups"][group_id]["days"] = days
        _sync_current_card_expire(data, group_id)
        save_auth_data(data)
        return True

def reduce_auth_time(group_id: str, days: int) -> bool:
    with _auth_lock:
        group_id = str(group_id)
        data = load_auth_data()
        if group_id not in data["groups"]:
            return False
        expire = _get_expire_int(data["groups"][group_id])
        if expire == -1:
            return False
        expire -= days * 86400
        if expire < int(time.time()):
            expire = int(time.time()) - 1
        data["groups"][group_id]["expire"] = expire
        old_days = data["groups"][group_id].get("days", 0)
        try:
            data["groups"][group_id]["days"] = max(0, int(old_days) - days)
        except (ValueError, TypeError):
            pass
        _sync_current_card_expire(data, group_id)
        save_auth_data(data)
        return True

def list_all_authorizations() -> dict:
    data = load_auth_data()
    return data.get("groups", {})


def list_all_authorizations_with_health() -> tuple[dict, bool]:
    """Return authorization groups plus whether the underlying read completed."""
    data, healthy = load_auth_data_with_health()
    groups = data.get("groups", {}) if isinstance(data, dict) else {}
    return (groups if isinstance(groups, dict) else {}), healthy

def list_expired_authorizations(days_expired: int = 0) -> dict:
    """列出已过期的授权群组
    Args:
        days_expired: 过期多少天后才算（0=刚过期即算）
    Returns:
        dict: {group_id: info} 已过期的群组
    """
    data = load_auth_data()
    groups = data.get("groups", {})
    now = int(time.time())
    threshold = now - (days_expired * 86400)
    return {
        gid: info for gid, info in groups.items()
        if int(info.get("expire", 0)) != -1 and int(info.get("expire", 0)) < threshold
    }

def delete_expired_authorizations(days_expired: int = 0) -> dict:
    """删除已过期的授权记录
    Args:
        days_expired: 过期多少天后才删除（0=刚过期即删除）
    Returns:
        dict: {"deleted": {group_id: info}, "kept": {group_id: info}}
              deleted=已删除的，kept=保留的（未过期或未达阈值）
    """
    with _auth_lock:
        data = load_auth_data()
        groups = data.get("groups", {})
        now = int(time.time())
        threshold = now - (days_expired * 86400)

        deleted = {}
        kept = {}
        for gid, info in list(groups.items()):
            raw_expire = info.get("expire", 0)
            try:
                expire = int(raw_expire)
            except (ValueError, TypeError):
                logger.warning(f"群 {gid} 的 expire 字段无效: {raw_expire!r}，跳过删除")
                kept[gid] = info
                continue
            if expire == -1:
                kept[gid] = info
            elif expire < threshold:
                logger.info(f"删除过期群组 {gid}: expire={expire} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire)) if expire > 0 else 'N/A'}), 阈值={threshold}")
                deleted[gid] = info
                del groups[gid]
            else:
                kept[gid] = info

        if deleted:
            data["groups"] = groups
            save_auth_data(data)

    return {"deleted": deleted, "kept": kept}

def delete_authorization(group_id: str) -> bool:
    """删除单个群组的授权记录"""
    with _auth_lock:
        group_id = str(group_id)
        data = load_auth_data()
        if group_id not in data.get("groups", {}):
            return False
        del data["groups"][group_id]
        save_auth_data(data)
    return True

def get_auth_statistics() -> dict:
    data = load_auth_data()
    groups = data.get("groups", {})
    now = int(time.time())
    
    total = len(groups)
    active = 0
    for info in groups.values():
        exp = _get_expire_int(info)
        if exp == -1 or exp > now:
            active += 1
    expired = total - active
    
    return {
        "total_groups": total,
        "active_groups": active,
        "expired_groups": expired,
        "unused_keys_count": len(data.get("unused_keys", [])),
        "used_keys_count": len(data.get("used_keys", {}))
    }

# ==================== 绑定管理 ====================

def load_bindings() -> dict:
    bindings_file = get_bindings_file()
    with _auth_lock:
        try:
            if os.path.exists(bindings_file):
                with open(bindings_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"加载绑定数据失败: {e}")
    return {}

def save_bindings(bindings: dict):
    bindings_file = get_bindings_file()
    with _auth_lock:
        try:
            _atomic_write_json(bindings_file, bindings)
        except Exception as e:
            logger.error(f"保存绑定数据失败: {e}")

def bind_server(group_id: str, server_id: str) -> bool:
    with _auth_lock:
        bindings = load_bindings()
        bindings[str(group_id)] = server_id
        save_bindings(bindings)
    return True

def unbind_server(group_id: str) -> bool:
    with _auth_lock:
        bindings = load_bindings()
        group_id = str(group_id)
        if group_id not in bindings:
            return False
        del bindings[group_id]
        save_bindings(bindings)
    return True

def get_group_binding(group_id: str) -> str:
    bindings = load_bindings()
    return bindings.get(str(group_id), "")

# ==================== 设置管理 ====================

def load_group_settings() -> dict:
    settings_file = get_group_settings_file()
    with _auth_lock:
        try:
            if os.path.exists(settings_file):
                with open(settings_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"加载群组设置失败: {e}")
    return {}

def save_group_settings(settings: dict):
    settings_file = get_group_settings_file()
    with _auth_lock:
        try:
            _atomic_write_json(settings_file, settings)
        except Exception as e:
            logger.error(f"保存群组设置失败: {e}")

def get_group_setting(group_id: str, key: str, default=None):
    settings = load_group_settings()
    return settings.get(str(group_id), {}).get(key, default)

def set_group_setting(group_id: str, key: str, value):
    with _auth_lock:
        settings = load_group_settings()
        group_id = str(group_id)
        if group_id not in settings:
            settings[group_id] = {}
        settings[group_id][key] = value
        save_group_settings(settings)
