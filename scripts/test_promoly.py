import copy
import email
import tempfile
import unittest
from unittest import mock

from promo_automation import Store
from promo_mail import links_from_message, route
from promoly import Promoly, normalize_release


def fixture():
    return {"key": "stream-test", "campaign": {"id": 1, "name": "Artist - Title", "labelName": "Label",
            "enableDownload": "on", "requireComment": "on", "tracks": [
                {"track_id": 1, "track_artist": "Artist", "track_title": "Title (Extended Mix)", "track_num": 1,
                 "files": [{"file_id": 2, "file_extension": "wav", "file_size": 100, "file_name": "a.wav"}]}]},
            "feedback": []}


class MailTests(unittest.TestCase):
    def test_recipient_links_are_separated_from_tracking_and_images(self):
        self.assertEqual(route("https://ln.promo.ly/servepromo/stream-abc"), "promoly")
        for url in ("https://ln.promo.ly/unsubscribe/abc", "https://evil.promo.ly/servepromo/x",
                    "https://ln.promo.ly.evil.test/servepromo/x", "http://ln.promo.ly/servepromo/x",
                    "https://user:pass@ln.promo.ly/servepromo/x"):
            self.assertIsNone(route(url))
        self.assertEqual(route("https://label.promo.ampsuite.com/a"), "ampsuite")
        self.assertIsNone(route("https://files.constantcontact.com/logo.png"))

    def test_html_entities_are_decoded_without_following_any_url(self):
        msg = email.message_from_string('Content-Type: text/html\n\n<a href="https://dropbox.com/s/abc/a.wav?x=1&amp;y=2">Download</a>')
        self.assertEqual(links_from_message(msg), {"https://dropbox.com/s/abc/a.wav?x=1&y=2"})


class PromolyTests(unittest.TestCase):
    def test_structured_artist_title_label_and_file_sizes_survive_mapping(self):
        release = normalize_release(fixture())
        self.assertTrue(release["gated"])
        self.assertEqual(release["tracks"][0]["name"], "Artist - Title (Extended Mix)")
        self.assertEqual(release["tracks"][0]["files"][0]["size_bytes"], 100)

    def test_disabled_and_archived_downloads_are_not_bypassed(self):
        for field, value in (("enableDownload", "off"), ("archived", True), ("visibility", "private"),
                             ("expires", "2020-01-01T00:00:00Z")):
            data = fixture()
            data["campaign"][field] = value
            with self.assertRaises(RuntimeError):
                normalize_release(data)

    def test_existing_feedback_unlocks_without_resubmitting(self):
        data = fixture()
        data["feedback"] = [{"comment": "Existing feedback"}]
        self.assertFalse(normalize_release(data)["gated"])

    def test_unsupported_legacy_payload_is_explicit(self):
        data = fixture()
        data["campaign"]["tracks"] = []
        with self.assertRaisesRegex(RuntimeError, "legacy"):
            normalize_release(data)

    def test_feedback_timeout_persists_unknown_state_and_blocks_resubmission(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.sync_logs = lambda: None
            sid = store.add_source("promoly", "Artist - Title", "https://ln.promo.ly/servepromo/stream-test")
            adapter = Promoly(store)
            adapter.snapshot = lambda _: fixture()
            adapter.request = mock.Mock(side_effect=RuntimeError("timeout"))
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                adapter.unlock(sid, normalize_release(fixture()))
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                adapter.unlock(sid, normalize_release(fixture()))
            self.assertEqual(adapter.request.call_count, 1)
            row = store.db.execute("SELECT * FROM feedback").fetchone()
            self.assertEqual(row["status"], "submitting")
            self.assertEqual(row["rating"], 4)
            store.db.close()


if __name__ == "__main__":
    unittest.main()
