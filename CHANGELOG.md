# Changelog

All notable changes to the Azure Log Collector are documented here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).
Release numbers follow [SemVer](https://semver.org/) — pre-releases use
`-alphaN`, `-betaN`, `-rcN` suffixes. The in-app updater correctly orders
`dev < alpha < beta < rc < final` for the same core version.

## [1.0.1] — 2026-10-28

First stable release of 1.0.1, with the same code as 1.0.1-rc1. Compared with
1.0.0 it runs on the Flex Consumption plan with Python 3.13, registers with
Site24x7 and supports its admin actions, delivers logs at least once with exact
checkpoints, and installs updates only from verified packages (automatic
installs are off by default). See the pre-release sections below for details.
1.0.0 collectors run on the older Consumption plan and cannot be updated in
place; deploy 1.0.1 instead.

## [1.0.1-rc1] — 2026-10-28

### Fixed
- `PINNED_VERSION` older than the installed version no longer installs the
  older release: a pin only freezes the collector and never downgrades
  (`action: pinned_older`).
- `GET /api/settings` reports the values the collector uses when a setting is
  unset (automatic scans on, 7 days of storage protection), instead of off and 1.

### Documentation
- Corrected the README, architecture, developer, troubleshooting, security and
  Entra ID documents against the code: tagged template URLs, function and
  endpoint names, schedules, registration retries, update controls and the
  test instructions. Removed instructions for test programs that are not in
  the repository.

## [1.0.1-beta15] — 2026-10-26

### Changed
- Automatic updates are off by default (`AUTO_UPDATE_ENABLED=false`, set by
  the template). The daily AutoUpdater only checks and records what is
  available; an admin installs it from Site24x7 (Update Collector). Set
  `AUTO_UPDATE_ENABLED=true` to let the collector install updates on its own.
  `SKIP_AUTO_UPDATE` still turns every update off.

### Security
- The release workflow can no longer replace a published release: the
  `force_release` option, which deleted and recreated an existing release and
  its tag, is removed. A run for a published version stops; a tag without a
  release is refused; only an unpublished draft left by a failed run is rebuilt.
- Documentation corrected: `PINNED_VERSION` freezes updates but cannot roll
  back (recovery is reinstalling the previous release's package); review before
  collecting applies to portal deployments with `confirmAfterDiscovery` on, not
  to `setup.sh`; delivery is at least once; permanent upload refusals are
  dropped, not retried.

### Fixed
- Pause/resume uses shared storage state without restarting the Function App or
  waiting for registration/key lookups before answering.
- Read operational health directly from the collector without adding periodic
  registration writes; retain registration on scans, restarts and key changes.
- Expose a closed live operational snapshot so Site24x7 can distinguish an
  unreachable collector API from outbound Site24x7 API or upload failures.
- Record actual upload outcomes separately from processing runs; empty runs do
  not clear failures. Stale reports no longer leave the page appearing Running.

## [1.0.1-beta14] — 2026-10-23

### Changed
- A portal form (`setup/createUiDefinition.json`): labelled fields ("Resource
  group name (optional)", "Deployment suffix (optional)"), a password box for
  the Device Key, a data center list, Yes/No choices, and only the regions that
  offer Flex Consumption. The Site24x7 page opens the portal with it for
  releases from 1.0.1-beta14 on.
- Log types are shown with Azure's own category names (`DeveloperPortalAuditLogs`
  rather than the catalog's `developerportalauditlogs`): scans save the names
  they see in `category-names.json`, and `GET /api/disabled-logtypes` returns
  them as `category`.
- Application Insights stores its data in a Log Analytics workspace the
  template creates in the collector's own resource group
  (`s247-diag-func-<suffix>-logs`: 30-day retention, 1 GB daily cap). Before,
  Azure created a workspace with no cap in a separate `ai_..._managed` resource
  group, which deleting the collector's group left behind.

### Fixed
- A batch Site24x7 refuses for good (HTTP 400, 404, 415, 422) is dropped and
  recorded, and the upload goes on with the next batch. Before, it was retried
  on every run and tripped the shared circuit breaker, which paused every
  upload. A payload too large (413) is split in halves first.
- A refused Device Key (401/403) is still retried, since every upload fails
  alike, and the status now reports it (`upload_refused`).
- A broken log-type configuration no longer trips the circuit breaker; only
  that blob waits.
- Checkpoint progress avoids resending records already saved as handled; delivery
  remains at least once after interrupted saves or unsplittable block ranges. Checkpoints
  record the exact position past what was handled, inside a block or an
  append envelope too (`skip_records`), and single-shot blobs remember how
  many records of that version were sent. Block positions are used only when
  every block parses on its own into exactly the records of the whole range.
- Concurrent checkpoint saves merge by whole-entry progress, so a resume
  position is never mixed with an older one.
- An upload never runs past the run's deadline: each request's timeout is
  capped to the time left, and none starts with less than 5 seconds left.
- `dropped` counts records only. Blobs and blocks too large to read are
  counted as `skipped_parts` (status: `recent_skipped_parts`).

## [1.0.1-beta13] — 2026-10-22

### Changed
- Deployment form: `startCollectingAtOnce` is now `confirmAfterDiscovery`
  (default `true`: after the first resource scan, the collector waits for an
  admin to confirm in Site24x7). `generalLogTypeEnabled` defaults to `false`.
  The resource group and suffix descriptions are shorter, start with
  "Optional", and use no angle brackets (the portal showed them as `&lt;`).

## [1.0.1-beta12] — 2026-10-22

### Changed
- The filter choices (resource groups, regions, resource types, tags,
  subscriptions) come from an inventory the resource scan saves
  (`resource-inventory.json`, values only). `GET /api/ignore-list` no longer
  lists the subscriptions and checks every resource type on each request, which
  took close to a minute in a mid-sized subscription. It returns the time of the
  scan the choices came from (`available_updated_at`) and no per-resource list.
- The template installs the release's own `released-package.zip` asset.
  OneDeploy follows GitHub's download redirect, so the `packages` branch is no
  longer published. The release workflow creates the release as a draft with all
  its files, publishes it, then checks that the template's package address serves
  the released zip.
- Less logging to Application Insights: the Azure SDK's per-request lines (URL,
  headers) are kept to warnings, status requests log at debug, and host
  categories default to warnings.

## [1.0.1-beta11] — 2026-10-22

### Changed
- Hosting moves to the **Flex Consumption** plan (`FC1`, 2,048 MB instances, at
  most 10 instances). Azure retires Linux Consumption on 30 September 2028 and
  gives it no new language versions. Flex Consumption has no in-place migration,
  so collectors from earlier betas must be deployed again.
- Python 3.13 (Flex Consumption supports 3.11 only until October 2027). The
  locked dependencies are unchanged; cffi, charset-normalizer and markupsafe use
  their CPython 3.13 wheels.
- The template installs the release package with OneDeploy into a deployment
  container in the collector's storage account (`app-package-<function app>`),
  from the release's `releases/download/<tag>/released-package.zip` asset (the file name Flex Consumption
  documents; each release publishes it next to `s247-function-app.zip`).
  The settings Flex Consumption rejects are gone (`WEBSITE_RUN_FROM_PACKAGE`,
  `FUNCTIONS_WORKER_RUNTIME`, `FUNCTIONS_EXTENSION_VERSION`,
  `WEBSITE_CONTENTAZUREFILECONNECTIONSTRING`, `WEBSITE_CONTENTSHARE`).
- Updates install the verified copy with OneDeploy instead of switching
  `WEBSITE_RUN_FROM_PACKAGE`; the link to the copy is read-only and expires
  after 6 hours (it was valid for 10 years). `S247_PREVIOUS_PACKAGE` is no
  longer kept: every release keeps its own package asset; the `packages` branch is no longer published.
- Application Insights is connected with `APPLICATIONINSIGHTS_CONNECTION_STRING`
  instead of the retired instrumentation key.
- The template first checks that the chosen region offers Flex Consumption, so
  preflight rejects any other region before a resource group, storage account
  or lock is created. The resource group lock is created last, after the app,
  its package and its roles, so a failed deployment leaves no locked partial
  group. CORS is set in the app's own `siteConfig`, and the `dashboardUrl`
  output uses the app's real host name.
- An update is reported as submitted, not done: the new version records
  `confirmed_at` on its first run, a refused install records `failed_at`, a
  second update waits while one is in progress (30 minutes), and nothing is
  installed when the state cannot be recorded. The post-update health check that
  ran in the old (replaced) process is removed.
- `setup/setup.sh` refuses to reuse an existing Function App that is not Flex
  Consumption on Python 3.13, instead of updating its code in place.
- `setup/setup.sh` creates a Flex Consumption app and deploys with a remote
  build; the Oryx settings and the Kudu ZipDeploy fallback are removed.
- CI runs on Python 3.13 with `actions/checkout` v7.0.1 and `actions/setup-python`
  v7.0.0 (Node.js 24) on `ubuntu-24.04`.

### Added
- Updates are refused when the new version cannot load: before installing, the
  collector imports every function and shared module of the package in a
  separate process with only the package's own dependencies (a missing
  dependency, broken import or build for another Python is caught). The release
  workflow runs the same check on the built zip, so such a package is never
  released.

