"""Inflyte adapter. Browserless: the remember-me cookie alone passes Cloudflare for every page this uses.

Login itself sits behind Cloudflare Turnstile, so the cookie is captured once from a Chrome login and stored in
Windows Credential Manager. Every response rolls it forward about six weeks; save_token keeps the newest copy.
The manager, locker and search indexes do not open releases. Recent mailed releases missing from those indexes
are inspected individually, with a published view intent; that detail request may mark the release viewed.
"""
from __future__ import annotations

import argparse
import copy
import email.utils
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import keyring
from bs4 import BeautifulSoup
from curl_cffi import requests

from promo_automation import ACCOUNT, Store, atomic_text, now
from promo_cloud import FEEDBACK_CLOSERS, FEEDBACK_OPENERS, SERVICE, protect_json, read_protected_json
from promo_mail import newest_offer_age
from promo_pipeline import LibraryIndex, _ARTIST_SPLIT, collect_release, normalized, version_kind, write_stream
from process_promos import normalize_mix_type

BASE = "https://inflyteapp.com"
TOKEN_KEY = "inflyte:remember-token"
# 7 to 9 out of 10 mapped onto Inflyte's five-star scale.
STARS = 4
_INITIAL_DATA = re.compile(r"var\s+initialData\s*=\s*")
_CSRF = re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"')
# A link-only offer has no cheap content revision. Retain its inspected inventory through the normal
# 30-day offer window, invalidating immediately on newer mail. Fresh locker/live inventories always win.
# A sender silently editing an unmailed, non-live offer is detected by bounded revalidation or real work.
NONLIVE_METADATA_MAX_AGE_DAYS = 30


class TokenExpired(RuntimeError):
    """The stored remember-me cookie no longer signs in. Recapture it from a Chrome login."""


class ReleaseUnavailable(RuntimeError):
    """The platform explicitly states that this recipient offer cannot be downloaded."""


def locker_entry(row):
    """The locker is a separate compact schema. Its 'w' is rating time, never an offer date."""
    rid = row["id"]
    tracks = [{"id": t["i"], "release_id": rid, "name": t["n"], "artist_name": t["a"],
               "duration": t.get("d"), "types": ",".join(t.get("t") or [])} for t in row["t"]]
    return {"promo": {"id": rid, "name": row["n"], "sender_label": row.get("s"),
                      "catalog_number": row.get("code"), "reviewed": True,
                      "extra": {"can_download": True}}, "tracks": tracks, "origin": "locker"}


def availability_message(text):
    """Inflyte's JSON can still serve and even rate an expired release. Its recipient HTML states the expiry."""
    soup = BeautifulSoup(text, "html.parser")
    for node in soup.select(".alert"):
        message = node.get_text(" ", strip=True)
        if re.search(r"no longer available to rate or download|release (?:has )?expired", message, re.I):
            return "Inflyte says this release is no longer available to rate or download"
    return None


def _artists(text):
    return {normalized(a) for a in _ARTIST_SPLIT.split(text or "") if normalized(a)}


_MIX_WORD = re.compile(r"\b(?:mix|remix|edit|extended|radio|short|version|instrumental|dub|intro|vocal)\b", re.I)


def track_title(name, label=None):
    """Inflyte permits mix names in square brackets or after dashes; those must not become title/label text."""
    if label:
        match = re.search(r"\s*\[([^\]]+)\]\s*$", name)
        if match and normalized(match.group(1)) == normalized(label):
            name = name[:match.start()].strip()
    name = re.sub(r"\[([^\]]+)\]", lambda m: "(" + m.group(1) + ")" if _MIX_WORD.search(m.group(1)) else m.group(0), name)
    parts, start, depth = [], 0, 0
    for index, char in enumerate(name):
        depth += char in "(["
        depth -= char in ")]"
        if depth == 0 and name.startswith(" - ", index):
            parts.append(name[start:index].strip())
            start = index + 3
    parts.append(name[start:].strip())
    if len(parts) > 1 and all(_MIX_WORD.search(p) for p in parts[1:]):
        name = parts[0] + " (" + " ".join(parts[1:]) + ")"
    return normalize_mix_type(name)


