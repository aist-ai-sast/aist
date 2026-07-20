# Threat-Model Scope

This baseline applies OWASP threat-model practice to the AIST operations that
accept source, make authorisation decisions, create findings, execute code, and
send data to configured integrations. It is maintained with the product and
data-flow pages, not as a one-time design report.

![Threat-model boundaries](../assets/threat-model-scope.svg)

## Assets under protection

The protected assets are organization-scoped project and finding data; uploaded
source archives and cloned source workspaces; repository, VPN, AI, work-item,
and notification credentials; pipeline reports and AI verdicts; and the
integrity of pipeline status and finding disposition.

## Boundaries that the model evaluates

The first boundary is the authenticated request entering the client and Django
application. The second is the organization and project scope used to select a
record. The third is background execution, where workers take queued work and
can create workspaces, contact integrations, or start containers. The fourth is
the container boundary around the SAST runtime and VPN sidecars. The fifth is
the named connection boundary to source-control, n8n triage, Slack/email,
work-item providers, and the local AI bridge.

## Scenarios covered by this baseline

The register covers tenant access, source archive and file access, integration
credentials, worker and container execution, AI callbacks and responses,
pipeline report import, and work-item/notification delivery. The linked flow
pages provide the exact trigger and durable outcome for these scenarios.

## How this stays current

Every change that adds a protected record, endpoint, worker task, integration,
container, callback, or source storage path must update its relevant product or
data-flow page first. The maintainer then checks whether the change alters an
asset, boundary, attacker capability, threat, mitigation, or validation case in
the [threat register](threat-register.md). A documentation change is required
when any answer is yes.
