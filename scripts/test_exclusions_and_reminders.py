import email.utils
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import promo_mail
import promo_research
from promo_automation import DEST, HOLD, Store


def mail(store, source_id, days_ago, uid):
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    store.db.execute("INSERT INTO messages VALUES('m','INBOX','1',?,?,?,?,?)",
                     (uid, "<" + uid + "@x>", "s", email.utils.format_datetime(moment), source_id))
    store.db.commit()


class ReminderAgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_email_led_reminder_does_not_reopen_an_old_promo(self):
        first = self.store.add_source("promochoons", "Song", "https://promochoons.com/listen?campaign=77&u=a")
        reminder = self.store.add_source("promochoons", "Re: Song", "https://promochoons.com/listen?campaign=77&u=b")
        mail(self.store, first, 40, "1")
        mail(self.store, first, 3, "2")
        mail(self.store, reminder, 2, "3")
        self.assertAlmostEqual(promo_mail.newest_offer_age(self.store, first), 40, delta=0.1)
        self.assertAlmostEqual(promo_mail.newest_offer_age(self.store, reminder), 40, delta=0.1,
                               msg="a reminder with a new recipient key is the same campaign")
        other = self.store.add_source("promochoons", "Other", "https://promochoons.com/listen?campaign=78&u=a")
        mail(self.store, other, 2, "4")
        self.assertAlmostEqual(promo_mail.newest_offer_age(self.store, other), 2, delta=0.1)

    def test_shared_folder_keeps_the_newest_mail_because_files_can_be_added(self):
        share = self.store.add_source("dropbox", "Pack", "https://www.dropbox.com/scl/fo/abc/xyz")
        mail(self.store, share, 40, "1")
        mail(self.store, share, 3, "2")
        self.assertAlmostEqual(promo_mail.newest_offer_age(self.store, share), 3, delta=0.1)


class ExcludedSweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.sync_logs = lambda force=False: None
        self.deleted = []
        self.client = SimpleNamespace(files_delete_v2=lambda path: self.deleted.append(path))
        paths = [DEST + "/Cajmere - Brighter Days (CASHEW Remix) (Extended Mix) [Self Released].aiff",
                 DEST + "/Other Artist - Song (Extended Mix) [Self Released].aiff",
                 DEST + "/Someone - Tune (Original Mix) [Platoon].aiff",
                 DEST + "/Someone - Keep (Original Mix) [Good Label].aiff",
                 HOLD + "/Someone - Held (Original Mix) [Platoon].aiff"]
        for path in paths:
            self.store.db.execute("INSERT INTO library(path) VALUES(?)", (path.lower(),))
        self.store.db.commit()
        self.rules = {"labels": ["platoon"], "artists": ["cajmere"], "title_words": []}

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_dry_run_lists_and_delivery_removes_only_excluded_test_folder_files(self):
        found = promo_research.purge_excluded(self.store, self.client, False, self.rules)
        self.assertEqual(sorted(f["file"][:8] for f in found), ["cajmere ", "someone "])
        self.assertEqual(self.deleted, [])
        promo_research.purge_excluded(self.store, self.client, True, self.rules)
        self.assertEqual(len(self.deleted), 2)
        self.assertFalse(any("needs research" in p for p in self.deleted), "held files are research's own job")
        left = [r[0] for r in self.store.db.execute("SELECT path FROM library")]
        self.assertEqual(len(left), 3)
        self.assertIn("self released", " ".join(left), "self released by another artist stays")

    def test_empty_list_removes_nothing(self):
        self.assertEqual(promo_research.purge_excluded(self.store, self.client, True,
                                                       {"labels": [], "artists": [], "title_words": []}), [])
        self.assertEqual(self.deleted, [])
