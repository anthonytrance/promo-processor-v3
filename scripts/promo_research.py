"""Evidence-first metadata research and repair for held test files. Asking the user is the last step.

Evidence order: announcement email (Label, catalog, ISRC, per-track label lines), platform metadata,
file tags, label_codes.csv catalog prefixes, Beatport (ISRC, then artist and title). Whatever stays
unresolved is written to research-leftovers.json for agent web research, and only then asked.
"""
import argparse
from collections import Counter
import email
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
import unicodedata

from promo_automation import ACCOUNT, DEST, HOLD, STATE, Store, atomic_text, dropbox_skill, email_skill, normalized, now
from promo_metadata import SELF_RELEASED, distributor_only, find_resolution, label_issues
from promo_pipeline import _ARTIST_SPLIT, excluded, load_exclusions, mix_key, validate_audio
from process_promos import (build_target_filename, clean_master_suffixes, load_label_codes, normalize_ft_to_feat,
                           normalize_mix_type, read_existing_tags, write_tags)

CODES = Path(__file__).with_name("label_codes.csv")
_MIX_WORDS = r"(?:Mix|Remix|Edit|Version|Dub|Rework|Bootleg|Mashup|Cut|VIP|Instrumental|Vocal)"
_MIX = re.compile(r"^(.*?)\s*\(([^()]*\b" + _MIX_WORDS + r"\b[^()]*)\)\s*$", re.I)
_ISRC = re.compile(r"\b([A-Z]{2})-?([A-Z0-9]{3})-?(\d{2})-?(\d{5})\b")


def trusted(label):
    return bool(label) and not label_issues(label)


def clean_label(label):
    """Library spelling drops company suffixes: 'Lost Kommunication Records Ltd' is filed without 'Ltd'."""
    return re.sub(r",?\s+(?:Ltd\.?|Limited|LLC|Inc\.?|GmbH|B\.?V\.?)$", "", label.strip(), flags=re.I)


def split_title(title):
    """('Song', 'Extended Mix') from 'Song (Extended Mix)'; no mix gives ('Song', None)."""
    match = _MIX.match(title or "")
    return (match.group(1).strip(), match.group(2).strip()) if match else ((title or "").strip(), None)


def artist_set(artist):
    return {normalized(a) for a in _ARTIST_SPLIT.split(artist or "") if normalized(a)}


def message_text(message):
    from bs4 import BeautifulSoup
    html_text = plain = ""
    for part in message.walk():
        if part.get_content_disposition() == "attachment":
            continue
        body = part.get_payload(decode=True)
        if body is None:
            continue
        body = body.decode(part.get_content_charset() or "utf-8", "replace")
        if part.get_content_type() == "text/html" and not html_text:
            html_text = BeautifulSoup(body, "html.parser").get_text("\n")
        elif part.get_content_type() == "text/plain" and not plain:
            plain = body
    text = html_text or plain
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def subject(message):
    from email.header import decode_header, make_header
    return " ".join(str(make_header(decode_header(message.get("Subject") or ""))).split())


def parse_mail(text):
    """Every label statement an announcement makes, keeping the line it came from."""
    found = {"labels": [], "catalogs": [], "isrcs": [], "track_labels": []}
    for line in text.splitlines():
        # 'Artist - Title [Label]' subjects name the imprint; a bracket with digits is a catalog number.
        match = re.match(r"^Subject:.* - .*\[([^\]\d]+)\]\s*$", line)
        if match:
            found["labels"].append(match.group(1).strip())
        match = re.match(r"^(?:Record\s+)?(?:Label|Imprint)\s*[:\-]\s*(.+)$", line, re.I)
        if match:
            found["labels"].append(match.group(1).strip())
        match = re.search(r"\b(?:Released|Out(?:\s+now)?)\s+(?:through|via|on)\s+([A-Z0-9][^\n.,!(]{1,50}?)(?:\s+on\s+\w+|\s*[.,!(]|$)", line)
        if match and not re.match(r"(?:Beatport|Spotify|all|\d)", match.group(1), re.I):
            found["labels"].append(match.group(1).strip())
        match = re.search(r"\bCat(?:alog(?:ue)?)?\.?\s*(?:No\.?|Num(?:ber)?\.?|#)?\s*[:\-]\s*([A-Z][A-Z0-9]*[\s\-]?\d{2,6})\b", line, re.I)
        if match:
            found["catalogs"].append(re.sub(r"[\s\-]", "", match.group(1)).upper())
        found["isrcs"] += ["".join(m) for m in _ISRC.findall(line)]
        # 'CAT001 Artist - Title (Mix)(Label)': the last parenthetical is a label when it names no mix.
        match = re.match(r"^(?:[A-Z]{2,}\d+\s+)?(.+? - .+?)\s*\(([^()]+)\)\s*$", line)
        if match and not re.search(r"\b" + _MIX_WORDS + r"\b", match.group(2), re.I):
            found["track_labels"].append((match.group(1).strip(), match.group(2).strip()))
    for key in ("labels", "catalogs", "isrcs"):
        found[key] = list(dict.fromkeys(found[key]))
    return found


