"""Renew SoundCloud's website session without a browser. All credentials stay DPAPI protected.

The website POSTs /n/api/refresh-token with its two cookies and stores the replacement
cookies. This is separate from the public developer API's confidential-client flow.
Provision a fresh token pair for automation, since refresh cookies rotate.
"""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import parse_qs, quote, urlsplit

from curl_cffi import requests

from promo_automation import _try_lock, _unlock
from promo_cloud import protect_json, read_protected_json

SESSION_FILE = "soundcloud-session.dpapi"
REFRESH_URL = "https://soundcloud.com/n/api/refresh-token"
PROFILE_URL = "https://api-v2.soundcloud.com/me"
COOKIE_NAMES = ("oauth_token", "oauth_refresh_token")
AUTH_BASE = "https://api-auth.soundcloud.com"
REDIRECT_URI = "https://soundcloud.com/signin/callback"
EXCHANGE_URL = "https://soundcloud.com/n/api/code-exchange"


class TokenExpired(RuntimeError):
    """The coordinator stops this platform after one session failure."""


def token_claims(token):
    """Unverified JWT expiry is a scheduling hint; only /me verifies authentication."""
    try:
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "===").decode())
        return claims if isinstance(claims, dict) else {}
    except (ValueError, IndexError, AttributeError, UnicodeError):
        return {}


