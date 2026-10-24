"""Promochoons (promochoons.com) campaign-link adapter. The mailed recipient link opens the release page without a login.

Each promo mail links one campaign page (`/campaigns/?campaign=&entry=mail&key=&uid=`) listing the tracks with
per-track download links. The saved Promochoons account is the label upload side, so mail stays the source.
Downloads stay empty until the feedback form ("Submit & Activate Download": name, e-mail, optional comment) is sent.
The player's preview MP3s are full length, so their headers give track lengths before any original transfer.
Some labels add a WAV link (&format=wav) beside the default MP3 link; the WAV is preferred. The default link's
format is not labelled, so the pipeline renames the staged file from its header.
"""
import argparse
from html import unescape
import json
import random
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

from audio_length import HEAD_BYTES, length_from_header
from promo_automation import Store, now
from promo_cloud import FEEDBACK_CLOSERS, FEEDBACK_OPENERS, RATING_RANGE, protect_json, read_protected_json
from promo_mail import discover, newest_offer_age
from promo_pipeline import LibraryIndex, collect_release, version_group, version_kind, write_stream

HOSTS = {"promochoons.com", "www.promochoons.com"}
SNIFF = ((b"RIFF", "wav"), (b"FORM", "aiff"), (b"fLaC", "flac"), (b"ID3", "mp3"), (b"\xff\xfb", "mp3"), (b"\xff\xf3", "mp3"))
_NUMBER = re.compile(r"^\s*\d+\.\s*")
_LABEL = re.compile(r"\s*\[([^\[\]]+)\]\s*$")


