# Security Overview — Azure Diagnostic Logs Collector

**Audience:** Customer security, compliance, and platform teams evaluating the Function App before deployment into their Azure tenant.

**One-line summary:** The collector runs entirely inside your Azure subscription, uses Managed Identity (no stored Azure credentials), has least-privilege roles scoped to read-only + diagnostic-settings management, and is open-source so every byte it runs is auditable.

---

## 1. Trust Boundary

```
┌────────────────────── Your Azure tenant ───────────────────────┐
│                                                                 │
│   ┌──────────────────────┐       ┌──────────────────────┐      │
│   │  Azure resources     │──►──► │  Diagnostic settings │      │
│   │  (VMs, AKS, SQL …)   │       │  (managed by us)     │      │
│   └──────────────────────┘       └──────────┬───────────┘      │
│                                              │ logs             │
│                                              ▼                  │
│   ┌──────────────────────┐       ┌──────────────────────┐      │
│   │  Function App        │◄───── │  Storage Accounts    │      │
│   │  (this project)      │       │  (per-region, ours)  │      │
│   └──────────┬───────────┘       └──────────────────────┘      │
│              │ outbound HTTPS                                   │
└──────────────┼──────────────────────────────────────────────────┘
               │
               ▼
   logc.site24x7.<dc>    ← the ONLY external endpoint the app contacts
   plus.site24x7.<dc>       with your data.
```

**Nothing in your environment phones home anywhere else.** The only outbound destinations are:

| Endpoint | Purpose | Protocol |
|---|---|---|
| `plus.site24x7.<dc>/applog/*` | Create log types, fetch config | HTTPS |
| `logc.site24x7.<dc>/upload/*` | Ship your log data | HTTPS |
| `management.azure.com` | Azure ARM API (stays inside Azure) | HTTPS |
| `api.github.com/repos/.../releases` | Check for updates (optional, disableable) | HTTPS |

You can add firewall / NSG rules allow-listing exactly these hosts.

---

## 2. Identity & Credentials

### What the app has
- **System-Assigned Managed Identity.** No Azure credentials are ever stored in code, config, or app settings. Azure mints short-lived tokens on demand.
- **One Site24x7 device key** (stored as an Azure app setting, encrypted at rest by Azure). Used to authenticate to the Site24x7 AppLogs API.

### What we never have
- No customer secrets leave your tenant.
- No service principal passwords or certificates.
- No network credentials.
- No long-lived Azure keys.

### Key rotation
- **Site24x7 device key:** update the `SITE24X7_API_KEY` app setting and Function App picks it up on the next invocation. `post_logs()` overrides the per-blob snapshot at runtime so rotation is instant.
- **Function keys** (for the management UI): rotate any time via Azure Portal or `az functionapp keys set`.
- **Managed Identity:** managed by Azure; no action needed.

---

## 3. Least-Privilege RBAC

The ARM template grants the Function App's Managed Identity **exactly three** roles — nothing more:

| Role | Scope | Why |
|---|---|---|
| `Reader` (`acdd72a7-3385-48ef-bd42-f606fba81ae7`) | Subscription | List resources so we know what to enable diagnostics on |
| `Monitoring Contributor` (`749f88d5-cbae-40b8-bcfc-e573ddc772fa`) | Subscription | Create/update `Microsoft.Insights/diagnosticSettings` on resources |
| `Contributor` (`b24988ac-6180-42a0-ab88-20f7382dd24c`) | **Resource group only** (`s247-diag-logs-rg`) | Manage the app's own storage accounts + self-update (`zipdeploy`) |

**What this explicitly does NOT grant:**
- No Owner, no `Microsoft.Authorization/*` (cannot change RBAC)
- No Key Vault access
- No access to data-plane secrets on any other resource
- No Contributor on your workload subscriptions — Contributor is scoped to our own RG

A resource-group **CanNotDelete lock** is applied at deployment so a misconfigured script cannot accidentally delete the storage accounts holding buffered logs.

---

## 4. Data Path & Residency

- **Ingest:** Azure resources stream logs into Storage Accounts provisioned **per-region inside your subscription**.
- **Buffer:** Logs sit in those storage accounts until the collector processes and ships them.
- **Egress:** Logs leave your tenant **only** over HTTPS to `logc.site24x7.<dc>` (where `<dc>` is the Site24x7 data center you chose — US / EU / IN / AU / CN).
- **No intermediate store.** We do not stage your data in our infrastructure before Site24x7.
- **TLS 1.2+ everywhere** (Azure Functions + Storage + Site24x7 all enforce).

