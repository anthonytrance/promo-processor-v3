"""hearthis.ai campaign-link adapter. The mailed recipient link (`/downloads/<campaign>?token=`) opens without a login.

The page heading is the release, usually `Artist "Title" (Label)`. Each `article.track` gives "Artist - Title (Mix)",
the format and size, and one download button per file (`.download[data-file-id][data-url]`). Every file needs its
own short review before its download appears: `POST /downloads/file/<id>/review` JSON {token, review_text}, answer
{ok: true}. A download is then `POST /downloads/file/<id>/download-click` {token} followed by GET of the button's
URL. Reviews are sent lazily, only for files the pipeline actually downloads, so skipped versions get no review.
A track already reviewed carries the class `track-reviewed`. Preview plays are recorded by the page, so never played.
"""
import argparse
import json
import random
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

from promo_automation import Store, now
from promo_cloud import FEEDBACK_CLOSERS, FEEDBACK_OPENERS, protect_json, read_protected_json
from promo_mail import discover, newest_offer_age
from promo_pipeline import LibraryIndex, collect_release, write_stream

BASE = "https://hearthis.ai"
HOSTS = {"hearthis.ai", "www.hearthis.ai"}
_LABEL = re.compile(r"\(([^()]+)\)\s*$")
_CATALOGUE = re.compile(r"^[A-Z]{2,}\d{2,}[A-Z]?\s*-\s*")


def squash(text):
    return re.sub(r"[^a-z0-9]+", "", text.lower().replace("&", " ").replace(" and ", " "))


def track_name(title, meta_artist):
    """'REND023 - Solardo Maur - Free' and a bare 'Set Me Free' become 'Artist - Title' from the track's artist field.
    A title artist that is fuller than the field ('KASIA & Faithless' beside 'Faithless') is kept."""
    title = _CATALOGUE.sub("", title).strip()
    artist, sep, rest = title.partition(" - ")
    if not meta_artist:
        return title, (artist if sep else None)
    if not sep:
        return f"{meta_artist} - {title}", meta_artist
    if squash(artist) == squash(meta_artist):
        return f"{meta_artist} - {rest}", meta_artist
    return title, artist


