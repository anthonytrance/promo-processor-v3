import email.utils
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import inflyte
from promo_automation import Store


def response(data=None, status=200):
    return mock.Mock(status_code=status, json=mock.Mock(return_value=data))


class InflyteCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(self.temporary.name)
        self.addCleanup(self.store.db.close)
        log_patch = mock.patch.object(self.store, "sync_logs")
        self.logs = log_patch.start()
        self.addCleanup(log_patch.stop)
        self.adapter = inflyte.Inflyte.__new__(inflyte.Inflyte)
        self.adapter.store, self.adapter.calls, self.adapter.csrf = self.store, 0, "test-csrf"
        self.adapter.save_token = mock.Mock()
        for target in ("curl_cffi.requests.Session.request", "requests.sessions.Session.request",
                       "socket.create_connection", "socket.socket.connect"):
            patch = mock.patch(target, side_effect=AssertionError("Tests cannot contact accounts"))
            patch.start()
            self.addCleanup(patch.stop)

    def mail(self, rid, age=1):
        import time
        sid = self.store.add_source("inflyte", f"Mail {rid}", f"https://inflyteapp.com/review/releases/{rid}")
        self.store.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
            ("test@example.invalid", "INBOX", "1", str(rid), f"<{rid}@example.invalid>", f"Mail {rid}",
             email.utils.formatdate(time.time() - age * 86400, usegmt=True), sid))
        self.store.db.commit()
        return sid

    @staticmethod
    def locker(rid):
        return {"id": rid, "n": "Alpha - Song [Label]", "s": "Label", "code": "ABC01", "w": 1,
                "t": [{"i": rid * 10, "n": "Song (Extended Mix)", "a": "Alpha", "d": "6:12",
                       "t": ["wav", "aif", "mp3"]}]}

    def cached(self, rid, **fields):
        entry = inflyte.locker_entry(self.locker(rid))
        entry.update(metadata_checked_at=inflyte.now(), newest_mail_at_inspection=self.adapter._newest_mail(
            self.store.db.execute("SELECT id FROM sources WHERE platform='inflyte' AND remote_id=?", (str(rid),)).fetchone()[0]))
        entry.update(fields)
        inflyte.protect_json(self.store.root / "inflyte-catalogue.dpapi", {str(rid): entry})
        return entry

    def test_catalogue_unions_live_recent_locker_and_all_mailed_placeholders_without_opening(self):
        for rid in (1, 2, 3, 4):
            self.mail(rid)
        self.mail(5, age=40)
        self.adapter.feed = mock.Mock(return_value={"promos": [{"id": 1, "name": "Live", "reviewed": False}],
                                                   "tracks": [{"id": 10, "release_id": 1}]})
        self.adapter.request = mock.Mock(side_effect=[response({"data": [self.locker(1), self.locker(2), self.locker(5)]}),
                                                      response({"results": [{"t": "promo", "id": 3}]})])
        result = self.adapter.catalogue()
        snapshot = inflyte.read_protected_json(self.store.root / "inflyte-catalogue.dpapi")
        self.assertEqual(set(snapshot), {"1", "2", "3", "4"})
        self.assertEqual(snapshot["1"]["origin"], "live")
        self.assertEqual(snapshot["2"]["origin"], "locker")
        self.assertNotIn("timestamp", snapshot["2"]["promo"], "rating time must not become the offer date")
        self.assertEqual(result["coverage"]["detail_pending_ids"], ["3", "4"])
        self.assertEqual(result["coverage"]["not_in_account_indexes"], ["4"])
        self.assertEqual([c.args[1] for c in self.adapter.request.call_args_list],
                         ["/review/releases/locker.json", "/search"])
        self.logs.assert_not_called()

    def test_locker_schema_preserves_duration_formats_and_download_permission(self):
        sid = self.mail(2)
        entry = self.cached(2)
        entry["promo"]["deleted"] = True
        entry["availability_checked"] = "test"
        inflyte.protect_json(self.store.root / "inflyte-catalogue.dpapi", {"2": entry})
        release = self.adapter.planning_release(sid)
        self.assertFalse(release["gated"])
        self.assertEqual(release["tracks"][0]["duration"], "6:12")
        self.assertEqual([f["type"] for f in release["tracks"][0]["files"]], ["wav", "aif", "mp3"])

    def test_detail_intent_is_published_before_opening_and_result_is_reused(self):
        sid = self.mail(3)
        def request(method, path, **kwargs):
            self.logs.assert_called_once()
            event = self.store.db.execute("SELECT action,status FROM events WHERE source_id=?", (sid,)).fetchone()
            self.assertEqual(tuple(event), ("view", "intent"))
            return response({"promo_data": {"promos": [{"id": 3, "name": "A - Song", "deleted": True,
                                                         "extra": {"can_download": True}}],
                                             "tracks": [{"id": 30, "release_id": 3, "artist_name": "A",
                                                         "name": "Song", "types": "wav", "duration": 320}]}})
        self.adapter.request = mock.Mock(side_effect=request)
        self.adapter.check_availability = mock.Mock(side_effect=lambda sid, rid, entry: entry)
        self.assertEqual(self.adapter.release(sid)["tracks"][0]["name"], "A - Song")
        self.adapter.release(sid)
        self.adapter.request.assert_called_once()

    def test_unknown_view_intent_is_never_followed_without_published_log(self):
        sid = self.mail(3)
        self.logs.side_effect = RuntimeError("log unavailable")
        self.adapter.request = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "log unavailable"):
            self.adapter.release(sid)
        self.adapter.request.assert_not_called()

    def test_old_or_undated_email_does_not_trigger_detail_request(self):
        sid = self.mail(3, age=40)
        self.adapter.request = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "recent emailed offer"):
            self.adapter.release(sid)
        self.adapter.request.assert_not_called()

    def test_detail_refusal_and_wrong_identity_are_not_silently_skipped(self):
        sid = self.mail(3)
        for reply, error in ((response(status=404), inflyte.ReleaseUnavailable),
                             (response({"promo_data": {"promos": [{"id": 99}], "tracks": []}}), RuntimeError)):
            self.adapter.request = mock.Mock(return_value=reply)
            with self.assertRaises(error):
                self.adapter.release(sid)
        self.assertFalse((self.store.root / "inflyte-catalogue.dpapi").exists())

    def test_batch_does_not_skip_manager_deleted_or_recently_reoffered_release(self):
        sid = self.mail(2)
        entry = inflyte.locker_entry(self.locker(2))
        entry["promo"].update(deleted=True, timestamp="2020-01-01T00:00:00Z")
        inflyte.protect_json(self.store.root / "inflyte-catalogue.dpapi", {"2": entry})
        self.adapter.catalogue = mock.Mock()
        with mock.patch.object(inflyte, "LibraryIndex"), mock.patch.object(inflyte, "collect_release",
                return_value={"tracks": [{"action": "skip"}]}) as collect:
            self.adapter.batch(dry_run=True)
        self.assertEqual(collect.call_args.args[2], sid)

    def test_expired_recipient_html_blocks_before_feedback_even_when_json_serves_tracks(self):
        sid = self.mail(2)
        self.cached(2)
        self.adapter.request = mock.Mock(return_value=mock.Mock(status_code=200,
            text='<title>Inflyte: Release - Song</title><div class="alert">This release is no longer available to rate or download. You can purchase it here.</div>'))
        for _ in range(2):
            with self.assertRaisesRegex(inflyte.ReleaseUnavailable, "no longer available"):
                self.adapter.release(sid)
        self.adapter.request.assert_called_once()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM feedback").fetchone()[0], 0)

    def test_feed_uses_entire_signed_id_list_and_checks_mailbox_identity(self):
        promos = [{"id": rid} for rid in range(1, 301)]
        data = {"base_data": {"reviewer_email": inflyte.ACCOUNT}, "promos": promos, "release_sig": "fake"}
        html = '<meta name="csrf-token" content="fake"><script>var initialData = ' + json.dumps(data) + ';</script>'
        self.adapter.request = mock.Mock(side_effect=[mock.Mock(status_code=200, text=html), response({"tracks": []})])
        self.assertEqual(len(self.adapter.feed()["promos"]), 300)
        self.assertEqual(self.adapter.request.call_args.kwargs["data"]["release_ids"], ",".join(str(rid) for rid in range(1, 301)))
        html = html.replace(inflyte.ACCOUNT, "different@example.invalid")
        self.adapter.request = mock.Mock(return_value=mock.Mock(status_code=200, text=html))
        with self.assertRaisesRegex(inflyte.TokenExpired, "different"):
            self.adapter.feed()
        self.adapter.request.assert_called_once()

    def test_non_expiry_alert_is_not_treated_as_withdrawal(self):
        self.assertIsNone(inflyte.availability_message('<div class="alert">Please rate this release</div>'))

    def test_catalogue_preserves_recent_hidden_detail_without_reopening_it(self):
        sid = self.mail(3)
        old = self.cached(3, origin="email_detail", availability_checked=inflyte.now(), unavailable_reason=None)
        self.adapter.feed = mock.Mock(return_value={"promos": [], "tracks": []})
        self.adapter.request = mock.Mock(side_effect=[response({"data": []}), response({"results": []})])
        self.adapter.catalogue()
        entry = inflyte.read_protected_json(self.store.root / "inflyte-catalogue.dpapi")["3"]
        self.assertEqual(entry, old)
        self.assertEqual(self.adapter.planning_release(sid)["tracks"][0]["track_id"], 30)
        self.assertEqual(self.adapter.request.call_count, 2)
        self.logs.assert_not_called()

    def test_newer_mail_invalidates_hidden_detail_and_expiry(self):
        sid = self.mail(3)
        self.cached(3, origin="email_detail", metadata_checked_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),
                    unavailable_reason="Expired before this new offer", availability_checked=inflyte.now())
        self.store.db.execute("UPDATE messages SET received=? WHERE source_id=?",
                              (email.utils.format_datetime(datetime.now(timezone.utc)), sid))
        self.store.db.commit()
        self.adapter.feed = mock.Mock(return_value={"promos": [], "tracks": []})
        self.adapter.request = mock.Mock(side_effect=[response({"data": []}), response({"results": []})])
        self.adapter.catalogue()
        entry = inflyte.read_protected_json(self.store.root / "inflyte-catalogue.dpapi")["3"]
        self.assertTrue(entry["needs_detail"])
        self.assertNotIn("unavailable_reason", entry)

    def test_legacy_snapshot_requires_recent_release_specific_inspection_and_no_newer_offer(self):
        sid = self.mail(3)
        old = inflyte.locker_entry(self.locker(3))
        self.assertFalse(self.adapter._cached_nonlive(sid, "3", old))
        self.store.event(sid, "view", "inspected", tracks=1)
        self.assertTrue(self.adapter._cached_nonlive(sid, "3", old))
        wrong = {**old, "promo": {**old["promo"], "id": 999}}
        self.assertFalse(self.adapter._cached_nonlive(sid, "3", wrong))
        old["metadata_checked_at"] = (datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
        self.assertFalse(self.adapter._cached_nonlive(sid, "3", old))

    def test_week_old_owned_hidden_release_is_planned_locally_without_recipient_checks(self):
        sid = self.mail(3, age=20)
        self.cached(3, origin="email_detail", metadata_checked_at=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat())
        library = mock.Mock()
        library.matches.return_value = ["/Music Library/Alpha - Song (Extended Mix) [Label].aiff"]
        self.adapter.request = mock.Mock(side_effect=AssertionError("Owned release must not be reopened"))
        result = self.adapter._collect_planned(sid, library)
        self.assertEqual(result["tracks"][0]["action"], "skip")
        self.adapter.request.assert_not_called()
        self.logs.assert_not_called()

    def test_fresh_locker_added_remix_is_not_hidden_by_owned_main_mix_or_old_expiry(self):
        sid = self.mail(2)
        self.cached(2, unavailable_reason="An earlier expiry result", availability_checked=inflyte.now())
        locker = self.locker(2)
        locker["t"].append({"i": 21, "n": "Song (Different Remix)", "a": "Alpha", "d": "6:20", "t": ["wav"]})
        self.adapter.feed = mock.Mock(return_value={"promos": [], "tracks": []})
        self.adapter.request = mock.Mock(side_effect=[response({"data": [locker]}), response({"results": []})])
        self.adapter.catalogue()
        library = mock.Mock()
        library.matches.side_effect = lambda key, *names: [] if any("Different Remix" in n for n in names) else ["owned.aiff"]
        result = self.adapter._collect_planned(sid, library, dry_run=True)
        self.assertEqual([t["action"] for t in result["tracks"]], ["skip", "download"])
        self.assertEqual(self.adapter.request.call_count, 2)

    def test_unchanged_locker_keeps_explicit_expiry_without_reopening(self):
        sid = self.mail(2)
        self.cached(2, unavailable_reason="Explicitly expired", availability_checked=inflyte.now())
        self.adapter.feed = mock.Mock(return_value={"promos": [], "tracks": []})
        self.adapter.request = mock.Mock(side_effect=[response({"data": [self.locker(2)]}), response({"results": []})])
        self.adapter.catalogue()
        with self.assertRaisesRegex(inflyte.ReleaseUnavailable, "Explicitly expired"):
            self.adapter.planning_release(sid)
        self.assertEqual(self.adapter.request.call_count, 2)

    def test_actual_nonlive_work_checks_availability_again_even_with_old_success(self):
        sid = self.mail(2)
        self.cached(2, availability_checked=inflyte.now(), unavailable_reason=None)
        self.adapter.request = mock.Mock(return_value=mock.Mock(status_code=200,
            text='<title>Inflyte: Release - Song</title><div class="alert">Please rate this release</div>'))
        self.adapter.planning_release(sid)
        self.adapter.request.assert_not_called()
        self.adapter.release(sid)
        self.adapter.release(sid)
        self.adapter.request.assert_called_once()
        self.assertEqual(self.adapter.request.call_args.args[1], "/review/releases/2")

    def test_planning_fast_path_still_verifies_the_mailbox(self):
        sid = self.mail(2)
        self.cached(2)
        self.adapter.csrf = None
        self.adapter.feed = mock.Mock(side_effect=inflyte.TokenExpired("wrong mailbox"))
        with self.assertRaisesRegex(inflyte.TokenExpired, "wrong mailbox"):
            self.adapter.planning_release(sid)
        self.adapter.feed.assert_called_once()

    def test_previous_live_inventory_remains_local_but_does_not_imply_current_availability(self):
        sid = self.mail(2)
        live = inflyte.locker_entry(self.locker(2))
        self.adapter.feed = mock.Mock(side_effect=[{"promos": [live["promo"]], "tracks": live["tracks"]},
                                                  {"promos": [], "tracks": []}])
        self.adapter.request = mock.Mock(side_effect=[response({"data": []}), response({"results": []}),
                                                      response({"data": []}), response({"results": []})])
        self.adapter.catalogue()
        self.adapter.catalogue()
        entry = inflyte.read_protected_json(self.store.root / "inflyte-catalogue.dpapi")["2"]
        self.assertEqual(entry["origin"], "email_cached")
        self.assertNotIn("availability_checked", entry)
        self.assertEqual(len(self.adapter.planning_release(sid)["tracks"]), 1)
        self.assertEqual(self.adapter.request.call_count, 4)
        self.logs.assert_not_called()

    def test_explicit_planning_refresh_detects_hidden_remix_without_availability_or_feedback(self):
        sid = self.mail(3)
        self.cached(3, origin="email_detail", unavailable_reason="Old explicit expiry")
        track = {"id": 31, "release_id": 3, "artist_name": "Alpha", "name": "Song (New Remix)",
                 "types": "wav", "duration": 350}
        self.adapter.request = mock.Mock(return_value=response({"promo_data": {
            "promos": [{"id": 3, "name": "Song", "extra": {"can_download": True}}], "tracks": [track]}}))
        release = self.adapter.planning_release(sid, refresh=True)
        self.assertEqual(release["tracks"][0]["track_id"], 31)
        self.adapter.request.assert_called_once()
        self.assertEqual(self.adapter.request.call_args.args[1], "/review/releases/3.json")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM feedback").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE action='availability'").fetchone()[0], 0)

    def test_adapter_inventory_cache_reuses_decrypt_and_detects_external_file_changes(self):
        sid = self.mail(2)
        self.cached(2)
        original = inflyte.read_protected_json
        with mock.patch.object(inflyte, "read_protected_json", wraps=original) as read:
            self.adapter.planning_release(sid)
            self.adapter.planning_release(sid)
            self.assertEqual(read.call_count, 1)
            entry = original(self.store.root / "inflyte-catalogue.dpapi")["2"]
            entry["tracks"][0]["name"] = "Song (External New Remix)"
            inflyte.protect_json(self.store.root / "inflyte-catalogue.dpapi", {"2": entry})
            release = self.adapter.planning_release(sid)
            self.assertIn("External New Remix", release["tracks"][0]["name"])
            self.assertEqual(read.call_count, 2)

    def test_own_catalogue_writes_publish_updated_adapter_cache_without_decryption(self):
        sid = self.mail(2)
        entry = self.cached(2)
        entry["tracks"][0]["name"] = "Song (Own Updated Remix)"
        with mock.patch.object(inflyte, "read_protected_json", side_effect=AssertionError("Own save must update local cache")):
            self.adapter._save_catalogue({"2": entry})
            self.assertIn("Own Updated Remix", self.adapter.planning_release(sid)["tracks"][0]["name"])

    def test_square_bracket_remixes_keep_distinct_identity_and_radio_edits_group(self):
        from promo_pipeline import mix_key, select_versions
        names = [inflyte.split_track("BLR ft. Lou", f"Move It On [{mix} {version} Mix]")[1]
                 for mix in ("Podium", "AM") for version in ("Extended", "Radio")]
        tracks = [{"name": "BLR ft. Lou - " + name} for name in names]
        selected = select_versions(tracks)
        self.assertEqual(sum(v[0] for v in selected.values()), 2)
        self.assertEqual(len({mix_key(t["name"]) for t in tracks}), 4)

    def test_platform_label_suffix_does_not_hide_the_mix_marker(self):
        title = inflyte.split_track("R3HAB & ARTBAT", "Dramma (Radio Edit) [UPPERGROUND]", "UPPERGROUND")[1]
        self.assertEqual(title, "Dramma (Radio Edit)")
        self.assertEqual(inflyte.version_kind("R3HAB & ARTBAT - " + title), "short")

    def test_dash_versions_group_without_changing_dashes_inside_a_remix_name(self):
        from promo_pipeline import version_group
        full = inflyte.track_title("Disconnected - Tibursky Vocal Edit")
        short = inflyte.track_title("Disconnected - Tibursky Vocal Edit - Short Mix")
        self.assertEqual(version_group(full), version_group(short))
        self.assertEqual(inflyte.track_title("Love (Cheat Codes - Extended Mix)"), "Love (Cheat Codes - Extended Mix)")


if __name__ == "__main__":
    unittest.main()
