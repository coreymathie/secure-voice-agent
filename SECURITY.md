# Security policy

Secure Voice Agent is built around security controls, so reports about them are welcome and taken seriously.

## Reporting a vulnerability

Please report privately through GitHub: open the **Security** tab of this repository and choose **Report a vulnerability**. Do not open a public issue or pull request for a security problem.

Include what you found, how to reproduce it, and the impact you expect. You will get an acknowledgement within 5 business days and a fix or mitigation plan once the report is confirmed. Reporters are credited in the changelog unless they prefer not to be.

## Supported versions

Only the latest release (currently 0.7.x) receives security fixes.

## In scope

- Bypassing the policy gate, step-up verification, velocity limits, or per-call spending and action caps
- Payment links or tool actions reaching anyone other than the verified caller
- Card numbers, SSNs, or other identifiers leaking past the PII scrubbing into tools, logs, or summaries
- Forging or replaying signed tool requests, or tampering with the audit log undetected
- Prompt injection that makes the agent take an action its policy forbids

## Out of scope

- The hosted demo console's simulated services. It runs in your browser against fakes, with no real accounts or data.
- Vulnerabilities in third-party dependencies with no impact on this project's use of them (report those upstream).
- Findings that need an already-compromised host, admin credentials, or a deliberately insecure configuration.

The design assumptions and known gaps are documented in [docs/threat-model.md](docs/threat-model.md).
