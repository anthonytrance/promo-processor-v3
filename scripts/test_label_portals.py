"""Synthetic portal contracts. No account, HTTP, mailbox or Dropbox access."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import label_portals as platform
from promo_automation import Store
from promo_pipeline import CollectionBlocked, collect_release


NYX_PAGE = '<a id="downloadTrack" href="/download">Download track</a>'
NYX_DATA = {"artist": "Alpha", "title": "Song", "label": "Example", "audio": True}
BH_BASE = "https://promo.blackhandmusic.net/releases/example/"


def blackhand_page():
    return ('<h1 id="innertitle">Song</h1><h2 id="innerartist">Alpha</h2>'
            '<a download="Alpha - Song (Extended Mix).wav" href="Alpha%20-%20Song%20(Extended%20Mix).wav">Wav</a>'
            '<a download="Alpha - Song (Extended Mix).mp3" href="Alpha%20-%20Song%20(Extended%20Mix).mp3">Mp3</a>'
            '<a download="Alpha - Song (Radio Edit).wav" href="Alpha%20-%20Song%20(Radio%20Edit).wav">Wav</a>'
            '<a download="pack.zip" href="pack.zip">Download All Tracks</a>'
            '<button id="buttonfeedback">Optional review</button><footer>PR Agency</footer>')


class LabelPortalTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(platform.requests, "Session", return_value=mock.Mock())
        patch.start()
        self.addCleanup(patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        patch = mock.patch.object(self.store, "sync_logs")
        self.logs = patch.start()
        self.addCleanup(patch.stop)
        self.adapter = platform.LabelPortals(self.store)
        self.adapter.session.get.side_effect = AssertionError("unstubbed network")
        self.sid = self.store.add_source("label-portal", "Example", "https://promo.nyxcreativestudio.com/")

    def test_routes_are_exact_and_invitations_are_distinct(self):
        self.assertEqual(platform.portal_kind(self.store.source_url(self.sid)), "nyx")
        self.assertEqual(platform.portal_kind(BH_BASE + "?refer=fake"), "blackhand")
        self.assertEqual(platform.portal_kind("https://promo.progressivevibesmusic.com/invitation.php?atk=fake"), "invitation")
        for url in (BH_BASE + "?action=unsubscribe", "https://promo.nyxcreativestudio.com/)", "https://promo.nyxcreativestudio.com/download",
                    "https://evil.invalid/", "https://user@promo.nyxcreativestudio.com/",
                    "https://promo.nyxcreativestudio.com:bad/", "https://promo.nyxcreativestudio.com:444/"):
            self.assertIsNone(platform.portal_kind(url))

    def test_optional_review_does_not_gate_nyx_and_format_is_not_invented(self):
        release = platform.parse_nyx(NYX_PAGE, NYX_DATA, self.store.source_url(self.sid))
        self.assertFalse(release["gated"])
        self.assertEqual(release["label"], "Example")
        self.assertEqual(release["tracks"][0]["files"][0]["type"], "original")
        self.assertEqual(self.adapter.extension(release["tracks"][0]["files"][0]), ".bin")

    def test_nyx_missing_audio_or_changed_schema_fails(self):
        for page, data in (("<html>login</html>", NYX_DATA), (NYX_PAGE, {**NYX_DATA, "audio": False}),
                           (NYX_PAGE, {**NYX_DATA, "artist": ""})):
            with self.assertRaises(RuntimeError):
                platform.parse_nyx(page, data, self.store.source_url(self.sid))

    def test_blackhand_groups_formats_and_never_uses_pr_footer_as_label(self):
        release = platform.parse_blackhand(blackhand_page(), BH_BASE)
        self.assertIsNone(release["label"])
        self.assertEqual(len(release["tracks"]), 2)
        self.assertEqual([f["type"] for f in release["tracks"][0]["files"]], ["wav", "mp3"])
        self.assertFalse(release["gated"])

    def test_campaign_files_cannot_leave_origin_folder_or_be_transformed(self):
        for href in ("https://evil.invalid/a.wav", "../a.wav", "%2E%2E%2Fa.wav", "a%5Cb.wav", "a.wav?format=wav",
                     "https://user@promo.blackhandmusic.net/releases/example/a.wav", "/download", "pack.zip"):
            with self.assertRaises(RuntimeError):
                platform.original_url(BH_BASE, href, "blackhand")
        with self.assertRaises(RuntimeError):
            platform.original_url("https://promo.nyxcreativestudio.com/", "/media/audio", "nyx")

    def test_invitation_homepage_wrapper_and_bad_route_never_make_a_request(self):
        for url, status in (("https://promo.progressivevibesmusic.com/accept.php?atk=fake", "consent_required"),
                            ("https://promo.blackhandmusic.net/index.php", "not_a_release"),
                            ("https://music.biginstrumental.com/click?url=fake", "unsupported_tracking_wrapper"),
                            ("https://promo.nyxcreativestudio.com/)", "unsupported_route")):
            sid = self.store.add_source("label-portal", "Blocked", url)
            with self.assertRaises(platform.PortalBlocked) as result:
                self.adapter.snapshot(sid)
            self.assertEqual(result.exception.status, status)
        self.adapter.session.get.assert_not_called()

    def test_snapshot_publishes_intent_and_caches_encrypted_metadata(self):
        def request(url, **kw):
            self.logs.assert_called_once()
            return (mock.Mock(status_code=200, text=NYX_PAGE, json=lambda: NYX_DATA), url)
        with mock.patch.object(self.adapter, "request", side_effect=request) as get:
            self.adapter.snapshot(self.sid)
            self.adapter.snapshot(self.sid)
        self.assertEqual(get.call_count, 2)
        self.assertTrue((self.store.root / f"label-portal-{self.sid}.dpapi").exists())

    def test_dry_plan_selects_extended_without_audio_or_optional_review(self):
        release = platform.parse_blackhand(blackhand_page(), BH_BASE)
        library = mock.Mock()
        library.matches.return_value = []
        with mock.patch.object(self.adapter, "release", return_value=release), \
                mock.patch.object(self.adapter, "download") as download, mock.patch.object(self.adapter, "unlock") as review:
            result = collect_release(self.store, self.adapter, self.sid, library, dry_run=True, max_assets=1)
        self.assertEqual([t["action"] for t in result["tracks"]], ["download", "not_wanted"])
        download.assert_not_called()
        review.assert_not_called()

    def test_redirect_to_login_or_external_host_is_not_followed(self):
        self.adapter.session.get.side_effect = None
        for location in ("https://evil.invalid/", "https://promo.nyxcreativestudio.com/login"):
            self.adapter.session.get.return_value = mock.Mock(status_code=302, headers={"Location": location})
            with self.assertRaises(RuntimeError):
                self.adapter.request(self.store.source_url(self.sid))
        self.assertEqual(self.adapter.session.get.call_count, 2)

    def test_original_must_be_in_saved_inventory_and_html_is_rejected(self):
        self.sid = self.store.add_source("label-portal", "Black Hand", BH_BASE)
        release = platform.parse_blackhand(blackhand_page(), BH_BASE)
        with mock.patch.object(self.adapter, "snapshot", return_value=release), mock.patch.object(self.adapter, "request") as get:
            with self.assertRaisesRegex(RuntimeError, "protected release inventory"):
                self.adapter.download(self.sid, {"type": "wav", "url": "https://evil.invalid/a.wav"}, Path(self.temp.name) / "a.wav")
            get.assert_not_called()
            get.return_value = (mock.Mock(status_code=200, headers={"Content-Type": "text/html"}), "unused")
            with self.assertRaisesRegex(RuntimeError, "non-audio"):
                self.adapter.download(self.sid, release["tracks"][0]["files"][0], Path(self.temp.name) / "a.wav")

    def test_complete_original_stream_is_hashed_and_closed(self):
        self.sid = self.store.add_source("label-portal", "Black Hand", BH_BASE)
        release = platform.parse_blackhand(blackhand_page(), BH_BASE)
        response = mock.Mock(status_code=200, headers={"Content-Type": "application/octet-stream", "Content-Length": "8"})
        response.iter_content.return_value = [b"RIFF1234"]
        destination = Path(self.temp.name) / "original.bin"
        with mock.patch.object(self.adapter, "snapshot", return_value=release), \
                mock.patch.object(self.adapter, "request", return_value=(response, "unused")):
            size, digest = self.adapter.download(self.sid, release["tracks"][0]["files"][0], destination)
        self.assertEqual(size, 8)
        self.assertEqual(len(digest), 64)
        self.assertEqual(destination.read_bytes(), b"RIFF1234")
        response.close.assert_called_once()

    def test_mutable_nyx_original_is_visible_in_plan_but_blocked_before_claim(self):
        release = platform.parse_nyx(NYX_PAGE, {**NYX_DATA, "id": 7}, self.store.source_url(self.sid))
        library = mock.Mock()
        library.matches.return_value = []
        with mock.patch.object(self.adapter, "release", side_effect=lambda sid: copy.deepcopy(release)), \
                mock.patch.object(self.adapter, "download") as download:
            result = collect_release(self.store, self.adapter, self.sid, library, dry_run=True)
            self.assertIn("mutable", result["collection_blocker"])
            with self.assertRaises(CollectionBlocked):
                collect_release(self.store, self.adapter, self.sid, library)
        download.assert_not_called()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 0)

    def test_undated_sources_do_not_open_portals(self):
        with mock.patch.object(platform, "newest_offer_age", return_value=None), \
                mock.patch.object(self.adapter, "snapshot") as snapshot:
            result = platform.batch(self.store, self.adapter)
        self.assertEqual(result["releases"][0]["status"], "undated_or_too_old")
        snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
