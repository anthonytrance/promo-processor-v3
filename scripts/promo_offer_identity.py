"""Read-only content/offer identities, separate from recipient credentials and reminder messages.

Only provider IDs or exact recipient routes prove identity. Subjects never merge releases.
New opaque recipient keys need one metadata inspection before their campaign can be recognised.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


EMAIL_MASTER_PLATFORMS = {"promoly", "ampsuite", "label-engine", "promo-delivery", "promochoons", "promopush",
                          "beatrolo", "needlepool", "label-portal", "hearthis", "wetransfer", "inflyte"}


def _route(url):
    parsed = urlsplit(url)
    query = [(k, v) for k, values in parse_qs(parsed.query, keep_blank_values=True).items()
             if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid"} for v in values]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, urlencode(sorted(query)), ""))


def content_identity(store, platform, source_id):
    row = store.db.execute("SELECT remote_id FROM sources WHERE id=? AND platform=?", (source_id, platform)).fetchone()
    if row is None:
        raise ValueError("Unknown offer source")
    url = store.source_url(source_id)
    parsed, query = urlsplit(url), parse_qs(urlsplit(url).query)
    value = lambda key: (query.get(key) or [""])[0]
    if platform in {"inflyte", "promo-cloud", "promobox"} and row[0]:
        return [platform, "release", row[0]]
    if platform == "label-engine" and all(value(k) for k in ("label_id", "release_id", "blast_id")):
        # check/echeck rotate authorization; a different blast can represent a new file edition.
        return [platform, "edition", value("label_id"), value("release_id"), value("blast_id")]
    if platform == "promo-delivery" and value("r").isdigit():
        return [platform, "release", value("r")]
    if platform == "promochoons" and value("campaign"):
        return [platform, "campaign", value("campaign")]
    if platform == "promopush" and value("track_id").isdigit():
        return [platform, "campaign", value("track_id")]
    if platform == "ctrl-promo" and parsed.path.startswith("/view-promo/"):
        remote = parsed.path.split("/")[2]
        if remote.isdigit():
            return [platform, "release", remote]
    if platform == "promoly":
        from promo_cloud import read_protected_json
        path = store.root / f"promoly-{source_id}.dpapi"
        if path.exists():
            try:
                data = read_protected_json(path)
                campaign = data.get("campaign") or {}
                key = parsed.path.rstrip("/").rsplit("/", 1)[-1]
                if data.get("key") == key and campaign.get("id") and campaign.get("tracks"):
                    return [platform, "campaign", str(campaign["id"])]
            except Exception:
                pass  # Missing/corrupt/unmatched local proof requires normal metadata resolution.
    return [platform, "route", _route(url)]


_IDENTITIES = {}


def _identities(store, platform):
    """Every source identity of one platform, computed once per process and refreshed when sources are added."""
    rows = [row[0] for row in store.db.execute("SELECT id FROM sources WHERE platform=? ORDER BY id", (platform,))]
    key = (str(store.root), platform)
    cached = _IDENTITIES.get(key)
    if cached is None or cached[0] != rows:
        identities = {}
        for sid in rows:
            try:
                identities[sid] = tuple(content_identity(store, platform, sid))
            except Exception:
                identities[sid] = ("unreadable", sid)  # never merged with anything
        cached = _IDENTITIES[key] = (rows, identities)
    return cached[1]


def equivalent_sources(store, platform, source_id):
    """Only proven content aliases, never destructive source/message/feedback merges."""
    identities = _identities(store, platform)
    identity = identities.get(source_id) or tuple(content_identity(store, platform, source_id))
    return [sid for sid, other in identities.items() if other == identity] or [source_id]


def initial_offer_age(store, source_id, platform=None):
    """First known offer of this proven content edition; repeated reminders cannot reset its age.

    A provider-verified content revision may explicitly set content_offered_at. A new opaque key or
    Label Engine blast is a separate edition until its identity has been resolved, not a title match.
    """
    row = store.db.execute("SELECT platform,metadata FROM sources WHERE id=?", (source_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown offer source")
    platform = platform or row[0]
    metadata = json.loads(row[1] or "{}")
    revision_offer = metadata.get("content_offered_at") if metadata.get("content_revision") else None
    if revision_offer:
        try:
            moment = datetime.fromisoformat(revision_offer.replace("Z", "+00:00"))
            moment = moment.replace(tzinfo=moment.tzinfo or timezone.utc)
            return (datetime.now(timezone.utc)-moment).total_seconds()/86400
        except (AttributeError, ValueError, TypeError):
            pass
    moments = []
    for sid in equivalent_sources(store, platform, source_id):
        for (received,) in store.db.execute("SELECT received FROM messages WHERE source_id=?", (sid,)):
            try:
                moment = parsedate_to_datetime(received)
                moments.append(moment.replace(tzinfo=moment.tzinfo or timezone.utc))
            except (ValueError, TypeError, OverflowError):
                continue
    return ((datetime.now(timezone.utc)-min(moments)).total_seconds()/86400) if moments else None
