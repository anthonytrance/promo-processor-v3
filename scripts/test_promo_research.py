import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import promo_metadata as metadata
import promo_pipeline as pipeline
import promo_research as research
from process_promos import read_existing_tags, write_tags
from test_promo_pipeline import make_wav


def bp(name, mix, artists, label, isrc="", catalog="LKR117", track_id=1):
    return {"track_id": track_id, "track_name": name, "mix_name": mix, "isrc": isrc, "catalog_number": catalog,
            "artists": [{"artist_name": a} for a in artists], "label": {"label_name": label}}


class MailParsingTests(unittest.TestCase):
    def test_label_catalog_isrc_and_per_track_lines(self):
        found = research.parse_mail("Label: XWaveZ\nCatalog Num: XWZ 012\nISRC: DE-H74-26-22010\n"
                                    "MELODY194 Nicola Maddaloni - I Miss You (Extended Mix)(Your Melodies)\n"
                                    "Released through Patronus Records on Friday")
        self.assertEqual(found["labels"], ["XWaveZ", "Patronus Records"])
        self.assertEqual(found["catalogs"], ["XWZ012"])
        self.assertEqual(found["isrcs"], ["DEH742622010"])
        self.assertEqual(found["track_labels"], [("Nicola Maddaloni - I Miss You (Extended Mix)", "Your Melodies")])

    def test_subject_bracket_names_the_label_but_catalog_brackets_do_not(self):
        self.assertEqual(research.parse_mail("Subject: Dan Stone feat. MOONE - Cold [FSOE]")["labels"], ["FSOE"])
        self.assertEqual(research.parse_mail("Subject: A - B [ABC001]")["labels"], [])

    def test_contained_label_names_agree(self):
        hits = [bp("Borrowed Time", "Extended Mix", ["Krevix"], "High Voltage Recordings")]
        resolution, _ = research.research({"artist": "Krevix", "title": "Borrowed Time (Extended Mix)", "tag_label": "HIGH VOLTAGE",
                                           "original_names": []}, search=lambda q: hits, codes={})
        self.assertEqual(resolution["label"], "High Voltage Recordings")

    def test_mix_parenthetical_is_not_a_label(self):
        self.assertEqual(research.parse_mail("A - Song (Extended Mix)")["track_labels"], [])

    def test_per_track_line_beats_release_wide_sender_line(self):
        found = research.parse_mail("Label: High Emotions Music Group\n"
                                    "END194 Andre Wildenhues - Through the Clouds (Extended Mix)(Endlessky Uplifting)")
        self.assertEqual(research.mail_label(found, "Andre Wildenhues", "Through the Clouds (Extended Mix)")[0],
                         "Endlessky Uplifting")
        self.assertEqual(research.mail_label(found, "Other", "Different (Extended Mix)"), (None, None))

    def test_sender_label_lines_are_not_evidence(self):
        found = research.parse_mail("Label: Reaktion DJ Promo")
        self.assertEqual(research.mail_label(found, "A", "Song"), (None, None))


class BeatportTests(unittest.TestCase):
    def test_same_artists_title_and_mix_wins(self):
        hits = [bp("Autumn Evening", "Original Mix", ["DJ Dani"], "Other", track_id=1),
                bp("Autumn Evening", "Extended Mix", ["DJ Dani"], "Lost Kommunication Records Ltd", track_id=2)]
        hit = research.beatport_match("DJ Dani", "Autumn Evening (Extended Mix)", search=lambda q: hits)
        self.assertEqual(hit["label"], "Lost Kommunication Records Ltd")
        self.assertTrue(hit["url"].startswith("https://www.beatport.com/track/autumn-evening/2"))

    def test_featured_artist_in_beatport_title_still_matches(self):
        hits = [bp("So Good (feat. Kuuda)", "Francis Mercier Extended Remix",
                   ["Josh Gigante", "Kuuda", "CAMELPHAT", "Francis Mercier"], "When Stars Align")]
        hit = research.beatport_match("CamelPhat, Josh Gigante feat. Kuuda", "So Good (Francis Mercier Extended Remix)",
                                      search=lambda q: hits)
        self.assertEqual(hit["label"], "When Stars Align")

    def test_version_inside_beatport_track_name_still_matches(self):
        hits = [bp("Traka Taka (Extended Remix)", "Original Mix", ["Rauschhofer"], "DistroKid")]
        hit = research.beatport_match("Rauschhofer", "TRAKA TAKA (Extended Mix)", search=lambda q: hits)
        self.assertEqual(hit["label"], "DistroKid")
        remix = [bp("Traka Taka (Somebody Remix)", "Original Mix", ["Rauschhofer"], "Other")]
        self.assertIsNone(research.beatport_match("Rauschhofer", "TRAKA TAKA (Extended Mix)", search=lambda q: remix))

    def test_different_artist_is_never_a_match(self):
        hits = [bp("Autumn Evening", "Extended Mix", ["Somebody Else"], "Wrong")]
        self.assertIsNone(research.beatport_match("DJ Dani", "Autumn Evening (Extended Mix)", search=lambda q: hits))

    def test_named_remix_never_takes_another_versions_label(self):
        hits = [bp("Strings Of Life / Un Amore Supremo", "Original Mix", ["Soul Central"], "CENTRL Music", track_id=1),
                bp("Strings of Life / Un Amore Supremo", "Bart Skils & Weska Remix", ["Soul Central", "Bart Skils", "Weska"],
                   "Drumcode", track_id=2)]
        title = "Strings Of Life - Un Amore Supremo (Bart Skils x Waska Remix)"
        self.assertEqual(research.beatport_match("Soul Central", title, search=lambda q: hits)["label"], "Drumcode")
        self.assertIsNone(research.beatport_match("Soul Central", title, search=lambda q: hits[:1]))

    def test_isrc_proves_identity_despite_spelling(self):
        hits = [bp("Sensory Overload", "Extended", ["zukrassverliebt"], "Real Label", isrc="DEZC62698400")]
        hit = research.beatport_match("Zu Krass Verliebt", "Sensory Overload", "DEZC62698400", search=lambda q: hits)
        self.assertEqual(hit["evidence"], "same ISRC")


