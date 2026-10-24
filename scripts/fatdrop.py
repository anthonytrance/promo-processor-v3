"""Guarded FATdrop recipient discovery and original-file collection.

The browser is only needed to renew a recipient session. A verification or CAPTCHA
page is an authentication blocker, never an expired release or an empty catalogue.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

from promo_automation import ACCOUNT, Store, atomic_text
from promo_cloud import protect_json, read_protected_json
from promo_mail import discover, newest_offer_age
from promo_pipeline import LibraryIndex, collect_release, seconds, write_stream

BASE = "https://promos.fatdrop.co.uk/"
API = "https://api.fatdrop.co.uk/api/v1/recipient/"
PAGE_HOSTS = frozenset({"promo.fatdrop.co.uk", "promos.fatdrop.co.uk", "api.fatdrop.co.uk"})
FORMATS = frozenset({"wav", "aif", "aiff", "flac", "mp3"})
# Signed-in recipient JSON names the uploaded original on a numbered audio host, observed 2026-10-24 as
# audio3-a1.fatdrop.co.uk/download/<track_id>/<uploaded file name>.wav. download_pref is the same path without
# the extension and lets the server pick a container, so it is never used.
AUDIO_HOST = re.compile(r"audio\d{0,3}-[a-z]\d{0,3}\.fatdrop\.co\.uk")
BLOCKED_ROUTE = re.compile(r"confirm|verify|subscrib|consent|register|preferences|change[_-]?email|logout|delete|remove", re.I)


class LoginRequired(RuntimeError):
    """Existing recipient login must be renewed in normal Chrome."""


class Unavailable(RuntimeError):
    """The service explicitly says the release or its audio is unavailable."""


class ContractReview(RuntimeError):
    """The response cannot safely establish the current platform contract."""


def recipient_link(url):
    p = urlsplit(url)
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or p.query or p.fragment:
        return None
    if p.hostname == "promo.fatdrop.co.uk" and re.fullmatch(r"/e/[A-Za-z0-9_-]{16,512}/?", p.path):
        return "email", p.path.rstrip("/").rsplit("/", 1)[1]
    if p.hostname == "promos.fatdrop.co.uk" and re.fullmatch(r"/promo/\d+/?", p.path):
        return "campaign", p.path.rstrip("/").rsplit("/", 1)[1]
    return None


def bootstrap_commits(text):
    """Read server-rendered JSON, without executing or attempting to repair JavaScript."""
    soup = BeautifulSoup(text, "html.parser")
    for script in soup.select("script:not([src])"):
        body = script.get_text()
        for match in re.finditer(r"\.default\(\s*(?=\{)", body):
            try:
                data, _ = json.JSONDecoder().raw_decode(body[match.end():])
            except (ValueError, TypeError):
                continue
            commits = data.get("bootstrap_store_commits") if isinstance(data, dict) else None
            if isinstance(commits, list):
                return commits
    return []


def inspect_page(text):
    """Recognise explicit gates before any metadata is accepted as a downloadable offer."""
    soup = BeautifulSoup(text, "html.parser")
    body = soup.get_text(" ", strip=True).casefold()
    if "confirm your email" in body or "send verify email" in body:
        raise LoginRequired("FATdrop requires existing-recipient email verification in normal Chrome")
    if "recaptcha.default(" in text or "this site is protected by recaptcha" in body:
        raise LoginRequired("FATdrop requires its normal browser CAPTCHA check")
    if "dj login" in body or "login with email" in body:
        raise LoginRequired("FATdrop recipient login has expired")
    if any(x in body for x in ("this promo has expired", "this promo is no longer available", "promo not found")):
        raise Unavailable("FATdrop explicitly reports this promo unavailable")
    commits = bootstrap_commits(text)
    identity, promo = None, None
    for item in commits:
        if not isinstance(item, dict):
            raise ContractReview("FATdrop recipient bootstrap schema changed")
        kind, payload = item.get("type"), item.get("payload")
        if kind == "recipient/boot" and isinstance(payload, dict):
            identity = payload.get("address")
        if kind == "promo/boot" and isinstance(payload, dict):
            promo = payload
    return identity, promo


def audio_id(url):
    """FATdrop's audio-file id, the number shared by a track's download, download_pref and streaming routes."""
    try:
        p = urlsplit(url or "")
    except ValueError:
        return None
    m = re.fullmatch(r"/(?:download|stream\w*)/(\d+)/[^/]*", p.path)
    return m.group(1) if m and AUDIO_HOST.fullmatch(p.hostname or "") else None


def original_url(url, file_id):
    """The uploaded original for exactly this audio file on FATdrop's audio hosts; no query, preference or stream."""
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return False
    return (p.scheme == "https" and not p.username and not p.password and port in (None, 443) and not p.query
            and not p.fragment and AUDIO_HOST.fullmatch(p.hostname or "") is not None
            and re.fullmatch(r"/download/" + re.escape(str(file_id)) + r"/[^/]+\.(?:wav|aif|aiff|flac|mp3)", p.path, re.I) is not None)


