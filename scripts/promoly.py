"""Promoly email-link adapter using the recipient API. No browser or account password is needed.

Discovery only reads mail. Inspect/collect can affect recipient state and are audited before contact.
Signed CDN links and recipient snapshots stay DPAPI protected. Feedback has a durable one-time gate.
"""
from __future__ import annotations

import argparse
import json
import random
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from curl_cffi import requests

from promo_automation import Store, now
from promo_cloud import FEEDBACK_CLOSERS, FEEDBACK_OPENERS, protect_json, read_protected_json
from promo_mail import discover, newest_offer_age, route
from promo_pipeline import LibraryIndex, collect_release, version_kind, write_stream

API = "https://api.promo.ly"
CDN = "https://drip38d09qbfk.cloudfront.net"
TYPES = {"wav", "aif", "aiff", "flac", "mp3", "m4a"}


def normalize_release(data):
    campaign = data["campaign"]
    if campaign.get("archived") or campaign.get("deleted_at") or campaign.get("visibility") == "private":
        raise RuntimeError("Campaign is archived, deleted or private")
    if campaign.get("enableDownload") != "on":
        raise RuntimeError("Uploader has disabled downloads")
    expires = campaign.get("expires")
    if expires:
        expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            raise RuntimeError("Campaign has expired")
    tracks = []
    for track in campaign.get("tracks") or []:
        artist = (track.get("track_artist") or "").strip()
        title = (track.get("track_title") or "").strip()
        files = [{"id": file["file_id"], "type": file["file_extension"].lower().lstrip("."),
                  "size_bytes": file.get("file_size"), "name": file["file_name"],
                  "index": int(track.get("track_num") or 1) - 1}
                 for file in track.get("files") or [] if file.get("file_extension", "").lower().lstrip(".") in TYPES]
        if not title:
            raise RuntimeError("Campaign has a track without a title; metadata research required")
        tracks.append({"name": f"{artist} - {title}" if artist else title, "artist": artist or None,
                       "track_id": track["track_id"], "files": files})
    if not tracks:
        raise RuntimeError("No supported tracks in campaign; legacy filesArr needs separate mapping")
    feedback = data.get("feedback") or []
    return {"title": campaign.get("name") or campaign.get("emailSubject"), "label": campaign.get("labelName"),
            "gated": campaign.get("requireComment") == "on" and not any(f.get("comment") for f in feedback),
            "feedback_left": bool(feedback), "tracks": tracks}


