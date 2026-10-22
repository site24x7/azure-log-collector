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

### Storage-name availability and repeat deployments

Azure storage names are globally unique. An account can be unavailable even
when it is absent from your subscription. Repeating the same deployment or
changing its deployment name does not change the default subscription-derived
suffix.

For existing deployments, leave `deploymentSuffix` empty to preserve legacy
names, or keep the exact suffix previously chosen. For a **fresh installation**,
set `deploymentSuffix` in the Azure template parameters to a new 12–13 character
lowercase alphanumeric value. The resolved suffix is included in deployment
Outputs; save it and reuse it for updates. Shell setup uses `DEPLOYMENT_SUFFIX`
in `config.env` for the same purpose. Do not change a live collector's suffix,
location, or deployment method as an upgrade. Each suffix gets its own resource
group, `s247-diag-logs-<suffix>-rg`; installs without a suffix keep
`s247-diag-logs-rg`. To redeploy an existing collector in place, set the
`resourceGroupName` parameter to the group it already uses.

Check an explicit ARM suffix before deployment (Azure CLI login required):

```bash
python3 setup/check-storage-names.py --suffix <chosen-suffix> --region centralindia \
  --subscription <subscription-id>
```

The check is read-only: it checks host, seed and optional tenant storage names,
allows reuse of tagged collector accounts in the same resource group/location,
and fails if availability or ownership cannot be verified. Availability can
change after the check; Azure's deployment validation remains authoritative.
Shell setup performs these checks before provisioning infrastructure.

Suffixes of six characters or fewer retain legacy names. Longer suffixes keep
the entire suffix in compact host/seed/tenant names; other regional names hash
the full suffix and region into at most 24 characters. This requires the
matching updated collector code—do not combine a new long suffix with an older
collector package that still truncates regional names.

The template creates a **Flex Consumption** Function App (Python 3.13) and installs
the release package with OneDeploy. The package already contains its Python
dependencies, so nothing is built in Azure. The template installs the release's
own `released-package.zip` asset (the file name Flex Consumption documents for
OneDeploy; OneDeploy follows GitHub's download redirect). A successful ARM
validation is not proof of code startup; verify `/api/health` and actual
ingestion after deployment.

**Which key opens the dashboard.** The deployment's `dashboardUrl` output is the
address without a key (keys are never written to deployment outputs). Open it with
the Function App's **host** key named `default` (App keys > Host keys > default).
The Dashboard function's own key (Functions > Dashboard > Function keys), also
named `default`, opens only the page: every data request it makes returns 401.
Avoid `_master`, which also grants key management.

## Quick Start

### Option A — Deploy to Azure (portal)

One-click ARM deployment. The public template is published as a release asset; no GitHub sign-in is required.

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

# REQUIRED — your Site24x7 Device Key
SITE24X7_API_KEY="your-device-key-here"

# OPTIONAL — Azure region (default: eastus)
FUNCTION_APP_REGION="eastus"
```

### Step 2: Deploy

```bash
bash setup.sh
```

This takes 5–10 minutes and provisions:
- Resource group, storage account, Function App
- Managed Identity with Reader + Monitoring Contributor (monitored subscriptions) and
  Contributor (the collector's own resource group)
- Initial resource scan and diagnostic settings configuration

Deployments from the ARM template start in **review mode**: the collector only discovers
resources until an admin reviews the exclusions and log categories in Site24x7 and clicks
**Start Collecting**. Set the template parameter `startCollectingAtOnce=true` to skip that.

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

Works only on the deployment's subscription and a resource group tagged
`managed-by=s247-diag-logs`: removes its locks and the identity's subscription role
assignments, then deletes the resource group. Add `--clean-diag-settings` to also remove this
collector's diagnostic setting from resources where it sends to this deployment's storage
(or use **Remove Diagnostic Settings** in Site24x7 / the dashboard first).

## Cost Estimate

| Resource | Cost |
|----------|------|
| Function App (Flex Consumption, 2,048 MB instances) | Billed per GB-second while functions run, plus executions; see [Azure Functions pricing](https://azure.microsoft.com/pricing/details/functions/). Grows with log volume. |
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
| `PINNED_VERSION` | *(unset)* | If set (e.g. `1.2.3`), only that version is deployed, and only while it is the latest release (older releases are not fetched). |
| `SKIP_AUTO_UPDATE` | `false` | `true` applies no update at all, automatic or manual — emergency switch, no redeploy needed. |
| `MIN_RELEASE_AGE_MINUTES` | `60` | Refuses releases younger than N minutes. Gives you a grace window to delete a bad release before it propagates. |

### Separating alpha/beta from customer builds

Check GitHub's "Set as a pre-release" box when creating alpha/beta tags. With defaults:

- Customers (`UPDATE_CHANNEL=stable`) skip pre-releases automatically — they only see stable `/releases/latest`.
- Your test environment (`UPDATE_CHANNEL=prerelease`) picks up the newest release of any kind.

### Rollback

1. Delete or un-publish the bad release on GitHub (or mark it a pre-release), so the previous
   stable release is the latest again.
2. Or release the known-good content as a new version (a released version's package is never
   replaced).
3. A collector that cannot start after a bad update cannot update itself. Install the
   previous version's package again (Flex Consumption keeps no earlier packages):

   ```bash
   az functionapp deploy -g <resource-group> -n <function-app> --type zip \
     --src-url https://github.com/site24x7/azure-log-collector/releases/download/v<previous-version>/released-package.zip
   ```

   Azure CLI 2.79 may then print a JSON parsing error although the install
   succeeded; check `/api/health` or the app's deployment status.

Every AutoUpdater run writes an audit event (`auto_update_run`) visible via the debug-logs endpoint; the new version records `confirmed_at` in `update_state` (`/api/status`) on its first run.

## Security

The collector runs inside your Azure tenant: its managed identity for ARM, storage account keys for
its own storage, and your Site24x7 Device Key. Site24x7 holds a dedicated `site24x7` function key,
valid only on the endpoints it calls, so admins can run the collector's actions from Site24x7.

See **[docs/security-overview.md](docs/security-overview.md)** for a customer-facing security brief covering: trust boundary, RBAC, data path & residency, self-update controls, logging & auditability, uninstall, and answers to common security questions.

## Development

```bash
cd function-app
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q     # ~600 tests, under 10 s
```

See [docs/developer-guide.md](docs/developer-guide.md) for detailed development setup.

## License

Internal — Site24x7 / Zoho Corporation

Stable publication requires manual approval with live-validation evidence.
See [Stable release validation](docs/release-validation.md) for fresh-install,
repeat-deployment, upgrade, rollback, ingestion and client-workflow requirements.
