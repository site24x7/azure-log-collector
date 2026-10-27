# Troubleshooting Guide — Azure Log Collector

This guide helps Site24x7 support engineers diagnose and resolve issues with the Azure Diagnostic Logs Function App deployed in customer environments.

## Prerequisites

You need:
- **Azure CLI** (`az`) with access to the customer's subscription
- **Resource Group name**: `s247-diag-logs-<suffix>-rg` when a deployment suffix was set, `s247-diag-logs-rg` for installs without one (shown in the deployment Outputs as `resourceGroupName`). The commands below use `s247-diag-logs-rg`; substitute yours.
- **Function App name**: `s247-diag-func-XXXXXX` (unique per customer, find via Portal or CLI)
- **App Insights name**: `s247-diag-func-XXXXXX-ai` (same suffix as function app)

Find function app name from subscription:
```bash
az functionapp list -g s247-diag-logs-rg --query "[].name" -o tsv
```

---

## Quick Health Check

### 1. Is the Function App running?

```bash
az functionapp show -g s247-diag-logs-rg -n <func-app-name> --query "state" -o tsv
```
Expected: `Running`

### 2. Are there recent errors?

```bash
az monitor app-insights query -g s247-diag-logs-rg --app <app-insights-name> \
  --analytics-query "exceptions | where timestamp > ago(1h) | summarize count() by outerMessage | order by count_ desc"
```

### 3. Are functions executing?

```bash
az monitor app-insights query -g s247-diag-logs-rg --app <app-insights-name> \
  --analytics-query "requests | where timestamp > ago(1h) | summarize count() by name, resultCode | order by count_ desc"
```

### 4. Use the Dashboard Debug tab

Open the dashboard URL in a browser:
```
https://<func-app-name>.azurewebsites.net/api/dashboard?code=<function-key>
```
Navigate to the **Debug** tab — it shows config validation, S247 connectivity, recent events, and processing stats.

---

## Common Issues

### Issue: "No module named 'azure.storage'" (or any ModuleNotFoundError)

**Symptom**: All functions fail with `ModuleNotFoundError`.

**Cause**: Pip packages not installed. Happens when the app was deployed without remote build, or the package cache was cleared.

**Fix**: Redeploy with remote build:
```bash
# Build zip from function-app directory (exclude tests)
cd function-app
zip -r /tmp/s247-deploy.zip . -x "tests/*" -x "__pycache__/*" -x "*.pyc" -x "requirements-dev.txt"

# Deploy with remote build to install pip packages
az functionapp deployment source config-zip \
  -g s247-diag-logs-rg -n <func-app-name> \
  --src /tmp/s247-deploy.zip --build-remote true

# Restart after deploy
az functionapp restart -g s247-diag-logs-rg -n <func-app-name>
```

**Verify**:
```bash
az monitor app-insights query -g s247-diag-logs-rg --app <app-insights-name> \
  --analytics-query "exceptions | where timestamp > ago(10m) | where innermostMessage contains 'ModuleNotFoundError' | count"
```

---

### Issue: Logs not appearing in Site24x7

**Step 1 — Check if BlobLogProcessor is running**:
```bash
az monitor app-insights query -g s247-diag-logs-rg --app <app-insights-name> \
  --analytics-query "traces | where message contains 'BlobLogProcessor' | where timestamp > ago(30m) | order by timestamp desc | take 10 | project timestamp, message"
```

**Step 2 — Check if S247 API is reachable**:
```bash
az monitor app-insights query -g s247-diag-logs-rg --app <app-insights-name> \
  --analytics-query "traces | where message contains 'Circuit breaker' or message contains 'S247' | where timestamp > ago(1h) | order by timestamp desc | take 10"
```

If you see `"Circuit breaker OPEN"` — uploads have repeatedly failed or the Device Key was refused. After 5 minutes it permits a test attempt, which may still fail. Check:
- Is `SITE24X7_API_KEY` configured correctly?
- Is `SITE24X7_BASE_URL` pointing to the right data center?
- Is the Site24x7 AppLogs endpoint operational?

**Step 3 — Check for config issues**:
```bash
az monitor app-insights query -g s247-diag-logs-rg --app <app-insights-name> \
  --analytics-query "traces | where message contains 'No logtype configs' or message contains 'nothing to process' | where timestamp > ago(1h) | take 5"
```

If you see `"No logtype configs found"` — the scan hasn't created log type configurations yet. Trigger a scan from the Dashboard.

