"""Tests for shared/ignore_list.py — blob operations mocked."""

import json
from unittest.mock import patch, MagicMock

import pytest

from shared.ignore_list import (
    load_ignore_list,
    save_ignore_list,
    is_ignored,
    get_ignore_list,
    update_ignore_list,
    _extract_rg_from_id,
    _migrate_tags,
    _tag_matches,
)


# ─── _extract_rg_from_id ────────────────────────────────────────────────────


class TestExtractRgFromId:
    def test_full_id(self):
        rid = "/subscriptions/s/resourcegroups/my-rg/providers/M/T/R"
        assert _extract_rg_from_id(rid) == "my-rg"

    def test_case_insensitive(self):
        rid = "/subscriptions/s/ResourceGroups/MyRG/providers/M/T/R"
        assert _extract_rg_from_id(rid) == "MyRG"

    def test_no_rg(self):
        assert _extract_rg_from_id("/subscriptions/s") == ""

    def test_empty_string(self):
        assert _extract_rg_from_id("") == ""


# ─── _tag_matches ────────────────────────────────────────────────────────────


class TestTagMatches:
    def test_key_value_match(self):
        assert _tag_matches({"env": "dev"}, "env=dev") is True

    def test_key_value_no_match(self):
        assert _tag_matches({"env": "prod"}, "env=dev") is False

    def test_key_value_case_insensitive(self):
        assert _tag_matches({"env": "Dev"}, "env=dev") is True

    def test_key_only_match(self):
        assert _tag_matches({"temporary": "yes"}, "temporary") is True

    def test_key_only_no_match(self):
        assert _tag_matches({"env": "prod"}, "temporary") is False

    def test_empty_tags(self):
        assert _tag_matches({}, "env=dev") is False


# ─── _migrate_tags ───────────────────────────────────────────────────────────


class TestMigrateTags:
    def test_legacy_flat_list(self):
        data = {"tags": ["env=dev", "temporary"]}
        result = _migrate_tags(data)
        assert result["tags"] == {"include": [], "exclude": ["env=dev", "temporary"]}

    def test_already_new_format(self):
        data = {"tags": {"include": ["env=prod"], "exclude": ["env=dev"]}}
        result = _migrate_tags(data)
        assert result["tags"] == {"include": ["env=prod"], "exclude": ["env=dev"]}

    def test_missing_tags(self):
        data = {}
        result = _migrate_tags(data)
        assert result["tags"] == {"include": [], "exclude": []}

    def test_dict_missing_keys(self):
        data = {"tags": {"exclude": ["env=dev"]}}
        result = _migrate_tags(data)
        assert result["tags"] == {"include": [], "exclude": ["env=dev"]}


# ─── is_ignored ──────────────────────────────────────────────────────────────


