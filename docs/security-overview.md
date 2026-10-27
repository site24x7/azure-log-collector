# Security Overview — Azure Diagnostic Logs Collector

**Audience:** Customer security, compliance and platform teams evaluating the collector before deploying it into their Azure tenant.

**Summary:** The collector is an Azure Function App that runs in your subscription. It uses its managed identity for Azure Resource Manager (ARM), storage account keys for reading the logs it buffers, and your Site24x7 Device Key for Site24x7. It creates diagnostic settings, regional storage accounts and Site24x7 log types. A deployment from the Azure portal template changes nothing until an admin starts collecting (`confirmAfterDiscovery`, on by default; turning it off, or installing with `setup/setup.sh`, which does not set `REVIEW_BEFORE_COLLECTING`, starts collecting at once). This document states what it can do and what it holds, including the open items in section 12.

---

## 1. Trust boundary and outbound traffic

```
┌──────────────────────────── Your Azure tenant ────────────────────────────┐
│  Azure resources ──diagnostic settings──► regional storage accounts       │
│                                            (created by the collector)     │
│                                                    │ read                 │
│                                                    ▼                      │
│                                  Function App (this project) ─────────────┼──► Site24x7
└───────────────────────────────────────────────────────────────────────────┘    (HTTPS)
```

The Function App makes outbound HTTPS calls only:

| Destination | Purpose |
|---|---|
| `plus.site24x7.<dc>` (`/applog/*`) | Log type catalog and creation, collector registration |
| `logc.site24x7.<dc>` (`/upload`) | Sending log records |
| `management.azure.com` | ARM: list resources, manage diagnostic settings, storage accounts, locks, own app settings, function keys |
| `*.blob/queue/table.core.windows.net` | The collector's own storage (state, queues) and the regional log storage accounts |
| Managed identity endpoint (local to the Function App) | Azure access tokens |
| `api.github.com`, `github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com` | Update checks and update packages, only when `UPDATE_CHECK_URL` is set (the template sets it to this repository) |

`<dc>` is the Site24x7 data center chosen at deployment: `com` (US), `in`, `eu`, `net.au`, `cn` or `jp`.

**Requests that carry the Device Key** (Site24x7 API calls, registration and uploads) go over HTTPS only and refuse HTTP redirects. A redirect is reported as an error and never followed, so the key is never sent to another host.

The Site24x7 addresses come from the `SITE24X7_BASE_URL` app setting. The upload host can be overridden with `SITE24X7_UPLOAD_DOMAIN`, for testing. Whoever can change app settings can therefore redirect where logs and the key are sent; that requires Contributor on the Function App.

---

## 2. Identities and credentials