**Step 4 — Check for processing errors**:
```bash
az monitor app-insights query -g s247-diag-logs-rg --app <app-insights-name> \
  --analytics-query "traces | where message contains 'Error reading blob' or message contains 'Failed to post' | where timestamp > ago(1h) | order by timestamp desc | take 10"
```

---

### Issue: Scan stuck / not completing

**Check scan status**:
```bash
az monitor app-insights query -g s247-diag-logs-rg --app <app-insights-name> \
  --analytics-query "traces | where message contains 'scan' or message contains 'DiagSettings' or message contains 'Phase' | where timestamp > ago(6h) | order by timestamp desc | take 20 | project timestamp, message"
```

**Common scan failures**:

| Message | Cause | Fix |
|---|---|---|
| `"Time budget exhausted"` | Too many resources, scan timed out at 8 min | Normal — it saves partial results and continues next cycle |
| `"Failed to list resources for subscription"` | Invalid subscription ID or insufficient permissions | Check `SUBSCRIPTION_IDS` app setting and Managed Identity role assignments |
| `"Category discovery failed"` | Specific resource type has API issues | Transient — will retry next scan |
| `"All log type creation(s) failed"` | Site24x7 API unreachable during scan | Check S247 connectivity (Device Key, Base URL) |

**Force a fresh scan**: Use the Dashboard → Controls → "Trigger Scan" button.

---

### Issue: Some resources not being monitored

**Check what resources were discovered**:
```bash
az monitor app-insights query -g s247-diag-logs-rg --app <app-insights-name> \
  --analytics-query "traces | where message contains 'discovery' or message contains 'resource' | where timestamp > ago(6h) | order by timestamp desc | take 20"
```

**Common causes**:
- **Resource in unsupported region**: Check Dashboard → Overview → Provisioned Regions
- **Resource type not supported**: Some Azure resource types don't emit diagnostic logs
- **Resource in ignore list**: Check Dashboard → Filters → Ignore List
- **Diagnostic categories disabled**: Check Dashboard → Filters → Disabled Log Types
- **Permissions**: Managed Identity needs Reader + Monitoring Contributor on the subscription

**Check role assignments**:
```bash
PRINCIPAL_ID=$(az functionapp identity show -g s247-diag-logs-rg -n <func-app-name> --query "principalId" -o tsv)
az role assignment list --assignee $PRINCIPAL_ID --query "[].{role:roleDefinitionName, scope:scope}" -o table
```
Expected: `Reader` and `Monitoring Contributor` at subscription scope.

---

### Issue: Function App in Error state

**Check host status**:
```bash
MASTER_KEY=$(az functionapp keys list -g s247-diag-logs-rg -n <func-app-name> --query 'masterKey' -o tsv)
az rest --method get \
  --url "https://<func-app-name>.azurewebsites.net/admin/host/status" \
  --headers "x-functions-key=$MASTER_KEY"
```

If `"state": "Error"` reports a missing storage connection string, verify
`AzureWebJobsStorage`. EventHubProcessor and `EVENTHUB_CONN` are no longer used:
```bash
# Disable the problematic function
az functionapp config appsettings set -g s247-diag-logs-rg -n <func-app-name> \
  --settings "AzureWebJobs.BlobLogProcessor.Disabled=true"
az functionapp restart -g s247-diag-logs-rg -n <func-app-name>
```

---

### Issue: High blob count / storage growing

**Check processing backlog**:
```bash
az monitor app-insights query -g s247-diag-logs-rg --app <app-insights-name> \
  --analytics-query "traces | where message contains 'backlog' or message contains 'pending' or message contains 'stale' | where timestamp > ago(1h) | take 10"
```

**Common causes**:
- **Processing disabled**: Check the processing control in Site24x7 or the dashboard; the stored flag overrides `PROCESSING_ENABLED`
- **BlobLogProcessor timing out**: If >500 blobs pending, processor may not finish in 2 min cycle
- **No S247 config for category**: Blobs accumulate until a scan creates the config; storage lifecycle expires blobs after BLOB_RETENTION_DAYS (default 7 days)

---

## Application Insights KQL Reference

### Recent exceptions (last hour)
```kql
exceptions
| where timestamp > ago(1h)
| order by timestamp desc
| take 20
| project timestamp, problemId, outerMessage, innermostMessage
```

### Function execution summary (last 24h)
```kql
requests
| where timestamp > ago(24h)
| summarize count(), avg(duration), percentile(duration, 95) by name, resultCode
| order by count_ desc
```

