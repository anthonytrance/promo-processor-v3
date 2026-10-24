# Inflyte catalogue and availability verification

Checked by Codex on 2026-10-24, continuing Claude's session-limit handoff. Runtime evidence lives outside the skill in the promo state folder. No protected source URLs, cookies, signed file URLs or mail bodies belong here.

## Website mechanisms

- `GET /promos` embeds the full manager array in `initialData.promos`. The browser paginates and filters it locally. The signed-in normal Chrome showed 193 initial, manager and filtered records, no sender/search/bookmark filter, and two pages at 100 per page. The scripted manager and the independent `/review/subscriptions/live_promos` agreed on the same 193 IDs. `track_infos` receives the whole signed ID list in one request, never chunks of 50.
- `GET /review/releases/locker.json` is the website's Promo Locker inventory. Chrome and the script both saw 5,000 entries; the UI displayed 417 pages at 12 per page. The response has no next-page/cursor contract. Treat 5,000 as a possible server cap, not complete history. Its compact track fields are mapped by `locker_entry`; `w` is rating time, never a new offer date.
- `GET /search` supplies the Dashboard search index. Chrome loaded 460 total results including 333 promos. Selecting a promo calls the normal website route `/promos?show=<release-id>`.
- That show route adds the requested release to its initial array even when absent from the default manager. Both exceptional non-deleted releases were present in 194-entry show-route responses. Opening a promo then calls `/review/releases/<id>.json`, the same detail request the adapter uses. It may mark the release viewed, so publish an audit intent first.
- The account was verified in normal Chrome and the script as the configured promo mailbox. Both exceptional senders had accepted subscriptions. No account switch, consent, subscription change, delete or restore action was performed.

## Important availability distinction

The default manager is not the full history. In the initial 30-day comparison, 301 emailed release IDs included 118 absent from the live manager: 79 in the rated locker, 33 additional releases in Dashboard search, and six in neither index. A fresh read-only mailbox scan raised the recent emailed set to 310; every one had a known track snapshot, with none silently absent. Thirty-seven of the 39 detailed offers outside the locker carried the reviewer-side `deleted` flag. The app's delete action explicitly says it removes a release from the manager; it does not erase the underlying release.

Metadata access is not download proof. A live attempt on Rromarin's expired offer received an accepted rating but no download permission. The original-file endpoint returned to the recipient HTML page, which explicitly said the release was no longer available to rate or download. Normal Chrome showed the same banner and no playing audio. No audio was transferred in that attempt. One stock rating was saved; it must not be repeated.

`check_availability` reads the recipient HTML for offers outside the live manager before feedback or audio. An explicit expiry banner or recipient refusal becomes `ReleaseUnavailable` and appears in the end report, separately from unexpected failures. A JSON response, an available file type, a successful rating or a guessed cause for absence must never be reported as a verified download. A changed page schema remains an error, not an expired or successful offer.

## Selection and regression protection

- Keep the manager plus all recent mailed locker entries. Queue every other recent mailed ID for detail checking, even when absent from Dashboard search or the capped locker.
- Do not discard `deleted` releases before checking the actual recipient offer. Do not suppress a release by title alone. Compare each mix against the library and durable claims.
- Square-bracket mix names such as `Podium Extended Mix` and `AM Extended Mix` become parentheses before version selection and duplicate identity. They must remain distinct mixes. A matching trailing label is removed from the title so it does not hide a radio/extended marker.
- Dash-separated version suffixes are folded into a mix marker without changing dashes inside an existing remix name. This groups a vocal edit with its short version correctly.
- The standalone batch refreshes Inflyte mail first; the coordinator refreshes mail in prepare. Default scope remains 30 days from the newest known offer, with same-pass upgrades only and no main-library replacement.
- Offline tests cover the complete signed ID list, mailbox mismatch, live/locker/mail union, recent-offer bounds, published view intent, detail identity, explicit refusal, expiry before feedback, cached unavailable results and mix identity. Test runs block real HTTP and sockets.

## Final verification

The full pass checked all 118 recipient pages outside the manager and accounts for all 310 recent mailed IDs, plus one live-only offer. Six are explicitly expired: Reyus, two Bodyrox/Leena Punks campaigns, Roland Leesker, Space Hemp and Rromarin. Those six are exactly the six absent from all three indexes. The other 112 have locker or Dashboard-search coverage. There are no unexplained release omissions in the checked 30-day set. No recent Archive messages were found. Track inventory has no missing format lists or within-release canonical-key collisions after normalization.

Rosbeh's Brainbreak supplied successful original-transfer proof for an eligible locker offer missing from the manager. Its WAV became a hash-verified AIFF under the test folder, preserving 16-bit/44.1 kHz audio, with clean tags and no new feedback. Find My Home was already in the library and its shorter version was omitted. Staging was deleted after delivery.

The final network-blocked selection covers 852 track offers: 497 known/delivered skips, 274 unwanted shorter versions, 73 new mix candidates and eight lossless-upgrade flags. No uncertain claim remains; the temporary pilot-worker hold was reconciled after its verified completion. This is a selection report, not a full bulk download. All 210 offline tests pass with real network access blocked.

The metadata-only first dry run selected 107 records, including 86 outside the live manager; those were not counts of delivered or proven downloadable files. Runtime evidence: `inflyte-coverage.json`, `inflyte-gap-dry-run-20261024.log`, `inflyte-index-recheck.json`, `inflyte-official-route-check.json`, `inflyte-hidden-release-pilot-20261024.log`, `inflyte-availability-dry-run-20261024.log`, `inflyte-selection-verified-20261024.json` and `inflyte-locker-pilot-20261024.log`.
