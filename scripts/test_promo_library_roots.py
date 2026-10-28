"""Additional ownership folders keep independent cursors and never replace the main library."""
from types import SimpleNamespace
from unittest import mock

import pytest

import promo_automation as automation


def entry(folder, name, revision="1"):
    return SimpleNamespace(path_lower=folder.lower()+"/"+name.lower(), name=name, id="id:"+name,
                           rev=revision, size=100, content_hash="hash"+revision)


def page(entries, cursor, more=False):
    return SimpleNamespace(entries=entries, cursor=cursor, has_more=more)


def test_additional_folder_full_delta_and_main_relist_keep_other_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(automation, "LIBRARY_ROOT", "/Music")
    monkeypatch.setattr(automation, "SETTINGS", {"additional_library_roots": ["/Sets"]})
    store = automation.Store(tmp_path)
    try:
        shared = SimpleNamespace(FileMetadata=SimpleNamespace)
        client = mock.Mock()
        client.files_list_folder.side_effect = lambda path, recursive: page([entry(path, "A.wav")], path+"-cursor")
        client.files_list_folder_continue.side_effect = lambda cursor: page([entry(cursor.partition("-cursor")[0], "A.wav", "2")], cursor)
        with mock.patch.object(automation, "dropbox_skill", return_value=(shared, client)):
            automation.index_library(store)
            assert {r[0] for r in store.db.execute("SELECT path FROM library")} == {"/music/a.wav", "/sets/a.wav"}
            automation.index_library(store)
            assert {r[0] for r in store.db.execute("SELECT revision FROM library")} == {"2"}
            assert client.files_list_folder_continue.call_count == 2
            automation.index_library(store, full=True)
            assert store.db.execute("SELECT count(*) FROM library").fetchone()[0] == 2
    finally:
        store.db.close()


def test_failed_additional_pagination_preserves_old_rows_and_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(automation, "LIBRARY_ROOT", "/Music")
    monkeypatch.setattr(automation, "SETTINGS", {"additional_library_roots": ["/Sets"]})
    store = automation.Store(tmp_path)
    try:
        store.db.execute("INSERT INTO library(path) VALUES('/sets/old.wav')")
        store.db.execute("INSERT INTO library_extra_cursors VALUES('/sets','old',0)")
        store.db.commit()
        shared = SimpleNamespace(FileMetadata=SimpleNamespace)
        client = mock.Mock()
        client.files_list_folder.return_value = page([entry("/Sets", "New.wav")], "next", True)
        client.files_list_folder_continue.side_effect = RuntimeError("listing failed")
        with pytest.raises(RuntimeError, match="listing failed"):
            automation.index_additional_libraries(store, shared, client, full=True)
        assert store.db.execute("SELECT path FROM library").fetchone()[0] == "/sets/old.wav"
        assert store.db.execute("SELECT cursor FROM library_extra_cursors").fetchone()[0] == "old"
    finally:
        store.db.close()


@pytest.mark.parametrize("root", ["/Music", "/Music/Nested", "/", "relative"])
def test_unsafe_or_overlapping_additional_root_is_rejected_without_requests(tmp_path, monkeypatch, root):
    monkeypatch.setattr(automation, "LIBRARY_ROOT", "/Music")
    monkeypatch.setattr(automation, "SETTINGS", {"additional_library_roots": [root]})
    store = automation.Store(tmp_path)
    try:
        client = mock.Mock()
        with pytest.raises(ValueError):
            automation.index_additional_libraries(store, SimpleNamespace(), client)
        client.files_list_folder.assert_not_called()
    finally:
        store.db.close()
