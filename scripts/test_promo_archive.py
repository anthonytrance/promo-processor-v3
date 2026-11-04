"""Month boundaries, hash preservation and uncertain-response recovery, without accounts."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dropbox.files import FileMetadata, FolderMetadata

import promo_archive as archive
from promo_automation import Store


class MonthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        self.dest, self.root = "/Music/Promos/Latest", "/Music/Promos"
        self.tree = {}
        self.folder(self.dest, "id:latest")
        self.file(self.dest + "/One.aiff", "id:one", "hash1")
        self.client = SimpleNamespace(files_get_metadata=lambda path: self.tree.get(path.lower()),
                                      files_list_folder=self.listing, files_move_v2=mock.Mock(),
                                      files_move_batch_v2=self.batch,
                                      files_move_batch_check_v2=lambda job: SimpleNamespace(is_complete=lambda: True))
        self.shared = SimpleNamespace(ensure_dropbox_folder=self.ensure)
        for name, value in (("DEST", self.dest), ("AUDIT", self.root + "/audit"),
                            ("HOLD", self.root + "/audit/holds"),
                            ("SETTINGS", {"archive_root": self.root, "latest_month": "2026-10"})):
            patch = mock.patch.object(archive, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(archive, "dropbox_skill", return_value=(self.shared, self.client))
        patch.start()
        self.addCleanup(patch.stop)

    def folder(self, path, identity):
        self.tree[path.lower()] = FolderMetadata(name=path.rsplit("/", 1)[-1], id=identity,
                                                 path_lower=path.lower(), path_display=path)

    def file(self, path, identity, digest):
        import hashlib
        self.tree[path.lower()] = FileMetadata(name=path.rsplit("/", 1)[-1], id=identity,
                                               path_lower=path.lower(), path_display=path,
                                               size=123, content_hash=hashlib.sha256(digest.encode()).hexdigest())

    def listing(self, path, recursive=False):
        base = path.lower() + "/"
        return SimpleNamespace(entries=[meta for name, meta in self.tree.items() if name.startswith(base)
                                        and (recursive or "/" not in name[len(base):])], has_more=False)

    def ensure(self, client, path):
        if path.lower() not in self.tree:
            self.folder(path, "id:new:" + path)

    def batch(self, entries, autorename):
        self.assertLessEqual(len(entries), archive.BATCH_SIZE)
        for entry in entries:
            self.move(entry.from_path, entry.to_path, autorename)
        return SimpleNamespace(is_async_job_id=lambda: False)

    def latest_id(self):
        return self.tree[self.dest.lower()].id

    def move(self, source, target, autorename):
        self.assertFalse(autorename)
        for path in list(self.tree):
            if archive.inside(path, source):
                meta = self.tree.pop(path)
                new = target + path[len(source):]
                meta.path_lower, meta.path_display = new.lower(), new
                meta.name = new.rsplit("/", 1)[-1]
                self.tree[new.lower()] = meta

    def test_first_new_month_archives_and_remaps_without_changing_pass_or_hash(self):
        old_path = self.dest + "/One.aiff"
        self.store.db.execute("INSERT INTO assets(asset_key,stage,dropbox_path,content_hash,updated_at) VALUES(?,?,?,?,?)",
                              ("one", "delivery_verified", old_path, "hash1", "original-time"))
        self.store.start_pass("original-pass")
        result = archive.rotate_month(self.store, "2026-11")
        self.assertEqual(result["files"], 1)
        row = self.store.db.execute("SELECT dropbox_path,content_hash,updated_at FROM assets").fetchone()
        self.assertEqual(tuple(row), (self.root + "/2026/10/One.aiff", "hash1", "original-time"))
        self.assertEqual(self.store.pass_started(), "original-pass")
        self.assertEqual(archive.inventory(self.client, self.dest), {})
        self.assertEqual(self.latest_id(), "id:latest")
        self.client.files_move_v2.assert_not_called()
        self.assertEqual(archive.rotate_month(self.store, "2026-11")["state"], "current")

    def test_stored_names_keep_their_case_and_a_big_month_splits_into_batches(self):
        for number in range(archive.BATCH_SIZE + 5):
            self.file(f"{self.dest}/Track {number}.aiff", f"id:{number}", str(number))
        calls = []
        def counted(entries, autorename):
            calls.append(len(entries))
            return self.batch(entries, autorename)
        self.client.files_move_batch_v2 = counted
        result = archive.rotate_month(self.store, "2026-11")
        self.assertEqual(result["files"], archive.BATCH_SIZE + 6)
        self.assertEqual(calls, [archive.BATCH_SIZE, 6])
        self.assertEqual(self.tree[(self.root + "/2026/10/one.aiff").lower()].path_display,
                         self.root + "/2026/10/One.aiff")

    def test_crash_mid_month_resumes_with_only_the_files_still_in_latest(self):
        self.file(self.dest + "/Two.aiff", "id:two", "hash2")
        real, seen = self.batch, []
        def partial(entries, autorename):
            seen.append([entry.from_path for entry in entries])
            if len(seen) == 1:
                real(entries[:1], autorename)
                raise RuntimeError("connection lost")
            return real(entries, autorename)
        self.client.files_move_batch_v2 = partial
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            archive.rotate_month(self.store, "2026-11")
        self.assertEqual(archive.rotate_month(self.store, "2026-11")["state"], "archived")
        self.assertEqual((len(seen[0]), len(seen[1])), (2, 1))
        self.assertEqual(self.latest_id(), "id:latest")

    def test_running_job_is_journaled_and_waited_for_after_a_crash(self):
        def later(entries, autorename):
            self.batch(entries, autorename)
            return SimpleNamespace(is_async_job_id=lambda: True, get_async_job_id=lambda: "job-1")
        self.client.files_move_batch_v2 = later
        self.client.files_move_batch_check_v2 = mock.Mock(side_effect=RuntimeError("connection lost"))
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            archive.rotate_month(self.store, "2026-11")
        saved = json.loads((Path(self.temp.name) / "monthly-archive.json").read_text())
        self.assertEqual(saved["pending"]["job"], "job-1")
        self.client.files_move_batch_check_v2 = mock.Mock(
            return_value=SimpleNamespace(is_complete=lambda: True))
        self.assertEqual(archive.rotate_month(self.store, "2026-11")["state"], "archived")
        self.client.files_move_batch_check_v2.assert_called_once_with("job-1")

    def test_file_arriving_during_the_archive_stays_for_the_new_month(self):
        real = self.batch
        def arriving(entries, autorename):
            self.file(self.dest + "/New.aiff", "id:new", "new")
            return real(entries, autorename)
        self.client.files_move_batch_v2 = arriving
        result = archive.rotate_month(self.store, "2026-11")
        self.assertEqual((result["files"], result["left_in_place"]), (1, 1))
        self.assertIn(self.dest.lower() + "/new.aiff", self.tree)

    def test_file_that_will_not_move_blocks_collection_and_can_be_resumed(self):
        real = self.client.files_move_batch_v2
        self.client.files_move_batch_v2 = lambda entries, autorename: SimpleNamespace(is_async_job_id=lambda: False)
        with self.assertRaisesRegex(RuntimeError, "would not move"):
            archive.rotate_month(self.store, "2026-11")
        self.client.files_move_batch_v2 = real
        self.assertEqual(archive.rotate_month(self.store, "2026-11")["state"], "archived")

    def test_replaced_delivery_folder_blocks_before_moving_anything(self):
        real = self.listing
        def swapped(path, recursive=False):
            page = real(path, recursive)
            self.tree[self.dest.lower()].id = "id:other"
            return page
        self.client.files_list_folder = swapped
        self.client.files_move_batch_v2 = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "replaced"):
            archive.rotate_month(self.store, "2026-11")
        self.client.files_move_batch_v2.assert_not_called()

    def test_pending_plan_from_the_old_whole_folder_move_is_not_guessed_at(self):
        (Path(self.temp.name) / "monthly-archive.json").write_text(json.dumps({
            "month": "2026-10", "destination": self.dest, "archive_root": self.root,
            "pending": {"source": self.dest, "target": self.root + "/2026/10", "id": "id:latest",
                        "inventory": {}, "next_month": "2026-11"}}))
        with self.assertRaisesRegex(RuntimeError, "predates"):
            archive.rotate_month(self.store, "2026-11")

    def test_lost_move_response_is_recovered_without_a_second_move(self):
        def uncertain(*args, **kwargs):
            self.move(*args, **kwargs)
            raise RuntimeError("response lost")
        self.client.files_move_batch_v2 = mock.Mock(side_effect=lambda entries, autorename: uncertain(
            entries[0].from_path, entries[0].to_path, autorename))
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            archive.rotate_month(self.store, "2026-11")
        self.assertIn("pending", json.loads((Path(self.temp.name) / "monthly-archive.json").read_text()))
        self.assertEqual(archive.rotate_month(self.store, "2026-11")["state"], "archived")
        self.assertEqual(self.client.files_move_batch_v2.call_count, 1)

    def test_conflicting_archive_stops_before_moving_anything(self):
        self.folder(self.root + "/2026/10", "id:existing")
        self.client.files_move_batch_v2 = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            archive.rotate_month(self.store, "2026-11")
        self.client.files_move_batch_v2.assert_not_called()

    def test_unfinished_replacement_stops_before_its_old_copy_becomes_archived(self):
        self.store.claim("one", None, "One", "mp3")
        self.store.db.execute("INSERT INTO replacements VALUES(?,?,?,?)", (
            "one", self.dest + "/One.mp3", "hash", "original-time"))
        self.store.db.commit()
        self.client.files_move_batch_v2 = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "pending replacements"):
            archive.rotate_month(self.store, "2026-11")
        self.client.files_move_batch_v2.assert_not_called()

    def test_changed_archived_hash_blocks_recovery_and_collection(self):
        def uncertain(*args, **kwargs):
            self.move(*args, **kwargs)
            self.tree[(self.root + "/2026/10/one.aiff").lower()].content_hash = "0" * 64
            raise RuntimeError("response lost")
        self.client.files_move_batch_v2 = lambda entries, autorename: uncertain(
            entries[0].from_path, entries[0].to_path, autorename)
        with self.assertRaises(RuntimeError):
            archive.rotate_month(self.store, "2026-11")
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            archive.rotate_month(self.store, "2026-11")
        self.assertEqual(self.latest_id(), "id:latest")

    def test_skipped_months_and_year_boundary_archive_the_stored_month(self):
        result = archive.rotate_month(self.store, "2027-01")
        self.assertEqual(result["path"], self.root + "/2026/10")
        self.file(self.dest + "/Two.aiff", "id:two", "hash2")
        self.assertEqual(archive.rotate_month(self.store, "2027-02")["path"], self.root + "/2027/01")

    def test_old_clock_is_rejected_and_disabled_installations_do_nothing(self):
        with self.assertRaisesRegex(RuntimeError, "precedes"):
            archive.rotate_month(self.store, "2026-09")
        with mock.patch.object(archive, "SETTINGS", {}):
            self.assertEqual(archive.rotate_month(self.store, "2026-11"), {"state": "disabled"})

    def test_shared_folder_self_entry_does_not_count_as_content(self):
        self.client.files_list_folder = lambda *args, **kwargs: SimpleNamespace(
            entries=[self.tree[self.dest.lower()]], has_more=False)
        self.assertEqual(archive.inventory(self.client, self.dest), {})
