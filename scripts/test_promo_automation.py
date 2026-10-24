import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
from promo_automation import Store
import promo_automation


class StateTests(unittest.TestCase):
    def test_protected_source_roundtrip_and_reminder_identity(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            url = "https://inflyteapp.com/review/releases/123?source=email"
            first = store.add_source("inflyte", "Promo", url)
            self.assertEqual(store.source_url(first), url)
            second_url = "https://inflyteapp.com/review/releases/123?source=reminder"
            second = store.add_source("inflyte", "Reminder", second_url)
            self.assertEqual(first, second)
            self.assertEqual(store.source_url(first), second_url)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sources").fetchone()[0], 1)
            store.db.close()


class LogSyncTests(unittest.TestCase):
    def test_upload_timeout_reuses_confirmed_audit_file_without_losing_intents(self):
        import dropbox
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            server = {}
            def upload(data, target, **kwargs):
                if "/audit/" in target:
                    if target in server:
                        reason = dropbox.files.UploadWriteFailed(reason=dropbox.files.WriteError.conflict(dropbox.files.WriteConflictError.file))
                        raise dropbox.exceptions.ApiError("test", dropbox.files.UploadError.path(reason), None, None)
                    server[target] = data
                    raise RuntimeError("response lost after server accepted the log")
                return SimpleNamespace(content_hash=promo_automation.dropbox_bytes_hash(data))
            client = SimpleNamespace(files_upload=upload,
                files_get_metadata=lambda target: SimpleNamespace(content_hash=promo_automation.dropbox_bytes_hash(server[target])))
            skill = SimpleNamespace(ensure_dropbox_folder=lambda *_: None)
            store.event(None, "download", "intent", track="First")
            with mock.patch.object(promo_automation, "dropbox_skill", return_value=(skill, client)):
                with self.assertRaisesRegex(RuntimeError, "response lost"):
                    store.sync_logs()
                store.sync_logs()
            self.assertEqual(len(server), 1)
            self.assertEqual(store.db.execute("SELECT published_sequence FROM log_checkpoint").fetchone()[0], 1)
            store.db.close()

    def test_events_are_batched_per_minute_and_summaries_only_when_asked(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            uploads, calls, clock = {}, [], [1000.0]
            def upload(data, target, **kwargs):
                uploads[target] = data
                calls.append(target)
                return SimpleNamespace(content_hash=promo_automation.dropbox_bytes_hash(data))
            client = SimpleNamespace(files_upload=upload)
            skill = SimpleNamespace(ensure_dropbox_folder=lambda *_: None)
            with mock.patch.object(promo_automation, "dropbox_skill", return_value=(skill, client)),                     mock.patch.object(promo_automation.time, "time", lambda: clock[0]):
                store.event(None, "download", "intent", track="First")
                store.sync_logs()
                self.assertEqual(len(calls), 1, "the first event goes out at once, summaries wait")
                self.assertIn("/audit/", calls[-1])
                store.event(None, "feedback", "intent", comment="Thanks")
                clock[0] += 10
                store.sync_logs()
                self.assertEqual(len(calls), 1, "a second event within the minute waits for the batch")
                store.event(None, "feedback", "intent", comment="Again")
                clock[0] += 60
                store.sync_logs()
                self.assertEqual(len(calls), 2, "one upload carries both waiting events")
                self.assertIn(b"Thanks", uploads[calls[-1]])
                self.assertIn(b"Again", uploads[calls[-1]])
                clock[0] += 600
                store.sync_logs()
                self.assertEqual(len(calls), 2, "no new event means no new network request")
                store.event(None, "download", "intent", track="Last")
                store.sync_logs(force=True)
                self.assertEqual(len(calls), 6, "a step ending flushes events and the first summaries")
                store.sync_logs(force=True)
                self.assertEqual(len(calls), 6, "forced again within 15 minutes: nothing new to send")
                store.sync_logs(summary=True)
                self.assertEqual(len(calls), 6, "unchanged summaries are not uploaded again")
                self.assertEqual(len([c for c in calls if c.endswith("README.txt")]), 1)
            self.assertEqual(store.db.execute("SELECT published_sequence FROM log_checkpoint").fetchone()[0], 4)
            store.db.close()

    def test_library_index_applies_changes_and_relists_after_a_cursor_reset(self):
        import dropbox
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            def file(name, rev="1"):
                return SimpleNamespace(path_lower="/music library/a/" + name.lower(), id="id:" + name, rev=rev,
                                       size=1, content_hash="h" + rev, name=name)
            shared = SimpleNamespace(FileMetadata=SimpleNamespace)
            listings = {"full": [file("One.aiff"), file("Two.aiff"), file("Gone.aiff")]}
            requests = []
            def page(entries, cursor):
                return SimpleNamespace(entries=entries, has_more=False, cursor=cursor)
            def full_list(path, recursive):
                requests.append("full")
                return page(listings["full"], "c1")
            def resume(cursor):
                requests.append(cursor)
                if listings.get("reset"):
                    raise dropbox.exceptions.ApiError("t", dropbox.files.ListFolderContinueError.reset, None, None)
                return page([file("Two.aiff", rev="2"), file("New.aiff"),
                             dropbox.files.DeletedMetadata(name="Gone.aiff", path_lower="/music library/a/gone.aiff")], "c2")
            client = SimpleNamespace(files_list_folder=full_list, files_list_folder_continue=resume)
            with mock.patch.object(promo_automation, "dropbox_skill", return_value=(shared, client)):
                self.assertEqual(promo_automation.index_library(store)["mode"], "full")
                result = promo_automation.index_library(store)
                self.assertEqual((result["mode"], result["changes"]), ("changes", 3))
                paths = dict(store.db.execute("SELECT path, revision FROM library"))
                self.assertEqual(sorted(p.rsplit("/", 1)[1] for p in paths), ["new.aiff", "one.aiff", "two.aiff"])
                self.assertEqual(paths["/music library/a/two.aiff"], "2")
                listings["reset"] = True
                self.assertEqual(promo_automation.index_library(store)["mode"], "full")
                self.assertEqual(requests, ["full", "c1", "c2", "full"])
                self.assertEqual(store.db.execute("SELECT count(*) FROM library").fetchone()[0], 3)
            store.db.close()

    def test_unverified_audit_upload_does_not_advance_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            client = SimpleNamespace(files_upload=lambda *a, **k: SimpleNamespace(content_hash="wrong"))
            skill = SimpleNamespace(ensure_dropbox_folder=lambda *_: None)
            store.event(None, "download", "intent", track="Held")
            with mock.patch.object(promo_automation, "dropbox_skill", return_value=(skill, client)):
                with self.assertRaisesRegex(RuntimeError, "outward action remains blocked"):
                    store.sync_logs()
            self.assertIsNone(store.db.execute("SELECT * FROM log_checkpoint").fetchone())
            store.db.close()

    def test_two_workers_cannot_claim_same_asset(self):
        with tempfile.TemporaryDirectory() as root:
            first, second = Store(root), Store(root)
            self.assertTrue(first.claim("inflyte:1:2:aiff", None, "Track", "aiff"))
            self.assertFalse(second.claim("inflyte:1:2:aiff", None, "Track", "aiff"))
            first.db.close()
            second.db.close()

    def test_completed_asset_cannot_be_claimed_again_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            self.assertTrue(store.claim("asset", None, "Track", "wav"))
            store.asset_update("asset", "delivery_verified", dropbox_path="/test/track.aiff")
            store.db.close()
            restarted = Store(root)
            self.assertFalse(restarted.claim("asset", None, "Track", "wav"))
            restarted.db.close()

    def test_intent_remains_visible_when_no_transfer_completes(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.event(None, "download", "intent", title="Track", platform_status="unknown")
            text = store.export_logs()["download_log.txt"].decode("utf-8")
            self.assertIn("intent", text)
            self.assertIn("Track", text)
            self.assertNotIn("download | delivery_verified", text)
            store.db.close()




class MailLinkTests(unittest.TestCase):
    def test_plain_text_brackets_and_punctuation_trimmed(self):
        from promo_mail import clean_link
        self.assertEqual(clean_link("https://on.soundcloud.com/AbC)"), "https://on.soundcloud.com/AbC")
        self.assertEqual(clean_link("https://example.com/a_(b)"), "https://example.com/a_(b)")
        self.assertEqual(clean_link("https://example.com/a."), "https://example.com/a")
        self.assertEqual(clean_link("https://example.com/a?b=1&amp;c=2"), "https://example.com/a?b=1&c=2")


if __name__ == "__main__":
    unittest.main()