### Data-center pinning
Site24x7 data centers are geographically distinct. Picking `www.site24x7.eu` at deploy time means logs are routed to Site24x7's EU DC and never touch other regions.

---

## 5. Self-Update — The Most Scrutinised Part

Because this Function App can update itself, we've built it to be **verifiable and controllable by you**, not us.

### Defense-in-depth pipeline
Before any new version is deployed, AutoUpdater runs:

1. **Opt-in channel filter** — by default (`UPDATE_CHANNEL=stable`), any alpha/beta/rc build is **refused**, even if GitHub is pointed at it by mistake.
2. **Pre-deploy package validation** (`validate_zip_package()`):
   - Archive integrity check
   - `ast.parse()` on every `.py` file — syntax errors refused
   - `json.loads()` on every `.json` file — malformed config refused
   - Required files must be present (`host.json`, `VERSION`, …)
3. **Age gate** — releases younger than 60 minutes (configurable) are deferred, giving you and us time to pull a bad release.
4. **Audit events** — every update run writes an `auto_update_run` event; every deploy writes an `auto_update_health_check` event pinging `/api/health` on the new build. Both queryable via App Insights / the debug bundle.

### Customer-controlled switches (no redeploy needed)

| App setting | Effect |
|---|---|
| `SKIP_AUTO_UPDATE=true` | Turns AutoUpdater off completely. You control when to update. |
| `PINNED_VERSION=1.2.3` | Only version 1.2.3 is ever deployed. Change it to roll forward/back. |
| `UPDATE_CHECK_URL=` *(unset)* | Skip update checks entirely. |
| `UPDATE_CHANNEL=stable` | Default. Pre-releases refused. |

### "I want to approve every update manually"
Set `SKIP_AUTO_UPDATE=true`. Invoke `POST /api/check-update` from your CI when you want to update; it returns release notes and a package URL. Only call the `/api/apply-update` endpoint when approved. (Available in v1.x.)

### Source & supply chain
- Every byte that runs is on GitHub: `github.com/site24x7/azure-log-collector`.
- Releases are built by GitHub Actions from a tagged commit; the workflow is in the repo (`.github/workflows/release-function-app.yml`).
- You can pin `UPDATE_CHECK_URL` to **your fork** and only release updates after your own review/signing.
- You can `diff` the running zip against the tagged commit anytime — nothing is obfuscated or minified.

---

## 6. Management Plane Security

The Function App exposes a small HTTP API for the dashboard (dashboard config, ignore-list, debug bundle).

- **Authentication:** Azure Functions `authLevel=function`. Every call requires a function key (Azure-managed, rotatable).
- **Authorization:** Each endpoint validates key scope and origin.
- **Input validation:** All request bodies are length-capped, type-checked, and sanitized before use.
- **No anonymous endpoints** other than `/api/health` (returns static "ok" — no data).
- **CSRF:** Same-origin policy + function key required; no session cookies.
- **Rate limiting:** Built-in Azure Functions throttling on Consumption plan. Premium/Dedicated customers can layer APIM / Front Door rate limits.

---

## 7. Data Handling by the Code Itself

- **Least retention:** Processed blobs are deleted within `SAFE_DELETE_MAX_AGE_DAYS` (default 7). Unprocessed / unconfigured blobs are aged out after `STALE_BLOB_MAX_AGE_DAYS`.
- **No disk writes** outside Azure Storage and Azure Functions' own temp directory (cleared between invocations on Consumption plan).
- **PII:** The collector does not inspect log content semantically. It parses JSON boundaries and forwards verbatim. Whatever Azure Diagnostic Settings emits is what Site24x7 receives.
- **Redaction:** If you need redaction before egress, run the collector on the Premium plan with a VNet-integrated egress proxy that masks fields. (The architecture supports it; we can help you configure.)

---

## 8. Logging, Auditability & Forensics

