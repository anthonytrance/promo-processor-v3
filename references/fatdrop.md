# FATdrop recipient adapter

`scripts/fatdrop.py`, with synthetic offline contracts in `scripts/test_fatdrop.py`.
Status on 2026-10-24: recipient session established, catalogue complete, original
route identified from live metadata and guarded. No audio has been transferred
live yet, because every in-window offer was already owned or needed a label
opt-in. Feedback stays closed. Do not describe FATdrop as a live-proven downloader.

## Recipient routes and authentication

- Exact mailed promo route: `https://promo.fatdrop.co.uk/e/<recipient-secret>`.
  The key is 16 to 512 ASCII letters, digits, underscores or hyphens. No query,
  fragment, userinfo or insecure scheme is accepted.
- The signed-in website uses `https://promos.fatdrop.co.uk/promo/<campaign-id>`.
  Campaign IDs are decimal and separate from recipient secrets.
- `promos.fatdrop.co.uk/s/<secret>?t=...` is login material, not a release.
  Homepage, help, subscription, confirmation and verification routes are never
  release candidates and stay excluded in batch reports instead of being opened.
- The DJ login is passwordless: an emailed link or PIN, or a social login. There
  is no recipient password, so 1Password cannot supply one (no op CLI is
  installed, and the 1Password UI was not opened for this).
- Session established 2026-10-24: a login link was requested for the configured
  promo mailbox (no CAPTCHA on the request form), read from the mailbox with
  BODY.PEEK, and opened in normal Chrome. The `/s/` page is a reCAPTCHA launch
  shell (`recipient_launch`); normal Chrome cleared it without a visible
  challenge, and no legal terms, subscription or account-creation page appeared.
- Runtime `fatdrop-session.dpapi` holds `identity` (the configured mailbox,
  confirmed by the API's logged-in answer) and a `cookies` list. Only cookies for
  `.fatdrop.co.uk`, `promo.` and `promos.` are stored or loaded. Renew it the same
  way when the API answers 401/403; never store cookies in the skill folder.

## Catalogue and completeness contract

Public website version `fatdrop-7.102.0` supplies these frontend contracts:

- `GET https://api.fatdrop.co.uk/api/v1/recipient/promos/stats` returns counts for
  all, new and old promos. `all` is the completeness count.
- `GET https://api.fatdrop.co.uk/api/v1/recipient/promos?filter=all&limit=50&offset=N`
  returns `campaigns` with stable decimal `id`, `name`, `album` and recipient
  `access`. Opened or reacted state is not an exclusion.
- The adapter advances offset by rows received, rejects duplicate or missing IDs
  and early empty pages, and rereads stats at the end. A changed count fails.
- Live 2026-10-24: 64 campaigns, count-checked complete, 4 HTTP calls.
- Each catalogue row becomes a protected `/promo/<campaign-id>` source. Undated
  offers are held by the 30-day policy; `access.created` is kept as evidence but
  is not yet treated as the offer date (see recommendations below).
- Recipient JSON: `GET https://promos.fatdrop.co.uk/promos/<id>?beta=1&basic=1&dmo=1`
  (`dmo=1` is the frontend's do-not-mark-open flag). Launch pages bootstrap
  `recipient/boot` and `promo/boot` JSON in a `.default(...)` call, which is
  decoded without running JavaScript and must carry the configured mailbox.

## Opt-in

`show_opt_in` true means FATdrop wants a label-list consent decision before the
promo opens. That is a subscription choice, so the adapter raises ContractReview
and never answers it. `accounts_not_opted_in` is only the count that prompt shows;
alone it is not a gate (this was a false block before 2026-10-24). Live example:
RIOT264 Frankyeffe "Desire" requires the opt-in and stays blocked.

## Tracks, original files and feedback

Tracks come from `playlist.tracks`: `track_id`, `artist`, `title`, `track_length`,
`allow_download`, `use_download_pref` and `urls`. Label is `LabelLocal.name` or the
album label, never the PR account. Duration uses the shared seconds parser.

Observed live `urls` for one track all live on audio hosts
(`audio<n>-<letter><n>.fatdrop.co.uk`) and share one audio-file id that differs
from `track_id`:

- `download`: `/download/<audio-id>/<uploaded file name>.<ext>`, no query. This is
  the uploaded original, and its extension is the source format.
- `download_pref`: the same path without the extension. It can render another
  container on demand and is never used.
- `streaming`: `/stream/<audio-id>/_`, never downloaded.

`original_url` accepts only HTTPS on an audio host, no userinfo, port, query or
fragment, path `/download/<audio-id>/<name>.(wav|aif|aiff|flac|mp3)`. The parser
offers that file only when its audio id equals the id in the same track's
`download_pref` and `streaming` routes. Everything else gets a review reason:
`downloads_disabled`, `zip_only_source_format_unverified` (download_flags bit 2),
`original_format_unverified` (preference route only) or
`original_endpoint_unverified` (page-host or relative routes, never seen live).

`download()` logs a `platform_download` intent and syncs logs before the GET,
streams with cookies only to FATdrop hosts, treats 401/403 as LoginRequired, and
rejects html, json, text or zip responses. Size must match Content-Length.

Feedback is required when `feedback_required_fields` is nonzero and
`access.has_left_feedback` is false. `unlock` still refuses: the POST to
`/feedback?k=<recipient-key>` has not been observed. Both in-window trance offers
already had feedback left, so no pilot needed it.

## CLI and reports

`fatdrop.py discover-mail` uses read-only BODY.PEEK discovery.
`fatdrop.py catalogue` needs the verified session.
`fatdrop.py collect SOURCE_ID --dry-run` plans one offer without audio.
`fatdrop.py collect SOURCE_ID --max-assets 1` is the only live form; a live run
without `--max-assets` is refused, and feedback is never sent.
`fatdrop.py batch --dry-run --report <runtime-path>` reports every source; batch
stays dry-run only.

Cached releases (`fatdrop-<source>.dpapi`), investigation captures and cookies
stay under the Store runtime root. A cache written by an older parser must be
removed with `os.remove` before a dry run to reparse.

## Verification 2026-10-24

- Offline: 16 synthetic tests pass, including the audio-id binding, query and
  foreign-host refusal, html refusal, intent before transfer, and the opt-in count.
- Batch dry run: 80 sources, 69 undated or too old, 9 excluded nonrelease links,
  2 planned: OUT312 Wavetraxx & HKL "Chains Will Break (Extended Mix)" [Outburst
  Records] and DJ San "Ikigai" [Fluentia Music], both one WAV original, ungated,
  and both already in the library (skip). 4 HTTP calls.
- No pilot: no in-window offer was both new and allowed. Live transfer, actual
  bit depth and link expiry remain unverified.

## Open recommendations (shared files)

- Use catalogue `access.created` as the offer date, or reconcile mailed `/e/`
  links with campaign ids, so catalogue-only offers such as RIOT264 are not held
  as undated.
- `batch` hard-codes `catalogue_complete` false; report the real catalogue result.
- The mail scan missed RIOT264, which reached the catalogue but not the mailed
  sources.

No new package, program or sibling-skill dependency was added.
