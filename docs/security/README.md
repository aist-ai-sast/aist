# Security documentation

AIST security documentation is intentionally split by reader question. These
pages describe the public security model; they do not act as a list of active
vulnerabilities or remediation work.

| Question | Canonical page |
|---|---|
| What can each role do? | [Access control and roles](access-control-and-roles.md) |
| How is tenant data isolated? | [Tenant isolation and access](tenant-isolation-and-access.md) |
| Which boundaries and residual assumptions matter? | [Security boundaries and trust assumptions](threat-register.md) |
| How do I report a suspected vulnerability? | [Security policy](../../SECURITY.md) |

Security-relevant architecture and data flows remain with their owning topics,
including [runtime deployment](../architecture/runtime-deployment.md),
[source file access](../data-flows/source-file-access.md), and
[VPN-routed operations](../data-flows/vpn-routed-operations.md).

Public pages should be updated when a durable control, responsibility, or trust
boundary changes. Exploit details, affected implementation paths, and work in
progress belong in a private security advisory until disclosure is safe.
