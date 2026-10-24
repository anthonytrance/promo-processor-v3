# Ampsuite recipient adapter

Live pilot verified 2026-10-22. scripts/ampsuite.py reuses the shared pipeline; no browser or saved
account password was needed. Each label has its own *.ampsuite.com host. The 30-day read-only mail
scan found 40 notifications and stored 33 candidate recipient URLs with message provenance.

Emailed path: /promotion/leave_feedback, with recipient parameters p, c and i. All full URLs and
page/release snapshots stay DPAPI protected. Never put parameter values in docs or error output.
Only the specifically emailed release is fetched. Outstanding-promos and preview-player links are
not used for inventory or downloads.

Structured page fields: release_title and release_artist contain artist and title respectively,
despite their names. release_date ends with the label after "on". Per-track track_title_<id> and
track_artist_<id> fields supply names, and .web-link image controls contain the real WAV/MP3 download
route in document.location. The adapter verifies same host and the exact /views/promotion/download_track.php
path; the preview route is never accepted as a source file.

The inspected page has a feedback form with leave_feedback, support, rating 1-10, fave_track and comments.
Its presence was not proof of a download gate. Live original-file downloads worked without submitting
the optional form. No feedback or decline was sent. A redirect, HTML response or changed mandatory gate
is a review exception, not a reason to bypass it or invent a new submission contract.

Pilot: Transcom, Jade's Touch, High Emotions Recordings. Extended Mix (79,531,280-byte WAV) and Intro Mix
(88,943,480-byte WAV) were downloaded, decoded, converted to 16-bit 44.1 kHz AIFF, tagged and hash-verified
in the test folder. Radio Edit was excluded. See runtime log ampsuite-pilot.log for the collect result.
First release contact costs one recipient page GET; each selected original file adds one GET.
Routine collection uses no browser/model calls. Full multi-label bulk coverage is still unverified.

Commands: discover-mail --since 30d; collect SOURCE_ID --dry-run; collect SOURCE_ID;
batch --dry-run; batch --max-age-days 30. Undated batch entries are skipped. Single-release collection
is an explicit selected test, so use the message's offer date before choosing it.
