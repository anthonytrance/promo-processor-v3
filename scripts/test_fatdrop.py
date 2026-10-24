"""Synthetic FATdrop contracts. Every network/log publisher is blocked before construction."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fatdrop as platform
from promo_automation import Store
from promo_pipeline import collect_release


def release_json():
    return {"campaign": {"id": 123, "name": "Alpha - Example"}, "album": {"artist": "Alpha", "title": "Example", "label": "Example Label"},
            "feedback_required_fields": 7, "access": {"has_left_feedback": False}, "k": "synthetic-key",
            "playlist": {"tracks": [
                {"track_id": 1, "artist": "Alpha", "title": "Example (Extended Mix)", "track_length": "6:30", "allow_download": True,
                 "urls": {"download": "https://audio3-a1.fatdrop.co.uk/download/1/Alpha.wav", "streaming": "https://audio3-j2.fatdrop.co.uk/stream/1/_"}},
                {"track_id": 2, "artist": "Alpha", "title": "Example (Radio Edit)", "track_length": 180, "allow_download": True,
                 "urls": {"download": "https://audio3-a1.fatdrop.co.uk/download/2/Alpha.wav"}}]}}


def bootstrap(identity="test@example.invalid", promo=None):
    commits = [{"type": "recipient/boot", "payload": {"address": identity}}]
    if promo is not None:
        commits.append({"type": "promo/boot", "payload": promo})
    return '<script>require("recipient").default(' + json.dumps({"bootstrap_store_commits": commits}) + ');</script>'


class FatdropTests(unittest.TestCase):
    def setUp(self):
        # Patch constructors, not merely the adapter instance after it was made.
        self.network = mock.patch.object(platform.requests, "Session", return_value=mock.Mock()).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(Store, "sync_logs", autospec=True).start()
        mock.patch("promo_automation.dropbox_skill", side_effect=AssertionError("real Dropbox forbidden")).start()
        mock.patch("promo_pipeline.dropbox_skill", side_effect=AssertionError("real Dropbox forbidden")).start()
        mock.patch.object(platform, "discover", side_effect=AssertionError("real mailbox forbidden")).start()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        self.adapter = platform.Fatdrop(self.store)
        self.adapter.session.request.side_effect = AssertionError("unstubbed network request")
        self.sid = self.store.add_source("fatdrop", "Synthetic release", "https://promo.fatdrop.co.uk/e/AAAAAAAAAAAAAAAAAAAAAAAA")

    def test_routes_exclude_verification_login_subscription_help_and_insecure(self):
        self.assertEqual(platform.recipient_link(self.store.source_url(self.sid))[0], "email")
        self.assertEqual(platform.recipient_link("https://promos.fatdrop.co.uk/promo/123"), ("campaign", "123"))
        for url in ("https://promos.fatdrop.co.uk/confirm", "https://promo.fatdrop.co.uk/verify/AAAAAAAAAAAAAAAAAAAAAAAA",
                    "https://promos.fatdrop.co.uk/s/AAAAAAAAAAAAAAAAAAAAAAAA?t=x", "https://www.fatdrop.co.uk/subscribe/AAA",
                    "https://www.fatdrop.co.uk/help", "https://evil.invalid/e/AAAAAAAAAAAAAAAAAAAAAAAA",
                    "http://promo.fatdrop.co.uk/e/AAAAAAAAAAAAAAAAAAAAAAAA", "https://promo.fatdrop.co.uk/e/AAAAAAAAAAAAAAAAAAAAAAAA?confirm=1"):
            self.assertIsNone(platform.recipient_link(url), url)

    def test_page_gates_are_login_blockers_not_expired_or_empty(self):
        for page in ("<h1>PLEASE CONFIRM YOUR EMAIL</h1><button>SEND</button>",
                     "<script>recaptcha.default('promo_launch')</script>", "<h1>DJ Login</h1>"):
            with self.assertRaises(platform.LoginRequired):
                platform.inspect_page(page)
        with self.assertRaises(platform.Unavailable):
            platform.inspect_page("<h1>This promo has expired</h1>")
        self.assertEqual(platform.inspect_page(bootstrap(promo=release_json()))[0], "test@example.invalid")

    def test_bootstrap_json_is_not_javascript_execution_or_repair(self):
        self.assertEqual(platform.bootstrap_commits("<script>require('x').default(alert('bad'));</script>"), [])
        self.assertEqual(platform.bootstrap_commits("<script>require('x').default({bad:1});</script>"), [])

    def test_parser_has_original_only_durations_real_label_and_feedback_gate(self):
        release = platform.parse_release(release_json())
        self.assertEqual(release["label"], "Example Label")
        self.assertEqual(release["tracks"][0]["duration"], 390)
        self.assertEqual(release["tracks"][0]["files"][0]["type"], "wav")
        self.assertTrue(release["gated"])
        self.assertFalse(release["original_contract_live_verified"])
        altered = release_json()
        altered["access"]["has_left_feedback"] = True
        self.assertFalse(platform.parse_release(altered)["gated"])

    def test_generated_format_and_disabled_zip_and_stream_only_are_explicit_review(self):
        data = release_json()
        data["playlist"]["tracks"][0]["use_download_pref"] = True
        data["playlist"]["tracks"][0]["urls"]["download"] = "https://audio3-a1.fatdrop.co.uk/download/1/Alpha"  # preference route only
        data["playlist"]["tracks"][1]["allow_download"] = False
        release = platform.parse_release(data)
        self.assertEqual([x["state"] for x in release["review_tracks"]], ["original_format_unverified", "downloads_disabled"])
        self.assertTrue(all(not x["files"] for x in release["tracks"]))
        data = release_json()
        data["download_flags"] = 2
        self.assertEqual(platform.parse_release(data)["review_tracks"][0]["state"], "zip_only_source_format_unverified")
        data = release_json()
        data["playlist"]["tracks"][0]["urls"] = {"streaming": "/stream/1.mp3"}
        self.assertFalse(platform.parse_release(data)["tracks"][0]["files"])
        data["playlist"]["tracks"][0]["urls"] = {"download": "/download/1.wav"}  # unobserved page-host route
        self.assertEqual(platform.parse_release(data)["review_tracks"][0]["state"], "original_endpoint_unverified")

    def test_metadata_success_never_proves_availability_or_optin(self):
        for key in ("available", "media_available"):
            data = release_json()
            data[key] = False
            with self.assertRaises(platform.Unavailable):
                platform.parse_release(data)
        for key in ("show_opt_in", "public_link"):
            data = release_json()
            data[key] = True
            with self.assertRaises(platform.ContractReview):
                platform.parse_release(data)
        data = release_json()
        data.update(show_opt_in=False, accounts_not_opted_in=7)  # a count only; no consent prompt is shown
        self.assertEqual(platform.parse_release(data)["remote_id"], "123")
        data = release_json()
        data["feedback_required_fields"] = None
        with self.assertRaises(platform.ContractReview):
            platform.parse_release(data)

    def test_snapshot_publishes_intent_before_get_and_caches_protected_metadata(self):
        body = bootstrap(platform.ACCOUNT, release_json())
        def request(*args, **kwargs):
            event = self.store.db.execute("SELECT action,status FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
            self.assertEqual(tuple(event), ("view", "intent"))
            Store.sync_logs.assert_called()
            return mock.Mock(status_code=200, text=body)
        with mock.patch.object(self.adapter, "request", side_effect=request) as get:
            self.assertEqual(self.adapter.snapshot(self.sid)["remote_id"], "123")
            self.adapter.snapshot(self.sid)
        get.assert_called_once()
        self.assertTrue((Path(self.temp.name) / ("fatdrop-" + self.sid + ".dpapi")).exists())

    def test_snapshot_does_not_accept_another_mailbox_or_expired_api_session(self):
        response = mock.Mock(status_code=200, text=bootstrap("other@example.invalid", release_json()))
        with mock.patch.object(self.adapter, "request", return_value=response):
            with self.assertRaises(platform.LoginRequired):
                self.adapter.snapshot(self.sid)
        response.close.assert_called_once()
        response = mock.Mock(status_code=401)
        with mock.patch.object(self.adapter, "request", return_value=response):
            with self.assertRaises(platform.LoginRequired):
                self.adapter.json_get(platform.API + "promos/stats")

    def test_count_checked_catalogue_follows_all_pages_and_records_campaign_ids(self):
        self.adapter.identity = platform.ACCOUNT
        first = [{"id": n, "name": "Synthetic"} for n in range(1, 51)]
        second = [{"id": 51, "name": "Synthetic"}]
        with mock.patch.object(self.adapter, "json_get", side_effect=[{"all": 51}, {"campaigns": first}, {"campaigns": second}, {"all": 51}]) as get:
            result = self.adapter.catalogue()
        self.assertTrue(result["complete"])
        self.assertIn("offset=50", get.call_args_list[2].args[0])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sources WHERE remote_id IS NOT NULL").fetchone()[0], 51)

    def test_catalogue_gaps_repeats_count_drift_and_wrong_account_fail_closed(self):
        with mock.patch.object(self.adapter, "json_get") as get:
            with self.assertRaises(platform.LoginRequired):
                self.adapter.catalogue()
        get.assert_not_called()
        self.adapter.identity = platform.ACCOUNT
        cases = [
            [{"all": 2}, {"campaigns": [{"id": 1}]}, {"campaigns": []}],
            [{"all": 2}, {"campaigns": [{"id": 1}]}, {"campaigns": [{"id": 1}]}],
            [{"all": 1}, {"campaigns": [{"id": 1}]}, {"all": 2}],
        ]
        for replies in cases:
            with mock.patch.object(self.adapter, "json_get", side_effect=copy.deepcopy(replies)):
                with self.assertRaises(platform.ContractReview):
                    self.adapter.catalogue(limit=1)

    def test_http_blocks_confirmation_unsubscribe_and_external_redirects_before_second_get(self):
        self.adapter.session.request.side_effect = None
        for target in ("https://evil.invalid/file.wav", "https://promos.fatdrop.co.uk/confirm", "https://promos.fatdrop.co.uk/unsubscribe"):
            self.adapter.session.request.reset_mock()
            self.adapter.session.request.return_value = mock.Mock(status_code=302, headers={"Location": target})
            with self.assertRaises(platform.ContractReview):
                self.adapter.request("GET", "/promos/123")
            self.assertEqual(self.adapter.session.request.call_count, 1)
        self.adapter.session.request.reset_mock()
        with self.assertRaises(platform.ContractReview):
            self.adapter.request("GET", "/confirm")
        self.adapter.session.request.assert_not_called()

    def test_post_redirect_and_lost_response_never_retry_submission(self):
        self.adapter.session.request.side_effect = None
        self.adapter.session.request.return_value = mock.Mock(status_code=302, headers={"Location": "/feedback"})
        with self.assertRaises(platform.ContractReview):
            self.adapter.request("POST", "/feedback", json={"comment": "synthetic"})
        self.assertEqual(self.adapter.session.request.call_count, 1)
        self.adapter.session.request.reset_mock()
        self.adapter.session.request.side_effect = TimeoutError("https://secret.invalid/token")
        with self.assertRaisesRegex(RuntimeError, "TimeoutError") as caught:
            self.adapter.request("POST", "/feedback", json={})
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.adapter.session.request.call_count, 1)

    def test_dry_run_uses_shared_versions_and_dedupe_and_never_audio_or_feedback(self):
        release = platform.parse_release(release_json())
        library = mock.Mock()
        library.matches.return_value = []
        with mock.patch.object(self.adapter, "release", return_value=release), \
                mock.patch.object(self.adapter, "download") as download, mock.patch.object(self.adapter, "unlock") as unlock, \
                mock.patch("promo_pipeline.load_exclusions", return_value={"labels": [], "artists": [], "title_words": []}):
            result = collect_release(self.store, self.adapter, self.sid, library, dry_run=True)
        self.assertEqual([x["action"] for x in result["tracks"]], ["download", "not_wanted"])
        download.assert_not_called()
        unlock.assert_not_called()

    def test_live_feedback_and_audio_remain_closed_until_authenticated_original_contract(self):
        with self.assertRaises(platform.ContractReview):
            self.adapter.unlock(self.sid, platform.parse_release(release_json()))
        with self.assertRaises(platform.ContractReview):
            self.adapter.download(self.sid, {"type": "wav"}, Path(self.temp.name) / "audio.wav")
        self.adapter.session.request.assert_not_called()

    def test_original_route_is_exact_track_on_audio_host_and_logs_intent_before_transfer(self):
        good = "https://audio3-a1.fatdrop.co.uk/download/1/Alpha%20-%20Example.wav"
        self.assertTrue(platform.original_url(good, 1))
        for url in (good + "?format=mp3", "https://audio3-a1.fatdrop.co.uk/download/1/Alpha", "https://audio3-a1.fatdrop.co.uk/download/2/x.wav",
                    "http://audio3-a1.fatdrop.co.uk/download/1/x.wav", "https://audio.evil.invalid/download/1/x.wav",
                    "https://promo.fatdrop.co.uk/download/1/x.wav", "https://audio3-a1.fatdrop.co.uk:8443/download/1/x.wav"):
            self.assertFalse(platform.original_url(url, 1), url)
        data = release_json()
        data["playlist"]["tracks"][0]["urls"]["download"] = good
        data["playlist"]["tracks"][0]["use_download_pref"] = True
        files = platform.parse_release(data)["tracks"][0]["files"]
        self.assertEqual([(f["id"], f["type"]) for f in files], [("1", "wav")])
        mismatch = copy.deepcopy(data)
        mismatch["playlist"]["tracks"][0]["urls"]["streaming"] = "https://audio3-j2.fatdrop.co.uk/stream/9/_"
        self.assertFalse(platform.parse_release(mismatch)["tracks"][0]["files"])  # another file's audio id
        def respond(method, url, **kwargs):
            event = self.store.db.execute("SELECT action,status FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
            self.assertEqual(tuple(event), ("platform_download", "intent"))
            self.assertTrue(kwargs.get("stream"))
            return mock.Mock(status_code=200, headers={"Content-Type": "audio/wav", "Content-Length": "4"},
                             iter_content=lambda size: [b"RIFF"])
        self.adapter.session.request.side_effect = respond
        size, _ = self.adapter.download(self.sid, files[0], Path(self.temp.name) / "a.wav")
        self.assertEqual(size, 4)
        self.adapter.session.request.side_effect = lambda *a, **k: mock.Mock(status_code=200, headers={"Content-Type": "text/html"})
        with self.assertRaises(platform.ContractReview):
            self.adapter.download(self.sid, files[0], Path(self.temp.name) / "b.wav")
        with self.assertRaises(platform.ContractReview):
            self.adapter.download(self.sid, dict(files[0], url=good + "?format=mp3"), Path(self.temp.name) / "c.wav")

    def test_batch_reports_every_release_and_does_not_repeat_known_auth_block(self):
        second = self.store.add_source("fatdrop", "Synthetic second", "https://promo.fatdrop.co.uk/e/BBBBBBBBBBBBBBBBBBBBBBBB")
        footer = self.store.add_source("fatdrop", "Synthetic footer", "https://promos.fatdrop.co.uk/confirm")
        with mock.patch.object(platform, "newest_offer_age", return_value=1), \
                mock.patch.object(self.adapter, "snapshot", side_effect=platform.LoginRequired("renew login")) as get:
            report = self.adapter.batch(dry_run=True)
        self.assertEqual(len(report["releases"]), 3)
        self.assertEqual({r["source_id"] for r in report["releases"]}, {self.sid, second, footer})
        self.assertEqual([r["state"] for r in report["releases"]].count("login_required"), 2)
        get.assert_called_once()
        self.assertFalse(report["catalogue_complete"])
        self.assertIn("session", report["catalogue_error"])
        self.assertFalse(report["live_original_verified"])

    def test_catalogue_date_dates_unmailed_promos_and_campaigns_dedupe(self):
        from datetime import datetime, timedelta, timezone
        stamp = lambda days: (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        recent = self.store.add_source("fatdrop", "Recent", platform.BASE + "promo/101", remote_id="101",
                                       catalogue_access_created=stamp(3))
        old = self.store.add_source("fatdrop", "Old", platform.BASE + "promo/102", remote_id="102",
                                    catalogue_access_created=stamp(90))
        footer = self.store.add_source("fatdrop", "Footer", "https://promos.fatdrop.co.uk/confirm")
        with mock.patch.object(platform, "newest_offer_age", return_value=None):
            self.assertAlmostEqual(self.adapter.offer_age(recent), 3, places=1)
            chosen = self.adapter.campaigns(max_age_days=30)
        self.assertIn(recent, chosen)
        self.assertNotIn(old, chosen)
        self.assertNotIn(footer, chosen)
        self.assertEqual(len(chosen), len(set(chosen)))


if __name__ == "__main__":
    unittest.main()
