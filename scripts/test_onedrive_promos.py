import base64
import hashlib
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dropbox_promos
import onedrive_promos as od
from promo_automation import Store
from promo_pipeline import collect_release


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.sync_logs = mock.Mock()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.db.close)
        for target in ("curl_cffi.requests.Session.request", "requests.sessions.Session.request", "socket.create_connection", "socket.socket.connect"):
            patch = mock.patch(target, side_effect=AssertionError("Real network disabled in direct-share tests"))
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(dropbox_promos, "dropbox_skill", side_effect=AssertionError("Real Dropbox source client disabled"))
        patch.start()
        self.addCleanup(patch.stop)

    def source(self, url="https://1drv.ms/f/s/fake-recipient", subject="Exclusive promo", platform="onedrive"):
        return self.store.add_source(platform, subject, url, sender="Promo <team@example.invalid>")


class URLTests(OfflineCase):
    def test_embedded_recipient_is_decoded_not_guessed(self):
        url = "https://1drv.ms/f/s/fake-recipient?e=fake"
        encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        self.assertEqual(od.recipient_url("https://onedrive.live.com/?id=%2Fpersonal%2Ffolder&redeem=" + encoded), url)
        self.assertEqual(od.recipient_url("https://onedrive.live.com/redir?resid=fake!123&authkey=fake"),
                         "https://onedrive.live.com/redir?resid=fake!123&authkey=fake")

    def test_foreign_hosts_custom_ports_and_consent_fail_closed(self):
        for url in ("http://1drv.ms/s/fake", "https://1drv.ms.evil.invalid/s/fake", "https://user@1drv.ms/s/fake",
                    "https://1drv.ms:8443/s/fake", "https://onedrive.live.com/"):
            with self.assertRaises(RuntimeError):
                od.recipient_url(url)
        with self.assertRaises(od.ShareAccessRequired):
            od.onedrive_page_path("https://onedrive.live.com/login?next=fake")
        foreign = base64.urlsafe_b64encode(b"https://evil.invalid/file").decode().rstrip("=")
        with self.assertRaises(RuntimeError):
            od.recipient_url("https://onedrive.live.com/?id=fake&redeem=" + foreign)

    def test_camelot_bpm_and_explicit_underscore_mix(self):
        self.assertEqual(od.metadata_filename("5A - 128 - Example_ARTIST TITLE_EXTENDED.mp3"),
                         "Example - ARTIST TITLE (Extended Mix).mp3")
        self.assertEqual(od.metadata_filename("Example_TITLE_Radio.mp3"), "Example - TITLE (Radio Edit).mp3")
        self.assertEqual(od.metadata_filename("Unmarked_Name.mp3"), "Unmarked_Name.mp3")


class InventoryTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.http = mock.Mock(calls=0)
        self.adapter = od.OneDrivePromos(self.store, self.http)
        self.root = "https://onedrive.live.com/_api/v2.0/shares/u!fake/driveItem"

    def item(self, file_id, name, size=100, **extra):
        return {"id": file_id, "name": name, "size": size, "file": {"hashes": {}}, **extra}

    def test_native_migrated_context_and_legacy_context(self):
        sid = self.source()
        self.http.page.return_value = ("https://onedrive.live.com/", 200, {}, b"var _spPageContextInfo={};")
        self.assertIn("/_api/v2.0/shares/u!", self.adapter._context(sid))
        self.http.page.return_value = ("https://onedrive.live.com/", 200, {}, b"legacy")
        self.assertTrue(self.adapter._context(sid).startswith("https://api.onedrive.com/v1.0/shares/u!"))

    def test_complete_paginated_nested_inventory_and_format_merge(self):
        sid = self.source(subject="Release [Fixture Records]")
        next_url = self.root + "/children?$skiptoken=fake"
        root_item = {"id": "root", "name": "Promo", "folder": {}, "parentReference": {"driveId": "fixture-drive"}}
        pages = {
            self.root: root_item,
            self.root + "/children?$top=200": {"value": [self.item("mp3", "Artist - Tune (Extended Mix).mp3"),
                 {"id": "folder", "name": "Originals", "folder": {}}], "@odata.nextLink": next_url},
            "https://onedrive.live.com/_api/v2.0/drives/fixture-drive/items/folder/children?$top=200": {"value": [self.item("wav", "Artist - Tune (Extended Mix).wav", 500,
                 audio={"duration": 420000})]},
            next_url: {"value": [self.item("press", "Press.pdf")]},
        }
        self.http.json.side_effect = lambda url, *a, **kw: pages[url]
        with mock.patch.object(self.adapter, "_context", return_value=self.root):
            inventory = self.adapter.snapshot(sid)
        self.assertEqual([x["id"] for x in inventory["files"]], ["mp3", "wav", "press"])
        self.assertEqual(inventory["files"][1]["duration"], 420)
        release = self.adapter.release(sid)
        self.assertEqual(len(release["tracks"]), 1)
        self.assertEqual(sorted(x["type"] for x in release["tracks"][0]["files"]), ["mp3", "wav"])
        self.assertEqual(release["tracks"][0]["label"], "Fixture Records")
        self.http.original.assert_not_called()
        self.store.sync_logs.assert_called()

    def test_pagination_cannot_escape_scope_loop_or_drop_inventory(self):
        for next_url in ("https://evil.invalid/children", self.root + "/children?$top=200", self.root + "/other/children"):
            self.http.json.return_value = {"value": [], "@odata.nextLink": next_url}
            with self.assertRaises(RuntimeError):
                self.adapter._children(self.root, drive_id="fixture-drive")
        self.http.json.return_value = {}
        with self.assertRaisesRegex(RuntimeError, "inventory"):
            self.adapter._children(self.root, drive_id="fixture-drive")

    def test_repeated_ids_unsafe_names_shortcuts_and_depth_refused(self):
        for values in ([self.item("a", "x.wav"), self.item("a", "y.wav")],
                       [self.item("a", "../x.wav")],
                       [{"id": "a", "name": "Shortcut", "remoteItem": {}, "folder": {}}]):
            self.http.json.return_value = {"value": values}
            with self.assertRaises(RuntimeError):
                self.adapter._children(self.root, drive_id="fixture-drive")
        with self.assertRaises(RuntimeError):
            self.adapter._children(self.root, depth=9)
        self.http.json.return_value = {"value": []}
        with self.assertRaisesRegex(RuntimeError, "child count"):
            self.adapter._children(self.root, drive_id="fixture-drive", expected_count=2)

    def test_next_adapter_reuses_bound_disk_snapshot_and_explicit_refresh_refetches(self):
        sid = self.source()
        self.http.json.return_value = self.item("file", "Artist - Tune.wav")
        with mock.patch.object(self.adapter, "_context", return_value=self.root):
            self.adapter.snapshot(sid)
        another = od.OneDrivePromos(self.store, self.http)
        self.http.json.return_value = self.item("file", "Artist - Tune.wav", 200)
        with mock.patch.object(another, "_context", return_value=self.root):
            self.http.json.reset_mock()
            self.assertEqual(another.snapshot(sid)["files"][0]["size"], 100)
            self.http.json.assert_not_called()
            self.assertEqual(another.snapshot(sid, refresh=True)["files"][0]["size"], 200)
            self.http.json.assert_called_once()

    def test_old_corrupt_foreign_and_incomplete_disk_snapshots_are_refetched(self):
        sid = self.source()
        cache = self.store.root / ("onedrive-" + sid + ".dpapi")
        self.http.json.return_value = self.item("file", "Artist - Tune.wav")
        with mock.patch.object(self.adapter, "_context", return_value=self.root):
            self.adapter.snapshot(sid)
        envelope = od.read_protected_json(cache)
        invalid = [{"files": []}, {**envelope, "source_id": "foreign"},
                   {**envelope, "source_url_sha256": "foreign"},
                   {**envelope, "inventory": {**envelope["inventory"], "files": [{}]}}]
        for saved in invalid:
            od.protect_json(cache, saved)
            another = od.OneDrivePromos(self.store, self.http)
            with mock.patch.object(another, "_context", return_value=self.root):
                self.http.json.reset_mock()
                self.assertEqual(another.snapshot(sid)["files"][0]["id"], "file")
                self.http.json.assert_called_once()
        cache.write_bytes(b"corrupt")
        another = od.OneDrivePromos(self.store, self.http)
        with mock.patch.object(another, "_context", return_value=self.root):
            self.http.json.reset_mock()
            another.snapshot(sid)
            self.http.json.assert_called_once()

    def test_child_drive_cannot_leave_anonymous_recipient_scope(self):
        item = self.item("file", "Artist - Tune.wav", parentReference={"driveId": "foreign-drive"})
        self.http.json.return_value = {"value": [item]}
        with self.assertRaisesRegex(RuntimeError, "recipient drive"):
            self.adapter._children(self.root, drive_id="fixture-drive")

    def test_one_file_root_and_metadata_only_selection(self):
        sid = self.source(subject="Tune [Fixture Records]")
        file = self.item("file", "Artist - Tune.wav", sha1="unused")
        self.http.json.return_value = file
        with mock.patch.object(self.adapter, "_context", return_value=self.root):
            self.adapter.snapshot(sid)
        library = mock.Mock()
        library.matches.return_value = []
        result = collect_release(self.store, self.adapter, sid, library, dry_run=True)
        self.assertEqual(result["tracks"][0]["action"], "download")
        self.http.original.assert_not_called()

    def test_archives_are_explicit_pending_work_without_audio(self):
        sid = self.source()
        self.http.json.return_value = self.item("pack", "Promo.zip", 500)
        with mock.patch.object(self.adapter, "_context", return_value=self.root):
            self.adapter.prepare_source(sid)
        release = self.adapter.release(sid)
        self.assertEqual(release["tracks"], [])
        self.assertIn("explicitly approved", release["warnings"][0]["reason"])
        with mock.patch.object(self.adapter, "_prepare_archive_source", side_effect=AssertionError("Archive transfer forbidden")):
            self.adapter.prepare_collection_source(sid)
        self.http.original.assert_not_called()

    def test_standalone_batch_refreshes_mutable_share_before_local_planning(self):
        sid = self.source()
        self.adapter.snapshot = mock.Mock(return_value={"share_key": "fixture"})
        with mock.patch.object(od, "newest_offer_age", return_value=1), \
                mock.patch.object(od, "LibraryIndex"), \
                mock.patch.object(od, "collect_release", return_value={"tracks": []}), \
                mock.patch.object(self.adapter, "release", return_value={"warnings": []}):
            self.adapter.batch(dry_run=True)
        self.adapter.snapshot.assert_called_once_with(sid, refresh=True)

    def test_download_refreshes_metadata_and_refuses_changed_original(self):
        sid = self.source()
        entry = {"id": "file", "name": "Artist - Tune.wav", "path": "Originals/Artist - Tune.wav", "size": 100,
                 "sha1": None, "sha256": None, "metadata_url": "https://onedrive.live.com/_api/v2.0/drives/fixture-drive/items/file"}
        item = self.item("file", entry["name"], 100, **{"@content.downloadUrlNoAuth": "https://my.microsoftpersonalcontent.com/original"})
        self.http.json.return_value = item
        self.http.original.return_value = (100, "sha")
        self.adapter.inventories[sid] = {"files": [entry]}
        with mock.patch.object(self.adapter, "_context", return_value=self.root):
            self.assertEqual(self.adapter._download_entry(sid, entry, Path(self.tmp.name) / "track.wav"), (100, "sha"))
        self.assertEqual(entry["metadata_url"], self.http.json.call_args.args[0])
        item["size"] = 200
        self.http.original.reset_mock()
        with mock.patch.object(self.adapter, "_context", return_value=self.root):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                self.adapter._download_entry(sid, entry, Path(self.tmp.name) / "bad.wav")
        self.http.original.assert_not_called()


