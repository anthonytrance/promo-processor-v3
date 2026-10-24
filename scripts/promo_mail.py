"""Read-only mailbox source discovery. Stores access links with DPAPI; never follows links or marks mail read."""
from __future__ import annotations

import argparse
import email
import email.utils
import hashlib
import html
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from promo_adapters import ADAPTERS
from promo_automation import ACCOUNT, Store, email_skill

# Source status and next action live in notes/source-backlog.md in the state folder.

DOMAINS = {
    "inflyte": ("inflyteapp.com",), "promoly": ("ln.promo.ly",),
    "promo-cloud": ("promo-cloud.com",), "ampsuite": ("ampsuite.com",),
    "promobox": ("promo-box.com",), "promochoons": ("promochoons.com",),
    "disco": ("disco.ac",), "label-engine": ("label-engine.com",),
    "promo-delivery": ("promodelivery.co.uk",), "fatdrop": ("fatdrop.co.uk",),
    "promopush": ("promopush.com",), "hearthis": ("hearthis.ai",),
    "beatrolo": ("beatrolo.com",), "ctrl-promo": ("ctrlpromo.com", "ctrlmusicpromo.com"),
    "needlepool": ("needlepool.com",), "promojukebox": ("promojukebox.com",),
    "droptrack": ("droptrack.com",), "dropbox": ("dropbox.com", "db.tt"),
    "constant-contact": ("files.constantcontact.com",), "soundcloud": ("soundcloud.com",),
    "google-drive": ("drive.google.com",), "onedrive": ("1drv.ms", "onedrive.live.com"),
    "pcloud": ("pcloud.link", "pcloud.com"), "wetransfer": ("we.tl", "wetransfer.com"),
    "label-portal": ("promo.progressivevibesmusic.com", "promo.nyxcreativestudio.com",
                     "promo.blackhandmusic.net", "music.biginstrumental.com", "promote-it.cc"),
}

# Adapters that exist; the coordinator runs every one of them.
IMPLEMENTED_PLATFORMS = frozenset(ADAPTERS)
# Saved per-message scan results are valid for this routing code and adapter set only.
SCAN_VERSION = hashlib.sha256(Path(__file__).read_bytes() + ",".join(sorted(IMPLEMENTED_PLATFORMS)).encode()).hexdigest()[:16]


# Mail tracker, newsletter and store hosts: never a promo source by themselves. Tracker links hide their target, and
# following one is a click, so mails that link only through these are reported as tracker-only.
NOISE_HOSTS = ("list-manage.com", "mailchi.mp", "mailchimp.com", "mailchimpapp.com", "sendibm1.com", "sendibm3.com",
               "sp1-brevo.net", "rs6.net", "constantcontact.com", "ccsend.com", "yamm-track.appspot.com",
               "mmemails.appspot.com", "mlsend.com", "awstrack.me", "sender.net", "campaign-statistics.com",
               "campaign-preferences.com", "squarespace-mail.com", "squarespace.com", "tsopensclicks.com",
               "itunes.apple.com", "music.apple.com", "play.google.com", "beatport.com", "songstats.com",
               "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "youtu.be", "tiktok.com",
               "spotify.com", "linktr.ee", "google.com", "apple.com", "disco-tracking.net", "resend-clicks-a.com",
               "links.send.needlepool.net", "trckln.com", "ffm.to", "found.ee", "amazon.com", "deezer.com", "distrokid.com",
               "vk.com", "1001tracklists.com")
PROMO_SUBJECT = re.compile(r"promo|premiere|exclusive|out now|remix|\bEP\b|single|release|support", re.I)


