"""Protected normalized track inventories for incremental coordinator planning.

Catalogues and mutable share manifests are revalidated each run. Link-only campaigns have no cheap
content revision; retain their inventory for the 30-day offer window, invalidate on newer mail/source
changes, and always refresh before real work. --refresh-metadata also detects unannounced edits.
Only metadata is cached here, never a claim, selection decision, delivery result or usable download session.
"""
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json

from promo_adapters import ADAPTERS
from promo_automation import now
from promo_cloud import protect_json, read_protected_json

MAX_AGE_SECONDS = 30 * 86400
# Windows file timestamps can lead datetime.now() slightly; bounded skew is not fresh metadata forever.
CLOCK_SKEW_SECONDS = 1
MANIFEST_PLATFORMS = {"dropbox", "google-drive", "onedrive", "soundcloud", "disco"}
PREFIXES = {"promo-cloud": "promo-cloud", "promoly": "promoly", "ampsuite": "ampsuite",
            "label-engine": "labelengine", "promochoons": "promochoons", "disco": "disco",
            "promo-delivery": "promo-delivery", "promopush": "promopush", "beatrolo": "beatrolo",
            "needlepool": "needlepool", "ctrl-promo": "ctrlpromo", "fatdrop": "fatdrop",
            "label-portal": "label-portal", "hearthis": "hearthis", "soundcloud": "soundcloud",
            "dropbox": "dropbox", "google-drive": "gdrive", "onedrive": "onedrive", "wetransfer": "wetransfer"}