class ResearchTests(unittest.TestCase):
    item = {"artist": "DJ Dani", "title": "Autumn Evening (Extended Mix)", "source_id": None, "original_names": []}

    def test_catalogue_spelling_wins_when_beatport_agrees(self):
        hits = [bp("Autumn Evening", "Extended Mix", ["DJ Dani"], "Lost Kommunication Records Ltd")]
        resolution, outcome = research.research(dict(self.item, catalog="LKR117"), search=lambda q: hits,
                                                codes={"LKR": "Lost Kommunication"})
        self.assertEqual(outcome["state"], "resolved")
        self.assertEqual(resolution["label"], "Lost Kommunication")

    def test_beatport_label_overrules_other_sources(self):
        hits = [bp("Autumn Evening", "Extended Mix", ["DJ Dani"], "MixCult Digital")]
        resolution, outcome = research.research(dict(self.item, tag_label="MixCult Records", catalog="MCR001"),
                                                search=lambda q: hits, codes={"MCR": "MixCult Records"})
        self.assertEqual(outcome["state"], "resolved")
        self.assertEqual(resolution["label"], "MixCult Digital")
        self.assertIn("overrules", outcome["evidence"][-1]["why"])

    def test_conflicting_labels_without_beatport_go_to_research_leftovers(self):
        resolution, outcome = research.research(dict(self.item, tag_label="Label B", catalog="LA001"),
                                                search=lambda q: [], codes={"LA": "Label A"})
        self.assertIsNone(resolution)
        self.assertEqual(outcome["state"], "conflict")

    def test_no_evidence_is_unresolved_not_guessed(self):
        resolution, outcome = research.research(dict(self.item, tag_label="Spin Me PR"), search=lambda q: [], codes={})
        self.assertIsNone(resolution)
        self.assertEqual(outcome["state"], "unresolved")

    def test_mail_evidence_is_an_accepted_resolution_source(self):
        with tempfile.TemporaryDirectory() as root:
            entry = {"artist": "A", "title": "Song (Extended Mix)", "label": "Real", "original_names": ["A - Song"],
                     "approved_at": "now", "sources": ["mail:abc@example.com"]}
            research.save_resolutions([entry], Path(root))
            self.assertEqual(metadata.find_resolution("A - Song (Extended Mix) [Spin Me PR].aiff", Path(root))["label"], "Real")
            entry["sources"] = ["http://insecure.example"]
            research.save_resolutions([entry], Path(root))
            self.assertIsNone(metadata.find_resolution("A - Song (Extended Mix)", Path(root)))


class DistributorTests(unittest.TestCase):
    item = {"artist": "MikeR", "title": "Wherever You Go (Extended Mix)", "source_id": None, "original_names": []}

    def test_distributor_only_store_label_files_as_self_released(self):
        hits = [bp("Wherever You Go", "Extended Mix", ["MikeR"], "LANDR, Self-Released")]
        resolution, _ = research.research(self.item, search=lambda q: hits, codes={})
        self.assertEqual(resolution["label"], "Self Released")
        self.assertFalse(metadata.label_issues("Self Released"))
        self.assertTrue(metadata.label_issues("DistroKid"))

    def test_sender_imprint_beats_a_distributor(self):
        hits = [bp("Wherever You Go", "Extended Mix", ["MikeR"], "igroovemusic")]
        resolution, _ = research.research(dict(self.item, tag_label="TKBZ"), search=lambda q: hits, codes={})
        self.assertEqual(resolution["label"], "TKBZ")

    def test_company_suffix_is_dropped(self):
        self.assertEqual(research.clean_label("Lost Kommunication Records Ltd"), "Lost Kommunication Records")


