"""Platform-independent promo pipeline: duplicate keys, verified download, processing, Dropbox delivery.

A platform adapter supplies releases and file transfers. This module owns everything after that so every
platform shares the same duplicate rules, claims, audit events and delivery verification.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import time
from functools import lru_cache
from pathlib import Path

from promo_automation import (DEST, HOLD, STATE, SETTINGS, TEST_FOLDER_IS_ONE_PASS, Store, dropbox_hash, dropbox_skill, normalized,
                              now, process_alive, upload_slot)
from process_promos import normalize_mix_type
from audio_length import container

SCRIPTS = Path(__file__).resolve().parent
STAGING = STATE / "staging"
AUDIO_SUFFIXES = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".m4a"}
LOSSLESS_SUFFIXES = {".wav", ".aif", ".aiff", ".flac"}
LOSSY_SUFFIXES = AUDIO_SUFFIXES - LOSSLESS_SUFFIXES
LOSSLESS_NAMES = {"wav", "aif", "aiff", "flac", "alac"}
DONE_STAGES = {"delivery_verified", "excluded_existing_library", "held_for_research"}
RESUMABLE_STAGES = {"downloaded", "processed"}
EXCLUSIONS = STATE / "exclusions.json"  # legacy list, read only when no CSV was ever fetched
# 2026-10-24: the master exclusion list is a CSV the user edits in the audit folder. Each process reads it once
# per EXCLUSIONS_TTL seconds and keeps the last good copy locally, so a Dropbox hiccup never lifts an exclusion.
EXCLUSIONS_REMOTE = DEST + "/audit/exclusions.csv"
EXCLUSIONS_CACHE = STATE / "exclusions.csv"
EXCLUSIONS_TTL = 300
_EXCLUSIONS_LOADED = {}
_EXCLUSION_TYPES = {"label": "labels", "labels": "labels", "artist": "artists", "artists": "artists",
                    "title word": "title_words", "title_word": "title_words", "title words": "title_words",
                    "title_words": "title_words", "word": "title_words"}

_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\band\b|\bx\b|\bvs\.?|\bfeat\.?|\bft\.?|\bfeaturing\b|\bpres\.?|\bwith\b)\s*",
                           re.IGNORECASE)
_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# 2026-10-28: a track number left on a Drive or folder file name ('01.Sound Players - ...') is never part of the artist.
# A digit right after the separator keeps names such as '4.0 Artist'; '3CHO' or '0555' have no separator at all.
_TRACK_NUMBER = re.compile(r"^\s*\d{1,3}\s*(?:[._)]|-(?=\s))\s*(?=[^\d\s])")


def strip_track_number(name):
    """'01.Artist - Title' or '02 - Artist - Title' without the number; anything else unchanged."""
    rest = _TRACK_NUMBER.sub("", name or "", count=1)
    return rest if rest != name and " - " in rest else name


def _script_trim(text):
    """'Yukes玉刻' is credited as 'Yukes' elsewhere: a name that mixes Latin and another script keeps the Latin part."""
    letters = [c for c in text if c.isalpha()]
    latin = [c for c in letters if "LATIN" in unicodedata.name(c, "")]
    if not latin or len(latin) == len(letters):
        return text
    return "".join(c for c in text if not c.isalpha() or "LATIN" in unicodedata.name(c, ""))


def artist_names(text, fold=normalized):
    """The order-free set of credited names in an artist field, each in its comparison form."""
    return {fold(_script_trim(a)) for a in _ARTIST_SPLIT.split(text or "") if fold(_script_trim(a))}


def label_is_artist(label, track):
    """True when the label is only a credited artist's name: the promo sender, not proof of an imprint."""
    artist = track.get("artist") or (track.get("name") or "").partition(" - ")[0]
    return bool(label) and normalized(_script_trim(label)) in artist_names(artist)


class GateRequired(RuntimeError):
    """The platform withholds downloads until feedback is recorded."""


class CollectionBlocked(RuntimeError):
    """Metadata can be inspected, but an original-file contract still needs verification."""


@lru_cache(maxsize=1 << 18)
def mix_key(name):
    """Canonical key for one mix: order-insensitive artists plus normalized title and mix.

    Punctuation, a trailing [label], the extension and artist separators or ordering do not change it.
    """
    stem = Path(name).stem if Path(name).suffix.lower() in AUDIO_SUFFIXES else name
    stem = normalize_mix_type(unicodedata.normalize("NFC", stem))
    stem = re.sub(r"\s*\[[^\]]*\]\s*$", "", stem)
    artist, separator, rest = stem.partition(" - ")
    if not separator:
        return normalized(stem)
    artists = sorted(artist_names(artist))
    return ";".join(artists) + "|" + normalized(rest)


@lru_cache(maxsize=1 << 18)
def loose_key(name):
    """mix_key that also matches the library's habit of crediting the remixer as an artist and wording mixes loosely.

    'Darude, ZANS - Sandstorm (ZANS Remix)' and 'Darude - Sandstorm (ZANS Remix)' agree, and so do
    'Sandstorm (Storm 25 Mix) (Extended Version)' and 'Sandstorm (Storm 25 Extended)': an artist named inside the
    brackets is dropped (one artist always stays), and the filler words Mix and Version are ignored.
    """
    stem = Path(name).stem if Path(name).suffix.lower() in AUDIO_SUFFIXES else name
    stem = normalize_mix_type(unicodedata.normalize("NFC", stem))
    stem = re.sub(r"\s*\[[^\]]*\]\s*$", "", stem)
    artist, separator, rest = stem.partition(" - ")
    if not separator:
        return None
    credits = " " + " ".join(normalized(c) for c in re.findall(r"\(([^()]*)\)", rest)) + " "
    artists = sorted(artist_names(artist))
    kept = [a for a in artists if " " + a + " " not in credits] or artists
    words = [w for w in normalized(rest).split() if w not in {"mix", "version"}]
    return ";".join(kept) + "|" + " ".join(words)


def safe_name(text, limit=170):
    text = unicodedata.normalize("NFC", text)
    # Separators become a dash, as in the processor's target names, so 'A / B' keeps both words apart
    # and a slash inside a name ('240 KM/H') stays one word.
    text = re.sub(r"\s+[/|\\]\s*|\s*[/|\\]\s+|\|", " - ", text)
    text = re.sub(r"[/\\]", "-", text).replace(":", " -")
    text = _BAD_FILENAME.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].rstrip(" .")


def format_rank(type_name):
    """Lossless beats lossy. Container names are evidence of intent only; the audio is probed later."""
    name = type_name.lower().strip(". ")
    if name in LOSSLESS_NAMES:
        return 3
    if "mp3" in name:
        return 1
    return 0