| Credential | Where it is | Used for |
|---|---|---|
| System-assigned managed identity | Azure-managed | All ARM calls |
| `AzureWebJobsStorage` / `DEPLOYMENT_STORAGE_CONNECTION_STRING` | App settings (connection strings with the account key of the collector's own storage account) | Functions runtime, the collector's state, scan queue |
| Regional storage account keys | Read at run time through ARM `listKeys` (Contributor on the collector's resource group), held in memory only | Reading the buffered log blobs |
| Site24x7 Device Key | App setting `SITE24X7_API_KEY` (encrypted at rest by Azure) | Authenticating to Site24x7. It is not stored with the saved log type configurations, and the collector never writes it to its logs. (On the Site24x7 side, a request with an unknown Device Key is logged by Site24x7's own log-collection service; that is outside this collector.) |
| Function App host keys | Azure-managed | Authorize every HTTP endpoint (section 7). Used by you, for example to open the dashboard. **Never sent to Site24x7.** |
| `site24x7` function key | Azure-managed; created by the collector on the 14 functions Site24x7 calls | **Sent to Site24x7** at registration (section 6). It does not open the dashboard, debug data or health endpoint. |

**Rotation**
- **Device Key:** update `SITE24X7_API_KEY`. Uploads always use the current setting.
- **Host keys:** rotate in the Azure portal. This doesn't affect Site24x7.
- **`site24x7` key:** rotate it on any one of its functions. Within 15 minutes the collector notices, sets one new value on all of them, and re-registers with Site24x7.
- **Storage account keys:** rotate the collector's own account and update the two connection strings. Regional account keys are read fresh on every run.

The deployment template writes no key to its outputs. `dashboardUrl` is the address without a key.

---

## 3. Roles granted to the managed identity

| Role | Scope | Why |
|---|---|---|
| Reader | Each monitored subscription | List resources and their log categories |
| Monitoring Contributor | Each monitored subscription | Create, update and delete diagnostic settings |
| Contributor | The collector's own resource group (`s247-diag-logs-<suffix>-rg`, or `s247-diag-logs-rg`) | Regional storage accounts and their locks, the Function App's own app settings, its function keys (the `site24x7` key) and code deployment (self-update) |

It has no Owner role and no `Microsoft.Authorization/roleAssignments` write. Its Contributor role covers its own resource group only.

Monitoring Contributor covers diagnostic settings on every resource in the subscription, so a compromised collector could change or remove diagnostic settings it did not create. The collector itself only touches settings named for this collector (`s247-diag-logs-<suffix>`). It removes the older shared name `s247-diag-logs` only when that setting sends to this collector's own storage account.

**Locks:** the resource group gets a CanNotDelete lock at deployment, and each regional storage account gets its own lock. The collector removes an account's lock only to delete an account it no longer needs (section 5).

**Ownership:** each storage account carries an `s247-collector` tag naming its collector. A collector only reads, adopts or deletes its own accounts, even when several share a resource group. It never overwrites or deletes a diagnostic setting with its name that sends somewhere other than its own resource group.

---

## 4. Review before collecting

A new portal-template deployment starts in review mode (`REVIEW_BEFORE_COLLECTING=true`; the template parameter `confirmAfterDiscovery` defaults to `true`).

**Until an admin starts collecting**, scans only:
- discover resources;
- apply the exclusions;
- report what they would set up: the number of resources, regions and resource types.

They create no storage accounts, Site24x7 log types or diagnostic settings, and remove nothing.

**Starting collection:** an admin reviews the exclusions and log categories on the Site24x7 page, then clicks **Start Collecting** (`POST /api/start-collecting`). The approval is stored in the collector's storage. If it can't be read, scans stay read-only.

Collectors installed before this feature, and deployments with `confirmAfterDiscovery=false`, collect without review.

---

## 5. What the collector changes in your tenant

Once collecting, every scan (every 6 hours, and on demand) does the following:
- **Diagnostic settings:** creates or updates this collector's diagnostic setting on each resource that is not excluded, sending the resource's log categories to the regional storage account.
- **Storage accounts:** creates one storage account per region that has resources.
  - It applies a lifecycle rule that deletes blobs after `BLOB_RETENTION_DAYS`, 7 by default.
  - Public blob access is off and TLS 1.2 is the minimum.
- **Clean-up:** removes this collector's setting from resources that became excluded or were deleted.
- **Unused storage:** deletes a regional storage account no longer needed. It skips that if the account holds logs newer than `SAFE_DELETE_MAX_AGE_DAYS`, 7 by default.
- **Site24x7:** creates the Site24x7 log types for new categories.

**Safeguards**
- If resource discovery is incomplete (a subscription could not be listed, or a check failed), the scan removes nothing that run: no settings and no storage.
- A scan that can't read the exclusion list stops.

**Admin actions**
- They can pause/resume processing through a shared storage flag, without restarting the Function App. Changes to automatic scans, general log type, own-resource collection and storage protection days still update app settings and restart it.
- They can remove all of this collector's diagnostic settings. This also turns automatic scans off first.

---

## 6. What Site24x7 holds and can do

The collector registers with Site24x7 after every scan, on restart, when its key changes (see Operational freshness and connectivity). It sends:
- its Function App name, address, resource ID, subscription, resource group and region;
- its version and a status summary;
- the **`site24x7` function key**. This is a dedicated key, not a host key, and the collector creates it on just these 14 functions: status, scan, processing, start-collecting, ignore-list (get and put), disabled-logtypes (get and post), entra-logtypes, general-logtype (put), settings, configured-resources, check-update and remove-diagnostic-settings.

Site24x7 stores the key encrypted. With it, Site24x7 admins can run those actions from the Site24x7 page. Each action is:
- available to Site24x7 Super Admins and Admins only;
- built by Site24x7's server from validated fields; the browser never sends a URL or a raw request body;
- audited in Site24x7.

The key never reaches the browser. Site24x7 checks at registration that the Function App itself accepts the key.

**Limiting or removing Site24x7's reach.** Set the app setting `SITE24X7_KEY_EXCLUDE`:
- A comma-separated list of functions (for example `RemoveDiagSettings,CheckUpdate`) takes Site24x7's key away from those functions only.
- `*` takes it away from all of them, and the collector then stops reporting to Site24x7.

The resource group lock forbids deleting keys, so the collector revokes a function's key by overwriting it with a random value nobody holds.

## 7. HTTP endpoints

All 18 HTTP functions use `authLevel: function`. Each needs a valid function or host key, and there is no anonymous endpoint (including `/api/health`). They are: dashboard, status, health, debug, scan, start-collecting, processing, settings, ignore-list (get and put), disabled-logtypes (get and post), general-logtype (get and put), entra-logtypes, configured-resources, check-update, remove-diagnostic-settings.

**Input checks**
- Request bodies must be JSON objects.
- On/off values must be real JSON booleans, and numbers must be integers in range.
- Settings come from a fixed allowlist.
- Exclusion lists are checked by type and size: at most 1,000 entries of up to 500 characters, with no `null` values.
- Log category and Entra category names are checked against known values.

**Dashboard**
- The page is opened with the key in the address (`?code=`). It immediately removes the key from the address bar and history, keeps it in session storage for that tab, and sends it to the API in the `x-functions-key` header, never in a URL.
- It sets `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, a Content Security Policy and no-cache headers.
- It HTML-escapes every value it displays, and handler arguments are passed as escaped JavaScript string literals.
- Site24x7 does not open it: its `site24x7` key is not valid for the dashboard. Open it with a host key from the Azure portal.

**Rate limits:** there is no application-level rate limit on the collector's endpoints. Site24x7 limits its own calls (20 actions per user per 5 minutes).

---

## 8. Data handling

- **Buffering:** logs are buffered in the regional storage accounts until they are sent.
  - The collector never deletes blobs.
  - The lifecycle rule deletes them after `BLOB_RETENTION_DAYS`, whether or not they were sent.
  - If Site24x7 can't be reached for longer than that, the oldest logs are lost.
- **Retries:** an upload that fails for a reason a retry can fix (Site24x7 unreachable, timeout, HTTP 408 / 429 / 5xx, or the Device Key refused with 401 / 403) is retried on the next run, every 2 minutes, while the logs are still kept.
  - Records Site24x7 refuses for good (HTTP 400 / 404 / 415 / 422, or a single record still too large after splitting on 413) are dropped and counted, not retried.
  - Delivery is at least once: a run that stops between an upload and saving its checkpoint, or a block range whose records cannot be split at block boundaries, can send some records again.
- **What is sent:** only the fields the Site24x7 log type defines are taken from each record.
  - Masking, hashing and derived fields from the log type configuration are applied before sending.
  - Masking and hashing fail closed: if a rule cannot be applied, the whole field is withheld.
  - The Function App name is added as the log source (`s247agentuid`), and the event time as `_zl_timestamp`.
- **Disabled categories** are not sent, including through the general log type. If the list of disabled categories can't be read, the run is skipped, and the logs wait for the next run.
- **Logging:** unparseable records are counted but never written to the collector's logs.
- **Encryption:** data is sent over TLS 1.2 or later. Storage is encrypted at rest by Azure.

---

## 9. Self-update

Updates come from the GitHub releases of `UPDATE_CHECK_URL`, which the template sets to this repository. **Automatic installs are off by default** (`AUTO_UPDATE_ENABLED=false`): the daily check only records what is available, and an update is installed when an admin asks for it from Site24x7. Every gate below applies to both.

**Before deploying a release, the updater checks:**
- the channel: pre-releases are refused unless `UPDATE_CHANNEL=prerelease`;
- the age: releases younger than `MIN_RELEASE_AGE_MINUTES` (60) are skipped;
- the package host must belong to the configured repository, over HTTPS;
- the package's SHA-256 must match the release's `version.json` (`REQUIRE_PACKAGE_SHA256`, on by default);
- the zip: archive integrity, application `.py` files parse (vendored `.python_packages` is excluded), every `.json` file is valid, and the required files are present;
- the package's `VERSION` must equal the version the release advertises, so an older package can't pass the downgrade check.

AutoUpdater records each run as an event; the new version confirms the update on its first processing run.

**Release builds**
- Releases are built by GitHub Actions from the tagged commit.
- The workflow validates the version first and pins its actions to commit SHAs.
- It never replaces a published release: a run for a version that is already published stops, a tag that exists without a release is refused, and only an unpublished draft left by a failed run is rebuilt. There is no option to force it.
- The repository does not yet enforce this on its own: immutable releases are off (see section 12), so a repository admin could still edit a release by hand.

**Controls**

| App setting | Effect |
|---|---|
| `AUTO_UPDATE_ENABLED=true` | The daily run installs updates on its own (default `false`: it only checks). |
| `SKIP_AUTO_UPDATE=true` | No automatic or manual update is applied. |
| `UPDATE_CHECK_URL` unset | No update checks at all. |
| `UPDATE_CHANNEL=stable` | The default: pre-releases are refused. |
| `PINNED_VERSION=x.y.z` | A pin freezes the collector at that version and never downgrades (`pinned_older` if the pin is older than installed). A newer pin installs only if it matches the fetched release; older releases are not fetched. |

**Manual update:** `GET /api/check-update` reports what is available, and `POST /api/check-update?apply=1` applies it. Site24x7 admins use **Update Collector** (Overview > Collector Details > Version).

**Installing the verified bytes:** after the checks, the collector copies the verified zip to its own storage account (`s247-packages`) and installs it with OneDeploy from a read-only link to that copy that expires after 6 hours, so Azure installs exactly the bytes that were checked.
- Before installing, the collector imports every function and shared module of the new version in a separate process, using only the package's own dependencies; a package that cannot load is refused. This catches package-loading errors but cannot guarantee startup or runtime health.
- An update counts as done only when the new version confirms on its first run (`update_state` in `/api/status`, which also records the previous version).
- A failed update is not rolled back automatically. To go back, install the previous release's package (`az functionapp deploy --type zip --src-url https://github.com/site24x7/azure-log-collector/releases/download/v<previous-version>/released-package.zip`).

---

## 10. Audit and troubleshooting data

- **Application Insights** receives the Function App's logs, if it is configured.
- **Collector events:** admin actions are recorded in the collector's event log (`log_audit`), along with scans, update runs and health checks. These records live in the collector's own storage account, keep only the most recent events and can be changed by anyone who can write to that account, so they are **not tamper-evident**. Use Application Insights or Site24x7's audit log for a protected trail.
- **Site24x7:** records an audit entry for every action an admin runs from the Site24x7 page.
- **Debug bundle:** the dashboard can export one, with secrets redacted. Clearing debug events takes a POST.

---

## 11. Uninstall

- **Remove the collector's settings first:** **Remove Diagnostic Settings** on the collector's Settings tab in Site24x7 (or the dashboard) removes this collector's diagnostic settings and turns automatic scans off.
- **Then run `setup/teardown.sh [--clean-diag-settings]`:**
  - It works only on the deployment's subscription and on a resource group tagged `managed-by=s247-diag-logs`.
  - It removes that group's locks, the managed identity's subscription role assignments, and the group itself, which holds the Function App, the regional storage accounts and their buffered logs.
  - With `--clean-diag-settings`, it also removes this collector's diagnostic setting from resources where it sends to this deployment's storage.
- **Deleting the resource group directly** does not remove diagnostic settings on your resources (they live on those resources) or the subscription-scope role assignments. Remove those separately.
- **In Site24x7:** delete the collector registration and the log types separately.

---

## 12. Known limits and open items

| Item | Status | Mitigation |
|---|---|---|
| Site24x7 holds a key to the collector's management endpoints (section 6) | By design (Site24x7 runs the admin actions) | Dedicated `site24x7` function key on 14 functions only; admin-only, server-built and audited actions in Site24x7; `SITE24X7_KEY_EXCLUDE` narrows or removes its reach. |
| A failed update is not rolled back automatically | Mitigated | A package whose functions cannot load (missing dependency, broken import, wrong Python build) is refused before install, by the release workflow and again by the collector. Packages are also checked (digest, structure, version) before install, releases install only after `MIN_RELEASE_AGE_MINUTES`, and the update is reported as confirmed only when the new version runs. The import check lowers the risk; it cannot prove the new version works at run time. Every release keeps its package as a release asset, and going back means installing the previous package again (README, Rollback). `SKIP_AUTO_UPDATE` stops updates; `PINNED_VERSION` freezes the collector at the pin and never downgrades (`pinned_older` for an older pin); a newer pin installs only if it matches the fetched release, and older releases are not fetched. |
| Masking and filter patterns come from Site24x7 and run with Python's backtracking regular expressions | Accepted | Patterns are defined by Site24x7, not by log content. A rule that fails is applied fail-closed (the field is withheld). |
| Release packages the template and updater install | Open (repository setting) | The template and the updater install release assets. The release workflow publishes each release once with all its files (draft, then publish) and has no path to replace a published one. Immutable releases are not yet on for the repository; until a repository admin turns them on, an admin could still change a published release's files or tag by hand. |
| No Key Vault, Private Endpoint or customer-managed key by default | By design (single template) | Key Vault references in app settings work for `SITE24X7_API_KEY`. The Flex Consumption plan supports virtual network integration; the template doesn't configure it. |

## 13. Common questions

| Question | Answer |
|---|---|
| Can it read data outside its own resource group? | It reads the resource inventory and log categories of the monitored subscriptions, and the log blobs in its own regional storage accounts. It does not read data of your other resources (disks, databases, other storage accounts). |
| Can it change my resources? | It manages diagnostic settings on resources in the monitored subscriptions (only its own, as described in section 3). It creates and deletes storage accounts in its own resource group, and writes its own app settings. It does nothing until an admin starts collecting (section 4). |
| Can it change permissions? | No. It has no role-assignment rights. |
| What if the GitHub repository were compromised? | An attacker would need to publish a release that passes the update gates (section 9) and wait for the age gate. You can turn updates off (`SKIP_AUTO_UPDATE=true`) or point `UPDATE_CHECK_URL` at your own fork. |
| What if Site24x7 were compromised? | With the Device Key, an attacker could send logs into your Site24x7 account. With the stored `site24x7` key (section 6), they could call those 14 endpoints, for example change settings or remove diagnostic settings, but not the dashboard or debug data. Revoke it with `SITE24X7_KEY_EXCLUDE=*`. They cannot reach your other Azure resources: the collector's identity is limited as described in section 3. |
| Can I check what is deployed? | Each release zip is built by GitHub Actions from a tagged commit, with its SHA-256 in `version.json`. Compare the running package with the tag. |

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
