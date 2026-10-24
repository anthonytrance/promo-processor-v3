import tempfile
import unittest
from pathlib import Path
from unittest import mock

import promopush
from promopush import PromoPush, campaign_query, fold_version, original_format, parse_campaign
from promo_automation import Store

PAGE = """<html><body>
<div id="forgotModal"><h4>Reset Your Password</h4></div>
<div class="col-sm-8"><h2 class="darkgrey small-top" id="19702">SNAJDER X MARQUS</h2><h3>Own The Night</h3>
<h4>Warner Music, Denmark</h4><p class="rel-info"></p><p class="rel-info">Release Date: Oct. 30, 2026</p></div>
<table><tr class="mix_tr" data-track-id="52034"><td><span>Own The Night (Extended)</span></td><td><span>SNAJDER X MARQUS</span></td>
<td><a class="download_track" data-href="/check_download_mix/52034/"></a></td></tr>
<tr class="mix_tr" data-track-id="52032"><td><span>Own The Night (AKI-HIRO Remix) Radio Edit</span></td><td><span>SNAJDER X MARQUS</span></td>
<td><a class="download_track" data-href="/check_download_mix/52032/"></a></td></tr></table>
<form name="react_19702"><input type="hidden" name="rating_val" value="{rated}" /></form></body></html>"""
LINK = "https://promopush.com/?track_id=19702&uuid=a1b2c3d4-0000-4000-8000-000000000000"


class ParseTests(unittest.TestCase):
    def test_campaign_page_label_mixes_and_gate(self):
        release = parse_campaign(PAGE.replace("{rated}", "0"))
        self.assertEqual(release["label"], "Warner Music, Denmark")  # not the dialog's h4
        self.assertEqual(release["package"], "19702")
        self.assertEqual(release["release_date"], "Oct. 30, 2026")
        self.assertTrue(release["gated"])
        self.assertEqual([t["name"] for t in release["tracks"]],
                         ["SNAJDER X MARQUS - Own The Night (Extended)",
                          "SNAJDER X MARQUS - Own The Night (AKI-HIRO Remix Radio Edit)"])
        self.assertFalse(parse_campaign(PAGE.replace("{rated}", "7"))["gated"])

    def test_fold_and_format(self):
        self.assertEqual(fold_version("Still (Airscape Remix) Extended"), "Still (Airscape Remix Extended)")
        self.assertEqual(fold_version("Light (Remix) Clubbers"), "Light (Remix) Clubbers")
        self.assertEqual(original_format("https://promopush.s3.amazonaws.com/wave_images/c8c6.wav_w.png"), "wav")
        self.assertEqual(original_format(None), "original")

    def test_link_parsing_refuses_other_hosts_and_shapes(self):
        self.assertEqual(campaign_query(LINK), ("19702", "a1b2c3d4-0000-4000-8000-000000000000"))
        self.assertIsNone(campaign_query("https://promopush.com/?utm=1"))
        self.assertIsNone(campaign_query(LINK.replace("promopush.com", "promopush.com.evil.example")))
        self.assertIsNone(campaign_query(LINK.replace("https", "http")))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.sync_logs = lambda force=False: None
        self.adapter = PromoPush(self.store)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_one_source_per_campaign_newest_mail_wins(self):
        old = self.store.add_source("promopush", "Own The Night", LINK)
        new = self.store.add_source("promopush", "Own The Night", LINK.replace("a1b2c3d4", "ffffffff"))
        copy = self.store.add_source("promopush", "Own The Night",
                                     LINK.replace("promopush.com/?", "promopush.com/campaign_email_inbrowser/?"))
        ages = {old: 20.0, new: 3.0, copy: 1.0}
        with mock.patch.object(promopush, "newest_offer_age", side_effect=lambda store, sid: ages[sid]):
            self.assertEqual(self.adapter.campaigns(), [new])
            ages[new] = 40.0
            self.assertEqual(self.adapter.campaigns(), [old])

    def test_reaction_is_saved_first_sent_once_and_never_resent_when_uncertain(self):
        sid = self.store.add_source("promopush", "Own The Night", LINK)
        release = parse_campaign(PAGE.replace("{rated}", "0"))
        self.adapter.session.cookies.set("csrftoken", "t", domain="promopush.com")
        ok = mock.Mock(status_code=200, text="OK")
        with mock.patch.object(self.adapter.session, "post", return_value=ok) as post:
            self.adapter.unlock(sid, release)
            self.adapter.unlock(sid, release)
        self.assertEqual(post.call_count, 1)
        sent = post.call_args.kwargs["data"]
        self.assertEqual((sent["favourite_mix"], sent["package_id"]), ("52034", "19702"))
        self.assertTrue(7 <= sent["rating"] <= 9)
        self.assertNotIn("support", sent)
        self.assertNotIn("radio_support", sent)

        other = self.store.add_source("promopush", "Other", LINK.replace("a1b2c3d4", "bbbbbbbb"))
        with mock.patch.object(self.adapter.session, "post", return_value=mock.Mock(status_code=500, text="")):
            with self.assertRaisesRegex(RuntimeError, "inspect before retrying"):
                self.adapter.unlock(other, release)
        with mock.patch.object(self.adapter.session, "post") as post:
            with self.assertRaisesRegex(RuntimeError, "uncertain outcome"):
                self.adapter.unlock(other, release)
        post.assert_not_called()

    def test_download_refuses_a_page_instead_of_audio(self):
        sid = self.store.add_source("promopush", "Own The Night", LINK)
        page = mock.Mock(status_code=200, headers={"Content-Type": "text/html"})
        with mock.patch.object(self.adapter, "_get", side_effect=[mock.Mock(status_code=200), page]):
            with self.assertRaisesRegex(RuntimeError, "page instead of audio"):
                self.adapter.download(sid, {"id": "52034"}, Path(self.tmp.name) / "x.wav")
        self.assertEqual(list(Path(self.tmp.name).glob("x.wav*")), [])

    def test_redirect_off_site_is_refused(self):
        hop = mock.Mock(status_code=302, headers={"Location": "https://evil.example/x"})
        with mock.patch.object(promopush, "PAUSE", 0), \
                mock.patch.object(self.adapter.session, "get", return_value=hop):
            with self.assertRaisesRegex(RuntimeError, "off-site"):
                self.adapter._get(LINK)


if __name__ == "__main__":
    unittest.main()