class NamingTests(unittest.TestCase):
    def test_pr_senders_are_untrusted(self):
        for label in ("Like That Underground", "Hey! This is Brad", "1001Tracklists", "Reaktion DJ Promo"):
            self.assertTrue(metadata.label_issues(label))

    def test_subject_pipe_label(self):
        self.assertEqual(pipeline.label_from_subject("LTU Promo: A - Song | Real Label"), ("Real Label", None))
        self.assertEqual(pipeline.label_from_subject("Some Label [SL001] : Artist - Title"), ("Some Label", "SL001"))

    def test_studio_bounce_tails_are_stripped(self):
        from process_promos import clean_master_suffixes
        for raw, clean in (("Momento_MainMaster", "Momento"), ("Another Interlude (Radio Edit) MASTER 24 bit", "Another Interlude (Radio Edit)"),
                           ("I Am Awakened (Extended Mix) RC4 4416", "I Am Awakened (Extended Mix)"),
                           ("Long Gone feat. Erin - Extended Mix v1", "Long Gone feat. Erin (Extended Mix)"),
                           ("Life Within Us Radio Edit", "Life Within Us (Radio Edit)"), ("Song (Extended Mix)", "Song (Extended Mix)"),
                           ("See The World (Extended) FINAL", "See The World (Extended)"), ("The Final", "The Final")):
            self.assertEqual(clean_master_suffixes(raw), clean)

    def test_slash_in_label_keeps_words_apart(self):
        self.assertEqual(pipeline.safe_name("A - Song [Rimas / 240 KM/H]"), "A - Song [Rimas - 240 KM-H]")

    def test_radio_edit_beside_extended_or_full_mix_is_skipped(self):
        rows = [{"source_id": "s", "final_name": n} for n in (
            "Garrido feat. Erin - Long Gone (Extended Mix)", "Garrido feat. Erin - Long Gone (Radio Edit)",
            "Steam Lizard - Candle (Original Mix)", "Steam Lizard - Candle (Edit)", "Steam Lizard - Candle (Instrumental)",
            "Scott Mac - Released (Radio Edit)")]
        self.assertEqual(research.shorter_versions(rows), {("s", "Garrido feat. Erin - Long Gone (Radio Edit)"),
                                                           ("s", "Steam Lizard - Candle (Edit)")})


class AuditRuleTests(unittest.TestCase):
    def test_resolution_rename_and_shorter_version_are_held(self):
        from promo_final_audit import resolution_and_version_issues
        from process_promos import normalize_mix_type
        self.assertEqual(normalize_mix_type("Circling Thoughts (ExtendedMix)"), "Circling Thoughts (Extended Mix)")
        with tempfile.TemporaryDirectory() as root:
            research.save_resolutions([{"artist": "Talla 2XLC", "title": "Dream Of You (Extended Mix)", "label": "Technoclub Retro",
                                        "original_names": [], "approved_at": "now", "sources": ["mail:x"]}], Path(root))
            store = mock.Mock(root=Path(root))
            issues = resolution_and_version_issues(store, "Talla 2XLC - Dream Of You (Extended Mix) [Talla 2XLC].aiff", [])
            self.assertTrue(issues[0].startswith("Approved metadata resolution renames"))
            self.assertFalse(resolution_and_version_issues(
                store, "Talla 2XLC - Dream Of You (Extended Mix) [Technoclub Retro].aiff", []))
            # The audit cache passes lowercase index names: only a real rename may force a full recheck.
            self.assertFalse(resolution_and_version_issues(
                store, "talla 2xlc - dream of you (extended mix) [technoclub retro].aiff", [], lowercase_name=True))
            self.assertTrue(resolution_and_version_issues(
                store, "talla 2xlc - dream of you (extended mix) [talla 2xlc].aiff", [], lowercase_name=True))
            names =["semper t. - circling thoughts (extendedmix) [tranzone recordings].aiff"]
            self.assertTrue(resolution_and_version_issues(
                store, "Semper T. - Circling Thoughts (Radio Mix) [TranZone Recordings].aiff", names))
            self.assertFalse(resolution_and_version_issues(store, "Scott Mac - Released (Radio Edit) [HVR].aiff", names))


def make_aiff(folder, name, tags):
    import subprocess
    wav = Path(folder) / "x.wav"
    make_wav(wav)
    path = Path(folder) / name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-c:a", "pcm_s24be", str(path)], check=True)
    wav.unlink()
    write_tags(str(path), tags)
    return path


