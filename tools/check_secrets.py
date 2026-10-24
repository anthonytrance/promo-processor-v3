"""Refuse a commit that would publish a secret or a private detail. Standard library only.

Run as the pre-commit hook (setup.py points git at .githooks) or by hand:
    python tools/check_secrets.py            staged files
    python tools/check_secrets.py --all      every tracked file
    python tools/check_secrets.py --history  every blob in every commit

Besides token, password and saved-login patterns, it reads a private word list outside the repo, one word per
line: names, addresses and folder names that must never be published. A line starting with "!" is an allowed
spelling that is removed before the check, such as a public GitHub account name. The list lives in
%APPDATA%/skill-secrets/private-words.txt, or the file named by SKILL_PRIVATE_WORDS, and is never committed.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "GitHub token": r"\bgh[opsru]_[A-Za-z0-9]{30,}",
    "Dropbox token": r"\bsl\.[A-Za-z0-9_-]{60,}",
    "Google token": r"\bya29\.[A-Za-z0-9_-]{20,}|\b1//0[A-Za-z0-9_-]{30,}",
    "Google client secret": r"\bGOCSPX-[A-Za-z0-9_-]{20,}",
    "Telegram bot token": r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}",
    "OpenAI-style key": r"\bsk-[A-Za-z0-9_-]{30,}",
    "AWS key": r"\bAKIA[0-9A-Z]{16}\b",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    # Split so this file does not match itself.
    "DPAPI blob": "AQAAANCMnd8" + r"BFdERjHoAwE/Cl\+s|01000000d08c" + "9ddf0115d1118c7a00c04fc297eb",
    "bearer header": r"(?i)authorization['\"]?\s*[:=]\s*['\"]?bearer\s+[A-Za-z0-9._-]{20,}",
    "password value": r"(?i)['\"]?(?:password|passwd|secret|refresh_token|access_token)['\"]?\s*[:=]\s*['\"][^'\"\s]{8,}['\"]",
    "cookie value": r"(?i)\b(?:sessionid|session_id|remember_token|laravel_session|PHPSESSID)\s*[=:]\s*['\"]?[A-Za-z0-9%._-]{20,}",
}
BLOCKED_SUFFIXES = {".dpapi", ".sqlite", ".sqlite3", ".db", ".key", ".pem", ".p12", ".pfx", ".har",
                    ".wav", ".aif", ".aiff", ".mp3", ".flac", ".m4a", ".zip", ".rar", ".exe"}
# LOCAL.md holds one installation's private notes beside SKILL.md.
BLOCKED_NAMES = re.compile(r"(?i)(credential|token|cookie|session)[^/]*\.(json|txt)$|(^|/)\.env$|(^|/)local\.md$")
# Test fixtures and documentation examples, never real values.
PLACEHOLDER = re.compile(r"(?i)not-a-real|example|placeholder|dummy|fake|redacted|\.\.\.|<[a-z_ -]+>|x{6,}")
MAX_BYTES = 5 * 1024 * 1024
COMPILED = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}


def private_words():
    path = os.environ.get("SKILL_PRIVATE_WORDS") or Path(os.environ.get("APPDATA", Path.home())) / "skill-secrets/private-words.txt"
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], []
    words = [line.strip().casefold() for line in lines if line.strip() and not line.startswith("#")]
    return [w for w in words if not w.startswith("!")], [w[1:] for w in words if w.startswith("!")]


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout


def problems(name, data, words, allowed=()):
    found = []
    lower = name.casefold()
    if Path(lower).suffix in BLOCKED_SUFFIXES or BLOCKED_NAMES.search(lower):
        found.append("file type that may hold a secret or audio")
    if len(data) > MAX_BYTES:
        found.append(f"larger than {MAX_BYTES // 1048576} MB")
    text = data.decode("utf-8", "replace")
    for label, pattern in COMPILED.items():
        if any(not PLACEHOLDER.search(match.group(0)) for match in pattern.finditer(text)):
            found.append(label)
    folded = text.casefold()
    for spelling in allowed:
        folded = folded.replace(spelling, "")
        lower = lower.replace(spelling, "")
    for word in words:
        if word in folded or word in lower:
            found.append("private word from the local list")
            break
    return found


def targets(mode):
    if mode == "history":
        seen = set()
        for line in git("rev-list", "--all", "--objects").decode().splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[0] not in seen:
                seen.add(parts[0])
                if git("cat-file", "-t", parts[0]).strip() == b"blob":
                    yield parts[1], git("cat-file", "blob", parts[0])
        return
    if mode == "all":
        names = git("ls-files", "-z").split(b"\0")
    else:
        names = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split(b"\0")
    for raw in filter(None, names):
        name = raw.decode()
        yield name, git("show", ":" + name) if mode == "staged" else Path(name).read_bytes()


def main():
    parser = argparse.ArgumentParser(description="Block secrets and private details from a commit")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_const", const="all", dest="mode")
    group.add_argument("--history", action="store_const", const="history", dest="mode")
    mode = parser.parse_args().mode or "staged"
    words, allowed = private_words()
    failed = False
    for name, data in targets(mode):
        for reason in problems(name, data, words, allowed):
            failed = True
            print(f"Blocked: {name}: {reason}")
    if failed:
        print("Nothing was committed. Remove the detail, or move the file outside the repo.")
        return 1
    print(f"Secret check passed ({mode}{', no private word list found' if not words else ''}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
