"""Google Drive promo links, single files and shared folders, through the google-workspace-files skill (gws).

Metadata and originals come from the Drive API as the signed-in Google account, which can open any "anyone with the link"
share. Downloads are checked against Drive's own size and MD5. Lengths for version choice are read from the first
64 KB through the public download endpoint, without the account. Opening a Drive link has no platform side effect
beyond an owner-visible view count; there is no feedback gate.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from curl_cffi import requests

from promo_automation import SKILLS, Store
from promo_cloud import protect_json, read_protected_json
from audio_length import HEAD_BYTES, length_from_header
from dropbox_promos import DropboxPromos
from promo_mail import discover, newest_offer_age
from promo_pipeline import AUDIO_SUFFIXES, LibraryIndex, collect_release
from process_promos import load_label_codes
from soundcloud import mail_label

GWS_PYTHON = Path(sys.executable)  # the bridge is stdlib-only; Google's libraries are needed only for its one-time login
GWS_BRIDGE = SKILLS / "google-workspace-files/scripts/gws_bridge.py"
FOLDER = "application/vnd.google-apps.folder"
MIME_SUFFIX = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
               "audio/wave": ".wav", "audio/vnd.wave": ".wav", "audio/aiff": ".aiff", "audio/x-aiff": ".aiff",
               "audio/flac": ".flac", "audio/x-flac": ".flac", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a"}
_ID = re.compile(r"[A-Za-z0-9_-]{10,}")


def drive_target(url):
    """("file"|"folder", id) for drive.google.com share links; None when the link carries no id."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "drive.google.com":
        return None
    match = re.search(r"/(?:file/d|folders)/([A-Za-z0-9_-]+)", parsed.path)
    if match and _ID.fullmatch(match.group(1)):
        return ("folder" if "/folders/" in parsed.path else "file"), match.group(1)
    ids = parse_qs(parsed.query).get("id")
    if ids and _ID.fullmatch(ids[0]) and parsed.path in {"/open", "/uc"}:
        return "file", ids[0]
    return None


def gws(*args, save_to=None):
    command = [str(GWS_PYTHON), str(GWS_BRIDGE)] + (["--save-to", str(save_to)] if save_to else []) + list(args)
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    if result.returncode:
        detail = re.sub(r"https?://\S+", "[URL]", (result.stderr or result.stdout).strip())[-300:]
        raise RuntimeError("Google Drive call failed: " + detail)
    return json.loads(result.stdout)


