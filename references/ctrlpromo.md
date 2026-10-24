# CTRL Promo recipient adapter

`scripts/ctrlpromo.py` (platform `ctrl-promo`, class `CtrlPromo(store)`), with
synthetic offline contracts in `scripts/test_ctrlpromo.py`.
Status on 2026-10-24: sign-in, catalogue and release metadata are live-proven
browserless. Feedback and the original download route are built from the
website's own forms and scripts but not yet exercised live, because no mailed
promo is inside the 30-day window. CTRL Promo is rare: 3 mails in 180 days.

## Routes and sign-in

- Host `ctrlpromo.com`, a Laravel site. Signed storage is `cdn.ctrlpromo.com`.
- Mailed recipient link: `https://ctrlpromo.com/view-promo/<promo-id>/<key>?mailingId=<n>`.
  `recipient_link` accepts only that shape (key 16 to 512 characters, `mailingId`
  the only query) or a keyless `/view-promo/<promo-id>` catalogue source. Login,
  unsubscribe, tracker pixel and preview routes are never followed.
- Opening a keyed mailed link signs the recipient account in and 302s to its
  own `/view-promo/<promo-id>`. `sign_in` takes the newest mailed keyed link and
  refuses any other redirect. No password or 1Password item is needed.
- Session cookies live in runtime `ctrlpromo-session.dpapi`. A stale session is
  renewed once from a mailed link; a second refusal is AuthBlocked.

## Catalogue

The signed-in inbox `/promos` lists every promo of the account in one page.
`parse_catalogue` reads each card's promo id, title, state and published date
and checks the total against the navigation count, so a mismatch fails instead
of passing as complete. Catalogue promos with no mailed link become keyless
sources. Cached as `ctrlpromo-catalogue.dpapi`.

Live 2026-10-24: 2 of 2 promos, complete. 4193 "P La Cangri - Free Ride"
(Gutierrez Enterprises, published 30/07 2026, not responded) and 3399 (2024,
responded, files removed by the sender).

## Release metadata

The promo page gives the CSRF token and per-file format classes (which of WAV
and AIF the page offers for each file). `/view-promo/<promo-id>?info=1` returns
the release JSON. Signed URLs, waveform peaks and `secured_*` fields are dropped
before the snapshot is cached in `ctrlpromo-<promo-id>.dpapi`.

`normalize_release` rules:

- Label is `label.name`. `label.company_name` is the sending PR account, never used.
- The uploaded format comes from `promo_files.original_filename`. An uploaded MP3
  is taken as MP3, with a warning that the WAV and AIF beside it are transcodes.
  A lossless upload the page does not offer raises instead of falling back.
- `files_deleted`, a past `expire_date` or no tracks raise as unavailable.
- `gated` is true until the account has responded to the promo.

## Feedback and download

Feedback is one POST to `/new-comment/<promo-id>` with exactly `file_id`,
`content`, `rate` (out of 10) and `_token`. Play, playing time and "Not for me"
are never sent. The rating (7 to 9) and stock thanks comment are persisted per
source before sending, with pending, submitting and submitted states and an
intent event keyed on the promo id, so a lost response is never resent.
Success requires HTTP 200 with `success: true`. Only runs with `--allow-feedback`.

Download is `GET /download-promo/<promo-id>/<file-id>` with `wavfile` and
`aiffile` flags for the uploaded format. A redirect is followed only to
`cdn_original`: `cdn.ctrlpromo.com/data/promos_files/<promo-id>/<file>.<ext>`
with `md5` and `expires`, the extension matching the uploaded format, fetched
without the account cookies. A redirect to `/login` is AuthBlocked; html, json
or text responses raise as still locked. Size must match Content-Length.

## CLI and policy

`ctrlpromo.py discover-mail`, `catalogue`, `collect SOURCE_ID [--dry-run]
[--allow-feedback] [--max-assets N]`, and `batch [--dry-run] [--allow-feedback]
[--report <runtime-path>]`. Offer age comes from the mail, else the catalogue
published date, under the usual 30-day policy. Homepage and non-promo links are
excluded in reports.

## Verification 2026-10-24

- Offline: 16 synthetic tests pass, network blocked, `sync_logs` stubbed.
- Batch dry run (`ctrlpromo-dry-run-20261024.json`): every mailed source is 72
  to 86 days old, two homepage rows excluded, 1 HTTP request.
- Collect dry run on the 4193 mailed source: two MP3-original tracks planned
  as "download new mix", with transcode warnings.
- No pilot: nothing in the window, and the only open promo is commercial GQOM.
  The first in-window CTRL mail should be piloted with `--max-assets 1` to prove
  the feedback answer and the storage redirect.

No new package, program or sibling-skill dependency was added.
