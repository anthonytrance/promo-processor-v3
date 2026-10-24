from email.message import EmailMessage
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import promo_mail as mail
import promo_update as update
from promo_automation import Store


class UnknownSourceTests(unittest.TestCase):
    def test_new_sender_portal_is_reported_even_beside_supported_link(self):
        report = mail.unrecognised(["https://inflyteapp.com/review/releases/12",
                                   "https://label.invalid/promo/private-key"], "New EP promo", "Label <x@label.invalid>")
        self.assertEqual(report["hosts"], ["label.invalid"])
        self.assertNotIn("private-key", json.dumps(report))

    def test_ep_word_boundary_and_noise_and_consent_filters(self):
        self.assertIsNotNone(mail.unrecognised(["https://portal.invalid/music"], "Artist EP", ""))
        self.assertIsNone(mail.unrecognised(["https://portal.invalid/music"], "Receipt", ""))
        self.assertIsNone(mail.unrecognised(["https://portal.invalid/consent", "https://portal.invalid/art.jpg"], "New promo", ""))
        self.assertTrue(mail.unrecognised(["https://x.list-manage.com/click?secret=1"], "New promo", "")["tracker_only"])

    def test_ctrl_promo_routes_only_promo_pages(self):
        self.assertEqual(mail.route("https://ctrlpromo.com/view-promo/12/fakekeyfakekey1234?mailingId=3"), "ctrl-promo")
        for url in ("https://ctrlpromo.com/", "https://ctrlpromo.com/login", "https://ctrlpromo.com/promos"):
            self.assertIsNone(mail.route(url))

    def test_known_platform_changed_recipient_path_is_reported(self):
        report = mail.unrecognised(["https://inflyteapp.com/new-recipient/12"], "New promo", "")
        self.assertEqual(report["known_platform_unrouted"], ["inflyteapp.com"])

    def test_homepage_footers_and_encoded_newsletter_trackers_are_not_new_sources(self):
        self.assertIsNone(mail.unrecognised(["https://label.invalid/", "https://disco.ac/"], "New promo", ""))
        report = mail.unrecognised(["https://label-dot-yamm-track.appspot.com/click?key=fake"], "New promo", "")
        self.assertEqual(report["hosts"], [])
        self.assertTrue(report["tracker_only"])

    def test_discovery_distinguishes_missing_adapter_from_selected_platform_filter(self):
        message = EmailMessage()
        message["Subject"], message["From"], message["Message-ID"] = "New promo", "x@label.invalid", "<synthetic>"
        message.set_content("https://hearthis.ai/downloads/a?token=fake https://fatdrop.co.uk/promo/a https://fatdrop.co.uk/promo/b")
        client = mock.Mock()
        client.response.return_value = ("OK", [b"1"])
        def uid(command, *args):
            return ("OK", [b"7"]) if command == "search" else ("OK", [(b"1 (UID 7)", message.as_bytes())])
        client.uid.side_effect = uid
        skill = SimpleNamespace(imap_login=mock.Mock(return_value=client), _select=mock.Mock(),
                                parse_date_arg=lambda x: "01-Oct-2026", decode_str=lambda x: x)
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            try:
                with mock.patch.object(mail, "email_skill", return_value=skill), \
                        mock.patch.object(mail, "IMPLEMENTED_PLATFORMS", mail.IMPLEMENTED_PLATFORMS - {"fatdrop"}):
                    report = mail.discover(store, platforms=["promo-cloud"], folders=("INBOX", "Archive"))
            finally:
                store.db.close()
        self.assertEqual(set(report["platforms_without_adapter"]), {"fatdrop"})
        self.assertEqual(report["platforms_without_adapter"]["fatdrop"]["mails"], 2)
        self.assertTrue(all(call.args[-1] == "(UID BODY.PEEK[])" for call in client.uid.call_args_list if call.args[0] == "fetch"))
        self.assertTrue(all(call.kwargs["readonly"] for call in skill._select.call_args_list))

    def test_scanned_mail_is_not_downloaded_again_until_a_needed_source_is_missing(self):
        message = EmailMessage()
        message["Subject"], message["From"], message["Message-ID"] = "New promo", "x@label.invalid", "<synthetic>"
        message.set_content("https://hearthis.ai/downloads/a?token=fake https://fatdrop.co.uk/promo/a")
        client = mock.Mock()
        client.response.return_value = ("OK", [b"1"])
        def uid(command, *args):
            return ("OK", [b"7"]) if command == "search" else ("OK", [(b"1 (UID 7)", message.as_bytes())])
        client.uid.side_effect = uid
        skill = SimpleNamespace(imap_login=mock.Mock(return_value=client), _select=mock.Mock(),
                                parse_date_arg=lambda x: "01-Oct-2026", decode_str=lambda x: x)
        fetches = lambda: sum(c.args[0] == "fetch" for c in client.uid.call_args_list)
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            try:
                with mock.patch.object(mail, "email_skill", return_value=skill):
                    first = mail.discover(store, platforms=["hearthis"])
                    self.assertEqual(fetches(), 1)
                    again = mail.discover(store, platforms=["hearthis"])
                    self.assertEqual(fetches(), 1, "an unchanged message is answered from the saved scan")
                    self.assertEqual(again["link_occurrences"], first["link_occurrences"])
                    self.assertEqual(again["messages_scanned"], 1)
                    wider = mail.discover(store, platforms=["hearthis", "fatdrop"])
                    self.assertEqual(fetches(), 2, "a newly selected platform needs its source stored")
                    self.assertEqual(wider["link_occurrences"], {"hearthis": 1, "fatdrop": 1})
                    mail.discover(store, platforms=["hearthis"])
                    mail.discover(store, platforms=["hearthis", "fatdrop"])
                    self.assertEqual(fetches(), 2)
                    self.assertEqual(store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
            finally:
                store.db.close()

    def test_end_summary_reports_coverage_gaps_and_does_not_excuse_old_unlisted_failure(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            run = Path(root)
            try:
                (run / "run.json").write_text(json.dumps({"started": "2026-10-01", "destination": "/test"}), encoding="utf-8")
                (run / "prepare.json").write_text(json.dumps({"mail": {"platforms_without_adapter": {"fatdrop": {"mails": 2}},
                    "unrecognised_promo_mails": [{"hosts": ["new.invalid"], "tracker_only": False}]}}), encoding="utf-8")
                (run / "inflyte.json").write_text(json.dumps({"releases": [{"state": "exception", "release": "Missing",
                    "error": "not in the current promo list"}]}), encoding="utf-8")
                with mock.patch.object(update, "STATE", run):
                    result = update.summary(store, run)
                self.assertEqual(result["exceptions"], ["inflyte: Missing"])
                self.assertIn("fatdrop (2 messages)", result["text"])
                self.assertIn("new.invalid", result["text"])
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