### Fixed
- An invalid escape sequence in the dashboard's script (`\.` in a regular expression), which Python
  3.12+ reports as a SyntaxWarning.

## [1.0.1-beta10] — 2026-10-21

### Added
- Review before collecting. A new deployment only discovers resources and
  reports what it would configure (resources, regions, resource types); it
  creates no storage accounts, Site24x7 log types or diagnostic settings until
  an admin reviews the exclusions and log categories and starts collecting
  (Site24x7 page, or the dashboard's Start Collecting button;
  `POST /api/start-collecting`). The approval is stored in a blob, so it does
  not restart the app. The template parameter `startCollectingAtOnce=true`
  skips the review; collectors installed before this version keep collecting.

- Site24x7 now gets its own function key, `site24x7`, on just the endpoints it
  calls, instead of the Function App's default host key. It no longer opens
  the dashboard or debug data, and the customer's own keys are independent of
  it. `SITE24X7_KEY_EXCLUDE` keeps chosen functions (e.g.
  `RemoveDiagSettings,CheckUpdate`) out of Site24x7's reach; `*` gives
  Site24x7 no key (and the collector stops reporting). Because the resource
  group lock forbids deleting keys, an excluded function's key is overwritten
  with a value nobody holds.

- Updates run from the collector's own copy of the verified package (a
  read-only link to a blob in its storage account), so Azure loads exactly the
  bytes whose SHA-256, structure and version were checked; before, Azure
  downloaded the release URL again after the check. The previous package
  address is kept in `S247_PREVIOUS_PACKAGE`, and an update counts as done only
  when the new version confirms on its first run (`update_state` in
  `/api/status`).

