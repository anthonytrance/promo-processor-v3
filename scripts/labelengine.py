"""Label Engine (label-engine.com) promo-link adapter. The mailed recipient link opens the release without a login.

The mail links `promo.php?label_id=&release_id=&recipient_id=&blast_id=&check=&echeck=`. Loading that page starts a web
session (PHPSESSID); only then does `api.label-engine.com/v1/promo/display/...` answer with the release JSON (label,
catalogue, C-line, tracks, feedback ids). Per-track download keys are only in the page HTML (player `downloadkey`).
Downloads stay locked until the feedback form (favourite track, 0-5 stars, comment) is posted to downloads_handler.php.
A track download is two calls: generate_file.php returns title/path/key, recipient_download.php streams the file.
Links stop working roughly two to three weeks after the blast (API HTTP 400, page "Error 404").
"""
import argparse
import json
import random
import re
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from curl_cffi import requests

from promo_automation import Store, now
from promo_cloud import FEEDBACK_CLOSERS, FEEDBACK_OPENERS, RATING_RANGE, protect_json, read_protected_json
from promo_mail import discover, newest_offer_age
from promo_pipeline import LibraryIndex, collect_release, seconds, version_kind, write_stream

SITE = "https://label-engine.com/"
API = "https://api.label-engine.com/v1/promo/display/{label_id}/{release_id}/{blast_id}/{recipient_id}/{check}/{echeck}"
HOSTS = {"label-engine.com", "www.label-engine.com", "api.label-engine.com"}
LINK_KEYS = ("label_id", "release_id", "blast_id", "recipient_id", "check", "echeck")
_SOURCE = re.compile(r"<md-wavesurfer-source\b([^>]*)>", re.I)
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
_LENGTH = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b")


