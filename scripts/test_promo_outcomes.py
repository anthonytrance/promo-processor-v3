"""Unavailable offers, irrelevant links and real failures must remain separate."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ampsuite
import fatdrop
import labelengine
import promo_outcomes as outcomes
import promo_update as update
import promoly
import soundcloud
from promo_automation import Store


class OutcomeTests(unittest.TestCase):
    def test_typed_outcomes_do_not_hide_network_or_transfer_errors(self):
        for error, state in ((outcomes.ReleaseUnavailable("gone"), "unavailable"),
                             (outcomes.AccessRequired("permission"), "access_blocked"),
                             (outcomes.NotARelease("profile"), "ignored_link"),
                             (RuntimeError("CDN HTTP 404"), "exception"),
                             (TimeoutError("transfer timed out"), "exception"),
                             (RuntimeError("Incomplete planning inventory"), "exception")):
            with self.subTest(state=state, error=str(error)):
                self.assertEqual(update.classify(error), state)

    def test_legacy_generic_400_and_unresolved_link_do_not_prove_expiry(self):
        self.assertEqual(outcomes.reviewed_state("label-engine", {
            "state": "exception", "reason": "Label Engine promo expired or removed (API HTTP 400)"}), "exception")
        self.assertEqual(outcomes.reviewed_state("soundcloud", {
            "state": "exception", "reason": "SoundCloud link did not resolve (resolve HTTP 404)"},
            "https://soundcloud.com/example/track"), "exception")

    def test_metadata_review_uses_explicit_provider_evidence(self):
        row = {"state": "exception", "reason": "schema or expired"}
        self.assertEqual(outcomes.reviewed_state("ampsuite", row, evidence={"status": 200,
            "availability_text": ["This promotion has expired"]}), "unavailable")
        self.assertEqual(outcomes.reviewed_state("label-engine", row, evidence={"status": 400,
            "api_body": "[]"}), "unavailable")
        self.assertEqual(outcomes.reviewed_state("label-engine", row, evidence={"status": 400,
            "api_body": '{"error":"bad parameters"}'}), "exception")

    def test_access_denied_is_not_a_removed_share(self):
        detail = SimpleNamespace(is_shared_link_not_found=lambda: False,
                                 is_shared_link_access_denied=lambda: True)
        self.assertIsInstance(outcomes.dropbox_metadata_error(SimpleNamespace(error=detail)), outcomes.AccessRequired)
        detail.is_shared_link_not_found = lambda: True
        self.assertIsInstance(outcomes.dropbox_metadata_error(SimpleNamespace(error=detail)), outcomes.ReleaseUnavailable)

    def test_profile_and_concatenated_html_links_are_ignored_without_requests(self):
        obj = soundcloud.SoundCloud.__new__(soundcloud.SoundCloud)
        obj._api = mock.Mock()
        for url in ("https://soundcloud.com/example", "https://site.example<br>www.soundcloud.com/example"):
            with self.assertRaises(outcomes.NotARelease):
                obj.resolve(url)
        obj._api.assert_not_called()

    def test_removed_canonical_track_is_unavailable_but_server_errors_fail(self):
        obj = soundcloud.SoundCloud.__new__(soundcloud.SoundCloud)
        obj._api = mock.Mock(return_value=SimpleNamespace(status_code=404))
        with self.assertRaises(outcomes.ReleaseUnavailable):
            obj.resolve("https://soundcloud.com/example/missing-track")
        obj._api.return_value.status_code = 503
        with self.assertRaises(RuntimeError) as caught:
            obj.resolve("https://soundcloud.com/example/track")
        self.assertNotIsInstance(caught.exception, outcomes.ReleaseUnavailable)

    def test_short_link_404_keeps_throttling_or_malformed_link_investigation_open(self):
        obj = soundcloud.SoundCloud.__new__(soundcloud.SoundCloud)
        obj._send = mock.Mock(return_value=SimpleNamespace(status_code=404, headers={}))
        obj._api = mock.Mock()
        with mock.patch.object(soundcloud.time, "sleep"), self.assertRaises(RuntimeError) as caught:
            obj.resolve("https://on.soundcloud.com/abcdefghijk")
        self.assertNotIsInstance(caught.exception, outcomes.ReleaseUnavailable)
        obj._api.assert_not_called()

    def test_ampsuite_expiry_is_distinct_from_a_changed_page(self):
        with self.assertRaises(outcomes.ReleaseUnavailable):
            ampsuite.parse_release("<p>This promotion has expired</p>", "https://example.ampsuite.com/promotion/leave_feedback")
        with self.assertRaises(RuntimeError) as caught:
            ampsuite.parse_release("<p>Unexpected page</p>", "https://example.ampsuite.com/promotion/leave_feedback")
        self.assertNotIsInstance(caught.exception, outcomes.ReleaseUnavailable)

    def test_promoly_deleted_offer_is_distinct_from_a_cdn_failure(self):
        with self.assertRaises(outcomes.ReleaseUnavailable):
            promoly.normalize_release({"campaign": {"archived": True}})
        obj = promoly.Promoly.__new__(promoly.Promoly)
        obj.calls = 0
        response = mock.Mock(status_code=404)
        obj.session = mock.Mock()
        obj.session.request.return_value = response
        with self.assertRaises(outcomes.ReleaseUnavailable):
            obj.request("GET", "/api/landers/example")
        with self.assertRaises(RuntimeError) as caught:
            obj.request("GET", "/api/landers/example/presigneds")
        self.assertNotIsInstance(caught.exception, outcomes.ReleaseUnavailable)

    def test_disco_campaign_copies_are_ignored_but_short_links_are_followed(self):
        import disco
        obj = disco.Disco.__new__(disco.Disco)
        obj.calls = 0
        obj.store = mock.Mock(root=Path(tempfile.gettempdir()) / "no-such-disco-cache")
        obj.store.db.execute.return_value.fetchone.return_value = (1,)
        obj.store.source_url.return_value = "https://label.disco.ac/view-campaign-email/abc/123/"
        obj.session = mock.Mock()
        with self.assertRaises(outcomes.NotARelease):
            obj.snapshot("sid")
        obj.session.get.assert_not_called()
        obj.store.source_url.return_value = "https://s.disco.ac/shortcode"
        obj.session.get.return_value = SimpleNamespace(status_code=302, close=lambda: None,
                                                       headers={"Location": "https://label.disco.ac/share-new/1/x"})
        obj._get = mock.Mock(return_value=SimpleNamespace(status_code=404))
        with self.assertRaises(RuntimeError) as caught:
            obj.snapshot("sid")
        self.assertIn("HTTP 404", str(caught.exception))
        obj._get.assert_called_once_with("https://label.disco.ac/share-new/1/x")

    def test_artwork_only_share_is_not_a_release_but_empty_share_still_fails(self):
        import dropbox_promos
        obj = dropbox_promos.DropboxPromos.__new__(dropbox_promos.DropboxPromos)
        obj.store, obj.codes = mock.Mock(root=Path(tempfile.gettempdir()) / "no-such-dropbox-cache"), {}
        obj.snapshot = mock.Mock(return_value={"files": [{"name": "Cover.jpg", "id": "1"}, {"name": "EPK.pdf", "id": "2"}]})
        with self.assertRaises(outcomes.NotARelease):
            obj.release("sid")
        obj.snapshot.return_value = {"files": []}
        try:
            obj.release("sid")
        except outcomes.NotARelease:
            self.fail("an empty share may be removed music and must not be ignored")
        except Exception:
            pass

    def test_legacy_disco_and_cut_off_drive_links_are_reviewed(self):
        row = {"state": "exception", "reason": "Not a Disco recipient share link (campaign e-mail copies carry no tracks)"}
        self.assertEqual(outcomes.reviewed_state("disco", row, "https://x.disco.ac/view-campaign-email/a/1/"), "ignored_link")
        self.assertEqual(outcomes.reviewed_state("disco", row, "https://s.disco.ac/abc"), "exception")
        self.assertEqual(outcomes.reviewed_state("google-drive", {"state": "exception", "reason":
            "Google Drive link carries no file or folder id (cut off in the mail)"}), "ignored_link")


class FatdropLimitTests(unittest.TestCase):
    def test_first_original_is_bounded_and_only_its_verified_delivery_lifts_limit(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            try:
                obj = fatdrop.Fatdrop.__new__(fatdrop.Fatdrop)
                obj.store = store
                self.assertTrue(obj.collection_enabled)
                self.assertEqual(obj.collection_limit(), 1)
                other = store.add_source("other", "Example", "https://example.invalid/other")
                store.claim("other", other, "Other", "wav")
                store.asset_update("other", "delivery_verified", dropbox_path="/Music/Other.aiff")
                self.assertEqual(obj.collection_limit(), 1)
                sid = store.add_source("fatdrop", "Example", "https://promo.fatdrop.co.uk/e/example-recipient")
                store.claim("one", sid, "One", "wav")
                store.asset_update("one", "held_for_research", dropbox_path="/Music/audit/One.aiff")
                self.assertEqual(obj.collection_limit(), 1)
                store.asset_update("one", "delivery_verified", dropbox_path="/Music/One.aiff", content_hash="verified")
                self.assertIsNone(obj.collection_limit())
            finally:
                store.db.close()

    def test_coordinator_applies_first_original_limit_then_reconciles_remaining_work(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            try:
                run = Path(root)
                (run / "fatdrop-plan.json").write_text(json.dumps({"releases": [
                    {"source_id": sid, "release": sid, "state": "planned", "plan": {"tracks": [
                        {"track": sid, "action": "download"}]}} for sid in ("one", "two")]}), encoding="utf-8")
                adapter = SimpleNamespace(collection_limit=mock.Mock(side_effect=[1, None]))
                collect = mock.Mock(return_value={"tracks": [{"delivered": "One.aiff"}]})
                with mock.patch.object(update, "adapter_for", return_value=adapter), \
                        mock.patch("promo_incremental.PlanningCache", return_value=SimpleNamespace(supported=False)), \
                        mock.patch("promo_pipeline.collect_release", collect), mock.patch.object(store, "sync_logs"):
                    update.collect_pass(store, "fatdrop", run)
                self.assertEqual(collect.call_args_list[0].kwargs["max_assets"], 1)
                self.assertNotIn("max_assets", collect.call_args_list[1].kwargs)
            finally:
                store.db.close()
