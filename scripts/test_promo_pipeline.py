import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import promo_pipeline as pipeline
from promo_automation import Store


def make_wav(path, seconds=6, rate=44100):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate={rate}",
                    "-c:a", "pcm_s24le", str(path)], check=True)


class DuplicateKeyTests(unittest.TestCase):
    def test_base_title_field_does_not_discard_the_offered_mix(self):
        self.assertEqual(pipeline.tag_mismatches(
            {"artist": "A", "name": "A - Song (Intro Mix)", "title": "Song", "mix": "Intro Mix"},
            "Label", {"artist": "A", "title": "Song (Intro Mix)", "label": "Label"}), [])

    def test_square_extended_marker_and_decomposed_accents_keep_one_identity(self):
        self.assertEqual(pipeline.mix_key("A - Cafe\u0301 [Extended]"), pipeline.mix_key("A - Caf\u00e9 (Extended Mix)"))

    def test_loose_key_matches_remixer_credited_as_artist_and_mix_wording(self):
        key = pipeline.loose_key
        self.assertEqual(key("Darude - Sandstorm (ZANS Remix) [Vibing Out].aiff"),
                         key("darude, zans - sandstorm (zans remix) [vibing out].aif"))
        self.assertEqual(key("Darude - Sandstorm (Storm 25 Extended)"),
                         key("Darude - Sandstorm (Storm 25 Mix) (Extended Version)"))
        self.assertEqual(key("KASIA & Faithless - Tarantula (Extended Mix) [Drumcode].aiff"),
                         key("KASIA & Faithless - Tarantula [Extended Mix] [Drumcode].aiff"))
        self.assertNotEqual(key("A - Song (Extended Mix)"), key("A - Song (Radio Edit)"))
        self.assertNotEqual(key("A - Song (Original Mix)"), key("A - Song"))
        self.assertNotEqual(key("A, B - Song (B Remix)"), key("A - Song (C Remix)"))

    def test_metadata_gate_accepts_feature_alias_and_unicode_normalization(self):
        self.assertEqual(pipeline.tag_mismatches(
            {"artist": "NOSI ft. Baby Volcano", "name": "NOSI ft. Baby Volcano - Cafe\u0301 (Extended Mix)"},
            "Label", {"artist": "NOSI feat. Baby Volcano", "title": "Caf\u00e9 (Extended Mix)", "label": "Label"}), [])
        self.assertEqual(pipeline.tag_mismatches(
            {"artist": "Nana Koloda ft.Sayhelooo", "name": "Nana Koloda ft.Sayhelooo - Gandara (Extended Mix)"},
            "Sunrise8", {"artist": "Nana Koloda feat. Sayhelooo", "title": "Gandara (Extended Mix)", "label": "Sunrise8"}), [])

    def test_missing_required_label_is_held(self):
        self.assertTrue(pipeline.tag_mismatches({"artist": "A", "name": "A - S"}, None,
                                              {"artist": "A", "title": "S", "label": None}))

    def test_label_extension_and_artist_order_do_not_change_the_key(self):
        a = pipeline.mix_key("Alpha & Beta - Song (Original Mix) [Black Hole].wav")
        b = pipeline.mix_key("Beta, Alpha - Song (Original Mix).aiff")
        c = pipeline.mix_key("Beta, Alpha - Song (Original Mix) [Other Label].aif")
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_different_mix_is_a_different_key(self):
        self.assertNotEqual(pipeline.mix_key("A - Song (Original Mix)"), pipeline.mix_key("A - Song (Radio Mix)"))

    def test_best_file_prefers_lossless_even_when_mp3_is_larger(self):
        files = [{"id": "m", "type": "MP3", "size_kb": 99999}, {"id": "w", "type": "Wav", "size_kb": 10}]
        self.assertEqual(pipeline.best_file(files)["id"], "w")

    def test_subject_label_parsing(self):
        self.assertEqual(pipeline.label_from_subject("Some Label [SL001] : Artist - Title"), ("Some Label", "SL001"))
        self.assertEqual(pipeline.label_from_subject("No pattern here"), (None, None))


def offer(name, seconds=300):
    return {"name": name, "duration": seconds}