- **Application Insights** captures every invocation, exception, and custom audit event, inside your tenant.
- **Audit events** we emit: `auto_update_run`, `auto_update_health_check`, `log_audit` (all admin actions), `scan_started/scan_completed`, `config_changed`, `deprovision_attempted`, and per-blob `processing_result`.
- **Debug bundle export** — one click in the dashboard produces a JSON snapshot of all config + recent events, for compliance audits.
- **Tamper-evidence:** Audit events are written with conditional ETag writes — concurrent runs can't silently overwrite each other. (See `shared/debug_logger.py`.)

---

## 9. Uninstall / Data-Destruction

Run `setup/teardown.sh` or:

```bash
az group delete --name s247-diag-logs-rg --yes
az role assignment delete --assignee <function-mi-principal-id>
```

This removes:
- The Function App
- All regional storage accounts (and all buffered logs)
- All diagnostic settings the collector created on your resources
- All RBAC assignments

**No residue anywhere in your tenant**, and we retain nothing.

---

## 10. Known Limits & What We Need From You

Things we do not do (by design, and why):

| Not done | Rationale | Workaround |
|---|---|---|
| No Key Vault integration for the Site24x7 API key | Keeps deployment a single-step ARM template; Azure app settings are already encrypted at rest | If policy requires Key Vault, add a Key Vault reference (`@Microsoft.KeyVault(SecretUri=…)`) in app settings; works transparently |
| No Private Endpoints by default | Consumption plan doesn't support them | Upgrade to Premium plan + set `WEBSITE_VNET_ROUTE_ALL=1`; allow `logc.site24x7.*` through egress firewall |
| No customer-managed encryption keys (CMK) on storage | Not all Azure regions support it uniformly | Enable CMK on the managed storage accounts post-deploy; the app doesn't read/write encryption settings |
| No mTLS to Site24x7 | Site24x7 uses API-key auth over TLS | Not applicable — TLS 1.2+ is enforced on both sides |

---

## 11. Response to Common Security Questions

| Question | Answer |
|---|---|
| "Can it read data from outside its resource group?" | Read *inventory* (resource list + diagnostic settings) across the subscription, yes — that's how it knows what to collect. **Data-plane access is zero** — it never reads VM disks, SQL tables, Storage blobs of your resources, etc. |
| "Can it write / modify / delete my resources?" | Only `Microsoft.Insights/diagnosticSettings`, and only on resources you told it to manage. Contributor is scoped to its **own** RG. |
| "Can it elevate privileges?" | No. `Microsoft.Authorization/*` is not in any granted role. |
| "What happens if the GitHub repo is compromised?" | Multiple barriers: (1) pre-deploy validation refuses malformed packages, (2) the age gate delays adoption 60 min, (3) you can set `SKIP_AUTO_UPDATE=true` any time, (4) you can pin a known-good version, (5) you can fork the repo and use your fork. |
| "What happens if Site24x7 is breached?" | The worst-case damage is an attacker with your device key could inject garbage logs into your Site24x7 tenant. They cannot reach back into your Azure tenant — outbound-only flow, no inbound. Rotate the device key via Site24x7 portal. |
| "Is the code open-source / auditable?" | Yes. MIT-style internal license. Complete source on GitHub including CI, tests (260+), and release scripts. No binary blobs. |
| "How big is the attack surface?" | ~3000 LOC of Python + 4 HTTP endpoints behind function keys + one timer-driven update check. No inbound network path other than Azure's own authenticated control plane. |
| "Can you prove what's deployed matches what's on GitHub?" | Every deploy is a zip built by GitHub Actions from a tagged commit. You can download the release artifact, `unzip -l` it, and compare against the source tree at that tag. |

---

## 12. Compliance Notes

The architecture is consistent with:
- **SOC 2 Type II** — audit trail via App Insights + conditional-write audit events.
- **ISO 27001** — separation of duties (MI has no permission to alter RBAC); data-at-rest encryption; data-in-transit TLS 1.2+.
- **HIPAA / PCI** — recommend running on **Azure Premium plan** with VNet integration and Private Endpoints; enable Key Vault references for the device key; enable CMK on storage.
- **Data residency** — logs never leave the Site24x7 data center you pick at deploy time.

For SOC 2 / ISO audits we can provide: architecture document, this security overview, RBAC matrix, deployment evidence, and a signed attestation of the SDLC (tests, code review, release process).

---

## 13. Contact

- Security concerns / responsible disclosure: *(your security contact)*
- Repo: `github.com/site24x7/azure-log-collector`
- Issue tracker: GitHub Issues on the same repo