### BlobLogProcessor activity (last hour)
```kql
traces
| where message contains "BlobLogProcessor"
| where timestamp > ago(1h)
| order by timestamp desc
| take 30
| project timestamp, message
```

### Scan history (last 24h)
```kql
traces
| where message contains "Phase" or message contains "scan" or message contains "DiagSettings"
| where timestamp > ago(24h)
| order by timestamp desc
| take 50
| project timestamp, message
```

### Site24x7 API errors
```kql
traces
| where message contains "Circuit breaker" or message contains "S247" or message contains "Site24x7"
| where timestamp > ago(6h)
| order by timestamp desc
| take 20
| project timestamp, message
```

### All errors by component (last 24h)
```kql
traces
| where message contains "error" or message contains "Error" or message contains "failed" or message contains "Failed"
| where timestamp > ago(24h)
| summarize count() by tostring(split(message, ":")[0])
| order by count_ desc
```

---

## App Settings Reference

Key settings to verify when troubleshooting:

| Setting | Required | Description | How to Check |
|---|---|---|---|
| `SUBSCRIPTION_IDS` | Yes | Comma-separated subscription IDs | Auto-set to deployment subscription |
| `SITE24X7_API_KEY` | Yes | Site24x7 Device Key | Dashboard → Debug → Config Validation |
| `SITE24X7_BASE_URL` | Yes | `https://www.site24x7.com` (or .in/.eu etc.) | Dashboard → Debug |
| `PROCESSING_ENABLED` | No | Legacy fallback only; shared processing-control.json controls pause/resume | Dashboard → Controls |
| `GENERAL_LOGTYPE_ENABLED` | No | `true` — enables fallback log type | Dashboard → Controls |
| `TIMER_SCHEDULE` | No | CRON for DiagSettingsManager (default: every 6h) | App Settings |
| `DIAG_STORAGE_SUFFIX` | Yes | Unique suffix for storage account names | Auto-generated |

Check all settings:
```bash
az functionapp config appsettings list -g s247-diag-logs-rg -n <func-app-name> \
  --query "[?name=='SUBSCRIPTION_IDS' || name=='SITE24X7_API_KEY' || name=='SITE24X7_BASE_URL' || name=='PROCESSING_ENABLED'].{name:name, value:value}" -o table
```

---

## AutoUpdater Issues

### "AutoUpdater deployed a bad build"

**Symptoms:** After a scheduled update the Function App returns 503, or one of the functions stops working.

**Recovery:** `PINNED_VERSION` cannot roll back (it only freezes updates and never fetches an older release), and a collector that does not start cannot update itself.

```bash
# 1. Stop further updates while you investigate
az functionapp config appsettings set -g <resource-group> -n <func-app-name> \
  --settings SKIP_AUTO_UPDATE=true

# 2. Install the previous known-good package again (Flex Consumption keeps no earlier packages)
az functionapp deploy -g <resource-group> -n <func-app-name> --type zip \
  --src-url https://github.com/site24x7/azure-log-collector/releases/download/v<known-good-version>/released-package.zip
```

Check `/api/health` afterwards (Azure CLI 2.79 may print a JSON parsing error although the install succeeded). Then release the fix as a new, higher version and set `SKIP_AUTO_UPDATE=false`.

**Prevention:** `validate_zip_package()` now refuses packages with Python syntax errors, malformed JSON, or missing required files. `MIN_RELEASE_AGE_MINUTES=60` gives you a grace window to stop installs with `SKIP_AUTO_UPDATE`; recover with a higher release or a manual package install.

### "AutoUpdater didn't update us"

Check these in order:

1. **Are automatic installs enabled?** `AUTO_UPDATE_ENABLED=false` is the default: the timer only checks; an admin uses **Update Collector** in Site24x7. `SKIP_AUTO_UPDATE=true` blocks manual and automatic updates.
   ```bash
   az functionapp config appsettings list -g s247-diag-logs-rg -n <func-app-name> \
     --query "[?name=='AUTO_UPDATE_ENABLED' || name=='SKIP_AUTO_UPDATE' || name=='PINNED_VERSION' || name=='UPDATE_CHANNEL' || name=='UPDATE_CHECK_URL' || name=='MIN_RELEASE_AGE_MINUTES'].{name:name,value:value}" -o table
   ```

2. **Is the remote release a pre-release?** On the `stable` channel (default), pre-releases are refused. Set `UPDATE_CHANNEL=prerelease` only on test environments.