- Storage accounts carry an `s247-collector` tag naming the collector that
  created them. Collectors sharing a resource group no longer read, adopt or
  delete each other's accounts; accounts created before the tag are
  recognised by the name this collector generates, and tagged on the next scan.
- A diagnostic setting with this collector's name that sends somewhere other
  than the collector's own resource group is someone else's: it is never
  overwritten or deleted.
- Regional storage names no longer cut a short suffix off in long region names
  (`switzerlandnorth` + `alpha` / `beta` gave the same name); names created by
  earlier versions are still recognised.

- Log pipeline: uploads go in order and stop at the first failed batch, and
  append blobs checkpoint exactly past what was sent, so a partial failure no
  longer re-sends records that arrived; uploads also stop at the run's deadline
  and are capped at about 4 MB per batch. A quiet block blob (no change for
  90 minutes) has its last block read too (it used to be lost); a backlog over
  50 MB is read in bounded steps instead of stalling; oversized blobs and
  blocks are recorded and skipped. Records that cannot be parsed count as
  dropped. Checkpoint clean-up survives the merge on save; circuit-breaker
  failures are saved between runs; timestamps with an offset (`+05:30`) are no
  longer read as UTC.

- Scans: once a day each tracked diagnostic setting is checked in Azure, so
  one deleted or changed by hand is repaired (before, tracking was trusted
  forever). Azure changes still running at the scan deadline finish (up to
  45 s) and are recorded before the scan ends. An exclusion or category change
  made while a scan runs queues another scan, so the running one cannot undo it.
  App-setting changes are read back and written again if a concurrent change
  lost them, and an unchanged value is not written (no needless restart).