class LibraryKeyCacheTests(unittest.TestCase):
    def test_saved_keys_build_the_same_index_without_parsing_names_again(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            try:
                for path in ("/music library/a/darude, zans - sandstorm (zans remix) [x].aiff",
                             "/music library/a/artist - song (extended mix) [l].wav", "/music library/a/cover.jpg"):
                    store.db.execute("INSERT INTO library(path) VALUES(?)", (path,))
                store.db.commit()
                fresh = pipeline.LibraryIndex(store)
                self.assertEqual(store.db.execute("SELECT count(*) FROM library_keys").fetchone()[0], 2)
                with mock.patch.object(pipeline, "library_keys", side_effect=AssertionError("parsed again")):
                    saved = pipeline.LibraryIndex(store)
                self.assertEqual((saved.by_key, saved.by_loose, saved.by_title, saved.versions.by_work),
                                 (fresh.by_key, fresh.by_loose, fresh.by_title, fresh.versions.by_work))
                with mock.patch.object(pipeline, "KEY_VERSION", "changed rules"):
                    pipeline.LibraryIndex(store)
                self.assertEqual({v for (v,) in store.db.execute("SELECT version FROM library_keys")}, {"changed rules"})
            finally:
                store.db.close()


class VersionSelectionTests(unittest.TestCase):
    def picked(self, tracks):
        return {n for n, (wanted, _) in pipeline.select_versions(tracks).items() if wanted}

    def test_extended_wins_over_radio_and_original(self):
        tracks = [offer("A - S (Radio Mix)", 180), offer("A - S (Original Mix)", 240), offer("A - S (Extended Mix)", 360)]
        self.assertEqual(self.picked(tracks), {"A - S (Extended Mix)"})

    def test_square_mix_markers_select_extended_and_exclude_radio(self):
        self.assertEqual(self.picked([offer("A - S [Extended]"), offer("A - S [Radio]")]), {"A - S [Extended]"})

    def test_single_version_is_taken_whatever_it_is_called(self):
        for name in ["A - S (Radio Mix)", "A - S (Original Mix)", "A - S"]:
            self.assertEqual(self.picked([offer(name)]), {name})

    def test_each_remix_chooses_its_own_extended(self):
        tracks = [offer("A - S (Bob Remix)", 200), offer("A - S (Bob Extended Remix)", 330),
                  offer("A - S (Eve Radio Edit)", 190), offer("A - S (Eve Extended Mix)", 340)]
        self.assertEqual(self.picked(tracks), {"A - S (Bob Extended Remix)", "A - S (Eve Extended Mix)"})

    def test_featuring_bracket_is_not_a_mix_name(self):
        tracks = [offer("A - S (feat. Lexi)", 241), offer("A - S (feat. Lexi) (Extended Mix)", 300),
                  offer("A - T (ft. Bo) (Radio Edit)", 180), offer("A - T (ft. Bo)", 330)]
        self.assertEqual(self.picked(tracks), {"A - S (feat. Lexi) (Extended Mix)", "A - T (ft. Bo)"})

    def test_remix_with_only_one_length_is_kept_next_to_another_remixs_pair(self):
        tracks = [offer("A - S (Bob Remix)", 300), offer("A - S (Eve Radio Edit)", 190), offer("A - S (Eve Extended Mix)", 340)]
        self.assertEqual(self.picked(tracks), {"A - S (Bob Remix)", "A - S (Eve Extended Mix)"})

    def test_longest_version_is_the_main_one_without_extended(self):
        self.assertEqual(self.picked([offer("A - S (Radio Edit)", 170), offer("A - S (Original Mix)", 310)]),
                         {"A - S (Original Mix)"})
        self.assertEqual(self.picked([offer("A - S", 400), offer("A - S (Original Mix)", 310)]), {"A - S"})

    def test_unmarked_beats_radio_edit_without_lengths(self):
        tracks = [{"name": "A - S"}, {"name": "A - S (Radio Edit)"}]
        self.assertEqual(self.picked(tracks), {"A - S"})

    def test_two_unmarked_versions_without_lengths_are_held_for_review(self):
        tracks = [{"name": "A - S"}, {"name": "A - S (Original Mix)"}]
        verdict = pipeline.select_versions(tracks)
        self.assertEqual(self.picked(tracks), set())
        self.assertIn("needs review", verdict["A - S"][1])

    def test_short_dj_edit_and_extended_kept_short_edit_dropped(self):
        tracks = [offer("A - S (Short Edit)", 200), offer("A - S (Short DJ Edit)", 280), offer("A - S (Extended Mix)", 280)]
        self.assertEqual(self.picked(tracks), {"A - S (Short DJ Edit)", "A - S (Extended Mix)"})

    def test_short_radio_edit_alone_is_taken(self):
        self.assertEqual(self.picked([offer("A - S (Radio Edit)", 175)]), {"A - S (Radio Edit)"})

    def test_club_is_a_distinct_alternative_mix(self):
        tracks = [offer("A - S (Original Mix)", 300), offer("A - S (Club Mix)", 360)]
        self.assertEqual(self.picked(tracks), {"A - S (Original Mix)", "A - S (Club Mix)"})

    def test_dub_is_a_separate_version_not_a_shorter_one(self):
        tracks = [offer("A - S (Extended Mix)", 360), offer("A - S (Dub Mix)", 340)]
        self.assertEqual(self.picked(tracks), {"A - S (Extended Mix)", "A - S (Dub Mix)"})

    def test_title_key_ignores_mix_label_and_artist_order(self):
        self.assertEqual(pipeline.title_key("B & A - Song (Extended Mix) [L].aiff"), pipeline.title_key("A, B - Song"))


class TransferTests(unittest.TestCase):
    def test_complete_transfer_is_published_atomically(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "raw" / "a.wav"
            size, digest = pipeline.write_stream([b"x" * 1000, b"y" * 1000], target, 2000)
            self.assertEqual(size, 2000)
            self.assertTrue(target.exists())
            self.assertFalse(target.with_name("a.wav.partial").exists())
            self.assertEqual(len(digest), 64)

    def test_truncated_transfer_is_rejected_and_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "a.wav"
            with self.assertRaises(RuntimeError):
                pipeline.write_stream([b"x" * 500], target, 2000)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_name("a.wav.partial").exists())

    def test_html_error_page_of_wrong_size_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError):
                pipeline.write_stream([b"<html>denied</html>"], Path(root) / "a.wav", 50_000_000)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def fake_library(self, paths):
        index = pipeline.LibraryIndex.__new__(pipeline.LibraryIndex)
        index.by_key = {}
        for path in paths:
            index.by_key.setdefault(pipeline.mix_key(Path(path).name), []).append(path)
        return index

    def offer(self, kind="Wav"):
        return {"best": {"type": kind}}

    def test_new_mix_downloads(self):
        action, _, _ = pipeline.decide(self.store, self.fake_library([]), "k", self.offer())
        self.assertEqual(action, "download")

    def test_legacy_identity_is_found_after_normalization_changes(self):
        self.store.claim("mix:old-square-bracket-key", None, "A - Cafe\u0301 [Extended]", "wav")
        self.store.asset_update("mix:old-square-bracket-key", "delivery_verified", dropbox_path="/done")
        name = "A - Caf\u00e9 (Extended Mix)"
        saved = pipeline.inventory_key(self.store, name)
        self.assertEqual(saved, "mix:old-square-bracket-key")
        self.assertEqual(pipeline.decide(self.store, self.fake_library([]), pipeline.mix_key(name), self.offer(), asset_key=saved)[0], "skip")

    def test_library_copy_skips_even_under_a_different_label_tag(self):
        key = pipeline.mix_key("A - Song (Original Mix)")
        library = self.fake_library(["/Music Library/Promos/Latest Promos/A - Song (Original Mix) [Other].aiff"])
        self.assertEqual(pipeline.decide(self.store, library, key, self.offer())[0], "skip")

    def test_lossless_offer_against_lossy_library_copy_is_only_flagged(self):
        key = pipeline.mix_key("A - Song (Original Mix)")
        library = self.fake_library(["/lib/A - Song (Original Mix) [X].mp3"])
        self.assertEqual(pipeline.decide(self.store, library, key, self.offer("Wav"))[0], "upgrade_candidate")
        self.assertEqual(pipeline.decide(self.store, library, key, self.offer("MP3"))[0], "skip")

    def deliver_lossy(self, key="mix:k", path="/test/A - Song (Original Mix) [X].mp3"):
        self.store.claim(key, None, "A - Song (Original Mix)", "mp3")
        self.store.asset_update(key, "delivery_verified", dropbox_path=path, content_hash="old")

    def test_lossless_replaces_a_lossy_copy_delivered_in_the_same_pass(self):
        self.store.start_pass("2000-01-01T00:00:00+00:00")
        self.deliver_lossy()
        library = self.fake_library(["/test/A - Song (Original Mix) [X].mp3"])
        self.assertEqual(pipeline.decide(self.store, library, "k", self.offer("wav"))[0], "replace")
        self.assertEqual(pipeline.decide(self.store, library, "k", self.offer("MP3"))[0], "skip")

    def test_lossy_copy_from_an_earlier_pass_is_only_flagged(self):
        self.deliver_lossy()
        self.store.start_pass("2999-01-01T00:00:00+00:00")
        self.assertEqual(pipeline.decide(self.store, self.fake_library([]), "k", self.offer("wav"))[0], "upgrade_candidate")

    def test_test_folder_counts_as_one_pass_until_production(self):
        self.deliver_lossy(path=pipeline.DEST + "/A - Song (Original Mix) [X].mp3")
        self.store.start_pass("2999-01-01T00:00:00+00:00")
        self.assertEqual(pipeline.decide(self.store, self.fake_library([]), "k", self.offer("wav"))[0], "replace")
        with mock.patch.object(pipeline, "TEST_FOLDER_IS_ONE_PASS", False):
            self.assertEqual(pipeline.decide(self.store, self.fake_library([]), "k", self.offer("wav"))[0], "upgrade_candidate")

    def test_lossless_delivery_is_never_replaced(self):
        self.store.start_pass("2000-01-01T00:00:00+00:00")
        self.deliver_lossy(path="/test/A - Song (Original Mix) [X].aiff")
        self.assertEqual(pipeline.decide(self.store, self.fake_library([]), "k", self.offer("wav"))[0], "skip")

    def test_interrupted_replacement_is_retried_not_flagged(self):
        self.store.start_pass("2000-01-01T00:00:00+00:00")
        self.deliver_lossy()
        self.store.reopen_for_replacement("mix:k")
        library = self.fake_library(["/test/A - Song (Original Mix) [X].mp3"])
        self.assertEqual(pipeline.decide(self.store, library, "k", self.offer("wav"))[0], "replace")

    def test_old_lossy_file_and_note_are_removed_only_after_the_new_one_verifies(self):
        self.store.start_pass("2000-01-01T00:00:00+00:00")
        self.deliver_lossy(path="/hold/A - Song (Original Mix) [X].mp3")
        self.store.reopen_for_replacement("mix:k")
        client = mock.Mock()
        client.files_get_metadata.return_value = mock.Mock(content_hash="old")
        pipeline.finish_replacements(self.store, client)
        client.files_delete_v2.assert_not_called()
        self.store.asset_update("mix:k", "delivery_verified", dropbox_path="/test/A - Song (Original Mix) [X].aiff")
        pipeline.finish_replacements(self.store, client)
        deleted = [c.args[0] for c in client.files_delete_v2.call_args_list]
        self.assertEqual(deleted, ["/hold/A - Song (Original Mix) [X].mp3", "/hold/A - Song (Original Mix) [X].txt"])
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM replacements").fetchone())

    def test_changed_old_file_is_left_in_place(self):
        self.store.start_pass("2000-01-01T00:00:00+00:00")
        self.deliver_lossy()
        self.store.reopen_for_replacement("mix:k")
        self.store.asset_update("mix:k", "delivery_verified", dropbox_path="/test/new.aiff")
        client = mock.Mock()
        client.files_get_metadata.return_value = mock.Mock(content_hash="someone edited it")
        pipeline.finish_replacements(self.store, client)
        client.files_delete_v2.assert_not_called()

    def test_claimed_asset_is_uncertain_not_retried(self):
        self.store.claim("mix:k", None, "T", "wav")
        self.assertEqual(pipeline.decide(self.store, self.fake_library([]), "k", self.offer())[0], "uncertain")

    def test_downloaded_asset_resumes_and_delivered_asset_skips(self):
        self.store.claim("mix:k", None, "T", "wav")
        local = Path(self.tmp.name) / "raw.wav"
        local.write_bytes(b"verified download")
        self.store.asset_update("mix:k", "downloaded", local_path=str(local))
        self.assertEqual(pipeline.decide(self.store, self.fake_library([]), "k", self.offer())[0], "resume")
        self.store.asset_update("mix:k", "delivery_verified", dropbox_path="/x")
        self.assertEqual(pipeline.decide(self.store, self.fake_library([]), "k", self.offer())[0], "skip")

    def test_missing_local_download_blocks_a_blind_resume(self):
        self.store.claim("mix:k", None, "T", "wav")
        self.store.asset_update("mix:k", "downloaded", local_path=str(Path(self.tmp.name) / "missing.wav"))
        self.assertEqual(pipeline.decide(self.store, self.fake_library([]), "k", self.offer())[0], "uncertain")

    def test_live_worker_blocks_a_second_worker_from_resuming_download(self):
        local = Path(self.tmp.name) / "raw.wav"
        local.write_bytes(b"data")
        self.store.claim("mix:k", None, "T", "wav")
        self.store.asset_update("mix:k", "downloaded", local_path=str(local))
        self.store.db.execute("UPDATE claims SET owner='99999999'")
        self.store.db.commit()
        with mock.patch.object(pipeline, "process_alive", return_value=True):
            self.assertEqual(pipeline.decide(self.store, self.fake_library([]), "k", self.offer())[0], "uncertain")

    def test_release_claim_reopens_uncertain_but_never_a_delivered_asset(self):
        self.store.claim("mix:k", None, "T", "wav")
        self.store.release_claim("mix:k", "checked the platform")
        self.assertTrue(self.store.claim("mix:k", None, "T", "wav"))
        self.store.asset_update("mix:k", "delivery_verified")
        with self.assertRaises(ValueError):
            self.store.release_claim("mix:k", "nope")


