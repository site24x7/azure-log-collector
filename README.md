# Azure Log Collector

Automatically collects diagnostic logs from **all** Azure resources across your subscriptions and forwards them to Site24x7 AppLogs.

**One-time setup, zero ongoing maintenance.** A timer trigger discovers new resources every 6 hours and configures them automatically.

## How It Works

```
Azure Resources ──► Storage Accounts (per region) ──► Function App ──► Site24x7 AppLogs
                          ▲                                │
                     Diagnostic Settings              Timer Trigger (6h)
                     (auto-configured) ◄──── discovers new resources
```

- **Storage Account per region** — Diagnostic settings stream logs to a storage account in the same region
- **Function App** — BlobLogProcessor polls every 2 min, parses logs, forwards to Site24x7
- **Web Dashboard** — Monitor status, manage filters, trigger scans, debug issues

---

## Quick Start

### Option A — Deploy to Azure (portal)

One-click ARM deployment. Requires you to be signed into GitHub with access to this repo (the template is published as a release asset).

| Version | Button / URL |
|---|---|
| **Pinned — v1.0.0 (current stable)** | [![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fgithub.com%2Fsite24x7%2Fazure-log-collector%2Freleases%2Fdownload%2Fv1.0.0%2Fazuredeploy.json) |
| **Latest stable** (auto-follows newest stable release) | [![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fgithub.com%2Fsite24x7%2Fazure-log-collector%2Freleases%2Flatest%2Fdownload%2Fazuredeploy.json) |

Raw URLs (if you prefer to paste directly into `Template spec deployment`):

- **Pinned v1.0.0:**
  `https://github.com/site24x7/azure-log-collector/releases/download/v1.0.0/azuredeploy.json`
- **Latest stable:**
  `https://github.com/site24x7/azure-log-collector/releases/latest/download/azuredeploy.json`

Pre-release track (alpha/beta/rc) is only available via explicit version tag in the raw URL, e.g.
`https://github.com/site24x7/azure-log-collector/releases/download/v1.0.1-beta1/azuredeploy.json`.

### Option B — Shell deploy

### Prerequisites

- Azure CLI (`az`) installed and logged in
- `jq` and `zip` installed
- One or more Azure subscriptions with resources to monitor

### Step 1: Clone & Configure

```bash
git clone https://github.com/site24x7/azure-log-collector.git
cd azure-log-collector/setup
cp config.env.example config.env
```

Edit `config.env`:
```bash
# REQUIRED — your Azure subscription ID(s), comma-separated
SUBSCRIPTION_IDS="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

# REQUIRED — your Site24x7 API token
SITE24X7_API_TOKEN="your-token-here"

# OPTIONAL — Azure region (default: eastus)
FUNCTION_APP_REGION="eastus"
```

### Step 2: Deploy

```bash
bash setup.sh
```

This takes 5–10 minutes and provisions:
- Resource group, storage account, Function App
- Managed Identity with Reader + Monitoring Contributor roles
- Initial resource scan and diagnostic settings configuration

At the end, you'll see the **Dashboard URL** — open it in your browser.

### Step 3: Open the Dashboard

The dashboard is served directly from the Function App:
```
https://<FUNCTION_APP_NAME>.azurewebsites.net/api/dashboard?code=<FUNCTION_KEY>
```

---

## Dashboard

| Tab | What it shows |
|-----|---------------|
| **Overview** | Subscriptions, regions, last scan details, errors |
| **Filters** | Ignore lists (subscriptions, RGs, locations, types, tags) + log type toggles |
| **Resources** | All configured resources with categories and storage accounts |
| **Debug** | System health, config validation, recent events, processing runs |

**Controls** (always visible):
- Toggle log processing, auto-scan, general log type, pipeline monitoring
- Trigger manual scan, check for updates, remove all diagnostic settings

---

## Teardown

```bash
cd azure-log-collector/setup
bash teardown.sh
```

Removes resource locks, cleans up diagnostic settings, then deletes the resource group.

## Cost Estimate

| Resource | Cost |
|----------|------|
| Function App (Consumption) | ~$0.20/million executions |
| Storage Account (per region) | ~$0.02/GB/month |
| **Total (typical)** | **~$5–10/month** |

No Event Hubs needed — storage-based polling eliminates the ~$11/region/month cost.

---

## Project Structure

```
├── setup/
│   ├── config.env.example    ← Configuration template
│   ├── setup.sh              ← One-click provisioning
│   ├── teardown.sh           ← One-click cleanup
│   └── azuredeploy.json      ← ARM template
├── function-app/
│   ├── VERSION               ← Semantic version (triggers CI/CD releases)
│   ├── shared/               ← Core Python modules
│   ├── BlobLogProcessor/     ← Timer: polls storage, forwards logs to S247
│   ├── DiagSettingsManager/  ← Timer: 6h resource scan + config
│   ├── Dashboard/            ← Web dashboard (single-page HTML)
│   ├── tests/                ← 217 unit tests (pytest)
│   └── ...                   ← 17 more endpoints (21 functions total)
├── testing/
│   ├── mock_s247_server.py   ← Mock S247 for local E2E testing
│   ├── test_e2e.py           ← End-to-end test suite
│   └── sample_blobs/         ← Sample Azure diagnostic log blobs
├── docs/
│   ├── architecture-document.md
│   ├── developer-guide.md
│   ├── troubleshooting-guide.md
│   ├── entra-id-logs.md           ← Tenant (Entra ID) log collection setup
│   └── security-overview.md       ← Customer-facing security brief
└── .github/workflows/
    └── release-function-app.yml  ← CI/CD: auto-release on VERSION bump
```

## Auto-Updates

The Function App can self-update from GitHub Releases.

### Basic setup

1. Set `UPDATE_CHECK_URL` app setting to `site24x7/azure-log-collector`
2. Bump `function-app/VERSION` and push to `main`
3. GitHub Actions creates a release with the deployable zip
4. The Function App detects the new version and deploys it on its next nightly run (3 AM UTC)

### Safety controls (app settings)

| Setting | Default | Purpose |
|---|---|---|
| `UPDATE_CHANNEL` | `stable` | `stable` refuses any alpha/beta/rc; `prerelease` accepts them. Use `prerelease` in test environments. |
| `PINNED_VERSION` | *(unset)* | If set (e.g. `1.2.3`), AutoUpdater only deploys this exact version. Use for freezing prod or rolling back. |
| `SKIP_AUTO_UPDATE` | `false` | `true` disables AutoUpdater entirely — emergency switch, no redeploy needed. |
| `MIN_RELEASE_AGE_MINUTES` | `60` | Refuses releases younger than N minutes. Gives you a grace window to delete a bad release before it propagates. |

### Separating alpha/beta from customer builds

Check GitHub's "Set as a pre-release" box when creating alpha/beta tags. With defaults:

- Customers (`UPDATE_CHANNEL=stable`) skip pre-releases automatically — they only see stable `/releases/latest`.
- Your test environment (`UPDATE_CHANNEL=prerelease`) picks up the newest release of any kind.

### Rollback

1. Delete or un-publish the bad release on GitHub (or mark it a pre-release).
2. Tag a new release with the previous known-good content, OR set `PINNED_VERSION=<known-good>` on prod.
3. Next AutoUpdater run reconciles to the pinned/latest-stable version.

Every AutoUpdater run writes audit events (`auto_update_run`, `auto_update_health_check`) visible via the debug-logs endpoint.

## Security

The collector runs entirely inside your Azure tenant, uses Managed Identity (no stored credentials), has least-privilege RBAC, and is open-source.

See **[docs/security-overview.md](docs/security-overview.md)** for a customer-facing security brief covering: trust boundary, RBAC, data path & residency, self-update controls, logging & auditability, uninstall, and answers to common security questions.

## Development

```bash
cd function-app
pip install -r requirements-dev.txt
python3 -m pytest tests/ -v     # 260 tests, ~4s
```

See [docs/developer-guide.md](docs/developer-guide.md) for detailed development setup.

## License

Internal — Site24x7 / Zoho Corporation
