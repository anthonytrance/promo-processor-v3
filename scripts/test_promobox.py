import tempfile
import unittest
from pathlib import Path
from unittest import mock

import promobox

TILE = """<div class="promo_tile">
<a href="https://promo-box.com/promo?dj=4242&id=abc123"><div release="{rid}"></div></a>
<div class="overflow-hidden">Sonik Recordings</div><div class="overflow-hidden">Jue feat. Gerri</div>
<div class="overflow-hidden">Don't Let Me Fade</div><div class="overflow-hidden">Sent: 23rd Oct 26</div>
<div class="promo_tile_status"><img src="/img/profile_tile_downloaded.png"></div></div>"""


class CatalogueTests(unittest.TestCase):
    def test_tile_fields_and_filler_skipped(self):
        html = TILE.format(rid="1523435") + '<div class="promo_tile"><div class="overflow-hidden">filler</div></div>'
        tiles = promobox.parse_catalogue(html)
        self.assertEqual(len(tiles), 1)
        tile = tiles[0]
        self.assertEqual((tile["release_id"], tile["label"], tile["artist"], tile["title"], tile["status"]),
                         ("1523435", "Sonik Recordings", "Jue feat. Gerri", "Don't Let Me Fade", "downloaded"))
        self.assertEqual(tile["sent"].date().isoformat(), "2026-10-23")

    def test_list_layout(self):
        html = """<div class="promo_list"><div class="promo_list_cover">
        <a href="https://promo-box.com/promo?dj=87&amp;id=d4"><img class="promo_link" release="1527760"/></a></div>
        <div class="promo_list_label"> Monster Pure <img src="images/star_off.png"/></div>
        <div class="promo_list_alias">Forbidden Mind</div><div class="promo_list_name">5AM</div>
        <div class="promo_list_date">Sent : 23rd October 2026 / Exp :&nbsp;2027-04-02</div>
        <div class="promo_list_status"><img src="https://www.promo-box.com/images/profile_list_new.png?v=1.00"/></div></div>"""
        tile = promobox.parse_catalogue(html)[0]
        self.assertEqual((tile["release_id"], tile["label"], tile["artist"], tile["title"], tile["status"]),
                         ("1527760", "Monster Pure", "Forbidden Mind", "5AM", "new"))
        self.assertEqual(tile["sent"].date().isoformat(), "2026-10-23")

    def test_shared_dj_value_keeps_releases_apart(self):
        tiles = promobox.parse_catalogue(TILE.format(rid="1") + TILE.format(rid="2"))
        self.assertEqual([t["release_id"] for t in tiles], ["1", "2"])

    def test_empty_list_and_foreign_link_refused(self):
        with self.assertRaises(RuntimeError):
            promobox.parse_catalogue("<html><body>login</body></html>")
        with self.assertRaises(RuntimeError):
            promobox.parse_catalogue(TILE.format(rid="1").replace("https://promo-box.com", "http://evil.example"))


class ReleasePageTests(unittest.TestCase):
    def test_older_template_with_rid_only(self):
        html = """<html><head><title>Amsterdam Trance Records - Limited Download Promo</title></head><body>
        <input id="dj" name="dj" value="8217"><input id="id" name="id" value="tok"><input id="rid" name="rid" value="1518660">
        <input id="force_feedback" name="force_feedback" value="1">
        <table><tr><td><a class="player_links">Alan Morris & Maria Nayler - Under Winter's Hold (Extended Mix)</a></td>
        <td><a class="wav_link" onclick="download_file('wav', '7663477', '1518660', '8217')">WAV</a></td></tr></table>
        </body></html>"""
        release = promobox.parse_release(html)
        self.assertEqual((release["release_id"], release["label"], release["gated"]), ("1518660", "Amsterdam Trance Records", False))
        self.assertEqual(release["tracks"][0]["files"], [{"id": "7663477", "type": "wav"}])

    def test_older_template_locked_uses_player_playlist(self):
        html = """<html><head><title>Amsterdam Trance Records - Limited Download Promo</title></head><body>
        <input id="dj" name="dj" value="8217"><input id="id" name="id" value="tok"><input id="rid" name="rid" value="1530542">
        <input id="force_feedback" name="force_feedback" value="1">
        <table><tr><td><a class="player_links" onclick="myPlaylist.play(1); return false;">A - Song (Original Mix)</a></td>
        <td><a class="wav_link" onclick="force()">WAV</a></td></tr></table>
        <script>myPlaylist = new jPlayerPlaylist({}, [ {title:"A - Song (Extended Mix)", trk_id:"7725655"},
        {title:"A - Song (Original Mix)", trk_id:"7725654"} ]);</script></body></html>"""
        release = promobox.parse_release(html)
        self.assertEqual(release["tracks"][0]["track_id"], "7725654")
        self.assertEqual(release["tracks"][0]["files"], [{"id": "7725654", "type": "wav"}])


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.adapter = promobox.PromoBox.__new__(promobox.PromoBox)
        self.adapter.store = mock.Mock(root=self.root)
        self.adapter.session = mock.Mock()
        self.adapter.calls = 0
        self.adapter.session.get.return_value = mock.Mock(text='<form><input name="_token" value="t"></form>')

    def test_refused_password_raises_without_caching(self):
        self.adapter.session.post.return_value = mock.Mock(status_code=302, headers={"Location": "https://promo-box.com/login"})
        with mock.patch.object(promobox.keyring, "get_password", return_value="secret"), \
                self.assertRaises(RuntimeError):
            self.adapter.login(force=True)
        self.assertFalse((self.root / "promobox-session.dpapi").exists())

    def test_missing_credential_raises(self):
        with mock.patch.object(promobox.keyring, "get_password", return_value=None), self.assertRaises(RuntimeError):
            self.adapter.login(force=True)


if __name__ == "__main__":
    unittest.main()
