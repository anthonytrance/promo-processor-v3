"""Offline incremental discovery and reconciliation, with no platform or Dropbox access."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import promobox
import promo_update as update
from promo_automation import Store


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.db.close)
        self.sync = mock.patch.object(self.store, "sync_logs").start()
        self.addCleanup(mock.patch.stopall)
        self.sid = self.store.add_source("promobox", "Label: Alpha - Song", "https://promo-box.com/promo?dj=1&id=fixture",
                                         remote_id="123", sent="2026-10-24", catalogue_signature="revision-one")
        self.adapter = promobox.PromoBox.__new__(promobox.PromoBox)
        self.adapter.store, self.adapter.calls, self.adapter.session = self.store, 0, mock.Mock()
        self.adapter.session.cookies.get_dict.return_value = {}
        self.adapter.session.get.side_effect = AssertionError("unexpected platform request")
        self.cache = self.root / f"promobox-{self.sid}.dpapi"
        self.saved = {"release": {"release_id": "123", "title": "Label: Alpha - Song", "label": "Label", "gated": False,
                                  "tracks": [{"name": "Alpha - Song (Extended Mix)", "track_id": "10",
                                              "files": [{"id": "10", "type": "wav"}]}]},
                      "cookies": {}, "fetched_at": datetime.now(timezone.utc).isoformat(),
                      "catalogue_signature": "revision-one", "mail_marker": self.adapter._mail_marker(self.sid)}
        # Synthetic caches only, never real encrypted account state.
        mock.patch.object(promobox, "read_protected_json", side_effect=lambda path: json.loads(path.read_text())).start()
        mock.patch.object(promobox, "protect_json", side_effect=lambda path, data: path.write_text(json.dumps(data))).start()
        self.write_cache()

    def write_cache(self):
        self.cache.write_text(json.dumps(self.saved))

    def set_metadata(self, **changes):
        metadata = json.loads(self.store.db.execute("SELECT metadata FROM sources WHERE id=?", (self.sid,)).fetchone()[0])
        metadata.update(changes)
        self.store.db.execute("UPDATE sources SET metadata=? WHERE id=?", (json.dumps(metadata), self.sid))
        self.store.db.commit()

    def plan_file(self, action="skip", state="planned"):
        update.save(self.root / "promobox-plan.json", {"started": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "releases": [{"source_id": self.sid, "release": "Song", "state": state,
                          "plan": {"tracks": [{"track": "Alpha - Song (Extended Mix)", "action": action}]}}]})

    def test_unchanged_inventory_reuses_complete_cache_without_requests_or_log_publication(self):
        with mock.patch.object(self.adapter, "snapshot") as fresh:
            for _ in range(2):
                self.assertEqual(self.adapter.prepare_planning(self.sid), {"source": "cache"})
        fresh.assert_not_called()
        self.sync.assert_not_called()
        self.adapter.session.get.assert_not_called()

    def test_changed_reoffer_or_link_invalidates_signature_but_download_status_does_not(self):
        self.set_metadata(status="downloaded")
        with mock.patch.object(self.adapter, "snapshot") as fresh:
            self.assertEqual(self.adapter.prepare_planning(self.sid)["source"], "cache")
            self.set_metadata(catalogue_signature="reoffered-or-edited")
            self.assertEqual(self.adapter.prepare_planning(self.sid)["source"], "fresh")
        fresh.assert_called_once_with(self.sid, refresh=True)
        base = ("123", "Label: Alpha - Song", "2026-10-23", "https://promo-box.com/promo?dj=1&id=one")
        sig = promobox.catalogue_signature(*base)
        for changed in (("123", base[1], "2026-10-24", base[3]),
                        ("123", "Label: Alpha - Song Remixes", base[2], base[3]),
                        ("123", base[1], base[2], base[3] + "new")):
            self.assertNotEqual(sig, promobox.catalogue_signature(*changed))

    def test_new_email_invalidates_even_when_catalogue_is_unchanged(self):
        self.store.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
                              ("fixture", "INBOX", "1", "42", "new-offer", "Song", "Sat, 24 Oct 2026 12:00:00 +0000", self.sid))
        self.store.db.commit()
        with mock.patch.object(self.adapter, "snapshot") as fresh:
            self.assertEqual(self.adapter.prepare_planning(self.sid)["source"], "fresh")
        fresh.assert_called_once_with(self.sid, refresh=True)

    def test_missing_corrupt_stale_incomplete_and_wrong_release_cache_refresh(self):
        original = json.loads(json.dumps(self.saved))
        cases = ("missing", "corrupt", "stale", "incomplete", "wrong-release", "future", "legacy")
        for case in cases:
            with self.subTest(case=case):
                self.saved = json.loads(json.dumps(original))
                if case == "stale":
                    self.saved["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
                elif case == "future":
                    self.saved["fetched_at"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                elif case == "incomplete":
                    self.saved["release"]["tracks"] = []
                elif case == "wrong-release":
                    self.saved["release"]["release_id"] = "999"
                elif case == "legacy":
                    self.saved.pop("catalogue_signature")
                self.write_cache()
                if case == "missing":
                    self.cache.unlink()
                elif case == "corrupt":
                    self.cache.write_text("invalid")
                with mock.patch.object(self.adapter, "snapshot") as fresh:
                    self.assertEqual(self.adapter.prepare_planning(self.sid)["source"], "fresh")
                fresh.assert_called_once_with(self.sid, refresh=True)

    def test_discovery_uses_adapter_cache_hook_and_explicit_refresh_is_forwarded(self):
        self.adapter.catalogue = mock.Mock(return_value=[])
        self.adapter.coordinator_sources = mock.Mock(return_value=[self.sid])
        with mock.patch.object(update, "adapter_for", return_value=self.adapter), \
                mock.patch.object(self.adapter, "snapshot") as fresh, \
                mock.patch("promo_pipeline.collect_release", return_value={"tracks": [{"action": "skip"}]}) as collect:
            update.discover_pass(self.store, "promobox", self.root, 30)
            fresh.assert_not_called()
            update.discover_pass(self.store, "promobox", self.root, 30, refresh_metadata=True)
            fresh.assert_called_once_with(self.sid, refresh=True)
        self.assertEqual(collect.call_count, 2)

    def test_matching_catalogue_migrates_a_recent_verified_legacy_inventory(self):
        self.saved.pop("catalogue_signature")
        self.saved.pop("fetched_at")
        self.saved.pop("mail_marker")
        self.write_cache()
        self.store.event(self.sid, "view", "inspected", tracks=1)
        self.set_metadata(catalogue_signature=None)
        self.adapter.session.get.side_effect = None
        self.adapter.session.get.return_value = mock.Mock(status_code=200, text="/promo?dj=1")
        tile = {"release_id": "123", "url": self.store.source_url(self.sid), "label": "Label", "artist": "Alpha",
                "title": "Song", "sent": datetime(2026, 10, 24, tzinfo=timezone.utc), "status": "downloaded"}
        with mock.patch.object(self.adapter, "login"), mock.patch.object(promobox, "parse_catalogue", return_value=[tile]), \
                mock.patch.object(self.adapter, "snapshot") as fresh:
            self.adapter.catalogue()
            self.assertEqual(self.adapter.prepare_planning(self.sid)["source"], "cache")
        fresh.assert_not_called()
        self.assertEqual(self.adapter.session.get.call_count, 1)

    def test_unverified_legacy_inventory_is_not_migrated(self):
        self.saved.pop("catalogue_signature")
        self.write_cache()
        self.assertFalse(self.adapter._adopt_legacy_cache(self.sid, "123", "revision-one"))
        self.store.event(self.sid, "view", "inspected", tracks=1)
        self.assertFalse(self.adapter._adopt_legacy_cache(self.sid, "999", "revision-one"))

    def test_reconcile_replans_locally_with_current_library_and_no_deferrals(self):
        self.plan_file(action="download")
        update.save(self.root / "global-plan.json", {"defer": {"promobox": {self.sid: {"Alpha - Song": "elsewhere"}}}})
        with mock.patch.object(update, "adapter_for", return_value=self.adapter), \
                mock.patch.object(self.adapter, "snapshot") as fresh, \
                mock.patch("promo_review.review", return_value={"counts": {}}), \
                mock.patch("promo_pipeline.collect_release", return_value={"tracks": [{"action": "skip"}]}) as collect:
            update.collect_pass(self.store, "promobox", self.root, reconcile=True)
        fresh.assert_not_called()
        collect.assert_called_once()
        self.assertTrue(collect.call_args.kwargs["dry_run"])
        self.assertIsNone(collect.call_args.kwargs["defer"])
        report = update.load(self.root / "promobox-reconcile.json")
        self.assertEqual(report["releases"][0]["state"], "nothing_to_collect")

    def test_retry_still_refreshes_before_real_work_and_keeps_new_remix(self):
        self.plan_file(state="exception")
        plan = {"tracks": [{"track": "Alpha - Song (Extended Mix)", "action": "skip"},
                            {"track": "Alpha - Song (New Remix)", "action": "download"}]}
        with mock.patch.object(update, "adapter_for", return_value=self.adapter), \
                mock.patch.object(self.adapter, "snapshot") as fresh, \
                mock.patch("promo_review.review", return_value={"counts": {}}), \
                mock.patch("promo_pipeline.collect_release", return_value=plan) as collect:
            update.collect_pass(self.store, "promobox", self.root, reconcile=True)
        fresh.assert_called_once_with(self.sid, refresh=True)
        self.assertEqual(collect.call_count, 2)
        self.assertNotIn("dry_run", collect.call_args.kwargs)

    def test_owned_main_mix_does_not_hide_a_new_remix_in_actual_pipeline(self):
        self.saved["release"]["tracks"].append({"name": "Alpha - Song (New Remix)", "track_id": "11",
                                                "files": [{"id": "11", "type": "wav"}]})
        self.write_cache()
        library = mock.Mock()
        def decide(store, library, key, track, **kwargs):
            return ("download", "new mix", {}) if "New Remix" in track["name"] else ("skip", "owned", {})
        with mock.patch("promo_pipeline.decide", side_effect=decide):
            result = self.adapter.collect_incremental(self.sid, library, dry_run=True)
        self.assertEqual({t["track"]: t["action"] for t in result["tracks"]},
                         {"Alpha - Song (Extended Mix)": "skip", "Alpha - Song (New Remix)": "download"})
        self.adapter.session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