def best_file(files):
    return max(files, key=lambda f: (format_rank(f["type"]), f.get("size_kb") or 0))


_VERSION_WORDS = re.compile(r"\b(extended|radio|edit|mix|original|version|short\w*|remix|single)\b", re.IGNORECASE)
_SHORT_WORDS = re.compile(r"\b(radio|short\w*|edit|single|clean)\b", re.IGNORECASE)
_DJ_EDIT = re.compile(r"\bdj\s+edit\b", re.IGNORECASE)
# "(feat. X)" names a guest, not a mix: "Orbit (feat. X)" and "Orbit (feat. X) (Extended Mix)" are one mix.
_FEATURING = re.compile(r"\s*\((?:feat\.?|ft\.?|featuring)\s[^()]*\)", re.IGNORECASE)
# 2026-10-24: Promoly served 15-second preview clips as 'extended mixes' for files it had not finished
# uploading. A real club track is never this short; intros, interludes and orchestral or piano pieces can be.
PREVIEW_SECONDS = 60
_SHORT_PIECE = re.compile(r"\b(intro|outro|interlude|orchestral|piano|ambient|skit|prelude|acapella|a cappella|loop|fx)\b",
                          re.IGNORECASE)
# 2026-10-23: an edit longer than this is still a DJ tool, whatever its name says. Too many versions beat too few.
LONG_EDIT_SECONDS = 300


def seconds(value):
    """Track length in seconds from a number or an 'm:ss' / 'h:mm:ss' string; None when unknown."""
    if isinstance(value, (int, float)):
        return float(value) or None
    parts = str(value or "").strip().split(":")
    if len(parts) > 1 and all(p.isdecimal() for p in parts):
        return float(sum(int(p) * 60 ** i for i, p in enumerate(reversed(parts))))
    return None


def dj_version(track):
    """A DJ Edit (no radio in the name) or any edit over LONG_EDIT_SECONDS: kept beside the extended mix."""
    kind = version_kind(track["name"])
    return kind == "dj" or (kind == "short" and (seconds(track.get("duration")) or 0) > LONG_EDIT_SECONDS)


_MIX_WORDS = re.compile(r"\b(mix|remix|edit|extended|version|dub|rework|bootleg|vip|radio|instrumental|vocal|club|original)\b",
                        re.IGNORECASE)


def mix_parts(name):
    """('Artist - Title', [mix credits]) for a promo or library name, with or without [label] and extension.

    Every trailing bracket counts, so 'Monologue (JØRD Remix) (Edit)' keeps the JØRD credit. A trailing [label]
    is dropped unless it names a mix ('[AM Extended Mix] [Blooom]'), and a '(2)' copy number is ignored.
    """
    base, mixes = _mix_parts(name)
    return base, list(mixes)


@lru_cache(maxsize=1 << 18)
def _mix_parts(name):
    stem = Path(name).stem if Path(name).suffix.lower() in AUDIO_SUFFIXES else name
    stem = _FEATURING.sub("", normalize_mix_type(unicodedata.normalize("NFC", stem))).strip()
    while (square := re.search(r"\s*\[([^\]]*)\]\s*$", stem)):
        stem = stem[:square.start()]
        if _MIX_WORDS.search(square.group(1)):
            stem += " (" + square.group(1) + ")"
            break
    stem = re.sub(r"(?:\s*\(\d+\))+\s*$", "", stem)
    match = re.search(r"(?:\s*\([^()]*\))+\s*$", stem)
    if not match:
        return stem, ()
    return stem[:match.start()], tuple(re.findall(r"\(([^()]*)\)", match.group(0)))


@lru_cache(maxsize=1 << 18)
def version_kind(name):
    """'extended', 'dj' (DJ Edit without radio), 'short' (radio or edit) or 'full' (original, club, anything else)."""
    mix = " ".join(mix_parts(name)[1])
    # 2026-10-28: Euphonic calls its extended mix 'DJ Version' ('David Broaders - Polylith (DJ Version)').
    if re.search(r"extend|\bdj\s+version\b", mix, re.IGNORECASE):
        return "extended"
    if _DJ_EDIT.search(mix) and not re.search(r"radio", mix, re.IGNORECASE):
        return "dj"
    return "short" if _SHORT_WORDS.search(mix) else "full"


@lru_cache(maxsize=1 << 18)
def version_group(name):
    """Tracks that are lengths of the same mix share a group: 'X Remix' and 'X Extended Remix' do, 'Dub Mix' does not."""
    base, mixes = mix_parts(name)
    rest = _VERSION_WORDS.sub(" ", re.sub(r"\bdj\s+(?=radio\b|version\b)", " ", " ".join(mixes), flags=re.IGNORECASE))
    return normalized(base) + "|" + normalized(rest)


