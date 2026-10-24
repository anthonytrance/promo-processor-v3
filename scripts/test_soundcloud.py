import unittest

from soundcloud import clean_link, link_variants, mail_label, normalize_release, split_title


def track(tid, title, duration=300000, downloadable=True, **extra):
    return dict({"id": tid, "kind": "track", "title": title, "duration": duration, "downloadable": downloadable,
                 "has_downloads_left": True, "secret_token": f"s-{tid}", "label_name": "",
                 "publisher_metadata": {}}, **extra)


class SoundCloudTests(unittest.TestCase):
    def test_mail_link_cleanup(self):
        self.assertEqual(clean_link("https://on.soundcloud.com/Qw3ErTy7UiOp1AsDf9)"), "https://on.soundcloud.com/Qw3ErTy7UiOp1AsDf9")
        self.assertEqual(clean_link("https://x.bandcamp.com<br>www.soundcloud.com/name"), "https://www.soundcloud.com/name")
        self.assertEqual(link_variants("https://on.soundcloud.com/Fx9TqWm2LpZr7NvBs4Hope"),
                         ["https://on.soundcloud.com/Fx9TqWm2LpZr7NvBs4Hope", "https://on.soundcloud.com/Fx9TqWm2LpZr7NvBs4"])
        self.assertEqual(len(link_variants("https://soundcloud.com/user/track/s-AbcDef")), 1)

    def test_title_cleanup_and_label(self):
        self.assertEqual(split_title("PROMO // Dave Harrigan - Drop Your Love (Extended Mix) [Sunwaves Digital]"),
                         ("Dave Harrigan", "Drop Your Love (Extended Mix)", "Sunwaves Digital"))
        self.assertEqual(split_title("Roger-M - Lose Myself [Extended Mix]"), ("Roger-M", "Lose Myself (Extended Mix)", None))
        self.assertEqual(split_title("Endless Cycle (Original Mix) [OUT NOW ON  GAIA-X MUSIC, 09/10/2026]", "Gaia-X"),
                         ("Gaia-X", "Endless Cycle (Original Mix)", "Gaia-X Music"))
        self.assertEqual(split_title("Gaia-X - Tranquility Base [FREE DOWNLOAD]"), ("Gaia-X", "Tranquility Base", None))
        self.assertEqual(split_title("[PROMO] Diego Antoine , DOSSCHY - See The World"), ("Diego Antoine, DOSSCHY", "See The World", None))
        self.assertEqual(split_title("Neounique vision", "Neounique"), ("Neounique", "vision", None))
        self.assertEqual(split_title("[PROMO] Diego Antoine , DOSSCHY - See The World (Extended) Generation Smash/Smash The House"),
                         ("Diego Antoine, DOSSCHY", "See The World (Extended)", "Generation Smash"))
        self.assertEqual(split_title("A - Title (Extended Mix)"), ("A", "Title (Extended Mix)", None))
        self.assertEqual(normalize_release(track(7, "Deeper and Deeper", user={"username": "REPLIKA"}))["tracks"][0]["name"],
                         "REPLIKA - Deeper and Deeper")

    def test_playlist_stubs_labels_and_offers(self):
        item = {"kind": "playlist", "id": 9, "title": "Promo", "label_name": "", "secret_token": "s-pl",
                "tracks": [track(1, "A - One (Extended Mix) [Azure]"), {"id": 2, "kind": "track"}]}
        release = normalize_release(item, [track(2, "B - Two", downloadable=False, label_name="Azure Recordings")])
        first, second = release["tracks"]
        self.assertEqual((first["label"], second["label"]), ("Azure", "Azure Recordings"))
        self.assertEqual(first["files"][0]["secret_token"], "s-1")
        self.assertEqual(second["files"], [])
        self.assertIsNone(release["label"])
        self.assertEqual(release["urn"], "playlist:9")
        with self.assertRaises(RuntimeError):
            normalize_release(item)  # a stub that could not be loaded is not silently dropped

    def test_repeated_title_keeps_the_longest(self):
        item = {"kind": "playlist", "id": 3, "title": "Janus", "tracks": [
            track(1, "PROMO // Delnoro - Janus [Sunwaves Digital]", 200000),
            track(2, "PROMO // Delnoro - Janus [Sunwaves Digital]", 400000)]}
        release = normalize_release(item)
        self.assertEqual([t["track_id"] for t in release["tracks"]], ["2"])
        self.assertTrue(release["warnings"])
        self.assertEqual(release["label"], "Sunwaves Digital")

    def test_generic_track_title_takes_the_mail_subject(self):
        item = track(5, "Andre S Demo", publisher_metadata={"artist": "Andre S"})
        subject = "PROMO: Andre S - Eclipse (Extended Mix) [Radio Show Submission]"
        named = normalize_release(item, subject=subject)["tracks"][0]
        self.assertEqual((named["name"], named["label"]), ("Andre S - Eclipse (Extended Mix)", None))
        self.assertEqual(normalize_release(track(6, "A - Real Title"), subject=subject)["tracks"][0]["name"], "A - Real Title")

    def test_same_length_uploads_are_one_track_in_two_formats(self):
        item = {"kind": "playlist", "id": 4, "title": "Janus incl. MP3 & WAV", "tracks": [
            track(1, "PROMO // Delnoro - Janus [Sunwaves Digital]", 425193),
            track(2, "PROMO // Delnoro - Janus [Sunwaves Digital]", 425193)]}
        (only,) = normalize_release(item)["tracks"]
        self.assertEqual([f["track_id"] for f in only["files"]], ["1", "2"])

    def test_label_from_the_mail(self):
        self.assertEqual(mail_label("New Promo: Ebon Light - Music Control [Pure Emotion Records]"), "Pure Emotion Records")
        self.assertEqual(mail_label("Azure Recordings New Promos", "Azure Recordings <promos@example.com>"),
                         "Azure Recordings")
        self.assertIsNone(mail_label("PROMO: Andre S - Eclipse (Extended Mix) [Radio Show Submission]"))
        self.assertIsNone(mail_label("PROMO: Super8 & Tab - Helsinki Scorchin (Sky Sound Extended Remix) [145 BPM]"))
        self.assertIsNone(mail_label("PROMO // Delnoro - Janus (Original Mix) [SWD111]"))
        self.assertIsNone(mail_label("Exclusive Promo | October Releases", "Sunwaves Digital <x@mailchimpapp.com>"))
        item = {"kind": "playlist", "id": 5, "title": "New", "tracks": [track(1, "Yozz - Victorious (Original Mix)")]}
        self.assertEqual(normalize_release(item, subject="Azure Recordings New Promos",
                                           sender="Azure Recordings <a@b.com>")["label"], "Azure Recordings")
        own = track(2, "Andre S - Eclipse (Extended Mix)")
        self.assertIsNone(normalize_release(own, subject="Andre S - Eclipse", sender="Andre S <a@b.com>")["label"])
        self.assertEqual(normalize_release(track(3, "IanT &amp; Flund - New Horizon"))["tracks"][0]["artist"], "IanT & Flund")

    def test_profile_links_are_not_releases(self):
        with self.assertRaises(RuntimeError):
            normalize_release({"kind": "user", "id": 1})


if __name__ == "__main__":
    unittest.main()