class Mail:
    """Read-only access to the announcement mail of a source; never marks mail read."""

    def __init__(self, store):
        self.store, self.conn, self.selected, self.cache = store, None, None, {}

    def texts(self, source_id):
        if source_id in self.cache:
            return self.cache[source_id]
        rows = self.store.db.execute("SELECT folder,uid,message_id FROM messages WHERE source_id=? ORDER BY received DESC",
                                     (source_id,)).fetchall()
        result = []
        for row in rows[:3]:
            if self.conn is None:
                self.skill = email_skill()
                self.conn = self.skill.imap_login(ACCOUNT)
            if self.selected != row["folder"]:
                self.skill._select(self.conn, row["folder"], readonly=True)
                self.selected = row["folder"]
            _, data = self.conn.uid("fetch", str(row["uid"]).encode(), "(BODY.PEEK[])")
            for item in data:
                if isinstance(item, tuple):
                    message = email.message_from_bytes(item[1])
                    result.append(("mail:" + (row["message_id"] or message.get("Message-ID") or row["uid"]).strip("<>"),
                                   "Subject: " + subject(message) + "\n" + message_text(message)))
        self.cache[source_id] = result
        return result

    def close(self):
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass


def beatport_search(query):
    from curl_cffi import requests
    for attempt in range(3):  # Beatport search times out now and then; one miss should not leave a track unresolved.
        try:
            response = requests.get("https://www.beatport.com/search/tracks", params={"q": query},
                                    impersonate="chrome124", timeout=30)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', response.text, re.S)
    if response.status_code != 200 or not match:
        return []

    def find(node):
        if isinstance(node, dict):
            value = node.get("data")
            if isinstance(value, list) and value and isinstance(value[0], dict) and "track_id" in value[0]:
                return value
            node = list(node.values())
        if isinstance(node, list):
            for child in node:
                hit = find(child)
                if hit:
                    return hit
        return None
    return find(json.loads(match.group(1))) or []


_GENERIC_MIX = {"original", "extended", "radio", "club", "intro", "outro", "dub", "instrumental", "vocal",
                "mix", "edit", "version", "remix", "rework", "piano", "orchestral", "short", "long"}


def compatible_mix(ours, theirs):
    """Generic versions share a release; a named remix must share its remixer."""
    words = lambda m: set(normalized(m or "").split()) - _GENERIC_MIX
    mine, other = words(ours), words(theirs)
    return mine & other if mine else not other


_NAMED_MIX = re.compile(r"\s*\(([^()]*\b(?:mix|remix|edit|version|rework|dub)\b[^()]*)\)\s*$", re.I)


