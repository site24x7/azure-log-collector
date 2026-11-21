# Site24x7 Azure Diagnostic Logs Collection — Architecture Document

**Version:** 1.1.0
**Last Updated:** November 2026

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Architecture Overview](#2-system-architecture-overview)
3. [Infrastructure Components](#3-infrastructure-components)
4. [Multi-Region Architecture](#4-multi-region-architecture)
5. [Core Workflows](#5-core-workflows)
6. [Shared Modules](#6-shared-modules)
7. [Function Inventory](#7-function-inventory)
8. [API Reference](#8-api-reference)
9. [Data Storage Architecture](#9-data-storage-architecture)
10. [Site24x7 Integration](#10-site24x7-integration)
11. [Security Architecture](#11-security-architecture)
12. [Resilience & Error Handling](#12-resilience--error-handling)
13. [Configuration Reference](#13-configuration-reference)
14. [Deployment Guide](#14-deployment-guide)

---

## 1. Executive Summary

This system automates the collection and forwarding of Azure diagnostic logs to Site24x7 AppLogs. It:

- **Discovers** all Azure resources across subscriptions that support diagnostic logging
- **Provisions** per-region Storage Accounts for log collection
- **Configures** Azure Diagnostic Settings to stream logs to regional storage
- **Auto-creates** Site24x7 log types for each Azure log category
- **Polls** Storage Account blobs every 2 minutes, parses logs, and uploads to Site24x7
- **Provides** a web dashboard and REST API for management and monitoring

**Cost:** ~$0.02/GB/month (Storage Account polling vs $11/region/month for Event Hub)

---

## 2. System Architecture Overview

```
╔══════════════════════════════════════════════════════════════════════════════════╗
║                    AZURE DIAGNOSTIC LOGS → SITE24X7 PIPELINE                    ║
╚══════════════════════════════════════════════════════════════════════════════════╝

┌──────────────────────────────────────────────────────────────────────────────────┐
│  AZURE SUBSCRIPTIONS (1..N)                                                      │
│                                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │  VM (eastus)  │  │  SQL (westus) │  │  AKS (eastus)│  │  KV (westeu) │  ...   │
│  └──────┬────────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘         │
│         │ Diagnostic        │                  │                 │                 │
│         │ Settings           │                  │                 │                 │
│         ▼ (s247-diag-logs-*) ▼                  ▼                 ▼                 │
│  ┌────────────────────┐  ┌────────────────────┐  ┌────────────────────┐           │
│  │ Storage: eastus     │  │ Storage: westus     │  │ Storage: westeu    │          │
│  │ s247diageastus...   │  │ s247diagwestus...   │  │ s247diagwesteu...  │          │
│  │ tag: managed-by:    │  │ tag: managed-by:    │  │ tag: managed-by:   │          │
│  │   s247-diag-logs    │  │   s247-diag-logs    │  │   s247-diag-logs   │          │
│  │                      │  │                      │  │                    │          │
│  │ ├─insights-logs-    │  │ ├─insights-logs-    │  │ ├─insights-logs-   │          │
│  │ │  AuditEvent/      │  │ │  SQLSecurityAudit/ │  │ │  AuditEvent/     │          │
│  │ ├─insights-logs-    │  │ ├─insights-logs-    │  │ └─insights-logs-   │          │
│  │ │  SignInLogs/      │  │ │  DatabaseWaitStat/ │  │    AppServiceHTTP/ │          │
│  │ └─...               │  │ └─...               │  │                    │          │
│  └────────┬─────────────┘  └────────┬────────────┘  └────────┬──────────┘          │
│           │                         │                         │                    │
└───────────┼─────────────────────────┼─────────────────────────┼────────────────────┘
            │           Poll every 2 min                        │
            ▼                         ▼                         ▼
╔══════════════════════════════════════════════════════════════════════════════════╗
║  FUNCTION APP: s247-diag-func-XXXXXX  (RG: s247-diag-logs-rg)                  ║
║  Auth: Managed Identity (Azure APIs) + Function Keys (HTTP endpoints)           ║
║                                                                                  ║
║  ┌─── TIMER FUNCTIONS ───────────────────────────────────────────────────────┐  ║
║  │                                                                            │  ║
║  │  ┌────────────────────────────┐    ┌─────────────────────────────────┐    │  ║
║  │  │  DiagSettingsManager       │    │  BlobLogProcessor               │    │  ║
║  │  │  (every 6h / on-demand)    │    │  (every 2 min)                  │    │  ║
║  │  │                            │    │                                 │    │  ║
║  │  │  1. List all resources     │    │  1. Find regional storage accs  │    │  ║
║  │  │  2. Reconcile regions      │    │     (by tag: s247-diag-logs)    │    │  ║
║  │  │     (add missing storage   │    │  2. Scan insights-logs-* ctrs   │    │  ║
║  │  │      accts; idle unused)   │    │  3. Load S247_{cat} config      │    │  ║
║  │  │  3. Fetch supported types  │    │     from blob store             │    │  ║
║  │  │     from Site24x7 API      │    │  4. Parse JSON blobs            │    │  ║
║  │  │  4. Batch-create log types │    │  5. Apply masking/hashing/      │    │  ║
║  │  │  5. Store sourceConfig in  │    │     filtering/derived fields    │    │  ║
║  │  │     blob config store      │    │  6. Gzip + POST to Site24x7    │    │  ║
║  │  │  6. Create diag settings   │    │  7. Retain processed blobs     │    │  ║
║  │  │     → regional storage     │    │  8. Update checkpoint           │    │  ║
║  │  └────────────┬───────────────┘    └──────────────┬──────────────────┘    │  ║
║  │               │                                   │                       │  ║
║  │  ┌────────────────────────────┐                   │                       │  ║
║  │  │  AutoUpdater               │                   │                       │  ║
║  │  │  (daily 3 AM UTC)          │                   │                       │  ║
║  │  │  Checks remote version.json│                   │                       │  ║
║  │  │  Installs only if enabled  │                   │                       │  ║
║  │  └────────────────────────────┘                   │                       │  ║
║  └───────────────────────────────────────────────────┼───────────────────────┘  ║
║                                                      │                          ║
║  ┌─── BLOB CONFIG STORE (AzureWebJobsStorage) ──────┼───────────────────────┐  ║
║  │  config/                                          │                       │  ║
║  │  ├── logtype-configs/S247_AuditEvent.json   ◄─────┤                       │  ║
║  │  ├── logtype-configs/S247_SignInLogs.json          │                       │  ║
║  │  ├── logtype-configs/S247_...json                  │                       │  ║
║  │  ├── azure-log-types.json  (supported types cache)│                       │  ║
║  │  ├── disabled-logtypes.json                        │                       │  ║
║  │  ├── configured-resources.json                     │                       │  ║
║  │  └── ignore-list.json                              │                       │  ║
║  │  s247-checkpoints/                                 │                       │  ║
║  │  └── blob-processor-checkpoint.json  ◄─────────────┘                       │  ║
║  └────────────────────────────────────────────────────────────────────────────┘  ║
║                                                                                  ║
║  ┌─── HTTP ENDPOINTS (protected by Function Keys) ───────────────────────────┐  ║
║  │  GET  /api/dashboard             — Read-only UI                            │  ║
║  │  GET  /api/status                — System overview + resource counts       │  ║
║  │  GET  /api/health                — Liveness probe + dependency checks      │  ║
║  │  GET|POST /api/debug             — Debug summary / redacted bundle         │  ║
║  │  POST /api/scan                  — Queue scan for ScanWorker               │  ║
║  │  GET  /api/configured-resources  — Resources with the collector's setting  │  ║
║  │  GET  /api/ignore-list           — Resource filters (includes + tags)      │  ║
║  │  PUT  /api/ignore-list           — Update filters; queue a scan            │  ║
║  │  GET  /api/disabled-logtypes     — Disabled log categories                 │  ║
║  │  POST /api/disabled-logtypes     — Disable/enable + cleanup                │  ║
║  │  POST /api/entra-logtypes        — Enable/disable Entra ID log types       │  ║
║  │  PUT  /api/processing            — Toggle BlobLogProcessor on/off          │  ║
║  │  PUT  /api/collection            — Log Collection on/off (queued)          │  ║
║  │  GET|PUT /api/settings           — Auto-scan / pipeline monitoring         │  ║
║  │  POST /api/remove-diagnostic-settings — Remove all diagnostic settings     │  ║
║  │  POST /api/delete-collector      — Delete the collector (queued)           │  ║
║  │  GET  /api/general-logtype       — General catch-all log type config       │  ║
║  │  PUT  /api/general-logtype       — Update general log type                 │  ║
║  │  GET  /api/check-update          — Check for available updates             │  ║
║  │  POST /api/check-update?apply=1  — Apply update if allowed                 │  ║
║  └────────────────────────────────────────────────────────────────────────────┘  ║
╚══════════════════════════════════════════════════════════════════════════════════╝
            │                                   │
            │  GET /applog/azure/               │  POST https://{uploadDomain}
            │    logtype_supported              │    /upload
            │  GET /applog/logtype              │  (gzip, X-DeviceKey,
            │    ?logType={category}            │   X-LogType, X-StreamMode)
            ▼                                   ▼
╔══════════════════════════════════════════════════════════════════════════════════╗
║  SITE24X7 SERVER (applogs)                                                       ║
║                                                                                  ║
║  ┌─── AppLogs API (deviceKey auth) ───────────────────────────────────────────┐  ║
║  │  GET  /applog/azure/logtype_supported → supported Azure log types         │  ║
║  │  GET  /applog/logtype    → log type definition for one category           │  ║
║  └────────────────────────────────────────────────────────────────────────────┘  ║
║                                                                                  ║
║  ┌─── Upload Endpoint ────────────────────────────────────────────────────────┐  ║
║  │  POST /upload — Receives gzipped log events, indexes into AppLogs         │  ║
║  └────────────────────────────────────────────────────────────────────────────┘  ║
║                                                                                  ║
║  AppLogs Console: Search, alerts, dashboards, saved searches                    ║
╚══════════════════════════════════════════════════════════════════════════════════╝
```

---

## 3. Infrastructure Components

### 3.1 Azure Resources

| Resource | Name | Purpose |
|----------|------|---------|
| **Resource Group** | `resourceGroupName` parameter; default `s247-diag-logs-rg`, or `s247-diag-logs-{suffix}-rg` when a deployment suffix is given | Contains all pipeline resources |
| **Function App** | `s247-diag-func-{suffix}` | Hosts all Azure Functions (Flex Consumption, Python 3.13) |
| **Hosting Plan** | `s247-diag-func-{suffix}-plan` | Flex Consumption plan (FC1) |
| **Application Insights** | `s247-diag-func-{suffix}-ai` | Function App telemetry |
| **Log Analytics Workspace** | `s247-diag-func-{suffix}-logs` | Workspace behind Application Insights |
| **Storage Account** (host) | `s247diag{suffix}` | Function App storage (AzureWebJobsStorage) + app package container + config blobs + checkpoints |
| **Storage Account** (seed) | `s247diag{region}{suffix}` (cut to 24 characters), or `s247dr{suffix}` when the suffix is longer than 6 characters | Regional account for the deployment region, created by the template |
| **Storage Accounts** (regional) | See [4.3](#43-storage-account-provisioning-details) | Per-region log collection (auto-provisioned) |
| **Storage Account** (Entra ID) | `s247diagtenant{suffix}` (cut to 24 characters), or `s247dt{suffix}` when the suffix is longer than 6 characters | Tenant-scoped Entra ID logs; created while an Entra ID log type is enabled |

`{suffix}` is the deployment suffix (1–13 lowercase letters or digits), or 6 characters derived from the subscription when none is given.

### 3.2 RBAC Role Assignments (Managed Identity)

| Role | Scope | Purpose |
|------|-------|---------|
| **Reader** | Each monitored subscription | Discover all resources |
| **Monitoring Contributor** | Each monitored subscription | Create/delete diagnostic settings |
| **Contributor** | The collector's resource group (default `s247-diag-logs-rg`) | Manage storage accounts, app settings |
| **Locks Contributor** | The collector's resource group | Remove the collector's own lock when Delete Collector deletes the group |

### 3.3 Dependencies

```
azure-functions          # Azure Functions runtime
azure-identity           # DefaultAzureCredential (Managed Identity)
azure-mgmt-resource      # Resource discovery
azure-mgmt-monitor       # Diagnostic settings + categories
azure-mgmt-storage       # Storage account lifecycle
azure-mgmt-web           # Function App settings management
azure-storage-blob       # Blob read/write/delete
requests                 # HTTP calls to Site24x7
```

---

## 4. Multi-Region Architecture

### 4.1 Why Per-Region Storage?

Azure Diagnostic Settings require the destination Storage Account to be **in the same region** as the resource. The system automatically provisions one Storage Account per active region. Global resources (Front Door, Traffic Manager, DNS, ...) have no region and may send to any region: they use the account in the deployment's region (`DIAG_SEED_REGION`).

### 4.2 Regional Storage Lifecycle

```
┌───────────────────────────────────────────────────────────────────┐
│                    REGION RECONCILIATION FLOW                      │
│                                                                   │
│  ┌─────────────────┐    ┌──────────────────┐                     │
│  │ Active Resources │    │ Provisioned       │                     │
│  │ (from scan)      │    │ Storage Accounts  │                     │
│  │                  │    │ (by tag lookup)   │                     │
│  │ eastus     ──────┼────┼── s247diageastus  │  ✓ Already exists  │
│  │ westus     ──────┼────┼── (missing)       │  → Provision new   │
│  │ westeurope ──────┼────┼── s247diagwesteu  │  ✓ Already exists  │
│  │ (none)     ──────┼────┼── s247diagcentus  │  → Keep idle       │
│  └─────────────────┘    └──────────────────┘                     │
│                                                                   │
│  Reconciliation:                                                  │
│  • to_add    = active_regions - provisioned_regions               │
│  • to_idle   = provisioned_regions - active_regions (1-day rule)  │
└───────────────────────────────────────────────────────────────────┘
```

### 4.3 Storage Account Provisioning Details

| Property | Value |
|----------|-------|
| **Naming** | `s247diag{region}{suffix}` when the suffix has at most 6 characters and the name fits in 24; otherwise `s247diag` + 16 hex characters of a hash of suffix and region (`s247dr{suffix}` for the deployment region). `s247diag` + 16 random hex characters when the name is taken elsewhere in Azure. Names from earlier versions (cut at 24 characters) are still recognised. Accounts are identified by their tags, not by name. |
| **SKU** | `Standard_LRS` |
| **Kind** | `StorageV2` |
| **TLS** | Minimum TLS 1.2 |
| **Public Access** | Disabled |
| **Base Container** | `insights-logs` (validation container) |
| **Retention** | Lifecycle rule `s247-diag-logs-retention`: `BLOB_RETENTION_DAYS` (1 by default); 1 day while the region is not collected. Never deleted: a deleted name can be taken by anyone in Azure. |
| **Tags** | `managed-by: s247-diag-logs`, `purpose: diag-logs-regional`, `region: {region}`, `s247-collector: {Function App name}` (collectors sharing a group never use each other's accounts) |

### 4.4 Multi-Region Data Flow

```
 Subscription 1                          Subscription 2
 ┌─────────────────────────────┐         ┌─────────────────────────────┐
 │  VM-1 (eastus)              │         │  SQL-1 (eastus)             │
 │  VM-2 (westus)              │         │  AKS-1 (westeurope)        │
 │  KeyVault-1 (eastus)        │         │  AppService-1 (westus)     │
 └──────┬──────────┬───────────┘         └──────┬──────────┬──────────┘
        │          │                             │          │
        ▼          ▼                             ▼          ▼
 ┌────────────┐ ┌────────────┐          ┌────────────┐ ┌────────────┐
 │  Storage    │ │  Storage    │          │  Storage    │ │  Storage    │
 │  eastus     │ │  westus     │          │  eastus     │ │  westeurope │
 │             │ │             │          │  (shared)   │ │             │
 │ insights-   │ │ insights-   │          │             │ │ insights-   │
 │ logs-Audit/ │ │ logs-Audit/ │          │             │ │ logs-kube/  │
 │ logs-SignIn/│ │ logs-HTTP/  │          │             │ │             │
 └──────┬──────┘ └──────┬──────┘          └──────┬──────┘ └──────┬──────┘
        │               │                       │               │
        └───────────────┼───────────────────────┼───────────────┘
                        │                       │
                        ▼                       ▼
              ┌─────────────────────────────────────────┐
              │       BlobLogProcessor (every 2 min)     │
              │  Discovers all regional accounts by tag  │
              │  Polls insights-logs-* containers        │
              │  Uploads parsed logs to Site24x7         │
              │  Keeps blobs (lifecycle rule expires)    │
              └─────────────────────────────────────────┘
```

---

## 5. Core Workflows

### 5.1 Workflow 1: Resource Discovery & Configuration (DiagSettingsManager)

**Trigger:** Timer (configurable via `%TIMER_SCHEDULE%`) or on-demand via `POST /api/scan`

A scan is also queued when a worker starts after an installation (no scan yet), an update (the last scan ran on another version) or a restart (the last scan finished `MIN_SCAN_INTERVAL_MINUTES`, default 30, or more ago). The first `BlobLogProcessor` run of each worker checks this (`shared/startup_scan.py`); `ScanWorker` drops a request that a scan started after it already covers.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STEP 1: LOAD CONFIGURATION                                            │
│  ├─ Read SUBSCRIPTION_IDS, RESOURCE_GROUP_NAME, DIAG_STORAGE_SUFFIX    │
│  ├─ Clear in-memory config cache                                        │
│  └─ Load ignore list + configured resources from blob                   │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 2: FETCH SUPPORTED LOG TYPES                                      │
│  ├─ Load cached azure-log-types.json as outage fallback                 │
│  ├─ Each scan: GET /applog/azure/logtype_supported                     │
│  └─ Build normalized lookup map (remove hyphens, lowercase)             │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 3: DISCOVER RESOURCES                                             │
│  ├─ For each subscription:                                               │
│  │   └─ ResourceManagementClient.resources.list()                       │
│  ├─ Filter: supports_diagnostic_logs() == True                          │
│  └─ Filter: is_ignored() == False                                       │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 4: RECONCILE REGIONS                                              │
│  ├─ Extract active regions from resources                                │
│  ├─ List provisioned storage accounts (by tag)                          │
│  ├─ to_add = active - provisioned → provision_storage_account()         │
│  └─ to_idle = provisioned - active → set_retention(1 day), never delete │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 5: CATEGORY COLLECTION                                            │
│  ├─ For each resource:                                                   │
│  │   ├─ Skip unchanged tracking; verify Azure when reconciliation is due │
│  │   ├─ Get diagnostic categories for resource                          │
│  │   ├─ Filter out disabled categories                                   │
│  │   ├─ Map resource → categories + storage account                     │
│  │   └─ Collect unconfigured categories for batch creation              │
│  └─ Build resource_category_map and categories_to_create list           │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 6: BATCH LOG TYPE CREATION                                        │
│  ├─ GET /applog/logtype per category (parallel + fallback)              │
│  ├─ Client builds: [{category, sourceConfig (base64)}]                 │
│  ├─ Decode and save each sourceConfig to blob:                          │
│  │   config/logtype-configs/S247_{category}.json                        │
│  └─ Track logtypes_created count                                        │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 7: CONFIGURE DIAGNOSTIC SETTINGS                                  │
│  ├─ For each resource in resource_category_map:                         │
│  │   ├─ Verify at least one category has config (specific or general)   │
│  │   ├─ Build storage_account_id from region + suffix                   │
│  │   ├─ MonitorManagementClient.diagnostic_settings.create_or_update()  │
│  │   │   setting_name: collector_setting_name()                         │
│  │   │   categories: selected effective categories                      │
│  │   │   storage_account_id: regional storage account                   │
│  │   └─ Mark resource as configured in blob tracking                    │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 8: FINALIZE                                                       │
│  ├─ Save last_scan_time in config/scan-state.json                       │
│  └─ Return summary with all stats                                       │
└─────────────────────────────────────────────────────────────────────────┘
```

**Output Statistics:**
```json
{
  "scan_time": "2026-03-11T05:00:00Z",
  "total_resources": 150,
  "active_resources": 120,
  "ignored_resources": 30,
  "already_configured": 100,
  "newly_configured": 20,
  "logtypes_created": 5,
  "specific_logtypes": 18,
  "general_logtypes": 2,
  "skipped": 0,
  "errors": 0,
  "regions": {
    "active": ["eastus", "westus", "westeurope"],
    "added": [{"region": "westus", "storage_account": "s247diag<westus><suffix>"}],
    "idle": ["centralus"]
  }
}
```

**Settings repair:** a diagnostic setting deleted or changed in Azure is written again. Step 5 skips unchanged tracked resources, but every 24 hours, and on every `POST /api/scan`, the scan first checks each tracked setting in Azure (in parallel); a missing or changed one is configured again in step 7.

---

### 5.2 Workflow 2: Log Processing & Upload (BlobLogProcessor)

**Trigger:** Timer — every 2 minutes (`0 */2 * * * *`)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STEP 1: PRE-FLIGHT CHECKS                                             │
│  ├─ Sync registration; shared processing flag → skip if off            │
│  ├─ Load all logtype configs from blob: config/logtype-configs/*.json  │
│  ├─ Load general config from S247_GENERAL_LOGTYPE env var              │
│  └─ If no configs found and general not enabled → exit early           │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 2: DISCOVER REGIONAL STORAGE ACCOUNTS                            │
│  ├─ List storage accounts in RG                                         │
│  ├─ Filter by tags: managed-by=s247-diag-logs, purpose=diag-logs-reg  │
│  └─ Load checkpoint: s247-checkpoints/blob-processor-checkpoint.json   │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 3: PROCESS EACH REGIONAL ACCOUNT                                  │
│  ├─ Get storage account keys                                            │
│  ├─ Load per-blob byte/block position and skip_records                  │
│  │                                                                      │
│  ├─ FOR EACH CONTAINER (matching insights-logs-*):                      │
│  │   ├─ Extract category from name: insights-logs-{cat} → {cat}        │
│  │   ├─ Normalize: remove hyphens → S247_{normalized}                  │
│  │   ├─ Look up sourceConfig:                                           │
│  │   │   ├─ Specific: config/logtype-configs/S247_{cat}.json           │
│  │   │   ├─ Fallback: general config (if enabled)                      │
│  │   │   └─ Skip container if neither found                            │
│  │   │                                                                  │
│  │   ├─ FOR EACH BLOB (.json files only):                              │
│  │   │   ├─ Resume per-blob byte offset / block position (checkpoint)  │
│  │   │   ├─ Read bounded blob ranges (up to 50 MB)                      │
│  │   │   ├─ Parse JSON → extract "records" array                       │
│  │   │   ├─ POST to Site24x7 via client.post_logs()                    │
│  │   │   │   ├─ Decode sourceConfig (base64 → JSON)                    │
│  │   │   │   ├─ Parse fields via jsonPath rules                        │
│  │   │   │   ├─ Apply filters (include/exclude by field patterns)      │
│  │   │   │   ├─ Apply masking (regex → replacement string)             │
│  │   │   │   ├─ Apply hashing (regex → SHA256 digest)                  │
│  │   │   │   ├─ Apply derived fields (regex → named groups)            │
│  │   │   │   ├─ Add _zl_timestamp + s247agentuid (app; RG fallback)   │
│  │   │   │   ├─ Gzip compress JSON array                               │
│  │   │   │   └─ POST to https://{uploadDomain}/upload                  │
│  │   │   ├─ If handled: advance per-blob checkpoint                    │
│  │   │   └─ Track handled byte/block position for checkpoint           │
│  │   │                                                                  │
│  │   └─ KEEP blobs until storage lifecycle expires them                │
│  │                                                                      │
│  └─ Save per-blob progress (at-least-once delivery)                    │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 4: SAVE CHECKPOINTS                                               │
│  └─ Write to s247-checkpoints/blob-processor-checkpoint.json           │
└─────────────────────────────────────────────────────────────────────────┘
```

**Output Statistics:**
```json
{
  "processed": 1250,
  "uploaded": 1200,
  "general": 50,
  "dropped": 0,
  "blobs_advanced": 45
}
```

---

### 5.3 Workflow 3: Disable Log Type

**Trigger:** `POST /api/disabled-logtypes` with `{"action": "disable", "category": "AuditEvent"}`

```
┌─────────────────────────────────────────────────────────────────────────┐
│  1. Add "AuditEvent" to disabled-logtypes.json                         │
│  2. Delete config/logtype-configs/S247_AuditEvent.json                 │
│  3. For each resource in configured-resources.json:                    │
│     ├─ If "AuditEvent" is its only category:                           │
│     │   ├─ DELETE this collector's setting (s247-diag-logs-<suffix>)   │
│     │   └─ Remove resource from configured tracking                    │
│     └─ Else: setting and tracking stay unchanged (the processor no     │
│        longer forwards the category; re-enabling needs no rediscovery) │
│  4. Return: disabled list + count of removed settings                  │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.4 Workflow 4: Enable Log Type

**Trigger:** `POST /api/disabled-logtypes` with `{"action": "enable", "category": "AuditEvent"}`

```
┌─────────────────────────────────────────────────────────────────────────┐
│  1. Remove "AuditEvent" from disabled-logtypes.json                    │
│  2. Next DiagSettingsManager scan will:                                 │
│     ├─ Re-create log type via Site24x7 API                             │
│     ├─ Store new sourceConfig in blob                                  │
│     └─ Re-configure diagnostic settings for affected resources         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.5 Workflow 5: Exclude Resources

**Trigger:** `PUT /api/ignore-list`

```
┌─────────────────────────────────────────────────────────────────────────┐
│  1. Save new filters to config/ignore-list.json                        │
│  2. Load currently configured resources                                │
│  3. For each configured resource (subscription rule only — the ID      │
│     alone decides it):                                                 │
│     ├─ If its subscription is no longer included:                      │
│     │   ├─ DELETE this collector's setting (s247-diag-logs-<suffix>)   │
│     │   └─ Remove from configured-resources.json                       │
│  4. Location / resource type / tag changes (and a wider subscription   │
│     list, or a scan already running): queue a scan, which reads the    │
│     resource details and applies them                                  │
│  5. Return: filters, settings removed / not removed, scan_queued       │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.6 Workflow 6: Auto-Update

**Trigger:** Timer — daily at 3:00 AM UTC (`0 0 3 * * *`)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  1. SKIP_AUTO_UPDATE=true? → exit (action=disabled)                    │
│  2. Read UPDATE_CHECK_URL (skip if unset)                              │
│  3. Pin freezes; older pin → pinned_older (never downgrade)            │
│  4. Fetch remote release (owner/repo → /releases/latest on stable      │
│     channel, /releases on prerelease channel)                          │
│  5. UPDATE_CHANNEL=stable? → skip if version is pre-release            │
│     (by -alpha/-beta suffix OR GitHub prerelease flag)                 │
│  6. Compare semver: local VERSION vs remote                            │
│  7. Release younger than MIN_RELEASE_AGE_MINUTES? → defer              │
│  8. If install enabled: validate zip and package imports:              │
│     ├─ zipfile integrity                                               │
│     ├─ ast.parse app .py files (skip .python_packages)                 │
│     ├─ json.loads every .json file                                     │
│     └─ required files present                                          │
│  9. Validation failed? → refuse to deploy (action=deploy_failed)       │
│ 10. Stage verified copy, PUT ARM onedeploy (Managed Identity)            │
│ 11. Function App restarts with new version                             │
│ 12. New worker records confirmed_at on its first processing run        │
│     No automatic rollback                                              │
│ 13. Every run writes auto_update_run audit event                       │
└─────────────────────────────────────────────────────────────────────────┘
```

**Failure modes and mitigations:**

| Failure | Outcome |
|---|---|
| Downloaded zip has a syntax error | Refused at step 9 (action=deploy_failed); current version stays |
| Release tagged as pre-release on GitHub | Skipped on stable channel (step 5) |
| Bad release published | 60-min grace window (step 7) allows deletion before propagation |
| Rollback needed | `SKIP_AUTO_UPDATE=true`, then reinstall the previous release's package (README, Rollback). `PINNED_VERSION` only freezes updates |
| Emergency halt | Set `SKIP_AUTO_UPDATE=true` — no redeploy required |
| New version fails to start | No automatic rollback; inspect startup errors and reinstall a known-good package. A pin cannot recover it |

### 5.7 Workflow 7: Log Collection On/Off (LogCollection)

**Trigger:** `PUT /api/collection` with `{"enabled": false}` or `{"enabled": true}` (`shared/collection.py`). Answers HTTP 202; `/api/status` (`collection`) shows the progress. It changes no app setting, so the Function App does not restart.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  OFF:                                                                   │
│  1. Save the switch: forwarding stops at once                           │
│  2. Queue a job on scan-requests; ScanWorker runs it under scan lock:   │
│     ├─ Remove this collector's diagnostic settings from every resource  │
│     ├─ Delete every container in its storage accounts, with the         │
│     │  processing checkpoints that pointed into them                    │
│     └─ Accounts stay, empty (the resource group lock forbids deleting)  │
│  3. Late writes from Azure Monitor are swept every 30 min for 24 h      │
│  4. While off, no scan sets anything up again                           │
│                                                                         │
│  ON:                                                                    │
│  1. Forwarding resumes                                                  │
│  2. A scan is queued, which sets everything up again                    │
└─────────────────────────────────────────────────────────────────────────┘
```

Switching off again while already off queues the removal again, so a removal that left something behind can be retried.

### 5.8 Workflow 8: Delete Collector (DeleteCollector)

**Trigger:** `POST /api/delete-collector` with `{"confirm": "<this Function App's name>"}` (`shared/teardown.py`). A wrong or missing name answers HTTP 400 and deletes nothing. Otherwise answers HTTP 202 and queues the job; `/api/status` (`collection.delete`) shows the progress until the app is gone.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  ScanWorker, under the scan lock:                                       │
│  1. Switch log collection off; remove every diagnostic setting of this  │
│     collector and empty its storage (as Workflow 7)                     │
│     └─ Anything left → stop (failed: settings_remain)                   │
│  2. The resource group must hold only this collector's resources        │
│     └─ Other resources → stop and report them (foreign_resources)       │
│  3. Remove the collector's own locks (s247-lock-*), which needs Locks   │
│     Contributor on the group                                            │
│     └─ A lock that cannot be removed, or any other lock → stop (locks)  │
│  4. Report to Site24x7, then start deleting the resource group (this    │
│     removes the Function App too)                                       │
└─────────────────────────────────────────────────────────────────────────┘
```

Left for an admin: the Reader and Monitoring Contributor grants on the monitored subscriptions, the subscription-level deployment record, and any Entra ID diagnostic setting a tenant admin created.

---

## 6. Shared Modules

### 6.1 region_manager.py — Regional Storage Lifecycle

Manages per-region Storage Account provisioning, retention, and reconciliation.

| Method | Purpose |
|--------|---------|
| `get_storage_name_for_region(region, suffix)` | Generate name: `s247diag{region}{suffix}`, or the hashed / `s247dr{suffix}` form when that does not fit (see 4.3; `shared/storage_names.py`) |
| `get_active_regions(resources)` | Extract unique regions from resource list |
| `get_provisioned_regions(resource_group)` | List accounts by tag: `managed-by: s247-diag-logs` |
| `provision_storage_account(rg, region, suffix)` | Create account + container + retention rule |
| `set_retention(rg, name, days)` | Set the retention rule, writing only on a change |
| `reconcile_regions(rg, active, provisioned, suffix)` | Add missing regions; idle unused ones (1-day rule); restore the normal rule for active ones |

### 6.2 azure_manager.py — Azure SDK Operations

Wraps all Azure Management SDK calls behind a clean interface.

| Method | Azure SDK Client | Purpose |
|--------|-----------------|---------|
| `get_all_resources(sub_ids)` | `ResourceManagementClient` | List all diagnostic-capable resources |
| `supports_diagnostic_logs(id, type)` | `MonitorManagementClient` | Check if resource type has log categories (cached) |
| `get_diagnostic_categories(id)` | `MonitorManagementClient` | List log category names for a resource |
| `get_diagnostic_setting(id)` | `MonitorManagementClient` | Check if this collector's setting (`collector_setting_name()`: `s247-diag-logs-<suffix>`) exists |
| `create_diagnostic_setting(id, sa_id, categories=None, setting_name=None)` | `MonitorManagementClient` | Enable selected categories; use `allLogs` only when categories is `None` |
| `delete_diagnostic_setting(id)` | `MonitorManagementClient` | Remove this collector's setting |
| `remove_all_diagnostic_settings(sub_ids)` | `MonitorManagementClient` | Bulk removal across all resources |
| `update_app_setting(key, value)` | `WebSiteManagementClient` | Update Function App configuration |

### 6.3 config_store.py — Blob-Based Configuration

All configuration stored in blobs for dynamic updates without redeployment.

| Function | Blob Path | Purpose |
|----------|-----------|---------|
| `get_supported_log_types()` | `config/azure-log-types.json` | Cached supported types from S247 |
| `get_logtype_config(cat)` | `config/logtype-configs/S247_{cat}.json` | Per-category sourceConfig |
| `get_all_logtype_configs()` | `config/logtype-configs/*.json` | All configs for BlobLogProcessor |
| `get_disabled_log_types()` | `config/disabled-logtypes.json` | Disabled category list |
| `get_configured_resources()` | `config/configured-resources.json` | Resource tracking map |
| `clear_cache()` | N/A | Reset in-memory cache per invocation |

**Caching Strategy:** In-memory cache with a 30-second TTL. `clear_cache()` called at start of every scan. Reduces blob reads during a single invocation cycle.

### 6.4 site24x7_client.py — Site24x7 API Client

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `get_supported_log_types()` | `GET /applog/azure/logtype_supported` | Fetch 50+ supported Azure log types |
| `create_log_types(categories)` | `GET /applog/logtype?logType=<category>` | Batch-create log types, returns sourceConfig |
| `post_logs(config_b64, events)` | `POST https://{domain}/upload` | Full log upload pipeline |

**Log Upload Pipeline (post_logs):**
```
Input: base64 sourceConfig + raw Azure log events
  │
  ├─ 1. Decode sourceConfig → jsonPath rules, masking, hashing, filters, derived
  ├─ 2. Parse events: extract fields via jsonPath mapping
  ├─ 3. Apply filters: include/exclude based on field pattern matching
  ├─ 4. Apply masking: regex match → replace with mask string
  ├─ 5. Apply hashing: regex match → replace with SHA256 hex digest
  ├─ 6. Apply derived fields: regex named groups → new fields
  ├─ 7. Add metadata: _zl_timestamp (ms epoch), s247agentuid (Function App name; resource-group fallback)
  ├─ 8. Gzip compress JSON array
  └─ 9. POST to https://{uploadDomain}/upload
         Headers: X-DeviceKey, X-LogType, X-StreamMode:1,
                  Log-Size, Content-Encoding:gzip,
                  User-Agent: AZURE-DiagLogs-Function
```

### 6.5 ignore_list.py — Resource Filtering

Subscriptions, regions and resource types are include lists (empty = all, including ones added later); the hosting subscription is always included and global resources pass the region list. Matching is case-insensitive. Tag names and values match case-insensitively (Azure tag names are case-insensitive); a `key=value` rule, including `key=` for an empty value, matches only resources that have the tag:

```
Resource → is_ignored()?
  ├─ Check: subscriptions set and subscription not in it (not the hosting one)?  → IGNORED
  ├─ Check: locations set and location (not global) not in it?  → IGNORED
  ├─ Check: resource_types set and type not in it?  → IGNORED
  ├─ Check: matches an exclude tag, or include tags set and matches none?  → IGNORED
  └─ Otherwise → NOT IGNORED
```

### 6.6 log_parser.py — Diagnostic Record Parsing

Parses Azure diagnostic log envelope format:
```json
{
  "records": [
    {
      "time": "2026-01-01T00:00:00Z",
      "resourceId": "/subscriptions/.../providers/.../resource",
      "category": "AuditEvent",
      "operationName": "VaultGet",
      "resultType": "Success",
      "level": "Information",
      "properties": { ... }
    }
  ]
}
```

### 6.7 updater.py — Self-Update Mechanism

Checks remote `version.json` and auto-deploys via ARM OneDeploy.

---

## 7. Function Inventory

### 7.1 Timer-Triggered Functions

| Function | Schedule | Purpose |
|----------|----------|---------|
| **DiagSettingsManager** | `%TIMER_SCHEDULE%` (configurable) | Resource discovery, region reconciliation, log type creation, diagnostic settings configuration |
| **BlobLogProcessor** | `0 */2 * * * *` (every 2 min) | Poll regional storage accounts, parse log blobs, upload to Site24x7 (blobs are kept; lifecycle rules expire them) |
| **AutoUpdater** | `0 0 3 * * *` (3 AM UTC daily) | Check for updates; install only when `AUTO_UPDATE_ENABLED=true` |
| **ScanWorker** | Queue `scan-requests` | Runs queued scans, Log Collection off jobs and Delete Collector jobs |

### 7.2 HTTP-Triggered Functions

| Function | Method | Route | Purpose |
|----------|--------|-------|---------|
| **Dashboard** | GET | `/api/dashboard` | Read-only web UI: status, filters, resources, platform logs, debug |
| **GetStatus** | GET | `/api/status` | System overview: resources, regions, configs, errors |
| **HealthCheck** | GET | `/api/health` | Liveness probe + dependency checks |
| **TriggerScan** | POST | `/api/scan` | Enqueue scan-requests for ScanWorker |
| **GetDebugInfo** | GET/POST | `/api/debug` | Debug summary or redacted debug bundle; POST `?clear=1` clears the event log |
| **GetConfiguredResources** | GET | `/api/configured-resources` | Resources with the collector's setting (`page`, `per_page`, `type`) |
| **GetIgnoreList** | GET | `/api/ignore-list` | List resource filters + available choices |
| **UpdateIgnoreList** | PUT | `/api/ignore-list` | Update resource filters; remove settings or queue a scan |
| **GetDisabledLogTypes** | GET | `/api/disabled-logtypes` | List disabled categories + supported types |
| **UpdateDisabledLogTypes** | POST | `/api/disabled-logtypes` | Disable/enable log types with cleanup |
| **GetGeneralLogType** | GET | `/api/general-logtype` | General catch-all log type status |
| **UpdateGeneralLogType** | PUT | `/api/general-logtype` | Toggle general log type on/off |
| **UpdateEntraLogTypes** | POST | `/api/entra-logtypes` | Enable/disable an Entra ID (tenant) log type |
| **StopProcessing** | PUT | `/api/processing` | Toggle BlobLogProcessor on/off |
| **LogCollection** | PUT | `/api/collection` | Log Collection on/off (see 5.7) |
| **UpdateSettings** | GET/PUT | `/api/settings` | Read/update `AUTO_SCAN_ENABLED` and `MONITOR_PIPELINE_RESOURCES` |
| **RemoveDiagSettings** | POST | `/api/remove-diagnostic-settings` | Bulk remove all diagnostic settings |
| **DeleteCollector** | POST | `/api/delete-collector` | Delete the collector (see 5.8) |
| **CheckUpdate** | GET/POST | `/api/check-update` | Check for updates / auto-apply |

---

## 8. API Reference

### 8.1 GET /api/status

Returns complete system status. A representative example (not every field is shown):

**Response:**
```json
{
  "last_scan_time": "2026-11-13T05:00:00Z",
  "scan_in_progress": false,
  "scan_phases": ["..."],
  "scan_details": {"newly_configured": 20, "already_configured": 100, "errors": 0, "...": "..."},
  "subscription_ids": ["xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"],
  "general_logtype_enabled": false,
  "processing_enabled": true,
  "collection": {"enabled": true, "changed_at": ""},
  "update_state": {"pending_version": "", "previous_version": "", "submitted_at": "", "confirmed_at": "", "failed_at": "", "error": ""},
  "monitor_pipeline_resources": false,
  "auto_scan_enabled": true,
  "update_check_url": true,
  "operational_status": {"...": "..."},
  "entra": {"target_storage_account_name": "...", "logtypes": [], "any_enabled": false},
  "provisioned_regions": [
    {"region": "eastus", "storage_account": "s247diag<eastus><suffix>"},
    {"region": "westus", "storage_account": "s247diag<westus><suffix>"}
  ],
  "resources": {"total": 150, "active": 120, "ignored": 30},
  "logtypes": {
    "configured_count": 8,
    "configured_keys": ["S247_AuditEvent", "S247_SignInLogs", "..."],
    "disabled_count": 2,
    "disabled": ["AppServiceConsoleLogs", "AppServiceHTTPLogs"]
  },
  "configured_resources_count": 120,
  "errors": []
}
```

### 8.2 POST /api/scan

Triggers an immediate resource discovery and configuration scan.

**Response:** HTTP 202 with `{"status": "started", "message": "Scan queued. Poll /api/status for results."}`.

### 8.3 GET /api/ignore-list

**Response:**
```json
{
  "ignore_list": {
    "subscriptions": [],
    "locations": ["westeurope"],
    "resource_types": [],
    "tags": {"include": ["env=prod"], "exclude": ["temporary"]}
  },
  "available": {
    "subscriptions": ["<subscription-id>", "..."],
    "subscription_names": {"<subscription-id>": "Production"},
    "locations": ["eastus", "westus", "westeurope", "..."],
    "resource_types": ["Microsoft.Web/sites", "..."],
    "tags": ["env=prod", "..."]
  }
}
```

### 8.4 PUT /api/ignore-list

**Request:**
```json
{
  "subscriptions": [],
  "locations": ["westeurope"],
  "resource_types": [],
  "tags": {"include": ["env=prod"], "exclude": ["temporary"]}
}
```

**Response:**
```json
{
  "ignore_list": { ... },
  "diag_settings_removed": 5,
  "diag_settings_not_removed": 0,
  "scan_queued": true
}
```

### 8.5 GET /api/disabled-logtypes

**Response:**
```json
{
  "disabled_logtypes": ["AppServiceConsoleLogs"],
  "supported_types": [
    {"category": "AuditEvent", "category_key": "auditevent", "s247_logtype": "<catalog-logtype>", "s247_display_name": "<catalog-display-name>", "resource_types": [], "disabled": false},
    {"category": "AppServiceConsoleLogs", "category_key": "appserviceconsolelogs", "s247_logtype": "<catalog-logtype>", "s247_display_name": "<catalog-display-name>", "resource_types": [], "disabled": true}
  ]
}
```

### 8.6 POST /api/disabled-logtypes

**Request:**
```json
{"action": "disable", "category": "AuditEvent"}
```

**Response:**
```json
{
  "action": "disable",
  "categories": ["AuditEvent"],
  "count": 1,
  "diag_settings_removed": 3,
  "diag_settings_not_removed": 0,
  "disabled_logtypes": ["AuditEvent", "AppServiceConsoleLogs"]
}
```

The request also accepts `"categories": [...]` (up to 200) instead of `category`. `enable` returns a `note` instead of the removal counts. `scan_queued: true` is added when a scan was running (it loaded the old list), so another scan is queued.

### 8.7 PUT /api/processing

**Request:**
```json
{"enabled": false}
```

**Response:**
```json
{"enabled": false, "message": "Log processing has been stopped"}
```

### 8.8 POST /api/remove-diagnostic-settings

**Response:**
```json
{
  "removed": 45,
  "skipped": 5,
  "errors": 0,
  "unresolved": 0,
  "details": [
    {"id": "/subscriptions/.../myVM", "status": "removed"},
    {"id": "/subscriptions/.../mySQL", "status": "removed"}
  ],
  "auto_scan_disabled": true,
  "tracking_cleared": true
}
```

The call first sets `AUTO_SCAN_ENABLED=false` (an app-setting change, which restarts the Function App) so the next timer scan cannot re-create the settings, and holds the scan lock for the whole removal: while a scan runs it answers HTTP 409. Tracking is cleared only for resources whose setting is confirmed gone.

### 8.9 GET /api/health

**Response:**
```json
{
  "status": "alive",
  "python_version": "Python 3.13.x",
  "dependencies": {
    "azure.identity": "ok",
    "azure.mgmt.resource": "ok",
    "azure.mgmt.monitor": "ok",
    "azure.mgmt.storage": "ok",
    "azure.mgmt.web": "ok",
    "azure.storage.blob": "ok",
    "requests": "ok",
    "shared": "ok",
    "shared.azure_manager": "ok",
    "shared.region_manager": "ok",
    "shared.ignore_list": "ok",
    "shared.log_parser": "ok",
    "shared.site24x7_client": "ok",
    "shared.updater": "ok"
  },
  "deps_ok": true
}
```

---

## 9. Data Storage Architecture

### 9.1 Primary Storage (AzureWebJobsStorage)

Used by the Function App runtime and for all configuration blobs.

```
AzureWebJobsStorage
├── config/                                    ← Configuration container
│   ├── azure-log-types.json                   ← Supported types cache from Site24x7
│   ├── disabled-logtypes.json                 ← ["AuditEvent", "SignInLogs"]
│   ├── configured-resources.json              ← {resource_id: {categories, storage, time}}
│   ├── ignore-list.json                       ← {subscriptions, locations, resource_types, tags}
│   └── logtype-configs/                       ← Per-category sourceConfig
│       ├── S247_AuditEvent.json
│       ├── S247_SignInLogs.json
│       ├── S247_SQLSecurityAuditEvents.json
│       └── ...
├── s247-checkpoints/                          ← Processing checkpoints
│   └── blob-processor-checkpoint.json         ← {account/container/blob: byte/block position, skip_records, last_seen}
└── azure-webjobs-*/                           ← Function runtime (managed by Azure)
```

### 9.2 Regional Storage Accounts

Auto-provisioned per region. Azure Diagnostic Settings write logs here.

```
s247diag{region}{suffix}  (e.g., s247diag<eastus><suffix>)
├── insights-logs/                             ← Base container (validation)
├── insights-logs-auditevent/                  ← Created by Azure Diagnostic Settings
│   └── resourceId=.../y=2026/m=03/d=11/h=05/m=00/PT1H.json
├── insights-logs-signinlogs/
│   └── resourceId=.../y=2026/m=03/d=11/h=05/m=00/PT1H.json
├── insights-logs-sqlsecurityauditevents/
│   └── ...
└── ...
```

### 9.3 Configured Resources Tracking

```json
{
  "/subscriptions/sub-123/resourceGroups/prod-rg/providers/Microsoft.KeyVault/vaults/myVault": {
    "categories": ["AuditEvent"],
    "storage_account": "s247diag<eastus><suffix>",
    "configured_at": "2026-03-11T05:00:00Z"
  },
  "/subscriptions/sub-123/resourceGroups/prod-rg/providers/Microsoft.Sql/servers/mySQL": {
    "categories": ["SQLSecurityAuditEvents", "DatabaseWaitStatistics"],
    "storage_account": "s247diag<westus><suffix>",
    "configured_at": "2026-03-11T05:00:00Z"
  }
}
```

---

## 10. Site24x7 Integration

### 10.1 Site24x7 API

The collector calls two Site24x7 endpoints, authenticated via the `deviceKey` query parameter.

#### GET /applog/azure/logtype_supported

Returns all supported Azure log types (50+ types).

```
GET /applog/azure/logtype_supported?deviceKey={api_key}

Response:
{
  "supported_types": [
    {
      "logtype": "S247_AuditEvent",
      "display_name": "Azure Audit Event",
      "log_categories": ["AuditEvent"]
    },
    ...
  ]
}
```

#### GET /applog/logtype

Creates or checks one Azure category per request; the collector batches these requests in parallel.

```
GET /applog/logtype?deviceKey={api_key}&logType=auditevent

Response (excerpt):
{
  "logType": "<catalog-logtype>",
  "apiUpload": true,
  "jsonPath": [{"name": "Time", "key": "time"}]
}
The client builds sourceConfig from this response.
```

### 10.2 Authentication Flow

```
Function App                          Site24x7 Server
    │                                      │
    ├─ GET /applog/azure/logtype_supported │
    │  ?deviceKey={SITE24X7_API_KEY}       │
    │ ────────────────────────────────────►│
    │                                      ├─ Validates the Device Key
    │                                      │
    │◄────────────────────────────────────│  200 + JSON (if valid)
    │                                      │  400 (if invalid key)
```

### 10.3 Log Upload Protocol

```
POST https://{uploadDomain}/upload

Headers:
  X-DeviceKey: {current SITE24X7_API_KEY}
  X-LogType: {logType from sourceConfig}
  X-StreamMode: 1
  Log-Size: {uncompressed byte size}
  Content-Type: application/json
  Content-Encoding: gzip
  User-Agent: AZURE-DiagLogs-Function

Body: gzip-compressed JSON array of parsed log records

Response Headers:
  x-uploadid: {upload tracking ID}
```

### 10.4 sourceConfig Structure

Built by the collector from the `/applog/logtype` response. Saved as JSON in `config/logtype-configs/S247_{category}.json` without the Device Key (`apiKey`): every upload uses the live `SITE24X7_API_KEY` and upload domain instead (`shared/config_store.py`, `shared/site24x7_client.py`).

```json
{
  "logType": "S247_AuditEvent",
  "uploadDomain": "logc.site24x7.com",
  "dateFormat": "%Y-%m-%dT%H:%M:%S.%f",
  "dateField": "time",
  "jsonPath": [
    {"name": "time", "key": "time"},
    {"name": "resourceId", "key": "resourceId"},
    {"name": "category", "key": "category"},
    {"name": "operationName", "key": "operationName"},
    {"name": "level", "key": "properties.level", "type": "string"},
    {"name": "properties", "key": "properties", "type": "json-object"}
  ],
  "filterConfig": {
    "level": {"match": true, "values": ["Error", "Warning", "Critical"]}
  },
  "maskingConfig": {
    "clientIpAddress": {"regex": "(\\d+\\.\\d+)\\.\\d+\\.\\d+", "string": "$1.xxx.xxx"}
  },
  "hashingConfig": {
    "callerIdentity": {"regex": "(.+)"}
  },
  "derivedConfig": {
    "operationName": [{"regex": "(?P<service>\\w+)/(?P<action>\\w+)"}]
  }
}
```

---

## 11. Security Architecture

### 11.1 Authentication Layers

| Component | Method | Details |
|-----------|--------|---------|
| **HTTP Endpoints** | Function Keys | `authLevel: "function"` — requires `?code=<key>` or `x-functions-key` header |
| **Azure APIs** | Managed Identity | `DefaultAzureCredential()` with system-assigned identity |
| **Site24x7 API** | Device Key | `SITE24X7_API_KEY` passed as `deviceKey` query parameter |
| **Site24x7 Upload** | Device Key | `X-DeviceKey` header with the live `SITE24X7_API_KEY` (never a stored copy) |

### 11.2 Function Key Management

```
Azure Portal → Function App → App Keys
  ├── Host Keys (apply to all functions)
  │   └── default: <auto-generated>
  └── Function Keys (per-function)
      └── default: <auto-generated>

Usage:
  curl "https://s247-diag-func-XXXXXX.azurewebsites.net/api/status?code=<KEY>"
  curl -H "x-functions-key: <KEY>" "https://s247-diag-func-XXXXXX.azurewebsites.net/api/status"
```

### 11.3 Key Rotation

If a function key is exposed:
1. Navigate to Azure Portal → Function App → App Keys
2. Renew the compromised key (generates new value)
3. Update any clients using the old key
4. Old key is immediately invalidated

### 11.4 Storage Security

| Setting | Value |
|---------|-------|
| Minimum TLS | 1.2 |
| Public blob access | Disabled |
| Management locks | `CanNotDelete` on the collector's resource group (set by the template) |
| Access method | Connection string (AzureWebJobsStorage) / Managed Identity |

---

## 12. Resilience & Error Handling

### 12.1 Rate Limiter

```
Type: Token Bucket
Rate: 100 tokens/second
Behavior: Blocks until token available (prevents API throttling)
```

### 12.2 Circuit Breaker

```
States: CLOSED (normal) → OPEN (failing) → HALF_OPEN (testing)

CLOSED → OPEN:    After 8 failures (persisted across runs)
OPEN → HALF_OPEN: After 300 seconds (5 minutes)
HALF_OPEN → CLOSED: On first success
HALF_OPEN → OPEN:   On first failure

When OPEN: post_logs() returns False immediately (records remain for retry within storage retention)
```

### 12.3 Checkpoint Recovery

- BlobLogProcessor tracks byte/block positions and skip_records per blob
- On function restart/retry, processing resumes from checkpoint
- At-least-once delivery: interrupted checkpoint saves or unsplittable block ranges can duplicate records; retention and permanent refusals can lose records

### 12.4 Error Handling Patterns

| Scenario | Handling |
|----------|---------|
| Resource doesn't support diagnostic logs | Skip, continue scanning |
| Storage account provisioning fails | Log error, continue with other regions |
| Site24x7 API unavailable | Circuit breaker opens; records remain for retry within storage retention |
| Upload answered HTTP 400 | The upload service also answers 400 to an unknown Device Key, so the collector checks the key against the Site24x7 API: if the key is refused too (or the check gets no answer), the batch is retried, not dropped, and `/api/status` reports the Device Key as refused; otherwise the batch is rejected (dropped or split) |
| Upload answered 401/403 | Device Key refused; batch retried, circuit breaker counts a failure |
| Blob download fails | Skip blob, continue processing others |
| Diagnostic setting creation fails | Log error, track in error count |
| Retention rule cannot be set | Counted as a region error; the next scan retries |
| Config blob not found | Return empty/default structure |
| Partial scan failure | Continue processing remaining resources |

---

## 13. Configuration Reference

### 13.1 Required Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `SUBSCRIPTION_IDS` | Comma-separated Azure subscription IDs | `xxxxxxxx-...,a1b2c3d4-...` |
| `SITE24X7_API_KEY` | Site24x7 device key for API authentication | `us_abc123def456` |
| `AzureWebJobsStorage` | Function App storage connection string | (auto-set by Azure) |
| `TIMER_SCHEDULE` | CRON expression for DiagSettingsManager | `0 0 */6 * * *` (every 6h) |

### 13.2 Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SITE24X7_BASE_URL` | `https://www.site24x7.com` | Site24x7 DC URL |
| `RESOURCE_GROUP_NAME` | `s247-diag-logs-rg` | Resource group name |
| `DIAG_STORAGE_SUFFIX` | `""` | Suffix for storage account naming |
| `PROCESSING_ENABLED` | `true` | Toggle BlobLogProcessor |
| `GENERAL_LOGTYPE_ENABLED` | `false` | Enable general catch-all log type |
| `S247_GENERAL_LOGTYPE` | (none) | Base64-encoded general sourceConfig |
| `UPDATE_CHECK_URL` | (none; the template sets `site24x7/azure-log-collector`) | URL or `owner/repo` shorthand for auto-updates |
| `UPDATE_CHANNEL` | `stable` | `stable` or `prerelease` — release channel filter |
| `PINNED_VERSION` | (none) | A pin freezes the collector at that version and never downgrades (`pinned_older` if the pin is older than installed). A newer pin installs only if it matches the fetched release; older releases are not fetched. |
| `SKIP_AUTO_UPDATE` | `false` | `true` blocks automatic and manual updates/checks; `AUTO_UPDATE_ENABLED=false` leaves checks and manual installs enabled |
| `MIN_RELEASE_AGE_MINUTES` | `60` | Minimum age before a release is eligible |
| `AUTO_UPDATE_ENABLED` | `false` | `true` lets AutoUpdater install new releases automatically |
| `GITHUB_TOKEN` | (none) | Optional token for GitHub API release checks |
| `AUTO_SCAN_ENABLED` | `true` | `false` skips scheduled and startup scans (the collector still reports to Site24x7) |
| `MONITOR_PIPELINE_RESOURCES` | `false` | `true` also collects logs from the collector's own resource group; other collectors' resources (tagged `managed-by: s247-diag-logs`) are never collected |
| `MIN_SCAN_INTERVAL_MINUTES` | `30` | A startup scan is skipped when the last scan finished more recently |
| `BLOB_RETENTION_DAYS` | `1` | Lifecycle retention of regional log blobs, in whole days (values below 1 use the default; idle accounts: 1 day). See [Log retention](../README.md#log-retention) |
| `DIAG_SEED_REGION` | (none; template: deployment region) | Region whose account receives global resources' logs; used in storage naming |
| `COLLECTOR_RESOURCE_ID` | (none; template: Function App resource ID) | The collector's own Azure resource ID; derived from the other settings when unset |
| `INITIAL_RESOURCE_FILTERS` | (none; template: deployment-form filters) | Saved once as the resource filters when none exist yet |
| `SITE24X7_KEY_EXCLUDE` | `""` | Comma-separated functions whose keys are not shared with Site24x7; `*` shares none (the collector then stops reporting) |
| `SITE24X7_UPLOAD_DOMAIN` | (derived from `SITE24X7_BASE_URL`) | Overrides the log upload domain (testing) |
| `DEPLOYMENT_STORAGE_CONNECTION_STRING` | (set by the template) | Host storage connection the Flex Consumption plan reads the app package from |
| `LAST_SCAN_TIME` | `never` | Legacy read fallback; current timestamp is in scan-state.json |
| `FUNCTION_APP_NAME` | `""` | Legacy fallback; Azure supplies `WEBSITE_SITE_NAME` |

### 13.3 Runtime Configuration (host.json)

```json
{
  "version": "2.0",
  "functionTimeout": "00:10:00",
  "logging": {
    "applicationInsights": {
      "samplingSettings": {
        "isEnabled": true,
        "excludedTypes": "Request"
      }
    },
    "logLevel": {
      "default": "Warning",
      "Host.Results": "Error",
      "Host.Aggregator": "Information",
      "Function": "Information",
      "Azure.Core": "Warning"
    }
  },
  "extensionBundle": {
    "id": "Microsoft.Azure.Functions.ExtensionBundle",
    "version": "[4.*, 5.0.0)"
  },
  "extensions": {
    "queues": {
      "visibilityTimeout": "00:05:00",
      "maxDequeueCount": 6
    }
  }
}
```

---

## 14. Deployment Guide

### 14.1 Prerequisites

- Azure subscription with Owner or User Access Administrator role
- Azure CLI (`az`) or Azure Cloud Shell access
- Site24x7 account with API key (device key)

### 14.2 Deploy Function App Code

The ARM template (`setup/azuredeploy.json`) installs the prebuilt `released-package.zip` of the release with OneDeploy (no remote build); `setup/setup.sh`, the shell alternative, zips the local source and deploys it with a remote build. See the README (Quick Start) for both paths. The steps below are for reference when configuring a deployment by hand.

### 14.3 Configure App Settings

```bash
az functionapp config appsettings set \
  --name s247-diag-func-XXXXXX \
  --resource-group s247-diag-logs-rg \
  --settings \
    SITE24X7_API_KEY="your_device_key" \
    SITE24X7_BASE_URL="https://www.site24x7.com" \
    SUBSCRIPTION_IDS="sub-id-1,sub-id-2" \
    TIMER_SCHEDULE="0 0 */6 * * *"
```

### 14.4 Assign RBAC Roles

```bash
SUB_ID="your-subscription-id"            # repeat Reader + Monitoring Contributor per monitored subscription
PRINCIPAL="managed-identity-principal-id"
RG="s247-diag-logs-rg"                   # the collector's resource group (default name)

az role assignment create --assignee "$PRINCIPAL" \
  --role "Reader" --scope "/subscriptions/$SUB_ID"

az role assignment create --assignee "$PRINCIPAL" \
  --role "Monitoring Contributor" --scope "/subscriptions/$SUB_ID"

az role assignment create --assignee "$PRINCIPAL" \
  --role "Contributor" \
  --scope "/subscriptions/$SUB_ID/resourceGroups/$RG"

az role assignment create --assignee "$PRINCIPAL" \
  --role "Locks Contributor" \
  --scope "/subscriptions/$SUB_ID/resourceGroups/$RG"
```

### 14.5 Verify Deployment

```bash
# Get function key
FUNC_KEY=$(az functionapp keys list \
  --name s247-diag-func-XXXXXX \
  --resource-group s247-diag-logs-rg \
  --query "functionKeys.default" -o tsv)

# Health check
curl "https://s247-diag-func-XXXXXX.azurewebsites.net/api/health?code=$FUNC_KEY"

# System status
curl "https://s247-diag-func-XXXXXX.azurewebsites.net/api/status?code=$FUNC_KEY"

# Trigger first scan
curl -X POST "https://s247-diag-func-XXXXXX.azurewebsites.net/api/scan?code=$FUNC_KEY"

# Open dashboard
open "https://s247-diag-func-XXXXXX.azurewebsites.net/api/dashboard?code=$FUNC_KEY"
```

---

## Appendix A: File Inventory

| File | Type | Description |
|------|------|-------------|
| `shared/azure_manager.py` | Module | Azure SDK wrapper (resources, diagnostics, settings) |
| `shared/region_manager.py` | Module | Regional storage account lifecycle |
| `shared/config_store.py` | Module | Blob-backed configuration store |
| `shared/site24x7_client.py` | Module | Site24x7 API client + log upload pipeline |
| `shared/ignore_list.py` | Module | Resource filtering (subscriptions, regions, resource types, tags) |
| `shared/storage_names.py` | Module | Storage account naming |
| `shared/collection.py` | Module | Log Collection on/off |
| `shared/teardown.py` | Module | Delete Collector |
| `shared/log_parser.py` | Module | Azure diagnostic log envelope parser |
| `shared/updater.py` | Module | Auto-update mechanism |
| `DiagSettingsManager/__init__.py` | Timer Function | Resource discovery + configuration |
| `BlobLogProcessor/__init__.py` | Timer Function | Blob polling + log upload |
| `AutoUpdater/__init__.py` | Timer Function | Self-update check |
| `ScanWorker/__init__.py` | Queue Function | Runs queued scans, Log Collection off and Delete Collector jobs |
| `Dashboard/__init__.py` | HTTP Function | Read-only web UI |
| `GetStatus/__init__.py` | HTTP Function | System status |
| `HealthCheck/__init__.py` | HTTP Function | Health probe |
| `TriggerScan/__init__.py` | HTTP Function | Manual scan trigger |
| `GetDebugInfo/__init__.py` | HTTP Function | Debug summary / bundle |
| `GetConfiguredResources/__init__.py` | HTTP Function | Resources with the collector's setting |
| `GetIgnoreList/__init__.py` | HTTP Function | Get resource filters |
| `UpdateIgnoreList/__init__.py` | HTTP Function | Update resource filters |
| `GetDisabledLogTypes/__init__.py` | HTTP Function | Get disabled log types |
| `UpdateDisabledLogTypes/__init__.py` | HTTP Function | Disable/enable log types |
| `GetGeneralLogType/__init__.py` | HTTP Function | Get general log type status |
| `UpdateGeneralLogType/__init__.py` | HTTP Function | Toggle general log type |
| `UpdateEntraLogTypes/__init__.py` | HTTP Function | Enable/disable Entra ID log types |
| `StopProcessing/__init__.py` | HTTP Function | Toggle processing |
| `LogCollection/__init__.py` | HTTP Function | Log Collection on/off |
| `UpdateSettings/__init__.py` | HTTP Function | Read/update whitelisted settings |
| `RemoveDiagSettings/__init__.py` | HTTP Function | Bulk remove diagnostic settings |
| `DeleteCollector/__init__.py` | HTTP Function | Delete Collector |
| `CheckUpdate/__init__.py` | HTTP Function | Check/apply updates |
| `requirements.txt` | Config | Python dependencies |
| `host.json` | Config | Function App runtime settings |
| `VERSION` | Config | Current version (1.1.0) |

---

*Document generated from codebase analysis — November 2026*
