# OneDrive and WeTransfer recipient shares

Implemented in `scripts/onedrive_promos.py` and `scripts/wetransfer_promos.py`. Both expose the shared
`snapshot`, `release`, `download`, `batch`, `platform`, `calls` and `close` contracts and use the normal
claim, mix selection, deduplication, processing, research hold and verified Dropbox pipeline.
Constructors do not initialize a Dropbox source client. They use existing `curl_cffi` and `beautifulsoup4`,
with no new adapter dependency or hard-coded machine path.

Commands: `collect SOURCE_ID --dry-run`, `collect SOURCE_ID --max-assets 1`, `batch --dry-run`,
and `discover-mail --since 30d`. A pilot uses the one-asset cap after complete version selection.

## OneDrive

Only exact recipient links already supplied in mail are used. Modern `onedrive.live.com` links may
contain a base64-encoded `redeem` value holding the original `1drv.ms` recipient link. Decode that value,
validate its provider host, and encode the complete original recipient URL as Microsoft's `u!` sharing token.
Do not drop its access key or guess another link from the item path.

First open the supplied page in one short-lived anonymous HTTP session, following only HTTPS OneDrive
redirects. This seeds the anonymous sharing cookies. Migrated Personal shares expose `_spPageContextInfo`
and use the native `https://onedrive.live.com/_api/v2.0/shares/{sharing-token}/driveItem` endpoint. Their
legacy `api.onedrive.com/v1.0/shares/.../root` request can return `unauthenticated` even when the recipient
website works anonymously. Non-migrated Personal shares use that legacy endpoint.

List the root's `children`, follow every `@odata.nextLink` within that exact collection, and recurse into
folder IDs returned by that listing. Migrated shares do not reliably support path-addressed child metadata.
Use the root's `parentReference.driveId` and the listed child ID with the native `drives/{drive-id}/items/{id}`
route under the same cookie context. Reject remote shortcuts, drive changes, duplicate IDs, unsafe names,
pagination loops, more than 100 pages per folder, more than 10,000 total items, and depth beyond eight.
No account-wide listing, login or durable permission grant is needed or attempted.

Each adapter instance refreshes its full metadata inventory once. Repeated calls in that operation reuse
the in-memory snapshot; encrypted disk snapshots are evidence, not indefinite caches. Reused folders are
therefore re-inventoried on the next operation. Snapshots contain no audio. Metadata durations are used
when offered. Dry runs never probe original audio headers. Explicit Camelot-key/BPM prefixes and an
underscore-delimited artist/title/extended-or-radio suffix are normalized for correct version selection.
Unmarked ambiguous names remain subject to the normal review rules.

Before transfer, reopen the recipient context and refresh the selected original's item metadata. Require
the inventoried ID, size, SHA-1/SHA-256 (when offered), eTag and cTag to agree. Obtain the original URL afresh,
never a preview/transcode URL. Allow only the official OneDrive content hosts through bounded redirects.
Native item responses may first return an `onedrive.live.com` original route that redirects to
`microsoftpersonalcontent.com`; both are required. Stream with an exact byte limit, check every supplied
hash, calculate SHA-256, and publish atomically. Missing access and unexpected hosts fail explicitly.

Live metadata proof, 2026-10-24: a migrated anonymous folder listed all five items, two MP3 mixes and three
press/art/support files. The dry run selected its extended mix and omitted the radio edit with zero audio
bytes transferred. The root, folder listing and item-ID original metadata route were checked against the
real recipient share. No browser or account login was used. A supplied 177 KB press PDF was read in memory
after a published `original_metadata` intent, using the already-installed optional `pypdf` only for this
one-off investigation, not as an adapter dependency. It confirmed track/version metadata but no imprint.

