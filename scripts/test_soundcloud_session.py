"""Offline renewal regressions: no browser, real accounts, promo views or log uploads."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode, unquote
from unittest import mock

from curl_cffi.requests import Cookies

from promo_cloud import read_protected_json
from soundcloud import SoundCloud
from soundcloud_session import SoundCloudSession, TokenExpired, atomic_save, token_claims


def token(expiry, subject="soundcloud:users:123"):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry, "sub": subject}).encode()).decode().rstrip("=")
    return "fixture." + payload + ".signature"


def response(access=None, refresh="replacement-refresh", status=200):
    cookies = Cookies()
    if access:
        cookies.set("oauth_token", access, domain="soundcloud.com", path="/")
    if refresh:
        cookies.set("oauth_refresh_token", refresh, domain="soundcloud.com", path="/")
    return mock.Mock(status_code=status, cookies=cookies)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.auth = SoundCloudSession(Path(self.folder.name))
        self.old = token(1)
        self.new = token(4102444800)
        self.save({"oauth_token": self.old, "oauth_refresh_token": "fx-rt", "user_id": "123"})
        self.client = mock.Mock()
        self.client.post.return_value = response(self.new)
        self.factory = mock.patch("soundcloud_session.requests.Session", return_value=self.client)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def save(self, value):
        atomic_save(self.auth.path, value)

    def saved(self):
        return read_protected_json(self.auth.path)

    def test_expired_access_renews_and_next_process_reuses_encrypted_pair(self):
        self.assertEqual(self.auth.token(), self.new)
        self.assertEqual(self.saved()["oauth_refresh_token"], "replacement-refresh")
        self.assertNotIn("refresh_pending", self.saved())
        self.assertNotIn("refresh_result", self.saved())
        self.assertEqual(SoundCloudSession(self.auth.root).token(), self.new)
        self.client.post.assert_called_once()
        self.assertFalse(self.client.post.call_args.kwargs["allow_redirects"])
        self.assertNotIn(b"replacement-refresh", self.auth.path.read_bytes())
        self.assertFalse(list(self.auth.root.glob("*.tmp")))

    def test_fresh_access_does_not_contact_refresh_endpoint(self):
        self.save({"oauth_token": self.new, "oauth_refresh_token": "fx-rt"})
        self.assertEqual(self.auth.token(), self.new)
        self.client.post.assert_not_called()

    def test_host_only_response_cookies_belong_to_the_fixed_soundcloud_endpoint(self):
        cookies = Cookies()
        cookies.set("oauth_token", self.new)
        cookies.set("oauth_refresh_token", "host-only-refresh")
        self.client.post.return_value = mock.Mock(status_code=200, cookies=cookies)
        self.assertEqual(self.auth.token(), self.new)
        self.assertEqual(self.saved()["oauth_refresh_token"], "host-only-refresh")

    def test_parallel_workers_consume_one_refresh_cookie_once(self):
        with ThreadPoolExecutor(2) as pool:
            result = list(pool.map(lambda _: SoundCloudSession(self.auth.root).token(), range(2)))
        self.assertEqual(result, [self.new, self.new])
        self.client.post.assert_called_once()

    def test_another_worker_already_replaced_the_rejected_token(self):
        self.save({"oauth_token": self.new, "oauth_refresh_token": "fx-rt"})
        self.assertEqual(self.auth.token(rejected_token=self.old), self.new)
        self.client.post.assert_not_called()

    def test_timeout_leaves_durable_pending_state_and_is_never_replayed(self):
        self.client.post.side_effect = RuntimeError("private request detail")
        with self.assertRaisesRegex(TokenExpired, "uncertain") as error:
            self.auth.token()
        self.assertNotIn("private request detail", str(error.exception))
        self.assertTrue(self.saved()["refresh_pending"])
        with self.assertRaisesRegex(TokenExpired, "interrupted"):
            SoundCloudSession(self.auth.root).token()
        self.client.post.assert_called_once()

    def test_incomplete_or_foreign_cookies_never_replace_saved_pair(self):
        for cookies in (response(self.new, refresh=None), response(self.new)):
            with self.subTest(cookies=cookies):
                self.save({"oauth_token": self.old, "oauth_refresh_token": "fx-rt"})
                if cookies.cookies.get("oauth_refresh_token"):
                    cookies.cookies.clear()
                    cookies.cookies.set("oauth_token", self.new, domain="example.com", path="/")
                    cookies.cookies.set("oauth_refresh_token", "other", domain="example.com", path="/")
                self.client.post.return_value = cookies
                with self.assertRaises(TokenExpired):
                    self.auth.token()
                self.assertEqual(self.saved()["oauth_token"], self.old)
                self.assertTrue(self.saved()["refresh_pending"])
                self.assertTrue(self.saved()["refresh_result"])

    def test_refused_refresh_stops_without_repeated_requests(self):
        self.client.post.return_value = response(status=401)
        with self.assertRaisesRegex(TokenExpired, "HTTP 401"):
            self.auth.token()
        with self.assertRaises(TokenExpired):
            self.auth.token()
        self.client.post.assert_called_once()

    def test_legacy_access_only_file_reports_the_required_migration(self):
        self.save({"oauth_token": self.old})
        with self.assertRaisesRegex(TokenExpired, "both the access and refresh"):
            self.auth.token()
        self.client.post.assert_not_called()

    def test_atomic_save_failure_preserves_the_previous_file(self):
        with mock.patch("soundcloud_session.os.replace", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                self.save({"oauth_token": self.new})
        self.assertEqual(self.saved()["oauth_token"], self.old)
        self.assertFalse(list(self.auth.root.glob("*.tmp")))

    def test_renewal_must_preserve_account_identity(self):
        self.client.post.return_value = response(token(4102444800, "soundcloud:users:456"))
        with self.assertRaisesRegex(TokenExpired, "changed account"):
            self.auth.token()
        self.assertEqual(self.saved()["oauth_token"], self.old)

    @mock.patch("soundcloud_session.requests.get")
    def test_check_uses_profile_and_can_renew_a_server_rejected_unexpired_token(self, get):
        self.save({"oauth_token": self.new, "oauth_refresh_token": "fx-rt", "user_id": "123"})
        newer = token(4102444900)
        self.client.post.return_value = response(newer)
        get.side_effect = [mock.Mock(status_code=401), mock.Mock(status_code=200, json=lambda: {"id": 123})]
        self.assertEqual(self.auth.check(), {"authenticated": True, "renewal_saved": True})
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "OAuth " + newer)
        self.client.post.assert_called_once()

    @mock.patch("soundcloud_session.requests.get")
    def test_save_refuses_invalid_credentials_without_destroying_working_session(self, get):
        get.return_value.status_code = 401
        with self.assertRaises(TokenExpired):
            self.auth.save({"oauth_token": "invalid", "oauth_refresh_token": "fixture"})
        self.assertEqual(self.saved()["oauth_token"], self.old)

    def test_jwt_expiry_hint_tolerates_opaque_and_malformed_tokens(self):
        for value in (None, "opaque", "a.b.c", "fixture.W10.signature"):
            self.assertEqual(token_claims(value), {})


class AdapterTests(unittest.TestCase):
    def test_api_401_refreshes_once_and_retries_the_same_request(self):
        adapter = SoundCloud.__new__(SoundCloud)
        adapter.auth = mock.Mock(calls=0)
        adapter.auth.token.side_effect = ["before", "after"]
        adapter.session = mock.Mock(headers={})
        adapter.calls = 0
        final = mock.Mock(status_code=200)
        adapter._send = mock.Mock(side_effect=[mock.Mock(status_code=401), final])
        self.assertIs(adapter._api("/resolve", url="https://soundcloud.com/example/track"), final)
        self.assertEqual(adapter.session.headers["Authorization"], "OAuth after")
        adapter.auth.token.assert_called_with(rejected_token="before")
        self.assertEqual(adapter._send.call_args_list[0], adapter._send.call_args_list[1])

    def test_second_401_is_typed_for_the_coordinator_without_a_refresh_loop(self):
        adapter = SoundCloud.__new__(SoundCloud)
        adapter.auth = mock.Mock(calls=0)
        adapter.auth.token.return_value = "fixture"
        adapter.session = mock.Mock(headers={})
        adapter.calls = 0
        adapter._send = mock.Mock(return_value=mock.Mock(status_code=401))
        with self.assertRaises(TokenExpired):
            adapter._api("/me")
        self.assertEqual(adapter._send.call_count, 2)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.auth = SoundCloudSession(Path(self.folder.name))
        self.new = token(4102444800)
        atomic_save(self.auth.path, {"oauth_token": "old-access", "oauth_refresh_token": "old-rt"})
        self.login = {"connect_session_cookie": "fixture-connect-cookie", "client_id": "fixture-client",
                      "expected_user_id": "123"}
        self.client = mock.Mock()
        self.authorized = None
        self.callback_host = "soundcloud.com"
        self.state_matches = True
        self.session_exists = True

        def request(method, url, **kwargs):
            self.assertFalse(kwargs["allow_redirects"])
            if url.endswith("/oauth/session"):
                return mock.Mock(status_code=200, json=lambda: {"session": self.session_exists})
            if url.endswith("/oauth/authorize"):
                self.authorized = kwargs["json"]
                return mock.Mock(status_code=200, json=lambda: {"redirect_uri":
                    "https://" + self.callback_host + "/signin/callback?" + urlencode({
                        "state": self.authorized["state"] if self.state_matches else "wrong",
                        "code": "fixture-code"})})
            self.assertEqual(url, "https://soundcloud.com/n/api/code-exchange")
            data = kwargs["data"]
            self.assertEqual(data["code"], "fixture-code")
            state = json.loads(base64.b64decode(unquote(data["state"])))
            auth_values = json.loads(base64.b64decode(unquote(data["authValues"])))
            self.assertEqual(state["nonce"], auth_values["nonce"])
            self.assertEqual(auth_values["pkceVerifier"], data["code_verifier"])
            self.assertEqual(auth_values["pkceChallenge"], self.authorized["code_challenge"])
            return response(self.new)

        self.client.request.side_effect = request
        factory = mock.patch("soundcloud_session.requests.Session", return_value=self.client)
        factory.start()
        self.addCleanup(factory.stop)
        profile = mock.patch("soundcloud_session.requests.get", return_value=mock.Mock(status_code=200, json=lambda: {"id": 123}))
        self.profile = profile.start()
        self.addCleanup(profile.stop)

    def test_bootstrap_uses_existing_signin_and_pkce_without_the_browser_refresh_cookie(self):
        self.assertEqual(self.auth.bootstrap(self.login), {"authenticated": True, "renewal_saved": True})
        self.client.cookies.set.assert_called_once_with("_soundcloud_session", "fixture-connect-cookie",
                                                        domain="api-auth.soundcloud.com", path="/")
        self.assertEqual(self.client.request.call_count, 3)
        self.assertEqual(read_protected_json(self.auth.path)["oauth_token"], self.new)
        self.assertNotIn("fixture-connect-cookie", json.dumps(read_protected_json(self.auth.path)))
        self.assertFalse((self.auth.root / "soundcloud-bootstrap.dpapi").exists())

    def test_refuses_wrong_callback_or_state_before_exchanging_a_code(self):
        for field, value in (("callback_host", "example.com"), ("state_matches", False)):
            with self.subTest(field=field):
                self.callback_host, self.state_matches = "soundcloud.com", True
                setattr(self, field, value)
                self.client.request.reset_mock()
                with self.assertRaisesRegex(TokenExpired, "unexpected callback or state"):
                    self.auth.bootstrap(self.login)
                self.assertEqual(self.client.request.call_count, 2)
                self.assertEqual(read_protected_json(self.auth.path)["oauth_token"], "old-access")

    def test_different_account_never_replaces_working_session(self):
        self.login["expected_user_id"] = "456"
        with self.assertRaisesRegex(TokenExpired, "different account"):
            self.auth.bootstrap(self.login)
        self.assertEqual(read_protected_json(self.auth.path)["oauth_token"], "old-access")
        self.assertTrue((self.auth.root / "soundcloud-bootstrap.dpapi").exists())

    def test_missing_browser_login_stops_before_authorization(self):
        self.session_exists = False
        with self.assertRaisesRegex(TokenExpired, "no longer valid"):
            self.auth.bootstrap(self.login)
        self.assertEqual(self.client.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
