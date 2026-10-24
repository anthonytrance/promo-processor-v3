import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ctrlpromo
from promo_automation import Store
from promo_pipeline import GateRequired, collect_release

KEY = "k" * 40
LINK = f"https://ctrlpromo.com/view-promo/101/{KEY}?mailingId=7"


def info(uploaded="fixture.wav", **promo):
    data = {"id": 101, "name": "Fixture Artist - Fixture Song", "release_date": "2099-01-01", "catalogue_number": "FIX001",
            "published_at": "2099-01-01 10:00:00", "expire_date": None, "files_deleted": None,
            "label": {"name": "Fixture Label", "company_name": "Fixture PR"},
            "secured_zip": "https://cdn.ctrlpromo.com/zip-fixture",
            "promo_files": [{"id": 501, "artist_name": "Fixture Artist", "song_name": "Fixture Song (Extended Mix)",
                             "original_filename": uploaded, "duration": "410", "file_size": "5", "bpm": 138,
                             "peaks": "[0]", "signed_url": "https://cdn.ctrlpromo.com/signed-fixture"}]}
    data.update(promo)
    return {"promo": data}


PAGE = """<html><input id="token" value="csrf-fixture"><table><tr class="songs"><td class="download-td wav-file" id="dfile-501">
</td></tr></table><textarea id="comment-content"></textarea><button id="not-for-me"></button><button id="send-comment"></button>
<form id="download-form" method="get"></form></html>"""

CATALOGUE = """<html><a class="promos-link"><span class="promos-count">2</span></a>
<div class="cp-card"><a class="view-promo" href="/view-promo/101"></a><span class="cp-status cp-status-new">New</span>
<div class="figcaption"><h5>Fixture Artist - Fixture Song</h5><span class="promo-label">Fixture Label</span>
<span class="cp-meta-line">Published 01/01 2099</span></div></div>
<div class="cp-card"><a class="view-promo" href="/view-promo/202"></a><span class="cp-status cp-status-done">Responded</span>
<div class="figcaption"><h5>Other - Song</h5><span class="promo-label">Other Label</span>
<span class="cp-meta-line">Published 01/01 2020</span></div></div></html>"""


class Response:
    def __init__(self, data=None, status=200, headers=None, body=b"audio", text=""):
        self.data, self.status_code, self.headers, self.body, self.text = data, status, headers or {}, body, text
        self.closed = False

    def json(self):
        if self.data is None:
            raise ValueError("not json")
        return self.data

    def close(self):
        self.closed = True

    def iter_content(self, _):
        yield self.body


class CtrlPromoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(Store, "sync_logs", return_value=None),
                        patch.object(ctrlpromo.requests, "Session"),
                        patch.object(ctrlpromo.requests, "get", side_effect=AssertionError("Live HTTP forbidden")),
                        patch.object(socket, "create_connection", side_effect=AssertionError("Live sockets forbidden")),
                        patch.object(socket.socket, "connect", side_effect=AssertionError("Live sockets forbidden")),
                        patch.object(ctrlpromo, "ACCOUNT", "recipient@example.test"),
                        patch.object(ctrlpromo, "protect_json", side_effect=lambda p, d: Path(p).write_text(json.dumps(d), encoding="utf-8")),
                        patch.object(ctrlpromo, "read_protected_json", side_effect=lambda p: json.loads(Path(p).read_text(encoding="utf-8")))]
        for item in self.patches:
            item.start()
        self.store = Store(self.root)
        self.sid = self.store.add_source("ctrl-promo", "Fixture", LINK)
        self.adapter = ctrlpromo.CtrlPromo(self.store)
        self.adapter.session.request.side_effect = AssertionError("Unstubbed HTTP forbidden")
        self.adapter.session.cookies = {}

    def tearDown(self):
        self.store.db.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def release(self, **kwargs):
        return ctrlpromo.normalize_release(info(**kwargs), ctrlpromo.parse_page(PAGE))

    def test_only_view_promo_links_are_accepted(self):
        self.assertEqual(ctrlpromo.recipient_link(LINK), ("101", KEY))
        self.assertEqual(ctrlpromo.recipient_link("https://ctrlpromo.com/view-promo/101"), ("101", None))
        for bad in ("http://ctrlpromo.com/view-promo/101", "https://ctrlpromo.com/", "https://ctrlpromo.com",
                    "https://evil.example/view-promo/101", "https://user@ctrlpromo.com/view-promo/101",
                    "https://ctrlpromo.com:8443/view-promo/101", "https://ctrlpromo.com/promo-unsubscribe/7/1?signature=x",
                    "https://ctrlpromo.com/promo-img/7/1", "https://ctrlpromo.com/view-promo/101?other=1",
                    "https://ctrlpromo.com/view-promo/abc"):
            self.assertIsNone(ctrlpromo.recipient_link(bad))

    def test_metadata_label_duration_and_uploaded_original(self):
        release = self.release()
        self.assertTrue(release["gated"])
        self.assertEqual(release["label"], "Fixture Label")  # never the PR company name
        track = release["tracks"][0]
        self.assertEqual((track["duration"], track["files"][0]["type"], track["files"][0]["id"]), (410.0, "wav", "501"))

    def test_mp3_upload_keeps_mp3_and_flags_transcode(self):
        release = self.release(uploaded="fixture.mp3")
        self.assertEqual(release["tracks"][0]["files"][0], {"id": "501", "type": "mp3", "bytes": 5})
        self.assertIn("transcodes", release["warnings"][0])

    def test_removed_expired_and_unknown_formats_fail(self):
        for kwargs, message in (({"files_deleted": 1}, "removed"), ({"expire_date": "2000-01-01 00:00:00"}, "expired"),
                                ({"promo_files": []}, "no tracks"), ({"uploaded": "fixture.zip"}, "unknown")):
            with self.assertRaisesRegex(RuntimeError, message):
                self.release(**kwargs)

    def test_lossless_upload_not_offered_is_refused(self):
        page = ctrlpromo.parse_page(PAGE.replace("wav-file", ""))
        with self.assertRaisesRegex(RuntimeError, "does not offer"):
            ctrlpromo.normalize_release(info(), page)

    def test_sign_in_requires_same_promo_redirect(self):
        self.adapter._request = Mock(return_value=Response(status=302, headers={"Location": "https://ctrlpromo.com/login"}))
        with self.assertRaises(ctrlpromo.AuthBlocked):
            self.adapter.sign_in()
        self.adapter._request = Mock(return_value=Response(status=302, headers={"Location": "/view-promo/101"}))
        self.assertTrue(self.adapter.sign_in()["signed_in"])
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM events WHERE action='login' AND status='intent'").fetchone())

    def test_snapshot_strips_signed_links_and_never_fetches_pixel_or_preview(self):
        self.adapter.session.cookies = {"laravel_session": "fixture"}
        calls = []
        def get(method, path, **kwargs):
            calls.append((method, path, kwargs.get("params")))
            return Response(text=PAGE) if not kwargs.get("params") else Response(info(secured_wav_zip="z"))
        self.adapter._request = Mock(side_effect=get)
        data = self.adapter.snapshot(self.sid)
        self.assertEqual([c[1] for c in calls], ["/view-promo/101", "/view-promo/101"])
        cached = (self.root / "ctrlpromo-101.dpapi").read_text(encoding="utf-8")
        self.assertNotIn("cdn.ctrlpromo.com", cached)
        self.assertNotIn("peaks", cached)
        self.assertEqual(data["page"]["token"], "csrf-fixture")

    def test_foreign_host_request_refused(self):
        with self.assertRaisesRegex(RuntimeError, "verified host"):
            self.adapter._request("GET", "https://evil.example/x")

    def test_feedback_once_stock_rating_and_no_decline_or_support(self):
        self.adapter.snapshot = Mock(return_value={"page": ctrlpromo.parse_page(PAGE)})
        self.adapter._request = Mock(return_value=Response({"success": True}))
        release = self.release()
        self.adapter.unlock(self.sid, release)
        method, path = self.adapter._request.call_args.args
        payload = self.adapter._request.call_args.kwargs["data"]
        self.assertEqual((method, path), ("POST", "/new-comment/101"))
        self.assertEqual(set(payload), {"file_id", "content", "rate", "_token"})
        self.assertIn(payload["rate"], (7, 8, 9))
        self.assertNotIn("not for me", payload["content"].lower())
        alias = self.store.add_source("ctrl-promo", "Alias", f"https://ctrlpromo.com/view-promo/101/{'j' * 40}?mailingId=8")
        self.adapter.unlock(self.sid, release)
        self.adapter.unlock(alias, release)
        self.assertEqual(self.adapter._request.call_count, 1)
        self.assertEqual(self.store.db.execute("SELECT status FROM feedback").fetchone()[0], "submitted")

    def test_unconfirmed_or_lost_feedback_never_resubmits(self):
        self.adapter.snapshot = Mock(return_value={"page": ctrlpromo.parse_page(PAGE)})
        self.adapter._request = Mock(return_value=Response({"errors": {"content": ["required"]}}))
        with self.assertRaisesRegex(RuntimeError, "without confirmed success"):
            self.adapter.unlock(self.sid, self.release())
        self.adapter._request.reset_mock()
        alias = self.store.add_source("ctrl-promo", "Alias", "https://ctrlpromo.com/view-promo/101")
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.adapter.unlock(alias, self.release())
        self.adapter._request.assert_not_called()

    def test_gate_requires_permission_and_dry_run_does_not_unlock(self):
        self.adapter.release = Mock(return_value=self.release())
        self.adapter.unlock, self.adapter.download = Mock(), Mock()
        library = Mock(); library.matches.return_value = []
        with patch("promo_pipeline.load_exclusions", return_value={}):
            dry = collect_release(self.store, self.adapter, self.sid, library, dry_run=True)
            self.assertEqual(dry["tracks"][0]["action"], "download")
            with self.assertRaises(GateRequired):
                collect_release(self.store, self.adapter, self.sid, library)
        self.adapter.unlock.assert_not_called(); self.adapter.download.assert_not_called()

    def test_download_follows_only_signed_cdn_original_without_cookies(self):
        storage = "https://cdn.ctrlpromo.com/data/promos_files/101/fixture.wav?md5=m&expires=1"
        def route(*args, **kwargs):
            self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM events WHERE action='platform_download' AND status='intent'").fetchone())
            self.assertEqual(kwargs["params"], {"wavfile": 1, "aiffile": 0})
            return Response(status=302, headers={"Location": storage})
        self.adapter._request = Mock(side_effect=route)
        audio = Response(headers={"Content-Type": "audio/wav", "Content-Length": "5"})
        with patch.object(ctrlpromo.requests, "get", return_value=audio) as get:
            size, _ = self.adapter.download(self.sid, {"id": "501", "type": "wav"}, self.root / "fixture.wav")
        self.assertEqual(size, 5)
        self.assertNotIn("cookies", get.call_args.kwargs)
        self.assertTrue(audio.closed)

    def test_download_refuses_foreign_redirect_preview_zip_and_html(self):
        for location in ("https://evil.example/data/promos_files/101/a.wav?md5=m&expires=1",
                         "https://cdn.ctrlpromo.com/data/promos_files/999/a.wav?md5=m&expires=1",
                         "https://cdn.ctrlpromo.com/data/promos_files/101/a.zip?md5=m&expires=1",
                         "https://cdn.ctrlpromo.com/data/promos_files/101/a.wav",
                         "https://cdn.ctrlpromo.com/data/promos_files/101/a.mp3?md5=m&expires=1"):
            response = Response(status=302, headers={"Location": location})
            self.adapter._request = Mock(return_value=response)
            with self.assertRaises(RuntimeError):
                self.adapter.download(self.sid, {"id": "501", "type": "wav"}, self.root / "fixture.wav")
            self.assertTrue(response.closed)
        page = Response(headers={"Content-Type": "text/html"})
        self.adapter._request = Mock(return_value=page)
        with self.assertRaisesRegex(RuntimeError, "locked"):
            self.adapter.download(self.sid, {"id": "501", "type": "wav"}, self.root / "fixture.wav")
        self.assertFalse((self.root / "fixture.wav").exists())

    def test_catalogue_count_checked_and_keeps_mailed_link(self):
        self.adapter._get = Mock(return_value=Response(text=CATALOGUE))
        result = self.adapter.discover_catalogue()
        self.assertTrue(result["complete"])
        self.assertEqual((result["total"], result["nav_count"], result["pending_feedback"]), (2, 2, 1))
        self.assertEqual(len(result["added_sources"]), 1)  # 101 already mailed, only 202 added
        self.assertEqual(self.store.source_url(self.sid), LINK)
        self.assertIs(self.adapter.responded("202"), True)

    def test_catalogue_count_mismatch_or_paging_is_incomplete(self):
        for html in (CATALOGUE.replace(">2<", ">3<"), CATALOGUE.replace("</html>", '<ul class="pagination"></ul></html>')):
            self.adapter._get = Mock(return_value=Response(text=html))
            self.assertFalse(self.adapter.discover_catalogue()["complete"])

    def test_batch_applies_age_policy_and_excludes_homepage(self):
        self.store.add_source("ctrl-promo", "Homepage", "https://ctrlpromo.com/")
        self.adapter.discover_catalogue = Mock(return_value={"available": True})
        with patch.object(ctrlpromo, "newest_offer_age", return_value=73.0), patch.object(ctrlpromo, "collect_release") as collect:
            result = self.adapter.batch(dry_run=True)
        collect.assert_not_called()
        self.assertEqual(result["excluded_nonrelease_sources"], 1)
        self.assertEqual(result["outside_age_policy"][0]["age_days"], 73.0)


if __name__ == "__main__":
    unittest.main()
