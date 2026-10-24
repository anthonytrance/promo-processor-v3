"""Offline coverage for provider-independent metadata reuse, without cached selection decisions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import promo_incremental as inc
from promo_automation import Store


class FixtureAdapter:
    __module__ = "promoly"

    def __init__(self, store, sid, platform="promoly"):
        self.store, self.sid, self.platform = store, sid, platform
        self.native = store.root / f"{inc.PREFIXES[platform]}-{sid}.dpapi"
        self.inventory = {"title": "Alpha - Song", "label": "Label", "gated": False,
                          "tracks": [{"name": "Alpha - Song (Extended Mix)", "files": [{"id": "1", "type": "wav"}]}]}
        self.snapshots, self.probes = [], 0

    def snapshot(self, sid, refresh=False):
        self.snapshots.append(refresh)
        if refresh or not self.native.exists():
            self.native.write_text(json.dumps(self.inventory))
        return json.loads(self.native.read_text())

    def release(self, sid):
        # Models duration/format enrichment that must not be repeated for an unchanged inventory.
        self.probes += 1
        return json.loads(self.native.read_text())


class ManifestFixture(FixtureAdapter):
    __module__ = "dropbox_promos"


class IncrementalCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.db.close)
        p = mock.patch.object(self.store, "sync_logs", side_effect=AssertionError("publication forbidden"))
        p.start()
        self.addCleanup(p.stop)
        for name, side in (("read_protected_json", lambda p: json.loads(p.read_text())),
                           ("protect_json", lambda p, v: p.write_text(json.dumps(v)))):
            p = mock.patch.object(inc, name, side_effect=side)
            p.start()
            self.addCleanup(p.stop)
        self.sid = self.store.add_source("promoly", "Alpha - Song", "https://ln.promo.ly/fixture")
        self.adapter = FixtureAdapter(self.store, self.sid)

    def get(self, **kwargs):
        return inc.PlanningCache(self.store, self.adapter, self.adapter.platform).adapter(self.sid, **kwargs)

    def record(self):
        return self.root / f"planning-{self.adapter.platform}-{self.sid}.dpapi"

    def bind_native(self):
        cache = inc.PlanningCache(self.store, self.adapter, self.adapter.platform)
        binding = {"signature": cache._signature(self.sid),
                   "native_sha256": hashlib.sha256(self.adapter.native.read_bytes()).hexdigest()}
        (self.root / f"planning-bind-{self.adapter.platform}-{self.sid}.dpapi").write_text(json.dumps(binding))

    def test_new_phase_reuses_normalized_tracks_and_avoids_provider_enrichment(self):
        first, metadata = self.get()
        self.assertEqual(metadata["source"], "fresh")
        second, metadata = self.get()
        self.assertEqual(metadata["source"], "cache")
        self.assertEqual(first.release(self.sid), second.release(self.sid))
        self.assertEqual(self.adapter.snapshots, [True])
        self.assertEqual(self.adapter.probes, 1)
        altered = second.release(self.sid)
        altered["tracks"].clear()
        self.assertEqual(len(second.release(self.sid)["tracks"]), 1)

    def test_weekly_reuse_does_not_reset_the_original_age_or_repeat_pages(self):
        self.get()
        record = json.loads(self.record().read_text())
        stamp = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        record["fetched_at"] = stamp
        self.record().write_text(json.dumps(record))
        self.assertEqual(self.get()[1]["source"], "cache")
        self.assertEqual(json.loads(self.record().read_text())["fetched_at"], stamp)
        self.assertEqual(self.adapter.snapshots, [True])

    def test_new_mail_or_catalogue_change_cannot_rebless_old_native_metadata(self):
        self.get()
        self.adapter.inventory["tracks"].append({"name": "Alpha - Song (New Remix)", "files": [{"id": "2", "type": "wav"}]})
        self.store.db.execute("UPDATE sources SET metadata=? WHERE id=?", (json.dumps({"track_count": 2}), self.sid))
        self.store.db.commit()
        planned, metadata = self.get()
        self.assertEqual(metadata["source"], "fresh")
        self.assertEqual(self.adapter.snapshots, [True, True])
        self.assertEqual(len(planned.release(self.sid)["tracks"]), 2)
        self.store.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
            ("fixture", "INBOX", "1", "10", "reminder", "Song", "Sat, 24 Oct 2026 12:00:00 +0000", self.sid))
        self.store.db.commit()
        self.assertEqual(self.get()[1]["source"], "fresh")
        self.assertEqual(self.adapter.snapshots, [True, True, True])

    def test_expired_corrupt_and_explicit_refresh_force_provider_revalidation(self):
        self.get()
        record = json.loads(self.record().read_text())
        record["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        self.record().write_text(json.dumps(record))
        self.assertEqual(self.get()[1]["source"], "fresh")
        self.record().write_text("corrupt")
        self.assertEqual(self.get()[1]["source"], "fresh")
        self.assertEqual(self.get(refresh=True)[1]["source"], "fresh")
        self.assertEqual(self.adapter.snapshots, [True] * 4)

    def test_recent_native_cache_is_migrated_once_without_a_release_page(self):
        self.adapter.snapshot(self.sid, refresh=True)
        self.bind_native()
        self.adapter.snapshots.clear()
        self.assertEqual(self.get()[1]["source"], "native_cache")
        self.assertEqual(self.adapter.snapshots, [False])
        self.assertEqual(self.get()[1]["source"], "cache")
        self.assertEqual(self.adapter.probes, 1)

    def test_no_native_cache_migration_when_a_newer_offer_exists(self):
        self.adapter.snapshot(self.sid, refresh=True)
        self.bind_native()
        self.adapter.snapshots.clear()
        offered = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        self.store.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
            ("fixture", "INBOX", "1", "10", "new-offer", "Song", offered, self.sid))
        self.store.db.commit()
        self.assertEqual(self.get()[1]["source"], "fresh")
        self.assertEqual(self.adapter.snapshots, [True])

    def test_old_native_binding_does_not_bless_changed_catalogue_data(self):
        self.adapter.snapshot(self.sid, refresh=True)
        self.bind_native()
        self.adapter.snapshots.clear()
        self.store.db.execute("UPDATE sources SET metadata=? WHERE id=?", (json.dumps({"track_count": 2}), self.sid))
        self.store.db.commit()
        self.adapter.inventory["tracks"].append({"name": "Alpha - Song (New Remix)", "files": [{"id": "2", "type": "wav"}]})
        result, metadata = self.get()
        self.assertEqual(metadata["source"], "fresh")
        self.assertEqual(len(result.release(self.sid)["tracks"]), 2)
        self.assertEqual(self.adapter.snapshots, [True])

    def test_live_refresh_records_new_remix_before_partial_collection_failure(self):
        cache = inc.PlanningCache(self.store, self.adapter, self.adapter.platform)
        cache.adapter(self.sid)
        self.adapter.inventory["tracks"].append({"name": "Alpha - Song (New Remix)", "files": [{"id": "2", "type": "wav"}]})
        cache.refresh_for_collection(self.sid)
        # A subsequent download may fail after delivering the first track. A fresh phase must retain both tracks.
        planned, metadata = self.get()
        self.assertEqual(metadata["source"], "cache")
        self.assertEqual(len(planned.release(self.sid)["tracks"]), 2)
        self.assertEqual(len(cache.adapter(self.sid)[0].release(self.sid)["tracks"]), 2)

    def test_interrupted_live_refresh_forces_reinspection_instead_of_old_native_adoption(self):
        cache = inc.PlanningCache(self.store, self.adapter, self.adapter.platform)
        cache.adapter(self.sid)
        with mock.patch.object(self.adapter, "snapshot", side_effect=RuntimeError("provider failed")):
            with self.assertRaises(RuntimeError):
                cache.refresh_for_collection(self.sid)
        self.adapter.snapshots.clear()
        self.assertEqual(self.get()[1]["source"], "fresh")
        self.assertEqual(self.adapter.snapshots, [True])

    def test_mutable_manifest_is_checked_each_run_but_unchanged_files_are_not_reprobed(self):
        self.sid = self.store.add_source("dropbox", "Folder", "https://www.dropbox.com/scl/fo/fixture")
        self.adapter = ManifestFixture(self.store, self.sid, "dropbox")
        self.get()
        self.assertEqual(self.get()[1]["source"], "cache")
        self.assertEqual(self.adapter.snapshots, [True, True])
        self.assertEqual(self.adapter.probes, 1)
        self.adapter.inventory["tracks"].append({"name": "Beta - New Song", "files": [{"id": "new", "type": "aiff"}]})
        result, metadata = self.get()
        self.assertEqual(len(result.release(self.sid)["tracks"]), 2)
        self.assertEqual(self.adapter.probes, 2)
        self.assertTrue(metadata["manifest_checked"])

    def test_reconcile_uses_manifest_already_checked_by_its_discovery(self):
        self.sid = self.store.add_source("dropbox", "Folder", "https://www.dropbox.com/scl/fo/fixture")
        self.adapter = ManifestFixture(self.store, self.sid, "dropbox")
        since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self.get()
        self.assertEqual(self.get(inspected_since=since)[1]["source"], "cache")
        self.assertEqual(self.adapter.snapshots, [True])

    def test_fresh_manifest_still_prepares_archive_audio_before_real_work(self):
        self.sid = self.store.add_source("dropbox", "Folder", "https://www.dropbox.com/scl/fo/fixture")
        self.adapter = ManifestFixture(self.store, self.sid, "dropbox")
        self.get()
        cache = inc.PlanningCache(self.store, self.adapter, "dropbox")
        _, metadata = cache.adapter(self.sid)
        expected_manifest = json.loads(self.record().read_text())["manifest"]
        self.adapter.prepare_source = mock.Mock()
        cache.refresh_for_collection(self.sid, already_fresh=metadata["manifest_checked"])
        self.adapter.prepare_source.assert_called_once_with(self.sid)
        self.assertEqual(json.loads(self.record().read_text())["manifest"], expected_manifest)

    def test_inflyte_real_work_refreshes_target_and_preserves_regular_availability_path(self):
        sid = self.store.add_source("inflyte", "Song", "https://inflyteapp.com/review/releases/123")
        real = mock.Mock(spec=["store", "inspect", "release", "planning_release"])
        real.store = self.store
        real.release.return_value = deepcopy(self.adapter.inventory)
        cache = inc.PlanningCache(self.store, real, "inflyte")
        cache.refresh_for_collection(sid)
        real.inspect.assert_called_once_with(sid, "123")
        real.release.assert_called_once_with(sid)
        real.planning_release.assert_not_called()

    def test_inventory_cannot_be_used_for_another_source(self):
        planned, _ = self.get()
        with self.assertRaises(ValueError):
            planned.release("different-source")

    def test_existing_ctrl_recipient_detects_catalogue_edits_but_not_response_status(self):
        import ctrlpromo
        from test_ctrlpromo import CATALOGUE, LINK, Response
        sid = self.store.add_source("ctrl-promo", "Mailed source", LINK)
        real = ctrlpromo.CtrlPromo.__new__(ctrlpromo.CtrlPromo)
        real.store, real.catalogue_path = self.store, self.root / "ctrlpromo-catalogue.dpapi"
        cache = inc.PlanningCache(self.store, real, "ctrl-promo")
        with mock.patch.object(self.store, "sync_logs"), mock.patch.object(real, "_save_session"), \
                mock.patch.object(real, "_get", return_value=Response(text=CATALOGUE)) as fetch:
            real.discover_catalogue()
            first = cache._signature(sid)
            fetch.return_value = Response(text=CATALOGUE.replace('cp-status-new', 'cp-status-done'))
            real.discover_catalogue()
            self.assertEqual(cache._signature(sid), first)
            fetch.return_value = Response(text=CATALOGUE.replace('Fixture Artist - Fixture Song', 'Fixture Artist - New Remixes'))
            real.discover_catalogue()
            self.assertNotEqual(cache._signature(sid), first)
        self.assertEqual(self.store.source_url(sid), LINK)


if __name__ == "__main__":
    unittest.main()
