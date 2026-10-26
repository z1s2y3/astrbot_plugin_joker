"""Small, bounded entertainment helpers for group chat commands.

The module deliberately keeps no QQ/AstrBot dependencies.  Runtime handlers
can therefore validate and exercise the random games without touching the
network or a group configuration file.  Cooldowns are process-local by
design: entertainment is best-effort chat state, not an accounting record.
"""

from __future__ import annotations

import secrets
import json
import logging
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from urllib.parse import quote, urlsplit

import httpx


logger = logging.getLogger(__name__)


MAX_DICE_SIDES = 100_000
MAX_RANDOM_OPTIONS = 50
MAX_OPTION_LENGTH = 120
MAX_COOLDOWN_SECONDS = 86_400
MAX_DAILY_LIKES = 20
# OneBot v11 documents ``send_like.times`` as at most ten per request.
# Keep the larger daily quota for accounts that support it, but never submit
# an out-of-range batch to the platform.
MAX_LIKES_PER_API_CALL = 10
MAX_MUSIC_POOL_ITEMS = 50
MAX_MUSIC_KEYWORD_LENGTH = 80
MAX_MUSIC_SEARCH_RESULTS = 8
# Used when a scheduled random-music task has no fixed song or artist.
DEFAULT_RANDOM_MUSIC_KEYWORDS = (
    "周杰伦", "林俊杰", "陈奕迅", "薛之谦", "张杰", "邓紫棋", "热门歌曲"
)
MUSIC_SELECTION_TTL_SECONDS = 600
MAX_MUSIC_SELECTIONS = 2_000
MUSIC_SEARCH_TIMEOUT_SECONDS = 8
MUSIC_PLAYBACK_TIMEOUT_SECONDS = 6
MUSIC_SIGN_TIMEOUT_SECONDS = 4
MUSIC_SIGN_URL = "https://oiapi.net/api/QQMusicJSONArk/"
QQ_MUSIC_COVER_URL = "https://y.gtimg.cn/mediastyle/global/img/logo_qqmusic.png"
QQ_MUSIC_PLAY_URL = "https://meting.mikus.ink/api?server=tencent&type=url&id={}"
QQ_MUSIC_PLAYBACK_API = "https://api.vkeys.cn/v2/music/tencent"
VIDEO_SEARCH_ENDPOINTS = (
    ("哔哩哔哩", "https://search.bilibili.com/all?keyword={query}"),
    ("YouTube", "https://www.youtube.com/results?search_query={query}"),
    ("YouTube 国内备用", "https://piped.video/results?search_query={query}"),
    ("YouTube 备用", "https://yewtu.be/search?q={query}"),
)
MUSIC_PROVIDER_ORDER = ("qq", "163")
MUSIC_PROVIDER_LABELS = {
    "qq": "QQ音乐",
    "163": "网易云音乐",
    "kugou": "酷狗音乐",
    "soda": "汽水音乐",
}
_MUSIC_PROVIDER_ALIASES = {
    "qq": "qq",
    "qqmusic": "qq",
    "qq音乐": "qq",
    "腾讯": "qq",
    "腾讯音乐": "qq",
    "tencent": "qq",
    "163": "163",
    "网易": "163",
    "网易云": "163",
    "网易云音乐": "163",
    "netease": "163",
    "neteasecloudmusic": "163",
    "kugou": "kugou",
    "酷狗": "kugou",
    "酷狗音乐": "kugou",
    "kg": "kugou",
    "soda": "soda",
    "汽水": "soda",
    "汽水音乐": "soda",
    "auto": "auto",
}
_cooldown_lock = threading.RLock()
_cooldowns: dict[tuple[str, str], float] = {}
_like_lock = threading.RLock()
_daily_likes: dict[tuple[str, str, str, str], tuple[str, int]] = {}
_music_selection_lock = threading.RLock()
_music_selections: dict[tuple[str, str], tuple[float, str, tuple["MusicTrack", ...]]] = {}


@dataclass(frozen=True)
class MusicTrack:
    """A provider-neutral track that can be encoded as a QQ music card."""

    provider: str
    music_id: str
    title: str
    artist: str = ""
    album: str = ""
    jump_url: str = ""
    music_url: str = ""
    preview_url: str = ""


def _as_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes"}
    return bool(value)


def _normalize_service_url(value: object) -> str:
    """Return a safe configured source endpoint, or empty when unusable."""
    url = str(value or "").strip().rstrip("/")
    if not url or len(url) > 2048:
        return ""
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return url


def normalize_music_source_services(value: object) -> dict[str, dict[str, object]]:
    """Normalize Linux-owned source service settings received by heartbeat."""
    raw = value if isinstance(value, Mapping) else {}
    cookie_sources = (
        raw.get("platform_cookies")
        if isinstance(raw.get("platform_cookies"), Mapping)
        else {}
    )
    soda_raw = raw.get("soda") if isinstance(raw.get("soda"), Mapping) else {}
    api_key = str(soda_raw.get("api_key", "") or "").strip()[:512]
    signer_raw = (
        raw.get("music_card_signer")
        if isinstance(raw.get("music_card_signer"), Mapping)
        else {}
    )
    signer_key = str(signer_raw.get("api_key", "") or "").strip()[:512]
    signer_cookie = str(signer_raw.get("cookie", "") or "").strip()[:8192]
    platform_cookies: dict[str, str] = {}
    for provider in ("qq", "163", "kugou", "soda"):
        provider_value = cookie_sources.get(provider)
        if isinstance(provider_value, Mapping):
            provider_value = provider_value.get("cookie", "")
        platform_cookies[provider] = str(provider_value or "").strip()[:8192]
    return {
        "platform_cookies": platform_cookies,
        "soda": {
            "enabled": _as_bool(soda_raw.get("enabled", False)),
            "base_url": _normalize_service_url(soda_raw.get("base_url", "")),
            "api_key": api_key,
        },
        "music_card_signer": {
            "enabled": _as_bool(signer_raw.get("enabled", False)),
            "sign_url": _normalize_service_url(signer_raw.get("sign_url", "")),
            "api_key": signer_key,
            "cookie": signer_cookie,
        },
    }


