# Collecting Entra ID (Azure AD) Logs

Entra ID logs — sign-ins, audit events, provisioning, risk — are **tenant-scoped**.
They are configured through the `microsoft.aadiam/diagnosticSettings` provider,
which behaves differently from every other source this collector handles.

## Why there's a manual step

Azure **does not allow a service principal or managed identity** to create an
Entra ID diagnostic setting. A call to `microsoft.aadiam/diagnosticSettings/write`
returns **HTTP 403** for app-only tokens — even when the identity has Global
Administrator. The API accepts **delegated (signed-in user) tokens only**. This
is a documented Azure platform limitation (the Terraform `azurerm` provider notes
the same for `azurerm_monitor_aad_diagnostic_setting`).

Because of that, this collector's managed identity **cannot** turn Entra logging
on for you. A tenant admin must create the diagnostic setting **once, manually**,
using their own login. After that one-time step, Entra logs flow through the same
pipeline as everything else — no ongoing manual work.

## What the collector does vs. what you do

| Step | Who |
|------|-----|
| Create the Site24x7 log types for Entra categories | **Collector** (per-category toggle on the Entra tab) |
| Expose a target storage account to send Entra logs to | **Collector** (shown in the dashboard) |
| Create the Entra ID diagnostic setting pointed at that account | **You** (tenant admin, one-time, manual) |
| Poll the storage account and forward logs to Site24x7 AppLogs | **Collector** (automatic) |

## Step 1 — Provision the log types (dashboard)

Open the dashboard's **Entra ID** tab and toggle on each log type you want to
collect. Toggling one on **creates that log type in Site24x7 immediately** and
the row shows the result:

- **✓ Created in Site24x7** — ready to receive.
- **⚠ Create failed** — usually means the log type isn't defined in Site24x7
  yet (the sign-in family is added over time). Toggle it on again once it exists.

The tab also shows the **target storage account** for Step 3 — a dedicated,
non-regional storage account (tagged `diag-logs-tenant`) provisioned
automatically and left untouched by region reconciliation, so the target never
changes. If no scan has run yet, it says so — run a scan first so the account is
created.

> This is our side only. It does not, and cannot, enable anything in Azure —
> that's Step 3.

## Step 2 — Create the Entra diagnostic setting (tenant admin)

You need the **Security Administrator** or **Global Administrator** role in Entra ID.

Copy the exact storage account resource ID from the dashboard's **Entra ID** tab
first, then use any one of the following.

### Portal

1. **Entra ID → Monitoring & health → Diagnostic settings → + Add diagnostic setting**
2. Name it e.g. `s247-entra-logs`.
3. Under **Logs**, tick the categories you want (see below).
4. Under **Destination details**, select **Archive to a storage account**, choose
   the subscription, and pick the storage account matching the ID from the dashboard.
5. **Save.**

### Azure CLI

```bash
# STORAGE_ID = the value copied from the dashboard's Entra ID tab
az monitor diagnostic-settings create \
  --name s247-entra-logs \
  --resource /providers/microsoft.aadiam \
  --storage-account "$STORAGE_ID" \
  --logs '[
    {"category":"AuditLogs","enabled":true},
    {"category":"SignInLogs","enabled":true},
    {"category":"NonInteractiveUserSignInLogs","enabled":true},
    {"category":"ServicePrincipalSignInLogs","enabled":true},
    {"category":"ManagedIdentitySignInLogs","enabled":true},
    {"category":"ProvisioningLogs","enabled":true},
    {"category":"ADFSSignInLogs","enabled":true},
    {"category":"RiskyUsers","enabled":true},
    {"category":"UserRiskEvents","enabled":true}
  ]'
```

Run this while **logged in as a user** (`az login`), not as the collector's
identity — a service principal login will get 403.

### PowerShell

```powershell
Set-AzDiagnosticSetting `
  -Name "s247-entra-logs" `
  -ResourceId "/providers/microsoft.aadiam" `
  -StorageAccountId $StorageId `
  -Category AuditLogs,SignInLogs,NonInteractiveUserSignInLogs,ServicePrincipalSignInLogs,ManagedIdentitySignInLogs,ProvisioningLogs,ADFSSignInLogs,RiskyUsers,UserRiskEvents `
  -Enabled $true
```

## Categories

Toggle any of these on from the Entra tab to create their Site24x7 log type:

| Azure category | Site24x7 log type | Availability |
|----------------|-------------------|--------------|
| `AuditLogs` | `auditlogs` | All tenants |
| `SignInLogs` | `signinlogs` | Entra ID P1/P2 |
| `NonInteractiveUserSignInLogs` | `noninteractiveusersigninlogs` | P1/P2 |
| `ServicePrincipalSignInLogs` | `serviceprincipalsigninlogs` | P1/P2 |
| `ManagedIdentitySignInLogs` | `managedidentitysigninlogs` | P1/P2 |
| `ProvisioningLogs` | `provisioninglogs` | With provisioning |
| `ADFSSignInLogs` | `adfssigninlogs` | With AD FS |
| `RiskyUsers` | `riskyusers` | P2 |
| `UserRiskEvents` | `userriskevents` | P2 |

Categories not licensed in your tenant simply produce no logs — enabling them is harmless.

### Turning individual categories off

You can disable any of these from the dashboard's **Log Type Filters** page. For
Entra (and other sources whose diagnostic setting we don't manage), the toggle
takes effect at the **processing** stage: the logs still land in the storage
account, but the collector stops forwarding the disabled category to Site24x7.
To also stop them being *written*, remove the category from the Entra diagnostic
setting in the portal.

## Verify

Within ~5–15 minutes of saving the diagnostic setting, containers named
`insights-logs-auditlogs`, `insights-logs-signinlogs`, … appear in the target
storage account, and the collector begins forwarding. Check the dashboard's
**Debug** tab (processing runs) and your Site24x7 AppLogs for the new log types.

## Troubleshooting

- **403 creating the setting** — you're authenticating as a service principal /
  managed identity, or you lack Security Administrator. Use a user login with the
  right role.
- **Dashboard shows no target storage account** — the dedicated tenant storage
  account hasn't been created yet. Run a scan (or wait for the scheduled one) —
  it's provisioned automatically.
- **Logs land but aren't forwarded** — the Site24x7 log type may not exist yet
  server-side (the sign-in family requires server-side log-type definitions).
  `auditlogs` works out of the box.
- **Notes on `log_source`** — Entra records carry a tenant-style `resourceId`, so
  the `log_source` field for these logs will not be an Azure resource group like it
  is for resource logs.