3. **Is the release too young?** `MIN_RELEASE_AGE_MINUTES` defaults to 60. Lower or set to `0` to pick up immediately.

4. **Audit events:** Query App Insights / debug logs:
   ```
   event == "auto_update_run" | order by timestamp desc | take 10
   ```
   The `action` field reveals the exact reason: `disabled`, `pinned_current`, `pinned_older`, `pinned_mismatch`, `update_available`, `prerelease_skipped`, `release_too_young`, `up_to_date`, `deployed`, `deploy_failed`.

### "Health check failed after deploy"

Check `update_state.confirmed_at` / `failed_at` in `/api/status`, and App Insights `exceptions` and `traces` for startup errors. There is no automatic rollback; reinstall the previous package per above.

---

## Escalation Checklist

Before escalating, collect:

1. **Function App name** and **subscription ID**
2. **App Insights exceptions** (last 24h): `exceptions | where timestamp > ago(24h) | summarize count() by outerMessage`
3. **Dashboard screenshot** (all tabs: Overview, Filters, Resources, Debug)
4. **Debug Bundle**: Dashboard → Debug → "Export Debug Bundle" (downloads JSON with all config + recent events)
5. **Processing stats**: Dashboard → Debug → Processing Runs section
6. **App settings**: Verify all required settings are present and non-empty

## Operational freshness and connectivity

Discovery and log processing are different schedules: discovery runs every six
hours by default; processing is scheduled every two minutes. Registration updates
Site24x7 after scans and on a worker's first BlobLogProcessor run. Failed
registration is retried and key changes are checked every 15 minutes.
It is not repeated every processing cycle and there is no periodic full-registration
heartbeat. Live authenticated status reads provide current health without rewriting
the database registration row. Pause/resume returns the saved state directly and
does not wait for a callback registration.

The Site24x7 detail page handles the two directions independently:

- If its authenticated Function App status read fails, returns a non-success HTTP
  response or an invalid body, it shows a collector API problem. It cannot infer
  the collector's current outbound health from that failure.
- If the API answers, its `operational_status` shows the last scan's Site24x7 API
  reachability and bounded phase/HTTP errors, plus the last observed upload
  attempt (`ok`, `failed`, `refused`, `rejected`) and its observation time.
  A reachable Function App does not prove that it can upload to Site24x7.
- With no fresh report or live snapshot for twenty-six hours, the page marks the
  historical status unavailable; a failed live check is shown immediately. Removing a stale registration still requires
  the separate twenty-six-hour policy. Viewer/list data remain registration
  snapshots; the existing live-read API is admin-only.

Refresh on the detail page re-reads the live collector status. While the collector
page is visible it also refreshes once a minute, so current status checks
continue without a manual reload. Live reads never
rewrite LAST_SYNC_TIME or claim that registration succeeded. Detailed errors are
allowlisted as phase/HTTP status only; response bodies, URLs and keys are omitted.

`last_processing_time` is the latest recorded processing-run completion, including
runs that uploaded zero records. It is not the last successful upload. Early
returns/failed runs can have no summary. `upload_status_time` identifies an actual
upload attempt: empty runs do not clear an earlier error. The last observation is
looked up in a bounded fifty-run history, so its absence means unknown. HTTP200
is receiver acknowledgement and does not prove that records are indexed.

A Site24x7 service outage can block both catalog calls and registration. Use
live status and report freshness to check the collector while reporting fails.

A failure of Site24x7's own collector-data REST endpoint is not evidence that
the Function App is unreachable. The client requires a recognised proxy result
(accepted + http_status) before classifying the collector API. A browser HTTP
error, missing result or Site24x7 error object shows Site24x7 API request failed,
with collector status unknown; it does not advise checking Azure. Settings and
filters use this same distinction. A stale report is historical evidence, not
a second diagnosis of an Azure outage.

### Pause/resume response time

`PUT /api/processing` saves `config/processing-control.json` rather than changing
an Azure app setting. It does not restart the Function App, enumerate keys or
wait for a registration callback. Status reads and each processing invocation
read this shared value without a worker-local cache. Only an absent control blob
uses the legacy `PROCESSING_ENABLED` app setting; storage/corrupt-state errors
must not permit processing. A failed save returns an error rather than success.
The next scheduled run observes the flag; a run already in progress may finish.
Other app-setting changes can still restart the collector. Network/storage and
audit persistence can still affect latency; no live response-time SLA is claimed.
