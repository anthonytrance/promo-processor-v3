"""Metadata versions are retained without extra source requests."""
import importlib.util
from datetime import datetime
from pathlib import Path
from unittest import mock

from dropbox.files import FileMetadata
from dropbox.sharing import FileLinkMetadata, FolderLinkMetadata, LinkPermissions
from promo_automation import SKILLS
from test_onedrive_promos import OfflineCase


class DropboxInventoryVersionTests(OfflineCase):
    def setUp(self):
        super().setUp()
        helper = Path(__file__).parent / "sibling-dropbox/dropbox_shared.py"
        if not helper.exists():
            helper = SKILLS / "dropbox/scripts/dropbox_shared.py"
        spec = importlib.util.spec_from_file_location("versioned_dropbox_shared", helper)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.client = mock.Mock()

    def test_single_file_retains_revision_and_original_permission_without_extra_requests(self):
        self.client.sharing_get_shared_link_metadata.return_value = FileLinkMetadata(
            name="Promo.zip", id="id:fixture", size=100, rev="abc123abc123", link_permissions=LinkPermissions(allow_download=False))
        inventory = self.module.shared_inventory(self.client, "https://www.dropbox.com/scl/fi/fixture/Promo.zip")
        entry = inventory["files"][0]
        self.assertEqual((entry["rev"], entry["size"], entry["is_downloadable"]), ("abc123abc123", 100, False))
        self.assertEqual(inventory["requests"], 1)
        self.client.files_list_folder.assert_not_called()

    def test_folder_inventory_preserves_revision_and_hash(self):
        self.client.sharing_get_shared_link_metadata.return_value = FolderLinkMetadata(name="Promo")
        self.client.files_list_folder.return_value = mock.Mock(entries=[FileMetadata(
            name="Promo.zip", id="id:fixture", size=100, rev="abc123abc123", content_hash="a" * 64,
            client_modified=datetime(2026, 1, 1), server_modified=datetime(2026, 1, 1), is_downloadable=True)], has_more=False)
        inventory = self.module.shared_inventory(self.client, "https://www.dropbox.com/scl/fo/fixture")
        self.assertEqual((inventory["files"][0]["rev"], inventory["files"][0]["content_hash"]), ("abc123abc123", "a" * 64))
        self.assertEqual(inventory["requests"], 2)
