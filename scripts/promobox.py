"""PromoBox adapter: password login, the /promos release list, original WAV download, feedback gate, batch."""
import argparse
import hashlib
from html import unescape
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import random
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests
import keyring

from promo_automation import ACCOUNT, Store, now
from promo_cloud import SERVICE, FEEDBACK_CLOSERS, FEEDBACK_OPENERS, RATING_RANGE, protect_json, read_protected_json
from promo_pipeline import LibraryIndex, collect_release, version_kind, write_stream
from process_promos import parse_filename

BASE = "https://promo-box.com"
HOSTS = {"promo-box.com", "www.promo-box.com"}
# The catalogue has no track-list revision. Periodically revalidate even an unchanged tile.
METADATA_MAX_AGE_SECONDS = 30 * 86400


def catalogue_signature(release_id, subject, sent, url):
    return hashlib.sha256(json.dumps([release_id, subject, sent, url], ensure_ascii=False).encode()).hexdigest()


def parse_catalogue(html):
    """Every release on the logged-in /promos page, tile or list layout: id, link, label, artist, title, sent date, status."""
    soup = BeautifulSoup(html, "html.parser")
    tiles = []
    for tile in soup.select("div.promo_tile, div.promo_list"):
        link, cover = tile.select_one('a[href*="/promo?dj="]'), tile.select_one("[release]")
        if not link or not cover:
            continue  # layout filler tiles carry no release
        url = link["href"]
        target = urlsplit(url)
        query = parse_qs(target.query)
        if target.scheme != "https" or target.hostname not in HOSTS or target.path != "/promo"                 or not query.get("dj", [""])[0].isdecimal() or not query.get("id") or not cover["release"].isdecimal():
            raise RuntimeError("PromoBox list tile schema changed")
        fields = [tile.select_one(f".promo_list_{name}") for name in ("label", "alias", "name")]
        if all(fields):
            lines = [d.get_text(" ", strip=True) for d in fields]
            lines.append(tile.select_one(".promo_list_date").get_text(" ", strip=True))
        else:
            lines = [d.get_text(" ", strip=True) for d in tile.select("div.overflow-hidden")]
        sent = next((re.search(r"Sent\s*:\s*(\d+)\w*\s+([A-Za-z]{3})\w*\s+(\d{2,4})", l) for l in lines if l.startswith("Sent")), None)
        status = tile.select_one(".promo_tile_status img, .promo_list_status img")
        tiles.append({"release_id": cover["release"], "url": url,
                      "label": lines[0] if lines else "", "artist": lines[1] if len(lines) > 1 else "",
                      "title": lines[2] if len(lines) > 2 else "",
                      "sent": datetime.strptime(f"{sent[1]} {sent[2]} {sent[3][-2:]}", "%d %b %y").replace(tzinfo=timezone.utc)
                      if sent else None,
                      "status": re.sub(r"^profile_(tile|list)_|\.png.*$", "", status["src"].rsplit("/", 1)[-1]) if status else None})
    if not tiles:
        raise RuntimeError("PromoBox promo list is empty or its schema changed")
    return tiles


