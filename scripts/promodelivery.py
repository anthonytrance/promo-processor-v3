"""Promo Delivery recipient links. Original ZIPs only, with one persisted reaction and shared pipeline delivery."""
import argparse
import json
from pathlib import Path
import random
import re
import shutil
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

from promo_automation import Store, now
from promo_cloud import FEEDBACK_CLOSERS, FEEDBACK_OPENERS, protect_json, read_protected_json
from promo_mail import discover, newest_offer_age
from promo_pipeline import LibraryIndex, collect_release, mix_key, version_kind, write_stream
from dropbox_promos import extract_audio
from process_promos import parse_filename

BASE = "https://www.promodelivery.co.uk/"
HOSTS = {"promodelivery.co.uk", "www.promodelivery.co.uk"}


def recipient_link(url):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    rid, user = (query.get("r") or [""])[0], (query.get("u") or [""])[0]
    if (parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username or parsed.password
            or parsed.path != "/promo.php" or not rid.isdecimal() or not re.fullmatch(r"[A-Za-z0-9_-]+", user)):
        return None
    return rid, user


def parse_page(text):
    soup = BeautifulSoup(text, "html.parser")
    def value(id):
        node = soup.select_one("#" + id)
        if node is None:
            raise RuntimeError("Promo Delivery recipient page schema changed or link expired: " + id)
        return node.get("value", "")
    rows = json.loads(value("trackInfoData"))
    fields = {n.select_one(".label").get_text(strip=True): n.select_one(".value").get_text(" ", strip=True)
              for n in soup.select("#release_info .info-row") if n.select_one(".label") and n.select_one(".value")}
    title = soup.select_one("#release_title_div [title]")
    if not rows or title is None:
        raise RuntimeError("Promo Delivery page has no recipient track inventory")
    archives = []
    for link in soup.select("a.downlink[href]"):
        kind = link.get_text(" ", strip=True).lower()
        filename, separator, user = link["href"].partition("&u=")
        if kind in {"wav", "mp3"} and separator and filename.lower().endswith(".zip") and user:
            archives.append({"type": kind, "filename": filename, "user": user})
    if not archives:
        raise RuntimeError("Promo Delivery page offers no original ZIP downloads")
    favourites = [n.get("value", "") for n in soup.select('input[name="favmix"]')]
    tracks = []
    for index, row in enumerate(rows):
        artist, title_text, mix = (str(row.get(k) or "").strip() for k in ("track_artist", "track_title", "track_mix"))
        if not artist or not title_text or not row.get("track_ID"):
            raise RuntimeError("Promo Delivery track metadata is incomplete")
        name = f"{artist} - {title_text}" + (f" ({mix})" if mix else "")
        tracks.append({"name": name, "artist": artist, "duration": None, "track_id": row["track_ID"],
                       "favourite": favourites[index] if index < len(favourites) else name,
                       "files": [{"id": str(row["track_ID"]), "type": a["type"], "track_name": name,
                                  "archive": a} for a in archives]})
    pending = [{"title": a.get_text(" ", strip=True), "url": urljoin(BASE, a["href"])}
               for a in soup.select("a[href]") if recipient_link(urljoin(BASE, a["href"]))]
    cdn = urlsplit(value("cloudfrontLink"))
    if cdn.scheme != "https" or not (cdn.hostname or "").endswith(".cloudfront.net"):
        raise RuntimeError("Promo Delivery original CDN changed; inspect before downloading")
    return {"title": title["title"], "label": fields.get("Label"), "catalogue": fields.get("Cat#"),
            "release_date": fields.get("Date"), "remote_id": value("promoID"), "reaction_id": value("reactionID"),
            "gated": value("reactionSubmitted") != "1", "tracks": tracks, "pending": pending,
            "cdn_host": cdn.hostname}


def archive_track(paths, name, kind):
    """Match archive members by parsed mix identity, never by ZIP ordering or a guessed CDN filename."""
    matches = []
    for path in paths:
        if path.suffix.lower() != "." + kind:
            continue
        candidates = [parse_filename(n, {}) for n in {path.name, re.sub(r"^\d{1,3}[. _-]+", "", path.name)}]
        if any(mix_key(f"{p.get('artist')} - {p.get('title')}") == mix_key(name) for p in candidates):
            matches.append(path)
    if len(matches) != 1:
        raise RuntimeError("Original ZIP does not identify exactly one file for " + name)
    return matches[0]


