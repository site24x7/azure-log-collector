# Security Overview — Azure Diagnostic Logs Collector

**Audience:** Customer security, compliance and platform teams evaluating the collector before deploying it into their Azure tenant.

**Summary:** The collector is an Azure Function App that runs in your subscription. It uses its managed identity for Azure Resource Manager (ARM), storage account keys for reading the logs it buffers, and your Site24x7 Device Key for Site24x7. It creates diagnostic settings, regional storage accounts and Site24x7 log types. On a new deployment it changes nothing until an admin starts collecting. This document states what it can do and what it holds, including the open items in section 12.

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
| `api.github.com`, `github.com`, `objects.githubusercontent.com`, `raw.githubusercontent.com` | Update checks and update packages, only when `UPDATE_CHECK_URL` is set (the template sets it to this repository) |
| `<function-app>.azurewebsites.net/api/health` | Health check after an update (its own endpoint) |

`<dc>` is the Site24x7 data center chosen at deployment: `com` (US), `in`, `eu`, `net.au`, `cn` or `jp`.

**Requests that carry the Device Key** (Site24x7 API calls, registration and uploads) go over HTTPS only and refuse HTTP redirects. A redirect is reported as an error and never followed, so the key is never sent to another host.

The Site24x7 addresses come from the `SITE24X7_BASE_URL` app setting. The upload host can be overridden with `SITE24X7_UPLOAD_DOMAIN`, for testing. Whoever can change app settings can therefore redirect where logs and the key are sent; that requires Contributor on the Function App.

---

## 2. Identities and credentials

