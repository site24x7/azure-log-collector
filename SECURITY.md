# Security Policy

## Reporting a Vulnerability

If you believe you have found a security vulnerability in the Azure Log
Collector, please report it privately to the Site24x7 security team.

**Do not file a public GitHub issue for security reports.**

### How to report

- Email: **security@site24x7.com**
- Include:
  - A description of the issue and its impact
  - Steps to reproduce (proof-of-concept if available)
  - The version (`function-app/VERSION` value) and your deployment region
  - Whether you believe the issue is being actively exploited

### What to expect

- Acknowledgement within **5 business days**
- A target remediation window communicated within **15 business days**
- A coordinated disclosure schedule once a fix is available
- Public credit in the release notes if you wish

## Supported Versions

| Version  | Supported |
| -------- | --------- |
| 1.0.x    | ✅ Yes    |
| < 1.0    | ❌ No (pre-release) |

## Out of Scope

- Vulnerabilities in Microsoft Azure or Site24x7 services themselves — please report those to the respective vendors.
- Issues that require an attacker to already have full administrator access to the customer Azure tenant.
- Findings from automated scanners that depend on undeployed default configurations.
