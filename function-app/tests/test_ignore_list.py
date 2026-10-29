"""Tests for shared/ignore_list.py — blob operations mocked."""

import json
from unittest.mock import patch, MagicMock

import pytest

from shared.ignore_list import (
    IgnoreListUnavailable,
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

    def test_tag_names_match_case_insensitively(self):
        # Azure tag names are case-insensitive; its tag list returns Owner and owner alike.
        assert _tag_matches({"Owner": "alex"}, "owner=alex") is True
        assert _tag_matches({"owner": "x"}, "Owner") is True

    def test_empty_value_rule_needs_the_tag(self):
        assert _tag_matches({"keep": ""}, "keep=") is True
        assert _tag_matches({}, "keep=") is False
        assert _tag_matches({"keep": "yes"}, "keep=") is False


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


# ─── Initial filters from the deployment form ────────────────────────────────


def _not_found_blob():
    blob_client = MagicMock()
    blob_client.download_blob.side_effect = Exception("BlobNotFound")
    return blob_client


class TestInitialFilters:
    FILTERS = {"resource_groups": ["rg-test"], "locations": ["westeurope"],
               "resource_types": ["Microsoft.KeyVault/vaults"],
               "tags": {"include": ["env=prod"], "exclude": ["skip"]}}

    @patch("shared.ignore_list._ensure_container")
    @patch("shared.ignore_list._get_blob_client")
    def test_saved_once_when_no_list_exists(self, mock_get_blob, _container, monkeypatch):
        monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps(self.FILTERS))
        blob_client = _not_found_blob()
        mock_get_blob.return_value = blob_client

        result = load_ignore_list(strict=True)

        assert result["resource_groups"] == ["rg-test"]
        assert result["tags"] == {"include": ["env=prod"], "exclude": ["skip"]}
        assert result["subscriptions"] == [] and result["resource_ids"] == []
        _, kwargs = blob_client.upload_blob.call_args
        assert kwargs == {"overwrite": False}

    @patch("shared.ignore_list._ensure_container")
    @patch("shared.ignore_list._get_blob_client")
    def test_existing_list_wins(self, mock_get_blob, _container, monkeypatch):
        """A saved list (here: everything cleared in Site24x7) is never replaced by the deployment's filters."""
        monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps(self.FILTERS))
        saved = {"resource_groups": [], "locations": [], "resource_ids": [], "subscriptions": [],
                 "tags": {"include": [], "exclude": []}, "resource_types": []}
        blob_client = MagicMock()
        download = MagicMock()
        download.readall.return_value = json.dumps(saved).encode()
        blob_client.download_blob.return_value = download
        mock_get_blob.return_value = blob_client

        assert load_ignore_list(strict=True) == saved
        blob_client.upload_blob.assert_not_called()

    @patch("shared.ignore_list._ensure_container")
    @patch("shared.ignore_list._get_blob_client")
    def test_another_worker_saved_first(self, mock_get_blob, _container, monkeypatch):
        from azure.core.exceptions import ResourceExistsError
        monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps(self.FILTERS))
        winner = {"resource_groups": ["other"], "locations": [], "resource_ids": [], "subscriptions": [],
                  "tags": {"include": [], "exclude": []}, "resource_types": []}
        download = MagicMock()
        download.readall.return_value = json.dumps(winner).encode()
        blob_client = MagicMock()
        blob_client.download_blob.side_effect = [Exception("BlobNotFound"), download]
        blob_client.upload_blob.side_effect = ResourceExistsError("exists")
        mock_get_blob.return_value = blob_client

        assert load_ignore_list(strict=True) == winner

    @patch("shared.ignore_list._get_blob_client")
    def test_no_filters_keeps_empty_list(self, mock_get_blob, monkeypatch):
        monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps({"resource_groups": [], "tags": {"include": [], "exclude": []}}))
        blob_client = _not_found_blob()
        mock_get_blob.return_value = blob_client

        assert load_ignore_list(strict=True)["resource_groups"] == []
        blob_client.upload_blob.assert_not_called()

    @patch("shared.ignore_list._get_blob_client")
    def test_malformed_filters_stop_scans(self, mock_get_blob, monkeypatch):
        """Scans never fall back to collecting everything."""
        monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps({"resource_groups": "rg-test"}))
        mock_get_blob.return_value = _not_found_blob()

        with pytest.raises(IgnoreListUnavailable):
            load_ignore_list(strict=True)

    @patch("shared.ignore_list._ensure_container")
    @patch("shared.ignore_list._get_blob_client")
    def test_save_failure_stops_scans(self, mock_get_blob, _container, monkeypatch):
        monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps(self.FILTERS))
        blob_client = _not_found_blob()
        blob_client.upload_blob.side_effect = Exception("AuthorizationFailure")
        mock_get_blob.return_value = blob_client

        with pytest.raises(IgnoreListUnavailable):
            load_ignore_list(strict=True)


class TestResourceGroupIds:
    """The deployment form saves excluded resource groups as full IDs (one subscription only)."""

    RG_ID = "/subscriptions/aaaa-1111/resourceGroups/rg-test"

    def test_full_id_matches_its_subscription(self):
        resource = {"id": "/subscriptions/aaaa-1111/resourceGroups/RG-Test/providers/Microsoft.KeyVault/vaults/kv1"}
        assert is_ignored(resource, {"resource_groups": [self.RG_ID]}) is True

    def test_full_id_does_not_match_same_name_elsewhere(self):
        resource = {"id": "/subscriptions/bbbb-2222/resourceGroups/rg-test/providers/Microsoft.KeyVault/vaults/kv1"}
        assert is_ignored(resource, {"resource_groups": [self.RG_ID]}) is False

    def test_name_still_matches_every_subscription(self):
        resource = {"id": "/subscriptions/bbbb-2222/resourceGroups/rg-test/providers/Microsoft.KeyVault/vaults/kv1"}
        assert is_ignored(resource, {"resource_groups": ["rg-test"]}) is True


class TestMissingContainer:
    @patch("shared.ignore_list._ensure_container")
    @patch("shared.ignore_list._get_blob_client")
    def test_new_collector_without_config_container_saves_the_filters(self, mock_get_blob, _container, monkeypatch):
        """A new collector has no config container yet: that is 'nothing saved', not a read failure."""
        from azure.core.exceptions import ResourceNotFoundError
        monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps({"resource_groups": ["rg-test"]}))
        blob_client = MagicMock()
        blob_client.download_blob.side_effect = ResourceNotFoundError("The specified container does not exist.")
        mock_get_blob.return_value = blob_client

        assert load_ignore_list(strict=True)["resource_groups"] == ["rg-test"]
        blob_client.upload_blob.assert_called_once()
