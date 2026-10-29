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
    LIST_KEYS,
    LegacyFilters,
    _initial_filters,
    validate_ignore_list,
    _migrate_tags,
    _tag_matches,
)


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
    @pytest.mark.parametrize("key,detail,selected,other", [
        ("include_subscriptions", "subscription_id", "sub-a", "sub-b"),
        ("include_locations", "location", "eastus", "westus"),
        ("include_resource_types", "type", "Microsoft.Web/sites", "Microsoft.KeyVault/vaults"),
    ])
    def test_include_lists(self, key, detail, selected, other):
        assert is_ignored({detail: selected.upper()}, {key: [selected]}) is False
        assert is_ignored({detail: other}, {key: [selected]}) is True
        # Empty means all, including values discovered after filters were saved.
        assert is_ignored({detail: other}, {key: []}) is False
        assert is_ignored({"id": "/unknown/resource"}, {key: [selected]}) is False

    def test_hosting_subscription_always_included(self, monkeypatch):
        monkeypatch.setenv("SUBSCRIPTION_IDS", " HOST , other")
        filters = {"include_subscriptions": ["selected"]}
        assert is_ignored({"subscription_id": "host"}, filters) is False
        assert is_ignored({"subscription_id": "other"}, filters) is True
        # Hosting exemption does not bypass an explicit resource exclusion.
        rid = "/subscriptions/HOST/providers/M/T/r"
        assert is_ignored({"id": rid}, dict(filters, resource_ids=[rid.lower()])) is True

    @pytest.mark.parametrize("location", ["global", "GLOBAL", ""])
    def test_nonregional_resources_pass(self, location):
        assert is_ignored({"location": location}, {"include_locations": ["eastus"]}) is False

    def test_resource_id_exclusion_is_case_insensitive(self):
        assert is_ignored({"id": "/SUB/R"}, {"resource_ids": ["/sub/r"]}) is True

    def test_subscription_extracted_from_id(self):
        resource = {"id": "/subscriptions/sub-abc/resourceGroups/rg/providers/M/T/R"}
        assert is_ignored(resource, {"include_subscriptions": ["sub-abc"]}) is False
        assert is_ignored(resource, {"include_subscriptions": ["sub-other"]}) is True

    # ── Exclude tags ──

    def test_ignored_by_exclude_tag_key_value(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "tags": {"env": "dev"}}
        ignore = {"include_locations": [], "resource_ids": [], "include_subscriptions": [],
                  "tags": {"include": [], "exclude": ["env=dev"]}}
        assert is_ignored(resource, ignore) is True

    def test_ignored_by_exclude_tag_key_only(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "tags": {"temporary": "yes"}}
        ignore = {"include_locations": [], "resource_ids": [], "include_subscriptions": [],
                  "tags": {"include": [], "exclude": ["temporary"]}}
        assert is_ignored(resource, ignore) is True

    def test_exclude_tag_no_match(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "tags": {"env": "prod"}}
        ignore = {"tags": {"include": [], "exclude": ["env=dev"]}}
        assert is_ignored(resource, ignore) is False

    def test_no_tags_on_resource_exclude(self):
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1"}
        ignore = {"tags": {"include": [], "exclude": ["env=dev"]}}
        assert is_ignored(resource, ignore) is False

    # ── Include tags ──

    def test_include_tags_resource_matches(self):
        """Resource has a matching include tag — NOT ignored."""
        resource = {"id": "/sub/rg/res1", "location": "eastus", "tags": {"env": "production"}}
        ignore = {"tags": {"include": ["env=production"], "exclude": []}}
        assert is_ignored(resource, ignore) is False

    def test_include_tags_resource_no_match(self):
        """Resource does NOT match any include tag — ignored."""
        resource = {"id": "/sub/rg/res1", "location": "eastus", "tags": {"env": "dev"}}
        ignore = {"tags": {"include": ["env=production"], "exclude": []}}
        assert is_ignored(resource, ignore) is True

    def test_include_tags_empty_means_all_allowed(self):
        """Empty include list = no allow-list filter — all resources pass."""
        resource = {"id": "/sub/rg/res1", "location": "eastus", "tags": {"env": "dev"}}
        ignore = {"tags": {"include": [], "exclude": []}}
        assert is_ignored(resource, ignore) is False

    def test_include_tags_no_tags_on_resource(self):
        """Resource has no tags at all, include tags defined — ignored."""
        resource = {"id": "/sub/rg/res1", "location": "eastus", "resource_group": "rg1"}
        ignore = {"tags": {"include": ["monitored"], "exclude": []}}
        assert is_ignored(resource, ignore) is True

    def test_include_key_only(self):
        """Include tag with key-only matches any value."""
        resource = {"id": "/sub/rg/res1", "location": "eastus", "tags": {"monitored": "true"}}
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
        resource = {"id": "/sub/rg/res1", "location": "eastus", "tags": {"env": "dev"}}
        ignore = {"tags": ["env=dev"]}
        assert is_ignored(resource, ignore) is True

# ─── load_ignore_list ────────────────────────────────────────────────────────


