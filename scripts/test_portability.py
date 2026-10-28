"""Settings file, sign-in doctor and secret check: everything a new user's setup depends on, offline."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import promo_automation
import promo_doctor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import check_secrets  # noqa: E402


class SettingsTest(unittest.TestCase):
    def test_missing_file_means_defaults(self):
        self.assertEqual(promo_automation.load_settings(Path(tempfile.gettempdir()) / "no-such-settings.json"), {})

    def test_file_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({"destination": "/Music/Promos"}), encoding="utf-8")
            self.assertEqual(promo_automation.load_settings(path)["destination"], "/Music/Promos")


class DoctorTest(unittest.TestCase):
    def test_platform_logins_read_only_local_stores(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(promo_doctor, "STATE", Path(folder)), \
                mock.patch.object(promo_doctor.keyring, "get_password",
                                  side_effect=lambda service, key: "x" if key.startswith("promobox:") else None):
            (Path(folder) / "soundcloud-session.dpapi").write_bytes(b"")
            saved = {name: ok for name, ok, _ in promo_doctor.platform_logins()}
        self.assertEqual(saved, {"Promo Cloud": False, "PromoBox": True, "Inflyte": False, "SoundCloud": True})

    def test_unknown_save_command_is_refused(self):
        with self.assertRaises(SystemExit):
            promo_doctor.save("save-password", "inflyte")

    def test_saved_but_invalid_soundcloud_session_fails_doctor(self):
        with mock.patch.object(sys, "argv", ["promo_doctor.py"]), \
                mock.patch.object(promo_doctor, "check_mail", return_value="mail okay"), \
                mock.patch.object(promo_doctor, "check_dropbox", return_value="Dropbox okay"), \
                mock.patch.object(promo_doctor, "check_google", return_value="Google okay"), \
                mock.patch.object(promo_doctor, "platform_logins", return_value=[("SoundCloud", True, "save again")]), \
                mock.patch.object(promo_doctor, "check_soundcloud", side_effect=RuntimeError("session expired")), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(promo_doctor.main(), 1)
        self.assertTrue(any("Not ready: SoundCloud" in str(call) for call in printed.call_args_list))


class SecretCheckTest(unittest.TestCase):
    def test_real_looking_token_is_blocked(self):
        token = "ghp_" + "A1b2C3d4E5" * 4
        self.assertIn("GitHub token", check_secrets.problems("a.py", token.encode(), []))

    def test_placeholders_pass(self):
        self.assertEqual(check_secrets.problems("a.py", b'password = "not-a-real-password"', []), [])

    def test_private_words_and_allowed_spellings(self):
        text = b"see github.com/someone/repo, written by Someone"
        self.assertEqual(check_secrets.problems("a.md", text, ["someone"], ["github.com/someone"]),
                         ["private word from the local list"])
        self.assertEqual(check_secrets.problems("a.md", b"github.com/someone/repo", ["someone"],
                                                ["github.com/someone"]), [])

    def test_state_files_are_blocked_by_type(self):
        self.assertTrue(check_secrets.problems("state/inventory.sqlite3", b"", []))
        self.assertTrue(check_secrets.problems("dropbox_credentials.json", b"{}", []))
        self.assertTrue(check_secrets.problems("LOCAL.md", b"# notes", []))
        self.assertFalse(check_secrets.problems("references/local-setup.md", b"# notes", []))


# rar5-solid.rar from the rarfile 4.5 test suite (ISC licence, Marko Kreen): two small text files.
RAR5 = ("UmFyIRoHAQAJ78hvCwEFBwQGAQGAgIAA3vVwchwCAroABIAQtoMCoua3xYAbAQpzdGVzdDEudHh0waw3REQj+iP2l/1iU1G+TJGBQQKM"
        "wQN5XL9Ho6otoDaT3aBYAEACANRQBHb2xH8y6p8ypcTM14PfABDoLYkcAgKNAASAELaDAqLmt8XAGwEKc3Rlc3QyLnR4dEUVCmABAAgD"
        "37f79vwdd1ZRAwUEAA==")


class RarTest(unittest.TestCase):
    def test_rar_reads_through_the_installed_extractor(self):
        import base64
        from process_promos import open_rar, safe_member
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "pack.rar"
            archive.write_bytes(base64.b64decode(RAR5))
            with open_rar(archive) as container:
                self.assertEqual([m.filename for m in container.infolist()], ["stest1.txt", "stest2.txt"])
                with container.open(container.infolist()[1]) as member:
                    self.assertTrue(member.read())
        self.assertFalse(safe_member("../evil.wav"))
        self.assertFalse(safe_member("C:/evil.wav"))
        self.assertTrue(safe_member("Label/Artist - Track.wav"))


if __name__ == "__main__":
    unittest.main()