def parse_release(html):
    soup = BeautifulSoup(html, "html.parser")
    # older campaign pages carry the release id only as #rid
    def value(selector):
        el = soup.select_one(selector)
        return el.get("value", "") if el else ""
    rid, dj, token, forced = value("#release_id") or value("#rid"), value('input[name="dj"]'), value("#id"), value("#force_feedback")
    if not rid.isdecimal() or not dj.isdecimal() or not token or forced not in {"0", "1"}:
        raise RuntimeError("PromoBox recipient schema changed or link expired")
    document_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    label = re.sub(r"\s*-\s*(?:Limited Download Promo|Promo Download).*", "", document_title, flags=re.I).strip()
    playlist = re.findall(r'trk_id:\s*"(\d+)"', html)
    tracks = []
    for player in soup.select("a.player_links"):
        row = player.find_parent("tr")
        flag = row.select_one('[id^="track_"]') if row else None
        button = re.search(r"download_file\('\w+',\s*'(\d+)'", str(row)) if row else None
        slot = re.search(r"myPlaylist\.play\((\d+)\)", player.get("onclick", ""))
        listed = playlist[int(slot[1])] if slot and int(slot[1]) < len(playlist) else None
        if not row or not (flag or button or listed):
            continue
        # older template: the id sits only in unlocked buttons, or in the player playlist while locked
        track_id = flag["id"].removeprefix("track_") if flag else button[1] if button else listed
        if not track_id.isdecimal():
            raise RuntimeError("PromoBox track identity changed")
        name = player.get_text(" ", strip=True)
        info = parse_filename(name + ".wav", {})
        files = []
        for kind in ("wav", "mp3"):
            control = row.select_one("a." + kind + "_link")
            if control is None:
                continue
            action = control.get("onclick", "") + control.get("href", "")
            if "download_file(" in action or (forced == "1" and "force()" in action):
                files.append({"id": track_id, "type": kind})
        length = re.search(r"Track Time :</div><div>(\d+:\d{2}(?::\d{2})?)<", unescape(str(row)))
        tracks.append({"name": name, "artist": info.get("artist"), "track_id": track_id, "files": files,
                       "duration": length[1] if length else None})
    if not tracks or not label:
        raise RuntimeError("PromoBox page contains no verified recipient tracks/label")
    received = soup.select_one("#feedback_top_text")
    unlocked = "download_file(" in html  # the force flag stays set after feedback; live buttons mean open
    return {"title": label + ": " + tracks[0]["name"], "label": label, "gated": forced == "1" and not unlocked,
            "feedback_left": bool(received and "feedback below was received" in received.get_text(" ", strip=True)),
            "tracks": tracks, "release_id": rid, "dj": dj, "download_token": token, "csrf": value('input[name="_token"]'),
            "name": value("#name"), "email": value("#email")}


