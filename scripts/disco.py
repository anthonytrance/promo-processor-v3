"""Disco (disco.ac) share-link adapter. The recipient share page embeds the playlist, so no login is needed.

Each promo mail carries per-recipient share links; every link opens one release page holding window.playlist_data
with tracks, lengths and signed original-file links. Disco has no recipient overview of every promo received, so
mail stays the source. Opening the page can show the sender a view; nothing else is sent and there is no feedback gate.
"""
import argparse
import json
import re
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests

from promo_automation import Store
from promo_cloud import protect_json, read_protected_json
from promo_mail import discover, newest_offer_age
from promo_pipeline import LibraryIndex, collect_release, write_stream

AUDIO = {"wav": "wav", "aif": "aiff", "aiff": "aiff", "flac": "flac", "mp3": "mp3", "m4a": "m4a"}
SHARE_PATHS = ("/share-new/", "/p/", "/share/")
_CATALOGUE = re.compile(r"^\s*[A-Z]{2,8}\s?\d{2,5}[A-Z]?\s*:")


def disco_host(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "disco.ac" or host.endswith(".disco.ac"))


def embedded(html, name):
    match = re.search(r"window\." + name + r"\s*=\s*", html)
    if not match:
        return None
    value, _ = json.JSONDecoder().raw_decode(html[match.end():])
    return value


def _flatten(tracks):
    for track in tracks or []:
        if isinstance(track, dict):
            yield track
            yield from _flatten(track.get("nested_tracks"))  # extra versions nested under the main track


def normalize_release(page):
    playlist, business = page["playlist"], page.get("business") or {}
    # A catalogue-numbered playlist ("ASR1183: ...") is a label's own release; otherwise the sender may be a PR agency.
    label = business.get("name") if _CATALOGUE.match(playlist.get("name") or "") else None
    tracks, seen = [], set()
    for track in _flatten(playlist.get("tracks")):
        if track.get("track_type") != "audio" or track.get("id") in seen:
            continue
        seen.add(track.get("id"))
        artist = (track.get("artist") or (track.get("metadata") or {}).get("Artist") or "").strip()
        title = (track.get("name") or "").strip()
        if not title:
            raise RuntimeError("Disco track without a title; metadata research required")
        files = []
        if playlist.get("download_links") and not track.get("outside_your_territories"):
            for ext, info in (track.get("all_files") or {}).items():
                kind = AUDIO.get(ext.lower())
                if kind and isinstance(info, dict) and info.get("play_url") and disco_host(info["play_url"]):
                    files.append({"id": str(info.get("id")), "type": kind, "url": info["play_url"],
                                  "name": info.get("original_name")})
        tracks.append({"name": f"{artist} - {title}" if artist else title, "artist": artist or None,
                       "title": title, "label": label, "duration": track.get("duration"),
                       "track_id": str(track["id"]), "files": files})
    if not tracks:
        raise RuntimeError("Disco playlist has no audio tracks")
    return {"title": playlist.get("name"), "label": label, "gated": False, "tracks": tracks,
            "playlist_id": playlist.get("playlist_id")}


class Disco:
    def __init__(self, store):
        self.store, self.calls = store, 0
        self.session = requests.Session(impersonate="chrome124")

    def _get(self, url, **kwargs):
        """GET that follows redirects only inside disco.ac."""
        for _ in range(6):
            if not disco_host(url):
                raise RuntimeError("Disco link redirected outside disco.ac")
            self.calls += 1
            response = self.session.get(url, allow_redirects=False, timeout=kwargs.pop("timeout", 60), **kwargs)
            if response.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.headers.get("Location", ""))
                response.close()
                continue
            return response
        raise RuntimeError("Disco link has too many redirects")

    def snapshot(self, source_id, refresh=False):
        row = self.store.db.execute("SELECT 1 FROM sources WHERE id=? AND platform='disco'", (source_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown Disco source")
        cache = self.store.root / f"disco-{source_id}.dpapi"
        if cache.exists() and not refresh:
            return read_protected_json(cache)
        url = self.store.source_url(source_id)
        if not disco_host(url) or not urlsplit(url).path.startswith(SHARE_PATHS):
            raise RuntimeError("Not a Disco recipient share link (campaign e-mail copies carry no tracks)")
        self.store.event(source_id, "view", "intent", platform="disco", platform_effect="opening the share page may show a view")
        response = self._get(url)
        if response.status_code != 200:
            raise RuntimeError(f"Disco share page HTTP {response.status_code}")
        playlist = embedded(response.text, "playlist_data")
        if not isinstance(playlist, dict) or not isinstance(playlist.get("tracks"), list):
            raise RuntimeError("Disco share page schema changed or link expired")
        page = {"playlist": playlist, "business": embedded(response.text, "business_data") or {}}
        protect_json(cache, page)
        self.store.event(source_id, "view", "inspected", platform="disco", tracks=len(playlist["tracks"]),
                         playlist_id=playlist.get("playlist_id"), note="No audio, plays or feedback submitted")
        return page

    def release(self, source_id):
        return normalize_release(self.snapshot(source_id))

    @staticmethod
    def extension(file):
        return "." + file["type"]

    def unlock(self, source_id, release):
        raise RuntimeError("Disco share links have no feedback gate; downloads are on or off per playlist")

    def download(self, source_id, file, destination):
        self.store.event(source_id, "platform_download", "intent", file_id=file["id"], platform="disco",
                         platform_effect="original file transfer from the signed share link")
        self.store.sync_logs()
        response = self._get(file["url"], stream=True, timeout=900)
        try:
            if response.status_code != 200:
                raise RuntimeError(f"Disco original HTTP {response.status_code}")
            if any(kind in response.headers.get("Content-Type", "").lower() for kind in ("html", "json")):
                raise RuntimeError("Disco returned a page instead of the original")
            return write_stream(response.iter_content(1 << 20), destination,
                                int(response.headers.get("Content-Length") or 0), tolerance=0)
        finally:
            response.close()

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30):
        """Every Disco share link within the age window; one mail has several links to the same playlist."""
        library, report, done = LibraryIndex(self.store), [], set()
        rows = self.store.db.execute("SELECT id,subject FROM sources WHERE platform='disco' ORDER BY discovered_at,id").fetchall()
        for sid, subject in rows:
            url = self.store.source_url(sid)
            if not urlsplit(url).path.startswith(SHARE_PATHS):
                continue
            age = newest_offer_age(self.store, sid)
            if age is None or age > max_age_days:
                continue
            try:
                playlist = self.snapshot(sid)["playlist"].get("playlist_id")
                if playlist in done:
                    continue
                done.add(playlist)
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
        adapter = Disco(store)
        if args.command == "discover-mail":
            result = discover(store, args.since, ["disco"])
        elif args.command == "collect":
            result = collect_release(store, adapter, args.source_id, LibraryIndex(store), dry_run=args.dry_run)
            store.sync_logs(force=True)
        else:
            result = adapter.batch(args.dry_run, max_age_days=args.max_age_days)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