def _safe_download_url(url):
    p = urlsplit(url)
    return (p.scheme == "https" and not p.username and not p.password and p.port in (None, 443)
            and p.hostname in PAGE_HOSTS and not BLOCKED_ROUTE.search(p.path)
            and "download" in p.path.casefold() and not p.fragment and "format=" not in p.query.casefold())


def parse_release(data):
    """Normalize only explicitly offered original files, never streaming or a format preference.

    FATdrop's download_pref URL can manufacture another container on demand. Until
    its actual source format/precision is independently established, those tracks
    stay format_review instead of being presented as proven lossless offers.
    """
    if not isinstance(data, dict):
        raise ContractReview("FATdrop release JSON is not an object")
    if data.get("error"):
        raise ContractReview("FATdrop returned a release error; inspect its protected response")
    if data.get("available") is False or data.get("media_available") is False:
        raise Unavailable("FATdrop explicitly disabled this release or its media")
    # Only show_opt_in renders the consent prompt; accounts_not_opted_in is merely the count that prompt displays.
    if data.get("show_opt_in"):
        raise ContractReview("FATdrop requires a recipient opt-in decision; no consent was submitted")
    if data.get("public_link"):
        raise ContractReview("FATdrop public-link identity/consent contract needs review")
    campaign = data.get("campaign") or data.get("Campaign") or {}
    album = data.get("album") or data.get("Album") or {}
    playlist = data.get("playlist") or {}
    rid = str(campaign.get("id") or "")
    if not rid.isdecimal() or not isinstance(playlist.get("tracks"), list) or not playlist["tracks"]:
        raise ContractReview("FATdrop recipient campaign/track schema changed")
    fields = data.get("feedback_required_fields")
    if not isinstance(fields, int) or fields < 0:
        raise ContractReview("FATdrop feedback requirement schema changed")
    local_label = data.get("LabelLocal") or album.get("label_local") or {}
    label = local_label.get("name") if isinstance(local_label, dict) else None
    label = label or album.get("label")
    if label is not None and not isinstance(label, str):
        raise ContractReview("FATdrop label schema changed")
    tracks, review = [], []
    for row in playlist["tracks"]:
        if not isinstance(row, dict):
            raise ContractReview("FATdrop track row schema changed")
        tid, artist, title = str(row.get("track_id") or ""), row.get("artist"), row.get("title")
        if not tid.isdecimal() or not isinstance(artist, str) or not artist.strip() or not isinstance(title, str) or not title.strip():
            raise ContractReview("FATdrop track identity is incomplete")
        name = artist.strip() + " - " + title.strip()
        urls = row.get("urls") or {}
        original = urls.get("download") or ""
        kind = Path(urlsplit(original).path).suffix.casefold().lstrip(".")
        files, reason = [], None
        if row.get("allow_download") is False:
            reason = "downloads_disabled"
        elif int(data.get("download_flags") or 0) & 2:
            reason = "zip_only_source_format_unverified"
        elif (kind in FORMATS and (aid := audio_id(original)) and original_url(original, aid)
              and all(audio_id(urls.get(k)) == aid for k in ("download_pref", "streaming") if urls.get(k))):
            # The uploaded original is offered beside the preference route; only the original is taken. Its audio id
            # must match this track's own stream and preference routes, so a file from another track cannot slip in.
            files.append({"id": aid, "type": kind, "url": original, "source_format": kind, "track_id": tid})
        elif row.get("use_download_pref"):
            reason = "original_format_unverified"
        else:
            # Page-host or relative download routes were never observed live; only the audio-host original qualifies.
            reason = "original_endpoint_unverified"
        if reason:
            review.append({"track": name, "state": reason})
        tracks.append({"name": name, "artist": artist.strip(), "duration": seconds(row.get("track_length")),
                       "track_id": tid, "files": files, "unavailable_reason": reason})
    access = data.get("access") or {}
    return {"title": campaign.get("name") or (str(album.get("artist") or "") + " - " + str(album.get("title") or "")).strip(" -"),
            "remote_id": rid, "label": label, "catalogue": album.get("catalogue_number"),
            "gated": fields != 0 and not bool(access.get("has_left_feedback")), "feedback_required_fields": fields,
            "feedback_key": data.get("k"), "tracks": tracks, "availability": "metadata_inspected",
            "review_tracks": review, "original_contract_live_verified": False}