def host_of(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _within(host, domains):
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def noise_host(host):
    return _within(host, NOISE_HOSTS) or host.endswith(("-dot-yamm-track.appspot.com", "-dot-mmemails.appspot.com"))


def unrecognised(links, subject, sender):
    """Report new hosts even beside a supported link, including a sender's own delivery portal. Never click."""
    if not PROMO_SUBJECT.search(subject or ""):
        return None
    relevant = [url for url in links if not re.search(
        r"unsubscribe|privacy|preferences|consent|register|/terms|/contact|/about|\.(?:png|jpg|jpeg|gif|svg|webp|css)$", urlsplit(url).path, re.I)
                and (urlsplit(url).path not in {"", "/"} or urlsplit(url).query)]
    hosts = sorted({host_of(url) for url in relevant} - {""})
    candidates = [h for h in hosts if not noise_host(h)]
    routed_hosts = {host_of(url) for url in relevant if route(url)}
    known = [h for h in candidates if h not in routed_hosts and any(_within(h, d) for d in DOMAINS.values())]
    unknown = [h for h in candidates if not any(_within(h, d) for d in DOMAINS.values())]
    sender_hosts = sender_domain(sender)
    known.extend(h for h in sender_hosts if not routed_hosts and any(_within(h, d) for d in DOMAINS.values()))
    tracker_only = not candidates and not routed_hosts and any(noise_host(h) for h in hosts)
    if not unknown and not known and not tracker_only:
        return None
    return {"sender": sender_domain(sender)[0] if sender_domain(sender) else "", "subject": subject[:120],
            "hosts": unknown, "known_platform_unrouted": sorted(set(known)), "tracker_only": tracker_only}


def sender_domain(sender):
    domain = email.utils.parseaddr(sender or "")[1].rpartition("@")[2].lower()
    return (domain,) if domain else ()


def route(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    for platform, domains in DOMAINS.items():
        if not any(host == domain or host.endswith("." + domain) for domain in domains):
            continue
        path = parsed.path.lower()
        if platform == "promoly" and not re.fullmatch(r"/servepromo/[^/]+/?", parsed.path):
            return None
        if platform == "inflyte" and not re.fullmatch(r"/review/releases/\d+/?", path):
            return None
        if platform == "promo-delivery" and (path != "/promo.php"
                or not (parse_qs(parsed.query).get("r") or [""])[0].isdecimal()
                or not (parse_qs(parsed.query).get("u") or [""])[0]):
            return None
        if platform == "promo-cloud" and path != "/targetedpromo/view":
            return None
        if platform == "disco" and not path.startswith(("/share-new/", "/p/", "/share/")):
            return None
        if platform == "needlepool" and path != "/.netlify/functions/auth-magic":
            return None
        if platform == "ctrl-promo" and not path.startswith("/view-promo/"):
            return None
        if platform == "promopush" and not parsed.query:
            return None
        if platform == "dropbox" and host != "db.tt" and not path.startswith(("/s/", "/sh/", "/scl/fi/", "/scl/fo/")):
            return None
        if re.search(r"unsubscribe|privacy|preferences|/logo|\.(?:png|jpg|jpeg|gif|svg|webp)$", path):
            return None
        return platform
    return None


def links_from_message(message):
    links = set()
    for part in message.walk():
        if part.get_content_type() not in {"text/html", "text/plain"} or part.get_content_disposition() == "attachment":
            continue
        body = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
        if part.get_content_type() == "text/html":
            for a in BeautifulSoup(body, "html.parser").select("a[href]"):
                links.add(a.get("href", ""))
                # A mail forwarded from Gmail keeps the real link beside the tracker; reading it is not a click.
                links.add(unwrap_google_redirect(a.get("data-saferedirecturl", "")))
        else:
            links.update(re.findall(r"https?://[^\s<>\"]+", body))
    return {clean_link(url) for url in links if url.strip().startswith("https://")}


def unwrap_google_redirect(url):
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname == "www.google.com" and parsed.path == "/url":
        return (parse_qs(parsed.query).get("q") or [""])[0]
    return ""


def clean_link(url):
    """Trailing sentence punctuation and the closing bracket of "(https://...)" in plain-text mail are not the link."""
    url = html.unescape(url).strip()
    while url and (url[-1] in ".,;:!'" or (url[-1] in ")]" and url.count(url[-1]) > url.count("([" [")]".index(url[-1])]))):
        url = url[:-1]
    return url


def discover(store, since="30d", platforms=None, folders=("INBOX",), sender_domain=None):
    skill = email_skill()
    client = skill.imap_login(ACCOUNT)
    counts, examined, unadapted, unselected, unknown = Counter(), 0, {}, Counter(), []
    try:
        for folder in folders:
            skill._select(client, folder, readonly=True)
            validity = client.response("UIDVALIDITY")[1]
            validity = validity[0].decode() if validity and validity[0] else "unknown"
            criteria = ["SINCE", skill.parse_date_arg(since)]
            if sender_domain:
                if not re.fullmatch(r"[A-Za-z0-9.-]+", sender_domain):
                    raise ValueError("Invalid sender domain")
                criteria += ["FROM", '"' + sender_domain + '"']
            status, data = client.uid("search", None, *criteria)
            if status != "OK":
                raise RuntimeError("Mailbox search failed")
            uids = data[0].split()

            def tally(uid, scan):
                """Count one scanned message; False when a selected platform still lacks its stored source."""
                for platform, sid in scan["links"]:
                    if platform in IMPLEMENTED_PLATFORMS and (not platforms or platform in platforms) \
                            and (sid is None or sid not in known):
                        return False
                if scan["report"]:
                    unknown.append(scan["report"])
                for platform, sid in scan["links"]:
                    if platform not in IMPLEMENTED_PLATFORMS:
                        # Count message identities across folders, not links or colliding folder-local UIDs.
                        seen = unadapted.setdefault(platform, {"mails": set(), "subjects": []})
                        identity = (folder, validity, uid)
                        if identity not in seen["mails"]:
                            seen["mails"].add(identity)
                            seen["subjects"].append(scan["subject"][:120])
                    if platforms and platform not in platforms:
                        if platform in IMPLEMENTED_PLATFORMS:
                            unselected[platform] += 1
                        continue
                    counts[platform] += 1
                return True

            # 2026-10-24: every run downloaded every full message of the last 30 days from both folders. A message
            # never changes under its folder, UIDVALIDITY and UID, so its result is kept and only new mail is read.
            known = {r[0] for r in store.db.execute("SELECT id FROM sources")}
            cached = {r[0]: json.loads(r[1]) for r in store.db.execute(
                "SELECT uid, data FROM mail_scans WHERE folder=? AND uidvalidity=? AND version=?",
                (folder, validity, SCAN_VERSION))}
            todo = []
            for raw_uid in uids:
                scan = cached.get(raw_uid.decode())
                if scan is not None and tally(raw_uid, scan):
                    examined += 1
                    continue
                todo.append(raw_uid)
            for offset in range(0, len(todo), 50):
                status, fetched = client.uid("fetch", b",".join(todo[offset:offset + 50]), "(UID BODY.PEEK[])")
                if status != "OK":
                    raise RuntimeError("Mailbox fetch failed; discovery incomplete")
                for item in fetched:
                    if not isinstance(item, tuple):
                        continue
                    uid = re.search(rb"\bUID\s+(\d+)", item[0])
                    if uid is None:
                        raise RuntimeError("Mailbox returned a message without its UID")
                    message = email.message_from_bytes(item[1])
                    examined += 1
                    subject = skill.decode_str(message.get("Subject", ""))
                    sender = skill.decode_str(message.get("From", ""))
                    links = links_from_message(message)
                    report = unrecognised(links, subject, sender)
                    if report:
                        report.update(folder=folder, uidvalidity=validity, uid=uid[1].decode(),
                                      message_id=message.get("Message-ID", ""))
                    scan = {"subject": subject, "report": report, "links": []}
                    for url in sorted(links):
                        platform = route(url)
                        if platform is None:
                            continue
                        sid = None
                        if platform in IMPLEMENTED_PLATFORMS and (not platforms or platform in platforms):
                            sid = store.add_source(platform, subject, url, sender=sender)
                            store.db.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?)",
                                             (ACCOUNT, folder, validity, uid[1].decode(), message.get("Message-ID", ""),
                                              subject, message.get("Date", ""), sid))
                            known.add(sid)
                        scan["links"].append([platform, sid])
                    tally(uid[1], scan)
                    previous = cached.get(uid[1].decode())
                    if previous and [p for p, _ in previous["links"]] == [p for p, _ in scan["links"]]:
                        # Links are sorted, so a run that selected other platforms stored those sources in place.
                        scan["links"] = [[p, s or old] for (p, s), (_, old) in zip(scan["links"], previous["links"])]
                    store.db.execute("INSERT OR REPLACE INTO mail_scans VALUES(?,?,?,?,?)",
                                     (folder, validity, uid[1].decode(), SCAN_VERSION, json.dumps(scan, ensure_ascii=False)))
                store.db.commit()
            window = re.fullmatch(r"(\d+)d", since or "")
            if window and int(window.group(1)) >= 30 and not sender_domain:
                # Mail that left the offer window, or a cache from another rule version, is never asked for again.
                current = [u.decode() for u in uids]
                store.db.execute("DELETE FROM mail_scans WHERE folder=? AND (uidvalidity!=? OR version!=? OR uid NOT IN "
                                 "(SELECT value FROM json_each(?)))", (folder, validity, SCAN_VERSION, json.dumps(current)))
                store.db.commit()
    finally:
        client.logout()
    totals = dict(store.db.execute("SELECT platform,COUNT(*) FROM sources GROUP BY platform"))
    return {"messages_scanned": examined, "link_occurrences": dict(counts), "stored_sources": totals,
            "mail_read_only": True, "platform_requests": 0,
            "platforms_without_adapter": {p: {"mails": len(v["mails"]), "subjects": v["subjects"][:5]}
                                          for p, v in sorted(unadapted.items())},
            "implemented_platforms_not_selected": dict(unselected),
            "unrecognised_promo_mails": unknown}


