"""Check every sign-in the promo run needs and say in plain words which one still needs work, and how.

    python promo_doctor.py

Mail, Dropbox, Google and a saved SoundCloud session get a read-only check. SoundCloud may renew an expired
access token first; no promo is opened. Other promo platforms are only checked for a saved login on
this computer, because opening some of them marks promos as viewed. A platform with no
saved login is fine when no promos arrive from it; the first run names any that are needed. Exit code 0 means
everything the run cannot do without is ready.
"""
import json
import sys

import keyring

import promo_automation
from promo_automation import ACCOUNT, SETTINGS_FILE, STATE

# Platform, where its saved login lives, and how to make one. CTRL Promo, Needlepool and Fatdrop save their own
# login the first time a mailed link opens; the other platforms need none at all.
PLATFORM_LOGINS = (
    ("Promo Cloud", "keyring", "promo-cloud:{account}", "python promo_doctor.py save-password promo-cloud"),
    ("PromoBox", "keyring", "promobox:{account}", "python promo_doctor.py save-password promobox"),
    ("Inflyte", "keyring", "inflyte:remember-token",
     "sign in once in Chrome, then pipe the remember_user_token cookie as {\"value\": ..., \"expires\": ...} into "
     "python inflyte.py save-token"),
    ("SoundCloud", "file", "soundcloud-session.dpapi",
     "run python promo_doctor.py save-soundcloud and supply both session cookies from a separate SoundCloud login"),
)
PASSWORD_PLATFORMS = {"promo-cloud", "promobox"}


def check_mail():
    skill = promo_automation.email_skill()
    connection = skill.imap_login(ACCOUNT)
    try:
        status, _ = connection.select("INBOX", readonly=True)
    finally:
        connection.logout()
    if status != "OK":
        raise RuntimeError("the inbox could not be opened")
    return f"mailbox {ACCOUNT} opens"


def check_dropbox():
    _, client = promo_automation.dropbox_skill()
    name = client.users_get_current_account().name.display_name
    client.files_get_metadata(promo_automation.DEST)
    return f"Dropbox signed in as {name}, destination folder found"


def check_google():
    from gdrive_promos import gws
    user = gws("drive", "about", "get", "--params", json.dumps({"fields": "user(emailAddress)"}))
    return "Google Drive signed in as " + user["user"]["emailAddress"]


def platform_logins():
    for name, kind, key, how in PLATFORM_LOGINS:
        key = key.format(account=ACCOUNT)
        if kind == "keyring":
            saved = keyring.get_password(promo_automation.CREDENTIAL_SERVICE, key) is not None
        else:
            saved = (STATE / key).exists()
        yield name, saved, how


def check_soundcloud():
    from soundcloud_session import SoundCloudSession
    result = SoundCloudSession(STATE).check()
    return "SoundCloud login verified, " + ("automatic renewal saved" if result["renewal_saved"] else
                                           "access token only, save the refresh token for automatic renewal")


def save(command, platform=None):
    """Ask for a secret with typing hidden and keep it only in Windows' own protected stores."""
    import getpass
    if command == "save-password" and platform in PASSWORD_PLATFORMS:
        secret = getpass.getpass(f"{platform} password for {ACCOUNT} (typing is hidden): ")
        if secret:
            keyring.set_password(promo_automation.CREDENTIAL_SERVICE, f"{platform}:{ACCOUNT}", secret)
    elif command == "save-soundcloud":
        from soundcloud_session import SoundCloudSession
        secret = getpass.getpass("SoundCloud oauth_token cookie value (typing is hidden): ").strip()
        if secret:
            refresh = getpass.getpass("SoundCloud oauth_refresh_token cookie value (typing is hidden): ").strip()
            SoundCloudSession(STATE).save({"oauth_token": secret, "oauth_refresh_token": refresh})
    else:
        raise SystemExit("Use: promo_doctor.py save-password promo-cloud|promobox, or promo_doctor.py save-soundcloud")
    print("Saved." if secret else "Nothing was entered, so nothing was saved.")
    return 0


def main():
    if len(sys.argv) > 1:
        return save(*sys.argv[1:3])
    print(f"Settings: {SETTINGS_FILE if SETTINGS_FILE.exists() else 'none found, using the defaults'}.")
    print(f"State folder: {STATE}.")
    failed = 0
    for label, check, how in (
        ("Mail", check_mail, "add the mailbox to the email skill's credentials file"),
        ("Dropbox", check_dropbox, "sign in with the dropbox skill, and create the destination folder"),
        ("Google Drive", check_google, "sign in with: python google-workspace-files/scripts/setup.py"),
    ):
        try:
            print(f"Ready: {check()}.")
        except Exception as error:
            failed += label != "Google Drive"
            print(f"Not ready: {label}, {type(error).__name__}: {str(error)[:200]}. To fix it, {how}.")
    missing = []
    for name, saved, how in platform_logins():
        if saved and name == "SoundCloud":
            try:
                print(f"Ready: {check_soundcloud()}.")
            except Exception as error:
                failed += 1
                print(f"Not ready: SoundCloud, {type(error).__name__}: {str(error)[:200]}. To fix it, {how}.")
        elif saved:
            print(f"Saved login: {name}.")
        else:
            missing.append(f"{name}: {how}")
    if missing:
        print("No saved login yet, needed only if promos arrive from these:")
        for number, line in enumerate(missing, 1):
            print(f"{number}. {line}")
    print("CTRL Promo, Needlepool and Fatdrop save their own login from the first mailed link.")
    print("Google Drive is optional: without it, Drive promo links are reported instead of collected.")
    print("All required sign-ins are ready." if not failed else f"{failed} required sign-in(s) still need work.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