def campaign_link(url):
    """(campaign id, token) of a recipient link, or None."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in HOSTS:
        return None
    match = re.fullmatch(r"/downloads/([A-Za-z0-9_-]+)/?", parsed.path)
    token = (parse_qs(parsed.query).get("token") or [""])[0]
    return (match.group(1), token) if match and token else None


def parse_campaign(html):
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1")
    title = heading.get_text(" ", strip=True) if heading else (soup.title.get_text(strip=True) if soup.title else "")
    match = _LABEL.search(title)
    label = match.group(1).strip() if match else None
    tracks, gated = [], False
    for article in soup.select("article.track"):
        name_node = article.select_one(".track-title")
        buttons = article.select(".download[data-file-id][data-url]")
        if not name_node or not buttons:
            continue
        meta = article.select_one(".track-meta")
        meta_artist = meta.get_text(" ", strip=True).split(" · ")[0].strip() if meta else ""
        name, artist = track_name(name_node.get_text(" ", strip=True), meta_artist)
        track_title = name.partition(" - ")[2]
        reviewed = "track-reviewed" in (article.get("class") or [])
        gated = gated or not reviewed
        files = []
        for button in buttons:
            kind = button.get_text(" ", strip=True).lower().removeprefix("download").strip() or "original"
            files.append({"id": button["data-file-id"], "type": {"aif": "aiff"}.get(kind, kind),
                          "url": button["data-url"], "reviewed": reviewed})
        tracks.append({"name": name, "artist": (artist or "").strip() or None, "title": (track_title or name).strip(),
                       "label": label, "duration": None, "files": files})
    if not tracks:
        raise RuntimeError("hearthis.ai campaign has no downloadable tracks; schema changed or link expired")
    return {"title": title, "label": label, "gated": gated, "tracks": tracks}


class HearThis:
    platform = "hearthis"

    def __init__(self, store):
        self.store, self.calls, self.allowed = store, 0, False
        self.session = requests.Session(impersonate="chrome124")

    def _source(self, source_id):
        row = self.store.db.execute("SELECT 1 FROM sources WHERE id=? AND platform='hearthis'", (source_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown hearthis.ai source")
        target = campaign_link(self.store.source_url(source_id))
        if target is None:
            raise RuntimeError("Not a hearthis.ai campaign link (no campaign id and token)")
        return target

    def _request(self, method, path, **kwargs):
        url = urljoin(BASE, path)
        if urlsplit(url).scheme != "https" or (urlsplit(url).hostname or "").lower() not in HOSTS:
            raise RuntimeError("hearthis.ai request left hearthis.ai")
        self.calls += 1
        return self.session.request(method, url, allow_redirects=False, timeout=kwargs.pop("timeout", 60), **kwargs)

    def snapshot(self, source_id, refresh=False):
        self._source(source_id)
        cache = self.store.root / f"hearthis-{source_id}.dpapi"
        if cache.exists() and not refresh:
            return read_protected_json(cache)
        self.store.event(source_id, "view", "intent", platform="hearthis",
                         platform_effect="opening the campaign page may show a view")
        response = self._request("GET", self.store.source_url(source_id))
        if response.status_code != 200:
            raise RuntimeError(f"hearthis.ai campaign HTTP {response.status_code}")
        release = parse_campaign(response.text)
        protect_json(cache, release)
        self.store.event(source_id, "view", "inspected", platform="hearthis", tracks=len(release["tracks"]),
                         gated=release["gated"], note="Page only; no previews, reviews or originals")
        return release

    def release(self, source_id):
        return self.snapshot(source_id)

    @staticmethod
    def extension(file):
        return "." + (file["type"] if file["type"] != "original" else "wav")  # the pipeline corrects it from the header

    def unlock(self, source_id, release):
        """Permission only: each file is reviewed just before its own download, so skipped versions get no review."""
        self.allowed = True

    def comment(self, source_id):
        """One stock comment per campaign, saved before the first review so retries reuse the same text."""
        row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            rng = random.Random(source_id)
            self.store.db.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?,NULL)",
                                  (source_id, 0, rng.choice(FEEDBACK_OPENERS) + ", " + rng.choice(FEEDBACK_CLOSERS),
                                   0, "pending", now()))
            self.store.db.commit()
            row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (source_id,)).fetchone()
        return row["comment"]

    def review_state(self, source_id, file_id):
        """Per-file review outcome from the event log: submitted, intent (uncertain) or None."""
        states = [json.loads(details or "{}").get("file_id") == file_id and status for status, details in
                  self.store.db.execute("SELECT status,details FROM events WHERE source_id=? AND action='review'",
                                        (source_id,))]
        states = [s for s in states if s]
        return "submitted" if "submitted" in states else ("intent" if states else None)

    def review(self, source_id, file, token):
        state = self.review_state(source_id, file["id"])
        if state == "submitted":
            return
        if state == "intent":
            raise RuntimeError("Previous review of this file has an uncertain outcome; inspect before retrying")
        if not self.allowed:
            raise RuntimeError("hearthis.ai file needs a review; rerun with explicit feedback permission")
        text = self.comment(source_id)
        self.store.event(source_id, "review", "intent", file_id=file["id"], comment=text,
                         platform_effect="unlocks this file's download, visible to the sender")
        self.store.sync_logs()
        response = self._request("POST", f"/downloads/file/{file['id']}/review",
                                 json={"token": token, "review_text": text})
        try:
            result = response.json()
        except Exception:
            result = {}
        if response.status_code != 200 or not result.get("ok"):
            raise RuntimeError(f"hearthis.ai review answered HTTP {response.status_code} "
                               f"({str(result.get('error') or '')[:80]}); inspect before retrying")
        self.store.event(source_id, "review", "submitted", file_id=file["id"])
        self.store.db.execute("UPDATE feedback SET status='submitted',submitted_at=? WHERE source_id=?", (now(), source_id))
        self.store.db.commit()
        self.store.sync_logs()

    def download(self, source_id, file, destination):
        _, token = self._source(source_id)
        if not file.get("reviewed"):
            self.review(source_id, file, token)
        self.store.event(source_id, "platform_download", "intent", file_id=file["id"], platform="hearthis",
                         platform_effect="records a download click for the sender")
        self.store.sync_logs()
        click = self._request("POST", f"/downloads/file/{file['id']}/download-click", json={"token": token})
        try:
            clicked = click.json().get("ok")
        except Exception:
            clicked = False
        if click.status_code != 200 or not clicked:
            raise RuntimeError(f"hearthis.ai refused the download (HTTP {click.status_code}); review missing?")
        url = file["url"]
        for _ in range(4):
            response = self._request("GET", url, stream=True, timeout=900) if campaign_host(url) else None
            if response is None:  # signed storage URL off hearthis.ai
                self.calls += 1
                response = requests.get(url, impersonate="chrome124", stream=True, timeout=900, allow_redirects=False)
            if response.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.headers.get("Location", ""))
                response.close()
                if urlsplit(url).scheme != "https":
                    raise RuntimeError("hearthis.ai download redirected off https")
                continue
            break
        try:
            if response.status_code != 200:
                raise RuntimeError(f"hearthis.ai original HTTP {response.status_code}")
            kind = (response.headers.get("Content-Type") or "").lower()
            if "html" in kind or "json" in kind or response.headers.get("Content-Length") == "0":
                raise RuntimeError("hearthis.ai answered the download with a page instead of audio")
            return write_stream(response.iter_content(1 << 20), destination,
                                int(response.headers.get("Content-Length") or 0) or None, tolerance=0)
        finally:
            response.close()

    def campaigns(self, max_age_days=30):
        """One source per campaign id: the newest mail's link. Reminders resend the same campaign."""
        best = {}
        for (sid,) in self.store.db.execute("SELECT id FROM sources WHERE platform='hearthis' ORDER BY discovered_at,id"):
            target = campaign_link(self.store.source_url(sid))
            age = newest_offer_age(self.store, sid)
            if target is None or age is None or age > max_age_days:
                continue
            if target[0] not in best or age < best[target[0]][1]:
                best[target[0]] = (sid, age)
        return [sid for sid, _ in best.values()]

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30):
        library, report = LibraryIndex(self.store), []
        for sid in self.campaigns(max_age_days):
            subject = self.store.db.execute("SELECT subject FROM sources WHERE id=?", (sid,)).fetchone()[0]
            try:
                summary = collect_release(self.store, self, sid, library, dry_run=dry_run, allow_gate=allow_gate)
                report.append({"release": summary.get("release") or subject, "label": summary.get("label"),
                               "tracks": summary["tracks"]})
            except Exception as error:
                self.store.event(sid, "batch", "release_failed", release=subject, error=str(error)[:200])
                report.append({"release": subject, "result": "failed: " + str(error)[:160]})
        self.store.sync_logs(force=True)
        return {"releases": report, "http_requests": self.calls}


def campaign_host(url):
    return (urlsplit(urljoin(BASE, url)).hostname or "").lower() in HOSTS


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
        command.add_argument("--allow-feedback", action="store_true", help="send the per-file reviews that unlock downloads")
    args = parser.parse_args()
    store = Store()
    try:
        adapter = HearThis(store)
        if args.command == "discover-mail":
            result = discover(store, args.since, [adapter.platform])
        elif args.command == "collect":
            result = collect_release(store, adapter, args.source_id, LibraryIndex(store), dry_run=args.dry_run,
                                     allow_gate=args.allow_feedback)
            store.sync_logs(force=True)
        else:
            result = adapter.batch(args.dry_run, args.allow_feedback, max_age_days=args.max_age_days)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
