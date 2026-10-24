"""Report live inventory health and reconcile legacy Unicode upload errors using Dropbox only."""
import argparse
import json
import os
from pathlib import Path

from promo_automation import DEST, Store, atomic_text, dropbox_hash, dropbox_skill, now, process_alive
from promo_pipeline import mix_key, inventory_key, tag_mismatches, validate_audio
from process_promos import read_existing_tags, verify_tags


def status(store):
    counts = [dict(r) for r in store.db.execute("""SELECT s.platform,a.stage,count(*) AS files FROM assets a
              JOIN sources s ON s.id=a.source_id GROUP BY s.platform,a.stage""")]
    outstanding = []
    for row in store.db.execute("""SELECT a.*,s.platform FROM assets a JOIN sources s ON s.id=a.source_id
                                WHERE stage != 'delivery_verified' ORDER BY a.updated_at"""):
        owner = store.db.execute("SELECT owner FROM claims WHERE asset_key=?", (row["asset_key"],)).fetchone()
        outstanding.append({"platform": row["platform"], "source_id": row["source_id"], "title": row["title"],
                            "stage": row["stage"], "local_file_exists": bool(row["local_path"] and Path(row["local_path"]).is_file()),
                            "owner_pid": owner[0] if owner else None, "owner_alive": process_alive(owner[0]) if owner else False})
    upgrades = sorted({json.loads(r[0])["track"] for r in store.db.execute("SELECT details FROM events WHERE status='upgrade_candidate'")})
    latest = store.db.execute("SELECT utc,source_id,action,status FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
    timings = [dict(row) for row in store.db.execute("""SELECT operation,count(*) samples,
               round(avg(seconds),3) average_seconds,round(sum(seconds),3) total_seconds,
               sum(bytes) bytes FROM timings GROUP BY operation""")]
    return {"as_of_utc": now(), "counts": counts, "outstanding": outstanding, "timings": timings,
            "upgrade_candidates_ask_user": upgrades, "latest_event": dict(latest) if latest else None}


def reconcile_normalized_uploads(store):
    """Only recover processed assets whose legacy upload failed solely on Dropbox's path normalization.

    This never contacts Inflyte, repeats feedback, releases claims or redownloads a provider's audio.
    Files are read back from Dropbox, decoded, checked against the original platform metadata/precision,
    and hash-verified before the inventory is updated.
    """
    from inflyte import Inflyte
    adapter = Inflyte.__new__(Inflyte)
    adapter.store = store
    _, client = dropbox_skill()
    results = []
    rows = store.db.execute("""SELECT a.* FROM assets a JOIN sources s ON s.id=a.source_id
       WHERE s.platform='inflyte' AND a.stage='processed' AND EXISTS(
       SELECT 1 FROM events e WHERE e.source_id=a.source_id AND e.status='release_failed'
       AND json_extract(e.details,'$.error') LIKE 'Dropbox renamed the upload;%')""").fetchall()
    for row in rows:
        if not row["local_path"]:
            continue
        output_name = Path(row["local_path"]).name
        target = DEST + "/" + output_name
        folder = store.root / "staging" / "reconcile" / row["source_id"]
        local = folder / output_name
        try:
            release = adapter.release(row["source_id"])
            track = next(t for t in release["tracks"] if inventory_key(store, t["name"]) == row["asset_key"])
            remote = client.files_get_metadata(target)
            store.event(row["source_id"], "reconcile", "intent", track=row["title"],
                        note="Dropbox readback after legacy Unicode path comparison failure; no platform contact")
            store.sync_logs()
            folder.mkdir(parents=True, exist_ok=True)
            client.files_download_to_file(str(local), target)
            if local.stat().st_size != remote.size or dropbox_hash(local) != remote.content_hash:
                raise RuntimeError("Dropbox readback content hash or size differs")
            audio = validate_audio(local, track.get("duration"))
            original = json.loads(row["metadata"]).get("raw_audio", {})
            if original and (audio["sample_rate"] != original["sample_rate"] or audio["bits"] < original["bits"]
                             or audio["channels"] != original["channels"]):
                raise RuntimeError("Readback precision differs from the validated source")
            tags = read_existing_tags(str(local))
            item = {"artist": tags.get("artist"), "title": tags.get("title"),
                    "label": tags.get("grouping") or tags.get("publisher")}
            problems = tag_mismatches(track, release["label"], item) + verify_tags(str(local), has_label=True)
            if problems:
                raise RuntimeError("Readback metadata needs review: " + "; ".join(problems))
            store.asset_update(row["asset_key"], "delivery_verified", dropbox_path=remote.path_display,
                               content_hash=remote.content_hash, local_path=None)
            store.event(row["source_id"], "delivery", "delivery_verified", track=row["title"],
                        dropbox_path=remote.path_display, final_name=remote.name, recovered=True,
                        note="Reconciled existing upload with Dropbox hash, full decode, precision and platform tag checks")
            os.remove(local)
            folder.rmdir()
            results.append({"track": row["title"], "result": "delivery reconciled"})
        except Exception as error:
            store.event(row["source_id"], "reconcile", "needs_review", track=row["title"], error=str(error)[:180])
            results.append({"track": row["title"], "result": "needs_review", "error": type(error).__name__})
    if results:
        store.sync_logs()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconcile-normalized-uploads", action="store_true")
    parser.add_argument("--save", action="store_true", help="save compact current audit in the runtime directory")
    args = parser.parse_args()
    store = Store()
    try:
        recovery = reconcile_normalized_uploads(store) if args.reconcile_normalized_uploads else None
        result = status(store)
        if recovery is not None:
            result["reconciliation"] = recovery
        if args.save:
            atomic_text(store.root / "review-audit.json", json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
