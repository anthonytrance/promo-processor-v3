# BeatRolo recipient adapter

Implemented in `scripts/beatrolo.py`, with network-blocked fixture tests in `scripts/test_beatrolo.py`.
Use the shared runtime `Store` and pipeline. Protected recipient URLs, cookies, API responses and credentials stay
DPAPI encrypted in runtime state, outside this skill. No additional dependency was introduced.

## Recipient access and coverage

The mailed `https://www.beatrolo.com/p/<token>?r=<recipient>` page is a React shell. Its public client calls
`GET /api/promos/public/<token>?r=<recipient>` for complete release JSON. No browser, password or played preview
is needed for a mailed promo. The adapter verifies the recipient mailbox against the configured mailbox and
checks the explicit expiry date before any feedback. HTTPS, the exact platform hosts, no userinfo and the normal
HTTPS port are required. API redirects are refused.

Metadata contains `recordLabel`, `labelName`, `primaryArtistName`, release title, catalogue number, explicit expiry,
recipient feedback state and a complete `tracks` array. Each track supplies a stable id, title, original format,
duration and file size. The original per-track route is
`GET /api/promos/public/<token>/tracks/<id>/download?r=<recipient>`.
Legacy single-file offers use `hasDownload`, `mixTitle`, `audioDurationMs`, `audioFormat` and `legacy-download`.
Downloadable tracklist entries carry `downloadWav` or `downloadMp3` and use `tracklist-download?format=&index=`.
The newer track route has live metadata proof. Legacy and tracklist normalization/routes are fixture tested,
not yet live download proven. Duplicate downloadable names across schemas stop for inspection.

The client also calls an outstanding-promos endpoint, but the observed response omits the per-promo recipient
tokens needed to construct extra recipient links. Do not assume the initial mail or outstanding list is the whole
inbox. A saved recipient portal session enables `GET /api/portal/inbox`, the same full array used by the website's
client-side All/New/Downloaded/Reviewed filters. `publicToken`, `recipientToken`, `episodeTitle` and `sentAt` give
recipient links and newest-offer dates. Existing mail sources are reused to preserve message dates and feedback.
The adapter explicitly reports when no saved portal session is available, and counts inbox entries missing their
recipient links. Full portal coverage remains unverified until a recipient login is available.

`login <credential-file>` accepts a runtime DPAPI JSON file with `email` and `password`, uses `/api/portal/login`,
and stores only the resulting cookies in `beatrolo-session.dpapi`. It never initiates a registration, password
reset or subscription change. There is no ordinary browser dependency once recipient links or a session exist.

## Feedback and originals

The public client requires one review to unlock originals. Its `POST .../review` payload has rating 1 to 10,
feedback, reviewer name/email and recipientToken. The adapter uses one persisted rating 7 to 9 and a stock thanks.
It omits wouldPlay and favourite-track claims. No decline is submitted. The shared pipeline requires explicit
`--allow-feedback`, and dry runs never call this endpoint. Review intents and download intents are published
before their side effects.

The website regards HTTP success as acceptance. The adapter additionally requires nonempty valid JSON without
error or negative-success fields, rejects HTML, and encrypts the response for reconciliation. A lost response or
unconfirmed result remains `submitting` and blocks a repeat. The release id in the intent links alternate source
IDs, so a catalogue alias cannot submit the same review twice. Confirmed feedback updates the encrypted metadata
cache. No website view/play/track-download tracking POST is sent separately.

Downloads stream the official original endpoint, reject HTML/JSON/error statuses and unverified storage redirects,
and check byte counts. The track route answers HTTP 302 to `https://www.eburgmedia.com/beatrolo/download_audio.php?token=`.
EBURGMEDIA is BeatRolo's owner (the client footer reads "BeatRolo / EBURGMEDIA" and links eburgmedia.com).
`original_storage_url` accepts only that exact path on eburgmedia.com or www.eburgmedia.com, over HTTPS on the normal
port, without userinfo and with a nonempty token. Storage is fetched by a separate client with no portal cookies or
recipient headers, redirects are not followed automatically, and every further hop is checked again. The shared pipeline handles mix selection, library deduplication, claims, precision,
tagging, WAV to AIFF, hash-verified test Dropbox delivery and staging cleanup. A bounded pilot uses
`collect <source-id> --allow-feedback --max-assets 1`, only after reviewing the dry plan. The cap is applied after
full version selection and deduplication, so it cannot make a radio edit win by hiding its extended sibling.

## Verification checkpoint, 2026-10-24

Live metadata dry run: five mailed releases, five new WAV extended mixes, zero metadata failures, five API
requests. No review, audio or preview was submitted by that dry run. Library decisions used the freshly refreshed
full library index. Runtime evidence: `beatrolo-dry-run-20261024.json` and `beatrolo-single-pilot-plan-20261024.json`.
The original transfer/delivery acceptance and full recipient inbox still need their separately recorded proof.

## Live pilot, 2026-10-24

`collect 06ec17c8febf2de91b9d --allow-feedback --max-assets 1` proved the whole route. The first attempt submitted the
single unlock review (HTTP 201, `success: true` with a reviewId, rating 9, stock thanks, no would-play claim) and then
stopped at the unverified 302 without fetching audio. After the storage route was verified, that asset's claim was
released deliberately with this reason and the rerun used the persisted `submitted` feedback state, so no second review
was posted (feedback events stayed at two: one intent, one submitted). Delivered and hash-verified in the test folder:
Mike EFEX - Free Your Mind (Extended Mix) [Coldharbour Recordings].aiff, from a 16-bit/44.1 kHz WAV, clean tags.
Beatport track 30969691 (catalogue CLHR689) confirms Coldharbour Recordings. Beatport spells the artist "Mike Efex".
Staging was empty afterwards. Runtime log: `beatrolo-single-pilot-rerun-20261024.log`.
Still unproven live: legacy and tracklist download routes, and full portal inbox coverage.
