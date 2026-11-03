"""Availability is a source outcome; network, parsing and transfer errors remain failures."""
import re
from urllib.parse import urlsplit


class ReleaseUnavailable(RuntimeError):
    """The recipient metadata endpoint confirms that this offer cannot be obtained."""


class AccessRequired(RuntimeError):
    """A share exists but this recipient lacks access; never infer deletion."""


class NotARelease(RuntimeError):
    """A footer/profile link is not a track or campaign download offer."""


def soundcloud_profile(url):
    # Older mail extraction could concatenate HTML and another link. Match the adapter's cleanup.
    cleaned = url.split("<br>")[-1].strip().rstrip(").,;:!?'\"")
    if not cleaned.startswith("http"):
        cleaned = "https://" + cleaned
    parsed = urlsplit(cleaned)
    return ((parsed.hostname or "").lower() in {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}
            and len([part for part in parsed.path.split("/") if part]) < 2)


def dropbox_metadata_error(error):
    """Only called for share inventory, never for an audio transfer or CDN response."""
    detail = getattr(error, "error", None)
    if detail is not None:
        if getattr(detail, "is_shared_link_not_found", lambda: False)():
            return ReleaseUnavailable("Dropbox no longer offers this shared link to the recipient")
        if getattr(detail, "is_shared_link_access_denied", lambda: False)():
            return AccessRequired("Dropbox denied access to this shared link")
    return error


def reviewed_state(platform, entry, source_url=None, evidence=None):
    """Conservative review of older reports, retaining their original reason and raw phase files.

    New adapters raise typed outcomes. Legacy 400s, generic 403s, CDN 404s, timeouts and schema
    failures are never relabelled as expired just because of their HTTP status or wording.
    """
    state, reason = entry.get("state"), entry.get("reason", "")
    if state != "exception":
        return state
    if platform == "soundcloud" and (reason == "SoundCloud link is a user, not a track or playlist"
                                     or source_url and soundcloud_profile(source_url)):
        return "ignored_link"
    if platform == "disco" and reason.startswith("Not a Disco recipient share link") and not (
            source_url and (urlsplit(source_url).hostname or "").lower() == "s.disco.ac"):
        return "ignored_link"
    if platform == "google-drive" and reason == "Google Drive link carries no file or folder id (cut off in the mail)":
        return "ignored_link"
    if platform == "promoly" and (reason in {"Campaign is archived, deleted or private",
                                             "Uploader has disabled downloads", "Campaign has expired"}
                                  or re.fullmatch(r"Promoly API answered HTTP (404|410)", reason)
                                  and not entry.get("plan", {}).get("tracks")):
        return "unavailable"
    if platform == "dropbox":
        if "SharedLinkError('shared_link_not_found'" in reason:
            return "unavailable"
        if "SharedLinkError('shared_link_access_denied'" in reason:
            return "access_blocked"
    if platform == "google-drive" and reason.startswith("Google Drive call failed:") and "File not found:" in reason:
        return "unavailable"  # Missing or no longer shared with this account, not proof of deletion.
    if evidence:
        if platform == "ampsuite" and evidence.get("status") == 200 and any(
                "this promotion has expired" in text.lower() for text in evidence.get("availability_text", [])):
            return "unavailable"
        if platform == "label-engine" and evidence.get("status") == 400 and evidence.get("api_body", "").strip() == "[]":
            return "unavailable"
    return state
