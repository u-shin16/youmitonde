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


if __name__ == "__main__":
    unittest.main()
