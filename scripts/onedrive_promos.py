"""OneDrive recipient shares: bounded metadata inventory and original-file collection.

Anonymous migrated shares use the website's native v2 API after opening the supplied
recipient URL. Legacy personal shares use the v1 API. No login, consent, link
redemption with durable permissions, recovery, or preview audio is automated.
"""
import argparse
import base64
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit

from curl_cffi import requests

from dropbox_promos import DropboxPromos
from process_promos import load_label_codes, parse_filename
from promo_automation import Store
from promo_cloud import protect_json, read_protected_json
from promo_mail import discover, newest_offer_age
from promo_pipeline import AUDIO_SUFFIXES, LibraryIndex, collect_release, write_stream
from soundcloud import mail_label

MAX_METADATA = 8 * 1024**2
SHARE_HOSTS = ("onedrive.live.com", "1drv.ms")
ORIGINAL_HOSTS = ("1drv.com", "microsoftpersonalcontent.com", "storage.live.com", "onedrive.live.com", "api.onedrive.com")


class ShareUnavailable(RuntimeError):
    """Explicit expiry/deletion or a provider 404, distinct from an empty share."""


class ShareAccessRequired(RuntimeError):
    """The exact recipient link needs login, password, consent or other access."""


def checked_url(url, domains):
    """Only HTTPS provider hosts, with no credentials, custom ports or local aliases."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if (parsed.scheme != "https" or parsed.username or parsed.password or not valid_port
            or not any(host == d or host.endswith("." + d) for d in domains)):
        raise RuntimeError("Direct share URL left its allowed provider hosts")
    return parsed


def safe_name(name):
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or any(c in name for c in ("/", "\\", ":", "\0")) or name.endswith((".", " "))):
        raise RuntimeError("Unsafe direct share item name")
    return name


def metadata_filename(name):
    """Remove a DJ key/BPM prefix and normalize an explicit underscore mix suffix."""
    suffix = Path(name).suffix
    stem = name[:-len(suffix)] if suffix else name
    stem = re.sub(r"^(?:[1-9]|1[0-2])[AB]\s*-\s*\d{2,3}(?:\.\d+)?\s*-\s*", "", stem, flags=re.I)
    match = re.fullmatch(r"([^_]+)_(.+)_(EXTENDED(?:[ _]MIX)?|RADIO(?:[ _]EDIT)?|ORIGINAL(?:[ _]MIX)?)", stem, re.I)
    if match and " - " not in stem:
        artist, title, mix = match.groups()
        mix = mix.replace("_", " ").title()
        mix = {"Extended": "Extended Mix", "Radio": "Radio Edit", "Original": "Original Mix"}.get(mix, mix)
        stem = artist + " - " + title.replace("_", " ") + " (" + mix + ")"
    return stem + suffix


class ShareHTTP:
    """Requests never follow redirects implicitly or log recipient URLs on failures."""
    def __init__(self):
        self.session = requests.Session(impersonate="chrome124")
        self.calls = 0

    def close(self):
        self.session.close()

    def response(self, method, url, domains, **kwargs):
        checked_url(url, domains)
        self.calls += 1
        try:
            return self.session.request(method, url, allow_redirects=False, stream=True,
                                        timeout=(30, 180), **kwargs)
        except requests.RequestException as error:
            raise RuntimeError("Direct share network request failed (" + type(error).__name__ + ")") from None

    def document(self, url, domains, method="GET", **kwargs):
        response = self.response(method, url, domains, **kwargs)
        try:
            status, headers = response.status_code, dict(response.headers)
            if status in (301, 302, 303, 307, 308):
                return status, headers, b""
            data = bytearray()
            for block in response.iter_content(65536):
                data.extend(block)
                if len(data) > MAX_METADATA:
                    raise RuntimeError("Direct share metadata exceeded its bounded response size")
            return status, headers, bytes(data)
        finally:
            response.close()

    def page(self, url, domains, path_check=None):
        seen = set()
        for _ in range(6):
            checked_url(url, domains)
            if path_check:
                path_check(url)
            if url in seen:
                raise RuntimeError("Direct share redirect loop")
            seen.add(url)
            status, headers, body = self.document(url, domains)
            if status in (301, 302, 303, 307, 308):
                location = headers.get("Location") or headers.get("location")
                if not location:
                    raise RuntimeError("Direct share redirect has no destination")
                url = urljoin(url, location)
                continue
            return url, status, headers, body
        raise RuntimeError("Direct share exceeded its redirect limit")

    def json(self, url, domains, method="GET", **kwargs):
        status, headers, body = self.document(url, domains, method, **kwargs)
        if status in (401, 403):
            raise ShareAccessRequired("Direct share requires account, password or access verification")
        if status in (404, 410):
            raise ShareUnavailable("Direct share is unavailable (provider HTTP " + str(status) + ")")
        if status != 200:
            raise RuntimeError("Direct share metadata HTTP " + str(status))
        try:
            result = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError("Direct share returned a page instead of metadata") from None
        if not isinstance(result, dict):
            raise RuntimeError("Direct share metadata is not an object")
        return result

    def original(self, url, destination, domains, size, sha1=None, sha256=None, md5=None):
        """Publish only a complete original with exact inventory size and offered hashes."""
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise RuntimeError("Direct share original has no positive inventory size")
        destination = Path(destination)
        temporary = destination.with_name(destination.name + ".share")
        try:
            for _ in range(6):
                response = self.response("GET", url, domains)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    response.close()
                    if not location:
                        raise RuntimeError("Direct share original redirect has no destination")
                    url = urljoin(url, location)
                    continue
                try:
                    if response.status_code in (401, 403):
                        raise ShareAccessRequired("Direct share original access was denied")
                    if response.status_code in (404, 410):
                        raise ShareUnavailable("Direct share original is no longer available")
                    if response.status_code != 200:
                        raise RuntimeError("Direct share original HTTP " + str(response.status_code))
                    kind = response.headers.get("Content-Type", "").lower()
                    if any(x in kind for x in ("text/html", "application/json", "text/plain")):
                        raise RuntimeError("Direct share returned a page instead of the original")
                    def bounded_chunks():
                        total = 0
                        for chunk in response.iter_content(1 << 20):
                            total += len(chunk)
                            if total > size:
                                raise RuntimeError("Direct share original exceeds its inventory size")
                            yield chunk
                    count, digest = write_stream(bounded_chunks(), temporary, size, tolerance=0)
                finally:
                    response.close()
                for algorithm, expected in (("sha1", sha1), ("sha256", sha256), ("md5", md5)):
                    if expected:
                        computed = hashlib.new(algorithm)
                        with temporary.open("rb") as stream:
                            for block in iter(lambda: stream.read(1 << 20), b""):
                                computed.update(block)
                        if computed.hexdigest().lower() != expected.lower():
                            raise RuntimeError("Direct share original " + algorithm.upper() + " differs from inventory")
                os.replace(temporary, destination)
                return count, digest
            raise RuntimeError("Direct share original exceeded its redirect limit")
        finally:
            for partial in (temporary, temporary.with_name(temporary.name + ".partial")):
                if partial.exists():
                    partial.unlink()


class DirectSharePromos(DropboxPromos):
    """Shared pipeline contract, without constructing the real Dropbox source client."""
    def __init__(self, store, http=None):
        self.store, self.http, self.lengths = store, http or ShareHTTP(), {}
        self.inventories = {}
        self.codes = load_label_codes(str(Path(__file__).with_name("label_codes.csv")))

    @property
    def calls(self):
        return self.http.calls

    def close(self):
        self.http.close()

    def _read_inventory(self, source_id, cache):
        """Disk metadata is reusable only for this exact protected recipient source.

        Old unbound snapshots are refreshed once. Signed original URLs are never
        trusted from this cache; each downloader obtains fresh provider permission.
        """
        if not cache.exists():
            return None
        try:
            saved = read_protected_json(cache)
            if (saved.get("schema") != 1 or saved.get("platform") != self.platform
                    or saved.get("source_id") != source_id
                    or saved.get("source_url_sha256") != hashlib.sha256(
                        self.store.source_url(source_id).encode()).hexdigest()):
                return None
            inventory = saved["inventory"]
            self._validate_inventory(inventory)
            return inventory
        except Exception:
            # An unreadable DPAPI blob is a cache miss, never proof of ownership.
            return None

    def _save_inventory(self, source_id, cache, inventory):
        self._validate_inventory(inventory)
        protect_json(cache, {"schema": 1, "platform": self.platform, "source_id": source_id,
                            "source_url_sha256": hashlib.sha256(self.store.source_url(source_id).encode()).hexdigest(),
                            "inventory": inventory})

    def _validate_inventory(self, inventory):
        if (not isinstance(inventory, dict) or not isinstance(inventory.get("name"), str)
                or not inventory.get("share_key") or not isinstance(inventory.get("files"), list)
                or isinstance(inventory.get("file_count"), bool)
                or inventory.get("file_count") != len(inventory["files"])
                or len(inventory["files"]) > 10000):
            raise RuntimeError("Direct-share metadata cache is incomplete")
        seen = set()
        for entry in inventory["files"]:
            if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                    or not entry["id"] or entry["id"] in seen
                    or isinstance(entry.get("size"), bool) or not isinstance(entry.get("size"), int)
                    or entry["size"] < 0 or not isinstance(entry.get("is_downloadable"), bool)):
                raise RuntimeError("Direct-share metadata cache has invalid file identity or size")
            safe_name(entry.get("name"))
            seen.add(entry["id"])

    def length(self, source_id, entry):
        # Metadata-only dry runs never fetch any audio bytes to determine duration.
        if entry.get("local_path"):
            import mutagen
            audio = mutagen.File(entry["local_path"])
            return audio.info.length if audio else None
        return entry.get("duration")

    def release(self, source_id):
        inventory = self.snapshot(source_id)
        tracks, warnings = [], []
        subject, metadata = self.store.db.execute("SELECT subject,metadata FROM sources WHERE id=?", (source_id,)).fetchone()
        fallback = mail_label(subject, json.loads(metadata or "{}").get("sender"))
        for entry in inventory["files"]:
            suffix = Path(entry["name"]).suffix.lower()
            if suffix not in AUDIO_SUFFIXES:
                if suffix in {".zip", ".rar"}:
                    warnings.append({"file": entry["name"], "reason": "archive inventory requires an explicitly approved original transfer"})
                continue
            info = parse_filename(metadata_filename(entry["name"]), self.codes)
            artist, title, label = info.get("artist"), info.get("title"), info.get("label")
            if label and label.startswith("UNKNOWN:"):
                label = None
            name = f"{artist} - {title}" if artist else title or Path(entry["name"]).stem
            offer = [{"id": entry["id"], "type": suffix[1:], "entry": entry}] if entry.get("is_downloadable", True) else []
            existing = next((t for t in tracks if t["name"] == name), None)
            if existing:
                existing["files"] += offer
                existing["label"] = existing["label"] or label or fallback
                continue
            tracks.append({"name": name, "duration": entry.get("duration"), "artist": artist,
                           "title": title, "label": label or fallback, "files": offer})
        return {"title": inventory["name"], "label": None, "gated": False, "tracks": tracks, "warnings": warnings}

    def prepare_source(self, source_id):
        """Metadata inspection only; never hide a whole-archive transfer in a dry run."""
        self.snapshot(source_id)

    def prepare_collection_source(self, source_id):
        # OneDrive/WeTransfer archive collection remains explicitly unsupported.
        # Do not inherit Dropbox's archive rehydration transfer by accident.
        self.prepare_source(source_id)

    def cleanup_source(self, source_id, result):
        pass  # Originals use the shared pipeline's zero-retention staging folder.

    def unlock(self, source_id, release):
        raise ShareAccessRequired("Direct shares never submit automatic feedback or bypass access gates")

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30):
        library, reports, unavailable, access, failed, done = LibraryIndex(self.store), [], [], [], [], set()
        rows = self.store.db.execute("SELECT id FROM sources WHERE platform=? ORDER BY discovered_at DESC,id", (self.platform,)).fetchall()
        for (source_id,) in rows:
            age = newest_offer_age(self.store, source_id)
            if age is None or age > max_age_days:
                continue
            try:
                # A standalone batch starts a new discovery of mutable shares.
                # Coordinator phases opt into cached metadata explicitly.
                inventory = self.snapshot(source_id, refresh=True)
                key = inventory["share_key"]
                if key in done:
                    continue
                done.add(key)
                result = collect_release(self.store, self, source_id, library, dry_run=dry_run, allow_gate=allow_gate)
                result["source_id"] = source_id
                result["warnings"] = self.release(source_id)["warnings"]
                reports.append(result)
            except ShareUnavailable as error:
                unavailable.append({"source_id": source_id, "reason": str(error)})
            except ShareAccessRequired as error:
                access.append({"source_id": source_id, "reason": str(error)})
            except Exception as error:
                failed.append({"source_id": source_id, "error": type(error).__name__ + ": " + str(error)[:160]})
        self.store.sync_logs(force=True)
        return {"releases": reports, "unavailable_releases": unavailable, "access_required": access,
                "failures": failed, "api_calls": self.calls,
                "upgrade_candidates_ask_user": [t["track"] for r in reports for t in r.get("tracks", [])
                                                   if t.get("action") == "upgrade_candidate"]}


def recipient_url(url):
    """Use only the supplied link, or its explicitly embedded base64 recipient URL."""
    parsed = checked_url(url, SHARE_HOSTS)
    query = parse_qs(parsed.query)
    if parsed.hostname == "onedrive.live.com" and query.get("redeem"):
        value = query["redeem"][0]
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
            checked_url(decoded, SHARE_HOSTS)
        except (ValueError, UnicodeError):
            raise RuntimeError("OneDrive embedded recipient URL is invalid") from None
        return decoded
    if parsed.hostname == "onedrive.live.com" and not (query.get("id") or query.get("resid") or parsed.path.startswith(("/:f:/", "/:u:/"))):
        raise RuntimeError("OneDrive URL has no recipient item or sharing key")
    if parsed.hostname == "1drv.ms" and len(parsed.path.strip("/")) < 6:
        raise RuntimeError("OneDrive short recipient URL is incomplete")
    return url


def onedrive_page_path(url):
    parsed = checked_url(url, SHARE_HOSTS)
    if any(word in parsed.path.lower() for word in ("login", "signin", "consent", "unsubscribe", "register")):
        raise ShareAccessRequired("OneDrive recipient link redirected to a login or consent page")


class OneDrivePromos(DirectSharePromos):
    platform = "onedrive"

    def _validate_inventory(self, inventory):
        super()._validate_inventory(inventory)
        if inventory.get("kind") not in {"file", "folder"}:
            raise RuntimeError("OneDrive metadata cache has an invalid item kind")
        if inventory["kind"] == "file" and (len(inventory["files"]) != 1
                or inventory["files"][0]["id"] != inventory["share_key"]):
            raise RuntimeError("OneDrive metadata cache has an invalid root file identity")
        root = checked_url(inventory.get("api_root", ""), ("onedrive.live.com", "api.onedrive.com"))
        if not re.fullmatch(r"/(?:_api/v2\.0/shares/[^/]+/driveItem|v1\.0/shares/[^/]+/root)", root.path):
            raise RuntimeError("OneDrive metadata cache has an invalid recipient scope")
        for entry in inventory["files"]:
            target = checked_url(entry.get("metadata_url", ""), (root.hostname,))
            if target.path != root.path and not re.fullmatch(
                    r"/(?:_api/v2\.0|v1\.0)/drives/[^/]+/items/[^/]+", target.path):
                raise RuntimeError("OneDrive metadata cache has an invalid file route")

    def _context(self, source_id):
        original = self.store.source_url(source_id)
        recipient = recipient_url(original)
        url, status, headers, body = self.http.page(original, SHARE_HOSTS, onedrive_page_path)
        if status in (401, 403):
            raise ShareAccessRequired("OneDrive recipient share needs account access")
        if status in (404, 410):
            raise ShareUnavailable("OneDrive recipient share is unavailable")
        if status != 200:
            raise RuntimeError("OneDrive recipient page HTTP " + str(status))
        sharing = "u!" + base64.urlsafe_b64encode(recipient.encode("utf-8")).decode("ascii").rstrip("=")
        # Migrated OneDrive Personal pages expose the official native v2 endpoint.
        if b"_spPageContextInfo" in body:
            return "https://onedrive.live.com/_api/v2.0/shares/" + sharing + "/driveItem"
        return "https://api.onedrive.com/v1.0/shares/" + sharing + "/root"

    def _get(self, url):
        parsed = checked_url(url, ("onedrive.live.com", "api.onedrive.com"))
        if not (parsed.path.startswith(("/_api/v2.0/shares/", "/v1.0/shares/"))
                or re.fullmatch(r"/(?:_api/v2\.0|v1\.0)/drives/[^/]+/items/[^/]+(?:/children)?", parsed.path)):
            raise RuntimeError("OneDrive metadata URL left its recipient share scope")
        return self.http.json(url, (parsed.hostname,), headers={"Prefer": "redeemSharingLinkIfNecessary"})

    @staticmethod
    def _item_url(root, item, drive_id):
        actual_drive = (item.get("parentReference") or {}).get("driveId") or drive_id
        if not drive_id or actual_drive != drive_id or not item.get("id"):
            raise RuntimeError("OneDrive child item left the inventoried recipient drive")
        prefix = root.split("/shares/", 1)[0]
        return prefix + "/drives/" + quote(drive_id, safe="") + "/items/" + quote(item["id"], safe="")

    def _children(self, root, prefix="", depth=0, seen=None, drive_id=None, node=None, expected_count=None):
        if depth > 8:
            raise RuntimeError("OneDrive share folder nesting exceeded its bounded depth")
        seen = set() if seen is None else seen
        collection = (node or root) + "/children"
        location, files, pages, direct_count = collection + "?$top=200", [], set(), 0
        while location:
            parsed = checked_url(location, (urlsplit(root).hostname,))
            if parsed.path != urlsplit(collection).path or location in pages:
                raise RuntimeError("OneDrive pagination left its folder scope or repeated a page")
            pages.add(location)
            if len(pages) > 100:
                raise RuntimeError("OneDrive folder pagination exceeded its bounded page count")
            page = self._get(location)
            if not isinstance(page.get("value"), list):
                raise RuntimeError("OneDrive children metadata is missing its inventory")
            for item in page["value"]:
                direct_count += 1
                item_id = item.get("id")
                if not item_id or item_id in seen:
                    raise RuntimeError("OneDrive folder inventory repeated or omitted an item id")
                seen.add(item_id)
                if len(seen) > 10000:
                    raise RuntimeError("OneDrive share inventory exceeded its bounded item count")
                name = safe_name(item.get("name"))
                path = prefix + name
                if "remoteItem" in item:
                    raise RuntimeError("OneDrive share contains a remote shortcut requiring separate review")
                metadata_url = self._item_url(root, item, drive_id)
                if "folder" in item:
                    files += self._children(root, path + "/", depth + 1, seen, drive_id, metadata_url,
                                            (item.get("folder") or {}).get("childCount"))
                elif "file" in item:
                    files.append({**self._entry(item, path), "metadata_url": metadata_url})
                else:
                    raise RuntimeError("OneDrive share contains an unsupported item type")
            location = page.get("@odata.nextLink")
        if isinstance(expected_count, int) and direct_count != expected_count:
            raise RuntimeError("OneDrive folder inventory differs from its advertised child count")
        return files

    @staticmethod
    def _entry(item, path):
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RuntimeError("OneDrive file metadata has no valid original size")
        hashes = (item.get("file") or {}).get("hashes") or {}
        seconds = (item.get("audio") or {}).get("duration")
        return {"id": item["id"], "name": safe_name(item["name"]), "path": path, "size": size,
                "duration": seconds / 1000 if isinstance(seconds, (int, float)) else None,
                "sha1": hashes.get("sha1Hash"), "sha256": hashes.get("sha256Hash"),
                "etag": item.get("eTag"), "ctag": item.get("cTag"),
                "is_downloadable": not bool((item.get("malware") or item.get("deleted")))}

    def snapshot(self, source_id, refresh=False):
        if not self.store.db.execute("SELECT 1 FROM sources WHERE id=? AND platform=?", (source_id, self.platform)).fetchone():
            raise ValueError("Unknown OneDrive source")
        cache = self.store.root / ("onedrive-" + source_id + ".dpapi")
        if source_id in self.inventories and not refresh:
            return self.inventories[source_id]
        if not refresh:
            inventory = self._read_inventory(source_id, cache)
            if inventory is not None:
                self.inventories[source_id] = inventory
                return inventory
        self.store.event(source_id, "view", "intent", platform=self.platform,
                         platform_effect="Recipient page and original-file metadata only; no audio transfer")
        self.store.sync_logs()
        root = self._context(source_id)
        item = self._get(root)
        if item.get("deleted"):
            raise ShareUnavailable("OneDrive shared item is explicitly deleted")
        if "remoteItem" in item:
            raise ShareAccessRequired("OneDrive remote shortcut requires separate access review")
        if "folder" in item:
            drive_id = (item.get("parentReference") or {}).get("driveId")
            if not drive_id:
                raise RuntimeError("OneDrive folder metadata omitted its recipient drive id")
            files, kind = self._children(root, drive_id=drive_id,
                                         expected_count=(item.get("folder") or {}).get("childCount")), "folder"
        elif "file" in item:
            files, kind = [{**self._entry(item, ""), "metadata_url": root}], "file"
        else:
            raise RuntimeError("OneDrive root has neither file nor folder metadata")
        inventory = {"name": safe_name(item["name"]), "files": files, "kind": kind,
                     "file_count": len(files), "share_key": item["id"], "api_root": root,
                     "inspected_at": datetime.now(timezone.utc).isoformat()}
        self._save_inventory(source_id, cache, inventory)
        self.inventories[source_id] = inventory
        self.store.event(source_id, "view", "inspected", platform=self.platform, files=len(files),
                         requests=self.calls, note="Share fully inventoried; no original or preview bytes transferred")
        return inventory

    def _download_entry(self, source_id, entry, destination):
        # Refresh metadata in a fresh recipient context. Preauthenticated URLs expire.
        self._context(source_id)
        if not any(x["id"] == entry["id"] and x.get("metadata_url") == entry.get("metadata_url")
                   for x in self.snapshot(source_id)["files"]):
            raise RuntimeError("OneDrive original is outside the inventoried recipient share")
        item = self._get(entry["metadata_url"])
        current = self._entry(item, entry["path"])
        for key in ("id", "size", "sha1", "sha256", "etag", "ctag"):
            if current.get(key) != entry.get(key):
                raise RuntimeError("OneDrive original changed since inventory; refresh and re-plan")
        if not current["is_downloadable"]:
            raise ShareAccessRequired("OneDrive original is not downloadable")
        location = item.get("@content.downloadUrlNoAuth") or item.get("@content.downloadUrl") or item.get("@microsoft.graph.downloadUrl")
        if not location:
            raise ShareAccessRequired("OneDrive metadata provides no original download permission")
        return self.http.original(location, destination, ORIGINAL_HOSTS, entry["size"],
                                  sha1=entry.get("sha1"), sha256=entry.get("sha256"))

    def download(self, source_id, file, destination):
        self.store.event(source_id, "platform_download", "intent", platform=self.platform, file_id=file["id"],
                         platform_effect="Single original file transfer, no feedback")
        self.store.sync_logs()
        return self._download_entry(source_id, file["entry"], destination)


def run_cli(adapter_class):
    parser = argparse.ArgumentParser(description=adapter_class.__doc__ or __doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    mail = sub.add_parser("discover-mail")
    mail.add_argument("--since", default="30d")
    collect = sub.add_parser("collect")
    collect.add_argument("source_id")
    collect.add_argument("--dry-run", action="store_true")
    collect.add_argument("--max-assets", type=int)
    batch = sub.add_parser("batch")
    batch.add_argument("--dry-run", action="store_true")
    batch.add_argument("--max-age-days", type=int, default=30)
    args = parser.parse_args()
    store, adapter = Store(), None
    try:
        adapter = adapter_class(store)
        if args.command == "discover-mail":
            result = discover(store, args.since, [adapter.platform])
        elif args.command == "batch":
            result = adapter.batch(args.dry_run, max_age_days=args.max_age_days)
        else:
            adapter.snapshot(args.source_id, refresh=True)
            result = collect_release(store, adapter, args.source_id, LibraryIndex(store), dry_run=args.dry_run,
                                     max_assets=args.max_assets)
            result["warnings"] = adapter.release(args.source_id)["warnings"]
        store.sync_logs(force=True)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    except (ShareUnavailable, ShareAccessRequired) as error:
        print(json.dumps({"status": "unavailable" if isinstance(error, ShareUnavailable) else "access_required",
                          "reason": str(error)}, ensure_ascii=False))
    finally:
        if adapter:
            adapter.close()
        store.db.close()


if __name__ == "__main__":
    run_cli(OneDrivePromos)