def labelengine_url(url):
    parsed = urlsplit(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in HOSTS


def link_params(url):
    """The six promo-link parameters, or None for links that are not a recipient promo page."""
    if not labelengine_url(url) or urlsplit(url).path != "/promo.php":
        return None
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    return query if all(query.get(k) for k in LINK_KEYS) else None


def store_label(name, c_line):
    """Label Engine names the imprint "Colorize (Enhanced)" while the C-line and stores say "Colorize"."""
    owner = re.sub(r"^\s*(?:\(c\)|©)?\s*\d{4}\s+", "", c_line or "", flags=re.I).split("/")[0].strip()
    name = (name or "").strip()
    if owner and re.sub(r"\s*\([^()]*\)\s*$", "", name).casefold() == owner.casefold():
        return owner
    return name or owner or None


def page_keys(html):
    """Per-track download keys and the WAV flag from the server-rendered player."""
    keys = {}
    for match in _SOURCE.finditer(html):
        attrs = dict(_ATTR.findall(match.group(1)))
        if attrs.get("trackid") and attrs.get("downloadkey"):
            keys[attrs["trackid"]] = {"key": attrs["downloadkey"], "wav": attrs.get("wav") == "1",
                                      "duration": attrs.get("duration")}
    return keys


def parse_release(data, html, params):
    keys = page_keys(html)
    label = store_label(data["label"]["NAME"], data.get("C_LINE"))
    tracks = []
    for item in data.get("tracks") or []:
        track_id = str(item["TRACK_ID"])
        page = keys.get(track_id) or {}
        name = unescape(item.get("TRACK_TITLE") or f"{item['ARTIST']} - {item['TITLE']}").strip()
        artist, _, title = name.partition(" - ")
        info = _LENGTH.search(item.get("FILE_INFO") or "")
        duration = seconds(info.group(1)) if info else seconds(float(page.get("duration") or 0))
        files = []
        if page.get("key"):
            if data.get("WAV_DOWNLOAD") in (1, "1") and page.get("wav") and item.get("WAV"):
                kind = item["WAV"].rsplit(".", 1)[-1].lower()  # stored lossless master, flac so far
                files.append({"id": f"{track_id}:wav", "type": kind if kind in {"wav", "flac", "aiff", "aif"} else "wav",
                              "format": "wav", "track_id": track_id})
            files.append({"id": f"{track_id}:mp3", "type": "mp3", "format": "mp3", "track_id": track_id})
        tracks.append({"name": name, "artist": artist.strip() or None, "title": (title or name).strip(), "label": label,
                       "catalogue": data.get("CATALOGUE_NUMBER"), "isrc": item.get("ISRC"), "duration": duration,
                       "track_id": track_id, "files": files})
    if not tracks:
        raise RuntimeError("Label Engine release has no tracks; schema changed or link expired")
    title = f"{data.get('ARTIST')} - {data.get('TITLE')}" + (f" [{data['CATALOGUE_NUMBER']}]" if data.get("CATALOGUE_NUMBER") else "")
    return {"title": title, "label": label, "catalogue": data.get("CATALOGUE_NUMBER"),
            "release_id": str(data["RELEASE_ID"]), "label_id": str(data["LABEL_ID"]),
            "recipient_id": params["recipient_id"], "gated": str(data.get("FEEDBACK_GIVEN")) in {"0", ""},
            "feedback": {"id": data.get("FEEDBACK_ID"), "alert": data.get("FEEDBACK_ALERT"),
                         "required": data.get("FEEDBACK_REQUIRED"), "check": data.get("DOWNLOAD_CHECK"),
                         "echeck": data.get("DOWNLOAD_ECHECK")},
            "keys": {t: v["key"] for t, v in keys.items()}, "tracks": tracks}


class LabelEngine:
    def __init__(self, store):
        self.store, self.calls = store, 0
        self.session = requests.Session(impersonate="chrome124")

    def _get(self, url, **kwargs):
        """GET that follows redirects only inside label-engine.com."""
        for _ in range(6):
            if not labelengine_url(url):
                raise RuntimeError("Label Engine link redirected off-site")
            self.calls += 1
            response = self.session.get(url, allow_redirects=False, timeout=kwargs.pop("timeout", 60), **kwargs)
            if response.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.headers.get("Location", ""))
                response.close()
                continue
            return response
        raise RuntimeError("Label Engine link has too many redirects")

    def _params(self, source_id):
        row = self.store.db.execute("SELECT 1 FROM sources WHERE id=? AND platform='label-engine'", (source_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown Label Engine source")
        params = link_params(self.store.source_url(source_id))
        if params is None:
            raise RuntimeError("Not a Label Engine promo link")
        return params

    def open_page(self, source_id):
        """Load the promo page (starts the session the API needs) and return its HTML."""
        params = self._params(source_id)
        response = self._get(SITE + "promo.php?" + urlencode({k: params[k] for k in LINK_KEYS}))
        if response.status_code != 200:
            raise RuntimeError(f"Label Engine promo page HTTP {response.status_code}")
        return params, response.text

    def snapshot(self, source_id, refresh=False):
        cache = self.store.root / f"labelengine-{source_id}.dpapi"
        if cache.exists() and not refresh:
            return read_protected_json(cache)
        params = self._params(source_id)
        self.store.event(source_id, "view", "intent", platform="label-engine",
                         platform_effect="opening the promo page may show a view")
        params, html = self.open_page(source_id)
        response = self._get(API.format(**params), headers={"Origin": SITE.rstrip("/"), "Referer": SITE + "promo.php",
                                                            "Accept": "application/json, text/plain, */*"})
        if response.status_code == 400:
            raise RuntimeError("Label Engine promo expired or removed (API HTTP 400)")
        if response.status_code != 200:
            raise RuntimeError(f"Label Engine API HTTP {response.status_code}")
        data = response.json()
        release = parse_release(data[0] if isinstance(data, list) else data, html, params)
        protect_json(cache, release)
        self.store.event(source_id, "view", "inspected", platform="label-engine", tracks=len(release["tracks"]),
                         release_id=release["release_id"], note="Release JSON and page only; no originals or feedback")
        return release

    def release(self, source_id):
        return self.snapshot(source_id)

    @staticmethod
    def extension(file):
        return "." + file["type"]  # the pipeline corrects it from the header

    def choose_feedback(self, source_id, release):
        """Persist rating and wording before any submission so a retry reuses exactly the same text."""
        row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (source_id,)).fetchone()
        if row:
            return row
        rng = random.Random(source_id + "|" + (release["title"] or ""))
        comment = rng.choice(FEEDBACK_OPENERS) + ", " + rng.choice(FEEDBACK_CLOSERS)
        self.store.db.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?,NULL)",
                              (source_id, rng.randint(*RATING_RANGE), comment, 0, "pending", now()))
        self.store.db.commit()
        return self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (source_id,)).fetchone()

    def unlock(self, source_id, release):
        """Post the feedback form once (favourite, stars out of 5, comment). Never decline or skip."""
        row = self.choose_feedback(source_id, release)
        if row["status"] == "submitted":
            return
        if row["status"] != "pending":
            raise RuntimeError("Previous feedback submission has an uncertain outcome; inspect before retrying")
        fb = release["feedback"]
        if not fb.get("check") or not fb.get("id"):
            raise RuntimeError("Label Engine feedback form fields missing")
        favourite = next((t for t in release["tracks"] if version_kind(t["name"]) == "extended"), release["tracks"][0])
        stars = row["rating"] / 2  # stored on the 10 scale, the form takes half stars out of 5
        self.store.db.execute("UPDATE feedback SET status='submitting' WHERE source_id=?", (source_id,))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "intent", rating=stars, scale="5 stars", comment=row["comment"],
                         favourite=favourite["name"], platform_effect="unlocks downloads, visible to the label")
        self.store.sync_logs()
        self.open_page(source_id)
        query = urlencode({"recipient_id": release["recipient_id"], "check": fb["check"],
                           "release_id": release["release_id"], "echeck": fb.get("echeck") or ""})
        form = {"rate_complete": "1", "rate_interested": "1", "rate_release_id": release["release_id"],
                "rate_recipient_id": release["recipient_id"], "rate_label_id": release["label_id"],
                "rate_feedback_id": fb["id"], "rate_alert": fb.get("alert") or 0, "rate_rating": stars,
                "rate_favorite": favourite["track_id"], "rate_comments": row["comment"],
                "rate_settings": fb.get("required") or 0}
        form.update({f"trackcheck_{t['track_id']}": "1" for t in release["tracks"]})
        self.calls += 1
        response = self.session.post(SITE + "downloads_handler.php?" + query, data=form, timeout=60,
                                     allow_redirects=False, headers={"Referer": SITE + "promo.php"})
        if response.status_code != 200:
            self.store.event(source_id, "feedback", "uncertain", http=response.status_code, answer=response.text[:120])
            raise RuntimeError(f"Label Engine feedback answered HTTP {response.status_code}; inspect before retrying")
        self.store.db.execute("UPDATE feedback SET status='submitted',submitted_at=? WHERE source_id=?", (now(), source_id))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "submitted", answer=re.sub(r"\s+", " ", response.text)[:120])
        self.store.sync_logs()
        protect_json(self.store.root / f"labelengine-session-{source_id}.dpapi", self.session.cookies.get_dict())

    def restore_session(self, source_id):
        saved = self.store.root / f"labelengine-session-{source_id}.dpapi"
        if saved.exists():
            self.session.cookies.update(read_protected_json(saved))

    def download(self, source_id, file, destination):
        release = self.snapshot(source_id)
        self.restore_session(source_id)
        key = release["keys"].get(file["track_id"])
        if not key:
            raise RuntimeError("Label Engine page gave no download key for this track")
        tracks = "".join(f"&t{t['track_id']}=1" for t in release["tracks"])
        self.store.event(source_id, "platform_download", "intent", file_id=file["id"], platform="label-engine",
                         platform_effect="original transfer may mark the track downloaded")
        self.store.sync_logs()
        query = urlencode({"file_format": file["format"], "file_id": file["track_id"], "file_type": "1", "check": key,
                           "recipient_id": release["recipient_id"]}) + tracks
        response = self._get(SITE + "generate_file.php?" + query, timeout=600)
        try:
            ticket = response.json()
        except Exception:
            ticket = None
        if response.status_code != 200 or not isinstance(ticket, dict) or not ticket.get("success"):
            raise RuntimeError(f"Label Engine could not generate the file (HTTP {response.status_code}); feedback gate still locked?")
        url = SITE + "recipient_download.php?" + urlencode({"download_title": ticket["title"],
                                                             "download_path": ticket["path"], "download_key": ticket["key"]})
        response = self._get(url, stream=True, timeout=900)
        try:
            if response.status_code != 200:
                raise RuntimeError(f"Label Engine original HTTP {response.status_code}")
            if any(kind in response.headers.get("Content-Type", "").lower() for kind in ("html", "json")) \
                    or response.headers.get("Content-Length") == "0":
                raise RuntimeError("Label Engine returned no original")
            return write_stream(response.iter_content(1 << 20), destination,
                                int(response.headers.get("Content-Length") or 0), tolerance=0)
        finally:
            response.close()

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30):
        library, report, done = LibraryIndex(self.store), [], set()
        rows = self.store.db.execute(
            "SELECT id,subject FROM sources WHERE platform='label-engine' ORDER BY discovered_at DESC,id").fetchall()
        for sid, subject in rows:
            params = link_params(self.store.source_url(sid))
            if params is None or params["release_id"] in done:
                continue
            age = newest_offer_age(self.store, sid)
            if age is None or age > max_age_days:
                continue
            done.add(params["release_id"])  # one blast per release; later mails for it repeat the same tracks
            try:
                summary = collect_release(self.store, self, sid, library, dry_run=dry_run, allow_gate=allow_gate)
                report.append({"release": summary.get("release") or subject, "tracks": summary["tracks"]})
            except Exception as error:
                self.store.event(sid, "batch", "release_failed", release=subject, error=str(error)[:200])
                report.append({"release": subject, "result": "failed: " + str(error)[:160]})
        self.store.sync_logs(force=True)
        return {"releases": report, "http_requests": self.calls}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    mail = sub.add_parser("discover-mail")
    mail.add_argument("--since", default="30d")
    collect = sub.add_parser("collect")
    collect.add_argument("source_id")
    batch = sub.add_parser("batch")
    batch.add_argument("--max-age-days", type=int, default=30)
    for command in (collect, batch):
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--allow-feedback", action="store_true", help="send the feedback form to unlock downloads")
    args = parser.parse_args()
    store = Store()
    try:
        adapter = LabelEngine(store)
        if args.command == "discover-mail":
            result = discover(store, args.since, ["label-engine"])
        elif args.command == "collect":
            result = collect_release(store, adapter, args.source_id, LibraryIndex(store), dry_run=args.dry_run,
                                     allow_gate=args.allow_feedback)
            store.sync_logs(force=True)
        else:
            result = adapter.batch(args.dry_run, args.allow_feedback, max_age_days=args.max_age_days)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
