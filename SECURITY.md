# Security policy

AIST treats reports about tenant isolation, authorization, source-code access,
credentials, scan execution, and integration boundaries as security-sensitive.

## Report a vulnerability

Use GitHub's private vulnerability reporting for this repository:
[report a vulnerability privately](https://github.com/aist-ai-sast/aist/security/advisories/new).

Include the affected revision, prerequisites, impact, and a minimal reproduction
when it is safe to do so. Do not include secrets or data belonging to another
organization.

Please do not open a public issue, pull request, or discussion containing details
of a suspected vulnerability. If private reporting is unavailable, open a public
issue that asks the maintainers to establish a private contact channel without
including technical details.

## What happens next

Maintainers will acknowledge the report, validate the affected boundary, and
coordinate remediation and disclosure with the reporter. Exploit details and
unpatched implementation paths remain in the private advisory. Public release
notes and security documentation describe the affected security property and
the remediation after disclosure is safe.

The public security model is documented under [`docs/security/`](docs/security/README.md).
