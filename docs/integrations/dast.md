# DAST integration

The DAST integration connects one AIST organization to an external DAST
gateway. It supports autonomous scans and manual result import without making
DAST part of the SAST analyzer fan-out.

![AIST and DAST service boundary](../assets/aist-dast-service-boundary.svg)

## Responsibility boundary

| AIST owns | DAST owns |
|---|---|
| Organization access, project binding, launch admission, and pipeline history | Target policy, target-side execution, and provider progress |
| Encrypted integration credential and synchronized capability snapshot | Gateway authentication and the provider contract |
| Result identity validation, import, deduplication, and finding review | Raw evidence, terminal result, and report production |

The products do not share users, sessions, credentials, or a database. The DAST
gateway is an external HTTPS boundary, optionally reached through the VPN
selected on the DAST integration.

## Configure the integration

1. Import the provider-generated onboarding bundle into the organization.
2. Select an organization VPN when the gateway is on a private network.
3. Validate gateway connectivity and credentials.
4. Synchronize the provider's targets and capabilities.
5. Bind an eligible target and source identity to one AIST project.
6. Create a DAST launch configuration from the enabled binding.

Only one DAST integration is active for an organization at a time; disabled
records remain as history. The service token is write-only after import.

## Readiness

A launch is ready only when the integration is healthy, synchronized
capabilities are current, the project binding is enabled, the parameter set is
valid, autonomous execution is allowed, and a private gateway has its explicit
DAST VPN route.

The same readiness result is shown during configuration and checked again when
execution is admitted. A stale capability revision or changed provider schema
must be reviewed before the binding becomes ready again.

## Autonomous execution

An authorized start enters the normal durable launch lifecycle. After admission,
an AIST worker starts the standalone DAST connector. The connector starts and
observes the provider run, while AIST retains cancellation intent, pipeline
state, and the final import decision.

Before importing a terminal result, AIST verifies the expected provider run,
target binding, source revision, result format, and nested report. Invalid,
ambiguous, or oversized results fail before findings are persisted.

## Manual result import

A DAST result produced outside AIST can be imported without contacting the
gateway. A user with project-operate permission selects an enabled binding,
uploads the terminal result, reviews its validation preview, and confirms the
import.

AIST derives the source revision from the selected binding rather than accepting
an unrelated commit from the client. Confirmation creates an import pipeline
and applies the same result validation and finding lifecycle used by an
autonomous result.

## Network and credentials

Private DAST traffic uses only the VPN attached directly to the DAST
integration; project and SCM VPN configuration does not substitute for it. A
direct connection is allowed only for destinations accepted by the deployment's
public-endpoint policy: the gateway URL must be an absolute HTTPS URL with no
credentials, query, or fragment, on port 443 or 8443, and it must resolve to a
public address (or, when the integration's VPN is trusted, to an address inside
that VPN's private ranges). Every other destination is rejected before any
connection is attempted.

The service token and optional custom CA are loaded from the organization
integration when the connector starts. They are not launch parameters or
general broker payloads.

Use the [DAST operations runbook](../runbooks/dast-operations.md) for API
examples, cancellation, recovery, contract promotion, and token rotation. Use
the [staging compatibility canary](../runbooks/dast-staging-canary.md) before a
provider contract or deployment change is promoted.