def beatport_match(artist, title, isrc=None, search=beatport_search):
    """The Beatport track with the same artists, title and mix. ISRC alone is proof of identity."""
    base, mix = split_title(title)
    artists = artist_set(artist)
    queries = ([isrc] if isrc else []) + [f"{artist} {base}"]
    for query in queries:
        best = None
        for track in search(query):
            label = (track.get("label") or {}).get("label_name") if isinstance(track.get("label"), dict) else None
            names = artist_set(", ".join(a.get("artist_name", "") for a in track.get("artists") or []))
            if not label:
                continue
            same_isrc = isrc and (track.get("isrc") or "").replace("-", "").upper() == isrc
            # Beatport keeps the featured artist in the title: "So Good (feat. Kuuda)".
            track_base = re.sub(r"\s*\((?:feat|ft)\.?\s[^()]*\)", "", track.get("track_name") or "", flags=re.I)
            # Some distributor uploads put the version in the name too: "Traka Taka (Extended Remix)".
            beatport_mix = track.get("mix_name") or ""
            embedded = _NAMED_MIX.search(track_base)
            if embedded:
                track_base = track_base[:embedded.start()]
                if normalized(beatport_mix) in {"", "original mix", "original"}:
                    beatport_mix = embedded.group(1)
            same_song = normalized(track_base) == normalized(base) and artists & names
            if not (same_isrc or same_song):
                continue
            same_mix = normalized(beatport_mix) == normalized(mix or "Original Mix")
            if not (same_isrc or same_mix or compatible_mix(mix, beatport_mix)):
                continue  # A remix is often released on another label than the original.
            slug = re.sub(r"[^a-z0-9]+", "-", (track.get("track_name") or "").casefold()).strip("-") or "track"
            hit = {"label": label.strip(), "catalog": track.get("catalog_number"), "isrc": track.get("isrc"),
                   "track_name": track.get("track_name"), "mix_name": track.get("mix_name"),
                   "artists": [a.get("artist_name") for a in track.get("artists") or []],
                   "url": f"https://www.beatport.com/track/{slug}/{track['track_id']}",
                   "evidence": "same ISRC" if same_isrc else "same artists and title" + (" and mix" if same_mix else "")}
            if same_isrc or same_mix:
                return hit
            best = best or hit
        if best:
            return best
    return None


def catalog_label(catalogs, codes=None):
    codes = load_label_codes(str(CODES)) if codes is None else codes
    for catalog in catalogs:
        for prefix, label in codes.items():
            if re.fullmatch(re.escape(prefix) + r"\d+", catalog, re.I):
                return label, catalog
    return None, None


def mail_label(found, artist, title):
    """A per-track label line for this recording wins over one release-wide Label line."""
    base, _ = split_title(title)
    for line, label in found["track_labels"]:
        line_artist, _, line_title = line.partition(" - ")
        if normalized(base) and normalized(base) in normalized(line_title) and artist_set(line_artist) & artist_set(artist):
            return label, "per-track label line: " + line + " (" + label + ")"
    labels = {normalized(l): l for l in found["labels"] if trusted(l)}
    if len(labels) == 1:
        return next(iter(labels.values())), "announcement Label line"
    return None, None


