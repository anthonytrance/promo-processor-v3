# Promo Processor v3

An AI agent skill for DJs. It collects promo tracks that labels and PR companies send you by email and through promo platforms, picks the right version of each mix, converts and tags the audio, names the files consistently, checks them against your existing library for duplicates, and delivers them to a Dropbox folder. Anything uncertain, such as an unknown label or a missing artist, is held for research instead of guessed.

It is written for Claude Code and Codex style agents: the agent reads `SKILL.md` and runs the scripts. It also works by hand from a terminal. Everything is plain text and command line, so it works well with a screen reader.

Supported sources: Promo Cloud, Inflyte, PromoBox, Promoly, Ampsuite, SoundCloud private links, Dropbox and Google Drive shared links, and direct download links in promo emails. Promo Delivery, FATdrop and CTRL Promo are partly supported; see `SKILL.md`.

## What you need

1. Windows 10 or 11.
2. Python 3.12.
3. An IMAP mailbox that receives your promos.
4. A Dropbox account holding your music library.
5. Google Chrome, for the one-time Inflyte and SoundCloud logins.

The installer names any other program it needs (ffmpeg, 7-Zip, browser-use) together with the command that installs it.

## Setup

1. Clone this repository into your agent's skills folder, for example `%USERPROFILE%\.claude\skills\promo-processor-v3`.
2. In that folder, run: `python install.py --mail-account promos@example.com --destination "/Music/Promos"`. Use your own mailbox and the Dropbox folder that should receive new promos. The installer clones the five sibling skills listed in `dependencies.json` next to this one, creates a virtual environment, installs the Python packages, downloads the pinned gws program and checks its hash, and writes the settings file `%APPDATA%\promo-processor\settings.json`. Running it again is safe.
3. Set up mail access as described in the email skill's README: add your promo mailbox to its credentials file.
4. Set up Dropbox access as described in the dropbox skill's README.
5. Optionally set up Google Drive access as described in the google-workspace-files skill's README, so Google Drive promo links can be read.
6. Run `python scripts/promo_doctor.py`. It makes one read-only request per service and names whatever still needs a login.
7. Save the platform logins the doctor asks for: `python scripts/promo_doctor.py save-password promo-cloud`, `save-password promobox`, and `save-soundcloud`. Typing is hidden. Logins are stored with Windows protection for your user account, never in this folder.

## Settings

The settings file is JSON. Its keys are listed in the Setup section of `SKILL.md`: the mailbox, the destination folder, the library root used for the duplicate check, the state folder for the local database, logs and saved logins, and optional credential file paths. Set the `PROMO_SETTINGS` environment variable to use a settings file somewhere else.

## Using it

Ask your agent to "update the promos". It will run a dry run first, then collect, and report what was delivered, what was skipped as a duplicate, and what is held for research. The full workflow is in `SKILL.md`.

## Tests

Run `python -m pytest scripts` from this folder. The tests are offline and never touch a real account.

## Licence

MIT; see `LICENSE`.

## Privacy

Your mail, logins, database and audio never go in this folder. The pre-commit hook in `.githooks` runs `tools/check_secrets.py`, which refuses to commit tokens, passwords, saved logins, audio, or any word from your own private word list. Personal notes for your installation can go in a `LOCAL.md` beside `SKILL.md`; git ignores it.
