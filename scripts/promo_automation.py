"""Resumable promo pilot inventory and shared audit log. Secrets stay local, DPAPI protected."""
from __future__ import annotations

import argparse
import csv
import email
import hashlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager
from functools import lru_cache
from urllib.parse import parse_qs, urlsplit, urlunsplit

SKILLS = Path(__file__).resolve().parents[2]
# Everything that differs between users lives in one settings file outside the skill (install.py writes it), so
# the code carries no names, mailboxes or folders. PROMO_SETTINGS names another file, for tests or a second setup.
SETTINGS_FILE = Path(os.environ.get("PROMO_SETTINGS")
                     or Path(os.environ.get("APPDATA") or Path.home()) / "promo-processor/settings.json")


def load_settings(path=SETTINGS_FILE):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


SETTINGS = load_settings()
LOCAL = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
STATE = Path(SETTINGS.get("state_dir") or LOCAL / "promo-processor/state")
DEST = SETTINGS.get("destination", "/Music/Promos")
# The whole library, listed for the duplicate check; DEST sits inside it.
LIBRARY_ROOT = SETTINGS.get("library_root") or "/" + DEST.strip("/").split("/")[0]
ACCOUNT = SETTINGS.get("mail_account", "")
# Keyring service holding the platform passwords and saved logins.
CREDENTIAL_SERVICE = SETTINGS.get("credential_service", "promo-processor")
# Held files wait here with a note, instead of on this computer; never merged until fixed and moved up.
HOLD = DEST + "/Needs Research"
# 2026-10-24: nothing in the test folder has been processed by the user yet, so the whole test folder counts as
# one pass and a lossless copy may replace any lossy copy in it. Set to False when delivery moves to Latest Promos;
# from then on each "update the promos" is its own pass and earlier passes are never replaced.
TEST_FOLDER_IS_ONE_PASS = True
# The full download logs hold every event ever (about 9 MB each on 2026-10-24) and were re-uploaded each minute:
# 3.5 GB and 40 minutes of transfer in one day. They now refresh at most every SUMMARY_SECONDS when a worker
# finishes, and always in the run's summary phase.
SUMMARY_SECONDS = 900
# 2026-10-24: one upload per event cost 1.8 hours on 2,528 uploads in one day. Every event is committed to
# the local database first; batches go to Dropbox at most every EVENT_SECONDS, and at once when a worker finishes.
EVENT_SECONDS = 60
# The library listing follows Dropbox's change cursor; a complete relist still runs after this many days.
FULL_RELIST_SECONDS = 7 * 86400


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def process_alive(owner):
    """Only used to avoid taking a resumable asset from another live worker."""
    try:
        pid = int(owner)
    except (ValueError, TypeError):
        return True  # Unknown ownership requires review.
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # Access denied is not proof of exit.
        try:
            code = wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def email_skill():
    result = module("promo_email", SKILLS / "email/scripts/email_cli.py")
    # Without a setting the email skill searches its usual places for the credentials file.
    result.load_config(SimpleNamespace(creds_file=SETTINGS.get("email_credentials")))
    return result


@lru_cache(maxsize=1)
def dropbox_skill():
    result = module("promo_dropbox", SKILLS / "dropbox/scripts/dropbox_cli.py")
    client = result.build_client(result.load_credentials(SETTINGS.get("dropbox_credentials")))
    return result, client


def normalized(text):
    return re.sub(r"\W+", " ", text.casefold(), flags=re.UNICODE).strip()


def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def dropbox_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(4 * 1024 * 1024):
            digest.update(hashlib.sha256(block).digest())
    return digest.hexdigest()


def dropbox_bytes_hash(data):
    digest = hashlib.sha256()
    for offset in range(0, len(data), 4 * 1024 * 1024):
        digest.update(hashlib.sha256(data[offset:offset + 4 * 1024 * 1024]).digest())
    return digest.hexdigest()


UPLOAD_SLOTS = 2