### Fixed
- Saving an include-tag exclusion (e.g. `env=prod`) removed the diagnostic
  settings of every configured resource, including the ones carrying the tag.
  Only rules decided by the resource ID (resource IDs, subscriptions, resource
  groups) now remove settings at once; location, type and tag changes queue a
  scan, which has each resource's details.
- Exclusion lists set to `null` were accepted and made every later scan fail;
  they are now rejected, and the filter tolerates them.
- A scan that cannot read the exclusion list stops instead of treating it as
  empty (which configured excluded resources).
- `{"enabled": "false"}` turned processing / the general log type on; these
  toggles and the settings endpoint now require real JSON booleans and
  integers.
- Saves that failed were reported as successful: disabling / enabling log
  categories, exclusion updates and their clean-up now report the failure, and
  a setting that could not be removed keeps its record.
- Disabling a log category no longer rewrites tracking for resources that still
  have other categories, so re-enabling it needs no rediscovery.
- Remove All refuses while a scan runs, turns automatic scans off before
  removing anything (and stops if that fails), counts failed lookups as errors,
  and clears only the records of settings confirmed gone.
- Removing settings no longer forgets a record when the legacy-named setting
  could not be looked up (it may still export). Configuration writes read
  their blob strictly, so an unreadable blob is never taken for "nothing to
  change", and the scan lock is only reported as taken when its write was
  committed.
- A scan request that finds another scan (or Remove All) running is retried by
  the queue every 5 minutes instead of being dropped, so an exclusion change
  that asked for a scan is always applied. Remove All holds the scan lock for
  the whole removal.
- Exclusion changes compare tag rules exactly (tag keys are case-sensitive),
  and the dashboard reports settings that could not be removed and scans
  started to apply location, type and tag rules.
- A disabled Entra ID category was still sent through the general log type;
  it is now skipped (and Entra categories wait for the next run when their
  state cannot be read).

### Changed
- An upload that fails (Site24x7 unreachable, timeout, rejected request) now
  counts as **retrying**, not dropped: the collector keeps its read position and
  sends those records again on the next run, so nothing is lost. Before, every
  failed attempt counted as dropped, so a short outage looked like tens of
  thousands of lost records. **Dropped** now means records that will never be
  sent (unreadable records, files too large to read). A blob that fails with a
  processing error is listed in the run's errors and read again next run.
- Registration reports `pending_records`: the records the latest run could not
  send. It goes back to 0 as soon as uploads work again.
- The dashboard checks the collector's status every 15 seconds while its tab is
  visible (it paused while hidden), so a scan started from Site24x7 or by the
  timer shows its progress there too. The run history shows retrying and
  dropped separately.
- The collector also registers with Site24x7 on a worker's first processing run
  (restart, redeploy, new instance) and whenever its function key changes; the
  key is read at most every 15 minutes, also while processing is paused. A
  rotated key used to reach Site24x7 only with the next scan.
- Pause / resume waits at most 10 seconds for its report to Site24x7 before
  answering, so the answer reaches Site24x7 within its 30-second limit. The
  setting is saved first; a slow report is sent by the next scan or timer run.

## [1.0.1-beta9] — 2026-10-20

### Added
- Registration sends the monitored subscriptions' display names (one ARM list
  call with the managed identity; kept within Site24x7's 4000-character status
  limit) and `recent_since`, the start of the runs the recent counts cover.
- Each deployment suffix gets its own resource group,
  `s247-diag-logs-<suffix>-rg`; installs without a suffix keep
  `s247-diag-logs-rg`. The new `resourceGroupName` template parameter redeploys
  a collector in its existing group.

