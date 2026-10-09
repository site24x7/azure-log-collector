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
| Fresh portal deployment | Published customer template and matching package deploy in East US and Central India; nested operations succeed, Oryx completes, authenticated health reports `deps_ok=true`, and expected functions register. |
| Repeated deployment | Same template, suffix and location reuse resources; no duplicate storage, Function App or forwarding destinations appear. |
| Name unavailable | Preflight rejects an unavailable name before provisioning, gives a naming override, and a fresh override succeeds. Availability-check errors never imply availability. |
| Existing collector upgrade | Start with the published stable version; upgrade through the actual supported updater path. Config, filters, storage destinations and processing checkpoints survive; new uniquely marked events arrive. |
| Rollback | Restore the known-good package using the tested deployment method and verify new events remain searchable. The current health check reports failures but does not automatically roll back. |
| Resource logs | A real Azure resource emits a marked diagnostic record, it reaches regional storage, and the same record is searchable in Site24x7 with correct category/resource/time fields. |
| Optional Entra logs | Verify AuditLogs end to end and each additional category actually advertised by the receiving server; unsupported categories must remain clearly unavailable. |
| Client onboarding | Test the deployed client link, prerequisites, Device Key/DC guidance, Azure handoff, deployment Outputs, dashboard/scan flow, ingestion confirmation, management and cleanup. Opening Azure is not deployment success. |
| Relay demo | Test both API queue/blob and actual log-upload blob forwarding. A local saved file, relay HTTP 200 or staging-blob acknowledgement does not establish AppLogs ingestion. |

## Publication gate

Prereleases remain publishable for candidate testing. A stable release is blocked
on automatic VERSION pushes. The release owner must manually dispatch the
release workflow with `stable_validation_confirmed=true` and
`validation_report_url=https://...` after the applicable checks pass. The evidence
link is included in the stable release notes. Forced replacement also requires
this approval.

This is an explicit human approval gate, not an automated execution of the live
tests or a verification of the linked report's claims. The release owner must
review evidence for the matching candidate. CI continues to enforce regression,
setup syntax, JSON and package/template VERSION pin checks. No stable release
should be approved solely because these automated checks are green.

## Current unresolved verification

The PR deployment template has passed Azure preflight; live ZipDeploy/Oryx
startup, fresh install, upgrade, rollback and ingestion are still pending.
Long-suffix naming requires the updated collector package and matching template
pin. Legacy URL-mode updater behavior and transition from old deployment methods
must be tested before customers receive the new stable version. The local demo
also requires an AppLogs-enabled build, actual upload/index services and the
queue/blob relay configuration.