def research(item, mail=None, search=beatport_search, codes=None):
    """Return (resolution or None, evidence). A resolution needs independent public or first-party proof."""
    artist, title = item["artist"], item["title"]
    evidence, sources = [], []
    found = {"labels": [], "catalogs": [], "isrcs": [], "track_labels": []}
    first_party = None
    for source, text in (mail.texts(item["source_id"]) if mail and item.get("source_id") else []):
        parsed = parse_mail(text)
        for key in found:
            found[key] += [v for v in parsed[key] if v not in found[key]]
        label, why = mail_label(parsed, artist, title)
        if label and not first_party:
            first_party = (label, source, why)
    if first_party:
        evidence.append({"source": first_party[1], "label": first_party[0], "why": first_party[2]})
    for label in [item.get("platform_label"), item.get("tag_label")]:
        if trusted(label):
            evidence.append({"source": "platform/tag", "label": label})
    isrc = item.get("isrc") or (found["isrcs"][0] if len(found["isrcs"]) == 1 else None)
    code_label, catalog = catalog_label(found["catalogs"] + [c for c in [item.get("catalog")] if c], codes)
    if code_label:
        evidence.append({"source": "label_codes.csv", "label": code_label, "catalog": catalog})
    beatport = None
    try:
        beatport = beatport_match(artist, title, isrc, search)
    except Exception as error:
        evidence.append({"source": "beatport", "error": type(error).__name__})
    self_released = beatport and distributor_only(beatport["label"])
    if beatport:
        evidence.append({"source": beatport["url"], "label": None if self_released else clean_label(beatport["label"]),
                         "distributor": beatport["label"] if self_released else None,
                         "catalog": beatport["catalog"], "why": "Beatport " + beatport["evidence"]})
    labels = [e for e in evidence if e.get("label")]
    if not labels and self_released:
        # Only a distributor on the store and no imprint anywhere else: the library files it as Self Released.
        labels = [{"source": beatport["url"], "label": SELF_RELEASED}]
        evidence.append(labels[0])
        beatport = None
    if not labels:
        return None, {"state": "unresolved", "evidence": evidence, "catalogs": found["catalogs"], "isrcs": found["isrcs"]}
    distinct = {normalized(re.sub(r"\b(?:ltd|llc|inc|gmbh|bv)\b\.?", "", e["label"], flags=re.I)) for e in labels}

    def agrees(a, b):
        # 'HIGH VOLTAGE' and 'High Voltage Recordings' agree; 'MixCult Records' and 'MixCult Digital' do not.
        a, b = normalized(a), normalized(b)
        return a in b or b in a
    if beatport and not self_released:
        # 2026-10-23: the Beatport label overrules every other source. The catalogue spelling
        # is kept only when it names the same label, so library folders stay consistent.
        store_label = clean_label(beatport["label"])
        chosen = clean_label(code_label) if code_label and agrees(code_label, store_label) else store_label
        overruled = sorted({e["label"] for e in labels if not agrees(e["label"], chosen)})
        if overruled:
            evidence.append({"source": "rule", "why": "Beatport label overrules " + ", ".join(overruled)})
    else:
        # The catalogue spelling keeps library folders consistent; otherwise the sender's own line.
        chosen = clean_label(code_label or labels[0]["label"])
        if not all(agrees(name, chosen) for name in distinct):
            return None, {"state": "conflict", "evidence": evidence}
    sources = [e["source"] for e in labels if e["source"].startswith(("https://", "mail:"))]
    if not sources:
        sources = ["https://www.beatport.com"] if beatport else []
    if not sources and code_label:
        sources = ["catalog:" + catalog]
    resolution = {"artist": artist, "title": title, "label": chosen, "original_names": item.get("original_names", []),
                  "catalog": catalog or (beatport or {}).get("catalog"), "approved_at": now(),
                  "approved_by": "promo_research evidence rule", "sources": sources, "evidence": evidence}
    return resolution, {"state": "resolved", "evidence": evidence}


def save_resolutions(new, root=STATE):
    path = Path(root) / "metadata-resolutions.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"resolutions": []}
    keys = {mix_key(e["artist"] + " - " + e["title"]): i for i, e in enumerate(data["resolutions"])}
    for entry in new:
        index = keys.get(mix_key(entry["artist"] + " - " + entry["title"]))
        if index is None:
            keys[mix_key(entry["artist"] + " - " + entry["title"])] = len(data["resolutions"])
            data["resolutions"].append(entry)
        else:
            old = data["resolutions"][index]
            entry["original_names"] = list(dict.fromkeys(old.get("original_names", []) + entry["original_names"]))
            data["resolutions"][index] = entry
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def add_catalog_prefixes(resolutions):
    """Learn new catalog prefixes from Beatport-confirmed resolutions only."""
    from process_promos import save_label_codes
    codes = load_label_codes(str(CODES))
    added = {}
    for entry in resolutions:
        catalog = (entry.get("catalog") or "").upper().replace(" ", "").replace("-", "")
        match = re.fullmatch(r"([A-Z][A-Z0-9]*?[A-Z])\d{2,6}", catalog)
        if not match or not any(e["source"].startswith("https://www.beatport.com/track/") for e in entry["evidence"]):
            continue
        prefix = match.group(1)
        if prefix not in codes and len(prefix) >= 2:
            codes[prefix] = added[prefix] = entry["label"]
    if added:
        save_label_codes(codes, str(CODES))
    return added


