import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hearthis
from hearthis import HearThis, campaign_link, parse_campaign, track_name
from promo_automation import Store

PAGE = """<html><body><h1>Solardo &amp; Maur "Free" (Rendezvous Sound)</h1>
<article class="track track-needs-review"><div class="track-title">REND023 - Solardo Maur - Free (Extended Mix)</div>
<div class="track-meta">Solardo &amp; Maur · WAV 99 MB</div>
<button class="download" data-file-id="501" data-token="t" data-url="/downloads/file/501/get">Download WAV</button></article>
<article class="track track-reviewed"><div class="track-title">Free</div>
<div class="track-meta">Solardo &amp; Maur · WAV 40 MB</div>
<button class="download" data-file-id="502" data-token="t" data-url="/downloads/file/502/get">Download WAV</button></article>
</body></html>"""
LINK = "https://hearthis.ai/downloads/abc123?token=00000000"


def answer(status=200, body=None, **headers):
    response = mock.Mock(status_code=status, headers=headers)
    response.json.return_value = body if body is not None else {}
    return response


class ParseTests(unittest.TestCase):
    def test_track_name_uses_the_artist_field(self):
        self.assertEqual(track_name("REND023 - Solardo Maur - Free (Extended Mix)", "Solardo & Maur"),
                         ("Solardo & Maur - Free (Extended Mix)", "Solardo & Maur"))
        self.assertEqual(track_name("Set Me Free", "Kryder"), ("Kryder - Set Me Free", "Kryder"))
        self.assertEqual(track_name("KASIA & Faithless - Tarantula", "Faithless"),
                         ("KASIA & Faithless - Tarantula", "KASIA & Faithless"))
        self.assertEqual(track_name("Olympe - Riot", ""), ("Olympe - Riot", "Olympe"))

    def test_campaign_page(self):
        release = parse_campaign(PAGE)
        self.assertEqual(release["label"], "Rendezvous Sound")
        self.assertTrue(release["gated"])
        self.assertEqual([t["name"] for t in release["tracks"]],
                         ["Solardo & Maur - Free (Extended Mix)", "Solardo & Maur - Free"])
        self.assertEqual([f["reviewed"] for t in release["tracks"] for f in t["files"]], [False, True])
        self.assertEqual(release["tracks"][0]["files"][0]["type"], "wav")

    def test_link_parsing_refuses_other_hosts(self):
        self.assertEqual(campaign_link(LINK), ("abc123", "00000000"))
        self.assertIsNone(campaign_link("https://hearthis.ai/downloads/abc123"))
        self.assertIsNone(campaign_link(LINK.replace("hearthis.ai", "hearthis.ai.evil.example")))
        self.assertIsNone(campaign_link(LINK.replace("https", "http")))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.sync_logs = lambda force=False: None
        self.adapter = HearThis(self.store)
        self.sid = self.store.add_source("hearthis", "Free", LINK)
        self.file = parse_campaign(PAGE)["tracks"][0]["files"][0]

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_one_source_per_campaign_newest_mail_wins(self):
        new = self.store.add_source("hearthis", "Free reminder", LINK.replace("00000000", "11111111"))
        ages = {self.sid: 9.0, new: 2.0}
        with mock.patch.object(hearthis, "newest_offer_age", side_effect=lambda store, sid: ages[sid]):
            self.assertEqual(self.adapter.campaigns(), [new])
            ages[new] = 40.0
            self.assertEqual(self.adapter.campaigns(), [self.sid])

    def test_no_review_without_permission(self):
        with mock.patch.object(self.adapter.session, "request") as request:
            with self.assertRaisesRegex(RuntimeError, "explicit feedback permission"):
                self.adapter.review(self.sid, self.file, "00000000")
        request.assert_not_called()

    def test_review_sent_once_and_never_resent_when_uncertain(self):
        self.adapter.unlock(self.sid, {})
        with mock.patch.object(self.adapter.session, "request", return_value=answer(body={"ok": True})) as request:
            self.adapter.review(self.sid, self.file, "00000000")
            self.adapter.review(self.sid, self.file, "00000000")
        self.assertEqual(request.call_count, 1)
        sent = request.call_args.kwargs["json"]
        self.assertEqual(sent["token"], "00000000")
        self.assertNotIn("support", sent["review_text"].lower())

        other = dict(self.file, id="777")
        with mock.patch.object(self.adapter.session, "request", return_value=answer(500)):
            with self.assertRaisesRegex(RuntimeError, "inspect before retrying"):
                self.adapter.review(self.sid, other, "00000000")
        with mock.patch.object(self.adapter.session, "request") as request:
            with self.assertRaisesRegex(RuntimeError, "uncertain outcome"):
                self.adapter.review(self.sid, other, "00000000")
        request.assert_not_called()

    def test_download_refuses_a_page_instead_of_audio(self):
        reviewed = dict(self.file, reviewed=True)
        page = answer(**{"Content-Type": "text/html"})
        with mock.patch.object(self.adapter.session, "request", side_effect=[answer(body={"ok": True}), page]):
            with self.assertRaisesRegex(RuntimeError, "page instead of audio"):
                self.adapter.download(self.sid, reviewed, Path(self.tmp.name) / "x.wav")
        self.assertEqual(list(Path(self.tmp.name).glob("x.wav*")), [])

    def test_requests_never_leave_hearthis(self):
        with self.assertRaisesRegex(RuntimeError, "left hearthis.ai"):
            self.adapter._request("GET", "https://evil.example/x")


if __name__ == "__main__":
    unittest.main()
