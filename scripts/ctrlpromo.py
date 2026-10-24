"""CTRL Promo recipient promos, browserless. No previews are played and no tracking pixel is fetched.

A mailed https://ctrlpromo.com/view-promo/<promo>/<key>?mailingId=<n> link signs the recipient account into a
Laravel session and 302s to /view-promo/<promo>. That session then reads the complete account inbox (/promos),
the promo page (CSRF token, per-track format classes) and /view-promo/<promo>?info=1 (release JSON). Feedback
(rating out of 10 plus a comment) unlocks the downloads; "Not for me" is never sent. Only the format the label
actually uploaded (promo_files.original_filename) is offered: a WAV beside an uploaded MP3 is a platform transcode.
"""
import argparse
import json
import random
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

from promo_automation import ACCOUNT, Store, now
from promo_cloud import FEEDBACK_CLOSERS, FEEDBACK_OPENERS, protect_json, read_protected_json
from promo_mail import discover, newest_offer_age
from promo_pipeline import LibraryIndex, collect_release, write_stream

BASE = "https://ctrlpromo.com"
HOSTS = {"ctrlpromo.com", "www.ctrlpromo.com"}
CDN = {"cdn.ctrlpromo.com"}
AUDIO = {"wav": "wav", "aif": "aiff", "aiff": "aiff", "mp3": "mp3", "flac": "flac"}
REDIRECTS = {301, 302, 303, 307, 308}


class AuthBlocked(RuntimeError):
    """The mailed link or saved session no longer signs the recipient in."""


def secure_host(parsed, hosts=HOSTS):
    try:
        return (parsed.scheme == "https" and parsed.hostname in hosts and parsed.port in (None, 443)
                and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


def recipient_link(url):
    """(promo id, recipient key or None) for a mailed view-promo link or a catalogue /view-promo/<id> source."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if not secure_host(parsed) or parsed.fragment:
        return None
    match = re.fullmatch(r"/view-promo/(\d{1,12})(?:/([A-Za-z0-9_-]{16,512}))?/?", parsed.path)
    if not match:
        return None
    mailing = parse_qs(parsed.query).get("mailingId", [""])[0]
    if set(parse_qs(parsed.query)) - {"mailingId"} or (mailing and not mailing.isdecimal()):
        return None
    return match.group(1), match.group(2)


def cdn_original(url, promo_id):
    """Signed original-file storage for this promo only; previews, ZIPs and artwork are refused."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    query = parse_qs(parsed.query)
    return (secure_host(parsed, CDN) and bool(query.get("md5", [""])[0]) and bool(query.get("expires", [""])[0])
            and re.fullmatch(rf"/data/promos_files/{re.escape(str(promo_id))}/[^/]+\.(?:wav|aif|aiff|mp3|flac)", parsed.path, re.I)
            is not None)


def date_age(value, formats=("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m %Y")):
    for fmt in formats:
        try:
            moment = datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - moment).total_seconds() / 86400
        except (ValueError, AttributeError, TypeError):
            continue
    return None


def parse_page(html):
    """CSRF token, per-file format classes and the gate text from the promo page; no JavaScript is evaluated."""
    soup = BeautifulSoup(html, "html.parser")
    token = soup.select_one("input#token")
    formats = {}
    for cell in soup.select("td.download-td[id^=dfile-]"):
        classes = set(cell.get("class") or [])
        formats[cell["id"].removeprefix("dfile-")] = {"wav": "wav-file" in classes, "aif": "aif-file" in classes}
    return {"token": token.get("value") if token else None, "formats": formats,
            "feedback_form": soup.select_one("#send-comment") is not None,
            "login_page": soup.select_one("input[type=password]") is not None and not formats}


