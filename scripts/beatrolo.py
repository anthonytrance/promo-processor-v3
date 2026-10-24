"""BeatRolo recipient promos, through the website's JSON API. No previews are played.

Mailed /p/<token>?r=<recipient> links supply recipient metadata directly. A saved
portal session can additionally enumerate the complete inbox. Feedback and original
downloads use the same endpoints as the website and the shared collection pipeline.
"""
import argparse
import json
import random
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from curl_cffi import requests

from promo_automation import ACCOUNT, Store, now
from promo_cloud import FEEDBACK_CLOSERS, FEEDBACK_OPENERS, protect_json, read_protected_json
from promo_mail import discover, newest_offer_age
from promo_pipeline import LibraryIndex, collect_release, write_stream

BASE = "https://www.beatrolo.com"
HOSTS = {"beatrolo.com", "www.beatrolo.com"}
AUDIO = {"wav", "aif", "aiff", "flac", "mp3", "m4a", "ogg"}
REDIRECTS = {301, 302, 303, 307, 308}


def secure_host(parsed, hosts=HOSTS):
    try:
        return (parsed.scheme == "https" and parsed.hostname in hosts and parsed.port in (None, 443)
                and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


def recipient_link(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    match = re.fullmatch(r"/p/([A-Za-z0-9_-]+)/?", parsed.path)
    recipient = parse_qs(parsed.query).get("r", [None])[0]
    if not secure_host(parsed) or not match or not recipient:
        return None
    return match.group(1), recipient


def original_storage_url(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (secure_host(parsed, {"www.eburgmedia.com", "eburgmedia.com"})
            and parsed.path == "/beatrolo/download_audio.php" and bool(parse_qs(parsed.query).get("token", [""])[0]))


def date_age(value):
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - moment).total_seconds() / 86400
    except (ValueError, AttributeError, TypeError):
        return None


def normalize_release(data):
    if not isinstance(data, dict) or not data.get("id") or not data.get("title"):
        raise RuntimeError("BeatRolo recipient schema changed or promo expired")
    label = data.get("recordLabel") or data.get("labelName")
    artist = (data.get("primaryArtistName") or "").strip()
    tracks = []
    for item in data.get("tracks") or []:
        title = (item.get("title") or "").strip()
        if not item.get("id") or not title:
            raise RuntimeError("BeatRolo track lacks its stable id or title")
        name = title if " - " in title else f"{artist} - {title}" if artist else title
        track_artist, sep, track_title = name.partition(" - ")
        kind = str(item.get("audioFormat") or "original").lower()
        if kind not in AUDIO | {"original"}:
            raise RuntimeError("BeatRolo offers an unrecognised original audio format")
        tracks.append({"name": name, "artist": track_artist if sep else artist or None,
                       "title": track_title if sep else title, "label": label,
                       "duration": item.get("duration") or (item.get("durationMs") or 0) / 1000 or None,
                       "files": [{"id": str(item["id"]), "type": "aiff" if kind == "aif" else kind,
                                  "route": "track", "bytes": item.get("fileSize")} ]})
    # Legacy uploads have one original outside the newer tracks array.
    if not tracks and data.get("hasDownload"):
        title = data["title"]
        mix = data.get("mixTitle")
        if mix and mix.lower() not in title.lower():
            title += f" ({mix})"
        kind = str(data.get("audioFormat") or "original").lower()
        if kind not in AUDIO | {"original"}:
            raise RuntimeError("BeatRolo legacy original format is unknown")
        tracks.append({"name": f"{artist} - {title}" if artist else title, "artist": artist or None,
                       "title": title, "label": label, "duration": (data.get("audioDurationMs") or 0) / 1000 or None,
                       "files": [{"id": "legacy", "type": "aiff" if kind == "aif" else kind, "route": "legacy"}]})
    # A tracklist may expose independent WAV and MP3 originals. Do not silently omit it.
    for section_index, section in enumerate(data.get("tracklist") or []):
        entries = section.get("tracks", []) if isinstance(section, dict) else section if isinstance(section, list) else []
        for track_index, item in enumerate(entries):
            files = [{"id": f"{section_index}-{track_index}-{kind}", "type": kind, "route": "tracklist",
                      "index": f"{section_index}-{track_index}"}
                     for key, kind in (("downloadWav", "wav"), ("downloadMp3", "mp3")) if item.get(key)]
            if not files:
                continue
            who = (item.get("artist") or artist).strip()
            title = (item.get("track") or item.get("title") or "").strip()
            if not title:
                raise RuntimeError("BeatRolo downloadable tracklist entry has no title")
            name = f"{who} - {title}" if who else title
            if any(t["name"] == name for t in tracks):
                raise RuntimeError("BeatRolo duplicates a downloadable mix across track schemas; inspect before collecting")
            tracks.append({"name": name, "artist": who or None, "title": title, "label": label,
                           "duration": item.get("duration"), "files": files})
    if not tracks:
        raise RuntimeError("BeatRolo promo has no downloadable original tracks")
    recipient = data.get("recipient") or {}
    return {"title": data["title"], "label": label, "gated": not bool(recipient.get("hasSubmittedFeedback")),
            "release_id": data["id"], "tracks": tracks, "expires_at": data.get("publicExpiresAt")}


class BeatRolo:
    platform = "beatrolo"

    def __init__(self, store):
        self.store, self.calls = store, 0
        self.session = requests.Session(impersonate="chrome124")
        self.session_path = store.root / "beatrolo-session.dpapi"
        if self.session_path.exists():
            self.session.cookies.update(read_protected_json(self.session_path).get("cookies", {}))

    def _request(self, method, url, **kwargs):
        url = urljoin(BASE, url)
        parsed = urlsplit(url)
        if not secure_host(parsed):
            raise RuntimeError("BeatRolo API request left its verified host")
        self.calls += 1
        try:
            return self.session.request(method, url, timeout=kwargs.pop("timeout", 60), allow_redirects=False, **kwargs)
        except Exception:
            raise RuntimeError("BeatRolo request failed; protected request details suppressed") from None

    def _source(self, source_id):
        row = self.store.db.execute("SELECT 1 FROM sources WHERE id=? AND platform=?", (source_id, self.platform)).fetchone()
        target = recipient_link(self.store.source_url(source_id)) if row else None
        if not target:
            raise ValueError("Not a known BeatRolo recipient promo")
        token, recipient = target
        return f"/api/promos/public/{quote(token, safe='')}", recipient

    def snapshot(self, source_id, refresh=False):
        path, recipient = self._source(source_id)
        cache = self.store.root / f"beatrolo-{source_id}.dpapi"
        if cache.exists() and not refresh:
            return read_protected_json(cache)
        self.store.event(source_id, "view", "intent", platform=self.platform,
                         platform_effect="recipient metadata GET only; website view/play tracking POSTs omitted")
        self.store.sync_logs()
        response = self._request("GET", path, params={"r": recipient})
        try:
            if response.status_code != 200:
                raise RuntimeError(f"BeatRolo recipient metadata HTTP {response.status_code}")
            data = response.json()
        finally:
            response.close()
        email = (data.get("recipient") or {}).get("email")
        if not email or email.casefold() != ACCOUNT.casefold():
            raise RuntimeError("BeatRolo recipient mailbox does not match configured promo mailbox")
        release = normalize_release(data)
        if release["expires_at"] and (date_age(release["expires_at"]) or 0) > 0:
            raise RuntimeError("BeatRolo explicitly expired this recipient promo")
        protect_json(cache, data)
        self.store.event(source_id, "view", "inspected", platform=self.platform, tracks=len(release["tracks"]),
                         note="Metadata only; no audio, feedback or tracking POST")
        return data

    def release(self, source_id):
        return normalize_release(self.snapshot(source_id))

    def login(self, credential_file):
        """Use an explicitly supplied DPAPI JSON {email,password}; never print credentials."""
        creds = read_protected_json(credential_file)
        if creds.get("email", "").casefold() != ACCOUNT.casefold():
            raise RuntimeError("BeatRolo login mailbox does not match configured mailbox")
        self.store.event(None, "login", "intent", platform=self.platform, platform_effect="creates recipient portal session")
        self.store.sync_logs()
        response = self._request("POST", "/api/portal/login", json={"email": creds["email"], "password": creds["password"],
                                                                  "rememberMe": True})
        try:
            if response.status_code != 200:
                raise RuntimeError(f"BeatRolo portal login HTTP {response.status_code}")
        finally:
            response.close()
        protect_json(self.session_path, {"cookies": dict(self.session.cookies)})
        self.store.event(None, "login", "confirmed", platform=self.platform)
        return {"logged_in": True}

    def discover_catalogue(self):
        if not self.session_path.exists():
            return {"available": False, "reason": "No saved recipient portal session; mail recipient links remain usable", "sources": []}
        self.store.event(None, "catalogue", "intent", platform=self.platform, platform_effect="read recipient promo inbox only")
        self.store.sync_logs()
        response = self._request("GET", "/api/portal/inbox")
        try:
            if response.status_code != 200:
                raise RuntimeError(f"BeatRolo portal inbox HTTP {response.status_code}; recipient login needs refresh")
            rows = response.json()
        finally:
            response.close()
        if not isinstance(rows, list):
            raise RuntimeError("BeatRolo portal inbox schema changed")
        protect_json(self.store.root / "beatrolo-inbox.dpapi", rows)
        sources, unavailable = [], 0
        for row in rows:
            token, recipient = row.get("publicToken") or row.get("token"), row.get("recipientToken")
            if not token or not recipient:
                unavailable += 1
                continue
            # Reuse a mailed source, preserving its message dates and once-only feedback state.
            remote = str(row.get("promoId") or token)
            existing = self.store.db.execute("SELECT id FROM sources WHERE platform=? AND remote_id=?", (self.platform, remote)).fetchone()
            if not existing:
                for saved in self.store.db.execute("SELECT id FROM sources WHERE platform=? ORDER BY discovered_at,id", (self.platform,)):
                    target = recipient_link(self.store.source_url(saved[0]))
                    if target and target[0] == token:
                        self.store.db.execute("UPDATE sources SET remote_id=? WHERE id=?", (remote, saved[0]))
                        self.store.db.commit()
                        break
            sid = self.store.add_source(self.platform, row.get("episodeTitle") or row.get("title") or "BeatRolo promo",
                                        BASE + "/p/" + quote(token, safe="") + "?" + urlencode({"r": recipient}),
                                        remote_id=remote, offered_at=row.get("sentAt"))
            old = self.store.db.execute("SELECT metadata FROM sources WHERE id=?", (sid,)).fetchone()
            metadata = json.loads(old[0]); metadata.update(offered_at=row.get("sentAt"),
                catalogue_title=row.get("episodeTitle") or row.get("title"), catalogue_updated=row.get("updatedAt"))
            self.store.db.execute("UPDATE sources SET metadata=? WHERE id=?", (json.dumps(metadata), sid))
            self.store.db.commit()
            sources.append(sid)
        self.store.event(None, "catalogue", "inspected", platform=self.platform, total=len(rows),
                         added=len(sources), missing_recipient_links=unavailable)
        return {"available": True, "total": len(rows), "sources": sources, "missing_recipient_links": unavailable}

    @staticmethod
    def extension(file):
        return "." + (file["type"] if file["type"] != "original" else "wav")

    def unlock(self, source_id, release):
        # The pipeline calls this only after checking explicit feedback permission.
        previous = self.store.db.execute("SELECT source_id FROM events WHERE action='feedback' AND status='intent' "
                                         "AND json_extract(details,'$.beatrolo_release')=? ORDER BY sequence LIMIT 1",
                                         (release["release_id"],)).fetchone()
        feedback_source = previous[0] if previous else source_id
        row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (feedback_source,)).fetchone()
        if row and row["status"] == "submitted":
            return
        if row and row["status"] != "pending":
            raise RuntimeError("BeatRolo feedback outcome is uncertain; inspect before retrying")
        if not row:
            rng = random.Random(source_id)
            comment = rng.choice(FEEDBACK_OPENERS) + ", " + rng.choice(FEEDBACK_CLOSERS)
            self.store.db.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?,NULL)",
                                  (feedback_source, rng.randint(7, 9), comment, 0, "pending", now()))
            self.store.db.commit()
            row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (feedback_source,)).fetchone()
        path, recipient = self._source(source_id)
        self.store.db.execute("UPDATE feedback SET status='submitting' WHERE source_id=?", (feedback_source,))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "intent", platform=self.platform, comment=row["comment"], rating=row["rating"],
                         beatrolo_release=release["release_id"],
                         platform_effect="one stock thanks review, no support or would-play claim")
        self.store.sync_logs()
        data = self.snapshot(source_id)
        who = data.get("recipient") or {}
        response = self._request("POST", path + "/review", json={"rating": row["rating"], "feedback": row["comment"],
                                 "reviewerName": who.get("name"), "reviewerEmail": who.get("email"), "recipientToken": recipient})
        try:
            if response.status_code not in {200, 201}:
                raise RuntimeError(f"BeatRolo feedback HTTP {response.status_code}; inspect before retrying")
            try:
                result = response.json()
            except Exception:
                protect_json(self.store.root / f"beatrolo-feedback-{source_id}.dpapi",
                             {"status": response.status_code, "body": getattr(response, "text", "")})
                raise RuntimeError("BeatRolo feedback did not return valid JSON; inspect before retrying") from None
            protect_json(self.store.root / f"beatrolo-feedback-{source_id}.dpapi", {"status": response.status_code, "json": result})
            if (not isinstance(result, dict) or result.get("success") is not True or not result.get("reviewId")
                    or result.get("error") or result.get("errors")
                    or "html" in response.headers.get("Content-Type", "").lower()):
                raise RuntimeError("BeatRolo feedback returned an error or unconfirmed body; inspect before retrying")
        finally:
            response.close()
        self.store.db.execute("UPDATE feedback SET status='submitted',submitted_at=? WHERE source_id=?", (now(), feedback_source))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "submitted", platform=self.platform)
        self.store.sync_logs()
        # Persist the confirmed recipient state to avoid a stale gate on repeat runs.
        data.setdefault("recipient", {})["hasSubmittedFeedback"] = True
        protect_json(self.store.root / f"beatrolo-{source_id}.dpapi", data)

    def download(self, source_id, file, destination):
        path, recipient = self._source(source_id)
        params = {"r": recipient}
        if file["route"] == "track":
            path += "/tracks/" + quote(file["id"], safe="") + "/download"
        elif file["route"] == "legacy":
            path += "/legacy-download"
        elif file["route"] == "tracklist":
            path += "/tracklist-download"
            params.update(format=file["type"], index=file["index"])
        else:
            raise RuntimeError("Unknown BeatRolo original route")
        self.store.event(source_id, "platform_download", "intent", platform=self.platform, file_id=file["id"],
                         platform_effect="original-file endpoint may record download for sender")
        self.store.sync_logs()
        response = self._request("GET", path, params=params, stream=True, timeout=900)
        url, redirects = urljoin(BASE, path), []
        for _ in range(5):
            if response.status_code in REDIRECTS:
                target = urljoin(url, response.headers.get("Location", ""))
                redirects.append({"status": response.status_code, "target": target})
                response.close()
                protect_json(self.store.root / f"beatrolo-original-{source_id}.dpapi", {"redirects": redirects})
                if not original_storage_url(target):
                    raise RuntimeError("BeatRolo original redirect needs provider route verification; no redirected audio fetched")
                self.calls += 1
                try:
                    # The observed original delivery host is separate from the recipient
                    # account. Never send portal cookies, credentials or recipient headers.
                    response = requests.get(target, impersonate="chrome124", stream=True, allow_redirects=False, timeout=900)
                except Exception:
                    raise RuntimeError("BeatRolo original storage transfer failed; signed URL suppressed") from None
                url = target
                continue
            try:
                if response.status_code != 200:
                    raise RuntimeError(f"BeatRolo original HTTP {response.status_code}")
                kind = response.headers.get("Content-Type", "").lower()
                if any(x in kind for x in ("html", "json", "text/")):
                    raise RuntimeError("BeatRolo returned a page instead of an original file")
                size = int(response.headers.get("Content-Length") or 0) or file.get("bytes")
                return write_stream(response.iter_content(1 << 20), destination, size, tolerance=0)
            finally:
                response.close()
        response.close()
        raise RuntimeError("BeatRolo original exceeded redirect limit")

    def campaigns(self, max_age_days=30):
        best = {}
        for row in self.store.db.execute("SELECT id,metadata FROM sources WHERE platform=? ORDER BY discovered_at,id", (self.platform,)):
            target = recipient_link(self.store.source_url(row["id"]))
            age = newest_offer_age(self.store, row["id"])
            if age is None:
                age = date_age(json.loads(row["metadata"]).get("offered_at"))
            if not target or age is None or age > max_age_days:
                continue
            if target[0] not in best or age < best[target[0]][1]:
                best[target[0]] = (row["id"], age)
        return [sid for sid, _ in best.values()]

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30):
        catalogue = self.discover_catalogue()
        library, report, seen = LibraryIndex(self.store), [], set()
        for sid in self.campaigns(max_age_days):
            try:
                release = self.release(sid)
                if release["release_id"] in seen:
                    continue
                seen.add(release["release_id"])
                summary = collect_release(self.store, self, sid, library, dry_run=dry_run, allow_gate=allow_gate)
                report.append({"source_id": sid, **summary})
            except Exception as error:
                self.store.event(sid, "batch", "release_failed", platform=self.platform, error=str(error)[:200])
                report.append({"source_id": sid, "result": "failed: " + str(error)[:160]})
        self.store.sync_logs(force=True)
        return {"releases": report, "catalogue": catalogue, "http_requests": self.calls}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    mail = sub.add_parser("discover-mail")
    mail.add_argument("--since", default="30d")
    login = sub.add_parser("login")
    login.add_argument("credential_file", help="DPAPI JSON, outside the skill")
    sub.add_parser("catalogue")
    collect = sub.add_parser("collect")
    collect.add_argument("source_id")
    batch = sub.add_parser("batch")
    batch.add_argument("--max-age-days", type=int, default=30)
    for command in (collect, batch):
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--allow-feedback", action="store_true")
    collect.add_argument("--max-assets", type=int, help="cap new transfers after normal version selection and dedup")
    args = parser.parse_args()
    store = Store()
    try:
        adapter = BeatRolo(store)
        if args.command == "discover-mail":
            result = discover(store, args.since, [adapter.platform])
        elif args.command == "login":
            result = adapter.login(args.credential_file)
        elif args.command == "catalogue":
            result = adapter.discover_catalogue()
        elif args.command == "collect":
            result = collect_release(store, adapter, args.source_id, LibraryIndex(store), dry_run=args.dry_run,
                                     allow_gate=args.allow_feedback, max_assets=args.max_assets)
            store.sync_logs(force=True)
        else:
            result = adapter.batch(args.dry_run, args.allow_feedback, args.max_age_days)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
