# Changelog

All notable changes to the Azure Log Collector are documented here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).
Release numbers follow [SemVer](https://semver.org/) — pre-releases use
`-alphaN`, `-betaN`, `-rcN` suffixes. The in-app updater correctly orders
`dev < alpha < beta < rc < final` for the same core version.

## [Unreleased]

### Fixed
- Refresh supported Azure log types on every scan, preserving the cached catalog
  during server outages; status/filter requests read the persisted catalog so
  changes are visible across Function App workers.
- Check Entra config/state persistence before reporting toggle success. Failed
  state writes attempt to restore the previous config; failed creation no longer
  enables a new category, and failed retries preserve an existing enabled category.
- Abort Entra toggles before mutation when snapshot reads fail; only confirmed
  BlobNotFound is treated as absent. Preserve server error details on retries of
  already-enabled categories without disabling their working config.
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