@lru_cache(maxsize=1 << 18)
def _fold(text):
    """normalized() that also ignores accents and apostrophes: 'Winter's' and 'Winters', 'André' and 'Andre'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return normalized(re.sub(r"['’`´]", "", text))


_FEATURING_NAME = re.compile(r"\((?:feat\.?|ft\.?|featuring)\s([^()]*)\)", re.IGNORECASE)
_PART = re.compile(r"\d|\b(?:part|pt|vol|chapter|act|episode)\b", re.IGNORECASE)
# 'Rocking (Reloaded) (Extended Mix)' is a new version of 'Rocking (Extended Mix)', not a subtitle.
_REWORK = re.compile(r"\b(?:reloaded|revisited|remaster\w*|redux|reprise|reimagined|refix|remake|re-?edit|rerub|update[ds]?"
                     r"|anniversary|classic|reboot|reconstruct\w*|reborn|returns|rewired|rebirth)\b", re.IGNORECASE)
_GUEST = re.compile(r"\((?:feat\.?|ft\.?|featuring)\s([^()]*)\)|\s(?:feat\.?|ft\.?|featuring)\s(.+)$", re.IGNORECASE)


@lru_cache(maxsize=1 << 18)
def track_identity(name):
    """(artists, work) for matching the same track across spellings and versions; None without 'Artist - Title'.

    Artists are an order-free set with guests from '(feat. X)' merged in; a remixer credited as artist is
    dropped as loose_key does. work is the title plus the mix credit without length words, so Original,
    unmarked, Extended and Radio Edit of one mix share it, and 'X Remix' differs from 'Y Remix'.
    """
    stem = Path(name).stem if Path(name).suffix.lower() in AUDIO_SUFFIXES else name
    artist, separator, title = unicodedata.normalize("NFC", stem).partition(" - ")
    if not separator:
        return None
    guests = [m.group(1) for m in _FEATURING_NAME.finditer(title)]
    base, mixes = mix_parts("x - " + title)
    base = base.partition(" - ")[2]
    # 2026-10-28: 'Sturm Der Nacht (Nur Für Dich) (Extended Mix)' is 'Sturm Der Nacht (Extended Mix)' in the library.
    # A bracket before the mix credit that names no mix, part or short piece is a subtitle, not a version.
    mixes = [m for i, m in enumerate(mixes) if i == len(mixes) - 1 or _MIX_WORDS.search(m)
             or _SHORT_PIECE.search(m) or _PART.search(m) or _REWORK.search(m)]
    guests += [g for part in (base, artist) for m in _GUEST.finditer(part) for g in m.groups() if g]
    artist = _GUEST.sub("", artist)
    credit = _fold(_VERSION_WORDS.sub(" ", re.sub(r"\bdj\s+(?=radio\b|version\b)", " ", " ".join(mixes), flags=re.IGNORECASE)))
    # 'Bart Skils x Waska Remix' and 'Bart Skils & Waska Remix' are one credit.
    credit = re.sub(r"(?<!\S)x(?!\S)", " ", credit).split()
    credit = " ".join(credit)
    title = _fold(_GUEST.sub("", base))
    artists = {a for part in [artist, *guests] for a in artist_names(part, _fold)}
    kept = {a for a in artists if " " + a + " " not in " " + credit + " "} or artists
    return (frozenset(kept), title + "|" + credit) if title else None


class VersionIndex:
    """Every version of each track among a set of names, for 'is a longer version of this already here?'."""

    def __init__(self, paths=()):
        self.by_work = {}
        self.by_title = {}
        for path in paths:
            self.add(path)

    def add(self, path):
        name = library_name(path)
        identity = track_identity(name)
        if identity:
            self.add_known(path, identity[0], identity[1], version_kind(name))

    def add_known(self, path, artists, work, kind):
        self.by_work.setdefault(work, []).append((artists, path, kind))
        self.by_title.setdefault(work.partition("|")[0], []).append((artists, path, kind, work.partition("|")[2]))

    def versions(self, *names, exact_artists=False, credit_typos=False):
        """(path, kind) of each indexed version of the same track; artist lists may differ by a missing co-artist."""
        found = {}
        for name in names:
            identity = track_identity(name or "")
            if not identity:
                continue
            for artists, path, kind in self.by_work.get(identity[1], ()):
                if artists == identity[0] or (not exact_artists and (artists <= identity[0] or identity[0] <= artists)):
                    found[path] = kind
            # Near remixer spellings are optional audio candidates, never automatic ownership evidence.
            title, _, credit = identity[1].partition("|")
            for artists, path, kind, other in self.by_title.get(title, ()) if credit_typos else ():
                if (artists == identity[0] or (not exact_artists and (artists <= identity[0] or identity[0] <= artists))) \
                        and one_letter_apart(credit, other):
                    found.setdefault(path, kind)
        return list(found.items())


def one_letter_apart(a, b):
    """Two credits of 8 or more characters that differ in exactly one letter at the same place."""
    return len(a) == len(b) >= 8 and sum(x != y for x, y in zip(a, b)) == 1 and \
        all(x.isalpha() and y.isalpha() for x, y in zip(a, b) if x != y)


def unmarked_name(path):
    """A name with no mix credit at all: 'Artist - Title [Label].aiff'."""
    return not mix_parts(path.rpartition("/")[2])[1]


def main_versions(members):
    """The main length of one mix: the version marked extended, else the longest, else by name.

    Without full lengths, an Original Mix or unmarked version outranks anything marked radio, edit or short.
    Returns (chosen, reason); an empty choice means the main version cannot be told apart.
    """
    extended = [t for t in members if version_kind(t["name"]) == "extended"]
    if extended:
        return extended, "extended mix"
    lengths = {id(t): seconds(t.get("duration")) for t in members}
    if all(lengths.values()):
        longest = max(lengths.values())
        return [t for t in members if lengths[id(t)] == longest][:1], "longest version, treated as the extended mix"
    full = [t for t in members if version_kind(t["name"]) == "full"] or members
    if len(full) == 1:
        return full, "full version, treated as the extended mix"
    known = [t for t in full if lengths[id(t)]]
    if len(known) == len(full):
        return [max(known, key=lambda t: lengths[id(t)])], "longest version, treated as the extended mix"
    return [], "multiple versions without an extended mix or lengths to compare; needs review"


def select_versions(tracks):
    """Per mix: the main version (extended, else longest); DJ edits stay beside it; one version is taken.

    Returns {track name: (wanted, reason)}. Shorter radio, short and unmarked versions are dropped.
    Intro, club, dub and other alternative mixes retain their own groups.
    """
    groups, verdict = {}, {}
    for track in tracks:
        if dj_version(track):
            verdict[track["name"]] = (True, "DJ edit, kept beside the extended mix")
            continue
        # The promo's own spelling: a resolved store name must not split one mix from its shorter lengths.
        groups.setdefault(version_group(track.get("original_name", track["name"])), []).append(track)
    for members in groups.values():
        chosen, why = (members, "only version offered") if len(members) == 1 else main_versions(members)
        for track in members:
            wanted = any(track is c for c in chosen)
            verdict[track["name"]] = (wanted, why if wanted or not chosen else "shorter version; the extended mix is wanted")
    return verdict


@lru_cache(maxsize=1 << 18)
def title_key(name):
    """Release-level key: artists plus title, ignoring mix, label and extension."""
    stem = Path(name).stem if Path(name).suffix.lower() in AUDIO_SUFFIXES else name
    stem = re.sub(r"\s*\[[^\]]*\]\s*$", "", stem)
    artist, separator, rest = stem.partition(" - ")
    if not separator:
        return None
    title = re.sub(r"\([^()]*\)", " ", rest)
    artists = sorted(artist_names(artist))
    return ";".join(artists) + "|" + normalized(title)


def _key_version():
    """Library keys depend on these sources; any edit to them recomputes the saved keys once."""
    digest = hashlib.sha256()
    for name in ("promo_pipeline.py", "process_promos.py", "promo_automation.py"):
        digest.update((SCRIPTS / name).read_bytes())
    return digest.hexdigest()[:16]


KEY_VERSION = _key_version()


def library_keys(path):
    """(mix, loose, title, artists, work, kind) for one library path; artists and work are None without 'A - T'."""
    name = library_name(path)
    identity = track_identity(name)
    return (mix_key(name), loose_key(name), title_key(name), sorted(identity[0]) if identity else None,
            identity[1] if identity else None, version_kind(name))


def library_name(path):
    """Recognize a set file's order number, Camelot key and trailing gain without changing the file."""
    name = path.rpartition("/")[2]
    stem = Path(name).stem
    match = re.fullmatch(r"[a-z]?\d{1,4}\s+-\s+(?:0?[1-9]|1[0-2])[ab]\s+-\s+(.+ - .+)", stem, re.I)
    if not match:
        return name
    return re.sub(r"\s+-\d+(?:[.,]\d+)?$", "", match.group(1)) + Path(name).suffix


