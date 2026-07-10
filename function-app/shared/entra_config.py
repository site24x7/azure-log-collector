"""Entra ID (Azure AD) tenant-log collection configuration.

Entra ID logs are **tenant-scoped**, not resource- or subscription-scoped, and
are configured via the ``microsoft.aadiam/diagnosticSettings`` provider. That
API only accepts **delegated (user) tokens** — a service principal / managed
identity gets HTTP 403 for ``microsoft.aadiam/diagnosticSettings/write`` even
with Global Administrator. (Confirmed Azure platform limitation; the Terraform
azurerm provider documents the same.)

Consequence: this collector's Managed Identity **cannot** create the Entra
diagnostic setting. A tenant admin must create it manually (portal / PowerShell
/ CLI with their own login), pointing it at the storage account this collector
exposes. Our side is therefore *receive-only* for Entra:

  1. expose a stable target storage account (see
     ``RegionManager.get_primary_storage_account``),
  2. let the operator provision each Entra log type ON DEMAND from the
     dashboard's Entra tab (``UpdateEntraLogTypes``), which creates the S247
     log type + stores its config so BlobLogProcessor doesn't drop the logs,
  3. process the resulting ``insights-logs-*`` blobs (already handled).

This module just enumerates the categories; provisioning state lives in
``config_store`` (``get/set_entra_logtype_states``). See ``docs/entra-id-logs.md``
for the manual onboarding steps.
"""

# Azure diagnostic-setting category names for Entra ID, paired with the
# normalized form BlobLogProcessor derives from the ``insights-logs-<cat>``
# container name (lowercase, hyphens/underscores stripped). The normalized
# value is also the S247 log-type key (``S247_<normalized>``).
#
# AuditLogs is the most broadly available and already exists server-side; the
# sign-in family requires server-side log-type definitions to be added.
ENTRA_LOG_CATEGORIES = [
    {"category": "AuditLogs", "normalized": "auditlogs"},
    {"category": "SignInLogs", "normalized": "signinlogs"},
    {"category": "NonInteractiveUserSignInLogs", "normalized": "noninteractiveusersigninlogs"},
    {"category": "ServicePrincipalSignInLogs", "normalized": "serviceprincipalsigninlogs"},
    {"category": "ManagedIdentitySignInLogs", "normalized": "managedidentitysigninlogs"},
    {"category": "ProvisioningLogs", "normalized": "provisioninglogs"},
    {"category": "ADFSSignInLogs", "normalized": "adfssigninlogs"},
    {"category": "RiskyUsers", "normalized": "riskyusers"},
    {"category": "UserRiskEvents", "normalized": "userriskevents"},
]


def get_entra_categories():
    """Return the list of Azure diagnostic-setting category names to enable."""
    return [c["category"] for c in ENTRA_LOG_CATEGORIES]


def get_entra_normalized_categories():
    """Return the normalized (S247 log-type key) forms of the Entra categories."""
    return [c["normalized"] for c in ENTRA_LOG_CATEGORIES]