class TestIsIgnored:
    def test_not_ignored(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1"}
        ignore = {"resource_groups": [], "locations": [], "resource_ids": [], "tags": {"include": [], "exclude": []}}
        assert is_ignored(resource, ignore) is False

    def test_ignored_by_resource_id(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1"}
        ignore = {"resource_groups": [], "locations": [], "resource_ids": ["/sub/rg/res1"]}
        assert is_ignored(resource, ignore) is True

    def test_ignored_by_resource_group(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1"}
        ignore = {"resource_groups": ["rg1"], "locations": [], "resource_ids": []}
        assert is_ignored(resource, ignore) is True

    def test_ignored_by_location(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1"}
        ignore = {"resource_groups": [], "locations": ["eastus"], "resource_ids": []}
        assert is_ignored(resource, ignore) is True

    def test_case_insensitive_matching(self):
        resource = {"id": "/SUB/RG/RES1", "location": "EastUS", "resource_group": "RG1"}
        ignore = {"resource_groups": ["rg1"], "locations": [], "resource_ids": []}
        assert is_ignored(resource, ignore) is True

    def test_extracts_rg_from_id_when_not_direct(self):
        resource = {
            "id": "/subscriptions/s/resourcegroups/auto-rg/providers/M/T/R",
            "location": "westus",
        }
        ignore = {"resource_groups": ["auto-rg"], "locations": [], "resource_ids": []}
        assert is_ignored(resource, ignore) is True

    def test_ignored_by_subscription(self):
        resource = {
            "id": "/subscriptions/sub-123/resourcegroups/rg1/providers/M/T/R",
            "location": "eastus",
            "resource_group": "rg1",
            "subscription_id": "sub-123",
        }
        ignore = {"resource_groups": [], "locations": [], "resource_ids": [], "subscriptions": ["sub-123"], "tags": {"include": [], "exclude": []}}
        assert is_ignored(resource, ignore) is True

    def test_subscription_extracted_from_id(self):
        resource = {
            "id": "/subscriptions/sub-abc/resourcegroups/rg1/providers/M/T/R",
            "location": "eastus",
        }
        ignore = {"subscriptions": ["sub-abc"]}
        assert is_ignored(resource, ignore) is True

    # ── Exclude tags ──

    def test_ignored_by_exclude_tag_key_value(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1", "tags": {"env": "dev"}}
        ignore = {"resource_groups": [], "locations": [], "resource_ids": [], "subscriptions": [],
                  "tags": {"include": [], "exclude": ["env=dev"]}}
        assert is_ignored(resource, ignore) is True

    def test_ignored_by_exclude_tag_key_only(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1", "tags": {"temporary": "yes"}}
        ignore = {"resource_groups": [], "locations": [], "resource_ids": [], "subscriptions": [],
                  "tags": {"include": [], "exclude": ["temporary"]}}
        assert is_ignored(resource, ignore) is True

    def test_exclude_tag_no_match(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1", "tags": {"env": "prod"}}
        ignore = {"tags": {"include": [], "exclude": ["env=dev"]}}
        assert is_ignored(resource, ignore) is False

    def test_no_tags_on_resource_exclude(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1"}
        ignore = {"tags": {"include": [], "exclude": ["env=dev"]}}
        assert is_ignored(resource, ignore) is False

    # ── Include tags ──

    def test_include_tags_resource_matches(self):
        """Resource has a matching include tag — NOT ignored."""
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1", "tags": {"env": "production"}}
        ignore = {"tags": {"include": ["env=production"], "exclude": []}}
        assert is_ignored(resource, ignore) is False

    def test_include_tags_resource_no_match(self):
        """Resource does NOT match any include tag — ignored."""
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1", "tags": {"env": "dev"}}
        ignore = {"tags": {"include": ["env=production"], "exclude": []}}
        assert is_ignored(resource, ignore) is True

    def test_include_tags_empty_means_all_allowed(self):
        """Empty include list = no allow-list filter — all resources pass."""
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1", "tags": {"env": "dev"}}
        ignore = {"tags": {"include": [], "exclude": []}}
        assert is_ignored(resource, ignore) is False

    def test_include_tags_no_tags_on_resource(self):
        """Resource has no tags at all, include tags defined — ignored."""
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1"}
        ignore = {"tags": {"include": ["monitored"], "exclude": []}}
        assert is_ignored(resource, ignore) is True

    def test_include_key_only(self):
        """Include tag with key-only matches any value."""
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1", "tags": {"monitored": "true"}}
        ignore = {"tags": {"include": ["monitored"], "exclude": []}}
        assert is_ignored(resource, ignore) is False

    # ── Include + Exclude combined ──

    def test_exclude_wins_over_include(self):
        """Resource matches both include and exclude — exclude wins, resource is ignored."""
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1",
                    "tags": {"env": "production", "temporary": "yes"}}
        ignore = {"tags": {"include": ["env=production"], "exclude": ["temporary"]}}
        assert is_ignored(resource, ignore) is True

    def test_include_and_exclude_no_conflict(self):
        """Resource matches include but not exclude — not ignored."""
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1",
                    "tags": {"env": "production"}}
        ignore = {"tags": {"include": ["env=production"], "exclude": ["temporary"]}}
        assert is_ignored(resource, ignore) is False

    # ── Legacy flat list format ──

    def test_legacy_flat_tags_still_works(self):
        """Legacy format: tags as flat list treated as exclude-only."""
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1", "tags": {"env": "dev"}}
        ignore = {"tags": ["env=dev"]}
        assert is_ignored(resource, ignore) is True

    # ── Resource types ──

    def test_ignored_by_resource_type(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1", "type": "Microsoft.Compute/virtualMachines"}
        ignore = {"resource_types": ["Microsoft.Compute/virtualMachines"]}
        assert is_ignored(resource, ignore) is True

    def test_resource_type_case_insensitive(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1", "type": "microsoft.compute/virtualmachines"}
        ignore = {"resource_types": ["Microsoft.Compute/virtualMachines"]}
        assert is_ignored(resource, ignore) is True

    def test_resource_type_no_match(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1", "type": "Microsoft.Storage/storageAccounts"}
        ignore = {"resource_types": ["Microsoft.Compute/virtualMachines"]}
        assert is_ignored(resource, ignore) is False


# ─── load_ignore_list ────────────────────────────────────────────────────────


class TestLoadIgnoreList:
    @patch("shared.ignore_list._get_blob_client")
    def test_load_existing(self, mock_get_blob):
        data = {"resource_groups": ["rg1"], "locations": [], "resource_ids": [],
                "tags": {"include": ["env=prod"], "exclude": ["env=dev"]}}
        blob_client = MagicMock()
        download = MagicMock()
        download.readall.return_value = json.dumps(data).encode()
        blob_client.download_blob.return_value = download
        mock_get_blob.return_value = blob_client

        result = load_ignore_list()
        assert result == data

    @patch("shared.ignore_list._get_blob_client")
    def test_load_migrates_legacy_tags(self, mock_get_blob):
        """Legacy flat tags list is auto-migrated to include/exclude."""
        data = {"resource_groups": [], "locations": [], "resource_ids": [], "tags": ["env=dev"]}
        blob_client = MagicMock()
        download = MagicMock()
        download.readall.return_value = json.dumps(data).encode()
        blob_client.download_blob.return_value = download
        mock_get_blob.return_value = blob_client

        result = load_ignore_list()
        assert result["tags"] == {"include": [], "exclude": ["env=dev"]}

    @patch("shared.ignore_list._get_blob_client")
    def test_load_not_found(self, mock_get_blob):
        blob_client = MagicMock()
        blob_client.download_blob.side_effect = Exception("BlobNotFound")
        mock_get_blob.return_value = blob_client

        result = load_ignore_list()
        assert result == {"resource_groups": [], "locations": [], "resource_ids": [], "subscriptions": [], "tags": {"include": [], "exclude": []}, "resource_types": []}

    @patch("shared.ignore_list._get_blob_client")
    def test_load_no_connection(self, mock_get_blob):
        mock_get_blob.return_value = None
        result = load_ignore_list()
        assert result == {"resource_groups": [], "locations": [], "resource_ids": [], "subscriptions": [], "tags": {"include": [], "exclude": []}, "resource_types": []}

    @patch("shared.ignore_list._get_blob_client")
    def test_load_returns_independent_copies(self, mock_get_blob):
        """Ensure multiple calls return independent objects (deep copy, not shared refs)."""
        mock_get_blob.return_value = None
        result1 = load_ignore_list()
        result2 = load_ignore_list()
        # Mutate the nested tags in result1
        result1["tags"]["exclude"].append("env=test")
        result1["resource_groups"].append("my-rg")
        # result2 should be unaffected
        assert result2["tags"]["exclude"] == []
        assert result2["resource_groups"] == []


# ─── save_ignore_list ────────────────────────────────────────────────────────


class TestSaveIgnoreList:
    @patch("shared.ignore_list.BlobServiceClient")
    def test_save_success(self, mock_bsc_cls, monkeypatch):
        monkeypatch.setenv("AzureWebJobsStorage", "DefaultEndpointsProtocol=https;AccountName=test")
        svc = MagicMock()
        mock_bsc_cls.from_connection_string.return_value = svc
        container = MagicMock()
        container.exists.return_value = True
        svc.get_container_client.return_value = container
        blob = MagicMock()
        container.get_blob_client.return_value = blob

        data = {"resource_groups": ["rg1"], "locations": [], "resource_ids": [], "tags": {"include": [], "exclude": []}}
        assert save_ignore_list(data) is True
        blob.upload_blob.assert_called_once()

    def test_save_no_connection(self):
        assert save_ignore_list({"resource_groups": []}) is False


# ─── update_ignore_list ──────────────────────────────────────────────────────


class TestUpdateIgnoreList:
    @patch("shared.ignore_list.save_ignore_list")
    def test_adds_missing_keys(self, mock_save):
        mock_save.return_value = True
        data = {"resource_groups": ["rg1"]}
        assert update_ignore_list(data) is True
        saved = mock_save.call_args[0][0]
        assert "locations" in saved
        assert "resource_ids" in saved
        assert saved["tags"] == {"include": [], "exclude": []}

    @patch("shared.ignore_list.save_ignore_list")
    def test_keeps_existing_keys(self, mock_save):
        mock_save.return_value = True
        data = {"resource_groups": ["a"], "locations": ["b"], "resource_ids": ["c"],
                "tags": {"include": ["env=prod"], "exclude": ["env=dev"]}}
        update_ignore_list(data)
        saved = mock_save.call_args[0][0]
        assert saved["tags"] == {"include": ["env=prod"], "exclude": ["env=dev"]}

    @patch("shared.ignore_list.save_ignore_list")
    def test_migrates_legacy_tags(self, mock_save):
        mock_save.return_value = True
        data = {"resource_groups": [], "tags": ["env=dev"]}
        update_ignore_list(data)
        saved = mock_save.call_args[0][0]
        assert saved["tags"] == {"include": [], "exclude": ["env=dev"]}
