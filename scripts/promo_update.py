"""Durable, on-demand promo update across every finished adapter.

One serial preparation (library index and a single mail scan), then a dry run of every source in parallel lanes,
one global plan that picks the best copy when two sources offer the same mix, parallel collection with a shared
Dropbox upload cap, and a serial tail: library refresh, reconcile, audit, research, audit and the summary.
Each phase runs in its own process with freshly loaded adapter code.
"""
import argparse
from collections import Counter
import email.utils
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from promo_adapters import PLATFORMS, adapter_for
from promo_automation import DEST, Store, STATE, atomic_text, index_library, now, process_alive

LANES = 4
# A session-wide login problem: stop asking that platform for the rest of the run, but report every release.
SESSION_ERRORS = {"LoginRequired", "TokenExpired"}
ACCESS_ERRORS = SESSION_ERRORS | {"AccessRequired", "ShareAccessRequired"}
UNAVAILABLE_ERRORS = {"ReleaseUnavailable", "Unavailable", "ShareUnavailable"}
WORK_ACTIONS = {"download", "replace", "resume"}


def safe_error(error):
    return re.sub(r"https?://[^\s\"'<>]+", "[protected URL]", str(error))[:300]


def save(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def load(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def classify(error):
    """Report state for a release that raised: explicit unavailability and access blocks are not failures."""
    name = type(error).__name__
    if name in UNAVAILABLE_ERRORS:
        return "unavailable"
    if name in ACCESS_ERRORS:
        return "access_blocked"
    if name == "PortalBlocked":
        return getattr(error, "status", None) or "access_blocked"
    if name in {"GateRequired", "CollectionBlocked", "ContractReview"}:
        return "blocked"
    return "exception"


def compact(value):
    """Catalogue results for the report: counts and flags only, never protected links."""
    if not isinstance(value, dict):
        return {"entries": len(value)} if isinstance(value, (list, tuple)) else None
    result = {}
    for key, item in value.items():
        if isinstance(item, (list, tuple, dict)):
            result[key] = len(item)
        elif isinstance(item, str):
            result[key] = safe_error(item)
        else:
            result[key] = item
    return result


def refresh_catalogue(adapter, platform, age_days):
    """Account catalogues find offers that mail alone misses; each adapter registers them as sources."""
    if hasattr(adapter, "discover_catalogue"):
        return adapter.discover_catalogue()
    if platform == "inflyte":
        return adapter.catalogue(max_age_days=age_days)
    if hasattr(adapter, "catalogue"):
        return adapter.catalogue()
    return None


def window_sources(store, adapter, platform, age_days):
    """(source_id, subject, state) for every stored source; state None means inside the window and collectable.

    Adapters with their own source choice (campaigns) drop old login links and aliases of one campaign.
    """
    from promo_mail import newest_offer_age
    rows = store.db.execute("SELECT id,subject FROM sources WHERE platform=? ORDER BY discovered_at,id", (platform,)).fetchall()
    chooser = getattr(adapter, "coordinator_sources", None) or getattr(adapter, "campaigns", None)
    if chooser is not None:
        selected = list(chooser(max_age_days=age_days))
        if any(not isinstance(sid, str) or not sid for sid in selected):
            raise RuntimeError(platform + " source selector must return a flat list of source IDs")
        chosen = set(selected)
        if chosen - {sid for sid, _subject in rows}:
            raise RuntimeError(platform + " source selector returned unregistered source IDs")
        return [(sid, subject, None if sid in chosen else "outside_window_or_alias") for sid, subject in rows]
    result = []
    for sid, subject in rows:
        age = newest_offer_age(store, sid)
        if platform == "promo-cloud":
            age = adapter.newest_offer_age_days(sid)
        elif platform == "inflyte" and age is None:
            from inflyte import sent_age_days
            try:
                _, entry = adapter._entry(sid, planning=True)
                age = sent_age_days(entry["promo"])
            except Exception:
                pass
        result.append((sid, subject, None if age is not None and age <= age_days else "undated_or_too_old"))
    return result


def discover_pass(store, platform, run_dir, age_days, refresh_metadata=False):
    """Plan every recent mix using validated metadata, without feedback or audio."""
    from promo_pipeline import LibraryIndex, collect_release
    from promo_incremental import PlanningCache
    report_path = run_dir / f"{platform}-plan.json"
    report = {"platform": platform, "started": now(), "state": "running", "releases": []}
    save(report_path, report)
    adapter = adapter_for(platform, store)
    metadata_cache = PlanningCache(store, adapter, platform)
    try:
        report["catalogue"] = compact(refresh_catalogue(adapter, platform, age_days))
    except Exception as error:
        # A catalogue failure never hides the mailed offers; they are still planned below.
        report["catalogue_error"] = type(error).__name__ + ": " + safe_error(error)
    if getattr(adapter, "collection_enabled", True) is False:
        report["collection_disabled"] = getattr(adapter, "collection_disabled_reason",
                                                "collection is switched off until its single pilot is verified")
    save(report_path, report)
    library, blocker = LibraryIndex(store), None
    try:
        selected = window_sources(store, adapter, platform, age_days)
    except Exception as error:
        report.update(state="failed", finished=now(), source_selection_error=safe_error(error))
        save(report_path, report)
        store.event(None, "update", "platform_failed", platform=platform, error=report["source_selection_error"])
        store.sync_logs()  # one lane of many; the summary phase flushes the full logs once
        raise
    for sid, subject, state in selected:
        entry = {"source_id": sid, "release": subject}
        if state:
            entry["state"] = state
        elif blocker:
            entry.update(state="access_blocked", reason=blocker)
        else:
            report["current"] = sid
            try:
                planning_adapter = adapter
                if hasattr(adapter, "prepare_planning"):
                    entry["metadata"] = adapter.prepare_planning(sid, refresh=refresh_metadata)
                elif metadata_cache.supported:
                    planning_adapter, entry["metadata"] = metadata_cache.adapter(sid, refresh=refresh_metadata)
                elif hasattr(adapter, "snapshot"):
                    adapter.snapshot(sid, refresh=True)
                if planning_adapter is adapter and hasattr(adapter, "prepare_source"):
                    adapter.prepare_source(sid)
                entry.update(state="planned", plan=collect_release(store, planning_adapter, sid, library, dry_run=True))
            except Exception as error:
                entry.update(state=classify(error), reason=safe_error(error))
                if entry["state"] == "exception":
                    store.event(sid, "update", "release_failed", platform=platform, error=entry["reason"])
                if type(error).__name__ in SESSION_ERRORS:
                    blocker = entry["reason"]
        report["releases"].append(entry)
        report.pop("current", None)
        save(report_path, report)
    report.update(state="finished", finished=now(), states=dict(Counter(r["state"] for r in report["releases"])))
    save(report_path, report)
    store.sync_logs()  # one lane of many; the summary phase flushes the full logs once
    print(json.dumps({"platform": platform, "phase": "discover", "states": report["states"]}, ensure_ascii=False), flush=True)
    return report["states"]


def plan_globally(run_dir, platforms=PLATFORMS):
    """When two sources offer the same mix this run, collect only the best copy: lossless first, then the
    platform order in promo_adapters. The others are deferred; reconcile ignores deferrals, so a failed winner
    is fetched from the next source in the same run."""
    from promo_pipeline import format_rank, loose_key, mix_key
    offers = {}
    for priority, platform in enumerate(platforms):
        report = load(run_dir / f"{platform}-plan.json")
        if not report or report.get("collection_disabled"):
            continue
        for release in report["releases"]:
            if release["state"] != "planned":
                continue
            for track in release["plan"].get("tracks", []):
                if track.get("action") not in {"download", "replace"}:
                    continue
                key = loose_key(track["track"]) or mix_key(track["track"])
                offers.setdefault(key, []).append({
                    "platform": platform, "source_id": release["source_id"], "track": track["track"],
                    "format": track.get("format") or "", "rank": format_rank(track.get("format") or ""),
                    "priority": priority})
    defer, choices = {}, []
    for group in offers.values():
        if len(group) < 2:
            continue
        best = max(group, key=lambda o: (o["rank"], -o["priority"]))
        reason = f"the same mix is collected from {best['platform']} ({best['format']}) this run"
        for offer in group:
            if offer is not best:
                defer.setdefault(offer["platform"], {}).setdefault(offer["source_id"], {})[offer["track"]] = reason
        choices.append({"track": best["track"], "chosen": f"{best['platform']} ({best['format']})",
                        "deferred": [f"{o['platform']} ({o['format']})" for o in group if o is not best]})
    result = {"finished": now(), "defer": defer, "choices": choices}
    save(run_dir / "global-plan.json", result)
    return {"duplicate_offers": len(choices), "deferred": sum(len(t) for s in defer.values() for t in s.values())}


def collect_pass(store, platform, run_dir, reconcile=False):
    """Collect what the dry run planned. Reconcile repeats it after the library refresh, retries failures and
    ignores deferrals; anything already delivered is then a plain skip."""
    from promo_pipeline import LibraryIndex, collect_release
    from promo_incremental import PlanningCache
    planned = load(run_dir / f"{platform}-plan.json", {"releases": []})
    previous_collection = load(run_dir / f"{platform}.json", {}) if reconcile else {}
    failed_sources = {r["source_id"] for r in previous_collection.get("releases", []) if r["state"] == "exception"}
    run_defer = load(run_dir / "global-plan.json", {}).get("defer", {}).get(platform, {})
    defer = {} if reconcile else run_defer
    # 2026-10-24: reconcile reopened all 247 PromoBox pages after collection had settled 226 of them, 21 minutes
    # for nothing. It now revisits only releases collection did not settle, and those with a deferred mix whose
    # chosen source may have failed.
    settled = {r["source_id"] for r in previous_collection.get("releases", [])
               if r["source_id"] not in run_defer and (r["state"] == "nothing_to_collect"
                                                       or r["state"] == "finished" and fully_delivered(r.get("result", {})))}
    report_path = run_dir / (platform + ("-reconcile" if reconcile else "") + ".json")
    report = {"platform": platform, "started": now(), "state": "running", "releases": []}
    save(report_path, report)
    adapter = adapter_for(platform, store)
    metadata_cache = PlanningCache(store, adapter, platform)
    if reconcile:
        from promo_review import review
        report["staged_review"] = review(store, platform)["counts"]
    if planned.get("collection_disabled"):
        report.update(state="finished", finished=now(), collection_disabled=planned["collection_disabled"])
        save(report_path, report)
        return {"platform": platform, "state": "collection_disabled"}
    library, blocker = LibraryIndex(store), None
    retry = {"planned", "exception"} if reconcile else {"planned"}
    for release in planned["releases"]:
        if release["state"] not in retry or (reconcile and release["source_id"] in settled):
            continue
        sid = release["source_id"]
        current = {"source_id": sid, "release": release["release"], "started": now()}
        plan = release.get("plan", {})
        work = any(t.get("action") in WORK_ACTIONS for t in plan.get("tracks", []))
        if blocker:
            current.update(state="access_blocked", reason=blocker)
        elif release["state"] == "planned" and not work and not reconcile:
            # Nothing new, so no feedback, no transfer and no second metadata request.
            if hasattr(adapter, "cleanup_source"):
                adapter.cleanup_source(sid, plan)
            current.update(state="nothing_to_collect", result=plan)
        else:
            report["current"] = current
            save(report_path, report)
            try:
                local_plan = None
                retry_failed = reconcile and (sid in failed_sources or release["state"] == "exception")
                if hasattr(adapter, "prepare_planning"):
                    current["metadata"] = adapter.prepare_planning(sid, refresh=retry_failed,
                                                                   inspected_since=planned.get("started"))
                    local_plan = collect_release(store, adapter, sid, library, dry_run=True, defer=defer.get(sid))
                elif metadata_cache.supported:
                    planning_adapter, current["metadata"] = metadata_cache.adapter(sid, refresh=retry_failed,
                                                                                  inspected_since=planned.get("started"))
                    local_plan = collect_release(store, planning_adapter, sid, library, dry_run=True, defer=defer.get(sid))
                if local_plan is not None and not any(t.get("action") in WORK_ACTIONS for t in local_plan.get("tracks", [])):
                    # Re-evaluate every mix against fresh library/asset state, without reopening an idle release.
                    result = local_plan
                    current["state"] = "nothing_to_collect"
                else:
                    # Fresh metadata only for actual work: download links may expire while other sources are planned.
                    just_checked = (current.get("metadata", {}).get("source") == "fresh"
                                    or current.get("metadata", {}).get("manifest_checked", False))
                    if metadata_cache.supported and not hasattr(adapter, "prepare_planning"):
                        metadata_cache.refresh_for_collection(sid, already_fresh=just_checked)
                    else:
                        if hasattr(adapter, "snapshot") and not just_checked:
                            adapter.snapshot(sid, refresh=True)
                        if hasattr(adapter, "prepare_source"):
                            adapter.prepare_source(sid)
                    result = collect_release(store, adapter, sid, library, allow_gate=True, defer=defer.get(sid))
                    current["state"] = "finished"
                if hasattr(adapter, "cleanup_source"):
                    adapter.cleanup_source(sid, result)
                current["result"] = result
            except Exception as error:
                current.update(state=classify(error), reason=safe_error(error))
                if current["state"] == "exception":
                    store.event(sid, "update", "release_failed", platform=platform, error=current["reason"])
                if type(error).__name__ in SESSION_ERRORS:
                    blocker = current["reason"]
        current["finished"] = now()
        report["releases"].append(current)
        report.pop("current", None)
        save(report_path, report)
        print(json.dumps({"platform": platform, "source_id": sid, "state": current["state"],
                          "releases_checked": len(report["releases"])}, ensure_ascii=False), flush=True)
    finished = [r for r in report["releases"] if r["state"] == "finished"]
    settled = [r for r in report["releases"] if r["state"] in {"finished", "nothing_to_collect"}]
    actions = Counter(t.get("action", "delivered" if t.get("delivered") else "other")
                      for r in finished for t in r["result"].get("tracks", []))
    report.update(state="finished", finished=now(), actions=dict(actions),
                  exceptions=sum(r["state"] == "exception" for r in report["releases"]))
    report["upgrade_candidates_ask_user"] = sorted({t["track"] for r in settled
        for t in r["result"].get("tracks", []) if t.get("action") == "upgrade_candidate"})
    save(report_path, report)
    store.sync_logs()  # one lane of many; the summary phase flushes the full logs once
    return {k: report[k] for k in ("platform", "state", "actions", "exceptions")}


def fully_delivered(result):
    """True when every planned transfer of a collected release ended as a delivered or held file."""
    tracks = result.get("tracks", [])
    if any(t.get("action") in {"claim_denied", "deferred"} for t in tracks):
        return False
    work = sum(t.get("action") in WORK_ACTIONS for t in tracks)
    done = sum(bool(t.get("delivered")) or t.get("action") == "held_for_research" for t in tracks)
    return done >= work


def housekeeping(store, run_dir, keep_runs=3, source_cache_days=60):
    """Leave nothing behind on this PC: every output lives in Dropbox. Removes older run folders, transfer
    leftovers and planning caches of promos far outside the offer window. Returns what it removed."""
    import shutil
    root = Path(store.root)
    removed = {"runs": 0, "files": 0, "bytes": 0}

    def drop(path):
        if path.is_dir():
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            shutil.rmtree(path, ignore_errors=True)
        else:
            size = path.stat().st_size
            os.remove(path)
        removed["files"] += 1
        removed["bytes"] += size

    runs = sorted((p for p in (root / "runs").glob("*") if p.is_dir() and p != Path(run_dir)), reverse=True)
    for old in runs[keep_runs - 1:]:
        try:
            drop(old)
            removed["runs"] += 1
        except OSError:
            pass
    leftovers = [p for p in root.rglob("*.partial")] + list((root / "final-audit-audio").glob("*"))
    leftovers += [p for p in (root / "staging").glob("*")
                  if time.time() - p.stat().st_mtime > 86400] if (root / "staging").exists() else []
    # Planning caches are named <platform>-<source id>.dpapi. Only those of known promos whose newest mail is far
    # past the offer window go; sessions, catalogues and feedback receipts never match this pattern.
    newest = {}
    for sid, received in store.db.execute("SELECT source_id, received FROM messages"):
        try:
            newest[sid] = max(newest.get(sid, 0), email.utils.parsedate_to_datetime(received).timestamp())
        except (TypeError, ValueError):
            newest[sid] = time.time()  # unreadable date: keep
    cutoff = time.time() - source_cache_days * 86400
    for path in root.glob("*.dpapi"):
        match = re.fullmatch(r"([a-z][a-z.-]*?)-([0-9a-f]{20})\.dpapi", path.name)
        if match and "feedback" not in match.group(1) and newest.get(match.group(2), time.time()) < cutoff:
            leftovers.append(path)
    for path in leftovers:
        try:
            drop(path)
        except OSError:
            pass
    store.event(None, "housekeeping", "cleaned", **removed)
    return removed


def final_states(run_dir, platforms=PLATFORMS):
    """Latest state per release over discovery, collection and reconcile, so a recovered failure is not reported."""
    states, notes = {}, {"catalogue_errors": [], "collection_disabled": [], "upgrades": set()}
    for platform in platforms:
        for suffix in ("-plan", "", "-reconcile"):
            report = load(run_dir / (platform + suffix + ".json"))
            if not report:
                continue
            if report.get("catalogue_error"):
                notes["catalogue_errors"].append(platform + ": " + report["catalogue_error"])
            if suffix == "-plan" and report.get("collection_disabled"):
                notes["collection_disabled"].append(platform + ": " + report["collection_disabled"])
            notes["upgrades"].update(report.get("upgrade_candidates_ask_user", []))
            for release in report.get("releases", []):
                states[(platform, release.get("source_id") or release.get("release"))] =(release["state"], release.get("release") or "")
    return states, notes


def summary(store, run_dir, platforms=PLATFORMS):
    """Plain-text end report: what arrived, what failed or was blocked, and what to ask the user."""
    manifest = load(run_dir / "run.json")
    delivered = [json.loads(d).get("dropbox_path") for (d,) in store.db.execute(
        "SELECT details FROM events WHERE action='delivery' AND status='delivery_verified' AND utc>=?", (manifest["started"],))]
    states, notes = final_states(run_dir, platforms)
    pick = lambda wanted: sorted({f"{p}: {name}" for (p, _), (state, name) in states.items() if state == wanted})
    exceptions, unavailable = pick("exception"), pick("unavailable")
    blocked = Counter(p for (p, _), (state, _) in states.items() if state in {"access_blocked", "blocked", "consent_required"})
    audit = load(run_dir / "final-audit.json", {})
    leftovers = load(STATE / "research-leftovers.json", [])
    choices = load(run_dir / "global-plan.json", {}).get("choices", [])
    upgrades = sorted(notes["upgrades"])
    result = {"delivered": len({p for p in delivered if p}), "audit": audit.get("counts", {}),
              "ready_to_merge": audit.get("ready_to_merge", False), "exceptions": exceptions,
              "unavailable_releases": unavailable, "blocked_by_platform": dict(blocked),
              "catalogue_errors": notes["catalogue_errors"], "collection_disabled": notes["collection_disabled"],
              "best_copy_choices": choices, "upgrade_candidates": upgrades, "questions": [
                  {"file": r["name"], "state": r["state"], "evidence": r.get("evidence", [])} for r in leftovers]}
    # A worker can fail before it reports any release. Do not let an empty plan
    # turn that platform-wide coverage gap into an apparently clean summary.
    phase_failures = [{"phase": step.get("phase"), "platform": step.get("platform"),
                       "exit_code": step.get("exit_code")} for step in manifest.get("phases", [])
                      if step.get("state") == "failed"]
    result["phase_failures"] = phase_failures
    lines = [f"{result['delivered']} new files delivered to {manifest['destination']}.",
             "Final audit: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in result["audit"].items()) + "."]
    if manifest.get("continues_pass"):
        lines.append(f"This run continued the run started {manifest['pass_started']}, so lossless copies could replace its lossy ones.")
    if choices:
        lines.append(f"{len(choices)} mixes were offered by more than one source; only the best copy was collected.")
    if phase_failures:
        lines.append("Run phases that failed: " + "; ".join(
            (step.get("platform") + " " if step.get("platform") else "") + str(step.get("phase"))
            for step in phase_failures) + ". Source coverage is incomplete; see the phase logs.")
    mail = load(run_dir / "prepare.json", {}).get("mail", {})
    result["platforms_without_adapter"] = mail.get("platforms_without_adapter", {})
    result["unrecognised_promo_mails"] = mail.get("unrecognised_promo_mails", [])
    if result["platforms_without_adapter"]:
        lines.append("Sources needing an adapter: " + "; ".join(
            f"{p} ({v['mails']} messages)" for p, v in result["platforms_without_adapter"].items()) + ".")
    unknown_hosts = sorted({h for m in result["unrecognised_promo_mails"]
                            for h in m.get("hosts", []) + m.get("known_platform_unrouted", [])})
    tracker_only = sum(bool(m.get("tracker_only")) for m in result["unrecognised_promo_mails"])
    if unknown_hosts:
        lines.append("Promo links needing investigation: " + "; ".join(unknown_hosts) + ".")
    if tracker_only:
        lines.append(f"{tracker_only} promo messages contain only tracking or newsletter links; their destinations were not opened.")
    if notes["catalogue_errors"]:
        lines.append("Account catalogues that could not be read, mailed offers still checked: " + "; ".join(notes["catalogue_errors"]) + ".")
    if notes["collection_disabled"]:
        lines.append("Planned only, collection switched off: " + "; ".join(notes["collection_disabled"]) + ".")
    if blocked:
        lines.append("Promos blocked by login, consent or a feedback gate: " + "; ".join(f"{p} {n}" for p, n in sorted(blocked.items())) + ".")
    if exceptions:
        lines.append(f"{len(exceptions)} promos failed and are retried next run: " + "; ".join(exceptions) + ".")
    if unavailable:
        lines.append(f"{len(unavailable)} promos were explicitly unavailable on the platform: " + "; ".join(unavailable) + ".")
    if upgrades:
        lines.append("Lossless upgrades offered for lossy library copies: " + "; ".join(upgrades) + ".")
    if leftovers:
        lines.append("Labels that promo pages, emails, Beatport and web research could not settle: "
                     + "; ".join(r["name"] for r in leftovers) + ".")
    result["text"] = "\n".join(lines)
    save(run_dir / "summary.json", result)
    return result


def phase(args):
    store = Store()
    try:
        if args.phase == "prepare":
            from promo_mail import discover
            from promo_pipeline import LibraryIndex
            inventory = index_library(store)
            LibraryIndex(store)  # saves keys for new library files once, before parallel lanes need them
            mail = discover(store, f"{args.max_age_days}d", list(PLATFORMS), folders=("INBOX", "Archive"))
            result = {"library": inventory, "mail": mail, "finished": now()}
            save(args.run_dir / "prepare.json", result)
        elif args.phase == "discover":
            result = discover_pass(store, args.platform, args.run_dir, args.max_age_days, args.refresh_metadata)
        elif args.phase in {"collect", "reconcile"}:
            result = collect_pass(store, args.platform, args.run_dir, reconcile=args.phase == "reconcile")
        elif args.phase == "refresh-library":
            from promo_pipeline import LibraryIndex
            result = index_library(store)
            LibraryIndex(store)
        elif args.phase == "audit":
            from promo_final_audit import audit
            result = audit(store, args.run_dir)
        elif args.phase == "research":
            from promo_research import run as research
            result = research(store, args.run_dir, deliver=True)
        elif args.phase == "summary":
            result = summary(store, args.run_dir, args.platforms)
            try:
                result["housekeeping"] = housekeeping(store, args.run_dir)
            except OSError as error:  # cleanup never costs the summary
                result["housekeeping"] = {"error": safe_error(error)}
            store.sync_logs(summary=True)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    except BaseException:
        failed = True
        raise
    else:
        failed = False
    finally:
        try:
            store.sync_logs(force=True)  # events still waiting for their minute go out when the step ends
        except Exception:
            if not failed:
                raise  # a failed step reports its own error; its events wait safely for the next sync
        finally:
            store.db.close()


def open_pass(store, started, continue_pass):
    """2026-10-24: continue the previous pass when the user has not processed its promos yet, so its lossy copies
    may still be replaced. Otherwise every run is a new pass and earlier runs are never touched."""
    previous = store.pass_started() if continue_pass else None
    return (previous, True) if previous else (store.start_pass(started), False)


def phase_command(args, name, platform=None):
    command = [sys.executable, "-u", str(Path(__file__).resolve()), "--phase", name,
               "--run-dir", str(args.run_dir), "--max-age-days", str(args.max_age_days),
               "--platforms", ",".join(args.platforms)]
    return command + (["--platform", platform] if platform else []) + (["--refresh-metadata"] if getattr(args, "refresh_metadata", False) else [])


def launch(command, log):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)


def run_serial(args, manifest, manifest_path, name):
    step = {"phase": name, "started": now(), "state": "running"}
    manifest["phases"].append(step)
    save(manifest_path, manifest)
    with (args.run_dir / f"{name}.log").open("ab") as log:
        code = launch(phase_command(args, name), log).wait()
    step.update(state="finished" if code == 0 else "failed", exit_code=code, finished=now())
    save(manifest_path, manifest)
    return code


def run_lanes(args, manifest, manifest_path, name, platforms):
    """One process per platform, at most args.lanes at once. A failed platform never stops the others."""
    pending, running = list(platforms), {}
    while pending or running:
        while pending and len(running) < args.lanes:
            platform = pending.pop(0)
            step = {"phase": name, "platform": platform, "started": now(), "state": "running"}
            manifest["phases"].append(step)
            log = (args.run_dir / f"{platform}-{name}.log").open("ab")
            running[platform] = (launch(phase_command(args, name, platform), log), step, log)
            save(manifest_path, manifest)
        time.sleep(1)
        for platform, (process, step, log) in list(running.items()):
            code = process.poll()
            if code is None:
                continue
            log.close()
            step.update(state="finished" if code == 0 else "failed", exit_code=code, finished=now())
            del running[platform]
            save(manifest_path, manifest)
    return [s["platform"] for s in manifest["phases"] if s["phase"] == name and s["state"] == "failed"]


def run(args):
    args.run_dir.mkdir(parents=True, exist_ok=True)
    lock = STATE / "complete-update-owner.json"
    if lock.exists():
        previous = load(lock)
        if process_alive(previous["pid"]):
            raise RuntimeError("A complete update coordinator is already alive")
    save(lock, {"pid": os.getpid(), "started": now(), "run_dir": str(args.run_dir)})
    manifest_path = args.run_dir / "run.json"
    manifest = {"started": now(), "pid": os.getpid(), "max_age_days": args.max_age_days, "lanes": args.lanes,
                "platforms": list(args.platforms), "destination": DEST,
                "state": "running", "phases": []}
    save(manifest_path, manifest)
    store = Store()
    try:
        manifest["pass_started"], manifest["continues_pass"] = open_pass(store, manifest["started"], args.continue_pass)
        save(manifest_path, manifest)
    finally:
        store.db.close()
    try:
        if run_serial(args, manifest, manifest_path, "prepare"):
            raise RuntimeError("Library/mail preparation failed; no further collection is safe")
        if "inflyte" in args.platforms and args.wait_pid:
            # An existing Inflyte worker must finish before another session refreshes its cookie.
            manifest["state"] = "waiting_for_existing_inflyte"
            save(manifest_path, manifest)
            while any(process_alive(pid) for pid in args.wait_pid):
                time.sleep(30)
            manifest["state"] = "running"
        failed = set(run_lanes(args, manifest, manifest_path, "discover", args.platforms))
        store = Store()
        try:
            manifest["global_plan"] = plan_globally(args.run_dir, args.platforms)
        finally:
            store.db.close()
        save(manifest_path, manifest)
        run_lanes(args, manifest, manifest_path, "collect", [p for p in args.platforms if p not in failed])
        if run_serial(args, manifest, manifest_path, "refresh-library"):
            raise RuntimeError("Library refresh failed; reconcile and audit need the current library")
        run_lanes(args, manifest, manifest_path, "reconcile", [p for p in args.platforms if p not in failed])
        # Research fixes held and staged files from evidence; the second audit checks only what changed.
        for name in ("audit", "research", "audit", "summary"):
            run_serial(args, manifest, manifest_path, name)
        manifest["state"] = "finished_review_reports"
    except BaseException as error:
        manifest.update(state="stopped", error=safe_error(error))
        raise
    finally:
        manifest["finished"] = now()
        save(manifest_path, manifest)
        if lock.exists() and load(lock).get("pid") == os.getpid():
            lock.unlink()


def platform_list(value):
    names = tuple(p for p in (value or "").split(",") if p) or PLATFORMS
    unknown = sorted(set(names) - set(PLATFORMS))
    if unknown:
        raise argparse.ArgumentTypeError("Unknown platform: " + ", ".join(unknown))
    return tuple(p for p in PLATFORMS if p in names)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "discover", "collect", "reconcile", "refresh-library",
                                            "audit", "research", "summary"))
    parser.add_argument("--platform", choices=PLATFORMS, help="The platform of a discover, collect or reconcile phase")
    parser.add_argument("--platforms", type=platform_list, default=PLATFORMS,
                        help="Comma-separated subset of platforms for this run; default is every adapter")
    parser.add_argument("--lanes", type=int, default=LANES, help="Platforms planned or collected at once (1 to 6)")
    parser.add_argument("--run-dir", type=Path, help="Defaults to a new timestamped folder under runs/")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--wait-pid", type=int, action="append", default=[])
    parser.add_argument("--continue-pass", action="store_true",
                        help="Join the previous run's pass: the user has not processed it, so its lossy copies may be replaced")
    parser.add_argument("--refresh-metadata", action="store_true", help="force fresh release inspection during discovery")
    args = parser.parse_args()
    if args.max_age_days < 1 or args.max_age_days > 30:
        parser.error("This test update is limited to the authorized recent 30-day window")
    if not 1 <= args.lanes <= 6:
        parser.error("--lanes must be between 1 and 6")
    if args.phase in {"discover", "collect", "reconcile"} and not args.platform:
        parser.error(f"--phase {args.phase} needs --platform")
    if args.run_dir is None:
        if args.phase:
            parser.error("--phase needs the --run-dir of its update")
        args.run_dir = STATE / "runs" / time.strftime("%Y%m%d-%H%M%S")
    if args.phase:
        phase(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
