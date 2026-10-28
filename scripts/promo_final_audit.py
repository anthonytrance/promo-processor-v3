"""Read back every test audio file, check filenames/tags/audio and reconcile other folders."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import unicodedata

from mutagen import File as MutagenFile

from promo_automation import DEST, HOLD, atomic_text, dropbox_hash, dropbox_skill, index_library, now
from promo_pipeline import (AUDIO_SUFFIXES, LOSSY_SUFFIXES, loose_key, mix_key, ownership_path, probe,
                            tag_mismatches, validate_audio)
from process_promos import (apply_typo_corrections, build_target_filename, clean_master_suffixes,
                           normalize_ft_to_feat, normalize_mix_type, parse_filename,
                           read_existing_tags, verify_tags)


# Bump when a filename, tag or audio rule changes, so earlier passes are checked again.
AUDIT_RULES = "2026-10-28c"


def nfc(value):
    return unicodedata.normalize("NFC", value or "")


def candidate_key(name):
    # Explicit Original Mix and an unqualified title are candidates, never automatic proof.
    # The loose key also catches a remixer credited as artist and 'Mix'/'Version' wording; audio decides.
    name = re.sub(r"\s*[\[(]Original(?: Mix)?[\])]", "", name, flags=re.I)
    return loose_key(name) or mix_key(name)


def filename_and_tag_issues(name, local):
    tags = read_existing_tags(str(local))
    artist = normalize_ft_to_feat(tags.get("artist") or "")
    title = normalize_mix_type(clean_master_suffixes(apply_typo_corrections(tags.get("title") or "")))
    label = tags.get("grouping") or tags.get("publisher")
    info = {"artist": artist, "title": title, "label": label}
    expected = build_target_filename(info, ".aiff" if Path(name).suffix.lower() in {".wav", ".aif"} else Path(name).suffix.lower())
    issues = verify_tags(str(local), has_label=True)
    from promo_metadata import find_resolution, label_issues
    from promo_pipeline import label_is_artist, strip_track_number
    issues += label_issues(label)
    if label_is_artist(label, {"artist": artist, "name": name}) and not find_resolution(name):
        issues.append("Label is only the artist's name (the promo sender); research the actual imprint")
    if strip_track_number(name) != name:
        issues.append("Filename starts with a track number")
    if nfc(name) != nfc(expected):
        issues.append("Filename does not match the normalized artist, title, mix, label and extension: " + expected)
    parsed = parse_filename(name, {})
    issues += tag_mismatches({"artist": parsed.get("artist"), "expected_title": parsed.get("title"),
                              "name": name}, parsed.get("label"), info)
    if artist != tags.get("artist") or title != tags.get("title"):
        issues.append("Artist/title tags need the agreed feature, mix or master-suffix normalization")
    if any(not v or str(v).casefold() in {"none", "unknown", "unknown artist", "unknown title"}
           for v in (artist, title, label)) or str(label).startswith("UNKNOWN:"):
        issues.append("Required artist, title or label metadata is unresolved")
    audio = MutagenFile(str(local))
    if audio is not None and audio.tags:
        for key in audio.tags:
            text = str(audio.tags[key])
            if str(key) in {"TXXX:ENCODER", "TXXX:HANDLER_NAME"}:
                issues.append("Residual encoder/handler junk tag")
            if str(key).startswith("USLT") and not text.strip():
                issues.append("Empty lyrics tag")
            if str(key) == "TCON" and text.strip().casefold() in {"", "other", "none"}:
                issues.append("Empty or placeholder genre tag")
    return list(dict.fromkeys(issues)), tags, expected


def pcm_identity(path):
    """An exact decoded-audio digest, independent of WAV/AIFF container and tags."""
    info = probe(path)
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-vn",
                             "-c:a", "pcm_f64le", "-f", "hash", "-hash", "sha256", "-"],
                            capture_output=True, text=True, timeout=300, check=True)
    return {"sha256": result.stdout.strip().split("=", 1)[-1], "sample_rate": info["sample_rate"],
            "channels": info["channels"]}


def readback(store, client, remote, folder):
    key = hashlib.sha256((remote.id + ":" + remote.rev).encode()).hexdigest()[:24]
    local = folder / (key + Path(remote.name).suffix.lower())
    if local.is_file() and local.stat().st_size == remote.size and dropbox_hash(local) == remote.content_hash:
        return local
    folder.mkdir(parents=True, exist_ok=True)
    partial = local.with_name(local.name + ".partial")
    # Dropbox rejects the separate rev argument; a "rev:" path pins the exact revision.
    client.files_download_to_file(str(partial), "rev:" + remote.rev)
    if partial.stat().st_size != remote.size or dropbox_hash(partial) != remote.content_hash:
        raise RuntimeError("Final audit readback differs from Dropbox size/hash")
    partial.replace(local)
    return local


def remove_test_copy(store, client, remote, reason, original):
    """Only a verified duplicate test revision, never a file in another library folder."""
    path = remote.path_lower
    if not path.startswith(DEST.lower() + "/"):
        raise ValueError("Refusing to remove an audio file outside the test folder")
    store.event(None, "final_audit", "remove_test_copy_intent", path=remote.path_display,
                reason=reason, existing_library_path=original)
    store.sync_logs()
    client.files_delete_v2(remote.id, parent_rev=remote.rev)
    with store.db:
        store.db.execute("DELETE FROM library WHERE remote_id=?", (remote.id,))
        for asset in store.db.execute("SELECT asset_key,dropbox_path FROM assets WHERE dropbox_path IS NOT NULL").fetchall():
            if nfc(asset["dropbox_path"]).casefold() == nfc(path).casefold():
                store.db.execute("UPDATE assets SET stage=?,dropbox_path=NULL,local_path=NULL,updated_at=? WHERE asset_key=?",
                                 ("excluded_existing_library", now(), asset["asset_key"]))
    store.event(None, "final_audit", "test_copy_removed", path=remote.path_display, reason=reason,
                existing_library_path=original)


def resolution_and_version_issues(store, name, test_names, duration=None, owned=None, lowercase_name=False):
    """An approved research resolution that renames the file, a shorter version beside a longer one in the test
    folder, or a track already owned elsewhere in the library under another length or spelling.

    test_names and owned may be name lists or prepared VersionIndex objects (the audit builds each once).
    lowercase_name: the name comes from the lowercase library index, so only a rename beyond capitalization counts.
    """
    from promo_metadata import find_resolution
    from promo_pipeline import VersionIndex, dj_version, unmarked_name, version_kind
    issues = []
    resolution = find_resolution(name, store.root)
    if resolution:
        wanted = build_target_filename(resolution, Path(name).suffix.lower())
        if (nfc(wanted).casefold() != nfc(name).casefold()) if lowercase_name else nfc(wanted) != nfc(name):
            issues.append("Approved metadata resolution renames this file: " + wanted)
    if dj_version({"name": name, "duration": duration}):
        return issues
    kind = version_kind(name)
    longer = lambda k: (k == "extended" and kind != "extended") or (k == "full" and kind == "short")
    tests = test_names if isinstance(test_names, VersionIndex) else VersionIndex(test_names)
    if any(longer(k) for p, k in tests.versions(name, exact_artists=True) if nfc(Path(p).name).casefold() != nfc(name).casefold()):
        issues.append("Shorter version; a longer version of the same track is in the test folder")
    owned = owned if isinstance(owned, VersionIndex) or owned is None else VersionIndex(owned)
    # An unmarked library copy of an extended mix is the same track (2026-10-28, Amos & Riot Night).
    hits = [p for p, k in (owned.versions(name, exact_artists=True) if owned else [])
            if longer(k) or k == kind or (kind == "extended" and k == "full" and unmarked_name(p))]
    if hits:
        issues.append("Same track already in the library in this or a longer version: " + hits[0])
    return issues


def audit(store, run_dir):
    index_library(store)
    _, client = dropbox_skill()
    report_path = Path(run_dir) / "final-audit.json"
    cache = store.root / "final-audit-audio"
    report = {"started": now(), "state": "running", "files": [], "ready_to_merge": False}
    rows = [dict(r) for r in store.db.execute("SELECT * FROM library")
            if Path(r["path"]).suffix.lower() in AUDIO_SUFFIXES]
    # Needs Research holds files that are known not to be final; they are neither audited nor duplicates of anything.
    tests = [r for r in rows if r["path"].startswith(DEST.lower() + "/") and not r["path"].startswith(HOLD.lower() + "/")]
    others = [r for r in rows if not r["path"].startswith(DEST.lower() + "/") and ownership_path(r["path"])]
    by_key, by_candidate, by_hash = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in others:
        by_key[mix_key(Path(row["path"]).name)].append(row)
        by_candidate[candidate_key(Path(row["path"]).name)].append(row)
        by_hash[row["content_hash"]].append(row)
    report.update(test_audio_files=len(tests), other_audio_files=len(others))
    # A revision that passed every check under the same rules needs no second download.
    passed_path = store.root / "audit-verified.json"
    passed = set(json.loads(passed_path.read_text(encoding="utf-8"))) if passed_path.exists() else set()
    saved = {"at": 0.0, "passed": len(passed)}
    def checkpoint(final=False):
        # 2026-10-24: the growing report and the verified list were rewritten twice per file, about 400 KB each
        # time. Progress is now saved every few seconds and at the end; a crash costs at most a few rechecks.
        if not final and time.monotonic() - saved["at"] < 10:
            return
        atomic_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        if len(passed) != saved["passed"]:
            atomic_text(passed_path, json.dumps(sorted(passed)))
            saved["passed"] = len(passed)
        saved["at"] = time.monotonic()
    checkpoint(final=True)
    # Include legacy/pilot files which have no inventory asset row.
    assets = {nfc(a["dropbox_path"]).casefold(): a for a in store.db.execute(
        "SELECT asset_key,metadata,dropbox_path FROM assets WHERE dropbox_path IS NOT NULL")}
    from promo_pipeline import VersionIndex
    test_names = VersionIndex(Path(t["path"]).name for t in tests)
    owned = VersionIndex(r["path"] for r in others)
    compared_versions = VersionIndex(r["path"] for r in others)
    compared_rows = {r["path"]: r for r in others}
    for row in tests:
        entry = {"path": row["path"], "state": "checking"}
        report["current"] = entry
        checkpoint()
        try:
            # The library listing just made carries the current revision and hash, so a revision that already
            # passed needs no metadata request of its own (one per test file before, minutes per audit).
            # The index keeps names in lowercase, so an exact-case comparison sent every file with an approved
            # resolution (154 on 2026-10-24) to a full download each audit. A capitalization-only resolution approved
            # after a revision passed is caught by the full check after the next AUDIT_RULES bump.
            name = Path(row["path"]).name
            # Credits such as '(Guest Extended Remix)' and '(Guest - Extended Mix)', missing co-artists,
            # or two trailing label brackets can miss the old string keys. These are audio candidates only.
            # Different audio here never proves a duplicate or creates a research hold by itself.
            audio_candidates = [compared_rows[p] for p, _ in compared_versions.versions(name, credit_typos=True)]
            stamp = ":".join((AUDIT_RULES, row["remote_id"], row["revision"], row["content_hash"]))
            if stamp in passed and not (by_hash.get(row["content_hash"]) or by_key.get(mix_key(name))
                                        or by_candidate.get(candidate_key(name)) or audio_candidates) \
                    and not resolution_and_version_issues(store, name, test_names, owned=owned, lowercase_name=True):
                entry.update(name=name, revision=row["revision"], content_hash=row["content_hash"],
                             state="verified", cached=True)
                by_key[mix_key(name)].append(row)
                by_candidate[candidate_key(name)].append(row)
                by_hash[row["content_hash"]].append(row)
                compared_versions.add(row["path"])
                compared_rows[row["path"]] = row
                report["files"].append(entry)
                report.pop("current", None)
                checkpoint()
                continue
            remote = client.files_get_metadata(row["remote_id"])
            stamp = ":".join((AUDIT_RULES, remote.id, remote.rev, remote.content_hash))
            local = readback(store, client, remote, cache)
            entry.update(name=remote.name, revision=remote.rev, content_hash=remote.content_hash,
                         audio=validate_audio(local))
            issues, tags, expected = filename_and_tag_issues(remote.name, local)
            issues += resolution_and_version_issues(store, remote.name, test_names, (entry["audio"] or {}).get("duration"),
                                            owned=owned)
            entry.update(tags=tags, expected_filename=expected, issues=issues)
            asset = assets.get(nfc(remote.path_lower).casefold())
            if asset:
                raw = json.loads(asset["metadata"] or "{}").get("raw_audio", {})
                out = entry["audio"]
                if raw and (out["bits"] < raw["bits"] or out["sample_rate"] != raw["sample_rate"] or out["channels"] != raw["channels"]):
                    issues.append("Source precision or channels were changed")
            candidates = {r["remote_id"]: r for r in by_hash.get(remote.content_hash, [])
                          + by_key.get(mix_key(remote.name), []) + by_candidate.get(candidate_key(remote.name), [])}
            strict_ids = set(candidates)
            candidates.update({r["remote_id"]: r for r in audio_candidates})
            duplicate = None
            different_audio = []
            own_pcm = None
            for match in candidates.values():
                original = client.files_get_metadata(match["remote_id"])
                if original.content_hash == remote.content_hash:
                    duplicate = original
                    entry["duplicate_evidence"] = "identical Dropbox content hash"
                    break
                other_local = readback(store, client, original, cache)
                own_pcm = own_pcm or pcm_identity(local)
                if pcm_identity(other_local) == own_pcm:
                    duplicate = original
                    entry["duplicate_evidence"] = "identical decoded audio, sample rate and channels"
                    break
                if match["remote_id"] in strict_ids:
                    different_audio.append(original.path_display)
            if duplicate:
                remove_test_copy(store, client, remote, entry["duplicate_evidence"], duplicate.path_display)
                entry.update(state="removed_duplicate", existing_library_path=duplicate.path_display)
            elif issues or different_audio:
                if different_audio:
                    issues.append("Same mix identity with different audio, requires review against " + "; ".join(different_audio))
                # Moved inside Dropbox, so questionable files cannot be merged by accident and nothing stays local.
                from promo_remote import hold_remote
                asset_key = asset["asset_key"] if asset else None
                moved = hold_remote(store, client, remote, issues, asset_key)
                store.event(None, "final_audit", "held_for_research", path=moved.path_display, issues=issues)
                entry.update(state="held_for_research", held_path=moved.path_display)
            else:
                entry.update(state="verified")
                passed.add(stamp)
                # Also deduplicate subsequent test outputs against this verified test file.
                by_key[mix_key(remote.name)].append(row)
                by_candidate[candidate_key(remote.name)].append(row)
                by_hash[remote.content_hash].append(row)
                compared_versions.add(row["path"])
                compared_rows[row["path"]] = row
        except Exception as error:
            entry.update(state="audit_exception", error=type(error).__name__ + ": " + str(error)[:200])
        finally:
            # Zero local retention: readbacks of this file and of compared library originals are temporary.
            for leftover in cache.glob("*") if cache.is_dir() else []:
                leftover.unlink()
        report["files"].append(entry)
        report.pop("current", None)
        checkpoint()
        print(json.dumps({"audit_checked": len(report["files"]), "total": len(tests), "state": entry["state"]}), flush=True)
    report.update(state="finished", finished=now(), counts=dict(Counter(e["state"] for e in report["files"])))
    # Held files are already out of the test folder, in Needs Research; what remains is mergeable.
    report["ready_to_merge"] = not any(e["state"] == "audit_exception" for e in report["files"])
    checkpoint(final=True)
    store.event(None, "final_audit", "finished", counts=report["counts"], ready_to_merge=report["ready_to_merge"])
    store.sync_logs(force=True)
    return {k: report[k] for k in ("state", "counts", "ready_to_merge")}