def ownership_path(path):
    """Research holds and rejection samples are not evidence that the user owns a playable mix."""
    ignored = [HOLD, SETTINGS.get("exclusion_samples_folder")]
    target = unicodedata.normalize("NFC", path).casefold()
    return not any(target.startswith(unicodedata.normalize("NFC", folder).casefold().rstrip("/") + "/")
                   for folder in ignored if folder)


class LibraryIndex:
    """In-memory view of the Dropbox library snapshot, keyed by mix_key.

    2026-10-24: parsing 79,000 names took 23 seconds and ran in every lane, about 63 times a run. The keys are
    now saved per path in library_keys, so only new or renamed files are parsed.
    """

    def __init__(self, store):
        self.by_key, self.by_title, self.by_loose = {}, {}, {}
        self.versions = VersionIndex()
        missing = []
        for path, data in store.db.execute("SELECT l.path, k.data FROM library l LEFT JOIN library_keys k "
                                           "ON k.path = l.path AND k.version = ?", (KEY_VERSION,)):
            if os.path.splitext(path)[1].lower() not in AUDIO_SUFFIXES or not ownership_path(path):
                continue
            if data is None:
                keys = library_keys(path)
                missing.append((path, KEY_VERSION, json.dumps(keys, ensure_ascii=False)))
            else:
                keys = json.loads(data)
            mix, loose, title, artists, work, kind = keys
            if work is not None:
                self.versions.add_known(path, frozenset(artists), work, kind)
            self.by_key.setdefault(mix, []).append(path)
            if loose:
                self.by_loose.setdefault(loose, []).append(path)
            if title:
                self.by_title.setdefault(title, []).append(path)
        if missing:
            try:
                with store.db:
                    store.db.execute("DELETE FROM library_keys WHERE version != ?", (KEY_VERSION,))
                    store.db.executemany("INSERT OR REPLACE INTO library_keys VALUES(?,?,?)", missing)
            except Exception:
                pass  # a busy database only means the next lane parses these names again

    def matches(self, key, *names):
        """Exact mix_key hits, else loose hits for any of the given track names (promo spelling and resolved)."""
        found = list(self.by_key.get(key, ()))
        for name in names if not found else ():
            found += [p for p in self.by_loose.get(loose_key(name) or "", ()) if p not in found]
        return found

    def title_matches(self, release_title):
        """Release-level hit. A title with a mix or remix credit is judged per track instead, so it never matches here."""
        if "(" in release_title:
            return []
        key = title_key(release_title)
        return list(self.by_title.get(key, ())) if key else []


def write_stream(chunks, destination, expected_bytes=None, tolerance=0.03):
    """Write a transfer to a .partial file and publish it atomically only once it looks complete."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    digest, size = hashlib.sha256(), 0
    with partial.open("wb") as target:
        for chunk in chunks:
            if chunk:
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        target.flush()
        os.fsync(target.fileno())
    if size == 0 or (expected_bytes and abs(size - expected_bytes) > expected_bytes * tolerance):
        os.remove(partial)
        raise RuntimeError(f"Transfer size {size} does not match the advertised size {expected_bytes}")
    os.replace(partial, destination)
    return size, digest.hexdigest()


def probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    if result.returncode:
        raise RuntimeError("ffprobe rejected the file: " + result.stderr.strip()[:160])
    data = json.loads(result.stdout)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise RuntimeError("File contains no audio stream")
    bits = int(audio.get("bits_per_sample") or 0) or int(audio.get("bits_per_raw_sample") or 0)
    return {"duration": float(data["format"].get("duration") or 0), "codec": audio.get("codec_name"),
            "sample_rate": int(audio.get("sample_rate") or 0), "bits": bits,
            "channels": int(audio.get("channels") or 0), "container": data["format"].get("format_name")}


def decode_check(path):
    """A full decode catches truncated or corrupt audio that a header probe accepts."""
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    if result.returncode or result.stderr.strip():
        raise RuntimeError("Audio decode reported errors: " + result.stderr.strip()[:160])


def validate_audio(path, expected_seconds=None, tolerance=3.0):
    info = probe(path)
    if info["duration"] < 5:
        raise RuntimeError("Audio is implausibly short")
    expected_seconds = seconds(expected_seconds)  # platforms give numbers or 'm:ss' strings
    if expected_seconds and abs(info["duration"] - expected_seconds) > tolerance:
        raise RuntimeError(f"Duration {info['duration']:.1f}s differs from the advertised {expected_seconds}s")
    decode_check(path)
    return info


def run_processor(raw_dir, out_dir, report_path):
    """Run the established tagging/renaming processor on one release's raw folder."""
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, str(SCRIPTS / "process_promos.py"), str(raw_dir), "--output-folder", str(out_dir),
                             "--search-dir", str(SCRIPTS), "--report", str(report_path)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", env=environment, timeout=3600)
    if result.returncode or not Path(report_path).exists():
        raise RuntimeError("Processor failed: " + result.stderr.strip()[-300:])
    return json.loads(Path(report_path).read_text(encoding="utf-8"))


def label_from_subject(subject):
    """Promo-Cloud style 'Label [CATALOG] : Artist - Title', or 'Promo: Artist - Title | Label'."""
    match = re.match(r"^(.*?)\s*\[([^\]]+)\]\s*:\s*", subject or "")
    if match:
        return match.group(1).strip(), match.group(2).strip()
    match = re.search(r" - [^|]+\|\s*([^|]+?)\s*$", subject or "")
    return (match.group(1), None) if match else (None, None)