def held_items(store, client):
    """Files waiting in Needs Research, read by byte range, each tied to its source when known."""
    from promo_remote import held_files, read_note, remote_tags
    items = []
    assets = store.db.execute("SELECT * FROM assets").fetchall()
    for remote in held_files(client):
        tags = remote_tags(client, remote.path_display) or {}
        asset = next((a for a in assets if (a["dropbox_path"] or "").casefold() == remote.path_display.casefold()), None) \
            or next((a for a in assets if a["title"] and mix_key(a["title"]) == mix_key(remote.name)), None)
        title = normalize_mix_type(clean_master_suffixes(tags.get("title") or ""))
        items.append({"name": remote.name, "remote": remote, "asset_key": asset and asset["asset_key"],
                      "source_id": asset and asset["source_id"], "asset_title": asset and asset["title"],
                      "duration": asset and json.loads(asset["metadata"] or "{}").get("raw_audio", {}).get("duration"),
                      "issues": read_note(client, remote.path_display), "tags": tags,
                      "artist": normalize_ft_to_feat(tags.get("artist") or ""), "title": title,
                      "tag_label": tags.get("grouping") or tags.get("publisher"),
                      "isrc": (tags.get("isrc") or "").replace("-", "").upper() or None, "album": tags.get("album"),
                      "original_names": [n for n in {re.sub(r"\s*\[[^\]]*\]\s*$", "", Path(remote.name).stem),
                                                     asset and asset["title"]} if n]})
    return items


def fix(store, client, item, resolution, deliver=True):
    """Rename by move when the tags already agree; otherwise retag a temporary copy and deliver it. Nothing stays local."""
    from promo_final_audit import filename_and_tag_issues, readback
    from promo_pipeline import deliver_file
    from promo_remote import delete_note, mark_moved, move
    remote = item["remote"]
    info = {"artist": resolution["artist"], "title": resolution["title"], "label": resolution["label"],
            "original_album": item.get("album")}
    name = build_target_filename(info, Path(remote.name).suffix.lower())
    if not deliver:
        return {"state": "would_fix", "file": name}
    if not item.get("asset_key"):
        return {"state": "needs_asset_row", "file": name}
    claim = store.db.execute("SELECT owner FROM claims WHERE asset_key=?", (item["asset_key"],)).fetchone()
    if claim and not store.resume_claim(item["asset_key"]):
        return {"state": "waiting_for_owner", "file": name}
    tags = item["tags"]
    store.event(item["source_id"], "research", "metadata_fixed", track=item["asset_title"] or item["name"],
                label=resolution["label"], sources=resolution["sources"], file=name)
    if (tags.get("artist"), tags.get("title"), tags.get("grouping"), tags.get("publisher")) == \
            (info["artist"], info["title"], info["label"], info["label"]):
        # Only the filename is wrong: a Dropbox move, no transfer. The next audit reads the file back in full.
        moved = move(client, remote, DEST + "/" + name)
        mark_moved(store, moved.id, moved.path_display, "delivery_verified", item["asset_key"])
        delete_note(client, remote.path_display)
        store.event(item["source_id"], "delivery", "delivery_verified", track=item["asset_title"] or item["name"],
                    dropbox_path=moved.path_display, research_fix=True, renamed_only=True)
        return {"state": "renamed", "file": name, "dropbox_path": moved.path_display}
    work = Path(tempfile.mkdtemp(prefix="promo-fix-"))
    try:
        source = readback(store, client, remote, work)
        target = work / name
        before = validate_audio(source)
        source.replace(target)
        write_tags(str(target), info)
        issues, _, _ = filename_and_tag_issues(name, target)
        after = validate_audio(target)
        if (before["bits"], before["sample_rate"], before["channels"]) != (after["bits"], after["sample_rate"], after["channels"]):
            issues.append("Retagging changed the audio format")
        if issues:
            return {"state": "fix_failed", "file": name, "issues": issues}
        path = deliver_file(store, *dropbox_skill(), target, item["asset_key"])
        client.files_delete_v2(remote.path_lower, parent_rev=remote.rev)
        delete_note(client, remote.path_display)
        with store.db:
            store.db.execute("DELETE FROM library WHERE remote_id=?", (remote.id,))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    store.event(item["source_id"], "delivery", "delivery_verified", track=item["asset_title"] or item["name"],
                dropbox_path=path, recovered=True, research_fix=True)
    return {"state": "redelivered", "file": name, "dropbox_path": path}