class FakeRemote:
    def __init__(self, path, local):
        from promo_automation import dropbox_hash
        self.path_display, self.path_lower, self.name = path, path.lower(), path.rsplit("/", 1)[1]
        self.id, self.rev, self.size, self.content_hash = "id:" + self.name, "rev1", local.stat().st_size, dropbox_hash(local)


class FakeClient:
    """Records Dropbox calls; serves downloads from local files."""
    def __init__(self, files):
        self.files, self.calls = files, []

    def files_move_v2(self, source, target, autorename=False):
        self.calls.append(("move", source, target))
        return mock.Mock(metadata=FakeRemote(target, self.files[source]))

    def files_delete_v2(self, path, parent_rev=None):
        self.calls.append(("delete", path))

    def files_download_to_file(self, local, path):
        import shutil
        self.calls.append(("download", path))
        shutil.copy(self.files[next(iter(self.files))], local)


class FixTests(unittest.TestCase):
    resolution = {"artist": "Steam Lizard feat. K McGill", "title": "Candle In The Dark (Extended Mix)",
                  "label": "Real Imprint", "sources": ["mail:x"]}
    final = "Steam Lizard feat. K McGill - Candle In The Dark (Extended Mix) [Real Imprint].aiff"

    def item(self, store, remote, tags):
        store.claim("mix:test", None, "Candle", "WAV")
        return {"asset_key": "mix:test", "remote": remote, "tags": tags, "album": None, "source_id": None,
                "asset_title": None, "name": remote.name}

    def test_correct_tags_only_need_a_dropbox_rename(self):
        from promo_automation import DEST, HOLD, Store
        with tempfile.TemporaryDirectory() as root:
            local = make_aiff(root, "a.aiff", {"artist": "x", "title": "y", "label": "z"})
            remote = FakeRemote(HOLD + "/Steam Lizard - Candle [Real Imprint].aiff", local)
            client = FakeClient({remote.path_lower: local})
            store = Store(root)
            tags = {"artist": self.resolution["artist"], "title": self.resolution["title"],
                    "grouping": "Real Imprint", "publisher": "Real Imprint"}
            result = research.fix(store, client, self.item(store, remote, tags), self.resolution)
            self.assertEqual(result["state"], "renamed")
            self.assertEqual(client.calls[0], ("move", remote.path_lower, DEST + "/" + self.final))
            self.assertNotIn("download", [c[0] for c in client.calls])
            self.assertEqual(store.asset("mix:test")["stage"], "delivery_verified")
            store.db.close()

    def test_wrong_tags_are_fixed_in_a_temporary_copy_that_is_deleted(self):
        from promo_automation import HOLD, Store
        with tempfile.TemporaryDirectory() as root:
            local = make_aiff(root, "a.aiff", {"artist": "Steam Lizard", "title": "Candle (Extended Mix)",
                                               "label": "Reaktion DJ Promo"})
            remote = FakeRemote(HOLD + "/Steam Lizard - Candle (Extended Mix) [Reaktion DJ Promo].aiff", local)
            client = FakeClient({remote.path_lower: local})
            store = Store(root)
            seen = {}

            def deliver(store_, shared, client_, target, asset_key):
                seen.update(name=target.name, folder=target.parent, tags=read_existing_tags(str(target)))
                return "/delivered/" + target.name
            with mock.patch.object(research, "dropbox_skill", return_value=(None, client)), \
                    mock.patch.object(pipeline, "deliver_file", deliver):
                result = research.fix(store, client, self.item(store, remote, {}), self.resolution)
            self.assertEqual(result["state"], "redelivered")
            self.assertEqual(seen["name"], self.final)
            self.assertEqual((seen["tags"]["grouping"], seen["tags"]["publisher"]), ("Real Imprint", "Real Imprint"))
            self.assertFalse(seen["folder"].exists())
            self.assertIn(("delete", remote.path_lower), client.calls)
            store.db.close()


class RemoteTagTests(unittest.TestCase):
    def test_aiff_tags_are_read_from_byte_ranges(self):
        import promo_remote
        with tempfile.TemporaryDirectory() as root:
            data = make_aiff(root, "a.aiff", {"artist": "Billy O", "title": "Stormcaller (Original Mix)",
                                              "label": "Killer Mushrooms"}).read_bytes()
            asked = []

            def ranged(client, path, start, end):
                asked.append(end - start + 1)
                return data[start:end + 1]
            with mock.patch.object(promo_remote, "ranged", ranged):
                tags = promo_remote.remote_tags(None, "/x.aiff")
            self.assertEqual((tags["artist"], tags["title"], tags["grouping"]),
                             ("Billy O", "Stormcaller (Original Mix)", "Killer Mushrooms"))
            self.assertLess(sum(asked), len(data) // 10)


if __name__ == "__main__":
    unittest.main()
