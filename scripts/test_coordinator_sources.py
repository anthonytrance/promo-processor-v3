"""Offline regressions for account catalogues used by the combined coordinator."""
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import ctrlpromo
import promobox
import promo_update as update
from promo_automation import Store


class FixedTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 24, 10, tzinfo=timezone.utc)


class SourceContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.db.close)
        p = mock.patch.object(self.store, "sync_logs")
        p.start()
        self.addCleanup(p.stop)

    def box_source(self, remote, sent, subject="Example", **metadata):
        url = "https://promo-box.com/promo?dj=42&id=fake" + str(remote or subject)
        return self.store.add_source("promobox", subject, url, remote_id=remote, sent=sent, **metadata)

    def test_ctrl_selector_uses_flat_ids_without_discarding_standalone_age_report(self):
        recent = self.store.add_source("ctrl-promo", "Recent", "https://ctrlpromo.com/view-promo/101/" + "k" * 40)
        old = self.store.add_source("ctrl-promo", "Old", "https://ctrlpromo.com/view-promo/202")
        adapter = ctrlpromo.CtrlPromo.__new__(ctrlpromo.CtrlPromo)
        adapter.store = self.store
        with mock.patch.object(ctrlpromo, "newest_offer_age", side_effect=lambda s, sid: 2 if sid == recent else 73):
            selected, outside = adapter.campaigns()
            rows = update.window_sources(self.store, adapter, "ctrl-promo", 30)
        self.assertEqual(selected, [recent])
        self.assertEqual(outside[0]["source_id"], old)
        self.assertEqual({sid: status for sid, _, status in rows}, {recent: None, old: "outside_window_or_alias"})

    def test_promobox_selects_recent_catalogue_releases_without_mail_and_excludes_digests(self):
        recent = self.box_source("123", "2026-10-23")
        old = self.box_source("124", "2026-08-01")
        undated = self.box_source("125", None)
        invalid = self.box_source("not-a-release-id", "2026-10-23")
        digest = self.box_source(None, "2026-10-23", subject="Email digest")
        adapter = promobox.PromoBox.__new__(promobox.PromoBox)
        adapter.store = self.store
        with mock.patch.object(promobox, "datetime", FixedTime), \
                mock.patch("promo_mail.newest_offer_age", side_effect=lambda s, sid: 1 if sid == digest else None):
            rows = update.window_sources(self.store, adapter, "promobox", 30)
        self.assertEqual({sid for sid, _, status in rows if status is None}, {recent})
        self.assertTrue(all(status == "outside_window_or_alias" for sid, _, status in rows if sid in {old, undated, invalid, digest}))

    def test_recent_mail_for_identified_release_can_refresh_old_catalogue_offer(self):
        recent = self.box_source("123", "2026-08-01")
        adapter = promobox.PromoBox.__new__(promobox.PromoBox)
        adapter.store = self.store
        with mock.patch.object(promobox, "datetime", FixedTime), mock.patch("promo_mail.newest_offer_age", return_value=1):
            self.assertEqual(adapter.coordinator_sources(), [recent])

    def test_catalogue_refresh_updates_existing_date_and_keeps_other_evidence(self):
        sid = self.box_source("123", "2026-08-01", origin="existing evidence")
        adapter = promobox.PromoBox.__new__(promobox.PromoBox)
        adapter.store, adapter.calls, adapter.session = self.store, 0, mock.Mock()
        adapter.session.get.return_value = mock.Mock(status_code=200, text="/promo?dj=42")
        tile = {"release_id": "123", "url": "https://promo-box.com/promo?dj=42&id=current-fake",
                "label": "Example", "artist": "Alpha", "title": "Song", "status": "new",
                "sent": FixedTime(2026, 10, 23, tzinfo=timezone.utc)}
        with mock.patch.object(adapter, "login"), mock.patch.object(promobox, "protect_json"), \
                mock.patch.object(promobox, "parse_catalogue", return_value=[tile]):
            adapter.catalogue()
        metadata = json.loads(self.store.db.execute("SELECT metadata FROM sources WHERE id=?", (sid,)).fetchone()[0])
        self.assertEqual(metadata["sent"], "2026-10-23")
        self.assertEqual(metadata["status"], "new")
        self.assertEqual(metadata["origin"], "existing evidence")

    def test_nonflat_chooser_leaves_failed_plan_instead_of_running_empty_success(self):
        self.store.add_source("ctrl-promo", "Example", "https://ctrlpromo.com/view-promo/101")
        adapter = mock.Mock(spec=["campaigns"])
        adapter.campaigns.return_value = (["source"], [{"age_days": 73}])
        with mock.patch.object(update, "adapter_for", return_value=adapter), \
                mock.patch("promo_pipeline.collect_release") as collect:
            with self.assertRaisesRegex(RuntimeError, "flat list"):
                update.discover_pass(self.store, "ctrl-promo", self.root, 30)
        report = json.loads((self.root / "ctrl-promo-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(report["state"], "failed")
        self.assertIn("flat list", report["source_selection_error"])
        collect.assert_not_called()

    def test_summary_includes_failed_phase_even_when_there_are_no_release_rows(self):
        (self.root / "run.json").write_text(json.dumps({"started": "2026-10-24T00:00:00+00:00", "destination": "/test",
            "phases": [{"phase": "discover", "platform": "ctrl-promo", "state": "failed", "exit_code": 1}]}), encoding="utf-8")
        (self.root / "ctrl-promo-plan.json").write_text(json.dumps({"releases": [], "state": "failed"}), encoding="utf-8")
        with mock.patch.object(update, "STATE", self.root):
            result = update.summary(self.store, self.root, ("ctrl-promo",))
        self.assertEqual(result["phase_failures"], [{"phase": "discover", "platform": "ctrl-promo", "exit_code": 1}])
        self.assertIn("ctrl-promo discover", result["text"])
        self.assertIn("coverage is incomplete", result["text"])

    def test_unknown_chosen_id_is_an_error_instead_of_silently_skipping_all_sources(self):
        self.store.add_source("ctrl-promo", "Example", "https://ctrlpromo.com/view-promo/101")
        adapter = mock.Mock(spec=["coordinator_sources"])
        adapter.coordinator_sources.return_value = ["unregistered-source"]
        with self.assertRaisesRegex(RuntimeError, "unregistered source IDs"):
            update.window_sources(self.store, adapter, "ctrl-promo", 30)

    def test_promobox_publishes_view_intent_before_opening_recipient_page(self):
        sid = self.box_source("123", "2026-10-23")
        adapter = promobox.PromoBox.__new__(promobox.PromoBox)
        adapter.store, adapter.calls, adapter.session = self.store, 0, mock.Mock()
        def get(*args, **kwargs):
            self.store.sync_logs.assert_called_once()
            return mock.Mock(status_code=200, text="synthetic page")
        adapter.session.get.side_effect = get
        with mock.patch.object(adapter, "login"), mock.patch.object(promobox, "protect_json"), \
                mock.patch.object(promobox, "parse_release", return_value={"release_id": "123", "tracks": [], "gated": False}):
            adapter.snapshot(sid, refresh=True)
        adapter.session.get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