def shorter_versions(rows):
    """Names of corrected tracks to skip: a radio edit or edit next to the extended or full mix of the same promo."""
    from promo_pipeline import dj_version, select_versions, version_group, version_kind
    skip = set()
    for source in {r["source_id"] for r in rows}:
        tracks = [{"name": r["final_name"], "duration": r.get("duration")} for r in rows if r["source_id"] == source]
        for name, (wanted, why) in select_versions(tracks).items():
            if not wanted and why.startswith("shorter version"):
                skip.add((source, name))
        groups = {}
        for track in tracks:
            groups.setdefault(version_group(track["name"]), []).append(version_kind(track["name"]))
        for track in tracks:
            kinds = groups[version_group(track["name"])]
            if version_kind(track["name"]) == "short" and not dj_version(track) and "extended" not in kinds \
                    and kinds.count("full") == 1:
                skip.add((source, track["name"]))
    return skip


def discard_excluded(store, client, path, asset_key, source_id, track, reason, note=False):
    """Remove a file that is on the user's exclusion list from Dropbox; it is never delivered again."""
    from promo_remote import delete_note
    store.event(source_id, "cleanup", "excluded_removal_intent", track=track, reason=reason, dropbox_path=path)
    store.sync_logs()
    try:
        client.files_delete_v2(path)
    except Exception as error:
        if "not_found" not in str(error):
            raise
    if note:
        delete_note(client, path)
    with store.db:
        store.db.execute("DELETE FROM library WHERE path=?", (path.lower(),))
    if asset_key:
        store.asset_update(asset_key, "excluded_label", dropbox_path=None)
    store.event(source_id, "cleanup", "excluded_removed", track=track, reason=reason)


def purge_excluded(store, client, deliver, rules=None):
    """Apply today's exclusion list to delivered test-folder files, so an edit of the CSV also clears earlier ones.

    The label is the [Label] suffix of the delivered filename or the label saved at download.
    """
    from promo_automation import TEST_FOLDER_IS_ONE_PASS
    rules = load_exclusions() if rules is None else rules
    if not rules or not any(rules.values()):
        return []
    since = None if TEST_FOLDER_IS_ONE_PASS else store.pass_started()
    assets = {(a["dropbox_path"] or "").lower(): a for a in store.db.execute(
        "SELECT * FROM assets WHERE dropbox_path IS NOT NULL").fetchall()}
    removed = []
    for (path,) in store.db.execute("SELECT path FROM library WHERE path LIKE ? AND path NOT LIKE ?",
                                    (DEST.lower() + "/%", HOLD.lower() + "/%")).fetchall():
        name = Path(path).stem
        if "/" in path[len(DEST) + 1:] or Path(path).suffix.lower() in {".txt", ".csv", ".json", ".jsonl"}:
            continue
        asset = assets.get(path)
        if since and (not asset or (asset["updated_at"] or "") < since):
            continue  # an earlier pass: the user may already have taken it
        match = re.search(r"\s*\[([^\]]+)\]$", name)
        label = (json.loads(asset["metadata"] or "{}").get("label") if asset else None) or (match and match.group(1))
        reason = excluded({"name": name[:match.start()] if match else name, "label": label}, {}, rules)
        if not reason:
            continue
        removed.append({"file": Path(path).name, "reason": reason})
        if deliver:
            discard_excluded(store, client, path, asset and asset["asset_key"], asset and asset["source_id"],
                             name, reason)
    return removed


