import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

import dropbox_promos as direct
import promo_final_audit as audit
import promo_remote
import promo_update as update
from promo_automation import Store
from test_promo_pipeline import make_wav
from process_promos import write_tags


class PassTests(unittest.TestCase):
    def test_continue_pass_joins_the_previous_run_only_when_asked(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            try:
                self.assertEqual(update.open_pass(store, "2026-10-01T00:00:00+00:00", True), ("2026-10-01T00:00:00+00:00", False))
                self.assertEqual(update.open_pass(store, "2026-10-02T00:00:00+00:00", True), ("2026-10-01T00:00:00+00:00", True))
                self.assertEqual(update.open_pass(store, "2026-10-03T00:00:00+00:00", False), ("2026-10-03T00:00:00+00:00", False))
                self.assertEqual(store.pass_started(), "2026-10-03T00:00:00+00:00")
            finally:
                store.db.close()


class ArchiveTests(unittest.TestCase):
    def test_nested_audio_is_extracted_and_images_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "pack.zip"
            with zipfile.ZipFile(source, "w") as z:
                z.writestr("folder/A - Song.wav", b"audio")
                z.writestr("folder/art.jpg", b"image")
            files = direct.extract_audio(source, Path(root) / "out")
            self.assertEqual([p.name for p in files], ["A - Song.wav"])
            self.assertEqual(files[0].read_bytes(), b"audio")

    def test_traversal_and_case_collisions_are_rejected(self):
        for members in (("../escape.wav",), ("A.wav", "a.wav"), ("C:/escape.wav",)):
            with self.subTest(members=members), tempfile.TemporaryDirectory() as root:
                source = Path(root) / "bad.zip"
                with zipfile.ZipFile(source, "w") as z:
                    for member in members:
                        z.writestr(member, b"data")
                with self.assertRaises(RuntimeError):
                    direct.extract_audio(source, Path(root) / "out")

    def test_original_label_conflict_is_held(self):
        obj = direct.DropboxPromos.__new__(direct.DropboxPromos)
        obj.store = mock.Mock()
        track = {"name": "A - Song", "artist": "A", "label": None}
        with mock.patch.object(direct, "read_existing_tags", return_value={"publisher": "One", "grouping": "Two"}):
            obj.resolve_metadata("source", track, Path("original.wav"))
        self.assertIn("metadata_conflicts", track)
        self.assertIsNone(track["label"])

    def test_dl_one_preserves_access_key_and_blocks_external_redirects(self):
        response = mock.Mock(is_redirect=True, headers={"Location": "https://evil.example/file.wav"})
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response
        with mock.patch.object(direct.requests, "Session", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "outside Dropbox"):
                direct.direct_download("https://www.dropbox.com/scl/fi/key/a.wav?rlkey=secret&dl=0", Path("unused.wav"))
        url = session.get.call_args.args[0]
        self.assertIn("rlkey=secret", url)
        self.assertIn("dl=1", url)
        self.assertNotIn("dl=0", url)


class FinalAuditTests(unittest.TestCase):
    def test_normalized_filename_and_tags_pass_but_mismatch_is_detected(self):
        import subprocess
        with tempfile.TemporaryDirectory() as root:
            wav = Path(root) / "source.wav"
            aiff = Path(root) / "cache.aiff"
            make_wav(wav, seconds=1)
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-c:a", "pcm_s24be", str(aiff)], check=True)
            self.assertEqual(write_tags(str(aiff), {"artist": "Alpha", "title": "Song (Extended Mix)", "label": "Label"}), [])
            issues, _, _ = audit.filename_and_tag_issues("Alpha - Song (Extended Mix) [Label].aiff", aiff)
            self.assertEqual(issues, [])
            issues, _, _ = audit.filename_and_tag_issues("Alpha - Wrong (Extended Mix) [Label].aiff", aiff)
            self.assertTrue(any("Filename" in issue for issue in issues))
            self.assertEqual(audit.pcm_identity(wav), audit.pcm_identity(aiff))

    def test_removal_is_limited_to_test_and_revision_guarded(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            client = mock.Mock()
            remote = SimpleNamespace(id="id:one", rev="rev1", name="A.aiff", path_lower="/music library/promos/latest promos/a.aiff", path_display="/Music Library/Promos/Latest Promos/A.aiff")
            with self.assertRaises(ValueError):
                audit.remove_test_copy(store, client, remote, "duplicate", "/existing")
            with self.assertRaises(ValueError):
                promo_remote.hold_remote(store, client, remote, ["label unconfirmed"])
            client.files_delete_v2.assert_not_called()
            client.files_move_v2.assert_not_called()
            remote.path_lower = audit.DEST.lower() + "/a.aiff"
            remote.path_display = audit.DEST + "/A.aiff"
            with mock.patch.object(store, "sync_logs"):
                audit.remove_test_copy(store, client, remote, "same audio", "/existing")
            client.files_delete_v2.assert_called_once_with("id:one", parent_rev="rev1")
            client.files_move_v2.return_value.metadata = SimpleNamespace(
                id="id:one", path_display=audit.HOLD + "/A.aiff", path_lower=audit.HOLD.lower() + "/a.aiff")
            promo_remote.hold_remote(store, client, remote, ["label unconfirmed"])
            client.files_move_v2.assert_called_once_with(remote.path_lower, audit.HOLD + "/A.aiff", autorename=False)
            note = client.files_upload.call_args[0]
            self.assertEqual(note[1], audit.HOLD + "/A.txt")
            self.assertIn(b"label unconfirmed", note[0])
            store.db.close()


class LoginRequired(RuntimeError):
    pass


def write(path, value):
    Path(path).write_text(json.dumps(value), encoding="utf-8")


def planned(sid, *tracks):
    return {"source_id": sid, "release": sid, "state": "planned",
            "plan": {"tracks": [{"track": t, "action": a, "format": f} for t, a, f in tracks]}}


class CoordinatorTests(unittest.TestCase):
    def test_release_failure_is_reported_without_hiding_later_releases(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            try:
                first = store.add_source("promoly", "One", "https://ln.promo.ly/one")
                second = store.add_source("promoly", "Two", "https://ln.promo.ly/two")
                def collect(s, a, sid, library, **kwargs):
                    if sid == first:
                        raise RuntimeError("contract changed")
                    return {"tracks": [{"track": "A - New", "action": "skip"}]}
                with mock.patch.object(update, "adapter_for", return_value=mock.Mock(spec=[])), \
                        mock.patch("promo_mail.newest_offer_age", return_value=1), \
                        mock.patch("promo_pipeline.collect_release", side_effect=collect), \
                        mock.patch.object(store, "sync_logs"):
                    states = update.discover_pass(store, "promoly", Path(root), 30)
                report = json.loads((Path(root) / "promoly-plan.json").read_text(encoding="utf-8"))
                self.assertEqual(states, {"exception": 1, "planned": 1})
                self.assertEqual({r["source_id"]: r["state"] for r in report["releases"]}[second], "planned")
            finally:
                store.db.close()

    def test_login_failure_stops_requests_but_reports_every_release(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            try:
                for name in ("One", "Two", "Three"):
                    store.add_source("promoly", name, "https://ln.promo.ly/" + name)
                collect = mock.Mock(side_effect=LoginRequired("session expired"))
                with mock.patch.object(update, "adapter_for", return_value=mock.Mock(spec=[])), \
                        mock.patch("promo_mail.newest_offer_age", return_value=1), \
                        mock.patch("promo_pipeline.collect_release", collect), \
                        mock.patch.object(store, "sync_logs"):
                    states = update.discover_pass(store, "promoly", Path(root), 30)
                self.assertEqual(collect.call_count, 1)
                self.assertEqual(states, {"access_blocked": 3})
            finally:
                store.db.close()

    def test_campaign_choice_replaces_the_age_filter(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            try:
                kept = store.add_source("needlepool", "Kept", "https://needlepool.invalid/a")
                alias = store.add_source("needlepool", "Alias", "https://needlepool.invalid/b")
                adapter = mock.Mock(spec=["campaigns"])
                adapter.campaigns.return_value = [kept]
                with mock.patch("promo_mail.newest_offer_age", side_effect=AssertionError("not used")):
                    rows = update.window_sources(store, adapter, "needlepool", 30)
                self.assertEqual({sid: state for sid, _, state in rows}, {kept: None, alias: "outside_window_or_alias"})
            finally:
                store.db.close()

    def test_best_copy_is_lossless_then_platform_order_and_disabled_platforms_never_win(self):
        with tempfile.TemporaryDirectory() as root:
            run = Path(root)
            write(run / "inflyte-plan.json", {"releases": [planned("i1", ("A - Song (Extended Mix)", "download", "mp3"),
                                                                     ("B - Tune", "download", "wav"))]})
            write(run / "promoly-plan.json", {"releases": [planned("p1", ("A - Song (Extended)", "download", "wav"),
                                                                     ("B - Tune", "download", "wav"),
                                                                     ("C - Alone", "download", "mp3"))]})
            write(run / "fatdrop-plan.json", {"collection_disabled": "pilot pending",
                                              "releases": [planned("f1", ("A - Song (Extended Mix)", "download", "aiff"))]})
            counts = update.plan_globally(run, ("inflyte", "promoly", "fatdrop"))
            defer = json.loads((run / "global-plan.json").read_text(encoding="utf-8"))["defer"]
            self.assertEqual(counts, {"duplicate_offers": 2, "deferred": 2})
            self.assertEqual(set(defer["inflyte"]["i1"]), {"A - Song (Extended Mix)"})
            self.assertEqual(set(defer["promoly"]["p1"]), {"B - Tune"})
            self.assertNotIn("fatdrop", defer)

    def test_collect_applies_deferrals_skips_idle_releases_and_reconcile_ignores_deferrals(self):
        with tempfile.TemporaryDirectory() as root:
            run = Path(root)
            store = Store(root)
            try:
                write(run / "promoly-plan.json", {"releases": [planned("p1", ("A - Song", "download", "mp3")),
                                                               planned("p2", ("B - Old", "skip", "wav"))]})
                write(run / "global-plan.json", {"defer": {"promoly": {"p1": {"A - Song": "better elsewhere"}}}})
                collect = mock.Mock(return_value={"tracks": [{"track": "A - Song", "action": "deferred"}]})
                with mock.patch.object(update, "adapter_for", return_value=mock.Mock(spec=[])), \
                        mock.patch("promo_pipeline.collect_release", collect), \
                        mock.patch.object(store, "sync_logs"):
                    result = update.collect_pass(store, "promoly", run)
                    self.assertEqual(collect.call_count, 1)
                    self.assertEqual(collect.call_args.kwargs["defer"], {"A - Song": "better elsewhere"})
                    self.assertEqual(result["actions"], {"deferred": 1})
                    with mock.patch("promo_review.review", return_value={"counts": {}}):
                        update.collect_pass(store, "promoly", run, reconcile=True)
                    # p1 had a deferred mix whose chosen copy may have failed; p2 was settled and is not reopened.
                    self.assertEqual(collect.call_count, 2)
                    self.assertEqual({c.args[2] for c in collect.call_args_list[1:]}, {"p1"})
                    self.assertIsNone(collect.call_args.kwargs["defer"])
            finally:
                store.db.close()

    def test_reconcile_reopens_only_unsettled_releases(self):
        self.assertTrue(update.fully_delivered({"tracks": [{"track": "A", "action": "download"}, {"delivered": "A.aiff"}]}))
        self.assertTrue(update.fully_delivered({"tracks": [{"track": "A", "action": "skip"}]}))
        self.assertFalse(update.fully_delivered({"tracks": [{"track": "A", "action": "download"}]}))
        self.assertFalse(update.fully_delivered({"tracks": [{"track": "A", "action": "claim_denied"}]}))
        self.assertTrue(update.fully_delivered({"tracks": [{"track": "A", "action": "download"},
                                                           {"track": "A", "action": "held_for_research"}]}))

    def test_housekeeping_keeps_sessions_recent_caches_and_the_latest_runs(self):
        import email.utils as eu
        import os
        import time
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            store = Store(root)
            try:
                for name in ("20261001-000000", "20261002-000000", "20261003-000000", "20261004-000000"):
                    (base / "runs" / name).mkdir(parents=True)
                    (base / "runs" / name / "run.json").write_text("{}", encoding="utf-8")
                current = base / "runs" / "20261005-000000"
                current.mkdir()
                old, new = "a" * 20, "b" * 20
                for sid, days in ((old, 90), (new, 5)):
                    store.db.execute("INSERT INTO sources VALUES(?,'promoly',NULL,'s',NULL,'{}','x')", (sid,))
                    store.db.execute("INSERT INTO messages VALUES('m','INBOX','1',?,?,?,?,?)", (sid, "<x>", "s",
                                     eu.formatdate(time.time() - days * 86400), sid))
                store.db.commit()
                for name in ("promoly-" + old + ".dpapi", "promoly-" + new + ".dpapi", "promoly-session.dpapi",
                             "beatrolo-feedback-" + old + ".dpapi", "ampsuite-" + "c" * 20 + ".dpapi"):
                    (base / name).write_bytes(b"x")
                (base / "staging" / "stale").mkdir(parents=True)
                os.utime(base / "staging" / "stale", (time.time() - 3 * 86400,) * 2)
                (base / "staging" / "busy").mkdir()
                (base / "final-audit-audio").mkdir()
                (base / "final-audit-audio" / "x.aiff").write_bytes(b"x")
                (base / "loose.partial").write_bytes(b"x")
                removed = update.housekeeping(store, current)
                self.assertEqual(sorted(p.name for p in (base / "runs").iterdir()),
                                 ["20261003-000000", "20261004-000000", "20261005-000000"])
                left = {p.name for p in base.glob("*.dpapi")}
                self.assertEqual(left, {"promoly-" + new + ".dpapi", "promoly-session.dpapi",
                                        "beatrolo-feedback-" + old + ".dpapi", "ampsuite-" + "c" * 20 + ".dpapi"})
                self.assertEqual(sorted(p.name for p in (base / "staging").iterdir()), ["busy"])
                self.assertFalse(any((base / "final-audit-audio").iterdir()))
                self.assertFalse((base / "loose.partial").exists())
                self.assertEqual(removed["runs"], 2)
            finally:
                store.db.close()

    def test_upload_slots_are_distinct_and_bounded(self):
        from promo_automation import upload_slot
        with tempfile.TemporaryDirectory() as root:
            with upload_slot(Path(root), slots=2) as one, upload_slot(Path(root), slots=2) as two:
                self.assertEqual({one, two}, {0, 1})
            with upload_slot(Path(root), slots=2) as again:
                self.assertEqual(again, 0)


if __name__ == "__main__":
    unittest.main()