def deliver_file(store, shared, client, local, asset_key, folder=DEST, stage="delivery_verified"):
    """Upload without overwriting, then prove the Dropbox bytes equal the local bytes."""
    import dropbox
    started = time.perf_counter()
    local = Path(local)
    target = folder + "/" + local.name
    wanted = dropbox_hash(local)
    try:
        existing = client.files_get_metadata(target)
    except dropbox.exceptions.ApiError as error:
        if error.error.is_path() and error.error.get_path().is_not_found():
            existing = None
        else:
            raise
    if existing is not None:
        if existing.content_hash != wanted:
            raise RuntimeError("A different file already exists at the delivery path; not overwriting")
    else:
        with upload_slot(store.root):
            upload_started = time.perf_counter()
            meta = shared.upload_file(client, local, target, dropbox.files.WriteMode.add)
        store.timing("dropbox_audio_upload", time.perf_counter() - upload_started,
                     store.asset(asset_key)["source_id"], local.stat().st_size)
        if unicodedata.normalize("NFC", meta.path_display).casefold() != unicodedata.normalize("NFC", target).casefold():
            raise RuntimeError("Dropbox renamed the upload; a same-named file appeared during delivery")
    remote = client.files_get_metadata(target)
    if remote.content_hash != wanted:
        raise RuntimeError("Dropbox content hash does not match the local file")
    store.asset_update(asset_key, stage, dropbox_path=remote.path_display, content_hash=wanted, local_path=None)
    store.timing("dropbox_audio_delivery_total", time.perf_counter() - started,
                 store.asset(asset_key)["source_id"], local.stat().st_size)
    return remote.path_display


def inventory_key(store, name):
    """Use canonical identities without orphaning assets saved before normalization fixes."""
    key = "mix:" + mix_key(name)
    if store.asset(key) is not None:
        return key
    rows = store.db.execute("SELECT asset_key,title,stage FROM assets WHERE title IS NOT NULL").fetchall()
    matches = [row for row in rows if "mix:" + mix_key(row["title"]) == key]
    if matches:
        return next((row["asset_key"] for row in matches if row["stage"] == "delivery_verified"), matches[0]["asset_key"])
    return key


def delivered_lossy_this_pass(store, row):
    if not row["dropbox_path"] or Path(row["dropbox_path"]).suffix.lower() not in LOSSY_SUFFIXES:
        return False
    if TEST_FOLDER_IS_ONE_PASS and row["dropbox_path"].casefold().startswith(DEST.casefold() + "/"):
        return True
    started = store.pass_started()
    return bool(started and row["updated_at"] >= started)


def _not_found(error):
    """Dropbox metadata and delete errors wrap the same lookup error under different names."""
    detail = error.error
    for check, get in (("is_path", "get_path"), ("is_path_lookup", "get_path_lookup")):
        if hasattr(detail, check) and getattr(detail, check)():
            return getattr(detail, get)().is_not_found()
    return False


def finish_replacements(store, client):
    """Remove lossy copies whose lossless replacement is verified in Dropbox, with any research note beside them."""
    import dropbox
    from promo_remote import note_path
    rows = store.db.execute("SELECT r.asset_key,r.old_path,r.old_hash,a.stage,a.dropbox_path,a.source_id FROM replacements r "
                            "JOIN assets a USING(asset_key)").fetchall()
    for row in rows:
        if row["stage"] not in {"delivery_verified", "held_for_research"} or row["dropbox_path"] == row["old_path"]:
            continue
        status = "old_lossy_removed"
        for path, expected in ((row["old_path"], row["old_hash"]), (note_path(row["old_path"]), None)):
            try:
                if expected and client.files_get_metadata(path).content_hash != expected:
                    status = "old_file_changed_left_in_place"
                    break
                client.files_delete_v2(path)
            except dropbox.exceptions.ApiError as error:
                if not _not_found(error):
                    raise
        store.db.execute("DELETE FROM replacements WHERE asset_key=?", (row["asset_key"],))
        store.db.commit()
        store.event(row["source_id"], "replace", status, old_path=row["old_path"], new_path=row["dropbox_path"])


def decide(store, library, key, offer, asset_key=None):
    """Pick an action for one track before any bytes move. Returns (action, reason, detail)."""
    row = store.asset(asset_key or "mix:" + key)
    lossless = format_rank(offer["best"]["type"]) >= 3
    if row is not None and row["stage"] == "planned" and lossless and store.db.execute(
            "SELECT 1 FROM replacements WHERE asset_key=?", (row["asset_key"],)).fetchone():
        return "replace", "retrying a lossless replacement that stopped early", {}
    # An excluded mix whose label or artist was taken off the list is a new offer again.
    if row is not None and row["stage"] not in {"planned", "excluded_label"}:
        if row["stage"] in DONE_STAGES:
            if lossless and delivered_lossy_this_pass(store, row):
                # 2026-10-23: within one pass the lossless copy wins outright, no upgrade question.
                return "replace", "lossless offer replaces a lossy copy delivered this pass", {"dropbox_path": row["dropbox_path"]}
            if lossless and row["dropbox_path"] and Path(row["dropbox_path"]).suffix.lower() in LOSSY_SUFFIXES:
                # 2026-10-24: never replace a copy from an earlier pass. The user sorts promos by date added, created
                # and modified and copies them into their selections, so an upgrade would come back as a duplicate.
                return "upgrade_candidate", "an earlier pass delivered only a lossy copy; lossless offered", {"library": [row["dropbox_path"]]}
            return "skip", "already delivered", {"dropbox_path": row["dropbox_path"]}
        if row["stage"] in RESUMABLE_STAGES:
            owner = store.db.execute("SELECT owner FROM claims WHERE asset_key=?", (row["asset_key"],)).fetchone()
            if owner and owner[0] != str(os.getpid()) and process_alive(owner[0]):
                return "uncertain", "another live worker owns this asset", {}
            if not row["local_path"] or not Path(row["local_path"]).is_file():
                return "uncertain", "saved " + row["stage"] + " file is missing; reconcile delivery before retrying", {}
            return "resume", "interrupted after " + row["stage"], {}
        return "uncertain", "previous attempt stopped at " + row["stage"], {}
    names = {offer.get("name"), offer.get("original_name")} - {None}
    found = library.matches(key, *names)
    if not found and names and not dj_version({"name": offer.get("name") or min(names), **offer}):
        # 2026-10-24: the same track in another length or spelling is owned too. A radio edit or an Original /
        # unmarked version beside an owned extended mix is a duplicate; an extended mix beside an owned Original is
        # reported for the user to judge, never delivered over it, unless that Original came in this same pass.
        kind = version_kind(offer.get("name") or min(names))
        owned = getattr(library, "versions", None)
        owned = owned.versions(*names) if isinstance(owned, VersionIndex) else []
        longer = [p for p, k in owned if (k == "extended" and kind != "extended") or (k == "full" and kind == "short")]
        same = [p for p, k in owned if k == kind or (k == "dj" and kind == "short")]
        # 2026-10-28: an unmarked library copy is usually the extended mix under a store-less name
        # ('Amos & Riot Night x ... - Loved In Silence [FSOE]' beside the offered Extended Mix), so it counts as owned.
        # The same unmarked name delivered in this test pass still gives way to the extended mix.
        unmarked = [p for p, k in owned if k == "full" and kind == "extended" and unmarked_name(p)
                    and not p.lower().startswith(DEST.lower() + "/")]
        if longer or unmarked:
            return "skip", "a longer version of this track is already in library", {"library": (longer or unmarked)[:3]}
        if same:
            found = same
        elif kind == "extended" and owned:
            earlier = [p for p, k in owned if k == "full" and not p.lower().startswith(DEST.lower() + "/")]
            if earlier:
                return "upgrade_candidate", "library has the Original or unmarked version; extended mix offered", {"library": earlier[:3]}
    if found:
        lossy_only = all(Path(p).suffix.lower() not in LOSSLESS_SUFFIXES for p in found)
        if lossy_only and format_rank(offer["best"]["type"]) >= 3:
            # 2026-10-24: never deliver an upgrade of a track from an earlier pass. The user has already copied it
            # into their selections, so a second copy would look like a new promo and end up as a duplicate there.
            return "upgrade_candidate", "library has only a lossy copy; lossless offered", {"library": found[:3]}
        return "skip", "already in library", {"library": found[:3]}
    return "download", "new mix", {}


