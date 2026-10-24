import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import needlepool
from promo_automation import Store
from promo_pipeline import GateRequired, collect_release


CAMPAIGN = "11111111-1111-4111-8111-111111111111"


def props():
    return {"campaignId": CAMPAIGN, "title": "Fixture Artist - Fixture Song", "releaseMeta": {
                "artist": "Fixture Artist", "label": "Fixture Label", "album": "Fixture Song"},
            "allowDownloads": True, "requireFeedback": True, "initialFeedback": None,
            "tracks": [{"id": "track-fixture", "title": "Fixture Song (Original Mix)", "duration_sec": 410,
                        "has_wav": True, "has_mp3": True}]}


def html(data=None):
    record = 'e:["$","$L1",null,' + json.dumps(data or props()) + ']\n'
    return "<html><script>self.__next_f.push(" + json.dumps([1, record]) + ")</script></html>"


def catalogue(count=1):
    return f'''<h1>Releases sent to you ({count})</h1><a href="/portal/campaign/{CAMPAIGN}">
    <div><p>Fixture Artist</p><p>Fixture Song</p><span>10/20/2026</span><p>WAV MP3</p></div></a>'''


class Response:
    def __init__(self, data=None, status=200, headers=None, text="", body=b"audio"):
        self.data, self.status_code, self.headers, self.text, self.body = data, status, headers or {}, text, body
        self.closed = False

    def json(self):
        return self.data

    def close(self):
        self.closed = True

    def iter_content(self, _):
        yield self.body


class NeedlepoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.patches = [patch.object(Store, "sync_logs", return_value=None), patch.object(needlepool.requests, "Session"),
                        patch.object(needlepool.requests, "get", side_effect=AssertionError("Live HTTP forbidden")),
                        patch.object(socket, "create_connection", side_effect=AssertionError("Live sockets forbidden")),
                        patch.object(socket.socket, "connect", side_effect=AssertionError("Live sockets forbidden")),
                        patch.object(needlepool, "protect_json", side_effect=lambda p, d: Path(p).write_text(json.dumps(d), encoding="utf-8")),
                        patch.object(needlepool, "read_protected_json", side_effect=lambda p: json.loads(Path(p).read_text(encoding="utf-8")))]
        for item in self.patches:
            item.start()
        self.store = Store(self.root)
        self.sid = self.store.add_source("needlepool", "Fixture", needlepool.BASE + "/portal/campaign/" + CAMPAIGN,
                                        remote_id=CAMPAIGN, offered_at="2026-10-20T00:00:00+00:00")
        self.adapter = needlepool.Needlepool(self.store)
        self.adapter.session.request.side_effect = AssertionError("Unstubbed HTTP forbidden")

    def tearDown(self):
        self.store.db.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_only_auth_magic_and_campaign_links(self):
        self.assertEqual(needlepool.recipient_link(needlepool.BASE + needlepool.AUTH + "?t=fixture"), ("magic", "fixture"))
        for bad in ("https://links.send.needlepool.net/track/fixture", needlepool.BASE + "/register",
                    needlepool.BASE + "/.netlify/functions/consent-confirm?r=fixture", "https://needlepool.com/",
                    "http://app.needlepool.com/.netlify/functions/auth-magic?t=fixture",
                    "https://user@app.needlepool.com/.netlify/functions/auth-magic?t=fixture",
                    "https://app.needlepool.com:8443/.netlify/functions/auth-magic?t=fixture",
                    "https://app.needlepool.com:bad/.netlify/functions/auth-magic?t=fixture"):
            self.assertIsNone(needlepool.recipient_link(bad))

    def test_decode_flight_without_executing_script(self):
        self.assertEqual(needlepool.flight_props(html()), props())
        with self.assertRaisesRegex(RuntimeError, "schema"):
            needlepool.flight_props("<script>window.something={tracks:[]}</script>")

    def test_split_flight_records_join_before_decoding(self):
        record = 'e:["$",null,null,' + json.dumps(props()) + ']'
        cut = len(record) // 2
        markup = "".join("<script>self.__next_f.push(" + json.dumps([1, value]) + ")</script>" for value in (record[:cut], record[cut:]))
        self.assertEqual(needlepool.flight_props(markup), props())

    def test_ambiguous_multiple_campaign_props_refused(self):
        with self.assertRaisesRegex(RuntimeError, "schema"):
            needlepool.flight_props(html() + html())

    def test_normalize_preserves_mixes_lengths_formats(self):
        data = props(); data["tracks"].append({"id": "remix-fixture", "title": "Fixture Song (Other Remix)",
                                              "duration_sec": 440, "has_wav": False, "has_mp3": True})
        release = needlepool.normalize_release(data)
        self.assertEqual(release["tracks"][1]["name"], "Fixture Artist - Fixture Song (Other Remix)")
        self.assertEqual([f["type"] for f in release["tracks"][0]["files"]], ["wav", "mp3"])
        self.assertTrue(release["gated"])

    def test_downloads_disabled_leaves_files_empty(self):
        data = props(); data["allowDownloads"] = False
        self.assertFalse(needlepool.normalize_release(data)["tracks"][0]["files"])

    def test_existing_artist_prefix_is_not_duplicated(self):
        data = props(); data["tracks"][0]["title"] = "Fixture Artist - Fixture Song (Original Mix)"
        track = needlepool.normalize_release(data)["tracks"][0]
        self.assertEqual(track["name"], "Fixture Artist - Fixture Song (Original Mix)")
        self.assertEqual(track["title"], "Fixture Song (Original Mix)")

    def test_catalogue_count_and_dates(self):
        card = needlepool.catalogue_cards(catalogue())[0]
        self.assertEqual(card["campaign_id"], CAMPAIGN)
        self.assertEqual(card["offered_at"], "2026-10-20T00:00:00+00:00")
        with self.assertRaisesRegex(RuntimeError, "count"):
            needlepool.catalogue_cards(catalogue(2))

    def test_all_unauthorized_routes_blocked_even_same_host(self):
        for route in ("/.netlify/functions/consent-confirm?r=fixture", "/register", "/.netlify/functions/portal-link-reissue",
                      "/.netlify/functions/share-download-confirm", "https://evil.example/portal"):
            with self.assertRaisesRegex(RuntimeError, "route refused"):
                self.adapter._request("GET", route)
        self.adapter.session.request.assert_not_called()

    def test_snapshot_reads_page_without_tracking_or_audio(self):
        self.adapter._request = Mock(return_value=Response(text=html()))
        data = self.adapter.snapshot(self.sid)
        self.assertEqual(data["campaignId"], CAMPAIGN)
        self.assertEqual(self.adapter._request.call_count, 1)
        self.assertEqual(self.adapter._request.call_args.args[0], "GET")
        self.store.sync_logs.assert_called()

    def test_auth_saves_session_before_campaign_fetch(self):
        sid = self.store.add_source("needlepool", "Magic fixture", needlepool.BASE + needlepool.AUTH + "?t=fixture")
        self.adapter._save_session = Mock()
        self.adapter._request = Mock(side_effect=[Response(status=302, headers={"Location": "/portal/campaign/" + CAMPAIGN}),
                                                 Response(text=html())])
        self.assertEqual(self.adapter.snapshot(sid)["campaignId"], CAMPAIGN)
        self.assertGreaterEqual(self.adapter._save_session.call_count, 1)

    def test_spent_auth_does_not_request_new_email(self):
        sid = self.store.add_source("needlepool", "Magic fixture", needlepool.BASE + needlepool.AUTH + "?t=fixture")
        self.adapter._request = Mock(return_value=Response(status=302, headers={"Location": "/link?t=fixture"}))
        with self.assertRaises(needlepool.AccessRequired):
            self.adapter.snapshot(sid)
        self.assertEqual(self.adapter._request.call_count, 1)

    def test_refresh_uses_saved_campaign_instead_of_consumed_auth_magic(self):
        sid = self.store.add_source("needlepool", "Magic fixture", needlepool.BASE + needlepool.AUTH + "?t=fixture")
        (self.root / f"needlepool-{sid}.dpapi").write_text(json.dumps(props()), encoding="utf-8")
        self.adapter.session_path.write_text("{}", encoding="utf-8")
        self.adapter._request = Mock(return_value=Response(text=html()))
        self.assertEqual(self.adapter.snapshot(sid, refresh=True)["campaignId"], CAMPAIGN)
        self.adapter._request.assert_called_once_with("GET", needlepool.BASE + "/portal/campaign/" + CAMPAIGN)

    def test_refresh_rejects_a_different_saved_campaign_response(self):
        sid = self.store.add_source("needlepool", "Magic fixture", needlepool.BASE + needlepool.AUTH + "?t=fixture")
        (self.root / f"needlepool-{sid}.dpapi").write_text(json.dumps(props()), encoding="utf-8")
        self.adapter.session_path.write_text("{}", encoding="utf-8")
        different = props(); different["campaignId"] = "22222222-2222-4222-8222-222222222222"
        self.adapter._request = Mock(return_value=Response(text=html(different)))
        with self.assertRaisesRegex(RuntimeError, "saved campaign"):
            self.adapter.snapshot(sid, refresh=True)

    def test_auth_redirect_to_consent_is_refused(self):
        sid = self.store.add_source("needlepool", "Magic fixture", needlepool.BASE + needlepool.AUTH + "?t=fixture")
        self.adapter._request = Mock(return_value=Response(status=302, headers={"Location": "/.netlify/functions/consent-confirm?r=fixture"}))
        with self.assertRaisesRegex(RuntimeError, "consent/register"):
            self.adapter.snapshot(sid)
        self.assertEqual(self.adapter._request.call_count, 1)

    def test_catalogue_registers_canonical_source_only(self):
        self.adapter.session_path.write_text("{}", encoding="utf-8")
        self.adapter._request = Mock(return_value=Response(text=catalogue()))
        result = self.adapter.discover_catalogue()
        self.assertEqual(result["sources"], [self.sid])
        self.assertEqual(self.adapter.campaigns(), [self.sid])

    def test_feedback_once_without_planned_use_or_decline(self):
        self.adapter.snapshot = Mock(return_value=props())
        self.adapter._request = Mock(return_value=Response({"ok": True}))
        self.adapter.unlock(self.sid, needlepool.normalize_release(props()))
        payload = self.adapter._request.call_args.kwargs["json"]
        self.assertEqual(set(payload), {"campaign_id", "rating", "comment"})
        self.adapter.unlock(self.sid, needlepool.normalize_release(props()))
        self.assertEqual(self.adapter._request.call_count, 1)

    def test_lost_feedback_response_never_repeats(self):
        self.adapter.snapshot = Mock(return_value=props())
        self.adapter._request = Mock(side_effect=RuntimeError("lost response"))
        with self.assertRaises(RuntimeError):
            self.adapter.unlock(self.sid, needlepool.normalize_release(props()))
        self.adapter._request.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.adapter.unlock(self.sid, needlepool.normalize_release(props()))
        self.adapter._request.assert_not_called()

    def test_feedback_once_across_auth_and_catalogue_source_aliases(self):
        self.adapter.snapshot = Mock(return_value=props())
        self.adapter._request = Mock(return_value=Response({"ok": True}))
        release = needlepool.normalize_release(props())
        self.adapter.unlock(self.sid, release)
        alias = self.store.add_source("needlepool", "Auth fixture", needlepool.BASE + needlepool.AUTH + "?t=fixture")
        self.adapter.unlock(alias, release)
        self.assertEqual(self.adapter._request.call_count, 1)

    def test_uncertain_feedback_blocks_other_source_alias(self):
        self.adapter.snapshot = Mock(return_value=props())
        self.adapter._request = Mock(side_effect=RuntimeError("lost response"))
        release = needlepool.normalize_release(props())
        with self.assertRaises(RuntimeError):
            self.adapter.unlock(self.sid, release)
        alias = self.store.add_source("needlepool", "Auth fixture", needlepool.BASE + needlepool.AUTH + "?t=fixture")
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.adapter.unlock(alias, release)
        self.assertEqual(self.adapter._request.call_count, 1)

    def test_gate_and_dry_run_prevent_feedback_and_original(self):
        self.adapter.release = Mock(return_value=needlepool.normalize_release(props()))
        self.adapter.unlock, self.adapter.download = Mock(), Mock()
        library = Mock(); library.matches.return_value = []
        with patch("promo_pipeline.load_exclusions", return_value={}):
            plan = collect_release(self.store, self.adapter, self.sid, library, dry_run=True)
            self.assertEqual(plan["tracks"][0]["format"], "wav")
            with self.assertRaises(GateRequired):
                collect_release(self.store, self.adapter, self.sid, library)
        self.adapter.unlock.assert_not_called(); self.adapter.download.assert_not_called()

    def test_download_calls_original_endpoint_only_after_intent(self):
        self.adapter.snapshot = Mock(return_value=props())
        def request(*args, **kwargs):
            self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM events WHERE action='platform_download'").fetchone())
            return Response({"url": "https://fixture.supabase.co/storage/audio.wav"})
        self.adapter._request = Mock(side_effect=request)
        with patch.object(needlepool.requests, "get", return_value=Response(headers={"Content-Type": "audio/wav", "Content-Length": "5"})) as get:
            result = self.adapter.download(self.sid, {"id": "fixture-wav", "track_id": "fixture", "type": "wav"}, self.root / "fixture.wav")
            self.assertEqual(result[0], 5)
            self.assertEqual(self.adapter._request.call_args.kwargs["json"]["intent"], "download")
            self.assertNotIn("cookies", get.call_args.kwargs)

    def test_foreign_signed_storage_and_html_are_refused(self):
        self.adapter.snapshot = Mock(return_value=props())
        self.adapter._request = Mock(return_value=Response({"url": "https://evil.example/audio.wav"}))
        with self.assertRaisesRegex(RuntimeError, "storage host"):
            self.adapter.download(self.sid, {"id": "fixture-wav", "track_id": "fixture", "type": "wav"}, self.root / "fixture.wav")
        needlepool.requests.get.assert_not_called()

    def test_storage_userinfo_ports_and_deceptive_hosts_refused(self):
        for url in ("https://user@fixture.supabase.co/audio.wav", "https://fixture.supabase.co:8080/audio.wav",
                    "https://fixture.supabase.co:bad/audio.wav", "https://fixture.supabase.co.evil.example/audio.wav",
                    "http://fixture.supabase.co/audio.wav", "https://[invalid/audio.wav"):
            self.assertFalse(self.adapter.storage_url(url))


if __name__ == "__main__":
    unittest.main()
