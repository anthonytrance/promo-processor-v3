import html
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import promodelivery as platform
from promo_automation import Store
from promo_pipeline import GateRequired, collect_release


def page():
    rows = [{"track_ID": 1, "track_artist": "Alpha", "track_title": "Song", "track_mix": "Extended Mix"},
            {"track_ID": 2, "track_artist": "Alpha", "track_title": "Song", "track_mix": "Radio Edit"}]
    inputs = {"trackInfoData": json.dumps(rows), "promoID": "12", "reactionID": "34", "reactionSubmitted": "0",
              "cloudfrontLink": "https://test.cloudfront.net"}
    return ('<div id="release_info"><div id="release_title_div"><span title="Alpha - Song [Label]">Song</span></div>'
            '<div class="info-row"><div class="label">Label</div><div class="value">Label</div></div></div>'
            '<input name="favmix" value="Alpha - Song (Extended Mix)">'
            '<input name="favmix" value="Alpha - Song (Radio Edit)">'
            '<a class="downlink" href="promos/pack_WAV.zip&amp;u=fake">WAV</a>'
            '<a class="downlink" href="promos/pack_320.zip&amp;u=fake">MP3</a>'
            + ''.join(f'<input id="{key}" value="{html.escape(value, quote=True)}">' for key, value in inputs.items()))


class PromoDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        patch = mock.patch.object(self.store, "sync_logs")
        self.logs = patch.start()
        self.addCleanup(patch.stop)
        self.adapter = platform.PromoDelivery(self.store)
        self.adapter.session = mock.Mock()
        self.sid = self.store.add_source("promo-delivery", "Test", "https://www.promodelivery.co.uk/promo.php?r=34&u=fake")

    def test_recipient_link_rejects_login_consent_and_external_hosts(self):
        self.assertEqual(platform.recipient_link(self.store.source_url(self.sid)), ("34", "fake"))
        for url in ("https://www.promodelivery.co.uk/logout.php?r=34&u=fake", "https://evil.invalid/promo.php?r=34&u=fake",
                    "http://www.promodelivery.co.uk/promo.php?r=34&u=fake", "https://www.promodelivery.co.uk/promo.php?r=bad&u=fake"):
            self.assertIsNone(platform.recipient_link(url))

    def test_parser_uses_full_metadata_and_only_original_archive_links(self):
        release = platform.parse_page(page())
        self.assertEqual(release["label"], "Label")
        self.assertEqual(release["tracks"][0]["name"], "Alpha - Song (Extended Mix)")
        self.assertEqual([f["type"] for f in release["tracks"][0]["files"]], ["wav", "mp3"])
        self.assertTrue(release["gated"])

    def test_changed_schema_or_cdn_does_not_guess(self):
        for text in ("<html>expired</html>", page().replace("test.cloudfront.net", "evil.invalid")):
            with self.assertRaises(RuntimeError):
                platform.parse_page(text)

    def test_snapshot_publishes_intent_before_get_and_reuses_encrypted_inventory(self):
        def request(*args, **kwargs):
            self.logs.assert_called_once()
            return mock.Mock(status_code=200, text=page())
        with mock.patch.object(self.adapter, "request", side_effect=request) as get:
            self.adapter.snapshot(self.sid)
            self.adapter.snapshot(self.sid)
        get.assert_called_once()

    def test_dry_run_neither_downloads_nor_reacts_and_real_run_requires_permission(self):
        release = platform.parse_page(page())
        library = mock.Mock()
        library.matches.return_value = []
        with mock.patch.object(self.adapter, "release", return_value=release), \
                mock.patch.object(self.adapter, "download") as download, mock.patch.object(self.adapter, "unlock") as unlock:
            result = collect_release(self.store, self.adapter, self.sid, library, dry_run=True)
            self.assertEqual([t["action"] for t in result["tracks"]], ["download", "not_wanted"])
            with self.assertRaises(GateRequired):
                collect_release(self.store, self.adapter, self.sid, library)
        download.assert_not_called()
        unlock.assert_not_called()

    def test_reaction_is_saved_once_has_no_support_claim_and_uncertain_result_blocks_resend(self):
        release = platform.parse_page(page())
        with mock.patch.object(self.adapter, "request", return_value=mock.Mock(status_code=200, text="1")) as post:
            self.adapter.unlock(self.sid, release)
            self.adapter.unlock(self.sid, release)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["data"]["support"], 0)
        self.store.db.execute("UPDATE feedback SET status='submitting'")
        self.store.db.commit()
        with mock.patch.object(self.adapter, "request") as post:
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                self.adapter.unlock(self.sid, release)
        post.assert_not_called()

    def test_lost_reaction_response_never_submits_twice(self):
        release = platform.parse_page(page())
        with mock.patch.object(self.adapter, "request", side_effect=RuntimeError("response lost")) as post:
            with self.assertRaisesRegex(RuntimeError, "response lost"):
                self.adapter.unlock(self.sid, release)
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                self.adapter.unlock(self.sid, release)
        post.assert_called_once()

    def test_original_redirect_cannot_leave_page_cdn(self):
        self.adapter.session.request.return_value = mock.Mock(status_code=302, headers={"Location": "https://evil.invalid/file.zip"})
        with self.assertRaisesRegex(RuntimeError, "left its original hosts"):
            self.adapter.request("GET", "download.php?fn=fake", cdn_host="test.cloudfront.net")
        self.assertEqual(self.adapter.session.request.call_count, 1)

    def test_zip_tracks_match_by_mix_instead_of_archive_order_and_ambiguity_blocks(self):
        paths = [Path("02 Alpha - Song (Radio Edit).wav"), Path("01 Alpha - Song (Extended Mix).wav")]
        self.assertEqual(platform.archive_track(paths, "Alpha - Song (Extended Mix)", "wav"), paths[1])
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            platform.archive_track(paths + [paths[1]], "Alpha - Song (Extended Mix)", "wav")


if __name__ == "__main__":
    unittest.main()