class FakeAdapter:
    """A platform double that serves a generated WAV and counts transfers."""

    def __init__(self, gated=False, names=("Test Artist - Test Song (Original Mix)",)):
        self.gated, self.names = gated, names
        self.downloads, self.unlocks, self.unlocked = 0, 0, False

    def release(self, source_id):
        return {"title": "Test Song", "label": "Test Label", "gated": self.gated, "feedback_left": self.gated,
                "tracks": [{"name": n, "artist": "Test Artist", "title": "Test Song", "mix": "Original Mix", "duration": 6,
                            "pack_id": 1, "files": [{"id": "f1", "type": "Wav", "size_kb": 1}]} for n in self.names]}

    @staticmethod
    def extension(file):
        return ".wav"

    def unlock(self, source_id, release):
        # Real adapters record a submitted gate and return early on a second call.
        self.unlocks += not self.unlocked
        self.unlocked = True

    def download(self, source_id, file, destination):
        self.downloads += 1
        make_wav(destination)
        return Path(destination).stat().st_size, "digest"


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.sync_logs = lambda: None
        self.source = self.store.add_source("promo-cloud", "Test Label [TL001] : Test Artist - Test Song",
                                            "https://www.promo-cloud.com/TargetedPromo/view?promo=pp&contact=cc")
        self.staging = Path(self.tmp.name) / "staging"
        self.held_notes = []
        self.exclusions = Path(self.tmp.name) / "exclusions.json"
        patches = [mock.patch.object(pipeline, "STAGING", self.staging),
                   mock.patch.object(pipeline, "EXCLUSIONS", self.exclusions),
                   mock.patch("promo_remote.write_note", lambda client, path, problems: self.held_notes.append((path, problems))),
                   mock.patch.object(pipeline, "dropbox_skill", lambda: (None, None)),
                   mock.patch.object(pipeline, "deliver_file", self.fake_deliver),
                   mock.patch.object(pipeline, "validate_audio", self.fake_validate)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.delivered = []
        self.library = pipeline.LibraryIndex.__new__(pipeline.LibraryIndex)
        self.library.by_key, self.library.by_loose = {}, {}

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def fake_deliver(self, store, shared, client, local, asset_key, folder=pipeline.DEST, stage="delivery_verified"):
        if stage == "delivery_verified":
            self.delivered.append(Path(local).name)
        store.asset_update(asset_key, stage, dropbox_path=folder + "/" + Path(local).name)
        return folder + "/" + Path(local).name

    @staticmethod
    def fake_validate(path, expected=None, tolerance=3.0):
        return pipeline.probe(path)

    def run_collect(self, adapter, **kw):
        return pipeline.collect_release(self.store, adapter, self.source, self.library, **kw)

    def test_dry_run_moves_nothing_and_claims_nothing(self):
        adapter = FakeAdapter()
        summary = self.run_collect(adapter, dry_run=True)
        self.assertEqual(adapter.downloads, 0)
        self.assertEqual(summary["tracks"][0]["action"], "download")
        self.assertIsNone(self.store.asset("mix:" + pipeline.mix_key("Test Artist - Test Song (Original Mix)")))

    def test_exclude_list_skips_before_download_and_feedback(self):
        adapter = FakeAdapter(gated=True)
        for rules, reason in (({"labels": ["TEST label"]}, "excluded label"), ({"artists": ["test artist"]}, "excluded artist"),
                              ({"title_words": ["original"]}, "excluded title word")):
            self.exclusions.write_text(json.dumps(rules), encoding="utf-8")
            summary = self.run_collect(adapter)
            self.assertEqual(summary["tracks"][0]["action"], "excluded")
            self.assertIn(reason, summary["tracks"][0]["reason"])
        self.assertEqual((adapter.downloads, adapter.unlocks), (0, 0))
        self.exclusions.write_text(json.dumps({"labels": [], "artists": [], "title_words": ["orig"]}), encoding="utf-8")
        self.assertEqual(self.run_collect(adapter, dry_run=True)["tracks"][0]["action"], "download", "whole words only")

    def test_label_known_only_from_the_file_is_excluded_before_processing_and_not_fetched_again(self):
        class TaggedAdapter(FakeAdapter):
            def resolve_metadata(self, source_id, track, path):
                track["label"] = "Late Label"
        adapter = TaggedAdapter()
        csv_path = Path(self.tmp.name) / "exclusions.csv"
        csv_path.write_text("type;value;note\nlabel;Late Label;seen in tags\n", encoding="utf-8")
        with mock.patch.object(pipeline, "EXCLUSIONS_CACHE", csv_path):
            summary = self.run_collect(adapter)
            self.assertEqual(summary["tracks"][0]["action"], "excluded")
            self.assertEqual((adapter.downloads, self.delivered), (1, []))
            key = "mix:" + pipeline.mix_key("Test Artist - Test Song (Original Mix)")
            self.assertEqual(self.store.asset(key)["stage"], "excluded_label")
            again = self.run_collect(adapter)
            self.assertEqual((again["tracks"][0]["action"], adapter.downloads), ("excluded", 1))
            csv_path.write_text("type,value,note\n", encoding="utf-8")
            self.assertEqual(self.run_collect(adapter, dry_run=True)["tracks"][0]["action"], "download",
                             "taking the label off the list lets the mix through again")

    def test_full_run_processes_to_aiff_with_precision_kept_and_second_run_transfers_nothing(self):
        adapter = FakeAdapter()
        self.run_collect(adapter)
        self.assertEqual(adapter.downloads, 1)
        self.assertEqual(len(self.delivered), 1)
        self.assertTrue(self.delivered[0].lower().endswith(".aiff"), self.delivered)
        row = self.store.asset("mix:" + pipeline.mix_key("Test Artist - Test Song (Original Mix)"))
        self.assertEqual(row["stage"], "delivery_verified")
        self.assertFalse((self.staging / self.source).exists(), "staging must be cleaned up")
        # Reminder email / second source for the same mix: no new transfer.
        again = self.run_collect(adapter)
        self.assertEqual(adapter.downloads, 1)
        self.assertEqual(again["tracks"][0]["action"], "skip")

    def test_same_pass_lossless_offer_replaces_the_delivered_mp3(self):
        self.store.start_pass("2000-01-01T00:00:00+00:00")
        key = "mix:" + pipeline.mix_key("Test Artist - Test Song (Original Mix)")
        self.store.claim(key, self.source, "Test Artist - Test Song (Original Mix)", "mp3")
        self.store.asset_update(key, "delivery_verified", dropbox_path=pipeline.DEST + "/old.mp3", content_hash="old")
        adapter = FakeAdapter()
        with mock.patch.object(pipeline, "finish_replacements") as finish:
            summary = self.run_collect(adapter)
        self.assertEqual(summary["tracks"][0]["action"], "replace")
        self.assertEqual(adapter.downloads, 1)
        self.assertTrue(self.delivered[0].lower().endswith(".aiff"))
        finish.assert_called_once()
        self.assertEqual(self.store.db.execute("SELECT old_path FROM replacements").fetchone()[0], pipeline.DEST + "/old.mp3")
        self.assertEqual(self.run_collect(adapter)["tracks"][0]["action"], "skip", "the lossless copy is final")

    def test_preview_clip_is_refused_before_processing(self):
        adapter = FakeAdapter()
        clip = lambda path, expected=None, tolerance=3.0: {**pipeline.probe(path), "duration": 15.2}
        with mock.patch.object(pipeline, "validate_audio", clip), mock.patch.object(pipeline, "PREVIEW_SECONDS", 60),                 self.assertRaisesRegex(RuntimeError, "preview clip"):
            self.run_collect(adapter)
        self.assertEqual(self.delivered, [])

    def test_processing_failure_keeps_nothing_local_and_retry_downloads_again(self):
        adapter = FakeAdapter()
        with mock.patch.object(pipeline, "run_processor", side_effect=RuntimeError("processor crashed")):
            with self.assertRaises(RuntimeError):
                self.run_collect(adapter)
        self.assertFalse((self.staging / self.source).exists(), "zero local retention")
        self.assertEqual(self.store.asset("mix:" + pipeline.mix_key("Test Artist - Test Song (Original Mix)"))["stage"], "planned")
        self.run_collect(adapter)
        self.assertEqual(adapter.downloads, 2)
        self.assertEqual(len(self.delivered), 1)
        self.assertFalse((self.staging / self.source).exists())

    def test_upload_failure_keeps_nothing_local_and_retry_gives_feedback_once(self):
        adapter = FakeAdapter(gated=True)
        with mock.patch.object(pipeline, "deliver_file", side_effect=RuntimeError("upload interrupted")):
            with self.assertRaises(RuntimeError):
                self.run_collect(adapter, allow_gate=True)
        self.assertFalse((self.staging / self.source).exists(), "zero local retention")
        self.run_collect(adapter, allow_gate=True)
        self.assertEqual((adapter.downloads, adapter.unlocks), (2, 1))
        self.assertEqual(len(self.delivered), 1)

    def test_artist_that_disagrees_with_the_platform_is_held_not_delivered(self):
        adapter = FakeAdapter()
        real = adapter.release
        def wrong_artist(source_id):
            release = real(source_id)
            release["tracks"][0]["artist"] = "Somebody Else"
            return release
        adapter.release = wrong_artist
        summary = self.run_collect(adapter)
        self.assertEqual(self.delivered, [])
        self.assertEqual(summary["tracks"][-1]["action"], "held_for_research")
        self.assertFalse((self.staging / self.source).exists(), "held files wait in Dropbox, not locally")
        row = self.store.asset("mix:" + pipeline.mix_key("Test Artist - Test Song (Original Mix)"))
        self.assertEqual(row["stage"], "held_for_research")
        self.assertTrue(row["dropbox_path"].startswith(pipeline.HOLD + "/"))
        self.assertEqual(self.held_notes[0][0], row["dropbox_path"])
        again = self.run_collect(adapter)
        self.assertEqual(adapter.downloads, 1)
        self.assertEqual(again["tracks"][0]["action"], "skip")

    def test_gated_release_needs_explicit_permission_and_unlocks_once(self):
        adapter = FakeAdapter(gated=True)
        with self.assertRaises(pipeline.GateRequired):
            self.run_collect(adapter)
        self.assertEqual((adapter.downloads, adapter.unlocks), (0, 0))
        self.run_collect(adapter, allow_gate=True)
        self.assertEqual((adapter.downloads, adapter.unlocks), (1, 1))

    def test_failed_download_leaves_an_uncertain_claim_that_blocks_blind_retry(self):
        adapter = FakeAdapter()
        adapter.download = mock.Mock(side_effect=RuntimeError("network dropped"))
        with self.assertRaises(RuntimeError):
            self.run_collect(adapter)
        key = "mix:" + pipeline.mix_key("Test Artist - Test Song (Original Mix)")
        self.assertEqual(self.store.asset(key)["stage"], "claimed")
        again = self.run_collect(FakeAdapter())
        self.assertEqual(again["tracks"][0]["action"], "uncertain")
        self.assertEqual(adapter.download.call_count, 1)
        # Deliberate release makes it eligible again.
        self.store.release_claim(key, "network failure confirmed, nothing counted")
        fresh = FakeAdapter()
        self.run_collect(fresh)
        self.assertEqual(fresh.downloads, 1)


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        import promo_cloud
        self.module = promo_cloud
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.sync_logs = lambda: None
        self.adapter = promo_cloud.PromoCloud.__new__(promo_cloud.PromoCloud)
        self.adapter.store, self.adapter.calls = self.store, 0
        url = "https://www.promo-cloud.com/TargetedPromo/view?promo=aaaa-1111&contact=bbbb-2222"
        self.source = self.store.add_source("promo-cloud", "Test Label [TL001] : Artist - Title", url)
        self.release = {"title": "Title", "label": "Test Label", "tracks": [{"pack_id": 77}]}

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    @staticmethod
    def reply(code=1):
        response = mock.Mock()
        response.json.return_value = {"responseCode": code, "responseString": "token"}
        return response

    def test_comment_and_rating_are_chosen_once_and_stable(self):
        first = self.adapter.choose_feedback(self.source, self.release)
        second = self.adapter.choose_feedback(self.source, self.release)
        self.assertEqual((first["rating"], first["comment"]), (second["rating"], second["comment"]))
        low, high = self.module.RATING_RANGE
        self.assertTrue(low <= first["rating"] <= high)
        self.assertFalse(first["supporting"])
        lowered = first["comment"].lower()
        for banned in ["listened", "played", "chart", "supporting", "rotation", "—"]:
            self.assertNotIn(banned, lowered)

    def test_feedback_is_submitted_exactly_once_even_if_asked_twice(self):
        self.adapter.request = mock.Mock(return_value=self.reply())
        self.adapter.unlock(self.source, self.release)
        self.adapter.unlock(self.source, self.release)
        self.assertEqual(self.adapter.request.call_count, 1)
        payload = json.loads(self.adapter.request.call_args.kwargs["data"])
        self.assertIs(payload["_Supporting"], False)
        self.assertEqual(payload["_FavoriteTrackInstanceID"], 77)
        self.assertEqual(payload["_PromotionGuid"], "aaaa-1111")

    def test_unknown_outcome_blocks_resubmission(self):
        self.adapter.request = mock.Mock(side_effect=RuntimeError("Platform request failed: Timeout"))
        with self.assertRaises(RuntimeError):
            self.adapter.unlock(self.source, self.release)
        self.adapter.request = mock.Mock(return_value=self.reply())
        with self.assertRaises(RuntimeError):
            self.adapter.unlock(self.source, self.release)
        self.adapter.request.assert_not_called()

    def test_conflict_response_counts_as_already_submitted(self):
        self.adapter.request = mock.Mock(side_effect=RuntimeError("Platform HTTP 409 at /api2/TargetedPromotion/CollectFeedback"))
        self.adapter.unlock(self.source, self.release)
        status = self.store.db.execute("SELECT status FROM feedback WHERE source_id=?", (self.source,)).fetchone()[0]
        self.assertEqual(status, "submitted")

    def test_other_releases_get_different_wording_pool_draws(self):
        wordings = set()
        for index in range(12):
            url = f"https://www.promo-cloud.com/TargetedPromo/view?promo=p{index}&contact=c{index}"
            sid = self.store.add_source("promo-cloud", f"L [C{index}] : A - T{index}", url)
            wordings.add(self.adapter.choose_feedback(sid, {"title": f"T{index}", "label": "L", "tracks": []})["comment"])
        self.assertGreater(len(wordings), 3)


class InflyteNameTests(unittest.TestCase):
    def test_repeated_artist_is_removed_from_the_title(self):
        import inflyte
        self.assertEqual(inflyte.split_track("Robbie Seed", "Robbie Seed - Home Is You (Extended Mix)"),
                         ("Robbie Seed", "Home Is You (Extended Mix)"))

    def test_remixers_listed_as_artists_give_way_to_the_named_artist(self):
        import inflyte
        self.assertEqual(inflyte.split_track("Black Box, Gareden, ARISEY, Taly Shum",
                                             "Black Box & Gareden - Tulum Nights (ARISEY & Taly Shum Remix)"),
                         ("Black Box & Gareden", "Tulum Nights (ARISEY & Taly Shum Remix)"))

    def test_dash_inside_the_mix_and_quotes_leave_the_artist_alone(self):
        import inflyte
        self.assertEqual(inflyte.split_track("Sir Ivan", '"Love Is The Piece" (Cheat Codes - Extended Mix)'),
                         ("Sir Ivan", "Love Is The Piece (Cheat Codes - Extended Mix)"))

    def test_bracketed_label_beats_the_sender_name(self):
        import inflyte
        self.assertEqual(inflyte.release_label({"name": "A - B [Gotta Move Records]", "sender_label": "GOTTA MOVE"}),
                         "Gotta Move Records")
        self.assertEqual(inflyte.release_label({"name": "A - B", "sender_label": "Big Bells Records\t"}), "Big Bells Records")


class AdvertisedLengthTests(unittest.TestCase):
    def test_minutes_string_is_compared_as_seconds(self):
        with mock.patch.object(pipeline, "probe", return_value={"duration": 397.2}),                 mock.patch.object(pipeline, "decode_check"):
            pipeline.validate_audio("x.wav", "06:37")
            with self.assertRaises(RuntimeError):
                pipeline.validate_audio("x.wav", "05:37")


class DjEditRuleTests(unittest.TestCase):
    """DJ Edits and any edit over five minutes are DJ tools; only real radio edits drop."""
    def kept(self, tracks):
        return {n for n, (wanted, _) in pipeline.select_versions(tracks).items() if wanted}

    def test_long_radio_named_edit_is_kept(self):
        b = "Factor B, Cat Martin - Crashing Over "
        tracks = [{"name": b + "(Lost Minds Extended Remix)", "duration": "07:41"}, {"name": b + "(Lost Minds Remix)"},
                  {"name": b + "(Lost Minds Shorter DJ Radio Edit)", "duration": "06:17"}]
        self.assertEqual(self.kept(tracks), {b + "(Lost Minds Extended Remix)", b + "(Lost Minds Shorter DJ Radio Edit)"})

    def test_dj_edit_kept_real_radio_edit_dropped(self):
        tracks = [{"name": "A - B (Extended Mix)"}, {"name": "A - B (Short DJ Edit)"},
                  {"name": "A - B (Short DJ Radio Edit)", "duration": 200}]
        self.assertEqual(self.kept(tracks), {"A - B (Extended Mix)", "A - B (Short DJ Edit)"})

    def test_shorter_joins_its_remix_group_and_dj_remixers_survive(self):
        self.assertEqual(pipeline.version_group("A - B (X Shorter Radio Edit)"), pipeline.version_group("A - B (X Extended Remix)"))
        self.assertNotEqual(pipeline.version_group("A - B (DJ Spaceman Remix)"), pipeline.version_group("A - B (Remix)"))
        self.assertEqual(pipeline.seconds("1:02:03"), 3723.0)


if __name__ == "__main__":
    unittest.main()


class OwnedVersionTests(unittest.TestCase):
    """Radio edits, Original aliases and spelling variants of owned tracks reached the test folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def library(self, paths):
        index = pipeline.LibraryIndex.__new__(pipeline.LibraryIndex)
        index.by_key, index.by_loose, index.by_title = {}, {}, {}
        index.versions = pipeline.VersionIndex(paths)
        return index

    def decide(self, name, paths, seconds=200, kind="Wav"):
        offer = {"name": name, "duration": seconds, "best": {"type": kind}}
        return pipeline.decide(self.store, self.library(paths), pipeline.mix_key(name), offer)[0]

    def test_every_trailing_bracket_counts(self):
        self.assertEqual(pipeline.version_group("Bonafique - Monologue (JØRD Remix) (Edit)"),
                         pipeline.version_group("Bonafique - Monologue (JØRD Extended Remix)"))
        self.assertNotEqual(pipeline.version_group("Bonafique - Monologue (JØRD Remix) (Edit)"),
                            pipeline.version_group("Bonafique - Monologue (Kashovski Remix) (Edit)"))
        self.assertEqual(pipeline.version_kind("RMC One - My Whole Life (Radio Mix) (1) [RV Records].mp3"), "short")
        tracks = [offer("Bonafique - Monologue (JØRD Extended Remix)", 281), offer("Bonafique - Monologue (JØRD Remix) (Edit)", 190)]
        picked = {n for n, (w, _) in pipeline.select_versions(tracks).items() if w}
        self.assertEqual(picked, {"Bonafique - Monologue (JØRD Extended Remix)"})

    def test_radio_edit_of_owned_extended_is_skipped(self):
        owned = ["/music library/09/scott mac - released (extended mix) [high voltage recordings].mp3",
                 "/music library/09/blr ft. lou - move it on [am extended mix] [blooom].aif",
                 "/music library/09/masaru hinaiji - shine (pink pig extended remix) [your melodies].mp3"]
        self.assertEqual(self.decide("Scott Mac - Released (Radio Edit)", owned), "skip")
        self.assertEqual(self.decide("BLR feat. Lou - Move It On (AM Radio Mix)", owned), "skip")
        self.assertEqual(self.decide("Masaru Hinaiji - Shine (Pink Pig Radio Edit Remix)", owned), "skip")
        self.assertEqual(self.decide("Masaru Hinaiji - Shine (Other Remix) (Radio Edit)", owned), "download")

    def test_long_edit_stays_beside_owned_extended(self):
        owned = ["/music library/09/kris-jay - aftermath (extended mix) [stratum records].aiff"]
        self.assertEqual(self.decide("Kris-Jay - Aftermath (Radio Edit)", owned, seconds=329), "download")

    def test_original_or_unmarked_beside_owned_extended_is_skipped(self):
        owned = ["/music library/09/andrea lovely - beyond (extended mix) [x].aiff"]
        self.assertEqual(self.decide("Andrea Lovely - Beyond (Original Mix)", owned), "skip")
        self.assertEqual(self.decide("Andrea Lovely - Beyond", owned), "skip")

    def test_extended_beside_owned_original_is_asked_not_delivered(self):
        earlier = ["/music library/09/sikoti - fall back into you (original mix) [x].aiff"]
        self.assertEqual(self.decide("Sikoti - Fall Back Into You (Extended Mix)", earlier), "upgrade_candidate")
        this_pass = [pipeline.DEST.lower() + "/sikoti - fall back into you (original mix) [x].aiff"]
        self.assertEqual(self.decide("Sikoti - Fall Back Into You (Extended Mix)", this_pass), "download")

    def test_spelling_variants_of_owned_tracks_are_skipped(self):
        owned = ["/music library/09/22bullets feat. anticalm - my motto (extended mix) [x].aiff",
                 "/music library/09/akona, nick slater - bleed the 303 (extended mix) [x].aiff",
                 "/music library/09/alan morris, maria nayler - under winters hold (extended mix) [x].aiff",
                 "/music library/09/ferry corsten & eddo brandwijk - oh how i miss you (extended mix) [x].aiff"]
        self.assertEqual(self.decide("22Bullets - My Motto (feat. Anticalm) (Extended Mix)", owned), "skip")
        self.assertEqual(self.decide("Akona - Bleed The 303 (Extended Mix)", owned), "skip")
        self.assertEqual(self.decide("Alan Morris & Maria Nayler - Under Winter's Hold (Extended Mix)", owned), "skip")
        self.assertEqual(self.decide("Eddo Brandwijk & Ferry Corsten - Oh How I Miss You (Extended Mix)", owned), "skip")
        self.assertEqual(self.decide("Akona - Bleed The 303 (Instrumental Extended Mix)", owned), "download")

    def test_lossless_offer_of_lossy_spelling_variant_stays_an_upgrade_question(self):
        owned = ["/music library/09/akona, nick slater - bleed the 303 (extended mix) [x].mp3"]
        self.assertEqual(self.decide("Akona - Bleed The 303 (Extended Mix)", owned), "upgrade_candidate")
        self.assertEqual(self.decide("Akona - Bleed The 303 (Extended Mix)", owned, kind="mp3"), "skip")

    def test_audit_flags_shorter_and_owned_versions(self):
        from promo_final_audit import resolution_and_version_issues
        from unittest import mock
        store = mock.Mock(root=Path(self.tmp.name))
        tests = ["Bonafique - Monologue (JØRD Extended Remix) [Get Physical Music].aiff",
                 "Bonafique - Monologue (JØRD Remix) (Edit) [Get Physical Music].aiff",
                 "Rony Rex feat. Coco K - Light All Night (Extended) [Rony Rex].aiff",
                 "Rony Rex, Coco K - Light All Night [Too Lost].aiff"]
        self.assertTrue(resolution_and_version_issues(store, tests[1], tests, 190))
        self.assertTrue(resolution_and_version_issues(store, tests[3], tests, 181))
        self.assertFalse(resolution_and_version_issues(store, tests[0], tests, 281))
        owned = ["/music library/09/andrea lovely - beyond (extended mix) [x].aiff"]
        self.assertTrue(resolution_and_version_issues(store, "Andrea Lovely - Beyond (Original Mix) [X].aiff", [], 300, owned))


class ExclusionCsvTests(unittest.TestCase):
    def test_excel_semicolons_bom_comments_and_self_released_label_guard(self):
        data = ("﻿type;value;note\n# a comment;x;\nlabel;Vorwärts Musik;\nLabel;Self Released;oops\n"
                "artist;Cajmere;self released\ntitle word;Bootleg;\nunknown;thing;\nlabel;;\n").encode("utf-8")
        rules = pipeline.parse_exclusions_csv(data)
        self.assertEqual(rules, {"labels": ["vorwärts musik"], "artists": ["cajmere"], "title_words": ["bootleg"]})
        self.assertEqual(pipeline.parse_exclusions_csv("type,value\nlabel,Caf\xe9 Records\n".encode("cp1252"))["labels"],
                         ["café records"])
        track = {"name": "Cajmere - Brighter Days (CASHEW Remix)", "label": "Self Released"}
        self.assertIn("excluded artist", pipeline.excluded(track, {}, rules))
        self.assertIsNone(pipeline.excluded({"name": "Other - Song", "label": "Self Released"}, {}, rules))

    def test_remote_list_is_cached_and_a_dropbox_failure_keeps_the_last_good_copy(self):
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root) / "exclusions.csv"
            calls = []
            def download(path):
                calls.append(path)
                if len(calls) > 1:
                    raise OSError("offline")
                return None, mock.Mock(content=b"type,value\nlabel,Gone Label\n")
            client = mock.Mock(files_download=download)
            with mock.patch.object(pipeline, "EXCLUSIONS_REMOTE", "/x/exclusions.csv"), \
                    mock.patch.object(pipeline, "EXCLUSIONS_CACHE", cache), \
                    mock.patch.object(pipeline, "dropbox_skill", return_value=(None, client)), \
                    mock.patch.dict(pipeline._EXCLUSIONS_LOADED, clear=True):
                self.assertEqual(pipeline.load_exclusions()["labels"], ["gone label"])
                self.assertEqual(pipeline.load_exclusions()["labels"], ["gone label"])
                self.assertEqual(len(calls), 1, "read once per TTL")
                pipeline._EXCLUSIONS_LOADED.clear()
                self.assertEqual(pipeline.load_exclusions()["labels"], ["gone label"], "offline uses the saved copy")