### Changed
- Logs are sent with the Function App name as their source (`s247agentuid`)
  instead of the resource group, so one collector's logs can be queried on
  their own. Outside Azure the resource group is still used.
- Diagnostic settings are per collector: `s247-diag-logs-<suffix>` (installs
  without a suffix keep `s247-diag-logs`). A collector removes its own old
  shared-name setting before writing the new one and restores it if that
  fails; it never re-points or deletes another collector's setting, and keeps
  its record when a delete fails.

### Fixed
- With automatic scans turned off the collector still reports to Site24x7 on
  its schedule, so it no longer looks stale there and key changes keep syncing.
- Dashboard update flow: refusals show the updater's reason instead of
  "unknown error"; one update at a time; the version badge clears after an
  update; the Check Update button no longer stays on "Working...".
- Pausing/resuming log processing, the general log type and other settings
  report a failure when Azure did not save the app setting (they returned
  success before); after pause/resume the collector reports its new state to
  Site24x7 straight away.

## [1.0.1-beta8] — 2026-10-15

### Added
- `COLLECTOR_RESOURCE_ID` app setting (set by the template) so registration uses
  the collector's own subscription even when it monitors others; the value is
  normalised to canonical ARM casing before it is sent.
- Dashboard banner when it is opened with the Dashboard-only function key (its
  data calls get 401), naming the host key to use instead.

### Fixed
- A rejected Device Key or wrong data centre is reported as such (scan status,
  Degraded system status, debug log, connectivity test) instead of "no log
  types"; the connectivity test calls the API host the collector really uses.
- The connectivity check treats any non-error log-type lookup as reachable, so
  a healthy API is no longer shown as failing.
- Dashboard and HTTP API state no longer goes stale: cached configuration now
  expires after 30 seconds in every worker (the Resources tab could miss
  resources configured after the worker started).
- Credentials echoed back in error bodies are scrubbed from logs and messages.

## [1.0.1-beta7] — 2026-10-14

### Fixed
- Stop a restart loop present since v1.0.0: every scan wrote the
  `LAST_SCAN_TIME` app setting, which restarts the Function App, and
  `runOnStartup` then started another scan — roughly every 4-5 minutes, with
  the dashboard showing "Unreachable" during each restart. The last scan time
  now lives only in the scan state, and a start-up run is skipped when a scan
  finished within `MIN_SCAN_INTERVAL_MINUTES` (default 30). The first scan
  after a fresh deploy still runs.

## [1.0.1-beta6] — 2026-10-14

### Added
- Register the collector with Site24x7 after every discovery scan (scheduled
  and manual): Function App URL, ARM resource ID, region, version, a read-only
  status summary and the function key, read fresh from ARM so a rotated key
  re-syncs on the next scan. Keys travel only in the POST body. Site24x7 stores
  the key encrypted and lists every collector on the Deploy Diagnostic Logs page.
- "Download Log File" on the dashboard (`/api/debug?download=log`): a readable
  log for Site24x7 support with keys, SAS tokens, connection strings and bearer
  tokens removed. All debug events, audit entries and debug downloads are now
  redacted.

### Fixed
- Portal installs: the deployment suffix is lowercased and stripped of `-`,
  `_`, `.` and spaces, so `-Oct14` no longer produces invalid storage names.
- A region with storage accounts left over from an earlier failed install now
  uses this deployment's own account, and diagnostic settings that were pointed
  at another account are re-pointed on the next scan.

## [1.0.1-beta5] — 2026-10-14

### Fixed
- Read Azure Append Blobs incrementally using acknowledged byte offsets instead
  of resending the entire hourly file whenever it grows. Preserve incomplete
  JSON/UTF-8 tails for the next poll, bound read sizes, and retain checkpoints
  when uploads fail. Merge offsets without regression and reset for recreated
  blobs. Unchanged legacy full-read checkpoints migrate without redelivery;
  a legacy blob changed before migration may be replayed once.
- Resolve configured date-field aliases from parsed fields before falling back
  to raw JSON, so Azure `time` mapped to `Time` supplies the event timestamp.
