import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gdrive_promos
from gdrive_promos import GoogleDrivePromos, drive_target
from promo_automation import Store


class DriveTargetTests(unittest.TestCase):
    def test_file_folder_and_open_links(self):
        self.assertEqual(drive_target("https://drive.google.com/file/d/1AbCdEfGhIjKlMn/view?usp=sharing"),
                         ("file", "1AbCdEfGhIjKlMn"))
        self.assertEqual(drive_target("https://drive.google.com/drive/folders/1ZyXwVuTsRqPo?usp=drive_link"),
                         ("folder", "1ZyXwVuTsRqPo"))
        self.assertEqual(drive_target("https://drive.google.com/open?id=1AbCdEfGhIjKlMn"), ("file", "1AbCdEfGhIjKlMn"))

    def test_gmail_safe_redirect_beside_a_tracker_is_read(self):
        from email.message import EmailMessage
        from promo_mail import links_from_message
        msg = EmailMessage()
        msg.set_content('<a href="https://pg4pr.r.ag.d.sendibm3.com/mk/cl/f/sh/abc" data-saferedirecturl="https://www.google.com'
                        '/url?q=https://drive.google.com/file/d/1AbCdEfGhIjKlMn/view?usp%3Dsharing&amp;source=gmail">x</a>'
                        '<a href="https://x.example/" data-saferedirecturl="https://evil.example/url?q=https://drive.google.com/file/d/1Zz">y</a>',
                        subtype="html")
        drive = {u for u in links_from_message(msg) if "drive.google.com" in u}
        self.assertEqual(drive, {"https://drive.google.com/file/d/1AbCdEfGhIjKlMn/view?usp=sharing"})

    def test_cut_off_and_foreign_links_refused(self):
        self.assertIsNone(drive_target("https://drive.google.com/file/"))
        self.assertIsNone(drive_target("https://evil.example/file/d/1AbCdEfGhIjKlMn/view"))
        self.assertIsNone(drive_target("http://drive.google.com/file/d/1AbCdEfGhIjKlMn/view"))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.sync_logs = lambda force=False: None
        self.adapter = GoogleDrivePromos(self.store)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def source(self, url, subject="Tony Irrmani - Abora [Pure Emotion Records]"):
        return self.store.add_source("google-drive", subject, url, sender="Promo Team <promo@example.com>")

    def test_folder_inventory_recurses_and_skips_google_docs(self):
        sid = self.source("https://drive.google.com/drive/folders/1RootFolderId")
        pages = {
            "1RootFolderId": {"files": [
                {"id": "1SubFolderIdX", "name": "WAV", "mimeType": gdrive_promos.FOLDER},
                {"id": "1PressDocIdX", "name": "Press", "mimeType": "application/vnd.google-apps.document"},
                {"id": "1Mp3FileIdXX", "name": "Tony Irrmani - Abora (Extended Mix).mp3", "mimeType": "audio/mpeg",
                 "size": "100", "md5Checksum": "a", "capabilities": {"canDownload": True}}]},
            "1SubFolderIdX": {"files": [
                {"id": "1WavFileIdXX", "name": "Tony Irrmani - Abora (Extended Mix).wav", "mimeType": "audio/wav",
                 "size": "900", "md5Checksum": "b", "capabilities": {"canDownload": False}}]}}

        def fake(*args, save_to=None):
            params = gdrive_promos.json.loads(args[-1])
            if args[1:3] == ("files", "get"):
                return {"id": params["fileId"], "name": "Abora"}
            return pages[params["q"].split("'")[1]]

        with mock.patch.object(gdrive_promos, "gws", side_effect=fake):
            inventory = self.adapter.snapshot(sid)
        self.assertEqual(inventory["kind"], "folder")
        inventory["files"].sort(key=lambda f: f["path"])
        self.assertEqual([f["path"] for f in inventory["files"]],
                         ["Tony Irrmani - Abora (Extended Mix).mp3", "WAV/Tony Irrmani - Abora (Extended Mix).wav"])
        self.assertEqual(inventory["files"][1]["size"], 900)
        self.assertFalse(inventory["files"][1]["is_downloadable"])
        release = self.adapter.release(sid)
        self.assertTrue(all(t["label"] == "Pure Emotion Records" for t in release["tracks"]))
        self.assertEqual(sorted((t["files"][0]["type"] if t["files"] else None) for t in release["tracks"]
                                if t["files"]), ["mp3"])

    def test_cut_off_link_fails_before_any_call(self):
        sid = self.source("https://drive.google.com/file/")
        with mock.patch.object(gdrive_promos, "gws") as call:
            with self.assertRaisesRegex(RuntimeError, "no file or folder id"):
                self.adapter.snapshot(sid)
        call.assert_not_called()

    def test_download_checks_md5_and_publishes_atomically(self):
        sid = self.source("https://drive.google.com/file/d/1Mp3FileIdXX/view")
        body = b"ID3 audio bytes"
        destination = Path(self.tmp.name) / "out" / "track.mp3"

        def fake(*args, save_to=None):
            Path(save_to).write_bytes(body)
            return {"saved": [str(save_to)], "bytes": len(body)}

        entry = {"id": "1Mp3FileIdXX", "size": len(body), "md5": hashlib.md5(body).hexdigest()}
        with mock.patch.object(gdrive_promos, "gws", side_effect=fake):
            size, sha = self.adapter._download_entry(sid, entry, destination)
        self.assertEqual((size, sha), (len(body), hashlib.sha256(body).hexdigest()))
        self.assertEqual(destination.read_bytes(), body)

        entry["md5"] = "0" * 32
        other = destination.with_name("bad.mp3")
        with mock.patch.object(gdrive_promos, "gws", side_effect=fake):
            with self.assertRaisesRegex(RuntimeError, "MD5"):
                self.adapter._download_entry(sid, entry, other)
        self.assertEqual(sorted(p.name for p in other.parent.iterdir()), ["track.mp3"])

    def test_formats_of_one_mix_merge_and_bare_names_get_their_type(self):
        sid = self.source("https://drive.google.com/drive/folders/1RootFolderId", "Exclusive Promo")
        listing = {"files": [
            {"id": "1JanusMp3IdX", "name": "Delnoro - Janus [Sunwaves Digital].mp3", "mimeType": "audio/mpeg", "size": "10"},
            {"id": "1JanusWavIdX", "name": "Delnoro - Janus [Sunwaves Digital].wav", "mimeType": "audio/wav", "size": "90"},
            {"id": "1BareFileIdX", "name": "Dj Nastor - State Of Mind", "mimeType": "audio/mpeg", "size": "10"}]}

        def fake(*args, save_to=None):
            return {"id": "1RootFolderId", "name": "SWD111"} if args[1:3] == ("files", "get") else listing

        with mock.patch.object(gdrive_promos, "gws", side_effect=fake):
            release = self.adapter.release(sid)
        offers = {t["name"]: sorted(f["type"] for f in t["files"]) for t in release["tracks"]}
        self.assertEqual(offers, {"Delnoro - Janus": ["mp3", "wav"], "Dj Nastor - State Of Mind": ["mp3"]})


if __name__ == "__main__":
    unittest.main()
