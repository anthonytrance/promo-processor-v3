import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import promo_watch as watcher


class CompletionWatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_finished_message_reports_coverage_failures_separately_from_audit(self):
        text = watcher.message({"state": "finished_review_reports"}, {"delivered": 151, "audit": {"verified": 915},
            "phase_failures": [{"platform": "ctrl-promo", "phase": "discover"}], "exceptions": ["one"], "questions": [{}, {}]})
        self.assertIn("151 files", text)
        self.assertIn("915 verified", text)
        self.assertIn("2 files still need", text)
        self.assertIn("ctrl-promo discover", text)
        self.assertIn("coverage is incomplete", text)

    def test_stopped_or_missing_summary_never_claims_complete_success(self):
        self.assertIn("stopped", watcher.message({"state": "stopped"}, {}))
        self.assertIn("summary is missing", watcher.message({"state": "finished_review_reports"}, None))

    def test_notification_is_persisted_before_send_and_deduplicates(self):
        path = self.root / "state.json"
        def send(text):
            self.assertEqual(json.loads(path.read_text())["notification"], "submitting")
            return 42
        send = mock.Mock(side_effect=send)
        self.assertTrue(watcher.notify_once(path, "Done", send))
        self.assertFalse(watcher.notify_once(path, "Done", send))
        send.assert_called_once()
        self.assertEqual(json.loads(path.read_text())["message_id"], 42)

    def test_lost_send_response_is_not_repeated(self):
        path = self.root / "state.json"
        send = mock.Mock(side_effect=TimeoutError("synthetic"))
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            watcher.notify_once(path, "Done", send)
        self.assertFalse(watcher.notify_once(path, "Done", send))
        self.assertEqual(json.loads(path.read_text())["notification"], "uncertain")
        send.assert_called_once()

    def test_running_then_finished_run_notifies_once_and_exits(self):
        states = iter([{"pid": 123, "started": "fixture", "state": "running"},
                       {"pid": 123, "started": "fixture", "state": "running"},
                       {"pid": 123, "started": "fixture", "state": "finished_review_reports"}])
        real_load = watcher.load
        def load(path, default=None):
            return next(states) if Path(path).name == "run.json" else real_load(path, default)
        send = mock.Mock(return_value=42)
        with mock.patch.object(watcher, "load", side_effect=load), \
                mock.patch.object(watcher, "process_alive", return_value=True), mock.patch.object(watcher.time, "sleep") as sleep:
            watcher.watch(self.root, send)
        sleep.assert_called_once()
        send.assert_called_once()

    def test_existing_live_watcher_is_not_duplicated(self):
        (self.root / "run.json").write_text(json.dumps({"pid": 123, "started": "fixture", "state": "running"}))
        (self.root / "completion-watch.json").write_text(json.dumps({"pid": 456, "notification": "waiting"}))
        with mock.patch.object(watcher, "process_alive", return_value=True), self.assertRaisesRegex(RuntimeError, "already"):
            watcher.watch(self.root, mock.Mock())


if __name__ == "__main__":
    unittest.main()
