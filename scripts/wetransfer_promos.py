"""WeTransfer recipient pages, with explicit expiry/access reporting and single originals.

Metadata reads use the official page's __NEXT_DATA__. Download requests select one
file ID and intent=single_file, never Download all, preview audio, or transfer recovery.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from onedrive_promos import (DirectSharePromos, ShareAccessRequired, ShareUnavailable,
                            checked_url, run_cli, safe_name)
from promo_cloud import protect_json, read_protected_json

PAGE_HOSTS = ("we.tl", "wetransfer.com")
ORIGINAL_HOSTS = ("download.wetransfer.com", "download.wetransfer.net")
ID = re.compile(r"[A-Za-z0-9_-]{6,128}")
ACTIVE_STATES = {"downloadable", "downloading", "finished"}
UNAVAILABLE_STATES = {"expired", "deleted", "aborted", "blocked", "invalid", "spam", "infected"}


def recipient_path(url):
    parsed = checked_url(url, PAGE_HOSTS)
    if parsed.hostname == "we.tl":
        if not re.fullmatch(r"/t-[A-Za-z0-9]+/?", parsed.path):
            raise RuntimeError("WeTransfer short recipient URL is incomplete")
        return
    parts = parsed.path.strip("/").split("/")
    if len(parts) not in (3, 4) or parts[0] != "downloads" or not all(ID.fullmatch(p) for p in parts[1:]):
        raise ShareAccessRequired("WeTransfer recipient link left its download page, access review is required")


def transfer_from_page(url, body):
    """Validate IDs against the actual recipient route before trusting its bootstrap."""
    recipient_path(url)
    soup = BeautifulSoup(body, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        raise RuntimeError("WeTransfer recipient page has no supported metadata schema")
    try:
        props = json.loads(script.string or script.get_text())["props"]["pageProps"]
        transfer = props["initialTransfer"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("WeTransfer recipient page metadata is missing or changed") from None
    if not isinstance(transfer, dict):
        raise RuntimeError("WeTransfer recipient page has no transfer object")
    parts = urlsplit(url).path.strip("/").split("/")
    expected_id, expected_hash = parts[1], parts[-1]
    recipient_id = parts[2] if len(parts) == 4 else None
    if (transfer.get("id") != expected_id or transfer.get("security_hash") != expected_hash
            or (transfer.get("recipient_id") or None) != recipient_id):
        raise RuntimeError("WeTransfer metadata does not belong to this recipient link")
    return transfer


def availability(transfer):
    state = transfer.get("state")
    if state in UNAVAILABLE_STATES or transfer.get("deleted_at"):
        raise ShareUnavailable("WeTransfer transfer is explicitly " + str(state or "deleted"))
    expires = transfer.get("expires_at")
    if expires:
        try:
            parsed = datetime.fromisoformat(expires.replace(" UTC", "+00:00").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("no timezone")
        except (ValueError, AttributeError):
            raise RuntimeError("WeTransfer expiry timestamp has an unsupported schema") from None
        if parsed <= datetime.now(timezone.utc):
            raise ShareUnavailable("WeTransfer transfer's explicit expiry time has passed")
    if transfer.get("password_protected"):
        raise ShareAccessRequired("WeTransfer transfer is password protected")
    if transfer.get("downloader_email_verification") not in (None, "anonymous"):
        raise ShareAccessRequired("WeTransfer transfer needs recipient identity verification")
    if transfer.get("paid") or transfer.get("encrypted") or transfer.get("force_watermark"):
        raise ShareAccessRequired("WeTransfer transfer needs a paid, encrypted or watermark access workflow")
    if transfer.get("view_option") in {"preview_only", "preview_only_with_watermark"}:
        raise ShareAccessRequired("WeTransfer sender disabled original downloads")
    if state not in ACTIVE_STATES:
        raise ShareAccessRequired("WeTransfer transfer is not ready for original download (" + str(state) + ")")


class WeTransferPromos(DirectSharePromos):
    platform = "wetransfer"

    def _validate_inventory(self, inventory):
        super()._validate_inventory(inventory)
        if (inventory.get("kind") != "transfer" or not ID.fullmatch(str(inventory["share_key"]))
                or not isinstance(inventory.get("availability"), dict)
                or not isinstance(inventory["availability"].get("state"), str)):
            raise RuntimeError("WeTransfer metadata cache has an invalid transfer identity")
        if any(not ID.fullmatch(entry["id"]) for entry in inventory["files"]):
            raise RuntimeError("WeTransfer metadata cache has an invalid file identity")

    def _transfer(self, source_id):
        location, status, headers, body = self.http.page(self.store.source_url(source_id), PAGE_HOSTS, recipient_path)
        if status in (404, 410):
            raise ShareUnavailable("WeTransfer recipient page is unavailable")
        if status in (401, 403):
            raise ShareAccessRequired("WeTransfer recipient page needs account or access verification")
        if status != 200:
            raise RuntimeError("WeTransfer recipient page HTTP " + str(status))
        transfer = transfer_from_page(location, body)
        # Store the full provider result encrypted even when it proves expiry.
        protect_json(self.store.root / ("wetransfer-status-" + source_id + ".dpapi"),
                     {"url": location, "transfer": transfer, "inspected_at": datetime.now(timezone.utc).isoformat()})
        availability(transfer)
        return transfer

    def snapshot(self, source_id, refresh=False):
        if not self.store.db.execute("SELECT 1 FROM sources WHERE id=? AND platform=?", (source_id, self.platform)).fetchone():
            raise ValueError("Unknown WeTransfer source")
        cache = self.store.root / ("wetransfer-" + source_id + ".dpapi")
        if source_id in self.inventories and not refresh:
            inventory = self.inventories[source_id]
            availability(inventory["availability"])
            return inventory
        if not refresh:
            inventory = self._read_inventory(source_id, cache)
            if inventory is not None:
                availability(inventory["availability"])
                self.inventories[source_id] = inventory
                return inventory
        self.store.event(source_id, "view", "intent", platform=self.platform,
                         platform_effect="Official recipient metadata only; no download preparation or audio transfer")
        self.store.sync_logs()
        try:
            transfer = self._transfer(source_id)
        except (ShareUnavailable, ShareAccessRequired) as error:
            self.store.event(source_id, "view", "unavailable" if isinstance(error, ShareUnavailable) else "access_required",
                             platform=self.platform, reason=str(error))
            raise
        items = transfer.get("items")
        if not isinstance(items, list) or len(items) != transfer.get("files_count"):
            raise RuntimeError("WeTransfer page has an incomplete transfer inventory")
        if len(items) > 10000:
            raise RuntimeError("WeTransfer transfer exceeded its bounded file count")
        files, seen = [], set()
        for item in items:
            file_id, size = item.get("id"), item.get("size")
            if not isinstance(file_id, str) or not ID.fullmatch(file_id) or file_id in seen:
                raise RuntimeError("WeTransfer original file id is missing, repeated or unsupported")
            seen.add(file_id)
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise RuntimeError("WeTransfer file metadata has no valid original size")
            if item.get("item_type") not in (None, "file"):
                raise RuntimeError("WeTransfer item type requires a separate inventory workflow")
            files.append({"id": file_id, "name": safe_name(item.get("name")), "size": size,
                          "sha256": item.get("sha256"), "md5": item.get("md5"),
                          "is_downloadable": True})
        inventory = {"name": transfer.get("title") or transfer.get("display_name") or transfer.get("recommended_filename") or "WeTransfer promo",
                     "share_key": transfer["id"], "files": files, "file_count": len(files), "kind": "transfer",
                     "availability": {k: transfer.get(k) for k in ("state", "expires_at", "deleted_at", "password_protected",
                         "downloader_email_verification", "paid", "encrypted", "force_watermark", "view_option")}}
        self._save_inventory(source_id, cache, inventory)
        self.inventories[source_id] = inventory
        self.store.event(source_id, "view", "inspected", platform=self.platform, files=len(files), requests=self.calls,
                         note="Complete recipient inventory, no original or preview transfer")
        return inventory

    def download(self, source_id, file, destination):
        # Fresh expiry and access check before the sender-visible download request.
        transfer = self._transfer(source_id)
        entry = file["entry"]
        matching = [x for x in transfer.get("items", []) if x.get("id") == entry["id"]]
        if len(matching) != 1 or any(matching[0].get(key) != entry.get(key)
                                     for key in ("id", "name", "size", "md5", "sha256")):
            raise RuntimeError("WeTransfer original changed since inventory; refresh and re-plan")
        payload = {"security_hash": transfer["security_hash"], "intent": "single_file", "file_ids": [entry["id"]]}
        if transfer.get("recipient_id"):
            payload["recipient_id"] = transfer["recipient_id"]
        self.store.event(source_id, "platform_download", "intent", platform=self.platform, file_id=file["id"],
                         platform_effect="Single original download request; may count or notify the sender")
        self.store.sync_logs()
        # This is the current official web client's endpoint and single-file contract.
        result = self.http.json("https://wetransfer.com/api/v4/transfers/" + transfer["id"] + "/download",
                                ("wetransfer.com",), method="POST", json=payload,
                                headers={"Accept": "application/json", "Content-Type": "application/json"})
        location = result.get("direct_link")
        if not isinstance(location, str):
            raise RuntimeError("WeTransfer download response has no original direct link")
        return self.http.original(location, destination, ORIGINAL_HOSTS, entry["size"],
                                  sha256=entry.get("sha256"), md5=entry.get("md5"))


if __name__ == "__main__":
    run_cli(WeTransferPromos)