class Promoly:
    def __init__(self, store):
        self.store, self.calls = store, 0
        self.session = requests.Session(impersonate="chrome")
        self.session.headers.update({"Accept": "application/json", "Origin": "https://ln.promo.ly",
                                     "Referer": "https://ln.promo.ly/"})
        self.signatures = {}

    def _scope(self, source_id):
        source = self.store.db.execute("SELECT * FROM sources WHERE id=? AND platform='promoly'", (source_id,)).fetchone()
        if source is None:
            raise ValueError("Unknown Promoly source")
        url = self.store.source_url(source_id)
        if route(url) != "promoly":
            raise ValueError("Source URL is not a recipient campaign")
        return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]

    def request(self, method, path, **kwargs):
        self.calls += 1
        try:
            response = self.session.request(method, API + path, timeout=60, allow_redirects=False, **kwargs)
        except Exception as error:
            raise RuntimeError("Promoly API request failed: " + type(error).__name__) from None
        try:
            if response.status_code != 200:
                raise RuntimeError(f"Promoly API answered HTTP {response.status_code}")
            return response.json()
        except (ValueError, TypeError):
            raise RuntimeError("Promoly API returned an unexpected response") from None
        finally:
            response.close()

    def snapshot(self, source_id, refresh=False):
        path = self.store.root / f"promoly-{source_id}.dpapi"
        if path.exists() and not refresh:
            return read_protected_json(path)
        key = self._scope(source_id)
        self.store.event(source_id, "view", "intent", platform_effect="recipient API may record access")
        self.store.sync_logs()
        data = self.request("GET", "/api/landers/" + quote(key, safe=""))
        if not isinstance(data, dict) or data.get("key") != key or not isinstance(data.get("campaign"), dict):
            raise RuntimeError("Promoly recipient schema changed")
        protect_json(path, data)
        self.store.event(source_id, "view", "inspected", tracks=len(data["campaign"].get("tracks") or []),
                         platform_effect="recipient API requested; no plays or pageview beacon sent")
        self.store.sync_logs()
        return data

    def release(self, source_id):
        return normalize_release(self.snapshot(source_id))

    @staticmethod
    def extension(file):
        kind = file["type"].lower()
        if kind not in TYPES:
            raise RuntimeError("Unsupported download type")
        return "." + kind

    def unlock(self, source_id, release):
        row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            rng = random.Random(source_id + "|" + release["title"])
            comment = rng.choice(FEEDBACK_OPENERS) + ", " + rng.choice(FEEDBACK_CLOSERS)
            self.store.db.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?,NULL)",
                                  (source_id, 4, comment, 0, "pending", now()))
            self.store.db.commit()
            row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (source_id,)).fetchone()
        if row["status"] == "submitted":
            return
        if row["status"] != "pending":
            raise RuntimeError("Previous Promoly feedback outcome is uncertain; inspect before retrying")
        data = self.snapshot(source_id)
        key = data["key"]
        self.store.db.execute("UPDATE feedback SET status='submitting' WHERE source_id=?", (source_id,))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "intent", rating=row["rating"], scale="5 stars", comment=row["comment"],
                         platform_effect="visible to sender, unlocks downloads")
        self.store.sync_logs()
        payload = {"key": key, "campaignId": data["campaign"]["id"], "rating": row["rating"],
                   "comment": row["comment"], "favTrack": None}
        result = self.request("PUT", "/api/landers/" + quote(key, safe="") + "/feedback", json=payload)
        if not isinstance(result, dict) or result.get("comment") != row["comment"]:
            raise RuntimeError("Feedback result did not confirm the saved comment; inspect before retrying")
        data["feedback"] = [result]
        protect_json(self.store.root / f"promoly-{source_id}.dpapi", data)
        self.store.db.execute("UPDATE feedback SET status='submitted',submitted_at=? WHERE source_id=?", (now(), source_id))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "submitted", rating=row["rating"])
        self.store.sync_logs()

    def download(self, source_id, file, destination):
        data = self.snapshot(source_id)
        if normalize_release(data)["gated"]:
            raise RuntimeError("Feedback gate remains locked")
        key = self._scope(source_id)
        if source_id not in self.signatures:
            signed = self.request("GET", "/api/landers/" + quote(key, safe="") + "/presigneds")
            parsed = urlsplit(signed.get("signedUrl", ""))
            if parsed.scheme != "https" or parsed.hostname != urlsplit(CDN).hostname or not parsed.query:
                raise RuntimeError("Unexpected signed download host or contract")
            self.signatures[source_id] = parsed.query
        # Same storage-path transformation as the recipient's TrackMultiDownloadButton.
        assets = re.sub(r"(?:s3://[^/]+/|https://[^/]+/)?(users/)?", "", data["campaign"]["assetsLocalPath"])
        if ".." in assets.split("/") or "/" in file["name"] or "\\" in file["name"]:
            raise RuntimeError("Unsafe platform file path")
        self.store.event(source_id, "platform_download", "intent", file_id=file["id"],
                         platform_effect="one download beacon and one CDN request")
        self.store.sync_logs()
        self.request("POST", "/api/landers/" + quote(key, safe="") + "/downloads",
                     json={"key": key, "name": file["name"], "index": file["index"]})
        url = CDN + "/" + quote(assets.strip("/"), safe="/") + "/" + quote(file["name"], safe="")
        self.calls += 1
        try:
            response = requests.get(url + "?" + self.signatures[source_id] + "&response-content-disposition=attachment",
                                    impersonate="chrome", stream=True, timeout=900, allow_redirects=False)
        except Exception as error:
            raise RuntimeError("Promoly file transfer failed: " + type(error).__name__) from None
        try:
            if response.status_code != 200:
                raise RuntimeError(f"Promoly CDN answered HTTP {response.status_code}")
            if any(kind in response.headers.get("Content-Type", "").lower() for kind in ("html", "json")):
                raise RuntimeError("Promoly returned a page instead of audio")
            return write_stream(response.iter_content(1 << 20), destination,
                                file.get("size_bytes") or int(response.headers.get("Content-Length") or 0), tolerance=0)
        finally:
            response.close()

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30, limit=None):
        library, report = LibraryIndex(self.store), []
        rows = self.store.db.execute("SELECT id,subject FROM sources WHERE platform='promoly' ORDER BY discovered_at,id").fetchall()
        for sid, subject in rows[:limit] if limit else rows:
            age = newest_offer_age(self.store, sid)
            if age is None or age > max_age_days:
                report.append({"release": subject, "result": "undated or too old; skipped"})
                continue
            try:
                result = collect_release(self.store, self, sid, library, dry_run=dry_run, allow_gate=allow_gate)
                report.append({"source_id": sid, **result})
            except Exception as error:
                self.store.event(sid, "batch", "release_failed", error=str(error)[:180])
                report.append({"source_id": sid, "release": subject, "error": str(error)[:180]})
        self.store.sync_logs(force=True)
        return {"releases": report, "http_requests": self.calls,
                "upgrade_candidates_ask_user": [t["track"] for r in report for t in r.get("tracks", [])
                                                   if t.get("action") == "upgrade_candidate"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    mail = sub.add_parser("discover-mail")
    mail.add_argument("--since", default="30d")
    collect = sub.add_parser("collect")
    collect.add_argument("source_id")
    batch = sub.add_parser("batch")
    batch.add_argument("--max-age-days", type=int, default=30)
    batch.add_argument("--limit", type=int)
    for command in (collect, batch):
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--allow-feedback", action="store_true")
    args = parser.parse_args()
    store = Store()
    try:
        adapter = Promoly(store)
        if args.command == "discover-mail":
            result = discover(store, args.since, ["promoly"])
        elif args.command == "collect":
            result = collect_release(store, adapter, args.source_id, LibraryIndex(store),
                                     dry_run=args.dry_run, allow_gate=args.allow_feedback)
            store.sync_logs(force=True)
        else:
            result = adapter.batch(args.dry_run, args.allow_feedback, args.max_age_days, args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
