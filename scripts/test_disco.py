import json
import unittest

import disco


def page(download_links=True, name="ASR1183: Vikram Prabhu - Kiss The Night"):
    track = {"id": 217068433, "artist": "Vikram Prabhu", "name": "Kiss The Night (Original Mix)", "duration": 452,
             "track_type": "audio", "outside_your_territories": False, "nested_tracks": [
                 {"id": 217068434, "artist": "Vikram Prabhu", "name": "Kiss The Night (Radio Edit)", "duration": 201,
                  "track_type": "audio", "all_files": {"mp3": {"id": 3, "play_url": "https://a.disco.ac/play/3.mp3"}}}],
             "all_files": {"wav": {"id": 619995212, "play_url": "https://addictivesounds.disco.ac/play/1/download2/x.wav",
                                   "original_name": "Vikram Prabhu - Kiss The Night (Original Mix).wav"},
                           "mp3": {"id": 619995447, "play_url": "https://addictivesounds.disco.ac/play/1/download2/x.mp3"}}}
    cover = {"id": 5, "name": "Cover Image", "track_type": "image", "all_files": {"jpg": {"id": 9, "play_url": "https://a.disco.ac/9.jpg"}}}
    return {"playlist": {"name": name, "playlist_id": 32229378, "download_links": download_links, "tracks": [track, cover]},
            "business": {"name": "Addictive Sounds"}}


class DiscoTests(unittest.TestCase):
    def test_embedded_playlist_is_read(self):
        html = '<script>window.playlist_data = {"tracks": [], "name": "x"};\nwindow.user_id = 1;</script>'
        self.assertEqual(disco.embedded(html, "playlist_data"), {"tracks": [], "name": "x"})
        self.assertIsNone(disco.embedded("<html></html>", "playlist_data"))

    def test_release_tracks_files_and_label(self):
        release = disco.normalize_release(page())
        self.assertEqual([t["name"] for t in release["tracks"]],
                         ["Vikram Prabhu - Kiss The Night (Original Mix)", "Vikram Prabhu - Kiss The Night (Radio Edit)"])
        main = release["tracks"][0]
        self.assertEqual((main["duration"], main["label"]), (452, "Addictive Sounds"))
        self.assertEqual(sorted(f["type"] for f in main["files"]), ["mp3", "wav"])

    def test_downloads_off_leaves_no_files(self):
        release = disco.normalize_release(page(download_links=False))
        self.assertTrue(all(t["files"] == [] for t in release["tracks"]))

    def test_label_only_for_catalogue_numbered_playlists(self):
        self.assertIsNone(disco.normalize_release(page(name="LEA KEY TOP5 October"))["label"])

    def test_foreign_file_links_refused(self):
        data = page()
        data["playlist"]["tracks"][0]["all_files"]["wav"]["play_url"] = "https://evil.example/x.wav"
        self.assertEqual([f["type"] for f in disco.normalize_release(data)["tracks"][0]["files"]], ["mp3"])


if __name__ == "__main__":
    unittest.main()