class TestLoadIgnoreList:
    @patch("shared.ignore_list._get_blob_client")
    def test_load_existing(self, mock_get_blob):
        data = {"include_locations": [], "resource_ids": [],
                "tags": {"include": ["env=prod"], "exclude": ["env=dev"]}}
        blob_client = MagicMock()
        download = MagicMock()
        download.readall.return_value = json.dumps(data).encode()
        blob_client.download_blob.return_value = download
        mock_get_blob.return_value = blob_client

        result = load_ignore_list()
        assert result == {**{key: [] for key in LIST_KEYS}, **data}

    @patch("shared.ignore_list._get_blob_client")
    def test_load_migrates_legacy_tags(self, mock_get_blob):
        """Legacy flat tags list is auto-migrated to include/exclude."""
        data = {"include_locations": [], "resource_ids": [], "tags": ["env=dev"]}
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
        assert result == {**{key: [] for key in LIST_KEYS}, "tags": {"include": [], "exclude": []}}

    @patch("shared.ignore_list._get_blob_client")
    def test_load_no_connection(self, mock_get_blob):
        mock_get_blob.return_value = None
        result = load_ignore_list()
        assert result == {**{key: [] for key in LIST_KEYS}, "tags": {"include": [], "exclude": []}}

    @patch("shared.ignore_list._get_blob_client")
    def test_load_returns_independent_copies(self, mock_get_blob):
        """Ensure multiple calls return independent objects (deep copy, not shared refs)."""
        mock_get_blob.return_value = None
        result1 = load_ignore_list()
        result2 = load_ignore_list()
        # Mutate the nested tags in result1
        result1["tags"]["exclude"].append("env=test")
        result1["include_subscriptions"].append("sub-new")
        # result2 should be unaffected
        assert result2["tags"]["exclude"] == []
        assert result2["include_subscriptions"] == []


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

        data = {"include_locations": [], "resource_ids": [], "tags": {"include": [], "exclude": []}}
        assert save_ignore_list(data) is True
        blob.upload_blob.assert_called_once()

    def test_save_no_connection(self):
        assert save_ignore_list({"include_subscriptions": []}) is False


# ─── update_ignore_list ──────────────────────────────────────────────────────


class TestUpdateIgnoreList:
    @patch("shared.ignore_list.save_ignore_list")
    def test_adds_missing_keys(self, mock_save):
        mock_save.return_value = True
        data = {"include_subscriptions": ["sub-1"]}
        assert update_ignore_list(data) is True
        saved = mock_save.call_args[0][0]
        assert "include_locations" in saved
        assert "resource_ids" in saved
        assert saved["tags"] == {"include": [], "exclude": []}

    @patch("shared.ignore_list.save_ignore_list")
    def test_keeps_existing_keys(self, mock_save):
        mock_save.return_value = True
        data = {"include_subscriptions": ["a"], "include_locations": ["b"], "resource_ids": ["c"],
                "tags": {"include": ["env=prod"], "exclude": ["env=dev"]}}
        update_ignore_list(data)
        saved = mock_save.call_args[0][0]
        assert saved["tags"] == {"include": ["env=prod"], "exclude": ["env=dev"]}

    @patch("shared.ignore_list.save_ignore_list")
    def test_migrates_legacy_tags(self, mock_save):
        mock_save.return_value = True
        data = {"tags": ["env=dev"]}
        update_ignore_list(data)
        saved = mock_save.call_args[0][0]
        assert saved["tags"] == {"include": [], "exclude": ["env=dev"]}


# ─── Initial filters from the deployment form ────────────────────────────────


def _not_found_blob():
    blob_client = MagicMock()
    blob_client.download_blob.side_effect = Exception("BlobNotFound")
    return blob_client


class TestInitialFilters:
    FILTERS = {"include_subscriptions": ["sub-test"], "include_locations": ["westeurope"],
               "include_resource_types": ["Microsoft.KeyVault/vaults"],
               "tags": {"include": ["env=prod"], "exclude": ["skip"]}}

    @patch("shared.ignore_list._ensure_container")
    @patch("shared.ignore_list._get_blob_client")
    def test_saved_once_when_no_list_exists(self, mock_get_blob, _container, monkeypatch):
        monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps(self.FILTERS))
        blob_client = _not_found_blob()
        mock_get_blob.return_value = blob_client

        result = load_ignore_list(strict=True)

        assert result["include_subscriptions"] == ["sub-test"]
        assert result["tags"] == {"include": ["env=prod"], "exclude": ["skip"]}
        assert result["resource_ids"] == []
        _, kwargs = blob_client.upload_blob.call_args
        assert kwargs == {"overwrite": False}

    @patch("shared.ignore_list._ensure_container")
    @patch("shared.ignore_list._get_blob_client")
    def test_existing_list_wins(self, mock_get_blob, _container, monkeypatch):
        """A saved list (here: everything cleared in Site24x7) is never replaced by the deployment's filters."""
        monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps(self.FILTERS))
        saved = {"include_locations": [], "resource_ids": [], "include_subscriptions": [],
                 "tags": {"include": [], "exclude": []}, "include_resource_types": []}
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
        winner = {"include_subscriptions": ["other"], "include_locations": [], "resource_ids": [],
                  "tags": {"include": [], "exclude": []}, "include_resource_types": []}
        download = MagicMock()
        download.readall.return_value = json.dumps(winner).encode()
        blob_client = MagicMock()
        blob_client.download_blob.side_effect = [Exception("BlobNotFound"), download]
        blob_client.upload_blob.side_effect = ResourceExistsError("exists")
        mock_get_blob.return_value = blob_client

        assert load_ignore_list(strict=True) == winner

    @patch("shared.ignore_list._get_blob_client")
    def test_no_filters_keeps_empty_list(self, mock_get_blob, monkeypatch):
        monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps({"tags": {"include": [], "exclude": []}}))
        blob_client = _not_found_blob()
        mock_get_blob.return_value = blob_client

        assert load_ignore_list(strict=True)["include_subscriptions"] == []
        blob_client.upload_blob.assert_not_called()

    @patch("shared.ignore_list._get_blob_client")
    def test_malformed_filters_stop_scans(self, mock_get_blob, monkeypatch):
        """Scans never fall back to collecting everything."""
        monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps({"include_subscriptions": "rg-test"}))
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


