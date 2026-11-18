"""F-03 phase 1: identity-capable storage clients; connection strings stay the default."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from shared import storage_clients as sc

CONN = "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=a2V5;EndpointSuffix=core.windows.net"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("AzureWebJobsStorage", "AzureWebJobsStorage__blobServiceUri",
                 "AzureWebJobsStorage__accountName", "AzureWebJobsStorage__credential", "STORAGE_AUTH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sc, "_credential", None)


def test_not_configured():
    assert sc.runtime_storage_mode() is None
    assert sc.runtime_storage_configured() is False
    assert sc.runtime_blob_service() is None


def test_connection_string_wins_over_identity_settings(monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage", CONN)
    monkeypatch.setenv("AzureWebJobsStorage__accountName", "other")
    assert sc.runtime_storage_mode() == sc.MODE_CONNECTION_STRING
    with patch("azure.storage.blob.BlobServiceClient.from_connection_string", return_value="svc") as fcs:
        assert sc.runtime_blob_service() == "svc"
    fcs.assert_called_once_with(CONN)


def test_account_name_selects_identity(monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage__accountName", "acct")
    cred = object()
    monkeypatch.setattr(sc, "_default_credential", lambda: cred)
    assert sc.runtime_storage_mode() == sc.MODE_IDENTITY
    assert sc.runtime_account_name() == "acct"
    with patch("azure.storage.blob.BlobServiceClient") as cls:
        sc.runtime_blob_service()
    cls.assert_called_once_with(account_url="https://acct.blob.core.windows.net", credential=cred)


def test_blob_service_uri_preferred_over_account_name(monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage__accountName", "acct")
    monkeypatch.setenv("AzureWebJobsStorage__blobServiceUri", "https://acct.blob.core.chinacloudapi.cn")
    monkeypatch.setattr(sc, "_default_credential", lambda: "cred")
    with patch("azure.storage.blob.BlobServiceClient") as cls:
        sc.runtime_blob_service()
    assert cls.call_args.kwargs["account_url"] == "https://acct.blob.core.chinacloudapi.cn"


def test_credential_is_cached(monkeypatch):
    created = []
    fake = SimpleNamespace(DefaultAzureCredential=lambda: created.append(1) or object())
    with patch.dict("sys.modules", {"azure.identity": fake}):
        assert sc._default_credential() is sc._default_credential()
    assert len(created) == 1


def _mgmt(key="k1"):
    mgmt = MagicMock()
    mgmt.storage_accounts.list_keys.return_value = SimpleNamespace(keys=[SimpleNamespace(value=key)])
    return mgmt


def test_regional_defaults_to_key_mode():
    mgmt = _mgmt()
    with patch("azure.storage.blob.BlobServiceClient.from_connection_string", return_value="svc") as fcs:
        assert sc.regional_blob_service("acct", storage_mgmt=mgmt, resource_group="rg") == "svc"
    mgmt.storage_accounts.list_keys.assert_called_once_with("rg", "acct")
    assert "AccountKey=k1" in fcs.call_args.args[0] and "AccountName=acct" in fcs.call_args.args[0]


def test_regional_key_mode_with_endpoint_uses_key_credential():
    with patch("azure.storage.blob.BlobServiceClient") as cls:
        sc.regional_blob_service("acct", blob_endpoint="https://acct.blob.core.windows.net",
                                 storage_mgmt=_mgmt(), resource_group="rg")
    assert cls.call_args.kwargs["credential"] == {"account_name": "acct", "account_key": "k1"}


def test_regional_identity_mode_never_lists_keys(monkeypatch):
    monkeypatch.setenv("STORAGE_AUTH", "identity")
    mgmt = _mgmt()
    cred = object()
    with patch("azure.storage.blob.BlobServiceClient") as cls:
        sc.regional_blob_service("acct", blob_endpoint="https://acct.blob.core.windows.net",
                                 credential=cred, storage_mgmt=mgmt, resource_group="rg")
    mgmt.storage_accounts.list_keys.assert_not_called()
    cls.assert_called_once_with(account_url="https://acct.blob.core.windows.net", credential=cred)


def test_regional_identity_rejects_foreign_endpoint(monkeypatch):
    monkeypatch.setenv("STORAGE_AUTH", "identity")
    for bad in ("http://acct.blob.core.windows.net", "https://evil.example.com", "https://other.blob.core.windows.net"):
        with pytest.raises(ValueError):
            sc.regional_blob_service("acct", blob_endpoint=bad, credential=object())


def test_no_key_fallback_after_403_in_identity_mode(monkeypatch):
    """A denied identity request surfaces as an error; no key is ever read."""
    from shared import collection
    monkeypatch.setenv("STORAGE_AUTH", "identity")
    acct = SimpleNamespace(name="s247diageastusx", tags={"purpose": "diag-logs-regional", "region": "eastus"},
                           primary_location="eastus", primary_endpoints=None)
    storage = MagicMock()
    storage.storage_accounts.list_by_resource_group.return_value = [acct]
    service = MagicMock()
    service.list_containers.side_effect = PermissionError("403 AuthorizationPermissionMismatch")
    region_mgr = MagicMock()
    region_mgr.set_retention.return_value = True
    with patch("azure.mgmt.storage.StorageManagementClient", return_value=storage), \
         patch("azure.storage.blob.BlobServiceClient", return_value=service), \
         patch("shared.region_manager.is_own_account", return_value=True), \
         patch("shared.collection._drop_checkpoints", return_value=True) as dropped:
        counts = collection._clear_storage(region_mgr, "rg")
    assert counts["storage_errors"] == 1 and counts["containers_cleared"] == 0
    storage.storage_accounts.list_keys.assert_not_called()
    dropped.assert_not_called()  # denied: checkpoints are kept


def test_checkpoint_and_state_helpers_use_identity_client(monkeypatch):
    """Identity-configured storage is 'configured' for every consumer (not local mode)."""
    from shared import config_store
    monkeypatch.setenv("AzureWebJobsStorage__accountName", "acct")
    service = MagicMock()
    with patch("shared.config_store.runtime_blob_service", return_value=service):
        assert config_store._get_service_client() is service
    with patch("shared.config_store._read_blob", return_value=None) as rb:
        assert config_store.get_processing_enabled() is True
    rb.assert_called_once()  # the blob was consulted, not the local-mode shortcut


def test_updater_identity_mode_uses_user_delegation_sas(monkeypatch, tmp_path):
    import shared.updater as up
    monkeypatch.setenv("AzureWebJobsStorage__accountName", "acct")
    pkg = tmp_path / "p.zip"
    pkg.write_bytes(b"zip")
    service = MagicMock()
    service.account_name = "acct"
    service.get_user_delegation_key.return_value = "udk"
    service.get_container_client.return_value.get_blob_client.return_value.url = "https://acct.blob.core.windows.net/c/b"
    with patch("shared.storage_clients.runtime_blob_service", return_value=service), \
         patch("azure.storage.blob.generate_blob_sas", return_value="sig=1") as gen:
        link = up._stage_verified_package(str(pkg), "2.0.0", "ab" * 32)
    kwargs = gen.call_args.kwargs
    assert kwargs["user_delegation_key"] == "udk" and "account_key" not in kwargs
    assert kwargs["protocol"] == "https" and kwargs["permission"].read and not kwargs["permission"].write
    start, expiry = service.get_user_delegation_key.call_args.args
    assert (expiry - start).total_seconds() <= 6 * 3600 + 300
    assert (kwargs["expiry"] - start).total_seconds() <= 6 * 3600 + 300
    assert link.endswith("?sig=1")


def test_updater_connection_string_mode_uses_account_key_sas(monkeypatch, tmp_path):
    import shared.updater as up
    monkeypatch.setenv("AzureWebJobsStorage", CONN)
    pkg = tmp_path / "p.zip"
    pkg.write_bytes(b"zip")
    with patch("azure.storage.blob.BlobClient.upload_blob"), \
         patch("azure.storage.blob.ContainerClient.create_container"), \
         patch("azure.storage.blob.generate_blob_sas", return_value="sig=2") as gen:
        up._stage_verified_package(str(pkg), "2.0.0", "ab" * 32)
    assert gen.call_args.kwargs["account_key"] == "a2V5"
    assert "user_delegation_key" not in gen.call_args.kwargs


def test_debug_info_shows_mode_not_credentials(monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage__accountName", "acct")
    assert sc.runtime_storage_mode() == "identity"
    assert sc.runtime_account_name() == "acct"
