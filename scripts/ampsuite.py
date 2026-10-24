"""Ampsuite recipient links across label subdomains, reusing the shared promo pipeline.

Only the explicitly emailed release is inspected. The page's outstanding-promos links are not followed.
Download routes come from the rendered controls, never the preview player. Recipient links stay DPAPI protected.
"""
import argparse
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

from promo_automation import Store
from promo_cloud import protect_json, read_protected_json
from promo_mail import discover, newest_offer_age
from promo_pipeline import LibraryIndex, collect_release, write_stream


def parse_release(html, source_url):
    soup = BeautifulSoup(html, "html.parser")
    date = soup.select_one("#release_date")
    label_match = re.search(r"\s+on\s+(.+)$", date.get_text(" ", strip=True)) if date else None
    label = label_match[1].strip() if label_match else None
    artist = soup.select_one("#release_title")
    title = soup.select_one("#release_artist")
    if artist is None or title is None:
        raise RuntimeError("Ampsuite release page is unavailable or its schema changed")
    tracks = []
    for field in soup.select('[id^="track_title_"]'):
        track_id = field["id"].removeprefix("track_title_")
        artist_field = soup.select_one("#track_artist_" + track_id)
        if artist_field is None:
            raise RuntimeError("Track artist missing")
        track_artist, track_title = artist_field.get_text(" ", strip=True), field.get_text(" ", strip=True)
        row = field.parent.parent
        files = []
        for button in row.select(".web-link[onclick]"):
            kind = button.get("alt", "").lower()
            match = re.fullmatch(r"document\.location\s*=\s*['\"]([^'\"]+)['\"];?", button["onclick"].strip())
            if not match or kind not in {"mp3", "wav"}:
                continue
            url = urljoin(source_url, match[1])
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname != urlsplit(source_url).hostname or parsed.path != "/views/promotion/download_track.php":
                raise RuntimeError("Unexpected Ampsuite download control")
            files.append({"id": track_id + ":" + kind, "type": kind, "url": url})
        tracks.append({"name": track_artist + " - " + track_title, "artist": track_artist,
                       "track_id": track_id, "files": files})
    if not tracks:
        raise RuntimeError("Ampsuite page has no structured tracks")
    # The inspected page exposes real download controls independently of its optional rating form.
    # Do not submit unsolicited feedback merely because a feedback form is present.
    return {"title": artist.get_text(" ", strip=True) + " - " + title.get_text(" ", strip=True),
            "label": label, "gated": False, "tracks": tracks}


class Ampsuite:
    def __init__(self, store):
        self.store, self.calls = store, 0
        self.session = requests.Session(impersonate="chrome")

    def snapshot(self, source_id, refresh=False):
        source = self.store.db.execute("SELECT * FROM sources WHERE id=? AND platform='ampsuite'", (source_id,)).fetchone()
        if source is None:
            raise ValueError("Unknown Ampsuite source")
        url = self.store.source_url(source_id)
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not host.endswith(".ampsuite.com") or parsed.path != "/promotion/leave_feedback":
            raise ValueError("Expected an Ampsuite recipient feedback page")
        path = self.store.root / f"ampsuite-{source_id}.dpapi"
        if path.exists() and not refresh:
            return read_protected_json(path)
        self.store.event(source_id, "view", "intent", platform_effect="emailed recipient release page only")
        self.store.sync_logs()
        self.calls += 1
        try:
            response = self.session.get(url, timeout=60, allow_redirects=False)
        except Exception as error:
            raise RuntimeError("Ampsuite page request failed: " + type(error).__name__) from None
        try:
            if response.status_code != 200:
                raise RuntimeError(f"Ampsuite page answered HTTP {response.status_code}")
            release = parse_release(response.text, url)
        finally:
            response.close()
        protect_json(path, release)
        self.store.event(source_id, "view", "inspected", tracks=len(release["tracks"]),
                         note="No preview playback, feedback submission or outstanding-promo navigation")
        self.store.sync_logs()
        return release

    def release(self, source_id):
        return self.snapshot(source_id)

    @staticmethod
    def extension(file):
        return "." + file["type"]

    def unlock(self, source_id, release):
        raise RuntimeError("Ampsuite feedback gate has not been verified for this page; inspect before submitting")

    def download(self, source_id, file, destination):
        url = file["url"]
        self.calls += 1
        try:
            response = self.session.get(url, timeout=900, stream=True, allow_redirects=False)
        except Exception as error:
            raise RuntimeError("Ampsuite download failed: " + type(error).__name__) from None
        try:
            # A changed route or feedback redirect is an explicit exception, never a stream/preview fallback.
            if response.status_code != 200:
                raise RuntimeError(f"Ampsuite download answered HTTP {response.status_code}; inspect redirect/gate")
            if any(kind in response.headers.get("Content-Type", "").lower() for kind in ("html", "json")):
                raise RuntimeError("Ampsuite returned a page; possible feedback gate, inspect before retrying")
            return write_stream(response.iter_content(1 << 20), destination,
                                int(response.headers.get("Content-Length") or 0), tolerance=0)
        finally:
            response.close()

    def batch(self, dry_run=False, max_age_days=30):
        library, report = LibraryIndex(self.store), []
        rows = self.store.db.execute("SELECT id,subject FROM sources WHERE platform='ampsuite'").fetchall()
        for sid, subject in rows:
            age = newest_offer_age(self.store, sid)
            if age is None or age > max_age_days:
                report.append({"source_id": sid, "release": subject, "result": "undated or too old; skipped"})
                continue
            try:
                report.append({"source_id": sid, **collect_release(self.store, self, sid, library, dry_run=dry_run)})
            except Exception as error:
                self.store.event(sid, "batch", "release_failed", error=str(error)[:160])
                report.append({"source_id": sid, "release": subject, "error": str(error)[:160]})
        self.store.sync_logs(force=True)
        return {"releases": report, "http_requests": self.calls,
                "upgrade_candidates_ask_user": [t["track"] for r in report for t in r.get("tracks", [])
                                                   if t.get("action") == "upgrade_candidate"],
                "needs_review": [t["track"] for r in report for t in r.get("tracks", [])
                                 if t.get("action") == "needs_review"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    mail = sub.add_parser("discover-mail")
    mail.add_argument("--since", default="30d")
    collect = sub.add_parser("collect")
    collect.add_argument("source_id")
    collect.add_argument("--dry-run", action="store_true")
    batch = sub.add_parser("batch")
    batch.add_argument("--dry-run", action="store_true")
    batch.add_argument("--max-age-days", type=int, default=30)
    args = parser.parse_args()
    store = Store()
    try:
        adapter = Ampsuite(store)
        if args.command == "discover-mail":
            result = discover(store, args.since, ["ampsuite"], sender_domain="ampsuite.com")
        elif args.command == "collect":
            result = collect_release(store, adapter, args.source_id, LibraryIndex(store), dry_run=args.dry_run)
            store.sync_logs(force=True)
        else:
            result = adapter.batch(args.dry_run, args.max_age_days)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
