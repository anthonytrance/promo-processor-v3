"""Recheck held staged outputs, with no provider download or feedback."""
import argparse
from collections import Counter
import json
from pathlib import Path

from promo_automation import Store, atomic_text, dropbox_skill, now
from promo_pipeline import STAGING, deliver_file, inventory_key, tag_mismatches, validate_audio
from process_promos import read_existing_tags, verify_tags


def review(store, platform=None):
    from promo_update import adapter_for
    adapters, results = {}, []
    rows = store.db.execute("""SELECT a.*,s.platform FROM assets a JOIN sources s ON s.id=a.source_id
                            WHERE a.stage='needs_review' ORDER BY a.updated_at""").fetchall()
    for row in rows:
        if platform and row["platform"] != platform:
            continue
        item = {"asset_key": row["asset_key"], "source_id": row["source_id"], "platform": row["platform"],
                "track": row["title"], "state": "needs_research"}
        try:
            adapter = adapters.setdefault(row["platform"], adapter_for(row["platform"], store))
            release = adapter.release(row["source_id"])
            track = next(t for t in release["tracks"] if inventory_key(store, t["name"]) == row["asset_key"])
            folder = STAGING / row["source_id"]
            report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
            raw = Path(row["local_path"])
            processed = next(p for p in report["processed"] + report["skipped"] if p["original"] == raw.name)
            output = folder / "processed" / processed["target"]
            tags = read_existing_tags(str(output))
            item.update(original_filename=raw.name, existing_tags=read_existing_tags(str(raw)),
                        processed_tags=tags, local_path=str(output))
            if hasattr(adapter, "resolve_metadata"):
                adapter.resolve_metadata(row["source_id"], track, raw)
            label = track.get("label") or release["label"]
            found = {"artist": tags.get("artist"), "title": tags.get("title"),
                     "label": tags.get("grouping") or tags.get("publisher")}
            problems = tag_mismatches(track, label, found) + verify_tags(str(output), has_label=True)
            from promo_metadata import label_issues
            problems += label_issues(label)
            problems += track.get("metadata_conflicts", [])
            from promo_metadata import find_resolution
            resolution = find_resolution(raw.name, store.root) or find_resolution(row["title"] or "", store.root)
            if resolution and (resolution["artist"], resolution["title"], resolution["label"]) != (
                    found["artist"], found["title"], found["label"]):
                problems.append("Approved metadata resolution differs; promo_research delivers the corrected file")
            if any(u.get("original_filename") == raw.name for u in report.get("unknowns", [])):
                problems.append("Processor flagged unresolved metadata")
            item["problems"] = problems
            if not problems:
                info = validate_audio(output, track.get("duration"))
                original = json.loads(row["metadata"] or "{}").get("raw_audio", {})
                if not original or info["bits"] < original["bits"] or info["sample_rate"] != original["sample_rate"] or info["channels"] != original["channels"]:
                    raise RuntimeError("Cannot prove output preserved the validated source precision")
                if not store.resume_claim(row["asset_key"]):
                    item["state"] = "waiting_for_owner"
                else:
                    store.event(row["source_id"], "review", "verified_staged_output", track=row["title"],
                                note="Fresh platform, filename, tags, source precision and full audio checks passed; no provider transfer or feedback")
                    store.asset_update(row["asset_key"], "processed", local_path=str(output))
                    path = deliver_file(store, *dropbox_skill(), output, row["asset_key"])
                    store.event(row["source_id"], "delivery", "delivery_verified", track=row["title"],
                                dropbox_path=path, recovered=True)
                    item.update(state="delivered_from_staging", dropbox_path=path)
        except Exception as error:
            item["error"] = type(error).__name__ + ": " + str(error)[:250]
        results.append(item)
    report = {"as_of_utc": now(), "counts": dict(Counter(r["state"] for r in results)), "files": results}
    atomic_text(store.root / ("metadata-review" + ("-" + platform if platform else "") + ".json"),
                json.dumps(report, ensure_ascii=False, indent=2))
    if results:
        store.sync_logs(force=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform")
    args = parser.parse_args()
    store = Store()
    try:
        print(json.dumps(review(store, args.platform), ensure_ascii=False, indent=2))
    finally:
        store.db.close()
