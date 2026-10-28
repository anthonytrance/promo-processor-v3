"""Promo Cloud / Fusion API adapter. Uses observed site contracts, never Previous/Next UI."""
from __future__ import annotations

import argparse
import email
import email.utils
from datetime import datetime, timezone
import json
import random
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import keyring
import win32crypt
from bs4 import BeautifulSoup
from curl_cffi import requests

from promo_automation import ACCOUNT, CREDENTIAL_SERVICE, STATE, Store, email_skill, now
from promo_pipeline import LibraryIndex, collect_release, label_from_subject, label_is_artist, write_stream

BASE = "https://www.promo-cloud.com"
SERVICE = CREDENTIAL_SERVICE
RATING_RANGE = (7, 9)
# Generic thanks only. Never claim to have listened, played, charted or supported anything.
FEEDBACK_OPENERS = ["Thanks for sending this over", "Appreciate the promo", "Thank you for the package",
                    "Received, thanks for including us", "Cheers for the promo", "Thanks for thinking of us"]
FEEDBACK_CLOSERS = ["wishing the release all the best.", "good luck with the release.", "all the best with it.",
                    "hope it does well for you.", "best of luck with the release.", "wishing you a great release."]


def protect_json(path, data):
    path.write_bytes(win32crypt.CryptProtectData(json.dumps(data, ensure_ascii=False).encode(),
                                               "promo cloud state", None, None, None, 0))


def read_protected_json(path):
    return json.loads(win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)[1])


def catalogue_label(catalogue):
    """Label for a catalogue number from label_codes.csv ('TCR077' -> Technoclub Retro), or None."""
    from pathlib import Path
    from process_promos import load_label_codes
    prefix = re.match(r"[A-Za-z]+", catalogue or "")
    codes = load_label_codes(str(Path(__file__).with_name("label_codes.csv")))
    return codes.get(prefix.group(0).upper()) if prefix else None


