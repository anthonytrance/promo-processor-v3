"""Offline sample import, exact imprint scope and rejection-folder ownership checks."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from promo_automation import Store, dropbox_bytes_hash
import promo_exclusions as importer
import promo_pipeline as pipeline


def sample(name):
    return SimpleNamespace(name=name, content_hash="example-hash")


def test_samples_use_only_the_final_whole_label_and_skip_self_releases():
    labels, skipped = importer.sample_labels([
        sample("A - Song (Extended Mix) [Suanda Gold Classics].aiff"),
        sample("B - Other [suanda gold classics].mp3"),
        sample("C - Track [Old Label] [Café Records].flac"),
        sample("D - Track [Self Released].mp3"), sample("missing.mp3"),
        sample("notes [Wrong].txt"), SimpleNamespace(name="Folder [Wrong].aiff"),
    ])
    assert labels == {"suanda gold classics": "Suanda Gold Classics", "café records": "Café Records"}
    assert len(skipped) == 2


@pytest.mark.parametrize("data", [
    b"type,value,note\nlabel,Existing,keep\n",
    b"\xef\xbb\xbftype;value;note\r\n# keep;comment;\r\nlabel;Existing;keep",
    "type;value;note\nlabel;Café;keep\n".encode("cp1252"),
])
def test_append_keeps_existing_rules_and_excel_delimiter_and_is_idempotent(data):
    before = pipeline.parse_exclusions_csv(data)
    updated, added = importer.append_labels(data, {"suanda gold classics": "Suanda Gold Classics"})
    after = pipeline.parse_exclusions_csv(updated)
    assert added == ["Suanda Gold Classics"]
    assert after["labels"] == before["labels"] + ["suanda gold classics"]
    assert after["artists"] == before["artists"]
    assert importer.append_labels(updated, {"suanda gold classics": "Suanda Gold Classics"}) == (updated, [])


def test_exact_imprint_does_not_exclude_sister_labels_or_a_sender():
    rules = {"labels": ["suanda gold classics"], "artists": [], "title_words": []}
    assert pipeline.excluded({"name": "A - Track", "label": "Suanda Gold Classics"}, {}, rules)
    for label in ["Suanda", "Suanda Voice", "Suanda Dark", "Suanda Progressive"]:
        track = {"name": "A - Track", "label": label}
        assert pipeline.excluded(track, {"label": "Suanda Gold Classics", "sender": "Suanda Gold Classics"}, rules) is None
    assert pipeline.excluded({"name": "A - Track"}, {"sender": "Suanda Gold Classics"}, rules) is None


def test_paginated_preview_never_writes_and_apply_checks_revision_and_hash(tmp_path, monkeypatch):
    store = Store(tmp_path)
    try:
        store.sync_logs = mock.Mock()
        data = b"type,value,note\nlabel,Existing,keep\n"
        client = mock.Mock()
        client.files_list_folder.return_value = SimpleNamespace(entries=[sample("A - Song [New Label].mp3")], has_more=True, cursor="page2")
        client.files_list_folder_continue.return_value = SimpleNamespace(entries=[sample("B - Track [Other Label].aiff")], has_more=False)
        client.files_download.return_value = (SimpleNamespace(rev="012345abc"), SimpleNamespace(content=data))
        client.files_upload.side_effect = lambda data, *a, **kw: SimpleNamespace(content_hash=dropbox_bytes_hash(data))
        cache = tmp_path / "cache.csv"
        monkeypatch.setattr(pipeline, "EXCLUSIONS_CACHE", cache)
        result = importer.sync_samples(store, client, "/Music/Label Samples")
        assert result["added_labels"] == ["New Label", "Other Label"]
        client.files_upload.assert_not_called()
        store.sync_logs.assert_not_called()
        assert not cache.exists()
        importer.sync_samples(store, client, "/Music/Label Samples", apply=True)
        assert client.files_upload.call_args.kwargs["mode"].get_update() == "012345abc"
        assert pipeline.parse_exclusions_csv(cache.read_bytes())["labels"] == ["existing", "new label", "other label"]
        client.files_upload.side_effect = RuntimeError("concurrent CSV edit")
        client.files_list_folder.return_value.entries.append(sample("C - New [Third Label].aiff"))
        cache.write_text("previous good copy", encoding="utf-8")
        with pytest.raises(RuntimeError, match="concurrent CSV edit"):
            importer.sync_samples(store, client, "/Music/Label Samples", apply=True)
        assert cache.read_text() == "previous good copy"
    finally:
        store.db.close()


def test_csv_removal_is_not_undone_by_a_previously_imported_sample(tmp_path, monkeypatch):
    store = Store(tmp_path)
    try:
        store.sync_logs = mock.Mock()
        client = mock.Mock()
        client.files_list_folder.return_value = SimpleNamespace(entries=[sample("A - Song [New Label].mp3")], has_more=False)
        client.files_download.return_value = (SimpleNamespace(rev="012345abc"), SimpleNamespace(content=b"type,value,note\n"))
        client.files_upload.side_effect = lambda data, *a, **kw: SimpleNamespace(content_hash=dropbox_bytes_hash(data))
        monkeypatch.setattr(pipeline, "EXCLUSIONS_CACHE", tmp_path / "cache.csv")
        assert importer.sync_samples(store, client, "/Samples", apply=True)["added_labels"] == ["New Label"]
        client.files_upload.reset_mock()
        # The user removed the label from the master CSV while its old sample remains.
        assert importer.sync_samples(store, client, "/Samples", apply=True)["added_labels"] == []
        client.files_upload.assert_not_called()
        assert pipeline.parse_exclusions_csv((tmp_path / "cache.csv").read_bytes())["labels"] == []
    finally:
        store.db.close()


def test_unconfigured_import_does_not_access_dropbox(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "SETTINGS", {})
    with mock.patch.object(importer, "dropbox_skill", side_effect=AssertionError("network")):
        assert importer.sync_samples(None, apply=True)["state"] == "not_configured"


def test_research_holds_and_sample_folder_do_not_count_as_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "SETTINGS", {"exclusion_samples_folder": "/Music/Label Samples"})
    store = Store(tmp_path)
    try:
        paths = ["/Music/Label Samples/A - Song [L].mp3", pipeline.HOLD + "/B - Held [L].aiff",
                 "/Music/Label Samples Extra/C - Keep [L].mp3", "/Music/Library/D - Owned [L].aiff"]
        store.db.executemany("INSERT INTO library(path) VALUES(?)", [(p.lower(),) for p in paths])
        store.db.commit()
        index = pipeline.LibraryIndex(store)
        assert not index.matches(pipeline.mix_key(Path(paths[0]).name))
        assert not index.matches(pipeline.mix_key(Path(paths[1]).name))
        assert index.matches(pipeline.mix_key(Path(paths[2]).name))
        assert index.matches(pipeline.mix_key(Path(paths[3]).name))
    finally:
        store.db.close()