class Fatdrop:
    # The coordinator plans FATdrop but collects nothing until one bounded original pilot is verified.
    collection_enabled = False
    collection_disabled_reason = "FATdrop collection stays off until its single original pilot is verified"

    def __init__(self, store, session=None):
        self.store, self.calls = store, 0
        self.session = session if session is not None else requests.Session(impersonate="chrome124")
        path = store.root / "fatdrop-session.dpapi"
        self.identity = None
        if path.exists():
            saved = read_protected_json(path)
            self.identity = saved.get("identity")
            for cookie in saved.get("cookies", []):
                domain = cookie.get("domain", "").lstrip(".")
                if domain == "fatdrop.co.uk" or domain in PAGE_HOSTS:
                    self.session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie.get("path", "/"))

    def request(self, method, url, *, download=False, **kwargs):
        """No login/confirmation/consent redirects, cookie leakage or implicit POST retry."""
        location = urljoin(BASE, url)
        timeout = kwargs.pop("timeout", 60)
        for _ in range(5):
            p = urlsplit(location)
            allowed_hosts = PAGE_HOSTS | ({"d-cdn.fatdrop.co.uk"} if download else set())
            audio_host = download and AUDIO_HOST.fullmatch(p.hostname or "") is not None
            if (p.scheme != "https" or p.username or p.password or p.port not in (None, 443)
                    or (p.hostname not in allowed_hosts and not audio_host)
                    or BLOCKED_ROUTE.search(p.path)):
                raise ContractReview("FATdrop request/redirect reached an unapproved host or account-action route")
            if p.path.startswith(("/login", "/s/")):
                raise LoginRequired("FATdrop requires recipient login in normal Chrome")
            self.calls += 1
            try:
                response = self.session.request(method, location, allow_redirects=False, timeout=timeout, **kwargs)
            except Exception as error:
                raise RuntimeError("FATdrop request failed: " + type(error).__name__) from None
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            target = response.headers.get("Location")
            response.close()
            if method != "GET":
                raise ContractReview("FATdrop reaction redirected; inspect outcome before retrying")
            if not target:
                raise ContractReview("FATdrop redirect omitted its destination")
            location = urljoin(location, target)
        raise ContractReview("FATdrop redirect limit reached")

    def json_get(self, url):
        response = self.request("GET", url, headers={"Accept": "application/json"})
        try:
            if response.status_code in (401, 403):
                raise LoginRequired("FATdrop recipient API requires renewed authentication")
            if response.status_code != 200:
                raise RuntimeError(f"FATdrop recipient API HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError:
                inspect_page(response.text)
                raise ContractReview("FATdrop recipient API returned non-JSON content") from None
        finally:
            response.close()

    def catalogue(self, limit=50):
        """Count-checked offset pagination across ALL promos, independent of opened state."""
        if self.identity != ACCOUNT:
            raise LoginRequired("FATdrop needs a protected session verified for the configured recipient mailbox")
        self.store.event(None, "catalogue", "intent", platform="fatdrop", filter="all")
        self.store.sync_logs()
        stats = self.json_get(API + "promos/stats")
        total = stats.get("all") if isinstance(stats, dict) else None
        if type(total) is not int or total < 0:
            raise ContractReview("FATdrop catalogue count schema changed")
        rows, seen, offset = [], set(), 0
        while offset < total:
            page = self.json_get(API + "promos?" + urlencode({"filter": "all", "limit": limit, "offset": offset}))
            campaigns = page.get("campaigns") if isinstance(page, dict) else None
            if not isinstance(campaigns, list) or not campaigns or len(campaigns) > limit:
                raise ContractReview("FATdrop catalogue pagination is incomplete")
            for row in campaigns:
                rid = str(row.get("id") or "")
                if not rid.isdecimal() or rid in seen:
                    raise ContractReview("FATdrop catalogue repeated or omitted a campaign identity")
                seen.add(rid)
                rows.append(row)
            offset += len(campaigns)
        after = self.json_get(API + "promos/stats")
        if len(rows) != total or not isinstance(after, dict) or after.get("all") != total:
            raise ContractReview("FATdrop catalogue changed or its final count disagrees; repeat discovery")
        for row in rows:
            self.store.add_source("fatdrop", row.get("name") or "FATdrop campaign", BASE + "promo/" + str(row["id"]),
                                  remote_id=str(row["id"]), catalogue_access_created=(row.get("access") or {}).get("created"))
        self.store.event(None, "catalogue", "inspected", platform="fatdrop", campaigns=total,
                         pagination="offset/count verified", requests=self.calls)
        return {"campaigns": total, "complete": True, "calls": self.calls}

    def snapshot(self, source_id, refresh=False):
        row = self.store.db.execute("SELECT * FROM sources WHERE id=? AND platform='fatdrop'", (source_id,)).fetchone()
        link = self.store.source_url(source_id) if row else ""
        identity = recipient_link(link)
        if identity is None:
            raise ValueError("Unknown FATdrop recipient release; login/confirmation/footer routes are excluded")
        path = self.store.root / ("fatdrop-" + source_id + ".dpapi")
        if path.exists() and not refresh:
            return read_protected_json(path)
        self.store.event(source_id, "view", "intent", platform="fatdrop", platform_effect="recipient page may record opening")
        self.store.sync_logs()
        if identity[0] == "campaign":
            if self.identity != ACCOUNT:
                raise LoginRequired("FATdrop session is not verified for the configured recipient mailbox")
            data = self.json_get(BASE + "promos/" + identity[1] + "?beta=1&basic=1&dmo=1")
        else:
            response = self.request("GET", link)
            try:
                if response.status_code in (404, 410):
                    raise Unavailable(f"FATdrop recipient page HTTP {response.status_code}")
                if response.status_code in (401, 403):
                    raise LoginRequired("FATdrop recipient link requires renewed authentication")
                if response.status_code != 200:
                    raise RuntimeError(f"FATdrop recipient page HTTP {response.status_code}")
                address, data = inspect_page(response.text)
                if address != ACCOUNT:
                    raise LoginRequired("FATdrop recipient page has no verified configured-mailbox identity")
                if data is None:
                    raise ContractReview("FATdrop recipient page did not provide its campaign bootstrap inventory")
            finally:
                response.close()
        release = parse_release(data)
        protect_json(path, release)
        self.store.event(source_id, "view", "inspected", platform="fatdrop", campaign=release["remote_id"],
                         tracks=len(release["tracks"]), review_tracks=len(release["review_tracks"]),
                         availability="metadata only, original transfer not live verified")
        return release

    def release(self, source_id):
        return self.snapshot(source_id)

    @staticmethod
    def extension(file):
        return "." + file["type"]

    def unlock(self, source_id, release):
        # This is intentionally closed until a live authenticated recipient sample
        # confirms the exact reaction response and original-source contract.
        raise ContractReview("FATdrop feedback is disabled pending authenticated live contract verification")

    def download(self, source_id, file, destination):
        # Only the uploaded original route qualifies; a preference-generated container is never a quality upgrade.
        if not file.get("url") or not original_url(file["url"], file.get("id", "")):
            raise ContractReview("FATdrop original endpoint is not verified for this track")
        self.store.event(source_id, "platform_download", "intent", platform="fatdrop", track_id=file["id"],
                         platform_effect="original download is recorded for the sender")
        self.store.sync_logs()
        response = self.request("GET", file["url"], download=True, stream=True, timeout=900)
        try:
            if response.status_code in (401, 403):
                raise LoginRequired("FATdrop original download requires renewed authentication")
            if response.status_code != 200:
                raise RuntimeError(f"FATdrop original HTTP {response.status_code}")
            kind = response.headers.get("Content-Type", "").casefold()
            if any(x in kind for x in ("html", "json", "text/", "zip")):
                raise ContractReview("FATdrop returned a page or archive instead of the original file")
            size = int(response.headers.get("Content-Length") or 0) or None
            return write_stream(response.iter_content(1 << 20), destination, size, tolerance=0)
        finally:
            response.close()

    def offer_age(self, source_id):
        """Days since the newest mail for this source, else since the catalogue says the promo reached the account."""
        age = newest_offer_age(self.store, source_id)
        if age is not None:
            return age
        row = self.store.db.execute("SELECT metadata FROM sources WHERE id=?", (source_id,)).fetchone()
        created = json.loads(row[0] or "{}").get("catalogue_access_created") if row else None
        try:
            moment = datetime.strptime(created, "%Y-%m-%dT%H:%M:%S%z")
        except (TypeError, ValueError):
            return None
        return (datetime.now(timezone.utc) - moment).total_seconds() / 86400

    def campaigns(self, max_age_days=30):
        """Recipient links inside the window, one per known campaign id; login and footer links are left out."""
        best, loose = {}, []
        for row in self.store.db.execute("SELECT id,remote_id FROM sources WHERE platform='fatdrop' ORDER BY discovered_at,id"):
            link = recipient_link(self.store.source_url(row[0]))
            age = self.offer_age(row[0]) if link else None
            if age is None or age > max_age_days:
                continue
            campaign = row[1] or (link[1] if link[0] == "campaign" else None)
            if campaign is None:
                loose.append(row[0])
            elif campaign not in best or age < best[campaign][1]:
                best[campaign] = (row[0], age)
        return [sid for sid, _ in best.values()] + loose

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30):
        library, reports, login_blocker = LibraryIndex(self.store), [], None
        try:
            catalogue = self.catalogue()
        except (LoginRequired, ContractReview) as error:
            catalogue = {"complete": False, "error": str(error)}
        rows = self.store.db.execute("SELECT * FROM sources WHERE platform='fatdrop'").fetchall()
        ages = {row["id"]: self.offer_age(row["id"]) for row in rows}
        rows = sorted(rows, key=lambda r: ages[r["id"]] if ages[r["id"]] is not None else float("inf"))
        seen_campaigns = set()
        for row in rows:
            link = recipient_link(self.store.source_url(row["id"]))
            result = {"source_id": row["id"], "release": row["subject"]}
            if not link:
                result["state"] = "nonrelease_link_excluded"
            elif (age := ages[row["id"]]) is None or age > max_age_days:
                result["state"] = "undated_or_too_old"
            elif login_blocker:
                result.update(state="login_required", error=login_blocker)
            else:
                try:
                    release = self.snapshot(row["id"], refresh=True)
                    if release["remote_id"] in seen_campaigns:
                        result["state"] = "same_campaign_already_reported"
                    else:
                        seen_campaigns.add(release["remote_id"])
                        if not dry_run:
                            raise ContractReview("FATdrop collection remains disabled until its single original pilot is verified")
                        planned = collect_release(self.store, self, row["id"], library, dry_run=True)
                        result.update(state="dry_run", tracks=planned["tracks"], review_tracks=release["review_tracks"],
                                      availability="metadata inspected; transfer unverified")
                except LoginRequired as error:
                    login_blocker = str(error)
                    result.update(state="login_required", error=login_blocker)
                except Unavailable as error:
                    result.update(state="unavailable", error=str(error))
                except ContractReview as error:
                    result.update(state="needs_review", error=str(error))
                except Exception as error:
                    result.update(state="exception", error="FATdrop operation failed: " + type(error).__name__)
            reports.append(result)
        self.store.sync_logs(force=True)
        return {"platform": "fatdrop", "dry_run": dry_run, "calls": self.calls, "releases": reports,
                "catalogue_complete": catalogue.get("complete") is True, "catalogue_error": catalogue.get("error"),
                "live_original_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover-mail")
    commands.add_parser("catalogue")
    batch = commands.add_parser("batch")
    batch.add_argument("--dry-run", action="store_true")
    batch.add_argument("--report", type=Path)
    collect = commands.add_parser("collect")
    collect.add_argument("source_id")
    collect.add_argument("--dry-run", action="store_true")
    collect.add_argument("--max-assets", type=int, help="cap new transfers after normal version selection and dedup")
    args = parser.parse_args()
    store = Store()
    try:
        adapter = Fatdrop(store)
        if args.command == "discover-mail":
            result = discover(store, "30d", ["fatdrop"], sender_domain="fatdrop.co.uk")
        elif args.command == "catalogue":
            result = adapter.catalogue()
        elif args.command == "batch":
            result = adapter.batch(dry_run=args.dry_run)
            if args.report:
                # Reports contain release names and account-derived state, so the
                # caller must keep them in the runtime root, outside the skill.
                target = args.report.resolve()
                if not target.is_relative_to(store.root.resolve()):
                    raise ValueError("FATdrop reports must stay in the protected runtime state tree")
                atomic_text(target, json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if not args.dry_run and args.max_assets is None:
                raise ContractReview("FATdrop live collection is limited to bounded pilots; pass --max-assets")
            # Feedback stays closed: only releases the recipient has already reviewed (or ungated ones) can transfer.
            result = collect_release(store, adapter, args.source_id, LibraryIndex(store), dry_run=args.dry_run,
                                     max_assets=args.max_assets)
            store.sync_logs(force=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (LoginRequired, ContractReview, Unavailable) as error:
        print(json.dumps({"platform": "fatdrop", "state": type(error).__name__, "error": str(error)}))
        return 2
    finally:
        store.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
