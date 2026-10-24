"""Dropbox-side file work: tags by byte range, the Needs Research folder and its notes, renames by move.

Promos keep no files on this computer. A held file waits in HOLD with a plain-text note beside it,
so a person or an agent can research it from Dropbox alone.
"""
import io
import json
from pathlib import Path, PurePosixPath
import struct

from promo_automation import DEST, HOLD, now

NOTE_SUFFIX = ".txt"


def ranged(client, path, start, end):
    """Bytes start..end inclusive of one Dropbox file, without downloading the rest."""
    client.check_and_refresh_access_token()
    headers = dict(client._headers or {})
    headers.update({"Authorization": "Bearer " + client._oauth2_access_token,
                    "Dropbox-API-Arg": json.dumps({"path": path}), "Range": f"bytes={start}-{end}"})
    response = client._session.post("https://content.dropboxapi.com/2/files/download", headers=headers, timeout=60)
    response.raise_for_status()
    return response.content


def remote_id3(client, path):
    """The ID3 tag of a remote AIFF or MP3, read from a few kilobytes. None when the format needs a full read."""
    from mutagen.id3 import ID3
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in {".aiff", ".aif"}:
        head = ranged(client, path, 0, 11)
        if head[:4] != b"FORM":
            return None
        offset, total = 12, struct.unpack(">I", head[4:8])[0] + 8
        while offset + 8 <= total:
            chunk = ranged(client, path, offset, offset + 7)
            name, size = chunk[:4], struct.unpack(">I", chunk[4:8])[0]
            if name in (b"ID3 ", b"id3 "):
                return ID3(io.BytesIO(ranged(client, path, offset + 8, offset + 7 + size)))
            offset += 8 + size + (size & 1)
        return ID3()
    if suffix == ".mp3":
        head = ranged(client, path, 0, 9)
        if head[:3] != b"ID3":
            return ID3()
        size = 0
        for byte in head[6:10]:
            size = (size << 7) | (byte & 0x7F)
        return ID3(io.BytesIO(ranged(client, path, 0, 9 + size)))
    return None


def remote_tags(client, path):
    from process_promos import id3_fields
    id3 = remote_id3(client, path)
    return None if id3 is None else id3_fields(id3)


def note_path(audio_path):
    return str(PurePosixPath(audio_path).with_suffix(NOTE_SUFFIX))


def write_note(client, audio_path, problems, evidence=()):
    """Plain text a screen reader or a person reads in Dropbox: why this file is not in the test folder yet."""
    import dropbox
    lines = ["Held by the promo update on " + now() + ".", "", "Why it is not merged yet:"]
    lines += ["- " + p for p in dict.fromkeys(problems)] or ["- needs a manual check"]
    if evidence:
        lines += ["", "Research so far:"] + ["- " + e for e in evidence]
    lines += ["", "When it is fixed, rename and retag the audio, then move it up into the promo folder "
              "and delete this note."]
    client.files_upload("\n".join(lines).encode("utf-8"), note_path(audio_path), mode=dropbox.files.WriteMode.overwrite)


def read_note(client, audio_path):
    try:
        _, response = client.files_download(note_path(audio_path))
    except Exception:
        return []
    return [line[2:] for line in response.content.decode("utf-8", "replace").splitlines() if line.startswith("- ")]


def delete_note(client, audio_path):
    try:
        client.files_delete_v2(note_path(audio_path))
    except Exception:
        pass


def move(client, remote, target):
    """Rename or move inside Dropbox without transferring audio; refuses to overwrite."""
    if remote.path_lower == target.lower():
        return remote
    return client.files_move_v2(remote.path_lower, target, autorename=False).metadata


def mark_moved(store, remote_id, path, stage, asset_key=None):
    """Keep the library index and the asset row in step with a move, so duplicates stay detected."""
    with store.db:
        store.db.execute("UPDATE library SET path=? WHERE remote_id=?", (path.lower(), remote_id))
        if asset_key:
            store.db.execute("UPDATE assets SET stage=?,dropbox_path=?,local_path=NULL,updated_at=? WHERE asset_key=?",
                             (stage, path, now(), asset_key))


def hold_remote(store, client, remote, problems, asset_key=None):
    """Move a test-folder file into Needs Research with its note. No local copy is kept."""
    if not remote.path_lower.startswith(DEST.lower() + "/"):
        raise ValueError("Refusing to hold a file outside the test folder")
    moved = move(client, remote, HOLD + "/" + remote.name)
    write_note(client, moved.path_display, problems)
    mark_moved(store, moved.id, moved.path_display, "held_for_research", asset_key)
    return moved


def held_files(client):
    """Audio files currently waiting in Needs Research."""
    import dropbox
    from promo_pipeline import AUDIO_SUFFIXES
    try:
        page = client.files_list_folder(HOLD)
    except dropbox.exceptions.ApiError:
        return []
    entries = list(page.entries)
    while page.has_more:
        page = client.files_list_folder_continue(page.cursor)
        entries += page.entries
    return [e for e in entries if isinstance(e, dropbox.files.FileMetadata) and Path(e.name).suffix.lower() in AUDIO_SUFFIXES]
