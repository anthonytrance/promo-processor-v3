# NeedlePool recipient adapter

Implemented in `scripts/needlepool.py`, with network-blocked fixture tests in `scripts/test_needlepool.py`.
It reuses `Store`, protected runtime state and `collect_release`. No additional dependency was introduced.

## Auth-magic and the proper recipient catalogue

Only the exact `https://app.needlepool.com/.netlify/functions/auth-magic?t=` route may start a recipient session.
It can consume a one-use mail token, set the `np_portal` cookie and redirect to `/portal/campaign/<uuid>`.
The adapter saves the encrypted session before fetching the campaign, since a later page failure must not lose
the only reusable login. A consumed link redirects to `/link`, whose page says the link has already been opened.
The adapter records that access condition without pressing the fresh-email button.

Consent-confirm, register, subscription, portal-link-reissue, share-download-confirm, tracker wrappers and all
other routes are refused. They are not interchangeable with auth-magic. Redirects stay on the exact recipient host
and may end only on a recipient campaign. HTTPS, no userinfo and the normal HTTPS port are required.

A valid saved session opens `/portal?view=dj`, the same complete catalogue linked from recipient campaign pages.
Cards provide campaign UUID, artist, title and the sent date. The adapter requires the heading's total count to
equal the unique campaign-card count, otherwise it stops for pagination/schema inspection. Canonical catalogue
sources use the campaign UUID and preserve their sent date for the rolling age rule. Old auth links, consent
links and homepages are never processed again as separate releases. This also recovers campaigns whose emailed
one-use token has already been spent, without asking for another email or confirming consent.

In the 2026-10-24 metadata check, the catalogue contained two campaigns and both cards were visible. One emailed
token was already spent; another valid token established a session, and both campaigns remained available through
the recipient catalogue. This proves current account coverage for those two entries, not all historical mail.
The current HTML does not need a browser. No shared Chrome state was changed.

## Campaign metadata and original-file contract

Campaign HTML is server rendered. The Next.js Flight records in `self.__next_f.push` contain the player's explicit
`campaignId`, `releaseMeta`, `allowDownloads`, `requireFeedback`, `initialFeedback` and `tracks` props. The adapter
decodes JSON strings only and never executes JavaScript. It joins split Flight strings and requires exactly one
campaign-props object, then verifies its UUID against a requested canonical source.

Per-track `id`, `title`, `artist`, `duration_sec`, `has_wav` and `has_mp3` determine selection and quality. A track
title already prefixed with its artist is normalized once. AIF is a client-generated conversion of WAV on the
website, so the adapter fetches the original WAV and lets the shared processor create precision-preserving AIFF.
Unavailable downloads remain explicit, with empty file offers and a warning. Preview audio is never requested.

The official original URL endpoint is `POST /.netlify/functions/portal-audio-url` with campaign_id, track_id,
intent=`download`, format and one persisted anonymous session id. It creates a sender-visible download event.
Only after publishing that intent does the adapter request the original. Returned HTTPS storage URLs are checked
against the recipient host and explicitly allowed storage families, with no userinfo or custom ports. A separate
unauthenticated client streams storage, never the portal cookie. Each redirect is checked and HTML/JSON/text
responses are rejected. Unrecognised storage hosts stop for inspection before file transfer.

## Feedback, retries and delivery

Some campaigns require one feedback submission first. The shared pipeline's `--allow-feedback` guard applies.
`POST /.netlify/functions/portal-feedback` carries campaign_id, a persisted rating 7 to 9 and one stock thanks.
No planned-use, listening, support, follow-up answer or decline claim is invented. The UI has optional review
elaboration; if the server refuses the stock thanks, stop and report that actual contract rather than manufacturing
an opinion. An explicit JSON `ok: true` confirms submission. Encrypted response evidence is kept in runtime state.
A lost or unconfirmed response blocks resubmission, including through another mail/catalogue source alias.

No visit, play, site-beacon, dismiss, ZIP event or consent endpoint is called. The shared pipeline handles the full
selection/deduplication plan, claims, audio validation, processing, hash-verified test Dropbox delivery and zero
local retention. A single bounded pilot uses `collect <source-id> --allow-feedback --max-assets 1`, after its dry
plan is reviewed. The cap is applied after complete version selection and deduplication.

## Verification checkpoint, 2026-10-24

Metadata dry run: two catalogue releases, four new mix candidates, three HTTP metadata requests, no failures.
One release offers one WAV, another one WAV plus two distinct MP3-only remixes. The duplicate artist prefix on
the first release was found in that dry run and corrected with a regression test. No audio, plays or reviews were
sent. Runtime evidence: `needlepool-dry-run-20261024.json` and `needlepool-single-pilot-plan-20261024.json`.
Feedback acceptance, original transfer and verified Dropbox delivery still need the separately recorded pilot.

## Live pilot, 2026-10-24

`collect ab7949f9bdfa1ec9d15e --allow-feedback --max-assets 1` after reviewing the dry plan. The campaign had
`requireFeedback: true` and no earlier feedback, so one stock review was submitted (rating 9, "Thanks for sending this
over, good luck with the release.") and confirmed with `ok: true`. The portal-audio-url response pointed at an allowed
storage family and the original streamed through the unauthenticated client. Delivered and hash-verified in the test
folder: JAZOV - I'm Trying (Original Mix) [Droomschipp].aiff, from WAV, clean tags. Beatport track 30950653 (catalogue
DSRDIGI093, released 2026-10-16) confirms Droomschipp. The two MP3-only remixes (TYGR TYGR, GambaTrax) were deferred by
the one-asset cap and remain for a later batch; they will not need another review. Staging was empty afterwards.
Runtime log: `needlepool-single-pilot-20261024.log`.