class GoogleDrivePromos(DropboxPromos):
    platform = "google-drive"

    def __init__(self, store):
        self.store, self.calls, self.lengths = store, 0, {}
        self.codes = load_label_codes(str(Path(__file__).with_name("label_codes.csv")))

    def _get(self, file_id, fields):
        self.calls += 1
        return gws("drive", "files", "get", "--params", json.dumps(
            {"fileId": file_id, "fields": fields, "supportsAllDrives": True}))

    def _children(self, folder_id, prefix="", depth=0):
        if depth > 4:
            raise RuntimeError("Google Drive folder nesting is deeper than expected")
        files, token = [], None
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed=false", "pageSize": 200,
                      "fields": "nextPageToken,files(id,name,mimeType,size,md5Checksum,version,modifiedTime,capabilities/canDownload)",
                      "supportsAllDrives": True, "includeItemsFromAllDrives": True}
            if token:
                params["pageToken"] = token
            self.calls += 1
            page = gws("drive", "files", "list", "--params", json.dumps(params))
            for item in page.get("files", []):
                if item["mimeType"] == FOLDER:
                    files += self._children(item["id"], prefix + item["name"] + "/", depth + 1)
                elif not item["mimeType"].startswith("application/vnd.google-apps."):
                    files.append(self._entry(item, prefix))
            token = page.get("nextPageToken")
            if not token:
                return files

    @staticmethod
    def _entry(item, prefix=""):
        name = item["name"]
        if Path(name).suffix.lower() not in AUDIO_SUFFIXES and item.get("mimeType") in MIME_SUFFIX:
            name += MIME_SUFFIX[item["mimeType"]]  # uploaded without an extension; Drive still knows the type
        return {"id": item["id"], "name": name, "path": prefix + name,
                "size": int(item["size"]) if item.get("size") else None, "md5": item.get("md5Checksum"),
                "version": item.get("version"), "modified_time": item.get("modifiedTime"),
                "is_downloadable": (item.get("capabilities") or {}).get("canDownload", True)}

    def snapshot(self, source_id, refresh=False):
        row = self.store.db.execute("SELECT 1 FROM sources WHERE id=? AND platform=?", (source_id, self.platform)).fetchone()
        if row is None:
            raise ValueError("Unknown Google Drive source")
        if refresh:
            getattr(self, "_prepared_archives", {}).pop(source_id, None)
        cache = self.store.root / f"gdrive-{source_id}.dpapi"
        if cache.exists() and not refresh:
            return read_protected_json(cache)
        target = drive_target(self.store.source_url(source_id))
        if target is None:
            raise RuntimeError("Google Drive link carries no file or folder id (cut off in the mail)")
        kind, drive_id = target
        self.store.event(source_id, "view", "intent", platform=self.platform, platform_effect="Drive metadata only")
        self.store.sync_logs()
        if kind == "folder":
            name = self._get(drive_id, "id,name")["name"]
            files = self._children(drive_id)
        else:
            item = self._get(drive_id, "id,name,mimeType,size,md5Checksum,version,modifiedTime,capabilities/canDownload")
            name, files = Path(item["name"]).stem, [self._entry(item)]
        inventory = {"name": name, "drive_id": drive_id, "kind": kind, "files": files}
        protect_json(cache, inventory)
        self.store.event(source_id, "view", "inspected", platform=self.platform, files=len(files),
                         note="Drive share inventoried; no audio downloaded")
        return inventory

    def release(self, source_id):
        release = super().release(source_id)
        subject, metadata = self.store.db.execute("SELECT subject,metadata FROM sources WHERE id=?", (source_id,)).fetchone()
        fallback = mail_label(subject, json.loads(metadata or "{}").get("sender"))
        for track in release["tracks"]:
            track["label"] = track.get("label") or fallback
        return release

    def length(self, source_id, entry):
        """Seconds from the first bytes of the public original; None when the share is not public or unreadable."""
        key = (source_id, entry["id"])
        if key in self.lengths:
            return self.lengths[key]
        seconds = None
        if entry.get("local_path"):
            return super().length(source_id, entry)
        try:
            wanted = HEAD_BYTES
            for _ in range(2):
                self.calls += 1
                response = requests.get("https://drive.usercontent.google.com/download",
                                        params={"id": entry["id"], "export": "download"},
                                        headers={"Range": f"bytes=0-{wanted - 1}"}, impersonate="chrome124", timeout=60)
                if response.status_code != 206 or "html" in response.headers.get("Content-Type", ""):
                    break
                seconds = length_from_header(response.content, entry["size"])
                if not isinstance(seconds, tuple) or seconds[1] + HEAD_BYTES > 4 * 1024**2:
                    break
                wanted = seconds[1] + HEAD_BYTES  # an ID3 tag with cover art pushes the first frame back
            seconds = seconds if isinstance(seconds, float) else None
        except Exception as error:
            self.store.event(source_id, "length_probe", "failed", file=entry.get("name"), error=str(error)[:200])
        self.lengths[key] = seconds
        return seconds

    def _download_entry(self, source_id, entry, destination):
        if entry.get("local_path"):
            return self._copy_local(Path(entry["local_path"]), destination)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".gws")
        try:
            saved = gws("drive", "files", "get", "--params", json.dumps(
                {"fileId": entry["id"], "alt": "media", "supportsAllDrives": True}), save_to=partial)
            self.calls += 1
            if entry.get("size") and saved.get("bytes") != entry["size"]:
                raise RuntimeError("Google Drive transfer size differs from the file's metadata")
            md5, sha = hashlib.md5(), hashlib.sha256()
            with partial.open("rb") as source:
                for block in iter(lambda: source.read(1 << 20), b""):
                    md5.update(block)
                    sha.update(block)
            if entry.get("md5") and md5.hexdigest() != entry["md5"]:
                raise RuntimeError("Google Drive original MD5 differs from the file's metadata")
            size = partial.stat().st_size
            os.replace(partial, destination)
            return size, sha.hexdigest()
        finally:
            if partial.exists():
                os.remove(partial)

    def download(self, source_id, file, destination):
        self.store.event(source_id, "platform_download", "intent", file_id=file["id"], platform=self.platform,
                         platform_effect="Drive original file transfer")
        self.store.sync_logs()
        return self._download_entry(source_id, file["entry"], destination)

    def same_release(self, source_id):
        """Several mails often share one folder; the coordinator collects each Drive id once per run."""
        return self.snapshot(source_id)["drive_id"]

    def sibling_link(self, source_id):
        """True when a mail carrying this cut-off link also carries a complete Drive link."""
        rows = self.store.db.execute("""SELECT DISTINCT o.source_id FROM messages m JOIN messages o
                   ON o.message_id=m.message_id AND o.source_id<>m.source_id JOIN sources s ON s.id=o.source_id
                   WHERE m.source_id=? AND s.platform=?""", (source_id, self.platform)).fetchall()
        return any(drive_target(self.store.source_url(other)) for (other,) in rows)

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30):
        library, report, done = LibraryIndex(self.store), [], set()
        rows = self.store.db.execute("SELECT id,subject FROM sources WHERE platform=? ORDER BY discovered_at DESC,id",
                                     (self.platform,)).fetchall()
        for sid, subject in rows:
            age = newest_offer_age(self.store, sid)
            if age is None or age > max_age_days:
                continue
            if drive_target(self.store.source_url(sid)) is None and self.sibling_link(sid):
                self.store.event(sid, "batch", "skipped", note="cut-off link; the same mail carries the full one")
                continue
            try:
                key = self.same_release(sid)
                if key in done:
                    continue
                done.add(key)
                self.prepare_source(sid)
                summary = collect_release(self.store, self, sid, library, dry_run=dry_run, allow_gate=allow_gate)
                if not dry_run:
                    self.cleanup_source(sid, summary)
                report.append({"release": summary.get("release") or subject, "tracks": summary["tracks"]})
            except Exception as error:
                self.store.event(sid, "batch", "release_failed", release=subject, error=str(error)[:200])
                report.append({"release": subject, "result": "failed: " + str(error)[:160]})
        self.store.sync_logs(force=True)
        return {"releases": report, "api_calls": self.calls,
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
    for command in (collect, batch):
        command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    store = Store()
    try:
        adapter = GoogleDrivePromos(store)
        if args.command == "discover-mail":
            result = discover(store, args.since, [adapter.platform])
        elif args.command == "collect":
            adapter.prepare_source(args.source_id)
            result = collect_release(store, adapter, args.source_id, LibraryIndex(store), dry_run=args.dry_run)
            if not args.dry_run:
                adapter.cleanup_source(args.source_id, result)
            store.sync_logs(force=True)
        else:
            result = adapter.batch(args.dry_run, max_age_days=args.max_age_days)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