def parse_exclusions_csv(data):
    """Rows of type,value,note. Excel on a Belgian PC saves with semicolons, so both separators are read."""
    import csv
    import io
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    first = next((line for line in text.splitlines() if line.strip()), "")
    reader = csv.reader(io.StringIO(text), delimiter=";" if first.count(";") > first.count(",") else ",")
    rules = {"labels": [], "artists": [], "title_words": []}
    for row in reader:
        if len(row) < 2 or row[0].strip().startswith("#"):
            continue
        kind = _EXCLUSION_TYPES.get(row[0].strip().casefold())
        value = normalized(row[1])
        if not kind or not value:
            continue  # header row, blank value or an unknown type
        if kind == "labels" and value in {"self released", "self release", "selfreleased"}:
            continue  # excluding the label would drop every self release; such rows belong under artist
        if value not in rules[kind]:
            rules[kind].append(value)
    return rules


def _read_rules(path):
    path = Path(path)
    if not path.is_file():
        return None
    if path.suffix.lower() == ".csv":
        return parse_exclusions_csv(path.read_bytes())
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: [normalized(v) for v in data.get(k, []) if normalized(v)] for k in ("labels", "artists", "title_words")}


def load_exclusions(path=None):
    """The user's out-of-style list: labels, artists and title words never downloaded. Empty means nothing excluded.

    The CSV in the Dropbox audit folder is the master. Without Dropbox the last fetched copy is used.
    """
    if path:
        return _read_rules(path) or {}
    if not EXCLUSIONS_REMOTE:
        return _read_rules(EXCLUSIONS_CACHE) or _read_rules(EXCLUSIONS) or {}
    loaded = _EXCLUSIONS_LOADED.get(EXCLUSIONS_REMOTE)
    if loaded and time.monotonic() - loaded[0] < EXCLUSIONS_TTL:
        return loaded[1]
    try:
        _, client = dropbox_skill()
        _, response = client.files_download(EXCLUSIONS_REMOTE)
        data = response.content
        rules = parse_exclusions_csv(data)
        if not Path(EXCLUSIONS_CACHE).is_file() or Path(EXCLUSIONS_CACHE).read_bytes() != data:
            partial = Path(str(EXCLUSIONS_CACHE) + ".partial")
            partial.write_bytes(data)
            os.replace(partial, EXCLUSIONS_CACHE)
    except Exception:
        rules = _read_rules(EXCLUSIONS_CACHE) or _read_rules(EXCLUSIONS) or {}
    _EXCLUSIONS_LOADED[EXCLUSIONS_REMOTE] = (time.monotonic(), rules)
    return rules


def excluded(track, release, rules):
    """Reason this track is on the exclude list, or None."""
    if not rules:
        return None
    # A credited track imprint outranks a sampler's label. A PR sender is never label evidence.
    label = track.get("label") or release.get("label")
    if label and normalized(label) in rules["labels"]:
        return "excluded label: " + label
    artists = {normalized(a) for a in _ARTIST_SPLIT.split(track.get("artist") or track["name"].split(" - ")[0])}
    hit = artists & set(rules["artists"])
    if hit:
        return "excluded artist: " + sorted(hit)[0]
    title = " " + normalized(track["name"]) + " "
    word = next((w for w in rules["title_words"] if " " + w + " " in title), None)
    return word and "excluded title word: " + word