def newest_offer_age(store, source_id):
    """Age in days of the offer that decides the 30-day window.

    2026-10-24: on email-led platforms a promo is offered once and reminders must be ignored, so the age is
    that of the first mail of the same provider campaign or release. Mutable shares keep the newest mail's age,
    because a new mail there can mean new files.
    """
    row = store.db.execute("SELECT platform FROM sources WHERE id=?", (source_id,)).fetchone()
    from promo_offer_identity import EMAIL_MASTER_PLATFORMS, initial_offer_age
    if row is not None and row[0] in EMAIL_MASTER_PLATFORMS:
        return initial_offer_age(store, source_id, row[0])
    ages = []
    for (received,) in store.db.execute("SELECT received FROM messages WHERE source_id=?", (source_id,)):
        try:
            moment = email.utils.parsedate_to_datetime(received)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            ages.append((datetime.now(timezone.utc) - moment).total_seconds() / 86400)
        except (ValueError, TypeError):
            continue
    return min(ages) if ages else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="30d")
    parser.add_argument("--platform", action="append", choices=sorted(DOMAINS))
    parser.add_argument("--folder", action="append")
    parser.add_argument("--sender-domain", help="Optional server-side mail filter to avoid rereading unrelated mail")
    args = parser.parse_args()
    store = Store()
    try:
        print(json.dumps(discover(store, args.since, args.platform, args.folder or ("INBOX",), args.sender_domain), ensure_ascii=False))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
