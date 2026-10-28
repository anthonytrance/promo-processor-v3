"""Import exact labels from a configured Dropbox sample folder. Preview unless --apply is given."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import PurePosixPath
import re

from promo_automation import SETTINGS, Store, atomic_text, dropbox_bytes_hash, dropbox_skill
import promo_pipeline as pipeline


def sample_labels(entries):
    """A filename's final [Label] is a whole imprint, never its parent brand or an artist rule."""
    labels, skipped = {}, []
    for entry in entries:
        if not hasattr(entry, "content_hash") or PurePosixPath(entry.name).suffix.lower() not in pipeline.AUDIO_SUFFIXES:
            continue
        match = re.search(r"\[([^\]]+)\]$", PurePosixPath(entry.name).stem)
        label = match.group(1).strip() if match else ""
        key = pipeline.normalized(label)
        if not key or key in {"self released", "self release", "selfreleased"}:
            skipped.append(entry.name)
            continue
        labels.setdefault(key, label)
    return labels, skipped


def append_labels(data, labels):
    """Preserve existing rules, comments, delimiter and line endings, including Excel CSVs."""
    rules = pipeline.parse_exclusions_csv(data)
    added = [label for key, label in sorted(labels.items()) if key not in rules["labels"]]
    if not added:
        return data, []
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    first = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = ";" if first.count(";") > first.count(",") else ","
    newline = "\r\n" if "\r\n" in text else "\n"
    out = io.StringIO()
    writer = csv.writer(out, delimiter=delimiter, lineterminator=newline)
    for label in added:
        writer.writerow(["label", label, "label sample folder"])
    text = (text + (newline if text and not text.endswith("\n") else "")) if text else "type,value,note" + newline
    # UTF-8 BOM lets Excel keep accented and non-ASCII label names intact.
    return ("\ufeff" + text + out.getvalue()).encode("utf-8"), added


def sync_samples(store, client=None, folder=None, apply=False):
    folder = folder or SETTINGS.get("exclusion_samples_folder")
    if not folder:
        return {"state": "not_configured", "added_labels": [], "skipped_samples": []}
    from dropbox.files import WriteMode
    client = client or dropbox_skill()[1]
    page = client.files_list_folder(folder)
    entries = list(page.entries)
    while page.has_more:
        page = client.files_list_folder_continue(page.cursor)
        entries.extend(page.entries)
    _, skipped = sample_labels(entries)
    receipts_path = store.root / "exclusion-samples.json"
    receipts = json.loads(receipts_path.read_text(encoding="utf-8")) if receipts_path.exists() else {}
    folder_key = folder.rstrip("/").casefold()
    seen = set(receipts.get(folder_key, []))
    def fingerprint(entry):
        fields = [getattr(entry, k, "") for k in ("id", "rev", "content_hash", "name")]
        return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()
    new_labels, _ = sample_labels([entry for entry in entries if fingerprint(entry) not in seen])
    remote, response = client.files_download(pipeline.EXCLUSIONS_REMOTE)
    # The CSV remains the master: a manually removed rule is not re-added by an old sample.
    data, added = append_labels(response.content, new_labels)
    result = {"state": "preview", "added_labels": added, "skipped_samples": skipped}
    if not apply:
        return result
    if added:
        store.event(None, "exclusions", "import_intent", labels=added)
        store.sync_logs()
        # Refuse a concurrent CSV edit, rather than overwriting it or submitting a second request.
        uploaded = client.files_upload(data, pipeline.EXCLUSIONS_REMOTE, mode=WriteMode.update(remote.rev))
        if uploaded.content_hash != dropbox_bytes_hash(data):
            raise RuntimeError("Uploaded exclusion CSV hash differs")
        store.event(None, "exclusions", "imported", labels=added)
    try:
        cached_text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        cached_text = data.decode("cp1252")
    atomic_text(pipeline.EXCLUSIONS_CACHE, cached_text)
    receipts[folder_key] = sorted(seen | {fingerprint(entry) for entry in entries})
    atomic_text(receipts_path, json.dumps(receipts, ensure_ascii=False, indent=2))
    pipeline._EXCLUSIONS_LOADED.clear()
    result["state"] = "applied"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples-folder", help="Overrides exclusion_samples_folder in settings")
    parser.add_argument("--apply", action="store_true", help="Update the master CSV")
    parser.add_argument("--sweep", action="store_true", help="Also remove excluded files from the delivery folder")
    args = parser.parse_args()
    store = Store()
    try:
        result = sync_samples(store, folder=args.samples_folder, apply=args.apply)
        if args.sweep:
            from promo_automation import index_library
            from promo_research import purge_excluded
            index_library(store)
            rules = pipeline.load_exclusions()
            rules = {key: list(value) for key, value in rules.items()}
            rules.setdefault("labels", []).extend(pipeline.normalized(label) for label in result["added_labels"])
            result["sweep"] = purge_excluded(store, dropbox_skill()[1], args.apply, rules)
        if args.apply:
            store.sync_logs(force=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
