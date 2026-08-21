import unittest
from unittest.mock import patch

import app as app_module


class CheckEndpointTest(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def test_check_requires_cookie_before_note_requests(self):
        with patch.object(app_module, "fetch_creator") as fetch_creator:
            response = self.client.post("/api/check", json={"username": "me"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Cookie", response.get_json()["error"])
        fetch_creator.assert_not_called()

    def test_check_returns_rate_limit_retry_hint(self):
        creator = {
            "urlname": "me",
            "nickname": "Me",
            "profileImageUrl": None,
            "followingCount": 1,
            "followerCount": 1,
        }

        with patch.object(app_module, "fetch_creator", return_value=creator), patch.object(
            app_module,
            "fetch_all_follows",
            side_effect=app_module.NoteApiError("レート制限です", status=429, retry_after=12),
        ):
            response = self.client.post("/api/check", json={"username": "me", "cookieHeader": "session=ok"})

        self.assertEqual(response.status_code, 429)
        data = response.get_json()
        self.assertEqual(data["error"], "レート制限です")
        self.assertEqual(data["retryAfterSeconds"], 12)
        self.assertEqual(response.headers["Retry-After"], "12")

    def test_fetch_follow_page_requests_per_param_to_lift_notes_600_item_cap(self):
        class RecordingResponse:
            status_code = 200
            headers = {}

            def json(self):
                return {"data": {"follows": [], "totalCount": 0, "isLastPage": True}}

        class RecordingSession:
            def __init__(self):
                self.calls = []

            def get(self, url, params=None, **kwargs):
                self.calls.append(params)
                return RecordingResponse()

        session = RecordingSession()
        app_module.fetch_follow_page(session, "me", "followings", 1)

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["page"], 1)
        # note.com silently caps these lists at ~600 items unless a `per`
        # param is present at all (its value doesn't matter, oddly) --
        # dropping this would silently reintroduce that cap.
        self.assertIn("per", session.calls[0])

    def test_fetch_follow_page_retries_then_raises_friendly_rate_limit(self):
        class RateLimitedResponse:
            status_code = 429
            headers = {"Retry-After": "12"}

        class RateLimitedSession:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return RateLimitedResponse()

        session = RateLimitedSession()
        with patch.object(app_module.time, "sleep") as sleep:
            with self.assertRaises(app_module.NoteApiError) as context:
                app_module.fetch_follow_page(session, "me", "followings", 1)

        self.assertEqual(session.calls, len(app_module.RATE_LIMIT_RETRY_DELAYS_SECONDS) + 1)
        self.assertEqual(sleep.call_count, len(app_module.RATE_LIMIT_RETRY_DELAYS_SECONDS))
        self.assertEqual(context.exception.status, 429)
        self.assertEqual(context.exception.retry_after, 12)
        self.assertIn("レート制限", context.exception.message)

    def test_fetch_follow_page_retries_on_server_busy_then_raises_friendly_message(self):
        class BusyResponse:
            status_code = 503
            headers = {}

        class BusySession:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return BusyResponse()

        session = BusySession()
        with patch.object(app_module.time, "sleep") as sleep:
            with self.assertRaises(app_module.NoteApiError) as context:
                app_module.fetch_follow_page(session, "me", "followings", 1)

        self.assertEqual(session.calls, len(app_module.RATE_LIMIT_RETRY_DELAYS_SECONDS) + 1)
        self.assertEqual(sleep.call_count, len(app_module.RATE_LIMIT_RETRY_DELAYS_SECONDS))
        self.assertEqual(context.exception.status, 503)
        self.assertIn("混み合っている", context.exception.message)

    def test_fetch_follow_page_succeeds_after_transient_server_error(self):
        class FlakyResponse:
            def __init__(self, status_code):
                self.status_code = status_code
                self.headers = {}

            def json(self):
                return {"data": {"follows": [], "totalCount": 0, "isLastPage": True}}

        class FlakySession:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return FlakyResponse(503 if self.calls == 1 else 200)

        session = FlakySession()
        with patch.object(app_module.time, "sleep"):
            follows, total, is_last = app_module.fetch_follow_page(session, "me", "followings", 1)

        self.assertEqual(session.calls, 2)
        self.assertEqual(follows, [])
        self.assertEqual(total, 0)
        self.assertTrue(is_last)

    def test_v3_list_short_of_reported_total_is_capped_and_scoped(self):
        # note.comの認証済みv3は「総数1629」と正しく返しながら、実際には1000件で
        # 打ち切って返すことがある。以前の判定は「プロフィール件数 vs 申告総数」
        # だけを見ていたため、これを「全部取れた」と誤判定し、1000件中の結果を
        # 1629件中の結果であるかのように表示していた（フォロー中1629・フォロワー1593
        # なのに片思い33人＝相互1596人という、フォロワー数を超える矛盾が出ていた）。
        creator = {
            "urlname": "me",
            "nickname": "Me",
            "profileImageUrl": None,
            "followingCount": 1629,
            "followerCount": 1593,
            "isMyself": True,
            "key": "creator-key",
        }
        followings = [{"key": f"f-{i}", "urlname": f"f_{i}", "nickname": f"F{i}"} for i in range(1000)]
        followers = [{"key": f"b-{i}", "urlname": f"b_{i}", "nickname": f"B{i}"} for i in range(1000)]

        def fake_fetch_creator(_session, urlname, headers=None):
            if urlname == "me":
                return creator
            # 取得できた1000人のうち、f_0 だけがフォローを返していない。
            return {"urlname": urlname, "isFollowing": True, "isFollowed": urlname != "f_0"}

        def fake_fetch_v3(_session, _key, kind, _cookie):
            # 申告総数は正しいが、返ってくるのは1000件だけ。
            return (followings, 1629) if kind == "followings" else (followers, 1593)

        with patch.object(app_module, "fetch_creator", side_effect=fake_fetch_creator), patch.object(
            app_module, "fetch_all_follows_v3", side_effect=fake_fetch_v3
        ):
            response = self.client.post(
                "/api/check",
                json={"username": "me", "cookieHeader": "session=ok"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["capped"])
        self.assertEqual(data["checkedFollowingCount"], 1000)
        self.assertEqual(data["checkedFollowerCount"], 1000)
        # 確認できた範囲の結果は捨てずに出す。ただし範囲を明示する。
        self.assertEqual([a["urlname"] for a in data["notFollowingBack"]], ["f_0"])
        self.assertFalse(data["notFollowingBackReliable"])
        self.assertIn("1,000人", data["notFollowingBackScope"])
        self.assertIn("1,629人", data["notFollowingBackScope"])

    def test_fetch_follow_page_retries_plain_403(self):
        # note.comの前段は一時的に403を返すことがある。以前は403をリトライ対象に
        # 入れていなかったため、1回弾かれただけで「status 403」の素っ気ない
        # エラーが利用者に出ていた。
        class PlainForbiddenResponse:
            def __init__(self, status_code):
                self.status_code = status_code
                self.headers = {}
                self.text = ""

            def json(self):
                return {"data": {"follows": [], "totalCount": 0, "isLastPage": True}}

        class FlakySession:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return PlainForbiddenResponse(403 if self.calls == 1 else 200)

        session = FlakySession()
        with patch.object(app_module.time, "sleep"):
            follows, total, is_last = app_module.fetch_follow_page(session, "me", "followings", 1)

        self.assertEqual(session.calls, 2)
        self.assertEqual(follows, [])
        self.assertTrue(is_last)

    def test_fetch_follow_page_does_not_retry_cloudfront_block(self):
        # ハードブロック中に投げ直しても通らず、叩き続けるとブロックが延びる。
        class CloudFrontBlockResponse:
            status_code = 403
            headers = {"X-Cache": "Error from cloudfront"}
            text = "<HTML><HEAD><TITLE>ERROR</TITLE></HEAD></HTML>"

        class BlockedSession:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return CloudFrontBlockResponse()

        session = BlockedSession()
        with patch.object(app_module.time, "sleep"):
            with self.assertRaises(app_module.NoteApiError) as context:
                app_module.fetch_follow_page(session, "me", "followings", 1)

        self.assertEqual(session.calls, 1)
        self.assertEqual(context.exception.status, 403)
        self.assertIn("アクセス制限", context.exception.message)

    def test_cloudfront_block_detected_beyond_first_500_characters(self):
        # 判定が本文の先頭500文字だけを見ていたため、<head>が長いブロックページを
        # 取りこぼして「status 403」になっていた。
        class LongCloudFrontBlockResponse:
            status_code = 403
            headers = {}
            text = "<!-- " + ("x" * 900) + " -->The request could not be satisfied"

        self.assertTrue(app_module.is_cloudfront_block_response(LongCloudFrontBlockResponse()))

    def test_fetch_follow_page_handles_non_json_note_response(self):
        class NonJsonResponse:
            status_code = 200
            headers = {}

            def json(self):
                raise ValueError("not json")

        class NonJsonSession:
            def get(self, *_args, **_kwargs):
                return NonJsonResponse()

        with self.assertRaises(app_module.NoteApiError) as context:
            app_module.fetch_follow_page(NonJsonSession(), "me", "followings", 1)

        self.assertEqual(context.exception.status, 502)
        self.assertIn("応答を読み取れませんでした", context.exception.message)

    def test_fetch_follow_page_resilient_recovers_beyond_the_inner_retry_budget(self):
        # fetch_follow_page itself only retries transient statuses twice
        # (3 attempts total). This exercises a page that keeps failing past
        # that budget but recovers within the outer resilient retry, which is
        # exactly the kind of flakiness a 1000+ item bulk fetch (many pages,
        # more chances for one to be unlucky) needs to survive.
        class FlakyResponse:
            def __init__(self, status_code):
                self.status_code = status_code
                self.headers = {}

            def json(self):
                return {"data": {"follows": [{"urlname": "x"}], "totalCount": 1, "isLastPage": True}}

        class FlakySession:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return FlakyResponse(503 if self.calls <= 5 else 200)

        session = FlakySession()
        with patch.object(app_module.time, "sleep"):
            follows, total, is_last = app_module.fetch_follow_page_resilient(session, "me", "followings", 1)

        self.assertEqual(follows, [{"urlname": "x"}])
        self.assertEqual(total, 1)
        self.assertTrue(is_last)
        self.assertEqual(session.calls, 6)

    def test_fetch_follow_page_resilient_gives_up_after_exhausting_all_retries(self):
        class AlwaysBusyResponse:
            status_code = 503
            headers = {}

        class AlwaysBusySession:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return AlwaysBusyResponse()

        session = AlwaysBusySession()
        expected_calls = (len(app_module.FOLLOW_PAGE_RETRY_BACKOFF_SECONDS) + 1) * (
            len(app_module.RATE_LIMIT_RETRY_DELAYS_SECONDS) + 1
        )
        with patch.object(app_module.time, "sleep"):
            with self.assertRaises(app_module.NoteApiError) as context:
                app_module.fetch_follow_page_resilient(session, "me", "followings", 1)

        self.assertEqual(session.calls, expected_calls)
        self.assertEqual(context.exception.status, 503)

    def test_fetch_all_follows_survives_one_flaky_page_among_many(self):
        # Page 1 reports enough total items to need several more pages; page 3
        # is flaky beyond fetch_follow_page's own retry budget but recovers
        # within fetch_all_follows's per-page resilience, so the whole list
        # still comes back complete instead of the request failing outright.
        class PagedResponse:
            def __init__(self, follows, total, is_last):
                self.status_code = 200
                self.headers = {}
                self._follows = follows
                self._total = total
                self._is_last = is_last

            def json(self):
                return {"data": {"follows": self._follows, "totalCount": self._total, "isLastPage": self._is_last}}

        class BusyResponse:
            status_code = 503
            headers = {}

        class FlakyPagedSession:
            def __init__(self):
                self.page3_calls = 0

            def get(self, _url, params=None, **_kwargs):
                page = params["page"]
                if page == 1:
                    return PagedResponse([{"urlname": "p1"}], 4, False)
                if page == 3:
                    self.page3_calls += 1
                    if self.page3_calls <= 3:
                        return BusyResponse()
                    return PagedResponse([{"urlname": "p3"}], 4, False)
                return PagedResponse([{"urlname": f"p{page}"}], 4, page == 4)

        session = FlakyPagedSession()
        with patch.object(app_module.time, "sleep"):
            follows, total = app_module.fetch_all_follows(session, "me", "followings")

        self.assertEqual(total, 4)
        self.assertEqual(sorted(f["urlname"] for f in follows), ["p1", "p2", "p3", "p4"])

    def test_fetch_all_follows_is_not_capped_when_notes_real_page_size_is_small(self):
        # note.com's actual per-page size for these endpoints is undocumented
        # and has changed before; the safety net used to be a fixed page-count
        # cap (MAX_PAGES), which silently turned into a much lower item cap
        # whenever the real page size came back smaller than assumed -- e.g.
        # a page size of 10 with a 100-page cap silently ceilinged every
        # account at exactly 1000 items. It must now scale with the real
        # (small) page size instead of capping prematurely.
        page_size = 10
        total = 1374

        class PagedResponse:
            def __init__(self, follows, is_last):
                self.status_code = 200
                self.headers = {}
                self._follows = follows
                self._is_last = is_last

            def json(self):
                return {"data": {"follows": self._follows, "totalCount": total, "isLastPage": self._is_last}}

        class SmallPageSession:
            def get(self, _url, params=None, **_kwargs):
                page = params["page"]
                start = (page - 1) * page_size
                end = min(start + page_size, total)
                follows = [{"urlname": f"u{i}"} for i in range(start, end)]
                return PagedResponse(follows, end >= total)

        with patch.object(app_module.time, "sleep"):
            follows, fetched_total = app_module.fetch_all_follows(SmallPageSession(), "me", "followers")

        self.assertEqual(fetched_total, total)
        self.assertEqual(len(follows), total)

    def test_fetch_all_follows_keeps_going_when_notes_totalcount_undercounts(self):
        # note.com's reported totalCount itself has been observed to be wrong
        # (capped independently of pagination), not just the page count -- a
        # page range computed purely from totalCount would stop right where
        # totalCount says to, even though isLastPage keeps reporting False
        # past that point. The real total (and every page's isLastPage flag)
        # must win over a stale/wrong totalCount.
        page_size = 10
        reported_total = 1000  # what note.com claims on page 1 (wrong/capped)
        real_total = 1374  # what actually exists and keeps coming back

        class PagedResponse:
            def __init__(self, follows, is_last):
                self.status_code = 200
                self.headers = {}
                self._follows = follows
                self._is_last = is_last

            def json(self):
                return {
                    "data": {"follows": self._follows, "totalCount": reported_total, "isLastPage": self._is_last}
                }

        class UndercountingSession:
            def get(self, _url, params=None, **_kwargs):
                page = params["page"]
                start = (page - 1) * page_size
                end = min(start + page_size, real_total)
                follows = [{"urlname": f"u{i}"} for i in range(start, end)]
                return PagedResponse(follows, end >= real_total)

        with patch.object(app_module.time, "sleep"):
            follows, fetched_total = app_module.fetch_all_follows(UndercountingSession(), "me", "followers")

        self.assertEqual(len(follows), real_total)
        self.assertEqual(fetched_total, real_total)

    def test_follow_back_candidates_use_account_key_before_urlname(self):
        creator = {
            "urlname": "me",
            "nickname": "Me",
            "profileImageUrl": None,
            "followingCount": 3,
            "followerCount": 3,
        }
        followings = [
            {"key": "same-user", "urlname": "old_name", "nickname": "Already Mutual"},
            {"id": 101, "urlname": "old_id_name", "nickname": "Already Mutual By Id"},
            {"key": "following-only", "urlname": "following_only", "nickname": "Following Only"},
        ]
        followers = [
            {"key": "same-user", "urlname": "new_name", "nickname": "Already Mutual"},
            {"id": 101, "urlname": "new_id_name", "nickname": "Already Mutual By Id"},
            {"key": "follower-only", "urlname": "follower_only", "nickname": "Follower Only"},
        ]

        def fake_fetch_all(_session, _urlname, kind):
            if kind == "followings":
                return followings, len(followings)
            return followers, len(followers)

        with patch.object(app_module, "fetch_creator", return_value=creator), patch.object(
            app_module, "fetch_all_follows", side_effect=fake_fetch_all
        ):
            response = self.client.post("/api/check", json={"username": "me", "cookieHeader": "session=other"})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["toFollowBack"], [])
        self.assertEqual([account["urlname"] for account in data["notFollowingBack"]], ["following_only"])
        self.assertFalse(data["toFollowBackReliable"])
        self.assertTrue(data["notFollowingBackReliable"])

    def test_follow_back_candidates_normalize_urlname_case(self):
        creator = {
            "urlname": "me",
            "nickname": "Me",
            "profileImageUrl": None,
            "followingCount": 1,
            "followerCount": 1,
        }
        followings = [{"urlname": "MixedCase", "nickname": "Mutual"}]
        followers = [{"urlname": "mixedcase", "nickname": "Mutual"}]

        def fake_fetch_all(_session, _urlname, kind):
            if kind == "followings":
                return followings, len(followings)
            return followers, len(followers)

        with patch.object(app_module, "fetch_creator", return_value=creator), patch.object(
            app_module, "fetch_all_follows", side_effect=fake_fetch_all
        ):
            response = self.client.post("/api/check", json={"username": "me", "cookieHeader": "session=other"})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["toFollowBack"], [])
        self.assertEqual(data["notFollowingBack"], [])

    def test_follow_back_candidates_are_suppressed_when_followings_are_capped(self):
        creator = {
            "urlname": "me",
            "nickname": "Me",
            "profileImageUrl": None,
            "followingCount": 601,
            "followerCount": 1,
        }
        followings = [{"key": "known-following", "urlname": "known_following"}]
        followers = [{"key": "maybe-already-followed", "urlname": "maybe_already_followed"}]

        def fake_fetch_all(_session, _urlname, kind):
            if kind == "followings":
                return followings, 600
            return followers, len(followers)

        with patch.object(app_module, "fetch_creator", return_value=creator), patch.object(
            app_module, "fetch_all_follows", side_effect=fake_fetch_all
        ):
            response = self.client.post("/api/check", json={"username": "me", "cookieHeader": "session=other"})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["toFollowBack"], [])
        self.assertFalse(data["toFollowBackReliable"])
        self.assertTrue(data["notFollowingBackReliable"])

    def test_not_following_back_is_suppressed_when_followers_are_capped(self):
        creator = {
            "urlname": "me",
            "nickname": "Me",
            "profileImageUrl": None,
            "followingCount": 1,
            "followerCount": 601,
        }
        followings = [{"key": "maybe-already-follower", "urlname": "maybe_already_follower"}]
        followers = [{"key": "known-follower", "urlname": "known_follower"}]

        def fake_fetch_all(_session, _urlname, kind):
            if kind == "followings":
                return followings, len(followings)
            return followers, 600

        with patch.object(app_module, "fetch_creator", return_value=creator), patch.object(
            app_module, "fetch_all_follows", side_effect=fake_fetch_all
        ):
            response = self.client.post("/api/check", json={"username": "me", "cookieHeader": "session=other"})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["notFollowingBack"], [])
        self.assertFalse(data["notFollowingBackReliable"])
        self.assertFalse(data["toFollowBackReliable"])

    def test_not_following_back_reliable_via_auth_even_when_followers_capped(self):
        creator = {
            "urlname": "me",
            "nickname": "Me",
            "profileImageUrl": None,
            "followingCount": 1,
            "followerCount": 601,
            "isMyself": False,
        }
        auth_creator = {**creator, "isMyself": True}
        mutual_candidate = {
            "key": "already-follows-me",
            "urlname": "already_follows_me",
            "nickname": "Already Follows Me",
        }
        followings = [mutual_candidate]
        followers = [{"key": f"follower-{i}", "urlname": f"follower_{i}"} for i in range(600)]

        def fake_fetch_creator(_session, urlname, headers=None):
            if urlname == "me" and headers and headers.get("Cookie") == "session=ok":
                return auth_creator
            if urlname == "me":
                return creator
            if urlname == "already_follows_me" and headers and headers.get("Cookie") == "session=ok":
                return {"urlname": urlname, "isFollowing": False, "isFollowed": True}
            return None

        def fake_fetch_all(_session, _urlname, kind):
            if kind == "followings":
                return followings, len(followings)
            # followerCount (601) > followers_total (600): followers are capped,
            # but followings are not, so the authenticated per-account check
            # should still be able to produce a reliable notFollowingBack list.
            return followers, 600

        with patch.object(app_module, "fetch_creator", side_effect=fake_fetch_creator), patch.object(
            app_module, "fetch_all_follows", side_effect=fake_fetch_all
        ):
            response = self.client.post(
                "/api/check",
                json={"username": "me", "cookieHeader": "session=ok"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["capped"])
        self.assertTrue(data["notFollowingBackReliable"])
        self.assertEqual(data["notFollowingBack"], [])

    def test_authenticated_own_account_check_uses_uncapped_v3_endpoint(self):
        # When checking your own account (authenticated_check True) and the
        # creator payload includes a "key", note.com's own web UI uses the
        # v3 endpoint instead of the public v2/creators one -- which has its
        # own undocumented item cap that `per` doesn't lift. This should be
        # preferred and should not report the account as capped even though
        # the public list (fetch_all_follows) would have looked capped.
        creator = {
            "urlname": "me",
            "nickname": "Me",
            "profileImageUrl": None,
            "followingCount": 0,
            "followerCount": 1178,
            "isMyself": False,
            "key": "my-key",
        }
        auth_creator = {**creator, "isMyself": True}
        followers = [{"key": f"follower-{i}", "urlname": f"follower_{i}"} for i in range(1178)]

        def fake_fetch_creator(_session, urlname, headers=None):
            if urlname == "me" and headers and headers.get("Cookie") == "session=ok":
                return auth_creator
            if urlname == "me":
                return creator
            return None

        def fake_fetch_all_v3(_session, key, kind, _cookie_header):
            self.assertEqual(key, "my-key")
            if kind == "followings":
                return [], 0
            return followers, len(followers)

        def unexpectedly_called_v2(*_args, **_kwargs):
            raise AssertionError("fetch_all_follows (v2) should not be used when the v3 path is available")

        with patch.object(app_module, "fetch_creator", side_effect=fake_fetch_creator), patch.object(
            app_module, "fetch_all_follows_v3", side_effect=fake_fetch_all_v3
        ), patch.object(app_module, "fetch_all_follows", side_effect=unexpectedly_called_v2):
            response = self.client.post(
                "/api/check",
                json={"username": "me", "cookieHeader": "session=ok"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data["capped"])
        self.assertEqual(data["checkedFollowerCount"], 1178)

    def test_authenticated_check_removes_already_followed_candidate(self):
        creator = {
            "urlname": "me",
            "nickname": "Me",
            "profileImageUrl": None,
            "followingCount": 0,
            "followerCount": 1,
            "isMyself": False,
        }
        auth_creator = {**creator, "isMyself": True}
        followed_candidate = {
            "key": "already-followed",
            "urlname": "already_followed",
            "nickname": "Already Followed",
        }
        followers = [followed_candidate]

        def fake_fetch_creator(_session, urlname, headers=None):
            if urlname == "me" and headers and headers.get("Cookie") == "session=ok":
                return auth_creator
            if urlname == "me":
                return creator
            if urlname == "already_followed" and headers and headers.get("Cookie") == "session=ok":
                return {"urlname": urlname, "isFollowing": True, "isFollowed": False}
            return None

        def fake_fetch_all(_session, _urlname, kind):
            if kind == "followings":
                return [], 0
            return followers, len(followers)

        with patch.object(app_module, "fetch_creator", side_effect=fake_fetch_creator), patch.object(
            app_module, "fetch_all_follows", side_effect=fake_fetch_all
        ):
            response = self.client.post(
                "/api/check",
                json={"username": "me", "cookieHeader": "session=ok"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["toFollowBack"], [])
        self.assertTrue(data["authenticatedCheck"])

    def test_authenticated_check_keeps_candidate_i_do_not_follow(self):
        creator = {
            "urlname": "me",
            "nickname": "Me",
            "profileImageUrl": None,
            "followingCount": 0,
            "followerCount": 1,
            "isMyself": False,
        }
        auth_creator = {**creator, "isMyself": True}
        candidate = {
            "key": "not-followed",
            "urlname": "not_followed",
            "nickname": "Not Followed",
        }

        def fake_fetch_creator(_session, urlname, headers=None):
            if urlname == "me" and headers and headers.get("Cookie") == "session=ok":
                return auth_creator
            if urlname == "me":
                return creator
            if urlname == "not_followed" and headers and headers.get("Cookie") == "session=ok":
                return {"urlname": urlname, "isFollowing": False, "isFollowed": False}
            return None

        def fake_fetch_all(_session, _urlname, kind):
            if kind == "followings":
                return [], 0
            return [candidate], 1

        with patch.object(app_module, "fetch_creator", side_effect=fake_fetch_creator), patch.object(
            app_module, "fetch_all_follows", side_effect=fake_fetch_all
        ):
            response = self.client.post(
                "/api/check",
                json={"username": "me", "cookieHeader": "session=ok"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual([account["urlname"] for account in data["toFollowBack"]], ["not_followed"])
        self.assertTrue(data["toFollowBackReliable"])
        self.assertTrue(data["authenticatedCheck"])

    def test_authenticated_check_drops_deleted_or_not_found_accounts(self):
        creator = {
            "urlname": "me",
            "nickname": "Me",
            "profileImageUrl": None,
            "followingCount": 1,
            "followerCount": 1,
            "isMyself": False,
        }
        auth_creator = {**creator, "isMyself": True}
        gone_following = {"key": "gone-following", "urlname": "gone_following", "nickname": "Gone"}
        gone_follower = {"key": "gone-follower", "urlname": "gone_follower", "nickname": "Gone"}

        def fake_fetch_creator(_session, urlname, headers=None):
            if urlname == "me" and headers and headers.get("Cookie") == "session=ok":
                return auth_creator
            if urlname == "me":
                return creator
            # note.com 404s the per-account lookup for both candidates,
            # simulating a deleted/withdrawn account still present in the
            # bulk follow-list response.
            return None

        def fake_fetch_all(_session, _urlname, kind):
            if kind == "followings":
                return [gone_following], 1
            return [gone_follower], 1

        with patch.object(app_module, "fetch_creator", side_effect=fake_fetch_creator), patch.object(
            app_module, "fetch_all_follows", side_effect=fake_fetch_all
        ):
            response = self.client.post(
                "/api/check",
                json={"username": "me", "cookieHeader": "session=ok"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["notFollowingBack"], [])
        self.assertEqual(data["toFollowBack"], [])
        self.assertTrue(data["notFollowingBackReliable"])
        self.assertTrue(data["toFollowBackReliable"])

    def test_authenticated_check_removes_account_that_follows_me_back(self):
        creator = {
            "urlname": "me",
            "nickname": "Me",
            "profileImageUrl": None,
            "followingCount": 1,
            "followerCount": 0,
            "isMyself": False,
        }
        auth_creator = {**creator, "isMyself": True}
        mutual_candidate = {
            "key": "already-follows-me",
            "urlname": "already_follows_me",
            "nickname": "Already Follows Me",
        }
        followings = [mutual_candidate]

        def fake_fetch_creator(_session, urlname, headers=None):
            if urlname == "me" and headers and headers.get("Cookie") == "session=ok":
                return auth_creator
            if urlname == "me":
                return creator
            if urlname == "already_follows_me" and headers and headers.get("Cookie") == "session=ok":
                return {"urlname": urlname, "isFollowing": False, "isFollowed": True}
            return None

        def fake_fetch_all(_session, _urlname, kind):
            if kind == "followings":
                return followings, len(followings)
            return [], 0

        with patch.object(app_module, "fetch_creator", side_effect=fake_fetch_creator), patch.object(
            app_module, "fetch_all_follows", side_effect=fake_fetch_all
        ):
            response = self.client.post(
                "/api/check",
                json={"username": "me", "cookieHeader": "session=ok"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["notFollowingBack"], [])
        self.assertTrue(data["authenticatedCheck"])

    def test_cookie_for_different_account_is_not_used_for_candidate_filtering(self):
        creator = {
            "urlname": "me",
            "nickname": "Me",
            "profileImageUrl": None,
            "followingCount": 0,
            "followerCount": 1,
            "isMyself": False,
        }
        follower = {
            "key": "candidate",
            "urlname": "candidate",
            "nickname": "Candidate",
        }

        def fake_fetch_creator(_session, urlname, headers=None):
            if urlname == "me":
                return creator
            if urlname == "candidate" and headers and headers.get("Cookie") == "session=other":
                return {"urlname": urlname, "isFollowing": True}
            return None

        def fake_fetch_all(_session, _urlname, kind):
            if kind == "followings":
                return [], 0
            return [follower], 1

        with patch.object(app_module, "fetch_creator", side_effect=fake_fetch_creator), patch.object(
            app_module, "fetch_all_follows", side_effect=fake_fetch_all
        ):
            response = self.client.post(
                "/api/check",
                json={"username": "me", "cookieHeader": "session=other"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["toFollowBack"], [])
        self.assertFalse(data["authenticatedCheck"])
        self.assertFalse(data["toFollowBackReliable"])
        self.assertTrue(data["toFollowBackUnavailableReason"])
        self.assertTrue(data["authWarning"])


class FollowActionEndpointTest(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def test_unfollow_confirms_success_despite_bad_status_when_state_already_changed(self):
        class FakeResponse:
            def __init__(self, status_code):
                self.status_code = status_code

        class FakeSession:
            def request(self, method, url, headers=None, timeout=None):
                # note.com occasionally answers a request it actually processed
                # with a server-error-looking status.
                return FakeResponse(500)

        def fake_fetch_creator(_session, urlname, headers=None):
            self.assertEqual(headers.get("Cookie"), "session=ok")
            return {"urlname": urlname, "isFollowing": False}

        with patch.object(app_module.requests, "Session", return_value=FakeSession()), patch.object(
            app_module, "fetch_creator", side_effect=fake_fetch_creator
        ), patch.object(app_module.time, "sleep"):
            response = self.client.post(
                "/api/unfollow",
                json={"cookieHeader": "session=ok", "targets": [{"key": "k1", "urlname": "someone"}]},
            )

        self.assertEqual(response.status_code, 200)
        results = response.get_json()["results"]
        self.assertEqual(results, [{"urlname": "someone", "success": True, "error": None}])

    def test_unfollow_reports_real_failure_when_state_did_not_change(self):
        class FakeResponse:
            def __init__(self, status_code):
                self.status_code = status_code

        class FakeSession:
            def request(self, method, url, headers=None, timeout=None):
                return FakeResponse(500)

        def fake_fetch_creator(_session, urlname, headers=None):
            return {"urlname": urlname, "isFollowing": True}

        with patch.object(app_module.requests, "Session", return_value=FakeSession()), patch.object(
            app_module, "fetch_creator", side_effect=fake_fetch_creator
        ), patch.object(app_module.time, "sleep"):
            response = self.client.post(
                "/api/unfollow",
                json={"cookieHeader": "session=ok", "targets": [{"key": "k1", "urlname": "someone"}]},
            )

        self.assertEqual(response.status_code, 200)
        results = response.get_json()["results"]
        self.assertFalse(results[0]["success"])
        self.assertIn("500", results[0]["error"])

    def test_unfollow_rate_limited_request_confirmed_success_but_later_targets_stay_skipped(self):
        class FakeResponse:
            def __init__(self, status_code):
                self.status_code = status_code
                self.headers = {}

        class FakeSession:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, headers=None, timeout=None):
                self.calls += 1
                return FakeResponse(429)

        fake_session = FakeSession()

        def fake_fetch_creator(_session, urlname, headers=None):
            self.assertEqual(urlname, "first")
            return {"urlname": urlname, "isFollowing": False}

        with patch.object(app_module.requests, "Session", return_value=fake_session), patch.object(
            app_module, "fetch_creator", side_effect=fake_fetch_creator
        ), patch.object(app_module.time, "sleep"):
            response = self.client.post(
                "/api/unfollow",
                json={
                    "cookieHeader": "session=ok",
                    "targets": [
                        {"key": "k1", "urlname": "first"},
                        {"key": "k2", "urlname": "second"},
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        results = response.get_json()["results"]
        self.assertEqual(fake_session.calls, 1)
        self.assertEqual(results[0], {"urlname": "first", "success": True, "error": None})
        self.assertFalse(results[1]["success"])
        self.assertEqual(results[1]["urlname"], "second")

    def test_unfollow_stops_immediately_on_cloudfront_block(self):
        # CloudFront in front of note.com has been observed to hard-block this
        # server after a burst of follow/unfollow calls, returning its own
        # generic error page (not a note.com response) for every subsequent
        # request. Continuing to hammer it would only prolong the block and
        # would dump the same raw HTML into every remaining result, so this
        # must stop immediately like the 429 case does.
        class CloudFrontBlockResponse:
            status_code = 403
            headers = {}
            text = (
                '<HTML><HEAD><TITLE>ERROR: The request could not be satisfied</TITLE></HEAD>'
                '<BODY><H1>403 ERROR</H1></BODY></HTML>'
            )

        class FakeSession:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, headers=None, timeout=None):
                self.calls += 1
                return CloudFrontBlockResponse()

        fake_session = FakeSession()

        with patch.object(app_module.requests, "Session", return_value=fake_session), patch.object(
            app_module.time, "sleep"
        ):
            response = self.client.post(
                "/api/unfollow",
                json={
                    "cookieHeader": "session=ok",
                    "targets": [
                        {"key": "k1", "urlname": "first"},
                        {"key": "k2", "urlname": "second"},
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        results = response.get_json()["results"]
        # Only the first target's request should actually be attempted.
        self.assertEqual(fake_session.calls, 1)
        self.assertFalse(results[0]["success"])
        self.assertFalse(results[1]["success"])
        self.assertNotIn("<HTML>", results[0]["error"])
        self.assertIn("アクセス制限", results[0]["error"])
        self.assertEqual(results[0]["error"], results[1]["error"])

    def test_unfollow_reports_plain_403_without_stopping_other_targets(self):
        # A genuine (non-CloudFront) 401/403 is a per-account auth problem,
        # not a blanket block, so it should not stop the remaining targets.
        class PlainForbiddenResponse:
            status_code = 403
            headers = {}
            text = "Forbidden"

        class FakeSession:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, headers=None, timeout=None):
                self.calls += 1
                return PlainForbiddenResponse()

        fake_session = FakeSession()

        with patch.object(app_module.requests, "Session", return_value=fake_session), patch.object(
            app_module.time, "sleep"
        ):
            response = self.client.post(
                "/api/unfollow",
                json={
                    "cookieHeader": "session=ok",
                    "targets": [
                        {"key": "k1", "urlname": "first"},
                        {"key": "k2", "urlname": "second"},
                    ],
                },
            )

        results = response.get_json()["results"]
        self.assertEqual(fake_session.calls, 2)
        self.assertIn("Cookieが正しいか確認", results[0]["error"])


class V3PageSizeTest(unittest.TestCase):
    """v3の一覧取得で使う per の値。

    2026-08-21に per=100 を試したところ、note.comのv3は 200 を返しながら中身を
    空で返し、フォロー中・フォロワーとも0件になった。エラーではないので
    「失敗したらperを下げる」形のフォールバックでは拾えない。
    公開v2では per を渡すと上限が緩むが、v3では通用しないと分かったため、
    Web UIと同じ20に固定する。ここを増やすと本番が0件になる。
    """

    def test_v3_page_size_stays_at_web_ui_value(self):
        self.assertEqual(app_module.NOTE_V3_USER_LIST_PAGE_SIZE, 20)

    def test_v3_requests_use_the_web_ui_page_size(self):
        seen = []

        def fake_page(_session, _key, kind, page, _cookie, per=None):
            seen.append((page, per))
            items = [{"urlname": f"u{page}_{i}"} for i in range(20)]
            return items, 40, page >= 2

        with patch.object(app_module, "fetch_follow_page_v3_resilient", side_effect=fake_page):
            follows, total = app_module.fetch_all_follows_v3(None, "key", "followers", "session=ok")

        self.assertEqual(len(follows), 40)
        self.assertEqual(total, 40)
        self.assertTrue(seen)
        self.assertTrue(
            all(per == 20 for _page, per in seen),
            f"v3へ20以外のperを送っている: {seen}",
        )

    def test_page_size_follows_the_actual_response_not_the_requested_per(self):
        """返ってきた件数が要求と違っても、ページ数の計算が壊れないこと。"""

        def fake_page(_session, _key, kind, page, _cookie, per=None):
            items = [{"urlname": f"u{page}_{i}"} for i in range(20)]
            return items, 60, page >= 3

        with patch.object(app_module, "fetch_follow_page_v3_resilient", side_effect=fake_page):
            follows, total = app_module.fetch_all_follows_v3(None, "key", "followers", "session=ok")

        self.assertEqual(len(follows), 60)
        self.assertEqual(total, 60)


if __name__ == "__main__":
    unittest.main()
