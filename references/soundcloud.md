# SoundCloud session renewal

`scripts/soundcloud.py` uses only uploader-enabled original downloads. There is no stream ripping or feedback.

Save both `oauth_token` and `oauth_refresh_token` in the state folder's `soundcloud-session.dpapi`. Provision a
fresh token pair for the processor. A rotating refresh cookie copied from an active personal browser can compete
with that browser's renewal. The website's existing sign-in session can authorize a fresh pair with PKCE, without
a password and without consuming the browser's refresh cookie. Use the browser skill's authorized connection.
Do not copy browser profiles or decrypt the browser's cookie database.

Preferred initial provisioning: `python soundcloud.py bootstrap-session`, JSON on stdin with
`connect_session_cookie` (the `_soundcloud_session` cookie scoped to `api-auth.soundcloud.com`), `client_id`
(the current public website configuration), and `expected_user_id` (the signed-in account's numeric ID).
An agent can read those through the authorized browser connection and pipe them directly to this command,
without displaying or writing the values. The command checks the existing sign-in, generates PKCE and a random
state/nonce, authorizes a new code, validates the exact callback and state, then exchanges it through the
website's `/n/api/code-exchange` proxy. It checks `/me` against the expected account before saving. The broader
browser sign-in cookie stays in memory only. This command is explicit provisioning, never a routine-run dependency.
If that browser sign-in has also expired, ordinary sign-in is still required.

`python promo_doctor.py save-soundcloud` asks for both values with hidden typing, verifies the access token using
`GET https://api-v2.soundcloud.com/me`, and only then replaces the protected file. For an agent-mediated capture,
`python soundcloud.py save-session` accepts a JSON object on stdin with those two keys, verifies it and prints
only success flags. Never put the values on a command line, in a script, in tool output or in the skill folder.
Legacy access-only files remain usable until expiry, then produce an explicit request to provision renewal.

Normal requests inspect JWT expiry as a scheduling hint and renew within 60 seconds of expiry. The adapter also
handles an unexpected API 401 by renewing once and retrying that exact GET once. JWT parsing does not verify
authentication; the profile check does. `TokenExpired` stops subsequent requests to this platform in coordinator
phases, rather than retrying a broken login for every release.

`python soundcloud.py check-session` verifies the account, renewing when needed. `python soundcloud.py
refresh-session` forces one renewal and then verifies it. Neither opens a promo, downloads audio or publishes
logs. `promo_doctor.py` runs this account check when a SoundCloud session file exists, and returns failure for
an invalid saved session. Other promo platforms still get local saved-login checks only.

Website contract verified 2026-10-25: the current website's `tokenRefreshProxyWebi` endpoint is
`POST https://soundcloud.com/n/api/refresh-token`, no body, with the two session cookies and the normal
SoundCloud Origin and Referer. Success is HTTP 200 plus replacement access and refresh cookies on
`soundcloud.com`, path `/`. The adapter disables redirects on this request, stores only these two response
cookies, and atomically replaces the encrypted file before exposing the renewed token to callers. It does not
need a developer app secret. This is the website's flow, distinct from the public developer OAuth API.

A per-session OS lock serializes renewal across processes. A protected `refresh_pending` flag is saved before
the request. A crash, timeout, refused response or incomplete rotation leaves it set. Subsequent runs stop and
request session recapture instead of blindly replaying a refresh cookie whose consumption is uncertain. A
successful HTTP response's candidate cookies are journaled inside that protected file before validation, so a
changed cookie format can be investigated without losing returned credentials. A network outage during a profile
GET can be retried normally. A separate session limits browser/processor races;
account revocation, provider changes and expired refresh cookies can still require a fresh login.

Live acceptance, 2026-10-25: existing browser sign-in recovered through the website's
`loginWithExistingOAuthSession` flow; a new processor token pair was provisioned using the installed bootstrap
command. Two forced processor renewals and fresh-instance profile checks succeeded. Alternating browser and
processor renewals both succeeded with distinct refresh tokens. Their JWT `sid` can still match because they
originate from the same browser sign-in; distinct `sid` is not a valid independence test here. This is same-day
acceptance, not proof that the service can never revoke either credential.

Public evidence: the live website's assets
[55-9a8aa66f.js](https://a-v2.sndcdn.com/assets/55-9a8aa66f.js) and
[56-3d5fc94a.js](https://a-v2.sndcdn.com/assets/56-3d5fc94a.js) define the cookie names, refresh proxy and
client-side renewal and existing-sign-in recovery;
[0-631475bf.js](https://a-v2.sndcdn.com/assets/0-631475bf.js) implements the website's PKCE state and code
exchange. SoundCloud's [developer authentication guide](https://developers.soundcloud.com/docs/api/guide#authentication)
describes the separate public OAuth API and its single-use refresh tokens. Live website renewal and a subsequent
profile request were verified without opening a promo. Ongoing survival across future days is not proven by
same-day checks.
