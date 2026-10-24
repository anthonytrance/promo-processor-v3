"""One-time completion notification for an existing promo run. Never starts collection."""
import argparse
import json
import os
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

from promo_automation import atomic_text, now, process_alive

TERMINAL = {"finished_review_reports", "stopped"}


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def message(manifest, summary):
    if manifest.get("state") != "finished_review_reports":
        return "The promo run stopped before completing. Ask me to check its reports and remaining work."
    if not summary:
        return "The promo coordinator finished, but its final summary is missing. The result still needs checking."
    lines = ["The promo run has finished.",
             f"{summary.get('delivered', 0)} files delivered to the promo folder."]
    counts = summary.get("audit", {})
    if counts:
        lines.append("Final audit: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items()) + ".")
    questions = len(summary.get("questions", []))
    if questions:
        lines.append(f"{questions} files still need metadata research.")
    failed = summary.get("phase_failures", [])
    if failed:
        lines.append("Failed phases: " + ", ".join(
            " ".join(str(s.get(k) or "") for k in ("platform", "phase")).strip() for s in failed) + ".")
    if (failed or summary.get("exceptions") or summary.get("blocked_by_platform")
            or summary.get("collection_disabled") or summary.get("catalogue_errors")):
        lines.append("Some source failures or access blocks remain, so source coverage is incomplete.")
    if summary.get("upgrade_candidates"):
        lines.append(f"{len(summary['upgrade_candidates'])} possible lossless upgrades were flagged for review.")
    lines.append("Ask me for the full results and remaining work.")
    return "\n\n".join(lines)


def notify_once(state_path, text, send):
    state = load(state_path, {})
    if state.get("notification") in {"submitting", "sent", "uncertain"}:
        return False
    state.update(notification="submitting", notification_started=now())
    atomic_text(state_path, json.dumps(state))
    try:
        message_id = send(text)
    except Exception as error:
        # A lost response may already have delivered the message. Never duplicate it.
        state.update(notification="uncertain", error_type=type(error).__name__)
        atomic_text(state_path, json.dumps(state))
        raise RuntimeError("Notification response was not confirmed; inspect watcher state before retrying") from None
    state.update(notification="sent", message_id=message_id, notified_at=now())
    atomic_text(state_path, json.dumps(state))
    return True


def telegram_sender(token, chat_id):
    def send(text):
        request = Request("https://api.telegram.org/bot" + token + "/sendMessage",
                          data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        if result.get("ok") is not True or not result.get("result", {}).get("message_id"):
            raise RuntimeError("Telegram did not confirm delivery")
        return result["result"]["message_id"]
    return send


def watch(run_dir, send, poll_seconds=20, max_hours=12):
    run_dir = Path(run_dir)
    manifest_path, state_path = run_dir / "run.json", run_dir / "completion-watch.json"
    initial = load(manifest_path)
    if not isinstance(initial, dict) or not initial.get("pid") or not initial.get("started"):
        raise RuntimeError("Existing run manifest is missing or invalid")
    prior = load(state_path, {})
    if prior.get("notification") in {"submitting", "sent", "uncertain"}:
        return
    if prior.get("pid") and process_alive(prior["pid"]):
        raise RuntimeError("This run already has a live completion watcher")
    identity = (initial["pid"], initial["started"])
    state = {"pid": os.getpid(), "started": now(), "run_started": initial["started"], "notification": "waiting"}
    atomic_text(state_path, json.dumps(state))
    deadline, dead_since = time.monotonic() + max_hours * 3600, None
    while True:
        manifest = load(manifest_path)
        if manifest and (manifest.get("pid"), manifest.get("started")) != identity:
            raise RuntimeError("Run identity changed; watcher refused to follow another run")
        if manifest and manifest.get("state") in TERMINAL:
            text = message(manifest, load(run_dir / "summary.json"))
            break
        if not process_alive(identity[0]):
            dead_since = dead_since or time.monotonic()
            if time.monotonic() - dead_since >= 60:
                text = "The promo coordinator exited without a final completion state. The run needs checking before it can be called complete."
                break
        else:
            dead_since = None
        if time.monotonic() >= deadline:
            text = "The promo run has not finished within the completion watcher's time limit. Please ask me to check its progress."
            break
        state["last_checked"] = now()
        atomic_text(state_path, json.dumps(state))
        time.sleep(poll_seconds)
    notify_once(state_path, text, send)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--chat-id", type=int, required=True)
    p.add_argument("--poll-seconds", type=int, default=20)
    p.add_argument("--max-hours", type=float, default=12)
    a = p.parse_args()
    if not 5 <= a.poll_seconds <= 300 or not 0 < a.max_hours <= 24:
        p.error("Use 5-300 second polling and a one-time watch of at most 24 hours")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed = {int(x) for x in re.findall(r"\d+", os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""))}
    if not token or a.chat_id not in allowed:
        raise RuntimeError("Watcher needs the bridge token and an explicitly allowed recipient from its environment")
    watch(a.run_dir, telegram_sender(token, a.chat_id), a.poll_seconds, a.max_hours)


if __name__ == "__main__":
    main()