class PromoBox:
    def __init__(self, store):
        self.store = store
        self.session = requests.Session(impersonate="chrome")
        self.calls = 0

    def login(self, force=False):
        """Password login, kept in a protected cookie cache. Credential Manager holds the 1Password copy."""
        cache = self.store.root / "promobox-session.dpapi"
        if cache.exists() and not force:
            self.session.cookies.update(read_protected_json(cache))
            return
        password = keyring.get_password(SERVICE, "promobox:" + ACCOUNT)
        if not password:
            raise RuntimeError("PromoBox password missing from Credential Manager; copy it from 1Password")
        self.session.cookies.clear()  # a live session bounces /login to the list and the form token goes missing
        page = self.session.get(BASE + "/login", timeout=40)
        token = BeautifulSoup(page.text, "html.parser").select_one('form input[name="_token"]')
        self.calls += 2
        answer = self.session.post(BASE + "/login", timeout=40, allow_redirects=False, headers={"Referer": BASE + "/login"},
                                   data={"_token": token["value"] if token else "", "profile_email": ACCOUNT, "password": password})
        if answer.status_code != 302 or urlsplit(answer.headers.get("Location", "")).path != "/promos":
            raise RuntimeError(f"PromoBox login refused (HTTP {answer.status_code})")
        protect_json(cache, self.session.cookies.get_dict())

    def catalogue(self):
        """The logged-in promo list, registered as sources keyed by PromoBox release id."""
        self.login()
        for attempt in range(2):
            self.calls += 1
            page = self.session.get(BASE + "/promos", timeout=60, allow_redirects=False)
            if page.status_code == 200 and "/promo?dj=" in page.text:
                break
            self.login(force=True)
        else:
            raise RuntimeError("PromoBox promo list unavailable after a fresh login")
        protect_json(self.store.root / "promobox-session.dpapi", self.session.cookies.get_dict())
        tiles = parse_catalogue(page.text)
        for tile in tiles:
            subject = f"{tile['label']}: {tile['artist']} - {tile['title']}"
            sent = tile["sent"].date().isoformat() if tile["sent"] else None
            signature = catalogue_signature(tile["release_id"], subject, sent, tile["url"])
            previous = self.store.db.execute(
                "SELECT id,subject,metadata FROM sources WHERE platform='promobox' AND remote_id=?", (tile["release_id"],)).fetchone()
            if previous and not json.loads(previous["metadata"]).get("catalogue_signature"):
                old = catalogue_signature(tile["release_id"], previous["subject"],
                                          json.loads(previous["metadata"]).get("sent"), self.store.source_url(previous["id"]))
                if old == signature:
                    self._adopt_legacy_cache(previous["id"], tile["release_id"], signature)
            tile["source_id"] = self.store.add_source(
                "promobox", subject, tile["url"], remote_id=tile["release_id"], sent=sent, status=tile["status"])
            # add_source preserves metadata on existing rows. Refresh this catalogue's
            # offer date and status so a reminder or reoffer is not missed next run.
            row = self.store.db.execute("SELECT metadata FROM sources WHERE id=?", (tile["source_id"],)).fetchone()
            metadata = json.loads(row[0])
            metadata.update(sent=sent, status=tile["status"],
                            catalogue_signature=signature)
            self.store.db.execute("UPDATE sources SET metadata=?,subject=? WHERE id=?",
                                  (json.dumps(metadata), subject, tile["source_id"]))
        self.store.db.commit()
        return tiles

    def coordinator_sources(self, max_age_days=30):
        """Choose actual account releases by catalogue offer date, never digest/footer URLs."""
        from promo_mail import newest_offer_age
        today, chosen = datetime.now(timezone.utc), []
        for row in self.store.db.execute("SELECT id,remote_id,metadata FROM sources WHERE platform='promobox'"):
            if not row["remote_id"] or not row["remote_id"].isdecimal():
                continue
            metadata = json.loads(row["metadata"])
            age = newest_offer_age(self.store, row["id"])
            try:
                sent = datetime.strptime(metadata.get("sent") or "", "%Y-%m-%d").replace(tzinfo=timezone.utc)
                catalogue_age = (today - sent).total_seconds() / 86400
                age = min(age, catalogue_age) if age is not None else catalogue_age
            except (ValueError, TypeError):
                pass
            if age is not None and age <= max_age_days:
                chosen.append(row["id"])
        return chosen

    def _mail_marker(self, source_id):
        offers = self.store.db.execute(
            "SELECT message_id,received FROM messages WHERE source_id=? ORDER BY message_id,received", (source_id,)).fetchall()
        return hashlib.sha256(json.dumps([list(row) for row in offers]).encode()).hexdigest()

    def _adopt_legacy_cache(self, source_id, release_id, signature):
        """One-time migration only when the old catalogue fields/link equal today's and no newer mail exists."""
        cache = self.store.root / f"promobox-{source_id}.dpapi"
        if not cache.exists():
            return False
        try:
            saved = read_protected_json(cache)
            fetched = datetime.fromtimestamp(cache.stat().st_mtime, timezone.utc)
            age = (datetime.now(timezone.utc) - fetched).total_seconds()
            if saved.get("catalogue_signature") or saved["release"]["release_id"] != release_id or not 0 <= age <= METADATA_MAX_AGE_SECONDS:
                return False
            inspected = self.store.db.execute(
                "SELECT MAX(utc) FROM events WHERE source_id=? AND action='view' AND status='inspected'", (source_id,)).fetchone()[0]
            if not inspected or abs((datetime.fromisoformat(inspected) - fetched).total_seconds()) > 10:
                return False
            for (received,) in self.store.db.execute("SELECT received FROM messages WHERE source_id=?", (source_id,)):
                offered = parsedate_to_datetime(received)
                if (offered if offered.tzinfo else offered.replace(tzinfo=timezone.utc)) > fetched:
                    return False
            saved.update(fetched_at=fetched.isoformat(), catalogue_signature=signature, mail_marker=self._mail_marker(source_id))
            protect_json(cache, saved)
            return True
        except Exception:
            return False

    def prepare_planning(self, source_id, refresh=False, inspected_since=None):
        """Reuse a complete, current track inventory; all decisions still use today's local library.

        A coordinator already in flight may reuse its own freshly inspected legacy snapshot.
        Discovery never trusts a legacy snapshot without a catalogue signature.
        """
        row = self.store.db.execute("SELECT remote_id,metadata FROM sources WHERE id=? AND platform='promobox'",
                                    (source_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown PromoBox source")
        metadata = json.loads(row["metadata"])
        cache = self.store.root / f"promobox-{source_id}.dpapi"
        valid = False
        if cache.exists() and not refresh:
            try:
                saved = read_protected_json(cache)
                release = saved["release"]
                fetched = datetime.fromisoformat(saved["fetched_at"]) if saved.get("fetched_at") else datetime.fromtimestamp(
                    cache.stat().st_mtime, timezone.utc)
                age = (datetime.now(timezone.utc) - fetched).total_seconds()
                complete = (release["release_id"] == row["remote_id"] and release.get("label") and release.get("title")
                            and release.get("tracks") and all(
                                t.get("name") and t.get("track_id") and isinstance(t.get("files"), list)
                                and all(f.get("id") and f.get("type") in {"wav", "mp3"} for f in t["files"])
                                for t in release["tracks"]))
                signature = metadata.get("catalogue_signature")
                current = bool(signature) and saved.get("catalogue_signature") == signature
                same_run_legacy = (not signature and not saved.get("catalogue_signature") and inspected_since
                                   and fetched >= datetime.fromisoformat(inspected_since))
                mail_current = saved.get("mail_marker") == self._mail_marker(source_id)
                valid = bool(complete and 0 <= age <= METADATA_MAX_AGE_SECONDS and
                             ((current and mail_current) or same_run_legacy))
            except Exception:
                # A broken/unreadable protected cache is a reason to inspect, never to hide a release.
                valid = False
        if valid:
            return {"source": "cache"}
        self.snapshot(source_id, refresh=True)
        return {"source": "fresh"}

    def snapshot(self, source_id, refresh=False):
        row = self.store.db.execute("SELECT platform, remote_id FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None or row[0] != "promobox" or not row[1]:
            raise ValueError("Unknown PromoBox source; releases come from the promo list")
        cache = self.store.root / f"promobox-{source_id}.dpapi"
        if cache.exists() and not refresh:
            saved = read_protected_json(cache)
            self.session.cookies.update(saved["cookies"])
            return saved
        self.login()
        location = self.store.source_url(source_id)
        parsed = urlsplit(location)
        if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.path != "/promo":
            raise RuntimeError("Unexpected PromoBox release link")
        self.store.event(source_id, "view", "intent", platform="promobox",
                         platform_effect="Release inspection marks the promo viewed")
        self.store.sync_logs()
        for attempt in range(2):
            target = location
            for hop in range(5):  # the recipient link stores the release in the session, then redirects to /promo
                self.calls += 1
                response = self.session.get(target, allow_redirects=False, timeout=40)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                target = urljoin(target, response.headers.get("Location", ""))
                if urlsplit(target).scheme != "https" or urlsplit(target).hostname not in HOSTS:
                    raise RuntimeError("PromoBox redirected off-site")
                if urlsplit(target).path == "/login":
                    break
            if response.status_code == 200 and urlsplit(target).path != "/login":
                break
            if attempt == 0:
                self.login(force=True)  # session expired: the page bounces to the login form
                continue
            raise RuntimeError(f"PromoBox release HTTP {response.status_code}")
        release = parse_release(response.text)
        if release["release_id"] != row[1]:
            raise RuntimeError("PromoBox returned a different release")
        metadata = json.loads(self.store.db.execute("SELECT metadata FROM sources WHERE id=?", (source_id,)).fetchone()[0])
        saved = {"release": release, "cookies": self.session.cookies.get_dict(), "fetched_at": now(),
                 "catalogue_signature": metadata.get("catalogue_signature"), "mail_marker": self._mail_marker(source_id)}
        protect_json(cache, saved)
        self.store.event(source_id, "view", "inspected", platform="promobox", tracks=len(release["tracks"]),
                         gated=release["gated"], note="No audio, plays, flags, decline or feedback submitted")
        return saved

    def release(self, source_id):
        release = dict(self.snapshot(source_id)["release"])
        release["feedback_left"] = release.get("feedback_left") or bool(self.store.db.execute(
            "SELECT 1 FROM feedback WHERE source_id=? AND status='submitted'", (source_id,)).fetchone())
        return release

    @staticmethod
    def extension(file):
        return "." + file["type"]

    def _ajax(self, path, release, data):
        self.calls += 1
        return self.session.post(BASE + path, data=dict(data, _token=release["csrf"]), timeout=60, allow_redirects=False,
                                 headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-TOKEN": release["csrf"],
                                          "Referer": BASE + "/promo", "Accept": "*/*"})

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
        """Satisfy a feedback gate exactly once. An unknown outcome blocks further automatic retries. Never Not For Me."""
        row = self.choose_feedback(source_id, release)
        if row["status"] == "submitted":
            return
        if row["status"] != "pending":
            raise RuntimeError("Previous feedback submission has an uncertain outcome; inspect before retrying")
        release = self.snapshot(source_id)["release"]
        favourite = next((t for t in release["tracks"] if version_kind(t["name"]) == "extended"), release["tracks"][0])
        self.store.db.execute("UPDATE feedback SET status='submitting' WHERE source_id=?", (source_id,))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "intent", rating=row["rating"], scale="10", comment=row["comment"],
                         supporting=bool(row["supporting"]), favourite=favourite["name"],
                         platform_effect="unlocks downloads, visible to the label")
        self.store.sync_logs()
        response = self._ajax("/ajax/promo-download/submit-feedback", release, {
            "feedback": "sent", "dj": release["dj"], "id": release["download_token"], "name": release["name"],
            "email": release["email"], "support": "Yes" if row["supporting"] else "No", "djquote": row["comment"],
            "rating": row["rating"], "bestmix": favourite["track_id"], "require_name": "0", "not_for_me": "0"})
        text = response.text.strip()
        if response.status_code != 200 or not text or text.startswith(("<", "{")):
            self.store.event(source_id, "feedback", "uncertain", http=response.status_code, answer=text[:120])
            raise RuntimeError(f"Feedback answered HTTP {response.status_code} without a receipt; inspect before retrying")
        self.store.db.execute("UPDATE feedback SET status='submitted',submitted_at=? WHERE source_id=?", (now(), source_id))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "submitted", rating=row["rating"], receipt=text[:80])
        self.store.sync_logs()
        if self.snapshot(source_id, refresh=True)["release"]["gated"]:
            # The page keeps force_feedback=1 after feedback; downloads are judged by the options call instead.
            self.store.event(source_id, "feedback", "page_still_flagged", note="download options decide access")

    def download(self, source_id, file, destination):
        release = self.snapshot(source_id)["release"]
        if file["type"] not in {"wav", "mp3"}:
            raise RuntimeError("Unexpected PromoBox original format")
        options = self._ajax(f"/ajax/promo-download/get-download-options/{release['release_id']}/{file['id']}/{release['dj']}",
                             release, {"format": file["type"], "download_all": "0", "promo_page": "1", "platform": ""})
        if options.status_code != 200 or not options.text.strip():
            raise RuntimeError(f"PromoBox refused download options (HTTP {options.status_code}); feedback gate still locked?")
        location = BASE + "/download-" + file["type"] + "/" + "/".join(
            [release["dj"], release["release_id"], file["id"], release["download_token"]])
        self.store.event(source_id, "platform_download", "intent", track_id=file["id"],
                         platform_effect="Original download may mark track downloaded")
        for _ in range(4):
            self.calls += 1
            response = self.session.get(location, allow_redirects=False, timeout=900, stream=True,
                                        headers={"Referer": BASE + "/promo"})
            if response.status_code in {301, 302, 303, 307, 308}:
                response.close()
                location = urljoin(location, response.headers.get("Location", ""))
                if urlsplit(location).scheme != "https":
                    raise RuntimeError("PromoBox download redirected off HTTPS; not following")
                continue
            break
        else:
            raise RuntimeError("Too many PromoBox download redirects")
        try:
            if response.status_code != 200:
                raise RuntimeError(f"PromoBox original HTTP {response.status_code}")
            if any(t in response.headers.get("Content-Type", "").lower() for t in ("html", "json")):
                raise RuntimeError("PromoBox returned a page instead of original audio")
            self.store.event(source_id, "platform_download", "host", host=urlsplit(location).hostname)
            return write_stream(response.iter_content(1 << 20), destination,
                                int(response.headers.get("Content-Length") or 0) or None, tolerance=0)
        finally:
            response.close()

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30):
        """Every release on the promo list sent within the age window, oldest first. The library check skips owned mixes."""
        library = LibraryIndex(self.store)
        today = datetime.now(timezone.utc)
        tiles = self.catalogue()
        recent = [t for t in tiles if t["sent"] and (today - t["sent"]).total_seconds() / 86400 <= max_age_days]
        report = []
        for tile in sorted(recent, key=lambda t: t["sent"]):
            name = f"{tile['label']}: {tile['artist']} - {tile['title']}"
            try:
                summary = self.collect_incremental(tile["source_id"], library, dry_run=dry_run, allow_gate=allow_gate)
                report.append({"release": summary.get("release") or name, "tracks": summary["tracks"]})
            except Exception as error:
                self.store.event(tile["source_id"], "batch", "release_failed", release=name, error=str(error)[:200])
                report.append({"release": name, "result": "failed: " + str(error)[:160]})
        self.store.sync_logs(force=True)
        return {"listed": len(tiles), "in_window": len(recent), "releases": report}

    def collect_incremental(self, source_id, library, dry_run=False, allow_gate=False, refresh=False):
        self.prepare_planning(source_id, refresh=refresh)
        plan = collect_release(self.store, self, source_id, library, dry_run=True)
        if dry_run or not any(t.get("action") in {"download", "replace", "resume"} for t in plan["tracks"]):
            return plan
        self.snapshot(source_id, refresh=True)
        return collect_release(self.store, self, source_id, library, allow_gate=allow_gate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="decide, download, process and deliver one release")
    collect.add_argument("source_id")
    batch = commands.add_parser("batch", help="every release on the PromoBox promo list within the age window")
    batch.add_argument("--max-age-days", type=int, default=30)
    for command in (collect, batch):
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--allow-feedback", action="store_true", help="submit rating feedback to unlock gated releases")
    collect.add_argument("--refresh-metadata", action="store_true", help="inspect the release even if its cached track list is current")
    args = parser.parse_args()
    store = Store()
    try:
        adapter = PromoBox(store)
        if args.command == "collect":
            result = adapter.collect_incremental(args.source_id, LibraryIndex(store),
                                                dry_run=args.dry_run, allow_gate=args.allow_feedback,
                                                refresh=args.refresh_metadata)
        else:
            result = adapter.batch(dry_run=args.dry_run, allow_gate=args.allow_feedback, max_age_days=args.max_age_days)
        store.sync_logs(force=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