def parse_catalogue(html):
    """Every card of the signed-in inbox, plus the nav count used to prove the list is complete."""
    soup = BeautifulSoup(html, "html.parser")
    cards, seen = [], set()
    for card in soup.select(".cp-card"):
        link = card.select_one("a.view-promo[href]")
        match = re.fullmatch(r"(?:https://ctrlpromo\.com)?/view-promo/(\d+)/?", link["href"]) if link else None
        if not match:
            raise RuntimeError("CTRL Promo inbox card lacks its promo id; catalogue schema changed")
        if match.group(1) in seen:
            raise RuntimeError("CTRL Promo inbox repeats a promo id; catalogue incomplete")
        seen.add(match.group(1))
        status = card.select_one(".cp-status")
        classes = set(status.get("class") or []) if status else set()
        published = re.search(r"Published\s+(\d{2}/\d{2}\s+\d{4})", card.get_text(" ", strip=True))
        title = card.select_one("h5")
        label = card.select_one(".figcaption .promo-label")
        cards.append({"id": match.group(1), "title": title.get_text(" ", strip=True) if title else "",
                      "label": label.get_text(" ", strip=True) if label else None,
                      "responded": "cp-status-done" in classes, "new": "cp-status-new" in classes,
                      "published": published.group(1) if published else None})
    count = soup.select_one("a.promos-link .promos-count")
    paged = bool(soup.select(".pagination, a[rel=next]"))
    return {"cards": cards, "count": int(count.get_text(strip=True)) if count and count.get_text(strip=True).isdigit() else None,
            "paged": paged}


def normalize_release(info, page, responded=None):
    promo = (info or {}).get("promo") if isinstance(info, dict) else None
    if not isinstance(promo, dict) or not str(promo.get("id") or "").isdigit() or not promo.get("name"):
        raise RuntimeError("CTRL Promo release schema changed or promo removed")
    if promo.get("files_deleted"):
        raise RuntimeError("CTRL Promo unavailable: the sender removed this promo's files")
    if promo.get("expire_date") and (date_age(str(promo["expire_date"])) or 0) > 0:
        raise RuntimeError("CTRL Promo explicitly expired this promo")
    label = (promo.get("label") or {}).get("name") or None  # label.company_name is the sending PR account
    tracks, warnings = [], []
    for item in promo.get("promo_files") or []:
        file_id, artist, title = str(item.get("id") or ""), (item.get("artist_name") or "").strip(), (item.get("song_name") or "").strip()
        if not file_id.isdigit() or not title:
            raise RuntimeError("CTRL Promo track lacks its stable id or title")
        uploaded = (item.get("original_filename") or item.get("filename") or "").rpartition(".")[2].lower()
        if uploaded not in AUDIO:
            raise RuntimeError("CTRL Promo original upload format is unknown; inspect before collecting")
        offered = (page.get("formats") or {}).get(file_id, {})
        kind = AUDIO[uploaded]
        if kind == "wav" and not offered.get("wav") or kind == "aiff" and not offered.get("aif"):
            raise RuntimeError("CTRL Promo does not offer the uploaded lossless original for download")
        if kind == "mp3" and (offered.get("wav") or offered.get("aif")):
            warnings.append(f"{artist} - {title}: WAV/AIF offered beside an uploaded MP3 are transcodes; MP3 original taken")
        try:
            duration = float(item.get("duration")) or None
        except (TypeError, ValueError):
            duration = None
        try:
            size = int(item["file_size"]) if kind == "mp3" else None
        except (KeyError, TypeError, ValueError):
            size = None
        tracks.append({"name": f"{artist} - {title}" if artist else title, "artist": artist or None, "title": title,
                       "label": label, "duration": duration, "bpm": item.get("bpm"),
                       "files": [{"id": file_id, "type": kind, "bytes": size}]})
    if not tracks:
        raise RuntimeError("CTRL Promo unavailable: no tracks are offered on this promo")
    release = {"title": promo["name"], "label": label, "release_id": str(promo["id"]), "tracks": tracks,
               "catalogue_number": promo.get("catalogue_number"), "published": promo.get("published_at"),
               "gated": responded is not True}
    if warnings:
        release["warnings"] = warnings
    return release