class TestMissingContainer:
    @patch("shared.ignore_list._ensure_container")
    @patch("shared.ignore_list._get_blob_client")
    def test_new_collector_without_config_container_saves_the_filters(self, mock_get_blob, _container, monkeypatch):
        """A new collector has no config container yet: that is 'nothing saved', not a read failure."""
        from azure.core.exceptions import ResourceNotFoundError
        monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps({"include_subscriptions": ["sub-test"]}))
        blob_client = MagicMock()
        blob_client.download_blob.side_effect = ResourceNotFoundError("The specified container does not exist.")
        mock_get_blob.return_value = blob_client

        assert load_ignore_list(strict=True)["include_subscriptions"] == ["sub-test"]
        blob_client.upload_blob.assert_called_once()


LEGACY_KEYS = ("subscriptions", "resource_groups", "locations", "resource_types")


@pytest.mark.parametrize("key", LEGACY_KEYS)
def test_empty_legacy_key_is_dropped_on_load(key):
    blob = MagicMock()
    blob.download_blob.return_value.readall.return_value = json.dumps(
        {key: [], "include_locations": ["eastus"], "tags": ["skip"]})
    with patch("shared.ignore_list._get_blob_client", return_value=blob):
        result = load_ignore_list(strict=True)
    assert key not in result
    assert result["include_locations"] == ["eastus"]
    assert set(result) == set(LIST_KEYS) | {"tags"}
    assert result["tags"] == {"include": [], "exclude": ["skip"]}


@pytest.mark.parametrize("key", LEGACY_KEYS)
def test_nonempty_legacy_key_stops_scans_but_dashboard_returns_empty(key):
    blob = MagicMock()
    blob.download_blob.return_value.readall.return_value = json.dumps({key: ["old-rule"]})
    with patch("shared.ignore_list._get_blob_client", return_value=blob):
        with pytest.raises(IgnoreListUnavailable, match="legacy exclusion filters") as error:
            load_ignore_list(strict=True)
        assert isinstance(error.value.__cause__, LegacyFilters)
        assert load_ignore_list() == {**{k: [] for k in LIST_KEYS}, "tags": {"include": [], "exclude": []}}
    blob.upload_blob.assert_not_called()


@pytest.mark.parametrize("key", LEGACY_KEYS)
def test_validator_rejects_legacy_keys_even_when_empty(key):
    assert "Unknown keys" in validate_ignore_list({key: []})


@pytest.mark.parametrize("key", LIST_KEYS)
def test_validator_accepts_current_list_keys(key):
    assert validate_ignore_list({key: ["value"]}) == ""


@pytest.mark.parametrize("key", LEGACY_KEYS)
def test_seed_drops_empty_legacy_keys_before_validation(key, monkeypatch):
    monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps({key: [], "include_locations": ["eastus"]}))
    result = _initial_filters()
    assert key not in result
    assert result["include_locations"] == ["eastus"]


@pytest.mark.parametrize("key", LEGACY_KEYS)
def test_seed_nonempty_legacy_keys_raise_legacy_filters(key, monkeypatch):
    monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps({key: ["old-rule"]}))
    with pytest.raises(LegacyFilters):
        _initial_filters()


@pytest.mark.parametrize("key", LEGACY_KEYS)
def test_legacy_seed_failure_is_strict_or_empty(key, monkeypatch):
    monkeypatch.setenv("INITIAL_RESOURCE_FILTERS", json.dumps({key: ["old-rule"]}))
    blob = _not_found_blob()
    with patch("shared.ignore_list._get_blob_client", return_value=blob):
        with pytest.raises(IgnoreListUnavailable):
            load_ignore_list(strict=True)
        assert load_ignore_list() == {**{k: [] for k in LIST_KEYS}, "tags": {"include": [], "exclude": []}}
    blob.upload_blob.assert_not_called()