Single-original pilot, 2026-10-24: exactly one extended MP3 original transferred, 9,985,314 bytes,
44.1 kHz stereo, 247.510178 seconds. OneDrive offered no cryptographic source hash, so the adapter checked
the exact advertised size and unchanged provider revisions and calculated SHA-256. The shared pipeline
fully decoded the original, kept MP3 format, and uploaded a hold because the actual imprint was absent
from both its original tags and the press PDF. Independent Dropbox metadata readback matched the
pipeline's content hash and found the research note. Staging was deleted. This proves original transfer
and verified hold delivery; it does not claim final metadata acceptance. No radio or second audio file
was downloaded. Runtime evidence is `direct-shares-verification-20261024.json` and the single-pilot log
outside the skill folder.

## WeTransfer

Open only the exact `we.tl/t-...` short recipient link or its official `wetransfer.com/downloads/...` page.
Never follow registration, consent, unsubscribe, recovery, payment or tracking paths. Parse the official
recipient page's `__NEXT_DATA__.props.pageProps.initialTransfer` and require transfer ID, security hash
and optional recipient ID to match the actual URL. There is no public transfer search.

An explicit expired/deleted/blocked state is unavailable, not an empty successful inventory. Check the
expiry timestamp and download/access state again immediately before an original request. Password,
identity-verification, paid/encrypted, CAPTCHA and preview-only cases require an explicit separate access
workflow; this adapter does not bypass them. Save the complete status response encrypted even for expiry.
Require the listed items to match `files_count`; incomplete inventories fail instead of silently dropping files.

For an eligible original, the official web client uses
`POST https://wetransfer.com/api/v4/transfers/{transfer-id}/download` with `security_hash`, optional
`recipient_id`, `intent: "single_file"` and `file_ids: [selected-id]`. This is a sender-visible download
action and is never sent during a dry run. The returned `direct_link` is streamed only from allowlisted
WeTransfer download hosts, with exact inventory size and any explicitly supplied MD5/SHA-256 checked.
Never use Download all, `single_file_preview`, recovery, or preview notifications.

Live metadata proof, 2026-10-24: the current historical recipient page explicitly reports `state=expired`
and deletion on 2026-10-09. Its live CLI dry check returns unavailable. No prepare-download, original,
preview, verification or recovery request was sent. The single-file request contract is verified in the
current official public JavaScript and offline fixtures. An unexpired recipient original still needs a
live transfer/delivery acceptance test, it is not claimed proven by the expired page.

## Limits and offline verification

ZIP/RAR entries are reported as pending archive inventory. They are not silently transferred during
metadata discovery or dry runs. Per-member planning of a previously unopened archive needs an explicitly
approved archive transfer and the existing bounded extractor. Neither current live source needs that path.

All 22 isolated tests pass. They block all real curl, requests and socket connections, stub `store.sync_logs`, and forbid
construction of the real Dropbox source client. Fixtures cover recipient URL validation, native and legacy
contexts, recursive/paginated inventory, scope/loop checks, duplicate IDs, safe names, multi-format merging,
version selection, zero-transfer dry runs, explicit archive deferral, stale original rejection, exact original
sizes/hashes, atomic cleanup, redirect host checks, expired/access-required states, complete WeTransfer item
counts and the single-file download payload.

## Primary references, fetched 2026-10-24

- Microsoft sharing URL encoding and access: https://learn.microsoft.com/en-us/graph/api/shares-get?view=graph-rest-1.0
- Microsoft child collections and pagination: https://learn.microsoft.com/en-us/graph/api/driveitem-list-children?view=graph-rest-1.0
- Microsoft original download URLs and ranges: https://learn.microsoft.com/en-us/graph/api/driveitem-get-content?view=graph-rest-1.0
- Microsoft native API endpoint differences: https://learn.microsoft.com/en-us/onedrive/developer/rest-api/concepts/direct-endpoint-differences?view=odsp-graph-online
- WeTransfer recipient downloads: https://wetransfer.com/help-center/how-to/download-a-transfer
- WeTransfer expiry: https://wetransfer.com/help-center/how-to/transfer-availability
- WeTransfer recipient identity checks: https://wetransfer.com/help-center/how-to/track-downloads-access-control
- The current official recipient page and public `cdn.wetransfer.com` JavaScript establish the internal
  single-file endpoint/payload. Versioned asset names and protected recipient URLs stay in encrypted runtime evidence.
