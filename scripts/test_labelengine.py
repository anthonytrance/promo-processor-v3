import unittest

from labelengine import link_params, page_keys, parse_release, store_label

LINK = ("https://label-engine.com/promo.php?label_id=80807&release_id=774637&recipient_id=9&blast_id=5"
        "&check=" + "a" * 64 + "&echeck=" + "b" * 64)
PAGE = ('<md-wavesurfer-audio><md-wavesurfer-source url="stream.php?track_id=11&key=K&t=1" duration="224" trackid="11" '
        'title="PRAANA - Mojave (mölly Remix)" downloadkey="k11" wav="1"></md-wavesurfer-source>'
        '<md-wavesurfer-source url="stream.php?track_id=12&key=K&t=1" duration="341" trackid="12" '
        'title="PRAANA - Mojave (mölly Extended Remix)" downloadkey="k12" wav="1"></md-wavesurfer-source></md-wavesurfer-audio>')
DATA = {"RELEASE_ID": 774637, "LABEL_ID": 80807, "CATALOGUE_NUMBER": "ENCOLOR15Y14P", "ARTIST": "PRAANA",
        "TITLE": "Mojave (mölly Remix)", "C_LINE": "2026 Colorize / Enhanced Music", "WAV_DOWNLOAD": 1,
        "FEEDBACK_GIVEN": 0, "FEEDBACK_REQUIRED": 1, "FEEDBACK_ID": 7, "FEEDBACK_ALERT": 0,
        "DOWNLOAD_CHECK": "c", "DOWNLOAD_ECHECK": "e", "label": {"NAME": "Colorize (Enhanced)"},
        "tracks": [{"TRACK_ID": 11, "ARTIST": "PRAANA", "TITLE": "Mojave", "TRACK_TITLE": "PRAANA - Mojave (mölly Remix)",
                    "FILE_INFO": "Stereo | 16-bit | 44100 Hz | 03:44 | 37.82 MB", "WAV": "11.flac", "ISRC": "X"},
                   {"TRACK_ID": 12, "ARTIST": "PRAANA", "TITLE": "Mojave",
                    "TRACK_TITLE": "PRAANA - Mojave (mölly Extended Remix)",
                    "FILE_INFO": "Stereo | 16-bit | 44100 Hz | 05:41 | 57.37 MB", "WAV": "12.flac", "ISRC": "Y"}]}


class LabelEngineTests(unittest.TestCase):
    def test_link_needs_all_six_parameters(self):
        self.assertEqual(link_params(LINK)["release_id"], "774637")
        self.assertIsNone(link_params("https://label-engine.com/"))
        self.assertIsNone(link_params(LINK.replace("&echeck=" + "b" * 64, "")))
        self.assertIsNone(link_params(LINK.replace("label-engine.com", "label-engine.com.evil.example")))

    def test_store_label_from_c_line(self):
        self.assertEqual(store_label("Colorize (Enhanced)", "2026 Colorize / Enhanced Music"), "Colorize")
        self.assertEqual(store_label("Enhanced Progressive", "2026 Enhanced Progressive / Enhanced Music"),
                         "Enhanced Progressive")
        self.assertEqual(store_label("Some Imprint", "2026 Other Owner"), "Some Imprint")

    def test_release_tracks_keys_and_lossless_first(self):
        release = parse_release(DATA, PAGE, link_params(LINK))
        self.assertEqual(release["label"], "Colorize")
        self.assertTrue(release["gated"])
        self.assertEqual(release["keys"], {"11": "k11", "12": "k12"})
        first, second = release["tracks"]
        self.assertEqual((first["artist"], first["title"]), ("PRAANA", "Mojave (mölly Remix)"))
        self.assertEqual((first["duration"], second["duration"]), (224.0, 341.0))
        self.assertEqual([f["type"] for f in second["files"]], ["flac", "mp3"])
        self.assertEqual(second["files"][0]["format"], "wav")

    def test_no_wav_offer_means_mp3_only_and_no_key_means_no_files(self):
        data = dict(DATA, WAV_DOWNLOAD=0)
        self.assertEqual([f["type"] for f in parse_release(data, PAGE, link_params(LINK))["tracks"][0]["files"]], ["mp3"])
        self.assertEqual(page_keys(PAGE.replace('downloadkey="k12" ', ""))["11"]["key"], "k11")
        self.assertEqual(parse_release(DATA, PAGE.replace('downloadkey="k12" ', ""), link_params(LINK))["tracks"][1]["files"], [])

    def test_feedback_already_given_is_not_gated(self):
        self.assertFalse(parse_release(dict(DATA, FEEDBACK_GIVEN=1), PAGE, link_params(LINK))["gated"])


if __name__ == "__main__":
    unittest.main()
