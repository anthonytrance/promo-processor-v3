"""NeedlePool recipient portal. Auth-magic and consent/register links are distinct.

Only auth-magic may start a recipient session. Consents, registrations, link reissue,
share-confirm and subscription routes are never followed. A saved recipient session
recovers mailed campaigns through the catalogue, including spent one-use mail links.
"""
import argparse
import json
import random
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

from promo_automation import Store, now
from promo_cloud import FEEDBACK_CLOSERS, FEEDBACK_OPENERS, protect_json, read_protected_json
from promo_mail import discover, newest_offer_age
from promo_pipeline import LibraryIndex, collect_release, write_stream

BASE = "https://app.needlepool.com"
HOST = "app.needlepool.com"
AUTH = "/.netlify/functions/auth-magic"
CAMPAIGN = re.compile(r"/portal/campaign/([a-f0-9-]{36})/?$")
REDIRECTS = {301, 302, 303, 307, 308}


class AccessRequired(RuntimeError):
    pass


def secure_url(parsed):
    try:
        return (parsed.scheme == "https" and parsed.port in (None, 443)
                and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


def recipient_link(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if not secure_url(parsed) or parsed.hostname != HOST:
        return None
    if parsed.path == AUTH and parse_qs(parsed.query).get("t"):
        return "magic", parse_qs(parsed.query)["t"][0]
    match = CAMPAIGN.fullmatch(parsed.path)
    return ("campaign", match.group(1)) if match else None


def flight_props(html):
    """Decode JSON data from Next's Flight records, without executing scripts."""
    decoder, parts = json.JSONDecoder(), []
    for script in BeautifulSoup(html, "html.parser").select("script"):
        content = script.string or script.get_text()
        for match in re.finditer(r"self\.__next_f\.push\(", content):
            try:
                value, _ = decoder.raw_decode(content[match.end():])
            except (ValueError, TypeError):
                continue
            if isinstance(value, list) and len(value) > 1 and value[0] == 1 and isinstance(value[1], str):
                parts.append(value[1])
    payload, found = "".join(parts), []
    # Decode only object starts whose immediate field is campaignId. This is the
    # component's explicit JSON props object, not arbitrary artist text or script.
    for match in re.finditer(r'\{\s*"campaignId"\s*:', payload):
        try:
            value, _ = decoder.raw_decode(payload[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("tracks"), list):
            found.append(value)
    if len(found) != 1:
        raise RuntimeError("NeedlePool campaign metadata schema changed or recipient access expired")
    return found[0]


def normalize_release(data):
    meta = data.get("releaseMeta") or {}
    artist, label = meta.get("artist"), meta.get("label")
    tracks = []
    for track in data.get("tracks") or []:
        title, who = (track.get("title") or "").strip(), track.get("artist") or artist
        prefix = (who + " - ") if who else ""
        if prefix and title.casefold().startswith(prefix.casefold()):
            title = title[len(prefix):].strip()
        if not track.get("id") or not title:
            raise RuntimeError("NeedlePool track lacks its stable id or title")
        files = [{"id": str(track["id"]) + "-" + kind, "track_id": str(track["id"]), "type": kind}
                 for kind in ("wav", "mp3") if data.get("allowDownloads", True) and track.get("has_" + kind)]
        tracks.append({"name": f"{who} - {title}" if who else title, "artist": who, "title": title,
                       "label": label, "duration": track.get("duration_sec"), "files": files})
    if not tracks or not data.get("campaignId"):
        raise RuntimeError("NeedlePool campaign contains no track metadata")
    return {"title": meta.get("album") or data.get("title"), "label": label, "tracks": tracks,
            "gated": bool(data.get("requireFeedback") and not data.get("initialFeedback")),
            "campaign_id": data["campaignId"], "warnings": ([] if data.get("allowDownloads", True)
              else ["The recipient portal disables downloads for this campaign"])}


def catalogue_cards(html):
    soup, cards, seen = BeautifulSoup(html, "html.parser"), [], set()
    for link in soup.select('a[href^="/portal/campaign/"]'):
        match = CAMPAIGN.fullmatch(urlsplit(link.get("href", "")).path)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        paragraphs = link.select("p")
        if len(paragraphs) < 2:
            raise RuntimeError("NeedlePool catalogue card schema changed")
        artist, title = [p.get_text(" ", strip=True) for p in paragraphs[:2]]
        date = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", link.get_text(" ", strip=True))
        offered = datetime.strptime(date.group(1), "%m/%d/%Y").replace(tzinfo=timezone.utc).isoformat() if date else None
        cards.append({"campaign_id": match.group(1), "title": title, "artist": artist,
                      "offered_at": offered, "url": BASE + "/portal/campaign/" + match.group(1)})
    text = soup.get_text(" ", strip=True)
    count = re.search(r"Releases sent to you\s*\((\d+)\)", text)
    if not count or int(count.group(1)) != len(cards):
        raise RuntimeError("NeedlePool catalogue count does not match visible cards; pagination or schema needs inspection")
    return cards


def date_age(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds() / 86400
    except (ValueError, TypeError, AttributeError):
        return None


class Needlepool:
    platform = "needlepool"

    def __init__(self, store):
        self.store, self.calls = store, 0
        self.session = requests.Session(impersonate="chrome124")
        self.session_path = store.root / "needlepool-session.dpapi"
        self.sid = str(uuid.uuid4())
        if self.session_path.exists():
            saved = read_protected_json(self.session_path)
            self.session.cookies.update(saved.get("cookies", {}))
            self.sid = saved.get("sid") or self.sid

    def _save_session(self):
        protect_json(self.session_path, {"cookies": dict(self.session.cookies), "sid": self.sid})

    def _request(self, method, url, **kwargs):
        url = urljoin(BASE, url)
        parsed = urlsplit(url)
        allowed = parsed.path in {AUTH, "/portal", "/.netlify/functions/portal-feedback", "/.netlify/functions/portal-audio-url"}
        if not secure_url(parsed) or parsed.hostname != HOST or not (allowed or CAMPAIGN.fullmatch(parsed.path)):
            raise RuntimeError("NeedlePool route refused; only recipient auth, catalogue, metadata, feedback and original routes are allowed")
        self.calls += 1
        try:
            return self.session.request(method, url, allow_redirects=False, timeout=kwargs.pop("timeout", 60), **kwargs)
        except Exception:
            raise RuntimeError("NeedlePool request failed; protected details suppressed") from None

    def _source(self, source_id):
        row = self.store.db.execute("SELECT 1 FROM sources WHERE id=? AND platform=?", (source_id, self.platform)).fetchone()
        target = recipient_link(self.store.source_url(source_id)) if row else None
        if not target:
            raise ValueError("Not a NeedlePool recipient auth-magic or portal campaign source")
        return target

    def snapshot(self, source_id, refresh=False):
        kind, remote = self._source(source_id)
        cache = self.store.root / f"needlepool-{source_id}.dpapi"
        if cache.exists() and not refresh:
            return read_protected_json(cache)
        location, expected_campaign = self.store.source_url(source_id), None
        if kind == "magic" and cache.exists() and self.session_path.exists():
            # A previous successful login consumed this mail token. Reuse its verified campaign route.
            try:
                known = read_protected_json(cache).get("campaignId")
                if known and CAMPAIGN.fullmatch("/portal/campaign/" + known):
                    location, expected_campaign = BASE + "/portal/campaign/" + known, known
            except (ValueError, TypeError, RuntimeError, OSError):
                pass
        self.store.event(source_id, "view", "intent", platform=self.platform,
                         platform_effect="auth-magic may consume one-use token and create session; no consent or tracking POST")
        self.store.sync_logs()
        response = self._request("GET", location)
        try:
            if kind == "magic" and response.status_code in REDIRECTS:
                target = urljoin(BASE, response.headers.get("Location", ""))
                parsed = urlsplit(target)
                if not secure_url(parsed) or parsed.hostname != HOST:
                    raise RuntimeError("NeedlePool auth redirected outside the recipient host")
                if parsed.path == "/link":
                    raise AccessRequired("NeedlePool one-use mail link was already opened; recover via a saved recipient portal session")
                if not CAMPAIGN.fullmatch(parsed.path):
                    raise RuntimeError("NeedlePool auth redirect is not a recipient campaign; consent/register routes refused")
                # Save the cookie before a later page/network error, since the token is consumed.
                self._save_session()
                response.close()
                response = self._request("GET", target)
            if response.status_code != 200:
                raise AccessRequired(f"NeedlePool recipient page HTTP {response.status_code}; valid recipient session required")
            data = flight_props(response.text)
        finally:
            response.close()
        if kind == "campaign" and data.get("campaignId") != remote:
            raise RuntimeError("NeedlePool recipient page campaign differs from requested source")
        if expected_campaign and data.get("campaignId") != expected_campaign:
            raise RuntimeError("NeedlePool refreshed page differs from its saved campaign")
        normalize_release(data)
        self._save_session()
        protect_json(cache, data)
        self.store.event(source_id, "view", "inspected", platform=self.platform, tracks=len(data["tracks"]),
                         note="Rendered metadata only; no audio, plays, feedback, visit beacon or consent")
        return data

    def release(self, source_id):
        return normalize_release(self.snapshot(source_id))

    def discover_catalogue(self):
        if not self.session_path.exists():
            # Latest mail first, because older one-use links may already have been consumed.
            candidates = []
            for (sid,) in self.store.db.execute("SELECT id FROM sources WHERE platform=?", (self.platform,)):
                target = recipient_link(self.store.source_url(sid))
                age = newest_offer_age(self.store, sid)
                if target and target[0] == "magic" and age is not None and age <= 30:
                    candidates.append((age, sid))
            for _, sid in sorted(candidates):
                try:
                    self.snapshot(sid)
                    break
                except AccessRequired:
                    continue
        if not self.session_path.exists():
            raise AccessRequired("NeedlePool has no valid saved session and no unused auth-magic link; a fresh recipient sign-in is required")
        self.store.event(None, "catalogue", "intent", platform=self.platform, platform_effect="read recipient catalogue only")
        self.store.sync_logs()
        response = self._request("GET", "/portal?view=dj")
        try:
            if response.status_code != 200:
                raise AccessRequired(f"NeedlePool catalogue HTTP {response.status_code}; session needs refresh")
            cards = catalogue_cards(response.text)
        finally:
            response.close()
        sources = []
        for card in cards:
            sid = self.store.add_source(self.platform, card["artist"] + " - " + card["title"], card["url"],
                                       remote_id=card["campaign_id"], offered_at=card["offered_at"], origin="recipient_catalogue")
            # add_source deliberately preserves old metadata, so refresh this catalogue's date explicitly.
            row = self.store.db.execute("SELECT metadata FROM sources WHERE id=?", (sid,)).fetchone()
            metadata = json.loads(row[0]); metadata.update(offered_at=card["offered_at"], origin="recipient_catalogue",
                                                         catalogue_title=card["artist"] + " - " + card["title"])
            self.store.db.execute("UPDATE sources SET metadata=? WHERE id=?", (json.dumps(metadata), sid))
            self.store.db.commit()
            sources.append(sid)
        self.store.event(None, "catalogue", "inspected", platform=self.platform, total=len(cards),
                         note="Count matched all visible recipient cards; no play/visit/download POST")
        return {"total": len(cards), "sources": sources}

    @staticmethod
    def extension(file):
        return "." + file["type"]

    def unlock(self, source_id, release):
        previous = self.store.db.execute("SELECT source_id FROM events WHERE action='feedback' AND status='intent' "
                                         "AND json_extract(details,'$.needlepool_campaign')=? ORDER BY sequence LIMIT 1",
                                         (release["campaign_id"],)).fetchone()
        feedback_source = previous[0] if previous else source_id
        row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (feedback_source,)).fetchone()
        if row and row["status"] == "submitted":
            return
        if row and row["status"] != "pending":
            raise RuntimeError("NeedlePool feedback outcome is uncertain; inspect before retrying")
        if not row:
            rng = random.Random(source_id)
            comment = rng.choice(FEEDBACK_OPENERS) + ", " + rng.choice(FEEDBACK_CLOSERS)
            self.store.db.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?,NULL)",
                                  (feedback_source, rng.randint(7, 9), comment, 0, "pending", now()))
            self.store.db.commit()
            row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (feedback_source,)).fetchone()
        data = self.snapshot(source_id)
        payload = {"campaign_id": data["campaignId"], "rating": row["rating"], "comment": row["comment"]}
        if data.get("shareToken"):
            payload["share"] = data["shareToken"]
        self.store.db.execute("UPDATE feedback SET status='submitting' WHERE source_id=?", (feedback_source,))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "intent", platform=self.platform, rating=row["rating"], comment=row["comment"],
                         needlepool_campaign=release["campaign_id"],
                         platform_effect="stock thanks only; no planned-use claim, decline or consent")
        self.store.sync_logs()
        response = self._request("POST", "/.netlify/functions/portal-feedback", json=payload)
        try:
            if response.status_code != 200:
                raise RuntimeError(f"NeedlePool feedback HTTP {response.status_code}; inspect before retrying, never invent a review")
            result = response.json()
            protect_json(self.store.root / f"needlepool-feedback-{source_id}.dpapi", {"status": response.status_code, "json": result})
            if result.get("ok") is not True:
                raise RuntimeError("NeedlePool feedback did not confirm success; inspect before retrying")
        finally:
            response.close()
        self.store.db.execute("UPDATE feedback SET status='submitted',submitted_at=? WHERE source_id=?", (now(), feedback_source))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "submitted", platform=self.platform)
        self.store.sync_logs()
        data["initialFeedback"] = {"rating": row["rating"], "comment": row["comment"]}
        protect_json(self.store.root / f"needlepool-{source_id}.dpapi", data)

    @staticmethod
    def storage_url(url):
        try:
            parsed = urlsplit(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        return secure_url(parsed) and (
            host == HOST or host.endswith((".supabase.co", ".r2.cloudflarestorage.com", ".amazonaws.com")))

    def download(self, source_id, file, destination):
        data = self.snapshot(source_id)
        payload = {"campaign_id": data["campaignId"], "track_id": file["track_id"], "intent": "download",
                   "format": file["type"], "sid": self.sid}
        if data.get("shareToken"):
            payload["share"] = data["shareToken"]
        self.store.event(source_id, "platform_download", "intent", platform=self.platform, file_id=file["id"],
                         platform_effect="creates one sender-visible original download event")
        self.store.sync_logs()
        response = self._request("POST", "/.netlify/functions/portal-audio-url", json=payload)
        try:
            result = response.json() if response.status_code == 200 else {}
            if response.status_code != 200 or not result.get("url"):
                raise RuntimeError(f"NeedlePool original URL HTTP {response.status_code}; download gate or session needs inspection")
            url = result["url"]
        finally:
            response.close()
        for _ in range(5):
            if not self.storage_url(url):
                raise RuntimeError("NeedlePool original storage host needs verification; no file was fetched")
            self.calls += 1
            try:
                # Separate unauthenticated client, never send portal cookies to storage.
                response = requests.get(url, impersonate="chrome124", stream=True, allow_redirects=False, timeout=900)
            except Exception:
                raise RuntimeError("NeedlePool original transfer failed; signed URL suppressed") from None
            if response.status_code in REDIRECTS:
                url = urljoin(url, response.headers.get("Location", ""))
                response.close()
                continue
            try:
                if response.status_code != 200:
                    raise RuntimeError(f"NeedlePool original HTTP {response.status_code}")
                content = response.headers.get("Content-Type", "").lower()
                if any(x in content for x in ("html", "json", "text/")):
                    raise RuntimeError("NeedlePool returned a page instead of original audio")
                return write_stream(response.iter_content(1 << 20), destination,
                                    int(response.headers.get("Content-Length") or 0) or None, tolerance=0)
            finally:
                response.close()
        raise RuntimeError("NeedlePool original exceeded redirect limit")

    def campaigns(self, max_age_days=30):
        # Only canonical catalogue rows; old auth links and consent/register noise are never collected twice.
        candidates = []
        for row in self.store.db.execute("SELECT id,metadata FROM sources WHERE platform=? ORDER BY discovered_at,id", (self.platform,)):
            target = recipient_link(self.store.source_url(row["id"]))
            age = newest_offer_age(self.store, row["id"])
            if age is None:
                age = date_age(json.loads(row["metadata"]).get("offered_at"))
            if target and target[0] == "campaign" and age is not None and age <= max_age_days:
                candidates.append(row["id"])
        return candidates

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30):
        catalogue = self.discover_catalogue()
        library, report = LibraryIndex(self.store), []
        for sid in self.campaigns(max_age_days):
            try:
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
        adapter = Needlepool(store)
        if args.command == "discover-mail":
            result = discover(store, args.since, [adapter.platform])
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