def promochoons_url(url):
    parsed = urlsplit(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in HOSTS


def promo_file(track_id, url):
    """Some labels add a second link with &format=wav beside the default one (MP3 so far, confirmed from the header)."""
    kind = (parse_qs(urlsplit(url).query).get("format") or [""])[0].lower()
    kind = {"aif": "aiff"}.get(kind, kind)
    return {"id": f"{track_id}:{kind or 'default'}", "type": kind or "original", "url": url}


def parse_campaign(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", attrs={"name": "feedback"})
    hidden = {i.get("name"): i.get("value") for i in form.find_all("input")} if form else {}
    header = soup.find("big")
    header_label = None
    if header:  # "Artist<br>Title</big><br>LABEL (CAT)"; some campaigns give no catalogue number
        match = re.search(r"</big>\s*<br/?>\s*([^<(]+?)\s*(?:&nbsp;|\xa0)?\s*(?:<small>\(|<br/?>|</big>)", str(header))
        if match:
            header_label = unescape(match.group(1)).strip() or None
    if header_label is None and soup.title:  # page title "Artist - Title [LABEL]"
        match = _LABEL.search(soup.title.get_text(strip=True))
        header_label = match.group(1).strip() if match else None
    tracks = []
    for row in soup.select("#tracklist tr"):
        cell = row.find("td", onclick=True)
        links = row.find_all("a", href=re.compile(r"^download\?track="))
        if not cell or not links:
            continue
        query = parse_qs(urlsplit(links[0]["href"]).query)
        track_id, owner = query["track"][0], query.get("owner", [""])[0]
        text = _NUMBER.sub("", cell.get_text(" ", strip=True).replace("\xa0", " ")).strip()
        label_match = _LABEL.search(text)
        label = label_match.group(1).strip() if label_match else header_label
        name = _LABEL.sub("", text).strip()
        artist, _, title = name.partition(" - ")
        if not title:  # "Artist- Title" or "Artist -Title"; a bare hyphen is left alone (names contain them)
            parts = re.split(r"\s-|-\s", name, maxsplit=1)
            if len(parts) == 2:
                artist, title = parts
                name = f"{artist.strip()} - {title.strip()}"
        tracks.append({"name": name, "artist": artist.strip() or None, "title": (title or name).strip(), "label": label,
                       "duration": None, "track_id": track_id,
                       "preview": urljoin(page_url, f"media/client{owner}/audio/{track_id}_preview.mp3"),
                       "files": [promo_file(track_id, urljoin(page_url, link["href"])) for link in links]})
    if not tracks:
        raise RuntimeError("Promochoons campaign has no tracks; schema changed or link expired")
    title = soup.title.get_text(strip=True) if soup.title else None
    return {"title": title, "label": header_label or tracks[0]["label"], "gated": form is not None,
            "campaign": hidden.get("campaign"), "client": hidden.get("client"),
            "name": hidden.get("name"), "email": hidden.get("email"),
            "submit": urljoin(page_url, form.get("action") or "submit") if form else None, "tracks": tracks}


class Promochoons:
    def __init__(self, store):
        self.store, self.calls = store, 0
        self.session = requests.Session(impersonate="chrome124")

    def _get(self, url, **kwargs):
        """GET that follows redirects only inside promochoons.com."""
        for _ in range(6):
            if not promochoons_url(url):
                raise RuntimeError("Promochoons link redirected off-site")
            self.calls += 1
            response = self.session.get(url, allow_redirects=False, timeout=kwargs.pop("timeout", 60), **kwargs)
            if response.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.headers.get("Location", ""))
                response.close()
                continue
            return response
        raise RuntimeError("Promochoons link has too many redirects")

    def snapshot(self, source_id, refresh=False):
        row = self.store.db.execute("SELECT 1 FROM sources WHERE id=? AND platform='promochoons'", (source_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown Promochoons source")
        cache = self.store.root / f"promochoons-{source_id}.dpapi"
        if cache.exists() and not refresh:
            return read_protected_json(cache)
        url = self.store.source_url(source_id)
        if not promochoons_url(url) or not urlsplit(url).path.startswith("/campaigns"):
            raise RuntimeError("Not a Promochoons campaign link")
        self.store.event(source_id, "view", "intent", platform="promochoons",
                         platform_effect="opening the campaign page may show a view")
        response = self._get(url)
        if response.status_code != 200:
            raise RuntimeError(f"Promochoons campaign HTTP {response.status_code}")
        release = parse_campaign(response.text, url)
        self.lengths(release)
        protect_json(cache, release)
        self.store.event(source_id, "view", "inspected", platform="promochoons", tracks=len(release["tracks"]),
                         campaign=release["campaign"], note="Preview headers only; no originals or feedback")
        return release

    def lengths(self, release):
        """Full-length preview MP3 headers give lengths for groups with several versions."""
        groups = {}
        for track in release["tracks"]:
            groups.setdefault(version_group(track["name"]), []).append(track)
        for members in groups.values():
            if len(members) < 2:
                continue
            for track in members:
                if version_kind(track["name"]) != "extended":
                    track["duration"] = self.preview_length(track["preview"])

    def preview_length(self, url):
        try:
            response = self._get(url, stream=True)
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
        return "." + (file["type"] if file["type"] != "original" else "mp3")  # the pipeline corrects it from the header

    def choose_feedback(self, source_id, release):
        """Persist rating and wording before any submission so a retry reuses exactly the same text."""
        row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (source_id,)).fetchone()
        if row:
            return row
        rng = random.Random(source_id + "|" + (release["title"] or ""))
        comment = rng.choice(FEEDBACK_OPENERS) + ", " + rng.choice(FEEDBACK_CLOSERS)
        self.store.db.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?,NULL)",
                              (source_id, rng.randint(*RATING_RANGE), comment, 0, "pending", now()))
        self.store.db.commit()
        return self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (source_id,)).fetchone()

    def unlock(self, source_id, release):
        """Send the campaign's feedback form once (name, e-mail, comment; no rating field). Never decline."""
        row = self.choose_feedback(source_id, release)
        if row["status"] == "submitted":
            return
        if row["status"] != "pending":
            raise RuntimeError("Previous feedback submission has an uncertain outcome; inspect before retrying")
        if not release.get("submit") or not promochoons_url(release["submit"]):
            raise RuntimeError("Promochoons feedback form missing or off-site")
        self.store.db.execute("UPDATE feedback SET status='submitting' WHERE source_id=?", (source_id,))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "intent", comment=row["comment"], name=release["name"],
                         platform_effect="activates downloads, visible to the label")
        self.store.sync_logs()
        self.calls += 1
        response = self.session.post(release["submit"], timeout=60, allow_redirects=False, data={
            "name": release["name"], "email": release["email"], "comments": row["comment"], "lets": "go",
            "campaign": release["campaign"], "client": release["client"]})
        if response.status_code != 200:
            self.store.event(source_id, "feedback", "uncertain", http=response.status_code, answer=response.text[:120])
            raise RuntimeError(f"Promochoons feedback answered HTTP {response.status_code}; inspect before retrying")
        self.store.db.execute("UPDATE feedback SET status='submitted',submitted_at=? WHERE source_id=?", (now(), source_id))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "submitted", answer=re.sub(r"\s+", " ", response.text)[:120])
        self.store.sync_logs()
        # The unlock lives in this web session, not the link: keep its cookies so a retry needs no second feedback.
        protect_json(self.store.root / f"promochoons-session-{source_id}.dpapi", self.session.cookies.get_dict())

    def restore_session(self, source_id):
        saved = self.store.root / f"promochoons-session-{source_id}.dpapi"
        if saved.exists():
            self.session.cookies.update(read_protected_json(saved))

    def download(self, source_id, file, destination):
        self.restore_session(source_id)
        self.store.event(source_id, "platform_download", "intent", file_id=file["id"], platform="promochoons",
                         platform_effect="original transfer may mark the track downloaded")
        self.store.sync_logs()
        response = self._get(file["url"], stream=True, timeout=900)
        try:
            if response.status_code != 200:
                raise RuntimeError(f"Promochoons original HTTP {response.status_code}")
            if any(kind in response.headers.get("Content-Type", "").lower() for kind in ("html", "json")) \
                    or response.headers.get("Content-Length") == "0":
                raise RuntimeError("Promochoons returned no original; feedback gate still locked?")
            return write_stream(response.iter_content(1 << 20), destination,
                                int(response.headers.get("Content-Length") or 0), tolerance=0)
        finally:
            response.close()

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30):
        library, report, done = LibraryIndex(self.store), [], set()
        rows = self.store.db.execute(
            "SELECT id,subject FROM sources WHERE platform='promochoons' ORDER BY discovered_at,id").fetchall()
        for sid, subject in rows:
            if not urlsplit(self.store.source_url(sid)).path.startswith("/campaigns"):
                continue
            age = newest_offer_age(self.store, sid)
            if age is None or age > max_age_days:
                continue
            try:
                campaign = self.snapshot(sid)["campaign"]
                if campaign in done:
                    continue
                done.add(campaign)
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
        command.add_argument("--allow-feedback", action="store_true", help="send the feedback form to activate downloads")
    args = parser.parse_args()
    store = Store()
    try:
        adapter = Promochoons(store)
        if args.command == "discover-mail":
            result = discover(store, args.since, ["promochoons"])
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