def split_track(artist_name, name, label=None):
    """Artist and title for one track. Some uploads repeat the artist inside the name ('Robbie Seed - Home Is You')
    or list remixers as artists ('Black Box, Gareden, ARISEY, Taly Shum' with 'Black Box & Gareden - ...').
    A leading 'Artist - ' outside brackets wins when it shares an artist with the credited list."""
    name = re.sub(r"\s+", " ", name or "").strip()
    depth, cut = 0, None
    for index, char in enumerate(name):
        depth += char in "(["
        depth -= char in ")]"
        if depth == 0 and name.startswith(" - ", index):
            cut = index
            break
    artist = (artist_name or "").strip()
    if cut is not None and _artists(name[:cut]) & _artists(artist):
        artist, name = name[:cut].strip(), name[cut + 3:].strip()
    return artist, track_title(name.replace('"', "").strip(), label)


def release_label(promo):
    """The bracketed label in the release name is the real imprint; sender_label is sometimes a short form."""
    match = re.search(r"\[([^\]]+)\]\s*$", promo.get("name") or "")
    return (match.group(1) if match else promo.get("sender_label") or "").strip() or None


def sent_age_days(promo):
    try:
        sent = datetime.fromisoformat(promo["timestamp"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - sent).total_seconds() / 86400


class Inflyte:
    def __init__(self, store):
        self.store = store
        self.calls = 0
        self.csrf = None
        stored = keyring.get_password(SERVICE, TOKEN_KEY)
        self.token = json.loads(stored) if stored else None
        self.session = requests.Session(impersonate="chrome")
        if self.token:
            self.session.cookies.set("remember_user_token", self.token["value"], domain="inflyteapp.com", path="/")

    def request(self, method, path, **kwargs):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Only this platform's relative endpoints are allowed")
        if not self.token:
            raise TokenExpired("No Inflyte token stored; log in through Chrome and run save-token")
        self.calls += 1
        kwargs.setdefault("timeout", 60)
        kwargs.setdefault("allow_redirects", False)
        try:
            response = self.session.request(method, BASE + path, **kwargs)
        except Exception as error:
            raise RuntimeError("Platform request failed: " + type(error).__name__) from None
        if response.status_code in (301, 302) and "sign_in" in (response.headers.get("Location") or ""):
            raise TokenExpired("Inflyte token was refused; log in through Chrome and run save-token")
        return response

    def save_token(self):
        """Persist the rolled-forward cookie so the next run starts with a fresh six-week window."""
        for cookie in self.session.cookies.jar:
            if cookie.name == "remember_user_token" and cookie.value and cookie.value != self.token["value"]:
                self.token = {"value": cookie.value, "expires": cookie.expires or self.token.get("expires")}
                keyring.set_password(SERVICE, TOKEN_KEY, json.dumps(self.token))

    def feed(self):
        """The promo manager list plus every track. Neither call marks a release viewed."""
        page = self.request("GET", "/promos", headers={"Accept": "text/html"})
        if page.status_code != 200:
            raise RuntimeError(f"Platform HTTP {page.status_code} at /promos")
        start = _INITIAL_DATA.search(page.text)
        csrf = _CSRF.search(page.text)
        if not start or not csrf:
            raise RuntimeError("Promo list schema changed: initialData or csrf-token missing")
        self.csrf = csrf.group(1)
        data, _ = json.JSONDecoder().raw_decode(page.text, start.end())
        account = data.get("base_data", {}).get("reviewer_email")
        if not account or account.casefold() != ACCOUNT.casefold():
            raise TokenExpired("Inflyte signed in as a different or unidentified mailbox; verify the account before continuing")
        promos = data["promos"]
        if not promos:
            self.save_token()
            return {"promos": [], "tracks": []}
        # release_sig signs the whole id list, exactly as the page sends it; a partial list is refused with 403.
        response = self.request("POST", "/review/releases/track_infos", headers=self._xhr(),
                                data={"sig": data["release_sig"], "release_ids": ",".join(str(p["id"]) for p in promos)})
        if response.status_code != 200:
            raise RuntimeError(f"Platform HTTP {response.status_code} at track_infos")
        tracks = response.json()["tracks"]
        self.save_token()
        return {"promos": promos, "tracks": tracks}

    def _xhr(self):
        return {"X-CSRF-Token": self.csrf, "X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/promos",
                "Accept": "application/json, text/javascript, */*; q=0.01"}

    def _catalogue_data(self):
        """Decrypt the whole inventory once per file revision, rather than once per release."""
        path = self.store.root / "inflyte-catalogue.dpapi"
        if not path.exists():
            self._catalogue_cache_key, self._catalogue_cache = None, {}
            return self._catalogue_cache
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        if key != getattr(self, "_catalogue_cache_key", None):
            data = read_protected_json(path)
            if not isinstance(data, dict):
                raise RuntimeError("Inflyte protected catalogue schema changed")
            self._catalogue_cache_key, self._catalogue_cache = key, data
        return self._catalogue_cache

    def _save_catalogue(self, data):
        path = self.store.root / "inflyte-catalogue.dpapi"
        protect_json(path, data)
        stat = path.stat()
        self._catalogue_cache_key = (stat.st_mtime_ns, stat.st_size)
        self._catalogue_cache = data

    def _newest_mail(self, source_id):
        dates = []
        for (received,) in self.store.db.execute("SELECT received FROM messages WHERE source_id=?", (source_id,)):
            try:
                moment = email.utils.parsedate_to_datetime(received)
                dates.append(moment.replace(tzinfo=moment.tzinfo or timezone.utc).timestamp())
            except (ValueError, TypeError, OverflowError):
                continue
        return max(dates, default=None)

    def _cached_nonlive(self, source_id, remote_id, entry):
        """Require matching release/tracks, a recent inspection and no mail newer than that inspection."""
        if not isinstance(entry, dict) or entry.get("needs_detail"):
            return False
        promo, tracks = entry.get("promo"), entry.get("tracks")
        if (not isinstance(promo, dict) or str(promo.get("id")) != remote_id
                or not isinstance(tracks, list) or not tracks
                or any(not isinstance(t, dict) or str(t.get("release_id")) != remote_id
                       or not t.get("id") or not t.get("name") or not t.get("types") for t in tracks)):
            return False
        stamp = entry.get("metadata_checked_at")
        if not stamp:
            # Migrate legacy protected snapshots only when the durable release-specific inspection proves age.
            row = self.store.db.execute("SELECT utc FROM events WHERE source_id=? AND action='view' "
                                        "AND status='inspected' ORDER BY sequence DESC LIMIT 1", (source_id,)).fetchone()
            stamp = row[0] if row else None
        try:
            checked = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            checked = checked.replace(tzinfo=checked.tzinfo or timezone.utc)
        except (AttributeError, TypeError, ValueError):
            return False
        age = (datetime.now(timezone.utc) - checked).total_seconds() / 86400
        if not 0 <= age <= NONLIVE_METADATA_MAX_AGE_DAYS:
            return False
        newest = self._newest_mail(source_id)
        recorded = entry.get("newest_mail_at_inspection")
        if recorded is not None and newest != recorded:
            return False
        return newest is None or newest <= checked.timestamp()

    @staticmethod
    def _inventory(entry):
        """Stable track metadata only; recipient read/rating flags are not content revisions."""
        from promo_pipeline import seconds
        return sorted((str(t.get("id")), str(t.get("artist_name") or ""), str(t.get("name") or ""),
                       seconds(t.get("duration")), tuple(sorted((t.get("types") or "").lower().split(","))))
                      for t in entry.get("tracks", []))

    def catalogue(self, max_age_days=30):
        """Union the complete client-side manager with recent mailed locker entries and detail placeholders.

        The UI's page size filters the initial array in JavaScript; it is not server pagination. The locker may
        be capped, so absence there or in dashboard search never proves a mailed offer unavailable.
        """
        self.max_age_days = max_age_days
        previous = self._catalogue_data()
        feed = self.feed()
        by_release = {}
        for track in feed["tracks"]:
            by_release.setdefault(track["release_id"], []).append(track)
        snapshot = {str(p["id"]): {"promo": p, "tracks": by_release.get(p["id"], []), "origin": "live"}
                    for p in feed["promos"]}
        response = self.request("GET", "/review/releases/locker.json", headers=self._xhr())
        if response.status_code != 200:
            raise RuntimeError(f"Platform HTTP {response.status_code} at locker")
        locker = response.json().get("data")
        if not isinstance(locker, list):
            raise RuntimeError("Locker schema changed; catalogue coverage is incomplete")
        lockers = {str(p["id"]): p for p in locker}
        response = self.request("GET", "/search", headers=self._xhr())
        if response.status_code != 200:
            raise RuntimeError(f"Platform HTTP {response.status_code} at dashboard search")
        search = response.json().get("results")
        if not isinstance(search, list):
            raise RuntimeError("Dashboard search schema changed; catalogue coverage is incomplete")
        searchable = {str(p["id"]) for p in search if p.get("t") == "promo"}
        recent = {}
        for row in self.store.db.execute("SELECT * FROM sources WHERE platform='inflyte'").fetchall():
            age = newest_offer_age(self.store, row["id"])
            if age is not None and age <= max_age_days and row["remote_id"]:
                recent[row["remote_id"]] = row
        live_ids = set(snapshot)
        for rid, row in recent.items():
            if rid in snapshot:
                continue
            if rid in lockers:
                entry = locker_entry(lockers[rid])
                old = previous.get(rid)
                if (self._cached_nonlive(row["id"], rid, old)
                        and self._inventory(old) == self._inventory(entry)):
                    for key in ("availability_checked", "unavailable_reason"):
                        if key in old:
                            entry[key] = old[key]
                entry.update(metadata_checked_at=now(), newest_mail_at_inspection=self._newest_mail(row["id"]))
                snapshot[rid] = entry
            elif self._cached_nonlive(row["id"], rid, previous.get(rid)):
                snapshot[rid] = copy.deepcopy(previous[rid])
                if snapshot[rid].get("origin") == "live":
                    # Previously live is not proof that the offer is in today's manager or is downloadable.
                    snapshot[rid]["origin"] = "email_cached"
            else:
                snapshot[rid] = {"promo": {"id": int(rid), "name": row["subject"]}, "tracks": [],
                                 "origin": "email", "needs_detail": True}
        for entry in snapshot.values():
            promo = entry["promo"]
            sid = self.store.add_source("inflyte", promo["name"], f"{BASE}/review/releases/{promo['id']}")
            if entry.get("origin") == "live":
                entry.update(metadata_checked_at=now(), newest_mail_at_inspection=self._newest_mail(sid))
            metadata = {"label": release_label(promo), "sender": promo.get("sender_label"),
                        "catalogue": promo.get("catalog_number"), "sent": promo.get("timestamp"),
                        "release_date": promo.get("release_date"), "reviewed": promo.get("reviewed"),
                        "viewed": promo.get("viewed"), "deleted": promo.get("deleted"),
                        "can_download": promo.get("extra", {}).get("can_download"),
                        "track_count": len(entry["tracks"]), "catalogue_seen": entry["origin"] == "live",
                        "catalogue_origin": entry["origin"], "detail_pending": bool(entry.get("needs_detail"))}
            prior = json.loads(self.store.db.execute("SELECT metadata FROM sources WHERE id=?", (sid,)).fetchone()[0])
            prior.update(metadata)
            self.store.db.execute("UPDATE sources SET metadata=? WHERE id=?", (json.dumps(prior), sid))
        self.store.db.commit()
        self._save_catalogue(snapshot)
        coverage = {"checked": now(), "max_age_days": max_age_days, "live_releases": len(live_ids),
                    "locker_entries": len(locker), "locker_may_be_capped": len(locker) >= 5000,
                    "recent_mailed_releases": len(recent), "mailed_not_live": len(set(recent) - live_ids),
                    "recovered_from_locker": len((set(recent) - live_ids) & set(lockers)),
                    "detail_pending_ids": sorted(r for r in recent if snapshot[r].get("needs_detail")),
                    "not_in_account_indexes": sorted(set(recent) - live_ids - set(lockers) - searchable)}
        atomic_text(self.store.root / "inflyte-coverage.json", json.dumps(coverage, indent=2))
        self.save_token()
        self.store.event(None, "catalogue", "complete", platform="inflyte", releases=len(feed["promos"]),
                         tracks=len(feed["tracks"]), requests=self.calls,
                         note="Manager plus recent mailed locker releases; pending email details are not unavailable",
                         detail_pending=len(coverage["detail_pending_ids"]))
        return {"catalogue_releases": len(snapshot), "tracks": sum(len(e["tracks"]) for e in snapshot.values()),
                "coverage": coverage, "http_requests": self.calls}

    def _entry(self, source_id, planning=False, refresh_metadata=False):
        source = self.store.db.execute("SELECT * FROM sources WHERE id=? AND platform='inflyte'", (source_id,)).fetchone()
        if not source:
            raise ValueError("Unknown Inflyte source")
        parsed = urlsplit(self.store.source_url(source_id))
        if (parsed.hostname not in {"inflyteapp.com", "www.inflyteapp.com"}
                or parsed.scheme != "https" or not re.fullmatch(r"\d+", source["remote_id"] or "")):
            raise ValueError("Refusing a non-Inflyte source link")
        entry = self._catalogue_data().get(source["remote_id"])
        if (entry is not None and entry.get("origin") != "live"
                and not self._cached_nonlive(source_id, source["remote_id"], entry)):
            entry = None
        if entry is None or entry.get("needs_detail"):
            age = newest_offer_age(self.store, source_id)
            if age is None or age > getattr(self, "max_age_days", 30):
                raise RuntimeError("No current snapshot or recent emailed offer; run catalogue first")
            entry = self.inspect(source_id, source["remote_id"])
        if entry.get("unavailable_reason") and not (planning and refresh_metadata):
            raise ReleaseUnavailable(entry["unavailable_reason"])
        if (refresh_metadata and entry.get("origin") != "live"
                and (planning or entry.get("origin") in {"email_detail", "email_cached"})
                and source["remote_id"] not in getattr(self, "_metadata_verified", set())):
            entry = self.inspect(source_id, source["remote_id"])
        if (not planning and entry.get("origin") != "live"
                and source["remote_id"] not in getattr(self, "_availability_verified", set())):
            entry = self.check_availability(source_id, source["remote_id"], entry)
        if entry.get("unavailable_reason"):
            raise ReleaseUnavailable(entry["unavailable_reason"])
        return source, entry

    def check_availability(self, source_id, remote_id, entry):
        self.store.event(source_id, "view", "intent", platform="inflyte", release_id=remote_id,
                         platform_effect="recipient HTML availability check may mark this emailed release viewed")
        self.store.sync_logs()
        response = self.request("GET", f"/review/releases/{remote_id}", headers={"Accept": "text/html"})
        self.save_token()
        try:
            if response.status_code in (403, 404, 410):
                reason = f"Inflyte recipient page unavailable (HTTP {response.status_code})"
            elif response.status_code != 200:
                raise RuntimeError(f"Platform HTTP {response.status_code} at recipient availability check")
            else:
                reason = availability_message(response.text)
                if "Inflyte: Release" not in response.text:
                    raise RuntimeError("Recipient availability page schema changed; availability is unverified")
        finally:
            response.close()
        entry.update(availability_checked=now(), unavailable_reason=reason)
        self._availability_verified = getattr(self, "_availability_verified", set()) | {remote_id}
        snapshot = self._catalogue_data()
        snapshot[remote_id] = entry
        self._save_catalogue(snapshot)
        coverage_path = self.store.root / "inflyte-coverage.json"
        if coverage_path.exists():
            coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
            coverage["availability_checked_releases"] = sum(bool(e.get("availability_checked")) for e in snapshot.values())
            coverage["explicitly_unavailable_ids"] = sorted(r for r, e in snapshot.items() if e.get("unavailable_reason"))
            atomic_text(coverage_path, json.dumps(coverage, indent=2))
        self.store.event(source_id, "availability", "unavailable" if reason else "page_checked", platform="inflyte",
                         release_id=remote_id, reason=reason,
                         note="Page availability is not proof of a completed audio transfer")
        return entry

    def inspect(self, source_id, remote_id):
        """Check a known recent recipient offer, never guess IDs or treat a manager deletion as a withdrawal."""
        if not self.csrf:
            self.feed()
        self.store.event(source_id, "view", "intent", platform="inflyte", release_id=remote_id,
                         platform_effect="detail request may mark this recent emailed release viewed")
        self.store.sync_logs()
        response = self.request("GET", f"/review/releases/{remote_id}.json", headers=self._xhr())
        self.save_token()
        if response.status_code in (403, 404, 410):
            self.store.event(source_id, "view", "unavailable", platform="inflyte", http=response.status_code)
            raise ReleaseUnavailable(f"Inflyte recipient detail unavailable (HTTP {response.status_code})")
        if response.status_code != 200:
            raise RuntimeError(f"Platform HTTP {response.status_code} at recipient detail")
        data = response.json().get("promo_data", {})
        promos, tracks = data.get("promos"), data.get("tracks")
        if (not isinstance(promos, list) or len(promos) != 1 or str(promos[0].get("id")) != remote_id
                or not isinstance(tracks, list) or any(str(t.get("release_id")) != remote_id for t in tracks)):
            raise RuntimeError("Recipient detail schema or release identity changed; coverage remains incomplete")
        entry = {"promo": promos[0], "tracks": tracks, "origin": "email_detail", "metadata_checked_at": now(),
                 "newest_mail_at_inspection": self._newest_mail(source_id)}
        self._metadata_verified = getattr(self, "_metadata_verified", set()) | {remote_id}
        snapshot = self._catalogue_data()
        snapshot[remote_id] = entry
        self._save_catalogue(snapshot)
        coverage_path = self.store.root / "inflyte-coverage.json"
        if coverage_path.exists():
            coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
            coverage["detail_pending_ids"] = [r for r in coverage["detail_pending_ids"] if r != remote_id]
            atomic_text(coverage_path, json.dumps(coverage, indent=2))
        self.store.event(source_id, "view", "inspected", platform="inflyte", tracks=len(tracks),
                         removed_from_manager=bool(promos[0].get("deleted")))
        return entry

    def release(self, source_id):
        if not self.csrf:
            self.feed()  # Verify the mailbox even when the track inventory came from a saved snapshot.
        source, entry = self._entry(source_id, refresh_metadata=True)
        return self._normalize(source, entry)

    def planning_release(self, source_id, refresh=False):
        """Bulk/cached inventory for per-mix local decisions. Availability is validated before real work."""
        if not self.csrf:
            self.feed()  # The fast path must retain the same mailbox identity check.
        source, entry = self._entry(source_id, planning=True, refresh_metadata=refresh)
        return self._normalize(source, entry)

    @staticmethod
    def _normalize(source, entry):
        promo = entry["promo"]
        tracks = []
        for row in entry["tracks"]:
            artist, title = split_track(row.get("artist_name"), row.get("name"), release_label(promo))
            types = [t.strip().lower() for t in (row.get("types") or "").split(",") if t.strip()]
            types.sort(key=lambda t: ["wav", "aif", "mp3"].index(t) if t in ("wav", "aif", "mp3") else 9)
            tracks.append({"name": f"{artist} - {title}" if artist else title, "artist": artist or None,
                           "duration": row.get("duration"), "track_id": row["id"],
                           "files": [{"id": row["id"], "type": t} for t in types]})
        return {"title": promo["name"], "label": release_label(promo), "gated": not promo.get("extra", {}).get("can_download"),
                "feedback_left": bool(promo.get("reviewed")), "tracks": tracks, "remote_id": source["remote_id"]}

    @staticmethod
    def extension(file):
        extension = {"wav": ".wav", "aif": ".aif", "aiff": ".aif", "mp3": ".mp3"}.get(file["type"])
        if not extension:
            raise RuntimeError("Unsupported file type from platform: " + file["type"])
        return extension

    def download(self, source_id, file, destination):
        source, _ = self._entry(source_id)
        response = self.request("GET", f"/review/releases/{source['remote_id']}/tracks/{file['id']}/download_audio",
                                params={"type": file["type"]}, headers={"Referer": BASE + "/promos"})
        self.save_token()
        if response.status_code == 302:
            location = urljoin(BASE, response.headers.get("Location") or "")
            host = urlsplit(location).hostname or ""
            if host == "inflyteapp.com" and urlsplit(location).path == f"/review/releases/{source['remote_id']}":
                response.close()
                _, entry = self._entry(source_id)
                entry = self.check_availability(source_id, source["remote_id"], entry)
                raise ReleaseUnavailable(entry.get("unavailable_reason") or "Inflyte refused the original and returned to the recipient page")
            if urlsplit(location).scheme != "https" or not host.endswith(".cloudfront.net"):
                raise RuntimeError("Download redirected somewhere unexpected; not following")
            self.calls += 1
            try:
                response = requests.get(location, impersonate="chrome", stream=True, timeout=900)
            except Exception as error:
                raise RuntimeError("File host request failed: " + type(error).__name__) from None
        try:
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code} on download")
            kind = (response.headers.get("Content-Type") or "").lower()
            if "html" in kind or "json" in kind:
                raise RuntimeError("Platform answered the download with a page instead of audio")
            expected = int(response.headers.get("Content-Length") or 0)
            return write_stream(response.iter_content(1 << 20), destination, expected or None, tolerance=0)
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
                              (source_id, STARS, comment, 0, "pending", now()))
        self.store.db.commit()
        return self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (source_id,)).fetchone()

    def unlock(self, source_id, release):
        """Rate once to unlock downloads. An unknown outcome blocks further automatic retries."""
        source, _ = self._entry(source_id)
        row = self.choose_feedback(source_id, release)
        if row["status"] == "submitted":
            return
        if row["status"] != "pending":
            raise RuntimeError("Previous feedback submission has an uncertain outcome; inspect before retrying")
        if not self.csrf:
            self.feed()
        favourite = next((t for t in release["tracks"] if version_kind(t["name"]) == "extended"), release["tracks"][0])
        self.store.db.execute("UPDATE feedback SET status='submitting' WHERE source_id=?", (source_id,))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "intent", rating=row["rating"], scale="5 stars", comment=row["comment"],
                         favourite=favourite["name"], platform_effect="unlocks downloads, visible to the label")
        self.store.sync_logs()
        response = self.request("POST", f"/review/releases/{source['remote_id']}", headers=self._xhr(),
                                data={"sharing[rating]": row["rating"], "sharing[favourite_track_id]": favourite["track_id"],
                                      "sharing[comments]": row["comment"], "_method": "PUT", "presenter": "true"})
        self.save_token()
        try:
            result = response.json()
        except Exception:
            result = None
        if response.status_code != 200 or not isinstance(result, dict):
            self.store.event(source_id, "feedback", "uncertain", http=response.status_code)
            raise RuntimeError(f"Rating answered HTTP {response.status_code} without a result; inspect before retrying")
        if not result.get("op_result", {}).get("okay"):
            self.store.db.execute("UPDATE feedback SET status='rejected' WHERE source_id=?", (source_id,))
            self.store.db.commit()
            self.store.event(source_id, "feedback", "rejected", messages=result.get("messages"))
            raise RuntimeError("Platform rejected the rating: " + "; ".join(result.get("messages") or [])[:160])
        self.store.db.execute("UPDATE feedback SET status='submitted',submitted_at=? WHERE source_id=?", (now(), source_id))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "submitted", rating=row["rating"])
        self.store.sync_logs()
        updated = result.get("promo_data", {}).get("promos", [])
        if updated and not updated[0].get("extra", {}).get("can_download"):
            raise ReleaseUnavailable("Inflyte saved the rating but still refuses downloads; no feedback will be repeated")

    def batch(self, dry_run=False, allow_gate=False, pause=0, max_age_days=30):
        """Refresh the list, then decide per mix. A title alone cannot prove all remixes are present."""
        self.catalogue(max_age_days=max_age_days)
        library = LibraryIndex(self.store)
        snapshot = self._catalogue_data()
        rows = self.store.db.execute("""SELECT id, subject, remote_id FROM sources WHERE platform='inflyte'
            AND remote_id IS NOT NULL""").fetchall()
        report = []
        for sid, subject, remote_id in sorted(rows, key=lambda r: snapshot.get(r[2], {}).get("promo", {}).get("timestamp") or ""):
            entry = snapshot.get(remote_id)
            if entry is None:
                continue
            ages = [a for a in (newest_offer_age(self.store, sid), sent_age_days(entry["promo"])) if a is not None]
            age = min(ages) if ages else None
            if age is None or age > max_age_days:
                self.store.event(sid, "select", "skipped_too_old", release=subject,
                                 age_days=round(age) if age is not None else None)
                report.append({"release": subject, "result": "undated or too old, skipped"})
                continue
            try:
                summary = self._collect_planned(sid, library, dry_run=dry_run, allow_gate=allow_gate)
                report.append({"source_id": sid, "release": subject, "catalogue_origin": entry.get("origin"),
                               "tracks": summary["tracks"]})
                if not dry_run and any(t.get("action") in {"download", "resume"} for t in summary["tracks"]):
                    time.sleep(pause)
            except TokenExpired:
                raise
            except ReleaseUnavailable as error:
                report.append({"source_id": sid, "release": subject, "state": "unavailable", "reason": str(error)})
            except Exception as error:
                self.store.event(sid, "batch", "release_failed", release=subject, error=str(error)[:200])
                report.append({"release": subject, "result": "failed: " + str(error)[:160]})
        self.save_token()
        self.store.sync_logs(force=True)
        return report

    def collect(self, source_id, dry_run=False, allow_gate=False):
        return self._collect_planned(source_id, LibraryIndex(self.store), dry_run=dry_run, allow_gate=allow_gate)

    def _collect_planned(self, source_id, library, dry_run=False, allow_gate=False):
        class Planning:
            release = self.planning_release
        plan = collect_release(self.store, Planning(), source_id, library, dry_run=True)
        if dry_run or not any(t.get("action") in {"download", "replace", "resume"} for t in plan.get("tracks", [])):
            return plan
        return collect_release(self.store, self, source_id, library, allow_gate=allow_gate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalogue")
    commands.add_parser("coverage", help="check manager/locker/recent-mail coverage without opening releases")
    commands.add_parser("save-token", help='store {"value":..., "expires": epoch} from stdin after a Chrome login')
    collect = commands.add_parser("collect", help="decide, download, process and deliver one release")
    collect.add_argument("source_id")
    collect.add_argument("--dry-run", action="store_true")
    collect.add_argument("--allow-feedback", action="store_true", help="permit the one-time rating that unlocks downloads")
    batch = commands.add_parser("batch", help="collect every release in the promo list not already in the library")
    batch.add_argument("--dry-run", action="store_true")
    batch.add_argument("--allow-feedback", action="store_true")
    batch.add_argument("--max-age-days", type=int, default=30, help="skip releases sent longer ago")
    args = parser.parse_args()
    store = Store()
    if args.command == "save-token":
        token = json.loads(sys.stdin.read())
        if not token.get("value"):
            raise SystemExit("No token value given")
        keyring.set_password(SERVICE, TOKEN_KEY, json.dumps({"value": token["value"], "expires": token.get("expires")}))
        print(json.dumps({"stored": True}))
        return
    adapter = Inflyte(store)
    if args.command in {"catalogue", "coverage"}:
        result = adapter.catalogue()
    elif args.command == "collect":
        result = adapter.collect(args.source_id, dry_run=args.dry_run, allow_gate=args.allow_feedback)
        store.sync_logs(force=True)
    else:
        # The coordinator refreshes mail in prepare; the standalone batch must do so itself as well.
        from promo_mail import discover
        discover(store, f"{args.max_age_days}d", ["inflyte"], sender_domain="inflyteapp.com")
        result = adapter.batch(dry_run=args.dry_run, allow_gate=args.allow_feedback, max_age_days=args.max_age_days)
        result = {"releases": result, "upgrade_candidates_ask_user": [t["track"] for r in result for t in r.get("tracks", [])
                                                                         if t.get("action") == "upgrade_candidate"],
                  "needs_review": [t["track"] for r in result for t in r.get("tracks", []) if t.get("action") == "needs_review"],
                  "unavailable_releases": [r for r in result if r.get("state") == "unavailable"]}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
