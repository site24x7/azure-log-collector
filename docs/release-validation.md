# Stable release validation

Passing unit tests and ARM validation does not establish production readiness.
The customer-facing template, exact code package, updater, and client onboarding
must be verified together against real Azure resources and the receiving
Site24x7 account. A prior Kudu deployment does not verify a new portal install.

## Required evidence

The release owner records the candidate version, source commit, ZIP SHA-256,
template revision, test date, Azure deployment operation IDs, regions and
receiving account/environment in an HTTPS-accessible report. Do not include
Device Keys, connection strings, function keys, or key-bearing dashboard URLs.
Use unique test-event markers and retain evidence of the events in AppLogs.

| Scenario | Pass condition |
|---|---|
| Fresh portal deployment | Published customer template and matching package deploy in East US and Central India; nested operations succeed, OneDeploy installs the package, authenticated health reports `deps_ok=true`, and expected functions register. |
| Repeated deployment | Same template, suffix and location reuse resources; no duplicate storage, Function App or forwarding destinations appear. |
| Name unavailable | Preflight rejects an unavailable name before provisioning, gives a naming override, and a fresh override succeeds. Availability-check errors never imply availability. |
| Existing collector upgrade | Start with the published stable version; upgrade through the actual supported updater path. Config, filters, storage destinations and processing checkpoints survive; new uniquely marked events arrive. |
| Rollback | Restore the known-good package using the tested deployment method and verify new events remain searchable. Check update confirmation and startup diagnostics; there is no automatic rollback. |
| Resource logs | A real Azure resource emits a marked diagnostic record, it reaches regional storage, and the same record is searchable in Site24x7 with correct category/resource/time fields. |
| Optional Entra logs | Verify AuditLogs end to end and each additional category actually advertised by the receiving server; unsupported categories must remain clearly unavailable. |
| Site24x7 cannot read collector API | Simulate timeout, denied key/non-2xx and malformed 200 response. The detail page shows collector API failure; cached healthy status cannot override it. Outbound health stays last-known/unknown, never inferred from inbound failure. |
| Collector cannot reach Site24x7 API/upload | Keep collector status API reachable, fail outbound API and upload paths independently. Live status shows API phase + HTTP status or last upload failure + observation time; empty runs do not establish recovery. A real successful upload clears the recorded upload failure. |
| Both paths fail | With no recent report/live snapshot, status becomes unavailable within the 26-hour historical-report policy (live failures show immediately); old data remains historical. Recovery preserves the server's true last registration time. |
| Shell install and removal | `setup.sh` and then `teardown.sh` run against a test subscription from Cloud Shell: setup creates the group, storage, Function App and roles and registers with Site24x7; teardown removes the diagnostic settings, role assignments and group, and refuses a group that holds other resources. |
| Client onboarding | Test the deployed client link, prerequisites, Device Key/DC guidance, Azure handoff, deployment Outputs, dashboard/scan flow, ingestion confirmation, management and cleanup. Opening Azure is not deployment success. |

## Publication gate

Prereleases remain publishable for candidate testing. A stable release is blocked
on automatic VERSION pushes. The release owner must manually dispatch the
release workflow with `stable_validation_confirmed=true` and
`validation_report_url=https://...` after the applicable checks pass. The evidence
link is included in the stable release notes.

This is an explicit human approval gate, not an automated execution of the live
tests or a verification of the linked report's claims. The release owner must
review evidence for the matching candidate. CI continues to enforce regression,
setup syntax, JSON and package/template VERSION pin checks. No stable release
should be approved solely because these automated checks are green.

## 1.1.3-beta1 validation (2026-11-18)

Verified in a test subscription:

- Update in place: a collector on 1.1.2 updated to 1.1.3-beta1 with Check for Updates, confirmed by
  the status check 20 seconds after the request.
- Device Key out of URLs: with a Site24x7 build that has the POST endpoints, both log type calls
  (`/applog/azure/logtype_supported`, `/applog/azure/logtype`) went as POST with the key in the body,
  with no GET fallback; 50 log types returned and the log type check passed. A server without them
  answers 400 or 404, which the collector meets with the earlier GET form.
- `/api/health` ignores `azure_test` and lists no resources.
- Rollback: `az functionapp deploy --src-url` with the 1.1.2 `released-package.zip` (README, Rollback)
  ran 1.1.2 within a minute. Azure CLI printed the JSON parsing error the README mentions; the install
  succeeded. Health, the Site24x7 calls, settings, filters, the configured resources and regional
  storage were kept. Updating forward to 1.1.3-beta1 again was confirmed in 41 seconds. After a manual
  rollback, `update_state` in `/api/status` still describes the later update until the next one.
- Shell install and removal (`setup.sh`, `teardown.sh`) on a new suffix, with automatic scans off so
  no resource was touched. The first run found three `setup.sh` faults, fixed in 1.1.3-beta2: no role
  was assigned (an Azure CLI warning was read as the managed identity ID), the Flex plan was not tagged
  (the plan ID is under `properties` in current Azure CLI output, and the tag was applied only when the
  app was created), and the script ended with exit code 1. With the fixes, two more runs on the same
  suffix assigned all four roles, tagged the plan and ended with exit code 0. `teardown.sh
  --clean-diag-settings` then removed the locks, the subscription role assignments and the resource
  group; no role assignment of the identity remained.

Not verified: uploads (the local upload server was off), Delete Collector, and end-to-end delivery.

## 1.1.2 validation (2026-11-17)

On a collector running 1.1.1 and updated in place to 1.1.2-beta1 (the same code):

- The update was applied from the in-app check and the new version was running within a minute.
- Log Collection switched off, then on again while the removal was running: the stored logs were
  kept (no containers emptied; pending records unchanged), the "off" was cleared from the status,
  and the queued scan put all 5 settings back with 0 errors.
- The shell uninstall checks (another collector's owner tag, setting destinations in another
  subscription) and the shell install's plan tagging are covered by offline tests with every Azure
  CLI call intercepted; a fresh shell install and uninstall were not run.

Not verified: rollback, as for 1.1.0.

## 1.1.1 validation (2026-11-17)

On a collector deployed from the v1.1.0 template and updated in place to 1.1.1-beta1 (the same code):

- The update was confirmed by the status check in under 2 minutes, and the settings were kept.
- No diagnostic setting was placed on another collector's resources, checked in Azure.
- Function apps got FunctionAppLogs; web apps got their own categories.
- Log Collection off then on: the settings were removed and restored by the next scan (21 resources, 0 errors).
- Delete Collector: the popup closed on confirm, the page showed the deleted state without a reload, and
  the resource group was removed; Remove From Site24x7 worked.

Not verified: rollback, as for 1.1.0.

## 1.1.0 validation (2026-11-16)

Verified on Flex Consumption collectors in a test subscription (candidates beta12 to beta15):

- Fresh install from the template, registration, first scan, and Scan now (21 resources configured, 0 errors).
- A second collector in the same subscription skips the first collector's resources.
- Upgrade in place (beta10 to beta12, beta10 to beta15), with settings and filters kept, and
  confirmation by the status check.
- Log Collection off (all settings removed), the toggles, Delete Collector (settings, lock and resource group
  removed), and Remove From Site24x7.
- Upload acknowledgement: the upload endpoint answered HTTP 200. This shows the upload was accepted,
  not that the records were indexed; end-to-end delivery (records searchable in AppLogs) is not
  recorded in this report.

Not verified: rollback to an earlier version. The updater never installs an older version; going back
means redeploying the earlier template.