VOLATILE_FIELDS = {"status", "viewed", "opened", "downloaded", "catalogue_seen", "read_at", "checked_at"}
MANIFEST_VOLATILE_FIELDS = {"inspected_at", "read_at", "checked_at", "requests", "playback_count", "download_count",
                            "likes_count", "comment_count", "reposts_count", "favoritings_count", "user_favorite",
                            "user_reposted", "comments_count", "track_count_downloaded"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def manifest_identity(value):
    if isinstance(value, dict):
        return {k: manifest_identity(v) for k, v in value.items() if k not in MANIFEST_VOLATILE_FIELDS}
    if isinstance(value, (list, tuple)):
        return [manifest_identity(v) for v in value]
    return value


def complete(release):
    return bool(isinstance(release, dict) and release.get("title") and "label" in release and "gated" in release
                and isinstance(release.get("tracks"), list) and release["tracks"] and all(
                    isinstance(t, dict) and t.get("name") and isinstance(t.get("files"), list)
                    and all(isinstance(f, dict) and f.get("type") for f in t["files"]) for t in release["tracks"]))


class PlanningAdapter:
    def __init__(self, adapter, source_id, release):
        self.adapter, self.source_id, self.inventory = adapter, source_id, release

    def release(self, source_id):
        if source_id != self.source_id:
            raise ValueError("Planning inventory belongs to a different source")
        return deepcopy(self.inventory)


class PlanningCache:
    def __init__(self, store, adapter, platform):
        self.store, self.real, self.platform = store, adapter, platform
        # Test doubles and unregistered integrations retain the coordinator's original contract.
        self.supported = platform in ADAPTERS and type(adapter).__module__ == ADAPTERS[platform][0]
        self.memory = {}
        self.manifests = {}

    def _signature(self, source_id):
        row = self.store.db.execute("SELECT remote_id,subject,metadata FROM sources WHERE id=? AND platform=?",
                                    (source_id, self.platform)).fetchone()
        if row is None:
            raise ValueError("Unknown planning source")
        metadata = {k: v for k, v in json.loads(row["metadata"]).items() if k not in VOLATILE_FIELDS}
        if self.platform == "promo-cloud":
            metadata.pop("release_title", None)  # The freshly updated source subject already carries this field.
        mail = [list(r) for r in self.store.db.execute(
            "SELECT message_id,received FROM messages WHERE source_id=? ORDER BY message_id,received", (source_id,))]
        return digest([self.platform, row["remote_id"], row["subject"], self.store.source_url(source_id), metadata, mail])

    def _native_path(self, source_id):
        identity = source_id
        if self.platform == "ctrl-promo":
            identity = self.real._target(source_id)[0]
        return self.store.root / f"{PREFIXES[self.platform]}-{identity}.dpapi"

    def _recent_native(self, source_id):
        """Migrate only snapshots explicitly bound to an inspected run at upgrade, never unbound old metadata."""
        path = self._native_path(source_id)
        binding = self.store.root / f"planning-bind-{self.platform}-{source_id}.dpapi"
        if not path.exists() or not binding.exists():
            return False
        fetched = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if not -CLOCK_SKEW_SECONDS <= (datetime.now(timezone.utc) - fetched).total_seconds() <= MAX_AGE_SECONDS:
            return False
        try:
            proof = read_protected_json(binding)
            if (proof.get("signature") != self._signature(source_id)
                    or proof.get("native_sha256") != hashlib.sha256(path.read_bytes()).hexdigest()):
                return False
            for (received,) in self.store.db.execute("SELECT received FROM messages WHERE source_id=?", (source_id,)):
                offer = parsedate_to_datetime(received)
                if (offer if offer.tzinfo else offer.replace(tzinfo=timezone.utc)) > fetched:
                    return False
            saved = read_protected_json(path)
            # The proof already binds the exact catalogue metadata. Promo Cloud's catalogue count
            # can be zero for inspected multi-track releases, so it is a change marker, not a row-count invariant.
            return isinstance(saved, dict)
        except Exception:
            return False

    def adapter(self, source_id, refresh=False, inspected_since=None):
        if source_id in self.memory and not refresh:
            return PlanningAdapter(self.real, source_id, self.memory[source_id]), {"source": "memory"}
        # Inflyte's refreshed bulk catalogue already includes full current track lists. Its special hook
        # defers per-recipient availability checks until collection actually needs a file.
        if hasattr(self.real, "planning_release"):
            release = (self.real.planning_release(source_id, refresh=refresh) if self.platform == "inflyte"
                       else self.real.planning_release(source_id))
            if not complete(release):
                raise RuntimeError("Incomplete planning inventory")
            self.memory[source_id] = deepcopy(release)
            return PlanningAdapter(self.real, source_id, release), {"source": "catalogue"}
        path = self.store.root / f"planning-{self.platform}-{source_id}.dpapi"
        signature = self._signature(source_id)
        saved = None
        if path.exists() and not refresh:
            try:
                candidate = read_protected_json(path)
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(candidate["fetched_at"])).total_seconds()
                if (candidate.get("version") == 1 and candidate.get("signature") == signature
                        and -CLOCK_SKEW_SECONDS <= age <= MAX_AGE_SECONDS and complete(candidate.get("release"))):
                    saved = candidate
            except Exception:
                pass
        manifest, checked = None, False
        same_run = saved and inspected_since and saved.get("checked_at", "") >= inspected_since
        if self.platform in MANIFEST_PLATFORMS and not same_run:
            manifest = digest(manifest_identity(self.real.snapshot(source_id, refresh=True)))
            self.manifests[source_id] = manifest
            checked = True
            if saved and saved.get("manifest") != manifest:
                saved = None
        if saved:
            if checked:
                saved["checked_at"] = now()
                protect_json(path, saved)
            self.memory[source_id] = deepcopy(saved["release"])
            return PlanningAdapter(self.real, source_id, saved["release"]), {"source": "cache", "manifest_checked": checked}
        # Migration is one-time. A changed/corrupt/expired normalized record must never bless the old
        # native snapshot with the new signature, otherwise a reoffer could silently retain old tracks.
        native = not path.exists() and not refresh and self.platform not in MANIFEST_PLATFORMS and self._recent_native(source_id)
        if not checked and hasattr(self.real, "snapshot"):
            self.real.snapshot(source_id, refresh=not native)
        if hasattr(self.real, "prepare_source"):
            self.real.prepare_source(source_id)
        release = self.real.release(source_id)
        if not complete(release):
            raise RuntimeError("Incomplete planning inventory")
        stamp = now()
        fetched = (datetime.fromtimestamp(self._native_path(source_id).stat().st_mtime, timezone.utc).isoformat()
                   if native else stamp)
        record = {"version": 1, "signature": self._signature(source_id), "manifest": manifest, "fetched_at": fetched,
                  "checked_at": stamp, "release": release}
        protect_json(path, record)
        self.memory[source_id] = deepcopy(release)
        return PlanningAdapter(self.real, source_id, release), {"source": "native_cache" if native else "fresh",
                                                              "manifest_checked": checked}

    def refresh_for_collection(self, source_id, already_fresh=False):
        """Persist the whole newly inspected inventory before any file is attempted.

        If one download fails after an earlier track succeeds, reconcile must still see a newly added remix.
        An interrupted refresh leaves an explicit invalid record, so the old native snapshot cannot be adopted.
        """
        self.memory.pop(source_id, None)
        path = self.store.root / f"planning-{self.platform}-{source_id}.dpapi"
        protect_json(path, {"version": 0, "state": "collection_refresh_pending"})
        manifest = self.manifests.get(source_id)
        if hasattr(self.real, "snapshot") and not already_fresh:
            snapshot = self.real.snapshot(source_id, refresh=True)
            if self.platform in MANIFEST_PLATFORMS:
                manifest = digest(manifest_identity(snapshot))
        elif self.platform == "inflyte":
            remote = self.store.db.execute("SELECT remote_id FROM sources WHERE id=?", (source_id,)).fetchone()[0]
            self.real.inspect(source_id, remote)
        if hasattr(self.real, "prepare_collection_source"):
            self.real.prepare_collection_source(source_id)
        elif hasattr(self.real, "prepare_source"):
            self.real.prepare_source(source_id)
        release = self.real.release(source_id)
        if not complete(release):
            raise RuntimeError("Incomplete collection inventory")
        stamp = now()
        record = {"version": 1, "signature": self._signature(source_id), "manifest": manifest,
                  "fetched_at": stamp, "checked_at": stamp, "release": release}
        protect_json(path, record)
        self.memory[source_id] = deepcopy(release)