class PromoCloud:
    def __init__(self, store):
        self.store = store
        self.calls = 0
        self.token = keyring.get_password(SERVICE, "promo-cloud:fusion-token")
        self.session = requests.Session(impersonate="chrome", headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE, "Referer": BASE + "/TargetedPromo/view",
        })
        if self.token:
            self.session.cookies.set("FusionToken", self.token, domain="www.promo-cloud.com", path="/")

    def request(self, method, path, **kwargs):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Only this platform's relative endpoints are allowed")
        self.calls += 1
        try:
            response = self.session.request(method, BASE + path, timeout=35, **kwargs)
        except Exception as error:
            raise RuntimeError("Platform request failed: " + type(error).__name__) from None
        if response.status_code != 200:
            raise RuntimeError(f"Platform HTTP {response.status_code} at {path}")
        return response

    def api(self, path, **kwargs):
        response = self.request("GET", path, params=kwargs)
        try:
            return response.json()
        except Exception:
            raise RuntimeError("Platform returned a non-JSON API response at " + path) from None

    def login(self):
        password = keyring.get_password(SERVICE, "promo-cloud:" + ACCOUNT)
        if not password:
            raise RuntimeError("Provision the platform credential from 1Password first")
        # The observed ASMX contract takes integer rememberMe, not a JSON boolean.
        result = self.request("POST", "/fusion/api/fusionMembership.asmx/signInForm",
                              data=json.dumps({"email": ACCOUNT, "pass": password, "rememberMe": 0})).json()["d"]
        if result.get("responseCode") != 1:
            raise RuntimeError("Platform login was rejected; no automatic password retry")
        self.token = result["responseString"]
        keyring.set_password(SERVICE, "promo-cloud:fusion-token", self.token)
        self.session.cookies.set("FusionToken", self.token, domain="www.promo-cloud.com", path="/")
        self.store.event(None, "platform_login", "verified", platform="promo-cloud", method="HTTP API")

    def catalogue(self):
        if not self.token:
            raise RuntimeError("No verified Fusion session; run login once")
        rows = self.api("/api2/TargetedPromotion/CheckFusionPromos/", FusionToken=self.token)
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected catalogue schema")
        seen = set()
        for row in rows:
            promotion, contact = row["_promotionID"], row["_contactID"]
            if promotion in seen:
                raise RuntimeError("Duplicate promotion ID in catalogue; completeness needs investigation")
            seen.add(promotion)
            url = BASE + "/TargetedPromo/view?" + urlencode({"promo": promotion, "contact": contact})
            sid = self.store.add_source("promo-cloud", row["_releaseTitle"], url)
            metadata = {"label": row.get("_LabelName"), "catalogue": row.get("_releaseCode"), "release_title": row.get("_releaseTitle"),
                        "track_count": row.get("_totalTracks"), "opened": row.get("_opened"),
                        "withdrawn": row.get("_withdrawn"), "expires": row.get("_endDate"),
                        "added": row.get("_dateAdded"), "catalogue_seen": True}
            prior = json.loads(self.store.db.execute("SELECT metadata FROM sources WHERE id=?", (sid,)).fetchone()[0])
            prior.update(metadata)
            self.store.db.execute("UPDATE sources SET metadata=?,subject=? WHERE id=?", (json.dumps(prior), row["_releaseTitle"], sid))
        self.store.db.commit()
        protect_json(self.store.root / "promo-cloud-catalogue.dpapi", rows)
        self.store.event(None, "catalogue", "complete", platform="promo-cloud", releases=len(rows), requests=1,
                         note="Explicit returned IDs; completeness reconciled separately with email links")
        return {"catalogue_releases": len(rows), "http_requests": self.calls}

    def discover_mail(self, since):
        skill = email_skill()
        connection = skill.imap_login(ACCOUNT)
        seen, messages = set(), 0
        try:
            skill._select(connection, "INBOX", readonly=True)
            validity = connection.response("UIDVALIDITY")[1][0].decode()
            typ, data = connection.uid("search", None, "SINCE", skill.parse_date_arg(since),
                                       "FROM", '"promo-cloud.com"')
            skill._require_imap_ok("platform mail search", typ, data)
            uids = data[0].split()
            for offset in range(0, len(uids), 25):
                typ, data = connection.uid("fetch", b",".join(uids[offset:offset + 25]), "(BODY.PEEK[])")
                skill._require_imap_ok("platform mail fetch", typ, data)
                for item in data:
                    if not isinstance(item, tuple):
                        continue
                    uid = re.search(rb"UID (\d+)", item[0]).group(1).decode()
                    message = email.message_from_bytes(item[1])
                    subject = skill.decode_str(message.get("Subject", ""))
                    links = set()
                    for part in message.walk():
                        if part.get_content_type() != "text/html":
                            continue
                        html = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
                        for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
                            parsed = urlsplit(anchor["href"])
                            query = parse_qs(parsed.query)
                            if (parsed.scheme == "https" and parsed.hostname in {"www.promo-cloud.com", "promo-cloud.com"}
                                    and parsed.path.lower() == "/targetedpromo/view" and query.get("promo") and query.get("contact")):
                                links.add(anchor["href"])
                    for url in links:
                        sid = self.store.add_source("promo-cloud", subject, url, email_uid=uid)
                        self.store.db.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?)",
                                              (ACCOUNT, "INBOX", validity, uid, message.get("Message-ID", ""),
                                               subject, message.get("Date", ""), sid))
                        seen.add(sid)
                    messages += 1
            self.store.db.commit()
        finally:
            connection.logout()
        catalogue_only = self.store.db.execute("""SELECT count(*) FROM sources s WHERE platform='promo-cloud'
            AND NOT EXISTS(SELECT 1 FROM messages m WHERE m.source_id=s.id)""").fetchone()[0]
        email_only = self.store.db.execute("""SELECT count(*) FROM sources WHERE platform='promo-cloud'
            AND json_extract(metadata,'$.catalogue_seen') IS NOT 1""").fetchone()[0]
        self.store.event(None, "mail_discovery", "complete", platform="promo-cloud", messages=messages,
                         releases=len(seen), catalogue_only=catalogue_only, email_only=email_only, since=since)
        return {"messages": messages, "email_releases": len(seen), "catalogue_only": catalogue_only,
                "email_only": email_only}

    def _scope(self, source_id):
        source = self.store.db.execute("SELECT * FROM sources WHERE id=? AND platform='promo-cloud'", (source_id,)).fetchone()
        if not source:
            raise ValueError("Unknown Promo Cloud source")
        parsed = urlsplit(self.store.source_url(source_id))
        if parsed.hostname not in {"www.promo-cloud.com", "promo-cloud.com"} or parsed.path.lower() != "/targetedpromo/view":
            raise ValueError("Refusing a non-promo source link")
        query = parse_qs(parsed.query)
        return source, query["promo"][0], query["contact"][0]

    def snapshot(self, source_id, refresh=False):
        """Viewer flags plus track list. A saved snapshot avoids a repeat page view, which the platform may count."""
        path = self.store.root / ("promo-cloud-" + source_id + ".dpapi")
        if path.exists() and not refresh:
            return read_protected_json(path)
        source, promotion, contact = self._scope(source_id)
        self.store.event(source_id, "view", "intent", platform_read_status="may become opened")
        self.store.sync_logs()
        try:
            page = self.request("GET", "/TargetedPromo/view", params={"promo": promotion, "contact": contact})
            soup = BeautifulSoup(page.text, "html.parser")
            flags = {}
            for flag in ["lockTracks", "feedbackLeft", "isFusion", "isFusionSignedIn"]:
                match = re.search(r"var\s+" + flag + r"\s*=\s*(true|false)\s*;", page.text)
                if match is None:
                    raise RuntimeError("Viewer schema changed: missing " + flag)
                flags[flag] = match.group(1) == "true"
            tracks = self.api("/api2/TargetedPromotion/GetTracks/", PromoKey=promotion, ContactKey=contact)
            if not isinstance(tracks, list):
                raise RuntimeError("Unexpected tracks schema")
            snapshot = {"source_id": source_id, "promotion": promotion, "contact": contact,
                        "flags": flags, "tracks": tracks, "title": soup.title.get_text() if soup.title else source["subject"]}
            protect_json(path, snapshot)
            self.store.event(source_id, "view", "inspected", platform_read_status="viewer requested",
                             platform_download_status="no download requested", tracks=len(tracks), **flags)
            self.store.sync_logs()
            return snapshot
        except Exception:
            self.store.event(source_id, "view", "uncertain", note="Inspect saved/browser state before any retry")
            self.store.sync_logs()
            raise

    def inspect(self, source_id):
        snapshot = self.snapshot(source_id)
        return {"source_id": source_id, "title": snapshot["title"].strip(), "flags": snapshot["flags"],
                "tracks": [{k: row.get(k) for k in ["_TrackTitle", "_trackDurationS"]}
                           | {"files": [{k: f.get(k) for k in ["_FileTypeName", "_FileSizeKB"]} for f in row.get("_files", [])]}
                           for row in snapshot["tracks"]],
                "http_requests": self.calls}

    def release(self, source_id):
        """Normalize this platform's contract for the shared pipeline."""
        source, promotion, contact = self._scope(source_id)
        snapshot = self.snapshot(source_id)
        subject_label, catalogue = label_from_subject(source["subject"])
        label = json.loads(source["metadata"]).get("label") or subject_label
        tracks = [{"name": row["_TrackTitle"].strip(), "artist": row.get("_trackArtist"), "title": row.get("_trackTiltleOnly"),
                   "mix": row.get("_trackVersion"), "duration": row.get("_trackDurationS"), "pack_id": row.get("_TrackPackId"),
                   "files": [{"id": f["_FileGUID"], "type": f["_FileTypeName"], "size_kb": f.get("_FileSizeKB")}
                             for f in row.get("_files", [])]} for row in snapshot["tracks"]]
        if any(label_is_artist(label, t) for t in tracks):
            # 2026-10-28: the subject names the sender ('Talla 2XLC [TTR080A] : Talla 2XLC, YORK ...'), and an artist
            # who sends their own promos is not the imprint. The catalogue prefix may name it; otherwise research does.
            label = catalogue_label(catalogue)
        title = re.sub(r"^\s*Music Promo:\s*", "", snapshot["title"]).strip()
        return {"title": title, "label": label, "gated": snapshot["flags"]["lockTracks"],
                "feedback_left": snapshot["flags"]["feedbackLeft"], "tracks": tracks}

    @staticmethod
    def extension(file):
        name = file["type"].lower()
        if name == "wav":
            return ".wav"
        if name == "mp3":
            return ".mp3"
        raise RuntimeError("Unsupported file type from platform: " + file["type"])

    def download(self, source_id, file, destination):
        source, promotion, contact = self._scope(source_id)
        self.calls += 1
        try:
            response = self.session.request("GET", BASE + "/download", stream=True, timeout=900,
                                            params={"promo": promotion, "contact": contact, "file": file["id"], "v": 1},
                                            headers={"Accept": "*/*", "Referer": BASE + "/TargetedPromo/view"})
        except Exception as error:
            raise RuntimeError("Platform download failed: " + type(error).__name__) from None
        try:
            if response.status_code != 200:
                raise RuntimeError(f"Platform HTTP {response.status_code} on download")
            kind = (response.headers.get("Content-Type") or "").lower()
            if "html" in kind or "json" in kind:
                raise RuntimeError("Platform answered the download with a page instead of audio")
            expected = (file.get("size_kb") or 0) * 1024
            return write_stream(response.iter_content(1 << 20), destination, expected or None)
        finally:
            response.close()

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
        """Satisfy a feedback gate exactly once. An unknown outcome blocks further automatic retries."""
        source, promotion, contact = self._scope(source_id)
        row = self.choose_feedback(source_id, release)
        if row["status"] == "submitted":
            return
        if row["status"] != "pending":
            raise RuntimeError("Previous feedback submission has an uncertain outcome; inspect before retrying")
        favourite = release["tracks"][0]["pack_id"] if release["tracks"] else 0
        payload = {"_ContactID": contact, "_PromotionGuid": promotion, "_Rating": row["rating"], "_Comments": row["comment"],
                   "_FavoriteTrackInstanceID": favourite, "_Supporting": bool(row["supporting"])}
        self.store.db.execute("UPDATE feedback SET status='submitting' WHERE source_id=?", (source_id,))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "intent", rating=row["rating"], comment=row["comment"],
                         supporting=bool(row["supporting"]), platform_effect="unlocks downloads, visible to the label")
        self.store.sync_logs()
        try:
            result = self.request("POST", "/api2/TargetedPromotion/CollectFeedback", data=json.dumps(payload)).json()
        except RuntimeError as error:
            if "HTTP 409" not in str(error):
                raise
            # Conflict means the platform already holds feedback for this release (for example from the browser).
            result = {"responseCode": 1, "note": "platform already had feedback"}
        if result.get("responseCode") != 1:
            self.store.db.execute("UPDATE feedback SET status='rejected' WHERE source_id=?", (source_id,))
            self.store.db.commit()
            self.store.event(source_id, "feedback", "rejected", response_code=result.get("responseCode"))
            raise RuntimeError("Platform rejected the feedback")
        self.store.db.execute("UPDATE feedback SET status='submitted',submitted_at=? WHERE source_id=?", (now(), source_id))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "submitted", rating=row["rating"])
        self.store.sync_logs()

    def newest_offer_age_days(self, source_id):
        """Days since the most recent email about this release, or None when no dated email is known."""
        ages = []
        for (received,) in self.store.db.execute("SELECT received FROM messages WHERE source_id=?", (source_id,)):
            try:
                moment = email.utils.parsedate_to_datetime(received.split(' (')[0])
                ages.append((datetime.now(timezone.utc) - moment).total_seconds() / 86400)
            except (TypeError, ValueError):
                continue
        if not ages:
            row = self.store.db.execute("SELECT metadata FROM sources WHERE id=?", (source_id,)).fetchone()
            added = json.loads(row[0]).get("added") if row else None
            try:
                moment = datetime.fromisoformat(added.replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                ages.append((datetime.now(timezone.utc) - moment).total_seconds() / 86400)
            except (AttributeError, TypeError, ValueError):
                pass
        return min(ages) if ages else None

    def batch(self, dry_run=False, allow_gate=False, pause=0, max_age_days=30):
        """Refresh catalogue and email offers, then decide per mix. Errors stop one release only."""
        self.catalogue()
        self.discover_mail(f"{max_age_days}d")
        library = LibraryIndex(self.store)
        rows = self.store.db.execute("""SELECT id, subject FROM sources WHERE platform='promo-cloud'
            ORDER BY json_extract(metadata,'$.expires')""").fetchall()
        report = []
        for sid, subject in rows:
            age = self.newest_offer_age_days(sid)
            if age is None or age > max_age_days:
                self.store.event(sid, "select", "skipped_too_old", release=subject, age_days=round(age) if age is not None else None,
                                 note="Newest offer is older than the age limit; not opened")
                report.append({"release": subject, "result": "undated or too old, not opened"})
                continue
            try:
                summary = collect_release(self.store, self, sid, library, dry_run=dry_run, allow_gate=allow_gate)
                report.append({"release": subject, "tracks": summary["tracks"]})
            except Exception as error:
                self.store.event(sid, "batch", "release_failed", release=subject, error=str(error)[:200])
                report.append({"release": subject, "result": "failed: " + str(error)[:160]})
            time.sleep(pause)
        self.store.sync_logs(force=True)
        return report

    def collect(self, source_id, dry_run=False, allow_gate=False):
        return collect_release(self.store, self, source_id, LibraryIndex(self.store), dry_run=dry_run, allow_gate=allow_gate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login")
    commands.add_parser("catalogue")
    discover = commands.add_parser("discover-mail")
    discover.add_argument("--since", default="30d")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("source_id")
    collect = commands.add_parser("collect", help="decide, download, process and deliver one release")
    collect.add_argument("source_id")
    collect.add_argument("--dry-run", action="store_true")
    collect.add_argument("--allow-feedback", action="store_true", help="permit the one-time feedback that unlocks downloads")
    batch = commands.add_parser("batch", help="collect every catalogue release not already in the library")
    batch.add_argument("--dry-run", action="store_true")
    batch.add_argument("--allow-feedback", action="store_true")
    batch.add_argument("--max-age-days", type=int, default=30, help="skip releases whose newest offer is older")
    retry = commands.add_parser("retry", help="deliberately reopen one uncertain asset")
    retry.add_argument("asset_key")
    retry.add_argument("--reason", required=True)
    args = parser.parse_args()
    store = Store()
    adapter = PromoCloud(store)
    if args.command == "login":
        adapter.login()
        result = {"login": "verified", "http_requests": adapter.calls}
    elif args.command == "catalogue":
        result = adapter.catalogue()
    elif args.command == "discover-mail":
        result = adapter.discover_mail(args.since)
    elif args.command == "collect":
        result = adapter.collect(args.source_id, dry_run=args.dry_run, allow_gate=args.allow_feedback)
        store.sync_logs(force=True)
    elif args.command == "batch":
        result = adapter.batch(dry_run=args.dry_run, allow_gate=args.allow_feedback, max_age_days=args.max_age_days)
        result = {"releases": result, "upgrade_candidates_ask_user": [t["track"] for r in result for t in r.get("tracks", []) if t.get("action") == "upgrade_candidate"]}
    elif args.command == "retry":
        store.release_claim(args.asset_key, args.reason)
        result = {"released": args.asset_key}
    else:
        result = adapter.inspect(args.source_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
