"""Evidence-backed per-recording metadata resolutions, independent of promo senders."""
import json
from urllib.parse import urlsplit

from promo_automation import STATE

# Promo services, PR agencies, groups and placeholders: they send promos but are never the imprint.
UNTRUSTED_LABELS = {"whoa!", "spin me pr", "khb music promotion", "tba", "tbc", "high emotions music group",
                    "like that underground", "hey! this is brad", "1001tracklists", "reaktion dj promo",
                    "alikoto music consult", "exclusive promo", "promo cloud", "promoly", "inflyte", "ampsuite", "unknown", "n/a"}
# Distributors name no imprint; a release that only names them is filed as 'Self Released'.
DISTRIBUTORS = {"distrokid", "landr", "self-released", "igroovemusic", "igroove", "cd baby",
                "tunecore", "amuse", "routenote", "ditto music", "ditto", "awal", "onerpm", "soundrop", "unitedmasters"}
SELF_RELEASED = "Self Released"
# Evidence schemes: public pages, the announcement mail (message id) or a known catalog prefix.
EVIDENCE_SCHEMES = ("https", "mail", "catalog")


def resolutions(root=STATE):
    path = root / "metadata-resolutions.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("resolutions", [])


_BY_KEY = {}


def _resolutions_by_key(root):
    """mix_key -> entries in file order, reread only when the file changes (it is 175 KB and asked per track)."""
    from promo_pipeline import mix_key
    path = root / "metadata-resolutions.json"
    try:
        info = path.stat()
        stamp = (info.st_mtime_ns, info.st_size)
    except FileNotFoundError:
        stamp = None
    cached = _BY_KEY.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    index = {}
    for entry in resolutions(root) if stamp else []:
        for key in {mix_key(n) for n in entry.get("original_names", []) + [entry["artist"] + " - " + entry["title"]]}:
            index.setdefault(key, []).append(entry)
    _BY_KEY[str(path)] = (stamp, index)
    return index


def find_resolution(name, root=STATE):
    from promo_pipeline import mix_key
    for entry in _resolutions_by_key(root).get(mix_key(name), ()):
        if not entry.get("approved_at") or not entry.get("label") or not entry.get("sources"):
            continue
        if not all(urlsplit(u).scheme in EVIDENCE_SCHEMES for u in entry["sources"]):
            continue
        return entry
    return None


def apply_resolution(track, root=STATE):
    entry = find_resolution(track["name"], root)
    if entry is None:
        return False
    track.setdefault("original_name", track["name"])
    track.update(artist=entry["artist"], expected_title=entry["title"], label=entry["label"],
                 name=entry["artist"] + " - " + entry["title"], metadata_resolution=entry)
    return True


def distributor_only(label):
    parts = [p.strip().casefold() for p in (label or "").split(",") if p.strip()]
    known = DISTRIBUTORS | {SELF_RELEASED.casefold()}
    return bool(parts) and all(p in known for p in parts) and label.strip() != SELF_RELEASED


def label_issues(label):
    if not label or label.strip().casefold() in UNTRUSTED_LABELS or distributor_only(label):
        return ["Actual track imprint requires AI research; sender/group/placeholder is not an imprint"]
    return []