class PromoDelivery:
    def __init__(self, store):
        self.store, self.calls, self.packs = store, 0, {}
        self.session = requests.Session(impersonate="chrome124")

    def request(self, method, target, cdn_host=None, **kwargs):
        location = urljoin(BASE, target)
        for _ in range(5):
            parsed = urlsplit(location)
            if parsed.scheme != "https" or parsed.hostname not in HOSTS | ({cdn_host} if cdn_host else set()):
                raise RuntimeError("Promo Delivery request or redirect left its original hosts")
            self.calls += 1
            try:
                response = self.session.request(method, location, allow_redirects=False, timeout=kwargs.pop("timeout", 60), **kwargs)
            except Exception as error:
                raise RuntimeError("Promo Delivery request failed: " + type(error).__name__) from None
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            next_location = urljoin(location, response.headers.get("Location", ""))
            response.close()
            if method != "GET":
                raise RuntimeError("Reaction redirected; its outcome needs inspection before retrying")
            location = next_location
        raise RuntimeError("Promo Delivery redirect limit reached")

    def snapshot(self, source_id, refresh=False):
        row = self.store.db.execute("SELECT * FROM sources WHERE id=? AND platform='promo-delivery'", (source_id,)).fetchone()
        if row is None or not recipient_link(self.store.source_url(source_id)):
            raise ValueError("Unknown Promo Delivery recipient link")
        path = self.store.root / f"promo-delivery-{source_id}.dpapi"
        if path.exists() and not refresh:
            return read_protected_json(path)
        self.store.event(source_id, "view", "intent", platform="promo-delivery", platform_effect="recipient page may record opening")
        self.store.sync_logs()
        response = self.request("GET", self.store.source_url(source_id))
        try:
            if response.status_code != 200:
                raise RuntimeError(f"Promo Delivery recipient HTTP {response.status_code}")
            release = parse_page(response.text)
        finally:
            response.close()
        protect_json(path, release)
        for pending in release["pending"]:
            self.store.add_source("promo-delivery", pending["title"], pending["url"])
        self.store.event(source_id, "view", "inspected", platform="promo-delivery", tracks=len(release["tracks"]),
                         pending_links=len(release["pending"]), note="ZIP originals only; one package transfer if any mix is wanted")
        return release

    def release(self, source_id):
        return self.snapshot(source_id)

    @staticmethod
    def extension(file):
        return "." + file["type"]

    def unlock(self, source_id, release):
        row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            rng = random.Random(source_id + "|" + release["title"])
            comment = rng.choice(FEEDBACK_OPENERS) + ", " + rng.choice(FEEDBACK_CLOSERS)
            self.store.db.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?,NULL)",
                                  (source_id, rng.randint(7, 9), comment, 0, "pending", now()))
            self.store.db.commit()
            row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (source_id,)).fetchone()
        if row["status"] == "submitted":
            return
        if row["status"] != "pending":
            raise RuntimeError("Previous Promo Delivery reaction has an uncertain outcome; inspect before retrying")
        favourite = next((t for t in release["tracks"] if version_kind(t["name"]) == "extended"), release["tracks"][0])
        self.store.db.execute("UPDATE feedback SET status='submitting' WHERE source_id=?", (source_id,))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "intent", platform="promo-delivery", rating=row["rating"],
                         comment=row["comment"], supporting=False)
        self.store.sync_logs()
        response = self.request("POST", "reactionSubmit.php", data={"support": 0, "favmix": favourite["favourite"],
            "rating": row["rating"], "djreaction": row["comment"], "reactionID": release["reaction_id"]})
        try:
            if response.status_code != 200 or response.text.strip() != "1":
                self.store.event(source_id, "feedback", "uncertain", platform="promo-delivery", http=response.status_code)
                raise RuntimeError("Promo Delivery reaction was not confirmed; inspect before retrying")
        finally:
            response.close()
        self.store.db.execute("UPDATE feedback SET status='submitted',submitted_at=? WHERE source_id=?", (now(), source_id))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "submitted", platform="promo-delivery", rating=row["rating"])
        self.store.sync_logs()

    def download(self, source_id, file, destination):
        release = self.snapshot(source_id)
        key = (source_id, file["type"])
        paths = self.packs.get(key)
        if not paths or not all(p.exists() for p in paths):
            folder = Path(destination).parent.parent / ("package-" + file["type"])
            folder.mkdir(parents=True, exist_ok=True)
            archive = folder / "original.zip"
            params = {"fn": file["archive"]["filename"], "u": file["archive"]["user"]}
            check = self.request("GET", "downloadcheck.php?" + urlencode(params))
            try:
                if check.status_code != 200 or check.text.strip() != "1":
                    raise RuntimeError("Promo Delivery original download check refused the ZIP")
            finally:
                check.close()
            self.store.event(source_id, "platform_download", "intent", platform="promo-delivery", format=file["type"],
                             note="One original ZIP includes unwanted versions; only selected mixes are processed")
            self.store.sync_logs()
            response = self.request("GET", "download.php?" + urlencode(params), cdn_host=release["cdn_host"], stream=True, timeout=900)
            try:
                size = int(response.headers.get("Content-Length") or 0)
                if response.status_code != 200 or any(t in response.headers.get("Content-Type", "").lower() for t in ("html", "json")):
                    raise RuntimeError(f"Promo Delivery original ZIP HTTP {response.status_code} or non-archive reply")
                if shutil.disk_usage(folder).free < size * 3 + 1024**3:
                    raise RuntimeError("Insufficient temporary disk space for the original ZIP")
                write_stream(response.iter_content(1 << 20), archive, size or None, tolerance=0)
            finally:
                response.close()
            paths = extract_audio(archive, folder / "audio")
            self.packs[key] = paths
        local = archive_track(paths, file["track_name"], file["type"])
        with local.open("rb") as source:
            return write_stream(iter(lambda: source.read(1 << 20), b""), destination, local.stat().st_size, tolerance=0)

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30):
        library, seen, report = LibraryIndex(self.store), set(), []
        rows = self.store.db.execute("SELECT * FROM sources WHERE platform='promo-delivery'").fetchall()
        rows.sort(key=lambda r: newest_offer_age(self.store, r["id"]) if newest_offer_age(self.store, r["id"]) is not None else float("inf"))
        for row in rows:
            recipient = recipient_link(self.store.source_url(row["id"]))
            if not recipient or recipient[0] in seen:
                continue
            seen.add(recipient[0])
            age = newest_offer_age(self.store, row["id"])
            if age is None or age > max_age_days:
                report.append({"release": row["subject"], "state": "undated_or_too_old"})
                continue
            try:
                self.snapshot(row["id"], refresh=True)
                result = collect_release(self.store, self, row["id"], library, dry_run=dry_run, allow_gate=allow_gate)
                report.append({"release": row["subject"], "tracks": result["tracks"]})
            except Exception as error:
                message = re.sub(r"https?://[^\s]+", "[protected URL]", str(error))[:200]
                self.store.event(row["id"], "batch", "release_failed", platform="promo-delivery", error=message)
                report.append({"release": row["subject"], "state": "exception", "error": message})
        self.store.sync_logs(force=True)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover-mail")
    for command in ("collect", "batch"):
        sub = commands.add_parser(command)
        if command == "collect":
            sub.add_argument("source_id")
        sub.add_argument("--dry-run", action="store_true")
        sub.add_argument("--allow-feedback", action="store_true")
    args = parser.parse_args()
    store = Store()
    try:
        adapter = PromoDelivery(store)
        if args.command == "discover-mail":
            result = discover(store, "30d", ["promo-delivery"], sender_domain="promodelivery.co.uk")
        elif args.command == "batch":
            result = adapter.batch(args.dry_run, args.allow_feedback)
        else:
            result = collect_release(store, adapter, args.source_id, LibraryIndex(store), dry_run=args.dry_run, allow_gate=args.allow_feedback)
            store.sync_logs(force=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
