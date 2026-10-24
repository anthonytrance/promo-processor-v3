import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import beatrolo
from promo_automation import Store
from promo_pipeline import GateRequired, collect_release


def page():
    return {"id": "release-fixture", "title": "Fixture Song", "primaryArtistName": "Fixture Artist",
            "recordLabel": "Fixture Label", "publicExpiresAt": "2099-01-01T00:00:00Z",
            "recipient": {"email": "recipient@example.test", "name": "Fixture Recipient", "hasSubmittedFeedback": False},
            "tracks": [{"id": "track-fixture", "title": "Fixture Artist - Fixture Song (Extended Mix)",
                        "duration": 410, "audioFormat": "WAV", "fileSize": 100}]}


class Response:
    def __init__(self, data=None, status=200, headers=None, body=b"audio"):
        self.data, self.status_code, self.headers, self.body = data, status, headers or {}, body
        self.closed = False

    def json(self):
        return self.data

    def close(self):
        self.closed = True

    def iter_content(self, _):
        yield self.body


class BeatRoloTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(Store, "sync_logs", return_value=None),
                        patch.object(beatrolo.requests, "Session"),
                        patch.object(beatrolo.requests, "get", side_effect=AssertionError("Live HTTP forbidden")),
                        patch.object(socket, "create_connection", side_effect=AssertionError("Live sockets forbidden")),
                        patch.object(socket.socket, "connect", side_effect=AssertionError("Live sockets forbidden")),
                        patch.object(beatrolo, "ACCOUNT", "recipient@example.test"),
                        patch.object(beatrolo, "protect_json", side_effect=lambda p, d: Path(p).write_text(json.dumps(d), encoding="utf-8")),
                        patch.object(beatrolo, "read_protected_json", side_effect=lambda p: json.loads(Path(p).read_text(encoding="utf-8")))]
        for item in self.patches:
            item.start()
        self.store = Store(self.root)
        self.sid = self.store.add_source("beatrolo", "Fixture", "https://www.beatrolo.com/p/fixture?r=recipient-fixture")
        self.adapter = beatrolo.BeatRolo(self.store)
        self.adapter.session.request.side_effect = AssertionError("Unstubbed HTTP forbidden")

    def tearDown(self):
        self.store.db.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_only_recipient_link_is_accepted(self):
        self.assertEqual(beatrolo.recipient_link("https://beatrolo.com/p/fixture?r=x"), ("fixture", "x"))
        for bad in ("http://beatrolo.com/p/f?r=x", "https://evil.example/p/f?r=x", "https://www.beatrolo.com/p/f",
                    "https://www.beatrolo.com/portal/login?r=x", "https://user@beatrolo.com/p/f?r=x",
                    "https://beatrolo.com:8443/p/f?r=x", "https://beatrolo.com:bad/p/f?r=x"):
            self.assertIsNone(beatrolo.recipient_link(bad))

    def test_tracks_metadata_and_original_format(self):
        release = beatrolo.normalize_release(page())
        self.assertTrue(release["gated"])
        self.assertEqual(release["label"], "Fixture Label")
        self.assertEqual(release["tracks"][0]["duration"], 410)
        self.assertEqual(release["tracks"][0]["files"][0]["type"], "wav")

    def test_missing_track_title_fails(self):
        data = page(); data["tracks"][0]["title"] = ""
        with self.assertRaisesRegex(RuntimeError, "title"):
            beatrolo.normalize_release(data)

    def test_legacy_keeps_extended_mix(self):
        data = page(); data.update(tracks=[], hasDownload=True, mixTitle="Extended Mix", audioDurationMs=410500)
        release = beatrolo.normalize_release(data)
        self.assertEqual(release["tracks"][0]["name"], "Fixture Artist - Fixture Song (Extended Mix)")
        self.assertEqual(release["tracks"][0]["files"][0]["route"], "legacy")
        self.assertEqual(release["tracks"][0]["duration"], 410.5)

    def test_tracklist_independent_files(self):
        data = page(); data["tracklist"] = [{"tracks": [{"artist": "Remixer", "title": "Other Song (Remix)",
                                                           "downloadWav": "protected-fixture", "downloadMp3": "protected-fixture"}]}]
        extra = beatrolo.normalize_release(data)["tracks"][1]
        self.assertEqual([f["type"] for f in extra["files"]], ["wav", "mp3"])
        self.assertEqual(extra["files"][0]["index"], "0-0")

    def test_snapshot_checks_mailbox_and_never_tracks(self):
        self.adapter.session.request.side_effect = None
        self.adapter.session.request.return_value = Response(page())
        self.adapter.snapshot(self.sid)
        method, url = self.adapter.session.request.call_args.args
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/api/promos/public/fixture"))
        self.assertFalse(self.adapter.session.request.call_args.kwargs["allow_redirects"])
        self.store.sync_logs.assert_called()

    def test_mismatched_mailbox_refuses_before_feedback(self):
        data = page(); data["recipient"]["email"] = "other@example.test"
        self.adapter.session.request.side_effect = None; self.adapter.session.request.return_value = Response(data)
        with self.assertRaisesRegex(RuntimeError, "mailbox"):
            self.adapter.snapshot(self.sid)
        self.assertFalse((self.root / f"beatrolo-{self.sid}.dpapi").exists())

    def test_expired_release_refused(self):
        data = page(); data["publicExpiresAt"] = "2000-01-01T00:00:00Z"
        self.adapter.session.request.side_effect = None; self.adapter.session.request.return_value = Response(data)
        with self.assertRaisesRegex(RuntimeError, "expired"):
            self.adapter.snapshot(self.sid)

    def test_foreign_api_request_refused(self):
        with self.assertRaisesRegex(RuntimeError, "host"):
            self.adapter._request("GET", "https://evil.example/api/promos/public/fixture")

    def test_feedback_sent_once_and_no_support_claim(self):
        self.adapter.snapshot = Mock(return_value=page())
        self.adapter._request = Mock(return_value=Response({"success": True, "reviewId": "review-fixture"}))
        release = beatrolo.normalize_release(page())
        self.adapter.unlock(self.sid, release)
        payload = self.adapter._request.call_args.kwargs["json"]
        self.assertNotIn("wouldPlay", payload)
        self.assertNotIn("favoriteTrack", payload)
        self.adapter.unlock(self.sid, release)
        self.assertEqual(self.adapter._request.call_count, 1)
        self.assertEqual(self.store.db.execute("SELECT status FROM feedback").fetchone()[0], "submitted")

    def test_lost_feedback_response_never_resubmits(self):
        self.adapter.snapshot = Mock(return_value=page())
        self.adapter._request = Mock(side_effect=RuntimeError("response lost"))
        with self.assertRaises(RuntimeError):
            self.adapter.unlock(self.sid, beatrolo.normalize_release(page()))
        self.adapter._request.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.adapter.unlock(self.sid, beatrolo.normalize_release(page()))
        self.adapter._request.assert_not_called()

    def test_feedback_once_across_mail_and_catalogue_source_aliases(self):
        self.adapter.snapshot = Mock(return_value=page())
        self.adapter._request = Mock(return_value=Response({"success": True, "reviewId": "review-fixture"}))
        release = beatrolo.normalize_release(page())
        self.adapter.unlock(self.sid, release)
        alias = self.store.add_source("beatrolo", "Fixture alias", "https://beatrolo.com/p/fixture?r=second-fixture")
        self.adapter.unlock(alias, release)
        self.assertEqual(self.adapter._request.call_count, 1)

    def test_http_200_error_body_stays_uncertain(self):
        self.adapter.snapshot = Mock(return_value=page())
        self.adapter._request = Mock(return_value=Response({"error": "not accepted"}))
        with self.assertRaisesRegex(RuntimeError, "unconfirmed body"):
            self.adapter.unlock(self.sid, beatrolo.normalize_release(page()))
        self.assertEqual(self.store.db.execute("SELECT status FROM feedback").fetchone()[0], "submitting")

    def test_uncertain_feedback_blocks_other_source_alias(self):
        self.adapter.snapshot = Mock(return_value=page())
        self.adapter._request = Mock(side_effect=RuntimeError("lost response"))
        release = beatrolo.normalize_release(page())
        with self.assertRaises(RuntimeError):
            self.adapter.unlock(self.sid, release)
        alias = self.store.add_source("beatrolo", "Fixture alias", "https://beatrolo.com/p/fixture?r=second-fixture")
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.adapter.unlock(alias, release)
        self.assertEqual(self.adapter._request.call_count, 1)

    def test_gate_requires_permission_and_dry_run_does_not_unlock(self):
        self.adapter.release = Mock(return_value=beatrolo.normalize_release(page()))
        self.adapter.unlock, self.adapter.download = Mock(), Mock()
        library = Mock(); library.matches.return_value = []
        with patch("promo_pipeline.load_exclusions", return_value={}):
            dry = collect_release(self.store, self.adapter, self.sid, library, dry_run=True)
            self.assertEqual(dry["tracks"][0]["action"], "download")
            with self.assertRaises(GateRequired):
                collect_release(self.store, self.adapter, self.sid, library)
        self.adapter.unlock.assert_not_called(); self.adapter.download.assert_not_called()

    def test_download_stream_and_intent_precede_transfer(self):
        response = Response(headers={"Content-Type": "audio/wav", "Content-Length": "5"})
        def request(*args, **kwargs):
            self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM events WHERE action='platform_download' AND status='intent'").fetchone())
            return response
        self.adapter._request = Mock(side_effect=request)
        target = self.root / "fixture.wav"
        size, _ = self.adapter.download(self.sid, {"id": "track-fixture", "route": "track", "type": "wav"}, target)
        self.assertEqual(size, 5); self.assertEqual(target.read_bytes(), b"audio"); self.assertTrue(response.closed)

    def test_redirect_and_html_never_accepted_as_audio(self):
        for response in (Response(status=302, headers={"Location": "https://evil.example/audio.wav"}),
                         Response(headers={"Content-Type": "text/html"})):
            self.adapter._request = Mock(return_value=response)
            with self.assertRaises(RuntimeError):
                self.adapter.download(self.sid, {"id": "track", "route": "track", "type": "wav"}, self.root / "fixture.wav")
            self.assertTrue(response.closed)

    def test_observed_original_redirect_uses_unauthenticated_storage(self):
        self.adapter._request = Mock(return_value=Response(status=302, headers={
            "Location": "https://www.eburgmedia.com/beatrolo/download_audio.php?token=fixture"}))
        with patch.object(beatrolo.requests, "get", return_value=Response(headers={"Content-Type": "audio/wav", "Content-Length": "5"})) as get:
            result = self.adapter.download(self.sid, {"id": "track", "route": "track", "type": "wav"}, self.root / "fixture.wav")
            self.assertEqual(result[0], 5)
            self.assertNotIn("cookies", get.call_args.kwargs)
            self.assertNotIn("headers", get.call_args.kwargs)
            self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_storage_redirect_requires_exact_route_token_and_safe_authority(self):
        self.assertTrue(beatrolo.original_storage_url("https://www.eburgmedia.com/beatrolo/download_audio.php?token=fixture"))
        for bad in ("https://www.eburgmedia.com/beatrolo/download_audio.php", "https://www.eburgmedia.com/preview.php?token=fixture",
                    "http://www.eburgmedia.com/beatrolo/download_audio.php?token=fixture",
                    "https://www.eburgmedia.com.evil.example/beatrolo/download_audio.php?token=fixture",
                    "https://user@www.eburgmedia.com/beatrolo/download_audio.php?token=fixture",
                    "https://www.eburgmedia.com:8443/beatrolo/download_audio.php?token=fixture"):
            self.assertFalse(beatrolo.original_storage_url(bad))

    def test_catalogue_reuses_mail_source_and_updates_offer_date(self):
        self.adapter.session_path.write_text("{}", encoding="utf-8")
        self.adapter._request = Mock(return_value=Response([{"publicToken": "fixture", "recipientToken": "fresh-fixture",
                                                           "episodeTitle": "Fixture Song", "sentAt": "2099-01-01T00:00:00Z"}]))
        result = self.adapter.discover_catalogue()
        self.assertEqual(result["sources"], [self.sid])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
        self.assertEqual(self.adapter.campaigns(), [self.sid])

    def test_catalogue_missing_tokens_explicit(self):
        self.adapter.session_path.write_text("{}", encoding="utf-8")
        self.adapter._request = Mock(return_value=Response([{"publicToken": "fixture"}]))
        self.assertEqual(self.adapter.discover_catalogue()["missing_recipient_links"], 1)

    def test_no_session_reports_mail_coverage_limit(self):
        result = self.adapter.discover_catalogue()
        self.assertFalse(result["available"])
        self.adapter.session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
