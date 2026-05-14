"""Tests for shared/config_store.py — all blob operations mocked."""

import json
from unittest.mock import patch, MagicMock

import pytest

from shared import config_store


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset module-level cache before each test."""
    config_store.clear_cache()
    yield
    config_store.clear_cache()


def _mock_service_client(blob_data=None, list_blobs_result=None):
    """Build a mock BlobServiceClient with optional pre-loaded blob data."""
    svc = MagicMock()
    blob_client = MagicMock()
    container_client = MagicMock()

    svc.get_blob_client.return_value = blob_client
    svc.get_container_client.return_value = container_client
    container_client.exists.return_value = True

    if blob_data is not None:
        download = MagicMock()
        download.readall.return_value = json.dumps(blob_data).encode()
        blob_client.download_blob.return_value = download
    else:
        blob_client.download_blob.side_effect = Exception("BlobNotFound")

    if list_blobs_result is not None:
        container_client.list_blobs.return_value = list_blobs_result

    return svc


# ─── Supported Log Types ────────────────────────────────────────────────────


class TestSupportedLogTypes:
    @patch("shared.config_store._get_service_client")
    def test_get_supported_empty(self, mock_get_svc):
        mock_get_svc.return_value = _mock_service_client(blob_data=None)
        result = config_store.get_supported_log_types()
        assert result == {}

    @patch("shared.config_store._get_service_client")
    def test_get_supported_returns_data(self, mock_get_svc):
        data = {"auditlogs": {"logtype": "AuditLogs"}}
        mock_get_svc.return_value = _mock_service_client(blob_data=data)
        result = config_store.get_supported_log_types()
        assert result == data

    @patch("shared.config_store._get_service_client")
    def test_get_supported_caches(self, mock_get_svc):
        data = {"auditlogs": {"logtype": "AuditLogs"}}
        mock_get_svc.return_value = _mock_service_client(blob_data=data)
        config_store.get_supported_log_types()
        config_store.get_supported_log_types()
        # _read_blob internally calls _get_service_client,
        # cache means second call shouldn't trigger blob read again
        assert mock_get_svc.call_count == 1

    @patch("shared.config_store._write_blob")
    def test_save_supported(self, mock_write):
        mock_write.return_value = True
        data = {"auditlogs": {"logtype": "AuditLogs"}}
        assert config_store.save_supported_log_types(data) is True
        # Verify cache updated
        assert config_store.get_supported_log_types() == data

    @patch("shared.config_store._get_service_client")
    def test_is_supported_normalized(self, mock_get_svc):
        data = {"auditlogs": {"logtype": "AuditLogs"}}
        mock_get_svc.return_value = _mock_service_client(blob_data=data)
        assert config_store.is_supported_log_type("audit-logs") is True
        assert config_store.is_supported_log_type("Audit_Logs") is True
        assert config_store.is_supported_log_type("unknown") is False


# ─── Log Type Configs ────────────────────────────────────────────────────────


class TestLogTypeConfigs:
    @patch("shared.config_store._read_blob")
    def test_get_logtype_config_found(self, mock_read):
        cfg = {"logType": "S247_AuditLogs", "apiKey": "k"}
        mock_read.return_value = json.dumps(cfg)
        result = config_store.get_logtype_config("AuditLogs")
        assert result == cfg
        mock_read.assert_called_once_with("logtype-configs/S247_auditlogs.json")

    @patch("shared.config_store._read_blob")
    def test_get_logtype_config_not_found(self, mock_read):
        mock_read.return_value = None
        assert config_store.get_logtype_config("Unknown") is None

    @patch("shared.config_store._write_blob")
    def test_save_logtype_config(self, mock_write):
        mock_write.return_value = True
        cfg = {"logType": "S247_Test"}
        assert config_store.save_logtype_config("Test", cfg) is True
        mock_write.assert_called_once()
        # Should be cached now
        assert config_store.get_logtype_config("Test") == cfg

    @patch("shared.config_store._delete_blob")
    def test_delete_logtype_config(self, mock_del):
        mock_del.return_value = True
        # Pre-populate cache with normalized key
        config_store._cache["logtype_configs"]["S247_test"] = {"x": 1}
        assert config_store.delete_logtype_config("Test") is True
        assert "S247_test" not in config_store._cache["logtype_configs"]

    @patch("shared.config_store._get_service_client")
    @patch("shared.config_store._read_blob")
    def test_get_all_logtype_configs(self, mock_read, mock_get_svc):
        mock_blob1 = MagicMock()
        mock_blob1.name = "logtype-configs/S247_A.json"
        mock_blob2 = MagicMock()
        mock_blob2.name = "logtype-configs/S247_B.json"

        svc = MagicMock()
        container_client = MagicMock()
        svc.get_container_client.return_value = container_client
        container_client.list_blobs.return_value = [mock_blob1, mock_blob2]
        mock_get_svc.return_value = svc

        mock_read.side_effect = [
            json.dumps({"logType": "A"}),
            json.dumps({"logType": "B"}),
        ]
        result = config_store.get_all_logtype_configs()
        assert "S247_A" in result
        assert "S247_B" in result


# ─── Disabled Log Types ──────────────────────────────────────────────────────


class TestDisabledLogTypes:
    @patch("shared.config_store._read_blob")
    def test_get_disabled_empty(self, mock_read):
        mock_read.return_value = None
        assert config_store.get_disabled_log_types() == []

    @patch("shared.config_store._read_blob")
    def test_get_disabled_returns_list(self, mock_read):
        mock_read.return_value = json.dumps(["AuditLogs", "SignInLogs"])
        result = config_store.get_disabled_log_types()
        assert result == ["AuditLogs", "SignInLogs"]

    @patch("shared.config_store._rmw_blob")
    def test_disable_log_type(self, mock_rmw):
        mock_rmw.return_value = ["Existing", "NewCat"]
        assert config_store.disable_log_type("NewCat") is True
        # Ensure mutate appended the new category
        mutate = mock_rmw.call_args.args[1]
        assert mutate(["Existing"]) == ["Existing", "NewCat"]

    @patch("shared.config_store._rmw_blob")
    def test_disable_already_disabled(self, mock_rmw):
        mock_rmw.return_value = None  # simulates abort (already present)
        # Still returns True — idempotent
        assert config_store.disable_log_type("auditlogs") is True
        # Verify mutate returns None for already-disabled
        mutate = mock_rmw.call_args.args[1]
        assert mutate(["AuditLogs"]) is None

    @patch("shared.config_store._rmw_blob")
    def test_enable_log_type(self, mock_rmw):
        mock_rmw.return_value = ["SignInLogs"]
        assert config_store.enable_log_type("AuditLogs") is True
        mutate = mock_rmw.call_args.args[1]
        assert mutate(["AuditLogs", "SignInLogs"]) == ["SignInLogs"]

    def test_is_log_type_disabled(self):
        config_store._cache["disabled_types"] = ["AuditLogs", "SignInLogs"]
        assert config_store.is_log_type_disabled("auditlogs") is True
        assert config_store.is_log_type_disabled("Unknown") is False


# ─── Configured Resources ───────────────────────────────────────────────────


class TestConfiguredResources:
    @patch("shared.config_store._read_blob")
    def test_get_configured_empty(self, mock_read):
        mock_read.return_value = None
        assert config_store.get_configured_resources() == {}

    @patch("shared.config_store._rmw_blob")
    def test_mark_resource_configured(self, mock_rmw):
        mock_rmw.return_value = {"/sub/rg/res1": {"categories": ["AuditLogs"]}}
        result = config_store.mark_resource_configured(
            "/sub/rg/res1", ["AuditLogs"], "sa1"
        )
        assert result is True
        mutate = mock_rmw.call_args.args[1]
        out = mutate({})
        assert "/sub/rg/res1" in out
        assert out["/sub/rg/res1"]["categories"] == ["AuditLogs"]

    @patch("shared.config_store._rmw_blob")
    def test_unmark_resource(self, mock_rmw):
        mock_rmw.return_value = {}
        config_store._cache["configured_resources"] = {
            "/res1": {"categories": ["A"], "storage_account": "sa1"}
        }
        assert config_store.unmark_resource_configured("/res1") is True
        mutate = mock_rmw.call_args.args[1]
        assert mutate({"/res1": {"x": 1}}) == {}

    @patch("shared.config_store._rmw_blob")
    def test_unmark_resource_not_tracked(self, mock_rmw):
        mock_rmw.return_value = None  # abort
        config_store._cache["configured_resources"] = {}
        assert config_store.unmark_resource_configured("/nonexistent") is True
        mutate = mock_rmw.call_args.args[1]
        assert mutate({}) is None


# ─── Cache ───────────────────────────────────────────────────────────────────


class TestClearCache:
    def test_clears_all(self):
        config_store._cache["supported_types"] = {"x": 1}
        config_store._cache["disabled_types"] = ["y"]
        config_store._cache["logtype_configs"] = {"z": {}}
        config_store._cache["configured_resources"] = {"w": {}}
        config_store.clear_cache()
        assert config_store._cache["supported_types"] is None
        assert config_store._cache["disabled_types"] is None
        assert config_store._cache["logtype_configs"] == {}
        assert config_store._cache["configured_resources"] is None


# ─── RMW helper + scan lock ─────────────────────────────────────────────────


class TestRmwBlob:
    @patch("shared.config_store._write_blob_conditional")
    @patch("shared.config_store._read_blob_with_etag")
    def test_rmw_succeeds_first_try(self, mock_read, mock_write):
        mock_read.return_value = (json.dumps([1, 2]), "etag-a")
        mock_write.return_value = (True, False)
        result = config_store._rmw_blob("x", lambda c: c + [3], default=[])
        assert result == [1, 2, 3]
        # etag passed through
        assert mock_write.call_args.args[2] == "etag-a"

    @patch("shared.config_store._write_blob_conditional")
    @patch("shared.config_store._read_blob_with_etag")
    def test_rmw_retries_on_conflict(self, mock_read, mock_write):
        # First read: [1]; conflict on write. Second read: [1, 2]; success.
        mock_read.side_effect = [
            (json.dumps([1]), "etag-a"),
            (json.dumps([1, 2]), "etag-b"),
        ]
        mock_write.side_effect = [(False, True), (True, False)]
        result = config_store._rmw_blob("x", lambda c: c + [99], default=[])
        assert result == [1, 2, 99]  # mutator saw fresh [1,2] after conflict
        assert mock_read.call_count == 2
        assert mock_write.call_count == 2

    @patch("shared.config_store._write_blob_conditional")
    @patch("shared.config_store._read_blob_with_etag")
    def test_rmw_aborts_on_mutator_none(self, mock_read, mock_write):
        mock_read.return_value = (json.dumps([1]), "etag-a")
        result = config_store._rmw_blob("x", lambda c: None, default=[])
        assert result is None
        mock_write.assert_not_called()

    @patch("shared.config_store._write_blob_conditional")
    @patch("shared.config_store._read_blob_with_etag")
    def test_rmw_gives_up_after_max_retries(self, mock_read, mock_write):
        mock_read.return_value = (json.dumps([]), "e")
        mock_write.return_value = (False, True)  # always conflicts
        result = config_store._rmw_blob(
            "x", lambda c: c + [1], default=[], max_retries=3
        )
        assert result is None
        assert mock_write.call_count == 3


class TestScanLock:
    @patch("shared.config_store._rmw_blob")
    def test_acquire_when_idle(self, mock_rmw):
        # Simulate RMW applying the mutate
        def fake_rmw(path, mutate, default=None, max_retries=5):
            result = mutate(default if default is not None else {})
            return result

        mock_rmw.side_effect = fake_rmw
        assert config_store.try_acquire_scan_lock() is True

    @patch("shared.config_store._rmw_blob")
    def test_skip_when_scan_active(self, mock_rmw):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()

        def fake_rmw(path, mutate, default=None, max_retries=5):
            # Active recent scan
            return mutate({"in_progress": True, "scan_started_at": now})

        mock_rmw.side_effect = fake_rmw
        assert config_store.try_acquire_scan_lock() is False

    @patch("shared.config_store._rmw_blob")
    def test_overrides_stale_lock(self, mock_rmw):
        from datetime import datetime, timezone, timedelta

        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        def fake_rmw(path, mutate, default=None, max_retries=5):
            return mutate({"in_progress": True, "scan_started_at": stale})

        mock_rmw.side_effect = fake_rmw
        # Stale > 15 min — should take lock
        assert config_store.try_acquire_scan_lock(ttl_seconds=900) is True
