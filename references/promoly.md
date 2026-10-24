# Promoly recipient adapter

Verified 2026-10-22. Code: scripts/promoly.py; discovery: scripts/promo_mail.py.

Email links: ln.promo.ly/servepromo/stream-<recipient-key>. Treat the whole key as an access secret.
The API is https://api.promo.ly. GET /api/landers/<key> returns the campaign, tracks, original files,
download-enabled flag, comment requirement and existing feedback. Store snapshots only with DPAPI.
No account credential or browser is required for the verified flow.

Campaign fields used: labelName, name, enableDownload, requireComment, expires, assetsLocalPath and tracks.
Track identity is track_id; each original file has file_id, extension, exact file_size and file_name.
Legacy filesArr-only campaigns are explicit exceptions, not silently completed.

A required comment is unlocked with PUT /api/landers/<key>/feedback containing key, campaignId,
rating (4 on five stars), the persisted generic thank-you comment and optional favTrack=null.
The code saves pending/submitting/submitted state and requires the response to echo the exact comment.
Timeouts stay submitting and block resubmission. Existing platform feedback is respected.
No listening, playing or audience claims are made.

GET /api/landers/<key>/presigneds returns a signedUrl. Verify the exact original-file CloudFront host,
then use the query with the campaign storage path and original filename, as the recipient UI does.
POST /api/landers/<key>/downloads is the normal per-file download beacon, sent once after the durable
download intent is synced. No plays or pageview beacons are manufactured. Download redirects are not followed.
Gate, signature, beacon or transfer uncertainty blocks a blind retry.

Live proof: Harshil Kamdar, Might Be Love (Extended Mix), Blue Dot Trance. 44,780,212-byte original WAV,
16-bit 44.1 kHz stereo, converted/tagged to AIFF and hash-verified in the test folder.
The short version was excluded. Second collect skipped the delivered mix with zero platform HTTP requests.
Steady-state single release with cached metadata: one feedback PUT if gated, one signature GET,
one beacon POST and one audio GET per selected file, plus Dropbox log/delivery calls.
First uncached contact adds one campaign GET. No model/browser calls inside collection.

Commands: discover-mail --since 30d; collect SOURCE_ID --dry-run; collect SOURCE_ID --allow-feedback;
batch --dry-run [--limit N]; batch --allow-feedback. A first dry run can inspect a new recipient campaign,
which may change platform access indicators, so use only releases intended for collection.
Batch age defaults to 30 days. Bulk coverage is implemented but not live-proven across all campaigns.
