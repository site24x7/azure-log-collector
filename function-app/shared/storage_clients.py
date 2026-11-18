"""Blob clients for the Function App's own (runtime) storage and for diagnostic accounts.

Runtime storage: an exact AzureWebJobsStorage connection string is used when set
(the default today). Otherwise the identity settings AzureWebJobsStorage__blobServiceUri
or AzureWebJobsStorage__accountName select the app's managed identity.

Regional / tenant accounts: account keys (ARM list_keys) by default. With the app
setting STORAGE_AUTH=identity the managed identity is used instead, and a denied
request is never retried with a key.
"""

import logging
import os
import threading
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CONNECTION_STRING_SETTING = "AzureWebJobsStorage"
BLOB_SERVICE_URI_SETTING = "AzureWebJobsStorage__blobServiceUri"
ACCOUNT_NAME_SETTING = "AzureWebJobsStorage__accountName"
STORAGE_AUTH_SETTING = "STORAGE_AUTH"

MODE_CONNECTION_STRING = "connection_string"
MODE_IDENTITY = "identity"

_credential = None
_credential_lock = threading.Lock()


def _default_credential():
    """One shared DefaultAzureCredential for the process."""
    global _credential
    if _credential is None:
        with _credential_lock:
            if _credential is None:
                from azure.identity import DefaultAzureCredential
                _credential = DefaultAzureCredential()
    return _credential


def _runtime_identity_endpoint() -> str:
    uri = os.environ.get(BLOB_SERVICE_URI_SETTING, "").strip()
    if uri:
        return uri
    name = os.environ.get(ACCOUNT_NAME_SETTING, "").strip()
    if name:
        return f"https://{name}.blob.core.windows.net"
    return ""


def runtime_storage_mode() -> Optional[str]:
    """MODE_CONNECTION_STRING, MODE_IDENTITY or None (not configured)."""
    if os.environ.get(CONNECTION_STRING_SETTING, ""):
        return MODE_CONNECTION_STRING
    if _runtime_identity_endpoint():
        return MODE_IDENTITY
    return None


def runtime_storage_configured() -> bool:
    return runtime_storage_mode() is not None


def runtime_account_name() -> str:
    """Account name of the identity-configured runtime storage ('' otherwise)."""
    if runtime_storage_mode() != MODE_IDENTITY:
        return ""
    host = urlparse(_runtime_identity_endpoint()).hostname or ""
    return host.split(".")[0]


def runtime_blob_service():
    """BlobServiceClient for the runtime storage, or None when it is not configured."""
    from azure.storage.blob import BlobServiceClient
    mode = runtime_storage_mode()
    if mode == MODE_CONNECTION_STRING:
        return BlobServiceClient.from_connection_string(os.environ[CONNECTION_STRING_SETTING])
    if mode == MODE_IDENTITY:
        return BlobServiceClient(account_url=_runtime_identity_endpoint(), credential=_default_credential())
    return None


def regional_auth_mode() -> str:
    """'identity' when STORAGE_AUTH=identity, else 'key' (the default)."""
    return "identity" if os.environ.get(STORAGE_AUTH_SETTING, "").strip().lower() == MODE_IDENTITY else "key"


def _check_endpoint(account_name: str, blob_endpoint: str) -> str:
    parsed = urlparse(blob_endpoint)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host.startswith(account_name.lower() + ".blob."):
        raise ValueError("Unexpected blob endpoint for the storage account")
    return blob_endpoint


def regional_blob_service(account_name: str, blob_endpoint: Optional[str] = None, credential=None,
                          storage_mgmt=None, resource_group: Optional[str] = None):
    """BlobServiceClient for a regional or tenant diagnostic account.

    Key mode (default) reads an account key with storage_mgmt.storage_accounts.list_keys.
    Identity mode uses ``credential`` (or the app's default credential) and never reads keys,
    so a 403 surfaces as an error instead of a key retry.
    """
    from azure.storage.blob import BlobServiceClient
    if regional_auth_mode() == MODE_IDENTITY:
        endpoint = _check_endpoint(account_name, blob_endpoint or f"https://{account_name}.blob.core.windows.net")
        return BlobServiceClient(account_url=endpoint, credential=credential or _default_credential())
    if storage_mgmt is None or not resource_group:
        raise ValueError("Key mode needs the storage management client and resource group")
    key = storage_mgmt.storage_accounts.list_keys(resource_group, account_name).keys[0].value
    if blob_endpoint:
        return BlobServiceClient(account_url=blob_endpoint,
                                 credential={"account_name": account_name, "account_key": key})
    conn_str = (
        f"DefaultEndpointsProtocol=https;AccountName={account_name};"
        f"AccountKey={key};EndpointSuffix=core.windows.net"
    )
    return BlobServiceClient.from_connection_string(conn_str)