def _music_provider_headers(
    source_services: object, provider: str
) -> dict[str, str]:
    """Build per-provider headers without leaking cookies across sources."""
    configured = normalize_music_source_services(source_services)
    headers: dict[str, str] = {}
    cookie = str(configured.get("platform_cookies", {}).get(provider, "") or "")
    if cookie:
        headers["Cookie"] = cookie
    if provider == "soda":
        api_key = str(configured["soda"].get("api_key", "") or "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _is_http_url(value: object) -> bool:
    parsed = urlsplit(str(value or "").strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _is_playable_audio_url(value: object) -> bool:
    """Reject catalogue pages that OneBot would otherwise treat as audio URLs."""

    if not _is_http_url(value):
        return False
    parsed = urlsplit(str(value).strip())
    host = parsed.netloc.casefold().split(":", 1)[0]
    path = parsed.path.casefold()
    # NetEase returns playable URLs from its own host, for example
    # ``/song/media/outer/url``.  Only reject known catalogue paths.
    if host == "music.163.com":
        return (
            path.startswith("/song/media/")
            or path.startswith("/song/media/outer/")
            or path.startswith("/api/song/enhance/")
        )
    return host not in {
        "y.qq.com",
        "c.y.qq.com",
        "www.kugou.com",
        "m.kugou.com",
    }


def build_video_search_links(keyword: object) -> tuple[tuple[str, str], ...]:
    """Build bounded video search links with external fallback frontends."""
    query_text = str(keyword or "").strip()[:MAX_MUSIC_KEYWORD_LENGTH]
    if not query_text:
        raise ValueError("请输入视频关键词")
    query = quote(query_text, safe="")
    return tuple(
        (label, template.format(query=query))
        for label, template in VIDEO_SEARCH_ENDPOINTS
    )


def flip_coin() -> str:
    """Return a fair, non-persistent coin result for group games."""
    return secrets.choice(("正面", "反面"))


def draw_fortune() -> str:
    """Return a short bounded fortune message without storing user data."""
    return secrets.choice(
        (
            "大吉：适合推进计划，今天会有好消息。",
            "吉：稳步行动，主动沟通会更顺利。",
            "中吉：先完成最重要的一件事，再处理其他安排。",
            "小吉：保持耐心，晚些时候会出现转机。",
            "平：按自己的节奏来，今天适合整理和复盘。",
        )
    )


def draw_answer_book() -> str:
    """Return a short, neutral answer for the group's question game."""
    return secrets.choice(
        (
            "可以，先把最重要的一步做完。",
            "暂时别急，等更多信息再决定。",
            "值得尝试，但先设好边界。",
            "今天适合行动，不必过度准备。",
            "换个角度再看，答案会更清楚。",
            "不建议现在决定，晚些时候再问一次。",
            "大概率没问题，记得和同伴确认。",
            "听听自己的第一反应，再做选择。",
        )
    )


def _today_key() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def claim_daily_likes(
    group_id: object,
    user_id: object,
    target_id: object,
    requested: object = MAX_DAILY_LIKES,
    daily_limit: object = MAX_DAILY_LIKES,
    bot_id: object = "",
) -> tuple[int, int, int]:
    """Reserve today's like quota and return ``(granted, used, limit)``.

    The reservation is rolled back when the platform rejects the request, so
    a transient OneBot failure never consumes a member's daily quota.
    """
    try:
        limit = max(1, min(int(daily_limit), MAX_DAILY_LIKES))
    except (TypeError, ValueError, OverflowError):
        limit = MAX_DAILY_LIKES
    try:
        amount = max(1, min(int(requested), MAX_DAILY_LIKES))
    except (TypeError, ValueError, OverflowError):
        amount = MAX_DAILY_LIKES
    key = (
        str(bot_id or ""),
        str(group_id or ""),
        str(user_id or ""),
        str(target_id or ""),
    )
    today = _today_key()
    with _like_lock:
        # Expired entries are cheap to prune and keep the process-local map
        # bounded even when a group receives many distinct targets.
        for entry, (day, _count) in list(_daily_likes.items()):
            if day != today:
                _daily_likes.pop(entry, None)
        day, used = _daily_likes.get(key, (today, 0))
        if day != today:
            used = 0
        granted = min(amount, max(0, limit - used))
        if granted:
            used += granted
            _daily_likes[key] = (today, used)
        return granted, used, limit


def rollback_daily_likes(
    group_id: object,
    user_id: object,
    target_id: object,
    amount: object,
    bot_id: object = "",
) -> None:
    """Release a previously reserved quota after a failed platform call."""
    try:
        count = max(0, int(amount))
    except (TypeError, ValueError, OverflowError):
        return
    if not count:
        return
    key = (
        str(bot_id or ""),
        str(group_id or ""),
        str(user_id or ""),
        str(target_id or ""),
    )
    with _like_lock:
        day, used = _daily_likes.get(key, (_today_key(), 0))
        _daily_likes[key] = (day, max(0, used - count))


def like_action_confirmed(result: object) -> bool:
    """Accept the success shapes returned by OneBot/NapCat like endpoints."""
    if result is True:
        return True
    if result is None or result is False:
        return False
    if isinstance(result, int) and not isinstance(result, bool):
        return result == 0
    if isinstance(result, str):
        return result.strip().lower() in {"ok", "success", "succeeded", "0"}

    payloads = [result]
    if isinstance(result, Mapping):
        payloads.extend(result.get(key) for key in ("data", "result"))
    else:
        payloads.extend(getattr(result, key, None) for key in ("data", "result"))

    for payload in payloads:
        if payload is None:
            continue
        if isinstance(payload, Mapping):
            success = payload.get("success")
            retcode = payload.get("retcode")
            status = str(payload.get("status", "") or "").strip().lower()
        else:
            success = getattr(payload, "success", None)
            retcode = getattr(payload, "retcode", None)
            status = str(getattr(payload, "status", "") or "").strip().lower()

        if (
            success is False
            or retcode not in (None, 0, "0")
            or status in {"failed", "failure", "error"}
        ):
            return False
        if success is True or status in {"ok", "success", "succeeded"} or retcode in (0, "0"):
            return True
    return False


def like_target_is_friend(result: object, target_id: object) -> bool | None:
    """Return whether a target appears in the bot's friend list.

    ``send_like`` is a friend-like API in NapCat.  Some builds return an
    apparently successful empty response for a non-friend target, so the
    caller must check the friend list before reserving quota or reporting
    success.  ``None`` means the friend list response could not be trusted.
    """
    normalized_target = str(target_id or "").strip()
    if not normalized_target or result is None:
        return None

    pending: list[object] = [result]
    visited: set[int] = set()
    records: list[object] | None = None
    while pending:
        payload = pending.pop(0)
        marker = id(payload)
        if marker in visited:
            continue
        visited.add(marker)
        if isinstance(payload, Mapping):
            status = str(payload.get("status", "") or "").strip().lower()
            retcode = payload.get("retcode")
            if status in {"failed", "failure", "error"} or retcode not in (None, 0, "0"):
                return None
            for key in ("data", "result"):
                nested = payload.get(key)
                if isinstance(nested, Mapping):
                    pending.append(nested)
                elif isinstance(nested, (list, tuple)):
                    records = list(nested)
            if isinstance(payload.get("data"), (list, tuple)):
                records = list(payload["data"])
            if isinstance(payload.get("result"), (list, tuple)):
                records = list(payload["result"])
        elif isinstance(payload, (list, tuple)):
            records = list(payload)

    if records is None:
        return None
    for record in records:
        if not isinstance(record, Mapping):
            continue
        candidate = next(
            (
                record.get(key)
                for key in ("user_id", "userId", "uin", "uid", "qq", "id")
                if record.get(key) not in (None, "")
            ),
            None,
        )
        if str(candidate or "").strip() == normalized_target:
            return True
    return False


def like_profile_state(result: object, bot_id: object) -> tuple[bool, dict[str, int]]:
    """Extract whether a bot appears in a target's profile-like record.

    NapCat exposes the target profile's voters through ``get_profile_like``.
    The action may return the raw data object or an outer OneBot envelope, so
    walk both shapes and keep the total count when it is available.
    """
    normalized_bot_id = str(bot_id or "").strip()
    if not normalized_bot_id or not isinstance(result, Mapping):
        return False, {}

    pending: list[object] = [result]
    visited: set[int] = set()
    found = False
    counts: dict[str, int] = {}
    while pending:
        payload = pending.pop(0)
        marker = id(payload)
        if marker in visited:
            continue
        visited.add(marker)
        if isinstance(payload, Mapping):
            for key in ("data", "result"):
                nested = payload.get(key)
                if isinstance(nested, Mapping):
                    pending.append(nested)
            section = payload.get("voteInfo")
            if not isinstance(section, Mapping):
                continue
            for count_key in ("total_count", "today_count", "new_count", "new_nearby_count"):
                value = section.get(count_key)
                try:
                    if value is not None:
                        numeric_value = max(0, int(value))
                        counts[count_key] = max(counts.get(count_key, 0), numeric_value)
                except (TypeError, ValueError, OverflowError):
                    pass
            users = section.get("userInfos")
            if not isinstance(users, (list, tuple)):
                continue
            for user in users:
                if not isinstance(user, Mapping):
                    continue
                candidate = next(
                    (
                        user.get(key)
                        for key in ("uin", "user_id", "userId", "qq")
                        if user.get(key) not in (None, "")
                    ),
                    None,
                )
                if candidate is not None and str(candidate).strip() == normalized_bot_id:
                    found = True
    return found, counts


def like_profile_confirmed(
    before: object,
    after: object,
    bot_id: object,
) -> bool:
    """Require the current bot to newly appear in the target's like record."""
    before_found, before_counts = like_profile_state(before, bot_id)
    after_found, after_counts = like_profile_state(after, bot_id)
    if not after_found:
        return False
    if not before_found:
        return True
    return any(
        after_counts.get(key, 0) > before_counts.get(key, 0)
        for key in set(before_counts) | set(after_counts)
    )


def _tracks_from_qq(
    payload: dict, *, limit: int = MAX_MUSIC_SEARCH_RESULTS
) -> list[MusicTrack]:
    items = (((payload.get("data") or {}).get("song") or {}).get("list") or [])
    tracks: list[MusicTrack] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        music_id = item.get("songid") or item.get("id")
        title = str(item.get("songname") or item.get("name") or "").strip()
        if not music_id or not title:
            continue
        song_mid = str(item.get("songmid") or music_id).strip()
        # The old songDetail URL is a web page and cannot be used as the
        # audio field of a QQ share card.  QQ's share page plus the public
        # playback redirect keeps the card playable when NapCat's signer is
        # unavailable.
        jump_url = (
            "https://i.y.qq.com/v8/playsong.html?platform=11&"
            "appshare=android_qq&appversion=10060507&songmid="
            f"{song_mid}&type=0&appsongtype=1&_wv=1&source=qq&ADTAG=qfshare"
        )
        singers = item.get("singer") or []
        artist = ", ".join(
            str(singer.get("name") or "").strip()
            for singer in singers
            if isinstance(singer, dict) and singer.get("name")
        )
        tracks.append(
            MusicTrack(
                provider="qq",
                music_id=str(music_id),
                title=title[:120],
                artist=artist[:120],
                album=str(item.get("albumname") or "").strip()[:120],
                jump_url=jump_url,
                music_url=QQ_MUSIC_PLAY_URL.format(str(music_id)),
                preview_url=(
                    f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{item.get('albummid')}.jpg"
                    if item.get("albummid")
                    else QQ_MUSIC_COVER_URL
                ),
            )
        )
        if len(tracks) >= limit:
            break
    return tracks


def _track_from_qq(payload: dict) -> MusicTrack | None:
    """Return the first QQ track for callers that only need one result."""
    tracks = _tracks_from_qq(payload, limit=1)
    return tracks[0] if tracks else None


def _tracks_from_netease(
    payload: dict, *, limit: int = MAX_MUSIC_SEARCH_RESULTS
) -> list[MusicTrack]:
    items = (((payload.get("result") or {}).get("songs")) or [])
    tracks: list[MusicTrack] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        music_id = item.get("id")
        title = str(item.get("name") or "").strip()
        if not music_id or not title:
            continue
        artists = item.get("artists") or []
        artist = ", ".join(
            str(artist_item.get("name") or "").strip()
            for artist_item in artists
            if isinstance(artist_item, dict) and artist_item.get("name")
        )
        album = item.get("album") or {}
        music_id_text = str(music_id)
        album_picture = ""
        if isinstance(album, dict):
            album_picture = str(album.get("picUrl") or "").strip()
            if not album_picture and album.get("picId"):
                album_picture = f"https://p2.music.126.net/{album.get('picId')}/{album.get('picId')}.jpg"
        tracks.append(
            MusicTrack(
                provider="163",
                music_id=music_id_text,
                title=title[:120],
                artist=artist[:120],
                album=str(album.get("name") or "").strip()[:120]
                if isinstance(album, dict)
                else "",
                jump_url=f"https://music.163.com/#/song?id={music_id_text}",
                music_url=f"https://music.163.com/song/media/outer/url?id={music_id_text}.mp3",
                preview_url=album_picture,
            )
        )
        if len(tracks) >= limit:
            break
    return tracks


def _track_from_netease(payload: dict) -> MusicTrack | None:
    """Return the first NetEase track for callers that only need one result."""
    tracks = _tracks_from_netease(payload, limit=1)
    return tracks[0] if tracks else None


def _tracks_from_kugou(
    payload: dict, *, limit: int = MAX_MUSIC_SEARCH_RESULTS
) -> list[MusicTrack]:
    """Parse Kugou's public mobile search response into neutral tracks."""
    data = payload.get("data") if isinstance(payload, dict) else {}
    items = data.get("info") if isinstance(data, dict) else []
    tracks: list[MusicTrack] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        music_id = str(item.get("hash") or item.get("FileHash") or "").strip()
        title = str(
            item.get("songname") or item.get("name") or item.get("filename") or ""
        ).strip()
        if not music_id or not title:
            continue
        title = re.sub(r"\s*-\s*[^-]+$", "", title).strip() or title
        artist = str(item.get("singername") or item.get("singer") or "").strip()
        album = str(item.get("album_name") or item.get("albumname") or "").strip()
        cover = str(
            item.get("img") or item.get("album_img_9") or item.get("album_img") or ""
        ).strip()
        tracks.append(
            MusicTrack(
                provider="kugou",
                music_id=music_id[:160],
                title=title[:120],
                artist=artist[:120],
                album=album[:120],
                jump_url=f"https://www.kugou.com/mixsong/{quote(music_id, safe='')}.html",
                preview_url=cover,
            )
        )
        if len(tracks) >= limit:
            break
    return tracks


def _soda_items(payload: object) -> list[dict]:
    """Find common song-list shapes used by self-hosted Soda API bridges."""
    def find(candidate: object) -> list[dict]:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if not isinstance(candidate, Mapping):
            return []
        if any(key in candidate for key in ("id", "track_id", "song_id")):
            return [dict(candidate)]
        for key in ("songs", "items", "results", "list", "data", "result"):
            found = find(candidate.get(key))
            if found:
                return found
        return []

    return find(payload)


def _soda_names(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return str(value.get("name") or value.get("title") or "").strip()
    if isinstance(value, list):
        return ", ".join(
            name
            for name in (_soda_names(item) for item in value)
            if name
        )
    return ""


def _tracks_from_soda(
    payload: object, *, limit: int = MAX_MUSIC_SEARCH_RESULTS
) -> list[MusicTrack]:
    """Parse a Soda-compatible bridge response without assuming one vendor."""
    tracks: list[MusicTrack] = []
    for item in _soda_items(payload):
        music_id = str(
            item.get("id") or item.get("track_id") or item.get("song_id") or ""
        ).strip()
        title = str(
            item.get("title") or item.get("name") or item.get("song_name") or ""
        ).strip()
        if not music_id or not title:
            continue
        album_value = item.get("album") or item.get("al") or {}
        album = _soda_names(album_value)
        artist = _soda_names(
            item.get("artists") or item.get("artist") or item.get("author") or item.get("ar")
        )
        jump_url = str(
            item.get("share_url") or item.get("jump_url") or item.get("song_url") or ""
        ).strip()
        audio_url = str(
            item.get("music_url") or item.get("audio_url") or item.get("play_url") or ""
        ).strip()
        image = str(
            item.get("cover") or item.get("cover_url") or item.get("image") or item.get("pic") or ""
        ).strip()
        tracks.append(
            MusicTrack(
                provider="soda",
                music_id=music_id[:160],
                title=title[:120],
                artist=artist[:120],
                album=album[:120],
                jump_url=jump_url if _is_http_url(jump_url) else "",
                music_url=audio_url if _is_http_url(audio_url) else "",
                preview_url=image if _is_http_url(image) else "",
            )
        )
        if len(tracks) >= limit:
            break
    return tracks


def normalize_music_providers(providers: object = None) -> tuple[str, ...]:
    """Return the enabled built-in music sources in the requested order."""
    if providers is None:
        return MUSIC_PROVIDER_ORDER
    if isinstance(providers, str):
        raw_values = re.split(r"[\s,，;；|]+", providers.strip())
    elif isinstance(providers, (list, tuple, set)):
        raw_values = list(providers)
    else:
        raise ValueError("音乐源平台格式无效")

    normalized: list[str] = []
    for raw_value in raw_values:
        value = str(raw_value or "").strip().casefold().replace(" ", "")
        if not value:
            continue
        provider = _MUSIC_PROVIDER_ALIASES.get(value)
        if provider == "auto":
            for default_provider in MUSIC_PROVIDER_ORDER:
                if default_provider not in normalized:
                    normalized.append(default_provider)
            continue
        if provider is None:
            raise ValueError("音乐源平台仅支持 QQ音乐、网易云音乐、酷狗音乐、汽水音乐")
        if provider not in normalized:
            normalized.append(provider)
    if not normalized:
        raise ValueError("请至少选择一个音乐源平台")
    return tuple(normalized)


def music_provider_label(provider: object) -> str:
    """Return a human-readable source label without exposing endpoint names."""
    return MUSIC_PROVIDER_LABELS.get(str(provider or "").strip().lower(), "未知音乐源")


def resolve_music_search_request(
    argument: object,
    *,
    providers: object = None,
    default_provider: object = None,
) -> tuple[str, tuple[str, ...]]:
    """Parse one explicit source, otherwise use the group's default source."""
    enabled = normalize_music_providers(providers)
    preferred = str(default_provider or "").strip().lower()
    if preferred not in enabled:
        preferred = enabled[0]
    query = _clean_text(argument, limit=MAX_MUSIC_KEYWORD_LENGTH)
    if not query:
        raise ValueError("请输入歌曲名称，例如 /点歌 晴天")
    source_token, separator, remainder = query.partition(" ")
    provider = _MUSIC_PROVIDER_ALIASES.get(
        source_token.casefold().replace(" ", "")
    )
    if provider and provider != "auto":
        song_name = _clean_text(remainder, limit=MAX_MUSIC_KEYWORD_LENGTH)
        if not separator or not song_name:
            raise ValueError("请选择歌曲名称，例如 /点歌 网易云 晴天")
        if provider not in enabled:
            raise ValueError(f"{music_provider_label(provider)}未在本群音乐源中启用")
        return song_name, (provider,)
    # A search result list must be from one provider.  Aggregating QQ and
    # NetEase candidates made `/选歌` indexes ambiguous and allowed a broken
    # signer to return a card for a different platform.
    return query, (preferred,)


async def search_music_candidates(
    keyword: str,
    *,
    limit: int = MAX_MUSIC_SEARCH_RESULTS,
    providers: object = None,
    source_services: object = None,
) -> list[MusicTrack]:
    """Search only the music sources enabled for the current group."""
    query = _clean_text(keyword, limit=80)
    if not query:
        raise ValueError("请输入歌曲名称，例如 /点歌 晴天")
    enabled_providers = normalize_music_providers(providers)
    try:
        result_limit = max(1, min(int(limit), MAX_MUSIC_SEARCH_RESULTS))
    except (TypeError, ValueError, OverflowError):
        result_limit = MAX_MUSIC_SEARCH_RESULTS
    # Split the candidate budget across the sources selected in this group.
    # A failed source never prevents the other selected sources from replying.
    provider_limit = max(1, (result_limit + len(enabled_providers) - 1) // len(enabled_providers))
    endpoints = {
        "qq": (
            "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
            {"format": "json", "p": 1, "n": provider_limit, "w": query},
            _tracks_from_qq,
        ),
        "163": (
            "https://music.163.com/api/search/get/web",
            {"s": query, "type": 1, "offset": 0, "limit": provider_limit, "total": "true"},
            _tracks_from_netease,
        ),
        "kugou": (
            "https://mobilecdn.kugou.com/api/v3/search/song",
            {"format": "json", "keyword": query, "page": 1, "pagesize": provider_limit},
            _tracks_from_kugou,
        ),
    }
    configured_services = normalize_music_source_services(source_services)
    errors: list[str] = []
    tracks: list[MusicTrack] = []
    seen: set[tuple[str, str]] = set()
    timeout = httpx.Timeout(MUSIC_SEARCH_TIMEOUT_SECONDS, connect=3.0)
    headers = {"User-Agent": "Joker-QQ-Admin/1.11"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for name in enabled_providers:
            request_headers = _music_provider_headers(configured_services, name)
            if name == "soda":
                soda = configured_services["soda"]
                base_url = str(soda.get("base_url", "") or "")
                if not soda.get("enabled") or not base_url:
                    errors.append("汽水音乐: 后台服务未配置")
                    continue
                url = f"{base_url}/search"
                params = {"keyword": query, "limit": provider_limit}
                parser = _tracks_from_soda
            else:
                url, params, parser = endpoints[name]
            try:
                response = await client.get(url, params=params, headers=request_headers)
                response.raise_for_status()
                provider_tracks = parser(response.json(), limit=provider_limit)
                if not provider_tracks:
                    errors.append(f"{music_provider_label(name)}: 未找到歌曲")
                    continue
                for track in provider_tracks:
                    identity = (track.provider, track.music_id)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    tracks.append(track)
            except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
                errors.append(f"{music_provider_label(name)}: {type(exc).__name__}")
    if tracks:
        return tracks[:result_limit]
    raise RuntimeError("点歌接口暂时不可用或未找到歌曲（" + "；".join(errors) + "）")


async def search_music(keyword: str, provider: str = "auto") -> MusicTrack:
    """Return one result from a selected source, retaining the legacy API."""
    providers = None if str(provider or "auto").strip().casefold() == "auto" else provider
    return (await search_music_candidates(keyword, limit=1, providers=providers))[0]


def _response_json(response: object) -> object:
    try:
        parsed = response.json()
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            raise ValueError("音乐源返回格式无效：响应为空")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.match(r"^[^(]*\((.*)\)\s*;?\s*$", text, re.DOTALL)
            if not match:
                raise ValueError(
                    "音乐源返回格式无效：" + text[:120].replace("\n", " ")
                )
            return json.loads(match.group(1))

    # Some signers return a JSON document as a quoted/plain response string
    # even though the HTTP client considers the response JSON-decoded.
    if isinstance(parsed, str):
        text = parsed.strip()
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
    return parsed


async def resolve_music_playback(
    track: MusicTrack,
    *,
    source_services: object = None,
) -> MusicTrack:
    """Fill optional playback metadata before a card is sent to QQ.

    Search must remain useful even when an upstream's playback endpoint is
    unavailable, so this deliberately returns the original candidate on any
    failed lookup.  The command layer will then use its normal signed-card or
    text-link fallback path.
    """
    if not isinstance(track, MusicTrack):
        raise ValueError("歌曲信息无效")
    provider = track.provider.strip().lower()
    if provider not in {"qq", "kugou", "soda"}:
        return track
    try:
        timeout = httpx.Timeout(MUSIC_PLAYBACK_TIMEOUT_SECONDS, connect=2.5)
        headers = {"User-Agent": "Joker-QQ-Admin/1.11"}
        headers.update(_music_provider_headers(source_services, provider))
        if provider == "qq":
            song_mid_match = re.search(
                r"(?:songmid=|song/)([^&/?#\s]+)",
                str(track.jump_url or ""),
            )
            song_mid = str(song_mid_match.group(1) if song_mid_match else "").strip()
            if not song_mid:
                return track
            url = QQ_MUSIC_PLAYBACK_API
            params = {"mid": song_mid}
        elif provider == "kugou":
            url = "https://wwwapi.kugou.com/yy/index.php"
            params = {"r": "play/getdata", "hash": track.music_id}
        else:
            soda = normalize_music_source_services(source_services)["soda"]
            base_url = str(soda.get("base_url", "") or "")
            if not soda.get("enabled") or not base_url:
                return track
            url = f"{base_url}/song/detail"
            params = {"track_id": track.music_id}
        async with httpx.AsyncClient(
            timeout=timeout, headers=headers, follow_redirects=True
        ) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = _response_json(response)
        if provider == "qq":
            data = payload.get("data") if isinstance(payload, Mapping) else {}
            if not isinstance(data, Mapping):
                return track
            audio_url = str(data.get("url") or "").strip()
            cover = str(data.get("cover") or "").strip()
            return replace(
                track,
                music_url=audio_url if _is_http_url(audio_url) else track.music_url,
                preview_url=cover if _is_http_url(cover) else track.preview_url,
                title=str(data.get("song") or track.title)[:120],
                artist=str(data.get("singer") or track.artist)[:120],
                album=str(data.get("album") or track.album)[:120],
            )
        if provider == "kugou":
            data = payload.get("data") if isinstance(payload, Mapping) else {}
            if not isinstance(data, Mapping):
                return track
            audio_url = str(data.get("play_url") or data.get("play_backup_url") or "").strip()
            cover = str(data.get("img") or data.get("album_img") or "").strip()
            return replace(
                track,
                music_url=audio_url if _is_http_url(audio_url) else track.music_url,
                preview_url=cover if _is_http_url(cover) else track.preview_url,
                album=str(data.get("album_name") or track.album or "")[:120],
            )
        resolved = _tracks_from_soda(payload, limit=1)
        if not resolved:
            return track
        detail = resolved[0]
        return replace(
            track,
            title=detail.title or track.title,
            artist=detail.artist or track.artist,
            album=detail.album or track.album,
            jump_url=detail.jump_url or track.jump_url,
            music_url=detail.music_url or track.music_url,
            preview_url=detail.preview_url or track.preview_url,
        )
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        return track


def save_music_selection(
    group_id: object,
    user_id: object,
    keyword: object,
    tracks: list[MusicTrack],
    ttl_seconds: object = MUSIC_SELECTION_TTL_SECONDS,
) -> int:
    """Store a member's latest candidates for a bounded group-scoped selection."""
    valid_tracks = tuple(track for track in tracks if isinstance(track, MusicTrack))
    if not valid_tracks:
        raise ValueError("没有可选择的歌曲")
    try:
        ttl = max(10, min(int(ttl_seconds), 3600))
    except (TypeError, ValueError, OverflowError):
        ttl = MUSIC_SELECTION_TTL_SECONDS
    key = (str(group_id or ""), str(user_id or ""))
    now = time.monotonic()
    with _music_selection_lock:
        for entry, (expires_at, _query, _tracks) in list(_music_selections.items()):
            if expires_at <= now:
                _music_selections.pop(entry, None)
        _music_selections[key] = (
            now + ttl,
            _clean_text(keyword, limit=MAX_MUSIC_KEYWORD_LENGTH),
            valid_tracks[:MAX_MUSIC_SEARCH_RESULTS],
        )
        if len(_music_selections) > MAX_MUSIC_SELECTIONS:
            oldest = sorted(_music_selections.items(), key=lambda item: item[1][0])[
                : len(_music_selections) - MAX_MUSIC_SELECTIONS
            ]
            for entry, _selection in oldest:
                _music_selections.pop(entry, None)
    return len(valid_tracks[:MAX_MUSIC_SEARCH_RESULTS])


def select_music_candidate(
    group_id: object, user_id: object, index: object
) -> tuple[str, MusicTrack]:
    """Resolve a 1-based member selection without crossing group/user state."""
    try:
        selected_index = int(index)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("歌曲序号必须是数字，例如 /选歌 1") from exc
    key = (str(group_id or ""), str(user_id or ""))
    with _music_selection_lock:
        selection = _music_selections.get(key)
        if not selection or selection[0] <= time.monotonic():
            _music_selections.pop(key, None)
            raise ValueError("没有可用的歌曲候选，请先使用 /点歌 搜索歌曲")
        _expires_at, keyword, tracks = selection
    if not 1 <= selected_index <= len(tracks):
        raise ValueError(f"歌曲序号应在 1-{len(tracks)} 之间")
    return keyword, tracks[selected_index - 1]


def _normalize_music_card_payload(payload: object) -> str:
    """Normalize a NapCat music-sign response to a JSON string.

    NapCat's signer has returned both a JSON string and an object across
    deployments.  AstrBot's ``Json`` component accepts either form, but the
    OneBot 11 payload is most compatible when the nested ``data`` value is a
    JSON string, matching NapCat's own converter.
    """
    candidate = payload
    for _ in range(3):
        if not isinstance(candidate, dict):
            break
        for key in ("data", "json", "card", "result"):
            value = candidate.get(key)
            if value not in (None, ""):
                candidate = value
                break
        else:
            break
    if isinstance(candidate, str):
        candidate = candidate.strip()
        if not candidate:
            raise ValueError("音乐签名服务返回空内容")
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ValueError("音乐签名服务返回的内容不是有效 JSON") from exc
    elif isinstance(candidate, (dict, list)):
        parsed = candidate
    else:
        raise ValueError("音乐签名服务返回格式无法识别")
    if not isinstance(parsed, (dict, list)) or not parsed:
        raise ValueError("音乐签名服务返回空卡片")
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _validate_signed_music_card(track: MusicTrack, serialized_payload: str) -> None:
    """Reject a signer response that identifies a different music platform."""
    try:
        payload = json.loads(serialized_payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("音乐签名服务返回的内容不是有效 JSON") from exc
    music = _decode_music_share_payload(payload)
    if not isinstance(music, Mapping):
        raise ValueError("音乐签名服务未返回可识别的音乐名片")

    requested = str(track.provider or "").strip().lower()
    serialized = json.dumps(music, ensure_ascii=False, sort_keys=True).casefold()
    provider_markers = {
        "qq": ("y.qq.com", "qqmusic", "qq音乐", "腾讯音乐", "tencent"),
        "163": ("music.163.com", "网易云", "netease"),
        "kugou": ("kugou.com", "酷狗"),
        "soda": ("汽水", "qishui", "douyin.com/music"),
    }
    markers = provider_markers.get(requested)
    if not markers or not any(marker in serialized for marker in markers):
        raise ValueError("音乐名片平台与所选歌曲不一致")

    returned_id = str(music.get("id", "") or "").strip()
    if returned_id and str(track.music_id).strip().isdigit() and returned_id.isdigit():
        if returned_id != str(track.music_id).strip():
            raise ValueError("音乐名片歌曲 ID 与所选歌曲不一致")


def build_native_music_segment(track: MusicTrack) -> dict[str, object]:
    """Build the OneBot ``music`` segment used by QQ's playable card UI.

    This is the protocol form ``type=qq/163, id=<song id>``. It lets
    NapCat/NT generate the native card with play and pause controls even when
    the optional third-party JSON signer is unavailable.
    """

    if not isinstance(track, MusicTrack) or not track.music_id:
        raise ValueError("歌曲信息不完整，无法生成音乐消息段")
    provider = str(track.provider or "").strip().lower()
    if provider not in {"qq", "163"}:
        raise ValueError("不支持的音乐平台")
    music_id = str(track.music_id).strip()
    if not re.fullmatch(r"\d{1,20}", music_id):
        raise ValueError("歌曲 ID 格式无效")
    return {"_type": provider, "id": int(music_id)}


def build_onebot_music_segment(track: MusicTrack) -> dict[str, object]:
    """Build a raw OneBot music segment for the local QQ adapter.

    The adapter, rather than this plugin, is responsible for using its
    configured ``musicSignUrl`` to sign a QQ/NetEase native music card.  This
    keeps the sender and the card provider aligned and avoids forwarding an
    unsigned JSON card that QQ rejects as an old-client message.
    """

    native = build_native_music_segment(track)
    return {
        "type": "music",
        "data": {
            "type": str(native["_type"]),
            "id": str(native["id"]),
        },
    }


def _decode_music_share_payload(payload: object) -> dict[str, object] | None:
    """Extract the nested music object from a QQ JSON share card."""
    candidate = payload
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except json.JSONDecodeError:
            return None
    if not isinstance(candidate, dict):
        return None
    nested = candidate.get("data")
    if isinstance(nested, (dict, str)):
        decoded = _decode_music_share_payload(nested)
        if decoded:
            return decoded
    meta = candidate.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("music"), dict):
        return meta["music"]
    if {"title", "jumpUrl", "musicUrl"}.issubset(candidate):
        return candidate
    return None


def music_track_from_share_payload(payload: object) -> MusicTrack | None:
    """Convert a QQ/NetEase JSON music share into a playable custom track."""
    music = _decode_music_share_payload(payload)
    if not music:
        return None
    title = str(music.get("title") or music.get("name") or "").strip()[:120]
    jump_url = str(music.get("jumpUrl") or music.get("url") or "").strip()
    music_url = str(music.get("musicUrl") or music.get("audio") or "").strip()
    image = str(music.get("preview") or music.get("image") or "").strip()
    tag = str(music.get("tag") or music.get("source") or "").casefold()
    combined_urls = f"{jump_url} {music_url}".casefold()
    is_netease = "music.163.com" in combined_urls or "网易云" in tag
    provider = "163" if is_netease else "qq"
    if not title or not jump_url and not music_url:
        return None
    id_match = (
        re.search(r"[?&]id=(\d+)", f"{jump_url} {music_url}")
        if provider == "163"
        else re.search(
            r"(?:songDetail/|[?&]songmid=)([^&/?#\s]+)",
            f"{jump_url} {music_url}",
        )
    )
    music_id = str(music.get("id") or (id_match.group(1) if id_match else "")).strip()
    if not music_id:
        music_id = jump_url or music_url
    artist = str(
        music.get("artist") or music.get("singer") or music.get("desc") or ""
    ).strip()[:120]
    album = str(music.get("desc") or music.get("album") or "").strip()[:120]
    return MusicTrack(
        provider=provider,
        music_id=music_id[:160],
        title=title,
        artist=artist,
        album=album,
        jump_url=jump_url or music_url,
        music_url=music_url or jump_url,
        preview_url=image,
    )


def build_custom_music_segment(track: MusicTrack) -> dict[str, object]:
    """Build a OneBot custom music card with a playable audio URL."""
    if not isinstance(track, MusicTrack) or not track.music_id or not track.title:
        raise ValueError("歌曲信息不完整，无法生成自定义音乐名片")
    jump_url = track.jump_url or track.music_url
    audio_url = track.music_url or jump_url
    if not _is_http_url(jump_url) or not _is_playable_audio_url(audio_url):
        raise ValueError("歌曲缺少播放地址，无法生成自定义音乐名片")
    artist = track.artist or track.album or music_provider_label(track.provider)
    return {
        "_type": "custom",
        "url": jump_url,
        "audio": audio_url,
        "title": track.title[:120],
        "content": artist[:120],
        "image": track.preview_url or QQ_MUSIC_COVER_URL,
    }


def build_onebot_custom_music_segment(track: MusicTrack) -> dict[str, object]:
    """Wrap a custom card in the raw OneBot message-segment shape."""

    custom = build_custom_music_segment(track)
    return {
        "type": "music",
        "data": {
            "type": "custom",
            "url": custom["url"],
            "audio": custom["audio"],
            "title": custom["title"],
            "content": custom["content"],
            "image": custom["image"],
        },
    }


def build_onebot_json_segment(payload: object) -> dict[str, object]:
    """Build a raw OneBot JSON segment while keeping its payload a string."""

    if isinstance(payload, str):
        serialized = payload.strip()
        try:
            json.loads(serialized)
        except json.JSONDecodeError as exc:
            raise ValueError("音乐名片签名内容不是有效 JSON") from exc
    else:
        try:
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("音乐名片签名内容无法编码") from exc
    if not serialized or len(serialized) > 64_000:
        raise ValueError("音乐名片签名内容过大")
    return {"type": "json", "data": {"data": serialized}}


async def sign_music_card(
    track: MusicTrack,
    sign_url: str = MUSIC_SIGN_URL,
    api_key: str = "",
    cookie: str = "",
) -> str:
    """Request a NapCat-compatible JSON music card for a search result."""
    if not isinstance(track, MusicTrack) or not track.music_id:
        raise ValueError("歌曲信息不完整，无法生成音乐名片")
    endpoint = str(sign_url or MUSIC_SIGN_URL).strip()
    if endpoint.rstrip("/") == MUSIC_SIGN_URL.rstrip("/"):
        endpoint = MUSIC_SIGN_URL
    try:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("音乐名片签名地址必须是 http(s) 地址")
        timeout = httpx.Timeout(MUSIC_SIGN_TIMEOUT_SECONDS, connect=2.0)
        headers = {
            "User-Agent": "Joker-QQ-Admin/1.11",
            "Content-Type": "application/json",
        }
        if str(api_key or "").strip():
            headers["Authorization"] = f"Bearer {str(api_key).strip()[:512]}"
        if str(cookie or "").strip():
            headers["Cookie"] = str(cookie).strip()[:8192]
        source = str(track.provider or "").strip().lower()
        jump_url = track.jump_url or {
            "qq": f"https://y.qq.com/n/ryqq/songDetail/{track.music_id}",
            "163": f"https://music.163.com/#/song?id={track.music_id}",
            "kugou": f"https://www.kugou.com/mixsong/{quote(track.music_id, safe='')}.html",
        }.get(source, "")
        # Match astr_meting_music's GET signer contract.  The selected source
        # is sent as ``format`` so the signer cannot silently cross platforms.
        meting_params = {
            "url": track.music_url or jump_url,
            "song": track.title,
            "singer": track.artist,
            "cover": track.preview_url,
            "jump": jump_url,
            "format": source,
        }
        # Preserve the legacy ID fields for signers that have disabled their
        # own remote ID lookup.
        request_body = {
            "type": source,
            "id": track.music_id,
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "jump_url": track.jump_url,
            "music_url": track.music_url,
            "preview_url": track.preview_url,
        }
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=True,
        ) as client:
            # Newer meting-compatible services use GET; older local signers
            # still use the JSON POST contract, so retain it as a fallback.
            last_error: Exception | None = None
            for method in ("get", "post"):
                try:
                    if method == "get":
                        response = await client.get(endpoint, params=meting_params)
                    else:
                        response = await client.post(endpoint, json=request_body)
                    response.raise_for_status()
                    envelope = _response_json(response)
                    if isinstance(envelope, dict):
                        code = envelope.get("code")
                        if code not in (None, 0, 1, "0", "1", True):
                            remote_message = str(
                                envelope.get("message")
                                or envelope.get("msg")
                                or envelope.get("error")
                                or "音乐签名服务返回失败"
                            ).strip()
                            raise ValueError(remote_message[:160])
                    signed_payload = _normalize_music_card_payload(
                        envelope
                    )
                    _validate_signed_music_card(track, signed_payload)
                    return signed_payload
                except (
                    httpx.HTTPError,
                    ValueError,
                    TypeError,
                    AttributeError,
                    json.JSONDecodeError,
                ) as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
            raise ValueError("音乐签名服务没有返回结果")
    except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
        # An unsigned com.tencent.structmsg payload is rejected by older QQ
        # clients as "sender version too low".  Do not send a payload that
        # claims to be a music card when the signer did not produce one; the
        # command layer will send a compatible text/link fallback instead.
        logger.warning(
            "音乐名片签名服务不可用 provider=%s music_id=%s error_type=%s detail=%s",
            str(track.provider or "")[:16],
            str(track.music_id or "")[:64],
            type(exc).__name__,
            str(exc).replace("\n", " ")[:160],
        )
        raise RuntimeError("音乐名片签名服务不可用") from exc


def build_music_fallback_text(track: MusicTrack) -> str:
    """Return a client-compatible share when the QQ signer cannot be used."""
    if not isinstance(track, MusicTrack) or not track.music_id:
        raise ValueError("歌曲信息不完整，无法生成分享内容")
    if track.jump_url:
        jump_url = track.jump_url
    elif track.provider == "qq":
        jump_url = f"https://y.qq.com/n/ryqq/songDetail/{track.music_id}"
    elif track.provider == "163":
        jump_url = f"https://music.163.com/#/song?id={track.music_id}"
    elif track.provider == "kugou":
        jump_url = f"https://www.kugou.com/mixsong/{quote(track.music_id, safe='')}.html"
    else:
        jump_url = ""
    artist = f" - {track.artist}" if track.artist else ""
    return f"🎵 {track.title}{artist}" + (f"\n{jump_url}" if jump_url else "")


def split_music_keywords(value: object, *, allow_empty: bool = False) -> list[str]:
    """Split a scheduled music pool on comma, pipe, or newline."""
    raw = str(value or "").replace("，", ",").replace("｜", "|")
    keywords: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,|\n\r]+", raw):
        keyword = item.strip()
        if not keyword:
            continue
        if len(keyword) > MAX_MUSIC_KEYWORD_LENGTH:
            raise ValueError(
                f"每个随机音乐关键词不能超过 {MAX_MUSIC_KEYWORD_LENGTH} 个字符"
            )
        folded = keyword.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        keywords.append(keyword)
        if len(keywords) > MAX_MUSIC_POOL_ITEMS:
            raise ValueError(f"随机音乐最多设置 {MAX_MUSIC_POOL_ITEMS} 个关键词")
    if not keywords:
        if allow_empty:
            return []
        raise ValueError("请至少设置一个随机音乐关键词")
    return keywords


def choose_random_music_keyword(value: object, previous_keyword: object = "") -> str:
    """Choose a scheduled keyword without immediately repeating it when possible."""
    keywords = split_music_keywords(value, allow_empty=True)
    if not keywords:
        keywords = list(DEFAULT_RANDOM_MUSIC_KEYWORDS)
    previous = str(previous_keyword or "").strip().casefold()
    alternatives = [item for item in keywords if item.casefold() != previous]
    pool = alternatives or keywords
    return pool[secrets.randbelow(len(pool))]


def music_track_identity(track: MusicTrack | object) -> str:
    """Return a stable, display-independent identity for a music result."""
    if not isinstance(track, MusicTrack):
        return ""
    provider = str(track.provider or "").strip().casefold()
    music_id = str(track.music_id or "").strip().casefold()
    if provider and music_id:
        return f"{provider}:{music_id}"
    return "|".join(
        value.casefold()
        for value in (
            provider,
            str(track.title or "").strip(),
            str(track.artist or "").strip(),
        )
        if value
    )


def choose_random_music_candidate(
    tracks: list[MusicTrack], previous_track: object = ""
) -> MusicTrack:
    """Choose a valid automatic result and avoid the prior song if possible."""
    candidates = [track for track in tracks if isinstance(track, MusicTrack)]
    if not candidates:
        raise ValueError("没有可播放的音乐候选")
    previous = str(previous_track or "").strip().casefold()
    alternatives = [
        track for track in candidates
        if not previous or music_track_identity(track).casefold() != previous
    ]
    pool = alternatives or candidates
    return pool[secrets.randbelow(len(pool))]


def _clean_text(value: object, *, limit: int = MAX_OPTION_LENGTH) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        raise ValueError(f"内容不能超过 {limit} 个字符")
    return text


def parse_dice_sides(value: object = "", *, maximum: int = MAX_DICE_SIDES) -> int:
    """Parse a conventional ``d20`` or integer dice argument safely."""

    raw = str(value or "").strip().lower()
    if raw.startswith("d"):
        raw = raw[1:].strip()
    if not raw:
        return 6
    if not raw.isdigit():
        raise ValueError("骰子面数必须是正整数，例如 /骰子 20")
    sides = int(raw)
    maximum = max(1, min(int(maximum), MAX_DICE_SIDES))
    if sides < 2 or sides > maximum:
        raise ValueError(f"骰子面数应在 2-{maximum} 之间")
    return sides


def roll_dice(value: object = "", *, maximum: int = MAX_DICE_SIDES) -> tuple[int, int]:
    """Return ``(sides, result)`` using a cryptographically strong draw."""

    sides = parse_dice_sides(value, maximum=maximum)
    return sides, secrets.randbelow(sides) + 1


RPS_CHOICES = ("石头", "剪刀", "布")
_RPS_ALIASES = {
    "石头": "石头",
    "石": "石头",
    "rock": "石头",
    "剪刀": "剪刀",
    "剪": "剪刀",
    "scissors": "剪刀",
    "布": "布",
    "paper": "布",
}
_RPS_BEATS = {"石头": "剪刀", "剪刀": "布", "布": "石头"}


def normalize_rps_choice(value: object) -> str:
    choice = _RPS_ALIASES.get(str(value or "").strip().lower())
    if choice is None:
        raise ValueError("猜拳请选择：石头、剪刀或布")
    return choice


def play_rps(value: object) -> tuple[str, str, str]:
    """Return ``(player, bot, outcome)`` for a rock-paper-scissors round."""

    player = normalize_rps_choice(value)
    bot = RPS_CHOICES[secrets.randbelow(len(RPS_CHOICES))]
    if player == bot:
        outcome = "平局"
    elif _RPS_BEATS[player] == bot:
        outcome = "你赢了"
    else:
        outcome = "我赢了"
    return player, bot, outcome


def split_random_options(value: object) -> list[str]:
    """Split choices by spaces, while keeping comma/pipe compatibility."""

    raw = str(value or "").strip()
    if not raw:
        raise ValueError("用法：/随机 选项1 选项2 选项3")
    # Spaces are the primary separator. Keep comma and pipe forms for old
    # messages and saved examples so existing groups do not break.
    for separator in ("，", ",", "|"):
        raw = raw.replace(separator, " ")
    options: list[str] = []
    for part in raw.split():
        option = _clean_text(part)
        if option and option not in options:
            options.append(option)
    if len(options) < 2:
        raise ValueError("至少提供两个不重复的选项")
    if len(options) > MAX_RANDOM_OPTIONS:
        raise ValueError(f"最多提供 {MAX_RANDOM_OPTIONS} 个选项")
    return options


def choose_random(value: object, *, maximum: int = MAX_RANDOM_OPTIONS) -> tuple[list[str], str]:
    options = split_random_options(value)
    maximum = max(2, min(int(maximum), MAX_RANDOM_OPTIONS))
    if len(options) > maximum:
        raise ValueError(f"最多提供 {maximum} 个选项")
    return options, options[secrets.randbelow(len(options))]


def claim_cooldown(group_id: object, user_id: object, seconds: object = 0) -> int:
    """Claim a per-group/user cooldown and return remaining seconds.

    A zero cooldown always succeeds.  The map is bounded and expired entries
    are pruned opportunistically so a busy bot cannot accumulate unbounded
    memory from arbitrary QQ IDs.
    """

    try:
        duration = max(0, min(int(seconds or 0), MAX_COOLDOWN_SECONDS))
    except (TypeError, ValueError, OverflowError):
        duration = 0
    if duration <= 0:
        return 0
    key = (str(group_id or ""), str(user_id or ""))
    now = time.monotonic()
    with _cooldown_lock:
        expired = [entry for entry, deadline in _cooldowns.items() if deadline <= now]
        for entry in expired:
            _cooldowns.pop(entry, None)
        deadline = _cooldowns.get(key, 0.0)
        if deadline > now:
            return max(1, int(deadline - now + 0.999))
        _cooldowns[key] = now + duration
        # Keep only the newest 10,000 claims if a deployment receives a large
        # burst of distinct user IDs.
        if len(_cooldowns) > 10_000:
            oldest = sorted(_cooldowns.items(), key=lambda item: item[1])[: len(_cooldowns) - 10_000]
            for entry, _deadline in oldest:
                _cooldowns.pop(entry, None)
    return 0


__all__ = [
    "MAX_DICE_SIDES",
    "MAX_RANDOM_OPTIONS",
    "MAX_DAILY_LIKES",
    "MAX_LIKES_PER_API_CALL",
    "MAX_MUSIC_POOL_ITEMS",
    "MAX_MUSIC_SEARCH_RESULTS",
    "MUSIC_SIGN_URL",
    "MUSIC_SIGN_TIMEOUT_SECONDS",
    "MUSIC_PLAYBACK_TIMEOUT_SECONDS",
    "MUSIC_PROVIDER_LABELS",
    "MUSIC_PROVIDER_ORDER",
    "QQ_MUSIC_COVER_URL",
    "QQ_MUSIC_PLAYBACK_API",
    "MUSIC_SELECTION_TTL_SECONDS",
    "MusicTrack",
    "build_native_music_segment",
    "build_onebot_music_segment",
    "build_custom_music_segment",
    "build_onebot_custom_music_segment",
    "build_onebot_json_segment",
    "build_video_search_links",
    "build_music_fallback_text",
    "RPS_CHOICES",
    "claim_daily_likes",
    "like_action_confirmed",
    "like_profile_confirmed",
    "like_profile_state",
    "claim_cooldown",
    "choose_random",
    "choose_random_music_candidate",
    "choose_random_music_keyword",
    "music_track_identity",
    "normalize_rps_choice",
    "normalize_music_providers",
    "normalize_music_source_services",
    "parse_dice_sides",
    "play_rps",
    "rollback_daily_likes",
    "roll_dice",
    "resolve_music_search_request",
    "resolve_music_playback",
    "search_music",
    "search_music_candidates",
    "music_provider_label",
    "save_music_selection",
    "select_music_candidate",
    "sign_music_card",
    "music_track_from_share_payload",
    "split_music_keywords",
    "split_random_options",
]