- Skip and count a malformed append-blob record that is already newline
  terminated instead of stalling every later record in that blob; invalid UTF-8
  no longer shifts byte offsets.
- Walk every chunk when migrating an unchanged legacy checkpoint, so blobs larger
  than one read are not resent past the first chunk.
- Parse Azure ISO-8601 times with 0-7 fraction digits and UTC offsets, and omit
  `_zl_timestamp` rather than sending epoch 0 when the time cannot be parsed.

## [1.0.1-beta4] — 2026-10-14

### Fixed
- Match Azure JSON field names case-insensitively, including nested properties
  and JSON-object fields. Preserve APIM `apiId`, `responseCode` and other
  camel-case values when templates use PascalCase names. Exact keys take priority;
  genuinely absent fields remain absent. Historical records require replay.
- Require explicit live-validation approval and an HTTPS evidence report before
  publishing stable releases; prerequisite CI alone no longer permits publication.
- Authenticate updater health checks with a function key and require an alive JSON
  response with healthy dependencies, rather than treating any HTTP 200 as success.
- Add an optional stable deployment suffix for fresh-install storage-name
  collisions, preserving legacy names when omitted. Longer suffixes use compact
  seed/tenant names and hash the full suffix and region for other storage accounts.
  Setup preflight checks host, seed and tenant names and stops on check failures.
- Deploy Linux Consumption code through ZipDeploy with remote build instead of
  setting WEBSITE_RUN_FROM_PACKAGE to a redirecting GitHub release URL.
- Refresh supported Azure log types on every scan, preserving the cached catalog
  during server outages; status/filter requests read the persisted catalog so
  changes are visible across Function App workers.
- Check Entra config/state persistence before reporting toggle success. Failed
  state writes attempt to restore the previous config; failed creation no longer
  enables a new category, and failed retries preserve an existing enabled category.
- Abort Entra toggles before mutation when snapshot reads fail; only confirmed
  BlobNotFound is treated as absent. Preserve server error details on retries of
  already-enabled categories without disabling their working config.
- Initialize a missing config container during strict snapshot reads and retry
  the blob read, including concurrent startup by multiple workers. Preserve
  legacy empty-config reads while rejecting empty mutation snapshots.
- Preserve the scan lock, last completed scan, connectivity, and previous results
  when publishing preliminary discovery progress.
- Shell setup now writes `SITE24X7_API_KEY`, accepts legacy `SITE24X7_API_TOKEN`
  config files, and rejects missing/placeholder keys before provisioning.
- Entra status now says "Ready to receive" rather than implying verified ingestion.

## [1.0.0] — 2026-05-14 — First official release

Inaugural release of the Site24x7 Azure Log Collector. Production-hardened
code with full self-update, per-region storage account polling, and
one-click ARM deployment.

### Included
- Storage Account polling pipeline (per-region SAs, BlobLogProcessor timer)
- Site24x7 AppLogs upload with per-DC API + upload domain routing
- Auto-discovery via scheduled scans + on-demand `TriggerScan`
- Function-key-authenticated dashboard + management API
- Self-update (`AutoUpdater`) with channel filter, version pinning,
  release-age gate, zip validation, post-deploy health check
- Block-list-based checkpointing for append-blob safety
- ETag-based optimistic concurrency on all mutable blob state
- Storage management policy for blob retention (default 7 days)
- ARM template (`setup/azuredeploy.json`) for one-click "Deploy to Azure"

### Deploy URLs
- **Pinned (v1.0.0)**
  - Raw template: <https://raw.githubusercontent.com/site24x7/azure-log-collector/refs/tags/v1.0.0/setup/azuredeploy.json>
  - Portal button: <https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fsite24x7%2Fazure-log-collector%2Frefs%2Ftags%2Fv1.0.0%2Fsetup%2Fazuredeploy.json>
- **Stable v1.0.0** (pinned tag)
  - Raw template: <https://raw.githubusercontent.com/site24x7/azure-log-collector/refs/tags/v1.0.0/setup/azuredeploy.json>

Release zip:
<https://github.com/site24x7/azure-log-collector/releases/download/v1.0.0/s247-function-app.zip>
