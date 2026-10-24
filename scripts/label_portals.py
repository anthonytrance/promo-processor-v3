"""Bounded recipient adapters for verified independent label portals.

NYX and Black Hand publish explicit original download links. Invitations, tracking
wrappers and portal homepages stay visible as blockers rather than guessed releases.
"""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

from promo_automation import Store
from promo_cloud import protect_json, read_protected_json
from promo_mail import newest_offer_age
from promo_pipeline import LibraryIndex, collect_release, write_stream

NYX = "promo.nyxcreativestudio.com"
BLACKHAND = "promo.blackhandmusic.net"
INVITATION = "promo.progressivevibesmusic.com"


class PortalBlocked(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def portal_kind(url):
    p = urlsplit(url)
    try:
        good_port = p.port in (None, 443)
    except ValueError:
        good_port = False
    if p.scheme != "https" or p.username or p.password or p.fragment or not good_port:
        return None
    if p.hostname == NYX and p.path in ("", "/") and not p.query:
        return "nyx"
    if (p.hostname == BLACKHAND and re.fullmatch(r"/releases/[A-Za-z0-9_.~-]+/?", p.path)
            and set(parse_qs(p.query, keep_blank_values=True)) <= {"refer"}):
        return "blackhand"
    if p.hostname == INVITATION:
        return "invitation"
    if p.hostname == "music.biginstrumental.com":
        return "tracking_wrapper"
    if p.hostname == BLACKHAND and p.path in ("", "/", "/index.php") and not p.query:
        return "homepage"
    return None


def original_url(base, href, kind):
    """Only the page's explicit same-origin original endpoint or campaign file."""
    target = urljoin(base, href)
    p, b = urlsplit(target), urlsplit(base)
    try:
        good_port = p.port in (None, 443)
    except ValueError:
        good_port = False
    if (p.scheme != "https" or p.hostname != b.hostname or p.username or p.password
            or not good_port or p.query or p.fragment):
        raise RuntimeError("Label portal original left its verified origin")
    path = unquote(p.path)
    if kind == "nyx":
        allowed = path == "/download"
    else:
        folder = b.path if b.path.endswith("/") else b.path + "/"
        filename = path[len(folder):] if path.startswith(folder) else ""
        allowed = (filename not in ("", ".", "..") and "/" not in filename and "\\" not in filename
                   and Path(filename).suffix.lower() in {".wav", ".aif", ".aiff", ".flac", ".mp3"})
    if not allowed:
        raise RuntimeError("Label portal link is not an explicit original audio file")
    return target


def parse_nyx(page, metadata, url):
    p = BeautifulSoup(page, "html.parser")
    link = p.select_one("a#downloadTrack[href]")
    artist, title, label = (str(metadata.get(k) or "").strip() for k in ("artist", "title", "label"))
    if not link or not artist or not title or not metadata.get("audio"):
        raise RuntimeError("NYX release metadata or original download link changed")
    target = original_url(url, link["href"], "nyx")
    return {"title": f"{artist} - {title}", "label": label or None, "gated": False,
            "remote_id": metadata.get("id"),
            "portal_kind": "nyx", "release_date": metadata.get("release_date"),
            "collection_blocker": "NYX serves a mutable /download endpoint without a release-bound file identity; original integrity needs verification.",
            "warnings": ["Original format is opaque until its audio header is checked; no lossless claim from the preview.",
                         "Live collection is blocked until the original can be bound to this release identity."],
            "tracks": [{"name": f"{artist} - {title}", "artist": artist, "duration": None,
                        "files": [{"type": "original", "url": target}]}]}


def parse_blackhand(page, url):
    p = BeautifulSoup(page, "html.parser")
    title, artist = p.select_one("#innertitle"), p.select_one("#innerartist")
    if title is None or artist is None:
        raise RuntimeError("Black Hand recipient page has no release metadata")
    tracks, grouped = [], {}
    for a in p.select("a[download][href]"):
        suffix = Path(unquote(urlsplit(a["href"]).path)).suffix.lower()
        if suffix not in {".wav", ".aif", ".aiff", ".flac", ".mp3"}:
            continue
        target = original_url(url, a["href"], "blackhand")
        filename = unquote(urlsplit(target).path.rsplit("/", 1)[-1])
        name = Path(filename).stem
        track_artist, separator, track_title = name.partition(" - ")
        if not separator or not track_artist.strip() or not track_title.strip():
            raise RuntimeError("Black Hand original filename has ambiguous artist or title")
        if name not in grouped:
            grouped[name] = {"name": name, "artist": track_artist, "duration": None, "files": []}
            tracks.append(grouped[name])
        grouped[name]["files"].append({"type": suffix[1:], "url": target})
    if not tracks:
        raise RuntimeError("Black Hand recipient page has no explicit original audio links")
    # Black Hand is a PR company. Its footer is not evidence of the release's actual imprint.
    return {"title": artist.get_text(" ", strip=True) + " - " + title.get_text(" ", strip=True),
            "label": None, "gated": False, "portal_kind": "blackhand", "tracks": tracks,
            "warnings": ["Actual imprint is not stated on this portal; research before a live pilot."]}


class LabelPortals:
    def __init__(self, store):
        self.store, self.calls = store, 0
        self.session = requests.Session(impersonate="chrome124")

    def request(self, url, stream=False):
        initial = urlsplit(url)
        target = url
        for _ in range(4):
            p = urlsplit(target)
            try:
                good_port = p.port in (None, 443)
            except ValueError:
                good_port = False
            if (p.scheme != "https" or p.hostname != initial.hostname or p.username or p.password
                    or not good_port):
                raise RuntimeError("Label portal redirect left its verified origin")
            self.calls += 1
            try:
                r = self.session.get(target, allow_redirects=False, stream=stream, timeout=60)
            except Exception as error:
                raise RuntimeError("Label portal request failed: " + type(error).__name__) from None
            if r.status_code not in {301, 302, 303, 307, 308}:
                return r, target
            following = urljoin(target, r.headers.get("Location", ""))
            r.close()
            # Only canonical trailing-slash redirects have been observed. Login, tracking
            # and transformed-file routes require fresh inspection rather than a guess.
            f = urlsplit(following)
            if f.path != p.path.rstrip("/") + "/" or f.query != p.query:
                raise RuntimeError("Label portal redirect changed the verified recipient route")
            target = following
        raise RuntimeError("Label portal redirect limit reached")

    def snapshot(self, source_id, refresh=False):
        row = self.store.db.execute("SELECT platform FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None or row["platform"] != "label-portal":
            raise ValueError("Unknown label portal source")
        url = self.store.source_url(source_id)
        kind = portal_kind(url)
        blocked = {"invitation": ("consent_required", "This is a promo pool invitation, not an accepted recipient release."),
                   "tracking_wrapper": ("unsupported_tracking_wrapper", "A tracking wrapper is not an original download link."),
                   "homepage": ("not_a_release", "Portal footer/homepage has no recipient release identity."),
                   None: ("unsupported_route", "Label portal route has not been verified.")}
        if kind in blocked:
            raise PortalBlocked(*blocked[kind])
        path = self.store.root / f"label-portal-{source_id}.dpapi"
        if path.exists() and not refresh:
            return read_protected_json(path)
        self.store.event(source_id, "view", "intent", platform="label-portal", portal=kind,
                         platform_effect="recipient page may record opening")
        self.store.sync_logs()
        r, resolved = self.request(url)
        try:
            if r.status_code in {404, 410}:
                raise PortalBlocked("unavailable", "Label portal recipient page is no longer available.")
            if r.status_code != 200:
                raise RuntimeError(f"Label portal recipient HTTP {r.status_code}")
            html = r.text
        finally:
            r.close()
        if kind == "nyx":
            r, _ = self.request(urljoin(resolved, "/api/promo"))
            try:
                if r.status_code != 200:
                    raise RuntimeError(f"NYX metadata HTTP {r.status_code}")
                release = parse_nyx(html, r.json(), resolved)
            finally:
                r.close()
        else:
            release = parse_blackhand(html, resolved)
        release["source_id"] = source_id
        protect_json(path, release)
        self.store.event(source_id, "view", "inspected", platform="label-portal", portal=kind,
                         tracks=len(release["tracks"]), feedback="optional, not submitted")
        return release

    def release(self, source_id):
        return self.snapshot(source_id)

    @staticmethod
    def extension(file):
        return ".bin" if file["type"] == "original" else "." + file["type"]

    def unlock(self, source_id, release):
        raise RuntimeError("Verified label portals do not require feedback; never post optional public reviews")

    def download(self, source_id, file, destination):
        release = self.snapshot(source_id)
        if release.get("collection_blocker"):
            raise PortalBlocked("integrity_review", release["collection_blocker"])
        if not any(file == offered for t in release["tracks"] for offered in t["files"]):
            raise RuntimeError("Original is not in the protected release inventory")
        original_url(self.store.source_url(source_id), file["url"], release["portal_kind"])
        r, _ = self.request(file["url"], stream=True)
        try:
            if r.status_code != 200:
                raise RuntimeError(f"Label portal original HTTP {r.status_code}")
            media = r.headers.get("Content-Type", "").lower().split(";", 1)[0]
            if media.startswith("text/") or media in {"application/json", "application/zip"}:
                raise RuntimeError("Label portal original returned a page or non-audio payload")
            expected = int(r.headers["Content-Length"]) if r.headers.get("Content-Length", "").isdigit() else None
            return write_stream(r.iter_content(256 * 1024), destination, expected, tolerance=0)
        finally:
            r.close()


def batch(store, adapter, dry_run=True, max_age_days=30, max_assets=None):
    library, results = LibraryIndex(store), []
    for row in store.db.execute("SELECT id,subject FROM sources WHERE platform='label-portal' ORDER BY discovered_at").fetchall():
        age = newest_offer_age(store, row["id"])
        if age is None or age > max_age_days:
            results.append({"source_id": row["id"], "release": row["subject"], "status": "undated_or_too_old"})
            continue
        try:
            adapter.snapshot(row["id"], refresh=True)
            result = collect_release(store, adapter, row["id"], library, dry_run=dry_run, max_assets=max_assets)
            results.append({"source_id": row["id"], "status": "planned" if dry_run else "collected", **result})
        except PortalBlocked as error:
            results.append({"source_id": row["id"], "release": row["subject"], "status": error.status, "reason": str(error)})
        except Exception as error:
            results.append({"source_id": row["id"], "release": row["subject"], "status": "failed", "error": str(error)[:200]})
    store.sync_logs()
    return {"platform": "label-portal", "dry_run": dry_run, "requests": adapter.calls, "releases": results}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect")
    c.add_argument("source_id")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--refresh", action="store_true")
    c.add_argument("--max-assets", type=int)
    b = sub.add_parser("batch")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--max-age-days", type=int, default=30)
    b.add_argument("--max-assets", type=int)
    a = p.parse_args()
    store = Store()
    adapter = LabelPortals(store)
    if a.command == "collect":
        adapter.snapshot(a.source_id, refresh=a.refresh)
        result = collect_release(store, adapter, a.source_id, LibraryIndex(store), dry_run=a.dry_run, max_assets=a.max_assets)
    else:
        result = batch(store, adapter, a.dry_run, a.max_age_days, a.max_assets)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
