"""Archive the previous delivery month before collection, with durable move recovery."""
import json
import re
from datetime import datetime

from dropbox.exceptions import ApiError
from dropbox.files import FileMetadata, FolderMetadata

from promo_automation import AUDIT, DEST, HOLD, SETTINGS, atomic_text, dropbox_skill


def key(path):
    return path.rstrip("/").casefold()


def inside(path, root):
    return key(path) == key(root) or key(path).startswith(key(root) + "/")


def metadata(client, path):
    try:
        return client.files_get_metadata(path)
    except ApiError as error:
        if error.error.is_path() and error.error.get_path().is_not_found():
            return None
        raise


def inventory(client, root):
    """Capture every descendant, not just the folder's own identity."""
    page = client.files_list_folder(root, recursive=True)
    entries = list(page.entries)
    while page.has_more:
        page = client.files_list_folder_continue(page.cursor)
        entries.extend(page.entries)
    result = {}
    for entry in entries:
        if key(entry.path_lower) == key(root):
            continue  # Dropbox may include the shared folder's own metadata.
        if not inside(entry.path_lower, root):
            raise RuntimeError("Dropbox returned an entry outside the requested folder")
        relative = entry.path_lower[len(root):].casefold()
        if isinstance(entry, FileMetadata):
            result[relative] = {"id": entry.id, "size": entry.size, "hash": entry.content_hash}
        elif isinstance(entry, FolderMetadata):
            result[relative] = {"id": entry.id}
    return result


def remap_paths(store, source, target):
    """Update current paths only. Historical events and pass dates remain unchanged."""
    def changed(path):
        return target + path[len(source):] if path and inside(path, source) else path
    with store.db:
        for table, column, identity in (("assets", "dropbox_path", "asset_key"),
                                        ("replacements", "old_path", "asset_key")):
            for row in store.db.execute(f"SELECT {identity},{column} FROM {table}").fetchall():
                if changed(row[1]) != row[1]:
                    store.db.execute(f"UPDATE {table} SET {column}=? WHERE {identity}=?", (changed(row[1]), row[0]))
        for table in ("library", "library_keys"):
            for row in store.db.execute(f"SELECT path FROM {table}").fetchall():
                if changed(row[0]) != row[0]:
                    store.db.execute(f"UPDATE {table} SET path=? WHERE path=?", (changed(row[0]).lower(), row[0]))


def rotate_month(store, current_month=None):
    archive_root = SETTINGS.get("archive_root")
    if not archive_root:
        return {"state": "disabled"}
    for path in (DEST, archive_root, AUDIT, HOLD):
        if not isinstance(path, str) or not path.startswith("/") or path == "/" or any(
                part in {"", ".", ".."} for part in path[1:].split("/")):
            raise ValueError("Archive settings require absolute Dropbox paths below root")
    if inside(archive_root, DEST) or inside(AUDIT, DEST) or inside(HOLD, DEST):
        raise ValueError("Monthly archives and audit/holds must be outside the delivery folder")
    current_month = current_month or datetime.now().astimezone().strftime("%Y-%m")
    state_path = store.root / "monthly-archive.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
        "month": SETTINGS.get("latest_month"), "destination": DEST, "archive_root": archive_root}
    month = state.get("month")
    if any(not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value)
           for value in (month, current_month)):
        raise ValueError("Set latest_month to the YYYY-MM represented by the current delivery folder")
    if key(state.get("destination", "")) != key(DEST) or key(state.get("archive_root", "")) != key(archive_root):
        raise RuntimeError("Archive settings changed; reconcile the saved month state before collecting")
    if current_month < month:
        raise RuntimeError("The current month precedes the delivery month; check the clock and saved state")
    def save():
        atomic_text(state_path, json.dumps(state, ensure_ascii=False, indent=2))
    if month == current_month and not state.get("pending"):
        save()
        return {"state": "current", "month": month}
    shared, client = dropbox_skill()
    if not state.get("pending"):
        if any(inside(row[0], DEST) for row in store.db.execute("SELECT old_path FROM replacements")):
            raise RuntimeError("Finish pending replacements before archiving the delivery folder")
        target = archive_root.rstrip("/") + "/" + month.replace("-", "/")
        if any(inside(target, path) or inside(path, target) for path in (DEST, AUDIT, HOLD)):
            raise ValueError("The monthly target overlaps delivery, audit or holds")
        if metadata(client, target) is not None:
            raise RuntimeError("Monthly archive already exists; no files moved: " + target)
        source = metadata(client, DEST)
        if source is not None and not isinstance(source, FolderMetadata):
            raise RuntimeError("The delivery path is not a folder")
        state["pending"] = {"source": DEST, "target": target, "id": source.id if source else None,
                            "inventory": inventory(client, DEST) if source else {}, "next_month": current_month}
        save()  # Commit before the first remote mutation.
    pending = state["pending"]
    target = pending["target"]
    if pending["source"] != DEST or target != archive_root.rstrip("/") + "/" + month.replace("-", "/"):
        raise RuntimeError("Saved archive operation does not match these settings")
    if pending["id"]:
        archived = metadata(client, target)
        if archived is None:
            source = metadata(client, DEST)
            if source is None or source.id != pending["id"] or inventory(client, DEST) != pending["inventory"]:
                raise RuntimeError("Delivery contents changed after the archive plan; no move retried")
            shared.ensure_dropbox_folder(client, target.rsplit("/", 1)[0])
            client.files_move_v2(DEST, target, autorename=False)
            archived = metadata(client, target)
        if archived is None or archived.id != pending["id"] or inventory(client, target) != pending["inventory"]:
            raise RuntimeError("Monthly archive identity or content verification failed; collection blocked")
    source = metadata(client, DEST)
    if source is not None and (not isinstance(source, FolderMetadata) or inventory(client, DEST)):
        raise RuntimeError("Delivery folder is occupied during archive recovery; collection blocked")
    shared.ensure_dropbox_folder(client, DEST)
    remap_paths(store, DEST, target)
    state.update(month=pending["next_month"], last_archive=target)
    del state["pending"]
    save()
    # If a failed rotation is resumed in a later month, the fresh folder is still empty.
    if state["month"] != current_month:
        state["month"] = current_month
        save()
    result = {"state": "archived", "from_month": month, "month": current_month,
              "path": target, "files": sum("hash" in row for row in pending["inventory"].values())}
    store.event(None, "monthly_archive", "verified", **result)
    return result