| Credential | Where it is | Used for |
|---|---|---|
| System-assigned managed identity | Azure-managed | All ARM calls |
| `AzureWebJobsStorage` / `WEBSITE_CONTENTAZUREFILECONNECTIONSTRING` | App settings (connection strings with the account key of the collector's own storage account) | Functions runtime, the collector's state, scan queue |
| Regional storage account keys | Read at run time through ARM `listKeys` (Contributor on the collector's resource group), held in memory only | Reading the buffered log blobs |
| Site24x7 Device Key | App setting `SITE24X7_API_KEY` (encrypted at rest by Azure) | Authenticating to Site24x7. It is not stored with the saved log type configurations, and it is never written to logs. |
| Function App default **host** key | Azure-managed | Authorizes every HTTP endpoint of the collector (section 7). **It is sent to Site24x7** at registration (section 6). |

**Rotation**
- **Device Key:** update `SITE24X7_API_KEY`. Uploads always use the current setting.
- **Host key:** rotate in the Azure portal. The collector re-reads its key from ARM on its first processing run after a restart and every 15 minutes, and re-registers with Site24x7 when the key has changed.
- **Storage account keys:** rotate the collector's own account and update the two connection strings. Regional account keys are read fresh on every run.

The deployment template writes no key to its outputs. `dashboardUrl` is the address without a key.

---

## 3. Roles granted to the managed identity

| Role | Scope | Why |
|---|---|---|
| Reader | Each monitored subscription | List resources and their log categories |
| Monitoring Contributor | Each monitored subscription | Create, update and delete diagnostic settings |
| Contributor | The collector's own resource group (`s247-diag-logs-<suffix>-rg`, or `s247-diag-logs-rg`) | Regional storage accounts and their locks, the Function App's own app settings, host key and code deployment (self-update) |

It has no Owner role and no `Microsoft.Authorization/roleAssignments` write. Its Contributor role covers its own resource group only.

Monitoring Contributor covers diagnostic settings on every resource in the subscription, so a compromised collector could change or remove diagnostic settings it did not create. The collector itself only touches settings named for this collector (`s247-diag-logs-<suffix>`). It removes the older shared name `s247-diag-logs` only when that setting sends to this collector's own storage account.

**Locks:** the resource group gets a CanNotDelete lock at deployment, and each regional storage account gets its own lock. The collector removes an account's lock only to delete an account it no longer needs (section 5).

---

## 4. Review before collecting

A new deployment starts in review mode (`REVIEW_BEFORE_COLLECTING=true`; the template parameter `startCollectingAtOnce` defaults to `false`).

**Until an admin starts collecting**, scans only:
- discover resources;
- apply the exclusions;
- report what they would set up: the number of resources, regions and resource types.

They create no storage accounts, Site24x7 log types or diagnostic settings, and remove nothing.

**Starting collection:** an admin reviews the exclusions and log categories on the Site24x7 page, then clicks **Start Collecting** (`POST /api/start-collecting`). The approval is stored in the collector's storage. If it can't be read, scans stay read-only.

Collectors installed before this feature, and deployments with `startCollectingAtOnce=true`, collect without review.

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
- They can change app settings, which restarts the Function App: processing on or off, automatic scans, general log type, own-resource collection, storage protection days.
- They can remove all of this collector's diagnostic settings. This also turns automatic scans off first.

---

## 6. What Site24x7 holds and can do

The collector registers with Site24x7 after every scan, on restart, and when its key changes. It sends:
- its Function App name, address, resource ID, subscription, resource group and region;
- its version and a status summary;
- its default **host key**.

Site24x7 stores the key encrypted. With it, Site24x7 admins can run the collector's actions from the Site24x7 page (scan, pause and resume, exclusions, log categories, Entra ID log types, settings, update, remove all, start collecting).

Each of those actions is:
- available to Site24x7 Super Admins and Admins only;
- built by Site24x7's server from validated fields; the browser never sends a URL or a raw request body;
- audited in Site24x7.

The key never reaches the browser.

**Implication:** someone who obtained the stored host key could call every collector endpoint, including settings changes, Remove All and applying an update. Site24x7 checks at registration that the key is accepted by the Function App itself. If that is not acceptable for your environment, see the open items in section 12.

---

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
- In Site24x7 it is offered to closed-beta admins only and will be removed once all actions are on the Site24x7 page.

**Rate limits:** there is no application-level rate limit on the collector's endpoints. Site24x7 limits its own calls (20 actions per user per 5 minutes).

---

## 8. Data handling

- **Buffering:** logs are buffered in the regional storage accounts until they are sent.
  - The collector never deletes blobs.
  - The lifecycle rule deletes them after `BLOB_RETENTION_DAYS`, whether or not they were sent.
  - If Site24x7 can't be reached for longer than that, the oldest logs are lost.
- **Retries:** failed uploads are retried on the next run, every 2 minutes.
- **What is sent:** only the fields the Site24x7 log type defines are taken from each record.
  - Masking, hashing and derived fields from the log type configuration are applied before sending.
  - Masking and hashing fail closed: if a rule cannot be applied, the whole field is withheld.
  - The Function App name is added as the log source (`s247agentuid`), and the event time as `_zl_timestamp`.
- **Disabled categories** are not sent, including through the general log type. If the list of disabled categories can't be read, the run is skipped, and the logs wait for the next run.
- **Logging:** unparseable records are counted but never written to the collector's logs.
- **Encryption:** data is sent over TLS 1.2 or later. Storage is encrypted at rest by Azure.

---

## 9. Self-update

Updates come from the GitHub releases of `UPDATE_CHECK_URL`, which the template sets to this repository.

**Before deploying a release, the updater checks:**
- the channel: pre-releases are refused unless `UPDATE_CHANNEL=prerelease`;
- the age: releases younger than `MIN_RELEASE_AGE_MINUTES` (60) are skipped;
- the package host must belong to the configured repository, over HTTPS;
- the package's SHA-256 must match the release's `version.json` (`REQUIRE_PACKAGE_SHA256`, on by default);
- the zip: archive integrity, every `.py` file parses, every `.json` file is valid, and the required files are present;
- the package's `VERSION` must equal the version the release advertises, so an older package can't pass the downgrade check.

Every run and post-update health check is recorded as an event.

**Release builds**
- Releases are built by GitHub Actions from the tagged commit.
- The workflow validates the version first and pins its actions to commit SHAs.
- It refuses to replace the package of a version that is already released.

**Controls**

| App setting | Effect |
|---|---|
| `SKIP_AUTO_UPDATE=true` | No automatic or manual update is applied. |
| `UPDATE_CHECK_URL` unset | No update checks at all. |
| `UPDATE_CHANNEL=stable` | The default: pre-releases are refused. |
| `PINNED_VERSION=x.y.z` | Deploys only that version, and only while it is the latest release. It does not fetch older releases. |

**Manual update:** `GET /api/check-update` reports what is available, and `POST /api/check-update?apply=1` applies it. Site24x7 admins can do the same from the collector's Settings tab.

**Known gaps** (section 12): Azure downloads the package again from its URL after the collector has verified it, and a failed update is not rolled back automatically.

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
| Site24x7 holds the default host key, which authorizes every collector endpoint (section 6) | Open | Admin-only, server-side actions; key verified at registration; encrypted at rest. A narrower key per action set is planned. |
| After verifying an update, the updater points `WEBSITE_RUN_FROM_PACKAGE` at the package URL, and Azure downloads it again | Open | The packages branch is written only by the release workflow, which refuses to replace a released version. Planned: deploy the verified bytes from the collector's own storage. |
| A failed update is not rolled back automatically | Open | Health check after the update; set `PINNED_VERSION` or `SKIP_AUTO_UPDATE`. |
| Masking and filter patterns come from Site24x7 and run with Python's backtracking regular expressions | Accepted | Patterns are defined by Site24x7, not by log content. A pathological pattern could slow processing. |
| Two collectors sharing one resource group (`resourceGroupName` override) can read each other's regional storage | Open | Each deployment gets its own resource group by default. Don't point two collectors at one group. |
| No Key Vault, Private Endpoint or customer-managed key by default | By design (Consumption plan, single template) | Key Vault references in app settings work for `SITE24X7_API_KEY`. A Premium plan with virtual network integration supports private networking. |
| Dependencies are pinned by version range, without hashes | Open | Releases build from `requirements.txt` in CI. A hashed lock file is planned. |

---

## 13. Common questions

| Question | Answer |
|---|---|
| Can it read data outside its own resource group? | It reads the resource inventory and log categories of the monitored subscriptions, and the log blobs in its own regional storage accounts. It does not read data of your other resources (disks, databases, other storage accounts). |
| Can it change my resources? | It manages diagnostic settings on resources in the monitored subscriptions (only its own, as described in section 3). It creates and deletes storage accounts in its own resource group, and writes its own app settings. It does nothing until an admin starts collecting (section 4). |
| Can it change permissions? | No. It has no role-assignment rights. |
| What if the GitHub repository were compromised? | An attacker would need to publish a release that passes the update gates (section 9) and wait for the age gate. You can turn updates off (`SKIP_AUTO_UPDATE=true`) or point `UPDATE_CHECK_URL` at your own fork. |
| What if Site24x7 were compromised? | With the Device Key, an attacker could send logs into your Site24x7 account. With the stored host key (section 6), they could call the collector's endpoints, for example change settings or remove diagnostic settings. They cannot reach your other Azure resources: the collector's identity is limited as described in section 3. |
| Can I check what is deployed? | Each release zip is built by GitHub Actions from a tagged commit, with its SHA-256 in `version.json`. Compare the running package with the tag. |
