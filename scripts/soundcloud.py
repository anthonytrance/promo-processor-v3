"""SoundCloud promo-link adapter. Original files only, where the uploader enabled downloads. Never rips streams.

Promo mails link a private track or playlist (soundcloud.com/<user>/<slug>/s-<secret>, often behind an
on.soundcloud.com short link). api-v2 /resolve turns the link into JSON; private items carry a secret_token that the
download call needs. Playlists list only the first few tracks in full, the rest are stubs fetched through /tracks with
the playlist's secret. /tracks/<id>/download returns a signed cf-media.sndcdn.com URL of the uploaded original.
The account login is an OAuth token captured once from normal Chrome (1Password login with authenticator code), kept
DPAPI-protected in soundcloud-session.dpapi. No feedback gate exists. Profile links name no specific promo and are skipped.
"""
import argparse
import html
import json
import re
import time
from urllib.parse import urlsplit

from curl_cffi import requests

from audio_length import container
from promo_automation import SETTINGS, Store
from promo_cloud import protect_json, read_protected_json
from promo_mail import discover, newest_offer_age
from promo_pipeline import LibraryIndex, collect_release, write_stream

API = "https://api-v2.soundcloud.com"
SESSION_FILE = "soundcloud-session.dpapi"
_PROMO_PREFIX = re.compile(r"^\s*(?:\[\s*promo\s*\]|promo\s*(?://|:|-|\|))\s*", re.I)
_BRACKET = re.compile(r"\s*\[([^\[\]]*)\]\s*$")
_MIX_WORDS = re.compile(r"\b(mix|remix|edit|version|rework|dub|instrumental|bootleg|vip)\b", re.I)
_RELEASE_NOTE = re.compile(r"\b(?:out\s+now|release[ds]?|coming\s+soon|free\s+download|forthcoming|promo)\b", re.I)
_TAIL_AFTER_MIX = re.compile(r"^(.*\([^()]*\b(?:mix|remix|edit|extended|version|rework)\b[^()]*\))\s+([A-Za-z].*)$", re.I)
_GENERIC = {"demo", "promo", "untitled", "new track", "track", "master", "final"}
_RELEASE_LABEL = re.compile(r"\b(?:out\s+now|release[ds]?)\s+on\s+(.+?)\s*(?:,|$)", re.I)
# Subject brackets that are notes about the mail, not an imprint: "[Radio Show Submission]", "[145 BPM]", "[SWD111]".
# The settings key subject_note_words adds the user's own, such as the name of their radio show.
_SUBJECT_NOTE = re.compile(r"\b(?:" + "|".join(
    ["submission", "demo", "bpm", "free", "exclusive", "promo", "premiere", "ep", "lp", "album"]
    + [r"\s+".join(map(re.escape, w.split())) for w in SETTINGS.get("subject_note_words", [])]) + r")\b"
    r"|^[A-Z]{2,8}\s?\d{2,5}$", re.I)
# Two uploads of one track (an MP3 and a WAV copy) share a title and differ in length by at most this many seconds.
SAME_UPLOAD_SECONDS = 2


def soundcloud_host(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "soundcloud.com" or host.endswith(".soundcloud.com"))


def clean_link(url):
    """Mail extraction can glue a closing bracket or the next line onto a link; the last <br> part is the link."""
    url = url.split("<br>")[-1].strip()
    if not url.startswith("http"):
        url = "https://" + url
    return url.rstrip(").,;:!?'\"")


def link_variants(url):
    """Short links whose next word was glued on ("...XuHope") retry once without the trailing capitalised word."""
    url = clean_link(url)
    variants = [url]
    if (urlsplit(url).hostname or "").lower() == "on.soundcloud.com":
        trimmed = re.sub(r"(?<=[A-Za-z0-9])[A-Z][a-z]+$", "", url)
        if trimmed != url:
            variants.append(trimmed)
    return variants


