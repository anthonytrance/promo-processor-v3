import unittest

from ampsuite import parse_release
from promo_pipeline import select_versions


def fixture(host="label.ampsuite.com"):
    rows = []
    for ident, mix in ((1, "Extended Mix"), (2, "Radio Edit"), (3, "Intro Mix")):
        rows.append(f'''<div><div><span id="track_title_{ident}">Song ({mix})</span>
          <span id="track_artist_{ident}">Artist</span></div><div>
          <img class="web-link" alt="wav" onclick="document.location='https://{host}/views/promotion/download_track.php?p=1&amp;c=2&amp;id={ident}&amp;i=test&amp;f=w';" />
          </div></div>''')
    return '''<span id="release_title">Artist</span><span id="release_artist">Song</span>
      <span id="release_date">Out 04 Nov 2026 on Test Label</span><form id="feedback_form"></form>''' + "".join(rows)


class AmpsuiteTests(unittest.TestCase):
    def test_structured_metadata_and_actual_download_controls_are_mapped(self):
        release = parse_release(fixture(), "https://label.ampsuite.com/promotion/leave_feedback?p=test")
        self.assertEqual(release["label"], "Test Label")
        self.assertEqual(release["tracks"][0]["name"], "Artist - Song (Extended Mix)")
        self.assertFalse(release["gated"], "an optional feedback form is not proof of a mandatory gate")
        selected = select_versions(release["tracks"])
        self.assertEqual({name for name, result in selected.items() if result[0]},
                         {"Artist - Song (Extended Mix)", "Artist - Song (Intro Mix)"})

    def test_a_changed_download_host_is_not_followed(self):
        with self.assertRaisesRegex(RuntimeError, "Unexpected"):
            parse_release(fixture("evil.example"), "https://label.ampsuite.com/promotion/leave_feedback?p=test")

    def test_login_or_expired_page_does_not_masquerade_as_an_empty_release(self):
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            parse_release("<html>Sign in</html>", "https://label.ampsuite.com/promotion/leave_feedback?p=test")


if __name__ == "__main__":
    unittest.main()
