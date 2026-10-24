import tempfile
import unittest
from pathlib import Path

from audio_length import container
from promo_pipeline import true_extension
from promochoons import parse_campaign, promochoons_url

PAGE = """<html><head><title>Activa - Velorum [VII]</title></head><body>
<table><tr><td><font><big><big>Activa<br>Velorum</big><br>VII&nbsp; <small>(VII132)</small><br></big></font></td></tr>
<tr><td><table class="tracklist" id="tracklist"><tbody>
<tr><td onclick="player.location = 'player?track=11&campaign=6737&key=K&owner=77';">&nbsp; &nbsp;1. Activa - Velorum (Extended Mix) [VII]</td>
<td><a href="download?track=11&campaign=6737&key=K&owner=77" target="iframe"><img></a></td></tr>
<tr><td onclick="player.location = 'player?track=12&campaign=6737&key=K&owner=77';">&nbsp; &nbsp;2. Activa - Velorum [VII]</td>
<td><a href="download?track=12&campaign=6737&key=K&owner=77" target="iframe"><img></a></td></tr>
<tr id="showzip"><td>Full release as .zip file</td><td><a href="downloadzip?campaign=6737&genkey=K&client=77"><img></a></td></tr>
</tbody></table></td></tr></table>
<form method="post" action="submit" target="iframe" name="feedback">
<input type="text" name="name" value="Example  &  Artist "><input type="text" name="email" value="promos@example.com">
<textarea name="comments"></textarea><input type="submit" value="Submit & Activate Download">
<input type="hidden" name="lets" value="go"><input type="hidden" name="campaign" value="6737"><input type="hidden" name="client" value="77">
</form></body></html>"""
URL = "https://promochoons.com/campaigns/?campaign=6737&entry=mail&key=K&uid=1"


class CampaignTests(unittest.TestCase):
    def test_tracks_label_previews_and_gate(self):
        release = parse_campaign(PAGE, URL)
        self.assertTrue(release["gated"])
        self.assertEqual(release["label"], "VII")
        self.assertEqual(release["campaign"], "6737")
        self.assertEqual(release["submit"], "https://promochoons.com/campaigns/submit")
        names = [t["name"] for t in release["tracks"]]
        self.assertEqual(names, ["Activa - Velorum (Extended Mix)", "Activa - Velorum"])  # zip row and numbering dropped
        first = release["tracks"][0]
        self.assertEqual((first["artist"], first["title"], first["label"]), ("Activa", "Velorum (Extended Mix)", "VII"))
        self.assertEqual(first["preview"], "https://promochoons.com/campaigns/media/client77/audio/11_preview.mp3")
        self.assertEqual(first["files"][0]["url"], "https://promochoons.com/campaigns/download?track=11&campaign=6737&key=K&owner=77")

    def test_header_label_without_catalogue_number(self):
        page = PAGE.replace("VII&nbsp; <small>(VII132)</small><br>", "Elpida Music<br>").replace(" [VII]", "")
        release = parse_campaign(page, URL)
        self.assertEqual(release["label"], "Elpida Music")
        self.assertEqual(release["tracks"][0]["label"], "Elpida Music")

    def test_wav_link_beside_default_is_preferred(self):
        from promo_pipeline import best_file
        wav = '<a href="download?track=11&campaign=6737&key=K&owner=77&format=wav" target="iframe"><img></a>'
        page = PAGE.replace('<td><a href="download?track=11&campaign=6737&key=K&owner=77" target="iframe"><img></a></td>',
                            '<td><a href="download?track=11&campaign=6737&key=K&owner=77" target="iframe"><img></a>' + wav + '</td>')
        first, second = parse_campaign(page, URL)["tracks"]
        self.assertEqual([f["type"] for f in first["files"]], ["original", "wav"])
        self.assertEqual(best_file(first["files"])["type"], "wav")
        self.assertEqual(len({f["id"] for f in first["files"]}), 2)
        self.assertEqual([f["type"] for f in second["files"]], ["original"])

    def test_lopsided_hyphen_split_bare_hyphen_kept(self):
        page = PAGE.replace("2. Activa - Velorum [VII]", "2. Dan Cooper & Rene Ablaze- I'm Floating (Extended Mix) [VII]")
        page = page.replace("1. Activa - Velorum (Extended Mix) [VII]", "1. Inoblivion x Brendan Bartels-Voidstalker (Extended Mix ) [VII]")
        first, second = parse_campaign(page, URL)["tracks"]
        self.assertEqual((second["artist"], second["title"]), ("Dan Cooper & Rene Ablaze", "I'm Floating (Extended Mix)"))
        self.assertEqual(second["name"], "Dan Cooper & Rene Ablaze - I'm Floating (Extended Mix)")
        self.assertEqual(first["artist"], "Inoblivion x Brendan Bartels-Voidstalker (Extended Mix )")  # left for research

    def test_only_promochoons_https(self):
        self.assertTrue(promochoons_url(URL))
        self.assertFalse(promochoons_url("http://promochoons.com/campaigns/"))
        self.assertFalse(promochoons_url("https://promochoons.com.evil.example/campaigns/"))


class TrueExtensionTests(unittest.TestCase):
    def test_container_from_header(self):
        self.assertEqual(container(b"RIFF\0\0\0\0WAVE"), "wav")
        self.assertEqual(container(b"FORM\0\0\0\0AIFF"), "aiff")
        self.assertEqual(container(b"ID3\x04"), "mp3")
        self.assertIsNone(container(b"<html>"))

    def test_mp3_staged_as_wav_is_renamed(self):
        with tempfile.TemporaryDirectory() as folder:
            staged = Path(folder) / "A - B (Extended Mix) [L].wav"
            staged.write_bytes(b"ID3\x04" + bytes(20))
            fixed = true_extension(staged)
            self.assertEqual(fixed.name, "A - B (Extended Mix) [L].mp3")
            self.assertTrue(fixed.exists() and not staged.exists())

    def test_matching_or_unknown_extension_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            wav = Path(folder) / "a.wav"
            wav.write_bytes(b"RIFF\0\0\0\0WAVE")
            self.assertEqual(true_extension(wav), wav)
            aif = Path(folder) / "b.aif"
            aif.write_bytes(b"FORM\0\0\0\0AIFF")
            self.assertEqual(true_extension(aif), aif)


if __name__ == "__main__":
    unittest.main()