def run(store, run_dir, deliver=False, search=beatport_search, client=None):
    from promo_remote import delete_note, write_note
    client = client or dropbox_skill()[1]
    mail = Mail(store)
    results, new, pending = [], [], []
    try:
        for item in held_items(store, client):
            existing = find_resolution(item["asset_title"] or item["name"], store.root) or \
                find_resolution(item["artist"] + " - " + item["title"], store.root)
            if existing:
                resolution, outcome = existing, {"state": "resolved", "evidence": existing.get("evidence", [])}
            elif trusted(item["tag_label"]) and item["title"].endswith(" [" + item["tag_label"] + "]"):
                # The processor kept the bracketed label inside the title; the label itself is fine.
                resolution = {"artist": item["artist"], "title": item["title"][:-len(item["tag_label"]) - 3],
                              "label": item["tag_label"], "sources": ["tag:" + item["tag_label"]]}
                outcome = {"state": "resolved", "evidence": [{"source": "tag", "why": "label suffix removed from title"}]}
            elif trusted(item["tag_label"]) and not any("label" in i.lower() for i in item["issues"]):
                resolution, outcome = None, {"state": "label_ok_other_issue"}
            else:
                resolution, outcome = research(item, mail, search)
                if resolution:
                    new.append(resolution)
            row = {k: item.get(k) for k in ("name", "asset_key", "source_id", "artist", "title", "tag_label", "issues",
                                            "duration")}
            row.update(outcome)
            final = resolution or item
            row["final_name"] = final["artist"] + " - " + final["title"]
            pending.append((item, resolution, row))
    finally:
        mail.close()
    skip = shorter_versions([row for _, _, row in pending])
    rules = load_exclusions()
    for item, resolution, row in pending:
        reason = excluded({"name": row["final_name"], "label": (resolution or {}).get("label") or item.get("tag_label")},
                          {}, rules)
        if reason:
            # 2026-10-24: research that lands on an excluded label or artist removes the file.
            row.update(state="excluded", reason=reason)
            if deliver:
                discard_excluded(store, client, item["remote"].path_display, item.get("asset_key"), item["source_id"],
                                 row["final_name"], reason, note=True)
        elif (row["source_id"], row["final_name"]) in skip or any(i.startswith("Shorter version") for i in row["issues"] or []):
            row["state"] = "shorter_version_skipped"
            if deliver:
                client.files_delete_v2(item["remote"].path_lower, parent_rev=item["remote"].rev)
                delete_note(client, item["remote"].path_display)
                with store.db:
                    store.db.execute("DELETE FROM library WHERE remote_id=?", (item["remote"].id,))
                if item.get("asset_key"):
                    store.asset_update(item["asset_key"], "not_wanted", dropbox_path=None)
                store.event(item["source_id"], "select", "not_wanted", track=row["final_name"],
                            reason="shorter version; the extended or full mix of the same promo is wanted")
        elif any(i.startswith("Same mix identity") for i in row["issues"] or []):
            # A library copy of this mix already exists; a label fix must not release it back into the test folder.
            row["state"] = "duplicate_review"
        elif resolution:
            row["label"] = resolution["label"]
            row["fix"] = fix(store, client, item, resolution, deliver)
        elif deliver and row["state"] in {"unresolved", "conflict"}:
            # The note tells a person what was already tried, so nobody repeats it.
            tried = [(e.get("source") or "") + (": " + e["label"] if e.get("label") else "")
                     + (" (" + e["why"] + ")" if e.get("why") else "") for e in row.get("evidence", [])]
            write_note(client, item["remote"].path_display, item["issues"] + ["label not settled by research (" + row["state"] + ")"],
                       tried or ["announcement email, platform data, tags, catalogue prefixes and Beatport gave no label"])
        results.append(row)
    if new:
        save_resolutions(new, store.root)
    swept = purge_excluded(store, client, deliver, rules)
    report = {"as_of_utc": now(), "deliver": deliver, "excluded_from_test_folder": swept, "learned_prefixes": add_catalog_prefixes(new) if deliver else {},
              "counts": dict(Counter(r["state"] + ("/" + r["fix"]["state"] if "fix" in r else "") for r in results)),
              "files": results}
    atomic_text(Path(run_dir) / "research.json", json.dumps(report, ensure_ascii=False, indent=2))
    leftovers = [r for r in results if r["state"] in {"unresolved", "conflict"}]
    atomic_text(store.root / "research-leftovers.json", json.dumps(leftovers, ensure_ascii=False, indent=2))
    store.sync_logs(force=True)
    return {k: report[k] for k in ("counts", "learned_prefixes")} | {"leftovers": len(leftovers), "excluded_removed": len(swept)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--deliver", action="store_true", help="Upload fixed files to the test folder")
    args = parser.parse_args()
    store = Store()
    try:
        print(json.dumps(run(store, args.run_dir, args.deliver), ensure_ascii=False, indent=2))
    finally:
        store.db.close()
