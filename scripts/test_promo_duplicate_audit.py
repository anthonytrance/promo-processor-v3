"""Audit aliases with audio proof, preserve distinct versions and floating-point samples."""
import json
from pathlib import Path
import struct
import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

from promo_automation import Store
import promo_final_audit as audit
import promo_pipeline as pipeline


@pytest.mark.parametrize("same_audio", [True, False])
def test_same_mix_aliases_in_delivery_are_compared_but_different_audio_is_kept(tmp_path, monkeypatch, same_audio):
    names = ["Artist - Song (Guest Extended Remix) [Label].aiff",
             "Artist - Song (Guest - Extended Mix) [Label].aiff"]
    store = Store(tmp_path)
    try:
        store.sync_logs = mock.Mock()
        remotes = {}
        for i, name in enumerate(names):
            path = audit.DEST + "/" + name
            remote = SimpleNamespace(id=f"id:{i}", rev=f"rev{i}", name=name, path_lower=path.lower(),
                                     path_display=path, content_hash=f"hash{i}")
            remotes[remote.id] = remote
            store.db.execute("INSERT INTO library(path,remote_id,revision,content_hash) VALUES(?,?,?,?)",
                             (path.lower(), remote.id, remote.rev, remote.content_hash))
        store.db.commit()
        client = mock.Mock(files_get_metadata=lambda key: remotes[key])
        monkeypatch.setattr(audit, "index_library", lambda store: None)
        monkeypatch.setattr(audit, "dropbox_skill", lambda: (None, client))
        monkeypatch.setattr(audit, "readback", lambda store, client, remote, folder: Path(remote.id[-1] + ".aiff"))
        monkeypatch.setattr(audit, "validate_audio", lambda path: {"duration": 360})
        monkeypatch.setattr(audit, "filename_and_tag_issues", lambda name, local: ([], {}, name))
        monkeypatch.setattr(audit, "pcm_identity", lambda path: {"sha256": "same" if same_audio else str(path)})
        result = audit.audit(store, tmp_path)
        if same_audio:
            assert result["counts"] == {"verified": 1, "removed_duplicate": 1}
            client.files_delete_v2.assert_called_once_with("id:1", parent_rev="rev1")
        else:
            assert result["counts"] == {"verified": 2}
            client.files_delete_v2.assert_not_called()
            client.files_move_v2.assert_not_called()
    finally:
        store.db.close()


def test_pcm_identity_does_not_round_distinct_float_audio_to_integer(tmp_path):
    identities = []
    for i, value in enumerate([0.25, 0.25 + 2**-40]):
        raw = tmp_path / f"{i}.raw"
        raw.write_bytes(struct.pack("<d", value) * 64)
        wav = tmp_path / f"{i}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "f64le", "-ar", "48000", "-ac", "1",
                        "-i", str(raw), "-c:a", "pcm_f64le", str(wav)], check=True, capture_output=True)
        identities.append(audit.pcm_identity(wav))
    assert identities[0] != identities[1]


def test_set_filename_prefix_and_gain_are_comparison_metadata_only():
    path = "/Sets/z145 - 12A - Duo x Artist - Song (Extended Mix) -7,5.wav"
    assert pipeline.library_name(path) == "Duo x Artist - Song (Extended Mix).wav"
    index = pipeline.VersionIndex([path])
    assert index.versions("Artist & Duo - Song (Extended Mix)") == [(path, "extended")]
    assert pipeline.library_keys(path)[0] == pipeline.mix_key("Artist & Duo - Song (Extended Mix)")
    for name in ["3CHO - Song.wav", "4.0 Artist - Song.wav", "Artist - Song -7,5.wav", "12A - Song.wav"]:
        assert pipeline.library_name(name) == name


def test_classic_and_new_coartist_version_are_audio_candidates_not_a_library_hold(tmp_path):
    store = Store(tmp_path)
    try:
        classic = "Artist - Song (Extended Mix) [Label].aiff"
        newer = "/Sets/z145 - 12A - Remixer x Artist - Song (Extended Mix) -7,5.wav"
        owned = pipeline.VersionIndex([newer])
        assert owned.versions(classic), "Still inspect the possible alias's audio"
        assert not audit.resolution_and_version_issues(store, classic, [classic], 490, owned)
    finally:
        store.db.close()


def test_near_remixer_spelling_does_not_automatically_own_a_different_mix(tmp_path):
    store = Store(tmp_path)
    try:
        name = "Artist - Song (Long Remixer Extended Remix) [Label].aiff"
        other = "/Library/Artist - Song (Long Remixor Extended Remix) [Label].aiff"
        owned = pipeline.VersionIndex([other])
        assert not owned.versions(name)
        assert owned.versions(name, credit_typos=True)
        assert not audit.resolution_and_version_issues(store, name, [name], 350, owned)
    finally:
        store.db.close()
