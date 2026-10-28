---
name: promo-processor-v3
description: "Discover and collect DJ promos from platform/email links, deduplicate, preserve audio precision, tag and rename, research unknowns, and verify Dropbox delivery."
---

# Promo Processor v3

Process incoming DJ promo music into a clean, consistently named and tagged library. The heavy lifting is done by a Python script; the AI handles only what requires judgment (unknown labels, missing artists, possible bootlegs).

## Shared Progress and Automation Work

## Setup

`python install.py --mail-account <mailbox> --destination "<Dropbox folder>"` installs the sibling skills, Python packages and programs listed in `dependencies.json` and writes the settings file, `%APPDATA%/promo-processor/settings.json` (or the file named by `PROMO_SETTINGS`). Its keys: `mail_account` (the mailbox promos arrive in, as named in the email skill's credentials), `destination` (Dropbox folder receiving finished promos), `library_root` (Dropbox folder holding the whole library, for the duplicate check), `state_dir` (local database, logs and saved logins), and optionally `credential_service` (keyring service name), `email_credentials` and `dropbox_credentials` (credential file paths), and `subject_note_words` (words in a promo subject's brackets that are never a label, such as the user's radio show). Then `python scripts/promo_doctor.py` verifies mail, Dropbox, Google and a saved SoundCloud session, and checks other platforms for saved logins locally; `promo_doctor.py save-password promo-cloud|promobox` and `save-soundcloud` store a login with hidden typing. SoundCloud needs both access and refresh tokens, preferably from its own automation login; it renews without Chrome on routine runs. See [SoundCloud session renewal](references/soundcloud.md). Below, "the state folder" is `state_dir` and "the test folder" is `destination`. The pre-commit hook in `.githooks` runs `tools/check_secrets.py`; keep it on.

Before working on this skill, open the settings file to find the state folder (`state_dir`), then read `notes/automation-progress.md` in it, and `notes/operator-notes.md` beside it for decisions that apply only to that installation. Both are private to the computer and never committed. The progress file is the shared project record for this skill across agents and sessions. Update it when starting work, after material findings or changes, and before handing off or ending a session. Record the current owner and files being changed, verified results, blockers, and the next concrete action. For ordinary processing runs, keep the update to a compact run summary and report path. Keep detailed per-track state in processing reports or the runtime database, not in this document. Re-read the shared record before editing it when another agent may have changed it.

Also read the [standing rules](references/standing-rules.md) at the start of every session and check them before finishing any code change: a new package, program or sibling skill goes into the dependency lists, no hard-coded user paths, no secrets in the skill folder, tests never touch real accounts.

For promo platform workflows, prefer reusable CLI/API scripts and compact structured results. Use the user's normal Chrome for necessary login and bounded protocol discovery, then script routine discovery, feedback, downloads, and verification where supported. Avoid repeated screenshots, full page dumps, and accessibility-tree inspection when a semantic request or targeted page query can provide the result. Record the remaining browser dependency and measured call cost for each platform. Feedback wording must vary appropriately and remain consistent with the user's instructions; persist the chosen text and submission state so a retry does not generate and submit another comment.

Live-proven downloaders: Promo Cloud (`scripts/promo_cloud.py`), Inflyte (`scripts/inflyte.py`), Promoly (`scripts/promoly.py`) and Ampsuite (`scripts/ampsuite.py`). They reuse `scripts/promo_pipeline.py` for selection, claims, original-file downloads, audio/tag validation and hash-verified Dropbox delivery. A Dropbox shared-link adapter is also implemented; the source backlog (`notes/source-backlog.md` in the state folder) and the shared checkpoint give their exact live verification limits. [Promoly notes](references/promoly.md) and [Ampsuite notes](references/ampsuite.md) document their recipient APIs. Measured timings are in `notes/performance.md` in the state folder. [FATdrop notes](references/fatdrop.md) and [CTRL Promo notes](references/ctrlpromo.md) cover two adapters whose metadata is live-proven but whose audio transfer is not yet. Other sources remain on the backlog.

Use `collect --dry-run` on intended releases first. A new-release dry run may inspect/open a platform page; it guarantees no audio transfer or feedback, not zero platform side effects. Inflyte catalogue discovery is separately proven not to open releases. Inflyte only needs Chrome to refresh login on TokenExpired, followed by `inflyte.py save-token`; routine runs use its protected cookie. What is authorized (bulk collection, schedules, messages to collaborators, library moves) is the user's decision per installation and recorded in the operator notes; nothing is authorized by default. Keep each source's implemented, offline-tested, dry-run and live evidence distinct.

Inflyte coverage uses its complete manager array, its rated locker, and recent mailed release IDs. The manager's page size is a browser filter, not server pagination. `inflyte.py coverage` inventories these without opening individual releases. Collection and `batch --dry-run` check recipient pages for offers outside the live list. A missing manager/search entry is not proof of expiry, and a successful release JSON response is not proof of download availability: Inflyte can serve metadata and accept a rating for an expired promo. The original recipient HTML gives an explicit expiry message, checked before feedback. Reports list these as `unavailable_releases`; unexpected failures remain failures. See [Inflyte verification](references/inflyte.md). Each feed also verifies the signed-in mailbox before collection.

Mail discovery reports `platforms_without_adapter` and candidate `unrecognised_promo_mails` without following their links. The update summary includes those warnings. Newsletter, tracker, social/store and homepage footer links are filtered; a new delivery host beside a supported link is still reported. Promo Delivery (`scripts/promodelivery.py`) is implemented with offline checks and a live metadata dry run; original ZIP download/delivery acceptance is still pending. The coordinator runs it with every other adapter.

Default selection, per mix: the version marked extended is the main one; without it, the longest version is treated as the extended, and without lengths an Original Mix or unmarked version outranks anything marked radio, edit or short. DJ Edits, and any edit over five minutes, stay beside it. Shorter radio, short and unmarked versions are dropped. A single offered version is always taken, a short radio edit included. Review only when two versions cannot be told apart by mark, name or length. Promo Cloud, Inflyte and PromoBox give lengths; Dropbox reads them from the file header. Preserve intro and alternative mixes. Flag lossless upgrades of existing MP3s at the end as upgrade_candidates_ask_user; do not download them automatically. Never replace or re-deliver a track from an earlier pass or from the main library. Exception while testing: the whole test folder counts as one pass (`TEST_FOLDER_IS_ONE_PASS` in promo_automation.py; set it to False when delivery moves to Latest Promos). Reason: DJs sort promos by date added, created and modified and copy them into their selections, so an upgraded copy looks like a new promo and becomes a duplicate there. A new "update the promos" is always a new pass. Within one pass (one "update the promos" run, `promo_automation.py start-pass` or the coordinator's start time) a lossless offer replaces a lossy copy the same pass delivered or held: it is downloaded and processed, and the old file and its research note are deleted only after the new one is hash-verified. No upgrade flag in that case. Batch age limit is 30 days since the newest known offer. Decide per mix, since a title-only match can hide a new remix. All test audio and logs go to the test folder.

Hold missing/conflicting artist, title or label, unknowns and required-tag failures as needs_review. A PR sender or group name does not prove the track's actual imprint. Research those cases using Phase 2 and update the label catalogue. Zero local retention: each release's staging folder is deleted when its collection ends, success or failure, and a failed asset is released to download again on the next run. Review holds go to Dropbox `Needs Research` (see below), never to local disk. Never release an uncertain claim blindly. `scripts/promo_status.py --save` reports live state; its narrow Unicode-upload reconciliation reads Dropbox only. `scripts/promo_update.py` is the on-demand all-adapter test coordinator (see "Update the promos" below). It is not scheduled. Its final audit must cover every test audio file against every folder under `library_root`, check filename/tag agreement and actual label correctness, and verify WAV inputs became AIFF while preserving precision. The verified checkpoint and source records distinguish implemented code from live proof. Whole-week all-source orchestration remains a goal.

## "Update the promos"

If the user asks for a completion notification, `scripts/promo_watch.py --run-dir <existing-run> --chat-id <approved-recipient>` watches that run once and sends a plain Telegram completion/failure summary, then exits. Launch it detached, without a console window, with the Telegram bridge's existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_IDS` environment, and verify its actual PID/heartbeat in `<run>/completion-watch.json`. No credentials go on the command line or into state. It checks terminal coordinator state, not just an early audit or summary file; uncertain notification delivery is never automatically duplicated. This is a bounded one-time notification, not a recurring promo schedule or an automatic new agent turn. Do not promise an agent wake-up from it.

When the user asks to update the promos, run the whole coordinator once, detached and without a console window, with the venv Python and `PYTHONUTF8=1`:

`scripts/promo_update.py` (no arguments; it creates `runs/<timestamp>/` in the state folder)

Every run is a new pass by default, and nothing from an earlier pass is replaced. Add `--continue-pass` only when the user says they have not processed the previous run's promos yet (for example "I haven't gone through the last batch"): the run then joins the previous pass, so a lossless copy may replace a lossy one it delivered, and the summary says which run it continued. Passes chain, so a continued pass reaches back to the last run started without the switch. If it is unclear, ask before launching. While delivery still goes to the test folder, `TEST_FOLDER_IS_ONE_PASS` already treats the whole test folder as one pass.

Phases, each a fresh subprocess with its own log in the run folder: prepare (library index plus one 30-day mail scan over every adapter), discover (a dry run of every source in up to four parallel lanes, writing `<platform>-plan.json`; account catalogues refresh here, adapters with `campaigns()` choose their own sources, others use the 30-day newest-offer filter), a global plan (`global-plan.json`: when two sources offer the same mix, only the best copy is collected, lossless first, then the platform order in `scripts/promo_adapters.py`; the rest are deferred), collect (parallel lanes; idle releases get no second request; Dropbox uploads share `PROMO_UPLOAD_SLOTS`, default 2), library refresh, reconcile (retries failures and ignores deferrals, so a failed best copy is fetched from the next source), final audit, `promo_research.py --deliver`, a second audit, then `summary`. A login or token failure stops requests to that platform for the rest of the run but every release is still reported; explicit unavailability is not a failure. An adapter with `collection_enabled = False` is planned only. `--platforms a,b` limits a run, `--lanes` sets parallelism (1 to 6). Every adapter is registered once in `scripts/promo_adapters.py`, which mail discovery and the coordinator both read. Post short progress updates from `run.json` while it runs. At the end, read `summary.json` and relay its `text` in plain words. It lists files delivered, audit counts, best-copy choices, blocked and failed promos (retried next run), lossless upgrade candidates and any labels still unsettled.

Normal repeated runs use protected normalized track inventories for up to the 30-day offer window, and always re-evaluate each mix against the current library, assets, exclusions and resolutions. New mail, changed recipient identity or stable catalogue fields invalidate metadata. Mutable Dropbox/Drive/OneDrive folders and SoundCloud/Disco playlists still get a fresh manifest; unchanged inventories avoid duration/format probes. Inflyte retains fresh complete bulk live/locker data and bounded inspected hidden releases; planning does not check recipient availability for an owned mix. Actual work refreshes recipient details and availability before feedback/download, preserving the full new inventory before any file attempt. Idle reconciliation stays local, while failed work is re-inspected. Generic legacy caches require source-signature plus exact snapshot-hash evidence from a verified run; missing evidence refreshes once. Archive manifests bind to roster/content versions, and originals cleaned after collection are restored only when real work needs bytes. `--refresh-metadata` forces a complete metadata recheck. Providers without a content revision can silently edit an unchanged offer; the bounded cache cannot prove absence of those edits before a forced refresh or actual work. These metadata rules do not alter `--continue-pass` or audit coverage.

Research order is fixed, and asking the user comes last: the platform page and per-track label lines, the announcement email (subject `[Label]`, Label/Imprint lines, "released on/through", catalogue number, ISRC, copyright line), `label_codes.csv`, then Beatport (same ISRC, or same title plus artist overlap and a compatible mix name), then web research through the `web-research` skill (Discogs, Bandcamp, Spotify, label sites). A store label naming only a distributor (DistroKid, LANDR and so on), an artist's own copyright line, or an unofficial remix sent by its remixers, files as `Self Released`. Record each manual decision in `metadata-resolutions.json` with `mail:<message-id>` or https sources. The research pass, the reconcile and the audit all honour it: the audit holds a delivered file that an approved resolution renames, and research delivers the corrected copy. When a promo also offers an extended or full mix, research and audit drop the radio edit or edit. When Beatport has the track, its label overrules every other source; a catalogue spelling that agrees with it is kept, and the overruled labels are logged as evidence. Beatport titles keep featured artists, `So Good (feat. Kuuda)`, and the matcher strips that before comparing. 1001Tracklists is a reliable second source, allowing for spelling variants. Tracks from one sampler share its label when the sampler names it and Beatport confirms the siblings.

Out-of-style promos are skipped through `audit/exclusions.csv` in the test folder, which the user edits: one row per entry, `label`, `artist` or `title word` then the value, any case. Labels match the entire normalized imprint name: excluding `Suanda Gold Classics` does not exclude `Suanda`, `Suanda Voice`, `Suanda Dark` or other sister labels. A per-track label outranks a sampler label; the PR sender is never label evidence. An empty list excludes nothing. Each process reads it at most every five minutes and keeps the last good copy in the state folder, so a Dropbox hiccup never lifts an exclusion. `collect_release` drops a matching track before version selection, so it is never downloaded, processed, or given feedback; the summary lists it as `excluded`.

Optionally set `exclusion_samples_folder` in settings to a Dropbox folder where the user puts examples of unwanted labels. The coordinator imports their final `[Label]` suffix into the CSV during prepare, before collection. `scripts/promo_exclusions.py` previews the import; add `--apply --sweep` to update the CSV and remove matching delivery-folder files, with library and asset state updated. Imports paginate, preserve existing CSV rules/comments/delimiters, guard concurrent edits by revision, and verify the uploaded hash. Private state records processed samples, so removing a rule from the master CSV is respected even if its old sample remains. A newly added or revised sample can add it again. Self Released and unlabeled samples are reported for review and never create a blanket label or artist exclusion. Holds and this sample folder are excluded from ownership checks. No sample is deleted by the importer.

The final audit compares same-mix aliases, including different remix wording and repeated label suffixes, against both the library and previously verified files in the delivery folder. A broad name match is only a candidate: deletion requires identical Dropbox bytes or identical decoded audio, sample rate and channels. Audio digests preserve floating-point precision. Different audio from an additional alias candidate alone never triggers deletion or a research hold; established metadata/version checks still apply.

If playable selections live outside `library_root`, add those exact, disjoint Dropbox folders to the optional `additional_library_roots` array in settings. Each keeps its own incremental cursor; no audio is moved or renamed there. An order number followed by a Camelot key, for example `z145 - 12A - Artist - Title (Extended Mix) -7,5.wav`, is treated as DJ filename metadata for comparison. Numeric artist names without that complete prefix are unchanged. A matching title alone never proves that a classic and a new remix are duplicates.

Held files live in `<test folder>/Needs Research` (`HOLD` in `promo_automation.py`), each with a same-named `.txt` note listing why it was held and what research already tried, so a person or any agent can finish it. Nothing is kept locally. The processor and the audit move or upload held files there; the audit skips that folder. Research reads tags by Dropbox byte range (`promo_remote.remote_tags`, a few KB per file), renames by a Dropbox move when the tags already match the resolution, and only otherwise downloads to a temporary folder, retags, redelivers and deletes the temp copy. Audit readbacks use the `rev:<rev>` path form, the `rev` parameter is deprecated. The audit skips revisions that already passed under the same `AUDIT_RULES` stamp. Bump that stamp after any rule change.

## How It Works

The workflow has three phases. Phase 1 (script) uses zero AI tokens. Phase 2 (AI research) handles only the unknowns. Phase 3 (user confirmation) batches all questions.

**Dependencies**: `install.py` installs everything; `dependencies.json` lists it. The script itself needs `mutagen`, `ffmpeg` and `ffprobe`, and 7-Zip for .rar archives.

## Phase 1: Run the Script

The script automatically searches for `label_codes.csv` in this order:
1. Explicit `--label-csv` path if provided
2. Recursively in directories given via `--search-dir` (you can pass multiple)
3. The input folder
4. Bundled with the skill (fallback — always available)

The user's local copy takes priority over the bundled one. When `--save-label-csv auto` is set, the script writes updated codes back to whichever file it loaded (or to the first `--search-dir` if using the bundled fallback). This keeps the user's copy growing over time.

For a typical run, point `--search-dir` at the user's selected/working folder so the script finds their personal CSV if they have one:

```bash
python3 <skill-path>/scripts/process_promos.py "<input_folder>" \
  --search-dir "<user_working_folder>" \
  --output-folder "<output_folder>" \
  --save-label-csv auto \
  --report "<output_folder>/processing_report.json"
```

For a dry run first (recommended for a new batch):
```bash
python3 <skill-path>/scripts/process_promos.py "<input_folder>" \
  --search-dir "<user_working_folder>" \
  --dry-run \
  --report "<output_folder>/dry_run_report.json"
```

The output folder should be a dated subfolder, e.g. `processed promos/Week 8 2026/`.

**Staging directory**: The v3 script stages WAV→AIFF conversions in `<output-folder>/.staging/` by default, avoiding the old `/tmp` disk space issue entirely. You can override this with `--staging-dir` if needed, but normally there is no reason to.

**Timeout**: For batches over 50 files use 30 minutes. Over 100 files use 60 minutes. The script prints progress to stderr (`[1/104] Parsing: filename.mp3`) so you can see it's still working — if output is still flowing, don't worry about the timeout.

**Re-runs are safe**: The script skips files that already exist in the output folder (with size > 0). If a run times out or crashes partway, just re-run it — it picks up where it left off. Use `--force` to override this and reprocess everything.

The script handles:
- Extracting ZIP/RAR archives
- Parsing all filename formats (catalog-prefix, numbered, standard, bare)
- Converting WAV to AIFF (for ID3 tag compatibility)
- Normalizing .aif → .aiff extensions for consistency
- Stripping master/bounce suffixes (MASTER, mastered, NEWFIXFIX master, etc.)
- Normalizing mix types ([Extended] → (Extended Mix), capitalization fixes)
- Normalizing ft. → feat. in artist names
- Writing clean ID3 tags (TIT2, TPE1, TPE2, TALB, GRP1, TPUB)
- Cleaning junk tags (all COMM fields, redundant encoders, empty USLT, "None" values)
- Post-processing tag verification on ALL output files (checks required tags, consistency, junk)
- Detecting duplicates (same artist+title within 2s duration = same mix, with ffprobe fallback for WAVs)
- Renaming to: `Artist - Title (Mix Type) [Label].ext`
- Printing a human-readable summary at the end

**After Phase 1, check the report**: The JSON report now includes a `tag_issues` section. If it's empty, all tags are clean — no AI verification needed. If it has entries, address them alongside the unknowns in Phase 2.

## Phase 2: AI Research on Unknowns

Read the JSON report. Focus on: `unknowns`, `tag_issues`, `duplicates`, and `errors`.

**Artist name policy**: Always prefer Beatport spelling of artist names. Beatport is the authoritative source. If the filename says "ForzaDuo" but Beatport says "Forza:Duo", use Beatport's version.

### Research strategy for large batches

**When unknowns ≤ 15**: research them inline, one by one, following the 7-step sequence below.

**When unknowns > 15**: use Task agents (subagents) to avoid context overflow. Split unknowns into groups of ~10 and send each group to a separate Task agent. Each agent follows the same research sequence. The main session stays lean — it only receives the results and applies corrections. This prevents the session crashes that happen with 60+ sequential web fetches.

Example agent prompt:
```
Research these unknown promo tracks on Beatport, 1001tracklists, and Discogs.
For each track, follow steps 1-7 and return: original_filename, corrected_artist, corrected_title, label, and confidence (high/medium/low).
Tracks: [list of unknowns with existing tag info]
```

### Research sequence per unknown

For each unknown track, follow this exact sequence. Stop as soon as you have the answer:

1. **Check existing tags** in the report's `existing_tags` field — TPUB, GRP1, COMM often contain the label name already
2. **WebFetch Beatport search**: `https://www.beatport.com/search?q=ARTIST+TITLE` — parse the label from results. For unknown catalog prefixes specifically, search the catalog code directly: `https://www.beatport.com/search?q=SSP016`
3. **If WebFetch fails or returns no usable results**: Use the `browser-computer-control` skill with the user's normal Chrome for the same Beatport URL. Extract only the needed search results through a targeted page query. Reuse verified connections and cached results; avoid full page dumps and repeated screenshot or accessibility-tree inspection. Record reusable extraction methods for future batches. Follow that skill's attachment and fallback routes rather than requiring a specific browser extension.
4. **WebSearch 1001tracklists**: `site:1001tracklists.com "ARTIST" "TITLE"` — one search only
5. **WebSearch Discogs**: `site:discogs.com "ARTIST" "TITLE"` — one search only
6. **Broad catch-all search**: `"ARTIST" "TITLE" label trance EDM release` — cast a wide net, check all results on the first page
7. **Bootleg check**: If the track looks like a rework/mashup of a well-known track with no ISRC and no label in any search results → classify as `[White Label]`
8. **Only after completing ALL steps above**: add to the user question batch, including a summary of what you tried and found

**Research rules — these exist because the first run burned 44 searches for 27 unknowns, mostly through redundant retries:**
- Do NOT repeat a search with slightly different keywords — one attempt per source is enough
- Do NOT explain your research steps in the chat — just do them silently and report results
- Do NOT ask the user about ANY track until you've completed steps 1–7 above
- Maximum 7 tool calls per unknown track (one per step)
- For unknown catalog prefixes: always try step 2 with the catalog code (e.g. `SSP016`) — the label is usually right there in the Beatport results

## Phase 3: Batch User Questions

After all research, present ALL remaining unknowns to the user in a single batch. Format each unknown like this:

**Example:**
> `Better off (Extended mix) mastered.mp3` — Found on Beatport: artist is "Starry Major", label is "Elliptical Sun Recordings". Missing: nothing, but please confirm this is correct.

> `TRIVIX_GECKO_MASTER V2.0.mp3` — No Beatport/Discogs results. Appears to be an unsigned/self-released track by Trivix. Suggest: `Trivix - Gecko v2.0 [Trivix]` or should this be labeled differently?

If research turned up a likely answer with good confidence (found on Beatport, Discogs, or label's own site), apply it and just list it for the user to verify. Only truly ambiguous cases need an actual question.

Wait for the user's answers, then apply corrections:
1. Re-run tag writing on the affected files with corrected info
2. Rename files that got new artist/label info
3. **Update label_codes.csv** with ALL new codes discovered during research — not just catalog prefixes, also labels found for bare-filename tracks. Save to the user's personal copy (the location found in Phase 1). This is mandatory before finishing.

## Tag Standards

The target state for every processed file:

| Tag | Value | Notes |
|-----|-------|-------|
| TIT2 | Title (Mix Type) | e.g. "Khronos (Extended Mix)" |
| TPE1 | Artist | Clean name, preserve "feat.", "pres.", "and" as-is |
| TPE2 | Artist | Same as TPE1 — never include catalog prefix |
| TALB | Album/Release name | Use EP name if known, otherwise track title without mix type |
| GRP1 | Label name | e.g. "Armada Music" |
| TPUB | Label name | Same as GRP1 |
| TSRC | ISRC code | Preserve if exists, never overwrite |
| TDRC | Year | Preserve if exists |
| TDRL | Release date | Preserve if exists |
| TBPM | BPM | Preserve if exists |

Tags to always clean/remove:
- ALL COMM (comment) fields — promo files never have meaningful comments, they're always distributor junk like "Encoded By Label Worx Limited"
- TXXX:ENCODER, TXXX:HANDLER_NAME (redundant encoder tags from ffmpeg)
- Empty USLT (lyrics) fields
- TCON with value "Other" or empty

The script verifies all of the above automatically after processing and reports issues in the `tag_issues` section of the JSON report. If `tag_issues` is empty, the AI does not need to do any tag checking.

## Filename Standards

Target format: `Artist - Title (Mix Type) [Label].ext`

Rules:
- `feat.` (not `ft.`, not `featuring`) for featured artists — the script normalizes this automatically
- `pres.` for alias/presents projects
- Keep `and` if it appears in original artist name (artist's choice)
- `&` for standard collaborations
- Commas for 3+ artists: `Artist1, Artist2 & Artist3`
- `x` or `X` for certain collabs — preserve original casing
- Mix type always in parentheses `()`, never brackets `[]` — the script normalizes this automatically
- Label always in brackets `[]` at end
- Fix common typos: "Extedned" -> "Extended" — the script handles this
- No doubled artist names: "Artist - Artist - Title" -> "Artist - Title"
- No doubled label brackets: `[Label] [Label]` -> `[Label]`
- No master/bounce suffixes in titles — the script strips these automatically

## Format Conversion

- **WAV → AIFF** always (WAV has no ID3 tag support)
- **.aif → .aiff** extension normalization (content stays the same, just the extension is standardized)
- All other formats (MP3, FLAC, M4A, OGG) stay in their original format — just copy and tag
- Do NOT convert MP3, FLAC, or any other format to AIFF — only WAV needs conversion

## Duplicate Handling

- Different mixes (duration differs by >2 seconds): keep both, they're different versions
- Same mix, different formats: keep the highest quality (lossless over lossy, higher bitrate). Delete the lower quality copy.
- Same mix, same format: flag for user decision
- v3: When both durations are unknown, the script flags them as potential duplicates instead of silently skipping

## Label Codes CSV

The file `label_codes.csv` maps catalog prefixes to label names. It grows over time as new labels are encountered. Format:

```
prefix,label_name
ABL,Ablazing
ARMAS,Armada Music
```

The script searches for this file automatically (see Phase 1 for search order). A bundled copy ships with the skill as a fallback — it contains all codes discovered so far. The user's personal copy (found in their working folder or input folder) always takes priority.

After processing, always ensure the CSV is updated with any new codes discovered during AI research. Use `--save-label-csv auto` to have the script handle this automatically for codes it discovers. For codes found during Phase 2 AI research, manually update the CSV at the location it was loaded from before finishing.
