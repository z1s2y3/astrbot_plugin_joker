"""Linux-owned settings for music sources that require a service bridge."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from .models import PortalSetting
from .settings import Settings
from .websocket_settings import _decrypt_token, _encrypt_token, _read_settings_fallback


MUSIC_SOURCE_SERVICES_SETTING_KEY = "music_source_services"
MUSIC_CARD_DEFAULT_SIGN_URL = "https://oiapi.net/api/QQMusicJSONArk"
MUSIC_PLATFORM_KEYS = ("qq", "163", "kugou", "soda")
DEFAULT_MUSIC_SOURCE_SERVICES = {
    "platform_cookies": {
        provider: {"cookie": ""} for provider in MUSIC_PLATFORM_KEYS
    },
    "soda": {
        "enabled": False,
        "base_url": "",
    },
    "music_card_signer": {
        "enabled": True,
        "sign_url": MUSIC_CARD_DEFAULT_SIGN_URL,
    },
}


def _as_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes"}
    return bool(value)


def _normalize_base_url(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(url) > 2048
    ):
        raise ValueError("汽水音乐服务地址必须是有效的 http(s) 地址，不能包含账号、查询参数或片段")
    return url


def _normalize_sign_url(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(url) > 2048
    ):
        raise ValueError("音乐名片签名地址必须是有效的 http(s) 地址，不能包含账号、查询参数或片段")
    return url


def _normalize_cookie(value: object, label: str = "Cookie") -> str:
    cookie = str(value or "").strip()
    if len(cookie) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in cookie):
        raise ValueError(f"{label} 不能超过 8192 个字符或包含控制字符")
    return cookie


def get_music_source_services(
    db,
    settings: Settings | None = None,
    *,
    include_secrets: bool = True,
) -> dict[str, Any]:
    setting = db.get(PortalSetting, MUSIC_SOURCE_SERVICES_SETTING_KEY)
    raw = setting.value_json if setting and isinstance(setting.value_json, Mapping) else {}
    soda_raw = raw.get("soda") if isinstance(raw.get("soda"), Mapping) else {}
    signer_raw = (
        raw.get("music_card_signer")
        if isinstance(raw.get("music_card_signer"), Mapping)
        else {}
    )
    cookie_sources = (
        raw.get("platform_cookies")
        if isinstance(raw.get("platform_cookies"), Mapping)
        else {}
    )
    signer_defaults = DEFAULT_MUSIC_SOURCE_SERVICES["music_card_signer"]
    current = settings or _read_settings_fallback()
    try:
        base_url = _normalize_base_url(soda_raw.get("base_url", ""))
    except ValueError:
        base_url = ""
    api_key = _decrypt_token(soda_raw.get("api_key_ciphertext"), current)
    try:
        sign_url = _normalize_sign_url(
            signer_raw.get("sign_url", signer_defaults["sign_url"])
        )
    except ValueError:
        sign_url = ""
    signer_key = _decrypt_token(signer_raw.get("api_key_ciphertext"), current)
    signer_cookie = _decrypt_token(signer_raw.get("cookie_ciphertext"), current)
    platform_cookies: dict[str, dict[str, Any]] = {}
    for provider in MUSIC_PLATFORM_KEYS:
        provider_raw = (
            cookie_sources.get(provider)
            if isinstance(cookie_sources.get(provider), Mapping)
            else {}
        )
        cookie = _decrypt_token(provider_raw.get("cookie_ciphertext"), current)
        platform_cookies[provider] = {"cookie_configured": bool(cookie)}
        if include_secrets:
            platform_cookies[provider]["cookie"] = cookie
    result: dict[str, Any] = {
        "platform_cookies": platform_cookies,
        "soda": {
            "enabled": _as_bool(soda_raw.get("enabled", False)),
            "base_url": base_url,
            "api_key_configured": bool(api_key),
        },
        "music_card_signer": {
            "enabled": _as_bool(
                signer_raw.get("enabled", signer_defaults["enabled"])
            ),
            "sign_url": sign_url,
            "api_key_configured": bool(signer_key),
            "cookie_configured": bool(signer_cookie),
        },
    }
    if include_secrets:
        result["soda"]["api_key"] = api_key
        result["music_card_signer"]["api_key"] = signer_key
        result["music_card_signer"]["cookie"] = signer_cookie
    return result


def save_music_source_services(
    db,
    *,
    soda_enabled: object,
    soda_base_url: object,
    soda_api_key: object = "",
    signer_enabled: object = False,
    signer_url: object = "",
    signer_api_key: object = "",
    platform_cookies: object = None,
    signer_cookie: object = "",
    settings: Settings | None = None,
    operator_id: int | None = None,
) -> dict[str, Any]:
    current = settings or _read_settings_fallback()
    if current is None:
        raise ValueError("网页加密配置不可用，请检查 session secret")
    enabled = _as_bool(soda_enabled)
    base_url = _normalize_base_url(soda_base_url)
    if enabled and not base_url:
        raise ValueError("启用汽水音乐前请填写服务地址")
    api_key = str(soda_api_key or "").strip()
    if len(api_key) > 512:
        raise ValueError("汽水音乐服务密钥不能超过 512 个字符")
    setting = db.get(PortalSetting, MUSIC_SOURCE_SERVICES_SETTING_KEY)
    previous = setting.value_json if setting and isinstance(setting.value_json, Mapping) else {}
    previous_soda = previous.get("soda") if isinstance(previous.get("soda"), Mapping) else {}
    previous_signer = (
        previous.get("music_card_signer")
        if isinstance(previous.get("music_card_signer"), Mapping)
        else {}
    )
    signer_enabled_value = _as_bool(signer_enabled)
    normalized_sign_url = _normalize_sign_url(signer_url)
    if signer_enabled_value and not normalized_sign_url:
        raise ValueError("启用音乐名片签名服务前请填写服务地址")
    signer_key = str(signer_api_key or "").strip()
    if len(signer_key) > 512:
        raise ValueError("音乐名片签名服务密钥不能超过 512 个字符")
    submitted_cookies = platform_cookies if isinstance(platform_cookies, Mapping) else {}
    previous_cookies = (
        previous.get("platform_cookies")
        if isinstance(previous.get("platform_cookies"), Mapping)
        else {}
    )
    platform_cookie_payload: dict[str, Any] = {}
    for provider in MUSIC_PLATFORM_KEYS:
        cookie = _normalize_cookie(
            submitted_cookies.get(provider, ""),
            f"{provider} 音乐 Cookie",
        )
        previous_provider = (
            previous_cookies.get(provider)
            if isinstance(previous_cookies.get(provider), Mapping)
            else {}
        )
        provider_payload: dict[str, Any] = {}
        if cookie:
            provider_payload["cookie_ciphertext"] = _encrypt_token(cookie, current)
        elif previous_provider.get("cookie_ciphertext"):
            provider_payload["cookie_ciphertext"] = str(
                previous_provider["cookie_ciphertext"]
            )
        platform_cookie_payload[provider] = provider_payload
    submitted_signer_cookie = _normalize_cookie(signer_cookie, "音乐名片签名服务 Cookie")
    soda_payload: dict[str, Any] = {"enabled": enabled, "base_url": base_url}
    if api_key:
        soda_payload["api_key_ciphertext"] = _encrypt_token(api_key, current)
    elif previous_soda.get("api_key_ciphertext"):
        soda_payload["api_key_ciphertext"] = str(previous_soda["api_key_ciphertext"])
    signer_payload: dict[str, Any] = {
        "enabled": signer_enabled_value,
        "sign_url": normalized_sign_url,
    }
    if signer_key:
        signer_payload["api_key_ciphertext"] = _encrypt_token(signer_key, current)
    elif previous_signer.get("api_key_ciphertext"):
        signer_payload["api_key_ciphertext"] = str(
            previous_signer["api_key_ciphertext"]
        )
    if submitted_signer_cookie:
        signer_payload["cookie_ciphertext"] = _encrypt_token(
            submitted_signer_cookie, current
        )
    elif previous_signer.get("cookie_ciphertext"):
        signer_payload["cookie_ciphertext"] = str(
            previous_signer["cookie_ciphertext"]
        )
    payload = {
        "platform_cookies": platform_cookie_payload,
        "soda": soda_payload,
        "music_card_signer": signer_payload,
    }
    if setting is None:
        db.add(
            PortalSetting(
                key=MUSIC_SOURCE_SERVICES_SETTING_KEY,
                value_json=payload,
                updated_by=operator_id,
            )
        )
    else:
        setting.value_json = payload
        setting.updated_by = operator_id
    return get_music_source_services(db, current)


def test_music_card_signer(value: object, *, timeout: float = 10.0) -> dict[str, Any]:
    """Check the configured signer contract without exposing its response."""

    raw = value if isinstance(value, Mapping) else {}
    signer = (
        raw.get("music_card_signer")
        if isinstance(raw.get("music_card_signer"), Mapping)
        else raw
    )
    sign_url = _normalize_sign_url(signer.get("sign_url", ""))
    if not _as_bool(signer.get("enabled", False)):
        return {"success": False, "code": "signer_disabled", "message": "请先启用音乐名片签名服务。"}
    if not sign_url:
        return {"success": False, "code": "signer_url_missing", "message": "请先填写音乐名片签名地址。"}
    api_key = str(signer.get("api_key", "") or "").strip()
    signer_cookie = str(signer.get("cookie", "") or "").strip()
    headers = {
        "User-Agent": "Joker-Portal/1.11",
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if signer_cookie:
        headers["Cookie"] = signer_cookie
    test_params = {
        "url": "https://example.com/joker-test.mp3",
        "song": "晴天",
        "singer": "周杰伦",
        "cover": "https://example.com/joker-test.jpg",
        "jump": "https://y.qq.com/n/ryqq/songDetail/1",
        "format": "qq",
    }
    test_body = {
        "type": "qq",
        "id": "1",
        "title": "Joker 测试歌曲",
        "artist": "Joker",
        "jump_url": test_params["jump"],
        "music_url": test_params["url"],
        "preview_url": test_params["cover"],
    }

    def _is_signed_payload(payload: object) -> bool:
        candidate = payload
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
        if not isinstance(candidate, Mapping) or not candidate:
            return False
        if candidate.get("code") in (0, "0", False):
            return False
        nested = candidate.get("data")
        if isinstance(nested, (Mapping, list, str)) and nested not in ({}, [], ""):
            return _is_signed_payload(nested)
        return bool(
            candidate.get("meta")
            or candidate.get("app")
            or candidate.get("config")
        )

    client_timeout = httpx.Timeout(max(1.0, min(float(timeout), 30.0)), connect=5.0)
    try:
        with httpx.Client(timeout=client_timeout, headers=headers, follow_redirects=False) as client:
            last_error: Exception | None = None
            payload: object = None
            for method in ("get", "post"):
                try:
                    if method == "get":
                        response = client.get(sign_url, params=test_params)
                    else:
                        response = client.post(sign_url, json=test_body)
                    response.raise_for_status()
                    payload = response.json()
                    if _is_signed_payload(payload):
                        break
                    raise ValueError("响应不是签名音乐名片")
                except (httpx.HTTPError, ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
                    last_error = exc
            if last_error is not None and not _is_signed_payload(payload):
                raise last_error
        return {
            "success": True,
            "code": "signer_connected",
            "message": "音乐名片签名服务已响应，Windows 将在下一次认证心跳获取配置。",
        }
    except Exception as exc:
        return {
            "success": False,
            "code": "signer_connection_failed",
            "message": f"音乐名片签名服务连接失败：{type(exc).__name__}。",
        }


def test_soda_music_source(value: object, *, timeout: float = 10.0) -> dict[str, Any]:
    """Check the bridge health endpoint without exposing its credentials."""
    raw = value if isinstance(value, Mapping) else {}
    soda = raw.get("soda") if isinstance(raw.get("soda"), Mapping) else raw
    base_url = _normalize_base_url(soda.get("base_url", ""))
    if not _as_bool(soda.get("enabled", False)):
        return {"success": False, "code": "soda_disabled", "message": "请先启用汽水音乐服务。"}
    if not base_url:
        return {"success": False, "code": "soda_url_missing", "message": "请先填写汽水音乐服务地址。"}
    api_key = str(soda.get("api_key", "") or "").strip()
    platform_cookies = raw.get("platform_cookies") if isinstance(raw.get("platform_cookies"), Mapping) else {}
    soda_cookie_raw = platform_cookies.get("soda") if isinstance(platform_cookies.get("soda"), Mapping) else {}
    soda_cookie = str(soda_cookie_raw.get("cookie", "") or "").strip()
    headers = {"User-Agent": "Joker-Portal/1.11"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if soda_cookie:
        headers["Cookie"] = soda_cookie
    client_timeout = httpx.Timeout(max(1.0, min(float(timeout), 30.0)), connect=5.0)
    try:
        with httpx.Client(timeout=client_timeout, headers=headers, follow_redirects=False) as client:
            response = client.get(f"{base_url}/api/capabilities")
            response.raise_for_status()
        return {
            "success": True,
            "code": "soda_connected",
            "message": "汽水音乐服务已响应，Windows 将在下一次心跳获取此配置。",
        }
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        return {
            "success": False,
            "code": "soda_connection_failed",
            "message": f"汽水音乐服务连接失败：{type(exc).__name__}。",
        }


__all__ = [
    "DEFAULT_MUSIC_SOURCE_SERVICES",
    "MUSIC_CARD_DEFAULT_SIGN_URL",
    "MUSIC_PLATFORM_KEYS",
    "MUSIC_SOURCE_SERVICES_SETTING_KEY",
    "get_music_source_services",
    "save_music_source_services",
    "test_music_card_signer",
    "test_soda_music_source",
]
