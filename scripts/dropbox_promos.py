"""Direct Dropbox promo links, for individual files and shared folders. Uses the shared Dropbox skill."""
import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from promo_automation import Store, SKILLS, dropbox_skill
from promo_cloud import protect_json, read_protected_json
from audio_length import HEAD_BYTES, length_from_header
from promo_pipeline import AUDIO_SUFFIXES, LibraryIndex, collect_release, version_group, version_kind, write_stream
from process_promos import load_label_codes, open_rar, parse_filename, read_existing_tags

# Import the reusable shared-link implementation from the existing Dropbox skill.
sys.path.insert(0, str(SKILLS / "dropbox/scripts"))
from dropbox_shared import content_hash, shared_inventory, download_shared_file, validate_link


def direct_download(url, destination, expected_size=None, expected_hash=None):
    """The original shared file via dl=1, without account credentials or preview audio."""
    validate_link(url)
    parsed = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k not in {"dl", "raw"}]
    location = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query + [("dl", "1")]), ""))
    with requests.Session() as session:
        for _ in range(6):
            host = urlsplit(location).hostname or ""
            if urlsplit(location).scheme != "https" or not any(host == d or host.endswith("." + d)
                    for d in ("dropbox.com", "dropboxusercontent.com", "dropboxapi.com", "db.tt")):
                raise RuntimeError("Dropbox original redirected outside Dropbox")
            response = session.get(location, stream=True, allow_redirects=False, timeout=(30, 180))
            if response.is_redirect:
                from urllib.parse import urljoin
                next_url = urljoin(location, response.headers.get("Location", ""))
                response.close()
                location = next_url
                continue
            try:
                if response.status_code != 200:
                    raise RuntimeError(f"Dropbox shared original HTTP {response.status_code}")
                if any(t in response.headers.get("Content-Type", "").lower() for t in ("text/html", "application/json")):
                    raise RuntimeError("Dropbox shared link returned a page instead of the original")
                result = write_stream(response.iter_content(1 << 20), destination,
                                      expected_size or int(response.headers.get("Content-Length") or 0), tolerance=0)
                if expected_hash and content_hash(destination) != expected_hash:
                    Path(destination).unlink()
                    raise RuntimeError("Dropbox original content hash differs from inventory")
                return result
            finally:
                response.close()
    raise RuntimeError("Dropbox shared original has too many redirects")


def extract_audio(archive, destination):
    """Extract bounded audio entries, refusing traversal, links and Windows path aliases."""
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if Path(archive).suffix.lower() == ".rar":
        container = open_rar(archive)
    else:
        container = zipfile.ZipFile(archive)
    extracted, seen = [], set()
    with container:
        infos = container.infolist()
        if len(infos) > 10000 or sum(i.file_size for i in infos) > 20 * 1024**3:
            raise RuntimeError("Promo archive exceeds the bounded inventory size")
        for member in infos:
            relative = PurePosixPath(member.filename.replace("\\", "/"))
            if relative.is_absolute() or any(p in {"..", "."} or ":" in p or p.endswith((".", " ")) for p in relative.parts):
                raise RuntimeError("Unsafe promo archive member path")
            if stat.S_ISLNK(getattr(member, "external_attr", 0) >> 16) or (hasattr(member, "is_symlink") and member.is_symlink()):
                raise RuntimeError("Links are not permitted in promo archives")
            if relative.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            target = destination.joinpath(*relative.parts).resolve()
            if not target.is_relative_to(destination) or str(target).casefold() in seen:
                raise RuntimeError("Promo archive member paths overlap")
            seen.add(str(target).casefold())
            target.parent.mkdir(parents=True, exist_ok=True)
            # A complete extraction is published only after all CRC-checked reads succeed.
            partial = target.with_name(target.name + ".partial")
            with container.open(member) as source, partial.open("wb") as output:
                shutil.copyfileobj(source, output, 1 << 20)
            if partial.stat().st_size != member.file_size:
                raise RuntimeError("Extracted audio differs from archive inventory")
            os.replace(partial, target)
            extracted.append(target)
    return extracted


