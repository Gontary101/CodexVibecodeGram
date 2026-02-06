# Security Policy

## Supported Versions

Security fixes are provided for the latest code on `main`.

## Reporting a Vulnerability

Please report vulnerabilities privately.

Preferred channel:

- Open a private GitHub security advisory for this repository.

If private advisories are unavailable, contact the maintainer directly and include:

- Affected version/commit
- Reproduction steps
- Impact assessment
- Suggested fix (if available)

Do not post exploit details in public issues.

## Response Expectations

- Initial acknowledgment target: 72 hours
- Triage/impact assessment target: 7 days
- Fix timeline depends on severity and complexity

## Scope Notes

This project executes automation tasks and can run shell commands through Codex.
Please prioritize reports related to:

- Secret leakage
- Authorization bypass
- Unsafe command execution paths
- Artifact exfiltration outside intended roots
- Insecure defaults in deployment/configuration