class CtrlPromo:
    platform = "ctrl-promo"

    def __init__(self, store):
        self.store, self.calls = store, 0
        self.session = requests.Session(impersonate="chrome124")
        self.session_path = store.root / "ctrlpromo-session.dpapi"
        self.catalogue_path = store.root / "ctrlpromo-catalogue.dpapi"
        self.signed_in = False
        if self.session_path.exists():
            saved = read_protected_json(self.session_path)
            if str(saved.get("identity", "")).casefold() == ACCOUNT.casefold():
                self.session.cookies.update(saved.get("cookies") or {})

    def _request(self, method, url, **kwargs):
        url = urljoin(BASE, url)
        if not secure_host(urlsplit(url)):
            raise RuntimeError("CTRL Promo request left its verified host")
        self.calls += 1
        try:
            return self.session.request(method, url, timeout=kwargs.pop("timeout", 60), allow_redirects=False, **kwargs)
        except Exception:
            raise RuntimeError("CTRL Promo request failed; protected request details suppressed") from None

    def _target(self, source_id):
        row = self.store.db.execute("SELECT 1 FROM sources WHERE id=? AND platform=?", (source_id, self.platform)).fetchone()
        target = recipient_link(self.store.source_url(source_id)) if row else None
        if not target:
            raise ValueError("Not a known CTRL Promo recipient promo")
        return target

    def _save_session(self):
        protect_json(self.session_path, {"identity": ACCOUNT, "cookies": dict(self.session.cookies)})

    def sign_in(self):
        """Open the newest mailed recipient link (it lands on its own promo page), which signs the account in."""
        keyed = []
        for (sid,) in self.store.db.execute("SELECT id FROM sources WHERE platform=?", (self.platform,)):
            target = recipient_link(self.store.source_url(sid))
            if target and target[1]:
                keyed.append(((newest_offer_age(self.store, sid) or 1e9), sid, target))
        if not keyed:
            raise AuthBlocked("No mailed CTRL Promo recipient link to sign in with")
        _, sid, (promo, _) = min(keyed)
        self.store.event(sid, "login", "intent", platform=self.platform,
                         platform_effect="mailed recipient link creates an account session; may count as a view")
        self.store.sync_logs()
        response = self._request("GET", self.store.source_url(sid))
        try:
            location = urljoin(BASE, response.headers.get("Location", ""))
            if response.status_code not in REDIRECTS or recipient_link(location) != (promo, None):
                raise AuthBlocked(f"CTRL Promo mailed link did not sign in (HTTP {response.status_code})")
        finally:
            response.close()
        self.signed_in = True
        self._save_session()
        self.store.event(sid, "login", "confirmed", platform=self.platform)
        return {"signed_in": True}

    def _get(self, path, **kwargs):
        """GET within the account session, signing in once from a mailed link when the session is missing or stale."""
        for attempt in (0, 1):
            response = self._request("GET", path, **kwargs)
            location = response.headers.get("Location", "")
            if response.status_code in REDIRECTS and "/login" in location or response.status_code in {401, 419}:
                response.close()
                if attempt or self.signed_in:
                    raise AuthBlocked("CTRL Promo session refused after a fresh mailed-link sign-in")
                self.sign_in()
                continue
            if response.status_code != 200:
                response.close()
                raise RuntimeError(f"CTRL Promo HTTP {response.status_code} for {path.split('?')[0]}")
            return response
        raise AuthBlocked("CTRL Promo sign-in failed")

    def snapshot(self, source_id, refresh=False):
        promo, _ = self._target(source_id)
        cache = self.store.root / f"ctrlpromo-{promo}.dpapi"
        if cache.exists() and not refresh:
            return read_protected_json(cache)
        if not self.session.cookies.get("laravel_session"):
            self.sign_in()
        self.store.event(source_id, "view", "intent", platform=self.platform,
                         platform_effect="promo page and info JSON GET; may mark the promo opened; no tracking pixel or preview")
        self.store.sync_logs()
        response = self._get(f"/view-promo/{promo}")
        try:
            page = parse_page(response.text)
        finally:
            response.close()
        if page["login_page"] or not page["token"]:
            raise AuthBlocked("CTRL Promo page did not load inside the recipient session")
        response = self._get(f"/view-promo/{promo}", params={"info": 1},
                             headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
        try:
            info = response.json()
        except Exception:
            raise RuntimeError("CTRL Promo info route did not return JSON") from None
        finally:
            response.close()
        # Signed storage links are never kept or used: downloads go through the gated download route only.
        for item in (info.get("promo") or {}).get("promo_files") or []:
            for key in [k for k in item if k.startswith("signed_") or k == "peaks"]:
                item.pop(key)
        for key in [k for k in info.get("promo") or {} if k.startswith("secured_") or k in {"description", "email_subject",
                                                                                           "email_preheader", "stripe_subscription"}]:
            info["promo"].pop(key)
        if str((info.get("promo") or {}).get("id")) != promo:
            raise RuntimeError("CTRL Promo info JSON is for a different promo")
        data = {"info": info, "page": page, "fetched_at": now()}
        normalize_release(info, page)  # schema check before caching
        self._save_session()
        protect_json(cache, data)
        self.store.event(source_id, "view", "inspected", platform=self.platform,
                         tracks=len((info.get("promo") or {}).get("promo_files") or []), note="Metadata only; no audio or feedback")
        return data

    def responded(self, promo):
        if not self.catalogue_path.exists():
            return None
        card = read_protected_json(self.catalogue_path).get("cards", {}).get(promo)
        return card.get("responded") if card else None

    def release(self, source_id):
        promo, _ = self._target(source_id)
        data = self.snapshot(source_id)
        release = normalize_release(data["info"], data["page"], self.responded(promo))
        if self._feedback_row(promo, "submitted"):
            release["gated"] = False
        return release

    def discover_catalogue(self):
        """The signed-in inbox is the complete recipient catalogue: count-checked against the nav total, no paging."""
        self.store.event(None, "catalogue", "intent", platform=self.platform, platform_effect="read recipient inbox only")
        self.store.sync_logs()
        response = self._get("/promos")
        try:
            listing = parse_catalogue(response.text)
        finally:
            response.close()
        self._save_session()
        complete = not listing["paged"] and listing["count"] == len(listing["cards"])
        cards = {card["id"]: card for card in listing["cards"]}
        protect_json(self.catalogue_path, {"cards": cards, "count": listing["count"], "complete": complete, "read_at": now()})
        mailed = {}
        for (sid,) in self.store.db.execute("SELECT id FROM sources WHERE platform=?", (self.platform,)):
            target = recipient_link(self.store.source_url(sid))
            if target:
                mailed.setdefault(target[0], []).append(sid)
        added = []
        for card in listing["cards"]:
            if card["id"] in mailed:
                source_ids = mailed[card["id"]]  # Preserve mailed recipient links and message dates.
            else:
                sid = self.store.add_source(self.platform, card["title"] or "CTRL Promo promo", f"{BASE}/view-promo/{card['id']}",
                                            remote_id=card["id"], offered_at=card["published"])
                added.append(sid)
                source_ids = [sid]
            for sid in source_ids:
                metadata = json.loads(self.store.db.execute("SELECT metadata FROM sources WHERE id=?", (sid,)).fetchone()[0])
                metadata.update(offered_at=card["published"], catalogue_card={
                    k: card.get(k) for k in ("id", "title", "label", "published")})
                self.store.db.execute("UPDATE sources SET metadata=? WHERE id=?", (json.dumps(metadata), sid))
        self.store.db.commit()
        self.store.event(None, "catalogue", "inspected", platform=self.platform, total=len(cards),
                         nav_count=listing["count"], complete=complete, added=len(added))
        return {"available": True, "total": len(cards), "nav_count": listing["count"], "complete": complete,
                "pending_feedback": sum(1 for c in cards.values() if not c["responded"]), "added_sources": added,
                "promos": [{"id": c["id"], "title": c["title"], "label": c["label"], "published": c["published"],
                            "responded": c["responded"]} for c in cards.values()]}

    @staticmethod
    def extension(file):
        return "." + file["type"]

    def _feedback_row(self, promo, status=None):
        previous = self.store.db.execute("SELECT source_id FROM events WHERE action='feedback' AND status='intent' "
                                         "AND json_extract(details,'$.ctrl_promo')=? ORDER BY sequence LIMIT 1", (promo,)).fetchone()
        if not previous:
            return None
        row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (previous[0],)).fetchone()
        return row if row and (status is None or row["status"] == status) else None

    def unlock(self, source_id, release):
        # The pipeline calls this only after checking explicit feedback permission.
        promo = release["release_id"]
        existing = self._feedback_row(promo)
        feedback_source = existing["source_id"] if existing else source_id
        row = existing or self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (feedback_source,)).fetchone()
        if row and row["status"] == "submitted":
            return
        if row and row["status"] != "pending":
            raise RuntimeError("CTRL Promo feedback outcome is uncertain; inspect before retrying")
        if not row:
            rng = random.Random(source_id)
            comment = rng.choice(FEEDBACK_OPENERS) + ", " + rng.choice(FEEDBACK_CLOSERS)
            self.store.db.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?,NULL)",
                                  (feedback_source, rng.randint(7, 9), comment, 0, "pending", now()))
            self.store.db.commit()
            row = self.store.db.execute("SELECT * FROM feedback WHERE source_id=?", (feedback_source,)).fetchone()
        page = self.snapshot(source_id)["page"]
        favourite = release["tracks"][0]["files"][0]["id"]
        self.store.db.execute("UPDATE feedback SET status='submitting' WHERE source_id=?", (feedback_source,))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "intent", platform=self.platform, comment=row["comment"], rating=row["rating"],
                         ctrl_promo=promo, platform_effect="one stock thanks review out of 10; no play/support claim, never Not for me")
        self.store.sync_logs()
        # Exactly the website's fields; 'play', 'playingtime' and 'notForMe' are never sent.
        response = self._request("POST", f"/new-comment/{promo}",
                                 data={"file_id": favourite, "content": row["comment"], "rate": row["rating"], "_token": page["token"]},
                                 headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json",
                                          "Referer": f"{BASE}/view-promo/{promo}"})
        try:
            try:
                result = response.json()
            except Exception:
                result = None
            protect_json(self.store.root / f"ctrlpromo-feedback-{promo}.dpapi", {"status": response.status_code, "json": result})
            if response.status_code != 200 or not isinstance(result, dict) or result.get("success") is not True or result.get("errors"):
                raise RuntimeError(f"CTRL Promo feedback HTTP {response.status_code} without confirmed success; inspect before retrying")
        finally:
            response.close()
        self.store.db.execute("UPDATE feedback SET status='submitted',submitted_at=? WHERE source_id=?", (now(), feedback_source))
        self.store.db.commit()
        self.store.event(source_id, "feedback", "submitted", platform=self.platform, ctrl_promo=promo)
        self.store.sync_logs()

    def download(self, source_id, file, destination):
        promo, _ = self._target(source_id)
        params = {"wavfile": int(file["type"] == "wav"), "aiffile": int(file["type"] == "aiff")}
        self.store.event(source_id, "platform_download", "intent", platform=self.platform, file_id=file["id"],
                         platform_effect="download route records the download for the sender")
        self.store.sync_logs()
        response = self._request("GET", f"/download-promo/{promo}/{file['id']}", params=params, stream=True, timeout=900)
        if response.status_code in REDIRECTS and "/login" in response.headers.get("Location", ""):
            response.close()
            raise AuthBlocked("CTRL Promo session expired before download")
        for _ in range(3):
            if response.status_code in REDIRECTS:
                target = urljoin(BASE, response.headers.get("Location", ""))
                response.close()
                if not cdn_original(target, promo):
                    raise RuntimeError("CTRL Promo download did not redirect to its original-file storage; downloads may still be locked")
                if not urlsplit(target).path.lower().endswith("." + ("aif" if file["type"] == "aiff" else file["type"])) \
                        and not (file["type"] == "aiff" and urlsplit(target).path.lower().endswith(".aiff")):
                    raise RuntimeError("CTRL Promo storage file format differs from the uploaded original")
                self.calls += 1
                try:
                    # Signed storage: never send the account session cookies.
                    response = requests.get(target, impersonate="chrome124", stream=True, allow_redirects=False, timeout=900)
                except Exception:
                    raise RuntimeError("CTRL Promo original storage transfer failed; signed URL suppressed") from None
                continue
            try:
                if response.status_code != 200:
                    raise RuntimeError(f"CTRL Promo original HTTP {response.status_code}")
                kind = response.headers.get("Content-Type", "").lower()
                if any(x in kind for x in ("html", "json", "text/")):
                    raise RuntimeError("CTRL Promo returned a page instead of an original file; downloads may still be locked")
                size = int(response.headers.get("Content-Length") or 0) or file.get("bytes")
                return write_stream(response.iter_content(1 << 20), destination, size, tolerance=0)
            finally:
                response.close()
        response.close()
        raise RuntimeError("CTRL Promo original exceeded redirect limit")

    def campaigns(self, max_age_days=30):
        best, outside = {}, []
        for row in self.store.db.execute("SELECT id,metadata FROM sources WHERE platform=? ORDER BY discovered_at,id", (self.platform,)):
            target = recipient_link(self.store.source_url(row["id"]))
            if not target:
                continue
            age = newest_offer_age(self.store, row["id"])
            if age is None:
                age = date_age(json.loads(row["metadata"]).get("offered_at") or "")
            if age is None or age > max_age_days:
                outside.append({"source_id": row["id"], "promo": target[0],
                                "age_days": round(age, 1) if age is not None else None})
                continue
            if target[0] not in best or age < best[target[0]][1]:
                best[target[0]] = (row["id"], age)
        return [sid for sid, _ in best.values()], outside

    def coordinator_sources(self, max_age_days=30):
        """The coordinator consumes source IDs; standalone batch also reports excluded dates."""
        selected, _outside = self.campaigns(max_age_days=max_age_days)
        return selected

    def batch(self, dry_run=False, allow_gate=False, max_age_days=30):
        try:
            catalogue = self.discover_catalogue()
        except AuthBlocked as error:
            catalogue = {"available": False, "reason": str(error)}
        selected, outside = self.campaigns(max_age_days)
        excluded = []
        for (sid,) in self.store.db.execute("SELECT id FROM sources WHERE platform=?", (self.platform,)):
            if not recipient_link(self.store.source_url(sid)):
                excluded.append(sid)  # homepage/footer rows routed by domain; never opened
        library, report, seen = LibraryIndex(self.store), [], set()
        for sid in selected:
            try:
                promo, _ = self._target(sid)
                if promo in seen:
                    continue
                seen.add(promo)
                summary = collect_release(self.store, self, sid, library, dry_run=dry_run, allow_gate=allow_gate)
                report.append({"source_id": sid, **summary})
            except Exception as error:
                self.store.event(sid, "batch", "release_failed", platform=self.platform, error=str(error)[:200])
                report.append({"source_id": sid, "result": "failed: " + str(error)[:160]})
        self.store.sync_logs(force=True)
        return {"releases": report, "catalogue": catalogue, "outside_age_policy": outside,
                "excluded_nonrelease_sources": len(excluded), "http_requests": self.calls}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    mail = sub.add_parser("discover-mail")
    mail.add_argument("--since", default="30d")
    sub.add_parser("catalogue")
    collect = sub.add_parser("collect")
    collect.add_argument("source_id")
    batch = sub.add_parser("batch")
    batch.add_argument("--max-age-days", type=int, default=30)
    batch.add_argument("--report", help="optional JSON report path under the runtime state root")
    for command in (collect, batch):
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--allow-feedback", action="store_true")
    collect.add_argument("--max-assets", type=int, help="cap new transfers after normal version selection and dedup")
    args = parser.parse_args()
    store = Store()
    try:
        adapter = CtrlPromo(store)
        if args.command == "discover-mail":
            result = discover(store, args.since, [adapter.platform], sender_domain="ctrlpromo.com")
        elif args.command == "catalogue":
            result = adapter.discover_catalogue()
        elif args.command == "collect":
            result = collect_release(store, adapter, args.source_id, LibraryIndex(store), dry_run=args.dry_run,
                                     allow_gate=args.allow_feedback, max_assets=args.max_assets)
            store.sync_logs(force=True)
        else:
            result = adapter.batch(args.dry_run, args.allow_feedback, args.max_age_days)
            if args.report:
                from pathlib import Path
                path = Path(args.report).resolve()
                if store.root.resolve() not in path.parents:
                    raise ValueError("Report path must stay under the runtime state root")
                path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
