#!/usr/bin/env python3
"""
Promo Processor v3 - Automated DJ promo music file processing.

Handles:
- Filename parsing (catalog-prefix, numbered, standard, bare/unknown)
- WAV -> AIFF conversion (for ID3 tag compatibility)
- .aif -> .aiff normalization
- Master/bounce suffix stripping
- Mix type normalization ([Extended] -> (Extended Mix))
- ft. -> feat. normalization
- ID3 tag writing/cleanup (TIT2, TPE1, TPE2, TALB, GRP1, TPUB)
- Tag cleanup (all COMM fields, redundant encoders, junk removal)
- Post-processing tag verification (checks all output files)
- Duplicate detection by normalized artist+title+duration (with ffprobe fallback)
- ZIP/RAR extraction
- Skip-existing for safe re-runs (idempotent)

Outputs a JSON report for AI-assisted follow-up on unknowns.

Usage:
    python process_promos.py <input_folder> [--label-csv <path>] [--output-folder <path>] [--dry-run] [--force]
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Try to import mutagen; give a clear error if missing
# ---------------------------------------------------------------------------
try:
    from mutagen import File as MutagenFile
    from mutagen.aiff import AIFF
    from mutagen.id3 import (
        COMM, GRP1, TALB, TCON, TIT2, TPUB, TPE1, TPE2, USLT,
    )
    from mutagen.mp3 import MP3
except ImportError:
    print("ERROR: mutagen is required. Install with: pip install mutagen --break-system-packages")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aif", ".aiff", ".flac"}
ARCHIVE_EXTENSIONS = {".zip", ".rar"}
JUNK_TALB_PATTERNS = [
    re.compile(r"\bmastered?\b", re.I),
    re.compile(r"\bmaster\b", re.I),
    re.compile(r"\bNEWFIXFIX\b", re.I),
    re.compile(r"^\d+[A-Z]\s*-\s*"),  # key notation like "6A - "
]
TYPO_CORRECTIONS = {
    "Extedned": "Extended",
    "Exteneded": "Extended",
    "Orignal": "Original",
    "Extnded": "Extended",
    "Tommorow": "Tomorrow",
}
# Tags to remove during cleanup
JUNK_TAG_KEYS = {"TXXX:ENCODER", "TXXX:HANDLER_NAME"}

# Master/bounce suffixes to strip from parsed titles
# Order matters: longest/most specific first
MASTER_SUFFIX_PATTERNS = [
    re.compile(r"\s+NEWFIXFIX\s+master(?:\s+V?\d+(?:\.\d+)?)?\s*$", re.I),
    re.compile(r"\s+NEWFIX\s+master(?:\s+V?\d+(?:\.\d+)?)?\s*$", re.I),
    re.compile(r"\s+master(?:ed)?(?:\s*[_\s]?\d+)?\s*$", re.I),
    re.compile(r"\s+MASTER(?:\s*[_\s]?V?\d+(?:\.\d+)?)?\s*$", re.I),
    re.compile(r"[_\s]+MASTER[_\s]*\d*\s*$", re.I),
    re.compile(r"\s+mastered\s*$", re.I),
]


# Studio bounce details at the end of a title: bit depth, sample rate, master words, versions, dates.
TECH_TAIL_PATTERNS = [
    re.compile(r"[\s_\-]+\d{2}\s*-?\s*bits?$", re.I),
    re.compile(r"[\s_\-]+(?:44[.,]?1|48|88[.,]?2|96|192)\s*k(?:hz)?$", re.I),
    re.compile(r"[\s_\-]+(?:main|final|pre)\s*master(?:ed)?$", re.I),
    re.compile(r"_(?:main|final|pre)?master(?:ed)?$", re.I),
    re.compile(r"(?<=\))[\s_\-]+final$", re.I),
    re.compile(r"[\s_\-]+v\d+(?:\.\d+)?$", re.I),
    re.compile(r"[\s_\-]+RC\d+(?:\s+\d{3,5})?$"),
    re.compile(r"[\s_\-]+\d{1,2}[-.]\d{1,2}[-.]\d{2,4}$"),
]


# ---------------------------------------------------------------------------
# Label code loading
# ---------------------------------------------------------------------------
def load_label_codes(csv_path: str) -> dict:
    """Load prefix->label_name mapping from CSV. Returns dict sorted longest-prefix-first."""
    codes = {}
    if not csv_path or not os.path.isfile(csv_path):
        return codes
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                prefix = row.get("prefix", "").strip()
                label = row.get("label_name", "").strip()
                if prefix and label:
                    codes[prefix] = label
    except Exception as e:
        print(f"WARNING: Failed to load label codes from {csv_path}: {e}", file=sys.stderr)
    # Sort by prefix length descending so longest match wins
    return dict(sorted(codes.items(), key=lambda x: -len(x[0])))


def save_label_codes(codes: dict, csv_path: str):
    """Save label codes back to CSV (sorted alphabetically)."""
    os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["prefix", "label_name"])
        writer.writeheader()
        for prefix in sorted(codes.keys()):
            writer.writerow({"prefix": prefix, "label_name": codes[prefix]})


def find_label_csv(explicit_path=None, search_dirs=None, input_folder=None, script_dir=None):
    """
    Find label_codes.csv by searching multiple locations.
    Returns (csv_path, source_description) or (None, None).

    Search order:
    1. Explicit --label-csv path
    2. Recursively in each search_dir (user's working/selected folders)
    3. Input folder
    4. Script directory (bundled fallback)
    """
    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path, "explicit path"

    # Search user's working/selected folders recursively
    if search_dirs:
        for search_dir in search_dirs:
            if not search_dir or not os.path.isdir(search_dir):
                continue
            for root, dirs, files in os.walk(search_dir):
                # Skip hidden directories and common non-relevant dirs
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__')]
                if "label_codes.csv" in files:
                    found = os.path.join(root, "label_codes.csv")
                    return found, f"found in {root}"

    # Input folder (non-recursive)
    if input_folder:
        p = os.path.join(input_folder, "label_codes.csv")
        if os.path.isfile(p):
            return p, "input folder"

    # Bundled fallback
    if script_dir:
        p = os.path.join(script_dir, "label_codes.csv")
        if os.path.isfile(p):
            return p, "bundled (fallback)"

    return None, None


# ---------------------------------------------------------------------------
# Filename cleaning helpers
# ---------------------------------------------------------------------------
def apply_typo_corrections(text: str) -> str:
    for wrong, right in TYPO_CORRECTIONS.items():
        text = text.replace(wrong, right)
    return text


def clean_master_suffixes(text: str) -> str:
    """Strip master/bounce/mastered suffixes from title strings."""
    if not text:
        return text
    # Master words and technical bounce tails, stripped from the end until none is left.
    previous = None
    while previous != text:
        previous = text
        for pattern in MASTER_SUFFIX_PATTERNS + TECH_TAIL_PATTERNS:
            text = pattern.sub("", text).rstrip(" _-")
    # A bare trailing version becomes the bracketed mix: 'Song - Extended Mix' -> 'Song (Extended Mix)'.
    if not text.endswith(")"):
        text = re.sub(r"(?:\s+-\s+|\s+)((?:Extended|Original|Radio|Club|Dub|Instrumental)\s+(?:Mix|Edit|Version))$",
                      lambda m: " (" + m.group(1).title() + ")", text, flags=re.I)
    # Clean trailing underscores, spaces, hyphens left behind
    text = text.rstrip(" _-")
    return text


def normalize_ft_to_feat(text: str) -> str:
    """Normalize 'ft.' to 'feat.' in artist strings (per filename standards)."""
    if not text:
        return text
    # Match standalone "ft." or "ft " (not inside a word)
    # "ft." -> "feat."
    text = re.sub(r"\bft\.\s*", "feat. ", text)
    # "ft " (without dot, followed by a capital letter = likely featured artist)
    text = re.sub(r"\bft\s+(?=[A-Z])", "feat. ", text)
    return text


def normalize_mix_type(text: str) -> str:
    """
    Normalize mix type variations in title strings.
    - [Extended] -> (Extended Mix)
    - [Original] -> (Original Mix)
    - (Extended mix) -> (Extended Mix) (capitalize)
    - (Extended Master) -> (Extended Mix)
    """
    if not text:
        return text

    # Brackets to parens for known mix types
    text = re.sub(r"\[Extended\]", "(Extended Mix)", text)
    text = re.sub(r"\[Original\]", "(Original Mix)", text)
    text = re.sub(r"\[Radio\]", "(Radio Edit)", text)
    # Glued words: (ExtendedMix) -> (Extended Mix)
    text = re.sub(r"\(((?:extended|original|radio|club|dub|instrumental|vocal))(mix|edit|remix)\)",
                  lambda m: "(" + m.group(1).capitalize() + " " + m.group(2).capitalize() + ")", text, flags=re.I)

    # Fix capitalization of mix types inside parens
    def fix_mix_caps(m):
        content = m.group(1)
        # Capitalize each word
        words = content.split()
        fixed = " ".join(w.capitalize() for w in words)
        return f"({fixed})"

    text = re.sub(
        r"\(((?:extended|original|radio|dub|club|instrumental|vocal)\s+(?:mix|remix|edit|dub))\)",
        fix_mix_caps,
        text,
        flags=re.I,
    )

    # (Extended Master) -> (Extended Mix)
    text = re.sub(r"\(Extended\s+Master\)", "(Extended Mix)", text, flags=re.I)
    # (Original Master) -> (Original Mix)
    text = re.sub(r"\(Original\s+Master\)", "(Original Mix)", text, flags=re.I)

    return text


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------
def parse_catalog_prefix_filename(filename: str, label_codes: dict):
    """
    Parse filenames like: ABL316_Artist__Name_-_Title_(Extended_Mix).wav
    Returns parsed dict or None if not a catalog-prefix file.
    """
    stem = Path(filename).stem

    # Pattern: PREFIX+DIGITS[+optional alphanumeric suffix] + underscore + rest
    # Prefix can start with digits (e.g. 2RT013, 2RBS131) or letters (ABL316)
    # Some have letter suffix after digits: FSOE969A, DLNA002R1B, FLASH218R1B
    m = re.match(r"^(\d*[A-Z][A-Z0-9]*?)(\d{2,}[A-Z0-9]*)_(.+)$", stem)
    if not m:
        return None

    prefix_code = m.group(1)
    catalog_num = m.group(2)
    rest = m.group(3)

    # Verify this looks like a catalog file: must have _-_ separator
    # (to avoid false positives on files like "TRIVIX_GECKO_MASTER")
    if "_-_" not in rest:
        return None

    # Look up the label - try longest prefix match first
    label = None
    matched_prefix = None
    full_prefix = prefix_code
    for code, label_name in label_codes.items():
        if full_prefix == code or full_prefix.startswith(code):
            label = label_name
            matched_prefix = code
            break

    if label is None:
        # Unknown prefix - still parse the rest but flag for AI
        label = f"UNKNOWN:{prefix_code}"

    # Parse the rest: Artist__Name_-_Title_(Mix)
    # Double underscore __ means & between artists
    # Single underscore _ means space
    # _-_ is the artist-title separator
    parts = rest.split("_-_", 1)
    if len(parts) == 2:
        artist_raw, title_raw = parts
    else:
        # No _-_ separator found - try other patterns
        artist_raw = rest
        title_raw = ""

    # Handle SounEmot-style double-underscore mix type encoding:
    # e.g. "Juntos__Club_Mix_" -> "Juntos_(Club_Mix)"
    # Pattern: title ends with __MixWords_ (trailing underscore)
    if title_raw:
        mix_match = re.search(r'__([A-Z][a-z]+(?:_(?:Out|[A-Z][a-z]+))*)_$', title_raw)
        if mix_match:
            mix_str = mix_match.group(1)
            title_raw = title_raw[:mix_match.start()] + '_(' + mix_str + ')'

    # Convert underscores: double underscore __ means & in ARTIST only, space in TITLE
    artist = artist_raw.replace("__", " & ").replace("_", " ").strip()
    title = title_raw.replace("__", " ").replace("_", " ").strip()

    # For 3+ artists joined with " & ", convert to comma-separated with & before last:
    # "A & B & C" -> "A, B & C"
    amp_parts = [p.strip() for p in artist.split(" & ")]
    if len(amp_parts) >= 3:
        artist = ", ".join(amp_parts[:-1]) + " & " + amp_parts[-1]

    # Clean up double spaces
    artist = re.sub(r"\s{2,}", " ", artist)
    title = re.sub(r"\s{2,}", " ", title)

    # v3: normalize ft. -> feat. in artist
    artist = normalize_ft_to_feat(artist)
    # v3: strip master suffixes from title
    title = clean_master_suffixes(title)
    # v3: normalize mix type variations
    title = normalize_mix_type(title)

    return {
        "artist": artist,
        "title": title,
        "label": label,
        "catalog_prefix": prefix_code,
        "catalog_number": catalog_num,
        "parse_type": "catalog_prefix",
    }


def parse_numbered_prefix_filename(filename: str):
    """
    Parse filenames like: 01 - Artist - Title (Mix) [Label].ext
    Returns parsed dict or None.
    """
    stem = Path(filename).stem
    # 2026-10-28: '01.Artist - Title' and '01) Artist - Title' too, when an 'Artist - Title' follows the number.
    m = re.match(r"^\d{1,3}\s*(?:-|[._)])\s*(?=[^\d\s])(.+ - .+)$", stem)
    if not m:
        return None
    rest = m.group(1)
    return parse_standard_filename_from_stem(rest, "numbered_prefix")


def parse_standard_filename_from_stem(stem: str, parse_type: str = "standard"):
    """Parse 'Artist - Title (Mix) [Label]' from a stem string."""
    # Extract [Label] if present
    label = None
    label_match = re.search(r"\[([^\]]+)\]\s*$", stem)
    if label_match:
        label = label_match.group(1)
        stem = stem[: label_match.start()].strip()

    # Normalize various dash separators:
    # " - " (standard), " – " (en-dash), " — " (em-dash)
    # Also handle tight dashes like "Artist- Title" or "Artist -Title"
    # but NOT dashes inside words (like "re-mix") or hyphenated names
    artist = None
    title = stem.strip()

    # First try standard separators
    for sep_pattern in [" - ", " – ", " — "]:
        if sep_pattern in stem:
            parts = stem.split(sep_pattern, 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                artist = parts[0].strip()
                title = parts[1].strip()
                break

    # If no standard sep found, try tight dashes (no space on one side)
    # Use regex to find "word- word" or "word -word" patterns (not mid-word hyphens)
    if artist is None:
        m = re.match(r"^(.+?\w)\s*[-–—]\s*(\w.+)$", stem)
        if m:
            candidate_artist = m.group(1).strip()
            candidate_title = m.group(2).strip()
            # Sanity check: artist shouldn't be too long relative to total
            # and title should look like a title (has parens or bracket or is multi-word)
            if len(candidate_artist) < len(stem) * 0.7:
                artist = candidate_artist
                title = candidate_title

    # v3: normalize ft. -> feat. in artist
    if artist:
        artist = normalize_ft_to_feat(artist)
    # v3: strip master suffixes from title
    title = clean_master_suffixes(title)
    # v3: normalize mix type variations
    title = normalize_mix_type(title)

    return {
        "artist": artist,
        "title": title,
        "label": label,
        "catalog_prefix": None,
        "catalog_number": None,
        "parse_type": parse_type if artist else "bare_unknown",
    }


def parse_underscore_separated_filename(filename: str):
    """
    Parse filenames like: Dark_Moon_pres._Suncolours_-_Moment_with_u_Extended.mp3
    Also: Mark_Sherry_-_Genesis_Where_It_Begins_Extended_Mix_[Outburst_Records].aif
    These have underscores throughout but are NOT catalog-prefixed.
    """
    stem = Path(filename).stem
    if "_-_" not in stem:
        return None
    # Must NOT have been caught by catalog parser (no leading PREFIX+DIGITS pattern)
    if re.match(r"^\d*[A-Z][A-Z0-9]*?\d{2,}_", stem):
        return None  # This is a catalog prefix file

    # First decode underscores to spaces, then extract label brackets
    decoded = stem.replace("__", " & ").replace("_", " ").strip()

    # Extract [Label] if present
    label = None
    label_match = re.search(r"\[([^\]]+)\]\s*$", decoded)
    if label_match:
        label = label_match.group(1)
        decoded = decoded[: label_match.start()].strip()

    # Split on " - " for artist/title
    parts = decoded.split(" - ", 1)
    artist = parts[0].strip() if parts[0].strip() else None
    title = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None

    # Try to restore parentheses around mix types in title
    # e.g. "Moment with u Extended" -> might need parens, but we can't be sure
    # "Genesis Where It Begins Extended Mix" -> "(Extended Mix)" at end
    if title:
        title = _restore_mix_parens(title)

    # v3: normalize ft. -> feat. in artist
    if artist:
        artist = normalize_ft_to_feat(artist)
    # v3: strip master suffixes from title
    if title:
        title = clean_master_suffixes(title)
    # v3: normalize mix type variations
    if title:
        title = normalize_mix_type(title)

    return {
        "artist": artist,
        "title": title,
        "label": label,
        "catalog_prefix": None,
        "catalog_number": None,
        "parse_type": "underscore_separated",
    }


def _restore_mix_parens(title: str) -> str:
    """
    If a title ends with a known mix type without parentheses, wrap it.
    e.g. "Track Name Extended Mix" -> "Track Name (Extended Mix)"
    """
    mix_patterns = [
        r"(Extended Mix)$",
        r"(Original Mix)$",
        r"(Extended Dub)$",
        r"(Dub Mix)$",
        r"(Radio Mix)$",
        r"(Radio Edit)$",
        r"(Club Mix)$",
        r"(Instrumental Mix)$",
        r"(Vocal Mix)$",
        r"(Extended Edit)$",
        r"(\w+ Extended Remix)$",
        r"(\w+ Extended Mix)$",
        r"(\w+ Remix)$",
        r"(Extended)$",
    ]
    for pat in mix_patterns:
        m = re.search(pat, title)
        if m:
            mix_text = m.group(1)
            before = title[: m.start()].strip()
            # Only wrap if it's not already in parens
            if not before.endswith("("):
                return f"{before} ({mix_text})"
    return title


def parse_standard_filename(filename: str):
    """
    Parse filenames like: Artist - Title (Mix) [Label].ext
    or: Artist - Title (Mix).ext (no label)
    Also handles en-dash and em-dash separators.
    """
    stem = Path(filename).stem
    # Must contain some kind of dash separator to be considered standard
    if not any(sep in stem for sep in [" - ", " – ", " — "]):
        return None
    return parse_standard_filename_from_stem(stem, "standard")


def parse_bare_filename(filename: str):
    """Fallback: file has no recognizable pattern."""
    stem = Path(filename).stem
    return parse_standard_filename_from_stem(stem, "bare_unknown")


def parse_filename(filename: str, label_codes: dict) -> dict:
    """Try all parsers in order and return the first match."""
    result = parse_catalog_prefix_filename(filename, label_codes)
    if result:
        return result
    result = parse_numbered_prefix_filename(filename)
    if result:
        return result
    result = parse_underscore_separated_filename(filename)
    if result:
        return result
    result = parse_standard_filename(filename)
    if result:
        return result
    return parse_bare_filename(filename)


# ---------------------------------------------------------------------------
# Tag reading / merging
# ---------------------------------------------------------------------------
def read_existing_tags(filepath: str) -> dict:
    """Read useful existing ID3 tags from a file. Returns a dict of tag values."""
    tags = {}
    try:
        mf = MutagenFile(filepath)
        if not mf or not mf.tags:
            return tags
        if Path(filepath).suffix.lower() == ".flac":
            for our_key, keys in {"title": ("title",), "artist": ("artist",),
                    "album_artist": ("albumartist",), "album": ("album",),
                    "grouping": ("grouping", "organization", "label"),
                    "publisher": ("publisher", "organization", "label"),
                    "isrc": ("isrc",), "year": ("date",), "bpm": ("bpm",)}.items():
                for key in keys:
                    values = mf.tags.get(key)
                    if values and str(values[0]).strip():
                        tags[our_key] = str(values[0]).strip()
                        break
            return tags
        tags.update(id3_fields(mf.tags))
    except Exception:
        pass
    return tags


def id3_fields(id3) -> dict:
    """The useful fields of an ID3 tag object, also used for tags read remotely by byte range."""
    tag_map = {
        "TIT2": "title",
        "TPE1": "artist",
        "TPE2": "album_artist",
        "TALB": "album",
        "GRP1": "grouping",
        "TPUB": "publisher",
        "TSRC": "isrc",
        "TDRC": "year",
        "TDRL": "release_date",
        "TCON": "genre",
        "TBPM": "bpm",
        "TRCK": "track",
    }
    tags = {}
    for id3_key, our_key in tag_map.items():
        if id3_key in id3:
            tag = id3[id3_key]
            if hasattr(tag, "text") and tag.text:
                val = str(tag.text[0])
                if val and val.lower() not in ("none", ""):
                    tags[our_key] = val
    # Check for embedded art
    if any("APIC" in str(k) for k in id3):
        tags["has_artwork"] = True
    return tags


def merge_info(parsed: dict, existing_tags: dict) -> dict:
    """
    Merge filename-parsed info with existing tag info.
    Filename takes priority for artist/title when parsed from catalog prefix.
    Existing tags supplement missing info.
    """
    info = dict(parsed)

    # If we couldn't parse an artist from filename, try tags
    if not info.get("artist") and existing_tags.get("artist"):
        info["artist"] = existing_tags["artist"]

    # If title is empty/missing from filename, try tags
    if not info.get("title") and existing_tags.get("title"):
        info["title"] = existing_tags["title"]

    # Label: if not from filename, try tag grouping or publisher
    if not info.get("label") or info["label"].startswith("UNKNOWN:"):
        for tag_key in ("grouping", "publisher"):
            if existing_tags.get(tag_key):
                info["label"] = existing_tags[tag_key]
                break

    # Preserve useful metadata from tags
    for key in ("isrc", "year", "release_date", "genre", "bpm", "has_artwork"):
        if existing_tags.get(key):
            info[key] = existing_tags[key]

    # TALB: preserve if it's meaningful (not same as title, no junk)
    if existing_tags.get("album"):
        album = existing_tags["album"]
        title = info.get("title", "")
        # Keep album if it's different from title and not junk
        if album != title and not any(p.search(album) for p in JUNK_TALB_PATTERNS):
            info["original_album"] = album

    return info


# ---------------------------------------------------------------------------
# Tag writing
# ---------------------------------------------------------------------------
def clean_talb(info: dict) -> str:
    """Determine the best TALB value."""
    if info.get("original_album"):
        album = info["original_album"]
        # Clean remaining junk from album
        for pattern in JUNK_TALB_PATTERNS:
            album = pattern.sub("", album).strip()
        # Remove trailing/leading separators
        album = album.strip(" -\u2013\u2014")
        if album:
            return album
    # Fallback: use title without mix type
    title = info.get("title", "")
    # Strip (Extended Mix) etc. from album name for cleaner look
    m = re.match(r"^(.+?)\s*\(.*$", title)
    if m:
        return m.group(1).strip()
    return title


def write_tags(filepath: str, info: dict) -> list:
    """
    Write/update ID3 tags on a file. Returns list of issues encountered.
    Handles MP3 and AIFF/AIF files.
    v3: Uses mf.save() instead of mf.tags.save() to fix AIFF fresh-tag bug.
    """
    issues = []
    ext = Path(filepath).suffix.lower()
    try:
        # v3: For AIFF files, use AIFF() directly for reliable tag handling
        if ext in (".aif", ".aiff"):
            audio = AIFF(filepath)
            if audio.tags is None:
                audio.add_tags()
            tags = audio.tags

            # --- CLEANUP PHASE ---
            for junk_key in JUNK_TAG_KEYS:
                if junk_key in tags:
                    tags.delall(junk_key)
            for ck in [k for k in tags if str(k).startswith("COMM")]:
                tags.delall(str(ck))
            for uk in [k for k in tags if str(k).startswith("USLT")]:
                tag = tags[uk]
                val = str(tag) if not hasattr(tag, "text") else str(tag.text[0]) if tag.text else ""
                if not val.strip():
                    tags.delall(str(uk))
            if "TCON" in tags:
                genre_tag = tags["TCON"]
                genre_val = str(genre_tag.text[0]).lower() if hasattr(genre_tag, "text") and genre_tag.text else ""
                if genre_val in ("other", "", "unknown", "none"):
                    tags.delall("TCON")

            # --- WRITE PHASE ---
            artist = info.get("artist", "")
            title = info.get("title", "")
            label = info.get("label", "")

            if title:
                title = apply_typo_corrections(title)
                tags.delall("TIT2")
                tags.add(TIT2(encoding=3, text=[title]))
            if artist:
                tags.delall("TPE1")
                tags.add(TPE1(encoding=3, text=[artist]))
                tags.delall("TPE2")
                tags.add(TPE2(encoding=3, text=[artist]))
            if label and not label.startswith("UNKNOWN:"):
                tags.delall("GRP1")
                tags.add(GRP1(encoding=3, text=[label]))
                tags.delall("TPUB")
                tags.add(TPUB(encoding=3, text=[label]))

            talb_val = clean_talb(info)
            if talb_val:
                tags.delall("TALB")
                tags.add(TALB(encoding=3, text=[talb_val]))

            # v3: Use audio.save() — NOT audio.tags.save() — to avoid
            # TypeError on freshly created AIFF ID3 chunks
            audio.save()

        else:
            # MP3 and other formats: use MutagenFile as before
            mf = MutagenFile(filepath)
            if mf is None:
                return [f"Cannot open file for tagging: {filepath}"]

            if mf.tags is None:
                try:
                    mf.add_tags()
                except Exception:
                    pass
            if mf.tags is None:
                return [f"Cannot create tags for: {filepath}"]

            # --- CLEANUP PHASE ---
            for junk_key in JUNK_TAG_KEYS:
                if junk_key in mf.tags:
                    mf.tags.delall(junk_key)
            for ck in [k for k in mf.tags if str(k).startswith("COMM")]:
                mf.tags.delall(str(ck))
            for uk in [k for k in mf.tags if str(k).startswith("USLT")]:
                tag = mf.tags[uk]
                val = str(tag) if not hasattr(tag, "text") else str(tag.text[0]) if tag.text else ""
                if not val.strip():
                    mf.tags.delall(str(uk))
            if "TCON" in mf.tags:
                genre_tag = mf.tags["TCON"]
                genre_val = str(genre_tag.text[0]).lower() if hasattr(genre_tag, "text") and genre_tag.text else ""
                if genre_val in ("other", "", "unknown", "none"):
                    mf.tags.delall("TCON")

            # --- WRITE PHASE ---
            artist = info.get("artist", "")
            title = info.get("title", "")
            label = info.get("label", "")

            if title:
                title = apply_typo_corrections(title)
                mf.tags.delall("TIT2")
                mf.tags.add(TIT2(encoding=3, text=[title]))
            if artist:
                mf.tags.delall("TPE1")
                mf.tags.add(TPE1(encoding=3, text=[artist]))
                mf.tags.delall("TPE2")
                mf.tags.add(TPE2(encoding=3, text=[artist]))
            if label and not label.startswith("UNKNOWN:"):
                mf.tags.delall("GRP1")
                mf.tags.add(GRP1(encoding=3, text=[label]))
                mf.tags.delall("TPUB")
                mf.tags.add(TPUB(encoding=3, text=[label]))

            talb_val = clean_talb(info)
            if talb_val:
                mf.tags.delall("TALB")
                mf.tags.add(TALB(encoding=3, text=[talb_val]))

            mf.save()

    except Exception as e:
        issues.append(f"Tag write error on {filepath}: {e}")

    return issues


# ---------------------------------------------------------------------------
# Post-processing tag verification (A9 — runs in script, saves AI tokens)
# ---------------------------------------------------------------------------
REQUIRED_TAGS = ["TIT2", "TPE1", "TPE2", "TALB"]
LABEL_TAGS = ["GRP1", "TPUB"]


def _get_tag_value(tags, key):
    """Extract a string value from a tag, handling both ID3 and Vorbis comment formats."""
    if key not in tags:
        return None
    tag = tags[key]
    # ID3 tags have .text attribute
    if hasattr(tag, "text") and tag.text:
        return str(tag.text[0]).strip()
    # Vorbis comments (FLAC) are plain lists of strings
    if isinstance(tag, list) and tag:
        return str(tag[0]).strip()
    return str(tag).strip() if tag else None


def verify_tags(filepath: str, has_label: bool) -> list:
    """
    Verify that a processed file has all required tags.
    Handles ID3 (MP3/AIFF) and Vorbis comments (FLAC).
    Returns list of issue strings (empty = all good).
    """
    issues = []
    fname = os.path.basename(filepath)
    ext = Path(filepath).suffix.lower()
    try:
        mf = MutagenFile(filepath)
        if mf is None:
            return [f"{fname}: Cannot open file for verification"]
        if mf.tags is None:
            return [f"{fname}: NO TAGS AT ALL"]

        # FLAC uses Vorbis comments with different key names
        if ext == ".flac":
            vorbis_required = {"title": "title", "artist": "artist", "album": "album"}
            vorbis_label = {"organization": "label"}  # Vorbis uses ORGANIZATION for label/publisher

            for vkey, desc in vorbis_required.items():
                val = _get_tag_value(mf.tags, vkey)
                if not val:
                    issues.append(f"{fname}: missing/empty {desc} (Vorbis: {vkey})")

            if has_label:
                # FLAC doesn't have GRP1/TPUB equivalents — check organization or publisher
                label_found = False
                for vkey in ("organization", "publisher", "label"):
                    val = _get_tag_value(mf.tags, vkey)
                    if val:
                        label_found = True
                        break
                if not label_found:
                    issues.append(f"{fname}: missing label tag (Vorbis)")
        else:
            # ID3 tags (MP3, AIFF)
            for tag_key in REQUIRED_TAGS:
                val = _get_tag_value(mf.tags, tag_key)
                if val is None:
                    issues.append(f"{fname}: missing {tag_key}")
                elif not val:
                    issues.append(f"{fname}: empty {tag_key}")

            # Check label tags if file should have a label
            if has_label:
                for tag_key in LABEL_TAGS:
                    val = _get_tag_value(mf.tags, tag_key)
                    if val is None:
                        issues.append(f"{fname}: missing {tag_key}")
                    elif not val:
                        issues.append(f"{fname}: empty {tag_key}")

            # Check TPE1 == TPE2
            tpe1 = _get_tag_value(mf.tags, "TPE1")
            tpe2 = _get_tag_value(mf.tags, "TPE2")
            if tpe1 and tpe2 and tpe1 != tpe2:
                issues.append(f"{fname}: TPE1 ({tpe1}) != TPE2 ({tpe2})")

            # Check GRP1 == TPUB
            grp1 = _get_tag_value(mf.tags, "GRP1")
            tpub = _get_tag_value(mf.tags, "TPUB")
            if grp1 and tpub and grp1 != tpub:
                issues.append(f"{fname}: GRP1 ({grp1}) != TPUB ({tpub})")

            # Check for lingering COMM junk
            for k in mf.tags:
                if str(k).startswith("COMM"):
                    val = str(mf.tags[k])
                    issues.append(f"{fname}: residual COMM tag: {val[:60]}")

    except Exception as e:
        issues.append(f"{fname}: verification error: {e}")

    return issues


# ---------------------------------------------------------------------------
# Format conversion
# ---------------------------------------------------------------------------
def convert_wav_to_aiff(wav_path: str, output_dir: str) -> tuple:
    """Convert WAV to AIFF using ffmpeg. Returns (path to new AIFF file, error or None)."""
    stem = Path(wav_path).stem
    aiff_path = os.path.join(output_dir, stem + ".aiff")
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_name,bits_per_sample,bits_per_raw_sample", "-of", "json", wav_path],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            return None, "Cannot determine source precision; WAV conversion stopped"
        streams = json.loads(probe.stdout).get("streams", [])
        if not streams:
            return None, "No audio stream found in WAV"
        stream = streams[0]
        source_codec = stream.get("codec_name", "")
        if source_codec in {"pcm_f32le", "pcm_f32be"}:
            target_codec = "pcm_f32be"
        elif source_codec in {"pcm_f64le", "pcm_f64be"}:
            target_codec = "pcm_f64be"
        else:
            bits = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0)
            if bits not in {8, 16, 24, 32}:
                return None, f"Unsupported WAV precision: {bits}; source retained"
            target_codec = f"pcm_s{max(bits, 16)}be"
        result = subprocess.run(
            [
                "ffmpeg", "-i", wav_path,
                "-c:a", target_codec,
                "-f", "aiff",
                "-y",  # overwrite
                aiff_path,
            ],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minutes — some master WAVs at 96kHz/24-bit can be 500+ MB
        )
        if result.returncode != 0:
            return None, f"ffmpeg error: {result.stderr[:200]}"
        return aiff_path, None
    except FileNotFoundError:
        return None, "ffmpeg not installed (required for WAV->AIFF conversion)"
    except subprocess.TimeoutExpired:
        return None, "ffmpeg timed out (file may be very large)"
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# Archive extraction
# ---------------------------------------------------------------------------
def open_rar(archive_path):
    """A RarFile that extracts through 7-Zip (PATH or Program Files), else unrar or bsdtar when present."""
    import rarfile
    for folder in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        candidate = Path(folder or "") / "7-Zip" / "7z.exe"
        if folder and not shutil.which("7z") and candidate.exists():
            rarfile.SEVENZIP_TOOL = str(candidate)
            rarfile.tool_setup(force=True)
            break
    return rarfile.RarFile(archive_path)


def safe_member(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return not (name.startswith(("/", "\\")) or any(p in {"..", "."} or ":" in p for p in parts))


def extract_archive(archive_path: str, extract_to: str) -> tuple:
    """Extract ZIP or RAR, return (list of extracted audio file paths, error string or None)."""
    ext = Path(archive_path).suffix.lower()
    extracted = []
    error = None
    try:
        if ext == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(extract_to)
        elif ext == ".rar":
            # 2026-10-24: this called unrar, which Windows does not have; 7-Zip reads RAR through rarfile.
            with open_rar(archive_path) as rf:
                members = [m for m in rf.infolist() if not m.is_dir()]
                if not all(safe_member(m.filename) for m in members):
                    raise ValueError("unsafe member path")
                for member in members:
                    if Path(member.filename).suffix.lower() in AUDIO_EXTENSIONS:
                        rf.extract(member, extract_to)
        # Collect extracted audio files
        for root, dirs, files in os.walk(extract_to):
            for fname in files:
                fext = Path(fname).suffix.lower()
                if fext in AUDIO_EXTENSIONS:
                    extracted.append(os.path.join(root, fname))
    except ImportError:
        error = "rarfile is not installed (needed for .rar files). Run install.py, or: pip install rarfile"
    except FileNotFoundError:
        error = "No RAR extractor found (needed for .rar files). Install 7-Zip with: winget install 7zip.7zip"
    except zipfile.BadZipFile:
        error = f"Corrupt or invalid ZIP: {os.path.basename(archive_path)}"
    except Exception as e:
        error = str(e)
    return extracted, error


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------
def normalize_for_comparison(text: str) -> str:
    """Normalize artist/title for duplicate comparison."""
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"[^\w\s]", "", t)  # Remove punctuation
    t = re.sub(r"\s+", " ", t).strip()
    return t


def get_duration(filepath: str) -> float:
    """
    Get audio duration in seconds. Returns -1.0 if unreadable.
    v3: Falls back to ffprobe when mutagen fails (fixes WAV duration detection).
    """
    try:
        mf = MutagenFile(filepath)
        if mf and mf.info and mf.info.length > 0:
            return round(mf.info.length, 1)
    except Exception:
        pass

    # v3: ffprobe fallback for WAV and other files mutagen can't read duration for
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                filepath,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return round(float(result.stdout.strip()), 1)
    except Exception:
        pass

    return -1.0


def detect_duplicates(file_infos: list) -> list:
    """
    Detect duplicates among processed files.
    Same mix = same normalized artist+title AND duration within 2 seconds.
    Different mixes (different duration) are always kept.
    v3: When both durations are unknown, flag as potential duplicate for manual check.
    Returns list of duplicate groups.
    """
    groups = defaultdict(list)
    for fi in file_infos:
        key = (
            normalize_for_comparison(fi.get("artist", "")),
            normalize_for_comparison(fi.get("title", "")),
        )
        groups[key].append(fi)

    duplicates = []
    for key, items in groups.items():
        if len(items) < 2:
            continue
        # Within each group, find items with similar duration (same mix)
        same_mix_groups = []
        used = set()
        for i, a in enumerate(items):
            if i in used:
                continue
            group = [a]
            used.add(i)
            for j, b in enumerate(items):
                if j in used:
                    continue
                dur_a = a.get("duration", -1)
                dur_b = b.get("duration", -1)
                # v3: If both durations unknown, treat as potential duplicate
                if dur_a < 0 and dur_b < 0:
                    group.append(b)
                    used.add(j)
                    continue
                # Skip comparison if only one duration is unknown
                if dur_a < 0 or dur_b < 0:
                    continue
                if abs(dur_a - dur_b) <= 2.0:
                    group.append(b)
                    used.add(j)
            if len(group) > 1:
                same_mix_groups.append(group)
        duplicates.extend(same_mix_groups)
    return duplicates


# ---------------------------------------------------------------------------
# Filename construction
# ---------------------------------------------------------------------------
def build_target_filename(info: dict, ext: str) -> str:
    """Build: Artist - Title (Mix) [Label].ext"""
    artist = info.get("artist") or "Unknown Artist"
    title = info.get("title") or "Unknown Title"
    label = info.get("label") or ""

    title = apply_typo_corrections(title) if title else title

    # Sanitize for filesystem
    def sanitize(s):
        if not s:
            return s
        s = s.replace("/", "-").replace("\\", "-")
        s = s.replace(":", "-").replace("*", "").replace("?", "")
        s = s.replace('"', "'").replace("<", "").replace(">", "")
        s = s.replace("|", "-")
        s = re.sub(r"\s{2,}", " ", s).strip()
        return s

    artist = sanitize(artist)
    title = sanitize(title)

    if label and not label.startswith("UNKNOWN:"):
        label = sanitize(label)
        filename = f"{artist} - {title} [{label}]{ext}"
    else:
        filename = f"{artist} - {title}{ext}"

    # Fix double brackets: [Label] [Label] -> [Label]
    filename = re.sub(r"\[([^\]]+)\]\s*\[(\1)\]", r"[\1]", filename)

    # Fix mix type in brackets: [Extended Mix] -> (Extended Mix)
    # Only if it's NOT the label bracket at the end
    def fix_mix_brackets(m):
        content = m.group(1)
        if re.match(r"(?:Extended|Original|Radio|Dub|Club|Instrumental|Vocal)\s+(?:Mix|Remix|Edit)", content, re.I):
            return f"({content})"
        return m.group(0)

    # Apply only to brackets NOT at end (before label)
    parts = filename.rsplit("[", 1)
    if len(parts) == 2:
        before_label = parts[0]
        before_label = re.sub(r"\[([^\]]+)\]", fix_mix_brackets, before_label)
        filename = before_label + "[" + parts[1]

    # Fix doubled artist name: "Artist - Artist - Title" -> "Artist - Title"
    m = re.match(r"^(.+?) - \1 - (.+)$", filename)
    if m:
        filename = f"{m.group(1)} - {m.group(2)}"

    return filename


# ---------------------------------------------------------------------------
# Main processing pipeline
# ---------------------------------------------------------------------------
def process_folder(
    input_folder: str,
    label_csv: str,
    output_folder: str = None,
    dry_run: bool = False,
    force: bool = False,
    staging_dir: str = None,
) -> dict:
    """
    Process all promo files in input_folder.
    Returns a report dict with results, unknowns, duplicates, and tag_issues.

    With force=False (default), existing files in output_folder are skipped,
    making re-runs safe and idempotent.
    """
    label_codes = load_label_codes(label_csv)

    report = {
        "processed": [],
        "unknowns": [],
        "duplicates": [],
        "errors": [],
        "skipped": [],
        "archives_extracted": [],
        "tag_issues": [],  # v3: post-processing tag verification results
        "new_label_codes": {},
        "stats": {
            "total_input": 0,
            "total_audio_files": 0,
            "total_archives": 0,
            "files_from_archives": 0,
            "total_after_extraction": 0,
            "total_output_files": 0,
            "processed_ok": 0,
            "unknowns_count": 0,
            "duplicates_count": 0,
            "converted_wav_to_aiff": 0,
            "normalized_aif_to_aiff": 0,
            "archives_extracted": 0,
            "skipped_count": 0,
            "master_suffixes_stripped": 0,
            "mix_types_normalized": 0,
            "ft_normalized": 0,
            "tag_issues_count": 0,
            "by_format": {"mp3": 0, "aiff": 0, "flac": 0, "other": 0},
        },
    }

    if output_folder and not dry_run:
        os.makedirs(output_folder, exist_ok=True)

    # v3 (A1): Staging dir defaults to <output_folder>/.staging/ instead of /tmp
    if staging_dir is None:
        if output_folder:
            staging_dir = os.path.join(output_folder, ".staging")
        else:
            staging_dir = os.path.join(input_folder, ".staging")
    os.makedirs(staging_dir, exist_ok=True)

    all_files = []
    archive_files = []

    # Collect files
    for fname in sorted(os.listdir(input_folder)):
        fpath = os.path.join(input_folder, fname)
        if not os.path.isfile(fpath):
            continue
        ext = Path(fname).suffix.lower()
        if ext in ARCHIVE_EXTENSIONS:
            archive_files.append(fpath)
        elif ext in AUDIO_EXTENSIONS:
            all_files.append(fpath)

    initial_audio_count = len(all_files)
    report["stats"]["total_audio_files"] = initial_audio_count
    report["stats"]["total_archives"] = len(archive_files)
    report["stats"]["total_input"] = initial_audio_count + len(archive_files)

    # Extract archives
    for archive_path in archive_files:
        archive_name = os.path.basename(archive_path)
        extract_dir = os.path.join(staging_dir, Path(archive_name).stem)
        os.makedirs(extract_dir, exist_ok=True)
        extracted, extract_error = extract_archive(archive_path, extract_dir)
        if extracted:
            all_files.extend(extracted)
            report["archives_extracted"].append({
                "archive": archive_name,
                "files_extracted": [os.path.basename(f) for f in extracted],
            })
            report["stats"]["archives_extracted"] += 1
            report["stats"]["files_from_archives"] += len(extracted)
        if extract_error:
            report["errors"].append(f"Archive '{archive_name}': {extract_error}")
        elif not extracted:
            report["errors"].append(f"No audio files found in: {archive_name}")

    report["stats"]["total_after_extraction"] = len(all_files)

    # Process each audio file
    file_infos = []
    total_files = len(all_files)
    for idx, fpath in enumerate(all_files):
        fname = os.path.basename(fpath)
        ext = Path(fname).suffix.lower()

        print(f"[{idx+1}/{total_files}] Parsing: {fname}", file=sys.stderr)

        # Parse filename
        parsed = parse_filename(fname, label_codes)

        # v3: Track normalization stats by checking if parsing changed things
        stem = Path(fname).stem
        parsed_title = parsed.get("title", "") or ""
        parsed_artist = parsed.get("artist", "") or ""
        if re.search(r"(?:MASTER|mastered|NEWFIXFIX)", stem, re.I) and not re.search(r"(?:MASTER|mastered|NEWFIXFIX)", parsed_title, re.I):
            report["stats"]["master_suffixes_stripped"] += 1
        if re.search(r"\[(?:Extended|Original|Radio)\]|\(Extended\s+Master\)", stem, re.I):
            report["stats"]["mix_types_normalized"] += 1
        if re.search(r"\bft[.\s]", stem, re.I) and "feat." in parsed_artist:
            report["stats"]["ft_normalized"] += 1

        # Read existing tags
        existing_tags = read_existing_tags(fpath)

        # Merge
        info = merge_info(parsed, existing_tags)
        info["original_filename"] = fname
        info["original_path"] = fpath
        info["duration"] = get_duration(fpath)

        # v3 (A2): Normalize .aif -> .aiff
        target_ext = ext
        needs_conversion = False
        if ext == ".wav":
            target_ext = ".aiff"
            needs_conversion = True
        elif ext == ".aif":
            target_ext = ".aiff"
            report["stats"]["normalized_aif_to_aiff"] += 1

        info["target_ext"] = target_ext
        info["needs_conversion"] = needs_conversion

        # Track format stats
        if target_ext in (".aif", ".aiff"):
            report["stats"]["by_format"]["aiff"] += 1
        elif target_ext == ".mp3":
            report["stats"]["by_format"]["mp3"] += 1
        elif target_ext == ".flac":
            report["stats"]["by_format"]["flac"] += 1
        else:
            report["stats"]["by_format"]["other"] += 1

        # Build target filename
        target_name = build_target_filename(info, target_ext)
        info["target_filename"] = target_name

        # Classify: is this an unknown?
        is_unknown = False
        unknown_reasons = []
        if not info.get("artist"):
            is_unknown = True
            unknown_reasons.append("missing_artist")
        if not info.get("label") or (isinstance(info.get("label"), str) and info["label"].startswith("UNKNOWN:")):
            is_unknown = True
            unknown_reasons.append("missing_label")
        if not info.get("title"):
            is_unknown = True
            unknown_reasons.append("missing_title")

        info["is_unknown"] = is_unknown
        info["unknown_reasons"] = unknown_reasons

        file_infos.append(info)

    # Detect duplicates
    duplicates = detect_duplicates(file_infos)
    for dup_group in duplicates:
        report["duplicates"].append({
            "tracks": [
                {
                    "filename": d["original_filename"],
                    "artist": d.get("artist"),
                    "title": d.get("title"),
                    "duration": d.get("duration"),
                    "ext": Path(d["original_filename"]).suffix,
                }
                for d in dup_group
            ],
            "recommendation": "keep_highest_quality",
        })
    report["stats"]["duplicates_count"] = len(duplicates)

    # Now do the actual processing (unless dry run)
    for idx, info in enumerate(file_infos):
        if info["is_unknown"]:
            report["unknowns"].append({
                "original_filename": info["original_filename"],
                "parsed_artist": info.get("artist"),
                "parsed_title": info.get("title"),
                "parsed_label": info.get("label"),
                "existing_tags": {
                    k: v for k, v in read_existing_tags(info["original_path"]).items()
                    if k != "has_artwork"
                },
                "unknown_reasons": info["unknown_reasons"],
                "duration": info.get("duration"),
            })
            report["stats"]["unknowns_count"] += 1
            # Still process what we can
            if not dry_run and output_folder:
                _do_file_processing(info, output_folder, staging_dir, report, force=force)
            continue

        if dry_run:
            report["processed"].append({
                "original": info["original_filename"],
                "target": info["target_filename"],
                "artist": info.get("artist"),
                "title": info.get("title"),
                "label": info.get("label"),
                "converted": info.get("needs_conversion", False),
            })
            report["stats"]["processed_ok"] += 1
        else:
            if output_folder:
                _do_file_processing(info, output_folder, staging_dir, report, force=force)

    # v3 (A9): Post-processing tag verification — runs entirely in-script
    if not dry_run and output_folder:
        print("\n--- Tag Verification Pass ---", file=sys.stderr)
        output_files = [
            os.path.join(output_folder, f) for f in os.listdir(output_folder)
            if Path(f).suffix.lower() in {".mp3", ".aif", ".aiff", ".flac"}
        ]
        # Build a set of files we know have labels for the check
        files_with_labels = set()
        for info in file_infos:
            label = info.get("label", "")
            if label and not label.startswith("UNKNOWN:"):
                files_with_labels.add(info.get("target_filename", ""))

        for fpath in sorted(output_files):
            fname = os.path.basename(fpath)
            has_label = fname in files_with_labels
            issues = verify_tags(fpath, has_label)
            if issues:
                report["tag_issues"].extend(issues)

        report["stats"]["tag_issues_count"] = len(report["tag_issues"])
        if report["tag_issues"]:
            print(f"  Tag issues found: {len(report['tag_issues'])}", file=sys.stderr)
            for issue in report["tag_issues"]:
                print(f"    - {issue}", file=sys.stderr)
        else:
            print(f"  All {len(output_files)} files verified OK", file=sys.stderr)

    # Count total output files
    if output_folder and os.path.isdir(output_folder):
        report["stats"]["total_output_files"] = len([
            f for f in os.listdir(output_folder)
            if Path(f).suffix.lower() in {".mp3", ".aif", ".aiff", ".flac"}
            and os.path.isfile(os.path.join(output_folder, f))
        ])

    # Cleanup staging
    try:
        shutil.rmtree(staging_dir, ignore_errors=True)
    except Exception:
        pass

    # v3 (A7): Print human-readable summary to stderr
    s = report["stats"]
    print(f"\n{'='*50}", file=sys.stderr)
    print(f"PROCESSING SUMMARY", file=sys.stderr)
    print(f"{'='*50}", file=sys.stderr)
    print(f"Input:  {s['total_audio_files']} audio + {s['total_archives']} archives "
          f"({s['files_from_archives']} files extracted) = {s['total_after_extraction']} total", file=sys.stderr)
    print(f"Output: {s['total_output_files']} files "
          f"(MP3: {s['by_format']['mp3']}, AIFF: {s['by_format']['aiff']}, "
          f"FLAC: {s['by_format']['flac']})", file=sys.stderr)
    print(f"Converted WAV->AIFF: {s['converted_wav_to_aiff']}", file=sys.stderr)
    if s['normalized_aif_to_aiff'] > 0:
        print(f"Normalized .aif->.aiff: {s['normalized_aif_to_aiff']}", file=sys.stderr)
    print(f"Unknowns: {s['unknowns_count']} | Duplicates: {s['duplicates_count']} | "
          f"Skipped: {s['skipped_count']}", file=sys.stderr)
    if s['tag_issues_count'] > 0:
        print(f"TAG ISSUES: {s['tag_issues_count']} (check report for details)", file=sys.stderr)
    else:
        print(f"Tag verification: ALL CLEAN", file=sys.stderr)
    if report["errors"]:
        print(f"Errors: {len(report['errors'])}", file=sys.stderr)
    print(f"{'='*50}", file=sys.stderr)

    return report


def _do_file_processing(info: dict, output_folder: str, staging_dir: str, report: dict, force: bool = False):
    """Actually convert/copy/tag a single file."""
    fpath = info["original_path"]
    target_name = info["target_filename"]
    target_path = os.path.join(output_folder, target_name)

    # Skip-existing: if the target already exists with size > 0, skip it.
    # This makes re-runs safe — no duplicates, no " 2" collision files.
    if not force and os.path.exists(target_path) and os.path.getsize(target_path) > 0:
        report["skipped"].append({
            "original": info["original_filename"],
            "target": target_name,
            "reason": "already exists in output folder",
            "artist": info.get("artist"),
            "title": info.get("title"),
            "label": info.get("label"),
        })
        report["stats"]["skipped_count"] += 1
        print(f"  Skipped (exists): {target_name}", file=sys.stderr)
        return

    # If force mode and collision exists, use counter suffix
    if force and os.path.exists(target_path) and os.path.abspath(fpath) != os.path.abspath(target_path):
        stem, ext = os.path.splitext(target_name)
        counter = 2
        while os.path.exists(target_path):
            target_name = f"{stem} {counter}{ext}"
            target_path = os.path.join(output_folder, target_name)
            counter += 1
        info["target_filename"] = target_name
        report["errors"].append(f"Filename collision resolved: '{info['original_filename']}' saved as '{target_name}'")

    try:
        if info.get("needs_conversion"):
            # Convert WAV -> AIFF
            print(f"  Converting WAV->AIFF: {info['original_filename']}", file=sys.stderr)
            converted, conv_error = convert_wav_to_aiff(fpath, staging_dir)
            if converted:
                shutil.copy2(converted, target_path)
                report["stats"]["converted_wav_to_aiff"] += 1
            else:
                report["errors"].append(f"WAV->AIFF failed for '{info['original_filename']}': {conv_error}")
                return
        else:
            # Copy file (v3: .aif files get copied with .aiff target name automatically
            # because target_ext was already set to .aiff in the main loop)
            shutil.copy2(fpath, target_path)

        # Write tags
        tag_issues = write_tags(target_path, info)
        if tag_issues:
            report["errors"].extend(tag_issues)

        report["processed"].append({
            "original": info["original_filename"],
            "target": target_name,
            "artist": info.get("artist"),
            "title": info.get("title"),
            "label": info.get("label"),
            "converted": info.get("needs_conversion", False),
        })
        report["stats"]["processed_ok"] += 1
        print(f"  Done: {target_name}", file=sys.stderr)

    except Exception as e:
        report["errors"].append(f"Processing error for {info['original_filename']}: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Process DJ promo music files (v3)")
    parser.add_argument("input_folder", help="Path to folder with unprocessed promos")
    parser.add_argument(
        "--label-csv",
        default=None,
        help="Path to label_codes.csv. If not specified, searches working dirs, input folder, then bundled copy.",
    )
    parser.add_argument(
        "--search-dir",
        action="append",
        default=[],
        help="Additional directories to search recursively for label_codes.csv (can specify multiple times).",
    )
    parser.add_argument("--output-folder", default=None, help="Output folder for processed files")
    parser.add_argument(
        "--staging-dir",
        default=None,
        help="Directory for temporary conversion staging. Default: <output-folder>/.staging/",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse and report only, don't modify files")
    parser.add_argument("--force", action="store_true", help="Force re-processing even if output files exist (disables skip-existing)")
    parser.add_argument("--report", default=None, help="Path to write JSON report (default: stdout)")
    parser.add_argument(
        "--save-label-csv",
        default=None,
        help="Path to save updated label_codes.csv after processing. Use 'auto' to save back to where it was found.",
    )

    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Build search dirs: CWD + any explicitly provided dirs
    search_dirs = [os.getcwd()] + args.search_dir

    # Find label_codes.csv
    label_csv, csv_source = find_label_csv(
        explicit_path=args.label_csv,
        search_dirs=search_dirs,
        input_folder=args.input_folder,
        script_dir=script_dir,
    )

    if label_csv:
        print(f"Using label codes from: {label_csv} ({csv_source})", file=sys.stderr)
    else:
        print("WARNING: label_codes.csv not found. Labels with catalog prefixes will be marked UNKNOWN for AI research.", file=sys.stderr)
        label_csv = ""

    report = process_folder(
        input_folder=args.input_folder,
        label_csv=label_csv,
        output_folder=args.output_folder,
        dry_run=args.dry_run,
        force=args.force,
        staging_dir=args.staging_dir,
    )

    # Handle --save-label-csv
    if args.save_label_csv and label_csv:
        save_path = label_csv if args.save_label_csv == "auto" else args.save_label_csv
        # Merge any new codes from the report
        codes = load_label_codes(label_csv)
        if report.get("new_label_codes"):
            codes.update(report["new_label_codes"])
        try:
            save_label_codes(codes, save_path)
            print(f"Label codes saved to: {save_path}", file=sys.stderr)
        except Exception as e:
            print(f"WARNING: Could not save label codes to {save_path}: {e}", file=sys.stderr)

    # Output report
    report_json = json.dumps(report, indent=2, ensure_ascii=False)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report_json)
        print(f"Report written to: {args.report}", file=sys.stderr)
    else:
        print(report_json)


if __name__ == "__main__":
    main()
