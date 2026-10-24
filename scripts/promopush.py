"""PromoPush (promopush.com) campaign-link adapter. The mailed recipient link opens the release page without a login.

Each promo mail links the campaign page (`/?track_id=<package>&username=&uuid=`) and an in-browser copy of the mail.
The page lists every mix (`tr.mix_tr[data-track-id]`) with its own download, plus the label (h4) and release date.
Downloads stay locked until a reaction is sent: `POST /package_react/` with a 1-10 personal and club rating, a
favourite mix and a comment (support boxes are left unticked). After that `/check_download_mix/<mix>/?uuid=` answers
"OK" and `/download_mix/<mix>/?uuid=` streams the original (WAV so far, the file name carries "[Label]").
A non-zero `rating_val` on the page means a reaction was already left, so the campaign is not gated.
The preview call `/top_bar/play_track/` names the original's waveform ("<hash>.wav_w.png", giving the format) and a
full-length preview MP3 whose header gives the length. Pages come back 403 when fetched too quickly, so calls are paced.
"""
import argparse
import json
import random
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

from audio_length import HEAD_BYTES, length_from_header
from promo_automation import Store, now
from promo_cloud import FEEDBACK_CLOSERS, FEEDBACK_OPENERS, RATING_RANGE, protect_json, read_protected_json
from promo_mail import discover, newest_offer_age
from promo_pipeline import LibraryIndex, collect_release, version_group, version_kind, write_stream

BASE = "https://promopush.com"
HOSTS = {"promopush.com", "www.promopush.com"}
PAUSE, BACKOFF = 1.5, 30
_FORMAT = re.compile(r"\.(wav|aiff?|flac|mp3|m4a)_w\.png$", re.IGNORECASE)
_TRAILING = re.compile(r"^(.*)\(([^()]*)\)\s+((?:extended|radio|club|original|instrumental|dub|short)\b[^()]*)$", re.IGNORECASE)