def _try_lock(handle):
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def upload_slot(root=STATE, slots=None):
    """Cross-process cap on simultaneous Dropbox audio uploads, so parallel collection lanes share the bandwidth.

    Each slot is a one-byte OS lock on its own file; a crashed worker's lock is released by the OS with its process.
    """
    slots = slots or int(os.environ.get("PROMO_UPLOAD_SLOTS") or UPLOAD_SLOTS)
    folder = Path(root) / "upload-slots"
    folder.mkdir(parents=True, exist_ok=True)
    while True:
        for index in range(slots):
            handle = (folder / f"slot-{index}.lock").open("a+b")
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if _try_lock(handle):
                try:
                    yield index
                finally:
                    _unlock(handle)
                    handle.close()
                return
            handle.close()
        time.sleep(0.5)


@contextmanager
def log_lock(root):
    with (Path(root) / "shared-log.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + 30
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Another worker is syncing the shared log; retry after it finishes") from None
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def browser_code(code):
    wrapper = SKILLS / "browser-computer-control/scripts/run_browser_use.py"
    result = subprocess.run([sys.executable, str(wrapper), "--normal-chrome"],
                            input=code, capture_output=True, text=True, encoding="utf-8", timeout=90)
    if result.returncode:
        # Browser errors can echo the submitted code, which may contain a signed source link.
        raise RuntimeError("Browser operation failed; inspect current page before retrying")
    return result.stdout.strip()


class Store:
    def __init__(self, root=STATE):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "inventory.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id TEXT PRIMARY KEY, platform TEXT NOT NULL, remote_id TEXT,
          subject TEXT NOT NULL, url_protected BLOB, metadata TEXT NOT NULL DEFAULT '{}',
          discovered_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS remote_source ON sources(platform,remote_id)
          WHERE remote_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS messages(
          mailbox TEXT, folder TEXT, uidvalidity TEXT, uid TEXT,
          message_id TEXT, subject TEXT, received TEXT, source_id TEXT REFERENCES sources(id),
          PRIMARY KEY(mailbox,folder,uidvalidity,uid,source_id));
        CREATE TABLE IF NOT EXISTS events(
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, utc TEXT NOT NULL,
          source_id TEXT REFERENCES sources(id), action TEXT NOT NULL,
          status TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS library(
          path TEXT PRIMARY KEY, remote_id TEXT, revision TEXT, bytes INTEGER,
          content_hash TEXT, name_key TEXT, indexed_at TEXT);
        CREATE TABLE IF NOT EXISTS assets(
          asset_key TEXT PRIMARY KEY, source_id TEXT REFERENCES sources(id),
          title TEXT, format TEXT, stage TEXT NOT NULL, local_path TEXT,
          raw_sha256 TEXT, dropbox_path TEXT, content_hash TEXT,
          metadata TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS claims(
          asset_key TEXT PRIMARY KEY REFERENCES assets(asset_key), owner TEXT, acquired_at TEXT);
        CREATE TABLE IF NOT EXISTS feedback(
          source_id TEXT PRIMARY KEY REFERENCES sources(id), rating INTEGER NOT NULL, comment TEXT NOT NULL,
          supporting INTEGER NOT NULL, status TEXT NOT NULL, chosen_at TEXT NOT NULL, submitted_at TEXT);
        CREATE TABLE IF NOT EXISTS log_checkpoint(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), published_sequence INTEGER NOT NULL,
          summary_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS log_files(name TEXT PRIMARY KEY, content_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS timings(
          utc TEXT NOT NULL, operation TEXT NOT NULL, source_id TEXT, seconds REAL NOT NULL, bytes INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS passes(started TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS replacements(
          asset_key TEXT PRIMARY KEY REFERENCES assets(asset_key), old_path TEXT NOT NULL, old_hash TEXT,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS library_cursor(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), cursor TEXT NOT NULL, listed_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS library_keys(path TEXT PRIMARY KEY, version TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS library_extra_cursors(root TEXT PRIMARY KEY, cursor TEXT NOT NULL, listed_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS mail_scans(
          folder TEXT, uidvalidity TEXT, uid TEXT, version TEXT NOT NULL, data TEXT NOT NULL,
          PRIMARY KEY(folder,uidvalidity,uid));
        -- Per-source lookups ran as full table scans: every planned release read all events and all messages.
        CREATE INDEX IF NOT EXISTS messages_source ON messages(source_id);
        CREATE INDEX IF NOT EXISTS messages_message_id ON messages(message_id);
        CREATE INDEX IF NOT EXISTS events_source ON events(source_id,action,status);
        CREATE INDEX IF NOT EXISTS events_action ON events(action,status);
        CREATE INDEX IF NOT EXISTS assets_source ON assets(source_id);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(log_checkpoint)")}
        if "events_at" not in columns:
            self.db.execute("ALTER TABLE log_checkpoint ADD COLUMN events_at REAL NOT NULL DEFAULT 0")
        self.db.commit()

    def event(self, source_id, action, status, **details):
        self.db.execute("INSERT INTO events(utc,source_id,action,status,details) VALUES(?,?,?,?,?)",
                        (now(), source_id, action, status, json.dumps(details, ensure_ascii=False)))
        self.db.commit()

    def timing(self, operation, seconds, source_id=None, byte_count=0):
        self.db.execute("INSERT INTO timings VALUES(?,?,?,?,?)", (now(), operation, source_id, seconds, byte_count))
        self.db.commit()

    def add_source(self, platform, subject, url, remote_id=None, **metadata):
        """remote_id: the platform's own release id when the URL does not carry it (PromoBox list tiles)."""
        import win32crypt
        match = re.fullmatch(r"/review/releases/([^/]+)/?", urlsplit(url).path) if platform == "inflyte" else None
        remote_id = match.group(1) if match else remote_id
        if platform == "promo-cloud" and urlsplit(url).path.lower() == "/targetedpromo/view":
            remote_id = parse_qs(urlsplit(url).query).get("promo", [None])[0]
        source_id = hashlib.sha256((platform + "|" + (remote_id or url)).encode()).hexdigest()[:20]
        if remote_id:
            existing = self.db.execute("SELECT id FROM sources WHERE platform=? AND remote_id=?",
                                       (platform, remote_id)).fetchone()
            if existing:
                source_id = existing[0]
        protected = win32crypt.CryptProtectData(url.encode(), "promo source", None, None, None, 0)
        self.db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET url_protected=excluded.url_protected",
                        (source_id, platform, remote_id, subject, protected, json.dumps(metadata), now()))
        self.db.commit()
        return source_id

    def source_url(self, source_id):
        import win32crypt
        row = self.db.execute("SELECT url_protected FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown source")
        return win32crypt.CryptUnprotectData(bytes(row[0]), None, None, None, 0)[1].decode()

    def claim(self, asset_key, source_id, title, fmt):
        """A durable claim cannot expire into a second transfer after an uncertain failure."""
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO assets(asset_key,source_id,title,format,stage,updated_at) VALUES(?,?,?,?,?,?)",
                            (asset_key, source_id, title, fmt, "planned", now()))
            if self.db.execute("SELECT stage FROM assets WHERE asset_key=?", (asset_key,)).fetchone()[0] != "planned":
                return False
            cursor = self.db.execute("INSERT OR IGNORE INTO claims VALUES(?,?,?)", (asset_key, str(os.getpid()), now()))
            if not cursor.rowcount:
                return False
            self.db.execute("UPDATE assets SET stage='claimed',updated_at=? WHERE asset_key=?", (now(), asset_key))
        return True

    def asset(self, asset_key):
        return self.db.execute("SELECT * FROM assets WHERE asset_key=?", (asset_key,)).fetchone()

    def resume_claim(self, asset_key):
        row = self.db.execute("SELECT owner FROM claims WHERE asset_key=?", (asset_key,)).fetchone()
        if row is None:
            return False
        owner = row[0]
        if owner != str(os.getpid()) and process_alive(owner):
            return False
        with self.db:
            result = self.db.execute("UPDATE claims SET owner=?,acquired_at=? WHERE asset_key=? AND owner=?",
                                     (str(os.getpid()), now(), asset_key, owner))
        return bool(result.rowcount)

    def release_claim(self, asset_key, reason):
        """Deliberately reopen an uncertain asset. Never automatic: the platform may already have counted the attempt."""
        with self.db:
            row = self.db.execute("SELECT stage FROM assets WHERE asset_key=?", (asset_key,)).fetchone()
            if row is None or row[0] in {"planned", "delivery_verified"}:
                raise ValueError("Nothing to release for this asset")
            self.db.execute("DELETE FROM claims WHERE asset_key=?", (asset_key,))
            self.db.execute("UPDATE assets SET stage='planned',updated_at=? WHERE asset_key=?", (now(), asset_key))
        self.event(None, "claim_released", row[0], asset_key=asset_key, reason=reason)

    def start_pass(self, started=None):
        """One "update the promos" run. Within a pass a lossless copy replaces a lossy one the pass delivered."""
        started = started or now()
        self.db.execute("INSERT OR IGNORE INTO passes VALUES(?)", (started,))
        self.db.commit()
        return started

    def pass_started(self):
        return self.db.execute("SELECT MAX(started) FROM passes").fetchone()[0]

    def reopen_for_replacement(self, asset_key):
        """Reopen a lossy delivery of this pass; the old Dropbox file is removed only after the new one verifies."""
        with self.db:
            row = self.db.execute("SELECT stage,dropbox_path,content_hash FROM assets WHERE asset_key=?",
                                  (asset_key,)).fetchone()
            if row is None or row[0] not in {"delivery_verified", "held_for_research"} or not row[1]:
                raise ValueError("Only a delivered or held asset can be replaced")
            self.db.execute("INSERT OR IGNORE INTO replacements VALUES(?,?,?,?)", (asset_key, row[1], row[2], now()))
            self.db.execute("DELETE FROM claims WHERE asset_key=?", (asset_key,))
            self.db.execute("UPDATE assets SET stage='planned',updated_at=? WHERE asset_key=?", (now(), asset_key))
        self.event(None, "replace", "reopened", asset_key=asset_key, old_path=row[1])

    def asset_update(self, asset_key, stage, **values):
        allowed = {"local_path", "raw_sha256", "dropbox_path", "content_hash", "metadata"}
        if set(values) - allowed:
            raise ValueError("Unsupported asset fields")
        fields = {"stage": stage, "updated_at": now(), **values}
        self.db.execute("UPDATE assets SET " + ",".join(k + "=?" for k in fields) + " WHERE asset_key=?",
                        (*fields.values(), asset_key))
        self.db.commit()

    def export_logs(self):
        """The human-readable summaries as {name: bytes}; built in memory, nothing is left on this computer."""
        rows = self.db.execute("""SELECT e.utc,coalesce(s.platform,'system') platform,
          coalesce(s.remote_id,s.id,'') release_id,coalesce(s.subject,'') promo,
          e.action,e.status,e.details FROM events e LEFT JOIN sources s ON s.id=e.source_id
          ORDER BY sequence""").fetchall()
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["UTC", "Platform", "Release ID", "Promo", "Action", "Status", "Details"])
        lines = ["PROMO DOWNLOAD LOG", "Updated: " + now(),
                 "Test folder: " + DEST,
                 "Views and downloads can change platform indicators. Check each event below.",
                 "Only delivery_verified confirms a processed file exists in Dropbox.",
                 "An intent or uncertain result must not be interpreted as a completed download.", ""]
        for row in rows:
            values = list(row)
            # Keep spreadsheet formula injection out of the human-facing CSV.
            writer.writerow(["'" + str(v) if str(v).startswith(("=", "+", "-", "@")) else v for v in values])
            lines.append(" | ".join(str(v) for v in values))
        readme = ("PROMO DOWNLOADS\n\nPromos collected by the promo automation live here.\n"
                  "The download_log.txt and download_log.csv files identify viewed promos, download attempts, "
                  "feedback, processing, delivery and any uncertain state.\n"
                  "A platform may mark a promo read/downloaded before a transfer or upload has completed. "
                  "Use delivery_verified entries to find the final processed files.\n"
                  "Failed or uncertain entries remain outstanding and must not be silently skipped.\n"
                  "The audit subfolder receives event batches about every minute while a run works. Human-readable "
                  "log summaries refresh at most every 15 minutes and at the end of every run.\n")
        return {"download_log.csv": output.getvalue().encode("utf-8"),
                "download_log.txt": ("\n".join(lines) + "\n").encode("utf-8"),
                "README.txt": readme.encode("utf-8")}

    def _log_state(self):
        row = self.db.execute("SELECT * FROM log_checkpoint WHERE singleton=1").fetchone()
        return (row["published_sequence"], row["summary_at"], row["events_at"]) if row else (0, 0, 0)

    def sync_logs(self, force=False, summary=False):
        """Publish logged events to Dropbox in small append-only batches.

        Every event is committed to the local database before any outward action, so a waiting batch loses nothing
        and the next sync of any worker publishes it. A plain call publishes at most every EVENT_SECONDS; force (a
        worker or step finishing) publishes at once and refreshes the human-readable summaries when they are older
        than SUMMARY_SECONDS; summary (the run's summary phase) always refreshes them.
        Server-returned Dropbox content hashes verify every log upload. Audio delivery keeps readback checks.
        """
        published, summary_at, events_at = self._log_state()
        pending = self.db.execute("SELECT 1 FROM events WHERE sequence>? LIMIT 1", (published,)).fetchone()
        events_due = pending and (force or summary or time.time() - events_at >= EVENT_SECONDS)
        summary_due = summary or (force and time.time() - summary_at >= SUMMARY_SECONDS)
        if not events_due and not summary_due:
            return
        import dropbox
        started, transferred = time.perf_counter(), 0
        with log_lock(self.root):
            # Another worker may have published while this one waited for the lock.
            published, summary_at, events_at = self._log_state()
            rows = self.db.execute("""SELECT e.sequence,e.utc,e.source_id,coalesce(s.platform,'system') platform,
                       coalesce(s.subject,'') promo,e.action,e.status,e.details FROM events e
                       LEFT JOIN sources s ON s.id=e.source_id WHERE e.sequence>? ORDER BY e.sequence""", (published,)).fetchall()
            shared, client = dropbox_skill()
            if rows and events_due:
                data = ("\n".join(json.dumps(dict(row), ensure_ascii=False) for row in rows) + "\n").encode("utf-8")
                audit_folder = DEST + "/audit"
                if not getattr(self, "_audit_folder_ready", False):
                    shared.ensure_dropbox_folder(client, audit_folder)
                    self._audit_folder_ready = True
                target = audit_folder + f"/events-{rows[0]['sequence']:09d}-{rows[-1]['sequence']:09d}.jsonl"
                wanted = dropbox_bytes_hash(data)
                try:
                    remote = client.files_upload(data, target, mode=dropbox.files.WriteMode.add,
                                                 autorename=False, strict_conflict=True, mute=True)
                except dropbox.exceptions.ApiError as error:
                    if not (error.error.is_path() and error.error.get_path().reason.is_conflict()):
                        raise
                    remote = client.files_get_metadata(target)
                if remote.content_hash != wanted:
                    raise RuntimeError("Audit event upload hash differs; outward action remains blocked")
                published = rows[-1]["sequence"]
                transferred += len(data)
                self.db.execute("INSERT INTO log_checkpoint(singleton,published_sequence,summary_at,events_at) VALUES(1,?,?,?) "
                                "ON CONFLICT(singleton) DO UPDATE SET published_sequence=excluded.published_sequence,"
                                "events_at=excluded.events_at", (published, summary_at, time.time()))
                self.db.commit()
            if summary_due:
                for name, data in self.export_logs().items():
                    wanted = dropbox_bytes_hash(data)
                    prior = self.db.execute("SELECT content_hash FROM log_files WHERE name=?", (name,)).fetchone()
                    if prior and prior[0] == wanted:
                        continue
                    remote = client.files_upload(data, DEST + "/" + name, mode=dropbox.files.WriteMode.overwrite, mute=True)
                    if remote.content_hash != wanted:
                        raise RuntimeError("Shared summary upload hash differs")
                    transferred += len(data)
                    self.db.execute("INSERT INTO log_files VALUES(?,?) ON CONFLICT(name) DO UPDATE SET content_hash=excluded.content_hash",
                                    (name, wanted))
                    self.db.commit()
                self.db.execute("INSERT INTO log_checkpoint(singleton,published_sequence,summary_at) VALUES(1,?,?) "
                                "ON CONFLICT(singleton) DO UPDATE SET summary_at=excluded.summary_at", (published, time.time()))
                self.db.commit()
        if transferred:
            self.timing("log_sync", time.perf_counter() - started, byte_count=transferred)


def open_source(store, source_id):
    row = store.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown source")
    store.event(source_id, "view", "intent", platform_read_status="unknown", platform_download_status="unknown")
    store.sync_logs()
    expression = ('JSON.stringify({url:location.origin+location.pathname,title:document.title,'
                  'text:document.body.innerText.slice(0,2200),'
                  'forms:[...document.forms].map(f=>({action:f.action.split("?")[0],method:f.method}))})')
    code = "\n".join(["new_tab(" + repr(store.source_url(source_id)) + ")", "wait_for_load()",
                      "print(js(" + repr(expression) + "))"])
    try:
        output = browser_code(code)
    except Exception:
        store.event(source_id, "view", "uncertain", note="Inspect current page before retrying")
        store.sync_logs()
        raise
    store.event(source_id, "view", "opened", platform_read_status="unknown",
                platform_download_status="unknown", note="Navigation completed; no audio download requested")
    store.sync_logs()
    print(output)


def _library_row(entry):
    return (entry.path_lower, entry.id, entry.rev, entry.size, entry.content_hash,
            normalized(re.sub(r"\s*\[[^]]*\]\s*$", "", Path(entry.name).stem)), now())


def index_additional_libraries(store, shared, client, full=False):
    """Disjoint optional ownership roots, each with its own incremental Dropbox cursor."""
    import dropbox
    roots = []
    for value in SETTINGS.get("additional_library_roots", []):
        root = value.rstrip("/")
        if not root.startswith("/") or not root:
            raise ValueError("Additional library roots must be absolute non-root Dropbox folders")
        folded = root.casefold()
        # A nested or parent root would give two cursors control of the same rows.
        for other in [LIBRARY_ROOT, *roots]:
            o = other.rstrip("/").casefold()
            if folded == o or folded.startswith(o + "/") or o.startswith(folded + "/"):
                raise ValueError("Library roots must not overlap")
        roots.append(root)
    active = {r.casefold() for r in roots}
    for (old,) in store.db.execute("SELECT root FROM library_extra_cursors").fetchall():
        if old not in active:
            with store.db:
                store.db.execute("DELETE FROM library WHERE substr(path,1,?)=?", (len(old) + 1, old + "/"))
                store.db.execute("DELETE FROM library_extra_cursors WHERE root=?", (old,))
    reports = []
    for root in roots:
        key = root.casefold()
        saved = store.db.execute("SELECT cursor,listed_at FROM library_extra_cursors WHERE root=?", (key,)).fetchone()
        incremental = bool(saved) and not full and time.time() - saved["listed_at"] < FULL_RELIST_SECONDS
        entries, calls = [], 0
        while True:
            try:
                page = (client.files_list_folder_continue(saved["cursor"]) if incremental
                        else client.files_list_folder(root, recursive=True))
                calls += 1
                while True:
                    entries.extend(page.entries)
                    if not page.has_more:
                        break
                    page = client.files_list_folder_continue(page.cursor)
                    calls += 1
                break
            except dropbox.exceptions.ApiError as error:
                if incremental and getattr(error.error, "is_reset", lambda: False)():
                    incremental, entries = False, []
                    continue
                raise
        # Do not advance a cursor or remove its old rows until every page succeeded.
        with store.db:
            if not incremental:
                store.db.execute("DELETE FROM library WHERE substr(path,1,?)=?", (len(key) + 1, key + "/"))
            for entry in entries:
                if isinstance(entry, shared.FileMetadata):
                    store.db.execute("INSERT OR REPLACE INTO library VALUES(?,?,?,?,?,?,?)", _library_row(entry))
                elif incremental and isinstance(entry, dropbox.files.DeletedMetadata):
                    path = entry.path_lower
                    store.db.execute("DELETE FROM library WHERE path=? OR substr(path,1,?)=?",
                                     (path, len(path) + 1, path + "/"))
            store.db.execute("INSERT OR REPLACE INTO library_extra_cursors VALUES(?,?,?)",
                             (key, page.cursor, saved["listed_at"] if incremental else time.time()))
        reports.append({"root": root, "list_requests": calls, "mode": "changes" if incremental else "full"})
    return reports


def index_library(store, full=False):
    """Mirror every file under LIBRARY_ROOT into the library table.

    The first listing is complete (about 230 requests for 79,000 files); later ones apply only the changes Dropbox
    reports since the saved cursor, usually one request. A complete relist still runs every FULL_RELIST_SECONDS and
    whenever Dropbox resets the cursor. Changes and the new cursor commit together, so a failed page changes nothing.
    """
    import dropbox
    shared, client = dropbox_skill()
    saved = store.db.execute("SELECT cursor,listed_at FROM library_cursor WHERE singleton=1").fetchone()
    incremental = bool(saved) and not full and time.time() - saved["listed_at"] < FULL_RELIST_SECONDS
    entries, calls = [], 1
    try:
        page = (client.files_list_folder_continue(saved["cursor"]) if incremental
                else client.files_list_folder(LIBRARY_ROOT, recursive=True))
        while True:
            entries.extend(page.entries)
            if not page.has_more:
                break
            page = client.files_list_folder_continue(page.cursor)
            calls += 1
    except dropbox.exceptions.ApiError as error:
        reset = getattr(error.error, "is_reset", lambda: False)()
        if incremental and reset:
            return index_library(store, full=True)
        raise
    with store.db:
        if incremental:
            # In listing order: a move arrives as a deletion of the old path and a file at the new one.
            for entry in entries:
                if isinstance(entry, shared.FileMetadata):
                    store.db.execute("INSERT OR REPLACE INTO library VALUES(?,?,?,?,?,?,?)", _library_row(entry))
                elif isinstance(entry, dropbox.files.DeletedMetadata):
                    path = entry.path_lower
                    store.db.execute("DELETE FROM library WHERE path=? OR substr(path,1,?)=?",
                                     (path, len(path) + 1, path + "/"))
            store.db.execute("UPDATE library_cursor SET cursor=? WHERE singleton=1", (page.cursor,))
        else:
            # A file changed during a long listing can appear on two pages; the later entry is the current one.
            files = {e.path_lower: e for e in entries if isinstance(e, shared.FileMetadata)}
            root = LIBRARY_ROOT.rstrip("/").casefold()
            store.db.execute("DELETE FROM library WHERE path=? OR substr(path,1,?)=?",
                             (root, len(root) + 1, root + "/"))
            store.db.executemany("INSERT INTO library VALUES(?,?,?,?,?,?,?)", [_library_row(e) for e in files.values()])
            store.db.execute("DELETE FROM library_keys WHERE path NOT IN (SELECT path FROM library)")
            store.db.execute("INSERT OR REPLACE INTO library_cursor VALUES(1,?,?)", (page.cursor, time.time()))
    additional = index_additional_libraries(store, shared, client, full=not incremental)
    calls += sum(r["list_requests"] for r in additional)
    count = store.db.execute("SELECT count(*) FROM library").fetchone()[0]
    result = {"indexed_files": count, "list_requests": calls, "mode": "changes" if incremental else "full",
              "changes": len(entries) if incremental else None}
    if additional:
        result["additional_roots"] = additional
    store.event(None, "library_index", "complete", files=count, list_requests=calls, mode=result["mode"],
                changes=result["changes"])
    print(json.dumps(result))
    return result


def discover_mail(store, since, limit):
    from bs4 import BeautifulSoup
    skill = email_skill()
    connection = skill.imap_login(ACCOUNT)
    count, sources = 0, set()
    try:
        skill._select(connection, "INBOX", readonly=True)
        validity = connection.response("UIDVALIDITY")[1][0].decode()
        typ, data = connection.uid("search", None, "SINCE", skill.parse_date_arg(since),
                                   "FROM", '"inflyteapp.com"')
        skill._require_imap_ok("promo mail search", typ, data)
        uids = data[0].split()[-limit:]
        for offset in range(0, len(uids), 25):
            typ, data = connection.uid("fetch", b",".join(uids[offset:offset+25]), "(BODY.PEEK[])")
            skill._require_imap_ok("promo mail fetch", typ, data)
            for item in data:
                if not isinstance(item, tuple):
                    continue
                uid = re.search(rb"UID (\d+)", item[0]).group(1).decode()
                message = email.message_from_bytes(item[1])
                subject = skill.decode_str(message.get("Subject", ""))
                urls = set()
                for part in message.walk():
                    if part.get_content_type() != "text/html":
                        continue
                    body = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
                    soup = BeautifulSoup(body, "html.parser")
                    for anchor in soup.find_all("a", href=True):
                        url = anchor["href"]
                        parsed = urlsplit(url)
                        # Only actual recipient promo links, not tracking pixels, stores or unsubscribe.
                        if parsed.hostname in {"inflyteapp.com", "www.inflyteapp.com"} and re.fullmatch(r"/review/releases/[^/]+/?", parsed.path):
                            urls.add(url)
                for url in urls:
                    source_id = store.add_source("inflyte", subject, url, email_uid=uid)
                    store.db.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?)",
                                     (ACCOUNT, "INBOX", validity, uid, message.get("Message-ID", ""),
                                      subject, message.get("Date", ""), source_id))
                    sources.add(source_id)
                count += 1
        store.db.commit()
    finally:
        connection.logout()
    store.event(None, "mail_discovery", "complete", messages=count, sources=len(sources), since=since)
    print(json.dumps({"messages": count, "recipient_links": len(sources)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("index-library")
    discover = commands.add_parser("discover-mail")
    discover.add_argument("--since", default="30d")
    discover.add_argument("--limit", type=int, default=100)
    commands.add_parser("sync-log")
    view = commands.add_parser("open-source")
    view.add_argument("source_id")
    commands.add_parser("status")
    begin = commands.add_parser("start-pass", help="Start an 'update the promos' pass (lossless replaces this pass's lossy copies)")
    begin.add_argument("--started", help="UTC ISO time; defaults to now")
    args = parser.parse_args()
    store = Store(args.state)
    if args.command == "init":
        store.event(None, "pilot", "initialized", destination=DEST,
                    note="No promos viewed/downloaded yet; shared log established before platform actions.")
        store.sync_logs()
    elif args.command == "index-library":
        index_library(store)
    elif args.command == "discover-mail":
        if args.limit <= 0:
            parser.error("--limit must be positive")
        discover_mail(store, args.since, args.limit)
    elif args.command == "sync-log":
        store.sync_logs()
    elif args.command == "open-source":
        open_source(store, args.source_id)
    elif args.command == "start-pass":
        print(json.dumps({"pass_started": store.start_pass(args.started)}))
    else:
        print(json.dumps({table: store.db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                          for table in ["sources", "messages", "library", "assets", "events"]}))


if __name__ == "__main__":
    main()