class TransportTests(OfflineCase):
    def response(self, body=b"audio", status=200, headers=None):
        r = mock.Mock(status_code=status, headers=headers or {"Content-Type": "application/octet-stream"})
        r.iter_content.return_value = iter([body])
        return r

    def test_original_exact_size_hash_and_atomic_failure_cleanup(self):
        http = od.ShareHTTP()
        body = b"original data"
        target = Path(self.tmp.name) / "track.wav"
        with mock.patch.object(http, "response", return_value=self.response(body)):
            size, sha = http.original("https://my.microsoftpersonalcontent.com/file", target, od.ORIGINAL_HOSTS,
                                      len(body), sha1=hashlib.sha1(body).hexdigest())
        self.assertEqual((size, sha, target.read_bytes()), (len(body), hashlib.sha256(body).hexdigest(), body))
        bad = target.with_name("bad.wav")
        for expected_size, expected_hash in ((1, None), (len(body), "0" * 40)):
            with mock.patch.object(http, "response", return_value=self.response(body)):
                with self.assertRaises(RuntimeError):
                    http.original("https://my.microsoftpersonalcontent.com/file", bad, od.ORIGINAL_HOSTS, expected_size, sha1=expected_hash)
            self.assertFalse(bad.exists())
            self.assertFalse(list(Path(self.tmp.name).glob("bad*")))
        http.close()

    def test_redirect_host_is_checked_before_contact_and_html_rejected(self):
        http = od.ShareHTTP()
        redirect = self.response(status=302, headers={"Location": "https://evil.invalid/file"})
        calls = []
        def fake(method, url, **kwargs):
            calls.append(url)
            return redirect
        with mock.patch.object(http.session, "request", side_effect=fake):
            with self.assertRaises(RuntimeError):
                http.original("https://my.microsoftpersonalcontent.com/file", Path(self.tmp.name) / "bad.wav", od.ORIGINAL_HOSTS, 100)
        self.assertEqual(calls, ["https://my.microsoftpersonalcontent.com/file"])
        with mock.patch.object(http, "response", return_value=self.response(b"<html>", headers={"Content-Type": "text/html"})):
            with self.assertRaisesRegex(RuntimeError, "page"):
                http.original("https://my.microsoftpersonalcontent.com/file", Path(self.tmp.name) / "bad.wav", od.ORIGINAL_HOSTS, 6)
        http.close()

    def test_metadata_bounded_and_error_types_do_not_echo_protected_urls(self):
        http = od.ShareHTTP()
        with mock.patch.object(http, "response", return_value=self.response(status=403)):
            with self.assertRaises(od.ShareAccessRequired):
                http.json("https://onedrive.live.com/_api/v2.0/shares/u!fake/driveItem", ("onedrive.live.com",))
        with mock.patch.object(http, "response", return_value=self.response(status=410)):
            with self.assertRaises(od.ShareUnavailable):
                http.json("https://onedrive.live.com/_api/v2.0/shares/u!fake/driveItem", ("onedrive.live.com",))
        with mock.patch.object(http, "response", return_value=self.response(b"x" * 12)), mock.patch.object(od, "MAX_METADATA", 10):
            with self.assertRaisesRegex(RuntimeError, "bounded"):
                http.document("https://onedrive.live.com/", ("onedrive.live.com",))
        http.close()


if __name__ == "__main__":
    unittest.main()