def collect_release(store, adapter, source_id, library, dry_run=False, allow_gate=False, max_assets=None, defer=None):
    """Collect one release: decide, download, validate, process, deliver, verify. Returns a compact summary.

    defer maps a track name to the reason the coordinator plans a better copy of it from another source this run.
    """
    if max_assets is not None and (type(max_assets) is not int or max_assets < 1):
        raise ValueError("max_assets must be a positive integer or None")
    release = adapter.release(source_id)
    from promo_metadata import apply_resolution
    for track in release["tracks"]:
        stripped = strip_track_number(track["name"])
        if stripped != track["name"]:
            track.setdefault("original_name", track["name"])
            if track.get("artist"):
                track["artist"] = strip_track_number(track["artist"] + " - x")[:-4]
            track["name"] = stripped
        apply_resolution(track, store.root)
    summary = {"release": release["title"], "label": release["label"], "tracks": []}
    if release.get("warnings"):
        summary["warnings"] = release["warnings"]
    plan = []
    rules = load_exclusions()
    kept = []
    for track in release["tracks"]:
        reason = excluded(track, release, rules)
        if not reason:
            # A label first seen in the file tags excluded this mix after an earlier download; re-check it against
            # today's list so it is not fetched again, and so a removed exclusion lets it through again.
            row = store.asset("mix:" + mix_key(track["name"]))
            if row is not None and row["stage"] == "excluded_label":
                seen = json.loads(row["metadata"] or "{}").get("label")
                reason = seen and excluded({**track, "label": seen}, release, rules)
                if not reason and not dry_run:
                    store.asset_update(row["asset_key"], "planned")
        if reason:
            # Out of style: never downloaded, processed or given feedback.
            summary["tracks"].append({"track": track["name"], "action": "excluded", "reason": reason})
            store.event(source_id, "select", "excluded", track=track["name"], reason=reason)
        else:
            kept.append(track)
    release["tracks"] = kept
    verdict = select_versions(release["tracks"])
    for track in release["tracks"]:
        if not track.get("files"):
            summary["tracks"].append({"track": track["name"], "action": "unavailable", "reason": "no downloadable audio offered"})
            continue
        track["best"] = best_file(track["files"])
        key = mix_key(track["name"])
        wanted, why = verdict[track["name"]]
        if not wanted:
            action = "needs_review" if "needs review" in why else "not_wanted"
            plan.append((track, key, action))
            summary["tracks"].append({"track": track["name"], "action": action, "reason": why,
                                      "format": track["best"]["type"]})
            store.event(source_id, "select", action, track=track["name"], reason=why)
            continue
        saved_key = inventory_key(store, track.get("original_name", track["name"]))
        action, reason, detail = decide(store, library, key, track, asset_key=saved_key)
        if action in {"download", "replace"} and track["name"] in (defer or {}):
            action, reason, detail = "deferred", defer[track["name"]], {}
        plan.append((track, saved_key.removeprefix("mix:"), action))
        summary["tracks"].append({"track": track["name"], "action": action, "reason": reason,
                                  "format": track["best"]["type"]})
        if action in {"skip", "upgrade_candidate", "uncertain", "replace", "deferred"}:
            store.event(source_id, "select", {"skip": "skipped", "upgrade_candidate": "upgrade_candidate",
                                              "uncertain": "needs_review", "replace": "replace",
                                              "deferred": "deferred"}[action],
                        track=track["name"], reason=reason, **detail)
    todo = [p for p in plan if p[2] in {"download", "resume", "replace"}]
    if max_assets is not None:
        # Apply the pilot cap after full version and duplicate selection. Filtering the release first could
        # incorrectly turn a radio edit into its only version and miss the offered extended mix.
        deferred = {id(t) for t, _, _ in todo[max_assets:]}
        for track, _, _ in todo[max_assets:]:
            for item in summary["tracks"]:
                if item.get("track") == track["name"] and item.get("action") in {"download", "resume", "replace"}:
                    item.update(action="deferred", reason="collection asset limit reached")
            store.event(source_id, "select", "deferred", track=track["name"], reason="collection asset limit reached")
        plan = [(t, k, "deferred" if id(t) in deferred else action) for t, k, action in plan]
        todo = todo[:max_assets]
        summary["asset_limit"] = max_assets
    if dry_run or not todo:
        if release.get("collection_blocker"):
            summary["collection_blocker"] = release["collection_blocker"]
        return summary
    if release.get("collection_blocker"):
        raise CollectionBlocked(release["collection_blocker"])
    if release["gated"] and not allow_gate:
        raise GateRequired("Release is feedback-locked; rerun with explicit feedback permission")

    folder = STAGING / source_id
    try:
        _fetch_and_deliver(store, adapter, source_id, release, plan, todo, summary, folder)
    finally:
        # Zero local retention: every output is in Dropbox (test folder or Needs Research) or is retried.
        shutil.rmtree(folder, ignore_errors=True)
        for _, key, _ in todo:
            row = store.asset("mix:" + key)
            if row is not None and row["stage"] in RESUMABLE_STAGES:
                # The bytes already came down once, so the platform has counted it; the next run fetches again.
                store.release_claim(row["asset_key"], "staging deleted after a failed release; download again")
    return summary


def _expected_parts(track, label):
    """Artist, title and label as the platform states them, for comparison with what the processor wrote."""
    artist = track.get("artist")
    prefix = (artist + " - ") if artist else ""
    title = track["name"][len(prefix):] if prefix and track["name"].startswith(prefix) else track["name"].split(" - ", 1)[-1]
    return {"artist": artist, "title": track.get("expected_title") or title, "label": label}


def tag_mismatches(track, label, item):
    """Differences between the platform's artist/title/label and the processor's result. Empty means they agree."""
    want, found = _expected_parts(track, label), {k: item.get(k) for k in ("artist", "title", "label")}
    problems = []
    for field, expected in want.items():
        def metadata_key(value):
            value = unicodedata.normalize("NFC", str(value or ""))
            if field == "artist":
                value = re.sub(r"\b(?:ft|featuring|feat)(?:\.\s*|\s+)", "feat. ", value, flags=re.IGNORECASE)
            elif field == "title":
                value = normalize_mix_type(value)
            return normalized(value)
        if not expected or not found.get(field):
            problems.append(f"{field}: required metadata is missing")
        elif metadata_key(expected) != metadata_key(found[field]):
            problems.append(f"{field}: platform says {expected!r}, file got {found.get(field)!r}")
    return problems


def true_extension(path):
    """Rename a transfer whose header disagrees with its staged extension; aif and aiff count as the same."""
    with open(path, "rb") as handle:
        kind = container(handle.read(16))
    if kind is None or path.suffix.lower().lstrip(".") in {kind, "aif" if kind == "aiff" else kind}:
        return path
    fixed = path.with_suffix("." + kind)
    if fixed.exists():
        raise RuntimeError("Staged file with the real extension already exists; inspect before collecting")
    path.rename(fixed)
    return fixed


