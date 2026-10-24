from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from promo_automation import Store
import promo_pipeline as pipeline


class CollectionLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        self.sid = self.store.add_source("fixture", "Fixture", "https://example.invalid/release")
        self.library = SimpleNamespace(matches=lambda *_: [])
        self.release = {"title": "Fixture", "label": "Label", "gated": False, "tracks": [
            {"name": "A - Song (Radio Edit)", "artist": "A", "duration": 180, "files": [{"id": "r", "type": "wav"}]},
            {"name": "A - Song (Extended Mix)", "artist": "A", "duration": 360, "files": [{"id": "e", "type": "wav"}]},
            {"name": "A - Song (Dub Mix)", "artist": "A", "duration": 340, "files": [{"id": "d", "type": "wav"}]}]}
        self.adapter = SimpleNamespace(release=lambda _: deepcopy(self.release))

    def test_one_asset_cap_keeps_full_version_selection_before_planning(self):
        with mock.patch.object(pipeline, "load_exclusions", return_value={}):
            result = pipeline.collect_release(self.store, self.adapter, self.sid, self.library, dry_run=True, max_assets=1)
        actions = {t["track"]: t["action"] for t in result["tracks"]}
        self.assertEqual(actions, {"A - Song (Radio Edit)": "not_wanted", "A - Song (Extended Mix)": "download",
                                   "A - Song (Dub Mix)": "deferred"})
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM claims").fetchone()[0], 0)

    def test_live_fetch_receives_only_one_asset_and_never_deferred_tracks(self):
        with mock.patch.object(pipeline, "load_exclusions", return_value={}), \
                mock.patch.object(pipeline, "STAGING", Path(self.temp.name) / "staging"), \
                mock.patch.object(pipeline, "_fetch_and_deliver") as fetch:
            pipeline.collect_release(self.store, self.adapter, self.sid, self.library, max_assets=1)
        todo = fetch.call_args.args[5]
        self.assertEqual(len(todo), 1)
        self.assertEqual(todo[0][0]["name"], "A - Song (Extended Mix)")

    def test_existing_mix_does_not_consume_the_one_asset_allowance(self):
        self.library.matches = lambda key, *_: ["/library/song.aiff"] if "extended" in key else []
        with mock.patch.object(pipeline, "load_exclusions", return_value={}):
            result = pipeline.collect_release(self.store, self.adapter, self.sid, self.library, dry_run=True, max_assets=1)
        actions = {t["track"]: t["action"] for t in result["tracks"]}
        self.assertEqual(actions["A - Song (Extended Mix)"], "skip")
        self.assertEqual(actions["A - Song (Dub Mix)"], "download")

    def test_invalid_limit_stops_before_release_access(self):
        adapter = mock.Mock()
        for limit in (0, -1, True, 1.5, "1"):
            with self.assertRaises(ValueError):
                pipeline.collect_release(self.store, adapter, self.sid, self.library, max_assets=limit)
        adapter.release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
