"""Original archive inventories must not outlive changes to their source files."""
import json
import zipfile
from pathlib import Path
from unittest import mock

import dropbox_promos as dbp
import gdrive_promos as gd
from test_onedrive_promos import OfflineCase


class ArchiveCacheTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.sid = self.source("https://www.dropbox.com/scl/fo/fixture", platform="dropbox")
        self.adapter = dbp.DropboxPromos.__new__(dbp.DropboxPromos)
        self.adapter.store, self.adapter.codes, self.adapter.lengths = self.store, {}, {}
        self.inventory = {"name": "Promo", "files": [self.pack()]}
        self.adapter.snapshot = mock.Mock(side_effect=lambda sid: self.inventory)
        self.transferred = []
        def download(sid, entry, destination):
            self.transferred.append(entry["id"])
            with zipfile.ZipFile(destination, "w") as archive:
                archive.writestr("Artist - " + entry.get("track", "Tune") + ".wav", b"fixture original")
        self.adapter._download_entry = mock.Mock(side_effect=download)
        patch = mock.patch.object(dbp, "read_existing_tags", return_value={})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(dbp.shutil, "disk_usage", return_value=mock.Mock(free=100 * 1024**3))
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def pack(**overrides):
        return {"id": "archive", "name": "Promo.zip", "path": "/Promo.zip", "size": 100, "rev": "one", **overrides}

    @property
    def manifest(self):
        return self.store.root / "dropbox-packs" / self.sid / "inventory.json"

    def complete(self):
        self.adapter.prepare_source(self.sid)
        self.adapter.cleanup_source(self.sid, {"tracks": []})

    def test_completed_unchanged_archive_reuses_tracks_without_transfer(self):
        self.complete()
        self.adapter.prepare_source(self.sid)
        self.assertEqual(self.transferred, ["archive"])
        self.assertEqual(len(self.adapter.release(self.sid)["tracks"]), 1)
        saved = json.loads(self.manifest.read_text())
        self.assertTrue(saved["collected_snapshot"])
        self.assertEqual(saved["archive_fingerprint"], self.adapter.archive_fingerprint(self.inventory))
        self.assertFalse(any(Path(entry["local_path"]).exists() for entry in saved["files"]))

    def test_real_collection_rehydrates_cleaned_archives_and_local_download_succeeds(self):
        self.complete()
        self.adapter.prepare_source(self.sid)
        self.assertEqual(self.transferred, ["archive"])
        self.adapter.prepare_collection_source(self.sid)
        self.assertEqual(self.transferred, ["archive", "archive"])
        file = self.adapter.release(self.sid)["tracks"][0]["files"][0]
        target = self.store.root / "collected.wav"
        size, _ = dbp.DropboxPromos._download_entry(self.adapter, self.sid, file["entry"], target)
        self.assertEqual((size, target.read_bytes()), (len(b"fixture original"), b"fixture original"))
        self.adapter.cleanup_source(self.sid, {"tracks": []})
        self.assertFalse(Path(file["entry"]["local_path"]).exists())

    def test_collection_rehydrates_incomplete_audio_after_partial_failure(self):
        self.adapter.prepare_source(self.sid)
        saved = json.loads(self.manifest.read_text())
        Path(saved["files"][0]["local_path"]).unlink()
        self.adapter.prepare_source(self.sid)
        self.assertEqual(self.transferred, ["archive"])
        self.adapter.prepare_collection_source(self.sid)
        self.assertTrue(Path(saved["files"][0]["local_path"]).is_file())
        self.assertFalse(json.loads(self.manifest.read_text()).get("collected_snapshot", False))

    def test_failed_rehydration_keeps_metadata_without_retaining_audio_and_retries(self):
        self.complete()
        original = self.adapter._download_entry.side_effect
        def failed(sid, entry, destination):
            Path(destination).write_bytes(b"partial")
            raise RuntimeError("fixture transfer interrupted")
        self.adapter._download_entry.side_effect = failed
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.adapter.prepare_collection_source(self.sid)
        self.assertTrue(json.loads(self.manifest.read_text())["collected_snapshot"])
        self.assertFalse(list(self.manifest.parent.rglob("*.zip")))
        self.assertFalse(list(self.manifest.parent.rglob("*.wav")))
        self.adapter._download_entry.side_effect = original
        self.adapter.prepare_collection_source(self.sid)
        file = self.adapter.release(self.sid)["tracks"][0]["files"][0]
        self.assertTrue(Path(file["entry"]["local_path"]).exists())

    def test_replacement_same_id_and_size_invalidates_old_tracks(self):
        self.complete()
        self.inventory["files"] = [self.pack(rev="two", track="New Remix")]
        before_prepare = self.adapter.release(self.sid)
        self.assertEqual(before_prepare["tracks"], [])
        self.assertIn("prepare_source", before_prepare["warnings"][0]["reason"])
        self.adapter.prepare_source(self.sid)
        self.assertEqual(self.transferred, ["archive", "archive"])
        self.assertIn("New Remix", self.adapter.release(self.sid)["tracks"][0]["name"])

    def test_new_sibling_archive_is_not_hidden_and_removed_archive_stops_contributing(self):
        self.complete()
        self.inventory["files"].append(self.pack(id="second", name="Second.zip", path="/Second.zip", track="New Remix"))
        self.adapter.prepare_source(self.sid)
        self.assertEqual(self.transferred, ["archive", "archive", "second"])
        self.assertEqual(len(self.adapter.release(self.sid)["tracks"]), 2)
        self.inventory["files"] = []
        self.adapter.prepare_source(self.sid)
        self.assertFalse(self.manifest.exists())
        self.assertEqual(self.adapter.release(self.sid)["tracks"], [])

    def test_legacy_unbound_manifest_is_rebuilt_once(self):
        self.complete()
        saved = json.loads(self.manifest.read_text())
        del saved["archive_fingerprint"]
        self.manifest.write_text(json.dumps(saved))
        self.adapter.prepare_source(self.sid)
        self.assertEqual(self.transferred, ["archive", "archive"])

    def test_unversioned_archive_is_only_reusable_in_the_current_operation(self):
        self.inventory["files"] = [self.pack(rev=None)]
        self.complete()
        self.adapter.prepare_source(self.sid)
        self.assertEqual(self.transferred, ["archive"])
        self.adapter._prepared_archives.clear()
        self.assertEqual(self.adapter.release(self.sid)["tracks"], [])
        self.adapter.prepare_source(self.sid)
        self.assertEqual(self.transferred, ["archive", "archive"])

    def test_hash_size_revision_and_roster_are_all_fingerprint_evidence(self):
        original = self.adapter.archive_fingerprint(self.inventory)
        for key, value in (("size", 200), ("rev", "two"), ("content_hash", "hash"),
                           ("md5", "md5"), ("version", "v2"), ("modified_time", "2026-01-01")):
            current = {"files": [self.pack(**{key: value})]}
            self.assertNotEqual(original, self.adapter.archive_fingerprint(current), key)
        files = [self.pack(), self.pack(id="second")]
        self.assertEqual(self.adapter.archive_fingerprint({"files": files}),
                         self.adapter.archive_fingerprint({"files": list(reversed(files))}))

    def test_google_drive_inherits_archive_binding_and_records_version(self):
        drive = gd.GoogleDrivePromos(self.store)
        self.assertEqual(drive.archive_fingerprint(self.inventory), self.adapter.archive_fingerprint(self.inventory))
        self.assertEqual(drive.prepare_collection_source.__func__, dbp.DropboxPromos.prepare_collection_source)
        entry = drive._entry({"id": "fixture-drive-id", "name": "Promo.zip", "size": "100",
                              "md5Checksum": "md5", "version": "23", "modifiedTime": "2026-01-01"})
        self.assertEqual((entry["md5"], entry["version"], entry["modified_time"]), ("md5", "23", "2026-01-01"))

    def test_cleanup_rejects_source_path_traversal(self):
        with self.assertRaisesRegex(RuntimeError, "left its source"):
            self.adapter._clear_archive_inventory("..")

    def test_fresh_snapshot_clears_same_operation_unversioned_reuse(self):
        self.adapter._prepared_archives = {self.sid: "old"}
        self.adapter.client = mock.Mock()
        with mock.patch.object(dbp, "shared_inventory", return_value={"name": "Promo", "files": [], "requests": 1}):
            dbp.DropboxPromos.snapshot(self.adapter, self.sid, refresh=True)
        self.assertNotIn(self.sid, self.adapter._prepared_archives)