def _fetch_and_deliver(store, adapter, source_id, release, plan, todo, summary, folder):
    """Download, process and deliver the wanted tracks; held tracks go to Needs Research with a note."""
    raw, processed = folder / "raw", folder / "processed"
    raw.mkdir(parents=True, exist_ok=True)
    label = release["label"]
    names = {}
    claimed = []
    for track, key, action in todo:
        label = track.get("label") or release["label"]
        asset_key = "mix:" + key
        extension = adapter.extension(track["best"])
        raw_name = safe_name(track["name"] + (f" [{label}]" if label else "")) + extension
        if raw_name in names and names[raw_name][1] != asset_key:
            raise RuntimeError("Two tracks collapse to the same staging filename; inspect before collecting")
        names[raw_name] = (track, asset_key)
        if action == "replace" and store.asset(asset_key)["stage"] in DONE_STAGES:
            store.reopen_for_replacement(asset_key)
        if action in {"download", "replace"}:
            if not store.claim(asset_key, source_id, track["name"], track["best"]["type"]):
                next(item for item in summary["tracks"] if item.get("track") == track["name"])["action"] = "claim_denied"
                continue
            store.event(source_id, "download", "intent", track=track["name"], file_type=track["best"]["type"],
                        platform_download_status="will be marked downloaded by the platform")
            claimed.append((track, asset_key, raw_name))
        else:
            if not store.resume_claim(asset_key):
                summary["tracks"].append({"track": track["name"], "action": "claim_denied"})
                continue
            claimed.append((track, asset_key, raw_name))
    if not claimed:
        return
    store.sync_logs()
    if release["gated"] and any(store.asset(key)["stage"] == "claimed" for _, key, _ in claimed):
        adapter.unlock(source_id, release)

    digests = {}
    for index, (track, asset_key, raw_name) in enumerate(claimed):
        label = track.get("label") or release["label"]
        row = store.asset(asset_key)
        path = raw / raw_name
        try:
            if row["stage"] == "claimed":
                started = time.perf_counter()
                size, digest = adapter.download(source_id, track["best"], path)
                path = true_extension(path)  # some platforms hide the format until the transfer
                claimed[index] = (track, asset_key, path.name)
                store.timing("audio_download", time.perf_counter() - started, source_id, size)
                started = time.perf_counter()
                info = validate_audio(path, track.get("duration"))
                store.timing("raw_audio_validation", time.perf_counter() - started, source_id, size)
                if info["duration"] < PREVIEW_SECONDS and not _SHORT_PIECE.search(track["name"]):
                    raise RuntimeError(f"Audio is only {info['duration']:.0f}s; looks like a preview clip, not the promo")
                if digest in digests and digests[digest] != track["name"]:
                    raise RuntimeError("Same bytes as " + digests[digest] + "; the platform served a placeholder")
                digests[digest] = track["name"]
                if hasattr(adapter, "resolve_metadata"):
                    adapter.resolve_metadata(source_id, track, path)
                    label = track.get("label") or release["label"]
                reason = excluded({**track, "label": label}, release, load_exclusions())
                if reason:
                    # The label or artist was only known from the file. Discard it before processing or delivery.
                    path.unlink(missing_ok=True)
                    with store.db:
                        store.db.execute("DELETE FROM claims WHERE asset_key=?", (asset_key,))
                    store.asset_update(asset_key, "excluded_label", local_path=None, raw_sha256=digest,
                                       metadata=json.dumps({"label": label, "excluded": reason}))
                    store.event(source_id, "select", "excluded", track=track["name"], reason=reason, after_download=True)
                    next(i for i in summary["tracks"] if i.get("track") == track["name"]).update(action="excluded",
                                                                                                reason=reason)
                    continue
                store.asset_update(asset_key, "downloaded", local_path=str(path), raw_sha256=digest,
                                   metadata=json.dumps({"raw_audio": info, "raw_bytes": size, "label": label}))
                store.event(source_id, "download", "downloaded", track=track["name"], bytes=size, audio=info)
            elif row["stage"] == "downloaded":
                if row["local_path"] and Path(row["local_path"]).parent == raw:
                    claimed[index] = (track, asset_key, Path(row["local_path"]).name)
                if hasattr(adapter, "resolve_metadata"):
                    adapter.resolve_metadata(source_id, track, Path(row["local_path"]))
        except Exception as error:
            store.asset_update(asset_key, "claimed", metadata=json.dumps({"error": type(error).__name__ + ": " + str(error)[:200]}))
            store.event(source_id, "download", "uncertain", track=track["name"], error=str(error)[:200],
                        note="Platform may have recorded the attempt; check before retrying")
            store.sync_logs()
            raise

    claimed = [entry for entry in claimed if store.asset(entry[1])["stage"] != "excluded_label"]
    if not claimed:
        store.sync_logs()
        return
    # A processed output can be retried after an upload failure without processing or platform feedback again.
    ready = [entry for entry in claimed if store.asset(entry[1])["stage"] == "processed"]
    for track, asset_key, _ in ready:
        output = Path(store.asset(asset_key)["local_path"])
        destination = deliver_file(store, *dropbox_skill(), output, asset_key)
        store.event(source_id, "delivery", "delivery_verified", track=track["name"], dropbox_path=destination,
                    final_name=output.name, recovered=True)
        summary["tracks"].append({"delivered": output.name, "recovered": True})
    claimed = [entry for entry in claimed if entry not in ready]
    if not claimed:
        finish_replacements(store, dropbox_skill()[1])
        store.sync_logs()
        return
    started = time.perf_counter()
    report = run_processor(raw, processed, folder / "report.json")
    store.timing("processing", time.perf_counter() - started, source_id)
    mapped = {item["original"]: item["target"] for item in report["processed"]}
    for item in report["skipped"]:
        mapped.setdefault(item["original"], item["target"])
    shared, client = dropbox_skill()
    for track, asset_key, raw_name in claimed:
        label = track.get("label") or release["label"]
        target_name = mapped.get(raw_name)
        output = processed / target_name if target_name else None
        if not output or not output.exists():
            store.event(source_id, "process", "failed", track=track["name"], errors=report.get("errors", [])[:3])
            raise RuntimeError("Processor produced no output for " + track["name"])
        issues = [i for i in report.get("tag_issues", []) if target_name in str(i)]
        raw_info = json.loads(store.asset(asset_key)["metadata"]).get("raw_audio", {})
        started = time.perf_counter()
        out_info = validate_audio(output, track.get("duration"))
        store.timing("processed_audio_validation", time.perf_counter() - started, source_id, output.stat().st_size)
        if raw_info and (out_info["sample_rate"] != raw_info["sample_rate"] or out_info["bits"] < raw_info["bits"]):
            store.event(source_id, "process", "precision_lost", track=track["name"], raw=raw_info, output=out_info)
            raise RuntimeError("Processing reduced audio precision; not delivering")
        item = next((i for i in report["processed"] + report["skipped"] if i["target"] == target_name), {})
        problems = tag_mismatches(track, label, item)
        from promo_metadata import label_issues
        problems += label_issues(label)
        if not track.get("metadata_resolution") and label_is_artist(label, track):
            # 2026-10-28: Talla 2XLC and Sound Players send their own promos; the sender's name ended up as the label.
            problems.append("Label is only the artist's name (the promo sender); research the actual imprint")
        problems += track.get("metadata_conflicts", [])
        problems += ["required tag validation failed: " + str(issue) for issue in issues]
        problems += ["processor flagged this file as unknown" for u in report.get("unknowns", [])
                     if u.get("original_filename") == raw_name]
        if problems:
            destination = deliver_file(store, shared, client, output, asset_key, HOLD, "held_for_research")
            from promo_remote import write_note
            write_note(client, destination, problems)
            store.event(source_id, "process", "held_for_research", track=track["name"], problems=problems,
                        dropbox_path=destination)
            summary["tracks"].append({"track": track["name"], "action": "held_for_research", "problems": problems})
            continue
        store.asset_update(asset_key, "processed", local_path=str(output))
        store.event(source_id, "process", "processed", track=track["name"], output=target_name,
                    audio=out_info, tag_issues=issues, unknown=bool(report.get("unknowns")))
        destination = deliver_file(store, shared, client, output, asset_key)
        store.event(source_id, "delivery", "delivery_verified", track=track["name"], dropbox_path=destination,
                    final_name=target_name, tag_issues=issues)
        summary["tracks"].append({"delivered": target_name, "tag_issues": issues})
    finish_replacements(store, client)
    store.sync_logs()