def campaign_query(url):
    """(package id, uuid) of a recipient link, or None for any other promopush.com link."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in HOSTS:
        return None
    query = parse_qs(parsed.query)
    package, uuid = (query.get("track_id") or [""])[0], (query.get("uuid") or [""])[0]
    if not re.fullmatch(r"\d+", package) or not re.fullmatch(r"[0-9a-fA-F-]{8,}", uuid):
        return None
    return package, uuid


def original_format(waveform):
    match = _FORMAT.search(urlsplit(waveform or "").path)
    return {"aif": "aiff"}.get(match.group(1).lower(), match.group(1).lower()) if match else "original"


def fold_version(title):
    """PromoPush labels write "Title (X Remix) Radio Edit"; the version belongs inside the bracket to group with the remix."""
    match = _TRAILING.match(title)
    return f"{match.group(1)}({match.group(2).strip()} {match.group(3).strip()})" if match else title


def parse_campaign(html):
    soup = BeautifulSoup(html, "html.parser")
    header = soup.select_one("h2.darkgrey")
    if header is None:
        raise RuntimeError("PromoPush campaign page has no release header; schema changed or link expired")
    text = lambda node: node.get_text(" ", strip=True) if node else ""
    rated = soup.select_one("[name=rating_val]")
    release_date = next((text(p).split(":", 1)[1].strip() for p in soup.select("p.rel-info")
                         if text(p).lower().startswith("release date")), None)
    tracks, seen = [], set()
    for row in soup.select("tr.mix_tr[data-track-id]"):
        mix_id = row["data-track-id"]
        cells = row.find_all("td")
        if mix_id in seen or len(cells) < 2 or not row.select_one("a.download_track"):
            continue
        seen.add(mix_id)
        title, artist = fold_version(text(cells[0])), text(cells[1])
        tracks.append({"name": f"{artist} - {title}", "artist": artist or None, "title": title, "duration": None,
                       "mix_id": mix_id, "files": [{"id": mix_id, "type": "original"}]})
    if not tracks:
        raise RuntimeError("PromoPush campaign has no mixes; schema changed or link expired")
    label = text(header.parent.find("h4")) or None  # the page's first h4 belongs to the password-reset dialog
    for track in tracks:
        track["label"] = label
    return {"title": f"{text(header)} - {text(soup.select_one('h3'))}", "label": label, "package": header.get("id"),
            "release_date": release_date, "gated": not (rated and rated.get("value") not in (None, "", "0")),
            "tracks": tracks}


class PromoPush:
    platform = "promopush"

    def __init__(self, store):
        self.store, self.calls, self.last = store, 0, 0.0
        self.session = requests.Session(impersonate="chrome124")

    def _get(self, path, params=None, stream=False, timeout=60):
        """Paced GET that follows redirects only inside promopush.com (the mailed link redirects to /associate/),
        with one slow retry when the site answers 403 for speed."""
        url, retried = urljoin(BASE, path), False
        for _ in range(6):
            if urlsplit(url).scheme != "https" or (urlsplit(url).hostname or "").lower() not in HOSTS:
                raise RuntimeError("PromoPush link redirected off-site")
            time.sleep(max(0.0, self.last + PAUSE - time.monotonic()))
            self.calls += 1
            response = self.session.get(url, params=params, stream=stream, timeout=timeout, allow_redirects=False)
            self.last = time.monotonic()
            if response.status_code in {301, 302, 303, 307, 308}:
                url, params = urljoin(url, response.headers.get("Location", "")), None
                response.close()
                continue
            if response.status_code == 403 and not retried:
                retried = True
                response.close()
                time.sleep(BACKOFF)
                continue
            return response
        raise RuntimeError("PromoPush link has too many redirects")

    def _source(self, source_id):
        row = self.store.db.execute("SELECT 1 FROM sources WHERE id=? AND platform='promopush'", (source_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown PromoPush source")
        target = campaign_query(self.store.source_url(source_id))
        if target is None:
            raise RuntimeError("Not a PromoPush campaign link (no package id and recipient key)")
        return target

    def snapshot(self, source_id, refresh=False):
        package, uuid = self._source(source_id)
        cache = self.store.root / f"promopush-{source_id}.dpapi"
        if cache.exists() and not refresh:
            return read_protected_json(cache)
        self.store.event(source_id, "view", "intent", platform="promopush",
                         platform_effect="opening the campaign page may show a view")
        response = self._get(self.store.source_url(source_id))
        if response.status_code != 200:
            raise RuntimeError(f"PromoPush campaign HTTP {response.status_code}")
        release = parse_campaign(response.text)
        if release["package"] != package:
            raise RuntimeError("PromoPush page shows a different campaign than the link")
        self.previews(release, uuid)
        protect_json(cache, release)
        self.store.event(source_id, "view", "inspected", platform="promopush", tracks=len(release["tracks"]),
                         gated=release["gated"], note="Preview details only; no originals or reaction")
        return release

    def previews(self, release, uuid):
        """Format for every mix; preview length only where a version group has no obvious extended mix."""
        groups = {}
        for track in release["tracks"]:
            groups.setdefault(version_group(track["name"]), []).append(track)
        for members in groups.values():
            for track in members:
                response = self._get("/top_bar/play_track/", params={"track_id": track["mix_id"], "uuid": uuid})
                info = response.json() if response.status_code == 200 else {}
                track["files"][0]["type"] = original_format(info.get("playform") or info.get("waveform"))
                if len(members) > 1 and version_kind(track["name"]) != "extended" and info.get("track_path"):
                    track["duration"] = self.preview_length(info["track_path"])

    def preview_length(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".amazonaws.com"):
            return None
        try:
            self.calls += 1
            response = self.session.get(url, stream=True, timeout=60)
            try:
                if response.status_code != 200:
                    return None
                head = b""
                for chunk in response.iter_content(HEAD_BYTES):
                    head += chunk
                    if len(head) >= HEAD_BYTES:
                        break
                total = int(response.headers.get("Content-Length") or 0)
            finally:
                response.close()
            value = length_from_header(head, total)
            return value if isinstance(value, float) else None
        except Exception:
            return None

    def release(self, source_id):
        return self.snapshot(source_id)

    @staticmethod
    def extension(file):
        return "." + (file["type"] if file["type"] != "original" else "wav")  # the pipeline corrects it from the header

    def choose_feedback(self, source_id, release):
        """Persist rating and wording before any submission so a retry reuses exactly the same text."""
        row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (source_id,)).fetchone()
        if row:
            return row
        rng = random.Random(source_id + "|" + release["title"])
        comment = rng.choice(FEEDBACK_OPENERS) + ", " + rng.choice(FEEDBACK_CLOSERS)
        self.store.db.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?,NULL)",
                              (source_id, rng.randint(*RATING_RANGE), comment, 0, "pending", now()))
        self.store.db.commit()
        return self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (source_id,)).fetchone()

    def unlock(self, source_id, release):
        """React once (rating, favourite mix, comment, no support claims) to unlock downloads. Never decline."""
        package, uuid = self._source(source_id)
        row = self.choose_feedback(source_id, release)
        if row["status"] == "submitted":
            return
        if row["status"] != "pending":
            raise RuntimeError("Previous feedback submission has an uncertain outcome; inspect before retrying")
        favourite = next((t for t in release["tracks"] if version_kind(t["name"]) == "extended"), release["tracks"][0])
        if not self.session.cookies.get("csrftoken"):
            self._get(self.store.source_url(source_id)).close()
        self.store.db.execute("UPDATE feedback SET status='submitting' WHERE source_id=?", (source_id,))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "intent", rating=row["rating"], scale="1-10", comment=row["comment"],
                         favourite=favourite["name"], platform_effect="unlocks downloads, visible to the label")
        self.store.sync_logs()
        self.calls += 1
        response = self.session.post(urljoin(BASE, "/package_react/"), timeout=60, allow_redirects=False, data={
            "form_id": f"react_{package}", "rating": row["rating"], "rating_club": row["rating"],
            "package_id": package, "package_id_club": package, "favourite_mix": favourite["mix_id"],
            "comment": row["comment"], "uuid": uuid},
            headers={"X-CSRFToken": self.session.cookies.get("csrftoken") or "", "X-Requested-With": "XMLHttpRequest",
                     "Referer": self.store.source_url(source_id)})
        if response.status_code != 200 or not response.text.strip():
            self.store.event(source_id, "feedback", "uncertain", http=response.status_code)
            raise RuntimeError(f"PromoPush reaction answered HTTP {response.status_code}; inspect before retrying")
        self.store.db.execute("UPDATE feedback SET status='submitted',submitted_at=? WHERE source_id=?", (now(), source_id))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "submitted", answer=re.sub(r"\s+", " ", response.text)[:40])
        self.store.sync_logs()

    def download(self, source_id, file, destination):
        _, uuid = self._source(source_id)
        check = self._get(f"/check_download_mix/{file['id']}/", params={"uuid": uuid})
        if check.status_code != 200:
            raise RuntimeError(f"PromoPush download check HTTP {check.status_code}; reaction missing?")
        self.store.event(source_id, "platform_download", "intent", file_id=file["id"], platform="promopush",
                         platform_effect="original transfer may mark the mix downloaded")
        self.store.sync_logs()
        response = self._get(f"/download_mix/{file['id']}/", params={"uuid": uuid}, stream=True, timeout=900)
        try:
            if response.status_code != 200:
                raise RuntimeError(f"PromoPush original HTTP {response.status_code}")
            kind = (response.headers.get("Content-Type") or "").lower()
            if "html" in kind or "json" in kind or response.headers.get("Content-Length") == "0":
                raise RuntimeError("PromoPush answered the download with a page instead of audio")
            return write_stream(response.iter_content(1 << 20), destination,
                                int(response.headers.get("Content-Length") or 0) or None, tolerance=0)
        finally:
            response.close()

    def campaigns(self, max_age_days=30):
        """One source per campaign: the newest mail's link. Reminders and the in-browser copy share the package id."""
        best = {}
        for (sid,) in self.store.db.execute("SELECT id FROM sources WHERE platform='promopush' ORDER BY discovered_at,id"):
            url = self.store.source_url(sid)
            target = campaign_query(url)
            age = newest_offer_age(self.store, sid)
            if target is None or urlsplit(url).path != "/" or age is None or age > max_age_days:
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
        command.add_argument("--allow-feedback", action="store_true", help="send the one-time reaction that unlocks downloads")
    args = parser.parse_args()
    store = Store()
    try:
        adapter = PromoPush(store)
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
