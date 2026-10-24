"""One-command setup for promo-processor-v3 on Windows. Standard library only; run it with any Python 3.12.

    python install.py --mail-account promos@example.com --destination "/Music/Promos"

It clones or updates the sibling skills named in dependencies.json next to this folder at their pinned versions,
creates or reuses a virtual environment and installs every requirements.txt into it, downloads the pinned gws
program and checks its SHA-256, writes the settings file when there is none, turns on the secret check in every
skill repo, and names each missing program with the command that installs it. It never signs in to anything:
promo_doctor.py does that check afterwards. Running it again is safe.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parent
LOCAL = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
SETTINGS = Path(os.environ.get("PROMO_SETTINGS") or Path(os.environ.get("APPDATA") or Path.home()) / "promo-processor/settings.json")
problems = []


def say(text):
    print(text, flush=True)


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def git_clean(folder):
    status = subprocess.run(["git", "-C", str(folder), "status", "--porcelain"], capture_output=True, text=True)
    return status.returncode == 0 and not status.stdout.strip()


def skills(manifest):
    for name, spec in manifest["skills"].items():
        folder = SKILLS / name
        if not folder.exists():
            say(f"Cloning {name} at {spec['ref']}.")
            run(["git", "clone", "--quiet", "--branch", spec["ref"], spec["repo"], str(folder)])
        elif (folder / ".git").exists():
            if git_clean(folder):
                say(f"Updating {name} to {spec['ref']}.")
                run(["git", "-C", str(folder), "fetch", "--quiet", "--tags", "origin"])
                run(["git", "-C", str(folder), "checkout", "--quiet", spec["ref"]])
            else:
                problems.append(f"{name} has uncommitted changes, so it was left at its current version.")
        else:
            say(f"{name} already exists and is not a git clone; it was kept as it is.")


def environment(venv):
    python = venv / "Scripts/python.exe"
    if not python.exists():
        say(f"Creating the virtual environment in {venv}.")
        run([sys.executable, "-m", "venv", str(venv)])
    files = [HERE / "requirements.txt"] + [SKILLS / name / "requirements.txt" for name in MANIFEST["skills"]]
    command = [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    for path in files:
        if path.exists():
            command += ["-r", str(path)]
    say("Installing the Python packages at their pinned versions.")
    run(command)
    if (SKILLS / "web-research").exists():
        say("Installing the Chromium copy the research skill renders pages with.")
        run([str(python), "-m", "playwright", "install", "chromium"])
    return python


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def gws(python):
    spec = MANIFEST["programs"]["gws"]
    scripts = SKILLS / "google-workspace-files/scripts"
    if not scripts.exists():
        return
    root = subprocess.run([str(python), "-c", "import profile_store; print(profile_store.APP_ROOT)"], cwd=scripts,
                          capture_output=True, text=True, check=True).stdout.strip()
    target = Path(root) / "gws" / spec["version"] / "gws.exe"
    if target.exists() and sha256(target) == spec["sha256_exe"]:
        say(f"gws {spec['version']} is already installed and verified.")
        return
    say(f"Downloading gws {spec['version']} from the googleworkspace/cli releases.")
    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / "gws.zip"
        with urllib.request.urlopen(spec["url"], timeout=120) as response, open(archive, "wb") as handle:
            shutil.copyfileobj(response, handle)
        if sha256(archive) != spec["sha256_zip"]:
            raise SystemExit("The gws download failed its SHA-256 check, so nothing was installed.")
        with zipfile.ZipFile(archive) as bundle:
            member = next(name for name in bundle.namelist() if name.lower().endswith("gws.exe"))
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
    if sha256(target) != spec["sha256_exe"]:
        target.unlink()
        raise SystemExit("The unpacked gws.exe failed its SHA-256 check and was removed.")
    say(f"gws installed and verified in {target.parent}.")


def seven_zip():
    found = shutil.which("7z")
    for folder in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        candidate = Path(folder or "") / "7-Zip/7z.exe"
        if not found and folder and candidate.exists():
            found = str(candidate)
    return found


def chrome():
    for folder in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), str(LOCAL)):
        candidate = Path(folder or "") / "Google/Chrome/Application/chrome.exe"
        if folder and candidate.exists():
            return str(candidate)
    return None


def programs():
    found = {"ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"), "7z": seven_zip(),
             "chrome": chrome(), "browser-use": shutil.which("browser-use")}
    for name, path in found.items():
        spec = MANIFEST["programs"][name]
        if path:
            say(f"Found {name}.")
        else:
            problems.append(f"{name} is missing, needed for {spec['why']}. Install it with: {spec['install']}")


def settings(args):
    if SETTINGS.exists():
        say(f"Kept the existing settings file {SETTINGS}.")
        return
    if not args.mail_account or not args.destination:
        problems.append("No settings file yet. Run install.py again with --mail-account and --destination.")
        return
    value = {"mail_account": args.mail_account, "destination": args.destination,
             "library_root": args.library_root or "/" + args.destination.strip("/").split("/")[0],
             "state_dir": str(Path(args.state_dir) if args.state_dir else LOCAL / "promo-processor/state")}
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    say(f"Wrote the settings file {SETTINGS}.")


def hooks():
    for folder in [HERE] + [SKILLS / name for name in MANIFEST["skills"]]:
        if (folder / ".git").exists() and (folder / ".githooks").exists():
            run(["git", "-C", str(folder), "config", "core.hooksPath", ".githooks"])
    say("The secret check runs before every commit in each skill repo.")


def main():
    parser = argparse.ArgumentParser(description="Set up promo-processor-v3 and the skills it uses")
    parser.add_argument("--mail-account", help="The mailbox promos arrive in, as named in the email skill")
    parser.add_argument("--destination", help="Dropbox folder that receives the finished promos")
    parser.add_argument("--library-root", help="Dropbox folder holding the whole music library (duplicate check)")
    parser.add_argument("--state-dir", help="Local folder for the database and saved logins")
    parser.add_argument("--venv", type=Path, default=LOCAL / "promo-processor/venv")
    args = parser.parse_args()
    if sys.platform != "win32" or sys.version_info < (3, 12):
        raise SystemExit("This skill needs Windows and Python 3.12 or newer.")
    if not shutil.which("git"):
        raise SystemExit("Git is missing. Install it with: winget install Git.Git")
    skills(MANIFEST)
    python = environment(args.venv)
    gws(python)
    programs()
    settings(args)
    hooks()
    if problems:
        say("Setup finished with these points still open:")
        for number, text in enumerate(problems, 1):
            say(f"{number}. {text}")
    else:
        say("Setup finished with nothing missing.")
    say(f"Next, check every sign-in with: {python} {HERE / 'scripts/promo_doctor.py'}")
    return 1 if problems else 0


MANIFEST = json.loads((HERE / "dependencies.json").read_text(encoding="utf-8"))

if __name__ == "__main__":
    sys.exit(main())