def atomic_save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(handle)
    try:
        protect_json(Path(name), data)
        with open(name, "rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.remove(name)


@contextmanager
def session_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "soundcloud-session.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 45
        while True:
            handle.seek(0)
            if _try_lock(handle):
                break
            if time.monotonic() >= deadline:
                raise TokenExpired("SoundCloud session renewal is busy; retry when the other worker finishes")
            time.sleep(0.1)
        try:
            yield
        finally:
            _unlock(handle)


class SoundCloudSession:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / SESSION_FILE
        self.calls = 0

    def _load(self):
        if not self.path.exists():
            raise TokenExpired("No SoundCloud session saved; capture an access and refresh token once")
        try:
            saved = read_protected_json(self.path)
            if not isinstance(saved.get("oauth_token"), str) or not saved["oauth_token"]:
                raise ValueError()
            return saved
        except Exception:
            raise TokenExpired("SoundCloud session cannot be read; save a fresh session") from None

    def token(self, rejected_token=None, force=False):
        """One serialized rotation, with a durable intent before consuming a refresh cookie.

        If another worker already replaced a refused token, reuse that replacement. Never
        automatically replay an uncertain refresh after a timeout, crash or interrupted save.
        """
        with session_lock(self.root):
            saved = self._load()
            if saved.get("refresh_pending"):
                raise TokenExpired("SoundCloud renewal was interrupted or refused; recapture the session before retrying")
            token = saved["oauth_token"]
            if rejected_token is not None and rejected_token != token:
                return token
            expiry = token_claims(token).get("exp", saved.get("expires_at"))
            fresh = isinstance(expiry, (int, float)) and expiry > time.time() + 60
            if not force and rejected_token is None and (fresh or expiry is None):
                return token
            if not saved.get("oauth_refresh_token"):
                raise TokenExpired("SoundCloud access token expired; save both the access and refresh tokens once")
            saved["refresh_pending"] = True
            atomic_save(self.path, saved)
            client = requests.Session(impersonate="chrome124")
            try:
                for name in COOKIE_NAMES:
                    client.cookies.set(name, saved[name], domain="soundcloud.com", path="/")
                self.calls += 1
                try:
                    response = client.post(REFRESH_URL, headers={
                        "Origin": "https://soundcloud.com", "Referer": "https://soundcloud.com/you/library"},
                        timeout=30, allow_redirects=False)
                except Exception:
                    raise TokenExpired("SoundCloud renewal outcome is uncertain; recapture the session before retrying") from None
                if response.status_code != 200:
                    raise TokenExpired(f"SoundCloud renewal refused (HTTP {response.status_code}); save a fresh session")
                # Journal returned credentials before interpreting their attributes. A cookie-shape
                # change must not lose a successful rotation or require replaying the old token.
                saved["refresh_result"] = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                                           for c in response.cookies.jar if c.name in COOKIE_NAMES]
                atomic_save(self.path, saved)
                # Read only response cookies, never mistake unchanged request cookies for a rotation.
                pair = {c.name: c.value for c in response.cookies.jar
                        if c.name in COOKIE_NAMES and c.domain.lstrip(".") in {"", "soundcloud.com"}
                        and c.path in {"", "/"}}
                if not all(pair.get(name) for name in COOKIE_NAMES):
                    raise TokenExpired("SoundCloud renewal returned no complete token pair; recapture the session")
                previous_subject = token_claims(token).get("sub")
                next_subject = token_claims(pair["oauth_token"]).get("sub")
                if previous_subject and previous_subject != next_subject:
                    raise TokenExpired("SoundCloud renewal changed account identity; save the intended account session")
                saved.update(pair, refreshed_at=time.time())
                saved.pop("refresh_pending", None)
                saved.pop("refresh_result", None)
                saved.pop("expires_at", None)
                atomic_save(self.path, saved)
                return saved["oauth_token"]
            finally:
                client.close()

    def check(self):
        """Read-only account check, refreshing once if the server refuses the access token."""
        token = self.token()
        for attempt in range(2):
            self.calls += 1
            try:
                response = requests.get(PROFILE_URL, headers={"Authorization": "OAuth " + token},
                                        impersonate="chrome124", timeout=30, allow_redirects=False)
            except Exception:
                raise RuntimeError("SoundCloud profile check could not connect") from None
            if response.status_code == 401 and attempt == 0:
                token = self.token(rejected_token=token)
                continue
            if response.status_code == 401:
                raise TokenExpired("SoundCloud refused the renewed access token; save a fresh session")
            if response.status_code != 200:
                raise RuntimeError(f"SoundCloud profile check HTTP {response.status_code}")
            profile = response.json()
            if not isinstance(profile, dict) or not profile.get("id"):
                raise RuntimeError("SoundCloud profile check returned no account identity")
            with session_lock(self.root):
                saved = self._load()
                if saved.get("user_id") and str(saved["user_id"]) != str(profile["id"]):
                    raise TokenExpired("SoundCloud account differs from the saved session identity")
                saved.update(user_id=str(profile["id"]), checked_at=time.time())
                atomic_save(self.path, saved)
            return {"authenticated": True, "renewal_saved": bool(saved.get("oauth_refresh_token"))}

    def save(self, pair, expected_user_id=None):
        """Verify a supplied pair before replacing any working saved credentials."""
        if not all(isinstance(pair.get(name), str) and pair[name].strip() for name in COOKIE_NAMES):
            raise ValueError("Both SoundCloud access and refresh tokens are required")
        token = pair["oauth_token"].strip()
        self.calls += 1
        try:
            response = requests.get(PROFILE_URL, headers={"Authorization": "OAuth " + token},
                                    impersonate="chrome124", timeout=30, allow_redirects=False)
        except Exception:
            raise RuntimeError("SoundCloud session validation could not connect") from None
        if response.status_code != 200 or not response.json().get("id"):
            raise TokenExpired("The supplied SoundCloud access token failed its profile check")
        if expected_user_id is not None and str(response.json()["id"]) != str(expected_user_id):
            raise TokenExpired("SoundCloud bootstrap returned a different account; existing credentials were preserved")
        with session_lock(self.root):
            atomic_save(self.path, {name: pair[name].strip() for name in COOKIE_NAMES} |
                        {"user_id": str(response.json()["id"]), "saved_at": time.time()})
        return {"authenticated": True, "renewal_saved": True}

    def bootstrap(self, browser_login):
        """Mint a fresh token pair using the website's existing sign-in session and PKCE.

        The browser's refresh cookie is neither read nor consumed. The broader sign-in
        cookie is used only in memory, for this explicitly invoked provisioning command.
        """
        cookie = browser_login.get("connect_session_cookie")
        client_id = browser_login.get("client_id")
        expected = browser_login.get("expected_user_id")
        if not all(isinstance(v, str) and v and "\r" not in v and "\n" not in v for v in (cookie, client_id)) or not expected:
            raise ValueError("The browser sign-in cookie, website client ID and expected account ID are required")
        client = requests.Session(impersonate="chrome124", headers={
            "Origin": "https://soundcloud.com", "Referer": "https://soundcloud.com/"})

        def send(method, url, **kwargs):
            self.calls += 1
            try:
                return client.request(method, url, timeout=30, allow_redirects=False, **kwargs)
            except Exception:
                raise RuntimeError("SoundCloud session bootstrap could not connect") from None

        try:
            client.cookies.set("_soundcloud_session", cookie, domain="api-auth.soundcloud.com", path="/")
            response = send("GET", AUTH_BASE + "/oauth/session", params={"client_id": client_id})
            if response.status_code != 200 or response.json().get("session") is not True:
                raise TokenExpired("SoundCloud's existing browser sign-in session is no longer valid")
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            nonce = secrets.token_urlsafe(48)
            state = {"client_id": client_id, "nonce": nonce}
            encoded = base64.b64encode(json.dumps(state, separators=(",", ":")).encode()).decode()
            response = send("POST", AUTH_BASE + "/oauth/authorize", params={"client_id": client_id}, json={
                "client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code",
                "code_challenge": challenge, "code_challenge_method": "S256", "state": encoded})
            if response.status_code != 200:
                raise TokenExpired(f"SoundCloud session authorization HTTP {response.status_code}")
            redirect = urlsplit(response.json().get("redirect_uri", ""))
            params = parse_qs(redirect.query)
            if (redirect.scheme != "https" or redirect.netloc != "soundcloud.com" or
                    redirect.path != "/signin/callback" or params.get("state") != [encoded] or
                    len(params.get("code", [])) != 1):
                raise TokenExpired("SoundCloud authorization returned an unexpected callback or state")
            auth_values = {"nonce": nonce, "pkceChallenge": challenge, "pkceVerifier": verifier}
            response = send("POST", EXCHANGE_URL, params={"grant_type": "authorization_code"}, data={
                "grant_type": "authorization_code", "code": params["code"][0], "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI, "client_id": client_id, "state": quote(encoded, safe=""),
                "authValues": quote(base64.b64encode(json.dumps(auth_values, separators=(",", ":")).encode()).decode(), safe="")})
            candidates = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                          for c in response.cookies.jar if c.name in COOKIE_NAMES]
            journal = self.root / "soundcloud-bootstrap.dpapi"
            atomic_save(journal, {"status": response.status_code, "cookies": candidates})
            if response.status_code != 200:
                raise TokenExpired(f"SoundCloud session exchange HTTP {response.status_code}")
            pair = {c["name"]: c["value"] for c in candidates
                    if c["domain"].lstrip(".") in {"", "soundcloud.com"} and c["path"] in {"", "/"}}
            result = self.save(pair, expected_user_id=expected)
            journal.unlink()
            return result
        finally:
            client.close()