def split_title(raw, artist_hint=None):
    """Clean a SoundCloud title: drop PROMO prefixes and release notes, keep mix names, pull a bracketed label."""
    title, label = _PROMO_PREFIX.sub("", raw or "").strip(), None
    while True:
        match = _BRACKET.search(title)
        if not match:
            break
        inner = match.group(1).strip()
        title = title[:match.start()].rstrip()
        if _MIX_WORDS.search(inner) and not _RELEASE_NOTE.search(inner):
            title = f"{title} ({inner})"  # "[Extended Mix]" is the mix name in square brackets
            break
        found = _RELEASE_LABEL.search(inner)
        if found:
            label = label or re.sub(r"\s+", " ", found.group(1)).strip().title()
        elif inner and not _RELEASE_NOTE.search(inner):
            label = label or inner  # "[Sunwaves Digital]", "[Aerodynamica Music]"
    title = re.sub(r"\s+,", ",", re.sub(r"\s+", " ", title)).strip()
    tail = _TAIL_AFTER_MIX.match(title)
    if tail:  # "See The World (Extended) Generation Smash/Smash The House": the label follows the mix bracket
        title, label = tail.group(1), label or tail.group(2).split("/")[0].strip()
    artist, sep, rest = title.partition(" - ")
    if sep:
        return artist.strip(), rest.strip(), label
    hint = (artist_hint or "").strip()
    if hint and title.casefold().startswith(hint.casefold() + " "):
        title = title[len(hint):].strip()  # "Neounique vision" by Neounique
    return hint or None, title, label


def mail_label(subject, sender=None):
    """Label named by the promo mail when SoundCloud has none; Beatport research still overrules it.

    A subject bracket ("... [Pure Emotion Records]") or the sender's name repeated in the subject
    ("Azure Recordings New Promos" from Azure Recordings) counts. Notes and catalogue numbers do not.
    """
    subject = re.sub(r"^\s*(?:(?:re|fwd?)\s*:\s*)+", "", subject or "", flags=re.I)
    for inner in reversed(re.findall(r"\[([^\[\]]+)\]", subject)):
        inner = inner.strip()
        if inner and not (_SUBJECT_NOTE.search(inner) or _MIX_WORDS.search(inner) or _RELEASE_NOTE.search(inner)):
            return inner
    name = re.sub(r"\s*<[^>]*>\s*$", "", sender or "").strip().strip('"')
    if len(name) > 2 and "@" not in name and name.casefold() in subject.casefold():
        return name
    return None


def normalize_release(item, extra_tracks=(), subject=None, sender=None):
    """Tracks with cleaned names, label evidence and the download offer from a resolved track or playlist."""
    if item.get("kind") not in {"track", "playlist"}:
        raise RuntimeError("SoundCloud link is a " + str(item.get("kind")) + ", not a track or playlist")
    full = {t["id"]: t for t in extra_tracks}
    entries = [full.get(t["id"], t) for t in item["tracks"]] if item["kind"] == "playlist" else [item]
    tracks, warnings = [], []
    for entry in entries:
        if "title" not in entry:
            raise RuntimeError("SoundCloud playlist track could not be loaded")
        meta = entry.get("publisher_metadata") or {}
        artist, title, bracket_label = split_title(html.unescape(entry["title"]), meta.get("artist"))
        if item["kind"] == "track" and subject and title.casefold() in _GENERIC:
            # "Andre S Demo": the mail subject names the track. Its brackets are notes, never the label.
            named = split_title(re.sub(r"^\s*(?:re|fwd?)\s*:\s*", "", subject, flags=re.I).split(":", 1)[-1])
            if named[0] and named[1].casefold() not in _GENERIC:
                artist, title, bracket_label = named[0], named[1], None
        if not artist and item["kind"] == "track":
            artist = (entry.get("user") or {}).get("username") or None  # an artist's own upload: "Deeper and Deeper"
        from_mail = mail_label(subject, sender)
        if from_mail and artist and from_mail.casefold() in artist.casefold():
            from_mail = None  # an artist mailing their own track names themselves, not a label
        label = (entry.get("label_name") or meta.get("publisher") or bracket_label or item.get("label_name")
                 or from_mail)
        files = []
        if entry.get("downloadable") and entry.get("has_downloads_left"):
            # The original's format is unknown until the transfer; the pipeline renames it from the file header.
            files.append({"id": str(entry["id"]), "type": "original", "track_id": str(entry["id"]),
                          "secret_token": entry.get("secret_token")})
        tracks.append({"name": f"{artist} - {title}" if artist else title, "artist": artist, "title": title,
                       "label": label, "isrc": meta.get("isrc"), "duration": round((entry.get("duration") or 0) / 1000),
                       "track_id": str(entry["id"]), "files": files})
    # Some uploads repeat one title for two lengths; keep the longest, as the version rule does. Two uploads of the
    # same length are one track in two formats ("incl. MP3 & WAV"): both offers stay and the probed lossless one wins.
    by_name = {}
    for track in tracks:
        kept = by_name.get(track["name"])
        if kept is not None and abs(track["duration"] - kept["duration"]) <= SAME_UPLOAD_SECONDS:
            kept["files"].extend(track["files"])
            warnings.append("same track uploaded twice; both copies offered, the lossless one is chosen")
        else:
            if kept is None or track["duration"] > kept["duration"]:
                by_name[track["name"]] = track
            if kept is not None:
                warnings.append("same title uploaded more than once; only the longest is kept")
    tracks = [t for t in tracks if by_name[t["name"]] is t]
    if not tracks:
        raise RuntimeError("SoundCloud item has no tracks")
    labels = {t["label"] for t in tracks if t["label"]}
    return {"title": item.get("title"), "label": labels.pop() if len(labels) == 1 else None, "gated": False,
            "tracks": tracks, "urn": f"{item['kind']}:{item['id']}", "warnings": warnings}