class DropboxPromos:
    def __init__(self, store):
        self.store = store
        self.shared, self.client = dropbox_skill()
        self.codes = load_label_codes(str(Path(__file__).with_name("label_codes.csv")))
        self.lengths = {}

    @staticmethod
    def archive_fingerprint(inventory):
        """Bind extracted tracks to the entire current archive roster and content.

        An archive id alone survives replacement on Dropbox and Drive. Keep all
        available revision/hash evidence, including Drive's MD5 and OneDrive tags.
        """
        keys = ("id", "name", "path", "size", "rev", "version", "modified_time", "content_hash", "md5",
                "sha1", "sha256", "etag", "ctag")
        packs = [{key: entry.get(key) for key in keys} for entry in inventory["files"]
                 if Path(entry["name"]).suffix.lower() in {".zip", ".rar"}]
        packs.sort(key=lambda entry: (str(entry["id"]), str(entry["path"]), entry["name"]))
        return hashlib.sha256(json.dumps(packs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def archive_versions_complete(inventory):
        keys = ("rev", "version", "content_hash", "md5", "sha1", "sha256", "etag", "ctag")
        return all(any(entry.get(key) for key in keys) for entry in inventory["files"]
                   if Path(entry["name"]).suffix.lower() in {".zip", ".rar"})

    def _clear_archive_inventory(self, source_id):
        base = (self.store.root / "dropbox-packs").resolve()
        target = base / source_id
        folder = target.resolve()
        if folder.parent != base or folder != target:
            raise RuntimeError("Archive inventory cleanup left its source folder")
        if folder.exists():
            shutil.rmtree(folder)

    def snapshot(self, source_id, refresh=False):
        row = self.store.db.execute("SELECT * FROM sources WHERE id=? AND platform='dropbox'", (source_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown direct Dropbox source")
        if refresh:
            getattr(self, "_prepared_archives", {}).pop(source_id, None)
        cache = self.store.root / f"dropbox-{source_id}.dpapi"
        if cache.exists() and not refresh:
            return read_protected_json(cache)
        self.store.event(source_id, "view", "intent", platform_effect="shared-link metadata and folder listings only")
        self.store.sync_logs()
        inventory = shared_inventory(self.client, self.store.source_url(source_id))
        protect_json(cache, inventory)
        self.store.event(source_id, "view", "inspected", files=len(inventory["files"]), requests=inventory["requests"],
                         note="Shared link inventoried; no audio downloaded")
        self.store.sync_logs()
        return inventory

    def release(self, source_id):
        inventory = self.snapshot(source_id)
        tracks, warnings = [], []
        files = list(inventory["files"])
        prepared = self.store.root / "dropbox-packs" / source_id / "inventory.json"
        prepared_matches = False
        if prepared.exists():
            saved = json.loads(prepared.read_text(encoding="utf-8"))
            fingerprint = self.archive_fingerprint(inventory)
            prepared_matches = (saved.get("archive_fingerprint") == fingerprint and (
                self.archive_versions_complete(inventory)
                or getattr(self, "_prepared_archives", {}).get(source_id) == fingerprint))
            if prepared_matches:
                files += saved["files"]
        for file in files:
            suffix = Path(file["name"]).suffix.lower()
            if suffix not in AUDIO_SUFFIXES:
                if suffix in {".zip", ".rar"}:
                    if not prepared_matches:
                        warnings.append({"file": file["name"], "reason": "archive requires prepare_source before per-mix collection"})
                continue
            info = parse_filename(file["name"], self.codes)
            artist, title = info.get("artist"), info.get("title")
            label = info.get("label")
            if label and label.startswith("UNKNOWN:"):
                label = None
            if file.get("local_path"):
                tags = read_existing_tags(file["local_path"]) if Path(file["local_path"]).is_file() else file.get("original_tags", {})
                artist = artist or tags.get("artist")
                title = title or tags.get("title")
                label = label or tags.get("grouping") or tags.get("publisher")
            name = f"{artist} - {title}" if artist else title or Path(file["name"]).stem
            offer = [{"id": file["id"], "type": suffix[1:], "entry": file}] if file.get("is_downloadable", True) else []
            same = next((t for t in tracks if t["name"] == name), None)
            if same:  # the MP3 and WAV of one mix are one track offering two formats
                same["files"] += offer
                same["label"] = same["label"] or label
                continue
            tracks.append({"name": name, "duration": None, "probe": file,
                           "artist": artist, "title": title, "label": label, "files": offer})
        # Lengths pick the main version of a mix offered more than once; marked extended mixes need none.
        groups = {}
        for track in tracks:
            groups.setdefault(version_group(track["name"]), []).append(track)
        for track in tracks:
            probe = track.pop("probe")
            if len(groups[version_group(track["name"])]) > 1 and version_kind(track["name"]) != "extended":
                track["duration"] = self.length(source_id, probe)
        return {"title": inventory["name"], "label": None, "gated": False, "tracks": tracks, "warnings": warnings}

    def prepare_source(self, source_id):
        """An archive needs one original transfer to discover its per-mix inventory, before dry run."""
        self._prepare_archive_source(source_id, require_audio=False)

    def prepare_collection_source(self, source_id):
        """Rehydrate archived originals only after local planning finds real work.

        A collected inventory intentionally survives zero-retention cleanup for
        later deduplication, but its old local paths are not downloadable bytes.
        """
        try:
            self._prepare_archive_source(source_id, require_audio=True)
        except Exception:
            manifest = self.store.root / "dropbox-packs" / source_id / "inventory.json"
            if manifest.exists():
                self.cleanup_source(source_id, {"tracks": []})
            else:
                self._clear_archive_inventory(source_id)
            raise

    def _prepare_archive_source(self, source_id, require_audio):
        inventory = self.snapshot(source_id)
        packs = [f for f in inventory["files"] if Path(f["name"]).suffix.lower() in {".zip", ".rar"}]
        folder = self.store.root / "dropbox-packs" / source_id
        manifest = folder / "inventory.json"
        fingerprint = self.archive_fingerprint(inventory)
        if manifest.exists():
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            if saved.get("archive_fingerprint") == fingerprint and (
                    self.archive_versions_complete(inventory)
                    or getattr(self, "_prepared_archives", {}).get(source_id) == fingerprint):
                if not require_audio:
                    return
                if all(Path(f["local_path"]).is_file() and Path(f["local_path"]).stat().st_size == f["size"]
                       for f in saved["files"]):
                    return
                self.store.event(source_id, "archive_inventory", "rehydrating", reason="Collection requires originals removed by cleanup")
            else:
                # Never allow a previously collected ZIP to conceal a replacement
                # or a new sibling archive. Legacy unbound manifests refresh once.
                self._clear_archive_inventory(source_id)
                self.store.event(source_id, "archive_inventory", "invalidated", reason="Archive roster or content identity changed")
        if not packs:
            return
        folder.mkdir(parents=True, exist_ok=True)
        files = []
        for pack in packs:
            pack_id = hashlib.sha256(str(pack["id"]).encode()).hexdigest()[:24]
            archive = folder / (pack_id + Path(pack["name"]).suffix.lower())
            if shutil.disk_usage(folder).free < (pack.get("size") or 0) * 3 + 2 * 1024**3:
                raise RuntimeError("Insufficient temporary disk space for the original promo archive and processing")
            if archive.exists() and (archive.stat().st_size != pack.get("size") or (
                    pack.get("content_hash") and content_hash(archive) != pack["content_hash"])):
                if not require_audio:
                    raise RuntimeError("Saved Dropbox archive differs from its inventory")
                archive.unlink()
            if not archive.exists():
                self.store.event(source_id, "archive_inventory", "download_intent", file=pack["name"],
                                 note="Original archive transfer is needed to inspect the mixes inside")
                self.store.sync_logs()
                self._download_entry(source_id, pack, archive)
            for path in extract_audio(archive, folder / pack_id):
                files.append({"name": path.name, "local_path": str(path), "size": path.stat().st_size,
                              "original_tags": read_existing_tags(str(path)),
                              "id": pack["id"] + ":" + path.relative_to(folder / pack_id).as_posix()})
        from promo_automation import atomic_text
        atomic_text(manifest, json.dumps({"archive_fingerprint": fingerprint, "files": files}, ensure_ascii=False, indent=2))
        if not hasattr(self, "_prepared_archives"):
            self._prepared_archives = {}
        self._prepared_archives[source_id] = fingerprint
        self.store.event(source_id, "archive_inventory", "complete", archives=len(packs), audio_files=len(files))

    def cleanup_source(self, source_id, result):
        manifest = self.store.root / "dropbox-packs" / source_id / "inventory.json"
        if not manifest.exists() or any(t.get("action") in {"uncertain", "claim_denied"} for t in result.get("tracks", [])):
            return
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        folder = manifest.parent.resolve()
        # Wanted held files have already been copied into the protected pipeline staging folder.
        for file in saved["files"]:
            path = Path(file["local_path"]).resolve()
            if not path.is_relative_to(folder):
                raise RuntimeError("Archive cache cleanup target left its source folder")
            if path.exists():
                path.unlink()
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() in {".zip", ".rar"}:
                path.unlink()
        saved["collected_snapshot"] = True
        from promo_automation import atomic_text
        atomic_text(manifest, json.dumps(saved, ensure_ascii=False, indent=2))

    def resolve_metadata(self, source_id, track, path):
        tags = read_existing_tags(str(path))
        publishers = {tags[k].strip() for k in ("grouping", "publisher") if tags.get(k)}
        if not track.get("label") and len(publishers) == 1:
            track["label"] = next(iter(publishers))
        if not track.get("artist"):
            track["artist"] = tags.get("artist")
        if len(publishers) > 1 or (publishers and track.get("label") and
                any(p.casefold() != track["label"].casefold() for p in publishers)):
            track["metadata_conflicts"] = ["Original label tags conflict with each other or the filename label"]
        # Conflicting original labels remain unresolved and are sent to AI research.
        self.store.event(source_id, "original_metadata", "inspected", track=track["name"], tags=tags,
                         label_conflict=len(publishers) > 1)

    def length(self, source_id, entry):
        """Seconds from the original's header, read from the start of the API stream; None if unreadable."""
        key = (source_id, entry["id"])
        if key in self.lengths:
            return self.lengths[key]
        seconds = None
        try:
            if entry.get("local_path"):
                import mutagen
                audio = mutagen.File(entry["local_path"])
                seconds = audio.info.length if audio else None
            elif entry.get("size"):
                _, response = self.client.sharing_get_shared_link_file(self.store.source_url(source_id), path=entry.get("path"))
                head, wanted = b"", HEAD_BYTES
                try:
                    for chunk in response.iter_content(1 << 14):
                        head += chunk
                        if len(head) < wanted:
                            continue
                        seconds = length_from_header(head, entry["size"])
                        if not isinstance(seconds, tuple):
                            break
                        wanted = seconds[1] + HEAD_BYTES  # an ID3 tag with cover art pushes the first frame back
                        if wanted > 4 * 1024**2:
                            break
                finally:
                    response.close()
                if not isinstance(seconds, float):
                    seconds = length_from_header(head, entry["size"]) if len(head) < wanted else None
                    seconds = seconds if isinstance(seconds, float) else None
        except Exception as error:
            self.store.event(source_id, "length_probe", "failed", file=entry.get("name"), error=str(error)[:200])
        self.lengths[key] = seconds
        return seconds

    def _download_entry(self, source_id, entry, destination):
        if entry.get("local_path"):
            local = Path(entry["local_path"])
            return self._copy_local(local, destination)
        if entry.get("path") is None:
            return direct_download(self.store.source_url(source_id), destination, entry.get("size"), entry.get("content_hash"))
        return download_shared_file(self.client, self.store.source_url(source_id), destination,
                                    relative=entry.get("path"), expected_size=entry.get("size"),
                                    expected_hash=entry.get("content_hash"))

    @staticmethod
    def _copy_local(local, destination):
        with local.open("rb") as source:
            return write_stream(iter(lambda: source.read(1 << 20), b""), destination, local.stat().st_size, tolerance=0)

    @staticmethod
    def extension(file):
        return "." + file["type"]

    def unlock(self, source_id, release):
        raise RuntimeError("Dropbox shared links do not have an automatic feedback gate")

    def download(self, source_id, file, destination):
        entry = file["entry"]
        self.store.event(source_id, "platform_download", "intent", file_id=file["id"],
                         platform_effect="shared-link file transfer")
        self.store.sync_logs()
        return self._download_entry(source_id, entry, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    store = Store()
    try:
        result = collect_release(store, DropboxPromos(store), args.source_id, LibraryIndex(store), dry_run=args.dry_run)
        store.sync_logs(force=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
