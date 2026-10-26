"""Regression tests for the local group entertainment helpers."""

import asyncio
import unittest
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch


_MODULE_PATH = Path(__file__).resolve().parent / "qqadmin" / "entertainment.py"
_SPEC = importlib.util.spec_from_file_location("joker_entertainment_helpers", _MODULE_PATH)
entertainment = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = entertainment
_SPEC.loader.exec_module(entertainment)


class EntertainmentHelperTests(unittest.TestCase):
    def tearDown(self):
        with entertainment._like_lock:
            entertainment._daily_likes.clear()
        with entertainment._music_selection_lock:
            entertainment._music_selections.clear()

    def test_daily_like_quota_is_batchable_and_bounded(self):
        granted, used, limit = entertainment.claim_daily_likes(
            "group-test", "user-test", "target-test", requested=10, daily_limit=10
        )
        self.assertEqual((granted, used, limit), (10, 10, 10))

        granted, used, limit = entertainment.claim_daily_likes(
            "group-test", "user-test", "target-test", requested=10, daily_limit=10
        )
        self.assertEqual((granted, used, limit), (0, 10, 10))

    def test_failed_like_reservation_can_be_rolled_back(self):
        granted, _used, _limit = entertainment.claim_daily_likes(
            "group-test", "user-test", "target-test", requested=10, daily_limit=10
        )
        entertainment.rollback_daily_likes(
            "group-test", "user-test", "target-test", granted
        )
        granted, used, _limit = entertainment.claim_daily_likes(
            "group-test", "user-test", "target-test", requested=10, daily_limit=10
        )
        self.assertEqual((granted, used), (10, 10))

    def test_like_quota_is_independent_per_bot_and_supports_vip_limit(self):
        granted, used, limit = entertainment.claim_daily_likes(
            "group-test",
            "user-test",
            "target-test",
            requested=20,
            daily_limit=20,
            bot_id="bot-one",
        )
        self.assertEqual((granted, used, limit), (20, 20, 20))

        granted, used, _limit = entertainment.claim_daily_likes(
            "group-test",
            "user-test",
            "target-test",
            requested=20,
            daily_limit=20,
            bot_id="bot-one",
        )
        self.assertEqual((granted, used), (0, 20))

        granted, used, _limit = entertainment.claim_daily_likes(
            "group-test",
            "user-test",
            "target-test",
            requested=20,
            daily_limit=20,
            bot_id="bot-two",
        )
        self.assertEqual((granted, used), (20, 20))

    def test_like_reservation_rollback_is_scoped_to_bot_account(self):
        entertainment.claim_daily_likes(
            "group-test", "user-test", "target-test", 20, 20, bot_id="bot-one"
        )
        entertainment.claim_daily_likes(
            "group-test", "user-test", "target-test", 20, 20, bot_id="bot-two"
        )
        entertainment.rollback_daily_likes(
            "group-test", "user-test", "target-test", 20, bot_id="bot-one"
        )

        first, first_used, _ = entertainment.claim_daily_likes(
            "group-test", "user-test", "target-test", 20, 20, bot_id="bot-one"
        )
        second, second_used, _ = entertainment.claim_daily_likes(
            "group-test", "user-test", "target-test", 20, 20, bot_id="bot-two"
        )
        self.assertEqual((first, first_used), (20, 20))
        self.assertEqual((second, second_used), (0, 20))

    def test_like_success_requires_an_explicit_platform_confirmation(self):
        self.assertTrue(
            entertainment.like_action_confirmed(
                {"status": "ok", "retcode": 0, "data": None}
            )
        )
        self.assertTrue(entertainment.like_action_confirmed({"success": True}))
        self.assertFalse(entertainment.like_action_confirmed({}))
        self.assertFalse(
            entertainment.like_action_confirmed(
                {"status": "failed", "retcode": 1200}
            )
        )
        self.assertFalse(
            entertainment.like_action_confirmed(
                {"status": "failed", "retcode": 0}
            )
        )
        self.assertFalse(entertainment.like_action_confirmed(None))

    def test_like_target_must_be_in_friend_list(self):
        response = {
            "status": "ok",
            "retcode": 0,
            "data": [{"user_id": 12345}, {"user_id": "67890"}],
        }
        self.assertTrue(entertainment.like_target_is_friend(response, "12345"))
        self.assertTrue(entertainment.like_target_is_friend(response, 67890))
        self.assertFalse(entertainment.like_target_is_friend(response, "99999"))
        self.assertIsNone(entertainment.like_target_is_friend(None, "12345"))
        self.assertIsNone(
            entertainment.like_target_is_friend(
                {"status": "failed", "retcode": 100}, "12345"
            )
        )

    def test_like_api_batch_limit_matches_onebot_limit(self):
        self.assertEqual(entertainment.MAX_LIKES_PER_API_CALL, 10)

    def test_like_profile_confirmation_requires_a_new_bot_like(self):
        before = {
            "voteInfo": {
                "total_count": 4,
                "userInfos": [{"uin": 90001}],
            }
        }
        after = {
            "voteInfo": {
                "total_count": 5,
                "userInfos": [{"uin": 90001}, {"uin": "12345"}],
            }
        }
        self.assertTrue(
            entertainment.like_profile_confirmed(before, after, "12345")
        )
        self.assertFalse(
            entertainment.like_profile_confirmed(before, before, "12345")
        )
        self.assertFalse(
            entertainment.like_profile_confirmed(
                before,
                {"voteInfo": {"total_count": 5, "userInfos": []}},
                "12345",
            )
        )

    def test_like_profile_confirmation_detects_new_count_when_total_is_unchanged(self):
        before = {
            "voteInfo": {
                "total_count": 100,
                "new_count": 0,
                "userInfos": [{"uin": "12345"}],
            }
        }
        after = {
            "voteInfo": {
                "total_count": 100,
                "new_count": 1,
                "userInfos": [{"uin": "12345"}],
            }
        }
        self.assertTrue(
            entertainment.like_profile_confirmed(before, after, "12345")
        )

    def test_video_search_includes_domestic_fallbacks(self):
        links = entertainment.build_video_search_links("周杰伦 晴天")
        labels = [label for label, _url in links]
        urls = [url for _label, url in links]
        self.assertEqual("哔哩哔哩", labels[0])
        self.assertIn("YouTube", labels)
        self.assertIn("piped.video", urls[2])
        self.assertIn("%E5%91%A8%E6%9D%B0%E4%BC%A6", urls[0])

    def test_video_search_rejects_empty_keyword(self):
        with self.assertRaises(ValueError):
            entertainment.build_video_search_links("  ")

    def test_answer_book_returns_a_nonempty_short_reply(self):
        response = entertainment.draw_answer_book()
        self.assertIsInstance(response, str)
        self.assertGreater(len(response), 3)

    def test_music_provider_payloads_are_normalized_for_cards(self):
        qq_track = entertainment._track_from_qq(
            {
                "data": {
                    "song": {
                        "list": [
                            {
                                "songid": 123,
                                "songname": "晴天",
                                "singer": [{"name": "周杰伦"}],
                                "albumname": "叶惠美",
                            }
                        ]
                    }
                }
            }
        )
        netease_track = entertainment._track_from_netease(
            {
                "result": {
                    "songs": [
                        {
                            "id": 456,
                            "name": "晴天",
                            "artists": [{"name": "周杰伦"}],
                            "album": {"name": "叶惠美"},
                        }
                    ]
                }
            }
        )
        self.assertEqual((qq_track.provider, qq_track.music_id), ("qq", "123"))
        self.assertEqual((netease_track.provider, netease_track.music_id), ("163", "456"))
        self.assertEqual(qq_track.artist, "周杰伦")
        self.assertEqual(netease_track.album, "叶惠美")
        self.assertTrue(qq_track.preview_url)
        self.assertIn("meting.mikus.ink/api?server=tencent&type=url&id=123", qq_track.music_url)

    def test_music_search_parsers_keep_multiple_candidates(self):
        tracks = entertainment._tracks_from_qq(
            {"data": {"song": {"list": [
                {"songid": 1, "songname": "晴天", "singer": [{"name": "周杰伦"}]},
                {"songid": 2, "songname": "晴天现场", "singer": [{"name": "周杰伦"}]},
            ]}}},
            limit=8,
        )
        self.assertEqual(["1", "2"], [track.music_id for track in tracks])

    def test_scheduled_music_avoids_the_previous_keyword_and_track_when_possible(self):
        self.assertEqual(
            "稻香",
            entertainment.choose_random_music_keyword("晴天|稻香", "晴天"),
        )
        tracks = [
            entertainment.MusicTrack("qq", "1", "晴天", "周杰伦"),
            entertainment.MusicTrack("qq", "2", "稻香", "周杰伦"),
        ]
        selected = entertainment.choose_random_music_candidate(tracks, "qq:1")
        self.assertEqual("2", selected.music_id)
        self.assertEqual("qq:2", entertainment.music_track_identity(selected))

    def test_kugou_and_soda_parsers_keep_playback_metadata(self):
        kugou = entertainment._tracks_from_kugou(
            {"data": {"info": [{
                "hash": "hash-123",
                "songname": "晴天",
                "singername": "周杰伦",
                "album_name": "叶惠美",
                "img": "https://example.com/cover.jpg",
            }]}},
            limit=1,
        )[0]
        soda = entertainment._tracks_from_soda(
            {"data": {"songs": [{
                "track_id": "soda-123",
                "title": "稻香",
                "artists": [{"name": "周杰伦"}],
                "album": {"name": "魔杰座"},
                "music_url": "https://music.example.com/audio.mp3",
                "share_url": "https://music.example.com/song/soda-123",
            }]}},
            limit=1,
        )[0]
        self.assertEqual(("kugou", "hash-123"), (kugou.provider, kugou.music_id))
        self.assertEqual("周杰伦", kugou.artist)
        self.assertEqual(("soda", "soda-123"), (soda.provider, soda.music_id))
        self.assertEqual("周杰伦", soda.artist)
        self.assertIn("audio.mp3", soda.music_url)

    def test_qq_playback_resolver_replaces_unplayable_bridge_url(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": {
                        "song": "晴天",
                        "singer": "周杰伦",
                        "album": "叶惠美",
                        "cover": "https://example.com/cover.jpg",
                        "url": "https://example.com/qq-preview.mp3",
                    }
                }

        class FakeClient:
            def __init__(self, **_kwargs):
                self.request = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, url, params):
                self.request = (url, params)
                return FakeResponse()

        track = entertainment.MusicTrack(
            "qq",
            "123",
            "晴天",
            jump_url="https://i.y.qq.com/v8/playsong.html?songmid=mid123",
            music_url=entertainment.QQ_MUSIC_PLAY_URL.format("123"),
        )
        with patch.object(entertainment.httpx, "AsyncClient", FakeClient):
            resolved = asyncio.run(entertainment.resolve_music_playback(track))
        self.assertEqual("https://example.com/qq-preview.mp3", resolved.music_url)
        self.assertEqual("https://example.com/cover.jpg", resolved.preview_url)

    def test_music_provider_aliases_include_kugou_and_soda(self):
        self.assertEqual(
            ("kugou", "soda"),
            entertainment.normalize_music_providers(["酷狗", "汽水音乐"]),
        )
        query, providers = entertainment.resolve_music_search_request(
            "酷狗 晴天", providers=["kugou", "soda"]
        )
        self.assertEqual(("晴天", ("kugou",)), (query, providers))

    def test_soda_search_requires_linux_owned_service_configuration(self):
        with self.assertRaisesRegex(RuntimeError, "后台服务未配置"):
            asyncio.run(
                entertainment.search_music_candidates("晴天", providers=["soda"])
            )

    def test_music_selection_is_scoped_to_group_and_user(self):
        tracks = [entertainment.MusicTrack("qq", "1", "晴天", "周杰伦")]
        self.assertEqual(1, entertainment.save_music_selection("g1", "u1", "晴天", tracks))
        self.assertEqual("1", entertainment.select_music_candidate("g1", "u1", 1)[1].music_id)
        with self.assertRaises(ValueError):
            entertainment.select_music_candidate("g1", "u2", 1)
        with self.assertRaises(ValueError):
            entertainment.select_music_candidate("g2", "u1", 1)

    def test_music_selection_timeout_can_be_configured_and_is_bounded(self):
        tracks = [entertainment.MusicTrack("qq", "1", "晴天", "周杰伦")]
        before = entertainment.time.monotonic()
        entertainment.save_music_selection(
            "g1", "u1", "晴天", tracks, ttl_seconds=30
        )
        expires_at = entertainment._music_selections[("g1", "u1")][0]
        self.assertGreaterEqual(expires_at - before, 29)
        self.assertLessEqual(expires_at - before, 31)

        entertainment.save_music_selection(
            "g2", "u1", "晴天", tracks, ttl_seconds=1
        )
        short_expires_at = entertainment._music_selections[("g2", "u1")][0]
        self.assertGreaterEqual(short_expires_at - entertainment.time.monotonic(), 9)

        entertainment.save_music_selection(
            "g3", "u1", "晴天", tracks, ttl_seconds=99999
        )
        long_expires_at = entertainment._music_selections[("g3", "u1")][0]
        self.assertLessEqual(long_expires_at - entertainment.time.monotonic(), 3600)

    def test_music_search_queries_both_built_in_providers(self):
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class FakeClient:
            def __init__(self, **_kwargs):
                self.urls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, url, **_kwargs):
                self.urls.append(url)
                if "qq.com" in url:
                    return FakeResponse({"data": {"song": {"list": [
                        {"songid": 1, "songname": "晴天", "singer": [{"name": "周杰伦"}]},
                    ]}}})
                return FakeResponse({"result": {"songs": [
                    {"id": 2, "name": "晴天", "artists": [{"name": "周杰伦"}]},
                ]}})

        with patch.object(entertainment.httpx, "AsyncClient", FakeClient):
            tracks = asyncio.run(entertainment.search_music_candidates("晴天"))
        self.assertEqual({"qq", "163"}, {track.provider for track in tracks})

    def test_music_search_only_queries_group_selected_source(self):
        calls = []

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, url, **_kwargs):
                calls.append(url)
                return FakeResponse({"result": {"songs": [
                    {"id": 2, "name": "晴天", "artists": [{"name": "周杰伦"}]},
                ]}})

        with patch.object(entertainment.httpx, "AsyncClient", FakeClient):
            tracks = asyncio.run(
                entertainment.search_music_candidates("晴天", providers=["163"])
            )
        self.assertEqual(["163"], [track.provider for track in tracks])
        self.assertEqual(1, len(calls))
        self.assertIn("music.163.com", calls[0])

    def test_music_search_uses_only_the_selected_platform_cookie(self):
        request_headers = []

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": {"song": {"list": [
                    {"songid": 2, "songname": "晴天", "singer": [{"name": "周杰伦"}]},
                ]}}}

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, _url, **kwargs):
                request_headers.append(kwargs.get("headers"))
                return FakeResponse()

        source_services = {
            "platform_cookies": {
                "qq": {"cookie": "qq-session=secret"},
                "163": {"cookie": "netease-session=secret"},
            }
        }
        with patch.object(entertainment.httpx, "AsyncClient", FakeClient):
            asyncio.run(
                entertainment.search_music_candidates(
                    "晴天", providers=["qq"], source_services=source_services
                )
            )
        self.assertEqual([{"Cookie": "qq-session=secret"}], request_headers)

    def test_music_source_alias_and_command_prefix_respect_group_policy(self):
        self.assertEqual(
            ("163", "qq"),
            entertainment.normalize_music_providers(["网易云", "QQ音乐"]),
        )
        query, providers = entertainment.resolve_music_search_request(
            "网易云 晴天", providers=["qq", "163"]
        )
        self.assertEqual(("晴天", ("163",)), (query, providers))
        with self.assertRaisesRegex(ValueError, "未在本群音乐源中启用"):
            entertainment.resolve_music_search_request(
                "网易云 晴天", providers=["qq"]
            )
        query, providers = entertainment.resolve_music_search_request(
            "爱的供养", providers=["qq", "163"], default_provider="163"
        )
        self.assertEqual(("爱的供养", ("163",)), (query, providers))

    def test_music_card_payload_accepts_signer_string_or_object(self):
        card = {"app": "com.tencent.music", "meta": {"music": {"title": "晴天"}}}
        serialized = entertainment._normalize_music_card_payload(card)
        self.assertEqual(entertainment.json.loads(serialized), card)
        self.assertEqual(
            entertainment.json.loads(
                entertainment._normalize_music_card_payload(
                    entertainment.json.dumps(card, ensure_ascii=False)
                )
            ),
            card,
        )

    def test_native_music_segment_is_playable_onebot_card_data(self):
        qq = entertainment.MusicTrack("qq", "123", "晴天", "周杰伦")
        netease = entertainment.MusicTrack("163", "456", "稻香", "周杰伦")
        self.assertEqual(
            entertainment.build_native_music_segment(qq),
            {"_type": "qq", "id": 123},
        )
        self.assertEqual(
            entertainment.build_native_music_segment(netease),
            {"_type": "163", "id": 456},
        )
        with self.assertRaises(ValueError):
            entertainment.build_native_music_segment(
                entertainment.MusicTrack("qq", "not-an-id", "坏数据")
            )

    def test_onebot_music_segment_keeps_the_selected_provider_and_id(self):
        qq = entertainment.MusicTrack("qq", "123", "晴天", "周杰伦")
        netease = entertainment.MusicTrack("163", "456", "稻香", "周杰伦")
        self.assertEqual(
            entertainment.build_onebot_music_segment(qq),
            {"type": "music", "data": {"type": "qq", "id": "123"}},
        )
        self.assertEqual(
            entertainment.build_onebot_music_segment(netease),
            {"type": "music", "data": {"type": "163", "id": "456"}},
        )

    def test_onebot_custom_music_segment_contains_playback_fields(self):
        track = entertainment.MusicTrack(
            "qq",
            "123",
            "晴天",
            "周杰伦",
            jump_url="https://y.qq.com/song/123",
            music_url="https://example.com/晴天.mp3",
        )
        segment = entertainment.build_onebot_custom_music_segment(track)
        self.assertEqual(segment["type"], "music")
        self.assertEqual(segment["data"]["type"], "custom")
        self.assertEqual(segment["data"]["title"], "晴天")
        self.assertEqual(segment["data"]["audio"], "https://example.com/晴天.mp3")

    def test_netease_share_payload_is_converted_to_custom_track(self):
        payload = {
            "app": "com.tencent.structmsg",
            "meta": {
                "music": {
                    "title": "晴天",
                    "desc": "周杰伦",
                    "jumpUrl": "https://music.163.com/#/song?id=123456",
                    "musicUrl": "https://music.163.com/song/media/outer/url?id=123456.mp3",
                    "preview": "https://example.com/cover.jpg",
                    "tag": "网易云音乐",
                }
            },
        }
        track = entertainment.music_track_from_share_payload(payload)
        self.assertIsNotNone(track)
        self.assertEqual((track.provider, track.music_id), ("163", "123456"))
        self.assertEqual(track.artist, "周杰伦")
        segment = entertainment.build_custom_music_segment(track)
        self.assertEqual(segment["_type"], "custom")
        self.assertEqual(segment["title"], "晴天")
        self.assertIn("123456.mp3", segment["audio"])

    def test_qq_share_payload_extracts_songmid_for_native_card(self):
        payload = {
            "meta": {
                "music": {
                    "title": "爱的供养",
                    "desc": "杨幂",
                    "jumpUrl": (
                        "https://i.y.qq.com/v8/playsong.html?"
                        "songmid=003Xapfn0xgx39"
                    ),
                    "musicUrl": "https://example.com/music.mp3",
                    "tag": "QQ音乐",
                }
            }
        }
        track = entertainment.music_track_from_share_payload(payload)
        self.assertIsNotNone(track)
        self.assertEqual((track.provider, track.music_id), ("qq", "003Xapfn0xgx39"))

    def test_music_share_payload_rejects_non_music_json(self):
        self.assertIsNone(
            entertainment.music_track_from_share_payload(
                {"app": "com.tencent.structmsg", "meta": {"news": {"title": "新闻"}}}
            )
        )

    def test_music_card_signing_posts_provider_and_id(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "app": "com.tencent.music",
                    "meta": {
                        "music": {
                            "title": "晴天",
                            "jumpUrl": "https://y.qq.com/n/ryqq/songDetail/123",
                            "musicUrl": "https://y.qq.com/n/ryqq/songDetail/123",
                            "tag": "QQ音乐",
                        }
                    },
                }

        class FakeClient:
            def __init__(self, **_kwargs):
                self.headers = _kwargs.get("headers", {})
                self.posted = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, endpoint, json):
                self.posted = (endpoint, json)
                return FakeResponse()

        track = entertainment.MusicTrack("qq", "123", "晴天", "周杰伦")
        with patch.object(entertainment.httpx, "AsyncClient", FakeClient):
            serialized = asyncio.run(
                entertainment.sign_music_card(track, "https://sign.example/card")
            )
        self.assertEqual(
            entertainment.json.loads(serialized)["meta"]["music"]["title"],
            "晴天",
        )

    def test_music_card_signing_supports_meting_get_contract(self):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 1,
                    "data": {
                        "app": "com.tencent.music.lua",
                        "config": {"token": "signed"},
                        "meta": {"music": {
                            "title": "晴天",
                            "jumpUrl": "https://y.qq.com/n/ryqq/songDetail/123",
                            "musicUrl": "https://example.com/晴天.mp3",
                            "tag": "QQ音乐",
                        }},
                    },
                }

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, endpoint, params):
                captured.update(endpoint=endpoint, params=params)
                return FakeResponse()

            async def post(self, *_args, **_kwargs):
                raise AssertionError("GET meting signer should be used first")

        track = entertainment.MusicTrack(
            "qq", "123", "晴天", "周杰伦",
            jump_url="https://y.qq.com/n/ryqq/songDetail/123",
            music_url="https://example.com/晴天.mp3",
        )
        with patch.object(entertainment.httpx, "AsyncClient", FakeClient):
            serialized = asyncio.run(
                entertainment.sign_music_card(track, "https://sign.example/card")
            )
        self.assertEqual("qq", captured["params"]["format"])
        self.assertEqual("晴天", entertainment.json.loads(serialized)["meta"]["music"]["title"])

    def test_music_card_signing_accepts_json_document_in_response_text(self):
        class FakeResponse:
            text = entertainment.json.dumps({
                "code": 1,
                "data": {
                    "app": "com.tencent.music.lua",
                    "meta": {"music": {
                        "title": "晴天",
                        "jumpUrl": "https://y.qq.com/n/ryqq/songDetail/123",
                        "musicUrl": "https://example.com/晴天.mp3",
                        "tag": "QQ音乐",
                    }},
                },
            }, ensure_ascii=False)

            def raise_for_status(self):
                return None

            def json(self):
                raise entertainment.json.JSONDecodeError("text", self.text, 0)

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, _endpoint, params):
                self.params = params
                return FakeResponse()

            async def post(self, *_args, **_kwargs):
                raise AssertionError("GET signer should be used first")

        track = entertainment.MusicTrack(
            "qq", "123", "晴天", "周杰伦",
            jump_url="https://y.qq.com/n/ryqq/songDetail/123",
            music_url="https://example.com/晴天.mp3",
        )
        with patch.object(entertainment.httpx, "AsyncClient", FakeClient):
            serialized = asyncio.run(
                entertainment.sign_music_card(track, "https://sign.example/card")
            )
        self.assertEqual("晴天", entertainment.json.loads(serialized)["meta"]["music"]["title"])

    def test_music_card_signing_sends_cookie_to_signer(self):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "app": "com.tencent.music",
                    "meta": {"music": {
                        "title": "晴天",
                        "jumpUrl": "https://y.qq.com/n/ryqq/songDetail/123",
                        "musicUrl": "https://y.qq.com/n/ryqq/songDetail/123",
                        "tag": "QQ音乐",
                    }},
                }

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, _endpoint, json):
                return FakeResponse()

        track = entertainment.MusicTrack("qq", "123", "晴天", "周杰伦")
        with patch.object(entertainment.httpx, "AsyncClient", FakeClient):
            asyncio.run(
                entertainment.sign_music_card(
                    track,
                    "https://sign.example/card",
                    cookie="sign-session=secret",
                )
            )
        self.assertEqual("sign-session=secret", captured["headers"]["Cookie"])

    def test_music_card_signing_rejects_a_card_from_another_platform(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "app": "com.tencent.music",
                    "meta": {
                        "music": {
                            "title": "爱的供养",
                            "jumpUrl": "https://y.qq.com/n/ryqq/songDetail/123",
                            "musicUrl": "https://y.qq.com/n/ryqq/songDetail/123",
                            "tag": "QQ音乐",
                        }
                    },
                }

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, _endpoint, json):
                return FakeResponse()

        track = entertainment.MusicTrack("163", "456", "爱的供养")
        with patch.object(entertainment.httpx, "AsyncClient", FakeClient):
            with self.assertRaisesRegex(RuntimeError, "签名服务不可用"):
                asyncio.run(
                    entertainment.sign_music_card(track, "https://sign.example/card")
                )

    def test_music_card_signing_rejects_html_instead_of_sending_unsigned_card(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                raise entertainment.json.JSONDecodeError("html", "<html>", 0)

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, _endpoint, json):
                self.posted = json
                return FakeResponse()

        track = entertainment.MusicTrack(
            "qq", "123", "晴天", "周杰伦", "叶惠美",
            "https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV",
            "https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV",
            "https://example.com/cover.jpg",
        )
        with patch.object(entertainment.httpx, "AsyncClient", FakeClient):
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    entertainment.sign_music_card(track, "https://sign.example/card")
                )

        self.assertIn(track.jump_url, entertainment.build_music_fallback_text(track))

    def test_music_track_contains_provider_links_for_fallback_card(self):
        qq = entertainment._tracks_from_qq(
            {"data": {"song": {"list": [{
                "songid": 123,
                "songmid": "mid123",
                "albummid": "album123",
                "songname": "晴天",
                "albumname": "叶惠美",
                "singer": [{"name": "周杰伦"}],
            }]}}}
        )[0]
        self.assertIn("mid123", qq.jump_url)
        self.assertIn("album123", qq.preview_url)

    def test_music_pool_supports_common_separators_and_deduplicates(self):
        self.assertEqual(
            ["晴天", "杨幂", "稻香"],
            entertainment.split_music_keywords("晴天|杨幂，稻香\n晴天"),
        )

    def test_music_pool_rejects_empty_or_oversized_input(self):
        with self.assertRaises(ValueError):
            entertainment.choose_random_music_keyword(" | ，\n ")
        with self.assertRaises(ValueError):
            entertainment.split_music_keywords("晴天," + ("x" * 81))


if __name__ == "__main__":
    unittest.main()