class SoundCloud:
    def __init__(self, store):
        self.store, self.calls, self.last = store, 0, 0.0
        saved = store.root / SESSION_FILE
        if not saved.exists():
            raise RuntimeError("No SoundCloud login saved; sign in once in normal Chrome and capture the OAuth token")
        token = read_protected_json(saved)["oauth_token"]
        self.session = requests.Session(impersonate="chrome124", headers={
            "Authorization": "OAuth " + token, "Origin": "https://soundcloud.com", "Referer": "https://soundcloud.com/"})

    def _send(self, url, **kwargs):
        """Paced GET; SoundCloud answers 429 (short links 404) after a quick burst, so wait and retry."""
        for wait in (30, 90, 240, None):
            pause = 1.5 - (time.monotonic() - self.last)
            if pause > 0:
                time.sleep(pause)
            self.calls += 1
            response = self.session.get(url, timeout=60, **kwargs)
            self.last = time.monotonic()
            if response.status_code != 429 or wait is None:
                return response
            time.sleep(int(response.headers.get("Retry-After") or wait))
        return response

    def _api(self, path, **params):
        response = self._send(API + path, params=params)
        if response.status_code == 401:
            raise RuntimeError("SoundCloud login expired; sign in again in normal Chrome")
        return response

    def resolve(self, url):
        """Resolve a mailed link to track or playlist JSON; short links redirect to the item page."""
        last = None
        for variant in link_variants(url):
            target = variant
            if (urlsplit(variant).hostname or "").lower() == "on.soundcloud.com":
                response = self._send(variant, allow_redirects=False)
                if response.status_code == 404:  # throttled short links answer 404, not 429; one patient retry
                    time.sleep(60)
                    response = self._send(variant, allow_redirects=False)
                target = response.headers.get("Location", "")
                if not soundcloud_host(target):
                    last = f"short link HTTP {response.status_code}"
                    continue
            response = self._api("/resolve", url=target)
            if response.status_code == 200:
                return response.json()
            last = f"resolve HTTP {response.status_code}"
        raise RuntimeError("SoundCloud link did not resolve (" + str(last) + "); removed, private or mangled in the mail")

    def snapshot(self, source_id, refresh=False):
        row = self.store.db.execute("SELECT 1 FROM sources WHERE id=? AND platform='soundcloud'", (source_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown SoundCloud source")
        cache = self.store.root / f"soundcloud-{source_id}.dpapi"
        if cache.exists() and not refresh:
            return read_protected_json(cache)
        self.store.event(source_id, "view", "intent", platform="soundcloud",
                         platform_effect="API lookup of the shared link; no plays are counted")
        item = self.resolve(self.store.source_url(source_id))
        extra = []
        if item.get("kind") == "playlist":
            stubs = [str(t["id"]) for t in item.get("tracks") or [] if "title" not in t]
            for start in range(0, len(stubs), 50):
                params = {"ids": ",".join(stubs[start:start + 50]), "playlistId": item["id"]}
                if item.get("secret_token"):
                    params["playlistSecretToken"] = item["secret_token"]
                response = self._api("/tracks", **params)
                if response.status_code != 200:
                    raise RuntimeError(f"SoundCloud playlist tracks HTTP {response.status_code}")
                extra.extend(response.json())
        page = {"item": item, "extra": extra}
        protect_json(cache, page)
        self.store.event(source_id, "view", "inspected", platform="soundcloud", kind=item.get("kind"),
                         tracks=len(item.get("tracks") or [item]), note="Metadata only; no audio fetched")
        return page

    def release(self, source_id):
        page = self.snapshot(source_id)
        subject, metadata = self.store.db.execute(
            "SELECT subject,metadata FROM sources WHERE id=?", (source_id,)).fetchone()
        release = normalize_release(page["item"], page["extra"], subject, json.loads(metadata or "{}").get("sender"))
        for track in release["tracks"]:
            if len(track["files"]) > 1:
                for file in track["files"]:
                    file["type"] = self.probe_format(file) or "original"
        return release

    def _original_url(self, file):
        params = {"secret_token": file["secret_token"]} if file.get("secret_token") else {}
        response = self._api(f"/tracks/{file['track_id']}/download", **params)
        if response.status_code != 200 or not response.json().get("redirectUri"):
            raise RuntimeError(f"SoundCloud gave no original download (HTTP {response.status_code})")
        url = response.json()["redirectUri"]
        if urlsplit(url).scheme != "https" or not (urlsplit(url).hostname or "").endswith(".sndcdn.com"):
            raise RuntimeError("SoundCloud download pointed outside its CDN")
        return url

    def probe_format(self, file):
        """Container of an uploaded original from its first 16 bytes; only used when one track was uploaded twice."""
        self.calls += 1
        response = requests.get(self._original_url(file), headers={"Range": "bytes=0-15"}, timeout=60,
                                impersonate="chrome124")
        return container(response.content[:16]) if response.status_code in (200, 206) else None

    @staticmethod
    def extension(file):
        return ".mp3"  # most originals; true_extension renames WAV, FLAC or AIFF from the header

    def unlock(self, source_id, release):
        raise RuntimeError("SoundCloud has no feedback gate; downloads are on or off per track")

    def download(self, source_id, file, destination):
        self.store.event(source_id, "platform_download", "intent", file_id=file["id"], platform="soundcloud",
                         platform_effect="original download counts against the uploader's download limit")
        self.store.sync_logs()
        url = self._original_url(file)
        self.calls += 1
        response = requests.get(url, stream=True, timeout=900, impersonate="chrome124")
        try:
            if response.status_code != 200:
                raise RuntimeError(f"SoundCloud original HTTP {response.status_code}")
            if any(kind in response.headers.get("Content-Type", "").lower() for kind in ("html", "json")):
                raise RuntimeError("SoundCloud returned a page instead of the original")
            return write_stream(response.iter_content(1 << 20), destination,
                                int(response.headers.get("Content-Length") or 0), tolerance=0)
        finally:
            response.close()

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30):
        """Every track or playlist link within the age window, newest mail first, each item once."""
        library, report, done = LibraryIndex(self.store), [], set()
        rows = self.store.db.execute(
            "SELECT id,subject FROM sources WHERE platform='soundcloud' ORDER BY discovered_at DESC,id").fetchall()
        for sid, subject in rows:
            age = newest_offer_age(self.store, sid)
            if age is None or age > max_age_days:
                continue
            try:
                item = self.snapshot(sid)["item"]
                if item.get("kind") not in {"track", "playlist"}:
                    report.append({"release": subject, "result": "skipped: profile link, no specific promo"})
                    continue
                urn = f"{item['kind']}:{item['id']}"
                if urn in done:
                    continue
                done.add(urn)
                summary = collect_release(self.store, self, sid, library, dry_run=dry_run, allow_gate=allow_gate)
                report.append({"release": summary.get("release") or subject, "tracks": summary["tracks"]})
            except Exception as error:
                self.store.event(sid, "batch", "release_failed", release=subject, error=str(error)[:200])
                report.append({"release": subject, "result": "failed: " + str(error)[:160]})
        self.store.sync_logs(force=True)
        return {"releases": report, "http_requests": self.calls}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    mail = sub.add_parser("discover-mail")
    mail.add_argument("--since", default="30d")
    collect = sub.add_parser("collect")
    collect.add_argument("source_id")
    batch = sub.add_parser("batch")
    batch.add_argument("--max-age-days", type=int, default=30)
    for command in (collect, batch):
        command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    store = Store()
    try:
        if args.command == "discover-mail":
            result = discover(store, args.since, ["soundcloud"])
        elif args.command == "collect":
            result = collect_release(store, SoundCloud(store), args.source_id, LibraryIndex(store), dry_run=args.dry_run)
            store.sync_logs(force=True)
        else:
            result = SoundCloud(store).batch(args.dry_run, max_age_days=args.max_age_days)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
