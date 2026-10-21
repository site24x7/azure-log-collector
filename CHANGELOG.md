# Changelog

All notable changes to the Azure Log Collector are documented here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).
Release numbers follow [SemVer](https://semver.org/) — pre-releases use
`-alphaN`, `-betaN`, `-rcN` suffixes. The in-app updater correctly orders
`dev < alpha < beta < rc < final` for the same core version.

## [1.0.1-beta10] — 2026-10-21

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
  - Raw template: <https://github.com/site24x7/azure-log-collector/releases/download/v1.0.0/azuredeploy.json>
  - Portal button: <https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fgithub.com%2Fsite24x7%2Fazure-log-collector%2Freleases%2Fdownload%2Fv1.0.0%2Fazuredeploy.json>
- **Latest stable** (auto-follows newest non-prerelease tag)
  - Raw template: <https://github.com/site24x7/azure-log-collector/releases/latest/download/azuredeploy.json>

Release zip:
<https://github.com/site24x7/azure-log-collector/releases/download/v1.0.0/s247-function-app.zip>
