import json
from pathlib import Path
from unittest import mock
import unittest

import wetransfer_promos as wt
from promo_cloud import protect_json, read_protected_json
from onedrive_promos import ShareAccessRequired, ShareUnavailable
from test_onedrive_promos import OfflineCase


URL = "https://wetransfer.com/downloads/fake-transfer-123/fake-security-123"


def transfer(**overrides):
    return {"id": "fake-transfer-123", "security_hash": "fake-security-123", "recipient_id": None,
            "state": "downloadable", "expires_at": "2099-01-01T00:00:00Z", "deleted_at": None,
            "password_protected": False, "downloader_email_verification": "anonymous", "paid": False,
            "encrypted": False, "view_option": "preview_and_download", "files_count": 2,
            "title": "Fixture promo", "items": [
                {"id": "fake-file-extended", "name": "Artist - Tune (Extended Mix) [Fixture Records].wav", "size": 900, "item_type": "file"},
                {"id": "fake-file-radio", "name": "Artist - Tune (Radio Edit) [Fixture Records].mp3", "size": 100, "item_type": "file"}],
            **overrides}


def page(value):
    return ('<script id="__NEXT_DATA__" type="application/json">' + json.dumps(
        {"props": {"pageProps": {"initialTransfer": value}}}) + '</script>').encode()


class WeTransferTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.http = mock.Mock(calls=0)
        self.http.page.return_value = (URL, 200, {}, page(transfer()))
        self.adapter = wt.WeTransferPromos(self.store, self.http)
        self.sid = self.source("https://we.tl/t-fake123", platform="wetransfer")

    def test_recipient_route_and_bootstrap_ids_must_match(self):
        self.assertEqual(wt.transfer_from_page(URL, page(transfer()))["id"], "fake-transfer-123")
        recipient = "fake-recipient-123"
        self.assertEqual(wt.transfer_from_page(URL.replace("/fake-security-123", "/" + recipient + "/fake-security-123"),
                         page(transfer(recipient_id=recipient)))["recipient_id"], recipient)
        for value in (transfer(id="other-transfer"), transfer(security_hash="other-security"), transfer(recipient_id="unexpected-recipient")):
            with self.assertRaisesRegex(RuntimeError, "belong"):
                wt.transfer_from_page(URL, page(value))
        for url in ("https://wetransfer.com/log-in", "https://wetransfer.com/downloads/incomplete", "https://wetransfer.com/unsubscribe/fake"):
            with self.assertRaises(ShareAccessRequired):
                wt.recipient_path(url)

    def test_expired_is_terminal_and_status_evidence_is_protected(self):
        self.http.page.return_value = (URL, 200, {}, page(transfer(state="expired", expires_at=None, deleted_at="2020-01-01 00:00:00 UTC")))
        with self.assertRaisesRegex(ShareUnavailable, "expired"):
            self.adapter.snapshot(self.sid)
        self.assertTrue((self.store.root / ("wetransfer-status-" + self.sid + ".dpapi")).exists())
        self.http.json.assert_not_called()
        self.http.original.assert_not_called()

    def test_access_expiry_preview_only_and_unknown_states_fail_closed(self):
        for value in (transfer(password_protected=True), transfer(downloader_email_verification="restricted"),
                      transfer(paid=True), transfer(encrypted=True), transfer(view_option="preview_only"),
                      transfer(state="recaptcha"), transfer(state="processing")):
            with self.assertRaises(ShareAccessRequired):
                wt.availability(value)
        with self.assertRaises(ShareUnavailable):
            wt.availability(transfer(expires_at="2020-01-01T00:00:00Z"))
        with self.assertRaises(RuntimeError):
            wt.availability(transfer(expires_at="unrecognised date"))

    def test_metadata_only_inventory_and_per_mix_version_choice(self):
        inventory = self.adapter.snapshot(self.sid)
        self.assertEqual(len(inventory["files"]), 2)
        from promo_pipeline import collect_release
        library = mock.Mock()
        library.matches.return_value = []
        result = collect_release(self.store, self.adapter, self.sid, library, dry_run=True)
        self.assertEqual([x["action"] for x in result["tracks"]], ["download", "not_wanted"])
        self.http.json.assert_not_called()
        self.http.original.assert_not_called()

    def test_disk_snapshot_reuse_refresh_and_expiry_without_requests(self):
        original = self.adapter.snapshot(self.sid)
        another = wt.WeTransferPromos(self.store, self.http)
        self.http.page.reset_mock()
        self.assertEqual(another.snapshot(self.sid), original)
        self.http.page.assert_not_called()
        another.snapshot(self.sid, refresh=True)
        self.http.page.assert_called_once()
        cache = self.store.root / ("wetransfer-" + self.sid + ".dpapi")
        saved = read_protected_json(cache)
        saved["inventory"]["availability"]["expires_at"] = "2000-01-01T00:00:00Z"
        protect_json(cache, saved)
        expired = wt.WeTransferPromos(self.store, self.http)
        self.http.page.reset_mock()
        with self.assertRaises(ShareUnavailable):
            expired.snapshot(self.sid)
        self.http.page.assert_not_called()

    def test_foreign_and_incomplete_disk_snapshots_refetch_metadata_only(self):
        self.adapter.snapshot(self.sid)
        cache = self.store.root / ("wetransfer-" + self.sid + ".dpapi")
        saved = read_protected_json(cache)
        invalid = [{**saved, "platform": "onedrive"},
                   {**saved, "inventory": {**saved["inventory"], "availability": None}},
                   {**saved, "inventory": {**saved["inventory"], "availability": {}}},
                   {**saved, "inventory": {**saved["inventory"], "files": []}},
                   {**saved, "inventory": {**saved["inventory"], "files": [{}]}}]
        for value in invalid:
            protect_json(cache, value)
            another = wt.WeTransferPromos(self.store, self.http)
            self.http.page.reset_mock()
            another.snapshot(self.sid)
            self.http.page.assert_called_once()
        self.http.json.assert_not_called()
        self.http.original.assert_not_called()

    def test_inventory_does_not_silently_accept_pagination_gaps_or_duplicate_ids(self):
        for value in (transfer(files_count=3), transfer(items=[transfer()["items"][0]] * 2),
                      transfer(items=[{**x, "size": None} for x in transfer()["items"]]),
                      transfer(items=[{**x, "name": "../track.wav"} for x in transfer()["items"]])):
            self.http.page.return_value = (URL, 200, {}, page(value))
            with self.assertRaises(RuntimeError):
                self.adapter.snapshot(self.sid, refresh=True)
        self.http.original.assert_not_called()

    def test_single_file_download_contract_no_download_all_or_preview(self):
        inventory = self.adapter.snapshot(self.sid)
        entry = inventory["files"][0]
        self.http.json.return_value = {"direct_link": "https://download.wetransfer.com/original"}
        self.http.original.return_value = (900, "sha")
        target = Path(self.tmp.name) / "track.wav"
        self.assertEqual(self.adapter.download(self.sid, {"id": entry["id"], "entry": entry}, target), (900, "sha"))
        args, kwargs = self.http.json.call_args
        self.assertEqual(args[0], "https://wetransfer.com/api/v4/transfers/fake-transfer-123/download")
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["json"], {"security_hash": "fake-security-123", "intent": "single_file", "file_ids": [entry["id"]]})
        self.assertEqual(self.http.original.call_args.args[3], 900)

    def test_expiry_or_original_change_before_download_never_requests_link(self):
        inventory = self.adapter.snapshot(self.sid)
        entry = inventory["files"][0]
        for value, expected in ((transfer(state="expired"), ShareUnavailable),
                                (transfer(items=[{**x, "size": 1234} for x in transfer()["items"]]), RuntimeError)):
            self.http.page.return_value = (URL, 200, {}, page(value))
            with self.assertRaises(expected):
                self.adapter.download(self.sid, {"id": entry["id"], "entry": entry}, Path(self.tmp.name) / "track.wav")
        self.http.json.assert_not_called()
        self.http.original.assert_not_called()


if __name__ == "__main__":
    unittest.main()
